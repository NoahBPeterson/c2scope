# The loop optimiser: LoptTerm, and strength reduction in rounds

Reported by tricko3D (spore-decomp PR #674): "Strength reduction runs in rounds, and round 1 builds induction
variables only for explicit `IV*const` / `IV+const` expressions. That is why the source walks an `offs` pointer with
`next = offs + 1`." They traced cl 13.10.3077 by name (LoptStrengthReduce 0x1079a796, LoptCombineInductionVariables
0x1079a700, LoptBuildAddressModes 0x1079b789) on Spore 0x010abb90 (a /vc71 row) and found that it closes the loop
preheader order and the loop-tail increments; also that retail's `jac` being address-taken was the biggest single gain
(450 -> 517 of 544 instructions).

Everything below was re-measured here on small probes (`tests/data/rules/RL_loptsr.cpp`, checked by
`tests/probes/loopopt.py`) with the tracer on 13.10.3077, 13.10.3052, 14.00 and 15.00. Each statement is marked
**[C]** confirmed (read in the c2 code, or seen in a traced compile) or **[I]** inferred. 13.10.3077 and 3052 behave
identically in every probe; 14.00 identically except for the spelling `add r,1` / `sub r,1` for `inc` / `dec`; 15.00
unrolls the loop by two before this pass, which shifts the counts (section 6). The Spore row itself was not reproduced
here (spore-decomp's binary is not in this repository's reach), so the numbers on it are tricko3D's.

## 1. Where it runs (per loop, innermost first)

`GoptOptimizeLoop` (0x1079a462) calls `LoptTerm` (0x1079a5bf) once per loop. **[C]** (call order read from the
disassembly and seen in the trace):

```
LoptInit, LoptFindNonInductionVariables, LoptFindInductionVariables, LoptCanonInductionVariables, LoptCalcDefUse,
LoptCheckEquivClassVars, LoptCalcLoopCount,
LoptCombineInductionVariables      (candidates mode 4)
LoptScalarReplace
LoptStrengthReduce                 (candidates mode 1: the rounds)
LoptOptimizeLandingPadPhase1, LoptSaveInitInfo, LoptEliminateInductionVariables, LoptBlockMoveInit,
LoptCombineInductionVariables      (mode 4 again: drops the IVs the rounds made redundant)
LoptLinearFunctionTestReplace      (mode 2: the loop-test rewrite; LoptFindBestIV / LoptFindNextIV)
LoptEliminateInductionVariables
LoptBuildAddressModes              (mode 6: LoptFoldIntoAddressMode / LoptGenAddressModeExpression)
LoptCountedLoop, LoptHoistFinalValues, LoptSubsumeInductionVariables, LoptRemoveEmptyLoop, LoptOptimizeLandingPadPhase2,
LoptCleanUpLoop
```

`LoptFindCandidates(loop, mode)` (0x1079a6e4, a jump table on `mode`) walks the loop body and calls
`LoptAddCandidate` (0x1079da3b) for each tuple of the wanted kind. The mode is a literal pushed by its caller: 4 in
Combine, 1 in StrengthReduce, 6 in BuildAddressModes **[C]**; 2 comes from the loop-test rewrite and 3 from the call
between Eliminate and the second Combine **[I]** (by call order only). LoptAddCandidate pushes on the head of the list
(0x108a9280), so a sweep handles its candidates **last found first [C]** (disassembly).

## 2. The rounds **[C]**

`LoptStrengthReduce` is called once per loop and iterates:

1. `LoptFindCandidates(loop, 1)`: collects the multiplies and adds that are IV-based (section 3);
2. rewrites them into new induction variables (an init in the preheader, `iv = iv + step` at the loop tail);
3. `LoptFindInductionVariables(loop)` again, so the new variables are IVs for the next sweep;
4. until `LoptFindCandidates(loop, 1)` finds nothing; then it returns -1.

