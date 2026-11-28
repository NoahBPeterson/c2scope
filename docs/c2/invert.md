# Inverting the compiler's rules: from the original's bytes to the source property

`tools/invert/` holds one inverse per documented decision: a deterministic function from what the original's code
shows (frame offsets, operand order, registers, rounding points, call sites) to the source property that makes cl
produce it, so the lifter can emit that source on the first try instead of searching. Markers as in the other specs:
**[C]** read in the code or seen in a trace, **[M]** measured by probes or on known answers, **[I]** inferred.
Tests: `tests/probes/invert.py` (sources `tests/data/rules/RI_*.cpp`).

Each section gives the forward rule (with its evidence), the inverse, its accuracy on functions whose answer is known
(the byte-exact functions of spore-decomp), whether the lifter uses it, and what the static benchmark gained.

## 1. /Od frame layout -> local names and declaration order (`odframe.py`)

### Forward rule **[C + M]**

At /Od every named automatic gets its own slot (`StkAllocateLocals`; frame.md 1). The slot order is the order in
which c2 *reads* the symbols, and that is c1xx's order of writing them, a hash-table walk:

1. **The identifier hash (c1xx).** `GetIdForString` (c1xx 14.00 0x10520370) hashes the spelling when the lexer first
   meets it: `h = 0xb170a1bf`; per little-endian dword `x = (dword + h) * 0x401; h = x ^ (x >> 6)`; then per
   remaining (signed) byte the same; then `h = h * 0x19660d + 0x3c6ef35f` and `Id_t+8 = (h ^ h >> 16) & 0xffff`. **[C]**
2. **The scope table.** `BlockScope_t::EnterSymbol` (0x10585200) puts a symbol at the head of bucket
   `Id_t+8 & mask`; a function body's scope and a nested block's have mask 15 (16 buckets). **[C code; M: the mask
   reproduces 48-name, 16-name and 3..5-name probes exactly]**
3. **The walk.** `BlockScope_t::ForEachSymbolDo` (0x10631977) visits buckets 0..15, each chain from its head, so the
   symbols come out by **bucket ascending, later declaration first within a bucket**; nested block scopes follow
   their parent in source preorder. c2 allocates each automatic as it reads it (`getsym` -> `datalloc` ->
   `allotemp`, traced on 14.00: `@allotemp@8` called from `@datalloc@4+0x62` <- `@getsym@4+0x2b6`), downward from ebp.
   The FE key (`SSR+0x28`) is not the order (it follows first use); c2's own `_LocalSyms` sort (by FE key,
   `initSymbolForCleanStackAlloc`) is the SEH path and not taken here. **[C]**
4. **The offsets.** `allotemp` rounds the size up to the alignment and the offset down to it: alignment 1 for a 1-byte
   object, 8 for an object of 64 bytes or more or one holding a `double` / `__int64`, else 4 (a `short` or a `char[3]`
   takes a 4-aligned slot, an `int[23]` an 8-aligned 96-byte one). An unreferenced local keeps its slot (it is only
   missing from the listing). **[M: tests/data/rules/RI_odsolve.cpp and the al1/al2 probes, 14.00 and 15.00; 13.10
   agrees on scalars, arrays and the 92-byte case; a 5-byte char array is 8-aligned on 13.10.3077 [M, one probe]]**
5. Statement order and first use play no part (RF_oddecl.cpp v3/v7). The same hash, mask and walk on 13.10.3052,
   13.10.3077, 14.00 and 15.00 (48-name probe identical on all four); VC6 (13.00.9044) sorts alphabetically instead.

This replaces the "set-dependent rank" picture of `tools/od_pick.py` / `od_rankf.py`: a name's rank in a pool is not
fixed because only 4 bits of its hash are compared and ties go by declaration order, but the rule itself depends on
nothing but the scope's names and their declaration order.

### Inverse

