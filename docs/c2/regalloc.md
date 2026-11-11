# c2 register allocation: priority-based colouring (color.obj) and the scratch-register pass (regasg.obj)

This spec covers how cl 14.00.50727.762's back end (`Bin/c2.dll`) chooses registers for integer values. It is
written so that you can work out which register a value gets from the source of a small function.

Every claim cites a VA and function name. **[C]** means CONFIRMED: read in the code, or tested by a compile, or
both. Tests used `tools/msvc.py` listings and `tools/c2trace.py` traces (docs/c2/tracer.md), with probe sources in
`work/scratch/glass_regcolor_p/`. **[I]** means INFERRED.

Read docs/c2/il.md first. It covers BLOCK, TUPLE and the pipeline. Register allocation is phases 5b and 5c there.

---

## 0. Summary for decompilers

1. **There are two allocators.** `Color` (10b0cf40, color.obj) colours the *globregs*: user variables and
   compiler temporaries that live across instructions. It runs on the **whole function**, and it is a Chow-Hennessy
   style **priority-based** colourer with live-range splitting, not Chaitin simplify/select. After it,
   `RegSimpleAssignBody` (10bda210, regasg.obj) gives the short-lived expression temporaries (*tmpregs*) the
   registers that are still free. **[C]**
2. **Globregs are coloured one at a time, in descending priority.** Equal priorities are ordered by *def
   ordinal* (section 3.4). Within one block that means the value defined first goes first. Across blocks, the value
   whose def is in the later block goes first. **[C]**
3. **Priority** (`GLOBREG.prio`, +0x0c) is computed per block (section 3.2):
   `prio = Σ_B [ cost_B(v) · P_B · w_B ]  −  Σ_{B: v live through B, not referenced} P_B · w_B`.
   - `cost_B(v)` is 2 per reference at /O2: every def and every use.
   - `P_B` is the number of distinct globregs referenced in block B.
   - `w_B` is 2^(loop depth) at /O2 and 1 at /O1.

   In one straight-line block this is just 2·refs·P, so the order is by reference count. **[C]**
4. **The register is chosen** by `ChoosePhysReg` (10b14b70). It takes the register with the lowest weight among the
   value's still-available registers, trying them in the order **eax, ecx, edx, esi, edi, ebx, ebp**
   (`_IntRegOrder` 10ce59a0). The comparison is strict, so a tie goes to the earlier register. All weights start at
   0. They are lowered by the value's own copy-preferences and raised by its uncoloured neighbours' needs
   (section 4). **[C]**
5. **A call removes eax, ecx and edx** from the available set of every globreg live across it, so call-crossing
   webs get **esi, edi, ebx, ebp, in priority order**. This is the empirical M&B rule, and it is the code. It breaks
   down only when preferences or neighbour penalties are nonzero, or when the range is split or spilled. **[C]**
6. **Scratch temporaries at /O2 rotate eax → ecx → edx → eax**, using one cursor for the whole function in IL
   order (`_NextIntPos`). At /O1 every temporary takes the first free register, which is usually eax. **[C]**

---

## 1. Where it runs

`dop2_GlobalRegAlloc` 0x10c07350 runs: FgEarlyTailDup (/O2), **Globregs** 0x10b563f0, MdmDetermineDoubleAlignAndFpo,
MdmFixEbxIntrinsics, then **Color** 0x10b0cf40. After that, `dop2_CodegenUpdate` 0x10c07400 runs
**RegSimpleAssignBody** 0x10bda210, then frame layout (StkStackAlloc). This order is from docs/c2/il_pipeline.md
5.3. **[C]** Under /Od only RegSimpleAssignBody runs. **[C]**

| Pass | Module | What it does |
|---|---|---|
| `Globregs` 10b563f0 | globregs.obj | picks candidates (`GrgIdentifyCandidates` 10b562e0, `EnumerateSymbols` 10b568f0), reaching defs, enterloads (`Enterloads` 10b588e0: OPENTERLOAD 0x295 / OPEXITSAVE 0x296 at range boundaries), lifetimes (`ComputeLifetimes` 10b59a20), then `CreateGlobregs` 10b5abe0 (one GLOBREG per lifetime) and `RewriteAsGlobregs` 10b5ae50 (operands now point at the GLOBREG) **[C names/calls; per-step detail I]** |
| `Color` 10b0cf40 | color.obj | the colouring loop (section 3); x86 cost hooks are in colormd.obj (`Cmd*`) **[C]** |
| `RegSimpleAssignBody` 10bda210 | regasg.obj | assigns tmpregs one instruction at a time (section 6) **[C]** |
| `FPColor` 10c33550 | fpreg.obj | x87 stack registers, called from Color at 10b0d106 when FUNC+0xac & 0x20, which is not set by default; normal x87 code is class-1 GFx colouring plus X87MapGFxToSTx (x87.md) **[C]** |

**interf.obj is not register interference.** Its functions (`ItfBuildInterference` 10b73430, `ItfPasInterf`
10b72990, `ComputePointsAtSets` 10b79740, ...) build alias/points-at sets (PAS) for memory disambiguation.
Register interference is computed inside color.obj (section 3.3). **[C names and callers; I that no part of it
feeds Color directly]**

---

## 2. Data

### 2.1 Register numbers and tables (all read from the binary) **[C]**

