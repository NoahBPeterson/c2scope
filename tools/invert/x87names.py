#!/usr/bin/env python3
"""x87 rounding points -> which values are float locals / aggregate members / kept in expressions.
Doc: docs/c2/invert.md section 4.

Forward rules (x87.md 12.1-12.2, x87round.md section 3-4): a value comes back rounded (`fstp dword/qword [m]` ...
`fld [m]`) when it is a float argument, a computed float return, live across a call that kills the x87 stack, spilled
under stack pressure (7+ deep), or assigned to something that has a memory home at Globregs: on 14.00 / 15.00 every
named float / double variable and every float-typed temporary (cast, inlined helper result); on 13.10 a named float
scalar only across a call / when volatile / under pressure, but a float field of an aggregate local always.

Inverse, per rounding point of the ORIGINAL (from its bytes alone: x87round's simulation of the original), the
source property required, and per value the original keeps at 80 bits the property forbidden:

  argument    the point stores into the argument area / feeds it   -> the parameter is float (double for qword)
  return      the returned value goes through a temporary          -> a computed float return (any spelling)
  call        a stack-killing call lies between store and reload   -> nothing to choose: any variable or temporary
                                                                      live across the call is rounded
  pressure    the stack is 7+ deep at the store                    -> nothing to choose (keep the expression's shape)
  named       no call, not deep, 14.00 / 15.00                     -> a float (dword) / double (qword) local, a
                                                                      float temporary or cast
  member      no call, not deep, 13.10                             -> a float field of an aggregate local, a float
                                                                      parameter of an inlined helper, a volatile, or a
                                                                      named float that FPColor leaves in memory (split
                                                                      by a stack-order blocker, then dropped by
                                                                      FPSpillWorthless: `rasim need SNAP stack:V`); a
                                                                      float local FPColor keeps is NOT rounded
  kept        an 80-bit value the original reuses without a store  -> no float local / float temporary for it on
                                                                      14.00 / 15.00 (fold it into its uses, or double)

usage: x87names.py orig --image EXE --va VA --build B     the required property of each original rounding point
       x87names.py measure [--limit N] [--jobs J]         on Mount & Blade's byte-exact x87 functions (13.10.3052):
                                                          predicted property vs the reason class x87round finds in
                                                          the true source
"""
import os, re, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def require(pt, build):
    """The source property one original rounding point needs (x87round.Point)."""
    if pt.kind == "arg" or pt.feeds_arg:
        return "argument"
    if pt.ret and not pt.calls:
        return "return"
    if pt.calls:
        return "call"
    if pt.depth >= 7:
        return "pressure"
    return "member" if build.startswith("13.10") else "named"


# x87round's reason classes that each predicted property is compatible with (the true source's reason)
COMPAT = {
    "argument": {"argument"},
    "return": {"return"},
    "call": {"call between def and use"},
    "pressure": {"spill under x87 pressure"},
    "named": {"float local", "float parameter", "float temporary", "cast to float", "inlined helper (float return/result)",
              "float parameter of an inlined helper", "volatile / address-taken", "float local (unlocated)",
              "float member of an aggregate local", "float member of an aggregate parameter"},
    "member": {"float member of an aggregate local", "float member of an aggregate parameter",
               "volatile / address-taken", "float parameter of an inlined helper",
               "float local", "float parameter"},      # a named float FPColor did not keep on the stack (below)
}


def orig_points(image, va, size=None):
    import x87round
    oi = x87round.orig_ins(None, image, va)
    sb = x87round.finish(x87round.simulate(oi, "original"))
    return [pt for pt in sb.points if pt.reload is not None or pt.kind == "arg"]


def members_13(body):
    """The lifter's call site (/O2 units, cl 13.10): every float frame slot of the lift (`float v_lN;`) is a memory
    home the original stores and reloads, i.e. a rounding point; 13.10 rounds a plain float local only across a call,
    an aggregate's float field always (`member`), so each becomes the field of a one-member structure local."""
    decls = re.findall(r"(?m)^    float (v_l\d+);$", body)
    for v in decls:
        body = body.replace("    float %s;\n" % v, "    struct { float f; } %s;\n" % v, 1)
        body = re.sub(r"(?<![\w.])%s\b(?!\s*;\n)" % v, "%s.f" % v, body)
        body = body.replace("struct { float f; } %s.f;" % v, "struct { float f; } %s;" % v)
    return body


MB = os.path.join(HERE, "..", "..", "mountandblade-decomp")


