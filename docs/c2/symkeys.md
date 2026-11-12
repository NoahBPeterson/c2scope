# Symbol keys: which number orders x87 operands, how it is assigned, and how to move it

Back end `c2.dll` of cl 14.00.50727.762 (VS2005 SP1); section 7 covers 13.10.3077, 13.10.3052 and 15.00.30729.1. Tool:
`tools/symkeys.py` (tracer.md, `symkeys`). Probes: `work/scratch/symkeys/` (`p1.cpp`, `c1.cpp`..`c4.cpp`,
`tu_a.cpp`/`tu_b.cpp`), regression file `tests/data/symkeys.cpp`. **[C]** = read in the code at the address given or
seen in a traced compile of the named probe; **[I]** = inferred. il_sym.md describes the symbol records themselves;
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
| frame slot order | slots are sorted by GS level and `1000*uses/size`; the seed is the colour creation order, which may follow key order | frame.md [I] |

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
| `[base + d]` | `d + 0x31 + (hash(base) << 8)` (memory ops in a float conversion add the conversion's opcode) |
| conversion / negation of x | `hash(x) + opcode - 0x274` |

`symkeys.py sites --line L --pass 2` prints each sort with both operands, their keys and weights.

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
