# c2.dll IL map: the driver, the per-function pipeline and the switches

This page covers how c2.dll (cl 14.00.50727.762) gets IL from the front end, which structures hold the
compilation and the function being compiled, the order in which functions are compiled, and the order of the
optimisation and code generation passes inside one function. It ends with the switch table, which is where
the `/d2` options are defined.

Declarations are in `src/c2/il_pipeline.h`. Every size assert in that header compiles under cl 14.00.50727.762
(`work/scratch/glass_ilpipe_t/hdr.c`).

**Tags.** **C** = CONFIRMED: read in the code, or shown by a compile. **I** = INFERRED. Addresses are c2.dll
VAs. Data publics come from `llvm-pdbutil dump -publics`, with the section bases .text 0x10b01000, .rdata
0x10cd8000 and .data 0x10d06000.

**Cross-references.** SSR, the front-end symbol record that serves as a function's "entry", is in il_sym
(`src/c2/il_sym.h`). TUPLE is in il_tuple. The flow graph at FUNC+0x08 is in il_flowgraph.

**Test sources.** All compile tests are in `work/scratch/glass_ilpipe_t/`: `t.c` (command lines), `ord.c` and
`ord2.c` (function order), `pr.c` (per-function flags) and `ipra.c` (static callee conventions).

---

## 1. Process-level driver (main.obj)

| step | function | what it does |
|---|---|---|
| 1 | `_CallMain` 0x10c05870 | `MainInit`, then (unless `_P2IsCOM`) `MainCont` if `!_CilToObjPass`, then `MainEnd`, `MainFinal`; returns `_Nerrors` **C** |
| 2 | `@MainInit@8` 0x10c058c0 | `_ExecutionState = 1`. It reads the `MSC_CMD_FLAGS` env var into argv (`unconcat` 0x10b03020), then calls `MscProcessSwitches` (§7). In non-LTCG mode `_Module = _ModListHead = &_MainMod` and MainMod.flags444 \|= 2. It then calls `MscFltInit`, `LiInit` and `build_predef`. Under `-ltcg` it runs `LtcgCilToObj` instead **C** |
| 3 | `@MainCont@0` 0x10c05c40 | SEH wrapper around `MainContReal` **C** |
| 4 | `@MainContReal@0` 0x10c05a90 | Opens the **gl** stream (`ModGetILMap(0)`, then `ilmap_open` with suffix L"gl") and reads it with `read_gloil_header` (§2.2). It deletes each module's listing file (MODULE+0x2b8), then runs `PogoModuleInit` (if PGO) and `WvmInit`. Next comes `read_globsym`, which reads the global symbols of every module (`getsym(0)` over gl). In non-LTCG mode it opens **in** for each module (`IlOpen("in")`). It then calls `ModCheckPchTimestamp` (if MODULE+0x2a4), `read_initil` and **`dop2`** **C** |
| 5 | `@MainEnd@0` 0x10c05cd0 | `PogoModuleDone`, `_ExecutionState = 4`, `obj_end`, `LiModuleEnd`, unmaps metadata views **C** |
| 6 | `@MainFinal@0` 0x10c05dd0 | `CoffEnd`, `_ExitCleanup(0)` **C** |

`_ExecutionState` (0x10d06760) is 1 during init, 2 per function (set in `startproc`) and 4 at the end **C**.

## 2. How IL arrives

### 2.1 Streams and the ILMAP

c1 writes a set of IL files named `<_Basename><suffix>`. `_Basename` (0x10d0d2d4) is the `-il <name>` argument
that cl passes to both passes **C** (`/Bd` command line, `t.c`). Each stream is mapped into an `ILMAP {cur,
base, aux}` (12 bytes). The reader keeps a pointer to the active map in **`_PIlPtr`** (0x10d1886c) and reads
bytes with `*(*_PIlPtr)++` **C**.

| kind | suffix / LTCG section | opened by | content |
|---|---|---|---|
| 0 | `gl` / `.cil$g` | MainContReal (`ilmap_open` 0x10b03950 with L"gl" at 0x10cf905c) | header + global symbols **C** |
| 1 | `sy` / `.cil$s` | dop2 (`IlOpen("sy")` 0x10c07f80) | per-function local symbols; RdrReadProc sets cur = base + SSR+0x5c **C** |
| 2 | `ex` / `.cil$e` | dop2 (`IlOpen("ex")`) | per-function expression IL; cur = base + SSR+0x58 **C** |
| 3 | `in` / `.cil$i` | MainContReal (`IlOpen("in")`) | initializers (`read_initil`) **C** |
| 4..6 | `.cil$d` `.cil$m` `.cil$f` | LTCG | db / md / f **I** (names only) |
| 7 | `.cil$p` | LTCG section only | MODULE has room for only 7 maps **C** |

The section classifier is `FUN_10b60960` (iltcg.obj). It returns g0 s1 e2 i3 d4 m5 f6 p7 **C**.

There are three sources of bytes:

- **Files.** `ilmap_open` builds `wcscpy(Basename) + wcscat(suffix)` (fatal if the result is 0x104 characters
  or longer) and calls `FMapFile(&map->aux, path)`, which sets `cur = base` **C**.
- **PDB (`ilpdb.obj`).** When MODULE.flags444 bit0 is set, `IlOpen` calls `GetMoreIL` 0x10b031e0. It opens a
  PDB stream named after the suffix (`PDBOpenStream`, `StreamQueryCb`, `p2malloc`, `StreamRead`), so `aux` =
  byte count. `InitializePDB` 0x10b03150 opens the PDB (`PDBOpen2W`; fatal C1300/C1301) **C**.
