# c2 register allocation: priorities, spill costs, candidates, x87

How c2.dll (cl 14.00.50727.762) decides **which web gets a register first**, **which register it gets**, and
**which webs it spills**. Everything was read from the code, and most of it was also checked with traced compiles
(`tools/c2trace.py`, see [tracer.md](tracer.md)). Data layouts follow [il.md](il.md) and `src/c2/il.h`.

Each rule is tagged **[C]** (CONFIRMED: read in the code, or seen in a traced or plain compile), **[M]** (measured
with a probe on all four builds: 14.00, 15.00, 13.10.3077, 13.10.3052) or **[I]** (INFERRED). Addresses are c2.dll
VAs.

Source modules: the Chow-style priority colorer is **color.obj** (`Color`, `Spillcosts`, `SpillOrDefer`,
`OrderGlobregs`, `SubsumeGlobregs`, `RemoveSplitGlobregs`, `ChoosePhysReg`, ...). The task text puts the last three
in globregs.obj, but the PDB puts them in color.obj. **globregs.obj** finds candidates and builds the webs.
**colormd.obj** holds the x86 cost tables (`CmdLoadCost`, `CmdSubCost`). **regasg.obj** assigns the leftover local
temporaries after coloring. **fpreg.obj** maps the x87 virtual registers GF0..GF7 onto the x87 stack.

Probe sources and the trace driver are in `work/scratch/glass_rcost_p1/` (`t.c` to `t7.c`, `run.sh`). Decompiles
are in `work/scratch/glass_rcost_*.txt`. Rule probes run on all four builds are `tests/data/rules/RC_*.cpp`, checked
by `tests/probes/regalloc_cost.py` (listings, plus `regs` traces where the deciding number is a priority).

---

## 1. Pipeline (where this sits)

`dop2_GlobalRegAlloc` 0x10c07350 runs `FgEarlyTailDup` (at /O2), then `Globregs`, then `Color`, then
`RegSimpleAssignBody` ([il_pipeline.md](il_pipeline.md) row 5b).

1. **`Globregs` 0x10b563f0** (globregs.obj) [C]:
   1. `CmdIdentifyImmedCandidates`, `EnumerateSymbols` (candidate SYMs, section 2), `ReachingOccurGenKill`.
   2. `GrgAvoidFpEnterloads` and `GrgAvoidLousyLoadsInLoops`, for aliased candidates only.
   3. `AvoidAirlocks`, `Enterloads`, `InitLoopRefdSyms`, `ComputeLifetimes`.
   4. `GrgSpillCheapSyms`, only under PGO (`_PogoFlag==2 && _Speed`).
   5. `CreateGlobregs` makes one GLOBREG (a *web*) per lifetime. `RewriteAsGlobregs` rewrites the operands to
      point at the webs.
2. **`Color` 0x10b0cf40** [C]:
   1. `SubsumeGlobregs` (copy coalescing), `FoldTooCheapDefs`, `CreateBlockBvs`.
   2. Then, for each register class `k = 6, 5, ..., 0` that has webs (`_NewGlobregs[k]`): `InitialSplitPoints`,
      **`Spillcosts`**, `SpillObvious`, **`OrderGlobregs`**, then the coloring loop (section 5).
   3. Class 0 (int32) comes last. Just before it, `MdmDetermineDoubleAlignAndFpo` and `CmdGetNumIntRegs` run,
      because whether ebp is free depends on the frame decision. Class 1 is x87 (GF0..GF7), 5 is MMX, 6 is XMM.
      The class of a web is `_RegClass[ty>>12]` (0x10cf6528):
      `{-1,0,0,0,0,1,0,-1,-1,-1,-1,-1,5,6,6,6}`.
   4. `RewriteAsReal`.
3. **`RegSimpleAssignBody` 0x10bda210** (regasg.obj) gives registers to the remaining temporaries that are not
   webs (section 7) [C].

---

## 2. Global register candidates (globregs.obj)

`EnumerateSymbols` 0x10b568f0 walks `_BESymTab->poolHead` pools in creation order and the 0x60-byte slots in each
pool in ascending order. That is SYM key order, which for user variables is first-reference order in the IL, not declaration order (symkeys.md). It
considers only base SYMs (`sym->base == sym`, kind != 0x10) [C]:

