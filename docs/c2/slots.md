# Stack-slot packing: the rule per build, the `slots` report, the model and what-if

Scope: which frame slot each memory-resident local, temporary and parameter gets, on all four c2 builds the tools
support (14.00.50727.762 default, 13.10.3077, 13.10.3052, 15.00.30729.1). `docs/c2/frame.md` section 4 is the
14.00 reading of the same code in detail (data layout, liveness, conflicts, param homes, /GS levels); this file
adds the other three builds, the tool (`c2trace.py slots`, code `tools/c2slots.py`), the replay model and how it was
validated. Rules carry **[C]** (read in the code at the cited address, or seen in a traced or probed compile) or
**[I]** (inferred).

## 1. The rule (all four builds unless a build is named)

1. **Elements.** Every frame SYM (temp, auto, param) left in the final code after register allocation; its use count
   is SYM+0x38, counted by `CollectStackElements`/`AddStackElement` over the instruction stream (sources, then
   destinations; +1 per operand). [C] 14.00 `@AddStackElement@8` 0x10be9190; 13.10.3077 0x10788642 (the increment
   is passed in ebx, always 1 from CollectStackElements 0x10785d18); 13.10.3052 0x10799e09; 15.00 0x51137c20.
2. **Visiting order** = the element list: size ascending; equal size: uses descending; equal uses: whichever reached
   that count first (a new element goes to the end of its size run; an element whose count grows moves back past
   same-size predecessors with *strictly* fewer uses). The element number SYM+0x3c is the list position
   (`EnumerateStackElements`). Identical code on all four builds. [C]
3. **Uses are counted before the late flow passes.** Stack packing runs in `dop2_CodegenUpdate` (`StkStackAlloc`),
   before `dop2_FinalFlowOpts` merges identical arms (`FgTryHeadMerge`), so an arm written twice counts its slot
   references twice although the final code has one copy. The report prints the packer's count next to the
   listing's; where they differ, that is why. [C: phase order frame.md section 1, regs/tracer.md for the merge; M&B
   PartyScreen::setup_party for the consequence]
4. **Parameter colours first** (one per parameter, in list order, size = align4 of the SSR size, at the param's
   home), then each non-param element tries the param colours in order (`size <= colour size`, no conflict). [C]
   - 14.00 and 15.00: never in a function with a /GS cookie or GS parameter copies (`FUNC+0x5c`, `FUNC.flags & 1`).
     [C 14.00 0x10beb14b..0x10beb16a, 15.00 0x51139b52..0x51139b71]
   - **13.10 (both builds) differs:** in a /GS function an element may still take a param home unless SYM.fl7 has
     0x20 or it is larger than 4 bytes and its type is not 0x4008. [C 3077 0x10790d16 -> 0x107af76c; probe
     `work/scratch/slots/gshome.c`: `int x` and `int *q` go into `p`'s home on 13.10.3077, a new slot on 14.00 and
     15.00]
   - No param homes at all when FUNC flags forbid them (14.00: `+0xac & 0x100` or `+0x52 & 1`; 13.10: `+0x8c` bit 7
     or `+0x46 & 1`). [C]
5. **Locals: `colorElemental`.** Each remaining element tries the existing local colours **newest first** and joins
   the first one where `size <= 2 * colour size` (the colour's current size: a char colour grown to a short by a
   short member then admits an int) and there is no conflict; otherwise it opens a new colour. Conflict = the
   element is in the colour's interference set (the union of its members' LiveOnDef) or the element's LiveOnDef
   meets the colour's members. T (the frame total) grows by `align4(new) - old size` on growth, `align4(size)` per
   new colour. [C 14.00 0x10beb760, 13.10.3077 0x10791426 (loop 0x10791a6a), 3052 0x1079dac6, 15.00 0x5113a140]
6. **Undo.** In an ESP frame, if any local took a param home and T >= 112, every colour is dropped and step 5 is
   rerun for all locals with no param colours. [C 14.00 0x10beb25e, 13.10.3077 0x10790d90 -> 0x10734a51, 15.00
   0x51139c64]
