# c2.dll IL map: the tuple

The tuple is the code generator's single node type. The code stream of a function is a doubly linked list of
tuples: entry and exit markers, blocks, labels, pragmas and instructions. Every operand of an instruction is
a tuple too (a *leaf*), and so are the small list nodes that tie branches to labels. All of them come from one
allocator (`TupAlloc`). Declarations: `src/c2/il_tuple.h`; the size and offset asserts in that header pass
under cl 14.00.50727.762 (`work/scratch/glass_tuple_hdr.c`).

Tags: **C** = CONFIRMED (read in code, or shown by a compile), **I** = INFERRED. Addresses are c2.dll VAs (the
image base is 0x10b00000). Section bases used for the PDB data publics: `.rdata` (section 2) = 0x10cd8000,
`.data` (section 3) = 0x10d06000.

Cross-references: SYM layout → il_sym; BB / block ownership → il_flowgraph; the order of lowering, register
allocation and encoding → il_pipeline.

---

## 1. Allocation

| item | address | fact |
|---|---|---|
| `TupKindSize[32]` | 0x10cec5b8 | byte size per kind (table in §2) **C** |
| `TupSizeFreeList[]` | 0x10d430c0 | free lists indexed by **byte size** (not by kind) **C** |
| `@TupAlloc@4` | 0x10bf0a00 | kind in `bl` (LTCG convention) **C** |
| `@TupFree@4` | 0x10bf0a60 | per-kind cleanup, then pushes onto `TupSizeFreeList[size]` **C** |

**TupAlloc(kind)** (0x10bf0a00) **C**:
1. `size = TupKindSize[kind]`.
2. Pop `TupSizeFreeList[size]` if it is not empty. Otherwise, if `size+4 <= 0x40`, pop
   `TupSizeFreeList[size+4]` (a block 4 bytes bigger can be reused). Otherwise, or if that list is empty too,
   call `getmem(0, size)` (0x10b03b60), arena 0.
3. A tuple taken from a free list is cleared with `memset(p, 0, size)`. A fresh one from `getmem` is **not**
   cleared by TupAlloc (the arena presumably hands out zeroed memory, **I**).
4. `p->kind = kind` (+0x08). Return p.

**TupFree(t)** (0x10bf0a60) **C**: calls `PogoTupRemove` when `PogoFlag` (0x10d0ffdc) is set. It then
dispatches on a class byte `[0x10bf0c18 + kind-1]` = `0 0 0 0 1 1 0 0 0 0 0 0 2 2 2 2 2 3 4 5 6 7 0 8 0 0 9 10 0 11 0`
for kinds 1..31:

| class | kinds | cleanup |
|---|---|---|
| 0 | leaves 1-4, 7-12, PRAGMA, EXIT, BLOCK, BRLIST, CCLIST | nothing |
| 1 | EA, INDIR | `TupFree(+0x2c base)`, `TupFree(+0x30 index)` |
| 2 | INSTR..QUESTION (13-17) | `TupFreeList(+0x28 src)`, `TupFreeList(+0x2c dst)` |
| 3 | BRANCH | Seh cleanup for OPFINALLYCALL/OPFINALLYRET (0x2c2/0x2c3); `TupRemoveFromBranchList` if the target is a label (`TupHasBranchLabel` 0x10bf7910); free +0x30 CC list, src, dst |
| 4 | INTRINSIC | if `+0x30 == 0x6eb` it is turned in place into kind 15 / OPCALL (0x2be) before freeing src/dst (**C**; the reason is not known) |
| 5 | SWITCH | `TupRemoveFromBranchList` if +0x30 (default label) is set; free +0x34 case list, src, dst |
| 6 | EHINSTR (21) | `EhFreeOPPUSHSTATE` (0x2db), `EhFreeOPEHTRY` (0x2df); OPASSUME (0x2ef) frees the +0x30 list; then src/dst |
| 7 | TUPLIST | under Pogo mode 2 `PogoTupRemove` on each sub-tuple; free the +0x30 list, src, dst |
| 8 | ENTRY | `TupFreeList(+0x24)` |
| 9 | LABEL | `TupFreeList(+0x28 refs)`; `label_sym->+0x33 = 0`; `TupFreeList(+0x2c aliases)` |
| 10 | SPLITPOINT | `SbvFree(+0x20)` |
| 11 | CASE | `TupRemoveFromBranchList` |

`TupFreeList` (0x10bf0dc0) frees a `next`-linked list. The free list is linked through +0x00.

## 2. Kinds (byte +0x08)

Sizes come from `TupKindSize` (**C**). The constructors come from scanning every call to TupAlloc for the
`mov bl, K` before it and the `mov [eax+4], OP` after it (`work/scratch/glass_tuple_kinds.py`) **C**.