| SYM kind | Candidate? | Notes |
|---|---|---|
| 3 tmp | yes if `+0x14 == 0` (no single def recorded? [I]: an internal field, no source form isolates it); otherwise excluded | clears `fl6 & 2` |
| 4 auto, 5 param | yes. If `fl5 & 2` (address taken) is set it becomes an *aliased* candidate, like a global | |
| 7, 8 global | yes, as *aliased* candidates (set `_GrgBvAliasedCands`) | they get a load before and a store after the region [C traced][M] `RC_global.cpp`: in `gl_loop` (`G += i`, no call) `G` is a kind-7 web colored edx, loaded before the loop and stored after it; with a call in the loop (`gl_call`) its benefit is -4 and it is spilled, so `add [G], esi` stays in the loop. All four builds |
| 10 func | only if `SSR+0x37 & 0x400` and not `& 0x4000`, and the member size is 4 | |
| others | excluded (`__GrgExcludeSymCount++`) | |

Every member of the equivalence chain (`equivNext`) whose type is register-sized (byte table 0x10ceb338 indexed by
`TyGetIndex`) gets a sequential **candidate bit number** (`getmem(0xe)` record at `sym+0x34`). That stops at the
first member that `FindMapSym` maps onto another member [C].

`CreateGlobregs` 0x10b5abe0 then walks the same pools again in the same order and calls `GrgNewGlobreg`
0x10b5b7b0 once per lifetime of each candidate. Each web gets `num = _NextGlobregNum++`, which starts at 1 per
function (`Globregs` 0x10b56490), or a recycled record from `_FreeGlobs`. It is linked into
`_GlobregHash[num & 0x3ff]` and added to `_NewGlobregs[class]` [C]. Immediate candidates (kind-13 constant SYMs
from `_ImmedHashTable`) whose record count `+0x14 < 1` are removed first [C].

**Web numbering is therefore: SYM key order, then lifetime order inside a SYM.** This is the "internal id"
that the empirical docs see in distant ties [C for the order; I that it is the only source]. It does not decide
the coloring order: `InsertInList` keys on priority and `defPos` only, and swapping two declarations moves no
register (`RC_decl.cpp`, section 9) [M]. Whatever the web number feeds lies outside this module.

---

## 3. The web record (GLOBREG)

A web is a SYM-shaped record of kind 2 (SK_GR, `gr<num>:<reg>`; `GrgNewGlobreg` is its creator, which il.h listed
as unknown) with `+8` = itself. Field use from Spillcosts, Color, OrderGlobregs, ChoosePhysReg and GrgNewGlobreg:

```c
typedef struct PREF {           /* move preference, getmem(7) in Spillcosts 0x10b1085a */
    struct PREF* next;          /* 0x00 */
    SYM*  reg;                  /* 0x04 &_RegSym[r] of the colored partner (partner->assigned) [C] */
    int   count;                /* 0x08 number of moves seen between the two [C] */
} PREF;

typedef struct GLOBREG {        /* "web"; GrgNewGlobreg 0x10b5b7b0 */
    SYM*  sym;                  /* 0x00 candidate SYM; sym->kind 13 = immediate candidate [C] */
    u8    kind;                 /* 0x04 2 [C] (_RegSym entries have 1 here) */
    u8    fl5;                  /* 0x05 01 costs dirty (set at create, cleared by InsertInList) [C]
                                         02 handled/being split (RemoveSplitGlobregs drops it) [C]
                                         04 has a reference that cannot be folded into memory [C]
                                         08 def not re-materialisable (CanRecalc gate) [I]
                                         40 deferred once (SpillOrDefer) [C]  80 byte-register constraint [I] */
    u8    fl6;                  /* 0x06 01 defined by an enterload whose operand has 0x80 [C]; 02 crosses a block
                                         with tuple flag 2 [I]; 08 on the block's "died" list (scratch) [C];
                                         10 referenced in the current block (scratch) [C] */
    u8    fl7;
    struct GLOBREG* self;       /* 0x08 */
    int   priority;             /* 0x0c sort key, signed (Spillcosts) [C] */
    SYM*  assigned;             /* 0x10 &_RegSym[r] after ChoosePhysReg; 0 = not colored [C] */
    struct GLOBREG* next;       /* 0x14 _SortedGlobregs list (Spillcosts reuses it as a per-block scratch link) [C] */
    struct GLOBREG* prev;       /* 0x18 [C] */
    u32   num;                  /* 0x1c web number; bit index in all web sets [C] */
    SBV*  avail;                /* 0x20 registers still allowed (constraints, physreg kills, colored neighbours) [C] */
    int   refs;                 /* 0x24 references: uses + non-enterload defs [C] */
    BLOCK* firstBlock;          /* 0x28 first block of the range (in block-list order) [C] */
    BLOCK* lastBlock;           /* 0x2c last [C] */
    struct GLOBREG* hashNext;   /* 0x30 _GlobregHash chain [C] */
    u8    f34;                  /* 0x34 counts exit-save uses and same-instruction def+use [C]; meaning [I] */
    u8    pad35[3];
    PREF* prefs;                /* 0x38 [C] */
    TUPLE* lastDef;             /* 0x3c last defining tuple seen [C] */
    int   spillCost;            /* 0x40 weighted cost of keeping it in memory, signed [C] */
    u32   defPos;               /* 0x44 position of its def (section 4.3) [C] */
    /* 0x48..0x5f: split bookkeeping (ColProcessSplitGlobregs uses +0x50 as an SBV*) [I] */
} GLOBREG;
```

