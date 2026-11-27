# x87round: where an x87 value is rounded, on our side and the original's, and why cl made ours

`tools/x87round.py` (tests: `tests/probes/x87round.py`, probe source `tests/data/x87round.cpp`). Built for the Mount & Blade
rows that differ in WHERE an x87 value is rounded: one build stores it (`fstp dword [m]` ... `fld dword [m]`: float32 / float64)
where the other keeps it in a register at 80 bits. That is a behaviour difference, so such rows need a source fix, not an
equivalence proof. A whole-function multiset of `fstp`/`fld dword` cannot say which value is rounded, so it misclassifies rows
(11 of 25 in one lane); this tool pairs the points by dataflow position.

Why a new tool and not a mode of `c2x87.py`: c2x87 traces c2's own x87 decisions for rasim's x87 model (14.00 / 15.00 only, one
function, a snapshot format); this one works on finished code of both sides and on every build (13.10 has no X87MapGFxToSTx),
and only borrows c2x87's call-kill hook where the build has it.

```
uv run tools/x87round.py SRC --func F (--asm-vs ORIG.s | --image EXE --va VA) [--asm-func DECORATED] [--all] [--show-il]
                         [--no-trace] [--keep DIR] [--source FILE] [-- <cl args>]
uv run tools/x87round.py --orig-only (--asm-vs ORIG.s | --image EXE --va VA)       # the original's points, no compile
```

