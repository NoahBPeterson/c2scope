# c2.dll intermediate language: overview

This page is the entry point to c2's IL: how a function travels through the back end, and which data
structures carry it. Read it top to bottom. Each section ends with a link to the per-family page that has the
full evidence:

| family | spec | per-family header |
|---|---|---|
| driver, phases, switches | [il_pipeline.md](il_pipeline.md) | `src/c2/il_pipeline.h` |
| tuples (instructions and operands) | [il_tuple.md](il_tuple.md) | `src/c2/il_tuple.h` |
| symbols, ids, types | [il_sym.md](il_sym.md) | `src/c2/il_sym.h` |
| flow graph, loops, regions, DAG, tail merging | [il_fg.md](il_fg.md) | `src/c2/il_fg.h` |

**One header for all of it: `src/c2/il.h`.** It holds one definition of each struct. Where the per-family
headers disagreed, the conflict was settled by re-reading the code (see §6). It compiles under cl
14.00.50727.762 with every size and offset assert passing
(`uv run tools/msvc.py /nologo /c /TC /Zs work/scratch/glass_ilmerge_hdr.c`). A negative test with a
deliberately wrong offset fails as it should (`work/scratch/glass_ilmerge_neg.c`, error C2118). Don't include
il.h together with `src/c2/tuple.h`: that file is the small TUPLE stub that `tuple.c` uses for byte matching.

**Target and tags.** The target is `Bin/c2.dll` from cl 14.00.50727.762. Addresses are c2.dll VAs (image base
0x10b00000). **C** means CONFIRMED: read in the code at the cited address, or shown by a test compile.
**I** means INFERRED. The `/O2` and `/O1` claims are about the default x86 native path: no `/clr`, PGO or
`/RTC`.

---

## 1. The big picture

```
c1 (front end) ──writes──> <base>gl  <base>sy  <base>ex  <base>in     (or .cil$* sections under LTCG)
                                │        │        │
c2: MainContReal ── read gl header, global SSRs; open "in"           -> MODULE, SSR (front-end symbols)
    dop2 loop: take next function from _Eentry_list (bottom-up order under -Og)
      ReadFunction: sy -> local SSRs, ex -> TUPLE stream            -> FUNC, SYM, TUPLE
      InitialOpts: Inline, flow graph, DFO block reorder            -> BLOCK, ARC, LOOP
      PreLowerGlobalOpts: GlobalOptimize, AddrModes ...             (IL opcodes)
      dop2_Codegen: Lower (IL opcode -> x86 opcode, same tuples)    (machine opcodes)
      EmitFunction: Peeps, Globregs/Color (registers), StkStackAlloc (frame),
                    FgDoLateFlowOpts (tail merging), LateGlobalOptimize, FgCodeMotion,
                    Schedule, FgLowerSwitches, EncEmitFunc (bytes + listing)
      endproc: free the per-function SYM table and pools
```

Five structure families carry this:

- **MODULE**: one per input. **FUNC**: the function being compiled.
- **SSR**: front-end symbols read from the IL. **SYM**: back-end symbols, numbered by a per-function `key`.
- **TUPLE**: one node type for every instruction, operand, label, block marker and list node.
- **BLOCK/ARC**: the flow graph laid over the tuple stream.

The rest of this page covers each family in turn.

## 2. Pipeline (il_pipeline)

### 2.1 Getting IL

`_CallMain` 0x10c05870 → `MainInit` (switches, `MscProcessSwitches` 0x10b97df0 over `_p2tab` 0x10ceda08) →
`MainContReal` 0x10c05a90 → `dop2` 0x10c05fb0 → `MainEnd`/`MainFinal` **C**.

c1 writes four streams per module, mapped as **ILMAP** {cur, base, aux} (12 bytes; `ilmap_open` 0x10b03950):

- gl: global symbols and types.
- sy: each function's locals.
- ex: each function's expression IL.
- in: initializers.

`MODULE.il[7]` sits at +0x250. `_PIlPtr` 0x10d1886c points at the ILMAP being read **C**.

A function definition is IL record kind 0xe. `readsym` 0x10bbaea0 stores it in a 0xc4-byte SSR with class 4
(`SSR_FUNC` in il.h). The SSR holds the function's offsets into the streams: ex at +0x58 and sy at +0x5c **C**.