The [I] field meanings above are left unprobed: they are internal flags with no isolated source-level effect.

---

## 4. Spillcosts: the priority and spill-cost formulas

`Spillcosts` 0x10b10420 (`Color` calls it per class, and again after each round of splits). It walks the blocks in
block-list order (`FUNC+8`) and, within each block, the instructions in order. It reads each instruction's sources
(`+0x28`) and then its destinations (`+0x2c`) [C].

### 4.1 Block weight `w_b` [C]

```
w_b = 1                                  if !_Speed                  (/O1, /Os)
w_b = count*10000/maxCount + 1           if _PogoFlag == 2 (PGO); 1 if no counts
w_b = 1 << BLOCK.f17e                    otherwise (/O2)             10b1071b
```

`BLOCK+0x17e` is the **Lg loop nesting depth**. `LgFixupTails` (10b80000.c:6027) writes the loop's depth into each
block whose innermost loop is that loop, and updates `_LgMaxLoopNestLevel` [C]. (This settles il_fg.md's open
`f17e` field.)

Traced [C] with `t2.c`/`t3.c` at /O2:
- straight code: w = 1;
- one loop body: w = 2;
- two nested loops: w = 4;
- loop headers and latch blocks get the weight of their own depth.

At /O1 the loop body block also has w = 1 [C, t2.c `/O1`].

All four builds weight the loop body w = 2 at /O2 (`RC_loopw.cpp`, t2.c `loopw`, traced `regs --why b`) [C traced].
13.10 makes fewer immediate candidates there (5 webs against 8), so its P values are lower: b's terms are
`1*5*2 + 2*3*2` on 14.00/15.00 and `1*4*2 + 2*2*2` on 13.10.

### 4.2 Per-reference cost `c` [C]

For an operand that is a web of the class being colored (`opnd.kind == 1`, `RegClass[ty] == k`):

| Reference | Added to `spillCost` | Added to block cost `blk` and `refs` |
|---|---|---|
| use, foldable as a memory operand (`CmdCanDirectSubstitute`) | `+CmdSubCost * w` | `blk += CmdSubCost; refs++` |
| use, not foldable | `+CmdLoadCost * w` | `blk += CmdLoadCost; refs++` (sets fl5 4) |
| use by OPEXITSAVE (0x296) | `-(2 at /O2, 3 or 6 at /O1) * w` | `f34++` (no refs) |
| def by OPENTERLOAD (0x295) | `-CmdLoadCost * w` | none |
| other def, not foldable | `+(2 at /O2; fa08+3 for kinds 3..6, else 6 at /O1) * w` | `blk += same; refs++` |
| other def, foldable, full width (`opnd+0x14 & 0xf == 0`) | `+CmdSubCost * w` | `blk += ...; refs++` |
| other def, foldable, partial | `+0` | `refs++` |

`CmdLoadCost` 0x10c23740 [C]:
- /O2: 1 for an immediate candidate (SYM kind 13), else 2.
- /O1: kinds 3..6 cost `fa08 + 3`, other kinds 6. An immediate costs 2, 3 or 5 depending on its value.

`CmdSubCost` 0x10c237c0 [C]:
- /O2: 2 for a non-immediate SYM. An immediate costs 0 or 1.
- /O1: kinds 3..6 cost `fa08 + 1`, others 4.

