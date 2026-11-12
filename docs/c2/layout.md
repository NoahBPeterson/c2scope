# Block layout: which C++ shapes come out in which order

Scope: c2.dll 14.00.50727.762, the game flags (`/O2 /TP /MT /GS /EHsc /Gy`). This page settles the layout
questions left open in il_fg.md §12 and il.md §5.2, and adds the placement rules for common game shapes.
It builds on il_fg.md (flow graph, §7 tail merging, §8 code motion) and il.md §5.2 (rule 1, the one global
reorder). **[C]** = shown in code and confirmed by a probe; **[R]** = read in code, not probed; **[I]** =
inferred from probes.

Probes: work/scratch/layout/ (`pd.cpp`, `pz.cpp`, `ps.cpp` for shapes, `tm.c` for tail merging). Helper:

    uv run python work/scratch/layout/order.py work/scratch/layout/pd.cpp '?d8@' /O2

It prints the order of `g(N)` marker calls (N >= 10) in c1xx's IL (at `FgDFOReorderLoopRecog` entry), after the
global reorder (at `SymGarbageCollection` entry, same phase 2), and in the final listing (with the jumps
between them). `|` separates blocks.

## 1. The model in one paragraph

Layout is decided in three steps. (1) c1xx emits the blocks in source order, except where noted in §3.
(2) Phase 2 reorders once: reverse postorder of a DFS (il.md §5.2 rule 1), then loop recognition, which moves
loop exits out of loop bodies (§4) and rotates loops (§5). (3) After register allocation, only local changes:
small join blocks and epilogues are copied into the jumps that reach them (FgCodeMotion, ≤ 20 bytes), tails are
merged (§6), and FgSimpleBranchPredict turns backward jumps into the conditional ones. So to predict the order
of blocks, apply the DFS rule to c1xx's emission order, then §4 and §5. To predict which bytes repeat, apply §6
and il_fg.md §8.

## 2. The DFS rule, stated so it can be applied by hand

`FgAddAllArcs` 10b28910 walks the blocks in lexical order and, for each label, adds an arc from every block
that branches to it (plus the fall-through arc from the block before). Each arc is **prepended** to its
source's successor list. So a block's successors end up in **reverse lexical order of the targets**, and the
DFS visits the lexically latest target first. In reverse postorder this gives, for a block B with successors
A (lexically earlier) and C (later): **B, then everything reachable from A that C did not reach first, then
C's region**. A join reached by both arms belongs to the region visited first (C's), and topological order
puts it after both arms. **[C]**

Consequence: for every shape in §3 with no loop and no `switch`, the order is the source order.

## 3. Shapes (probe, prediction, result)

All at /O2 with the game flags. "RPO" is the order after the phase-2 reorder.

| probe | source shape | predicted RPO | actual RPO | final layout |
|---|---|---|---|---|
| d1 | `if (x) A;` | source | source | `je` over A |
| d2 | `if (x) A; else B; J` | source | source | J (small) copied into both arms |
| d3 | `if (x) { A; return; } B` | source | source | return arm inline, then B |
| d6 | `if (a && b) A; else B` | source | source | two `je` to B |
| d7 | `if (a \|\| b) A` | source | source | `jne` to A, `je` past it |
| d15 | `if / else if / else` | source | source | join copied into each arm |
| d16 | nested `if` with an early `return` | source | source | |
| d5 | `while` with `break` and `continue` (no statements in the `break` arm) | source | source | test rotated (§5) |
| d8 | `for` with `if (..) { A; return i; }` in the body | source | **A moved after the loop** | A after the loop's bottom test, then the exit code reached by a `jmp` |
| d17 | `while` with `if (..) { A; break; }` | source | A moved after the loop | A placed after the function's last block, the exit copied into it |
| d19 | the same in an inner loop of a nested loop | (after d17) A after the inner loop | as predicted | |
| d20 | two `return` arms in one loop | (after d8) each arm after the loop | both moved, **in source order**, before the exit block | |
| sw1 (il_fg t3) | dense table `switch`, `default` written first | (from the IL) cases in source order, `default` after them | as predicted | |
| s3 | dense table `switch`, `default` in the middle | cases in source order, then `default` | as predicted | |
| d10 | 3-case `switch` (compare chain) | source (DFS rule) | **`default` first, then cases by descending value** | |
| s2 | compare-chain `switch`, values 30, 10, 20 written in that order | value order or source order (deciding test) | `default`, 30, 20, 10: **by value** | |
| s4 | sparse `switch`, 6 values | | c1xx's binary split (§3.2) | |

