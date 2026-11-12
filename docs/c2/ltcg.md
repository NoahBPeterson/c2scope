# c2 under link /LTCG: what a caller's code depends on, and how to reproduce it without the whole binary

With `/GL`, cl writes IL objects and generates no code; `link /LTCG` loads c2.dll and runs the back end over all
/GL objects as one unit. This file states which facts about other functions an LTCG function's code depends on,
where c2 decides each, and how to supply them with stubs so that a target can be matched alone. Primary build:
**cl 13.10.3077** (the RenderWare 4 core of Spore); 14.00.50727.762 and 15.00.30729.1 compared in section 8.

**[C]** = seen in a compile, a trace or the code at the cited address. **[I]** = inferred. Addresses are 14.00
(`Bin/c2.dll`, PDB names) unless marked 13.10 (c2 13.10.3077, names carried over from 13.10.6030, port-1310.md
section 3; decompile `work/decomp/c2-13.10.3077`). Probes: `work/scratch/ltcg/pN/` (git-ignored), each an `a.c`
(callers) and a `b.c` (callees), built with `uv run tools/c2ltcg.py --c2 13.10 asm work/scratch/ltcg/pN/a.c --with
work/scratch/ltcg/pN/b.c`.

## 0. Summary for decompilers

An LTCG function's code is a function of (a) its own body, (b) the bodies of the callees that get inlined, and
(c) for each callee that stays a call, three facts only:

1. **Memory summary** (callee.md): which pointer parameters it reads / writes / lets escape, which globals it
   reads / writes. Under LTCG every function with a body in any /GL object has one. **[C]** p6
2. **Kill set**: the volatile registers (eax, ecx, edx; xmm0-7) its final code writes, through its own calls. A
   caller keeps a value in a register its callee does not write. A callee without a body kills them all. **[C]** p3, p4
3. **Calling convention**: the declared one, unless nothing outside the /GL set can reach the callee (not
   exported, not referenced by a non-/GL object, address not taken, not virtual, not varargs, not recursive):
   then each parameter its entry block loads into a register **arrives in that register**, unused parameters are
   **removed**, and (14.00/15.00 only) a parameter every caller passes the same constant is **folded** into the
   callee. **[C]** p2, p7, p9

The hypothesis holds on the tested functions: a target plus generated stubs, linked with /LTCG on 13.10.3077,
gives the same code as with the real callee bodies (section 7). The "original reads ecx/eax at entry, our decl
passes nothing" pattern is rule 3: the reading function is itself a custom-convention callee, and compiling it
under /LTCG with only /GL callers reproduces the register argument (FUN_011f3540: `test eax, 0x800000` at entry).

## 1. Tools

```sh
# listing of an LTCG build: map file + per-function disassembly of the output image (capstone, /FAsc line format)
uv run tools/c2ltcg.py --c2 13.10 asm a.c --with b.c [--export SYM] [--func NAME] [--out DIR] -- /MD /Gy /TP
# the trace reports, and the tools that need a listing, on an LTCG build (the template / TU is a.c)
uv run tools/c2ltcg.py --c2 13.10 regs|scratch|il|regmap|tsweep|rwsweep|helpersweep a.c --with b.c ... -- <cl args>
# callee facts from the original executable, stubs, and the comparison (section 6)
uv run tools/ltcgstub.py facts|callees|stub|stubs|cmp EXE ...
```

