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
- **Spec language**: `base { '>' off | '~' table } [ '#' n | '$' | '%' | '%+' ]` (`%`: IL dump from tuple v, `%+`: the same with notes and the P/C/A lines, see `il`)
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

On the default c2 build `regs` also records what the register choice is made from (regalloc.md §4), for
`--export` only; the report is unchanged. Hooks 13..26 (`CHOICE_HOOKS`):
- ChoosePhysReg probes: each member of `nbrs` (10b14bd2: record, web number, avail chunk, benefit, colour, name)
  and each of its PREFs (10b14be3; reached only when its benefit is > 0), the globreg's own avail set (10b14d3f)
  and PREFs (10b14d50), each `_UpdatePrefsBv` partner (10b15152);
- `@InsertInList@8` and `+0x6`: the avail set and up to 7 PREFs (an unrolled `>0x38>0>0..` chain; it ends at the
  first `?`) of every globreg entering the priority list; `@InsertByBenefit@8` marks a deferral;
- six probes on Spillcosts' PREF sites (10b1082d, 10b10883, 10b10c31, 10b10d9c, 10b10e5f, 10b11238): the globreg,
  the register and the other side of the copy.

Snapshot fields added (older readers ignore them): `pops[].choice = {avail, prefs, nbrs[], partners[]}`,
`inserts[]`, `pref_events[]`, `regnames`. Registers are c2 numbers; an avail set is decoded from the first SBV
chunk (bit n of chunk `base` = register `base + n`). A build table for another c2 (tools/c2builds) has only the
first 13 events; then these fields are absent and `rasim check` says the registers were not checked.

Workflow for a register swap: run `regs` on the near miss, find the two variables that took each other's
registers, and read `--why` for both. The difference between their prios is the exact amount the source has to
move, and the blocks where their terms differ say where. Then edit only those blocks and check that the prio
moves the way the formula says *before* comparing bytes. **Ties (equal prio) go to the larger `def`**, in every
class and on all three builds: inside one block that is the value defined earlier, across blocks the value defined
in the later block (regalloc.md §3.4; `uv run tools/c2trace.py regs tests/data/tieorder.c --all-classes` shows it
for int and x87, and with `-- /arch:SSE2` for XMM).

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

### `--func`: one spelling for every tool

`--func` selects functions by their decorated name. `regs`, `scratch`, `il`, `regmap` (also its `--asm-func`),
`c2sched.py`, `tsweep.py`, `rwsweep.py` and `helpersweep.py` all go through one helper (`c2trace.func_pat`,
`func_match`, `func_decl`, `listing_proc`) and accept:

| form | matches |
|---|---|
| `Class::method` (`N::C::m`, `C::C`, `C::~C`) | names starting with `?method@Class@@` (`?m@C@N@@`, `??0C@@`, `??1C@@`) |
| `::name` | the C++ global function `?name@@...` |
| anything else: `tessellate`, `init_screen@CharSheet`, `_sum`, a full decorated name | a substring of the decorated name, as before |

`rwsweep.py` and `helpersweep.py`, which need the class to find the definition in the source, take
`Class::method` and `method@Class` as well as the decorated name. A listing is searched for the selected `PROC`
whether the name is followed by a space or a tab (C names, and every name on cl 13.10, use a tab; `tsweep`'s
`ins()`/`dst()`/`regdiff` and `regmap --asm-func` used to need the full decorated name and failed on those).
Operators and templates have no short form: give a substring of the decorated name. **[C]** (`regs --func
CharSheet::init_screen`, `scratch --func ::eA`, `regmap --func renderer::tessellate --asm-func renderer::tessellate`,
`rwsweep --func CharSheet::init_screen`, each on 13.10 or 14.00.)

### Immediate candidates: which constant an `imm0` is

Every immediate candidate used to print as `imm0`. `regs` now reads the constant from the candidate's symbol
(kind 0xd; a copy of the operand leaf at symbol +0x28, value or symbol at leaf +0x18, kind and type at leaf +8;
the same offsets on 13.10, 14.00 and 15.00) and shows it wherever the name is shown:

```
  2  imm0 imm        6       2    3    0  ecx           ecx:0 edx:0 ebx:0   = 7 (const, 4 bytes)
  why imm0 = &ga (addr, 4 bytes) (pop 2): prio 4 = 4
      imm0                         gr 1053670  cost 0 -> +0   = &?g_game_vars@@3Uini_file@@A#2 (addr, 4 bytes)
```

- `const`: the integer (decimal up to ±4095, else hex), `addr`: `&symbol`, `&symbol+offset`, `&"text"` for a string
  literal whose text is plain, `code`: `&function`. The size is that of the first use; the candidate is shared by
  every size (immcand.md §2.2). `#2`, `#3`: a second or third symbol that prints alike (the same address through
  another member symbol is another candidate).
- `--why 'imm=7'` or `--why 'imm=&ga'` selects immediates by that text (`--why imm0` still selects them all).
- Nothing existing moved: the `var` column still says `imm0`, the note is appended to the row, to the `why`
  header and to the `--block` lines. The export gets `pops[].imm` and `passes[].grs[gr].imm` =
  `{text, kind, size, value | sym}`; rasim and the other readers ignore them.
- How: two specs are appended to the pop event and five to each end-of-block event (after the existing ones;
  `c2regs._imm_specs`, positions in `IMMPOS`), derived from each build's own hook strings, so the build tables
  did not change. A table can switch it off with `"regs_imm": false`. The IL dump (`--blocks`, `il`) still prints
  `k0`: that text comes from c2trace.dll and was left alone; the load `OPENTERLOAD k0 <- 1000` names the constant.

What decides whether a constant is a candidate and in which blocks it counts is in docs/c2/immcand.md.

## `il`: the function's IL at chosen passes

```sh
uv run tools/c2trace.py il tu.cpp --func FUN_00e0a0b0 --count center -- /TP /MT /GS /EHsc /Gy   # refs per pass
uv run tools/c2trace.py il tu.cpp --func FUN_00e0a0b0 --at @Color@4,@FgCodeMotion@4 --grep 'center|fcom'
uv run tools/c2trace.py il tu.cpp --func FUN_00e0a0b0 --at @MscRVO@4 -- /TP /MT /GS /EHsc /Gy   # what c1xx handed c2
uv run tools/c2trace.py il tu.cpp --func FUN_00e0a0b0 --diff @Globregs@4,@Color@4 -- /TP /MT /GS /EHsc /Gy
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

**The dump is complete for the 14.00 build** (`il` uses the spec suffix `%+`, the extended dumper `dumpilx` in
c2trace.dll; re-run `uv run tools/c2trace.py install` once after updating, an older installed DLL silently
prints the plain dump). Everything new is either a note at the end of an `I` line, after ` ; `, or a new line
kind, so the `I <tuple> <opcode> <line> <len> | dsts <- srcs` prefix is what it always was; `regs`, `scratch`,
`regmap` and `c2sched.py` keep using the plain `%` dump, which is unchanged. `--plain` gives the old output.
Example (`work/scratch/ilx/ilx_eh.cpp`, `g2`, at `@MscRVO@4`; cut):

```
  P d19540 OPBLKSTART d0d5e0                                  scope marker (a pragma tuple, not an instruction)
  I d19620 OPARG 1 0 | t64:1004 <- _w:1004 ; s0=&             src 0 is &w, not w
  I d19670 OPCALL 1 0 | t65:1004 <- s0:1004 t64:1004 ; s0=??0D@@QAE@XZ      call target
  I d19838 OPPUSHSTATE 1 0 | <- t65:1004 ; eh{fl=1}           EH record; its unwind action follows as A lines
  A I d1975c OPARG 1 0 | t66:1004 <- _w:1004 ; s0=&
  A I d1978c OPCALL 1 0 | <- s0:1004 t66:1004 ; s0=??1D@@QAE@XZ
  I d1762c OPSWITCH 1 0 | <- t67:1004 ; default=Ld19e04 cases=2
  C d1762c 1 1 Ld19958                                        case 1..1 -> label tuple d19958
  C d1762c 2 2 Ld19edc
  L d19958
  ...
  I d173d8 OPDTORACTION 1 0 | <- ; eh{n=2 fl=1}               this exit unwinds two states (y and w)
  I d17444 OPRET 1 0 | <- s5:1004
  I d174b0 OPGOTO 1 0 | <- s0:0 -> ; s0=Ld17478               branch target = the `L d17478` line
  I d17524 OPPOPSTATE 1 0 | <- ; eh{n=1 fl=a1}
  P d1a254 OPBLKEND d0d6f0