c2 register numbers come from `_RegName` 10cec7b8: 1 eax, 2 ecx, 3 edx, 4 ebx, 5 esp, 6 ebp, 7 esi, 8 edi;
0x1f..0x26 GF0..7 (x87 pseudo), 0x4c..0x53 mm0..7, 0x54..0x5b xmm0..7.

| Global | VA | Contents |
|---|---|---|
| `_IntRegOrder` | 10ce59a0 | {1,2,3,7,8,4,6,0} = eax, ecx, edx, esi, edi, ebx, ebp (Color's try order, class 0) |
| `_MMXRegOrder` / `_XMMXRegOrder` | 10ce59c0 / 10ce59e4 | mm0..mm7 / xmm0..xmm7 |
| `_FPRegOrder` | 10d439b0 | set at run time to 0x2f (ST0) or 0x1f (GF0) by CmdPostspill-area code (10c20000.c:926/936) |
| `_RegOrders[7]` | 10ce5a08 | {Int, FP, 0, 0, 0, MMX, XMM}: indexed by register class |
| `_MaxPhysRegs[7]` | 10d0dd58 | initial {7, 8, 0, 0, 0, 8, 8}; slot 0 is overwritten by `CmdGetNumIntRegs` |
| `_IntPhysRegs` | 10d0cfd0 | {eax, ecx, edx, esi, edi, ebx, ebp, 0}: tmpreg order; RegSimpleAssignBody zeroes slot 5 (ebx) or slot 6 (ebp) when they are unavailable (10bda2c6..10bda2ea) |
| `_regAvail[9]` | 10d0f9f0 | per register number, 1 if usable this function |
| `_AvailPhysRegs[class]` | 10d0fa18 | SBV of usable registers per class; copied into each new globreg |
| `_RegSym[]` | 10d09f70 | 0x60-byte register symbols; `+0x1c` = number, `+8` = self (tracer.md) |
| `_ChoosePhysReg_weights` | 10d14f10 | int[0x5d], cleared per call (memset 0x174 at 10b14b85) |
| `_SortedGlobregs` | 10d1508c | the priority list being coloured |
| `_GlobregHash[0x400]` | 10d15090 | buckets by `id & 0x3ff`, chained through +0x30 |
| `_NewGlobregs[7]` | 10d12278 | per-class SBV of globreg ids still to colour |
| `_UpdatePrefsBv` | 10d15088 | copy partners of the current globreg (section 4.3) |

**Availability** (`CmdAdjustIntRegAvail` 10c23320, `CmdGetNumIntRegs` 10c23380) **[C]**:

- regAvail starts with every register at 1 except esp.
- ebx is cleared when `MdmWillUseEbxConservative` (10c58980) returns nonzero.
- ebp is cleared when FUNC+0xac & 0x20000000, or when FUNC+0xac & 0x40 is clear. 0x40 is the FPO / no-frame-pointer
  bit **[I]**.
- `CmdGetNumIntRegs` returns the count and sets or clears the ebx bit (0x10) in `AvailPhysRegs[0]`.
- Color calls it only when class 0 is reached, after `MdmDetermineDoubleAlignAndFpo` (10b0d119; the call is at 10b0d120). So whether
  ebp is a colour is decided just before the integer pass. **[C]**

### 2.2 GLOBREG (one live range; `GrgNewGlobreg` 10b5b7b0)

```c
/* Shares its header with REGSYM (_RegSym entries): +4 kind, +8 self, +0x1c number. */
typedef struct PREF {              /* getmem'd in Spillcosts / ChoosePhysReg */
    struct PREF*  next;            /* 0x00 */
    struct REGSYM* reg;            /* 0x04 physical register (&_RegSym[n]) */
    int           count;           /* 0x08 number of copies with that register */
} PREF;

typedef struct GLOBREG {
    struct SYM*   sym;             /* 0x00 the variable (sym->+0x38 canonical sym if set) [C] */
    u8            kind;            /* 0x04 = 2 (REGSYM has 1); operands test this to tell pseudo from physreg [C] */
    u8            fl5;             /* 0x05 1 new (cleared by InsertInList), 2 split handled, 4 must-enregister /
                                      not foldable, 8 no recalc, 0x40 deferred once (benefit <= 0),
                                      0x80 needs a byte register [C bits used; meanings I] */
    u8            fl6;             /* 0x06 Spillcosts scratch: 8 on the died-in-block list, 0x10 referenced in
                                      the current block; bit 1 checked by SpillOrDefer [C] */
    u8            f07;
    struct GLOBREG* self;          /* 0x08 [C] */
    int           prio;            /* 0x0c sort key (section 3.2) [C] */
    struct REGSYM* color;          /* 0x10 0 = uncoloured; else &_RegSym[reg] (10b14e24..10b14e2a) [C] */
    struct GLOBREG* next;          /* 0x14 SortedGlobregs link; Spillcosts list scratch [C] */
    union { struct GLOBREG* prev;  /* 0x18 SortedGlobregs back link [C] */
            int blkcost; };        /*      Spillcosts: unweighted cost in the current block, reset per block [C] */
    u32           id;              /* 0x1c from _NextGlobregNum 10d12270 or reused from _FreeGlobs [C] */
    struct SBV*   avail;           /* 0x20 registers still allowed; empty => uncolourable [C] */
    int           nrefs;           /* 0x24 references counted by Spillcosts (SubsumeGlobregs reuses it as a def
                                      count and zeroes it) [C] */
    struct BLOCK* firstBlk;        /* 0x28 first block of the range in block-list order [C] */
    struct BLOCK* lastBlk;         /* 0x2c last block [C] */
    struct GLOBREG* hashNext;      /* 0x30 _GlobregHash chain; _FreeGlobs link [C] */
    u8            nEnter;          /* 0x34 enterload/exitsave count; SpillOrDefer spills when 0 [C use, I name] */
    PREF*         prefs;           /* 0x38 [C] */
    struct TUPLE* lastDef;         /* 0x3c last def instruction seen (fold checks) [C] */
    int           benefit;         /* 0x40 weighted savings Σ cost·w (no P) [C] */
    u32           defOrd;          /* 0x44 tie-break: def ordinal (section 3.4) [C] */
} GLOBREG;                         /* at least 0x48 bytes */
```

### 2.3 BLOCK fields used by Color (`CreateBlockBvs` 10b0d990, `ChoosePhysReg` 10b14b70) **[C use, I names]**

| Off | Meaning |
|---|---|
| +0x40 / +0x44 | registers of coloured globregs live into / out of the block (one-word bit vectors) |
| +0x48 / +0x4c | SBV of globregs live into / out of the block |
| +0x50 | SBV of globregs present in the block (CreateBlockBvs copies it at Color entry) |
| +0x54 | registers taken in the block by coloured globregs |
| +0x17e | **loop nest level** (short). `LgFindExitBlocks1` 10b87b10 writes it: 1 for a top-level loop, +1 per nesting level, 0 outside loops (`LgBuildLoopGraphMinimal` 10b85e10 passes 1 and 2) **[C]** |

---

## 3. The colouring loop

### 3.1 Order of operations (`Color` 10b0cf40) **[C]**

```text
if every _NewGlobregs[c] is empty: NoColoring = 1; FPPeeps; return
SubsumeGlobregs; FoldTooCheapDefs; CreateBlockBvs
if FUNC+0xac & 0x20: FPPeeps or FPColor        // not taken by default (x87.md): FP values are class 1 below
for class c = 6 down to 0:                       // class 1 = x87 virtual GF0..GF7 (mapped to the stack later by
                                                 //   X87MapGFxToSTx, x87.md); class 0 = int, coloured last
    if c == 0: MdmDetermineDoubleAlignAndFpo; MaxPhysRegs[0] = CmdGetNumIntRegs()
    if NewGlobregs[c] is empty: continue
    InitialSplitPoints(...)                       // 10b0ded0
    Spillcosts(...)                               // 10b10420: avail sets, prefs, prio, benefit
    SpillObvious(...)                             // 10b11560
    OrderGlobregs(c)                              // 10b11770: builds _SortedGlobregs
    while SortedGlobregs:
        if NeedNewLives after a split: RemoveSplitGlobregs, ColProcessSplitGlobregs,
            Spillcosts and SpillObvious again, InsertNewGlobregs   (10b0d270..10b0d330)
        gr = pop head                                             (10b0d33b)
        if gr.benefit < 0
           or (gr.benefit == 0 and not (firstBlk == lastBlk and sym kind != 0xd
                                        and nrefs > 2 and _Speed)):  (10b0d350..10b0d376)
            SpillOrDefer(gr); continue
        if !(gr.fl5 & 4) and BetterToDefer(gr):                    (10b0d388..10b0d3b6)
            if gr.avail: SortedGlobregs = InsertInList(gr)          // re-queued at new prio
            continue
        if gr.avail is empty: HandleCurrUncolorable(gr); continue  (10b0d3bb..10b0d3c5)
        mark gr coloured; nbrs = InsertSplitPoints(...)            (10b0d464)
        ChoosePhysReg(nbrs, gr, c)                                 (10b0d471)
        UpdateAffectedGlobregs(..., gr, nbrs, ...)                 (10b0d480)
RewriteAsReal                                                       // operands -> physical regs
```

x87: by default (`-QImapX87`, `_Module+0x44c & 0x8000000` set) neither FPPeeps nor FPColor runs here; class 1 (FP) is
coloured by this same loop as GF0..GF7 and mapped to stack slots later. See docs/c2/x87.md.

`_Speed` (10d0d71c) is nonzero under /O2 and zero under /O1. **[C use; that /O1 clears it is C by the /O1 trace in 3.2]**

### 3.2 Priority and benefit (`Spillcosts` 10b10420) **[C, code and trace]**

Spillcosts walks blocks in block-list order (FUNC+8). In each block it walks instructions forward, from
`BLOCK.first` (+0x1c) to `BLOCK.end` (+0x20). For each block B:

- **Block weight** w (10b106d9):
  - /O1: w = 1.
  - /O2: w = `1 << BLOCK+0x17e`, so 1 outside loops, 2 in a loop and 4 in a nested loop.
  - PGO (`_PogoFlag` == 2): w = count·10000/maxcount + 1.
- **Reference cost** c. It goes into `blkcost` (+0x18). Also `nrefs` (+0x24) += 1 and `benefit` (+0x40) += c·w
  (10b10952, 10b109c0, 10b10f80, 10b11002).
  - **/O2: c = 2 for every def and use** of a non-immediate globreg. `CmdLoadCost` 10c23740 returns 2 (1 for an
    immediate candidate, sym kind 0xd). `CmdSubCost` 10c237c0 returns 2 when the operand could be used directly
    from memory. A def that `CmdCanDirectSubstitute` 10c23980 rejects also costs 2.
  - /O1: c = 3 + (ebp available) for stack-frame variables (sym kinds 3..6), 6 otherwise. Direct-substitute uses
    and defs cost ebpAvail + 1, or 4. Immediates are priced by size. Traced: a stack local with ebp available costs
    2 per reference.
  - OPENTERLOAD 0x295 (the load into a register at the range entry) *subtracts* loadcost·w from benefit. OPEXITSAVE
    0x296 subtracts 2 at /O2 (10b108d4).
- **Pressure** P_B = the number of distinct globregs of this class referenced in B. This counts the ones that died
  in B, the live-out ones referenced in B, and immediate-candidate globregs (traced: an extra globreg of sym kind
  0xd, the constant of `cmp gx,0`, was counted) (10b113cf..10b11484).
- **At the end of B** (10b11484..10b114e8):
  - Every globreg referenced in B gets `prio += blkcost·(P_B·w)`, then `blkcost = 0`.
  - Every globreg live through B but not referenced in it gets `prio -= P_B·w`. Under PGO it gets `prio -= w`.

So:

```text
benefit(v) = Σ_B Σ_{refs r of v in B} c(r)·w_B                      (- enterload cost)
prio(v)    = Σ_{B ref v} (Σ_{refs in B} c(r))·P_B·w_B  −  Σ_{B live-through, unref} P_B·w_B
```

**Checks (traces of `@InsertInList@8` entry and a probe at 10b11487).** The numbers below are what the formula gives
and what the trace shows; in every case the two agree.

| Probe | Blocks (P, w) | Prediction = trace |
|---|---|---|
| t2.c: one block, refs a..d = 2,3,4,5 | (4,1) | prio 16, 24, 32, 40; benefit 4, 6, 8, 10 |
| t6.c: `if (gx) { 6 × g(a) }`, then `g(b)×2, g(c), g(d)` | (4,1) (1,1) (3,1) | a = 8+12 = 20, b = 8−1+12 = 19, c = d = 8−1+6 = 13 |
| t1.c: `do { g(a); g(a); } while (gx);` | (4,1) (1,1) (2,2) (0,1) (3,1) | a = 8−1+16 = 23, b = 8−1−4+18 = 21, c = d = 9 |
| t1.c at /O1 | w = 1 everywhere | a's benefit = 6 (no loop weight) |

### 3.3 Interference and available sets **[C for the set operations; I for some event names]**

The colourer has no explicit interference graph. Each globreg carries `avail` (+0x20), and neighbours are found
while the globreg itself is being coloured.

1. **Initial avail** = `AvailPhysRegs[class]`, copied in `GrgNewGlobreg` 10b5b7b0. **[C]**
2. **Calls.** For an operand of kind 0xb (the register-kill set that a call or OPKILLREGS carries),
   `avail -= kills` for every globreg live at that point (10b1137e..10b11396). This is why call-crossing values
   lose eax, ecx and edx. If the callee is visible in the same TU, the kill set can be smaller **[I]**: see
   `MscRecordInterProcRegInfo` in il_pipeline, and the M&B 6.3 / vs2005 "callers see a same-TU callee's
   register use" levers.
3. **Fixed registers.** At a def of a physical register (an operand of kind 1 whose register is in AvailPhysRegs),
   that register is cleared from the avail set of every live globreg. The exception is the source of a
   `mov phys, gr`-class copy: that copy records a preference instead (10b10b8b..10b10bdb, 10b10dd5 ff). At each
   def of a globreg, `avail -= ` the registers live there (`SbvMinus` 10b11093). **[C op; I operand set]**
4. **Globreg–globreg interference** is found by `InsertSplitPoints` 10b13460, which walks the blocks of the
   current range. **[C structure, I precise rule]**
   - At each def of the current globreg, it ORs into `nbrs` the set of globregs live there.
   - At each def of another globreg while the current one is live, it adds that globreg to `nbrs`, unless the def
     is a copy (`mov`, `movss`/`movsd`/`movaps`/`movapd`/`movdqa`/`movq`, opcodes 1, 0x125, 0x189, 0x11f, 0x17f,
     0x181, 0x1fd) from the current globreg. This is the Chaitin copy exception.
   - After the choice, `UpdateAffectedGlobregs` 10b15470 clears the chosen register from the `avail` of every
     member of `nbrs` (10b155xx loop). **[C]**
   - A neighbour whose avail becomes empty is folded away (`FoldAwaySplitGlobreg` 10b19ed0), recalculated
     (`TryToRecalc` 10b1a180), or split (`InsertAdditionalSplits` 10b15d50). **[C calls]**
5. **Copy coalescing before colouring.** `SubsumeGlobregs` 10b0fdb0 merges `mov x, y` pairs where a side has a
   single def and the other is not killed in between (`ComponentKilled` 10b19fe0). **[C partial read; exact
   condition I]**

### 3.4 The sort order and the tie-break (`OrderGlobregs` 10b11770, `InsertInList` 10b11970) **[C]**

- OrderGlobregs visits `_GlobregHash` buckets 0..0x3ff and each chain (+0x30) in turn. It inserts every globreg of
  the class with InsertInList (eax = gr, edi = head; it returns the head).
- A globreg whose avail is already empty is first split (`InsertAdditionalSplitsOne` 10b16720).
- InsertInList keeps the list sorted by **prio descending (signed), then defOrd descending (unsigned)**: `jg` at
  10b11988, `jae` at 10b11992. When both keys are equal, the newcomer goes before the existing entry, so a full tie
  is won by the globreg **inserted last**. Full ties occur between immediates (defOrd 0) and between split pieces,
  which copy their parent's defOrd (`NewLiveRanges` 10b12480, `RewriteSplitGlobregs` 10b12d20). Insertion order:
  - the initial list: OrderGlobregs visits buckets `num & 0x3ff` ascending, so the **higher bucket wins**;
    within a bucket the chain is visited head first (which one wins there is not yet distinguished by any trace);
  - pieces made by a split round are inserted afterwards by `InsertNewGlobregs` 10b133e0, which walks the split
    set in **ascending web number** (`SbvNext`), so a later round's pieces win over earlier entries, and among one
    round's pieces the **higher `num` wins**.
  `rasim check` with these rules: 29/29, 69/69 and 69/69 pops on the three Fallout 3 snapshots, 2546/2548 on the
  26k-instruction synthetic (work/scratch/big/b250.c). **[C]**

**defOrd** (+0x44) is written by `RewriteAsGlobregs` 10b5ae50:

- A counter starts at 0 per function and is incremented once per instruction (10b5b04c).
- Blocks are walked in **block-list order**. Instructions within a block are walked **backwards**, from `end->prev`
  (TUPLE+0x10) down to `first`.
- At every def of a globreg, `defOrd = counter` (10b5b099), so the last write wins. For a single-def value this is
  its def's ordinal.

Consequences, each tested with the tracer and the listing **[C]**:

- **Within one block, the earlier def has the larger defOrd and wins a tie.** In t2.c, a..d have defOrd 0x30, 0x2c,
  0x28, 0x24. p1.c f3 shows four tied webs taking esi, edi, ebx, ebp in definition order.
- **Across blocks, a def in a later block wins.** In t5.c, a, c and b all have prio 12. b is defined after an `if`
  and gets defOrd 0x22, against 0xd for a and 9 for c. b is coloured first (esi), a second (edi). c is coloured
  third and gets esi again, because it does not interfere with b.
- `id` (+0x1c) decides only full ties. Ids are given in `CreateGlobregs` order, which follows the symbol table, not
  the source. In every probe here they were the *reverse* of definition order.

### 3.5 Deferral, spilling, splitting **[C structure]**

- **benefit ≤ 0** → `SpillOrDefer` 10b0d690.
  - If the globreg is not restricted to a register (`RestrictToReg` 10b1b430), the first time it gets fl5 |= 0x40
    and `InsertByBenefit` 10b119f0 moves it behind the regular entries. Those deferred entries are kept in
    ascending spill-cost (+0x40) order (a newcomer goes before the first deferred entry whose cost is ≥ its own),
    with immediates (sym kind 0xd) appended at the very end. **InsertByBenefit copies the priority (+0x0c) of the
    entry it is linked next to into the re-inserted web** (10b11a24, 10b11a63), so a deferred web pops a second
    time showing its neighbour's priority (often the list's last value, e.g. -11), not a Spillcosts total. `regs`
    shows these as a second pop of the same globreg; `rasim check` excludes them from the totals and checks their
    order separately. **[C]**
  - The second time it is spilled (`Spill` 10b17220) if its range is one block or nEnter == 0. Otherwise it is cut
    into single-block lives (`ForceSingleBlockLives` 10b1b3b0, or `ForceNoAirlocks` 10b1b280), and the same is done
    to any later deferred globreg with benefit ≤ 0.