`fa08` (0x10d0fa08) is "ebp is allocatable", that is, the frame is FPO and esp-based. It is cleared when
`FUNC+0xac & 0x20000000` is set or `& 0x40` is clear (`RegSimpleAssignBody` 0x10bda2a0) [C]. **So the /O1 costs are
encoding bytes**: an esp-relative frame access needs one extra SIB byte, and an absolute address takes 6 bytes [I:
an interpretation of the constants, which are [C]; no probe can show their intent].

In practice, at /O2 **every reference of an ordinary variable costs 2**. The traced `t4.c` shows +2 for each use,
def and enterload [C].

### 4.3 Block pressure and the priority [C]

At the end of each block (10b113cb..10b114e3):

```
P_b = number of distinct webs of this class referenced in b
      (dirty and clean, colored or not; physical registers do not count)
for each web v referenced in b:      v.priority += blk_b(v) * P_b * w_b     /* blk_b = sum of its costs in b */
for each web v live through b but not referenced in b:
                                     v.priority -= P_b * w_b                /* PGO: -= w_b */
```

Summed over blocks:

```
priority(v)  = sum over b referencing v of  w_b * P_b * blk_b(v)
             - sum over b that v lives through without a reference of  w_b * P_b
spillCost(v) = sum of the signed per-reference terms of 4.2 (weighted by w_b, not by P_b)
```

Traced checks (all exact, [C]):

| Probe | Web | Computation | Traced |
|---|---|---|---|
| t.c straight, P=4 | d (5 refs) | 10 * 4 | 0x28 |
| t.c straight, P=4 | b (4 refs) | 8 * 4 | 0x20 |
| t.c straight, P=4 | a (3 refs) | 6 * 4 | 0x18 |
| t.c straight, P=4 | c (2 refs) | 4 * 4 | 0x10 |
| t2.c `ifw` | a (def + 3 uses in B1, P1=3) | 8 * 3 | 0x18 |
| t2.c `ifw` | b (def in B1; 5 uses under `if`, P2=1) | 2*3 + 10*1 | 0x10 |
| t2.c `loopw` | b (def in B1, P=5; 1 use in the loop body, P=3, w=2; live through B2 (P=2), B4 empty) | 10 + 12 - 2 | 0x14 |
| t3.c `nest` | b (def P=4; use under `if` P=2; use in the inner `else` P=1) | 8 + 4 + 2 | 0xe |
| t3.c `nest` | c (def P=4; 2 uses in the inner `then` P=1; live through B2 P=2) | 8 + 4 - 2 | 0xa |
| t3.c `loop2` | b (def P=5; use at depth 2, P=3, w=4; live through three blocks with P\*w = 2, 4, 4) | 10 + 24 - 10 | 0x18 |

`defPos` (+0x44) is written by `RewriteAsGlobregs` 0x10b5ae50 (10b50000.c:9022-9033). A counter starts at 0 per
function and goes up by one for every real instruction. Blocks are visited in block-list order, but each block is
walked **from its last instruction back to its first**. Each def stores the counter, and later defs overwrite it,
so the value that remains is the def visited last [C].
- Within one block, a smaller source position gives a *larger* `defPos`. `RC_tie.cpp` `ti_blk`: a, b, c (prio 18 each) get
  esi, edi, ebx in def order [C traced][M].
- With defs in several blocks, the def in the block that comes latest in the list wins. `RC_tie.cpp` `ti_xblk`:
  a, c (entry block) and b (after an `if`) all have prio 12; b (defPos 34) pops first and takes esi, then a (13)
  edi, then c (9) esi again [C traced][M], all four builds.
- Split webs copy the parent's `defPos` (`RewriteSplitGlobregs` 10b10000.c:2601) [C].

### 4.4 Where "1/2 under an if" comes from

A reference is worth `2 * P_b * w_b`, not 2. A block under an `if` usually references fewer webs than the
straight-line block around it, so its references count for less. Values that are live through such a block without
being used there also lose `P_b * w_b` each. With 3 to 5 webs in the main block and 1 or 2 under the `if`, that gives
the observed "about 1/2" at one level and "about 1/3" at two levels. It is a ratio of pressures, not a fixed
constant [C formula; I as the explanation of the M&B grids].

---

## 5. OrderGlobregs and the coloring loop

**`OrderGlobregs` 0x10b11770** [C]:
1. It visits `_GlobregHash` buckets 0..1023 in order, each chain head-first (head insertion, so within a bucket
   the newest web comes first).
