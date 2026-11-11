# c2 IL map: symbols, pseudo-registers and ids

Back end `c2.dll` of cl 14.00.50727.762. Modules covered: `sym.obj`, `p2symtab.obj`, `type.obj`, `hash.obj`,
`addr.obj`. C declarations are in `src/c2/il_sym.h`. They compile under cl and all of their size asserts hold.

Each claim is tagged **[C]** (CONFIRMED: read in the code at the given address, or tested by a compile) or
**[I]** (INFERRED). Addresses are VAs in `Bin/c2.dll`. Functions are `__fastcall` unless noted, and many
internal ones take arguments in eax/esi/edi because of LTCG.

## 0. The rules that matter for matching

1. **Every back-end symbol has a numeric id, `SYM.key` (+0x1c).** Sparse bit vectors (live sets, the
   `frameKeys`/`globalKeys` sets, the hash-temp set `_HshTmps`) are indexed by it. `SymCmpSym` sorts
   temporaries and registers by it. [C]
2. **Keys come in blocks of 32, handed out when a pool is created, not when a symbol is allocated.** There are
   five pool classes (frame vars, globals, temps, labels/functions, hash vars), and each one fills its current
   pool before it takes a new block. [C] `SymAllocPool` 0x10beef50, `SymAlloc` 0x10bef1d0.
   - At function start: frame vars get keys 0..31 (key 0 is a dummy), globals 32..63, temps 64..95, and
     labels/functions/kind-9 symbols 96..127. Hash vars get no block until the first `SymMakeHashVar`. Every
     later block is the next 32 keys after the last one handed out, whichever class asks first. [C]
     Compile test: temporary stack slots are named `tv<key>` in /FAsc listings. Across the 41 compiles of
     `t6.c` the numbers run tv65 .. tv95 and then jump to tv129, skipping 96..127. [C]
3. **A freed SYM keeps its key and goes onto a LIFO free list.** The next allocation of that class pops it
   before taking a fresh slot. So a new temporary can get a lower key than older ones. `SymGarbageCollection`
   0x10bf0470 runs early in optimization (`dop2_InitialOptsAndSetup` 0x10c06890 and
   `dop2_PreLowerGlobalOpts` 0x10c06cc0). It frees every unreferenced temporary, which reshuffles the keys
   handed out next. [C] In compile tests a temporary's key does not move steadily as code is added; it
   drops from tv206 to tv143 when a fourth call is added (section 9). [C]
4. **The symbol table is rebuilt for each function**: `startproc` 0x10c07a60 calls `SymInit`. Identical
   functions in one file get identical keys (`fa`/`fb` in `t5.c`, both tv203). [C]
5. **User variables get their back-end SYM (and key) the first time the reader meets an operand that uses
   them, not where they are declared.** The path is `RdrOpName` 0x10bd0030 → `TupMakeUserAddr` 0x10bf4e80 →
   `SymMakeUserVar` 0x10bef7b0. Locals and parameters share the frame pool. [C] Compile test: at /O2 the
   frame order of three address-taken arrays follows the order of first use and ignores the order of
   declaration (`t7.c`). [C] Whether that order comes from the key or from tuple order is [I].
6. Temporaries made by the reader take keys in source (IL) order, before any made by the optimizer.
   Adding one call result anywhere in the function (before or after the loop) moved a temporary created by
   the loop optimizer up by exactly one key (`t5.c`: fc, fe). Adding a user local that needs no temporary
   moved nothing (ff). [C]
7. The front end's own symbol ids (`SSR.key` +0x28) are a separate number space, read from the IL.
   `SymCmpSym` uses them only to break the last tie between same-size, same-offset memory symbols. [C]

Practical consequence: any source change that adds or removes a temporary, a field view of a struct (see
`SymNewEquiv`), or a referenced variable shifts the keys of everything allocated after it in the same pool
class. The shift can be by a whole block of 32 if a pool boundary moves. Code far away can change wherever
the back end breaks ties by key.

## 1. Two layers of symbol

