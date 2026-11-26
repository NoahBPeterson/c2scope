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
with `;`. The default is `/O2,/GS-`. The C++ names functions `FUN_<va>` and globals `g_<va>`, and x64check resolves
those names to their addresses.

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

## Not yet handled
- Re-rolling loops. cl rotated and duplicated the loop tests, so the goto-shaped lift gets re-optimised
  differently. This is the main cause of misses above 128 bytes.
- x64 C++ EH. This needs objects with destructors (the -2 unwind-help store).
- Packed SSE, parity float branches, sbb, and one-operand mul / imul.
- Differences in register choice.