`odframe.solve(slots)`: given the original's named slots of one scope (offset, size, type), walk them from ebp down;
where a slot does not land by itself (dead room: an inlined callee's locals) put unreferenced filler locals first
(chars to a 4-aligned depth, `int x[k]` with k <= 15 so they stay 4-aligned, chars for the rest); an array of 64
bytes or more at a 4-aligned offset is split (the lower 60 bytes keep the name, filler above: the original's object
there was several, and /Od code only takes the base address). Then names: keep a longest run of the lifter's names
whose buckets already do not decrease, give every other slot the shortest suffix (`_a`.., `_2`..) that puts it in a
bucket between its neighbours', and declare each bucket's members in reverse slot order. Every frame without
overlapping slots is solvable (all names could share one bucket); the result is checked against the forward rule
before it is returned.

### Below the named locals: IL temporaries **[C + M]**

What the walk visits are symbols (named locals, and the inliner's `sN` symbols: an inlinee's `this` or parameter
copy, a return value that is a load). c2's IL temporaries that /Od keeps in memory (a `?:` result, an inlined
callee's return value that is itself a temporary, as for `return x++ + 1`) get slots after them and after `this`'s
home, in one downward sequence in evaluation order (lift2il.md, "Postfix results dropped"; same on 13.10.3077,
14.00, 15.00). So a slot below `this`'s home is never a declared local: the inverse is a construct, not a name.

### Two exceptions **[M: probes RI_odalign.cpp and work/invert/probes/a16..a20.cpp, identical on 13.10.3052, 13.10.3077, 14.00, 15.00]**

- **A frame realigned to 16** (any local of alignment 16, `__declspec(align(16))` or a Havok vector: the prologue
  does `and esp, -16`) is allocated in **first-reference order** of the function's tuples, not by the hash walk: a
  16-aligned POD is placed where it is first used, a class object where its constructor runs (its declaration).
  Plain 8-aligned objects, constructors without the over-alignment, destructors and `new` initialisers change
  nothing (a17-a19). This was the mechanism behind 4 of the first measurement's misses (`/Oy` Havok functions).
- **A register parameter's home** (a fastcall `ecx` / `edx` argument copied to the frame) is allocated after the
  body's locals, whatever its bucket (0x00428130 `Vector48_Free`: `q` before `v`).

### Accuracy on known answers **[M]**

`odframe.py measure`: Spore's 2,487 byte-exact /Od rows (slice manifests), their sources compiled with `/FAs`; the
function scope's named locals (`this`'s home and register-parameter homes left out) ordered by the rule from their
names and declaration order in the source (first-reference order in the 4 realigned frames), compared with the
listing: **296 of 296** functions with two or more such locals predicted exactly (2,528 of 2,528 ordered pairs).
(The first version, without the two exceptions and with a declaration finder that read comments, had 288 of 294.)

### Lifter

`il2c_lift.py` (/Od units, call site marked `# invert: odframe`): the frame slots (`v_lN`) become separate locals
placed by `odframe.place_locals`; the packed frame structure of `il2c_od.od_frame` stays the fallback (overlapping
slots, a scalar wider than its room, a declared local whose slot is unknown). A realigned frame would need the
first-reference order instead; the lifter's /Od units have none (no 16-aligned type is lifted), so the solver does not
handle it. An address-only array the lifter sized
past its room is shrunk to the room. `INVERT_OD=struct` keeps the structure whenever it applies, `INVERT_OFF=1`
turns every inverse off.

Static benchmark, Spore /Od selection (1,023 rows, `lbench --task lift2il`, same lifter state):

| run | MATCH |
|---|---|
| inverses off | 244 |
| separate locals only where the structure fails | 245 (+1: 0x004549b0) |
| separate locals wherever placeable (the default) | 247 (+3, none lost) |

New byte-exact functions, verified with spore-decomp `tools/matching/chk.py`: `work/invert/new_spore_1.txt`
(0x004549b0), `new_spore_2.txt` (0x00474bd0), `new_spore_3.txt` (0x0054ac70).

### /O2 frame packing

At /O2 the order is the packer's (slots.md rules 2, 5, 7: element list by size, uses, first to reach the count;
colours newest-first; sorted by `1000*uses/size` only above 128 bytes or with /GS). Names and declaration order do not
enter (`RF_declorder.cpp`: three declaration orders, same code) **[M]**, so the inverse of a /O2 frame is not a name or a
declaration order but use counts and scopes (slots.md's consequences: separate per-loop counters, a block scope for a
local whose address escapes); `c2slots.py --orig` already reports those per slot. Not wired.

## 2. Commutative operand order -> source operand order (`operands.py`)

### Forward rule **[C]**

