# Symbol keys: which number orders x87 operands, how it is assigned, and how to move it

Back end `c2.dll` of cl 14.00.50727.762 (VS2005 SP1); section 7 covers 13.10.3077, 13.10.3052 and 15.00.30729.1,
section 10 the x64 cross compiler cl 17.00.61030, section 11 the VC6 Processor Pack back end 13.00.9044. Tool:
`tools/symkeys.py` (tracer.md, `symkeys`). Probes: `work/scratch/symkeys/` (`p1.cpp`, `c1.cpp`..`c4.cpp`,
`tu_a.cpp`/`tu_b.cpp`), regression file `tests/data/symkeys.cpp`. **[C]** = read in the code at the address given or
seen in a traced compile of the named probe; **[I]** = inferred; **[M]** = measured: a probe in `tests/data/rules/RX_*.cpp` decides it on all four builds (checks in `tests/probes/misc.py`). il_sym.md describes the symbol records themselves;
this page is about the number.

## 0. Rules for matching

1. **The number is `SYM.key` (+0x1c), the back end's own per-function id, not c1xx's symbol id.** `OptHashNode`
   0x10bb6720 hashes a named variable leaf as `fold(key) << 5` (0x10bb6813..6845), a temporary without a single
   definition as `key << 6` (0x10bb6803), and a memory operand `[base+d]` as `d + (opcode - 0x274) + (hash(base) << 8)`
   (0x10bb6798). That hash is the low 16 bits of the weight `OptLeftWeightTree` 0x10bb64b0 sorts commutative operands
   by (x87.md rule 2). c1xx's id (`SSR.key` +0x28) is used only in `SymCmpSym`'s last tie (il_sym.md section 6) and in
   the listing names of temporaries (`$T5944`). **[C]**
2. **The hash is periodic.** For a named symbol `hash = ((key & 0x7ff) << 5) ^ (key >> 11)`, so keys 2048 apart hash
   almost alike and **a key just past 2048 or 4096 hashes lower than every key below it**. For a member operand only
   `(key(pointer) & 7) << 13` (plus `(key >> 11) << 8` once keys pass 2048) survives the `<< 8`; for a pointer held in a
   temporary only `key & 3`. **[C: code; 806ab0 below]**
3. **Keys are handed out by an allocator, not by the source.** Five pool classes (frame = locals, parameters, field
   views, compiler temporaries `$T`; global; temp; label/function; hash), each filled from its current block of 32
   consecutive keys; a full class takes the next 32 keys after the last block anyone took. A freed key goes on a LIFO
   list of its class and is the next one that class hands out. **[C: il_sym.md section 4; replayed exactly, section 5]**
4. **The table is per function** (`SymInit` from `startproc`); inline expansions are read into the caller's table.
   Functions earlier in the unit, and helpers instantiated earlier, change nothing (`tu_a.cpp` vs `tu_b.cpp`: 79 symbols
   identical). c1xx's ids are per unit and do shift, which only renames `$T`s. **[C]**
5. **Order of allocation** (each one takes the next key of its class):
   - parameters first, in reverse declaration order (`this` first); then every local, field view and reader temporary
     at its **first reference in the IL**, which is evaluation order, not declaration order; **[C: c1 kparams, order2,
     firstuse]**
   - the inliner runs after the reader: for each call in IL order, depth first (a callee's own inlines inside its
     expansion), it reads the callee: the callee's parameters, then its locals by first reference; a parameter it can
     substitute is **freed at the splice**, so the next frame allocations reuse those keys (LIFO). Call arguments are
     evaluated, and so inlined, **right to left**: in `f(g(), h())` h is expanded before g. **[C: c2 two, nest; c4 e2]**
   - the optimiser then makes temporaries and value-numbering temps (hash class); `SymGarbageCollection` frees
     unreferenced temps, whose keys are reused. **[C: p1 events]**
6. **In a big function the frame class runs through many blocks.** Its keys jump (29, 160, 191, 992, 1600, 2176 ... in
   0x00806ab0) because the temp class takes blocks in between. A frame symbol's key is therefore *block base + position*:
   one more temp in the reader can move every later frame key by 32 (hash + 0x400) without changing key mod 8; one more
   frame symbol moves the later ones by 1 each until the next block boundary, then by 32 at once. **[C]**
7. **To move a variable's key, change allocation counts before its first allocation, then check with `whatif`
   before compiling.** `symkeys.py levers --var X --hash-below H` lists the single edits that do it; `--match` ranks them
   against a draft whose code is already right. Section 4 says which construct makes which allocation.

## 1. What uses the key

| user | what the key decides | evidence |
|---|---|---|
| `OptHashNode` / `OptLeftWeightTree` (optimize.obj) | order of operands of OPADD/OPMUL/OPAND/OPOR/OPXOR/OPEQ/OPNE: which one is evaluated first; for two memory leaves which is `fld` and which the memory operand (x87.md rule 2) | code 0x10bb6720, 0x10bb6625; `symkeys.py sites` [C] |
| `SymCmpSym` 0x10bf00b0 | order of registers/temps (kinds 1, 3) by key; same-size memory symbols by offset, then FE key | il_sym.md section 6 [C] |
| `EnumerateSymbols` / `CreateGlobregs` (globregs.obj) | GLOBREG web numbers are given in pool order, i.e. key order (regalloc_cost.md section 2) | [C] there; so **the GLOBREG record numbers of webs follow SYM keys**; split pieces then come from the LIFO record list (regalloc.md 3.6) |
| bit vectors (live sets, `_HshTmps`, frame/global key sets) | indexing only, no order | [C] |
| frame slot order | nothing: slots are sorted by GS level and `1000*uses/size` (T > 128 or /GS), else kept in colour creation order, which is the element list order (size, uses, first to reach the count in the instruction stream; slots.md rules 2, 7). **Correction**: not key order. `o_args` (`use2(&a, &b)`, keys a < b, b pushed first) and `o_hoist` (b's second use in a loop before a's) put b's slot first | [M: `tests/data/rules/RX_slotseed.cpp`, `symkeys.py keys` and `slots`, all four builds] |

## 2. Where the keys come from: the allocation stream

The tool hooks (14.00 VAs; tracer.md has the 13.10/15.00 equivalents):

| event | hook | logged |
|---|---|---|
| new table | `SymInit` 0x10bef020 | (four blocks: frame 0..31, global 32..63, temp 64..95, other 96..127; key 0 is a dummy) |
| allocation | `SymAlloc` 0x10bef1d0 entry/exit | kind in al, current line (`[0x10d0d6fc]`), stack words for the creator; key, SYM |
| free | `SymFree` 0x10bef3e3 (inside the chain loop) | each member's key and the head's kind; the list is chosen by the head's kind (table 0x10bef44c: 3 temp, 4/5 frame, 7/8 global, 9-11 other, 6 none) |
| block | `SymAllocPool` 0x10beef50 exit | first key of the block |
| user variable | `SymMakeUserVar` 0x10bef7b0 | SSR name, SSR, FE key (names unnamed c1xx temps `$T<fekey>` like the listing) |
| field view | `SymNewEquiv` 0x10befce0 | base, offset; type and size at exit (`_v+4.f4`, `_v+4.b0` = the 8-byte rest view) |
| inline read / splice | `GetInlinee` 0x10b6b950 (nested `RdrReadProc`) / `InlineInlinee` 0x10b6c770 | callee name (`ebx>4>0x28>0x18>4`), call line |
| operand sort | `MscMergeSort` 0x10b9d4b0 called from 0x10bb662d, and `OptLeftWeightTree` exit | sorted operand list: kind, weight, leaf key, first two sources of a subtree |

`replay()` in symkeys.py is the allocator: classes, blocks of 32, per-class LIFO free lists (13.10: temp and frame
only). On every traced compile so far it reproduces **every** key: 0x00806ab0 48 486/48 486, 0x005c6320 6 504/6 504, all
probe functions, on all four builds. **[C]**

## 3. The hash and the order (what to aim for)

A two-operand commutative node puts the operand with the larger weight first: `SU << 24 | count << 16 | hash`. With
equal SU and count (two loads, two field views, two `(double)` conversions of loads) the hash decides:

| operand | hash (low 16 bits) |
|---|---|
| named variable or field view, key k | `((k & 0x7ff) << 5) ^ (k >> 11)` |
| temporary with several definitions | `(k << 6) & 0xffff` |
| temporary with one definition | its definition's weight |
| `[base + d]` (a memory leaf, OPBASED 0x27c) | `d + (0x27c - 0x274) + (hash(base) << 8)` = `d + 8 + (hash(base) << 8)` (`[p+0]` with p key 2: 0x4008) |
| member at `d != 0` before AddrModes: `[t+0]` with `t = p + d` (one definition) | the weight of `OPADD(p, d)`: `h(p) + 2·d + (0x2a5 - 0x274 = 0x31)` (13.10: `+ 0x2e`), SU 1, count 2 |
| conversion / negation of x | `hash(x) + opcode - 0x274` |

`symkeys.py sites --line L --pass 2` prints each sort with both operands, their keys and weights.

The `+ 0x31` once given here for `[base + d]` was the OPADD constant of the address temporary, not the leaf's: the code
(`OptHashNode` case 2, 0x10bb6798) adds the leaf's own opcode minus 0x274, which is 8 for OPBASED, and the R200 /
R450 notes of f3decomp quote the two forms (rules_xref.md). Traced on all four builds with
tests/data/rules/R200_memhash.cpp: `p->a * q->a` sorts h4008 / h2008, `p->c * q->b` h0081 / h0059 (13.10: h007e /
h0056). **[C]**

**Sorts of more than two operands.** The optimiser flattens associative chains, so `a + b + c + d` and an address
such as `&scene[vs].visitors[p0].troop` (`base + vs*size + p0*8 + d`) reach OptLeftWeightTree as one OPADD with a
list of three or four operands, which `MscMergeSort` sorts by the same weight, descending; the merge sort is stable.
The `sort3` probe (right after the call: 14.00 0x10bb6635, 13.10.3077 0x1075d977, 3052 0x10770243, 15.00
0x51103975) logs the third and fourth sorted operands and the fifth one's kind, and the model re-sorts the whole
list (operands whose weights stay equal keep the traced order: the input order is not traced). Probe
tests/data/symkeys_nary.cpp: `kn4` sorts `_a _b _c _d` (keys 4..1); in `kcall3` referencing `c` first
(`int c = f3();` before a and b) turns `c b a` into `b a c`, predicted by `swap:_c,_a` (`compare`: 1 of 1, all
four builds). In Mount & Blade's Operation::execute the `scene(vs)->visitors[p0]` adds are four-operand sorts
(`[$o58791 key 24043+0]`, `(?g_visitor_scene key 4135 ..)`, `$o53502`, a constant; lines 1260-1269). **[C]**

