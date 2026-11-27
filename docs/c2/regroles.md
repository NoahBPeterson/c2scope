# Register roles: why the loop pointer and the parameter swap places, and the levers

`tools/regroles.py` (this page), on top of `regs`, `regmap`, `rasim` and `realize` (regalloc.md, realize.md). Request
from spore-decomp: "large regalloc/schedule rows are where most bytes are; any lever that moves the esi/edi/ebx role
choice (like 009e0b90's) generalises well". Measured on cl 15.00 (/O2 /MD /Gy /TP /arch:SSE /fp:fast) against Spore's
analysis image; every claim below is a compile.

## 1. The mechanism (Spore 0x009e0b90, `DestroyRangeA4n`, 106 bytes)

Spore's hand source (`first` stepped by `++first`, the trip count `((last - first) - 1) / 0xa4 + 1` before the loop)
and the lifted source that is byte-exact (`work/lbench/spore/l2c-nmall2/src/009e0b90.cpp`) give the same loop, the
same `count` in edi and the same loop pointer in esi. What differs is the **parameter `first`**:

| | Spore's source | the original / the lifted source |
|---|---|---|
| `first` | `esi` (`push esi; mov esi,[esp+8]` at entry) | `eax` (`mov eax,[esp+4]`; `lea esi,[eax+36]` after the guard) |
| `last` | ecx | ecx |
| code | 106 bytes, 31 differ | the original |

`regs` for both (15.00): the pointer temporary (`t200` / `t1004`, prio 72, the +36 induction value cl creates from
`first`) is popped first and takes esi in both. `last` (prio 44) takes ecx. `count` (30) takes edi. `first`
(a parameter, prio 24) is last in the order, and its candidate list is what differs:

```
Spore:     first  cands  esi ebx ebp          (eax, edx gone)     -> esi, shared with the pointer (it dies where
                                                                      the pointer is defined: no interference)
original:  first  cands  eax edx edi ebx       (no esi, no ecx)    -> eax
```

Two rules make that:

1. **A value live across a `mul` / `div` cannot be in eax or edx** (`noreg:V:eax`; regalloc.md "registers clobbered
   by"). In Spore's source `first` is read after the trip count (`first + 36` is made after it, by cl's induction
   pass), so it lives across the `mul` and is left with the callee-saved registers. With only esi/ebx/ebp it takes
   esi, because the pointer, which starts where `first` dies, can share it.
2. **A value live across another value's definition interferes with it** (`interf:first:ptr`). In the original `first`
   is still needed when the pointer is defined, so it cannot have esi; and since the pointer is defined *before* the
   `mul`, `first` is dead before it and eax is allowed.

So "the esi/edi/ebx role swap" is not a priority fight among the loop variables: the loop pointer and the count have
the same registers in both. It is the **parameter** going to a callee-saved register (and the prologue changing with
it: `push esi` first, then `mov esi,[esp+8]`) instead of staying in eax. The deciding fact is the **position of the
definition of the loop pointer relative to the clobber**, a source-order fact.

The lever: give the pointer the loop walks an explicit definition before the trip count (the original's `lea esi,[eax+36]`
is the definition of a variable that exists in the original's source). cl invents its own induction temporary only
where the source has none; where the source defines it, it is defined where the source says (the symbol keys and
life ranges follow the source's order, symkeys.md):

```cpp
Vec40* p = &first->v24;                                   // before the count
uint32 count = ((uint32)((char*)last - (char*)first) - 1) / 0xa4 + 1;
do { p->DestroyRange(p->mpBegin, p->mpEnd); ... p = (Vec40*)((char*)p + 0xa4); --count; } while (count != 0);
```

Order matters and nothing else does: the same two statements the other way round (`count` first) give Spore's 31
differing bytes again; `ElemA4* p = first;` (a copy, which copy propagation folds), and `p` computed as `&first->v24` but
unused in the loop (it is dead code) give the 31 as well. The pointer has to be a different value than
`first` (an add), be used by the loop, and be defined before the clobber.

## 2. The tool

```
uv run tools/regroles.py SRC --func F --asm-vs ORIG.s [--asm-func DECORATED] [--max-compiles N] [--jobs N]
                         [--rounds N] [--span K] [--no-ptrfield] [--no-diag] [--keep DIR] -- <cl args>
```

`--asm-func` is OUR function's decorated name when `--func` does not give it (as in tsweep and realize: the function of
our listing that ORIG.s is scored against).

1. **Diagnosis.** `regmap --all` against ORIG.s; for each variable whose register differs from the *plurality* of the
   original's votes it asks `rasim need reg:V=R` and prints the single edits that close it. regmap says `mixed`
   (not `WANTS`) for a parameter whose first instructions align badly (`mov esi,[esp+8]` against the original's
   `mov ecx,[esp+8]`), which is why `realize --from-rasim auto` stopped with "0 answers" on this row: it only follows
   `WANTS`. The only rasim answer is a life-range edit, `canreg:first:eax` (or `interf:first:p`), and realize has no
   construct for `canreg` / `noreg` / `interf` (it prints "no catalogued construct applies here").
2. **Candidates** (the constructs for those answers):
   - `move`: one simple statement moved up or down past up to `--span` independent neighbours of its block (rwsweep's
     `swap` is the distance-1 case; a declaration with an initialiser moves too). Ends a value's life before a clobber
     or lets it span another's definition.
   - `ptrfield`: for a pointer V stepped in a loop body (`++V`, `V += N`, `V = V + N`, `V = (T*)((char*)V + N)`): a
     definition `char* rg_p = (char*)V + OFF;` before one of the last four statements ahead of the loop, the loop's
     uses of V read `((T*)(rg_p - OFF))`, the step advances `rg_p`. OFF is one of the displacements the original
     materialises in a register (`lea r,[r2+N]`, `add r,N` of ORIG.s, 4..0x3ff), the +36 here.
3. **Measure.** Every candidate is compiled with the traced c2 and scored against ORIG.s (tsweep's byte diff, then the
   register diff); the best becomes the next round's base (`--rounds`, default 2). A byte diff of 0 stops the search
   and `DIR/best.cpp` has the source. The rewrite is mechanical and ugly (`((T*)(rg_p - 36))->x`); once it matches,
   fold it back by hand to the form of section 1 (it matched Spore's `Vec40* p = &first->v24;` form too).

## 3. Results (cl 15.00, `uv run tools/regroles.py`, checked with spore-decomp's `tools/matching/chk.py`)

| row | flags | before | after | lever |
|---|---|---|---|---|
| 0x009e0b90 `DestroyRangeA4n` (106 B) | /O2 /MD /Gy /TP /arch:SSE /fp:fast | 31 bytes differ | **MATCH** | `ptrfield` at +36, before the count |
| 0x00c781e0 `DestroyPlayerPlanetDataRange` | /O2 /Ob2 /MD /Gy /TP /arch:SSE /GS- | 44 | 12 (esi/edi/eax roles, prologue right), 2 with the loop written as `do { } while` | `ptrfield` at +112 |
| 0x00aeae10 `Vec12::erase` (116 B), 0x00e1f700 (116 B) | /O2 /MD /Gy /EHsc /TP, resp. /O2 /MD /Gy /TP /arch:SSE /fp:fast | 70, 69 | 70 (no candidate better), 63 | different shape (erase / insert: the stepped pointer is not a loop walked from a count the way the lever needs) |
| 0x00826290 (596 B), 0x00902950 (508 B) | | | no candidate improves the base | not reachable with these levers (below) |

The large rows spore's notes blame on esi/edi/ebx roles (`this` in ebx vs edi, `p`/`t` swapped) were diagnosed but not
closed. Their rasim answers are order edits between `this`, the loop values and a few temporaries (`before:`, `interf:`,
`pref:`, `canreg:` on several values at once, each "also moves" other registers), and no statement move changes them;
a ptrfield needs a stepped pointer. The tool reports the diagnosis for those rows; the realisations are the open
part (no construct yet for `pref:` / `copy:` between values that are not copies in the source).

## 4. When to reach for it

The symptom: `regmap` says a **parameter** (or `this`) is `mixed` / `WANTS eax` while we give it a callee-saved
register, the prologue has an early `push esi; mov esi,[esp+..]`, and the loop pointer, the counter and the other
values agree. Then ask whether the original's pointer is a value cl would have made after a `mul` / `div` / call
(`ptrfield`), or a plain statement order (`move`). The converse (the original has the parameter in esi and we do
not) is the same two levers the other way: the explicit pointer after the clobber, i.e. delete it.