2. For each web of class `k` it calls `CmdSetConstraints` (sets `avail`).
3. If `avail` is empty and fl5 2 is clear, it sets fl5 2, adds the web to the to-split set, and calls
   `InsertAdditionalSplitsOne`.
4. Every web, either way, goes into the sorted list `_SortedGlobregs` through **`InsertInList`** 0x10b11970.
   That function sorts by **`priority` descending (signed), then `defPos` descending (unsigned)**. A new web goes in
   front of the first node that is not greater than it, so on an exact tie the web inserted later comes first
   (10b11986..10b119c3) [C].
5. ebp is removed from the candidate register sets when `fa08 == 0`, and ebx when `fa00 == 0` (0x10d0fa00, cleared
   by `MdmWillUseEbxConservative`).

**Main loop** (`Color` 10b0d33b..10b0d48b) [C]: pop the head `v` of `_SortedGlobregs`, then:

1. If `v.spillCost < 0`, or `v.spillCost == 0` and the web is not (single block, not immediate, `refs > 2`,
   `_Speed`): call **`SpillOrDefer`**.
2. Else, if fl5 4 is clear (every reference can be folded into memory) and `BetterToDefer` says yes:
   `BetterToDefer` sets `v.priority = min(component priorities) - 1`, and `v` is re-inserted with `InsertInList`
   (10b19e60).
3. Else, if `avail` is empty: **`HandleCurrUncolorable`** (split the range, section 6).
4. Else: `InsertSplitPoints`, then **`ChoosePhysReg`**, then `UpdateAffectedGlobregs`, which removes the chosen
   register from interfering webs.
5. If a split round produced new pieces (`_NeedNewLives`), the colorer runs `RemoveSplitGlobregs`,
   `ColProcessSplitGlobregs`, `Spillcosts` again (new pieces only), `SpillObvious`, then `InsertNewGlobregs`, and
   goes on.

**`ChoosePhysReg` 0x10b14b70** [C] builds `_ChoosePhysReg_weights[r]` (zeroed at entry):
- For each interfering web `n` with `n.spillCost > 0`:
  - for each preference `(r, cnt)` of `n` where `r` is in `n.avail`: `weights[r] += cnt`;
  - if `n.avail` has exactly one register `r`: `weights[r] += 100 * n.spillCost`.
- For each preference `(r, cnt)` of `v` itself: `weights[r] -= cnt`.
- Pick the register in `v.avail` with the **lowest weight, scanning `_RegOrders[k]` in order, strict `<`**, so ties
  go to the earlier register. `_RegOrders[0]` (0x10ce59a0) = **eax, ecx, edx, esi, edi, ebx, ebp**, `[5]` = mm0..7,
  `[6]` = xmm0..7, and `[1]` is filled at run time (GF0.. in the trace).
- Afterwards each move partner of `v` gets a preference for the chosen register (count + 1).

Preferences come from moves (`mov`, `movaps`, `movss`, `movsd`, `movapd`, `movdqa`, `movq`) and `lea` between a web
and a colored web or physical register (Spillcosts 10b107e9..10b1085a, 10b10dd5) [C].

