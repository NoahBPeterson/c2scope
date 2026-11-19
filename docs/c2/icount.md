# Instruction-count differences: which pass made, kept or removed each instruction (`tools/icount.py`)

The register and slot tools (`regs`, `regmap`, `scratch`, `slots`, rasim) explain functions whose instructions
already line up with the original. Most remaining near misses have a different instruction count. `icount.py`
explains those one instruction at a time. For each instruction that only we have, it names the IL tuple behind it,
the pass that created the tuple, and the passes that could have removed it but did not. For each instruction that
only the original has, it finds our tuple at that place and the pass that removed or folded it. It then gives a
verdict class and, where one is known, the source change that flips it.

```sh
uv run tools/icount.py SRC --func F [--c2 BUILD] --asm-vs ORIG.s [--asm-func G] [--json OUT] [--show-il] [--fine] -- <cl flags>
uv run tools/icount.py SRC --func F --vs-func G -- <cl flags>      # "original" = function G of the same compile
```

The conventions are those of tsweep, realize and regmap: `--func` is any `--func` form, and ORIG.s holds
`VA mnemonic operands` lines (capstone). One traced compile goes to the compile server. When a server is configured,
icount refuses to fall back to wine (`--allow-local` permits it); with none configured, or `C2TRACE_REMOTE=0`, it compiles
under wine. The compile takes 1 to 5 s on the server, plus about 1 s of analysis
(alignment is O(n·m), about 3 s for a 500-instruction function).

## 1. How it works

1. **Snapshots.** The IL of F is dumped at the entry of about 60 passes. The list is `PIPE` in icount.py, resolved
   by base name in each build's index; names a build lacks are skipped. The passes are MscRVO, Inline, OptFuncExpr,
   every GlobalOptimize sub-pass (ReverseCopyProp, DeadStores, SingleUseCopyProp, LurUnroll,
   ConstantFoldCompareBlk per block, HshBuildTable, InsertAssertions, the Lopt loop passes, LocalCopyPropagation,
   CleanUp), Opeq, Mul2Shift, AddrModes, Lower, Peeps, Globregs, Color, RegSimpleAssign, Riscify and
   MdmAvoidMOBStalls (13.10), the late flow optimiser (each FgDoLateFlowOptsOnce), LoFinalLower,
   LateGlobalOptimize, FgCodeMotion, MscClean, Schedule, MdmSuperHack, EncEmitFunc and Encode. `--fine` adds
   GoptOptimizeBlock and GoptOptimizeLoop, one snapshot per block and sweep. The tracer's function filter
   (tracer.md, "Function filter") keeps the trace to F: Mount & Blade 0x4b6180 (1747 bytes, ~60 dumps) logs 21 MB;
   without the filter every other function of the TU is dumped at the same passes. The same compile carries c2merges' hooks (the late flow optimiser's events) and
   `C2TRACE_ILPAS=1` (loadcse's PAS index).
2. **Tuple identity.** A tuple keeps its address for its whole life, and Lower rewrites most tuples in place
   (0x47cfe0: 157 of 157 IL tuples survive Lower). Freed memory is reused, so an address starts a new tuple when its
   source line changes, or when it comes back as another opcode after being absent. A short gap with the same line
   and opcode is a partial stream during a flow-graph rebuild and is bridged.
3. **Final code to tuples.** The IL at Encode, with encoded lengths, gives each listing offset its tuple
   (the same mapping as `scratch`).
4. **Alignment.** Our listing is aligned with ORIG.s by a global alignment that maximises a similarity score: exact
   bdiff key 4, same registers 3, registers masked 2, stack displacements masked too 1.5, same mnemonic and kind 0.5.
   Unlike difflib's longest-block-first match, this pairs a whole register-exchanged region instead of anchoring on
   a few equal instructions elsewhere. Unmatched instructions form the runs: EXTRA (ours only) and MISSING (the
   original only). An EXTRA and a MISSING instruction of the same shape within 48 instructions (another register or
   another stack slot) count as **moved**. So does the same operation with a register operand on one side and a
   memory operand on the other. Moved instructions are listed separately and are not count differences.
5. **EXTRA.** The tool reports the tuple's birth snapshot and its history: each change of text and the pass interval
   that made it. The verdict comes from the birth interval (`made-by:<class>`) or from the kind:
   - **Loads:** loadcse's analysis on the same trace (§2 of loadcse.md: FIRST / REUSED / RELOADED R1-R6), matched by
     tuple, by the OPLOAD that feeds a variable's initialisation, or by location. If loadcse has no pair, the tool
     checks itself for a call or a possibly aliasing store between two reads of the location at V.
   - **Enter loads:** `regalloc:enterload`.
   - **Front-end tuples nothing removes:** `structure`.
6. **MISSING.** The tool collects candidates: tuples of our IL that lie between the run's surviving neighbours (two
   anchors of slack for scheduling), in every snapshot, and that were removed there or lost a memory operand (a load
   CSE replaced). Candidates are matched to the missing instruction by kind: cmp to OPCMP, jcc to OPBRANCH, a load
   to a load or copy. A load plus store is one memory-to-memory copy. Push and pop of a callee-saved register in the
   prologue or epilogue are frame instructions. If nothing matches at that place, a second search looks for a
   removed tuple of a compatible kind within 8 source lines, preferring one that names the variable the
   instruction's stack slot holds (read off our listing's `_t$[esp+..]`). The scheduler can put the original's
   instruction far from its statement (0x4b1930).
   Refinements:
   - **Compares** removed in sweep 1 look for the GdfInsertAssertions fact that decided them (`folded:nonnull` after
     a dereference, `folded:range-check` for a second test of the same value).
   - **Tuples removed in FgDoLateFlowOpts** are checked against the merge events: a twin matched in a merge that was
     carried out gives `merged:`; no twin gives `removed-by:FgDoLateFlowOpts`.
   - **Copies that became register-to-register copies of one register** give `regalloc:coalesced`.
   - **Copies an expression fold produced** give `folded:expr`.
