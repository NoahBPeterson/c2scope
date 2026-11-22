# NColor: static map of the x64 register allocator in c2 17.00.61030 (x86_amd64 c2.dll)

Binary: `work/vs2012/bin/c2_x64_1700.dll` (PE32, base 0x10000000). All addresses are VAs in that image.
Method: static reading only (capstone disassembly via `work/port1700/c2dis.py`). Nothing was run.
Disassembly dumps of the ~60 functions read are in `work/port1700/t/*.s`; `work/port1700/regtab.py` dumps
register names / attributes / order tables (`regtab.py names`, `regtab.py <table-va>...`).

Marking: **[C]** = confirmed from the instructions read. **[I]** = inferred (from names, shape of the
code, or partial reading). Where a function was only skimmed this is said.

Calling convention: `__fastcall` (ecx, edx, then stack, callee pops). "TLS+x" = field x of the
per-thread compiler state block.

---------------------------------------------------------------------------------------------------

## 0. The model in one paragraph

NColor is not a graph colorer and not the 14.00/15.00 `Color`. **[C]** It is a priority-ordered,
one-candidate-at-a-time allocator over a linear tuple numbering:

* every physical register has, per block, a list of *available tuple ranges* (TLS+0xf65c); interference
  is "the register is not available over that range", nothing else;
* candidates (`NCOLLIFETIME`, one per enregisterable symbol) are sorted once, then processed in order;
* for a candidate, a register is searched that is available over **all** ranges of the lifetime
  (`NColDeterminePhysregAvail`, walks a register order list, first acceptable wins, with a few
  fallback classes); if none, the register with the best partial "coverage" is taken
  (`NColBestCoverage`, benefit minus load cost, weighted 4^depth), the covered part is separated into
  its own `NCOLREGION` and the remainder is processed again as a new region (loop in
  `NColProcessCandidate`);
* a region that gets no register is folded (recalculated / memory operand / load folded), or steals a
  use-free "gap" window from an already allocated region (`NColTryToImproveAllocation` ->
  `NColSearchForRegionWithGap`), or as last resort forces a register free (`NColMakePhysregAvail`);
* after each candidate the chosen ranges are subtracted from the availability of the register and of
  all its aliases (`NColUpdatePhysregAvail`);
* after all candidates, callee-saved registers whose total benefit is below a threshold are given
  back (`NColPostspill` -> `NColSpillAllocatedReg`), then the code is rewritten.

---------------------------------------------------------------------------------------------------

## 1. Phase structure of NColor (0x10066390, ecx = FUNC*) [C]

Order of calls, as in the code:

| # | call | what it does |
|---|------|--------------|
| 1 | `freemem(8)`, `freemem(9)` | reset the two NColor pools |
| 2 | zero TLS+0xda24, f660, f664, f694, f6a4, f6a8, f6ac, f6d4, f6dc, f6e0; TLS+0xf6b0 = `NColAddingLoopBodyToCluster` (0x1018a120) unless TLS+0x1073c == 2 (then cold path 0x102d0cce; **[I]** installs `NColAddingHotTraceToCluster`, the PGO-trace variant) | per-function init |
| 3 | `NColZeroSymScratch` 0x10179100 | zero SYM+0x38..+0x44 of every symbol-table entry (0x64-byte entries, skipping kind 0x10) |
| 4 | `NColBlockInfo` 0x10081e50 | flow graph, block numbering, (opt) loop recognition, `NColBuildHotZones`, landing/sink pads, dominators; allocates per-block arrays f648 (0x34/block), f64c, f68c, f69c, f680, f684; f690 = numBlocks+1 |
| 5 | `NColCanon` 0x1008fed0 | canonicalise candidate symbols (replacement syms, enter-load / exit-save transforms) **[I]** skimmed |
| 6 | `NColMdPerFuncInit` 0x100925b0 | machine-dependent: builds the per-class "initial register" BVs at TLS+0x11970.., allocates the pref-order buffer TLS+0x119c4, frame-pointer regs |
| 7 | `NColInitializeSyms(func, &nPhys, &nTuples)` 0x1017a350 | numbers symbols: returns nCand (-> TLS+0xf6b8); candidate index stored in SYM+0x38 of `operand.sym->+0xc`; operands with a pre-assigned physreg (operand+0x28 != 0) get a separate numbering (nPhys); counts memory-operand uses in SYM+0x40 |
| 8 | 6 x `getmem(8, ..)` | f640 = (nCand+1)*0x5c lifetimes; f644 = (nPhys+1)*0xc; f668, f66c = nTuples*0x18; f678 = nTuples*4; f67c = nTuples; f6d0 = 0x5b0 (364 regs * 4) |
| 9 | `NColDoLiveness(func, nCand, nTuples)` 0x10221640 | numbers tuples; fills f654 (tup# -> Tuple*), f650 (tup# -> block#), f670 (tup# -> loop depth, capped 5), f674 (tup# -> zone depth), blockinfo first-tuple / depth; live-in/out BVs per block |
| 10 | `NColBuildInfo(func, nTuples, nCand)` 0x100d7220 | `NColInitializeAvailability`; backward walk over all tuples building for each candidate its range list, def / use / appearance lists, first/last tuple, the "copy to/from physreg" preference (L+0x38, L+0x54); per-block live count f69c[b] and its max f698; marks physregs already referenced (P+9 \|= 0x10); ends with `NColCreateAvailabilityBlockMaps` (f65c) |
| 11 | only if optimizing (TLS+0xdcf4 != 0 and !FWvmSafe): `NColSubsumption(nCand)` 0x1009ab40, `NColFoldTooCheap(nCand)` 0x10066040, f688 = getmem(4*nTuples), `NColBuildSpillAvoid(nPhys)` 0x10185380, f6a0 = getmem(0x5b0) | coalescing of copy-related lifetimes (`NColCombineLifetimes`), folding of defs that are cheaper to recompute, "spill avoid" table, per-reg preference-range lists. Non-opt: f688 = 0 |
| 12 | `NColPrioritizeCandidates(nCand, nTuples)` 0x10184d70 | returns a null-terminated array of NCOLLIFETIME* in processing order (section 2) |
| 13 | loop 0x10066583: `NColProcessCandidate(L)` 0x10068300 for each array entry | **the allocation** (section 3) |
| 14 | `NColPostspill()` 0x1006cfa0 | un-allocates callee-saved registers that did not earn their save/restore (section 5) |
| 15 | TLS+0xdcbc = 0; `NColInsertPhysregSpills(func)` 0x101a0c60 | materialise the spills recorded by `NColMakePhysregAvail` (list f660) |
| 16 | loop 0x1006652c: `NColRewriteCandidate(L, func)` 0x10066600, same array order | rewrite operands to physregs, insert loads/stores; marks P+9 \|= 0x10 for every reg used |
| 17 | `NColSinkStores()` 0x1019dc60; if f694: cold 0x102d0d34 (**[I]** `NColAdjustTransferSpills`) | |
| 18 | opt only: `NColRemovePlaceholders(func)` 0x1018a510 | |
| 19 | `NColDetectDuplicateSpills(func)` 0x1017a7c0, `freemem(8)`, `freemem(0xf)`, `LgFreeReachInfoGraph` | |

Mode flags **[C as tested, I as named]**: TLS+0xdcf4 = "optimizing" (0 -> the simple path: no
subsumption, no priorities, no folding); global `FWvmSafe` (0x103f99a4) forces the simple path;
TLS+0xdd08 = "favor speed" (when 0 all the `<< 2*depth` weightings are dropped); TLS+0x1073c == 2 =
a third mode with separate cold code (probably PGO trace layout).

---------------------------------------------------------------------------------------------------

## 2. Candidate order: NColPrioritizeCandidates (0x10184d70, ecx = nCand, edx = nTuples) [C]

Non-opt (or nCand == 0): array = lifetimes in **descending index** (life[n], ..., life[1]), no sort.

Opt: for each lifetime i = 1..n (L = f640 + i*0x5c), skipped if L+0x1c (appearance list) is empty:

1. If L+0x38 != 0 (lifetime is copied to/from a physical register at tuple L+0x54): for the range of
   L+0x10 that begins (or ends) at L+0x54, call `NColAddRegPrefRange(L, begin, end)` (0x101850c0)
   with the range clipped to 15 tuples from L+0x54. This inserts a {next, prev, begin, end} node in
   the sorted list TLS+0xf6a0[reg(L+0x38)] and stores it in L+0x3c. Other lifetimes avoid that
   register over that window (section 4, "fallback B").
2. Priority (L+0x48):
   * **local** lifetime (block of first tuple == block of last tuple, f650[L+0x40] == f650[L+0x44]):
     `prio = nTuples - (last - first)` -- shortest first.
   * **global** lifetime: sum over every range of L+0x10, over every block b from block(range.begin)
     to block(range.end):
     * b contains k appearances of L: `+ ((f698 + 4) - f69c[b]) * k * 8 << (2 * depth[b])`
     * b contains none: `- f69c[b] << (2 * depth[b])`
     where depth[b] = blockinfo[b]+0x2c (zone depth), f69c[b] = number of candidates live in b,
     f698 = max of f69c. If TLS+0xdd08 == 0 the same without the shifts (cold 0x102d07f9).
     The maximum depth seen is kept (-> `NColMdAdjustPriority`).
3. Bucket (6 singly linked lists through L+0):
   class = (SYM.type >> 12) & 0x1f, int if `(1 << class) & 0x1e` (classes 1..4):

   | list | contents |
   |---|---|
   | 0 | non-int (xmm/float), local |
   | 1 | non-int, global |
   | 2 | int, local |
   | 3 | int, global |
   | 4 | int, local, single def whose def tuple `NColUsesOnlyConsts` |
   | 5 | int, global, single def from constants only |

   TLS+0xf6e0 = number of non-int candidates.
4. `NColMdAdjustPriority(lists, maxDepth)` 0x10185150: if maxDepth != 0 and !(FUNC+0xe4 & 0x80):
   the int-global candidate (list 3) with the highest priority that exceeds 16 * max(1, highest
   priority in list 2) and whose SYM+9 bit 2 is clear is moved to the head of list 2.
5. `NColSortAndOrderLists` 0x10185200: each list is copied to the output array and `qsort`ed
   **separately** with `NColColPriorityCompare` (0x1018e350): descending L+0x48, ties by ascending
   lifetime index. Output = list0, list1, ..., list5 concatenated, 0-terminated.

So the processing order is: xmm locals, xmm globals, int locals (shortest first), int globals
(by the weighted appearance score), then the constant-defined ints.

---------------------------------------------------------------------------------------------------

## 3. Processing one candidate

### 3.1 NColProcessCandidate (0x10068300, ecx = L) [C]

1. opt, single def (else L+0x4c |= 0x40): if `NColMdIsFoldableDef(defTuple)` and `NColCanRecalc(L, 0)`:
   builds an alloca'd `NCOLUSEBENEFITS` list {next, useTup#, benefit}, benefit =
   `NColMdNoFoldBenefit(useTuple, defTuple, sym, 0)` or 0x7fffffff for uses whose bit is set in the
   BV L+0x24. Otherwise `NColCheckCanUseLoadedMemOpr(L)`.
2. opt: TLS+0xf6dc = `NColIdentifyExtraPhysAvail(L)` (0x100c8c80): physregs that the lifetime's own
   def / last use instruction reads or writes (e.g. `mov cand, rcx`) are made temporarily available
   from that tuple; list of {next, ->tup#, physreg SYM}.
3. Builds the first `NCOLREGION` (0x40 bytes) as a copy of the whole lifetime: ranges (L+0x10),
   defs (L+0x14), uses (L+0x18), first/last (L+0x40/0x44 -> R+0x24/0x28).
4. Loop (0x100684a9): `next = NColProcessCandidateRegion(L, R, R->next, useBenefits, defTuple, flag)`;
   push R on the list L+8; R = next; until next == 0.
5. `NColCalcFixupCodeForLifetime(L)` 0x10069ad0 (loads/stores between regions, `NColDetermineStores`);
   if L+0x4c & 1: `NColHandleFolds`; if & 2 and & 0x40: `NColFoldRecalcs`.
6. `NColUpdateRegisterUsage(L)` 0x10069e00: for each region with R+0x1c != 0 (and not a foldable
   singleton, which becomes a memory operand instead): `NColUpdatePhysregAvail(P, R, L, 0, ..)`.
7. Unlink L+0x3c from the f6a0 preference-range list; `NColRestoreExtraPhysAvail()`; L+0x4c |= 0x100.

### 3.2 NColProcessCandidateRegion (0x10068680) [C]

Args: ecx = L, edx = R, [ebp+8] = next region (0 the first time), [ebp+0xc] = NCOLUSEBENEFITS* (0 if
the def cannot be recalculated), [ebp+0x10] = def Tuple*, [ebp+0x14] = flag (def has a register
source). Returns the remainder region still to process, or 0.

1. `useable = NColDeterminePhysregUseable(R, sym, &prefList, &conflictTup)` (section 4.1).
   Empty BV -> cold 0x102d0875: `NColSplitRegion(R, conflictTup)` and retry on the first piece.
2. First region of the lifetime only: every entry of TLS+0xf6dc whose register is in `useable` is
   chained in front of the preference list (through P+0x34, P+0x38 = tup#).
3. first = begin of the first range of R, last = end of the last range.
4. If recalculation is possible: `NColBenefitOfUsesInRange(useBenefits, first, last, &n, ..)` gives
   the benefit of keeping the value in a register; weight = zone depth of the def (f674).
5. `P = NColDeterminePhysregAvail(L, R, useable, first, last, prefList, &fullCover, &benefit)`;
   R+0x34 = 0x7fffffff.
6. Recalc check: if `MdmDefCost(defTuple) << 2*depth * nDefs >= benefit` -> `NColFoldAwayRegion(R, L,
   defTuple)` (the region is rematerialised at its uses, no register), return. Else R+0x34 =
   benefit - cost.
7. **P != 0 and fullCover**: R+0x2c = first, R+0x30 = last; loads are taken from the lifetime
   (first region) or built by `NColBuildRegionLoads`; a second recalc-vs-load cost check may still
   fold the region away (0x10068994..); finally **R+0x1c = P** at 0x1006887b and the saved "next"
   is returned.
8. **P != 0, partial** (from `NColBestCoverage`): unless recalculable, `NColIsLousyCoverage(R, n,
   &b, &e, first, last)` (opt only: the deepest loop range intersecting the region is not covered)
   -> `NColTryToImproveAllocation(L, R, useable, b, e, &benefit, 0)` may replace P. Then
   `NColSeparateProcessedRegion(L, R, next, n)` cuts R down to the covered ranges (array TLS+0xf668)
   and returns the remainder region, which becomes the function's return value.
9. **P == 0**: in order
   * recalculable -> `NColFoldAwayRegion`;
   * opt and !(L+0x4c & 8): `NColTryFoldUses(R, L)`;
   * `NColSourceOfLoadCanBeFolded(R, L)` -> `NColBuildRegionLoads`, `NColDoLoadFold`, L+0x4c |= 4;
   * `NColCanReplaceWithMem(R, sym, L+0x24, L+0x30)` -> `NColDoReplaceWithMem` (use the home
     location as a memory operand), L+0x4c |= 4;
   * `NColTryToImproveAllocation(L, R, useable, R.first, NColLastAppearInBlock(R, block(first)),
     &benefit, 1)`; cold 0x102d0942: again with the window (first, first);
   * cold 0x102d0965: `NColMakePhysregAvail(?, R, useable)` (0x10222350) ->
     `NColSelectPhysregToMakeAvail` picks a register, `NColAddToPhysregSpillList(P, first, tup)`
     records a save/restore of that register around the appearance (list f660); R+0x38 |= 0x20
     (forced). The window is written to f668[0..].
   Then continue at step 7 or 8 depending on whether the obtained window equals the whole region.

So nothing is ever "left in memory" by a spill decision as in `Color`: a region either gets a
register, or its appearances are rewritten (fold / memory operand), or a register is forced.

---------------------------------------------------------------------------------------------------

## 4. How the register is picked

### 4.1 NColDeterminePhysregUseable (0x10068d50) [C]

Args: ecx = R, edx = SYM*, [ebp+8] = &prefListHead (out), [ebp+0xc] = &conflictTup (out). Returns BV*.

* `BV = NColMdGetInitialRegisters(sym.type)` (0x100693b0): copy of the class master set chosen by
  type class and byte size (TLS+0x11970 qword int, 0x11974 dword, 0x11978 word, 0x1197c byte,
  0x11984.. xmm variants, 0x119ac/0x119b0 float 4/8).
* For every def tuple of R: `NColMdTrimUseableRegisters(BV, tuple, dstOperand)` (only acts when
  FUNC+0x84 != FUNC+0x80, which is a dynamically aligned frame (FUNC+0x5c bit 0), not EH in general: removes
  r13/r13d/r13w/r13b in funclets or with FUNC+0xe4 & 0x80000, cold 0x102d13a5);
  if BV becomes empty: *conflictTup = tup#, return. Then `NColMdAddToPreferences(operand, tuple,
  BV)`.
* Same for every use tuple (source operand found by symbol or by overlap).
* Preference list (chain of physreg SYMs through P+0x34, P+0x38 = tup#):
  * last use tuple: if `NColMdIsCopyInstr` (or opcode 0x114 whose source has opcode 0x4fe / 0x501):
    `NColAddToPreferences(tuple.dst, tup#, BV, 0, !isCopy)`;
  * every def tuple that is a copy: `NColAddToPreferences(tuple.src, tup#, BV, head, 0)` (prepended);
    opcode 0x114: `NColMdLeaPref(tuple)`, appended.

`NColAddToPreferences` (0x100691f0; ecx = operand, edx = tup#, [ebp+8] = useable BV, [ebp+0xc] = list
head, [ebp+0x10] = append flag; returns new head): operand must be kind 1. P = operand+0x28 if the
operand is already a physreg; otherwise the operand's symbol is another candidate: its lifetime is
f640[SYM+0x38]; `NColFindRegionInTree(L'+0xc, tup#)` gives the region covering that tuple and P =
that region's R+0x1c (so only **already processed** candidates contribute). P is added if its number
is in the useable BV and it is not already in the list.

`NColMdAddToPreferences` (0x10069010; ecx = operand, edx = tuple, stack = BV) does **not** add
preferences; it sets the candidate's **anti-preference** set SYM+0x44 when the candidate is the base
of a memory operand (operand kinds 5/6) in an opcode-0x13b tuple, or the source of opcode 0x114:
base without index -> TLS+0x119c8; base without displacement -> TLS+0x119cc; both -> TLS+0x119d0.
**[I]** these are the thread copies of `NColMdAntiPrefR12MasterBv`, `...RBPR13...`, `...R12RBPR13...`
(r12 as base needs a SIB byte, rbp/r13 need a disp8).

`NColDesiredReg` (0x100c6680; ecx = P, edx = byte size, stack = useable BV): walks P's alias chain
(P+0xc = master register, then +0x10) and returns the alias of the requested size that overlaps P and
is in the BV. Used by `NColTryToImproveAllocation` to convert the stolen register to the right width.

### 4.2 Register order

`NColMdGetRegOrder(sym)` (0x1009e410, ecx = SYM*; returns a 0-terminated DWORD array of register
numbers) **[C]**: by type class = (type >> 12) & 0x1f and size = type & 0xfff:

| class, size | table | contents |
|---|---|---|
| 1,2,3,6 (int) | `NColMdGetPrefRegOrder(sym)` | built per call, below |
| 5, 8 | 0x100060d0 | fxmm0l..fxmm15l (320..335) |
| 5, 4 | 0x10006150 | fxmm0s..fxmm15s (336..351) |
| 0xc, 0x10 | 0x10006020 | xmm0..xmm15 (144..159) |
| 0xc, 8 | 0x10006080 | xmm0l..xmm15l (240..255) |
| 0xc, 4 | 0x10021c50 | xmm0s..xmm15s (288..303) |
| 0xc, 0x20 | 0x10005ea0 | ymm0..ymm15 (112..127) |
| 0xd, 0x10 | 0x10005fb8 | dxmm0..dxmm15 (160..175) |
| 0xe, 0x10 | 0x10005f50 | ixmm0..ixmm15 (176..191) |
| 0xe, 8 | 0x10005ee8 | ixmm0l..ixmm15l (272..287) |
| 0xf | 0x1001d520 | mm0..mm7 |

All xmm-family tables are in plain numeric order.

The publics `?IntRegOrder@@3QBKB` (0x100068b8 = rax rcx rdx rsi rdi rbx rbp) and
`?XMMXRegOrder@@3QBKB` (0x10005f94 = xmm0..xmm7) are **not referenced by any NCol code**: their only
references are from a pointer table in .rdata near 0x100015e4 **[C]**. They look like leftovers of the
x86 tables; NColor uses the two unnamed tables below.

`NColMdGetPrefRegOrder(sym)` (0x1009e500) **[C]** writes into the buffer TLS+0x119c4 (16 dwords, entry
15 forced to 0) from two static tables of 64-bit register numbers:

* T1 at 0x10002704 (7 entries): **rax rcx rdx rbx rdi rsi rbp**
* T2 at 0x1000553c (8 entries): **r8 r9 r10 r11 r14 r15 r12 r13**

Each emitted number is `base + delta`, delta = 0 (size 8), 0x10 (size 4: eax..), 0x20 (size 2),
0x30 (size 1). Let A = SYM+0x44 (anti-pref BV, may be null), cs(r) = RegAttrib[r] & 1 (callee-saved:
rbx rbp rsi rdi r12..r15, xmm6..15), used(r) = physreg SYM flag P+9 & 0x10 ("already referenced in this
function": set by `NColBuildInfo` for regs present in the incoming code and by `NColRewriteCandidate`
for every allocated reg). Passes, all skipping regs in A:

1. T1 regs with !cs or used
2. T2 regs with !cs
3. T1 regs with cs and !used
4. T2 regs with cs
5. (only if A != 0) T1 regs in A, then T2 regs in A

Resulting default order with nothing used yet: rax rcx rdx | r8 r9 r10 r11 | rbx rdi rsi rbp |
r14 r15 r12 r13. Note `used` is updated by `NColRewriteCandidate`, which runs after all candidates
are processed, so during allocation it only reflects registers present before NColor (parameters,
calls, fixed-register instructions) **[I]** unless another writer exists that was not read.

RegAttrib (0x10001a40, DWORD per register number, 364 registers): bit 0 callee-saved, bit 1 not
allocatable (rsp, segment, x87, cc...), 0x8000 on r10/r11/r12, 4 on ymm?h.

### 4.3 NColDeterminePhysregAvail (0x10069570) [C]

Args: ecx = L, edx = R, [ebp+8] = useable BV, [ebp+0xc] = first, [ebp+0x10] = last, [ebp+0x14] =
preference list (physreg SYM chained by +0x34), [ebp+0x18] = &fullCover, [ebp+0x1c] = &benefit.
Returns physreg SYM* or 0.

**A. Preferences first.** Two passes over the preference list: first the preferred registers that are
*not* in the anti-pref set SYM+0x44, then (only if SYM+0x44 != 0) those that are. For each D:
`r = NColFindFirstOverlappingAvailRegion(D, first, last)`; if `NColDetermineCovered(R.ranges, r, L, D)`
-> *fullCover = 1, **return D**. The register numbers of the first and of the last preference tried
are remembered and skipped in B.

**B. Order loop.** `order = NColMdGetRegOrder(sym)`. For each register number r:

1. r must be in the useable BV and not one of the two remembered preference numbers.
2. P = physreg table (TLS+0x113f4) + r*0x64. The first available range of P overlapping
   [first, last] is located through f65c[(block(first)*364 + r)] (per-block entry points into one
   sorted doubly linked list of {next, prev, begin, end} per register). None -> next register.
3. Every range of R must lie inside one available range. Boundary cases: if an available range begins
   exactly at the region range's begin, the tuple there must not have P among its sources when the
   candidate is also a source there (or the def needs a load, `NColIsNoLoadDef`); if it ends exactly
   at the range end, the tuple must not define P while also defining the candidate
   (`NColPhysregInOprList`, `Tuple::FindSrcSym/FindDstSym`). Any failure -> next register.
4. Covered: *fullCover = 1. Then classification of P:
   * P number in 0x76..0x7f (ymm6..ymm15): remembered as **fallback A** (first such only), continue.
   * opt, and P's master register != L+0x38, and the list f6a0[master reg] contains a preference
     range of another lifetime overlapping R's first range: if the master is callee-saved ->
     fallback A, else **fallback B** (first only); continue.
   * master register callee-saved (RegAttrib bit 0, via `MscGetCorrespondingXMMRegister(P, 0)`, which
     for integer regs just returns P+0xc): **return fallback C if set, else P**.
   * otherwise (scratch register): gap = first - begin of the available range. If gap >= 1 (int) or
     >= 3 (non-int) -> **return P**. Else P becomes **fallback C** if its gap is larger than the
     current fallback C's; continue. (A register freed by the immediately preceding tuple is avoided
     when another one will do.)
5. End of the order: return fallback C, else fallback B, else fallback A, else
   `NColBestCoverage(L, R, useable, first, last, &benefit)` with *fullCover left 0.

Consequences useful for rasim **[C]**: among scratch registers the first one in order that has been
free for at least one tuple wins; a callee-saved register is reached only after all scratch registers
in the order failed, and then the first fully covering one is taken at once (preference-range
conflicts are not looked at for a callee-saved hit that passes the f6a0 test).

### 4.4 NColBestCoverage (0x1006a030) [C for the frame, I for cluster-growth details]

Args: ecx = L, edx = R, [ebp+8] = useable BV, [ebp+0xc] = first, [ebp+0x10] = last, [ebp+0x14] =
&nRanges (out). Returns physreg SYM* or 0. 4.3 KB; read in full only for the outer loop and scoring.

For each register of the same order list that is in the useable BV: `NColGetPhysregAvail` builds a
bit vector of the tuples where the register is available in [first-1, last+1]. Starting at each
appearance of the candidate where the register is available, a *cluster* (alloca, 0x20 bytes:
+0 next, +4 id, +8 first block, +0xc last block, +0x10 begin tup, +0x14 end tup, +0x18 benefit,
+0x1c cost) is grown block by block over successors while the register stays available
(`NColMarkSuccessors`, `NColCanUsePhysregAtStartPoint/EndPoint`, `NColRegAvailAtExit`; the callback
TLS+0xf6b0 decides whether a loop body / hot trace may be added; overlapping clusters are merged by
`NColMergeClusters`). benefit += appearances covered << (2 * zone depth); cost = loads on entry
(1 << 2*depth, 0 if the first appearance is a def that needs no load) plus entry edges from
uncovered predecessors. Score of a register = max over its clusters of (benefit - cost)
(0x1006a334). The register with the **strictly** greatest score wins (0x1006a348 `jg`), so ties go
to the earlier register in the order. On a new best: `NColCopyClusterToRangesArray(f668, cluster)`
writes the covered ranges to TLS+0xf668 and its return value to *nRanges (0x1006aeb3). If the
candidate has more than 1000 appearances in play ([ebp-0x68] > 0x3e8) the search stops at the first
register that scores.

### 4.5 Gaps, windows, splits

* `NColTryToImproveAllocation` (0x100c6940; ecx = L, edx = R, stack: useable BV, begin, end, &benefit,
  force) **[C]**, opt only: `victim = NColSearchForRegionWithGap(begin, end, size, useable, &ownerL,
  &b, &e, sym)`; `NColAdjustWindow(&begin, &end, b, e)`; unless `force`, requires the window to span
  different f670 values (loop depths) at its ends and `NColUsesInWindow(R, begin, end)`; then
  `NColSubtractWindowFromRegion(ownerL, victim, L, begin, &end)` (the victim keeps its register
  outside the window; store before / load after), `NColAddWindowToRegAvail(victim.reg, begin, end)`,
  `NColNewTupleRangesFromWindow(begin, end, L, R, &benefit)`; returns the victim's register, converted
  with `NColDesiredReg` if the size differs.
* `NColSearchForRegionWithGap` (0x10071950) **[C for the head, rest skimmed]**: expands the useable
  BV to all aliases; scans **all** lifetimes 1..f6b8 not flagged folded (L+0x4c & 1), all their
  regions with R+0x1c in that set whose window [R+0x2c, R+0x30] (or a range, once the owner is fully
  processed) contains [begin, end] and that have no def and no use inside it; hot zones flagged by
  `NColZoneIsProblematic` are excluded; keeps the cheapest (initial best = 0x7fffffff).
* `NColSeparateProcessedRegion` (0x100c7a20; ecx = L, edx = R, stack: next, nRanges): keeps in R the
  ranges of f668 (`NColRangesToMinTupleRegions`), moves the rest
  (`NColSubtractRangesFromTupRegions`) with their defs/uses into a new region and returns it.
* `NColSplitRegion` (0x10222880; ecx = R, edx = tup#): split at a tuple whose register constraints
  emptied the useable set.

---------------------------------------------------------------------------------------------------

## 5. After allocation

* `NColUpdatePhysregAvail` (0x1006b120; ecx = P, edx = R, stack: L, flag, ?) **[C]**: if P's master is
  callee-saved: TLS+0xf6d0[master reg] accumulates R+0x34 (the region benefit; 0x7fffffff sticks).
  Then for every range of R and every alias of P that overlaps it (walk P+0xc, +0x10 with the
  +0x24/+0x28 overlap test) the range is cut out of that alias's available list.
* `NColPostspill` (0x1006cfa0) **[C]**: for every master register with RegAttrib bit 0: total =
  f6d0[reg]; if total != 0, != 0x7fffffff and total < threshold -> `NColSpillAllocatedReg(P)`
  (0x100ca950: every region of every lifetime using an alias of P is folded away / unassigned,
  R+0x1c = 0). Threshold: 3 when TLS+0xdd08 != 0 (speed); otherwise 10 for reg numbers < 0x70 and 12
  for the rest.
* `NColRewriteCandidate` (0x10066600; ecx = L, edx = FUNC) **[C for the head]**: walks L+8; region
  flags R+0x38 & 8 and & 1 take other paths; for a region with uses and R+0x1c: marks P and P.master
  used (P+9 |= 0x10) and rewrites the def/use operands.

---------------------------------------------------------------------------------------------------

## 6. Layouts

### NCOLLIFETIME (0x5c bytes; array TLS+0xf640, index 1..nCand; index = SYM+0x38) [C unless noted]

| off | content |
|---|---|
| +0x00 | next (bucket list in Prioritize) |
| +0x04 | SYM* of the candidate |
| +0x08 | list of processed NCOLREGION (through R+0) |
| +0x0c | region tree root (`NColRewriteAsTree`, `NColFindRegionInTree`), built lazily when L+0x4c & 0x100 |
| +0x10 | range list: NCOLTUPRGN {+0 next, +4 prev, +8 begin tup#, +0xc end tup#} |
| +0x14 | def list: NCOLTUPNUMLIST {+0 next, +4 tup#} |
| +0x18 | use list (same node type) |
| +0x1c | appearance list (defs and uses, ascending tup#) |
| +0x20 | BV, tested per def tup# in `NColUpdateRegisterUsage` **[I]** defs already stored |
| +0x24 | BV of use tup#s that force "infinite" no-fold benefit; also passed to `NColCanReplaceWithMem` |
| +0x30 | passed to `NColCanReplaceWithMem` **[I]** memory home / tuple |
| +0x34 | load list of the lifetime (NCOLLOAD, +0x14 = tup#), handed to the first region when it covers everything |
| +0x38 | master physreg SYM* the lifetime is copied to/from (0 if none) |
| +0x3c | this lifetime's node in the f6a0 preference-range list |
| +0x40 | first tup# |
| +0x44 | last tup# |
| +0x48 | priority (int) |
| +0x4c | flags: 1 has folds, 2 recalcs, 4 replaced by memory / load folded, 8 no fold attempts, 0x20 loads taken from lifetime, 0x40 more than one def, 0x100 fully processed |
| +0x50 | 3-bit field copied from operand+0x1c bits 10..12 **[I]** meaning unknown |
| +0x54 | tup# of the copy to/from L+0x38 |

### NCOLREGION (0x40 bytes) [C unless noted]

| off | content |
|---|---|
| +0x00 | next |
| +0x10 | range list (NCOLTUPRGN) |
| +0x14 | def list |
| +0x18 | use list |
| +0x1c | **assigned physreg SYM*** (0 = none / folded / spilled back) |
| +0x20 | load list (NCOLLOAD) |
| +0x24 | first tup# |
| +0x28 | last tup# |
| +0x2c | begin of the window actually held |
| +0x30 | end of the window actually held |
| +0x34 | benefit (0x7fffffff = unbounded; else benefit - recalc cost) |
| +0x38 | flags: 1, 2, 8 (alternate rewrite forms **[I]**), 0x20 register was forced |

### Physreg SYM (0x64 bytes; table TLS+0x113f4, index = register number) [C]

+0 type word (low 12 bits byte size, bits 12..16 class); +8 kind byte (1 = physreg); +9 flags (0x10
used in function); +0xc master (widest containing) register SYM; +0x10 next alias in the master's
chain; +0x20 register number; +0x24, +0x28 bit offset / extent used for overlap tests; +0x34
preference-list link; +0x38 preference tup#.

Candidate SYM: +0 type; +8 kind; +9 flags (bit 2 tested by `NColMdAdjustPriority`); +0xc
equivalence root (candidates are keyed on operand.sym->+0xc); +0x38 lifetime index; +0x40 count of
memory-operand uses (x2 inside a loop **[I]**); +0x44 anti-preference BV.

Tuple: +4 next, +8 opcode, +0xc kind byte (0x19 block label, +0x28 = BLOCK*), +0xd bit 0 =
instruction, +0x18 prev, +0x30 source operand list, +0x34 destination operand list.
Operand: +4 next, +0xc kind (1 register/symbol, 5/6 memory), +0x1c flags, +0x24 SYM*, +0x28
pre-assigned physreg SYM*; memory: +0x30 base, +0x34 index, +0x38 displacement symbol.
BLOCK: +8 predecessor arcs, +0xc successor arcs, +0xb4 HOTZONE*, +0xb8 block number, +0xbe loop depth.
Sparse BV: {SBV* head, +4 pool byte}; SBV node {+0 base (bit & ~63), +4 low dword, +8 high dword,
+0xc next}.

### TLS fields

| TLS+ | content | conf. |
|---|---|---|
| 0xdccc | CurFunc | given |
| 0xdcf4 | optimizing flag | C/I |
| 0xdd08 | favor-speed flag (depth weighting on) | C/I |
| 0x1073c | mode, == 2 selects trace code | I |
| 0x113f4 | physreg SYM table (364 * 0x64) | C |
| 0x11534 | symbol table chunks walked by NColZeroSymScratch | C |
| 0x11970..0x119b0 | per-class initial register BVs | C |
| 0x119c4 | int pref-order output buffer (16 dwords) | C |
| 0x119c8 / cc / d0 | anti-pref BVs (r12 / rbp,r13 / all three) | C use, I names |
| 0xf640 | NCOLLIFETIME array (0x5c each) | C |
| 0xf644 | per pre-assigned physreg record, 0xc bytes, +0 = physreg SYM | C |
| 0xf648 | per-block info, 0x34 bytes: +0x18/+0x1c/+0x20 BVs, +0x24 first tup#, +0x28 loop depth (max 5), +0x2c zone depth; last tup# of b = info[b+1]+0x24 - 1 | C |
| 0xf64c | block# -> BLOCK* | C |
| 0xf650 | tup# -> block# | C |
| 0xf654 | tup# -> Tuple* | C |
| 0xf658 | availability scratch (NColInitializeAvailability) | C alloc only |
| 0xf65c | availability map: [(block# * 364 + reg) * 4] -> first available NCOLTUPRGN of reg at/after that block | C |
| 0xf660 | physreg spill list (NColAddToPhysregSpillList) | C |
| 0xf668 | range array, 0x18 * nTuples; output of BestCoverage / MakePhysregAvail ({begin, end, ...}) | C |
| 0xf66c | second 0x18 * nTuples array | C alloc only |
| 0xf670 | tup# -> loop depth | C |
| 0xf674 | tup# -> zone depth (the exponent in every `<< 2*d`) | C |
| 0xf678 | tup# -> NCOLTUPRGN* scratch (BestCoverage) | C |
| 0xf67c | nTuples | C |
| 0xf680 | per-block cluster scratch, 0x18 bytes | C |
| 0xf684 | per-block appearance list scratch | C |
| 0xf688 | spill-avoid table, 4 * nTuples (opt) | C alloc, I use |
| 0xf68c | per-block, 0x10 bytes | C alloc only |
| 0xf690 | numBlocks + 1 | C |
| 0xf694 | set when a forced spill hits a call-like tuple | C |
| 0xf698 | max live-candidate count over blocks | C |
| 0xf69c | block# -> live-candidate count | C |
| 0xf6a0 | reg# -> sorted list of preference ranges (0x5b0 bytes) | C |
| 0xf6a4, 0xf6a8 | tuple index / tuple of two special tuples (opcode 0x595, kind 0x17) | C |
| 0xf6ac | hot zone root | C |
| 0xf6b0 | cluster-extension callback | C |
| 0xf6b8 | nCand | C |
| 0xf6d0 | master reg# -> accumulated benefit (0x5b0 bytes) | C |
| 0xf6dc | extra-avail list for the current candidate | C |
| 0xf6e0 | number of non-int candidates | C |

---------------------------------------------------------------------------------------------------

## 7. Hook points for the tracer

"len" = bytes of whole instructions displaced at the address (all >= 5, none rip/eip-relative, no
branch target strictly inside the displaced bytes; checked against the branch targets visible in the
functions read). Instructions that use esp/ebp-relative operands are noted: the trampoline must run
them on the original stack.

### Function entries

| VA | function | len | registers / stack at entry |
|---|---|---|---|
| 0x10066390 | NColor | 6 | ecx = FUNC* |
| 0x10184d70 | NColPrioritizeCandidates | 6 | ecx = nCand, edx = nTuples |
| 0x10068300 | NColProcessCandidate | 6 | ecx = L |
| 0x10068680 | NColProcessCandidateRegion | 6 | ecx = L, edx = R, [esp+4] = next R, [esp+8] = use benefits, [esp+0xc] = def Tuple*, [esp+0x10] = flag |
| 0x10069570 | NColDeterminePhysregAvail | 6 | ecx = L, edx = R, [esp+4] = useable BV, [esp+8] = first, [esp+0xc] = last, [esp+0x10] = pref list, [esp+0x14] = &fullCover, [esp+0x18] = &benefit |
| 0x1006a030 | NColBestCoverage | 9 | ecx = L, edx = R, [esp+4] = useable BV, [esp+8] = first, [esp+0xc] = last, [esp+0x10] = &nRanges |
| 0x100c6940 | NColTryToImproveAllocation | 6 | ecx = L, edx = R, [esp+4] = BV, [esp+8] = begin, [esp+0xc] = end, [esp+0x10] = &benefit, [esp+0x14] = force |
| 0x10222350 | NColMakePhysregAvail | 8 | edx = R, [esp+4] = useable BV (ecx: sym, not verified) |
| 0x100ca950 | NColSpillAllocatedReg | 6 | ecx = physreg SYM* being given back |
| 0x1006b120 | NColUpdatePhysregAvail | 6 | ecx = physreg SYM*, edx = R, [esp+4] = L |
| 0x10069e00 | NColUpdateRegisterUsage | 6 | ecx = L (all regions decided: walk L+8, read R+0x1c) |
| 0x10066600 | NColRewriteCandidate | 6 | ecx = L, edx = FUNC*; **final** state after Postspill and window stealing |

### Mid-function points

| VA | len | what is observable |
|---|---|---|
| 0x1006650f (NColor, after Prioritize) | 6 | eax = 0-terminated NCOLLIFETIME* array in processing order; L+0x48 = priority, L+4 = SYM |
| 0x100686b2 (PCR, after Useable) | 6 | eax = useable BV*; edi = L, esi = R; [esp+0x3c] = preference list head, [esp+0x44] = conflict tup#. Second displaced instruction is esp-relative |
| 0x10068751 (PCR, after DeterminePhysregAvail) | 6 | eax = chosen physreg SYM* or 0; esi = R, edi = L, ebx = useable BV, [esp+0x4c] = fullCover, [esp+0x20] = benefit. First displaced instruction is esp-relative |
| 0x10068877 (PCR, final store) | 7 | "region R of L got register": esi = R, edi = L, [esp+0x18] = physreg SYM* (stored to R+0x1c by the second displaced instruction). Is itself a branch target (from 0x10068d44), nothing inside |
| 0x10069635 (DPA order loop) | 5 | register number esi passed the useable test and is about to be checked; [ebp-0xc] = L, [ebp-0x44] = R. A register seen here and at none of the next four rows was rejected for availability / coverage |
| 0x10069750 (DPA) | 7 | physreg SYM* esi fully covers the region; edx = R's range list. Displaced `cmp` feeds a later `jae`: keep flags |
| 0x100699d0 (DPA) | 5 | esi becomes fallback C (scratch reg freed too recently), ecx = gap |
| 0x10069a35 (DPA) | 5 | esi becomes fallback B (another lifetime's preference range overlaps) |
| 0x100699fb (DPA) | 5 | esi becomes fallback A (ymm6..15, or callee-saved with a preference-range conflict) |
| 0x1006988e (DPA common exit) | 7 | eax = result; [ebp-0xc] = L, [ebp-0x44] = R, *[ebp+0x18] = fullCover. Displaces the `ret 0x18` |
| 0x1006a348 (BestCoverage) | 6 | per register: ecx = best cluster score (0x80000000 = none), [ebp-0x20] = physreg SYM*, [ebp-0x98] = best so far, [ebp-0x38] = L, [ebp-0x7c] = R |
| 0x1006aeb3 (BestCoverage) | 9 | new best: ecx = score, edx = cluster, [ebp-0x20] = physreg SYM* |
| 0x100c69ca (TryToImprove exit) | 6 | eax = stolen physreg SYM* or 0. Displaces the `ret 0x14` |
| 0x100d7b53 (NColBuildInfo, a REGSET leaf) | 7 | esi = the kind-0xa leaf (+0x24 its register-set sparse BV: {head, pool}, nodes {base, low, high, next}); [esp+0xc] = tup#, [esp+0x1c] = Tuple*. Second displaced instruction is esp-relative. `c2ncol.REGSET_HOOK` (12.4) |

Avoid: 0x100697bf (the "next register" join in DPA) -- 0x100697c2 is a branch target inside; and
0x1006988c -- 0x1006988e is a branch target inside.

Cheapest complete trace for rasim: 0x1006650f (order + priorities), 0x10068751 (first choice per
region with the useable set), 0x10068877 (register per region), 0x100ca950 (registers given back),
0x10066600 (final regions per lifetime).

---------------------------------------------------------------------------------------------------

## 8. Not read / open points

* `NColBuildInfo` (5 KB) and `NColDoLiveness` (3 KB) were read only at their heads and at the sites
  that write the fields listed; how availability ranges are derived from calls and fixed-register
  instructions (`NColStartAvailability`, `NColTerminateAvailability`) was not read.
* `NColBestCoverage`: cluster growth across blocks is described from a partial reading; the exact
  cost of entry/exit edges is not pinned down.
* `NColSearchForRegionWithGap`: victim cost function not read past the head.
* `NColBuildSpillAvoid` / TLS+0xf688 and `NColSelectPhysregToMakeAvail`: only callee lists seen.
* `NColSubsumption`, `NColFoldTooCheap`, `NColCanon`, `NColCalcFixupCodeForLifetime`: described from
  names and callee lists.
* Opcode numbers 0x114, 0x13b, 0x4fe, 0x501, 0x595 are not identified (0x114 is treated like LEA by
  `NColMdLeaPref`, 0x13b like a plain move with memory operands).
* The writer(s) of physreg flag P+9 & 0x10 other than `NColBuildInfo` and `NColRewriteCandidate` were
  searched only inside the functions dumped in `t/`.

---------------------------------------------------------------------------------------------------

## 9. The model against traces (tools/ncolsim.py, 2026-11-20)

Input: `tools/c2ncol.py SRC --export FILE -- FLAGS` (every function's NColor run: candidates in processing
order with appearances, ranges, own preference window, tied physreg, per region the preference list, the
anti-preference set and the useable register set; after NColPrioritizeCandidates the liveness arrays, the
initial availability lists of rax..r15 and the preference-window lists). Corpus: zlib 1.2.1 (12 files, 97
functions, 3189 candidates) at /O2; fixtures in tests/data/ncol (tests/run_tests.py `ncolsim_tests`).

Tracing note: c2 17.00 generates code on worker threads (WorkerThreadCount, 4 by default; 1 only under PGO or
CLR). c2trace.dll serializes its handlers, keeps frames per thread and writes `# thread N` when the logging
thread changes; parsers keep one context per thread.

**Priority** (`ncolsim.py prio`): 3159 of 3189 as traced, 1044 of 1053 globals. Measured refinements of section
2: a block counts per range (appearances of that range only); a hole between two ranges inside one block costs
that block's `-live` once more. Misses: locals off by 1 or 2 (lifetimes whose range grew after prioritization,
e.g. by coalescing) and globals with an appearance listed twice for one tuple.

**Register choice** (`ncolsim.py replay`), given the traced order and what the trace assigned before: of 2669
integer candidates that got one register for their whole range, **2585 (96.9%)** get the model's register. The
walk, in this order of refinement (each measured):

| step | correct |
|---|---|
| default order rax rcx rdx r8 r9 r10 r11 rbx rdi rsi rbp r14 r15 r12 r13, first covering register | 2261 |
| preferences first (the region's preference list, a register only if it covers) | 2390 |
| scratch register free < 1 tuple before the region: fallback, not taken | 2461 |
| another lifetime's preference window overlapping the first range: fallback A (callee-saved) / B (scratch); end: C, B, A | 2547 |
| anti-preference (memory bases avoid r12 / rbp, r13: last in the order); tied register by master | 2554 |
| a window stops counting when its owner has been processed; overlap must be more than a shared end tuple | 2573 |
| only registers of the region's useable set (rbp is not useable in an EH frame) | 2585 |

**xmm candidates.** The xmm family's lists are not kept under xmmN: a float candidate's useable set is fxmm0s..15s
(336..351), a double's fxmm0l..15l (320..335), and preference windows are kept per master, ymm0..15 (112..127).
The order is numeric; a scratch xmm register needs a gap of 3 tuples; xmm6..15 are callee-saved. With those
banks: 159 of 160 xmm candidates of tests/data/ncol (fp.cpp: vector math, calls, a loop) and 21 of 21 in zlib.
**Whole run** (`ncolsim.py sim`: the model's own order inside the traced buckets, its own choices, from the traced
initial state only): 2549 of 2690 zlib candidates, 283 of 291 of the fixtures.

**What-if** (`ncolsim.py whatif EXPORT --func F EDIT..`): `+app:VAR:B[:N]` / `-app:VAR:B[:N]` (appearances of VAR
in block B, as `c2ncol.py --why` numbers blocks), `prio:VAR=P`; prints every candidate's priority and register
before and after. Ties are broken by the lifetime index (exported), as NColColPriorityCompare does.

Open: a value copied into another lifetime that the trace shows in the same register over the same tuples
(coalesced copies: NColSubsumption / NColCombineLifetimes, not modelled), the boundary rules of 4.3 step 3,
split regions (NColBestCoverage), xmm candidates, and computing the availability and liveness without a trace.

---------------------------------------------------------------------------------------------------

## 10. cl 17.00 x64: the source levers (realize, rwsweep, helpersweep), 2026-11-21

`tools/realize.py`, `tools/rwsweep.py` and `tools/helpersweep.py` switch to NColor when the build table says
`"arch": "x64"` and has the `ncol` hooks (`ncolsim.is_x64()`; nothing is keyed on a build name). On the four x86
builds their code paths are unchanged. Under x64 every variant is compiled with the hooks of `c2ncol.py --export`
plus the IL at NColor's entry (`ncolsim.trace_hooks`: the `B` lines number the blocks as NColBlockInfo does, the
line words give block -> source line), one compile at a time (wine), and read with the helpers of ncolsim.py:

| helper | what it does |
|---|---|
| `NRun(src, func, cl, keep)` | one traced compile: export record, listing, block -> source lines |
| `run_delta(a, b)` | what changed in NColor's inputs between two runs, as edits: `+app:V:B:N` / `-app:V:B:N` (appearances per block), `index:V=K` (lifetime-index order of named candidates), `live:B:+K` (candidates live in block B); flags: block count changed (no edits then), ranges moved, new / gone candidates |
| `predict(f, edits)` | the registers after edits, from the traced start (`simulate` with the edited priorities; `before:A:B` / `index:V=K` reorder ties, `live:B:+K` changes live[b] and the maximum) |
| `need(f, want)` | the edits that give `want = {V: reg}`: singles `+app` / `-app` (N = 1..4) of every named global in every block it is live in and `before:A:B` for equal priorities, then pairs; fewest appearances first |
| `gap(f, A, B)` | prio(B) - prio(A) with the index tie folded in (<= 0: A is processed first, same bucket) |

### 10.1 What the x86 lever classes become

| x86 lever (Color) | x64 (NColor) | how it is predicted |
|---|---|---|
| references per block (`ref` / `unref`: Spillcosts' cost per block) | appearances per block: k of the global priority `((maxLive+4) - live) * k * 8 << 2*depth` | `+app:V:B:N` / `-app` |
| loop weight (block weight in Spillcosts) | zone depth: `<< 2*depth` per block (an appearance in a depth-1 block is worth 4) | inside `+app` (the block's depth) |
| P per block (`P:B:+1`, new candidates) | live[b], and maxLive: a candidate more in B lowers every global's per-appearance term there | `live:B:+K` (measured; need() does not propose it) |
| defOrd tie-break (statement swap, declaration order) | lifetime index (NColColPriorityCompare: ties to the lower index = the earlier first reference) | `before:A:B`, `index:V=K` |
| interference, copies, preferences | availability per register and the preference windows of copies to / from a physreg; scratch registers end at calls, so a value live across a call takes the first callee-saved register in order rbx rdi rsi rbp r14 r15 r12 r13 that its processing position leaves | the order walk of `simulate` (preferences from the traced export; a new copy partner is not modelled) |
| equal arms (`eqif`), tail / head duplication: merged after Color | the arms are still blocks at NColor and are merged later: code-neutral, but the block structure at allocation changes | not predicted (block count differs): measured only |

Locals (first and last tuple in one block) have priority nTuples - length and are processed before every global of
their class; an edit that adds a tuple inside a local's range moves all locals alike, and the tools flag it as
`ranges` without it mattering.

### 10.2 The tools on x64

- **realize.py** `edit SRC --func F EDIT..` with EDIT `+app:V:B[:N]`, `-app:V:B[:N]`, `ref:V:B` / `unref:V:B` (= one
  appearance), `before:A:B`: the constructs are rwsweep's families plus `eqif` (`if ((__int64)(V) & 1) { S } else
  { S }`) and `store` (`rz_keep(V);`, a store to a global: one appearance, one instruction of code), placed at the
  statements of block B's source lines (lines of B alone first; a line shared with another block is ranked by
  position). Each variant: verdict `exact` (the request and no other input change), `yes` (and others, listed),
  `blocks` (block structure changed), `no`; `pred(req)`: the registers ncolsim predicts for the request = traced
  (exact variants only); `pred(meas)`: the registers predicted from what the variant measurably did = traced.
  `--from-ncol --want-regs v=reg` (`--from-rasim` is read the same on x64): `need`'s answers, each unit edit
  through its constructs, then the realisations of an answer's units combined, stopping at a code-neutral variant
  with the wanted registers (FOUND; any variant compiled on the way counts, also one that changed blocks).
  `store` is used by `edit` and, with `--with-store`, by the pipeline (a diagnosis: never code-neutral).
  `measure BASE VARIANT --func F`: the inputs moved, registers, priorities, whether ncolsim explains them.
  `pairs` is not ported.
- **rwsweep.py**: the same families and variants; every single compiled; per single the measured edits, the
  flags, whether ncolsim's registers from those edits are the traced ones (`predicted ok / MISS`); combinations of
  the code-neutral singles predicted by adding their edits and simulating, ranked by `--want-regs` (new option) and
  then by the predicted gap of `--want A>B`, the best `--verify` compiled and compared with the prediction.
- **helpersweep.py**: its `hs_bind` sites, measured the same way (`rwsweep.sweep_x64`); `--want A>B` is NColor's
  order, `--want-regs` ranks combinations.

### 10.3 Measured (tests/data/ncol_levers/levers.cpp, /O2 /EHsc)

Benchmarks: `swap1` (p and s across calls with equal priorities: p first by lifetime index, rdi; wanted s=rdi),
`swap2` (the T16 swap: the pointer temporary p has more appearances in block 1, takes rbx; wanted s=rbx), `loop1`
(an induction pointer in a loop, t only outside it; wanted t=rsi), `four` (four values across calls; wanted u=rsi,
one place up), `hsw` (an EH frame, an inline helper taking member addresses; wanted k=rdi).

**The model on realised edits** (`rz_keep` stores: realize `edit` for `+app:V:B:1` of every named global and
block, one store each, 36 compiles): 25 realised the request exactly, and for all **25 of 25** the registers
ncolsim predicted for the request are the traced ones (11 of them with a register change of a named value, up to
four registers moved at once in `four`: `u:rbp->rdi v:rdi->rsi x:rsi->r14 w:r14->r15`). Predicted from what each
variant measurably did: **36 of 36**. The other 11 put the store in another block than asked (a line shared by two
blocks) or moved a local's range too. (One earlier run of the same compiles lost a variant to a trace value c2ncol
could not parse, `?`: the tools report such a variant as failed.)

**need**: the wanted change of each benchmark has answers (`swap2` s=rbx: `+app:s:2:1` first; one appearance
more in block 1 is not enough there, two are: block 1 has live 11 of maxLive 11, so its per-appearance term is
(15 - 11) * 8 = 32 against 88 in block 2). `loop1` t=rdi has none (rdi is held by a local), t=rsi has 10.

**Code-neutral realisations** (`realize --from-ncol`, `rwsweep`, `helpersweep`):

| benchmark, wanted | realize --from-ncol | rwsweep (families without touch / repair / recompute / byval) | helpersweep |
|---|---|---|---|
| swap1 s=rdi | FOUND: the declarations exchanged (`before:s:p`, index; predicted) | 88 singles: FOUND twice (declaration swap, predicted; a value copy of p, not predicted); 3 combinations verified, 3 of 3 as predicted | - |
| swap2 s=rbx | FOUND: `eqif s` at `g(s);` (blocks changed, measured) | 105 singles, NOT FOUND (no code-neutral single moves an appearance); 2 of 2 combinations as predicted | - |
| loop1 t=rsi | NOT FOUND (10 answers, 60 compiles: no construct realises `+app:t:3`) | 74 singles, NOT FOUND | - |
| four u=rsi | FOUND: `eqif v` (blocks changed) | 237 singles, NOT FOUND; 7 of 7 combinations as predicted | - |
| hsw k=rdi | FOUND: `eqif k` (blocks changed) | 83 singles, NOT FOUND; 8 of 8 combinations as predicted | 2 sites: one is `live:1:+1 live:3:+1` (dgap -8, predicted), NOT FOUND |

rwsweep singles over the five runs: 587 compiled, 34 moved an input or a register; ncolsim's registers from their
measured edits are the traced ones for 18 of 34, and for **5 of 5** of those whose inputs are all edits (the 16
misses all moved a global's range or added / removed a candidate: value copies `alias val`, a declaration moved
past a call). Combinations (code-neutral singles' edits added up): **20 of 20** verified as predicted.

Findings: almost every do-nothing construct is gone before NColor at /O2, as on x86 before Color: `(void)V;`,
empty inline helpers by value and by reference, `__assume`, `V = V;`, `hs_id` / `hs_cp` of a use, a local copy, a
reference alias, `V + V - V` (14 forms by hand on swap1: none moved an input; of rwsweep's 587 singles, 553 moved
nothing). An identity helper around a call result (`g(hs_id(h(p)))`) leaves one candidate live more in that
block (`live:B:+1`, code-neutral, predicted). What reaches NColor and
leaves the code alone is (1) the statement / declaration order (lifetime index: exactly predicted), (2) equal arms
and duplicated tails (the arms are blocks at NColor and are merged afterwards: they flip registers code-neutrally,
but change the block structure, so they are measured, not predicted), (3) in an EH function an inline helper's
bound reference (`hs_bind`): one candidate more live in its blocks (`live:B:+1`, the x86 frontend.md rule holds on
x64), predicted. An appearance edit `+app` proper has no code-neutral construct in these benchmarks: the store
realises it with one instruction and is the diagnosis (the original references V in B).

### 10.4 Not covered

- (Section 12.2 / 12.3: a variant's registers are now computed from its own IL, which covers block-structure changes,
  moved ranges and copies when the variant is compiled.) Edits that change the block structure (eqif, cflow, tail
  duplication) are not predicted from the base run; nor are moved ranges
  (a statement swap that lengthens a global, a use moved past a call), new copy partners and their preferences
  (an `alias val` copy: NColSubsumption is not modelled), or a new candidate's own register.
- need() proposes `+app` / `-app` / `before` only, not liveness or range edits; it searches singles and pairs.
- Block -> line: from the IL's line words; a statement that shares a source line with another block (one-line
  `if (c) { .. }`, a braceless loop body under its `for`) may land in the wrong block (the verdict says so).
- realize `pairs`, `--from slots|scratch`, key edits (`add:frame@..`) and the load levers are x86 only here;
  symkeys and slots have their own x64 paths (symkeys.md 10, slots.md 6).
- Offline test: `ncol_levers_tests` (tests/run_tests.py) replays the stores' fixture
  tests/data/ncol_levers/levers.json on any build.

---------------------------------------------------------------------------------------------------

## 11. Trace-free inputs (ncolsim.py ilin / ilprio / ilsim, 2026-11-21)

`c2ncol.py SRC --export F.json --il -- FLAGS` adds each function's IL as NColor numbered it: the whole tuple stream
dumped at the state probe (0x1006650f, after NColPrioritizeCandidates), with `T R` raw words of every tuple and
leaf (`C2TRACE_ILRAW=12`: a branch's code-address leaf +0x24 is the label SYM, a LABEL tuple's +0x2c the same SYM),
`T S` the first 11 words of every leaf's symbol (`C2TRACE_ILSYMW=11`, a new c2trace.dll setting: +0xc the root
SYM, +0x24 size, +0x28 offset) and memory leaves' PAS; and each candidate's SYM (`sym`, the root its leaves'
symbols point to). `ncolsim.il_inputs(f)` computes from that IL alone, `il_state(f, use)` substitutes the computed
inputs into the export record so that `prio`, `replay` and `simulate` run on them unchanged:

- **Tuple numbers** **[C, measured]**: every tuple of the stream counts (BLOCK, LABEL, pseudo and instruction
  tuples), from 1; blocks start at BLOCK tuples, block 0 the entry. (The tuple's +0x28 is not the number:
  NColDoLiveness keeps it in its own arrays, f650 / f654.)
- **Candidates and appearances**: REG leaves whose register field is empty (a leaf printed `phys=sym` is already a
  physical register: NColInitializeSyms' separate numbering), grouped by **root SYM** (a 32-bit view `t992` of
  the 64-bit `t1224`, the byte view `s14` of `s2` are one candidate); one appearance per operand (a read-modify-write
  appears twice, as traced).
- **Flow graph**: a branch's target is the block of the LABEL whose +0x2c is the leaf's +0x24; fall through
  unless the block ends in jmp / ret; a switch (no case list in the dump) goes to every labelled block no branch
  names.
- **Liveness and ranges** (NColBuildInfo's lists): backward liveness per block, pruned to where a definition
  reaches; per block a segment from the block's first tuple (live in) or the first appearance to the block's last
  tuple (live out) or the last appearance; a pure definition more than one tuple after the previous appearance
  starts a new range (the hole is dead); a byte / word write of a wider candidate is no definition for that
  purpose (it keeps the rest; a dword write zero-extends and counts as one); a block's segment continues the
  previous block's only when that block falls through into it and the candidate is live across.
- **Depth**: loop nesting from the flow graph (natural loops of back edges to a dominator, capped at 5) is
  blockinfo +0x28 exactly; the **zone depth** +0x2c the priorities use is the same except 0 for the blocks of a
  loop too large to be a hot zone (more than `ZONE_MAX_BLOCKS` = 100 blocks: in zlib only inflate's and
  inflateBack's state-machine loops, 279 and 200 blocks; the next largest has 35), not for their headers, and not
  for blocks of their inner loops (which keep the full depth).

Measured, zlib (97 functions, 3189 candidates, 2431 blocks without each function's end block, whose blockinfo the
state probe reads past the array):

| input from the IL | as traced |
|---|---|
| tuple numbering (nTuples) and the block of every tuple | 97 of 97 functions |
| appearances | 3133 of 3189 candidates |
| ranges | 3155 of 3189 |
| loop depth / zone depth per block | 2431 / 2431 of 2431 |
| live count per block (f69c) | not reproduced: about one block in three |

**Priorities** from the IL's appearances, ranges, blocks and zone depth (live counts as traced): **3182 of 3189**,
more than from the traced inputs (3159): the IL's lists are those at prioritization, the traced ones are read when
each candidate is processed, after coalescing grew some ranges and with an appearance listed twice for one tuple.
With the IL's live counts too: 2180. **Register choice** on the IL's inputs (availability as traced): replay 2567
of 2669 integer candidates (2585 from the traced inputs), 21 of 21 xmm; sim 2520 of 2690 (2549). Of the 18
replay cases lost, 15 have exactly their traced ranges (13 are crc32's unrolled `c$1`..`c$7`): what moves them is
the availability left by earlier candidates whose IL ranges differ **[I]**.
Fixtures (tests/data/ncol/il: c1, c2, sum; `ncolsim_il_tests`): appearances 63 / ranges 62 of 65, priorities 62
(64 traced), replay 53 of 54 and 9 of 9, sim 61 of 63 (as from the traced inputs).

**The live count** (`f69c[b]`, the `live` of the priority formula) **[C in part]**: NColBuildInfo's backward walk
starts each block's count at `SbvCount` of the live-out set (0x100d77fc), adds one each time a lifetime's bit is
newly set in the live set during the walk (0x100d7cb4, 0x100d7da5), and stores it when it reaches the block's BLOCK
tuple (0x100d761f; max in f698). The bit vector numbers more than the candidates: the lifetimes already in a
physical register (NColInitializeSyms' nPhys numbering) are in it, and a block's empty `op577` pseudo-tuples raise
the count too; no rule tried (candidates and tied lifetimes touching the block, live-out plus births with either
operand order, with or without the flags temporaries and the op577 tuples) reproduces more than 44% of the traced
counts, so `live` / maxLive stay read from the trace (`IL_USE` leaves them out; `ilprio+live` measures them).

**Still read from the trace**: the live counts and maxLive; the initial availability lists of rax..r15 and the
fxmm banks (`state.avail`, what is free when before allocation: fixed-register operands, calls, the incoming
arguments); the preference windows of copies to / from a physreg (f6a0) and each region's preference list, the
useable and anti-preference sets, the tied register and the lifetime index; the candidate order's buckets (the
traced order). Availability without a trace (what is free at each point given earlier choices) needs those initial
lists from the IL: the tied lifetimes `il_inputs` already computes (`tied`: ranges of operands with a preassigned
register) plus the call clobber sets are the obvious input, not yet compared with `state.avail`.

Commands: `ncolsim.py ilin EXPORT..` (the inputs against the trace), `ilprio` / `ilprio+live`, `ilsim` (replay
and sim on the IL's inputs); `prio`, `replay`, `sim` and `whatif` are unchanged (an `--il` export also
carries everything they read).

The rest of these inputs: section 12.

---------------------------------------------------------------------------------------------------

## 12. All of NColor's inputs from the IL (ncolsim.py ilmodel / ilfull, il_predict; T17 1, 6, 7; 2026-11-21)

`c2ncol.py --export --il` now also dumps the IL at **NColSubsumption's entry** (0x1009ab40, `c2ncol.PRE_HOOK`; the
record's `il_pre`). That is the stream NColInitializeSyms and NColBuildInfo saw: the IL of section 11 is read after
NColSubsumption and NColFoldTooCheap, which replace each removed copy / folded definition by an empty `op577` in the
same slot (NColTupRemove 0x100b98f0) and rename the absorbed lifetime's leaves to views of the survivor
(NColCombineLifetimes). The tuple numbering is the same in both dumps. Read statically (work/port1700 helpers),
measured on zlib 1.2.1 at /O2 (97 functions, 3189 candidates, re-exported with `il_pre`) and on tests/data/ncol/ilpre
(c1, c2, sum; `ncolsim_ilmodel_tests`).

| input | rule **[C] unless marked** | zlib | fixtures |
|---|---|---|---|
| lifetime index (`il_index`) | NColInitializeSyms 0x1017a350: tuples forward, destinations then sources, REG leaves by root; an untied leaf numbers its root as a candidate, a tied one (preassigned register, root not a physreg) in the nPhys sequence, in the same field SYM+0x38: first role seen wins. The gaps of the traced indices are lifetimes absorbed later | 3189 / 3189 | 65 / 65 |
| live count per block, maxLive (`il_live`) | NColBuildInfo 0x100d7220, backward: liveness of the *global* lifetimes only (candidates and tied lifetimes whose first and last appearance are in different blocks), pruned by NColDoLiveness' second fixpoint (in = live-in & U pred out; out = live-out & (appears \| in - defs)); per block the count starts at \|out\| and adds one per source whose bit is not set (a birth: a value used again after it died counts twice); a definition never adds (sets, then clears the bit unless the tuple reads it or it is a partial view); block 0 stores 0 | 2528 / 2528 blocks | 65 / 65 |
| integer availability (`il_avail`) | NColInitializeAvailability / NColStartAvailability / NColTerminateAvailability, backward: a register is available at a defining tuple and at the last use of the value defined there, busy strictly between; a def or clobber with no tied use later splits `[.., t] / [t+1, ..]`; a register the clobbering tuple reads is not cut; calls clobber rax rcx rdx r8..r11 **[I]**; `[0,0]` / `[n,n]` are sentinels. With the REGSET probe (12.4) a call clobbers what its kind-0xa leaf says **[C]** | 1442 / 1455 lists (92 of 97 functions all exact); with the probe **1455 / 1455** | 60 / 60 |
| xmm availability (`il_xavail`, 12.4) | the same rules over xmm0..15 by master; both banks (fxmm l 320.., s 336..) are one list per register; the upper halves a call lists (ymm0h, ymm6h..15h) do not touch them; a call's clobbers: the REGSET probe, else the Win64 volatiles xmm0..5 in a function with floating point and all 16 in one without **[I]** | 1552 / 1552 with the probe (1376 without) | 800 / 800 |
| tied register, window (`il_tied_pre`) | set in NColBuildInfo by both sites as overwrites in a backward walk: the qualifying copy with the smallest tuple number; `mov x <- phys` (any def) or `mov phys <- x` where the use ends one of x's ranges; NColMdIsCopyInstr: mov, movaps / movapd / movdqa / movdqu, movsd / movss (scalar float types); never movzx / movsx / lea. NColCombineLifetimes gives the survivor the absorbed tie if it has none; the window is the range that begins (or ends) at the copy, clipped to 15 tuples. As read (12.5, `tieread`): the first site ties every candidate a copy to a physreg reads where its range ends, an address too; a FoldTooCheap temporary's tie is not carried | 3182 / 3181 of 3189; read **3189 / 3189** | 65 / 64; read 65 / 65 |
| region preference list (`il_pref_sources`) | the destination of a mov at the last use, then the source of every mov defining the candidate, each at the head; partners resolved with the registers given so far (simulate's `prefs_of`). Adding loads at the last use or NColMdLeaPref's lea base measured worse. As read (12.4, `prefread`): loads and leas count, the partner's register *number* must be in the useable set; 12.5: a folded-in use counts, the partner's region covering the tuple | 3042 / 3189; read **3187**; 12.5 **3189** | 63 / 65; read 65 |
| useable set (`il_useable`) | NColMdGetInitialRegisters: every integer register but rsp (the masters lack rbp; NColMdPerFuncInit adds it back unless a frame register or a reservation needs it); rbp out when the function has a try block (OPEHTRY; 12.4: FUNC+0xe4 & 0x4801000 / +0xe8 & 0x100 **[C]**, set for EH frames) or NColMdPerFuncInit reserves it as a frame base (0x1009271d: TLS+0x1196c >= 6 * 24 when favoring speed; the score NColCanon accumulates, fitted as 6 per stack-symbol leaf + 1 per kind-5 memory operand **[I]**: inflate, inflateBack, inflate_table; read in 12.4, `rbpread`) | 3189 / 3189 | 65 / 65 |
| anti-preference (`il_anti`) | NColMdAddToPreferences 0x10069010: the memory operand of a mov (first destination, else first source) or an lea source whose base is the candidate: no index -> r12, no displacement -> rbp / r13, both -> all three; a union over the appearances. As read (12.4, `antiread`): the base must be the candidate's own leaf in that tuple; with the folds in processing order and their rewritten addresses (12.5) | 3147 / 3189; read **3171**; 12.5 **3189** | 64 / 65; read 65 |
| lists and order (`il_lists`, `order_of`) | below | 97 / 97 functions | 4 / 4 |

**Lists [C].** class = (SYM type >> 12) & 0x1f from the IL's `T S` line of the candidate's root; int when `(1 << class)
& 0x1e`. **Class 6** (a small struct held whole in an integer register: type 0x6004 / 0x6008) gets integer registers
(NColMdGetRegOrder) but is *not* int for the lists: it is in list 0 / 1 with the xmm candidates, processed before every
integer candidate. Local / global from the IL lifetime at prioritization (the traced first / last are read later, after
coalescing grew some). Lists 4 / 5: one definition whose sources are all constants, symbol or code addresses (leaf kinds
7, 3, 4: `NColUsesOnlyConsts`). NColMdAdjustPriority: with a loop in the function the int-global of highest priority
above 16 * max(1, highest int-local priority) goes to the head of list 2; list 3 is built by pushing on its head and
scanned with `>`, so a tie goes to the *higher* lifetime index; not in a function with a try block (c2.cpp's f2: FUNC+0xe4
& 0x80 **[I]**; 12.4: the flag is "uses floating point" **[C]**, f2 has a double; `fpflag`). With traced priorities and
indices this gives the traced order of every zlib function.

**Numbers** (`ncolsim.py ilfull-fit`, the fitted rules: every input above from the IL, xmm availability still
traced; `ilfull-read` adds IL_READ, 12.4; `ilfull`, the default since 12.5, adds IL_FOLD):

| | priorities | replay int | replay xmm | sim |
|---|---|---|---|---|
| traced inputs (section 9) | 3159 / 3189 | 2585 / 2669 | 21 / 21 | 2549 / 2690 |
| section 11 (appearances, ranges, blocks, depth from the IL) | 3182 | 2567 | 21 | 2520 |
| all from the IL (`ilfull`) | 3182 | 2547 | 21 | 2498 |
| the same with the traced preference lists | 3182 | 2563 | 21 | 2514 |
| the same, lifetimes coalesced by the model from `il_pre` (`subsume`) | 3099 | 2538 | 21 | 2366 |
| **IL_READ (`ilfull`, 12.4): + xmm banks and the calls' REGSET sets, preferences, anti-preference, float flag, rbp score, load folds** | 3182 | **2580** | 21 (xmm availability from the IL) | **2561** |
| IL_READ with `subsume` | 3099 | 2574 | 21 | 2427 |
| **IL_FOLD (`ilfull`, 12.5): + the fold cost test in processing order, ties, window lists, partial definitions, float flag and frame score as read** | **3187** | **2585** (the traced inputs' 84 misses, the same ones) | 21 | **2565** |
| IL_FOLD with IL_SUB (`ilfull-sub`: the lifetimes from `il_pre`, il_subsume + NColFoldTooCheap) | **3189** | 2585 | 21 | 2565 |

Fixtures: replay 52 / 54 and 9 / 9, sim 60 / 63 (traced inputs: 53 / 54, 9 / 9, 61 / 63); with IL_READ 53 / 54, 9 / 9,
61 / 63. What the fitted path lacked (the preference lists, the clobber set of a call to a function of the same unit,
the xmm availability banks) is read in 12.4; what still remains is listed there.

### 12.1 The list lever: a one-member struct is allocated before every int candidate

tests/data/ncol/bucket/scope.cpp (our own source, /O2 /GS- /EHsc, throw() callees): `plain` saves a TLS mode word in
`int saved`, does work on a pointer p across calls and restores it; saved is an int global of low priority (78 against
p's 544 and r's 251) and gets the fourth callee-saved register, rbp. In `boxed` the same value is held in `struct Saved
{ int v; } s`, in `scoped` in a scope object (constructor saves and sets, destructor restores): **s is class 6, list 1**,
processed first, rbx; p moves to rdi, r to rsi, the code is otherwise the same. (Not, as an earlier guess had it, the
int-local list: by priority alone s would come last.) The scope object is not needed; a one-member struct does it.

- `ncolsim.py whatif EXPORT --func F bucket:V=aggr|local|global|const|N`: V moved to another list (local / global keep
  the class, aggr = list 0 / 1 by V's locality, const 4 / 5; `local` on a global also takes the local priority). On
  plain, `bucket:saved=aggr` predicts exactly boxed's traced registers (every candidate).
- `lists_of(f)`: from the IL; for realize's runs (NRun now keeps the IL of NColor's entry, `il_fn`) the type by SYM
  and, as NColCanon may give a candidate a new SYM, by name. `run_delta` reports a list change as `bucket:V=N`.
- realize (x64) construct **box**: `T V = E;` -> `struct { T rz_v; } V = { E };`, every use `V.rz_v`, selected for
  `bucket:` requests. `realize.py edit scope.cpp --func plain bucket:saved=aggr`: verdict exact, the registers as
  predicted, the same instructions with other registers.

### 12.2 Edits that change the block structure (T17 6)

`il_predict(f)`: the registers from a compile's own IL (il_model_state + simulate). Instead of editing the base run's
inputs, NColor's inputs are recomputed for the edited block graph. realize / rwsweep runs now carry `il` and `il_pre`
(`trace_hooks`), so a variant whose blocks changed is judged too (`x64_inputs`: "the block structure (ncolsim from the
variant's IL: as traced)"; `realize --from-ncol levers.cpp --func swap2 --want-regs s=rbx`: FOUND with `eqif s`, as
traced). Measured on equal-arms ifs `if ((__int64)(V) & 1) { S } else { S }` around every `g(..);` after each local's
definition in tests/data/ncol_levers/levers.cpp (57 variants in one compile, all with a changed block structure, 38 with
a register change): **57 of 57** from the variant's IL (56 with the `subsume` model); from the base run's edits: none
(the block structure differs). Fixture tests/data/ncol/eqif (20 variants with a register change, gzipped). Not done:
predicting the new graph from the base's IL without compiling the variant.

### 12.3 Copy coalescing (T17 7)

NColSubsumption 0x1009ab40 [C, read statically]: lifetimes in index order; for each def that is a copy `mov L <- S`
(NColMdIsCopyInstr) writing the whole L from an untied S at offset 0 with equal L+0x50: rejected when L is defined inside
S's ranges (NColCandModifiedInLife: a def at a range end is harmless when it is the copy L <- S itself or a tuple that
reads S where S dies) or S inside L's (unless S has one use and one def in the copy's block and L is not used inside
S); the wider survives, at equal size S when its leaf is a view or L is a temporary (SYM+8 == 3, +0x18 == 0) and S is
not, else L; the copy is removed, NColCombineLifetimes merges uses / defs / appearances (duplicates kept), unions the
ranges (adjacent ones joined in one block or across a successor edge), carries the tie, and zeroes the dead lifetime
(empty appearance list: NColPrioritizeCandidates skips it); when L survives its defs are scanned again. `il_subsume`
models it on `il_pre`: of the 3189 surviving zlib candidates 3102 get the traced appearances and 3137 the IL's ranges
(564 changed by coalescing).

Measured on the rwsweep miss class of 10.3 (value copies): `T V_a = V;` before each top-level statement that still
uses V, later uses renamed (levers.cpp, 62 variants in one compile; 20 moved a range or added / removed a candidate, 10
changed a register): from the base run's measured edits 52 of 62 (10 of the 20); from the variant's IL **62 of 62** (56
with `subsume`). Fixture tests/data/ncol/copies (12 variants).

### 12.4 The rest read (IL_READ; 2026-11-21)

`ncolsim.py ilmodel` / `ilfull` now run with `IL_READ` = (`xavail`, `regsets`, `prefread`, `fpflag`, `antiread`,
`rbpread`, `loadfold`) on top of IL_USE + IL_MODEL; `ilmodel-fit` / `ilfull-fit` and the functions' defaults
(`cmd_ilfull(fns)`, `il_model_state(f)`, `il_predict(f)`: IL_MODEL) are unchanged, so earlier numbers and tests stand.
Measured on zlib 1.2.1 /O2 re-exported with the new probe (97 functions, 3189 candidates; the traced-input numbers of the
new exports are the old ones: 3159 / 2585 / 21 / 2549) and on new fixtures: tests/data/ncol/xmm (xmm.c, our float source:
xmm parameters, extern and static callees, conversions, loops; fp.cpp), tests/data/ncol/calls (calls.c: static noinline
helpers of the same unit), tests/data/ncol/flags (flags.cpp /O2 /EHsc: try / destructor / float combinations around an
adler32-like loop); `ncolsim_ilread_tests`.

**Calls' clobber sets: the REGSET probe [C].** NColBuildInfo's backward walk (0x100d7b4b) takes every kind-0xa leaf
(OPREGSET 0x50b, `regset:0` in the dump), walks its register-set sparse BV (+0x24) with SbvNextEx, marks each register
used (P+9 |= 0x10) and ORs `NColPhysregOverlapsBvs[reg]` into the set that NColStartAvailability splits. `c2ncol.py
--export --il` hooks 0x100d7b53 (`REGSET_HOOK`, defined in the tool like PRE_HOOK: `mov ecx, [esi+0x24]; lea edx,
[esp+0x38]`, 7 bytes, nothing branches inside) and logs the tuple, its number and the BV's nodes; the record gets
`regsets` ([] for a function without such a leaf). What the sets say: a call to another unit clobbers rax rcx rdx r8..r11
(minus the return register, a destination of its own) and **ymm0..15 when the caller has no floating point, ymm0/1..5
when it has** (zlib, calls.c: all 16; xmm.c, fp.cpp: 1..5); a static callee compiled before clobbers only what it uses
(calls.c: `rax`, `rcx`, `rcx rdx`, `rcx r8 r9 r10 r11`; s_sq clobbers nothing: no leaf at all). `il_regsets(f)` maps them
to masters (ymmN -> xmmN, upper halves dropped); `il_avail(.., regsets=)` uses them instead of CALL_CLOBBER. zlib integer
lists **1455 / 1455** (1442 without); calls.c 180 / 180 (146 without): replay 70 / 74 and sim 61 / 74 with them, 54 and
47 without (traced inputs 70 / 59).

**xmm availability banks** (`il_xavail`) **[C for the rules, measured]**: il_avail's walk over xmm0..15 by master (tied
`fxmm0s=t..` leaves, physreg destinations, the calls' sets); one list per register for both banks (320 + k and 336 + k
are always equal); the upper halves a call lists among its destinations (ymm0h, ymm6h..15h: the Win64 volatile upper
halves, numbers 128..143 and 196..243) do not overlap the low 64 bits and leave the banks alone. zlib **1552 / 1552**
(every function's 16; 1376 with the fallback below), xmm.c + fp.cpp 800 / 800 (772). Without the probe: the Win64
volatiles xmm0..5 in a function that uses floating point, all 16 otherwise **[I]**. Replay / sim with the IL's banks
equal those with the traced ones (zlib 21 / 21; xmm.c 63 / 63, sim 87 / 87; fp.cpp 149 / 151, the traced inputs 150).

**Preference lists as read** (`prefread`, `il_pref_sources_read`) **[C]**: NColDeterminePhysregUseable 0x10068eb8..: at
the *last use* (the last node of the region's use list) NColAddToPreferences(first destination) when the tuple is a copy
by NColMdIsCopyInstr 0x10069170 (opcodes 0x13b mov, 0x13c / 0x13d movapd / movaps, 0x142 / 0x143 movdqa / movdqu,
0x326.. the v-forms; 0x154 movsdx, 0x159 movss, 0x33e / 0x342 only with a scalar float tuple type 0x5000) **whatever its
source**, so a load through the candidate counts, or an lea whose source is a memory leaf of opcode 0x4fe (OPBASED) /
0x501 (OPSCALED); then for every definition: a copy's first source at the head, or an lea's NColMdLeaPref operand
(0x101a3080: +0x30 of an OPBASED source, +0x34 of an OPSCALED one; OPINDEXED / OPLOCALSCALED give none) appended.
NColAddToPreferences 0x100691f0 takes a REG operand only, a physreg as it is, a candidate by the register of its region
covering the tuple, adds it only if its **register number** is in the useable BV (a dword partner's ecx is not in a qword
candidate's set; an fxmm0l not in a float's), and never twice: a register already in the list keeps its place (the list is
modelled as the sequence of head / tail calls). zlib **3187 / 3189** (3042 fitted); the two left: deflateCopy ss$1 (a
load's destination whose region covering the tuple has no register) and inflate_table mask$1 (a partner renamed by the
coalescing). Replay +16, sim +16. The xmm last-use copy (`movsd fxmm0l <- x`) is what the fitted rule missed in fp.cpp.

**Anti-preference as read** (`antiread`, `il_anti_read`) **[C]**: NColMdAddToPreferences 0x10069010 is called per
appearance with the candidate's own leaf in that tuple (a use: the first source REG leaf of the lifetime; the set is
stored on that leaf's SYM +0x44, so a view's never reaches the candidate). The memory operand's base field (+0x30)
must be **that leaf**: mov (0x13b): the first destination if it is memory (kind 5 / 6), else the first source; lea
(0x114): the source if it is kind 5 and its base is the leaf, or it has no base and the leaf is an index of scale 2
(`[i*2]` = `[i+i]`). No index (+0x34) -> r12; no displacement (+0x38, the numeric displacement) -> rbp / r13. The fitted
rule matched the base by name (another lifetime's leaf of that name, an index-only `[i*4]` taken for a base). With
C2TRACE_ILRAW=16 the fields are raw words (+0x30 / +0x34 / +0x38 are words 12..14 and point at the leaves); with the
default 12 they come from the text (same result on zlib). zlib **3171 / 3189** (3147); the 18 left are candidates whose
lists the folds below changed before they were processed (an lea temporary of the candidate folded away: its tuple
leaves the list, its uses come in with another base) and deflateParams t717 (an `[t717+t797*1]` load that should give
rbp / r13 and does not; scale 1 is the only feature that sets it apart, not read). No replay change on zlib.

**NColMdAdjustPriority's flag** (`fpflag`, `il_uses_float`) **[C]**: FUNC+0xe4 & 0x80 is written by OtlScanFuncLeaves
0x101df4e0 for any leaf of type class 5 (float / double), and by RdrFinalCleanup / PtInlProcessInstr (not read). It has
nothing to do with EH: flags.cpp's `try_int` (try, no float) has its global moved, `float_int` (float, no EH) not, f2 of
c2.cpp has both. The fitted try rule got xmm.c / fp.cpp's float loops wrong (order 18 of 25 functions; 24 with the flag;
sim xmm.c 78 -> 84 of 87, fp.cpp 195 -> 216 of 228 by the flag alone, 87 and 222 with all of IL_READ).
The flag also
changes NColFoldTooCheap's lea folds (NColMdNoFoldBenefit counts one register less in a function with floating point:
a two-register `lea` used only as an address is folded into its uses; the x64 TLS address `[base+secrel]` in two
registers, docs/c2/tls17.md), and it is set by float leaves the optimiser removes before NColor, which
`il_uses_float` cannot see in NColor's IL **[C]**.

**EH and rbp** **[C for the test, I for which constructs set the bits]**: NColMdPerFuncInit adds rbp (NColMdSetAvailReg(6),
0x1009261f) and takes it out again (NColMdClrAvailReg(6), 0x1009286b) when FUNC+0xe4 & 0x4801000 or FUNC+0xe8 & 0x100.
0x4000000 is written by RgnMdPrepFunclet (a funclet: a catch / finally handler) and by OtlScanTuplist for opcode 0x56b;
0x1000 by RdrGetTuples and WvmLoIntrin. Measured: try blocks take rbp out, a destructor-only unwind does not (flags.cpp
try_int / try_float against dtor_int), which is what il_has_try keeps doing.

**The rbp reservation score as read** (`rbpread`, `il_rbp_score`) **[C, one I]**: NColCanon 0x10090040..0x10090147 /
0x1009049b, per instruction tuple, every source and destination leaf: a memory leaf (kind 5 / 6) whose +0x2c SYM is of
kind 3..6 adds 6; else a leaf of opcode 0x4fc (OPLOCAL, a symbol leaf `s7:1008`) or 0x500 (OPLOCALSCALED) adds 6 when its
root SYM has +9 bit 2, else 1. NColMdPerFuncInit (0x10092788..0x100927d6) reserves rbp when the score is >= 6 * 24 (speed;
6 * 12 otherwise), rbp is in the initial set and not yet used, and the frame registers are the defaults. The +0x2c SYM's
kind is in the dump only when that SYM is also some leaf's symbol; counting an unknown one as 6 **[I]** gives inflateBack
577, inflate 204, inflate_table 186 (559, 186, 96 counting them 0: only then does inflate_table reach 144, as its traced
useable sets say it does), the next 89 (spring, fp.cpp) and 73 (compress2): the same three functions as the fit, now from
the code.

**Folds while allocating (`loadfold`; `addrfold` optional)** **[measured; the places C, the cost test not read]**: the
traced lists are those of NColProcessCandidate's entry, after earlier candidates were folded (NColFoldAwayRegion,
NColDoLoadFold) and their address went into their uses; the priorities were computed before (so `prio_of` keeps them).
`il_load_folds`: `mov T <- [mem]` as T's only definition, one use in the same block that can take the memory operand in
T's place (add / sub / xor / and / or / adc / sbb reading T second into its own first source, movsx / movzx / test reading
T first): the memory's base and index lifetimes appear at the use and live until it (crc32's unrolled `c$n` chains: the
18 range misses of crc32_little / crc32_big). `il_addr_folds`: `lea T <- [S + disp]` (OPBASED) as T's only definition,
every use of T an address component: S loses the lea, gains T's uses (zlib: 550 of 550 such temps folded, none of the 128
with a plain register use). zlib replay **2565 -> 2580**, sim **2518 -> 2561**; addrfold adds nothing to either (it moves
the apps match 3140 -> 3151 of 3189).

**zlib, step by step** (`ncolsim.py ilfull` with the keys added in turn; prio 3182 throughout):

| IL_MODEL + | replay int | xmm | sim |
|---|---|---|---|
| (fitted) | 2547 / 2669 | 21 / 21 | 2498 / 2690 |
| regsets | 2549 | 21 | 2502 |
| xavail | 2549 | 21 (from the IL) | 2502 |
| prefread | 2565 | 21 | 2518 |
| fpflag, antiread, rbpread | 2565 | 21 | 2518 |
| loadfold (= IL_READ) | **2580** | 21 | **2561** |
| traced inputs | 2585 | 21 | 2549 |

Of the replay cases, 6 are missed from the IL that the traced inputs get (deflate t793, gen_bitlen t726 / t785, get_byte
t188, gzread t774 / t775: ranges the folds and the coalescing leave different) and 1 the other way. Substituting a traced
input for the IL's moves replay by at most 2 (ranges: 2582 / 2561; before the load folds the traced ranges were worth 18). Fixtures with
IL_READ / fitted: xmm.c + fp.cpp replay 99 / 101 and 212 / 214, sim 309 / 315 (97, 210, 273); calls.c 70 / 74, sim 61 / 74
(54, 47); flags.cpp 67 / 70, 4 / 4, 71 / 74 (both). il_predict on the eqif / copies variants: 20 / 20 and 12 / 12 either
way.

**il_subsume (the apps 97%).** The traced appearance lists it was compared with are read at processing, after the folds
above: of the 123 survivors whose lists differ from il_subsume's, 97 differ exactly by folded single-def temporaries of
the candidate (its tuple gone, the temporary's uses in), and the rest mostly by several of them. Folds applied to its
lifetimes take the apps match from 3066 to 3137 of 3189 (ranges 3105 -> 3108); `subsume` with IL_READ: replay 2574, sim
2427 (its priorities, 3099, are the loss). NColSubsumption itself was not changed.

**Still not computed / not exact** (as of 12.4; what 12.5 closed: 12.5): the tie (3182 / 3189) and window (3181); 18 anti-preference sets (lists changed by
folds of earlier candidates, and one scale-1 load); 2 preference lists; the ranges of a few lifetimes at processing
(the 6 replay cases above); the cost test of NColFoldAwayRegion / NColDoLoadFold (the fold rules are measured shapes);
the kinds of some memory leaves' +0x2c SYMs in the rbp score; the writers of FUNC+0xe4 & 0x80 other than OtlScanFuncLeaves
and the source constructs behind FUNC+0xe4 & 0x4801000. The live counts, lists, index, availability (integer and xmm,
with the probe), useable sets and order are exact on zlib.

### 12.5 The fold cost test, ties and windows as read (IL_FOLD; 2026-11-21)

`ncolsim.py ilmodel` / `ilfull` and `il_predict` now run with **IL_FOLD** = (`partlive`, `defsplit`, `tieread`,
`fpread`, `rbpscore`, `foldcost`, `foldorder`, `foldext`, `foldaddr`, `pwlist`, `prefregion`) on top of IL_READ
(`ilmodel-read` / `ilfull-read`: IL_READ alone; the functions' own defaults, `cmd_ilmodel(fns)`, `cmd_ilfull(fns)`,
`il_model_state(f)`, are unchanged, so the earlier tests stand). `c2ncol.py --export --il` logs two more words: FUNC+0xe4
at NColor's entry (`fflags`, the FN hook's last spec `ecx>0xe4`) and NColMdPerFuncInit's inputs (`perfunc`: a probe at its
entry 0x100925b0, ecx = FUNC: FUNC+0xe4, FUNC+0xe8, the frame score TLS+0x1196c and TLS+0x1073c; `c2ncol.PERFUNC_HOOK`);
ncolsim's `trace_hooks` (NRun: realize / rwsweep runs) adds the REGSET and PERFUNC probes. Measured on zlib 1.2.1 /O2
re-exported with them (97 functions, 3189 candidates; the traced-input numbers unchanged: 3159 / 2585 / 21 / 2549) and on
new fixtures tests/data/ncol/fold (fold.c: load and two-register lea folds, a stack-array frame, a float leaf the optimizer
removes; tests/data/tls17/fp.cpp; flags.cpp /EHsc and fp.cpp re-exported with the probes; `ncolsim_fold_tests`).

**zlib, all of NColor's inputs from the IL** (`ilmodel`; IL_READ in brackets): order 97 / 97, tied register **3189**
(3182), window **3189** (3181), preference lists **3189** (3187), useable **3189** (3189), anti-preference **3189** (3155
with the load folds of 12.4, 3171 without them); priorities **3187** (3182), 3189 with IL_SUB; the lists at processing
(appearances / ranges against the traced ones, read when each candidate is processed) **3188 / 3186** (3140 / 3156),
**3188 / 3188** with IL_SUB (il_subsume with IL_READ's folds: 3080 / 3116); the preference-window lists 6208 / 6208 (every
register of every function, as the state probe reads them). Replay **2585 / 2669** (2580): exactly the 84 misses of the
traced inputs; xmm 21 / 21; sim **2565 / 2690** (2561; traced inputs 2549). Fixtures (IL_READ / IL_FOLD): ilpre window
64 / 65; xmm + fp.cpp preferences 315 / 316, replay xmm 212 / 213 of 214, sim 309 both; calls, flags, scope unchanged;
the fold fixtures (381 candidates) window 380 / 381, preferences 380 / 381, anti 379 / 381, replay 209 / 212 of 219 and
154 / 155 of 156, sim 360 / 363 of 375; il_predict on the eqif / copies variants 20 / 20 and 12 / 12 either way.

**The fold cost test** (`foldcost`: il_proc_folds, il_fold_too_cheap, il_nofold_benefit) **[C]**. Two places fold a
definition into its uses, both through NColMdNoFoldBenefit (0x100cb140; ecx = use, edx = def, the candidate's SYM, a
flag k), which returns what keeping the value in a register is worth at that use, 0x7fffffff (INF) when the use cannot
take the definition:

- *lea* (0x100cb229..0x100cb403): INF when the use reads T as a plain register (a source leaf without the
  address-component bit +0x1c & 0x80); else the use's memory operand holding T (the first kind-5 / 6 source with T as
  base or index, else the first destination): occ = T's places in it, regs = the lea address's base + index (+1 for
  OPLOCALSCALED 0x500), others = the use address's other registers (+1 for 0x500); **n = regs * occ + k + others, INF
  when n > 2**, when both addresses carry a scale (+0x1c & 0xf, log2) or both a symbol (+0x2c), when T occurs twice and
  the lea address has a scale or symbol, when T is a scaled index and the lea address has two registers or a symbol, or
  the displacements overflow 32 bits; else 0.
- *load* (`mov T <- [mem]`, kind 6; 0x100cb1c1..): INF when the use's first source is wider than the memory, else 1
  when NColMdCanReplaceCandWithMemWithAlignment accepts the use (not read: the shapes of 12.4, plus `cmp` with the other
  operand not memory, either order), else INF. Constants and symbol addresses take other paths (not modelled: they fold
  only themselves, and simulate reads which candidates got no register from the trace).

*NColFoldTooCheap* (0x10066040, after NColSubsumption, before the priorities; il_fold_too_cheap): lifetimes in index order;
one definition, or several CmpInstrs-identical ones, that NColMdIsFoldableDef (0x10066310: an lea of a kind-5 address
with a 64-bit type; mov / movap* / movdqa / movsd / movss from a non-register) accepts and NColCanRecalc (0x1009ae10:
one destination, REG sources untied and not redefined inside T's ranges, at most one memory source not written in T's
life) agrees with, at least one REG source and no kind-6 source, so an lea; and **every use finite** with **k = 1 when
favoring speed (TLS+0xdd08), without floating point (FUNC+0xe4 & 0x80) and TLS+0x1073c != 1, else 0** (the sum is not
compared). The definition becomes op577, every source gets T's uses in its use and appearance lists (NColInsertInNumList)
and T's ranges (NColUpdateLiveness: the union by overlap), T is zeroed. zlib: **474 / 474** of the leas il_pre has and il
lacks, none wrong; fixtures 68 / 68 and 67 / 67 (init_c and addr_fp: the flag from a removed float leaf, k = 0, the
two-register TLS lea folded as docs/c2/tls17.md has it). A zeroed lifetime loses its tie: NColCombineLifetimes' carry
applies to absorbed copies only (`tieread` drops the ties of FoldTooCheap's temporaries; il_premap, which sees their leaves
renamed to the source, had given them to s$1 / state$1 in five functions).

*While allocating* (NColProcessCandidate 0x10068300 / NColProcessCandidateRegion 0x10068680; il_proc_folds): a
candidate with exactly one definition (else L+0x4c |= 0x40, no benefits), foldable and recalculable, gets a benefit per
use NColMdNoFoldBenefit(use, def, T, **0**) (INF also for the uses in L+0x24), NColBenefitOfUsesInRange sums them shifted
by twice the use's zone depth (INF if any is), and the region is folded away (NColFoldAwayRegion) when **MdmDefCost
(1 favoring speed) << 2 * the definition's zone depth, times the region's definitions, is not below the sum** (an lea's
benefit is 0: it folds whenever every use takes it; a load folds into one use at its own depth, not into two, nor into
one inside a deeper loop). When the definition has a register source each use is also checked by NColLoadCanBeRecalcd
(0x100c7640): a source already processed must hold its register at the use or be extendable to it,
NColCanExtendAlreadyProcessed (0x100c8a70): the region before the use has a register, the blocks of both ends have the
same +0xac (modelled as the same loop depth **[I]**), no external flow enters, the register is free over the window from
that region's end to the use (NColFindFirstOverlappingAvailRegion) and not used in it. `foldext` checks this with the
registers of the candidates processed before as traced (replay's premise; the one trace input left in il_predict). zlib
with the traced registers: leas **141 / 141** (131 folded; 9 kept because a source processed before cannot be extended, 1
because the use is in a deeper loop), loads **116 / 116** (none wrong; NColMemOprModifiedInLife approximated: a call or a
store inside the load's lifetime); the shapes of 12.4 had 97 found and 34 missed, 107 found, 9 missed, 1 wrong. Fold
fixtures 9 / 9.

**In processing order** (`foldorder`, il_apply_folds) **[C, measured]**: a fold changes the lists of a source only when
that source is processed after the folded candidate (lifetimes processed before keep their lists and are extended
instead): zlib, of the folds with a traced source list, 97 + 77 changed a later source, 49 load folds left the earlier
base's list alone, none the other way. The order is order_of on the priorities of the IL's lists before the folds. An lea
fold also rewrites its uses' addresses (`foldaddr`, _fold_address: T as base gives the lea's registers with the use's
index, an index-only lea beside a scale-1 index moves that index to the base, displacements add), and NColMdAddToPreferences
then reads the rewritten operand: the anti-preference set of a candidate is the union over its lists at its processing, of
the operands as the folds made before it left them. That closes item 2: the 18 sets of 12.4 were lists changed by earlier
folds (crc32's `c$n` chains, the temporaries of compress_block / gen_codes) and deflateParams t717, whose `[t717+t797*1]`
load gets t797 = `lea [t733+8]` folded in first: `[t717+t733*1+8]` has a displacement, so no rbp / r13 (not a scale-1
rule). A use a fold gave a candidate counts as a use for its preference list (`foldcost`: deflateCopy ss$1, whose last use
became an `add` with the folded load), and a partner's register is the one of its region covering the tuple
(`prefregion`, NColFindRegionInTree: inflate_table mask$1's used$1 has two regions).

**Ties** (`tieread`) **[C]**: NColBuildInfo's first site (0x100d7cf8..0x100d7d2d) runs where a use starts a new range in
the backward walk; a copy by opcode (NColMdIsCopyInstr: mov whatever its source) whose first destination is a physreg ties
every candidate it reads there, an address component of a memory source too: `mov rdx <- [t223+32]` (flush_pending,
deflate_stored, deflate_slow, deflate_fast, inflateCopy, zError's `mov rax <- [t75]`). deflate t837's window: the range a partial
definition starts (below). Ties 3189 / 3189, windows 3189 / 3189.

**Preference windows** (`pwlist`, il_prefwin_lists) **[C]**: NColAddRegPrefRange (0x101850c0) inserts each window before
the first node from the head whose begin is not below its own, and sets the head f6a0[reg] only for the first node; the
users (NColDeterminePhysregAvail's fallback B, 0x10069792) walk from the head by next. So a window inserted before the
first one (lifetimes in index order) is off the walk: in zlib 39 of the 595 windows (gzread's rax list has 11 windows, one
of them reachable). The unlink after processing (0x10068528) sets the head only for a node without prev: a head unlinked
while hidden windows are still linked stays on the walk, and so does every node unlinked after it (_pw_drop's ghost). This
was gzread t774 / t775 (rax blocked by windows the walk never sees), the last two of the six replay cases of 12.4; the
other four (deflate t793, gen_bitlen t726 / t785, get_byte t188) were the folds' lists and the partial definitions.

**Lifetimes** (`partlive`, `defsplit`) **[C for the walk, measured]**: a byte / word write of a wider lifetime reads the
rest of it, so in the liveness it neither kills nor starts the value (deflate t837: `sete t836` after the coalesced copy);
a pure definition starts a new range whatever the gap to the previous appearance (gen_codes code$1 `[13,25] [26,32]`; the
earlier rule wanted a hole of a tuple). Priorities 3182 -> 3187. The two left (longest_match scan$1, inflate hold$1) are
coalescings: a 32-bit view used at n and the survivor defined at n + 1 are one range. IL_SUB gets them: il_subsume's union
joins touching ranges only when the dead lifetime's comes first (`deadfirst`; code$1 and huff$1 keep the survivor's
`[.., n]` and the dead `[n + 1, ..]` apart), and NColFoldTooCheap's folds go to the subsumed lifetimes (`ftc`,
il_apply_fold_too_cheap): priorities **3189 / 3189**, lists 3188 / 3188. IL_SUB is not the default: on fp.cpp's integrate
il_subsume merges a float function's copies differently (pos$1 / t3770: 4 candidates' lists), sim 306 against 309.

**Float flag and frame score as read** (`fpread`, `rbpscore`) **[C]**: the flag from `fflags` (else `perfunc`'s FUNC+0xe4,
else il_uses_float); zlib has no function with it, the fixtures two whose float leaf the optimizer removed. The frame
score from `perfunc` (TLS+0x1196c): zlib inflateBack 559, inflate 186, inflate_table 186 (the three that reserve rbp),
then compress2 73; computed (il_rbp_score) it matches the probe in 97 / 97 functions and 26 / 26 fixture functions when a
memory leaf whose +0x2c SYM is not in the dump scores 6 only on an OPLOCALSCALED leaf (an index into a local array: its
SYM is that stack local) and otherwise falls to the leaf rules **[I]** (91 / 97 counting every unknown one 6:
deflate_slow, deflate_fast, _tr_tally, compress_block, inflate: +18 / +42 from lea and load addresses, 0x4fe / 0x4ff).

**Still not computed / not exact**: the order walk itself (replay's 84 misses are the traced inputs' 84: compressBound,
longest_match, deflate_slow's long globals with no full cover, ..: NColBestCoverage / gaps, section 4); `foldext` reads the
earlier candidates' registers from the trace (simulate does not yet decide the extension with its own choices, nor cut
the extension window from the source register's availability); NColMemOprModifiedInLife and
NColMdCanReplaceCandWithMemWithAlignment are approximated by shapes; the blocks' +0xac (as loop depth); the +0x2c SYM kind
of the frame score's unknown leaves (read by the probe instead); constant folds (simulate takes them from the trace);
IL_SUB's float coalescing; inflateBack have$1's ranges and gen_bitlen base$1's appearances at processing.