**When SU or count decides** (`tools/sulever.py --site LINE:A>B`): no key edit moves the order, and the operand
has to change shape without changing code. Known constructs **[C]**:
- a zero-extended narrow variable in memory (a byte or short parameter) is SU0; `(*(unsigned*)&x & 0xff)` (a 32-bit
  view of it, masked) is SU1 and instruction selection turns `and m32, 0xff` back into `movzx r32, byte ptr [m]`:
  Spore 0x0068c6d0, `(hi << 8) | lo | (g & 0xffff0000)` with lo before the mask term, byte match.
  `(unsigned)x & 0xff` is folded before pass 1; a wider mask (`& 0xffff`) gives SU1 but keeps the `and`.
- a single-use local or a `v |= ..` chain is substituted before the sort (no effect); an inner pair under another
  opcode (`+`, `^`) is one operand of the outer sort but changes the instruction.
- an offset-0 member is SU0 n1 (or n2 through a float conversion); there is no known code-neutral way to raise it
  (Fallout 3 0x00a72450: 12 spellings, `rot.Column(n).x` CSEs into a hash temp, still SU0).

## 4. Constructs: what takes a key (probes `c1.cpp` .. `c4.cpp`, 14.00, the same on 15.00)

| construct | frame keys | other | probe |
|---|---|---|---|
| unused local, no initialiser | 0 | - | c1 unused1 [C] |
| unused local with initialiser (`int u = 0;`), also in an inline helper | 1 (at the store) | the store's temps | c1 unused2/3, c3 ul [C] |
| local in an `if (0)` block | 1 (c1xx keeps the code) | + temps | c1 deadif [C] |
| declaration order | none: only first reference counts | - | c1 order2, firstuse [C] |
| block-scoped locals | 1 each; keys are never reused after the scope | - | c1 scope1 [C] |
| for-init variable | 1 at first reference | - | c1 forinit [C] |
| reference local (`float& r = x`) | 1 (a pointer local) | - | c1 ref1 [C] |
| static local, string literal, global | 0 frame; 1 global key at first reference | - | c1 stat1, str1, glob1 [C] |
| struct local, fields touched | 1 + one per (offset, type) touched + the `b0` rest views: `V v; v.x=..; v.y=..; v.z=..` = 6 | - | c1 struct1/2 [C] |
| by-value struct parameter of an inline helper | 1 + 2 per field read (copied, views) | - | c2 byval: 12 keys for two V [C] |
| inline helper parameters | 1 each, **freed at the splice when substituted** (keys come back LIFO) | - | c2 two, c3 up [C] |
| inline helper locals | 1 each per expansion | - | c2 two [C] |
| a by-value return of an inline helper | `$T` (1) + `___$ReturnUdt` (freed) + views of the `$T` | - | c3 rvsub2 [C] |
| EH: a local with a destructor | `__$EHRec` + 1 view | - | c3 eh1/eh2 [C] |
| `switch` | 0 frame | temps only | c3 sw1 [C] |
| local array, elements touched | 1 + 1 per element offset touched | - | c3 arr1 [C] |
| function earlier in the unit | nothing | - | tu_a/tu_b [C] |
| struct copied element by element through an inlined `operator=` (f3decomp R310) | 14.00/15.00: the `+0` field view is made inside the first `operator=` expansion (its `this` is the local's own address), so element 0 gets a low key; `+12`, `+24` views are made later by the optimiser. 13.10: every element gets a reader-time view first (no asymmetry) | - | rules/R310_copy_elem0.cpp: `_m+0.f4` key 6 (`V::operator=#1`), `_m+12.f4` 13, `_m+24.f4` 14; 13.10: 4, 5, 6 [C] |
| `if ((x & 0x80000000) == 0)` instead of `if (x >= 0)` (R480) | +1 symbol before OptFuncExpr, +7 keys in all (the AND and compare temps); code `test; js` against `test; jl`, all builds | temps | rules/R480_js_jl.cpp: 10 / 42 against 11 / 49 [C] |

**Counts, not keys (R470).** `double s = x*x; s += y*y; s += z*z;` gives the first OPADD of pass 2 the accumulator
(the converted `x*x`) with count 4 against 3 for `y*y` (13.10: 5 against 4), so `x*x` is evaluated first whatever
the keys; a plain float sum `x*x + y*y + z*z` has equal counts and the keys decide. All four builds. **[C: `sites`
on rules/R470_double_acc.cpp]**

## 5. The tool, and how accurate it is

```sh
uv run tools/symkeys.py keys   tu.cpp --func F -- /TP /MT /GS /EHsc /Gy          # every symbol: key, class, origin
uv run tools/symkeys.py sites  tu.cpp --func F --line 293-294 --pass 2 -- ...     # operand orders and their keys
uv run tools/symkeys.py export tu.cpp --func F --out a.json -- ...                # then --from a.json, no compile
uv run tools/symkeys.py whatif --from a.json 'add:frame@_vC01:3'                  # keys that move, orders that flip
uv run tools/symkeys.py levers --from a.json --var 'X' --hash-below 0x9b41 [--match ref.json] [--n 1,2,16]
uv run tools/symkeys.py compare --from a.json 'add:frame@_r:1' --vs variant.json  # prediction vs a real compile
```

Also (tracer.md): `whatif --batch FILE|-` (one edit set per line, one load; stdin doubles as a persistent mode),
`levers --site LINE:A>B` (several wanted orders at once, below), and optimiser symbols as `$o<n>`: every symbol
made after the first OptFuncExpr is named by its rank among the optimiser's allocations (`$o34` = the 34th), which
an edit before it does not change while `$t<key>` would; `keys` shows those an operand sort depends on (`--phase
opt --all` all of them, with the c2 function that made them: `HshInsertEntry` value-numbering temps, `GdfInstrSdsu`,
`OptRebuildTuples`, `TupMakeTmpBinary`), whatif and compare follow them, and `levers --var '$o34'` targets one.
Their keys are replayed exactly; what is assumed is that the optimiser makes the same symbols after the edit.

**The hash pool class (value-numbering temporaries).** `HshInsertEntry` makes the optimiser's value-numbering
temps from the hash pool (blocks of 32, no free list of their own; after the optimiser they are freed onto the
temp list, section 8.1). As a memory base they hash by `64*key`, so
through the `<< 8` of a memory operand only `key & 3` reaches the weight (`[$o key 8554+0]` h8025, 8557 h4025;
model: `KEYED_BASE` in symkeys.py); a reader temporary as a base may hash by its single definition and is still
taken as fixed. Edits: `add:hash@V:n` / `sub:hash@V:n` (V any symbol, `$o<n>` included: the n-th symbol the
optimiser made), and `levers` tries hash edits at the first value-numbering temp of every source line (for
`--match` over the whole function, since those temps come after any reader target; `--classes` picks the
classes). `compare` and `levers --match` name optimiser symbols `$o~k` (the k-th one a sort depends on), so a
hash-class difference now counts. Fallout 3 0x00806ab0 (work/scratch/f3/keywall/RESULTS.md): jp18 against best1
differs only in 3 value-numbering temps made at line offset 218 in best1; `whatif --from 806/jp18.keys.json
'add:hash@$o20695:3'` (the first hash temp of line 218) moves the site-1 temp `$o20956` from 8554 to 8557, the
key measured in best1, and flips exactly the four `[$o] * $T` crosses (lines 439, 513, 660, 734 = source 1191,
1265, 1412, 1486). It also flips 12 later sorts (lines 65-68, 167-202) whose `$o` temps (keys 13.6k-13.9k) the
three extra temps shift; in jp18 the same three temps are made later rather than not at all, so the edit that
stands for moving vDCrossE back is `add:hash@$o20695:3` plus a `sub:hash@..:3` where jp18 makes them. **[C keys;
I: which later sorts best1 keeps]**

`levers --match` and `levers --site` evaluate their edits in parallel (`--jobs`, default min(8, cores)) from the
allocator snapshot (`FastKeys`), and evaluate only one insertion point per equivalence class: two points with no
op of the edited class and no new block of 32 between them give every symbol the same key (exact; checked on 116
samples of 0x00806ab0, 0 differ). On 0x00806ab0 (94k events, 792 sorts) an edit costs about 0.1 s of CPU;
`levers --match jp18 --n 17,18 --classes frame` (664 edits after pruning) took 30 s on 8 workers, so 14 n values
with frame and tmp is a few minutes. On M&B Operation::execute (186k events, 1909 pass-2 sorts) `levers --site`
with three g_visitor_scene sites and `--classes glob --n 1,2,4,8 --depth 2`: 2029 points pruned to 1296 single
edits in 128 s, 120 pairs in 35 s more (a loaded machine), 187 edit sets satisfying all three; the wanted
direction in that run was a guess, not the original's.

**Several sites at once.** `levers --site '1260:(?g_visitor_scene>[$o' --site ...` takes the wanted order at N
sorts (A and B substrings of two operands as `sites` prints them), tries every single add/sub of the chosen
classes (`--classes`, default frame,tmp,glob; `--n`) at every insertion point before the last symbol the sites
depend on, then pairs (and with `--depth 3` triples) of the `--pool` best, and ranks by sites satisfied, then
other pass-2 orders flipped (computed for the best candidates only), then size; partial solutions are listed with
the sites they miss. The single evaluations replay only from a snapshot of the allocator before the edit up to
the last dependency (`FastKeys`, equal to the full replay on the M&B export).

Predictive mode is a replay of the traced stream with edits (add/sub n allocations of a class at a point, delete a
symbol, swap two first references); the sort model recomputes every two-operand weight whose hash depends on a key it
can follow (named leaves, field views, multi-def temps, member operands, the first two sources of a subtree). `compare` and `levers --match` pair
the sorts of two compiles by opcode and operand set (the k-th sort of that set), not by visit order, which a flip
upstream changes (before this the table's first and last rows counted 788 and 131 pairs). Pure
source analysis is not attempted: the temp counts come from the reader and the optimiser. The cheapest input is one
traced compile (3-5 s for 0x00806ab0). Measured accuracy, prediction vs a traced compile of the edited source:

| edit (function) | keys predicted exactly | pass-2 operand orders predicted |
|---|---|---|
| 3 dead locals before `vC01` (0x00806ab0) | 468/468 (411 moved) | 794/794 (12 changed) |
| 15 dead locals before `vC01` | 468/468 (411 moved) | 794/794 (20 changed) |
| the decompiler's padded helper (16 locals in `EC`), as `add:frame@_r@EdgeCross#2:16` | 460/460 (191 moved) | 792/792 (4 changed) |
| swap first references of `left`/`right` (0x005c6320) | 239/239 | 133/133 (1 changed) |
| tests/data/symkeys.cpp (dead local in a function and in an inline helper) | all | all, on all four builds |
| 13.10 member operands: 6 unused parameters between `p` and `q` in `p->x * q->x` (`work/scratch/symkeys/mem1310/l0`, `l6`), as `add:frame@_p:6` | 5/5 | 1/1 (1 changed: `fld [q]` first, as the listing) |
| the same in `p->x * q->y + p->z * q->x` (`m0`, `m6`): the flip is the outer OPADD, through a second source `[p+0]` | 5/5 | 5/5 (1 changed) |

The edit is only as right as its translation of the source change: a construct that also adds temps or reorders
first references needs those edits too, and a different helper (another inline body) is not modelled (it changes which
symbols meet at the site). Sorts whose operands are deeper subtrees are taken as unchanged (none differed in the runs
above). Keys made after the first OptFuncExpr (optimiser temps) are replayed with the traced stream, so they are right
only while the edit does not change what the optimiser makes.

## 6. Fallout 3

**0x00806ab0** (`FUN_00ea3d60`, 14 KB; draft fn3-test `work/806ab0/ba0.cpp`, copy in `work/scratch/symkeys/f3/806ab0`).
Baseline regdiff 28 (17 of them are the jump table, which every draft has). The only code difference is R11
(+0xa9b..+0xacf): in `fC01Dot = vC01.Dot(vDelta)` and `fC01Vel = vC01.Dot(vRelVel)` the x products come out with the
EdgeCross result first. `sites --line 293-294 --pass 2`:

| product | 1st (ours) | 2nd | original wants |
|---|---|---|---|
| x of fC01Dot | `$T5944+0.f4` (EdgeCross#2's return temp) key 4081, hash fe21 | `$T5942+0.f4` (p0 - center) key 3790, d9c1 | the other first |
| x of fC01Vel | `$T5944+0.f4` key 4081 | `_vRelVel+0.f4` key 3290, 9b41 | the other first |

So `$T5944+0.f4` needs a hash below 0x9b41: a key in 4096..5337 (past the 4096 wrap). The decompiler's padding (16
dead `int`s in a copy of EdgeCross) does exactly that: key 4161, hash 0822. `levers --match best1` (the padded draft,
which matches the original's code) over 5184 single edits: **exactly +16 frame allocations, made anywhere from the first
reference of `center` (switch case 1) to the read of EdgeCross#2, leaves 0 operand orders different from the matching
draft**; 15 or 17 leave 4-5, the same +16 earlier in the function leaves 8 or more, and no temp edit of 1-40 does it
(2 orders remain). Natural edits tried, each traced and diffed: the cross product of vC01 through
`FUN_00475280`, `Cross`, initialisation instead of assignment (vA-vE); the same for vC00 (vF, vG); `center` as a
copy (n1, +10 temps, no frame change), the switch's arrays declared in another order (n3, nothing moves), `center`
dropped (n4, -10 frame, other damage). None fixes the site without breaking another (vA/vC/vF/vG break a second site at
+0x1093, lines 440/514/661/735, the `[this+d] * $T` products in the FUN_00475280 crosses). **No natural fix found**;
what one must do is known: a +16 net frame-allocation change in that window (for example a ReworkPoint local or
temporary with its fields touched, about 6 each, in the switch cases), checkable in seconds with `compare --vs`.

**0x005c6320** (`SearchC::FUN_005c6320`, 3205 B; draft `work/5c6320/c8.cpp`). Baseline regdiff 48. The x87 order
sites are member-by-member products; their order is set by `(key(pointer) & 7) << 13`:

| src line | sort | 1st | 2nd |
|---|---|---|---|
| 3257 wedge | `left->y*right->y` | `[_left+..]` key 30 (mod 8 = 6, hash c0..) | `[_right+..]` key 29 (mod 8 = 5) |
| 3259 probe | `left->x*tangent->from.x` | `[_left+0]` key 30 | `[$t2232+0]` (address temp, key mod 4 = 0) |
| 3266-3267 ax/bx | `a.radius*tangent->from.x` | `[$t2114+0]` (mod 4 = 2) / `_a+12.f4` key 235 | |
| 3271 crossA | `first.x*delta.z` ... | `_c@V3C::V3C#3` key 872 | `_first+0.f4` key 363 |

Swapping the first references of `left` and `right` is predicted (and measured: 239/239 keys, 131/131 orders) to flip
exactly the wedge sort, and that makes the code worse (regdiff 52). The address-temp sites depend on reader temp keys
mod 4, i.e. on the number of reader temporaries before them; not pursued further here.

## 7. Other builds

| | 13.10.3077 / 13.10.3052 | 14.00 | 15.00.30729.1 |
|---|---|---|---|
| blocks of 32, four initial blocks, same order | yes | yes | yes |
| free lists | temp and frame only; SymFree puts every non-temp chain on the frame list | per class | per class |
| key order of the probes in tests/data/symkeys.cpp, c3.cpp | same (fewer field views: 13.10 makes one view per field read, no `b0` rest views; c2-made globals have no name) | | identical to 14.00 |
| replay exact | yes | yes | yes |
| operand-sort hash (OptHashNode) | same code shape; memory operand `fold(d) + opcode - 0x24a + (hash(base) << 8)` (3052 0x10753452: d +0x30, base +0x34) | | same |
| member operands in `sites` / `whatif` | `[_p key 2+0]`: base and displacement of a memory leaf, base of a subtree's second source | `([_p key 3+0] ..)` | as 14.00 |

Hooks are in the build tables (`"hooks": {"symkeys": {...}}`, `symkeys_*` options). 13.10's `SymNewEquiv` is inlined;
a probe at 0x10769fe9 names the views. 13.10's sort operands are memory leaves (no float to double conversion), not
subtrees as on 14.00; the trace's `MscMergeSort` exit has no room left (MAXSPEC 12), so the hook's entry logs for the
first two list members weight, kind, memory base key (+0x34 > +0x18 > +0x1c), displacement (+0x30) and the base key of
the second source, and the parser pairs them with the sorted operands by kind and weight (the sort is stable). The
model then moves a member operand's weight by `(hash(new) - hash(old)) << 8` (`<< 9` through a second source), as on
14.00. Probe `work/scratch/symkeys/mem1310/l0.cpp` (`return p->x * q->x;`) and `l6.cpp` (6 unused parameters between):
`sites` shows `[_p key 2+0] h4008` before `[_q key 1+0] h2008`, and in l6 `[_q key 1+0]` before `[_p key 8+0] h0008`;
`whatif add:frame@_p:6` on l0 predicts that flip and `compare --vs l6` scores 5/5 keys, 1/1 orders; the /FAs listings
change from `fld [p]; fmul [q]` to `fld [q]; fmul [p]`; the same on 3052. 13.10.3052 has the same hooks as 3077, found
by unique byte context and read side by side (port-1310.md section 16). Not modelled (as on 14.00): a member operand
whose base is an address temporary with one definition (`[t64+0]`, hash from its definition). **[C: runs; I: that no
other 13.10 site creates views]**

### 7.1 What a global costs in keys (13.10.3077, probe tests/data/symkeys_glob.cpp) **[C]**

Measured as allocations per class against a function that names no global (Mount & Blade's measurements, each
checked; tests/run_tests.py checks the row on 13.10):

| construct (S a struct global, i a parameter) | global keys | frame keys | 14.00 |
|---|---|---|---|
| `(void)gs;` (bare mention) | 1 (the symbol) | 0 | same |
| `gi + i` (scalar global) | 1 | 0 | same |
| `gs.a` (offset-0 field) | 2: the symbol at first use + 1 view `+0` | 0 | same (`+0.i4`) |
| `gs.b` (field at offset 4); `gs.b + gs.b` | 3: symbol + 2 views `+4` (14.00: `+4.b0` and `+4.i4`) | 0 | same |
| `gs.m()` (member call) | 2: symbol + view `+0` | 0 | 1 (no view) |
| `gs.arr[i]` (variable index) | 2: symbol + view `+8` | 0 | same (`+8.b0`) |
| `gs.arr[3]` (constant index) | 3: symbol + `+8` + `+20` | 0 | same |
| `S* p = &gs; p->b` or `S& r = gs; r.b` | 1 (the symbol) | 1 (p / r) | 1 + a view made later |

So a field read through a pointer or reference local costs no global key beyond the struct's own one, and each
further field (offset, or array base) is one or two more global keys the first time it is named. Global keys come
from their own pool (class glob, blocks of 32) in first-reference order, so removing or adding globals before a
global moves its key by that many; the hash is periodic (rule 2): g_visitor_scene in Operation::execute was the
72nd global, key 4135 (`keys` on work/from-mb/lane_sib/base.cpp reproduces it), just past the 4096 wrap, so its
hash (04e2) is tiny and its operand sorts last in the four-operand adds above; a key just below 4096 hashes high
(4095: ffe1). Keys move by whole blocks of 32 when a block boundary is crossed (rule 6), so the number of globals
to remove is found with `levers`, not by subtraction. The constraint is function-wide and
coupled across sites, which is what `levers --site` is for.

## 8. The optimiser's value-numbering temps (hash class)

Probes `work/scratch/hashkeys/` (`p1.cpp`..`p4.cpp`, `pr.py` prints per function the hash temps made in the walk
(MW), in the assignment tail (AS) and later (LT), and whether the code is the same; `hk.py` lists each one with
the pass that made it), regression file `tests/data/symkeys_hash.cpp`. `keys --phase opt --all` now shows the
phase as `@HshInsertEntry@12/walk`, `/assign` or `/later` (hooks `hshwalk`, `hshasg` on every build).

### 8.1 When a value-numbering temp is made (14.00, code read; the other builds by trace, 8.3)

1. **One walk over the whole function.** `HshBuildTable` 10b5eb30 (GlobalOptimize step 3g, opt.md section 1)
   follows the tuple list from the first block (`[[FUNC+8]]+0x1c`, layout order at that time, i.e. after dead-store
   removal, single-use copy propagation, unrolling and branch folding) to the end, and per tuple: **[C]**
   - first its memory operands (tuple flag +9 bit 0): every `[base+d]` operand (kind 5/6) among the sources
     (+0x28), then the destinations (+0x2c), gives an `OPBASED (base value, d)` entry, and the operand's symbol
     (+0x18) becomes that entry's temp. This is the `[$o key K+0]` base of the operand sorts;
   - then the tuple itself, by kind (+8): an expression (kind 13) without side effects
     (`TupQuerySideEffectHelper`) becomes `TupMakeHshSym` → `HshEnter(opcode, operand value numbers)`, and its result
     temp is replaced by the entry's temp (`HshFixDst` 10b5f0f0 frees the reader's temp). `OPSUB x, K` is first
     rewritten `OPADD x, -K`. `OPARG`: nothing. `OPCMP` feeding a branch: `HshCompareAssertion` 10b5f0a0 makes the
     entry of the branch's condition (OPLT, OPGE, ...). A call of a pure intrinsic (`fabs`; intrinsic attribute
     0x20) gives an `OPLOCAL` entry for its argument list (`HshEnterArgList` 10b5f230) and one for the call. Other
     calls, stores, branches, labels: nothing.
2. **Then the assignment tail.** After the walk the same list is walked again and every `OPASSIGN` goes through
   `HshEnterAssign` 10b5f4d0 (an entry per assignment of a value to a variable). So all assignment entries of the
   function are numbered **after every walk entry** of the function. Then `HshBuildKillLists`. **[C]**
3. **An entry is new only when no equal one exists**: same opcode, type and source value numbers
   (`HshGetKey` 10b5f9b0 hashes `(opcode & 0xfff) + type + Σ leaf values` mod 101; `HshGetLeafVal` 10b5f940: a
   variable or temp is its SYM key, an integer constant its value; `HshFindEntry` 10b5fa00 compares). This is
   lexical: by symbol, not by the value it holds (an assignment in between does not make `a + 1` a new entry; the
   kill lists decide what is available). Hence per expression kind: **[C: code; probes p3]**

   | expression | temps |
   |---|---|
   | variable, parameter, constant, a global read directly (`gi`: a direct memory leaf) | none |
   | `a + b`, `a * b` (any operator, first occurrence; commutative operands ordered) | 1; repeated (`(a+b)*(a+b)`, a common subexpression, a compare spelled `b > a`): 0 |
   | `*p` | 2: `OPBASED [p+0]`, `OPLOAD` |
   | `p[1]` | 3: `OPADD p, 4`, `OPBASED`, `OPLOAD`; the same load again: 0 |
   | `a < b` feeding `if` | 1 in the walk (the branch condition) + 1 later (GdfInsertAssertions: the other arm) |
   | float arithmetic on 14.00/15.00 | each float→double `OPCONVERT` of an x87 operand is an entry (13.10 has none: fewer temps) |
   | parentheses around a float expression that is not a bare variable or load | 1 `OPPAREN` per pair (`/fp:precise` keeps them); none for ints |
   | `(float)fabs(x)` | `OPPAREN` of x, convert, `OPLOCAL`, the intrinsic, convert, parens of the call expression |
   | an inline helper's body | as if written at the call: its parameters are substituted before (the caller's value numbers), its locals give assignment entries |
   | dead code, `(void)(a + b)`, a dead local, `x + 0` | none (gone before the walk) |
   | a CSE candidate that is later not used, a value later folded | counted: the walk sees everything that reached it |
   | a single-use named local (`int c = a + b; return c;`) | none (propagated before the walk) |
   | a local used twice; `gi = c` | +1 assignment entry per assignment |
   | a `const T&` bound to a temporary | +1 walk, +2 assignment entries |