| Layer | Struct | Created by | Lifetime | Id |
|---|---|---|---|---|
| Front end (IL) | `SSR`, packed, size depends on class | `readsym` 0x10bbaea0 / `newsym` 0x10bbde50, `newnametabwithname` 0x10bbe360 (made by c2) | module (globals, `_Symtab0`) or function (`_Symtab`) | `SSR.key` +0x28 |
| Back end | `SYM`, 0x60 bytes | `SymAlloc` 0x10bef1d0 and its wrappers | one function (`SymInit`) | `SYM.key` +0x1c |

They are linked both ways: `SSR.sym` (+0x10) points to the current function's `SYM`, and `SYM.ssr` (+0x00)
points back. A SYM with `ssr == NULL` is a temporary. [C] `SymMakeUserVar` 0x10bef7b0 (sets both),
`SymReset` 0x10bef190 (sets `ssr->sym = sym->base` for every live SYM), `SymTerm` 0x10bef150 (clears
`ssr->sym`).

Tuple cross-references (tuple family): a leaf tuple's +0x18 is its `SYM*`, and +0x1c is a second `SYM*`
(the assigned physical register, a `_RegSym` entry, or the same SYM). `TupMakeUserVar` 0x10bf4e40 stores
`tuple+0x18 = sym`, `tuple+4 = _SymExtractOpcode[sym->kind]` and `tuple+0x0a = sym->ty`. The register paths
store `&_RegSym[r]` in both +0x18 and +0x1c, and `SymWalkTuples` 0x10bf0560 walks both. [C]

## 2. SYM (0x60 bytes)

Size [C]: pool stride 0x60 (`SymAllocPool`), `memset(sym, 0, 0x60)` in `SymAlloc`, `(key & 31) * 0x60` in
`SymKeyToSym` 0x10bef650, and `_RegSym` stride 0x60.

| Off | Type | Meaning | Evidence |
|---|---|---|---|
| 0x00 | SSR* | front-end symbol; NULL for temporaries | SymMakeUserVar 0x10bef83f [C] |
| 0x04 | u8 | kind (section 3); 0x10 = free | SymFree 0x10bef3ea [C] |
| 0x05 | u8 | 0x02 address taken (set on the whole equivalence chain); 0x20 has a frame home | SymSetAddrTaken 0x10bf0090, SymSetOffsets 0x10beff60, SymCmpSym 0x10bf0189 [C] |
| 0x06 | u8 | 0x02 visited (GC mark); 0x08 pointer-typed user var; 0x40 FE basetype 0xc (SymCheckEquiv rejects these) | SymMarkVisited 0x10bf04f0, SymMakeUserVar 0x10bef901/0x10bef95f, SymCheckEquiv 0x10bef9b0 [C] |
| 0x07 | u8 | 0x10 explicit alignment (from SSR+0x3b bit 4); 0x20 pointer-typed; 0x40 SymName prints "tb"; 0x80 read-only global (left out of `modGlobalKeys`) | SymMakeUserVar 0x10bef8d3/0x10bef940, SymName [C] |
| 0x08 | SYM* | base of the equivalence class (self for a base) | SymMakeTmpReg 0x10bef6e7, SymNewBaseEquiv 0x10bef9d0 [C] |
| 0x0c | SYM* | next member of the base's chain | SymFree loop 0x10bef3e0, SymSetAddrTaken [C] |
| 0x10 | u16 TY | type word (section 7) | SymMakeTmpReg 0x10bef6dc [C] |
| 0x14 | tuple* | the single defining tuple ("SyDef"); 0 for a temp with several defs | SymSetSyDef 0x10bf0060; SymName "t" vs "tv"; AddrModes 0x10c09cf0 requires it [C] |
| 0x18 | HSH* | hash vars: their value-number entry | HshInsertEntry 0x10b5f880 [C] |
| 0x1c | u32 | **key** (id). For `_RegSym`: the register number (index into `_RegName`) | SymAllocPool 0x10beef84, `_RegSym` data [C] |
| 0x20 | int | size in bytes (in bits for `_RegSym`) | SymMakeTmpReg 0x10bef6e0, SymMakeUserVar 0x10bef8c4 [C] |
| 0x24 | int | offset inside the base (in bits for `_RegSym`; ah = 8); frame offset added by SymSetOffsets | SymNewEquiv, SymSetSyDef, SymSetOffsets [C] |
| 0x28 | union | free-list link while free; frame offset of a homed temp; hardware register code in `_RegSym` (eax..edi = 0..7, al..bh = 0..7); leaf tuple for kinds 13/14 | SymAlloc 0x10bef1f3, SymSetOffsets, `_RegSym` data, SymCmpSym [C] |
| 0x2c | u8 | alignment code = log2(align)+1 | SymMakeTmpReg 0x10bef715, SymChangeAlignment 0x10bef670 [C] |
| 0x30 | SYM* | hash vars of opcode 0x27c: link in `_HshIndirList` | HshInsertEntry [C] |
| 0x34..0x5f | | not mapped; `SymNewEquiv` copies only 0x00..0x2f from the base | SymNewEquiv 0x10befce0 [C] |

