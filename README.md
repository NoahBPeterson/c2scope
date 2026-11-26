# c2scope: see inside the MSVC back end while it compiles

Tools that look inside Microsoft's C++ back end (`c2.dll`) while it compiles your source, so that matching a
decompiled function becomes prediction instead of guessing. Five compilers are supported, each as one exact build
of `c2.dll` (setup checks its SHA-256):

| product | also called | `cl.exe` | `c2.dll` | option | used by |
|---|---|---|---|---|---|
| Visual C++ 6.0 SP5/SP6 **with Processor Pack 5** | VC6 + PP | 12.00.8804 | 13.00.9044 | `--c2 9044` | VC6-era targets (Vampire: Bloodlines) |
| Visual Studio .NET 2003 | VC7.1, MSVC 2003 | 13.10.3077 / 13.10.3052 | the same | `--c2 13.10` / `--c2 13.10.3052` | Mount & Blade |
| Visual Studio 2005 SP1 | VC8, MSVC 2005 | 14.00.50727.762 | the same | default | Fallout 3 |
| Visual Studio 2008 SP1 | VC9, MSVC 2008 | 15.00.30729.1 | the same | `--c2 15.00` | Spore |
| Visual Studio 2012 Update 4, x86_amd64 (64-bit code) | VC11, MSVC 2012 | 17.00.61030 | the same | `--c2 17.00` | x64 targets |

**Looking for "VC7"?** VC7.1 (Visual Studio .NET 2003) is supported: use `--c2 13.10`. Not supported, because their
`c2.dll` is a different build (setup refuses it):
- **VC7.0, Visual Studio .NET 2002** (`cl` 13.00.9466). Its version also starts with 13.00, like the VC6 Processor
  Pack back end above, but it is a different compiler.
- **Plain VC6 without the Processor Pack** (`c2.dll` 12.00.8168 and the later 12.00 builds of the service packs).
  The Processor Pack replaces VC6's `c2.dll` with 13.00.9044.
- Other service packs or updates of the products in the table, e.g. VS2005 RTM, VS2008 RTM or VS .NET 2003 SP1
  (13.10.6030).

Which compiler built a binary: its Rich header (when the linker wrote one) lists the build of every object, e.g.
build 9044 for VC6 + Processor Pack 5; otherwise compare its code with test compiles.

They trace c2's own decisions (which values get registers and in what order, which register each temporary gets,
the IL at every pass, the scheduler's choices), model the register allocator well enough to answer "what source
edit gives these variables these registers?" in milliseconds, and search for source rewrites that fix an
allocation without changing any code.

The specs in `docs/c2/` describe what the compiler does (front end, optimiser, instruction selection, register
allocation, scheduler, inliner, block layout, x87, stack frame, IL) with each rule marked confirmed or inferred
and backed by probe compiles. VS2005 is the reference; `docs/c2/port-1300.md` (VC6 + Processor Pack), `port-1310.md` (VS .NET 2003),
`port-1500.md` (VS2008) and `port-1700.md` (VS2012 x64) cover the others.

No Microsoft code or binaries are included. You need your own copy of each toolchain. Setup patches a private
copy of `c2.dll` in this directory (an entry-point hook that loads the tracer DLL); your toolchain is never
modified.

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