- **LTCG (`iltcg.obj`, `mod.obj`).** `IltcgInit` 0x10b60a90 maps the .obj (`LtcgMapFile`) and builds an
  `objf::ObjectCode`. For each `.cil$x` section, `FUN_10b60a00` records size at obj+0x4c+k*4 and offset at
  obj+0x6c+k*4. `ModGetILMap(kind)` 0x10ba5510 promotes the object (`LtcgPromoteObj`/`LtcgPromoteFile`), maps
  the section on demand (`LtcgMapILSection`) and moves `cur` by the same delta as `base`. In non-LTCG mode
  `ModGetILMap` just returns the module's `&il[kind]` **C**. `ModRepointILPtrs` 0x10ba5470 rebases `cur`/`base`
  of all 7 maps when the LTCG file view (MODULE+0x240) moves **C**.

### 2.2 gl header (`read_gloil_header` 0x10bbbd60)

The fields are read in this order **C**:

1. A byte, which must be `0x11`. Otherwise C1900 "P1/P2 mismatch".
2. A ushort, which must be `0x386` or `0x2386`. Otherwise `fatal(0x389)`.
3. A 32-bit IL version. All modules must agree (`fatal(0x2f)` names the older or newer file).

The accepted IL versions run from 0x1302051 to 0x1321829 (the current one). Each newer version turns on more
reader features: `_WideAsmIL`, `_AlignmentInfo`, `_ClrCallIL`, `_GetFileHash`, `_CorSigLenIsLong`,
`_QuestionHasCorsig`, `_ExpectAssemblyLocations`, `_NoExtraOPextractOnOPmfunc`, `_LinenumbersAre32bit`,
`_DebugCallSiteInfo`, `__GeneralizedOPthunkx` and `__AlwaysOPfunctionTI`. An unknown version is fatal C1900
(`fatal(900, "P1", ver, "P2", 0x1321829)`).

After the version come:

- `_P2_Start_Key = long_read()`, raised to at least `_Key`. Keys are front-end symbol ids, see il_sym.
- One signed byte into MODULE+0x414.
- On new IL only: a flags long (bit1 `_LongTypeIndex`, bit2 `_IsCorModule`, 0x10, 0x20), then three shorts
  `_FeMajor`, `_FeMinor`, `_FeBuild`.
- On the newest IL only: `__SsrTrailerOffset`.

### 2.3 Function records and the entry list

`readsym` 0x10bbaea0 reads symbol records. A function **definition** is record kind 0xe. For each one it does
the following **C**:

- `SSR.attr(+0x37) |= 0x4000`. This marks "has a body"; il_sym reads bits 14-16 as the storage class.
- `+0xa0 = loread()` if `SSR+0x20 & 0x200000`.
- **`+0x58` = offset into ex**, **`+0x5c` = offset into sy**, `+0x54` = ushort (added to the LTCG progress
  counter 0x10d0d220 in ReadFunction).
- **`+0x50 = keyread() & ~4`**: function flags written by c1.
- `+0x56` = ushort.
- `+0x20 |= (MODULE.flags44c >> 11) & 0x20`. That is bit16, -Gy, which ends up as SSR+0x20 bit5.
- If `+0x50 & 0x1000`, it creates a **CGNODE** (`CgAddNode` 0x10b05260) and reads the callee list: a count
  (short, or long on new IL), then that many `{key, ushort count}` records pushed onto `node->rawCalls`.
- `+0x8c` = running ordinal (0x10d39ae8++).

`getsym` 0x10bba720, case 4 (function), then **inserts the SSR into `_Eentry_list`** (0x10d1885c; links
SSR+0x80 next, +0x84 pprev). It does this when the record is a function (`+0x30 == 4`) with a body (attr 0x4000)
that is not dllimport (attr 0x400). The list is **sorted by SSR+0x58**, the ex-stream offset, which is the
order in which c1 wrote the bodies. The search starts from a remembered cursor (0x10d0d098) when the new offset
is not smaller **C**.

The same case then uses MODULE+0x418 (the `-Ob` level, default -1) to index a jump table at 0x10bbad20 **C**:

| `-Ob` | action on SSR+0x50 bit 0x40 |
|---|---|
| default (-1) / 2 | kept |
| 0 | cleared |
| 1 | cleared unless `+0x50 & 0x2080` |

Bit 0x40 is the "may be inlined / inline-only" bit. `InlCollectInfo` 0x10b70b50 records inlinee info only for
functions that have it. The bit names in this paragraph are **I**: 0x80 is read as `inline`, 0x2000 as
`__forceinline`.

SSR+0x50 bits used by the driver (see il_sym for the record itself):

| bit | meaning | evidence |
|---|---|---|
| 0x1 | body comes from a PCH (`ModGetPchData` instead of sy/ex) | RdrReadProc **C** |
| 0x2 | already taken by the dop2 loop | dop2 **C** |
| 0x4 | cleared at read | readsym **C** |
| 0x20 | **must be compiled**. c1 sets it, or one of these does: `ModMarkSymAsReferenced` 0x10ba81e0, `ModMarkWeakEntryReferenced` 0x10ba8320, `EncProcessSymbols` 0x10b23a50 (references found while encoding another function), `initializers_available` 0x10bbfa90, `CgPropagateInstantiate` | **C** |
| 0x40 | inline candidate (see above) | **C** use, **I** name |
| 0x100 | has EH; with `-noehopt` it turns off global opts (MscProcessPragstat) | **C** |
| 0x1000 | c1 wrote a callee list, so the function gets a CGNODE | readsym **C** |
| 0x40000 | must-align; sets `_FHasMustAlign` (CgBottomUpOrder) | **C** |
| bits 8,9,10,15,19 | copied into FUNC.flags 0x40000, 0x100000, 0x200000, 0x8000000, 0x400000 by RdrReadProc | **C** |

## 3. Compilation-level structures

### 3.1 MODULE (0x458 bytes, `ModNew` 0x10ba55f0)

