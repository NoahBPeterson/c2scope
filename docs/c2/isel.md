# c2 instruction selection: which x86 instruction c2 picks for integer code

Scope: c2.dll 14.00.50727.762 (VS2005 SP1), x86, integer code, `/O2` (speed, `_Speed` = 1) and `/O1` (size,
`_Speed` = 0). This page says how IL operations become x86 instructions and which of several equivalent
encodings comes out. Not covered here, see instead: x87 code (x87.md), register choice (regalloc.md,
regalloc_cost.md), instruction order inside a block (sched.md), block order and tail merging (layout.md,
il_fg.md), frame layout (frame.md), what c1xx hands over (frontend.md).

**Tags.** **[C]** = confirmed: shown by a probe compile, and where an address is given also read in the code.
**[R]** = read in the code, not probed. **[I]** = inferred. Addresses are c2.dll VAs.

**Probes.** All in `work/scratch/isel/`. `run.sh FILE /O2` compiles one file and prints a compact listing;
`out/<file>.O2.fmt` / `.O1.fmt` hold one line per function. `passdiff.py FILE FUNC passA,passB,..` prints, per
function, the IL difference between the entries of consecutive passes (it wraps `c2trace.py il`), which is how
the "pass" column below was established. `opnames.txt` is the dump of `_TupOpcodeName` (10ceb3d8): machine
opcode numbers used below (1 mov, 0x12 lea, 0x2d add, 0x2f cmp, 0x33 test, 0xc2 movzx, ...) come from it.

**Re-verification.** A second pass re-tested the /O2 rules with fresh C++ probes in `work/scratch/isel/verify/`
(`run.sh FILE.cpp` compiles with `/O2 /TP /MT /GS /EHsc /Gy`; `show.py out/FILE.fmt` prints one line per function),
in free functions, member functions and functions with an EH frame, and against Fallout 3's code. Probe lists
marked "(re-verified, C++ game flags)" held there; the tally and the corrections are in §14. The /O1 columns
were not re-tested.

**The CPU level is fixed.** cl 14.00 rejects `/G5` `/G6` `/G7` (warning D9002) and never passes `-G#` to c2, so
MODULE+0x420 is always 7 (il_pipeline.md §3.1). Every "blend / Pentium 4" choice below (`add r,1` instead of
`inc`, `add r,r` instead of `shl r,1`, no `push [mem]`) therefore depends only on `_Speed`. **[C]** (`/Bd`
shows c2's command line; `cmp.c` compiled with `/G5`, `/G6`.)

---

## 1. Where each decision is made

| step | pass (il_pipeline.md §5.3) | decides |
|---|---|---|
| c1xx | front end | byte/word extracts through pointers (`(*p>>16)&0xff` arrives as a 1-byte load of `[p+2]`), struct copies as block moves, `x % 2^k` |
| read | RdrGetTuples → SwGenSwitch 10bed250 | switch method: chain, binary search, table (§9) |
| 3 | Half 10c3aa80 | byte 1 of a variable (`(v>>8)&0xff`) (§4) |
| 3 | SizeOpt 10be0340 | 8/16-bit compares and operations (§4) |
| 3 | Mul2Shift 10baa730 | multiply and divide by constants (§3) |
| 3 | Opeq 10c715f0 | `x = x op y` as a read-modify-write (§7) |
| 3 | AddrModes 10c09cf0 | address expressions and `lea` (§2) |
| 3 | MscBuildQuestion | which if/else diamonds become OPQUESTION (§5) |
| 4 | Lower 10c3e5e0 | machine opcodes, operand legality (memory operands), setcc/sbb templates, call sequences, div/rem |
| 5a | Peeps 10c5ba80 | compare removal after arithmetic, and/test folding, tail calls (§6, §8) |
| 5b | Globregs, Color | constants kept in registers, loads folded back into uses (regalloc.md) |
| 5c | Sizeext 10c65000 (/O2 only) | `mov r8,[m]` widened to `movzx r32,[m]` (§4) |
| 5c | MdmInsertEntryExit 10c53e80 | prologue and epilogue pseudo-instructions `_epush`, `_epop`, `_eret` |
| 5e | LoFinalLower 10c28970 | `xor r,r`, `test r,r`, `or r,-1`, lea→add/mov, inc/dec, shl→add, stack cleanup forms, partial-register fixes (§11.1) |
| 5e | LateGlobalOptimize 10cc9e20 | redundant copies and loads across the function (§11.2) |
| 5f | FgDoLateFlowOpts, FgCodeMotion | tail merging, epilogue duplication (layout.md) |
| 5h | MscClean 10b9f040 | reuse of a register that already holds a constant, an address or a memory value (§11.3) |
| 5h | Schedule | order (sched.md) |
| 5i | FgLowerSwitches 10b2a0e0 | jump tables emitted; `_epush`/`_epop`/`_eret` become `push`/`pop`/`ret` |

The split matters for prediction: a form decided before register allocation (lea, setcc, load folding) can be
predicted from the source alone; a form decided after it (§11) depends on which registers hold what at that
point, and on the instruction order *before* scheduling.

## 2. Addressing modes and `lea`

Probes: `lea.c`, `mem.c`, `ad_*.c`; `verify/v_int.cpp` a_*, `verify/v_int2.cpp` lea2, lea48, sub2, a5b, K::ehlea
(re-verified, C++ game flags). The forms are made by AddrModes 10c09cf0 (BuildAddr 10c09ea0) at the end of
phase 3, before Lower and before register allocation; `il --at @AddrModes@4,@Lower@4` shows an OPADD at the
first and an `lea` tuple at the second.

Terms: a **variable** is a named local, parameter, global, or a temporary with several uses (all are register
candidates by now); an **expression temp** is a single-def single-use temporary; **memory** is a load through a
pointer (`*p`, `p->m`, `a[i]`).

**A0. What AddrModes looks at. [C]** It walks the function backwards and calls BuildAddr for every memory
operand whose base is an expression temp, and for every 4-byte integer instruction with a register destination
(a candidate lea; 10c09d90..10c09db3). BuildAddr follows the definitions of expression temps through
assignments, adds, subtract-of-constant, multiply by 1/2/4/8 and shift left by 1..3, and absorbs them. Anything
else (subtract of a register, a load, a conversion) is a leaf. There is no cost or use-count test: a value used
twice is a variable, so it is a leaf and is computed once (ad_1.c b4: `lea esi, [ecx+eax*4]; lea edx, [esi+5]`).
No code here or in Lower reads the CPU level, and nothing splits a 3-part lea.

**A1. What a non-memory result becomes (10c0afde..). [C]**

| shape | result |
|---|---|
| `B + I`, no scale, no displacement | `lea` only if B and I are both variables and B is not the destination; otherwise a real `add` (an operand is an expression temp or memory, or the source is `x = x + y`) |
| `B + d` | `add B, d` if B is an expression temp or the destination itself; `lea [B+d]` if B is another variable (which F9 turns into `add r, d`, signed constant kept, when the registers coincide) |
| `I * s` alone | `shl` under the same test, else `lea [I*s]` |
| any scale > 1 with another part, or index with displacement | always `lea` |

So `a + b` on two parameters is `lea t, [a + b]` at Lower's entry (lea.c a1; `il --at @Lower@4`). An lea needs
both parts in registers, so **both operands are loaded**: `mov eax, b; mov ecx, a; add eax, ecx` (the final
`add` is F10, because the destination ended up in one of the part registers). Compare:
- `a - b`: not an address; `mov eax, a; sub eax, DWORD PTR b` (a14);
- `a + b + c`: `lea t, [a+b]`, then a real add of the temp with c folded: `add eax, ecx; add eax, DWORD PTR c` (a9);
- `a += b` kept in a register: `add esi, DWORD PTR _b$` (ad_1.c b7), but `c = a + b` with all three live:
  `lea esi, [edi+ebx]` (b9);
- `a + *p`: a real add; the pointer value is loaded and the variable folded (`mov eax, [eax]; add eax, a`;
  mem.c m19), while `a + gi` with a global is an lea and loads both (m18).

**A2. Operand order. [C]** Base = first operand of the add in the IL, index = second. The optimizer has put the
operands in its canonical order by then, so `a + b` and `b + a` both give `[a+b]` (ad_1.c b1, b2). A scaled term
is always the index, and a scale is absorbed only once. A pointer plus scaled index prints `[ecx+eax*4]` with the
pointer as base (a3, a19).

**A3. One address holds base + index·{1,2,4,8} + displacement (+ symbol). [C]**

| source | code |
|---|---|
| `a + b + 5` | `lea eax, [ecx+eax+5]` (a2) |
| `a + b*4 + 12` | `lea eax, [ecx+eax*4+12]`, one lea; a 3-part lea is never split (a4) |
| `a*2 + 7` | `lea eax, [eax+eax+7]` (a6; F12) |
| `a + b*8 + c + 3` | `lea edx, [ecx+eax*8]; lea eax, [edx+eax+3]`: a fourth term starts a second lea, with the constant in the last (a10) |
| `a + b + c` | `add eax, ecx; add eax, DWORD PTR c` (a9) |
| `a + b + b` | `lea eax, [ecx+eax*2]` (a32) |
| `a + 1`, `a - 5` | `add eax, 1`, `add eax, -5` (lea with base = destination, F9) |
| `p[i]`, `p[i+3]`, `s->z[i]` | `[ecx+eax*4]`, `[ecx+eax*4+12]`, `[ecx+eax*4+8]` |
| `g[i]`, `g[i+5]` | `_g[eax*4]`, `_g[eax*4+20]` |
| `&p[i+7]` | `lea eax, [ecx+eax*4+28]` |

**A4. A scale applies to one variable only. [C]** A shift or multiply of a sum is not an address:
`(a + b) << 2` is `add eax, ecx; add eax, eax; add eax, eax` (a13). The optimizer factors common scales first,
so `a*4 + b*4` gives the same code (a18) and `a*2 + b*4` is `lea eax, [ecx+eax*2]; add eax, eax` (a17).
An index that is itself a sum is computed first: `p[i + j]` → `add ecx, eax; mov eax, [edx+ecx*4]` (a22),
`p[i*2 + j]` → `lea edx, [eax+ecx*2]; mov eax, [eax+edx*4]` (a23). For a `char` array all three go into the
address in two steps: `add ecx, eax; movsx eax, BYTE PTR [ecx+edx]` (a21).
`b*4 - a` is a shift then `sub eax, DWORD PTR a` (a16): subtraction never enters an address.

**A5. Multiplies feed addresses. [C]** After Mul2Shift (§3) the ×2/×4/×8 pieces are absorbed:
`gs[i].z[j]` with a 48-byte element is `lea eax, [eax+eax*2]; lea edx, [ecx+eax*4]; mov eax, _gs[edx*4+8]`
(a29: 48i + 4j + 8 = 4(12i + j) + 8), and `a*2 + gi` is `lea eax, [ecx+eax*2]` (m84).

**A6. Where an lea turns back into something else. [C]** Only in LoFinalLower, after allocation: F8-F12 in
§11.1. So whether the listing shows `lea` or `add`/`shl` depends on the registers: destination equal to a part
→ `add`; three parts or a third register → `lea`.

**A7. Memory operands always take the whole mode. [C]** `mov eax, _gi[eax*4+8]`, `_loc$[esp+ecx*4]`,
`[ecx+eax*4+12]`. One exception (10c0aed4): when base and index are both not enregisterable (volatile), they are
added first: `mov eax, _gv; add eax, _gvp; movsx eax, BYTE PTR [eax]` (ad_2.c f4).

**A8. An address used twice. [C]** `p[i] += a` reaches Color as `lea t, [p+i*4]; add [t], a` (the address temp
has two uses). Color's FoldTooCheapDefs (regalloc.md) folds it back into the operand: /O2 `add [eax+ecx*4], edx`;
at /O1 only when IsFoldWorthIt (test at 10b0ddf6), else `lea eax, [ecx+eax*4]; add [eax], ecx` (ad_1.c e14).
AdrFoldLea 10c0b640 does the same for a base defined by an add or lea during Lower **[R]**.

## 3. Multiply, divide and shift by constants

Probes: `mul.c`, `div.c`, and `ms_tab.c` (340 constants), `ms_mul.c`, `ms_div.c`, `ms_sh.c`, `ms_cache.c`;
`verify/v_int.cpp` mul_*, mc_*, d*, r*, shl*, rot*, `verify/v_int2.cpp` r25, r7, dff, K::ehmul (re-verified, C++ game flags).
`ms_model.py` (`gen(c, speed)`) implements 3.1 and agrees with cl on imul-versus-sequence for all 340 constants
at /O2 (`out/ms_tab.O2.cmp`). Only `_Speed` matters here: `/O2 /Os` behaves as /O1 and `/O1 /Ot` as /O2.

### 3.1 Multiply (Mul2Shift 10baa730, phase 3)

**M1. Gate. [C]** (MsIntConsiderMul 10baa640, MsGenMulShift 10ba9550.) Integer multiply with one constant
operand, result exactly 4 bytes, constant ≠ 0. `short`/`char` operands are promoted first and qualify; 64-bit
goes to `__allmul`.

**M2. Budget. [C]** A shift/lea sequence replaces `imul` only if its cost is below B, where
**B = 9 at /O2 and 2 at /O1** (10ba957c).

**M3. Costs. [C]**

| step | cost |
|---|---|
| ×3, ×5, ×9 (lea) | 6 |
| `(m << k) + m'` with result `m*2^k + 1` | 6 if k is 1, 2 or 4; else k + 2 |
| `(m << k) − 1` form | 5 if k ≤ 4; else k + 2 |
| negative `1 − (m << k)` (sub, then neg) | 6 if k ≤ 4; else k + 3 |
| neg | 1 |
| ×2, ×4, ×8 | 1, 2, 3 |
| trailing shift k of an even constant | k is taken from the budget, not added to the cost |

**M4. Search. [C]** (MsFindSequence 10ba9250; memoised in a table MsInit 10ba9750 preloads with 1, −1, 2, 4, 8,
3, 5, 9.) For odd c: try the factors 9, 5, 3 (c divisible), then c − 1, then c + 1 (for c < 0: factors, then
1 − c, then c + 1), each recursively with the remaining budget. A later candidate wins only if strictly cheaper
(MsTryOption, `jae` at 10ba9229). For even c = m·2^k: needs B − k > 0, solve m with budget B − k.

**M5. Consequences at /O2. [C]**
- 3, 5, 9: `lea eax, [eax+eax*2]` etc. 6, 10, 18, 12, 20, 24, 36, 40, 72: lea then `add eax, eax` once to
  three times (F14); 48, 80, 96: lea then `shl eax, 4/5`.
- 7: `lea eax, [ecx*8]; sub eax, ecx`. 15, 31, 63: `mov eax, ecx; shl eax, k; sub eax, ecx`.
  17, 33, 65: the same with `add`. 127, 129 and beyond: `imul`.
- A product of two leas costs 12: 25, 27, 45, 81 are `imul`. So are 11, 13, 19, 21, 22, 23, 26, 29, 35, 37, 100.
- Sequence (not imul) for 2..260: 2-10, 12, 14-18, 20, 24, 28, 30-34, 36, 40, 48, 56, 60, 62-66, 68, 72, 80,
  96, 120, 128, 144, 160, 192, 256. Negative to −69: −2..−10, −12, −14..−18, −20, −28, −30..−34, −36, −60,
  −64, −68 (−1: `neg`; −3: `lea eax, [ecx*4]; sub eax, ecx; neg eax`).
- Pure powers of two always end as shifts: those that fail the budget stay a multiply and Lower makes them
  `shl` (`shl eax, 10`).

**M6. /O1. [C]** With B = 2 only ×2 (`add eax, eax`) and ×−2 (`neg eax; add eax, eax`) pass; powers of two are
`shl`; everything else, including 3, 5 and 9, is `imul eax, c`.

**M7. The table is per function, and that leaks. [C]** The memo table lives for the whole function and the
even-constant path checks a cached cost against B instead of B − k. So an odd multiplier solved earlier in the
function makes `m << k` succeed where it fails alone: `g = a*7; return b*112;` gives
`lea eax, [ecx*8]; sub eax, ecx; shl eax, 4`, while `b*112` alone, or with the statements swapped, is
`imul eax, 112` (ms_cache.c; also 124 after 31, 126 after 63, 1088 after 17). When a target shows a shift
sequence for a constant that should be imul, look for an earlier multiply by its odd part in the same function
(inlined code counts).

**M8. What the constant is. [C]** Mul2Shift sees the constant after c1xx and the optimizer have combined
things: `arr[i*7]` with int elements is a multiply by 28 (`lea ecx, [eax*8]; sub ecx, eax; mov eax, _arr[ecx*4]`),
a struct index is a multiply by `sizeof` (48: `lea eax, [eax+eax*2]; shl eax, 4`; lea.c a28), and
`a*7 + b*112` is first factored to `(b*16 + a)*7`. The multiplicand is copied into a temp first, so the
decision does not depend on whether it is in memory. The emitted ×2/×4/×8 and adds are then folded into
address modes by AddrModes (§2): `a*5 + b` → `lea eax, [b+a*4]; add eax, a`.

### 3.2 Divide and remainder

**D1. Magic numbers only at /O2. [C]** MsIntConsiderDiv 10baa5c0 tests `_Speed` first. Also required: constant
divisor, non-constant dividend, not 64-bit, |divisor| not a power of two and not 0. Unsigned divisors of the
form 2^32 − 2^k (0xFFFFFFFF, 0xFFFFFFFE) get no magic (`xor edx, edx; or ecx, -1; div ecx`), because the
power-of-two test is made on the value as an int.

**D2. The numbers** are those of Hacker's Delight `magic`/`magicu` (MsGetSignedMagic 10baa000,
MsGetUnsignedMagic 10baa4a0; tables for 3..12 at 10ced6a8 and 10ced710). **[C for 3, 5, 6, 7, 9, 10, 12, 25,
100, 641, 1000]**