7. **Source lines.** Each run gets its source line from our listing's `; NNN :` comments. A tuple's line field is
   relative to the function and is shifted by the majority offset.

## 2. Verdict classes and levers

| class | meaning | lever (where known) |
|---|---|---|
| `removed-by:GlobalOptimize/CSE` | sweep 2 (GoptOptimizeBlock with CSE) deleted our reload; the original reloads | read through a reference or pointer used twice (loadcse R3), or a store or call between (loadcse.md §4); realize `alias2:CHAIN` |
| `removed-by:GlobalOptimize/LocalCopyProp`, `/SingleUseCopyProp`, `/ReverseCopyProp`, `/sweep1`, `/Canonicalize` | copy and constant propagation removed a copy or a value the original keeps | the original's variable has a second definition, a call-argument use or its address taken (opt.md §3) |
| `removed-by:GlobalOptimize/DeadStores` | GdfRemoveDeadStores deleted a store the original makes | in the original something reads it before the overwrite (opt.md §6) |
| `folded:range-check` | sweep 1 decided a compare from a dominating test of the same value | the original's first test reads another value (the field through the pointer instead of the local copy); 13.10 only, 14.00 and 15.00 fold both (probe `range`) |
| `folded:nonnull` | sweep 1 decided a null test from the `p != 0` assertion c2 inserts after `*p` | only a memory operand `[p+K]` asserts `p != 0` (opt.md 7.7): a copy into p written as `memset` or as an inline helper that passes the size to `memcpy` as a parameter (`MemCopy(p, src, sizeof(T))`) is still an intrinsic at that point and keeps the test; `*p = *src` and `memcpy` with a constant size at the site do not (f3decomp R1050, all four builds). Otherwise the original's test comes before the dereference, or the dereference is of another value. F3 0x00422420: byte match |
| `folded:compare` | another compare folded (constant, assertion on a branch arm) | |
| `folded:expr` | OptFuncExpr folded the expression into a copy, which a later pass removed | spell it through the inline function or operator, so it folds only after inlining (M&B 0x4b1930) |
| `kept:alias` / `kept:kill` / `kept:path` / `kept:type` | CSE kept our reload: R2-R4 (another alias class or base), R5 (a call or aliasing store between), R6 (not on every path), R1 | `rwsweep --families unalias`, `Q* q = p->q;` (loadcse.md §4); for R5 find the store the original does not have |
| `merged:FgDoLateFlowOpts` | the late flow optimiser merged our duplicate into its twin (tail, head, cross jump); the original keeps both | `c2trace.py merges --off OFF`: make the tails differ, or equal, at the first difference |
| `removed-by:FgDoLateFlowOpts` | removed there without a merge (dead code, branch chains) | |
| `merged:FgCodeMotion` / `FgCoalesceLabels` | block layout or label coalescing | layout.md |
| `regalloc:Globregs`, `regalloc:enterload`, `regalloc:coalesced`, `regalloc:spill`, `regalloc:callee-saved`, `made-by:regalloc:*` | a register-allocation difference that changes the count: enter loads of a global register candidate, a spill or reload, one more callee-saved register, a copy coalesced away | `c2trace.py regs --why`, regmap, `rasim need`: the variable, then the usual levers |
| `made-by:lower:*`, `lower:*` | instruction selection: Lower, AddrModes, Mul2Shift, Opeq, LoFinalLower, MdmInsertEntryExit | isel.md |
| `riscify`, `riscify-undo`, `made-by:riscify-undo` | 13.10: a memory operand split off into a load, or a split put back after Schedule | regalloc.md §6.6, `scratch` R rows |
| `peephole:*` | Peeps, MscClean, LateGlobalOptimize, MdmSuperHack | |
| `x87:*` | fld / fxch / fstp shape | `c2x87.py`, x87.md |
| `loop:*` | rotation, unrolling, strength reduction, landing pads, test replacement | opt.md §8 |
| `structure`, `structure:inline` | front-end IL: no pass made or removed it | draft structure: a statement, temporary or branch the original's source has or lacks |

