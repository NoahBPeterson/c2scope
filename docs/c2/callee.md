# c2 callee summaries: what a same-unit callee's body tells its callers

cl 14.00.50727.762 (`Bin/c2.dll`). When global optimisation is on, c2 compiles a callee before its callers and
keeps a summary of what the callee does to memory. Every caller in the same translation unit then uses the summary
at the call instead of assuming the worst. A draft that only *declares* a callee the original unit *defined*
therefore compiles its callers differently: sometimes visibly (a reload, a store, a tail call), sometimes only in
the registers. Fallout 3 0x00a89680 was the second kind (frontend.md, "Applied to 0x00a89680").

**[C]** = seen in a compile, a trace or the code. **[I]** = inferred. Probes are in
`work/scratch/calleesum/probes/` (run `uv run python work/scratch/calleesum/probe.py NAME`); each is one callee
body plus a caller whose code shows what c2 concluded: `int x = 5; callee(&x); return x;` ends in `mov eax, 5`
when c2 knows `*p` is not written, and loses `mov [x], 5` when it knows `*p` is not read either.

---

## 0. Summary for decompilers

1. **A callee defined in the unit is summarised; a callee only declared is not.** For a declared callee every
   pointer argument counts as read, written and kept, and every global as read and written. **[C]** `o03`, `n02`
2. **The summary is per parameter**: read through it, written through it, pointer kept (stored or handed on),
   pointer returned. `this` is parameter 0. Plus, for the whole callee: the globals it writes, the globals it reads,
   whether it stores or loads through a pointer of unknown origin, and whether it calls code without a summary.
   **[C]** section 1
3. **"Written" means any store through the parameter in the callee's IL after inlining and before optimisation**:
   conditional, through a copy, at any offset, volatile or not, by `memset`/struct assignment, even `*p = *p` or a
   store under `if (g != g)`. Granularity in the caller is the whole object the pointer points into, not a field.
   **[C]** section 2
4. **It is transitive through same-unit callees and stops at anything else.** Handing the pointer to a function
   with a body in the unit gives that function's verdict; handing it to an external function, a function pointer
   or a virtual call counts as read + written + kept. **[C]** `w04`, `w05`, `w06`, `v01`, `v02`
5. **Source order does not matter**: the callee may be defined after the caller. It exists at `/O1`, `/O2`, `/Ox`
   and `/Og`, not at `/Od`; `/Gy`, `/Ob0-2`, `inline`, templates and COMDAT placement do not change it. **[C]**
   section 3
6. **`__declspec(noalias)` on a bodiless declaration does not reproduce a body.** No declaration-only spelling
   (`const`, `__restrict`, `noalias`) made a pointer argument read-only for the caller. **[C]** `o04`, `o05`, and
   frontend.md's `decl_const`/`decl_restrict`
7. **A stub is enough.** A body with the same per-parameter pattern, kept out of line with
   `__declspec(noinline)`, gives c2 the same summary as the real body (28 of 28 checked field by field, section 5).
   `uv run tools/calleesum.py EXE VA --proto '...'` reads the pattern from the original executable and prints
   the stub. **[C]**

---

## 1. What c2 records, and where

**Recording [C].** `ItfBuildInterference` (10b73430, interf.obj), called from `dop2_InitialOptsAndSetup` (the
first interference build, after `Inline`, before `GlobalOptimize`), calls `ItfFlagParamsGoingGlobal` (10b827b0, at
+0x568) and then **`ItfSummarizeFuncInterf`** (10b7f5d0, at +0x574). The second allocates and fills the fields
below on the function's call-graph node (`SSR+0x88`), under `_BottomOpt`. A trace shows it running once per
function, for the callee before the caller's first `ItfBuildCallInterf`:

```sh
uv run tools/c2trace.py -H '@ItfSummarizeFuncInterf@12=g10d123ac>0>0>4$' \
   -H '10b83555=ebp>4$,ebp>0x88>0x3c>0#2,ebp>0x88>0x40>0#2,ebp>0x88>0x64>0#2,ebp>0x88>0x68>0#2,ebp>0x88>0x70,ebp>0x88>0x50,ebp>0x88>0x54' \
   work/scratch/calleesum/probes/p01_two.cpp -- /TP
#   0 > @ItfSummarizeFuncInterf@12 ... "?callee@@YAXPAH0@Z"
#   1 > @ItfBuildCallInterf@20+0x525 ... "?callee@@YAXPAH0@Z" ..0x3c>0#2=[0 2] ..0x40>0#2=[0 1] ..0x64>0#2=[0 3] ...
#   2 > @ItfSummarizeFuncInterf@12 ... "?caller@@YAHXZ"
```

(Output shortened. `void callee(int* p, int* q) { *q = *p; }`: each set is one node `[next, bits]`; written = 2 =
{q}, read = 1 = {p}, not escaping = 3 = {p, q}. An empty set prints `?`: the node pointer is null.)

**The summary (call-graph node, pointer at `SSR+0x88`) [C: values read in traces of about 40 callees; offsets read in
`ItfBuildCallInterf` 10b83030]:**

| field | content |
|---|---|
| `+0x3c` | bit set of parameter positions **written through** (`this` = 0; fastcall ecx = 0, edx = 1) |
| `+0x40` | bit set of parameter positions **read through** |
| `+0x64` | bit set of parameters whose pointer value **does not escape** (not stored to memory, not handed to a call without a summary). When the bit is clear, the caller treats the argument as "going global" (`ProcessOprGoingGlobal`, 10b83362) |
| `+0x68` | bit set of parameters **returned** (`return p`). Consulted only when the call's result is used (10b83314) |
| `+0x50`, `+0x54` | lists of the globals the callee **writes** / **reads** (symbol level) |
| `+0x70` | flags. bit0: the write side is exact; clear when the callee stores through a pointer that is neither a parameter nor a named variable (`**pp = 1`, `*gp = 1`), and then `+0x50` is dropped. bit1: the same for loads and `+0x54`. Values seen: `f` (all known), `e`, `d`, `c`, and **0 when the callee calls anything without a summary** |
| `+0x58`, `+0x5c` | read only when `_Function+0xac & 0x100`; always 0 in the probes. Meaning not established |

The split of bits 2 and 3 of `+0x70` was not isolated (they were only ever seen together) **[I]**.

**Use [C].** `ItfBuildCallInterf` (10b83030), called from `ComputeReachesAndAliases` for every call tuple, takes
the summary only when the call target is a known function symbol whose node has one (10b830b3..10b830fa). It then
calls `ItfBuildOneSideCallInterf` (10b838e0) twice: once with `TupAppendSrcSymSet` and the read side (`+0x40`,
`+0x54`, flag bit1), once with `TupAppendDstSymSet` and the write side (`+0x3c`, `+0x50`, flag bit0). That is why
a summarised call carries two points-at sets (`pas47`/`pas48` in an `il` dump) where an unsummarised call has
one. Without a summary it marks every pointer argument as going global (10b83376..10b833c0), or takes
`ItfBuildNoAliasCallInterf` (10b82f90) for a `noalias` callee.

---

## 2. Boundaries: what counts

Caller unless noted: `int x = 5; callee(&x); return x;`. "known" = `mov eax, 5` after the call.

**Writes through a pointer parameter [C]**