```

| output | read from (14.00 offsets; il_tuple.md) | status |
|---|---|---|
| `; cc=ne` on a branch (`OPBRANCH`, `_jcc`) | low nibble of tuple +0x0a, IL condition code 1..15 printed as the x86 suffix (`e ne l g le ge b a be ae s ns p np o`) | CONFIRMED: `TupMakeBranch` 10bf4868; the cc of every `_jcc` at `@EncEmitFunc@4` equals the listing's `jcc` (`work/scratch/ilx/ilx_cc.c`: jge, jb, jne, jns, jle, ja) |
| `; cc=ne` on `OPQUESTION` | byte at tuple +0x30. `dst <- flags a b ; cc=X` is `dst = X(flags) ? a : b` | CONFIRMED: `TupMakeQuestion` 10bf3dde; `a ? 7 : 9` prints `OPCMP _a 0` / `OPQUESTION .. 7 9 ; cc=ne` |
| `sN=L<tuple>` | a CODEADDR leaf (kind 4) whose symbol's +0x33 is a LABEL tuple: branch target, jump-table `_data` entry. The value is the tuple address an `L` line prints | CONFIRMED: `TupMakeBranch` builds the leaf from `label+0x24`, `TupMakeLabel` 10bf4bc2 stores the back pointer |
| `sN=<name>` | any other CODEADDR leaf: the symbol's name pointer at sym+4 (these leaves hold the front-end symbol, so the name is at `[sym+4]`, not `[[sym+0]+4]`). Call and tail-jump targets, `&f`, `__ehhandler$f` | CONFIRMED (trace) |
| `sN=&` / `dN=&` | the operand is a DATAADDR (kind 3) or EA (kind 5) leaf: the address of what is printed. Without the note the same text is the value (kind 2 SYM, kind 6 INDIR) | CONFIRMED (trace: `int *p = &x` is `_p <- _x ; s0=&`, `h(y)` for an array `y` is `OPARG <- _y ; s0=&`, every `lea`) |
| `sN=1.5` | FCONST leaf (kind 9): the 80-bit x87 value at leaf +0x18, converted to a double by the DLL (`%.9g` for a 4-byte operand, else `%.17g`). c2il.py shortens it to the shortest text that reads back the same, and adds `(0.1f)` when the value is exactly that float (a float literal used in double arithmetic) | CONFIRMED: `TupMakeFloatConst` 10bf1dc9 copies 3 dwords; 1.5, 0.1, 2.0 and 0.1f print right (`ilx_ops.cpp` f1, f2) |
| `intrin=159` | u16 at tuple +0x30 of `OPINTRINSIC` (159 `_rotl`, 167 `strlen` seen) | CONFIRMED (storage: il_tuple.md §6.3; the two numbers: trace) |
| `default=L<tuple> cases=N` and `C <switch> <low> <high> L<tuple>` | SWITCH +0x30 default LABEL tuple, +0x34 list of CASE nodes {+0xc low, +0x10 high, +0x14 LABEL tuple}; values are in the selector's units after the reader's normalisation (`case 1:` with a preceding `OPSUB t,1` prints 0) | CONFIRMED: `TupMakeCaseListNext` 10bf2350; `case 2: case 3:` prints one line `1 2`, which settles that +0xc/+0x10 is a range and not a 64-bit value (`ilx_cc.c` c3) |
| `L <tuple> $out` | LABEL tuple (+0x24 symbol) with the symbol's name when it has one: user labels, `__catch$f$0`, `__unwindfunclet$f$2` | CONFIRMED (trace) |
| `P <tuple> <opcode> <word>` | every stream tuple that is neither BLOCK, LABEL nor instruction (kinds 23 PRAGMA, 24 ENTRY, 25 EXIT, 28 SPLITPOINT), in stream position; word = tuple +0x20. `OPUSERLABEL` adds the label's name. `OPBLKSTART`/`OPBLKEND`: a matching pair carries the same word; it is non-zero exactly for blocks that declare a variable (a user local or the compiler's return-value temporary) | CONFIRMED (trace) for the pairing and the zero/non-zero rule; that the word points at the block's symbol list is INFERRED (`[[word+4]+4]` was the first local's name in 6 of 7 samples) |
| `eh{n=2 fl=1}` on `OPDTORACTION` / `OPPOPSTATE` | EH record at tuple +0x30 (0x18 bytes): +0 n = number of states the action unwinds, +8 flags | CONFIRMED: reader at 10bcf2a4..10bcf2da; `n` equals the number of live destructible objects (plus the catch state inside a handler) at every exit of `ilx_eh.cpp` and `eh_states.cpp`. Flags seen: 0x21 = the action at the textual end of a scope (no `OPPOPSTATE` follows), 0x01 = an early exit (return, break, continue, goto), 0xa1 = the `OPPOPSTATE` that closes a scope whose end is not reached by falling through; the bit names are INFERRED |
| `eh{fl=1 ...}` on `OPPUSHSTATE`, then `A` lines | record +0 = the unwind action as a detached tuple stream (reader form), +4 = ehinfo (0 until EH canonicalisation), +8 flags (1 = destructor state, 0x101 = the `OPPUSHSTATE <- 0` of a catch handler). After canonicalisation +0 is 0 and the action is the funclet body at `[[[ehinfo+0x34]+8]+0]+0x1c`. The `A` lines are that stream (at most 64 tuples) | CONFIRMED: `RdrOpPushState` 10bd0dd0, `EhFreeOPPUSHSTATE` 10c6c5e1 (same path), trace |
| `state=1 to=0 kind=1` | ehinfo +0x14 = the state number, +0x18 = the state it unwinds to, low byte of +0x10 = kind (1 destructor state, 6 try and catch-handler states). Printed when record +4 looks like an ehinfo (kind 1..15, small state) | CONFIRMED (trace): the numbers equal the `OPNEWSTATE` constants, the `$N` of `__unwindfunclet$f$N` and the listing's `__unwindtable` (`eh_states.cpp` s1: 0/-1, 1/0, 2/1; s3: try 0, x 1, handler 2, y 3). The validity test is a heuristic |
| `eh{flag=1 state=3 to=2 kind=1 next=2}` on `OPNEWSTATE` | record +0 flag, +4 ehinfo, +0xc ehinfo of the state being entered. flag 1 = the store that leaves `state` for `next` just before the destructor call; flag 0 with an ehinfo = the store that enters `state` (for a kind-6 ehinfo: the store that leaves the try) | CONFIRMED (trace) as observed pairs; the meaning of the flag is INFERRED |
| `eh{obj=_e type=??_R0H@8 fl=8}` on `OPCATCH` | reader form: +0 catch variable's symbol, +4 type descriptor symbol, +8 flags (8 seen for `int&`, 0x40 for `catch (...)`). After canonicalisation the record is printed raw as `w=a,b,c` plus the handler's `state=`, and `try=<ptr>` (+0xc), which `OPEHTRY` carries too | CONFIRMED (trace, `ilx_ops.cpp` f11); flag meanings INFERRED |

Things to know when reading it:
- **At `@MscRVO@4` the cc of an unsigned compare is the signed one** (`(unsigned)a >= (unsigned)b` branches away
  on `cc=l`); the operand types of the `OPCMP` (`:2004`) carry the signedness and c2 switches to `b` later
  (`_jcc ; cc=b` at `@EncEmitFunc@4`). `a + b < 0` is `cc=ge` on the reader's `OPCMP` and `ns` after lowering.
  CONFIRMED (`ilx_cc.c` c1).
- `sN`/`dN` count the operands as printed, from 0, including the mirrored address registers and `pas` sets.
- Tuple addresses are stable for the life of a tuple, so `Ld17478` at one pass and `L d17478` at another are the
  same label; a pass that frees a label can leave a note pointing at a tuple the dump no longer lists.
- The funclet body under an `OPPUSHSTATE` is lowered early: at `@dop2_Codegen@4` the `A` lines already read
  `lea ecx, obj / jmp ??1D` while the function itself is still IL. CONFIRMED (`eh_states.cpp` s3).
- `--grep` matches `I`, `A`, `C` and `P` lines; `--count` counts `I` lines only (so the numbers are the same as
  before the notes existed, unless the regex matches a note).

**`--diff PASS_A,PASS_B[,PASS_C]`** (the former `work/scratch/isel/passdiff.py`) dumps at the listed passes and
prints, per function, a unified diff between each consecutive pair with tuple addresses, line and length fields
removed from the instruction lines (`--addr` keeps them, to see which tuple object moved):

```
$ uv run tools/c2trace.py il work/scratch/ilx/ilx_cc.c --func c3 --diff @MscRVO@4,@dop2_Codegen@4
#### _c3: @MscRVO@4 -> @dop2_Codegen@4: 45 -> 35 lines
-I OPASSIGN | t64:1004 <- _a:1004
-I OPSUB | t71:1004 <- t64:1004 1:1004
-I OPASSIGN | t64:1004 <- t71:1004
-I OPCMP | t72:9000 <- t64:2004 11:2004
+I lea | t137:1004 <- [_a:1004+-1]:1004 _a:1004 ; s0=&
+I OPCMP | t72:9000 <- t137:2004 11:2004
 I OPBRANCH | <- s0:0 t72:9000 -> ; cc=g s0=Ld147a8
