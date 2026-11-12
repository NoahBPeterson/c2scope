# From an abstract edit to C++: the catalogue and `tools/realize.py`

The models answer in abstract edits: `rasim need` / `rasim want` say "one more reference to VAR in block B"
(`ref:VAR:B`), `symkeys levers` says "16 more frame keys before V" (`add:frame@V:16`). This page is the inverse
table: for each kind of edit, the C++ constructs that realise it, what else each one moves, and what it costs in
code. `tools/realize.py` applies the table to a function: it places every catalogued construct at the source
lines of the block the edit names, compiles and traces each variant, and says whether the edit happened.

Back ends: cl 14.00.50727.762 (default), 13.10.3077, 13.10.3052, 15.00.30729.1. **[C]** = measured here from two
traced compiles (probe file named); **[I]** = inferred. Probe sources: `work/scratch/realize/p1.cpp` .. `p4.cpp`
(recorded outputs `work/scratch/realize/out/<probe>_<build>.txt`), regression file `tests/data/realize.cpp`.
Every row below was measured on 14.00 and repeated on 13.10.3077 and 15.00 with the same result unless a build is
named; `tests/data/realize.cpp` also on 13.10.3052.

## 0. Summary for a decompiler

1. **A value that is never modified cannot get a no-code reference.** Eighteen do-nothing uses (`(void)V;`,
   `if (V) { }`, `__assume`, `__noop`, empty inline helpers by value and by reference, dead reads, dead compares,
   aliases, reference bindings without a use, dead stores to a local, a dead ternary, an empty if/else) leave
   nothing at Color in seven contexts (free function, loop with a call, loop without, `this` after a loop, EH
   frame, inside an inline helper, inside an inline helper of an EH function) on all three builds. So
   `ref:this:B` has no code-neutral spelling; the cheapest real one is a store (one instruction). **[C: p1]**
2. **A value that is modified later can.** `T k = V;` written before V's next modification, with the later
   uses spelled `k`, reaches Color as a copy between two candidates and is coalesced away: the code is identical,
   and at allocation V has two more references in the block of the copy, that block has one more candidate, and
   V's range ends there. The block is the one the statement is in, so it can be chosen. The same thing happens
   when a loop is written in an inline helper that modifies its by-value parameter, and with
   `for (T k = V; ...)`. **[C: p2 cpmod/cpptr, p3 cpl/cpf/wk/lp, tests/data/realize.cpp]**
3. **A statement at the head of both arms of an if/else counts in the arms**, not in the block of the test:
   head merging runs after Color (il_fg.md rule 12). Same instructions; the references move. **[C: p2 hdup, p3 hds]**
4. **`extend:V:B` on an address-taken local is a callee's body**, per parameter (callee.md). `realize.py` writes
   the stubs from the declarations in the unit; which pattern is true has to come from the original's callee
   (`calleesum.py`). It is code-neutral when the uses after the call read memory anyway. **[C: Fallout 3
   0x00a89680, 0x006dc990, 0x004da900 below]**
5. **One more candidate in a block (`P:B:+1`, `var:`)** without code: the copy of rule 2 (always), a narrow
   constant respelled to share a 32-bit constant (immcand.md A2), in a function with an EH frame a reference
   bound to `&p->m` (frontend.md). A named local or `hs_cp(x)` for a single-use value also adds a candidate with
   the same instructions, but it replaces a scratch pick, so that value's register and the later rotation move.
6. **Frame symbol keys**: +1 per unused local with an initialiser (also in a nested scope); +1 per expansion for
   an inline helper that names its result (`float r = E; return r;`); −1 for an unnamed temporary instead of a
   named struct local; 0 for `(void)c;`, an unused local without initialiser and assignment instead of
   initialisation. All with identical code in the probe on 14.00 and 15.00 (13.10: the unnamed temporary
   changed code). **[C: p4]** A reference or pointer local is +1 but changed the x87 code on 14.00 and 15.00.
   There is no construct for the optimiser's hash-class keys (2.6).