The command line fills the static `_MainMod` (0x10d123b8). `MscProcessSwitches` packs the switch globals into
it. `ModNew` either copies `_MainMod` (0x116 dwords) or allocates an LTCG module, then appends the result to
`_ModListHead`/0x10d0d11c. `ModSetModuleFlags` 0x10ba5730 copies the same fields from `_MainMod` into another
module. The full layout is in the header. These fields matter for codegen **C**:

| offset | field | source |
|---|---|---|
| +0x250 | `ILMAP il[7]` | §2.1 |
| +0x418 | inline level | `-Ob#` (cl passes `-Ob2` for /O1 /O2 /Ox, `-Ob0` for /Od; forced to 2 under PGO) |
| +0x41c | `-Og` given | cl passes `-Og` for /O1, /O2, /Ox and /Og |
| +0x420 | CPU level (short) | `-G3`..`-G8` (Gtab), default 7 |
| +0x444 | flags | bit0 IL in PDB, bit1 main module, bit2 metadata/CIL module (no ex/sy; `MSILReadProc`), bit3 !clrsafe |
| +0x44c | flags A | listing (`-FA`/`-Fa`), `-W` level, `-Wall`, `-WX`, `-Zi`, `-Z7`, `-EHs` (0x8000), `-Gy` (0x10000), `-GS` (0x20000), `-Gh`/`-GH`/`-callcap` (0x380000), `-QIfist`, `-QIftol`, `-QImapX87` (0x8000000) |
| +0x450 | arch | `-archSSE`=2, `-archSSE2`=3 (SSEtab 0x10ced9dc) |
| +0x454 | flags B | `-QIWMTemu` 1, `-QIf` 2, `-hotpatch` 4, `-NoLUR` 8, (0x10d0d158) 0x10, `-QIdivrem64` 0x20, `-clrpure` 0x40, `-Gil` 0x80, `-Yc` 0x100, `-nolock` 0x200 |

### 3.2 Per-compilation globals

The complete list is at the end of the header. These matter most:

| global | address | role |
|---|---|---|
| `_Module` / `_ModListHead` | 0x10d195cc / 0x10d195c8 | current module / list **C** |
| `_Eentry_list` | 0x10d1885c | functions still to compile, in order (§4) **C** |
| `_Entry` | 0x10d1239c | SSR of the function being compiled **C** |
| `_CGnodes` | 0x10d0ef2c | call-graph nodes. Non-empty means bottom-up mode (§4.2) **C** |
| `_Ltcg` / `_CilToObjPass` | 0x10d1882c / 0x10d1884c | `/GL` link-time pass / `-ltcg` CIL-to-obj pass **C** |
| `_LineOffset` | 0x10d0d6fc | current tuple's line offset (tuple+0x14). Every phase resets it to 0 **C** |
| `HandlingControlC` | 0x10d0675c | when set, `_OKToHandleCtrlC` is polled between every pair of phases **C** |

## 4. Which function is compiled next (dop2 0x10c05fb0)

### 4.1 dop2 setup, in order **C**

1. Non-LTCG: for every module, open **ex** and **sy** (`ModGetILMap(2)`/`IlOpen("ex")`, then `(1)`/`"sy"`),
   unless it is a metadata module.
2. `_LabelHashMap = getmem(4, 0xfffc)` once.
3. Managed modules: `IMetaLoadALink`, `IMetaGetImportedMetaData`, `ooBuildInheritanceGraph` per module, then
   release the imported scopes.
4. LTCG: `SsrFree` the list at 0x10d0d114.
5. **If `_CGnodes` is empty: `SsrOrderInitializer`. Otherwise: `CgBuildCallGraph`** (§4.2).
6. `EncInit`, `LtcgInlClassify`, `InlInit`.
7. LTCG only (and not `-cvtcil`): `WpaBottomUp`, `WpaTopDown`, then clear CGNODE+0x50..0x71 of every node.
8. PGO: `PogoCgInitOrderList` (`_PogoMasterFlag == 2`) or `PogoCgEstimateRunTimeConstraints` (== 1).

### 4.2 Bottom-up order (CgBuildCallGraph 0x10b056b0, CgBottomUpOrder 0x10b05bd0) **C**

`CgBuildCallGraph` does the following:

1. Sets `_BottomOpt = 1`.
2. For every node, resolves each raw call key to an SSR (`symlookglobal`, then the local table). If the callee
   is a function with a body, it calls `CgAddCallee` `count` times. Otherwise it sets node flag 0x100 (calls
   something unknown).
3. Calls `CgPropagateInstantiate`, or `CgRemoveUnreachable` under `-optref`.
4. Calls `CgIdentifyRecursion`, which marks back edges (edge+0x0c bit0).
5. Calls **`CgBottomUpOrder`**, which rewrites `_Eentry_list`.

Algorithm of `CgBottomUpOrder`:

```
out = []; inits = []
for e in list:                         # pass 0: functions without a CGNODE go first, in list order
    if e.node == NULL: move e to out
    else: e.node.flags &= ~0x30
while list not empty:                  # repeated sweeps in list order
    for e in list:
        if any callee edge c of e.node with !(c.flags & 1) and !(c.callee.flags & 0x30): continue   # skip: a callee is not placed yet
        remove e; e.node.flags = (e.node.flags & ~0x20) | 0x10        # placed
        append e to (e.SSR+0x20 & 0x400000 ? inits : out)            # dynamic initializers go last
list = out + inits                     # or SsrSortInitializerEvalOrder(inits) when asked
```

A function is placed in the first sweep in which all its non-recursive callees are already placed. Within a
sweep, callees placed earlier in the same sweep count. Without -Og the list is not reordered.

**Test (`ord2.c`).** The source order is `a b c helper2 helper e d`, where b calls helper and helper2 (both
defined later), helper calls helper2, and d calls e. `/O1`, `/O2`, `/Ox` and `/Od /Og` all emit `a c helper2
helper e d b`. `/Od` emits `a b c helper2 helper e d`. **C**

