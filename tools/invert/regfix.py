#!/usr/bin/env python3
"""regorder applied to a lifted unit: one traced compile of OUR unit (the model state), regmap against the original,
the definition-order constraints solved (regorder.py), the statements moved, one compile to verify.
Doc: docs/c2/invert.md section 3.

Not a search: the traced compile is the analysis step (rasim's state of our code), the edit follows from the model's
answer, and the verification compile only reports whether the bytes now match.

usage: regfix.py CORPUS VA [VA..] [--out DIR] [--jobs J]
       regfix.py CORPUS --vas FILE ...
Per row: the registers regmap says we got wrong, how each was classified (Color pop order with a priority tie -> a
definition move; a priority gap; a scratch-register pick, which Color's order does not decide), the edit made, and
the byte diff before / after.
"""
import json, os, re, shlex, subprocess, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regorder

WANT = re.compile(r"^(\S+)\s+(\S+)\s+(\S+?):\d+\s+.*?\s+WANTS (\w+)\s*$")


def orig_asm(img, va, size, path):
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    o = img.off(va)
    with open(path, "w") as f:
        for i in md.disasm(img.data[o:o + size], va):
            f.write("%x %s %s\n" % (i.address, i.mnemonic, i.op_str))


def decorated(text, va):
    """Our function's decorated name (regmap's --asm-func) from the unit's signature."""
    m = re.search(r"(__\w+) FUN_%08x\(([^)]*)\)" % va, text)
    conv = m.group(1) if m else "__cdecl"
    nargs = len([p for p in (m.group(2).split(",") if m else []) if p.strip()])
    return {"__fastcall": "@FUN_%08x@%d" % (va, 4 * nargs), "__stdcall": "_FUN_%08x@%d" % (va, 4 * nargs)}.get(
        conv, "_FUN_%08x" % va)


def regmap(src, func, dec, asm, flags, build):
    r = subprocess.run([sys.executable, os.path.join(HERE, "c2trace.py"), "regmap", src, "--func", func, "--asm-vs",
                        asm, "--asm-func", dec, "--all", "--"] + flags, capture_output=True, text=True,
                       env=dict(os.environ, C2_BUILD=build))
    out, scratch = {}, set()
    last = None
    for ln in r.stdout.splitlines():
        m = WANT.match(ln)
        if m:
            out[m.group(2)] = m.group(4)
            last = m.group(2)
        elif last and "from: scratch pick" in ln:
            scratch.add(last)
        elif not ln.startswith(" "):
            last = None
    return out, scratch


def export(src, func, flags, build, path):
    subprocess.run([sys.executable, os.path.join(HERE, "c2trace.py"), "regs", src, "--func", func, "--export", path,
                    "--"] + flags, capture_output=True, text=True, env=dict(os.environ, C2_BUILD=build))
    return os.path.exists(path)


def move_def(text, u, v):
    """Move u's single definition statement to just before v's (a straight-line move inside one block): allowed when
    u is defined once, v's definition comes first, nothing in between reads or writes u or writes what u's right side
    reads, and, when the right side reads memory or calls, nothing in between stores or calls."""
    lines = text.split("\n")
    du = [i for i, l in enumerate(lines) if re.match(r"^(\s+)%s = " % re.escape(u), l)]
    dv = [i for i, l in enumerate(lines) if re.match(r"^(\s+)%s = " % re.escape(v), l)]
    if len(du) != 1 or not dv:
        return None
    iu, iv = du[0], dv[0]
    if iv >= iu or lines[iu][:len(lines[iu]) - len(lines[iu].lstrip())] != \
            lines[iv][:len(lines[iv]) - len(lines[iv].lstrip())]:
        return None
    rhs = lines[iu].split("=", 1)[1]
    reads = set(re.findall(r"\b([at]\d+|v_l\d+|self)\b", rhs))
    mem = "*(" in rhs or "->" in rhs or re.search(r"\bFUN_|\bg_", rhs)
    for l in lines[iv:iu]:
        s = l.strip()
        if s.endswith("{") or s.startswith("}") or re.match(r"^L\w+:", s) or s.startswith(("if", "goto", "return")):
            return None
        if re.search(r"\b%s\b" % re.escape(u), l):
            return None
        m = re.match(r"^\s*(\w+) = ", l)
        if m and m.group(1) in reads:
            return None
        if mem and (re.match(r"^\s*\*", l) or "FUN_" in l or "->" in l):
            return None
    ln = lines.pop(iu)
    lines.insert(iv, ln)
    return "\n".join(lines)