**D3. Signed form. [C]** (MsSignedDivide 10ba9ba0.) The dividend is always copied to a register first:

```
mov  ecx, x            ; copy of the dividend (never eax or edx)
mov  eax, M
imul ecx               ; edx = high half
add  edx, ecx          ; only if d > 0 and M < 0   (7: M = 92492493h)
sub  edx, ecx          ; only if d < 0 and M > 0   (-3)
sar  edx, s            ; only if s != 0
mov  eax, edx
shr  eax, 31
add  eax, edx          ; quotient
```

A negative divisor uses its own magic, with no trailing `neg`. `unsigned short` and `unsigned char` dividends
are promoted to int and use this signed form (div.c wud16, bud8).

**D4. Unsigned form. [C]** (MsUnsignedDivide 10baa0d0.) Without the add indicator the multiply takes the
dividend straight from memory: `mov eax, M; mul DWORD PTR x; shr edx, s; mov eax, edx`. With it (7, 14, 19,
21, 0x7fffffff): `mov ecx, x; mov eax, M; mul ecx; mov eax, ecx; sub eax, edx; shr eax, 1; add eax, edx;
shr eax, s−1`.

**D5. Remainder. [C]** GoptCanonicalize 10b4a920 (in GlobalOptimize; test at 10b4b0bd) rewrites `x % c` to
`x − (x / c) * c` when D1 would accept the division and x is a plain variable. The multiply back then follows
3.1 (lea for 3, 5, 6, 9, 10, 12; `imul edx, 25`), ending `mov eax, ecx; sub eax, edx`. When x is an expression
the rewrite is not made and a real `idiv` stays even at /O2: `(a + b) % 10` is `cdq; mov ecx, 10; idiv ecx;
mov eax, edx`, while `(a + b) / 10` uses the magic number (ms_div.c d_sum_s, d_sum_u). `a/10 + a%10` shares the
quotient and becomes `a − 9q` (`lea eax, [eax+eax*8]`; div.c dm10). The sign fix appears as `add edx, eax`
only when the multiply back is an `imul edx, c` (v_int2.cpp r25: `shr eax, 31; add edx, eax; imul edx, 25`); when
it is an lea or shift sequence the fix is the usual `add eax, edx` and the sequence starts from eax (r7:
`add eax, edx; lea edx, [eax*8]; sub edx, eax`; v_int.cpp r10s) **[C]** (corrected in §14).

**D6. Powers of two and the generic form (Lower: LoSpecialDivRem 10c4a270, LoDivide 10c49d50, LoRemainder
10c4a000; both levels). [C]**

| operation | code |
|---|---|
| unsigned `/ 2^k`, `% 2^k` | `shr eax, k`; `and eax, 2^k−1` |
| signed `/ 2` | `cdq; sub eax, edx; sar eax, 1` |
| signed `/ 2^k` | `cdq; and edx, 2^k−1; add eax, edx; sar eax, k` |
| signed `/ −2^k` | the same, then `neg eax` |
| signed `% 2^k` | `and eax, 80000000h+(2^k−1); jns L; dec eax; or eax, −2^k; inc eax; L:` (a branch, at both levels) |
| signed `% −2^k` | `cdq; xor eax, edx; sub eax, edx; and eax, 2^k−1; xor eax, edx; sub eax, edx` |
| other signed | dividend in eax, `cdq`, `idiv` by a register or memory operand |
| other unsigned | `xor edx, edx; div` |

At /O1 a constant divisor is loaded with `push imm8; pop ecx` (F4) or `mov ecx, imm32`, then `idiv ecx`.

### 3.3 Shifts

- Register `shl` by 1 is `add r, r` at both levels; by 2 or 3 it is two or three `add r, r` at /O2 and `shl` at
  /O1; by 4 or more always `shl` (F13, F14). **[C]**
- Shifts in memory are never converted: `shl DWORD PTR [eax], 1` (div.c shlm1). **[C]**
- `x << 2` and `x << 3` leave AddrModes as `lea t, [x*4]`; LoFinalLower turns an index-only lea whose
  destination is the index register back into a shift (F11), so the add chain appears only when the value
  stays in one register. **[C]**
- Variable count: count in ecx, `shl/sar/shr r, cl`. `sar` and `shr` by a constant are never rewritten. **[C]**
- `(a << 5) | (a >> 27)` on unsigned is `rol eax, 5`, with a variable count `rol eax, cl`, at both levels;
  `_rotl`/`_rotr` are inline only with `/Oi` (/O2) and calls at /O1 (div.c rot, rotv; idi.c i6, i7). Which pass
  recognises the pattern: `OptRotatePeep` (§13). **[C probes]**

## 4. Zero and sign extension, 8- and 16-bit operations

Probes: `ext.c`, `ext2.c`, `half.c`; `verify/v_int.cpp` ex_*, cp4b, cp2b, nar, nars, cmpch, cmpc5, cmpm,
`verify/v_int2.cpp` stall, and0, or5, K::ehsz (re-verified, C++ game flags).

**4.1 Extension is always `movzx` / `movsx`. [C]** Loading an 8/16-bit value into a 32-bit result uses
`movzx r32, BYTE/WORD [m]` or `movsx`; the `xor r,r` + `mov r8,[m]` pair is never generated for an extension
(e1-e10, e39-e42). A register source gives `movzx eax, al`, `movsx eax, ax` (h18-h21). `int → __int64` is
`cdq`; `unsigned → __int64` is `xor edx, edx` (e52, e53).

**4.2 A truncating cast and an `&` are different IL and stay different. [C]**

| source | code |
|---|---|
| `(unsigned char)a`, a in memory | `movzx eax, BYTE PTR a` (e7) |
| `a & 0xff`, a in memory | `mov eax, a; and eax, 255` (e11, h13, h15) |
| `(unsigned char)b`, b in a register | `movzx eax, al` (h18) |
| `b & 0xff`, b in a register | `and eax, 255` (h17) |

So `and eax, 0FFh` in the target means the source had a mask, `movzx` means a cast (or a narrower variable).

**4.3 Byte 1 of a value (Half, phase 3). [C]** `(v >> 8) & 0xff`, with v a named variable (signed or unsigned
shift), becomes a byte load of v's second byte: `movzx eax, BYTE PTR _a$[esp-3]` for a stack variable (h1, h4),
`movzx eax, ah` for a register (h16). Shifts by 16 and 24 are not converted for a variable (h2, h3, h5:
`shr/sar eax,16; and eax,255`). Through a pointer every byte position is narrowed, but by c1xx: the IL at
Half's entry already holds `[p+2]:1` (h9-h12, h28; `il --at @Half@4`). With a **signed** `*p`, byte 2 keeps a
redundant mask: `(*(int*)p >> 16) & 0xff` is `movzx eax, BYTE PTR [eax+2]; and eax, 255`, while byte 1 (`>> 8`) and
every unsigned form have no `and` (v_int.cpp ex_pb2 against ex_pb1; h10 is unsigned) **[C]**. `(unsigned char)(*p >> 8)` is not
narrowed by either (h26: `mov ecx,[eax]; sar ecx,8; movzx eax,cl`).
Stores: `*p &= 0xffffff00` is `mov BYTE PTR [eax], 0`, `*p = (*p & ~0xff) | 5` is `mov BYTE PTR [eax], 5`
(h30, h31, x39; c1xx). `*p |= 0xff` stays a dword `or` (h32).

**4.4 8/16-bit arithmetic stays narrow when the result is stored narrow. [C]** `*p = *q + 1` on chars is
`mov cl,[eax]; add cl,1; mov [edx],cl`; shorts use `cx` with the 66h prefix (x7-x11, e18-e21). When an operand
is an `int` already in a register the operation is done on 32 bits and only the store is narrow (x12:
`add eax,1; mov WORD PTR [ecx],ax`); for a byte result from two int variables in memory it is done in 8 bits
(x13, x14: `mov al, BYTE PTR _b$; add al, BYTE PTR _a$`). A narrow value returned or used as `int` is extended
at the load and computed in 32 bits (e35, e54, x16-x20). 16-bit multiply by a constant is `imul si, 3` (x30).

