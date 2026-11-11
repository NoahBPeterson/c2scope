# From C++ to the IL the register allocator sees

c1xx (the C++ front end) writes IL that c2 reads; c2's early passes then rewrite it before register allocation
chooses its candidates (regalloc.md §7). This page catalogues, construct by construct, what c1xx emits and what
survives to allocation, because a register-allocation fix is only a fix if some C++ produces the IL it needs.

Method: probe functions compiled with the game's flags (`/TP /MT /GS /EHsc /Gy` plus `/O2`), IL dumped with
`c2trace.py il --func '' --at @MscRVO@4,@GrgIdentifyCandidates@4,@Color@4` (docs/c2/tracer.md). `@MscRVO@4` is
the first hook after c2 has read the function, so its dump is c1xx's output; `@GrgIdentifyCandidates@4` is
after GlobalOptimize, just before candidate selection; `@Color@4` is allocation. Probes: work/scratch/fe/ (p1..p9).
**[C]** = shown by these dumps; **[I]** = inferred.

## What c1xx's IL looks like

- One IL tuple per source operation (`OPADD`, `OPMUL`, `OPASSIGN`, `OPCMP` + `OPBRANCH`, `OPCONVERT`, `OPARG` +
  `OPCALL`, `OPRET`), every subexpression in its own temporary, with the source line offset. `if`/`else`
  become branches and labels in source order; a condition repeated in two arms is two separate compares
  (0x00a89680: the Y compare at lines 27 and 32). **[C]**
- **`__forceinline` members are calls in c1xx's IL** (`OPARG this; OPCALL`). Inlining is c2's `Inline` pass,
  so everything about inlined code (its temporaries, its `this` copy) is decided in c2. **[C]** (p1 f03, f04, f08)
- **A ternary is one `OPQUESTION` tuple**, not branches (p1 f09); `&&` is branches. **[C]** So `?:` versus `if`
  changes the block structure the allocator later sees, and with it P and live-through terms. In 0x00a89680 the
  nested-ternary spelling lowered `center` from 74 to 62 but changed the code (127 lines), so it is no fix there.

## What c2 does to it before allocation (GlobalOptimize and friends)

- **Named local vs expression** (f01, f02): `int t = a; h(t)` keeps `_t` as a user variable through to Color (a
  candidate: regalloc.md §7); `h(a)` leaves a single-use temporary (scratch rotation). **[C]**
