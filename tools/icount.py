#!/usr/bin/env python3
"""Why our function has more or fewer instructions than the original: each extra and missing instruction traced to
the IL tuple it came from and the c2 pass that made, kept or removed it.  Doc: docs/c2/icount.md

usage: icount.py SRC --func F [--c2 BUILD] --asm-vs ORIG.s [--asm-func G] [--fine] [--json OUT] [--keep DIR]
                 [--show-il] [--all] [-- <cl args>]

One traced compile (on the compile server; never under wine unless --allow-local) dumps the function's IL at the
entry of every pass of the per-function pipeline that this build lets us hook (about 60: the GlobalOptimize
sub-passes, loop optimisation, Lower, Peeps, register allocation, Riscify on 13.10, the late flow optimiser,
LateGlobalOptimize, MscClean, Schedule, Encode; `--fine` adds GoptOptimizeBlock, one snapshot per block and sweep).
The dumps are logged only while F is compiled (the tracer's function filter, C2TRACE_FUNC / `func`), so a big TU
stays small. A tuple keeps its address for its whole life (il_tuple.md), so every final instruction has a history:
the snapshot it first appeared in and the passes that rewrote it. Then:

1. our final code (listing) is aligned against ORIG.s (tsweep's bdiff keys, refined: exact keys, then register
   shapes, then registers masked, then mnemonics) and the unmatched instructions are grouped into runs: EXTRA
   (ours only) and MISSING (the original only). Aligned pairs that differ are not counted: they are not an
   instruction-count difference (bdiff, regmap, scratch explain those).
2. EXTRA: its tuple (listing offset -> tuple through the IL at Encode), where the tuple was born (front end, Inline,
   Lower, Riscify, register allocation ...) and, for its kind, which later passes would remove such an instruction,
   ran on it and kept it, and why when the IL shows it (loads: tools/loadcse.py's rules R1-R6).
3. MISSING: the tuples of OUR IL that sat at the same place (between the surviving neighbours of the run) and were
   removed or rewritten, with the pass interval that did it; matched to the missing instruction by kind (a missing
   `cmp`/`jcc` to a removed OPCMP/OPBRANCH, a missing load to a removed load or a memory operand CSE replaced ...).
   Nothing removed there: `structure` (our IL never had it: the front end produced different tuples).
4. each run gets a source line (our listing's line comments) and a verdict class, with the lever where one is known.

Verdict classes (docs/c2/icount.md has the table with the levers):
  removed-by:GlobalOptimize/<sub>   CSE (sweep 2), sweep1 (copy/constant propagation), DeadStores, CopyProp, ...
  folded:range-check | folded:compare   a compare and branch decided from a dominating test (sweep 1, RCE)
  merged:FgDoLateFlowOpts           tail / head merge or cross jump (`c2trace.py merges` for the pair)
  peephole:<pass>                   Peeps, MscClean, LateGlobalOptimize, MdmSuperHack, Sizeext ...
  riscify / riscify-undo            13.10: a memory operand split off into a load, or a split put back
  regalloc:<pass>                   spill, reload or copy made by Globregs / Color / RegSimpleAssign
  lower:<pass>                      instruction selection (Lower, LoFinalLower, Mul2Shift, AddrModes ...)
  loop:<pass>                       loop optimisation (strength reduction, hoisting, unrolling, rotation)
  kept:<why>                        a pass that removes such instructions ran and kept it (kept:alias R2/R3,
                                    kept:kill R5 (call/store between), kept:path R6, kept:type R1, kept:copy ...)
  structure                         front-end IL: no pass made or removed it (draft structure)

Examples:
  uv run tools/icount.py --c2 13.10 work/bench/mb/0047cfe0/tu.cpp --func setup_item_meshes \\
      --asm-vs work/bench/mb/0047cfe0/orig.s -- /O2 /MT /Gy /TP /I...
  uv run tools/icount.py tests/data/icount_cse.cpp --func f --asm-vs want.s -- /O2
"""
import argparse, collections, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace, c2il, tsweep, loadcse, c2merges, c2scratch  # noqa: E402
import unitcompose  # noqa: E402  (a composed unit, --unit: reports print the candidate's lines)
from c2trace import c2build  # noqa: E402

# The per-function pipeline (il_pipeline.md §5.3, opt.md §1) as hook points, in order. Each is a base name looked
# up in the build's index (any decoration); the ones a build lacks or that cannot be hooked are left out (listed by
# --show-passes). The dump at a pass's entry shows what the passes before it did; a change between two snapshots is
# credited to the interval that starts at the first (INTERVAL below names it).
PIPE = """MscRVO Inline FgSimpleFoldCmps FgDFOReorderLoopRecog OptFuncExpr GlobalOptimize GoptCanonicalize
GoptReverseCopyProp GdfRemoveDeadStores GoptSingleUseCopyProp LurUnroll GoptConstantFoldCompareBlk HshBuildTable
GdfInsertArtificialAssigns GdfInsertAssertions GoptRemoveAssumes LoptInitFunc LoptStrengthReduce
LoptOptimizeLandingPadPhase1 LoptLinearFunctionTestReplace LoptHoistFinalValues LoptTermFunc GdfCalcAvail
GoptLocalCopyPropagation GoptCleanUp GoptTermFunc ItfFuncInterf2 Half GrgIdentifyCandidates Opeq SizeOpt Mul2Shift
AddrModes MscBuildQuestion dop2_Codegen FgReturnValueMerge Lower MscProcessParams Peeps FgEarlyTailDup Globregs
Color RewriteAsReal RegSimpleAssign RegSimpleAssignBody Riscify MdmAvoidMOBStalls Sizeext MdmInsertEntryExit
FgDoLateFlowOpts LoFinalLower LateGlobalOptimize LoFinishFinalLower FgCodeMotion FgCoalesceLabels MscClean Schedule
MdmSuperHack FgLowerSwitches MdmEnsureNotEndInCall EncEmitFunc Encode""".split()
FINE = ["GoptOptimizeBlock", "GoptOptimizeLoop"]
# cl 17.00 x64 (docs/c2/icount.md, 17.00): names the x86 pipeline lacks, or whose base name is ambiguous there,
# by decorated name. GlobalOptimize runs the SSA optimiser (SsaOptimize) before value numbering and again between
# the assertions and sweep 1 (then WpaRangeGlobopt), GoptQuickConstantFoldCompare in place of 14.00's compare pass,
# and the vectoriser's cleanup (Vectorize_Finish: local copy propagation, OptFuncExpr, canonicalisation); allocation
# is NColor (which runs FgDFOReorderLoopRecogEx again from NColBlockInfo).
PIPE_X64 = [
    ("MscRVOProcessFunc", "?MscRVOProcessFunc@@YIXPAUtagFUNC@@@Z"),
    ("FgDFOReorderLoopRecogEx", "?FgDFOReorderLoopRecogEx@@YIXPAUtagFUNC@@HH@Z"),
    ("SsaOptimize", "?SsaOptimize@@YIXPAUtagFUNC@@HHHHHH@Z"),
    ("WpaRangeGlobopt", "?WpaRangeGlobopt@@YIXPAUtagFUNC@@@Z"),
    ("SsaOptimizeEnd", "?SsaOptimizeEnd@@YIXPAUtagFUNC@@@Z"),
    ("Vectorize", "?Vectorize@@YIXPAUtagFUNC@@@Z"),
    ("Vectorize_Finish", "?Vectorize_Finish@@YIXPAUtagFUNC@@@Z"),
    ("MscAvoidStoreForwardStalls", "?MscAvoidStoreForwardStalls@@YI_NPAUtagFUNC@@@Z"),
    ("GoptQuickConstantFoldCompare", "?GoptQuickConstantFoldCompare@@YIXPAUtagFUNC@@@Z"),
    ("NColor", "?NColor@@YIXPAUtagFUNC@@@Z"),
    ("Schedule", "?Schedule@@YIXPAUtagFUNC@@H@Z"),
    ("Encode", "?Encode@@YIXPAUtagFUNC@@@Z"),
]

