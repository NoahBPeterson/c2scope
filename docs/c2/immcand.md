# Constants as register candidates: when a constant counts in a block's pressure P

A constant operand can be a register candidate (an *immediate candidate*: a globreg whose symbol has kind 0xd,
`imm` in `c2trace.py regs`). Most of them end up without a register and leave no code, but while Spillcosts
runs they are globregs like any other: every block that **references** one has it in its pressure P, and P
multiplies the priority of every other value referenced in that block (regalloc.md §3.2). This file says which
constants become candidates, in which blocks they are referenced, and what in the source changes that.

Tags: **[C]** read in the code at the address given (14.00.50727.762 unless a 13.10.3077 address is named) or
seen in a trace; **[I]** inferred. Every rule below was probed on 13.10.3077, 14.00 and 15.00.30729; the probe
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

That the field at `BLOCK+0x16c` means "in a loop" is **[I]** from the probes; the test itself is **[C]**.

### 2.4 The region, and where the load is placed

The dataflow is the one used for memory variables (`ReachingOccurGenKill`, `GrgPropagateSetsBackwards` 10b57fd0,
`GrgPropagateSetsForwards` 10b57da0, then the per-block sets are intersected in `Globregs`). Nothing kills a
constant, so the result is: the candidate is "in a register" at a point when some path from an earlier use
reaches the point **and** a use follows it. **[I for this reading of the sets; C for every probe below]**

`Enterloads` 10b588e0 then walks each block; a candidate operand met while the candidate is not in the block's
"in register" set gets an `OPENTERLOAD K` inserted before that instruction (`CmdInsertEnter`, 10b58c06). **[C]**

`AvoidAirlocks` 10b581f0 runs before it and is what moves loads upward. For a block B and a forward edge from a
predecessor that does not have a candidate which B expects in a register, the candidate would need a load on the
edge (an "airlock" block). Instead, for an **immediate, unconditionally** (the kind-0xd test at 10b58358 skips
`CanExtendLiveness` 10b58365), the load is inserted in the block `GrgFindNearestDominator` 10b5c0c0 returns (10b582e1), at
`GetEnterInsertPoint` 10b58500 (10b58435; before the block's ending branch; `CmdInsertEnter` 10b58442), and the candidate is made live from there
(`DoExtendLiveness`). **[C for the pass and the unconditional case; I that the dominator is taken over B's
predecessors]**

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
  b9 and u15 (the same constant once in each arm, or in two exits) end with no candidate at all **[C trace]**;
  that this is the path that removes them is **[I]**.
- `CreateGlobregs` 10b5abe0 (test at 10b5ac15) removes the globreg of every immediate symbol whose candidate
  record has `+0x14 < 1`. **[C for the test; I for what the counter counts: lifetimes that survived]**

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
- /O1: `_Speed` == 0 makes every once-only constant a non-candidate even in loops (the code at 10b57827); not
  probed.
- 64-bit constants: only the low dword is compared at 10b56731; not probed.
