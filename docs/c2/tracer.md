# c2trace: watching c2.dll's decisions during a real compile

`tools/c2trace.py` compiles a `.c` file with a traced copy of the VS2005 SP1 back end (c2.dll 14.00.50727.762)
and prints a log of chosen c2 functions being entered, being left, and of chosen instructions being reached,
together with registers and values you pick (struct fields, globals, table lookups, strings).
The point is to answer "why did it pick ebx here?" by watching the deciding code run, without guessing.

Status: **it works.** Every claim below is marked CONFIRMED (read in the code/binary, or seen in a traced compile)
or INFERRED.

## Quick start

```sh
uv run tools/c2trace.py install          # once: patch the traced copy's c2.dll, build c2trace.dll
uv run tools/c2trace.py t.c              # default hooks: @Color@4, @Spillcosts@12
uv run tools/c2trace.py --exit \
    -H '@Color@4=g10d123ac>0>0>4$' \
    -H '@ChoosePhysReg@12=s1>0x1c/xa1>0x10>0x1c~10cec7b8$' \
    -H '@ChoosePhysReg@12+0x279=esi~10cec7b8$,esi~10d14f10,ebx~10cec7b8$,s5' \
    t.c -- /Oy-                          # cl args after --  (default /c /O2 /FAsc)
```

Options: `-H/--hook` (repeatable), `--exit` (log returns of every entry hook), `--max N` (stop logging after
N events), `--raw` (raw log), `--keep DIR` (keep `c2trace.cfg`, `c2trace.log`, the `.obj` and the `/FAsc` listing).

The traced toolchain is `work/toolchain/vs2005-trace`, a dereferenced copy (`cp -RL`) of
`work/toolchain/vs2005`. `install` changes only `VC/Bin/c2.dll` there (re-patched from pristine `Bin/c2.dll`
every time, so it can be run again) and adds `VC/Bin/c2trace.dll`. `Bin/` and the original toolchain are not touched.

## Demo (the 10-line test function)

`work/scratch/glass_tracer_demo/t.c`:

```c
int g;
int sum(int *a, int n, int k)
{
    int i, s = 0, t = 1;
    for (i = 0; i < n; i++) {
        s += a[i] * k;
        t ^= a[i] + s;
    }
    g = t;
    return s + t;
}
```

`/O2` puts i, s, t, n, a in eax, edx, esi, edi, ebx, uses ecx for a[i], and uses ebp for a[i]*k. Running the
three hooks from Quick start (without `--exit`) prints (cut down, CONFIRMED):

```
    0 > @Color@4  ecx=0 edx=d0f3e4 eax=0 esp=22ebf8  g10d123ac>0>0>4$="_sum"
    1 > @ChoosePhysReg@12  ... s1>0x1c=8 s2=0
    2   > @ChoosePhysReg@12+0x279  esi~10cec7b8$="eax" esi~10d14f10=0 ebx~10cec7b8$="noreg" s5=d17990
    3   > @ChoosePhysReg@12+0x279  esi~10cec7b8$="ecx" esi~10d14f10=0 ebx~10cec7b8$="eax" s5=0
    ...                              (edx, esi, edi, ebx, ebp are tried too; none beats eax)
    9 < @ChoosePhysReg@12  eax=d17908  a1>0x10>0x1c~10cec7b8$="eax"
   10 > @ChoosePhysReg@12  ... s1>0x1c=9      -> candidates ecx..ebp        -> picks "ecx"
   18 > @ChoosePhysReg@12  ... s1>0x1c=6      -> candidates edx..ebp        -> picks "edx"
   25 > @ChoosePhysReg@12  ... s1>0x1c=7      -> candidates esi,edi,ebx,ebp -> picks "esi"
   31 > @ChoosePhysReg@12  ... s1>0x1c=4      -> candidates edi,ebx,ebp     -> picks "edi"
   36 > @ChoosePhysReg@12  ... s1>0x1c=5      -> candidates ebx,ebp         -> picks "ebx"
```

