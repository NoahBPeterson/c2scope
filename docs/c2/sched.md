# c2 instruction scheduler: which order independent instructions come out in

This spec covers `Schedule@8` (sched.obj, schedmd.obj, dag.obj) in cl 14.00.50727.762's `Bin/c2.dll`: the last
pass that reorders instructions, after register allocation, flow opts and LoFinalLower, just before encoding. It
only permutes instructions inside a region; registers, addressing modes and code size are already fixed (except for
the displacement rewrite of rule 8). It is meant for matching: given a draft, predict the order of loads, stores,
pushes, compares and stack adjustments the compiler will emit, and know which source change can move them.

**[C]** = CONFIRMED (read in the code, and/or shown by a compile or trace; each claim says which). **[I]** =
INFERRED. Probes: `work/scratch/sched/` (`s*.cpp`, `q.cpp`, `c.cpp`, `w.cpp`, `v.cpp`, predictions in `*.pred`
written before compiling). Tool: `uv run tools/c2sched.py <src> --func SUBSTR [--all] [--verify [-v]] -- <cl args>`
prints, per region, the DAG numbers of every instruction (lex, height, successors, flags, priority) and the order
it issued them in; `--verify` checks rules 2-5 against every region of the compile.

The model in one sentence: **within each region, a list scheduler emits, one integer and one x87 instruction per
cycle, the ready instruction with the highest priority `1024·(height + successors) + 32768·[reads memory] +
2²³·[sets the flags of the region's final jcc]`, ties going to the earlier instruction.**

Checked on 13,798 scheduled instructions (the Fallout 3 unit `work/scratch/f3/pk/v_gg.cpp` at the game flags,
and `src/cl/*.cpp` at their `/O1` flags): height rule 13,798/13,798, priority formula 13,790/13,798, selection
model 13,795/13,798 picks (section 9).

---

## Rules you can use when matching

1. **Where it runs.** `dop2_PreEncode` 0x10c07850 calls `Schedule` only with the global optimiser on (`/O1`,
   `/O2`, not `/Od`), and not for functions with SEH (`__try`: FUNC.sym+0x20 & 0x1000), and not for functions
   with flag 0x100000 unless they also have flag 8. Observed **[C, compiles]**:
   - `/Od`: never scheduled. `__try/__except`: not scheduled (seh.cpp).
   - C++ EH: a function with only a destructor local is scheduled under `/EHsc` and `/EHa`; a function with
     `try/catch` is scheduled under `/EHsc` and `/EHs` but **not under `/EHa`** (tc.cpp). So 0x100000 is
     "has a C++ try under async EH" **[I]**.
   - `/O1` and `/O2` both schedule. `_Speed` only changes which dependencies may be broken (rule 8) and an x87
     check (`SchmdBadForFPSch`).