Callee lists exist only when c1 got `-Og`, which is what sets SSR+0x50 0x1000. So **bottom-up order is on
exactly when global optimisation is on**, and `/Ob` and `/Gy` do not change it (also tested) **C**.

### 4.3 The compile loop **C**

```
loop:
    for (pp = &_Eentry_list; (e = *pp); pp = &e->next80)
        if ((e->f50 & 0x20) && !(e->f50 & 2)) break;      # first entry that must be compiled and is not done
    if (!e) { MscStoreMnUnmRatio(); return; }
    e->f50 |= 2; *pp = e->next80;                         # unlink it
    _Entry = e
    func = ReadFunction()
    if (func == NULL) reset frame globals (_Alloc_local = 0, _Alloc_params = 8, ...)
    else {
        if (!(func->flags & FUNCF_DELETED)) func->flags.bit25 = dop2_Codegen()
        EmitFunction(func); endproc()                     # + LTCG progress callback
    }
    goto loop                                             # restart from the HEAD every time
```

Because the scan restarts at the head, a function whose 0x20 bit is set **while** another function is being
encoded is compiled right after that function, even if it comes earlier in the list.

**Test (`ord.c`, `/Od`).** The source order is `helper a helper2 b c helper3 d`, with static noinline helpers
called from b and d. The emit order is `a b helper helper2 c d helper3`: each static is emitted right after its
first caller, and statics found together come out in list order **C**. With `-Og`, `CgPropagateInstantiate`
has already set 0x20 on everything reachable before the loop starts, so the order is just the bottom-up list.

The matching rule: under /O1 and /O2, a function is emitted after every same-module function it calls, unless
recursion breaks the cycle. Ties keep source order, with sweeps as described in §4.2.

## 5. One function: ReadFunction, dop2_Codegen, EmitFunction

### 5.1 FUNC (0xc4 bytes)

`RdrReadProc` 0x10bcb360 calls `getmem(0, 0xc4)`. The result is held in `_Function` 0x10d0d700 and
`_CurFunc` 0x10d123ac. Every `dop2_*` phase receives it in a register (LTCG convention). The fields are in the
header **C**. The important ones:

- `+0x00` entry SSR, `+0x04` hidden struct-return SYM, `+0x08` flow graph, `+0x0c` loop list.
- `+0x14` local symbol table (0x400 buckets).
- `+0x1c` **pragstat**, `+0x28` EH region tree, `+0x44` base line.
- `+0xac` and `+0xb0` flag words. 0x1000000 means "deleted by MscGlobalCtorOpt" and 0x2000000 means "already
  code-generated".

### 5.2 Reading: `ReadFunction` 0x10c06480 **C**

1. `_Module = SSR+8`. Under LTCG, add SSR+0x54 to the progress counter.
2. **`startproc`** 0x10c07a60. It zeroes `_RegUsed[0x81]`; if the function is not outlined it calls `SymInit`
   and sets `_LabelCounter = 1`; it clears `_TupSizeFreeList` (0x104 bytes) and sets `_SdsuIsValid = 1`. It
   also resets `_Fltused`, `_InterfIsBuilt`, `_PostStackPack`, the RTC lists, `_ReturnAddr`, `_GlobMemSym`,
   `_VolatileOpsConverted` and `_fEBPisParam`. Finally `_ExecutionState = 2` and `InlSetConsts`.
3. `PogoFunctionInit` (PGO).
4. **`dop2_GetInitialTuples`** 0x10c06670. It starts with `WvmInitFuncFlags`, then gets the tuples from one of
   three sources:
   - `OtlRetrieveOutlinedProc`, when SSR+0xc0 says the function was outlined;
   - `MSILReadProc`, for a metadata module;
   - otherwise **`RdrReadProc`**:
     1. allocate FUNC and its symtab, and copy the SSR+0x50 bits into FUNC.flags;
     2. create entry/exit tuples (0x2f3) and `FgBuildNullFlowGraph`;
     3. for thunks, call `RdrGenerateThunk`/`PogoGenerateThunk`;
     4. **set `_PIlPtr` = sy, `cur = base + SSR+0x5c`, and `getsym(func)`** to read the locals;
     5. **set `_PIlPtr` = ex, `cur = base + SSR+0x58`**, and make `_ExitLabel` (tuple 0x2f2);
     6. **`RdrGetTuples(func, ..., -1, 1)`**: a byte-coded stack machine (`getattrib`; byte 'M' = end;
        0xff = fatal) that dispatches on the P1 node opcode to `RdrBinaryOps`, `RdrUnaryOp`, `RdrOpEqs`,
        `RdrPragma`, ... and builds tuples (il_tuple);
     7. `RtcEmitInitialization`, `RdrFinalCleanup`, `RdrReadProcEnd`;
     8. LTCG/WPA parameter info and the PGO checksum.

   After the read:
   - `_Symtab = func+0x14`.
   - FUNC.flags & 0x2000 sets `_BottomOpt = _Globopt_flag = 0`.
   - `MscUpdateMnUnmRatio`, then `OmpTransform` if FUNC.flags2 & 0x400000.
   - **`MscRVO`**.
   - With C++ EH (flags & 0x300000): `init_ehinfo`, `preprocess_EH`, `CxxEH_canons`, `postprocess_CxxEH`.
   - With `__try` (SSR+0x20 & 0x1000): `canon_SEH`.
   - With `__asm` (flags & 0x10) under globopt: if `MscCheckAsm` and not asm-only, warning C4740 and globopt
     off for this function.
5. **`dop2_InitialOptsAndSetup`** (§5.4).
6. If `_Globopt_flag`: **`dop2_PreLowerGlobalOpts`**. Its return value goes into FUNC.flags bit 24 (deleted).
7. `RgnRemoveOpleave` if FUNC+0x28.