| K | size | name (mine) | opcode(s) | constructor(s) |
|---|---|---|---|---|
| 0 | 12 | ? | — | none found |
| 1 | 32 | REG | OPREG 0x279 | `TupMakeReg` 0x10bf1de0, `TupMakePhysReg` 0x10bf4d40, `TupMakeTmpReg` 0x10bf4db0, `TupMakeBLT` 0x10bf4e10 |
| 2 | 32 | SYM | OPLOCAL 0x27a / OPGLOBAL 0x27b (from `SymExtractOpcode[sym->+4]` 0x10cec728) | `TupMakeSym` 0x10bf1e70, `TupMakeSymSimple` 0x10bf1ea0, `TupMakeUserVar` 0x10bf4e40; `TupMakeTmpVar` 0x10bf4de0 (OPLOCAL) |
| 3 | 32 | DATAADDR | caller-supplied, type = `TyPtrType` (0x10d0d6e0) | `TupMakeDataAddr` 0x10bf1e10 |
| 4 | 32 | CODEADDR | OPEXTERNAL 0x283 / OPINTERNAL 0x284 | `TupMakeCodeAddr` 0x10bf1e40; also built inline by `TupMakeBranch` |
| 5 | 52 | EA | OPBASED/OPSYMOFF/OPSYMSEG/OPPCREL... | `TupMakeEa` 0x10bf1ed0 |
| 6 | 52 | INDIR | OPBASED/OPINDEXED/OPSCALED/OPSEGOFF... | `TupMakeIndir` 0x10bf1f70 |
| 7 | 32 | ICONST | OPICONST 0x275 | `TupMakeIntConst` 0x10bf1d00 |
| 8 | 28 | ? (OPSCONST? **I**) | — | none found |
| 9 | 36 | FCONST | OPFCONST 0x277 | `TupMakeFloatConst` 0x10bf1db0 |
| 10 | 28 | TEXT | OPTEXT 0x278 | `RdrGetTuples` 0x10bce2a0 |
| 11 | 28 | REGSET | OPREGSET 0x288 | `TupMakeRegSet` 0x10bf1d40 |
| 12 | 28 | SYMSET (PAS) | OPSYMSET 0x289, type 0x8000 | `TupMakeSymSet` 0x10bf1d60 |
| 13 | 48 | INSTR | any | `TupMakeInstr` 0x10bf3540, `...NoSrc` 0x10bf35a0, `...UnaryNoDst` 0x10bf3630, `...Unary` 0x10bf3810, `...Binary` 0x10bf39f0, `...Ternary` 0x10bf3ae0, `...MultiSrc` 0x10bf3c00; SSA OPPHI/OPRHO |
| 14 | 48 | MBINSTR | multi-byte (struct) ops | `TupMakeMbUnary` 0x10bf38c0 |
| 15 | 64 | CALL | OPCALL 0x2be or x86 `call` | `TupMakeCall` 0x10bf43b0, `TupChangeToCall` 0x10bf4480 |
| 16 | 52 | RETURN | OPRET, x86 `ret`, MSIL ret/endfilter | `TupMakeReturn` 0x10bf4610 |
| 17 | 52 | QUESTION | OPQUESTION 0x2cb | `TupMakeQuestion` 0x10bf3d00 |
| 18 | 60 | BRANCH | OPBRANCH/OPGOTO/OPLEAVE/..., x86 jcc/jmp | `TupMakeBranch` 0x10bf4850 |
| 19 | 64 | INTRINSIC | OPINTRINSIC 0x2cc | `TupMakeIntrinsic` 0x10bf46e0 |
| 20 | 64 | SWITCH | OPSWITCH 0x2c9, OPSCOPEEXIT | `TupMakeSwitch` 0x10bf4980 |
| 21 | 52 | EHINSTR (misc instr) | OPSIDEEFFECT, OPEHTRY/OPEHENDTRY, OPNEWSTATE, OPPUSHSTATE, OPASSUME, OPPROBE | `TupMakeProbe` 0x10bf4b40 (`mov bl,0x15` at 0x10bf4b5a); otherwise built inline (`CgiInsertSideEffect`, `EhgUpdateEhState`...) |
| 22 | 56 | TUPLIST | OPTUPLIST 0x2cd | `TupMakeTupList` 0x10bf4a30, `TupMakeTupListFromStream` 0x10bf4a60 |
| 23 | 40 | PRAGMA | OPBLKSTART/END, OPPROLOGEND, OPUSERLABEL, OPINLINE*, OPSEGMENT... | `TupMakePragma` 0x10bf4d20 |
| 24 | 44 | ENTRY | OPENTRY 0x2f4 | `RdrEntry`, `MSILReadProc`, `OtlMakeOutlineFunction` |
| 25 | 36 | EXIT | OPEXIT 0x2f5 | `TupMakeExit` 0x10bf4cf0 |
| 26 | 44 | BLOCK | OPBLOCK 0x2f3 | `TupMakeBlock` 0x10bf4b90 |
| 27 | 56 | LABEL | OPLABEL 0x2f2 | `TupMakeLabel` 0x10bf4bb0 |
| 28 | 44 | SPLITPOINT | OPSPLITPOINT 0x2f6 | `ColMakeSplitPoint`, `ColMakeSplitPointOne` (color.obj) |
| 29 | 24 | BRLIST node | — | `TupAddToBranchList` 0x10bf2c80 |
| 30 | 32 | CASE node | — | `TupMakeCaseListNext` 0x10bf2350 |
| 31 | 16 | CCLIST node | — | `TupMakeCondSetterList` 0x10bf2290, `TupCopyCondSetterList` 0x10bf22b0 |

Kind masks that appear as immediates (`1 << kind & mask`) **C**:
- `0x1ffe`: leaf (kinds 1-12), e.g. TupAppendSrc 0x10bf2442.
- `0x60`: memref (EA, INDIR), which has base/index registers.
- `0x06`: REG/SYM, whose +0x18 is a SYM*.
- `0x7e`: kinds 1-6, which can be volatile (TupHasVolatile 0x10bf0ca0).
- `0x4000`: MBINSTR (LoMapToMachine 0x10c43dc0).

The one-instruction predicate is `iflags & 1` (+0x09). Each of kinds 13-22 sets it, and so do `TupMakeProbe`
and `TupMakeTupListFromStream`. Labels, blocks, pragmas, entry and exit do not set it. **C** (TupMakeLabel,
TupMakeBlock, TupMakePragma, TupMakeExit store no +9 bit; `TupFindNextInstr` 0x10bf5360 tests `+9 & 1`.)

## 3. Common header (all kinds)