How to read it: `@Color@4` (10b0cf40) is the graph-coloring register allocator. It runs once per function, and
the function's name is `[[_CurFunc]+0]+4`. `@ChoosePhysReg@12` (10b14b70) runs once for each global pseudo that
gets a register. The probe at +0x279 (10b14de9) sits in its loop over `_RegOrders[class]`: `esi` holds the
candidate register's number, `[_ChoosePhysReg_weights + 4*esi]` holds that register's weight, `ebx` holds the
best register so far, and `[esp+0x14]` holds the best weight. In this function every weight is 0, the compare is a
strict `<` (`cmp [esi*4+weights], eax; jge` at 10b14df1), and so the first register in the order that is still
free wins each time. The six pseudos therefore get eax, ecx, edx, esi, edi, ebx, which is `_RegOrders[0]` in
order. That fully answers "why ebx" for `a`: it was colored last, and ebx was the first free register left.
(CONFIRMED by the trace. That `s1>0x1c` is the pseudo's number is INFERRED: `+0x1c` is the field that
ChoosePhysReg hashes with `& 0x3ff` at 10b14bb2.) ebp is not picked in any ChoosePhysReg call: the a[i]*k temp
gets it from some other path (INFERRED).

The traced compile is byte-identical to an untraced one. CONFIRMED: the `/FAsc` listings are identical for
t.c, for a three-function t2.c, and for `src/c2/bitvect.c` (868 lines, 6 hooks, 10,772 events). The `.obj` differs
only in the embedded obj path and the COFF timestamp. Traced compiles take about 1-2 s, which is the same as untraced.

## How it works

1. **Cave in c2.dll** (`patch_c2` in c2trace.py). CONFIRMED by disassembling the patched file:
   - The PE entry point moves from `__DllMainCRTStartup@12` (RVA 0x1d706d, VA 10cd706d) to a 45-byte cave at
     RVA 0x1d7bb0 (VA 10cd7bb0). The cave sits in the zero slack after the end of .text (VirtualSize 0x1d6bab,
     raw size 0x1d7000; for .text, file offset == RVA). The .text VirtualSize grows to 0x1d6bf0 so that the cave
     is mapped, the PE checksum is recomputed, and nothing else changes (same file size, sections, SizeOfImage).
   - The cave is position-independent: `cmp [esp+8],1 / jne` (DLL_PROCESS_ATTACH only), `pushad`,
     `call $+5 / pop ebx`, `push "c2trace.dll"`, `call [ebx + (IAT LoadLibraryA - here)]` (the IAT slot of
     KERNEL32!LoadLibraryA is RVA 0x1d8028 = VA 10cd8028; c2 already imports it), `popad`,
     `jmp __DllMainCRTStartup@12`.
   - LoadLibraryA finds c2trace.dll in cl.exe's directory (`VC/Bin`). If the DLL is missing, the call returns 0
     and c2 runs normally.
