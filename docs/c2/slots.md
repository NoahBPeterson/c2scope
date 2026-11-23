# Stack-slot packing: the rule per build, the `slots` report, the model and what-if

Scope: which frame slot each memory-resident local, temporary and parameter gets, on all four c2 builds the tools
support (14.00.50727.762 default, 13.10.3077, 13.10.3052, 15.00.30729.1; the x64 cl 17.00.61030 is section 6). `docs/c2/frame.md` section 4 is the
14.00 reading of the same code in detail (data layout, liveness, conflicts, param homes, /GS levels); this file
adds the other three builds, the tool (`c2trace.py slots`, code `tools/c2slots.py`), the replay model and how it was
validated. Rules carry **[C]** (read in the code at the cited address, or seen in a traced or probed compile) or
**[I]** (inferred) or **[M]** (measured: a probe in `tests/data/rules/RX_*.cpp`, checked on all four builds by `tests/probes/misc.py`).

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
- A temporary bound to `const float&` packs differently from a named local whose address escapes: the named local
  is live over its whole scope (a function-scope one conflicts with the earlier parameters and with every other
  escaping local of that scope), the temporary only over its full-expression, so `use(a * b); use(a + b);` gives the
  second temporary `a`'s parameter home and the two named `float t, u` get two new slots. **Correction**: it is the
  scope, not the temporary: the same named locals each in its own `{ }` block pack exactly like the temporaries.
  Passing the named local to a `const float&` parameter instead of a pointer changes nothing. All four builds alike
  [M: `tests/data/rules/RX_cfref.cpp` s_temp, s_named, s_named_ref, s_block; `slots` trace on 14.00 and 13.10.3077
  shows the conflicts]
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

## 6. cl 17.00 x64

The x64 cross compiler (cl 17.00.61030 x86_amd64, `--c2 17.00` / `C2_BUILD=17.00`, table
`tools/c2builds/17.00.61030-x64.json`, generated by `work/port1700/mktable.py`) keeps the x86 packer as members of
`class StackPack` (stackpack.obj). The static map is `work/port1700/slots_map.md`; this section is what the tool
needed and what the traces showed. [C] unless marked.

**Probe addresses** (`hooks.slots`, in `c2slots.ROLES` order, then the extra role `frame`; a `null` entry is a role
the build does not have). StackPack fields replace the x86 globals: `this+4` colour table, `+0x28` colour count,
`+0x2c` parameter count, `+0xc` LiveOnDef array, `+8` element count (ecx at both entries).

| role | 17.00 | what is logged |
|---|---|---|
| start | 100e1b40 | `ColorStackElements` entry: `tdccc>0>0>4$`, ecx+8 elements, ecx+0x2c params |
| pnew | 100e1bcf | a parameter element (ebx), its SSR size and home (`[ebx+4]+0x1c` / `+0x18`) |
| pnew_lod | 100e1c2e | `call SbvCopy`: edx = its LiveOnDef |
| ptry | 100e1e89 | param colour tried: esi colour, `[ebp-0x18]` element, ecx+8 colour size |
| pjoin | 100e1cf8 | local joins a param home: `[ebp-8]` colour, `[ebp-0xc]` SYM |
| pjoin_lod | 100e1d1c | `call SbvOr`: edx = its LiveOnDef |
| elem | 10052ec0 | `colorElemental` entry: SYM `[esp+4]`, ecx+0x28 colours, ecx+0x2c params |
| ctry | 10052eea | colour tried (newest first): edi colour, ebx+8 its size |
| cjoin | 10053066 | joined colour edi |
| cjoin_lod | 10053086 | `call SbvOr`: edx = LiveOnDef |
| cnew_lod | 10052f50 | new colour, `call SbvCopy`: edx = LiveOnDef |
| gs_join | 1005319a | al = `StkGSBufferLevel` of a joining buffer (logged on entry, not at an exit) |
| gs_new | 10053159 | al = GS level of a new colour |
| done | 100e1d49 | esi = T, `[ebp-0x10]` = param colours offered (nP) |
| redo | - (null) | there is no undo |
| sorted | 100e1d93 | after the sort: colours, T, params, cookie SSR (FUNC+0x68), FUNC+0xe4, MOD+0x396, Alloc_local (TLS+0xdd14), cap FUNC+0x4c; named by `slots.sorted` |
| col | 100e1dc2 | offset loop: esi colour, eax base |
| mem | 100e1e1d | member: edx SYM, its index +0x40, eax packed offset |
| frame | 1006c6a1 | `MdmCalculateFrameInfo`: name, A = FUNC+0xa4, F (edi), pushed bytes (eax), B = FUNC+0xb8, the frame registers of locals / params FUNC+0x74 / +0x78 with RegSym[5] (rsp; `[TLS+0x113f4]` + 5 * 0x64), FUNC+0x5c (bit 0 dynamic alignment), FUNC+0x4c, FUNC+0xe4 / +0xe8 (alloca: 0x1000 / 0x100) |
| fend | 1011b9b0 | `EncEmitFunc` entry: name, F = FUNC+0x70 (0 when `TryRemoveArgumentAreaFromStackFrame` removed the argument area) |