### 2.2 Which function is compiled next

`getsym` inserts every defined function into `_Eentry_list` 0x10d1885c, sorted by ex offset, which is source
order. Under -Og (cl passes it for /O1, /O2 and /Ox), `CgBuildCallGraph` → `CgBottomUpOrder` 0x10b05bd0
reorders the list so that callees come before their callers. It does this in repeated sweeps in source order.
The dop2 loop then takes the first entry with `flags50 & 0x20` (must compile) and without `& 2` (taken). It
compiles that function and starts again from the head of the list **C** (tests `ord.c`, `ord2.c`).

**Matching consequence.** A function's code can depend on its same-module callees, which were compiled
first:

- Static callees can get custom conventions. In `ipra.c` a static leaf takes its argument in eax **C**.
- Inliner information and register sets are recorded on each callee's CGNODE (+0x60, `MscRecordInterProcRegInfo`)
  **I** for the effect on callers.

### 2.3 Phases of one function

The full call list with gating is in il_pipeline §5.3. In short:

| step | function | what matters for codegen |
|---|---|---|
| read | `ReadFunction` 0x10c06480 → `startproc` (SymInit, `_LabelCounter = 1`, clears tuple free lists) → `RdrReadProc` 0x10bcb360 | tuples created in IL order; user SYMs created at first operand use |
| 2 | `dop2_InitialOptsAndSetup` 0x10c06890 | Inline; FgBuildFlowGraph; **FgDFOReorderLoopRecog** (the one global block reorder); SymGarbageCollection |
| 3 | `dop2_PreLowerGlobalOpts` 0x10c06cc0 | GlobalOptimize (CSE, copy propagation, loop opts, unrolling); SymGarbageCollection; AddrModes |
| 4 | `dop2_Codegen` 0x10c06fa0 | InlCollectInfo; **Lower** (in place, IL opcode → machine opcode) |
| 5a-c | `EmitFunction` 0x10c06540 | Peeps; FgEarlyTailDup (/O2 only); **Globregs, Color**; RegSimpleAssignBody; Sizeext (/O2 only); **StkStackAlloc**; MdmInsertEntryExit |
| 5d-f | | **FgDoLateFlowOpts** (tail merging); LoFinalLower; LateGlobalOptimize; **FgCodeMotion** |
| 5h-i | | MscClean; **Schedule**; **FgLowerSwitches**; MscRecordInterProcRegInfo; **EncEmitFunc** |
| end | `endproc` 0x10c07b40 | SymTerm; per-function pools freed |

Under `/Od` (`_Globopt_flag` = 0), register allocation is only `RegSimpleAssignBody`. Globregs, Color, Peeps,
Schedule and MscClean do not run **C**.

### 2.4 /O1 versus /O2

cl gives c2 the same command line for /O1 and /O2. The difference arrives per function in the IL as
`FUNC.pragstat` (IL pragma 0x1f → `MscProcessPragstat` 0x10ba26b0): bit 23 is `_Speed`, bit 21 globopt and bit
11 FPO **C**. Both levels run the same pass list. At the driver level `_Speed` adds only FgEarlyTailDup and
Sizeext. About 120 functions read `_Speed` internally, for example the tail-merge limit of 20 bytes versus 2
(§5) **C**.

`/d2<x>` reaches c2 as `-<x>`; il_pipeline §7.2 lists all 140 switches. `/d2FAf` is useful: it prints the
flow graph into the listing (§5).

## 3. Tuples (il_tuple)

### 3.1 One node type

`TupAlloc` 0x10bf0a00 takes the kind in bl. It allocates `TupKindSize[kind]` bytes (table at 0x10cec5b8) from
free lists indexed by size **C**. Every tuple starts with `TUPHDR`:

| off | field |
|---|---|
| +0x00 | next (stream link, operand-list link or node-list link) |
| +0x04 | opcode |
| +0x08 | kind (TK_*, 32 kinds) |
| +0x09 | iflags (bit 0 = instruction) |
| +0x0a | type word TY |

The kinds come in four groups:

- **Leaves** (operands), kinds 1-12: REG, SYM, DATAADDR, CODEADDR, EA, INDIR, ICONST, FCONST, TEXT, REGSET,
  SYMSET.