**4.5 Compares of narrow values. [C]** Two chars or shorts compare in their own width, the first loaded into
a byte/word register, the second from memory: `mov cl,[a]; cmp cl,[b]` (e28-e31, x22). A narrow memory operand
against a constant is compared in place: `cmp BYTE PTR [ecx], 5`, `cmp WORD PTR [ecx], 1000` (e32-e34), and so
is a cast of an int in memory: `(unsigned char)a == 5` → `cmp BYTE PTR _a$, 5` (x24-x27). SizeOpt (SzoResizeCmp
10be0bf0, between GrgIdentifyCandidates and AddrModes in the trace) narrows `(x & 0xff) == c` to a byte compare
(h35: `cmp BYTE PTR [ecx], 18`; h41: `cmp al, 18`; c69: `and al,3; cmp al,1`; c68: `cmp BYTE PTR _a$, 0`). Masks
that are not the low byte are not narrowed (h34, h38, h39: `and ecx, 65280; cmp ecx, 4608`).

**4.6 Sizeext: repeated narrow loads into one register (/O2 only). [C]** Sizeext 10c65000 runs after
RegSimpleAssignBody, only when `_Speed` (il_pipeline.md). Per block it counts, for each 8/16-bit register, the
loads `mov r8/r16, mem` into it (`_LdDstRegDefCount`); when a register is loaded more than once in the block
(compare at SizeextTuple 10c650f0: `1 < count`), is not ebp, and the full register is free of other values there
(SizeextRegLiveNoConflict 10c65240), each such load becomes `movzx r32, mem`.
Probe x2 (`p[0]=q[0]; ... p[3]=q[3]`): /O2 `movzx edx, BYTE PTR [eax]; mov [ecx], dl` three times, then
`mov al, [eax+3]` (al is loaded once); /O1 `mov dl, [eax]` each time. x1 (two copies, two different
registers) keeps `mov dl` / `mov al` at /O2. Same for words (x3).

**4.7 Partial-register fix. [C]** In LoFinalLower, SmdFixPartialStalls 10c631f0 looks for a full-register
read that follows a narrow write of the same register. The narrow load is then widened: x21
`movzx eax, BYTE PTR _a$; cmp al, BYTE PTR _b$; sbb eax, eax` (the trace shows `mov al` at LoFinalLower's
entry and `movzx eax` after it), while x23, where the byte is in cl and the `sbb` uses eax, keeps `mov cl`.
It applies at /O1 too (x23 at /O1). This is also why a setcc result is always preceded by `xor r32, r32`
(§5): a zeroing `xor`/`mov r,0` of the full register counts as the fix (InstrFixesPartialStall 10c62cb0) **[R]**.

**4.8 Bit tests on bytes: see §6.4.**

## 5. Bool values, `?:`, and the neg/sbb forms

Probes: `cmp.c`, `idi.c`, `q_ques.c`, `q_misc.c`; `verify/v_int.cpp` q_*, K::eq0, K::lt, `verify/v_int2.cpp` qp, qc1,
sbbv, ucmp, K::ehq (re-verified, C++ game flags); `bool` results behave as `int`.

**5.1 Which diamonds become a "question" (MscBuildQuestion 10b9dfe0 → BuildQuestion 10b9e5c0, end of
phase 3). [C]** A conditional branch qualifies only as a full diamond: the compare directly before the branch;
each arm exactly one assignment of a leaf (constant, variable; no `*p`) to the same destination, or one
`return leaf`. `a < b ? 4 : 6`, `if (a<b) r = 4; else r = 6;` and `return a == b;` qualify.
`r = 6; if (a < b) r = 4;` does **not**: it stays `mov eax, 6; jge L; mov eax, 4` (q_ques.c e9 vs e10).
An arm with a computation (`c + 1`), a load through a pointer, or different destinations stays a branch.

**5.2 Type gate (MscIsQuestionable 10b9e0e0, QuesNoJumpsNeeded 10c5dc60). [C]** Result and compared operands
must be integers of at most 4 bytes; float, double and `__int64` compares stay branches (idi.c i21, i22).

**5.3 Arms. [C]** Both arms constant, or one arm the constant 0 and the other a variable (QuesSccArmsSimple
10c5ebb0, QuesSbbArmsSimple 10c5ec50). `a < b ? c : d` with two variables is always a branch
(`mov eax, c; jl L; mov eax, d`; cmp.c c25), and so are min, max, abs and clamps written with `?:` or `if`
(c26-c30, i2-i5, i8, i10). `abs()` under `/Oi` is the only source of `cdq; xor eax, edx; sub eax, edx` (i1, i11).

**5.4 Cost and choice (QuestionLower 10c5dd00, called from LoSetOpcode in Lower). [C]** Each form gets a cost
from the table at 10cdf098 (pairs are /O2, /O1); a non-byte setcc result adds (2, 2) for the `xor r, r`. The
form is branchless only if min(cost) ≤ **10 at /O2, 12 at /O1** (10c5dcdc); sbb wins ties (10c5dd2b).

*setcc templates* (FindSetccPattern 10c5dd50). T = value when the condition holds, F = otherwise.

| arms | cost | code after `xor r, r; cmp` |
|---|---|---|
| {1, 0} | (2, 3) | `setcc` |
| {0, −1} | (4, 4) | `setcc; dec` |
| {2, 1} | (4, 4) | `setcc; inc` |
| differ by 1 | (4, 6) | `setcc; add r, min` |
| both nonzero, differ by 2, 4 or 8 | (4, 8) | `setcc; lea r, [r*d + min]` |
| both nonzero, other | (8, 10) | `set!cc; dec; and r, T−F; add r, F` |
| one arm 0, other X (constant or variable) | (6, 7; +4 at /O1 for a non-byte result) | `set!cc; dec; and r, X` |

The condition is inverted whenever the 1 has to land on the smaller arm: `a < b ? 3 : 7` is
`setge al; lea eax, [eax*4+3]` (c24). At /O2 `dec`/`inc` print as `sub eax, 1` / `add eax, 1` (F15).

*sbb templates* (FindSbbPattern 10c5e470). Only for unsigned `<`, `>`, `<=`, `>=` and for `==`/`!=`.
Signed relations cannot use sbb. For `==`/`!=` the carry is made with `sub t, b` (omitted when b is 0) and
`neg t`, so the tested value must be in a register (cost (2, 3) more if it has to be loaded). `>` and `<=` swap
the compare operands, which puts a constant into a register (`mov eax, 5; cmp eax, x`; c15); `<=` and `>=` swap
the arms. T = arm taken when CF = 1.

| arms (T, F) | cost | code after the compare |
|---|---|---|
| (−1, 0) | (2, 2) | `sbb r, r` |
| (0, 1) | (4, 3) | `sbb; inc` |
| (1, 0) | (4, 4) | `sbb; neg` |
| (0, −1) | (4, 5) | `sbb; neg; dec` |
| T = F − 1 | (4, 5) | `sbb; add r, F` |
| F = 0 | (4, 5) | `sbb; and r, T` |
| T = F + 1 | (6, 7) | `sbb; neg; add r, F` |
| T = 0, F variable | (6, 7) | `sbb; not; and r, F` |
| other constants | (6, 8) | `sbb; and r, T−F; add r, F` |

**5.5 Worked cases. [C]**

| source | sbb vs setcc | /O2 | /O1 |
|---|---|---|---|
| `a == 0` (int) | | `xor eax,eax; cmp a,eax; sete al` | same |
| `a < b` unsigned | 4 vs 4 | `cmp; sbb eax,eax; neg eax` | same |
| `a < b` signed | — vs 4 | `xor eax,eax; cmp; setl al` | same |
| `a ? -1 : 0` | | `neg eax; sbb eax,eax` | same |
| `a ? 1 : 2` | 6 vs 6 (/O1 8 vs 6) | `neg; sbb; add eax,2` | `xor; cmp; sete al; inc eax` |
| `a == 0 ? 5 : 0` | | `neg; sbb; and eax,-5; add eax,5` | same |
| `a < b ? c : 0` | setcc 8 (/O1 13 > 12) | `setge al; sub eax,1; and eax,c` | branch |
| `a >= 3 && a <= 9` | | `add eax,-3; mov ecx,6; cmp ecx,eax; sbb eax,eax; add eax,1` | same with `push 6; pop ecx`, `inc eax` |
| `a && b`, `a \|\| b` | | branches, `mov eax,1` / `xor eax,eax` arms | same |
| `(a \| b) == 0` | | `or eax,b; neg eax; sbb eax,eax; add eax,1` | `inc eax` |

The zero operand of the compare in the first row is the freshly zeroed result register (§11.2). A range test
`lo <= a && a <= hi` is turned into one unsigned compare of `a − lo` against `hi − lo` before c2's lowering;
as a branch it is `add eax, -lo; cmp eax, hi−lo; ja` (c45).

## 6. Compares and branches

Probes: `cmp.c`, `half.c`, `q_cmp.c`, `q_tr.c`; `verify/v_int.cpp` c_*, b_*, f_*, K::bt* (re-verified, C++ game flags).

**6.1 Operand order follows the source. [C]** Nothing canonicalises `b > a` into `a < b`: `if (a < b)` is
`mov eax, a; cmp eax, b; jge`, `if (b > a)` is `mov eax, b; cmp eax, a; jle` (c50, c51). Exceptions: a constant
on the left is swapped with the condition reversed before Lower (`if (5 < a)` → `cmp a, 5; jle`; c52), and the
sbb forms of §5.4 swap for `>` and `<=`.

**6.2 Which operand is loaded. [C]** With two memory operands the first is loaded and the second stays in
memory (`mov ecx, [eax]; cmp ecx, [edx]`; c57). The first operand is compared in place when the second is a
register or an immediate: `cmp DWORD PTR [eax], 5`, `cmp DWORD PTR _gi, 1000`, `cmp _a$, eax` (c56, c91). The
folding is done in Lower (LoSDSUPeep 10c48fa0) **[R]**.

**6.3 Compare with zero. [C]** In a register: `test r, r` (F19, made in LoFinalLower, so it depends on the
value being in a register after allocation). In memory: `cmp DWORD PTR [m], 0`, with no load, when the value is
not needed afterwards (c47, c55, c90). At /O1, and at /O2 when a zero register exists, the 0 becomes that
register (`xor eax, eax; cmp _a$, eax`; §11.2, §11.3).

**6.4 Bit tests. [C]** `if (x & mask)`: the `and` becomes `test` when its result is otherwise unused. Narrowing
happens in SizeOpt (before Lower) and in F21:

| operand | mask | /O2 | /O1 |
|---|---|---|---|
| memory | within bits 0-7 | `test BYTE PTR [m], imm8` (also 80h) | same |
| memory | within bits 8-15 | `test DWORD PTR [m], imm32` | `test WORD PTR [m], imm16` |
| memory | higher | `test DWORD PTR [m], imm32` | same |
| register | 01h..7Fh | `test al, imm8` | same |
| register | 80h | `test al, al; jns/js` (F20) | same |
| register | FFh | `test al, al; je` | same |
| register | 100h, FF00h | `test eax, imm32` | `test ax, imm16` |
| register | 8000h | `test eax, 8000h` | `test ax, ax; jns` |
| register | ≥ 10000h | `test eax, imm32` | same |

The address is never offset to reach a higher byte. Two tests of the same operand joined by `||` with the same
target merge: `(a & 1) || (a & 2)` → `test BYTE PTR a, 3` (PpTestFold 10c5cc20); the `&&` form does not.
`(a & 0x80000000) != 0` as a value is `shr eax, 31` (c77).

**6.5 Flags reused after arithmetic (PpExtraCompare 10c5c110, in Peeps). [C]** `cmp X, 0` is dropped when the
nearest preceding flag-setting instruction in the block computed X (one copy `mov X, t` in between is allowed;
instructions that do not touch flags are skipped; a label or call stops the search):
- after `and`, `or`, `xor`, `test`: for every condition;
- after `add`, `sub`, `neg`, `inc`, `dec`, and shifts by a constant 1..31: only for `==`/`!=`, and for `< 0` /
  `>= 0`, which become `js` / `jns` (c65: `sub eax, b; jns`). `> 0` and `<= 0` keep the `test`;
- never after `imul`, `mul`, `div`, rotates, `not`, `lea`, a shift by cl, or sbb-style results.

The check runs in Peeps and again in LoFinalLower at each `cmp` (before F19). That matters for `c = a + b`,
which is an `lea` during Peeps (§2): when the lea ends as `add r, other` (F10: the destination is one of the
components) the compare is dropped afterwards (c64: `add eax, ecx; jne`); when it stays an lea into a third
register the `test` stays (q_cmp.c xa1: `lea esi, [ecx+eax]; test esi, esi; jne`). `a += b` with b in memory is
an add from the start (o15: `add esi, _b$; jne`).
`(x & 2^k) == 2^k` uses the `and` flags with the jump inverted.

**6.6 Sign and unsigned idioms. [C]** `a < 0 ? -1 : 0` is setcc-based (`setge al; sub eax, 1`), not
`sar eax, 31` (c37); the source `a >> 31` gives `sar eax, 31`. Unsigned `a < 1` / `a >= 1` are `cmp a, 1; jae/jb`
(not `test`; c74, c75).

## 7. Loads, stores and immediates

Probes: `mem.c`, `cl.c`, `ext.c`, `ad_*.c`; `verify/v_int.cpp` l_*, r_*, s_*, push*, call*, K::rmw, K::pushm,
`verify/v_int2.cpp` K::st2*, st8, ld3, K::ehpush (re-verified, C++ game flags).