```

A function compiled several times at a pass (inline expansions, funclets) is diffed occurrence by occurrence
when the counts agree, otherwise last against last, with a line saying so.

**Other c2 builds.** The extended dump is used only when the build table's `il_spec` ends in `%+`; the 13.10 and
15.00 tables end in `%` and keep the plain dump. To port it, check these in that build and add what differs to
the table's `dll` section (`set` directives; defaults are the 14.00 values): `ext` (0x30: the per-kind word
after the operand lists: QUESTION cc, INTRINSIC number, SWITCH default with the case list at ext+4, EH record
pointer), `labtup` (0x33: label symbol → LABEL tuple), `brcc` (0x0a: BRANCH cc nibble); the existing `blk` is
also used for the +0x20 word of `P` lines. Not parameterised (edit `dumpilx` if they differ): operand lists at
+0x28/+0x2c, leaf payload +0x18, kind numbers, the CASE node {+0xc, +0x10, +0x14}, LABEL +0x24, the name pointer
at symbol +4, the EH record {+0, +4, +8, +0xc} and ehinfo {+0x10, +0x14, +0x18, +0x34}, and the IL opcode numbers
0x2db..0x2e4 / 0x300 that select the EH and `OPUSERLABEL` decoding (13.10's IL opcodes are numbered differently).

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

### `scratch --why`, `--export` and `rasim scratch`: predict the picks, ask what an edit shifts

```sh
uv run tools/c2trace.py scratch tu.cpp --func F --why [--all] -- /TP /MT /GS /EHsc /Gy    # what RegAssign saw
uv run tools/c2trace.py scratch tu.cpp --func F --export s.json -- /TP /MT /GS /EHsc /Gy
rasim scratch s.json [-v] [--run K]      # replay the pick rule: predicted vs traced, every run
rasim scratch s.json drop:13             # what-if: pick 13 does not happen
rasim scratch s.json ins:9 ins:9:2       # a new temp picked before pick 9 (the second one live over 2 more picks)
rasim scratch s.json nopref:2 pref:13:eax
```

The rule is regalloc.md §6. `--why` adds to each row the cursor after the pick (`cur>next`), the **candidates**
(which of eax ecx edx were free in `_RegMap` and outside the temp's conflict set) and, for every pick that is not
simply "the cursor's register by rotation", the reason in brackets: `preferred eax (source register of this
lea)`, `preference edx refused: busy phys`, `eax busy phys` (a coloured variable or a value Color placed holds
it), `eax busy t162` (an earlier temp still live), `ecx conflict` (free now, but in the temp's conflict set: it is
needed before the temp dies). `--all` lists every `RegSimpleAssignBody` run (EH funclets, then the body); the
default is still the body only, and the default table is unchanged. These options are 14.00-only (15 hooks: the
4 of the plain table plus `_RegMap`/`regAvail`/cursor/preference at `RegAssign`, `RegResetRegMap`, `RegSpill`,
`RegFixConflict`, and 7 probes inside `RegBuildGlobalConflict` from which the conflict sets are rebuilt). **[C]**

The snapshot (`--export`) has, per run and pick: `tmp`, `cls` (int or the float class), `cur`/`cur_after`,
`pref` and `pref_pre` (set by the conflict builder) and `pref_src` (the earlier temp whose register the
preference copies), `how` (ROT / pref / fit), `reg`, `busy` (register → `phys` or the temp holding it), `avail`,
`conf` (registers the temp conflicts with), `cand`, `why`, `off` (listing offset) and the instruction.

`rasim scratch SNAP` replays every run with its own cursor and its own temp → register map and prints one
summary line per run and a total, e.g. `scratch: 63 of 63 integer picks predicted (register and how); cursor
right at 63 of 63; 0 not modelled; 16 non-integer picks skipped; 5 runs`; a miss is listed with both sides and
the model's reason. Results: eA 7/7, eB 6/6, 0x00e0a0b0 23/23, 0x00c7e9a0 63/63 (regalloc.md §6.5). **[C]**

With edits it prints the picks whose register changes, with their listing offsets:

```
$ rasim scratch e2.json drop:9
    9    +0x8d  removed (was ROT eax)  lea t312:1004 <- _p3:1004
   10    +0x92  ecx -> eax  (ROT -> ROT; cursor ecx -> edx)  [edx busy (coloured variable)]  lea t313:1004 <- _p2:1004
   ...
   21    +0xe5  edx -> ecx  (ROT -> ROT; cursor edx -> ecx)  lea t311:1004 <- _p0:1004