Every non-loop, non-switch prediction held. The two failures (d8, d10) were the two mechanisms below, and both
were then predicted correctly on fresh probes (d17, d19, d20; s2, s3).

### 3.1 Why a switch's `default` comes last, or first

There are two kinds of switch at phase 2, and they take different paths. **[C]**

- **Table switch** (dense values). c1xx itself emits the bounds check as ordinary IL before the `OPSWITCH`:
  `t = x - min; cmp t, range; OPBRANCH (above) -> default`. So `default` is the *branch target* of the
  bounds-check block, whose fall-through is the switch block. By §2 the DFS visits `default` first from the
  bounds block, and RPO places the bounds block, then the whole switch region (cases in source order, i.e.
  their lexical order), then `default`. This is the "default last, even when written first" result (il_fg t3,
  il.md §5.2 rule 5). It is the ordinary DFS rule, not a special case.
- **Compare-chain switch** (few or sparse values; the switch tuple has `+0x3c` bit 0). `FgSortFlow` 10b28b90
  deletes the switch block's arcs and rebuilds them: the `default` arc first, then `FgCaseArcs` 10b28c70,
  which recurses down the case list *before* adding each arc. The result is a successor list in case-list
  order with `default` last. c1xx's case list is **sorted by value** (s2). So the DFS visits cases in
  ascending value order and `default` last, and RPO places **`default` first, then the cases in descending
  value order** (cases that fall through into another case stay in front of it). FgLowerSwitches later turns
  the switch into the `je` chain in ascending value order, ahead of the bodies.

### 3.2 Sparse switches

For widely spread values (s4: 1, 20, 77, 300, 500, 9000) the IL at `@MscRVO@4` already holds a value split:
`cmp 300` with a branch to the upper part, a second `cmp 300` for the equal case, an `OPSWITCH` for the lower
values, and single compares for 500 and 9000. **The split is not c1xx's: c2's reader makes it** (`SwGenSwitch`
10bed250, called from `RdrGetTuples`, before any pass that can be dumped), and the rule is isel.md §9.2 to §9.4
**[C there]**:
- a switch is split when it has 4 or more compared values and its range R is ≥ 255 and > 4 × that count (/O2);
- the pivot is sorted node `N/2` unless there is a dense cluster of 4 or more values (isel.md §9.4), and the
  code is `cmp x, pivot; jg RIGHT; je pivot_case`;
- each side goes through the method choice again, so a leaf is a compare chain of at most 3 values (a
  compare-chain `OPSWITCH` or bare compares) or a table.

Applied to s4: N = 6, R = 8999, no cluster → pivot = node 3 = 300, left leaf {1, 20, 77}, right leaf {500, 9000},
which is the IL above. `work/scratch/openq/sw.cpp` **[C]**: e5 (10, 200, 3000, 40000, 500000) → pivot 3000; s8
(the s4 values plus 20000, 50000) → pivot 500, and the left side {1, 20, 77, 300} is split again at 77; d2
(1..4, 100..103: R = 102) is one table.

Block order after the phase 2 reorder (`order.py`, same probes) **[C for these three, I as a general rule]**:
left subtree, pivot's case, then (once, after the outermost pivot's case) `default`, then the right subtree;
a leaf chain lists its cases in descending value. s4: 77, 20, 1 | 300 | default | 9000, 500. s8: 20, 1 | 77 | 300 |
500 | default | 50000, 20000, 9000. e5: 200, 10 | 3000 | default | 500000, 40000. In the listing all compares
of a subtree come first (`jg je je je jne` for s4), then its bodies in that order.

## 4. Loop exits are moved out of the loop (LgRemoveBreakBlocks)

