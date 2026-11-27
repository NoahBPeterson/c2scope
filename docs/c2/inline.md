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

1. **c2 compiles functions callee-first** (bottom-up call-graph passes; exact rule in "Compile order" below).
   When a function calls a function defined later in the file, it is compiled after that callee: in a2.cpp `c_plain` calls `late`, which is defined after it, and the order was
   `... c_ehcallee, late, c_plain`. Otherwise the source order is kept. A cycle (mutual recursion) is broken at a back edge: the function defined first is compiled first. The `/FAsc` listing and the object come out in this compile order.
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
     - A callee whose only EH is a local with a destructor **is** inlined (e2/ie2 in a2) on 14.00; **on 13.10 only into a
     caller that has EH state itself**, see "Why one caller calls a helper its sibling inlines". **[C: a5, a2]**
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

   **13.10 (3052 and 3077): the same model plus a loop term.** Between the early accept and the speed test, 13.10
   adds `size * B / 255` to d, where B is a byte of the callee's call-graph node (+0x4e) that is 0 for a loop-free
   callee and reads as its loop share in 255ths (21% for a 64-byte callee with one 4-instruction loop, 62% for a
   52-byte callee whose body is the loop) **[C: code at 3052 1079452c (`imul size; x/255`, then `idiv` by a
   runtime constant that is 1); traced B and bonus on p3: 0/0, 54/13, 159/32; the meaning of B is I]**. So:

   ```
   d'  = d + floor(size * B / 255)          (13.10; the early accept still tests d, before this)
   v   = (d'*1e8/w + 6d') / 7  if w <= 1e8,  (d'*1e8/w + 2d') / 3  if w > 1e8;  inline iff v < 74
   ```

   A callee that is mostly loop pays up to its whole size again: `big` (34 bytes, c 31, loop 57%) gets v 50 and is
   inlined, `huge` (63 bytes, c 60, loop 79%) gets v 80 even with its call in a loop (w 5e8). Straight-line code
   then needs c + loop bytes < 74, not c < 74. Per site: `C2_BUILD=13.10.3077 uv run tools/c2inline.py f.cpp
   [--func F] [--callee C] -- <cl flags>` prints caller, callee, size, c, d, loop%, d', w, v and the deciding step,
   and checks v against this formula. Static-callee / call-count adjustments of 14.00's d' were not seen on 13.10
   (not tested). **[C: traces p2/p3, both builds]**

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