# interval label of a snapshot's pass: (verdict class, what runs from this pass's entry to the next snapshot)
INTERVAL = {
    "MscRVO": ("front", "MscRVO / EH canonicalisation (reader IL)"),
    "Inline": ("inline", "Inline (callee bodies expanded)"),
    "FgSimpleFoldCmps": ("folded", "FgSimpleFoldCmps / FgBuildFlowGraph / ItfBuildInterference / dead arguments"),
    "FgDFOReorderLoopRecog": ("loop:rotation", "FgDFOReorderLoopRecog (loop recognition and rotation: the guard "
                                               "test is duplicated, LgZtripTest1)"),
    "OptFuncExpr": ("expr", "OptFuncExpr (expression rewrites: folding, reassociation, distribution)"),
    "GlobalOptimize": ("removed-by:GlobalOptimize/SimpleConstantFold", "GoptSimpleConstantFoldFunc"),
    "GoptCanonicalize": ("removed-by:GlobalOptimize/Canonicalize", "GoptCanonicalize"),
    "GoptReverseCopyProp": ("removed-by:GlobalOptimize/ReverseCopyProp", "GoptReverseCopyProp (t = e; x = t -> x = e)"),
    "GdfRemoveDeadStores": ("removed-by:GlobalOptimize/DeadStores", "GdfRemoveDeadStores (dead definitions and stores)"),
    "GoptSingleUseCopyProp": ("removed-by:GlobalOptimize/SingleUseCopyProp",
                              "GoptSingleUseCopyProp (a single-use definition substituted into its use)"),
    "LurUnroll": ("loop:unroll", "LurUnroll (loop unrolling)"),
    "GoptConstantFoldCompareBlk": ("folded:compare", "GoptConstantFoldCompareBlk (branches with a known outcome)"),
    "HshBuildTable": ("hash", "HshBuildTable (value numbering: every expression a hash temp; loads split out)"),
    "GdfInsertArtificialAssigns": ("removed-by:GlobalOptimize/sweep1", "GlobalOptimize sweep 1 (GoptOptimizeBlock: "
                                   "copy and constant propagation, constant and branch folding, redundant compare "
                                   "elimination from dominating tests; CSE off)"),
    "GdfInsertAssertions": ("removed-by:GlobalOptimize/sweep1", "GlobalOptimize sweep 1 (GoptOptimizeBlock: copy and "
                            "constant propagation, branch folding, redundant compare elimination; CSE off)"),
    "GoptRemoveAssumes": ("removed-by:GlobalOptimize/DeadStores", "GoptRemoveAssumes, dead stores, rebuilds"),
    "LoptInitFunc": ("loop:invariant", "loop phase: GoptOptimizeLoop, invariant code motion"),
    "LoptStrengthReduce": ("loop:strength-reduction", "LoptStrengthReduce (induction variables)"),
    "LoptOptimizeLandingPadPhase1": ("loop:landing-pad", "LoptOptimizeLandingPadPhase1"),
    "LoptLinearFunctionTestReplace": ("loop:test-replace", "LoptLinearFunctionTestReplace (down-counter, exit test)"),
    "LoptHoistFinalValues": ("loop:final-values", "LoptHoistFinalValues / LoptCleanUpLoop"),
    "LoptTermFunc": ("removed-by:GlobalOptimize/CSE", "GlobalOptimize sweep 2 (GoptOptimizeBlock with CSE)"),
    "GdfCalcAvail": ("removed-by:GlobalOptimize/CSE", "GlobalOptimize sweep 2 (GoptOptimizeBlock with CSE)"),
    "GoptLocalCopyPropagation": ("removed-by:GlobalOptimize/LocalCopyProp", "GoptLocalCopyPropagation"),
    "GoptCleanUp": ("removed-by:GlobalOptimize/CleanUp", "GoptCleanUp (hash temps with one def and use back to "
                                                         "expression temps)"),
    "GoptTermFunc": ("removed-by:GlobalOptimize/end", "GoptTermFunc, SymGarbageCollection, OptFuncExpr"),
    "ItfFuncInterf2": ("expr", "ItfFuncInterf2"),
    "Half": ("lower:Half", "Half"),
    "GrgIdentifyCandidates": ("lower:GrgIdentifyCandidates", "GrgIdentifyCandidates"),
    "Opeq": ("lower:Opeq", "Opeq (read-modify-write formation)"),
    "SizeOpt": ("lower:SizeOpt", "SizeOpt"),
    "Mul2Shift": ("lower:Mul2Shift", "Mul2Shift (multiplies to shifts / lea / magic numbers)"),
    "AddrModes": ("lower:AddrModes", "AddrModes, FgDoEarlyLateFlowOptsOnce (address modes, early flow opts)"),
    "MscBuildQuestion": ("lower:MscBuildQuestion", "MscBuildQuestion (?: to setcc / cmov shapes)"),
    "dop2_Codegen": ("lower:Codegen", "dop2_Codegen start: InlCollectInfo, FgReturnValueMerge"),
    "FgReturnValueMerge": ("merged:FgReturnValueMerge", "FgReturnValueMerge, parameter cleanup, FgInsertPragmas"),
    "Lower": ("lower:Lower", "Lower (instruction selection)"),
    "MscProcessParams": ("lower:MscProcessParams", "MscProcessParams, EmitFunction start"),
    "Peeps": ("peephole:Peeps", "Peeps, flow graph and loop graph rebuild"),
    "FgEarlyTailDup": ("merged:FgEarlyTailDup", "FgEarlyTailDup (tail duplication)"),
    "Globregs": ("regalloc:Globregs", "Globregs (global register candidates, enter loads)"),
    "Color": ("regalloc:Color", "Color (graph colouring, splits, spills)"),
    "RewriteAsReal": ("regalloc:RewriteAsReal", "RewriteAsReal, CodegenUpdate start"),
    "RegSimpleAssign": ("regalloc:RegSimpleAssign", "RegSimpleAssign (scratch registers, spill code)"),
    "RegSimpleAssignBody": ("regalloc:RegSimpleAssign", "RegSimpleAssignBody (scratch registers, spill code)"),
    "Riscify": ("riscify", "Riscify (13.10: memory operands split into loads), MdmAvoidMOBStalls, "
                           "RiscifyEarlyUndo"),
    "MdmAvoidMOBStalls": ("riscify-undo", "MdmAvoidMOBStalls, RiscifyEarlyUndo (13.10)"),
    "Sizeext": ("peephole:Sizeext", "Sizeext, frame setup, StkStackAlloc"),
    "MdmInsertEntryExit": ("lower:MdmInsertEntryExit", "MdmInsertEntryExit (prologue / epilogue), "
                                                       "FgDoLateFlowOpts (post-codegen)"),
    "FgDoLateFlowOpts": ("merged:FgDoLateFlowOpts", "FgDoLateFlowOpts (tail / head merges, cross jumps, branch "
                                                    "chains; `c2trace.py merges`)"),
    "FgDoLateFlowOptsOnce": ("merged:FgDoLateFlowOpts", "an iteration of FgDoLateFlowOpts (tail / head merges, cross "
                                                        "jumps, branch chains; `c2trace.py merges`)"),
    "LoFinalLower": ("lower:LoFinalLower", "LoFinalLower, FgRemoveAllDeadCodeAfterBranchOrReturn"),
    "LateGlobalOptimize": ("peephole:LateGlobalOptimize", "LateGlobalOptimize (SSA peepholes, dead code)"),
    "LoFinishFinalLower": ("lower:LoFinishFinalLower", "LoFinishFinalLower, FinalFlowOpts' FgDoLateFlowOpts"),
    "FgCodeMotion": ("merged:FgCodeMotion", "FgCodeMotion (block layout)"),
    "FgCoalesceLabels": ("merged:FgCoalesceLabels", "FgCoalesceLabels, dead code after branches"),
    "MscClean": ("peephole:MscClean", "MscClean (copy / reload / dead code cleanup after allocation)"),
    "Schedule": ("peephole:Schedule", "Schedule, Riscify_undo (13.10: splits put back)"),
    "MdmSuperHack": ("peephole:MdmSuperHack", "MdmSuperHack, SsePeep"),
    "FgLowerSwitches": ("lower:FgLowerSwitches", "FgLowerSwitches"),
    "MdmEnsureNotEndInCall": ("lower:Encode", "MdmEnsureNotEndInCall, EncEmitFunc start"),
    "EncEmitFunc": ("lower:EncEmitFunc", "EncEmitFunc (late intrinsics, EH funclets, branch lengthening)"),
    "Encode": ("final", "Encode"),
    "GoptOptimizeBlock": ("removed-by:GlobalOptimize/sweep", "GoptOptimizeBlock of one block"),
    # 17.00 x64 (PIPE_X64)
    "MscRVOProcessFunc": ("front", "MscRVOProcessFunc / EH canonicalisation (reader IL)"),
    "FgDFOReorderLoopRecogEx": ("loop:rotation", "FgDFOReorderLoopRecogEx (loop recognition and rotation: the guard "
                                                 "test is duplicated)"),
    "SsaOptimize": ("removed-by:GlobalOptimize/SSA", "SsaOptimize (the SSA optimiser: renaming with copy and constant "
                    "propagation, partial redundancy (SsaPartiallyAvailable), range checks, aliased rewrite)"),
    "WpaRangeGlobopt": ("folded:range-check", "WpaRangeGlobopt (compares decided from value ranges)"),
    "SsaOptimizeEnd": ("removed-by:GlobalOptimize/SSA", "SsaOptimizeEnd (out of SSA)"),
    "Vectorize": ("loop:vectorize", "Vectorize (the loop vectoriser)"),
    "Vectorize_Finish": ("loop:vectorize", "Vectorize_Finish (local copy propagation, OptFuncExpr, canonicalisation "
                                           "after the vectoriser's pass)"),
    "MscAvoidStoreForwardStalls": ("lower:MscAvoidStoreForwardStalls", "MscAvoidStoreForwardStalls, then the "
                                   "second dead-store and single-use copy propagation round"),
    "GoptQuickConstantFoldCompare": ("folded:compare", "GoptQuickConstantFoldCompare (branches with a known outcome, "
                                                       "before value numbering), dead stores"),
    "NColor": ("regalloc:NColor", "NColor (x64 allocation: candidates, regions, spill code, rewrite to registers)"),
    "GoptOptimizeLoop": ("loop:invariant", "GoptOptimizeLoop of one loop (invariant code motion, LoptOptimizeLoop)"),
}
# what removes an instruction of a kind (EXTRA: which of these ran after the tuple was born and kept it)
REMOVERS = {
    "load": ["GoptOptimizeBlock sweep 2 CSE", "MscClean", "LateGlobalOptimize"],
    "store": ["GdfRemoveDeadStores"],
    "copy": ["GoptSingleUseCopyProp", "GoptLocalCopyPropagation", "Color (coalescing)", "MscClean"],
    "cmp": ["GoptOptimizeBlock sweep 1 (redundant compare elimination)", "GoptConstantFoldCompareBlk"],
    "jcc": ["GoptConstantFoldCompareBlk", "FgDoLateFlowOpts"],
    "jmp": ["FgDoLateFlowOpts (tail merge, branch chains)", "FgCodeMotion"],
}
ALLOW_LOCAL = []


def build_key():
    return c2build.name()


# ---------------------------------------------------------------- passes and the trace
def resolve(img, names):
    """[(short name, va, symbol)] of the names this build has (one function per base name) and can hook."""
    out, seen = [], set()
    for n in names:
        pat = re.compile(r"^[@_?]%s@" % re.escape(n))
        hit = sorted({r["va"]: k for k, r in img.by_sym.items() if pat.match(k)}.items())
        if len(hit) != 1:
            continue
        va, sym = hit[0]
        if va in seen:
            continue
        try:
            c2trace.steal_len(img, va)
            c2trace.parse_hook(img, "%x" % va)
        except SystemExit:
            continue
        seen.add(va)
        out.append((n, va, sym))
    return out


def resolve_x64(img, passes):
    """passes + PIPE_X64 (exact decorated names; snapshots are ordered by the log, not by this list)."""
    seen = {va for _, va, _ in passes}
    out = list(passes)
    # a function the build table names is indexed under its table key (`@Encode@4`), not its PDB name
    keyof = {v[3]: k for k, v in (c2build.get("functions") or {}).items() if len(v) > 3}
    for n, sym in PIPE_X64:
        r = img.by_sym.get(sym) or img.by_sym.get(keyof.get(sym, ""))
        if r is None or r["va"] in seen:
            continue
        try:
            c2trace.steal_len(img, r["va"])
            c2trace.parse_hook(img, "%x" % r["va"])
        except SystemExit:
            continue
        seen.add(r["va"])
        out.append((n, r["va"], sym))
    return out


def il_spec():
    return c2il.SPEC