- **Two reads of the same member** (f03, f04): the optimiser makes one address temporary `t = this+off` at the
  top of the function and loads through it. That temporary is an optimiser temp (no single def recorded), so it
  is a candidate, and it gives `this` an extra reference in that block. Color's FoldTooCheapDefs folds it back
  into `[this+off]` operands, so it leaves no code. **[C]** for the IL; **[I]** that it never survives to code.
  By-value and by-reference accessors produced the same IL in this simple probe; in 0x008e7d40 they differ
  (the f3decomp session's result), so the difference needs calls or aliasing between the reads to show. **[I]**
- **A member read in a loop with no call** (f05) is hoisted to the preheader as one load; `this` leaves the
  loop entirely. **[C]**
- **With a call in the loop** (f07) the load stays in the loop (the call might write it), but the address
  `this+off` is hoisted into the preheader: an extra `this` reference there, and the loop references the
  address temporary instead of `this`. **[C]**
- An array element `arr[i & 7]` (f06) is reassociated to `(i*4 + this)` inside the loop, not hoisted. **[C]**
- An inlined accessor in a loop with a call (f08) leaves `[this+0] + i` in the loop: no hoisting because of the
  call. **[C]**

- **An inlined by-value accessor leaves a return-value variable only in functions with a C++ EH frame**
  (probe p4: a local with a destructor under `/EHsc`). There, `h(getv())` becomes `s = [this+off]; h(s)` with
  `s` an inliner-created local that survives GlobalOptimize and is a Color candidate: it gets an allocated
  register and does not move the scratch cursor. Without an EH frame (e1) the variable is propagated away and
  the load feeds the argument directly (a scratch temporary), and a by-reference accessor (f2, g2) never creates
  one (but see the refinement below and inline.md). **[C]** (IL at GrgIdentifyCandidates and `regs` on p4:
  `local4`/`local5` coloured eax/esi in f1 only.)
  This is the mechanism behind the f3decomp session's by-value `GetName()` result in 0x008e7d40.

- **Aliases of a pointer are free** (probe p6, 7 spellings of passing a pointer parameter to calls in a loop): `p`,
  `*p` to a reference parameter, a reference local `C& r = *p`, a copied pointer `C* d = p`, `&p->first_field`
  and a reference parameter all give identical IL at allocation and identical priorities: c2 copy-propagates the
  alias away before candidates are chosen. Only hoisting the loads into named locals (`int cx = p->x`) changes
  the picture (it changes the code too). **[C]** The same holds in the real 0x008e7d40 unit for 32 spellings of
  its early `name` accesses (`name`, `GetName()`, `this->name`, a reference local `char*& nm = name`, a named
  temporary for the assigned value, operand order of `!=`): all 32 give the same priorities and the same code.
  **[C]** So when a decompiled draft differs from the target only in register allocation, rewriting an expression
  through an alias, a reference or a getter is not a lever unless the getter returns **by value in an EH function**
  (below) or the expression changes which **constants** appear in arithmetic (next item).

- **A constant used by two `add`s becomes a register candidate** (probe p7). When two `OPADD`s with the same
  immediate survive to allocation (`&s->object->local` twice: `object + 52` twice), the constant becomes an
  immediate-candidate globreg (`imm` in `regs`). It is usually not coloured (cost 0 at /O2), so it leaves no
  code, but it counts in P of every block that references it, which raises the priority of every other value
  referenced there. With one such add (a4) there is no candidate. **[C]** Whether the address is an `OPADD` or is
  folded into an addressing mode depends on how the base is reached: in an EH function the inlined by-value
  `visual()` makes the base a variable and `&base->local` folds into `lea ecx,[edi+0x34]` at the use, so only one
  add remains. **[C]** (0x008e7d40 below.)

- **Refinement: what the inlined by-reference `localMatrix()` leaves depends on its `this`.** In 0x008e7d40 (EH
  function), `scene->object->localMatrix()` left a variable `s386 = object + 52` computed before the call's
  argument pushes, while `scene->visual()->localMatrix()` (by-value `visual()`) left `s386 = object` and formed
  `+52` at the use (`lea ecx,[edi+0x34]`). inline.md (rules on the return value and the inlinee's `this`) has the
  general rule: a by-reference return is substituted, and an inlinee whose `this` is reached through a loaded
  pointer gets its own `sN`. Here that `sN` is what appears. **[C]** for the two IL shapes.

- **Where the member-address temporary goes** (probe p5, EH functions, by-value accessor): one is formed for
  `&member` even when the member is read only once (r1), and it is defined in the block of the member's first
  use: the entry block when the first statement reads the member (r1..w2), the branch block when the first read
  is inside an `if` (late). **[C]** Whether a single read forms one outside EH functions is not tested.

## Struct copies (probe p8, /O2, code)

| size | `*d = *s` | by-value argument | return by value |
|---|---|---|---|
| 4 | `mov ecx,[eax]; mov [edx],ecx` | loads it and **stores it back into the caller's own argument slot, then `jmp`s to the callee** (tail call) | in `eax` |
| 8 | two load/store pairs through `edx`/`eax` | `push [s+4]; push [s]` | in `eax:edx` |
| 12..24 | one load/store pair per dword, through `edx` | `sub esp,N; mov eax,esp`, then one pair per dword | through the hidden pointer `$T`, one pair per dword |
| ≥ 28 | `push esi; push edi; mov ecx,N/4; rep movsd` | `sub esp,N; mov edi,esp; rep movsd` | `mov edi,$T; rep movsd` |

The cut-over is between 24 and 28 bytes (6 → 7 dwords) for all three. A tail that is not a whole dword is
copied with a word then a byte (3 bytes: `mov dx,[eax]` + `mov al,[eax+2]`; 6 bytes: dword + word; 9 bytes:
2 dwords + byte). `rep movsd` makes esi and edi busy for the copy, so a struct of 28 bytes or more copied in a
function costs two callee-saved registers (push/pop) unless they are already in use. **[C]**

## Float and int conversions (probe p9, /O2, default /arch, /fp:precise)

- `(int)f`, `(short)f`, `(int)(f + 0.5f)`: `fld` then **`jmp __ftol2_sse`** when it is the return value (a tail
  call), otherwise `call __ftol2_sse`. `(long long)f` uses `__ftol2`. `(unsigned)f` is inline: `fnstcw`, set
  rounding to truncate (`or eax, 3072`), `fldcw`, `fistp qword`, restore. **[C]**
- `(float)i` is `fild` and is returned **without** the store/reload that rounds a float result (x87.md rule on
  float rounding: int → float is the exception). `(float)u` adds `2^32` (`__real@4f800000`) when the sign bit
  is set. **[C]**
- `(double)f` costs nothing. `(float)d` stores and reloads (rounding) when returned; stored to memory it is one
  `fstp DWORD PTR`. **[C]**
- **Literal suffixes do not matter when the value is exact:** `f * 0.5` and `f * 0.5f` give the same code (both
  `fmul QWORD PTR __real@3fe0000000000000`, a double constant), and `f < 1.0` and `f < 1.0f` both become
  `fld1; fcomp`. **[C]** (A literal that is not exact in float, such as 0.1, would differ; not probed.)
- **Float intermediates are not rounded between operations.** `return a + b + c` (floats) and
  `double t = a; t += b; return (float)(t + c)` compile to the same five instructions: one rounding at the
  return. So float versus double for a local intermediate only matters where the value goes to memory (a
  store, a call argument, a spill), not inside one expression. **[C]**

## Applied to 0x008e7d40

With the by-value accessor (both `name` loads become allocated, the scratch sites match), `this` is 85 in
Spillcosts pass 2 against `scene`'s 79 (rasim on the real snapshot agrees with the f3decomp session's 85). Block 1
holds exactly two `this` references: the copy from ecx, and `t576 = this+672`, the optimiser's address
temporary for `&name` (two or more reads of a member create one, see above). `rasim order ... ref:this:1:-2`
(one reference fewer there) gives `this` 77, below `scene`, which is the original's order. So the lead is a
source in which the `&name` address temporary is not formed in the entry block, with everything else as in the
by-value draft. Probe p5 says the temporary goes where `name` is first used, and the draft's (and the target's)
first statement reads `name`. So the open question is narrower: which construct makes that first read go
through something other than `this` (a base-class subobject pointer, the inlinee's own `this` surviving as a
separate variable, a reference member), so that the entry block's address computation does not count against
`this`. Moving it to another block where `this` is already referenced does not help (block 2 has the
same P, so the terms just move).