def fix_one(corpus_path, va, outdir):
    import corpus as corpusmod, lbench, w8_score, il2c_lift
    from w8_emit import Image
    corp = corpusmod.Corpus(corpus_path)
    row = next(r for r in corp.rows if r["va"] == va)
    build = lbench.c2_name(row.get("c2") or corp.head["c2"])
    os.environ["C2_BUILD"] = build
    eimg = Image(corp.head["image"])
    v = int(va, 16)
    text, _ = il2c_lift.unit(eimg, v, row["size"], corpusmod.unpack(row["mask"]), flags=row["flags"])
    d = os.path.join(outdir, va)
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, "lifted.cpp")
    open(src, "w").write(text + "\n")
    asm = os.path.join(d, "orig.s")
    orig_asm(eimg, v, row["size"], asm)
    flags = shlex.split(row["flags"])
    func, dec = "FUN_%08x" % v, decorated(text, v)
    img = corp.image()

    def bdiff(path, tag):
        obj, _log = w8_score.compile_one((path, row["flags"] + " /Z7", tag))
        return lbench.score(obj, row, img).get("bdiff") if obj else None

    res = dict(va=va, before=bdiff(src, "rf0_" + va))
    want, scratch = regmap(src, func, dec, asm, flags, build)
    res["wants"] = {k: w for k, w in want.items()}
    res["scratch"] = sorted(scratch)
    color = {k: w for k, w in want.items() if k not in scratch}
    if not color:
        res["verdict"] = "no Color register to change" if want else "registers agree"
        return res
    snap = os.path.join(d, "snap.json")
    if not export(src, func, flags, build, snap):
        res["verdict"] = "no regs snapshot"
        return res
    sol = regorder.solve(snap, color)
    res["constraints"] = [(c["u"], c["v"], c["kind"]) for c in sol["cons"]]
    res["model_ok"] = sol["model_ok"]
    new, moved = text, []
    for c in sol["cons"]:
        if c["kind"] != "def":
            continue
        t = move_def(new, c["u"], c["v"])
        if t:
            new = t
            moved.append("%s before %s" % (c["u"], c["v"]))
    res["moves"] = moved
    if moved:
        p2 = os.path.join(d, "fixed.cpp")
        open(p2, "w").write(new + "\n")
        res["after"] = bdiff(p2, "rf1_" + va)
        res["verdict"] = "MATCH" if res["after"] == 0 else "moved"
    else:
        kinds = sorted({c["kind"] for c in sol["cons"]})
        res["verdict"] = "no definition move applies (constraints: %s)" % (", ".join(kinds) or "none")
    return res


def main(argv):
    import argparse
    from concurrent.futures import ThreadPoolExecutor
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("corpus")
    ap.add_argument("vas", nargs="*")
    ap.add_argument("--vas-file")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "work", "invert", "regfix"))
    ap.add_argument("--jobs", type=int, default=3)
    a = ap.parse_args(argv)
    vas = list(a.vas) + (open(a.vas_file).read().split() if a.vas_file else [])
    with ThreadPoolExecutor(a.jobs) as ex:
        rs = list(ex.map(lambda x: _safe(a.corpus, x, a.out), vas))
    for r in rs:
        print(json.dumps(r))
    return 0


def _safe(c, va, out):
    try:
        return fix_one(c, va, out)
    except Exception as e:
        return dict(va=va, verdict="error: %s: %s" % (type(e).__name__, str(e)[:100]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