def run(src, func, cl, fine=False, keep=None):
    """-> (passes [(short, va, sym)], snapshots [Snap], listing path, rc, cl output)"""
    from image import Image
    img = Image("c2")
    names = PIPE + (FINE if fine else [])
    passes = resolve(img, names)
    if c2build.get("arch") == "x64":
        passes = resolve_x64(img, passes)
    spec = il_spec()
    # the late flow optimiser's own events first (tools/c2merges.py: which tuples a merge matched), hook ids
    # 0..len(KEYS)-1; its FgDoLateFlowOptsOnce and Encode hooks dump the IL and double as snapshots
    mtexts = []
    if c2build.name() in c2merges.BUILDS:
        mh = dict(c2merges.BUILDS[c2build.name()])
        mh["once"] = mh["once"] + spec
        enc = next((va for n, va, _ in passes if n == "Encode"), None)
        mh["final"] = ("%x" % enc + spec) if enc else c2merges.hooks_for(c2build.name())[c2merges.KEYS.index("final")]
        mtexts = [mh[k] for k in c2merges.KEYS]
        taken = set()
        for t in mtexts:
            h = c2trace.parse_hook(img, t)
            taken.add(h["va"])
        passes = [p_ for p_ in passes if p_[1] not in taken]
    texts = mtexts + ["%x%s" % (va, spec) for _, va, _ in passes]
    os.environ["C2TRACE_ILPAS"] = "1"           # memory operands print their PAS index (loadcse's R2)
    real = c2trace.run_remote

    def guarded(args, cfg, log):                 # traced compiles go to the compile server, never wine
        r = real(args, cfg, log)
        if r is None and not ALLOW_LOCAL and c2trace.remote_configured():
            sys.exit("icount: the compile server did not take this compile (C2TRACE_REMOTE_DEBUG=1 says why); "
                     "not compiling under wine (--allow-local does)")
        return r
    c2trace.run_remote = guarded
    if keep:
        kd = keep
    else:   # one directory per run: two runs on one source (other builds, other lanes) must not share it
        import atexit, shutil, tempfile
        base = os.path.join(c2trace.ROOT, "work", "icount", "k")
        os.makedirs(base, exist_ok=True)
        kd = tempfile.mkdtemp(prefix=tsweep.out_name(src) + "-", dir=base)
        atexit.register(shutil.rmtree, kd, True)
    try:
        img, hk, rc, out, log = c2trace.run_trace(src, texts, cl, keep=kd, quiet=True, func=func)
    finally:
        c2trace.run_remote = real
    nm = len(mtexts)
    snap_ids = set(range(nm, len(hk)))
    if nm:
        snap_ids |= {c2merges.KEYS.index("once"), c2merges.KEYS.index("final")}
    snaps = parse_snaps(log, hk, passes, func, snap_ids)
    merges = None
    if nm:
        sub = []
        keep_t = False
        for ln in log.splitlines():                  # the merge events alone, as c2merges.parse reads them
            if ln[:2] in ("E ", "X ") and "|" in ln:
                keep_t = int(ln.split()[3]) < nm
                if keep_t:
                    sub.append(ln)
            elif ln.startswith("T ") and keep_t:
                sub.append(ln)
            elif ln.startswith("# thread "):         # cl 17.00's worker threads: c2merges.parse demuxes by them
                sub.append(ln)
        try:
            merges = [g for g in c2merges.parse("\n".join(sub), hk[:nm]) if c2trace.func_match(func, g["name"])]
        except Exception as e:                          # never let the merge model take the report down
            merges = {"error": repr(e)}
    stem = os.path.splitext(os.path.basename(src))[0]
    return passes, snaps, os.path.join(kd, stem + ".asm"), rc, out, merges


class Snap:
    def __init__(self, idx, name, occ, lines, fn):
        self.idx, self.name, self.occ, self.lines, self.fn = idx, name, occ, lines, fn
        self._stage = None
        self.ins = []
        for ln in lines:
            if ln.startswith("I "):
                head, _, rest = ln.partition(" | ")
                hp = head.split()
                if len(hp) < 5:
                    continue
                notes = ""
                if " ; " in rest:
                    rest, _, notes = rest.partition(" ; ")
                d, _, s = rest.partition("<-")
                self.ins.append(dict(tup=hp[1], op=hp[2], line=int(hp[3]) if hp[3].lstrip("-").isdigit() else 0,
                                     len=int(hp[4]) if hp[4].isdigit() else 0, dsts=loadcse.split_ops(d),
                                     srcs=loadcse.split_ops(s), notes=notes, text=ln, pos=len(self.ins)))
        self.by_tup = {i["tup"]: i for i in self.ins}

    @property
    def label(self):
        return "%s%s" % (self.name, "#%d" % self.occ if self.occ > 1 else "")

    def stage(self):
        if self._stage is None:
            self._stage = loadcse.Stage(self.lines)
        return self._stage


def parse_snaps(log, hooks, passes, func, snap_ids=None):
    short = {va: n for n, va, _ in passes}
    for h in hooks:                                     # the merge hooks that double as snapshots
        base = re.sub(r"^[@?_]|@.*$", "", h["name"])
        if base in ("FgDoLateFlowOptsOnce", "Encode"):
            short.setdefault(h["va"], base)
    out, cur, occ = [], None, collections.Counter()
    for ln in log.splitlines():
        if ln.startswith("T "):
            if cur is not None:
                cur[1].append(ln[2:])
            continue
        if not ln.startswith("E "):
            continue
        head, _, rest = ln.partition("|")
        hid = int(head.split()[3])
        if snap_ids is not None and hid not in snap_ids:
            cur = None
            continue
        hk = hooks[hid]
        name = rest.split('"')[1] if '"' in rest else "?"
        if not c2trace.func_match(func, name):
            cur = None
            continue
        cur = (short.get(hk["va"], hk["name"]), [], name)
        out.append(cur)
    snaps = []
    fns = collections.Counter(fn for _, _, fn in out)
    main = fns.most_common(1)[0][0] if fns else None
    for n, lines, fn in out:
        if fn != main:
            continue
        occ[n] += 1
        snaps.append(Snap(len(snaps), n, occ[n], lines, fn))
    return snaps


# ---------------------------------------------------------------- tuple histories
class History:
    """Per tuple: the snapshots it is present in. Tuple memory is reused once a tuple is freed, so an address names a
    new tuple when its source line changes, or when it was absent from a snapshot and comes back as another opcode
    (a short gap with the same line and opcode is a partial stream while the flow graph is rebuilt: bridged)."""
    def __init__(self, snaps):
        self.snaps = snaps
        self.lives = collections.defaultdict(list)          # tup -> [[snapshot idx, ...], ...]
        for s in snaps:
            for i in s.ins:
                L = self.lives[i["tup"]]
                if L:
                    last = L[-1][-1]
                    j = snaps[last].by_tup[i["tup"]]
                    same = j["line"] == i["line"] and (last == s.idx - 1 or j["op"] == i["op"])
                    if same:
                        L[-1].append(s.idx)
                        continue
                L.append([s.idx])
        self._at = {}
        for t, L in self.lives.items():
            for life in L:
                for k in life:
                    self._at[(t, k)] = life

    def life(self, tup, k):
        return self._at.get((tup, k))

    def born(self, tup, k=None):
        L = self.lives.get(tup)
        if not L:
            return None
        life = self.life(tup, k) if k is not None else L[-1]
        return life[0] if life else None

    def last(self, tup, k=None):
        L = self.lives.get(tup)
        if not L:
            return None
        life = self.life(tup, k) if k is not None else L[-1]
        return life[-1] if life else None

    def changes(self, tup, k=None):
        """[(snapshot idx where the text is new, old text, new text)] (opcode and operands; line and len masked)"""
        out, prev = [], None
        life = self.life(tup, k) if k is not None else (self.lives.get(tup) or [[]])[-1]
        for x in life or ():
            i = self.snaps[x].by_tup[tup]
            t = norm(i)
            if prev is not None and t != prev:
                out.append((x, prev, t))
            prev = t
        return out


def norm(i):
    t = re.sub(r"^I \S+ (\S+) -?\d+ \d+ \|", r"\1 |", i["text"])
    return re.sub(r"@[0-9a-f]+(?=:)", "", t)


def kind_of(mn, ops):
    """instruction kind of a machine instruction (mnemonic, capstone operand text)"""
    o = ops.lower()
    if mn == "mov" or mn in ("movzx", "movsx"):
        a, _, b = o.partition(",")
        if "[" in b:
            return "load"
        if "[" in a:
            return "store"
        if re.fullmatch(r"\s*(?:0x[0-9a-f]+|\d+)\s*", b):
            return "const"
        return "copy"
    if mn in ("cmp", "test"):
        return "cmp"
    if mn.startswith("j") and mn != "jmp":
        return "jcc"
    if mn == "jmp":
        return "jmp"
    if mn in ("push", "pop"):
        return mn
    if mn == "call":
        return "call"
    if mn in ("ret", "retn"):
        return "ret"
    if mn in ("add", "sub") and o.startswith("esp,"):
        return "stack"
    if mn == "lea":
        return "lea"
    if mn.startswith("f"):
        return "x87"
    if mn.startswith("set"):
        return "setcc"
    return "alu"


IL_KIND = {"OPCMP": "cmp", "OPBRANCH": "jcc", "OPGOTO": "jmp", "OPCALL": "call", "OPARG": "push", "OPLOAD": "load",
           "cmp": "cmp", "test": "cmp", "_jcc": "jcc", "jmp": "jmp", "call": "call", "push": "push", "pop": "pop",
           "lea": "lea", "OPRET": "ret", "ret": "ret", "_eret": "ret", "OPQUESTION": "setcc"}


def il_kind(i):
    op = i["op"]
    if op in IL_KIND:
        return IL_KIND[op]
    if op.startswith("j"):
        return "jcc"
    if op in ("OPASSIGN", "mov", "OPMBASSIGN", "movzx", "movsx", "OPCONVERT", "OPCVT"):
        d = i["dsts"][0] if i["dsts"] else ""
        s = i["srcs"][0] if i["srcs"] else ""
        if d.startswith("["):
            return "store"
        if s.startswith("[") and "s0=&" not in i["notes"]:
            return "load"
        if loadcse.is_const(s):
            return "const"
        return "copy"
    if op.startswith("f") or op.startswith("OPF"):
        return "x87"
    return "alu"


COMPAT = {"load": {"load", "copy"}, "copy": {"copy", "load", "const"}, "cmp": {"cmp"},
          "jcc": {"jcc"}, "jmp": {"jmp", "jcc"}, "push": {"push"}, "store": {"store"},
          "const": {"const", "copy"}, "alu": {"alu", "lea"}, "lea": {"lea", "alu"}, "call": {"call"},
          "setcc": {"setcc", "alu", "cmp"}, "x87": {"x87"}, "pop": {"pop"}, "ret": {"ret"}, "stack": {"stack"}}
# GlobalOptimize's assertion tuples (GdfInsertAssertions: `t = x != 0` on a branch arm), removed again by
# GoptRemoveAssumes: never an instruction of the final code
ASSERT_OPS = {"OPNE", "OPEQ", "OPLT", "OPLE", "OPGT", "OPGE", "OPULT", "OPULE", "OPUGT", "OPUGE", "OPASSUME"}
# never code: SSA phis. GlobalOptimize's SsaOptimize adds them before LurUnroll whenever LurWorthIt passes (/O2 and
# the function has loops) and LurEndUnroll's SsaOptimizeEnd removes them whether or not a loop was unrolled, so a
# phi removed in the LurUnroll interval is no evidence of unrolling (M&B 0x532de0: two loops with calls, none
# unrolled; opt.md §8.7)
NOCODE_OPS = {"OPPHI"}


# ---------------------------------------------------------------- alignment
def _mask(k):
    """bdiff key with registers masked: mnemonic and operand kinds"""
    out = [k[0]]
    for o in k[1:]:
        if "[" in o:
            out.append("m" + re.sub(r"[a-z]{2,3}(?=[\]+*])", "r", o.split("[", 1)[1]))
        elif tsweep.REG.fullmatch(o):
            out.append("r8" if len(o) == 2 and o[1] in "lh" else "r")
        else:
            out.append("i" if o not in ("A", "T", "L") else o)
    return tuple(out)