**Result (sweeps in the real unit, tsweep over 32 + 32 + 64 + 16 + 32 variants).** Spelling the early `name`
accesses differently changes nothing (all 32 identical, see "Aliases" above). Spelling the `scene` accesses
differently does: `scene->object` versus the by-value `scene->visual()` changes `scene`'s and `this`'s
priorities in each of the five places. The combination that reads `scene->object` directly at lines 3998
(`SceneSetup::apply(*cacheSlot, scene->object->localMatrix())`) and 4009 (`copied =
scene->object->localMatrix().FUN_0056fe20(transform)`), with `visual()` elsewhere, ties `this` and `scene` at 85,
and the tie goes to `scene` (defOrd 140 > 7), the original's order. `this` is then split as in the original and the
function differs from the original in **5 instructions** (aligned diff, 481 lines; the previous best was 98),
all at the 4009 call: we emit `add edi,52 / push ecx / mov ecx,edi` where the original has `push ecx / lea
ecx,[edi+0x34]`. Mechanism: reading `object` directly at both lines leaves two `object + 52` adds, so 52 becomes
an immediate candidate (rule above) and raises P in `scene`'s blocks; but at 4009 the same spelling also makes the
inlined `localMatrix()` compute `object + 52` early into a variable, which gives the `add`. The `visual()`
spelling at 4009 gives the original's `lea` and loses the candidate. Every other 4009 spelling tried (8) falls in
one of these two classes. So the original most likely had some other construct at 3998..4009 that leaves a second
referenced globreg in those blocks without code. Draft: `work/scratch/f3/pk/v_best5.cpp`.

**Second session (hacks allowed, aim 100%).** The original's code at 4009 equals the `visual()` spelling
(`mov edi,[edi+8]` early, `lea ecx,[edi+0x34]` late, an ordinary scratch pick), so its extra pressure on `scene`
must come from a no-code value elsewhere. What was learned:
- **A reference bound inside an inline helper creates a no-code variable.** `apply(p, const Matrix& t)` written
  as `const Matrix& u = t; FUN_008db970(..., &u);` (applyF) leaves a variable holding `object+52` that counts in P
  of the 3998 block (`scene` +4, code unchanged). Binding a stack address (`transform`, `copied`) or a value already
  in a variable does nothing (they are substituted or coalesced). **[C]**
- **By-value getter chains in an EH region add variables too** (`p->GetSkeleton()`, `->GetTransform()`, or a
  named `void* tr = ...` local in the helper): each raises P by one in its block, but each replaces a scratch pick,
  which moves the rotation cursor and changes the registers of the next picks. **[C]**