**7.1 Folding a load into the operation. [C]** In `x op y`, y is used from memory and x is loaded:
`sub eax, DWORD PTR b`, `and eax, DWORD PTR a`, `imul eax, DWORD PTR a`, `add eax, DWORD PTR [ecx]`,
`test DWORD PTR b, eax`, `idiv DWORD PTR [ecx]` (a14, m21-m25, c63, dvm). When one operand of a commutative
operator is read through a pointer and the other is a variable, the pointer value is the one loaded and the
variable is folded (`*p + a` and `a + *p` both give `mov eax, [eax]; add eax, a`; m19, m20). Exception:
`+` of two variables (A1).

How it is decided **[C]**: Lower (LoOkInstr 10c43b20; no `_Speed` test) treats variables as registers and only
pointer loads as memory. For `dst = s1 op s2`: s2 may stay memory when dst and s1 are expression temps; a
commutative op with a variable first and a pointer load second is swapped, so the load becomes s1 and gets its
own `mov`; otherwise s1 is copied to a temp and s2 is left alone. Of two pointer loads the first in IL order is
loaded (`*p + *q` and `*q + *p` both load `[p]`; ad_2.c f2), a non-commutative op keeps source order. Then
Globregs/Color decide for each variable: one that gets no register becomes the memory operand if the instruction
accepts one there, else it is loaded.

Per instruction (LoLegalTupleDstSrcs 10c423a0) **[C]**:
- `cmp [m], imm` and `cmp [m], reg` are legal; memory with memory loads the first (§6.2);
- `test [m], reg` in either order;
- `imul r, [m]` yes; `imul r, [m], imm` never: the multiplicand is always loaded first
  (`mov eax, [eax]; imul eax, 100`; m29, m30), at both levels;
- `idiv [m]` / `div [m]` keep a memory divisor; a constant divisor goes to a register;
- `op [m], x` (7.2) needs x in a register or immediate: `mov eax, gj; add gi, eax` (m17).

**7.2 Read-modify-write. [C]** Opeq 10c715f0 (opeq_tuple 10c71660, phase 3) turns `t = X op y; X = t` into one
tuple `X = X op y` (trace of m4) when the two X are the identical operand and the computation is directly before
the store (a store through another pointer in between prevents it; ad_1.c e16). Operators: add, sub (left
operand), and, or, xor (either operand, also through a chain of the same operator), shl/shr/sar (left operand).
Not multiply, divide, neg, not, rotate, or `y − X`. At /O1 only, Peeps (10c5bb56, `_Speed == 0`) additionally
merges `mov t, M; op t; mov M, t` for any two-address operator except multiplies, which gives the `neg [m]`,
`not [m]`, `shl [m], 1`, `rol [m], 3` forms:

| source | /O2 | /O1 |
|---|---|---|
| `*p += a`, `*p = a + *p`, `gi += gj` | `add [eax], ecx` | same |
| `*p += 5`, `&= ~4`, `\|= 4`, `^= 4` | `add [eax], 5`, `and [eax], -5`, ... | same |
| `*p -= 1` | `add [eax], -1` | `dec DWORD PTR [eax]` |
| `*p <<= 3`, `>>= 1` | `shl [eax], 3`, `sar [eax], 1` | same |
| `*p = -*p`, `~*p` | load, `neg`/`not`, store | `neg [eax]`, `not [eax]` |
| `*p = *p * 2` | load, `add ecx, ecx`, store | `shl [eax], 1` |
| `*p = a - *p` | load a, `sub ecx, [eax]`, store | same |
| `*p = *p * 3` | load, lea, store | load, `imul`, store |
| byte / word | `add BYTE PTR [eax], cl`, `add WORD PTR [eax], 3`, `or BYTE PTR [eax], 128` | same, with `inc` |

**7.3 `push` and `call` of memory. [C]** At /O2 a pushed argument that is not a register, constant or address
is forced into a register (LoLegalTupleDstSrcs case push, `_Speed` test at 10c42ecc); at /O1 only operands
narrower than 4 bytes are. A pointer load is split by Lower itself; a variable that gets no register is loaded
between the entries of Globregs and Color (passdiff on mem.c m44: `push <- _gi` at Peeps, `mov t <- _gi; push t`
at Color). So /O2 never has `push DWORD PTR [m]` and /O1 has it whenever possible.
Indirect calls: a target read through a pointer is loaded first at /O2 (LoLowerCall, `_Speed` test at 10c41004:
`mov edx, [ecx+4]; call edx`) and called through memory at /O1 (`call DWORD PTR [eax+4]`); a function-pointer
variable is called through memory at both (`call DWORD PTR _gfp`). Tail jumps follow the same rule
(`mov ecx, [eax+4]; jmp ecx` against `jmp DWORD PTR [eax+4]`).

**7.4 Storing constants. [C]** `mov [m], imm` for 8, 16 and 32 bits (`mov WORD PTR [eax], 7`) is always legal.
A constant used several times is an immediate candidate (CmdIdentifyImmedCandidates 10c26030 in Globregs;
regalloc_cost.md) and is kept in a register when that pays. At /O2 the observed rule is: each use counts 1 if
the other operand is a global/stack variable or a memory operand with a symbol or nonzero displacement, 0
otherwise (CmdSubCost 10c237c0); a register is used when the sum is ≥ 2 **[C for the outcomes: five probes
here and eight in v_int.cpp / v_int2.cpp (s_00, s_01, s_12, s_gg, s_1000, K::st2, K::st2b, K::st2c); I for the
mechanism]**. Through `this`, two stores of one constant to members at nonzero offsets therefore already share a
register (`xor eax, eax; mov [ecx+8], eax; mov [ecx+12], eax`; K::st2). Byte stores alone use a byte register:
three `p[k] = 0` on chars give `xor cl, cl; mov BYTE PTR [eax], cl; ...` (v_int2.cpp st8) **[C]**. So it is not "three stores": `p[1] = 5; p[2] = 5;` → `mov ecx, 5` (1+1), `p[0] = 5; p[1] = 5;` → two
immediates (0+1), two globals → register (cl.c z10). A pushed constant counts 0, so /O2 never shares a register
for pushes. Mixed 8/16/32-bit stores share one register (`mov BYTE PTR _gb, al`; z12). /O1 uses byte sizes.

Examples (mem.c m9-m13, m53-m56):

| stores of the same constant | /O2 | /O1 |
|---|---|---|
| 0, twice | `mov [eax], 0; mov [eax+4], 0` | `and [eax], 0; and [eax+4], 0` |
| 0, three times | `xor ecx, ecx; mov [eax], ecx` ×3 | same |
| 5, three times | `mov ecx, 5` + 3 stores | `push 5; pop ecx` + 3 stores |
| 1000, twice | two immediates | `mov ecx, 1000` + 2 stores |
| −1, three times | `or ecx, -1` + 3 stores | `or [eax], -1` ×3 |
| 4 equal arguments | `push 0` ×4 | `xor eax, eax; push eax` ×4 |

A register that already holds the constant is used regardless of these counts (§11.2, §11.3).

