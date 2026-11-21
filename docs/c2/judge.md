# Static equivalence judge (`tools/judge.py`)

Is a non-matching draft *right* (it only compiles differently) or *wrong* (it does something else)? bdiff and rdiff
measure how close the bytes are; they cannot tell. Mount & Blade 0x4db660, `((UiA*)row[2])->f184`:

| source | bdiff | rdiff | judge |
|---|---|---|---|
| `f184 = 1` (the draft) | 9 | 2 | MISMATCH: ours stores `1`, the original `or(1, ld(...))` |
| `f184 \|= 1` | 48 | 7 | EQUIVALENT |

Both byte scores prefer the wrong source. The judge compares what the two codes *do*.

```sh
uv run tools/judge.py SRC --func F --asm-vs orig.s [--show] -- FLAGS      # compile ours, judge
uv run tools/judge.py --listing ours.asm --func F --asm-vs orig.s          # an existing /FAsc listing
uv run tools/judge.py export rows.jsonl -o jobs.jsonl --symbols mb=DIR ...  # rows {id, func, orig, asm}: decode here
uv run tools/judge.py run jobs.jsonl -o out.jsonl --jobs 4 --timeout 300   # standard library only: runs anywhere
uv run tools/judge.py mutants jobs.jsonl --ids FILE -o mutants.jsonl       # the soundness check below
uv run tools/judge.py hints out.jsonl --out DIR [--project mb]             # the pairings, one <va>.json per row
```

Batches: decode with `export` (capstone, cheap), judge with `run` on another machine if the local one is busy: each
job is its own subprocess with a timeout and a comparison budget, so no function can take the machine's memory.
`--symbols` takes a project's symbol files (`VA name` lines: Mount & Blade `symbols/slices`, Spore `symbols`).

## 1. Model

Both functions are executed symbolically. Every register, local stack slot and x87 stack entry holds an
expression over entry values; memory is a sequence of effects. What instruction selection, register allocation and
scheduling change does not survive the translation:

- linear forms: `add`, `sub`, `lea`, `inc`, `dec`, `neg`, `shl` by a constant, scaled indices all become one sum;
  `and` / `or` / `xor` / `mul` / float `add` and `mul` are flattened and commutative;
- `test r, r` is `cmp r, 0` for the conditions where the flags agree; a `sub` sets the flags of `cmp`; arithmetic
  sets "result" flags (`e`, `ne`, `s`, `ns` against 0); `xor r, r; setcc r8` is a zero-extension;
- loads are values: `ld(size, address, the stores and calls that may alias it)`. A folded load and a separate one
  are the same; a split read-modify-write and `or [m], 1` are the same;
- a local stack slot is a variable named by its offset from the entry esp; pushes are state (call arguments), not
  memory; once any local's address escapes (`lea` of a frame address), locals are memory with a paired address;