def _nodisp(k):
    """masked key without stack displacements (a moved frame slot is the same instruction)"""
    return tuple(re.sub(r"[+-]\d+(?=\])", "", x) if x.startswith("m") and ("esp" in x or "r" in x) else x
                 for x in k)


def align(a, b, kinds_a=None, kinds_b=None):
    """pairs [(i, j)] of a global alignment of bdiff key lists a, b that maximises the summed similarity: exact key
    4, same registers 3, registers masked 2, stack slots masked too 1.5, the same mnemonic and kind 0.5; anything
    else does not pair. Unlike difflib's longest-block-first matching this pairs a whole register-exchanged region
    instead of anchoring on a few equal instructions elsewhere."""
    n, m = len(a), len(b)
    fa = [(x, tsweep._bshape(x), _mask(x), _nodisp(_mask(x)), x[0]) for x in a]
    fb = [(x, tsweep._bshape(x), _mask(x), _nodisp(_mask(x)), x[0]) for x in b]
    ka = kinds_a or [None] * n
    kb = kinds_b or [None] * m

    def sim(i, j):
        x, y = fa[i], fb[j]
        if x[0] == y[0]:
            return 4.0
        if x[1] == y[1]:
            return 3.0
        if x[2] == y[2]:
            return 2.0
        if x[3] == y[3]:
            return 1.5
        if x[4] == y[4] and ka[i] == kb[j] and len(a[i]) == len(b[j]):
            return 0.5
        return None
    prev = [0.0] * (m + 1)
    back = []
    for i in range(1, n + 1):
        cur = [0.0] * (m + 1)
        bk = bytearray(m + 1)
        for j in range(1, m + 1):
            best, how = prev[j], 1                 # 1: skip a[i-1]
            if cur[j - 1] > best:
                best, how = cur[j - 1], 2          # 2: skip b[j-1]
            s_ = sim(i - 1, j - 1)
            if s_ is not None and prev[j - 1] + s_ > best:
                best, how = prev[j - 1] + s_, 3
            cur[j], bk[j] = best, how
        back.append(bk)
        prev = cur
    pairs, i, j = [], n, m
    while i > 0 and j > 0:
        h = back[i - 1][j]
        if h == 3:
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif h == 1:
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


def listing_lines(asm, func):
    """{listing offset: (source line, text)} from our /FAsc listing's `; NNN : text` comments"""
    text = open(asm, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)
    out, cur = {}, None
    for ln in text[i:j].splitlines():
        m = re.match(r";\s+(\d+)\s*:\s?(.*)$", ln)
        if m:
            cur = (int(m.group(1)), m.group(2).strip())
            continue
        m = re.match(r"\s+([0-9a-f]{5})\t", ln)
        if m and cur:
            out.setdefault(int(m.group(1), 16), cur)
    return out