`OptLeftWeightTree` sorts the operands of OPADD / OPMUL / OPAND / OPOR / OPXOR / OPEQ / OPNE by
`SU << 24 | count << 16 | hash`, larger first, with a stable merge sort (symkeys.md sections 0, 3). A tie keeps the
order the source wrote: `f1() * f2()` and `f2() * f1()` both evaluate the left call's temporary first (`sites` prints
`tie: source order`); `g(a) * g(b)` and `g(b) * g(a)` both evaluate `g(a)` first (a, declared first, has the larger
parameter key, so its call's subtree hash is larger) **[C: traced, 14.00, work probe tie.cpp]**.

### Inverse

For a site where the original evaluates X before Y, with our compile's weights (`symkeys.py sites --pass 2`, or the
model's replay): **free** when weight(X) > weight(Y) (any source order); **write** when they are equal (X must be
written first: the only case where operand order is a source property); **keys** when only the key-following hashes
put Y first (no operand order can do it: X's key must grow, i.e. X first referenced later in the IL than Y, or, for
parameters, declared earlier; `symkeys.py levers --site` gives the edits and their side effects); **shape** when SU or
the node count decides, or a hash that follows no key (no operand order and no key edit; sulever.py's catalogue, on
13.10 usually different code).

### Accuracy on known answers **[M]**

`operands.py measure`: 100 byte-exact Spore /O2 functions (seeded sample of the slice manifests, 15.00), 557 pass-2
two-operand sorts: **6 ties (1.1%)**, 551 decided by weight. So the source's operand order matters at about one site in
a hundred; everywhere else it is free, and an operand-order mismatch with the original is a key or shape question,
never a spelling of the same expression. The tie rule itself is the probe above; the measurement's source locator
(both leaves named on one line) found too few tie sites in the sample to score them against the source text.

### Lifter