**Calls.** A call's clobbered physical registers are removed from the `avail` set of every web live at the call
(Spillcosts' physreg and kind-11 operand paths). A web that lives across a call therefore takes the first free
register of **esi, edi, ebx, ebp**. Traced in t.c, t2.c and t3.c [C]. That holds for a callee the unit does not
define. A callee defined anywhere in the same unit (static or extern, above or below the caller) kills only the
registers it uses: in `RC_callee.cpp` `leaf` uses only eax, and the two values live across it take **ecx and edx**
(their cands start at ecx); with `leaf` only declared they take esi and edi [C traced][M], all four builds.

---

## 6. Spilling: SpillObvious, SpillOrDefer, uncolorable webs

**`SpillObvious` 0x10b11560** runs right after `Spillcosts` [C]:
- `refs < 2` and (`spillCost < 1`, or the last def is not an enterload): `Spill` at once.
- `refs == 2`: `CmdSubstDef` (fold the single def into the single use).

A parameter used once therefore stays in memory: in t4.c, `x` (one use, enterload) was spilled here. Its enterload
cost (-2) cancels the use (+2) [C].

**`SpillOrDefer` 0x10b0d690** [C]:
1. If fl6 1 is clear and `RestrictToReg(v)` succeeds: push `v` back on the front and retry.
2. Else, the first time (fl5 0x40 clear, list not empty): set 0x40 and re-insert with **`InsertByBenefit`**
   0x10b119f0. It walks past the non-deferred webs into the deferred tail, which is kept in **ascending `spillCost`**
   order (a new web goes before the first deferred web with cost >= its own), and puts immediate candidates at the
   very end. Deferred webs are therefore retried after all the others.
3. The second time: if fl6 1 is clear and (single block, or `f34 == 0`): **`Spill`**.
4. Otherwise `ForceSingleBlockLives` (or `ForceNoAirlocks`) splits `v` into per-block pieces, together with every
   later deferred web of the same immediate-ness. Their numbers go into the returned set, which drives the next
   split round.

**`HandleCurrUncolorable` 0x10b0d830** is reached when the cost is positive but no register is left [C]:
1. Set fl5 2 and split at more points (`InsertAdditionalSplitsOne`, byte-register variant if fl5 0x80; immediates
   first through `CmdSpillImmedBytes`).
2. If `refs > 1`, try `ColCheckForOneInstrLife`.
3. If fl5 8 is clear and `CanRecalc`, then `TryToRecalc` (rematerialize the value instead of reloading it).

**`RemoveSplitGlobregs` 0x10b11ad0** unlinks every web with fl5 2 from `_SortedGlobregs` before the split pieces
are rebuilt [C].

**`SubsumeGlobregs` 0x10b0fdb0** (coalescing, runs before any costs) [C]:
- For a move `dst = src` between two webs, where src is not an immediate and has no indirection, and the
  instruction has a single dst:
  - **forward:** if `dst` has exactly one def (this move), its later uses are renamed to `src`, up to the point
    where `ComponentKilled` says `src` is redefined. This needs `size(dst) <= size(src)`.
  - **backward:** if `src` has exactly one def and that def is the instruction just before the move (same width),
    the def is retargeted.
- The def counts it uses (`+0x24`) are reset to 0 at the end, so they do not carry into Spillcosts.

So a copy that is coalesced away adds no references, and a copy that survives adds a use of `src` and a def of
`dst`. `RC_copy.cpp` `cp` (`int b = a; a = g(a); ... u(b)`): the copy survives; a's first web has 3 refs (its def,
the copy, the call argument) and b has 3 (the copy and two uses), prio 18 each [C traced], all four builds.

---

## 7. Leftover temporaries: regasg.obj (scratch rotation)

After `Color`, `RegSimpleAssignBody` 0x10bda210 walks every instruction and calls `RegAssignSrcs` and
`RegAssignDsts`. `RegAssign` 0x10bda7c0 gives each non-web temporary a register [C]:

1. **Copy hint:** if the temporary is defined by a move from a register (or a `lea` from a register base), and that
   register is free and allowed, take it.
2. **/O2 rotation:**
   - Start at `_NextIntPos` and scan `_IntPhysRegs` (0x10d0cfd0 = eax, ecx, edx, esi, edi, ebx, ebp).
   - Wrap back to the start at the first entry that is 0 or > 3, so **the rotation covers only eax, ecx and edx**.
   - Take the first register that is free (`_RegMap`) and does not conflict (`_BvConflicts`).
   - Then set `_NextIntPos` to the next slot.
   - `_NextIntPos` is reset to eax only once per function. Block starts (`RegResetRegMap`) refill `_RegMap` from
     the live-in registers but keep the position.
3. **Fallback:** the first free register of the whole list, then any register that does not interfere, and finally
   `RegSpill`.
4. Byte-register temporaries (fl5 0x80) are limited to register numbers < 6 [C].

The trace of `t6.c` shows the position moving across picks (edx, then back to eax, ...) [C].

---

## 8. x87 (class 1 and fpreg.obj), overview

- **Coloring.** In this build x87 values are colored by the **same** loop as integers, as register class 1 with
  virtual registers GF0..GF7 (register numbers 0x1f..0x26). They use the same Spillcosts priorities and the same
  `ChoosePhysReg`.
  - Traced `t7.c` [C]: three double webs get GF0, GF1 and GF2 in priority order 0x34, 0x2c, 4, before class 0 is
    colored.
  - `FPColor` 0x10c33550 (with `FPSpillcosts` and `FPAddToPriorityList`) is an alternative path. It runs only if
    `Module+0x44c & 0x8000000` is clear. It did not run in any probe, with /fp:precise, /fp:fast, /fp:strict,
    /arch:SSE or /arch:SSE2 [C]. In that path priority equals spill cost: both are `sum cost * 2^depth`, with no
    pressure factor (10c30000.c:3294-3529) [C code].
