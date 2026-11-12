# c2.dll flow graph: blocks, arcs, order, dominators, loops, regions, tail merging

Target: `Bin/c2.dll`, cl 14.00.50727.762 (VS2005 SP1). Modules: fg.obj, fgopt.obj, graph.obj, loop.obj,
lg.obj, fgweight.obj, dag.obj, rgn.obj, plus listing support in list.obj. C declarations are in
`src/c2/il_fg.h`. They compile under cl with every size assert passing.

Each claim is tagged. **CONFIRMED** means I read it in the code (VA given) or a test compile showed it
(test named). **INFERRED** means it is a reading of code that I did not verify end to end.

Calling convention: c2 is built with LTCG, so most internal functions take arguments in arbitrary
registers. Below, `f(eax=..., edi=...)` gives the registers read from the disassembly. The Ghidra
decompile drops these (`in_EAX`, `unaff_EDI`).

Scratch evidence (tests, decompile extracts, helper scripts): `work/scratch/glass_fg_t/`,
`work/scratch/glass_fg_dc_<module>.c`, `work/scratch/glass_fg_tools/{xref.py,dc.py}`.

---

## 0. Rules you can use when matching code

These rules predict output. The sections after this one give the evidence for each.

1. **Block order is the reverse postorder (DFO) of one DFS, done early.** With global optimisation on
   (/Og, which /O2 and /O1 include), `dop2_InitialOptsAndSetup` 10c06890 calls `FgDFOReorderLoopRecog`
   10b26ca0. That pass relinks the block list into DFO and moves the exit block to the end. The DFS visits
   each block's successor list in list order. That list is built by prepending (rule 2), so **among a
   block's successors, the one whose label is lexically latest is visited first, and the lexically
   earliest one ends up placed right after the block.** Dead blocks are deleted. CONFIRMED (code §3;
   test t1, where a `goto`-reached block moved to right after the block that jumps to it).
2. **Successor list order** is (lexically latest target, ..., lexically earliest target). Arcs are made
   while walking target blocks in lexical order, and each new arc is prepended to `src->succs`.
   **Predecessor lists** are reversed after building, so they read (label references in reference order,
   ..., fall-through predecessor last). CONFIRMED: `FgAddAllArcs` 10b28910, and the `/d2FAf` dump in t1
   and t9 (`Out(4,3)`, `In(3,2)`).
3. **The listing can show the flow graph.** `cl /FAsc /d2FAf` prints a `; Block(n) In(..) Out(..)
   Instrs(k) Bytes(b)` line in front of every block of the final code. `-FAf` and `-FAF` are c2 options
   (table entries 10cedd5c/10cedd68, flag global 10d19460) that feed listing flag 0x20 (`MscProcessSwitches`
   10b9829x, then `ModSetModuleFlags` 10ba5781). `LiListFunction` 10b889e0 then rebuilds a minimal graph,
   merges trivial blocks, weights it, and calls `LiPrintBlock` 10b89560. CONFIRMED (t1f.asm, t9.asm).
4. **Common tails reached by two jumps** are merged only if the merged code is big enough. Under /O2
   (`_Speed` != 0) the tail must be **more than 20 bytes**. Under /O1 it must be more than 2 bytes. Size
   is the real encoded length (`decode()`), with an "already saved" credit carried on the jump tuple
   (+0x1c). If the whole block matches (the match reaches an unconditional transfer), it is merged at any
   size. This is `FgTryCommonTailMerge` 10b306c0 → `FgDoCommonTailMerge` 10b307e0. CONFIRMED in code. The
   tests agree: an 18 to 20 byte tail stayed duplicated at /O2 (t4 a2, t5 t20), 28 bytes was merged
   (t4 a3), and at /O1 a 20 byte tail was merged (t5 t20).
5. **A jump that meets code falling into the same label is tail-merged with no size limit**, through a
   dependence DAG, by `FgTryTailMerge` 10b2e1b0. The matched instructions that do not depend on each other
   come out in **reverse source order** in the merged copy (`v1=..; v2=..` becomes `v2; v1`). This is a
   tell: reversed independent stores at a join mean the source had them in both arms. CONFIRMED (t4 a1:
   18 bytes merged; t6 u17/u20/u21/u27: every tail merged and every order reversed).
6. **if/else that both assign the same destination becomes "assign then-value; jcc over else-value".**
   The arms must be single assignments with no interference between them. `FgCrossJumpRemove` 10b2e700
   moves the then-arm assignment above the branch, inverts the jcc to target the join, and deletes the
   `jmp`. CONFIRMED (t8 d2: `mov eax,a; jg L; mov eax,b; L: ret`).
7. **Small join blocks are duplicated into their jumps.** In the last pass, `FgCodeMotion` 10b312c0
   replaces `jmp L` with a copy of the code at L when the copy is at most 20 bytes (/O2) or 2 bytes (/O1)
   and ends in a jump or return. If the code at L can only be entered by jumps, it is moved instead of
   copied (the "late block mover" of the M&B notes). CONFIRMED in code. Test t2 shows `mov [x],1000;
   jmp _g` copied into every switch case.
8. **Loops are rotated.** A `while`/`for` gets a duplicated zero-trip test, a landing-pad block (where the
   alignment `npad` goes), and a bottom test. The work is done by `LgZtripTest1` 10b871a0 and
   `LgMakeLandingPad1` 10b87bd0. CONFIRMED (t9: `Block(2) ... npad 4`, then the body, then `jl` back).
9. **`jcc A; jmp B` with B a backward target is inverted** so that the back edge is the conditional jump.
   This is `FgSimpleBranchPredict` 10b2cae0, which compares DFO numbers. CONFIRMED in code.
10. **Switch layout:** case bodies come in source order and the `default` body comes after all of them,
    even when it is written first. CONFIRMED (t2, t3). The mechanism is open (§12).
11. `@tailmatch@12` 10b02f50 is **not** tail merging. It is getflags.obj's command-line suffix matcher
    (it sits next to `tailsubstr`, `substr`, `crack_cmd`). CONFIRMED (module and neighbours in the index).
12. **Head merging happens only after register allocation, so the allocator counts shared head code once
    per arm.** When both successors of a branch start with the same instructions (e.g. `if (a < X) { if (b <= Y)
    ...} else if (b > Y) ...`, whose two Y compares end up as one `fld/fcompp/fnstsw` before the first jcc),
    `FgTryHeadMerge` 10b2eb00 → `FgDoHeadMerge` hoists the copy into the common predecessor.
    `FgTryHeadMerge` and `FgTryCommonHeadMerge` 10b30f00 are called only from `FgDoLateFlowOptsOnce` 10b2ccf0;
    the pre-allocation loop `FgDoEarlyLateFlowOptsOnce` 10b2c6c0 does not call them (call lists, CONFIRMED). In
    Fallout 3 0x00a89680 the merge ran in `dop2_FinalFlowOpts` (5f), not in `dop2_PostCodegenFlowOpts` (5d),
    which made no head-merge attempts (trace, CONFIRMED for that function). Consequence for matching: a
    variable read in a duplicated head is credited by Spillcosts in **each** arm (regalloc.md §3.2), even though
    the final code reads it once. Writing the source with the read hoisted changes the branch tree, so it is not
    a fix; the priority has to be balanced somewhere else.
