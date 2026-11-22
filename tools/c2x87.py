#!/usr/bin/env python3
"""c2x87: the x87 stack decisions cl 14.00 / 15.00 make after Color, traced, for tools/rasim (x87stack.rs).

Doc: docs/c2/x87.md section 12.

usage: c2x87.py SRC --func F [--export SNAP.json] [--keep DIR] [-- <cl args>]
       (also run by `c2trace.py regs --export` on 14.00 / 15.00, which adds the key `x87` to its snapshot)

One traced compile with five hooks (addresses from the build's own index, so 14.00 and 15.00 alike):
  BuildLiveOnEntryAndExitSets entry  the GF form as X87MapGFxToSTx maps it (after PerformPreMappingPeepholes):
                                     the plain IL dump ('%') and the globreg view ('^': blocks with their
                                     predecessor and successor lists, in list order)
  FPPeeps entry                      the mapper's output (ST form, after PerformPostMappingPeepholes): the IL dump
  MscCollectUsedRegs entry / exit    every function body generated before: name, return class (sym+0x37 & 0x1f:
                                     1 int, 5 float/double, 7 void), sym+0x20, and the count of x87 pushes its final
                                     code makes (2 = it calls something that is not x87-free, or has asm)
  LoAddCallScratchRegs entry / exit  every call of the function: the target's name and sym+0x20, and the kill set
                                     the call gets (first SBV chunk of each dst register-set leaf: GF0..GF7 are
                                     registers 0x1f..0x26)
Without --export it prints the calls and their kill sets and the callee table.
"""
import argparse, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace
from image import Image

FUNCS = ["BuildLiveOnEntryAndExitSets", "FPPeeps", "MscCollectUsedRegs", "LoAddCallScratchRegs"]
CURFUNC = {"14.00": "10d123ac", "15.00": "51258400"}


def _va(img, plain):
    """VA of the c2 function whose symbol is @plain@N (14.00) or ?plain@@... (15.00)."""
    for r in img.rows:
        s = r.get("symbol") or ""
        if s.startswith("@%s@" % plain) or s.startswith("?%s@@" % plain):
            return r["va"]
    sys.exit("c2x87: no function %s in this build's index" % plain)


def hooks():
    b = c2trace.c2build.name()
    key = "15.00" if b.startswith("15.00") else "14.00" if b == c2trace.c2build.DEFAULT else None
    if c2trace.c2build.get("arch") == "x64":
        sys.exit("c2x87: not applicable to %s: x64 code uses SSE, there is no x87 stack" % b)
    if key is None:
        sys.exit("c2x87: cl 13.10 has no X87MapGFxToSTx (its x87 view is FPColor's: regs --all-classes)")
    img = Image("c2")
    cf = CURFUNC[key]
    nm, ft = "g%s>0>0>4$" % cf, "g%s>0>8>0>0x1c" % cf
    v = {f: "%x" % _va(img, f) for f in FUNCS}
    return [
        "%s=%s,%s%%,%s^" % (v["BuildLiveOnEntryAndExitSets"], nm, ft, ft),
        "%s=%s,%s%%" % (v["FPPeeps"], nm, ft),
        "%s=s1>0>4$,s1>0>0x37,s1>0>0x20/xa2>0" % v["MscCollectUsedRegs"],
        "%s=%s,s1>0x28>0x18>4$,s1>0x28>0x18>0x20,s1>0x28>8/x" % (v["LoAddCallScratchRegs"], nm) +
        ",".join("a1>0x2c%s>8,a1>0x2c%s>0x18>0#3" % (">0" * k, ">0" * k) for k in range(6)),
    ]


def _num(x):
    try:
        return int(x, 16)
    except (TypeError, ValueError):
        return None


def _str(x):
    return x[1:-1] if isinstance(x, str) and x.startswith('"') else None


def _gfkill(vals):
    """vals: (kind, chunk) pairs of dst leaves; the GF registers (0x1f..0x26) in a kind-0xb leaf's first chunk."""
    out = dict(gf=[], int=[])           # no register-set leaf: the call kills nothing (LoAddCallScratchRegs frees it)
    for kind, ch in vals:
        k = _num(kind)
        if k is None or k & 0xff != 0xb or not isinstance(ch, str):
            continue
        w = [_num(x) for x in ch.strip("[]").split()]
        if len(w) < 3 or w[0] != 0:
            continue
        lo, hi = w[1], w[2]
        regs = [r for r in range(0x1f, 0x27) if ((lo >> r) & 1 if r < 32 else (hi >> (r - 32)) & 1)]
        ints = [r for r in range(1, 9) if (lo >> r) & 1]
        out = dict(gf=[r - 0x1f for r in regs], int=ints)
    return out