For the other compilers, after the setup above: `uv run tools/c2build.py setup --c2 13.10` (or `--c2 15.00`),
then add the same `--c2` option to any tool command. For 17.00, stage an unpacked VS2012 first with
`uv run tools/c2stage_vs2012.py ROOT`; the x64 back end is a different allocator (NColor), scheduler and frame
packer, and docs/c2/port-1700.md lists which tools run there. For 9044, docs/c2/port-1300.md says where its headers come from
(VC6 SP6's Include/Lib) and which tools run. `tools/c2build.py` explains where it looks for each toolchain.

Coding agents: read `AGENTS.md` first (which tool for which symptom, and the rules that avoid wrong answers).

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
| `tools/c2ncol.py` | the x64 allocator NColor traced: candidates, priority order, regions, registers (`--export` for the model) | docs/c2/ncolor.md |
| `tools/ncolsim.py` | NColor's model, offline: `replay`/`sim`, `whatif` register edits (prio/bucket/app/split), IL-only inputs | docs/c2/ncolor.md |
| `tools/ncolspill.py` | split/spill report of an export + frame load/store side-by-side for two listings | docs/c2/ncolor.md §13 |
| `tools/callform.py` | call census by dispatch form, two listings: missing block vs allocator in one command | docs/c2/callform.md |
| `tools/tsweep.py` | variant sweep scored by trace numbers and aligned register diff | tracer.md |
| `tools/helpersweep.py` | finds inline-helper rewrites that fix a colouring order without code change | tracer.md, frontend.md |
| `tools/rwsweep.py` | general search over mechanical source rewrites (identity helpers, aliases, dead uses, swaps): which ones move registers without changing code | tracer.md |
| `tools/c2sched.py` | the instruction scheduler's decisions and a model check | docs/c2/sched.md |
| `tools/ccache.py` | content cache behind every traced compile (`stats`, `clear`; `CCACHE=0` bypasses it) | tracer.md |
| `tools/slotwant.py` | frame-colouring inverse: a wanted slot layout (`with:A,B`, `frame=N`, ...) -> source edits (scope, share, array size), each verified by a traced compile | docs/c2/slotwant.md |
| `tools/il2c_lift.py lift` | lift one function of a retail x86 binary to C++ that cl should compile back to its bytes (`--partial` drafts, `--outline` per block) | docs/c2/lift2il.md |
| `tools/lift2il64.py` | the same for x64 (MSVC 2012 / cl 17.00): `il`, `unit`, `check` (compile with `C2_CL64` and compare), `bench`; `tidy64.py` / `tidycheck64.py` make the result readable | docs/c2/lift2il64.md |
| `tools/x64check.py` | compile one C++ function with an x64 cl and compare it with the executable, byte by byte and relocation by relocation | docs/c2/lift2il64.md |

Worked example: `docs/c2/frontend.md`, "Applied to 0x008e7d40", is the full story of a Fallout 3 function that
went from 98 differing instructions to byte-exact, and `helpersweep` finds that match automatically in 11 compiles.

## Lift a function from a binary

`lift2il` reads a function's machine code from a 32-bit PE built by MSVC 7.1 / 8 / 9 (c2 13.10, 14.00, 15.00)
and writes C++ for it; compile that at the binary's flags and it should give the same bytes back.

```
uv run python tools/il2c_lift.py lift game.exe 4d5940 --size 54 --c2 13.10.3052 > FUN_004d5940.cpp
uv run python tools/il2c_lift.py lift game.exe 448eb0 --size 11648 --c2 13.10.3052 --partial   # a draft of a big one
uv run python tools/il2c_lift.py lift game.exe 531800 --size 3277 --c2 13.10.3052 --outline    # its shape per block
```

The output is a matching draft, not readable source: names are addresses (`FUN_<va>`, `g_<va>`), types are what the
instructions show (int, float, double, pointers as char*), and the control flow is what cl's code implies. Hit rate
on three games' functions (lbench, a fixed sample of 300-400 functions per game): 30% of
Fallout 3's, 22% of Mount & Blade's and 23% of Spore's recompile byte-identical (the largest so far: 5,237 bytes,
1,778 instructions with 312 branches), most of them under 256 bytes; many
more lift and compile but differ in register allocation, scheduling or frame layout, which the tools above explain.
What it does and does not lift: docs/c2/lift2il.md.

For x64, `lift2il64.py` does the same for code built by Visual Studio 2012 (cl 17.00). It writes the C++, then
compiles it and checks it against the executable in one step:

```
export C2_CL64="wine /path/to/VC/bin/x86_amd64/cl.exe"
uv run python tools/lift2il64.py check 1401234560 --binary game.exe --flags /O2,/GS-
```

On 1,000 functions of a retail x64 game, 51% recompile byte-identical: 82% of the ones up to 32 bytes and 14% of
those between 129 and 256 bytes. docs/c2/lift2il64.md has the details.

## License

MIT (see LICENSE). The documentation describes the behaviour of Microsoft's compiler as observed through the
tools; names and addresses of c2 functions come from Microsoft's public symbols.
