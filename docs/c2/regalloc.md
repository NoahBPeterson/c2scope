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
   ordinal*, larger first (section 3.4). Within one block that means the value defined first goes first. Across
   blocks, the value whose def is in the later block goes first. The rule is the same for the int, x87 and XMM
   classes and on c2 13.10, 14.00 and 15.00 (probe `tests/data/tieorder.c`). **[C]**
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
   (section 4). Replayed from traces, this rule gives the register of every integer colouring checked (1,500+,
   section 4). **[C]**
5. **A call removes eax, ecx and edx** from the available set of every globreg live across it, so call-crossing
   webs get **esi, edi, ebx, ebp, in priority order**. This is the empirical M&B rule, and it is the code. It breaks
   down only when preferences or neighbour penalties are nonzero, or when the range is split or spilled. **[C]**
6. **Scratch temporaries at /O2 rotate eax → ecx → edx → eax**, using one cursor for the whole function in IL
   order (`_NextIntPos`). At /O1 every temporary takes the first free register, which is usually eax. **[C]**
7. **A value is cut in two when a neighbour takes its last register**, at that neighbour's colouring, not when its
   own turn comes (3.6.4). **A value whose register would save nothing** (benefit <= 0: one use behind a reload, most
   constants) is moved behind everything else; when its turn comes again it is spilled if it lives in one block
   or has no load/store at its range ends, and otherwise cut into block-sized pieces together with the other
   such values (3.6.2). A value with fewer than two references never gets a register (3.6.7). `rasim check
   --predict` makes each of these decisions as c2 does on every traced function so far (3.6.8); what the pieces
   of a cut look like is still taken from the trace. **[C]**