| callee body | probe | caller sees |
|---|---|---|
| `*p = 1;` | `w01` | written |
| `if (g) *p = 1;` | `w02` | written |
| `int* q = p; *q = 1;` | `w03` | written |
| `p[1] = 1;`, `p[g] = 1;`, `q = p + 1; q--; *q = 1;` | `w09`, `w10`, `h08` | written |
| `*(volatile int*)p = 1;` | `w07` | written |
| `memset(p, 0, g);`, `*p = gb;` (struct) | `h06`, `h10` | written |
| `*p = *p;`, `if (g != g) *p = 1;` | `k04`, `k01` | written: the summary predates optimisation |
| an inline helper that stores, inlined into the callee | `k03` | written: the summary postdates inlining |
| `inner(p);` with `inner` defined in the unit and storing | `w04` | written |
| `inner(p);` with `inner` defined in the unit and only loading | `w05` | **known** |
| `ext(p);` (no body in the unit) | `w06` | written, read and kept; flags 0 |
| `q = id(p); *q = 1;` or `g = *q;` (`id` returns its argument, body in the unit) | `h01`, `h04` | written (the returned pointer is not followed) |
| `*(int*)v = 1;` with an `int v` parameter | `h05` | written: the type of the parameter does not matter |
| `p->b = g;` while the caller reads `t.a` | `m01`, `m02`, `m04` | written: the whole local is one unit |
| `g = *p;`, `g = *(volatile int*)p;`, `memcpy(buf, p, n)`, `gb = *p;` | `r01`, `w08`, `h07`, `h09` | **known**, store before the call kept (it is read) |
| `g = (p != 0);`, nothing | `r03`, `r02` | **known**, and the store `x = 5` before the call is deleted (not read) |
| the same, plus a call to an external function that does not get `p` | `rd2` | the same: an unknown call does not touch a pointer it was not given |
| `*gp = 1; g = *p;` (`gp` an unrelated global pointer) | `h02` | **known**: a store through an unknown pointer is not a store through `p` |
| `*p = g;` (write only) | `d01`, `rd3` | written; the store before the call is kept |

**Per parameter [C].** `callee(int* p, int* q) { *q = *p; }` with `callee(&x, &y)`: `x` known, `y` reloaded
(`p01`). Passing the same local for both: reloaded (`p02`). `__fastcall`, `__stdcall`, thiscall, a hidden
struct-return pointer and reference parameters behave the same (`f01`, `f02`, `t05`, `f04`, `s01`, `s02`). The
unnamed arguments of a `...` function count as written (`f03`).

**Pointer to pointer [C].** The summary separates the pointee from what the pointee points to:

| callee (`int** pp`), caller `int x = 5; int* px = &x; callee(&px);` | probe | `px` | `x` |
|---|---|---|---|
| `g = **pp;` | `pp2` | known | known |
| `*pp = &g;` | `pp3` | reloaded | known |
| `**pp = g;` | `pp1`, `pp4` | known | reloaded |
| `ext(*pp);` | `pp5` | known | reloaded |
| `***ppp = g;` (one more level) | `pp6` | reloaded | reloaded |
| `*h->p = g;` (`struct H { int* p; int v; }`, caller reads `h.v`) | `pp8` | `h.v` known | reloaded |

Level one is the parameter's bit in `+0x3c`. Anything deeper is the single "stores through an unknown pointer"
flag, and the caller then assumes every local whose address has been stored somewhere may be written. This is
the 0x00a89680 shape: `QuadElement** p0` is read at level one (`*p0`); the elements it leads to are heap memory.

**Escape [C].** `gp = p;` in the callee writes and reads nothing through `p` (`e01`: `x` known), but clears the
parameter's bit in `+0x64`: a later call to anything unknown then reloads `x` (`e04`, `e06`), where after a
read-only callee `x` stays known even across `ext0()` (`e05`). Storing `p` into memory reached through another
parameter does the same (`e07`). `return p` sets `+0x68` (`c_ret` in the self-test) and matters when the caller
uses the result (`u03`, `u04`).

**Globals [C].** The summary names the globals the callee touches:

| callee | caller | probe | result |
|---|---|---|---|
| `g = g2;` | `g = 5; callee(); return g;` | `g01` | `g` reloaded |
| `g2 = g;` | same | `g02` | `mov eax, 5` |
| `g2 = 1;` | same | `g03`, `g09` | `mov eax, 5`, **and the call becomes a tail jump** |
| `g2 = 1;` | `g = 5; callee(); g = 7;` | `g05` | the first store to `g` is deleted |
| `ext0();` | first caller | `g04` | `g` reloaded |
| `inner();` (in the unit, writes `g`) / (writes only `g2`) | first caller | `g07` / `g08` | reloaded / known |
| `*gp = 1;` | first caller | `u01` | `g` reloaded |
| `*p = 1;` with `callee(&x)` | `g = 5; ...; return g + x;` | `g06` | `g` known: a store through a pointer to the caller's local is not a store to a global |

