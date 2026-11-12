# c2 stack frame layout: slots, packing order, /GS cookie and parameter shadow copies

Scope: how c2.dll (cl 14.00.50727.762) gives frame slots to locals, temporaries and parameters, in what order,
which ones share a slot, where the /GS cookie goes, and which parameters get a `$GSCopy$` shadow. Module
`stack.obj` (`@StkStackAlloc@4` 0x10be8640 and below) plus the /GS code in `misc.obj` (`MscGS*`) and `mdmisc.obj`.

Every claim is tagged **[C]** (read in the code at the cited address, or seen in a traced/probed compile) or
**[I]** (inferred). "Probe" means a small function compiled with `tools/msvc.py /O2 /FAsc` (sources and listings in
`work/scratch/glass_frame_gs/`, `glass_frame_pk/`, `glass_frame_cl/`, traces in `glass_frame_tr/`).
"Trace" means `tools/c2trace.py` with hooks at the addresses named.

Read with `docs/c2/il.md` and `src/c2/il.h` (SYM, SSR, FUNC, TUPLE, BLOCK layouts). Field names below follow il.h.

---

## 0. Summary for matchers (the rules that predict a listing)

1. **Only memory-resident values get slots.** Stack packing runs after register allocation
   (`dop2_CodegenUpdate` 0x10c07400 calls `StkStackAlloc` after `MdmSaveAndRestoreCallees`). An *element* is the
   base SYM (kind 3 temp, 4 auto, 5 param) of every frame operand left in the code. [C]
2. **Use count** of an element = number of operand occurrences (sources, then destinations, of every instruction
   except `OPSIDEEFFECT`) in the final code, plus members of points-at sets on memory/symset operands. Deleted
   code doesn't count, and neither does an operand folded into a register. [C] `CollectStackElements` 0x10be8f10.
3. **Base order** (one sorted list): size ascending; equal size: use count descending; equal count: whoever reached
   that count first in a forward walk over blocks in layout order and tuples in stream order. [C]
   `AddStackElement` 0x10be9190, probes T1/T2/T2r/T3.
4. **Colors (slots)** are made by walking that list once. Each element joins the **newest** existing slot it is
   compatible with: its size ≤ 2 × the slot's current size, and no conflict. Otherwise it opens a new slot. [C]
   `colorElemental` 0x10beb760. So a char never shares with an int (4 > 2·1), a short and an int can, and an int
   and a double can, but not an int and a 12-byte struct (probes T5c..T5f).
5. **Conflict** = one is live at a definition of the other. Liveness = backward liveness ∧ forward "has been
   defined" (so nothing is live before its first definition). [C] `ComputeLocalLiveness` 0x10be9390,
   `ComputeLiveOnDef` 0x10be9ed0.
6. **Slot order.** If the locals' slot total ("T", sum of rounded slot sizes) is > 128 bytes, or the function has
   a /GS buffer or GS copies, quicksort the slots (K&R, middle pivot, strict) by GS level, then by
   `1000*uses/size` descending (integer division), uses and size being the slot's summed uses and max member size.
   Otherwise keep the creation order. [C] `ColorStackElements` 0x10beb341, `QSortColors` 0x10bebbb0,
   `StkColorCmp` 0x10bebb30; probes T4a/b/c.
7. **Addresses.** In an ESP (FPO) frame, slot 0 ends up nearest esp (lowest address) and the last slot nearest the
   top. In an EBP frame it's the reverse: slot 0 is nearest ebp (highest address). Either way the head of the
   order is nearest the frame register. [C] 0x10beb36e..0x10beb3d8; probe E1 with `/Oy-`.
8. **/GS buffers go next to the cookie.** That means char/short arrays over 4 bytes (level 3), aggregates with
   such a buffer (level 2), and then everything else (level 1). The cookie `__$ArrayPad$` sits at the top of the
   locals, directly below the EH registration node if there is one. [C] `StkGSBufferLevel` 0x10beb730,
   `StkAllocateSecurityCookie` 0x10be8880; probes G1..G3.
9. **Dead parameter homes** take a local only when all of these hold [C] 0x10beb0de..0x10beb24d; probes V*, W*, H1:
   - the function has no /GS buffer and no GS copies;
   - the local's size ≤ the param's (rounded) size;
   - no conflict;
   - the param is "safe": a **cdecl** param counts as safe only if it is read by something other than a plain call
     argument (OPARG/OPMBARG) in a block that dominates the exit (`MdmFlagUnsafeForStackPackParams` 0x10c5ad30);
   - after packing, the slot total of locals not in param homes is < 112 bytes. At 112 or more, all of it is
     undone and redone without param homes.
10. **/O1 only:** FPO is dropped for an EBP frame when the function has more than 4 frame operands (no locals) or
    more than 1 (with locals). [C] `WorthitFPO` 0x10c56260, called from `StackPacking` 0x10be8bf8 only when
    `_Speed == 0`; probe F4/F5/L1/L2 under `/O1`.