- **benefit == 0 is still coloured** at /O2 when the range is one block, the value is not an immediate, and it has
  at least 3 references (10b0d357..10b0d376).
- **Foldable defs** (`BetterToDefer` 10b19de0) are values that can be re-materialised from components, such as
  address arithmetic (`CmdGetComponents` 10c24590, `ComputeFoldLegalityAndWorth` 10b191e0). For class 0 their prio
  becomes `min(prio of components) − 1` and they are re-queued, so they are coloured right after their components.
  Otherwise the def is folded (`DoFoldDef` 10b195e0) and the globreg is removed.
- **Uncolourable** (avail empty when popped): `HandleCurrUncolorable` 10b0d830 splits the range
  (`InsertAdditionalSplitsOne`), or with fl5 & 0x80 (byte register) runs `CmdSpillImmedBytes`. Then it tries
  recalculation (`CanRecalc`/`TryToRecalc`).
- Split points and the register-pressure counts behind them (`InitialSplitPoints` 10b0ded0, `InsertSplitPoints`,
  `CmdReduceSplits` 10c24aa0) are not specified here. See section 9.

---

## 4. Which register: `ChoosePhysReg` 10b14b70 **[C]**

Arguments: eax = `nbrs` (an SBV of uncoloured interfering globreg ids from InsertSplitPoints), `[esp+4]` = gr,
`[esp+8]` = class.