| off | type | meaning | tag |
|---|---|---|---|
| 0x00 | TUPLE* | next: the stream link for stream tuples, the operand-list link for leaves, the list link for nodes, the free-list link once freed | C |
| 0x04 | uint | opcode (§7) | C |
| 0x08 | u8 | kind | C |
| 0x09 | u8 | iflags: bit0 = instruction; bit3 = non-local branch, set by TupMakeBranch for opcodes 0x2c1..0x2c8 and MSIL `leave` 0x408; bit7 is tested on calls in TupGetCallSignature 0x10bf40d6; the other bits are set or tested by optimizer code (bit2 in FgCompareMerge, bit3 in FgLabelHasFallThrough...) | C (bit0, bit3 set), I (rest) |
| 0x0a | u16 | type word: low 12 bits = size in bytes, high nibble = class (`TyBase` table 0x10ceb298: 1 signed, 2 unsigned, 5 x87 float, 0xc..0xf MMX/SSE). Leaves and instructions take it from the constructor. A symset gets 0x8000 and a probe 0x1004. For TK_BRANCH this word is reused (see §6.4) | C (storage, size/class split from LoMoveOpcode 0x10c3ede0 and LoLowerTuple 0x10c3ebb0), I (class names) |

## 4. Leaves (kinds 1-12)

### 4.1 Leaf header

| off | type | meaning | tag |
|---|---|---|---|
| 0x0c | ? | unused by tuple.obj | — |
| 0x10 | int | **stack bias** of ESP-relative frame operands. MdmSetOperandStackLevels (0x10c56170) writes it as `MdmAdjustOffs()+level` when the frame register is ESP (`&RegSym[5]` = 0x10d0a150), otherwise 0. MdPrintOperand subtracts it from the frame offset (0x10c50c7d, 0x10c50b9a). A stream tuple keeps its `prev` here, but leaves are never in the stream | C |
| 0x14 | u8 | lflags, see below | C/I |
| 0x15 | u8 | lflags2, see below | C/I |

The bias is shown by a compile (`work/scratch/glass_tuple_t1.c`, /O2): one array local is printed as
`_buf$[esp+edx*4]` before `sub esp,16`, as `_buf$[esp+20]` after `sub esp,16; push ecx`, and as
`_buf$[esp+32]` after three pushes. The displacement follows the push depth, as `+0x28 - +0x10` predicts. **C**

**lflags (+0x14)**:
- `0x0f`: scale as log2, for memrefs. MdPrintOperand prints `*%d` with `1<<(b&0xf)` (0x10c50aed), and the
  compile shows `edx*4`. **C**
- `0x10`: **attached**. Set when the leaf is put on an operand list or into an EA. If the bit is already set,
  the leaf is copied first. This is the single-owner rule, §4.4. **C** (TupUseLeaf 0x10bf1cd0, TupAppendSrc)
- `0x20`: address component, meaning the leaf is the base or index register of an EA or INDIR. Set by
  TupMakeEa and TupMakeIndir. TupDetachInstrMemRefRegs removes leaves with this bit from the src list.
  **C**
- `0x40`: volatile, for kinds 1-6 (TupHasVolatile 0x10bf0ca0). On a SYMSET it marks a volatile PAS. That
  kind of PAS is kept at the end of the operand list (TupAppendSrc 0x10bf2467) and removed by
  TupRemoveVolatilePASes 0x10bf0d50. **C**
- `0x80`: set by FlagAsNotReallyLastUse* and CorrectDeadBits (register allocation). Probably "not the last
  use". **I**

**lflags2 (+0x15)**:
- `0x07`: segment override, where 0 means none. MdPrintOperand prints `ds:` for OPSEGOFF only when this is 0
  (0x10c50948). TupCmpMemRefFields compares it. **C**
- `0x20`: this memref's registers are **not** mirrored into the instruction's src list (TupDetachInstrMemRefRegs
  0x10bf2a60 sets it; TupAppendSrc and TupAttach test it). **C**
- `0x08`, `0x10`, `0x40`, `0x80`: used by GoptCanonicalize, InitialSplitPoints, FlagSplitAppears and
  GdfMarkRegionConstantsLoop. Meaning unknown. **I**

### 4.2 Payloads

| kind | +0x18 | +0x1c | +0x20.. | tag |
|---|---|---|---|---|
| REG | SYM* of the pseudo-register / temp (for a temp, `sym->+0x14` = its defining tuple, set in TupMakeTmpCall 0x10bf51b0 and read in TupCmpInstrs) | SYM* of the physical register: `&RegSym[n]` (RegSym = 0x10d09f70, 0x60-byte entries; n = `reg->+0x1c` indexes the name table 0x10cec7b8 `noreg eax ecx edx ebx esp ebp esi edi ax ...`). Before allocation it equals +0x18 (TupMakeTmpReg). TupMakePhysReg stores `&RegSym[n]` in both | — | C |
| SYM / DATAADDR / CODEADDR | SYM* | — | — | C |
| ICONST | int64 value, normalized for the type by TupFixIval 0x10bf7210 | (high dword) | — | C |
| FCONST | 3 dwords (10-byte long double + pad) | | | C |
| REGSET | value (register set) | | | C (storage) |
| SYMSET | PAS index; also set in a global SBV when `PasReferencedByIndirOrSymset` | | | C |
| EA / INDIR | ? | INDIR: PAS index (alias class) | +0x20 u8 alignment log2 (INDIR, via MscRestrictAlign); +0x24 SYM* base symbol; +0x28 int displacement; +0x2c base REG leaf; +0x30 index REG leaf | C |

How the leaf kinds compare (TupCmpLeafs 0x10bf6d20) **C**: kind first, then `TyCompatible(type)`.
- REG: by `+0x1c` (physical register), else `SymCmpSym(+0x18)`.
- CODEADDR: by `sym->+0x28`.
- EA/INDIR: by TupCmpMemRefFields 0x10bf6f70. Its order is `+0x28` disp, seg `+0x15&7`, scale `+0x14&0xf`,
  [align `+0x20` if `FTupCmpIndirAlign`], [PAS `+0x1c` if `FTupCmpIndirPasIndex`], `SymCmpSym(+0x24)`. Then it
  compares base and then index recursively.
- ICONST: 64-bit compare.

### 4.3 Addressing forms (operand opcodes) as the listing prints them

From the MdPrintOperand jump table at 0x10c50cd8 (index `opcode-0x275`), all **C**:

| opcode | printed as | fields used |
|---|---|---|
| OPICONST 0x275 | `%ld` | +0x18 |
| OPSCONST 0x276 | `%ld` of `(+0x18)->+0x24` | +0x18 |
| OPREG 0x279 | register name | `+0x1c -> +0x1c` (classes 0xd-0xf use `+0x1c->+8->+0x1c`) |
| OPLOCAL 0x27a | `sym[frame + off]`, frame = `Function+0x60` if `sym->+4 == 5` else `Function+0x64`; off = `sym+0x24 - (sym+8)->+0x24 - bias` | +0x18, +0x10 |
| OPGLOBAL 0x27b | kind 2: `sym+off`; kind 3: `OFFSET sym+off` | +0x18 |
| OPBASED 0x27c | `sym[base+disp]` | +0x24, +0x2c, +0x28 |
| OPINDEXED 0x27d | `sym[base+index*s+disp]` | +0x24, +0x2c, +0x30, +0x14, +0x28 |
| OPLOCALSCALED 0x27e | `sym[frame+index*s+disp-bias]` | +0x24, +0x30, +0x14, +0x28, +0x10 |
| OPSCALED 0x27f | `sym[index*s+disp]` | +0x24, +0x30, +0x14, +0x28 |
| OPSYMOFF 0x280 | `OFFSET sym+disp`, or `OFFSET $+disp` without a sym | +0x24, +0x28 |
| OPSYMSEG 0x281 | `SEG sym+disp` | +0x24, +0x28 |
| OPSEGOFF 0x282 | `sym+disp`, or `ds:%ld` when no segment is set | +0x24, +0x28, +0x15 |
| OPEXTERNAL 0x283 / OPINTERNAL 0x284 | name; the `OFFSET ` prefix is printed when `sym->+0x30 == 4` and the instruction is not a CALL or BRANCH | +0x18 |
| OPPCREL 0x286 | `$+%d` or `$` | +0x28 |

The listing's `_x$[esp+N]` syntax is this table at work. OPLOCAL is a frame variable, and OPLOCALSCALED is a
frame variable plus an index register.

### 4.4 Attach rule and memref registers

Every constructor and appender runs the same three steps on a leaf it attaches (TupAppendSrc 0x10bf2430,
TupAppendDst 0x10bf2560, TupMakeBinary 0x10bf39f0 and the other TupMake*) **C**:

```
attach(leaf):
  if kind(leaf) in 1..12:
     if leaf->lflags & 0x10:  leaf = TupCopy(leaf)        // a leaf has exactly one owner
     leaf->lflags |= 0x10
  return leaf
```

**Memref register mirroring** (TupAppendSrcInstrMemRefRegs 0x10bf2790, TupAppendMemRefReg 0x10bf26f0) **C**:
when an EA or INDIR leaf is on an instruction's src **or dst** list and its `lflags2 & 0x20` is clear, its
`+0x2c` base and `+0x30` index REG leaves are **appended to the src list**. This is the same tuple object, not a
copy: the `0x10` bit is cleared first, then the leaf is re-attached. The memref keeps pointing to it, and the
leaf's `next` links it into the src list. So a store `mov [ebx+esi*4], eax` has src = {eax, ebx, esi} and
dst = {INDIR}. The address registers count as uses, which is what the register allocator and liveness need.
TupAppendMemRefReg skips a leaf that is already on the list.
`TupDetachInstrMemRefRegs` (0x10bf2a60) does the reverse: it sets `+0x15|=0x20` on each memref and unlinks
from the src list every leaf with `lflags & 0x20`. `TupAttachInstrMemRefRegs` (0x10bf2ad0) re-mirrors.

**List order** (TupAppendSrc/Dst) **C**: a new operand goes before the first SYMSET leaf that has the 0x40 bit.
Volatile PAS sets always stay at the tail, and TupSkipPas (0x10bf7130) skips symset leaves. The function returns
the last leaf of the appended chain.

`TupQueryLeafLocation` (0x10bf2b30) **C** gives a leaf's operand position: dst i → `-(i+1)`, src i → `+(i+1)`,
not found → 0.

## 5. Instruction and stream-marker header (kinds 13-28)

| off | type | meaning | tag |
|---|---|---|---|
| 0x0c | ? | — | — |
| 0x10 | TUPLE* | **prev** (stream back link) | C |
| 0x14 | u16 | line offset. Constructors copy the global `LineOffset` (0x10d0d6fc). Labels, blocks and pragmas get 0. EncSetOffsets adds it to the function's base line (0x10b24480) | C |
| 0x18 | ptr | region (`TupGetRegion` 0x10bf78b0 walks instructions and labels to the nearest non-null +0x18) | C (field), I (type: rgn.obj region) |
| 0x1c | u16 | sequence number from `TupNumberTuplesInRange` 0x10bf72f0, which counts instructions and labels and fails over 0xffff. The encoder reads `+0x1c` as a **byte**: the encoded length of the instruction, summed in EncSetOffsets at 0x10b24480 | C (both uses), I (that they are phase-disjoint) |
| 0x20 | ptr | per kind: BLOCK → BB*; EXIT → value (TupMakeExit); PRAGMA → operand (OPUSERLABEL: label SYM, TupRemoveLabel 0x10bf1070); OPPROBE → a 0x50-byte probe record (TupMakeProbe 0x10bf4b40); ENTRY → pointer whose [0] is the function SYM (TupGetCallSignature 0x10bf3ffc); SPLITPOINT → SBV*; branch instructions under PGO → counts record (taken/not-taken at +0x20/+0x28, LiPrintInstr 0x10b89f70) | C |
| 0x24 | ptr | instruction: **owning BB** (TupSetInstrBlock 0x10bf7330); LABEL: label SYM*; ENTRY: formal list (freed by TupFree) | C |
| 0x28 | TUPLE* | instruction: **src** operand list; LABEL: refs list (BRLIST) | C |
| 0x2c | TUPLE* | instruction: **dst** operand list; LABEL: alias labels | C |