`LgBuildLoopGraph` 10b85e90 (called at the end of FgDFOReorderLoopRecog) runs `LgRemoveBreakBlocks1`
10b86d60 over every loop, innermost first. It walks the blocks from the loop's head to its last block. A run of
blocks that lies inside that span but does **not belong to the loop** (the block's loop chain at +0x16c does
not contain it), and whose DFO numbers do not exceed the first one's, is moved with `FgMoveBlock` to just after
the loop's last block. **[R]** for the details, **[C]** for the effect:

- Blocks that leave the loop and never come back: `if (..) { ...; return; }`, `if (..) { ...; break; }`, a
  `goto` out. An arm that is only the `break`/`return` jump itself is part of the test block and does not move
  (d5).
- Several moved runs keep their source order and sit between the loop and the code after it (d20).
- In a nested loop, a block leaving only the inner loop moves to the end of the inner loop and stays in the
  outer one (d19).

What it means in code: the loop body is contiguous, the exits come after the bottom test, and the code after
the loop is often reached by a `jmp` (or its small blocks are copied into the exits by FgCodeMotion). In a
decompilation, an exit arm whose code comes after the loop's back edge is the normal result of an `if` inside
the loop. It does not mean the source had a `goto`.

## 5. Loop rotation (zero-trip test)

`LgZtripTest1` 10b871a0 rotates a loop whose head ends in a conditional exit. It copies the **head block**
(`TupCopyUniqueList`) above the loop as the zero-trip guard, with its branch inverted, and the loop then ends in
the bottom test. Conditions, from the code and probes:

- needs `_Globopt_flag` (so not /Od), the loop not flagged +0x4c bit 0, and the loop's last block ending in a
  plain `goto` back to the head **[R]**;
- `LgHasZtripTest` 10b878a0: the head's conditional branch must leave the loop (its target is outside the loop,
  in the parent loop or at function level) **[R, not fully decoded]**;
- `LgUsefulZtripTest` 10b879b0: a one-block loop is inverted with `FgJumpInvert` instead of being copied
  **[R]**;
- `LgDontDupLoopTest` 10b87f10 refuses when the head holds inline asm (OPASM 0x2ce) or a kind-0x13 tuple with
  +0x30 == 0xde **[R]**. **There is no size limit**: a test of six calls is copied (z3) **[C]**.

| probe | loop | result |
|---|---|---|
| z1 | `while (h(x)) A` | rotated: `je` guard, A, call, `jne` back |
| z2 | `for (i = 0; i < 10; i++)` | no guard (the trip count is known), bottom test only |
| z3 | `while (h(1)+...+h(6) > 0)` | rotated; all six calls copied |
| z4 | `for (;;) { if (h(x)) break; A; }` | **rotated too**: the head ends in a conditional exit, which is all it needs |
| z6 | `while (volatile)` | rotated |
| z7 | `while (h(a) && h(b)) A` | **only the first block** (`h(a)`) is copied: the guard is `h(a)`; inside the loop, `h(b)` stays at the top as a `je` exit, and the bottom test is `h(a)` again |
| d4, d8 | `for (i = 0; i < n; i++)` | c1xx emits `init; goto test; inc: ...; test: ...; body; goto inc`; after phase 2 it is `init; guard; body; inc; bottom test` |

### 5.1 Header split (LgFindLoops)

`LgFindLoops` 10b86210 finds each header (a block with a predecessor it dominates, at a later DFO number). At
10b86487 it gives the header a fresh pre-header: a new label (tuple 0x2f2) and `FgSplitBlock` of the header's
lexical predecessor, and redirects every **entry** edge (a predecessor not dominated by the header) that
*branches* to the header's label to the new block. It does this when the header's lexical predecessor belongs
to a different loop than the header, or when an entering branch (`OPBRANCH` or a switch case) targets the
header directly; with `param_4` set (a split already happened) it does it for every header. **[R]** Not probed:
the common shapes (d4, d5, d8, z1-z7) need no split.

## 6. Tail merging of two jumps: the exact size rule (the t5 `t17` anomaly)

il_fg.md §7.2 has the outline. The size check in `FgDoCommonTailMerge` 10b307e0, traced (`c2trace.py -H
10b30b06=esi>4,esi>9,esi -H 10b30b74=ebp,ebx`):