2. **c2trace.dll** (C source embedded in c2trace.py as `DLL_SRC`, built `/O2 /MT /LD` with our own cl). Its
   DllMain runs while c2's DllMain is still pending, which is after the loader has applied c2's relocations.
   - If `%C2TRACE_CFG%` is unset it does nothing, so the traced toolchain can also be used as a normal compiler
     (CONFIRMED: the output is identical).
   - Otherwise it reads the config and, for each hook, builds a trampoline in VirtualAlloc'd RWX memory:
     `pushfd; pushad; push esp; push id; call on_entry; add esp,8; popad; popfd; <displaced instructions>; jmp back`.
     It then writes `jmp trampoline` (5 bytes, with int3 over any remaining displaced bytes) over the hook address
     in c2's .text, using VirtualProtect. All registers and flags are preserved. The displaced bytes are copied
     from memory, so relocated absolute operands stay correct. Relative `jcc`/`jmp`/`call` among them are
     re-encoded as rel32 to the same target (the config's `i` lines).
   - **Exit logging**: on_entry saves the return address in a shadow stack `frames[depth]` and replaces it with
     the address of exit stub `depth` (`push depth; jmp common`; one 10-byte stub per depth level, 4096 of them).
     The common stub does `pushfd; pushad; push esp; call on_exit_`. on_exit_ logs eax/edx and the exit specs,
     writes the real return address into the slot that `push depth` used, and the stub's `ret` goes there. Using
     one stub per depth level means a return never has to be matched by guessing. On entry, frames whose
     saved esp is <= the current esp are dropped first (they are dead, e.g. after a longjmp).
   - Log lines are buffered (64 KiB) and written with WriteFile to `%C2TRACE_LOG%` when the buffer fills and at
     DLL_PROCESS_DETACH. CONFIRMED: when c2 dies with C1001 the log is still flushed, because cl exits normally.
   - Memory reads made by specs are wrapped in `__try/__except`, so a bad pointer logs `?` instead of crashing.
3. **c2trace.py** resolves each hook's name to a VA (`work/index/c2.dll.csv` via tools/image.py) and uses capstone
   to choose the displaced instructions. It refuses a hook if:
   - fewer than 5 bytes are available before ret/int/indirect branch/loop/jecxz;
   - an instruction in the containing function branches into the displaced bytes;
   - the detour would run past the end of the function;
   - a probe is not on an instruction boundary.

   It then writes the config, runs `msvc.py` with `VS8_ROOT=work/toolchain/vs2005-trace` and the C2TRACE_* env
   vars (as Z:\ paths), and decodes the log.

Raw log format (`--raw`): `E seq depth hook eax ecx edx ebx ebp esi edi esp | specs...` and
`X seq depth hook eax edx | specs...`. `esp` is esp at the hooked instruction (at function entry, `[esp]` is the
return address).

## Adding a hook

You don't edit code: `-H` is the whole interface.

- **Pick the function**: `uv run tools/card.py c2 --module color` lists a module's functions, and
  `uv run tools/card.py c2 @Name@N` prints the decompile and disassembly. Work out where its arguments live.
  c2 is /Gr fastcall (ecx, edx, then the stack), but under LTCG internal functions often take arguments in other
  registers: `@ChoosePhysReg@12` takes one in eax (`mov esi, eax` at 10b14b83) plus two on the stack (Ghidra's
  `param_3`, `param_4` = `s1`, `s2`).
- **Entry hook**: `-H '@Name@N=spec,spec/xspec,spec'`. Specs before `/x` are logged at entry. Specs after `/x` are
  logged at return (this turns on exit logging for that hook). Use `aN` in exit specs to reach the entry stack words,
  because the callee's argument area has already been popped and overwritten by then.
- **Probe on an instruction**: `-H '@Name@N+0xOFF=specs'` or `-H <hex VA>`. It logs every time that instruction runs.
  `sN` is `[esp+4N]` at that point, so locals at `[esp+X]` are `s(X/4)`. Probes have no exit logging. Pick an
  instruction that no branch targets inside the next 5 bytes. The tool checks branches within the same function;
  it does not check branches from other functions.
- **Spec language**: `base { '>' off | '~' table } [ '#' n | '$' | '%' ]` (`%`: IL dump from tuple v, see `il`)
  - base: `eax ecx edx ebx esp ebp esi edi` (values at the hook), `sN` (stack word N), `aN` (entry stack word N,
    saved, so it can be used at exit), `gHEX` (a c2 VA as a constant, e.g. a global's address; adjusted if c2 were
    rebased).
  - `>off`: v = dword [v + off]. `~HEX`: v = dword [HEX + 4*v] (index a table of dwords or pointers).
  - `#n`: log n dwords at v. `$`: log the C string at v (≤79 chars, non-printables shown as `.`).
- **Global addresses** come from `llvm-pdbutil dump -publics work/pdb/c2.pdb`: `addr = 0003:X` is .data, VA
  10d06000 + X; `0002:X` is .rdata, VA 10cd8000 + X.
- More than about 10 hooks? The DLL takes up to 128 hooks, 12 entry plus 12 exit specs each, and nesting up to
  4096 deep.

## `regs`: the register-allocation report (predict, don't permute)

```sh
uv run tools/c2trace.py regs tu.cpp --func FUN_00e0a0b0 --why center,this -- /TP /MT /GS /EHsc /Gy
```

For each function whose decorated name contains `--func`, it lists the globregs (variables, parameters,
multi-def temporaries) **in the order Color pops them**, with prio, benefit, nrefs, defOrd, the registers
ChoosePhysReg tried and their weights, and the outcome (register, `spill/defer`, `uncolourable`).
`--why a,b` (or `*`) breaks each prio down by block: `w`, `P`, the block's reference cost and the term, or
the live-through penalty. The terms must add up to the popped prio; the report flags a mismatch (a split or a
recomputed range). Hooks used: `@Color@4+0x401` (10b0d341, gr in ebp just after the pop), ChoosePhysReg
entry/exit and its candidate probe, SpillOrDefer, HandleCurrUncolorable, and four probes in Spillcosts'
end-of-block update (10b11487, 10b11495, 10b114c4, 10b114d2). Code: `tools/c2regs.py`.

Workflow for a register swap: run `regs` on the near miss, find the two variables that took each other's
registers, and read `--why` for both. The difference between their prios is the exact amount the source has to
move, and the blocks where their terms differ say where. Then edit only those blocks and check that the prio
moves the way the formula says *before* comparing bytes. Ties go to the lower `def`.

Worked example, Fallout 3 0x00a89680 (the draft differs from the original only by `this` and `center` swapping
esi and ebp): `center` 74 → esi, `this` 52 → ebp. Excluding blocks where both score the same, `center` nets +20
in the loop's compare blocks (it is read in three of them), while `this` nets −2 (+26 for the entry copy from
ecx, minus the penalties for living through the loop). The Y compare that appears once in the final code is
still in both branches at allocation time; it is merged after Color. Every spelling of the compares that keeps
the original's branch tree leaves 74/52. So does an inlined helper taking `this`, a `self = this` copy, or
`/Ox`, `/GS-`, `/fp:strict`. Only reading Y once flips the pair (this 61, center 59), and that changes the code.
This was found independently here and by the f3decomp agent (fn3-test docs/TRACER-a89680.md). The merge is
`FgTryHeadMerge` in `dop2_FinalFlowOpts`, after Color; head merging never runs before register allocation
(il_fg.md rule 12, found with `il` below). Also seen with `il`: before Color, loop optimisation hoists
`lea t243 = center+100` and `lea t252 = center+104` into the preheader, and Color's FoldTooCheapDefs folds them
back into each compare, which is what gives `center` its three loop references.

