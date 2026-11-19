# Load reuse across blocks and read-modify-write: who decides, the rules, and the source constructs that flip them

Scope: why c2 keeps a memory value in a register and reuses it in a later block (or the same block), why it reloads
it instead, and when `x op= y` on memory becomes one `op [m], y` instead of load / op / store. All four builds:
14.00.50727.762 (default), 15.00.30729.1, 13.10.3077, 13.10.3052. Read opt.md §4 and §5 first (CSE and the kill
table); this page is the part they did not cover: **which reads have the same value number at all**.

**Tags.** **[C]** seen in the code (address given), in a trace or in a probe compile (probe named); **[I]** inferred.
Probes: `work/scratch/loadcse/` (`p1.cpp` .. `p8.cpp`; `run.sh FILE` compiles with chosen builds, `lc.sh FILE FUNC`
runs the tool), the construct table `work/scratch/loadcse/cons/cons.cpp` + `cons.py` (all four builds, counts loads,
stores and RMWs of `[reg+92]` per function; result in `cons/table.txt`). Regression tests: `tests/data/loadcse.cpp`.

## 0. Summary for decompilers

1. **The decision is GlobalOptimize's CSE (sweep 2), not register allocation.** Two reads of one location become one
   load only if HshBuildTable gave them the same hash temp and the value is available on every path into the later
   read (opt.md §4). Color never rematerialises a CSE'd load from memory (a CSE temp it cannot keep goes to a `tv`
   slot), and the post-allocation passes only remove reloads of values still sitting in a register. **[C]**