- effects compared: stores (address, value, size), calls (target, stack arguments, ecx for thiscall and virtual
  calls, ecx/edx for fastcall; the callee's stack cleanup from our decorated name), `rep` string operations, the
  branch condition (in either sense: layout inverts it), the returned value (eax / edx:eax / st(0) by the decorated
  name's return type). Runs of stores that cannot alias are compared in a canonical order.

**Pairing.** The two sides' names meet through one consistent pairing built while comparing: an entry value of a
loop head in our code with the original's, an effect with an effect, our symbol (a relocation) with the original's
address (one to one; a symbol the project's map names must be at that address), an escaped frame object with the
original's. An entry value may also be paired with a constant.

**Paths.** Execution is cut only at loop heads (targets of back edges): any other join is run through on each path,
so the two sides may merge and duplicate tails differently (Mount & Blade 0x48f9f0: our `ret 0` and `ret ebp`
against the original's one exit). A block that only moves values on (`xor ebp, ebp` before a loop) is part of the
edge. At a loop head every entry pairing must hold on every incoming paired edge: induction over the paths, so loops
are covered without bounds.

## 2. Verdicts

- **EQUIVALENT**: every paired path does the same and every loop-head pairing holds on every edge. Sound up to
  the instruction model and the symbol pairings: `unanchored` counts the pairings no project map confirmed
  (`--show` lists them). Two callees that each appear once can be paired either way round consistently (§3).
- **MISMATCH**: the first effect, condition, edge value or return value that differs, with both expressions.
  Very likely a real difference, but not proved: another pairing, a code motion across a loop head, or the frame
  model (§4) can hide an equivalence. Confirm with an emulator (Mount & Blade / Spore `tools/difftest/equiv.py`).
- **UNKNOWN**: outside the model (a jump table, a loop structure that does not pair, an unsupported instruction,
  a stack depth it cannot follow, the comparison budget).

## 3. Measured (2026-11-20)

130 near-miss functions (the model F rows: 61 Mount & Blade, 13.10; 69 Spore, 15.00; the listings of their scans),
judged on the Windows worker at Idle priority, six jobs at a time, 300 s each: **36 EQUIVALENT, 52 MISMATCH,
40 UNKNOWN, 2 ERROR**. 25 of the 36 assume at least one unanchored symbol pairing.

Against Mount & Blade's own verdicts (`docs/equiv_verdicts.json`, emulation and proofs): of 23 PROVEN rows, 9
EQUIVALENT (8 since the call-convention fixes below), 11 UNKNOWN, 3 MISMATCH (0x49b4f0: the escaped-frame model,
§4; 0x451c50 and 0x47c6b0: the scan's listing and the original differ in the first block, not explained yet). Of
17 PASS rows, 5 EQUIVALENT, 7 MISMATCH. (An earlier note here called 0x41b960 a callee mismatch; it is not: `range_equals` is 0x42c160 on both sides, and
the difference is a local store scheduled across `mov ecx, [esi]`.)

**Soundness check** (`mutants`): single-instruction mutants of the original of every EQUIVALENT row (an
immediate + 1, a displacement + 4, a branch inverted, an instruction dropped, the operands of a sub / cmp swapped
where the order matters). 353 mutants: 314 MISMATCH, 34 UNKNOWN, 5 EQUIVALENT. The five: three in Spore 0x420390
(a dead store to an incoming argument and the two loads that feed it), one store to a stack slot that a later store
at another depth overwrites (Mount & Blade 0x43c9c0, probably dead), and one branch inverted between two callees
with no symbol map entry (0x43f6d0: the swapped pairing is consistent; the verdict reports 5 unanchored pairings).
Bugs this check found and that are fixed: a comparison cache keyed by object ids (reused once freed: 12 of 33
mutants survived), capstone's one-operand x87 forms (`faddp st(i)` is st(i) = st(i) + st(0) popped, `fmul st(i)`
is st(0) = st(0) * st(i)), and a symbol pairing that was not one to one.

Tests: `judge_tests` in tests/run_tests.py (synthetic code: renaming, lea / add, split RMW, `=` / `|=`, store order,
an inverted branch with swapped blocks, a loop in other registers, call arguments, the x87 forms, the callee swap
with and without a symbol map, mutants of a loop).

**Current sources (2026-11-20, evening).** The rows above were judged on the listings of earlier scans. A triage of
the 52 MISMATCHes found 7 judging the wrong function (a listing lookup that took the first PROC whose name
*contains* the decorated name: a local class's member inside `compute_normals` came first; and renamed functions in
the row data), the Spore jobs anchored with Mount & Blade's symbol map, and 3 stale listings. Fixed (an exact PROC
name wins; one symbol map per project), every row compiled from its current source with its own c2 build:
**43 EQUIVALENT, 43 MISMATCH, 42 UNKNOWN, 2 ERROR** (30 of the 43 with an assumed pairing); of Mount & Blade's 23
PROVEN rows 8 EQUIVALENT, 12 UNKNOWN, 1 MISMATCH (0x49b4f0: four frame stores on either side of a branch); mutants
of the equivalent rows: 5 of 425 survive, the same five as above. Of the triage's REAL rows the Spore session fixed
0x0091c2d0 (now proven equivalent) and 0x0106eb40, and its own checker found the differences in 0x008027a0,
0x008300e0, 0x008e2b90 and 0x0054def0; Mount & Blade fixed 0x4db660 with `|= 1`.

## 4. Hints for another prover

`hints` writes what the judge paired, per row: `regs` and `slots` (our register / stack slot, offset from esp at
entry, holding the original's value at every loop head the judge compared), `at` (the same per loop head, keyed by
the original's address), escaped stack addresses (in `slots`), `globals` (our symbol: the original's address) with
`assumed` naming those no project map anchored, and the row's `verdict`. A straight-line function has no loop heads:
its hints are frame slots and globals only. Hints from a MISMATCH or UNKNOWN row hold up to the point the judge
stopped.

## 5. Limits

- **Escaped frames.** When any local's address escapes, every local slot is memory, so a spill placed elsewhere
  is a different store (false MISMATCH). Mount & Blade's prover names the same blocker
  (`equiv_proofs_for_spore.md`, lesson 5).
  **Tried: `--frame-objects` (experimental, off).** Pass 1 treats every local as memory and pairs the escaped
  addresses; pass 2 treats only the bytes of our listing's escaped objects as memory (`_x$ = -12 ; size = 12`;
  the frame base the values are relative to differs by function, 0 or -4, and is measured from our own accesses),
  the original's object at its paired address with our object's size. On the 130 rows it ran on 20 and changed no
  verdict to EQUIVALENT: escaped frames are not where the false MISMATCHes come from. It is also unsound where the
  original's object is larger than ours (Spore 0xc3db50 writes a float 4 bytes past our object: a mutant of that
  store was EQUIVALENT), hence off.
- Calls: a direct callee's convention and stack cleanup come from our decorated name (the original takes the
  paired call's); a call through a vtable (`[[obj] + k]`) is thiscall; any other indirect call is cdecl when an
  `add esp` follows it and callee-cleaned otherwise, read on each side; `__chkstk` / `_alloca_probe` move esp by
  eax; a function may end in a call that does not return.
- Jump tables, `lock xadd`, `lahf`, x87 control words, x87 values live across a loop head: UNKNOWN.
- Callee-saved registers are not checked to be restored; floating-point precision (x87 80-bit against a 32-bit
  store) is modelled as `fround` at stores only.