**`this` and other pointers the caller holds [C].** In `int S::caller() { m = 5; r = callee(); return m + r; }`
the caller's `this->m` is memory behind a pointer of unknown target, so it survives the call only if the callee
writes nothing the pointer could reach: known when the callee only reads (`t07`, `t08`), reloaded when the callee
writes any member (`t09`, `t11`) or any global (`t10`). The same holds for `*q` with `q` a pointer parameter of
the caller (`q01`..`q04`). A caller's global is reloaded when the callee stores through one of its own pointer
parameters (`q06`), but a caller's local is not, even if its address was given away earlier (`q05`).

**A call with no effects is deleted [C].** `__declspec(noinline) int* callee(int* p) { return p; }` with the
result unused: the caller contains no call (`e02`, `k02`).

---

## 3. When a summary exists

- **Order [C].** c2 compiles callee-first whenever global optimisation is on (il_pipeline.md §4.2), so the summary
  is there whether the callee is defined before or after the caller (`o01`, `o02`; the listing shows the callee
  first).
- **Recursion [C].** A self-recursive call has no summary (`y01`), but a recursive callee still gets one for its
  callers (`x01`, `x02`). In a cycle the function compiled first has none for the other: with `callee` defined
  first, `caller2` sees it (`x07`); with `caller2` defined first, it does not (`x08`). The listing's function order
  is the compile order.
- **Options [C].** Present at `/O1`, `/O2`, `/Ox`, `/Og`; absent at `/Od`. Identical with `/Gy`, with the Fallout 3
  flags `/O2 /MT /GS /EHsc /Gy`, with `/Ob0` and `/Ob1`. `/Ob` only decides whether the callee is inlined instead.
- **Linkage [C].** Used for external functions, `static` functions, `inline` functions, members defined in the
  class body and template instances (`x04`, `c01`, `c02`, `c03`): c2 trusts a COMDAT body.
- **No summary [C]:** a declaration without a body; a call through a function pointer or a virtual call through
  a pointer (`v01`, `v02`; a virtual call on a local object is direct and has one, `v03`); a callee compiled
  under `#pragma optimize("", off)` (`z01`); a callee containing `__asm` (`z02`); a caller compiled without `g`
  (`z03`). `try`/`catch` and destructors in the callee do not disturb it (`z04`, `z05`).
- **Inlining [C].** At `/O2` a small callee with no `noinline` is inlined even with a dozen external calls in it
  (`x06`); then there is no call and no summary question. A stub must therefore be `__declspec(noinline)`.
  `noinline` changes nothing else in the caller (0x00a89680 matches with it).
- **`noalias` [C].** With a body, the body decides (`x03`). On a bodiless declaration it did not make a pointer
  argument read-only (`o04`), and in two probes it did not keep a global or an escaped local known across the
  call either (`n01`, `n03`). It does change the caller relative to a plain declaration in 0x00a89680 (frontend.md:
  regdiff 16 with it, 80+ without); how was not examined here.
- **`static` callees [C].** They also get a private calling convention (`x04`: the argument arrives in eax). A
  stub of a `static` function changes that; use the real body.

---

## 4. What changes in the caller

1. **Reload after the call** of a local, global or member the callee is known not to write: gone, or replaced by
   a constant or a register copy (`r01`, `g02`, `t07`).
2. **Store before the call** to a local the callee does not read, or to a global it does not read when the
   caller overwrites it afterwards: deleted (`r02`, `g05`). A local's `lea`/`push` remains.
3. **Tail call** when nothing is live after the call any more (`g03`).
4. **Registers only.** A local whose address is passed and which the callee only reads stays one web across the
   call. `int x = a * 3; callee(&x); callee(&x); return x;` keeps `x` in esi with a read-only callee and reloads
   it from the stack with a writing one (`n04`, `n05`). In 0x00a89680 the instructions were identical and only
   the longer web's interference moved `center` and `this` between esi and ebp (frontend.md).
5. **Escape state.** After a callee that keeps the pointer, later unknown calls clobber the local; after one that
   does not, they do not (`e04`, `e05`).

---

## 5. `tools/calleesum.py`

```sh
uv run tools/calleesum.py EXE VA --proto 'void C::f(T** a, U* b, int n)'    # table + stub
uv run tools/calleesum.py EXE --caller VA                                     # where the rule can matter
uv run tools/calleesum.py EXE VA --trace --no-stub                            # the evidence, per instruction
```