11. **GS parameter shadowing lives in c2, not c1xx.** `MscGSShadowPtrParams` 0x10b9b620 runs right after inlining
    in `dop2_InitialOptsAndSetup`. A pointer-like parameter gets `name$GSCopy` iff the function has a GS buffer and
    something derived from the parameter is stored through, used as an address of a store, or passed to a call
    whose callee is not known (from a bottom-up-compiled callee's mask) to be safe for that argument. [C]
    Section 3.
12. **cl.exe `compile()` 0x407d96:** the original almost certainly also shadows `cx`. Its callee
    `limit_batch_files` (0x406082) writes through a pointer loaded from `cx` (0x4060fa), and that alone forces a
    copy (Section 3.6). The original keeps the copy in ebx for the whole function. Ours spills ebx inside the loop,
    and the spill needs a new 4-byte slot (`_cx$GSCopy$`), where a spilled non-copied parameter would just reload
    from its home. The difference is register allocation, not the /GS decision. [C for the rule, I for the original's
    internal state]

---

## 1. Where it runs, and the three allocation paths

`dop2_CodegenUpdate` 0x10c07400 (excerpt, [C] 0x10c00000.c:6660-6690):

```
StkSetStackAlignment();      // 0x10bed0a0: FUNC+0x48 = required stack alignment (0 = default 4)
MdmInitFrameInfo();
MdmSaveAndRestoreCallees();  // callee-saved pushes chosen BEFORE slots
StkStackAlloc();             // 0x10be8640
SSR_FUNC.frameSize(+0x60) = FUNC.frameExtra(+0x74) + align4(-_Alloc_local)
MdmInsertEntryExit();
```

`StkStackAlloc` 0x10be8640 [C]:

1. If `FUNC+0x5c` (the `__$ArrayPad$` SSR) is set, re-scan the live SYMs. If no surviving local or param SSR is a GS
   buffer (`MdmGSBufferCandidate`, Section 2.1) and `FUNC.flags & 0x200` (an alloca-like intrinsic forced a cookie,
   `RdrGetTuples` 0x10bce2a0) is clear, the cookie symbol is freed (`SymFree`, `FUNC+0x5c = 0`). So **a buffer
   that is optimized away takes the cookie with it**. If a buffer survives and globopt is off, warning 0x2ec (C4748)
   is issued. [C 0x10be8658..0x10be8756]
2. `_FNeedFreshStack = _RTC_StackVerify || _EnC_rebuild || _EnC_build` (/RTCs, Edit and Continue). Then:
   - fresh stack: `StkBuildFreshStack` 0x10becfb0, with no sharing [C, not detailed];
   - `_Globopt_flag && !_FWvmSafe` (normal /O1 /O2): `_StkDontPack = 0; StackPacking(FUNC)` 0x10be8b80,
     then `EhCatchObjectAlloc` if `FUNC.flags & 0x100000`;
   - otherwise (/Od): `StkAllocateLocals` 0x10be88e0. This allocates in first-reference order in the tuple stream,
     and temporaries are released at each `OPUSERSTATEMENT` (0x303) and reused (`_Alloc_local` reset to the
     statement start, maximum kept) [C 0x10be8955]. Under /Od the automatics are allocated by
     `AllocateAutomatics` 0x10becf60 in `_LocalSyms` order [I: not traced].
3. `EhFuncletStackAlloc` when `FUNC.flags & 0x40000`.

`StkInitAllocation` 0x10becc50 (called from `Init` 0x10be8e10 and from the /Od path) [C]:

```
_Alloc_local = 0
if (_CanonLocal)                       // EH registration node (0x10d0d730)
    _CanonLocal.offset = allotemp(_CanonLocal.size); mark allocated
    (+4 extra pad under SEH / FUNC+0x52 bit 0 with a non-EBP param frame)
StkAllocateSecurityCookie()            // 0x10be8880
```

`StkAllocateSecurityCookie` 0x10be8880 [C]: if `/GS` (`_Module+0x44c & 0x20000`) and `FUNC+0x5c` is set:
`off = -align4down_from(_Alloc_local - arrayPad.size)`, i.e. `_Alloc_local = -((3 - (_Alloc_local - size)) & ~3)`.
It sets `SSR.offset = off`, `SSR.flags |= 0x800` (allocated) and `SYM.fl5 |= 0x20` (homed). **The cookie is the
first thing below the EH node, so it is the highest local** (`__$ArrayPad$ = -4` in every FPO probe). [C]

`allotemp(size, align)` 0x10c515c0 [C]: `align = min(align, FUNC+0x48 ? FUNC+0x48 : 4)` (`align == 0` is
treated as 4), `size = roundup(size, align)`, then `_Alloc_local = -roundup(-_Alloc_local + size, align)`. It
returns the new `_Alloc_local`, so the frame grows downward and each call returns the slot's base (lowest address).

---

## 2. The /GS buffer test and the cookie symbol

### 2.1 What is a "GS buffer"

`MdmGSBufferCandidate(FUNC, ssr)` 0x10c511f0 [C]:

```
if (FUNC.entry.flags50 & 0x200000)              // a per-function strict mode [I: #pragma strict_gs_check]
    return (ssr.flags & 0x2100) == 0x2100;
return ( ((ssr.attr & 0x40000) && ssr.elemSize(+0x63) <= 2)   // array of 1- or 2-byte elements [I names]
         || (ssr.flags & 0x4000000) )                         // aggregate containing such an array [I]
       && ssr.size > 4;
```

`StkGSBufferLevel(ssr)` 0x10beb730 [C]: 3 for the array case (`attr & 0x40000`, `+0x63 <= 2`, size > 4),
otherwise `1 + (ssr.flags & 0x4000000 != 0)`, so 2 for a buffer-holding aggregate and 1 for anything else.

Probes [C]: `char b[4]` gives no cookie; `char b[5]` and `short b[3]` give one; `int b[2]` gives none. A
`struct { char b[16]; }` local gets a cookie, and `struct { int a; int b[8]; }` does not
(`glass_frame_gs/b.c`: Q4, Q5, Qs3, Qi2, P_struct, P_structint).

### 2.2 Making the cookie

`MscGSMakeArrayPadIfNeeded` 0x10b9c4c0 runs while local SSRs are read (callers at 0x10bb8270 and 0x10c50695). If
the SSR is class 1 (data), has sclass 1 (auto) or 3 (param), /GS is on, `FUNC+0x5c == 0`,
`entry.flags & 0x480 == 0` and the SSR is a GS buffer, it calls `MscMakeArrayPad` 0x10b9c480. That makes a
4-byte int local named `__$ArrayPad`, sets `SYM.fl6 |= 4` (never packed), `FUNC.flags |= 2` and
`FUNC+0x5c = ssr` [C]. A by-value struct parameter that holds a buffer therefore also gets a cookie (sclass 3)
[C code; I not probed].

---

## 3. GS parameter shadow copies (`$GSCopy$`)

**Where:** entirely in c2. `dop2_InitialOptsAndSetup` 0x10c06890 calls `MscGSShadowPtrParams(FUNC)` 0x10b9b620
right after `Inline()` and before the global optimizer, when `_Module+0x44c & 0x20000` [C 0x10c06083]. c1xx only
supplies the type bits it reads. The only string `GSCopy` in all of Bin/ is in c2.dll at 0x10cf9e14, used by
`MscGSMakeProtectedCopy` 0x10b9baa0 [C, `rg -a`].

### 3.1 Data

During this phase the SYM tail (`SYM.rest`, il.h) holds [C, `MscGSInitShadowParams` 0x10b9b820]:

```c
/* SYM fields while MscGS* runs (dop2_InitialOptsAndSetup). Overlaps the stack-packing use of the same bytes. */
struct SYM_GS {
    /* 0x34 */ struct SYM* gsCopy;     /* the $GSCopy local made for this param (MscGSMakeProtectedCopy 0x10b9bc3e) */
    /* 0x38 */ int   vulnerable;       /* 1 = needs a copy (MscGSPropagateVulnerable 0x10b9ba60, MarkParamCands) */
    /* 0x3c */ int   notAddrTaken;     /* init 1; cleared on the equiv chain when a TK_DATAADDR of it is seen */
    /* 0x40 */ SBV*  derived;          /* keys of SYMs whose value derives from this param (ptr params only) */
};
/* globals */
SYMLIST* _MscGSPtrParamSymList;   /* 0x10d11054: candidate params (list nodes {next, SYM*}) */
SBV*     _MscGSVulnerableSymKeysBv;/* 0x10d11050: base keys used "vulnerably" anywhere in the function */
int      _MscGSHasUnsafeBuffers;  /* 0x10d11034 */
/* per callee, in its call-graph node: CGNODE+0x6c = mask of NOT-vulnerable argument positions (bit i = arg i) */
```

### 3.2 Gate (`MscGSIsPtrParamCandidateFunction` 0x10b9be40) [C]

Shadowing is considered only if all of these hold:
- `_Globopt_flag && !_FWvmSafe`;
- `FUNC.flags & 0x10000` (a POGO bit) is clear, `_Wvm_flag == 0`, and `entry.flags & 0x400` is clear;
- `MscInlineAsmAssumesCallingConvention` returns 0;
- some SYM'd local or param SSR (class 1, sclass 1 or 3) is a GS buffer (`MscGSLookForUnsafeBuffersHelper`
  0x10b9be90).