```text
weights[*] = 0                                                  10b14b85
for each n in nbrs with n.benefit > 0:                          10b14bd2
    for each PREF p in n.prefs: if p.reg in n.avail: weights[p.reg] += p.count     10b14c2c
    if |n.avail| == 1 (the only register r):       weights[r] += n.benefit * 100   10b14ca7
for each PREF p in gr.prefs:                       weights[p.reg] -= p.count        10b14d59
best = none
for r in _RegOrders[class] (eax, ecx, edx, esi, edi, ebx, ebp):                    10b14d6a
    if r in gr.avail and (best == none or weights[r] < weights[best]): best = r    10b14df1 (jge)
gr.color = &_RegSym[best]; free gr.avail
for blocks firstBlk..lastBlk: record best in BLOCK +0x54/+0x40/+0x44 where gr is present/live-in/live-out
for each m in _UpdatePrefsBv with m.color == 0: m.prefs[best].count += 1            (10b15152..)
```

### 4.1 Where preferences come from (`Spillcosts`, `InsertSplitPoints`) **[C]**

- A copy (opcodes 1, 0x11f, 0x1fd, 0x17f, 0x181, 0x125, 0x189) between an uncoloured globreg and a physical
  register or coloured globreg adds 1 to the globreg's PREF for that register. Typical sources: `mov gr, eax` after
  a call, `mov eax, gr` before `ret`, and arguments arriving in ecx/edx under fastcall or thiscall.
