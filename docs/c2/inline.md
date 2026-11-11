# c2 inliner: which calls are inlined at /O2, and what IL the inlining leaves

This spec covers the `Inline` pass of cl 14.00.50727.762's back end (`Bin/c2.dll`, inline.obj), without LTCG and
without PGO, at the game flags (`/O2 /TP /MT /GS /EHsc /Gy`). It answers two matching questions:

1. **Will this call be inlined?** Rules 1-9. They predict from a number you can read off our own listing: the
   callee's compiled size in bytes.
2. **What does an inlined call leave in the caller's IL?** Rules 10-15: the return-value variable, copies of
   parameters, and the inlinee's `this`. These decide which values become register-allocation candidates
   (regalloc.md §7) and, through them, priorities and the scratch rotation.

**[C]** = CONFIRMED: read in the code, and shown by a traced compile; each claim says which. **[I]** = INFERRED.
Probes are in `work/scratch/inline/`. `hooks.sh` there holds the trace hooks used below (source it, then
`uv run tools/c2trace.py "${H[@]}" f.cpp -- /TP /MT /GS /EHsc /Gy`). They print each `ConsiderCandidate` and the
intermediate values of the cost model (`InlCallGraphDecision` 10b6a040, ICGD below).

Cross-references: frontend.md (c1xx emits every call, including `__forceinline`, as `OPARG`/`OPCALL`, so inlining
is decided here), regalloc.md §7 (which variables are candidates), il_pipeline.md (`Inline` runs in
`dop2_InitialOptsAndSetup`, before GlobalOptimize).

---

## Rules you can use when matching

### Which calls are inlined

1. **c2 compiles functions callee-first.** When a function calls a function defined later in the file, it is
   compiled after that callee: in a2.cpp `c_plain` calls `late`, which is defined after it, and the order was
   `... c_ehcallee, late, c_plain`. Otherwise the source order is kept. A cycle (mutual recursion) is broken in
   source order. The `/FAsc` listing and the object come out in this compile order.
   **[C: @Inline@4 order in traces a2, a3, a4]**
   So when a caller is compiled, its callees usually have already been compiled out of line, and their final
   code size is known. That size drives rule 3.

2. **Calls that are never candidates** (`InlCallTupleIsCandidate` 10b69bd0, `InlMightInlineEntry` 10b69b50,
   `InlIsNormalCall` 10b6d180):
   - indirect calls: virtual calls through a pointer, function pointers, and `__declspec(dllimport)` calls
     (which go through `__imp_`).
     - A virtual call on an object of known type (`V o; o.v()`) is direct, so it is a candidate and is inlined.
       **[C: a4]**
   - callees that contain `try`/`catch` (callee flag `+0x50 & 0x200`) or `__try` (`+0x20 & 0x1000`).
     - A callee whose only EH is a local with a destructor **is** inlined (e2/ie2 in a2). **[C: a5, a2]**
   - varargs functions and `__declspec(noinline)` (both lack the entry flag `+0x50 & 0x40`). **[C: a5]**

   What else blocks inlining:
   - `_alloca` callees are candidates but are rejected later. **[C: a5; the rejecting test was not traced]**
   - A callee frame larger than 0x2000 bytes is rejected (callee field `+0x60`). The limit is 0x1000 when a flag on
     the caller's call-graph list is set. **[C: code; a5, where a 5000-byte buffer was inlined and a 9000-byte
     one was not; I: that the flag means "the caller has a loop"]**

   What does not matter:
   - Inline asm, static locals and EH regions in the caller (`try` around the call) do not block inlining.
     **[C: a5, a6]**