`slots` keys added for this build (all read by c2slots, none by build name): `sym` (the SYM field template: index
+0x40, size +0x24, uses +0x3c, flags dword +8, number +0x20, name `[[+4]+4]`, alignment code +0x30, type +0,
SSR+0x28), `sorted` / `gsbit` (the `sorted` record's fields; /GS is bit 0 of MOD+0x396), `gs_join_probe` (gs_join is a probe
whose first value is the level, not a function exit), and the rule switches
`tround: 16`, `undo: false`, `up: true`, `palign: true`, `capdef: 16`, `argarea: true` (defaults in `c2slots.RULES`
are the x86 rules, so the four x86 tables need no change).

**The rule on x64**, differences from section 1 only:
- Rule 4: param colours are offered only when FUNC+0x9c is set (`MdmInitParamShadowUsage`; `done` logs nP), and a
  local also needs `(home - 8) % alignment == 0`; dummy parameters (`tvN`, 8 bytes) fill the unused home slots of
  the four, so a function with fewer than four parameters still has four param colours. nP is read from the trace,
  not modelled; it was 0 in 10 of the 29 zlib functions (all of them sorted ones), while `slots.c` `big` and `gsbuf`
  have a cookie, nP = 4 and every home refused by the cookie rule (or for size).
- Rule 5: T grows by align16 (x86 align4); the alignment cap is FUNC+0x4c, 16 when 0.
- Rule 6 (undo) does not exist.
- Rule 7: same trigger (T > 128, cookie, GS copies), same compare; the direction is the x86 ESP one (GS level
  ascending: buffers end up highest, next to the cookie).