`c2ltcg.py` links with `/MAP` and `/OPT:NOICF` (identical stubs would otherwise share an address) and writes
`ltcg.map`, `ltcg.asm` (every function of the map, publics and statics, `NAME PROC` .. `NAME ENDP`, lines
`  OOOOO<tab>bytes<tab> mnemonic<tab> operands`, offsets from the function start, so they equal the IL's encoded
offsets) and, under `--out`, one `NAME.s` per function in the `VA mnemonic operands` format that `--asm-vs` takes.
In the traced subcommands the listing is also copied to the `.asm` path a traced compile would have written,
which is what `tsweep.listing_instrs`, `listing_offsets` and `regmap` read; their non-LTCG behaviour is unchanged.
Function extents come from the map (up to the next symbol, int3/zero padding after the last instruction dropped).
Without `--export` nothing is exported and every function is internal (all may get custom conventions). The
wrapper always compiles and links locally: it sets `C2TRACE_REMOTE=0` and disables `c2trace.run_remote`, because
a traced compile sent to the remote compile server is one ordinary object compile, which bypasses the /GL + /LTCG
step (the listing is then cl's own and the code is not the LTCG code).

## 2. Custom calling conventions **[C]**

### 2.1 Which functions get one

| callee (probe) | convention under /LTCG on 13.10 |
|---|---|
| extern, called only from /GL code (p2 `f1`, p1 `callee_ext`) | custom: `a` in eax, nothing on the stack |
| `static` (p1 `s_leaf`) | custom |
| address taken anywhere, also in another object (p2 `f3`, p8 `e3`, member pointer `m`) | standard |
| `__declspec(dllexport)`, `/EXPORT`, or `#pragma comment(linker, "/EXPORT:" __FUNCDNAME__)` in its body (p2 `f4`, p8 `e2`) | standard |
| also called from a non-/GL object (p2 `f9` from `c.obj`) | standard |
| virtual (p7 `vf`), even for a direct call on a local object | standard thiscall |
| varargs (p2 `f7`) | standard |
| self-recursive (p2 `f8`) | standard |
| `__stdcall` (p2 `f5`, p7 `s3`), `__fastcall` (p2 `f6`), thiscall member (p7 `get`, `st3`), static member (p7 `sm`) | custom: the declared convention does not protect it |
| inline asm that does not touch the parameters (p8 `e4`) | custom (MscInlineAsmAssumesCallingConvention is false) |

The gate is **`MscAllowCallingConventionChange`** 10b98b90 (the same test is inlined in **`MdmAllowFlexParams`**
10c59420, called from `MscProcessParams` 10b99100 after Lower): global optimisation on, not WVM, **SSR+0x20 &
0x2400 == 0**, SSR+0x50 & 4 == 0, and no inline asm that assumes the convention. MdmAllowFlexParams then sets
SSR+0xb4 = 1 ("may be flexible"). Bit **0x2000** of SSR+0x20 is "referenced from outside the /GL set": the linker
hands c2 the list of such symbols in `CC2::XObjHandler2::CodeGen` 10cc3400, and `LtcgMarkExtRefSyms` 10b8c510 /
`LtcgMarkExtRef_OneSym` 10b8c4c0 set it (13.10: `@LtcgMarkExtRef_OneSym@4` 1070bf50). Exports and references
from non-/GL objects arrive that way. **Bit 0x400 is address-taken [I: the probes show address-taken functions
stay standard; the bit's writer was not read]**; SSR+0x50 & 4 is presumably recursion **[I]** (`f8`).

### 2.2 Which registers

`MdmFlexParams` 10c59460 (in `dop2_CodegenUpdate`, i.e. after register allocation of the callee itself; 13.10:
107863a3) walks the callee's entry block from the top, at most 4 parameters, stopping at a label or call: an
instruction `mov reg, [param]` (also movzx/movsx-class opcodes 0x38, 0xc2..0xc5, 0xd0) that loads a parameter
which is not referenced anywhere else as memory becomes "parameter arrives in reg"; the load is deleted
(`MdmAllocateFlexParam` 10c5a010 writes reg << (3k+1) into SSR+0xb4; XMM parameters 4 bits each in SSR+0xb8).
eax, ecx, edx, ebx count against a budget of 3 (2 when ebx is needed for alignment); esi, edi do not (p6
`medium` got its two parameters in edi and esi). A parameter whose entry-block register is already taken is given
another free volatile register. Callers read the map in `MdmAlloRegParams` 10c563b0 (`_MdmFlexParamMap`) and
load the argument into that register instead of pushing it. 13.10 keeps the map at **SSR+0xe1** (MdmFlexParams
107863a3 tests it first) **[C from the 13.10 decompile]**.

So **the convention is a function of the callee's own allocation**: a parameter used only as a memory operand
stays on the stack (p2 `f2`: `a` eax, `b` ecx, `c d e` on the stack; p2 `f10`: the doubles stay, `char c` eax,
`short s` ecx; p7 `wide`: imul with a memory operand, stays), and the order is the order of first use (p2 `f13`:
`b` eax, `a` ecx). `this` can move off ecx (p7 `get`: this eax, k ecx). A hidden struct-return pointer can be a
register argument (p7 `mk`: eax = return buffer, ecx = x).

**Return and callee-pop.** The return register is unchanged. The stack part keeps the declared cleanup: cdecl
callers pop what is left (p2 `f2`: `add esp, 0xc`), stdcall / fastcall / thiscall callees `ret N` for the stack
part only (p2 `f6` `ret 4`, p7 `st3` and `s3` `ret 8`); a function with no stack parameters left returns with
`ret` whatever it was declared (p2 `f5` stdcall).

### 2.3 Dead parameters **[C]**

`MscSetDeadParamMap` 10b98c20 (dop2_PreLowerGlobalOpts; 13.10 1077d00c) runs behind the same gate and writes
SSR+0xbc: bit 0 = valid, bit k clear = parameter k is never used; the parameter is deleted from the callee, and
`MscRemoveDeadArguments` 10b98da0 (13.10 107708a3) deletes the argument, and its computation, at every caller
(`MscParamIsDead` 10b99080 answers the same for the inliner). p2 `f12(int unused, int b)`: the caller passes only
`b`, in eax. p9 `dm(a, dead, c)` with `dm(x, y * 3, y)`: `y * 3` is gone. An exported function keeps its dead
parameter (p9 `dx`).

### 2.4 Constant arguments: 14.00 and 15.00 only **[C]**

On 14.00 and 15.00, WPA (`WpaCollectCallConstParamInfo` 10c00080, `WpaGenConstParamInfoUse` 10c03a20, which
also calls MscAllowCallingConventionChange) folds a parameter that every caller passes as the same constant into
the callee and drops it from the calls: p9 `kc(a, 5)` from two callers becomes `a * 5 + g` with one argument;
`kb(a, 3)` loses its `k == 3` test. `kv` (called with 5 and 7) is left alone. **13.10 does neither**: p9 `kc`
still multiplies by its stack argument and the callers push 5, and the 6030 PDB has no `Wpa*` names. A
"FoldCmpSpeedWeight" inliner bonus for compares against constant arguments is reported elsewhere; no symbol of
that name is in our 14.00 index and it was not examined **[not verified]**.

## 3. Kill sets **[C]**

**Recording.** `MscCollectUsedRegs` 10ba2030 walks the callee's final instruction list and collects every register
that appears as a **destination** operand, plus the kill-set operands of the calls it contains (so the set is
transitive). `MscRecordInterProcRegInfo` 10ba2290 (dop2_EncodeAndRelated, under `_BottomOpt`) stores it on the
call-graph node, **CGNODE+0x60**. 13.10: the same pair at 107b9f14 / 107b61d6 stores it on the symbol,
**SSR+0xad**, with no `_BottomOpt` condition.

**Use.** `LoAddCallScratchRegs` 10c3f5f0 (lowering of each call; 13.10 10778a6d) builds the call's kill operand
(kind 0xb): every register 1..8 whose `_RegAttrib & 0xb` is 0 (eax, ecx, edx), XMM 0x4c..0x53 and 0x54..0x5b,
**if the callee has no recorded set or the set contains it**, minus the call's own destinations (the return
register). The recorded set is used when the call target is a direct function symbol with SSR+0x37 & 0x4000
(13.10: & 0x2000) whose node is its own. Color then removes the kill set from the avail set of every value live
across the call (regalloc.md 3.3.2). Only writes count: a callee that reads ecx but never writes it leaves ecx to
the caller (p3 `c_ecx`).

Trace (14.00, p4): `-H '@MscCollectUsedRegs@8=g10d123ac>0>0>4$/xeax>0#3'` gives k_div 0xa = {eax, edx}, k_stk 6 =
{eax, ecx}; `-H '@LoAddCallScratchRegs@4=...,s1>0x28>0x18>4$/xa1>0x2c>0>0x18>0#3'` gives the kill operand of the
call in c_div = {edx} (eax is the call's destination), in c_stk = {ecx}, in c_edx2 = {ecx, edx}. `regs` then
shows the candidates: c_eax's `a` keeps edx; c_edx's `a` gets esi because edx is killed and ecx went to `p`.

| caller keeps `a` across (p3, p4) | callee writes | `a` gets |
|---|---|---|
| `k_eax` (`lea eax,[eax+eax*4]`) | eax | edx |
| `k_ecx` (`shl eax, cl`, a in ecx) | eax | ecx (the argument register itself) |
| `k_stk` | eax, ecx | edx |
| `k_tr` (calls k_eax) | eax | edx |
| `k_div` / `k_edx` | eax, edx | esi (ecx taken by `p`) |
| `k_ext` (calls a bodiless `ext`) | all | esi |

Exported and address-taken callees keep their recorded set (p8 `e2`, `e3`, `m`: `a` stays in edx), so a
standard convention and a small kill set go together, as in Spore (FormatGetDepth). **Outside LTCG the same rule
holds inside one translation unit** (p5, 13.10 and 14.00: an extern same-TU callee writing only eax leaves edx to
the caller), which answers il.md's open question about `MscRecordInterProcRegInfo`.

## 4. Cross-module summaries and order **[C]**

The callee-body memory summary applies across objects (p6, callers in a.c, callees in b.c): `rd(&x)` (reads `*p`)
→ `mov eax, 5` after the call; `wr(&x)` → reload; `gw()` (writes only g2) → `g` known and the call becomes a tail
jump. Code generation is bottom-up over the whole call graph, not per object: the `regs` order for p3 is every
`k_*` of b.c, then the `c_*` of a.c that call them, then `k_tr` and `c_tr` (il_pipeline.md 4.2 applied to all
modules).

## 5. Cross-module inlining **[C for the probes, I for the rule]**

Small and loop functions from another object are inlined as within one unit (p6 `small`, `medium` into a.c
callers); two larger loop functions (p10 `big`, `big2`) stay calls under LTCG and in one TU alike. No LTCG-only
limit was seen. The LTCG-specific inliner paths (`LtcgInlClassify`, `PtInl*`, inline.md "not covered") were not
read.

## 6. The stub generator: `tools/ltcgstub.py`

```sh
IMG=../spore-decomp/work/SporeApp.analysis.bin
uv run tools/ltcgstub.py callees $IMG 011efeb0        # each call: what the caller keeps across it, callee facts
#   011efedf call 011ef910  keeps edx   | 011ef910  conv cdecl  ret 0  entry regs -  kill eax,ecx
uv run tools/ltcgstub.py stubs $IMG --protos init.protos --prelude prelude.h --out init_stubs.cpp
uv run tools/ltcgstub.py --c2 13.10 cmp $IMG 011efeb0 init.cpp --with init_stubs.cpp --func Initialize \
    --export '?Initialize@Raster@graphics@rw@@SAPAV123@ABUMemoryResource@23@GGEHI@Z' --check-stubs init.protos -- /MD /Gy /TP
```

`facts` reads from the original: the kill set (every destination register of the reachable code, through direct
calls and tail jumps; an indirect call, an import or a recursion cycle means all volatile registers; jump tables
followed), the entry registers (read before written on some path; `push` of a callee-saved register and `xor r, r`
are not reads), `ret N`, and a convention label (`custom` when eax, ebx, esi or edi are read at entry). `--protos`
lines are `VA  DECLARATION  [regs=eax,ecx] [std|custom]`. `cmp` builds the target with the stubs (`--no-gl`: the
same sources as ordinary objects, for a baseline), compares the function with the original (in-image absolute
operands and rel32 targets leaving the function masked on both sides; bytes, positional instructions, and
tsweep's `regdiff`), lists each call's callee facts on both sides, and with `--check-stubs` checks each stub's
kill set, entry registers and `ret N` against its original.

| stub property | what c2 reads | how the stub gets it |
|---|---|---|
| stays a call | Inline | `__declspec(noinline)` |
| memory summary | ItfSummarizeFuncInterf, callee.md | calleesum's lines; under LTCG every direct callee with a body is summarised, so calleesum runs with the whole image as `--tu` (`--tu LO-HI` narrows it) |
| kill set | MscCollectUsedRegs → CGNODE+0x60 / SSR+0xad | destination registers of the stub's own code: a returned value writes eax; `<< ltcgstub_k[1]` adds ecx (`shl r, cl`), `/ ltcgstub_k[1]` adds edx (`idiv`), folded into the returned value or applied to a volatile local at the top, where the scheduler cannot move another value into those registers first. No global is written (that would change the memory summary) |
| standard convention | SSR+0x20 & 0x2000 → MscAllowCallingConventionChange false | `#pragma comment(linker, "/EXPORT:" __FUNCDNAME__)` in the body (works for members, where `dllexport` on the definition is C2375) |
| custom convention | MdmFlexParams on the stub's own entry block | not exported; the register parameters are stored to a volatile local first, in the order eax, ecx, edx (the scratch rotation), so the stub's entry block loads them into those registers |
| live / dead parameters | MscSetDeadParamMap | in a custom stub every parameter the original reads is used; one the original never reads is left unused (and c2 removes it, as in the original) |
| no constant folding (14.00/15.00) | WPA | `--vary`: an exported second caller with unknown arguments (free functions with scalar parameters) |
| getter result | constant propagation of the return | a callee that returns a global's value returns `ltcgstub_gread`, not the constant 0 |

xmm kill sets are reported, not generated. A stub is compile-only: its `calleesum_*` / `ltcgstub_*` externs are
unresolved, and the link runs with `/FORCE:UNRESOLVED`.

## 7. Validation on Spore's RenderWare 4 core (cl 13.10.3077) **[C]**

Sources copied to `work/scratch/ltcg/spore/` from spore-decomp (read-only): `init.cpp` (Raster::Initialize
0x011efeb0, s011efb60), `pitch.cpp` (Raster::D3D9GetStreamedMipLevelPitch 0x011efe30), `t3540.cpp`
(FUN_011f3540, s011f3130, with a stand-in caller), `real.cpp` (FormatGetDepth's decompiled body from s011eec00).
Original bytes from `spore-decomp/work/SporeApp.analysis.bin`. Flags `/O2 /MD /Gy /TP`.

| target | one object, no LTCG | LTCG, callees only declared | LTCG, real callee body | LTCG, generated stubs |
|---|---|---|---|---|
| Raster::Initialize 0x011efeb0 (167 B) | regdiff 25, 117 bytes | regdiff 25 | **regdiff 2**, 1 instruction | **regdiff 2**, the same instruction |
| D3D9GetStreamedMipLevelPitch 0x011efe30 (116 B), spore source | regdiff 44 | 44 | 28 | 28 |
| the same, `blocks` folded into `width` (found with `regmap` and `tsweep` through c2ltcg) | 28 | 28 | | **regdiff 2**, 116 bytes, 5 differ |
| FUN_011f3540 0x011f3540 (58 B) | regdiff 11, entry `mov eax, [esp+4]` | 10, entry `test eax, 0x800000` as the original | | 10, the same |

- Initialize keeps `edx = 0` across FormatGetDepth only when the callee's kill set is {eax, ecx}. Ablations of the
  stub (`abl_*.cpp`): kill set {eax, edx} → regdiff 18; {eax} → 31. **[C]**
- Remaining difference in Initialize: `test ebx, 0x40` in the original, `test bl, 0x40` in ours (the two earlier
  tests, `& 8` and `& 0x200`, are shrunk to byte tests in both). Not an LTCG effect: 13.10.3052 and 13.10.6030 give
  the same, and 11 source respellings tried with `c2ltcg.py tsweep` did not change it. **[unexplained]**
- Pitch: the last two instructions are in the other order (`shl edx, 4; mov eax, edx` against `mov eax, edx; shl
  eax, 4`); 4 respellings of the return did not change it. **[unexplained, copy/coalescing]**
- FUN_011f3540: the register argument ("reads eax at entry") is reproduced; the rest is the body's shape (the
  original computes `!x` through `sete`), not LTCG. The stand-in caller passes `obj[4]` in eax, as the original's
  call site 0x011f3828.
- FormatGetDepth's real body, linked without an export, becomes a custom-convention function (argument in eax), so
  in the original it is referenced from outside the /GL set or has its address taken **[C for the prediction, I
  for which]**. Initialize itself must be exported for the same reason.
- Provenance: with the real callee body, Initialize gives regdiff 2 on 13.10.3077, 3052 and 6030, 6 on 14.00 and
  10 on 15.00, which supports spore-decomp's "RW4 core = cl 13.10 + LTCG".

## 8. Parity

| | 13.10.3077 / 3052 | 14.00 | 15.00 |
|---|---|---|---|
| c2ltcg listing, `asm`, `regmap` through c2ltcg | works (tests) | works (tests) | works (tests) |
| `tsweep` / `rwsweep` through c2ltcg | run on 3077 (section 7; rwsweep on p11 `W::run`) | not run | not run |
| custom convention, kill set (tests/data/ltcg_kill_*.c) | same code | same code | same code |
| recorded used-register set | SSR+0xad, flag SSR+0x37 & 0x2000 | CGNODE+0x60, SSR+0x37 & 0x4000 | not read (same behaviour) |
| flex map | SSR+0xe1 | SSR+0xb4 / +0xb8 | not read |
| dead parameters | removed | removed | removed |
| constant arguments folded into the callee (WPA) | **no** | yes | yes |

## 9. Workflow for a decompiler agent (an LTCG caller)

1. `ltcgstub.py callees EXE VA`: the calls, what the caller keeps across each (`keeps edx`), and each callee's
   convention and kill set. A callee labelled `custom` takes register arguments; the target itself, if `custom`,
   must stay internal in the probe build (no `--export`), and if standard must be exported (`cmp --export`).
2. Write a protos file with the declarations as the slice has them; `ltcgstub.py stubs EXE --protos F --prelude
   H --out stubs.cpp` (H: the slice's type declarations). For a callee that is already decompiled, use its real
   body instead (add the export pragma when the original is standard).
3. `ltcgstub.py --c2 13.10 cmp EXE VA target.cpp --with stubs.cpp --func NAME --export SYM --check-stubs F`:
   `regdiff` against the original; every stub line must say `ok`.
4. Remaining register differences are the target's own source: `c2ltcg.py --c2 13.10 regmap target.cpp --with
   stubs.cpp --export SYM --func NAME --asm-vs orig.s --asm-func NAME`, then `tsweep` / `rwsweep` the same way.
5. For hooking (spore-decomp's hybrid): a replacement may not write a register outside the original's kill set,
   and an LTCG caller of a custom-convention function passes arguments in registers no C declaration names.

## 10. Not verified

- The writer of SSR+0x20 bit 0x400 (address taken) and the meaning of SSR+0x50 & 4 (recursion) were not read.
- x87 / MMX parts of the kill set (LoAddCallScratchRegs adds x87 registers only under a module flag) and the XMM
  flex parameters (SSR+0xb8) were not probed; ltcgstub does not generate xmm writes.
- The second MdmFlexParams loop (parameters whose register is taken get another free one) was read, not probed.
- 15.00's addresses for these functions were not looked up; only behaviour was compared.
- Cross-module inlining limits beyond four probe functions; the LTCG inliner paths.
- Spore: which external reference keeps FormatGetDepth and Initialize standard.
- `rwsweep` / `helpersweep` find the target's definition by `?name@Class@@`, so a member of a namespaced class
  (`rw::graphics::Raster`) is not found (a limit of those tools, LTCG or not); helpersweep through c2ltcg was not run.