3. **The /O2 cost model (callee already compiled).** For a non-`__forceinline` callee, ICGD computes:

   ```
   size  = callee's compiled code size in bytes (entry +0x68; the ret instruction included)
   B2    = 1 + 2 per ordinary argument + 5 per constant or address argument   (this counts as an argument)
   c     = size - B2 - B0 - (10 if the callee has no branch, no switch and no side-effecting call)
           (+ bloat, only when the caller's and callee's instruction counts add up to more than 9000)
   reject if c > 450
   d     = c - (the other benefit terms; all their weights are 0 at /O2 except one, see below)
   accept at once if d' < 14, where d' = d/3 for a static callee called once in this file,
                                    d - d/(2n) for a static callee called n < 10 times, else d
   w     = the call block's static weight: 1e8 for straight-line code, x5 per loop level, x0.5 in an if arm
   v     = (d*1e8/w + 6d) / 7     if w <= 1e8
           (d*1e8/w + 2d) / 3     if w >  1e8   (in a loop)
   inline iff v < 74
   ```

   - In straight-line code this is simply **c < 74**.
   - Constants: `_InlCurConsts` 10d116d8, a copy of `_InlNativeByteConsts` 10d18388, set by `InlInitNativeByte`
     10b68c20. SizeLimit is 9 (so the early-accept limit is 9+5 = 14), SpeedLimit 74, Denom 7, LoopDenom 3,
     NoBranchSizeAdj 10, and the argument benefits 2 and 5.
   - B0 is the code that folds away when a constant argument decides a branch inside the callee
     (`InlBenefitForScope` 10b6b1d0). The term "+3 when the callee's return value is compared with a constant"
     comes from `InlBenefit` 10b6ae90.
   - The "other benefit terms" are B1 times FoldCmpSpeedWeight 30 (B1 = scopes that a constant argument
     decides), and B7 times 30 (B7 = code-address arguments to side-effect-free functions). The constant-argument,
     data-address and code-address weights are 0.

   Probes (byte size, then c and v from the trace):
   - a1: `f_k` with k volatile stores is 1+9k bytes; f1..f9 are inlined, f10 (91 bytes, c = 78) is not.
   - a3: `h10(5)` gives c = 75 and is not inlined; `hp(&x)` gives c = 68 and is inlined.
   - a3: in a nested loop v = 53 (inlined); in an if arm v = 89 (not inlined); a callee with a branch gets no -10
     (c = 101).
   - a2: a static callee called once in a loop gives v = 54 and is inlined.

   **[C: code of ICGD (disassembly 10b6a040-10b6a5c5) and InlBenefit; traces a1-a3, a5; blind test p1 below]**

4. **`inline` makes no difference at /O2.** `i10` (`inline`) and `f10` (plain) got the same cost, and both
   were rejected (a2). The keyword flag (`+0x50 & 0x80`) is read only on the non-speed path (/O1: -7). Member
   functions defined in the class body carry the same flag. **[C: code, a2]**

5. **`__forceinline` (`+0x50 & 0x2000`) skips the cost model.** It also skips the depth and frame limits. a2's
   `x10` (91 bytes) was inlined. **[C: code, a2]**

6. **Nested inlining is decided again at every depth.** The callee's IL that is copied is its IL from before its
   own inlining. When `c1` (which called `c2`, which called `c3`) is inlined into `top`, the inliner reconsiders
   `c2` at depth 2 and `c3` at depth 3, each with rule 3 and each callee's compiled size. That size already
   includes whatever was inlined into the callee. The depth limit is 254 (the per-candidate byte 0xfe, from
   `#pragma inline_depth`), except for `__forceinline`. A recursion cycle stops 16 levels below where it began.
   **[C: a4 trace (depths 1-3); code of ShouldInlineCallTuple 10b69e90 for the limits]**

7. **When the callee is not compiled yet** (the back edge of a recursion cycle), there is no call-graph data,
   and `ShouldInlineCallTuple` + `InlineeIsTooBig` 10b70870 decide on c1xx IL instead. Each instruction tuple
   counts 1, and each call or intrinsic counts 2; branches, returns, `?:` and switches count 0. The budget is
   `3*(params that are used + 2) + 3`, plus 6 until the first call if a flag on the caller is set; the callee is
   rejected once the count exceeds the budget.
   - The 3 is `_InliningVolume` (10d0d300, default 3, option `-vol#`).
   - A compare of a parameter with a constant that the constant argument decides skips the code it jumps over.
   - Also: the caller's running size must stay at or below 35000, and either the callee's IL size is at or below
     the remaining budget, or it is at or below 40. The budget starts at clamp(2 × caller size, 1000, 35000).
   - In a7, with one parameter: counts 8, 10 and 12 were inlined; 13 and 14 were not (budget 12).

   **[C: code; a7 trace of the InlineeIsTooBig exits]**