13. **A tail merge compares instructions after register allocation, operand by operand: call targets by
    symbol, registers by number.** Two arms that call two different functions with identical bodies (which
    the linker later folds with /OPT:ICF, so the exe shows one address) stop the backward walk at the call;
    a forwarder that makes both arms call one symbol lets the walk run back past it. Arms that differ only in
    a scratch register (`lea REG, [slot]; push REG`, the register from the rotation of regalloc.md §6) stop
    there too, so the rotation decides which arms share code. Which copy survives: the jump that comes
    first in the label's reference list keeps it (14.00: the lexically last arm; 13.10: the first one).
    `c2trace.py merges` lists every pair and why it stopped. §7.5. CONFIRMED (code on all four builds;
    probe tests/data/tailmerge.c on 14.00, 13.10.3077, 13.10.3052, 15.00).

---

## 1. Data structures

### 1.1 GRAPH (graph.obj), 0x24-byte header, FG instance 0x34

| off | type | meaning | evidence |
|---|---|---|---|
| 0x00 | GRNODE* | first node (lexical/layout order) | GrAddNode@4 10b5cbc0 |
| 0x04 | GRNODE* | last node (flow graph: exit block) | GrAddNode, FgChangeNextToDFOEx 10b281e0 |
| 0x08 | GRNODE* | postorder head (first node to finish) | GrInitDFOWalk@4 10b5cd30 |
| 0x0c | GRNODE* | DFO head (last to finish = entry) | GrInitDFOWalk, FgNumberBlocksDFO 10b276f0 |
| 0x10 | u32 | getmem heap id | FgBuildNullFlowGraph 10b26a41 |
| 0x14 | u32 | node size: **0x1a0** FG, 0x44 DAG | 10b26a4f, DagCreateGraph 10b1bc70 |
| 0x18 | u32 | edge size: **0x38** FG, 0x18 DAG | 10b26a56 |
| 0x1c | GRNODE* | node free list | GrAddNode |
| 0x20 | GREDGE* | edge free list | FgAddArc 10b28b40, GrDeleteEdge 10b5cce0 |

The FG header is `getmem(heap, 0x34)` (10b26a38, `lea edx,[esi+0x34]`). Its fields 0x24 to 0x30 are
not written in fg.obj. The DAG context is `getmem(7, 0x38)`. fgopt's DAG contexts are larger: they also
use +0x38 (last moved tuple) and +0x3c (flags). All CONFIRMED.

### 1.2 GRNODE and GREDGE (shared by every graph)

Node: `next 0x00, prev 0x04, preds 0x08, succs 0x0c, post_next 0x10, dfo_next 0x14, flags 0x18`.
Edge: `next_succ 0x00, next_pred 0x04, src 0x08, dst 0x0c`. CONFIRMED: `GrAddEdge2` 10b5cc70,
`GrDeleteEdge` 10b5cce0, `GrInitDFOWalk`.

* `GrAddNode` (edi=graph) appends a node at the lexical tail. `GrInsertNode` (eax=graph, ebx=before)
  inserts before `ebx`. With ebx=0 it appends. Recycled nodes are zeroed with `memset`. CONFIRMED.
* `GrAddEdge2` (esi=src, edi=dst, graph = `_DagGraph`) **appends** to both lists. `FgAddArc`
  **prepends** to both. This difference sets the order rules in §0. CONFIRMED.
* `GrDeleteEdge` (ecx=edge, esi=graph) unlinks the edge from both lists and pushes
  it on `graph->edge_free`. CONFIRMED.

### 1.3 ARC (flow-graph edge), 0x38 bytes

| off | meaning |
|---|---|
| 0x00 to 0x0c | GREDGE |
| 0x10 | zero on create. `FgChangeSource`/`FgChangeSink` 10b28cf0/10b28db0 copy it to the replacement arc (INFERRED: flags) |
| 0x18 | i64 count (profile count or derived weight). `FgWeightGraph` 10b32360 sums a block's preds into `BLOCK.count` |
| 0x20 | u8 count valid |
| 0x24 | arc number. -1 on create; `FgNumberArcs` 10b2a7e0 numbers 0..n-1, and `FgWeightFlowGraph` 10b322d0 indexes an array by it |

CONFIRMED: `FgAddArc`, `FgAddAllArcs`, `FgInsertAirlockBlock` 10b28ed0 (sets 0x18/0x1c and 0x20=1).

### 1.4 BLOCK (basic block), 0x1a0 bytes