7. **Order.** Colours keep creation order unless T > 128 or the function has a /GS cookie (14.00/15.00 also: GS
   parameter copies); then a K&R quicksort (middle pivot, strict) orders them by GS level, then key
   `1000 * uses / size` descending (integer division; uses summed over the members, size the largest member).
   Equal keys keep whatever the partitioning leaves (unstable). [C 14.00 `@QSortColors@12` 0x10bebbb0 /
   `@StkColorCmp@12` 0x10bebb30; 13.10.3077 0x107ae0c4 / 0x107ae9c2; 3052 0x107b8a06 / 0x107b979e; 15.00
   0x5113a590 / 0x5113a510]
   - **GS level:** 14.00/15.00 three levels (`StkGSBufferLevel`, frame.md 4.7); 13.10 a single bit in the colour's
     byte +0x18 (`or byte [colour+0x18], 1` at 3077 0x10722f9b on a join, 0x107ae176 for a new colour), compared as
     a sign-extended 1-bit field. Same direction rule. [C]
8. **Offsets.** ESP frame: the last colour is allocated first (just below the cookie / EH node), colour 0 last,
   nearest esp; EBP frame the reverse. Every member of a colour sits at its base (ESP); in an EBP frame a smaller
   member sits at the top end. `allotemp` differs: 14.00/15.00 round the size to the alignment and the offset to
   it; **13.10 does not align a 1-byte slot and aligns a 2-byte one to 2** (3077 `@allotemp@8` 0x1077479d, default
   alignment 0x108a91d0). 13.10 adds the EBP adjustment inside the loop (0x10797b94); 14.00/15.00 apply it
   afterwards (`FixOffsetsForEbpAdj`). [C]

Consequences seen in real code (M&B, 13.10.3077, from their probes; each is a rule above):
- One function-wide loop counter with many uses is visited first in its size run and becomes the oldest colour of
  that size; every later local that tries newest-first lands somewhere else. Separate counters per loop (block
  scope) fixed five interpreter cases. (Rules 2, 5.)
- `else if (a || b) { X }` written as two identical arms: the duplicate arm is merged after packing but counted
  before, so the slot's key and the sort order change (PartyScreen::setup_party). (Rules 3, 7.)