2. **Regions.** Scheduling starts after `OPPROLOGEND` and walks to `OPEXIT`. `SchBlockTail` 0x10c725c0 cuts a
   region at the next BRANCH (kind 18, included as the region's last node), SWITCH, LABEL, EXIT or
   `OPEPILOGSTART`, **or after 81 tuples** (big.cpp: a 300-instruction straight block came out as regions of
   81, 81, 81, 59, each unchanged). A region with fewer than 2 instructions is skipped (`SchNeedSchedule`).
   Nothing ever moves between regions, so a label (any jump target, loop head) is a hard wall. **[C code +
   big.cpp]**
3. **Barriers inside a region (control points).** `DagBuildGraph` 0x10b1be00 makes these nodes depend on every
   earlier node and every later node depend on them: **calls**, returns, EH instructions (`OPPUSHSTATE` and the
   other kind-21 tuples except `OPPROBE`; `OPSIDEEFFECT` is exempt), `int`, `pushf`, `fwait`, `emms`, `cli`/`sti`,
   `OPSPADJUST`, `OPHARDINTRIN`, pragmas (an `OPUSERLABEL` only if it is an annotation), TUPLIST, and a store to the
   EH registration record (`_GEH_rec`) in a function with FUNC+0xac & 0x140000, or to `_SEH_rec` under SEH. The
   state store of a destructor-only function under `/EHsc` is *not* a barrier (a9, rule 10); under `/EHa` it
   behaves like one, consistent with the 0x140000 condition **[I]**. So **nothing crosses a call**: argument setup stays between the previous call and its
   call, and the code after a call stays after it. **[C code, every probe]**
4. **Dependencies (DAG edges)** **[C code; latencies from 13,798 edges, section 9]**. Edge types: 1 true (read
   after write), 2 anti (write after read), 4 output (write after write), 0x40 memory, 0x2000 volatile, 0x80000
   control, combined. Registers (including esp and the flags) give true/anti/output edges. **Only a true edge has
   a latency**, the producer's latency:
   - the P5 table `OpSchedInfoP5` 0x10cdd328 (12-byte rows: latency, cost, unit; dump: `work/scratch/sched/optab.txt`):
     mov/push/pop/lea/add/sub/and/or/xor/cmp/inc/dec/test/shifts 1, movzx/movsx 3, imul 10, mul 11, div 17,
     idiv 22 (plus 0/8/24 for an 8/16/32-bit operand, `SchmdAdjustDagLatency`), call 0, fld 1, fadd/fmul 3,
     fdiv 39;
   - plus `SchmdAdjustDagLatency` 0x10c60ab0: an ALU op (push/pop/add/adc/and/cmp/or/sbb/sub/test/xor/inc/dec/neg)
     with a memory source +1, with a memory destination +2;
   - plus 6 when a narrow register write feeds a wider read (partial-register stall, `_DAT_10cd9808`);
   - the compare feeding the region's jcc: latency 1 (`SchmdFindCBranchDep`).
   All other edges have latency 0.
   **Memory disambiguation** (`DagChkMemrefDefs`/`Refs`, which call the PAS interference test `ItfPartialInterf`
   / `ItfTotalInterf`) behaves like this **[C, probes s3/s5]**:
   - two accesses through the same base register at disjoint byte ranges are independent (n1: `[eax+4]`/`[eax+8]`
     loads move above a store to `[eax]`; n3: a byte store at `+21` does not hold back loads at `+0`/`+4`);
   - through different base registers: dependent (n2, m2), also between `int*` and `short*` (m8: no type-based
     aliasing);
   - a pointer access and a named global: dependent (m3, m4, n4, n5);
   - two different globals: independent (m5, n4);
   - stack parameters and locals whose address is not taken: independent of everything (every probe: `mov reg,
     _p$` loads move freely).
5. **Height** (`SchComputeStaticPriority` 0x10c71f00): walking the region backwards from its exit pseudo-node
   (height 0), **height(n) = 1 + max over successors s of (height(s) + latency(n→s))**. So height counts the
   instructions on the longest path to the end of the region, plus the latencies on it. **[C code; 13,798/13,798]**
6. **Priority** (static; `_preSched` is never set in this build, so the dynamic priority equals it):

       prio = (height + successors) << 10        // successors = number of DAG out-edges (DAGNODE +0x26)
            + reads_memory << 15                 // a SYM or INDIR source operand (DAGNODE +0x40 bit 2)
            + sets_branch_flags << 23            // true-dep predecessor of the region's final jcc / jecxz
            + 1 if it is an x87 store            // (+0x40 bit 3 and type class 5)

   The shifts are the table `_SchmdHWT` 0x10cd981c = {23, 10, 10, -1, -2, 15, 0} (the critical-path bit has
   shift -1, i.e. no weight). **[C code; 13,790/13,798: the 8 misses are nodes that gained a successor after
   their priority was computed]**
   The memory bonus is worth 32 height levels and the branch-flag bonus beats everything: in practice **loads
   go as early as their dependencies allow, and the compare goes as early as its operands and the last earlier
   flag writer allow**.
7. **Selection** (`SchSchedGraph` 0x10c720d0, `SchSelectInstrs` 0x10c722c0, `SchmdGetP6FuncUnit` 0x10c5f710)
   **[C code; 13,795/13,798 picks]**:
   - A node enters the ready list when all its predecessors have been issued (or only breakable ones remain,
     rule 8). The list is sorted by priority, descending; **equal priority goes to the lower lexical index, i.e.
     source/IL order** (`SchAddToReadyList` 0x10c72490).
   - Each cycle, walk the list and issue the first node whose unit is free and whose earliest cycle `est` is <=
     `cycle + busy[unit]`; then walk again from the top, up to 3 issues (`_SchmdIssueRate`). Issuing adds the op's
     cost to `busy[unit]`; every cycle subtracts 1; a unit takes one node per cycle and none while `busy >= 3`.
   - Units (low byte of the table's unit field): 0 = every integer instruction, 2 = x87, 4 = mul/imul ("any free
     unit", then counted on unit 0). So **at most one integer and one x87 instruction per cycle**, which is why
     integer instructions interleave with x87 runs (x87.md rule 15) and never with each other.
   - Because `busy` is added to the cycle in the `est` test, latency rarely reorders integer code: after a movzx
     (latency 3, cost 3) its consumer is still eligible on the next cycle (v.cpp, a failed prediction). Latency
     matters only where it exceeds the cost (ALU with a memory operand, fadd/fmul) and for imul/div, whose cost
     stalls the integer unit (a5: nothing issues for 8 cycles after `imul`, but the order is unchanged).
   - Practical shortcut that reproduced every probe: **repeatedly emit the ready instruction with the highest
     (prio, -lex)**.
8. **Dependency breaking (AGI avoidance).** `SchmdDepIsBreakable` 0x10c5f930 marks a true edge from `add/sub reg,
   imm`, `inc`/`dec reg`, `lea` (and op 0x22 by 1/2/4/8 when it fits a scale) to a later memory operand `[reg+d]`
   as breakable;
   `SchmdBreakDep` 0x10c601e0 lets the consumer issue first by rewriting its displacement. The reverse also
   happens: the pointer bump is hoisted above earlier uses, whose displacements are reduced. Seen in the Fallout 3
   draft of 0x008e7d40 (v_gg.cpp, `FUN_00c7e9a0`): source order `fld [esi+48]; fstp; fld [esi+52]; ...;
   add esi,32` came out as `fld [esi+48]; add esi,32; fstp; push esi; fld [esi+20]; ...` (three breaks; the
   listing shows `[esi+20]` for the IL's `[esi+52]`). Some cases need `_Speed` (`/O2`). A plain loop
   `add edx,4 ... mov ecx,[edx]` did not move (a3: an anti-dependence on ecx held the load), so whether a
   break happens needs the trace. **[C code + trace + listing]**
9. **Prolog and epilog** (`SchmdAdjustProEpi` 0x10c5f000, `SchmdRestoreProEpi` 0x10c5f510) **[C code + s6/w
   probes]**:
   - Before scheduling, `OPPROLOGEND` is moved up to just after the last prolog instruction that must stay
     (push ebp / mov ebp,esp / and esp,-N / frame-register setup, and `sub esp,N` **if the first block stores a
     scalar local**, `SchmdStackDefInFirstBlk`). The remaining prolog instructions (at most 10: callee-saved
     `push`es, `sub esp,N` otherwise) join the first region and are scheduled like any other. Likewise
     `OPEPILOGSTART` moves down so the epilog `pop`s join the last region. Not done for SEH functions, C++-EH
     functions (flags 0x140000) or under POGO instrumentation. If a prolog instruction ends up more than 29
     tuples into the block, the prolog is put back.
   - Consequences: `push ebx; push esi; mov esi,[esp+p]; mov eax,[esi]; push edi` (p1); `push ebx; mov ebx,
     [esp+n]; push esi; push edi` (p2): parameter loads slip between the callee-saved pushes, each push still
     before the first write of its register (anti-dependence) and the pushes in order (esp chain).
   - `sub esp,N` sinks when the first block has no scalar-local store: in w1 and m7 it comes out **after** the
     stores into the local array (writes below esp, offsets adjusted); in w3 a `lea eax,_t$[esp]` precedes it.
     With a scalar local store in the first block (w2) it stays first.
10. **Patterns that follow** (each held in the probes; use them to read a listing back to source order):
    - **Compare early:** a block ending in jcc puts the `cmp`/`test` right after the last instruction that writes
      its operands or the flags; flag-neutral moves, stores and leas move below it: `cmp [x],5; mov [ecx+4],eax;
      jle` (a1, c1, q3), and `cmp DWORD PTR _x$,0` first in c2 when nothing before it writes the flags.
    - **Loads first, stores last:** loads (memory bonus) rise to the top of the region; a store with nothing
      after it but the call sinks to **just before the call, after the argument pushes**: `push 0; push 0; push 0;
      mov [ecx],eax; mov [ecx+4],eax; call` (q1, n1, w2). Under `/EHsc` the EH state store `mov
      __$EHRec$.state, 0` likewise sinks to just before the call (a9); under `/EHa` it stays put (rule 3).
    - **Argument loads before pushes:** `g(p->a, p->b, p->c)` gives `mov ecx,[eax+8]; mov edx,[eax+4]; mov
      eax,[eax]; push ecx; push edx; push eax` (a4): the push chain gives the first-pushed argument's load the
      greatest height.
    - **Call result copies sink:** `call h; mov ecx,_y$; push ecx; mov esi,eax; call h` (q2, p1): the copy of
      a call's result into a callee-saved register goes below the next call's argument setup.
    - **cdecl cleanup sinks:** `call; mov eax,[esi+12]; add esp,12; ...` (f2, a7, q9): the `add esp,N` after a
      call has a low priority and goes after loads that follow the call (stack-symbol offsets are adjusted).
    - **Epilog loads:** the return value's load comes before the `pop`s (f2, q9).
    - **Ties keep source order:** independent instructions with equal priority (two stores before a call, two
      equal chains) come out in IL order, so swapping two source statements swaps them only when they tie.

## How to move an instruction from source

Registers are allocated before this pass, so only priorities and dependencies can be changed:
- Height and successor counts come from the DAG: adding or removing a use of a value, or changing which value
  feeds a push, changes them (q4: in `a*b + c*d` the load of `c` jumps above the first `imul`, while in `a*b +
  (c^d)` (a5) it does not).
- A value the register allocator puts in the same register as an earlier value creates an anti-dependence
  that pins the order (m1, m6, n6: a load into eax cannot rise above a store that reads eax). This is often why
  an expected reordering does not happen.
- Aliasing: going through the same base pointer (rather than a second pointer to the same object) lets loads
  pass stores at other offsets (rule 4).
- A label (loop head, `if` join, any jump target) ends the region: code cannot move across it.

---

## Data structures

- **DAGNODE** (0x44 bytes, `DagCreateNode` 0x10b1c650): +0x00/+0x04 node list, +0x08 predecessor edges, +0x0c
  successor edges, +0x10/+0x14 ready-list links, +0x1c tuple, +0x24 unissued predecessors, +0x26 successor
  count, +0x28 breakable predecessors, +0x2c static priority, +0x30 dynamic priority, +0x34 earliest cycle,
  +0x38 height, +0x3a lexical index (`_Dag_LexicalOrder`), +0x3c latency, +0x3e unit, +0x40 flags (bit 0
  critical path, 1 sets branch flags, 2 reads memory, 3 writes memory, 4 issued, 5 control point). **[C]**
- **DAGEDGE** (0x18): +0x00 next successor edge, +0x04 next predecessor edge, +0x08 from, +0x0c to, +0x10 type
  bits, +0x14 latency, +0x16 break kind. **[C]**
- Globals: `_dagGraph` 0x10d0fb88, ready list `_readyHead` 0x10d0fb78 / `_readyTail` 0x10d0fb74, issue array
  0x10d0fb60, cycle `_SCHED_ccc` 0x10d0fb4c, unit busy counters 0x10d0f810 (stride 12), `_preSched` 0x10d0fb48
  (only ever written 0, in `Schedule`). **[C]**

## Probes (predictions written before compiling, `*.pred`)

| probe | prediction | result |
|---|---|---|
| s2 a1 | `cmp x,5` right after x's load, above the store | held |
| s2 a4 | loads c, b, a, then three pushes, call | held |
| s2 a7 | p's load issued before `add esp,12` | held |
| s2 a3 | loop load issued above `add p,4` with displacement +4 | **failed** (anti-dependence on ecx) |
| s2 a5 | imul first, xor behind it | held (trivial) |
| s2 a9 | destructor-only EH function scheduled under /EHsc, not under /EHa | /EHsc held; **/EHa failed** (scheduled; only try/catch under /EHa is excluded) |
| s3 m2 m3 m4 m5 m8 m9 | aliasing outcomes of rule 4 | held (6) |
| s3 m1 m6 | load above a store at another offset | inconclusive: same-register anti-dependence (rerun as n1/n3) |
| s5 n1-n5 | aliasing with distinct registers | held (5) |
| q1 | loads, pushes, stores (source order), call | held in shape; the two loads came out x before p |
| q2 | result copy below the next argument setup | held exactly |
| q3 | `cmp y,3` first | held exactly |
| q4 | `mov a; mov c; imul; imul; add` | held exactly |
| q6 | load before the second argument push, store before the call | partly: the second call became a tail jump |
| q9 | `add eax,[esi+4]` before `add esp,4` | partly: load before `add esp` held, the allocator chose `mov ecx,eax; mov eax,[esi+4]` |
| c1 c2 | cmp right after the last flag writer / first | held exactly (2) |
| v1 v2 | lea fills the movzx latency slot | **failed** (busy term hides the latency, rule 7) |
| w1 w2 | `sub esp` sinks / stays first | held (2) |
| w3 | `sub esp` stays first | **failed** (a lea of the local came before it) |

Totals: 25 held, 3 partly, 4 failed, 2 inconclusive. The whole-compile checks (`--verify`) are the stronger
evidence for rules 5-7: on v_gg.cpp (/O2 game flags) and the seven `src/cl` units (/O1), 13,798 nodes: height
13,798, priority 13,790, selection with the unit/busy model 13,795 (the 3 misses are at cycle 0 of x87 regions in
v_gg.cpp), plain "highest (prio, -lex) with est <= cycle" 13,658 of 13,729.

## Unknown / not covered

- **The aliasing rule behind rule 4 (when a load may cross a store), read in the code. [C code; probes where
  named]** `DagChkMemrefRefs` 10b1d0f0 / `DagChkMemrefDefs` 10b1d2d0 add a memory edge (type 0x40) between two
  memory operands of a region exactly when `ItfPartialInterf` 10b71f80 returns non-zero. A load never crosses a
  call: calls are control points (rule 3), whatever they touch. `ItfPartialInterf(A, B)`:
  - **named variable against named variable** (operand kind SYM, or a register operand that stands for one):
    interfere iff same base symbol and overlapping byte ranges (`offset`, `offset + size` at sym+0x24/+0x20).
    Two different globals, or two fields of one struct variable, never interfere.
  - **dereference against named variable**: `ItfIsMemberOfPas` 10b72660 (after stack packing
    `ItfStackLocIsMemberOfPas` 10b72930): is the variable a member of the dereference's points-at set? A
    local or parameter whose address was never taken is in no set, so it is independent of every pointer
    access.
  - **dereference against dereference** (INDIR leaves; the alias class is the PAS index at leaf+0x1c, assigned
    by `ComputePointsAtSets` before GlobalOptimize): `ItfPasInterf` 10b72990.
    1. Either index is 2: no interference. Either index is 1, or the two are equal: interference.
    2. Both indices are ≥ `_NextPasIndex` (these are entries of `_PASMap`, 16 bytes each: parent PAS, pointer
       value number, size, offset; made when `_PtrValueSensitivity` is on) **and have the same pointer value
       number**: interfere iff the byte ranges `[offset, offset+size)` overlap. This is "same base register,
       disjoint offsets are independent" (probes n1, n3).
    3. Otherwise each index is replaced by its parent and the two points-at sets are intersected
       (`FbvAnyCommon` on `_PointsAtSetBvs`). No common member: no interference (after stack packing, locals
       that share a stack slot are checked as well, `StkGetOverlayedSyms`). Two pointer parameters both
       point at "unknown memory", so they intersect: dependent (n2, m2), with no regard to `int*` against
       `short*` (m8).
    4. **One exception by type:** if the sets intersect but one class is dereferenced only as floating point
       (`ItfIndirPasIsFP` 10b7c970, table `_PasFPIndirTable`) and the other only as a pointer
       (`ItfIndirPasIsPtr`, `_PasPtrIndirTable`; both built by `BuildPasTypeTables` 10b7c9a0), they do
       **not** interfere. Probe `work/scratch/openq/alias.c`, where the same function serves GlobalOptimize:
       `float x = a[0]; *b = v; return x + a[0];` with `int **b` reads `a[0]` once, after the store (`mov [ecx],
       eax; .. fld [eax]; fadd [eax]`), and `int *x = a[0]; *b = v;` with `float *b` likewise (`fstp [eax]; mov
       eax,[ecx]; add eax,eax`); with `int *b` and a float `a`, or `int **b` and an int `a`, the value is
       kept and `a[0]` is reloaded after the store. **[C code + compile]**
  - Operands flagged volatile (leaf+0x15 & 8) always get the edge, except against an INDIR of PAS 3; a
    definition through an INDIR of PAS 2 gets no memory edges at all. What classes 2 and 3 stand for was not
    traced (2 behaves as "aliases nothing"). **[C code, I meaning]**
  - `ItfTotalInterf` 10b721a0 is the stronger test "B lies entirely inside A" (same symbol and range
    containment, or two INDIRs of the same value-numbered class); `DagChkMemrefDefs` uses it for a store to a
    named variable to stop the search at the store that fully overwrites it.
  - Not probed: arrays indexed by registers and `this`-relative members through two different registers
    (by the code they follow 2 and 3: same pointer value number → range test, otherwise set intersection).
- When exactly a dependency is breakable (`SchmdDepIsBreakable`'s cases for lea/shl and the `_Speed`
  differences), and how `SchmdBreakDep` chooses which uses to rewrite.
- The 3 selection misses: x87 regions where the first pick differs from the model (unit 2 busy or
  `SchmdBadForFPSch` at /O2).
- `SchmdBuildFPTrueDep` (the x87 stack-order chain) and `SchmdShiftOPParenDep` were not read; x87.md rule 15
  covers their effect.
- Flag 8 in FUNC+0xac (rule 1) was identified only by outcome.