Probe [C]: `H_nobuf` (an `int x[4]` buffer) gets no copy even though it writes through `p`.

### 3.3 Candidates

First, every SYM with `fl7 & 0x20` (pointer-typed) gets `fl6 |= 8` [C 0x10b9b673]. Walk the formals list of the
`TK_ENTRY` tuple. A formal is a candidate if its base SYM has `fl6 & 8`, or its SSR's FE basetype is 3, 4 or 0xc
(pointer, unsigned pointer, ...; il_sym 7) [C 0x10b9b8a0]. **For a varargs function (`entry.flags & 0x40`), the last
named formal is skipped** [C 0x10b9b880; probe Va2 vs NoVa2: `f$GSCopy` disappears]. Each candidate's `derived`
set starts as {itself}.

Probe [C]: an `int` parameter that is later cast to a pointer and passed on also gets a copy (`IntCast`:
`_n$GSCopy$`), while one only passed as `int` does not (`IntPlain`). So pointer-ness comes from any pointer-typed
SYM in the parameter's equivalence chain [I for the exact chain rule].

### 3.4 One forward pass: `MscGSVisitAllAppearancesFwd` 0x10b9bf70 + `MscGSMarkParamCands` 0x10b9c250

The pass visits every instruction once, in tuple-stream order. It recurses into `OPPUSHSTATE` bodies, `OPTUPLIST`
(0x2ce) sublists and `OPASSUME`, and runs these steps in order [C]:

1. For each operand of kind `TK_DATAADDR`, clear `notAddrTaken` on the whole equivalence chain.
2. **Derivation** (`MscGSPropagatePtrUse` 0x10b9c080): for every destination D, every source S and every
   candidate P, if `S.base ∈ P.derived` then add `D.base` to `P.derived`. Memory operands contribute their
   symbol (`TMEM+0x24`), and base and index registers are in the source list. So taint flows through copies,
   arithmetic, **loads through the pointer** and call results. It is one pass with no fixpoint, so a value
   defined later in the stream than its use (a loop back edge) is missed [C, structural].
3. Marking `vulnerable` (adds base keys to `_MscGSVulnerableSymKeysBv`):
   - a machine opcode (`0 < op < 0x273`, inline asm and so on): every operand;
   - `OPASSIGN` (0x28d) or `OPMBASSIGN` (0x29e) whose destination is a `TK_INDIR`, i.e. a store through memory:
     the memref's base and index registers. If its symbol is itself a candidate param, that param is marked
     directly (`+0x38 = 1`);
   - a call (`TK_CALL`): the target operand (a function pointer variable), plus argument *i* (0-based) when
     `MscGSCallArgIsVulnerable(call, i)`;
   - an intrinsic (`TK_INTRINSIC`) whose descriptor has flags `0xc800`: every source;
   - any operand naming a by-value struct param (basetype 6, `ssr.flags & 0x40000000` [I: "contains pointers"]):
     marked if `ssr.flags & 0x4000000`, or if any member of its chain has `fl6 & 8`.
4. After the pass (`MscGSPropagateVulnerable` 0x10b9ba60): **P is vulnerable iff `P.derived ∩ vulnerable ≠ ∅`.**
   Then a basetype-6 param without `0x40000000`, and the varargs last formal, are reset to 0.

`MscGSCallArgIsVulnerable(call, i)` 0x10b9c5f0 [C]:

```
if (i > 31) return true
t = call.src[0]                                  // target
if (t.kind == TK_CODEADDR) callee = t.ssr
else if (t.kind in 1..4 && t.sym.base.ssr && (ssr.attr & 0x400 /*dllimport*/)) callee = that ssr
else return true                                  // indirect call: always vulnerable
if (callee.cls == 4 && (callee.attr & 0x4000 /*has body here*/) && _BottomOpt
    && callee.cg && (callee.cg.entry == callee || POGO alias) && callee.cg.mask(+0x6c) != 0)
    return (callee.cg.mask >> i & 1) == 0
return true                                       // unknown callee, or mask == 0
```