- Rule 8: offsets ascend from Alloc_local (the `sorted` record's value, 0 normally) in colour order, every member
  at its colour's base; `allotemp` rounds the size to the alignment, a 1-byte slot is not aligned, a 2-byte one to 2.
- Final displacement (`MdmInsertEntryExit`; other frame kinds in 6.1): a local's `[rsp+N]` is packed + A, a parameter's and anything sharing
  its home is the home (8 + 8i) + F + pushed bytes. The report prints this `final` column and compares it with the
  listing's `name$ = N` lines (`listing: K of N`).
- Code generation runs on worker threads: `parse` keeps one context per `# thread N` of the log.

**Tool changes** (`tools/c2slots.py`): the build table's `sym` template and the role list without null entries;
the `frame` role; per-thread parsing; `RULES` gating the T rounding, the undo, the param-home alignment test, the
upward allotemp and member offsets, and the final-offset/listing check; x64 listing lines (`name$ = N`, no size,
`[rsp]` operands). `--orig` / `--search` / `orig` and framefields: section 6.2.

**Validation** (`c2trace.py slots SRC --func "" --all --quiet -- /O2`, one compile at a time, local wine).
"exact" as in section 5; "listing" = elements the listing names whose `[rsp+N]` the model gives.

| what | functions with a packed frame | model exact | listing `[rsp+N]` |
|---|---|---|---|
| `tests/data/ncol/*.c/cpp` (c1, sum: no frame elements beyond parameters; c2 `f2`, fp `spring`) | 2 | 2 / 2 | 15 / 15 |
| zlib 1.2.1, 14 files (`/I` the zlib folder); 10 sorted (6 with T > 128), 8 with a local in a param home, 2 with a local sharing a local colour | 29 | 29 / 29 | 143 / 143 |
| `tests/data/slots.c` `/GS` (creation order, sort, param homes, GS buffer, sharing) | 6 | 6 / 6 | 23 / 23 |
| `work/scratch/slots/gen.py` seeds 1-3, 100 functions each, `/TP`, default and `/GS-`; 357 sorted, 4,298 colours for 6,694 elements | 600 | 600 / 600 | 5,556 / 5,556 |

zlib in numbers: 166 elements, 152 colours. Most zlib functions never reach `ColorStackElements`: on x64 their
locals live in registers and the only frame elements are the parameters (`numElements == numParams` returns before
colouring), e.g. all 18 of deflate.c, adler32, crc32, zutil; their listing lines are parameter homes, which the
packer does not place. Not exercised by any of these: the param-home alignment refusal (a home is 8 bytes, so only an
over-aligned element of at most 8 bytes could hit it), `PartitionSortedColors` (FrameChain builds: never, 6.1),
and LiveOnDef sets longer than 12 nodes (the largest frame had 20 elements). Dynamic alignment, alloca and the
removed argument area: 6.1.

Example (`tests/data/slots.c --func big -- /O2 /GS`):

```
# big  (c2 17.00.61030-x64)
# 8 elements (4 params), 8 colours, T=208 sorted (T > 128), rsp frame, A=32 F=224 pushed=8
model: reproduces the trace exactly (decisions, colour order, offsets)
listing: 5 of 5 named elements at the model's final offset (packed + A; param homes + F + pushed)
 pos made  offset  final  size   uses      key  members (name:packer uses/listing refs line)
   0   p0       8    240     4      1      250  n$:1/0
   1   p1      16    248     8      1      125  tv10:1/-
   ...
   4    4       0     32     1      2     2000  ch$1:2/2 L23
   5    5       4     36     4      6     1500  i$:6/6 L20
   6    6       8     40     8      2      250  d$2:2/2 L22
   7    7      16     48   160      2       12  s$:2/2 L19
```

The x86 rules are untouched: `run_tests.py`'s slots tests pass under `C2_BUILD=14.00` and `15.00` (7 / 7 each).

### 6.1 Dynamic alignment, alloca, the removed argument area (cl 17.00 x64)

The packer's decisions are the same in these frames (the model was exact on every function below); what differs
is the register the listing's `name$ = N` is relative to, and so the `final` column. `MdmSetFramePointerRegs`
picks the frame registers (FUNC+0x74 locals, +0x78 parameters), `MdmInsertEntryExit` adds the deltas. Read off the
frame record and the listings **[C]**:

| frame | prologue (examples) | locals (FUNC+0x74) | parameter homes (FUNC+0x78) |
|---|---|---|---|
| plain | `push ..; sub rsp, F` | rsp: packed + A | rsp: 8 + 8i + F + pushed |
| dynamic alignment (FUNC+0x5c bit 0: a local aligned above 16; FUNC+0x4c = that alignment, 32 / 64 seen) | `push rbp; sub rsp, F; lea rbp, [rsp+A+cap]; and rbp, -cap` | the aligned rbp: packed + A - B = packed (B = A) | rsp: as plain |
| alloca (FUNC+0xe4 & 0x1000) | `push rbp; sub rsp, F; lea rbp, [rsp+B]`, B = min(A, 0xf0) | rbp: packed + A - B | rbp: 8 + 8i + F + pushed - B |
| alloca and dynamic alignment | `push r13; push rbp; ..; lea rbp, [rsp+0x40]; and rbp, -32`, epilogue `lea rsp, [r13+..]` | the aligned rbp: packed + A - B | r13 = rsp + B: 8 + 8i + F + pushed - B |
| argument area removed (`TryRemoveArgumentAreaFromStackFrame`: F == align16(A) + 8, no call, rsp a register operand only of `sub` / `add rsp, F` and tail jumps (opcodes 0x103 / 0x104); `[rsp+N]` operands do not count) | none (tail jump, `mov [rsp+8], ecx` into a home) | (none packed: F would be larger) | rsp: 8 + 8i + pushed (F subtracted) |

So locals: + A when FUNC+0x74 is rsp and B == A, else + A - B, as the map says (slots_map.md 3.2); homes: - B
when FUNC+0x78 is not rsp, - F when FUNC+0x70 is 0 at encoding. `c2slots.frame_deltas` is that rule; it reads the
new fields of the `frame` record and the `fend` role (build table: `hooks.slots`, generated by mktable.py), and
the report says which frame it is (`frame, A=272 F=304 pushed=40, B=240, alloca; locals from the frame register,
homes from the frame register`). Before, `final_offsets` used "+ A unless B != A": wrong in every dynamically
aligned function (A instead of 0) and in every alloca frame (B == A whenever A <= 0xf0, and homes without - B).

The flags as traced (frame probe, `tests/data/slots17/frames.cpp`, /GS): FUNC+0x5c = 1 and FUNC+0x4c = 0x20 / 0x40
in al32 / al64 / alp / aca (16 otherwise); FUNC+0xe4 = 0x5103 / 0x5102 / 0x5183 in the alloca functions ac / ac2 /
aca (0x1000 set), 0x41xx elsewhere; FUNC+0x74 != FUNC+0x78 in the dynamically aligned ones. `tc` (only a tail
call: F = 0x28 at the frame decision) has no `sub rsp` and lists `a$ = 8`, `b$ = 16`; it has no packed elements, so
the report does not show it, but `t2` / `t5` of `tests/data/slots17/tr.cpp` do (a struct and a volatile local in
the parameter homes).

`FrameChain` (0x103e78f8): read once (`MdmSetFramePointerRegs` 0x10092a35), written only by its static
initialiser (`mov [0x103e78f8], 0` at 0x1025690c), no other reference in the image: `PartitionSortedColors` and the
FrameChain frame variants cannot run in this build.

**Validation** (`c2trace.py slots SRC --func "" --all -- /TP /O2 /GS` and `/GS-`, one compile at a time, local wine):

| what | functions | model exact | listing `[reg+N]` | frame kinds |
|---|---|---|---|---|
| `tests/data/slots17/{frames,frames2,tr}.cpp` /GS (fixtures: `snap/*.json`, `run_tests.py` `slots17_frames_tests`) | 15 | 15 / 15 | 56 / 56 (rule before: 26 / 56) | 7 alloca (3 with B < A), 6 dynamic alignment, 2 removed argument areas |
| `tests/data/slots17/genframes.py` seeds 1-3, 100 functions each, /GS and /GS- | 600 | 600 / 600 | 3,941 / 3,941 | 274 alloca, 114 dynamic alignment (32 and 64), 64 both, 112 with B < A, 112 removed argument areas |
| `work/scratch/slots/gen.py` seed 1, `tests/data/slots.c` /GS, `tests/data/ncol` (no such frames: unchanged) | 108 | 108 / 108 | 919 + 23 + 15 | |

### 6.2 The original's slot map on x64 (`--orig`, `--search`, `c2slots.py orig` / `search`, framefields)

Same use as on x86 (section 3): ORIG.s is the original function as `VA mnemonic operands` lines (capstone text, e.g.
an image's disassembly; jump targets in the same VA space), or `--image EXE --va VA` (capstone 64-bit, x64 jump
tables `lea rB, [rip+d]` = the image base, `mov r32, [rB + rI*4 + T]` RVAs, followed; not yet run on a real image).
What differs is the walk, `c2slots.orig_slots64` (chosen by the build table's `arch: x64`):

- **Keys** are relative to the return-address slot at entry, as on x86: the four register arguments' homes are
  +8 .. +32, stack arguments from +40, locals negative. Depth = entry rsp - rsp: `push` +8, `pop` -8, `sub/add
  rsp, N`, `mov eax, N; call __chkstk; sub rsp, rax`, and the epilogue's `lea rsp, [rbp+N]` / `mov rsp, rbp` from the
  frame register. Calls change nothing (no callee pops, no pushes after the prologue).
- **Frame registers**: any register set from rsp (`mov rbp, rsp`, `lea rbp, [rsp+N]`, `mov r11, rsp` for the saves
  `mov [r11+8], rbx`, r13 next to an aligned rbp) keeps its depth until it is written; `and rbp, -cap` makes it a
  dynamically aligned one, whose operands are keyed `ALIGNED` (-2^20) + displacement (their distance to rsp is not
  known). Callee-saved registers saved into the homes before the prologue (`mov [rsp+8], rbx`) or into the frame
  later (shrink-wrapped, `mov [rsp+0x78], rbx` after an early exit test) are just unnamed keys.
- **Body depth** = the deepest point of the walk (shrink-wrapped paths are shallower). Code only reachable through a
  jump table (`jmp rcx`) is walked at the most common state.
- **Our coordinates, absolute** (no fitted shift, unlike x86): a home's key is its packer offset (8 + 8i) whatever
  the original's frame size; a local is at body rsp + A + packed in every frame kind of 6.1 (MdmInsertEntryExit), so
  packed = key + the original's body depth - A, with A (the outgoing-argument area) taken as ours; an aligned frame
  register's operand: packed = displacement - the locals' delta (frame_deltas). The report prints the original's
  frame (`frame: 24 pushed, sub rsp 128, body depth 152, frame registers rbp`) and says when the x86 best-fitting
  shift would differ (a frame of another size, an element added in front: inftrees_pad below says -8; or another
  argument area, which this rule cannot see).
- **Fields**: each of our operands votes for its element's base, not for the byte it touches. The listing writes
  `name$[REG+X]` = REG + name$ + X with name$ relative to the region's frame register (FUNC+0x74 / +0x78); X minus
  (rsp - REG when that register is rsp but the code goes through REG) is the field. Without it a struct read only by
  fields (example.c's `d_stream`, 88 bytes, never at +0) or `this$[rbp-127]` (inflate's `code`, 4 bytes, +1) or
  va_start's `format$[rsp+8]` (gzprintf: the address after a home) voted one field off.
- **Alignment**: regmap's alignment with x64 operand shapes (regmap.REG is the 32-bit set: r12 would be an
  immediate), our side as capstone text of the listing's bytes (the original's form), names from the listing text.

