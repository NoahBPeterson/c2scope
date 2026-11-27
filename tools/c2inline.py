#!/usr/bin/env python3
"""c2inline: why c2 inlined a call here (or not), per call site, from one traced compile.  Doc: docs/c2/inline.md

usage: c2inline.py SRC [--func F] [--callee C] [--order] [--with OTHER.cpp,..] -- <cl args>       (C2_BUILD=13.10.3052 / 13.10.3077)

For every call site the inliner weighs (InlCallGraphDecision, the /O2 cost model of inline.md rule 3) it prints the
function being compiled, the callee, the inlining depth, and the model's numbers:

  size   the callee's compiled size in bytes
  c      size - benefits (+ bloat once caller + callee pass 9000 instructions); c > 450 rejects
  d      c - the weighted benefit terms; d < SizeLimit + 5 (14) accepts at once
  d'     d + size*B/255 (13.10), B = the callee's loop share in 255ths (FUNC node byte +0x4e; 0 for a loop-free
         callee; shown as loop%): on 13.10 a callee that is mostly loop is less likely to be inlined
  w      the call block's static weight (1e8 straight line, x5 per loop level, x0.5 in an if arm)
  v      (d'*1e8/w + (D-1)*d') / D, D = 7 when w <= 1e8 and 3 when w > 1e8 (a hotter block takes another
         branch, with no 64-bit divide); inline iff v < SpeedLimit (74). The tool recomputes v from d', w and
         flags a site where it disagrees with the traced value (another branch of the model).

  body   whether the callee's body had been compiled when the caller's site was weighed. READ, not inferred: bit 1 of
         the dword at callee FUNC+0x69, the bit ShouldInlineCallTuple tests before it asks the cost model (set once
         the callee was compiled and its call-graph data collected). "yes": the cost model above ran on the callee's
         compiled size. "no": the callee comes later in the TU's compile order, there is no size, and c1xx's IL
         size decides instead (inline.md rule 7). "never": the callee is not compiled as a function in this TU at all
         (extern, or an inline / static / __forceinline function that is only ever expanded: it has no code of its own). The compile order (@Inline@4
         entry order) is a cross-check: a row where it disagrees with the bit is marked NOTE.
  --order  also prints the TU's compile order, one line per function, with the in-TU callees compiled after it.

and the verdict with the step that decided it. A site the model never reached (a __forceinline callee, a callee
that is not compiled yet, a veto) shows only the verdict; the verdict is ConsiderCandidate's final answer. InlCallGraphDecision's "yes" is the cost model's; the
vetoes after it (InlBadCandidate, inline.md 8a) can still keep the call.

The probes are found by byte signature in the build's c2.dll (taken from 13.10.3052, where the function is split
into chunks: the size load at +0x2a, the c > 450 test, the early-accept compare, the v compare), so both 13.10
builds work without an address table. 14.00 has its own probe set: work/scratch/inline/hooks.sh.

Built for Mount & Blade's inline questions: ??_H / ??_E called in the original but inlined in ours.
"""
import argparse, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# (name, signature bytes, offset of the probed instruction in the signature, specs)
PROBES = [
    ("c", bytes.fromhex("81fec2010000"), 0, "esi,edi"),               # cmp esi, 0x1c2: esi = c, edi = size
    ("d", bytes.fromhex("83c0053bd8"), 3, "ebx,eax"),                  # add eax, 5; cmp ebx, eax: ebx = d
    ("B", bytes.fromhex("8a424e84c0"), 0, "edx>0x4c"),                # mov al, [edx+0x4e]: the callee's loop share
    ("v", bytes.fromhex("f7f18b4c241c3b05"), 6, "eax,ebp,ebx,esi,ecx>0"),  # div ecx; ..; cmp eax, [SpeedLimit]: v, w, d'

]


def reverse_first(order):
    """(index, name) for the first occurrence of each name."""
    seen = {}
    for i, n in enumerate(order):
        seen.setdefault(n, i)
    return [(i, n) for n, i in seen.items()]


def find_probes(dll):
    import pefile
    pe = pefile.PE(dll, fast_load=True)
    ib, data = pe.OPTIONAL_HEADER.ImageBase, pe.get_memory_mapped_image()
    out = {}
    for name, sig, off, spec in PROBES:
        hits = [m.start() for m in re.finditer(re.escape(sig), data)]
        if len(hits) != 1:
            sys.exit("c2inline: probe %s: %d matches of its signature in %s (want 1); this build needs a table"
                     % (name, len(hits), dll))
        out[name] = ("%x" % (ib + hits[0] + off), spec)
    return out