- **Instructions**, kinds 13-22: INSTR, MBINSTR, CALL, RETURN, QUESTION, BRANCH, INTRINSIC, SWITCH, EHINSTR,
  TUPLIST.
- **Stream markers**, kinds 23-28: PRAGMA, ENTRY, EXIT, BLOCK, LABEL, SPLITPOINT.
- **List nodes**, kinds 29-31: BRLIST, CASE, CCLIST.

**One opcode space** covers all three instruction sets:

- x86 machine opcodes 1..0x272 (`Opcode_name` 0x10cdf220);
- IL opcodes 0x274..0x313;
- MSIL opcodes 0x315..0x477.

`TupOpcodeName` 0x10ceb3d8 holds all 1144 names. Lowering rewrites a tuple's opcode **in place**:
LoLowerTuple 0x10c3ebb0 → LoMapToMachine → LoSetOpcode → LoLegalizeTuple **C**.

### 3.2 The stream

A function is a doubly linked list. `next` is at +0x00 and `prev` at +0x10 (INSTRHDR). Each block in the list
looks like this:

```
BLOCK [LABEL ...] instr instr ... BLOCK [LABEL ...] instr ...   ... EXIT
```

The INSTRHDR fields are:

| off | instruction | marker |
|---|---|---|
| +0x14 | line (offset from FUNC.baseLine) | line |
| +0x18 | region | region |
| +0x1c | seqno; the encoder later reuses its low byte as the encoded length **C** | seqno |
| +0x20 | per-kind data | BLOCK: its BLOCK*; LABEL: the BLOCK* it is in **C** |
| +0x24 | owning BLOCK* (`TupSetInstrBlock` 0x10bf7330) | LABEL: its SSR_LABEL |
| +0x28 | src operand list | |
| +0x2c | dst operand list | |

### 3.3 Operands

Each leaf has one owner. Attaching a leaf that is already attached copies it first: flag 0x10 at +0x14,
`TupUseLeaf` 0x10bf1cd0 **C**.

**Memory operands** (EA, INDIR) store:

| off | field |
|---|---|
| +0x24 | base SYM |
| +0x28 | displacement |
| +0x2c | base register leaf |
| +0x30 | index register leaf |
| low nibble of +0x14 | scale (log2) |
| +0x15 | segment |

The base and index register leaves are **also on the instruction's src list**, so address registers count as
uses **C**.

**REG** keeps the pseudo-register SYM at +0x18 and the physical `&RegSym[n]` at +0x1c (equal to +0x18 before
allocation) **C**.

`+0x10` is the ESP bias. The printed `[esp+N]` grows with each push (tested) **C**.

### 3.4 Labels and branches

A label tuple points to its symbol at +0x24. That symbol is a **front-end SSR of class 3** (`SSR_LABEL`,
0x48 bytes, made by `newlabel` 0x10bbe280), not a back-end SYM:

- SSR+0x33 points back to the tuple.
- SSR+0x3b counts references.
- SSR+0x3f is the label number.

A branch's target leaf (TK_CODEADDR, +0x18) holds the same SSR (`TupMakeBranch` 0x10bf489b) **C**. The label
keeps a BRLIST of its referencing branches and cases at +0x28. The condition code sits in the low nibble of the
branch's +0x0a. il_tuple §6.4 decodes the inverse, reverse, unsigned and encoding tables.

## 4. Symbols and ids (il_sym)

### 4.1 Two layers