Not covered by `regs`: scratch picks (see `scratch`), x87 `FPColor`, names for temporaries (shown as
`t<key>`), and mapping a block number to source lines.

## `il`: the function's IL at chosen passes

```sh
uv run tools/c2trace.py il tu.cpp --func FUN_00e0a0b0 --count center -- /TP /MT /GS /EHsc /Gy   # refs per pass
uv run tools/c2trace.py il tu.cpp --func FUN_00e0a0b0 --at @Color@4,@FgCodeMotion@4 --grep 'center|fcom'
```

At the entry of each pass in `--at` (default: the per-function phases of il_pipeline.md §5.3 plus the passes
around register allocation), it prints the tuple stream of the function being compiled. Start:
`_CurFunc → FUNC.fg (+8) → first block → +0x1c`, then the `next` links. Each instruction shows the opcode name
(`_TupOpcodeName`), its source line offset (tuple +0x14, relative to FUNC+0x44), and dsts `<-` srcs. Operands
print as SSR names, `t<key>` temps, `k<key>` immediate candidates, `phys=pseudo` once registers are assigned,
and `[sym+base+index*s+disp]` for memory, with `:type`. `--count RE` gives one line per pass with the number of
matching instructions; it is the quickest way to find the pass that changes something. Implementation: the
`%` spec suffix in c2trace.dll (dump the stream from tuple v, at most 2,000,000 tuples by default (`ilmax` directive, `C2TRACE_ILMAX`), guarded reads), and
`tools/c2il.py`. A pass that runs while the flow graph is being rebuilt can print a partial stream.

## `scratch`: scratch-register picks, mapped to listing offsets

```sh
uv run tools/c2trace.py scratch tu.cpp --func FUN_00c7e9a0@SceneOwner -- /TP /MT /GS /EHsc /Gy
```

One row per `RegAssign` call (regasg, after Color; regalloc.md §6), in IL order: the cursor `_NextIntPos`
before the pick (10d0f854, a pointer into the byte array `_IntPhysRegs` 10d0cfd0 = 1 2 3 7 8 4 6 0), the temp's
preferred register (read at exit, since RegAssign sets it itself for copies and `lea`), `ROT` when
`CmdSetNextPos` ran, the register given (RegAssign's return value, `&_RegSym[r]`), the instruction at pick time,
and its **offset in the final listing**. The offset comes from a second IL dump at `@Encode@4`: once
`EncSetOffsets` has run, tuple +0x1c holds each instruction's encoded length, and the running sum equals the
listing offsets (checked on `sum`: 0x20 at the loop label). A pick whose instruction no longer exists at
Encode shows `gone`. That is the hidden-pick case: it moved the cursor but left nothing in the listing. `~` means
the instruction is still there with another opcode (`mov` → `xor`, `lea` → `add`). IL order is not layout
order: EH unwind code, for example, is assigned first and laid out last.

Worked example, Fallout 3 0x008e7d40 (fn3-test checkpoint chk_6207ebabb831472ba89d, compiled after the pack
unit): every pick lands on the original's register except picks 7 (+0xa8), 13 (+0x166) and 14 (+0x1b1). All
three are ordinary rotation picks, so the late `push [mem]` split is not involved. Reading the original's
registers at the same offsets constrains its cursor:
- If pick 13 (`mov r,[this+0x2a0]`) did not rotate, the cursor would give 14 = ecx, 15 = edx and 16 = eax,
  exactly the original. So in the original that load got eax without moving the cursor (a preference, or a
  Color variable).
- Pick 7 (the same load) needs the original's cursor on eax with pick 9 still on edx: exactly one more cursor
  move between picks 5 and 9 than in our draft.

Spelling the load as `GetName()` or `name` makes no difference. Making the load a variable (regalloc.md §7)
does give `push eax` at +0xa8 without moving the cursor. But it raises `this` by 4 in block 12 (pressure 5 → 6),
from a tie with `scene` (79, lost on defOrd) to 83. `this` is then coloured whole in edi instead of being split
into the original's esi and edi pieces: 174+ register lines differ. That was so for all 26 variants tried (named
local, reused `old`, shared local, parameter, both compare spellings). What the original needs, in these terms:
the two `name` loads must not move the cursor, there must be one extra hidden cursor step before pick 9, and
`this` must stay at or below `scene` (79) in Spillcosts pass 2.

