# Constants as register candidates: when a constant counts in a block's pressure P

A constant operand can be a register candidate (an *immediate candidate*: a globreg whose symbol has kind 0xd,
`imm` in `c2trace.py regs`). Most of them end up without a register and leave no code, but while Spillcosts
runs they are globregs like any other: every block that **references** one has it in its pressure P, and P
multiplies the priority of every other value referenced in that block (regalloc.md §3.2). This file says which
constants become candidates, in which blocks they are referenced, and what in the source changes that.

Tags: **[C]** read in the code at the address given (14.00.50727.762 unless a 13.10.3077 address is named) or
seen in a trace; **[I]** inferred; **[M]** measured with `tests/data/rules/RF_immcand.cpp` on all four builds
(14.00, 15.00, 13.10.3077, 13.10.3052; checks in `tests/probes/frame.py`, read from `regs --export`). Every rule below was probed on 13.10.3077, 14.00 and 15.00.30729; the probe
sources and the recorded outputs are in `work/scratch/immcand/` (`a1 a2 b1 c1 d1 e1 .c`, `out_<probe>_<build>.txt`;
`pb.py` prints, per function, every block's P, the immediates referenced `K(cost)` or live through `K~`, and a
hash of the encoded bytes). Where a build behaves differently it is said.

`regs` now prints the constant behind each `imm0` (tracer.md, "Immediate candidates"), which is how the tables
below were read.

## 1. Summary for a decompiler

1. A constant is a candidate when the function has **two or more qualifying operands with the same value**, or
   **one qualifying operand inside a loop**. A constant used once outside loops is never a candidate.
2. "The same value" is the operand kind plus one 32-bit word: the integer value, or the symbol for an address.
   Size and type do not matter: `p->b = 7; p->c8 = 7;` is one candidate; `char = -1` and `int = -1` share one,
   `unsigned char = 255` does not share with them. `0`, `NULL`, and on cl 13.10 also `0.0f`, are one candidate.
3. A candidate is in a register "region" from its first use to its last along every path. It is **referenced**
   (counts in P) in each block that uses it and in the block that holds its load (`OPENTERLOAD`), and it is only
   *live through* (does not count in P) in the blocks between.
4. The load sits just before the first use, except when a join is reached both from a path that has used the
   constant and from one that has not, and the constant is used after the join. Then the load is hoisted to the
   nearest block that dominates the join's predecessors, and placed before that block's branch. For a loop that
   is the block ending in the loop's zero-trip test. **That is why a constant used in a loop and again after it
   is "loaded before the loop guard" and counts in the P of the loop-entry block**, while a constant used only
   inside the loop is loaded in the preheader (after the guard) and does not.
5. Naming the constant (`#define`, `enum`, `const`, `static const`, `sizeof`, an inline helper returning it),
   declaring a variable earlier or later, or splitting `T x = K;` into `T x; x = K;` changes nothing: the front
   end hands c2 the same operand in the same instruction. What matters is which instructions carry the constant
   and which blocks they are in when `Globregs` runs.

## 2. The rules

### 2.1 Which operands qualify **[C]**

`CmdIdentifyImmedCandidates` 10c26030 (13.10.3077: 1077d560) runs first in `Globregs` 10b563f0, after lowering:
the IL is machine instructions. It walks every instruction's source operands (10c2611b..10c26176):

- an integer constant (leaf kind 7) qualifies if `CmdCanEnregImmed` 10c23fb0 accepts the instruction;
- an address constant (kind 3 data address, kind 4 code address) qualifies under the same test, unless the
  instruction is a call (instruction kind 0xf: the call target) or of kind 0x12;
- `lea` (opcode 0x12) is handled separately and never yields a candidate: a displacement or `[reg+K]` is not a use.

`CmdCanEnregImmed` refuses (returns 0 for) these, and accepts everything else:

| refused | code |
|---|---|
| `enter`, `imul` (all three forms: 6, 0xa3, 0xc1), `int`, `ret`, `in`, `out` | switch at 10c23fe6 |
| shifts and rotates `rcl rcr rol ror sar shl shr`, `shld shrd`, `bt bts btr btc` | same |
| `pextrw pinsrw pshufw shufps pshufd pshufhw pshuflw pslldq psrldq shufpd`, `psllw..psrlq`, 0x1bd, `OPHARDINTRIN` | same |
| `mov R, 0` where R is a register operand that is **not** a register candidate (a single-def temporary or a physical register) | case 1 |
| `cmp R, 0` with the same kind of R (it becomes `test R, R`) | case 0x2f |
| `add` / `sub` on `esp` or `ebp` | cases 0x2d, 0x32 (`_RegSym[5]`, `_RegSym[6]`) |
| everything when `_Wvm_flag` is set (managed code) | 10c23fb0 |