It disassembles the callee from the original executable (capstone; no symbols needed) and follows each parameter
through registers, stack slots (spills, reloads, param homes reused for locals), pointer arithmetic, one or more
loads (`*p`, `**p`), calls and tail jumps. Conventions: thiscall, stdcall, cdecl, fastcall, from `--proto`,
`--conv`, or detected (ecx/edx read before written; `ret N`). Output:

- **Table**: per parameter `reads` / `writes` / `escapes` as `yes`, `no` or `unknown`, with the offsets, the
  call sites it is handed to, where the pointer is stored, and deeper access. Then the calls (which have no
  summary), the globals written and read, and stores/loads through pointers of unknown origin.
- **Stub**: a `__declspec(noinline)` body with the prototype given. Its lines map one to one onto the summary:

  | stub line | summary bit |
  |---|---|
  | `cs += *(char*)p;` | `+0x40` (read) |
  | `*(char*)p = (char)cs;` | `+0x3c` (written) |
  | `*(char*)p += 1;` | both |
  | `calleesum_keep = (void*)p;` | clears `+0x64` (escapes) |
  | `calleesum_pass((void*)p);` | read + written + escapes, flags 0 (handed to an unsummarised call) |
  | `**(char**)p = ...;` / `cs += **(char**)p;` | flag bit0 / bit1 (unknown-pointer store / load) |
  | `calleesum_ext(cs);` | flags 0 (the callee calls unsummarised code) |
  | `return (T)p;` | `+0x68` |

  The `(char*)` casts make the stub independent of the pointee type; a one-byte access sets the same bit as a
  full one **[C]** (`st1`..`st6`).
- **`--caller VA`**: every call in the caller that receives the address of one of its stack slots (pushed, or in
  ecx/edx), with the slot and, for a direct callee, that parameter's verdict.
- **`--tu LO-HI`**: treat direct callees in that VA range as bodies in the same unit and merge their summaries,
  as c2 does. Default: every call is unsummarised. The tool cannot know which functions shared a unit; when a
  parameter is handed to a direct call, run it both ways.

**Checks [C].**
- `work/scratch/calleesum/selftest/selftest.py`: 28 callees with known source (all four conventions, spills,
  loops, a switch table, tail calls, x87, struct copies, EH, a pointer kept in a local object): 28 of 28 tables
  as expected, conventions and parameter counts detected.
- `selftest/stubcheck.py`: for the same 28, c2's own summary fields (`+0x3c`, `+0x40`, `+0x64`, `+0x68`, `+0x70`)
  traced for the real bodies and for the generated stubs: 28 of 28 equal.
- Fallout 3 0x00a8a2c0: `this`, `p0`, `p5`, `center` read only; `mid`, `p2`, `p3`, `p4` read+written; two
  unsummarised calls. The stub appended to `work/scratch/f3/rw/e2.cpp` (the `noalias` declaration left as it is)
  gives **regdiff 0** and is byte-exact against the executable except for the three call displacements (435
  bytes). 0x00a89f40's stub alone leaves regdiff 16 (it writes through every pointer it is given, which is what the
  declaration already said); both stubs: 0. Files: `work/scratch/calleesum/f3/`.
- Robustness, 1,500 functions sampled from Fallout3.exe: no crash; 30 (2%) flagged unreliable; 124 of 3,932
  parameters (3%) `unknown`.

**Limits.**
- The tool reads optimised machine code; c2 summarises the IL before optimisation. An access the optimiser
  removed (`if (g != g) *p = 1`, a dead load) is in c2's summary and not in the code. The table can only say "no
  store survives". **[C]** for the direction (`k01`), not measured how often it bites.
- `unknown` is printed when a parameter's value was lost (merged with another value, used in arithmetic the tool
  does not model, kept in a local whose address a callee has) *and* the function stores or loads through
  pointers of unknown origin. The stub then assumes read+written and says so.