**Block ownership** (TupSetInstrBlock 0x10bf7330) **C**. The argument's `+8` points to {first BB, last BB}. The
function walks from `firstBB->+0x1c` (first tuple) up to `lastBB->+0x20->next`. A BLOCK tuple sets
`cur = blk->+0x20` (BB*), and each instruction gets `+0x24 = cur`. `TupFindBlock` (0x10bf5310) instead walks
`prev` back to the nearest BLOCK tuple and returns its +0x20 (see il_flowgraph for the BB fields `+0x1c`/`+0x20`).

A block in the stream is laid out as `BLOCK [LABEL...] instr... `, and the next BLOCK begins the next block.
`TupFindNextInstrBB` (0x10bf52e0) stops at kind 26, and `TupFindNextBB(t,k)` (0x10bf52c0) looks for kind k
before the next BLOCK. **C**

## 6. Per-kind instruction fields

### 6.1 INSTR / MBINSTR (48)
These kinds have only src (+0x28) and dst (+0x2c). The constructors (TupMakeBinary 0x10bf39f0 is the model)
**C**:

```
t = TupAlloc(13); t->iflags |= 1; t->opcode = op; t->type = ty;
t->dst = attach(dst);  t->src = attach(src1);  t->src->next = attach(src2);
TupAppendSrcInstrMemRefRegs(t);        // mirror memref regs into src
if (cb) cb(arg, t);                    // optional callback (7th arg), e.g. an inserter
t->line = LineOffset;  return t;
```
`TupMakeTernary` and `TupMakeMultiSrc` chain more sources the same way. `TupMakeMbUnary` (kind 14) frees its
size operand (param 6) unless it is attached elsewhere.

### 6.2 CALL (64)
- src[0] is the call target (a CODEADDR leaf, or a REG/INDIR for an indirect call). dst is the result.
- +0x34 is the call descriptor or signature, read by TupGetCallSignature 0x10bf3fb0. The signatures are
  hash-consed in `TupCallSignatureList[128]` (TupEnterCallSignature 0x10bf3ef0, records of
  `getmem(4, 8*n+0x18)`).
- +0x3e (u8) is a flag argument of TupMakeCall and TupChangeToCall.
- TupChangeToCall (0x10bf4480) turns an intrinsic into a call. It moves the src list (after the new target)
  and the dst list over, retargets the temp `sym->+0x14` definitions, and splices the new tuple into the old
  one's stream position. **C**

### 6.3 RETURN (52), QUESTION (52), INTRINSIC (64), EHINSTR (52), TUPLIST (56)
- RETURN: optional dst (eax) and src. **C**
- QUESTION: dst = result; src = {cond-operand, a, b}; +0x30 u8 = condition code (TupMakeQuestion 0x10bf3d00).
  **C** (storage), **I** (the meaning of the first src)
- INTRINSIC: opcode 0x2cc; +0x30 u16 = intrinsic number; up to four sources (TupMakeIntrinsic 0x10bf46e0). **C**
- EHINSTR: +0x30 is an EH record (OPPUSHSTATE: `+0x30->+4` is ehinfo, copied by `new_copy_ehinfo` in TupCopy)
  or a tuple list (OPASSUME). **C**
- TUPLIST: +0x30 is a detached sub-stream. TupMakeTupListFromStream (0x10bf4a60) cuts a range out of the stream
  and replaces it with one OPTUPLIST tuple (type 0x8000). TupInlineTupList (0x10bf4ac0) splices it back and
  frees the wrapper. **C**

### 6.4 BRANCH (60)
TupMakeBranch(target, cc, op, cond, ccsetter) at 0x10bf4850 **C**:
- The type word is reused. The **low nibble of +0x0a is the IL condition code** (`+0xa ^= (+0xa ^ cc) & 0xf`),
  and TupInvertCBranchCondCode (0x10bf7380) toggles bit 0 of +0x0b.
- If the target is a LABEL tuple (kind 27), the branch registers itself on the label's refs list
  (`TupAddToBranchList`). Then it builds `CODEADDR(OPINTERNAL, label->+0x24 sym)` as src[0].
- src[1] = the condition operand (optional). In that case one CCLIST node is pushed onto +0x30, with
  `+0xc = ccsetter`.
- Opcodes 0x2c1..0x2c8 and 0x408 set `iflags |= 8`.
- **Fall-through** (TupHasFallThrough 0x10bf7490) **C**: a tuple falls through unless it is an unconditional
  branch, a SWITCH, or (when `SdsuIsValid`) a RETURN. An unconditional branch is kind 18 with no CC list, not
  OPONERROR, and either below the MSIL range or one of MSIL br/br.s/leave/leave.s.

IL condition codes (**C**, decoded through `TupCCEncode` 0x10cec6e0 → `CondCodeName` 0x10ce2b68):

| cc | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| x86 | e | ne | l | g | le | ge | b | a | be | ae | s | ns | p | np | o | no |

Companion tables, all **C**:
- `TupCCInverse` (0x10cec638) gives the negation: 1↔2, 3↔6, 4↔5, 7↔10, 8↔9, 11↔12, 13↔14, 15↔16.
- `TupCCReverse` (0x10ceb0a4) gives the operand swap: 3↔4, 5↔6, 7↔8, 9↔10.
- `TupCCUnsigned` (0x10cead14) maps 3..6 → 7..10.

When a machine-level branch is inverted (opcode < 0x273), the matching EFLAGS source register is rewritten too:
`&RegSym[MdCCToReg[TupCCEncode[cc]]]`.

### 6.5 SWITCH (64)
- src[0] = selector.
- +0x30 = default LABEL tuple (registered on its refs list).
- +0x34 = list of CASE nodes.
- `TupMakeCaseListNext(label, v10, v0c, sw, next)` (0x10bf2350) allocates CASE (kind 30): +0xc, +0x10 values,
  +0x14 label, +0x18 switch, next. It sets `sw->+0x34 = node` and appends a BRLIST node (`+0xc = case node`) to
  the label's refs. `MdmSetTupleStackLevel` (0x10c55cf0) walks `+0x34` and reads `node->+0x14` (the label).
  **C**. Whether +0xc/+0x10 is a 64-bit value or a low/high range is **I**.