what-if: 12 of 23 integer picks change register (last: pick 21); final cursor eax (as traced)
```

The shift stops at pick 29, which has a single candidate. That is the general pattern: a changed pick count
shifts the rotation only up to the next forced pick. What the what-if holds fixed: Color's result, the conflict
sets and the other temps' lifetimes (all independent of scratch picks in c2 itself, but a source edit can
change them; re-trace to confirm). An inserted temp is assumed to have no preference and no conflicts. Checked
against real compiles on a straight-line function: `drop:2` 9/9 and `ins:5` 8/8 later registers. **[C]** Runs
without conflict sets (EH funclets) and `RegSpill`/`RegFixConflict` are flagged, not modelled.

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
rasim check  snap.json --predict [-v]                     # spill / defer / split decisions predicted, per round
rasim blocks snap.json --pass 2 scene this                # per-block terms side by side
rasim order  snap.json --pass 2 var:nm1:12                # the colouring order after an edit
rasim need   snap.json --pass 2 var:nm1:12 'scene>this'   # which single edits (then pairs) restore the order
rasim regs   snap.json [CHANGE|EDIT...]                   # every colouring's register per the ChoosePhysReg model
rasim need   snap.json reg:this=esi reg:center=ebp        # which changes (source-level edits first) give these registers
rasim want   snap.json regmap.txt                         # the same, targets read from `c2trace.py regmap` output
```

The snapshot holds every Spillcosts pass: its class, each block's w and true P, and each uncoloured globreg's
per-block terms, plus the actual pops. rasim applies the Spillcosts formula and the tie-break (regalloc.md §3.2,
§3.4) to that block × globreg matrix. At pass k the list is pass k's new pieces plus every earlier-costed globreg
of the class that was neither popped nor replaced by split pieces; those keep the priority they were costed with.
Edits are what source changes do to the IL (regalloc.md §7):
- `ref:VAR:B[:C]`: a reference in block B;
- `unref:VAR:B`: no reference, still live through B;
- `drop:VAR:B`;
- `extend:VAR:B`: the range grows to reach block B (a reference there, the blocks in between lived through);
- `var:NAME:B[:C],..`: a new globreg, e.g. a named local;
- `w:B:N`, `P:B:N`.

A new reference changes P_B, so it moves every other variable in the block too. `need` only proposes references
in blocks where the variable is already live (elsewhere the edit would also stretch its range, which isn't
modelled), and runs in milliseconds.

**Which pass `need 'A>B'` and `blocks` answer in.** With `--pass N`: that pass. Without it they follow the
variables named: the register class those variables are costed in (int, x87, XMM: each class has its own
priority list), and in it the pass in which the first of them is popped, provided all of them are in that pass's
list; otherwise the first pass that has them all. When that is not the first integer pass, the choice is printed:

```text
$ rasim need sh0.json 'local6079#169a840>dq0#1c8f500'
pass 31 (x87 class) chosen from the variables named: the first of them is popped in it; --pass N overrides
  local6079: x87 class, costed in passes 29, 32, 57
  dq0: x87 class, costed in passes 29, 30
local6079#169a840 (4026, def 15771) before dq0#1c8f500 (6648, def 15731): slack -2622  (VIOLATED)
```