`regs --block N` lists every value with a term in block N, for each Spillcosts pass: the referenced ones (they make
up P, and each gets cost·P·w) and the live-through ones (each loses P·w). From pass 2 on, only uncoloured values are
updated and listed, but P still counts the coloured ones. Unnamed values show as `t<key>` (temps), `imm<key>`
(immediates) or `local<key>`. Other `regs` extras used here: `--why` prints each variable's total per Spillcosts pass (split pieces are separate
globregs), the block terms carry source offsets, and `--blocks LO-HI` prints a block's IL at Color entry.

**EH funclets.** `RegSimpleAssignBody` runs once per unwind funclet as well as for the body, all under the
function's name (0x008e7d40: 13 funclets, then the body). `scratch` used to concatenate all of their picks, which
shifted pick numbers, cursors and offsets in EH functions and made the picks look as if a later pass renamed
registers. Fixed: only the run with the most picks (the body) is reported. There is no renaming pass after
RegAssign: the final scratch registers are the picks, in IL order at `RegSimpleAssignBody`; the scheduler then
reorders the instructions (sched.md).

## `tsweep.py`: sweep variants scored by trace numbers

```sh
uv run tools/tsweep.py tmpl.cpp --func FUN_00c7e9a0@SceneOwner \
    --metric "hinge(prio('this',2), prio('scene',2)) + 10*(dst(0xa8)!='eax')" \
    --show "prio('this',2)" --show "ins(0xa8)" --show regdiff \
    --asm-vs orig.s --asm-func 'FUN_00c7e9a0@SceneOwner@@QAEXPAUArgument@@PBD@Z' -- /TP /MT /GS /EHsc /Gy
```