The mask is written by **`MscGSRecordVulnerableParameters`** 0x10b9c520. It runs for every function compiled with
global optimization, cookie or not, late in `dop2_Codegen` (0x10c07230, after optimization). It runs the same
analysis on the optimized IL and sets bit *k* for each formal that is **not** vulnerable. Bit 0 is the hidden
struct-return pointer when there is one, and the formals follow it [C]. Because c2 compiles bottom-up
(il_pipeline), a callee defined **anywhere** in the same compilation (even after the caller) usually has its mask
ready. Probes [C, `glass_frame_gs/b.c`]: `D2_rdg` (the callee only reads `*p` into a global) gets no copy;
`K2_later` (the same, but the callee is defined after the caller) gets no copy; `D5_wrg` (the callee writes `*p`)
and `D4_taint` (the callee passes `*p` to an external function) get copies.

Consequences:
- A mask of 0 means "unknown", so **a one-parameter callee whose parameter is vulnerable, or any callee all of
  whose params are vulnerable, makes the argument vulnerable in every caller**, whatever the mask says. [C]
- Reading through the param and storing to a global or a local is not vulnerable (`B3_loadglob`,
  `B4_cmp` [C]). Passing a value *loaded through* it to an unknown call is vulnerable (`B2_loadext`, `G_int` [C]).
- A dllimport or external callee is unknown (no body), so it is always vulnerable (`C_ext` [C]).

### 3.5 The copy

`MscGSMakeProtectedCopy` 0x10b9baa0 [C]: it sets `FUNC.flags |= 1` and creates a local named `"%s$%s"`, i.e.
`p$GSCopy` (the listing appends `$`). Its type and size are the param's; for a struct, `size = ssr.size`. It
inserts `copy = param` (an `OPASSIGN`, or a block copy via intrinsic 0xac for a large struct) **before the first
instruction of the function**. A copy is made the same way for the hidden return pointer (`FUNC.retSym`, a fresh
user var). Then `MscGSReplaceVulnerableAppearances` 0x10b9bed0 rewrites every use of the param into the copy
(`SymMakeEquiv` on `SYM+0x34`).

The copy is an ordinary local from then on. **If the register allocator keeps it in a register for its whole life
it leaves no trace in the frame**, apart from the early `mov reg, [param]` at entry (probes `A_write`, `C_ext`:
`mov esi, _p$` before the first call, same `sub esp` as the uncopied `B_read`) [C]. Only a spill gives it a slot
(`Many`: `_e$GSCopy$`, `_f$GSCopy$` at the lowest addresses) [C]. It is a GS level 1 element, so it is sorted away
from the buffers, towards esp in an FPO frame.

### 3.6 The cl.exe `compile()` case (0x407d96)

