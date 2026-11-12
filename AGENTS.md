# AGENTS.md: using c2scope to match decompiled functions

For coding agents working on a byte-matching decompilation compiled with MSVC 13.10, 14.00 or 15.00. Read this
first; read `docs/c2/tracer.md` for every tool's details. Every tool prints its full usage with `--help`.

## Ground rules

- Run everything with `uv run` from this directory. Setup once: `uv sync && uv run python setup.py --vs2005 DIR`;
  for the other compilers also `uv run tools/c2build.py setup --c2 13.10` (or `--c2 15.00`).
- Pick the compiler with `--c2 13.10` (3077), `--c2 13.10.3052` or `--c2 15.00` on any tool, or `C2_BUILD=...`.
  The default is 14.00.50727.762. Using the wrong build gives plausible but wrong answers.
- Pass compiler flags literally after `--`: `-- /TP /MT /GS /EHsc /Gy`. In zsh a `$FLAGS` variable arrives as one
  argument and the compile silently uses the wrong flags.
- `--func` takes a decorated name, a substring of one, `Class::method` or `::free_function`.
- Traced 13.10 compiles go to a Windows compile server when one is configured (Mount & Blade's: it needs
  `~/.config/mb-compile-token` and `-server`); the tool says so once per run. `C2TRACE_REMOTE=0` keeps them local,
  for example when a brief says local only or the source must not leave the machine. Results are identical.
- Compiles are cached by content (source, includes, flags, compiler build). `CCACHE=0` bypasses the cache.
- The original's disassembly (`orig.s`) is one instruction per line: `VA mnemonic operands`, capstone style.
- Trust order: a byte comparison against the original, then a traced compile, then a model answer, then a doc
  rule marked [I]. Model answers are hypotheses until one compile confirms them.

## Which tool for which symptom

| Your draft differs from the original by | Start with | Then |
|---|---|---|
| Registers only (same instructions) | `c2trace.py regmap` (which variable wants which register) | `rasim want SNAP regmap.txt` for the source edits that fix them |
| Two values in swapped callee-saved registers | `c2trace.py regs --why A,B` (priority per block) | `rasim need SNAP 'A>B'` or `reg:A=esi` |
| eax/ecx/edx choices (scratch temporaries) | `c2trace.py scratch --why` | `rasim scratch SSNAP drop:N` / `ins:N` |
| eax/ecx/edx a step off in many later switch cases | `c2trace.py scratch --phase orig.s` (phase per case, first divergence) | its scan: the one inserted/removed pick that re-aligns most cases |
| x87 operand order, commutative operand order | `symkeys.py sites` | `symkeys.py levers` for natural edits that move a key; `levers --site L:A>B ...` for several sites at once |
| Instruction order (loads, stores, cmp placement) | `c2sched.py` (scheduler decisions) | `c2trace.py il --diff` between passes |
| Reloads after a loop in the wrong order | `rasim check SNAP --predict` (split-piece numbering) | regalloc.md 3.6.9: first-mention order, dead mentions |
| A function that calls others with addresses of its locals | `calleesum.py EXE --caller VA` | `calleesum.py EXE CALLEE_VA --proto '...'` generates the stub body |
| Anything, when you have no idea | `rwsweep.py` over the function (mechanical rewrites, each compiled and scored) | `tsweep.py` with `{{a<|>b}}` alternatives you write |
| Code inside an EH frame (C++ destructors, try) | `helpersweep.py` (reference bindings in inline helpers) | |
| A tail shared in one and duplicated in the other (switch cases, if/else arms) | `c2trace.py merges` (each jump pair: merged, or the first difference: call target, register, size) | il_fg.md §7.5: twins called by two symbols, scratch rotation, which copy survives |
| Stack offsets differ (`_x$`/`$T`/`tv` slots in other places, same code) | `c2trace.py slots SRC --func F --orig orig.s` (colours, visiting order, every colour refused and why; ours against the original's slots) | `--search` (single edits: uses, visiting position, a conflict) and `c2slots.py what SNAP uses:X=N` to try one; rules: docs/c2/slots.md |
| /GL + /LTCG code | `c2ltcg.py` (traces at link time) | docs/c2/ltcg.md when present |
| A model answer (`ref:V:B`, `extend:V:B`, `P:B:+1`, `add:frame@V:n`) you cannot spell in C++ | `realize.py edit SRC --func F EDIT` (each catalogued construct applied at the block's source lines, compiled, checked) | `realize.py --from-rasim auto SRC --func F --asm-vs orig.s` runs regmap, `rasim want` and the rewrites until one matches; catalogue: docs/c2/realize.md |

`SNAP` comes from `c2trace.py regs SRC --func F --export snap.json -- FLAGS`; `SSNAP` from `c2trace.py scratch ...
--export s.json`. `rasim` is `tools/rasim/target/release/rasim`.

## Workflow for one function

1. Get the instructions right first: block order, instruction choice and frame size. The rules are in
   `docs/c2/frontend_il.md` (spellings that are identical after the front end, so not worth trying),
   `opt.md`, `isel.md`, `layout.md`, `frame.md`.
2. Check the translation unit, not just the function: callees with bodies in the same unit change the caller's
   allocation (`docs/c2/callee.md`), and so do declarations such as `__declspec(noalias)`.
3. Then registers: `regmap` -> `rasim want` -> apply the cheapest edit -> one compile to confirm.
4. Then scratch picks, operand order and instruction order, with the tools above.
5. If the model names an edit nobody can write in C++, run `rwsweep` with all families before concluding the
   original used something unusual. Record what was ruled out; it is the most useful thing for the next agent.

## Rewrites that are known to matter with identical final code

From `docs/c2/frontend.md`, `immcand.md`, `symkeys.md` and `x87.md`; each is an `rwsweep` family where noted.

- A same-unit callee that only reads through a pointer parameter (`calleesum`).
- `delete p; p = 0;` inside or outside `if (p)` (`--families guard`).
- Redundant parentheses on float expressions (`--families paren`).
- A class temporary in a folded `&&`/`||` arm leaves a zeroing store to a hidden flag (frontend_il.md addendum).
- A narrow constant stored through the other signedness stops sharing a constant candidate (`--families const`).
- A dead mention (`(void)y;`) or first-mention order changes record numbers and reload order.
- An unused local *with* an initialiser costs one symbol key; one without costs none.
- A copy made before a variable is next modified (`int k = n;` then the loop counts `k`; an inline helper that
  walks its by-value parameter) is two more references to `n` and one more candidate in the block of the copy
  (`realize.py`, construct `copy`; docs/c2/realize.md).
- A statement written at the head of both arms of an if/else instead of before it counts in the arms (it is
  merged back after allocation; construct `headdup`).

## Pitfalls

- Do not compare against a listing from another compiler build or other flags.
- `regdiff` counts aligned differences including registers; a shape-only difference (registers masked) of 0
  means the code is right and only allocation differs.
- Doc rules carry [C] (seen in code, trace or compile) or [I] (inferred). Re-check an [I] rule with a probe
  before building on it.
- Model coverage gaps: x87 on 13.10 goes through a separate pass, FPColor (docs/c2/x87.md section 11): `regs
  --all-classes --why` and `rasim check / need 'A>B'` cover its priorities and order, but whether a web stays on
  the x87 stack (FPCheckColorability) is read from the trace, not predicted; `symkeys` is not
  ported to 13.10.3052.