- A temporary bound to `const float&` packs differently from a named local whose address escapes (inferred: the
  temporary's conflict set differs). [I]
- Frames with T <= 128 and no GS keep creation order (frame.md 4.7). (Rule 7.)

## 2. Probe addresses per build (hooks key `slots`)

`c2trace.py slots` uses 18 hooks, in the order of `c2slots.ROLES`; 14.00's are `c2slots.HOOKS`, the others are
`hooks.slots` in `tools/c2builds/<build>.json` plus `slots: {sbv, v13}` (the sparse bit-vector node layout: 14.00
and 15.00 nodes are `base, bits lo, bits hi, next` with 64-bit chunks; 13.10 nodes are `base, next, bits` with
32-bit chunks). Every address was read in the disassembly of that build; 3052's were found from 3077's by masked byte
context (unique match) and its globals sit 4 bytes below 3077's. [C]

| role | 14.00 | 13.10.3077 | 13.10.3052 | 15.00 |
|---|---|---|---|---|
| ColorStackElements entry (function name) | 10beaef0 | 10790ad6 | 1079d16a | 51139920 |
| param colour made (SYM) | 10beaf5a | 10790c45 | 1079d2d9 | 5113998a |
| its LiveOnDef | 10beb009 | 10790ba1 | 1079d235 | 51139a3e |
| param colour tried | 10beb143 | 10790d11 | 1079d3a5 | 51139b4a |
| local joins a param home | 10beb1d8 | 107349c0 | 10734868 | 51139bd5 |
| its LiveOnDef | 10beb222 | 10734a17 | 107348bf | 51139c1d |
| colorElemental entry | 10beb760 | 10791426 | 1079dac6 | 5113a140 |
| colour tried (loop head) | 10beb791 | 10791a6a | 107ad452 | 5113a179 |
| joined | 10beb81b | 10791ab7 | 107ad49f | 5113a202 |
| join: LiveOnDef | 10beb865 | 10791aef | 107ad4d7 | 5113a24b |
| new colour: LiveOnDef | 10beb992 | 107914c4 | 1079db64 | 5113a37a |
| GS level, join | StkGSBufferLevel exit | 10722f9b | 10722fa3 | 5113a110 exit |
| GS level, new | 10bebb02 | 107ae176 | 107b8ab8 | 5113a4d9 |
| colouring done (T, param colours, bEbp, joins) | 10beb25e | 10790d90 | 1079d424 | 51139c64 |
| undo | 10beb2d7 | 10734b33 | 107349db | 51139cd4 |
| after the sort (T, frame, alloc) | 10beb36e | 10790df3 | 1079d487 | 51139d65 |
| offset loop: colour, base, members | 10beb584 | 10790e88 | 1079d51c | 51139f6e |
| member offset | 10beb618 | 10790ea9 | 1079d53d | 51139ff5 |

Globals (colour table, count, params, LiveOnDef array): 14.00 0x10d0fa9c / 0x10d0fa98 / 0x10d0faa8 / 0x10d0faa0;
3077 0x108a7890 / 0x108a788c / 0x108a789c / 0x108a7894; 3052 four lower; 15.00 0x51255ae8 / 0x51255ae4 / 0x51255af4 /
0x51255aec. Colour record 0x1c bytes on every build: +0 members, +4 interference, +8 size, +0xc uses, +0x10 param
offset, +0x14 alignment, +0x18 GS level/bit. SYM: +0x20 size, +0x38 uses, +0x3c index, +0x2c alignment code. [C]

The M&B project's hand probes (13.10.3077) are the same points: 0x10790ded is just after the sort, 0x10790e38 the
offset loop, 0x107914c1 the new-colour path (`eax` = the members vector there; LiveOnDef is in eax one instruction
later, 0x107914c4), and `@StkColorCmp@12+0x48` the compare after both divisions.

## 3. The tool

```sh
uv run tools/c2trace.py slots SRC --func F [--export SNAP.json] [--what EDIT ...] [--orig ORIG.s [--search]] -- FLAGS
uv run tools/c2slots.py what SNAP.json EDIT ...             # what-if without a compile
uv run tools/c2slots.py search SNAP.json --asm OUR.asm --orig ORIG.s
uv run tools/c2slots.py orig --orig ORIG.s                  # (or --image EXE --va VA) the original's slots alone
```

The report (`tests/data/slots.c`, `--func big`, 14.00):

```
# _big  (c2 14.00)
# 5 elements (1 params), 4 colours, T=176 sorted (T > 128), ESP frame, undo: param homes dropped (T >= 112)
model: reproduces the trace exactly (decisions, colour order, offsets)

## colours, in final (sorted) order; offsets as the packer gave them
 pos made  offset  size   uses      key  members (name:packer uses/listing refs line)
   0    0    -173     1      2     2000  _ch$716:2/2 L23
   1    1    -172     4      6     1500  _i$:6/6 L20
   2    2    -168     8      2      250  _d$714:2/2 L22
   3    3    -160   160      2       12  _s$:2/2 L19

## elements in visiting order (size asc, uses desc, first to reach the count)
 vis   idx size  uses   lst  name                       -> colour/off  tried (newest first): reason
   0     0    1     2     2  _ch$716 L23                new 0@-173
   1     1    4     6     6  _i$ L20                    new 1@-172     0:size
   2     2    4     1     1  _n$ L21                    param home
   3     3    8     2     2  _d$714 L22                 new 2@-168     1:conf _i$ 0:size
   4     4  160     2     2  _s$ L19                    new 3@-160     2:size 1:size 0:size
```

`made` is the creation number (`p` = a parameter colour), `lst` the listing's reference count, `L` the first source
line in the listing; `$T` temporaries are named from SSR+0x28 (their number), `tv` temporaries from SYM.key. The
element's LiveOnDef is read as up to 12 bit-vector nodes (sparse chunks of 64 elements on 14.00/15.00, 32 on 13.10);
a longer set is marked `[LOD cut at #N]`. Cutting only loses conflicts, so where the trace refused a colour for a
conflict the recorded sets do not show, the pair (element, member whose set or the element's own was cut beyond the
other's number) is added and listed as inferred; members that end in the same colour are known compatible and never
added. On the M&B function this makes the replay exact.

**Model** (`Model` in c2slots.py): the parameter colours, the param-home pass, colorElemental, the undo, the sort and
the offsets of section 1, fed only with the recorded elements (size, uses, alignment, GS level, flags) and their
LiveOnDef sets. `check()` compares every decision (colours tried, the one joined), the sorted colour list, every base
and every member offset with the trace; `model:` prints the differences (none on every function so far, section 5).

**What-if** (`--what` or `c2slots.py what`): `uses:NAME=N[^]`, `size:NAME=N`, `pos:NAME=P`, `conf+:A,B`, `conf-:A,B`,
`add:NAME,SIZE,USES[,POS][,conf=A;B]`. A changed use count or size re-sorts the element to the end (`^`: the front)
of its new run, which is where AddStackElement puts an element that reaches the count last (first). Output: the new T,
colour count and every element whose offset moves.

**The original's slot map** (`--orig ORIG.s` or `--image EXE --va VA`): the original's frame operands, keyed by
their address relative to the return-address slot (esp: displacement minus the push depth tracked through push/pop,
`sub/add esp`, `__chkstk`, and callee pops: `ret N` of the callee with `--image`, otherwise the pushes since the last
call when no `add esp` follows; ebp: from where `mov ebp, esp` / `lea ebp, [esp-N]` put it; code only reachable
through a jump table is walked at the body's depth). Our listing is aligned with it (regmap's mnemonic + operand-shape
alignment); each aligned pair with the same number of frame operands is a vote for the element in our operand, and
the most common difference between the original's key and our listing offset is the frame shift. Side by side: our
offset, the original's (in our coordinates), votes. `--search` replays single edits (uses -3..+3 at either end of the
run, visiting positions within the size run, removing a conflict that refused a join, adding one against a colour
mate) for the elements not at the original's offset and their colour mates, and ranks them by how many elements end
at the original's offsets.

## 4. Limits

- LiveOnDef is read as at most 12 nodes (section 3). The inferred pairs make the replay exact but may over-state a
  conflict (all candidate members of a refused colour get one), so a what-if involving an element marked `[LOD cut]`
  is weaker evidence.
- The what-if keeps every other element's uses, sizes and conflicts as they were: an edit that would change the code
  (another temporary, a different register allocation) is outside the model. `uses` re-sorts by rule 2 but cannot know
  when the element reaches its new count relative to others with the same count (`^` picks the other end).
- The EBP adjustment (`TryEbpAdjust`) is read from the trace, not modelled: an edit that changes T across 128 in an EBP
  frame may move every offset by the adjustment.
- The original's map is only as good as the alignment and the push-depth tracking; `depth conflicts` counts the
  blocks reached with two depths (a sign the callee-pop guess was wrong somewhere; pass `--image` for `ret N`).

## 5. Validation

"Exact" = the model reproduced every decision (colours tried, the one joined), the sorted colour list, every base
and every member offset of the trace. Sources under `work/scratch/slots/` (probes) and the real units named.

| what | build(s) | functions | model exact |
|---|---|---|---|
| `p1.c` / `tests/data/slots.c` (creation order, sort, param homes, undo, GS, sharing by growth) | all four | 6 | 6 / 6 on each |
| `gen.py` random functions (mixed char/short/int/double/structs in nested scopes and loops, 0-3 params, cdecl / stdcall / fastcall), seeds 1-3, default / `/Oy-` (EBP frames) / `/GS-` | all four | 960 + 640 | all |
| `gshome.c` (13.10's /GS param-home rule) | 14.00, 3077, 15.00 | 3 each | all |
| M&B `Operation::execute` (0x49fa10; `work/from-mb/lane_sif/sif_base.cpp`), 1144 elements, 167 colours, T 0x1ec0 (= the original's frame) | 13.10.3077 | 1 | yes (157 LiveOnDef sets cut at 12 nodes, 8019 pairs inferred from the trace's refusals) |
| Fallout 3 0x00806ab0 (`FUN_00ea3d60`, `work/scratch/symkeys/f3/806ab0/best1.cpp`), 191 elements, 43 colours, T 460, undo | 14.00 | 1 | yes |
| Spore 0x00f7d9d0 (`UpdateNormalMapRegion`, `work/scratch/regmap_xmm/terrain.cpp`), 688 elements, 53 colours | 15.00 | 1 | yes |

Against the originals (`--orig`; offsets of elements seen in aligned instructions on both sides):
- M&B execute: 167 of 215 at the original's offset (15,129 instructions, 13,801 aligned, 2 depth conflicts). The
  best single edits `--search` finds (+6: `_mp$116431` 5-6 uses; +4: a conflict between `_n$116884` and the
  function-wide `_i$`, i.e. its own counter) are of the kind the M&B project found by hand. 800 edits in 6 minutes.
- Fallout 3 0x806ab0: 91 of 92 (the one left is a parameter with split votes).
- Spore 0x00f7d9d0: 13 of 290. Only 2,040 of 8,430 instructions align (nine similar inlined bodies, as with regmap);
  treat it as no evidence either way.

Without the inference of section 3 the M&B function differed in 2,093 places: on 13.10 a node covers 32 elements,
so 12 nodes are 384, and long-lived elements of a 1,144-element frame have more. [C]
