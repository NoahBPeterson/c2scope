# Symbol keys: which number orders x87 operands, how it is assigned, and how to move it

Back end `c2.dll` of cl 14.00.50727.762 (VS2005 SP1); section 7 covers 13.10.3077 and 15.00.30729.1. Tool:
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
probe functions, on all three builds. **[C]**

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

Predictive mode is a replay of the traced stream with edits (add/sub n allocations of a class at a point, delete a
symbol, swap two first references); the sort model recomputes every two-operand weight whose hash depends on a key it
can follow (named leaves, field views, multi-def temps, member operands, the first two sources of a subtree). Pure
source analysis is not attempted: the temp counts come from the reader and the optimiser. The cheapest input is one
traced compile (3-5 s for 0x00806ab0). Measured accuracy, prediction vs a traced compile of the edited source:

| edit (function) | keys predicted exactly | pass-2 operand orders predicted |
|---|---|---|
| 3 dead locals before `vC01` (0x00806ab0) | 468/468 (411 moved) | 788/788 (12 changed) |
| 15 dead locals before `vC01` | 468/468 (411 moved) | 794/794 (20 changed) |
| the decompiler's padded helper (16 locals in `EC`), as `add:frame@_r@EdgeCross#2:16` | 460/460 (191 moved) | 792/792 (4 changed) |
| swap first references of `left`/`right` (0x005c6320) | 239/239 | 131/131 (1 changed) |
| tests/data/symkeys.cpp (dead local in a function and in an inline helper) | all | all, on all three builds |

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

| | 13.10.3077 | 14.00 | 15.00.30729.1 |
|---|---|---|---|
| blocks of 32, four initial blocks, same order | yes | yes | yes |
| free lists | temp and frame only; SymFree puts every non-temp chain on the frame list | per class | per class |
| key order of the probes in tests/data/symkeys.cpp, c3.cpp | same (fewer field views: 13.10 makes one view per field read, no `b0` rest views; c2-made globals have no name) | | identical to 14.00 |
| replay exact | yes | yes | yes |
| operand-sort hash (OptHashNode) | same code shape | | same |

Hooks are in the build tables (`"hooks": {"symkeys": {...}}`, `symkeys_*` options). 13.10's `SymNewEquiv` is inlined;
a probe at 0x10769fe9 names the views. 13.10's sort operands are memory leaves (no float to double conversion), shown
as `mem` without the base key, so the flip model treats them as fixed there. **[C: runs; I: that no other 13.10 site
creates views]**

## 8. Open

- Natural constructs that give exactly +16 frame keys in 0x00806ab0's switch were not searched exhaustively.
- Whether frame-slot seeds and Color ties follow keys anywhere else than through GLOBREG numbering: not checked.
- 13.10: member-operand bases in `sites`; the `free2` probe is the only path for non-temp frees seen.
