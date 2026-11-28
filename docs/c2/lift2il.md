# lift2il: retail x86 code to C++ that recompiles to it

`tools/lift2il.py` lifts a function's machine code to c2's IL at Lower's entry (the dialect `c2il.py` dumps);
`tools/il2c_lift.py` writes that IL as C++ through `il2c.py`'s emitter. Compiled at the binary's flags, the C++ should
give the original bytes back. 32-bit x86 only, MSVC 7.1 / 8 / 9 (c2 13.10, 14.00, 15.00).

```
il2c_lift.py lift BINARY VA --size N --c2 BUILD [--partial] [--outline]
```

- `--c2` names the build the binary came from: it decides the IL's float types (4004/4008 on 13.10, 5004/5008 later).
- `--partial` writes what lifts and a `/* lift2il: unsupported <what> at <va> */` comment at each construct it cannot
  lift (an EH shape it cannot write leaves the plain body, the EH states as comments): a draft of a big function, never
  a match.
- `--outline` prints one line per basic block (size, memory reads / writes, named calls, how it ends, loop heads, the
  EH frame) instead of C++: for functions too big to read as code.
- With a corpus (`corpus.py`'s JSONL: an image, rows of VA / size / relocation mask), `il2c_lift.py check` lifts,
  compiles and scores a sample (`lbench.py`'s scorer).
- `l2c_search.py CORPUS VA...` (or `--from CHECK.jsonl`, the DIFF rows of `il2c_lift.py check --json`) takes the
  near misses further: it compiles every single rewrite of the lifted C++ that leaves what it computes alone
  (adjacent independent statements swapped, a compare's or a commutative operator's operands exchanged, two
  declarations swapped, a temporary's signedness, a reference alias that shifts the symbol keys behind register
  ties: symkeys.md) and keeps the closest, for `--rounds N`. On 442 register / copy / schedule near misses it matched
  47; on Spore's 124 large near misses, 4. A variant is never proved equivalent: a MATCH is the original's bytes, a
  DIFF only the search produced is a lead.

## How it works

1. capstone, then the CFG; switch tables (`cmp r, N; ja D; [movzx r, byte [r+B]]; jmp [r*4+T]`) are found first, and
   the instruction list stops at the tables inside the function's bytes.
2. The stack: esp tracked from the entry, so `[esp+d]` and `[ebp+d]` name one frame slot (`_aN` an argument, `_lN` a
   local). The prologue (callee-saved pushes before any write, also shrink-wrapped ones past the first branch, frame
   setup, `push ecx` slots) leaves no tuples.
3. Register webs: reaching definitions over the CFG; one web is one variable. ecx read from the entry is `this`.
4. One IL tuple per instruction pattern; calls take the pushes and stores since argument setup began. The calling
   convention is inferred (the callee pops: `__stdcall` / `__thiscall`; ecx written for the call: `this`; cl merges
   cdecl cleanups across consecutive calls).
5. il2c_lift writes C++: control flow structured by il2c (if, if / else, do-while; other jumps stay `goto`), callees
   declared from their call sites, then clean-ups that matter to cl's register allocator (dead copies dropped,
   single-use temporaries inlined, address-only locals sized to their frame room).

Globals and callees are named by address (`g_<va>`, `FUN_<va>`), with the binary's relocations deciding which
constants are addresses.

## What lifts

| construct | as |
|---|---|
| if, if / else, else-if chains | il2c's structuring of forward branches |
| while, for, do-while | cl rotates them into a guard and a bottom test: `if (c) do { .. } while (c);` |
| break, continue, early return, && / \|\| | `goto` where the structuring rules do not place them |
| dense switch (jump table, byte-indexed two-level tables) | `switch (x) { case N: goto L; .. }` |
| sparse switch | cl's compare tree, lifted as if-chains |
| `c ? x : 0`, boolean results | `setcc`; `sbb` masks folded into selects |
| calls | cdecl / stdcall / fastcall / thiscall, virtual calls (`il2c_vc` stand-in classes), function pointers, imports, recursion |
| structs and classes passed by value | `*(S*)(base + off)` for a dword copy; a class with its copy constructor `FUN_<ctor>` |
| integer arithmetic | signed division by constants (magic numbers), `rep movsd` as memcpy, `rep stos` as memset |
| x87 | arithmetic incl. integer operands (`fimul` ..), `fsqrt` / `fabs` / `fsin` / `fcos` / `fpatan`, compares through `fnstsw` + `test ah` (incl. the parity forms, NaN-exact) and `fcomi` / `lahf`, float returns (typed double), `_ftol` as `(int)x`, values merging at joins |
| `__int64` (register pairs) | `add/adc`, `sub/sbb`, constant `shld`/`shrd` shifts and `and`/`or`/`xor` on a pair, as one `__int64` operation; the pair's inputs are a call's edx:eax (through `mov` copies), a memory pair (also each half loaded separately), `cdq` / zero, constants, or another such value; its consumers a pair store, the compare chain `cmp hi; jl/jg; cmp lo; jb` as one compare, an `__int64` return in edx:eax, or the halves split back out (`lo = (int)t`, `hi = (int)(t >> 32)`) for other readers. A fold is kept only when every reader of what it drops is accounted for (`find_i64`). Not joined: two halves of unrelated origin (cl compiles `(hi << 32) \| lo` as real code) |
| SSE | scalar float / double; on Spore, `cvtss2si`, the floor / ceil sequences and the `maxss` / `minss` clamps as the vetted helpers of spore-decomp `match/include/sse_helpers.h` (`SseRoundToInt` ..), and on cl 15.00 `fistp qword` as `X87RoundToInt64` |
| TLS | `__declspec(thread)` globals |
| C++ EH (`__CxxFrameHandler3`) | static initialisers, constructors / destructors (incl. polymorphic), RAII locals, `new` with a class `operator delete` (incl. placement), `try { } catch (...) { ..; throw; }` |

C++ EH needs C++ linkage (an `extern "C"` function is nothrow under /EHsc, so cl drops the states).

## What does not lift yet

- more than one `try` block, typed catches, EH states changed inside a branch or set from a variable;
- jump tables of other shapes (an index bias, computed jumps);
- `mul` with a 64-bit result (`__int64` products, the `new T[n]` overflow check `mul; seto; neg; or`), `__int64`
  values whose halves arrive from unrelated places (a 64-bit local split across registers), 8-byte arguments pushed
  from a register pair, `rdtsc`;
- x87 rounding-mode changes (`fnstcw` / `fldcw`), `lahf` on integer flags, packed SSE;
- member functions called from a C unit, `this` reassigned.

## /Od mode (`tools/il2c_od.py`)

Selected when a corpus row's flags contain `/Od` (`lbench.py`), or `il2c_lift.py unit CORPUS VA --od`. At /Od cl gives
every declared local a frame slot and reloads it for each statement, so the /O2 shape (a temporary per register value) adds
a store and a reload per temporary. The /Od emitter writes what a source file would have:

- **Folding.** A temporary with one definition and one read goes into that read when nothing between them writes a slot
  it names (any store / call when it reads memory) and no control flow lies between; a plain reload (`a4`, `this`, a
  constant, `&slot`) is repeated at every read (reaching definitions split one eax into its webs first); a result nobody
  reads loses its assignment (a call keeps the call). Consecutive stores of one constant are a chain (`c = b = a = 0`).
- **Parentheses.** cl evaluates `x = (e)` and `x = e` in different orders (the right side first with the parentheses), so
  the text carries only the parentheses precedence needs (`minparens`); `(int)` / `(float)` casts of values already of
  that type go too (a float load under `(float)` becomes an SSE convert).
- **Frame.** Named locals are allocated by a name hash, so the lifter's `v_lN` (slot at ebp-(N-4)) go into one packed
  struct local laid out at the original's offsets, the dead room (inlined callees' slots) as `int dead[]`; a block of 64
  bytes or more is 8-aligned, so a block whose size is 4 mod 8 is two structs, named `fb` (allocated first) and `fa`. Slots
  that overlap (a structure seen in pieces) are reached by byte offset from the struct. `this` is the member function's own
  home slot (the lifted unit is `D_<va>::FUN_<va>`, a thiscall method: a `__fastcall` free function would store edx),
  and slots deeper than it are compiler temporaries: an if / else assigning one becomes a `?:`.
