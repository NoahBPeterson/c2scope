# What c1xx hands to c2, construct by construct

Scope: cl 14.00.50727.762 (VS2005 SP1), front end `c1xx.dll`, back end `c2.dll`. This page says, for each C++
construct, which tuples c2 starts from. Its use in matching: you have a decompiled function and several source
spellings to choose from. Spellings that give c2 **the same tuple stream cannot differ in the generated code**,
so they are not worth trying; spellings that differ are listed with the difference and, where probed, with whether
`/O2` still produces the same code.

Companion pages: frontend.md (what c2's early passes do to this IL before register allocation), inline.md (which
calls c2 inlines), frame.md (stack slots), il_tuple.md (tuple format), tracer.md (`c2trace.py il`).

**[C]** = confirmed: seen in an IL dump or listing of the named probe. **[I]** = inferred. A rule's tag names the
probe file and function; all probes are in `work/scratch/feil/`.

**Independently re-tested** (second agent, fresh probes in `work/scratch/feil/verify/`, C++ at the game flags, as free
functions, as member functions with neighbouring code and as member functions with an EH frame): rules carrying
"(re-verified, C++ game flags)" held as stated; rules carrying "**Corrected**" or "**Condition**" were changed by
that pass; "(not re-verified)" means no fresh probe was written. Summary and list of corrections: section 9.

## 0. Method, and what "identical" means here

- **Flags.** Everything is at the game's flags, `/TP /MT /GS /EHsc /Gy /O2`, unless a rule names others.
- **The IL** is the tuple stream at `@MscRVO@4`, the first c2 pass after c2's reader (`RdrReadProc`) has turned
  c1xx's IL files into tuples (il_pipeline.md §5.3). It is therefore "c1xx's output as c2's reader presents it":
  two things in it are the reader's work, not c1xx's, and are marked where they matter (`?:` expansion, rule 1.6;
  switch partitioning, rule 1.12).
- **Harness**: `work/scratch/feil/fe.py` (wraps `c2trace.run_trace`; nothing under `tools/` was changed).

  ```sh
  uv run work/scratch/feil/fe.py il  FILE [--func SUB,SUB] [-- cl flags]   # annotated IL at @MscRVO@4
  uv run work/scratch/feil/fe.py cmp FILE                                  # classes of functions with identical IL
  uv run work/scratch/feil/fe.py asm FILE [--func SUB] [--cmp]             # normalised /FAsc listing, or classes
  ```

  When this page was written `c2trace.py il` printed no branch condition, branch target, call target or
  `OPQUESTION` condition, and skipped non-instruction tuples; it prints all of them now (tracer.md, `il`: notes
  after ` ; `, `P`/`C`/`A` lines), so `c2trace.py il FILE --func F --at @MscRVO@4` is the direct view and `fe.py`
  is needed only for `cmp` and `asm`. `fe.py` adds them from hooks on the tuple constructors (`@TupMakeBranch@28`: target in
  eax, opcode and cc on the stack; `@TupMakeQuestion@36`; `@TupMakeCall@28`, name at `[[call+0x28]+0x18]+4`;
  `@TupMakePragma@4` between `@RdrReadProc@4` and `@MscRVO@4`). In `cmp`, temporaries and labels are renumbered by
  first appearance and tuple addresses and line numbers dropped; variables must have the same names in the
  functions compared.
- **Limits of "identical IL"** in this page. Two functions in one `cmp` class have the same instruction tuples,
  operand types, branch structure and the same *sequence* of pragma tuples (`OPBLKSTART`/`OPBLKEND`...). Not
  compared: line numbers; the position of pragma tuples; symbol attributes that are not in a tuple (`__restrict`,
  `static`, calling convention, EH records behind `OPPUSHSTATE`/`OPDTORACTION`); the per-function option word
  (`#pragma optimize`); values of float constants (printed as `fconst`); `&x` and `x` print alike. Where one of
  these changes the code although the stream is the same, a rule says so (6.3, 6.5, 6.8). (These are limits of
  the `cmp` classes on this page, not of the dump any more: `c2trace.py il` now prints pragma positions, EH
  records, float values and an `&` note; it still does not print symbol attributes or the option word.)
- **IL at `/Od`, `/O1`, `/O2` is the same** for every probe file except in switch statements (the reader's
  partitioning, rule 1.12) and string literals (`/GF`, rule 3A.23): c1xx does not optimise by level. With
  `/fp:fast` float arithmetic is typed differently (rule 2.22). **[C: cf_q, ex_misc, ex_order2, dc_decl, cf_sw
  dumped with `/Od`, `/O1` and `/GS- /Oy- /fp:fast`, diffed against the default]**
- The `/Od` listing follows the IL tuple by tuple, so it is a second view of the front end's output (call
  targets, constants, order). The `/O2` listing does not: c2 reorders even calls (rule 2.4).

IL notation in snippets: `t1` temporary, `_a` user variable, `sN` anonymous compiler symbol, `[t1+0]` memory
operand, `:1004` type word (size in the low 12 bits; class 1 signed, 2 unsigned, 5 float, 6 multi-byte, 9 condition
code). `OPBRANCH(e) t1 -> L1` branches to L1 when the compare in `t1` was "equal". cc names are x86's
(il_tuple.md §6.4); **signedness is in the operand types, not in the cc** (`OPBRANCH(l)` on `:2004` operands is an
unsigned compare).

---

## 1. Statements and control flow

Probes: `cf_if.cpp`, `cf_q.cpp`, `cf_loop.cpp`, `cf_sw.cpp`, `cf_sw2.cpp`, `cf_ret.cpp`.

### Function skeleton and `return`