- `lea gr, [base]` (0x12) with a base that is a register adds a preference for that register.
- A copy between the current globreg and another *uncoloured* globreg puts the partner into `_UpdatePrefsBv`. Once
  the current globreg is coloured, the partner gets a preference for the same register, which is coalescing by
  preference.

### 4.2 The resulting rules

- With all weights 0, the first available register in eax, ecx, edx, esi, edi, ebx, ebp wins. This is the tracer
  demo `sum` (tracer.md). **[C]**
- A call-crossing value has only esi, edi, ebx, ebp available (3.3.2), so the n-th call-crossing web in colouring
  order takes the n-th free of esi, edi, ebx, ebp. **[C, p1 to p6]**
- **A copy with a register moves the choice.** t3.c `__fastcall f(x, y)`: y arrives in edx and is reassigned
  `y = g(y)` (eax). Its weights are eax −1 and edx −1 (ecx is not available). eax wins the tie by order. The listing
  has `mov eax, edx` at entry. **[C trace]**
- A value avoids a register that an uncoloured neighbour prefers: +count per preference. It strongly avoids a
  neighbour's last remaining register: +100·benefit. **[C code; not separately probed]**

---

## 5. Recipe: predicting the callee-saved assignment of a small function (/O2)

1. List the webs that live across a call. A reassignment like `x = g(x)` starts a new web, because
   `CreateGlobregs` makes one per lifetime.