8. **At most one inlining is attempted per call site.** Candidates are tried in IL order (`GetInlineCandidates`
   10b6d2f0 collects them in stream order). **[C: code, traces]**

9. **Reading a decision.** `uv run tools/c2trace.py "${H[@]}" t.cpp -- <flags>` with `work/scratch/inline/hooks.sh`.
   - `ConsiderCandidate` exit eax=1 means the call was inlined.
   - The ICGD probe at +0x88 shows the size; +0xe7 the B array; +0x359 c; +0x40f d and d'; +0x489 the weight;
     +0x542 v.

### What inlining leaves in the IL

Dump points: `@Inline@4` is c1xx's IL; `@WrnEmitWarnings@4` comes right after Inline; `@GlobalOptimize@4`;
`@GrgIdentifyCandidates@4` is what the allocator chooses from (`c2trace.py il ... --at ...`).

10. **Shape.** The call becomes the callee's blocks, spliced in: the inlinee's entry code, then each `return e`
    becomes `ret = e; OPGOTO Lexit`, and an exit label. The extra blocks and gotos are removed by later flow opts.
    **[C: WrnEmitWarnings dumps, b1]**

11. **Return value** (`SubstituteReturnExpr` 10b6fb60, `CanMoveReturnExpr` 10b6e3e0, `UseCallDst` 10b6e4d0):
    - The return expression is substituted at the use, with no variable, when it is:
      - a constant;
      - an address: `this+off`, which is what a **by-reference** accessor returns;
      - a variable or temporary not killed before the use.

      That is why a by-reference accessor never leaves a variable (c8: `r_eh`, `r_val`). **[C: code, c8]**
    - When the caller writes the result straight into a local (`int r = f();`), that local is the return
      variable: every return in the inlinee assigns `r` (c7 `u_dst`). **[C: code, c7]**
    - Otherwise (a load such as a **by-value** accessor's `return x;`, arithmetic, a call result, or several
      returns) the inliner creates a new local `sN` (an unnamed SYM; it prints as `s2`, `s3`...), assigned at
      each return. **[C: b1, c1-c8]**

12. **Whether that `sN` survives GlobalOptimize** (and so becomes a Color candidate) **[C: matrix c1, c3-c5, c8]**:

    | use of the inlined call's value | outside EH regions | inside an EH region (a local with a destructor is live, or `try`/`__try`) |
    |---|---|---|
    | argument of a call whose result is **used** (`return ext(s->get())`, `g = ext(...)`, `if (ext(...))`) | survives | survives |
    | argument of a call statement whose result is **discarded** (`vext(s->get())`, `ext(s->get());`) | propagated: the load becomes the argument | survives |
    | arithmetic, `return`, a store (`s->get() + 1`, `return s->get()`, `g = s->get()`) | propagated | propagated, unless a call (e.g. the destructor) lies between the definition and the use |
    | several returns (`if (k) return x; return y;`) | survives | survives |

    This generalises frontend.md's p4 finding. p4's non-EH case was a discarded-result call, and its EH case was
    inside the destructor's region. The mechanism is GlobalOptimize's forward substitution, not the inliner's: the
    IL entering GlobalOptimize is the same in the "survives" and "propagated" rows (k_ign vs k_ret). **[C for the
    table; I for the reason]**

13. **Parameters** (`ActualsToFormals` 10b6e670, `CanAvoidTempCopy` 10b6faa0, `RewriteFormal` 10b6f040):
    - A variable or constant argument is substituted for the formal: no copy (c6 `f_var`, `f_cst`).
    - An expression argument (`p + 1`, a load `s->x`, `this + off`) is assigned once to a new local `sN` at the
      call site. It survives to Color when the formal is used twice or more (`f_expr`, `f_load`). It is propagated
      when the formal is used once (`f_load1`).
    - A formal that the callee modifies gets a copy even for a variable argument. GlobalOptimize then usually
      removes it (`f_mod`).

    **[C: c6]**

