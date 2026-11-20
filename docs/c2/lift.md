# Lifting machine code to c2's IL (`tools/lift.py`)

icount says where our code has instructions the original lacks, and which pass made them; for a `structure`
instruction (the front end wrote other IL) it cannot say what the original had instead. lift.py reads that from the
original's machine code: it lifts the code back to c2's IL at **Lower's entry** and compares it with our IL there,
so a difference is named as an IL operation at one of our tuples and its source line ("ours `st`, original
`rmw.or`" at `p->a = 1;`: the original ORs).

```sh
uv run tools/lift.py tu.cpp --func F --asm-vs orig.s -- FLAGS      # one traced compile; --show: the self-check's misses
uv run tools/lift.py selfcheck --rows rows.jsonl --jobs 10 --out DIR   # rows: {id, src, func, c2, flags, orig}
```

icount runs it on its own compile (no compile more) and prints the result under the runs (`lift (...)`), when the
rules give back at least 80% of our own code; below that it says so instead.

## 1. Why Lower's entry, and why a backward pass works

The pipeline after Lower (instruction selection, register allocation, scheduling, peepholes) is nearly invertible
locally: each instruction comes from one tuple pattern, and what allocation and scheduling erase (names, order) an
IL token does not need. The optimiser before it is not invertible: CSE, copy and constant propagation, folding and
dead stores are many-to-one. It need not be: at Lower's entry the difference of the two IL streams *is* the
difference of the two sources after optimisation, and our tuples carry their source lines. So the backward pass
stops there. It is checked on every compile for free: lifting our own code must give back our own IL (the
self-check).

## 2. Tokens

One per tuple or instruction, the same vocabulary on both sides:

| token | IL at Lower's entry | code |
|---|---|---|
| `ld` | an operand read from memory (each memory source of an operation is one explicit `ld`) | `mov r, [m]`, a memory source operand |
| `st` | an assignment to memory (any value; a float constant may be stored as an integer) | `mov [m], x`, `fstp m`, `movss m, x` |
| `rmw.OP` | Opeq's `[m] op= x` | `op [m], x`; `mov r, [m]` .. `op r, x` .. `mov [m], r` within 7 instructions (13.10 Riscify) |
| `OP:K,K` | arithmetic into a variable / temporary | `add` `sub` `and` `or` `xor` `imul` `shl` `shr` (`sar` = `shr`), `lea` not of a frame slot = `add` |
| `cmp:K,K` | OPCMP | `cmp`, `test`; a branch on the flags of arithmetic gets the compare it stands for |
| `jcc` | OPBRANCH | `jcc` (the condition is left out: block layout can invert it) |
| `push` | OPARG | `push`; `mov`/`lea`/`xor` into `ecx` before a call (`this` of a thiscall) |
| `call`, `copy`, `div` | OPCALL, OPMBASSIGN, OPDIV / OPMOD | `call`, `rep movs`, `idiv` / `div` |
| `f.add` `f.sub` `f.mul` `f.div` `f.cmp` `f.cvt` | float operations (types 4xxx on 13.10, 5xxx on 14.00 / 15.00) | x87 and SSE scalar arithmetic, compares, conversions |

Kinds: `R` a register, a variable or a constant (c2 keeps some constants in registers, so constant and register are
one kind; constant moves are copies), `M` memory. A frame slot (`[esp+..]`, `[ebp-..]` when `ebp` holds the frame)
and an absolute address (a global) are variables. Dropped on both sides: copies, constant moves, the prologue and
epilogue (as many pushes of a callee-saved register as there are pops of it), the stack adjustment after a call,
`jmp` / OPGOTO (layout), float loads and stack shuffles, `lea` of an address that is only dereferenced, listing
pseudo-instructions.

## 3. Alignment and the report

Our IL against our code: a sequence alignment, then each still unmatched token paired with an equal one within 8
positions (the scheduler moved it). The original against our IL goes **through our code**: the two codes come from
one compiler, so their alignment follows the real instruction order (two equal stores are not swapped), and the
self-check ties each of our instructions to its tuple. The report lists the unmatched stretches at our tuples'
source lines (`~line` for an original-only stretch: the line of the matched token after it). Left out, and counted:
an original token that pairs with one of our own code's tokens the rules could not lift (our code has it too), and
an IL token our own code does not give back either (both are gaps of the rules, not differences of the sources).

## 4. Measured (2026-11-20)

Self-check over 126 near-miss functions (61 Mount & Blade, 13.10; 65 Spore, 15.00): median 86%, mean 80% of tokens
matched. On the 20 rows with an icount `structure` extra at a source line: where the self-check reaches 80% (14
rows), a lift difference lies within one line of the extra in 10; the other 6 are below 80% and not shown.
Examples: Mount & Blade 0x4db660 line 280, ours `st`, original `rmw.or` (`((UiA*)row[2])->f184 = 1;`, the original
ORs); 0x4ef7d0 line 65, ours `cmp`, original nothing (`if (rank >= cap)`: the original does not compare there);
Spore 0xd1f250: the original *has* the `p->mRef = 1` store that `rwsweep --families repair` "improved" by dropping
(a drop lowers the bdiff by removing misaligned instructions; the lift does not have that bias).
Probe: tests/data/lift/lift_rmw.cpp, `= 1` against `|= 1`, named at its line on all four builds (tests/probes/lift.py).

## 5. Limits

- Inline intrinsics (`strlen`, `strcpy`, `strcmp`, `memcpy`): a call with arguments at Lower's entry, a loop or
  `rep movs` in the code. Functions full of them self-check poorly (Mount & Blade 0x4b9110: 46%).
- Division by a constant (a multiply and shifts), `cdq`/`sbb` idioms, `setcc`: partly lifted.
- Struct copies unrolled into loads and stores: one `copy` token against several.
- Arguments passed in registers other than `ecx`, and `this` left in `ecx` from an earlier call: counted wrong.
- A token says what kind of operation, not on what: two stores to different members are the same token, so a
  store moved to another member shows up only as a count difference somewhere.
- The original's IL above Lower (before Opeq, AddrModes) is not reconstructed: a difference is reported in the
  optimised IL's terms, and the source construct behind it is left to the reader (frontend_il.md, the line).