A run takes the most frequent informative class among its instructions (`structure` only when nothing else
applies). `--json` keeps every instruction's class, tuple, birth, history and reason.

## 3. Probes (`tests/data/icount.cpp`, `tests/run_tests.py` icount_tests)

Each probe is an `X_ours` / `X_orig` pair that differs by one construct (run with `--vs-func`). Every cell shows
the count difference and the class.

| probe | construct | 14.00 | 15.00 | 13.10.3077 / 3052 |
|---|---|---|---|---|
| cse | `p->q` read three times, against `Q*& r = p->q` used twice | -1 CSE | -1 CSE | -1 CSE |
| alias | the reverse | +1 kept:alias (R3) | +1 kept:path (plain dump: no branch targets) | +1 kept:alias |
| kill | `p->q->m = 0` between two reads of `p->a`, against `p->b = 0` | +2 kept:kill (+ the p->q load, structure) | +2 kept:kill | +2 kept:kill |
| range | a struct copy's field tested twice, against the outer test through the pointer | 0 (both folded) | 0 | -5 folded:range-check (+ callee-saved esi) |
| dead | `p->a = 1; p->a = 2;` with nothing read between | -1 DeadStores | -1 | -1 |
| tail | equal arm tails | -8 merged:FgDoLateFlowOpts (twins from FgTryTailMerge) | -8 | -8 |
| stmt | a statement the original has | -1 structure | -1 | -1 |

Rule probes (tests/data/rules, rules_misc_tests; opt.md §8.1 items 5 and 6):

| probe | construct | verdict on every build | lever |
|---|---|---|---|
| R1280 `copy_ours` vs `direct_orig` | `void* start = First(); Node* cursor = (Node*)start; while (cursor)` against the result assigned straight to `cursor` | +1: `structure` (`xor eax, eax`; `mov esi, eax`) + `structure:inline` (`jmp`) + MISSING `regalloc:callee-saved` | a kept `xor; test; je` join after an inlined `return 0` is this copy: assign to the tested variable to get the threaded form, or add the copy to keep the join. The class names no flow pass: the threading is GlobalOptimize deciding the guard on the 0 path, and the copy in the join block blocks it |
| R1276 `f_hlive` vs `l1_rev` | guard copy-propagated to the head, head live after the loop | +0, no class (registers only) | `regmap`; spell the loop variable as the definition and the head as its copy |

## 4. The benchmark functions

- **M&B 0x0047cfe0** (13.10.3077, 207 against 209). The difference is one run, `cmp ecx, ebp; jl` before
  `cmp ecx, g_item_kind_count`: `folded:range-check`. The `>= 0` half of the inlined `ir_mesh_name(ir)` test
  (OPCMP `s12 >= 0`) was removed in sweep 1, decided by the caller's `if (ir.item >= 0)` on the same field of the
  local copy. The "one fewer reload of `[ebx]`" in RESULTS2.md Run 3 was a difflib alignment artefact: with the
  global alignment both codes have the reload. **Lever confirmed:** writing the outer test as `if (it->item >= 0)`
  (the field through the pointer) keeps both halves of the inner test (the run disappears;
  `work/icount/mb/ic47_a.cpp`). It moves the register allocation: 210 against 209.
