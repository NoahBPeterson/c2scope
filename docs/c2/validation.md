# Validation of the c2 regalloc and frame specs (adversarial probes)

This doc tests the rules in [regalloc.md](regalloc.md), [regalloc_cost.md](regalloc_cost.md) and
[frame.md](frame.md) against new probe functions, chosen so that each rule makes a specific prediction the
authors did not test: ties, multi-def webs, nested ifs, loops, mixed sizes, scopes, and K&R sort ties.
Each probe's prediction was written into its source comment before the compile.

- Compiler: `uv run tools/msvc.py /nologo /c /O2 /FAsc` (cl 14.00.50727.762); `/O1` and `/Oy-` where noted.
- Traces: `tools/c2trace.py` (see [tracer.md](tracer.md)).
- Probe sources, listings and the two driver scripts are in `work/scratch/glass_val_p/`. `cc.sh name [args]`
  compiles; set `OPT=/O1 SUF=_o1` for /O1. `tr.sh file.c` logs, for each web, its id (+0x1c), prio (+0x0c),
  benefit (+0x40), defOrd (+0x44), nrefs (+0x24) and the chosen register, and logs every `MscGSMakeProtectedCopy`
  call.
- Hooks used:
  - `@ChoosePhysReg@12` 0x10b14b70: entry and exit.
  - `@ChoosePhysReg@12+0x279` 0x10b14de9: the per-register weight.
  - `@Spillcosts@12+0x1064` 0x10b11484: edi = P_B and ebp = w_B, before the `imul`.
  - `@MscGSMakeProtectedCopy@24` 0x10b9baa0.
  - `@Color@4` 0x10b0cf40.
- Tags: **CONFIRMED** means seen in a compile or a trace. **INFERRED** means a mechanism I propose to explain a
  result but did not read in the code.

## Verdicts at a glance

| # | Rule | Verdict |
|---|---|---|
| 1 | Call-crossing webs get esi, edi, ebx, ebp in priority order; ebp or ebx drop out of the pool | holds (it stops predicting once webs are split) |
| 2 | Priority = Σ cost·P_B·w_B − Σ live-through P_B·w_B | holds exactly; the recipe for counting P_B needs corrections |
| 3 | Tie-break by defOrd: within a block the earlier def wins; across blocks the later block wins | holds, including multi-def webs and a 4-way /O1 tie |
| 4 | ChoosePhysReg weights: own preferences, neighbour preferences, physreg kills | holds |
| 5 | Scratch tmpregs rotate eax, ecx, edx with one cursor per function at /O2; first fit at /O1 | holds |
| 6 | Slot base order: size ascending, then count descending, then first to reach the count; color 0 nearest the frame register | holds, but counts must be taken from the final listing |
| 7 | Slot sharing: join the newest color when E.size ≤ 2·S, where S has grown | holds; the summary line "a char never shares with an int" is wrong |
| 8 | Escaped locals: "only in that call's PAS, so sibling-block locals share" | holds with correction (refuted as stated for enclosing scopes) |
| 9 | Quicksort iff T > 128 (or GS), K&R partition, key 1000·uses/size | holds, including the unstable tie flip, in both ESP and EBP frames |
| 10 | GS shadowing: derivation pass, vulnerable sites, callee masks | holds with correction (marking is order-independent) |
| 11 | /O1 FPO abandoned past 4 frame operands (no locals) or 1 (with locals) | holds |

---

## 1. Call-crossing webs take esi, edi, ebx, ebp in priority order

Spec: regalloc.md 0.5 and 5. `_RegOrders[0]` is at 0x10ce5a08. The call kill is applied at 0x10b1137e. ebp
availability comes from `RegSimpleAssignBody` 0x10bda2a0, and ebx availability from `MdmWillUseEbxConservative`
0x10c58980.

| Probe (r1.c, r5.c) | Prediction | Actual | |
|---|---|---|---|
| R1a: 5 call-crossing webs, use counts 1..5 | e esi, d edi, c ebx, b ebp, a in memory | e esi, d edi, c ebx, b ebp, `_a$` slot | CONFIRMED |
| R5a: same body plus `_alloca` | EBP frame; pool esi, edi, ebx; a and b in memory | e esi, d edi, c ebx; a and b `[ebp]` | CONFIRMED |
| R5b: same body in `__try/__finally` | the same as R5a | the same | CONFIRMED |
| R5c: `__declspec(align(16))` local | ebp lost to `and esp,-16`; ebx kept? | `push ebp; mov ebp,esp; and esp,-16`; e esi, d edi, c ebx; a and b in esp-relative slots | CONFIRMED: an aligned frame takes ebp, not ebx |
| R5d: plus a `double` local | full pool | ebp, ebx, edi, esi in priority order | CONFIRMED |
| R1f: 6 call-crossing values (a, b, n, two loop counters) | not predicted by the recipe | b (prio 0x20, highest) is coloured esi, then split: it is stored to `_b$` before the loops and reloaded into esi after them. The inner counter (prio 0x10) takes esi inside. a edi, n ebx, outer counter (prio −4) ebp | CONFIRMED trace |