2. Split the function into blocks at branch targets and conditional jumps. Calls do not end a block. Give each
   block w = 2^(loop depth) (/O1: 1) and P = the number of distinct globregs referenced in it. Count every
   globreg, including values that are not live across a call, loop counters, and immediates used more than once.
3. prio = Σ 2·refs_B·P_B·w_B − Σ_{live-through, unreferenced} P_B·w_B.
4. Sort by prio descending. Break ties by defOrd: within a block the earlier def wins; across blocks the later
   block wins.
5. Assign the first free of esi, edi, ebx, ebp, skipping registers taken by an interfering web already coloured.
   Remove ebx or ebp when they are unavailable (2.1). Nonzero preferences (4.1) can override the order.
6. More webs than registers means deferral, splitting and spills (3.5). The recipe no longer predicts then.

Probe results, all as predicted (sources in work/scratch/glass_regcolor_p/; `regs.sh` lists the register each def
of a, b, c, d moves into, in order):

| File | Variation | Threshold seen | Formula |
|---|---|---|---|
| p2.c | n × `g(a)` in an if-arm vs `g(b)×2` after the join | a overtakes b at n = 6 | 8+2n ≥ 19 ⇔ n ≥ 6 |
| p3.c / p5.c | n × `g(a)` in a do-while vs k × `g(b)` after it | n=1: k ≤ 2; n=2: k ≤ 3 | X = P·w = 4 (P=2 with the immediate 0, w=2): a−b = 3X−6k (n=1), 5X−6k (n=2) |
| p6.c | `g(a)` in a nested do-while | a first for k ≤ 4 | X = 8: 3·8 ≥ 6k |
| p3.c at /O1 | the same | the loop does not count | w = 1 |

---

## 6. Scratch temporaries: `RegSimpleAssignBody` 10bda210 / `RegAssign` 10bda7c0 **[C]**

This pass runs after Color. It walks the function's tuples in order. On each block tuple (kind 0x1a) it clears
`_RegMap` (`RegResetRegMap` 10bdc830). For each instruction it runs `RegAssignSrcs` 10bda4a0, then
`RegAssignDsts` 10bda5c0. The conflict sets `_BvConflicts[reg]` come from `RegBuildGlobalConflict` 10bdba20.

`RegAssign` picks a register for a tmpreg in this order:

1. **Preferred register** (`tmp+0x30`). It is set when the def is a copy from a register of a compatible type, or a
   `lea` from a base register. The preferred register is taken if it is free in RegMap, available in regAvail, and
   not conflicting (10bda7c0..10bda8a0).
2. **/O2 (`_Speed`): round robin over eax, ecx, edx.** The scan starts at `CmdGetNextPos()` (`_NextIntPos`) and wraps
   when the entry is 0 or greater than 3 (the scratch registers). It takes the first register that is free and not
   in `_BvConflicts`. Then it sets `_NextIntPos` to the following entry (`CmdSetNextPos` 10c23520). The cursor is
   reset only at function start (10bda2f0: `_NextIntPos = &_IntPhysRegs`) and at EnC 0x303 tuples.