def parse(log, funcpat):
    """-> list of per-function dicts {name, gf: [lines], gview: [lines], st: [lines], calls: [...]}, plus callees."""
    funcs, callees, cur, dump = [], [], None, None
    byname = {}
    for ln in log.splitlines():
        if ln.startswith("T "):
            if dump is not None:
                dump.append(ln[2:])
            continue
        if "|" not in ln or ln.startswith("#"):
            continue
        head, _, rest = ln.partition("|")
        f = head.split()
        ev, hk = f[0], int(f[3])
        v = c2trace._split(rest)
        dump = None
        if hk == 2:
            if ev == "E":
                callees.append(dict(name=_str(v[0]), ret=(_num(v[1]) or 0) & 0x1f, flags=_num(v[2])))
            elif callees:
                callees[-1]["pushes"] = _num(v[0])
            continue
        name = _str(v[0]) if v else None
        if hk in (0, 1, 3) and ev == "E":
            if name is None or not c2trace.func_match(funcpat, name):
                cur = None
                continue
            cur = byname.get(name)
            if cur is None or (hk == 0 and cur.get("gf")):
                cur = dict(name=name, gf=[], gview=[], st=[], calls=[])
                funcs.append(cur)
                byname[name] = cur
        if cur is None:
            continue
        if hk == 0:
            # the '%' dump comes first, then the '^' view: T G lines go to gview
            cur["_dump0"] = True
            dump = _Split(cur)
        elif hk == 1 and ev == "E":
            dump = cur["st"]
        elif hk == 3 and ev == "E":
            cur["calls"].append(dict(target=_str(v[1]), tflags=_num(v[2]), tkind=_num(v[3])))
        elif hk == 3 and ev == "X" and cur["calls"]:
            cur["calls"][-1]["kill"] = _gfkill([(v[i], v[i + 1]) for i in range(0, len(v) - 1, 2)])
    for fn in funcs:
        fn.pop("_dump0", None)
    return funcs, callees


class _Split(list):
    """Receives the lines after the BuildLiveOnEntryAndExitSets event: 'G ...' to gview, the rest to gf."""
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def append(self, ln):
        (self.fn["gview"] if ln.startswith("G ") else self.fn["gf"]).append(ln)


def export(fn, callees, cl=()):
    return dict(build=c2trace.c2build.name(), cl=list(cl), func=fn["name"], gf=fn["gf"], gview=fn["gview"], st=fn["st"],
                calls=fn["calls"], callees=callees)


def collect(src, func, cl, keep=None):
    img, hk, rc, out, log = c2trace.run_trace(src, hooks(), cl, keep=keep, quiet=True)
    if rc:
        sys.stdout.write(out)
    funcs, callees = parse(log, func)
    return funcs, callees


def add_to_snapshot(path, src, func, cl, keep=None):
    """`regs --export`: add key x87 to the snapshot(s) written (one function each)."""
    funcs, callees = collect(src, func, cl, keep)
    snap = json.load(open(path))
    for fn in funcs:
        if fn["name"] == snap.get("func"):
            snap["x87"] = export(fn, callees, cl)
            json.dump(snap, open(path, "w"), indent=0)
            return True
    return False


def main(argv):
    ap = argparse.ArgumentParser(prog="c2x87.py", usage=__doc__.replace("%", "%%"))   # argparse %-formats the usage text
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    ap.add_argument("--export")
    ap.add_argument("--keep")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    funcs, callees = collect(a.src, a.func, cl, a.keep)
    if not funcs:
        sys.exit("c2x87: no function matching %r reached X87MapGFxToSTx" % a.func)
    if a.export:
        if os.path.exists(a.export):
            snap = json.load(open(a.export))
            if snap.get("func") in [f["name"] for f in funcs]:
                snap["x87"] = export([f for f in funcs if f["name"] == snap["func"]][0], callees, cl)
                json.dump(snap, open(a.export, "w"), indent=0)
                print("# added x87 to %s (%s)" % (a.export, snap["func"]))
                return
        json.dump(dict(func=funcs[0]["name"], x87=export(funcs[0], callees, cl)), open(a.export, "w"), indent=0)
        print("# exported %s (%s)" % (a.export, funcs[0]["name"]))
        return
    for c in callees:
        print("callee %-40s ret %d pushes %s" % (c["name"], c["ret"], c.get("pushes")))
    for fn in funcs:
        print("==", fn["name"], "%d GF-form lines, %d mapped lines" % (len(fn["gf"]), len(fn["st"])))
        for c in fn["calls"]:
            print("  call %-40s kill %s" % (c["target"], c["kill"]))


if __name__ == "__main__":
    main(sys.argv[1:])