`SymName` 0x10bf0750 (called only from `CDbgInfoGenerator::GetDbgSymbolForSymid` 0x10cd2be0, and it builds
the names seen in listings) [C]:

- kind 1: `_RegName[key]` (0x10cec7b8: noreg, eax, ecx, edx, ebx, esp, ebp, esi, edi, ax, ..., xmm7h).
- kind 2: `gr<key>:<reg>`.
- kind 3 with def: `t<key>`; kind 3 without def: `tv<key>`; flag 7&0x40: `tb<key>`.
- kind 6: `ta<key>`; kind 9: `sc<offset>`; kind 14: "ComplexSym".
- otherwise the SSR's name. Equivalence members print as `name+off:index`.

The number printed is the **base** SYM's key. [C]

## 3. Kinds

| Kind | Extract / address opcode (`_SymExtractOpcode` 0x10cec728, `_SymAddressOpcode` 0x10cec770) | Pool | Made by |
|---|---|---|---|
| 0 | OPILLEGAL / OPILLEGAL | temp | - |
| 1 | OPREG / - | none (static `_RegSym[]` 0x10d09f70) | data |
| 2 | OPLOCAL / OPLOCAL | temp | none found [I] |
| 3 | OPLOCAL / OPLOCAL | temp | SymMakeTmpReg 0x10bef6c0 (same address as SymMakeTmpVar), SymMakeHashVar 0x10bef720 |
| 4 | OPLOCAL / OPLOCAL | frame | SymMakeUserVar: SSR class 1 sclass 1 (auto) or class 2; SymPromoteTmpvar 0x10bf0260 (turns a tmp into kind 4) |
| 5 | OPLOCAL / OPLOCAL | frame | SymMakeUserVar: sclass 3 (param); sclass 5 when SSR.flags & 0x20 is clear |
| 6 | OPLOCAL / OPLOCAL | temp | unknown |
| 7 | OPGLOBAL / OPGLOBAL | global | sclass 2/6 (segclass != 4), sclass 4/7 (segclass 1-2), sclass 5 (flags & 0x20) |
| 8 | OPGLOBAL / OPGLOBAL | global | sclass 2/4/6/7 with segclass 4 |
| 9 | OPREG / OPGLOBAL | other | sclass 4/7 with segclass 0, 3 or >4 |
| 10 | OPGLOBAL / OPLABOFF | other | SSR class 4 (function) |
| 11 | OPILLEGAL / OPLABOFF | other | SSR class 3 (label) |
| 12 | - | none (SymAlloc jump index 5) | - |
| 13 | OPREG / - | frame | WvmChangeToSystemMath (MSIL) |
| 14 | - | none | "ComplexSym" |
| 15 | (SymAlloc argument only) | hash | SymMakeHashVar, which then stores kind 3 |

Opcode names come from `_TupOpcodeName` 0x10ceb3d8. The SymAlloc pool dispatch is the index table at
0x10bef395 and the jump table at 0x10bef380. The SymMakeUserVar mapping uses tables at 0x10bef970 (SSR
class) and 0x10bef984 (sclass = SSR.attr bits 14-16), plus the segclass (SSR.attr bits 5-8) tests at
0x10bef7eb and 0x10bef801. All of this is [C] as code. The plain-language names (auto, param, static) are
[I], except that sclass 3 = parameter is [C]: `getsym` 0x10bba720 calls `SsrAlignParam` for it.