### 6.6 LABEL (56)
- `TupMakeLabel(sym)` (0x10bf4bb0) sets +0x24 = sym and `sym->+0x33 = label`.
- +0x28 = refs. This is a BRLIST whose head node holds the tail pointer at +0x14. Each append does
  `sym->+0x3b++`, so the reference count lives in the symbol (TupAddToBranchList 0x10bf2c80).
- +0x2c = alias labels.
- `sym->+0x31` is a label class: `'Z'` = annotation (TupIsAnnotation 0x10bf4c30); `'S'` SEH, `'V'` C++ EH, `'G'`
  (TupRemoveLabel).
- TupRemoveLabel (0x10bf1070) leaves an OPUSERLABEL pragma behind for user or annotation labels. **C**

## 7. Opcode numbering (+0x04)

One numbering space **C** (`TupOpcodeName` at 0x10ceb3d8 has 1144 entries; `Opcode_name` at 0x10cdf220 is its
first 628 entries):

| range | contents |
|---|---|
| 0x000 | `_first` |
| 0x001..0x272 | x86 machine opcodes: mov 1, arpl, bound, enter, esc, imul 6, int, ret 8, xchg, in, out, pop 0xc, push 0xd, call 0xe (far form), `_jcc` 0xf, jmp 0x10, ... lea 0x12, dec 0x1e, ... add 0x2d, cmp 0x2f, sub 0x32, test 0x33, xor 0x34, ... movzx 0xc2, movsx 0xc4, ... `_jcc` 0x1c6, jmp 0x1c8, call 0x1c9, sete 0x1d7 ... (SSE/AMD-V/VMX at the end) |
| 0x273 | `_last` |
| 0x274..0x313 | IL opcodes (below) |
| 0x314 | `OP_LIMIT_` |
| 0x315..0x477 | `_firstwvm` .. `_lastwvm`: MSIL opcodes for /clr (WVM) code, e.g. 0x368 add, 0x375 call, 0x381 br, 0x408 leave, plus the `ann.*` annotations |

IL opcodes (complete):
0x274 OPILLEGAL, 0x275 OPICONST, 0x276 OPSCONST, 0x277 OPFCONST, 0x278 OPTEXT, 0x279 OPREG, 0x27a OPLOCAL,
0x27b OPGLOBAL, 0x27c OPBASED, 0x27d OPINDEXED, 0x27e OPLOCALSCALED, 0x27f OPSCALED, 0x280 OPSYMOFF,
0x281 OPSYMSEG, 0x282 OPSEGOFF, 0x283 OPEXTERNAL, 0x284 OPINTERNAL, 0x285 OPLABOFF, 0x286 OPPCREL,
0x287 OPFRAMESIZE, 0x288 OPREGSET, 0x289 OPSYMSET, 0x28a OPARG, 0x28b OPOUTARG, 0x28c OPMGDCONVERT,
0x28d OPASSIGN, 0x28e OPVOLATILEASSIGN, 0x28f OPLOAD, 0x290 OPSTORE, 0x291 OPCONVERT, 0x292 OPNOT, 0x293 OPNEG,
0x294 OPPAREN, 0x295 OPENTERLOAD, 0x296 OPEXITSAVE, 0x297 OPHASH, 0x298 OPHARDINTRIN, 0x299 OPOVCHECK,
0x29a OPOVCONVERT, 0x29b OPLEA, 0x29c OPLEA_LAB, 0x29d OPMBARG, 0x29e OPMBASSIGN, 0x29f OPMBOUTARG,
0x2a0 OPMBVOLATILEASSIGN, 0x2a1 OPRET, 0x2a2 OPPHI, 0x2a3 OPRHO, 0x2a4 OPINLINEARGS, 0x2a5 OPADD, 0x2a6 OPSUB,
0x2a7 OPMUL, 0x2a8 OPMULHI, 0x2a9 OPSHL, 0x2aa OPAND, 0x2ab OPOR, 0x2ac OPXOR, 0x2ad OPDIV, 0x2ae OPREM,
0x2af OPDIVREM, 0x2b0 OPSHR, 0x2b1 OPRNGCHECK, 0x2b2 OPIVADD, 0x2b3 OPIVSUB, 0x2b4 OPIVMUL, 0x2b5 OPIVCONVERT,
0x2b6 OPCMP, 0x2b7 OPEQ, 0x2b8 OPNE, 0x2b9 OPLT, 0x2ba OPGT, 0x2bb OPLE, 0x2bc OPGE, 0x2bd OPSPADJUST,
0x2be OPCALL, 0x2bf OPBRANCH, 0x2c0 OPGOTO, 0x2c1 OPCATCHRET, 0x2c2 OPFINALLYCALL, 0x2c3 OPFINALLYRET,
0x2c4 OPSCOPEGOTO, 0x2c5 OPSCOPEEXITGOTO, 0x2c6 OPONERROR, 0x2c7 OPEXITGOTO, 0x2c8 OPLEAVE, 0x2c9 OPSWITCH,
0x2ca OPSCOPEEXIT, 0x2cb OPQUESTION, 0x2cc OPINTRINSIC, 0x2cd OPTUPLIST, 0x2ce OPASM, 0x2cf OPTRY, 0x2d0 OPTRYEND,
0x2d1 OPFINALLY, 0x2d2 OPFINALLYEND, 0x2d3 OPEXCEPT, 0x2d4 OPEXCEPTEND, 0x2d5 OPFILTERSTART, 0x2d6 OPFILTER,
0x2d7 OPFILTEREND, 0x2d8 OPKILLREGS, 0x2d9 OPSIDEEFFECT, 0x2da OPSCOPEINDEX, 0x2db OPPUSHSTATE, 0x2dc OPPOPSTATE,
0x2dd OPDTORACTION, 0x2de OPDTOREND, 0x2df OPEHTRY, 0x2e0 OPCATCH, 0x2e1 OPEHENDTRY, 0x2e2 OPEHRET,
0x2e3 OPNEWSTATE, 0x2e4 OPSETSTATE, 0x2e5 OPARGPLACE, 0x2e6 OPCORTHROW, 0x2e7 OPCORRETHROW, 0x2e8 OPEHFINALLY,
0x2e9 OPEHFINALLYEND, 0x2ea OPCOMOBJECT, 0x2eb OPEHENDCATCH, 0x2ec OPFAULT, 0x2ed OPEHFAULT, 0x2ee OPEHFAULTEND,
0x2ef OPASSUME, 0x2f0 OPASSUMEEXPR, 0x2f1 OPPROBE, 0x2f2 OPLABEL, 0x2f3 OPBLOCK, 0x2f4 OPENTRY, 0x2f5 OPEXIT,
0x2f6 OPSPLITPOINT, 0x2f7 OPPROLOGEND, 0x2f8 OPEHPROLOGSTART, 0x2f9 OPEPILOGSTART, 0x2fa OPBLKSTART,
0x2fb OPBLKEND, 0x2fc OPINLINE, 0x2fd OPINTERLOCK, 0x2fe OPALIGNCODE, 0x2ff OPEMPTYBLOCK, 0x300 OPUSERLABEL,
0x301 OPSEGMENT, 0x302 OPSTATEMENT, 0x303 OPUSERSTATEMENT, 0x304 OPDEADCATCHRET, 0x305 OPINLINESTART,
0x306 OPINLINEEND, 0x307 OPPOGOPROLOGEND, 0x308 OPREGION, 0x309 OPEXCEPTCODEWARNOFFSTART,
0x30a OPEXCEPTCODEWARNOFFEND, 0x30b OPOMP_SECSTART, 0x30c OPOMP_SINGLE, 0x30d OPOMP_BARRIER, 0x30e OPOMP_MASTER,
0x30f OPOMP_ORDERED, 0x310 OPOMP_LASTPRIVATE, 0x311 OPOMP_COPYIN, 0x312 OPMSIL_DANGLINGCODE_START,
0x313 OPMSIL_DANGLINGCODE_END.
The full dump, including all machine and MSIL names, is in `work/scratch/glass_tuple_tupopnames.txt`
(`work/scratch/glass_tuple_tab.py` regenerates it).