| off | type | meaning | evidence |
|---|---|---|---|
| 0x00 | GRNODE | next/prev = layout order; preds/succs; DFO links | §1.2 |
| 0x18 | u32 flags | bit0 visited. 0x01000000: block contains opcode 0x2c1 (FgBuildBasicBlocks 10b27460). 0x10000000: FgFlowOpts will not merge the block (10b2ffa0). 0x18000000 cleared after loop finding (LgBuildLoopGraph 10b85ee0) | CONFIRMED |
| 0x1c | TUPLE* | this block's BLOCK tuple (kind 0x1a, opcode 0x2f3 when FgSplitBlock makes it) | FgSplitBlock 10b28610 |
| 0x20 | TUPLE* | next block's BLOCK tuple; the block is the range [0x1c, 0x20) | FgMoveBlock 10b282a3 |
| 0x24 to 0x158 | | not touched by this family (other passes' per-block dataflow) | - |
| 0x15c | BV* | dominator set, indexed by `number` | FgFindDominatorsGraph 10b27770 |
| 0x160 | BV* | post-dominator set | FgFindPostDominators2 10b27bb0 |
| 0x164 | BV* | dominated set (the inverse relation) | FgFindDominatedGraph 10b27a40 |
| 0x168 | BV* | reachable set | LgFindReachableGraph 10b86a70 |
| 0x16c | LOOP* | innermost Lg loop | LgMarkLoopBody 10b869d0 |
| 0x170 | LPLOOP* | innermost Lp loop | LpAddBlockToBlockListInDFO 10b88530 |
| 0x178 | int | number: DFO (FgNumberBlocksDFO) or lexical (FgNumberBlocksHelper 10b27660). -1 = unreachable or new | CONFIRMED |
| 0x17c | u8 | POGO count valid | LiPrintBlock 10b89560 (`Count(%I64d)`) |
| 0x17e | short | copied by FgSplitBlock, written by LgFixupTails; meaning unknown | CONFIRMED use |
| 0x180 | short | EH try depth while `_SdsuIsValid`: +1 at opcodes 0x2cf/0x2df, -1 at 0x2d1/0x2d3/0x2e8 (FgBuildBasicBlocks 10b274c4). -1 on new blocks | CONFIRMED (name INFERRED) |
| 0x190 | i64 | block count/weight | FgWeightGraph, FgMergeBlock 10b29510 |

The BLOCK tuple and LABEL tuples (kind 0x1b) carry their block at tuple+0x20 (`tuple.h` `block`). See
il_tuple. A label's symbol is at label+0x24, and that symbol's +0x33 points back to the label tuple. The
label's reference list is at label+0x28: nodes `{next, ?, ?, branch tuple @+0x0c, scratch @+0x10}`.

### 1.5 Loop structures

**LOOP** (lg.obj, `getmem(3,0x50)` in LgFindLoops 10b8621a/10b8685a; FUNC+0x0c = first top-level loop):
`next 0x00, child 0x04, parent 0x08, nrf_header 0x0c, head 0x14, tail 0x18, flags 0x4c`. `flags` bit0
means irreducible (made by the NRF path) or poisoned by EH, and bit2 means the loop contains a block with
`eh_depth` != 0. Bit0 is propagated to every parent (10b86980). CONFIRMED.

**LPLOOP** (loop.obj, `getmem(3,0x18)` in LpFindNaturalLoop 10b88370; FUNC+0x10): `outer 0x00, child
0x04, sibling 0x08, blocks 0x0c` (an LPBLK list sorted by DFO number, nodes `{block, next, prev}` of
size 0xc), and `0x14` = the eax argument (INFERRED: the latch). CONFIRMED except where marked.

### 1.6 REGION (rgn.obj, EH regions), 0x60 bytes

`RgnMakeRegion` 10bdcd90 (eax=parent): `begin 0x00, kind 0x04, end? 0x08, children 0x0c (RGNLIST
{rgn,next}, appended at the tail), parent 0x10, flags 0x14, info 0x20, try_kind 0x24`. The root has kind
2 (`RgnCreateAndPushRoot` 10bdcfa0) and is stored in `_regionGraph` and FUNC+0x28. Every region is also
on FUNC+0x2c. `RgnCreateRegionTree` 10bdd000 walks the tuples and switches on the EH opcodes: 0x2cf/0x2df
push a try, 0x2d0 pops it; 0x2d1/0x2d2 are a finally; 0x2d3/0x2d4 an except; 0x2d5 a filter; 0x2e0/0x2e1
others. `RgnIs1DominatedBy2` 10bdd420 is ancestor-or-self on `parent`. CONFIRMED (kind numbering partial).

### 1.7 DAG (dag.obj), node 0x44, edge 0x18

DAGNODE: GRNODE header, `tup 0x1c`, `npred 0x24`, `nsucc 0x26`, `nctl 0x28`, `lex 0x3a` (creation
order, `_Dag_LexicalOrder`), `latency 0x3c`, `func_unit 0x3e`, `flags 0x40`. CONFIRMED: `DagCreateNode`
10b1c650. DAGEDGE: GREDGE, `kind 0x10`, `0x14`, `flags 0x16`. CONFIRMED: `DagMakeDep` 10b1c740 and
`DagRemoveDep` 10b1c790. `DagInitializeGraph` 10b1bcc0 picks scheduler latency callbacks
(`SchmdDagLatency`) or null ones. fgopt.obj builds the DAG with null callbacks, so it can match tails out
of order. CONFIRMED.

---

## 2. Construction: `FgBuildFlowGraph@4` 10b26b40

```
FgFreeExistingGraph(func)          10b26d50  free old BLOCK tuples, reset free lists, FgBuildNullFlowGraph
_FgIgnorePragmas = 0
FgCoalesceLabels                   10b26e50  runs of adjacent labels -> one label (FgCoalesceLabelGroup)
FgDeleteUnusedLabels               10b27170  remove labels with no refs (and a lone `goto next` 0x2c0)
FgBuildBasicBlocks                 10b272c0  split the tuple list into blocks
FgAddAllArcs                       10b28910  make arcs
FgSortFlow                         10b28b90  re-order switch arcs (only for switch tuples with +0x3c bit0)
[POGO] PogoFgAnnotationPropagation
```
CONFIRMED. `FgBuildFlowGraphMinimal@4` 10b26bc0 does the same without label coalescing, with
`_FgIgnorePragmas = 1`, and without FgSortFlow. The listing and the inliner use it.

**FgBuildNullFlowGraph** 10b26a20 (eax=heap, ecx=func, ebx=first tuple, stack=end tuple): it clears
`_FgBlockCount`/`_FgArcCount`, allocates the 0x34 header into FUNC+0x08, and makes one block (entry) with
`number=-1, f17e=0, eh_depth=-1`. CONFIRMED.

**FgBuildBasicBlocks** 10b272c0 walks the tuples once. A new block starts (`FgSplitBlock`, which inserts
a BLOCK tuple 0x2f3 and a node after the current one) at:
* a LABEL (kind 0x1b), the function exit (0x19), or opcode 0x301;
* the tuple after a block-ending tuple. The previous tuple ends the block when it is a return (0x10), a
  branch (0x12), a switch (0x14), or 0x18 (except an EH-special 0x2c6), opcode 0x2e0, or pragma 0x2ff;
* EH opcodes 0x2cf to 0x2d5, 0x2d7, 0x2df, unless a LABEL precedes them (skipping 0x2c6);
* POGO count probes (0x2f1 with operand kind 0x10 or 5) when POGO is on.

Each LABEL gets `label+0x20 = current block`. CONFIRMED (10b27371 to 10b27530).

**FgAddAllArcs** 10b28910, for each block B in lexical order:
1. For every reference of B's leading label, prepend an arc `src_block → B`. References from tuples
   with flag bit 0x02 at +0x09 are skipped, and so are EH-special branches when `_FFgEHFlowIsSpecial` is
   set.
2. If the previous block falls through, prepend an arc `prev → B`. It does not fall through when its last
   real tuple is a switch, an exit (0x19), opcode 0x301, or an unconditional branch.
3. Reverse B's pred list.

The result: succ lists are in reverse lexical order of target, and pred lists are (label refs...,
fall-through last). CONFIRMED (10b28940 to 10b28a50; `/d2FAf` dumps).

"Unconditional branch" is tested the same way all over fg/fgopt: kind 0x12, tuple+0x30 == 0, opcode !=
0x2c6, and the opcode outside 0x316..0x476 except 0x381, 0x399, 0x408, 0x409. Opcode 0x2c0 is `goto`;
0x2c7 is the exit goto; 0x10 (0x2c0 under WVM) is what `TupMakeBranch` makes in FgBreakFallThrough.
Kind mask 0x140000 = {0x12, 0x14} = transfer kinds. CONFIRMED pattern; opcode names belong to il_tuple.

**FgSortFlow** 10b28b90: for a switch with +0x3c bit0, it deletes and re-adds the default arc (from
tuple+0x30) and then each case arc (list at tuple+0x34, the label at node+0x14) through the recursive
`FgCaseArcs` 10b28c70. The final succ list is (case_list[0], ..., case_list[n-1], default, ...).
CONFIRMED code; effect on layout: §12.

Editing primitives (all CONFIRMED by reading):
* `FgSplitBlock@12` 10b28610: inserts a new block after the block, starting at a given tuple. It copies
  `f17e`/`eh_depth` and, with POGO, calls PogoFgSplitBlock. It does not move arcs.
* `FgMoveBlock@12` 10b28270 (eax=block, ecx=new next, stack=func): relinks `block->next` and the tuple
  chain. If the old next was a fall-through successor, it calls `FgBreakFallThrough` 10b28310, which
  inserts a goto plus a new block and fixes the arcs (`block→new→oldnext`). That is where a reorder
  creates jumps. If a loop break is involved, it calls LgInvertBreak.
* `FgDeleteBlock@8` 10b287a0, `FgMergeBlock@12` 10b29510 (moves preds with FgChangeSink, then deletes),
  `FgDeleteTuples@8` 10b286e0.
* `FgInsertAirlockBlock@16` 10b28ed0: splits an edge with a new block (used for landing pads and sink
  pads), copying the edge count. `FgInsertSafeAirlockBlock2` 10b29340.
* `FgRedirectTransfer@12` 10b2a440, `FgChangeSource/Sink`, `FgFindArc@8` 10b28cd0 (src in eax, dst in
  ecx), `FgRemoveDuplicateArcs` 10b2a180.

---

## 3. DFS, DFO numbering and the block order

**GrInitDFOWalk@4** 10b5cd30 (esi=graph) is an iterative DFS from `graph->first`:
```
clear bit0 of every node; graph->dfo_first(+0x0c) = 0
n = first; n.visited = 1; n.post_next = 0 (parent); n.iter = n.succs
loop:
  e = n.iter; skip edges whose dst is visited
  if e: n.iter = e.next_succ; d = e.dst; d.visited = 1; d.parent(+0x10) = n; d.iter = d.succs; n = d
  else:  /* n finishes */
     p = n.parent
     if graph[+0x0c]==0: graph[+0x08] = n; n[+0x14] = 0
     else: graph[+0x0c][+0x10] = n; n[+0x14] = graph[+0x0c]
     graph[+0x0c] = n; n = p; if !n return
```
After the walk, +0x10 runs forward in postorder and +0x14 runs forward in **reverse postorder (DFO)**
starting at `graph+0x0c` (the entry). Unvisited nodes keep bit0 clear. CONFIRMED.

* `FgNumberBlocksDFO@4` 10b276f0: sets number=-1 on unvisited blocks, then numbers 0,1,2,... along
  `dfo_first`/+0x14. It sets `_FgBlockCount` and calls fatal(0x35) at 0x7fffffff. CONFIRMED.
* `FgNumberBlocksHelper@12` 10b27660 / `FgNoFatalNumberBlocks` 10b27610: number in lexical order.
  CONFIRMED.

**FgDFOReorderLoopRecog@8** 10b26ca0 is the one block reorder done at /O2:
```
FgThreadDFOAndFreeDeadCode   10b28470  GrInitDFOWalk; delete tuples and blocks not visited
FgChangeNextToDFOEx          10b281e0  relink lexical order = DFO, exit last
[POGO master] PogoFgRemoveDummyFlow
FgNumberBlocksHelper                    lexical numbers (now equal to DFO numbers)
FgFindDominatorsGraph
LgBuildLoopGraph (tail jmp)             loop tree, zero-trip tests, landing pads (§5)
```
`FgChangeNextToDFOEx` walks the postorder list from `graph+0x08` along +0x10 and **prepends** each block
(`graph->first = b; FgMoveBlock(b, old_first)`). The final list is therefore reverse postorder. Then, if
the old exit block is not last, it unlinks it and appends it with FgMoveBlock(last, exit). Every lost
fall-through gets a goto block from FgBreakFallThrough. Later passes (`FgDoLateFlowOpts`,
`FgCodeMotion`) remove `jmp next` and reshuffle locally. CONFIRMED in code. Callers:
`dop2_InitialOptsAndSetup`+0x3d8 (10c06c68), which runs after FgBuildFlowGraph and
FgEliminateTailRecursions, and `WpaInitialOptsAndSetup`+0x1cd (10c015dd). CONFIRMED.

Consequence (CONFIRMED, test t1):
```c
g(0); goto B;  A: g(1); return;  B: g(2); if (x) goto A; g(3);
```
compiles to `g0; g2; je L3; g1(tail); L3: g3(tail)`. A now comes right after B: B's successors are
(g3, A), the DFS goes to g3 first, so A finishes later and is placed earlier. Structured if/else keeps
source order for the same reason: the then-arm label is lexically before the else-arm label, so the
then-arm is placed next.

Other numberings are recomputed after edits by `LgBuildLoopGraph` (lexical, 10b86090) and by many
callers of `FgNumberBlocksHelper` (Gopt, inliner, POGO). The usual back-edge test across the family is
`pred.number >= block.number`. That is only correct because lexical order equals DFO after the reorder.

---

## 4. Dominators and post-dominators

* `FgFindDominatorsGraph@4` 10b27770 (stack=graph): entry `dom = {0}`. Every other block gets a fresh BV
  of `_FgBlockCount` bits, set to all ones. Then `FgComputeDominatorsGraph` 10b27870. CONFIRMED.
* `FgComputeDominatorsGraph` is the iterative dataflow, walking in **lexical** order (`graph->first`,
  `->next`) until nothing changes:
  `dom(b) = AND over preds p of dom(p), plus {b}`, for blocks with number != -1 and at least one pred.
  It has fast paths for one and two preds. It compares with the old BV and swaps buffers on change. Since
  lexical order equals DFO at this point, this is the standard RPO iteration. CONFIRMED.
* `FgCorrectDominatorsGraph@4` 10b27820: reuse the BVs if the block count did not change, else rebuild.
* `FgFindDominatedGraph@4` 10b27a40: inverts dom into `dominated` (0x164). CONFIRMED.
* `FgFindPostDominators2@4` 10b27bb0 and `FgCorrectPostDominators2` 10b27c80: the same over succs,
  seeded at the exit (`graph->last`), walking backwards along `prev`. CONFIRMED. Used by
  MscShrinkWrap.
* `FgFindForwardPostDominators@4` 10b27e50: post-dominators that ignore back edges. Only successors with
  a higher number count, and the first one is ORed in, not ANDed. At the end each block's own bit is
  cleared. Used by `LurUnroll`. CONFIRMED.
* `FgFreeDomInfoGraph@4` 10b28170 returns dom and pdom BVs to `_rgbvFreeList[heap]`. CONFIRMED.

---

## 5. Loops

### 5.1 Lg loop graph (the optimizer's loops): `LgBuildLoopGraph@8` 10b85e90

```
LgFlowGraphMayHaveLoops 10b86130: any pred p with p.number >= b.number ?
LgFindLoops 10b86210                 build LOOP tree, set BLOCK.loop
clear BLOCK.flags 0x18000000
LgFixupTails 10b86f50 (renumber if changed)
for each loop: LgRemoveBreakBlocks/1  10b86d20/10b86d60  (moves blocks with FgMoveBlock)
if edx(arg2): for each loop: LgZtripTest/1 10b87160/10b871a0   zero-trip test (loop rotation)
FgFlowOpts 10b2ff00                  local block merging and branch simplification
for each loop: LgFindExitBlocks/1, LgMakeLandingPad/1 (pre-header), LgMakeSinkPad/1 (exit pads)
if changed: renumber, FgFreeDomInfoGraph, FgFindDominatorsGraph
```
CONFIRMED (sequence read at 10b85e90 to 10b86120).

`LgFindLoops`, for each block H in lexical order:
* **Irreducible (NRF) part:** for every pred P with `P.number > H.number` that H does *not* dominate,
  run `LgB1ReachesB2Search` 10b86180, a reachability search inside [H.number, P.number]. If it succeeds,
  P becomes the tail of a loop with `flags |= 1`, `nrf_header = head = H`, and the body marked by
  `LgMarkNRFLoopBody` 10b86a10 (by number range). CONFIRMED.
* **Natural part:** if some pred P dominated by H has `P.number >= H.number` (a back edge), H is a
  header. When needed (several entering edges, or a loop sharing H), it inserts a new label at the top of
  H and splits H with FgSplitBlock. Then FgChangeSink moves the non-back-edge preds onto the new block
  and an arc is added. The LOOP gets `head = H` and `tail = highest-numbered back-edge source`. The body
  is found by `LgMarkLoopBody` 10b869d0, a reverse DFS from the tail over preds that stops at
  `head->prev`. The loop is linked under `H.loop` as a child, or into the top list. CONFIRMED, though the
  split condition is only partly read.
* The zero-trip machinery: `LgHasZtripTest` 10b878a0 (is the block's last tuple a conditional branch
  out of the loop), `LgUsefulZtripTest` 10b879b0, `LgDontDupLoopTest` 10b87f10 (refuses tests that
  contain call kind 0x13 with +0x30 == 0xde or opcode 0x2ce, plus POGO vetoes), and `LgZtripTest1`
  (duplicates the test above the loop). Test t9 shows the effect. CONFIRMED effect, INFERRED details.

### 5.2 Lp natural loops (loop.obj): `FgNaturalLoopRecog@4` 10b26ce0

This is used only by the warning paths (`WrnPrepareFlowGraph`, `WrnEmitWarnings`):
FgBuildFlowGraphMinimal, DFS, FgNumberBlocksDFO, dominators. Unless EH is present (FUNC+0xac 0x100000
or FUNC->+0x20 0x1000) it runs `LpCanonicalizeLoops` 10b88040, which gives each header exactly one
back edge by inserting merge blocks (GrInsertNode before the header) and redirecting the extra back edges
with FgRedirectTransfer/FgChangeSink, carrying the counts. If that changed anything it recomputes, then
runs `LpBuildLoopGraph` 10b87f90. That pass walks DFO, and for each back edge (`pred dominated by
block`) calls `LpFindNaturalLoop` 10b88370. Members go into a DFO-sorted list
(`LpAddBlockToBlockListInDFO`) and a work stack (`LpPushOntoBlockStack`, recycled through
`_BlockStackRecycleList`). CONFIRMED.

### 5.3 Loop weights (fgweight.obj)

`FgWeightFlowGraph@4` 10b322d0 numbers arcs and builds an arc array. `FgWeightGraph@12` 10b32360
allocates `_LoopEntryBlock`, `_LoopZeroTrip`, `_LoopBackArc` and `_LoopExit` and runs
`WGIdentifyLoops` 10b324c0 (an arc whose ends have different `BLOCK.loop` is a loop entry or exit),
`WGIdentifyBackArcs` 10b325d0 and `WGWeightFG` 10b32760. It then sets `BLOCK.count = sum(pred
ARC.count)`. CONFIRMED structure; the weighting formula is not read. Static branch probabilities are in
bp.obj (`BpComputeFlowgraphProbabilities` 10c73df0, the heuristics `BpLoopBranchHeuristic`...
`BpStoreHeuristic`), which is not mapped here.

---

## 6. Where the flow graph is built and used in the pipeline

| driver (main.obj) | flow-graph calls | evidence |
|---|---|---|
| dop2_InitialOptsAndSetup 10c06890 | RgnCreateRegionTree (EH), FgSimpleFoldCmps, **FgBuildFlowGraph**, FgEliminateTailRecursions, **FgDFOReorderLoopRecog** | 10c06a21, 10c06bc1, 10c06bd7, 10c06c48, 10c06c68 |
| dop2_PreLowerGlobalOpts | FgDoEarlyLateFlowOptsOnce | 10c06f61 |
| dop2_Codegen | FgReturnValueMerge | 10c07123 |
| dop2_PostLowerPreRegAllocGlobalOpts | FgBuildFlowGraph, FgNumberBlocksHelper, FgFindDominatorsGraph, LgBuildLoopGraph | 10c072eb to 10c0732a |
| dop2_GlobalRegAlloc | FgEarlyTailDup | 10c0738c |
| dop2_PostCodegenFlowOpts 10c07630 | FgSimpleBranchPredict + FgFreeExistingGraph (if Globopt); **FgDoLateFlowOpts(mode 0)** unless FUNC+0xac & 0x2000 | 10c0765e, 10c07679 (`xor eax,eax`) |
| dop2_CodegenFinish 10c07690 | FgRemoveAllDeadCodeAfterBranchOrReturn, FgBuildFlowGraph, LateGlobalOptimize, FgFreeExistingGraph | read |
| dop2_FinalFlowOpts 10c07780 | **FgDoLateFlowOpts(mode 2)**, [POGO PogoOptCodeMotion], **FgCodeMotion**, FgCoalesceLabels, FgRemoveAllDeadCodeAfterBranchOrReturn | 10c077aa (`mov eax,2`), 10c07807 |

`FgDoLateFlowOpts` is also called from EhGenFunclet 10c6bd91 and WvmCodegen 10c75457 (mode not read).
The late passes work on the **tuple list**, not the graph (§7, §8). They use labels and reference lists,
but the block numbers they read (FgSimpleBranchPredict) come from the last graph. CONFIRMED.

---

## 7. Late flow optimisation: `FgDoLateFlowOpts@8` 10b2c950 (eax=mode, ecx=func)

It runs `FgDoLateFlowOptsOnce@20` 10b2ccf0 until no change, at most 17 times (`iVar6 < 0x11`). With
mode != 0 it also initialises the scheduler model (SchmdInitialize). Afterwards it turns exit gotos that
were made unconditional (`_FChangeExitGotos`, opcode 0x2c0 with flag 8) back into plain branches.
CONFIRMED.

One pass of `FgDoLateFlowOptsOnce` walks the tuples once and dispatches on the kind:
* **call (0x0f):** `PpCallRetToJmp` (call+ret becomes a tail jump) when mode == 0 and not WVM-managed.
* **return (0x10), mode 2 or WVM:** `FgRemoveDeadCodeAfterBranchOrReturn`.
* **branch (0x12):** dead code after unconditional transfers, then
  `FgRemoveJumpToNextInstr` 10b2d400, then `FgAdjustJumpToExitGoto` 10b2d530. With Globopt:
  * unconditional, mode 2: **`FgTryTailMerge`** (§7.1) and, failing that, **`FgCrossJumpRemove`** (§7.3);
  * conditional: `FgRemoveRedundantJcc` 10b2d890, `FgFixInvertedBranches` 10b2dff0 (`jcc L; jmp M; L:`
    becomes `j!cc M`), `FgFixJccToJcc` 10b2d6c0 (threads jcc to jcc). In mode 2 also `FgTryHeadMerge`
    10b2eb00, `FgTryLoopInversion` 10b2eff0 (only when `_Speed == 0`, i.e. /O1), and `FgCompareMerge`
    10b2f700 for opcodes 0x2f/0x30/0x33.
* **switch (0x14):** dead code after it.
* **label (0x1b):** remove if unused; coalesce with an adjacent label; forward a label whose next
  instruction is `jmp M` (FgTransferLabelRefs). In mode 2 also **`FgTryCommonTailMerge`** (§7.2) and then
  `FgTryCommonHeadMerge` 10b30f00. In mode 1, `FgTryEpilogMerge` 10b321e0.

CONFIRMED (10b2cd82 dispatch).

### 7.1 FgTryTailMerge@16 10b2e1b0: a jump against fall-through, DAG matching, no size limit

The input is `jmp L` (param_3) and two DAG contexts. It does nothing under POGO instrumentation
(`_PogoFlag == 1`). It finds the start of the jump's block and of the code that falls into L
(`FgDagFindStartTuple` 10b31b30). If neither range is empty, it builds a DAG for each (DagBuildGraph
with null latencies), removes label nodes (`FgDagRemoveUserLabelNodes` 10b31d20), and seeds the
"tail-ready" lists with `FgDagUseTailNode(ctx, end node)` 10b31f00. A node becomes ready when its last
dependent is consumed. Ready nodes are appended to `ctx+0x08/0x0c` through node+0x10/+0x14.

Loop: for each ready node F on the fall-through side (in ready-list order), look for a ready node J on the
jump side with `FgMdInstrsMatch` 10b29e90. On a match, move F to just before the previously moved
instruction (the first goes just before L, `ctx+0x38` remembers the last), move J the same way before
the jump, and consume both nodes. Labels on either side are moved and flagged (+0x3c bit0). Repeat until
nothing changes. At the end, if anything matched: POGO fix-up, `FgRelocateLabelsForMerge` if labels
moved, `FgMergeInterferenceInfo` 10b29600, retarget the `jmp` to a label in front of the moved
fall-through copy (`TupFindOrMakeAnAdjacentLabel`), and delete the jump-side copy (`TupRemoveRange`).
CONFIRMED in code.

There is **no byte threshold**. Because the initial ready list is in creation (lexical) order and each
match is inserted *before* the previous one, independent instructions end up in **reverse order**.
CONFIRMED by tests t6 (`v1,v2` becomes `v2,v1`; `b1,b2,b3` becomes `b3,b2,b1`) and t4 a1 (a call and a
store merged in dependence order).

### 7.2 FgTryCommonTailMerge@8 10b306c0 and FgDoCommonTailMerge@20 10b307e0: two jumps, linear match, size limit

`FgTryCommonTailMerge(label)`: it marks each reference of the label that is an unconditional jump with
pass 0 (+0x10 of the ref node) and clears the jump's accumulated credit (`jump+0x1c` ushort). Other refs
get -1. Then, for each jump A in reference order and each later jump B not yet tried in this round, it
calls `FgDoCommonTailMerge(B, A)`. CONFIRMED.

`FgDoCommonTailMerge(J1, J2)`:
1. Both jumps must be ordinary (flag 8 clear and opcode 1..0x272). Flag 8 means "stack ref" under one
   personality (`_FCantMergeStackRef`).
2. Walk back from both jumps together (tuple+0x10), skipping labels, and compare with
   `FgMdInstrsMatch`. It stops at the other jump, at a mismatch, or (after at least one match) at an
   unconditional branch, return or switch on J1's side. In that last case it records the point as "whole
   block" (`local_5c`). It counts matches, with 0x2bd pseudo-instructions not counted. It also stops at
   WVM-managed opcodes 0xd/0xe. If J1's walk passed J2's target label, the roles swap (`local_64`).
3. No match: stop. Under `_Speed` with exactly one match, if the instruction before the J1 copy is a
   conditional branch: stop.
4. Size check. It is skipped when `_Speed == 0` (/O1, merge always) or when `FgTailMergeWithoutCheck`
   10b30d90 accepts. That function accepts when the instruction before the copy is an unconditional
   transfer or a return, or a conditional branch whose target chain reaches the end-of-function marker
   0x2f9 within 7 jumps.
   Otherwise: `size` = the sum of `decode()` lengths of the matched instructions on J1's side (opcode
   0x1e3 counts 8), stopping once it reaches `limit = _Speed ? 20 : 2`. Let
   `credit = max(J1+0x1c, J2+0x1c)`. **If `size + credit <= limit`, merge only at the recorded
   whole-block point, or not at all.** After merging, `J2+0x1c = min(size + credit + J2.credit, limit)`.
   So once a tail has been shared, later jumps into it merge more easily.