8a. **(14.00; not on 13.10, see the section below) Two vetoes after the cost model accepts: `InlBadCandidate` 10b6a690** (84 bytes; called at the end of ICGD,
   10b6a46d, so `__forceinline` never reaches it). It returns 1 = do not inline in exactly two cases; there are no
   thresholds. Inputs: the call tuple (eax), the callee's call-graph node (esi; flag byte at node+0xb3), and bit 2
   of the candidate's `+0x1c` (stack argument: 1 when the call site is inside a loop of the caller, 0 otherwise
   **[C trace: 1 for every call in a `for` body, 0 for straight-line code and for an `if` arm]**).
   - **(a) A loop-invariant call to a side-effect-free callee that is not straight-line stays a call.** All of:
     the call site is in a loop; the callee has a branch, a switch or a side-effecting call (node+0xb3 bit 0x80
     clear; this is the same bit that gives the −10 of rule 3, set by `InlCollectInfo` 10b70b50 when the callee
     was compiled); `TupQueryCallSideEffect` 10bf5940 says the call writes nothing (the callee's summary: no
     store to a global or through a pointer; it may read globals and read through pointer arguments); and
     `InlInvariantArgs` 10b6a860 accepts every argument. The call is then left for loop-invariant code motion.
     It is hoisted to the loop preheader when nothing in the loop can change what it reads; **if it cannot be
     hoisted it still stays an out-of-line call inside the loop**.
     - `InlInvariantArgs`: each argument must be a constant, a variable with no definition inside the loop, or
       the result of another such call; a value loaded from memory (`a[i]`, `p->m`) or computed in the loop is
       not invariant. **At most 4 variable arguments** (`this` counts); a call with 5 is "not invariant" and is
       inlined. **[C code for the count; the per-kind tests read only in outline: I]**
     - Probes (`work/scratch/openq/inlbad2.cpp`, `inlbad3.cpp`; callee `if (x > 5) return x*3; return x+1;`, 17
       bytes, far below the rule 3 limit) **[C trace of the return value + listing]**:

       | call in a `for` loop | InlBadCandidate | listing |
       |---|---|---|
       | `br_pure(k)`, k a parameter | 1 | `call` hoisted in front of the loop |
       | `br_pure(9)` | 1 | call hoisted |
       | `br_rd(k)` (callee reads a global), loop has no store | 1 | call hoisted |
       | `br_rd(k)`, loop stores `a[i]` | 1 | **call stays in the loop**, not inlined |
       | `br_ptr(k)` (callee reads `*k`) | 1 | call stays in the loop, not inlined |
       | `o->m(k)` (const member with a branch), `inline` and `static` versions | 1 | call |
       | `br_pure(a[i])`, `br_pure(m)` with `m = k + 1` in the loop | 0 (args not invariant) | inlined |
       | `br_wr(k)` (callee stores a global) | 0 (side effect) | inlined |
       | `pure1(k)` (straight-line callee `x*3+1`) | 0 (bit 0x80 set) | inlined |
       | `a5(k, n, k, n, k)` (5 variable arguments) | 0 | inlined |
       | `__forceinline` version | not called | inlined |
       | the same call outside a loop, or in an `if` arm | 0 | inlined |

       For matching: a small `const` getter with an `if` in it, called in a loop with loop-invariant arguments,
       is a `call` in the original even though the same getter is inlined everywhere else. To get it inlined
       there, the original source must have had a side effect in it, an argument computed in the loop, or
       `__forceinline`.
   - **(b) An unused result of a side-effect-free callee that contains a loop.** All of: the call tuple has no
     destination (`tuple+0x2c` == 0); the callee has a loop (node+0xb3 bit 0x40 = `FUNC+0x0c` loop list non-empty
     when the callee was compiled) or node+0xc is non-zero; and `TupQueryCallSideEffect` = 0. The call is not
     inlined, and it is then deleted as dead: `void U(int k) { lp_pure(k); }` compiles to `ret`. With the result
     used (`return lp_pure(k);`) or a loop-free callee (`br_pure(k);`) the veto does not fire. No effect on code.
     **[C: code; trace inlbad2.cpp U_lp_pure, U_lp_rd = 1, V_lp_pure, U_br_pure = 0]** What node+0xc holds is
     not known (0 in every probe). **[I]**
   - Node flag byte +0xb3, as seen: bit 0 = the callee stores nothing visible (set by `ItfFuncInterf2`
     10b80fb0), bit 1 = it reads no global or pointed-to memory, bits 0x10/0x20 = recursion state
     (`CgIdentifyNodeRecursion`), 0x40 = has a loop, 0x80 = straight-line. Values: `x*3+1` 0x93, reads a global
     0x91, stores a global 0x90 (straight-line) / 0x12 (with a branch), branch only 0x13, loop 0x53. **[C trace
     for the values; the meaning of bits 0 and 1 is I from them]**

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

### Compile order, and when a caller is compiled before its callee (13.10)