3. Otherwise, including always at /O1: **first fit** over `_IntPhysRegs`: eax, ecx, edx, esi, edi, [ebx], [ebp].
4. Then first fit ignoring `_BvConflicts` but checking `TupFindDstRegInterf`. Last, spill (`RegSpill` 10bdad70) the
   register whose next use is farthest (`RegFindPhysRegUse` 10bdacc0).

Tested with t4.c, `x1 = s[0]; ... x5 = s[4];`: /O2 gives eax, ecx, edx, eax, ecx. /O1 gives eax five times. **[C]**


## 7. Which values reach Color: the candidate rule (`GrgIdentifyCandidates`, `EnumerateSymbols`) **[C]**

Every value that needs a register goes down one of two paths. **Globregs** are coloured by priority (sections 3
and 4) and never touch the scratch cursor. **Tmpregs** are given registers afterwards by regasg's rotation
(section 6). Which path a value takes is decided by symbol kind, not by how it is used:

1. `GrgIdentifyCandidates` 10b562e0 (run in `dop2_PreLowerGlobalOpts`, before Lower; il_pipeline.md §5.3) walks
   every instruction's src and dst operands. A memory-variable leaf (tuple kind 2, OPLOCAL/OPGLOBAL) is turned
   in place into a candidate register leaf (kind 1, OPREG 0x279, register = the dummy SYM 10d0d7e8) when all of
   these hold (10b5634c..10b563cd):
   - the leaf is not volatile (leaf+0x14 & 0x40 clear);
   - the symbol's equivalence base has an SSR (`[[sym+8]] != 0`: a user variable or parameter), **or** it is a
     kind-3 temporary with no single defining tuple (`sym+0x14 == 0`);
   - its type is enregisterable (byte table 10ceb338 indexed by `TyGetIndex`) and not x87 (`type & 0xf000 !=
     0x5000`; x87 values go to FPColor);
   - its kind is not 0xa (function);
   - globals 10d0ddac == 0, 10d0d708 != 0 and 10d07a24 == 0 (meanings not identified **[I]**).
   A call's target operand is skipped (kind 0xf with the leaf at src[0]), as is opcode 0x2d9 with +0x30 & 2.
2. `EnumerateSymbols` 10b568f0 then gives each base symbol a candidate bit (10b56956 switch on the kind byte):
   - kind 3 (temporary): **only if `+0x14 == 0`** (no single def); otherwise it is counted as excluded;
   - kinds 4 and 5 (local, parameter): always; address-taken ones (`+5 & 2`) also go into the aliased set
     (`_GrgBvAliasedCands`, which GrgAvoidLousyLoadsInLoops uses);
   - kinds 7 and 8: always, in the aliased set;
   - kind 0xa: functions whose SSR is dllimport (attr 0x400) without a body (0x4000), each member of size 4.
     These are the `__imp_` pointers that appear as globregs, e.g. `_imp__FUN_00d9b080@4` (`k10` in `regs`);
   - anything else, or a type the table rejects: not a candidate.

**Which temporaries have no single def.** Temporaries made by the optimiser to hold a value across instructions
are temp *variables* (`TupMakeTmpVar`, OPLOCAL, printed `tv<key>`): common-subexpression temps, loop-invariant
hoists (Fallout 3 0x00a89680: `lea t243 = center+100` in the preheader), and `a[i]` reused in the demo `sum`
(`t132`: SyDef 0 at Color, traced). They become globregs. Temporaries made when expressions are lowered
(`TupMakeTmpReg`, one def recorded in `+0x14`) are tmpregs and always go to the scratch rotation.

**Consequences for matching:**
- A value loaded and used once (`push this->name`, `f(p->x)`) is a tmpreg. It takes the next scratch register
  in the eax/ecx/edx rotation and moves the cursor.
- The same value read through a named local or a reused local is a globreg. Color gives it the cheapest free
  register (usually eax, if nothing interferes), and the cursor does not move. Seen in Fallout 3 0x008e7d40:
  `char* nm = name; FUN_005c8b00(nm, ...)` turned `mov ecx,[esi+0x2a0]; push ecx` into `push eax`.
- The extra symbol is not free. It adds a web to Color, which can change other variables' live ranges and
  priorities. In 0x008e7d40 it merged `this`'s two lifetimes (priority 64 and 40) into one (83), which then took
  edi instead of esi. That is a callee-saved swap across the whole function.
