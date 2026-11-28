#!/usr/bin/env python3
"""regfix64: a lifted function whose code is right but whose registers are not -> source edits that give the
original's registers (the x64 allocator NColor, through vs2005-cpp's c2ncol / ncolsim / realize).

    python3 tools/regfix64.py VA --binary EXE [--index FILE] [--src FILE] [--flags /O2,/GS-,...] [--max-compiles 40]
                              [--work DIR]

1. the lifter's best variant (or --src) is compiled and compared with the original (lift2il64.check: C2_CL64 and
   x64check): x64metrics' register mapping over the shape-aligned instructions says which original register each
   of ours stands for ("regpairs")
2. web names t1xxx become v1xxx (lift2il64 already names them so; ncolsim takes t<digits> for compiler temporaries)
3. c2ncol (traced cl 17.00) gives the register each variable got in our compile
4. realize --from-ncol --want-regs asks for the original's register for each callee-saved one and writes variants
5. every variant is compared the same way; the matches are listed (WORK/<va>/match_N.cpp)
   No register to move (the difference is the stack frame): frameinfo64's reading of each address-taken slot
   (hidden return slot or out parameter) and c2scope's frame packer against the original (slots --image --search).

The variants realize writes can carry steering constructs (`if (v & 1) { S } else { S }`: both arms the same, so
the code does not change but the variable gains an appearance); see the five-tries rule before submitting."""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lift2il64 as L    # noqa: E402

ROOT = os.path.dirname(HERE)
JOBS = 6
CALLEE_SAVED = {"rbx", "rbp", "rsi", "rdi", "r12", "r13", "r14", "r15"}


def wanted(got, pairs):
    """got: [(variable, our register)] (c2ncol); pairs: {our register: the original's} (x64metrics regpairs).
    -> (want: {variable: original register} for the callee-saved ones, moves: the part of want that differs)."""
    want = {}
    for name, reg in got:
        o = pairs.get(reg)
        if reg in CALLEE_SAVED and o in CALLEE_SAVED:
            want[name] = o
    return want, {k: v for k, v in want.items() if dict(got)[k] != v}


def check1(va, size, text, flags, work):
    """lift2il64.check under one flag set, its x64metrics fields at the top level."""
    r = L.check(va, size, text, work, [list(flags)])
    m = r.get("metrics") or {}
    return dict(r, edits=m.get("edits"), edits_noreg=m.get("edits_noreg"), mapcost=m.get("mapcost"),
                regpairs=m.get("regpairs"))


def check(va, size, texts, flags, work):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(JOBS) as ex:
        return list(ex.map(lambda nt: check1(va, size, nt[1], flags, os.path.join(work, "c%d" % nt[0])),
                           enumerate(texts)))


def best_lift(va, size, entry, work):
    """(text, flags, result) of the lifter variant and flag set with the fewest edits."""
    img = L.Image()
    vs = [t for _, t in L.variants(img, va, size)][:4]
    fls = L.flag_sets(va, entry)[:2]
    combos = [(t, f) for t in vs for f in fls]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(JOBS) as ex:
        rs = list(ex.map(lambda k: check1(va, size, combos[k][0], combos[k][1], os.path.join(work, "v%d" % k)),
                         range(len(combos))))
    best = None
    for (t, f), x in zip(combos, rs):
        if x.get("status") == "match":
            return t, f, x
        if x.get("edits") is not None and (best is None or x["edits"] < best[2]["edits"]):
            best = (t, f, x)
    return best or (combos[0][0], combos[0][1], rs[0])


def c2ncol(src, func, flags):
    """[(short name, register)] of the named candidates, from a traced compile."""
    cmd = ["uv", "run", "tools/c2ncol.py", src, "--func", func, "--c2", "17.00", "--"] + list(flags)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800)
    out = []
    for ln in p.stdout.splitlines():
        m = re.match(r"^\s*\d+\s+(\S+)\s+prio\s+-?\d+\s+\S+\s+->\s+(\w+)", ln)
        if m:
            name = m.group(1).split("$")[0]
            if not re.match(r"^[ts]\d+$", name):
                out.append((name, m.group(2)))
    return out, p.stdout + p.stderr