4. **Later passes add more**, all after the walk and its tail: `GdfInsertAssertions` 10b35240 (assertions on
   branch arms), the propagation sweeps (`GoptOptimizeBlock` → `GoptRehashLexicalHelper` 10b50900: a new entry for
   every expression and assignment copy or constant propagation rewrote; `GoptRehashMemRefs` 10b50630: `OPBASED`
   for rewritten memory operands), the loop phase (`LoptInit`, `LoptMakeIVorRC` induction values,
   `LoptReplaceInductionVariable` 10b42f50 calls `SymMakeHashVar` itself), `GoptRemoveAssumes`. A loop typically
   adds 10-16 such temps (p3 `i0_v20`). In 0x00806ab0 (j1): 6242 = 2633 walk + 374 tail + 3235 later
   (assertions and sweep 1 about 2700, the loop phase about 450; `hk.py --sum`). **[C: trace]**
5. **Pool and freeing.** `HshInsertEntry` 10b5f880 → `SymMakeHashVar` 10bef720 → `SymAlloc` kind 15: class hash,
   its own blocks of 32, no free list, so **key mod 32 = the temp's rank among the function's hash temps, and
   key & 3 = (number of hash temps made before it) mod 4**. `SymMakeHashVar` then sets the SYM's kind byte to 3
   (temporary). After the optimiser's last one, `GoptCleanUp` / `HshFreeSymsAndLeafTuples` 10b5ea60 free nearly all
   of them, and because of that kind byte they go on the **temp** class's LIFO list: the temporaries of pass 2
   (the OptFuncExpr of PreLowerGlobalOpts) are handed those keys. 0x00806ab0: 6198 of 6242 freed, 8985 temp
   allocations reuse a hash key. **[C: code, trace; replay exact]**