Our `compile(context_s* cx)` (src/cl/compile.cpp, `/O1 /MD /Gy /TP /Zc:wchar_t-`) has `wchar_t buf[0x400]`, a
level 3 GS buffer. One-callee probes (`glass_frame_cl/probe.cpp`, appended to a copy of compile.cpp) show that
passing `cx` to `limit_batch_files`, `mr_prepare`, `mr_next`, `compile_batch` or `post_compile` makes it vulnerable.
`build_full_worklist` is the only callee that leaves it alone, and reading `cx->uptodate` doesn't count [C]. The
original's `limit_batch_files` (0x406082) loads `[cx+0x20]` and stores through the derived pointer
(`and word ptr [esi], 0` at 0x4060fa). Its only parameter is therefore vulnerable, its mask is 0, and by 3.4
**any caller passing `cx` to it must shadow `cx`** [C for the rule and the original's bytes].

The original compile() holds `cx` in ebx from 0x407db6 to the end and never reloads `[ebp+0x7ac]`. That fits a
copy living in ebx throughout [I]. Ours copies it into ebx too, spills ebx in the loop and reloads from
`_cx$GSCopy$` at +0xa8. A spilled non-copied param would reload from its home and need no extra slot, which
explains our extra 4 bytes (0x828 vs 0x824) [C our listing]. **The lever to look for is what takes ebx in our loop
(register pressure or priority), not anything that removes the copy.** That also explains why "a local copy of the
parameter" and "moving buf into the inner block" were neutral: neither changes vulnerability.

---

## 4. Stack packing (`StackPacking` 0x10be8b80)

### 4.1 Data

```c
/* SYM fields during StackPacking (they reuse SYM.rest; il.h's hshIndirNext at +0x30 is dead by now) [C] */
struct SYM_STK {
    /* 0x30 */ struct SYM* elemNext;  /* _symStackElements list (AddStackElement 0x10be9190); also _symParams chain */
    /* 0x34 */ struct SYM* elemPrev;
    /* 0x38 */ int   uses;            /* reference count (AddStackElement) */
    /* 0x3c */ int   index;           /* element number = bit index in every SBV below (EnumerateStackElements) */
    /* 0x40 */ u8    stkflags;        /* bit 0 cleared in Init; must be set to escape _StkDontPack [C 0x10be9445] */
};
/* SYM.fl6 & 4: "never share" (SbvSetAll on its conflict set). Set by MdmFlagUnsafeForStackPackParams
   (cdecl params, 4.5), MscMakeArrayPad (the cookie), AddStackElement (FE basetype 0xc, fl6 & 0x40),
   double-align decisions (CgNewDoubleAlignDecision 0x10b06f90), EH/SEH helpers, _StkDontPack. [C] */

typedef struct STKCOLOR {          /* _Colort 0x10d0fa9c, getmem(7, n*0x1c) in ColorStackElements [C] */
    /* 0x00 */ SBV*  members;      /* element indices in this slot */
    /* 0x04 */ SBV*  interf;       /* union of LiveOnDef[m] over members */
    /* 0x08 */ int   size;         /* params: align4(ssr.size); locals: max member SYM.size (not rounded) */
    /* 0x0c */ int   uses;         /* sum of member uses */
    /* 0x10 */ int   paramOff;     /* params: ssr.offset (the home) */
    /* 0x14 */ int   align;        /* max over members of min(1 << (SYM.alignCode-1), cap) */
    /* 0x18 */ u8    gsLevel;      /* StkGSBufferLevel of a GS-buffer member, else 0 (set only in GS functions) */
    u8 pad[3];
} STKCOLOR;                        /* 0x1c */

typedef struct OVERLAY { int off; int size; SYMLIST* syms; SBV* keys; struct OVERLAY* next; } OVERLAY;
/* 0x14 bytes, one per color, list _OverlayList 0x10d0dd38 (debug info / EnC; keys only under POGO) [C 0x10beb3e3] */

/* globals [C, PDB publics] */
SYM*  _symStackElements;  /* 0x10d0fab0 */   int _numOfStackElements; /* 0x10d0faac */
int   _numOfParameters;   /* 0x10d0faa8 */   SYM** _symNumToSym;      /* 0x10d0faa4 */
SBV** _bvLiveOnDef;       /* 0x10d0faa0 */   STKCOLOR* _Colort;       /* 0x10d0fa9c */
int   _numOfColors;       /* 0x10d0fa98 */   int _EbpAdj;             /* 0x10d0fa94 */
int   _AlignPad;          /* 0x10d0fa90 */   SYM* _symParams;         /* 0x10d0fa8c */
int   _StkDontPack;       /* 0x10d0fa88 */   SBV* _bvParameters;      /* 0x10d0fa84 */
SSR*  _CanonLocal;        /* 0x10d0d730 */   int _Alloc_local;        /* 0x10d0d728 */
SBV*  _BvCantPackInScope; /* 0x10d11698 */   int _PostStackPack;      /* 0x10d0d894 */
/* FUNC: +0x48 stack alignment (0 = 4), +0x4c EBP adjustment, +0x5c __$ArrayPad SSR, +0x60/+0x64 frame
   register SYMs for params/locals (&_RegSym[5]=esp 0x10d0a150, [6]=ebp 0x10d0a1b0, [4]=ebx 0x10d0a0f0),
   flags +0xac: 0x1 GS copies made, 0x2 has ArrayPad, 0x40 FPO, 0x100 alloca/setjmp-like intrinsic,
   0x200 cookie forced. [C: MdmSetFramePointerRegs 0x10c51f30, RdrGetTuples 0x10bce2a0, MscMakeArrayPad] */
```

### 4.2 Steps, in order [C]

1. `Init` 0x10be8e10: zero the globals, `unalloc` every SYM, `StkInitAllocation` (EH node, cookie), and chain all
   kind-5 SYMs on `_symParams`.
2. `CollectStackElements` 0x10be8f10: walk **blocks in list order, tuples in stream order**, instructions only,
   skipping `OPSIDEEFFECT` (0x2d9; the scope-entry kills of 4.4 are side effects and don't count). For each
   instruction take **sources first, then destinations**. Each `TK_SYM`/`TK_DATAADDR` operand whose base is kind 3,
   4 or 5 counts once, and so does each `TK_EA`/`TK_INDIR` with a symbol. A `TK_SYMSET` inside an `OPTUPLIST`
   (0x2ce) counts every member of its points-at set. Each count calls `AddStackElement(base)` (except
   `_CanonLocal`).
3. `AddStackElement` 0x10be9190 keeps the list sorted:
   - a new element goes at the **end of its equal-size run** with `uses = 1`;
   - an existing one does `uses++` and moves backwards past predecessors of the same size with **strictly smaller**
     `uses`.
   The result is size ascending, uses descending, and among equal uses the element that reached the count first
   ahead.
4. `EnumerateStackElements` 0x10be92d0: index elements in list order (`SYM+0x3c`) and put params in `_bvParameters`.
5. FPO check (/Os only): if `FUNC.flags & 0x40` and `!DAT_10d40c58 && _Speed == 0 && !_fEBPisParam`, and
   `WorthitFPO(nLocalElems + (_CanonLocal != 0))` 0x10c56260 is false, then clear 0x40 and switch to an EBP frame
   (`MdmSetFramePointerRegs`). `WorthitFPO` counts `OPLOCAL`/`OPLOCALSCALED` operands among dst[0], src[0] and
   src[1] of every instruction in the function, and fails once the count exceeds **4 with no local elements, or 1
   with any**. Probes under `/O1` [C]: 4 param reads stay FPO and 5 get `push ebp; mov ebp, esp`; a local with 1
   reference stays FPO and one with 2 does not. Under `/O2` (`_Speed = 1`) this check never runs.
6. If there are no local elements, stop here (params only).
7. `ComputeLocalLiveness` 0x10be9390 and `ComputeLiveOnDef` 0x10be9ed0 (4.3).
8. `ColorStackElements(FUNC, nonPacked)` 0x10beaef0 (4.5 to 4.8), then free the sets and set `_PostStackPack = 1`.

### 4.3 Liveness and conflicts [C]

Per block (`BLOCK` +0x24..+0x3c, seven SBVs over element indices):
- `use` (+0x24): upward-exposed reads. Direct `TK_SYM` reads not yet killed in the block, plus points-at-set (PAS)
  members of `TK_INDIR`/`TK_SYMSET` sources (the PAS comes from `_PointsAtSets`, i.e. interf.obj).
- `defd` (+0x28): defined in the block: direct writes plus PAS members of memory/symset destinations, which are
  **may-defs**.
- `kill` (+0x2c): direct whole-variable writes only (the leaf's SYM is the base itself, the leaf flag `0x08` at +0x15
  is clear, and the element is not in `_BvCantPackInScope`, which is EH try-scope aliasing from
  `ProcessEhAliasing` 0x10b7e9f0).
- Backward fixpoint over blocks in reverse list order: `out = ∪ succ.in; in = (out − kill) ∪ use`.
- Forward fixpoint: `dIn = ∪ pred.dOut; dOut = dIn ∪ defd`. In the entry block params are defined.
- Finally `in &= …`, `out &= …` (`SbvAnd` 0x10be9e30): **an element is live only where it is live and has been
  defined**.

`ComputeLiveOnDef` walks each block backwards from its live-out. It first seeds `LiveOnDef[e] |= liveIn(entry) −
{e}` for the entry live set [C 0x10be9f84]. Then at each definition of element e (direct, or a PAS member of a
destination) it does `LiveOnDef[e] |= live` (0x10bea6bc and 0x10bea457). A whole definition, or the first
definition in the block (leaf flag 0x80 from the first pass), removes e from `live`. For calls (`TK_CALL`,
`OPTUPLIST` 0x2ce, opcode 0x1bf) the sources are made live before the destinations are processed, so call
arguments conflict with whatever the call defines. One special case: an `lea` (machine op 0x12) of a local whose
single-definition result feeds a push-type op (0x2f, 0x30 or 0x33) makes everything live there conflict with that
local (0x10bea7ec..0x10bea821).

**conflict(a, b) ⟺ a ∈ LiveOnDef[b] ∨ b ∈ LiveOnDef[a]**: a coloring tests both (`E ∈ c.interf`, then
`SbvAnyCommon(LiveOnDef[E], c.members)`, at 0x10beb180..0x10beb1b7 and in `colorElemental`).

### 4.4 Address-taken locals, scopes, "escape" [C behaviour, I mechanism]

The stack packer never sees C scopes. It sees (a) PAS membership on memory and call operands and (b) the
**scope-entry kills**:
- `ItfRemoveScopes` 0x10b77c10 deletes the front end's `OPBLKSTART`/`OPBLKEND` pragmas (0x2fa/0x2fb). At a
  block's start it inserts an `OPSIDEEFFECT` that fully defines each declared local meeting all of: kind 4,
  address-taken (`fl5 & 2`), **size > 16**, and a membership bit at `ssr+0x5f`. It then sets
  `FUNC.flags |= 0x4000000`.
- `MscRemoveAcyclicScopeEntryKills` 0x10ba2540 deletes those kills again in blocks with `BLOCK+0x16c == 0` (not in
  a loop [I]).
- So only large address-taken block locals inside loops get a lifetime start at each scope entry. Everywhere else
  the forward "defined" set does that job.

What decides whether an address-taken local stays live after its scope is the PAS of later operations, and that
PAS is computed in interf.obj (`ItfRepairScopes` 0x10b74320 records each local's scope at `SYM+0x34`) [I]. Observed
[C, probes and a trace]:
- `{ int a; ext(&a); } { int b; ext(&b); }` share a slot (T5a, R2). The trace shows `a` only in the first call's
  PAS and `b` only in the second's (two PAS definition events at 0x10bea457).
- `{ int a; ext(&a); g = a; } { int b; ext(&b); g = b; }` do **not** share (R1). Six PAS definition events: both
  locals are in every call's PAS, so `a` stays live through the second block. **A local that escapes and is also
  referenced directly afterwards joins the PAS of every later opaque memory operation, whatever its scope.**
- Function-scope `int a, b; ext(&a); ext(&b);` don't share (T5b).
- A visible `static __declspec(noinline)` callee that only reads `*p` did not make R1's shape share (R3).

### 4.5 Parameter homes (only before 4.6) [C 0x10beaf50..0x10beb24d]

Each parameter element becomes its own color first, in list order: `size = align4(ssr.size)`, `uses = uses`,
`paramOff = ssr.offset`, `members = {p}`, `interf = LiveOnDef[p]`, or *all* if `fl6 & 4`. `nParamColors` is the
number of params, forced to 0 if `FUNC.flags & 0x100` (alloca/setjmp-like intrinsic) or `FUNC+0x52 & 1`.

Then for every non-param element E in list order, try each param color c in order:
- skip c if `E.size > c.size`, or if the function has a cookie (`/GS && FUNC+0x5c`) or GS copies
  (`FUNC.flags & 1`). So **a function with a /GS buffer never reuses a param home** (H1 with and without `/GS-`);
- skip c on a conflict;
- otherwise E joins c (`members += E; interf |= LiveOnDef[E]; uses += E.uses`) and E is done.

If no param color takes E, E goes to `colorElemental` (4.6).

**Undo rule** [C 0x10beb25e..0x10beb30d]: in an ESP frame, if at least one local went into a param home and
the slot total T of the local colors is ≥ 0x70 (112), every color is freed and `colorElemental` is rerun on all
non-param elements with `_numOfParameters = 0`. Probes W1/W2/W3 [C]: a 104- or 108-byte struct leaves `x` in
`p`'s home, a 112-byte one moves `x` into the frame. EBP frames never undo.

**cdecl "unsafe" params** (`MdmFlagUnsafeForStackPackParams` 0x10c5ad30, called in `dop2_InitialOptsAndSetup`
at 0x10c06208 on unoptimized IL) [C]: only when `entry.flags & 0x1c == 0` (cdecl; stdcall and fastcall are exempt).
A parameter stays packable iff some instruction **other than `OPARG` (0x28a) / `OPMBARG` (0x29d)** reads it in a
block that dominates the exit (`BLOCK+0x178` tested against the exit block's dominator set at `+0x15c`). Every other
cdecl param gets `fl6 |= 4`, so its home is never shared. Probes [C]:

| function | param read as | home reused |
|---|---|---|
| `V1(int p){int x; ext2(p); ext(&x); return x;}` | call argument only | no |
| `V9` = the same with `ext2(p + 1)` | an add | **yes** (`_x$ = 8`) |
| `V5`: `ext2(p + 1); ext2(p);` | add + argument | yes |
| `V10(int p,int n){int x; ext3(p,&x); return x==n;}` (cdecl) | argument | no |
| `V6`: the same body, `__stdcall` | argument | **yes** |

(The trace of V1 at 0x10beaf5a shows `p.fl6 = 0x06` vs `0x02` in V9, and the param color's interference test
fails at 0x10beb18d.)

### 4.6 Local colors: `colorElemental(E, T)` 0x10beb760 [C]

```
for c = numColors-1 downto numParams:            // newest first
    if E.size <= 2*c.size and !conflict(E, c):
        c.members += E; c.interf |= LiveOnDef[E]; c.uses += E.uses
        if c.size < E.size: T += align4(E.size) - c.size; c.size = E.size
        if GS function and E is a GS buffer: c.gsLevel = StkGSBufferLevel(E)
        c.align = max(c.align, min(1 << (E.alignCode-1), cap))
        return T
new color: members={E}, interf=copy(LiveOnDef[E]), size=E.size, uses=E.uses,
           align=min(1<<(E.alignCode-1), cap), gsLevel as above
return T + align4(E.size)
```

`cap = FUNC+0x48 ? FUNC+0x48 : 4`. Observed alignments [C]: char 1 (`_c$ = -5`), short 4, int and double 4.
Note that T adds `align4(new) − old_raw`, so a slot grown from 1 byte to 2 counts 3 extra bytes. T can therefore
exceed the real frame slightly, and that can move the 112 and 128 thresholds [C arithmetic, not probed].

### 4.7 Ordering [C]

(All four builds, their differences, the `slots` report and the replay model: docs/c2/slots.md.)

`bEbp = (FUNC+0x64 == &_RegSym[ebp])`, i.e. locals addressed from ebp.

```
if (T > 0x80 || (/GS && FUNC+0x5c) || (FUNC.flags & 1))
    QSortColors(numParams, numColors-1, dir = bEbp)          // params are never moved
```

`QSortColors` 0x10bebbb0 is K&R's: `swap(lo, (lo+hi)/2); last = lo; for i in lo+1..hi: if cmp(a[lo], a[i]) < 0:
swap(++last, i); swap(lo, last); recurse(lo, last-1); loop on (last+1, hi)`. `StkColorCmp(pivot, x)` 0x10bebb30:

```
if pivot.gsLevel != x.gsLevel:
    return (pivot.gsLevel > x.gsLevel) == dir ? +1 : -1      // ESP: lower levels move left; EBP: higher levels left
kp = 1000*pivot.uses / pivot.size;  kx = 1000*x.uses / x.size  // signed int division
return kp == kx ? 0 : (kp > kx ? +1 : -1)                     // x moves left iff kx > kp (strict)
```

So after the sort, index order is GS level (ESP: ascending; EBP: descending), then key descending. Equal keys keep
whatever order partitioning leaves them in (unstable). Probes [C]: T4a/T4b (T = 124, 128) keep the base order
(`_a$` nearest esp); T4c (T = 132) sorts the 128-byte struct with 40 uses (key 312) below the int (key 250). G3:
with /GS the 8-byte buffer goes next to the cookie although the struct has fewer uses; with `/GS-` the base order
puts the buffer lower.

### 4.8 Offsets [C 0x10beb36e..0x10beb6ef]

```
order = bEbp ? 0, 1, ..., n-1 : n-1, ..., 1, 0
for c in order:
    base = (c < numParams) ? c.paramOff : allotemp(c.size, c.align)
    for m in c.members:
        off = bEbp ? -roundup(-base - (c.size - m.size), align(m))  // smaller member at the TOP end of the slot
                   : base                                            // every member at the slot base
        auto: ssr.offset = off, ssr.flags |= 0x800;  temp: SYM+0x28 = off;  SYM.fl5 |= 0x20
if (_EbpAdj) FixOffsetsForEbpAdj()
```

So in an ESP frame the last color is allocated first (highest address, just below the cookie) and color 0 last
(nearest esp). In an EBP frame color 0 is allocated first (nearest ebp). Probe E1 (`int a,b,c`, uses 1/3/2)
[C]: `/O2` gives b -12, c -8, a -4, and `/O2 /Oy-` gives a -12, c -8, b -4.

### 4.9 EBP adjustment (`lea ebp, [esp-N]`) [C code, I for some fields]

When T > 0x80, `TryEbpAdjust` 0x10bebd40 runs. Its legality test, `EbpAdjIsLegal` 0x10bebe50, requires:
- `/Oy` on, no POGO pass 1, no inline asm that modifies ebp;
- no must-align flag (`FUNC.flags & 0x20000000`), and `entry.flags & 0x400` clear;
- both params and locals addressed from ebp, and `FUNC+0x48 == 0`.

The first candidate is `_EbpAdj = 0x78 − entry+0x44` [I: +0x44 is the distance from ebp to the frame base]. It is
kept if `WorthitEbpAdj` 0x10bebeb0 finds that the uses reachable with a disp8 (`[ebp−0x80, ebp+0x80)`, each color
weighted by its uses, prorated when it straddles the window, `StkColorWeightForEBPAdj` 0x10bec120) gain at least 3
over no adjustment (15 more in some SEH /Os cases). In a GS function a second candidate puts ebp so that the GS
buffer colors and the next one fit the window: `min(gsBytes − _Alloc_local + 0x80, T − _Alloc_local − 0x80)`
rounded to 4. `FUNC+0x4c = _EbpAdj`, and `FixOffsetsForEbpAdj` 0x10bec220 rebiases every frame offset. cl.exe's
`compile()` (`lea ebp, [esp−0x7a8]`) is this second case [I].

---

## 5. Worked prediction recipe

1. Take the final instruction stream, after register allocation, with the blocks in layout order. List every
   operand that is a stack slot (`_x$`, `$T`, spilled `tv`), with its size and SYM alignment.
2. Count references per slot (sources then destinations, per instruction). Sort by size ascending, then count
   descending, then first to reach the count (rule 3).
3. Work out the conflicts: liveness from the first definition to the last use. An escaped local that is also
   referenced directly stays live through every later opaque call (4.4).
4. If there is no /GS buffer, try param homes first (4.5, including the cdecl rule). Then color: each element
   joins the newest compatible slot under the 2× rule. Compute T. In an ESP frame with T ≥ 112 and any param-home
   use, redo without param homes.
5. If T > 128 or the function has GS, sort by (GS level, `1000*uses/size`) with the K&R partition.
6. Lay out: ESP frame from the last slot down to slot 0 (slot 0 nearest esp); EBP frame from slot 0 down. The
   cookie (and the EH node, if any) are above all slots.

---

## 6. How this maps to the empirical levers

M&B `docs/codegen.md` §7 and vs2005 `docs/codegen.md` §2.

**Explained (and now exact):**
- "/O2 homes come from a conflict graph over lifetimes, ordered by increasing size then decreasing use count;
  parameters are scanned separately": rule 3, 4.5 and 4.6. "Declaration order is irrelevant": no input of
  StackPacking depends on it.
- "Ties go to the local whose *last* reference comes earliest in evaluation order": AddStackElement's
  strict-`<` promotion. The element whose n-th (last) reference comes first reaches n first. "Call arguments
  evaluate right to left" is the IL's push order.
- "Large frames: K&R quicksort, middle pivot, strict `>`, key `1000*uses/size`, seeded from the base order": exact.
  The seed is the color creation order, the key uses the slot's summed uses over its max member size, and the
  division is signed integer (floored for these positive values, which is why floored and exact ratios matched).
- "The switch is in the 128..143 bucket": it is `T > 128`, where T is the rounded slot total (cookie, EH node,
  callee-saved registers and param homes excluded). The 128 bucket "fit both models" because T = 128 does not sort.
- "Each local joins the newest compatible group" (start_setup refinement): `colorElemental` scans newest first.
  The untested "disjoint locals merge with the largest member's size" is right, with one extra condition: a member
  can be at most twice the slot's current size.
- "Slots order by the total uses of the merged group" (rule 5): true **only for sorted frames** (T > 128 or GS).
  Below that, slots keep creation order, i.e. the position of their first member.
- "Dead incoming-argument slots hold locals", "a dead parameter's home is overlaid by a block-scoped copy, first
  free home wins", "dead parameter homes hold address-taken locals" (`WriteFile(..., &written)`): 4.5. Params are
  tried in list order (size, then uses), not by stack position. It never happens with a GS buffer, nor for cdecl
  params that are only forwarded as call arguments, nor when the other locals total ≥ 112 bytes.
- "...that can force an ebp frame" (vs2005, `/O1`): reusing a param home puts more frame operands into the code,
  and `WorthitFPO` gives up FPO beyond 1 (with locals) or 4 (without) frame operands under /Os.
- "Block-scoped locals share stack slots; function-scoped ones don't" (vs2005 `start_setup`, M&B rule 3/4):
  liveness ∧ defined, plus 4.4. "Address-taken and read after the escape never packs; address-taken but never read
  after the escaping call packs" (M&B 600-case probes): exactly R1 vs R2 (4.4).
- "A spilled variable keeps one slot for all its webs": elements are base SYMs. Several webs of one variable are
  one element with one slot. Separate variables are separate elements and may or may not share.
- "Dead references don't count; folded references add no use; use counts are taken before tails are duplicated":
  counts come from the final post-RA instruction stream. Tail duplication happens later in this phase order [I].
- "An output dword local must be `uint32_t`, not a 12-byte struct" (size decides the run and the 2× rule);
  "a dword flag must be `int`, not `bool`": a bool/char element is 1-byte aligned and can't share with a 4-byte
  slot (4 > 2·1).
- `16-byte alignment, and esp,-16`: `FUNC+0x48` caps and raises slot alignment (`allotemp`, `colorElemental`).
  `StkSetStackAlignment` 0x10bed0a0 sets it [I: not read in detail].
- vs2005 "compile: the /GS parameter shadow copy": Section 3.6. The copy is (almost certainly) present in the
  original too; the mismatch is ebx being spilled in our loop.

**Contradicted or refined:**
- vs2005 "**Stack-slot order in a big function follows the declaration order of hoisted locals**"
  (`handlematch`): declaration order is not an input to the packer [C]. What hoisting changes is liveness
  (scope-entry kills, 4.4) and therefore the colors. Declaration order can still matter indirectly through the
  register allocator, whose SYM keys follow `getsym` order and decide which variables spill [I]. Expect that
  permuting the hoisted declarations among themselves is neutral; test before relying on it.
- M&B "escape is interprocedural: a callee defined in the TU lets the temporaries pack": with a local that is read
  after the call, a visible `static __declspec(noinline)` callee did **not** let two block locals share (R3).
  Interprocedural escape may matter only for locals with no later direct reference (as in M&B's `$T` probe), or
  through PAS details in interf.obj that weren't read here. Open.
- M&B "the order did not depend on declaration order in 24 shuffles": consistent.

---

## 7. Open questions

- The exact PAS rule (interf.obj): when exactly an escaped local enters the points-at set of later calls (direct
  reference after the escape? scope via `SYM+0x34`?), and why the visible callee in R3 didn't help.
- `SSR+0x63` (element size?) and the meaning of `ssr.flags & 0x4000000` / `0x40000000` / `0x2100`. Their effects
  are pinned by probes, their names are inferred. `entry.flags50 & 0x200000` (strict GS?) and `entry.flags & 0x400`
  / `0x480` are unknown.
- Why short locals get 4-byte alignment (`alignCode` set where?). Read `SymMakeUserVar` / `StkSetStackAlignment`.
- `TryEbpAdjust`'s field `SSR_FUNC+0x44`, and the GS variant's exact window. Probe a >128-byte /O1 function and trace
  `_EbpAdj` (0x10d0fa94).
- `StkBuildFreshStack` (/RTCs, EnC) and the /Od `AllocateAutomatics` order weren't read in detail.
- Whether `MscGSRecordVulnerableParameters`' bit numbering matches the caller's argument index for
  `__thiscall`/`__fastcall` callees whose first args travel in registers. If the call's source list holds only the
  pushed args, the bits are shifted. Untested.