- How the extra symbol caused that, traced step by step (0x008e7d40, `regs --why this,scene` per Spillcosts pass):
  live-range splitting is driven by **colouring order**. When a globreg is coloured, `InsertSplitPoints` 10b13460
  (via `ColMakeSplitPoint` 10b146c4) splits the ranges of uncoloured neighbours, and `InsertAdditionalSplits` /
  `ColProcessSplitGlobregs` / a new Spillcosts pass turn the pieces into new globregs. A variable coloured
  *before* its neighbours keeps one range; one coloured after them is split. In the baseline, `this` was 79 in pass
  2, tied with `scene` (79), and `scene` wins the tie on defOrd (140 > 7, §3.4). So `this` was split later into an
  esi piece (40) and an edi piece (64), as in the original. The named local at site 1 raised the pressure of block
  12 from 5 to 6. `this` has two references there (cost 4), so it gained exactly +4 = 83 and was coloured before
  `scene`, whole, in edi. Rule: **a new globreg raises the priority of every variable referenced in its blocks by
  (that variable's cost in the block) × w**, which can reorder near-ties far away.
- An optimiser temp appears only when the optimiser can reuse a value. Two reads of `this->name` with a call
  between them are not CSE'd, because the call may write `name`.

---

## 8. Mapping to the empirical levers

M&B docs/codegen.md was measured on **cl 13.10 (VC7.1)**, not on this c2: docs/codegen.md line 152 and M&B 1.1.
Agreement there is evidence that the algorithm family is the same, and a disagreement may come from the version.

### Explained

| Lever | Explanation |
|---|---|
| M&B 7, "Callee-saved register order", rule 1 (order esi, edi, ebx, ebp; reference-count priority; ties to first defined) | `_IntRegOrder` minus the call kills (2.1, 3.3); prio = 2·refs·P in one block (3.2); defOrd tie-break (3.4). "Ties to first defined" holds **within a block** only. |
| rule 2 (what counts: `x += y` is 2; `x = q(x)` is a new web; naming a call result adds its copy) | each def and use costs 2 (3.2); one GLOBREG per lifetime (`CreateGlobregs`); the `mov v, eax` after a call is a def reference, and a bare call result is a tmpreg, which is not a globreg **[I for the last part]** |
| rule 3 (a use under an `if` is worth about 1/2 or 1/3, boundaries a little above the exact fractions) | the P_B factor and the live-through penalty (3.2). Thresholds are exact for this c2 (section 5); the fractions approximate P_arm / P_main |
| rule 4 (in a switch, each case's values take the free registers in priority order) | webs dead in a case don't occupy its registers; each case's webs are coloured by prio among what is left |
| rule 5 (the 70 KB interpreter depends on function-wide state, and removing cases changes it non-monotonically) | prio is a sum over **all** blocks: long-lived webs pay −P_B·w_B in every case they pass through unused, P_B depends on every other web in B, and with more than 4 call-crossing webs, deferral and splitting start (3.5) |
| M&B 6.1 ("whole-function, keyed by internal ids") | whole-function: yes. "Ids": the key is prio, then defOrd (an instruction ordinal); the globreg id breaks only full ties |
| M&B 6.4 / 6.5 (scratch rotation; a chain from the first temporary; one extra temporary rotates later code) | `_NextIntPos` round robin over eax, ecx, edx, one cursor per function in IL order (section 6) |
| M&B 6.3, 6.5 "Callee visibility", vs2005 "Callers see a same-TU callee's register use" | call kill sets subtract from avail (3.3.2); a visible callee's smaller kill set leaves eax, ecx or edx available across the call **[I mechanism]** |
| M&B 7 "naming a global's re-read raises its priority" | the named local is a globreg with more references, while a re-read is a fresh load into a tmpreg |
| vs2005 "Declaration order of locals moved registers" (`strqlen`) | declaration order itself is not an input. Moving a declaration with an initializer moves the def, which changes defOrd and the block it is in **[I]** |

### Contradicted or refined

- **M&B rule 3, "Loop bodies don't count more"**: false for cl 14 at /O2. A reference in a loop body is weighted
  w = 2 per nest level (`1 << BLOCK+0x17e`, 10b106d9; p3, p5, p6). It is true at /O1, where `_Speed` = 0 and
  w = 1, and possibly for cl 13.10. vs2005-cpp's cl.exe is built /O1, so there it holds.
- **"Ties go to the web defined first"** is wrong across blocks. A def in a later block (in block-list order) wins
  (t5.c).
- **M&B 6.2 "commutative and SIB order keyed by ids"** is not part of this allocator. Nothing here orders operands.
  Where 6.2 says the cause is a temporary's id, the register side of that may come from defOrd or the tmpreg cursor
  **[I]**.

### Not explained here

- vs2005 "Declaration order sets the order of zero-initialised registers" (`tail = 0; head = 0` → xor ebp, ebx,
  edi). The values involve zero constants, which may be immediate-candidate globregs with their own costs and
  folding (`FoldTooCheapDefs` 10b0da80). Not probed.
- M&B 15, reload order after a loop (`Terrain::tessellate`): reloads are spill and split code (`Spill`,
  `InsertSplitPoints`), which this spec does not cover.

---

## 9. Open questions

1. The exact split-point rule (`InitialSplitPoints` 10b0ded0, `InsertSplitPoints` 10b13460, `CmdReduceSplits`
   10c24aa0) and when `NeedNewLives` (10d0d690) forces a recompute. Only these decide functions with more than
   about 4 call-crossing webs.
2. Which operand set `SbvMinus` subtracts at each def (10b11093). It is presumed to be the physical registers live
   at that point.
3. `Globregs` candidate selection: which variables become globregs, and which stay in memory or as tmpregs
   (`GrgIdentifyCandidates`, `GrgSpillCheapSyms` 10b5c160, `Enterloads`).
4. `SubsumeGlobregs`'s exact conditions, and how coalescing changes refs and P.
5. FPColor (x87), and the byte-register path (fl5 & 0x80, `CmdProcessBytePressure` 10c24fe0).
6. Whether cl 13.10 (M&B) has the same formula without loop weighting. This c2 cannot test that.