- **Stack mapping.** **`X87MapGFxToSTx` 0x10c2eb00**, called from `dop2_CodegenUpdate` 0x10c07400 after register
  allocation, turns GFx into ST(i) [C]:
  1. It rebuilds the flow graph and runs `PerformPreMappingPeepholes`, `BuildLiveOnEntryAndExitSets` and
     `SetGFxLifeAndDeath`, then `FgWeightFlowGraph` (no PGO).
  2. It processes blocks from a worklist (`InitializeFunctionBlockList` seeds the blocks with no predecessors
     [I]: in source-level code only the entry block has none, so no probe separates this). For each block it
     copies the entry stack model and runs `ProcessBlock`. `ProcessBlock` dispatches each instruction
     through the handler table at 0x10d08fe8 (`processEgFADD`, `processFMOV_*`, `processCALL`, ...), which raise
     sources to ST0 with `fxch` and pop dead values.
  3. `FinalizeBlock` pushes `fldz` for GFx registers that must be live on exit and are not. Then, for each
     successor whose entry layout is already fixed, `BalanceBlockWithConstraint` adds `fxch`s
     (`InsertExitToEntryAdjustments`). Otherwise it passes its exit layout on as the successor's entry layout
     (`PropagateBlocksLiveOnExit`). So **the first block to reach a join sets the stack order there**; after a
     two-way branch that is the fall-through arm (x87.md 11). `RC_x87join.cpp` `j1`: the fall-through arm does
     `fxch ST(1)` to store x, and that layout holds at the join; the taken arm stores y from ST0 and then gets the
     `fxch ST(1)` on its edge [M], 14.00 and 15.00. 13.10 has no `X87MapGFxToSTx` (tools/c2x87.py): its arms use
     `fld ST(1); fstp` and plain `fst` and need no fxch [M].
  4. `PerformPostMappingPeepholes`, then `FPPeeps` (fxch elimination, `try_fxch_peep`, ...).

---

## 9. Mapping to the empirical levers

M&B (`mountandblade-decomp/docs/codegen.md`) is **cl 13.10**, so its rules are only evidence for 14.00. The
vs2005 doc is this compiler (cl.exe is built /O1).