While RdrGetTuples runs, IL pragma 0x1f stores the per-function option word in FUNC+0x1c and calls
`MscProcessPragstat` (§6). The option flags therefore change **per function**, and they are already set
before any pass runs **C** (RdrPragma 0x10bd1150, case 0x1f).

### 5.3 Phase order

The table lists every call, in order, for a native x86 function: no `/clr`, no PGO, no `/RTC`. **G** means
"only when `_Globopt_flag && !_FWvmSafe`". **S** means "only when `_Speed`". Each row is **C** (read in the
`dop2_*` bodies).

| # | phase (function) | calls, in order | gating |
|---|---|---|---|
| 1 | dop2_GetInitialTuples 0x10c06670 | RdrReadProc, MscRVO, EH canon | always |
| 2 | dop2_InitialOptsAndSetup 0x10c06890 | (PGO) PogoDecorateFG; PtInlGetRemainder (bottom-up and node bit10); MscGlobalCtorPrePass (SSR+0x20 & 0x400000); **Inline**; MscGSShadowPtrParams (-GS); postprocess_SEH; RgnCreateRegionTree; ComputeAddressTaken + SehAddOptConstraints, or EhSyncWalk/EhStateNumbering; WrnEmitWarnings; MscFixGotoLinenumber; RTC checks; RgnCleanupInitialEh | always |
| | | **FgSimpleFoldCmps, FgBuildFlowGraph, ItfBuildInterference** | G (`_Globopt_flag`) |
| | | MscRemoveDeadArguments | always |
| | | FgEliminateTailRecursions (not PGO-instrument), **FgDFOReorderLoopRecog**, MscRemoveAcyclicScopeEntryKills, MdmFlagUnsafeForStackPackParams, **OptFuncExpr**, SymGarbageCollection | G |
| 3 | dop2_PreLowerGlobalOpts 0x10c06cc0 | MscAvoidStoreForwardStalls; **GlobalOptimize** (§5.5); SymGarbageCollection; OptFuncExpr; MscUpdateSeenDynInitializers or MscGlobalCtorOpt (may delete the function); **ItfFuncInterf2**; **Half**; **GrgIdentifyCandidates**; **Opeq**; **SizeOpt**; **Mul2Shift**; CapInsertProbes (-cap/-fastcap); MscConvertVolatileOps; **AddrModes**; `while (FgDoEarlyLateFlowOptsOnce());` **MscBuildQuestion** | whole phase G |
| 4 | dop2_Codegen 0x10c06fa0 | (non-G) CapInsertProbes, MscConvertVolatileOps, AddrModes; EhgStartupSehCodegen (SEH); **InlCollectInfo** (G: records this function as an inlinee); **FgReturnValueMerge** (G); MscTrackParameterAndFunctionUsage / MscCleanupParameterAppearances; SseFloatToXmm (arch SSE/SSE2); **MscSetDeadParamMap** (G, unless bottom-up-eligible node); MscGSRecordVulnerableParameters (G); FgInsertPragmas; PogoPreLower; **Lower**; MscVolatileCleanup; **MscProcessParams** | mixed |
| 5 | EmitFunction 0x10c06540 | skipped entirely if FUNC.flags & 0x3000000; MdmFixupXMMAlignment (flags2 & 8) | |
| 5a | dop2_PostLowerPreRegAllocGlobalOpts 0x10c072b0 | **Peeps**, FgBuildFlowGraph, FgThreadDFOAndFreeDeadCode, FgNumberBlocksHelper, FgFindDominatorsGraph, **LgBuildLoopGraph**, FgRemoveDuplicateArcs, MscRemoveAcyclicScopeEntryKills | G |
| 5b | dop2_GlobalRegAlloc 0x10c07350 | **FgEarlyTailDup** (S, not PGO-instrument), **Globregs**, MdmDetermineDoubleAlignAndFpo, MdmFixEbxIntrinsics, **Color** | G |
| 5c | dop2_CodegenUpdate 0x10c07400 | **RegSimpleAssignBody** (EBP forced unless `MdmMustUseEbpForLocalReg`); **Sizeext** (G+S); PogoSbRemoveDummyDests/PogoSbDefPush (PGO); MdmFlexParams (G); StkSetStackAlignment; MdmInitFrameInfo; MdmSaveAndRestoreCallees; **StkStackAlloc**; SSR+0x60 = FUNC+0x74 + align4(_Alloc_local); **MdmInsertEntryExit**; emit_scopetable (SEH); X87MapGFxToSTx (-QImapX87) | always |
| 5d | dop2_PostCodegenFlowOpts 0x10c07630 | `_KeepLoopLabels = 0`; FgSimpleBranchPredict, FgFreeExistingGraph (G); **FgDoLateFlowOpts** unless FUNC.flags & 0x2000 | |
| 5e | dop2_CodegenFinish 0x10c07690 | **LoFinalLower**; then (G) FgRemoveAllDeadCodeAfterBranchOrReturn, FgBuildFlowGraph, **LateGlobalOptimize**, FgFreeExistingGraph; `_SdsuIsValid = 0`; SehFixFinallyFlow (SEH); LoFinishFinalLower | |
| 5f | dop2_FinalFlowOpts 0x10c07780 | MdmClearStackLevels, then FgDoLateFlowOpts, PogoOptCodeMotion, **FgCodeMotion**, **FgCoalesceLabels**, FgRemoveAllDeadCodeAfterBranchOrReturn | G |
| 5g | (EmitFunction) | MdmWrnMMXFloatMixing (flags & 0x40000000) | |
| 5h | dop2_PreEncode 0x10c07850 | PogoLowerProbes; **MscClean** (G); PogoSbCopyElim; EhRemoveExceptNodes (flags & 0x100000); **Schedule** (G, no SEH, and no 0x100000 EH unless -EHs); **MdmSuperHack** (G); SsePeep (arch SSE and FUNC.flags & 0x20) | |
| 5i | dop2_EncodeAndRelated 0x10c079a0 | PogoRecordDynInstr; **FgLowerSwitches**; MdmInsertProfileCode (-Gh/-GH); **MscRecordInterProcRegInfo** (`_BottomOpt`); MdmEnsureNotEndInCall (G, or SSR+0x20 & 0x400); **EncEmitFunc** | |
| 6 | endproc 0x10c07b40 | ItfFinalCleanup, init_ehinfo, remove_processed_initializers, SymTerm, frees the per-function pools (not while outlining), `_Module = _Function = 0` | |