`uv run tools/c2inline.py f.cpp --order -- <cl flags>` prints the order c2 compiles the TU's functions in, and every
site has a `body` column: **yes** = the callee had been compiled when the site was weighed (the cost model of rule 3
ran on its size), **no** = it had not (no size; c1xx's IL count decides, rule 7), **never** = not compiled as a
function in this TU (extern, or an inline/static/`__forceinline` callee that is only expanded).
`body` is a **read field**: bit 1 of the dword at callee FUNC+0x69, which `ShouldInlineCallTuple` 1078cf54 tests
before it calls `InlCallGraphDecision` (clear: it takes the IL-size path instead). The order (the `@Inline@4`
entry sequence; `@dop2_EncodeAndRelated@4` gives the same sequence, each function is compiled through to its
emission before the next starts) is a cross-check, and the tool marks a row where the two disagree (none seen).
**[C: traces on both 13.10 builds; tests/data/inline_order.cpp, work/scratch/inline/ord/o1-o4.cpp, m1.cpp]**

What decides the order **[C: code of `CgBottomUpOrder` 1070cfdf, read in disassembly; `CgIdentifyNodeRecursion`
1070ce78; matched against five traced files (o1-o4, a2)]**:

- `CgBuildCallGraph` makes one node per function and an edge per direct call. `CgIdentifyNodeRecursion` walks the
  graph depth-first and marks the edge that closes a cycle as a back edge (edge flag +0xc & 1).
- `CgBottomUpOrder` then makes **passes over the node list in source order**. In a pass, a function is emitted when
  every non-back-edge callee in the TU has already been emitted; it is removed from the list and the pass goes on
  with the next node (so a callee emitted earlier in the same pass releases its caller in that pass). A function
  with a callee defined after it therefore waits for the next pass, and goes after everything the first pass emitted.
  Examples (o1): `plain_c, inl_c, ... , two_c` (all leaves, source order), then `plain_a, inl_a, ... chain_b, two_a, two_b`,
  then `chain_a` (it precedes `chain_b` in source, so it misses pass 2). a2: `... c_ehcallee, late, c_plain`.
- Not an edge, so no ordering: an **indirect call** (function pointer, a virtual call through `this` or a pointer),
  and anything c2 never sees as a call. `V::u` calls `vf` virtually: `u` is compiled before `vf` (defined after it). **[C: o2, inline_order.cpp]**
- Which member of a cycle goes first: the one whose only callees are back edges. The depth-first walk starts at the
  **last-defined** function and goes towards the first, so in a two-function cycle the **first-defined** is compiled
  first (it sees the other uncompiled, body no); in a three-function cycle a, b, c called by an outside function
  defined before them the order is b, a, c, and with the outside caller defined after them c, b, a. Self recursion
  is no cycle problem. **[C: traces m1, o1 mut, o2 cy_*, o3 x_*, y_*; I: that the start is the last-defined node
  (the list that `CgAddNode` builds is pushed at the head; the code that walks it was not read)]**

Source arrangements, with what each did (13.10.3052 and 3077 agree):

| arrangement, caller defined first | caller compiled before callee? |
|---|---|
| callee defined later, plain, `inline`, `static`, `__forceinline`, `__declspec(noinline)`, a template, a class-body member, `operator+`, the address also taken | **no**: callee first (o1, o2) |
| `/Ob0`, `/Ob1`, `/O1`, `#pragma optimize("", off)` / `("g", off)` / `("t", off)` on the caller or callee | **no**: same order; the order does not depend on /Ob or on pragmas (o1, o4) |
| mutual recursion (a calls b, b calls a, a defined first) | **yes**: `a` first, a's call to `b` has body no (m1, o1) |
| a virtual call through `this` or a pointer; a call through a function pointer | **yes**, but these are not inline candidates anyway (o1, o2) |
| a known-type virtual call (`V o; o.vf()`) | no: it is a direct call with an edge (o2) |
| `/Od` or `/Og-` on the command line | **yes, always**: plain source order, no call-graph ordering (o1, o4) |

So in a plain `/O2` compile the only way to get "caller compiled before callee" for a direct call is a cycle in the
call graph (directly or through other functions) with the caller in the position that goes first. A caller whose
call is in a cycle with a callee that is **too big for the IL-size budget of rule 7** is the case that reproduces
"the original made a call where ours inlines": tests/data/inline_order.cpp `cyc_a` (call) against `cyc_b` (inlined,
because `cyc_a` is already compiled). In a cycle the callee's body is never visible to the member that goes first.
**[C for the cycle behaviour; I that this explains the M&B rows: not tested on them]**

**What turns the order off, and what does not (13.10.3077; 3052 spot-checked).** The call graph has a node for a
function only if bit 12 of the dword at FUNC+0x69 (SSR+0x50 0x1000 in il_pipeline.md) is set: the IL reader
(10708c86, just before its `CgAddNode` call at 10708c90) tests it. c1xx writes it per function; a probe there saw it
set on every function at `/O2` and clear on every function at `/Og-` and at `/Od`, and `/Od /Og` brings it back. With
no nodes `CgBuildCallGraph` has nothing to order and the functions stay in c1xx's emission order, which puts
a compiler-generated helper right after its first user: with `/Og-`, `build` then `??_E` of the class it deletes
(`/O2`: `??_E` first). **[C: tests/data/inline_gen.cpp, work/scratch/inline/ord/g1.cpp, p1.cpp]**

- **Per-function `#pragma optimize` never changes the order.** `("g", off)`, `("", off)`, `("t", off)` around the
  caller, the callee, both or the whole file at `/O2` (o4, p2, q_g, q_all), and `("g", on)` for some or all functions
  at `/Og-` (p1, p2): the order is callee-first in the first case and source order in the second, whatever the
  pragmas say, and the 0x1000 bit is the same with and without the pragma. The pragma only decides whether a function
  may inline and be inlined: at `/Og-` a pair with `g` on in both inlines by the IL path (rule 7), with the callee
  `g` off it stays a call, with the caller `g` off the site is never weighed (p2). So the pragma can be scoped to the
  few functions concerned for the *inlining*; the *order* is a TU-wide property of the command line (`-Og`).
- **Command-line switches tried at `/O2`, none changes the order:** `/Ob0 /Ob1 /Ob2 /O1 /Os /Ox /Oa /Ow /Op /Zi /Z7
  /Gz /GS- /arch:SSE2 /fp:fast`. Only `/Og-` and `/Od` turn it off. (`/GL` writes no IL to compare.)
- **c2 has no `-d2` switch for it.** The option table in c2.dll (name, global, flags triples from 108664cc to
  10866a18: `-Ob#`, `-inl*#`, `-vol#`, `-loopopt`, `-ltcg`, `-optref`, `-dlp`, `-dos`, `-datasort`, ...) has no
  ordering option, and a `-d2` prefix is not in it; `CgBuildCallGraph` reads `-optref` (108819ec: unreachable-function
  removal) and `-ltcg` (10881d14: the entry-point list), not the order. c1xx.dll's switch strings include `-Og`,
  `-Binl`, `-Bdlp`, `-BnoImplicitInline`, `-Goptil`; the last three were not tried and the code that writes the
  0x1000 bit in c1xx was not read. **[C for the c2 table; unconfirmed for c1xx]**
- **Two more things the walk does** (14.00 text in il_pipeline.md 4.2; the 13.10 code has the same shape, read at
  1070d00b-1070d0d6, 107c0463, 107c049f): a function **without a node goes first** (pass 0, in list order), and a
  function with **FUNC+0x20 & 0x400000 (a dynamic initializer, il_pipeline.md) goes to a second list that is appended
  last**. Neither was produced on 13.10: functions with `try`, `__try`, `setjmp`, varargs, `_alloca`, inline asm,
  a destructor, a loop, an unknown callee, `noinline` all got nodes and were ordered like plain ones (n1.cpp); a
  nodeless caller would have gone before an earlier-defined leaf. The DFS start (the last-defined function) is read
  in code now: `CgIdentifyNodeRecursion` is called from the head of the node list (1070cc77), and `CgAddNode` pushes
  new nodes at the head. **[C code; the 0x400000 effect not traced]**

**Is the 0x1000 bit per function (late instantiations, generated members, big TUs)? No.** Probed on every function
(`g<_CurFunc>>0>0>0x69`, with the node pointer at +0x99, in the `@Inline@4` hook: work/scratch/inline/ord/nodes.sh):
- t1.cpp (75 functions, `<vector>` and `<string>`: a class-template `operator+`/`operator*`/ctor used only late,
  `??_E`, implicit NavCell ctor/dtor, vector iterator `operator+`), t2.cpp (dynamic initializers `_$E*`, thunk
  targets, covariant return, `dllexport`, `naked`, `__fastcall`, `__stdcall`, `extern "C"`, a guarded static, a
  function-try-block, a switch, explicit and implicit instantiation), t3.cpp (EH, `new`/`delete`, `throw`),
  big1.cpp (1200 functions, one with 1200 calls): **every function has the bit and a node**, on 3077 and (t1) 3052.
  Every order is callee-first. In t1, t2 and g1 no site has body "no" except in a cycle.
- c1xx writes the bit as `(global byte 1058d0e8) << 12` while it composes the function's flag word (c1xx.dll
  1046fffa-10470008). That byte is set once, at the end of option processing, if `-Og` was given (104246b7-104246c3)
  and cleared otherwise (10401012), and nothing else writes it. The same byte gates c1xx's callee-list recording
  (10415d0c). So the bit is a **TU-wide** property of `-Og`, not of the function. **[C code; c1xx.dll 13.10.3077]**