**7.5 Register constants. [C]** 0 → `xor r, r`; −1 → `or r, -1`; others `mov r, imm32` at /O2; /O1 forms in F3,
F4. Where the flags of a preceding instruction are still needed, `mov r, 0` stays (F1's ccPending test).

**7.6 Memory is re-read after a store through a pointer or a call. [C]** `*p = gi; *q = gi; return gi;` loads
gi three times (cl.c z14); `a = *p; ext(); return a + *p;` keeps a in a callee-saved register and reloads `*p`
(m94). Copies memory to memory go through a scratch register of the value's own width
(`mov cl, [eax]; mov [edx], cl`).

## 8. Calls and returns

Probes: `call.cpp`, `call2.c`, `pro.c`, `q_tail.c`, `fl_b.c`; `verify/v_int.cpp` k_*, p_*, K::tail*, K::ehtail,
`verify/v_int2.cpp` cl1-cl3, fc, td, tf, tll, taddr, K::fwd*, K::gsbuf, `verify/v_t.cpp` (re-verified, C++ game flags).

**8.1 Arguments. [C]** Arguments are evaluated and pushed right to left, each pushed as soon as it is computed;
a nested call's result is pushed right after that call's own cleanup (k28: `call ext; add esp, 4; push eax;
push esi; call ext; add esp, 4; push eax; call ext2; add esp, 8`).

| argument | /O2 | /O1 |
|---|---|---|
| constant, address | `push imm8/imm32`, `push OFFSET x` | same (4 equal constants: one register, `xor eax,eax; push eax` ×4; regalloc_cost.md) |
| value in memory | `mov eax, [m]; push eax` | `push DWORD PTR [m]` |
| value in a register | `push r` | same |
| char / short | `movsx/movzx eax, ...; push eax` | same |
| `__int64` | high half pushed first; from an int: `cdq; push edx; push eax` | same |
| float / double | `push ecx` / `sub esp, 8`, then `fstp [esp]` (/O1: `push ecx; push ecx`) | |
| struct by value | `sub esp, N` + copy (§10.5) | |

At /O2 the loads and pushes of several memory arguments interleave under the scheduler, three registers in
rotation (q1: `mov ecx,[eax+28]; mov edx,[eax+24]; push ecx; mov ecx,[eax+20]; push edx; ...`; sched.md).
The `mov`-then-`push` choice is made at Lower (§7.3).

**8.2 Conventions. [C]** cdecl: caller cleans (8.3). stdcall and thiscall callee: no cleanup, `ret N` in the
callee. fastcall: stack arguments pushed first, then `mov edx, ..; mov ecx, ..` (k8-k10). Member call: `this` in
ecx, vtable in eax; the target is loaded at /O2 (`mov edx, [eax]; push 1; call edx`) and called through memory
at /O1 (`call DWORD PTR [eax]`; k16). A function pointer that is a named variable is called through memory at
both levels (`call DWORD PTR _fp$[esp]`; k18).

**8.3 Cleanup. [C]** After a cdecl call: `add esp, N`, merged forward into the last call of a straight-line run
and into the frame release (LoFinalStackOpt, §11.1). /O2 uses `pop ecx` only for a 4-byte cleanup directly
followed by the return (F24). /O1 uses `pop ecx` for 4 and two for 8 after any call (F26), and reuses the top
slot with `mov [esp], imm` (§11.1).

**8.4 Tail calls (PpCallRetToJmp 10c5cef0, called from FgDoLateFlowOptsOnce). [C]** `call f; ret` becomes
`jmp f` when all of these hold:
- optimising, the function has no C++ EH state, not instrumented for PGO;
- only labels between the call and the return, and the call's result is returned unchanged (or void);
- the callee's stack-argument bytes equal the caller's, or the callee takes none and the caller is cdecl
  (`return ext0();` from any cdecl function; k33). A callee with more, or with fewer but nonzero, argument
  bytes is called normally (q_tail.c k1, k26);
- a callee-pop caller needs a callee-pop callee with the same byte count; cdecl → stdcall: no (k6);
- when the callee takes stack arguments: every incoming parameter is at most 4 bytes and is an integer or
  pointer. A `double`, `__int64` or `float` parameter forwarded unchanged is still a normal call (v_t.cpp td2,
  tl2, tf2; the float is re-pushed through `fld`/`fstp`). The test does **not** apply to a callee without stack
  arguments: `int f(double d) { return ext0(); }` and the `__int64` and `float` versions are `jmp ext0`
  (v_int2.cpp td, tll, tf) **[C]** (corrected in §14);
- no `/GS` cookie in the function: with a local buffer the call is followed by the cookie check and stays a call
  (v_int2.cpp K::gsbuf) **[C]**;
- the call does not take the address of, or otherwise reference, the caller's frame;
- each outgoing argument is either the identical incoming slot (no code), or, **at /O2 only** (`_Speed` test at
  10c5d2c8), a register or constant, in which case the push becomes a store into the incoming slot:
  `mov DWORD PTR _a$[esp-4], 1; jmp _ext` (c47, k23, k24). An argument pushed from another memory location is
  refused, so `return ext2(b, a);` is a normal call (k4).

At /O1 only the "identical slots or no stack arguments" case tail-calls (k1, k3, k33). Indirect and fastcall
register-only tail calls work (`mov edx, b; mov ecx, a; jmp fc2`; k8). At /O2 a value held in a register is
stored back to its slot even when unchanged (k23: `mov _a$, eax; jmp`).
Member functions **[C]** (v_int2.cpp, v_int.cpp): thiscall → thiscall with the same argument bytes is a tail call
(`int K::fwd(int a) { return other(a); }` is `jmp K::other`; a virtual one is `mov eax, [ecx]; mov eax, [eax];
jmp eax`; `return o->vf(1);` is `mov ecx, o; mov eax, [ecx]; mov _o$, 1; mov edx, [eax]; jmp edx`); thiscall with
stack arguments → cdecl is not (K::tail: `call ext; add esp, 4; ret 4`), nor is a callee with fewer argument bytes
(K::fwd2).

**8.5 Frame allocation and epilogue. [C]** (Layout is frame.md's subject; the instruction forms are:)

| item | /O2 | /O1 |
|---|---|---|
| 4 bytes of locals | `push ecx` (F22) | `push ecx` |
| 8 bytes | `sub esp, 8` | `push ecx; push ecx` (F23) |
| ≥ 12 bytes | `sub esp, N` | `sub esp, N` |
| ≥ 4096 bytes | `mov eax, N; call __chkstk` | same |
| release without frame pointer | `add esp, N`, merged with a preceding argument cleanup; `pop ecx` if 4 bytes right before `ret` | `pop ecx` ×1-2 or `add esp, N` |
| release with ebp | `mov esp, ebp; pop ebp` | `leave` |
| return | `ret`, `ret N` for stdcall/thiscall/fastcall with stack arguments | same |

Callee-saved registers are popped in reverse push order before the frame release (`pop edi; pop esi; pop ebx;
add esp, 8`). `_alloca` forces an ebp frame and `lea esp, [ebp-8]` before the pops (p11). The scheduler may move
an independent `lea` above the `sub esp` (p13: `lea eax, _a$[esp]; sub esp, 8`).

## 9. Switch statements

The method is chosen when the IL is read (SwGenSwitch 10bed250, called from RdrGetTuples at 10bcea67), so it
sees the source's case list before any optimisation: two cases with identical bodies written separately are
two targets (v_sw.cpp `same`, t3n9 against v_sw2.cpp g3n9); only `case a: case b:` on one body share a target. Compare chains and range checks are ordinary
IL from then on. A jump table stays an OPSWITCH tuple until FgLowerSwitches (phase 5i) emits the tables
(SwMdLowerJumpTable 10c63cf0). Probes: `sw.c`, and the boundary probes `sw_b.c`, `sw_c.c`, `sw_d.c`
(`uv run python sw_show.py out/<file>.fmt [func..]` prints them with table runs collapsed).
`verify/v_sw.cpp` and `verify/v_sw2.cpp` (62 functions: free, member, enum operand, EH frame) (re-verified, C++ game flags): every
/O2 rule of 9.1-9.7 held; the /O1 rules (9.2 step 4, the /O1 pivot, the /O1 column) are (not re-verified).

**9.1 Preparation (SwTidyCases 10bedb20, SwNumCases 10bedf20). [C]** Cases are sorted ascending by value;
source order does not matter. Cases that jump to the default label, or whose body is `__assume(0)`, are
dropped. Adjacent values with the same target merge into one range node [lo,hi]. Then:
N = nodes; ncmp = 1 per single node, 2 per range node; R = last hi − first lo; T = distinct case targets
(default not counted). `_NoDefault` is set when the default starts with `__assume(0)` (SwNoDefault 10beeee0).

**9.2 Method (SwSelectType 10beda70; `_Switch_Parameters` 10cd97c0, speed row {4,4,3,255}, size row {8,4,3,0}). [C]**

1. `ncmp < 4` at /O2, `ncmp < 8` at /O1 (compare at 10beda80): **linear chain** (9.5). `/Od` uses the speed row.
2. Operand wider than 4 bytes: split (9.4) **[R]**.
3. /O2: **table** iff `R < 255` or `R <= 4*ncmp`; otherwise **split**.
4. /O1: table iff LinearSize > JumpTableSize, otherwise split, with
   LinearSize (SwMdLinearSearchSize 10c63bc0) = sum over compared bounds of 6 (value fits a signed byte) or 9,
   and JumpTableSize (SwMdJumpTableSize 10c63c30) = min(4R, R + 4T + 11) + 11 (the second term only if T < 256).

| probe | cases | /O2 | /O1 |
|---|---|---|---|
| sw_b d3 / d4 | 0..2 / 0..3 | chain / table | chain / chain |
| sw_b d7 / d8 | 0..6 / 0..7 | table | chain / table |
| sw_b r254 / r255 | 0,1,2,254 / 0,1,2,255 | table / split | |
| sw_c w64a / w64b | 64 nodes, R = 256 / 257 | table / split | |
| sw_d p9 / p10 | 8 small cases, R = 9 / 10 | | table (47 < 48) / split |
| sw sp4 | 10,20,30,40 | two-level table | |
| sw sp4w | 100, 2000, 30000, 400000 | split | |

**9.3 Direct or two-level table (SwMdTwoLevelJumpTable 10c63c70, compare at 10c63ce4). [C]** With n = R + 1:
two-level iff `T < 256` and `3n > 4T + 12` at /O2 (`4T + 11` at /O1). Two-level form:
`movzx eax, BYTE PTR idx[eax]; jmp DWORD PTR tbl[eax*4]`; the DD table has T entries plus one for the default
when there are holes; the DB index table follows it.
Checks: sw `s4g` (1,3,5,7: n=7, T=4, 21 ≤ 28) direct; `sp4` (n=31, T=4) two-level; `sd10` (n=15, T=10, 45 ≤ 52)
direct; `t2` (n=9, T=3, 27 > 24) two-level; sw_c `u9t4` direct, `u10t4` two-level; `u8t3` direct at /O2 and
two-level at /O1.

**9.4 Split = binary search step (SwChooseMethod 10bed470, SwBinarySearch 10bee590). [C]** Pivot = sorted node
k. Code: `cmp x, pivot.hi; jg RIGHT` (`ja` if unsigned), then `je target` on the same flags (`cmp x, pivot.lo;
jge target` for a range pivot). The nodes left and right of the pivot each go through 9.2 again, so leaves are
chains of at most 3 compares at /O2 (7 at /O1) or tables.
- /O2 pivot (SwJumpTableSplit 10beea00): cluster the sorted nodes greedily from the left; a cluster with first
  value c0 and count c ends before value v (previous value p) when `v − p > 3` and v is not the last node and
  `v − p <= next − v`, or when `v − c0 + 1 > 3c`. Then a cluster absorbs following clusters lying within 255 of
  its start if the combined count is ≥ 10. Then: one cluster, or largest cluster < 4 nodes → k = N/2; else if
  the first cluster has ≥ 4 nodes → k = its count (the pivot is the node right after the dense run); else
  k = sum of leading clusters while the sum stays ≤ N/2.
  Checks: sw `s4w` (1,2,3,1000): largest cluster 3 → k = 2, pivot 3 (`cmp eax,3; jg; je`); `s5w`
  (1,2,3,4,1000): k = 4, pivot 1000, then `add eax,-1; cmp eax,3; ja; jmp tbl`; sw_b c16, c44, cgap2.
- /O1 pivot (SwSplit 10beed10): r = length of the initial run under the same break tests; pivot = node r if
  `8 < r < N`, else N/2.

**9.5 Linear chain (SwLinearSearch 10bee000). [C]** Always in ascending value order (which, after the DFS
reorder, is why layout.md finds compare-chain case bodies laid out by descending value).
- **`sub` form** (SwMdDoCmpAsSub 10c64a30): `sub reg, v − prev; je` per case, including `sub eax, 0` for a first
  case 0. Used when optimising, operand ≤ 4 bytes, no range nodes, first value ≥ 0, and score > 0, where each
  case adds 1 if its delta (prev starts at 0) is 1 or 2, and 1 if its value does not fit a signed byte but its
  delta does. Checks: `s2` (0,1), `s3` (0,1,2), `s3w` (1,2,1000: `sub 1; sub 1; sub 998`), sw_b q100
  (100,200,300), q130.
- **`cmp` form** otherwise: `cmp x, v; je` (`test x,x; je` for 0). Checks: `sp3` (10,20,30), `s2z` (0,7),
  sw_b qneg (−1,0,1).
- Range node: lower bound first, `cmp x, lo; jl default` (`test x, x; jl` for lo = 0), except lo = 1, which is
  `test x, x; jle default`; then `cmp x, hi; jle target`; unsigned operands use `jb`/`jbe`. A switch that is one
  range only ends `cmp x, hi; jg default`. (v_sw2.cpp rng `cmp eax, 5; jl`, rng0, rng1, rngu, r1; wording
  corrected in §14. Other lo values and a range that is not the first node are (not re-verified).)
- With `_NoDefault` the last node is not compared.
- Byte compares (SmdByteSwitch 10c63600): only in the `cmp` form, when the operand is an int extension of a
  1-byte value and all case values fit: `cmp al, imm` / `cmp BYTE PTR m, imm`. Never for 16-bit operands.

**9.6 Table prologue (SwJumpTable 10bee750). [C]** `add eax, -lo` when the first value is not 0 (`add eax, 2`
for lo = −2; never `sub`), `cmp eax, R; ja default`, `jmp DWORD PTR tbl[eax*4]`. The check is always unsigned
and on the 32-bit register; a `char` operand is extended first (`movsx eax, BYTE PTR _a$`; sw `sc`, `suc`).
The range check is omitted only under `_NoDefault` (10bee7fa); holes are then `DD 0`.

**9.7 Table placement. [C]** After the function's last instruction, per switch in source order: `npad` to a
4-byte boundary, the DD table, the DB index table right after it without padding. Also at /O1. In a function
with an EH frame the tables still follow the body in `_TEXT`, before the unwind funclets, which are in their own
`text$x` section (v_sw.cpp ehsw, ehtl; K::two has two switches, tables in source order).

## 10. Struct copies, memcpy, memset

Probes: `mb_cp.c` (sizes), `mb_big.c`, `mb_kind.c` (types, kinds of operand), `mb_misc.c` (arguments, loops,
`= {0}`), `mb_set.c`, `mb_mc.c`, `mb_oi.c` (/O1 with `#pragma intrinsic`), `mb_str.c`, `mb_sse.c`;
`verify/v_cp.cpp`, `v_cp2.cpp`, `v_cp3.cpp`, `v_set.cpp`, `v_fill.cpp` (re-verified, C++ game flags). This replaces
the "≤ 24 unrolled, ≥ 28 rep" numbers of frontend.md, which hold only for multiples of 4.

Let `moves(n) = n/4 + ((n>>1)&1) + (n&1)`: the dwords, plus one word, plus one byte.

**10.1 What arrives. [C]** A struct assignment, and `memcpy(p, q, constant ≤ 4095)` under `/Oi`, reach c2 as
one OPMBASSIGN tuple of type `0x6000 | size` (`il --at @Lower@4`: `OPMBASSIGN [_p]:601c <- [_q]:601c`); c1xx does
not emit per-field moves. Lower expands it (LoSetOpcode → CgiStructOp 10c14920 → struct_mov 10c14950 →
do_blkmov 10c14330 → blkmovbody 10c121b0), so at Peeps the IL already holds the final form. A copy of 4096 bytes
or more arrives as an intrinsic and is a `call _memcpy` at both levels (mb_big.c cp4095 / cp4096). memset is
never a block tuple; it stays an intrinsic until Lower.

**10.1a Which C++ copies are block moves. [C]** (v_cp.cpp, v_cp2.cpp, v_cp3.cpp.) Only a copy that c1xx treats as
trivial is one OPMBASSIGN: a C struct, a class with user constructors but no user copy operations (C6, C7), a
derived class without virtual functions (Der: base and derived members in one 28-byte move). When the implicit
copy constructor or `operator=` is not trivial (the class has a vtable, or a member or base with a user copy
operation), c1xx copies **member by member**, and 10.2-10.3 apply to each member separately:
- every scalar member is its own load/store pair, with no limit: ten `int` members are ten pairs and never
  `rep movsd` (VS10); in `operator=`, `float` members go through `fld`/`fstp` (VFs);
- a POD struct member is one block move with its own threshold (a 28-byte member: `mov ecx, 7; rep movsd`; a
  12-byte member: three pairs; VP28b, VF);
- an array member is a block move in the **copy constructor** (`int a[7]`: `mov ecx, 7; rep movsd`; VA7, VA20,
  also for `float m[16]`), but an element loop in **`operator=`**: up to 10 elements the loop is unrolled into
  pairs (`int a[7]`, `a[10]`: 7 and 10 pairs, where a block move would be `rep movsd`), from 11 elements it is a
  real loop `mov esi, [ecx+eax]; mov [eax], esi; add eax, 4; sub edx, 1; jne` (VA11, VA12, VA20; `char a[40]`
  is a byte loop; `float m[16]` is an `fld`/`fstp` loop);
- the vptr is not copied by `operator=`; the copy constructor stores it (`mov [eax], OFFSET vftable`).
So more than six consecutive dword pairs, or pairs next to a `rep movsd`, mean a member-wise copy of a
non-trivially-copyable class (Fallout 3 0x0083b376..0x0083b3b9: scalar pairs, then `lea esi, [ebx+0DCh];
lea edi, [ebp+0DCh]; mov ecx, 7; rep movsd`, then scalar pairs again).

**10.2 Sizes 1, 2, 4, 8 are scalar moves. [C]** LoSetOpcode 10c46ce8..10c46d02 turns types 0x6001, 0x6002,
0x6004, 0x6008 into an ordinary assignment. Size 8 is a 64-bit integer assignment, i.e. two load/store pairs,
at /O1 too (cp8).

**10.3 /O2 copy: unrolled iff `moves(n) <= 6`. [C]** (blkmovbody 10c123ca..10c123d5.)
- Unrolled: n = 3, 5..7, 9..22, 24. One load/store pair per unit, ascending offsets, dwords then the word then
  the byte: `mov edx,[eax+k]; mov [ecx+k],edx`. Through pointers the pairs stay paired and the last
  pair goes through the source pointer's register (cp24: `mov eax,[eax+20]; mov [ecx+20],eax`); with direct
  addresses (globals, locals) the scheduler runs up to three loads ahead through eax/ecx/edx (mb_kind.c gg24).
  Two conditions found on re-test (v_cp.cpp, v_cp2.cpp) **[C]**: (a) the temporary is whichever of eax/ecx/edx
  is free, so with both pointers in scratch registers every pair uses the same one (edx above), but with a
  pointer in a callee-saved register the pairs rotate (`this` in esi: ecx, edx, ecx, ...; both in esi/edi: eax,
  ecx, edx, ...); (b) "paired" holds only for pointer to pointer: when the destination is the hidden return
  pointer or `esp` (struct return, by-value argument) the scheduler pulls the last load ahead
  (K::get24: `mov edx,[ecx+20]; mov ecx,[ecx+24]; mov [eax+16],edx; mov [eax+20],ecx`; arg12, arg24), and a
  following `push` can land between a load and its store, also in EH functions (K::eh24).
- Otherwise (n = 23 and every n ≥ 25): `mov ecx, n/4; rep movsd`, then `movsw` if n&2, then `movsb` if n&1.

| n | 22 | 23 | 24 | 25 | 28 |
|---|---|---|---|---|---|
| /O2 | 5 dword pairs + word pair | `mov ecx,5; rep movsd; movsw; movsb` | 6 dword pairs | `mov ecx,6; rep movsd; movsb` | `mov ecx,7; rep movsd` |

**10.4 /O1 copy: string instructions; unrolled iff `n/4 <= 4`. [C]** (not re-verified) (10c123e8..10c123f5; tails not counted.)
n = 3, 5..7, 9..19: `movsd` × n/4, `movsw`, `movsb` with esi/edi loaded. n ≥ 20: `push N; pop ecx; rep movsd`
plus tails (`mov ecx, N` once N > 127). cp19 = `movsd ×4; movsw; movsb`; cp20 = `push 5; pop ecx; rep movsd`.

**10.5 What does not matter. [C]** Field types, `#pragma pack`, the kind of source and destination, loops and
other live values do not move any threshold (the tests read n, a memset flag and `_Speed` only). Only the
address setup differs: globals `mov esi, OFFSET x`, locals `lea`; a by-value argument is `sub esp, align4(n)`
then the same copy into `[esp]` (`mov eax, esp` + pairs when unrolled, `mov edi, esp` otherwise; LoMbArg
10c4a520), an 8-byte struct argument is two pushes; a struct return copies through the hidden pointer.
Re-tested in C++ for n = 1..33, 36, 40, 48, 64 and for `float`/`double`/mixed/packed members, base classes,
member functions (`this` as source or destination), globals, locals under a `/GS` cookie, loops around the copy
and EH frames (v_cp.cpp, v_cp2.cpp K::eh22..eh28): the thresholds did not move. In all of Fallout 3's `.text`
an adjacent `mov ecx, N; rep movsd` has N ≥ 7 (130 sites: N = 7, 8, 9, 12, ...; `verify/f3scan.py stats`).
The one thing that does matter is whether the copy is a block move at all (10.1a).

**10.6 esi, edi, ecx. [C]** The expander does not save registers. It creates temporaries bound to the physical
registers (at `@Peeps@4`: `esi=t131 <- _q; edi=t132 <- _p; ecx=t130 <- 7; rep.movs`), and the `push esi; push edi`
are the ordinary prologue saves. So a function needs esi and edi iff it contains a copy with `moves(n) > 6` at
/O2, or any copy of a size other than 1, 2, 4, 8 at /O1; /O1 inline memset needs edi only; /O2 unrolled copies
and /O2 memset need only scratch registers. Variables live across the copy are coloured around esi/edi or
spilled (mb_misc.c lv28).

**10.7 memcpy. [C]** At /O2 (`/Oi`), `memcpy(p, q, constant)` is exactly the struct copy of that size. With a
variable length it is always a call (`jmp _memcpy` in tail position); there is no inline `rep movsd` +
`rep movsb` from memcpy (that pair comes from inline `strcat`, 10.9). memmove is always a call. At /O1 without `#pragma intrinsic(memcpy)` every memcpy is a call, even
for n = 1; with the pragma, 10.2 and 10.4 apply.

**10.8 memset and zero initialisation. [C]** `memset(p, c, constant n)` in intrinsic form is a call iff
`moves(n) > 10` (generateCall 10c0bb00, compare at 10c0bc0b, at both levels); variable n is always a call.
- /O2 inline: the fill value in one register, then `mov [p+k], reg` ascending, dwords/word/byte. A `memset`
  never becomes `rep stosd` at /O2. Largest inline sizes: 40, 38, 37; 39 and ≥ 41 call (mb_set.c z39, z40, z41;
  v_set.cpp z36..z64, also in a member function and an EH frame: K::m40, K::m41, K::e40, K::e41).
  **`rep stosd` does appear in /O2 code, but from fill loops, not from memset** (corrected in §14; opt.md rule
  11): `for (i = 0; i < n; i++) p[i] = 0;` on ints is `xor eax, eax; rep stosd` with the count in ecx, on shorts
  `or eax, -1; shr ecx, 1; rep stosd; adc ecx, ecx; rep stosw`, on chars `call _memset`; a copy loop stays a loop
  (v_fill.cpp f1, f2, f4, f3). Fallout 3 has both forms (0x0050c33d, 0x0084d91c). `__stosd`/`__stosw`/`__movsb`
  from `<intrin.h>` give a bare `rep stosd`/`rep stosw`/`rep movsb` (f7, f8, f11). **[C]**
- /O1 inline (needs the pragma, or the c1-generated initialiser below): `n/4 <= 5` → `stosd` × k, `stosw`,
  `stosb`; otherwise `push N; pop ecx; xor eax, eax; rep stosd`; still a call above 10 moves.
- Fill value: 0 → `xor eax, eax` (a lone 1/2/4-byte store uses an immediate); constant c →
  `mov eax, c*01010101h`; variable c → `mov ah, al` for n ≤ 3, else
  `mov cl,al; mov ch,cl; mov eax,ecx; shl eax,16; mov ax,cx`.
- `T s = {0};` and `char s[N] = "";`: c1xx emits "first element = 0" plus a block set over the remaining
  `n − sizeof(element)` bytes, which is inline-capable even at /O1 without `/Oi`. So `int s[11] = {0}` is 11
  stores (remainder 40) and `int s[12] = {0}` is `mov [s], 0` + `call _memset(s+4, 0, 44)` (mb_misc.c zi44, zi48;
  v_set.cpp zi10..zi13, zs40..zs48). For `char s[N] = ""` the remainder is N − 1: `char s[40] = ""` is
  `mov BYTE PTR [s], 0` + `call _memset(s+1, 0, 39)`, `char s[41] = ""` is eleven inline stores (remainder 40),
  `char s[42] = ""` calls again (v_set.cpp zc40, zc41, zc42).

**10.9 Others. [C probes only]** `/arch:SSE2` at /O2 changes the unrolled copy to 8-byte `movq xmm0` units
(16-byte `movdqa` when both sides are alignable locals; CgiBlkMovUseXmm 10c11ed0); the default arch never uses
XMM. String intrinsics exist only at /O2: strlen and strcpy are byte loops, `strcpy(d, "lit")` is a block copy of
len+1 bytes, `strcmp(a, "lit")` is `mov ecx, len+1; repe cmpsb`, memcmp is a generic dword loop
(the string forms re-verified in C++: v_set.cpp sl, sc, scl, scm, scm2, mcm; the `/arch:SSE2` part is
(not re-verified)). Inline `strcat(d, s)` is a strlen loop on s, a scan loop on d, then
`mov ecx, eax; shr ecx, 2; mov esi, edx; rep movsd; mov ecx, eax; and ecx, 3; rep movsb` (v_set.cpp sca): this is
the only source found for the `shr ecx, 2; rep movsd; and ecx, 3; rep movsb` pair in /O2 code **[C]**.

## 11. Peepholes after allocation

Three passes rewrite the allocated code. Their inputs are physical registers, so their effects depend on
allocation and, for 11.3, on the instruction order before scheduling.

### 11.1 LoFinalLower 10c28970 (phase 5e)

Probes: `fl_a.c`, `fl_b.c`, `fl_c.c` (function names below), plus `mem.c`, `pro.c`. Re-verified, C++ game
flags, in `verify/v_int.cpp`, `v_int2.cpp`, `v_f15.cpp`: F1, F2, F9-F15, F19-F22, F24, the LoFinalStackOpt merges
(last call of a run, stop at a stdcall call, the 64 cap, merge with the frame release) and "no cmov" of F27.
(not re-verified): every /O1-only rule (F3, F4, F6, F16, F23, F25, F26, the `mov [esp], imm` reuse), F5, F7, F8,
F17, F18 and the `/arch:SSE` half of F27. `_Speed` is read at
10c28ba4, 10c28dcd, 10c28ee6, 10c290fb, 10c29194, 10c294ca, 10c29534, 10c29695, 10c29b49. "G" = only with
global optimisation on.

**Order.** First, over the whole function: LoFinalStackOpt (G), LoShortenLogicalInstrSize (G),
SmdFixPartialStalls (G, §4.7). Then one forward walk; per instruction: address-mode fixes, then one case of a
switch on the opcode, then LoFinalNoopInst. The walk tracks **ccPending**: nonzero when the instruction's own
flags are consumed later. Most rewrites that change flags are skipped when ccPending is set.

*Constants and moves (opcode 1)*

| # | rule | condition | address | probe |
|---|---|---|---|---|
| F1 | `mov r, 0` → `xor r, r` | register dst, ccPending = 0, both levels | 10c28e81 | m59, c_m1 |
| F2 | `mov r, -1` → `or r, -1` | same, both levels | 10c28ed3 | m61 |
| F3 | `mov r32, 1` → `xor r, r; inc r` | /O1 only | 10c28ee6 | m60 |
| F4 | `mov r32, imm8` → `push imm8; pop r` | /O1 only, signed imm8 other than 0, −1, 1; 32-bit only (16-bit stays `mov ax, 5`); no ccPending test | 10c290fb | m62, m63; 128 stays `mov` (m64) |
| F5 | `mov r, r` deleted | same register | 10c29048 | trace |
| F6 | `mov [m], 0` → `and [m], 0`; `mov [m], -1` → `or [m], -1` | /O1 only, not volatile, ccPending = 0, operand size > 1 (bytes stay `mov BYTE PTR [m], 0`) | 10c29194 | m6, m7, m77, m79 vs m76, m78 |
| F7 | `op X, Y; mov Y, X` → `op Y, X` | previous instruction is a commutative reg-reg op and X a single-def temp **[R]** | 10c28f7c | |

At /O2 a 32-bit constant is always `mov r, imm32` except 0 and −1. **[C]**

*lea (opcode 0x12, no symbol)*

| # | rule | condition | address | probe |
|---|---|---|---|---|
| F8 | `lea r, [r2]` → `mov r, r2`, or deleted | one register, no displacement **[R]** | LoFinalLEAtoMOV 10c2a6e0 | |
| F9 | `lea r, [r+d]` → `add r, d` | base = dst, ccPending = 0. The signed displacement is kept: `a − 5` is `add eax, -5`, never `sub`. /O1: d = 1 → `inc`, d = −1 → `dec` | 10c28b90 | a7, a15, m68-m72 |
| F10 | `lea r, [a+b]` → `add r, other` | dst is a or b, scale 1, no displacement | 10c28cb3 | a1, m18 |
| F11 | `lea r, [r*4]` / `[r*8]` → `shl r, 2/3` (then F13) | no base, index = dst. Index ≠ dst: /O2 keeps the lea (`lea eax, [ecx*8]`, m7), /O1 gives `mov r, i; shl r, n` | 10c28dc8 | fl_a l_4 |
| F12 | `[r*2]` → `[r+r]` | scale 2, no symbol (LoRewriteOpscale 10c2af30) | | a6: `lea eax, [eax+eax+7]` |

A lea with three components is never split here (a4: `lea eax, [ecx+eax*4+12]` at both levels). **[C]**

*Shifts, inc/dec, add/sub*

| # | rule | condition | address | probe |
|---|---|---|---|---|
| F13 | `shl r, 1` → `add r, r` | both levels (G) | | a5, a33 |
| F14 | `shl r, n` → n × `add r, r` | /O2 only, n ≤ 3 (LoFinalLeftShiftToAdd 10c286c0, compare 10c286dd) | | a11, a34 vs a35 (`shl eax, 4`) |
| F15 | `inc x` → `add x, 1`, `dec x` → `sub x, 1` | /O2, 32-bit, register or memory. Lowering emits inc/dec and /O2 undoes it. The ccPending condition read in the code was not reproduced: with the flags consumed by `je`/`jne`/`js`/`jns` the result is still add/sub (`sub eax, 1; jns`, `add eax, 1; jns`, `sub edx, 1; jne` in a loop, `add [eax], -1; jne`), and no probe produced an inc/dec at /O2 (corrected in §14) | LoFinalIncDecOpt 10c2ac56 | m1, m16; c67 (`sub eax, 1; jne`); verify/v_f15.cpp d1-d6 |
| F16 | `add r, ±1` → `inc/dec`; `add r, ±2` → two `inc/dec` | /O1, register, 32-bit; a value that arrives as `lea [r+2]` takes F9 instead and stays `add r, 2` | LoFinalAddToIncOpt 10c2a9d0, 10c294ca | fl_b b_add2r vs fl_a a_add2 |
| F17 | two consecutive add/sub/inc/dec of constants on one register fold into one | 32-bit, ccPending = 0 **[R]** | LoFinalIncDecOpt 10c2aa60 | |
| F18 | `add/or/sub x, 0`, `and x, -1` deleted; `and r, 0` → `xor r, r` | ccPending = 0 **[R]** | LoFinalNoopInst 10c2b110, 10c2b190 | |

Note the asymmetry at /O2: `a + 1` is `add eax, 1`; `a − 1` is `add eax, -1` when it came from an lea (a value
computed into a register, m71) and `sub eax, 1` when it came from a decrement (`a--`, c67; `*p -= 1` is
`add [eax], -1`, m2, because that one is an OPADD of −1 in memory).

*Compares and tests*

| # | rule | condition | address | probe |
|---|---|---|---|---|
| F19 | `cmp r, 0` → `test r, r` | first operand is a register. Memory keeps `cmp [m], 0` | 10c292e8 | c28 vs c47 |
| F20 | `test x, signbit; je/jne` → `test x, x; jns/js` | mask is the sign bit of the operand size, the consumer is je/jne (TrySignBitTestPeep 10c2a750). Applies after F21 has narrowed the operand; a dword in memory is left as `test DWORD PTR [m], 80000000h` | | h42 (`test al, al; jns`), c76 |
| F21 | and/or/test with an immediate narrowed | LoShortenLogicalInstrSize 10c2ad80 → SmdShortenLogicalCandidate 10c63760 (G): `test` only when the consumer is je/jne; memory operands: `test` only. Mask inside bits 0-7 → byte form at both levels. Mask inside bits 0-15 → 16-bit form at /O1 only. A mask reaching the high word is never narrowed and the address is never offset | | c58, c60, h45; c59, c61, h44, h47 (/O1 `test ax, -256`); c62 |

*Stack pointer*

| # | rule | condition | address | probe |
|---|---|---|---|---|
| F22 | `sub esp, 4` → `push ecx` | both levels, always ecx, no liveness test | 10c29593 | p1 |
| F23 | `sub esp, 8` → `push ecx; push ecx` | /O1 only | 10c295bd | p2 |
| F24 | `add esp, 4` → `pop ecx` | both levels, only when the next tuple is the return | 10c2964a | m41, q8 vs k2, p15 |
| F25 | `add esp, 8` → `pop ecx; pop ecx` | /O1, next tuple is the return | 10c2965a | |
| F26 | after a call: `add esp, 4/8` → one / two `pop ecx` | /O1, the call clobbers ecx; 12 or more stays `add esp, N` | 10c29695 | k4, m52 |

So the frame is allocated as `sub esp, N` (MdmInsertEntryExit) and F22/F23 turn the 4- and 8-byte cases into
pushes. F24 looks at the tuple order before scheduling: `call; add esp,4; mov eax,[a]; ret` keeps the add.

**LoFinalStackOpt 10c2b1d0** (G, both levels) runs before all of the above and merges stack adjustments. It
keeps one pending `add/sub esp` tuple P:
- P is forgotten at a label, branch, switch, at any instruction other than push that changes esp (pop, a
  callee-pop call), at `push esp` and at any use of esp as a value. So nothing merges across blocks, across a
  stdcall call, or across epilogue pops (p16: `add esp, 4; add esp, 16` in a duplicated epilogue; p10:
  `add esp, 12; ...; pop edi; pop esi; pop ebx; add esp, 8`). **[C]**
- Pushes after P are counted; a cdecl call must be followed by `add esp, pushed` or P is forgotten. **[C]**
- At the next `add/sub esp` with nothing pushed in between (10c2b2a2) the earlier adjustment is deleted and
  its amount added to the later one. Cleanups therefore collect at the **last** call of a straight-line run
  (k20: three calls, one `add esp, 12`), and merge with the frame release when no pop intervenes (p1:
  `add esp, 8` = argument 4 + local 4). A chain stops once the sum reaches 64 (10c2b34e; fl_b k_many: ten
  8-byte calls give `add esp, 64` after the eighth, then `add esp, 16`). **[C]**
- /O1 only (10c2b448): when P is exactly `add esp, 4` and the next push is of an address constant or an integer
  ≥ 256, P is deleted and the push becomes `mov DWORD PTR [esp], imm` (call2.c q16: `push 97; call _extc;
  mov [esp], 1000; call _exts; pop ecx`). **[C]**
- At /O1 the merge runs first, then F24-F26: 4 → `pop ecx`, 8 → `pop ecx; pop ecx`, 12 → `add esp, 12`. **[C]**

With an ebp frame the cleanup before `mov esp, ebp` is not removed (`add esp, 4; mov esp, ebp; pop ebp`,
pro.c with `/Oy-`). **[C]**

*cmov*

**F27. cmov needs `/arch:SSE` or `/arch:SSE2`. [C]** Gate at 10c29c13: MODULE+0x450 ≥ 2; the default arch never
emits cmov, for any source shape (cmp.c c25-c30, idi.c i2-i10). With `/arch:SSE`, LoTryCMOVBlock 10c2b980
converts `jcc L; mov..; L:` when every mov is legal (LoLegalCMOV 10c2b770: not a byte, register destination,
source a register or a direct variable, not `[reg]`): at /O2 only when exactly one mov is skipped and there is
no else arm; at /O1 also the if/else shape with at most two movs in total. fl_c.c: /O2 m_b
`cmp ecx, edx; cmovl esi, _g2`, m_c `cmovl edi, esi`, m_d (two movs) keeps the branch; plain
`if (a < b) a = b; return a;` gets no cmov at /O2 because the return was tail-duplicated into the arm first.

*Other cases* **[R]**: LDCARRY → `sbb r, r` (seen in the trace of ext2.c x21), LDZERO → `xor r, r`,
`sahf` + jcc → `test ah, mask` (LoFinalSahfOpt 10c2a850, x87.md).

### 11.2 LateGlobalOptimize 10cc9e20 (phase 5e, on SSA built over the machine code)

(Not re-verified, except the zero-register compare `xor eax, eax; cmp [m], eax; sete al`, which v_int.cpp q_eq0,
q_bne, q_sgn and K::eq0 show under the game flags.)

Sub-steps (il_pipeline.md §5.4): LgoTrackHiZero, SsaPeepsMd 10ccf590, the CleanSsa* sequence, PpSsaDeadStores,
LgoDeadCode. Seen in `passdiff.py src/c2/bitvect.c Sbv @LateGlobalOptimize@16,@FgDoLateFlowOpts@8` **[C trace]**:

- a copy whose source and destination already hold the same value is deleted (`mov esi, ecx` ... `mov ecx, esi`
  before a call: the second is removed);
- a load of a location stored earlier from a register uses the register: `mov [eax+8], ebp` ...
  `or ecx, [eax+8]` → `or ecx, ebp`; `mov ecx, [eax+8]` → `mov ecx, ebp` (CleanSsaMdIsRedundantLoad 10ccf9a0);
- an immediate 0 becomes a register known to be zero: `xor eax, eax; cmp [ebx], 0; setne al` →
  `cmp [ebx], eax`. This is where `xor eax,eax; cmp DWORD PTR _a$, eax; sete al` comes from (§5);
- dead definitions are removed.

The opposite substitution, an immediate for a register that holds it, is refused when it would lengthen the
code (PpSsaMdConstSubstWorthIt 10ccf290): not into `push` when the constant is not imm8 and the register has
other uses; not into the second operand of adc/add/and/or/sbb/sub/test/xor whose first operand is eax under the
same condition **[R]**.

### 11.3 MscClean 10b9f040 (phase 5h, before Schedule)

A forward walk that forgets everything at each label. It keeps, per register, the set of things known equal to
it: a constant, a symbol address, a memory operand. **[R]** for the mechanism, **[C]** for the effects:

1. `mov r1, r2` when both are already known equal: deleted.
2. A memory source operand whose value is in a register is replaced by the register (MdmCanReplaceMemWithReg
   10c571b0), `cmp x, 0` then becoming `test`.
3. `OFFSET sym` in a register: the register is used, `lea` becoming `mov`.
4. An immediate operand whose value is in a register is replaced by the register (MdmCanReplaceImmedWithReg
   10c57470), for every instruction except shifts and rotates, `imul`, `bt*`, `shld/shrd`, `ret`, `in/out`,
   `enter`, and `add`/`sub` on esp or ebp; a byte operand needs eax, ecx, edx or ebx.
   Seen: `mov [ecx], 0` → `mov [ecx], edx`; `cmp [ecx], 0` → `cmp [ecx], edx` (bitvect.c);
   `xor eax,eax; cmp _a$,eax; jne; mov _a$,eax; jmp _ext` (cl.c z7).
5. `mov r32, imm32` when some register holds a constant c with `imm − c` in [−128, 127] becomes
   `lea r, [r2 + (imm − c)]` (MdmIsLongLoadImmed 10c57770, MdmShortenLoadImmedWithReg). At /O2 only if r is not
   read by the next 4 instructions (stopping at a call or branch).
   Seen: `mov ecx, 1; lea edx, [ecx+1]; lea esi, [ecx+2]` (call.cpp k14: a struct {1,2,3} built for a call);
   `xor eax, eax; ...; lea edx, [eax+20]` (ext.c e45, a loop count of 20); `mov edi, 32` → `lea edi, [edx+32]`.

The register must hold the value **before** the use in pre-scheduling order: `*p = 5; return 5;` is
`mov [eax], 5; mov eax, 5` and `*p = 0; return 0;` is `mov [eax], 0; xor eax, eax` (cl.c z2, z3), because the
return value is loaded last (re-verified, C++ game flags: v_int.cpp s_ret5, s_ret0, and s_123 for item 5; items
1-3 are (not re-verified)). So an odd `lea r, [r2+k]` with r2 a constant means "r2 was loaded with a constant
earlier in this straight-line run", and the scheduler may have moved the instructions apart since.

MdmSuperHack 10c58c40 (after Schedule) removes store/reload pairs through spill temporaries (§13); it changes
nothing unless `RegSpill` ran.

---

## 12. /O1 against /O2 at a glance

| item | /O2 | /O1 |
|---|---|---|
| ±1 | `add r, 1`, `sub r, 1`, `add [m], 1` | `inc`, `dec` |
| constant 1; small constant | `mov eax, 1`; `mov eax, 5` | `xor eax, eax; inc eax`; `push 5; pop eax` |
| store 0 / −1 | `mov [m], 0` / `mov [m], -1` | `and [m], 0` / `or [m], -1` |
| `x << 2`, `x << 3` in a register | `add r, r` chains | `shl` |
| ×3, ×5, ×9, ×7, ×15 ... | lea / shift sequences (budget 9) | `imul` |
| / constant | magic number | `idiv` / `div` |
| memory argument | `mov r, [m]; push r` | `push [m]` |
| virtual call | `mov edx, [eax]; call edx` | `call [eax]` |
| cleanup 4 / 8 bytes | `add esp, 4/8` (`pop ecx` only before `ret`) | `pop ecx` / `pop ecx; pop ecx` |
| 8 bytes of locals | `sub esp, 8` | `push ecx; push ecx` |
| frame release with ebp | `mov esp, ebp; pop ebp` | `leave` |
| struct copy | ≤ 6 moves: mov pairs; else `rep movsd` | `movsd` × ≤ 4, else `rep movsd` |
| switch table from | 4 compares | 8 compares, and only if smaller |
| narrow load repeated in a block | `movzx r32` (Sizeext) | `mov r8` |
| 16-bit `test` | no | yes |
| tail call with rewritten arguments | yes | no |
| `x ? c : 0` | branchless | branch |

## 13. Open questions

- **MdmSuperHack** 10c58c40 (after Schedule, before encoding): **settled. It removes the store/reload pair of
  a stack temporary that is written once and read once**, in practice the spill temporaries that `RegSpill`
  10bdad70 creates after Color (regalloc.md §6.3 step 4). It only looks at symbols of kind 3 with no single
  defining tuple (`sym+0x14 == 0`), non-float, not volatile. **[C code + trace]**
  - Pass 1 (10c58c40..): one walk over the function; three bit sets (stored, read, candidate) are reset at
    every label and branch. A candidate is a temp whose first reference is a read by an instruction with a
    register destination; the walk records (temp, that register, the reading instruction).
  - Pass 2: for an instruction whose destination is a candidate temp: if the recorded reader is a plain
    `mov reg, [tmp]`, and `reg` is neither used nor defined and the temp not killed between the two, the
    store's destination is replaced by `reg` (`TupReplaceDst`) and the `mov` is deleted. Otherwise, if the
    store is `mov [tmp], reg1` with nothing touching `reg1` in between, the reader's operand becomes `reg1`
    and the store is deleted.
  - Pass 3: `mov [tmp], rA` directly followed by `mov rB, [tmp]` becomes nothing (rA == rB) or `mov rB, rA`.
  - Trace: 0 changes in `src/c2/bitvect.c` and in every earlier probe (no spill temps); 7 `TupReplaceDst`
    calls in `work/scratch/big/b250.c` `_big`, each `mov t35797 <- ecx; ...; mov edx <- t35797` → `mov edx <-
    ecx` (`il --diff @MdmSuperHack@4,@dop2_EncodeAndRelated@4`: 12079 → 12072 lines).
  - For matching: it only matters in functions large enough to exhaust the scratch registers; there a
    register-to-register `mov` between two scratch registers can be the remains of a spill.
- **Rotate recognition: settled. `OptRotatePeep` 10bb7730** (optimize.obj), called twice (once for each
  direction, intrinsic ids 0x9f `_rotl` and 0xa0 `_rotr`) from the `OPOR` case of `OptPeeps` 10bb29f0, i.e.
  in `OptFuncExpr`, before GlobalOptimize. The IL already holds `OPINTRINSIC intrin=159` at `@GlobalOptimize@4`
  (0 at the first `@OptFuncExpr@8`). `GoptFoldRotate` 10b4c770 is only the constant folder for a rotate with
  constant operands. **[C code + trace]** Conditions read in the code: an `|` node of at most 4 bytes, native
  code; its operands are a left shift and a right shift of equal trees; the counts are two constants that add
  up to the operand width, or `n` and `width − n`; the value is unsigned (or is widened from a narrower
  unsigned type of that width, which selects the 8- and 16-bit forms). Probes (`work/scratch/openq/rot2.c`)
  **[C]**: `(a<<5)|(a>>27)` → `rol eax,5`; `(a>>5)|(a<<27)` → `ror eax,5`; variable count → `rol eax,cl`;
  through two named temporaries, on `*p`, and on `(unsigned)a` for an `int a` → `rol`; on `a + b` → `add; rol
  eax,7`. **Not** recognised: `+` or `^` instead of `|`; `int` with a signed `>>` (`sar; shl; or`); counts
  that do not add up (5 and 26); `unsigned short` written `(unsigned short)((a << 3) | (a >> 13))` (the
  shifts are done at int width: `shr eax,13` and three `add ecx,ecx`).
- **Signed `% 2^k`: settled.** The intrinsic tuple (0x298, id 0x6e2; printed `OPHARDINTRIN .. <- 1762 x 8`)
  survives register allocation as one instruction and is expanded by **`CgiFinalLowerHardIntrin` 10c16b00**
  (cgintrin.obj, `case 0x6e2`), called from `LoFinalLower` 10c28970 in `dop2_CodegenFinish`. It builds the
  mask constant `(2^k − 1) | 0x80000000`, the `and`, a new label and the `jns`, then `dec; or −2^k; inc`.
  **[C code + trace: `il --count 'cc=ns|dec'` is 0 at `@dop2_CodegenFinish@4` and 2 at the next pass;
  `-H @CgiFinalLowerHardIntrin@4` fires once inside `LoFinalLower` for `rem8`]** Consequences: the value
  occupies one register across the whole sequence (it is one instruction for Color and for the scratch
  rotation), and the branch does not exist for any flow optimisation before `dop2_FinalFlowOpts`.
- **LateGlobalOptimize**: rules in 11.2 are from traces of bitvect.c and two predicates; the CleanSsa* set logic
  (which loads count as redundant across blocks, when a constant goes back into an instruction) is not read.
- **MscClean**: the group bookkeeping (when a register's knowledge is dropped on a partial write, the branch
  inversion at 10b9f79e) is read only in outline.
- **AddrModes**: BuildAddr's three-term re-association (10c0a6b7..10c0a8e0, scale 1 only, uses
  AdrKilledBetween) is not decoded; whether AdrFoldLea fires in ordinary code was not seen. The "sum ≥ 2" rule
  for constants in registers (7.4) is fitted to probes, not read in Color.
- **Load folding at /O1**: `a + *p` after a call keeps `add eax, _a$` (ad_2.c f19); the allocation side of 7.1
  was not examined at /O1.
- **Switch**: the `_NoDefault` collapse path (SwNoDefPossibleCollapseCount 10bed2e0, SwUseIgnore 10bed980),
  `__int64` switches (SzoInt64Switch) and PGO's pivot adjustment are not decoded. `_Switch_Parameters` row
  entries [1] and [3] have no reader in the functions examined.
- **Block copies**: where memcpy(constant) becomes OPMBASSIGN and where the 4096 cut is made (c1xx or the
  reader); blkmovbody's "large type coverage" branch (10c121ee) never fired at the default arch.
- **Tail calls**: `a ? e1(a) : e1(b)` merges into one call and gets no tail call at /O2 (q_tail.c k36), reason
  not traced. FUNC+0xac bits 0x40/0x100/0x400 tested in PpCallRetToJmp are unidentified.
- **Question costs**: the rejection of SDSU-temp arms at /O2 in the cost check has no probe.
- **F7, F8, F17, F18** and the esp-relative path of LoFinalStackOpt (LoFixStackIndir 10c2b720) are read from
  code only.
- **64-bit integers** are outside this page except where a probe happened to show them (idi.c i16-i24:
  `add/adc`, `shld`, `__allmul`, compare as high-then-low with `jg/jl/jae`).
- **C++ game flags** (`/TP /MT /GS /EHsc /Gy`): the original probes are C at plain /O2 or /O1 (call.cpp
  excepted); §14 re-tests the /O2 rules under these flags. Functions with EH state lose tail calls (8.4). They
  do **not** lose scheduling under `/EHsc`: il_pipeline.md 5h skips Schedule for EH functions "unless -EHs", and
  the probes with an EH frame show scheduled code (v_cp2.cpp K::eh24: `mov edx, [eax+20]; push 2;
  mov [esi+24], edx`; v_sw.cpp ehsw: `cmp eax, 4; mov __$EHRec$[esp+32], 0; ja`) **[C]**. An earlier version of
  this bullet said they lose both.
- `/GL` (LTCG) and PGO were not probed.

## 14. Verification (second pass, C++ game flags)

Method: fresh probes in `work/scratch/isel/verify/` (`v_cp*.cpp`, `v_set.cpp`, `v_fill.cpp`, `v_sw.cpp`,
`v_sw2.cpp`, `v_int.cpp`, `v_int2.cpp`, `v_t.cpp`, `v_f15.cpp`; about 550 functions, compiler-generated
ones included), compiled as C++ with
`/O2 /TP /MT /GS /EHsc /Gy` by `verify/run.sh`, in free functions, member functions (`this` in ecx), functions
with an EH frame and a `/GS` cookie, and next to loops and calls. Listings are in `verify/out/*.fmt`. Only /O2
was re-tested.

**Tally. [C]** 92 rules re-tested (§2: 5, §3: 9, §4: 7, §5: 5, §6: 6, §7: 7, §8: 10, §9: 13, §10: 11, §11: 19).
84 held as written. 8 were corrected or given a condition:

| # | where | was | now | probe |
|---|---|---|---|---|
| 1 | 10.1 / 10.3 | every struct copy is one block move, unrolled iff `moves(n) <= 6` | true only for trivially copyable types; a class with a vtable or a non-trivial member is copied member by member (scalars as pairs without limit, array members as an element loop in `operator=`, unrolled up to 10 elements) (10.1a) | v_cp2.cpp, v_cp3.cpp |
| 2 | 10.8 | "`rep stosd` never appears at /O2" | never from memset; fill loops give `rep stosd` / `rep stosd` + `rep stosw` | v_fill.cpp; Fallout 3 0x0050c33d, 0x0084d91c |
| 3 | 10.3 | pairs "strictly paired", always through edx | holds for pointer-to-pointer copies; the temporary rotates when a pointer is in a callee-saved register; the last load runs ahead for struct returns and by-value arguments | v_cp.cpp K::get24, arg24; v_cp2.cpp K::eh24 |
| 4 | 8.4 | no tail call with a `double`/`__int64` parameter | only when the callee takes stack arguments; a callee without them is still `jmp`. Added: no tail call under a `/GS` cookie; thiscall forwarders | v_int2.cpp td, tll, tf, K::gsbuf; v_t.cpp |
| 5 | F15 | inc/dec survives at /O2 when the flags are consumed | not reproduced: add/sub in all six flag-consuming shapes | v_f15.cpp |
| 6 | D5 | remainder: sign fix is `add edx, eax` **[I]** | only when the multiply back is `imul`; with an lea/shift sequence it is `add eax, edx` **[C]** | v_int2.cpp r25, r7 |
| 7 | 9.5 | range node lower bound `cmp x, lo−1; jle` or `cmp x, lo; jl` (unclear condition) | `cmp x, lo; jl`; `test x, x; jle` only for lo = 1 | v_sw2.cpp rng, rng0, rng1 |
| 8 | §13 | EH functions lose scheduling | not under `/EHsc`; they are scheduled | v_cp2.cpp K::eh24, v_sw.cpp ehsw |

Additions found on the way: the redundant `and eax, 255` for byte 2 of a signed `*p` (4.3); `xor cl, cl` for
byte-only constant stores and the two-member case through `this` (7.4); `char s[N] = ""` sizes (10.8); inline
`strcat` as the source of `rep movsd` + `rep movsb` (10.9); switch tables before the EH funclets (9.7).
Upgraded from [I] to [C]: the outcomes of the "sum ≥ 2" rule of 7.4 (mechanism still [I]); D5's sign fix.

**Cross-check against Fallout 3** (`Fallout3_1703.exe`, read-only; `verify/f3scan.py`, `f3find.py`,
`f3imul.py`). Five sites, each predicted by the rules **[C]**:

| VA | construct | rule | result |
|---|---|---|---|
| 0x0083b376 | member-wise copy: dword pairs rotating eax/ecx/edx (pointers in ebx/ebp), a 28-byte member as `lea esi; lea edi; mov ecx, 7; rep movsd` | 10.1a, 10.3 | as predicted |
| 0x00405597 | `movzx ecx, BYTE PTR [eax+4]; add ecx, -24; cmp ecx, 57; ja; movzx ecx, BYTE PTR idx[ecx]; jmp tbl[ecx*4]`; DD table of 7 entries at 0x00405684, DB table right after at 0x004056a0 | 9.3 (n = 58, T = 6: 174 > 36), 9.6 (`add`, never `sub`; byte operand extended first), 9.7 | as predicted |
| 0x004076b5 | switch on five FOURCC values: `cmp ebx, 'EFID'; jg; je`, then `cmp; je; cmp; jne` chains on each side | 9.2 (R ≥ 255 → split), 9.4 (no cluster of 4: pivot = sorted node N/2 = 2), 9.5 (`cmp` form, ascending) | as predicted |
| 0x0058556a | `add ecx, -50; mov eax, 66666667h; imul ecx; sar edx, 2; mov eax, edx; shr eax, 31; add eax, edx` | D3 (dividend copied to ecx), F9 (`add r, -50`) | as predicted |
| 0x00426cbf | `mov eax, [esi+34h]; sub eax, 0; je; sub eax, 1; je; sub eax, 1; je` | 9.2 step 1 (3 cases: chain), 9.5 (`sub` form, `sub eax, 0` for case 0) | as predicted |

Whole-`.text` counts **[C]**: adjacent `mov ecx, N; rep movsd`: 130, none with N < 7; direct tables behind
`cmp r, imm8`: 342, smallest bound 3 (four cases), none smaller; two-level tables: 299 behind an imm8 bound,
smallest bound 6; table prologues `add eax, -lo; cmp; ja`: 208, with `sub eax, lo`: 0; `imul r, r, imm8` in the
game's own code (below 0x00bf0000): 44, 52, 76, 84, 100, 104, 108, 112 (33-47 each), all constants M5 says are
`imul`, and none of 6, 10, 12, 20, 24, 40, 48, 56, 60, 72, 80, 96. The statically linked CRT (from about
0x00bff000) is built with other options: it has `imul r, r, 56`, `imul r, r, 12` and `push DWORD PTR [esp+n]`,
so do not apply the /O2 rules there.

**Not re-verified** (rules kept as written): every /O1 rule and column (M6, D1's /O1 half, 5.4's /O1 costs,
7.2-7.4 /O1, 8.x /O1, 9.2 step 4, the /O1 pivot, 10.4, F3, F4, F6, F16, F23, F25, F26, §12's right column);
the cost numbers of M2-M4 and 5.4 as such (only their outcomes were re-tested: 37 multipliers, 22 question
shapes); A0 and A7's volatile exception; D2 beyond 3, 7, 10, 25, 641 and −3; the internals of 4.7; 7.2's list of
operators that do not merge; 9.1's `__assume(0)` case bodies and 9.2 step 2; the absorb step of 9.4's clustering;
10.9 `/arch:SSE2`; F5, F7, F8, F17, F18; F27 with `/arch:SSE`; 11.2 except the zero-register compare; 11.3
items 1-3; everything marked [R].