When G is false (`/Od`), only phases 1, 2 (minus the G rows), 4, 5c, 5d, 5e (minus G), 5h (minus G) and 5i
run. Register allocation is then **only** `RegSimpleAssignBody`: there are no `Globregs`/`Color`, `Peeps`,
`Schedule` or `MscClean` **C**. In non-G mode, `AddrModes` and `MscConvertVolatileOps` move into dop2_Codegen
**C**.

### 5.4 Sub-pass order of GlobalOptimize (globopt.obj 0x10b49300) **C**

1. GoptInitFunc, GoptSimpleConstantFoldFunc, GoptRebuildFlowGraph (if the flow changed), GoptCanonicalize.
2. Only for SEH or 0x100000-EH-without-EHs functions: an SSA pass to derive EH write-through scalars
   (SsaOptimize ... SsaDeriveEHWriteThroughScalars, SsaOptimizeEnd).
3. GoptReverseCopyProp, then HshInit, GdfInitFlowGraph, GdfRemoveDeadStores, GoptSingleUseCopyProp.
4. If `LurWorthIt()` and fewer than 10000 blocks: SsaOptimize, **LurUnroll** (loop unrolling), then the step 3
   group again.
5. GoptConstantFoldCompareBlk over blocks, rebuild, dead stores, region constants.
6. HshBuildTable, GdfInsertArtificialAssigns/Assertions, an SSA range pass (WpaRangeGlobopt), then
   **GoptOptimizeBlock per block** (the CSE/copy-prop sweep), remove assertions, GoptRemoveAssumes, dead stores.
7. GoptRebuildSdsu/HashTable, GdfCalcAvail, LoptInitFunc, **GoptOptimizeLoop per loop** (FUNC+0x0c list),
   LoptTermFunc.
8. GdfCalcAvail, then a second **GoptOptimizeBlock** sweep with GoptLimitCsesFromLoop.
9. Dead stores, rebuilds, GoptLocalCopyPropagation, GoptCleanUp, GoptTermFunc.

`LateGlobalOptimize` (lglobopt.obj 0x10cc9e20, after LoFinalLower) is an SSA pass: LgoTrackHiZero, SsaPeepsMd,
the CleanSsa* sequence, PpSsaDeadStores and the LgoDeadCode/LgoDelete pair **C**.

These passes run in the following order **C**:

- `Inline` 0x10b69110 → `DoInline`, `InlEmitAllWarnings`.
- `Lower` 0x10c3e5e0 → LoLowerArgplace, MdmAlignCallParams, LoIdentifyXmmChain, MdmDeterminePromoteAlign, then
  LoLowerTuple per tuple.
- `Globregs` 0x10b563f0 → CmdInit, CmdIdentifyImmedCandidates, EnumerateSymbols, ReachingOccurGenKill,
  GrgAvoid*, Enterloads, ComputeLifetimes, GrgSpillCheapSyms, CreateGlobregs, RewriteAsGlobregs.
- `Color` 0x10b0cf40 → FPPeeps, SubsumeGlobregs, FoldTooCheapDefs, FPColor, MdmDetermineDoubleAlignAndFpo,
  InitialSplitPoints, Spillcosts, SpillObvious, OrderGlobregs, ..., ChoosePhysReg, RewriteAsReal.
- `EncEmitFunc` 0x10b23700 → EncExpandLateIntrins, EH funclets, EncCondCodes, **EncProcessSymbols** (marks
  referenced functions 0x20, §4.3), SymSetOffsets, MdmEnsureFuncHotPatchable, EncSetOffsets,
  **EncLengthenBranches**, EncEmitSymbols, **Encode**, LiListFunction.

il_flowgraph and the register-allocation families describe these passes in detail.

## 6. Per-function options and /O1 vs /O2

### 6.1 What cl passes (tested with `/Bd`, `t.c`) **C**

| cl | c1 gets | c2 gets |
|---|---|---|
| /O1 | `-Og -Os -Oy -GF` | `-Og -Ob2 -Gy` |
| /O2 | `-Og -Oi -Ot -Oy -GF` | `-Og -Ob2 -Gy` |
| /Ox | `-Og -Oi -Ot -Oy` | `-Og -Ob2` |
| /Od | `-Ot` | `-Ob0` |
| /Og | `-Og -Ot` | `-Og -Ob0` |
| /O2 /Oy- | as /O2 without `-Oy` | as /O2 |
| /GL | | adds `-ltcg` |

All of them also pass `-il <base> -f <src> -W<n> -Gs4096 -dos -GS -MT -Fo...`.

**c2's command line is identical for /O1 and /O2.** The difference reaches c2 only as the per-function
pragstat word in the IL **C**.

### 6.2 MscProcessPragstat 0x10ba26b0 (called from RdrPragma case 0x1f) **C**