- `/d1<opt>` passes `-<opt>` to c1xx (works through cl). `/d1Binl`, `/d1Og`: no change. `/d1BnoImplicitInline` clears
  0x40 (the inline-candidate bit) of the implicit members, not 0x1000. `/d1Bdlp` and `/d1Goptil` gave no IL to read.
- cl 13.10.6030 (work/toolchain/c2-13.10.6030, untraced `/FAsc` listing of o1.cpp) gives the same order as 3077.

**But a caller-first order does occur at `/O2`, for a call that is not an edge.** `delete[] p` of a class with a
virtual destructor calls the vector deleting destructor `??_E` through the vtable (`call DWORD PTR [edx]`), so there is
no call-graph edge, and c1xx emits `??_E` after its first user: `vdel` is compiled before `??_EDer`
(tests/data/inline_gen.cpp, t3.cpp `eh6`; both 13.10 builds). A direct `??_E`/`??_G` call (class without a virtual
destructor: `build` / `??_ET`, t3 `eh3` / `??_GD`) is an edge and goes callee-first. The same holds for every other
virtual or indirect call. Such a call is never an inline candidate, so it does not explain a site where ours
inlines; it does explain "the original's helper sits right after its caller" when the original's call is virtual.
**[C: listing and order; I: that it is the source of the M&B rows]**