## 4. The per-function SYM table and key assignment

`_BESymTab` (0x10d40e4c) points to a `BESYMTAB` (0x4c bytes, getmem arena 0). Layout in `il_sym.h` [C].

**SymInit** 0x10bef020 [C]:

```
t = getmem(0,0x4c); t->keyMap = getmem(0,4000) (1000 pool slots); nextKey = 0; poolHead = poolTail = 0
t->curFrame = SymAllocPool()   // keys 0..31
t->curGlobal= SymAllocPool()   // keys 32..63
t->curTmp   = SymAllocPool()   // keys 64..95
t->curOther = SymAllocPool()   // keys 96..127
curHash = 0; free lists = 0
frameKeys/globalKeys/modGlobalKeys = new empty SBV headers (from _rgbvFreeList 0x10d0d590)
dummy = SymAlloc(4); dummy->kind = 0x10; SbvFreeList(frameKeys)   // key 0 is used up and never handed out
```

It is called from `startproc` 0x10c07a60 unless `_Entry+0xc0` names an existing table (inlining and
outlining reuse it; `SymCopyToSymtab` 0x10bef470 and the Otl* split functions call SymInit for a new
table). [C]

**SymAllocPool** 0x10beef50 [C]:

```
p = getmem(0, 0xc0c); p->next = 0; p->bump = &p->slot[0]; p->end = &p->slot[32]
for each slot s: s->key = t->nextKey++          // the block of 32 keys is fixed here
append p to keyMap (doubling the array when full) and to the poolHead/poolTail list
```

**SymAlloc**(kind in al) 0x10bef1d0 [C]:

```
class = {0,1,2,3,6: tmp; 4,5,13: frame; 7,8: global; 9,10,11: other; 15: hash}
if class has a free list and it is non-empty:            // hash has no free list
    s = pop(freelist); k = s->key; memset(s,0,0x60); s->key = k
else:
    if cur[class] is NULL (hash only) or full: cur[class] = SymAllocPool()
    s = cur[class]->bump++                               // fresh slot; arena memory, zero [I]
if class == frame:  SbvSetBit(frameKeys, s->key)
if class == global: SbvSetBit(globalKeys, s->key)
return s                                                 // the caller stores kind, type, base...
```

**SymFree**(sym) 0x10bef3b0 [C]: it clears `sym->ssr->sym`. Then for every member of the chain
(`equivNext`, starting at sym): kind = 0x10, and push onto the free list chosen from the *current* kind by
the table at 0x10bef44c. Kind 3 → freeTmp; 4/5 → freeFrame (and clears the frameKeys bit); 7/8 →
freeGlobal (clears the globalKeys and modGlobalKeys bits); 9/10/11 → freeOther. A hash var already has
kind 3, so it is recycled as an ordinary temporary.

**SymKeyToSym**(key) 0x10bef650 [C]: `keyMap[key >> 5]->slot[key & 31]`. This works only because every pool
holds exactly 32 consecutive keys.

**SymGarbageCollection** 0x10bf0470 [C]:

1. `SymWalkFunction(SymMarkVisited)` sets `fl6 |= 2` on every SYM that a tuple references, through
   tuple+0x18/+0x1c. It walks the function's tuple list and recurses into the sub-lists of opcodes
   0x2db/0x2ef and the PGO side lists. `SymWalkTuples` 0x10bf0560.
2. It walks pools in creation order and slots in ascending order. For each kind-3 **base** SYM whose chain has
   no visited member, it calls `SymFree`; otherwise it clears the marks. It returns the count.

Because SymFree pushes to the front, the free list afterwards holds the freed temps in **descending** order of
(pool creation order, slot): the next `SymMakeTmpReg` gets the last unreferenced temp in the newest pool.
That reading of the code is [C]; how it plays out in the sweep below is [I].

Callers: `dop2_InitialOptsAndSetup` 0x10c06890 and `dop2_PreLowerGlobalOpts` 0x10c06cc0. Other passes
free SYMs directly with SymFree (`GoptSymFree`, `HshBuildTable`, `OptRebuildTuples`, `remove_tmpreg`,
`RemoveBESyms`, ...; 30+ callers). [C]