```
w = FUNC+0x1c
_DefaultInt = w>>8 & 1;  _StackCheck = w>>31
_Speed = w>>23 & 1            (under PGO-optimize it is saved in _PogoSavedFuncSpeedFlag instead)
if (w & 0x40000) FUNC+0x1c |= 0x1000
_Precision_flag = 1           (always: the "p" in the listing is not /Op)
_BottomOpt   = _CGnodes && (w & 0x200000)
_Globopt_flag = w>>21 & 1     (if _NoCodeGenPass and !globopt: SSR+0x20 |= 0x2000)
_Fpo_flag    = w>>11 & 1
_RTC_Uninit = w>>5&1; _RTC_StackVerify = w>>3&1; _RTC_ShorteningConvert = w>>6&1
if any RTC or _RTC_GZFlag or _EnC_build: _Globopt_flag = _BottomOpt = _Fpo_flag = 0
if (_Globopt_flag && !_FWvmSafe) _Loop_flag = 1   (otherwise left as it was)
if (-noehopt && SSR+0x50 & 0x100): _BottomOpt = _Globopt_flag = _Loop_flag = 0
```

The listing proves it. `LiDumpFuncOpts` 0x10b89220 prints `; Function compile flags: /O{g|d}{t|s}[p][y]
[/RTC..]` from these variables. Results for `pr.c` **C**:

| cl | f1 | f2 `optimize("",off)` | f3 `optimize("s",on)` | f4 `optimize("y",off)` |
|---|---|---|---|---|
| /O1 | /Ogspy | /Odsp | /Ogspy | /Ogsp |
| /O2 | /Ogtpy | /Odtp | /Ogspy | /Ogtp |
| /Od | /Odtp | /Odtp | /Odsp | /Odtp |

### 6.3 Consequences for matching

- **/O1 and /O2 run the same pass list.** The only driver-level difference is `_Speed`, which turns on
  **FgEarlyTailDup** (before Globregs) and **Sizeext** (after RegSimpleAssignBody) **C**. Everything else is
  inside the passes. About 120 functions read `_Speed`; the main ones are LoFinalLower (10 reads), Spillcosts,
  FPSpillcosts, FgDoCommonTailMerge, InitialSplitPoints, GetInlinee/InlineeIsTooBig/InlCallGraphDecision,
  LurWorthIt/LurClassifyNests, the switch lowering (SwSelectType, SwJumpTableSplit, SwMdJumpTableSize), Peeps,
  Lower, EncLengthenBranches, GetSegmentAlign and MdmGenEpilog **C** (list from rg over the decompile).
- c1 also behaves differently: `-Oi` only for /O2, `-Os` vs `-Ot`. Any IL difference c1 makes between /O1 and
  /O2 (intrinsics, for example) comes on top of `_Speed` **I**.
- Bottom-up order, `MscRecordInterProcRegInfo` (callee register sets in CGNODE+0x60) and `InlCollectInfo`
  (callee size after global opts) mean that **a function's code depends on the already-compiled code of its
  same-module callees**. Static functions also get custom conventions through `MscSetDeadParamMap` and
  `MscProcessParams`. For example, in `ipra.c` the static `leaf` takes its argument in **eax** (`lea eax,
  [eax+eax*2]`), and `leaf2`, defined after its caller, is still compiled first **C**. When you match a
  function, the static callees must be matched or present first **I**.

## 7. Switches (getflags.obj, misc.obj)

### 7.1 Parsing

- `MscProcessSwitches` 0x10b97df0 sets defaults, then loops `crack_cmd` 0x10b02ab0 over argv (`_Argc`/`_Argv`).
  Return 6 means unknown: it copies the text and reports C1007 "unrecognized flag '-x' in 'p2'" **C**
  (`/d2bogus` test).
- `crack_cmd` walks `_p2tab` 0x10ceda08 (12-byte `P2SWITCH`, ending at kind 0). It compares wide characters;
  `#` in the pattern means the argument is the rest of the argv or, if that is empty, the next argv. It calls
  `substr` 0x10b02c70, which stores the value by kind: SET1, SET0, ENUM (`SSEtab`), STR, CHARS (`Gtab`
  0x10ced8c8, `Wtab` 0x10ced988), NUM (`getnumber` 0x10b02ec0: wcstol base 0, `fatal(0x31)` on junk), LIST, STR1 **C**.
- cl turns **`/d2<x>` into `-<x>`** on c2's command line. For example `/d2inlT88` becomes `-inlT88` and `/d2Zo`
  becomes `-Zo` **C** (`/Bd`). The same table also holds the options cl itself passes (`-Og`, `-Ob2`, `-Gy`,
  `-il`, ...).
- After the loop, MscProcessSwitches checks name lengths (0x104) and resolves the PGO mode (`-pgi`/`-pgo`/
  `-pgu`/`-pv`). `-Fl` (a CIL list) forces `_Ltcg = 1`. It then packs the MODULE fields (§3.1) and builds the
  listing name (`.cod` with 'c', otherwise `.asm`). `-cap`/`-fastcap`/`-callcap` set `_UserProbe_Flag` **C**.

### 7.2 The table (all 140 entries; target is a public name or a .data VA) **C**

Value kinds: `=1` sets 1, `=0` sets 0, `#n` number, `#s` string.

**Optimisation and inlining knobs** (the ones to try when matching):

