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
- Traced compiles can run on a Windows compile server: `c2trace.py` sends them to the clients listed in
  `~/.config/c2trace-remote` (see the comment above `REMOTE_CLIENTS` in tools/c2trace.py) and says so once per run.
  `C2TRACE_REMOTE=0` keeps them local, for example when a brief says local only or the source must not leave the
  machine. Without a configured client everything compiles locally.
- A candidate checked inside a bigger unit (hoisted headers, unit headers, a pragma): pass `--unit SPEC.json` (or
  `--f3pack PACKDIR` for an f3decomp pack) to any tool; SRC is the candidate, the tool traces the composed unit and
  reports candidate line numbers. docs/c2/tracer.md "Composed units".
- A traced compile logs only the compile of the tool's `--func` (symkeys excepted), so a large unit traces about as
  fast as a small one. `C2TRACE_FUNC=PAT` picks another function; `C2TRACE_FUNC=` (empty) logs every function.
- Compiles are cached by content (source, includes, flags, compiler build). `CCACHE=0` bypasses the cache.
- The original's disassembly (`orig.s`) is one instruction per line: `VA mnemonic operands`, capstone style.
- Trust order: a byte comparison against the original, then a traced compile, then a model answer, then a doc
  rule marked [I]. Model answers are hypotheses until one compile confirms them.

## Which tool for which symptom