5. Merge: relocate labels, merge interference info, put a label in front of the kept copy (making one
   with `newlabel` if needed), retarget the other jump to it, `TupRemoveRange` the duplicate. An exit
   goto 0x2c7 becomes 0x2c0 (`_FChangeExitGotos`). Then it removes dead code after the new jump.

CONFIRMED in code (10b30a3f and 10b30b28 read `_Speed`). Test status: at /O2 tails of 18 to 20 bytes
were not merged (t4 a2, t5 t20) and 28 to 30 bytes were (t4 a3/a4). At /O1 a 20-byte tail was merged.
t5 `t17` merged 16 bytes at /O2: the count includes the jump, see layout.md §6.

### 7.3 FgCrossJumpRemove@4 10b2e700: if/else assignments

The input is `jmp L2` (param_3). It matches
```
   jcc L1            (conditional; its target label is the one right after the jmp)
   I1: d = s1        (kind 0x0d, not 0x298, not leaf class 5/0xd/0xe/0xf at tuple+0x0a>>12)
   jmp L2
L1:
   I2: d = s2        (the last instruction before L2; same final leaf: TupAreSameFinalLeaf)
L2:
```
It requires no interference (`ItfPartialInterf`) between the source operands of one assignment and the
destination of the other, and no strongly biased branch under POGO. The rewrite removes the `jmp`, moves
I1 above the `jcc`, inverts the jcc (`TupInvertCBranch`) and retargets it to L2. A second variant moves
both assignments. The result is `d = s1; j!cc L2; d = s2; L2:`. CONFIRMED in code and by test t8 d2.