1. The walk back from both jumps finds the matched instructions.
2. The count starts at the **first** matched instruction and goes **forward**, adding each real instruction's
   `decode()` length (tuple +9 bit 0 set; `0x1e3` counts 8), **including the jump itself (5 bytes)**, and it
   stops as soon as the running total is **≥ 20** (2 at /O1).
3. The larger of the two jumps' credits (+0x1c) is added. The tails are merged only if the total is **> 20**
   (unless an earlier exception applies: a whole-block match, or FgTailMergeWithoutCheck).

So the effective rule at /O2, with no credit: **merge if the shared bytes plus 5 exceed 20, but a prefix of the
shared tail that sums to exactly 20 blocks the merge**, however long the tail is. t5 `t17` (10 + 6 + jmp 5 = 21)
merged; t20 (10 + 10, the count stops at 20) did not; a2 in t4 (call 5 + store 10 + jmp 5 = 20) did not; a3
(5 + 10 + 10 = 25) did. The credit was 0 in all of these.

Blind test (tm.c, predictions written before compiling, all 7 held) **[C]**:

| probe | shared tail (bytes, in order) | count | predicted | result |
|---|---|---|---|---|
| m1 | 10, 10, 10 | 20 (stops) | not merged | not merged |
| m2 | 10, 10, 10, 10 | 20 (stops) | not merged | not merged |
| m4 | 7, 7 + jmp 5 | 19 | not merged | not merged |
| m5 | 7, 7, 7 | 21 | merged | merged |
| m6 | 10, 7 + jmp 5 | 22 | merged | merged |
| m7 | 10, 10, 7 | 20 (stops) | not merged | not merged |
| m8 | 7, 10, 10 | 27 | merged | merged |

m7 and m8 are the same 27 bytes in a different order. For matching: if a shared tail is duplicated in the
target but merged in yours (or the reverse), look at the first instructions of the tail. Reordering two
independent stores in all arms flips the result, and so does a store of a different size (an immediate
that fits in a byte, a register source instead of an immediate).

## 7. Open

- The sparse-switch split: settled in §3.2 (it is c2's reader; isel.md §9.4 has the pivot rule).
- §5.1 header split: not probed. A probe needs a loop entered by a branch to its head (a `goto` into a
  `while`, or a loop right after a `switch` case label).
- **Credit (+0x1c): settled; it exists but left no trace in the final code of any probe.** Trace of
  `size + credit` at 10b30b74 (`ebp`; limit in `ebx`) on `work/scratch/openq/tmc.c`, three arms jumping to one
  join, two of them sharing 10 + 7 + jmp = 22 bytes and the third only the 7-byte store (12 with the jump)
  **[C]**:
  - Credit is per `FgTryCommonTailMerge` call: it is cleared for every jump at the start of the call
    (il_fg.md §7.2), and the pairs are tried from the **lexically last** jump backwards (the label's
    reference list is newest first). So credit only reaches a jump that lies *before* the pair that merged.
    c1 and c6 (short arm written last): the pairs come out 12, 12, 22; the two short pairs are refused before
    the long pair merges, and later calls start from 0 again. c3 and c5 (short arm written first): 22
    (merged), then 12 + 20 = 32 > 20, so the short arm **is** merged into the shared store. c4 (short arm in
    the middle): 12, 22, no credit use.
  - The merge that credit buys is undone. Its shared piece is by construction at most 15 bytes (otherwise it
    would pass without credit), and `FgCodeMotion` copies a join block of ≤ 20 bytes back into the jumps that
    reach it (il_fg.md §8). c3 and c5 end with two copies of `mov BYTE PTR _b1, 21` exactly like c1 and c6,
    with a tail-call join (c3) and with a 40-byte join (c5).
  - For matching: ignore credit. The order of the arms decides which pairs are tried first, but in these
    shapes the bytes do not depend on it. Not tried: /O1 (limit 2, where the size test rarely fails anyway)
    and a join that `FgCodeMotion` refuses to copy for another reason.
- The exact DFO condition that ends a moved run in LgRemoveBreakBlocks1. All probed runs were one block.
