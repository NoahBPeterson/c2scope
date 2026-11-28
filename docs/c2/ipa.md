# Dead stores before a call: when c2 drops them because the callee's body is in the unit

Question (Mount & Blade 0x004b9aa0, cl 13.10.3077, `/O2 /MT /Gy /TP`): a function zeroes the fields of a local
struct and passes its address to `http_fetch`. With `http_fetch` defined in the same .cpp, cl drops the stores
the callee never reads; the original keeps them. Is the callee's body in the unit the only condition?

**[C]** = seen in a compile, a trace or an IL dump here. **[I]** = inferred. Probes: `tests/data/icount_ipa.cpp`
(run by `tests/probes/icount_ipa.py`), plus the throw-away sets described in section 3 (`work/ipa/v2.cpp`,
`v3.cpp`: counting the stores before the call in the listing). The mechanism is the callee summary of
[callee.md](callee.md); this note adds the dead-store consequence, the per-build check, the `noinline` answer and the
tool hint.

## 1. The rule

1. **A store to an address-taken local before a call is deleted by `GdfRemoveDeadStores` when the call's IL tuple
   says the callee does not read the local, and nothing after the call reads the stored field.** The local is
   split into one symbol per stored offset (`s2 s4 s6 s7` for `R { char* url; char* headers; char* body; int
   len; }`), so a field no later code reads goes even when the callee *writes* the struct (a write is only
   "may write": it does not make the earlier store dead, and the later read keeps what it needs). A field that is
   read after the call stays (M&B: `reply.body`). **[C]** all four builds.
2. **What the callee says is its summary, not its body.** c2 compiles the callee first (callee-first order,
   `_BottomOpt`, il_pipeline.md 4.2); `ItfSummarizeFuncInterf` records on the callee's call-graph node (`SSR+0x88`)
   which pointer parameters it reads through, writes through, lets escape, and which globals it reads or writes
   (callee.md section 1). `ItfBuildCallInterf` -> `ItfBuildOneSideCallInterf` (once with the read side, once with the
   write side) turns that into the call tuple's two points-at-set lists. A callee with no summary (declaration only,
   function pointer, virtual call through a pointer, `#pragma optimize("", off)`, `__asm`, `/Od` unit) is "reads and
   writes everything the pointer reaches", so the stores stay. **[C]** v2 and v3 sets, every build.
3. **The read bit is per parameter, not per field.** Reading any field through the pointer (`o->url`, `q[-1]`, a
   loop over `((int*)o)[i]`) keeps every store to the local. Handing the pointer on to a function without a
   summary (an extern) counts as read; handing it to another in-unit function takes that function's verdict
   (transitive). Storing the pointer to a global (`g = o`) reads nothing: the stores still go, but later unknown
   calls then see the local as escaped. **[C]**
4. **The caller side can veto.** If the caller itself stores `&local` to a global before the call, the callee can
   read it without a parameter, and the stores stay (a callee that reads `g_keep->url`: kept). **[C]** `t_gp_r`,
   `t_p_noread` in v3.

### Conditions, one by one (stores before the call out of 4; "0" = all dropped)

All rows are identical on 13.10.3077, 13.10.3052, 14.00.50727.762 and 15.00.30729.1. **[C]**

| callee | stores kept | note |
|---|---|---|
| declared only (`extern`) | 4 | no summary |
| defined in the unit before the caller, writes `*out`, never reads (`__declspec(noinline)`) | 0 | the M&B case |
| the same, defined **after** the caller | 0 | compile order is callee first whatever the source order |
| `static`, not `noinline`, `/Ob0` (otherwise inlined) | 0 | |
| `inline` keyword, `/Ob0` | 0 | |
| non-static external-linkage body | 0 | COMDAT bodies are trusted (callee.md section 3) |
| **`__declspec(noinline)`** | 0 | **noinline keeps the summary** (section 2) |
| callee under `#pragma optimize("", off)` | 4 | no summary |
| callee reads a field through the parameter, or `q[-1]`, or loops over it | 4 | per-parameter read bit |
| callee reads only through a global pointer, and the caller set it to `&local` | 4 | the caller's escape |
| callee returns the pointer (`return (int)o`), nothing read | 0 | |
| callee hands the pointer to an extern (`ext(o)`) | 4 | extern = read + written + kept |
| callee hands it to an in-unit `noinline` function that only writes | 0 | transitive |
| callee hands it to an in-unit function that reads | 4 | transitive |
| call through a function pointer | 4 | no summary |
| virtual call through a pointer | 4 | no summary (a virtual call on a local object is direct and has one, callee.md) |

## 2. `__declspec(noinline)` keeps the information

`noinline` only stops `Inline` from substituting the body. The summary is recorded in `dop2_InitialOptsAndSetup`
(`ItfBuildInterference` -> `ItfSummarizeFuncInterf`) for the callee as a function of its own, so a `noinline` body
is summarised exactly like any other, and the callers see it. It is the way to reproduce the information while
keeping a real call (callee.md section 0 item 7: a `noinline` stub gives the same summary as the real body). At
`/O2` a small callee without `noinline` is inlined instead, and then there is no call at all: the stores are
dropped (or not) as any local stores would be; that is not the summary at work. **[C]**

Trace (14.00; the hooks of callee.md section 1 on `tests/data/icount_ipa.cpp`; node = `[next, bit set]`, `?` = empty):

