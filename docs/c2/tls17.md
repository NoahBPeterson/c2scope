# x64 TLS addresses on cl 17.00: one pointer or base + offset in two registers (2026-11-21)

A function that reads and writes a `__declspec(thread)` variable across calls gets one of two forms from cl
17.00.61030 x64 at /O2, with the same source otherwise:

```
folded (one pointer)                         unfolded (base and offset)
mov  ecx, _tls_index                         mov  ecx, _tls_index
mov  rax, gs:[0x58]                          mov  rax, gs:[0x58]
mov  edx, OFFSET tls_var    ; secrel         mov  esi, OFFSET tls_var    ; secrel, callee-saved
mov  rdi, [rax+rcx*8]                        mov  rdi, [rax+rcx*8]
add  rdi, rdx                                mov  ebx, [rdi+rsi]
mov  ebx, [rdi]                              ...  [rdi+rsi] at every use
```

The unfolded form costs one callee-saved register more (rsi saved and restored: 10 bytes in the small save / set /
call / restore case: 94 against 104 bytes). **The rule: the second form appears when the function has a
floating-point leaf anywhere in its IL as c2 first sees it** (FUNC+0xe4 & 0x80, set by OtlScanFuncLeaves), even one the
optimiser removes before code generation. **[C]** for the code path and the flag (traced), measured on the sources
in tests/data/tls17.

## The mechanism [C]

1. The front end writes each access as `[[gs:0x58 + _tls_index*8] + secrel]`; global CSE makes one address:
   `mov t173 <- [t128+t95*8]` (the block base), `movzxd t171 <- secrel` (the offset, a constant candidate) and
   `lea t167 <- [t173+t171*1]`, and every access uses `[t167+0]`. That is NColor's input in both forms.
2. NColFoldTooCheap (0x10066040, in NColor after NColSubsumption) folds a definition into all its uses when
   NColMdIsFoldableDef accepts it (an lea of a kind-5 address, 64-bit type), NColCanRecalc agrees (the sources are
   not redefined inside the lifetime), and **NColMdNoFoldBenefit (0x100cb140) returns a finite value for every
   use**. No sum is compared: one 0x7fffffff (no fold) at any use keeps the lea.
3. NColMdNoFoldBenefit for an lea definition (0x100cb229..0x100cb385) counts the registers of the address the use
   would get: `n = regs(lea address) * (times the candidate occurs in the use's address) + k + other registers of
   the use's address`, and refuses (0x7fffffff) when `n > 2`. **k is 1 when the call favours speed (its last
   argument, TLS+0xdd08), the function has no floating point (FUNC+0xe4 & 0x80 clear) and TLS+0x1073c != 1; else
   k = 0.** For `[t167+0]` with `t167 = [t173+t171]`: n = 2 * 1 + k + 0, so 3 (no fold) in an integer-only
   function and 2 (fold) in one with a float leaf.
4. Folded, t167 is gone (its tuple becomes op577), the three uses address `[t173+t171]`, and t173 and t171 live
   across the calls: rdi and rsi.

Traced (`c2trace.py -H "100cb140=ecx>8,edx>8,s2,tdccc>0>0xe4" -H @NColFoldTooCheap@4 --exit`): FUNC+0xe4 = 0x4120 in
the integer function (first use: 0x7fffffff, no fold), 0x41a0 with a dead float local (all three uses: 0, folded).

## Which sources set the flag [C, measured]

- Any float / double leaf at OtlScanFuncLeaves: a float local that is assigned but dead (`float f = 0.0f; (void)f;`),
  `if (0.0f) {}`, a float computation whose result is unused, a float store. The IL at NColor need not show it: in
  `fp.cpp` init_c the float multiply is gone by dop2_Codegen, and `ncolsim.il_uses_float` (which reads the IL at NColor)
  says no.
- A global object whose inline constructor stores a float member and then registers the object (tests/data/tls17/
  obj2.cpp): c2 turns the constant stores into static data, so the initializer `??__E..` has no float instruction,
  but the flag is set and the TLS address is unfolded; the same object with an `int` member folds. float and double
  members, and a float 0.0, all set it.
- A float value that reaches the function only through an inlined call (tests/data/tls17/setting.cpp, the object
  extern, measured with the flag at NColor's entry, with and without /EHsc; all 104 bytes and byte-identical to the
  dead-local spelling): a float argument to an inline member or free function that ignores it (`s.Register(2.0f)`
  with `void Register(float)`; a template `InitSetting(s, 2.0f)`; `float def = 2.0f; s.Register(def);`;
  `s.Register(K::kDefault)` with an extern `const float`); a float default argument of an inline wrapper, given or
  not (`inline void AddSetting(T*, float def = 0.0f)`); an inline `Reset(float v)` that only writes a local; a
  float accessor called and discarded (`s.GetFloat();`, `(void)s.GetFloat();`, an inline `float Default()` called
  as a statement); a float comparison in an empty `if` (`if (s.IsDefault(2.0f)) {}`); a scope object whose
  constructor stores a float member (`Registrar r(&s, 2.0f);`). A call expression survives the front end's dead
  statement removal where `(void)0.0f;` does not; the inlined body brings its float leaf (the parameter assignment
  or the load) into the caller's IL, and the optimiser removes it later.
- Not: a float only in a type (`TSetting<float>` whose `Register()` has no float expression); `__noop(2.0f)`.
  A float default argument of a non-inline function (`void Ensure(float = 0.0f)`, a virtual `Add(T*, float =
  0.0f)`) sets the flag but passes the value: `xorps xmm0, xmm0` in the code (110 bytes).
- The out-of-line COMDAT copies of the inline helpers are still emitted (as for any inline function referenced in
  the unit at 17.00, e.g. obj2.cpp's constructors) and dropped by the linker's /OPT:REF; the accessor copies have
  float code, the caller has none.
- Not: `(void)0.0f;`, `(void)(float)0;`, `(void)(g * 2.0f);`, `(void)(double)i;` (statements without an assignment are
  dropped by the front end; spell.cpp k0, k2, k3, k4, k6).

With and without /EHsc the choice is the same (obj2.cpp); with a scope object (its destructor restores) and /EHsc
the initializer gets an unwind frame and the same TLS choice (obj.cpp).

## Beyond TLS [I]

The rule is NColFoldTooCheap's, not TLS's: any `lea T <- [B + I]` with two registers whose uses only take T as an
address base folds in a function with floating point and stays an lea in one without. The TLS case shows it because
the secrel offset is a register (a 32-bit relocated constant, not a displacement). The same k is 0 when the
function does not favour speed (/O1, /Os: TLS+0xdd08 = 0) or TLS+0x1073c == 1; neither was compiled.

## Tools

- `realize.py edit SRC --func F fpflag:1` (x64): the construct **fpleaf** puts `float rz_f = 0.0f; (void)rz_f;` before
  the body's first statement; the verdict is measured (an lea of il_pre is op577 in il: NColFoldTooCheap folded one
  more than the base). tests/data/tls17/base.cpp init_plain: exact, the unfolded form. ncolsim does not model
  NColFoldTooCheap, so the prediction is "no register change".
- The flag per function, from a traced compile: `c2trace.py SRC --raw -H '@NColor@4=ecx>0xe4,tdccc>0>0>4$' -- FLAGS`
  (NColor's ecx is the FUNC; the second spec names the function); bit 0x80 of the first logged word.
- Wanted (not added): c2ncol's export could log the same word, so ncolsim's `fpflag` reads the flag instead of
  looking for float leaves in an IL that may have lost them.