# ---------------------------------------------------------------- analysis
class Analysis:
    def __init__(self, func, snaps, asm, orig_path, asm_func=None):
        self.func, self.snaps = func, snaps
        self.hist = History(snaps)
        enc = [s for s in snaps if s.name == "Encode"] or [s for s in snaps if s.name == "EncEmitFunc"]
        if not enc:
            sys.exit("icount: no IL at Encode / EncEmitFunc for %s" % func)
        self.final = max(enc, key=lambda s: len(s.ins))
        self.final_idx = self.final.idx
        off, self.tup_at = 0, {}
        for i in self.final.ins:
            if i["len"]:
                self.tup_at.setdefault(off, i["tup"])
            off += i["len"]
        self.ours = tsweep.listing_instrs(asm, asm_func or func)
        if orig_path.startswith("listing:"):           # --vs-func: another function of the same compile
            self.orig = tsweep.listing_instrs(asm, orig_path[8:])
        else:
            self.orig = tsweep.orig_instrs(orig_path)
        self.srcl = listing_lines(asm, asm_func or func)
        self.ka, self.at_a, _ = tsweep._bside(self.ours, getattr(self.orig, "va0", None))
        self.kb, self.at_b, _ = tsweep._bside(self.orig, getattr(self.orig, "va0", None))
        self.off_of = {k: va for va, k in sorted(self.at_a.items(), reverse=True)}
        self.va_of = {k: va for va, k in sorted(self.at_b.items(), reverse=True)}
        main_end = len(self.ka)
        if self.ours.addrs:                    # EH funclets after the body: their offsets restart
            for k in range(1, len(self.ka)):
                o0, o1 = self.off_of.get(k - 1), self.off_of.get(k)
                if o0 is not None and o1 is not None and o1 < o0:
                    main_end = k
                    break
        self.main_end = main_end
        self.pairs = align(self.ka[:main_end], self.kb, [kind_of(*self.mn_ops_a(k)) for k in range(main_end)],
                           [kind_of(*self.mn_ops_b(k)) for k in range(len(self.kb))])
        self.base = self._line_base()
        self.lc = None

    # -- helpers
    def text_a(self, k):
        return " ".join(x for x in self.ka[k] if x) if k < len(self.ka) else "?"

    def cs_a(self, k):
        d = self.ours.decoded[self._dec_a(k)] if self.ours.decoded else None
        return (d[0] + " " + d[1]).strip() if d else self.text_a(k)

    def _dec_a(self, k):
        off = self.off_of.get(k)
        return self.ours.addrs.index(off) if off is not None and self.ours.addrs else k

    def cs_b(self, k):
        va = self.va_of.get(k)
        if va is not None and self.orig.addrs and va in self.orig.addrs:
            n = self.orig.addrs.index(va)
            d = self.orig.decoded[n] if getattr(self.orig, "listing", False) and self.orig.decoded else None
            x = (d[0], d[1]) if d else self.orig[n]
            return ("%s %s" % x).strip()
        return " ".join(self.kb[k])

    def mn_ops_a(self, k):
        t = self.cs_a(k).split(None, 1)
        return t[0], t[1] if len(t) > 1 else ""

    def mn_ops_b(self, k):
        t = self.cs_b(k).split(None, 1)
        return t[0], t[1] if len(t) > 1 else ""

    def tup_of(self, k):
        off = self.off_of.get(k)
        return self.tup_at.get(off) if off is not None else None

    def line_of(self, k):
        off = self.off_of.get(k)
        if off is None:
            return None
        best = None
        for o in sorted(self.srcl):
            if o <= off:
                best = self.srcl[o]
            else:
                break
        return best

    def _line_base(self):
        """listing source line minus the tuple's line offset, by majority over the final tuples"""
        c = collections.Counter()
        for k in range(self.main_end):
            t, ln = self.tup_of(k), self.line_of(k)
            i = self.final.by_tup.get(t) if t else None
            if i and ln and self.off_of.get(k) in self.srcl:
                c[ln[0] - i["line"]] += 1
        return c.most_common(1)[0][0] if c else 0

    def src_line(self, tup_line):
        return self.base + tup_line

    def src_text(self, n):
        for v in self.srcl.values():
            if v[0] == n:
                return v[1]
        return ""

    # -- the diff
    def runs(self):
        """[(kind 'extra'|'missing', [indices]), with the aligned neighbours (ours before, ours after)]"""
        ma = {i for i, _ in self.pairs}
        mb = {j for _, j in self.pairs}
        out = []
        gaps = [(-1, -1)] + self.pairs + [(self.main_end, len(self.kb))]
        for (i0, j0), (i1, j1) in zip(gaps, gaps[1:]):
            ex = [i for i in range(i0 + 1, i1) if i not in ma and self.ka[i] != ("npad",)]
            mi = [j for j in range(j0 + 1, j1) if j not in mb and self.kb[j] != ("npad",)]
            if ex:
                out.append(dict(kind="extra", idx=ex, prev=i0, next=i1, oprev=j0, onext=j1))
            if mi:
                out.append(dict(kind="missing", idx=mi, prev=i0, next=i1, oprev=j0, onext=j1))
        return out

    def counts(self):
        na = sum(1 for k in self.ka[:self.main_end] if k != ("npad",))
        nb = sum(1 for k in self.kb if k != ("npad",))
        return na, nb

    # -- extra
    def explain_extra(self, k):
        mn, ops = self.mn_ops_a(k)
        kind = kind_of(mn, ops)
        t = self.tup_of(k)
        r = dict(ins=self.cs_a(k), off=self.off_of.get(k), kind=kind, tuple=t)
        ln = self.line_of(k)
        if ln:
            r["line"] = ln[0]
            r["src"] = ln[1]
        if not t:
            r.update(verdict="unknown", why="no tuple at this listing offset in the IL at Encode")
            return r
        b = self.hist.born(t, self.final_idx)
        s0 = self.snaps[b]
        i0 = s0.by_tup[t]
        r["born"] = s0.label
        r["born_as"] = norm(i0)
        ch = self.hist.changes(t, self.final_idx)
        r["history"] = ["%s: %s" % (self.snaps[c].label, new) for c, _, new in ch[-6:]]
        if b == 0 or s0.name == "Inline":
            import feshape
            sh = feshape.shape(i0["text"])
            if sh:
                r["fe"] = "%s:%s:-1" % ("fe" if s0.name == "MscRVO" or b == 0 else "fi", sh)
        if b == 0:
            origin = ("front end" if s0.name in ("MscRVO", "Inline") else "first dump (%s)" % s0.label)
            cls = "structure"
        else:
            p = self.snaps[b - 1]
            cls, desc = self.interval(b - 1)
            origin = "made between %s and %s: %s" % (p.label, s0.label, desc)
            if cls.startswith("removed-by:") or cls in ("hash", "expr", "front", "folded", "folded:compare"):
                cls = "structure" if cls in ("front",) else cls
        r["origin"] = origin
        # which passes that remove this kind ran after the birth and kept it
        why, vcls = self.kept_reason(t, kind, b)
        if b > 0 and not self.snaps[b - 1].name.startswith(("Inline", "MscRVO")):
            c0 = self.interval(b - 1)[0]
            if c0.startswith(("riscify", "regalloc", "lower", "loop", "peephole", "merged")):
                vcls = vcls or ("made-by:" + c0)
        if s0.name == "Inline" and b > 0 or (b > 0 and self.snaps[b - 1].name == "Inline"):
            origin = "inlined body (Inline)"
            vcls = vcls or "structure:inline"
        r["verdict"] = vcls or cls if (vcls or cls) != "structure" or b == 0 else "structure"
        if i0["op"] == "OPENTERLOAD" or "OPENTERLOAD" in r.get("born_as", ""):
            r["verdict"] = "regalloc:enterload"
            why = ("Globregs made `%s` a register candidate and loads it on entry to its live range (OPENTERLOAD); "
                   "the original keeps it in memory there or loads it elsewhere: `c2trace.py regs --why`" %
                   (i0["dsts"][0].split(":")[0] if i0["dsts"] else "?"))
        if kind == "x87":
            tail = r["verdict"].split(":")[-1]
            r["verdict"] = "x87:" + ("Schedule" if tail == "riscify-undo" else tail)  # Riscify leaves x87 alone
            why = (why or "") + ("; x87 stack shape (fld / fxch / fstp order): tools/c2x87.py, docs/c2/x87.md")
        if r["verdict"] in ("removed-by:GlobalOptimize/sweep1", "removed-by:GlobalOptimize/CSE", "hash"):
            r["verdict"] = "made-by:GlobalOptimize"        # a tuple GlobalOptimize created (a hash temp's load)
        if why:
            r["why"] = why
        return r

    def gopt_phase(self, k):
        """where snapshot k lies in GlobalOptimize: pre (before value numbering), sweep1, loop, sweep2 (CSE), None"""
        ph = None
        for x in self.snaps[:k + 1]:
            n = x.name
            if n == "GlobalOptimize":
                ph = "pre"
            elif n in ("HshBuildTable",) and ph in ("pre", None):
                ph = "hash"
            elif n in ("GdfInsertArtificialAssigns", "GdfInsertAssertions") and ph in ("hash", "pre"):
                ph = "sweep1"
            elif n == "LoptInitFunc" and ph in ("sweep1", "hash"):
                ph = "loop"
            elif n == "LoptTermFunc":
                ph = "sweep2"
            elif n in ("GoptLocalCopyPropagation", "GoptCleanUp", "GoptTermFunc"):
                ph = None
        return ph

    def interval(self, k):
        s = self.snaps[k]
        cls, desc = INTERVAL.get(s.name, ("?", s.name))
        ph = self.gopt_phase(k)
        if s.name in ("SsaOptimize", "SsaOptimizeEnd") and ph is None and \
                next((x.name for x in reversed(self.snaps[:k]) if x.name in ("GlobalOptimize", "LateGlobalOptimize")),
                     None) == "LateGlobalOptimize":       # 17.00: LateGlobalOptimize runs the SSA optimiser too
            cls, desc = "peephole:LateGlobalOptimize", "LateGlobalOptimize (its SSA pass: %s)" % s.name
        elif s.name == "FgDFOReorderLoopRecogEx" and any(x.name == "NColor" for x in self.snaps[:k]):
            cls, desc = "regalloc:NColor", "NColor (from its loop recognition in NColBlockInfo on)"
        elif s.name in ("GoptOptimizeBlock", "GoptConstantFoldCompareBlk", "FgCoalesceLabels", "GdfCalcAvail",
                        "GdfInsertAssertions", "GdfInsertArtificialAssigns", "GoptRemoveAssumes", "SsaOptimizeEnd") and \
                ph in ("sweep1", "sweep2"):
            if ph == "sweep1":
                cls = "removed-by:GlobalOptimize/sweep1"
                desc = ("SsaOptimizeEnd (out of SSA), then " if s.name == "SsaOptimizeEnd" else "") + (
                        "GlobalOptimize sweep 1 (GoptOptimizeBlock per block: copy and constant propagation, constant "
                        "and branch folding, compares decided by dominating tests (assertions); CSE off)")
            else:
                cls = "removed-by:GlobalOptimize/CSE"
                desc = "GlobalOptimize sweep 2 (GoptOptimizeBlock per block with CSE: redundant loads and expressions)"
        elif s.name == "GoptConstantFoldCompareBlk":
            desc = "GoptConstantFoldCompareBlk (branches with a known outcome, before value numbering)"
        elif s.name == "Schedule" and build_key().startswith("13.10"):
            cls = "riscify-undo"
            desc = ("Schedule, then Riscify_undo (13.10: a split memory operand put back into its use, as a new "
                    "tuple, unless the scheduler separated them; regalloc.md 6.6)")
        nxt = self.snaps[k + 1].label if k + 1 < len(self.snaps) else "end"
        return cls, "%s [%s .. %s]" % (desc, s.label, nxt)

    def loadcse_report(self):
        if self.lc is None:
            got = {}
            want = {"V": "GdfInsertArtificialAssigns", "S2": "GoptLocalCopyPropagation", "O": "Opeq",
                    "C": "Color", "E": "EncEmitFunc"}
            for tag, n in want.items():
                s = next((x for x in self.snaps if x.name == n), None)
                if s:
                    got[tag] = s.lines
            try:
                self.lc = loadcse.analyse(self.func, got, all_locs=True)
            except Exception as e:                    # never let the load model take the report down
                self.lc = {"locations": [], "rmw": [], "error": repr(e)}
        return self.lc

    def kept_reason(self, t, kind, born):
        """(why, verdict) for an extra instruction of this kind whose tuple is t"""
        if kind == "load":
            lc = self.loadcse_report()
            anc = self.ancestors(t)
            locs = self.locations_of(t)
            for loc in lc["locations"]:
                for ld in loc["loads"]:
                    if ld["tuple"] in anc:
                        st = ld["status"]
                        if st.startswith("RELOADED"):
                            rule = st.split()[1]
                            v = {"R1": "kept:type", "R2": "kept:alias", "R3": "kept:alias", "R4": "kept:alias",
                                 "R5": "kept:kill", "R6": "kept:path"}.get(rule, "kept:load")
                            return ("CSE (sweep 2) kept this reload of %s: %s %s" % (
                                loc["location"], st.replace("RELOADED ", ""), ld["why"]), v)
                        if st == "FIRST":
                            rl = [x for x in loc["loads"] if x["status"].startswith("RELOADED")]
                            if rl:
                                rule = rl[0]["status"].split()[1]
                                v = {"R1": "kept:type", "R2": "kept:alias", "R3": "kept:alias", "R4": "kept:alias",
                                     "R5": "kept:kill", "R6": "kept:path"}.get(rule, "kept:load")
                                return ("first of %d loads of %s; CSE (sweep 2) kept the reload (tuple %s): %s %s" % (
                                    len(loc["loads"]), loc["location"], rl[0]["tuple"],
                                    rl[0]["status"].replace("RELOADED ", ""), rl[0]["why"]), v)
                            k = self.kill_between(t, {loc["location"]})
                            if k:
                                return k
                            return ("first load of %s (loadcse: %s)" % (loc["location"], ld.get("why") or "FIRST"),
                                    None)
            for loc in lc["locations"]:                 # not a load of its own at V: another read of its location
                if loc["location"] not in locs:
                    continue
                rl = [ld for ld in loc["loads"] if ld["status"].startswith("RELOADED")]
                if rl:
                    rule = rl[0]["status"].split()[1]
                    v = {"R1": "kept:type", "R2": "kept:alias", "R3": "kept:alias", "R4": "kept:alias",
                         "R5": "kept:kill", "R6": "kept:path"}.get(rule, "kept:load")
                    return ("%s is read %d times; CSE (sweep 2) kept a reload (tuple %s): %s %s" % (
                        loc["location"], len(loc["loads"]), rl[0]["tuple"], rl[0]["status"].replace("RELOADED ", ""),
                        rl[0]["why"]), v)
            k = self.kill_between(t, locs)
            if k:
                return k
            return ("not a GlobalOptimize load (no OPLOAD ancestor at HshBuildTable): made later, e.g. a memory "
                    "operand split off (Riscify / Lower) or a spill reload", None)
        if kind == "copy":
            return ("register copy: kept by copy propagation / Color's coalescing (regs --why on both registers)",
                    None)
        if kind in ("cmp", "jcc"):
            return ("compare / branch not folded: no dominating test of the same value decides it", None)
        if kind == "jmp":
            return ("jump not merged away: `c2trace.py merges --off %x` shows the tail-merge decisions" %
                    (self.off_of.get(self._idx_of_tup(t), 0) or 0), None)
        return None, None

    def kill_between(self, t, locs):
        """(why, verdict) when another read of one of `locs` precedes or follows t in the IL at V with a call or a
        store that may alias in between (opt.md §5): the two reads cannot be one load"""
        v = next((x for x in self.snaps if x.name == "GdfInsertArtificialAssigns"), None)
        if v is None or not locs:
            return None
        st = v.stage()
        reads = []
        for i in st.ins:
            for x in i.srcs + [d for d in i.dsts if d.startswith("[")][:0]:
                if x.startswith("[") and "s0=&" not in i.notes:
                    try:
                        if st.memkey(x, i.idx)[0] in locs:
                            reads.append(i)
                            break
                    except Exception:
                        pass
        me = next((st.by_tup[a_] for a_ in self.ancestors(t) if a_ in st.by_tup and st.by_tup[a_] in reads), None)
        if me is None or len(reads) < 2:
            return None
        k = reads.index(me) if me in reads else None
        if k is None:
            return None
        for a_, b_ in ((reads[k - 1], me) if k else (None, None), (me, reads[k + 1]) if k + 1 < len(reads) else (None, None)):
            if a_ is None:
                continue
            for i in st.ins[a_.idx + 1:b_.idx]:
                if i.op in ("OPCALL", "call"):
                    return ("%s is read again after a call (%s) between the reads: a call kills every memory value "
                            "(opt.md §5)" % (sorted(locs)[0], i.notes.replace("s0=", "") or "?"), "kept:kill")
                if i.dsts and i.dsts[0].startswith("["):
                    sk = st.memkey(i.dsts[0], i.idx)[0]
                    loc = sorted(locs)[0]
                    if sk.rsplit("+", 1)[0] != loc.rsplit("+", 1)[0]:
                        return ("%s is read again after the store to %s between the reads, which may alias it "
                                "(opt.md §5; loadcse R5)" % (loc, sk), "kept:kill")
        return None

    def locations_of(self, t):
        """loadcse location keys of the memory operands the tuple t reads, at the value-numbering stage (V)"""
        v = next((x for x in self.snaps if x.name == "GdfInsertArtificialAssigns"), None)
        if v is None:
            return set()
        st = v.stage()
        out = set()
        for a_ in self.ancestors(t):
            i = st.by_tup.get(a_)
            for x in (i.srcs if i else ()):
                if x.startswith("[") and "s0=&" not in i.notes:
                    try:
                        out.add(st.memkey(x, i.idx)[0])
                    except Exception:
                        pass
        return out

    def _idx_of_tup(self, t):
        for k in range(self.main_end):
            if self.tup_of(k) == t:
                return k
        return 0

    def ancestors(self, t):
        """t plus, at the value-numbering stage V, the OPLOAD tuples that define the temps t reads (a variable's
        initialisation `_a <- t130` whose load is `t130 <- [_p+0]` until GoptCleanUp folds it back)"""
        out = {t}
        v = next((x for x in self.snaps if x.name == "GdfInsertArtificialAssigns"), None)
        if v is not None and t not in v.by_tup and c2build.get("arch") == "x64":
            for u in self._split_from(t, v):          # 17.00: a load re-made after V (SizeOpt: `a <- [p]` becomes
                out |= self.ancestors(u)              # `t137 <- [p]; a <- t137`): the lineage of its consumer
            return out
        if v is None or t not in v.by_tup:
            return out
        st = v.stage()
        i = st.by_tup.get(t)
        for x in (i.srcs if i else ()):
            if x.startswith("["):
                continue
            d = st.last_def(loadcse.leaf_name(x), i.idx)
            if d is not None and d.op == "OPLOAD":
                out.add(d.tup)
        return out

    def _split_from(self, t, v):
        """Tuples present at V that read t's destination in the first snapshot after V that has t (t a load split off
        a V-stage tuple later: its reader is the original statement)"""
        s0 = next((x for x in self.snaps[v.idx + 1:] if t in x.by_tup), None)
        if s0 is None:
            return []
        i = s0.by_tup[t]
        if not i["dsts"] or i["dsts"][0].startswith("["):
            return []
        d = loadcse.leaf_name(i["dsts"][0])
        return [j["tup"] for j in s0.ins if j["tup"] != t and j["tup"] in v.by_tup and
                any(not x.startswith("[") and loadcse.leaf_name(x) == d for x in j["srcs"])]

    # -- missing
    def anchors(self, run, snap, n=2):
        """(previous, next) stream positions in snapshot `snap` of the n-th surviving tuple of OUR code before and
        after a run (n = 2: one instruction of slack for scheduling and layout moves)"""
        def walk(k, step):
            got = None
            seen = 0
            while 0 <= k < self.main_end:
                t = self.tup_of(k)
                if t and t in snap.by_tup:
                    got = snap.by_tup[t]["pos"]
                    seen += 1
                    if seen >= n:
                        break
                k += step
            return got
        return walk(run["prev"], -1), walk(run["next"], 1)

    def tline(self, k, step):
        """IL line offset of the nearest final instruction from k (walking by step) that has a tuple"""
        while 0 <= k < self.main_end:
            t = self.tup_of(k)
            if t and t in self.final.by_tup:
                return self.final.by_tup[t]["line"]
            k += step
        return None

    def removed_between(self, run):
        """[(snapshot idx k (last present), tuple dict, 'removed'|'changed', new text)] near the run: between the
        run's surviving neighbours in each snapshot's stream, and (when those are far apart) at their source lines"""
        out = []
        final = self.final_idx
        l0, l1 = self.tline(run["prev"], -1), self.tline(run["next"], 1)
        ls = [x for x in (l0, l1) if x is not None]
        lwin = (min(ls) - 1, max(ls) + 1) if ls else None
        width = len(run["idx"])
        for s in self.snaps[:final]:
            lo, hi = self.anchors(run, s)
            lo = -1 if lo is None else lo
            hi = len(s.ins) if hi is None else hi
            if hi < lo:                      # block layout moved the anchors: use a window
                lo, hi = min(lo, hi) - 2, max(lo, hi) + 2
            tight = hi - lo <= 3 * width + 8
            for i in s.ins:
                inwin = lo < i["pos"] < hi and (tight or not lwin or lwin[0] <= i["line"] <= lwin[1])
                inline = lwin and lwin[0] <= i["line"] <= lwin[1] and (l0 is None or l1 is None or l1 - l0 <= 3)
                if not (inwin or inline):
                    continue
                t = i["tup"]
                last = self.hist.last(t, s.idx)
                d = (0 if inwin else 1, abs(i["line"] - (l0 if l0 is not None else l1 or 0)))
                if last == s.idx and s.idx < final:
                    out.append((s.idx, i, "removed", None, d))
                elif s.idx + 1 < len(self.snaps) and t in self.snaps[s.idx + 1].by_tup:
                    n = self.snaps[s.idx + 1].by_tup[t]
                    if self._lost_mem(i, n):
                        out.append((s.idx, i, "changed", norm(n), d))
        return out

    @staticmethod
    def _lost_mem(a, b):
        ma = sum(1 for x in a["srcs"] if x.startswith("["))
        mb = sum(1 for x in b["srcs"] if x.startswith("["))
        return mb < ma and a["op"] == b["op"]

    def explain_missing(self, run):
        cands = self.removed_between(run)
        res = []
        used = set()
        last_copy = getattr(self, "_last_copy", None)       # a load in the previous run: (best, orig index)
        if last_copy and run["idx"][0] - last_copy[1] > 3:
            last_copy = None
        for j in run["idx"]:
            mn, ops = self.mn_ops_b(j)
            kind = kind_of(mn, ops)
            r = dict(ins=self.cs_b(j), va=self.va_of.get(j), kind=kind)
            best = None
            if kind == "store" and last_copy:            # load + store: one memory-to-memory copy of the IL
                best, last_copy = last_copy[0], None
                used.discard((best[1], best[2]["tup"], best[3]))
            if kind in ("push", "pop") and re.fullmatch(r"e(?:bx|si|di|bp)", ops.strip()) and \
                    (j < 8 or j > len(self.kb) - 10):
                cands_ = []                                  # a prologue save / epilogue restore: the frame
            else:
                cands_ = cands
            for k, i, how, new, d in cands_:
                if i["op"] in ASSERT_OPS or i["op"] in NOCODE_OPS:
                    continue
                ik = "load" if how == "changed" else il_kind(i)
                if (k, i["tup"], how) in used or ik not in COMPAT.get(kind, {kind}):
                    continue
                score = (ik == kind, how == "removed", -d[0], -d[1])
                if best is None or score > best[0]:
                    best = (score, k, i, how, new)
            if best is None and kind not in ("push", "pop") or (best and best[0][0] is False):
                g = self.removed_near(run, j, kind, used)
                if g and (best is None or g[0] > best[0]):
                    best = g
            if best:
                _, k, i, how, new = best
                used.add((k, i["tup"], how))
                ch = self.hist.changes(i["tup"], k)
                if ch:
                    r["history"] = ["`%s` -> `%s` by %s" % (o, t, self.interval(c - 1)[1]) for c, o, t in ch[-3:]]
                if il_kind(i) == "copy" and kind == "load":
                    last_copy = (best, j)
                cls, desc = self.interval(k)
                r.update(tuple=i["tup"], il=norm(i), line=self.src_line(i["line"]), how=how,
                         removed_after=self.snaps[k].label, by=desc)
                if how == "changed":
                    r["now"] = new
                self._why = None
                cls = self.refine_removed(cls, i, k, kind, how)
                if self._why:
                    r["why"] = self._why
                for c, o, t in self.hist.changes(i["tup"], k):
                    pn = self.snaps[c - 1].name if c else ""
                    if pn in ("OptFuncExpr", "GlobalOptimize", "GoptCanonicalize", "Inline") and \
                            re.search(r"<- t\d+:\w+$", o) and il_kind(i) == "copy":
                        r["why"] = ("its value was an expression that %s folded into a plain copy (`%s` -> `%s`), "
                                    "which %s then removed" % (pn, o.split("| ", 1)[-1], t.split("| ", 1)[-1],
                                                               cls.split(":", 1)[-1]))
                        cls = "folded:expr"
                r["verdict"] = cls
            elif kind in ("load", "store") and re.search(r"\[(?:esp|ebp)\b", ops):
                r["verdict"] = "regalloc:spill"
                r["why"] = ("the original %s a stack slot here and nothing of ours was removed: it spills or reloads "
                            "a value ours keeps in a register (or reads a local ours holds in one): `c2trace.py regs "
                            "--why`, `rasim need`" % ("loads" if kind == "load" else "stores"))
            elif kind in ("push", "pop") and re.fullmatch(r"e(?:bx|si|di|bp)", ops.strip()):
                r["verdict"] = "regalloc:callee-saved"
                r["why"] = "the original saves one more callee-saved register: it uses %s where ours does not" % ops
            else:
                r["verdict"] = "structure"
                r["why"] = ("no tuple of our IL at this place was removed or lost a memory operand in any pass: "
                            "the front end produced different tuples")
            res.append(r)
        self._last_copy = last_copy
        return res, cands

    def _off_of_tup(self, t):
        for off, x in self.tup_at.items():
            if x == t:
                return off
        return None

    def slot_var(self, run, j):
        """our name of the stack slot the original's instruction j addresses ([esp + 0x44] -> `_t`), read off our
        listing near the run (the same esp-relative operand in an instruction of ours), or None"""
        mn, ops = self.mn_ops_b(j)
        m = re.search(r"\[(?:esp|ebp)[^\]]*\]", ops)
        if not m or not self.ours.decoded:
            return None
        want = m.group(0).replace(" ", "")
        lo, hi = max(0, run["prev"] - 8), min(self.main_end, run["next"] + 9)
        for k in range(lo, hi):
            n = self._dec_a(k)
            d = self.ours.decoded[n]
            if want in (d[1] or "").replace(" ", ""):
                v = re.search(r"([\w?@]+)\$", self.ours[n][1])
                if v:
                    return v.group(1)
        return None

    def removed_near(self, run, j, kind, used, span=8):
        """second chance for a missing instruction nothing at its place explains: a removed tuple of a compatible
        kind within `span` source lines anywhere in the function (the original may have scheduled it far from its
        statement), preferring one that names the variable the instruction's stack slot holds"""
        l0, l1 = self.tline(run["prev"], -1), self.tline(run["next"], 1)
        ls = [x for x in (l0, l1) if x is not None]
        if not ls:
            return None
        var = self.slot_var(run, j)
        ok = COMPAT.get(kind, {kind}) | ({"copy"} if kind in ("store", "load") else set())
        best = None
        for s in self.snaps[:self.final_idx]:
            for i in s.ins:
                if i["op"] in ASSERT_OPS or i["op"] in NOCODE_OPS or self.hist.last(i["tup"], s.idx) != s.idx:
                    continue
                if (s.idx, i["tup"], "removed") in used:
                    continue
                ik = il_kind(i)
                dl = min(abs(i["line"] - x) for x in ls)
                if ik not in ok or dl > span:
                    continue
                names = var is not None and var in i["text"]
                sc = (ik == kind or names, names, 1, -1 - dl, 0)
                if best is None or sc > best[0]:
                    best = (sc, s.idx, i, "removed", None)
        return best

    def merge_twins(self):
        """{tuple hex: (twin tuple hex, how)} of the instructions the late flow optimiser matched in a merge that it
        carried out (c2merges events: FgMdInstrsMatch under a successful FgDoCommonTailMerge / FgTryTailMerge /
        FgDoCommonHeadMerge / FgTryHeadMerge / FgCrossJumpRemove)"""
        if getattr(self, "_twins", None) is not None:
            return self._twins
        out = {}
        names = {"dotail": "common tail (FgDoCommonTailMerge)", "ftail": "fall-through tail (FgTryTailMerge)",
                 "dohead": "common head (FgDoCommonHeadMerge)", "jcchead": "head of a jcc (FgTryHeadMerge)",
                 "cross": "cross jump (FgCrossJumpRemove)"}

        def walk(recs, ctx):
            for r in recs:
                c = ctx
                if r["kind"] in names:
                    c = names[r["kind"]] if r.get("ret") else None
                if r["kind"] == "match" and c and r.get("ret") and r["ret"] & 0xff and len(r["args"]) >= 2:
                    a_, b_ = r["args"][0], r["args"][1]
                    if isinstance(a_, int) and isinstance(b_, int):
                        out.setdefault("%x" % a_, ("%x" % b_, c))
                        out.setdefault("%x" % b_, ("%x" % a_, c))
                walk(r.get("sub", []), c)
        for g in getattr(self, "merges", None) or []:
            walk(g.get("ev", []), None)
        self._twins = out
        return out

    def refine_removed(self, cls, i, k, kind, how):
        regs = re.findall(r"\b(e[a-ds][xip])=", i["text"])
        if il_kind(i) == "copy" and how == "removed" and len(i["dsts"]) == 1 and len(i["srcs"]) == 1 and \
                len(regs) == 2 and regs[0] == regs[1]:
            self._why = ("a copy between two values that register allocation gave the same register (%s): removed as "
                         "a no-op; the original gives them two registers (`c2trace.py regs --why`, regmap)" % regs[0])
            return "regalloc:coalesced"
        if kind == "x87" or il_kind(i) == "x87":
            self._why = "x87 stack shape (fld / fxch / fstp order): tools/c2x87.py, docs/c2/x87.md"
            tail = cls.split(":")[-1]
            return "x87:" + ("Schedule" if tail == "riscify-undo" else tail)
        if cls.startswith("merged:FgDoLateFlowOpts") and how == "removed" and getattr(self, "merges", None):
            tw = self.merge_twins().get(i["tup"])
            if tw:
                self._why = "merged into its twin tuple %s by the %s%s" % (
                    tw[0], tw[1], " (@%x in our code)" % self._off_of_tup(tw[0]) if self._off_of_tup(tw[0]) is not None
                    else "")
                return "merged:FgDoLateFlowOpts"
            self._why = ("removed by FgDoLateFlowOpts without a merge match (dead code after a branch, a branch "
                         "chain or a jump to the next instruction)")
            return "removed-by:FgDoLateFlowOpts"
        ik = il_kind(i)
        if ik == "jcc" and len(i["srcs"]) >= 2:          # a branch takes its compare's class
            f = loadcse.leaf_name(i["srcs"][1])
            c = next((x for x in reversed(self.snaps[k].ins[:i["pos"]]) if x["dsts"] and
                      loadcse.leaf_name(x["dsts"][0]) == f), None)
            if c is not None and il_kind(c) == "cmp":
                i = c
                ik = "cmp"
        if ik in ("cmp", "jcc") and (cls.startswith("removed-by:GlobalOptimize/sweep") or cls.startswith("folded")):
            s = self.snaps[k]
            if ik == "cmp" and i["srcs"]:
                v = loadcse.leaf_name(i["srcs"][0])
                # GdfInsertAssertions' facts before the compare: `t = v != 0` after a dereference of v, `t = v == K`
                # on a branch arm (the latest snapshot that still has them: they go at GoptRemoveAssumes)
                for s2 in self.snaps[k::-1]:
                    asr = [x for x in s2.ins if x["op"] in ASSERT_OPS and x["srcs"] and
                           loadcse.leaf_name(x["srcs"][0]) == v]
                    if asr:
                        break
                pos = s2.by_tup[i["tup"]]["pos"] if asr and i["tup"] in s2.by_tup else None
                before = [x for x in asr if pos is None or x["pos"] < pos]
                if before:
                    x = before[-1]
                    deref = [y for y in s2.ins if y["pos"] < x["pos"] and y["line"] == x["line"] and
                             any(o.startswith("[") and re.search(r"\[%s:" % re.escape(v), o) for o in y["dsts"] + y["srcs"])]
                    c = loadcse.leaf_name(x["srcs"][1]) if len(x["srcs"]) > 1 else "?"
                    if deref:
                        self._why = ("decided by the assertion `%s != 0` that GdfInsertAssertions put after the "
                                     "dereference of %s at line %s (c2 takes a dereferenced pointer to be non-null)" % (
                                         v, v, unitcompose.label(self.src_line(deref[-1]["line"]))))
                        return "folded:nonnull"
                    self._why = "decided by the assertion `%s %s %s` (GdfInsertAssertions, line %s)" % (
                        v, {"OPNE": "!=", "OPEQ": "=="}.get(x["op"], x["op"]), c, unitcompose.label(self.src_line(x["line"])))
                prior = [x for x in s.ins[:i["pos"]] if x["op"] in ("OPCMP", "cmp") and x["srcs"] and
                         loadcse.leaf_name(x["srcs"][0]) == v]
                if prior:
                    return "folded:range-check"
            return "folded:compare"
        if how == "changed" and cls.startswith("removed-by:GlobalOptimize"):
            return "removed-by:GlobalOptimize/CSE" if "CSE" in cls or "sweep" in cls else cls
        if how == "changed":
            return cls
        return cls