| Your draft differs from the original by | Start with | Then |
|---|---|---|
| A different instruction count (extra or missing loads, compares, moves, a loop unrolled or not) | `icount.py SRC --func F --asm-vs orig.s` (each extra/missing instruction: its IL tuple, the pass that made, kept or removed it, a verdict class) | the lever printed for its verdict class (docs/c2/icount.md table); `--vs-func G` compares against another function of the same compile |
| Registers only (same instructions) | `c2trace.py regmap` (which variable wants which register) | `rasim want SNAP regmap.txt` for the source edits that fix them |
| Two values in swapped callee-saved registers | `c2trace.py regs --why A,B` (priority per block) | `rasim need SNAP 'A>B'` or `reg:A=esi` |
| eax/ecx/edx choices (scratch temporaries) | `c2trace.py scratch --why` | `rasim scratch SSNAP drop:N` / `ins:N`; several `want:` at once: the joint edit set is printed first |
| A variable missing from the `regs` table | its "not in the table" line (volatile, address-taken, propagated away, spilled, left without a register) | the phase named there |
| eax/ecx/edx a step off in many later switch cases | `c2trace.py scratch --phase orig.s` (phase per case, first divergence) | its scan: the one inserted/removed pick that re-aligns most cases |
| x87 operand order, commutative operand order | `symkeys.py sites` | `symkeys.py levers` for natural edits that move a key; `levers --site L:A>B ...` for several sites at once |
| A load reused in a register where the original reloads it (or the reverse), `op [m], y` against load/op/store | `loadcse.py` (each read: reused or reloaded and the rule; each store: RMW formed or why not) | docs/c2/loadcse.md §4 constructs; `rwsweep --families unalias` |
| Instruction order (loads, stores, cmp placement) | `c2sched.py` (scheduler decisions) | `c2trace.py il --diff` between passes |
| Instruction order inside one scheduling region (a load issued after a store, a reload first) | `schedwant.py SRC --func F --asm-vs orig.s` (the original's order per region, replayed; the DAG changes that give it: `unalias:X`, `alias:X`, `cut:X>Y`, `ns:X+1`) | `unalias` of a copy's loads: copy from the parameters, not an address-taken local (Spore 00598db0); `alias` of a store: a non-literal index or a member's inlined constructor pins earlier stores (sched.md rule 4a) |
| Operand order that `symkeys levers` cannot move (SU or count decides) | `sulever.py SRC --func F --site 'LINE:A>B'` (which weight component decides, what each operand needs, regrouping) | `--spell 'TEXT' --asm-vs orig.s` compiles the catalogue spellings (`(*(unsigned*)&lo & 0xff)`: SU1, same code; Spore 0068c6d0) |
| An aggregate local whose fields sit elsewhere (displacements of one local off, `slots --orig` votes split) | `framefields.py SRC --func F --orig orig.s` (per element: our field offsets against the original's, extent, order-only differences) | a key in front: the original passes `pair<key, value>` (Spore 00e2c840) |
| Reloads after a loop in the wrong order | `rasim check SNAP --predict` (split-piece numbering) | regalloc.md 3.6.9: first-mention order, dead mentions |
| A function that calls others with addresses of its locals | `calleesum.py EXE --caller VA` | `calleesum.py EXE CALLEE_VA --proto '...'` generates the stub body |
| Anything, when you have no idea | `rwsweep.py` over the function (mechanical rewrites, each compiled and scored) | `tsweep.py` with `{{a<|>b}}` alternatives you write |
| Code inside an EH frame (C++ destructors, try) | `helpersweep.py` (reference bindings in inline helpers) | |
| A tail shared in one and duplicated in the other (switch cases, if/else arms) | `c2trace.py merges` (each jump pair: merged, or the first difference: call target, register, size) | il_fg.md §7.5: twins called by two symbols, scratch rotation, which copy survives |
| Stack offsets differ (`_x$`/`$T`/`tv` slots in other places, same code) | `c2trace.py slots SRC --func F --orig orig.s` (colours, visiting order, every colour refused and why; ours against the original's slots) | `--search` (single edits: uses, visiting position, a conflict) and `c2slots.py what SNAP uses:X=N` to try one; rules: docs/c2/slots.md |
| /GL + /LTCG code | `c2ltcg.py` (links an LTCG probe and gives the other tools its listing: `c2ltcg.py regmap`, `c2ltcg.py tsweep`) | `ltcgstub.py callees EXE VA` (each callee's kill set, read from the original), `ltcgstub.py stubs` (/GL stubs that reproduce them), `ltcgstub.py cmp` (the target linked with the stubs, against the original); tracer.md "LTCG builds" |
| A model answer (`ref:V:B`, `extend:V:B`, `P:B:+1`, `add:frame@V:n`) you cannot spell in C++ | `realize.py edit SRC --func F EDIT` (each catalogued construct applied at the block's source lines, compiled, checked) | `realize.py --from-rasim auto SRC --func F --asm-vs orig.s` runs regmap, `rasim want` and the rewrites until one matches; catalogue: docs/c2/realize.md |
| A model answer realize has no construct for (`drop:N`, `uses:X=N`, `conf-:A,B`, `pref:V:REG`, `before:A:B`, `sched:A>B`, a key count) | `effectmine.py mine SRC --func F --want EFFECT --asm-vs orig.s` (generic mutations of the function and its unit, each compiled traced, every model's effect recorded in a database) | `effectmine.py table`: per mutation class, how often it produced each effect elsewhere; docs/c2/effectmine.md |

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
  merged back after allocation; construct `headdup`); the same at the tail (`taildup`), also making an `else`.
- `if ((int)V & 1) S; else S;` around an existing statement S: one more reference to V there and none in the
  code, also for `this` and read-only parameters (merged after allocation; 13.10 keeps the test when it reads
  memory). Construct `eqif`; conditions in docs/c2/realize.md §0 rule 8. It is not what the original had, but it
  gives the original's code (Mount & Blade `CharSheet::init_screen`).

## Pitfalls

- Do not compare against a listing from another compiler build or other flags.
- `bdiff` (the default score of tsweep, rwsweep, helpersweep and realize) counts aligned instructions that differ
  as the bytes encode them: registers, displacements, immediates, operand order and size; relocations, call/jump
  targets and padding are masked. `regdiff` is the same count over mnemonics and registers only: it cannot see a
  wrong displacement or immediate (an x87 sum in another order scores 0). A shape-only difference (registers
  masked) of 0 means the code is right and only allocation differs.
- Doc rules carry [C] (seen in code, trace or compile) or [I] (inferred). Re-check an [I] rule with a probe
  before building on it.
- Model coverage gaps:
  - On 13.10, x87 goes through a separate pass, FPColor (docs/c2/x87.md section 11). It is modelled:
    `rasim check / need 'A>B'` and `need stack:V`.
  - On 14.00 and 15.00, x87 values are coloured in Color like the integer registers (class 1). Their Color
    decisions are read from the trace, not predicted. The mapping onto the x87 stack afterwards is modelled
    (docs/c2/x87.md section 12: `rasim check --predict`, `need stack:V`): which calls kill the stack, fxch,
    pops, fldz and airlocks.
  - Split points made between rounds are still read from the trace.
  - Reference costs of split pieces are checked on all four builds; on 13.10 one tied-destination cost rule still
    misses (regalloc.md 3.6.11).