**/GL + link /LTCG (13.10.3077; 3052 same order and layout).** `c2inline.py SRC --with OTHER.cpp,.. [--export NAMES] --order
-- <cl flags>` compiles every source with `/GL` (c2ltcg.py's `build`) and traces `link /LTCG` loading the patched c2.dll;
`--order` then also prints the **image layout** from the link map. Sources: tests/data/inline_ltcg_a.cpp and
_b.cpp (a caller with a same-module helper defined after it, a helper in the other module, a template `operator+`,
`??_E` for `delete[]` of a class with a non-virtual dtor defined in the other module, the same with everything local);
probes in work/scratch/inline/ord/ltcg/ (m, h, x: three modules, with and without exported roots).
- **Compile order under /LTCG is still bottom-up and callee-first**, over the whole program: `late_helper` before
  `a_late`, `??_ET` before `a_vec`, a leaf in another module before its callers. A cycle behaves as in a single TU.
- **Cross-module inlining happens, and every site the model sees is decided as at `/O2`**: `a_far` inlines `far_helper`
  from the other module (body yes), `a_tfar` inlines `far_tpl`, the template `operator+` / `operator*` / ctor are
  inlined in all three modules, `??_ET` into `a_vec` (88 bytes, d -43), with `/OPT:NOREF` and with exported roots.
  No site stayed a call and none had body "no" outside a cycle. **[C]**
- **The image layout is different, and is caller-first.** Without /GL, `/Gy` COMDATs are laid out in compile order
  (map of the same sources: `late_helper, a_far, a_tfar, ??_ET, ??_EL, It<int> members, ..., a_late, a_tpl, a_vec, ...`:
  helpers first). With /LTCG the link map has the functions in **IL order, module by module, first-need helpers right
  after their user**: `a_late, late_helper, a_far, a_tpl, a_tfar, a_vec, ??_ET, a_lvec, ??_EL, It members`, then the
  other module's `T::T, T::~T, far_helper, far_tpl`. That is the shape seen in the M&B image (`build` then
  `??_Etstring`; a caller followed by the helpers it needs). **[C: maps k1 (3077) and k3052, kb (no /GL)]**
  So a helper sitting after its caller in the image is *not* evidence that it was compiled after the caller, and
  the compile order, which is what decides inlining, is the bottom-up one either way.
- Not explained by LTCG: a helper that is **not inlined** where `/O2` inlines it. In every LTCG build here the
  tiny helpers (`operator+` 14 bytes, `??_E` 78-88, vector ctor) were inlined at "accepted at once" or `v < 74`.
- No Rich header can be read from the M&B exe (`mount&blade.exe` / `mb_original.exe`: the stub area at 0x40-0x128 is
  Steam-wrapped data, linker version 7.10, an extra `.bind` section), so the prodid check (a /GL compile has its own)
  could not be done on it. **[C for "no Rich header"]**

**Register conventions, link order and budgets (follow-up on the LTCG question; 13.10.3077, 3052 spot-checked).**
- **One plain `/O2` TU already gives register arguments to statics.** A `static` function whose address is not taken
  takes its parameters in registers (`; _a$ = edx`, `_b$ = edi`, `_c$ = esi` in the listing, the callers load them
  instead of pushing), also `static __stdcall`, `static __fastcall` and a self-recursive static, even when it
  is called once and not inlined (`/Ob0`). Extern functions, an address-taken static, functions in an anonymous
  namespace (external linkage as far as c2 can tell), `inline` COMDAT functions and static members keep the stack
  convention. tests/data/inline_static_cc.cpp, work/scratch/inline/ord/cc/s1.cpp, s2.cpp. No `/GL` is involved, so
  T-region helpers with whole-TU register conventions need no LTCG. **[C: listing, both builds]**
- **Under /GL + /LTCG almost everything gets them.** Same file linked with no exports: every function except the
  address-taken one (`f_addr`) takes register arguments: extern, anonymous-namespace, static member, recursive,
  stdcall (`ltcg.asm` of work/scratch/inline/ord/cc/kl). ltcg.md 2.1 has the rules (not exported, not referenced from a
  non-/GL object, address not taken, not virtual, not varargs): the gate is the same "SSR+0x20 & 0x2400" test, and
  bit 0x2000 is "referenced from outside" (set for exports and non-/GL references by the linker). So an LTCG
  link that keeps the layout but not the conventions needs the functions to be reachable from outside the /GL set:
  exported, or referenced from a non-/GL object. c2's option table has nothing that turns the change off (`-dlp` /
  `-nodlp` is read by the profile-probe code, not by the gate). **[C for the listing; the switch search: the
  table of 108664cc-10866a18 and the references of 108a91ac / 108a91b0]**