# ---------------------------------------------------------------- levers
LEVERS = {
    "removed-by:GlobalOptimize/CSE": "the original reloads: read the location through a reference/pointer variable "
                                     "used twice or more (loadcse R3: `T& r = p->m;`), or a store/call between "
                                     "(loadcse.md §4: 'two loads instead of one'); realize `alias2:CHAIN`",
    "kept:alias": "the original loads once: drop the alias variable (rwsweep --families unalias) or copy a loaded "
                  "pointer into a local (`Q* q = p->q;`, loadcse R2/R3; realize `ptrlocal:CHAIN`)",
    "kept:kill": "the original loads once: nothing may write the location in between in its source; look for a "
                 "store through another pointer or a call the original does not have between the two reads (R5)",
    "kept:path": "the original loads once: its first read is on every path (no PRE: read it before the branch, R6)",
    "kept:type": "the two reads have different IL types (a null test against a dereference, opt.md §4 rule 8)",
    "folded:range-check": "the original keeps the test: its value is not provably the one tested before (a "
                          "different variable or a reload from memory, e.g. read the field again rather than the "
                          "local copy), or the earlier test is not on every path",
    "folded:nonnull": "the original keeps the null test (c2 asserts `p != 0` after a memory operand [p+K] only): if "
                      "the dereference is a copy into p, write it as memset or as an inline helper that passes the "
                      "size to memcpy as a parameter (`MemCopy(p, src, sizeof(T))`: still an intrinsic call when "
                      "the assertions are made; `*p = *src` and memcpy with a constant size at the site are moves "
                      "already); else the test comes before the dereference, or the dereference goes through "
                      "another variable (opt.md 7.7, f3decomp R1050; F3 0x00422420)",
    "folded:compare": "the original keeps the compare: the operand is not a known constant/range there",
    "folded:expr": "ours folds the expression as written (OptFuncExpr runs before inlining reaches it); the original "
                   "keeps the copy because its expression is folded only after inlining: spell it through the inline "
                   "function / operator (M&B 0x4b1930: `vector4(..) * t` instead of `lo.y = 1.0f * t`)",
    "removed-by:GlobalOptimize/sweep1": "the original keeps it: the value it computes is not propagated "
                                        "(a variable with two definitions, an address-taken local, opt.md §3)",
    "removed-by:GlobalOptimize/DeadStores": "the original keeps the store: it is read (a call, a load through "
                                            "another pointer) before it is overwritten, or its object escapes",
    "removed-by:GlobalOptimize/SingleUseCopyProp": "the original keeps the variable: give it a second use or a "
                                                   "call-argument use (opt.md summary 2)",
    "merged:FgDoLateFlowOpts": "`c2trace.py merges --off OFF` names the pair and the first difference; "
                               "make the two tails differ (or equal) at that instruction",
    "regalloc:coalesced": "a register-allocation difference, not a count one at heart: `c2trace.py regs --why` / "
                          "regmap name the variable whose register differs",
    "regalloc:enterload": "ours keeps the value in a register (a Globregs candidate) where the original reads memory: "
                          "fewer uses or a shorter live range make it a non-candidate (`c2trace.py regs --why`)",
    "regalloc:spill": "`c2trace.py regs --why`, `rasim need` (which value the original spills)",
    "regalloc:callee-saved": "the original uses one more callee-saved register: a value lives across a call there "
                             "(`c2trace.py regs`)",
    "structure": "draft structure: the front end produced different IL (a statement, a temporary or a branch the "
                 "original's source has or lacks)",
}