2. **Same location is not same value number.** HshFindEntry (14.00 10b5fa00) compares operands with TupCmpLeafs, and
   during GlobalOptimize that includes the INDIR leaf's **points-at-set (PAS) index** (`_FTupCmpIndirPasIndex`,
   TupCmpMemRefFields 10bf6f70). Three source patterns give the same address another value number (§2): an **alias
   variable** (`T& r = p->m`, `T* q = &p->m`, an inline method's `this` on a sub-object) used twice or more (R3); a
   **dereference of a pointer loaded from memory** (`p->q->m`, `(*pp)->m`, `gq->m`, `*pf` with `int*& pf`) in the
   block that also stores through it (R2); and everything built on such a value (R4). The IL type does **not** split
   values (`:2004` and `:1004` reads share a hash temp: TyCompatible). **[C]**
3. **Kills and paths are opt.md §5**: calls, stores through other pointers, variable-index stores. No partial
   redundancy: a read on one arm only is not available after the join (R6). **[C]**
4. **RMW is Opeq (14.00 10c715f0 / opeq_tuple 10c71660), phase 3.** It needs: the stored value computed by the tuple
   just before the store, by add/sub/and/or/xor/shl/shr/sar, from an operand that is **a memory leaf equal to the
   destination** (directly, or through a chain of the *same* operator: deep_opeq 10c71950). A CSE'd read of the
   location is a temp, not a memory leaf, so **any earlier reuse of the old value blocks the RMW** (M1). `(x & ~1) | 2`
   is a mixed chain and never an RMW (M6). **[C]**
5. **13.10 forms the same RMWs in the IL but splits many of them back after allocation** (between
   `dop2_CodegenUpdate` and `dop2_CodegenFinish`): a register source operand (`or [m], reg`), a reused result, a
   read before it, `volatile`. 14.00/15.00 keep them (and reload the value if it is used again). **[C]**
6. **Fixes, by the rule** (table §4): to make a reload, read through an alias variable with two or more uses, or
   through an inline method of a sub-object that touches the member twice; to make one load, copy a loaded pointer
   into a local (`Q* q = p->q;`) or drop the reference/pointer alias (`rwsweep --families unalias`). To get
   `op [m], y` where ours has load/op/store: make the old value's other read go through another spelling (R3) so it
   is not CSE'd with the RMW's read.

## 1. Where it is decided

| step | pass (14.00) | 15.00 | 13.10.3077 | 13.10.3052 | role |
|---|---|---|---|---|---|
| alias classes | ComputePointsAtSets 10b79740 (in ItfBuildInterference) | 510c0e90 | 107661c6 | 107a1e60 | gives every INDIR leaf its PAS index |
| single-use substitution | GoptSingleUseCopyProp 10b4e110 | 5109c510 | 107633b7 | 107a568b | removes alias variables with one use (R3 does not apply then) |
| value numbers | HshBuildTable 10b5eb30, HshFindEntry 10b5fa00 | 510ad6a0, 510ae630 | 107637f3, 10761176 | 107a556f, 1076eb09 | one hash temp per (opcode, compatible type, operand leaves) |
| leaf compare | TupCmpLeafs 10bf6d20 / TupCmpMemRefFields 10bf6f70 | 51145e10 / 51146050 | 1075a137 / 1079a06f | 1076be90 / 1076e554 | base, displacement, scale, index, align, **PAS** |
| availability | GdfCalcAvail 10b394c0 | 51086370 | 10768a0b | 10796d13 | intersection over predecessors |
| CSE | GoptCommonSubExpression 10b515e0 (sweep 2) | 5109fa10 | 10762b53 | 10797064 | deletes a read whose temp is available |
| RMW | Opeq 10c715f0, opeq_tuple 10c71660, deep_opeq 10c71950 | 511d9490, 511d9500, 511d9820 | 107815f0, 10781845, 10788c9f | 10798f77, 10798ea2, 1079a068 | `t = X op y; X = t` → `X op= y` |
| after allocation | LateGlobalOptimize 10cc9e20, MscClean 10b9f040 | 511b19c0 | (13.10: RMW split in `dop2_CodegenUpdate`..`dop2_CodegenFinish`) | same | remove reloads of a value still in a register (isel.md §11.2, §11.3) |

Addresses of 13.10 and 15.00 are from `work/index/<build>/c2.dll.csv` (carried names, tools/c2port.py). **[C]** for
the 14.00 code read below (HshBuildTable, HshFindEntry, Opeq); for the other builds **[C]** that the same functions are
the deciders by trace (the `loadcse.py` reports of `tests/data/loadcse.cpp` agree on all four builds, §5), **[I]** that
their code is the same.

**How it was found.** `c2trace.py il --diff` between `@HshBuildTable@4`, `@GdfInsertArtificialAssigns@4` (first
entry: right after the hash table), `@GoptRemoveAssumes@4` (after sweep 1), `@GoptLocalCopyPropagation@4` (after
sweep 2), `@Opeq@4`, `@AddrModes@4`, `@Color@4`, `@EncEmitFunc@4`. In every probe the load count at Color entry is
fixed by sweep 2; nothing between Color and encoding adds a load. **[C]** The PAS index is not in the normal dump;
`C2TRACE_ILPAS=1` makes the tracer print it after every memory operand (`[t128+0]@7:1004`), at leaf +0x1c on 14.00 and
15.00, +0x24 on 13.10 (build tables `dll.ilpas_off`). The 13.10 offset is **[I]** from behaviour: its value differs
exactly where the hash temps differ (p4 `c`: 5/5 against 0x1d/0x1e).

## 2. The rule: when two reads of one location are one load

A later read R2 of location L reuses an earlier read R1 (one load) **iff all of**:

1. **Same hash temp** (HshFindEntry). Opcode the same, types compatible, and every operand leaf equal by
   TupCmpLeafs. For a memory operand that is: the same base *as written after copy propagation* (a symbol, or a hash
   temp of the address), the same displacement / index / scale, and the **same PAS index**. **[C]** (code; traces)
2. **R1 dominates R2 and nothing on any path between them may write L** (opt.md §5 table; GdfCalcAvail's
   intersection). **[C]**
3. R1 is not volatile; `volatile` reads are never joined (cons `A7`, `C7`). **[C]**

The reasons a read is reloaded, as `loadcse.py` names them:

**R1 type: not a reason.** The IL types `:2004` (unsigned; compares) and `:1004` (pointer) of one location get the
same hash temp (TyCompatible): p1 `f1`, the loop guard `[t129]:2004` and the body `[t129]:1004` are both `t133`. **[C]**
(opt.md §4 rule 8's null-test pair differs by more than the type; not re-examined here.)

**R2 pas: a dereference through a pointer loaded from memory, in the block that stores through it.** When the
pointer is a value read from memory (a member `p->q`, `*pp`, a global pointer with an offset added, `*pf` where `pf`
is a reference to the pointer parameter), the dereferences in the block that stores through it get PAS indices of
their own (0x1f/0x20/0x21 on 14.00, 0x1c/0x1d/0x1e on 13.10), different from those of the same dereferences
elsewhere (7/6). So the test `if (!(p->q->m & 3))` and the update `p->q->m = (p->q->m & ~1) | 2` in the arm are
different values: `test byte [ecx+92],3` and then `mov edx,[eax+92]` again. **[C]** (p4 `c`, p6, p7, p8; cons `B0`
`B2` `B3` `B4` `B6` two loads on all four builds; `B1` `B7` `B12` one load). Which dereferences share the store's
class, in the probes **[C]**:

| construct (test in block 1, update in the arm) | loads of `m` | probe |
|---|---|---|
| `p->q->m` everywhere | 2 | cons `B0`, p4 `c` |
| the arm splits it into `int v = p->q->m; p->q->m = (v & ~1) \| 2;` | 2 | cons `B4` |
| `Q*& rq = p->q;` then `rq->m` | 2 | cons `B2` |
| `Q*& q` parameter, `(*pp)->m`, `gq->m` (global, offset 92) | 2 | cons `B6`, p8 `B11`, cons `B3` |
| `Q* q = p->q;` / `Q* q = *pp;` (a local copy of the pointer) | 1 | cons `B1`, p8 `B12` |
| `Q* q` parameter, `int* p` parameter | 1 | cons `B7`, p3 `a` |
| global pointer used at offset 0 (`gq0->m`, `*gp`) | 1 | p8 `B8`, p4 `f` |
| `int& r = p->q->m;` and every use through `r` | 1 | cons `B5` |

A plain store in the arm (`p->q->m = 2`), or a call between the read and the store (p6 `c3`), keeps one class
(p6 `c6`, `c3`: the PAS stays 7/6). Why ComputePointsAtSets splits exactly these is **[I]** (presumably a points-at
set per stored-through pointer *value*, and a loaded pointer is a new value in every block). The dependent loads
built on a renumbered pointer get new numbers too (**R4 dependent**: `[t136+0]` after `t136 = [p+4]` was renumbered).

**R3 base: an alias variable.** A pointer or reference variable whose definition is an address expression is not
copy-propagated by GoptCopyPropagation (only copies of a variable or constant are), so a read through it has the base
`_r` and its own hash, never equal to the direct read's `[t128]` with `t128 = p + off`. Color folds the variable back
(`mov eax,[eax+4]`), so the listing shows no trace of it, only the extra load. **Condition: two or more uses.** With
one use GoptSingleUseCopyProp substitutes `p + off` into it first (`il --count _r:` on cons `A1_ref`: 2 before the first
GoptSingleUseCopyProp, 0 after; `A15_ref_two_uses` keeps 3 to Color) and the read joins the others. **[C]**

| alias | uses | result (all four builds) | probe |
|---|---|---|---|
| `int& r = p->m;`, `int* q = &p->m;`, `int const& r = x.m` | 1 | substituted: one load | cons `A1`, `A2` |
| the same | 2 or more | separate value: 3 loads (block 1 + each arm) | cons `A15`, `A17`, `A21`; p2 `d`, `i` |
| an inline method on a sub-object (`p->s.get()`, `this` = `p+4`), each call | 1 per call | substituted: one load | cons `A19`, `A20` |
| an inline method whose body reads **and** writes through `this` (`pop_back() { --e; }`), or two reads | 2 in one call | separate: reloads, and the write can be an RMW | p1 `f4`, Spore 00b8de60 |
| `V& r = *(V*)&b;` defined before a loop and used inside it | 1 in the loop | kept (not substituted into the loop) | p1 `f1` |
| `P* p2 = p;` (a copy of a pointer variable) | any | propagated: one load | cons `A13` |
| inline getter by value / returning a const reference | any | one load | cons `A4`; p2 `b`, `c` |

**R5 kill** and **R6 path**: opt.md §5 and §4 rule 2. In the probes **[C]**, all four builds: a call (cons `A10`), a store
through an `int*` or `char*` (`A8`, `A11`), a store to another member of the same base (`A9`: no kill, one load), a
read on one arm only and again after the join (`A14`: 2 loads).

**After allocation** the count can only go down: LateGlobalOptimize (14.00 10cc9e20) turns a reload of a value still in
a register into a copy (p4 `c`: `mov eax, ecx` where the IL had a second `[p+4]` load), and 13.10 removes more of
them (`tests/data/loadcse.cpp` `twolevel`: 2 loads of `m` at Color, 1 in the 13.10 code). `loadcse.py` prints both
counts. **[C]**

## 3. The rule: when `x op= y` on memory is one instruction

Opeq walks every `OPASSIGN` (0x28d) / `OPVOLATILEASSIGN` (0x28e) whose destination is not a register temp
and, in opeq_tuple, requires **[C: decompile of Opeq, opeq_tuple, deep_opeq, opeq_tryToCreateOpeq; probes]**:

1. the stored value is a temp defined by the tuple **immediately before** the store (the store's `+0x10` back link)
   (else **M4 not-adjacent**);
2. that tuple's operator is add/sub (0x2a5/0x2a6: `x - y` only with x the memory), and/or/xor (0x2aa/0x2ab/0x2ac,
   either operand), or a shift (left operand); multiply, divide, neg, not, `y - x` have no form (**M3 operator**;
   isel.md §7.2);
3. an operand is a memory leaf TupCmpLeafs-equal to the destination, directly or through a chain of the **same**
   operator (deep_opeq recurses only while the opcode equals the first one) (else **M6 mixed-chain**, e.g.
   `(x & ~1) | 2`; same chain `(x | a) | 2` is one `or [m], ..`). At Opeq time the PAS index is not compared any more
   (`_FTupCmpIndirPasIndex` is cleared in GoptTermFunc), so two spellings of one address match here;
4. the old value is that memory leaf, i.e. OptFuncExpr folded a **single-use** load into the operand. If sweep 2 made
   the read a CSE temp (shared with another read: a test, an address computation, the value read into a local), it is
   not a memory leaf and nothing happens (**M1 shared-load**);
5. on 14.00 and 15.00 a result used again (`p->m += 4; return p->m;`) still gives the RMW, followed by a reload (opeq_tuple
   makes a new temp and re-reads it: the TupMakeTmpReg / TupReplaceDstMemOpt path; **[I]** that this path is the one); on 13.10 that is load / add / store with the register reused (cons
   `C11`, `C14`) (**M2 shared-result** is reported where the temp has several uses before Opeq).

| construct (cons.cpp) | 14.00 | 15.00 | 13.10 / 13.10.3052 |
|---|---|---|---|
| `p->m += 4;` `p->m <<= 2;` `p->m += 4; p->m += 8;` (C0, C10, C12) | RMW | RMW | RMW |
| `E* x = w->e[-1]; --w->e;` (C1: the end pointer read once for both) | load/op/store | same | same |
| the same through `((V2*)&w->e)->back()/pop()` or `E**& r = w->e` (C2, C2b) | **RMW** | **RMW** | load/op/store (split after allocation) |
| `p->m = (p->m & ~1) \| 2;` (C3, mixed chain) | load/op/store | same | same |
| `p->m = (p->m \| a) \| 2;` (C4) | RMW `or [m], reg` | RMW | split after allocation |
| `p->m = p->m * 3;` `p->m = 5 - p->m;` (C5, C6) | load/op/store | same | same |
| `p->vm += 4;` volatile (C7) | RMW | RMW | split |
| `if (p->m & 1) p->m += 4;` (C8: the test's read is CSE'd) | load/op/store | same | same |
| `if (p->m & 1) { int& r = p->m; r += 4; }` (C9) | **RMW** | **RMW** | **RMW** |
| `p->m = p->m + 4; return p->m;` (C11) | RMW + reload | RMW + reload | load/op/store, register reused |
| `h(p->m); p->m += 4;` (C13: read before, a call between) | RMW | RMW | split |
| `p->m += 4; h(p->m);` (C14) | RMW + reload | same | load/op/store |

The 13.10 split **[C trace]**: on `C4` the tuple `or [t128] <- [t128] t64` is at `@Color@4` and `@dop2_CodegenUpdate@4`;
at `@dop2_CodegenFinish@4` it is `or edx, ecx` between a new load and store (the tuple keeps its address). Which
function inside that window does it, and its exact condition, is open **[I]**: it keeps immediate-operand RMWs with no
other use of the location (C0, C10, C12, C9) and splits the rest seen.

## 4. Source constructs that flip a decision (measured on all four builds)

`cons.py` result, loads (L), stores (S) and RMW instructions on the probe location; identical on the four builds except
where noted. Full table: `work/scratch/loadcse/cons/table.txt`.

| want | construct | before → after | probes |
|---|---|---|---|
| one load instead of two (cross-block) | copy a loaded pointer into a local: `Q* q = p->q;` | L2 → L1 | B0 → B1, p8 B11 → B12 |
| | drop a reference to a pointer parameter (`int*& pf = flags;` → use `flags` or `int* pf = flags;`) | L2 → L1 | B6 → B7; F3 0x006bd9c0 |
| | drop an alias used twice (`int& r = p->m;` → `p->m`), rwsweep family `unalias` | L3 → L1 | A15 → A0 |
| | read the value into a local once (`int v = p->m;`) | (L1, keeps it a variable) | A3 |
| two loads instead of one | read the later one through a reference/pointer variable used twice or more | L1 → L2/L3 | A0 → A15, A16, A17, A21 |
| | through an inline method of a sub-object that reads and writes the member | L1 → L2 + RMW | p1 f0 → f4; Spore 00b8de60 |
| | `volatile` (also no CSE of the two reads among themselves) | L1 → L2 | A7 |
| | a store through a pointer, a call, between | L1 → L2 | A8, A10, A11 |
| **no** change (front-end identical or substituted) | single-use reference/pointer alias, inline getter by value or by const reference, pointer copy `P* p2 = p`, reference parameter | — | A1, A2, A4, A12, A13, A19, A20; p2 b, c |
| `op [m], y` instead of load/op/store (14.00, 15.00) | make the earlier read of the old value another spelling (R3), e.g. `back()`/`pop_back()` through a sub-object accessor, or the update through `int& r = p->m` | S1 → RMW1 | C1 → C2, C2b; C8 → C9 |
| | one operator chain instead of a mixed one | S1 → RMW1 (not 13.10) | C3 vs C4 |
| load/op/store instead of `op [m], y` | read the old value first through the same spelling (`x = p->e[-1]; --p->e;`) | RMW → S1 | C2 → C1 |

For `realize.py` (another agent owns it): constructs that need a type, not catalogued by rwsweep:
`alias2:CHAIN` (a reference `T& r = CHAIN;` used twice; the read through it reloads), `ptrlocal:CHAIN` (a loaded
pointer chain into `T* q = CHAIN;` before the block; one load instead of the per-block alias class),
`subobj:MEMBER` (a member accessed through an inline method of a sub-object struct, as in Spore 00b8de60).

## 5. The tool: `tools/loadcse.py`

```sh
uv run tools/loadcse.py SRC --func F [--c2 BUILD] [--loc REGEX] [--all] [--json OUT] -- /TP /MT /O2 /GS /EHsc /Gy
```

One traced compile (C2TRACE_ILPAS=1) dumps the function at `@GdfInsertArtificialAssigns@4` (first entry),
`@GoptLocalCopyPropagation@4`, `@Opeq@4`, `@Color@4` and `@EncEmitFunc@4`. For each location read more than once
(addresses resolved through address temps: `[t128+0]` with `t128 = _this + 352` prints as `[_this+352]`, a loaded
pointer as `{[_p+4]}`) it lists every read: FIRST, REUSED (deleted by sweep 2), or RELOADED with the rule (R2 pas, R3
base with the alias variable's name, R4 dependent, R5 kill with the call or store, R6 path), its block, hash temp, PAS
index and the spelling, then the number of reads at Color entry and in the final code. For each store of `X op y`
it prints FORMED, FORMED + SPLIT after allocation (13.10), or NOT FORMED with M1..M6. Pairs use dominators when the dump
has branch targets (the 14.00 extended dump); on 15.00 and 13.10 (plain dump, no targets) they are paired in stream
order and the output says so, so a read in a sibling arm can be shown as R5/R6 where it is simply on another path.

Example (14.00, p4 `c`; F3 0x006bd9c0's shape):

```
[_p+4]   loads at Color entry: 2, in the final code: 1
  FIRST                  blk 2   t130   pas 7   :1004  [t128+0]
  RELOADED R2 pas        blk 3   t136   pas 20  :1004  [t128+0]   alias class (PAS index) 7 / 20, nothing in between
[{[_p+4]}+0]   loads at Color entry: 2, in the final code: 2
  FIRST                  blk 2   t132   pas 6   :1004  [t130+0]
  RELOADED R4 dependent  blk 3   t138   pas 21  :1004  [t136+0]   its base t136 is a reload of [_p+4] ...
read-modify-write:
  store 15c1f44 [{[_p+4]}+0]: NOT FORMED M6 mixed-chain   OPOR over OPAND of ...
```

`rwsweep.py --families unalias` (in the default set) removes each reference or pointer alias of a member chain and
spells its uses as the chain; `realize.py` constructs for the other direction are listed in §4.

## 6. Benchmark functions (before → after; bdiff = tsweep.bdiff, regdiff as before)

| function | rule | change | before | after |
|---|---|---|---|---|
| Spore 00b8de60 (15.00) | R3 + Opeq: the original reaches the vector at +0x15c through inline methods, so `back()` and `pop_back()` read the end pointer separately and `pop_back()` is `add [esi+0x160],-4`; the loop head pad comes with it | `empty()/back()/pop_back()` through `((OwnedVec*)&mField15C)->` (v5.cpp) | regdiff 13, bdiff 13 | **MATCH** (Spore fingerprint: byte-identical modulo relocations); bdiff 1 (padding mask, see below) |
| Spore 00c94c30 (15.00) | R3: `x.mpEnd` and `this->mpBegin` reloaded per arm, `x.mpBegin` kept | per-arm reference bindings (v4.cpp) | shape 28, regdiff 64, bdiff 65 | shape 11, regdiff 63, bdiff 64; the rest is allocation (x/x.mpBegin swap ebp/ebx, `x.mpEnd` in a stack temp) |
| F3 0x006bd9c0 (14.00) | R2: `unsigned*& pf = flags;` makes `*pf` a dereference of a loaded pointer, so the else arm's test and update are two loads | `unsigned* pf = flags;` (lc1.cpp; also rwsweep `unalias` v0000) | regdiff 14, bdiff 16 | the load decision flips as the original (one load, `test cl,3`), but both arms then pick ecx and their heads and tails merge: regdiff 18, bdiff 19. Open: the original's arm 1 is in edx (scratch rotation) |
| F3 0x00ab47e0 (14.00) | not load CSE: `cmp [eax+8],esi` against `lea edx,[eax+8]; cmp [edx],esi` is the fold of an address temp into the compare at allocation (regalloc.md FoldTooCheapDefs/CmdSubstDef) | — | regdiff 2 | open |
| M&B 0051d7c0 (13.10) | not CSE: the guard `cmp [eax],0` against `mov ecx,[eax+edx]; test ecx,ecx` is load folding into the compare (the value is single-use in both) | — | regdiff 17 | open |

**bdiff and the byte check disagree on 00b8de60 v5**: Spore's fingerprint says byte-identical, bdiff says 1. The only
difference is the loop-alignment pad: the original's 14 pad bytes decode as `jmp +0xc; lea esp,[esp]; jmp +3;
lea ecx,[ecx]`, our listing has `npad 14`, and bdiff keeps the first `jmp` as an instruction.

## 7. Open

- Why ComputePointsAtSets splits the alias class of a loaded pointer's dereferences per storing block (R2 mechanism),
  and the exceptions (p6 `c3`: a call between read and store keeps one class).
- The 13.10 RMW split: the function in `dop2_CodegenUpdate`..`dop2_CodegenFinish` and its exact condition.
- 15.00 and 13.10 dumps carry no branch targets, so loadcse.py pairs reads in stream order there.
- Address-temp folding into a compare (F3 0x00ab47e0) and load folding of a single-use guard (M&B 0051d7c0) are
  allocation/lowering decisions not covered here.