- **The /Ob level is recorded in the /GL object and honoured at link:** `/GL /Ob0` inlines nothing at link,
  `/Ob1` inlines the class-body / `inline` / template helpers (13 sites) but not the plain `far_helper` /
  `late_helper` / `far_tpl`, `/Ob2` all (22). **[C: m.cpp + h.cpp]** (M&B's `/Ob1` observation fits "plain helpers
  stay calls", not "helpers defined after the caller".)
- **Mixed link order.** Objects `b1` (plain), `a` (/GL), `b2` (plain), `c` (/GL), linked `/LTCG` in the orders
  b1 a b2 c, a b1 c b2, c b2 a b1 and b1 b2 a c (work/scratch/inline/ord/mix): **all /GL code comes first, in the
  command-line order of the /GL objects, then the plain objects in command-line order**; the layout never
  interleaves. So a binary whose T and B regions interleave was not linked with /LTCG. **[C: link maps]**
- **The link-time budget is per caller, not per module or program.** The bloat term of the cost model
  (rule 3) grows with the caller's own instruction count: a caller `big` with 4000 calls to a 37-byte helper has c =
  24 at the first site, 43 at site 250, 65 at 1500, and from site 555 of 4000 (c = 74) the rest stay calls, while
  another caller `other` in the same TU, compiled after it, still inlines the same helper at c = 24. The caller's
  total size, including calls not yet expanded, sets the term. No counter that accumulates across the program (or
  a module) was found in the paths read (`InlCallGraphDecision`, `ShouldInlineCallTuple`, `InlineeIsTooBig`: all per
  caller). So a big program does not exhaust it; a very large *caller* does, and its remaining helper calls then
  stay calls even for tiny helpers only if the caller is huge (thousands of calls). **[C: big300/1500/4000.cpp;
  I: that nothing global exists]**

So, for "caller first with a later-defined, non-cyclic helper": on this build it needs no call graph, i.e. c1xx run
without `-Og`. With it on, only a cycle, an indirect/virtual call or a nodeless caller (none found) puts a caller
first. Whether an `/Og-` + `optimize("g", on)` TU generates the same code as `/O2` for the other functions was not
checked (the cost model needs a node, so every site there is decided by the IL budget). **[I]**

### Why one caller calls a helper its sibling inlines (13.10.3077 and 3052, `/O2 /EHsc /GS`)

`c2inline.py` now says where a site that passed the cost model was kept: `kept by GetInlinee` etc. (the exits of
`GetCandidateInsertionPoint`, `GetInlinee`, `ShouldInlineInlinee`, `InlineInlinee`, `InlBadCandidate`), and a site that
`InlCallTupleIsCandidate` filtered out has **no row at all** (look for the call in the listing). Evidence: tests/data/inline_site.cpp
(5 checks in `inline_order_tests`), work/scratch/inline/ord/pair/*.cpp (p1, p2, ctx, eh, ret, w, w2-w5, depth, big_it).

**What is not a reason** (minimal pairs, a tiny helper inlined in every one of them): the caller having a `try`,
an object with a destructor live across the call, `__try`, `__asm`, `setjmp`, `_alloca`, a volatile or address-taken
local, a 64-byte buffer, a `__fastcall` or varargs caller, the call in a loop, an if arm, a switch case, a nested call
argument, a condition, a ctor initializer, a destructor body, an unused result (p1, ctx). The site's position in the
function never matters for a helper with d < 14 (the early accept comes before everything below), except `__try`.

**Reasons, in the order the code meets them (13.10):**

1. **Candidate filter** (`InlCallTupleIsCandidate` 1077a3a2 / `InlMightInlineEntry` 10774830; no row when it says no).
   The callee is skipped when its SSR flags say: address taken (+0x20 & 0x400), `__try` (+0x20 & 0x1000),
   `setjmp` (+0x20 & 0x100), +0x20 & 0x40 (varargs), a C++ `try` (+0x69 & 0x200), +0x69 & 0x80000 (not seen set), or the inline bit
   +0x69 & 0x40 is missing (varargs, `noinline`, **a function that returns a class with a destructor by value**:
   `r_dd`, `r_xd`, `k_rethdl`: +0x69 = 0x152a). Also: the call's argument count differs from the callee's
   parameter count. Self-recursive callees (`k_rec`) are skipped too.
   - **A callee with unwind state** (+0x69 & 0x100: a local or temporary with a destructor, e.g. a constructor of a
     class with members that need unwinding, `k_dtor`) is a candidate **only when the caller has EH state too**
     (caller flag +0x8c & 0x60000: any object with a destructor in the caller, or a `try`). A caller without any
     (`e_none`) calls it; the same call in a caller with `Dt d;` (also one constructed after the call, or a temporary
     in the statement) inlines it. A `__try` caller (+0x8c 0x4000) does not count. Without `/EHsc` the callee has no
     unwind state and the rule disappears; under `/EHa` the costs change (k_dtor: call, v >= 74). **[C: eh.cpp,
     inline_site.cpp, flags read at the filter]**. This is the one caller-wide property found that makes a helper
     inlined in one function and called in another, with the same call and no size effect.
   - **What carries the unwind bit (+0x69 & 0x100), probed on 13.10.3077 under `/EHsc`, `/EHs`, `/GX` and `/EHa`**
     (work/scratch/inline/ord/eh/v1.cpp, v2.cpp, probe.py: `uv run work/scratch/inline/ord/eh/probe.py SRC -- /TP /MT /GS /EHsc`
     prints callee +0x69/+0x20, caller +0x8c, the filter's answer and whether the helper's own listing has an EH frame):
     - **It comes from the class layout, not from emitted EH code.** The implicit or user constructor and the destructor
       of a class with a **member or base that has a destructor** have it (`??0MemDt`, `??1MemDt`, `??0BaseDt`,
       `??0UserMem1`, `??0NavLike`, `??1NavLike`, and the same with an inline member dtor): +0x69 = 0x1dea, and their
       listings have **no EH frame** (only the two-member classes `Mem2` / `UserMem` do). A function with a local that has a
       destructor has it too (k_dtor 0x156a).
     - **It is not on `??_E`, `??_G` or `??_H`** (the vector/scalar deleting destructors and the vector constructor iterator:
       0x18ea for every class, also those with dtor-bearing members and bases), on a ctor of a class without a dtor-bearing
       member (`??0Tr`, `??0NavPod`, `??0NavVirt` 0x18ea), nor on functions that only hold a pointer. So `??_Etstring` and
       `??_H` themselves are candidates for every caller.
     - **But what they call is.** The generated `??_G` / `??_E` call `~NavLike` / `~tstring` per element, and that
       destructor has the bit while the generated caller has no EH state (flag 1): **no site, a call**
       (`d_nav` in inline_site.cpp; `??1NavLike` from `??_GNavLike`, `??1MemDt` from `??_EMemDt`). That is the shape of the
       original's `??_Etstring` (a plain element loop with `call ~tstring`). A caller with a local, a temporary, or a
       placement/ordinary `new` of a class with a ctor (flag 0x20001, also when the caller emits no EH frame) inlines them.
     - **EH flags.** `/EHsc`, `/EHs`, `/GX` and `/EHa` all set the bit (0x1dea); with no EH switch the callee reads 0x1cea
       (0x400, no 0x100) and every caller inlines it. Caller flags: plain function 1 under `/EHsc` / `/EHs` / `/GX`, 0 under
       `/EHa`; with an object, a temporary or `new`: 0x20001 / 0x20000; `try`: 0x60001; `__try` 0x4001.
     So a member-wise ctor or dtor of a class like NavCell (a `std::vector` or string member) and the destructor of `tstring`
     are the helpers this rule covers; `??_E` and `??_H` are not. **[C: v1.cpp, v2.cpp, both flag sets;
     I: that c1xx sets it per function that constructs or destroys a dtor-bearing subobject, not from the IL's state changes]**
2. **`ShouldInlineCallTuple` before the cost model:** the inline depth (`#pragma inline_depth(n)`, default 254) counts the
   nesting level of the site; deeper sites stay calls ("decided before the cost model"; with `inline_depth(2)` a
   chain d3 -> d2 -> d1 -> d0 keeps the level-3 call). The pragma applies to the function being compiled. In a
   function with `__try`, only a callee of size <= 10 bytes is inlined, whatever its cost (sx1 inlined, sx2..6 calls).
3. **Cost model (rule 3).** Early accept d < 14, independent of the site's weight. Otherwise v = (d'*1e8/w + 6d')/7
   (w <= 1e8) or (d'*1e8/w + 2d')/3: with a callee of c = 42, if-arm nesting 0, 1, 2 gives v = 42, 48, 60 (inlined), 3
   gives 84 (call), 4 and 5 give 132 and 228. The largest d' still inlined: straight 73, 1 arm 64, 2 arms 51, 3 arms
   36, 4 arms 23, 5 arms none (< 14 is the early accept). A switch case weighs 1e8 / (2 * cases + ...): one case +
   default 5e7, 4 cases + default 1e7 (v 96, call). **A site after a loop weighs 5e8** (the loop's weight), in a loop
   5e8, in a loop nest 2.5e9, in `if` in a loop 2.5e8; loops favour inlining (v 28-33 for c = 42). **[C: w.cpp, w2.cpp]**
4. **A site in a catch handler** (or in code only the handler reaches: `try { return ext(x); } catch (int) {..}
   return h(x);`) with d >= 14 is rejected right after the d test, before the speed test ("rejected after d (a flag
   test)"): `catch (...) { return h(x); }` calls `h` (c 42) and inlines a tiny `t`. The same for `__except`
   and `__finally` handlers and filter expressions. A site in a try *body*, before the try, after a handler that
   returns, or after a try whose body can fall through inlines normally. **[C: w3.cpp, w4.cpp; the exact flag
   not read]**
5. **Bloat** (`InlCallGraphDecision` 1078d1ef): for a callee with size > SizeLimit (9 bytes), when the caller's
   instruction count so far + the callee's instruction count > 9000, `c += (size/2) * (callerInstrs + calleeInstrs)
   / 9000` (a multiply by 0xe90452d5 and a shift: 2^45/9000). The caller's count starts as its whole instruction
   count (counter 10882b58, set by `InlCollectInfo`) and grows by about one callee per expansion. So the term is
   about size/2 times (instructions / 9000): a **6-instruction helper of 14 bytes** adds 7 at 9000 instructions and
   needs about 30000 (d >= 14 at c = -9) to stop being accepted at once, about 110000 to fail v < 74. The flip points for other sizes are computed from the formula, not measured (a 40-byte helper adds 20 at 9000
   instructions, an 85-byte `??_E` with c 35 about 42). Measured: a caller with
   4000 calls to a 37-byte helper (c 24): first call stays at site 555 (c 74), a second caller in the same TU still
   inlines it at c 24 (big4000.cpp). What to measure on the original: the caller's instruction count **before
   inlining** (the listing's count minus what inlining added); under about 9000 the term is 0. **[C code and
   traces; I: that M&B's callers are below it: a function of 9000 instructions is 30-40 KB of code]**
6. **`c > 450` reject** (c = size - benefits + bloat): any helper over about 450 bytes. **`GetInlinee` / `ShouldInlineInlinee` /
   `InlineInlinee`** reject what the earlier tests could not see: a callee with `_alloca` (`k_alloca`: kept by
   GetInlinee), frame limits (rule 2). **A callee not compiled yet** (a back edge of a cycle, or a call with no
   call-graph edge: virtual calls, indirect calls are not candidates anyway) takes the IL-size budget of rule 7.
7. **13.10 has no "loop-invariant call to a branchy callee stays a call" veto** (rule 8a was read on 14.00):
   `InlBadCandidate` returned 0 for every site of inlbad2.cpp except `U_lp_pure` (an unused result of a side-effect-free callee
   that contains a loop: kept by InlBadCandidate, then deleted as dead), and the hoisted-call table of rule 8a does not
   happen. **[C: inlbad2.cpp on 13.10.3077, InlBadCandidate exits, listings]**

**Decision list for a parked site** (a direct call to an inline/template helper that ours inlines):
- **Is the helper's own code size <= 10 bytes (a few instructions)?** Then only the candidate filter, the inline depth, `__try`
  and the bloat (needs ~30000 instructions) can keep it. Check, in this order: (a) is the call inside a `__try` or
  `catch`-handler region of the original (the surrounding code, funclets, a frame with a handler)? For tiny helpers
  a handler does not matter, `__try` does not either when size <= 10; (b) did the helper have unwind state in the
  original (`??_E` / `??_H` / member-wise ctors: the callee that constructs members or elements, each needing
  cleanup)? **Then check whether the calling function has any EH state** (a local with a destructor, a `try`,
  `/EHsc` frame, an `__ehhandler`/`$L` unwind table in the caller): the callers that call it have none, the ones that inline
  it have. (c) is the helper's address taken anywhere in the TU (vtable slot, pointer, `&`): +0x20 & 0x400 stops
  every site. (d) `#pragma inline_depth`: is the call nested under an inlined helper in the original (depth)?
- **Larger helper (c >= 14)?** Additionally: (e) the site's if-arm nesting / switch width (weights above: c = 40
  stops at 3 arms), (f) the caller is a catch handler (calls stay), (g) the caller's size (about 9000+ instructions).
- Everything else of the earlier sections (order, LTCG, /Ob) was refuted for these rows.

Not covered: whether 14.00/15.00 use the same walk (rule 1 was seen there; the cycle start and the `/Od` behaviour
were not tried), `/GL` (the order is then the linker's), `#pragma inline_depth` / `inline_recursion` and `-vol`,
and what the `FUNC+0x20 & 0x400000` skip in `CgBottomUpOrder` is.

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
| p2, p3 (13.10) | loop callees of 34/63/52 bytes, a 79-byte branchy one, sites straight / if arm / loop | the 14.00 model | v off by a loop term: d' = d + size·B/255 (rule 3, 13.10) **[C]** |

## Unknown / not covered

- **`InlBadCandidate`** is settled: rule 8a. Left open there: the exact per-operand tests of `InlInvariantArgs`
  (address arguments, nested calls), what call-graph node+0xc is, and the PGO caller of the veto
  (`PogoPrepAnalyzeCallSites`).
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