def mb_rows():
    import glob, json
    out = []
    for m in glob.glob(os.path.join(MB, "match", "manifests", "**", "*.manifest.txt"), recursive=True):
        for ln in open(m):
            if ln.startswith("#") or not ln.strip():
                continue
            p = ln.split(None, 3)
            if len(p) < 3:
                continue
            out.append((os.path.normpath(os.path.join(MB, "match", p[0])), p[1], p[2].lower(),
                        p[3].strip() if len(p) > 3 else "/O2 /MT /Gy /TP"))
    f = open(os.path.join(HERE, "..", "work", "corpus", "mb.jsonl"))
    head = json.loads(f.readline())
    return sorted(out, key=lambda r: r[2]), head


def _measure_one(args):
    import shlex, subprocess, tempfile
    import x87round
    (src, sym, va, flags), head = args
    d = tempfile.mkdtemp(prefix="x87n_")
    asm = os.path.join(d, "x.asm")
    cl = [f for f in shlex.split(flags) if f != "/O2"] + head.get("src_inc", [])
    r = subprocess.run([sys.executable, os.path.join(HERE, "msvc.py"), "cl", "/nologo", "/c", "/O2"] + cl +
                       ["/FAsc", "/Fa" + asm, "/Fo" + os.path.join(d, "x.obj"), src], capture_output=True, text=True,
                       env=dict(os.environ, C2_BUILD=head.get("c2", "13.10.3052")))
    if not os.path.exists(asm):
        return args[0], None
    try:
        ins = x87round.our_instrs(asm, sym)
        sa = x87round.finish(x87round.simulate(ins, "ours"))
        srcinfo = x87round.Source(src, sym)
    except Exception as e:
        return args[0], None
    out = []
    for pt in sa.points:
        if pt.reload is None and pt.kind != "arg":
            continue
        cls, ev = x87round.reason(pt, sa, srcinfo, None)
        out.append((require(pt, head.get("c2", "13.10.3052")), cls, "%s %s depth %d size %d: %s" % (
            va, pt.home, pt.depth, pt.size, ev[:110])))
    return args[0], out


def measure(limit=120, jobs=6):
    import collections, json, random
    from concurrent.futures import ThreadPoolExecutor
    rows, head = mb_rows()
    # rows whose original has an x87 store to the frame or the argument area (cheap byte scan of the image)
    import x87round
    f = open(os.path.join(HERE, "..", "work", "corpus", "mb.jsonl"))
    f.readline()
    size = {json.loads(l)["va"]: json.loads(l)["size"] for l in f}
    from w8_emit import Image
    img = Image(head["image"])
    pick = []
    for r in rows:
        n = size.get(r[2])
        if not n:
            continue
        o = img.off(int(r[2], 16))
        b = img.data[o:o + n] if o is not None else b""
        if re.search(rb"[\xd9\xdd][\x5c\x5d\x1c\x54\x55\x14\x9c\x9d]", b) and re.search(rb"[\xd9\xdd][\x44\x45\x04\x84\x85]", b):
            pick.append(r)
    pick = sorted(random.Random(3).sample(pick, min(limit, len(pick))), key=lambda r: r[2])
    with ThreadPoolExecutor(jobs) as ex:
        got = list(ex.map(_measure_one, [(r, head) for r in pick]))
    tot = ok = 0
    conf = collections.Counter()
    nf = 0
    for row, res in got:
        if not res:
            continue
        nf += 1
        for pred, cls, ev in res:
            tot += 1
            hit = cls in COMPAT[pred]
            ok += hit
            conf[(pred, cls, hit)] += 1
            if not hit and cls == "float local":
                print("  float-local miss: " + ev)
    print("Mount & Blade byte-exact x87 functions compiled: %d of %d picked; rounding points: %d; required property "
          "predicted from the code alone compatible with the true source's reason: %d (%.1f%%)" %
          (nf, len(pick), tot, ok, 100.0 * ok / max(1, tot)))
    for (pred, cls, hit), n in conf.most_common():
        print("  %4d  %-9s <- %-45s %s" % (n, pred, cls, "ok" if hit else "MISS"))
    return got


def main(argv):
    import argparse
    if not argv or argv[0] not in ("orig", "measure"):
        print(__doc__)
        return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("--image")
    ap.add_argument("--va")
    ap.add_argument("--build", default="15.00")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--jobs", type=int, default=6)
    a = ap.parse_args(argv)
    if a.cmd == "measure":
        measure(a.limit, a.jobs)
        return 0
    for pt in orig_points(a.image, int(a.va, 16)):
        print("%-9s %s at %x%s" % (require(pt, a.build), pt.kind, pt.store.addr,
                                   " (calls %s)" % ",".join("%x" % c.addr for c in pt.calls) if pt.calls else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