### 7.4 Other merges

* `FgTryHeadMerge` 10b2eb00 and `FgTryCommonHeadMerge` 10b30f00 → `FgDoCommonHeadMerge` 10b31050 hoist
  identical leading instructions of two targets above the branch. They use a linear forward match and
  have no size check. CONFIRMED in code.
* `FgTryEpilogMerge` 10b321e0 (mode 1) runs the two tail-merge functions on epilogue code.
* `FgEarlyTailDup` 10b2bad0 (before register allocation, /O2 only) duplicates small tails, with a
  size/predecessor ratio check (`(_Speed+1)*4 <= ...`). INFERRED details.
* `FgReturnValueMerge` 10b2be20 (dop2_Codegen) and tail-recursion-to-loop (`FgEliminateTailRecursions`
  10b2aed0 → `FgConvertTailRecursionToLoop` 10b2b270, at setup) are not mapped in detail.

### 7.5 The instruction compare, survivors, the 13.10 functions and `c2trace.py merges`

**The compare, `FgMdInstrsMatch`** (14.00 10b29e90, eax = t1, ebx = t2; 13.10.3077 107b6bf8 and 3052 1078d0b8,
eax = t1, [esp+4] = t2, `ret 4`; 15.00 510765b0, eax/ebx as 14.00). Every tail and head merge goes through it
(FgDoCommonTailMerge's walk, FgTryTailMerge's DAG match, FgDoCommonHeadMerge, FgDoHeadMerge). It requires the
same opcode (+4) and kind (+8), then by kind (14.00 jump table 10b2a03c, 13.10.3077 107b6bd0, read in both):
instruction 0x0d and return 0x10: type bits (+0xa & 0xfff) and both operand lists; call 0x0f: both operand lists
and the call word +0x30; branch 0x12: the condition nibble and the same target label; switch 0x14: never.
Then, with `_FCantMergeStackRef` set, an instruction that references esp (`TupHasStackSymRef`) is refused. The
operand lists go through `TupLeafListsAreEquivalent` → `TupAreSameFinalLeaf` (14.00 10bf6fe0, 13.10.3077
1078755c; table 10787740): same leaf kind, compatible types, then **REG (1): the physical register number
(+0x1c)**; SYM / DATAADDR (2, 3): `SymCmpSym`; **CODEADDR (4): the symbol pointer (+0x18), by identity**;
EA / INDIR (5, 6): symbol, displacement, base, index and scale (14.00; not re-read in 13.10); ICONST (7): the
value. **[C]** in 14.00 and 13.10.3077 (kinds 1 and 4 read in both); 13.10.3052 is the 3077 code (`sig` match,
identical masked bytes); 15.00 by trace.

