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

## Structure causes of the closest DIFFs (2026-11-28)

The Spore-Decomp session sorted the 498 closest lifts of the full runs (bdiff <= 32; spore-decomp
work/claude/lift_structure_breakdown.md) by cause. What each turned out to be, and the rule now in the lifter:

- **Calling conventions at call sites** (#2).
  - The frame has one teardown: in `call f; add esp, 0xc; pop edi; pop esi; pop ebx; add esp, 0xc; ret 4` the first
    add is f's cleanup (f is cdecl), the second the frame's; also `add esp, N; mov esp, ebp` / `leave` at /O2 (Spore
    00656b80). The prologue walk-back from a ret takes one `add esp`.
  - A call whose stack arguments the caller pops by exactly their size before the next call / branch, with no earlier
    call's arguments still pending below them, is cdecl even when ecx was written before it: ecx is a leftover
    (`sub ecx, eax; and ecx, -2; cmp ecx, 2; jle; push eax; call free; add esp, 4`: Spore 0065d9f0, 0065d720, 006963d0,
    007ec660, 008d6be0, 009338e0, 009699d0). A merged cleanup counts the pending bytes too (`own + pending`).
  - A cleanup merged with later calls' (`push x3; call edx; push x3; call ecx; add esp, 0x18`) is a caller pop for
    the stack depth as well (all_caller_popped), so the slots read after the first call are the right ones (Spore
    007f7670 read a28..a44 for a4..a12). `call ecx` passes no `this`.
  - In an esp frame whose entry reserves a slot with `push ecx`, the epilogue's `pop ecx` is that slot, not a
    cleanup of the last call (`call [g]; mov ecx, eax; call; pop edi; ...; pop ecx; ret 0x10`: the stdcall callee pops
    its own: Spore 00c8f470).
  - ebp read from the entry (a catch / unwind funclet in its parent's frame, Spore 01211470 and 14 neighbours) is
    UNSUPPORTED with the LTCG custom-convention reason.
  - Not fixable here: `add esp, 4` where cl writes `pop ecx` for a 4-byte cleanup (Spore 011e3c50, cl 13.10: both
    compilers always choose `pop ecx`; the project notes it as /GL's peephole); `push edi; ..; call; pop edi; ret`
    and `push esi; mov esi, eax; call; pop esi; ret` (Spore 00a60e70, 00e78de0, 00a62f80..00a63060): the callee takes
    esi / edi (an LTCG custom convention), and these are also the #9 rows where retail has `call; ret` for our `jmp`;
    Spore 00830910 is /Od code under a corpus row that says /O2.
- **Calls through pointers** (the rest of #2). A call through a register loaded from memory, or through memory,
  that is no virtual call takes the convention the bytes show: the callee pops (`stdcall=1`) through a `__stdcall`
  pointer; `this` in ecx (`mov edx, [esi]; mov ecx, ebx; call edx`: a delegate or a pointer to member, Spore
  00dddb10; /Od 00545220) as `((C*)obj->*(*(PM*)&slot))(..)`, a pointer to member of a one-member class (4 bytes,
  single inheritance). `mov edx, [eax]; push ..; push eax; call [edx+K]` (the vtable of the first stack argument)
  and `call [ecx+K]` pass no `this` in ecx: a COM-style interface, __stdcall (Spore 011fd6f0, 011fd730).
- **Nested calls.** A cdecl call whose cleanup (the first `add esp` before the next call / push / branch) pops less
  than was pushed since argument setup began is nested in the next call's arguments: it takes only its own, the
  pushes above wait (`push eax; push 1; push K; push g; call f; add esp, 0xc; push eax; call h; add esp, 8` is
  `h(f(g, K, 1), x)`; the lifter had given f all four). Written nested (with the outer callee's load, and the inner
  one's, inside the call expression), cl pushes x before calling f as retail does (Spore 010527f0; nest_calls).
- **A scaled index after a shift.** `sar ebx, 2; lea eax, [eax + ebx*4]` (an element count scaled back: `p + n`)
  lifts the scale as `n * 4`: as `(n << 2)` cl folds the pair into `and ebx, -4; add` [M: probe, 15.00] (Spore
  00a9a7f0, 00eaeef0).
- **cl 13.10 rows built /G7** (a finding for the project's flags, not the lifter): Spore 01133020, 0113b9d0,
  0112da30, 0112da60 (`/O2 /MD /Gy /TP`, no /arch) match with /G7 added (`sub edi, 1` for `dec edi`, `add esi,
  esi` x2 for `shl esi, 2`); the /arch:SSE 13.10 rows tried do not change with it.
- **A virtual call on a member object.** `mov eax, [ecx+0x120]; mov edx, [eax+0x58]; add ecx, 0x120; call edx`: the
  vtable read through the owner's base before `this` is formed is `((V*)(p + 0x120))->f()`, written with the object
  as that expression at the call (vcall_member): a named `this` computed before a store that comes between is a lea
  first and the vtable read through it [M: probe, 15.00] (Spore 00971b60, 00971be0, 00c24430).
- **bool results** (#5).
  - /O2: in a bool function the byte variable it returns (and the byte copies it is returned through) is the source's
    `bool` when every assignment is 0 / 1, a compare, a byte load (`*(bool*)`), or a call's result used nowhere else
    (the callee then returns bool, so `bool b = f()` keeps al): `mov al, bl` instead of `test bl, bl; setne al`
    (bool_vars; Spore 006bd430, 00805310, 00806230, 00aeeda0, 00c5b210, 00ff5c50, 0112c640, 00675700, 0065bc20).
  - /Od [M: probes, 15.00]: a bool function returning a byte it loads returns a bool lvalue (`return *(bool*)&v;`:
    `mov al, [ebp-x]`, where `(bool)(unsigned char)v` costs `cmp; setne`: Spore 0045cd90, 0054efb0, 007fc690,
    007fc790); an int function's `return (x == 0)` compiled `neg eax; sbb eax, eax; add eax, 1` is `return !x;` (the
    compare spelled out is `test; sete; movzx`: Spore 005632d0); a temporary set to 1 / 0 in an if / else and returned
    through a cast is a `return` in each arm (od_return_merge looks through `(bool)`: Spore 007fd390).
  - Not done: a ret of a callee's tested al next to a bool variable's (`call; test al, al; jne L; ret; L: mov al, bl`,
    Spore 0069df90): every source form probed (`if (!f()) return false; return b;`, `r ? b : r`, an unsigned char
    function) compiles branch-free (`neg al; sbb al, al; and al, bl`).
- **Member address in the base's register** (#4). `mov ebx, [esi+0x18]; mov edi, [esi+0x1c]; add esi, 0x18` (an
  in-place add, the base's web ends there): the source read the member through its address (`v = &this->m; v->b;
  v->e`). Loads at the member's offsets in the straight run before the add (since the last call, past every other
  read of the base; also an operation's memory operand) read through the pointer, formed before them (member_base;
  Spore 006a2b20). The other direction (retail `lea eax, [esi+0x5a8]` into a new register where we `add esi`,
  Spore 00d943b0) is register allocation: the same text with the address inlined into its store compiles the same.
- **`sub r, K` / `add r, -K`** (#7). cl lowers a switch with a few sparse cases as `sub eax, K1; je L1; sub eax, K2;
  je L2` [M: probe, 15.00; the if-chain `x == K1 .. x == K1 + K2` is `cmp eax, K; jne`, the differences written out
  `sub eax, K; jne; add eax, -8`]. A chain of subs whose differences only the next sub reads (the lifter's reaching
  definitions; a void function's ret, a bool function's byte writes do not count), each tested against 0, is a
  `switch` on the first operand with cases K1, K1+K2, ..; a last `jne D` makes D the default and the fall-through the
  last case (sub_switch; Spore 00ea2670, 00a52840, 00816b90).
- **Frame size** (#10). A cdecl parameter copied to a temporary that is only passed as stack arguments, in a
  function with an address-taken local, passes the parameter itself: a dead cdecl parameter's home takes a local
  only when the parameter is read by something other than a call argument (frame.md rule 9), and the copy is such a
  read: cl put the local in the home (`sub esp, 0xc` for retail's 0x10, Spore 00f39130; param_args). Other frame
  sizes (00f2f5e0: 0x27d8 for 0x27e4) are arrays whose extent the code does not show.
- **fild for fld** (#13). A float stored over an int argument's slot and read back is the slot's bits
  (`*(float*)&a4`), not a conversion (Spore 00f1aa10).
- **Webs.** A call's ecx reached by several explicit writes (`lea ecx, [eax-4]` on one arm, `xor ecx, ecx` on the
  other) is one variable: the else arm's `this = 0` was dropped (Spore 00804f80).
- **Row extent** (#11, #12). Rows whose size runs into the next function with no padding between (Spore 0094d360,
  00dd1910, 00b204d0: `ret 4`, then another function's code up to its `ret 8`) cannot match (a /Gy object puts each
  function in its own section); the signature now takes the first ret's pop. The rest of #11 is retail code our
  structuring does not produce (tail-duplicated returns, Spore 0057c4b0, 00813370).
- **Measured** (9ba5742 -> 18439c4 -> the call rules above): the 498 rows 2 -> 56 -> 63 MATCH; staticbench `_v2`
  (refreshed 2026-11-28: Spore /Od 635 rows, /O2 1,500, M&B 254) Spore /Od 0 -> 7 -> 8, /O2 5 -> 20 -> 22, M&B
  0 -> 0, no MATCH lost; all new ones confirmed by chk.py (work/staticbench/new_spore_od_v2_3/4.txt,
  new_spore_o2_v2_2/3.txt, new_spore_498_1/2.txt).
- **Not addressed** (#1 independent loads / stores in another order, #3 a load on one side, #6 branch polarity, #8 a
  memory operand folded): the examples looked at are /Od frame layouts of inlined callees (0044c450, 0045b660,
  00548860), a redundant `test ecx, ecx` our code drops after a refcount release (0060d210), and a member read twice
  that our compile merges (00968c10); no source rule found for them yet.

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