So these **are** uses: a store `mov [m], K`, `push K`, `cmp [m], K`, `cmp var, K`, `and`/`or`/`xor`/`test` with
K, `add var, K` when it stays an `add`, `mov var, K` and `cmp var, 0` when `var` is a register candidate (a named
variable or a multi-def temporary, regalloc.md §7), `mov eax, K` for a non-zero return value, `push OFFSET str`,
`mov ecx, OFFSET global`, a stored vtable or function address. Probes (`c1.c`, same on the three builds unless
noted):

| function | uses | candidate | note |
|---|---|---|---|
| u01 | two stores `[p+4]`, `[p+8]` of 1000 | yes, cost 2 | coloured (`mov ecx, 1000`) |
| u02 | two stores `[p]`, `[q]` | yes, cost 0 | |
| u03 | two `push 1000` | yes, cost 0 | |
| u04, u28 | two `cmp`/`test [m], 1000` | yes, cost 1 each | |
| u05, u07 | `cmp var, 1000` / `and var, 1000` twice | yes, cost 0 | |
| u06 | `x + 1000` twice | no | the adds are `lea` |
| u08, u09 | `<< 5`, `* 1000` twice | no | refused opcodes |
| u13 | `x = 0, y = 0, i = 0` on register variables | yes (0) | |
| u14 | `[m] == 0` twice | yes (0) | |
| u10, u12 | `float = 1.0f` twice, `gf(2.5f)` twice | **13.10: yes** (0x3f800000, 0x40200000); 14.00, 15.00: no | on 13.10 a float store is an integer store of the bit pattern at this point |
| u11 | `p->x = 0.0f; p->b = 0;` | 13.10: one candidate 0, two uses; 14.00, 15.00: none | |
| u19 | `int = 7; char = 7` | one candidate, two uses | size is not part of the key |
| u22, u23 | 0x12345678 twice, −1 twice | yes | value does not matter |
| u24 | `g(1); g(2);` | no | two different constants, each once |
| u25, u26 | `h(); h();` / `gp(h); gp(h);` | no / yes | a call target is not a use, a pushed function address is |
| u16..u18 | `&gs`, `&gs.first` in any mix | one candidate | same symbol |

A cost column of 0 means the use adds nothing to the candidate's own priority; **it still counts in P**.
(`CmdSubCost` 10c237c0: 1 when the other operand is memory with a symbol or displacement, else 0;
regalloc_cost.md.)

### 2.2 Which constants are the same candidate **[C]**

`GrgRewriteImmedAsReg` 10b56700 looks the operand up in `_ImmedHashTable` (10d12170, 64 buckets, cleared per
function at Globregs entry). Two operands are the same candidate when the leaf kind byte is equal and the dword
at leaf +0x18 is equal (10b56727..10b56750): the low 32 bits of an integer, or the symbol pointer of an address.
Neither the operand's type nor its size is compared. A new constant gets a symbol of kind 0xd with a copy of the
leaf at +0x28 and one candidate bit; a second sighting sets `fl5 |= 0x10` on that symbol (10b56865).

- Same value at different sizes: one candidate (u19; `d1.c` s1, s3: `int = -1` with `char = -1`, `short = -1`
  with `char = -1`).
- The integer is the sign- or zero-extended value of the operand's own type: a `char` −1 is 0xffffffff, an
  `unsigned char` 255 is 0xff. So the same stored byte is two different candidates depending on the signedness
  of the lvalue (s1 against s2, s5 against s6). **[C trace]**
- Two symbols at one address are two candidates. Seen in Mount & Blade `CharSheet::init_screen`: `&g_game_vars`
  as the `this` of a member call, and `&g_game_vars.entries[i].key` after strength reduction (offset 0, another
  member symbol), both `mov r, OFFSET g_game_vars`. `regs` marks the second `#2`. Which symbol an address uses
  follows the type of what is accessed; none of four respellings of the access changed it (casts through the
  struct, a reference or pointer parameter of an inline helper). **[C trace; no source lever found]**
- `0`, `NULL`, `false` and `(T*)0` are the integer 0.

### 2.3 Once-only constants **[C]**

`ReachingOccurGenKill` 10b56f20 visits every candidate operand again. At 10b57802..10b5782e: if the symbol is an
immediate (kind 0xd), its `fl5 & 0x10` is clear (seen once in the function), and the block is not in a loop
(`BLOCK+0x16c` == 0) or `_Speed` is 0 (/O1), the operand is turned back into a plain constant (10b57834 ff.). So:

- one use, outside any loop: not a candidate (u21; `a1.c` f2);
- one use inside a loop at /O2: a candidate. It is loaded in the loop's preheader and referenced there and in
  the block of the use (u20, `b1.c` b6: P counts it in both blocks). It never appears as a pop in `regs`, because
  it is dropped after costing, but **it was in P**;
- two or more uses anywhere: every use is a candidate operand.

That the field at `BLOCK+0x16c` means "in a loop" is **[M]**: a once-only 1000 is a candidate inside a `for`, a
`while`, a `do`-`while`, a backward-`goto` loop, the inner loop of a nest and an `if` arm inside a loop, and not
after a loop or with no loop (`RF_immcand.cpp` l_*); at /O1 none of the loop forms makes it one. Same on all four
builds. The test itself is **[C]**.

