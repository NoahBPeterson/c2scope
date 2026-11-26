# slotwant: from the original's stack-slot layout to the source edit that gives it

`c2trace.py slots` (docs/c2/slots.md) explains a frame forward: for each element, the colours the packer tried and why
it refused them. `tools/slotwant.py` runs the other way, in the style of `schedwant.py` (scheduler) and `sulever.py`
(operand-sort weights): from a wanted layout to a ranked list of source edits, each one compiled with the slots trace
and judged on what it really did.

```sh
uv run tools/slotwant.py SRC --func F --image EXE --va VA --diag -- FLAGS      # the wants the original implies
uv run tools/slotwant.py SRC --func F --image EXE --va VA [--want W ...] -- FLAGS
uv run tools/slotwant.py SRC --func F --asm-vs ORIG.s --want 'with:$T183410,_namebuf$118012' -- FLAGS
```

`--asm-vs ORIG.s` is the original as `VA mnemonic operands` lines; `--image EXE --va VA` reads it from the image and
uses the callees' `ret N` for the push depth (for Mount & Blade: `work/mb.analysis.bin`). Options: `--max-compiles N`
(16), `--jobs J` (4 compiles at once), `--depth 1|2` (single model edits, or pairs too), `--no-verify` (print the
levers, compile nothing), `--keep DIR` (default `work/slotwant/<stem>-<hash>/`, with `base/` and one `vK/` per lever).

## 1. What decides a slot (the inputs the tool inverts)

From slots.md section 1, the packer's decision for element X depends on three inputs, and nothing else:

| input | what it decides | source levers that move it |
|---|---|---|
| **size** | the visiting order (size ascending) and which colours X may join (at most twice the colour's current size) | an array's dimension; a scalar's type width (bool/char vs int: a 1-byte colour admits at most 2 bytes) |
| **use count** (frame operands in the final code, before the late flow merges) | the visiting order within a size, and the colour order when T > 128 (key 1000 * uses / size) | code changes only: a read cached in a register, an arm written twice; no code-neutral lever |
| **conflicts** (LiveOnDef) | whether X may join a colour at all | the scope of an address-taken local (live from its definition to its scope's end once it escaped and is read again; frame.md 4.4); where its first definition is; one variable for two |

Two consequences the tool's "why" line prints when they apply:
- **A larger element can never join a colour made by a much smaller one.** Elements are visited by size, and a
  colour admits at most twice its current size, so a 256-byte buffer joins a 64-byte temporary's colour only if
  something of 128..255 bytes joined it first. When the original has the 64-byte temporary inside the buffer's slot
  and ours has no such bridge, no conflict or order edit can do it (Mount & Blade 0x457b40 below).
- A colour is tried **newest first**: X that could join Y's colour may take a newer colour first. The wanted join then
  needs X to conflict with the newer colour's members, or to be visited before the newer colour exists (a size or use
  edit), not a conflict removed.

## 2. The steps

1. **The want.** Explicit: `with:A,B` (A shares B's slot), `apart:A,B`, `at:A=OFF` (packer offset), `frame=N` (the
   `sub esp` of the prologue), `ncol:SIZE=N` (N colours of that size). Names as c2slots' what-if takes them: a
   listing name (`_namebuf$118012`), a stem, `L<line>` (the element first referenced on that line; best for `$T` and
   `tv` temporaries, whose numbers change between compiles) or `#IDX`. Derived from the original (`--asm-vs` /
   `--image`, no `--want`): its frame size, and for every pair of well-voted elements (2+ votes, a majority) of 8+
   bytes, `with` or `apart` where the two listings disagree. Pairs are shift-invariant: an extra slot of the
   original low in the frame moves every offset below it, not which elements share; `--diag` prints them.
2. **The original's slots.** Each of our elements gets the original's address from the aligned code (regmap's
   alignment). Two estimates per operand, and per element the one whose top address has more of its votes:
   c2slots' walk of the original's push depth, and **our own push depth at the aligned instruction** (the listing's
   `name$[esp+K]` gives it exactly; aligned code has the same pushes): `key = disp_orig - K + (frame + saves)_ours -
   (frame + saves)_orig`. The field offset of a struct access cancels too. On 0x457b40 the walk put msgbuf's two
   blocks 4 bytes apart (-512 / -508, a lost push) and many 16-byte temporaries at -984 against -988; with our depth
   all of them agree, and 16 more elements (the `$T`s) get an address.
3. **Why** (each unmet `with`): the later-visited element's decision against the other's colour: not made yet,
   refused for size, refused for a conflict with members M..., or a newer colour joined first.
4. **The model inverse.** c2slots.Model replays single edits, then pairs of the 40 most promising (`--depth 3`:
   the 10 best pairs with a third edit): `size:X=N`
   (halved, doubled, quartered; `^`: at the front of its new run), `uses:X=N` (+-1..3), `conf-:X,M` for each refusal
   on X's way, the composite join (every conflict between X and the wanted colour removed at once), `conf+` with the
   members of the colour X joined, and a bridge (`add:bridgeN`) when the wanted colour is too small for X: an element
   of N bytes the original has and we lack, between the two sizes. Score: weight of the wants met, then (with an
   original) the number of large well-voted elements whose slot-mates are the original's, then the frame against
   the original's, then the elements moved.
5. **Levers** (source edits; no line moves, so the listing's line numbers stay valid and the variant's elements are
   matched to the base's by name stem and first line). Each winning model edit becomes the levers that make it; a
   pair becomes one edit doing both:

   | lever | text | model edit |
   |---|---|---|
   | `narrow:X@L` | `{` before X's declaration, `}` after the statement of its last use (when no declaration in between is used after it) | conf- |
   | `narrow-late:X@L` | the same, the declaration (no initializer) moved down to the first use | conf- |
   | `widen:X@L` | the declaration (builtin or array type, no initializer) moved to the function body's first line | conf+ |
   | `share:X,Y` | Y's declaration blanked, its uses renamed X, X declared in the block holding both (same type) | with:X,Y |
   | `size:X@L=N` | the array's dimension for N bytes | size: |

   plus narrow / narrow-late / widen for every named element of the unmet wants (the model cannot see a scope
   change's effect on LiveOnDef exactly: these are tried anyway). Scope levers are made only for address-taken
   elements (a `lea` of them in our listing): any other element lives from its definitions to its uses, so its
   scope changes nothing (0x448eb0 below: 14 such levers, none moved anything).
6. **Verified.** Every lever (at most `--max-compiles`) is compiled with the slots trace. Per variant: the wants met
   in the traced packing (`WANT` = all, `better`, `no`, `worse`), the frame, bdiff against the original, `code
   changed` (regdiff of the variant's listing against ours: 0 = only frame offsets differ), `regrouped` (elements
   whose slot-mates changed: offsets all shift when the frame size changes, slot-mates are what really moved), and the
   number of elements at the original's address. `verified.json` in the work dir holds the table.

## 3. Results

Perturbation round trips (tests/probes/slotwant.py; our own compile of the X_orig source is the original):

| fixture | perturbation | derived wants | top verified lever |
|---|---|---|---|
| `tests/data/slotwant/scope.cpp` | `char a[64]` at function scope (orig: in its own block) | frame=64, with:a,b | `narrow:a@L7` WANT (frame 128 -> 64, code changed 0); `share:a,b` also WANT |
| `size.cpp` | `char h[256]` (orig `h[64]`) | frame=384 | `size:h@L12=64` WANT, bdiff 4 -> 0 |

Mount & Blade (cl 13.10.3077, `src/game/agent/agent_logic.cpp`, the full unit: 45 s per traced compile):

- **0x457b40 `Agent::process_blow`** (T30 item 8: ours 0x42c, the original 0x3f0). The original's map (our-depth
  estimate): msgbuf at -512, namebuf and the attach_psys temporary `$T183410` at -768, M at -832, **mm, m2 and both
  hbufs at -912**. Ours: both hbufs in namebuf's 256-byte colour, `$T` a colour of its own at -64.
  The wants `with:_hbuf$117756,_mm$117805`, `with:_hbuf$117772,_mm$117805`, `with:$T183410,_namebuf$118012`,
  `apart:$T183410,_M$117802`, `--depth 3`:
  - why (`$T` with namebuf): "colour 18 (made by $T, 64 bytes) is refused for size": a 256-byte buffer cannot join a
    64-byte colour, so no conflict or order edit does it (the 2x rule of section 1).
  - the model's best: `size:hbuf=64^` twice and `size:namebuf$118012=128` (10 of 39 large elements share slots as
    in the original, against 4). The 128-byte namebuf joins `$T`'s colour first (64 -> 128) and the other 256-byte
    namebuf then fits (256 <= 2 x 128): the bridge.
  - verified: **`char hbuf[64]` at L9602 and L9624, `char namebuf[128]` at L10015: WANT (all four)**, frame 1068 ->
    1004 (the original 1008: the 4 bytes left are the original's spill slot that ours does not have, which M&B's
    frame_solve also lists), code changed 0 (only frame offsets move), bdiff 1241 -> 1222, 12 of 77 elements at the
    original's address (from 0; the others sit 4 bytes off, below that spill slot). The hbufs alone (`hbuf[64]` x2)
    meet the two hbuf wants (bdiff 1237); one hbuf at 128 instead bridges `$T` and namebuf but leaves mm's slot.
  - these are type hypotheses (smaller buffers): the lane checks them against what the code writes there
    (ui_weapon_class_name into namebuf, the two sprintf into hbuf).
- **0x448eb0 `Agent::ai_update_control`** (five 16-byte colours in the original, six in ours; frame 33132 against
  33112). `--want ncol:16=5 --want frame=33112`: the model meets both with a 16-byte element at 32 bytes or one
  conflict removed (`conf-:_tv,_p`, `conf-:_q,_tv`), but every element it names is a vector4 that is never
  address-taken: its lifetime is its definitions and uses, so no scope lever moves it (14 narrow / widen levers
  compiled: all leave the packing as it is). The lever this case needs is a statement moved (where `_tv` or `_q` is
  defined), which the catalogue does not have yet.
- **0x4465e0 `Agent::update_ai`** (frame 176 against 160; M&B: the original has two slots we lack, an ItemRef and an
  iterator passed by address). Derived wants: 24 pairs around the 16-byte `q` temporaries. Levers: share / narrow of
  `q`; `share:q,q` meets 64 of 388 but moves 53 elements and makes bdiff worse (1207 -> 1272, code changed 24): no.
  The missing slots are elements the original has (an `add:` in the model), not a colouring choice.

## 4. Limits

- Levers are text edits on one function's body found by a light C++ reader (comments and strings masked, braces,
  statements, `T name[...]` declarations). Multi-declarator lines, macros that hide braces and declarations
  inside `for (...)` are not edited. `$T` and `tv` temporaries have no declaration: their levers are the named
  elements around them.
- Use counts and visiting positions have no code-neutral lever; the model prints such answers but nothing is
  compiled for them.
- A size lever changes the program (a smaller buffer): it is a hypothesis about the original's type, to check
  against what the code stores there.
- Derived pair wants are as good as the alignment; on big functions many are noise. Name the few that matter.
