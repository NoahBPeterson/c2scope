# c2 machine-independent optimizer: what the IL looks like when register allocation starts

This spec covers the passes of cl 14.00.50727.762's back end (`Bin/c2.dll`) that run between the front end's IL
and register allocation: the expression optimizer (`OptFuncExpr`, optimize.obj), `GlobalOptimize` (globopt.obj,
globdf.obj, hash.obj), the loop optimizer (globlopt.obj), loop unrolling (lur.obj) and the small passes after
them (`Mul2Shift`, `AddrModes`). It is written so that you can predict, from the source of a function, which
values exist at `Color` entry: which copies are gone, which expressions share a temporary, what a loop's
induction variables are.

**Tags.** **[C]** = CONFIRMED: shown by a probe compile (the probe is named) or read in the code (the address
is given). **[I]** = INFERRED. Addresses are c2.dll VAs. All probes are in `work/scratch/opt/` and were compiled
`/TP /O2` unless a rule says otherwise; `NAME.il` holds the IL of every function of `NAME.cpp` at `@MscRVO@4`
(c1xx's output), `@GrgIdentifyCandidates@4` (after GlobalOptimize) and `@Color@4`.

**Verification pass.** The rules were re-tested by a second agent with new probes in `work/scratch/opt/verify/`,
compiled as C++ with the Fallout 3 flags (`/O2 /TP /MT /GS /EHsc /Gy`), each in up to four contexts: a free
function, a member function (`this` as the base), a function with a C++ EH frame and other code in front, and
inlined into such a function. A probe reference followed by "(re-verified, C++ game flags: `name`)" means the
rule held in every context tried; `name` is a function in `verify/v_*.cpp`. Rules that failed or hold only under
a condition are corrected in place and listed in §13. "(not re-verified)" marks rules the pass did not test.

Read docs/c2/il_pipeline.md §5.3 to §5.4 (pass order), docs/c2/il_tuple.md (opcodes) and docs/c2/frontend.md
(what c1xx emits) first. Register allocation itself is docs/c2/regalloc.md.

---

## 0. Summary for decompilers

1. **A copy of a non-address-taken scalar disappears** when it has one definition: `int t = a;` leaves no `t`,
   every use reads `a` (§3 rule 1). A variable with two definitions (`t = a; if (c) t = b;`), or one whose address
   escapes, survives as a variable and is a register candidate. **[C]** This also holds inside C++ EH regions.
2. **A named local initialised from a memory load survives when its single use is a call argument**
   (`int t = p->m; h(t)` keeps `t`), and disappears when its single use is arithmetic or a compare
   (`return t + 1`, `if (t)`) (§3 rule 4). **[C]** **Not in EH functions:** a single-use named local defined
   inside a C++ EH state region, or on a path that still enters one, stays a variable whatever its use
   (§3 rule 4a). **[C]**
3. **Two equal expressions become one temporary** when the expression is available on every path (global
   available expressions, no partial redundancy), is not "too cheap", and nothing in between kills it (§4). The
   temporary is an optimizer temp with more than one use, so it is a `Color` candidate like a variable: across
   a call it takes a callee-saved register. **[C]**
4. **A sum inside a sum is not shared.** `h(a + b) + (a + b)` computes `a + b` twice, because `OptFuncExpr`
   flattens `h + (a + b)` to `(h + a) + b` before CSE runs. `a * b`, `a / b`, `a & b`, a compare result and a
   load are shared. Writing `int t = a + b;` makes one variable (§2 rule 2, §4 rule 3). **[C]**
5. **A memory load is reloaded after anything that might write it**: any call (also `__declspec(noalias)`
   callees), a store through any pointer that is not provably elsewhere, a store to any global when the load is
   through a pointer. It is **not** reloaded after a store through the same base at another offset, a store
   through a `__restrict` pointer, a store to a different global, or (for a `static` whose address is never
   taken) a store through a pointer (§5). There is no type-based disambiguation: a `float*` store kills an `int`
   load. **[C]** "Another offset" means two constant offsets: `p->arr[i] = ..` (variable index) does kill `p->m`, and
   `p->m = ..` kills `p->arr[i]`. **[C]**
   **A pointer that is null-tested and then dereferenced is often loaded twice** (`cmp [mem],0; je; mov reg,[mem]`)
   although nothing kills it: the test and the dereference read it with different IL types (§4 rule 8). **[C]**
6. **`p->next->m = 1; p->next->n = 2;` loads `p->next` twice; with `P* q = p->next;` it is loaded once** (§5
   rule 6). Reading it twice without a store in between gives one load in both spellings. **[C]**
7. **Dead stores**: a scalar's dead definition is removed across the whole function; a store through a pointer
   is removed only when a later store to the same address follows in the same block with no possible read in
   between (calls and loads through other pointers are reads; stores through other pointers are not). A store to
   an address-taken local is dead after the last call and live before any call (§6). **[C]**
8. **Loops are rotated** before the optimizer sees them: guard test at the top, test at the bottom
   (il_fg.md rule 8). The whole first test block is duplicated, calls included. **[C]**
9. **Induction variables** (§8.4): every `i*c + base` first becomes its own pointer. Then
   - one array (or several accesses off one base) with element size 1, 2, 4 or 8 and an `int` index compared
     against the bound, **and an access at displacement 0 from a base variable** (`a[i]`; a global array also
     qualifies): the pointer is folded back, the code is `[base+i*size]` with `i` counting up;
   - a member array `p->arr[i]` at a non-zero offset, or `a[i + 1]` alone: a moving pointer started at
     `base + offset` and a down-counter;
   - element size not 1/2/4/8, or two or more different bases, or a `short`/`char` index, or an offset index
     such as `a[i-1]` with `i` from 1: a moving pointer; other bases are reached as `[ptr + (b - a)]` with the
     difference computed in the preheader;
   - if `i` is then used for nothing but the exit test, it is replaced by a **down-counter** initialised with
     the trip count (`sub reg,1; jne`). **[C]**
10. **Invariant code** is hoisted into the preheader (after the guard) when it is pure, its block runs on every
    iteration, and no store or call in the loop can change it. A load from memory is hoisted only if the loop
    has no call and no store that may alias it; with a call only the address `p+off` is hoisted in the IL, and
    it is folded back into the `[p+off]` operand, so the final code shows nothing hoisted (§8.2). **[C]**
11. **`a[i] = v` loops become `rep stosd`/`memset`, `a[i] = b[i]` loops become `rep movsd` only when the two
    arrays cannot overlap** (globals, `__restrict`) (§8.6). **[C]**
12. **Unrolling exists only at /O2** and only for innermost counted `for`/`while` loops with step 1 and no
    call: complete for a constant trip count of 10 or less, by the largest factor from 6 (or 5, 4, 3, 2) that divides a
    constant trip count otherwise, and by 4 with a remainder loop for float loops of unknown trip count. A float
    loop whose constant trip count has no divisor is unrolled by 8 with a remainder loop. Integer loops with
    unknown trip count, or with a constant trip count above 10 that has no divisor from 6 to 2, are not unrolled
    (§8.7). **[C]**
13. **/O1 and /O2 produce the same IL here except for unrolling** (§10). **[C]** (131 probe functions compared.)
14. **The same constant used twice in instructions that also carry a displacement goes into a register**
    (`xor ecx,ecx; mov [eax+4],ecx; mov [eax+8],ecx`); `mov [eax], imm`, `push imm` and `op reg, imm` never
    cause it; across a call it takes four uses (§7 rule 5). **[C]**

---

## 1. Where it runs and in what order

All of this is phase 2 and 3 of il_pipeline.md §5.3 and runs only when `_Globopt_flag` is set (`/Og`, so /O1,
/O2, /Ox).

| # | pass | address | what it does here | facts it needs |
|---|---|---|---|---|
| 1 | `FgDFOReorderLoopRecog` → `LgBuildLoopGraph` | 10b26ca0 → 10b85e90 | finds loops, **rotates them** (`LgZtripTest1` 10b871a0), makes the preheader ("landing pad", `LgMakeLandingPad1` 10b87bd0) and exit pads | dominators |
| 2 | `OptFuncExpr` | 10baf600 | per-expression tree rewrites: constant folding, reassociation, distribution (§2) | none (one expression tree at a time) |
| 3 | `GlobalOptimize` | 10b49300 | the sub-passes below | alias sets from `ItfBuildInterference` (10b73430, run in phase 2) |
| 3a | `GoptSimpleConstantFoldFunc`, `GoptCanonicalize` | 10b530a0, 10b4a920 | local folding; canonical forms (not read, §12) | none |
| 3b | `GoptReverseCopyProp` | 10b4d050 | `t = expr; x = t` → `x = expr` (§3 rule 3) | local def/use |
| 3c | `GdfRemoveDeadStores` | 10b36920 | dead definitions and dead stores (§6); runs 5 to 6 times in all | live/dead sets (`GdfInitFlowGraph` 10b34050) |
| 3d | `GoptSingleUseCopyProp` | 10b4e110 | substitutes a single-use definition into its use (§3 rule 4) | single-def-single-use ("SDSU") info |
| 3e | `LurUnroll` | 10b8d670 | loop unrolling, only if `LurWorthIt` (10b8db00) and fewer than 10000 blocks; then 3c and 3d again | SSA (`SsaOptimize` runs just before) |
| 3f | `GoptConstantFoldCompareBlk` per block | 10b55460 | folds branches whose outcome is known | |
| 3g | `HshBuildTable` | 10b5eb30 | value numbering: every expression gets a hash temp, equal expressions the same one; builds the kill lists | |
| 3h | `GdfInsertArtificialAssigns`, `GdfInsertAssertions` | 10b349b0, 10b35240 | records `x == K` facts on branch arms | |
| 3i | sweep 1: `GoptOptimizeBlock` per block | 10b4fea0 | copy and constant propagation, constant folding, branch folding, redundant compare removal. **CSE is off** (`_GoptDoCSE = 0`, `_GoptDoRCE = 1` at 10b49300) | availability, computed block by block |
| 3j | `GoptRemoveAssumes`, 3c | 10b4ccd0 | | |
| 3k | loop phase: `GoptOptimizeLoop` per loop, inner loops first | 10b501b0 | invariant code motion, then `LoptOptimizeLoop` (§8) | `GdfCalcAvail` 10b394c0, loop tree at FUNC+0x0c, region constants |
| 3l | sweep 2: `GoptOptimizeBlock` per block, `GoptLimitCsesFromLoop` at loop ends | 10b4fea0, 10b51910 | the same as sweep 1 **plus CSE** (`_GoptDoCSE = 1`) | `GdfCalcAvail` |
| 3m | 3c, `GoptRebuildSdsu`, `GoptLocalCopyPropagation`, `GoptCleanUp` | 10b4fab0, 10b4fd70, 10b54570 | hash temps with one def and one use go back to expression temporaries; the rest stay | |
| 4 | `OptFuncExpr` again | 10baf600 | re-canonicalises the rewritten expressions | |
| 5 | `Mul2Shift` | 10baa730 | `x*K`, `x/K`, `x%K` into shifts, `lea` shapes, magic multiplies (§7 rule 6) | |
| 6 | `AddrModes` | 10c09cf0 | folds address arithmetic into `[base+index*scale+disp]` operands and `lea` | |

Order and gating are **[C]**: read in `GlobalOptimize` (10b49300, decompile `work/decomp/c2.dll/c/10b40000.c`
line 7465) and `dop2_PreLowerGlobalOpts` (il_pipeline.md), and seen in per-sub-pass IL dumps
(`work/scratch/opt/gstage.sh FILE FUNC` dumps one function at 33 pass entries and prints only the stages that
changed it). The "what it does" column is **[C]** where a rule below cites a dump, otherwise **[I]** from the
function names.

Consequences of the order:
- CSE runs **after** the loop optimizer, so a loop's strength-reduced pointers and hoisted temporaries are what
  CSE sees, and an expression hoisted out of a loop is available to the code after the loop. **[C]** (order)
- Copy propagation runs before and after the loop phase; unrolling runs before everything except the first
  copy/dead-store round, so each unrolled copy is optimized like hand-written code. **[C]** (order)

**How to see it.** `uv run tools/c2trace.py il FILE --func NAME --at @GoptSingleUseCopyProp@4,@HshBuildTable@4,...
-- /TP /O2`. Useful hook points, all entered once or more per function: `@GoptReverseCopyProp@4`,
`@GdfRemoveDeadStores@8`, `@GoptSingleUseCopyProp@4`, `@LurUnroll@4`, `@HshBuildTable@4`,
`@GdfInsertArtificialAssigns@4` (entered before sweep 1 and again before the loop phase), `@GoptRemoveAssumes@4`
(after sweep 1), `@LoptInitFunc@4`/`@LoptTermFunc@4` (around the loop phase), `@GoptLocalCopyPropagation@4` (after
sweep 2), `@Mul2Shift@4`, `@AddrModes@4`. Inside the loop phase (`work/scratch/opt/lstage.sh`):
`@LoptStrengthReduce@12`, `@LoptOptimizeLandingPadPhase1@8`, `@LoptBlockMoveInit@8`,
`@LoptLinearFunctionTestReplace@8`, `@LoptBuildAddressModes@4`, `@LoptCountedLoop@8`, `@LoptHoistFinalValues@8`,
`@LoptCleanUpLoop@8`.

**Reading the dumps.** `_x` is a user variable, `t<n>` a temporary. After `HshBuildTable` every expression
result is a hash temp (numbers from 128 up); `GoptCleanUp` turns those with one def and one use back into plain
expression temporaries. What matters for allocation is not the number but the use count: a temporary used once
is a scratch temporary (regalloc.md §6), one used more than once (or defined more than once) is a `Color`
candidate, shown by `c2trace.py regs` as kind `temp`. **[C]** (c1 `cs01`: `t128`, 3 references, coloured esi.)

---

## 2. Expression rewrites (`OptFuncExpr` 10baf600)

`OptFuncExpr` rebuilds each expression tree (`OptBuildTree` 10bb0140, `OptTree` 10bb0f10, `OptRebuildTuples`
10bb59c0). It runs before GlobalOptimize and again after it. Probe dumps taken at `@GlobalOptimize@4` already
show its result.

1. **Constants fold and reassociate**: `(a + 3) + 5` → `a + 8`; `(a * 3) * 5` → `a * 15`; `x = 3; y = x * 4;
   a + y` → `a + 12`. **[C]** (k1 `k25`, `k26`, `k15`; code `OptConstFold` 10bb51b0.) (re-verified, C++ game flags: `kf1`, `kf4`)
2. **Add chains are flattened and left-weighted.** `h(..) + (a + b)` becomes `(h(..) + a) + b`; `t + (a +
   h(1))` with `t == a` becomes `h(1) + a*2`. This is why a repeated sum is not a common subexpression (§4 rule
   3). A product, quotient or load inside the chain stays one operand. **[C]** (c3 `ce21`: at `@MscRVO@4` the
   second `a + b` is one tuple, at `@GlobalOptimize@4` it is `t64 + a` then `+ b`; c1 `cs01`; c2 `cq02`; (re-verified, C++ game flags: `cs2`). Code:
   `OptLeftWeightTree` 10bb64b0 **[I]** that this is the function.)
3. **Common factors are pulled out**: `a*3 + a*5` → `a*8`; `a*4 + b*4` → `(a + b)*4`; `x + x` → `x*2`
   (also for loads: `p->m + p->m` → `[p]*2`, one load); `(a & b) + ((a & b) << 3)` → `(a & b)*9`;
   `a[i]*k + k*3` → `(a[i] + 3)*k`. **[C]** (k1 `k24`, `k27`; c1 `cs03`; c3 `ce27`, `ce24`; l1 `lp04`. Code:
   `OptDistrib` 10bb0bf0, factor.obj `Factoring` 10b251b0 **[I]** for which of the two does each case. (re-verified, C++ game flags: `kf2`, `kf3`, `xx1`, `xx2`.)
4. `x << n` is rewritten as `x * 2^n` (`OptShlToMul` 10bb4950); the shift comes back in `Mul2Shift`. **[C]**
   (c3 `ce22`: `OPSHL` and `OPMUL _a 4` in the same function.) (not re-verified)
5. Because of 3, **`x + x` of a memory value reads memory once** even though a load is otherwise re-read
   after a kill: `x = p->m; ... return x + p->m` with nothing in between becomes `[p]*2`. **[C]** (c3 `ce01`,
   `ce06`.) (re-verified, C++ game flags: `xx1`, also with `this` as the base; in an EH region the local `x` stays a variable and the
   load is still single.)

---

## 3. Copies and aliases

Mechanisms: `GoptReverseCopyProp` 10b4d050, `GoptSingleUseCopyProp` 10b4e110 (legality:
`GoptSingleUseCopyPropLegal` 10b4f6e0), `GoptCopyPropagation` 10b51e10 with `GoptFindCopyValue` 10b526e0 (in
both sweeps), `GoptLocalCopyPropagation` 10b4fd70, and `GdfRemoveDeadStores` to delete the copy once nothing
reads it.

1. **`T t = a;` with `a` a parameter or local that is not redefined while `t` is live: `t` disappears.** Uses
   of `t` read `a`. This holds for chains (`b = a; c = b; d = c`), for pointers (`P* q = p`), across calls and
   into loops. **[C]** (c1 `cp01`: the copy is propagated in sweep 1, seen between `@GdfInsertArtificialAssigns@4`
   and `@GoptRemoveAssumes@4`, and the dead `t = a` is removed by the next `GdfRemoveDeadStores`; c2 `cq01`,
   `cq03`, `cq04`; frontend.md "Aliases of a pointer are free".) (re-verified, C++ game flags: `cp1`, `cp2`, `cp5`; also inside an
   EH region: `cp1_e`, `ehc1`.)
2. **Both arms assign the same value**: `if (c) t = a; else t = a;` → `t` disappears. A swap through a
   temporary (`t = a; a = b; b = t`) disappears completely. **[C]** (c2 `cq11`, `cq14`.) (re-verified, C++ game flags: `cp4`; the swap not
   re-verified.)
3. **`{ int t = expr; x = t; }` leaves only `x = expr`** (reverse copy propagation). **[C]** (c2 `cq09`: gone
   at `@GoptSingleUseCopyProp@4` entry, so removed by `GoptReverseCopyProp` or the dead-store pass after it.)
   (re-verified, C++ game flags, and it still works in an EH region: `ehc2`, `ehc8`; with `int t = a + b; int u = t; return h(u) + u;`
   only `u` survives: `ehc5`.)
4. **Single-use locals** (`GoptSingleUseCopyProp`):

   | definition | single use | result | probe |
   |---|---|---|---|
   | `t = a + b`, `t = a * b` (no memory) | call argument | substituted, `t` gone; the computation moves to the argument's position | c2 `cq05`, c6 `su11`, `su12` |
   | `t = p->m` or `t = g` (a load) | arithmetic, compare, return value | substituted, `t` gone | c6 `su02`, `su15` |
   | `t = p->m` or `t = g` (a load) | **call argument** | **`t` stays a variable**, even with nothing between the load and the call | c6 `su01`, `su03`, `su04`, `su07`, `su09`; c2 `cq08` |
   | `r = h(..)` (a call result) | return, arithmetic | substituted | c6 `su05`, `su06` |
   | `q = p->next` | base of one load that is a call argument (`h(q->m)`) | `q` stays a variable; `h(p->next->m)` has a temporary instead | c6 `su13`, `su14` |

   All rows **[C]**. This is the IL difference behind frontend.md's "named local vs expression": the variable
   is a `Color` candidate, the temporary a scratch pick.
   (re-verified, C++ game flags, outside EH regions, also with `this` as the base: `su1` to `su8`.)

   **4a. In a function with a C++ EH frame the "substituted" rows do not apply ahead of or inside an EH
   region. [C]** With a destructible local alive (`D d;` under `/EHsc`) or inside a `try` block, every
   single-use named local stays a variable at `Color`: `int t = p->m; return t + 1;` keeps `_t` (verify `su2_e`),
   so do `t = a * b; h(t)` (`su3_e`), `r = h(a); return r + 1` (`su4_e`), `int t = p->m; if (t) ..` (`su8_e`,
   `er4`), `x = a + b; y = a + b; x * y` (both `_x` and `_y` stay, the sum is computed once: `cs5_e`), a
   reference local `int& r = p->m` (`ehc9`), and the same bodies inlined from a `__forceinline` helper (`*_i`).
   What decides is where the definition is, not the function: the local also stays when it is defined and used
   *before* the region begins (`er2`, `z2`, `z4`), between two regions (`er3`) or in a `catch` block (`er8`);
   it is substituted as usual when no EH region can be entered after it (after the last destructor scope has
   closed: `ehs1`, `z1`, `z7`; after a `try`/`catch`: `eht1`; in an arm that does not lead to a region: `z3`,
   `z5`, `z6`). The control without an EH frame but with the same calls in front substitutes (`ehn1` to `ehn3`).
   Unaffected by EH: copies of a variable (rule 1), reverse copy propagation (rule 3), constant propagation
   (`ehc6`), CSE of expressions and loads (`cs1_e`, `ehc7`: one load, two variables), dead code (`ehd1`), all
   of §5, §6 and §8. The final instructions are usually the same; what changes is that the value is a `Color`
   candidate (frontend.md describes the consequences for registers). Mechanism **[I]**: GlobalOptimize's SSA
   pass for EH functions (`SsaDeriveEHWriteThroughScalars`, §12) presumably marks these definitions as
   write-through, which makes `GoptSingleUseCopyPropLegal` refuse them; not traced.
5. **A local used twice stays a variable**, initialised from the expression: `int t = a + b; return h(t) + t;`
   keeps `_t` (and, unlike the spelled-twice form, computes the sum once). **[C]** (c2 `cq06`, `cq10`; c3
   `ce15`.) (re-verified, C++ game flags: `tw1`, `cs10`)
6. **Two definitions**: the variable survives; each use reads the variable. **[C]** (c1 `cp02`, k1 `k14`: no
   constant is merged across the join either, the code is `mov eax,5; je; mov eax,7; add eax,eax`.) (re-verified, C++ game flags:
   `cp3`, `kj1`)
7. **A copy whose source changes later** keeps the old value in a variable only if it is still needed: `t = a;
   t++; return h(t) + t + a` keeps `_t = a + 1`. **[C]** (c2 `cq13`.) (not re-verified)
8. **Address-taken locals** (the address escapes to a call):
   - the variable lives in memory and every definition before a call is a real store; **[C]** (c1 `cp03`)
   - a copy **from** it (`u = t`) survives as a variable when a call follows, because the call may change `t`;
     uses before the next call read `t` itself; **[C]** (c2 `cq12`: `h(u) + u` pushes `t` and keeps `u` for the
     use after the call; c4 `at02`; (re-verified, C++ game flags: `at1`, `atc`)
   - between two calls it behaves like a normal variable: `t + t` is one read, and a store through an
     **incoming pointer parameter does not kill it**; **[C]** (c4 `at01`, `at03`, `at07`; (re-verified, C++ game flags: `atq`)
   - if the address is only used locally (`int* p = &t; *p += 1;`), the indirection is resolved and `t` is an
     ordinary variable; local arrays and structs whose address never escapes are tracked element by element
     (`arr[0] = 1; arr[1] = 2; return arr[0];` → `return 1`). **[C]** (c4 `at05`; d1 `ds08`, `ds09`, `ds12`; k1
     `k22`; (re-verified, C++ game flags: `at2`, `kb12`)
9. **Copies of memory values**: `x = p->m` cannot be replaced by `p->m` past a kill, so `x` survives across a
   call or an aliasing store (§5), and is substituted when nothing kills in between (rule 4). `t = a; g = t;
   h(t) + g` reads `a` for `t` but re-reads `g` after the call. **[C]** (c1 `cs04`..`cs10`; c2 `cq17`.) (re-verified, C++ game flags:
   `kcall`, `gcall`)

---

## 4. Common subexpressions

Mechanism: `HshBuildTable` 10b5eb30 gives equal expressions the same hash temp ("lexical" value numbering: the
operator, the type and the operands' value numbers; commutative operands are ordered). `GdfCalcAvail` 10b394c0
computes, per block, which hash temps are available on exit. In sweep 2 `GoptOptimizeBlock` 10b4fea0 starts each
block with the **intersection of its predecessors' available sets** (the `SbvCopy`/`SbvAnd` loop at its top) and
`GoptCommonSubExpression` 10b515e0 deletes a computation whose temp is available (`GoptIsSymAvail` 10b54440),
unless `GoptCantCse` 10b518f0 objects (side effects) or the tuple is marked too cheap. **[C]** (code)

1. **Scope is the whole function, all paths.** An expression computed in both arms of an `if` is available
   after the join (the temp then has two definitions); an expression computed before a branch is available in
   and after the arms. **[C]** (c1 `cs12`; c3 `ce18`, `ce26`; c5 `sg11`.) (re-verified, C++ game flags: `cs6`, `cs12`)
2. **No partial redundancy elimination.** Computed in one arm only, it is computed again after the join;
   nothing is hoisted above the branch. **[C]** (c1 `cs13`; c3 `ce25`.) (re-verified, C++ game flags: `cs7`)
3. **What qualifies** (each row **[C]** by the probe):

   | expression | shared? | probe |
   |---|---|---|
   | `a * b`, `a / b`, `a & b`, a compare result `(a < b)` | yes, also across a call (the temp is then a callee-saved candidate) | c1 `cs01`; c3 `ce12`, `ce13`, `ce27`; (re-verified, C++ game flags: `cs1`, `cs3`, `cs4`, `cs11`) |
   | `a + b`, `a << 2`, `p->m + 1` as an operand of a larger sum | no: flattened by §2 rule 2 before CSE | c3 `ce21`, `ce22`, `ce23`; k1 `k11`; (re-verified, C++ game flags: `cs2`) |
   | `a + b` assigned to two locals (`x = a + b; y = a + b; x * y`) | yes | c1 `cs02`; (re-verified, C++ game flags: `cs5`; in an EH region both locals stay variables, the sum is still computed once: `cs5_e`) |
   | a memory load `p->m`, `a[i]`, `g` | yes, until killed (§5); two locals loaded from the same place share one temp. **Exception: a pointer read for a null test, rule 8** | c3 `ce10`, `ce28`; c5 `sg13`; (re-verified, C++ game flags: `cs9`, `xx2`, `ehc7`, `q1` to `q14`) |
   | the address `p + off` of a member | "too cheap": computed once in the IL as a temp when the member is used on both sides of a kill, but folded back into each `[p+off]` operand before allocation; `p` is the candidate | c5 `sg09` (`regs`: one globreg, `p`); frontend.md |
   | a pointer loaded from memory (`p->next` in `p->next->m + p->next->n`) | yes, one temp | c4 `ch01`; (re-verified, C++ game flags: `cs8`) |
   | float arithmetic | yes under /fp:precise and /fp:fast, no under /fp:strict (§9) | f1 `cf02` |

4. **Too cheap** (`GoptCheckTooCheapCse` 10b4a6d0 sets tuple+0x1d = 3; `GoptCommonSubExpression` then only
   flags the tuple instead of deleting it, and invariant code motion copies it instead of moving it): `OPPAREN`;
   a float-to-wider-float `OPCONVERT`; a float `OPLOAD` unless /arch:SSE2 (or /arch:SSE for 4-byte floats);
   conversions of symbols with flag 0x1000000. **[C]** (code) The member-address case of the table is observed
   (**[C]** c5 `sg09`); which code marks plain `OPADD ptr, const` is **[I]** (not in this function).
5. **A compare repeated on a path where its outcome is known is removed** together with its branch
   (`GoptRemoveRedundantCmp` 10b517a0; compares are handled in sweep 1 too, through `_GoptDoRCE`). **[C]**
   (code; no probe isolates it from branch folding.) (not re-verified)
6. **Loops.** `GoptLimitCsesFromLoop` 10b51910 runs at each loop's last block in sweep 2 and removes from the
   available set expressions that the loop altered and that are not available on every exit. **[C]** (code;
   not probed separately.) (not re-verified)
7. **Source spelling.** Reading `p->m` twice and reading it once into a local give the same single load when
   nothing kills between the reads (the local is then substituted or becomes the CSE temp). They differ when
   (a) there is a kill between the reads: the spelled-twice form reloads, the local keeps the old value (§5); (b)
   the read is a call argument: the local stays a variable (§3 rule 4). **[C]** (c3 `ce14` vs `ce15`; c4 `mb01`
   to `mb03` show inline getters behave like the direct spelling.) (re-verified, C++ game flags: `cs9`, `cs10`, `su1`, `su2`; getters not
   re-verified.)
8. **A pointer that is null-tested and then dereferenced is not always one load. [C]** c1xx types the null test
   as an unsigned compare of the memory operand (`OPCMP [addr]:2004, 0`) and the dereference as a load of pointer
   type (`[addr]:1004`); the two get different hash temps, so CSE does not join them (IL of verify `g18` at
   `@GoptRemoveAssumes@4`: `t130:2004` for the test, `t133` for both dereferences). The address is shared. Seen:

   | source (no named local for the pointer) | code | probe (verify/v_g.cpp, v_q.cpp) |
   |---|---|---|
   | `if (one) return one->b + one->a;` (dereferenced twice in the block after the test) | `cmp [ecx+144],0; je; mov ecx,[ecx+144]` | `g18`, `g15` |
   | `if (items[i]) return items[i]->b;` with `items` a member (the element is reached through a loaded pointer), also `items[2]` | `cmp [eax+ecx*4],0; lea eax,[eax+ecx*4]; je; mov edx,[eax]` | `g1`, `g17`, `g21`, `g22` |
   | `if (one) return one->b;`, `if (p->next) return p->next->m;`, `if (it[i]) return it[i]->b;` with `it` a parameter (dereferenced once) | one load: `mov eax,[..]; test eax,eax` | `g9`, `q6`, `g8`, `g13`, `g16` |
   | `if (p->next && p->next->m) return p->next->n;` (the two dereferences are in different blocks) | one load | `q15` |
   | `E* e = items[i]; if (e) return e->b;` (a named local, or the base copied to a local) | one load | `g19`, `g20`, `q7`, `q16` |
   | an `int` member tested and used (`if (p->m) return p->m + 1;`): same type both times | one load | `q1` to `q5`, `g7` |

   Why the single-dereference cases are joined after all is **[I]** (in `g9` both loads carry the same hash temp
   after sweep 1; the zero arm then has an assertion `t130 <- 0`); the table is what was observed. For a
   decompiler: `cmp [mem],0` followed by a reload of the same pointer does not prove a store, a call or a
   `volatile` in between, and a named local for the pointer always gives one load. Fallout 3 0x00ad1250 and
   0x0044ec70 show the two-load form (§13).

---

## 5. What kills a memory value

The kill rules come from the alias ("points-at") sets built by interf.obj (`ItfBuildInterference` 10b73430) and
are applied through the hash table's kill lists (`HshUpdateKillVectors` 10b60210, `GoptUpdateAvail` 10b53f60).
The table is **[C]** by probe; the mechanism is **[I]**.

| between two reads of ... | ... there is | reload? | probe |
|---|---|---|---|
| `p->m` | a call (any, including `__declspec(noalias)`, also with `extern "C"`) | yes | c1 `cs04`; c3 `ce04`; c4 `na01`, `na04`; (re-verified, C++ game flags: `kcall`; `noalias` not re-verified) |
| `p->m` | `p->n = ..` (same base, other **constant** offset; also a `short` or `char` member) | no | c3 `ce01`; (re-verified, C++ game flags: `ksame`, `kb3`, `kb10`, `sf4`) |
| `p->m` | `p->arr[i] = ..` (same base, **variable index**) | **yes** | verify `karr`, `kb1`; in a loop `ic6` |
| `p->arr[i]` | `p->m = ..` (variable-index read, constant-offset store, same base) | **yes** | verify `kb2` |
| `p->arr[i]` | `p->arr[i + 1] = ..` | no | verify `kb8` |
| `a[i]` | `a[i + 1] = ..` | no | c3 `ce11`; (re-verified, C++ game flags: `aidx1`) |
| `a[i]` | `a[j] = ..` | yes | verify `aidxj` |
| `p->m` | `q->n = ..`, `q->m = ..` (other pointer, any offset, any pointee type; also `p->next->m = ..`) | yes | c1 `cs07`, `cs08`; c4 `sf06`; (re-verified, C++ game flags: `kqn`, `kqm`, `kb9`, `kb4`) |
| `p->m` (int) | `*q = ..` with `q` an `int*`, `char*` or **`float*`** | yes (no type-based rule) | c1 `cs05`, `cs06`; c3 `ce02`; (re-verified, C++ game flags: `kint`, `kchar`, `kflt`) |
| `p->m` | `*q = ..` with `p` and/or `q` declared `__restrict` (one is enough) | no | c3 `ce03`; c4 `rs01`, `rs02`; (re-verified, C++ game flags: `krs2`, `krs1`) |
| `p->m` | `g = ..` (a global) | yes | c1 `cs09`; (re-verified, C++ game flags: `kg`, `kloc`) |
| `p->m` | `sg = ..` (a `static` whose address is never taken) | no | verify `kb11` |
| `p->m` | a store to an address-taken local (`hv(&loc); x = p->m; loc = 5; y = p->m;`) | no | verify `kb14` |
| `g` | a call | yes | c1 `cs10`; c4 `na02`; (re-verified, C++ game flags: `gcall`) |
| `g` | `*q = ..` with `q` a pointer parameter | yes | verify `gptr` |
| `g` | `ga[i] = ..` (another global, variable index) | no | verify `kb5` |
| `g` | `g2 = ..` (another global) | no | c3 `ce29`; c4 `gl04`; (re-verified, C++ game flags: `gg2`) |
| `ga[i]` (global array) | `g = ..`; `ga[i + 1] = ..`; `gb[i] = ..` | no | c4 `gl02`, `gl03`; (re-verified, C++ game flags: `gaidx`, `gaidx1`, `kb7`) |
| `ga[i]` | `ga[j] = ..` | yes | verify `kb6` |
| `g` | `*p = ..` with `p` returned by a `__declspec(restrict)` function | no | c4 `rs03` (not re-verified) |
| `sg` (a `static` whose address is never taken) | `*q = ..` | no | c3 `ce19`; (re-verified, C++ game flags: `sgptr`) |
| `sg` | a call | yes (the call might re-enter this file) | c3 `ce20`; (re-verified, C++ game flags: `sgcall`) |
| address-taken local `t` | `*q = ..` with `q` a pointer parameter | no | c4 `at03`; (re-verified, C++ game flags: `atq`) |
| address-taken local `t` | a call | yes | c4 `at02`; (re-verified, C++ game flags: `atc`) |
| `p->m` | an assignment to a non-address-taken local, or to a local array whose address does not escape | no | c3 `ce06`; (re-verified, C++ game flags: `kloc2`, `kb12`) |

Further rules:

1. **Store-to-load forwarding**: `p->m = v; return p->m;` returns `v` without a load; likewise constants,
   computed values, call results, globals, `a[i]`, and across a store to another offset of the same base. A
   store through another pointer in between forces the reload. **[C]** (c3 `ce07`; c4 `sf01` to `sf06`; c5
   `sg01` to `sg06`.) (re-verified, C++ game flags: `sf1`, `sf2`, `sf4`, `sf5`)
2. One shape is not forwarded: `int* q = &p->n; *q = 1; return p->n;` stores and reloads (`mov [eax+4],1; mov
   eax,[eax+4]`). `*q` on both sides, or `p->n` on both sides, is forwarded. **[C]** (c2 `cq16`; c5 `sg03`,
   `sg01`.) Why is open (§12). (re-verified, C++ game flags, all four contexts: `sf3`)
3. **A store that writes the value already there is removed**: `p->m = v; p->m = v;` keeps one store; `p->m =
   p->m;` and `g = g;` disappear. Across a call the second store stays. **[C]** (c3 `ce08`, `ce09`; d1 `ds25`,
   `ds26`. Code: `GoptIsRedundantStore` 10b513a0, called from `GoptCommonSubExpression` for `OPASSIGN`.) (re-verified, C++ game flags:
   `rs1`, `rs2`, `rs3`)
4. **Partial kills**: killed on one path means reloaded after the join. **[C]** (c3 `ce17`; c5 `sg12`.) (re-verified, C++ game flags: `pk1`)
5. `__declspec(noalias)` on the callee changes nothing in these probes. **[C]** (c3 `ce04`, `ce05`; c4 `na01`
   to `na04`: the IL is the same as for a plain callee and the call's alias operand is the same set.) (not re-verified;
   see the addendum)
6. **Pointer chains.** `p->next` is a load like any other: a store through it (`p->next->m = 1`) may alias `p->next`
   itself, so the next `p->next` is loaded again, and the address `p + 8` becomes a shared temporary.
   With `P* q = p->next;` there is one load and `q` is a variable. Without a store in between, both spellings
   load once (a temp in one, the variable `q` in the other). **[C]** (c4 `ch01` to `ch06`; c1 `cs11`.) (re-verified, C++ game flags:
   `ch1`, `ch2`, `ch3`, `cs8`)

---

## 6. Dead code and dead stores

Mechanism: `GdfRemoveDeadStores` 10b36920 walks blocks backwards with live/dead sets (`GdfInstrLiveDead`
10b36da0); stores through pointers are tracked in a per-block list (`GdfUpdateDeadIndirList` 10b38190,
`GdfMarkNonDeadIndirs` 10b38140). It runs after every phase that can create dead code (table in §1). Rules are
**[C]** by probe; that the indirect-store list is per block is **[C]** by probe 6 and **[I]** from the function
names.

1. **Scalars: a definition never read is removed, across branches and loops.** `x = a*3; x = a*5` keeps the
   second; a definition overwritten on all paths goes even with a call in one arm (the call stays). **[C]** (d1
   `ds01`, `ds23`.) (re-verified, C++ game flags: `ds13`, `ehd1`, `ehd2`)
2. **Unused computations go, including ones that could trap**: an unused division, an unused load. An unused
   call result keeps the call. **[C]** (d1 `ds13`, `ds14`, `ds15`.) (re-verified, C++ game flags: `ds10`, `ds13`)
3. **A loop with no effect is removed entirely** (`LoptRemoveEmptyLoop` 10b461b0). **[C]** (d1 `ds24`.) (re-verified, C++ game flags:
   `ds11`)
4. **Store through a pointer followed by a store to the same address in the same block**: the first is removed
   when nothing between them may read it. A store through another pointer or to another offset is not a read; a
   load through another pointer, or a call, is. **[C]** (d1 `ds02`, `ds19` removed; `ds04` removed with `q->m =
   3` in between; `ds05` kept because of `t = q->m`; `ds03` kept because of the call.) (re-verified, C++ game flags: `ds1` to `ds4`)
5. The same for globals: `g = 1; g = 2` keeps one store; with a call in between both stay. **[C]** (d1 `ds10`,
   `ds11`.) (re-verified, C++ game flags: `ds6`, `ds7`)
6. **Not across blocks**: `p->m = 1; if (c) p->m = 2; else p->m = 3;` keeps all three stores in the IL. **[C]** (d1
   `ds20`.) In the final code this particular shape has two: a later pass turns the two arms into one
   conditional value (`neg ecx; sbb ecx,ecx; add ecx,3`), giving `mov [eax+36],1; mov [eax+36],ecx`; the first
   store is still not removed. **[C]** (d1 `ds20` listing; verify `ds5`, all contexts.)
7. **Address-taken locals**:
   - a store after the last call that could read the variable is dead (the variable dies at return): `hv(&x);
     x = 5; return a;` has no store of 5; **[C]** (d1 `ds06`, `ds21`; (re-verified, C++ game flags: `ds8`). Implicit destructor calls
     count as calls: with a destructible local still alive the store stays (verify `ds8_e`: `mov [x],5555`
     before `call D::~D`); **[C]**
   - a store before a later call is live even if this function never reads it again: `hv(&x); x = 7; h(1);`
     keeps the store; with two stores and a call between them the first stays and the second goes; **[C]** (d1
     `ds07`, `ds16`, `ds22`; (re-verified, C++ game flags: `ds9`)
   - two stores with no call between them: the first goes (`x = a; x = a + 1`); **[C]** (d1 `ds18`; (re-verified, C++ game flags: `ds14`)
   - the same for struct locals passed by address: fields stored after the last call are dead. **[C]** (d1
     `ds17`) (not re-verified)
8. `t = p->m; h(1); p->m = t;` keeps load and store (the call may have changed `p->m`). **[C]** (d1 `ds27`.) (re-verified, C++ game flags:
   `ds12`)

---

## 7. Constants

1. **Constant propagation is through single definitions and through branch facts.** `x = 3; y = x*4` folds.
   On the true arm of `if (a == 5)`, `a` is 5 (`return a*2` → 10). `if (a == b) return a - b` is **not** folded
   to 0: only facts against constants are used. **[C]** (k1 `k15`, `k16`, `k17`, `k23`. Code:
   `GdfInsertAssertions` 10b35240 inserts the facts, `GoptCopyPropagation`/`GoptConstantFold` 10b53290 use them. (re-verified, C++ game flags:
   `kf4`, `ke5`, `keb`.)
2. **No value-range reasoning in these probes**: `if (a > 10) { if (a > 5) ...` keeps the inner compare.
   **[C]** (k1 `k18`; (re-verified, C++ game flags: `krg`.) `GoptRangeCmpFold` 10b53b40 exists and is called for compares in sweep 1; what it folds
   is open (§12).
3. **No merge of constants at a join**: `x = 5; if (c) x = 7; return x*2;` keeps `x` as a variable. **[C]**
   (k1 `k14`; (re-verified, C++ game flags: `kj1`.)
4. **Branches on known conditions are deleted** with their dead arm (`GoptConstantFoldCompareBlk` 10b55460,
   `GoptRedirectConstantCompareJump` 10b55b20). **[C]** (code; l1 `lp12`: the guard `0 < 4` is gone; (re-verified, C++ game flags: every `ut*` probe.)
5. **A constant in a register** is not decided here but in `Globregs`/`Color` (immediate candidates,
   `CmdIdentifyImmedCandidates`, regalloc.md and regalloc_cost.md). What the optimizer contributes is which
   constants remain as operands. **Corrected by the verification pass**: the first version of this table gave
   "2 stores: immediates, 3 or more: a register"; that held only because its probes stored to `[eax]` and
   `[eax+4]`. What counts is the number of instructions that carry **both the constant and a displacement**
   (`[reg+disp]`, `[reg+reg*s+disp]` or an absolute address). Observed, **[C]** (probes in verify/v_i.cpp, v_x.cpp,
   v_y.cpp, v_w.cpp, v_h.cpp; l4 and k1 for the older rows):

   | uses of the same constant | result | probe |
   |---|---|---|
   | 2 or more stores with a displacement: `p->b = K; p->c = K;` (offsets 4, 8), two globals, K = 0x12345, 5, 0 or -1; also byte and mixed-size stores | one `mov reg, K` (`xor reg,reg` for 0, `or reg,-1` for -1, `mov cl,1` for bytes), then `mov [mem], reg` | `imBK`, `imBZ`, `imBF`, `imEK`, `imLK`, `imb2`, `imz2`, `ims2`, `imm2`, `imJ`, `imx3`, `img3`, `imM`; l4 `im02`, `im03`, `im05`, `im06`; k1 `k12`, `k13` |
   | 2 stores of which only one has a displacement: `p->a = K; p->b = K;` (offsets 0, 4), `p->a = K; p->e[5] = K;`, `g = K; p->a = K;` | `mov [mem], imm` twice | `imAK`, `imAZ`, `imAF`, `imDK`, `imFK`; l4 `im01`, `im04` |
   | stores without displacement only, any count: `*a = K; *b = K; *c = K;` | immediates | `nd3`, `nd4` |
   | 3 stores at offsets 0, 4, 8 (two with a displacement) | register | `imTK`, `nd5` |
   | `add`/`and`/`or`/`cmp` **to memory** with a displacement, twice (`p->b += K; p->c += K;`, `if (p->b == K) .. if (p->c == K)`) | register: `mov ecx,K; add [eax+4],ecx; add [eax+8],ecx` | `imR`, `imS`, `nd7` |
   | `and`/`add` immediate with a **register** destination, any count | stays an immediate | l4 `im07`, `im08`; k1 `k11`; verify `ima`, `imQ`, `nd6` |
   | pushed as an argument, any count | `push imm` | k1 `k19`, `k20`; verify `imp` |
   | two different constants | counted separately (`K, K2, K`: K in a register, K2 immediate) | `nd8` |
   | one store inside a loop | stays immediate | l4 `im09`; verify `iml` |
   | a call inside the constant's range (the register would have to be callee-saved), 2 or 3 uses | stay immediates | l4 `im10`; verify `imc3`, `imG`, `imH`, `imI`, `wd2`, `wd3` |
   | the same with 4 or more uses | a callee-saved register (`xor esi,esi` / `mov edi,K` in the prologue area) | `imc4`, `imz4c`, `wd4`, `wd6`, `wd8` |

   The range is that of the constant in the whole function, and every use of the value counts as part of it, so
   an earlier `cmp [mem], 0` puts a later run of zero stores "across a call" if a call lies between: `void
   clr3(int c) { if (c) { fr(data); data = 0; cap = 0; n = 0; } }` keeps three `mov [esi+N], 0`, while the same
   three stores right after a call with no earlier use of 0 use `xor eax,eax` (verify v_h.cpp `clr3` against `clr`,
   `clr4`). **[C]** The thresholds are the outcome of `Color`'s cost for immediate candidates, not of a counter
   here **[I]**. The member-function variants behave like the plain ones.
   **EH state stores count as uses. [C]** In a function with an EH frame the state updates `mov [__$EHRec+8], N`
   (N = 0, 1, 2, ... and -1) are stores of a constant with a displacement and belong to the same candidate as
   the user's own stores of that value: with two stores of -1 before the final state update the listing is `or
   ecx,-1; mov [eax+36],ecx; mov [eax+40],ecx; mov [__$EHRec+8],ecx` (verify `imm2_e`); with three stores of 0
   after calls the state-0 update at the top is the fourth use and everything goes through a callee-saved
   register, `xor esi,esi; mov [__$EHRec+8],esi; call ..; mov [eax+36],esi; ..` (`imz3_e`); with two stores of 0
   it is the third use across a call and all three stay immediates (`imz2_e`). So an EH state stored from a
   register means the function uses that same small constant elsewhere in a displacement instruction.

6. **`x * K`, `x / K`, `x % K` are expanded in `Mul2Shift` 10baa730** (`MsGenMulShift` 10ba9550,
   `MsSignedDivide` 10ba9ba0, `MsUnsignedDivide` 10baa0d0), after GlobalOptimize and before `AddrModes`: at
   `@GrgIdentifyCandidates@4` the IL still has `OPMUL _a 7`, at `@AddrModes@4` it has `t*8` and `OPSUB`. **[C]**
   (k1 `k02`, `k09` dumps; not re-verified.) So CSE, strength reduction and factoring all see the multiplication, never the
   shifts. The /O2 shapes (`work/scratch/opt/m1.cpp`, **[C]**; (re-verified, C++ game flags, free function and EH function: `mu2` to
   `mu1000`, `mun1`, `mun3`, `mun5`, 39 multipliers, all rows below reproduced):

   | K | /O2 code |
   |---|---|
   | 2, 4, 8 | `add eax,eax` once, twice, three times |
   | 3, 5, 9 | `lea eax,[eax+eax*2/4/8]` |
   | 6, 10, 18, 12, 20, 36, 24, 40, 72 | the `lea`, then 1 to 3 `add eax,eax` |
   | 7, 14, 28 | `lea eax,[ecx*8]; sub eax,ecx`, then 0 to 2 `add eax,eax` |
   | 15, 31 / 17, 33 | `mov eax,ecx; shl eax,4/5; sub/add eax,ecx` |
   | 30, 60, 34 | the previous shape, then `add eax,eax` |
   | 16, 32, 64, ... (2^n, n ≥ 4) | `shl eax,n` |
   | 48, 80, 96 | `lea`, then `shl eax,4/5` |
   | 11, 13, 19, 21..23, 25..27, 29, 35, 37..39, 41, 50, 81, 100, 255, 257, 1000 | `imul eax,K` |
   | -1 / -3 / -5 | `neg eax` / `lea eax,[ecx*4]; sub eax,ecx; neg eax` / `mov eax,ecx; neg eax; add eax,eax; add eax,eax; sub eax,ecx` |

   At /O1 only powers of two are expanded (`add eax,eax` for 2, `shl` otherwise); everything else is `imul`.
   **[C]** (/O1 not re-verified) Division: `/4` → `cdq; and edx,3; add; sar`, `/3` → magic `imul` + sign fix, unsigned `/10` → `mul`
   + `shr`, `%8` → `and 80000007h; jns; dec; or; inc`. **[C]** (k1 `k07` to `k10`; (re-verified, C++ game flags: `dv4`, `dv3`, `dvu`, `md8`.)

---

## 8. Loops

### 8.1 Shape on entry: rotation (`LgZtripTest1` 10b871a0)

c1xx emits `while`/`for` with the test at the top and a `goto` at the bottom (l1 `lp10` at `@MscRVO@4`).
`LgBuildLoopGraph` duplicates the test: at `@OptFuncExpr@8` the loop is `test; branch-over; L: body; test;
branch L`. **[C]** (`gstage.sh l1 lp10`.) See il_fg.md §5.1 and rule 8 there.

1. `for`, `while`, `for(;;) { if (..) break; ..}` with the test first, and a `goto`-built loop with the test
   first all give the same rotated loop. `do { } while` and `for(;;) { ..; if (..) break; }` have no guard.
   **[C]** (l1 `lp01`, `lp10`, `lp11`; l4 `in04`, `in05`, `in06`; (re-verified, C++ game flags: `iv1`, `rt3`, `rt4`.) "No guard" for the
   `for(;;)` form needs the `break` to be the last thing in the iteration (`if (++i >= n) break;`). With the
   step after it (`for (i = 0;; i++) { s += a[i]; if (i >= n) break; }`) the test is in the middle: everything
   up to the test is the "first test block" and is duplicated in front of the loop (`mov eax,[edx]; test
   esi,esi; jle` and then a loop over `[edx+ecx*4+4]`). **[C]** (verify `rt5`.)
2. **Only the first test block is duplicated.** With `i < n && a[i] != m` the guard is `n <= 0`, the bottom test
   is `i < n`, and `a[i] != m` stays at the top of the body. A call in the test is duplicated (two call sites).
   **[C]** (l4 `in01`, `in02`, `in03`; (re-verified, C++ game flags: `rt1`, `rt2`.)
3. Not duplicated: a test block containing inline assembly or one particular intrinsic call (`LgDontDupLoopTest`
   10b87f10: tuple kind 0x13 with id 0xde, or opcode `OPASM`). **[C]** (code; not probed; not re-verified.)
4. The guard is then simplified like any compare: `i = 0; i < n` becomes `0 < n` (`test reg,reg; jle`), and
   with a constant bound it disappears. **[C]** (every l1 probe; (re-verified, C++ game flags: every `iv*` and `ut*` probe.)

### 8.2 Invariant code motion (`GoptCodeMotionBlock` 10b504c0, `GoptInvariantCodeMotion` 10b51a60)

Per loop, inner loops first (`GoptOptimizeLoopHelper` 10b50360), before strength reduction. An instruction is
moved to the end of the preheader when, as read in 10b51a60:
- its result is a hash temp (an expression, or an assignment to a temp that is dead outside), and the
  expression is "available" in the loop-invariant sense (all operands unchanged in the loop: the bit vectors
  from `GdfCalcLoopAvail` 10b396f0);
- **and** its block dominates all loop exits (`LoptBlockDominatesAllExits` 10b436e0), or the expression is
  already available at the end of the preheader or of the exit pad;
- **and** it has no side effect (`TupQuerySideEffectHelper`).
A too-cheap instruction (§4 rule 4) is copied to the preheader and left in the loop as well. **[C]** (code)

Probes, all **[C]**:

| in the loop | hoisted? | probe |
|---|---|---|
| `x * y`, `x + 5`, `x * 5` executed every iteration (also as a call argument) | yes, one temp in the preheader | l1 `lp31`; l3 `ic02`, `ic09`, `ic10`; (re-verified, C++ game flags: `ic1`) |
| `x * y` or `x / y` under an `if` in the body | no | l3 `ic01`; l1 `lp32`; (re-verified, C++ game flags: `ic2`) |
| `p->m` every iteration, loop has no store and no call | yes, the load moves out | l1 `lp05`; l3 `ic04`; (re-verified, C++ game flags: `ic3`, also `this->m`) |
| `p->m` under an `if` | no | l3 `ic03` (not re-verified) |
| `p->m`, loop stores through another pointer (`q[i] = ..`, `a[i] = p->m`) | no, reloaded every iteration | l1 `lp07`; l3 `ic05`, `ic06`; (re-verified, C++ game flags: `ic4`) |
| `p->m`, loop stores through the **same base with a variable index** (`p->arr[i & 7] += p->m`) | no, reloaded every iteration (§5) | verify `ic6`, also with `this` |
| `p->m` or `p->n`, loop has a call | the load stays; only the address `p + off` is hoisted in the IL (nothing for offset 0), and it is folded back before allocation: the final loop has `add edi,[ebp+40]` with `p` in the register and no `lea` in the preheader | l1 `lp06`; l3 `ic08`; frontend.md f07; (re-verified, C++ game flags: `ic5`) |
| `p->next->m` every iteration, no store | both loads hoisted | l3 `ic16`; (re-verified, C++ game flags: `ic11`) |
| a global `g` read in the loop | not by this pass (a global is an operand, not an expression); `Globregs` loads it into a register before the loop when no call intervenes | l3 `ic12` (IL keeps `g` in the loop, code has `mov edi, g` before it), `ic11`; (re-verified, C++ game flags: `ic8`) |
| the loop bound | never recomputed if it is a parameter or local; a global bound is re-read every iteration when the loop stores through a pointer; so is a **member** bound (`for (i = 0; i < p->n; i++) p->arr[i] = ..`: `cmp eax,[edx+40]` at the bottom), while with no store in the loop the member bound is loaded once | l1 `lp24`; (re-verified, C++ game flags: `ic10`, `iv14`, `bm9`, `iv13`) |
| a loop-invariant branch (`if (k > 3)` in the body) | no unswitching | l1 `lp30`; (re-verified, C++ game flags: `ic9`) |

### 8.3 Scalar replacement (`LoptScalarReplace` 10b449d0)

`for (..) p->n += i;` with no other memory access in the loop: the member is loaded into a temp in the
preheader, updated in the loop, and stored once after the loop. With a load of other memory in the loop
(`g += a[i]`, `*out += a[i]`) this pass does not apply; `Globregs` still keeps the global in a register but
writes it back on every iteration. **[C]** (l3 `ic07`; `ic13`, `ic14`; (re-verified, C++ game flags: `ic7`, also `this->n`; the `g +=` cases not re-verified.)

### 8.4 Induction variables

Order inside `LoptOptimizeLoop` 10b39c50 (**[C]**, read there; it runs only for loops of at most
`_GoptLoopOptimizeThreshold` blocks, default 100 at 10d0d390, `/d2Loop#`): `LoptFindInductionVariables`
10b3bd20, `LoptCalcLoopCount` 10b3fe50, `LoptCombineInductionVariables` 10b46c00, `LoptScalarReplace`,
**`LoptStrengthReduce` 10b3e000**, `LoptEliminateInductionVariables` 10b40c00, `LoptBlockMoveInit` 10b43e60,
`LoptCombineInductionVariables` again, `LoptLinearFunctionTestReplace` 10b40960, **`LoptBuildAddressModes`
10b47a90**, **`LoptCountedLoop` 10b404f0**, `LoptHoistFinalValues` 10b44660, `LoptRemoveEmptyLoop` 10b461b0,
then a block sweep without CSE and `LoptCleanUpLoop` 10b48e20.

What the stages do to `s += a[i]` and to `a[i] = b[i]` (**[C]**, `lstage.sh l1 lp01`, `lstage.sh l1 lp03`):
1. Strength reduction makes a pointer for **every** address: `t154 = a + i*4` initialised in the preheader and
   incremented by a step temp at the bottom. `i` and its test are still there.
2. Combine: pointers with the same step and different bases are merged. One is kept (`t155` from `a`), the
   other becomes `t155 + (b - a)`, with `b - a` computed in the preheader.
3. Build address modes: a pointer derived from a basic induction variable with multiplier 1, 2, 4 or 8 is
   turned back into `[base + i*scale]` **if every use of it can absorb that** (a memory operand, or an add of a
   constant which becomes a displacement). If one use cannot (for example the add of the variable `b - a`), the
   pointer stays (`LoptUndoOplea` 10b48a10 path). **[C]** for the multiplier test and the all-uses loop (read at
   10b47b74..10b47f09); the exact use rules are **[I]**. Observed **[C]**: the fold happens only when one access is at
   displacement 0 from a base variable, i.e. the pointer's start value is the plain base (`a[i]`, `a[i] + a[i+1]`,
   `p->pad[i] + p->arr[i]` with `pad` at offset 0, a global array). When every access has a non-zero
   displacement the start value is `base + d`, the pointer stays and `i` becomes a down-counter (verify `iv13b`,
   `iv13c`, `iv13e`, `zi1` to `zi4`). This is also why `a[i - 1]` keeps a pointer (the old open question).
4. Counted loop: if the basic variable `i` now has no use other than its increment and the exit test, and the
   trip count is computable (single exit at the bottom, bound invariant), `i` is deleted and a down-counter
   initialised with the trip count takes over the test.

Resulting rules, **[C]** by the probes named:

| loop body (index `i` from 0 to `n`, step 1, unless noted) | result | probe |
|---|---|---|
| one array, element size 1, 2, 4, 8 (`char`, `short`, `int`, `double`); also `a[i] + a[i+1]`, `a[i] += 1`, bounds `<`, `<=`, `!=`, start `lo` | `[a+i*size]`, `i` counts up, `cmp i,n`. **Condition: one access has displacement 0 from the base variable** (`a[i]`; a global array `ga[i]` qualifies too) | l1 `lp01`, `lp20`; l2 `iv01`, `iv02`, `iv04`, `iv05`, `iv20`, `iv21`, `iv29`; (re-verified, C++ game flags: `iv1`, `iv6`, `iv13d`, `zi1`) |
| **a member array `p->arr[i]` or `this->arr[i]` at a non-zero offset; `a[i + 1]` or `a[i + 3]` alone; `a[i+1] + a[i+2]`; `p->arr[i] += 1`** | pointer started with `add reg, offset` (or `lea`) in the preheader, `+= 4`, `i` replaced by a down-counter (`sub reg,1; jne`) | verify `iv13b`, `iv13c`, `iv13e`, `zi2`, `zi3`, `zi4`, `iv13` |
| the same member array with a call taking `i`, or with `i` stored (`p->arr[i] = i`) | pointer for the array and `i` kept counting up | verify `zi5`, `iv14` |
| one array, element size 12 or 16 (struct), one or two fields | pointer `+= size` (starting at the first used field's address or base), `i` replaced by a down-counter | l1 `lp09`; l2 `iv03`, `iv08`, `iv30`; (re-verified, C++ game flags: `iv2`, `zi6`) |
| two arrays `a[i]`, `b[i]` (any element size) | one pointer; the other array is `[ptr + (b-a)]` with `sub` in the preheader; down-counter | l1 `lp03`, `lp16`, `lp29`; l2 `iv24`; (re-verified, C++ game flags: `iv3`, `bm3`) |
| three arrays | one pointer and two differences | l2 `iv07` (not re-verified) |
| two arrays and `i` used as a value (`a[i] = b[i] + i`) | pointer and difference, **and** `i` kept counting up for the value and the test | l2 `iv06` |
| one array and `i` used as a value (`s += a[i] + i`, `h(i)`) | `[a+i*4]`, `i` kept | l1 `lp25`, `lp15`, `lp26`; (re-verified, C++ game flags: `iv6`) |
| struct array and a call taking `i` | pointer for the array, `i` kept in its own register | l2 `iv13`; (re-verified, C++ game flags: `iv12`) |
| `short` or `unsigned char` index | pointer and down-counter (the index variable is gone) | l2 `iv22`, `iv23`; (re-verified, C++ game flags: `iv4`) |
| `a[i - 1]` with `i` from 1; `a[n - i - 1]` | pointer (ascending / descending) and down-counter | l2 `iv12`, `iv18`; (re-verified, C++ game flags: `iv5`) |
| `i` running down (`for (i = n-1; i >= 0; i--) s += a[i]`), step 2 (`i += 2`) | `[a+i*4]`, `i` kept | l1 `lp17`, `lp18`; (re-verified, C++ game flags: `iv11`, `ust2`) |
| nested `a[i*m + j]` | inner pointer `+= 4` restarted from an outer pointer `+= m*4`; two down-counters | l1 `lp21` |
| `i * 12`, `i * 5`, `i * k` (k invariant) as a value | a second variable `+= 12` / `+= 5` / `+= k`; `i` replaced by a down-counter | l1 `lp08`; l2 `iv16`, `iv17`; (re-verified, C++ game flags: `iv7`) |
| `i * 4` as a value | not reduced: `lea eax,[eax+ecx*4]`, `i` kept | l2 `iv15`; (re-verified, C++ game flags: `iv8`) |
| `i * 3` stored with `a[i]` | `[a+i*4]` with `i` kept, plus a second variable `+= 3` | l2 `iv28` |
| `i + 123456` as a call argument | computed from `i` each iteration (`lea`), no second variable | k1 `k21` |
| written with a pointer: `for (p = a; p < a + n; p++)` | stays a pointer compared (unsigned) against an end pointer computed once before the loop; no down-counter | l2 `iv10`; (re-verified, C++ game flags: `iv9`) |
| `while (n--) s += *a++;` | stays as written: pointer `+= 4`, `n` counted down with a separate `test` | l2 `iv11`; (re-verified, C++ game flags: `iv10`) |
| exit test on loaded data (`for (i = 0; q[i].a; i++) s += q[i].b`) | no strength reduction: `q + i*12` recomputed each iteration with two `lea` | l2 `iv19` (cause open, §12) |

**Down-counter rules** (`LoptCountedLoop`), **[C]**:
- The count is `n` for `i < n` from 0, `n + 1` for `i <= n`, `n - m` for `i` from `m`, and for step 3 it is
  `(n - 1) / 3 + 1` computed with a magic multiply in the preheader. (l5 `cl03` to `cl06`, `cl02`; (re-verified, C++ game flags: `cl1`,
  `cl3`, `iv5`; step 3 not re-verified.)
- The loop then ends in `sub reg,1; jne`. The counter is unsigned in the IL (type 0x2004).
- No down-counter when the loop has a second exit (`break`): the count is not computed and `i` is kept (l5
  `cl01`; l1 `lp22`, `lp23`; (re-verified, C++ game flags: `cl2`, `rt2`). `LoptCalcLoopCount` 10b3fe50 requires the tail block to be the only exit and
  the header to have exactly two predecessors. **[C]** (code)
- A loop already counting down against 0 with a signed test is left alone (`sub esi,1; test esi,esi; jg`).
  (l2 `iv26`.)
- If `i` is read after the loop and was eliminated, the code after the loop uses its final value directly
  (`n` for `i < n` from 0: `add eax,esi` with `n` in esi), set by `LoptHoistFinalValues` 10b44660; the
  down-counter is then a copy of `n` so that `n` survives. With one int array `i` simply stays. (l6 `fv01`,
  `fv02`; l2 `iv09`; (re-verified, C++ game flags: `fv1`.)

### 8.5 Values read by the loop test

After rotation the test's operands are evaluated in the guard and again at the bottom: `while (*s++)` and
`for (; p; p = p->next)` compare memory directly in both places, with no temporary. A value the test and the
body both need is one temporary: in `if (a[i] < 0) break; s += a[i];` the element is loaded once per iteration.
**[C]** (l1 `lp14`, `lp27`, `lp23`; (re-verified, C++ game flags: `lt1`, `lt2`, `rt2`.)

### 8.6 Block fill and copy (`LoptBlockMoveInit` 10b43e60)

| loop | result | probe |
|---|---|---|
| `a[i] = 0` or `a[i] = v`, `int` elements, `for` shape | `rep stosd` (an `OPINTRINSIC` in the IL), guarded by `n > 0` | l1 `lp02`, `lp28`; (re-verified, C++ game flags: `bm1`, `bm11`) |
| a member array filled up to a parameter bound (`for (i = 0; i < n; i++) p->arr[i] = 0`, also `this->arr`) | `rep stosd` | verify `bm7` |
| the same with a **member** bound (`i < p->n`) | no: the bound is re-read every iteration (§8.2), pointer loop with `mov [ecx],0` | verify `bm9` |
| the same with `short` elements | `rep stosd` with the value replicated, then `rep stosw` | l5 `bm04`; (re-verified, C++ game flags: `bm10`) |
| the same with `char` elements | `call _memset` | l5 `bm03`; (re-verified, C++ game flags: `bm2`) |
| constant trip count 16, zero fill | 16 inline stores from one zeroed register | l5 `bm05`; (re-verified, C++ game flags with trip count 8: `bm8`) |
| `a[i] = b[i]` with pointer parameters | **not** a block move (may overlap): pointer + difference loop | l1 `lp03`, `lp29`; (re-verified, C++ game flags: `bm3`) |
| `ga[i] = gb[i]` (two global arrays); `a[i] = b[i]` with both `__restrict` | `rep movsd` | l5 `bm01`, `bm02`; (re-verified, C++ game flags: `bm4`, `bm5`) |
| three fields of a struct array set to 0 | no: pointer loop with three stores | l5 `bm06` (not re-verified) |
| fill with a call in the loop; `do { a[i] = 0; } while (++i < n)`; descending fill | no | l5 `bm07`, `bm08`, `bm09`; (re-verified, C++ game flags for the `do`-`while` form: `bm6`) |

All **[C]**. The fill is recognised at /O1 too (l1_O1 `lp02`).

### 8.7 Unrolling (lur.obj, /O2 only)

`LurWorthIt` 10b8db00: unrolling runs only if `_Speed` is set (/O2, not /O1), `/d2NoLUR` is not given, the
function is not being PGO-instrumented and it has loops. It also sets the mode flags: complete unrolling on,
fixed-amount on, modulo (remainder-loop) unrolling on, peeling off, `do`-`while` and `while` class unrolling off,
reduction unrolling off. **[C]** (code) Only innermost loops are considered (`LurUnrollLoop` 10b90e40 requires
no child loop) and the step must be +1 or -1 (`LurUnrollDoLoop` 10b95bf0). **[C]** (code; l3 `ur16` step 2
not unrolled, `ur15` only the inner loop; (re-verified, C++ game flags: `ust2`, `unest`.) Unrolling behaves the same in a member
function, in an EH function and in inlined code (every `ut*_e`, `u*_i` variant matches its plain function).

The amount (`LurDetermineDoUnrollAmount` 10b91160, counts from `LurCountTuples` 10b8e090), **[C]** (code):
1. A loop containing a call or another inadmissible tuple is never unrolled.
2. **Complete unrolling** (`LurCompletelyUnroll` 10b91590): constant trip count ≤ 10, at most 4 blocks, fewer
   than 45 tuples. (re-verified, C++ game flags at trip count 7, which no later rule can unroll: a body with one `if` is unrolled, with
   two it is not (`pb1`, `pb2`); 7 statements `s ^= a[i] >> k` are unrolled, 8 are not (`ps7`, `ps8`).) A loop that
   fails these limits is not thereby left alone: rules 4 and 5 still apply, and when the trip count itself is
   one of their factors (2 to 6) the loop is unrolled by its whole trip count and ends up as straight-line
   code all the same (trip 4 with 12 `if`s or 20 statements: verify `zb12`, `us20`). **[C]**
3. Otherwise none if: more than 128 tuples (re-verified, C++ game flags: `pq30`, `zb14`); or the loop stores to memory but loads
   nothing; or it has no float operation and its number of loads equals its number of stores (a copy loop, and
   also a loop that touches no memory: l6 `fv03`, trip 12, is not unrolled).
4. Integer loop with more than 50 tuples and a constant trip count: 5, 4, 3 or 2, the first that divides it.
   (re-verified, C++ game flags: trip 12 is unrolled by 6 with a small body and by 4 once the body has 12 or more statements: `pq4`,
   `pq8`, `pq12` to `pq24`.)
   **Then** none if the loop has more than 15 blocks (`0xf < blocks` at 10b91160 comes after rule 4, not before
   it as the first version said; an integer loop with that many blocks has more than 50 tuples, so for a
   constant trip count rule 4 decides first: 25 blocks, trip 4, unrolled by 4: `zb12`). **[C]** (code, probe)
5. Otherwise, with a constant trip count: the largest factor from 6 down to 2 that divides it (from 10 down
   for float loops without float compares). No divisor means no unrolling by this rule.
   (re-verified, C++ game flags: integer 12→6, 14→2, 15→5, 16→4, 18→6, 20→5, 21→3, 22→2, 25→5, 64→4, 100→5, and 11, 13, 49 not
   unrolled: `ut*`; float 14→7, 16→8, 20→10, 27→9: `uf*`.)
6. Unknown trip count: 4 (8 for a float loop with no loads or stores and fewer than 85 tuples), carried out
   only when the loop qualifies for modulo unrolling. **A constant trip count that rule 5 could not divide
   gets 8** (the function's final `return 8`; 4 is returned only when the count is unknown), again only
   for loops that qualify for modulo unrolling, which in the probes means float loops: `float` sums of 11 or
   13 elements become 8 straight-line additions and a remainder loop from `i = 8`, of 17, 19 or 23 elements a
   main loop of two rounds of 8 and a remainder loop (verify `uf11`, `uf13`, `uf17`, `uf19`, `uf23`); the
   integer loops of 11, 13 and 49 are left alone. **[C]** (code for the 8, probes for the rest)

Probes, **[C]**:

| loop | result | probe |
|---|---|---|
| `s += a[i]`, trip 2, 4, 10 | straight-line code, no loop | l3 `ur01`, `ur02`; l1 `lp12`; (re-verified, C++ game flags: `ut2`, `ut4`, `ut9`, `ut10`, `umem`) |
| trip 11, 13 (integer) | not unrolled (prime, above 10) | l3 `ur03`, `ur05`; (re-verified, C++ game flags: `ut11`, `ut13`, `ut49`) |
| trip 11, 13, 17, 19, 23, `float` sum | by 8 with a remainder loop (rule 6) | verify `uf11` to `uf23` |
| trip 12 / 64 / 100 | by 6 (2 iterations) / by 4 (16) / by 5 (20); pointer `+= 4*factor` with displacements around it, down-counter | l3 `ur04`, `ur06`; l1 `lp13`; (re-verified, C++ game flags: `ut12`, `ut64`, `ut100`) |
| trip 14, 22 | by 2, and the index form is kept: `mov esi,[edx+ecx*4+4]; add esi,[edx+ecx*4]; add ecx,2; cmp ecx,14` (no pointer, no down-counter) | verify `ut14`, `ut22` |
| `a[i] = i`, `a[i] = b[i]`, trip 4 | complete | l3 `ur07`, `ur08`; (re-verified, C++ game flags: `uc4`) |
| `a[i] = b[i]` or `a[i] = b[i] + 1`, trip 16 | not unrolled (loads == stores) | l3 `ur09`, `ur10`; (re-verified, C++ game flags: `uc16`) |
| call in the body, trip 4 | not unrolled | l3 `ur11`; (re-verified, C++ game flags: `ucall`, `pc1`) |
| 3×3 nest | inner unrolled completely, outer loop stays | l3 `ur15`; (re-verified, C++ game flags: `unest`) |
| `do { } while (i < 4)` | **not unrolled**; the same loop written `while`/`for` is | l3 `ur17`, `ur18`; (re-verified, C++ game flags: `udo`, `uwh`) |
| `int` loops, unknown `n` | not unrolled | l1 `lp01`; l3 `ur14`; (re-verified, C++ game flags: `uin`, `iv1`) |
| `float` sum or `a[i] = b[i]*2.0f`, unknown `n` | by 4: guard `n < 4`, main loop with count `((n-4)>>2)+1`, then a remainder loop | l3 `ur12`, `ur13`; (re-verified, C++ game flags: `ufn`) |

---

## 9. Floating point (x87, default /arch)

1. /fp:precise (the default): float arithmetic is done in double temps (`OPCONVERT` to type 0x5008). A shared
   product is a double temp; across a call it is stored as an 8-byte stack temp (`fst QWORD PTR tv`) and read back
   with `fadd QWORD PTR`. **[C]** (f1 `cf02`, `cf05`; (re-verified, C++ game flags: `fl3`, `fl6`.)
2. A float **load** is not shared under x87: `x = p->f; ...; x + p->f` reads memory twice even with no kill;
   `p->f * p->f` reads once only because both operands are the same converted value. With /arch:SSE2 loads are
   shared like integers. **[C]** (c3 `cf03`, `cf01`; f1 `cf06` default vs f1archSSE2 `cf06`; code: the `OPLOAD`
   branch of `GoptCheckTooCheapCse` 10b4a6d0; (re-verified, C++ game flags: `fl1`, `fl2`, `fl4`, also `this->f`; /arch:SSE2 not
   re-verified.)
3. **/fp:strict: no float CSE at all.** `GoptCommonSubExpression` 10b515e0 returns at once for float-typed
   tuples when bit 7 of the function's option word (FUNC+0x1c) is set; under /fp:strict `(a*b) + hf(a*b)`
   multiplies twice. **[C]** (code and f1fpstrict `cf02`, `cf08`; that bit 7 is the /fp:strict (fenv) bit is
   **[I]** from this probe.) (not re-verified)
4. /fp:fast: no promotion to double in the IL; the shared product is a float temp (`fst DWORD PTR tv`).
   **[C]** (f1fpfast `cf02`, compiled with `CCACHE=0`, see §12.) (not re-verified)
5. A double across a call is reloaded like any memory value. **[C]** (c3 `cf04`; (re-verified, C++ game flags: `fl5`.)

---

## 10. /O1 against /O2

At `@GrgIdentifyCandidates@4` the IL of 131 probe functions (l1, l2, l3, c3) is identical at /O1 and /O2 except
for the 12 functions that /O2 unrolls and one loop-bound temp that unrolling's preprocessing turns into a
variable (l2 `iv10`). So rotation, CSE, copy rules, dead stores, strength reduction, down-counters and block
fills are the same; what differs is unrolling (§8.7), `Mul2Shift`'s choices (§7 rule 6) and everything from
`Color` on. **[C]** (comparison script in the session; inputs `*_O1.il`.) (not re-verified)

---

## 11. Probes

`work/scratch/opt/run.sh NAME [flags]` writes `NAME.il` and `keep_NAME/NAME.asm`; `show.py NAME [stage]
[func]` and `asm.py NAME [func]` print them compactly; `gstage.sh` and `lstage.sh` dump one function at every
sub-pass.

| file | contents |
|---|---|
| c1.cpp, c2.cpp, c6.cpp | copies, single-use locals, first CSE cases |
| c3.cpp, c4.cpp, c5.cpp | CSE, kills, store forwarding, `__restrict`, `noalias`, address-taken locals, pointer chains |
| d1.cpp | dead code and dead stores |
| k1.cpp, m1.cpp, l4.cpp (`im*`) | constants, multiply shapes, immediates in registers |
| l1.cpp, l2.cpp, l5.cpp, l6.cpp | loops: hoisting, induction variables, down-counters, final values, block fills |
| l3.cpp | unrolling (`ur*`) and invariant code motion (`ic*`) |
| l4.cpp (`in*`) | rotation shapes |
| f1.cpp (+ f1fpstrict, f1fpfast, f1archSSE2) | floats |
| l1_O1, l2_O1, l3_O1, c3_O1 | the same sources at /O1 |
| verify/gen.py → verify/v_c, v_k, v_i, v_l, v_u, v_f, v_x, v_y, v_z, v_q, v_w .cpp; verify/v_g.cpp, v_h.cpp | the verification pass (§13): `vrun.sh NAME` compiles with the game flags, `tab.py GROUP [regex] [cols]` counts features per function, `tab.py GROUP -a FN..` prints listings, `cmpv.py GROUP` lists variants that differ from the plain function |

---

## 12. Open questions and things not verified

- **`LoptBuildAddressModes` use rules: settled.** The test is not on the start value but on the *expression* of
  the strength-reduced pointer, in `LoptGetCanonIV` 10b48940 (called at 10b47b8c for each candidate, i.e. each
  pointer's initialisation in the preheader). It returns 1 only if the pointer's hash expression (`sym+0x18`)
  is `X + base` or `X - base` (hash opcodes 0x2b2/0x2b3) where `X` is an induction-variable temp whose own
  expression is a multiply (0x2b4) whose first operand is a symbol with **no** expression, i.e. the basic
  induction variable itself: `(i * k) ± base`. `LoptBuildAddressModes` then requires `k` ∈ {1, 2, 4, 8}
  (10b47bdd..10b47bf2) and a base that is a constant, an address, a plain variable, or a temp defined as
  `variable + constant` (10b47c05..10b47c6a). **[C code + trace]**
  - `a[i]`: pointer = `(i*4) + a` → canonical → folded back to `[a+i*4]`.
  - `a[i + 1]`, `a[i - 1]` (also with `i` starting at 1, where the pointer's start value is exactly `a`), a
    member array at a non-zero offset: pointer = `((i*4) ± c) + base`. The first operand's expression is an add
    of `i*4` and a constant, not the multiply, so `LoptGetCanonIV` returns 0 (10b4899c) and the pointer stays.
    Traced on `work/scratch/openq/am.cpp` (probes at 10b48947, 10b48990, 10b4899c): `am0` and `am01` reach the
    0x2b4 test with an operand that has no expression; `am1` (`a[i+1]`) and `amm` (`a[i-1]`, `i` from 1) show
    the first operand's opcode 0x2b2 with an inner 0x2b4 and return 0.
  - `a[i] + a[i + 1]`: one canonical pointer `(i*4) + a`; `a[i+1]` is that pointer `+ 4` (an add of a constant,
    which a use may absorb as a displacement), so both fold: `[a+i*4]` and `[a+i*4+4]`.
  - Per use (after the candidate is accepted): `LoptFoldIntoAddressMode` 10b477a0 is called once per use in
    test mode and once to rewrite; a use that is an `OPCMP` against a region constant goes through
    `LoptCompareFoldable` 10b48a50, an add of the pointer and a 4-byte value through `LoptFoldToEa` 10b48c40.
    One failing use undoes the candidate (`LoptUndoOplea`). The conditions inside those three helpers were
    not read. **[C calls, I details]**
- **l2 `iv19`: settled.** `LoptFindCandidates` 10b3c730 (mode 1, the strength-reduction search) rejects a
  multiply `t = i * c` when `i` or the destination temp `t` is in `_GoptLoopHoisted` (10d0d7ac; the two
  `SbvTestBit` calls at 10b3c996 and 10b3c9ae, both must be 0 before `LoptAddCandidate`). `_GoptLoopHoisted`
  is the set of temps that invariant code motion (`GoptInvariantCodeMotion` 10b51a60, §8.2) has given a
  definition in this loop's preheader. **[C code + trace]**
  - In `for (i = 0; q[i].a; i++) s += q[i].b` the rotated loop computes `q + i*12` at the bottom (for the
    test, after `i++`) and again at the top of the next iteration (for `q[i].b`). Both are the same hash temp
    (`t128 = i*12`, `t129 = t128 + q`), so the top computation is redundant along the back edge and is moved
    to the preheader (IL at `@LoptStrengthReduce@12`: the top `OPMUL`/`OPADD` carry line 0; after the loop phase
    the preheader has `t128 <- 0`). That puts `t128` in `_GoptLoopHoisted`, and the remaining bottom multiply
    is refused: second test = 1 at 10b3c9b3 for `sr1`, `sr3`, `sr4`, `sr5` (`work/scratch/openq/sr.cpp`), 0 for
    every loop that is reduced.
  - What you see: `add ecx,1; lea edx,[ecx+ecx*2]; cmp [esi+edx*4],0; lea edx,[esi+edx*4]; jne` with the body
    reading `[edx+4]`. With element size 1, 2, 4 or 8 the same thing is `mov ecx,[esi+edx*4]` at the bottom
    (`sr3`), which looks like an ordinary folded address mode.
  - The condition is "the bottom test and the next iteration's body address the same element". It is the same
    for `do { .. i++; } while (q[i].a)` (`sr4`) and `for (;;) { if (!q[i].a) break; .. }` (`sr5`). It is **not**
    met, and the loop gets a pointer `+= 12`, when the body does not use the element the test loads (`sr9`:
    `add edx,12; cmp [edx],0`), or when the data test sits at the top of the body behind a counted bottom
    test (`i < n && q[i].a`, `if (!q[i].a) break;` under `i < n`: `sr2`, `sr6`). **[C compiles]**
  - Why the top computation counts as hoistable (which clause of §8.2) was not traced. **[I]**
- **Integer loops of unknown trip count: settled.** The rejecting test is in `LurUnrollDoLoop` 10b95bf0:
  modulo (remainder-loop) unrolling is entered only if `_LurUnrollModuloDoLoops` (10d11384) is set, the cached
  amount is non-zero (`cmp [ebp+0x10],0` at 10b95d4f) **and the loop's count of floating-point operations is
  non-zero** (`cmp [ebp+0xc],0` at 10b95d59); otherwise the loop's record gets reason code 0x11 and nothing
  is unrolled. The record is the one `LurCacheUnrollInfo` 10b900b0 fills from `LurClassifyDo` 10b8f4f0:
  `+0xc` is the third count `LurCountTuples` returns (the one `LurDetermineDoUnrollAmount` tests for "has a
  float operation"), `+0x10` the amount. **[C code + trace]** Trace (`work/scratch/openq/lur.cpp`, probe
  `-H '10b95d59=ebp#5'`, record = class, level, 0, float ops, amount): `int` sum, unknown `n`: `[1 1 0 0 4]`,
  no unrolling; `float` sum: `[1 1 0 7 4]` → `LurUnrollModuloDoLoop`; `int` sum of 11 elements: `[1 1 0 0 8]`,
  no unrolling. **One float statement is enough**: `for (..) { s += a[i]; b[i] = b[i] * 2.0f; }` with `int s`
  gives `[1 1 0 5 4]` and is unrolled by 4 with a remainder loop. So §8.7 rule 6 reads: unknown or
  non-divisible trip count → by 4 (8) **iff the loop body has a floating-point operation**.
- **`__declspec(noalias)`** has no visible effect on kills. Whether c2 reads the attribute anywhere is not
  checked.
- **c2 `cq16`: settled.** Two memory operands are the same value for GlobalOptimize only if their
  points-at-set indices are equal as well: `GoptInitFunc` 10b49cb0 sets `_FTupCmpIndirPasIndex = 1` (cleared
  in `GoptTermFunc` 10b4a1e0), and `TupCmpMemRefFields` 10bf6f70 then compares the INDIR leaf's `+0x1c` (PAS
  index, il_tuple.md) in addition to displacement, scale and base. The index is given to each dereference by
  `ComputePointsAtSets` (in `ItfBuildInterference`, before GlobalOptimize) from the pointer it goes through
  *as written*. In `int* q = &p->n; *q = 1; return p->n;` the store goes through `q` (PAS 0x24 in the trace)
  and the load through `p` (PAS 6). Copy propagation later replaces `q`, and from `HshBuildTable` on both
  operands print as `[t128+0]` with the same address temp, but the leaves keep their indices, so the load is
  not the stored value; the store still kills it, hence store + reload. With `p->n` on both sides both
  leaves have PAS 0x1f and the load becomes the constant in sweep 1. **[C code; trace
  `-H '@HshBuildTable@4=..,gd1b0fc>0x2c#14,gd1b0c8>0x28#14'` on `work/scratch/openq/fwd.cpp` f1/f2, word 7 of
  each leaf]**
  - Same result the other way round (`q = &p->n; p->n = 1; return *q;`, f6) and for a plain pointer
    (`q = p + 1; *q = 1; return p[1];`, f5). Forwarded: `*q` on both sides (f3), and `q = &p->m` with `m` at
    offset 0 (f4: c1xx emits `q = p`, a plain copy, and `q` is replaced by `p` before the sets are computed).
  - For matching: `mov [reg+d], x` directly followed by a reload of `[reg+d]` means the source reached the
    location through two different pointer spellings (a named alias or reference for one, the member
    expression for the other). Why `q` gets a set different from `p`'s is **[I]** (it is a variable assigned
    an address expression; `p` is a parameter).
- **`GoptRangeCmpFold` 10b53b40: settled; it is the range-check fold** `lo <= x && x <= hi` →
  `(unsigned)(x - lo) <= hi - lo`. It is called from `GoptOptimizeInstr` 10b4ffb0 for every `OPCMP` in sweep 1
  only (`_GoptDoCSE == 0` and not inside the loop phase), after constant folding. Conditions, all read in
  the code **[C]**:
  - the compare has one constant operand and its flags feed a conditional branch to a label L;
  - the next instruction (labels and assertion tuples skipped) is another `OPCMP` of the same type, of **the
    same leaf** (`TupCmpLeafs`) against a constant, feeding a branch to **the same label L**, and the leaf is
    not killed in between (`ItfKilledInRange`);
  - the two conditions are one of `<`/`<=` and one of `>`/`>=` (either order; `<=`/`>=` adjust the constant by
    one), and lo ≤ hi;
  - not under PGO instrumentation.

  It then inserts `t = x - lo`, turns the first compare into an unsigned compare of `t` with `hi - lo`
  (branch condition "above"), and deletes the second compare and branch. Code: `add eax,-lo; cmp eax,hi-lo; ja`.
  Probes (`work/scratch/openq/rng.cpp`, `rng2.cpp`; return value of the function traced) **[C]**:

  | source | folded? |
  |---|---|
  | `c >= 'a' && c <= 'z'`, `c < 10 \|\| c > 20`, `c > 10 && c < 20`, `unsigned`, `char` (`sub al,97; cmp`) | yes |
  | `if (c >= 10) { if (c <= 20) return *p; } return 0;`, `if (c >= 10) if (c <= 20) ..` (both exits the same) | yes |
  | `c >= 0 && c <= 20` | yes; lo = 0 leaves a single unsigned compare with no `add` |
  | `int v = *p;` / `int v = h(c);` then `v >= 10 && v <= 20` | yes |
  | **`*p >= 10 && *p <= 20`, `m >= 10 && m <= 20` on a member, an `unsigned char` member, `h(c)` twice** | **no**: `cmp eax,10; jl; cmp eax,20; jg` on one loaded value. The two reads are different temps in sweep 1 (CSE runs in sweep 2, after this fold) |
  | `if (c < 10) return 0; if (c > 20) return 0;` (two `return` statements = two labels), `if (c >= 10) { if (c <= 20) return 1; return 2; }` | no (different targets) |
  | `c >= d && c <= 20` (variable bound), `c >= 20 && c <= 10` (empty), `c == 5 \|\| c == 6` | no |

  For matching: `add reg,-lo; cmp reg,span; ja` means the tested value was a named local, a parameter or a
  call result kept in a local; two signed compares of one loaded value mean the member (or `*p`) was spelled
  in both halves of the condition.
- **`WpaRangeGlobopt` 10c03330** (between `SsaOptimize` and sweep 1): for each `OPCMP` against a constant whose
  other operand has an SSA definition it records a value range (`WpaGenRangeFromAssume`, `WpaSetRange`). What
  uses the ranges without LTCG was not read; it changed nothing in k1 `k18` or in the probes above. **[I]**
- **`GoptCanonicalize` 10b4a920 (7 KB) and `GoptCleanUp` 10b54570** are described from dumps only.
- **hash.obj kill lists**: `HshCleanupTypedKillVectors` 10b60120 and `HshCleanupFPKillVectors` 10b5fe50 suggest
  typed kills exist in some mode; the probes show none for int/float in native code.
- **EH functions.** For SEH functions and C++ EH functions without `-EHs` semantics, GlobalOptimize first runs an
  SSA pass that marks "write-through" scalars (`SsaDeriveEHWriteThroughScalars`). The verification pass probed
  `/EHsc` functions: §3 rule 4a is the one rule that changes. That this pass is the cause is **[I]**; SEH
  (`__try`) and `/EHa` are not probed.
- **Inlined code**: the verification pass ran every rule through a `__forceinline` helper inlined into an EH
  function (`*_i` variants); all behaved like the same body written in place (`*_e`). inline.md covers what the
  inliner leaves.
- **Null test and dereference (§4 rule 8)**: why the single-dereference cases end up as one load is not traced.
- **Immediate candidates (§7 rule 5)**: the thresholds are observed; the cost computation is in regalloc_cost.md's
  territory and was not traced for these probes.
- **Loops larger than 100 blocks**, functions with 10000 or more blocks (several sub-passes are skipped), LTCG
  and PGO are not probed.
- **Tool issue found**: `tools/ccache.py` treats `/fp:...` as an output option (`/Fp`, compared without case),
  so `/fp:strict`, `/fp:fast` and `/fp:precise` traces of the same source share one cache entry. Use
  `CCACHE=0` for `c2trace.py` runs that differ only in `/fp:`. The f1 results above were taken that way.

## Addendum: `noalias` and callee bodies (from the 0x00a89680 match)

The open question "does c2 read `noalias` anywhere" has a partial answer from a real function: a call to a
`__declspec(noalias)` *declaration* is treated as possibly writing through every pointer argument, while a callee
whose body is in the unit is summarised per pointer parameter (written or only read), and that changes which
address-taken locals of the caller are reloaded after the call. See frontend.md, "Applied to 0x00a89680". **[C
for that function]** The pass is `ItfSummarizeFuncInterf` 10b7f5d0, called from the callee's
`ItfBuildInterference`; it stores a written-through and a read-through parameter set on the callee's call-graph
node, and the caller's `ItfBuildCallInterf` 10b83030 turns them into the call's def and use points-at sets
(docs/c2/callee.md; checked independently, frontend.md "Which pass records the summary", probes `work/scratch/openq/callee*.cpp`). **[C code + trace]**

---

## 13. Verification (second pass, C++ with the game flags)

Done by a second agent that did not write §0 to §12. Every probe is new, in `work/scratch/opt/verify/`, compiled
with cl 14.00.50727.762 as `/O2 /TP /MT /GS /EHsc /Gy` (`vrun.sh`). `gen.py` writes each rule in up to four
contexts: plain function `NAME`, member function `NAME_m` (`p` replaced by `this`), `NAME_e` (the body after
`D d; if (h(7) > 3) hv(&d.v);` with `D` having a destructor, so there is an EH frame and other code in front),
and `NAME_i` (the body as a `__forceinline` helper inlined into such a function). 13 source files, about 1200
functions. `tab.py` counts, per function, loads and stores at the probe struct's distinctive offsets, `imul`,
`idiv`, immediate stores, `rep`, down-counter steps and so on, and lists the user variables still present in the
IL at `@Color@4`; `cmpv.py` lists every variant whose counts differ from its plain function.

**Numbers.** 122 rule statements and table rows were re-tested. 111 held in every context tried and now carry
"(re-verified, C++ game flags: ...)". 11 were wrong or hold only under a condition and are corrected in place
(list below). 20 statements carry "(not re-verified)". Outside the corrections, the member-function, EH and inlined
variants differed from their plain functions in one way only: immediate stores of 0 and -1 in EH functions, which
is the EH-state rule now in §7 rule 5.

**Corrections.**

| # | where | first version | what the probes show | probes |
|---|---|---|---|---|
| 1 | §0 item 2, §3 rule 4 → new rule 4a | a single-use local is substituted when its use is arithmetic, a compare or a return | not ahead of or inside a C++ EH state region: there every single-use named local stays a variable | `su2_e`, `su3_e`, `su4_e`, `su8_e`, `cs5_e`, `ehc*`, `er*`, `z1` to `z7`; controls `ehn1` to `ehn3` |
| 2 | §0 item 5, §5 table | no reload after a store "through the same base at another offset" | only for two constant offsets; `p->arr[i] = ..` kills `p->m` and `p->m = ..` kills `p->arr[i]` | `karr`, `kb1`, `kb2`, `ic6`; holds: `ksame`, `kb3`, `kb8`, `kb10` |
| 3 | §4 rule 3 (loads are shared until killed) → new rule 8 | one load | a pointer that is null-tested and then dereferenced is loaded twice in several common shapes (different IL types for the test and the dereference) | v_g.cpp `g1`, `g15`, `g17`, `g18`, `g21`, `g22` against `g8`, `g9`, `g19`; v_q.cpp |
| 4 | §7 rule 5 | "2 stores: immediates; 3 or more: a register; `and`/`add` any count: immediate; 3 stores with a call between: immediate" | 2 uses that carry a displacement are enough; uses without a displacement never count; memory-destination `add`/`and`/`or`/`cmp` count; across a call it takes 4 uses | `imA*` to `imT*`, `imR`, `imS`, `nd3` to `nd8`, `imc3`, `imc4`, `wd3`, `wd4`, v_h.cpp |
| 5 | §0 item 9, §8.4 table row 1 | one array with element size 1/2/4/8 gives `[a+i*size]` | only with an access at displacement 0 from a base variable; member arrays at a non-zero offset and `a[i + k]` alone give a moving pointer and a down-counter | `iv13b`, `iv13c`, `iv13e`, `zi1` to `zi5` |
| 6 | §0 item 12, §8.7 rule 6 and table | a prime trip count above 10 is not unrolled | true for integer loops; a float loop with such a count is unrolled by 8 with a remainder loop | `uf11`, `uf13`, `uf17`, `uf19`, `uf23`; `ut11`, `ut13`, `ut49` |
| 7 | §8.7 rule 3 | "more than 15 blocks: none" listed before the 5/4/3/2 rule | in the code it comes after it, so a constant-trip integer loop of 25 blocks is still unrolled by 4 | `zb12`; 10b91160 |
| 8 | §8.7 rule 2 | (implied) a loop over the complete-unroll limits keeps its loop | it is unrolled by rules 4/5, and by its whole trip count when that is 2 to 6 | `ub8`, `us20`, `zb12`; limits themselves confirmed with `pb1`/`pb2`, `ps7`/`ps8` |
| 9 | §6 rule 6 | "keeps all three stores" | true in the IL; the listing has two (the arms are merged into one conditional store) and the first store is still there | `ds5`; d1 `ds20` listing |
| 10 | §8.1 rule 1 | `for(;;) { ..; if (..) break; }` has no guard | only when the `break` ends the iteration; with the step after it the part before the test is duplicated in front of the loop | `rt5` |
| 11 | §0 item 10, §8.2 table | "with a call only the address `p + off` is hoisted" | IL only; it is folded back, the listing has `[reg+off]` in the loop and nothing in the preheader | `ic5` |

**Additions** (new rows, not corrections): `g` is reloaded after `*q = ..` (`gptr`); `p->m` is not reloaded after a
store to a never-address-taken `static` (`kb11`) or to an address-taken local (`kb14`); `ga[i]` against `ga[j]` and
`gb[i]` (`kb6`, `kb7`); a member loop bound is re-read every iteration when the loop stores through a pointer
(`iv14`, `bm9`), which also blocks `rep stosd`; unrolling by 2 keeps the `[a+i*4]` form (`ut14`, `ut22`); implicit
destructor calls keep stores to address-taken locals alive (`ds8_e`).

**Cross-check against Fallout 3** (`Fallout3_1703.exe`, read-only; `verify/f3dis.py VA` disassembles,
`verify/f3scan.py` finds small functions with a given shape). **[C]** for what the listing shows; the sources are
unknown, so "predicted" means the rule names a source shape that gives these instructions.

| function | what is there | rule | verdict |
|---|---|---|---|
| 0x00a89680 | `mov eax,66666667h; imul ecx; sar edx,3; mov eax,edx; shr eax,31; add eax,edx` for a pointer difference / 20; `lea edx,[ecx+ecx*4]; lea edx,[eax+edx*4]` for `base + n*20` | §7 rule 6 (magic divide; `lea` multiply folded on by `AddrModes`) | predicted |
| 0x00a89680 | `mov [esi+8],eax` … `mov [eax+4],ecx` … `sub ebx,[esi+8]`: reload after a store through another pointer; `[esp+20h]` re-read after the call in the loop | §5 (other pointer kills; address-taken local against a call) | predicted |
| 0x00ad1250 | `cmp dword ptr [edx+eax*4],0; lea edx,[edx+eax*4]; jne` and later `mov ecx,[edx]` | §4 rule 8 (the first version predicted one load); verify `g1` has the same shape | predicted after correction 3 |
| 0x00ad1250 | `[ecx+8Ch]` (a member pointer) loaded at 0x00ad126e and again at 0x00ad128b with no store or call between | none. In verify `g2` the IL at `@GrgIdentifyCandidates@4` also has the member load twice, but the listing ends up with `mov ecx,edx` | **not explained** |
| 0x0044ec70 | 38 iterations over two arrays with a call: `sub edi,ecx` before the loop, `[edi+esi]`, `add esi,4`, `mov ebx,26h` … `sub ebx,1; jne` | §8.4 (two arrays: pointer, difference, down-counter); §8.7 rule 1 (call: no unrolling) | predicted |
| 0x0044ec70 | `cmp dword ptr [esi],0; je` … `mov ecx,[esi]` in the body | §4 rule 8 | predicted after correction 3 |
| 0x004654e0 | loop over 16-byte elements with a virtual call: `add esi,10h; sub ebx,1; jne`, guard `test ebx,ebx; jbe` | §8.4 (struct array: pointer and down-counter) | predicted |
| 0x004654e0 | after the last call `mov [edi+4],0; mov [edi+0Ch],0; mov [edi+8],0` | §7 rule 5: three uses, range across calls. The first version predicted a register; verify v_h.cpp `Arr::~Arr` reproduces these three instructions | predicted after correction 4 |
| 0x004282f0 | after a call `xor eax,eax` and four `mov [esi+2Ch..38h],eax`; the vtable store `mov [esi],imm` stays immediate | §7 rule 5 | predicted |
| 0x004b3410 | `xor eax,eax` and four stores to `[ecx+74h..80h]` | §7 rule 5 | predicted |
| 0x00aef570 | `mov [edx],200h; mov [eax],200h` (same constant, no displacement, several places) | §7 rule 5 (no displacement: never a register) | predicted |
| 0x0050c300 | zero fill as `test ebp,ebp; jbe; mov ecx,ebp; xor eax,eax; mov edi,esi; rep stosd`; then a loop whose bound `[1071A3Ch]` is compared from memory every iteration and whose global `[1071A40h]` is loaded again after `mov [esi+eax*4],edi` | §8.6; §8.2 (global bound with a store in the loop); §5 (`g` against `*q`) | predicted |
| 0x0040e880 | a fill with a null check per element (`test eax,eax; je; fst [eax]`): no `rep stos`, pointer `add eax,4`, `sub ecx,1; jne` | §8.6 (only a plain store body is a block fill); §8.4 | predicted |

Nine functions, thirteen constructs: twelve predicted (three of them only by the corrected rules), one not
explained.

**Not done.** `/O1`, `/fp:strict`, `/fp:fast` and `/arch:SSE2` were not re-run. `__declspec(noalias)`,
`__declspec(restrict)`, three-array loops, nested loops, the step-3 trip count, `iv19`, struct locals passed by
address, the too-cheap list and the code-only rules of §4 (rules 4 to 6) and §8.1 rule 3 were not re-tested. Rule
4a's mechanism was not traced in c2; its boundary ("an EH region can still be entered after the definition") is
fitted to 20 probes. SEH (`__try`), `/EHa`, LTCG and PGO were not probed. `tests/run_tests.py` was not run by this
pass (no tool or test file was touched).