6. **Per statement and per block nothing is reset**; the table is per function (`HshInit`). A statement's temps
   are its distinct new values in tuple order; a block's are its statements'.

Consequences for steering:
- A walk temp's key depends on the walk entries before it in tuple order (and on the class's blocks, i.e. only on
  that count); an assignment-tail or later temp depends on the **whole** walk of the function and the tail before
  it. In 0x00806ab0 the four `[$o] * $T` cross sites (source 1191, 1265, 1412, 1486) are walk temps
  (`OPADD this, 8`, the address of a member); the sorts of lines 65-68 and 167-202 that a +3 also flips use later
  temps (keys 13.6k-13.9k).
- One more temp at a point moves every later hash key by one, later temps included, and with them the pass-2
  temporaries that reuse freed hash keys. To move only a range, compensate after it, **exactly** (net 0, not
  0 mod 4): 0x00806ab0 with +3 in the window and +1 after it (`c3`, net +4) still had two x87 differences (a
  temporary key in two radius products), +3 / −3 (`x5`) has none.

### 8.2 Levers (code-neutral constructs, measured with pr.py on all four builds)

| construct (at the statement where the temps should come) | 14.00 | 15.00 | 13.10.3077 | 13.10.3052 | code |
|---|---|---|---|---|---|
| one more pair of parentheses around a float subexpression that is not a bare variable or load: `((A) + (B))` for `(A) + (B)`, `d + (m * v)` for `d + m * v` | walk +1 | +1 | +1 | +1 | same (all builds) |
| two / three more pairs `((e))`, `(((e)))` (nested pairs are new values each) | +2 / +3 | same | same | same | same |
| parentheses around a variable, a load, an `int` expression | 0 | 0 | 0 | 0 | same |
| one pair fewer around a product in a sum, `(a * b) + c` → `a * b + c`; both products in `(e0 * f0) + (e1 * f1)` | −1; −2 | same | same | same | same (13.10: `d + m*d` shape 2) |
| `(float)` cast of a float expression | +1 | +1 | +1 | +1 | same |
| a float sum computed into a `double` local, then `(float)` | +1 | +1 | +3 | +3 | same |
| an inline `float Abs(float f) { return float(fabs(f)); }` instead of `(float)fabs(x)` | +2 per use (p2), +3 (p4) | same | +1 | +1 | same |
| parentheses around an inline call's result `(a.Dot(b))` | +1 | +1 | +1 | +1 | 14.00/15.00: shape 2; 13.10 same |
| a `bool` flag for a float compare (`bool hit = d > r; if (hit)`) | walk +1, tail +2, later +1 | same | same | same | same |
| a reference or pointer alias (`const V& aa = a;`, `const V* pb = &b;`) or a copy of a float parameter used in the compares | tail +1, later +15..+34 | same | tail +1, later +9..+26 | same | same (14.00/15.00) |
| dead expressions, dead locals, folded constants, a compare spelled reversed, a repeated spelling of a value | 0 | 0 | 0 | 0 | same |

The parenthesis rows are the cleanest: they add or remove exactly one walk temp at that statement and nothing
else (no frame, temp, global key; `whatif` can follow them as `add:hash@$oN:k` / `sub:hash@$oN:k` with `$oN` the
first value-numbering temp after the statement). `levers` prints these constructs for hash edits and says
whether the point is in the walk, the assignment tail or later (`/walk`, `/assign`, `/later`).

### 8.3 Other builds

| | 14.00 | 15.00.30729.1 | 13.10.3077 | 13.10.3052 |
|---|---|---|---|---|
| `HshBuildTable` | 10b5eb30 | 510ad6a0 | 107637f3 | 107a556f |
| `HshEnterAssign` | 10b5f4d0 | 510ae0d0 | 107617b5 | 1076f051 |
| `HshInsertEntry` | 10b5f880 | 510ae4a0 | 10761516 | 1076eecc |
| `SymMakeHashVar` | 10bef720 | 5113e460 | 107615bc | 1076ed2c |
| `HshGetKey` / `HshFindEntry` | 10b5f9b0 / 10b5fa00 | 510ae5d0 / 510ae630 | 10761253 / 10761176 | 1076ec25 / 1076eb09 |
| `GdfInsertAssertions` | 10b35240 | 51081f10 | 10762ea1 | 1077ea99 |
| `HshFreeSymsAndLeafTuples` | 10b5ea60 | 510ad5d0 | 107950e4 | 10798dd0 |
| walk, tail, later; kind 15, own blocks of 32, key mod 32 = rank; freed keys reused by temps | yes (code) | yes (trace) | yes (trace) | yes (trace) |
| counts per construct (8.2, tests/data/symkeys_hash.cpp) | | = 14.00 | = 14.00 except no float→double converts (fewer temps per float expression) and the `double` local / Abs rows | = 3077 |

Addresses from the PDB indexes (`work/index/*/c2.dll.csv`); on 15.00 and 13.10 only the behaviour was traced
(hooks at these addresses; every probe gives the same walk / tail / later split as 14.00), the code was read on
14.00. **[C: 14.00 code; others trace]**

### 8.4 Fallout 3 0x00806ab0: the three temps found in the source

j1 (vDCrossE before the switch, jumps right) needs, against best1, **+3 walk temps from vC00's statements to the
vC10 cross and −3 before the end of the walk** (best1 has the `??_H` loop's 3 temps right after vC00 and not at
the switch), plus the +17/18 frame keys. Measured with `work/scratch/hashkeys/ev806.py` (site ranks mod 4, walk
and total counts; regdiff), variants in `work/scratch/hashkeys/806/`:

| on jp18 (frame part by padding) | sites (Δ mod 4) | walk / all | regdiff | bcmp |
|---|---|---|---|---|
| jp18 | 0 | 0 / 0 | 45 | |
| +1 paren at each of the C00, C01, C02 radius lines (`radius = ((..) + (..));`) and +1 at C22's (`c3`) | 3 | +4 / +4 | 25 | 6 instructions (two radius products) |
| the same three, and at C22: no parentheses around the two products of `radius` and around `(maxTime) * (fC22Vel)` (4 uses) (`x5`) | 3 | 0 / 0 | **17** | **0 of 4406 differ** |
| x5 on j1 (no padding) | 3 | 0 / 0 | 39 (from 67): the frame sites | |

The source change (jp18 → `806/jp18_x5.cpp`; j1 → `806/j1_x5j1.cpp`, the same lines one earlier):

