# lift2il: retail x86 code to C++ that recompiles to it

`tools/lift2il.py` lifts a function's machine code to c2's IL at Lower's entry (the dialect `c2il.py` dumps);
`tools/il2c_lift.py` writes that IL as C++ through `il2c.py`'s emitter. Compiled at the binary's flags, the C++ should
give the original bytes back. 32-bit x86 only, MSVC 7.1 / 8 / 9 (c2 13.10, 14.00, 15.00).

```
il2c_lift.py lift BINARY VA --size N --c2 BUILD [--partial] [--outline]
```

- `--c2` names the build the binary came from: it decides the IL's float types (4004/4008 on 13.10, 5004/5008 later).
- `--partial` writes what lifts and a `/* lift2il: unsupported <what> at <va> */` comment at each construct it cannot
  lift (an EH shape it cannot write leaves the plain body, the EH states as comments): a draft of a big function, never
  a match.
- `--outline` prints one line per basic block (size, memory reads / writes, named calls, how it ends, loop heads, the
  EH frame) instead of C++: for functions too big to read as code.
- With a corpus (`corpus.py`'s JSONL: an image, rows of VA / size / relocation mask), `il2c_lift.py check` lifts,
  compiles and scores a sample (`lbench.py`'s scorer).
- `l2c_search.py CORPUS VA...` (or `--from CHECK.jsonl`, the DIFF rows of `il2c_lift.py check --json`) takes the
  near misses further: it compiles every single rewrite of the lifted C++ that leaves what it computes alone
  (adjacent independent statements swapped, a compare's or a commutative operator's operands exchanged, two
  declarations swapped, a temporary's signedness, a reference alias that shifts the symbol keys behind register
  ties: symkeys.md) and keeps the closest, for `--rounds N`. On 442 register / copy / schedule near misses it matched
  47; on Spore's 124 large near misses, 4. A variant is never proved equivalent: a MATCH is the original's bytes, a
  DIFF only the search produced is a lead.

## How it works

1. capstone, then the CFG; switch tables (`cmp r, N; ja D; [movzx r, byte [r+B]]; jmp [r*4+T]`) are found first, and
   the instruction list stops at the tables inside the function's bytes.
2. The stack: esp tracked from the entry, so `[esp+d]` and `[ebp+d]` name one frame slot (`_aN` an argument, `_lN` a
   local). The prologue (callee-saved pushes before any write, also shrink-wrapped ones past the first branch, frame
   setup, `push ecx` slots) leaves no tuples.
3. Register webs: reaching definitions over the CFG; one web is one variable. ecx read from the entry is `this`.
4. One IL tuple per instruction pattern; calls take the pushes and stores since argument setup began. The calling
   convention is inferred (the callee pops: `__stdcall` / `__thiscall`; ecx written for the call: `this`; cl merges
   cdecl cleanups across consecutive calls).
5. il2c_lift writes C++: control flow structured by il2c (if, if / else, do-while; other jumps stay `goto`), callees
   declared from their call sites, then clean-ups that matter to cl's register allocator (dead copies dropped,
   single-use temporaries inlined, address-only locals sized to their frame room).

Globals and callees are named by address (`g_<va>`, `FUN_<va>`), with the binary's relocations deciding which
constants are addresses.

## What lifts

| construct | as |
|---|---|
| if, if / else, else-if chains | il2c's structuring of forward branches |
| while, for, do-while | cl rotates them into a guard and a bottom test: `if (c) do { .. } while (c);` |
| break, continue, early return, && / \|\| | `goto` where the structuring rules do not place them |
| dense switch (jump table, byte-indexed two-level tables) | `switch (x) { case N: goto L; .. }` |
| sparse switch | cl's compare tree, lifted as if-chains |
| `c ? x : 0`, boolean results | `setcc`; `sbb` masks folded into selects |
| calls | cdecl / stdcall / fastcall / thiscall, virtual calls (`il2c_vc` stand-in classes), function pointers, imports, recursion |
| structs and classes passed by value | `*(S*)(base + off)` for a dword copy; a class with its copy constructor `FUN_<ctor>` |
| integer arithmetic | signed division by constants (magic numbers), `rep movsd` as memcpy, `rep stos` as memset |
| x87 | arithmetic incl. integer operands (`fimul` ..), `fsqrt` / `fabs` / `fsin` / `fcos` / `fpatan`, compares through `fnstsw` + `test ah` (incl. the parity forms, NaN-exact) and `fcomi` / `lahf`, float returns (typed double), `_ftol` as `(int)x`, values merging at joins |
| `__int64` (register pairs) | `add/adc`, `sub/sbb`, constant `shld`/`shrd` shifts and `and`/`or`/`xor` on a pair, as one `__int64` operation; the pair's inputs are a call's edx:eax (through `mov` copies), a memory pair (also each half loaded separately), `cdq` / zero, constants, or another such value; its consumers a pair store, the compare chain `cmp hi; jl/jg; cmp lo; jb` as one compare, an `__int64` return in edx:eax, or the halves split back out (`lo = (int)t`, `hi = (int)(t >> 32)`) for other readers. A fold is kept only when every reader of what it drops is accounted for (`find_i64`). Not joined: two halves of unrelated origin (cl compiles `(hi << 32) \| lo` as real code) |
| SSE | scalar float / double; on Spore, `cvtss2si`, the floor / ceil sequences and the `maxss` / `minss` clamps as the vetted helpers of spore-decomp `match/include/sse_helpers.h` (`SseRoundToInt` ..), and on cl 15.00 `fistp qword` as `X87RoundToInt64` |
| TLS | `__declspec(thread)` globals |
| C++ EH (`__CxxFrameHandler3`) | static initialisers, constructors / destructors (incl. polymorphic), RAII locals, `new` with a class `operator delete` (incl. placement), `try { } catch (...) { ..; throw; }` |

C++ EH needs C++ linkage (an `extern "C"` function is nothrow under /EHsc, so cl drops the states).

## What does not lift yet

- more than one `try` block, typed catches, EH states changed inside a branch or set from a variable;
- jump tables of other shapes (an index bias, computed jumps);
- `mul` with a 64-bit result (`__int64` products, the `new T[n]` overflow check `mul; seto; neg; or`), `__int64`
  values whose halves arrive from unrelated places (a 64-bit local split across registers), 8-byte arguments pushed
  from a register pair, `rdtsc`;
- x87 rounding-mode changes (`fnstcw` / `fldcw`), `lahf` on integer flags, packed SSE;
- member functions called from a C unit, `this` reassigned.

## What a match is, and what it is not

A MATCH is byte-identical code, relocations aside, from source with placeholder names and types. Many more functions
lift and compile but differ (DIFF), mostly in register allocation, instruction scheduling or frame layout; the c2scope
tools (`c2trace.py regs`, `slots`, `schedwant.py`, `slotwant.py`) explain and search those differences. On functions
over a few KB, read the `--outline` or the `--partial` draft for structure (one base pointer, which copies, the block
order) rather than as code to keep.