def main(argv):
    if "--" in argv:
        i = argv.index("--")
        argv, cl = argv[:i], argv[i + 1:]
    else:
        cl = []
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--func", help="only sites in functions whose decorated name contains this")
    ap.add_argument("--callee", help="only sites whose callee's name contains this")
    ap.add_argument("--order", action="store_true", help="print the compile order of the TU's functions first")
    ap.add_argument("--with", dest="more", help="LTCG: compile SRC and these (comma list) with /GL, link /LTCG under the trace "
                    "(c2ltcg.py's build) and report every function of the whole program; the order is then the linker's")
    ap.add_argument("--export", default="", help="with --with: names exported from the linked DLL (comma list; the roots, "
                    "/OPT:REF); default none: /OPT:NOREF, every function internal")
    ap.add_argument("--keep")
    a = ap.parse_args(argv)
    import c2build, c2trace
    if not c2build.name().startswith("13.10"):
        sys.exit("c2inline: 13.10 builds only (C2_BUILD=13.10.3052 / 13.10.3077); 14.00: work/scratch/inline/hooks.sh")
    cf = c2trace.curfunc_va()
    pr = find_probes(os.path.join(c2trace.TBIN, "c2.dll"))
    hooks = ["@Inline@4=g%s>0>0>4$" % cf,          # hook order is the log's hook index (parsed below)
             "@InlCallGraphDecision@12=s1>4>4$,s1>4>0x81/xeax"] + ["%s=%s" % pr[n] for n in ("c", "d", "v", "B")] + \
            ["@dop2_EncodeAndRelated@4=g%s>0>0>4$" % cf,        # hook 6: the function's code is emitted (it is done)
             # hook 7: one per call site weighed; the caller, the callee, the dword at callee FUNC+0x69 (bit 1: the callee
             # has been compiled, its call-graph data is complete), the depth. hook 8: ConsiderCandidate's final verdict
             "@ShouldInlineCallTuple@28=g%s>0>0>4$,eax>4>0x28>0x18>4$,eax>4>0x28>0x18>0x69,s2,s1" % cf,
             "@ConsiderCandidate@36=ecx/xeax"]
    if a.more:
        import c2ltcg
        os.environ["CCACHE"] = "0"
        os.environ["C2TRACE_REMOTE"] = "0"                    # the /GL compiles and the link run here (c2ltcg.py)
        if hasattr(c2trace, "run_remote"):
            c2trace.run_remote = lambda *x, **k: None
        more = [os.path.abspath(m) for m in a.more.split(",") if m]
        plain = c2trace.run_tool

        def ltcg_tool(args, trace=True, extra_env=None):
            src = args[-1]
            obj = next(x[3:] for x in args if x.startswith("/Fo"))
            clx = [x for x in args[:-1] if not x.startswith(("/FA", "/Fa", "/Fo"))]
            rc_, text, _dll = c2ltcg.build([src] + more, clx, os.path.dirname(obj), [e for e in a.export.split(",") if e], plain, trace, extra_env, None)
            return rc_, text
        c2trace.run_tool = ltcg_tool
    if a.more and not a.keep:
        import tempfile
        a.keep = tempfile.mkdtemp(prefix="c2inline_ltcg_")       # the map file is read from here
    img, hk, rc, out, log = c2trace.run_trace(a.src, hooks, cl, keep=a.keep, func=False)
    if rc:
        sys.stdout.write(out)
        return rc
    # raw log: `E seq nest hook eax ecx edx ebx ebp esi edi esp | specs` (entry / probe), `X seq nest hook eax .. | specs`
    # hooks: 0 Inline (one per function, in compile order; its ebx is the FUNC), 1 ICGD (ebp = the caller's FUNC),
    # 2 c, 3 d, 4 v, 5 B, 6 EncodeAndRelated (one per function, after its decisions), 7 ShouldInlineCallTuple (one per
    # site; its callee-ready bit is the "body" column), 8 ConsiderCandidate (its exit is the final verdict)
    REG = ("eax", "ecx", "edx", "ebx", "ebp", "esi", "edi", "esp")
    s32 = lambda x: (int(x, 16) ^ 0x80000000) - 0x80000000
    names, rows, cur = {}, [], None
    order, done = [], {}           # order: names in @Inline@4 entry order; done: name -> position in the emission order
    for line in log.splitlines():
        if not line[:2] in ("E ", "X ") or "|" not in line:
            continue
        head, tail = line.split("|", 1)
        f = head.split()
        kind, hook = f[0], int(f[3])
        regs = dict(zip(REG, f[4:]))
        v = re.findall(r'"[^"]*"|\S+', tail)
        if kind == "E" and hook == 0:
            names[regs.get("ebx")] = v[0].strip('"') if v else "?"
            order.append(names[regs.get("ebx")])
        elif kind == "E" and hook == 6:
            done.setdefault(v[0].strip('"') if v else "?", len(done))
        elif kind == "E" and hook == 7:            # ShouldInlineCallTuple: one per call site weighed
            dp = int(v[3], 16)
            prow = next((r for r in reversed(rows) if r["depth"] == dp - 1), None) if dp > 1 else None
            top = names.get(v[4].lower().lstrip("0") or "0", v[0].strip('"'))       # s1 = the caller's FUNC
            cur = dict(func=("in " + prow["callee"]) if prow else top, callee=v[1].strip('"'), depth=dp,
                       top=prow["top"] if prow else top,                         # the function being compiled
                       ready=bool(int(v[2], 16) & 2), size=0)                   # callee FUNC+0x69 bit 1
            rows.append(cur)
        elif kind == "E" and hook == 1 and cur is not None:
            cur["size"] = int(v[1], 16) if len(v) > 1 and v[1] != "?" else 0
        elif kind == "X" and hook == 1 and cur is not None:
            cur["yes"] = int(f[4], 16) != 0
        elif kind == "X" and hook == 8 and cur is not None:
            cur["final"] = int(f[4], 16) != 0
        elif cur is not None and hook == 2:
            cur["c"] = s32(v[0])
        elif cur is not None and hook == 3:
            cur["d"], cur["dlim"] = s32(v[0]), s32(v[1])
        elif cur is not None and hook == 5:
            cur["B"] = (int(v[0], 16) >> 16) & 0xFF
        elif cur is not None and hook == 4:
            cur["v"], cur["w"], cur["dp"] = s32(v[0]), (int(v[2], 16) << 32) | int(v[1], 16), s32(v[3])
    pos = dict((n, i) for i, n in reverse_first(order))
    calls = {}
    for r in rows:
        calls.setdefault(r["top"], [])
        if r["callee"] not in calls[r["top"]]:
            calls[r["top"]].append(r["callee"])
    if a.order:
        print("compile order (%d functions; @Inline@4 entry order; each is compiled through to its code emission "
              "before the next starts):" % len(order))
        for i, n in enumerate(order):
            later = [c for c in calls.get(n, []) if c in pos and pos[c] > i]
            print("  %2d  %-36s%s" % (i + 1, n, ("  calls later-compiled (body not visible): " + ", ".join(later)) if later else ""))
        print("  emission order %s the compile order" % ("=" if sorted(done, key=done.get) == order else "DIFFERS FROM"))
        if a.more:
            import c2ltcg
            lay = [n for n, _va, _o in c2ltcg.read_map(os.path.join(c2trace.keep_path(a.keep), "ltcg.map"))
                   if not n.startswith(("$L", "__ehhandler"))]
            print("image layout (link map, address order; under /LTCG this is the IL order, not the compile order):")
            for i, n in enumerate(lay):
                print("  %2d  %s" % (i + 1, n))
        print()
    shown = 0
    print("%-34s %-34s %2s %5s %5s %5s %5s %5s %10s %4s %-6s %s" % ("caller", "callee", "dp", "size", "c", "d", "loop%",
                                                                 "d'", "w", "v", "body", "verdict"))
    for r in rows:
        if a.func and a.func not in (r["func"] or "") and a.func not in (r["top"] or ""):   # a site nested in an inlinee shows for the function it is expanded into too
            continue
        if a.callee and a.callee not in r["callee"]:
            continue
        if "c" not in r:
            why = "decided before the cost model" if r["ready"] else \
                "callee not compiled: no cost model, c1xx IL size (inline.md rule 7)"
        elif r["c"] > 450:
            why = "c > 450"
        elif "v" not in r and "d" in r and r["d"] < r["dlim"]:
            why = "d < %d: accepted at once" % r["dlim"]
        elif "v" in r:
            why = "v %s 74" % ("<" if r.get("yes") else ">=")
        else:
            why = "rejected after d (a flag test)"
        if "v" in r and r["w"]:
            D = 7 if r["w"] <= 100000000 else 3
            pv = (r["dp"] * 100000000 // r["w"] + (D - 1) * r["dp"]) // D
            if pv != r["v"]:
                why += "; model gives v=%d: another branch" % pv
        body = "never" if r["callee"] not in pos else "yes" if r["ready"] else "no"
        # the same from the order: the callee came through its code emission before the top-level caller started
        inferred = "yes" if r["callee"] in done and pos[r["callee"]] < pos.get(r["top"], -1) else "no"
        if body != "never" and body != inferred and r["callee"] != r["top"]:
            why += "; NOTE the order says body=%s" % inferred
        verdict = "INLINE" if r.get("final", r.get("yes")) else "call"
        if "yes" in r and r["yes"] != r.get("final", r["yes"]):
            why += "; the cost model said %s, then vetoed" % ("yes" if r["yes"] else "no")
        print("%-34s %-34s %2d %5d %5s %5s %5s %5s %10s %4s %-6s %s (%s)" % (
            (r["func"] or "?")[:34], r["callee"][:34], r["depth"], r["size"], r.get("c", ""), r.get("d", ""),
            ("%d" % round(r["B"] * 100 / 255)) if "B" in r else "",
            r.get("dp", ""), r.get("w", ""), r.get("v", ""), body, verdict, why))
        shown += 1
    if not shown:
        print("(no call site matched)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