### 2.4 The region, and where the load is placed

The dataflow is the one used for memory variables (`ReachingOccurGenKill`, `GrgPropagateSetsBackwards` 10b57fd0,
`GrgPropagateSetsForwards` 10b57da0, then the per-block sets are intersected in `Globregs`). Nothing kills a
constant, so the result is: the candidate is "in a register" at a point when some path from an earlier use
reaches the point **and** a use follows it. **[I for this reading of the sets; C for every probe below; M for dnest]**

`Enterloads` 10b588e0 then walks each block; a candidate operand met while the candidate is not in the block's
"in register" set gets an `OPENTERLOAD K` inserted before that instruction (`CmdInsertEnter`, 10b58c06). **[C]**

`AvoidAirlocks` 10b581f0 runs before it and is what moves loads upward. For a block B and a forward edge from a
predecessor that does not have a candidate which B expects in a register, the candidate would need a load on the
edge (an "airlock" block). Instead, for an **immediate, unconditionally** (the kind-0xd test at 10b58358 skips
`CanExtendLiveness` 10b58365), the load is inserted in the block `GrgFindNearestDominator` 10b5c0c0 returns (10b582e1), at
`GetEnterInsertPoint` 10b58500 (10b58435; before the block's ending branch; `CmdInsertEnter` 10b58442), and the candidate is made live from there
(`DoExtendLiveness`). **[C for the pass and the unconditional case; M for the dominator]** For a forward edge the
nearest common dominator of B's predecessors is B's immediate dominator, so "over B's predecessors" and "B's
idom" are the same block. `RF_immcand.cpp` dnest (`if (a) { h(); if (b) g(K); } h(); g(K);`) rules out one load
per predecessor that lacks K: K is referenced in block 1 (the `if (a)` test: the load), the arm and the join, and
only live through block 2 (the `if (b)` test, itself a predecessor without K) on all four builds.

Probes (`b1.c`, K = 1000; block numbers as `regs --why` prints them; identical placement on the three builds,
except that 14.00 and 15.00 also show the loop increment's `1` as a candidate where 13.10 uses `inc`):

| function | uses of K | referenced in (load) | live through |
|---|---|---|---|
| b1 | twice inside a loop | preheader (load), loop block | nothing before the guard |
| b6 | once inside a loop | preheader (load), loop block | (rule 2.3) |
| b2 | in a loop and after it | **the guard block** (load), loop block, the block after | preheader, exit |
| b3 | before a loop and in it | first use's block (load), loop block | guard, preheader |
| b4 | before and after a loop, not in it | both use blocks | guard, preheader, **the loop** |
| b5 | in two loops | the first guard block (load), both loop blocks | everything between |
| b8 | in an `if` arm and after the join | **the block with the `if` test** (load), the arm, the join | |
| b9 | once in each arm of an if/else | none: not a candidate | each use is alone in its region and is dropped (2.5) |
| `a1.c` f5 | twice after a loop that was tail-duplicated | each copy has its own load | |

On the real function (`CharSheet::init_screen`, 13.10): the block that ends with `cmp [this+0x2d0], 0 / jle` of
the first loop holds seven loads (`??_7UiA@@6B@`, 2, `&g_game_vars#2`, 44, `""`, `"+"`, 4): the constants used
in the first loop and again later. The eleven constants used only in that loop are loaded in the preheader.

### 2.5 When a candidate is dropped before Spillcosts **[C]**

- A once-only use outside loops: at `ReachingOccurGenKill` (2.3).
- `Enterloads` can undo a candidate operand (`UndoRegCandidate` 10b597e0, called at 10b58afa, 10b58d3e,
  10b59189; for kind 0xd it calls `GrgChangeRegToImmed` 10b599b0, which puts the constant back). **[C code]**
  b9 and u15 (the same constant once in each arm, or in two exits) end with no candidate at all **[C trace]**,
  and this is the path that removes them **[C trace, all four builds]**: in b9 (`RF_immcand.cpp`) Enterloads calls
  `UndoRegCandidate` (14.00 from 10b59189) and each of the two uses goes back through `GrgChangeRegToImmed` on
  the kind-0xd symbol, before `CreateGlobregs`; in b8 nothing is put back there (15.00 and 13.10 put b8's 1000
  back later, after CreateGlobregs).
- `CreateGlobregs` 10b5abe0 (test at 10b5ac15) removes the globreg of every immediate symbol whose candidate
  record has `+0x14 < 1`. **[C for the test; I for what the counter counts: lifetimes that survived; an internal
  counter, not decidable from a listing]**

A candidate that survives these is in P of every block that references it, whether or not it is ever coloured.
After costing, `Color` spills or defers almost all of them (benefit ≤ 0: a load costs 1, a use saves 0 or 1).

### 2.6 What the constant costs the others

For a block B with weight w, one more referenced candidate raises every referenced value's term by
(its cost in B)·w and lowers every live-through value's term by w. The constant itself is live through the
blocks between its uses and pays −P·w there, which only matters for its own (irrelevant) priority.

## 3. Recipe

Read the block first: `regs --block N` lists the referenced values with `= K (const|addr|code, size)` for the
immediates; `regs --why 'imm=K'` shows in which blocks K is referenced.

**To remove one constant candidate from block B** (all of its references there must go):

| lever | what to do | code-neutral? | evidence |
|---|---|---|---|
| R1 once-only | bring the function's qualifying uses of K down to one (outside loops) | only if the removed use was not needed | 2.3 |
| R2 other signedness | a `char`/`short` store or compare of −1, −2, .. (or 128..255, 32768..65535) through the type of the other signedness, so that it no longer shares a value with the other uses: `*(unsigned char*)&x = 255` for `x = -1` | **yes** when the candidate was not coloured: same bytes | `e1.c` k1 → k2: identical code on 13.10, 14.00 and 15.00, P 3 → 2, both variables 24 → 16 |
| R3 region | make the load land elsewhere: if K is used in a loop and after it, the load is in the guard block (2.4). It leaves that block only if K is no longer used after the loop (then: preheader), or if K is already used before the block on every path (then it is live through, not referenced) | no: needs a use added or removed | b1/b2/b3 |
| R4 block | move the *instruction* that uses K into another block with the same final code | yes where the compiler merges it back | the `guard` family of rwsweep: `if (p) { delete[] p; p = 0; }` against `delete[] p; p = 0;` (frontend.md) |

**To add one candidate to block B:**

| lever | what to do | code-neutral? | evidence |
|---|---|---|---|
| A1 second use | give a constant that B uses once a second qualifying use anywhere on a path through B (it becomes a candidate at both uses; on the blocks between it is only live through) | no | f2 against f1 |
| A2 same signedness | the reverse of R2: spell a narrow store so that its value equals a 32-bit constant already used | **yes** when the candidate is not coloured | k2 → k1: P 2 → 3 |
| A3 variable instead of temporary | `mov`/`cmp` with 0 is a use only when the register operand is a candidate: `int r = f(); if (r == 0)` with `r` surviving as a variable references 0, `if (f() == 0)` does not | only if the variable survives copy propagation, which usually changes code | `d1.c` z1, z2 |
| A4 loop | a constant used once is a candidate only inside a loop | no | u20, u21 |

Things that were tested and do **not** move a constant reference: a named constant of any kind (M&B: 108
variants), a second identical string literal (pooled: same symbol), `0` against `NULL` or `0.0f`, the same
store written in both arms of an if/else instead of after the join (`e1.c` m1/m2: merged before allocation, same
P), `for (int i = 0; ..)` against a hoisted `int i = 0;` when `i` lives in a register (`d1.c` l1/l2: same block,
same P), the order of statements after a loop (it only changes where the constant is live through), declaration
position, `T x = K;` against `T x; x = K;`.

When a candidate **is** coloured the levers change code (`mov [m], K` becomes `mov r, K` + `mov [m], r`); see
opt.md §7 item 5 for when that happens.

`tools/rwsweep.py --families const` generates R2/A2 (guarded by a `sizeof` check, so the wrong width does not
compile) and the for-init moves (`for (T i = K; ..)` hoisted over one to three statements, split off, or sunk
back). The for-init move is what places a `mov i, 0` and its reference to `i` and to 0 in an earlier block; it is
code-neutral only when `i` is in a register.

## 4. The two Mount & Blade functions (cl 13.10)

Sources copied to `work/scratch/immcand/mb/` (nothing written to the M&B tree); `cs.sh` holds the command line
(`/MT /Gy /TP` plus the project's include directories), `cs_orig.s` is the original's disassembly.

### `Game::load_game_data` 0x5236f0

Matched meanwhile by the M&B project through R4 (the `guard` family), not through a constant: see frontend.md.
Not pursued here.

### `CharSheet::init_screen` 0x507400: reproduced, explained, **not fixed**

With the natural `for (int i = 0; ..)` (`csn.cpp`), `regmap` against the original: 773 of 774 variables agree; the
one that does not is `t261` (the joined result of `skills = new text_box`): eax, the original has esi.

```
why t261 (pop 267): prio 34      block 29 P=3 +6    block 30 P=2 +4    block 31 P=12 +24
why local19 (pop 268): prio 34   block 28 P=4 +16   block 29 P=3 +18
```

A tie; `t261` has the later definition, is coloured first and takes eax. With `local19` (the `operator new`
result) first, `t261` follows it into esi through the copy preference. Any one of these would do it: P of block
31 one lower, P of block 30 one lower, P of block 28 or 29 one higher.

What the blocks hold (`regs --block`): 28 = {local19, 392, `&text_box vftable`, 0}; 29 = {local19, t261, the
vftable}; 30 = {t261, 0}; 31 = {this, t2743 (the reloaded `skills`), t261, i, 0, and the seven constants of 2.4}.

Why no constant lever applies **[C for each statement, from the trace and the original's bytes]**:

- The seven constants are in block 31 by rule 2.4 and their uses are instructions the original has too (the
  first loop and later code: `new UiA` ×3, the `add r, 44` of the inlined key walks, `push OFFSET ""` ×6 at one
  address 0x54a480 in the original, `"+"` ×6 at one address, the temp-destructor flag bits 2 and 4). None has a
  qualifying use before block 31, in ours or in the original (the earlier `imul r, 44` are refused operands).
  R1 and R3 would need different instructions; R2 does not apply (no narrow store among them).
- 0 is referenced in 28 (`cmp local19, 0`), 30 (`mov t261, 0`) and 31 (`mov i, 0`, the guard `cmp`): all in the
  original's code.
- The second `&g_game_vars` symbol could not be merged with the first by respelling (2.2).
- `rwsweep --families const` on the function: 12 variants, none code-neutral with a better result. Hoisting
  `int i = 0` over three statements (the M&B stand-in) is the only one that gives esi; it works by taking `i` out
  of block 31 (P 12 → 11, t261 32), and misplaces the store because `i` lives in memory.

So with the instructions the original has, the constants of blocks 28 to 31 are forced, and the original must
differ in block structure or in a non-constant candidate: a value referenced in block 28 or 29 that leaves no
code, or `t2743`/`i` not being a candidate in block 31. Tried here without effect: binding `skills` to a
reference before the allocation, `(&skills)[0] = new text_box`, and three spellings of the key walk. That is
where it stands.

## 5. Not verified

- 13.10 and 15.00 were checked by probes only; the code addresses are 14.00's (13.10.3077 has the same functions
  by carried-over name: `CmdIdentifyImmedCandidates` 1077d560, `CmdCanEnregImmed` 1077d728,
  `GrgRewriteImmedAsReg` 1077e132, `ReachingOccurGenKill` 1077e873, `AvoidAirlocks` 1077ecbd, `Enterloads`
  1077f00c, `CreateGlobregs` 1077ff3b; not read).
- The forward/backward set propagation was not read line by line; the region rule of 2.4 is the reading that fits
  all probes.
- /O1: `_Speed` == 0 makes every once-only constant a non-candidate even in loops (the code at 10b57827): holds
  on all four builds (`RF_immcand.cpp` l_for, l_while, l_do, l_goto at /O1) **[M]**.
- 64-bit constants: only the low dword is compared at 10b56731, but on x86 no 64-bit constant reaches Globregs: a
  `long long` store is two dword stores by then, and each half is its own 32-bit value. The high half 7 of
  `0x0000000700000005` shares one candidate with a separate `= 7` (k64d); halves 5 and 6 used once are not
  candidates (k64c) **[M, all four builds]**.

## 6. When a constant gets a register, and what that costs the others (Color, cl 13.10 to 15.00)

Sections 1 to 5 say which constants are candidates and where they count in P. This section says when Color gives
one a register, how that takes a register from a variable, and what in the source changes it. Checked on
13.10.3077 and 13.10.3052 (the Mount & Blade builds) and on 14.00 and 15.00 with `tests/data/rules/RA_constbyte.cpp`
(`tests/probes/constregs.py`); code addresses are 14.00's unless a 13.10.3077 one is given.

### 6.1 The rule

1. **Candidacy**: sections 2 and 2.3 (two qualifying uses of one 32-bit value, or one inside a loop). **[C]**
2. **Benefit** (`GLOBREG+0x40`, Spillcosts): Σ over its uses of cost·w, minus 1·w for its load (`OPENTERLOAD`;
   `CmdLoadCost` is 1 for an immediate). A use costs 1 when the instruction's other operand is memory with a symbol
   or displacement (`cmp [eax+ecx+4], K`, `mov byte [m+0x8b4], K`), else 0 (`cmp reg, K`, `mov var, K`, a store
   through `[reg]` with no displacement; `CmdSubCost`, regalloc_cost.md). **[C; M: the probe's `type` at offset 0 gave
   benefit 1, at offset 4 benefit 4]** A spill of a web that shares an instruction with the constant raises the
   constant's benefit by that block's w (`CmdUpdateCost`, regalloc.md 3.6.4). **[C]**
3. **Priority** (`GLOBREG+0x0c`): the formula of every web (regalloc.md 3.2) with those costs: cost·P·w in each
   block that references it, −P·w in each block it is only live through. So a constant whose uses are far apart
   pays −P·w in every block between them, and a constant used in a block with a large P (a loop body full of
   values) gains a lot from one use there. Immediates have defOrd 0; between equal priorities the one inserted last
   goes first (regalloc.md 3.4). **[C]**
4. **The pop** (regalloc.md 3.6.1): benefit ≤ 0 → deferred to the very end of the list and then spilled or cut
   (never coloured; immediates are never coloured at benefit 0, the one-block exception excludes them). Benefit > 0
   → coloured with the first free register in eax ecx edx esi edi ebx ebp order (weights permitting), **at its
   place in the priority order, like a variable**. A constant with prio −11 pops after every positive-prio web, but
   before a call-crossing parameter whose many live-through blocks gave it −51. **[C]**
5. **A one-byte use flags the whole web** (`fl5 |= 0x80`): `InitialSplitPoints` tests every operand's type size and
   marks the operand's globreg when it is 1 (14.00 10b0e2e9..10b0e2fb, `and cx, 0xfff; cmp cx, 1; jne; mov eax,
   [edi+0x1c]; or byte [eax+5], 0x80`; the same sequence in 13.10.3077 at 1078b91c..1078b931, 1078c8e6, 1078c96b:
   BBT-scattered, so the function name is the index's guess). The immediate symbol itself is always made 4 bytes
   (`GrgRewriteImmedAsReg` 10b56770: type 0x1004), but the rewritten operand keeps the store's byte type. **[C]**
   So a constant shared by dword uses and one byte store needs a byte register: eax, ecx, edx or ebx; across a
   call, **ebx only** (`avail at queue {ebx}`). **[M, all four builds]**
6. **Its only register is a penalty for everyone coloured before it.** While it is queued, every interfering web
   that ChoosePhysReg colours sees ebx weight +100·benefit (regalloc.md 4.2). This steers a web off ebx only when
   ebx was its best register (it then takes ebp). **[C; M: RA_constbyte q_ref, `vv` → ebp on 13.10, `k` → ebp on
   14.00/15.00]**
7. **When it is coloured it takes the register from every neighbour that still has it** (`UpdateAffectedGlobregs`):
   a call-crossing variable left with only ebx is split at that moment (its pieces are recosted in the next round
   and get what is left). **[C trace, M&B 0x4915d0 below]**
8. **When a neighbour is coloured first with its only register** (a call-crossing web that takes ebx), the constant
   is taken off the list as a neighbour without a register, and every use stays an immediate. **[C trace; M: q_ref]**
9. **After Color**, `CmdPostspill` gives a callee-saved register back unless the constant's benefit is ≥ 3 (opt.md 7
   item 5): with benefit 1 or 2 the code has immediates although Color gave the constant ebx, *and the variable it
   displaced still lost ebx*. **[C, opt.md; M: q_byte benefit 4 keeps `mov ebx, 1` and `mov [eax+12], bl`]**

| RA_constbyte.cpp | uses of 1 | byte | avail | outcome (all four builds) |
|---|---|---|---|---|
| `q_byte` | 2 dword `cmp [m+4], 1`, `dirty = 1` byte store | yes | {ebx} | ebx at its pop; `mov ebx, 1`, `mov [eax+12], bl` |
| `q_dword` | the store as a dword | no | {ebx,ebp,esi,edi} | ebx (first free) |
| `q_two` | `dirty = 2` instead (1 keeps the two compares: benefit 2) | no | {ebx,ebp,esi,edi} | taken off the list (lower prio), no register holds 1 |
| `q_ref` | as q_byte, plus `int& e = vv->b[k];` kept across the loop's first call | yes | {ebx} | a call-crossing web takes ebx first; 1 taken off the list, immediates |
| `q_noref` | the same loop without the reference | yes | {ebx} | ebx |

### 6.2 Levers (what to change in the source)

| want | lever | why |
|---|---|---|
| the constant out of ebx, a variable in it | give the variable a higher prio than the constant, **or** lower the constant's prio below the variable's (remove its use in the high-P block, or its uses in loops) | 6.1.3, 6.1.7 |
| the same | drop the byte use from the shared value: the store of another value, a non-constant, or (128..255, negatives) the other signedness (R2) | without the byte use the web is not byte, its avail has four registers, and its term in that block is gone (q_two; for M&B 0x4915d0 `constregs` estimates prio −11 → −73) |
| the same | a call-crossing web that the original has and the draft lacks, coloured before the constant, takes ebx | 6.1.8; the M&B fix below |
| no register for a constant at all | benefit ≤ 0: remove the uses that carry a memory operand with a displacement, or move them out of loops; or bring it to one use outside loops (2.3) | 6.1.2, 6.1.4 |
| a constant in a callee-saved register in the final code | benefit ≥ 3 | 6.1.9 |

Things that do not move it (the value is the key, 2.2): `1u`, `(char)1`, `true`, a `bool` field, a named constant;
`type - 1 != 0` keeps the same operand after the optimizer.

### 6.3 `tools/constregs.py`

```sh
uv run tools/constregs.py tu.cpp --func Class::method [--c2 13.10] [--value 1] [--export snap.json] -- <cl args>
uv run tools/constregs.py --from snap.json [--value 1]
```

One traced compile with the `regs` hooks plus an IL dump at `@Globregs@4` entry, where constants are still
literals. Each `k0` operand of the IL at Color entry is paired with the literal at the same operand position of the
same instruction, which gives every reference's value and width (the IL at Color prints every immediate candidate
as `k0`). Per constant of the first Spillcosts pass it prints: its prio and place in the list, benefit, references
by width plus loads, the avail set when it was queued and the byte flag; the outcome (`pop N -> reg` and the
neighbours that lost that register then, with their prio and whether it was their last register; or `taken off the
list when pop N gave X reg`); how many webs saw its only register as a +100·benefit penalty and which of them were
steered off it; each use (block, w, P, width, `mem`/`reg` = cost 1/0, the instruction); the prio terms; and levers
from 6.2 with the numbers filled in. `--export` writes the `regs --export` snapshot plus the dump (key `gil`);
`--from` reads it back (a plain `regs --export` snapshot works too, without per-use widths). Probe:
`tests/probes/constregs.py` on `tests/data/rules/RA_constbyte.cpp`.

### 6.4 Mount & Blade `GameData::remove_party` 0x4915d0 (cl 13.10): explained and fixed

`constregs.py --value 1` on the module source: `const 1  prio -11  benefit 4  refs byte 1, dword 2  avail {ebx}
BYTE`; uses `cmp [..+4], 1` (block 3, w 1, +5), the loop's `cmp [..+4], 1` (block 16, w 2, +10), `dirty = 1` (block
17, w 2, P 16, +32); `pop 34 -> ebx`, taking ebx from `id` (prio −51, its last register: split; the pieces get ebp),
`this`, `k` and the constants 0 and −1. The zero constant (16 uses, also byte through `engaged = 0`, benefit 8) is
then taken off the list, so every `cmp x, 0` is `test`/an immediate. Same on 13.10.3052.

The original keeps id in ebx outside the loop and reloads it at the loop end; inside the loop ebx is
`&p->attached[k]` (`lea ebx, [edx+ebp]`), kept across the first `lookup` call and read again as `mov edx, [ebx]`,
while the draft re-read `p->attached[k]` through a reloaded vector base. That address is the call-crossing web of
6.1.8: written as a reference,

```cpp
for (unsigned k = 0; k < p->attached.size(); k++) {
    int& a = p->attached[k];
    if ((unsigned)p->attached[k] >= m->serial) continue;
    int j = m->lookup(p->attached[k]);
    ...
    wd_at(m, m->lookup(a))->attached_to = -1;
```

it pops at 16 (prio 110, only ebx left) and takes ebx; the constant 1 is taken off the list, `id` gets ebx at pop
34, the zero constant ebp at pop 36 (`xor ebp, ebp` after the first lookup, `cmp x, ebp`), as in the original.
regmap: every Color web agrees (98 of 104; the six left are scratch picks). M&B `chk.py` aligned 158 → 55 (size 776 →
760 = the original's), `insdiff` 61/54 → 14/15 (the throwaway `dirty = (id != 0) | 1` gave 34/32). The throwaway
worked for two reasons, neither of them the sharing as such: it adds a use of `id` in the loop's store block (P 17,
w 2: `id` −51 → 49, so `id` pops at 23 and takes ebx), and the constant's byte use becomes `or r8, 1` with a register
operand (cost 0: its term there +32 → 0, prio −11 → −43, benefit 4 → 2; still byte). The rest is
scratch rotation and the order of the esi/ebp saves. Other spellings: the reference bound after the bound check,
or also used for the first lookup, gives the same 55; the reference used in the bound check too gives 74 (the
serial is loaded into ecx first); a pointer `int* a = &p->attached[k]` used throughout gives 74. **[C trace and
checker]**

### 6.5 The same family elsewhere in Mount & Blade (not constants)

- `Mission::spawn` 0x4832b0: explained and **fixed**, see 6.6 (the in-place `t` chain; the original's single `lea` is
  the sum shared by two evaluations). Before: `found` (prio 72) pops before `t` (59), `t` takes ebx and keeps it
  across the do-loop; the original has `t` in edi outside the loop and in memory inside it (`mov [esp+0x14], edi`
  ... `mov edi, [esp+0x14]`), the loop's callee-saved registers all taken by `a` (esi), `pm` (ebx), the hoisted
  `g_game->main_party` (ebp) and `found` (edi). `t`'s 59 = +96 in the entry block (P 8, **6 references**: the
  chain `t740 = [a+136]; t93 = t740*1288; t129 = t93 + [g_game+0x13a00]; t = t129` was folded into `t` by
  `SubsumeGlobregs`, so `t` is defined three times in place: `imul ebx, 1288; add ebx, ebp`) + 32 after the loop
  - 69 live-through. **[C trace for ours]**
- `Terrain::make_grid` 0x47a460: no constant holds a callee-saved register. `this` (prio 103) takes ebx on a 0/0 tie
  with ebp (register order); the original has `this` in ebp and the inner loop's count-down counter in ebx. For
  `this` to take ebp, ebx must carry a positive weight when it is coloured (an uncoloured neighbour whose only
  register is ebx, or one that prefers it), or the counter must be coloured first. **[C trace for ours; I for the
  original]** 67 of 87 regmap votes agree: more than one decision differs.

### 6.6 `base + index*size`: an in-place chain or one `lea` of two temporaries (cl 13.10 .. 15.00)

`T* t = g_game->troops + a->troop` (element size 1288) reaches Color in one of three shapes, and the shape fixes the
pointer's references, its priority and whether it can be split. Probe `tests/data/rules/RA_addrsum.cpp`
(tests/probes/addrsum.py), the same on 13.10.3077, 13.10.3052, 14.00 and 15.00 **[C]** (traced IL at `@Globregs@4` /
`@Color@4`, and the listings):

| shape | when | IL at Globregs entry | code, `t` |
|---|---|---|---|
| **chain** | the sum is evaluated once | `t145 = [a+136]; t66 = t145*1288; t67 = t66 + [g_game+0x13a00]; t = t67` (the load is a memory operand of the add; 14.00/15.00 print it so, 13.10 loads it into a register first) | `imul r, 1288; add r, base`: `t` is defined three times in place (SubsumeGlobregs coalesces `t67` into `t`, then `t66` into it); 7 refs, prio 38 in a 3-web probe, 9 refs / 59 in M&B |
| **lea** | the same sum is evaluated twice in one region (no call or aliasing store between): one common subexpression with two users | `t64 = t147*1288; t148 = [g_game+0x13a00]; t135 = lea [t64+t148*1]; t = t135; ...[t135+0]...` | `imul ecx, 1288; mov eax, [g+0x13a00]; lea esi, [ecx+eax]`: the sum is a temporary `t135` with one def; `t` itself loses its references (a copy) |
| **base** | the *product* is shared (a named `off = i*1288` used again, or a store between the two evaluations kills the base load but not the product) | `off = t65; t145 = [g_game+0x13a00]; t67 = t145 + off; t = t67` | `mov edi, [g+0x13a00]; add edi, esi`: the base is loaded into `t`'s register, the product added |

What does not matter **[C]**: `p + i`, `&p[i]`, `(T*)((char*)p + i*size)`, a local base or index (copy propagation
removes them before GrgIdentifyCandidates), an unsigned or size_t index, a parameter index, the base a member or a
plain global, the assignment inside a condition, `t` used before the call and after it: all **chain**. The second
evaluation after a call (the loads are killed): **chain**. Two evaluations that both stay live (`Troop* u =
g_game->troops + a->troop;`, `g_game->troops[a->troop].flags` inside an expression, or `int f0 = ...troops[...].flags`
passed to a call): **lea**. The mechanism **[I]**: `dop2_Codegen` lowers an `OPADD` of a product and a load whose
result has one use (the copy to the variable) as the two-address `add` with the load as its memory operand, so the
result is the product's register; when the result tuple has a second user (the CSE temporary read again, usually as
the base of a load) it is emitted as a non-destructive `lea` of two registers instead and the base load stays a
separate `mov` (`dop2_Codegen` IL: `mov t148; lea t135`). SubsumeGlobregs is not what chooses: it only merges the
copies it finds (`mov t, t67` with a single def before it); in the lea shape the copy `t = t135` stays because
`t135` has another reader.

For a decompiler: **a `lea edi, [eax+edx]` with eax = `imul` and edx = a loaded base (`mov edx, [ecx+0x13a00]`) after
`mov ecx, [g]` means the address was computed in two places** (two accesses to `troops[i]` in the same region): the
source evaluates it twice. In Mount & Blade `Mission::spawn` the second evaluation is the entry test

```cpp
Troop* t = &g_game->troops[a->troop];
if (g_game->troops[a->troop].flags & 0x10) {     // was: if (t->flags & 0x10)
```

(the original's `t->flags` read is the same instruction, `mov eax, [edi+0x30]`). Then `t` has no web of its own: the
sum is the temporary (`t495` in the trace: 2 refs, prio -5, below `pm`, `found` and the hoisted `main_party` web): it is split around the
do-loop (`mov [esp+0x14], edi` / `mov edi, [esp+0x14]`) as in the original, `found` takes edi inside the loop, `this`
gets ebx and the g_game web ecx (3077: `this` ebx, g_game ecx; the `mov ebx, ecx` / `lea ebx, [ecx+0xf8]` pair of the
original appear). With the sum no longer owning `t`'s priority, hoisting `int mp = g_game->main_party;` (declared after
`PartyMgr& pm`, used as `pm.lookup(mp)`) now helps: `mov ebp, [ecx+0x78]` before the loop as in the original (before
the CSE spelling it was worse, 6.5). Mount & Blade checker, cl 13.10.3052: `chk.py` aligned 366 -> 284, size 1064 ->
1067 (the original's), `insn_align` identical 241 -> 261 of 288, changed 38 -> 20, and ignoring stack-slot numbers
changed 20 -> 4 (the scratch order of `ecx`/`edx` in the party lookup, and the capsule's float temporaries). The
declaration order of `mp` matters (aligned): `mp; n; pm` 328, `mp; pm; n` 334, `pm; mp; n` 296, `n; pm; mp` 284 (the
best; size 1067). **[C trace and checker]** The edited copy is in the Mount & Blade session's hands as a patch
(two lines: the condition and `mp`); the repository was not touched.