7. **What has no code-neutral realisation here**: a reference to an unmodified value (rule 1), a register
   preference, an interference or a copy partner on their own (they come only as side effects of the constructs
   above, or with code: tracer.md's pairs table), and `unref:` of a value whose reads the original's code has.

## 1. How a construct is measured

`realize.py pairs probes.cpp -- FLAGS` compiles the file once with the `regs` trace and compares every function
named `X__base` with each `X__<variant>`:

```text
## cpl: 7 blocks, 38 instructions; P per block 1:4 2:2 3:2 4:1 5:4 6:0 7:2
  c2    code 0 (bag 0, regs 0, +0 instr)  var:k:1:2,5:6 ref:n:1:+2 drop:n:5 P:1:+1 interf:a:k interf:k:p interf:k:s copy:k:n
  c3    code 0 (bag 0, regs 0, +0 instr)  var:k:3:2,5:6 ref:n:3:+2 drop:n:5 P:3:+1 interf:k:p interf:k:s copy:k:n
```

The list after the counts is the difference of the two traces in the models' own vocabulary, read from the
first integer Spillcosts pass (blocks numbered as `regs --why` numbers them) and from what ChoosePhysReg saw:

| Item | Meaning |
|---|---|
| `ref:V:B:+2` / `ref:V:B:-2` | V's reference cost in block B changed (2 per reference at /O2) |
| `unref:V:B` | V was referenced in B and is only live through it now |
| `extend:V:B` / `drop:V:B` | V is referenced in B and was absent there / the reverse |
| `var:NAME:B:c,..` / `gone:NAME:B,..` | a new named candidate with its blocks and costs / one that no longer exists |
| `imm+:imm=K:B` / `imm-:imm=K:B` | a constant candidate appeared / disappeared (immcand.md) |
| `tmp:B:+n` | n more anonymous candidates (compiler temporaries) referenced in B |
| `P:B:+n` | the block's pressure changed |
| `blocks:-a+b` | the block list changed; blocks are then aligned on weight and source lines (`nK` = a new block) |
| `interf:A:B`, `copy:A:B`, `pref:A:REG:+n` | relations among named values: neighbours and copy partners as ChoosePhysReg listed them (symmetric closure), register preferences Spillcosts recorded |

Code cost, three numbers: `code` = aligned diff of the two listings with registers masked and operands kept
(labels, `$T` names, block-scoped suffixes and the printed form of narrow immediates normalised); `bag` = the
same as a multiset with frame displacements dropped, so 0 means "the same instructions, possibly in another order";
`regs` = the diff with registers. `code 0` is code-neutral in the strict sense. `bag 0` with `code > 0` is what a
construct looks like when it changed registers and the prolog or the scheduler then ordered things differently;
against an original only the regdiff decides. `--keys` adds the symbol-key difference (allocations per pool
class, named keys that moved, operand orders that flipped) from a second compile with the `symkeys` trace.

`realize.py measure base.cpp variant.cpp --func F` does the same for two files.

## 2. The catalogue

Columns: *realises* = the abstract edit, exactly (`=`) or with the side effects listed; *code* on 14.00 (other
builds the same unless said); *limits*; probe. "B" is the block the statement is in.

### 2.1 References: `ref:V:B`, `unref:V:B`

| Construct | Realises | Side effects | Code | Limits | Probe |
|---|---|---|---|---|---|
| `T k = V;` before V's next modification, later uses spelled `k` (construct `copy`) | `ref:V:B:+2` | `P:B:+1` (k is a new candidate defined in B), `drop:V:` every later block, `var:k:` with V's later references, `copy:k:V`, k interferes with what V met later | 0 (regs 0) | V must be modified after the copy, else the copy is propagated away (`cp2`: nothing; `cpl c1`: only the name changes). Not for `this`. Copying a stack parameter before a call that precedes its first use moves the parameter's load (`cpmod early`: 2 instructions reordered). Not inside a loop | p2 cpmod, cpptr; p3 cpl c2/c3, cpf; tests |
| `for (T k = V; ..; k = ..)` instead of stepping V | the same | the same | 0 | | p2 cpptr for, p3 lp forc |
| the loop in an inline helper that modifies its by-value parameter (`walk(p)`) | the same (the copy is the helper's parameter: an anonymous temporary) | the same, `tmp:` instead of `var:` | 0 for a list walk; a clamp helper (`if (v < 0) v = 0;`) keeps the instructions and reorders two (`bag 0`, code 2) | | p3 wk, clh |
| statement before `if/else` written at the head of both arms (`headdup`) | `ref:V:arm:+2` in each arm, `drop:V:B` or `ref:V:B:-2` in the block of the test | P of the three blocks follows; the registers moved in the probes (the arms' P rose) | bag 0 (the read is merged back above the branch after Color); with the registers the prolog order changed (code 4) | the statement must not feed the condition; both copies must get the same registers to merge | p2 hdup, p3 hds; tests |
| the reverse (`headhoist`) | the reverse | | | | |
| `g = V;` (a store; `rz_keep(V)` in the tool) | `ref:V:B:+2` = | none (a preference can go: `pref:this:ecx:-1` in 0x00a89680) | +1 instruction | always available: it shows what the reference would do | p1 store |
| 18 do-nothing uses of V | nothing | | 0 | 7 contexts, 3 builds. `__assume(p != 0)` inside an inline helper is not harmless: it changes the helper's code (p1 inl/ehinl) | p1 |
| `int x = V->m;` used by both arms, written once before the test instead (`hcmp`) | `ref:V:B` moves, a named value appears | | code changes (a reload) | not neutral | p2 hcmp |
| `int* pc = &V->m;` before a loop with a call, the loop reading `*pc` | `extend:V:preheader`, `P:+1` there | block list changes | bag 0, code 6 | not neutral | p2 lea |
| re-reading `V->m` instead of keeping it in a local across a call | V takes the local's place | | code changes | | p2 rd2 |

`unref:V:B` and `drop:V:B` are the same rows read backwards: remove a copy (`uncopy`: `T k = V;` deleted, `k`
spelled `V`), hoist a common head statement. A reference that the original's instructions need cannot be removed.

### 2.2 Ranges: `extend:V:B`, `drop:V:B`

| Construct | Realises | Side effects | Code | Limits | Probe |
|---|---|---|---|---|---|
| a body for a same-unit callee that only **reads** through the parameter receiving `&V` (construct `callee`) | `extend:V:` from the call back to V's definition: one web across the call, no reload after it | V interferes with everything live across the call | 0 when V's uses after the call are memory operands (0x00a89680: regdiff 16 → 0); otherwise V moves into a register (probe: −5 instructions, a callee-saved register more) | the callee must really be in the unit and really only read (callee.md; `calleesum.py` reads it from the executable). A body for a callee of another file breaks a match | p2 ext; tests; §4 |
| bodies for **all** callees that receive `&V`, each writing through it but not keeping the pointer | V does not escape: loads before and after later external calls become one value | several values merge | 0 in 0x004da900 (regdiff 31 → 0) | every callee that gets the address needs its body; one alone changes nothing | §4 |
| a real use of V at B | `extend:V:B` = | | the use | | tracer.md int_ext, iv_ext |
| the copy of 2.1 | `drop:V:` after the copy | see 2.1 | 0 | | |

### 2.3 Candidates: `var:NAME:B`, `P:B:+1`, `P:B:-1`

| Construct | Realises | Side effects | Code | Limits | Probe |
|---|---|---|---|---|---|
| the copy of 2.1 | `P:B:+1` | `ref:V:B:+2`; if V was only live through B, P rises by 2 | 0 | as 2.1 | p3 cpl |
| `char`/`short` store of −1.. (128.., 32768..) through the other signedness (immcand.md R2/A2; `const`) | `P:B:-1` or `+1` = (one constant candidate) | none | 0 while the candidate is not coloured | needs a narrow store and another use of the 32-bit value | p2 imm, imm2; tests |
| in a function with an EH frame: `T& r = p->m; f(&r);` or `T* r = &p->m; f(r);`, in the function or in an inline helper (`helperbind`, `wrap`) | `var:r:B`, `P:B:+1` | r interferes with the values live there | bag 0. In the probe the address was a rotation pick, so its register changed (ecx → eax, code 2); where the address already had a copy hint it is pick-neutral (0x008e7d40, frontend.md) | nothing without an EH frame (p2 neb) | p2 ehb, ehs, neb |
| a named local for a single-use value (`int t = p->a; g(t);`), or `g(hs_cp(p->a))` | `var:t:B`, `P:B:+1` | the value is coloured by Color instead of taking a scratch pick: a scratch `drop:N` | bag 0, code 2 (its register changed, one `mov` moved) | with and without an EH frame | p2 nm, nmeh; p3 hcp |
| `delete p; p = 0;` guarded or not (`guard`) | the reset store's references change block | | same code when the delete runs a destructor | frontend.md | work/scratch/guard |
| for-init hoisted over statements | the definition of `i` and the reference to 0 change block | | 0 only when `i` is in a register; in the probe two instructions changed order (bag 0) | immcand.md | p2 fori |
| ternary as an argument instead of a named result | the named value becomes an anonymous one | none (no P change) | 0 | | p2 tern |
| a named `bool` for a condition; `x = 0; x = E;`; split declaration; `&&` as nested ifs; `while` / `for` / `if + do-while` | nothing | | 0 | | p2 boolm, dinit, nest; p3 lp |
| a second variable instead of reusing one after its last use | nothing but the name (one candidate per lifetime either way) | | 0, same registers | | p2 web |

### 2.4 Order, interference, preference, copy partners

`before:A:B` / `after:A:B` are priority edits: ask `rasim need SNAP 'A>B'` for the `ref:` / `unref:` form and take
it to 2.1 or 2.3. For the others no construct was found that realises one of them alone without code:

| Edit | What realises it | Code |
|---|---|---|
| `copy:A:B` | the copy of 2.1 makes k and V partners (and it is what makes k take V's register) | 0, but it is a side effect |
| `interf:A:B` / `nointerf:A:B` | `extend:` / `drop:` of one of them (2.2); a callee body | as 2.2 |
| `pref:A:REG` | a copy with a fixed register: return value, fastcall or thiscall argument, call result | changes code (tracer.md pairs fc_pref, h_ret) |
| `noreg:A:REG` / `canreg:A:REG` | A lives (no longer lives) across a call, shift, division | changes code, or 2.2 |

### 2.5 Scratch picks: `rasim scratch SSNAP drop:N` / `ins:N`

`drop:N`: give the value of pick N a name (a named local, `hs_cp`, in EH functions a by-value getter or a bound
reference): it becomes a Color variable, the pick disappears, the instructions stay (bag 0). `ins:N`: the reverse,
write the named single-use value back into its expression. Both also change P of the block (2.3). **[C: p2 nm,
nmeh, ehb; p3 hcp]**

### 2.6 Symbol keys: `add:frame@V:n`, `sub:frame@V:n`

Measured with `pairs --keys` on p4 (an x87 function with two inline helpers). 14.00 and 15.00 give the same
numbers and the same code cost in every row; 13.10 gives the same frame numbers for the rows marked *, and its
code cost differs where said (its operand sort sees memory leaves, symkeys.md section 7).

| Construct | frame | temp | Code | Probe |
|---|---|---|---|---|
| unused local with an initialiser `int u = 0;` * | +1 each | 0 | 0 | ky dead1, dead3 |
| the same in a nested scope `{ int u = 0; }` * | +1 | 0 | 0 | ky scope |
| unused local without initialiser *; `(void)c;` before c's first use *; `V d; d = f();` instead of `V d = f();` * | 0, no key moves | 0 | 0 | ky dead0, void, voidd, assign |
| inline helper that names its result: `float r = E; return r;` * | +1 per expansion | 0 | 0 | ky dotl |
| reference or pointer local `const V& rc = *c;` * | +1 | 0 | 14.00, 15.00: **changed** (8 instructions: an operand order flips); 13.10: 0 | ky ref, ptr |
| unnamed temporary instead of a named struct local: `dot(sub(a, b), c)` * | −1 | 0 | 14.00, 15.00: 0; 13.10: changed (12) | ky argtmp |
| result used directly instead of through a named `float t` * | −1 | 0 | 14.00, 15.00: changed (6); 13.10: 0 | ky noname |
| `hs_id(expr)` / `hs_cp(expr)` around a float expression | +1 / +2 | +2 | 0 | ky hsid, hscp |
| three named products in the helper | +3 per expansion | +6 (13.10: 0) | changed (16: evaluation order; 13.10: the same instructions reordered) | ky dot3 |
| a struct copied to a local, fields read into locals | +4, +3 | +1, −5 | changed | ky copyc, fields |
| constructing in place `W d(a, b, c);` instead of through a helper that returns `W(a, b, c)` | −3 (13.10: −2) | −4 | 0 | kw ctor, ctmp |

**Negative results on Fallout 3 0x00806ab0** (measured by the key-wall agent, `work/scratch/f3/keywall/RESULTS.md`,
`probe/pec.cpp`; cl 14.00; not re-run here):

| Construct | Keys | Result there |
|---|---|---|
| dead expression statements `(void)(a + b);` | no hash-class temp (removed before value numbering), no frame key | nothing moves (regdiff 45 to 48 against 45) |
| a by-value struct operand in the cross helper | +6 frame per expansion (both operands by value, free function: +12) | changes the code from the first products on |
| `P v = f();` instead of `P v; v = f();` for the cross results | | changes the code (177 to 265) |
| the cross returned through a constructor (`return P(..)`) instead of a named result | −3 frame per expansion; a named copy before the return −2 | same code as the named form at one site, worse at another |
| three float locals in the helper; `const P& a = *this;` | +3; +1 per expansion | no form gives exactly +8 per expansion with the code kept |

**Hash class** (`add:hash@V:n`): the optimiser's value-numbering temporaries (`$o..`, made by `HshInsertEntry`)
have their own key class, and for an operand reached through one only `key & 3` enters the sort weight. No
construct is catalogued that adds one without code. The one known source is code the compiler itself generates
at a point: the constructor loop (`??_H`) of a class array declared there made the 3 temps the matching draft
has. `realize.py` accepts the edit and says so; it generates nothing for it.

**An anchor inside an inline expansion** (`add:frame@_r@Cls::Helper#2:n`) has no statement of its own. The tool
offers two different placements and names them: n locals at the call site of the k-th `Helper(` (reader-phase
keys: they are allocated before every expansion, so they also move what lies between the site and the anchor),
and locals in the helper's body (every expansion gets them, so expansion #k sees k times the count; offered as
n / k when that divides, and as n). A copy of the helper used at one site only, which is what the padded draft
does, has to be written by hand.

Two things from Fallout 3 0x00806ab0 (§4) that a key edit has to respect: the keys must arrive **at one point**
(the same total spread over eight expansions fixed the target site and flipped two others), and a temp-class
side effect can move a whole block of 32.

### 2.7 x87 live ranges ("V must not be one range across the calls")

x87.md §10 is the catalogue: assigning the value again per row, a variable per row or a block-scoped variable
gives one web per row (different code); storage whose address escapes cuts the range at every call (same size,
other frame); a parameter, a named constant or an inline spelling are one web. `rwsweep --families recompute`
generates the first form. Nothing was added here; Spore's RotateSH needs several values changed together
(x87.md §10.1).

## 3. The tool

```sh
# one edit, in the syntax rasim prints
uv run tools/realize.py edit tu.cpp --func F ref:n:3 [extend:p0:1 ..] [--want-regs this=esi,center=ebp] \
    [--asm-vs orig.s] [--constructs copy,callee] [--list] -- /TP /MT /GS /EHsc /Gy
# the pipeline: regmap against the original, rasim want, then the rewrites
uv run tools/realize.py --from-rasim auto tu.cpp --func F --asm-vs orig.s -- /TP /MT /GS /EHsc /Gy
# or with an answer list you already have
rasim need snap.json reg:this=esi reg:center=ebp | uv run tools/realize.py --from-rasim - tu.cpp --func F ...
# what did my own change do, in abstract edits
uv run tools/realize.py measure base.cpp variant.cpp --func F [--keys] -- FLAGS
uv run tools/realize.py pairs probes.cpp [--keys] [--diff] -- FLAGS
```

**`edit`.** The base is compiled with the `regs` trace. Block B is mapped to source lines through the trace's
line numbers (the `src` column of `regs --why`), and to the statement boundaries on those lines. For the edit's
kind the constructs of section 2 are generated there (`--list` prints them without compiling): `copy`,
`uncopy`, `headdup`, `headhoist`, `callee`, `store`, and from `rwsweep` the `const`, `guard` and `wrap` families
restricted to those lines. Rewrites add no line, so line numbers stay comparable. Every variant is compiled and
traced once (`--jobs`), compared with the base as in section 1, and gets a verdict:

```text
## ref:n:3   (block 3 = source lines +6, P=2)
  1 candidates, 1 compiled; 1 realise the edit, 1 of them with the same instructions
  yes   copy      code 0 bag 0 regs 0  int rz_c = n; at line +6, 3 later uses renamed
        also: drop:n:5 var:rz_c:3:2,5:6 interf:p:rz_c interf:rz_c:s copy:n:rz_c
        registers: rz_c:-->esi
        work/realize/realize/e0_000/e0_000.cpp
```

`exact` = the edit and nothing else (the block's own P change is implied), `yes` = the edit and the side effects
on the `also:` line, `no` = it did something else (`did:`). With `--want-regs` each row says whether those
registers came out; with `--asm-vs` it carries the regdiff against the original. Several edits are realised one
by one and then together. `add:frame@V:n` is realised at V's first reference (the line comes from a `symkeys`
trace) by n unused initialised locals, plain or in a nested scope, and by n call arguments through `hs_cp`; the
verdict is `exact` when symkeys' replay of the base plus the edit gives every key and operand order of the
variant.

**`--from-rasim`.** Reads `rasim need` / `rasim want` output (the wanted registers from its header lines, the
answers in the order printed), or with `auto` makes it: `regmap --all` against `--asm-vs`, then `rasim want`.
Three stages, stopping at the first variant that matches (regdiff 0 with `--asm-vs`; otherwise the wanted
registers with `bag 0`):

1. each answer made only of source-level edits, each edit through its code-neutral constructs (a store is tried
   only with `--with-store`: it costs an instruction and can only confirm the model);
2. every structural code-neutral construct that touches a block any answer names (`guard`, `const`, `headdup`,
   `headhoist`, `uncopy`, `copy` of every variable, and `helperbind`: `&p->m` in the inline helpers through a
   bound reference), singly; then those that changed the allocation's inputs without changing an instruction
   together, per family and all at once (priorities add);
3. stub bodies for every bodiless callee that receives the address of a local: per parameter "only reads", for
   all callees "reads and writes, keeps nothing", and "reads only everywhere".

A stub found in stage 3 is a hypothesis about the original's callee: check it with `calleesum.py EXE VA` or
replace it by the decompiled body.

Files: variants and traces in `work/realize/<tu>[@build]/` (or `--keep DIR`), with `base.json` (the snapshot),
`regmap.txt` and `answers.txt` in pipeline mode. Limits of the rewriter: it works on the target's own body with
`rwsweep`'s tokenizer (a function template or `template <> R f<T>(..)` specialisation is found by its `??$name@` decorated name; macros that hide statements are not seen); `copy` refuses loops and
address-of-uninitialised cases by simple rules, a wrong rewrite fails to compile or shows up as changed code.

## 4. Validation

### 4.1 The three dead ends (local copies only)

**Fallout 3 0x00a89680** (`work/scratch/f3/rw/e2.cpp`; the original has `this` esi, `center` ebp, the draft the
reverse; regdiff 16). The edit nobody could spell was `ref:this:12`, "a reference to `this` after the loop with
no code". Result: **there is none** (section 0 rule 1: `this` is never modified, and every dead use is gone
before Color). What the tool shows instead, `realize.py edit e2.cpp --func FUN_00e0a0b0 ref:this:12 extend:p0:1
--asm-vs a89680_orig.s`:

| Edit | Construct | Verdict | Registers | regdiff |
|---|---|---|---|---|
| `ref:this:12` | `rz_keep(this);` at line +38 (a store) | yes (`pref:this:ecx:-1` also) | this esi, center ebp | 2 (the store) |
| `ref:this:1`, `ref:this:4` | the same store at lines +2 / +24 | exact | this esi, center ebp | 3, 4 |
| `extend:p0:1` | body for `FUN_00e0acb0` that only reads through `p0` | yes | this esi, center ebp | **0** |
| `extend:p0:1` | body that reads and writes every pointee | no (nothing changes: it is what the `noalias` declaration says) | unchanged | 16 |
| `extend:p0:1` | body that reads every pointee, writes none | yes, and four more ranges grow | wrong | 74 |

So each of the model's `ref:this:B` answers is right (a real reference there gives the registers, at the price
of its instruction), and the only code-neutral answer on the list is the `extend:`, which the stub generator
realises. The pipeline (`--from-rasim auto`) finds it by itself: 55 answers read, the `ref:` / `unref:` answers
have no code-neutral construct here and are skipped, the first answer that has one is `extend:p0:9`, and its
first stub is the match: `FOUND after 1 answers, 3 compiles ... body for FUN_00e0acb0: only reads through p0`,
regdiff 0 (`work/scratch/realize/out/pipe_a89680.txt`). **[C]**

**Fallout 3 0x00806ab0** (`work/scratch/symkeys/f3/806ab0/ba0.cpp`, regdiff 28; `best1.cpp`, padded with 16 dead
locals in a copy of `EdgeCross`, regdiff 17 = the jump table). The edit: 16 more frame keys between the first
reference of `center` and the read of the second `EdgeCross` expansion. Work files: `work/scratch/realize/f806/`
(`gen.py`, `gen2.py`, `run.sh`, `*_results.txt`; untraced compiles scored by regdiff).

- The stream of the unpadded draft (from `ba0.keys.json`): before `EdgeCross#2` the inliner makes 49 expansions,
  among them all 8 of `ReworkPoint::Dot`, 4 of 8 `operator-`, 12 of 29 constructors, 4 of 12 `ReworkAbs`.
- **A natural construct that puts 16 keys there exists: `Dot` with two extra frame symbols**, which is 2 × 8
  expansions. `float f = x*o.x + y*o.y + z*o.z; float r = f; return r;` (`dC`), a block-scoped second local
  (`dR2`) and a result local plus one unused initialised `int` (`dU2`) all give **regdiff 25**: the dead-end
  site (+0xa9b..+0xacf, the x products of `fC01Dot` / `fC01Vel`) comes out as in the original, the code has
  best1's 4406 instructions, and two other operand orders flip (+0xafe in line 1044, +0x1bd6 in line 1487).
- Why it is not a match: the padding arrives in steps of 2 at eight places, so every symbol allocated between
  the first and the last `Dot` moves by 2, 4, .. 14, and member operands hash by `key(pointer) & 7` (symkeys.md
  rule 2). The padded draft moves nothing before `EdgeCross#2`. **[I for the cause; C for the three results]**
- Tried without success: one, three, four and eight locals in `Dot` (56, 36, 90, 90), `Dot` taking its argument
  by value (146 and worse), 35 spellings of `EdgeCross` or of a copy used at the second site only (by-value
  parameter, copies of `*this` / `o`, reference locals, six float locals, products named first, result copied:
  the closest are identical to the draft, 28; the rest 63 and worse), 16 combinations of result locals in `Dot`,
  `ReworkAbs`, `FUN_00475250` and two spellings of `operator-` (35 and worse).
- So: **no natural match**; the nearest natural spelling is 3 better than the draft and 8 worse than the padding.
  What a match needs is now narrower: the 16 keys at a single point (one expansion or one statement), or in
  multiples of 8 at each point. Not searched: constructs in the switch cases (reader phase), and rewriting the
  draft's nine edge blocks with one `Dot` / `Cross` pair as the original presumably had (the draft uses
  `Dot`, `FUN_00475250`, `ProjectionR2`, `Projection16` for the same product).