## 5. How each kind of SYM is created

### 5.1 Temporaries (pseudo-registers)

`SymMakeTmpReg`(ty in edi) 0x10bef6c0 [C]. `SymMakeTmpVar` is folded to the same address.

```
s = SymAlloc(3); s->ty = ty; s->size = ty & 0xfff; s->kind = 3; s->base = s
a = ty&0xfff ? ceil(log2(size)) : 3        // size 0 counts as 8 bytes
s->alignCode = MscRestrictAlign(ty, a+1)   // 0x10ba31f0: block types (class 6/7) larger than 8 are capped
```

It is inlined as `mov al,3; call SymAlloc` at about 150 more sites (reader, lowering, loop optimizer, ...).
A full list is in the xref dump: `work/scratch/glass_sym_x1.txt`, `glass_sym_xref.py`. `TupMakeTmpReg`
0x10bf4db0 and `TupMakeTmpVar` 0x10bf4de0 wrap it with a leaf tuple. [C]

`SymMakeHashVar`(ty) 0x10bef720 is the same, but allocates from the hash pool (kind 15) and then stores
kind 3. Callers: `HshInsertEntry` 0x10b5f880, `LoptInit` 0x10b3a040, `LoptReplaceInductionVariable`. [C]
So CSE and loop-optimizer value temps are ordinary "t/tv" temporaries whose keys come from a block that is
taken only at the first hash var of the function, after every block handed out before it. [C]

`SymSetSyDef`(sym esi, tuple edi) 0x10bf0060 stores the def on every chain member that overlaps sym's
byte range. [C]

### 5.2 User variables

`SymMakeUserVar`(ssr in eax) 0x10bef7b0 [C]:

```
if ssr->sym: return it                                  // one SYM per SSR per function
kind = map(ssr->cls, sclass = (ssr->attr>>14)&7, segclass = (ssr->attr>>5)&15)   // section 3
s = SymAlloc(kind); ssr->sym = s; s->ssr = ssr
cls 3 (label): ty 0x1004, size 4, alignCode = 1
else: size = (cls==4 ? ssr+0x3c : ssr->size)
      ty = p2type(ssr->attr & 0x1f, size)                // 0x10b33670
      alignCode = cls in {1,2,8} ? ((ssr->attr>>25)&15)+1 : cls==4 ? 3 : 1
s->kind = kind; s->base = s; s->size = size
cls 1: fl7.0x10 = ssr->attr2 bit 4; pointer basetypes 3/4/0xc set fl6|=8, fl7|=0x20
basetype 0xc: fl6 |= 0x40 (and sets global 0x10d0f3b4)
kinds 7/8: if FeSymIsReadOnly(ssr) (0x10bef780) or WpaSymbolIsGlobConst: fl7 |= 0x80
           else SbvSetBit(modGlobalKeys, key)
```

It is called lazily: the reader calls it when an operand names the SSR (`RdrOpName` 0x10bd0030 →
`TupMakeUserAddr` 0x10bf4e80, which picks a different leaf for SSR classes that have no SYM;
`TupMakeUserVar` 0x10bf4e40; `RdrFunction`, `RdrEntry`, `RdrReturn`, ...). About 90 other sites call it:
prolog/epilog, EH, GS cookie, OpenMP, inliner. [C] The SSRs themselves were all created earlier, when
`RdrReadProc` 0x10bcb360 read the function's symbol section (`getsym` 0x10bba720) before its tuples
(`RdrGetTuples` 0x10bce2a0). [C] So key order among user variables is the order of their **first reference
in the IL tuple stream** (plus prolog-generated references), within the frame pool (locals and params
together) or the global pool. [C] for the mechanism. The exact place of params in that order (does the entry
code touch them before the body?) is [I].

### 5.3 Equivalences (field and overlay views)

A base SYM stands for a whole storage object. A reference to part of it with a different type, size or
offset becomes a member SYM. `SymMakeEquiv`(sym, ty, size, off) 0x10befa00 calls `SymFindEquiv` 0x10befdd0;
on a miss it calls `SymNewEquiv` 0x10befce0 [C]:

```
n = SymAlloc(base->kind)       // same pool class as the base => a new key in that class
copy base[0x00..0x2f] into n; restore n->key; n->def = sym->def; n->ty = ty; n->size = size; n->offset = off
n->ssr = 0; clear fl7.0x20, fl6.0x08; alignCode from the low set bit of (off - base->offset) | align
kinds 7/8 not read-only: SbvSetBit(modGlobalKeys, n->key)
```

SymMakeEquiv may also re-root the class (`SymNewBaseEquiv` 0x10bef9d0 rewrites `base` along the chain) when
the new view covers more than the old base. SymMakeEquiv has 150+ callers (BuildAddr, lowering, I64 splitting,
`LoI64HiWord`, ...). [C] Matching consequence: splitting an `__int64` or touching a struct field through
another type uses up a key in the frame (or global) pool. [C] for the mechanism.

For `_RegSym` entries (kind 1), `SymFindEquiv` walks the register's sub-register chain and matches
(size*8, off*8), for example eax → ax → al. [C]

### 5.4 Physical registers

`_RegSym` 0x10d09f70 is a statically initialised array of SYMs, `_RegSym[r]` for r = `_RegName` index:
1 eax ... 8 edi, 9 ax ..., 17 al ..., 21 ah ..., 31 GF0 ..., 47 ST(0) ..., 75 mm0 ..., 83 xmm0. [C] Data
dump: kind 1, `base` = the 32-bit parent (ax.base = &eax), `equivNext` = the next smaller sub-register,
ty 0x1004/0x1002/0x1001, x87 0x5008 or 0x500a, size and offset in bits, +0x28 the hardware code.
Their keys equal the register number, so the key space overlaps pool keys. They are told apart by
`kind == 1`, and SymCmpSym compares kinds 1 and 3 by key together. [C]

## 6. SymCmpSym: an explicit symbol order

`SymCmpSym`(a ecx, b edx) 0x10bf00b0 returns -1/0/1. Callers: `TupCmpLeafs`, `TupCmpMemRefFields`,
`TupAreSameFinalLeaf`, `ItfCSEGetDataAddrVal`, `MscClean`, `PpSsaIndir1Covers2`,
`LurTupIsPrimaryOrRenamableSecondary`. [C]

```
a==b: 0;  a==NULL: -1;  b==NULL: 1
kind 13 vs 13: TupCmpLeafs(a->leaf, b->leaf); 13 sorts last
a in {1,3} and b in {1,3}: compare key (unsigned)            // registers and temps: by key
a in {1,3} only: -1;  b in {1,3} only: 1;  either is kind 6: -1
size differs: by size (signed);  then TyCompatible fails: by ty
both kind 4/5 with a frame home: by (frame offset + offset + _Function+0x50/0x51)
else: by offset, then by base->ssr->key (front-end key)
```

## 7. Types (type.obj, `p2type`)

TY = 16 bits: class = ty>>12, size = ty&0xfff bytes. [C] `p2type`(FE basetype, size) 0x10b33670 (getattr.c)
maps FE basetype → class:

| FE | TY |
|---|---|
| 1 | 0x1000\|size |
| 2 | 0x2000\|size |
| 3, 0xb, 0xc | ptr class of `_TyPtrType` (0x3004) |
| 4 | class of `_TyUPtrType` (0x4004) |
| 5, 10 | 0x5000\|size (10 → 0x500a) |
| 6 | 0x6000\|size (size < 4096, else 0x6000) |
| 7 | 0x6000\|size, or 0x8000 when size is 0 |
| 0xd | 0xc008 / 0xd010 / 0xe010 / 0xf010, chosen by size through a byte table |
| 0xe | 0x7000 |
| 0x11 | 0xf010 |
| 0x12 | 0xe010 |