Opcode-class predicates in tuple.obj, all **C** switch tables:
- `TupIsOpcodeCommutative` 0x10bf66f0: OPADD, OPMUL, OPAND, OPOR, OPXOR, OPEQ, OPNE.
- `TupIsBinary` 0x10bf66a0: 0x2a5-0x2b1 minus OPMULHI, plus 0x2b6-0x2bc.
- `TupIsOpcodeUnary` 0x10bf6610: OPARG, OPMGDCONVERT, OPASSIGN, OPLOAD..OPEXITSAVE, OPOVCHECK, OPOVCONVERT,
  OPMBARG, OPMBASSIGN, OPRET, OPGOTO, OPSWITCH, OPASSUMEEXPR.

### 7.1 Machine-opcode property tables (indexed by opcode 0..0x273)

| table | VA | element | meaning | tag |
|---|---|---|---|---|
| `Opcode_name` | 0x10cdf220 | char* | mnemonic | C |
| `Opcode_oprnum` | 0x10cdfbf0 | s8 | number of operands printed (mov 2, push 1, cdq 0) | C (MdPrintInstr 0x10c4ffa0) |
| `Opcode_byte2` | 0x10cdfe70 | u8 | ModRM reg field already shifted (sub 0x28 = /5, cmp 0x38 = /7, xor 0x30 = /6, `call` 0x10 = FF /2, `jmp` 0x20 = FF /4) | C (values), I (consumer) |
| `Opcode_opbyte` | 0x10ce00f0 | u8[6] | opcode byte per encoding form (mov `8c a0 b0 c6 88`, add `04 80`, push `06 68 50 ff`) | I (column pairing with form) |
| `Opcode_form` | 0x10ce0fa8 | u8[6] | encoding-form ids, parallel to opbyte | I |
| `Opcode_dope` | 0x10ce1e60 | u32 | property bits | see below |
| `Opcode_numdsts` | 0x10ce2830 | s8 | number of dsts (cmp/test/jcc 0, mov/add 1, pop/cdq/imul 2, call/ret/rep 100 = special) | I |

`Opcode_dope` bits:
- `0x800`: dst is printed first. A mov is printed `dst, src0` and an add is printed `src0, src1`. So for two-
  address ALU ops, src[0] is the tied destination copy. The operand-list model for `add eax, ecx` is therefore
  dst = {eax}, src = {eax, ecx}. **C** for the print rule (MdPrintInstr 0x10c4ffa0), **I** for the model.
- `0x100`: prefix (rep, lock). LiPrintInstr (0x10b89f70) prints and decodes the next tuple with it. **C**
- `0x1`: x87 (LoLowerTuple sends these to LowerFloat, 0x10c3ebb0). **C**
- **I**:
  - `0x1000`: commutative (add/xor yes, sub/cmp no).
  - `0x400`: read-modify-write or pushes on the stack (add/sub/cmp/test/neg/inc/shl/push).
  - `0x40`: arithmetic result.
  - `0x02`: writes EFLAGS (also on call/ret).
  - `0x04`: reads EFLAGS (jcc, setcc).
  - `0x20`: control or no-operand (jmp, jcc, cdq, nop).
  - `0x10`: dst size differs from src (movzx/movsx/setcc).
  - `0x40000000`, `0x80000000`: x87 push/pop.

`LoOpLook` (0x10c3ed10) has a small table at 0x10ce3690: OPADD→adc, OPSUB→sbb, OPXOR→xor, OPAND→and, OPOR→or,
OPNOT→not, OPNEG→neg. It gives the high-word opcode used when 64-bit ops are split. **C**

## 8. Creating, inserting and moving tuples

