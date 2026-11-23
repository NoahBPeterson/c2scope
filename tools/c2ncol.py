#!/usr/bin/env python3
"""The x64 register allocator NColor (cl 17.00 x86_amd64), traced: which register every candidate got, and in which
order.  Doc: docs/c2/ncolor.md

usage: c2ncol.py SRC --func F [--why] [--raw] [--keep DIR] [-- <cl args>]     (needs --c2 17.00)

NColor is not the x86 back end's graph colourer (Color / Spillcosts / ChoosePhysReg, docs/c2/regalloc.md): it takes
the candidates (lifetimes of temporaries and variables) one at a time in priority order, and gives each the first
register in its order that is available over the whole lifetime; when none is, the best-covered part becomes a
region of its own and the rest is processed again. The report lists, in processing order:

  #  candidate (variable or tN)  priority  tuples first..last  ->  register per region  [first choice]  notes
and, after allocation, the callee-saved registers NColPostspill gave back. --why adds per region the registers the
order walk looked at and what each was (full cover / fallback classes / rejected): the probes of the build table's
hooks.ncol_why.

Hook points: tools/c2builds/<build>.json hooks.ncol (entries: candidate, first choice, region store, spill-back,
rewrite); read off the code in work/port1700/ncolor_map.md.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2build   # noqa: E402  (--c2 is taken out of argv here)
import c2trace   # noqa: E402

# event kinds in hooks.ncol order; --why adds STATE (hooks.ncol_state: the liveness arrays after prioritization)
CAND, FIRST, STORE, GIVEBACK, REWRITE, STATE, FN, PREF, PRE, REGSET, PERFUNC, COVREG, COVBEST, SEPARATE = range(14)
MINKEPT, MINREST = 14, 15
DPA_TRY, DPA_COVER, DPA_C, DPA_B, DPA_A = range(16, 21)
# --il: the IL also at NColSubsumption's entry (0x1009ab40, after NColInitializeSyms / NColBuildInfo, before the copies
# are coalesced and NColFoldTooCheap folds: the stream the lifetime indices, live counts and availability come from)
PRE_HOOK = "1009ab40=tdccc>0>0>4$,tdccc>0>8>0>0x1c%"
# --il: every kind-0xa (REGSET) leaf NColBuildInfo's backward walk meets (0x100d7b53, `mov ecx, [esi+0x24]` before
# SbvNextEx: esi = the leaf, [esp+0xc] = the tuple number, [esp+0x1c] = the tuple): its register-set bit vector, which
# the IL dump prints as `regset:0`. A call's clobber set: the Win64 volatiles, or for a callee of the same unit compiled
# before, the registers it uses (interprocedural register usage). Sparse BV nodes {+0 base, +4 low, +8 high, +0xc next}.
REGSET_HOOK = "100d7b53=s7,s3,esi>0x24>0*16:0:4:12,esi>0x24>0*16:8:8:12"
# --il: NColMdPerFuncInit's entry (0x100925b0, ecx = FUNC; after NColCanon): the words its rbp reservation reads
# (0x10092733..0x1009279b): FUNC+0xe4 (0x80 floating point, 0x4801000 EH), FUNC+0xe8, the frame-reference score
# NColCanon summed (TLS+0x1196c, rbp is reserved at 6 * 24 when favoring speed) and TLS+0x1073c (docs/c2/ncolor.md 12.5)
PERFUNC_HOOK = "100925b0=ecx>0xe4,ecx>0xe8,t1196c>0,t1073c>0"
# --cov (with --export --il): NColBestCoverage's intermediate state (docs/c2/ncolor.md 4.4). COVREG_HOOK at 0x1006a348
# (`cmp ecx, [ebp-0x98]`, 6 bytes, the flags it sets feed the jg after it: the trampoline runs it last): one register
# done, ecx = its best cluster's score (benefit - cost; 0x80000000 none), [ebp-0x20] = the physreg SYM, [ebp-0x98] = the
# best score so far, and every cluster of the register ([ebp-0x4c], next at +0): begin / end tuple (+0x10 / +0x14),
# benefit / cost (+0x18 / +0x1c), first / last block (+8 / +0xc). COVBEST_HOOK at 0x1006aeb3 (9 bytes): a new best,
# edx = its cluster. SEPARATE_HOOK at NColSeparateProcessedRegion's entry (0x100c7a20): the covered ranges BestCoverage
# (or NColMakePhysregAvail) left in TLS+0xf668 (0x18-byte entries), [esp+8] their number; edx = R (+0x24 / +0x28)
NCLU = 24
COVREG_HOOK = ("1006a348=ecx,ebp>-0x20>0x20,ebp>-0x98,ebp>-0x4c*%d:16:20,ebp>-0x4c*%d:24:28,ebp>-0x4c*%d:8:12"
               % (NCLU, NCLU, NCLU))
COVBEST_HOOK = "1006aeb3=ecx,ebp>-0x20>0x20,edx>0x10,edx>0x14,edx>0x18,edx>0x1c"
NSEP = 400           # f668 entries read at NColSeparateProcessedRegion (a split global has one per block it keeps)
SEPARATE_HOOK = "100c7a20=s2,edx>0x24,edx>0x28,tf668>0#%d" % (6 * NSEP)
# --cov: what NColSeparateProcessedRegion makes of those pieces (docs/c2/ncolor.md 4.5): MINKEPT_HOOK after the first
# NColRangesToMinTupleRegions (0x100c7a55, eax = the NCOLTUPRGN list the region keeps; `mov edx, [ebp-4]; mov [ebp+0xc],
# eax`, 6 bytes, nothing branches inside), MINREST_HOOK where the second one's result goes into the new region
# (0x100c7acb, esi = the rest's list, eax = the new region; `mov [eax+0x10], esi; mov ecx, [edi+0x14]`, 6 bytes)
MINKEPT_HOOK = "100c7a55=eax*%d" % NSEP
MINREST_HOOK = "100c7acb=esi*%d" % NSEP
# --dpa (with --export --il, after --cov's hooks): NColDeterminePhysregAvail's order loop (docs/c2/ncolor.md 7): a
# register number that passed the useable test (0x10069635, esi), a physreg that covers every range (0x10069750, its
# number; the displaced cmp feeds a later jae), the fallbacks C (0x100699d0, ecx = its gap), B (0x10069a35), A
# (0x100699fb); the result is the FIRST event (0x10068751; the common exit 0x1006988e displaces the ret)
DPA_HOOKS = ["10069635=esi", "10069750=esi>0x20", "100699d0=esi>0x20,ecx", "10069a35=esi>0x20", "100699fb=esi>0x20"]
FN_FLAGS = ",ecx>0xe4"   # NColor's entry (ecx = FUNC): FUNC+0xe4, whose bit 0x80 stops NColMdAdjustPriority
NAVAIL = 80          # nodes of each register's initial availability list (--export)
# register numbers of _RegName whose lists are exported: rax..r15; fxmm0l..15l (doubles), fxmm0s..15s (floats);
# preference windows are kept per master: rax..r15 and ymm0..15 (the xmm family's master, docs/c2/ncolor.md 9)
AVAIL_REGS = list(range(1, 17)) + list(range(320, 352))
PREFWIN_REGS = list(range(1, 17)) + list(range(112, 128))
NAPP, NRNG, NBLK, NTUP = 400, 64, 640, 4000   # appearances and ranges read per candidate; blocks / tuples of the state


def why_specs():
    """Extra entry specs of the candidate hook: the appearance list (tup#s) and the range list (begin, end)."""
    return ["ecx>0x1c*%d:4" % NAPP, "ecx>0x10*%d" % NRNG,
            "ecx>0x3c>8", "ecx>0x3c>0xc", "ecx>0x38>0x20",   # its own preference window; the tied physreg
            "ecx>4>0x44",                                     # the anti-preference set (TLS+0x119c8 / cc / d0)
            "ecx", "tf640>0",                                 # its record and the array: lifetime index (ties)
            "ecx>4"]                                          # its SYM (--il: the root of the IL leaves' symbols)


def hooks():
    h = c2build.hooks("ncol", None)
    if h is None:
        sys.exit("c2ncol: the selected c2 build has no NColor hooks (cl 17.00 x64 only: --c2 17.00)")
    return h


def symtext(name, num, kind):
    """A candidate as the IL dump prints it: the variable's name, else tN (temporaries) / sN."""
    name = (name or "").strip('"')
    if name and name != "?":
        return name
    try:
        k = int(kind, 16) & 0xff
        return ("t%d" if k == 3 else "s%d") % int(num, 16)
    except ValueError:
        return "?"


def unq(v):
    return v.strip('"') if v and v not in ("?",) else "-"


def parse(text):
    """Events -> candidates in processing order: dict(name, prio, first, last, regions=[...], choices=[...])."""
    fns = parse_all(text)
    if not fns:
        return [], [], None
    f = fns[-1] if len(fns) > 1 and not fns[0]["cands"] else fns[0]
    return f["cands"], f["given"], f["state"]


def parse_all(text, fn_flags=False):
    """Events -> one record per NColor run: {func, cands, given, state} (state and per-candidate apps / ranges /
    avail only with --why / --export). c2 17.00 compiles functions on worker threads: the log's "# thread N" lines
    say whose events follow, and each thread has its own current function and candidate."""
    fns = []
    ctx = {}            # thread -> {"fn": record, "cur": candidate}
    th = 0
    ilrec = {}          # thread -> the record whose STATE event the following T lines (--il) belong to

    def fn_of(t):
        c = ctx.setdefault(t, {"fn": None, "cur": None})
        if c["fn"] is None:
            c["fn"] = dict(func=None, cands=[], given=[], state=None)
            fns.append(c["fn"])
        return c
    hx = lambda x: int(x, 16)
    # '?' = a word the probe could not read (the state probe reads the block arrays past their end): 0
    arr = lambda t: [hx(x) if x != "?" else 0 for x in t.strip("[]").split()]
    for ln in text.splitlines():
        if ln.startswith("# thread "):
            th = int(ln.split()[2])
            continue
        if ln.startswith("T ") and th in ilrec:
            r_, key = ilrec[th] if isinstance(ilrec[th], tuple) else (ilrec[th], "il")
            r_.setdefault(key, []).append(ln[2:])
            continue
        if not ln.startswith("E ") or "|" not in ln:
            continue
        ilrec.pop(th, None)
        head, _, rest = ln.partition("|")
        f = head.split()
        kind, v = int(f[3]), c2trace._split(rest)
        if kind == FN:
            c = ctx.setdefault(th, {"fn": None, "cur": None})
            c["fn"] = dict(func=v[0].strip('"') if v else "?", cands=[], given=[], state=None)
            if fn_flags and len(v) > 1 and v[-1] not in ("?", ""):
                c["fn"]["fflags"] = hx(v[-1])          # FUNC+0xe4 at NColor's entry (FN_FLAGS, the last spec)
            c["cur"] = None
            fns.append(c["fn"])
            continue
        c = fn_of(th)
        rec, cur = c["fn"], c["cur"]
        if kind == PRE:
            ilrec[th] = (rec, "il_pre")    # --il: the IL before NColSubsumption (T lines follow)
            continue
        if kind == PERFUNC:                # --il: NColMdPerFuncInit's inputs (PERFUNC_HOOK)
            g = lambda x: hx(x) if x not in ("?", "") else None
            if len(v) >= 4:
                rec["perfunc"] = dict(e4=g(v[0]), e8=g(v[1]), rbpscore=g(v[2]), m1073c=g(v[3]))
            continue
        if kind in (COVREG, COVBEST, SEPARATE) and cur is not None:     # --cov
            g = lambda x: hx(x) if x not in ("?", "") else None
            pairs = lambda t: [(x[2 * k], x[2 * k + 1]) for x in [arr(t)] for k in range(len(x) // 2)] \
                if t.startswith("[") else []
            if kind == COVREG and len(v) >= 6:
                cl = [dict(begin=b, end=e, benefit=bn, cost=co, blocks=bl) for (b, e), (bn, co), bl in
                      zip(pairs(v[3]), pairs(v[4]), pairs(v[5]))]
                cur.setdefault("cov", []).append(dict(reg=g(v[1]), score=g(v[0]), best=g(v[2]), clusters=cl))
            elif kind == COVBEST and len(v) >= 6:
                cur.setdefault("covbest", []).append(dict(score=g(v[0]), reg=g(v[1]), begin=g(v[2]), end=g(v[3]),
                                                          benefit=g(v[4]), cost=g(v[5])))
            elif kind == SEPARATE and len(v) >= 4:
                n, w = g(v[0]) or 0, arr(v[3]) if v[3].startswith("[") else []
                cur.setdefault("separate", []).append(dict(n=n, first=g(v[1]), last=g(v[2]),
                                                           ranges=[w[6 * k:6 * k + 6] for k in range(min(n, len(w) // 6))]))
            continue
        if kind in (MINKEPT, MINREST) and cur is not None and cur.get("separate"):   # --cov: the kept / rest lists
            w = arr(v[0]) if v and v[0].startswith("[") else []
            cur["separate"][-1]["kept" if kind == MINKEPT else "rest"] = [w[2 * k:2 * k + 2] for k in range(len(w) // 2)]
            continue
        if DPA_TRY <= kind <= DPA_A and cur is not None:                # --dpa
            g = lambda x: hx(x) if x not in ("?", "") else None
            tag = ("try", "cover", "C", "B", "A")[kind - DPA_TRY]
            cur.setdefault("dpa", []).append([tag] + [g(x) for x in v])
            continue
        if kind == REGSET:                 # --il: a REGSET leaf's registers (NColBuildInfo, before il_pre)
            if len(v) >= 4 and v[2].startswith("[") and v[3].startswith("["):
                lo, hi = arr(v[2]), arr(v[3])
                regs = []
                for k in range(min(len(lo), len(hi)) // 2):
                    base, m = lo[2 * k], lo[2 * k + 1] | hi[2 * k] << 32
                    regs += [base + i for i in range(64) if m >> i & 1]
                rec.setdefault("regsets", []).append(dict(tup=v[0], n=hx(v[1]), regs=regs))
            continue
        if kind == CAND:
            cur = dict(name=symtext(v[0], v[1], v[2]), prio=int(v[3], 16) if v[3] != "?" else None,
                       first=hx(v[4]), last=hx(v[5]), regions=[], choices=[], final=None)
            if len(v) >= 8 and v[6].startswith("["):
                cur["apps"] = arr(v[6])
                rg = arr(v[7])
                cur["ranges"] = [(rg[2 * k], rg[2 * k + 1]) for k in range(len(rg) // 2)]
                if len(v) >= 11:
                    g = lambda x: int(x, 16) if x not in ("?", "") else None
                    cur["prefwin"] = (g(v[8]), g(v[9])) if g(v[8]) is not None else None
                    cur["tied"] = g(v[10])
                    cur["anti"] = g(v[11]) if len(v) > 11 else None
                    if len(v) > 13 and g(v[12]) is not None and g(v[13]):
                        cur["index"] = (g(v[12]) - g(v[13])) // 0x5c
                    if len(v) > 14 and g(v[14]) is not None:
                        cur["sym"] = g(v[14])
            rec["cands"].append(cur)
            c["cur"] = cur
        elif kind == PREF and cur is not None:
            x = arr(v[0]) if v and v[0].startswith("[") else []
            cur.setdefault("prefs", []).append([(x[2 * k], x[2 * k + 1]) for k in range(len(x) // 2)])
            if len(v) > 1 and v[1] != "?":
                cur.setdefault("anti_r", []).append(hx(v[1]))
            if len(v) > 2 and v[2].startswith("["):
                cur.setdefault("useable", []).append([int(x, 16) if x != "?" else 0 for x in v[2].strip("[]").split()])
        elif kind == FIRST and cur is not None:
            cur["choices"].append(unq(v[0]))
        elif kind == STORE and cur is not None:
            cur["regions"].append(dict(reg=unq(v[0]), first=hx(v[1]), last=hx(v[2]), benefit=v[3]))
        elif kind == STATE:
            st = dict(maxlive=hx(v[0]), ntup=hx(v[1]), live=arr(v[2]), blk=arr(v[3]), info=arr(v[4]), speed=hx(v[5]))
            if len(v) >= 6 + len(AVAIL_REGS):
                st["avail"], st["prefwin"] = {}, {}
                for r, t in zip(AVAIL_REGS, v[6:6 + len(AVAIL_REGS)]):
                    x = arr(t) if t.startswith("[") else []
                    st["avail"][r] = [(x[2 * k], x[2 * k + 1]) for k in range(len(x) // 2)]
                np_ = 6 + len(AVAIL_REGS)
                for r, t in zip(PREFWIN_REGS, v[np_:np_ + len(PREFWIN_REGS)]):
                    x = arr(t) if t.startswith("[") else []
                    st["prefwin"][r] = [(x[2 * k], x[2 * k + 1]) for k in range(len(x) // 2)]
                na = np_ + len(PREFWIN_REGS)
                if len(v) >= na + 3:
                    st["anti"] = [hx(x) if x != "?" else None for x in v[na:na + 3]]
            rec["state"] = st
            ilrec[th] = rec       # --il: the IL as NColDoLiveness numbered it follows (T lines)
        elif kind == GIVEBACK:
            rec["given"].append(unq(v[0]))
        elif kind == REWRITE:
            nm = symtext(v[0], v[1], v[2])
            for x in reversed(rec["cands"]):
                if x["name"] == nm:
                    x["final"] = [unq(y) for y in v[3:]]
                    break
    return fns


def prio_terms(c, st):
    """The global-priority formula of NColPrioritizeCandidates (docs/c2/ncolor.md 2) from the traced liveness:
    [(block, appearances, term)], total. None when the candidate is local or the state is missing."""
    if not st or not c.get("ranges"):
        return None
    blk = lambda t: st["blk"][t] if t < len(st["blk"]) else None
    if blk(c["first"]) is not None and blk(c["first"]) == blk(c["last"]):
        return None
    depth = lambda b: st["info"][b * 13 + 11] if (b * 13 + 11) < len(st["info"]) else 0
    terms, total = [], 0
    for lo, hi in c["ranges"]:
        b0, b1 = blk(lo), blk(hi)
        if b0 is None or b1 is None:
            return None
        for b in range(b0, b1 + 1):
            k = sum(1 for a in c.get("apps", []) if blk(a) == b)
            sh = 2 * depth(b) if st["speed"] else 0
            live = st["live"][b] if b < len(st["live"]) else 0
            t = (((st["maxlive"] + 4) - live) * k * 8 << sh) if k else -(live << sh)
            terms.append((b, k, live, depth(b), t))
            total += t
    return terms, total


def report(cands, given, state=None):
    out = []
    w = max([len(c["name"]) for c in cands] + [9])
    for i, c in enumerate(cands):
        regs = ", ".join("%s %x..%x" % (r["reg"], r["first"], r["last"]) for r in c["regions"]) or "(no register)"
        notes = []
        ch = [x for x in c["choices"] if x != "-"]
        if c["regions"] and ch and ch[0] != c["regions"][0]["reg"]:
            notes.append("first choice %s" % ch[0])
        if len(c["regions"]) > 1:
            notes.append("split")
        out.append("%3d  %-*s  prio %-8s  %4x..%-4x -> %s%s" % (
            i, w, c["name"], "%d" % c["prio"] if c["prio"] is not None else "?", c["first"], c["last"], regs,
            "  ; " + ", ".join(notes) if notes else ""))
        if state is not None and "apps" in c:
            pt = prio_terms(c, state)
            if pt is None:
                out.append("       local: prio = %d tuples - length %d" % (state["ntup"], c["last"] - c["first"]))
            else:
                out.append("       global: " + "  ".join(
                    "B%d:%s%+d" % (b, "%dx(%d-%d)" % (k, state["maxlive"] + 4, live) if k else "live%d" % live, t)
                    + ("<<%d" % (2 * d) if d and state["speed"] else "") for b, k, live, d, t in pt[0])
                    + "  = %d%s" % (pt[1], "" if pt[1] == c["prio"] else "  (traced %s: model off)" % c["prio"]))
    if given:
        out.append("given back by NColPostspill: %s" % ", ".join(given))
    if state is not None:
        out.append("max live %d (+4 = the per-appearance base); live per block: %s" % (
            state["maxlive"], " ".join("B%d:%d" % (b, n) for b, n in enumerate(state["live"][:max(state["blk"][:state["ntup"]] or [0]) + 1]))))
    return "\n".join(out) + "\n"


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="c2ncol.py", usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--why", action="store_true", help="appearances per block and the priority formula per candidate")
    ap.add_argument("--export", help="JSON of every function's NColor run (candidates, liveness, initial "
                    "availability): rasim's input; --func optional (empty: all functions)")
    ap.add_argument("--il", action="store_true", help="with --export: also the IL after prioritization, with raw "
                    "words (T R: branch targets) and each leaf's symbol (T S: root, size, offset), the IL at "
                    "NColSubsumption's entry and every REGSET leaf's registers (the calls' clobber sets), for ncolsim's "
                    "trace-free inputs (docs/c2/ncolor.md 11, 12)")
    ap.add_argument("--cov", action="store_true", help="with --export --il: NColBestCoverage's scores and clusters per "
                    "register, its new bests and the ranges NColSeparateProcessedRegion keeps (candidates get cov, "
                    "covbest, separate: the pieces, kept and rest; docs/c2/ncolor.md 4.4, 4.5)")
    ap.add_argument("--dpa", action="store_true", help="with --export --il --cov: NColDeterminePhysregAvail's order "
                    "loop per region (candidates get dpa: [event, register ..]; docs/c2/ncolor.md 4.3)")
    ap.add_argument("--keep")
    argv, cl = c2trace.split_cl(argv)
    a = ap.parse_args(argv)
    if not a.func and not a.export:
        sys.exit("c2ncol: --func is required (or --export)")
    hk = list(hooks())
    if a.why or a.export:
        hk[CAND] += "," + ",".join(why_specs())
        st = c2build.hooks("ncol_state", None) % dict(nblk=NBLK, ntup=NTUP, ninfo=13 * NBLK)
        if a.export:
            st += "," + ",".join("tf65c>0>%d*%d" % (4 * r, NAVAIL) for r in AVAIL_REGS)
            st += "," + ",".join("tf6a0>0>%d*%d" % (4 * r, NAVAIL) for r in PREFWIN_REGS)  # preference windows
            st += ",t119c8>0,t119cc>0,t119d0>0"     # the anti-preference sets: r12 / rbp, r13 / all three
            if a.il:          # the whole tuple stream (NColDoLiveness numbers every tuple from 1), raw words
                st += ",tdccc>0>8>0>0x1c%"
                os.environ.setdefault("C2TRACE_ILRAW", "12")
                os.environ.setdefault("C2TRACE_ILSYMW", "11")    # each leaf's symbol: +0xc root, +0x24 / +0x28
                os.environ.setdefault("C2TRACE_ILPAS", "28")
        hk.append(st)
        hk.append(c2build.hooks("ncol_fn", None) + (FN_FLAGS if a.export and a.il else ""))
        hk.append(c2build.hooks("ncol_pref", None))
        if a.export and a.il and c2build.name().startswith("17.00"):
            hk.append(PRE_HOOK)
            hk.append(REGSET_HOOK)
            hk.append(PERFUNC_HOOK)
            if a.cov or a.dpa:
                hk += [COVREG_HOOK, COVBEST_HOOK, SEPARATE_HOOK, MINKEPT_HOOK, MINREST_HOOK]
            if a.dpa:
                hk += DPA_HOOKS
    img, hk, rc, out, text = c2trace.run_trace(a.src, hk, cl or ["/O2"], keep=a.keep,
                                               func=a.func if a.func else (False if a.export else None))
    if rc:
        sys.stdout.write(out)
        return rc
    if a.raw:
        sys.stdout.write(text)
        return 0
    if a.export:
        import json
        fns = [f for f in parse_all(text, fn_flags=a.il) if f["cands"]]
        if a.il:
            for f in fns:                     # the REGSET probe ran: a function without an event has no such leaf
                f.setdefault("regsets", [])
        json.dump(dict(c2=c2build.name(), src=os.path.basename(a.src), functions=fns), open(a.export, "w"))
        print("%d functions, %d candidates -> %s" % (len(fns), sum(len(f["cands"]) for f in fns), a.export))
        return 0
    cands, given, state = parse(text)
    if not cands:
        sys.exit("c2ncol: no NColor events for %s (wrong --func, or the function was not optimized)" % a.func)
    sys.stdout.write(report(cands, given, state))
    return 0


if __name__ == "__main__":
    sys.exit(c2trace.pipe_safe(main, sys.argv[1:]))