framefields uses the same walk and alignment on both sides; on x64 it skips elements of at most 8 bytes (pointers)
unless `--all` and takes access widths from the operands (`qword ptr`) for the extents.

**Validation** (our own 17.00 compile as "the original": ORIG.s = capstone of its listing's bytes; one compile at a
time, local wine, `/O2`; zlib `/I` its folder, slots17 / genframes `/TP /GS`, gen.py `/TP`). "exact" = the offset
`--orig` gives an element is the original's packed offset (the original's own trace, matched by listing name;
`tvN` temporaries are numbered per compile and left out of the perturbed rows).

| source | functions | identity: elements exact (functions) | `volatile char` in front: moved / exact | `volatile int[5]`: moved / exact | one more use (`ext(0); g += v;`): moved / exact; `--search` |
|---|---|---|---|---|---|
| zlib 1.2.1, 14 files | 29 | 70 / 70 (29 / 29) | 14 functions changed, 28 elements moved; 68 / 69 | 20 changed, 28 moved; 67 / 68 | (no such lines) |
| tests/data/slots.c `/GS` | 6 | 17 / 17 | 9 moved; 13 / 13 | 4 moved; 14 / 14 | |
| tests/data/slots17 frames, frames2, tr | 15 | 29 / 29 | nothing moves; 27 / 27 | 27 / 27 | |
| gen.py seeds 1-3 (100 each) | 300 | 2,308 / 2,308 | 266 changed, 924 moved; 1,420 / 1,420 | 264 changed, 960 moved; 1,431 / 1,432 | 34 changed, 86 moved; 2,166 / 2,176; one edit puts every element back in 31 of 34 |
| genframes seeds 1-3 (alloca, dynamic alignment, B < A, removed argument areas) | 300 | 1,103 / 1,103 | nothing moves (the pad lands last); 957 / 957 | 957 / 957 | 48 changed, 116 moved; 890 / 912; one edit restores all in 40 of 48, improves 47 |