- **Control flow.** `goto L; I: inc; L: if (c) { body; goto I; }` is `for (; c; inc)` and `L: if (c) { ..; goto L; }` is
  `while (c)` (a goto followed by its label costs an extra jmp); `if (A) { if (B) goto L; } body L:` is
  `if (!A || !B) { body }`; an arm that leaves by `goto` is an else; both arms ending in `t = e` before `return t` are a
  `return` each, and the last one is not wrapped in an else (cl emits a jmp for it).
- **Shapes.** Dword copies from one pointer to another, from a pointer to a local structure, and `memcpy` of a multiple of
  4 bytes are one structure assignment (`*(S3*)d = *(S3*)s`, `rep movsd` from 16 bytes); a virtual call evaluates its
  object twice (once for the vtable, once for `this`); under `/Ob1` an inlined `if (flags & 1) f(p)` with a constant flag
  and `xor eax, eax; cmp; setcc al` (an inlined bool compare) are `__forceinline` helpers.
- **Return value.** A value is returned only when eax is written right before the epilogue (`return e;`); a leftover eax
  is a void function. A callee whose pushed arguments the caller pops at once is cdecl, not thiscall (`and ecx, 1; je`
  leaves a dead ecx).

- **More control flow.** An arm's `goto` to a label that other arms also reach is an else (else-if chains), also when
  the label follows the ends of enclosing if / else arms; `if (A) { if (B) { s; goto L; } }` is `if (A && B)` with an
  else. Loops whose body has more back edges are `continue`, a goto to the label right after the loop `break`, and a
  body ending `if (c) { A } else { goto I; }` (the other way leaves the loop) is `if (c) { A; break; }`. A compare
  tree on cl's switch temporary (`mov [t], x; cmp [t], K; jg ..; je ..`) is a `switch` whose case labels sit at the
  targets and which closes at the label every other value reaches (the temporary's slot leaves the named frame).
- **Inlined callees' traces.** An operation on two constants was an inlined callee's parameter that c2 replaced by a
  constant (`mov eax, 31; cmp eax, 60`): the left one is written `il2c_k(31)` (an identity `__forceinline` template);
  `neg x; sbb r, r; neg r` is a bool store of `x != 0` (`il2c_nz(x)`); `x = x & ~(w << s) | (il2c_k(k) & w) << s` is
  an inline bitfield setter (`il2c_bf_S_N(&x, k)`: the field is computed before x is loaded). Shift counts are ints
  (an `(unsigned char)` count is a movzx).
- **By-value objects built in place.** `sub esp, N; mov [ebp-x], esp` and stores through the slot's reloads: an
  inline constructor's `this` building the argument in the outgoing area; written `il2c_bi_SIG(m0, m1, ..)`, a class
  with that `__forceinline` constructor and a declared copy constructor (without one cl builds a temporary and copies
  it); the constructor's two temporaries (its `this` and the slot above) leave the named frame.
- **Calls.** An entry ecx reaching a call is a leftover (/Od reloads `this` from its home for every call); a float
  call's result that goes straight to the shared epilogue is the return value.

What it does not do yet (Spore's 1,023 non-exact /Od rows <= 1500 B, 2026-11-27: 275 MATCH, 645 DIFF): inlined
callee bodies whose parameters cl spilled to temporaries (the deep slots below `this`'s home that are not `?:`
results or dropped postfix results, below: about 110 DIFF rows; an inline helper with the expressions as arguments reproduces the spill only for some
argument shapes), complex expressions that need a callee-saved register (`push esi` in an /Od frame, 27 rows), objects
built in place by nested inline constructors (11 UNSUPPORTED), x87 stack underflow, `sbb` masks other than the bool
store, EH frames (1 row of the 1,023 has one: not a volume class at /Od).

**Nested in-place copies** (`nested_inplace`, Spore 005240e0): a by-value argument built in the argument area by an
inline copy constructor whose member at +K has its own inline copy constructor. /Od shows the member's source as a
reference temporary (`lea r, [ebp+S+K]; mov [ebp-B], r`: the address of a subobject at a nonzero offset is an
expression, inline.md rule 13) and the member's `this` as another (`mov r, [ebp-A]; add r, K; mov [ebp-C], r`), its
floats copied by fld / fstp (an indirect source, even under /arch:SSE). The lift is one argument
`*(il2c_bvn_i_ff*)&p` with both copy constructors written out; the window's instructions are that copy. Rows whose
source is two adjacent locals rather than one object (00524150) lift but keep a frame DIFF.

**Postfix results dropped** (`od_postinc`, Spore 00540210). A slot below `this`'s home that is stored once and never
read, right before `X = X + 1` (or `- 1`) of a value it was computed from (`t = *(p+4) + 1; *(p+4) = *(p+4) + 1`), is an
inlined member's `return x++ op k;` whose result the caller drops:

- **What c2 makes of it [C: c2trace il at @WrnEmitWarnings, 15.00].** The inliner turns the callee's return into
  `t70 <- t69` with `t69 = [n] + 1`, and the increment follows. t70 is an IL temporary, not a symbol (rule 11's `sN`
  is a symbol: a return value that is a load or a nested inline call's result gets one, and /Od places those with the
  named locals, above `this`'s home).
- **Where /Od puts it [M: probes, identical on 13.10.3077, 14.00, 15.00].** Below the named locals and `this`'s home,
  in one downward sequence with the `?:` results, in evaluation order (`x ? a : b`, `q->D()`, `b ? a : x`, `q->P()`
  take -0x14, -0x18, -0x1c, -0x20 under a home at -0x10). In a free function the same sequence starts below the
  named locals.
- **What does not make one [M].** The same statement written directly (`q->n++ + 1;`, `(void)(...)`), `return ++n`,
  `return n = n + 1`, `return n += 1`, `return n + 1`, a dropped call result (`return ext(n)`): none stores a
  temporary. A named local in the inline (`int r = n++; return r;`) is a named slot, above `this`.
  `return n++;` (old value), `return n-- * 4` and `return (m = 2, n++)` do.
- **The lift.** The two statements become `((il2c_pi_K_T_H*)(B))->f();`, a packed helper with the value at +K and
  `__forceinline int f() { return x++ op k; }` (E with X replaced by `x++` / `x--`). E may read X once besides
  constants, and X's base B must be a variable, `this` or a constant (`il2c_k(0)` included): an expression would be
  the inlinee's `this` symbol, a named slot (inline.md rule 14). A member of the packed frame structure (`fb.v_l56`)
  is such an expression (Spore 004ca430 grew by 24 bytes), so the rewrite runs after the frame is placed and only
  where the base is a separate local. Only in member functions (where the lifter knows which slots are below
  `this`'s home); `L2C_OD_POSTINC=0` turns it off.
- **Measured** (staticbench `_v2` selections, e4a644c -> this change with the error fixes below): Spore /Od 76 -> 80
  MATCH, all four through this rewrite (004ad370, 00521ba0, 00540210, 00546f80; chk.py confirmed,
  work/staticbench/new_spore_od_v2_2.txt); Spore /O2 14 -> 22 (the prologue and x87 fixes below; new_spore_o2_v2_1.txt);
  M&B 1 -> 1; no MATCH lost.

## /O2 shapes (2026-11-28)

- **Calling conventions.** A call whose stack arguments the caller pops, even when the cleanup is scheduled a few
  instructions later (`call f; mov ebp, [edi+4]; add esp, 0xc`) or merged with later calls' (`add esp, 0x14` after two
  calls of 2 and 3 arguments), is cdecl: not a thiscall (a thiscall callee pops its own), and the stack depth after
  it counts the cleanup once. `add esp, 4; add esp, N` before the ret is this call's cleanup, then the frame's.
- **Return values.** `call f; ret` in a function whose other paths return a value returns f's result.
- **Constants.** `or r, -1` / `and r, 0` of a register nothing set (or a call clobbered) is the constant.
- **Loads.** movsx reads a signed char / short.
- **SSE compares.** comiss / ucomiss set the flags as fcomi does (unordered sets ZF, PF, CF): the branch takes the
  fcomi table, so `comiss a, b; jbe L` skips code that runs when `a > b`.
- **Out-structures.** A local whose address leaves and the named slots above it (to the next such local or the
  frame's top) are one array: as separate ints cl drops the stores it cannot see read.

- **Function addresses.** A relocated immediate whose target lies in an executable section is a function's
  address, not data: `&FUN_<va>` (declared `extern "C" void __cdecl FUN_<va>(...)` when no call declares it), or,
  for a callee known only as a member stand-in, the bits of a pointer to that member (`il2c_mfa(&C_x::FUN_x)`,
  single inheritance: 4 bytes). M&B passes constructors to `eh vector constructor iterator` this way (00524860).
  The bytes do not change (both forms are a dir32 fixup); the source reads as what it is.

Measured on the Spore /O2 sample: 51 -> 71 MATCH; M&B 0 -> 1; Spore /Od 275 -> 283 (the cdecl / constant rules apply
there too); no MATCH lost, the /O2 regression sets unchanged or better.

## Lifter errors of the full Spore runs (2026-11-27)

The full runs (`spore_all_od1`, `spore_all_o2a`, `spore_all_o2b`) had 52 EMIT-ERROR / CL-ERROR / TIMEOUT rows; by cause:

- **x87.** capstone spells `fcomip` / `fucomip` as `fcompi` / `fucompi`: they pop too (the stack was left one deep,
  then underflowed: most of the IndexErrors). A /MD binary's CRT math helpers are import thunks (`jmp [IAT]` to
  msvcr90's `_CIasin`): `crtfp.match` names them by the import. A call's st0 result is looked for past conditional
  branches on the fall-through path (`call f; test esi, esi; jle L; fsub [x]`).
- **Frames.** A callee-saved push popped in an epilogue that still calls (`pop esi; test ecx, ecx; je L; call edx;
  L: add esp, N; ret`) is a save, not an argument; the stack probe after `push ebp; mov ebp, esp` (/Od frames over a
  page) is frame setup and keeps ecx (it was a call that clobbered `this`); the bytes above the topmost dword local of
  an esp frame are `v_lmN` (were `v_l-N`); an argument slot no parameter starts at (`lea eax, [esp+0x25]`) is
  `(char*)&a36 + 1`; `mov edi, edi` (filler after a jmp) is nothing.
- **Row extent.** int3 padding after a ret / jmp that nothing branches past ends the function (rows whose size runs
  into an adjacent `jmp` thunk).
- **EH.** A destructor body that reads the function's slots is refused; the return value loaded after the
  destructor's call is the function's `return`.
- **Folds.** A temporary whose address is its one use (`f(&t)`) is not folded (`&0`).
- **Refused with a reason (UNSUPPORTED) instead of broken C++:** a register argument no standard convention passes
  (eax, ebx, esi or edi live from the entry and read: LTCG custom conventions such as Spore 0084cf20; a register a
  call leaves beside its modelled result, edx of an edx:eax return or eax beside st0, also reads as never written but
  stays a DIFF row); a slot above a realigned frame's top (`and esp, -16` at /Od /Oy); an EH state set inside a
  branch (a leftover `__EH`); in an EH function, esp's value stored (an object built in the argument area whose
  address a state keeps); a jump to the function's own entry (tail recursion as a loop); a callee taken for a
  by-value class's copy constructor that is also called plainly.
- **Time.** `rename_webs` keeps reaching definitions as bitsets, `od_fold` updates its counts per edited line,
  `drop_unused_decls` and `inline_x87` count once: Spore 00441440 (23 KB /Od) and 007864c0 (58 KB /O2), over lbench's
  60 s before, lift in 21 s and 17 s (00441440's unit is byte-identical with and without the speedups).

## Inlined callees at /O2 (`inline_sites`, opt-in: `L2C_INLINE=1`)

The project's byte-exact leaf callees (no call, no x87, 6..40 body instructions; `build_inline_db` over the corpus's
matched rows, cached in work/cache/inlinedb_<corpus>.json) are shapes (registers and stack slots wildcarded, member
offsets kept: invert/inlining.py's `detect`). A region of the lifted function with a callee's shape is replaced by
one `OPINLINE` tuple: the callee lifted on its own becomes `__forceinline il2c_inl_<va>(..)` defined before the
caller, its inputs (stack arguments, `this`) bound to the caller's registers at the region by aligning the two
instruction lists, its result to the register its last eax write maps to (when the caller reads it). Regions whose
other values the caller reads afterwards stay written out. On the Spore /O2 selection 31 of 1,500 rows have such a
region (mostly 12- and 16-byte copies: `SetKey`, `hkVector4` copies); re-inlined they compile to the same bytes as the
written-out lift on 29 rows (no MATCH gained or lost; 2 rows change by a few bytes), so it stays off by default.

## Measuring (`tools/staticbench.py`)

`staticbench.py run` lifts each project's real non-exact functions (fixed selections in work/staticbench/sel_*.txt:
Spore's /Od rows <= 1500 B, a 1,500-row sample of Spore's /O2 rows, Mount & Blade's rows <= 1500 B; `refresh`
rebuilds them as the `*_v2` selections from the projects' current state: Spore's nonmatching.txt rows under match/
and src/game/ minus every manifest VA, M&B's status.py `equiv` tier; `run` defaults to the `_v2` ones, and `compare`
of an old run against a `_v2` run restricts both to the rows they share) with the plain
lifter (lbench --task lift2il) and appends the counts, DIFF classes, UNSUPPORTED reasons, MATCH set and a hash of
each emitted unit to work/staticbench/history.jsonl; `compare A B` lists MATCHes gained and lost and the rows whose
C++ changed; `verify A B spore_od` checks B's new MATCHes with spore-decomp's chk.py.

## What a match is, and what it is not

A MATCH is byte-identical code, relocations aside, from source with placeholder names and types. Many more functions
lift and compile but differ (DIFF), mostly in register allocation, instruction scheduling or frame layout; the c2scope
tools (`c2trace.py regs`, `slots`, `schedwant.py`, `slotwant.py`) explain and search those differences. On functions
over a few KB, read the `--outline` or the `--partial` draft for structure (one base pointer, which copies, the block
order) rather than as code to keep.