14. **The inlinee's `this`** follows rule 13. **[C: c6 f_thisemb, f_thisptr; c7 u_sub0, u_sub8, u_two0]**
    - A member call on `*this`, or on a subobject at offset 0, uses the caller's `this` directly.
    - A call on a subobject at a nonzero offset (`in.sum()`, `in` at +8) or through a loaded pointer
      (`pin->sum()`) creates `sN = this+8` / `sN = [this+16]`. This `sN` is a separate variable: when the inlinee
      reads its `this` twice or more, it survives as a candidate, and the caller's `this` is referenced only once,
      to define it.
    - With one read, the access folds to `[this+off]` (u_sub8).

    For 0x008e7d40 (frontend.md): reading `name` through an inlined member of a subobject at a nonzero offset,
    used twice, is a construct whose entry-block references count against `sN`, not `this`. It is untested in that
    unit.

15. **Static and `inline` callees are still compiled out of line first** (rule 1), and they stay in the
    listing as COMDATs even when every call was inlined (`s10b` in a2). `/Gy` plus the linker's `/OPT:REF`
    decide whether they ship. **[C: a2 listing; I: the link-time removal]**

---

## Probes

| probe | what | prediction | result |
|---|---|---|---|
| a1 | f1..f10, 1+9k bytes, 1 int argument, straight line | (made after the code read, before the trace) inline while c = size-13 < 74, i.e. up to f9 | f1-f9 inlined, f10 (c 78) not **[C]** |
| a2 | inline/plain/forceinline/static f10, EH callee, EH caller, callee defined later | `inline` no help; forceinline inlined; dtor callee inlined | as predicted; plus: the callee defined later was compiled first (rule 1, found here) |
| a3 | constant and address arguments, nested loop, if arm, branchy callee, two calls | constant arg +5 helps (75, still rejected); weights x5 per loop | as predicted; an if arm is x0.5; a branchy callee loses the -10 |
| a4 | chain c1→c2→c3, mutual recursion, virtual, function pointer | chain re-decided per depth; virtual/pointer not candidates | as predicted; the recursion's back edge went through rule 7 |
| a5 | try, `__try`, `_alloca`, varargs, inline asm, noinline, dllimport, static local, 5000/9000-byte frames | try/`__try`/varargs/noinline/dllimport blocked | as predicted; `_alloca` blocked too; asm, static local and the 5000-byte frame inlined |
| a6 | calls inside `try` and `__try` regions | inlined | inlined |
| a7 | recursion back edge, IL count 8..14 | budget 12 | ≤12 inlined, ≥13 not **[C]** |
| **p1 (blind)** | t1 (80 B, 2 args), t2 (60 B, branch), static t3 in a loop, `T::m` (84 B) in an if arm and in straight-line code, t4 (45 B, 4 calls) | inline, inline, inline (~60), reject (78), inline (69), inline (32) | **5 of 6 held**. t4 got c = 42, not 32: the no-branch bonus also needs no side-effecting call (corrected in rule 3 from `InlCollectBlockCmpInfo` 10b70de0) |
| b1, c1-c8 | IL left by inlining (rules 10-14) | — | the tables above |

## Unknown / not covered

- **`InlBadCandidate`** 10b6a690 (run on every accept): it seems to reject a call whose result is unused when the
  callee has no side effects. Not probed. **[I]**
- **Why `_alloca` callees are rejected.** The test after ICGD was not traced.
- **The flag that adds `2*vol`** to the rule 7 budget (`_Function+0xac & 0x140000` and `& 8`), and the caller's
  call-graph list bit that halves the frame limit.
- **The static block weights** (x5 per loop, x0.5 per if arm) are observed values of `block+400`
  (FgWeightFlowGraph). `switch` arms, `else` arms, nested ifs and loops with known trip counts were not measured.
- **Why GlobalOptimize keeps `sN`** in front of a valued call or in an EH region (rule 12). This needs a read
  of its forward substitution.
- **Struct (UDT) and float returns** (`EntryHasReturnUDT`, `InlFixMBAllocs`, `InlFixLDOBJ`) and `__fastcall` /
  register-convention statics: the size of a static callee depends on its convention, which can change once its
  address is taken (t3 was 100 bytes address-taken and 96 called directly).
- **/O1 (the non-speed path)**: `v = c - B7*15*... - 7 (inline keyword) - 45 (called once, static) ...; inline iff
  v < 9`, read in code only.
- **LTCG/PGO paths** (PtInl*, PogoInline*, `_InlNativeConsts`): not covered.