Identity is exact everywhere (3,527 elements in 650 functions, 0 depth conflicts). The perturbed rows miss 35 of
8,072 elements, 32 of them in the `use` rows; every miss looked at is an alignment error next to the inserted code
with one or two votes (zlib inflate_fast `window$1$` 1/2 votes; genframes f40 `v1$`: our `lea rcx, v1$` paired with
the original's `lea rcx` of v2), not the walk. `--search`'s winners are the inverse of the perturbation
(`uses:v=N-1`, or a visiting position that amounts to it). framefields: no layout difference reported in 650 of 650
identity functions and in all padded ones but 3 of zlib's (compress2 / uncompress `stream$`, minigzip main
`outmode$`: one pair each misaligned by the inserted store); a second read (`use`) gives 15 such one-pair false
reports in 600. A real layout change, S12 `{ int v[3]; }` -> `{ int x0; int v[3]; }` (gen.py, genframes): every one
of the 186 S12 locals read through a field is reported as moved (`+0->+4` ...), none else; 91 S12 locals only passed
by address (`ext(&v)`) cannot show it. `tests/data/framefields` (a.cpp against b.cpp) on 17.00: the temporary `$T1`
is reported (`+8->+4`, extent 16 ours against 12), the x64 form of the x86 `_value$` row.

Fixtures (`tests/data/slots17orig/`, `run_tests.py` `slots17_orig_tests`, no compile): `gen_use_f46` (8 of 16 moved;
`--search` top edit `uses:#5=2` puts 16 of 16 back), `inftrees_pad` (zlib inflate_table padded; all 12 at the
original's offsets), `gf_padarr_f76` (alloca + dynamic alignment, r13 and the aligned rbp, B < A), `gzprintf_id`
(`__chkstk`, va_start on a home); each NAME.json (snapshot), NAME.lst (our listing, the function only), NAME.s (the
original).