```
-    radius = (pExtents[1] * (float)fabs(axE[2][0])) + (pExtents[2] * ReworkAbs(axE[1][0]));
+    radius = ((pExtents[1] * (float)fabs(axE[2][0])) + (pExtents[2] * ReworkAbs(axE[1][0])));
     (the same for axE[..][1] (C01) and axE[..][2] (C02))
 ...
-    radius = (pExtents[0] * (float)fabs(axE[1][2])) + (pExtents[1] * ReworkAbs(axE[0][2]));
+    radius = pExtents[0] * (float)fabs(axE[1][2]) + pExtents[1] * ReworkAbs(axE[0][2]);
-                if ((fC22Dot) + ((maxTime) * (fC22Vel)) > (radius))
+                if ((fC22Dot) + (maxTime) * (fC22Vel) > (radius))
     (and the other three `((maxTime) * (fC22Vel))` of the C22 block)
```

So the hash part has a natural form: the window's radius sums are written with one more pair of parentheses
and the C22 block with fewer, i.e. the macro bodies were parenthesised differently. The frame part (+17/18
between case 1's `center` and the vC01 cross) is still the padding: on j1+x5 pads 17-20 give regdiff 16,
16 gives 28, 15 gives 52. Tried and rejected (code changes): reference locals in `Dot` and `EdgeCross`
(`d_*`: 70-141), more locals in the hit-point blocks of the switch (`m_*`: 101-338), float locals in EdgeCross
(`j1x5_E*`: 34 and worse).

**Model gap.** `compare --from jp18.keys.json` with the c3 placement as hash edits predicts all 794 orders of
best1; the compile differs in two radius products. Their operands are subtrees whose weight comes through a
chain of hash temps (paren, convert), which the model takes as unchanged (section 5). For hash edits, `whatif`
narrows the search; a compile decides. Measured against the traced x5 (`806/jp18_x5.keys.json`), x5 written as
edits on jp18 (`add:hash@$o20731:1 add:hash@$o20838:1 add:hash@$o20916:1 del:$o21406 del:$o21408 del:$o21415`,
the three new parens and the three dropped ones): 736 of 738 pass-2 orders predicted (4 of the 6 that really
change); of the 251 symbols whose key moved only 18 exactly, because the optimiser's own symbols after the edit
are not the same ones (`$o~k` pairing). **[C: compare run]**

## 9. Open

- Natural constructs that give exactly +16 frame keys in 0x00806ab0's switch were not searched exhaustively.
- Whether frame-slot seeds and Color ties follow keys anywhere else than through GLOBREG numbering: not checked.
- n-ary sorts: the fifth and later operands of a list are not logged (shown by kind; such a sort is not
  re-sorted). The input order of a list is not traced, so operands that end with equal weights are taken in the
  traced order.
- 13.10: the `free2` probe is the only path for non-temp frees seen. Member operands based on single-definition
  address temporaries (`[t+d]`) are taken as fixed on every build.

## 10. cl 17.00 x64

The x64 cross compiler (cl 17.00.61030 x86_amd64, `--c2 17.00` / `C2_BUILD=17.00`, table
`tools/c2builds/17.00.61030-x64.json`, generated by `work/port1700/mktable.py`; DLL layout docs/c2/port-1700.md).
`keys`, `sites`, `events`, `whatif`, `compare`, `export` and `levers` run unchanged on its traces; what differs is
in the build table (`hooks.symkeys` and the `symkeys_*` keys) and in parser switches those keys turn on.
**[C]** unless marked.

### 10.1 Hooks (`hooks.symkeys`)

| role | 17.00 VA | function | logged |
|---|---|---|---|
| init | 10093940 | `SymInit` | new table (per worker thread, see 10.2) |
| alloc | 100a8c60 | `SymAlloc` entry/exit | kind in cl, line offset (TLS+0xdcbc, the word `Tuple::CompleteInstr` copies to tuple+0x20), 16 stack words; key (+0x20), SYM |
| free | 10060f1b | `SymFree` chain loop | member key, head kind byte `[ebp-1]` |
| free2 | 100906bd | `SymGarbageCollection` (SymFree inlined) | member key (edi), head kind (ecx) |
| free3 | 100ab747 | `OptPureExpr` (inlined) | ecx, edx |
| free4 | 100a9310 | `OptRebuildTuples` (inlined) | esi, eax |
| free5 | 1009e087 | `LsraProcessIntervals` (inlined) | ebx, ecx |
| uservar | 100a8210 | `SymMakeUserVar` | SSR name `[ecx+4]`, SSR (ecx), FE key (+0x28); SYM |
| equiv | 1018a390 | `SymNewEquiv` | sym, root (+0xc), offset (s2), root's offset (+0x28); view, type (+0), size (+0x24) |
| splice | 100ba7b0 | `InlineInlinee` | callee name (call tuple s3, src +0x30, code leaf +0x24 = SSR, name +4), line (+0x20) |
| getinl | 10094f00 | `GetInlinee` | the same from CANDIDATES (ecx) +4 |
| read | 10126e20 | `RdrReadProc` | nested = an inlinee |
| opt | 100a74b0 | `OptFuncExpr` | `tdccc>0>0>4$` (CurFunc is in TLS) |
| gc | 10090610 | `SymGarbageCollection` | |
| hshwalk / hshasg | 100a4b80 / 100d8d20 | `HshBuildTable` / `HshEnterAssign` | |
| pool | 10093a80 | `SymAllocPool` exit | first key of the block (`eax>4>0x20`) |
| sort | 1016ede0 | `MscMergeSort<Tuple>` called from 0x100ac143 (`symkeys_sort_ret` 100ac148) | node edi: opcode (+8), line (+0x20); per operand kind (+0xc), weight (+0x18), leaf key (+0x24>0x20), first source kind / key; the two displacements (+0x38) |
| sortx | 100abf60 | `OptLeftWeightTree` exit | sources of the first two operands (memory: base +0x30, displacement +0x38) |
| sort3 | 100ac148 | probe at the sort's return | third and fourth operands, fifth one's kind |
| (skip) | 100a8bc0 / 100a7370 / 100a7410 : 5, 100a7260 : 7, 100cec10 : 4 | SymMakeTmpReg / SymMakeTmpVar / SymMakeHashVar, Tuple::MakeTmpReg, Tuple::MakeTmpVar | forwarders between SymAlloc and the creator |

Every hook has 6 or more bytes of whole instructions and no branch into them (`work/port1700/hookchk.py`). The
operand sort is the same: OptLeftWeightTree (0x100abf60) sorts the source list of a commutative pseudo-opcode
(OPADD .. 0x540, opcode - 0x52c through the table at 0x100ac2a0) with `OptCmpHashVal`; the weight is
`SU << 24 | count << 16 | hash` at operand+0x18, and OptHashNode (0x100ac8c0) has 14.00's formulas: a named leaf
`((k & 0x7ff) << 5) ^ (k >> 11)` of the folded key, a temporary of kind 3 without a single definition `k << 6`, a
memory leaf `fold(d) + (opcode - 0x4f6) + (hash(base) << 8)` (= `d + 8 + ...`, as 14.00), an instruction
`Σ hash(src_i) << (i & 7) + opcode - 0x4f6`. So `symkeys_*` hash code and `op_weight` apply as they are.

### 10.2 Where the x64 key allocator differs

| | x86 (14.00 / 15.00) | 17.00 x64 |
|---|---|---|
| where the table lives | globals | `[TLS+0x11534]` of the compiling thread: +0 next key, pools +0x18 frame, +0x1c glob, +0x20 hash, +0x24 tmp, +0x28 other; free lists +0x30 frame, +0x34 glob, +0x2c tmp, +0x38 other |
| threads | one | each function is read, optimised and compiled on one of the 4 workers; every worker has its own table and SymInit per function. The parser splits the log by `# thread N` and orders functions by their SymInit |
| block size | 32 SYMs | 32 (SymAllocPool: 0xc8c bytes = 12-byte header + 32 SYMs of 0x64; keys numbered from +0 at block creation) |
| initial blocks | frame 0-31, glob 32-63, tmp 64-95, other 96-127; key 0 a dummy | the same; key 0 is SymInit's `SymAlloc(4)`, marked free (kind 0x10) but on no list; the hash pool is taken at the first hash temp |
| kind → class | table 0x10bef395 | the same (0x100a900d: 0-3, 6 tmp; 4, 5, 13 frame; 7, 8 glob; 9-11 other; 15 hash) |
| free-list choice | head kind - 3 through 0x10bef44c (6: none) | head kind byte: 3 tmp, < 6 frame, 6-8 glob, else other (`symkeys_free_by: "kindmap"`, `symkeys_free_map`); the inlined copies use a jump table with the same classes for 3-11 (6 there is a null entry, never reached) |
| free-list order | LIFO per class | LIFO per class (the SYM's +0x2c links the list) |
| where frees happen | SymFree | SymFree plus four inlined copies of its loop (free2..free5 above). Without them the replay failed: 35 of 61 keys of `S::meth` in tests/data/symkeys.cpp, 28 of 49 of `kparams` |
| pass 1 / pass 2 | OptFuncExpr twice | the same (pass 2 after HshBuildTable and SymGarbageCollection) |
| reader globals | first reference | the same, then at the end of the reader `DummyGlobal` and `DummySafeGlobal` take the next two glob keys in every function (32, 33 in 104 of 168 functions measured) |
| temporaries | | OptPureExpr makes a temp per pure tree and frees it at once (three allocations of the same key in a row are common); OptRebuildTuples, GdfInstrSdsu and the hash pool's frees (to the tmp list, as 8.1.5) as on x86; LsraProcessIntervals frees after pass 2 |
| SYM record | key +0x1c (il_sym.md) | 0x64 bytes: type +0, SSR +4, kind +8, root SYM +0xc, chain next +0x10, single definition +0x18, key +0x20, size +0x24, offset +0x28, free link +0x2c |
| tuple kinds in the sort | an instruction operand is kind 13 | kind 12 (x64 has one leaf kind fewer, mask 0xffe): raw kinds >= 12 are shifted up by one (`symkeys_kind_shift: [12, 1]`) |
| memory operand of a sort | a subtree's source | often the leaf itself (no float to double conversion): its base sits at +0x30 like a source list, so the sort's "first source" fields are the base (read so when `symkeys_kind_shift` is set); its displacement is logged separately (`symkeys_sort_disp`) |
| field view offset | relative | SymNewEquiv's offset is the absolute one; the name uses offset - root offset (`symkeys_equiv_rel`) |
| names | `_x` (C decoration) | as c2 has them: `x`, `this`, some C symbols with a scope suffix (`val$1`); a c1xx temporary without a name is `$T<fekey>` as before |

Rules 1-7 of section 0 and the construct table of section 4 hold on the probes measured (tests/data/symkeys.cpp:
parameters in reverse, `c:1 b:2 a:3 r:4 q:5`; an unused local takes no key, an initialised dead one does,
`b:1 a:2 d:3 r:4`; `r@S::meth#1:7`; `t@dot#1:8 t@dot#2:9`; call arguments inlined right to left, tB 5 < tA 7).

The first OptFuncExpr of a function is called from `Vectorize_Finish` (return 0x1009b4d7), the second, pass 2, from
`dop2_PreLowerGlobalOpts` (0x1004efb2), traced on a one-function probe; `sites --pass 2` is the one the code
follows, as on x86.

### 10.3 Measured

Replay (`keys`: `replay: N/N keys reproduced`; one traced compile per file, local wine, /O2):

| input | functions | keys replayed | pass-2 sorts |
|---|---|---|---|
| tests/data/symkeys.cpp, symkeys_nary.cpp, symkeys_hash.cpp, symkeys_glob.cpp, tests/data/ncol/c1.c, sum.c, c2.cpp, fp.cpp | 52 | 21 813 / 21 813 | 670 (10 n-ary) |
| zlib 1.2.1, all 14 .c files (`/I` the zlib folder) | 116 | 124 411 / 124 411 | 3 202 (60 n-ary) |

Every allocation and free of every function is reproduced, optimiser temporaries and the hash class included.
Of 129 822 zlib allocations 107 163 are tmp, 14 487 hash, 2 226 frame, 533 glob, 2 other; 112 611 frees go to the
tmp list and 32 to the frame list.

What-if (`compare`: the base trace plus the edit against a traced compile of the edited source; keys = the symbols
`keys` shows that exist in both, sorts = pass-2 sorts with the same operand set in both):

| edit (function) | as | keys | pass-2 orders | really changed, predicted |
|---|---|---|---|---|
| symkeys.cpp: `float e = 1;` before r (kdead), `float e = 2;` in dot (kinl) | `add:frame@r:1`; `add:frame@t@dot#1:1 add:frame@t@dot#2:1` | 8/8, 18/18 | 2/2, 12/12 | 0, 0 |
| `int p0 = 0, .., p29 = 29;` before a local t compared with a global (three probes: `t * gf`, `(t + gi) * a`, `t * gf + u * gf`) | `add:frame@t:30` | 23/23 | 5/5 | 4, 4 (t's key jumps from 2-4 to the next frame block, 128-130, and hashes above the global's key 33) |
| 1, 3 dead initialised locals at the top of adler32, inflate_fast, longest_match, send_tree; 1, 5, 17 in inflate; 2, 7 in compress_block; 6, 30 in inflate_fast; 5 in send_tree | `add:frame@<next frame symbol>:k` | 774/774 | 2 356/2 356 | 4, 4 (send_tree +3, +5: `len` 29 crosses into the next frame block, 544, and now hashes above the temp `$o1659`, key 1149) |
| `int d0 = 0, d1 = 1, d2 = 2;` before r in `r * s + a` (k_dead) | `add:frame@r:3` | 9/9 | 4/4 | 0, 0 |
| 3 or 6 unused parameters between p and q (`p->x * q->y + p->y * q->x`, `p->fx * q->fx`) | `add:frame@p:3`, `:6` | 12/14 | 6/6 | 0, 0 |
| locals first referenced in another order (`int y = g2(); int x = g1();`) | `swap:y,x`, `swap:c,a` | 11/11 | no common sort | |

In all, 855 of 857 keys and 2 385 of 2 385 pass-2 orders, the 8 orders that really changed all predicted. The two
keys missed are optimiser temporaries of the unused-parameter probe: the parameters' new keys flip a pass-1 sort,
which changes the tuple order the optimiser numbers its temps in (section 5: optimiser symbols are right only while
the edit does not change what the optimiser makes). Few orders change because on x64 most commutative operands are
temporaries that hash by their definition, constants, or `[$o+d]` members whose base is an address temp (the x64
code loads a pointer member's address into a temp first: `p->fx * q->fx` sorts `[$o22 key 64+0]` against
`[$o21 key 65+0]`, not `[p+0]` against `[q+0]`), and a pad at the top shifts every later frame key alike; what flips
orders is a key crossing into a later block (rule 6) against an operand of another class.

### 10.4 Not covered on x64

- `levers` runs (it only replays) but was not measured against compiles here; the construct table of section 4
  and the hash-temp levers of 8.2 were not re-measured beyond the rows above.
- A memory leaf's displacement is logged at the sort (`symkeys_sort_disp`), not in `sort3` (shown as `+?` for the
  third and fourth operands when absent); the hash itself is recorded, so the model is not affected.
- Sorts whose first source changes with the edit (an inner sort flips, so the outer OPADD's subtree is named by
  another symbol) are not paired by `compare` (k_two: 3 of its 4 sorts paired).
- The creator column stops after 16 stack words; `Tuple::MakeTmpVar` and deeper forwarders can still appear as
  the creator.
- tests/run_tests.py's symkeys checks expect x86 names (`_c:1`, `_r@S::meth#1:7`); on x64 the same rows read
  `c:1`, `r@S::meth#1:7`, so the tests would need x64 expectations before they run on this build.

## 11. cl 13.00.9044 (VC6 Processor Pack 5)

The back end of Visual C++ 6.0 Processor Pack 5 (`c2.dll` 13.0.9044 behind cl 12.0.8804, `--c2 9044` /
`C2_BUILD=13.00.9044`, table `tools/c2builds/13.00.9044.json`, generated by `work/port1300/mktable9044.py`; layouts
docs/c2/port-1300.md). `keys`, `sites`, `events`, `whatif`, `compare`, `export` and `levers` run on its traces;
what differs is in the table (`hooks.symkeys`, `symkeys_*`) and in four parser switches those keys turn on. Every
address below was read in the 9044 code next to 3077's (`work/port1300/hookchk9044.py`: 150 hook addresses, whole
instructions over 5 bytes, nothing branches into them, no overlaps). **[C]** unless marked.