Stream link primitives (tuple.obj), all **C**. None of them check for NULL neighbours, so the stream is assumed
to be bracketed by ENTRY ... EXIT.

| function | VA | effect |
|---|---|---|
| `TupInsertAfter(a,b)` | 0x10bf0e10 | `b->next=a->next; a->next=b; b->prev=a; b->next->prev=b` |
| `TupInsertBefore(a,b)` | 0x10bf0e20 | mirror image |
| `TupInsertListAfter` | 0x10bf0e40 | splice a chain |
| `TupUnlink(t)` | 0x10bf0e70 | fixes the neighbours (NULL-checked), clears t->next/prev |
| `TupMoveAfter(t,pos)` / `TupMoveBefore` | 0x10bf0ea0 / 0x10bf0ee0 | unlink + insert |
| `TupMoveRangeBefore` | 0x10bf0f20 | range splice |
| `TupRemove(t)` | 0x10bf1050 | unlink (no NULL checks) + TupFree |
| `TupRemoveRange` | 0x10bf1190 | |
| `TupReplace(old,new)` | 0x10bf1240 | |
| `TupReverseList` | 0x10bf2410 | reverses a `next` list |

Other helpers:
- Operand-list editing (all on +0x28/+0x2c, using the attach rule): `TupAppendSrc` 0x10bf2430, `TupPrependSrc`
  0x10bf24f0, `TupAppendDst` 0x10bf2560 (also calls `GCSetPtrRefTuple`), `TupPrependDst` 0x10bf2620,
  `TupInsertBeforeSrc` 0x10bf23c0, `TupInsertAfterLeaf` 0x10bf34e0, `TupRemoveSrc/Dst` 0x10bf2bd0/0x10bf2c30,
  `TupReplaceSrc/Dst` 0x10bf30c0/0x10bf2e40, `TupUnlinkSrc/Dst` 0x10bf0fa0/0x10bf1000.
- `TupCopy(t)` (0x10bf1270) **C**. It memcpys `TupKindSize[kind]` bytes and sets next=0, then branches on a class
  byte at `0x10bf1513+kind`:
  - Class 0, kinds 0-2 (REG, SYM): clear `+0x15 & 0x10` and `+0x14 & 0x30`.
  - Class 1, other simple leaves: clear `+0x14 & 0x10`.
  - Class 2, EA/INDIR: deep-copy base and index (marked 0x30), keep the alignment.
  - Class 3, kinds 13-22: deep-copy src (skipping mirrored address registers, then re-mirroring) and dst.
    BRANCH re-registers on its label and copies its CC list. SWITCH re-registers and rebuilds its case nodes.
    TUPLIST copies its sub-stream (`TupCopyUniqueTupleList`).
  - Class 4, markers (kinds 23-28): `prev = 0`.
  - Class 5, TEXT (kind 10): nothing extra.

  The table only covers kinds 0-28. List nodes (29-31) are not copied with TupCopy.

  Under Pogo it also calls `PogoTupCopy`.
- `TupFindNextInstr/PrevInstr` 0x10bf5360/0x10bf5380; `TupFindNext/PrevLabelOrInstr` (instr or kind 27);
  `...BB` variants stop at kind 26.
- `TupFindPrevLineOffset` 0x10bf5460 recovers the line of an instruction that has line 0 from the previous
  instruction or label.

**Producers.**
- Reader (reader.obj `RdrGetTuples` 0x10bce2a0, `RdrEntry`) and MSIL import (msil.obj `MSILBytesToTuples`,
  `MSILReadProc`) build IL-level tuples.
- The optimizer rebuilds and canonicalizes them in place.
- **Lowering** (lower.obj) maps each IL instruction to x86 in place. `Lower` (0x10c3e5e0) walks the stream
  through `LoLowerTuple` (0x10c3ebb0), which returns the next tuple to visit. LoLowerTuple skips
  non-instructions. A float tuple (type class 5, an x87 opcode, or a float src[0]) goes through
  `LoFixIndirections` + `LowerFloat`. Everything else goes through `LoMapToMachine` (0x10c43dc0), which calls
  `LoMapI64` when the operation is wider than 4 bytes and then `LoSetOpcode` (0x10c46c70) to set the machine
  opcode, then `LoFixIndirections` and `LoLegalizeTuple` (0x10c3f3d0) to make the operands legal. A tuple left
  as OPILLEGAL (0x274) is unlinked and freed. So **the same tuple object goes from IL opcode to machine opcode;
  its operand lists are edited, not rebuilt.** **C**
- lowerw.obj (`WvmLower`, `WvmLo*`) is the /clr path to MSIL opcodes 0x315+. It is not used for native x86.
- **Emit.** encode.obj `Encode` (0x10b24080), `EncSetOffsets` (0x10b24480, sums the per-instruction length byte
  at +0x1c and tracks line numbers from +0x14), `EncLengthenBranches` (0x10b24ee0) and `EncCondCodes`
  (0x10b24d70) walk the final stream. The listing (/FA) comes from `LiPrintInstr` (0x10b89f70) →
  `MdPrintInstr` (0x10c4ffa0) → `MdPrintOperand` (0x10c50890). emit.obj writes the COFF sections and does not
  read tuples directly. **C** (call structure), **I** (the full encoder algorithm is out of scope here)

## 9. Open points
- What +0x0c holds in the header, what kind 0 and kind 8 are, and the meaning of +0x18 in EA/INDIR.
- The remaining iflags bits (+0x09: 0x02, 0x04, 0x10, 0x20, 0x40, 0x80), and the remaining lflags/lflags2 bits
  used by the register allocator (InitialSplitPoints, Spillcosts, FlagSplitAppears).
- CALL +0x30/+0x38/+0x3c, BRANCH +0x34/+0x38, LABEL +0x30/+0x34.
- The Opcode_form id → encoding mapping (encoder internals: `decode` and the encoder tables).
- Whether +0x1c in a stream tuple holds the seqno (u16) and the encoded length (u8) in different phases only.