`TyGetIndex` 0x10bf7940 maps a TY to a dense index for tables. int sizes 1/2/4/6/8 → 1/2/4/5/6, uint → 7/8/10/11/12,
float 4/8/other → 13/14/15, ptr 4 → 16, uptr 4 → 17, ptr/uptr 6 → 18, block 1/2/4/8/other → 20..24,
class 7 → 25, 0xc → 26, 0xd → 27..29, 0xf → 30..32, 0xe → 33..35, 9/10/11 → 36..38, void → 39. [C] (jump
tables 0x10bf7ab0, 0x10bf7af0, 0x10bf7b10, 0x10bf7b30)

`TyCompatible` 0x10bf7b50: equal → true. Integer and pointer classes (1-4) are compatible with each other at
equal size. Block classes 6 and 7 are compatible with each other and with 1-4 at equal size (not under
`_Wvm_flag`). SIMD classes 0xd-0xf are compatible with each other. [C] `TyIsLessPrecise` 0x10bf7c20,
`TyMaxInt` 0x10bf7ca0, `TyMinInt` 0x10bf7cf0 use class 1 = signed. [C]

## 8. Front-end symbols (p2symtab.obj)

**Tables.** `_Symtab0` is the module-global hash with 0x400 buckets by `key & 0x3ff`; the scan bound in
`findsym` 0x10bc13e0 puts its end at 0x10d3eb78. `_Symtab` points to the current function's local table
(`CurFunc+0x14`, getmem 0x1000 in RdrReadProc). `symadd` 0x10bbd3a0 pushes at the head of the bucket. Lookups:
`symtablook` 0x10bbdfb0, `symlook` 0x10bbe1b0 (global, then local), `symlooklocal`, `symlookglobal`; class 0x21
records forward to `aliasKey` (+0x33). Under LTCG it uses `ModSymLook`/`ModL2SymLook` and `_L2Symtab` (0x4000
buckets, `(k>>16 ^ k) & 0x3fff`). [C]

**Records.** `readsym` 0x10bbaea0 reads one IL record. The first byte is the record class, stored in
`SSR.cls` (+0x30); data records 1/2/0x1a are stored as class 1. [C] Data record field order: level byte, key
(`keyread`) → +0x28, subclass → +0x31, name, sclass byte → attr bits 14-16, segclass byte → attr bits 5-8,
size → +0x1c, flags → +0x20, optional segment key → +0x0c (`.tls$` check), FE type index → +0x2c. The SSR is
built in a stack buffer and copied into `newsym`. [C] Other classes:

- 3: label. Number `+0x3f = _LabelCounter++` when it has no name.
- 4/0xe/0x10: function. 0xe carries the symbol and tuple IL offsets, +0x5c and +0x58, which RdrReadProc
  uses.
- 5: member. +0x3b = parent SSR.
- 7, 8, 0xb: emitted data.
- 9: segment. +0x4d is its kind, read by FeSymIsReadOnly.
- 0x12: file name. 0x18: warning info. 0x19: module.

The per-class sizes are in `ssr_sizeof` 0x10bbd450 and in `il_sym.h`. [C]

**c2-made front-end symbols.** `newnametabwithname`(segclass, arena, name, symtab, basetype, size, sclass,
align) 0x10bbe360 makes a class-1, subclass '&' SSR with `flags |= 0x2000` and key `_Key++`. `_Key` is c2's
counter; the IL sets the start key through the pragma record that writes `_P2_Start_Key`
(`read_pragma_info` 0x10bba130, p2pragma.c; how `_Key` is seeded from it is [I]). `newlabel` 0x10bbe280 and `newkey` 0x10bbc190 also take `_Key++`. [C] Users:
`SymPromoteTmpvar` 0x10bf0260 / `SymPromoteTmpvarInSymtab` 0x10bf0380 make a stack variable for a temporary.
They create an SSR (segclass 4, sclass 1) and turn every chain member into kind 4 **in place, keeping its
key**, and set its frameKeys bit. `RdrUnextract` 0x10bd2da0 makes block temps; others are EH, GS, OpenMP. [C]

**Front-end keys versus source order.** The IL keys are written by c1, so their order is c1's numbering,
which is not analysed here. c2 compares them only in the last tie-break of SymCmpSym. [C] The /Od frame
order of `t7.c` (z, x, y for declarations x, y, z) shows that the debug build orders locals by
something other than first use. [C] Whether that is FE key order or bucket order is [I].