# ---------------------------------------------------------------- report
def moved_pairs(an, runs, window=48):
    """{('extra', ours idx) | ('missing', orig idx)} of instructions that are only moved: an extra and a missing one
    with the same shape (registers and stack slots masked), nearest first, within `window` instructions"""
    ex = [k for r in runs if r["kind"] == "extra" for k in r["idx"]]
    mi = [j for r in runs if r["kind"] == "missing" for j in r["idx"]]
    key = _mask

    def opos(i):                                        # an ours index as an orig position (through the pairs)
        best = None
        for (a_, b_) in an.pairs:
            if a_ <= i:
                best = b_
            else:
                break
        return (best if best is not None else 0) + 0.5
    cands = []
    stack = lambda k: any(isinstance(o, str) and re.search(r"\[(?:esp|ebp)[+\]-]", o) for o in k[1:])
    for i in ex:
        for j in mi:
            if abs(opos(i) - j) > window:
                continue
            if key(an.ka[i]) == key(an.kb[j]):
                cands.append((abs(opos(i) - j), i, j))
            elif stack(an.ka[i]) and stack(an.kb[j]) and _nodisp(_mask(an.ka[i])) == _nodisp(_mask(an.kb[j])):
                cands.append((1000 + abs(opos(i) - j), i, j))     # another stack slot: after exact shapes
            elif an.ka[i][0] == an.kb[j][0] and len(an.ka[i]) == len(an.kb[j]) and abs(opos(i) - j) <= 12 and \
                    an.ka[i][0] in ("cmp", "test", "push", "add", "sub", "and", "or", "xor", "inc", "dec") and \
                    [o for o in an.ka[i][1:] if not tsweep.REG.fullmatch(o) and "[" not in o] == \
                    [o for o in an.kb[j][1:] if not tsweep.REG.fullmatch(o) and "[" not in o]:
                cands.append((2000 + abs(opos(i) - j), i, j))     # the same operation, register vs memory operand
    out, ui, uj = {}, set(), set()
    for d, i, j in sorted(cands):
        if i in ui or j in uj:
            continue
        ui.add(i)
        uj.add(j)
        out[("extra", i)] = j
        out[("missing", j)] = i
    return out