- **F3 0x00422420** (14.00, 62 against 64). `test esi, esi` and its `je` (AddHead's `if (!a_item) return;`) are
  `folded:nonnull`. GdfInsertAssertions puts `_item != 0` right after the dereference `*(Pair42*)item = *src` (one
  line earlier), and sweep 1 decides the test from it. The original's test survives, so in its source the test is
  not dominated by a dereference of the same variable. Copying through another pointer (`Pair42* n = new Pair42;
  *n = *src; item = n;`) does not help: copy propagation makes n and item one value first. **Lever found
  (2026-11-19, f3decomp R1050, opt.md 7.7):** `MemCopy42(item, src, sizeof(Pair42))` with `inline void
  MemCopy42(void* d, const void* s, unsigned int n) { memcpy(d, s, n); }` keeps the test (the copy is an
  OPINTRINSIC at GdfInsertAssertions, not a memory operand), bdiff 16 -> 39, 63 -> 65 instructions: the test is
  back and its region (`test esi, esi` among the four copy moves, `je` after) is the original's, but the loop now
  differs. Moving `p = p->GetNext();` from before the allocation to after `list.AddHead(item)` then gives **bdiff 0,
  regdiff 0, 64 = 64** (`work/bench/f3/00422420/r1050_memcopy2.cpp`). The reorder alone (with `*(Pair42*)item =
  *src`) gives bdiff 36, 62 instructions: both changes are needed.
- **Calibration, M&B 0x4b1930 color_of** (pre-fix source from git 285d351f^). -2 is reported as two `folded:expr`
  instructions, `mov ecx, [t]; mov [esp+0x34], ecx`. Our `lo.y = 1.0f * t` became the copy `s131 <- _t` in
  OptFuncExpr (history: `s131 <- t92` -> `s131 <- _t`), and GdfRemoveDeadStores then removed it. This is the
  documented cause. The fixed source gives no difference.
- **M&B 0x004b4470** (186 against 193) and **0x004e6050** (276 against 292). These have twelve and nineteen runs.
  0x4b4470 mixes loop landing pads, scheduling and Riscify undo, Globregs and Lower. 0x4e6050 is mostly x87
  (Schedule) and structure in the scene setup (`s2->fc = ..` stores and `if (!data)`).

## 5. The Mount & Blade rows (14 rows from the register-lane triage, 2026-11-15; 13.10.3077)

`work/icount/mb/rows.py prep|run|table` compiles a snapshot of the M&B tree (`work/icount/mb/tree`, HEAD
880eae87 plus the working tree). Original counts agree with M&B's `docs/insn_exact.json` (the TSV had stale numbers
for 4 rows). The "ours" counts here leave out alignment pads and jump-table data.