```
@ItfSummarizeFuncInterf  "?in_fetch@@YAHPBDHPAUR@@@Z"     (before the callers: callee first)
@ItfSummarizeFuncInterf  "?in_read@..."   @ItfSummarizeFuncInterf  "?in_pass@..."
@ItfBuildCallInterf+0x525 in_fetch:  +0x3c written [0 5]  +0x40 read [? ?]  +0x64 not-escaping [0 4]  +0x70 flags f
@ItfBuildCallInterf+0x525 in_read :  written [0 4]  read [0 4]  not-escaping [0 5]  flags f
@ItfBuildCallInterf+0x525 in_pass :  written [0 4]  read [0 4]  not-escaping [0 1]  flags 0   (hands `out` to an extern)
```

`in_fetch`: the read set is empty, so the caller's stores to the local are not read by the call. `in_pass`: the
pointer escapes and the flags are 0 (it calls code without a summary). Bit 0 of the written set of `in_fetch` (5 =
{param 0, param 2}) is not explained **[I]**: `out` is parameter 2.

The same function names exist on every build (indices: `@ItfSummarizeFuncInterf@12` 13.10.3077 10765fec,
13.10.3052 107a22af, 14.00 10b7ce20; `?ItfSummarizeFuncInterf@@YIXPAUtagFUNC@@II@Z` 15.00 510c6e40;
`ItfBuildCallInterf` 1076963c / 107a7dc1 / 10b80720 / 510ca820; `GdfRemoveDeadStores` 10760a71 / 1076e0e0 /
10b358e0 / 51083640). The field offsets (`+0x3c` .. `+0x70`) were read on 14.00 only; on the other three the
behaviour is identical (every row above) and the IL shape below is the same. The 13.10 `ItfBuildCallInterf` is laid
out differently (the node is not reached the way 14.00's code does), so its offsets are **unconfirmed**.

### What the dump shows (all four builds) **[C]**

At the entry of `GdfRemoveDeadStores` the call tuple is
`OPCALL | t64 pas14 pas13 pas12 <- fn args` (summarised, callee reads nothing: written sets on the
destination side, none among the sources) against `OPCALL | t64 pas8 <- fn args pas8` (no summary, or a callee
that reads: the same set on both sides). The M&B call, with `http_fetch` reading globals and calling externs but
never the struct: `t64 pas5 pas12 <- http_fetch args pas5` (pas12, the struct, is written and not read; pas5, the
globals, is both). That asymmetry (a written set that is not among the read sets) is what `tools/icount.py`
tests.

## 3. The Mount & Blade row **[C]**

`mountandblade-decomp/src/game/config/launcher_options_dialog.cpp` (copied to `work/ipa/mb/lod.cpp`; the
original's instructions from `work/mb.analysis.bin`): `get_latest_version` 0x4b9aa0, 13.10.3077.

| variant | ours / original instructions | icount |
|---|---|---|
| `http_fetch` defined in the unit (their source) | 21 / 24 | `removed-by:DeadStores:callee-summary` on the two `mov [esp+0x18/0x1c], eax` (the chained `url = headers = body = 0`: `s5 <- s4`, `s6 <- s5`) |
| `http_fetch` definition cut out (declaration only, `lod_ext.cpp`) | **24 / 24, 0 runs** | none: the three stores are back and the function has the original's instruction sequence |

So the callee being in the unit is the cause, and the original's `http_fetch` was in another unit (or a summary-less
function). `http_fetch` in their source writes `out->url / headers / body` and reads none of them, and calls only
externs with other pointers, so its summary reads nothing through `out`.

## 4. Where a decompilation can go wrong, and the lever

- The original keeps the stores: either the callee is in another translation unit (declare it; do not put the body
  in this .cpp), or its body reads the object, or hands the pointer to unsummarised code, or is compiled with
  `#pragma optimize("", off)`. A bodiless declaration cannot be made to behave like a body (`const`, `noalias`
  do not help: callee.md section 3).
- The original drops them: the body (or a stub, `tools/calleesum.py`) must be in the unit.
- Which of the two is for the unit layout, not for the function: an M&B-style TU split is the likeliest cause when
  a function is byte-exact with its callee as an opaque extern and loses stores otherwise.

## 5. Tool support

`tools/icount.py`: for an original store that ours lacks, removed by `GdfRemoveDeadStores`, when the next call after
it in the snapshot takes the address of a local (`OPARG ... ; s0=&`) and has a written points-at set that is not
among its read sets, the verdict is `removed-by:DeadStores:callee-summary` with

```
why: removed because the callee's body is visible in this unit (interprocedural mod/ref): `in_fetch` has a summary
(c2 compiled it first) that says it does not read the object the pointer argument points to ...; the original probably had the
callee in another unit (declaration only), or its body reads the object
```

This also matches the missing store to an IL assignment of a local (`s2 <- 0`, kind const/copy) that icount's
store matching skipped before (a store to a stack slot is an instruction, but its IL tuple is a symbol assignment).
Heuristic: it checks the call's shape, not which local the written set stands for **[I]**; a call that writes some
global it does not read, next to an unrelated dead local store, can be named too. Probe: `tests/probes/icount_ipa.py`
(`ipa_ours` against `ipa_orig`: -5, the class and the hint; `ipa_read`, `ipa_pass`: equal, no hint).

## 6. Not verified

- cl 17.00 x64 (the same functions exist; no probe).
- The summary field offsets on 13.10.x and 15.00 (the behaviour and the IL shape are identical; only 14.00's
  offsets were read).
- Bit 0 of the written set in the `in_fetch` trace.
- LTCG (`/GL`): callee.md section 7.