def report(an, show_il=False, show_all=False):
    na, nb = an.counts()
    runs = an.runs()
    mv = moved_pairs(an, runs)
    keep, moved = [], []
    for r in runs:
        rest = [k for k in r["idx"] if (r["kind"], k) not in mv]
        gone = [k for k in r["idx"] if (r["kind"], k) in mv]
        if gone:
            moved.append(dict(kind=r["kind"], idx=gone, to=[mv[(r["kind"], k)] for k in gone]))
        if rest:
            keep.append(dict(r, idx=rest))
    runs = keep
    rep = dict(function=an.final.fn, build=build_key(), ours=na, orig=nb, runs=[])
    for r in runs:
        ent = dict(kind=r["kind"])
        if r["kind"] == "extra":
            ent["items"] = [an.explain_extra(k) for k in r["idx"]]
            ent["off"] = an.off_of.get(r["idx"][0])
        else:
            items, cands = an.explain_missing(r)
            ent["items"] = items
            ent["va"] = an.va_of.get(r["idx"][0])
            ln = an.line_of(r["prev"]) if r["prev"] >= 0 else None
            ln2 = an.line_of(r["next"]) if r["next"] < an.main_end else None
            ent["between"] = [an.cs_a(r["prev"]) if r["prev"] >= 0 else "start",
                              an.cs_a(r["next"]) if r["next"] < an.main_end else "end"]
            ent["line"] = (ln or ln2 or (None, ""))[0]
            ent["src"] = (ln or ln2 or (None, ""))[1]
            if show_il:
                ent["cands"] = ["%s %s %s %s" % (an.snaps[k].label, how, i["tup"], norm(i)) for k, i, how, _, _d in cands]
        vs = collections.Counter(x["verdict"] for x in ent["items"])
        inf = [(n, v) for v, n in vs.items() if v not in ("structure", "unknown")]
        ent["verdict"] = max(inf)[1] if inf else vs.most_common(1)[0][0]
        ent["classes"] = dict(vs)
        if ent["kind"] == "extra":
            ent["line"] = next((x.get("line") for x in ent["items"] if x.get("line")), None)
            ent["src"] = next((x.get("src") for x in ent["items"] if x.get("src")), "")
        lv = lever(ent["verdict"])
        if lv:
            ent["lever"] = lv
        rep["runs"].append(ent)
    rep["classes"] = dict(collections.Counter(r["verdict"] for r in rep["runs"]))
    rep["moved"] = [dict(kind=m["kind"], ins=[an.cs_a(k) if m["kind"] == "extra" else an.cs_b(k) for k in m["idx"]],
                         where=[an.off_of.get(k) if m["kind"] == "extra" else an.va_of.get(k) for k in m["idx"]])
                    for m in moved if m["kind"] == "extra"]
    rep["unexplained"] = sum(len(r["items"]) for r in rep["runs"] if r["verdict"] in ("structure", "unknown"))
    return rep


def lever(v):
    if v in LEVERS:
        return LEVERS[v]
    for k, t in LEVERS.items():
        if v.startswith(k):
            return t
    return None


def show(rep):
    print("## %s (%s): ours %d, orig %d instructions (%+d); %d extra in %d runs, %d missing in %d runs" % (
        rep["function"], rep["build"], rep["ours"], rep["orig"], rep["ours"] - rep["orig"],
        sum(len(r["items"]) for r in rep["runs"] if r["kind"] == "extra"),
        sum(1 for r in rep["runs"] if r["kind"] == "extra"),
        sum(len(r["items"]) for r in rep["runs"] if r["kind"] == "missing"),
        sum(1 for r in rep["runs"] if r["kind"] == "missing")))
    for n, r in enumerate(rep["runs"], 1):
        where = ("ours @%x" % r["off"]) if r["kind"] == "extra" and r.get("off") is not None else \
            ("orig %08x" % r["va"]) if r.get("va") is not None else ""
        print("\n%s %d  %s  line %s%s  -> %s" % ("EXTRA" if r["kind"] == "extra" else "MISSING", n, where,
                                                 unitcompose.label(r.get("line")) if r.get("line") is not None else None,
                                                 "  `%s`" % r["src"][:70] if r.get("src") else "",
                                                 r["verdict"]))
        if r["kind"] == "missing":
            print("   between ours `%s` and `%s`" % tuple(r["between"]))
        for it in r["items"]:
            print("   %-7s %-36s %s" % ("+" if r["kind"] == "extra" else "-", it["ins"][:36], it["verdict"]))
            if r["kind"] == "extra":
                if it.get("tuple"):
                    print("           tuple %s, born at %s as `%s`" % (it["tuple"], it.get("born"), it.get("born_as", "")[:90]))
                    print("           %s" % it.get("origin", ""))
                    for h in it.get("history", [])[-3:]:
                        print("             then %s" % h[:110])
            else:
                if it.get("tuple"):
                    print("           our tuple %s `%s` (line %s) %s after %s" % (
                        it["tuple"], it["il"][:80], unitcompose.label(it["line"]), "removed" if it["how"] == "removed" else
                        "lost its memory operand", it["removed_after"]))
                    print("           by %s" % it["by"])
                    if it.get("now"):
                        print("           now `%s`" % it["now"][:100])
                    for h in it.get("history", []):
                        print("           history: %s" % h[:200])
            if it.get("why"):
                print("           why: %s" % it["why"])
            if it.get("fe") and it.get("verdict", "").startswith("structure"):
                import feshape
                found = feshape.lookup(it["fe"])
                print("           front end: `%s`; removed elsewhere by %s (effectmine mine --want '%s')" % (
                    it["fe"].rsplit(":", 1)[0], ", ".join("%s %d/%d" % x for x in found) if found else
                    "no measured mutation yet", it["fe"]))
        if r.get("lever"):
            print("   lever: %s" % r["lever"])
        for c in r.get("cands", []):
            print("   cand %s" % c[:140])
    if rep.get("moved"):
        print("\nmoved (an extra and a missing instruction of the same shape: order, not count): " + "; ".join(
            "%s @%s" % (t, "%x" % w if w is not None else "?") for m in rep["moved"] for t, w in zip(m["ins"], m["where"])))
    for h in rep.get("hints", []):
        print("\nflags: " + h)
    lv = rep.get("lift")
    if lv and lv.get("error"):
        print("\nlift: not run (%s)" % lv["error"])
    elif lv:
        if lv["self"] < 0.8:
            print("\nlift: the rules give back only %d%% of our own code here (below 80%%): the original's IL is not "
                  "shown; `lift.py --show` lists what they miss" % (100 * lv["self"]))
        else:
            print("\nlift (the original's code lifted to our IL at Lower's entry; docs/c2/lift.md): %s" % (
                "%d difference(s)" % lv["n"] if lv["n"] else "no difference the rules can see"))
            for l in lv["text"].splitlines()[2:]:
                print("  " + l)
    print("\nverdict classes (runs): " + ", ".join("%s %d" % kv for kv in sorted(rep["classes"].items(),
                                                                                  key=lambda kv: -kv[1])))


FP_MEM = re.compile(r"^(fstp|fld) ((?:dword|qword) ptr \[.*\])$")


def lift_view(snaps, asm, a):
    """lift.py on this compile's own data (no compile more): our IL at Lower's entry, our listing, the original's code
    lifted to the same tokens; the differences at our tuples' source lines (docs/c2/lift.md)."""
    import io, contextlib, lift
    low = next((s for s in snaps if s.name == "Lower"), None)
    if low is None or not a.orig or a.orig.startswith("listing:"):
        return None
    func = a.asm_func or a.func
    code = tsweep.listing_cs_offsets(asm, func)
    srcl = lift.listing_lines(asm, func)
    offs = sorted(code)
    try:
        src_lines = open(a.src, errors="replace").read().splitlines()
    except OSError:
        src_lines = None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = lift.report(low.lines, [code[o] for o in offs], [srcl.get(o) for o in offs], lift.read_orig(a.orig),
                        False, src_lines)
    return dict(self=r["self"], orig=r.get("orig"), text=buf.getvalue(), n=len(r.get("diffs", [])))


def fp_hints(an, cl):
    """Flag-level causes the IL cannot show. /fp:precise rounds every float assignment through its home: `fstp m`
    and `fld m` of the same slot, where /fp:fast keeps the value on the stack and stores a copy (`fst m`)."""
    if any(c.lower().startswith(("/fp:fast", "-fp:fast")) for c in cl) or build_key().startswith("13.10"):
        return []                                   # 13.10 has no /fp switch
    def trips(ins):
        out = []
        for i, s in enumerate(ins):
            m = FP_MEM.match(s)
            if m and m.group(1) == "fstp":
                for t in ins[i + 1:i + 4]:
                    n = FP_MEM.match(t)
                    if n and n.group(1) == "fld" and n.group(2) == m.group(2):
                        out.append((s, t))
                        break
        return out
    ours = trips([an.cs_a(k) for k in range(an.main_end)])
    if len(ours) <= len(trips([an.cs_b(k) for k in range(len(an.kb))])):
        return []
    return ["ours `%s` ... `%s` rounds the value through its home; the original has %s such round trip%s and keeps "
            "the value on the x87 stack (a copy stored with `fst`, or none). /fp:precise rounds every float assignment "
            "that way and /fp:fast does not: try /fp:fast (Spore 00d38780; docs/c2/x87.md section 2); "
            "tools/x87round.py pairs every rounding point with the original's by dataflow and names why cl made ours"
            % (ours[0] + ("fewer" if len(ours) > 1 else "no", "s" if len(ours) > 1 else ""))]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="icount.py", usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    ap.add_argument("--asm-vs", dest="orig")
    ap.add_argument("--vs-func", help="compare with this function of the same compile instead of --asm-vs (probes, "
                                      "two drafts in one TU)")
    ap.add_argument("--asm-func")
    ap.add_argument("--fine", action="store_true", help="also a snapshot per GoptOptimizeBlock / GoptOptimizeLoop")
    ap.add_argument("--json")
    ap.add_argument("--keep")
    ap.add_argument("--show-il", action="store_true", help="list the removed / changed tuples near each missing run")
    ap.add_argument("--show-passes", action="store_true", help="print the snapshots taken")
    ap.add_argument("--allow-local", action="store_true", help="compile under wine when the server declines")
    argv, cl = c2trace.split_cl(sys.argv[1:] if argv is None else argv)
    a = ap.parse_args(argv)
    if not a.orig and not a.vs_func:
        ap.error("--asm-vs ORIG.s or --vs-func G is required")
    if a.vs_func:
        a.orig = "listing:" + a.vs_func
    if a.allow_local:
        ALLOW_LOCAL.append(1)
    passes, snaps, asm, rc, out, merges = run(a.src, a.func, cl, a.fine, a.keep)
    if rc:
        sys.stdout.write(out)
    if not snaps:
        sys.exit("icount: function %r not seen at any pass" % a.func)
    if a.show_passes:
        print("# snapshots: " + " ".join("%s(%d)" % (s.label, len(s.ins)) for s in snaps))
    an = Analysis(a.func, snaps, asm, a.orig, a.asm_func)
    an.merges = merges if isinstance(merges, list) else []
    rep = report(an, a.show_il)
    rep["hints"] = fp_hints(an, cl)
    try:
        rep["lift"] = lift_view(snaps, asm, a)
    except Exception as e:                            # a second opinion: never the reason a report fails
        rep["lift"] = dict(error=str(e)[:200])
    show(rep)
    if a.json:
        json.dump(rep, open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(c2trace.pipe_safe(main))