| va | function | ours - orig | net instructions per family (moved pairs excluded) |
|---|---|---|---|
| 0040aef0 | face::get_min | -1 | regalloc -1 (`regalloc:coalesced`: `mov esi, ecx`; `this` stays in ecx, the original copies it to esi) |
| 0040aff0 | face::get_max | -1 | regalloc -1 (the same) |
| 00424650 | face_normal | +1 | x87 +1 (fld / fxch shape) |
| 00414fc0 | noise_fractal | 0 | none: equal counts at HEAD (insn_exact.json agrees: 90 / 90; the TSV's 91 is stale) |
| 0048bb50 | UiE::key | +1 | regalloc +2 (`regalloc:enterload` of `[this+384]`; the original compares memory) |
| 00532de0 | update_party_sprites | -10 | loop -5 (`loop:unroll`; **wrong**, see below), isel -4, structure +2, folded -1, GlobalOptimize -1, flow -1 |
| 00423b60 | FxAnim::chain | -2 | GlobalOptimize -2, structure +1, regalloc -1, flow -1, isel +1 |
| 0051c5a0 | C4c2fb0::frame_update | -3 | flow -2 (`merged:FgDoLateFlowOpts`), structure -1 |
| 00502920 | PartyWindow::refresh_page | +2 | regalloc +2 (`regalloc:enterload` of g_game, a Globregs jump) |
| 00443140 | Agent::update_alarm | +3 | x87 +9, regalloc -6 |
| 0043df60 | Agent::advance_path | -6 | regalloc -9, isel +2, flow +1 |
| 0049d650 | Elem::get_operand | -10 | regalloc -18, isel +15, flow +12, GlobalOptimize -10, structure -7, schedule -2 |
| 004d3c00 | HashTab::insert | -18 | regalloc -14, GlobalOptimize -13, schedule / Riscify -5, structure +4, kept:kill +1 |
| 004b6180 | BattleRec::detach | -22 | regalloc -13, schedule / Riscify -9, structure +8, GlobalOptimize -5, isel -2, folded -1 |

Runs per family over all 14 rows (instructions; rows having the family):

| family | runs | instructions | rows |
|---|---|---|---|
| structure | 32 | 47 | 6 |
| regalloc (Globregs, enter loads, coalesced copies, callee-saved, spills) | 31 | 67 | 10 |
| isel / lowering (Lower, AddrModes, Mul2Shift, Opeq, LoFinalLower, OptFuncExpr) | 26 | 47 | 6 |
| GlobalOptimize removed (LocalCopyProp 9, CSE 6, SingleUseCopyProp 2, DeadStores 2, Canonicalize 2) | 21 | 35 | 5 |
| schedule / peephole / Riscify (Peeps, Riscify undo after Schedule) | 17 | 30 | 4 |
| flow (tail merges, FgCodeMotion, FgCoalesceLabels) | 12 | 24 | 6 |
| x87 | 5 | 12 | 2 |
| folded (compare, expr) | 2 | 2 | 2 |
| loop (unroll) | 1 | 5 | 1 |
| kept (CSE declined: kill) | 1 | 1 | 1 |

What it says:
- **Small rows (-3 to +3).** They are register-allocation differences that change the count: a coalesced `this`
  copy, enter loads of a value the original keeps in memory. There are two x87 shape rows and one tail-merge row.
  These have levers in the register tools once the variable is named.
- **Large rows (-10 to -22).** Each mixes many runs. Register allocation is the largest net term, and
  GlobalOptimize removals are the second in 0x4d3c00. Structure is between 7 and 10 runs in each.

The table is in `work/icount/mb/TABLE.md`; the per-row reports are `work/icount/mb/<va>/icount.txt`.

**Correction (2026-11-19, rules_xref.md R1100): 0x532de0 has no unrolled loop.** Both loops are `do`-`while` loops with a
call, which LurUnroll never unrolls (opt.md §8.7), and our listing still has both. The two `loop:unroll` tuples were
`OPPHI`s: `SsaOptimize` adds phis before `LurUnroll` whenever `LurWorthIt` passes (/O2, the function has loops), and
`LurEndUnroll` → `SsaOptimizeEnd` removes them whether or not anything was unrolled, so they always disappear in the
LurUnroll interval. They were matched to the original's `WL_MARKER` data bytes decoded as `add byte ptr [eax], al`.
icount now skips phis as candidates (`NOCODE_OPS`); a re-run gives no `loop:*` class for this row (the five data bytes
go to other removed tuples, which is equally noise). The `loop (unroll)` line of the table above is that artefact.

## 6. Limits

- **Alignment.** It is a heuristic. Two loads of the same shape can be paired either way: in probe `kill`, ours'
  first read of `p->a` is the extra instead of the reload. Read the run, not only the single instruction. Large
  structural differences (35 or more runs) give long reports that are partly alignment noise.
- **Only our IL is observed.** For the original, "structure" means only that nothing in our IL at that place was
  removed. The tool cannot see that the original's front end produced other tuples; that is inferred.
- **Tuples made and freed between two snapshots** (FgEarlyTailDup's copies that a merge removes again) are not
  seen. `--fine` narrows GlobalOptimize down to one block.
- **Plain IL dump (13.10, 15.00).** There are no branch targets, so loadcse pairs reads in stream order, and the
  15.00 `alias` probe says `kept:path` where 14.00 says `kept:alias`.
- **Verdicts without a pass behind them.** `regalloc:spill` and `regalloc:callee-saved` for missing instructions
  are heuristics: a stack load or store, or a push or pop, with nothing removed at that place.