Variables of different classes are never compared by c2, so that is an error, not an answer from one of the two
passes (`these variables are in different register classes ...`, with each variable's class and passes); so is a
name no pass costs. Before this, the default was always the first integer pass, where an x87 or XMM name is
either missing or, worse, another value of the same name. The snapshot must hold the classes asked about, which
any `regs --export` does (the export always has every class; `--all-classes` only changes the printed report).
**[C: `default_pass` in tools/rasim/src/main.rs; the example is Spore's RotateSH]**

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
- **Ties.** Equal prio: the larger defOrd first (`regs`' `def` column; within a block the earlier def, across
  blocks the def in the later block). **Exact ties** (equal prio and defOrd) follow insertion order backwards
  (regalloc.md §3.4): bucket descending in the initial list; later split rounds first and higher web number
  first among split pieces. Deferred webs (benefit ≤ 0, InsertByBenefit) are outside both rules, see above.

### Register choice (`rasim check`, `regs`, `need reg:`)

`check` also replays ChoosePhysReg (regalloc.md §4; code `tools/rasim/src/choose.rs`) and prints a second line:

```text
registers: 26 of 26 int colourings predicted from the simulated state (26 of 26 with the traced inputs); 0 state divergences
```

- *simulated state*: each globreg starts with the avail set it has when InsertInList first queues it; its PREFs
  are the Spillcosts events; from there the model removes each chosen register from the neighbours' avail sets and
  adds the partner preferences itself. The register is predicted from that state.
- *traced inputs*: the formula applied to exactly what ChoosePhysReg read (a check of the formula alone).
- *state divergences*: places where c2's avail set or PREF list differs from the model's (re-queues and every
  neighbour sighting are compared too). After a divergence the replay continues from the traced state.

Trace facts, not predictions: the initial avail sets, the neighbour list and partner list of each colouring, the
Spillcosts preference events, and which globregs are split, spilled or deferred.

Results **[C]**: every colouring predicted with 0 divergences on eh_helpers eA/eB/nA/nB (6, 7, 4, 4), 0x00e0a0b0
and 16 source variants of it (26 or 27 each), 0x00e0a950 (32), 0x00e0a550 (22), 0x00c7e9a0 v_match (77) and the
26k-instruction synthetic b250.c (1360).

`need reg:VAR=REG ...` runs the model free (its own registers feed the later choices) under every single change
and lists those that give every target its register, with the other registers each one moves. Source-level edits
(`ref:`, `unref:`, `drop:`, `extend:`) are tried first; they are described under "Combined what-ifs" below. The
abstract changes are:

| Change | Meaning | Source-level lever |
|---|---|---|
| `before:A:B`, `after:A:B` | A is coloured just before / after B | a priority change: `rasim need snap 'A>B'`; the gap shown is the priority difference to close |
| `interf:A:B`, `nointerf:A:B` | A and B interfere / do not | overlapping or separating two live ranges |
| `pref:A:REG[:N]` | N more preferences (N < 0: fewer) | a copy with a fixed register: a call result (eax), `return` (eax), a thiscall/fastcall argument (ecx, edx) |
| `copy:A:B`, `nocopy:A:B` | A and B are / are not copy partners | `a = b` that survives to Color as a `mov` |
| `noreg:A:REG`, `canreg:A:REG` | A cannot / can have REG | A lives (no longer lives) across something that overwrites REG: a call (eax ecx edx), a shift count (ecx), a division (eax edx), an argument loaded into ecx or edx |

`regs snap CHANGE...` prints the whole assignment under given changes, with each colouring's candidates and
weights. VAR is a name, or `name@pN` for the piece coloured as pop N.

What the model cannot decide is said in the output:
- `[I: n inferred relations]`: the change colours A before B although the trace had B first. c2 records "B is a
  neighbour (partner) of A" only while B is uncoloured, so the model takes it from "A was a neighbour (partner) of
  B". For interference this held in the one case tested (below); for copy partners c2's rule is not symmetric
  (regalloc.md §4.3), so treat those as hints. A move across a Spillcosts round is also flagged: the splits are
  replayed as traced.
- `not decidable`: a web is left with no register (c2 would split or spill it), or a piece would be coloured
  before the split that creates it.
- A change is applied to the choice only. The source edit that realises it also changes priorities, P and
  possibly live ranges; re-trace.

Tested against a real compile **[C, one case]**: work/scratch/f3/rw/a2.cpp and a3.cpp differ by one statement that
keeps `this` alive after a call, which raises its priority from 52 to 92 (pop 24 becomes pop 16, across the split
round after pop 17). `rasim regs a2.json before:this:t350` gives the registers, candidate lists and weights of all
26 colourings of the a3 trace (`this` esi with esi:0 edi:0 ebp:0, `center` ebp, `quadrant` edx:0 ebx:200, ...),
using 5 inferred neighbour relations. Interference, preference, copy and source-level edits are tested against
compiles under "Combined what-ifs" below.

Answer for 0x00e0a0b0 (e2.cpp; `this` has ebp and `center` esi, the original has the reverse),
`rasim need e2.json reg:this=esi reg:center=ebp`. `center` (pop 21, prio 74) is coloured with esi:0 ebp:0 and
takes esi by register order; that removes esi from its neighbour `this` (pop 24, prio 52), which is left with
ebp. Any one of these, each with no other register moving:
- order: `this` coloured before `center` (close the gap of 22; this is `need 'this>center'` above, and what a3
  does);
- interference: `this` with the `mid` piece coloured ebp at pop 17 (`this` then has only esi left, so `center`
  sees esi:+800 and takes ebp); or `center` with `outA` or `t332` (both esi), so that esi is gone for `center`;
- preference: `this` (or `quadrant`, `context`) prefers esi, which costs `center` esi:+1; `copy:outA:this` or
  `copy:t332:this` is the same thing through a copy with a value already in esi.

Known limits:
- `SpillObvious` (10b11560) is not modelled by the what-if commands (`check --predict` does model it, below). A
  globreg that is never popped and never split is treated as removed (trace fact), so an edit cannot add or undo
  such a removal. For `var:` edits this means a new value is assumed
  to survive; a real named local with 2 references is usually folded away (`CmdSubstDef`). For the *other*
  variables' priorities only the new value's presence in P matters, and an immediate candidate (cost 0, leaves no
  code: frontend.md) is the cheapest way to get it.
- Splitting is not simulated by the what-if commands. `check --predict` predicts *which* webs are split and when;
  the pieces are replayed. An edit that changes the colouring order can change later splits; re-trace to confirm.
- `order` and `need 'A>B'` answer about priorities only. `regs`, `need reg:` and `want` carry an edit through
  both models (next section). Which register each value *should* have comes from `regmap` (below).

**Speed of `need reg:`.** Every candidate change is one run of the register model; a function with n coloured
values has about 2·n² of them (415 globregs: about 180,000 runs of 2.7 ms). They are now evaluated on all cores
(`par_map` in combo.rs; `RASIM_THREADS=N` to limit, 1 = serial; the report is in the same order), and a change
between two values that are both coloured after the last target is not run at all (it cannot reach a target;
it is still counted as tried, and is run if picked for a pair; with such changes skipped, an undecidable one
among them is no longer counted in the "not decidable" lines). `need reg:t261=esi` on the 415-globreg
snapshot of M&B `CharSheet::init_screen`: 9 min 08 s before (one core, 489 s CPU), 1 min 16 s to 1 min 34 s after
on a 10-core machine that was busy with other compiles (load 10 to 30). Not done: restarting each run from a
saved state at the first affected colouring and stopping when the state is the base's again, which is where the
remaining factor is. `need 'A>B'` (priorities) was and is a few milliseconds. **[C]**

### Combined what-ifs: source-level edits in the register model, pairs, `want`

```sh
rasim check snap.json                                  # third line: can edits be simulated for this snapshot?
rasim regs  snap.json ref:this:12                      # every register after "one more reference to this in block 12"
rasim regs  snap.json extend:p0:1 pref:x:eax noreg:y:ecx   # edits and abstract changes mix freely
rasim need  snap.json reg:this=esi reg:center=ebp [--top K] [--pool N] [--depth 1|2|3]
uv run tools/c2trace.py regmap tu.cpp --func F --asm-vs orig.s --asm-func DECORATED --all -- /TP /MT /GS /EHsc /Gy > regmap.txt
rasim want  snap.json regmap.txt                       # smallest change sets that give every wanted register at once
```

Code: `tools/rasim/src/combo.rs`. Blocks are numbered as `regs --why` numbers them (the first int Spillcosts pass).

**What an edit does in the model.** **[C]** for the formula parts (regalloc.md §3.2, §4), **[I]** where marked:

| Edit | Priority | Benefit | Overlaps and registers |
|---|---|---|---|
| `ref:V:B[:C]` | cost +C (default 2) in B; P_B += 1 for everyone if V was only live through | += C·w_B | unchanged |
| `unref:V:B` | V's cost in B goes, V pays the live-through penalty; P_B -= 1 for everyone | −= cost·w_B | unchanged |
| `drop:V:B` | B must be an end of V's range; the range shrinks to the nearest block that still has a reference | −= cost·w_B | V stops interfering with the values it met only there; it gets back the registers killed in the blocks it lived through **[I]** |
| `extend:V:B` | a reference (cost 2) in B, a penalty for every block in between | += 2·w_B | V interferes with every value present in a block it now lives through or live across a block boundary it now crosses **[I: block granularity]**; V loses the registers killed in the blocks in between **[I]** |

- **Order.** The colouring order is recomputed from the edited priorities with the sort rule of regalloc.md §3.4.
  Round by round: a split round still starts after the pop that started it in the trace **[I]**, so a value whose
  priority rises above that pop is coloured in the earlier round (the a2/a3 case). A value the trace split
  before its turn is coloured whole if it now outranks the split **[I]**.
- **Killed registers** of a block are estimated from the other values **[I]**: the registers no value living
  through the block may use; if no value lives through it, which of eax ecx edx a value spanning it lacks.
- **An extended value that runs out of registers** before its turn is split by c2. The model continues with one
  piece over the extension: the value's initial set minus the register just taken and the registers of the
  coloured values the extension overlaps **[I, fitted on the Fallout 3 case below]**. A value that has no register
  at its turn is reported as split, with its pieces not modelled.
- **Not modelled.** Priorities of split pieces (a later Spillcosts pass visits a subset of the blocks and numbers
  them from 1, so an edit is applied to the first pass only); new splits; the register of a new variable (`var:`
  only raises P); preferences an edit creates (give `pref:` / `copy:` / `noreg:` with it).

`rasim check` prints a third line saying whether the order recomputed from the unedited priorities is the traced
one. If not, edits are refused and only abstract changes are searched. It holds **[C]** on eh_helpers eA,
0x00e0a0b0 (e2, 4 rounds), 0x00c7e9a0 v_match (12 rounds), b250.c (34 rounds) and the probes below.

**The search.** `need reg:` and `want` try every single change: edits on every source variable (not on `imm0`
or `t123` temporaries, which no source edit addresses; name them explicitly if wanted), then the abstract kinds.
If no single source-level edit works they try pairs of the `--pool` (160) most promising single source-level
edits, ranked by targets missed, how near the misses are, and cost. If no single change of any kind works, the
pairs are taken from all kinds, and then triples are tried (the 40 best pairs, each with one of the 60 best
singles). Answers are listed with the fewest other
registers moved first, and each says which other registers move. `--depth 1` stops at singles. When nothing is
found the three closest attempts are printed with the targets they leave unmet. Run time: 0.05 s on 0x00e0a0b0,
2 s on 0x00c7e9a0 with 67 targets.

**`want`.** The file is `regmap` output. A `WANTS r` row is a target for every coloured piece of that variable
that now has the row's register; `WANTS r for q` for the pieces that have q; with `regmap --all` every `ok` row
pins that variable's registers, so an answer cannot move them; `mixed` rows and scratch temporaries are skipped
and listed. Lines of the form `VAR=REG` are accepted too.

**Fallout 3 0x00a89680** (e2.cpp has `this` ebp and `center` esi; the original has the reverse).
`rasim want e2.json e2_regmap_all.txt` (2 wanted, 24 pinned) and `need e2.json reg:this=esi reg:center=ebp` give,
each with no other register moving:
- `ref:this:1`, `ref:this:4` or `ref:this:12`: one more reference to `this` in the entry, the loop head or after
  the loop (prio 52 → 78, 80, 92);
- `unref:center:4` or `unref:center:10`;
- `extend:p0:1` (also to block 2, 3, 6, 7 or 9): "p0 range 12..12 -> 1..12, now overlaps this context center in p3
  cur quadrant". p0 then has only ebp left from pop 9 on, runs dry when `mid` takes ebp, and its piece has esi
  and edi; `in` takes edi, so `center` sees esi:+400 and takes ebp, and `this` gets esi. This is what
  work/scratch/f3/surv/match_min.cpp does: the second callee gets a body that only reads `*p0`, so p0 is one web
  from the entry to its use after the loop. The traced match has the same chain (p0 prio 64 as predicted, the
  piece queued with esi edi, `center` esi:200 ebp:0); only the weight differs (the piece's benefit is 2, the
  model keeps 4).
On the by-value draft of 0x008e7d40 (6 wanted, 61 pinned, 10 rows skipped) nothing up to triples gives all six;
the closest single change leaves one unmet. The original splits `this` there, which no change in the model does.

**Against real compiles [C].** `uv run python work/scratch/rasim_combo/pairs.py [-v]` compiles 15 pairs (a base
and a variant that differs by one source edit), gives rasim the base trace and the edit, and compares every
predicted register with the variant's trace (values matched by name; temporaries only when both sides number them
alike). Probe sources: work/scratch/rasim_combo/src/probes.cpp and the Fallout 3 files.

| Pair | Source edit | Given to rasim | Really moved | Predicted |
|---|---|---|---|---|
| f3_p0 | e2 → match_min (callee body that only reads `*p0`) | `extend:p0:1` | this, center | 25 / 25 |
| f3_this | a2 → a3 (`this` kept after the second call) | `ref:this:12` | this, center | 14 / 14 named |
| ord_ref | `s += g(y)` → `s += g(y) ^ y` | `ref:y:4` | x, y | 3 / 3 |
| ord_unref | `s = g(x)` → `g(x)` | `unref:s:2` | x, y | 3 / 3 |
| ord_pair | three stores added in block 1 (a pair-search answer) | `ref:y:1:4 ref:x:1` | y, s | 3 / 3 |
| int_ref | `gy = x;` added (a `need` answer) | `ref:x:3` | x, y | 6 / 6 |
| int_ext | `+ w` at the end: w now crosses two calls | `extend:w:7` | none | 5 / 6 |
| int_drop | the reverse | `drop:w:7` | none | 6 / 6 |
| iv_ext | `+ w` at the end, w the most referenced value | `extend:w:5` | w, x, a, b | 5 / 5 |
| iv_drop | the reverse | `drop:w:5` | w, x, a, b | 5 / 5 |
| fc_pref | `fc(x, y)` → `fc(y, x)` (fastcall) | `pref:x:ecx:-1 pref:x:edx pref:y:edx:-1 pref:y:ecx canreg:x:edx noreg:y:edx` | x, y | 4 / 4 |
| cp_copy | `y = x; gy = u;` → `y = u; gy = x;` | `copy:u:y nocopy:x:y interf:x:y nointerf:u:y` | x, u, a | 7 / 7 |
| h_ext_ref | `gz = x;` added (a `need` answer) | `ref:x:1` | x, s | 5 / 5 |
| h_f3_loop | e2 with `g_keep = this;` in the loop head (a `need` answer) | `ref:this:4` | this, center | 14 / 14 named |
| h_ret | `gy = y; return x;` → `gy = x; return y;` | `pref:x:eax:-2 pref:y:eax:2` | x, y | 4 / 4 |

109 of 110 registers; in 14 pairs the values that move are exactly the predicted ones. How to read this:
- The `h_` pairs were written after the model was frozen. h_ext_ref and h_f3_loop were right on the first run.
  h_ret was first given as `pref:x:eax:-1 pref:y:eax` and predicted no change (2 of 4): the `return` copy exists
  twice at Color (the tail is duplicated into both branches), so x holds eax:2. The base trace shows it (x's
  weights column has eax:-2); count the copies there before writing a `pref:` change.
- The other pairs were used while building the model. Three things were fitted on them: the split-piece rule
  (f3_p0), the kill estimate (iv_drop), and the comparison by register set when a value is coloured in a
  different number of pieces than traced (iv_ext, iv_drop).
- **A preference rarely comes alone.** fc_pref with the four `pref:` changes alone predicted 2 of 4: loading an
  argument register also takes that register from every other value live at that point (regalloc.md §3.3.3),
  hence `canreg:x:edx noreg:y:edx`. cp_copy with the two copy changes alone predicted 4 of 7: the copy's source
  does not interfere with the destination at that def, the other value does, hence `interf:x:y nointerf:u:y`.
  rasim does not derive these companions from a source statement; they have to be given.
- The one miss, int_ext: w has no register left at its turn, c2 splits it and gives the pieces eax; the model
  says "c2 splits it; its pieces are not modelled" and predicts the other five.
- All probe functions are small (3 to 11 globregs) except the Fallout 3 one (29). `extend:` and `drop:` are
  checked on 5 pairs only, at block granularity; an overlap inside one block cannot be expressed.

Answers on the two Fallout 3 functions (millisecond runs):
- 0x00a89680, `need 'this>center'`: any one of
  - one more reference to `this` in block 1 (the entry, P=13) or block 12 (after the loop, P=20);
  - dropping `center`'s reference in block 10 or 12.
- 0x008e7d40, `need --pass 2 var:nm1:12 'scene>this'`: with the site-1 variable in place, any one of
  - one more reference to `scene` in a block where it is live: 27 (P=13), 36 (P=10), 30 or 35 (P=8);
  - one fewer reference to `this` in block 1, 2 or 6 (initialize's `name` handling).

### `rasim check --predict`: spill, deferral and split decisions predicted, not replayed

```sh
uv run tools/c2trace.py regs tu.cpp --func F --export snap.json -- /TP /MT /GS /EHsc /Gy
rasim check snap.json --predict        # per-round table, the first misses
rasim check snap.json --predict -v     # every miss, every round on its own line
rasim check snap.json                  # fourth line: "decisions: N of M predicted ..."
```

`check` replays the order and the registers with the splits, spills and deferrals taken from the trace.
`--predict` runs Color's loop as a model instead (tools/rasim/src/predict.rs; rules in regalloc.md §3.6): it keeps
its own priority list (InsertInList and InsertByBenefit node for node), its own benefit, priority, deferred flag,
avail set and PREF list per globreg, and at every pop decides what c2's code decides:

- which web is at the head of the list;
- the branch: SpillOrDefer, no register left, or colour;
- in SpillOrDefer: pin to the preferred register (RestrictToReg), defer, spill, or cut into single-block lives,
  and which other deferred webs are cut in the same call;
- the register;
- after a colouring, which neighbours are left without a register (they are split);
- whether a split round starts before the next pop;
- at the start of each round, which new globregs SpillObvious spills, and the order the rest enter the list.

Each decision is compared with the trace. After a miss the model is put back on the trace, so every decision is
scored against a correct history and one miss is counted once.

```text
predict: ?FUN_00e0a0b0@QuadSplitter@@...: 29 int pops in 4 rounds; each decision is predicted from the model's own state and checked against the trace
  round    new   list head      branch spill/defer    register    no-reg   cut set split round SpillObvious ins order
      1     31       17/17       17/17           -       17/17       1/1         -         1/1        34/34     31/31
      2      2         7/7         7/7         1/1         6/6       1/1         -         1/1          2/2       2/2
      3      1         1/1         1/1           -         1/1       1/1         -         1/1          1/1       1/1
      4      0         4/4         4/4         2/2         2/2         -         -           -          1/1         -
    all     34       29/29       29/29         3/3       26/26       3/3         -         3/3        38/38     34/34
  SpillObvious spills: 4 of 4 predicted; RemoveSplitGlobregs sets consistent with the marks: 3/3
  split rounds: 3 of 3 are started by a decision of the model alone (...); 0 by _NeedNewLives (...), 0 only by InsertSplitPoints' replayed set (...)
  replayed from the trace (need the IL): the 3 pieces of 3 split rounds; the neighbour and partner lists of 26 colourings; ...
  state: 0 places where the model's record differs from c2's (it is then taken from the trace)
```

Columns: `new` = globregs entering the list in that round; `no-reg` = colourings after which a neighbour has no
register left (the set must match); `cut set` = the deferred webs cut together; `split round` = traced rounds the
model also starts, `+N` = rounds only the model starts; `ins order` = the insertion order follows the web-number
rule (regalloc.md §3.4). Rounds with one pop and nothing new are folded into one line unless `-v`.

**Results [C]**: every decision as c2 made it on the validation set (eh_helpers eA and eB, 0x00e0a0b0 in 4 rounds,
0x00c7e9a0 in 12 rounds, b250.c in 34 rounds: 3933 pops, 2573 spill/defer decisions, 33 round starts) and on every
function of 15 traced compiles (257 functions: list head 4401/4401, spill/defer kind 2054/2054, register
2338/2338, round starts 270/270, SpillObvious 5085/5085; /O1 and /Oy- included). The per-round tables are in
regalloc.md §3.6.8.

**What is still replayed, and why.** Each of these needs the instructions, which the snapshot does not hold:

| Replayed | c2 function | What it would take |
|---|---|---|
| the pieces of a split round: how many, their blocks, costs, benefit, avail set, PREFs | ColProcessSplitGlobregs, Spillcosts | the split points (below) and per-instruction liveness |
| the neighbours and copy partners of each colouring | InsertSplitPoints, `_UpdatePrefsBv` | per-instruction liveness |
| InsertSplitPoints' own split set (webs cut for register pressure) | InsertSplitPoints | the live count at every instruction |
| byte-register restrictions of queued webs | InsertSplitPoints, CmdProcessBytePressure | the same, for byte values |
| whether a def is substituted, folded or re-queued, and BetterToDefer's priority | CmdSubstDef, FoldAwaySplitGlobreg, ProcessFoldableDefs, BetterToDefer | instruction patterns, the def's components |
| benefit changes after a spill, `_NeedNewLives` | CmdUpdateCost, Spill | which globregs share an instruction with the spilled one |

So `--predict` answers "given the webs of each round, what does Color decide", which is what was replayed before;
it does not answer "what pieces does this split make". A what-if that changes which webs are split (`rasim regs`
with an edit) therefore still stops at the split and says so.

**Snapshot fields** (added by `regs --export` on the default c2 build; older readers ignore them, and a snapshot
without them makes `--predict` say so and leaves the fourth `check` line out):

- `decisions[]`: one entry per event, in order, with `npass` and `after_pop`. `t` is `so` (SpillObvious looks at a
  new globreg: `nrefs`, `ben`, `lastop`), `spill`, `subst` (CmdSubstDef; `ok` = folded), `remove` (a record is
  retired), `restrict` (`ret` 1 = pinned), `force1` / `forceair`, `btd` (BetterToDefer; `ret`), `fold`
  (FoldAwaySplitGlobreg; `ret`), `mark` (`fl5 |= 2`; `site` = `isp` for InsertSplitPoints' set, `nbr` / `nbr8` for a
  neighbour without a register, `needlives` for a web whose user was deleted by a spill), `recalc`, `bencost`
  (CmdUpdateCost: `delta`), `bytepress` (`regs` = the registers the web is restricted to).
- `inserts[].x`, `pops[].x`: the GLOBREG record when it enters the list and when it is popped: `fl5`, `fl6`,
  `nrefs`, `single` (firstBlk == lastBlk), `nenter`, and for inserts `prio`, `defOrd`, `name`, `kind`; for pops
  `avail_empty`, the last def's opcode and `Opcode_dope` word, and `_Speed`.
- `split_rounds[]`: every RemoveSplitGlobregs set, also of a round whose Spillcosts pass walks no block (such a
  pass has no entry in `passes`); `npasses`: the number of Spillcosts calls.

Hooks (`DEC_HOOKS` in tools/c2regs.py, 22 of them, after the choice hooks): a probe in SpillObvious' loop
(10b115b2), `@Spill@8`, `@CmdSubstDef@12`, `@ColRemoveGlobregFromHashTable@8`, `@RestrictToReg@4`,
`@ForceSingleBlockLives@4`, `@ForceNoAirlocks@4`, `@BetterToDefer@4`, `@FoldAwaySplitGlobreg@4`, the three
`fl5 |= 2` sites of UpdateAffectedGlobregs (10b156de, 10b158aa, 10b15ab2) and the one in Spill (10b1788f), a third
probe in InsertInList (10b1197d), a probe after the pop (10b0d350), `@TryToRecalc@4`, the four `benefit +=` sites
of CmdUpdateCost (10c2435a, 10c24387, 10c243c2, 10c243f9), and the two byte-pressure sites (10c25147, 10b148ec).
The `regs` report itself is unchanged.

**Limits.**
- Integer class only. /O1 is handled (`_Speed` is in the snapshot), but RestrictToReg's load cost at /O1 is
  approximated (2 for an immediate, 3 otherwise; the real values depend on the constant and on whether ebp is
  free). It was right in the 9 /O1 pins traced.
- Never exercised by a trace: a web popped with no register (HandleCurrUncolorable), a web that starts with none
  (OrderGlobregs), RestrictToReg's `nrefs == 3` exception. The rules are in the model; they have no test.
- A state difference seen twice (bitvect.c `SbvAnd`): `fl5 & 4` becomes set while the web is queued. The model
  reports it and takes the trace's value.

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

**Every register class is covered**, on 14.00, 13.10 and 15.00 (the final IL names all of them):
- integer rows first, unchanged, in the format `rasim want` reads;
- then `# XMM values (Color class 6): N checked, M agree everywhere` and one row per XMM value (15.00 writes the
  scalar views `xmm0s`/`xmm0d`/`xmm0l`; they count as `xmm0`), then MMX if any;
- then `# x87 values, by stack position`: the final IL writes `ST(2)=var`, so the "register" of an x87 value is
  the stack position it has at each aligned instruction. A row is a layout check (the original has the value at
  the same depth there, or `WANTS st(1) for st(2)`), not a colour to ask rasim for; why the layouts differ is
  read from `regs --all-classes` and x87.md rules 5, 8 and 11. MASM's `faddp ST(1), ST(0)` and capstone's
  `faddp st(1)` are aligned as the same operand.
- the last lines give the total and, when more than the integer class is present, `# by class: int 64 of 116,
  xmm 325 of 1121`. Swaps are listed per class (the first 12 for a class with more).

`rasim want` reads only integer registers; it skips the XMM and x87 rows (their names are not coloured values of
its snapshot's integer class), so a regmap file with all classes can be passed to it as it is.

Example, `tests/data/fpclasses.c -- /arch:SSE2` against a variant of itself compiled the same way (the variant
swaps two statements of the loop; `work/scratch/regmap_xmm/` has the files and `objasm.py`, which turns an object
file's function into the `--asm-vs` format). cl 15.00, function `_f`:

```text
var (IL)     regs name      ours                     original's votes             verdict
# XMM values (Color class 6): 9 checked, 7 agree everywhere
_s           s              xmm2:3                   xmm1:1 xmm0:1 xmm2:1         mixed (2 of 3 votes differ: xmm2->xmm1:1 xmm2->xmm0:1)
      00031  ours addps xmm3, xmm2                     orig addps xmm3, xmm1
      00034  ours movaps xmm2, xmm3                    orig movaps xmm0, xmm3
_t           t              xmm0:1                   xmm2:1                       WANTS xmm2
      00028  ours mulps xmm3, xmm0                     orig mulps xmm3, xmm2
# 12 variables checked, 10 agree with the original everywhere
# by class: int 3 of 3, xmm 7 of 9
```

Spore 0x00f7d9d0 (`UpdateNormalMapRegion`, cl 15.00, `/O2 /MD /Gy /TP /GS- /arch:SSE /fp:fast`; 8,379
instructions of ours against 8,430): before this, 116 integer variables and no XMM row; now `int 64 of 116, xmm
325 of 1121`. The alignment is `difflib`'s, on mnemonic and operand shape, and this function has nine similar
inlined bodies: only about a quarter of its instructions align, so its rows are evidence, not a verdict. **[C for
the output; I for how much of it to trust on a function that aligns this badly]**

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

### rwsweep: the general rewrite search

`tools/rwsweep.py tu.cpp --func DECORATED [--asm-vs orig.s] [--want 'A>B' --pass K] [--families ...] [--vars v,w]`
does not assume a rule. It enumerates mechanical, meaning-preserving rewrites of the target's own body, compiles
each once with the regs trace, and reports per variant: `shape` (aligned diff against the baseline with registers
masked; 0 = code-neutral), `regs` (the same with registers), `regdiff` against the original and the change of the
`--want` gap. Code-neutral variants that help are then combined and verified. Families:

| family | rewrite |
|---|---|
| `wrap` | one variable use, member chain or call argument through an inline identity helper (by value, by value through a local, by reference) |
| `alias` | `T a = V;` or `T& a = V;` at a statement boundary, later uses in the block renamed |
| `touch` | a do-nothing use of V at a statement boundary (`(void)V;`, empty inline helper, `if (V) { }`, `__assume`, `V = V;`) |
| `cflow` | if/else arms swapped under a negated condition; `while` as `for(;;)`+break, `for(;c;)`, if+do-while or a goto loop; the statement after an if copied into both arms |
| `paren` | redundant parentheses around a right-hand side, return value, call argument, additive term or left prefix (`(a + b) + c`) |
| `guard` | `delete p; p = 0;` <-> `if (p) { delete p; p = 0; }` |
| `swap` | two adjacent independent statements exchanged |
| `split` | `T x = e;` as `T x; x = e;` |
| `reread` | a use of X after `X = p->m;` spelled `(p->m)` again (check by eye: only safe if nothing stores there in between) |
| `recompute` | (not in the default set) `V = E;` with E made of literals and pure math calls (`dq = sqrt(13.0 / 8.0);`) written again before a later statement that uses V after a call: same value, still computed once, but V is one live range per call-free stretch instead of one across the calls (x87.md §10) |
| `const` | a `char`/`short` store of −1.. or 128.. through the type of the other signedness (same byte, another constant candidate); `for (T i = K; ..)` with the init hoisted over 1 to 3 statements, split off, or sunk back (immcand.md §3) |

Result on 0x00a89680 (no EH frame; `this` 52 must reach `center` 74): about 1300 variants, the `this`/`center`
gap moved in none of them (the four loop spellings of `cflow` compile to identical code), and no variant that changed anything was closer to the original. In detail: 722 of the
804 wrap/alias/swap/split variants and all 468 `touch` variants for `this` and `center` compile to the baseline
with the same priorities. So, outside EH frames, c1xx and c2's early passes remove every copy, alias, identity
helper and dead use before Color **[C for this function]**; a rewrite has to change the IL's block structure or a
real use to move a priority here. The match came from outside the function: the second callee's body (frontend.md,
"Applied to 0x00a89680"). rwsweep's value there was closing off the caller's body quickly. A negative result takes about five minutes per 500 variants.

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