- Best combinations: applyF at 3998 plus a `tr` local in the 4010 `apply` (`v_fz8.cpp`): order right (85/85),
  8 aligned differences, all in the 4010 call (`tr` coloured eax, the original has it in ecx and the skeleton load in
  eax). v_best5 (3 differences, one site) is still the closest. A 32-body grammar sweep of the 4010 helper bottoms
  out at 8.
- The blocker is predicting the final scratch registers: the picks logged by `scratch` do not map one-to-one onto
  the final registers (v_best5's picks at 4010 are edx/eax/ecx, its code is eax/eax/ecx/edx), so something after
  RegAssign renames them. That pass is the next thing to read.

**Matched (byte-exact, 1711 bytes, relocations masked): `work/scratch/f3/pk/v_match.cpp`.** Three changes to
inline helpers on top of the by-value draft, each a reference bound to an address expression:
- `ReleaseString`: `InternedHeader& header = *((InternedHeader*)text - 1); InterlockedDecrement(&header.references);`
  (the shape `RetainString` already had): `this` 85 → 84;
- `SceneRef(CachedScene* v)`: `long& r = p->references; InterlockedIncrement(&r);`: `this` → 83;
- the 3998 `apply`: `const Matrix& u = t; FUN_008db970(..., &u);`: `scene` 79 → 83.
`this` and `scene` tie at 83 and the tie goes to `scene` (defOrd), the original's order; nothing else changes.

**The rule behind it [C]: a reference (or pointer) local bound to an address expression inside an inline
helper survives inlining as a variable.** It counts in P of its block (raising every value referenced there and
lowering every value only live through it), it takes the register the address would have had anyway, and it is
pick-neutral: it does not replace a scratch pick, so the rotation cursor and every later scratch register are
unchanged. Without the binding the address is substituted into its use and no variable exists. Bindings to stack
addresses, or to values already in a variable, are substituted or coalesced and change nothing. By contrast a
named *value* local (`void* tr = ...`) or a by-value getter in an EH region also adds a variable, but it replaces a
scratch pick and so shifts later scratch registers.

**Catalogue of helper-body shapes (probes p10, p11; game flags) [C]:**

| shape inside an inline helper | EH function (local with a destructor) | no EH frame |
|---|---|---|
| `f(&p->m)` (baseline) | no variable | no variable |
| `T& r = p->m; f(&r);` | **+1 variable**, pick-neutral | nothing |
| `T* q = &p->m; f(q);` | +1 variable, pick-neutral | nothing |
| `f(&hs_bind(p->m))` with `template<class T> T& hs_bind(T& x) { T& r = x; return r; }` | +1 variable, pick-neutral | nothing |
| `const T& u = t; f(&u);` (t a reference parameter bound to an address expression, e.g. `s->vis()->local`) | +1 variable | nothing |
| the same with t bound to `*loaded_ptr` | nothing | nothing |
| `T v = p->m; use(v);` (a value local) | +several variables; replaces scratch picks when they were rotation picks | — |

Pick-neutral means the variable replaces a temporary whose register came from a copy hint (the `lea` of an
address from a register base), so the scratch rotation cursor does not move. A variable that replaces a
rotation pick (a value load) shifts every later scratch register. `tools/helpersweep.py` applies the `hs_bind`
rewrite to every member address and reference parameter in the helpers a function uses, measures each, and
predicts combinations (tracer.md).

**Lesson for the decompilation project:** the bodies of inline helpers (refcount, string interning, transform
setters) are part of every caller's register allocation. Two helpers that compile to the same code on their own
can allocate their callers differently. When a function is stuck on a register order with all its own code
right, vary the helper bodies' local bindings (reference vs direct expression) before anything else, and expect
the original helpers to bind references to sub-objects (`T& h = *p; f(&h.x)`), as `RetainString` here did.

## Applied to 0x00a89680

`this` has exactly 2 references in c1xx's IL (the two member calls' `OPARG`), and no c2 pass adds one before
allocation (`il --count _this:` at every early pass). The original never reads a field of `this` (it is only
ever copied to ecx for the two calls), so the address-temporary mechanism above cannot be what gave the
original's `this` its extra priority. Remaining candidates: a change in how `center` reaches the two calls
(rasim: dropping `center`'s reference in the in-loop or post-loop call block closes the gap), or declarations
that differ from the pack's.

## Next probes

What else creates a no-code globreg in a block (the open 0x008e7d40 question): other immediates, multi-def
temporaries, address temporaries of other objects. Inexact float literals (0.1 vs 0.1f). Struct copies inside
loops and with other live values (does the 28-byte cut-over move?).