### 11.1 Hooks (`hooks.symkeys`)

| role | 9044 VA | function (how it was identified) | logged |
|---|---|---|---|
| init | 107190df | `SymInit` (vt9044 1.0; four `SymAllocPool` calls into frame / glob / tmp / other, then `SymAlloc(4)` marked free) | |
| alloc | 10701701 | `SymAlloc` (match9044; kind in `cl`, class switch 10704600 through the byte table 10704615) | kind, `_LineOffset` 107c44b4 (word; 3077 108a9120), 9 stack words; key (+0x1c), SYM |
| free / free2 | 1071d352 / 1071d37b | `SymFree` 1071d343: its temp-list loop (head kind 3) and its frame-list loop (any other head) | member key, kind byte |
| uservar | 10733750 | `SymMakeUserVar` (match9044; `ecx` = the c1 symbol record SSR, `[SSR+8]` its SYM) | name `[SSR+0x18]`, SSR, c1 number `[SSR+0x28]` (3077's offset); SYM |
| equivp | 1071b35c | `SymMakeEquiv` 1071b2e7, after its inlined SymNewEquiv's `SymAlloc` | view, root (`edi` = `[sym+8]`), absolute offset (`ebp`), type word (`dx`), size (`[esp+0x1c]`) |
| splice | 1075d1dc | `InlineInlinee` (its caller 1075cd17 calls GetInlinee first; same body as 3077 8dc87: call tuple, line, MergeSymTab) | callee name (call tuple s3 src `+0x1c` > leaf `+0x14` > `+0x18`), line (`+0x10`) |
| getinl | 1075ceda | `GetInlinee` (vt9044 high; `ecx` = CANDIDATE, `[ecx+4]` the call) | the same from `ecx>4` |
| read | 1073426e | `RdrReadProc` (`_FromInlining` test, new FUNC of 0x68 bytes, line offset := 0) | |
| opt | 1071d3dd | `OptFuncExpr` (`edx` = pass; pass 1 from 1071ce6a, whose function falls into SymGarbageCollection, pass 2 from dop2_PreLowerGlobalOpts 1073e11a) | name `[[FUNC]+0x18]`, base line `FUNC+0x24` |
| gc | 1071ce7b | `SymGarbageCollection` (match9044; dop2_PreLowerGlobalOpts 1073e109) | |
| hshwalk / hshasg | 107338f0 / 107224d5 | `HshBuildTable` (GlobalOptimize 1073e27e; walk, then every OPASSIGN 0x25d through `HshEnterAssign(ecx, 1)`) / `HshEnterAssign` | |
| pool | 1071c6fb | `SymAllocPool` exit | first key of the block (`eax>4>0x1c`) |
| sort | 10725527 | `MscMergeSort` (`ecx` list, `edx` compare = OptCmpHashVal 10724175, unsigned weight at +0xc, descending) called from 1071fedf (`symkeys_sort_ret` 1071fee4) | entry: return address, weight / kind / memory base key (+0x30) / displacement (+0x2c) of the first two list members, each one's second-source base key; exit: node (`ebp`) opcode and line (+0x10), per operand kind, weight (+0xc), leaf key (+0x14>0x1c), first source kind / key (+0x1c) |
| sortx | 1071fd83 | `OptLeftWeightTree` exit | first two sources of the first two operands (as 3077's, offsets -4 / -0xc) |
| sort3 | 1071fdfc | the join after the sort (`mov ecx,ebp; call OptExpr2`) | third and fourth operands, fifth one's kind |
| immnew, immwalk, immfree, newgr | 1072e93b, 10730589, 1073059c, 1070637f | `keys --imm` only (11.5): GrgRewriteImmedAsReg, the bucket walk of 1073053e and its frees, GrgNewGlobreg | |
| (skip) | 1070260e : 3, 10722a72 : 3, 107025f6 : 2, 1071329e : 2 | SymMakeTmpReg, SymMakeHashVar, TupMakeTmpReg, TupMakeTmpVar | forwarders between SymAlloc and the creator |

There is no PDB, so the `made by` column names come from the table's `functions` (the hooked ones and the main
creators: TupMakeTmpBinary 1071d002, TupMakeTmpUnary 107024d5, OptPureExpr 1071d5cd, OptRebuildTuples 1071feec,
GdfInstrSdsu 107257be; vt9044 / match9044 names, read only as far as their call to TupMakeTmpReg **[I]**); other
creators print as the hex return address.

### 11.2 Where the allocator differs

| | 13.10.3077 | 13.00.9044 |
|---|---|---|
| SYM | 0x60 bytes | **0x58**: kind byte +4, root +8, chain next +0xc, type +0x10, single definition +0x14, key +0x1c, size +0x20, offset +0x24, free link +0x28 |
| block | 32 SYMs | 32 (`SymAllocPool`: getmem 0xb0c = 12-byte header + 32 x 0x58; keys from `_SymCount` 107b16b8) |
| initial blocks, key 0 | frame 0-31, glob 32-63, tmp 64-95, other 96-127; key 0 SymInit's dummy | the same; the hash pool (107b1698) starts empty and takes a block at the first value-numbering temp |
| kind -> class | 0-3, 6 tmp; 4, 5, **13** frame; 7, 8 glob; 9-11 other; 15 hash | the same except 13: kinds 12-14 index a null jump-table entry (never allocated) |
| free lists | tmp and frame; SymFree puts every non-temp chain on the frame list | the same (`_SymTmpFreeList` 107b168c, `_SymLocalFreeList` 107b1688), both in SymFree; nothing else references them, so there are **no inlined copies** of its loop (17.00 has four) |
| reader globals | first reference | the same, then at the end of the reader two unnamed c1 globals (SSR kind byte 1, flags 0x26, the same records in every function) take the next two glob keys: 32 and 33 in a function that names no global. They are listed as `$T<c1 number>` |
| field views | one per field read, no rest views | as 14.00: a rest view first (`_v+4.b20`: the 20 bytes from +4), then the field (`_v+4.i4`); names carry the type: the TY class nibble is 1 int, 2 unsigned, 3 pointer, 4 float, 5 block (`symkeys_tycls`; 14.00: 5 float, 6 block) |
| inlining | GetInlinee reads, InlineInlinee splices; parameters freed at the splice | the same (symkeys.cpp: `_r@S::meth#1:7`, `_t@dot#1:8 _t@dot#2:9`, call arguments right to left) |
| tuple kinds | an instruction operand is 13 | 12 (kinds from 0xc are 13.10's minus 1: `symkeys_kind_shift: [12, 1]`); a memory leaf's base is at its own +0x30, not at the source list as on 17.00 (`symkeys_mem_src: false`) |

The VC6 front end emits no body for an inline function nobody calls out of line, so tests/data/symkeys.cpp has 6
functions on this build (10 on the others).

### 11.3 The operand hash (OptHashNode 1071f7f0)

OptLeftWeightTree 1071fd83 keeps the weight at operand +0xc (`SU << 24 | count << 16 | hash`) and sorts the source
list of OPADD .. OPNE (opcodes 0x26e..0x280, test 1071ee5d). OptHashNode switches on the leaf kind (table
10726f38):

| operand | hash (low 16 bits) |
|---|---|
| named variable, field view (kind 1, 2) | `((e & 0x7ff) << 5) ^ (e >> 11)`, `e = (key & 0xffff) ^ (key >> 16)` (as 14.00) |
| temporary (SYM kind 3) without a single definition | `key << 6` |
| a symbol with a single definition (SYM +0x14) | its definition's hash |
| `&x` (kind 3) | `e` itself, not shifted |
| memory `[base+d]` (kind 5, 6) | `fold(d) + opcode - 0x247 + (hash(base) << 8)`: OPBASED is 0x24e, so `d + 7 + ...` (14.00: `+ 8`) |
| integer constant | the folded 64-bit value |
| instruction | `Σ hash(src_i) << (i & 7) + opcode - 0x247` |

So the model's hash code and `op_weight` apply unchanged. OptExpr (1071d4a0, OptLeftWeightTree's only caller) is
also called by three later passes (10728373, 1072b424, 10786e09), so the sorts after the GC between the two
OptFuncExprs count as pass 1 and those after pass 2 as pass 2 (sites shows them all; compare pairs by operand set).

### 11.4 Measured

Replay (`keys`: `replay: N/N keys reproduced`; local wine, /O2):

| input | functions | keys replayed | pass-2 sorts |
|---|---|---|---|
| tests/data/symkeys.cpp, symkeys_nary.cpp, symkeys_hash.cpp, symkeys_glob.cpp (`/TP /MT /GS /EHsc /Gy`) | 29 | 3 599 / 3 599 | 175 (13 n-ary) |
| tests/data/ncol/c1.c, sum.c, fp.cpp (c2.cpp: `long long` is not VC6) | 15 | 3 539 / 3 539 | 107 (7 n-ary) |
| zlib 1.2.1, all 14 .c files (`/I` the zlib folder) | 115 | 89 403 / 89 403 | 2 640 (46 n-ary) |

Of zlib's 89 403 allocations 76 725 are tmp, 11 132 hash, 1 021 frame, 520 glob, 5 other; all 75 394 frees go to the
tmp list (the tests/data files have 27 frame-list frees: inline parameters freed at the splice, section 0 rule 5).

Value numbering (tests/data/symkeys_hash.cpp): `hb` has 51 walk, 4 assignment, 6 later temps, as 13.10 (no float to
double converts), and the `double` local costs +3 as on 13.10; one difference from every other build: parentheses
around a load (`(e[0])` in `hleaf`) are one walk temp more, an OPPAREN of VC6's front end. Key mod 32 = rank, and
freed hash keys reused by temporaries, as 8.1.

`C2_BUILD=13.00.9044 C2TRACE_REMOTE=0 uv run tests/run_tests.py` runs the symkeys checks (no `lacks` entry any more;
9044 expectations: 6 functions in symkeys.cpp, the hash counts above, and the `--imm` dial check of 11.5), and with
them the key checks of realize2, sulever and effectmine: 194 of 194 pass.

What-if (`compare`, prediction from the base trace plus the edit against a traced compile of the edited source):

| edit | as | keys | pass-2 orders | really changed, predicted |
|---|---|---|---|---|
| symkeys.cpp: `float e = 1;` before r (kdead), `float e = 2;` in dot (kinl) | `add:frame@_r:1`; `add:frame@_t@dot#1:1 add:frame@_t@dot#2:1` | 9/9, 22/22 | 2/2, 11/11 | 0 |
| 1, 3 dead initialised locals at the top of adler32, longest_match; 1, 3, 6, 30 in inflate_fast; 1, 3, 5 in send_tree; 2, 7 in compress_block; 1, 5, 17 in inflate | `add:frame@<first local>:k` | 739/739 | 2 334/2 334 | 0 |
| 30 dead locals before t in `t * gf` with t assigned twice (hashes by its key) | `add:frame@_t:30` | 7/7 | 2/2 | 1, 1 (t's key 3 -> 129: h0060 -> h1020, now above `gf`'s h0400) |
| the same in `t * gf + u * gf`, and three probes with a single-definition t (`t * gf`, `(t + gi) * a`, `t * gf + u * gf`) | `add:frame@_t:30` | 42/42 | 13/13 | 0 |
| 3 / 6 unused parameters between p and q (`p->x * q->y + p->y * q->x`, `p->fx * q->fx`) | `add:frame@_p:3`, `:6` | 12/14 | 6/6 | 0 |
| `int d0 = 0, d1 = 1, d2 = 2;` before r; `float y = g2(); float x = g1();` | `add:frame@_r:3`; `swap:_y,_x` | 16/16 | 3/3 | 0 |

In all 847 of 849 keys and 2 371 of 2 371 orders; the two keys missed are optimiser temporaries of the
unused-parameter probe (`$o~1` / `$o~2` made in the other order), as on 17.00. The member operands here are
`[$o key 64+0]`: the address `p + d` goes to a temporary first, which hashes by its definition (h0077 with p key 2,
h0137 with p key 8), the case the model takes as fixed (section 5).

### 11.5 The typedef dial: constants held in registers, and why the window is 8..32 (mod 64)

Question (asked by a decompilation of a VC6 /O2 game, docs/tickets/T18): in one of its functions the number N of
`typedef int dN;` lines in front decides the order of two tied immediate webs, -1 and 1 (`or ebx,-1` / `mov ebp,1`
at a join swap), and the game's order holds exactly when N mod 64 is in 8..32. Which key decides it?

**No SYM key does.** A typedef takes no key: compiled with 0 and 20 typedefs, every SymAlloc / SymFree of every
function is identical except the c1 numbers (SSR +0x28) of the per-function dummy globals, which move by one per
typedef (`$T328` -> `$T348`). The constants are not allocated by SymAlloc at all. What moves is the **c1 symbol
number** of other symbols, through Globregs' immediate table and the globreg free list: **[C: code and traces]**

1. `CmdIdentifyImmedCandidates` (1072e5e5, Globregs 1071be9e, after zeroing 64 buckets at 107ad868) walks every
   instruction's sources; for an integer constant (leaf kind 7), an address `&x` (kind 3) or a code address (kind 4)
   that `CmdCanEnregImmed` accepts, `GrgRewriteImmedAsReg` (1072e93b) looks the value up in bucket
   - `value & 63` for a constant (-1: 63, 1: 1),
   - `[SSR(root of x)+0x28] & 63` for `&x`, `[record+0x28] & 63` for a code address: the **c1 number**,
   and on a miss makes a SYM of kind 0xd with getmem (no key; its +0x28 is a copy of the leaf) and numbers its web
   (`GrgNewGlobreg`: the next number, `_NextGlobregNum` 107ad96c). So the constants' own webs are numbered in
   first-reference order and nothing about them depends on N.
2. After the lifetimes, 1073053e (Globregs 1071bf48) walks the buckets 0..63, each chain from its head (the
   immediate made last first), and frees every immediate whose reference count (record +0x14) is below 1:
   `ColRemoveGlobregFromHashTable(web, 0)` clears the record and pushes it on `_FreeGlobs` (107c4658), **keeping its
   number** (+0x1c). Typically these are addresses used once (`push offset ?ga@@3HA`).
3. `GrgNewGlobreg` (1070637f) takes `_FreeGlobs` first, last freed first. The webs numbered after the walk get those
   numbers: the variables' webs (1073053e's second loop, SYMs in key order) and Color's split pieces
   (NewLiveRanges), immediates' pieces included.