| | SSR (front end) | SYM (back end) |
|---|---|---|
| comes from | IL records (`readsym`) | made by c2 per function |
| size | by IL record kind (`ssr_sizeof` 0x10bbd450): data 0x63, label 0x48, function decl 0x50, function def 0xc4, ... | 0x60 |
| id | `SSR.key` +0x28 (c1's numbering) | `SYM.key` +0x1c |
| table | 0x400-bucket hash, `key & 0x3ff` (`_Symtab0` module, `FUNC.symtab` function) | `BESYMTAB` `_BESymTab` 0x10d40e4c, pools of 32 |
| link | SSR+0x10 → SYM | SYM+0x00 → SSR |

The SYM kinds are:

- 1: physical register (the static `RegSym[]` 0x10d09f70; key = register number);
- 3: temporary / pseudo-register ("t<key>" with a single def at SYM+0x14, "tv<key>" otherwise);
- 4: local;
- 5: parameter;
- 7, 8: global;
- 10: function;
- 11: label;
- 15: hash var.

**C** for all of them (il_sym §3).

### 4.2 How keys are assigned (the part that moves code)

- Keys come in **blocks of 32**, one block per pool, handed out when a pool is created (`SymAllocPool`
  0x10beef50). There are five pool classes. At function start:

  | keys | pool class |
  |---|---|
  | 0..31 | frame variables (key 0 is a dummy) |
  | 32..63 | globals |
  | 64..95 | temps |
  | 96..127 | labels, functions, kind 9 |

  Hash vars get their block at their first use. **C** (listing names `tv<key>` skip 96..127.)
- A freed SYM keeps its key and goes onto a LIFO free list for its class. The next allocation of that class
  reuses it first **C**.
- `SymGarbageCollection` 0x10bf0470 runs in phases 2 and 3. It frees every unreferenced temp, so the keys
  handed out afterwards are reshuffled **C** (tested: a temp's key jumps non-monotonically as calls are added).
- User variables get their SYM at their **first operand reference** in the reader
  (`RdrOpName` → `TupMakeUserAddr` → `SymMakeUserVar` 0x10bef7b0). At /O2 the stack order of address-taken
  locals follows first use, not declaration **C** (compile). That this order comes from the key is **I**.
- Temps made by the reader take keys in IL order, before any temp the optimizer makes **C**.
- The table is rebuilt for every function, so identical functions get identical keys **C**.
- `SymCmpSym` 0x10bf00b0 orders registers and temps by key. It orders other symbols by size, then type, then
  offset, then front-end key **C**.

### 4.3 Types

TY is a 16-bit word: the high nibble is the class and the low 12 bits the size in bytes **C**. The classes:

| class | meaning |
|---|---|
| 1 | int |
| 2 | unsigned |
| 3, 4 | pointer |
| 5 | x87 float |
| 6, 7 | block (struct) |
| 8 | void (also the type of set leaves) |
| 0xc-0xf | SIMD |

`p2type` 0x10b33670 maps front-end types to TY **C**.

### 4.4 Frame offsets

`SymSetOffsets` 0x10beff60, called from `EncEmitFunc` 0x10b238d3, adds the signed byte bias `FUNC+0x50`
(locals and homed temps) or `FUNC+0x51` (params) to the frame offsets **C**. The listing prints a frame
operand relative to `FUNC+0x60` (params) or `FUNC+0x64` (locals). Each of these fields is a `RegSym` entry
for ebp or esp (MdPrintOperand 0x10c50c40) **C**. The slot allocator itself, `StkStackAlloc` 0x10be8640, is not
yet mapped.

## 5. Flow graph (il_fg)

### 5.1 Structures

`FUNC+0x08` points to an FGRAPH: a 0x24-byte GRAPH header plus 0x10 more bytes. The nodes are **BLOCK**
(0x1a0 bytes) and the edges are **ARC** (0x38 bytes) **C**.

A block owns the tuple range **[BLOCK+0x1c, BLOCK+0x20)**:

- +0x1c is its own TK_BLOCK tuple.
- +0x20 is the next block's TK_BLOCK tuple (§6, conflict 3).

The tuple side points back: the BLOCK tuple's +0x20 holds the block, and so does each label's +0x20. Each
instruction's +0x24 holds its owning block **C**.

Other BLOCK fields:

| off | field |
|---|---|
| +0x15c | dominators (FBV) |
| +0x160 | post-dominators |
| +0x16c | Lg loop |
| +0x178 | DFO number |
| +0x180 | EH depth |
| +0x190 | profile count |

Loops (LOOP, 0x50 bytes) hang from FUNC+0x0c, natural loops (LPLOOP) from FUNC+0x10 and EH regions (REGION)
from FUNC+0x28/+0x2c. The scheduler and the tail merger use a DAG (DAGNODE 0x44, DAGEDGE 0x18) **C**.

### 5.2 Rules that predict layout

1. **One global reorder.** `FgDFOReorderLoopRecog` 0x10b26ca0 (phase 2) relinks the blocks into reverse
   postorder of a DFS and moves the exit block last. `FgAddArc` 0x10b28b40 **prepends** to the successor list,
   so the DFS visits the lexically latest target first. As a result, **the lexically earliest successor ends
   up right after the block** **C** (code and test t1).
2. Later, only local changes:
   - `FgSimpleBranchPredict` 0x10b2cae0 makes a backward jmp the conditional branch.
   - Loops are rotated: a zero-trip test is duplicated above the loop, and a landing pad is added that holds
     the alignment padding (`LgZtripTest1` 0x10b871a0, `LgMakeLandingPad1`).
   - `FgCodeMotion` 0x10b312c0 copies a small join block into each jump that targets it (≤20 bytes at /O2, ≤2
     at /O1). It moves a block that is reachable only by jumps instead of copying it **C**.
3. **Tail merging.**
   - `FgTryTailMerge` 0x10b2e1b0 merges a jump with the code that falls into the same label. It has no size
     limit. It matches through the DAG, so independent instructions come out in **reverse source order** **C**.
   - `FgDoCommonTailMerge` 0x10b307e0 merges two jumps. The shared tail must be more than 20 bytes at /O2 (more
     than 2 at /O1), with a credit for bytes already shared, unless the whole block matches **C**.
4. `FgCrossJumpRemove` 0x10b2e700 rewrites if/else assignments to one destination as "assign; inverted jcc;
   assign" **C**.
5. **Switch layout**: case bodies come in source order and the default body comes last, even when default is
   written first (tested). Mechanism: layout.md §3.1 (c1xx's bounds check is a branch to `default`).
6. `/d2FAf` prints `; Block(n) In() Out() Instrs Bytes` before every block of the `/FAsc` listing **C**.

## 6. Conflicts settled in this merge

Each of these was re-read in the code. Each is also recorded at the top of `il.h`.

1. **The label symbol is an SSR, not a SYM.** `newlabel` 0x10bbe280 allocates `ssr_sizeof(3)` = 0x48 bytes,
   sets +0x30 = 3 and sets +0x3f from `_LabelCounter` 0x10d0dd00. `TupMakeLabel` 0x10bf4bb0 stores it at
   label+0x24 and writes the tuple back to its +0x33. `TupAddToBranchList` 0x10bf2ca4 increments the dword at
   +0x3b. So il_tuple's "SYM +0x30/+0x33/+0x3b" are SSR_LABEL fields. SYM+0x30 remains il_sym's hash-var link
   **C**.
2. **Leaf +0x18 types.**
   - TK_CODEADDR holds an SSR: `TupMakeBranch` copies label+0x24 into it, and the printer tests `+0x30 == 4`,
     which is SSR class 4.
   - TK_SYM holds a SYM: the OPLOCAL printer reads +0x08 base and +0x24 offset, 0x10c50c6e **C**.
   - TK_DATAADDR is assumed to hold a SYM **I**.
3. **The block range is half open.** `FgMoveBlock` 0x10b282a3 sets `blk->end = next->first`.
   `TupSetInstrBlock` 0x10bf7330 walks up to and including `end`, but `end` is a TK_BLOCK and only
   instructions are stamped, so the extra step has no effect. il_tuple's summary wording, "first/last tuple",
   was imprecise **C**.
4. **SSR size goes by IL record kind.** In the decoded `ssr_sizeof` table a function definition is record 0xe
   (0xc4 bytes) stored with class 4, and a declaration is record 4 (0x50 bytes). The pipeline's SSR+0x50..+0xc0
   fields are therefore in `SSR_FUNC` **C**.
5. **FUNC fields that the pipeline left unnamed:**
   - +0x10 LPLOOP*;
   - +0x2c RGNLIST*;
   - +0x50/+0x51 frame biases (`SymSetOffsets`, which `EncEmitFunc` calls with esi = FUNC, as its
     `test [esi+0xb0]` shows);
   - +0x60/+0x64 frame-register SYMs.

   **C**. FUNC+0x0c is one field: the Lg loop tree root (`LgFindLoops`), which GlobalOptimize's loop step walks
   **I** (same offset).
6. **Names.** il_tuple's `BB` is il_fg's `BLOCK`. il_fg's `BV` is bitvect.h's `FBV` (same layout). The type
   macros were defined twice, identically.

Also found here: the FgNaturalLoopRecog test that il_fg described as "FUNC->+0x20 & 0x1000" is
`FUNC.entry->flags & 0x1000`, which is the function's `__try` bit (`canon_SEH` uses the same bit), at
0x10b26d0c **C**.

## 7. Still unknown

These are gathered from the four families. Each per-family page has the details.

**Tuples**

- Header +0x0c in every kind.
- Kinds 0 and 8: no constructor found (kind 8 may be OPSCONST).
- EA/INDIR +0x18.
- The +0x09 instruction flag bits 0x04, 0x10, 0x20, 0x40 and 0x80, and the leaf +0x14/+0x15 bits set by the
  optimizer and the register allocator (InitialSplitPoints, Spillcosts, FlagSplitAppears).
- CALL +0x30/+0x38/+0x3c and the meaning of the +0x3e flag byte; BRANCH +0x34/+0x38; LABEL +0x30/+0x34.
- CASE +0x0c/+0x10: a 64-bit value or a low/high range?
- The `Opcode_form` ids and the opbyte/form pairing; `Opcode_dope` bits other than 0x800, 0x100 and 0x1.
- Whether the +0x1c seqno and the encoded length are used in separate phases.

**Symbols**

- The order in which the reader first references parameters versus locals, which fixes their relative keys.
- Whether `StkStackAlloc` 0x10be8640 orders slots by key or by tuple order. This is the main open question for
  stack layout.
- A per-k explanation of the tv sweep in il_sym §9; a SymAlloc/SymFree trace would settle it.
- SYM kinds 2, 6 and 12: no creator found.
- SYM +0x34..+0x5f, probably used by liveness and the register allocator.
- SSR attr bits 24-28 (the two readers disagree), sclass 5/7, the segclass values, and how `_Key` is seeded.
- Whether getmem arenas are zero-filled.

**Flow graph**

- ~~Why a switch's default comes last~~ SETTLED: the bounds-check branch, emitted by c1xx (layout.md §3.1).
- ~~A 16-byte common tail was merged at /O2 (t5 t17)~~ SETTLED: the count includes the jump (layout.md §6).
- The `LgFindLoops` header-split condition (0x10b86487, read, not probed: layout.md §5.1); the preconditions of
  `LgZtripTest1` are in layout.md §5.
- Unmapped fields: BLOCK +0x24..+0x158, +0x174, +0x17e, +0x182..+0x18f, +0x198..+0x19f; ARC +0x10/+0x14,
  +0x28..+0x34; FGRAPH +0x24..+0x30.
- Which FgDoLateFlowOpts mode EhGenFunclet and WvmCodegen use.

**Pipeline**

- Which SSR_FUNC.flags50 bits c1 sets. In particular, what makes a non-static function "must compile" (0x20)
  under /Od, and are 0x80/0x2000 really inline/__forceinline?
- MODULE+0x41c's single reader (decompile 10be0000.c:7724).
- Whether `MscRecordInterProcRegInfo` (CGNODE+0x60) changes a caller's register choice outside LTCG. A test
  needs a caller that keeps a value in ecx/edx across a call to a static callee. Answered in docs/c2/ltcg.md section 3: yes, for static and extern callees
  of the same unit (13.10 and 14.00).
- Whether c1's IL differs between /O1 and /O2 beyond the `_Speed` bit (-Oi, -Os/-Ot shaping).
- The source of FUNC.flags 0x2000 (#pragma optimize? naked?) and of the other SSR+0x50-derived bits.
- MODULE+0x454 bit 0x10 (0x10d0d158, default on, no switch sets it).

**Introduced by this merge**

- SSR_FUNC +0x33..+0x4f, +0x64..+0x7f and +0x90..+0x9f. The meaning of SSR.flags 0x1000 on a function: the
  `__try` reading comes from il_pipeline's `canon_SEH` note **C**, but the same bit means TLS on a data SSR.
- TK_PRAGMA OPUSERLABEL operand and TK_ENTRY +0x20[0]: SSR or SYM? Not checked.
