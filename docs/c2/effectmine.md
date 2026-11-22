# Effect mining: the source → model map, measured (`tools/effectmine.py`)

The models answer in their own vocabulary: `rasim need` says `ref:V:B` or `before:A:B`, `rasim scratch` says
`drop:8` / `ins:4`, `c2slots what` says `uses:tick=4` / `pos:#3` / `conf-:A,B`, `symkeys levers` says
`add:frame@V:17`, c2sched shows an issue order, loadcse a load count. Turning an answer into C++ was realize.py's
hand-written catalogue (realize.md §2), and the benchmarks ran out of entries: about 28 of 64 unmatched functions
stalled with "a model answer with no spelling". This tool replaces the catalogue's *writing* with *measuring*: it
applies a large generic set of meaning-preserving mutations to one function and its unit, compiles each one traced,
reads every model's input and decisions with the existing parsers, and stores mutation → effect in a database that
grows with every function it is run on. The database's per-class rates are the measured inverse map (section 4).

Builds: all four (14.00 default, 15.00, 13.10.3077, 13.10.3052; `--c2`). Every traced compile goes to the compile
server; a compile the server declines would run under wine here and is refused instead (`RemoteDeclined`, the run
stops after the first one; `EFFECTMINE_ALLOW_LOCAL=1` lifts it). With no compile server configured (or
`C2TRACE_REMOTE=0`) every traced compile runs under wine, as in any other tool.

## 1. Use

```sh
# mine one function (default 400 traced compiles), with what the model wants and the original's code
uv run tools/effectmine.py mine tu.cpp --func F --asm-vs orig.s --want 'uses:#0=4' 'pos:#3=4' bdiff -- /TP /MT /O2 ...
# the same for a 13.10 function; --list prints the generated mutations and stops
uv run tools/effectmine.py mine --c2 13.10 tu.cpp --func http_fetch --list -- /O2 /MT /Gy /TP ...
# ask the database (no new mining): this function's own records, then the classes that produced the effect elsewhere;
# --verify N compiles up to N unmeasured instances of those classes in this function (and pairs)
uv run tools/effectmine.py query tu.cpp --func F --want drop:6 [--verify 60] -- FLAGS
# the measured table, and the database
uv run tools/effectmine.py table [--build 13.10.3077] [--type 'S\.|L\.'] [--by vtype] [--min-n 3]
uv run tools/effectmine.py stats
```

Options of `mine`: `--features regs,scratch,slots,keys,sched,loads` (default all), `--eager` (every feature for
every variant) / `--eager-feats keys` (that one for every variant), `--classes RE` / `--skip RE` (mutation classes),
`--focus LO-HI` (line offsets in the function measured first, for answers the tool cannot place, such as a
scheduler order), `--pairs N` (pair compiles, default 16, on their own budget), `--depth 2 --depth-beam 4
--depth-compiles 60` (second edits, section 3), `--jobs N` (24, the server's slots), `--keep-variants N` (variant
sources kept, 12), `--fresh` (measure again what the database has). The budget can be overrun by the compiles in
flight (at most `--jobs` times the features).