4. Color's tie-break: `InsertInList` orders by prio, then defOrd; webs equal in both (immediates and their pieces have
   def 0) are inserted in number order (traced: InsertInList is called in increasing number) and the one inserted
   last, **the larger number, pops first** (regalloc.md section 3.4).

So N moves an address immediate's bucket `(c1 + N) & 63`; when `c1 + N` crosses a multiple of 64 that immediate goes
from the last bucket freed to the first, the free list's order changes, and the pieces made later swap numbers. **The
order can change only at the N where `(c1(a) + N) mod 64 = 0` for a freed address immediate a**, i.e. N ≡ -c1(a)
(mod 64) with c1 taken at N = 0. A window 8..32 means two such immediates, with c1 ≡ 56 and c1 ≡ 31 (mod 64): at
N = 8 the first one wraps to bucket 0 (its number is handed out last instead of first), at N = 33 the second one
does, which restores the relative order of the two numbers the tied -1 / 1 pieces take; the period is 64 because the
table has 64 buckets. The peer's list (game order at 8 20 32 72 80 81 96, swapped at 0 7 33 71 97 103 104) fits 8..32
mod 64.

**Which nodes the operand sort reorders (9044).** OptLeftWeightTree's commutativity test (1071ee5d: `opcode -
0x26e`, byte table 10726e0c) accepts OPADD, OPMUL, OPAND, OPOR, OPXOR, OPEQ and OPNE only. A compare used as a
condition (`if (a == b)`) reaches it as OPCMP feeding the branch, which is not sorted: its operand order is kept. Which
of two call operands is *evaluated* first is decided right after, by weight, when the trees become tuples again
(OptRebuildTuples, 11.7); `sites` lists that decision in its own section. A value compare (`bool t = (a == b);`) is an
OPEQ and is sorted by key / hash. **[C]**

**Only symbols the front end makes after the inserted declarations shift.** N moves c1 numbers by N only for
symbols created after the dial's declarations (the function's own string literals, statics, later externs); an
address whose symbol was made earlier in the unit (e.g. a class's vtable, emitted with the class definition)
keeps its c1 number and its bucket at every N. Predict the edges from the movable immediates only: on four more
game functions of the same kind, the edges predicted from `keys --imm` at N = 0 with this rule were 23 of 23
compiles (one window predicted 44..54 without it, 44..53 with it). In three of them the swapped pair was two
enterloads at a join (`mov edi,_this; mov ebp,_src` in piece-number order); within one bucket the record
inserted later is walked first.

Reproduced on `work/port1300/dial9044.cpp` (r2: a loop with eight `gp(&gvK)` calls whose addresses are used once, and
-1 / 1 / 0 / 32 held in registers or split; the tied pair there is the pieces of 0 and 32, prio 0, def 0, benefit -1
and -2; both stay in memory, so the code does not change, only Color's pop order): `uv run python
work/port1300/dial9044.py work/port1300/dial9044.cpp r2` lists the freed addresses' c1 numbers (318 287 330 298 322
292 301 325 -> buckets 62 31 10 42 2 36 45 5), predicts the order can change only at N = 2 19 22 28 33 54 59 62
(= -c1 mod 64), and every one of the 64 compiles N = 0..63 changes the pieces' numbers exactly there; the traced pop
order of the tied pair (`regs`, `@Color@4+pop`) is the larger number first in all 64: 0's piece first at N = 0-18,
22-27, 33-53, 62-63, 32's at 19-21, 28-32, 54-61.

To find the two immediates of the game function: `symkeys.py keys SRC --func F --imm -- FLAGS` prints the immediate
table: every constant / address in first-reference order with its c1 number, bucket, references and web number, which
ones the walk frees (in bucket order, then the free list's order), and the numbers each later web and piece got
(`reused` = a freed one). At count 0, the freed addresses with c1 ≡ 56 and ≡ 31 (mod 64) are the ones; declaring
either symbol earlier or later, or one more / one fewer used-once address immediate, moves the window, and none of
this touches a SYM key. `--imm` is 9044-only (the immediate roles are in no other table; without the option the
trace is unchanged).

### 11.6 Not covered on 9044

- `levers` runs (tests: `--var` on an optimiser temporary, `--site` on the n-ary add) but was not measured against
  compiles; the construct table of section 4 was checked only through the symkeys.cpp rows.
- `--imm` is not followed by whatif / compare: an edit that adds or removes an address immediate or changes its c1
  number is predicted only by the rule above, not by the replay.
- The rule probes of tests/data/rules (`RX_*`, R310, R480) have no 9044 run here.
- `whatif` / `compare` do not predict the evaluation order of 11.7 (only the commutative sorts); read the keys off
  `sites` and apply the formula there by hand.

### 11.7 Which of two calls feeding a compare is evaluated first (OptRebuildTuples)

Question: two calls of one member function through the same object feed an `if` compare
(`L.Lookup(&elem[i]) == L.Lookup(&h)`, typically an inlined `operator==` inside an inlined find loop). With dead code
added before or after (dead `{ int dN = c * k + 1; }` blocks), the code calls the element's lookup first in one
variant and the handle's in the other. Which decision is that? **[C: code read, traced on
`work/port1300/evalorder9044.py`]**

**Where.** In OptFuncExpr pass 2: the IL at `plgo.SymGarbageCollection` still has the reader's order, at
`plgo.OptFuncExpr2` (and Codegen) the moved one. OptExpr (1071d4a0) takes the compare: OptPureExpr builds its tree
(a temporary with a single definition is replaced by that definition's tree, so both OPCALLs and their OPARGs become
part of the OPCMP's tree), OptLeftWeightTree weights every node (and sorts nothing here: OPCMP is not commutative),
then **OptRebuildTuples** (1071feec) makes tuples again, subtree by subtree, in front of the compare. For a binary
node (TupIsBinary 10720358: OPADD..OPSHR, OPCMP, OPEQ..OPGE, i.e. 0x26e..0x284 except OPMULHI and 0x27a..0x27d) it
calls `OptCmpHashVal(src0, src1)` at 10720133 (10724175: 1 when weight(src0) < weight(src1), unsigned, 0 equal,
-1 above), and when that is positive and the node's type class is not float (`ty & 0xf000 != 0x4000`) it rebuilds
the **second operand's subtree first** (10720160..1072018c), then appends the two operands in their old order.
Otherwise (equal or src0 heavier) the operands are rebuilt in order. Not copy propagation, not code motion: the
compare's operands stay `OPCMP t_elem t_h` in both variants; only the emission order of the two call trees changes.
After register allocation the first call's result waits in a callee-saved register, so the `cmp` operands read the
other way round (`cmp ebp, eax` / `cmp eax, ebp`). The same test sits in the tuple-kind 0xe/0x12 path at 107869ff
(not probed).

**Formula.** The weight is `SU << 24 | count << 16 | hash` (11.3). Two calls of the same shape (one object, one
argument each) have equal SU and count, so the 16-bit hash decides. An OPCALL's hash is
`Σ hash(src_i) << i + (0x286 - 0x247)` over code address (i = 0), `this` (i = 1) and the arguments (i = 2..), an
OPARG's `hash(x) + 0x15`, all mod 0x10000. Everything but the argument is common, so with
`C` = the common part:

    hash(call) = C + 4 * (hash(arg) + 0x15)  (mod 0x10000)
    the handle's call is evaluated first  <=>  hash(call(element)) < hash(call(handle))   (the element written first)

with `hash(arg)` as 11.3: a named variable or parameter of key k `32k` (folded), a temporary with several
definitions (a strength-reduced loop pointer `$o..`) `64k`, `&x` the folded key itself, a single-definition temporary
its definition's hash. The shift by 2 drops the top bits: a loop pointer counts only by `k mod 256`
(`(k << 8) & 0xffff`), a named pointer by `k mod 512`. In the probe (element argument: the loop pointer, handle
argument: the pointer variable `hp`): **element first iff `2 * (k_iv mod 256) >= k_hp mod 512`** (up to the wrap of
`C + ...` past 0xffff). The order flips when either key crosses that line, i.e. when a 32-key block boundary moves one
of them: the loop pointer is an optimiser temporary of the tmp class, so dead code before *or after* the loop moves
it by whole blocks (`+32`), while `hp`'s frame key moves by one per dead local before it.

Measured (`evalorder9044.py K1 K2`, `/O2 /GR`; `C` = 0x1b5 there):

| K1 (before) | K2 (after) | loop pointer key | hp key | weights element / handle | evaluated first |
|---|---|---|---|---|---|
| 0 | 0 | 156 | 4 | | element |
| 1..16 / 17 / 18..32 | 0 | 188 / 220 / 252 | 5..20 / 21 / 22..164 | K1 = 32: hfe09 / h5409 | element (every K1) |
| 33..40 | 0 | 284 | 165..172 | K1 = 33: h1e09 / h5489 | handle |
| 32 | 4, 16 | 284 | 164 | h1e09 / h5409 | handle |
| 32 | 40 | 380 | 164 | h7e09 / h5409 | element |

All 41 + 3 compiles agree with the formula (2·252 = 504 >= 164; 2·28 = 56 < 165; 2·124 = 248 >= 164). The machine
code follows: `push ebx (pointer); call; push eax (hp); call; cmp ebp, eax` at K1 = 32, `push eax (hp); call;
push ebx; call; cmp eax, ebp` at 33.

**What it needs.** Both compare operands must be single-definition temporaries when pass 2 runs, i.e. the calls are
compared directly. A result stored first (`int x = L.Lookup(&h); if (L.Lookup(&e[i]) == x)`, or an inline `Get()`
returning through a c1 temporary `$T`, `return Get() == o.Get();`) stays a separate statement in front of the
element's call at every key on this build (61 compiles, K1 = 0..60: the variable is never propagated;
GoptLocalForwardCopyPropagation 10725e17 folds a single-definition destination only into the next instruction of its
block). If the IL at `@dop2_PreLowerGlobalOpts@4` shows such a variable and the Codegen IL compares the call result
directly, look at `plgo.GlobalOptimize` for the pass that removed it **[I: not reproduced]**; the order itself is
still this decision. Before pass 2 the element's argument is the address arithmetic (`i * 4 + base`: SU 9, count 10)
and outweighs the handle's call whatever the keys; it becomes a single loop pointer (same SU and count) only after
GlobalOptimize's strength reduction, which is why only pass 2 is key-driven here.

**Seeing it on your own source.**

```sh
# the IL after each sub-pass of dop2_PreLowerGlobalOpts (probes plgo.GlobalOptimize, plgo.SymGarbageCollection,
# plgo.OptFuncExpr2, plgo.Half (only when Half runs), plgo.GrgIdentifyCandidates, plgo.2812f, plgo.SizeOpt,
# plgo.28d53, plgo.28eed; table key hooks.il_plgo)
C2_BUILD=13.00.9044 C2TRACE_REMOTE=0 uv run tools/c2trace.py il SRC --func F \
    --at @dop2_PreLowerGlobalOpts@4,plgo.GlobalOptimize,plgo.SymGarbageCollection,plgo.OptFuncExpr2,@dop2_Codegen@4 \
    --grep 'OPCALL|OPARG|OPCMP' -- /O2 /GR
# the decision with both weights and the argument keys that set them (role `order` of hooks.symkeys, 9044 only)
C2_BUILD=13.00.9044 C2TRACE_REMOTE=0 uv run tools/symkeys.py sites SRC --func F --pass 2 -- /O2 /GR
C2_BUILD=13.00.9044 C2TRACE_REMOTE=0 uv run tools/symkeys.py events SRC --func F -- /O2 /GR | rg '^order'
```

`sites` prints, after the commutative sorts, every binary node of the pass whose two operands are both subtrees
(count >= 2), the one evaluated first on top:

```text
pass 2 line   37 OPCMP      first  t218 = OPCALL(&?g_ents..(key 33) h0021, _hp(key 165) h14a0) SU8 n7 h5489   (2nd operand, heavier)
                                then   t205 = OPCALL(&?g_ents..(key 33) h0021, $o240(key 284) h4700) SU8 n7 h1e09
```

The argument column is the leaf behind each source of the call (an OPARG's operand) and its hash. To move the order,
move the key of one argument across the line above (`keys` / `whatif` for frame and tmp keys: a dead local before
the variable's first reference moves it by 1, an optimiser temporary moves by 32 per tmp block taken before it).