Verdict: **holds.** Practical caveat, CONFIRMED by R1f: with more webs than registers, the highest-priority web
is not guaranteed a register for its whole life. It can be coloured first and then split around a loop where
other webs need its register. The register you see at its later uses is then the one it was given, not the one
it holds inside the loop.

## 2. The priority formula

Spec: regalloc_cost.md 4.3. The pressure step is at 0x10b113cb..0x10b114e3, the weight at 0x10b1071b.

To test the formula on its own, I compared the traced prio with the formula fed the traced P_B and w_B. To test
the recipe, I also predicted P_B by hand from the source.

| Probe | Traced blocks (P, w) | Formula with traced P | Traced prio | My hand prediction |
|---|---|---|---|---|
| R4a (r4.c): `a` def + 3 uses in an if-arm; `b`, `c` def + 2 uses after the join | (4,1) (1,1) (3,1) | a = 2·4 + 6·1 = 14; b = c = 8 − 1 + 4·3 = 19 | a 0xe, b 0x13, c 0x13 | order b, c, a: **right** (CONFIRMED) |
| R4b: one more use of `a` in the arm | same | a = 16, b = 19 | a 0x10, b 0x13 | predicted a first: **wrong** (I assumed P3 = 2) |
| R4c: the arm is a do-while (w = 2) | (5,1) (0,1) (2,2) (0,1) (3,1) | a = 10 + 4·2·2 = 26; b = 10 − 2·2 + 12 = 18 | a 0x1a, b 0x12 | order a, b, c: **right** |
| R1d (r1.c): `a` used twice in a doubly nested if; `b` used twice after it | from the trace | a − b = 1 (b also loses P·w in the inner block it lives through) | a 9, b 8 | predicted b first: **wrong** (I forgot b's live-through penalty in the inner block) |
| R1e: `a` used once in a loop, `b` 3 times after it | | a 0x12 > n 0x10 > i 0xc > b 6 | as traced | a esi, b ebx: **right** |
| R1e at /O1 | P = 3, 2, 0, 2, 0, 1; w = 1 everywhere | all four webs 8 | 8, 8, 8, 8 | an exact 4-way tie (see 3) |

Verdict: **holds exactly** whenever P_B and w_B are known (6/6 traced cases, CONFIRMED). The hand recipe for P_B
needs three corrections; all three are CONFIRMED by trace:

- **A constant used twice anywhere is a web**, and it counts in P_B of every block it appears in. In R4a the 1
  in `g(1)` and in `return 1` makes P = 4 in the entry block and P = 3 in the exit block. Since P multiplies
  every reference in the block, sharing a constant like this is enough to flip an order.
- **A parameter read once through a memory operand** (`cmp [x],0`) is not counted.
- **A conditional-header block with no web references has P = 0.** A web living through it pays nothing there.
  In R4c the arm's entry block and the loop exit block both have P = 0.

So rank orders computed with "count the variables in the block" are fragile within about ±1 prio unit, which is
the size of a single live-through penalty. Trace when it matters.

## 3. The defOrd tie-break

Spec: regalloc.md 3.4; `RewriteAsGlobregs` 0x10b5ae50 (counter at 0x10b5b04c, store at 0x10b5b099);
`InsertInList` 0x10b11970 (`jg` 0x10b11988, `jae` 0x10b11992).

| Probe | Prediction | Actual |
|---|---|---|
| R1b: `if(x){a=g(1); b=g(2);} else {b=g(3); a=g(4);}`, then one use each. Equal prio, two defs per web | The last write wins and the else arm is later in the block list. b is defined first in the else arm, so b has the larger defOrd: **b esi, a edi** (source order would say a esi) | prio 0xe for both; defOrd b 0x11, a 0xd; b esi, a edi. CONFIRMED |
| R1c: the arms swapped | a esi, b edi | a esi, b edi. CONFIRMED |
| R1e at /O1: four webs at prio 8 | the earlier def in the entry block goes first | defOrd 0xe (n), 8 (a), 4 (b), 3 (counter) → esi, edi, ebx, esi. CONFIRMED |

Verdict: **holds**, including multi-def webs, where the def in the later block sets defOrd. Exact ties are common
at /O1, where w = 1 everywhere.

## 4. ChoosePhysReg weights and preferences

Spec: regalloc.md 4 and 3.3. The neighbour preference is added at 0x10b14c2c and the own preference subtracted at
0x10b14d59. The strict compare is the `jge` at 0x10b14df1. The physreg-def removal is at 0x10b10b8b.

| Probe (r2.c, no calls) | Prediction | Actual (weights traced at 0x10b14de9) |
|---|---|---|
| R2a: loop with s, t, i; `return t - s` | t has a pref for eax (`mov eax,t`). The first-coloured web is t's neighbour, so its eax weight is +1 and it takes ecx. t takes eax with weight −1 | web 8 (prio 0x65): eax weight 1, so it picks **ecx** (the counter i). Web 7 (t): eax weight −1, picks **eax**. CONFIRMED |
| R2b: `s += p[i] << k` | k ends up in ecx with no reload in the loop; s in eax | `mov ecx,_k$` once before the loop; s in eax. The first web (i) has **no ecx in its avail**: eax weight 2 (preferences of s), so it picks edx. CONFIRMED |
| R2c: `s += p[i] / d` | idiv defines eax and edx, so no loop-live web is in eax or edx | webs in ecx, esi, edi, ebx, ebp; s in esi, and then `mov eax,esi` at exit. CONFIRMED |

Verdict: **holds.** A detail worth knowing, CONFIRMED in R2b: a fixed-register use such as the shift count in cl
does not show up as a penalty weight. It removes ecx from the avail set of every web live across it (3.3 item 3),
and the copy source k gets the preference instead. The "+100·benefit for a neighbour's last register" term was
not exercised by any probe (**untested**).

## 5. Scratch-register rotation

Spec: regalloc.md 6. `RegAssign` is at 0x10bda7c0, the cursor reset at 0x10bda2f0, `CmdSetNextPos` at 0x10c23520.

| Probe (r3.c) | Prediction /O2 | Actual /O2 | /O1 |
|---|---|---|---|
| R3a: a copy before an `if`, one in the arm, three after | eax, ecx, edx, eax, ecx (the cursor survives block boundaries) | eax, ecx, edx, eax, ecx | eax ×5 |
| R3b: a copy, a loop doing `B[5]+=A[1]`, two copies after | eax; loop tmp ecx; then edx, eax | eax. A[1] is hoisted into a **globreg** (ecx) and n is a globreg (eax). After the loop: ecx, edx. In the tail-duplicated `n<=0` path: eax, ecx | eax only |
| R3c: the next function | it restarts at eax | eax, ecx | eax |
| R3d: if/else; the tail is duplicated into both arms | then: eax, ecx, edx; else continues the cursor | then: eax, ecx, edx; else: eax, ecx (consistent with continuing from edx) | eax |

Verdict: **holds** (CONFIRMED). The R3b miss came from my prediction, not from the rule: a loop-invariant load
becomes a Color globreg, not a tmpreg, so it does not advance the cursor.

## 6. Slot base order and slot placement

Spec: frame.md 0.3, 0.7, 4.8. `AddStackElement` is at 0x10be9190; offsets are assigned at
0x10beb36e..0x10beb6ef.

| Probe (f1.c, f3.c) | Prediction | Actual |
|---|---|---|
| F1a: `short s[2]; int a[2], b[2], e[3]` with variable indices | from **source** order: a reaches 2 uses before b, so the order is s, a, b, e | `_b$=-28, _a$=-20, _e$=-12`; s took param k's home (`_s$ = 8`). **Wrong from source order, right from the final listing**: the scheduler moved the `b[m]=` store and the `b` load ahead of the second `a` store, so b reaches 2 first |
| F3b: `B[4], C[4]` tied at 2 uses each | I predicted B, C (B is referenced first) | **C, B**. In the listing B is referenced first, but C reaches count 2 first (`_C$` at +0x57, `_B$` at +0x5b), so the moving-back rule puts C ahead. CONFIRMED |
| F2d: `char c1,c2` (conflicting) plus a sibling int | 3 slots: c1 color 0 (2 uses), c2 color 1, i color 2 | c1 +2, c2 +3, i +4..+7 from esp, 8-byte frame. CONFIRMED; the chars are byte-packed (align 1) |
| F3 with `/Oy-` | the order reverses: color 0 nearest ebp | F3b: C −16, B −32, D −64, A −128. CONFIRMED |

Verdict: **holds**, provided the counts and the first-to-reach order are read from the **final** listing in
layout order (frame.md 5 step 1 says this; source order is not a substitute). An element that is touched first
can still come second (F3b).

## 7. Slot sharing: the 2× rule against the grown size

Spec: frame.md 4.6, `colorElemental` 0x10beb760.

All locals below are in sibling scopes and escape once.

| Probe (f1.c) | Prediction | Actual |
|---|---|---|
| F2a: char, short, int, double, 16-byte struct | each joins the newest color and grows it (1 → 2 → 4 → 8 → 16): **one 16-byte slot for all five** | `sub esp,16`; all five at the same address. CONFIRMED |
| F2b: int, char, double | c alone (4 > 2·1); i and d share (8 ≤ 2·4) | c at +3, i and d at +4; 12 bytes. CONFIRMED |
| F2c: int, 12-byte struct, double | sorted i(4), d(8), s(12): the color grows to 8, then 12 ≤ 16, so all three share | all at +0; 12 bytes. CONFIRMED |

Verdict: **holds.** Correction to frame.md 0.4: "a char never shares with an int" is true only pairwise. A char
does share an int's slot when a short (or any size ≤ 2) has grown the color in between (F2a).

## 8. Escaped locals and liveness

Spec: frame.md 4.3/4.4 and the frame rule "a local whose address escapes and which is not referenced directly
afterwards appears only in that call's points-at set, so sibling-block locals share". All probes below use
`ext(&x)` only.

| Probe (f4.c to f7.c) | Shape | Prediction from the rule | Actual |
|---|---|---|---|
| F4a | function scope `int a,b; ext(&a); ext(&b);` | share | 2 slots (matches frame.md T5b) |
| F5e | outer `a` escapes, then an inner-scope `b` | 2 slots | 2 slots |
| **F5f** | outer `b`; inner `{int a; ext(&a);}` ends; **then** `ext(&b)` | share: b is used only after a is dead, and a is in no later PAS | **2 slots** |
| **F6i** | the same with b in a block scope `{ int b; {int a; ...} ext(&b); }` | share | **2 slots** |
| F6j, F6k | if/else arms; three sibling scopes | share | share (one slot). CONFIRMED |
| **F7a / F7b** | like F5f/F6i, but b's first event after the inner scope is a direct store `b = g(1);` | — | **share** (both at +4). CONFIRMED |
| F7c | sibling scopes, each stored directly then escaped | share | share |

Verdict: **holds with correction; the condensed rule is refuted for enclosing scopes.**

- The behaviour is CONFIRMED by listings. An escape-only local conflicts with every escaped local of a scope
  nested inside its own, even when it is first touched after the inner scope has ended (F5f, F6i).
- A direct whole-variable store between the two events removes the conflict (F7a, F7b).
- Proposed mechanism (INFERRED, not traced): a call's PAS holds every address-taken local whose scope encloses the
  call, not only the local whose address is passed. That makes b a may-def'd, live member at `ext(&a)`, so
  b ∈ LiveOnDef[a]. A direct store kills b (frame.md 4.3, the kill set), which removes it from the live set
  before that call.
- To confirm it, trace the PAS-definition events at 0x10bea457 on F5f against F7a.
- Matching rule: **only locals in disjoint (sibling) scopes, or ones separated by a killing direct store, can
  share a slot.**

## 9. Slot quicksort

Spec: frame.md 4.7: `ColorStackElements` 0x10beb341, `QSortColors` 0x10bebbb0, `StkColorCmp` 0x10bebb30.

The base order from the listings is C, B, D, A (see 6). The predicted columns below simulate K&R's partition by
hand on that base order.

| Probe (f3.c) | T | Keys 1000·uses/size | K&R result (predicted) | Actual, /O2 (esp up) | Actual, /Oy- (ebp down) |
|---|---|---|---|---|---|
| F3a | 144 | C 125, B 125, D 93, A 50 | **B, C, D, A**: the tie is flipped by the middle-pivot swap | B −144, C −128, D −112, A −80 | B −16, C −32, D −64, A −144 |
| F3b | 128 (not > 128) | A 156 would win if sorted | no sort: C, B, D, A | C, B, D, A | C, B, D, A |
| F3c | 132 | A 147, C 125, B 125, D 93 | A, B, C, D | A, B, C, D | A −68, B −84, C −100, D −132 |

Verdict: **holds** (CONFIRMED, 6/6 layouts). The threshold is strictly greater than 128. Equal keys come out in
whatever order the partition leaves them, which is not the base order (F3a), so ties must be simulated, not
assumed stable.

## 10. GS parameter shadowing

Spec: frame.md 3:
- `MscGSShadowPtrParams` 0x10b9b620 calls `MscGSVisitAllAppearancesFwd` 0x10b9bf70 once (0x10b9b6fc), then
  `MscGSPropagateVulnerable` 0x10b9ba60 (0x10b9b701).
- `MscGSCallArgIsVulnerable` 0x10b9c5f0.

A copy is detected by a `MscGSMakeProtectedCopy` call in the trace. When the copy stays in a register it does not
show in the listing as a `$GSCopy$` symbol.

| Probe (g1.c, g2.c; each has `char buf[16]` and `use(buf)`) | Prediction | Copy? |
|---|---|---|
| G1: `buf[*p & 15] = 1` (a value loaded through p indexes a store) | copy | yes. CONFIRMED |
| G1r: the same value indexes a load | none | none |
| G2: `gp = p` (p stored to a global as data) | none | none |
| G3: `*(char**)(buf+4) = p; use(buf);` | none (p is data, not an address) | **yes** |
| G3b: `use(buf); *(char**)(buf+4) = p;` | — | **none** |
| G3c: p stored into buf, buf never escapes, `buf[k&15] = 0` | none | **yes** |
| G4: loop `{ *q = 0; q = p; }`: the store precedes the taint in stream order | none (the spec says back edges are missed) | **yes** |
| G4b: `{ q = p; *q = 0; }` | copy | yes |
| G4c: loop `{ *r = 0; r = q; q = p; }`: two-step chain against stream order | none | **none**. CONFIRMED |
| G4d: the same chain in forward order | copy | yes |
| G5: same-TU `noinline struct R rd(char *a)` (hidden return pointer; only reads a) | none, if arg i maps to mask bit i with bit 0 = the return pointer | none. CONFIRMED |
| G6: `w2(p, q)`; w2 writes only through its 2nd formal | q copied, p not | q copied (`mov esi,_q$` at entry; p loaded late). CONFIRMED |
| G7 / G7b: `w3(int n, char *b)` writes `*b`; passed (1, p) / ((int)p, q) | p copied / q copied, p not | the same. CONFIRMED |

Verdict: **holds with correction.**

- **Vulnerability marking does not depend on order; derivation does.** Marking adds base keys to
  `_MscGSVulnerableSymKeysBv` whether or not they are tainted yet. The intersection `derived ∩ vulnerable` is
  taken after the pass, so a store through q that comes before `q = p` still counts (G4). Only a chain of
  derivations that runs against stream order is lost (G4c). frame.md 3.4's "a value defined later in the stream
  than its use (a loop back edge) is missed" holds only for chains of two or more steps. CONFIRMED by G4/G4c.
- **Taint flows into memory and back out through its address.** A store of p into buf puts buf in p's derived
  set: the destination's memory symbol is a derived destination (step 2). After that, `&buf` or a buf-based
  store address is derived. Passing buf to an unknown call (G3) or storing into buf with a computed address
  (G3c) then marks p vulnerable. The same code with the escape before the store (G3b) does not. That `&buf`
  (TK_DATAADDR) inherits buf's derivation is INFERRED from these three results.
- Practical consequence: storing a parameter pointer into a GS buffer and then using the buffer is enough to
  shadow the parameter, even though nothing is ever written through it.

## 11. /O1 FPO abandonment

Spec: frame.md 0.10, `WorthitFPO` 0x10c56260, called at 0x10be8bf8 only when `_Speed == 0`.

| Probe (o1.c) | Frame operands | Prediction /O1 | Actual /O1 | /O2 |
|---|---|---|---|---|
| O1d: `int v; ext(&v);` | 1, with a local | FPO | FPO | FPO |
| O1c: `int v; ext(&v); g(a);` | 2, with a local | EBP | EBP | FPO |
| O1e: `int v; ext(&v); ext(&v);` | 2, with a local (same local twice) | EBP | EBP (`leave`) | FPO |
| O1f: params a, b each pushed twice | 4, no locals | FPO | FPO | FPO |
| O1g: plus `g(c)` | 5, no locals | EBP | EBP | FPO |

Verdict: **holds** (CONFIRMED).

---

## Not tested, or open

- ChoosePhysReg's `+100·benefit` term for a neighbour's last free register, SpillOrDefer and deferral order,
  `BetterToDefer` re-queueing, the parameter-home undo at T ≥ 112, and the EBP adjustment were not exercised by
  any of these probes. They remain as the authors left them.
- The P_B accounting for immediates: whether a constant must be used two or more times in the function to become
  a web was not isolated, only seen in R4a and R1b (INFERRED).
- The PAS mechanism behind section 8 (INFERRED; trace 0x10bea457 to confirm).