def realize(src, func, want, flags, keep, max_compiles):
    spec = ",".join("%s=%s" % kv for kv in sorted(want.items()))
    cmd = ["uv", "run", "tools/realize.py", "--from-ncol", src, "--func", func, "--want-regs", spec,
           "--max-compiles", str(max_compiles), "--keep", keep, "--c2", "17.00", "--"] + list(flags)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=7200)
    return p.stdout + p.stderr


def frame_report(va, size, src, func, flags):
    """Registers are right but the code still differs: the stack frame. The binary's own reading of each
    address-taken slot (frameinfo64: hidden return slots vs out parameters), then c2scope's frame packer against
    the original (c2trace.py slots --image --search)."""
    import frameinfo64
    print("stack slots passed to calls (from the binary):")
    print("\n".join(frameinfo64.report(L.Image(), va, size)) or "  none")
    cmd = ["uv", "run", "tools/c2trace.py", "slots", src, "--func", func, "--image", L.BINARY, "--va", "0x%x" % va,
           "--search", "--c2", "17.00", "--"] + list(flags)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800)
    out = p.stdout
    i = out.find("## ours against the original")
    print(out[i:] if i >= 0 else (out + p.stderr)[-1500:])


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("va")
    ap.add_argument("--binary", help="the x64 executable (or LIFT64_BINARY)")
    ap.add_argument("--index", help="function index JSON (or LIFT64_INDEX); default: the executable's .pdata")
    ap.add_argument("--size", type=int)
    ap.add_argument("--src")
    ap.add_argument("--flags")
    ap.add_argument("--max-compiles", type=int, default=40)
    ap.add_argument("--work", default=os.path.join(os.path.dirname(HERE), "work", "regfix"))
    a = ap.parse_args(argv)
    va = int(a.va, 16)
    L.BINARY, L.INDEX = a.binary or L.BINARY, a.index or L.INDEX
    entry = dict(L.index(L.Image()).get(va) or {})
    if a.size:
        entry["size"] = a.size
    if "size" not in entry:
        sys.exit("regfix64: no size for 0x%x: give --size" % va)
    size = entry["size"]
    d = os.path.join(a.work, "%x" % va)
    os.makedirs(d, exist_ok=True)
    func = "FUN_%x" % va
    if a.src:
        text = open(a.src).read()
        flags = a.flags.split(",") if a.flags else L.flag_sets(va, entry)[0]
        r = check(va, size, [text], flags, os.path.join(d, "start"))[0]
    else:
        text, flags, r = best_lift(va, size, entry, os.path.join(d, "lift"))
    print("start: %s edits %s shape %s mapcost %s  flags %s" % (r.get("status"), r.get("edits"),
                                                                  r.get("edits_noreg"), r.get("mapcost"), " ".join(flags)))
    if r.get("status") == "match":
        open(os.path.join(d, "match_0.cpp"), "w").write(text)
        print("already matches")
        return 0
    pairs = r.get("regpairs") or {}
    text = re.sub(r"\bt(1\d{3})\b", r"v\1", text)
    src = os.path.join(d, "start.cpp")
    open(src, "w").write(text)
    got, log = c2ncol(src, func, flags)
    if not got:
        print("c2ncol gave no candidates:\n" + log[-800:])
        return 1
    want, moves = wanted(got, pairs)
    print("ours -> original registers: %s" % ", ".join("%s->%s" % kv for kv in sorted(pairs.items()) if kv[0] != kv[1]))
    print("variables to move: %s" % (", ".join("%s %s->%s" % (k, dict(got)[k], v) for k, v in sorted(moves.items()))
                                     or "none"))
    if not moves:
        frame_report(va, size, src, func, flags)
        return 1
    keep = os.path.join(d, "rz")
    out = realize(src, func, want, flags, keep, a.max_compiles)
    print("\n".join(ln for ln in out.splitlines() if "not ported" not in ln)[-1500:])
    vs = sorted(glob.glob(os.path.join(keep, "v*", "v*.cpp")))
    if not vs:
        return 1
    res = check(va, size, [open(p).read() for p in vs], flags, os.path.join(d, "verify"))
    n = 0
    for p, x in zip(vs, res):
        if x.get("status") == "match":
            n += 1
            dst = os.path.join(d, "match_%d.cpp" % n)
            open(dst, "w").write(open(p).read())
            print("MATCH %s -> %s" % (os.path.basename(p), dst))
        else:
            print("%s: %s edits %s shape %s" % (os.path.basename(p), x.get("status"), x.get("edits"),
                                                 x.get("edits_noreg")))
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