Output: `work/effectmine/effects.jsonl` (one JSON record per measured variant: class, description, the edits as
text positions, the site context, the code numbers and every model's effect strings; never pruned),
`work/effectmine/texts/<sha>.cpp.gz` (each base source, so a record's edits can be applied again),
`work/effectmine/runs/<tu>-<hash>[@build]/<run>/` (per run: the base and the `--keep-variants` best variants,
sources and listings; every trace is deleted once it is read). `EFFECTMINE_DIR` moves all of it (the tests use a scratch directory).

Headers: at the start the unit's include closure (and the /I dirs) is copied to a temp folder (`tools/incsnap.py`)
and every source and compile of the run uses the copy, so editing the tree during a run does not reach it; the
records, the base text and its sha keep the live paths. rwsweep does the same.

## 2. What one variant costs and what is recorded

One compile with the `regs` hooks gives the final code and the allocation's input. Its record:

| model | source of the effect strings | vocabulary |
|---|---|---|
| C (code) | realize.Delta's code numbers, plus a byte-key diff (tsweep.bkeys) and bdiff against `--asm-vs` | `code` (registers masked), `bag` (same instructions in any order), `ident` (with registers), `bytes` (displacements too: the listing names stack slots symbolically), `bdo` (bdiff against the original) |
| R (allocation) | realize.Delta, plus colouring order and compiler locals | `ref:V:B:+2` `unref:` `extend:` `drop:` `var:` `gone:` `P:B:+1` `tmp:` `interf:` `copy:` `pref:V:REG:+1` `tie:A>B` `reg:V:old>new` `before:A:B` (A now coloured before B) |
| S (scratch picks) | realize.pick_snap / PickDelta | `ins:N` `drop:N` `regs:k picks, first N r>r` |
| L (stack slots) | realize.slot_snap / SlotDelta, c2slots' map of the original | `uses:X:a>b` `pos:X:a>b` (rank in the size run) `ord:X:a>b` (visiting position) `conf+:A,B` `conf-:A,B` `new:` `gone:` `off:X:a>b` `T:a>b`; `slot_agree` = elements at the original's offset |
| K (symbol keys) | symkeys.trace / realize.key_delta_f | `keys:frame:+n` (allocations per pool class), `key:V:+d` (a named key moved), `flips:n` (pass-2 operand orders flipped) |
| Q (scheduler) | c2sched.parse | `sched:R<k>:A > B` (instruction A now issued before B in region k), `regions:n` (regions whose input changed) |
| M (loads) | loadcse (child process: its PAS dump changes every trace's configuration) | `load:LOC:a>b` (loads of LOC in the final code), `rmw:LOC:..>..` |
| F (front end) | tools/feshape.py: the function's tuple shapes in the reader IL and after inlining (section 7) | `fe:SHAPE:+n` (reader IL), `fi:SHAPE:+n` (after Inline, when not already an `fe:`) |

S, L, K, Q and M cost one compile each and are traced only when the regs trace changed: the IL at Color (tuple
addresses removed: they are heap pointers that move with any declaration), an allocation-input item, or the code
(bytes included). The reason: everything those models decide ends in the code or in the IL Color gets, so an
identical IL and identical bytes leave nothing to see. What this misses: a model input that changes without moving
anything (a slot use count that leaves every offset where it was, a key renumbering that flips no sort) when the
IL at Color is also identical. The table therefore reports S/L/K/Q/M rates over the variants where that model was
traced (column denominators, section 4). Features named by `--want` are traced for every variant.

**Effect types.** Each effect string has a type that generalises across functions: `R.ref+`, `R.P-`, `R.before`,
`R.pref+`, `R.reg`, `S.ins`, `S.drop`, `L.uses+`, `L.conf-`, `L.off`, `K.frame+`, `K.flips`, `Q.flip`, `M.load-`,
and for the code `C.same` / `C.frame` (only stack offsets) / `C.regsonly` / `C.bag0` / `C.changed`, `C.bdiff-` /
`C.bdiff+` (closer to / farther from the original: by `(rdiff, bdiff)` since 2026-11-20, tracer.md `rdiff`; a
record measured before then has no rdiff and is compared by bdiff), `C.match` (bdiff 0). A variant with no effect and the same bytes is `null`.

**Helper controls.** Rewrites that need a helper template (`hs_id`, `hs_cp`, `rz_sub`, `em_cr`, ..) add declarations
before the function, and on 13.10 a declaration count alone flips the order of two loads (Mount & Blade 0x4b9110,
section 5). Each helper set is compiled once on its own (class `decl.helpers`), and a variant that uses it is
measured against that control, not against the bare base.

**Context.** Each record carries where the mutation is: `line` (offset in the function), `block` (the base's
block holding that line), `loop`, the variable it touches with `vkind` (this / param / local) and `vtype`
(ptr / int / float / class / ref), the statement kind, or `where: unit` for a mutation outside the body; and per
run the build, the flags and the source's hash (`fkey`). `table --by vtype|vkind|stmt|loop|build` splits the rates.

## 3. The mutation classes

Generic: no class knows the function. The generators are rwsweep's families and realize's constructs applied to
the whole body (not to one block's lines), and this tool's own:

| family | classes | what |
|---|---|---|
| rwsweep | `wrap.{var,chain,this,arg}.{hs_id,hs_cp,hs_bind}`, `alias.{ref,val}`, `swap`, `split`, `touch.*`, `cflow.*`, `paren.*`, `guard.*`, `const.*`, `unalias`, `byval` | as rwsweep.py documents them |
| realize | `copy`, `uncopy`, `headdup[.mkelse]`, `headhoist`, `taildup[.mkelse]`, `tailhoist`, `eqif.{assign,decl,call}` (tested on `this` and the parameters), `eqif2` (tested on a value in a callee-saved register), `eqifv` (tested on a local, around the statements next to its uses), `uneqif`, `helperbind` | realize.md §2 |
| unit | `decl.<kind>.<before,top>` with n = 1..32: `extvar`, `fdecl`, `fwd`, `sdef`, `tdef`, `tmpl`, `enum`, `inl` dummy declarations, on one line before the function's top-level construct or at the unit's start; `deffn.before` (n dummy external functions, compiled: c2 runs on each); `move.up` (the definition before the previous one), `move.end` | the cross-function state: declaration counts, function order |
| locals | `pad.init[.top]` (`int em_u = 0;` unused, n = 1..16 at the start), `pad.noinit.top`, `pad.vol`, `declhoist[.top]`, `declsink`, `merge` (`T x; x = e;` as one), `scope` (a `{ }` from a declaration to the end of its block), `constq.{add,addptr,remove}`, `width` (int ↔ long, unsigned ↔ unsigned long) | declarations |
| expressions | `commute` (operands of `+ * & \| ^ == !=`), `cmpflip` (`a < b` as `b > a`), `compound.{expand,fold}` (`x op= y` ↔ `x = x op (y)`), `incdec`, `nullcmp.{add,drop}`, `index` (`a[i]` → `*(a + (i))`), `retwrap.*`, `hop.{up,down}` (a statement over two independent ones), `argswap` (the two compared operands of memcmp / strcmp / .. where only `== 0` is used) | evaluation order and operand order |
| inline boundaries | `wrapx.{var,chain,this}.{em_cr,em_mkv,em_idr,hs_id2}` (a const reference, a by-value struct with an inline getter, a named copy through a reference, hs_id twice), `subobj` (a member through `rz_sub<T>::get()`) | inline expansions |

Not generated, on purpose: callee bodies (they change the program), `reread` (meaning kept only without stores in
between), `recompute`. Inline assembler blocks are never touched. A rewrite that does not compile is recorded as
failed (the error kept) and costs one compile.

**Order.** Candidates are measured in this order until the budget is spent: (1) with `--want`, every class's
instances on the wanted decision's source lines (±1: the pick's line, the slot element's uses, the variables of an
order answer, the block's lines) round robin over classes; (2) every class round robin, classes with a higher
measured rate for the wanted effect type (or for any effect, without `--want`) first and with more instances per
round; inside a class, the instances nearest the wanted lines first, the rest shuffled over the function. A variant
the database already has for the same source, build and flags is not compiled again.

**Depth 2.** The `--depth-beam` best code-neutral variants that changed something (a change in a wanted model that
names a wanted variable first) become bases; the generators run again on their text, near the wanted lines, and up
to `--depth-compiles` second edits are measured (class `chain`; the record keeps both edit lists). This reaches an
edit that only exists after another one (a declaration hoisted, then a test on it).

**Pairs.** Every pair of variants that helped on its own (bdiff lower, or a wanted effect) and edits different
text, ranked by their summed gains; the first `--pairs` are compiled, on their own budget.

## 4. The measured table

`effectmine.py table` over the database after the validation runs below (4 functions, 3 builds; 1,248
active records). n = compiled variants of the class, fn = functions it was measured on, null = the regs trace was
identical to the reference. Per effect type: the share of variants that showed it, in brackets the share that
showed it **with the instructions unchanged** (registers masked: `code` 0). R and C columns are of all compiled
variants; S L K Q M of those where that model was traced (section 2). Classes with n >= 8:

| class | n | fn | null | R.ref+ | R.P+ | R.before | R.reg | S.ins | S.drop | L.uses+ | L.uses- | L.ord | L.conf- | L.off | K.frame+ | Q.flip | M.load- | C.bag0 | C.bdiff- |
|---|--:|--:|--:|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `alias.ref` | 43 | 4 | 2% |  |  |  |  | 29% (24%) | 29% (24%) |  |  |  |  |  | 86% (65%) | 22% |  | 20% |  |
| `alias.val` | 34 | 4 | 2% |  |  |  | 8% | 36% (33%) | 33% (33%) |  | 9% | 9% | 41% (32%) | 41% (32%) | 100% (87%) | 3% |  |  |  |
| `chain` | 102 | 4 | 29% |  | 0% | 4% | 6% | 11% (2%) | 7% (2%) | 4% |  | 8% | 4% (3%) | 11% (3%) | 71% (54%) | 4% |  | 6% | 5% (5%) |
| `decl.enum.before` | 12 | 2 | 75% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 25% |  |
| `decl.enum.top` | 12 | 2 | 58% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 41% |  |
| `decl.extvar.before` | 12 | 2 | 75% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 25% |  |
| `decl.extvar.top` | 12 | 2 | 66% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 33% |  |
| `decl.fdecl.before` | 12 | 2 | 75% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 25% |  |
| `decl.fdecl.top` | 10 | 2 | 50% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 50% |  |
| `decl.fwd.before` | 12 | 2 | 75% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 25% |  |
| `decl.fwd.top` | 12 | 2 | 58% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 41% |  |
| `decl.helpers` | 100 | 4 | 98% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 2% |  |
| `decl.inl.before` | 11 | 2 | 72% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 27% |  |
| `decl.inl.top` | 12 | 2 | 58% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 41% |  |
| `decl.sdef.before` | 12 | 2 | 91% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 8% |  |
| `decl.sdef.top` | 10 | 2 | 70% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 30% |  |
| `decl.tdef.before` | 12 | 2 | 75% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 25% |  |
| `decl.tdef.top` | 12 | 2 | 66% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 33% |  |
| `decl.tmpl.before` | 12 | 2 | 75% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 25% |  |
| `decl.tmpl.top` | 12 | 2 | 50% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 50% |  |
| `deffn.before` | 8 | 2 | 25% |  |  |  |  |  |  |  |  |  |  |  |  |  |  | 12% |  |
| `pad.init` | 24 | 4 | 4% |  |  |  |  | 22% (22%) | 22% (22%) |  |  |  |  |  | 100% (100%) |  |  |  |  |
| `pad.vol` | 25 | 4 | 0% | 24% | 88% | 32% | 32% | 37% | 37% | 15% |  | 100% |  | 70% | 100% | 15% |  |  |  |
| `scope` | 14 | 4 | 85% |  |  |  |  |  |  |  | 20% (20%) |  | 20% (20%) | 10% (10%) |  |  |  |  |  |
| `touch.assume` | 39 | 4 | 2% | 5% | 7% | 12% | 7% | 5% | 28% |  | 5% | 5% |  |  |  |  |  |  |  |
| `touch.cp` | 50 | 4 | 4% |  | 4% | 2% | 6% | 10% (4%) | 10% (4%) | 2% |  | 6% | 23% | 28% | 100% (56%) |  |  | 36% | 2% (2%) |
| `touch.if` | 38 | 4 | 7% |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| `touch.nop` | 50 | 4 | 8% |  |  | 2% | 6% | 6% | 6% | 2% |  | 6% |  | 4% | 100% (54%) |  |  | 36% |  |
| `touch.nopref` | 44 | 4 | 6% |  |  |  |  |  |  |  |  |  |  |  | 100% (56%) |  |  | 40% |  |
| `touch.scope` | 45 | 4 | 6% |  |  |  |  | 2% (2%) | 2% (2%) |  |  |  |  |  | 100% (40%) | 10% |  | 55% |  |
| `touch.self` | 45 | 4 | 77% |  |  |  |  | 15% (7%) | 15% (7%) |  |  |  |  |  | 100% (90%) |  |  |  |  |
| `touch.void` | 51 | 4 | 100% |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |  |
| `wrap.arg.hs_cp` | 8 | 3 | 0% |  |  | 12% | 25% |  | 12% |  | 12% | 12% | 12% |  | 100% (62%) | 14% |  |  | 12% |
| `wrap.arg.hs_id` | 8 | 3 | 0% |  |  | 12% | 12% |  | 12% |  | 12% |  | 12% |  | 100% (75%) | 12% |  |  | 12% |
| `wrap.chain.hs_bind` | 9 | 3 | 0% |  |  |  |  | 11% (11%) | 11% (11%) |  |  |  |  |  | 100% (100%) |  |  |  |  |
| `wrap.var.hs_bind` | 26 | 4 | 7% |  |  |  |  | 8% (8%) | 8% (8%) |  |  |  |  |  | 100% (86%) | 15% |  | 11% |  |
| `wrap.var.hs_cp` | 21 | 4 | 0% | 9% | 9% | 9% | 19% | 28% (9%) | 23% (9%) |  | 5% | 21% | 21% (5%) | 26% (5%) | 100% (73%) | 5% |  |  |  |
| `wrap.var.hs_id` | 23 | 4 | 13% | 8% | 13% | 8% | 21% | 19% | 14% |  |  | 21% | 10% | 21% | 100% (78%) |  |  |  |  |
| `wrapx.var.em_cr` | 20 | 4 | 15% |  |  |  |  |  |  |  |  |  |  |  | 100% (100%) |  |  |  |  |
| `wrapx.var.em_idr` | 22 | 4 | 4% | 9% | 13% | 18% | 22% | 38% (9%) | 33% (9%) |  | 5% | 27% | 22% (5%) | 33% (5%) | 100% (72%) |  |  |  |  |
| `wrapx.var.em_mkv` | 23 | 4 | 4% | 8% | 8% | 13% | 17% | 30% (8%) | 26% (8%) |  | 10% | 25% | 25% | 30% | 100% (65%) | 5% |  | 4% |  |
| `wrapx.var.hs_id2` | 22 | 4 | 13% | 9% | 9% | 13% | 18% | 23% | 19% |  | 11% | 22% | 16% | 22% | 100% (72%) | 5% |  |  |  |

75 classes were measured, 1,171 compiled variants (pairs and the retired first runs left out); the 33 classes
with fewer than 8 instances are in `table --min-n 1` (among them `copy`, `eqif.*`, `commute` 5 of 6 null, `paren.*`
14 of 14 null, `swap`, `hop.*`, `subobj`, `move.*`).

What the table says, by effect the models ask for:

1. **Many rewrites do nothing.** `touch.void` (50 of 50 null), `paren.*` (14 of 14), `decl.helpers` (93 of 96),
   `touch.self` (77%), `scope` (85%) and, on 14.00 / 15.00, the `decl.*` declaration counts (Spore 34 of 34 null;
   Fallout 3 66 of 66 in the first, retired run) are identical after the front end; the ordering measures such
   classes last once the database knows them. A null class is still an answer: those spellings need not be tried.
2. **Frame keys (`add:frame@V:n`) have many code-neutral spellings, with fixed counts.** Measured per instance
   (frame keys added, and how often the bytes stayed identical): an unused initialised local +1 (19/19),
   `em_cr(x)` +1 (17/17), `hs_nopr(x);` +1, `hs_nop(x);` +1, `hs_bind(x)` +2 (19/22), `{ hs_bind(x); }`
   (`touch.scope`) +2, `hs_cp(x)` +2, `hs_cp(x);` +2, `hs_id(hs_id(x))` +2, `em_mkv(x).get()` +6 (9/10), a reference or value alias
   +1. These add, so +n at one point is a sum of them. They also renumber every later key: on x87 code that can flip
   operand sorts (realize.md §2.6), so each needs its compile.
3. **Scratch picks (`ins:N` / `drop:N`) move with the code unchanged** under aliases and unused locals (alias.ref,
   alias.val, pad.init: 22 to 36% of the traced variants, with the instructions unchanged in most; all on the two
   13.10 functions) and, rarely, wrappers. On Mount & Blade 0x0044be50 a `hs_cp(ident);` statement inserts and drops picks with the instructions unchanged and
   bdiff 36 → 30 (section 5): the kind of answer realize had no construct for.
4. **Slot conflicts (`conf-:A,B`) and lower use counts (`uses-`) have code-neutral spellings.** A block scope
   `{ }` from a declaration to the end of its block (class `scope`) removed three conflicts and two uses of a
   structure local in Fallout 3 0x009c7730 with identical bytes (one of 2 instances that changed the packer; the
   other moved offsets). A value alias `T a = V;` with the later uses renamed (`alias.val`) removed conflicts with
   identical bytes in 10 Mount & Blade 0x004b9110 variants (13.10). realize.md §2.8 had "nothing shortens a range
   without code". Why the scope or the copy shortens the packer's ranges is not established **[I]**.
5. **Slot use count up (`uses+`), visiting position (`ord`)**: produced only by rewrites that change code
   (`pad.vol`, wrappers, `eqif.call`, taildup); no code-neutral instance in 4 functions.
6. **Colouring order (`before:A:B`), registers (`reg:`), preferences**: wrappers and aliases move them in 10 to 25%
   of cases, but with code changes; code-neutral register moves come with the key-shifting classes (R.reg with code
   0: alias.*, touch.*, pad.*).
7. **Scheduler order (`sched:A>B`)**: rare everywhere (`Q.flip` <= 22%, never code-neutral by construction: a new
   order is new code). On 13.10 the *pre-schedule* order of a region flips with the number of declarations before
   the function, the helper set, or unused locals at its start (`C.bag0`, the same instructions reordered; section
   5, Mount & Blade 0x004b9110), non-monotonic in the count. The same flip, to the same second state, came from 145
   variants of many unrelated classes there (`hs_nopr(x);` 40%, `{ hs_bind(x); }` 55%, aliases 20%): it follows
   something global to the compile (symbol or allocation counts **[I]**), not a construct.
8. **Fewer bytes against the original (`C.bdiff-`)** came from wrappers on call arguments and member chains (Spore
   0x005ec030: 9 to 14% of those classes) and from one `touch.cp` on Mount & Blade 0x0044be50; nothing else in 4
   functions.

## 5. Validation on the benchmark's blockers

Each function from the benchmark's current best draft, with its flags, the model's answers as `--want` and the
original's listing. Numbers are tsweep.bdiff (the current definition), regdiff in brackets where it matters.
"Variants" = distinct mutations measured (database records of that source, build and flags), plus depth-2 second
edits and pairs. Logs: `work/effectmine/val/` (not in git).

| Function | Build | Model's answer | Variants (compiles) | bdiff before → after | Spelling found? |
|---|---|---|---|---|---|
| Fallout 3 0x009c7730 (`b_c1.cpp`) | 14.00 | `uses:#0=4` (tick 3 → 4 uses) + `pos:#3=4` (v0 visited 4th); no construct in realize | 266 singles (242 compiled, 44 null), 48 depth-2 (about 2,300 traced compiles over 4 runs) | 18 (0) → 18 | **no.** `uses:#0=4`: 0 variants. `pos:#3=4`: 3, all with code changes (bdiff 44, 49, 80). Nothing below 18 |
| Mount & Blade 0x0044be50 (`s0044be50.cpp`) | 13.10.3077 | `drop:6` (the g_items load's pick; one pick costs a tail merge) | 60 singles (25 null), 16 depth-2 (none below 30), no pair (one helpful single), features regs + scratch only (about 230 compiles; 463 KB unit, 177 MB trace per compile) | 36 (32) → **30** (26) | **no** for `drop:6` (0 variants drop that pick). `hs_cp(ident);` (`touch.cp`) at line +14: same instructions, 6 registers move, picks inserted and dropped later in the function (`ins:24`, `ins:42`, `drop:20` ..) |
| Mount & Blade 0x004b9110 (`s004b9110.cpp`) | 13.10.3077 | the inline memcmp's `mov ecx, 5` issued after `mov edi` / `lea esi` (`sched:edi=t>ecx=t`, `sched:esi=t>ecx=t`) | 281 singles (260 compiled), 165 declaration-count variants, 24 depth-2 (about 3,100 compiles) | 6 (2) → 6 | **no.** No variant moves that `mov ecx`. The declaration count flips another region instead (below) |
| Spore 0x005ec030 (`f005ec030.cpp`) | 15.00 | `pref:local4:ebp`, the order `this>local4`; registers key=esi, this=edi, itLower(local4)=ebp | 263 singles (260 compiled, 83 null), 25 depth-2, 18 pairs (about 1,900 compiles) | 54 (46) → **32** (26) | **partly.** `KeyLess_s005e_c030(hs_id(*key), ..)` (`wrap.arg`) gives this=edi and bdiff 32, but key goes to ebp, not esi: the benchmark's best variant (rwsweep, RESULTS2 `r2_rw35`), found again without knowing it. `pref:local4:ebp`, `this>local4`, `local4=ebp`: 0 variants. Pairs: 33 to 35 |

No byte-exact match. Per function:

- **Fallout 3 0x009c7730.** `tick` lives in a block that holds an `__asm` write (`mov dword ptr [tick], eax`) and two
  plain stores (`record->tsc = tick; buffer->cur = record + 1;`). A use without code needs a statement that reaches
  the packer in tick's scope (equal arms around a plain store are merged before Color: realize.md rule 8), and there
  is none; the 48 second edits of depth 2 (on code-neutral bases) found no other way. What mining found instead,
  on this function: a block scope `{ }` from `NativeHitInfo hit;` to the end of its block removes three conflicts and two uses of `_hit` with
  identical bytes (`conf-:_bodyA,_hit`, `conf-:_bodyB,_hit`, `conf-:_hit,_tick`, `uses:_hit:15>13`) — a code-neutral
  `conf-:` spelling, which realize.md §5 lists as missing. It is not the answer this function needs.
- **Mount & Blade 0x0044be50.** The unit is the expensive case: one regs trace is 177 MB, so 60 single rewrites were
  all the budget gave (focus first: the 60 are mostly at the lines of pick 6). None removes pick 6 (the `g_items`
  load in the else arm). `hs_cp(ident);` before the `identity_pos` call is a code-neutral rewrite that moves the
  scratch rotation downstream and gets 6 more registers right (bdiff 36 → 30, regdiff 32 → 26; checked by a separate compile); depth 2 on it
  and on three other code-neutral variants (16 second edits) gave nothing better, and with one helpful single
  there is no pair. The tail merge that
  pick 6 costs is still missing.
- **Mount & Blade 0x004b9110.** The declaration count before the function (and the helper set, an unused local at
  the function's start, a dummy function definition) flips the function between two states: A (the draft, bdiff 6)
  and B (bdiff 10: after `call _malloc`, `mov ebx, [sock]` and `mov ebp, [__imp__recv]` exchange). B is a change of
  the *pre-schedule* order of that region (`regions:1`, the same instructions: `C.bag0`), not of the scheduler's tie.
  Measured over 165 variants (11 kinds x 2 places x n): n = 1: 1 of 17 in B, 2: 5/15, 3: 7/16, 4: 8/15, **5: 15/16**,
  6: 9/14, 8: 7/17, 12: 2/13, 16: 3/12, 24: 4/15, **32: 0/15**; the kind matters less than n (per kind 1 to 6 of 10).
  Neither state has the original's `mov ecx` order, and none of 281 single and 24 depth-2 rewrites (argswap of the
  memcmp operands included: bdiff 8) moved it. The order is the memcmp intrinsic's own operand setup; the original
  probably wrote the call differently, which no generic mutation reaches.
- **Spore 0x005ec030.** The rediscovered `hs_id(*key)` is the only class that helps (`wrap.arg.*`, and `wrap.chain`
  on `itLower.node->key`: 38); 7 helpful singles, their 10 pairs no better. The wanted `local4` (itLower's node, a
  compiler local) is never moved to ebp; the by-value wrapper of `DoInsertValue_v` (rwsweep's new `byval`) changed
  nothing (54), the one for `lower_bound_v` was not reached. The draft's structure (how the by-value iterator results
  are copied, RESULTS2) is the stall, as the benchmark said.

Count: 0 of 4 matched. 2 improved: Spore 0x005ec030 54 → 32 (to the benchmark's known best, found again blind) and
Mount & Blade 0x0044be50 36 → 30 (new; the benchmark had stalled it). 2 unchanged (Fallout 3 0x009c7730, Mount &
Blade 0x004b9110). The mining did not find a spelling for any of the four model answers it was given. It found, on
the way, measured spellings that realize lacks (code-neutral `conf-:` / `uses-`, pick insertions and drops with
unchanged instructions, frame-key adders with fixed counts) and the 13.10 declaration-count map.


## 6. Limits and what would help

- **Cost per variant is the trace, not the compile, for large units.** A regs trace logs every function of the unit:
  Mount & Blade `agent_logic.cpp` (463 KB) writes a 177 MB log per compile, which the compile server sends back;
  with the 26 s compile that is about one variant per minute per slot (Fallout 3 and Spore units: 1 to 10 s). The
  tracer's function filter (tracer.md, "Function filter") now applies by default: every trace logs only F's
  compile, so a large unit costs its compile time and F's events. The key feature (symkeys) still traces the
  whole unit, so on large units leave it out (`--features` without `keys`) unless the answer is a key.
- **Lazy secondary traces** miss a model input that changes while the IL at Color and the bytes stay the same (a
  slot use count that moves no offset, a key renumbering that flips no sort). `--eager` / `--eager-feats` measure
  them; the table counts S L K Q M over the variants where they were traced.
- **Names.** Compiler locals are named by symbol key (`local4`); a variant that adds or removes a key renames them.
  They are mapped back by colouring order when the counts agree, otherwise the variant's effects on them are not
  comparable (an answer about `local4` can then be missed). Slot temporaries (`tv`, `$T`) are matched by size and
  line, as realize does.
- **The mutations are textual** (rwsweep's tokenizer, no types): 5 to 15% of the instances in a large function do
  not compile (each costs one compile). Inline assembler blocks are skipped.
- **The scheduler feature** compares regions with the same pre-schedule instruction list; a mutation that changes
  that list shows only as `regions:n`. Several such changes are renumbering that the normalisation does not catch
  yet (the same code, other numbers in the IL text).
- **Pairs** are formed only from singles that helped alone; **depth 2** only from code-neutral singles. An edit
  that is useless alone and decisive with another one is found only when the first is code-neutral.
- **Retired runs.** The first runs of this tool (one per function) used an earlier effect vocabulary (no byte
  diff, compiler locals unmapped, a noisier scheduler signature); their records stay in effects.jsonl and are left
  out by `work/effectmine/retired.txt` (run ids). The functions were measured again.
- **The database is small** (section 4): rates over fewer than ~10 instances, from 4 functions, are a first
  estimate. Each `mine` run adds to it; the ordering of later runs uses it.

## 7. Model F: the front end, measured (`tools/feshape.py`)

icount's `structure` verdict says only that our IL never had the original's tuples: the front end (c1/c1xx) wrote
other IL. Which source construct makes which IL is not modelled; model F measures it instead, the same way as the
other models. Per variant whose regs trace changed (one more traced compile), feshape takes the function's IL at two
passes and counts its tuple *shapes*: the opcode, a branch's condition and the operand kinds without names or numbers
(`OPASSIGN(V:5004<-T:5004)`, `OPBRANCH[l](<-L,T:9000)`, `OPCALL(T:5004<-F)`; kinds T temporary, V named variable,
K immediate candidate, C literal, M memory, F function, L label, and implicit operands by name). The stages: the
reader IL (MscRVO's entry on 14.00; Inline's entry on 15.00 and 13.10, which have no hookable MscRVO; both are
before same-unit callees are expanded) and the IL after inlining (FgSimpleFoldCmps' entry). The effect strings are
the multiset changes, `fe:SHAPE:+n` / `fi:SHAPE:+n`. Their effect types count per stage and opcode, net:
`F.fe:OPPAREN-` only when the reader IL has fewer OPPAREN tuples; a change of operand kinds alone is `F.fe:kind`
(`hs_id(this)` turns `OPADD(T<-V,C)` into `OPADD(T<-T,C)`: one shape less, one more, no tuple fewer). The same rule
decides a `--want 'fe:SHAPE:-n'` and icount's lookup (`feshape.hit`): the exact shape must drop by n *and* the
opcode's net count with it. Without it, 20 of the 24 mined rows below "found" their removal through such renames.
`effectmine.py table --type '^F\.'` lists the most frequent F types (they are open-ended, one per opcode).

```sh
uv run tools/feshape.py tu.cpp --func F [--stage reader|inlined] -- FLAGS     # a function's shapes, counted
uv run tools/effectmine.py mine tu.cpp --func F --want 'fe:OPPAREN(T:5004<-V:5004):-1' -- FLAGS
```

**icount uses it.** An EXTRA instruction with a `structure` verdict whose tuple was born in the reader IL (or at
Inline) gets a `front end:` line: its shape, the `--want` string that asks effectmine for a mutation removing it, and
the classes of the database whose variants removed that shape elsewhere (`hits/measured`), or "no measured mutation
yet". A MISSING instruction has no tuple of ours, so no shape: the original's IL is not observable.

**First measurement** (tests/data/fe/fe_cond.cpp, probes in tests/probes/feshape.py): `if ((f = g()) >= 0.5f)`
against `f = g(); if (f >= 0.5f)` adds `OPPAREN` of `f` in the reader IL and the compare reads the temporary, on all
four builds (14.00/15.00: `OPCONVERT(T:5008<-T:5004)` for `<-V:5004`; 13.10: `OPCMP(T:8000<-T:4004,C:4004)` for
`<-V:4004`, its front end compares floats as floats, x87.md). On Spore 0x00d38780 a 40-compile mine with this want
found `alias.ref` (a reference to `f`) as the one class that removes the `OPPAREN` (an exact-shape match, made
before the net-count rule above; not re-checked under it).

**What the front-end probes say** (tests/data/rules/RE_*, frontend_il.md; 2026-11-19), the starting points of a
model from source construct to reader IL:
- The reader IL is strictly left to right, one tuple per operator, on every build; every call-order difference
  (frontend_il.md 2.4) is c2's, and 13.10 reorders the same IL differently from 14.00/15.00. A model can walk the
  expression tree left to right and leave reordering to a build-dependent c2 stage.
- c1xx keeps facts outside the tuple stream that later passes read: `h(); return r;` and `return (h(), r);` dump
  the same tuples, yet NRVO elides only the first (frontend_il.md 3A.8). Model F cannot see such attributes.
- Emission order is a dependency order: implicit members in order of first need (a constructed object's destructor
  at once), dynamic-initialiser cycles cut at the edge into the last-defined member; both predicted exactly from
  source (frontend_il.md 4.11, 4.13).
- Constructor IL follows the class layout, not the initialiser list; a body assignment equals the list form iff
  no later-declared member is initialised (frontend_il.md 3B.10).
- A local with a destructor wraps the return value in a temporary (`sN = a; OPDTORACTION; OPRET <- sN`): where
  by-value and by-reference accessors diverge (frontend.md).
- The reader's switch partitioning is build-dependent (13.10 needs 5 dense values inside a sparse set for a table,
  14.00/15.00 need 4), and at /Od local slot order depends on the names (il_sym.md 8).

**Seeding run** (2026-11-20). 130 near-miss rows (61 Mount & Blade, 13.10; 69 Spore slices, 15.00, one small-diff
row per slice) through icount: 29 have `structure` extras whose tuple has a shape. 24 of them mined (the rest:
the function defined in a header, or /Od slices with no register allocation), 200 traced compiles each, every
variant with model F traced: 2,142 records with F data, 1,157 with a front-end change, over 47 functions in the
database. Measured:
- **The catalogue does not remove front-end tuples.** Its classes were built to move allocation without changing
  code, and that is what they do: the wrap / wrapx / alias classes change operand kinds (`F.fe:kind` in 60-85% of
  their variants) or add tuples (helper calls, declarations); the only classes that remove one are branch-sense
  changes: `cflow.swaparms` (`OPBRANCH[ge]-` 30%), `cflow.emptythen` (`OPBRANCH[ne]-`/`[e]-` 39%), `cmpflip`
  (`OPBRANCH[ge]-` 52%). Under the net-count rule two rows have a real removal (Mount & Blade 0x4e8720 by
  `cflow.emptythen`, Spore 0x8bdf80 by `cflow.swaparms` / `cmpflip`), and in neither did it lower the bdiff.
- **Most `structure` extras are meaning, not spelling.** By source line: `((UiA*)row[2])->f184 = 1` where the
  original ORs (`or [eax+0x184],1`), `*g |= 1` against a plain store, `p->mRef = --n`, an extra `if (!src) return;`,
  an extra compare in `if (rank >= cap)`, `(char*)this + 0x20` passed where the original passes something else.
  A meaning-preserving mutation cannot reach these; the draft's statement is different. So the verdict is mostly a
  pointer to re-read the original at that line, not a lever.
- **Single-use locals are not the cause.** `rwsweep --families subst` (new: `T v = E;` read once, no call / store
  / loop between, substituted as `((T)(E))`) on Mount & Blade 0x532de0 (`Party* pp = ...`) and Spore 0xd9eb70
  (`cPoolSlot* pSlot = &mpBegin[nSlot]`): the regs trace is identical, c2's copy propagation already removes such
  a local, so the extra comes from the expression, not the variable.

**Meaning-changing rewrites** (`rwsweep --families repair`, 2026-11-20; the same rows, on a 32-vCPU Linux pod under
wine). 22 rows swept (the rest: /Od, C code, header functions, one that does not build in a sweep copy), 6-84
variants each. Spore: 9 of 13 rows have a rewrite that lowers the bdiff; Mount & Blade: 0 of 9. Every hit but one
is `repair.drop` (the other `repair.step`); `rmw`, `edge` and `sign` lowered nothing. Read a drop as a locator, not a
fix: removing a statement the original has differently also removes misaligned instructions. The strongest are
the ones with shape 1-3: Spore 0xd1f250 `p->mRef = 1;` removed 16 -> 4 (and `p->mRef = --n` as `n - 1` 16 -> 10),
0xfc2f90 `p->mRefs = 1;` 16 -> 14 at two sites (a reference-count initialisation the original writes elsewhere or
not at all), 0x957a60 `*g |= 1;` 9 -> 5. No row reached bdiff 0. So the original's statement is findable by
removal, then has to be written by reading the original there; Mount & Blade's extras (branches and compares) are
not single statements.

**Limits.** Shapes drop names, so two tuples of the same kind are not told apart, and an `fe:` change says only
that a mutation added or removed such a tuple somewhere in the function; the site is in the record's edits. The map
is only as wide as the mutation classes (section 3): a construct no class writes (a different loop form, a helper
the source never had) is not measured. F records accumulate with every mine run that traces `frontend` (`--features ...,frontend` or a `--want fe:`).