| Lever | Explained by | Verdict |
|---|---|---|
| M&B 7 "callee-saved order": webs that live across a call are colored in priority order, each takes the first free of esi, edi, ebx, ebp | `InsertInList` order, `ChoosePhysReg` scan of `_RegOrders[0]` (eax, ecx and edx removed by the call), weights 0 when there are no preferences | **explained** [C], for calls to functions the unit does not define (a callee defined in the unit leaves ecx and edx free, section 5) |
| "priority = reference count, defs included" | priority = `2 * refs * P` in a single block, and P is the same for every web there | **explained** [C]; holds only within one block |
| "ties go to the web defined first" | `defPos` descending, counted backwards inside a block | **explained** [C]; across blocks the def in the later block wins [C traced][M] (`RC_tie.cpp`, section 4.3) |
| "under one if ~1/2, two levels ~1/3, `a && b` 1/3, else arm 1/2, boundaries a little above the exact fractions" | weight per reference is `P_b * w_b`; unreferenced live-through blocks subtract `P_b * w_b` | **explained as a pressure ratio** [C formula], not a constant. The "a little above" fits the def cost and live-through terms [I] |
| "loop bodies don't count more" | /O2: `w_b = 2^depth` [C traced]. /O1: `w_b = 1` [C traced] | **contradicted for /O2 on all four builds, 13.10 included** (`RC_loopw.cpp`: a loop use counted `2*3*2 = 12` against `2*5 = 10` straight on 14.00, `2*2*2 = 8` against `2*4 = 8` on 13.10) [C traced]. It looks flat only because the loop block has low P. True for /O1 code (vs2005 cl.exe) |
| "600-case switch changes none of this" / "in a switch long-lived webs are dead in other cases" | P is per block; unreferenced live-through blocks only subtract | consistent [I: an M&B observation, not reprobed here] |
| "Limit: in the 70 KB interpreter the order depends on function-wide state" | priority sums over every block in the range, plus split rounds (`HandleCurrUncolorable`, `ForceSingleBlockLives`) and `ChoosePhysReg` preference and constraint weights | **explained in principle** [I: about one M&B function, not reproducible from a small probe] |
| "x = q(x) starts a new web" | webs are lifetimes (`ComputeLifetimes`) | **holds** [C traced]: `RC_web.cpp` `nw` gives x two webs of 4 refs each, all four builds |
| "naming a call result counts its copy and the coalesced two-address result" | copies that survive `SubsumeGlobregs` add a use and a def; two-address results are defs of the same web | **holds** [C traced], all four builds: `RC_copy.cpp` (section 6); `RC_web.cpp` `tw`: `a += g(2)` is a use and a def of a's one web (6 refs, no second web) |
| "Naming a global's re-read raises its register priority" | globals are aliased candidates (kind 7/8), and a named local is a separate web with its own refs | **holds** [C traced][M], `RC_gname.cpp`: the global's own web in `gn0` has 1 ref (its def is the enter-load, which adds no block cost); the copy `l = G` in `gn1` adds its def in the entry block. Prio 24 -> 32 on 14.00, 60 -> 78 on 15.00 (it unrolls the loop), 20 -> 29 on 13.10; the copy pops ahead of the parameters and takes an earlier register (edi -> edx; 15.00 edx -> ecx) |
| M&B 6.2 "commutative and SIB order keyed by ids" / distant flips | web numbers follow SYM key order (section 2); exact priority ties fall back to `defPos`, then insertion order | partly explained [I]; operand order is not in this module |
| M&B 6.4/6.5 "scratch-register rotation is a chain from the first temporary" | regasg `_NextIntPos` rotates through eax, ecx, edx over the whole function in instruction order and is reset only at function entry | **explained** [C code]; one extra temporary shifts every later pick |
| M&B 6.5 "callee visibility sets the global register map" | callees defined in the unit kill only the registers they use, so caller-saved registers stay in `avail` across their calls | **holds** [C traced][M], `RC_callee.cpp`: values live across `leaf` take ecx, edx whether `leaf` is static or extern, defined above or below the caller; esi, edi when it is only declared. All four builds (section 5) |
| M&B refinement "use counts are taken before cl duplicates tails, so a duplicated statement counts once" | 14.00 runs `FgEarlyTailDup` **before** `Globregs`, and Spillcosts scans the copied block (block number -1 in t4.c) | **contradicted for 14.00** register priorities (a duplicated tail counts in each copy) [C]. The M&B note was about stack slots, which are out of scope here |
| vs2005 "declaration order of locals moved registers" (`strqlen`) / "declaration order sets the order of zero-initialised registers" | equal priorities are broken by def position, so the variable initialised first is colored first; declaration order does not enter | **declaration order: contradicted; initialisation order: holds** [C traced][M], `RC_decl.cpp`: a, b tie at prio 12 and the one defined first takes esi; swapping the declarations changes nothing, swapping the defs swaps the registers. All four builds. A declaration-order effect seen in a real function must come through a different def order or different P |
| vs2005 /O1 code generally | `_Speed == 0`: no loop weights; costs are byte counts (esp-frame 4, ebp-frame 3, absolute 6) | [C code] |

---

## 10. Open questions

- The exact meaning of fl5 08, fl6 02 and `f34`, and what `RestrictToReg` and `BetterToDefer` /
  `ComputeFoldLegalityAndWorth` accept.
- `InsertSplitPoints` and `UpdateAffectedGlobregs`: where split moves go when a high-priority web takes a register
  that a live neighbour needed.
- `CmdSetConstraints` per-instruction constraints (byte registers, shifts in ecx, div in eax and edx) and how they
  feed the `100 * spillCost` weight.
- Exact `(priority, defPos)` ties: `defPos` is unique per def instruction, so they need two webs defined by one
  instruction; untested. (The cross-block `defPos` tie-break is now probed, section 4.3.)
- What `Module+0x44c & 0x8000000` means (it is set in every probe tried), and so when `FPColor` ever runs.
- How `X87MapGFxToSTx` orders its worklist beyond a two-way branch (the fall-through arm's layout wins at the join,
  section 8).
- Settled: cl 13.10 weights loops like 14.00 (section 4.1); so the flat loops M&B saw must come from low P in their loop blocks [I].
