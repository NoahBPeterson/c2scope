# c2 glass box: tools for matching code compiled by VS2005 SP1

Tools that look inside the Visual C++ 2005 SP1 back end (`c2.dll` 14.00.50727.762, the compiler Fallout 3 was built
with) while it compiles your source, so that matching a decompiled function becomes prediction instead of guessing.
They trace c2's own decisions (which values get registers and in what order, which register each temporary gets,
the IL at every pass), model the register allocator well enough to answer "what would make A get a register
before B?" in milliseconds, and search for source rewrites that fix an allocation without changing any code.

The specs in `docs/c2/` describe what the compiler does (register allocation, scheduler, inliner, block layout, x87,
stack frame, IL) with each rule marked confirmed or inferred and backed by probe compiles.

No Microsoft code or binaries are included. You need your own copy of the VS2005 SP1 toolchain. Setup patches a
private copy of `c2.dll` in this directory (an entry-point hook that loads the tracer DLL); your toolchain is
never modified.

## Setup (once)

Requirements: macOS or Linux with [wine](https://www.winehq.org/) (32-bit support), [uv](https://docs.astral.sh/uv/),
[Rust](https://rustup.rs) (cargo) and `llvm-pdbutil` (part of LLVM: `brew install llvm`, or your distribution's
llvm package). Then, in this directory:

```sh
uv sync
uv run python setup.py --vs2005 /path/to/vs2005
```

`/path/to/vs2005` is the directory that contains `VC/` and `SDK/` (for f3decomp: the toolchain its setup unpacks).
Setup checks every requirement, verifies that `c2.dll` is the exact build, fetches c2's public symbols from the
Microsoft symbol server (or pass `--pdb c2.pdb`), builds the function index, creates a wine prefix, builds the
traced toolchain and rasim, and finishes with a regression test (9 checks). If a step fails it says why and how
to fix it; run the same command again and it continues where it stopped.

## Quick start

```sh
# which values became register candidates, their priorities, the order and the registers they got
uv run tools/c2trace.py regs t.cpp --func MyFunc -- /TP /MT /GS /EHsc /Gy
# why A outranks B, block by block; the IL of blocks 10-12
uv run tools/c2trace.py regs t.cpp --func MyFunc --why a,b --blocks 10-12 -- /TP /MT /GS /EHsc /Gy
# the scratch-register picks (eax/ecx/edx rotation) mapped to listing offsets
uv run tools/c2trace.py scratch t.cpp --func MyFunc -- /TP /MT /GS /EHsc /Gy
# the IL at chosen passes
uv run tools/c2trace.py il t.cpp --func MyFunc --at @GrgIdentifyCandidates@4,@Color@4 -- /TP /MT /GS /EHsc /Gy
# which register each of your variables should have, read off the original's code
uv run tools/c2trace.py regmap t.cpp --func MyFunc --asm-vs orig.s --asm-func '?MyFunc@@YAXXZ' -- /TP /MT /GS /EHsc /Gy
# what-if: which single edits make A get a register before B
uv run tools/c2trace.py regs t.cpp --func MyFunc --export snap.json -- /TP /MT /GS /EHsc /Gy
tools/rasim/target/release/rasim need snap.json --pass 2 'a>b'
# rank source variants ({{x<|>y}} alternatives) by trace numbers and by aligned diff against the original
uv run tools/tsweep.py tmpl.cpp --func MyFunc --metric "regdiff" --show "prio('a',2)" \
    --asm-vs orig.s --asm-func '?MyFunc@@YAXXZ' -- /TP /MT /GS /EHsc /Gy
# in a function with a C++ EH frame: inline-helper rewrites that fix "a before b" without changing code
uv run tools/helpersweep.py tu.cpp --func '?MyFunc@@YAXXZ' --want 'a>b' --asm-vs orig.s -- /TP /MT /GS /EHsc /Gy
```

`orig.s` is the original function's disassembly, one instruction per line: `VA mnemonic operands` (capstone style).
Pass compiler flags literally after `--`; in zsh a `$FLAGS` variable holding several flags arrives as one argument.

## The tools

| tool | what it does | doc |
|---|---|---|
| `tools/msvc.py` | runs cl/link/lib/dumpbin under this directory's own wine prefix | |
| `tools/c2trace.py` | patched-c2 tracer: hook any c2 function by PDB name or address, log registers/stack/IL | docs/c2/tracer.md |
| `c2trace.py regs` | register allocation report: candidates, priorities with per-block terms, colouring order, registers | docs/c2/regalloc.md |
| `c2trace.py scratch` | scratch-register picks with the rotation cursor, mapped to listing offsets | docs/c2/regalloc.md §6 |
| `c2trace.py il` | IL dump at any pass | docs/c2/il*.md |
| `c2trace.py regmap` | which register each variable should have, from the original's code (inverse constraints) | tracer.md |
| `tools/rasim` | Rust model of c2's priority formula and colouring order: `check`, `order`, `blocks`, `need` | tracer.md |
| `tools/tsweep.py` | variant sweep scored by trace numbers and aligned register diff | tracer.md |
| `tools/helpersweep.py` | finds inline-helper rewrites that fix a colouring order without code change | tracer.md, frontend.md |
| `tools/c2sched.py` | the instruction scheduler's decisions and a model check | docs/c2/sched.md |
| `tools/ccache.py` | content cache behind every traced compile (`stats`, `clear`; `CCACHE=0` bypasses it) | tracer.md |

Worked example: `docs/c2/frontend.md`, "Applied to 0x008e7d40", is the full story of a Fallout 3 function that
went from 98 differing instructions to byte-exact, and `helpersweep` finds that match automatically in 11 compiles.

## License

MIT (see LICENSE). The documentation describes the behaviour of Microsoft's compiler as observed through the
tools; names and addresses of c2 functions come from Microsoft's public symbols.