- An indirect call's stack cleanup is guessed (all pushes since the frame set-up or the last cleanup); if the
  function's `ret` then finds the stack unbalanced, caller-cleanup is tried instead, and failing that the result
  is marked `UNRELIABLE` (`--pop VA=BYTES` fixes it by hand). Variable `alloca` is not followed.
- ecx at an indirect call is reported as `this` of that call; it can be a stale register.
- A parameter spilled to a slot that stack packing later reuses for an address-taken local looks like "stored in
  a local object passed to a call". The tool treats a slot the function reloaded the parameter from as a spill
  home and ignores it, with a note (0x00a8a2c0 does exactly this with `this`).
- Globals: with no unsummarised call in the callee, c2 has exact lists. The stub cannot name the game's globals;
  it lists their addresses in a `NOTE` and the agent adds the accesses that concern the caller.
- Compiler helper calls (`__ftol2`, `_allmul`) look like unsummarised calls; name them with `--pure`.
  `__security_check_cookie` and `__chkstk` are recognised.
- Value parameters wider than 4 bytes are handled for `double`/`__int64` in `--proto`; a struct passed by value
  needs `--nparams`.

---

## 6. How a decompiler agent uses this

**Suspect it when** a draft has the original's instructions but different registers (or one extra/missing reload
of a local or global after a call, a missing store before a call, a `call` where the original has `jmp`), and the
function calls another function of the same source file, especially one that takes addresses of its locals,
`this`, or works on the same globals.

1. List the candidate calls:

   ```sh
   uv run tools/calleesum.py game/Fallout3.exe --caller 00a89680
   #   00a8976c  call 00a8a2c0
   #         arg0  = &local[entry esp-16]   callee #1: reads only        <- differs from a declaration
   #         arg1  = &param home[entry esp+4]   callee #2: reads+writes
   ```

   A parameter that is not `reads+writes, escapes` is where a body differs from a declaration. Indirect calls
   never have a summary; skip them.
2. For each such direct callee that plausibly lives in the same source file (same class, adjacent in the image),
   get the stub, giving the declaration exactly as the pack has it:

   ```sh
   uv run tools/calleesum.py game/Fallout3.exe 00a8a2c0 \
       --proto 'void QuadSplitter::FUN_00e0acb0(QuadElement** p0, QuadElement** mid, ..., QuadCenter* center)'
   ```

3. Paste the stub at the end of the caller's unit (after the caller is fine; an existing `noalias` on the
   declaration can stay) and compare again. If the callee is already decompiled, put its real body in the unit
   instead: the body is always at least as good as the stub.
4. If the callee hands a pointer parameter to another direct call, try `--tu LO-HI` covering that function too.
   If a row says `unknown`, try the stub both ways (delete the write line).
5. Read `--trace` when a verdict surprises: it shows, for every instruction, what each register holds.

**In the pack** (what a decompiler session needs up front): for every direct callee of the target that is in the
same source file, either its decompiled body or the calleesum stub, marked as a stub so nobody mistakes it for
the function; the `--caller` listing; and one line saying that bodiless declarations of same-file callees are
wrong by default. For callees in other files a plain declaration is right, and adding a body there is as wrong
as omitting one here.

**Do not** use a stub for a `static` callee (private calling convention, section 3), and do not leave the stub's
`calleesum_*` externs in a unit that is linked: they are compile-only.

---

## 7. Not verified