So a loop with R rounds shows `LoptFindCandidates(mode 1)` R+1 times and `LoptFindInductionVariables` R+1 times
(LoptTerm's own call plus one per round). A sweep converts every candidate it found: for `a[i]*b[i]` round 1 found both
`i*4` tuples (one IV, they are the same expression), round 2 found `i*4+b` and `i*4+a` and made two IVs.

Measured rounds (13.10.3077 / 3052 / 14.00; the probe, `lp_*`):

| source in the loop | rounds | what each round makes |
|---|---|---|
| `s += a[i]` (frontend IL: `t = i*4; p = t + a; load [p]`) | 2 | 1: `t143 = i*4` (init 0, step 4). 2: `t154 = t143 + a` (init `a`, step 4) |
| `s += a[i*3]` | 2 | same with 12 |
| `s += a[i<<2]` | 2 | `i<<2` is already `i*16` in c2's IL **[C]** (IL dump), same as the row above |
| `s += a[i+k]` | 3 | 1: `(i+k)*4` made as `i*4 + 4*k` (the multiply distributes over the sum, **same round**), plus `i+k`; 2, 3: the `+k*4` and `+a` terms, one add per round |
| `s += (i+k) ^ s` | 1 | `t = i + k` (init `k`, step 1) |
| `s += (i*k) ^ s` | 1 | `t = i*k` (init 0, step `k`) |
| `s += a[i*3+1]`, `a[(i+1)*3]`, struct array `a[i].x + a[i].y` (stride 12) | 3 | `i*12`, `+a`, `+4` (or `+12`): **each added invariant is one more round** |
| downward `for (i = n; i > 0; i--) s += a[i-1]` | 3 | `i*4` (step -4), `+a`, `+(-4)` |
| `for (; p < e; p++) s += *p` | 0 | `p` already is an IV |
| `a[i+s]` (`s` changes in the loop) | 0 | the addend is not region constant |
| `a[i*j]` (`j` is a second IV) | 0 | the factor is not region constant |
| `sink(&i); for (i...) a[i*3]` | 0 | `i` is address-taken (section 5) |

The pattern: a round extends the chain by **one add of a region constant** (`+a`, `+4`, `+k`) onto the previous
round's IV, or by one multiply (which also distributes over a sum of IV and invariant). **[C]** for the table rows;
the general law ("rounds = 1 + number of chained invariant adds, give or take a distributed multiply") is **[I]**.

To see the IL of each round, dump at the entry of the pass and of each rebuild (the Nth `LoptFindInductionVariables`
dump is the state after round N-1):

```sh
C2_BUILD=13.10.3077 uv run tools/c2trace.py il t.c --func _f \
    --at @LoptStrengthReduce@12,@LoptFindInductionVariables@4,@LoptEliminateInductionVariables@8 | rg -v '^  B'
```

For `s += a[i]` (the first dump is the entry; the later ones are after round 1, after round 2, after Eliminate):

```
  entry:    t129 = i*4 ; t130 = t129 + a ; t132 = [t130] ; s += t132 ; i = i + 1
  round 1:  t143 = i*4 ; t145 = 4*1 ; t146 = t143 + a ; ... ; t143 = t143 + t145        (new IV t143, step t145)
  round 2:  t154 = i*4 + a ; ... [t154] ... ; t154 = t154 + t145                        (new IV t154)
  Eliminate: preheader  t143 = 0 ; t145 = 4 ; t154 = a      (the i*4 recomputations are replaced by these inits)
  Combine #2 drops t143 (dead after round 2); BuildAddressModes then folds [t154] back into [a + i*4]
```

On 15.00 the hook names are the mangled ones (`?LoptStrengthReduce@@YIXPAUtagLOOP@@PAUtagFUNC@@E@Z`,
`?LoptFindInductionVariables@@YIHPAUtagLOOP@@@Z`, ... in work/index/15.00.30729.1/c2.dll.csv); the pass structure and the
rounds are the same.

## 3. What round 1 accepts

The claim holds, with one correction to what "const" means. **[C]**

- The operand must be a **real induction variable** (a variable updated `x = x + c` in the loop; the first loop of
  `LoptFindInductionVariables` collects them) combined with a **region constant** (`LoptIsRegionConstant`
  0x1079c265): a literal, or a variable the loop does not change. A literal is not required: `i*k`, `i+k`, `i*st`
  with `k`, `st` loop-invariant parameters are round-1 candidates (`lp_ivk`, `lp_ivv`, `g_stride`: the new IV steps by
  `k` / `st`, its init is hoisted to the preheader). `i + s` with `s` modified in the loop, or `i*j` with two IVs,
  produce nothing.
- IV*const and IV+const are the **first-level** forms. A temporary computed from an IV (`t = i*4`) is not an IV until
  the next `LoptFindInductionVariables`, so `t + a` cannot be a candidate in round 1; it becomes one in round 2. That is
  why `a[i]` takes two rounds although its IL is just two operations.
- `IV<<const` arrives already as `IV*const` (c2's IL has no shift at this point). Pointer increments (`p++`, `offs =
  offs + 1`) are IVs from the start, so they need no round.
- A pointer written as a **copy through a temporary that stays live** is not an IV: `int *next = offs + 1; ...use
  *next...; offs = next;` defines `offs` by a copy, not by `offs = offs + 4`, so `LoptFindInductionVariables` rejects
  it and the loop gets no rounds at all (`lp_next`: 0 rounds, listing `lea edx,[ecx+4]` ... `mov ecx,edx`). With `next`
  used once the copy folds and `offs = offs + 1` is the plain pointer IV (`lp_next1`: `add ecx,4`, 0 rounds on 13.10).
  With `offs[1]` instead of `next` (offs an IV), round 1 does build `offs + 4` as an IV **[C]** (a scratch probe, not in the
  suite: one round), which BuildAddressModes then folds back into `[offs+4]`.

## 4. How it shows in the emitted code

- **The rounds' IVs are raw material, not the final shape [C].** `a[i]` ends as `add eax,[esi+ecx*4]` / `inc ecx`: the
  pointer IV `t154` built in round 2 is folded back into `[a + i*4]` by LoptBuildAddressModes, because `i` is the loop
  counter anyway and 4 is an x86 scale **[I]** for why. `a[i*3]` (scale 12, not an address-mode scale) keeps the
  pointer walk: `add eax,[ecx]` / `add ecx,12` / `dec edx` with the loop test rewritten as a count-down
  (`LoptLinearFunctionTestReplace`). `a[i+k]` ends as a pointer walk too (`lea ecx,[esi+ecx*4]` in the preheader,
  `add ecx,4`).
- **Preheader order.** The new IVs' inits come out of `LoptEliminateInductionVariables` in creation order **[C]**, and a
  sweep visits its candidates last-found-first (section 1; that this is what reverses a / b is **[I]**). In `a[i]*b[i]` the preheader is `mov ecx,[b]` then `mov edx,[a]` (b first,
  although a is read first), then `sub edx,ecx` so the a access is `[edx+ecx]`: the second pointer IV is not
  stepped, it rides on the first as a difference **[C]** (`lp_two`). Source order of the variables does not change
  this; the tuple order inside the loop does **[I]**.
- **Loop-tail increments.** The new IVs' `iv = iv + step` tuples are appended after the original increments, in
  the order the rounds created them (`t143 += 4` before `t154 += 4`, after `i += 1`) **[C]**; Combine #2 then deletes
  the dead ones. So an IV that exists in the source (an explicit `offs++`) keeps its source position among the tail
  increments, while a synthesised one lands after them: this is why spelling the walk as an explicit pointer
  variable can reproduce a tail order that `a[i]` cannot **[I]** (consistent with tricko3D's finding; the Spore row
  was not run here).

## 5. Address-taken variables

`jac` being address-taken changed 450 -> 517 of 544 on Spore. What was confirmed here, on two mechanisms **[C]**:

1. An **address-taken IV variable is not an IV.** `int i; sink(&i); for (i = 0; ...) s += a[i*3];` gets 0 rounds
   (the code tests the symbol's flag byte at +0x36, bit 1, in `LoptFindInductionVariables` 0x1079ab25 and in
   `LoptIsRegionConstant`: that this bit is "address taken" is **[I]**; the compiled result is **[C]**), so the loop
   keeps `lea edi,[eax+eax*2]` / `add esi,[edx+edi*4]` / `inc eax` / `mov [i],eax`: the multiply stays and `i` is
   stored back each iteration, against the pointer walk (`add ecx,12`) when `i` is a plain local. If a retail loop is
   that shape, the counter or index is address-taken (`probe lp_itaken`).
2. An **address-taken base is not region constant when the loop has a call.** `int *jac = a; sink2(&jac);` with
   `ext()` called in the loop: round 2 (`+jac`) does not happen (1 round instead of 2) and `jac` is reloaded from its
   stack slot every iteration (`mov edx,[jac]`; `add eax,[edx+esi*4]`), against a register-held base when it is not
   address-taken (`lp_jac`). The same loop with stores through an `int*` instead of a call (`out[i] = jac[i] + 1`)
   still strength-reduces (the int store does not kill an `int*` variable) **[C]**.

Which of the two (or another) applies to `jac` in Spore 0x010abb90 is **not confirmed**: it needs the row's source.

## 6. Per build

| | 13.10.3077 / 3052 | 14.00 | 15.00 |
|---|---|---|---|
| passes and call order (section 1) | yes | same | same names (mangled), same order |
| rounds for the table rows | as the table | same | one more where a pointer IV is derived: the loop is unrolled by two first (`LurUnroll`), e.g. `a[i]` 3, `a[i+k]` 4, `p++` 1, `lp_next1` 1 |
| `LoptFindCandidates` mode numbers | 1 / 2 / 3 / 4 / 6 | same | **not established**: the argument values logged differ from 13.10's, so the probe counts `LoptFindInductionVariables` calls instead |
| listing | `inc` / `dec` | `add r,1` / `sub r,1` | unrolled bodies |

## 7. Reproducing

```sh
uv run python tests/run_tests.py            # default build; C2_BUILD=13.10.3077 / 13.10.3052 / 15.00 likewise
C2_BUILD=13.10.3077 uv run tools/c2trace.py -H '@LoptFindCandidates@8=s2' -H '@LoptAddCandidate@4=s1' \
    -H '@LoptFindInductionVariables@4' --exit tests/data/rules/RL_loptsr.cpp -- /TP     # with C2TRACE_FUNC='?lp_two@@'
```

Unconfirmed: the Spore 0x010abb90 numbers (450 -> 517 of 544; the preheader order and tail increments on that row);
which candidate kinds `LoptFindCandidates` mode 1 selects beyond the multiply and add tuples seen here (its jump-table
body was not read); modes 2 and 3 by name; the "one add per round" law for forms not in the table.
