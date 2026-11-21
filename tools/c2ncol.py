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
CAND, FIRST, STORE, GIVEBACK, REWRITE, STATE, FN, PREF = range(8)
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


def parse_all(text):
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
    arr = lambda t: [hx(x) for x in t.strip("[]").split()]
    for ln in text.splitlines():
        if ln.startswith("# thread "):
            th = int(ln.split()[2])
            continue
        if ln.startswith("T ") and th in ilrec:
            ilrec[th].setdefault("il", []).append(ln[2:])
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
            c["cur"] = None
            fns.append(c["fn"])
            continue
        c = fn_of(th)
        rec, cur = c["fn"], c["cur"]
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
                    "words (T R: branch targets) and each leaf's symbol (T S: root, size, offset), for ncolsim's "
                    "trace-free inputs (docs/c2/ncolor.md 11)")
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
        hk.append(c2build.hooks("ncol_fn", None))
        hk.append(c2build.hooks("ncol_pref", None))
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
        fns = [f for f in parse_all(text) if f["cands"]]
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