- Bits 2 and 3 of `+0x70` separately, and `+0x58`/`+0x5c`.
- What exactly `noalias` on a bodiless declaration changes (section 3).
- LTCG (`/GL`): the summary code has `Wpa*` branches; not probed (Fallout 3 is not LTCG).
- Whether a global's summary is per symbol or per field for struct globals.
- How often the pre-optimisation/post-optimisation gap (section 5, first limit) occurs in real code.
- The interaction with stack packing of param homes (frame.md rule 9 mentions the callee's mask): not probed.
- cl 13.10 and 15.00: nothing here was run on them.

---

## 8. Field results: Fallout 3 register near-misses

Run on 15 functions whose drafts differed from the original only or mostly by registers (fn3-test notes).
Details, units and commands: `work/scratch/f3/callee/RESULTS.md`. **[C]** = compiled here and compared with the
executable; nothing went through f3check.

| function | calls that get a local's address | body in the caller's unit? | regdiff before -> after | result |
|---|---|---|---|---|
| 0x00a89680 (435 B) | 0x00a8a2c0: reads `*p0` only | yes (same file) | 16 -> 0 | matched (frontend.md) **[C]** |
| 0x006dc990 (203 B) | 0x0049fac0: reads only | yes (template member) | 27 -> 0 | matched; stub equals the hand-written body **[C]** |
| 0x004209c0 (102 B) | 0x0049fac0: reads only | yes (template member) | 2 -> 0 | **new match**, with the variable itself as the argument and one helper call **[C]** |
| 0x004da900 (227 B, 51 give-ups) | 0x005c8b00 and 0x008e9650: both write, neither keeps | yes (template members) | 49 -> 20 -> 0 | **new match**; 20 -> 0 is one merged early return **[C]** |
| 0x00a692e0 (436 B) | 0x00901e90: writes, does not keep | no (another file) | 2 -> 115 with a stub | declaration is right; a body breaks the match **[C]** |
| 0x0054d850 (218 B) | 0x006db460: hands the pointer on | irrelevant | 27 | same as a declaration; open |
| 0x00ad1250 (125 B) | 0x004721c0: hands the pointer on | irrelevant | matched earlier | not applicable |
| 0x00902ea0 (194 B) | indirect call only | never a summary | matched earlier | not applicable |
| 0x004ddab0, 0x006bd9c0, 0x00422420 | none | | 14, 14, 9 | not applicable; open |
| 0x00570d50, 0x008338f0, 0x007eedf0, 0x0080a1f0 | none | | | not applicable |

Counts: a local's address is passed to a call in 8 of 15; the callee's body belongs in the unit in 4 of those 8;
the rule closed the register difference in 4 of 4 (two of them needed one further source change that only works
once the body is there); 1 of 8 is the opposite case, where adding a body is wrong.

What the field run added to the rule:

- **Escape state alone is enough (section 4, effect 5). [C]** In 0x004da900 both callees read and write the
  local `result`; a `--caller` listing that only looked for "reads only" would have skipped it. With both bodies
  the local does not escape, so the `InterlockedDecrement` and virtual calls after it do not clobber it, and the
  loads in the two arms, the return value and the destructor's pointer become one value in one callee-saved
  register (`this` then needs a third one, ebx). One body alone changes nothing: the other call still makes the
  local escape. So when several callees receive the same local, give all of them bodies.
- **Template and in-class members count as "same unit". [C for three callees]** 0x0049fac0 (list AddHead,
  `T* const&`), 0x008e9650 (smart-pointer assignment), 0x005c8b00 (map lookup) are 0.5 to 4 MB away from these
  callers, and their bodies are still in every caller's unit (COMDAT). Address distance says nothing for them.
  The reverse holds for a free function of another file (0x00901e90).
- **A read-only by-reference parameter lets the variable itself be the argument. [C]** In 0x004209c0 the draft
  passed a by-value copy to work around the bodiless declaration (113 bytes otherwise). With the body the
  variable stays in its register across the call and the copy is unnecessary; the instruction order then matched.
- **Image-wide (`work/scratch/f3/callee/census.tsv`) [C for the counts, tool limits apply]:** of 20,783 call
  sites in Fallout3.exe that pass a stack slot's address to a direct callee, the callee writes without keeping
  at 31.5%, only reads at 22.4%, and does what a declaration says at 27.3% (14.8% not analysable, 3.9% unknown).
  So a body would differ from a declaration at 54% of the sites; how many of those callees share the caller's unit
  is not known. 0x0049fac0 alone has 271 such sites in 161 callers.

Tool fixes from this run: ecx is no longer treated as an argument of an indirect call through an import slot
(it made 0x008e9650's `this` look kept); a parameter that only "escapes" by being returned is labelled
`returned (not kept)`; a stub for `T& f()` that returns `*this` or `*p` now returns it. selftest and stubcheck
still 28 of 28.

Not verified: the two new matches under f3check; whether c2's "returned" bit (`+0x68`) ever makes a local escape
when the caller keeps the result (here the result was dropped); the census rows against known sources.