Consequences, each probed in `tests/data/tailmerge.c` (three arms jumping to one join that is too long for
FgCodeMotion to copy back; /O2; same result on 14.00, 13.10.3077, 13.10.3052 and 15.00 unless stated) **[C]**:

| function | arms | result | why |
|---|---|---|---|
| c1 | `w_k = 11; s1 = 7; f(&gv); b1 = 21;` | 2 of 2 pairs merged (15.00: none) | the walk passes the call; 9 + 5 + 5 + 3 + 7 > 20 (layout.md §6). 15.00 stores the short constant through a scratch register (`mov ax, 7`), so its arms stop at a register |
| c2 | arm 2 calls `f2`, arms 1 and 3 `f1` (twins: identical bodies elsewhere) | arms 1 and 3 merged; arm 2 stops at the call (`call target _f2 / _f1`), 15 <= 20 | CODEADDR leaves compare by symbol. The linker's /OPT:ICF folds f1 and f2 later, so the exe shows two arms calling one address that the compiler did not merge |
| c3 | c2 with arm 2 calling `f2fwd`, an inline forwarder to `f1` | as c1 | after inlining every arm calls `_f1`: one symbol. This is the M&B fix (three cases of Operation::execute) |
| r1 | `f(&a)` with a local `a`: `lea REG, a; push REG`, one scratch pick per arm | no pair merged; every pair stops at the push (`register ecx / edx` ...) | the rotation (regalloc.md §6) gives the three arms eax, ecx, edx |
| r2 | two more scratch picks before the `lea` in every arm | 2 of 2 merged | three picks per arm: each lea gets the same register |

