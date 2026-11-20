# C++ from c2's IL (`tools/il2c.py`)

lift.py (docs/c2/lift.md) reads the original's IL at Lower's entry from its machine code. il2c.py goes the rest of the
way back: it writes C++ from IL at Lower's entry, built so that compiling it gives that IL again, and with it the same
code. Matching the original *source* is not the goal; matching its *code* is.

```sh
uv run tools/il2c.py emit tu.cpp --func F [-o OUT.cpp] -- FLAGS       # the function's IL written back as C++
uv run tools/il2c.py test --rows rows.jsonl --limit N --jobs 10 --out DIR   # the round trip, measured
```

Needs `llvm-undname` (Homebrew `llvm`) for callee and global names.

## 1. Why it can work

The IL at Lower's entry has been through GlobalOptimize: CSE, copy and constant propagation, folding and dead stores
are done. Written back one statement per tuple, the front end turns it into nearly that IL again (the reader IL is a
left-to-right flattening, one tuple per operator: frontend_il.md), and the optimiser finds little left to do: the IL
is close to a fixed point. The temporaries and casts the emitter adds are copy-propagated away. Whether the back end
then makes the same code depends on what the tuple stream does not carry; §3 lists what was found.

## 2. What is written

- The body is replaced; the signature, the unit and its declarations stay. Every IL temporary and compiler symbol
  is a local declared at the top with the C type of its type code (1001/1002/1004/1008 signed, 2xxx unsigned,
  4004/5004/e004 float, 4008/5008/e008 double; `e` is an SSE register on /arch:SSE). Source variables keep their
  names (`v_` prefix for locals).
- Memory operands are `*(T*)((char*)base + idx*S + D)`; a `pas` operand on a load, store or RMW marks a volatile
  access (Spore 0x68c700: `*(volatile uint32*)&key->mGroup ^= bits`) and is written `volatile`.
- Opeq's `[m] <- [m] op x` is the compound assignment `m op= x` (the front end gives `x = x op y` another tuple, and
  the re-optimised code then merges a read the original kept).
- Compares take their signedness from the operand type; `cc=b/be/a/ae` are unsigned whatever the operands
  (`cc=g` on 2004 operands is `ja`).
- Control flow is structured, because the front end writes `if (c) { S }` as one branch on `!c` over S, while
  `if (c) goto L` is two branches that the flow passes lay out differently: a forward branch over a region no
  outside jump enters is `if (neg) { region }`, with a goto over a second such region `if .. else`; a backward
  branch to a label only it uses is `do { .. } while (cond)`. Loops come back in the rotated form c2 left them in
  (`if (0 < n) { do .. while }`). Float conditions are negated as `!(..)` (NaN).
- Calls by undecorated name (llvm-undname), each argument cast to the parameter's type (a reference parameter gets
  `*(T*)addr`); member calls qualified, `((C*)p)->C::m(..)`, a direct call as in the IL. A call through `[vp+D]`
  where vp was loaded from the first argument is a virtual call through a class declared before the function with
  a virtual function at slot D/4 of the IL's argument and result types; any other indirect call goes through a
  `__cdecl` function pointer. Overloads: the definition with the decorated name's parameter count.
- A byte variable returned by a `bool` function is declared `bool` (else the return converts it: an extra test).

## 3. Measured (2026-11-20)

300 of Spore's 17,237 matched optimised functions (every 57th, 15.00; matched means our compile of their source is
the original's code, so the round trip is checked against our own compile):

| run | emitted and compiled | byte-identical (bdiff 0) |
|---|---|---|
| first version | 64 | 34 |
| + compare signedness, RMW as `op=`, volatile, overloads, `bool` locals, virtual calls | 108 | 63 |
| + SSE float types, operand parsing fix, addresses of globals | 122 | 69 |
| + structured control flow | 122 | **85** (70% of compiled) |

Probe (tests/data/il2c/il2c_round.cpp; tests/probes/il2c.py): a loop over a member array, an unsigned compare with
a member call, an RMW and a virtual call round-trip byte for byte on 14.00, 13.10 and 13.10.3052; on 15.00 all but
the loop.

Not written yet, by count in the 300: tuples (`?:` OPQUESTION 8, OPMULHI 6, intrinsics 5, switch 3, struct
arguments and copies 4), address-taken locals 33 (a struct local's fields are separate symbols in the IL with no
record of their parent: Spore 0x625600 `Vector3Copy tmp = *pPos`), function definitions rwsweep's lookup does not
find 25, C functions 17, addresses of vtables and string literals 22, constructor / destructor / operator calls 7.
29 rows did not compile in the test setup (the slice needs more include paths): not counted against the emitter.

## 4. What the tuple stream does not carry (where a compiled round trip still differs)

- **Alias classes (PAS).** Two reads of one address are merged only with the same points-at-set index; the original
  can carry different ones (Spore 0x5cc0e0 `if (!mFlag96 && mWin) mWin->SetFlag(1, false)`: the compare's read has
  PAS 6, the read before the virtual call 0x20, loadcse.md R2), so it keeps two loads where the emitted code keeps
  one. `C2TRACE_ILPAS=1` shows the indices; the emitter does not yet choose spellings by them.
- **15.00's loop unrolling** runs before Lower and is not idempotent: an unrolled loop written back is unrolled again.
- Facts known to sit outside the tuples (frontend_il.md, effectmine.md §7): NRVO, EH states, declaration order
  (symbol keys, slot order).

## 5. Limits

The emitter starts from IL that is exact (our own compile). For an unmatched function, the IL comes from lift.py,
whose tokens do not yet carry operands, constants or types: emitting from the original's lifted code needs those
first. At /Od nothing removes the emitted temporaries; an /Od emitter has to fold single-use temporaries back into
nested expressions (the same folding would also give `?:` for a value merged from two arms).