1.1 **Every function ends `OPGOTO -> Lexit; Lexit:`** (the reader's exit label). **Each `return` statement is its
   own tuple pair** `OPRET <- value; OPGOTO -> Lexit` at its place in the source; `return;` and falling off the end
   are just `OPGOTO -> Lexit`. c1xx never merges returns and never creates a result variable: a function with
   three `return`s has three `OPRET`s. **[C: cf_ret r1, r7; cf_q q7]**

   ```
   int r1(int a) { if (a < 0) return -1; if (a > 10) return 1; return h(a); }
       OPCMP t1 <- _a 0 ; OPBRANCH(ge) t1 -> L1 ; OPRET <- -1 ; OPGOTO -> L3
   L1: OPCMP t2 <- _a 10 ; OPBRANCH(le) t2 -> L2 ; OPRET <- 1 ; OPGOTO -> L3
   L2: OPARG t3 <- _a ; OPCALL t4 <- h t3 ; OPRET <- t4 ; OPGOTO -> L3
   L3:
   ```
   Merging identical return sequences is c2's work (`FgReturnValueMerge`, tail merging). In code, early returns
   (r1), a result variable with one `return r` (r2), a nested `?:` (r4) and nested `if/else` returns (r5) came out
   the same; the `goto out` single-exit spelling (r3) did not (it kept one shared `ret` for the first path).
   **[C: cf_ret asm classes r1 == r2 == r4 == r5, r3 apart]** With destructible locals early returns and a single
   exit differ visibly (rule 3B.3), and a returned value is first copied to a compiler temporary (rule 3B.2).
   **Condition (verify/v1.cpp r1..r5):** the code class r1 == r2 == r4 == r5 holds only for the bare free function
   (re-verified, C++ game flags). In a member function with two statements in front of the same body the class
   splits into r1 == r5 (early returns) and r2 == r4 (result variable, nested `?:`): same instructions, but the
   parameter `a` and the scratch value swap eax and edx (`r1_m` against `r2_m`). In a member function with an EH
   frame r2 == r3 == r4 are one class and r1 == r5 another. So early returns against a result variable is a
   register-allocation lever even without destructible locals. **[C]**
1.2 **`return (e1, e2);` is `e1; return e2;`**, and `g1 = a, g2 = a + 1;` is the two statements: the comma operator
   leaves no trace. **[C: cf_ret r11 == r12, m1 == m2; verify/v1.cpp m1 == m2 in all three contexts (re-verified, C++ game
   flags)]**

### `if`, `!`, `&&`, `||`

1.3 **`if (c) A; else B;`** is `OPCMP; OPBRANCH(not c) -> Lelse; A; OPGOTO -> Lend; Lelse: B; Lend:`. Without
   `else` the branch goes to `Lend`. The blocks stay in source order; c1xx never swaps arms. So `if (c) A; else B;`
   and `if (!c) B; else A;` are different IL (and different code: cf_if j1 against j2). **[C: cf_if i1, j1, j2]** (not re-verified)
1.4 **Logical negation and comparison against zero cost nothing; they only pick the branch sense.** `if (a)`,
   `if (a != 0)`, `if (!!a)` are one class (`OPCMP _a 0; OPBRANCH(e)`); `if (!a)`, `if (a == 0)` the inverse.
   `!(a >= b)` is `a < b`. De Morgan forms are the same IL: `a && b` == `!(!a || !b)`; `a || b` == `!(!a && !b)`.
   **[C: cf_if i1 == i2 == i4, k1 == k3, k6 == k8; cf_q q24 == q25; ex_cast q11 == q12; verify/v1.cpp i1 == i2 == i4,
   k1 == k3 in IL, i8 and q13/q14 same code, q15 different code, in all three contexts (re-verified, C++ game flags)]**
   Not normalised: operand order (`0 != a` is `OPCMP 0, _a`; `b > a` is `OPCMP _b _a` with cc g, not `a < b`;
   `5 < a` keeps the constant on the left) and neighbouring constants (`a > 5` against `a >= 6`). These differ in
   IL; `5 < a` and `a > 5` gave the same code, `a > 5` and `a >= 6` did not. **[C: cf_if i8; ex_cast q10, q13 ==
   q14 in code, q15]** Comparing a bool-valued expression with `true` is not folded: `(a != 0) == true` is
   `OPQUESTION 1:0` then `OPCMP t, 1`. **[C: cf_if i7]** (not re-verified)
1.5 **`&&` and `||` are always branches**, in a condition and in a value context. `if (a && b) S;` is two
   compare-and-branch pairs to the same label; `if (a) if (b) S;` differs only by having two labels at the same
   place. In a value context the result is a two-definition temporary: `t = 1; goto; L: t = 0`. `(a != 0) & (b !=
   0)` is two `OPQUESTION`s and an `OPAND` instead (different code). **[C: cf_if k1, k2, k6; cf_q v1, v6, v8]** (not re-verified)
   Early-return spellings of the same test (`if (!a || !b) return; S;`, `if (!a) return; if (!b) return; S;`) are
   different IL, same code in the probe. **[C: cf_if k4, k5; asm class k1..k5]**
   **Condition (verify/v1.cpp k1..k5):** that probe is a `void` function. When the function returns a value, the
   two-`if` early-return form k5 lays the shared `return` block out differently from k1..k4 (it sits between the two
   tests; one more `jne`), in the free function and in a member function; with an EH frame k4 leaves the class as
   well (k1 == k2 == k3 only). **[C]**
   `a && h(1);` as a statement also tests `h`'s result (a dead compare); `a ? h(1) : 0;` assigns both arms to a dead
   temporary. Same code as `if (a) h(1);`. **[C: cf_if i9, i10; verify/v1.cpp i9 == i10 == i11 in all three contexts
   (re-verified, C++ game flags)]**

### `?:` and comparisons used as values

1.6 **`c ? x : y` is one `OPQUESTION` tuple only when both arms are integer constants.** Otherwise it is
   branches with a two-definition temporary, exactly the shape of `if (c) t = x; else t = y;`:

   ```
   return a ? 1 : 2;      OPCMP t1 <- _a 0 ; OPQUESTION(ne) t2 <- t1 1 2 ; OPRET <- t2
   return a ? b : c;      OPCMP t1 <- _a 0 ; OPBRANCH(e) t1 -> L1 ; OPASSIGN t2 <- _b ; OPGOTO -> L2
                      L1: OPASSIGN t2 <- _c
                      L2: OPRET <- t2
   ```
   Variables, loads, calls and **float constants** as arms all give the branch form. **[C: cf_q q1, q2, q3, q12,
   q13, q17; cf_if j4, j5]** The expansion is done by c2's reader: a trace shows `TupMakeQuestion` for `b ? a : 3`
   followed by the `TupMakeBranch` calls that replace it, before `MscRVO` (t0.cpp f1). **[C]** (not re-verified) (frontend.md's "a
   ternary is one `OPQUESTION`" holds for its probe, constant arms, only.) c2 may rebuild a question later
   (`MscBuildQuestion`, il_pipeline.md).
   Consequences: `r = c ? x : y`, `if (c) r = x; else r = y;` and returns in both arms differ in IL only by the
   temporary (`t2` then `_r = t2`, against `_r` assigned in the arms, against two `OPRET`s) and gave the same code
   (cf_q q5 == q6 == q7 == q8 in code). `int r = y; if (c) r = x;` is a different shape and different code (q9).
   With constant arms, `a ? 1 : 2`, the `if/else` assignment and the two-return form also gave the same code
   (q1 == q10 == q11 == q16 in code). **[C]** (re-verified, C++ game flags: verify/v1.cpp q5 == q6 == q7, q9 apart, q1 ==
   q10 == q11, as a free function and as a member function.) **Condition:** with a destructible local live
   (EH frame) the two-`return` spellings q7 and q11 leave their classes, as rule 3B.3 predicts.
   A nested `?:` nests: the inner one is an `OPQUESTION` if its own arms are constants (q14, q18). An lvalue
   `(a ? g1 : g2) = b` is the branch form on addresses, then a store through the pointer; same IL shape as `*(a ?
   &g1 : &g2) = b` plus one copy, same code; `if (a) g1 = b; else g2 = b;` is different code. **[C: cf_q q19, q20,
   q21]**
1.7 **A relational or `!` in a value context is `OPCMP` + `OPQUESTION(cc) 1 : 0`.** `a < b`, `(a < b) ? 1 : 0` are
   identical IL; `(a < b) != 0` adds a second compare/question (same code); `if (a < b) return 1; else return 0;`
   is branches (same code). `!a` == `a == 0`; `a ? 0 : 1` is the same question with the arms swapped (same code).
   **[C: cf_q q24..q31; verify/v1.cpp q27 == q28 in IL, q27 == q28 == q29 == q30 in code (re-verified, C++ game flags;
   in the EH-frame context the two-`return` form q30 differs)]** For floats the value form is branches (`t = 1; goto; t = 0`), not a question. **[C:
   ex_misc x17, x19]**

### Loops

1.8 Shapes (labels in stream order; `continue` → `Lcont`, `break` → `Lend`) **[C: cf_loop w1, w2, d1, d2, f1, f5,
   f8, c2]**:

   | statement | IL |
   |---|---|
   | `while (c) B` | `Ltop: OPCMP; OPBRANCH(not c) -> Lend; B; OPGOTO -> Ltop; Lend:` (`continue` → `Ltop`) |
   | `for (i; c; n) B` | `i; OPGOTO -> Ltest; Lcont: n; Ltest: OPCMP; OPBRANCH(not c) -> Lend; B; OPGOTO -> Lcont; Lend:` |
   | `do B while (c);` | `Ltop: B; Lcont: OPCMP; OPBRANCH(c) -> Ltop; Lend:` |

   The test is emitted once, where the source has it; loop inversion (`if (c) do ... while (c)`) is c2's.
   `for (;;)` and `while (1)` have no compare at all (`while (true) { if (!n) break; ...}` is the `Ltop:` form with
   the `if`). `while (0)` / `do ... while (0)` keep an `OPCMP 0, 0` and its branch (rule 2.25).
1.9 **Identical IL:** `while (n) { B; n--; }` == `for (; n; ) { B; n--; }` == `top: if (n) { B; n--; goto top; }`
   == `while (n != 0) { B; --n; }`. `for (int i = 0; ...)`, `int i; for (i = 0; ...)`, and `i++` / `++i` / `i = i +
   1` in the increment are one class; bracing the body adds a pragma pair only. **[C: cf_loop w1 == w3 == w9, w7
   (same instruction tuples, one more scope pragma pair), f1 == f3 == f11, f4 (braced)]** A hand-written `goto` loop is therefore not distinguishable from
   `while`, except that the label keeps its name in the listing (`$top$2572:`).
   **Different IL, same code in the probes:** `for (; c; n)` against `while (c) { ...; n; }` (the increment sits
   before the test in the stream); `do/while` against `for (;;) { ...; if (!c) break; }`; `if (x) continue;`
   against `if (!x) { ... }`; `break` in the body against the extra `&&` in the loop condition; `i < n` against
   `n > i`. **[C: cf_loop asm classes w1..w10, d1..d4, f1 == f2 == f10, f5 == f6, f8 == f9]**
   (Re-verified, C++ game flags, verify/v1.cpp, all three contexts: IL classes w1 == w3 == w4, f1 == f3, a `goto` loop
   the same tuples plus a pragma pair; code classes w1 == w3 == w9 == w4, f1 == f2 == f3 == f10, d1 == d2, f5 == f6.)
   **Condition (v1 f8, f9):** `break` against the extra `&&` gave the same code only with the constant bound of
   cf_loop (`i == 3`). With a variable bound (`i == c`) the `break` form gets its own copy of the epilogue and the
   `&&` form shares one, as a free function and as a member function; with an EH frame they are equal again. **[C]**
   Different code: a `while` loop with the increment duplicated before `continue` (f7 against f5).

### `switch`

1.10 **The selector is copied to a temporary first** (`OPASSIGN t <- _a`, after an `OPCONVERT` to int for a small
   type), also when the switch has no cases. **The case bodies stay in source order**, `default` included; a
   `break` is `OPGOTO -> Lend`, fall-through is nothing. An `enum` selector changes nothing; braces around case
   bodies add scope pragmas only (rule 1.15). **[C: cf_sw s1 == s13, s11 (same instruction tuples), s4, s8, s10,
   s14, s15]**
1.11 **The dispatch at the top is one `OPSWITCH <- t` when the case values are dense.** Its case list (value →
   label, default label) is in the tuple (il_tuple.md §6.5); `il` prints it as `default=L..` and one `C <switch>
   <low> <high> L<label>` line per case **[C: cf_sw s1: `C 1 1`, `C 2 2`, default = the `default:` label; s5: the
   reader first subtracts the lowest case (`OPSUB t,1`), compares with the span (`OPCMP t,4; OPBRANCH(g)
   default`) and the cases are `0..4`; adjacent cases with one body are one range entry (`case 2: case 3:` after
   the subtraction = `C 1 2`, work/scratch/ilx/ilx_cc.c c3)]**. Which code that
   becomes (a `sub`/`je` chain, a jump table) is c2's decision: three dense cases gave `sub eax,1 / je / sub eax,1
   / je`, five gave `cmp eax,4 / ja / jmp DWORD PTR $LN[eax*4]`. **[C: cf_sw s1, s5; cf_sw2 t1]**
   **Threshold located (verify/v1s.cpp, all three contexts): three case values are a `sub`/`je` chain, four are a
   jump table** (`cmp ecx,3 / ja / jmp DWORD PTR $L[ecx*4]`), with or without `default`, also for cases 10..13 and
   for 0, 1, 3, 4 (one hole; table of 5). **[C: s3, s3d, s4, s4d, s4o, s4g]** Fallout3.exe agrees: of 342 jump
   tables of the form `cmp reg,N-1 / ja / jmp [reg*4+T]`, 80 have four entries and none has fewer (`verify/f3x.py
   stats`). **[C]**
1.12 **Sparse case sets are already split into compares around smaller `OPSWITCH`es at MscRVO**, by c2's reader
   (the `Sw*` functions of switch.obj), not by c1xx: `case 1: case 2: ... case 7:` arrives as `OPCMP t,0 /
   OPBRANCH(le) default; OPCMP t,2 / OPBRANCH(le) L1; OPCMP t,7 / OPBRANCH(e) L2`; cases 1, 100, 1000, 5000 as
   `OPCMP t,1000; OPBRANCH(g) Lhi; OPCMP t,1000; OPBRANCH(e) L1000; OPSWITCH t; Lhi: OPCMP t,5000 ...`.
   This partitioning depends on the optimisation flags (`/Od` and `/O1` give other streams for the same source);
   it is the one place where the IL at MscRVO is level-dependent. **[C: cf_sw s9, s16; diff of cf_sw dumps at
   /O2, /O1, /Od]**
1.13 **Source order of the cases:** with a jump table the bodies are laid out in source order, so permuting the
   cases changes the code (cf_sw2 t1 against t2: only the block order and the table differ). With two cases and
   a default (a `sub`/`je` chain) all three orders gave byte-identical code, bodies placed default, case 2, case 1.
   **[C: cf_sw2; cf_sw s1 == s3 == s4, cf_ret s1 == s3 == s4 in code]** Also byte-identical for three cases plus
   default in three source orders, and for two cases plus default, as free function, member function and member
   function with an EH frame **[C: verify/v1s.cpp p1 == p2 == p3, t1 == t2 == t3 (re-verified, C++ game flags)]**.
   Since four cases are already a jump table (rule 1.11), a compare-chain switch on dense values never depends on
   the case order; sparse sets (rule 1.12) were not permuted **[I]**.
   `switch` against the equivalent `if / else if` chain: different IL and different code (`sub eax,1; je` against
   `cmp eax,1; jne`). **[C: cf_sw s1, s2; verify/v1s.cpp t1 against c1, IL and code, all
   three contexts (re-verified, C++ game flags)]** An `else if` chain is the same IL as the nested `else { if ... }`
   (cf_if c1 == c2).

### `goto`, labels, block scopes

1.14 A `goto` **that leaves a block scope** is `OPGOTO` to a small landing block that jumps on to the user label
   (`OPGOTO -> Lx ... Lx: OPGOTO -> Luser`); the landing blocks are where scope-exit work (rule 3B.3) goes. A `goto`
   to a label in the same or an enclosing position that leaves no scope (`top: if (b) { ...; goto top; }`) is a
   direct `OPGOTO -> Luser` **[C: verify/v1.cpp w9 (direct), r3 (two landing blocks); corrected: the rule said every
   goto]**. A user label keeps a pragma
   (`OPUSERLABEL`) so its name survives into the listing. **[C: cf_if i6; cf_ret r3; t0.cpp f2]**
1.15 **Every compound statement, and every `if`/`while`/`for`/`switch` statement and substatement, is bracketed by
   `OPBLKSTART`/`OPBLKEND` pragma tuples** (observed counts: a function with only its body has one pair; a `for`
   or `while` statement adds one, an `if` statement two, and each brace pair one more). They are not instructions. Extra braces, empty statements and an empty `else {}`
   change only these pragmas and never the code, **as long as no address-taken local is declared inside**
   (rule 6.5). **[C: dc_scope b1..b5 (IL classes differ only in the pragma line; one asm class); verify/v1.cpp b1 == b2 in
   code in all three contexts (re-verified, C++ game flags)]**

### Conditions with assignments

1.16 **An assignment inside a condition is hoisted in front of it and the variable is re-read**: `if ((x = h(a))
   != 0)`, `x = h(a); if (x)`, `int x = h(a); if (x != 0)`, `if (x = h(a))` and `if (int x = h(a))` are identical
   IL. `while ((x = h(a)) != 0) B` is `Ltop: x = h(a); OPCMP _x 0; ...`, the same code as the `for (;;) { x =
   h(a); if (x == 0) break; B }` and the "call before the loop and at the end of the body" spellings.
   **[C: cf_ret a1 == a2 == a3 == a4 == a5; a6, a7, a8 one asm class; verify/v1.cpp a1 == a2 == a3 == a5 in IL and
   a6 == a7 == a8 in code, in all three contexts (re-verified, C++ game flags)]**

---

## 2. Expressions

Probes: `ex_order.cpp`, `ex_order2.cpp`, `ex_order3.cpp`, `ex_ops.cpp`, `ex_arith.cpp`, `ex_cast.cpp`,
`ex_misc.cpp`, `dc_prag.cpp`.

### Evaluation order

2.1 **Binary operators: left operand first, then right**, for every operator and whatever the precedence nesting
   (`ga() - gb() * gc()` is `ga, gb, gc`, then `OPMUL`, then `OPSUB`). **[C: ex_order3 o1..o13 IL]** (not re-verified)
2.2 **Call arguments: right to left, each followed at once by its `OPARG`; the object expression of a member call
   is evaluated last** and is the last `OPARG`. The `OPCALL` lists its operands first-parameter first.

   ```
   gs()->m2(ga(), gb())      OPCALL t1 <- gb ; OPARG t2 <- t1 ; OPCALL t3 <- ga ; OPARG t4 <- t3
                             OPCALL t5 <- gs ; OPARG t6 <- t5 ; OPCALL t7 <- ?m2@S@@ t6 t4 t2
   ```
   A nested call is completed (its own arguments, then the call) at the point where its value is needed as an
   argument. `this` is an ordinary `OPARG`; that it travels in ecx is not in the stream. For a virtual call the
   vptr load comes after the arguments (rule 3B.16). **[C: ex_order c1, c2, c5, c6, c7, c8; verify/v2o.cpp o14, o16 at
   `/Od` and `/O2`, verify/v4c.cpp `M::c5`, `M::o1` IL (re-verified, C++ game flags)]**
2.3 **Assignment: right side first, then the address of the left side** (`p[ga()] = gb()` is `gb, ga`;
   `gs()->x = ga()` is `ga, gs`). **Compound assignment: left address first, then the right side** (`p[ga()] +=
   gb()` is `ga, gb`; `gs()->x += ga()` is `gs, ga`). **[C: ex_order s1, s8, s9; ex_order3 s1, s4; verify/v2o.cpp o9, o10 at `/Od` (assignment
   only; re-verified, C++ game flags)]**
2.4 **At `/O2` c2 does not keep this order for calls inside one expression.** The stream order above is what the
   `/Od` listing shows; at `/O2` a pass between the second `OptFuncExpr` and `ItfFuncInterf2` (il_pipeline.md
   phase 3) re-linearises the expression and the calls move with it. **[C: `c2trace il` on ex_order s9 at those
   passes]** Observed (ex_order3, IL order → `/O2` call order):

   | expression | IL | /O2 code |
   |---|---|---|
   | `ga() + gb()`, `ga() - gb()`, `ga() - (gb() - gc())`, `ga()*gb() + gc()*gd()` | left to right | same |
   | `ga() - gb() * gc()` (also assigned to a global) | ga gb gc | **gb gc ga** |
   | `ga() + gb() * 2`, `ga() + g1 * gb()` | ga gb | **gb ga** |
   | `gs()->x = ga()` | ga gs | **gs ga** |
   | `gp()[ga()] = gb()` | gb gp ga | **ga gb gp** |
   | `ga() + gs()->x` | ga gs | **gs ga** |
   | `gp()[ga()]` | gp ga | **ga gp** |
   | `gs()->m(ga() - gb() * gc())` | ga gb gc gs | **gb gc ga** gs |
   | call arguments `h2(ga() + gb(), gc())` | gc ga gb | same |

   Every row was reproduced as a free function, as a member function with code in front and as a member function
   with an EH frame: the three contexts always agree, and `/Od` always gives the IL order **[C: verify/v2o.cpp o1..o17
   via verify/calls.py (re-verified, C++ game flags)]**. More shapes from that probe (IL order is left to right in
   all of them):

   | expression | /O2 code |
   |---|---|
   | `ga() * 3 + gb()`, `ga() + -gb()`, `ga() + (gb() + 1)` | ga gb (unchanged) |
   | `ga() + (gb() << 2)` | **gb ga** |
   | `ga() \| gb() & gc()`, `ga() + gb() / gc()`, `ga() < gb() * gc()`, `if (ga() + gb() * gc() > 3)` | **gb gc ga** |
   | `(ga() + c) + gb() * a` | **gb ga** |
   | `ga() + gb() * gc() * gd()` | **gb gc gd ga** |
   | `ga() - gb() + gc() * gd()` | **gc gd gb ga** |

   So the reordering is not limited to products and addresses: the operand that is itself a binary operation on a
   call result (multiply, shift, divide, `&`) goes first, and in the last row even `ga() - gb()` is turned round
   once it has such a sibling. **[C for the rows; I for the wording]**

   **[I]** A product, or an operand that is used as an address, is evaluated before a plain sibling. So in a
   target, a call order that contradicts left-to-right usually means the calls were operands of one expression;
   to pin an order use named locals (`int a = ga(); int b = gb(); ...` keeps `ga gb gc`: ex_order3 s6) or
   separate statements. Argument order (pushes) is never changed.
2.5 **Side effects embedded in an expression are pulled out of it** **[C: ex_order2]**:
   - **Assignments, compound assignments and prefix `++`/`--` are executed before the rest of the expression, in
     left-to-right order, and the expression then re-reads the variable.** `h2(g1 = ga(), gb())` is `ga; g1 = t;
     gb; OPARG; OPARG <- g1` (so `ga` runs before `gb`, against rule 2.2); `(a = b) + (b = 3)` is `a = b; b = 3;
     a + b`; `h2(a += 2, a)` passes the new `a` twice. (c1, c2, c4, c6, c14)
   - **Postfix `++`/`--` are executed after the whole full expression.** The operand is read where it stands (a
     copy `t = a` is made only when the value is needed across the increment) and the increment follows the last
     tuple of the statement, even after a call: `h2(a++, ga())` is `t1 = a; ga; OPARG; OPARG <- t1; call; a = a +
     1`. In a condition the order is compare, increment, branch (`if (a++ > 3)`, `while (a-- > 0)`). In `return
     h(a++) + a;` the result is saved to a temporary, the increment follows, then `OPRET`. `a++ + a++` is `a + a`
     and two increments. `p[i++] = i` stores the old `i` at the old index. (c3, c5, c7..c13, c15..c17)

   Hence `g1 = a++;` is exactly `g1 = a; a = a + 1;`, and `*p++ = v;` exactly `*p = v; p = p + 1;`. **[C: verify/v2c.cpp
   pf1 == pf2, pf3 == pf4, IL and code, all three contexts (re-verified, C++ game flags)]**
2.6 **Chained assignment re-reads the left neighbour**: `g1 = g2 = ga();` is `g2 = t; g1 = g2`, identical to the two
   statements (and the same code as `t = ga(); g2 = t; g1 = t`). With a narrowing in the chain the re-read shows:
   `*pc = *pi = ga()` stores the low byte of `*pi`, loaded from memory. **[C: ex_order s3 == s4, s5 same code, s6 ==
   s7, s10; verify/v2c.cpp ch1 == ch2 in IL, ch1 == ch2 == ch3 in code, all three contexts (re-verified, C++ game flags)]**

### Operators

2.7 **Compound assignment is the spelled-out form**: `x += a` == `x = x + a` (also `-=`, `<<=`, `*=`, `|=`, through
   pointers and members). The address is computed twice in both; they are one IL class unless the address has two
   index parts, where only the operand order of one `OPADD` differs. `x = a + x` has the other operand order
   (different IL, same code). **[C: ex_ops a1 == a2, a3, a4 == a5, a6 == a7, a8/a9, a10..a13, a18 == a19; verify/v2c.cpp
   ca1 == ca2 in IL, ca3 same code, all three contexts (re-verified, C++ game flags)]**
2.8 **`x++`, `++x`, `x += 1`, `x = x + 1` as statements are identical IL** (`OPADD x 1`); `x--` == `x -= 1`
   (`OPSUB x 1`), while `x += -1` is `OPADD x -1` (different IL, same code). Pointer forms likewise: `(*p)++` ==
   `*p += 1`; for `int** p`, `(*p)++` == `*p += 1` == `*p = *p + 1` (`OPADD [p] 4`: the element size is already
   multiplied in). **[C: ex_ops i1..i7, i11..i15; verify/v2c.cpp in1 == in2 == in3 == in4, de1 == de2 in
   IL, de3 same code, all three contexts (re-verified, C++ game flags)]** As values: `return a++` copies the old value to a temporary,
   which is the same code as `b = a; a = a + 1; return b` (i8, i10).
2.9 **Small types**: `c += 1` and `c = c + 1` on a `char` promote (`OPCONVERT` to int, `OPADD`, store the low
   byte), but **`c++` / `++c` add in the 1-byte type directly** (`OPADD t:1001 <- [..]:1001 1:1001`). Different IL,
   same code in the probe. **[C: ex_ops a14 == a15, a16 == a17, one asm class; verify/v2c.cpp cc1 == cc2 in IL, cc3 same
   code, all three contexts (re-verified, C++ game flags)]**
2.10 **No strength reduction and no algebra in the front end**: `a * 2`, `a << 1`, `a + a` are `OPMUL`, `OPSHL`,
   `OPADD`; `a / 2` `OPDIV`, `a % 4` `OPREM`, `-a` `OPNEG`, `0 - a` `OPSUB 0, a`, `~a` `OPNOT`, `a ^ -1` `OPXOR`,
   `a - b` against `a + -b` (`OPNEG` + `OPADD`). All are distinct IL. c2 maps these pairs to the same code: `a*2`/`a<<1`/`a+a`;
   `a*8`/`a<<3`; `a*5`/`a + a*4`/`(a<<2)+a`; unsigned `/2`/`>>1` and `%4`/`&3`; `-a`/`0-a`; `~a`/`a^-1`;
   `a-b`/`a+-b`/`-b+a`; `a+b`/`b+a`; `(a+b)*2`/`a*2+b*2`. Signed `a/2` against `a>>1` and `a%4` against `a&3`
   differ, of course. **[C: ex_arith IL classes all single; asm classes as listed; verify/v2c.cpp ar1..ar23: every listed
   pair is one code class when the value is a call argument, in all three contexts (re-verified, C++ game flags);
   `a * 2 * 4` and `a * 8 + 0 * b` also compile to `a * 8`]**
2.11 **Pointer arithmetic and indexing are the same tuples**: `p[i]` == `*(p + i)` == `i[p]` == `*(i + p)` ==
   `*&p[i]`: `OPMUL t1 <- _i 4; OPADD t2 <- _p t1; [t2+0]`. The scale is an explicit `OPMUL` by the element size
   (never a shift); a constant index is folded to a byte offset (`p[2]` == `*(p + 2)`: `OPADD p 8`). A field is
   `OPADD base, offset` and offset 0 is no tuple, so `s->x`, `(*s).x`, `s[0].x`, `*(int*)s` are one class and
   `s->y`, `*(int*)((char*)s + 4)`, `((int*)s)[1]`, `(&s->x)[1]` another. `s[i].y` == `(s + i)->y`. A global
   array: `arr[i]` == `*(arr + i)`. **[C: ex_ops p1..p4, p6, p7 == p8, p9..p16, p17 == p18, p22 == p23, p25 ==
   p26]**
   Different IL, same code: a named intermediate pointer (`int* q = p + i; *q`; `t = s->next; u = t->next`),
   `p += 2; return *p`, and the association of constant parts (`p[i + 1]` is `(i+1)*4 + p`; `(p + 1)[i]` is
   `(p+4) + i*4`; `*(p + i + 1)` is `(p + i*4) + 4`; three IL classes, one code class). **[C: ex_ops p5, p20/p21,
   p24, p32..p34]** (Re-verified, C++ game flags, verify/v2c.cpp pt1..pt12: the IL classes and the code classes hold in
   all three contexts, with one exception.) **Condition (v2c pt8, pt9):** in a member function with an EH frame
   `gsp->next->next->y` and the spelling with two named intermediate pointers differ: the chain loads through
   ecx, edx (`mov ecx,[eax+24] / mov edx,[ecx+24] / mov esi,[edx+4]`), the named-pointer form reuses eax three
   times. Without the EH frame they are identical. This is frontend.md's "named variable in an EH function" effect
   and makes the named intermediate pointer a real lever there. **[C]**
   The index type shows: an `unsigned` index multiplies as `:2004` (same code), a `short` index gets an
   `OPCONVERT` (movsx), a `char*` base has no `OPMUL`. **[C: p29, p30, p31]** (not re-verified)
2.12 **Pointer difference** `q - p` is `OPSUB` then `OPSHR` by log2 of the size on a signed type (sar); the
   hand-written `((int)q - (int)p) / 4` is `OPDIV` (different code). **[C: ex_ops p27, p28; verify/v2c.cpp pd1 == pd3 (the
   pointer difference is the IL of `(a - b) >> 2`), pd2 different code (re-verified, C++ game flags)]**

### Casts, promotions, truncation

2.13 **Casts between integer types of the same size, pointers, and enums leave no tuple**; only the type words of
   the operands change (`:1004` signed, `:2004` unsigned and pointers in compares). `(unsigned)a`, `(int)u`,
   `(long)a`, `(int)p`, `(int*)a`, `(char*)p`, `(E)a`, enum → int, `(int)(unsigned)a`, `static_cast`,
   `const_cast`, `reinterpret_cast` are all nothing. An enum is a plain `:1004` int everywhere (compare, switch,
   arithmetic). **[C: ex_cast k1..k13 (one asm class per parameter kind); ex_misc e1..e6]** (not re-verified)
2.14 **Narrowing is no tuple either**: the value is used through a narrower view of the same symbol or temporary
   (`(char)a` reads `_a` as `:1001`; a store of an int expression into a `char` is `OPASSIGN dst:1001 <-
   t':1001`). So `gc = a` == `gc = (char)a`. **[C: ex_cast n1, n17 == n18, n19, n24, m1]** (not re-verified)
2.15 **Widening is one `OPCONVERT`, signed or unsigned by the source type** (`char` → int `:1004 <- :1001`,
   `unsigned char` `:1004 <- :2001`, int → `__int64` `:1008 <- :1004`). `__int64` → int is also an `OPCONVERT`
   (the only narrowing that has one). A narrowing-then-widening cast is the convert on the narrow view: `(char)a`
   returned as int is `OPCONVERT t:1004 <- a:1001` (movsx), `(unsigned char)a` the unsigned one (movzx), and
   **`a & 0xff` is an `OPAND`, different IL and different code** (`mov; and eax,255` against `movzx`); the same
   for `(unsigned short)a` against `a & 0xffff`. **[C: ex_cast n2..n16; asm of n8, n9; verify/v2c.cpp nw1..nw4: four code
   classes in all three contexts (re-verified, C++ game flags)]**
2.16 **Integer promotion is explicit: every operand narrower than int gets its own `OPCONVERT` before any
   arithmetic or comparison**, and the result is narrowed by view on the store (`gs = gs * gs` is two converts,
   `OPMUL :1004`, store `:1002`; `gc == 5` is convert + `OPCMP :1004`). c2 narrows again where it can (`cmp BYTE
   PTR gc, 5`). `short = char` and `ushort = uchar` are one `OPCONVERT` to the 2-byte type. `guc = gc` (same size)
   is a plain `OPASSIGN`. **[C: ex_cast n22, n23, n25, m1..m10]** (not re-verified)
2.17 **Signed against unsigned compare**: the usual arithmetic conversions show only as type words. `a < b` with
   `int a, unsigned b`, `(unsigned)a < (unsigned)b` and `unsigned < unsigned` are the same IL (`OPCMP _a:2004
   _b:2004`, cc l). Pointer compares are unsigned. The front end does not fold `u < 0` or `u >= 0` (c2 does: `xor
   eax,eax` / `mov eax,1`). The range idiom `(unsigned)a < 10` is one compare; `a >= 0 && a < 10` is two branches
   (different code). **[C: ex_cast q1 == q2 == q3, q5, q6, q7, q8, q9; verify/v2c.cpp sc1 against sc2 different code, sc3 ==
   sc4 same code, all three contexts (re-verified, C++ game flags)]**

### `bool`

2.18 **Conversion to `bool` is `OPCMP x, 0` + `OPQUESTION(ne) 1 : 0`**, and there are two forms that differ by the
   type of the question:
   - implicit or cast conversion (`gb = a`, `gb = (bool)a`, `gb = a ? true : false`, `gb = p`, `return a;` from a
     `bool` function): the question is 1 byte wide (`t:2001 <- 1:2001 0:2001`);
   - a comparison (`gb = a != 0`, `gb = !!a`, `gb = a < b`, `return a != 0;`): the question is an `int`
     (`:1004`) and its low byte is stored.

   Same code when stored to memory (ex_misc b1 == b2 == b3 == b4 == b5 in code), **different code as a return
   value**: `bool f(int a) { return a; }` is `cmp [a],0; setne al`, `return a != 0;` is `xor eax,eax; cmp [a],eax;
   setne al`. **[C: ex_misc b1..b7, b20, b22 against b23 == b24; cf_q q32 == q34 against q33 == q35, asm]**
   (Re-verified, C++ game flags, verify/v2c.cpp bs1 == bs4 == bs5 and bs2 == bs3 in IL, one code class for the five
   stores, all three contexts; the return-value difference was not re-tested.)
   A float to bool is the branch form on a double compare (b8, b9). `gb = (a >> 2) & 1` has no compare at all (low
   byte of the `OPAND`).
2.19 **Reading a `bool` promotes it**: `OPCONVERT t:1004 <- _x:2001`, then `OPCMP t, 0` for a test. `if (x)` ==
   `if (x != false)`; **`if (x == true)` compares with 1** (`OPCMP t, 1; OPBRANCH(ne)`, different code); `!x` ==
   `x == false`; `return x;` (bool → int) is the bare convert, `x ? 1 : 0` adds compare + question (same code as
   `if (x) return 1; return 0;`, different from the bare convert). `x && y` on bools is branches; `x & y` an
   `OPAND` of two converts. **[C: ex_misc b10..b19; verify/v2c.cpp bo1 == bo2 in IL and code, bo3 different, bo4 different
   from bo5 == bo6, as free and member function (re-verified, C++ game flags); with an EH frame bo5 and bo6 differ
   because bo6 has two `return`s, rule 3B.3]**

### Bit-fields

2.20 **Bit-fields are expanded by c1xx into shifts and masks on the containing unit**, so nothing bit-field-specific
   reaches c2. Unsigned read: `OPSHR unit, pos; OPAND mask` (position 0: only the `OPAND`). Signed read: `OPSHL`
   to the top, then `OPSHR` on the signed type. Write: `OPAND t1 <- value, mask` (then `OPSHL pos`); `OPAND t2 <-
   unit, ~field`; `OPOR t3 <- t2, t1`; store. A constant write folds to `OPAND unit, ~field; OPOR const`; a 1-bit
   set is `OPOR unit, bit`. `f.b++` reads, adds, masks, shifts, merges. Two writes in a row are two complete
   read-modify-write sequences (c2 may combine them). A hand-written mask expression has the same tuples when its
   operands are in the same order (`gw |= 0x100` is the IL of `gbf.d = 1`; `(gw >> 3) & 31` that of the read,
   except that the shift count is typed int for the bit-field and unsigned for the literal `3`).
   **[C: ex_misc f1..f11; dc_prag f1..f6]** (not re-verified)

### Floating point (`/fp:precise`, the default)

2.21 **All `float` arithmetic and comparison is written in `double`**: each float operand gets `OPCONVERT :5008 <-
   :5004`, the operation is `:5008`, and the result is converted back on assignment or return. `a + b` and
   `(float)((double)a + (double)b)` differ only by an `OPPAREN` tuple that an explicit cast leaves (same code).
   Unary minus is the exception: `OPNEG :5004`. A float/double store or copy without arithmetic has no convert.
   int ↔ float are `OPCONVERT`s; `a * i` == `a * (float)i` (int → float `:5004`, then to double). Float compares
   are converts + `OPCMP :5008`. **[C: ex_misc x1, x2, x6..x10, x15..x23]** (not re-verified)
2.22 **Literals**: `2`, `2.0f`, `2.0` multiplied with a float are all a double constant in the IL, so the suffix
   does not matter when the value is exact in float (`a * 2`, `a * 2.0f`, `a * 2.0` one code class; `1` and
   `1.0f` in `gf2 * gf2 + 1`). When it is not exact the suffix decides the constant: `a * 0.1f` multiplies by
   `__real@3fb99999a0000000` (the float value widened), `a * 0.1` by `__real@3fb999999999999a`. A constant
   expression is folded in its own type first (`a * (1.0f / 3.0f)` uses `3fd5555560000000`). `a / 2` is `OPDIV` in
   the IL and c2 turns it into a multiply by 0.5 (same code as `a * 0.5f`). A named `const float KF = 2.5f` is a
   float constant with a convert (`OPCONVERT :5008 <- fconst:5004`) where the literal is a double constant; same
   code. **[C: ex_misc x3 == x4 == x5, x11, x12, x13 == x14, x24 == x25, c12, c13; dc_prag k5 == k6 in code; verify/v2c.cpp
   fl1 == fl2 == fl3, fl4 against fl5 different, fl6 == fl7, code classes in all three contexts (re-verified, C++ game
   flags)]**
   With `/fp:fast` the converts are gone and float operations are typed `:5004` (ex_misc dumped with `/fp:fast`).

### Constant folding in the front end

2.23 **Folded**: constant subexpressions (`2 + 3 * 4` → 14, `1.0f / 3.0f`, `sizeof`), constants that are adjacent
   after reassociation of a left-leaning chain (`a + 2 + 3` and `2 + 3 + a` → `a + 5`; `(a + 1) + 1` → `a + 2`),
   the identities `a * 1`, `a + 0`, `a / 1`, `a << 0`, `a & ~0` (no tuple), `0 && a` → 0, and a literal on the left
   of a commutative operator moves right (`-1 + a` == `a + -1`). **[C: ex_misc c1, c2 == c3, c4 == c5 == c8 ==
   c14 == c17, c18, c21; ex_arith m27 == m28; verify/v2c.cpp cf1 == cf2 == cf3 in IL, all three contexts (re-verified,
   C++ game flags)]**
2.24 **Named constants**: a `const int` with a constant initialiser at namespace scope (also `static const`), an
   enumerator and a `#define` are the literal (one class). A constant-index element of a `const int[]` is folded
   too (but stays on the left: `KA[1] + a` is `OPADD 2, _a`). An `extern const int` and a non-const `static int`
   are memory operands. A **local** `const int k = 4` is substituted in its uses, but its initialising store is
   still emitted (`OPASSIGN _k <- 4`, dead for c2). **[C: dc_prag k1 == k4, k2, k3, k7, k8, k9 == k10 == k11;
   ex_misc c15]**
2.25 **Not folded** (left to c2, which removes them at `/O2`): `a * 0`, `a - a`, `-(-a)`, `a * 2 * 4` (two
   `OPMUL`s), `a - -1` (`OPSUB a, -1`), `a && 0`, and **conditions on constants**: `1 ? a : 5`, `if (0) ...`,
   `while (0)`, `do ... while (0)` all keep an `OPCMP const, 0` with its branch, and the dead arm's tuples are
   handed to c2. **[C: ex_misc c6, c7, c9, c10, c16, c19, c20, c22, c23; verify/v2c.cpp cf4, cf5, cf6 differ in IL from
   their folded forms and give the folded code, all three contexts (re-verified, C++ game flags)]**

---

## 3A. Objects, data side (rules cited as 3A.n): `this`, references, struct values, initialisers, temporaries, statics, literals

Flags: `/TP /MT /GS /EHsc /Gy /O2` unless a rule says otherwise. "IL" is the tuple stream at `@MscRVO@4`
(`fe.py il`), "code" the `/FAsc` listing (`fe.py asm`; `oa_fe.py` is the copy used while probing, its two fixes
are now in fe.py). Probes: `work/scratch/feil/oa_*.cpp`; a rule's tag names file and function.
Type words in snippets: `:1004` int/pointer, `:600c` a 12-byte struct ("multi-byte", class 6).

### Member access and references

1. **`m`, `this->m` and `(*this).m` are the same IL**: `OPADD t = _this, off` then `[t+0]` as the operand; a
   member at offset 0 is `[_this+0]` with no add (`*(int*)s`, `*&s->a` and `s->a` are one class).
   **[C: oa_ref m1/m2/m3, f0/f1/f2]** (not re-verified)
2. **A reference is a pointer in the IL, with no trace of the difference.** Reference and pointer parameters
   (`S&` / `S*`, `x = 5` / `*x = 5`), reference and pointer locals (`S& t = *this` / `S* t = this`,
   `int& x = b` / `int* x = &b`) give identical tuples. `const S*`, `S* const` change nothing.
   **[C: oa_ref r1/r2/r5/r6/r7, w1/w2, m4/m5, m8/m9]** (not re-verified)
3. **A named alias local (pointer or reference) is one extra `OPASSIGN _t <- _s`**, and later operands use `_t`.
   So `S& t = *s; t.b` differs in IL from `s->b`, but the /O2 code is the same in every probe here (c2
   copy-propagates; frontend.md "Aliases of a pointer are free" gives the exceptions: inline helpers in EH
   functions). **[C: oa_ref r3/r4 vs r1, w3 vs w1, a3 vs a1; code classes equal]** (Re-verified in part: verify/v6.cpp u7, u8,
   u11 give the code of the direct use in all three contexts; but see rule 2.11's condition, verify/v2c.cpp pt8/pt9,
   for an alias that does change registers in an EH function.)
4. **A reference member costs a load like a pointer member**: `r` is `t2 = [this+off]; [t2+0]`, identical in
   shape to `*p`. **[C: oa_ref m6, m7]** (not re-verified)
5. **Array member indexing**: `s->arr[i]`, `*(s->arr + i)`, `i[s->arr]`, `(&s->arr[0])[i]` are one class:
   `OPADD t1 = s, off; OPMUL t2 = i, 4; OPADD t3 = t1, t2; [t3+0]`. The scale is an explicit `OPMUL` by the
   element size; addressing modes are c2's work. **[C: oa_ref a1/a2/a4/a5]** (not re-verified)

### Struct values: return

6. **Which structs come back in registers is decided by c1xx, from the type and from the kind of function, and
   shows in the IL.** **Corrected (verify/v3b.cpp, verify/v3a.cpp): the three cases below are for free functions and
   static member functions only. A non-static member function returns every struct through the hidden pointer, and
   the pointer is always named `___$ReturnUdt`**, whatever the type: plain aggregates of 1, 4, 8 and 12 bytes
   included (`R4 M::q4()`: `mov eax, ___$ReturnUdt$[esp-4] / mov [eax], edx / ret 4`), and for `__thiscall`,
   `__stdcall` and `__cdecl` members alike (a `__fastcall` member gets the pointer in edx). `this` comes first, the
   hidden pointer second (rule 4.9). Static members follow the free-function cases (`M::s4`, `M::s8` return in eax
   / eax:edx). **[C: v3b `f4`, `f8`, `f1` (registers), `f3`, `f12` (`$T`), `fc4` (`___$ReturnUdt`), `M::q1`, `M::q4`,
   `M::q8`, `M::q12`, `M::d4`, `M::c4`, `M::f8`, `M::s4`, `M::s8`; v3a `M::q1`..`M::qe0c`: 19 member functions, all
   `___$ReturnUdt`]** For free functions the cases are (re-verified, C++ game flags):
   - Returned as a value (`OPRET <- _r:6008`, eax or eax:edx): size 1, 2, 4 or 8 **and** a plain aggregate:
     no user constructor, destructor, copy constructor or `operator=`, no base class, no virtual function, no
     private/protected member, no const or reference member. Member types do not matter (`float`, `double`,
     two floats, a union, `char[8]`, an empty struct = size 1 all qualify; a float struct is returned in eax,
     not st0). **[C: oa_ret dP4/dP8/dF4/dD8; oa_sz r1/r2/r8/re/rfd/ru/ra]**
   - Plain aggregate of any other size (3, 5, 6, 7, 12, 16...): hidden pointer, an **unnamed** symbol (`$T<n>`
     in the listing), `OPMBASSIGN [hidden] <- value; OPRET <- hidden`. **[C: oa_sz r3/r5/r6/r7/r16; oa_ret dP12]**
   - Anything that fails the aggregate test, whatever its size (4 bytes included): hidden pointer named
     **`___$ReturnUdt`**. **[C: oa_ret dC4 (ctor), dK4 (copy ctor), dX4 (dtor), dV4 (virtual), dB4 (base),
     dA4 (operator=); oa_sz rpm (private member), rcs (const member), rrf (reference member)]**
   So in a listing **of a free or static member function**, `$T` vs `___$ReturnUdt$` at `[esp+4]` tells you
   whether the original type was a plain aggregate, and adding a constructor to a 4- or 8-byte struct changes
   every caller. In a non-static member function neither holds: the name is always `___$ReturnUdt$` and a small
   aggregate is returned through memory regardless, so a getter returning an 8-byte POD by value is `ret 4` with a
   hidden pointer if it is a member and `ret 0` in eax:edx if it is a free function.
7. **c1xx performs no NRVO.** `T r; ...; return r;` always arrives as: construct `_r`, then copy into the return
   slot (`OPMBASSIGN [___$ReturnUdt] <- _r`, or a copy-constructor call `??0T@@QAE@ABU0@@Z(ReturnUdt, &r)` when
   one is declared), then `_r`'s destructor action. **NRVO is c2's `MscRVO` pass** (the dump at `@Inline@4`, the
   next hook, has `_r` replaced by `___$ReturnUdt` and the copy gone). **[C: oa_ret dC4/dK4/dX4/dV4 at MscRVO
   vs `c2trace il --at @MscRVO@4,@Inline@4` on dV4]** It runs at /O2 and /O1, not at /Od (the copy-ctor call
   stays). **[C: oa_nrvo n1 with /Od, /O1]** (not re-verified)
8. **When MscRVO elides the copy (code, class with copy ctor and dtor):**

   | shape | elided? | probe |
   |---|---|---|
   | one `return r;` (also `return (r);`, also with `if/else` before it, a `throw` path, other locals with destructors declared before, after or in a nested block) | yes | oa_nrvo n1, n7-n9, n11, n12; oa_nrvo2 m1-m6 |
   | two `return r;` statements naming the same local | **no**: copy ctor + dtor at each | oa_nrvo n2 |
   | `return r;` on one path, `return s;` or a temporary on another | no | oa_nrvo n3, n4, n14 |
   | `return p;` for a by-value parameter; a `static` local; `K& q = r; return q;` | no | oa_nrvo n13; oa_nrvo2 m8, m7 |
   | one `return r;` reached by `goto` (`if (a) goto out; ...; out: return r;`) | yes | verify/v3a.cpp `M::n3` |
   | one `return r;` inside a loop, `r` declared in the loop body, a second `return K(0)` after the loop | no (copy ctor + dtor) | verify/v3a.cpp `M::n4` |
   | the only `return` is `return r;` inside `for (;;) { K r(a); h(a); if (..) return r; a++; }` | **yes, and the destructor of the iterations that do not return is gone**: the loop constructs into `[___$ReturnUdt]` again without destroying the previous object; only the unwind funclet still calls `??1K` | verify/v3a.cpp `M::n5` |

   The rows above the added ones were reproduced in member functions with a may-throw call between construction
   and return (`M::n1`, `n2`, `n6`, `n7`, `n8`, `n9`: elided, not, elided, elided, not, not) **[C: verify/v3a.cpp
   (re-verified, C++ game flags)]**. The `n5` row is wrong code from the compiler, not a probe artefact: a target
   that shows a loop constructing into the return slot with no destructor call on the back edge has this shape.

   **[C]** for each row; **[I]** that the criterion is "exactly one OPRET and its value is one non-static local"
   (MscRVO was not read). For a type with a constructor but trivial copy and no destructor the copy is an
   `OPMBASSIGN`, and c2 removes it even with two `return r;` (oa_nrvo2 t2 builds in `[ReturnUdt]`), but not with two
   different locals (t3: both built on the stack, 3-dword copy on each path). **[C]**
9. **Returning a temporary or a call is elided by c1xx itself** (no local exists in the IL): `return K(x);`,
   `return x;` (converting constructor) construct into `___$ReturnUdt`; `return gK();` passes `___$ReturnUdt`
   on as the callee's hidden argument. **[C: oa_nrvo n5 == n6, n10]** (not re-verified) `K r(x)`, `K r = K(x)`, `K r = x` are one
   IL class (copy-initialisation from a temporary is elided by the front end), and after MscRVO they give
   n5's code. **[C: oa_nrvo n7 == n8 == n9; code class n5..n9]** (not re-verified)
10. **A plain aggregate returned through the hidden pointer is never forwarded or built in place.** At the call
    site c1xx always passes the address of a fresh temporary and then copies twice: `OPMBASSIGN t <- [ret]`,
    `OPMBASSIGN sN <- t` (a second compiler temporary), and a third `OPMBASSIGN` into the named destination.
    `P k = gP();` and `P k; k = gP();` are identical IL; `return gP();` copies the callee's temporary into the
    own hidden pointer instead of passing the pointer through. **[C: oa_nrvo d1 == d2, p4, p5 == p6, d3]** (not re-verified) c2
    folds the chain to one copy (callee's `$T` → destination), but the frame keeps both slots (24 bytes for a
    12-byte struct in d1) and the copy stays. **[C: code of d1..d4, p5]** (not re-verified) A class with a copy constructor is
    built straight into the named local instead (`gK(&k)`). **[C: oa_nrvo c1 == c2]** (not re-verified)
11. **A return-by-value function whose class has a destructor carries a "return value constructed" flag.** The
    code sets a local `$T = 0` on entry and bit 0 after the return slot is constructed; the unwind funclet
    destroys `[___$ReturnUdt]` only if the bit is set. When MscRVO elides and no EH frame remains, the dead
    `mov DWORD PTR $T[esp], 0` and its stack slot (`push ecx`) survive. **[C: oa_nrvo n1 (store survives), n2/n13
    (funclet `and eax,1 / je / and $T,-2 / jmp ??1K`)]** The IL shows it as `OPPUSHSTATE <- (result of the copy
    ctor)` just before `OPDTORACTION`/`OPRET`.

### Struct values: parameters and assignment

12. **By-value argument, no copy constructor: one `OPMBARG t:60NN <- value` per argument** of any size (4, 8,
    12; frontend.md's table gives the code per size). A user constructor or destructor does not change this
    (bitwise copy; the **callee** destroys its parameter: `OPPUSHSTATE <- 0` on entry and `OPDTORACTION` before
    `OPRET` in the callee, nothing in the caller). **[C: oa_ret pP4/pP8/pP12/pC4/pX4, qX4]** (not re-verified)
13. **By-value argument with a copy constructor**: `OPINTRINSIC t = (size)` reserves the argument slot, the copy
    constructor is called with that slot as `this`, a second `OPINTRINSIC :6004 <- slot, size` turns it into the
    `OPMBARG`. Code: `push ecx; mov ecx, esp; push src; call ??0K@@QAE@ABU0@@Z; call callee`. A temporary or a
    by-value call result is constructed **directly in the slot** (`uK(gK())`: `push ecx; push esp; call gK;
    call uK`, no copy). When the class also has a destructor the IL has `OPPUSHSTATE` after the slot's
    construction and `OPPOPSTATE` right before the `OPCALL` (ownership passes to the callee).
    **[C: oa_ret pK4, pgK4; oa_tmp t9, t10]** (not re-verified)
14. **Parameter read in the callee**: a by-value struct parameter is an ordinary local symbol; fields are
    direct symbol operands (`OPRET <- s3:1004`), no address arithmetic. **[C: oa_ret qP12, qK4]** (not re-verified)
15. **Struct assignment without a user `operator=` is one `OPMBASSIGN` pair for the whole object, padding
    included**: `t:600c <- [s]; [d] <- t`. A reference spelling is identical; bit-fields, nested structs and
    arrays wrapped in a struct are the same single block copy. **[C: oa_asg s1 == s3, s6 == s12, s7 == s17]**
    Member-by-member assignment is different IL (one `OPASSIGN` per member, offsets by `OPADD`), and
    `d->p = s->p; d->d = s->d` is a 12-byte `OPMBASSIGN` plus a scalar store; at /O2 all of these gave the
    same code as the whole-struct copy for 12 and 16 bytes (below the `rep movsd` cut-over; not checked above
    it). **[C: oa_asg s2, s8 vs s1, s7: code classes s1 == s2 == s3 == s4, s7 == s8]**
    **Cut-over located (verify/v3c.cpp `cp*`/`cw*`/`cm*`, verify/v3d.cpp `cp21`..`cp30`): c2 expands an `OPMBASSIGN`
    into load/store pairs when the block needs at most 6 moves (dword, word and byte pieces counted separately)
    and uses `rep movsd` (+ `movsw`/`movsb`) otherwise.** Inline: 4..24 bytes in dwords, 21 and 22 bytes (5 dwords +
    1 piece). `rep movsd`: 23 bytes (5 + word + byte = 7 pieces), 25, 26, 27 and everything from 28 up. So "28
    bytes" is the cut-over only for sizes that are multiples of 4. **Member-wise assignment is the whole-struct
    code only up to 24 bytes; from 28 bytes on it stays N separate load/store pairs and is never turned into `rep
    movsd`** (checked at 28, 32, 36, 40, 64). An `int[N]` wrapped in a struct and N `int` fields copy identically
    at every size. **[C]** Fallout3.exe: the smallest constant count in front of a `rep movsd` is 7 dwords (26
    sites); no count of 6 or less occurs (`verify/f3x.py stats`). **[C]**
16. **With a user `operator=` (own, in a member, or implied by a vtable) the assignment is a call**:
    `??4O@@QAEAAU0@ABU0@@Z(d, s)`. For a class that only *contains* such a member, or has a vptr, c1xx
    generates an implicit `??4M` / `??4V` (memberwise; the vptr is not copied), emits it as a COMDAT function
    after its first user and calls it; c2 may inline it. **[C: oa_asg s9, s10, s11, `??4M`, `??4V`]** (not re-verified)
17. **Value-initialised temporary `P()`**: a `memset` intrinsic on a compiler temporary (`OPINTRINSIC <- &tmp,
    0, 12`) followed by `OPMBASSIGN` to the destination; `P z = P(); *d = z;` adds one more copy in the IL and
    gives the same code. **[C: oa_asg s15, s16 (code class equal)]** (not re-verified)
18. `(*d = *s).b` re-reads through `d` (`[d+4]`); `*d = *s = *d` is two independent copy pairs.
    **[C: oa_asg s14, s13]** (not re-verified)

### Aggregate and array initialisers

19. **A local aggregate initialiser is always inline stores, never a copy from a static image**: one `OPASSIGN`
    per written initialiser, in order, for arrays and structs alike, constants or expressions, 4 or 10 or 40
    elements, `const` or not. **[C: oa_init i1, i8, i9, i13, i15, i16; verify/v3c.cpp `in4`..`in100`, `ic4`..`ic100`: 4, 8,
    16, 32 and 100 `int` initialisers are 4..100 immediate stores, `const` or not; `is1` (array of `{const char*,
    int}`) 12 stores; `if1` (8 floats) `fld`/`fstp` pairs (re-verified, C++ game flags)]** `int a[4] = {1,2,3,4}` and four
    statements `a[0] = 1; ...` differ in the IL only in the numbering of the anonymous element symbols
    (`s2, s4, s6, s8` vs `s2..s5`) and give the same code; `P p = {1,2,3}` and three member stores are
    identical IL. **[C: oa_init i1 vs i2 (code equal), i9 == i10]** (not re-verified) If the original has `rep movsd` from a
    `.rdata` table into a local array, the source had a `static const` table and a struct copy or loop, not an
    initialiser.
20. **Missing trailing initialisers become one `memset` intrinsic for the rest**, starting right after the last
    written element: `OPINTRINSIC <- &elem_k, 0, bytes_left`. `= {0}` is a store of element 0 plus a memset of
    size-4; `= {}` is a memset of the whole object (no store). c2 expands a small memset to stores (`xor eax,eax`
    + dword stores, seen up to 15 bytes) and calls `_memset` for larger ones (60 bytes and up here; the
    threshold was not located). **Threshold located (verify/v3c.cpp `mz*`/`ms*`/`mi*`, verify/v3d.cpp `A::mz33`..`mz48`,
    the latter in member functions): the zeroing is expanded inline when it takes at most 10 stores (dword, word
    and byte pieces counted separately), otherwise it is `call _memset`.** Inline: every size tested up to 38 bytes, and
    40 bytes (10 dwords). Call: 39 bytes (9 dwords + word + byte = 11), 41 and up. For `= {0}` the memset covers
    size-1 or size-4 bytes, so `char a[36] = {0}` (35 bytes: 8 + 1 + 1) is inline and `char a[40] = {0}` (39
    bytes) is a call, while `char a[40] = {}` is inline; `int a[16] = {1}` (60 bytes) is a call, `int a[8] = {1}`
    inline. **[C]** **[C: oa_init i3, i4, i5, i6, i7, i11, i12, i16; code of i3..i6, i16]**
    `{0}` and `{}` give the same code at 16 bytes. **[C: i4 == i5 code]** **Condition (verify/v3h.cpp, verify/v3c.cpp
    `ms16`, `mz16`): that is for `int` arrays** (`i0` == `iz`), where the first element and the rest are both
    dwords. For a `char` array they differ: `char a[16] = {0}` (and `= ""`, same code) is a byte store of `a[0]`
    and a 15-byte fill laid out from offset 1 (`mov BYTE PTR a, al / mov DWORD PTR a+1 / +5 / +9 / mov WORD PTR
    a+13 / mov BYTE PTR a+15`), while `char a[16] = {}` is four aligned dword stores. Zero stores at odd offsets
    in a target therefore mean `= {0}` or `= ""`. **[C]** Note the order in i3's code: the
    zeroing stores come first, the explicit `a[0] = 1` after.
21. **A `char` array initialised from a string literal is a block copy from the literal's COMDAT**:
    `OPMBASSIGN _s:60NN <- ??_C@...` with NN = the literal's length including the NUL, then rule 20's memset
    for the remaining bytes (`char s[64] = "abc"`: 4-byte copy + memset 60). 4 bytes becomes an immediate store
    (`mov DWORD PTR _s$, 6513249`), 37 bytes `rep movsd` + `movsb` from the literal. `{'a','b','c',0}` is four
    byte `OPASSIGN`s instead, and four byte stores in the code (c2 does not merge them). `char s[16] = ""` copies the 1-byte literal `??_C@_00...` and
    memsets 15; `= {0}` stores a byte and memsets 15; same code (re-verified, C++ game flags: verify/v3h.cpp `c0` ==
    `ce`). `const char s[] = "abc"` is the same as the
    non-const array. **[C: oa_init c1, c2, c3, c4, c5, c6 == c7 (code), c11]** The copy from the literal follows
    rule 15's cut-over: immediate stores when the literal (NUL included) needs at most 6 pieces (sizes tested: 2..22 and 24
    bytes), `rep movsd` from `OFFSET ??_C@...` for 23, 25 and more. **[C: verify/v3c.cpp `cs2`..`cs64`, verify/v3d.cpp
    `cs21`..`cs30`]**
22. Float initialisers are `OPASSIGN :5004 <- fconst` per element (code: `fld`/`fstp` pairs from `__real@`
    constants, `fldz`/`fld1` for 0 and 1). **[C: oa_init c13, c14]** (not re-verified)

### String literals

23. **Under /O2 every string literal is the COMDAT symbol `??_C@_<len><hash>@<text>@`**, the same symbol for
    every occurrence, so identical literals are one object per unit and one per image after linking.
    `const char* s = "abc"; f(s)` and `f("abc")` give the same code. A literal used at an offset (`"xabc" + 1`)
    is its own symbol plus `OPADD` (code `OFFSET ??_C@...+1`); it is not merged with `"abc"`.
    **[C: oa_init c8 == c9 (code), c12]** (not re-verified) This is /GF, which /O2 (and /O1) turn on: with `/Od` the two `"abc"`
    in one function are two different anonymous symbols (`$SG<n>`), and `/Od /GF` gives `??_C@` again.
    **[C: oa_init c12 with /Od, /Od /GF]** (not re-verified)
24. A `static const char s[] = "abc"` is a named object (`?s@?1??f@@YAXXZ@4QBDB`), not pooled with the literal.
    **[C: oa_init c10]** (not re-verified)

### Class temporaries

25. **A temporary with a destructor is `OPPUSHSTATE` right after its constructor (or the call that returns it)
    and one `OPDTORACTION` at the end of the full expression.** The destructor call is not in c1xx's IL as a
    call: `OPDTORACTION` is a marker that c2's EH passes expand. Several temporaries give several
    `OPDTORACTION`s in a row; the code destroys them in reverse order of construction.
    **[C: oa_tmp t1, t2, t7, t8, t13, t14; code of t2]** (not re-verified)
26. **The value of the full expression is saved before the destructors run.** `return gK().get();` is
    `s1 = call; OPDTORACTION; OPRET <- s1`; `int r = uK(gK());` assigns `_r` first. A function returning a plain
    int with any destructor pending at the `return` gets this extra compiler temporary (`sN`), also for named
    locals (`K k; ...; return k.a;` → `s2 = _k; OPDTORACTION; OPRET <- s2`). **[C: oa_tmp t1, t4, t7]** (not re-verified)
27. **A condition that creates a temporary is materialised before the branch.** `if (uK(K(1)))` is
    `s1 = call; OPDTORACTION; OPCMP s1, 0; OPBRANCH`. A `for`/`while` condition `i < uK(K(3))` becomes a bool:
    `OPQUESTION(l) 1 : 0` into a 1-byte temporary, `OPDTORACTION`, `OPCONVERT` back to int, `OPCMP != 0`.
    The temporary is constructed and destroyed on every iteration. **[C: oa_tmp t11, t12]** (not re-verified)
28. **A conditionally constructed temporary (`x ? uK(K(1)) : 0`, `x && uK(K(1))`) has one unconditional
    `OPDTORACTION` after the merge point in the IL**; c2 guards it with a flag: a local `$T` zeroed on entry,
    bit 0 set after the constructor, `test bl,1 / je` around the destructor call, and the unwind funclet tests
    and clears the same bit. **[C: oa_tmp t5, t6; code of t5]** (not re-verified)
29. **A temporary bound to a `const T&` local is a named local `_$S<n>`** with scope lifetime: the reference is
    `OPASSIGN _k <- &_$S1`, and the object is destroyed where a named `K k = gK();` would be. The IL differs
    from the by-value local only by that pointer; the code is the same apart from the symbol name.
    **[C: oa_tmp t3 vs t4; oa_nrvo c5 vs c1, d4 vs d1; verify/v3g.cpp `U::k1` against `U::k2`: member functions with
    an EH frame, listings identical except `_k$` / `_$S1$` (re-verified, C++ game flags)]**
30. **A temporary passed to a `const T&` parameter**: with a copy constructor/destructor the callee's hidden
    return pointer result is passed straight on (`uK(gK())`: one temporary); for a plain aggregate rule 10's
    double copy happens first and the address of the second temporary is passed. **[C: oa_ret prK4, prP12;
    oa_nrvo c4, d3]**
31. Assignment from a by-value call into an existing object without `operator=` (`k = gK();`) is temporary +
    `OPMBASSIGN` + destructor of the temporary, even for a class with a copy constructor.
    **[C: oa_nrvo c3; verify/v3g.cpp `U::k4` is its own IL and code class against `U::k1` == `U::k3` (re-verified, C++
    game flags)]**

### Static locals

32. **Constant-initialised statics have no code**: `static int n = 5`, `static P p = {1,2}` are initialised
    data. `static const int n = 7` is folded to the literal in the IL, and so are a `static const float` and
    a constant-index element of a `static const int[]` (`t[1]` → `2`). **[C: oa_static s1, s6, s7, s11]** (not re-verified)
33. **Dynamically initialised statics use one guard dword per function, one bit per static in declaration
    order** (`?$S<n>@?1??f@@...@4IA`, bits 1, 2, 4...; statics in different blocks of one function share it).
    IL per static: `OPAND t = guard, bit; OPCMP t, 0; OPBRANCH(ne) skip; OPOR/OPASSIGN guard |= bit;
    OPPUSHSTATE <- guard; <initialiser>; OPPOPSTATE; skip:`. The bit is set **before** the initialiser runs.
    Code: `mov eax,1 / test BYTE PTR guard, al / jne / or DWORD PTR guard, eax` for the first, `test BYTE PTR
    guard, 1`-style immediates when eax is not free. **[C: oa_static s2, s3, s5, s8, s9; verify/v3e.cpp `M::st3`: three
    statics, one of them in an inner block, one guard `?$S1@`, bits 1, 2, 4 (re-verified, C++ game flags)]**
    **The 33rd dynamically initialised static of a function starts a second guard dword** (`?$S2@` for statics
    1..32, byte tests for the low bits and `test DWORD PTR` with a register mask for the high ones; `?$S3@` from
    the 33rd, again from bit 1). **[C: verify/v3e.cpp `M::st34`]**
34. **The guarded initialiser is an EH state**: if the initialiser contains a call that can throw, the function
    gets a C++ EH frame, with one unwind funclet per static that clears its bit (`mov eax, guard / and eax,
    -2 / mov guard, eax / ret`). An initialiser with no call (`static P p = {x, 2}`) has the same IL markers
    but no frame in the code. **[C: oa_static s2, s3, s5 (frame), s8 (no frame)]** So a hand-written
    `static int n; if (!n) n = h(1);` (no guard, no frame) is never equivalent. **[C: s10]** (not re-verified)
35. **A static with a destructor registers `atexit` inside the guarded region, after the constructor**:
    `OPCALL _atexit(<thunk>)`, the thunk being a separate function `??__F<name>@?1??f@@...@YAXXZ` whose body is
    `mov ecx, OFFSET obj / jmp ??1K@@QAE@XZ`. A constructor without a destructor has no `atexit`.
    **[C: oa_static s4, s12, s5]** In the listing the thunks come at the end of the unit, last-declared first
    (`??__Fb` before `??__Fa`), and the functions that own such statics (s4, s12) were emitted after all other
    functions of the probe. **[C: listing order of oa_static; the general ordering rule belongs to §4]**
    **Condition (verify/v4d.cpp):** "last-declared first" holds among the static locals of **one function** (`a2`
    before `a1`); across functions and namespace-scope objects the stubs are in definition order (`p1`'s two, `g0`,
    `p2`'s `b1`, `g1`), and all of them come after all `??__E` initialisers. The owning function is emitted late
    because its `??__F` stub, which it references by address, is at the end of c1xx's list: rule 4.13 then defers
    the function by one sweep (`p0, p3, p1, p2`). **[C]**

### Spellings that are identical after the front end (same IL class at MscRVO)

| spellings | probe |
|---|---|
| `b` / `this->b` / `(*this).b` | oa_ref m1-m3 |
| `S* t = this; t->b` / `S& t = *this; t.b` | oa_ref m4, m5 |
| `int& x = b; x` / `int* x = &b; *x` | oa_ref m8, m9 |
| `S* s` / `S& s` / `const S* s` / `S* const s` parameter; `s->b` / `(*s).b` | oa_ref r1, r2, r5, r6, r7 |
| `*x = 5; hp(x)` (pointer param) / `x = 5; hp(&x)` (reference param) | oa_ref w1, w2 |
| `s->arr[i]` / `*(s->arr + i)` / `i[s->arr]` / `(&s->arr[0])[i]` | oa_ref a1, a2, a4, a5 |
| `s->a` (offset 0) / `*(int*)s` / `*&s->a` | oa_ref f0-f2 |
| `*d = *s` / `d = s` through references | oa_asg s1, s3 |
| struct copy of a bit-field struct / a padded struct / an array wrapped in a struct of the same size | oa_asg s6 == s12, s7 == s17 |
| `P p = {1,2,3}` / `P p; p.a = 1; p.b = 2; p.c = 3;` | oa_init i9, i10 |
| `int a[4] = {..}` / `const int a[4] = {..}` | oa_init i1, i15 |
| `K r(x)` / `K r = K(x)` / `K r = x` | oa_nrvo n7-n9 |
| `return K(x)` / `return x` (converting ctor) | oa_nrvo n5, n6 |
| `K r = gK()` / `K r(gK())` (also as a local: `K k = gK()` / `K k(gK())`) | oa_nrvo n11 == n12, c1 == c2 |
| `P k = gP()` / `P k; k = gP()` (plain aggregate) | oa_nrvo d1 == d2, p5 == p6 |

Different IL, same /O2 code in the probe (worth trying only when chasing register allocation in an EH function,
frontend.md): alias local vs direct use (rule 3); member-wise vs whole-struct copy at 12/16 bytes (rule 15);
`a[0]=1;...` statements vs initialiser (rule 19); `{0}` vs `{}` (rule 20); `char s[16] = ""` vs `= {0}`
(rule 21); `const char* s = "abc"; f(s)` vs `f("abc")`; `const K& k = gK()` vs `K k = gK()` (rule 29);
`K r(x); return r;` vs `return K(x);` (rules 7-9); `P z = P(); *d = z;` vs `*d = P();`.

Different IL **and** different code: plain aggregate vs class with ctor/dtor/private member for a 4/8-byte
return (rule 6); two `return r;` vs one (rule 8); `k = gK()` vs `K k = gK()` for a class (rule 31);
`{'a','b','c',0}` (four `mov BYTE PTR` stores, not merged) vs `"abc"` (one dword store) (oa_init c2, c1);
guarded static vs hand-written flag (rule 34).

### Open questions

- MscRVO's exact acceptance test (rule 8 is a black-box table; 0x10b9dc00 was not read). Answered by probe
  for two shapes: a single `return r;` reached by `goto` qualifies, and so does one inside a loop, with the lost
  destructor described in rule 8 (verify/v3a.cpp n3, n5).
- Answered (rule 15): member-wise assignment is not merged above the cut-over; the cut-over is 6 moves, not 28
  bytes.
- Answered (rule 20): the `_memset` threshold is 10 stores. Whether `/Oi-` changes it was not tested.
- Rule 10's surviving second stack slot: which of the two temporaries the frame keeps was not traced.
- Execution order of two calls feeding one binary operator, with temporaries (oa_tmp2 t2, t2b, t2c: both
  constructors run before either call): the stream order at MscRVO is not the `/O2` execution order; rule 2.4.
- Answered (rule 33): the 33rd dynamic static starts a second guard dword. Thread-safety is absent in this
  compiler (no lock in the IL) **[I]**.
- By-value struct parameters larger than 8 bytes with a destructor: who destroys when the callee is inlined
  was not probed.

---

## 3B. Objects, code side (rules cited as 3B.n): construction, destruction, virtual dispatch, member pointers, new/delete

Probes (all in `work/scratch/feil/`): `ob_ctor.cpp` (placement of ctor/dtor calls), `ob_body.cpp` (ctor/dtor
bodies, implicit members), `ob_misc.cpp` (empty/implicit dtors, arrays, returns of class type), `ob_virt.cpp`
(virtual calls, multiple-inheritance casts), `ob_thunk.cpp` (adjustor thunks, MI ctor/dtor), `ob_pm.cpp`
(pointers to members, virtual bases), `ob_new.cpp` (new/delete). IL is the dump at `@MscRVO@4`
(`uv run work/scratch/feil/fe.py il FILE --func NAME`), code is `/O2` with the game flags
(`fe.py asm`). `ob_lst.py FILE DIR` keeps a whole listing (thunks, vtables, COMDAT order).
For all four files checked (`ob_new`, `ob_ctor`, `ob_virt`, `ob_misc`) the IL is the same at `/Od` and `/O2`,
function for function: c1xx does not look at the optimisation level for anything in this section. **[C]**

A function can appear several times in an `il` dump: c2 reads an inline candidate's body again for each
caller that might inline it. The copies are identical.

### Destructors are not calls in the IL

1. **A constructor is an ordinary `OPARG this; OPCALL ??0T` followed by `OPPUSHSTATE`; a destructor of a local
   is no call at all.** c1xx emits one `OPDTORACTION` at each point where control leaves the scope; c2's EH
   phase expands it into the destructor calls for every state pushed since the matching level, in reverse order
   of construction, and into the `__$EHRec$` state stores. So the order of destructor calls is never a
   property of the spelling: it is the reverse of the `OPPUSHSTATE` order. **[C]** (ob_ctor c1..c8; listing of
   c3, c4: `e` then `d`.)
   ```
   c1:  OPARG t1 <- _d ; OPCALL t2 <- ??0D t1 ; OPPUSHSTATE <- t2
        OPARG/OPCALL h ; OPASSIGN s3 <- t4 ; OPDTORACTION ; OPRET <- s3 ; ... OPPOPSTATE
   ```
2. **A returned value is copied to a compiler temporary before the destructors run**: `return h(a);` with a
   live destructible local is `sN = h(a); OPDTORACTION; OPRET sN`, for constants too (`s3 = 1`). Without such
   a local it is `OPRET t` directly. In code this is the `mov esi, eax` around the destructor call. **[C]**
   (c1, c4, c8, c11.) (not re-verified)
3. **Every exit from a scope gets its own `OPDTORACTION`, and c2 does not merge them.** Three `return`s with
   two live objects give three complete copies of the destructor sequence in the `/O2` code (ob_misc r1); the
   single-exit spelling `r = ...; return r;` gives one (r2). `break`, `continue` and the normal end of a loop
   body each get one (ob_ctor c5: three `OPDTORACTION`s for one `D d` in the body). `goto` out of a scope goes
   through a small landing block holding the `OPDTORACTION` and a second goto (c6); at `/O2` that block is
   merged with the scope's normal exit when the code after it is the same. **[C]** So the number of destructor
   call sites in a decompiled function counts the original's exits from that scope: early returns versus a
   result variable is a real, visible choice.
   **Corrected (verify/v3f.cpp, verify/v3e.cpp `M::r3`): "c2 does not merge them" is true in the IL but not in the
   code. c2 merges the per-exit destructor sequences whenever the exits are otherwise identical, and they are
   identical unless the returned values are different constants.** Member functions with `D d; D e;` and three
   `return`s:

   | the three returns | copies of the `~e; ~d` sequence in the code | probe |
   |---|---|---|
   | `return 1; return 2; return h(a);` (the ob_misc r1 shape) | 3 (6 destructor calls, 3 `ret`) | v3f `t1` |
   | `return 1; return 2; return 3;` with or without a may-throw call before | 3 | v3f `t2`, `t8` |
   | `return m[1]; return m[2]; return m[3];` (member loads) | **1** (each exit loads into esi and jumps to the shared tail) | v3f `t3`, v3e `M::r3` |
   | `return h(1); return h(2); return h(3);` | **1** | v3f `t4` |
   | `void`: `return;` twice and the fall-off end | **1** | v3f `t7` |
   | exits with different sets of live objects | one per set | v3f `t6` |

   A loop body with `break`, `continue` and a normal end (three `OPDTORACTION`s for the inner object) has two
   inner-destructor call sites, not three (v3f `t5`). **[C]** So the count of destructor call sites is a lower
   bound on the number of exits, and equals it only when the exits return distinct constants or leave different
   objects alive. The single-exit spelling and the early-return spelling can still differ in registers (rule
   1.1's condition).
4. **Inner-scope objects**: the `OPDTORACTION` sits at the closing brace, before the next statement (c3, c7).
   A local declared after an early `return` is constructed after it and is not destroyed on that path (c4).
   **[C]**
5. **Full-expression temporaries** (`h(D(a).x)`): the temporary is a compiler local (`sN`/`$T`), constructed
   through the same `OPARG/OPCALL/OPPUSHSTATE`; here `OPPUSHSTATE` has a result (`t4 <- t3`), the object's
   address, used for the member access. Its `OPDTORACTION` is at the end of the full expression, i.e. *after*
   the assignment of the expression's value (`_r = t6; OPDTORACTION`). A temporary bound to a `const T&`
   local is a named local `$S1`, the reference is assigned its address, and it lives to the end of the block.
   **[C]** (c12, c13.) (not re-verified)
6. **Conditionally constructed temporaries** (`a ? h(D(a).x) : 0`, `a && h(D(a).x)`): the IL shows only the
   constructor in the taken arm and one `OPDTORACTION` after the merge. c2 adds a flag word (`$T`, zeroed in
   the prologue, bit set after the constructor), tests it before the destructor call (`test bl, 1; je`), and the
   unwind funclet tests and clears the same bit. The flag is not visible at MscRVO. **[C]** (c14, c15; listing
   of c14.) (not re-verified) The same flag appears for a by-value return through two different locals (rule 23).
7. **Initialisation spellings**: `D d(a);`, `D e = a;` and `D f = D(a);` are the same IL: one constructor call
   on the variable, no temporary, no copy constructor. **[C]** (c10; verify/v3g.cpp `U::i1` == `U::i2` == `U::i3`, IL
   and code, member functions with an EH frame: re-verified, C++ game flags.)
8. **An empty inline destructor still gets `OPPUSHSTATE`/`OPDTORACTION` and the constructor call** in the IL
   (ob_misc e0); c2 inlines both to nothing and then the function has no EH frame. A destructor with any body
   (e1: `x = 0`, which is then dead) keeps the EH frame even though its inlined body leaves no code on the
   normal path. **[C]** The rule for "gets an EH frame" is therefore c2's (after inlining), not c1xx's: c1xx
   always emits the states. (Re-verified, C++ game flags: verify/v3e.cpp `M::e0` no frame, `M::e1` frame; verify/v5a.cpp
   `a22`, `a23`.) **Condition (verify/v3g.cpp `td1`, `td2`): `~T() {}` is empty only in a class without virtual
   functions.** In a polymorphic class the user-written empty destructor stores the vptr (rule 12), which counts
   as a body: `VD o; uvd(&o);` with `~VD() {}` keeps the EH frame and a funclet `jmp ??1VD`, while the same class
   with no destructor declared (`VE`) has no frame. **[C]**
9. **Arrays of objects**: a local `D arr[3]` is one call of `??_L` (`eh vector constructor iterator`: address,
   element size, count, `&ctor`, `&dtor`), `OPPUSHSTATE <- 0`, and its `OPDTORACTION` becomes a call of `??_M`
   (`eh vector destructor iterator`: address, size, count, `&dtor`), in the body and in the unwind funclet. A
   member array does the same inside the constructor. For a class with a constructor but no destructor the
   iterator is `??_H` (`vector constructor iterator`, 4 arguments), and c1xx emits `??_H`'s body as IL into the
   object (a COMDAT function with a loop: `n--; if (n < 0) done; f(t); t += s`). **[C]** (ob_misc la, `??0ARR`,
   `??1ARR`; ob_new n13.) (not re-verified)

### What is in a constructor or destructor

10. **Constructor order is fixed by the class, not by the mem-initialiser list.** `C::C(int a) : m2(a), i(a),
    B(a), m1(a), j(i)` (scrambled) emits: base constructors in base-list order, each followed by
    `OPPUSHSTATE`; then the vptr store(s); then the members in declaration order (constructor call +
    `OPPUSHSTATE` for class members, a plain `OPASSIGN` for scalars, interleaved in declaration order); then
    the body. Members without an initialiser that have a default constructor are still constructed at their
    declaration position; scalars without one get nothing. The constructor returns `this` (`OPRET <- _this`
    at the exit label, after `OPPOPSTATE`). **[C]** (ob_body `??0C@@QAE@H@Z`, `??0C@@QAE@XZ`,
    `??0C@@QAE@HH@Z`; verify/v3g.cpp: `C::C(int)` with a scrambled list and `C::C(int, int)` with the list in class
    order are one IL class, and the listing calls `??0B`, stores the vptr, then `??0M1` twice: re-verified, C++ game
    flags.) `x(v)` in the initialiser list and `x = v;` as the first body statement differ only
    when a class member or the vptr-dependent call order lies between them; for trailing scalars they are
    the same stream position. **[I]**
11. **vptr stores come after all base constructors and before the first member.** With two polymorphic bases
    both base constructors are called first, then both vptrs are stored (`[this+0] = ??_7C@@6BA@@@`,
    `[this+8] = ??_7C@@6BB@@@`). **[C]** (ob_thunk `??0C`.) c2 may move the store past a later call's argument
    setup (listing of `??0C@@QAE@H@Z`: the store lands between `lea ecx,[esi+8]` and `call ??0M`).
    (Single-base case re-verified, C++ game flags: verify/v3g.cpp `??0C`; two polymorphic bases not re-verified.)
12. **A user-written destructor, even `~T() {}`, starts by storing the class's own vptr(s)**; an implicit
    destructor does not (it is only the member/base destruction). Then one `OPPUSHSTATE <- 0` per destructible
    member and base (bases first, then members in declaration order, so that unwinding is the reverse), the
    body, and at the exit label one `OPDTORACTION`: members in reverse declaration order, then bases in reverse
    order. **[C]** (ob_body `??1C`: listing calls `~M` on +16, `~M` on +8, `~B`; ob_misc `??1VD` (empty user
    dtor, vptr store) against `??1VE` (implicit, none); ob_thunk `??1C`: both vptrs; verify/v3g.cpp `??1C` (vptr store,
    body, `~M1` twice, `~B`), `td3`: an inlined `p->~VF()` with `~VF() {}` is the single store `mov [eax], OFFSET
    ??_7VF@@6B@`: re-verified, C++ game flags.)
13. **Virtual calls on `this` inside a constructor or destructor body are direct calls** (`f()` and
    `this->f()` both `OPCALL ?f@C@@UAEHXZ`). This holds only lexically inside the ctor/dtor: a member function
    called from the constructor and inlined there by c2 keeps its vtable calls (ob_misc `??0VD`: `f()` direct,
    the inlined `m()` four indirect calls). **[C]** (ob_body `??0C@@QAE@HH@Z`, `??1C`; verify/v3g.cpp `??0C`: `f()` is
    `call ?f@C@@UAEHXZ`, the inlined `callf()` is `call eax`; `??1C`: `this->f()` direct: re-verified, C++ game flags.)
14. **Implicit special members** (ob_body, ob_misc):
    - Nothing is emitted, and no call, for a trivial default constructor (`P p;`). `P p = P();` is a `memset`
      intrinsic into a temporary plus an `OPMBASSIGN` to `p` (at `/O2` two stores). **[C]** (i1, i2.) (not re-verified)
    - A class whose only non-trivial part is a member with a constructor/destructor (`Q { M m; int q; }`) gets
      implicit `??0Q`/`??1Q` as real COMDAT functions that are called (c2 then inlines them: `??1Q` is
      `jmp ??1M`; the unwind funclet still calls `??1Q`). Its copy constructor and assignment stay trivial:
      `Q q(*s)` and `*a = *b` are `OPMBASSIGN` (block copy), no call. **[C]** (i3, i4, i5.) (not re-verified)
    - A class with a vptr gets an implicit default constructor (vptr store only, called and inlined), an
      implicit copy constructor `??0T@@QAE@ABU0@@Z` (vptr store, then **member-wise** `OPASSIGN`s) and an
      implicit `??4T` (member-wise, vptr skipped, returns `this`); both are calls in the user (i8, i9, i10).
      With bases or class members that have their own, the implicit ones call `??4Base` / `??0Member(const&)`
      and copy the scalars one `OPASSIGN` each, in declaration order (`??4R`, `??0H@@QAE@ABU0@@Z`, `??4H`).
      **[C]** So `*a = *b` on a polymorphic class never becomes one block copy in the IL; a POD or a class
      with only non-trivial ctor/dtor members does.
    - Implicit and inline members reach c2 only if used in this translation unit, each as its own COMDAT.
15. **Deleting destructors.** `??_G` (scalar deleting destructor) is IL written by c1xx:
    `~T(this); if (flags & 1) operator delete(this); return this;`. It is emitted as a COMDAT wherever the
    class's vtable is emitted (virtual destructor) or where `delete p` / `p->~T()` on a non-virtual destructor
    is used. `??_E` (vector deleting destructor) is emitted as a body only in a unit that uses `new T[n]` /
    `delete[]` on the class: `if (flags & 2) { ??_M(this, size, this[-1], &dtor); if (flags & 1) delete
    (this-4); return this-4; } else <the ??_G body>`. Otherwise the vtable slot references `??_E` as an EXTRN
    (resolved to `??_G` through a weak alias). The vtable slot is always `??_E`. **[C]** (ob_new `??_GD`,
    `??_ED`, `??_EZ`; ob_thunk vtables and EXTRN list.) (not re-verified)

### Virtual calls, thunks, casts

16. **A virtual call is `load vptr; indirect OPCALL`** with `this` as the last-pushed argument:
    ```
    slot 0:  OPASSIGN t2 <- [_p+0]      ; OPARG t3 <- _p ; OPCALL t4 <- [t2+0] t3 args
    slot n:  OPADD t1 <- [_p+0] 4n      ; OPARG t2 <- _p ; OPCALL t3 <- [t1+0] t2
    ```
    Arguments are evaluated before the vptr load. `p->f()`, `(*p).f()`, `r.f()` and a call through a
    `C*` to a function first declared in the primary base are the same IL. **[C]** (ob_virt v1 == v3 == v9,
    v2 the same with `_r`; v8 == v11.) (not re-verified)
17. **Direct (non-virtual) call when**: the call is qualified (`p->A::f(x)`, v4); the object expression is a
    named object (`A o; o.f(x)`, v5); or rule 13. Going through a pointer or reference local to a known object
    (`A* p = &o; p->f()`, `A& r = o; r.f()`) is a virtual call in the IL (v6, v7); c2 does not devirtualise it.
    **[C]** (Re-verified, C++ game flags, verify/v3e.cpp in member functions: `M::dv1` is `call ?f@A@@UAEHH@Z`;
    `M::dv2` == `M::dv3` are `call DWORD PTR ??_7A@@6B@`: c2 forwards the vptr it has just stored, so the call reads
    the slot straight from the vtable's address, but it stays an indirect call, is not inlined, and counts as
    may-throw for the EH frame, verify/v5a.cpp `a37` against `a38`.)
18. **A virtual function introduced by a non-primary base is called through that base's vptr with the adjusted
    pointer**: `p->k()` (C : A, B; `k` from B) is `t = p+8; vptr = [p+8]; call [vptr+0](t)`, with no null
    check. The overrider `C::k` is compiled with `this` pointing at the B subobject: every member access in
    its body is written `(this - 8) + offset` (`OPSUB this 8; OPADD .. 16`), folded by c2 to `[ecx+8]`. No
    thunk exists for it. **[C]** (ob_virt v10, `?k@C@@UAEHXZ`.) (not re-verified)
19. **Adjustor thunks** exist for a function that overrides the same virtual in two bases (`C::g` over `A::g`
    and `B::g`) and for the deleting destructor: `?g@C@@W7AEHXZ` and `??_EC@@W7AEPAXI@Z` (`W7` = adjustor{8}).
    They are functions in c1xx's IL already written with physical registers
    (`OPSUB ecx <- ecx 8; OPGOTO target`), emitted as COMDATs `sub ecx, 8; jmp target` and marked
    `/Odtpy` in the listing. They are emitted in the unit that emits the vtable (the one that defines the
    constructor, or any that needs the vtable). **[C]** (ob_thunk.) (not re-verified) Other thunks, same form: the vcall thunk
    `??_9A@@$BA@AE` (`mov eax,[ecx]; jmp [eax+slot]`), created when the address of a virtual member function
    is taken (ob_virt v14, ob_pm m9), and the vtordisp thunk `?vf@V@@$4PPPPPPPM@A@AEHH@Z`
    (`sub ecx,[ecx-4]; jmp ?vf@V@@UAEHH@Z`) for a virtual function of a virtual base overridden in a class
    with a user constructor or destructor (ob_pm). **[C]** (not re-verified)
20. **Derived-to-base conversion to a base at a non-zero offset is null-checked, for references and `this`
    too.** `B* q = p;` (C : A, B) is
    ```
    OPCMP p 0 ; OPBRANCH(e) L1 ; t3 = p + 8 ; goto L2 ; L1: t3 = 0 ; L2:
    ```
    and the check survives `/O2` (`test eax,eax; je; add eax,8`), including for `B& k2(C& r) { return r; }`
    and for `B* q = this;` inside a member function (ob_virt k2, `C::nv`: `test esi,esi; je; lea edi,[esi+8]`).
    The down-cast `static_cast<C*>(b)` / `static_cast<C&>(b)` is the same with `-8`. Identical IL: implicit
    conversion, `static_cast<B*>(p)`, `(B*)p`, `&*p` (k1 == k6 == k7 == k8). `reinterpret_cast` and conversion
    to the primary base (offset 0) are no IL at all (k3 == k11 == k12). A non-polymorphic first base is laid
    out *after* a polymorphic second base, so `F : E, A` converts to `A*` for free (k11). Comparing `C*` with
    `B*` converts first, with the null check (k13). **[C]** (not re-verified)
    Not null-checked: member access through the derived pointer (`p->b` is `[p+12]`, k9) and the implicit
    `this` adjustment of a call (rule 18). So `p->b` and `B* q = p; q->b` differ in code (k9 against k10: the
    latter keeps the test and a dead null path). **[C]** (Re-verified, C++ game flags, with the `C*` read from a
    member inside a member function: verify/v3e.cpp `M::nb1` and `M::nb3` keep `test / je`, `M::nb2` has none.)
21. **Virtual bases.** Access to a virtual-base member or conversion to the virtual base reads the vbtable each
    time it is written: `t = [p+0] + 4; base = p + [t]` (`vbptr` at offset 0, entry 1), with a null check for
    the pointer conversion (`vb3`) and none for member access or calls (`vb1`, `vb4`: the sequence appears
    twice, once for the vptr load and once for `this`). The constructor takes a hidden last argument
    `$initVBases`; under `if ($initVBases)` it stores the vbptr (`??_8V@@7B@`) and constructs the virtual
    base, then stores the vptr through the vbtable and the vtordisp field (`[vbase-4] = vbdisp - static
    offset`). The destructor repeats the vptr and vtordisp stores. **[C]** (ob_pm vb1..vb4, `??0V`, `??1V`.) (not re-verified)

### Pointers to members

22. Representations and uses (ob_pm) **[C]** (not re-verified):

    | class of the pointer | data member pointer | function member pointer | call `(p->*m)(1)` |
    |---|---|---|---|
    | single inheritance | 4 bytes, the offset; `p->*m` is `[p + m]` | 4 bytes, the code address | `OPCALL _m` with `this = p` (m1) |
    | multiple inheritance | same as single (d2 == d1 shape) | 8 bytes {address, this-delta} | `this = p + m.delta; call m.addr` (m2) |
    | virtual inheritance | 8 bytes {offset, vbtable index}: `p + [[p+0] + idx] + off` unconditionally (d3) | 12 bytes {address, delta, vbtable index}: `this = p + [[p+0]+idx] + delta` (m3) | as left |
    | incomplete class (unknown) | 12 bytes; branches on the vbtable index `!= 0` (d7) | 16 bytes {address, delta, vbptr offset, index}; same branch (m4) | as left |

    - `&A::f` used directly in the call (`(p->*(&A::f))(1)`) is folded by c1xx to a direct call (m5).
    - A pointer to a virtual function is the address of the `??_9` vcall thunk (m9), so the call site is the
      same as for a non-virtual one.
    - `&B::g` converted to `int (C::*)(int)` stores delta 8 (m8); `&C::k` delta 0 (m7); the 8-byte value is
      returned in `eax:edx`.
    - Null tests: single-inheritance pointers compare one word (`m != 0`, d6, m12). For the 8-byte form
      c1xx writes the full comparison as branches (`addr != addr' → ne; addr == 0 → eq; delta != delta' → ne`);
      against the null constant c2 folds it to one compare of the address (m10), between two pointers it
      stays three compares (m11). Data member pointers of a single-inheritance class test against 0 in the IL
      as written (d6); the null value's encoding (-1) was not probed.
    - `r.*m` and `p->*m` are the same IL (m6 against m1).

### new and delete

23. **Class return values (the part c1xx decides).** `return d;` of a local is, in the IL, always a copy
    constructor call into `___$ReturnUdt` followed by `OPDTORACTION` for the local: c1xx does no NRVO. c2's
    `MscRVO` removes the copy when the function has a single `return d;` (table in rule 3A.8; ob_misc rv1: the `/O2` code constructs
    straight into the return slot, no EH frame, no destructor). With two different locals returned (rv2) the
    copies stay and a flag `$T` records "return value constructed" for the unwinder. `return D();` constructs
    in `___$ReturnUdt` directly in the IL (rv3). **[C]** (The by-value argument side, `OPMBARG` with a stack
    slot from `OPINTRINSIC`, is in section 3A; pv1/pv2 here show the shape.)
24. **`new T`** = `operator new(sizeof T)` then, in this order of cases **[C]** (ob_new):
    - no initialisation (`new P`, `new int`, `new P[3]`): the call's result is the value. No null check
      (n1, n3, n19).
    - value initialisation or an initialiser of a scalar/POD (`new P()`, `new int()`, `new int(v)`): a null
      check, then `memset` (intrinsic; two stores at `/O2`) or one store, with the result merged through a
      two-definition temporary `t = p` / `t = 0` (n2, n4, n5). So `new P` and `new P()` differ in code.
    - constructor: `sN = operator new(n); OPPUSHSTATE <- sN; if (sN) t = ctor(sN) else t = 0; sM = t;
      OPPOPSTATE; OPRET sM`. The constructor's return value (rule 10: `this`) is the value of the
      expression. The `OPPUSHSTATE` is the "delete the memory if the constructor throws" state: it gives the
      function an EH frame and a funclet `push $T; call operator delete` (n6, n7, n8, n9 identical in shape,
      with or without a destructor, virtual or not).
    - **a constructor declared `throw()`** has no `OPPUSHSTATE`: no EH frame, and the constructor call
      becomes a tail jump (n10). **Placement new** `new (m) K` calls `operator new(size, m)` (pushed after
      the size), keeps the null check, and has no state when no matching placement `operator delete` is
      declared (n17, n18).
    (Re-verified, C++ game flags, in member functions, verify/v3e.cpp: `M::nw1` `new P` no test; `nw2` `new P()` and
    `nw5` `new int(a)` test; `nw3` (`throw()` constructor) test, tail `jmp ??0KT`, no frame; `nw4`, `nw6` frame
    with a funclet calling `??3@YAXPAX@Z`. Placement new not re-verified.)
25. **`new T[n]`** **[C]**: the size is `n * sizeof T` through intrinsic 679, an overflow-checked multiply
    (`mul; seto cl; neg ecx; or ecx, eax`: size −1 on overflow); with a constant count it is folded
    (`new D[4]` → 20). A class with a destructor adds a 4-byte count cookie: checked add (intrinsic 677,
    `add ecx,4; setb; neg; or`), `[p] = n` after the null check, constructors through `??_L(p+4, size, n,
    &ctor, &dtor)`, value `p+4` (n14, n15). A destructor without a constructor still gets the cookie but no
    iterator call (n16). A constructor without a destructor: no cookie, `??_H(p, size, n, &ctor)`, plus the
    "delete on throw" state (n13). PODs: no check, no cookie (n11, n12).
    **Condition (verify/v3e.cpp `M::na1`, `M::na2`, `M::dl2`, `M::dl7`): which allocation function is called depends on
    the declarations in scope.** With `<new>` included (it declares `operator new[]` / `operator delete[]`),
    `new T[n]` calls `??_U@YAPAXI@Z` and `delete[] p` calls `??_V@YAXPAX@Z`; the probes of this section include no
    header and get `??2` / `??3` for both forms. A real translation unit that includes `<new>`, directly or through
    any standard header, shows `??_U` / `??_V`. **[C]**
26. **`delete p`** **[C]**:
    - no destructor (POD, scalar, class with only a constructor; also `delete[]` of those): `sN = p;
      operator delete(sN)`. **No null check.** `delete p` and `delete[] p` are identical (x1 == x2 == x3 ==
      x8). **Condition:** identical only while no `operator delete[]` is declared; with `<new>` included
      `delete[] p` calls `??_V` instead of `??3` (rule 25; verify/v3e.cpp `M::dl1` against `M::dl2`). No null check in
      either (re-verified, C++ game flags).
    - non-virtual destructor: `if (p) ??_GD(p, 1)`, a direct call of the scalar deleting destructor, which c2
      inlines: `test; je; call ??1D; push; call operator delete` (x4).
    - virtual destructor: `if (p) (*vptr[slot])(p, 1)` (x5: `push 1; call edx`; verify/v3e.cpp `M::dl6`: re-verified,
      C++ game flags). The slot is the destructor's
      position in the vtable (ob_thunk t1/t2/t3: `[vptr+8]`); for a pointer to a non-primary base the call
      goes through that base's vptr unadjusted, and the thunk `??_EC@@W7AEPAXI@Z` adjusts.
    - The result of the deleting destructor is assigned to a dead two-definition temporary (`t = call` /
      `t = 0`); it leaves no code.
    - `if (p) delete p;` has two null tests in the IL and compiles to exactly `delete p;` (x9 == x4 in code).
      The hand-written `if (p) { p->~D(); operator delete(p); }` also compiles to the same code (x14 == x4 in
      code, different IL). (Re-verified, C++ game flags, for `if (pd) delete pd;` == `delete pd;` on a member
      pointer: verify/v3e.cpp `M::dl3` == `M::dl4`.) **Condition (v3e `M::dl5`):** the hand-written form is the same
      code only when `p` is a local or a parameter. When the pointer is a member (`pd`), `delete pd` reads it once
      (`mov esi,[ecx+40]`), while the hand-written form reads it again after the destructor call (`mov
      eax,[esi+40] / push eax`), because the destructor may have changed it. **[C]**
27. **`delete[] p`** with a destructor **[C]**: non-virtual: `if (p) ??_ED(p, 3)` (x6, x13; verify/v3e.cpp `M::dl7`:
    test, `??_M`, then `??_V` because `<new>` is included, rule 25: re-verified, C++ game flags). Virtual: `if (p)
    { if (p[-1] != 0) (*vptr[slot])(p, 3); else operator delete(p - 4); }` (x7: the count cookie is tested
    before the virtual call).
28. **Explicit destructor calls**: `p->~D()` is `??_GD(p, 0)` (the scalar deleting destructor with flag 0),
    inlined at `/O2` to `jmp ??1D` (x10). `p->~W()` with a virtual destructor is the virtual call with
    argument 0 (x11). The qualified `p->W::~W()` is a direct call of `??1W` (x12). **[C]** (not re-verified)

### Spellings that are identical after the front end

| spellings | probe | identical in |
|---|---|---|
| `D d(a);` / `D d = a;` / `D d = D(a);` | ob_ctor c10 | IL |
| `p->f(x)` / `(*p).f(x)` / the same through a derived-class pointer (primary base) | ob_virt v1, v3, v9 | IL |
| `p->f()` / `r.f()` (pointer against reference parameter) | ob_virt v1, v2 | IL shape (names differ) |
| `f()` / `this->f()` / `(*this).f()` / `VD* p = this; p->f()` in a member function | ob_misc `VD::m` | IL shape; all virtual |
| `f()` / `this->f()` in a constructor or destructor | ob_body `??0C@@QAE@HH@Z` | IL; both direct |
| implicit `C*`→`B*` / `static_cast<B*>(p)` / `(B*)p` / `&*p` | ob_virt k1, k6, k7, k8 | IL |
| `C*`→primary base / `reinterpret_cast<B*>(p)` / polymorphic base behind a non-polymorphic one | ob_virt k3, k11, k12 | IL (no tuple) |
| `(p->*m)(1)` / `(r.*m)(1)` | ob_pm m1, m6 | IL shape |
| mem-initialiser list in any order | ob_body `??0C@@QAE@H@Z` | IL (class order always) |
| `delete p` / `delete[] p` for a type without destructor | ob_new x1, x2, x3, x8 | IL |
| `delete p` / `if (p) delete p` / `if (p) { p->~D(); operator delete(p); }` (non-virtual dtor) | ob_new x4, x9, x14 | code only (IL differs) |
| any of these at `/Od` against `/O2` | ob_new, ob_ctor, ob_virt, ob_misc | IL |

Spellings that look equivalent and are **not**: `new P` / `new P()` (rule 24); `p->b` / `B* q = p; q->b`
(rule 20); early returns / one exit, when destructible locals are live (rule 3); `~T() {}` / no destructor
declared, in a polymorphic class (rule 12: vptr store); `o.f()` / `(&o)->f()` through a named pointer (rule
17); `p->~W()` / `p->W::~W()` (rule 28); returning one named local / two (rule 23).

### Open questions

- `OPPUSHSTATE`/`OPDTORACTION`/`OPPOPSTATE` carry their EH record at tuple +0x30 (il_tuple.md §6.3); the `il`
  dump now prints it as `eh{..}` (tracer.md, `il`). **[C]** An `OPDTORACTION` says how many states it unwinds
  (`eh{n=N ..}`): N is the number of destructible objects alive at that exit (eh_a `a1` 1, `a2` 2, `a3` inner
  block 1 and return 1). The "stray" `OPPOPSTATE` after the last `OPRET` is the textual end of the scope: it has
  the same n and flags 0xa1, and appears exactly when the scope's end is not reached by falling through (its
  last statement is a `return`/`break`/`goto`, whose own `OPDTORACTION` has flags 0x01); a scope that ends by
  falling through has one `OPDTORACTION` with flags 0x21 and no `OPPOPSTATE` (`a3`, eh_states `s1`). The
  `OPPUSHSTATE` flags distinguish what was pushed: 1 a named object, 3 a temporary (`a6`; its action has 0x23),
  9 a by-value class parameter (`a9`), 0x101 a catch handler's state (`a4`, `a5`). The flag values are **[C]**
  as observed; that they are bit sets with these meanings is **[I]**. The conditional-construction flag
  (rule 6) is added after MscRVO; which pass adds it was not traced.
- Rule 10's last sentence (initialiser list against first body statement for scalars) is inferred, not probed
  as a pair.
- The null encoding of data member pointers and comparisons of 12/16-byte member pointers were not probed. The
  `/O2` listing of the unknown-inheritance call (m4) tests `ecx` on entry without loading it, so the IL (d7,
  m4) is the only evidence for that row of the table.
- Order in which thunks, `??_G`/`??_E` and implicit members land in the object relative to user functions
  (they follow c2's callee-first compile order; in ob_thunk the two adjustor thunks come after `t3` and before
  `??0C`): left to section 4.
- `??_E` as a weak alias of `??_G` in units without array new is inferred from the EXTRN and the missing body;
  the object's symbol table was not dumped.
- Classes with both a virtual base and non-virtual polymorphic bases, `dynamic_cast`, and covariant-return
  thunks were not probed.

---

## 4. Functions (rules cited as 4.n): calls, inlining boundary, conventions, emission order

Probes: `work/scratch/feil/fn_inl.cpp` (inline kinds, intrinsics), `fn_cc.cpp` (conventions, default arguments),
`fn_ord.cpp`, `fn_ord2.cpp`, `fn_ord3.cpp`, `fn_ord4.cpp` (emission order), `fn_lk_a.cpp` + `fn_lk_b.cpp` (linked
order). Tools: `fe.py il|cmp` (IL at `@MscRVO@4`), `fn_obj.py FILE [-- flags]` (prints the order of `@MscRVO@4`
events, of `@EncEmitFunc@4` events, of the listing's PROCs and data labels, and the `.obj` section table with each
section's COMDAT selection and first symbols). Flags are the game's (`/TP /MT /GS /EHsc /Gy /O2`) unless stated.

### What reaches c2 as a call

1. **Every call to a function with a body is an `OPARG`.. `OPCALL` in the IL, whatever the keyword.** `inline`,
   `__inline`, `__forceinline`, `static`, in-class member, out-of-class `inline` member, static member, template
   instantiation, an empty constructor `S() {}` and an empty destructor all arrive as calls to the decorated name;
   the keyword is a flag on the callee's symbol, not something in the caller's tuples. c1xx substitutes no body.
   **[C: fn_inl `c_plain`, `c_under`, `c_force`, `c_static`, `c_tmpl`, `c_member`, `c_ctor`, `c_ctor0`; verify/v4c.cpp
   `M::c1`..`M::c5`: five member functions calling an `inline`, `__forceinline`, `static`, template and in-class
   member version of one body are five IL classes that differ only in the call target (and the `this` argument),
   and one code class (re-verified, C++ game flags)]**
   So for the caller's IL these spellings are interchangeable; they differ only in what c2's inliner then decides
   (inline.md rules 1-9) and in linkage (rule 14).
2. **What c1xx does expand itself** (no `OPCALL` exists, so no inlining decision either):
   - trivial special members. A trivial copy is one `OPMBASSIGN dst:60NN <- src:60NN` (N = size), a trivial
     default constructor and a trivial destructor are nothing. **[C: fn_inl `c_triv`]** (not re-verified) Non-trivial implicit
     members are real functions that c1xx generates and calls (rule 12).
   - `__assume(e)` is one `OPASSUME` tuple, no compare. **[C: `c_assume`]** (not re-verified)
   - `strlen("literal")` is folded to the constant (`OPRET <- 5`). **[C: `c_strlenk`, /O2 only; verify/v4c.cpp `M::sl1`
     with `<string.h>` (re-verified, C++ game flags)]**
3. **Intrinsics (`/Oi`, which `/O2` implies) are not calls.** By `@MscRVO@4`:

   | source | IL at MscRVO | probe |
   |---|---|---|
   | `memcpy(d, s, 16)` (constant size) | `OPMBASSIGN [d]:6010 <- [s]:6010`, the same tuple as a 16-byte struct copy | `c_memcpy` |
   | `memcpy(d, s, n)`, `memset(d, 0, 16)` | `OPARG` x3 (right to left) + `OPINTRINSIC` | `c_memcpyn`, `c_memset` |
   | `strlen(s)`, `strcmp(a, b)`, `abs`, `fabs`, `sqrt`, `sin`, `_rotl` | `OPARG`s + `OPINTRINSIC` with a result | `c_strlen`.. `c_rotl` |
   | `strcpy(d, s)` | already an open-coded byte loop (`t4 = [t1]; [t2] = t4; t1++; t2++; if (t4 != 0) goto`) | `c_strcpy` |

   With `/Od`, or `/O2 /Oi-`, every one of these is an ordinary `OPCALL {_memcpy}`, `{_strlen}`, `{_abs}`...
   **[C: fn_inl at both flag sets; verify/v4c.cpp `M::mc1` (`OPMBASSIGN :6010`), `M::mc3` (`OPINTRINSIC`), `M::sl2`,
   and `M::mc1` == `M::mc2` (struct assignment) in code: re-verified, C++ game flags, `/O2` only]** A constant-size `memcpy` and a struct assignment of the same size are
   therefore the same IL at `/O2` **[C for the tuple; the struct side is `c_triv`]**. Whether the expansion is done
   by c1xx or by c2's reader is not separable with this dump **[I]**; it does not matter for prediction.
4. **Unreferenced inline functions never reach c2.** An `inline` function, a `static` function, an in-class member or
   a template that nothing references has no `@MscRVO@4` event and no section in the object (`i_unused`,
   `s_unused`, `S::unused_member`, `i_tmpl_unused`). **[C: fn_inl; verify/v4b.cpp: the inline virtual `Vx::v` of a class
   whose constructor is external, called only through a pointer, is not emitted either, since nothing in the unit
   needs the vtable (re-verified, C++ game flags)]** A referenced one is always compiled and
   emitted as its own COMDAT, even when every call to it was inlined (`i_plain`, `i_force`, the static
   `i_static`). **[C: fn_inl listing; verify/v4a.cpp `il_early`, `il_late`, `s1`, `Vc::nv`, `tmin<int>` are all
   emitted although every call was inlined (re-verified, C++ game flags)]** In the linked image such a COMDAT is
   then removed if nothing references it (rule 15). Taking the address counts as a reference (`c_addr`, fn_ord3 `addr_user`).
5. **Why a body shows up at `@MscRVO@4` several times.** c2 reads a function once to compile it, and reads it again
   from the IL file for every call site at which the inliner expands it (the events directly after the caller's:
   `c_member` is followed by `S::get`, `S::outl`, `S::st`; fn_ord `u3` by `Imp::Imp`, the copy constructor,
   `operator=`, `~Imp`, `~Imp`: one per call, in the order the inliner takes them). Only the first event, in compile
   order, produces code (`@EncEmitFunc@4` fires once per function). The re-read is not limited to `inline`
   functions: `u5` is followed by the plain external `u6`. **[C: fn_obj.py MSCRVO vs ENCODE lines on fn_inl,
   fn_ord]** (not re-verified) When using `il --func`, take the first dump of a callee.
6. **The front-end IL does not depend on the optimisation level.** `/Od` and `/O2` give tuple-for-tuple identical
   IL at `@MscRVO@4` for every function of fn_cc.cpp (28), eh_a.cpp (9) and eh_states.cpp (14), except: the
   intrinsics of rule 3, the `strlen` fold of rule 2, and string literals, whose symbol is `??_C@...` under `/GF`
   (implied by `/O2`) and `$SGnnnn` otherwise. **[C]** What does change with the level is which functions c1xx
   writes and in what order (rules 11-13).

### Default arguments

7. **A default argument is re-evaluated at each call site as if it had been written there.** `k_def(a)`,
   `k_def(a, 7)` and `k_def(a, 7, g1 + 1, "dflt")` are identical IL (the constant, an `OPADD` on the global, the
   pooled string's address); `m->dm()` equals `m->dm(3)`. A default of class type (`const Q& q = Q(5)`) builds the
   temporary in the caller exactly like the spelled-out `Q(5)`: constructor call, `OPPUSHSTATE`, the call,
   `OPDTORACTION` (and so gives the caller an EH frame, section 5). **[C: fn_cc `c_def1` == `c_def2` == `c_def3`,
   `c_dm1` == `c_dm2`, `c_defc1` == `c_defc2`; verify/v4c.cpp `M::d1` == `M::d2` == `M::d3`, `M::e1` == `M::e2`, `M::q1` ==
   `M::q2` in IL and in code, in member functions, the last pair with an EH frame (re-verified, C++ game flags)]**
   Omitting an argument is never a lever.

### Calling conventions in the IL

8. **The IL is convention-free.** `__cdecl`, `__stdcall`, `__fastcall` and thiscall calls have the same shape:
   one `OPARG` per argument, emitted **last argument first**, then `OPCALL result <- target, args in declaration
   order`. The convention is carried by the callee symbol (its decorated name: `YA`/`YG`/`YI`, `QAE`/`QAG`/`QAA`/
   `QAI`) and applied by c2: register assignment for `__fastcall` (ecx, edx), `this` in ecx, who pops. The body of
   a function likewise contains nothing convention-specific: a thiscall and a `__stdcall` member with the same
   body are identical IL. **[C: fn_cc `c_cdecl`, `c_std`, `c_fast`, `c_m`, `c_ms`, `c_mc`, `c_mf` differ only in the
   target name; `M::m` == `M::ms`; verify/v4c.cpp `M::v1`, `M::v2`, `M::v3` (call side, in member functions: re-verified,
   C++ game flags; the definition side not re-verified)]**
9. **Operand order of the call tuple**: `this` first (its `OPARG` is emitted last), then the hidden
   struct-return pointer, then the declared arguments. For a free function the hidden pointer is the first
   operand. The return buffer is a compiler temporary of the caller (`s3`), and the call's result is that
   pointer. **[C: `c_sret`, `c_sretf`, `c_mret`]** (not re-verified) A static member call has no `this` operand (`c_sm`). An indirect
   call has the pointer variable as target operand (`OPCALL <- _f, args`; `c_fp`), a virtual call the loaded slot
   `[t+off]` (t1.cpp `e3`).
10. **Evaluation follows the OPARG order: right to left**, including side effects inside the list:
    `k_cdecl(h(a), h(b), g1 = b)` stores `g1`, then calls `h(b)`, then `h(a)`. **[C: `c_order`; verify/v4c.cpp `M::o1`
    (re-verified, C++ game flags)]** Varargs calls apply
    the default promotions as explicit tuples before the `OPARG` (`(char)a` gets `OPCONVERT :1004 <- :1001`,
    `(float)a` gets `OPCONVERT :5008 <- :5004`), and `...` is otherwise an ordinary call; a by-value struct
    argument is one `OPMBARG :60NN`. **[C: `c_var`, `c_byval`]** (not re-verified)

### Emission order: what decides the order of functions in the object and the binary

The chain is: c1xx writes function bodies in some order (A) → c2 compiles them in a derived order (B) → every
compiled function becomes the next section(s) of the `.obj`, so the listing, the `@EncEmitFunc@4` order and the
COFF section order are the same order **[C: fn_obj.py prints all three; fn_ord, fn_ord2, fn_inl]** → the linker
concatenates by section name (C).

11. **(A) c1xx's order with inlining enabled (`/O1`, `/O2`, `/Ox`; also `/O2 /Ob0`).**
    - A function is written at the position of its **definition** if it is an ordinary function, a `static`
      function, or a non-virtual inline function (in-class members at their class, in declaration order; an
      out-of-class `inline` definition where it stands, even when its first use is earlier).
    - **Implicitly generated members** (default/copy constructor, `operator=`, destructor, the scalar/vector
      deleting destructors `??_G`/`??_E`) and **virtual inline members** (reached through the vtable) are written
      directly after the function whose body first needed them. A user-written in-class constructor counts as such
      a body at its own position, so the virtuals of a class with an in-class constructor come right after that
      constructor.
    - **Template instantiations** come after all non-template code, in order of first use.
    - Last: the dynamic initialisers `??__E<var>` in order of the variables' definitions, then the atexit
      destructor stubs `??__F<var>` (static locals' stubs interleaved in definition order).
    - Unreferenced inline/static/template functions are skipped (rule 4).

    **[C: fn_ord2 /O2: `K::K, K::m1, K::m2, ia1, ia2, ib1, leaf0, leaf1, {Imp::Imp, ~Imp, Imp(const Imp&)}, leaf2,
    leaf3, late, uD, K::m3, leaf4, uE, tmpl<int>` then the deferred callers; fn_ord4: `V1::nv, V1::V1, V1::v`
    at the class but `V2::V2, V2::v` (implicit constructor) after `leaf0`, i.e. at `useV2`; fn_ord: `tmpl<int>`,
    `tmpl<char>` after `u8`, then `$E`/`$F`]** The order of implicit members within one group was constructor,
    destructor, copy constructor, `operator=` in both probes, not the order of use **[C for these; I as a rule]**.
    **Re-verified by prediction (C++ game flags): for verify/v4a.cpp (28 functions: in-class constructor with two
    virtuals, implicit constructor with a virtual, implicit members, two templates, a late `inline`, a `static`,
    a caller-before-callee pair, one global with a destructor) the order was written down from rules 11-13 before
    compiling, and 26 of 28 positions were right; the two wrong ones are the `??__E`/`??__F` pair (rule 13,
    corrected there).** Observed `/O2` order: `Vc::Vc, Vc::v, Vc::w, Vc::nv, Vi::nv, il_early, f1, Imp::Imp,
    Imp::~Imp, f4, Vi::Vi, Vi::v, il_late, f9, s1, f10, f11, f12, tmin<int>, tmax<int>`, then the deferred `f2, f3,
    f5, f6, f7, f8`, then `??__Egt, ??__Fgt`. `/O1` and `/O2 /Ob0` give the same order. **[C]**
12. **(A') c1xx's order at `/Od`** (no inlining): ordinary external functions at their definition; **inline,
    static and implicit functions are written after the function that first references them**, the group in
    definition order (unless the definition is still to come: then at the definition); templates and `$E`/`$F` at
    the end as above. **[C: fn_ord2 /Od: `leaf0, uA, ia1, ia2, leaf1, uB, Imp.., leaf2, uC, K::K, K::m1, K::m2, leaf3,
    late, uD, ib1, leaf4, uE, K::m3, tmpl<int>`; fn_ord3 /Od: the static `s0` after `user`]** At `/Od` c2 compiles in
    exactly this order. (Re-verified, C++ game flags + `/Od`, by prediction on verify/v4a.cpp, 28 of 28 positions:
    `f1, il_early, f2, f3, Imp::Imp, Imp::~Imp, f4, f5, Vi::nv, Vi::Vi, Vi::v, f6, f7, il_late, f8, f9, f10, s1, f11,
    Vc::Vc, Vc::v, Vc::w, Vc::nv, f12, tmin<int>, tmax<int>, ??__Egt, ??__Fgt`. Within a group the explicitly
    written inline member comes before the implicit constructor and its virtual: `Vi::nv, Vi::Vi, Vi::v`.)
13. **(B) c2's compile order under `/O1`, `/O2`, `/Ox`: callee-first sweeps over (A)** (il.md §2.2, inline.md
    rule 1). Repeatedly walk the list (A) in order; a function is compiled when every function it references
    that has a body in this file has already been compiled; otherwise it waits for the next sweep. Functions that
    call each other in a cycle do not wait for each other and come out in (A) order; self-recursion does not
    delay. **[C: fn_ord3 /O2: `i0, K::m1, K::m0, i1, s0, addr_user, rec_b, rec_a, K::m2, self, chain3, far, tail`
    (sweep 1), `user, chain2` (sweep 2), `chain1` (sweep 3); fn_ord2: `uA, uB, uC` after everything else because
    `late`, the `Imp` members and `tmpl<int>` come later in (A)]**

    Worked prediction: with definitions in the order `caller(); callee();` the object has `callee` first; with
    `callee(); caller();` the order is as written. A caller of an inline function defined above it, or of a class's
    non-virtual in-class members, keeps its place (the callees precede it in (A)), whereas a function that is the
    first to use an implicit constructor/destructor, a virtual inline member, or any template is pushed behind
    **all** functions that are not themselves deferred. The dynamic initialisers reference their `$F` stubs and are
    still last.

    **Corrections and additions from verify/v4a.cpp, v4b.cpp, v4d.cpp (order predicted first, then compiled):**
    - **`??__E` initialisers and `??__F` stubs are outside the sweeps: they are always the last functions of the
      object, all `??__E` first, then all `??__F`, however many sweeps the rest needs** (v4b has four sweeps and
      still ends `??__Ega, ??__Egb, ??__Fga, ??__Fst, ??__Fgb`). The earlier wording ("reference their `$F` stubs
      and are still last") suggested they take part; reading the rule that way predicts `??__F` before the deferred
      functions, which is wrong. il_pipeline.md §4.2 has the mechanism: `CgBottomUpOrder` appends functions flagged
      as dynamic initialisers to a separate list that is concatenated after the others. **[C: order; I: that the
      `??__F` stubs carry the same flag]**
    - **A reference by address counts as a call edge and delays**: `int (*e4())(int) { return adr; }` with `adr`
      defined later is compiled one sweep later, after `adr`. **[C: v4b]**
    - **A function with a static local that has a destructor is deferred by one sweep**, because it takes the
      address of its `??__F` stub, which sits at the end of the list (v4b `e5`, v4d `p1`, `p2`: emitted after the
      functions defined below them). **[C]**
    - **Cycles: "come out in (A) order" holds for a two-function cycle only.** For `c1 → c2 → c3 → c1` (defined in
      that order, nothing else deferred) the order was `c2` in the first sweep and `c1, c3` in the second. This is
      what il_pipeline.md's algorithm gives if the edge ignored as "recursive" is the one that closes the cycle
      when the walk starts from the **last**-defined member (`c3 → c1 → c2`, back edge `c2 → c3`): `c2` then has no
      unplaced callee, `c1` waits for `c2`, `c3` for `c1`. The same reading gives (A) order for a two-cycle.
      **[C: v4b order; I: the mechanism, `CgIdentifyRecursion` was not traced]** Self-recursion does not delay
      (v4b `e2`). A chain `e1 → g1 → g2 → tsq<int>` takes one sweep per link when each callee is defined after its
      caller (`g2`, `g1`, `e1` in sweeps 2, 3, 4). **[C]**
    - In-class members that call each other: `k1() { return k2() + 1; }` declared before `k2` is compiled after
      it (`k2` sweep 1, `k1` sweep 2), so "non-virtual in-class members at their class" is the (A) position only.
      **[C: v4b]**
    Observed v4b order: `K::k2, e2, c2, K::k3, Vo::Vo, Vo::v, Vo::w, adr, e6, tsq<int>` | `K::k1, c1, c3, e3, K::k4,
    e4, g2, e5` | `g1` | `e1` | `??__Ega, ??__Egb, ??__Fga, ??__Fst, ??__Fgb`.
14. **Sections.** `/O2` implies `/Gy`: adding `/Gy` changes nothing, and only `/O2 /Gy-` (or `/Od` without `/Gy`)
    puts the ordinary functions into one shared `.text` section, created at the first of them; inline, template
    and implicit functions are COMDATs regardless. **[C: fn_ord, section tables at `/O2`, `/O2 /Gy`, `/O2 /Gy-`,
    `/Od`]** Per function, in order, the object gets:
    - `.text` (COMDAT selection 1 "no duplicates" for external functions, 2 "any" for inline/template/implicit
      ones and for `$E`/`$F`);
    - immediately after it, the read-only data it was the first to reference: pooled strings `??_C@` and
      float/double constants `__real@` (`.rdata`, COMDAT any), vtables `??_7` (`.rdata`, selection 6 "largest")
      with their RTTI `??_R4`/`??_R3`/`??_R2`/`??_R1` (`.rdata$r`) and `??_R0` (`.data`) after the first
      constructor that stores the vtable, static locals and their guard `?$S1@` (`.bss`);
    - for an EH function `.text$x` (unwind funclets, catch-less handler `__ehhandler$`; selection 5
      "associative" to the function) and `.xdata$x` (`__unwindtable$`, `__tryblocktable$`, `__ehfuncinfo$`).
    - `??__E` initialisers are in `.text$yc`, `??__F` stubs in `.text$yd`, and the initialiser pointers in
      `.CRT$XCU` at the end.

    At `/Od` string literals are `$SG` labels in one non-COMDAT `.rdata` section at the top of the object.
    **[C: fn_ord section tables]** In the `/FAsc` listing a function's data (constants, EH tables) is printed
    before its PROC. (Re-verified, C++ game flags, verify/v4a.cpp section table: `??0Vc` `.text` selection 2, then
    `??_7Vc` `.rdata` selection 6 and its RTTI, then `Vc::v`...; external functions selection 1, the internal-linkage
    `s1` also selection 1; `f3`'s `.text$x` and `.xdata$x` selection 5 directly after it; `.text$yc`, `.text$yd`,
    `.CRT$XCU` last.)
15. **(C) In the linked image**, `.text` contributions come in object order on the link line, each object's
    functions in order (B); a COMDAT that several objects define is kept at its position in the **first** object
    that has it and dropped from the others; then all objects' `.text$x` (funclets and `__ehhandler$`), then all
    `.text$yc` (`??__E`), then all `.text$yd` (`??__F`), each group again in object order; library code follows the
    objects within each group. **[C: fn_lk_a.cpp + fn_lk_b.cpp, `/O2 /Ob0`, link map: `shared_inl, K::K, K::v,
    a_late, a1, a2` (a.obj), `W::W, W::~W, ext, b0, b1, main` (b.obj; its `shared_inl`, `K::K`, `K::v` dropped),
    LIBCMT, then `__unwindfunclet$a1$0, __ehhandler$a1, __unwindfunclet$b1$0, __ehhandler$b1`, then `??__Ega,
    ??__Egb`, then `??__Fga, ??__Fgb`]** So in Fallout3.exe a run of small funclets far from their functions is the
    `.text$x` block, and a run of `??__E` bodies is `.text$yc`; neither says anything about source order. The map
    was made without `/OPT:REF`/`/OPT:ICF` changes to the default; `/OPT:ICF` folding and `/ORDER` are not covered.
    **Re-verified (C++ game flags, default `/O2` with inlining on): verify/lk/la.cpp + lb.cpp**, map: `Q::v, sh1, a3,
    a1, a2` (la.obj, its compile order), `W::W, W::~W, ext, b1, main` (lb.obj; its `Q::v` and `sh1` dropped), LIBCMT,
    then `.text$x`: `__ehhandler$a3, __unwindfunclet$a1$0, __ehhandler$a1, __unwindfunclet$b1$0, __ehhandler$b1`,
    then `.text$yc`: `??__Ega, ??__Egb`, then `.text$yd`: `??__Fga, ??__Fgb`. **Addition:** the default link (no
    `/DEBUG`, so `/OPT:REF` is on) removes every COMDAT nothing references: `Q::Q`, `tsq<int>`, `sh2` and `b0`
    (external, called only from `main`, inlined there) are in the objects and absent from the image. With `/Gy`
    every function is a COMDAT, so **a function whose every call was inlined, and whose address is not taken, is
    not in the binary at all** if the game was linked with `/OPT:REF`. **[C: la/lb map]**
    **Fallout3.exe has exactly this layout [C: `verify/f3x.py stats`, `verify/f3y.py`]:** `.text` code from
    0x00401000; one contiguous block of unwind funclets and `__ehhandler$` thunks from 0x00c24b80 to about
    0x00c8289f (12,314 funclets and 6,339 handler thunks found through the `FuncInfo` of 8,042 EH prologs; every
    handler ends in a `jmp` to the same `___CxxFrameHandler3` at 0x00bffe4e); from 0x00c828a0 a run of `??__E`
    dynamic initialisers (3,156 of them have an EH frame of their own; 0x00c828a0 is `ctor(&obj); ...;
    atexit(0x00d86640)`), whose handlers and funclets lie inside the block in the same order as the initialisers
    (3,155 of 3,155 consecutive pairs ascending); and the `??__F` stubs at the end of `.text` (0x00d86640 is one).
    No EH function lies inside the funclet block.

**Reading a binary's function order with these rules.** Within one translation unit's stretch of `.text`:
a function that sits *before* a function defined earlier in the source is (i) called by it (callee-first), or
(ii) the other was deferred for another callee. An inline member that appears between two ordinary functions
of another file's class is a COMDAT that this object happened to be the first to define. A callee placed directly
before its only caller, out of source order, is the ordinary result of rule 13 and not evidence of a `static`
or of a different file.

### Spellings that are identical after the front end

| A | B | probe |
|---|---|---|
| call to an `inline` function | call to `__inline` / `__forceinline` / `static` / template / in-class member (same body) | fn_inl: same caller IL; the callee symbol's flags differ |
| `f(a)` relying on defaults | `f(a, 7, g1 + 1, "dflt")` spelled out | fn_cc `c_def1/2/3` |
| `m->dm()` | `m->dm(3)` | fn_cc `c_dm1/2` |
| `k(a)` with default `const Q& = Q(5)` | `k(a, Q(5))` | fn_cc `c_defc1/2` |
| `__cdecl` / `__stdcall` / `__fastcall` call or definition | each other, apart from the symbol name | fn_cc `c_cdecl/c_std/c_fast`, `M::m` == `M::ms` |
| `memcpy(d, s, 16)` at `/O2` | a 16-byte struct assignment | fn_inl `c_memcpy`, `c_triv` |
| any function at `/Od` | the same at `/O2` (IL at MscRVO; rule 6 exceptions) | fn_cc, eh_a, eh_states |
| `/O2` | `/O2 /Gy` | fn_ord section tables |

Not identical: definition order of functions (changes object order, rules 11-13); an in-class constructor versus an
implicit one (moves the class's virtuals and the constructor, rule 11); `/Oi` on or off (rule 3).

### Open questions

- Rule 13's tie-breaking for a three-cycle and for a function referenced only by address is now probed (v4b);
  which edge of a longer or overlapping cycle is ignored is inferred from one case.
- Whether c1xx or c2's reader expands `memcpy`/`strcpy` (rule 3). c1xx.dll was not read for this section.
- Order of the implicit members inside one group (rule 11) for classes that need only some of them.
- `/OPT:ICF`, `/ORDER`, and incremental-link padding in rule 15. LTCG is out of scope (Fallout 3 has none).
- Explicit template instantiation, `extern template`, member templates, and inline functions referenced only from
  another inline function that is itself unreferenced were not probed.
- `fe.py il` shows the call target only when the `@TupMakeCall@28` hook saw it; a target taken as an address
  (`OPASSIGN _p <- s0`) prints as `s0`, and at `/Od` one intrinsic call (`c_rotl`) printed a stale name.

---

## 5. Exception handling as c2 sees it, /EHsc (rules cited as 5.n)

Probes: `work/scratch/feil/eh_a.cpp` (basic shapes), `eh_frame.cpp` (what gets a frame), `eh_states.cpp` (state
numbering, constructors/destructors, try/catch), `eh_more.cpp` (exception specifications, new/delete, catch forms,
static locals). Tools: `fe.py il` (IL at `@MscRVO@4`), `FE_AT=@Inline@4 uv run work/scratch/feil/eh_fe.py il ...`
(the same dump after c2's EH canonicalisation: `preprocess_EH`/`CxxEH_canons` run between `MscRVO` and `Inline`,
il_pipeline.md §5.3), `eh_chk.py FILE [-- flags]` (per function: frame or not, `maxState` from `__ehfuncinfo$`,
and the state values stored into `__$EHRec$` in listing order; `eh_chk.py FILE dump` prints the whole listing).
Flags are the game's (`/TP /MT /GS /EHsc /Gy /O2`) unless stated.

### What c1xx emits

c1xx does **not** emit destructor calls for scope exits, state numbers, or unwind funclets. It brackets object
lifetimes with marker tuples and c2 builds everything else from them.

1. **An object with a non-trivial destructor: `OPPUSHSTATE` right after its constructor call**, with the
   constructor's result (the object's address) as source:
   ```
   OPARG t1 <- _d ; OPCALL t2 <- {??0D@@QAE@XZ} t1 ; OPPUSHSTATE <- t2
   ```
   One per object, in construction order. A class with a constructor but a trivial destructor gets none and is
   plain calls. **[C: eh_a `a1`, `a2`, `a7`]** (IL not re-verified; the resulting frames and states are, rules 11-15)
2. **Each exit from the scope gets one `OPDTORACTION`**, which stands for "destroy everything pushed in the
   scopes being left", however many objects that is (`a2`: two `OPPUSHSTATE`, one `OPDTORACTION`). Every exit path
   has its own: the fall-off end, each `return`, each `break`, each `continue` (eh_states `s9`: three in one loop
   body; `s11`: two returns, two actions). The textual end of the scope carries an `OPPOPSTATE`, after the
   `OPRET`/`OPGOTO` when the scope ends in a `return`. **[C]** After canonicalisation each `OPDTORACTION` has become
   `OPNEWSTATE` + the destructor call(s) in reverse construction order + `OPDTOREND`, in line at that exit
   (`eh_fe.py` on `a1`, `a3`). So **destructor calls are duplicated per exit path in the IL**; any sharing in the
   listing is c2's tail merging, and reordering returns or turning `break` into a flag changes how many copies
   exist. **How much c2 shares (verify/v3f.cpp, table in rule 3B.3):** exits that return a variable, a call result or
   nothing are merged into one destructor sequence; exits that return different constants, or leave different
   objects alive, keep one sequence each. **[C]**
3. **`return expr;` with live destructible objects: the value goes to a compiler temporary first**:
   `OPASSIGN sN <- value; OPDTORACTION; OPRET <- sN`. Each `return` has its own temporary (`s11`: `s3`, `s4`) and
   its own `OPRET`. Without live objects the `OPRET` takes the expression directly. **[C: `a1`, `s11`, `a7`]** (IL not re-verified; the resulting frames and states are, rules 11-15) This
   is the `mov esi, eax` .. `mov eax, esi` around the destructor call in the listing.
4. **Temporaries** use the same pair, with the temporary's address as the `OPPUSHSTATE`'s *destination* operand
   (`OPPUSHSTATE t4 <- t3`, later uses go through `t4`), and the `OPDTORACTION` at the end of the full expression;
   there is no `OPPOPSTATE`. **[C: `a6`, `s2`]** (not re-verified) A temporary created in only one arm of `?:`/`&&`/`||` is still
   pushed inside the arm and destroyed after the join; c2's canonicalisation adds a flag word (`sN = 0` at entry,
   `sN |= 1` after the constructor, `if (sN & 1) { sN &= ~1; dtor }` at the action; the unwind funclet tests the
   same bit). **[C: `s7` at MscRVO and at Inline; listing]** (not re-verified)
5. **By-value class parameters are destroyed by the callee**: the function starts with `OPPUSHSTATE <- 0` (one per
   such parameter) and every exit has an `OPDTORACTION`. **[C: `a9`]** (IL not re-verified; the resulting frames and states are, rules 11-15) The caller copy-constructs into the
   argument area and emits nothing afterwards.
6. **Constructors**: one `OPPUSHSTATE` after each base and each member constructor call, bases first (in
   declaration order), then members in declaration order regardless of the mem-initialiser order, then the body,
   then one `OPPOPSTATE` before `OPRET <- this`. **Destructors**: one `OPPUSHSTATE <- 0` per destructible
   base/member at entry, the body, then one `OPDTORACTION`, which canonicalisation expands to the member and base
   destructor calls in reverse order, each preceded by `OPNEWSTATE`. A non-first base gets the adjusted pointer
   with a null test (`this ? this+4 : 0`) even inside the destructor. **[C: eh_states `C::C()`, `C::C(int)`,
   `C::~C`, at both passes; eh_init.cpp: the two mem-initialiser orders give identical IL apart from the
   constructor's own name]**
7. **`new T(args)`** with a constructor that may throw: `s = operator new(size); OPPUSHSTATE <- s; if (s) ctor(s,
   args); ...; OPPOPSTATE` — the state's unwind action is `operator delete(s)`. When T's constructor is declared
   `throw()` (or T is a POD) c1xx emits **no** `OPPUSHSTATE` at all. This is the one place where c1xx itself uses
   an exception specification. **[C: eh_frame `f14`, eh_more `m09` (ctor `throw()`), `m08` (POD), `m10` (array)]**
   Arrays of destructible objects are constructed by `??_L` (vector constructor iterator, with the element
   constructor and destructor addresses as arguments) followed by `OPPUSHSTATE <- 0`. **[C: `f17`, `m10`]** (IL not re-verified; the resulting frames and states are, rules 11-15)
8. **A static local with a destructor**: `if (!(guard & 1)) { guard |= 1; OPPUSHSTATE <- guard; ctor(&obj);
   atexit(&$F stub); OPPOPSTATE }`. The state's action clears the guard bit if the constructor throws. **[C:
   eh_more `m17`]** (IL not re-verified; the resulting frames and states are, rules 11-15)
9. **try/catch**:
   ```
   OPEHTRY ; <try body> ; [OPGOTO end] ; OPCATCH ; OPPUSHSTATE <- 0 ; <handler> ; OPDTORACTION ; OPGOTO end ;
   [OPCATCH ...] ; OPEHENDTRY ; end:
   ```
   Handlers are in source order inside the `OPEHTRY`..`OPEHENDTRY` bracket; each starts with `OPPUSHSTATE <- 0`
   (the catch state; a catch **by value** of a class with a destructor has a second one for the caught object)
   and ends in `OPDTORACTION`. A `return` inside a try body is a plain `OPRET` (c2 turns it into a branch out of
   the region; after canonicalisation the returns sit after `OPEHENDTRY` and the handlers end in `OPCATCHRET`).
   `throw x;` is `sN = x; __CxxThrowException(&sN, &_TI...)` + `OPEXITGOTO`; `throw;` is
   `__CxxThrowException(0, 0)`. **[C: eh_a `a4`, `a5`, `a8`; eh_more `m12`, `m13`]** (The extra state for a catch by
   value re-verified, C++ game flags: verify/v5b.cpp `b12` (by reference) `maxState` 2, `b13` (by value) 3. The tuple
   sequence itself not re-verified.)
10. **The EH tuples do not depend on the `/EH` switch, on the optimisation level, or on anything known about the
    callees.** Without `/EH`, with `/EHsc`, `/EHs` and `/EHa`, at `/Od` and `/O2`, and whether the calls in between
    are `throw()`, `extern "C"` or ordinary, the IL at `@MscRVO@4` is the same; a function declared `throw()` or
    `__declspec(nothrow)` has the same body IL as the undecorated one. **[C: eh_frame `f01`/`f03`/`f04`/`f05` with
    and without `/EHsc`; eh_more `cmp`: `m01` == `m02` == `m03`; eh_a, eh_states `/Od` vs `/O2` identical]** Whether
    a frame results is entirely c2's decision (rules 15-17). (Exception: rule 7.)

### From markers to states, stores and funclets

11. **State numbers are assigned in order of the `OPPUSHSTATE`/`OPEHTRY` tuples in the function**, from 0; the
    idle state is -1. Scopes that are siblings do not share a number (`if (a) { D x; } else { D y; }`: x is 0, y
    is 1). A try block takes one state for the guarded range, then its body's objects, then one state per try for
    the handlers, then the handlers' objects: `try { D x; } catch (int) { D y; }` is try 0, x 1, catch 2, y 3
    (`__tryblocktable`: tryLow 0, tryHigh 1, catchHigh 3). `maxState` in `__ehfuncinfo$` is the count. **[C:
    eh_states `s1` (0,1,2), `s6`, `s3`, `s8`; constructor `C::C` 0..3]** (Re-verified, C++ game flags, member
    functions: verify/v5b.cpp `b08` stores 0, 1, 2, 1, 0, -1; `b10` siblings 0 and 1; `b11` try 0, x 1, catch 2, y 3,
    `maxState` 4; `b17` five states.)
    **Corrected (verify/v5b.cpp `b09`, verify/v5c.cpp `c01`, `c02`, `c03`, `c10`, `c11`, `c12`): the numbers are assigned to
    the states that survive, not to the `OPPUSHSTATE` tuples. A state inside which no may-throw call remains is
    deleted, and the later states move down.** The constructor of a nested object lies inside the enclosing
    object's state, not inside its own, and its destructor runs after its own state has been left. So:

    | body | `maxState` | states |
    |---|---|---|
    | `D x; D y; D z; return a;` | 2 | x 0, y 1; **z has none** (nothing happens while only z is the innermost) |
    | `D w; D x; D y; D z; return a;` | 3 | the last object never has a state |
    | `D x; h(1); D y; D z;` | 2 | x 0, y 1 (z's constructor is inside y's state) |
    | `D x; h(1); { D y; } h(2); D z; h(3);` | 2 | x 0, **z 1**; y is deleted |
    | `D x; { D y; n_c(1); } D z; h(3);` (`n_c` is `extern "C"`) | 2 | y deleted: only a nothrow call inside it |
    | `D x; h(1); D y; n_c(2); D z; h(3);` | 3 | y kept: z's constructor may throw inside it |

    `maxState`, the number of `__unwindtable$` entries and the funclet numbers `$0, $1..` all count surviving
    states. This is rule 15's criterion applied state by state. **[C]**
12. **`__unwindtable$` has one entry per state: {state to go to, funclet}.** The funclet of an object's state is
    `lea ecx, obj; jmp dtor` — always an out-of-line call to the destructor, even when the destructor was inlined
    on the normal path (so an inline destructor used under EH is always emitted as a COMDAT). Try and catch
    states have a null funclet. A `new` state's funclet calls `operator delete`; a conditional temporary's tests
    its flag bit. Funclets are numbered by state and laid out in `.text$x` in state order, followed by
    `__ehhandler$F` (`mov eax, OFFSET __ehfuncinfo$F; jmp ___CxxFrameHandler3`, with a `/GS` cookie check
    first when the function has a cookie). **[C: listings of `f01`, `f12`, `f14`, `s1`, `s3`, `s7`, `C::~C`]**
    (Re-verified, C++ game flags: verify/v5c.cpp `c06`: `~F` inlined and deleted on the normal path, funclet `lea ecx,
    _f$[ebp] / jmp ??1F@@QAE@XZ`; `c07`: try and catch states with null funclets; verify/v5b.cpp `b08`: three funclets
    in state order, then the handler.) **Under `/GS` every EH function in the verify probes has the cookie**: the
    prolog pushes `___security_cookie ^ esp` (or `^ ebp`) after the registration even when the function has no
    buffer, and `__ehhandler$` checks it; the epilogue checks it only when there is a buffer. **[C: v5c `c06`, `c07`,
    `c08`]**
13. **State stores** go to `__$EHRec$+8` (`[ebp-4]` in an ebp frame). The prolog pushes -1. Then:
    - entering state n: a store of n **after** the constructor call returns, scheduled before the next call;
    - leaving a state on the normal path: a store of the enclosing state **before** the destructor call (`s1`:
      0, 1, 2, then 1 before `~z`, 0 before `~y`, -1 before `~x`);
    - a destructor's body starts in its highest state (`C::~C`: 3) and steps down before each member/base
      destructor;
    - a store whose state can no longer be observed is dropped: no store before a destructor that is inlined
      away, none at all between the last may-throw call and the return (`f12`: only the 0; `f14`: no -1 after the
      constructor);
    - **encoding**: the first store in a function is `mov DWORD PTR [..], n`; later stores of
      small non-negative states are `mov BYTE PTR [..], n` (low byte only, shorter) as long as the upper bytes are known to be
      zero; going back to -1 is a DWORD store; a zero may come from a register (`mov [..], ebx` / `bl`). **[C:
      `s1`, `s3`, `C::~C`, `s7`]** (Re-verified, C++ game flags, verify/v5b.cpp `b08`: `mov DWORD PTR .., 0`, `mov BYTE
      PTR .., 1`, `.., 2`, `BYTE 1` before `~z`, `BYTE 0` before `~y`, `DWORD -1` before `~x`; verify/v5c.cpp `c06`: no
      store before the inlined destructor and no -1.) **Addition:** a store that follows a -1 on its path is a
      DWORD again: in `if (a) { D x; h(1); } else { D y; h(2); }` both arms store with `mov DWORD PTR` (0 and 1),
      v5b `b10`. **[C]** In Fallout3.exe's ebp-frame functions `mov byte ptr [ebp-4], 0xff` never occurs (0 sites
      against 79 `mov dword ptr [ebp-4], -1`), and non-negative states are stored both ways (236 BYTE, 139 DWORD).
      **[C: `verify/f3x.py stats`]**
14. **Frame shape.** Unwind-only functions (objects, no try) use the normal frame (esp-based at `/O2`) with
    `push -1; push __ehhandler$F; mov eax, fs:0; push eax` in the prolog and the `fs:0` restore in every epilog.
    A function with a `try` always gets an **ebp frame**, saves esp into `__$EHRec$[ebp]`, and pushes ebx, esi and
    edi whether used or not; each handler is a funclet in the function's own `.text` that ends `mov eax,
    OFFSET continuation; ret`. **[C: `f01` vs `f21`, `s3`]** (Re-verified, C++ game flags, member functions:
    verify/v5c.cpp `c08` esp frame; `c07` `push ebp / mov ebp, esp`, `sub esp, 8`, `push ebx / push esi / push edi`
    with none of the three used, `mov __$EHRec$[ebp], esp`, handler ending `mov eax, $LN9 / ret 0`.)
    **Fallout3.exe [C: `verify/f3y.py`]:** of 8,042 EH functions found by their prolog, 91 have a try block and all
    91 have an ebp frame (90 `push ebp / mov ebp, esp`; one, 0x00ba7860, the biased form `push ebp / lea ebp,
    [esp-0x3c] / sub esp, 0x3c`). Of the 7,951 without a try, 7,924 have an esp frame and **27 have an ebp frame**
    (0x0049e490 is one: unwind-only, one `new` state); why those 27 is not established, so "esp frame" for an
    unwind-only function is the normal case, not a law. **[C for the counts; cause unknown]**

### What makes a function get an EH frame

c2 decides, after inlining, from the markers plus what it knows about each remaining call.

15. **Rule: a frame exists iff, after inlining, some state other than -1 still contains a call that may throw**,
    or the function has a `try` whose body contains one. One object alone is not enough: its constructor runs
    before its state and its destructor after it. **[C: eh_frame, `/EHsc /O2`, table below]**
    **Every row of the table was reproduced in member functions that also contain a loop over a member array,
    member stores and an inlined member call (verify/v5a.cpp `Actor::a01`..`a26`, `/TP /MT /GS /EHsc /Gy /O2`): 26
    of 26 as stated (re-verified, C++ game flags).** Further rows from the same probe and v5b, v5c, v5e are in
    the second table.

    | function body | frame | why |
    |---|---|---|
    | `D d; return h(a);` (`h` external C++) | yes | `f01` |
    | `D d; return a * 3;` | no | nothing between ctor and dtor (`f02`) |
    | `D d; D e; return a;` | yes | `e`'s ctor and dtor run while `d` is live (`f18`, also with `throw()` ctors: `f19`) |
    | `D d; return n(a);`, `n` declared `throw()` / `__declspec(nothrow)` / `extern "C"` | no | `f03`, `f04`, `f05` |
    | `D d;` + call to an inline function that is inlined and calls nothing | no | `f06` |
    | `D d;` + call to a `noinline` function **defined in this file** whose compiled body cannot throw | no | `f07`; defined later in the file too (`m04`): callee-first compile order (rule 13 of section 4) |
    | `D d;` + call to a function in this file that calls `h` | yes | `f22`, `m18` |
    | `D d;` + indirect or virtual call | yes | `m06`, `m07` |
    | `D d; if (a) throw 1;` | yes | `f25` |
    | `throw a;` alone | no | `f15` |
    | by-value `D` parameter + `h(a)` | yes | `f09`; unused by-value parameter: no (`f23`) |
    | temporary `D(a).get()` | yes | `f10` (`get` may throw while the temporary lives) |
    | `new D(a)` | yes | `f14`; `new` of a POD or of a class with a `throw()` ctor: no (`m08`, `m09`) |
    | `D d[3]; h(a);` | yes | `f17` |
    | static local `D` | yes | `m17` (guard reset) |
    | `E e; h(a);` with **empty** inline `~E() {}` | no | `f11`: the state has no action left |
    | `F f; h(a);` with inline `~F() { x = 0; }` | yes | `f12`: the normal-path destructor is inlined and deleted as a dead store, but the funclet still calls `??1F` |
    | `try { n_c(a); } catch (...)` (nothrow body) | no | `f08`: try and handler removed |
    | `try { h(a); } catch (...)` | yes | `f21` |
    | the same function declared `throw()` or `__declspec(nothrow)` | **no**, even with `D d; h(a);` inside | `m01`, `m03`: the specification is taken as a promise for the body too |

    Added by the verification pass (all member functions, `/EHsc /O2`) **[C]**:

    | function body | frame | probe |
    |---|---|---|
    | `D d;` + call to a `noinline` member of the same class whose body cannot throw | no | v5a `a27` |
    | `D d;` + call to a `noinline` member whose body calls `h` | yes | v5a `a28` |
    | `D d;` + call to an `inline` function that is **not** inlined (large body, defined later, calls nothing) | no: a COMDAT callee compiled earlier is trusted like any other | v5a `a33` |
    | `D d;` + call through a pointer whose type is an `extern "C"` function pointer | yes: indirect calls always count | v5a `a29` |
    | `D d; int (*q)(int) = n_ni; q(a);` (`n_ni` nothrow, same file) | **yes**, although the final code is the direct `call ?n_ni@@YAHH@Z` | v5a `a44` |
    | `DT d;` (all members `throw()`) + **self-recursive** call | **yes**: the function's own nothrow status is not known while it is compiled | v5b `b01` |
    | two functions with `DT d;` that call each other | yes, both | v5b `b02`, `b03` |
    | `DT d; DT e;` with constructor and destructor both `throw()` | no | v5a `a34` |
    | `D d; { D e; }` | yes (`e`'s constructor and destructor inside `d`'s state) | v5a `a41` |
    | `{ D d; } h(a);` and `h(a); D d;` | no (the call is outside the state) | v5a `a42`, `a43` |
    | `D d; VL o; o.vf(a);` (named object, inline virtual, direct call) | no | v5a `a37` |
    | the same through `VL* p = &o; p->vf(a)` | yes | v5a `a38` |
    | `DT d; new P0;` (POD, plain `operator new`) | yes, with or without `<new>` | v5b `b07`, v5e `e1` |
    | `DT d; new (std::nothrow) P0;` | no | v5c `c05`, v5e `e2` |
    | `DT d; delete p;` (POD) **without `<new>`** | **yes**: the implicitly declared `operator delete` has no `throw()` | v5a `a31`, v5b `b04` |
    | `DT d; delete p;` **with `<new>` included** | no (`<new>` declares it `throw()`) | v5c `c04`, v5e `e3` |
    | `DT d; delete pw;` (virtual destructor) | yes (indirect call) | v5b `b06` |

    Game code includes `<new>` through almost any standard header, so there a plain `delete` of a POD does not
    create a frame; a probe written without headers gets one. Check the draft's headers before blaming the body.

16. **"May throw" under `/EHsc`** = a call c2 cannot prove nothrow: external C++ functions, indirect and virtual
    calls, `__CxxThrowException`, `operator new`. Nothrow = declared `throw()`, `__declspec(nothrow)`,
    `extern "C"` (the `c` of `/EHsc`), or a function of this translation unit already compiled and found to
    contain no may-throw call (interprocedural, available because of the callee-first order; not at `/Od`).
    Ordinary instructions (memory access, division) never count. **[C: the table; `/Od`: `f06`, `f07`, `f11` get
    frames, `f03`, `f04`, `f05` still do not]** (Re-verified, C++ game flags, member functions, verify/v5d.cpp at
    `/Od`, `/O1`, `/O2`: `throw()`, `nothrow`, `extern "C"` callees and a division never give a frame; the inline
    callee, the `noinline` callee and the empty destructor give one at `/Od` only. `/O1` equals `/O2`.)
    **Conditions:** "already compiled" excludes the function itself and the other members of a call cycle (v5b
    `b01`..`b03`), and a function reached through a pointer even when c2 later resolves the pointer (v5a `a44`).
    `operator new` counts unless it is the `std::nothrow` overload; `operator delete` counts only when no
    declaration with `throw()` is in scope (table above).
17. **Other `/EH` settings, same source (eh_frame, `/O2`)** **[C]**:
    - no `/EH` flag: only functions with `try` get a frame (`f08`, `f21`; warning C4530), and `f08` keeps it
      although its body is `extern "C"`; no unwind states at all, so destructors do not run on unwind.
    - `/EHs`: as `/EHsc` except that `extern "C"` functions may throw (`f05`, `f20`, `f26` gain frames).
    - `/EHa`: every function with a state keeps its frame, including `D d; return a * 3;` (`f02`), a lone object
      with `throw()` members (`f16`) and nothrow-only bodies; only the empty-destructor case (`f11`) and the
      bare `throw` (`f15`) have none.
    (Re-verified on verify/v5a.cpp with the four settings: no `/EH`: only the two `try` functions `a24`, `a25` have
    frames; `/EHs`: `a06` (`extern "C"` callee) and `a24` (try around an `extern "C"` call) gain one; `/EHa`: every function with a
    state has one except `a22` (empty destructor), `a18` and `a19` (`new` without a state). **Addition:** under
    `/EHa` a `throw()` specification on the function itself no longer removes the frame (`a26`), and `{ D d; }
    h(a);` / `h(a); D d;` get one too (`a42`, `a43`). **[C]**)
18. **Consequences for matching.** A target function with an EH frame and one visible destructor call has, in the
    original, some may-throw call inside that object's lifetime; if the draft has no frame, either the callee is
    declared nothrow in the draft's headers (`throw()`, `extern "C"`), or it is defined in the draft's translation
    unit where the original only saw a declaration (or was inlined). The reverse (a frame the target lacks)
    points at a missing `throw()`/`extern "C"` on a declaration, or at a destructor that the original had empty
    and inline. The number of unwind-table entries equals the number of `OPPUSHSTATE`s that survive, i.e.
    destructible locals + temporaries + by-value class parameters + `new` expressions with throwing
    constructors (+ try and catch states), in source order of construction, which pins down their order.
    **Corrected:** "that survive" must be read with rule 11's correction: an object inside whose lifetime nothing
    may throw has no entry, and that always includes the innermost, last-constructed object when only its own
    destructor follows. `D x; D y; D z;` has two entries. So the entry count is a lower bound on the number of
    destructible objects, and a target with N entries and N+1 destructor calls in a row is normal. **[C: v5b
    `b09`, v5c `c12`]**
    **Checked on Fallout3.exe [C: `verify/f3x.py eh VA`, `verify/f3y.py`]:**
    - 0x00487210 (4 states, esp frame): state 0 is an object (`lea ecx,[ebp-0x24] / jmp dtor`), states 1, 2, 3 are
      three `new` expressions (funclets `mov eax,[ebp+N] / push eax / call 0x00401010`, each with parent 0).
      Stores: `mov dword [esp+0x38], 0` first, then `mov byte .., 1` between two calls (taken to be the allocation
      and the constructor **[I]**), `mov byte .., 0` after the second, and the same with 2 and 3: rules 7, 11, 13
      as written.
    - 0x00aa2d70 (a constructor, 4 states): state 0 `mov ecx,[ebp-0x10] / jmp` (base, through `this`), state 1 a
      member, states 2 and 3 `new` states with parent 1; first store a zero from a register, then BYTE stores 1, 2,
      1, 3; no store of -1 before the `ret 0xc` (rule 13, "dropped when it can no longer be observed").
    - 0x00589560 (a destructor, 3 states): funclets `mov ecx,[ebp-0x18] / (add ecx, 8 | 0xc) / jmp dtor` chained 2
      → 1 → 0; the body's first store is `mov dword [esp+0x28], 2` (starts in its highest state), then `mov byte
      .., 1` and `mov byte .., bl` before the member destructors: rule 13's third bullet.
    - 0x0075c0e0 (3 states, each with parent -1): three sibling scopes, three numbers (rule 11); the stores are
      DWORD each time because a -1 lies between them (rule 13's addition).
    - 0x00ba7860 (try block, 6 states): biased ebp frame, `mov [ebp-0x10], esp`, ebx/esi/edi pushed; state 0 (try)
      and state 3 (catch) have null funclets, states 1, 2 are objects of the try body, 4, 5 objects of the handler:
      rules 11, 12, 14.
    - 0x00c828a0 (a `??__E` initialiser with a frame) and 0x00d86640 (its `??__F` stub, `mov ecx, OFFSET obj / jmp
      dtor`): rules 3A.35 and 4.15.
    `maxState` over all 8,042: 6,069 functions have 1 state, 854 have 2, 360 have 3; 93 have 12 or more.

### Spellings that are identical after the front end (EH)

| A | B | probe |
|---|---|---|
| `int f(int a) { D d; return h(a); }` | the same with `throw()` or `__declspec(nothrow)` on `f` (IL identical; c2 then drops the frame) | eh_more `m01` == `m02` == `m03` |
| callee declared plain | callee declared `throw()` / nothrow / `extern "C"` (caller IL identical apart from the symbol; frame differs) | eh_frame `f01`, `f03`, `f04`, `f05` |
| `/EHsc` | no `/EH`, `/EHs`, `/EHa` (IL at MscRVO) | eh_frame `f01`, `f21` |
| `/Od` | `/O2` (IL at MscRVO) | eh_a, eh_states: all functions |
| `k(a)` with a class-type default argument | `k(a, Q(5))` | fn_cc `c_defc1` == `c_defc2` |

Not identical: one `return` at the end versus several early returns (one `OPDTORACTION` + return temporary +
`OPRET` each: rule 2, 3); declaring an object in an inner block versus the function block (different
`OPPOPSTATE`/`OPDTORACTION` placement and, with siblings, different state numbers: rule 11); catch by value
versus by reference of a class (extra state: rule 9); mem-initialiser order does **not** change anything (rule 6).

### Open questions

- The dump now prints the payload of `OPPUSHSTATE`/`OPDTORACTION`/`OPNEWSTATE` (EH record at tuple +0x30;
  tracer.md, `il`). **[C]** "One action covers N objects" is in the record: eh_a `a2` has two `OPPUSHSTATE`s,
  each followed by its unwind action (`A` lines: `OPARG <- _d ; s0=&` / `OPCALL ; s0=??1D@@QAE@XZ`), and one
  `OPDTORACTION ; eh{n=2 fl=1}`. After canonicalisation each `OPPUSHSTATE` prints `state=N to=M` (eh_states `s1`:
  0/-1, 1/0, 2/1; `s3`: x 1/0, the handler state 2/-1, y 3/2), its action is the funclet
  `__unwindfunclet$F$N` (`lea ecx, obj; jmp dtor`), and every `OPNEWSTATE` names the state it enters or leaves;
  this is rule 11 and rule 12 read from the records instead of the listing.
- `goto` out of several nested scopes and `switch` fall-out: **one `OPDTORACTION` per jump, with n = the number
  of states left.** `{ D x; { D y; if (a) goto out; .. } .. } out:` has one landing block `L: OPDTORACTION ;
  eh{n=2 fl=1}; OPGOTO -> $out`; in a `switch` under a live `D w`, `case 1: { D x; h(1); break; }` is
  `OPDTORACTION ; eh{n=1 fl=1}; OPGOTO -> end; OPPOPSTATE ; eh{n=1 fl=a1}` and `case 2: { D y; h(2); return 5; }`
  is `s = 5; OPDTORACTION ; eh{n=2 fl=1}; OPRET <- s`. **[C: work/scratch/ilx/ilx_eh.cpp `g1`, `g2`]**
- Function-try-blocks, exception specifications other than `throw()`, catch of a class by value with a copy
  constructor, and nested try blocks were not probed.
- Rule 13's condition for a BYTE store ("upper bytes known zero") is inferred from four listings; the c2 routine
  that emits the store (`EhgUpdateEhState`?) was not read.
- Answered (rule 15, second table): an `inline` COMDAT callee that is not inlined is trusted the same way (v5a
  `a33`).
- `f07` showed c2 moving a call to a side-effect-free local function past the destructor (tail call after `~D`);
  not an EH rule, noted for the back-end docs.

---

## 6. Declarations that change code without changing meaning

Probes: `dc_decl.cpp`, `dc_scope.cpp`, `dc_prag.cpp`. Stack-slot rules are in frame.md; this section only says what
the front end contributes.

6.1 **`const` and `register` on locals and parameters, and the initialisation syntax, change nothing**: `int t = e`,
   `const int t = e`, `register int t = e`, `int t(e)`, `int t; t = e;`, a `const int` parameter are one IL class
   and one code class. A top-level `const` on a pointer parameter (`int* const p`) and a reference parameter
   against a pointer parameter are the same IL too. **[C: dc_decl d1 == d2 == d3 == d4 == d6 == d7, r1 == r4 == r5;
   verify/v6.cpp d1 == d2 == d3 == d4 == d6, IL and code, all three contexts (re-verified, C++ game flags)]**
   (A local `const int k = 4` with a *constant* initialiser is different: its uses become the literal, rule 2.24.)
6.2 **`volatile` changes the opcode of every access**: reads and writes of a volatile object are
   `OPVOLATILEASSIGN` (one per source-level access, each read into its own temporary), so nothing is merged or
   kept in a register: `gv + gv` is two loads; `int t = gv; t + t` is one. **[C: dc_decl d5, v1, v2; verify/v6r.cpp
   `M::v1` (two loads of `gv`), `M::v2` (one load, `lea eax,[ecx+eax*2]`), in member functions (re-verified, C++ game
   flags)]**
6.3 **`__restrict` is invisible in the tuple stream but changes the code**: `r3(int* __restrict p, int* __restrict
   q)` has the IL of the unqualified function, yet c2 forwards the stored value across the other store
   (`mov eax, 1` instead of reloading `[eax]`). The qualifier travels on the symbol's type, not in a tuple, and
   shows in the decorated name (`PIAH`). **[C: dc_decl r1 == r3 in IL, different asm; verify/v6r.cpp `M::r1` == `M::r3` in
   IL, and in code `M::r1` reloads (`add eax, DWORD PTR [edx]`) where `M::r3` has `add eax, 1`, in member functions
   (re-verified, C++ game flags)]** A pointer to `const`
   (`const int* p`) gives c2 nothing: the load is still repeated after a store through another pointer (r2).
6.4 **`__declspec(noalias)`, `__declspec(nothrow)`, `throw()`, `__declspec(noinline)` and `static` on a function do
   not change its body's IL** (one class: dc_decl n1..n6). What they change is on the symbol and acts in c2:
   - `noinline`: the call is never an inline candidate (inline.md rule 2);
   - `nothrow` / `throw()`: callers need no EH state around the call, and the function itself gets no EH frame
     (rules 5.15, 5.16); a `throw()` constructor also removes the `new` state already in c1xx (rule 5.7);
   - `static`: with no address taken, c2 may give the function a private calling convention when it compiles the
     callee first (n6's body reads its argument from eax; il.md §2.2) and may inline and then drop it;
   - `noalias`: no difference in these probes (n1 == n2 in code).
   **[C: dc_decl n1..n7 IL and asm]** (not re-verified)
6.5 **Block scope is not in the instruction tuples, only in `OPBLKSTART`/`OPBLKEND` pragmas (rule 1.15), and it
   changes the code only through the lifetime of address-taken locals.** With scalars that stay in registers,
   declaring a variable in an inner block, at the top of the function, or C-style at the top with a later
   assignment is the same code (dc_scope c1 == c2 == c3; dc_decl o1 == o2 == o3 == o4). With a local whose address
   escapes, the block end tells c2 the object is dead: in

   ```c
   { int b[4]; h2(b); a += b[0]; } { int c[4]; h2(c); a += c[0]; } return a;     // dc_decl o7
   int b[4]; int c[4]; h2(b); a += b[0]; h2(c); a += c[0]; return a;             // dc_decl o8
   ```
   the instruction tuples are identical, but o7 delays the load of `b[0]` until after `h2(c)` (the second call
   cannot legally touch `b` any more) and needs no callee-saved register, while o8 must load before the call and
   keeps the sum in esi. Scalars behave the same way (`dc_scope` e1 against e2). **[C]** The two block-scoped arrays
   do **not** share a stack slot here (frame 32 bytes in both); when slots are shared is frame.md's subject.
   A local declared inside a loop body against outside it: same IL and code when it is assigned before use in
   every iteration (dc_scope e4 == e5).
   (Re-verified, C++ game flags, verify/v6.cpp, all three contexts: o1 == o2 == o3 one code class; o7 against o8
   different code with identical instruction tuples; b1 == b2, a local declared inside against outside the loop,
   IL and code.)
   A block-scoped local is named `_b$<n>` in the listing (with a number) where a function-scope local is `_b$`:
   a cosmetic difference that identifies inner-scope declarations in an original listing, if one exists.
6.6 **Declaration order of locals is not in the IL at all**, and at `/O2` it does not decide stack offsets
   (dc_decl o5 against o6: `b` and `c` declared in either order get the same offsets, -32 and -16; frame.md rule 3
   gives the order: size, use count, first reference). **Unused variables leave nothing**: `int unused;`, `int
   unused[8];`, an expression statement `a;` and `(void)a;` are identical IL to their absence. An initialised
   unused variable keeps its store (and the initialiser's computation, and of course its calls) in the IL as dead
   code that c2 deletes: same code. **[C: dc_scope u1 == u2 == u4 == u9 == u10 in IL; u3, u5 same code; u6 keeps
   the call; verify/v6.cpp u1 == u2 == u4 == u9 in IL, u3 same code, o5 == o6 in IL and code, all three contexts
   (re-verified, C++ game flags)]**
6.7 **Copies into named locals are one `OPASSIGN` each** (`int t = a; h(t)`; a reference `int& r = a` is the
   address in a pointer local and `[_r+0]` at the use; a pointer `int* p = &a` the same). Different IL from the
   direct use, same code in these probes; frontend.md lists when such a variable survives into register allocation.
   **[C: dc_scope u7, u8, u11, u12: one asm class with u1; verify/v6.cpp u7, u8, u11 one code class with u1 in all three
   contexts, EH frame included (re-verified, C++ game flags)]** A named copy of a *loaded pointer* did change the
   code in an EH function (rule 2.11's condition), so "same code" here is for copies of a parameter.
6.8 **`#pragma optimize`, `/Oy`, `/GS`**: the tuple stream is the same under `#pragma optimize("", off)`, `("t",
   on)`, `("s", on)`, `("y", off)` and under `/GS-`, `/Oy-` on the command line; the options reach c2 as the
   per-function option word (IL pragma 0x1f, il_pipeline.md §6), and c2 does the rest: `("", off)` gives the `/Od`
   code for that function, `("y", off)` an ebp frame with otherwise optimised code, `("s", on)` size-biased code.
   **[C: dc_prag p1 == p2 == p3 == p4 == p5 in IL; asm p1 == p3, p2, p4, p5 distinct]** (not re-verified)
6.9 **`__asm` and `__declspec(naked)`**: an `__asm` block is one opaque `OPASM` tuple (consecutive blocks are
   merged into one); a naked function is just that tuple and gets no prologue, epilogue or `ret` from c2. A
   non-naked function containing `__asm` still gets frame code as needed, and c2 turns global optimisation off for
   it when the asm reads or writes registers it cannot track (il_pipeline.md, C4740). **[C: dc_prag p6, p7]** (not re-verified)
6.10 **Namespace-scope `const`** objects with constant initialisers are substituted as literals and need no
   storage (rule 2.24); `static` locals are in 3A.32-35. A `static int` local used as an ordinary variable is a
   global memory operand under a decorated name (`?t@?1??d8@@YAHH@Z@4HA`) with no guard. **[C: dc_decl d8]** (not re-verified)

---

## 7. Quick reference

### 7.1 Spellings that are identical after the front end (not worth trying)

Same IL class at `@MscRVO@4`; section tables in 3A, 3B, 4 and 5 list the object-related ones.

| spellings | probe |
|---|---|
| `if (a)` / `if (a != 0)` / `if (!!a)`; `if (!a)` / `if (a == 0)`; `!(a >= b)` / `a < b` | cf_if i1, i2, i4; cf_q q24, q25; ex_cast q11, q12 |
| `a && b` / `!(!a \|\| !b)`; `a \|\| b` / `!(!a && !b)` | cf_if k1, k3, k6, k8 |
| `else if` chain / nested `else { if }` | cf_if c1, c2 |
| `a < b` / `(a < b) ? 1 : 0` (value); `a && b` / `(a && b) ? 1 : 0` / `a != 0 && b != 0` | cf_q q27, q28; cf_if v1, v2, v5 |
| `while (n) {..}` / `for (; n; ) {..}` / `top: if (n) {..; goto top;}` | cf_loop w1, w3, w7 |
| `for (int i = 0;..)` / `int i; for (i = 0;..)`; `i++` / `++i` / `i = i + 1` as the increment | cf_loop f1, f3, f11 |
| `switch` on an `enum` / on an `int` | cf_sw s1, s13 |
| `return (e1, e2);` / `e1; return e2;`; `x = a, y = b;` / two statements | cf_ret r11, r12, m1, m2 |
| `if ((x = f()) != 0)` / `x = f(); if (x)` / `int x = f(); if (x != 0)` / `if (int x = f())` | cf_ret a1..a5 |
| `g1 = g2 = e;` / `g2 = e; g1 = g2;`; `*p = *q = 0` / `*q = 0; *p = 0` | ex_order s3, s4, s6, s7 |
| `x += a` / `x = x + a` (any operator; also through `*p`, `s->m`) | ex_ops a1, a2, a4..a7, a10..a13, a18, a19 |
| `x++;` / `++x;` / `x += 1;` / `x = x + 1;`; `x--;` / `x -= 1;` | ex_ops i1..i6 |
| `(*p)++` / `*p += 1`; `(*pp)++` / `*pp += 1` / `*pp = *pp + 1` | ex_ops i11..i15 |
| `p[i]` / `*(p + i)` / `i[p]` / `*(i + p)` / `*&p[i]`; `p[2]` / `*(p + 2)`; `arr[i]` / `*(arr + i)` | ex_ops p1..p4, p6..p8, p22, p23 |
| `s->x` / `(*s).x` / `s[0].x` / `*(int*)s`; `s->y` / `((int*)s)[1]` / `*(int*)((char*)s + 4)` / `(&s->x)[1]` | ex_ops p9..p16 |
| `s->a[i]` / `*(s->a + i)`; `s[i].y` / `(s + i)->y` | ex_ops p17, p18, p25, p26 |
| any same-size integer, pointer or enum cast; `static_cast` / `reinterpret_cast` / `const_cast` / C cast | ex_cast k1..k13 |
| `c = a` / `c = (char)a` (narrowing store) | ex_cast n17, n18 |
| `a < b` (int against unsigned) / `(unsigned)a < (unsigned)b` | ex_cast q1, q2, q3 |
| `gb = a` / `gb = (bool)a` / `gb = a ? true : false`; `gb = a != 0` / `gb = !!a` | ex_misc b1, b3, b5; b2, b4 |
| `if (x)` / `if (x != false)` (bool); `!x` / `x == false` | ex_misc b15..b17, b19 |
| `a * i` / `a * (float)i` | ex_misc x7, x8 |
| `a + 2 + 3` / `2 + 3 + a` / `a + 5`; `a * 1`, `a + 0`, `a / 1`, `a << 0`, `a & ~0` / `a`; `-1 + a` / `a + -1` | ex_misc c2..c5, c8, c14, c17; ex_arith m27, m28 |
| literal / `#define` / enumerator / namespace-scope `const int` (also `static const`) | dc_prag k1, k4, k9..k11 |
| `int t = e` / `const` / `register` / `int t(e)` / `int t; t = e;` | dc_decl d1..d4, d6, d7 |
| unused `int u;` / `int u[8];` / `a;` / `(void)a;` / nothing | dc_scope u1, u2, u4, u9, u10 |
| extra braces (also around loop and case bodies), empty statements, empty `else {}`: same instruction tuples, scope pragmas only; no address-taken locals inside | dc_scope b1..b5; cf_loop f1, f4; cf_sw s1, s11 |
| the same source at `/Od`, `/O1`, `/O2`, `/GS-`, `/Oy-`, any `/EH` (except switch partitioning, intrinsics, string symbols) | §0, 4.6, 5.10 |

### 7.2 Different IL, but the same `/O2` code in the probes (low-value levers)

They can matter only through register allocation or scheduling in larger functions; none changed a probe's code.
**Corrected by the verification pass: that holds for the one-statement free-function probes only.** Re-tested as
member functions with neighbouring code and with an EH frame (verify/v1.cpp, v2c.cpp), these entries of the list did
change the code: early returns / result variable / nested `?:` (registers swap in a plain member function, rule
1.1); the two-`if` early-return form of `a && b` when the function returns a value (block order, rule 1.5); `break`
/ extra `&&` in the loop condition with a variable bound (rule 1.9); named intermediate pointer / direct chain in
an EH function (scratch registers, rule 2.11); and every pair that differs in the number of `return`s once a
destructible local is live (rules 1.6, 1.7, 2.19, 3B.3). The others in the list held in all three contexts.
`0 != a` / `a != 0`; `5 < a` / `a > 5`; `if (a && b) S` / nested `if`s / early-return forms; `r = c ? x : y` /
`if/else` assignment / returns in both arms; `for` / `while` with trailing increment; `do-while` / `for(;;)` with
`break` at the end; `continue` / inverted `if`; early returns / result variable / nested `?:` (no destructible
locals); `x = a + x` / `x += a`; `x += -1` / `x--`; `c += 1` / `c++` on a `char`; named intermediate pointer /
direct chain; `p[i+1]` / `(p+1)[i]` / `*(p+i+1)`; `a*2` / `a<<1` / `a+a` and the other pairs of rule 2.10;
`(a < b) != 0` / `a < b`; `(float)((double)a + (double)b)` / `a + b`; `a * 2` / `a * 2.0f` / `a * 2.0`; `a / 2` /
`a * 0.5f` (float); `const float K` / the literal; dead initialised locals; `int t = a; h(t)` / `h(a)`; `a && h(1);`
/ `if (a) h(1);`; switch case order for a two-case chain.

### 7.3 Look alike, but differ in IL and in code (real choices)

| choice | difference | rule |
|---|---|---|
| `if (c) A; else B;` / `if (!c) B; else A;` | block order follows the source | 1.3 |
| `c ? x : y` with constant arms / anything else | `OPQUESTION` (setcc/sbb forms) / branches | 1.6 |
| `a && b` / `(a != 0) & (b != 0)` | branches / two questions and an `and` | 1.5 |
| `switch` / `if-else if` chain; case order with a jump table | `sub/je` chain or table / `cmp/jne`; body layout | 1.11-1.13 |
| `goto out` single exit / early returns | shared `ret` | 1.1 |
| one expression with several calls / named locals per call | c2 may reorder the calls at `/O2` | 2.4 |
| `f(a++)`, `g = a++` / increment as its own earlier statement | postfix increment is placed after the full expression | 2.5 |
| `(unsigned char)a` / `a & 0xff` | `movzx` / `and` | 2.15 |
| `(unsigned)a < 10` / `a >= 0 && a < 10` | one unsigned compare / two branches | 2.17 |
| `bool f(int a) { return a; }` / `return a != 0;` | 1-byte / int question: `cmp mem,0; setne` / `xor; cmp; setne` | 2.18 |
| `if (x == true)` / `if (x)` for a bool | compare with 1 / with 0 | 2.19 |
| `q - p` / `((int)q - (int)p) / 4` | `sar` / signed divide sequence | 2.12 |
| `a * 0.1f` / `a * 0.1` | different 8-byte constant | 2.22 |
| `a > 5` / `a >= 6`; signed `a / 2` / `a >> 1` | as written | 1.4, 2.10 |
| `volatile` on a local or global | every access is its own load/store | 6.2 |
| `__restrict` on pointer parameters | store-to-load forwarding across the other pointer | 6.3 |
| inner block around an address-taken local / function scope | load placement, callee-saved register use | 6.5 |
| `#pragma optimize`, `/Oy-` | per-function code generation mode | 6.8 |
| free function / non-static member function returning a small aggregate by value | registers / hidden pointer `___$ReturnUdt` | 3A.6 |
| member-wise assignment / whole-struct assignment, 28 bytes and more (or 23, 25..27) | N load/store pairs / `rep movsd` | 3A.15 |
| three dense `case`s / four | `sub`/`je` chain / jump table | 1.11 |
| `char a[40] = {0}` / `= {}`; in general a zero fill needing 11 or more stores | `call _memset` / inline stores | 3A.20 |
| `delete p` / hand-written `p->~D(); operator delete(p)` on a member pointer | one load / reload after the destructor | 3B.26 |
| `~T() {}` / no destructor, in a polymorphic class | EH frame in every function with such a local and a may-throw call / none | 3B.8 |
| object-related choices | see the "not identical" lists in 3A, 3B, 4, 5 | |

## 8. Open questions and tool gaps

- **Closed: `c2trace.py il` (tools/c2il.py and the `%+` dumper in c2trace.py) now prints** branch condition
  codes and targets, the `OPQUESTION` cc, call-target names (function symbols keep the name pointer at `sym+4`,
  not `[sym+0]+4`), the `OPSWITCH` case list, float-constant values, an `&` note distinguishing `&x` from `x`,
  non-instruction tuples in stream position (pragmas, `OPBLKSTART`/`OPBLKEND`, `OPUSERLABEL`, label names) and
  the EH record of `OPPUSHSTATE`/`OPDTORACTION`/`OPNEWSTATE`/`OPCATCH` (tuple +0x30) with the unwind action.
  Format and field sources: tracer.md, `il`. **[C]** The claims this settled are marked in rules 1.11, 3B's and
  5's open questions and below. Still not in the dump: symbol attributes that are not in a tuple (`__restrict`,
  `static`, calling convention) and the per-function option word. One thing to know when reading it: at
  `@MscRVO@4` an unsigned compare carries the signed condition code (`cc=l` for `<` on `:2004` operands) and c2
  switches to `b`/`a`/`be`/`ae` later, from the operand type. **[C: work/scratch/ilx/ilx_cc.c `c1`]**
- **c1xx.dll was not disassembled for this page.** Everything is black-box. Two attributions rest on traces of
  c2 rather than on c1xx code: that the `?:` expansion (1.6) and the switch partitioning (1.12) happen in c2's
  reader. Whether intrinsic expansion (4.3) is c1xx's or the reader's is open.
- Rule 2.4: the c2 pass that reorders calls inside an expression is bracketed (after the second `OptFuncExpr`
  entry, before `ItfFuncInterf2`) but not read; the table of shapes is observational and the "product or address
  operand first" summary is **[I]**.
- Rule 1.13: independence of a compare-chain switch from case order is shown for two cases plus default only.
- Rule 6.5: when block-scoped locals share stack slots, and how far a block end moves loads in larger functions,
  was not explored; nor was the effect of scope pragmas on register allocation priorities (frontend.md's
  0x00a89680 has no such case).
- Positions of `OPBLKSTART`/`OPBLKEND` relative to instructions are now printed (`P` lines). **[C:
  work/scratch/ilx/ilx_eh.cpp `g3`, `g2`]** The function body's `OPBLKSTART` is the first tuple after `OPENTRY`
  and its `OPBLKEND` the last before the exit label; an `if` statement is `OPBLKSTART; OPCMP; OPBRANCH;
  OPBLKSTART; <substatement> OPBLKEND; Lelse: OPBLKEND`; the block around a return-value temporary closes
  between the `OPRET` and the `OPGOTO` to the exit, the function body's `OPBLKEND` comes after that `OPGOTO`. The word at pragma +0x20 is the same on a matching pair and
  non-zero exactly for blocks that declare a variable, including the block c1xx wraps around a return-value
  temporary (`sN = a; OPDTORACTION; OPRET <- sN`). What the word points at is **[I]** (the block's symbols).
- Not probed in sections 1, 2 and 6: `__int64` arithmetic and shifts (helper calls), `long double`, `wchar_t`,
  compound literals, `?:` with mixed types, `switch` on `__int64`, `goto` into a scope, labels-as-values (none in
  MSVC), SEH (`__try`), `setjmp`, and RTTI (`dynamic_cast`, `typeid`).
- Sections 3A, 3B, 4 and 5 end with their own open questions.

---

## 9. Verification (independent re-test)

A second agent re-tested this page as a skeptic, without reading the first pass's probe sources beyond what a rule
quotes. Nothing in c1xx or c2 was read; it is black-box like the rest. All of it is `cl 14.00.50727.762` under
`/TP /MT /GS /EHsc /Gy /O2` unless a line says otherwise.

**Method.** Fresh probes in `work/scratch/feil/verify/`. Where a rule says "identical IL" or "same code", the body
was compiled in three contexts from one macro (`ctx.h`, driver `mk.py FILE`, which prints the IL classes and the
code classes per context): `_n` a free function; `_m` a member function with two statements on members in
front; `_e` a member function that begins `D d; n += h(m[a & 7]);`, so it has an EH frame and a live destructible
local. Thresholds were swept with generated files (`v3c.cpp`, `v3d.cpp`). Emission order was **predicted from the
rules before compiling** (`v4a.cpp`, `v4b.cpp`). `calls.py` prints call order per function; `f3x.py` / `f3y.py`
read Fallout3_1703.exe (read-only) with capstone.

```sh
uv run python work/scratch/feil/verify/mk.py work/scratch/feil/verify/v1.cpp     # IL and code classes, 3 contexts
uv run work/scratch/feil/eh_chk.py work/scratch/feil/verify/v5a.cpp             # frame / maxState / state stores
uv run work/scratch/feil/fn_obj.py work/scratch/feil/verify/v4b.cpp mscrvo      # compile order
uv run work/scratch/feil/verify/f3x.py stats ; uv run work/scratch/feil/verify/f3x.py eh 0x00487210
```

| probe | covers |
|---|---|
| `v1.cpp`, `v1s.cpp` | section 1: returns, `&&`, `?:`, loops, assignment in conditions; switch thresholds and case order |
| `v2o.cpp`, `v2c.cpp` | 2.1-2.6 evaluation order at `/O2` and `/Od`; 2.5-2.25 equivalences |
| `v3a.cpp`, `v3b.cpp` | 3A.6 struct returns (free, member, static member, conventions), 3A.8 NRVO |
| `v3c.cpp`, `v3d.cpp`, `v3h.cpp` | 3A.15, 19, 20, 21: block-copy, zero-fill and literal-copy thresholds |
| `v3e.cpp`, `v3f.cpp`, `v3g.cpp` | 3A.29, 31, 33; 3B.3, 7, 8, 10-13, 17, 20, 24-27 |
| `v4a.cpp`, `v4b.cpp`, `v4c.cpp`, `v4d.cpp`, `lk/la.cpp` + `lk/lb.cpp` (+ `lk/t.map`) | 4.1-4.15 |
| `v5a.cpp` .. `v5e.cpp` | 5.9, 5.11-5.18 (also at `/Od`, `/O1`, no `/EH`, `/EHs`, `/EHa`) |
| `v6.cpp`, `v6r.cpp` | 6.1-6.3, 6.5-6.7 |

**Count.** 81 rules re-tested (section 1: 12, section 2: 18, 3A: 11, 3B: 13, section 4: 12, section 5: 9,
section 6: 6).

| outcome | rules | which |
|---|---|---|
| held as stated | 57 | 1.2, 1.4, 1.7, 1.15, 1.16; 2.2, 2.3, 2.5-2.10, 2.12, 2.15, 2.17-2.19, 2.22, 2.23, 2.25; 3A.3, 19, 29, 31; 3B.7, 10-13, 17, 20, 24, 27; 4.1-4.4, 4.7, 4.8, 4.10-4.12, 4.14, 4.15; 5.9, 5.12-5.15, 5.17; 6.1-6.3, 6.5-6.7 |
| held, and a threshold or open case was pinned down | 8 | 1.11, 1.13, 2.4, 3A.8, 3A.15, 3A.21, 3A.33, 5.16 |
| holds only under a condition the rule did not state | 10 | 1.1, 1.5, 1.6, 1.9, 2.11, 3A.20, 3A.35, 3B.8, 3B.25, 3B.26 |
| wrong as written, corrected | 6 | 1.14, 3A.6, 3B.3, 4.13, 5.11, 5.18 |

Rules not listed were not re-tested and carry "(not re-verified)" where they stand. That includes all of 3B.14-16,
18, 19, 21-23, 28, 3A.1, 2, 4, 5, 7, 9-14, 16-18, 22-28, 30, 32, 34, the IL-shape rules 5.1-5.8 and 5.10, 4.5, 4.6,
4.9, 6.4 and 6.8-6.10, and every "identical at `/Od` and `/O2`" statement about the IL.

**Corrections, most consequential first.**

1. **3A.6: non-static member functions return every struct through a hidden pointer named `___$ReturnUdt`**,
   plain aggregates of 1, 4 and 8 bytes included. The register-return cases and the `$T` / `___$ReturnUdt` tell are
   for free and static member functions only (v3b).
2. **5.11 / 5.18: EH states are numbered after deletion.** A state inside which no may-throw call remains has no
   number, no unwind-table entry and no funclet; `D x; D y; D z;` has two states. `maxState` is a lower bound on the
   number of destructible objects, not the count (v5b `b09`, v5c `c01`-`c12`).
3. **3B.3 / 5.2: c2 does merge per-exit destructor sequences** unless the exits return different constants or leave
   different objects alive. The number of destructor call sites is a lower bound on the number of exits (v3f).
4. **4.13: `??__E` and `??__F` are outside the callee-first sweeps and always last; a cycle of three does not come
   out in definition order; address-of references and static locals with destructors delay a function** (v4a, v4b,
   v4d).
5. **5.15 / 5.16, conditions on "may throw":** a self-recursive or mutually recursive call counts as may-throw; an
   indirect call counts even when c2 resolves it; `operator delete` counts unless a `throw()` declaration (from
   `<new>`) is in scope; an un-inlined `inline` callee is trusted (v5a, v5b, v5c, v5e).
6. **3B.8: an empty user-written destructor is "empty" only in a non-polymorphic class**; with a vptr it keeps
   the EH frame (v3g `td1`).
7. **Thresholds located:** jump table from 4 cases (1.11); inline block copy up to 6 moves, i.e. `rep movsd` at
   23, 25-27 and from 28 bytes (3A.15, 3A.21); inline zero fill up to 10 stores, i.e. `_memset` at 39 and from 41
   bytes (3A.20); second static-local guard from the 33rd static (3A.33). Member-wise assignment is not merged
   into `rep movsd` above the cut-over (3A.15).
8. **3A.8:** a single `return r;` reached by `goto` is elided; a single `return r;` of a loop-scoped local is
   elided too, and the destructor on the loop's back edge is lost (v3a `n5`).
9. **3B.25 / 3B.26:** with `<new>` in scope the array forms call `??_U` / `??_V`, so `delete p` and `delete[] p`
   of a POD are not identical; the hand-written `p->~D(); operator delete(p)` reloads a member pointer.
10. **"Different IL, same code" (7.2) is context-dependent.** In a member function, and more so with an EH frame,
    early returns against a result variable, the early-return forms of `&&`, `break` against `&&` in a loop
    condition and a named intermediate pointer changed registers or block layout (1.1, 1.5, 1.9, 2.11).
11. **1.14:** only a `goto` that leaves a scope goes through a landing block. **3A.35:** `??__F` stubs are
    reverse-ordered only within one function. **3A.20:** `{0}` and `{}` differ for `char` arrays.

**Cross-check against Fallout3_1703.exe** (details where the rules stand: 1.11, 3A.15, 4.15, 5.13, 5.14, 5.18).
Functions read: 0x00487210, 0x00aa2d70, 0x00589560, 0x0075c0e0, 0x00ba7860, plus the initialiser 0x00c828a0 with
its stub 0x00d86640 and the outlier 0x0049e490. Their prologs, state numbering, state-store encodings, funclet
shapes and try/catch state layout are what rules 5.7 and 5.11-5.14 predict. Whole-image counts: 8,042 EH functions;
all 91 with a try block have an ebp frame; 27 of the 7,951 without one also do (unexplained); the image is laid
out `.text`, funclets and handlers, `??__E`, `??__F` as rule 4.15 says; no jump table has fewer than 4 entries
(342 found); no `rep movsd` has a constant count below 7 dwords.

**Not verified, or weak.**
- The Fallout 3 check is of code shape against the rules. No Fallout 3 function was recompiled from source here,
  so it cannot show that a rule's *source-level* claim (which spelling produced the shape) is right.
- The three contexts are small functions. "Held in all three contexts" does not extend to large functions, where
  register pressure differs; item 10 above shows the direction in which such claims fail.
- 5.14's 27 unwind-only functions with an ebp frame are unexplained. The `[I]` parts added by this pass: which
  cycle edge 4.13 ignores, that `??__F` stubs carry the dynamic-initialiser flag, the wording of 2.4's extra rows,
  and the identification of the allocation call in 0x00487210.
- Thresholds 3A.15 and 3A.20 were swept on `char` arrays and `int`-field structs with default alignment; structs
  with other member layouts, `/Oi-`, `/O1` and volatile or overlapping operands were not tried.
- `/OPT:REF` in 4.15 is the linker default without `/DEBUG`; how Fallout 3 was linked is not known, and `/OPT:ICF`
  (which the non-monotonic handler order in the first `.text` region suggests) was not examined.

## Addendum: the hidden "was it constructed" flag (probe work/scratch/ctorflag/f.cpp)

A class temporary with a destructor built in one arm of `&&`, `||` or `?:` gets a hidden flag local, set to 0 at
the start and tested at the end of the full expression to decide whether to destroy it. When the optimiser later
proves that arm unreachable it deletes the arm and every test of the flag, but keeps the zeroing store: the code
keeps `mov DWORD PTR $Tnnn[esp+..], 0` (a `$T` slot, which costs `push ecx` for the frame) and nothing else.
`if (p != 0 && S("x").c_str()) {}` after `p = 0;`, and `if (always_true() || S("x").c_str()) {}` with an inline
`always_true`, both leave exactly that store, on 14.00, 13.10 and 15.00; with a condition the optimiser cannot fold
the whole EH frame appears instead. **[C]** Found by the Mount & Blade project (PartyScreen::init_screen
0x502cc0, matched with this construct): an unexplained store of 0 to a stack slot that no code reads, with no
EH frame, is this flag. The bytes cannot say which statement the original had; a compiled-out debug or logging
call taking a string temporary is the likely shape.