The lifter already writes each commutative operator's operands in the original's evaluation order (the machine
order of lift2il), which is the inverse's answer for **write** sites and harmless for **free** ones; no call site was
added. For **keys** sites the source edit is a first-reference order change, which `symkeys.py levers` computes from
a trace; it is not applied statically (the lifted unit's keys depend on its whole allocation stream).

## 3. Callee-saved register roles -> definition order (`regorder.py`)

### Forward rule **[C]** (regalloc.md 0, 3.4, 4.2, 5; rasim replays it)

Webs are coloured by priority, equal priorities by defOrd descending (within a block the value defined first, across
blocks the one in the later block); with zero ChoosePhysReg weights the k-th call-crossing web coloured takes the
k-th free of esi, edi, ebx, ebp.

### Inverse

`regorder.py solve SNAP VAR=REG..`: from the original's registers (regmap) and one model state of our compile, every
pair of competing webs that our compile gives each other's registers (or orders wrongly) is a required colouring
order `u before v`; with **equal priority** it is a definition-order fact (define u before v, or in a later block),
with **priority(u) < priority(v)** it is not an order fact but a priority gap (references x P x w), stated with its
size for rasim's `need` / realize's constructs; the answer is checked with `rasim regs SNAP before:u:v ..`. Probe:
`int a = g(1); int b = g(2); int c = g(3);` gives a/b/c esi/edi/ebx (all priority 24); wanted c=esi b=edi a=ebx, the
solver says define c, b, a in that order, the model agrees, and the compiled permutation gives exactly that **[C]**.

### Accuracy on known answers **[M]**

`regorder.py measure`: 68 byte-exact Spore /O2 functions with a regs report (80 sampled, 15.00), 193 pairs of webs
holding callee-saved registers: **6 (3.1%) have equal priority**, and in all 6 the web with the larger defOrd was
coloured first (the tie rule holds 6/6). So definition order decides about one callee-saved pairing in thirty; the
rest are priority (use counts and loop weights) or interference facts, which is why statement moves alone rarely close
a register-role row (regroles.md 3).

### Applied to lifted units (`regfix.py`)

`regfix.py CORPUS VA..` lifts the row, makes ONE traced compile of the lifted unit (regmap against the original and a
`regs --export` snapshot: the model's state, not a search), solves the constraints above, moves each definition the
`def` constraints ask for (`move_def`: the same block, nothing in between reading or writing what it touches), and
compiles once more to score. It is a post-pass, not inside `il2c_lift.unit`, because it needs that compile.

Spore /O2 benchmark rows of class `regalloc` (all 7): 3 differ only in scratch-register picks (RegAssign's rotation,
not Color's order: `rasim scratch`, out of this inverse's reach), 3 in caller-saved Color registers (eax / ecx /
edx: decided by preferences and neighbour weights, not by the callee-saved order), 1 (0x00924bd0) in callee-saved
registers with a **priority gap**, no tie. So no definition move applied; the model's answer for 0x00924bd0 is
`before:t219:t1009` with a gap, i.e. a reference-count change.

The coordinator's cases, on the project's own sources (the lifted units of these three rows are structurally far
from the original, byte diff 553..803, so their register lists are not comparable):

- **0x00d247f0** `cCreatureCamera::ReloadTuning`: pm=esi keyA=edi keyB=ebx, the original pm=ebx keyA=esi keyB=edi.
  `regorder.py solve` says: `keyA before pm`, `keyB before pm`, both **priority 15 < 17**, not a definition order; the
  model (`rasim regs before:keyA:pm before:keyB:pm`) then gives every wanted register. `rasim blocks`: pm loses 2 in
  each of blocks 10, 11 (lived through) but gains 16 in block 13 (two uses, P 4); `rasim need 'keyA>pm' 'keyB>pm'`
  finds three single edits: `unref:pm:9`, `unref:pm:13`, `var:new:10:2` (a new two-reference candidate in the
  `if (GetCurrentGameMode() == ..)` arm, P 2 -> 3). Moving `pm = PropertyManager()` later is the wrong direction
  (model: pm 17 -> 24). The construct for `var:new:10:2` (realize.md 2.3) needs a value used twice in that arm; the
  one tried (a named local for `kDead_b1b104`) is constant-propagated away (`regs`: "every reference was removed
  before GrgIdentifyCandidates"). The original's arm stages that constant through a register (`mov eax, 0xb1b104;
  .. mov [esp+0x1c], eax`, no relocation) where ours stores the immediate directly: an immediate candidate, which
  needs two operands of the value 0xb1b104 at Globregs (immcand.md 1), yet the original's code holds only one. So the
  original's second use was removed after Globregs. Tried (traced `regs`, priorities unchanged at 17 / 15 / 15): the
  store written twice, a local stored twice under a test, a self-assignment of another field next to it. `unref:pm:13`
  (pm's two uses in `pm->GetPropertyList(..)`: the vtable load and `this`) and `unref:pm:9` (the call result's
  definition) have no code-neutral construct. Not resolved: shown which state change is needed and that the cheap
  constructs do not make it.
- **0x00b56fe0** and **0x00902950**: regmap's differences on the lifted units are scratch picks and caller-saved
  registers (5 of 11, 14 of 21 are scratch picks); no callee-saved tie.

So on every case available the callee-saved differences are priority or pick facts, matching the measured 3.1% tie
rate; the definition-order inverse is correct where it applies (probe) but has had no row to apply to.

## 4. x87 rounding points -> float locals / aggregate members / expressions (`x87names.py`)

### Forward rule (x87.md 12, x87round.md 3-4)

A value comes back rounded when it is a float argument, a computed float return, live across a stack-killing call,
spilled at depth 7+, or assigned to something with a memory home at Globregs: on 14.00 / 15.00 every named float /
double variable and float-typed temporary; on 13.10 a named scalar only across a call / volatile / under pressure,
but a float field of an aggregate local, and a float parameter of an inlined helper, always.

### Inverse

Per rounding point of the original, from its bytes alone (x87round's simulation of the original): `argument` (float
parameter), `return`, `call` (nothing to choose), `pressure` (nothing to choose), `named` (14.00 / 15.00: a float
local, temporary or cast), `member` (13.10: a float field of an aggregate local or an inlined helper's float
parameter; a plain float local is not rounded there); a value the original reuses without a store must not be a float
local or temporary on 14.00 / 15.00 (il2c_lift's `inline_x87` already folds those).

### Accuracy on known answers **[M]**

`x87names.py measure`: 108 byte-exact Mount & Blade functions with x87 frame stores (120 sampled; 13.10.3052; the true
source compiled, so ours is the original), 808 rounding points: the property predicted from the code alone agrees
with the reason x87round finds in the true source for **749 (92.7%)**; 99.1% of the 756 that x87round can classify.

The first version (88.2%) had 36 misses that were named float locals rounded on 13.10 with no call between, against
x87.md 12.2 rule 7 as written. Traced (0x00423080 `quat_to_matrix`, `regs --export` + `rasim need SNAP stack:V`):
`z2`, `xx`, `wx` .. are x87 webs that FPColor does not keep on the stack: each is split where a web below it (`y2`)
ends while it sits above (`InsertColoredLifetimeEndSplit`), and the pieces have benefit <= 0 and are dropped by
`FPSpillWorthless`, so the variable lives in its frame home and every assignment is a rounded store **[C trace]**. So
on 13.10 a no-call rounding means "the value had a memory home": an aggregate's float field, an inlined helper's float
parameter, a volatile, or a named float FPColor left in memory. The remaining misses: 2 float temporaries, 1 cast,
1 inlined-helper result, 2 arguments whose reload crosses a call, 1 return through a named local; x87round leaves 52
points `unclassified`. The bytes do not separate the `member` cases (187 inline parameters, 52 fields, 36 uncoloured
locals, 5 volatiles).

### Lifter

`il2c_lift.py`, /O2 units on 13.10 (marked `# invert: x87names`): every float frame slot of the lift (`float v_lN`,
a memory home the original stores and reloads) becomes the field of a one-member structure local. Static benchmark,
Mount & Blade selection (263 rows, 13.10.3052): MATCH 0 -> 0; of the 11 rows whose code changed, 8 moved closer to
the original (byte diff down by 1..7) and 3 further (up by 1..6); Spore /O2 selection (1,500 rows, its 13.10 rows
included): 51 -> 51 MATCH. `INVERT_X87=0` turns it off.

## 5. Inlining -> which callees must be hidden / defined first (`inlining.py`)

### Forward rule **[C]**: inline.md rules 1-8 (callee-first compile order, the /O2 cost model c, d', w, v < 74, the
never-candidates).

### Inverse

Per call site of the original: a call that stayed although the model would inline the callee (v < 74) means the
callee was **not visible with its body** when the caller was compiled (declared only, another unit, or noinline); a
call the model would not inline is **free** (the callee may be defined in the unit); an inlined callee must be
**defined and compiled first** with the model accepting the site, or be `__forceinline`.

### Measured **[M, static]**

`inlining.py measure` over the Spore /O2 benchmark units (1,500 rows): of 6,679 direct call sites with a known callee
size, the model would inline **3,604 (54.0%)** if the unit defined the callee ahead of the caller. The lifter's
`FUN_<va>` extern declarations are therefore required, not a convenience: defining those callees (to get rid of a
stand-in) would inline them and change the caller. Accuracy against known answers was not measured (it would need
c2inline traces of the true sources; the forward model's own validation is inline.md's).

### Accuracy on known answers **[M]**

`inlining.py known 40`: 40 Mount & Blade byte-exact sources traced with c2inline (13.10.3052), 6,072 depth-1 sites
whose callee was compiled: the verdict from what the bytes give (the callee's compiled size, its argument count read
from its decorated name, `this` included, constants unknown and taken as none; the block weight; the callee's loop
share) agrees with the inliner for **5,386 (88.7%)**: 5,283 inline / inline, 103 call / call, 553 predicted inline but
called (B0 / branch / veto terms the bytes do not give), 133 predicted call but inlined (constant arguments, which
add 5 instead of 2 each). 4,873 callees have C names (no parameter list); they were taken as 0 arguments.

### Recognising inlined bodies (`inlining.py detect`)

Every byte-exact leaf callee of the project (no call, 6..40 instructions after dropping pushes, pops, the stack
argument loads and `ret`) is turned into a shape (mnemonic and operands, registers and stack slots wildcarded, member
offsets kept), and the non-exact rows' code is searched for it: **32 of the 1,500 Spore /O2 benchmark rows contain an
inlined copy of a known callee** (43 sites, 8 distinct callees; 0x00ff0430 23 times, 0x01103300 11 times). With
offsets wildcarded too the count is 158 rows / 32 callees, mostly short load/store idioms (false positives).

### Lifter

No change: every lifted callee is an extern declaration, which is the inverse's answer for every kept call. Inlined
regions of the original are lifted as written-out code; recognising them as a callee's body (to emit the helper and
let c2 inline it, with the inliner's temporaries and copies, inline.md rules 10-15) is detected (above) but not
emitted: the lifter would have to replace the region's statements by the call and add the callee's source, which
needs a region-to-statement map inside lift2il / il2c_lift (the other agent's code) and the callee's source text with
its argument mapping. Not wired.

## 6. Scheduling -> statement order (`schedorder.py`)

### Forward rule **[C]** (sched.md): per region the list scheduler issues the ready instruction of highest priority
`1024*(height + successors) + 32768*[reads memory] + 2^23*[sets the final jcc's flags]`, ties to the earlier tuple
(lex: the order after register allocation, set by the source's statement and evaluation order).

### Inverse

At the first difference of a region (schedwant.py's original order and model replay), the original issues W where we
issue X: **tie** (equal priority, both ready: W's tuple must come first in lex order, a source-order constraint),
**dag** (a DAG change explains it: schedwant's unalias / alias / cut / ns edits, a construct), **other** (neither:
a different pre-schedule order from an operand sort or the front end, or the cycle model's unit / latency choice).
`schedorder.py fix` moves the statement defining W's value before X's when both are definitions in one block and
compiles once.

### Evaluation orders behind the ties **[M: tests/data/rules/RI_evalorder.cpp; 13.10.3077, 14.00, 15.00 alike]**

- **Call arguments are evaluated last first**: `g(p->a, p->b, p->c)` loads `c`, `b`, `a`; `g(h1(), h2(), 3)` calls `h2`
  before `h1`; a float argument (`gf(p->a, p->f)`) is loaded before the integer one on 14.00 / 15.00, and spelling it
  through a local before the call (`float f = p->f; gf(p->a, f)`, `int x = p->a; gf(x, p->f)`) changes nothing (13.10
  passes the float as a dword copy). So an argument-setup tie in the original's other order is not an argument spelling.
- **Member stores keep the statement order** (`p->a = x; p->b = y;` and the reverse give the two store orders, the loads
  of x and y follow).
- **A constant return value is set at the return, after the statement's stores**, for every spelling tried:
  `b = -1; return false;`, `bool r = false; b = -1; return r;`, `return b = -1, false;`, through a pointer, `return !true;`,
  `return a < 0 && false;` (work/invert/probes ev2.cpp). When it is a variable merged across a join (`bool ok = false;
  if (c) { ..; ok = true; } else b = -1; return ok;`) the `xor al, al` is hoisted to the function's entry instead. None
  of the 12 forms puts `xor al, al` just before the member store in the same tail block, which is what 0x0060c6d0's
  original does: that tie is not reachable by a return-value spelling found so far.

### Measured **[M]**

The 58 Spore /O2 benchmark rows of class `schedule` (15.00): 68 differing regions, **11 tie**, 14 dag, 43 other; 10
rows had no differing region schedwant could locate. `schedorder.py fix` (statement moves for register definitions and
for member stores, base pointers followed through their copies; kept only when the byte diff drops) applies to one
(0x00d72fd0: the `[self+8]` store moved before the `[self+4]` one): the tie is resolved but the byte diff goes from
29 to 35 (the stores' values change their last-use order and registers), so it is rejected. The other ten are a
member store against the return value (above), call-argument setup (evaluation order fixed, above), a stack
adjustment after a call (`add esp` against a store: the call's cleanup position, not a statement), and loads / `lea`s
of one expression. No new match.

## Summary

| inverse | rule confidence | accuracy on known answers | wired | benchmark |
|---|---|---|---|---|
| /Od frame -> names, declaration order | C (code) + M (4 builds) | 100% of 296 Spore /Od frames | yes (il2c_lift /Od) | Spore /Od 244 -> 247 MATCH |
| commutative operand order | C | ties 1.1% of 557 sites (source order free elsewhere) | lifter's default already | - |
| callee-saved roles -> definition order | C | ties 3.1% of 193 pairs, rule 6/6 | post-pass (`regfix.py`), 0 of 7 regalloc rows are ties | - |
| x87 rounding -> locals / members | C + I | 92.7% of 808 M&B points | yes (13.10 /O2) | M&B 0 -> 0 MATCH, 8 rows closer, 3 further |
| inlining -> hidden / defined first | C | 88.7% of 6,072 M&B sites (c2inline); 32 Spore rows hold a known callee's body | detection only | - |
| scheduling -> statement order | C | 11 of 68 differing regions are ties (order-decided) | post-pass (`schedorder.py fix`), 0 applicable | - |