| option (`/d2...`) | target | meaning / use |
|---|---|---|
| `-Og` | 0x10d0d15c → MODULE+0x41c | global opts allowed; also gates a path in 0x10be0000 block (7724) when a function has globopt off |
| `-Ob#` | 0x10d19480 → MODULE+0x418 | inline level: clears SSR bit 0x40 (§2.3) |
| `-inlS#` / `-inlT#` | `_InlSizeLimitUsr` / `_InlSpeedLimitUsr` | inliner size / speed budgets (copied by the code at 0x10b60000 block, line 8050) |
| `-inlfcsw#` `-inlflcsw#` `-inlld#` `-inld#` `-inlniln#` `-inlnild#` `-inlnoln#` `-inlnold#` `-inlnobr#` `-inlnlw` `-inlocsa1#`..`-inlocsa4#` `-inlcrmax#` `-inlfcsa#` `-inluserinl#` `-inlmlsa#` `-inlcsw#` `-inlcasw#` `-inldasw#` | `_Inl*Usr` | inliner weights: fold-compare speed weight, loop/new-inner/outer-loop numer/denom, no-branch size adj, one-call size adj 1-4, CR max, few-call size adj, user-inline size adj, max loop size, const/code-addr/data-addr arg speed weights |
| `-vol#` | `_InliningVolume` | `_MaxInlineeSize = 0x10 << vol` when vol < 7 (0x10b60000 block, line 7977) |
| `-Loop#` | `_GoptLoopOptimizeThreshold` | loop optimisation is skipped when the loop is larger (3 uses) |
| `-loopopt` | `_Loopopt_flag` | (no reader found) |
| `-NoLUR` | MODULE+0x454 bit 8 | no loop unrolling |
| `-optref` | `_OptRef` | CgRemoveUnreachable instead of CgPropagateInstantiate |
| `-AlignFuncs` | `_ForceFuncAlign` | align functions even when `_Speed == 0` (0x10c00000 block, line 8737) |
| `-hotpatch` | MODULE+0x454 bit 2 | MdmEnsureFuncHotPatchable |
| `-schdat#` | `_ScheduleData` | scheduler data (no direct reader found) |
| `-Zo` | `_OptCodeDebug` | enhanced optimised debug info |
| `-nodatasort`, `-dshigh#`, `-dslow#` | `_NoDataSort`, `_DataSortHigh/Low` | data symbol sort by size window |
| `-Gs#` | `_StackThreshold` | stack probe threshold (cl passes `-Gs4096`) |
| `-Gt#` | `_DataThreshold` | |
| `-isize#` | `_Intsize` | |
| `-stkfill#` | `_Debug_stackfill` | |
| `-QIfist` `-QIftol` `-QImapX87`/`-QInomapX87` `-QIdivrem64`/`-QInodivrem64` `-QIf` `-QIWMTemu` `-QiPSC`/`-archPSC` | §3.1 bits | x87 / int64 helper choices |
| `-QiSSE` `-QiSSE2` `-archSSE` `-archSSE2` (enum `SSE`=2, `SSE2`=3) | 0x10d0d13c → MODULE+0x450 | `/arch` |
| `-G#` (Gtab) | `h` 0x10d0d16c (-Gh), `H` 0x10d0d170 (-GH), `3`-`8` CPU level, `y` `_CodeInComdat`, `A` `_GenApplication`, `D` `_GenDll`, `T` `_GenFiber`, `S` 0x10d1947c (-GS), `Z` `_RTC_GZFlag`, `X` `_Synchronous_EH` | |
| `-W#` (Wtab) | `0`-`4` level 0x10d0d174, `X` 0x10d19478 | |
| `-EHs` / `-EHa` | `_Synchronous_EH` = 1 / 0 | |
| `-ehopt` / `-noehopt` | 0x10d0d17c | `-noehopt` turns off globopt for EH functions (§6.2) |
| `-useoldhandlers` | `_UseOldHandlers` | 26 uses in the EH code |

**Output, debug and environment:** `-bigobj` (`_ECoff`), `-cbstring`, `-Fd#`, `-Fo#`, `-Fa#`, `-FA#`
(letters c, s, b, u), `-FAf`/`-FAF`, `-Fs#`, `-Fg#` (PGO log), `-Zi`, `-Z7`, `-ZI`, `-ZX`, `-Za`, `-Zl` (=0),
`-Zvc6`, `-H#`, `-S`, `-il#` (`_Basename`), `-f#` (source), `-db#`, `-off#`, `-ef#`, `-dos` (`_Msdos`),
`-MT` `-MD` `-ML` and the `d` variants, `-Bd` (`_Gpfscreen`: let exceptions through), `-Brepro`
(`_Self_build`), `-xoff` and `-errorreport:{queue,prompt,send,none}` (`_DontTrapExceptions`), `-MPdiagMutex`,
`-mnunmrf#`, `-UI#`, `-pchmap#`, `-Yc`, `-Y-`, `-nowarn4715`, `-nowarn4786`, `-Wall`, `-compflag`,
`-initialAppDomain`, `-searchAltPathForScope`.

**Managed, LTCG and PGO:** `-Gil`, `-Gmanaged`, `-clrpure`, `-clrsafe`, `-cef`, `-cvtcil`, `-SIAuthor`,
`-msilDebug`, `-size`/`-speed` (MSIL optimisation: `_MSILOptSize`/`_MSILOptSpeed` choose `_Speed` and globopt
in MSILReadProc), `-Fl#` (CIL list), `-ltcg`, `-omp`, `-pgi#` `-pgo#` `-pgu#` `-pi#` `-po#` `-pv#`,
`-PogoCodeCoverage`, `-PogoNtoskrnl`, `-PogoKmode`, `-PogoNoThunks`, `-cap`, `-fastcap`, `-callcap`, and the
catch-all `*` (ret 6).

The full dump with entry addresses is `work/scratch/glass_ilpipe_p2tab.txt` and the char tables are
`work/scratch/glass_ilpipe_gtab.py` output.

## 8. Open points

- Who sets SSR+0x50 bit 0x20 in the IL for /Od extern functions is not traced. It is written by c1; the
  `keyread` value is not decoded bit by bit.
- The names of SSR+0x50 bits 0x80 and 0x2000 (inline, forceinline) and of the jump-table action for `-Ob1` are
  inferred.
- `MODULE+0x41c` (`-Og`) has a single reader (0x10be0000 block, line 7724), which is not analysed.
- The ex-stream node format read by `getattrib` (attributes, `_Expdat_nodeattrib`) belongs to il_tuple and the
  reader family. Only the outer loop is described here.
- The effect of `MscRecordInterProcRegInfo` on caller register choice was not shown by a test. `ipra.c` showed
  only the eax-argument convention.