So which arms share a tail depends on everything that moves the scratch rotation before them (an extra or
missing pick anywhere earlier in the function shifts the register of every later arm by one), and on the call
targets being one symbol. For matching: a duplicated tail in yours that is shared in the original (or the
reverse) is first a register question when the tails differ in one `lea/push` register, and a symbol
question when they differ at a call whose target the exe shows as one address. The distance from the jump
back to the first difference also decides the size rule (layout.md §6): the matched part alone must exceed
20 bytes (with the jump's 5).

**Which copy survives.** `FgTryCommonTailMerge` takes the label's references in list order; for each jump A
it pairs every later reference B and calls `FgDoCommonTailMerge(B, A)` (§7.2); unless the roles swap
(J1's walk passed J2's label), A (J2) keeps the code and B's copy is deleted, B being retargeted into A's
copy. In tailmerge.c c1 the reference list starts with the lexically last arm on 14.00 (the last arm keeps
the shared code, the earlier arms jump forward into it) and with the first arm on 13.10.3077 and 3052 (the
first arm keeps it, the later arms jump back). **[C: trace (`merges` J1/J2 and the listing) on both]** M&B's
own reading on 13.10, "the earlier jump keeps its copy" (widget_mgr::set_size), agrees with the 13.10 result;
the reference order for other shapes (a label referenced from a jump table, jumps made by different passes)
was not probed. **[I beyond these shapes]**

**The 13.10 functions.** No PDB (port-1310.md §3); the names come from the 6030 PDB by code matching. The
merge functions, read in 3077 and placed in 3052 by the index (`sig`/`calls`) and, for the probes, by the
unique masked byte context **[C]**:

| function | 3077 | 3052 | arguments (13.10) | 14.00 |
|---|---|---|---|---|
| `@FgDoLateFlowOpts@8` | 107b211d | 107786ed | eax = mode, ecx = FUNC | same |
| `@FgDoLateFlowOptsOnce@8` | 10775ec7 | 10778724 | | `@20` |
| `@FgTryCommonTailMerge@4` | 107b62d3 | 1078ce34 | [esp+4] = label | `@8`, [esp+4] = label |
| `@FgDoCommonTailMerge@16` | 107b89a9 | 107ac80c | [esp+4] = J1 (B), [esp+8] = J2 (A), [esp+0xc] = pass, [esp+0x10] = &out; returns eax != 0 when merged; `ret 0x10` | `@20`: ecx = J1, edx = J2, [esp+4] = pass, [esp+8] = &out |
| walk end (probe) | 107b8aac | 107ac90f | [esp+0x18] = matches, esi / ebx = the J1 / J2 tuples where the walk stopped, [esp+0x24] / [esp+0x28] = whole-block point | 10b30a1f: [esp+0x24], edi / esi, [esp+0x30] / [esp+0x34] |
| size rule (probe) | 1079f7c5 | 107afab8 | ebp = size + credit, edi = _Speed (limit 20, or 2) | 10b30b6a: ebp, ebx |
| `@FgMdInstrsMatch@8` | 107b6bf8 | 1078d0b8 | eax = t1, [esp+4] = t2 | eax, ebx |
| `@FgTryTailMerge@4` | 107b6ddd | 1079aa64 | [esp+4] = the jmp; builds both DAGs itself | `@16`: [esp+4] = jmp, plus the two DAG contexts |
| `@FgCrossJumpRemove@4` | 107b719a | 1079adde | [esp+4] = jmp | same |
| `@FgTryCommonHeadMerge@4` | 107b6344 | 1078cdb5 | edx = label | same |
| `@FgDoCommonHeadMerge@8` | 1078ea60 | 107ac958 | [esp+4], [esp+8] = the two jumps | same |
| `@FgTryHeadMerge@4` | 107b6ae3 | 1078ceb5 | [esp+4] = the jcc | `@16` |

`FgDoCommonTailMerge@16` fires in every 13.10 probe here (`work/scratch/layout/tm.c`, tailmerge.c), so a hook
that "never fired" was on another address or another build; an entry hook that reads ecx/edx (the 14.00
convention) sees unrelated values. The walk and the size rule are the 14.00 code (same tests, same order: the
whole-block point, `FgTailMergeWithoutCheck` 1079f6ec, `decode` with one opcode (0x1c4) special-cased, the
larger of the two credits at tuple +0x16). FgTryTailMerge is `@4` in 13.10: the DAG contexts that 14.00 passes in are built
inside (`FgDagInitializeGraph`, `DagBuildGraph`). 15.00 (PDB names): `FgDoCommonTailMerge(ecx = A, edx = B,
[pass, &out])`, walk end 5107d4bd ([ebp-0x5c] matches, esi = B side, ebx = A side), size 5107d609 (ecx, edi).

**`c2trace.py merges SRC --func F -- FLAGS`** (also `il --merges`; tools/c2merges.py) hooks the functions above
plus the two probes and prints, per `FgDoLateFlowOptsOnce` iteration of the mode-2 run, every tail pair with
its two jumps (tuple, source line offset, listing offset or `gone`), the number of matched instructions, the
verdict (`merged`, or `stop:` call target / register / operand / instruction / same text / other jump /
whole block) with the two instructions where it stopped, and the size rule's total. Fall-through tails
(FgTryTailMerge), cross jumps and head merges are listed when they merged (all with `--pairs`); `--off` keeps
what involves given listing offsets (one case of a big switch). Example (13.10.3077, c2):

```text
tail Lc83160: 2 pair(s), 1 merged
  J1 c82d18 l2 @0005e  J2 c828a0 l1 @00030: 2 matched: stop: call target _f2 / _f1; size 15 <= 20
      J1 side c82b88 l1 @0004f: call pas5:6000 regset:0 cc:8000 <- _f2:1004 pas5:6000 esp:1004
      J2 side c82710 l1 @00021: call pas5:6000 regset:0 cc:8000 <- _f1:1004 pas5:6000 esp:1004
  J1 c83190 l2 @0006f  J2 c828a0 l1 @00030: 5 matched: merged; size 22 > 20
```

Cost: a 300-case switch (work/scratch/tailmerge/big.c) took 58 s under wine with an 8 MB log. The verdict
for a call is read at the compare (the name at the CODEADDR symbol +4), not from the dump, so it holds with an
older c2trace.dll too.

---

## 8. Final code motion: `FgCodeMotion@4` 10b312c0 (after FgDoLateFlowOpts mode 2)

It does nothing under POGO instrumentation. Two passes over the tuples. CONFIRMED in code.

**Pass 1, block moving.** For `jmp L` (unconditional, with a real label):
`FgCanMoveCode` 10b319b0 succeeds when the code at L can only be entered by jumps (`FgSafeLocation`
10b31aa0: the instruction before L does not fall through), L is not the jump itself, and scanning forward
from L reaches an instruction that does not fall through before reaching the jump (or the 0x301 marker).
Then `TupMoveRangeBefore` moves L..end to where the jump was, the jump is removed, and the labels
coalesce. For `jcc A; jmp B`: if B's code can be moved it is pulled in. Otherwise, if A's can,
`FgInvertAndSwapBranches` 10b2cca0 makes it `j!cc B; jmp A` and A is pulled in. CONFIRMED in code.

**Pass 2, tail copy.** For `jmp L` that is not a jump to the next instruction: scan from L to the first
return, branch, switch or exit. It gives up on EH labels ('S'/'V' symbol kinds). Then:
* if the region is preceded by an unconditional jump and has no inner label, *move* it into place
  (`TupInsertListAfter`);
* else, if `csize` of the region <= `limit` (20 at /O2, 2 at /O1) and it has no POGO probe or entry
  pragma, *copy* it (`TupCopy`) over the jump.

CONFIRMED in code; test t2 shows the copy (13 bytes into every case).

The block mover is why the M&B notes saw a "late block mover that needs an unconditional skip". Code
reached only through a `jmp`, and ending in a `jmp`/`ret`, is pulled up to the jump site.

---

## 9. Branch layout heuristic: `FgSimpleBranchPredict@4` 10b2cae0

Runs in dop2_PostCodegenFlowOpts, before FgDoLateFlowOpts(0), while the graph from the post-lower
rebuild still exists. For each block ending in `jmp B` that is preceded by `jcc A` (both plain): if
`B.number <= blk.number` (jmp goes backward) and (`A.number > blk.number` or `A.number < B.number`), it
inverts the jcc and swaps the targets. The backward edge becomes the conditional jump and the forward
exit the unconditional one. Then late opts usually remove the jmp through layout. It also handles a jcc
followed by a block that holds only a jmp. CONFIRMED in code.

---

## 10. Smaller fg.obj pieces (CONFIRMED by reading)

* Labels: `FgCantCoalesceLabel` 10b26f90 refuses when the label symbol is address-taken
  (+0x43 bit0, with SDSU), when it has a POGO probe, or when its kind (sym+0x31) is 0/5/6/7/0xc/'Z'
  and `_KeepLoopLabels` is set. `FgCanRemoveLabel` 10b271e0 says yes when there are no refs and it is
  not address-taken. `FgTransferLabelRefsNoRemove` 10b28500 retargets every reference (branch or jump
  table).
* Scope gotos (C++ destructors on `goto`): `FgFixScopeGotoFlow` 10b2a970, `FgUnFixScopeGotoFlow`
  10b2ac10, `FgGetNextScopeExit` 10b2a8c0. Not mapped.
* `FgLowerSwitches` 10b2a0e0, `FgSetBranchPassCount` 10b2a250 (POGO), `FgIsInvBranch` 10b29be0
  (`_TupCCInverse` table), `FgOnePathToLabel` 10b29c50, `FgLabelHasFallThrough` 10b29cd0.
* `FgMergeInterferenceInfo@20` 10b29600 merges per-tuple interference sets (`_PasReferencedByIndirOrSymset`)
  when two copies of code collapse into one. See il_sym for symsets.

---

## 11. Experiments (all compiled with `uv run tools/msvc.py /nologo /c /O2 /FAsc`)

| test | source | result | supports |
|---|---|---|---|
| t1 | `g(0); goto B; A: g(1); return; B: g(2); if(x) goto A; g(3);` | order g0, g2, je, g1, g3. `/d2FAf`: `Block(2) Out(4,3)` | rule 1, rule 2 |
| t2 | 5 cases + default last, join `g(1000)` | cases in source order, default last, join copied into each case | rules 7, 10 |
| t3 | `default:` written first | default body placed after all cases | rule 10 |
| t4 | if/else-if arms with a common tail | 2 arms: merged (fall-through, no limit). 3 arms: 19-byte tail not merged, 29 bytes merged | rules 4, 5 |
| t5 | 3 arms, tails of 20/21/17/14/7 bytes at /O2 and /O1 | /O2: 20 not merged. /O1: 20 merged. 17 at /O2 merged (open) | rule 4 |
| t6 | 2 arms + returning else (puts the join after arm 2) | every tail merged; independent stores reversed | rule 5 |
| t7 | `if(c) v=K1; else v=K2;` | branchless `neg/sbb/and/add` (globopt, before fg) | - |
| t8 | `if(c>5) r=a; else r=b; return r;` | `mov eax,a; jg L; mov eax,b; L: ret` | rule 6 |
| t9 | `while`/`for` with `continue`, `/d2FAf` | zero-trip test, landing pad with `npad`, bottom test | rule 8 |

---

## 12. Open questions

* Switch order: by my reading of FgSortFlow and FgAddAllArcs, the DFS should put `default` first, but it
  comes last (t3). The bounds-check branch made by switch lowering may come first, or the case list may
  be ordered differently. Settle it by dumping `/d2FAf` before final opts, or by reading `FgLowerSwitches`
  10b2a0e0 and where the switch tuple's +0x30/+0x34/+0x3c are built (il_tuple).
* Why t5 `t17` (16 bytes) was merged at /O2: trace the credit at `jump+0x1c` and the order of pairs in
  FgTryCommonTailMerge.
* The exact condition in LgFindLoops for splitting a header (param_4 path, 10b86487), and the
  LgZtripTest1 conditions.
* BLOCK 0x24 to 0x158, 0x174, 0x17e, 0x182 to 0x18f, 0x198 to 0x19f; ARC 0x10/0x14/0x28 to 0x34; FGRAPH
  0x24 to 0x30. These belong to globopt/regalloc/POGO users outside this family.
* `FgDoLateFlowOpts` mode used by EhGenFunclet and WvmCodegen (likely 1, because of FgTryEpilogMerge).

Settled since (docs/c2/layout.md):
* Switch order: SETTLED, layout.md §3.1. A table switch's `default` is the target of c1xx's own bounds-check
  branch, so the ordinary DFS rule puts it after the cases; a compare-chain switch (FgSortFlow) gets
  `default` first, then cases by descending value.
* t5 `t17`: SETTLED, layout.md §6. Not credit: the size count includes the jump (5 bytes) and stops at 20, and a
  merge needs > 20 (10 + 6 + 5 = 21). 7/7 blind probes held.
* LgZtripTest1 conditions and the LgFindLoops header split: layout.md §5 (conditions read, no size limit, probed)
  and §5.1 (split read, not probed).
* Loop exits moved after the loop (LgRemoveBreakBlocks1): layout.md §4, a rule not listed above.

Cross-references: tuple kinds, opcodes, `tuple+0x09` flags, branch operand at +0x28, cond at +0x30,
and switch lists at +0x30/+0x34 are in il_tuple. Label symbols (+0x24 → sym, sym+0x31 kind, +0x33 label
tuple, +0x3b ref count, +0x43 flags) are in il_sym. The driver order (§6) is in il_pipeline. The BV
layout is in `src/c2/bitvect.h`.
