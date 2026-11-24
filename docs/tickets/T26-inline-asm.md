# T26: inline asm blocks — model them, lift them from the binary, and regenerate their source from the surrounding C++

Opened 2026-11-24 from the user's question. We match assembly against candidate sources; when the
ORIGINAL source used inline asm (`__asm { ... }`), the candidate must reproduce those blocks
byte-exactly. The ask: is there a way to **model, reverse, or lift** these asm blocks to matching
source code, using the rest of the C++ source as the guide?

## The premise, and why the tools should be good at this

The docs already say the shape (verify each, they are marked not-re-verified):

- `frontend_il.md` §6.9: an `__asm` block is **one opaque `OPASM` tuple** (opcode 0x2ce, il_tuple.md;
  consecutive blocks are merged into one); a naked function is just that tuple with no prologue,
  epilogue or `ret` from c2; a non-naked function still gets its frame code.
- `il_pipeline.md` §5.4: `FUNC.flags & 0x10` = the function contains `__asm`; under globopt, if
  `MscCheckAsm` and the function is not asm-only, **warning C4740 and GlobalOptimize off for this
  function** ("when the asm reads or writes registers it cannot track"). The 17.00 index carries the
  string "Inline asm has flow" (inline.obj).
- `frame.md` §"frame code": asm counts as a machine opcode (`0 < op < 0x273`, "inline asm and so
  on") whose operands the frame walk reads like any other.
- `effectmine.md` already measured a real case: Fallout 3 `0x009c7730`, a block holding an `__asm`
  write (`mov dword ptr [tick], eax`). `callee.md`'s `z02` probe is a callee containing `__asm`.

Two consequences fall out of "the block's instructions are emitted **verbatim**" (the scheduler does
not touch them):

1. **Matching:** once the surrounding compiler-generated code matches, the island's bytes must match
   exactly — the block is the *strongest* fingerprint in the function, not a liability. A candidate
   source whose only unknown is the block can be verified byte-wise over the island alone.
2. **Lifting:** the block's source text is recoverable from the binary (disassemble the island in the
   compiler's own syntax). The hard part is not the instructions — it is the **bindings**: which C++
   local or label each symbolic reference in the original source bound to. In the binary those are
   registers, frame slots, addresses; the source wrote names.

And the "guide" is concrete, not hand-wavy: **the surrounding C++ fixes the frame map, the local
names, the register assignment, and the island's position** — everything the bindings need. Better:
when C4740 fires, the *whole function* compiles with GlobalOptimize off, i.e. in the /Od-style regime
whose rules the tools already document (frame.md's /Od allocation rules, no packing, first-reference
allocation). The rest of the source around an asm block is therefore compiled under the most
modelable mode we have.

## What the tools already have (each to be verified, not assumed)

| tool / doc | what it offers for asm |
|---|---|
| `OPASM` 0x2ce (il_tuple.md), `FUNC.flags & 0x10` (il_pipeline.md) | the IL models the block as one opaque tuple; what the tuple CARRIES (assembled instructions? byte count? source text? clobbers?) is unread — ask 1 |
| the tracer's IL dumps (docs/c2/tracer.md) | how a `0x2ce` tuple prints per build (the plain dump, the `%+` notes) — unread |
| `lift.py` (`--asm-vs`) | lifts the ORIGINAL's machine code to c2's IL at Lower's entry and aligns it to ours, per tuple and source line. At an island: our side is one opaque tuple, the original's side is N instructions — the alignment needs the island marked and its range reported as one unit |
| `il2c.py` | IL → C++, one statement per tuple — the natural home for regenerating `__asm { ... }` text, if the tuple carries (or the encoder can rebuild) the block's instructions |
| the `/Fa` listing of OUR source | the block's text and its exact encodings, printed by the compiler's own encoder — the candidate side's ground truth for byte-diffing the island |
| the frame tools (frame.md, framefields.py, orig_slots-style maps) | the island's `[ebp-N]` / register operands bind to the frame map — the binding table's raw material |
| the allocation models (c2regs x86, c2ncol x64, ncolsim) | the block's clobber effect on allocation is measurable: a register the asm destroys cannot hold a C variable across the island — *if* c2 models the clobber at all (measure) |

## The asks

1. **The OPASM story per build (14.00, 15.00, 17.00).** What the tuple carries (assembled
   instructions? a byte blob? the source text? the clobber list), what the IL dump prints for it,
   whether a block is really ONE tuple (frontend_il says one, consecutive merged — verify with the
   probe family), and what `FUNC.flags & 0x10` actually gates: measure the C4740 condition (flow
   in/out of the block, register-clobber heuristics) and whether the rest of the function really
   loses GlobalOptimize — **that single fact decides which matching rules apply around the block**.
2. **Measured effects around the island** (probe family below, each build): scheduling (nothing
   moves across — verify in the final IL and the encodes), allocation (are the clobbered registers
   unavailable over the island's range? `c2regs --why` / `c2ncol --why`), the frame (does
   referencing a local in asm force it to memory and out of the candidate set? if yes that is ALSO a
   matching lever: a one-asm-reference local is forced out of the register pool on purpose),
   FPO/ebp (frame.md: no FPO with asm that modifies ebp), and with globopt off: do the /Od rules
   (name-order allocation) apply to the rest of the function?
3. **The lift-and-regenerate procedure** (the core ask). Given the original disassembly and a
   candidate source that matches around the blocks:
   - **Pin the island:** the candidate's `/Fa` listing contains the block's instructions verbatim —
     locate the same encodings in the original (exact byte match over the block's range; the
     candidate side knows the length).
   - **Check the neighborhood:** the compiler-generated code around the island must match; a
     mismatch there is a source problem, not an asm problem (the guide in action — fix the C++,
     not the asm).
   - **Regenerate the block's text:** disassemble the island in the syntax cl's assembler accepts
     (MASM/Intel forms, the exact expression subset per build), labels preserved, and bind the
     symbolic references via the frame map: each `[ebp-N]` / register operand inside the island →
     the frame map's slot/parameter table → the C++ name.
   - **Round-trip verdict:** compile the regenerated source; the island's bytes must equal the
     original's exactly — one unambiguous line, the f4check convention.
4. **The tool shape:** extend `lift.py --asm-vs` (it already lifts and aligns — add the island
   marking and the regeneration), or a sibling that takes (lift JSON or disassembly, candidate
   `/Fa` listing, frame map) and prints each island's regenerated `__asm` text plus its binding
   table (operand → local). The verdict goes through the existing checkers.
5. **The failure modes, measured** (they are the match's actual risk): the assembler's encoding
   choices inside the island (immediate widths, short vs near jumps — cl's assembler vs the
   disassembler's defaults), island-internal labels and how disassembly renders them, the MASM
   expression subset per build, flow into/out of the block (C4740's exact condition), and
   `__declspec(naked)` (frame.md's separate path: the tuple is the whole function).

## How this feeds the other tickets

- **T25 ask 2 (the merge-blocker construct family):** an asm reference to a local is a
  force-to-memory lever the construct family should carry (`__asm { mov eax, x }` as an explicit
  "keep this local out of the register pool" edit) — same shape as `hs_bind`, measured the same way.
- **T16/T17 (x64):** the OPASM tuple and the flags bit exist on 17.00 too; the probe family runs
  there unchanged.
- **The matching workflow:** islands are position-stable fingerprints — if a function's source is
  unknown but the binary shows an asm island, the island's bytes anchor the function before any
  source guess is needed.

## Test material (ours, per T20)

Each compiled at 14.00 and 17.00 (15.00 where the question touches Spore):

- `z_asm_reg` — a register-only block (`__asm mov eax, ecx`), no symbolic references: the pure island.
- `z_asm_local` — a block referencing a C local and a parameter symbolically: the binding case.
- `z_asm_label` — a block with internal labels and a jump: the label-rendering case.
- `z_asm_clobber` — a block that destroys every volatile register: the allocation-effect case
  (expect the C locals around it to be pushed to memory; measure exactly which, and whether the
  allocator saw the clobber or was saved by the spill convention).
- `z_asm_flow` — flow into/out of the block from C code: the C4740 / globopt-off case.
- the motivating function: the f4decomp target with inline asm blocks (the user's session has it) —
  the procedure's acceptance test is that function matching end-to-end.

## Risks / unknowns

- If the tuple does not carry the block's instructions (only a blob / byte count), the regeneration
  path goes through the encoder side instead (the `/Fa` machinery) — still workable, but measure
  first; ask 1 exists for exactly this.
- The bindings are only as good as the frame map: /Od slots are disjoint (frame.md), which helps;
  enregistered locals cannot be bound by a frame map at all — but a block reference forces them to
  memory first (measure; if true, the binding table is always frame-based).
- The scheduler/barrier claims are [I] until the probe family runs; the ticket exists to turn them
  into [C].
- cl's inline-assembler is a MASM subset whose accepted forms drift across builds (13.10 → 17.00);
  the regenerated text must compile on the target build's own assembler, so the per-build quirks
  are part of the match, not noise.