The template uses `sweep.py`'s `{{a<|>b}}` alternatives. Every combination is traced (the `regs` hooks, the
`scratch` hooks, or both, chosen from the names the expressions use) and ranked by `--metric`, lower first. Use it
when the fix is "make X outrank Y" or "get eax at +0xa8", where most steps toward the fix look like no progress in
bytes. The names available are listed in the script's docstring:
- `prio(var[, pass])`, which falls back to the latest earlier pass, since later passes only re-cost new pieces;
- `reg`, `pops`;
- the scratch picks `cur(n)`, `sreg(n)`, `rot(n)`, `off(n)`, `gone()`, `sat(offset)`;
- `ins(offset)` and `dst(offset)` from our listing;
- `regdiff`: an **aligned** difference against a `VA mnemonic operands` disassembly of the original: instructions
  (mnemonic plus registers) left unmatched by a sequence alignment, both sides counted, our trailing EH funclets
  ignored. `posdiff` is the old positional count. Use `regdiff`: one inserted instruction shifts every later line
  in a positional count (0x008e7d40's best variant: posdiff 238, regdiff 5);
- `hinge(x, t)`.

Traces are cached (tools/ccache.py, below), so re-scoring a finished sweep with a different metric takes seconds
(64 variants in 19 s).

Variants go to `work/tsweep/<template>/vNNN/`, which keeps the traces' listings. Checked on 0x008e7d40 with a
4-variant grid: it reproduces the measured trade-off (named local: dst(0xa8) = eax but `this` 83, regdiff 174; the
draft: ecx, 79, regdiff 7).

## `rasim`: what-if simulator for Color's priorities (Rust)

```sh
cargo build --release --manifest-path tools/rasim/Cargo.toml          # once; binary tools/rasim/target/release/rasim
uv run tools/c2trace.py regs tu.cpp --func F --export snap.json -- /TP /MT /GS /EHsc /Gy
rasim check  snap.json                                    # does the model reproduce this trace?
rasim blocks snap.json --pass 2 scene this                # per-block terms side by side
rasim order  snap.json --pass 2 var:nm1:12                # the colouring order after an edit
rasim need   snap.json --pass 2 var:nm1:12 'scene>this'   # which single edits (then pairs) restore the order
```

The snapshot holds every Spillcosts pass: its class, each block's w and true P, and each uncoloured globreg's
per-block terms, plus the actual pops. rasim applies the Spillcosts formula and the tie-break (regalloc.md §3.2,
§3.4) to that block × globreg matrix. At pass k the list is pass k's new pieces plus every earlier-costed globreg
of the class that was neither popped nor replaced by split pieces; those keep the priority they were costed with.
Edits are what source changes do to the IL (regalloc.md §7):
- `ref:VAR:B[:C]`: a reference in block B;
- `unref:VAR:B`: no reference, still live through B;
- `drop:VAR:B`;
- `var:NAME:B[:C],..`: a new globreg, e.g. a named local;
- `w:B:N`, `P:B:N`.

A new reference changes P_B, so it moves every other variable in the block too. `need` only proposes references
in blocks where the variable is already live (elsewhere the edit would also stretch its range, which isn't
modelled), and runs in milliseconds.

`check` results (every per-block term and every Spillcosts total matches the formula in all of them):

| snapshot | terms | colouring order predicted |
|---|---|---|
| 0x00a89680 draft | 150 | 29 / 29 |
| 0x008e7d40 by-value draft | 851 | 69 / 69 |
| 0x008e7d40 match (v_match) | 905 | 69 / 69 |
| p4 / p6 probes (4 functions) | 7..86 | all |
| 26k-instruction synthetic (b250.c) | 323,019 | 2546 / 2548 |

What made the order exact (each read from c2's code, then checked):
- **Splits are recorded, not guessed.** `regs` probes RemoveSplitGlobregs (10b11ae9) and the export lists, per
  pass, the globregs split during its colouring (`split_removed`). Earlier, splits were inferred from names, which
  fails for immediates (all named `imm0`) and inlined locals (`p`, `a`).
- **A globreg is (record pointer, the pass it was costed in).** GLOBREG records are recycled (`_FreeGlobs`), even
  under the same name.
- **Deferral.** A deferred web is re-inserted by InsertByBenefit with its neighbour's priority, so its second
  pop is checked against the deferral order (non-immediates by ascending spill cost, then immediates), not
  against a total (regalloc.md §3.5).
- **Exact ties** follow insertion order backwards (regalloc.md §3.4): bucket descending in the initial list;
  later split rounds first and higher web number first among split pieces.

Known limits:
- `SpillObvious` (10b11560) is not modelled. A globreg that is never popped and never split is treated as removed
  (trace fact), so an edit cannot add or undo such a removal. For `var:` edits this means a new value is assumed
  to survive; a real named local with 2 references is usually folded away (`CmdSubstDef`). For the *other*
  variables' priorities only the new value's presence in P matters, and an immediate candidate (cost 0, leaves no
  code: frontend.md) is the cheapest way to get it.
- Splitting is not simulated. An edit that changes the colouring order can change later splits; re-trace to
  confirm.
- Register choice (interference, preferences) is not simulated: rasim answers "who is coloured first". Which
  register each value *should* have comes from `regmap` (below).

Answers on the two Fallout 3 functions (millisecond runs):
- 0x00a89680, `need 'this>center'`: any one of
  - one more reference to `this` in block 1 (the entry, P=13) or block 12 (after the loop, P=20);
  - dropping `center`'s reference in block 10 or 12.
- 0x008e7d40, `need --pass 2 var:nm1:12 'scene>this'`: with the site-1 variable in place, any one of
  - one more reference to `scene` in a block where it is live: 27 (P=13), 36 (P=10), 30 or 35 (P=8);
  - one fewer reference to `this` in block 1, 2 or 6 (initialize's `name` handling).

## `regmap`: which register each variable should have (inverse constraints)

```sh
uv run tools/c2trace.py regmap tu.cpp --func FUN_00c7e9a0@SceneOwner --asm-vs orig.s \
    --asm-func '?FUN_00c7e9a0@SceneOwner@@QAEXPAUArgument@@PBD@Z' -- /TP /MT /GS /EHsc /Gy
```

The final IL (at `@Encode@4`) writes every allocated operand as `reg=var`. regmap places each instruction in our
listing by its encoded offset, aligns our listing to the original (same mnemonic and operand shape), and, where we
have `var` in register r, takes the original's register at the same operand position as a vote. Per variable it
prints our registers, the votes and a verdict (`ok`, `WANTS r`, `mixed`, judged instruction by instruction, so
split ranges and inlined locals that share a name are handled), then the swaps (A has what B wants and vice
versa), which become `rasim need 'A>B'` constraints.

On 0x008e7d40: the by-value draft has 16 variables off, including `this` (5 of 13 votes want esi: the
original splits it) and a set of edi/esi and scratch swaps. The best variant (v_best5): 157 of 157 variables
agree everywhere, so its remaining 5-instruction difference is instruction selection, not allocation.

## `helpersweep.py`: inline-helper rewrites that fix a colouring order without changing code

```sh
uv run tools/helpersweep.py tu.cpp --func '?FUN_00c7e9a0@SceneOwner@@QAEXPAUArgument@@PBD@Z' \
    --want 'scene>this' --pass 2 --asm-vs orig.s -- /TP /MT /GS /EHsc /Gy
```

For a function whose own code is right but whose register order is not (the `regs`/`rasim` diagnosis: "A must be
coloured before B"), it finds the inline helpers the function uses, lists every member address and reference
parameter taken in them (`&p->m`, `&t`), and rewrites each to `&hs_bind(...)`: a reference binding, which in an EH
function leaves a pick-neutral variable (frontend.md, catalogue). Each single rewrite is traced once (gap of A>B,
code change against the baseline, scratch-pick change). Combinations are predicted by adding the single gaps
(the priority formula is linear in P, rasim's model) and only the best predicted sets are compiled.

On 0x008e7d40, starting from the by-value draft: 59 helpers, 9 sites, 3 code-neutral singles (gap −1, −1, −4
against a gap of 6), 1 predicted combination, verified byte-exact: 11 compiles in all, against about 500 hand-written
variants in the session that first found the match, and the same three edits. On 0x00a89680 (no EH frame) it
finds no sites, as the rule predicts.

Shell note: pass cl flags literally (`-- /TP /MT /GS /EHsc /Gy`). In zsh an unquoted `$F` holding several flags
is one argument, cl then compiles with other settings, and the traces silently describe a different compile.

## Compile cache (`tools/ccache.py`)

`c2trace.run_trace` (so `regs`, `il`, `scratch`, `regmap`, `tsweep`) and `chk.py` cache by content: the
source, every file it pulls in with `#include "..."` (recursively), the compiler arguments without output paths,
the trace config and the hashes of the traced c2.dll and c2trace.dll. A hit restores the log, listing and object
(2.7 s → 0.2 s per trace). `CCACHE=0` bypasses it, `CCACHE=refresh` recomputes; `uv run tools/ccache.py
stats|clear`. System headers are not hashed (the toolchain is fixed). The IL dump's tuple cap is now 2,000,000
per dump (`C2TRACE_ILMAX` to change it; it was 20,000).

## Things learned while building it (useful for the specs)

| Fact | Where | Status |
|---|---|---|
| `_CurFunc` (VA 10d123ac) == `_Function` (10d0d700) == the argument of `@Color@4` (in edx at entry, also `[esp+4]`) | trace | CONFIRMED |
| `[[_CurFunc]+0]` is the function's symbol; its name (decorated, e.g. `_sum`, `@FbvSetBit@8`) is at `+4` | trace; `@SSGetUnameW@4` 10bc14d0 reads a symbol's name at `+4` | CONFIRMED |
| `_RegName` (10cec7b8): `char*[]` indexed by c2 register number: 0 noreg, 1 eax, 2 ecx, 3 edx, 4 ebx, 5 esp, 6 ebp, 7 esi, 8 edi, 9 ax, 10 cx, 11 dx ... | read from the binary | CONFIRMED |
| `_RegSym` (10d09f70): array of 0x60-byte register symbols; entry n has register number n at `+0x1c`, and `+8` points to itself | trace dump | CONFIRMED |
| ChoosePhysReg stores its answer as `pseudo->+0x10 = &_RegSym[reg]` | 10b14b70 decompile, trace | CONFIRMED |
| `_RegOrders` (10ce5a08) `[0]` -> {eax, ecx, edx, esi, edi, ebx, ebp, 0}: the order ChoosePhysReg tries registers in for class 0 (int32) | read from the binary, seen in the trace | CONFIRMED |
| `_ChoosePhysReg_weights` (10d14f10, 0x174 bytes, memset at entry 10b14b85): per-register cost; lowest wins, ties go to the earliest in `_RegOrders` | 10b14df1 `cmp/jge`, trace | CONFIRMED |
| In `sum`, all weights were 0, so the order alone decided the picks | trace | CONFIRMED (this function only) |
| `@Color@4` runs once per function compiled; `@Spillcosts@12` (10b10420) runs 1-2 times per Color call in these tests (56 per 40 in bitvect.c) | trace | CONFIRMED (these inputs) |

## Limits

- **x86-only, this exact c2.dll.** The patch asserts the pristine entry point, the .text size and the zero slack.
- **Displacement constraints**: a hook needs 5 bytes of displaceable instructions with no branch into them. Branches
  from *other* functions into a hooked prologue (cross-function tail merges) are not checked; they are rare in
  c2 but possible. If a hook makes a compile misbehave, compare the listing with an untraced compile.
- **Exit hooks change the return address** while the function runs. Any c2 code that inspects its own return
  address would see the stub (none is known). Code that unwinds by longjmp (c2's `fatal` / error recovery)
  leaves stale frames, which are dropped at the next entry. The exit of the function that longjmp'd is not logged.
- **One thread assumed** (c2 compiles serially in-process). The shadow stack and the log buffer are not
  thread-safe.
- **No reads of floating point or SSE state**, and no logging of memory *writes* (use a probe after the store).
  Specs print hex dwords or strings; interpreting structs is up to you (use `src/c2/tuple.h` etc.).
- **LTCG (`/GL` + `link /LTCG`)**: link.exe loads c2.dll from the same `VC/Bin`, so the cave should fire there too.
  Untested (INFERRED). Run it via `VS8_ROOT=... uv run tools/msvc.py --tool link ...` with C2TRACE_CFG/LOG set by
  hand. Copy a cfg from `--keep`.
- **Cost**: each event costs a sprintf. 10k events add no measurable time. A hook on a very hot function
  (`@SbvNext@4` gets ~5k calls in bitvect.c) produces large logs, so use `--max`.

## Options evaluated

**(a) Binary patch + helper DLL: chosen, and it works** (above). It costs one 45-byte cave in c2.dll. The detours
are written at runtime by our own DLL, so adding a hook needs no repatching or rebuild.

**(b) winedbg `--gdb` + lldb: works, but slow and awkward; kept as a fallback for mid-instruction-level
experiments.** wine-11.16 runs cl.exe as a WoW64 process; lldb is Apple's `/usr/bin/lldb`; there is no gdb.
Results (CONFIRMED):
- `winedbg --gdb --no-start --port P <...>/cl.exe ...` plus `lldb` with `gdb-remote 127.0.0.1:P` connects and shows
  i386 registers. The proxy accepts **one** connection only: probing the port with `nc -z` uses it up.
- lldb does not see DLL loads as modules. A breakpoint set before c2.dll maps is never inserted. Each DLL load is
  reported as a SIGTRAP stop, and c2.dll is mapped after about 8 stops.
- `lldb --batch` stops running commands at the first signal stop, so it has to be scripted in Python
  (`SBProcess.Continue` loop).
- `Z0` (software breakpoint) gets an empty reply (unsupported). `Z1` (hardware) replies OK but **never fires**:
  Rosetta does not emulate the debug registers (INFERRED; it was observed not firing).
- What does work: write `0xCC` yourself with `SBProcess.WriteMemory` once c2 is mapped. On the stop at bp+1,
  restore the byte, set pc back, `StepInstruction`, and re-arm. That caught all 3 `@Color@4` calls for t2.c, with
  the same ecx/edx as the tracer, and 105 `@SbvNext@4` calls in about 0.6 s, i.e. a few ms per hit.
- Prototype script: `work/scratch/glass_tracer_dbg/glass_tracer_lldb2.py` (usage: start
  `winedbg --gdb --no-start --port P cl.exe ...` in the background, then
  `lldb --batch -o "command script import .../glass_tracer_lldb2.py" -o "command script add -f glass_tracer_lldb2.run tr" -o "tr P <va> <first-byte-hex>"`).

  Its advantages: no displacement constraints (any instruction), and you can stop and look around interactively.
  Its drawbacks: about 1000x slower per event than (a), single-stepping through wine's proxy, fragile setup.

**(c) Other ideas, not needed**: an import-table entry for c2trace.dll instead of the cave (larger PE change); a
proxy c2.dll that forwards cl's few entry points to a renamed original (it works in principle, but it is more
work than the cave and gives no extra visibility); wine's `WINEDEBUG=+relay` only shows calls
between DLLs (cl<->c2 entry points), not c2's internal decisions.