`--asm-vs` is the usual 'VA mnemonic operands' file; `--image/--va` disassemble the function by recursive descent
(c2slots.read_orig). Without `--no-trace` our side is a traced compile (compile server; 20-45 s): the final IL gives the
home symbol of each store (`--show-il`), and on 14.00 / 15.00 the call kill sets come from `LoAddCallScratchRegs` (c2x87's hook).
`--no-trace` is a plain `cl /FAsc` compile: same points and reasons, no IL line, no kill sets (13.10 has none either).

## 1. What a rounding point is

| kind | pattern | note |
|---|---|---|
| `rt` | a dword / qword store of an x87 value whose location is reloaded later (`fstp dword [m]` ... `fld dword [m]`) | the value came back rounded; a store nothing reloads is an ordinary output and not listed |
| `arg` | a dword / qword store into the argument area (`fstp [esp]`, with or without `push ecx` / `sub esp`) that is followed by a call and never reloaded | the argument is rounded to its parameter type |
| returned | an `rt` whose reload is what `ret` leaves in ST(0) (`fstp tv; fld tv; ret`) | printed as `(the returned value)` on the point |

Tbyte (`fstp xword`) spills are not rounding and are ignored.

## 2. How the two sides are paired (dataflow, not counts)

Both sides run through the same symbolic x87 stack: frame slots (esp- and ebp-relative, with the esp delta tracked through
push / pop / `sub esp` / `and esp` and calls: a cdecl call is followed by `add esp`, otherwise the callee is taken to pop) and
register+offset addresses (with register versions) are the memory model. Every value gets a KEY, the expression that makes it
with round trips removed: `fmul(fsub(m,m),m)`; loads are `m`, constants `k`, integer conversions `cv`, unknown (before the code,
a call's result) `u`; `fadd`/`fmul` operands sorted; three levels deep. The two sides' value lists (every operation, plus any
rounded load) are aligned by key sequence (difflib) and a paired value's rounding is compared:

* `BOTH` rounded (shown with `--all`),
* `OURS ONLY`: ours rounds it, the original keeps it on the x87 stack ("ours rounds `r = p - fr.o` before the mul; the original
  keeps it on the stack"),
* `ORIGINAL ONLY`: the original rounds it and we do not (the source line printed is the line that makes the value in ours),
* `UNPAIRED`: no counterpart with that key (another association or a folded constant: the expression itself differs).

The straight-line walk follows fall-through order; a forward jump target takes the stack of the first jump to it when the
previous instruction cannot fall through. Loop-carried values come in as `u`.

## 3. The reason classes (our points only)

Read = taken from c2 (trace or the documented decision); inferred = from the listing and the source.

| class | determined by | evidence printed |
|---|---|---|
| `call between def and use` | a `call` between the store and its reload. 14.00 / 15.00: the call's kill set from `LoAddCallScratchRegs` (read; x87.md 12.2 rule 1: a call kills GF0..GF7 unless its callee is x87-free, `MscRecordInterProcRegInfo`); a callee that kills nothing is shown and is NOT blamed. 13.10: call kill sets are not traced (any call is taken to kill; `RX_x87free` measures the x87-free exception on 13.10) | the call operands |
| `argument` | an `arg` point, or an `rt` temp whose reload is stored into the argument area (the OPCONVERT to the parameter's type is stored to a `tv` first) | the call |
| `return` | the point is the returned value and its home is a compiler temp (`fstp tv; fld tv; ret`; x87.md 12.2 rule 7 / rule 13: a float return of a computed value) | the temp |
| `float parameter of an inlined helper` | the home is a `$T<n>` argument temporary of an inlined call (listing symbol, the inliner's parameter temp), or a named variable declared outside the function's span in the source | the line |
| `spill under x87 pressure` | the stack is 7 or more deep at the store (live globregs + temporaries over 8 slots: `InsertSplitPoints` K = 8 / `RegSpill`, x87.md 5, 12.1). The depth is counted by the simulation | the depth and the home |
| `cast to float` | the home is a compiler temp (`tv`) and the source line has an explicit `(float)` / `float(` / `static_cast<float>` | the line |
| `volatile / address-taken` | the variable is declared `volatile` or `&x` occurs in the function in the source | the declaration |
| `float local` / `float parameter` | a named home (`_x$`) declared in the function as `float` / `double`: Lower stores an assigned float to its home and Globregs reloads it (x87.md 12.1 "rounding to float", 12.2 rule 7: read in the decompile, [C] on 14.00 by IL at Color) | the declaration line |
| `float member of an aggregate local/parameter` | a named home whose declared type is not a scalar (`vector4 r`): a per-field float store of a converted double-typed expression | the declaration; the IL line shows the narrowing `ST(0):4008 <- ...:4004` (inferred cause: the field is not promoted to a register) |
| `float temporary` | a `tv` home, no call between, not deep, not a cast: a float-typed expression rounded through its temp, or an inlined helper's float result (x87.md 12.2 rule 7 `r_inl`) | the line |

Order: argument, return, call, inlined-helper parameter, pressure, cast, then the named-variable classes. When a call lies
between a named float local's store and reload the class is the call and the evidence mentions the variable (cl would have
rounded neither without the call on 13.10; on 14.00 it would have, so both causes hold: remove either, the count stays).

What is read and what is inferred: the SYMBOL KIND of the home (`_x$` user variable, `tvN` temporary without a defining tuple,
`$Tn` inline argument temp: `SymName`, il_sym.md section 2) comes from the listing and the final IL at `@Encode@4`; the kill
sets of calls (14.00 / 15.00) and the decision that a float assignment becomes a store to the home (`Lower`, `OPCONVERT 5008 ->
5004`) are c2's, documented in x87.md section 12; which class applies is the tool's inference from those plus the source text.
The pressure class does not read RegSpill's events (not hooked): it counts the simulated depth.

## 4. Builds

14.00 / 15.00 round every float assignment that survives to Globregs (the table in `tests/probes/x87round.py`: f_local,
f_inl, f_cast, f_ret, f_arg all show points). 13.10 (both builds) rounds a named float/double scalar only across a call, when
volatile, or under stack pressure (x87.md 12.2 rule 7; the same probes), but does store a converted double expression into the
float fields of a struct local (Mount & Blade 0x42b080: `vector4 r`). The probe checks the per-build table.
`/fp:fast` (14.00 / 15.00) removes the float-home round trips; the report ends with that hint when it applies.

## 5. Mount & Blade examples (13.10.3077, flags `/O2 /MT /Gy /TP`, original = `work/mb.analysis.bin`)

0x42b080 `strategic_entity::create_shadow_mesh` (traced, `--show-il`):

```
# x87 rounding points: ours 13 (rt 13, ...), original 12 (rt 12, ...)
# paired by dataflow position: 8 rounded on both sides, 3 ours only, 2 original only, 4 unpaired
OURS ONLY  value fsub(fadd(fmul(..,m),m),m)
  ours     produced at line 361 `vector4 r = p - fr.o;`: fsub DWORD PTR _fr$[esp+352]
    rt ours 4-byte DWORD PTR _r$93044[esp+304] ... consumed by fmul
      IL: s211:4004 ST(0):4008 <- ST(0)=t1274:4004 fcw:9002
      why: float member of an aggregate local -- `r` (type vector4, line 361) ...
  original produced at 42b367: fsub dword ptr [esp + 0xd0]
    kept on the x87 stack
```
(three such rows: r.x, r.y, r.z; plus `inv` / `ik` rounded by ours and the products at 42b3d2 / 42b3e4 / 42b3ee rounded by the
original only.)

0x48a420 `Mission::get_frame`: three `ORIGINAL ONLY` rows, `m.o = frame_b.o * t + ...`, `m.f = ...`, `m.u = ...` (the original rounds
the `frame_b.* * t` products at `[esp+0x2c]`, `[esp+0x1c]`, `[esp+0x1c]`, ours keeps them): ours 15 rt points, the original 18.

0x4650f0 `Terrain::scatter` (the source of the commit before the fix, git 755896f3^): two `OURS ONLY` rows, both
`call between def and use ... __aullshr` (`rr` and the tv of the `it->rate` expression); after the fix the sides agree (7 rounded on both).

## 6. Limits

* The source line printed for a point is the nearest preceding `; N : text` comment of our listing: after scheduling it can be
  a neighbouring statement (the declaration line printed in the evidence is exact).
* Pairing is by key sequence: when association differs the rows are `UNPAIRED`, not paired; a value whose key collides with another
  (`fmul(m,m)` is common) is paired in order.
* The kill-set binding matches calls by the callee's name in the listing; a call the trace does not name is reported without it.
* Branch merges are not modelled beyond the snapshot rule; a rounding that only exists on one path is still found, a stack that
  differs between paths can produce `u` values.
* Calls through `__fastcall`/register conventions that pop nothing are fine; a callee that pops more than was pushed after the
  prologue shifts the esp delta (frame keys after it can fail to match a store with its reload: such a point is missed, not invented).