**Spore RotateSH** (`work/scratch/x87life/spore/`): not re-run. The edit ("the coefficient must not be one live
range across the band") has the realisations of section 2.7, each already measured on the function in x87.md
§10.1 with the result that it has to change together with the float temporaries and the shared square roots.
This work added no x87 construct, and a traced compile of that unit on cl 15.00 takes longer than was available.

### 4.2 The pipeline on functions that are already matched

`realize.py --from-rasim auto DRAFT --func F --asm-vs orig.s`: the unmatched draft and the original's listing in,
nothing else. "Found" = a variant with regdiff 0 against the original.

| Function | Draft | Base regdiff | Model's answers | Result | Compiles |
|---|---|---|---|---|---|
| Fallout 3 0x00a89680 (14.00) | `work/scratch/f3/rw/e2.cpp` | 16 | 55 (`ref:this:B`, `unref:center:B`, `extend:p0:B`, ..) | **found**, stage 1, first answer with a construct (`extend:p0:9`): body for `FUN_00e0acb0` that only reads through `p0`; regdiff 0 | 3 |
| Fallout 3 0x006dc990 (14.00) | `work/scratch/f3/callee/006dc990/nobody.cpp` | 27 | none at source level (order, interference and preference answers only) | **found**, stage 3: any body for the list's `FUN_0049fac0` (the three stubs all give regdiff 0) | 3 |
| Fallout 3 0x004da900 (14.00), draft with the merged early return | `.../004da900/s_none.cpp` | 31 | none at source level | the stage-3 variant "bodies for `FUN_005c8b00`, `FUN_008e9650`: each reads and writes every pointee, keeps none" has **regdiff 0** (`work/realize/pipe_4da900/cb_006/cb_006.cpp`, checked with a plain compile); the single-callee stubs stay at 31. The pipeline run itself was stopped before it printed: the two traced two-stub compiles of that 26,000-line unit had not finished after 30 minutes on the loaded machine | 8 generated |
| the same function, earlier draft (two separate early returns) | `.../004da900/draft.cpp` | 49 | none at source level | **not found** (49): only one of the two callees is recognised in this spelling, and the match also needs the merged return, which is not an allocation edit | 3 |
| Mount & Blade `Game::load_game_data` (13.10), the four resets written unguarded | `work/scratch/realize/mb/lgd_nat.cpp` (made from the matched copy) | 106 | `unref:this:3`; order answers `after:t969:t1644` (gap 3), ..: turned into blocks by `rasim need` | **not found**: 106 → 46 with the four guards (see below) | 6 (guard family only) |

Mount & Blade in detail (`work/scratch/realize/out/pipe_lgd.txt`, local compiles, `C2TRACE_REMOTE=0 CCACHE=0`,
guard family only): the model's cheapest answer is the order `after:t969:t1644` with a gap of 3, which is the
47-against-44 pair frontend.md describes. Stage 2 generates the four `guard` rewrites (they need the cast-aware
pattern, `delete[] (T*)p; p = 0;`). Singly: `factions` 106 → 56, `parties` 96, `troops` and `quests` 106, each
with changed instructions (the store moves, as frontend.md says). Together, all four: **regdiff 46, not 0**, and
the two that helped alone give the same 46. So the pipeline prints `NOT FOUND` with that variant as the closest.
Not established: whether the reference copy this draft was made from (`work/scratch/immcand/mb/game_2.cpp`)
compiles to regdiff 0 with the flags and include directories used here; they were taken from the neighbouring
`cs.sh`, not from the Mount & Blade build, and the reference was not compiled in this run. Until that is checked,
46 can be the tool's shortfall or the setup's.

Count: 2 of 5 runs printed `FOUND` (0x00a89680, 0x006dc990); in a third (0x004da900 with the merged return) the
matching variant was generated and confirmed by a plain compile; 1 is out of the tool's reach by construction;
1 (Mount & Blade) improved from 106 to 46 without matching. All three successes are callee bodies (one as the
realisation of an `extend:` answer, two from stage 3). No run was closed by realising one of the model's `ref:` /
`unref:` answers code-neutrally: on these drafts the values the model wants referenced are never modified
(`this`, compiler temporaries), which is the case section 0 rule 1 excludes.

Not run: Fallout 3 0x008e7d40 (its draft unit did not compile here on its own); the cl.exe functions of `src/cl/`
(looked up in the git history: their fixes are block-layout and tail-merge spellings, e.g. `get_message`,
`do_rm_il`, not allocation edits, so there is no model answer to start from).


## 5. Not verified, and what would help

- Everything in section 2 is from small probes plus the functions of section 4. A construct that is
  code-neutral in a probe can still move a scratch pick or a scheduling decision in a large function; the tool
  measures it per function, the table does not promise it.
- `headdup` was only seen merging back when both copies got the same registers; when they do not, the code
  differs (not probed separately).
- `copy` on 13.10.3052 and the key table on 13.10.3052: only `tests/data/realize.cpp` was run there.
- 13.10 compiles: `c2trace.py` sends a 13.10 traced compile to the Mount & Blade compile server when
  `~/.config/mb-compile-token` exists. `C2TRACE_REMOTE=0` keeps it on local wine. The 13.10 numbers in this file
  were first obtained through that default and then repeated with `C2TRACE_REMOTE=0 CCACHE=0` (p1 to p4 and
  `tests/data/realize.cpp`: the same edit lists and key counts; `out/*_local.txt`).
- The x87 class: `Delta` reads the first integer Spillcosts pass. x87 and XMM candidates are not compared, and
  13.10's FPColor is outside it.
- Splits: an edit is matched block by block against the first pass; a construct that changes which webs are
  split is reported as extra `gone:` / `var:` items of the same name, not as a split.
- `helperbind` (stage 2) has not closed a function yet: 0x008e7d40, where it is the known answer
  (`helpersweep.py` found it), could not be run.
- Stage 2's combination rules have produced an improvement (Mount & Blade, 106 → 46) but no match yet.
- rasim features that would shorten the loop, described for whoever owns `tools/rasim/src/`:
  1. `want` / `need reg:` printing, for each `before:` / `after:` answer, the cheapest `ref:` / `unref:` /
     `var:new` realisations inline (today realize.py runs `need 'A>B'` again for the first three);
  2. a machine-readable answer list (`--json`): edit strings, cost, registers moved;
  3. `var:new:B` and `P:B:±1` as first-class answers of `need reg:` (they exist only in `need 'A>B'`), since the
     copy construct and the constant respelling realise exactly those;
  4. answers restricted to edits a given construct list can realise (`--only ref,extend,P`), so that the search
     depth goes to pairs of realisable edits instead of pairs with `before:`.
