# The tools on the Xbox 360 cross compiler: c2.dll 15.00.8153 (XDK 9328, PowerPC)

The build `15.00.8153-xbox360` of `tools/c2builds/` (aliases `xbox360`, `xenon`, `8153`) is the back end of the
Xbox 360 XDK 2.0.9328 (June 2009): cl 15.00.8153, the Visual C++ 2008 compiler with a PowerPC (Xenon) code
generator. The compiler itself is a 32-bit x86 Windows program, so the tracer patches and hooks it exactly as it does
the desktop builds; only its output is PowerPC. This file records the binaries, how every address was found, what
works, what was measured and what is not ported.

Tags: **[C]** confirmed (read in the code at the cited address, or seen in a trace), **[I]** inferred. Addresses are
RVAs (image base 0x10b00000) unless written as VAs.

## 1. Summary

- `c2trace il` dumps the IL at any of 16 passes, with register names and opcode names (shared IL opcodes from an
  opcode probe aligned with x86 15.00's order; 101 PowerPC machine opcodes named from the listing). **[C]**
- `c2trace regs` reports the register allocator's decisions, and `rasim check` reproduces them: on 14 zlib 1.2.1
  functions (inflate_table, inflate_fast, deflate_slow, send_tree, longest_match ..; 6,782 priority terms, 674 pops)
  0 priority mismatches and every colouring order predicted. The x86 Spillcosts formula (cost·P·w per referencing
  block, −P·w per live-through block, w = 2 per loop level) and pop order (prio desc, defOrd desc) hold unchanged.
  **[C]**
- `c2sched` reports the list scheduler per region (DAG data, issue cycle; two instructions per cycle: Xenon's dual
  issue). On zlib inflate_table the static priority rule holds for 1786 of 1786 nodes with this build's row, and the
  simple selection rule (max (prio, −lex) among ready nodes with est ≤ cycle) predicts 1241 of 1249 picks. **[C]**
- `x360check` compiles one function and compares it with a game image word by word, relocations resolved against
  `FUN_` / `g_` placeholders (REL24, REFHI / REFLO, ADDR32). Seven small Left 4 Dead 2 functions written from
  their code match (engine_360: leaf stores and a global's address, then calls: a call and a virtual call through
  its result, two calls on global addresses, a call that passes its argument through and returns `this`); wrong
  sources are reported at the differing word. **[C]**
- `x360img` turns game files into PE images: STFS packages (XBLA, arcade), XDVDFS disc images, decrypted XEX2 (no keys:
  decrypt first; basic compression), and reads which XDK and compiler build made a game (Rich header). **[C]**
- `symkeys` replays every symbol key (27,233 of 27,233 on o_loop and 7 zlib functions) and lists the commutative
  operand sorts (`sites`): two allocators, five pools, SymInit's frame key (section 6.1). **[C]**
- Not ported (section 7): `scratch` (an x86 pass), the x87 view, `slots`, `regs --why` neighbour /
  preference events (ChoosePhysReg's inputs: 13..17), the scheduler's DAG-edge probe (rule 5), a Xenon issue model
  for `c2sched --verify` rule 7, `lift2il` (x86 only).

## 2. Which binaries

| file | version | sha256 (first 16) |
|---|---|---|
| c2.dll | 15.00.8153 "Microsoft Xbox 360 Compiler Back End" | `54c9b25b057948af` |
| cl.exe, c1.dll, c1xx.dll | 15.00.8153 | |
| link.exe | 9.00.8153 | |

- From `XDKSetupXenon9328.exe` (InstallShield; `7z x` extracts it, nothing is run): `XDK/bin/win32` (the compiler
  and its runtime: msvcr90, msvcp90, atl90, tlbref, pgodb90, mspdbXX, msobjXX, `1033/clui.dll`), `include/xbox`,
  `lib/xbox`. The tech preview `TechPreview/Mar09Compiler` (cl 15.00.8327) runs too, with `WINEPATH` set to
  `XDK/bin/win32` for its runtime DLLs; it is not a build table yet.
- No PDB on Microsoft's symbol server for c2, c1xx, cl or link of either build (404; the same request for
  15.00.30729's c2.pdb redirects to the file). **[C]**
- Setup: a stage with `bin -> XDK/bin/win32`, `include -> XDK/include/xbox`, `lib -> XDK/lib/xbox`
  (`work/xdk/stage`), then `uv run tools/c2build.py setup --c2 xbox360` (the table's `setup.from`). The patched
  c2.dll's cave calls `LoadLibraryExW(L"c2trace.dll", 0, 0)` (table `patch.loadlib: "exw"`: this c2.dll imports
  no other loader); c2trace.dll is built with x86 15.00 (`dll_build`).
- A matching game: Left 4 Dead 2 (disc, November 2009): every module's Rich header names build 8153 (C++ 15.00,
  C 15.00, masm 9.00, linker 9.00) and its XEX headers XDK 2.0.9328. Not this compiler: TRON (XBLA, XDK 6534,
  cl 14.00.6502), Zombie Apocalypse: Never Die Alone (XBLA, XDK 11886, cl 16.00); Xbox Live Indie Games are XNA
  (.NET assemblies, no native code). `uv run tools/x360img.py info GAME.xex` answers this for any title.

## 3. Symbols and how functions were located

There are no names. Two references with public PDBs were tried: x86 15.00.30729 (`c2port.py match`: 5 of the 47
hooked functions, all weak) and its x64 cross c2 (4 of 64). The PowerPC back end is mostly other code, and where the
source is shared the compiled bytes differ (another register allocation of c2 itself), so byte signatures fail.
What worked:

1. **Anchors by structure.** Calls to the four weak matches lead to the back-end driver 0x75834, which calls the
   passes in x86's order: PostLowerPreRegAlloc 0x74dc0, GlobalRegAlloc 0x74e1b, CodegenUpdate 0x74ef6,
   PostCodegenFlowOpts 0x74fc9, 0x75014 (PreEncode's slot, **[I]**), FinalFlowOpts 0x7508f, CodegenFinish 0x75111,
   EncodeAndRelated 0x751ec. A trace of dop2_Codegen 0x75bec fires once per compiled function. **[C]**
2. **A loose matcher** (`work/xdk/port/loose.py`: register-erased instruction multisets, callee lists aligned by
   dynamic programming from the anchors outward): 1778 functions, 16 of the hooked ones. Its names are leads, not
   facts: it called dop2_GlobalRegAlloc "EmitFunction" and Schedule "LoFinishFinalLower".
3. **Reading the code** against x86 15.00's, decision by decision, and **traces** that confirm each address (a hook
   fires the expected number of times with the expected arguments).

| function | rva | how |
|---|---|---|
| dop2_GlobalRegAlloc | 74e1b | driver's 2nd pass: FgEarlyTailDup, Schedule, Globregs, Schedule, Color, Schedule |
| Globregs | 50b36 | called by dop2_GlobalRegAlloc |
| Color | 2bcc1 | dop2_GlobalRegAlloc; class loop and pop as x86's (section 5) |
| Spillcosts | 2768f | Color's class loop, again after splits; the prio updates |
| OrderGlobregs, SpillObvious, InitialSplitPoints | 2b6d8, 2b606, 26f0f | Color's class loop |
| SpillOrDefer, BetterToDefer, InsertInList, HandleCurrUncolorable | 2b429, 29477, 258a3, 2b56b | Color's decisions |
| InsertSplitPoints, ChoosePhysReg, UpdateAffectedGlobregs | 2a536, 28860, 2baf0 | Color, in x86's order |
| RemoveSplitGlobregs | 259df | same code as x86's |
| Schedule, SchSchedGraph, SchSelectInstrs, SchAddToReadyList, SchComputeStaticPriority | dba9c, db986, db7da, db6d5, db52b | traced (section 6) |
| EncEmitFunc | 2e176 | dop2_EncodeAndRelated's last call |

Globals: `_CurFunc` 0x10c38108 (traced: `[[[_CurFunc]]+4]` is every function's name, the x86 layout), `_RegName`
0x10b12328 (357 names: noreg r0 sp toc r3..r31 fp0..fp31 cr cr0..cr7 .. lr ctr .. vr0..vr127), the colouring list
head 0x10c3ba64, the register weights 0x10c3b4d0, register orders 0x10c304e4, the issue array 0x10c35058, the
static-priority row pointer 0x10c69cac, OpSchedInfo 0x10b1c390 (0x295 rows of 12 bytes).

## 4. The IL

The tuple and leaf layout is x86 15.00's: the dump needed no layout change. **[C]** This c2.dll has no opcode-name
table, so names come from the build table (`opnames`, written to c2trace.dll as `op` lines; `set opcount 0`):

- Shared IL opcodes: `tests/data/x360_ops.cpp` exercises each operation; its IL at dop2_Codegen names them by
  their operands. They sit at x86's positions minus 0x27..0x2d: ARG 2ae, ASSIGN 2af .. NEG 2b5 (−0x29), RET 2c0,
  ADD 2c5 .. REM 2ce (−0x2b), SHR 2cf (no DIVREM), CMP 2d4 .. GOTO 2de (−0x2d), SWITCH 2e8, QUESTION 2ea. 2e7 is
  Xbox-only (the counted-loop branch, `bdnz`). Runs between anchors whose members are not all present are marked
  `?` (op2b6..2bf, op2c1..2c4, op2d0..2d3); 2f8 is the function's entry / exit pseudo-op (not named yet). **[C]**
- Machine opcodes: `tools/x360/opnamer.py` pairs the IL at EncEmitFunc (one tuple per emitted instruction) with the
  /FAsc listing: 429 of 429 functions (zlib, tests/data, the probe; /O2 /O1 /Od) give 101 names. Simplified forms
  fold into one opcode (bc, bclr, mtspr, rlwinm, tw, bdz); a mnemonic made by several opcodes keeps the first and
  suffixes the others (`lis_286`, `addi_287`: the relocated halves of a global's address). **[C]**

## 5. Register allocation

Color has x86 15.00's structure **[C: 0x2bcc1 read against x86 0x2f880]**: classes 7 down to 0
(`[0x10c37fc8 + 4·class]`), per class InitialSplitPoints, Spillcosts, SpillObvious, OrderGlobregs, then the pop
loop: head 0x10c3ba64, `next` +0x14, `prev` +0x18; benefit +0x40 ≤ 0 → SpillOrDefer; BetterToDefer → InsertInList;
an empty register set (`[gr+0x20]`) → HandleCurrUncolorable; else InsertSplitPoints, ChoosePhysReg (ecx = gr,
edx = class), UpdateAffectedGlobregs. GLOBREG fields are x86's (prio +0xc, benefit +0x40, nrefs +0x24, defOrd
+0x44, id +0x1c, symbol +0). The 13 `regs` events:

| event | site | logs |
|---|---|---|
| 0 Color | entry | function name, base line, IL |
| 1 class | OrderGlobregs entry | edx |
| 2 pop | 0x2bec8 | edi = the popped gr and its fields |
| 3 colour | 0x2bf7b, after the call | edi, `[[gr+0x10]+0x1c]` by name (c2regs reads a hook-3 entry with a name as the colour) |
| 4 candidate | 0x28942 | each register tried and its weight (before the free test: x86 logs only free ones) |
| 5, 6 | SpillOrDefer, HandleCurrUncolorable | ecx |
| 7 | Spillcosts entry | a priority pass starts |
| 8 | 0x27dc5 | block end: esi = P·w, edi = w |
| 9, 10 | 0x27dd0, 0x27dff | died in / live out of the block: prio += blkcost·P·w |
| 11 | 0x27e0d | live through, unreferenced: prio −= P·w (when `[0x10c35d1c]` = 2 the subtraction is w) |
| 12 | 0x259f1 | RemoveSplitGlobregs: a split piece unlinked |

Every probe was checked: no branch target inside its stolen bytes (0x27e1b, the first choice for event 11, has one).
The register order of the integer class is r11 r10 .. r3 r31 .. r14 (ChoosePhysReg's candidate walk on o_loop).

## 6. Scheduling

Schedule (0xdba9c) runs before register allocation and again after Color, from dop2_GlobalRegAlloc, when
`[0x10c262d8]` is set (it is at /O2), and from dop2_CodegenFinish. **[C: traces]** Its helpers are x86's with moved
fields: DAGNODE prio +0x38 (x86 +0x2c), sort key +0x3c (+0x30), height +0x48 (+0x38), lex +0x44 (+0x3a), est
+0x40 (+0x34), flags +0x4e (+0x40), nsucc +0x26 (same). The static priority is the sum of node terms shifted by
the row at `[0x10c69cac]`, read at runtime: B −1, height 13, nsucc 8, C −1, −2, L 10, x87 store 0
(`sched_hwt`). SchSelectInstrs takes the cycle as its argument and returns how many it issued (two per cycle at
most: Xenon's dual issue). Latencies: OpSchedInfo word 2 is an instruction class 0..10, and a class × class
matrix at 0x10c340b8 gives producer → consumer latencies (not modelled yet).

### 6.1 Symbol keys

SymAlloc (0xc7b5b) takes the kind in cl; temporaries have their own allocator (0xc7ea9, SymAlloc's tmp path as a
function: role `alloc2`, always class tmp). The symbol table is `[0x10c67684]`; per class a free list and a pool of
32-key blocks, read in SymAlloc / SymFree / SymInit **[C]**:

| class | alloc kinds | free list | pool | free (head kind) |
|---|---|---|---|---|
| tmp | 3, 6 | +0x2c | +0x24 | 3 |
| frame | 4, 5, 0xd | +0x30 | +0x18 | 0, 1, 2, 4, 5 |
| glob | 7, 8 | +0x34 | +0x1c | 6, 7, 8 |
| other | 9, 0xa, 0xb | +0x38 | +0x28 | 9 and above |
| (the model's hash) | 0, 1, 2, 0xc, 0xe, 0xf | none | +0x20, made on first use | |

SymInit makes the frame, glob, tmp and other blocks in that order and takes frame key 0 itself (`symkeys_init_used`).
SymFree (0xc7da7) walks the chain (probe 0xc7dbd: the member's key, the head's kind). The current line is
`[0x10c262c8]` (30 of 30 votes from matched functions that read x86's 0x51253cf0). Operand sorts: MscMergeSort
(0x7cfb8; list ecx, compare edx) is also called by the Fact*Sort family, so the role keeps the return-address test
(`symkeys_sort_ret` 0x10b89a00); OptLeftWeightTree's node is in ebp; the tree-node fields are x86's.

## 7. Not ported

- `scratch`: x86's scratch-register pass (eax/ecx/edx rotation) has no PowerPC counterpart here; temporaries are
  globregs.
- The x87 view, `regs --pieces`, `origin`, `regs_imm`: x86-only or not needed.
- `slots`: ColorStackElements (loose 0x61f41) and colorElemental are found, but the x86 packer's 18 probe roles
  encode x86 frames (ESP / EBP frames, parameter homes on the stack, the T >= 112 undo); PowerPC passes arguments in
  registers and addresses its frame from r1, so the roles need their own reading, not new addresses.
- `icount`, `loadcse`, `realize`, `merges`, `regmap`, `tsweep`: they compare x86 listings; PowerPC listings and a
  PowerPC alignment (x360check has the word compare) come first.
- ChoosePhysReg's inputs for `rasim`'s register-choice model (x86 events 13..17), the DAG-edge probe (c2sched rule
  5), a Xenon issue model (rule 7 with units: the P6 model matches 1235 of 1760 picks, by chance).
- `lift2il` / `il2c_lift`: x86 only. `x360check` is the comparison half of a PowerPC lifter.
- The c2s compile server has no Xbox toolchain: traced compiles of this build run locally under wine.