8. **A dword of an address-taken local or a global is read into a register web only when two references share
   one reload** (between its load and the next instruction that may write it). With one reference, the copy
   `int t = (int)params[2];` loads t straight from memory; with a second read in that stretch (the optimizer
   copy-propagates the dword into t's uses before the next call) you get `mov eax,[mem]; mov esi,eax`.
   `CmdSubstDef` plays no part (3.6.10, `rasim fold`). **[C]**

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
  Which constants are candidates and which blocks reference them: docs/c2/immcand.md.
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

**A web that is split is re-costed at the pressure of the later pass; one that is not keeps its first
priority.** The pieces of a split are new globregs, so they raise P in their blocks, and each piece's priority is
its cost times that larger P. A value that crosses a call in a class whose registers the call kills (x87: all of
GF0..GF7) is split before the first pop, so each of its pieces outranks a never-split value of the same cost in
the same block. Traced: `work/scratch/x87life/mini.c` `m2` (10 → 12 and 8 as P goes 1 → 2); Spore RotateSH (P 671 →
1108). x87.md §10 has the consequences for float code. **[C]**

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
     member of `nbrs` (10b155xx loop). **[C code and trace, section 4]** This is the only thing that changes an
     avail set between two Spillcosts passes, apart from RestrictToReg (4.4).
   - The neighbour list is recorded per colouring by `regs --export` (`pops[].choice.nbrs`). A split piece made
     in a later pass starts with an avail set that already lacks the registers of its coloured neighbours
     (item 3: Spillcosts subtracts the registers live at its defs). **[C trace]**
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

**One rule for every register class and every build. [C]** There is one list and one insert routine per Color
run; the class only selects which globregs are in it. The compare is the same instruction sequence in all three
c2 builds: `cmp new.prio, cur.prio; jg` (signed), then on equal prio `cmp new.defOrd, cur.defOrd; jae` (unsigned):
14.00 `InsertInList` 10b11986..10b11992, 15.00 51063f16..51063f22, 13.10.3077 10782b41..10782b54 (`jb` to the
walk, the same test). 13.10 colours x87 values through FPColor, whose list is built by `FPAddToPriorityList`
107118e8; its compare is the same pair (1071196b..10711981: `jg`, then `jae` on +0x44). So **among equal
priorities the larger defOrd is popped first**, for int, x87 (GFx) and XMM values alike. What "larger defOrd"
means in source terms is below. Outside this order: webs with benefit ≤ 0, which are deferred and re-queued by
`InsertByBenefit` (3.5).

Probe `tests/data/tieorder.c` (`uv run tools/c2trace.py regs tests/data/tieorder.c --all-classes [--c2 13.10|15.00]
[-- /arch:SSE2]`), pops in order as `name prio/def`; identical on 14.00, 15.00 and 13.10 except where noted:

| Function | Class | Pops (prio/def) | Shows |
|---|---|---|---|
| `ti_blk` | int | a 18/32, b 18/28, c 18/24 → esi, edi, ebx | one block: earlier def = larger defOrd = first |
| `ti_xblk` | int | b 12/34, a 12/13, c 12/9 | across blocks: the def in the later block first |
| `tf_blk` | x87 | a 18/16, b 18/13, c 18/10 → GF0, GF1, GF2 (13.10, FPColor: a 6/16, b 6/13) | the same within a block |
| `tf_xblk` | x87 | b 12/23, a 12/11, c 12/8 (13.10: no tie, FPColor's priorities are 6, 5, 5: a 5/11 before c 5/8) | the same across blocks |
| `tf_blk` with `/arch:SSE2` | XMM | the two tied values: 30/16 then 30/13 → xmm1, xmm2 | the same for class 6 |

The XMM class has no cross-block tie in this probe (its copies give the values different reference counts); the
rule there rests on the shared code and the in-block tie. **[C code; I for an XMM cross-block trace]**

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
  the source. In every probe here they were the *reverse* of definition order. (3.6.9 has the rule as far as it
  is known: the order of first mention, with records freed before Color reused; and what they decide for split
  pieces.)

### 3.5 Deferral, spilling, splitting **[C structure]**

- **benefit ≤ 0** → `SpillOrDefer` 10b0d690.
  - If the globreg is not restricted to a register (`RestrictToReg` 10b1b430, section 4.4), the first time it gets fl5 |= 0x40
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
  `CmdReduceSplits` 10c24aa0): 3.6.5 gives the structure; the exact member rule is open (section 9).
- The decision rules, test by test, and what a model of them predicts: 3.6.

### 3.6 Spill, defer or split: the decision rules **[C code; traces where stated]**

This section is the rule set behind `rasim check --predict` (tools/rasim/src/predict.rs, tracer.md). 3.5 is the
overview; here every test is the code's, with its address. "Web" means one GLOBREG record.

#### 3.6.1 One pop (`Color` 10b0d33b..10b0d48b) **[C]**

The head `g` of `_SortedGlobregs` is unlinked, then, in this order:

| Test (first that holds) | Action |
|---|---|
| `g.benefit < 0`, or `g.benefit == 0` and not (`firstBlk == lastBlk` and not an immediate and `nrefs > 2` and `_Speed`) (10b0d350..10b0d376) | `SpillOrDefer(g)` (3.6.2) |
| `g.fl5 & 4` clear and `BetterToDefer(g)` returns nonzero (10b0d388..10b0d397) | `g` is re-queued with `InsertInList` at the priority BetterToDefer wrote (3.6.3) |
| `g.avail` is empty (10b0d3bb) | `HandleCurrUncolorable(g)`: `fl5 \|= 2`, more split points (`InsertAdditionalSplitsOne`, 3.6.5), the split set is {g} |
| otherwise | `InsertSplitPoints` (neighbours, register pressure), `ChoosePhysReg` (section 4), `UpdateAffectedGlobregs` (3.6.4) |

Each action returns a set of web numbers or null (eax, kept in esi at 10b0d489). **A non-null set starts a split
round before the next pop** (3.6.6), provided the list is not empty (10b0d264).

#### 3.6.2 `SpillOrDefer` 10b0d690 **[C]**

1. **Pin** (10b0d696..10b0d6c5). If `fl6 & 1` is clear and `RestrictToReg(g)` succeeds (4.4: load cost + benefit
   > 0 and a PREF for a register still in avail), `g` goes back to the **head** of the list with
   `avail = {that register}` and `benefit = 1`, so the next pop colours it.
2. **Defer** (10b0d6c6..10b0d6f8). Else, if `fl5 & 0x40` is clear and the list is not empty: `fl5 |= 0x40` and
   `InsertByBenefit(g)`.
3. **Spill** (10b0d6f9..10b0d723). Else, if `fl6 & 1` is clear and (`firstBlk == lastBlk` or `nEnter == 0`):
   `Spill(g)`. The record is retired.
4. **Cut** (10b0d724..10b0d823). Else `ForceNoAirlocks(g)` when `fl6 & 1`, `ForceSingleBlockLives(g)` otherwise;
   `g` goes back to the head with `fl5 |= 2`. Then **every later list entry** `p` with `p.benefit <= 0`, `fl5 & 0x40`
   set, the same immediate-ness as `g`, and (`p.fl6 & 1`, or `p` spans several blocks with `nEnter > 0`) is cut the
   same way and gets `fl5 |= 2`. The returned set is `g` plus those entries.

So a web with benefit <= 0 is looked at twice. The first time it is only moved back; the second time it is
spilled if it lives in one block or has no enterload/exitsave, and cut into block-sized pieces otherwise.

**`InsertByBenefit` 10b119f0**, node for node. `D` is the first list node with `fl5 & 0x40`:

- an immediate (`sym kind 0xd`) goes to the very end;
- no deferred node yet: the end;
- `D` is the list head and (`g.benefit <= D.benefit` or `D` is an immediate): `g` becomes the head;
- otherwise walk on from `D` while the node's benefit is smaller than `g`'s, stopping at an immediate or at the last
  node; `g` goes after that node if it is not an immediate and its benefit is still smaller, else before it.

In every case `g.prio` is overwritten with the priority of the node it is linked next to (10b11a24, 10b11a63):
the deferred tail has no priorities of its own. The tail is therefore ordered by ascending benefit, immediates
last. **[C: the model's list has the traced head at every pop of the validation set, 3.6.8; the copied priority
is compared at every re-pop]**

#### 3.6.3 `BetterToDefer` 10b19de0 and folding a queued def **[C code; traces: 9 re-queues]**

Only for a web whose every reference can be folded (`fl5 & 4` clear). `CmdGetComponents` (10c24590) gives the
globregs its def is computed from; if there are none, or `ComputeFoldLegalityAndWorth` (10b191e0) says no, or a
component has `benefit < 1`, the web is coloured normally. Otherwise, for class 0, `prio = min(component prios) - 1`
and the web is re-queued (10b19e60); for other classes the def is folded at once.

The fold itself happens later, **while a component is being coloured**: `InsertSplitPoints` collects the defs
`d = f(g)` in the range of the web `g` it is colouring whose dst is an uncoloured web with `fl5 & 4` clear and
whose source `g` is not used again (`UsedAgain`, 10b14071..10b140a3), and `ProcessFoldableDefs` (10b19030, called at 10b14a52) folds each one that
`ComputeFoldLegalityAndWorth` accepts and retires its record. Such a web disappears from the list without being
popped. **[C: bitvect.c `SbvFreeNode`: t144 is re-queued at 13 = prio(k) - 1 and retired when k is coloured]**

#### 3.6.4 After a colouring: `UpdateAffectedGlobregs` 10b15470 **[C]**

`nbrs` is InsertSplitPoints' neighbour set, `S` its own split set (3.6.5, register pressure), `r` the chosen
register.

1. For every `n` in `nbrs`: `n.avail -= r`. If `n.avail` is now empty: when `n.fl5 & 4` is clear,
   `FoldAwaySplitGlobreg(n)` (10b19ed0) may fold its def into the uses instead (then `n` leaves `S` and is
   retired); otherwise `n` joins a set `E`.
2. For every `m` in `S`: `fl5 |= 2`; when `m.fl5 & 4` is clear, `FoldAwaySplitGlobreg(m)`; else, if `fl5 & 8` is
   clear and `CanRecalc`, `TryToRecalc(m)` (10b1a180).
3. `InsertAdditionalSplits(E)` puts split points into the webs of `E` (byte-register webs separately); each gets
   `fl5 |= 2`, a recalculation attempt, and joins `S`.
4. `S` is returned (null if empty).

**So a web is split when the colouring of a neighbour takes its last register**, at that moment, not when it is
popped. In the traces this is the usual cause: of the split rounds of the validation set, all but two start
this way or with a cut (3.6.8), and `HandleCurrUncolorable` (a web popped with an empty avail set) never ran.
**[C trace; that it never runs in general is not claimed]**

Other things that change a **queued** web, each seen in a trace:

- **Byte-register pressure.** Where the live values that need a byte register (`fl5 & 0x80`) use up the byte
  registers, InsertSplitPoints restricts the web it is colouring (10b147ac..10b148f3), and
  `CmdProcessBytePressure` (10c24fe0) every other uncoloured web live there, to `_UnByteAbleRegs` (10d0dd54:
  ebp, esi, edi) if it can use one of them; a web that cannot is cut there. **[C code and trace: stress f1, `i`
  goes from eax edx esi edi ebx ebp to esi edi ebp before it is coloured; 128 restrictions in the sweep of 3.6.8]**
- **A spill changes other webs' benefit.** `Spill` calls `CmdUpdateCost` (10c242d0) for each instruction that
  loses its register operand: the other globreg in that instruction gets `benefit += w_B` when it is an immediate
  (/O2), `+= (CmdLoadCost - CmdSubCost)·w_B` otherwise. So spilling `x` makes the constant of `cmp x, K` more
  worth a register. **[C code and trace: at /O2 the amount was 0 for every non-immediate (534 calls) and 1, 2, 4
  or 8, the block weight, for an immediate (32 calls); at /O1 it was 1 or 2. In src/cl/compile.cpp 7 webs were
  popped with another benefit than they were queued with until these calls were replayed. Which instructions
  qualify (operand flag +0x15 & 0x80, tested at 10b175c1) is read only in outline: I]**
- **A spill can delete a def.** If the spilled web has one reference left and that is a def, the instruction is
  deleted; the uncoloured webs it used get `fl5 |= 2` and go into `_NeedNewLives` (10d0d690)
  (10b1784e..10b1788f). The set is looked at only at the end of a split round (10b0d327): if it is not empty,
  another round starts at once. **[C code; trace: bitvect.c /O1 `SbvFirstBitCommon`]**
- **Spillcosts of a later round** adds PREFs to queued webs (use-side copies are counted in every pass, 4.1).
  It does not re-cost them.
- `fl5 & 4` can become set while a web is queued **[C trace, one case: bitvect.c `SbvAnd` t172; cause not read]**.

#### 3.6.5 Where split points go **[C structure; details I unless marked]**

A split point is a pseudo-instruction (`ColMakeSplitPoint` 10b185d0, opcode 0x2f6) carrying a set of web numbers;
`ColMakeSplitPointOne` (10b18700) adds one web to the split point at a place, creating it if needed. Six places
make them:

1. **Register pressure, in `InsertSplitPoints` 10b13460** while web `g` is coloured. It walks each block of `g`'s
   range backwards with a count of the values that need a register at each instruction (coloured values live
   there, plus the operands of the instruction). Where the count would pass `_MaxPhysRegs[class]`, `SpillPseudo`
   takes values out (10b14395), and where it reaches the limit a split point is made after the
   instruction for the uncoloured webs live across it (`ColMakeSplitPoint` at 10b146bf). Those webs are InsertSplitPoints'
   return set `S`. `CmdReduceSplits` (10c24aa0, called at 10b14b33) then prunes the list for class 0. **[C that this is where S comes
   from; the exact member rule is I]** In the traces `S` is mostly a subset of the neighbours that lose
   their last register anyway. Exceptions: b250.c round 1, where 61 webs that still had a register were cut,
   one web in v_match, and the stress file of 3.6.8.
2. **Byte-register pressure**, same walk (3.6.4).
3. **A web with no register**: `InsertAdditionalSplitsOne` 10b16720 (from OrderGlobregs when a web starts with an
   empty avail set, 10b118bb; from HandleCurrUncolorable, 10b0d8f8) and `InsertAdditionalSplits` 10b15d50 (several webs at once, from
   UpdateAffectedGlobregs). The blocks of the range where the web is present are grown into regions. For each
   block `BlockSplitsOne` (10b16bd0) walks the instructions with the set of registers still free for the web:
   registers of coloured webs and physical registers defined or live there are removed, a call's kill set too. A
   reference of the web reached with an empty set gets a split point in front of it and the set starts again. A
   successor or predecessor where the web is live joins the region while a register is still common; where none
   is, or where two regions meet, a split point goes on the boundary. **The pieces are therefore the maximal
   stretches over which one register is free all the way.**
4. **`ForceSingleBlockLives` 10b1b3b0**: a split point at the top of every block of the range where the web is
   live in (after the label). **[C]**
5. **`ForceNoAirlocks` 10b1b280**: every enterload (0x295) of the web whose operand has bit 0x80 (an airlock
   load) is deleted and a split point is put at the top of each neighbouring block (BLOCK+0xc list) where the
   web is live in. **[C code; which side the list is, I]**
6. `InitialSplitPoints` 10b0ded0, before the first Spillcosts. Not read. **[I: loop boundaries]**

#### 3.6.6 The split round (`Color` 10b0d270..10b0d330) **[C]**

1. `RemoveSplitGlobregs` (10b11ad0) unlinks every list node with `fl5 & 2`.
2. `ColProcessSplitGlobregs` (10b11b20), 32 webs at a time: over the blocks that hold one of the webs it rebuilds
   gen/kill and liveness with the split points as boundaries (`CreateGenAndKill` 10b11f30, `ComputeNewLiveness`),
   `NewLiveRanges` (10b12480) makes one new GLOBREG per connected piece, and `RewriteSplitGlobregs` (10b12d20)
   renames the operands and puts an OPEXITSAVE / OPENTERLOAD pair where a value crosses a split point. A piece
   copies its parent's `defOrd`. The old records are retired.
3. `Spillcosts` costs the pieces only, with the formulas of 3.2: a piece's enterload costs it `CmdLoadCost·w`,
   an exitsave 2·w, so a piece that is one use behind an enterload has benefit 0. Its avail set starts full and
   loses the registers of the coloured values live at its defs (3.3). Blocks not touched by the split are not
   walked (a round can cost nothing: b250.c rounds 5 to 30).
4. `SpillObvious` (3.6.7) on the pieces.
5. `InsertNewGlobregs` (10b133e0): `fl5 |= 0x20`, `CmdSetConstraints`, `InsertInList`, in ascending web number.
   Unlike OrderGlobregs it does not look for an empty avail set; such a piece is found when it is popped.
6. If `_NeedNewLives` is not empty and the list is not empty, the next round starts at once.

Webs that were queued and not split keep their priority and benefit (they are not re-costed).

#### 3.6.7 `SpillObvious` 10b11560 **[C]**

For every new web, in ascending web number. "New" includes the first Spillcosts pass of a class: SpillObvious then
looks at every web of the class, not only at split pieces (`so` events at pass 1 in every trace of 3.6.10). **[C trace]**

- `nrefs < 2` and (`benefit <= 0`, or `lastDef` exists and is not an OPENTERLOAD): `Spill`.
  **[C: the test gives c2's answer for all 4,378 new webs of the validation set and 5,085 of the sweep, 3.6.8]**
- `nrefs == 2`: `CmdSubstDef` (10c25df0). It folds the def into the use only when the def is `mov web, mem`
  (a symbol or an indirection), the next instruction is the use, `CmdCanDirectSubstitute` accepts it and, at
  /O2, that instruction is not opcode 0xd. Then the record is retired. In the sweep of 3.6.8 this succeeded
  twice in 1,489 attempts.
  A reload (a web defined by an OPENTERLOAD 0x295) never qualifies: see 3.6.10 for when a reload folds instead.
- anything else stays.

Benefit <= 0 is **not** a reason here: a piece with two references and benefit 0 goes into the list and is
handled by SpillOrDefer when its turn comes.

#### 3.6.8 Validation: `rasim check --predict` **[C]**

The model runs the loop above from each round's inputs and is compared with the trace at every decision. After
a miss it is put back on the trace, so each decision is scored against a correct history.

Validation set (`rasim check SNAP --predict`; "a/b" = a of b decisions as c2 made them):

| Function | rounds | list head | branch | spill/defer kind | register | no-register sets | cut sets | split rounds | SpillObvious | state differences |
|---|---|---|---|---|---|---|---|---|---|---|
| eh_helpers.cpp eA | 1 | 6/6 | 6/6 | - | 6/6 | - | - | - | 6/6 | 0 |
| eh_helpers.cpp eB | 1 | 7/7 | 7/7 | - | 7/7 | - | - | - | 7/7 | 0 |
| e2.cpp FUN_00e0a0b0 | 4 | 29/29 | 29/29 | 3/3 | 26/26 | 3/3 | - | 3/3 | 38/38 | 0 |
| v_match.cpp FUN_00c7e9a0 | 12 | 98/98 | 98/98 | 21/21 | 77/77 | 8/8 | 2/2 | 11/11 | 124/124 | 0 |
| b250.c | 34 | 3933/3933 | 3933/3933 | 2573/2573 | 1360/1360 | 28/28 | 4/4 | 33/33 | 4203/4203 | 0 |

Columns: *list head* = the web c2 pops is the head of the model's list; *branch* = SpillOrDefer / no register /
colour (3.6.1); *spill/defer kind* = pin / defer / spill / cut (3.6.2); *no-register sets* = for each colouring
after which a neighbour is left without a register, the set of those neighbours; *cut sets* = the deferred webs cut
together; *split rounds* = traced rounds the model also starts (it starts none that c2 does not).

Per round (the trace calls Spillcosts once per round; v_match was "10 rounds" by the count of passes that cost a
block, two of its 12 cost nothing):

| | round | new webs | list head | branch | spill/defer kind | register | no-reg sets | cut sets | split round starts | SpillObvious |
|---|---|---|---|---|---|---|---|---|---|---|
| e2 | 1 | 31 | 17/17 | 17/17 | - | 17/17 | 1/1 | - | 1/1 | 34/34 |
| | 2 | 2 | 7/7 | 7/7 | 1/1 | 6/6 | 1/1 | - | 1/1 | 2/2 |
| | 3 | 1 | 1/1 | 1/1 | - | 1/1 | 1/1 | - | 1/1 | 1/1 |
| | 4 | 0 | 4/4 | 4/4 | 2/2 | 2/2 | - | - | - | 1/1 |
| v_match | 1 | 89 | 1/1 | 1/1 | - | 1/1 | 1/1 | - | 1/1 | 89/89 |
| | 2 | 7 | 16/16 | 16/16 | - | 16/16 | 1/1 | - | 1/1 | 7/7 |
| | 3 | 10 | 3/3 | 3/3 | - | 3/3 | 1/1 | - | 1/1 | 10/10 |
| | 4 | 1 | 12/12 | 12/12 | - | 12/12 | 1/1 | - | 1/1 | 1/1 |
| | 5 | 7 | 1/1 | 1/1 | - | 1/1 | 1/1 | - | 1/1 | 7/7 |
| | 6 | 0 | 7/7 | 7/7 | - | 7/7 | - | - | 1/1 | - |
| | 7 | 1 | 4/4 | 4/4 | - | 4/4 | 1/1 | - | 1/1 | 2/2 |
| | 8 | 2 | 15/15 | 15/15 | - | 15/15 | 1/1 | - | 1/1 | 2/2 |
| | 9 | 1 | 7/7 | 7/7 | - | 7/7 | 1/1 | - | 1/1 | 1/1 |
| | 10 | 0 | 22/22 | 22/22 | 11/11 | 11/11 | - | 1/1 | 1/1 | - |
| | 11 | 0 | 1/1 | 1/1 | 1/1 | - | - | 1/1 | 1/1 | 2/2 |
| | 12 | 2 | 9/9 | 9/9 | 9/9 | - | - | - | - | 3/3 |
| b250 | 1 | 1070 | 1018/1018 | 1018/1018 | 17/17 | 1001/1001 | - | - | 1/1 | 1070/1070 |
| | 2 | 61 | 10/10 | 10/10 | 9/9 | 1/1 | 1/1 | - | 1/1 | 61/61 |
| | 3 | 1429 | 12/12 | 12/12 | 11/11 | 1/1 | 1/1 | - | 1/1 | 1964/1964 |
| | 4 | 1 | 6/6 | 6/6 | 4/4 | 2/2 | 1/1 | - | 1/1 | 1/1 |
| | 5-8 | 0 | 4/4 | 4/4 | - | 4/4 | 4/4 | - | 4/4 | - |
| | 9 | 0 | 11/11 | 11/11 | 10/10 | 1/1 | 1/1 | - | 1/1 | - |
| | 10-24 | 0 | 15/15 | 15/15 | - | 15/15 | 15/15 | - | 15/15 | - |
| | 25 | 0 | 372/372 | 372/372 | 217/217 | 155/155 | 1/1 | - | 1/1 | - |
| | 26 | 0 | 26/26 | 26/26 | 25/25 | 1/1 | 1/1 | - | 1/1 | - |
| | 27 | 0 | 27/27 | 27/27 | 26/26 | 1/1 | 1/1 | - | 1/1 | - |
| | 28 | 0 | 20/20 | 20/20 | 19/19 | 1/1 | 1/1 | - | 1/1 | - |
| | 29 | 0 | 11/11 | 11/11 | 10/10 | 1/1 | 1/1 | - | 1/1 | - |
| | 30 | 0 | 899/899 | 899/899 | 899/899 | - | - | 1/1 | 1/1 | - |
| | 31 | 354 | 923/923 | 923/923 | 748/748 | 175/175 | - | 1/1 | 1/1 | 535/535 |
| | 32 | 0 | 118/118 | 118/118 | 118/118 | - | - | 1/1 | 1/1 | 2/2 |
| | 33 | 39 | 40/40 | 40/40 | 40/40 | - | - | 1/1 | 1/1 | 360/360 |
| | 34 | 210 | 421/421 | 421/421 | 420/420 | 1/1 | - | - | - | 210/210 |

How much of this is the model's own work: of the 47 split rounds above, 45 are started by a decision the model
makes alone (a neighbour without a register: 39; a cut: 6). The other two (v_match round 6 to 7, b250 round 1 to
2) start only because InsertSplitPoints' replayed set `S` holds webs that still had a register (1 and 61 webs).

Wider sweep, every function of 15 traced compiles (257 functions; the Fallout 3 TUs e2.cpp and v_match.cpp,
src/cl/util.cpp, compile.cpp and dos.cpp, src/c2/bitvect.c, tuple.c and misc.c, tests/data, b100.c, a
byte-register / shift / division stress file; bitvect.c and the stress file also at /O1, the stress file also with
/Oy-): list head 4401/4401, branch 4392/4392 (9 BetterToDefer re-queues are replayed), spill/defer kind 2054/2054
(14 pins; ForceNoAirlocks ran 520 times, ForceSingleBlockLives 6 times), register 2338/2338, no-register sets 141/141,
cut sets 100/100, split rounds 270/270 and none extra, SpillObvious 5085/5085. Two state differences, both the
`fl5 & 4` case of 3.6.4. Misses found while building the model, each now a rule or a replayed input above: the
fold of a queued def, `CmdUpdateCost`, `_NeedNewLives`, byte-register pressure, and a split round that leaves no
piece (the web is dissolved; the round still runs).


What the model does **not** predict, and takes from the trace (each needs the instructions, which the snapshot
does not hold):

- **the pieces of a split round**: how many, their blocks, costs, benefit, avail set, PREFs, flags (3.6.5 and
  3.6.6 say how c2 makes them; the model does not compute them; their record numbers it does predict, 3.6.9);
- the neighbour set and copy-partner set of each colouring (3.3 item 4, 4.3);
- InsertSplitPoints' own split set `S` (3.6.5 item 1) and the byte-pressure restrictions;
- whether `CmdSubstDef`, `FoldAwaySplitGlobreg`, `BetterToDefer` and the fold of a queued def succeed, and
  the priority BetterToDefer gives;
- `CmdUpdateCost` benefit changes and `_NeedNewLives`.

So the model answers "given these webs, which are spilled, deferred, pinned, cut or split, in which order, and
when does the next round start"; it does not yet answer "what do the pieces look like after this edit".

Not exercised by any trace so far: `HandleCurrUncolorable` (no web was popped with an empty avail set), a
successful `FoldAwaySplitGlobreg` at /O2, `RestrictToReg`'s `nrefs == 3` exception, classes other than int.

**On cl 13.10 (3077, 3052) and 15.00 [C].** The same model, unchanged, predicts every decision on both builds
once their tables carry the decision hooks (port-1310.md section 15, port-1500.md section 12). The rules
above were re-read in each build's SpillOrDefer, RestrictToReg, CmdLoadCost, SpillObvious, InsertInList and
UpdateAffectedGlobregs; the only differences are opcode numbers (OPENTERLOAD 0x26a / 0x295 / 0x2e0, first
pseudo-opcode 0x249 / 0x273 / 0x2be), which the snapshot carries (`c2.ops`). Results, every column a/a and 0
state differences: eh_helpers eA, the f3 byte-register test (9 rounds), b25.c (13.10: 420 pops in 8 rounds;
15.00: 588 pops in 31 rounds), b250.c on 13.10 (3787 pops, 2427 spill/defer decisions), the 25 traced functions
of M&B slice s0040a640 (`renderer::tessellate`: 27 pops, 6 rounds), M&B `Terrain::tessellate`
(src/game/mesh_slot/mesh_terrain.cpp, 13.10.3077: 117 pops in 14 rounds) and the 23 functions of Spore slice
s0059f510 on 15.00. 13.10's x87 values are coloured by FPColor, outside Color's loop: the model does not
predict FPColor's order or stack slots (as on 14.00 it predicts only the int class), but FPColor's record frees
and allocations are in the event stream, so 3.6.9 holds through them.

#### 3.6.9 Split pieces: record numbers and the reload order **[C code on 13.10, 14.00, 15.00; C trace]**

Every GLOBREG has a record number (+0x1c). It decides the order InsertNewGlobregs puts a round's pieces into the
list (3.4: ascending number, so among full ties the higher number wins) and the order of the reloads that
`GrgAirlockEnterloads` puts at a block entry after a split point. Both follow from four pieces of code, the same
in all three builds (14.00 / 15.00 / 13.10.3077 addresses):

1. **The numbers.** `Globregs` sets `_NextGlobregNum = 1` and `_FreeGlobs = 0` per function (10b564ae / PDB
   `Globregs` / 1077c421). `GrgNewGlobreg` (10b5b7b0 / 510a9d10 / 1077c10f) takes the head of `_FreeGlobs` if
   there is one (the record keeps its old number), else a new record numbered `_NextGlobregNum++`.
   `ColRemoveGlobregFromHashTable` (10b18790 / 5106a940 / 10784d3b) clears a retired record but keeps its number
   and pushes it on `_FreeGlobs`. So the free list is **last in, first out**, and a freed number always comes
   back before a new one. **[C]**
2. **Who frees, who takes.** Freed: every split parent at the end of its round (below), every spilled web
   (Spill), a folded or substituted def (CmdSubstDef, BetterToDefer, FoldAwaySplitGlobreg,
   ProcessFoldableDefs), FoldTooCheapDefs, x87 webs (FPSpillWorthless, FPRewriteAsReal), and before
   Color CreateGlobregs and SubsumeGlobregs. Taken: the pieces of NewLiveRanges, the pieces RewriteSplitGlobregs
   makes for a use or a def that no piece reaches, x87 splits, and before Color every candidate (CreateGlobregs,
   GrgRewriteImmedAsReg, RewriteNewBLTsAsGlobregs). **[C: the callers of both functions in 14.00; in 13.10 and
   15.00 the split-round callers were read and the rest are I]**
3. **A split round** (`ColProcessSplitGlobregs` 10b11b20 / 510640b0 / 1078b2aa): the parents are linked in
   ascending number (SbvNext over the split set, appended), handed to NewLiveRanges 32 at a time in that order,
   each parent's pieces taken one after another (one per connected region; their order within one parent, the
   reverse of the regions' creation order, is I); then RewriteSplitGlobregs walks the blocks forward and takes a
   record for each use (and def) of a parent that no piece reaches; **only then** are the parents retired, in the same ascending order. So
   a round's pieces never reuse its own parents' numbers; the next round's do, newest first: the next round's
   first parent takes the previous round's highest parent number. **[C code; trace: parents ascending in every
   NewLiveRanges call, 27/27 on 15.00 b25.c, 10/10 on M&B Terrain::tessellate]**
4. **The reloads** (`GrgAirlockEnterloads` 10b59320, called by RewriteSplitGlobregs for each block): for an edge
   into a block, the pieces live into the block and not live out of the predecessor get an enterload each, in
   **ascending piece number** (SbvNext over that set), each at the same insertion point, so the listing shows
   them in that order (CmdInsertEnter inserts before the point). The scheduler keeps their order (M&B c2sched:
   one region, equal priority). **[C code; C listings below]**

So, for the reloads after a loop: **the order is ascending piece number; pieces get numbers in ascending parent
number, first from the free list (newest freed first, which after an earlier round is that round's parents
in descending order), then from the counter in ascending order.** Free-list numbers are smaller than counter
numbers, so their reloads come first. Neither priorities, definition order nor first use after the loop enter.
In M&B `Terrain::tessellate` (13.10) the three reloads after the corner loop all came from the free list: cr, x,
t583 in descending parent number (M&B's own trace, 2026-11-11; the trace of the current mesh_terrain.cpp here
agrees with the rule in all 18 of its reload groups, the three names were not looked up). **[C]**

**What can move it (source levers).** Only the numbers can: the parents' numbers (the function-wide numbering)
and what is on the free list when the round runs.

- *The numbering.* CreateGlobregs walks the function's symbol table, so the numbers follow the order in which
  the compiler first met each local, parameter and temporary, not declaration order: `int y, x;` and `int x, y;`
  with the same statements give the same numbers (splitnum s3, s4). A declarator with an initializer mentions
  the declared variable before its initializer (`int x = a[0] + 1`: x before a, s1); an assignment mentions the
  right-hand side first (`x = a[0] + 1;`: a before x, s4); the parameters came together at the first use of
  one of them (s1, s4) **[C for these probes; I as a general rule]**. A dead mention counts: `(void)y;` or
  `if (0) y = 0;` before `x = ...` gives y the lower number (s6, s7), so the order is fixed before dead code
  goes **[C]** (by the front end's symbol order **[I]**). A value defined by a call result (`int a = g(1)`) may
  not keep its first-mention number: in t5.c (port1310/p) `a` has 4 although it is mentioned first, and the
  Color-entry free list holds 1 and 3, i.e. records retired before Color (by SubsumeGlobregs merging the call
  temporary into the variable, **I**). Temporaries the optimizer makes (tNNN) came after the named ones in every probe here. Any
  extra candidate mentioned earlier shifts everything after it by one, which reorders nothing; reordering two
  first mentions swaps two numbers.
- *Earlier rounds.* One more record freed late in an earlier round (one more spilled web, one more split
  parent, one more folded def) is the first one the next round's first parent takes; one more piece taken in an
  earlier round (a parent with one more region, one more use reached by no piece) removes the newest free
  number. Which source edits do that is what 3.6.1 to 3.6.7 predict (`rasim check --predict` shows each). **[C
  code; no source probe of this lever was made]**

**Probe pair** (tests/data/splitnum.c, also work/scratch/splitnum/s4.c, s6.c; the regression test checks it on
every build). f6 is f4 with `(void)y;` before `x = a[0] + 1;`: the same instructions, the numbers of x and y
swapped (f4: x 6, y 7; f6: y 6, x 7 on 14.00 and 15.00; 5 and 6 on 13.10), so after the loop the reloads swap
and with them the registers:

```text
            f4 (x, y)                       f6 (y, x)                       (13.10, 14.00, 15.00 alike)
  after the loop
            mov edx, DWORD PTR _x$[esp+36]  mov edx, DWORD PTR _y$[esp+36]
            mov eax, DWORD PTR _y$[esp+36]  mov eax, DWORD PTR _x$[esp+36]
  entry     mov edx, [ecx] / mov eax, [ecx+4] become mov eax, [ecx] / mov edx, [ecx+4]
```

s1 against s2 (`int x = ..., y = ...` against `int y = ..., x = ...`) flips the same reloads on all builds
(and the loads at entry, whose order the initializers set). **[C]**

**In rasim.** `rasim check --predict` replays the free list over the snapshot's event stream (Color-entry state:
`_NextGlobregNum` and the first 11 free records, `freelist`; every `remove` pushes, every `new` pops) and
scores every record handed out, that each NewLiveRanges call takes its parents in ascending number, and that
every group of enterloads at one place is in ascending order of the model's numbers. What it replays: which
records are retired when (those are the decisions scored above), how many pieces each parent gets and which
uses need a reload (the IL). Results, every one c2's: b25.c 450 of 450 records on 14.00, 413 of 413 on 13.10
(both builds), 1347 of 1347 on 15.00, reload groups 87/87, 74/74, 68/68; b250.c 3133 of 3133 (14.00) and 2735 of
2735 (13.10), reload groups 716/716 and 692/692 (5463 and 5415 reloads); M&B Terrain::tessellate 93 of 93,
18/18 groups; M&B slice s0040a640 42 of 42; Spore slice s0059f510 50 of 50.

#### 3.6.10 When a reload folds into its variable **[C code on 14.00 and 13.10; C trace on 13.10.3077, 13.10.3052, 14.00, 15.00]**

The shape (M&B `Operation::execute`, the open split-web cases): `int t = (int)params[2];` where `params` is address-taken (a
local array passed to a call; the same holds for globals and address-taken parameters, the aliased candidates of
section 7). One output is `mov eax,[params+16]; mov esi,eax` with the uses before the next call reading eax, the
other `mov esi,[params+16]`: the dword's load goes straight into the variable. Which one comes out is decided
**before Color**, by `Enterloads`, and the threshold is **one** reference besides the reload, not two.

1. **`Enterloads`** (14.00 10b588e0, 13.10.3077 1077f00c, 13.10.3052 10782b4f, 15.00 510a6ea0; run by `Globregs`,
   regalloc_cost.md §1). Per block, forward over the instructions, with a set L of candidates now held in a
   register and a set M of candidates whose reload has had one reference so far:
   - a reference of an aliased candidate that is not in L gets an OPENTERLOAD in front of it (`CmdInsertEnter`,
     10b58c06) and goes into L and M; if it was still in M from an earlier stretch, that earlier reload is undone
     first (`UndoRegCandidate` 10b597e0, call at 10b58afa);
   - a reference while it is in L takes it out of M (the reload has a second reference and stays);
   - an instruction whose destination points-at set (`pas` operand) may write the candidate takes it out of L, and
     `GrgKillerTuple` 10b5bfe0 empties L (tuples of kind 0x15 or opcode 0x2c1 and a table above it; which IL
     instructions those are was not identified);
   - at the end of the block, every candidate still in M and not in BLOCK+0x4c (`SbvMinus` at 10b5915b; that +0x4c
     is the live-out set is **I**, consistent with probe fe) is undone: `UndoRegCandidate` puts the memory operand
     back, so its single reference reads memory.
   So **a stretch (from the reload to the next write of the memory or the block end) with exactly one reference has
   no reload web**: `mov t <- s12` stays a load into t's register. With two or more references the reload is a web
   and the copy a register move. **[C code 14.00. 13.10: the 3077 and 3052 functions are the 6030 one by an exact
   instruction-multiset match (port-1310.md §3), with the same callees (GrgAirlockEnterloads, SbvCopy, CmdInsertEnter,
   UndoRegCandidate at three sites, SbvMinus); probes identical on all four builds]**
2. The extra reference comes from the optimizer: before regalloc the dword is copy-propagated into the variable's
   uses up to the first instruction that may write it (a call, once the array's address has escaped), so a use of
   `t` before the next call reads `s12`, a use after it reads `t` (IL at `@Globregs@4`, probes fa, fb). **[C trace;
   the escape reasoning I]** A store through an `int *` parameter did not end the stretch (probe fc: two references,
   reload kept). **[C trace]**
3. **`CmdSubstDef` never folds a reload.** It is called only by SpillObvious (14.00 10b11630, 13.10.3077 10782b1d,
   13.10.3052 1078421d; one call site each), only for `nrefs == 2`, and it first requires the web's def
   (GLOBREG+0x3c) to have opcode 1, `mov` (14.00 `cmp [edi+4],1` at 10c25dfb; 3077 `cmp [ebx+4],1` at 107820c0, the
   rest of its 387 bytes scattered by BBT from 1078c249). A reload's def is OPENTERLOAD 0x295, so the call returns at
   once. The 13.10 function is the same as 14.00's test for test: mov def, a dst leaf with +0x10, the next tuple an
   instruction using the web (`RegFindInSrcList`), src kind 2 (SYM) or 6 (INDIR), `CmdCanDirectSubstitute`, not the
   use's first source when `_Opcode_dope` has 0x40, not opcode 0xd at /O2, then the size checks and
   `SymMakeEquiv`/`TupMakeIndir`, `TupReplaceSrc`, `TupRemove`, `ColRemoveGlobregFromHashTable`. **[C code, both]**
   The M&B reading ("CmdSubstDef needs a piece with exactly 2 references") is right about CmdSubstDef, but
   CmdSubstDef is not what removes a reload. nrefs does not count the OPENTERLOAD: a reload with the copy and one more
   read shows `refs 2` in `regs` (probe fa). **[C trace]**
4. **Split pieces.** A piece made by a split round whose def is a reload is spilled by SpillObvious when `nrefs < 2`
   and benefit <= 0 (3.6.7). A reload costs `CmdLoadCost·w` (3.6.6), so a piece with one use behind its reload has
   benefit 0 and is spilled: its one use, the copy into the variable, reads the slot. That is the same fold in a
   split round. With two uses CmdSubstDef is tried and refuses (item 3) and the benefit is positive, so it is
   coloured. **[C code; the SpillObvious rule is validated on 9,463 webs, 3.6.8. b25.c on 14.00: all 92 reload
   pieces with one reference and benefit <= 0 spilled, all 66 with two references went to CmdSubstDef and none
   was folded (nor any of the 67 `mov`-defined ones)]**
5. **The tie is not a lever.** The reload is the instruction just before the copy, and defOrd numbers each block
   backwards (3.4), so the reload's defOrd is the copy's plus one: in an exact priority tie (same block, two
   references each) the reload pops first, every time (fa: local5 19 / t 18; p2: local12 48 / t 47, local11 46 / p
   45, the "piece, variable, piece, variable" pattern). The order decides registers, not the fold: a reload that
   exists has a reference after the copy, so it interferes with the variable and the two cannot share a register in
   either order (`rasim regs SNAP before:t:local12` on p2: 0 of 10 registers change). The record number (first
   mention) breaks only ties of priority *and* defOrd (3.4), so it cannot reorder this pair either. **[C model; no
   spelling flips the defOrd without changing the IL, so not compiled]**

**Probes** (`work/scratch/fold/p4.cpp`, `p6.cpp`, `p2.cpp`; fa and fb are `tests/data/fold.cpp`, checked on every
build). The globreg tables and listings are the same on 13.10.3077, 13.10.3052, 14.00 and 15.00:

| probe | source after `fill(params); if (chk((int)params[2])) {` | dword's stretch | result |
|---|---|---|---|
| fa | `int t = (int)params[2]; use(t); mark(); use(t);` | copy + `push s5` | reload web local5 (eax): `mov eax,[params+16]; push esi; push eax; mov esi,eax` |
| fb | `int t = (int)params[2]; mark(); use(t); use(t);` | the copy only | no web: `mov esi,[params+16]` |
| fc | fa with `*q = 0;` after the copy | copy + push (the store does not write the dword) | reload web, as fa |
| fe | `int t = ...; if (*q) use(t); mark(); use(t);` | the copy only, but read again in the next block | reload web (live out), `mov eax,[..]; mov esi,eax` |
| ff | `int t = ...; q[0] = t; q[1] = t; q[2] = t; mark(); use(t);` | copy + 3 stores | reload web, refs 4 |
| p2 f2 | M&B `party_count_members_of_type`: `int t = (int)params[2], p = (int)params[1]; int n = cnt(t, p, 1); use(n + cnt(t, p, 0));` | copy + push, for both dwords | `mov eax,[params+16]; ... push eax; mov edi,eax`: ours in M&B |

**For matching.** If the original loads the variable straight from the dword and yours goes through a scratch
register, the dword has a second reference between the reload and the next call in yours: count the reads of the
dword (and of the variable before the call: they are the same after copy propagation) in that stretch. One edit
folds it when there is exactly one extra: move that read past the next call (or anything that may write the array),
or drop it. `rasim fold SNAP VAR` (tracer.md) lists, from a snapshot's IL at Color's entry, every reload copied into
VAR with its stretch, says whether it folds, names the extra references, gives the edit in rasim's EDIT language
(`rasim order SNAP unref:S:B ref:VAR:B:2` to see the priorities after it) and reports the tie; it also gives the
SpillObvious and CmdSubstDef outcome of each of VAR's webs.

---

## 4. Which register: `ChoosePhysReg` 10b14b70 **[C]**

Arguments: eax = `nbrs` (an SBV of uncoloured interfering globreg ids from InsertSplitPoints), `[esp+4]` = gr,
`[esp+8]` = class.

```text
weights[*] = 0                                                  10b14b85
for each n in nbrs with n.benefit > 0:                          10b14bd2 (jle 10b14bd6 skips benefit <= 0)
    for each PREF p in n.prefs: if p.reg in n.avail: weights[p.reg] += p.count     10b14c2c
    if |n.avail| == 1 (the only register r):       weights[r] += n.benefit * 100   10b14ca7
for each PREF p in gr.prefs:                       weights[p.reg] -= p.count        10b14d59 (no avail test)
best = none
for r in _RegOrders[class] (eax, ecx, edx, esi, edi, ebx, ebp):                    10b14d6a
    if r in gr.avail and (best == none or weights[r] < weights[best]): best = r    10b14df1 (jge)
gr.color = &_RegSym[best]; free gr.avail
for blocks firstBlk..lastBlk: record best in BLOCK +0x54/+0x40/+0x44 where gr is present/live-in/live-out
for each m in _UpdatePrefsBv with m.color == 0: m.prefs[best].count += 1            (10b15152..)
```

Then `UpdateAffectedGlobregs` 10b15470 clears `best` from the avail set of **every** member of `nbrs`, whatever
its benefit. **[C trace: the avail set of every neighbour, at every later sighting, is its initial set minus the
registers of the neighbours coloured in between; 0 exceptions in 1,500+ colourings, see the table below]**

**The avail bit vector.** `GLOBREG.avail` (+0x20) points to an SBV: a list of chunks `{base, lo, hi, next}`, and
bit *n* of the chunk with `base == n & ~0x3f` is register number *n* (2.1). The set and clear masks are the
64-bit tables at 10cf65e8 (`1 << n`) and 10cf67e8 (read from the binary). For the integer class that is one chunk
with base 0: 0x2 eax, 0x4 ecx, 0x8 edx, 0x10 ebx, 0x40 ebp, 0x80 esi, 0x100 edi. **[C]**

**This is the whole rule. [C]** `rasim check` (tracer.md) replays it from a traced snapshot: each globreg starts
with the avail set and preferences it has when `InsertInList` first queues it; the neighbour list and the partner
list of each colouring are taken from the trace; everything else (every later avail set, every preference count,
every weight, every register) is computed and compared with what ChoosePhysReg read.

| Function | int colourings | register predicted | state divergences |
|---|---|---|---|
| tests/data/eh_helpers.cpp eA, eB, nA, nB | 6, 7, 4, 4 | all | 0 |
| Fallout 3 0x00e0a0b0 (work/scratch/f3/rw/e2.cpp) and 16 source variants of it | 26 or 27 each | all | 0 |
| 0x00e0a950, 0x00e0a550, 0x00e0a850 (same file) | 32, 22, 2 | all | 0 |
| Fallout 3 0x00c7e9a0 (work/scratch/f3/pk/v_match.cpp), an EH function with 10 Spillcosts passes | 77 | 77 | 0 |
| 26k-instruction synthetic (work/scratch/big/b250.c), 34 Spillcosts passes | 1360 | 1360 | 0 |

### 4.1 Where preferences come from: `Spillcosts` **[C code and trace]**

Only two places write PREF nodes: Spillcosts (six sites) and ChoosePhysReg's partner loop (4.3). Summing the
Spillcosts events and the partner increments reproduces every PREF list seen, both when a globreg is queued and
when it is coloured (same runs as the table above).

Spillcosts walks every instruction in every pass. A copy is opcode 1, 0x11f, 0x1fd, 0x17f, 0x181, 0x125 or
0x189; G is an **uncoloured** globreg; S is a physical register or a globreg that already has a colour (then the
preference is for that colour).

| Site | Instruction | Counted |
|---|---|---|
| 10b1082d | `mov G, S`, S a coloured globreg (use side) | in every pass |
| 10b10890 | `lea G, [S]` (opcode 0x12), S a coloured globreg | in every pass |
| 10b10c31 | `mov G, S`, S a physical register (use side) | in every pass |
| 10b10d9c | `lea S, [G + ..]`, S a coloured globreg (def side, base of the address) | in every pass |
| 10b10e68 | `mov S, G`, S a coloured globreg (def side) | only while G is new (`fl5 & 1`, 10b10e25) |
| 10b11238 | `mov S, G`, S a physical register (def side) | only while G is new (10b11208), and one of: the operand has `+0x15 & 0x10` (last use **[I]**), its symbol is its own base (`sym+8 == sym`), or `NoOverlappingEnregisterableSyms` 10b1bb90 returns nonzero |

Typical sources: `mov gr, eax` after a call, `mov eax, gr` before `ret`, arguments arriving in or leaving through
ecx/edx (thiscall, fastcall).

**Use-side copies are counted again in every Spillcosts pass. [C]** `fl5 & 1` is cleared by InsertInList, so the
def-side sites fire once per globreg, but the use-side sites have no such test: each split round (a new
Spillcosts pass, 3.1) adds 1 more for every `mov G, reg` of every globreg that is still uncoloured. Traced in
0x00e0a0b0: `this` has ecx:3 after pass 1 (`mov this, ecx` at entry plus two `mov ecx, this` before calls) and
ecx:4 after the split round that follows pop 17. A later pass is also where a copy with a globreg coloured in the
meantime becomes a preference even if 4.3 did not make it one (in that same pass `t353` got ecx:1 from
`lea t353, [t231 + ..]`, t231 having been coloured ecx at pop 12).

### 4.2 The resulting rules

- With all weights 0, the first available register in eax, ecx, edx, esi, edi, ebx, ebp wins. This is the tracer
  demo `sum` (tracer.md). **[C]**
- A call-crossing value has only esi, edi, ebx, ebp available (3.3.2), so the n-th call-crossing web in colouring
  order takes the n-th free of esi, edi, ebx, ebp. **[C, p1 to p6]**
- **A copy with a register moves the choice.** t3.c `__fastcall f(x, y)`: y arrives in edx and is reassigned
  `y = g(y)` (eax). Its weights are eax −1 and edx −1 (ecx is not available). eax wins the tie by order. The listing
  has `mov eax, edx` at entry. **[C trace]**
- A value avoids a register that an uncoloured neighbour prefers: +count per preference. **[C trace]** 0x00e0a850:
  `c` is coloured with ecx:+1 because its neighbour `this` holds ecx:1; `c` takes eax anyway (0), and `this` then
  gets ecx at −1.
- A value strongly avoids a neighbour's last remaining register: +100·benefit. **[C trace]** 0x00e0a0b0: `t332`
  sees ebp:800 because two neighbours have only ebp left (`mid`, benefit 2, and `p4`, benefit 6); `dst` sees
  ebp:600 for `p4` alone.
- Consequence for swaps between two callee-saved registers: the value coloured first takes the earlier register
  unless something raises that register's weight for it. A neighbour that prefers it (+1) or has nothing else left
  (+100·benefit) is enough, because all other weights are usually 0.

### 4.3 Copy partners: `_UpdatePrefsBv` **[C code; the symmetry question is I]**

`InsertSplitPoints` 10b13460 fills `_UpdatePrefsBv` while it walks the range of the globreg X being coloured:

- **Use side** (10b14032..10b14047): X is the source of a copy `mov Y, X` and Y is a globreg. Y is added
  unconditionally, coloured or not (a coloured Y gets nothing: 10b15152 tests `color == 0`).
- **Def side** (10b13939..10b139bb): X is defined by a copy `mov X, Y`. If Y is coloured it goes on a new-def list
  instead (`ColGetNewDefList`, used by `FlagAsNotReallyLastUse*`). If Y is uncoloured it is added only when it is
  not in the set at `[esp+0x9c]` (the set that is ORed into `nbrs` right after, so the globregs live there **[I]**),
  or the operand has `+0x15 & 0x10` (last use **[I]**). A copy with `TUPLE+0xa & 0xfff == 1` is skipped when either
  operand's symbol has `+0x24 & 1` (10b1394f..10b13971; meaning not identified).

So for one copy the relation depends on which side is coloured first: the destination always becomes a partner of
the source; the source becomes a partner of the destination only if it dies at the copy **[I for "dies"]**. Traces agree: of 13
globregs listed as an uncoloured partner and coloured later, 1 listed the first one back; in 0x00c7e9a0 `local31`
lists the already coloured `values`, while `values` (coloured first) had not listed `local31`. **[C]**

A partner that is skipped here still gets the preference from the next Spillcosts pass if one runs before it is
coloured (4.1, sites 10b1082d and 10b10e68).

### 4.4 `RestrictToReg` 10b1b430: a deferred web is pinned to its preferred register **[C code, one trace]**

`SpillOrDefer` 10b0d690 first calls RestrictToReg (unless `fl6 & 1`). If `CmdLoadCost(sym) + benefit > 0` and the
web has a PREF whose register is still in its avail set, the avail set becomes that one register (the highest
count; the list is scanned from its head and replaced only by a strictly higher count), benefit becomes 1, and
SpillOrDefer puts the web back at the **head** of `_SortedGlobregs` (10b0d6a9), so it is popped again at once and
coloured. One exception is not decoded: `nrefs == 3` with a `lastDef` whose opcode has bit 0x40 in table 10ce1e60
and whose dst operand has `+0x10 == 0` (10b1b4d3..10b1b4ff) **[I]**. `regs` shows this as a `spill/defer` pop followed by a pop of the same globreg
with a single candidate. Seen once: b250.c pop 3513/3514, an immediate with esi:1 and avail eax ecx edx esi edi
ebx, coloured esi.

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

This pass runs after Color and gives every tmpreg a register. There is no later renaming pass: the registers in
the listing are these picks; the scheduler only reorders the instructions afterwards (sched.md). The whole rule
below is replayed by `rasim scratch` (tracer.md), which predicts every integer pick of the validation set (6.5).

### 6.1 The walk (`RegSimpleAssignBody`) **[C code]**

One run per function body and one per EH unwind funclet, each starting with `_RegMap` (10d0fd10, one slot per
register: 0 = free, else the symbol that holds it) cleared and the cursor `_NextIntPos` (10d0f854) pointing at the
first entry of `_IntPhysRegs` (10d0cfd0, dwords 1 2 3 7 8 4 6 0 = eax ecx edx esi edi ebx ebp; ebx and ebp are
cut off when `regAvail` 10d0f9f0 says they are taken). So **the cursor starts at eax in every run and is never
reset inside a run** (the only other write is at EnC tuples, opcode 0x303, under `_EnC` 10d0d31c). **[C]**
At /O2 the conflict sets are built first for the whole run by `RegBuildGlobalConflict` 10bdba20 (6.2); the EH
funclet runs of 0x00c7e9a0 did not go through it (they take the `RegBuildConflict` 10bdb270 branch at 10bda2f0..
10bda339; which of the three flags tested there differs for a funclet was not determined **[I]**).

Then the tuples are walked in IL order:
- a block tuple (kind 0x1a): `RegResetRegMap` 10bdc830 clears `_RegMap` and marks every register in the block's
  live-in set (`BLOCK+0x40`) as held by itself. So nothing a temp holds survives a block boundary, and a
  coloured variable live into the block makes its register busy from the top of the block. **[C]**
- an instruction: `RegAssignSrcs` 10bda4a0, then `RegAssignDsts` 10bda5c0.
  - **Sources first.** A tmpreg operand is replaced by the register `_RegMap` shows for it; if the operand carries
    the last-use flag (operand `+0x15` bit 0x10, set by the conflict builder) the slot is freed. A coloured
    variable's register is freed the same way at its last use (10bda510). **A register freed by a source is
    therefore free for the destination of the same instruction**: `mov t2, [t1+8]` with t1 dying can put t2 in
    t1's register, if the cursor points there or the other candidates are blocked. **[C code; trace: eA pick 2]**
  - **Destinations.** A coloured variable (or a temp Color already placed) being defined marks its register busy
    (10bda650); if a live tmpreg sits there, `RegFixConflict` 10bdc8e0 moves it. A bare physreg operand (eax
    after a call, ecx for a shift count) is *not* entered in `_RegMap`: it is kept clear of temps by the conflict
    sets only. A kill set (operand kind 0xb: the registers a call clobbers) calls `RegFixConflict` for every
    register in it that a non-physreg symbol holds. A tmpreg destination not yet in `_RegMap` is one **pick**:
    a `RegAssign` call. A tmpreg defined twice while still live is one pick. **[C code]**

### 6.2 The conflict sets (`RegBuildGlobalConflict`) **[C code; the sets [C] against memory, 6.5]**

`_BvConflicts[r]` (10d0fba0) is the set of tmpreg ids that must not get register r. It is built before the first
pick, so **it does not depend on any scratch pick**, only on Color's result and the IL. Each block is walked
backwards with P = the physregs live at this point (initially `BLOCK+0x44`, the live-out set) and T = the tmpregs
live at this point:

| event in the backward walk | effect |
|---|---|
| last use of temp t (first time it is met) | T += t; t joins `_BvConflicts[r]` for every r in P (10bdc440); the operand gets the last-use flag |
| def of temp t (not also a source of the instruction) | T -= t |
| last use of a coloured variable in r, or of a temp Color placed in r | `_BvConflicts[r]` \|= T; P += r; last-use flag (10bdc69d, 10bdc1c3) |
| def of a coloured variable in r | P -= r (no conflict is added here) |
| use of a bare physreg r | P += r only |
| def of a bare physreg r | `_BvConflicts[r]` \|= T; P -= r (10bdbe42) |
| kill set (call) containing r | `_BvConflicts[r]` \|= T; P -= r (10bdbffd) |
| a byte-sized temp (type size 1) | joins the sets of esi, edi and ebp, and gets flag `+5` bit 0x80 |

In words: a temp conflicts with r when r is live at the temp's last use, when a value in r has its last use
while the temp is live, or when r is defined bare or clobbered by a call while the temp is live. **A temp that
is live across a call conflicts with eax, ecx and edx**, so it can only be placed by first fit in esi/edi/ebx/ebp
(or spilled); temps that the rotation serves are exactly those that live between two calls. Not covered by the
table: a temp defined after a bare physreg's def and still live after that physreg's last use gets no conflict
with it **[I, from the code; no case seen]**.

The same walk sets the temp's **preferred register** (`tmp+0x30`), a register symbol or 0:
- at the temp's def: the instruction is a copy (opcodes 1, 0x11f, 0x1fd, 0x17f, 0x181, 0x125, 0x189) from a
  register operand of a compatible type → that register;
- at each use, overwriting (so the *first* use in program order wins, unless the def rule fired, which runs
  last): a copy of the temp *into* a register → that register (also one `lea` form, 10bdc593 on); otherwise 0. **[C code, no trace case]**

### 6.3 One pick (`RegAssign`) **[C code, C trace]**

1. **Preference.** If `tmp+0x30` is still 0, RegAssign derives it from the defining instruction, whose sources
   have already been turned into registers: a copy from a register (same opcode list, compatible type, not esp)
   → that register; a `lea` (opcode 0x12) whose address is base register only or base + displacement (no symbol,
   and no index unless scale and displacement are 0) → the base register. This is how a preference follows an
   earlier pick: `t2 = t1`, or `lea t2, [t1+8]`, prefers whatever register t1 was given. The preferred
   register is taken when its `_RegMap` slot is free, `regAvail` is set, it is below 6 for a byte temp, and the
   temp is not in its conflict set. **The cursor does not move.** The preferred register may be any register
   (esi, edi: `lea t, [esi+32]` with esi's variable dying takes esi).
2. **Rotation** (only when `_Speed` 10d0d71c, i.e. /O2). Scan from the cursor: eax → ecx → edx → eax, at most
   three entries (the scan wraps when the next entry is 0 or ≥ 4 and stops when it is back at its start). Take the
   first register whose `_RegMap` slot is free and whose conflict set does not hold the temp. `regAvail` is not
   tested here. Then **the cursor is set to the entry after the register taken** (`CmdSetNextPos`; after edx:
   eax), not to the entry after the old cursor. A skipped register is therefore not retried by the next pick.
3. **First fit** (rotation found nothing, or /O1): eax, ecx, edx, esi, edi, [ebx, [ebp]] with the same two tests.
   The cursor does not move.
4. First fit ignoring `_BvConflicts` but requiring `TupFindDstRegInterf` = 0, then `RegSpill` 10bdad70 of the
   register whose next use is farthest (`RegFindPhysRegUse` 10bdacc0). Not seen in the validation set; not
   modelled.

Float temps (type class 5) use the list at 10d0cff0 (GF0..) and the cursors 10d0f850/10d0f84c; they never move
the integer cursor (`CmdGetNextPos`/`CmdSetNextPos` 10c234c0/10c23520 switch on the type's top nibble: 1, 2 and 6
are the integer class). **[C code; trace: e2 picks 3-8, 22-28]**

### 6.4 What this means for a decompiler

A pick is a **rotation pick** when the temp has no usable preference; it gets the cursor's register unless
that one is *forced away*, and the trace (`scratch --why`) names the reason:
- *busy, coloured variable*: Color put a variable, or a call result, in that register and it is live here;
- *busy, temp tN*: an earlier temp of the same block has not had its last use yet (the usual case is the
  second and third argument of a call being computed while the first waits);
- *conflict*: the register is free now but something will be put in it before the temp dies.

Consequences, each seen in the traces **[C]**:
- A pick with a preference taken is cursor-neutral (eA pick 3: removing it changes nothing later).
- One more or one fewer rotation pick shifts every later rotation pick by one register **until a forced pick**:
  a pick with only one candidate lands on it from any cursor and sets the cursor after it, so the rotation is
  back in step. In straight-line code with all three registers free the shift runs to the end of the function
  (the straight-line test of 6.5: 9 of 9 later picks move). In eA, removing pick 1 changes only pick 2. In 0x00c7e9a0,
  inserting a pick before pick 7 changes nothing after it (pick 7 has the single candidate edx).
- So to move the register at one listing offset, look for the nearest earlier pick whose candidate set has more
  than one entry and change the number of rotation picks between the last forced pick and it; `rasim scratch
  SNAP ins:N` / `drop:N` gives the picks that move.
- The picks are in IL order, per block; the listing order differs (sched.md), so use the `off` column.
- In a large switch the cursor carries from case to case, so one case with a pick more or fewer shifts every
  later case up to the next forced pick (and can change which identical tails merge). `c2trace.py scratch
  --phase orig.s` reads the original's picks, prints the phase per case and scans every single `ins:N`/`drop:N`
  for the one that re-aligns the most cases (tracer.md).

`/O1`: `_Speed` is 0, step 2 is skipped, every pick is first fit. Tested with t4.c,
`x1 = s[0]; ... x5 = s[4];`: /O2 gives eax, ecx, edx, eax, ecx; /O1 gives eax five times. **[C]**

### 6.5 Validation **[C]**

`c2trace.py scratch --export` records, per pick, `_RegMap`, `regAvail`, the cursor before and after, the
preference before and after, and the conflict sets (rebuilt from the builder's events of 6.2). `rasim scratch`
replays 6.3 with its own cursor and its own temp → register map:

| function | runs with picks | integer picks predicted (register, kind, cursor) | float picks (not modelled) |
|---|---|---|---|
| tests/data/eh_helpers.cpp eA | 1 | 7 / 7 | 0 |
| tests/data/eh_helpers.cpp eB | 1 | 6 / 6 | 0 |
| Fallout 3 0x00e0a0b0 (work/scratch/f3/rw/e2.cpp) | 1 | 23 / 23 | 13 |
| Fallout 3 0x00c7e9a0 (work/scratch/f3/pk/v_match.cpp) | 5 (4 funclets, body) | 63 / 63 (body 59 / 59) | 16 |

No misses, so there is no miss to explain. The reconstructed conflict sets were also compared with the bits
read from `_BvConflicts[eax/ecx/edx]` in memory at every pick: 21/21 (eA), 104/104 (e2; 4 more out of reach of the
tracer's pointer depth), 225/225 (0x00c7e9a0). What-if answers were checked against real compiles of a
straight-line function (three calls with three loaded arguments each, two stores between): `drop:2` predicted
the 9 later registers of the variant with that load replaced by a constant (9/9), `ins:5` the 8 registers from
the inserted load on in the variant with one more store (8/8). Limits: the four funclet runs have no conflict
sets in the snapshot (one pick each); a what-if assumes the edit leaves Color's result and the other temps'
lifetimes alone, which a real source edit often does not (replacing an argument in eA also changed which
values Color handles).


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
   - `_Wvm_flag` (10d0ddac) == 0, `_Globopt_flag` (10d0d708) != 0 and `_FWvmSafe` (10d07a24) == 0: native code
     (not the managed/MSIL back end), global optimisation on (/Og, so /O1, /O2, /Ox; il_pipeline.md), not
     verifiable managed code. **[C: `CgCanForceDoubleAlign` 10b06130 tests the same three addresses in this
     order (`cmp [10d0ddac],0; jne` / `cmp [10d0d708],0; je` / `cmp [10d07a24],0; jne`) and its decompile, which
     has the PDB names, reads `_Wvm_flag != 0 || _Globopt_flag == 0 || _FWvmSafe != 0`]**
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
   about 4 call-crossing webs. 3.6.5 has the structure and 3.6.4 the `NeedNewLives` rule; what is still
   open is the exact set InsertSplitPoints cuts under register pressure and how the pieces come out
   (`BlockSplitsOne`), which is why `rasim check --predict` replays the pieces.
2. Which operand set `SbvMinus` subtracts at each def (10b11093). It is presumed to be the physical registers live
   at that point.
3. `Globregs` candidate selection: which variables become globregs, and which stay in memory or as tmpregs
   (`GrgIdentifyCandidates`, `GrgSpillCheapSyms` 10b5c160, `Enterloads`).
4. `SubsumeGlobregs`'s exact conditions, and how coalescing changes refs and P.
5. FPColor (x87). The byte-register path (fl5 & 0x80, `CmdProcessBytePressure` 10c24fe0) is described in 3.6.4;
   its trigger count (how many byte values are live against how many byte registers) is read only in outline.
6. Whether cl 13.10 (M&B) has the same formula without loop weighting. This c2 cannot test that.