## 9. Experiments

The sources are in `work/scratch/glass_sym_t/` and were compiled with
`uv run tools/msvc.py /nologo /c /O2 /FAsc ...`.

- `t5.c`: one loop with three calls; the spilled loop temp is tv203 in `fa` and `fb`.
  - Adding `h(n+1);` before the loop gives tv204 (`fc`); three such calls give tv206 (`fd`).
  - Adding it **after** the loop also gives tv204 (`fe`): the reader makes its temps before the loop
    optimizer runs.
  - Adding `int q = n*2;` and returning `s+q` leaves tv203 (`ff`).
- `t6.c`: k = 0..40 extra `h(n+j);` calls before the same loop. The tv numbers are 203 204 205 206 | 143 ..
  158 157 141 148 | 65 67 69 ... 95 129 131. Rising runs alternate with drops, which shows free-list reuse
  after GC and a different temporary being spilled (from k = 23 on, two keys per added call). The
  numbers never fall in 96..127. [C] that this happens; the per-k explanation is [I].
- `t7.c`: /O2 frame order = order of first use (fa x,y,z; fb z,y,x; fc, declared z,y,x and used x,y,z,
  gives x,y,z; fd y,x,z). [C]

## 10. hash.obj and addr.obj (where they touch symbols)

- `HshInit` 0x10b5e580 clears `_HshTable` (101 buckets) and allocates the alias kill vectors. `HshEnter`
  0x10b5f480 computes the key (opcode&0xfff + extra + leaf values) % 101 (`HshGetKey` 0x10b5f9b0), looks it up
  with `HshFindEntry` 0x10b5fa00, and on a miss calls `HshInsertEntry`. That makes a 0x14-byte HSH (getmem
  arena 0xc) and a hash var through `SymMakeHashVar`, and sets `_HshTmps[key]`. Opcode 0x27c entries go on
  `_HshIndirList` through SYM+0x30. `HshFreeSymsAndLeafTuples` 0x10b5ea60 clears `sym->hsh`. Callers:
  `GlobalOptimize` 0x10b49300 and `GoptTermFunc` 0x10b4a1e0. [C] So within global optimization the hash
  vars' keys follow the order in which `HshBuildTable` walks the blocks. [I] Assertions (opcodes 0x2b7..0x2bc)
  go through `HshEnterAssertion` and use TY 0x9000. [C]
- `AddrModes` 0x10c09cf0 (called from `dop2_PreLowerGlobalOpts`, `dop2_Codegen`, `EhGenFunclet`) visits each
  instruction tuple. For a dst or src that is an indirection of size 1/2/4/8 whose address is a temp leaf
  (operand kinds 1/2) with a single def (`sym->def`), it calls `BuildAddr` 0x10c09ea0 to fold the def into
  an addressing mode. `AdrKilledBetween` 0x10c0b5c0 checks for interfering stores between the def and the
  use (`ItfPartialInterf` under global optimization). `AdrFoldLea` 0x10c0b640 (from `Lower`) makes temps
  with SymMakeTmpReg. [C] The details of BuildAddr (5816 bytes) are not mapped here.

## 11. Cross-references for the other families

- Tuple: +0x04 opcode, +0x08 operand kind byte, +0x0a TY, +0x18 SYM*, +0x1c second SYM*
  (register / base), +0x28/+0x2c src/dst operand lists of an instruction tuple, +0x24 label SSR for a label
  tuple (`_ExitLabel`, `SSR+0x33` back-link). See il_tuple.
- Bit vectors: frameKeys/globalKeys/modGlobalKeys are SBVs (bitvect.h), indexed by SYM.key, with nodes
  holding 64 bits (key & ~63).
- Flow graph: `SymWalkTuples` follows the function's tuple list (`func+8 → *+0x1c`).
- Stack layout: `SymSetOffsets` 0x10beff60 (called from `EncEmitFunc`) adds `_Function+0x50` (locals and
  homed temps) or `+0x51` (params) to `ssr->offset` and propagates it into each member's `offset`.
  `StkStackAlloc` 0x10be8640 is the allocator (not mapped here).
