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
  FUNC+0x84 != FUNC+0x80, i.e. EH funclets: removes r13/r13d/r13w/r13b there, cold 0x102d13a5);
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
