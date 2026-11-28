# lift2il64: lift2il for x64

`tools/lift2il64.py` is lift2il's x64 front end (lift2il.py decodes 32-bit x86 only). It takes a function's
machine code from a PE32+ built by MSVC, lifts it to the lift2il IL dialect, writes C++ through il2c's Emitter, and
`check` compiles that C++ and compares it with the original (`tools/x64check.py`). It was written against cl 17.00
(Visual Studio 2012) x64 code, and other versions are untested.

    export C2_CL64="wine /path/to/VC/bin/x86_amd64/cl.exe"      # the compiler command (on Windows, the cl.exe path)
    python3 tools/lift2il64.py il    1401234560 --binary game.exe           # the lifted IL
    python3 tools/lift2il64.py unit  1401234560 --binary game.exe           # C++
    python3 tools/lift2il64.py check 1401234560 --binary game.exe --flags /O2,/GS-,/MD,/GR
    python3 tools/lift2il64.py bench --binary game.exe --sample 1000 --seed 3 --min-size 1 --max-size 100000 --json out.json

Function sizes come from the executable's `.pdata`. A leaf function has no unwind data, so give it `--size N`, or
pass `--index FILE`, a JSON file `{"functions": [{"address": "0x...", "size": N, "flags": [[...], ...]}]}`. The
optional `flags` field gives the flag sets to try for that function, for example `/O1` for the size-optimised ones
or `/EHsc` for the ones with C++ EH. `--flags` gives one set, comma-separated; to try several, separate the sets
with `;`. The default is `/O2,/GS-`. A function whose float compares branch on equality without a parity check
(`ucomiss` then `je` / `jne`, no `jp`) was built with `/fp:fast`: each set is tried with `/fp:fast` first; one with a
parity check never gets it; when the bytes do not say, `LIFT64_FPFAST=1` tries `/fp:fast` after the plain sets (for
code built with it throughout). `--extra-set A,B,..` (repeatable) adds whole flag sets after those, `--only-extra`
tries them alone. A C++ exception frame is read from the executable's unwind data (a `__CxxFrameHandler3` FuncInfo,
`ehinfo64.py`); give such functions `/EHsc` in the index. The C++ names functions `FUN_<va>` and globals `g_<va>`, and x64check resolves
those names to their addresses. Register webs are named `v1001`, `v1002`, ... (variables; `t\d+` is the name of
cl's own temporaries to `ncolsim.named()` and `realize --from-ncol`).

What the lifter writes beyond plain statements:
- packed SSE through the `_mm_*` intrinsics, scalar ops on vectors through the `_ss` ones (`sqrtss` from another
  register as `_mm_move_ss(d, _mm_sqrt_ss(s))`), division by constants, `sbb` / `bt`, variadic calls;
- cl's null-checked pointer adjustment (`lea r, [x+D]; test x, x; jne; xor r, r`, or the `je` form) as
  `nullcast_downD(x)` / `nullcast_upD(x)`: inline helpers that `static_cast` between two classes D bytes apart;
- `rdtsc; shl rdx, 32; or rax, rdx` as `__rdtsc()`, `rol` / `ror` as `_rotl*` / `_rotr*`;
- one signature per callee: when two call sites of one callee disagree (a stale register read as an argument), the
  function is lifted again with an integer register wherever they disagree;
- C++ EH frames (the `-2` UnwindHelp store): the body inside `try { } catch (...)`, rethrown or swallowed;
- `MARKS = True` ends each statement with `//@<address>` (lsearch64), `RETVAR` (off) gives the return value its own
  variable.

The register webs are numbered in address order of their first definition, so the text does not depend on
`PYTHONHASHSEED`.

## Metrics and fixed benches

`tools/x64metrics.py` measures how far a no-match is from the original, from x64check's two listings: `edits`
(Levenshtein distance over the instructions, in-function jumps position-free), `edits_noreg` (the same once every
register is `R` and every `[rsp/rbp+off]` is `[S]`), `mapcost` (register choice and stack layout as a mapping over the
shape-aligned instructions: the pairs that break the majority mapping plus the mappings that are not the identity,
so one different allocation costs about 1) and `regpairs` (our register -> the original's). `lift2il64 check` prints
them and `bench` rows carry `edits` / `shape` / `mapcost`. `edits_noreg` 0 with `mapcost` above 0 means the code is
right and only registers or slots differ.

`tools/lbench64.py make NAME --min 200 --max 1200 --count 300` picks a fixed set of functions by size (from the
index, or `--from LIST`, a JSON list of `[address, size]`), `run NAME --tag T` lifts and checks them (6 jobs) and
writes `work/lbench64/NAME.T.json`, `compare NAME A B` lists the matches won and lost between two runs and how many
shapes got better or worse.

## regfix64: the code is right, the registers are not

`tools/regfix64.py VA --binary EXE` takes the lifter's best variant (or `--src`), and:
1. compiles and compares it: the `regpairs` mapping says which original register each of ours stands for;
2. traces our compile with `c2ncol.py --c2 17.00`, which gives each variable (`v1xxx`) its register;
3. asks `realize.py --from-ncol --want-regs` for the original's register for each callee-saved variable;
4. compares every variant realize keeps and lists the matches (`WORK/<va>/match_N.cpp`).

When no register needs to move, the difference is the stack frame. regfix64 then prints `frameinfo64`'s reading of
each stack address passed to a call. A slot is a return slot when the callee returns that pointer (the hidden return
value of a class returned by value: `T x = f();`, which cl's frame packer places after the named locals); otherwise
it is an out parameter. regfix64 also prints `c2trace.py slots --image --search` (the frame packer against the
original).

## Compiling on a Windows compile machine (`--laptop`)

`lift2il64.py bench`, `lbench64.py run` and `regfix64.py` take `--laptop`: the lifting stays local, the compiles and
compares go to a Windows machine through `tools/laptop64.py` (one upload per chunk of 120 functions, its own worker
pool there; lint off and /W0 as the local bench). The client reads its connection settings from
`~/.config/mb-worker`; nothing about the machine is in the tools. laptop64.py is optional: without it, `--laptop`
stops with a message and every tool runs on the local x64check. Rows carry the same metrics as the local path
(edits, shape, mapcost; regfix64 also needs `regpairs`, which laptop64 computes the same way as x64metrics).

## C++ EH tables

`tools/ehinfo64.py VA` decodes a function's `__CxxFrameHandler3` FuncInfo: states and their cleanup funclets (each
call, and where its `rcx` points in the parent's frame: a local, a pointer in a slot, a by-value argument's home, a
member of `this`), try blocks and catch handlers, and ip-to-state. `tools/ehobj64.py VA` uses it on constructors whose
every state destroys a member of `this`: it rebuilds the members as classes whose inline constructor forwards to the
original's call and whose destructor binds to the original's through its `FUN_` name. The constructor runs them as
member initializers, so cl writes the original's state machine.

`tools/lsearch64.py` (mutation search: merge / fold / type / order edits scored by x64metrics) is a reference only and is
not part of the release.

## x64check

`tools/x64check.py BINARY VA SIZE SOURCE.cpp [--flags ...] [--warn-clean]` compiles with `C2_CL64` and compares the
target function with the original:
- The bytes outside relocation fields are equal, and the compiled function is no longer than the original
  (trailing int3 and nop padding is fine).
- Every relocation resolves to the address the original uses:
  - a `FUN_/g_/DAT_/PTR_/LAB_<hex>` name is that address;
  - `__ImageBase` is the image base, and `_tls_index` is the slot that the image's TLS directory names;
  - a label inside the function is the same offset;
  - a string or float literal counts as long as the original reads the same bytes there;
  - a thread-local variable compares by its offset in `.tls` (SECREL).

If anything differs, x64check prints an aligned listing of the two functions.

## tidy64

`tools/tidy64.py` rewrites lift2il64's C++ (one statement per line, every operand cast) into plain C++:
- it drops implicit casts;
- it propagates copies and folds single-use temporaries;
- it names variables by what they do;
- it writes frame objects as named structs;
- it declares locals where they are first set.

`tools/tidycheck64.py one lifted.cpp PACKDIR` keeps the most rewritten candidate that still matches and builds
without warnings at /W3. PACKDIR is the per-function folder that `check` and `bench` write under `--work`.

## Result (2026-11-25, a retail x64 game built with cl 17.00, 1,000 functions sampled uniformly from its index)

| size | match | close (<=10% or <=3 insns) | near (<=25%) |
|---|---|---|---|
| <=32   | 82% | 7% | 0% |
| <=64   | 77% | 3% | 1% |
| <=128  | 49% | 3% | 15% |
| <=256  | 14% | 5% | 14% |
| <=512  | 2%  | 1% | 10% |
| >512   | 0%  | 0% | 6% |
| all    | 51% | 4% | 7% |

2% of the functions are compiler-made (adjustor thunks, unwind and catch funclets) and have no source form. A match
uses offsets, stand-in types and `FUN_`/`g_` names; the real types and names are still to come.

## Return slots (`frameinfo64.py`, opts `retslot`)

`frameinfo64.py VA` reads, from the binary alone, every stack address passed to a call: a **return slot** when the
callee returns that same register (`mov rax, rdx` before its ret: the hidden return pointer of a class returned by
value), else an **out param**. regfix64's frame report prints it. `unit(..., {"retslot": 1})` rewrites a named local
filled through a return slot as `RetVal8 x = ((RvObj_FUN_X*)obj)->call();` (RetVal8 has a declared copy
constructor, so it comes back through the hidden pointer) and its reads as `x.v`. Measured on 150 open functions of
a retail x64 game that pass a return slot: no match either way, shape better 2 / worse 28 / same 113, 7 compile
errors (a later use outside the declaring block; `FUN_X_rv` clashing with the function itself). It is therefore
opt-in, not a default variant. The frame packer visits the object by its uses exactly like the plain local, so a
home-slot order like 0x1406e7170's (the local the original places after a callee-saved save) is not explained by the
return slot alone. It is two other things (slots.md 6.3): the lift copies a parameter into a variable at entry
(`v1009 = a0`) and passes the copy, which leaves the parameter's home free for a local where the original's
parameter (passed to the calls itself) refuses every local; and a local the original has smaller (4 bytes) is visited
before an 8-byte one. 0x1406e7170 reproduces the original's whole frame with `a0` in place of `v1009` and `int` for
the two out-parameter locals.

## Parameter copies, out-parameter sizes, `or r, -1`

- `or r, -1` lifts as the constant -1 (cl's short load of -1; the register's old value is not read). Before, the
  lift read the register's previous value (`v = w | -1`), a false dependency that changed allocation.
- opts `pcopy` (a variant whenever a parameter is copied at entry): `v1009 = a0` with no other def of either and no
  address taken becomes a0 itself. cl 17.00's packer leaves a parameter's home to the parameter when its only uses
  are call arguments (slots.md 6.3); the copy frees the home for a local. 300 functions: shape better 14 / worse 16.
- opts `outsize`: a local whose address is only passed to calls is typed by the callee's first store through that
  pointer (1 / 2 / 4 bytes; following register copies of it). The packer visits frame locals by size first.
0x1406e7170 with pcopy and its two out locals declared int: frame 4 of 4 at the original's offsets, 4 edits left (a
callee-saved register for the second `lock xadd`'s -1: ebp in the original, eax here, also 1 byte of `test`).

## Interlocked decrement / increment

`mov|or r, -1; lock xadd [m], r; dec r` (or `add r, -1`; r may be a copy of a hoisted -1) lifts as
`r = _InterlockedDecrement(&m)`, and the `1 / xadd / inc` form as `_InterlockedIncrement` (opts `noxaddfuse` turns it
off). That's cl's own expansion of the intrinsic. With the xadd's result as a variable of its own, the chain splits
from the hoisted -1 and takes another register (port-1700.md 13). 0x1406e7170 matched with it, plus the pcopy and
`ret void` variants and its two out locals as int.

## Zero pairs, byte webs, submit-clean output

- opts `zeropair` (a variant when the lift has a literal 8-byte zero store): `*(__int64*)(p+N) = 0` as two int zero
  stores. While a register holds a known 0, cl stores 0 through it (`mov [m], rbp`); `mov qword ptr [m], 0` is two
  adjacent int zero stores combined late (immcand.md 7). 0x140cc1870 matched with it.
- opts `bytewebs` (a variant when a web qualifies; Lifter.byte_webs): a web whose every def writes a byte (8-bit
  destination, movzx from a byte) and whose every read is an 8-bit register read is declared unsigned char. Wider,
  its zero def becomes `mov ecx, edi` instead of `xor cl, cl`. 0x141c40fd0 matched with it.
- Intrinsic casts are `(long*)`, not `(long volatile*)` (same code; volatile in a function body is refused at
  submit), and locals that are declared but never referenced are dropped (C4101 at /W3 is refused as Q11).

## Not yet handled
- Re-rolling loops. cl rotated and duplicated the loop tests, so the goto-shaped lift gets re-optimised
  differently. This is the main cause of misses above 128 bytes.
- x64 C++ EH beyond try / catch (...) and ehobj64's constructors: other objects with destructors.
- Parity float branches and one-operand mul / imul.
- Hidden return slots. A class returned by value is lifted as a named local and a pointer, not as `T x = f();`, so
  cl can place its slot elsewhere (frameinfo64 finds these slots).
- Register choice beyond what regfix64 can ask realize for.
