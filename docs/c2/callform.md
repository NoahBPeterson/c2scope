# The call-form census: missing block or allocator? (`tools/callform.py`)

One-command triage for "why is my draft N bytes short / long". The aligned diff
(`f4check --full-diff`, `icount.py --asm-vs`) shows a **missing whole basic block**
as scattered replacement rows that are indistinguishable from register-allocation
divergence — on `bhkCollisionQuery::Process` a 277-byte absent teardown block was
read as "NColor cascade" for two passes. The census is the discriminator: a missing
structural block almost always changes the **multiset of calls grouped by dispatch
form**; pure allocation or scheduling divergence changes none.

```sh
uv run tools/callform.py orig.s my.s            # the census, flagged deltas, verdict
uv run tools/callform.py orig.s my.s --targets  # also diff the callees per form
uv run tools/callform.py orig.s                 # census of one listing
```

Both listings are `VA mnemonic operands` lines (capstone style; f4check's `+0xN`
form is accepted). Offline: no compile, no trace, no symbol map — it reads only
what the listing says, and normalises away the two things that otherwise bury the
signal: **register names** (allocator spelling) and **callee resolution**
(`FUN_<addr>` in the original's image, mangled `?sym@@` in the draft, `[rip]`
where the original spells `__imp_X`).

## 1. The forms

| form | what | example |
|---|---|---|
| `call [reg] (slot N)` | memory-indirect, displacement / 8 = vtable entry | `call qword ptr [rax]` → slot 0 (the scalar-deleting dtor), `[r8 + 0x18]` → slot 3 |
| `call [reg+d] (other)` | displacement that is not a vtable slot | base+index, not 8-aligned |
| `call [rip] (thunk)` | rip-relative indirect: IAT thunks and function-pointer globals | `call [rip -> __imp_TlsGetValue]`, `[rip]` unresolved |
| `direct` | a direct call, named or not | `call ?slowBlockFree@@QEAAXPEAXH@Z`, `call FUN_141507190` |
| `call reg` | register-indirect | `call rax` |
| `other` | far call, segment forms | |

Nonzero deltas are flagged with a rule-based hint (slot 0 = the virtual-dtor
teardown lead; a direct delta naming `??1…` = devirtualised destructors), and the
report ends with the verdict:

- **deltas → "structural lead"**: a missing/extra block, devirtualisation, or a
  wrong vtable slot; find the first side's calls that have no counterpart and read
  around those addresses.
- **every form equal → the byte gap is not a missing block**: take it to the
  allocator (`ncolspill.py` for the split/spill view, `ncolsim.py whatif` for the
  levers) and the alignment (`icount.py`) for the per-instruction story.

## 2. The measured case (docs of the two passes it would have saved)

`bhkCollisionQuery::Process`, original vs the 7231-byte draft (replayed by
deleting the two teardown calls from the 7417 listing):

```
  call [reg] (slot 0)          2     0    -2   vtable slot 0: a missing virtual-dtor element teardown
## verdict: 1 form(s) differ -> structural lead (missing/extra block, devirtualisation, wrong vtable slot)
```

That one line is the §4.5/§10 finding: two `call [rax]`/`[r8]` element-destruct
loops absent from the draft. After the third pass (virtual dtor on the base,
placement-new construction, two-loop destroy — ticket §10) the same command on
the real 7417 draft prints the other verdict:

```
  (every form equal)
## verdict: the call census is identical in every dispatch form -- the byte gap is NOT a missing block;
   take it to the allocator (tools/ncolspill.py) and the alignment (icount.py)
```

with the 121-byte residue confirmed as the allocator wall (§9.2), not structure.

## 3. What the census cannot say (read the caveats)

- **Unresolved placeholders.** The original's listing spells direct callees
  `FUN_<addr>` where the draft has mangled names, and `[rip]` where the original
  resolves `__imp_X`. `--targets` marks such pairs ("pairs with ... by count:
  symbol-resolution artifact"), and register rows inside a memory form are
  labelled allocator spelling. `--syms FILE` resolves the placeholders through a
  registry — a pack.json (`callee_names`/`data_names`) or any `{"name": "0xVA"}`
  map — printing the resolved name in parentheses; a pair whose sides resolve to
  one callee is marked *confirmed* instead of artifact, and a mangled destructor
  (`??1...`) is never treated as an artifact: it is a finding.
- **Slot numbers assume 8-byte vtable entries** (x64). A non-8-aligned
  displacement is reported raw, without a slot claim.
- **Equal census does not prove equal structure** — two different blocks can call
  the same set. It is a discriminator (structure changed or not), not a proof of
  placement; follow a nonzero delta with `f4check`'s diff and the address search
  named in the verdict.
- A devirtualised call in *both* listings stays a `direct`/`direct` pair and is
  invisible here; the tool sees what the listings show, and slots only move when
  one side dispatches and the other does not.

Tests: `callform_tests` in `tests/run_tests.py` (synthetic listings: equal census,
the missing-teardown case, the devirtualisation case, the `+0xN` form, and the
unresolved-thunk artifact note). Offline, no compile — runs on any build table.
