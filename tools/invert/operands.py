#!/usr/bin/env python3
"""Commutative operand order -> source operand order.  Doc: docs/c2/invert.md section 2.

Forward rule (symkeys.md sections 0 and 3, [C]): `OptLeftWeightTree` sorts the operands of OPADD / OPMUL / OPAND /
OPOR / OPXOR / OPEQ / OPNE by the weight `SU << 24 | count << 16 | hash`, larger first, with a stable merge sort.
So the operand evaluated first (loaded with fld, the left register of the two-address instruction) is

  * the heavier one, whatever order the source writes, when the weights differ;
  * the one the source writes first (left) when the weights are equal (`sites` prints `tie: source order`;
    probe: `f1() * f2()` and `f2() * f1()` both keep the source order).

Inverse, per site where the original evaluates X before Y (from the original's code; for a lifted unit the lifter
writes X on the left):

  free     weight(X) > weight(Y) in our compile: any source order gives X first.
  write    weight(X) = weight(Y): X must be written first (the only case where the source's operand order matters).
  keys     weight(X) < weight(Y) and only the hash differs, both hashes following symbol keys: no operand order can
           give it; the keys must move (X referenced later than Y in the IL, or a parameter declared earlier: keys go
           to parameters in reverse declaration order, then to locals at first reference), see `symkeys.py levers`.
  shape    SU or node count decides against X, or a hash that follows no key (constant, unnamed subtree, a
           temporary hashed by its definition): no operand order and no key edit can give it; the expression's shape
           has to change (sulever.py; on 13.10 usually different code).

usage: operands.py classify SRC --func F [-- <cl args>]     every pass-2 site of our compile: free / write / keys / shape
                                                            taking the source's left operand as the wanted first
       operands.py measure [--limit N] [--jobs J]           on Spore's byte-exact /O2 functions: how often the source's
                                                            order matters (ties), and that the tie rule holds
"""
import os, re, subprocess, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = re.compile(r"^pass (\d) line\s+(-?\d+) (OP\w+)\s+1st (.*?)\s+SU(\d+) n(\d+) h([0-9a-f]{4})\s*(.*)$")
SITE2 = re.compile(r"^\s+2nd (.*?)\s+SU(\d+) n(\d+) h([0-9a-f]{4})\s*(.*)$")
NAMED = re.compile(r"(?<![\w$])_(\w+?)(?: \(key|\s+key|\+|\b)")


def sites(src, func, cl, build=None):
    """Pass-2 two-operand sorts of `func` from `symkeys.py sites`: [{line, op, first, second}] where first / second
    are (text, SU, count, hash) and first is the operand evaluated first."""
    env = dict(os.environ)
    if build:
        env["C2_BUILD"] = build
    r = subprocess.run([sys.executable, os.path.join(HERE, "symkeys.py"), "sites", src, "--func", func, "--pass", "2",
                        "--"] + list(cl), capture_output=True, text=True, env=env)
    out, cur = [], None
    for ln in r.stdout.splitlines():
        m = SITE.match(ln)
        if m:
            cur = dict(line=int(m.group(2)), op=m.group(3),
                       first=(m.group(4).strip(), int(m.group(5)), int(m.group(6)), int(m.group(7), 16)))
            continue
        m = SITE2.match(ln)
        if m and cur is not None:
            cur["second"] = (m.group(1).strip(), int(m.group(2)), int(m.group(3)), int(m.group(4), 16))
            cur["tie"] = "tie" in m.group(5)
            out.append(cur)
            cur = None
        elif cur is not None and not ln.startswith(" "):
            cur = None                       # a sort of three or more operands: not a two-operand site
    return out


def keyed(text):
    """Does this operand's hash follow a symbol key (a named variable, a field view, a member of a named base)?"""
    return bool(re.search(r"\bkey \d+", text)) and not text.startswith("iconst")


def classify(want, other):
    """want / other: (text, SU, count, hash) of the operand the original evaluates first and the other one, as our
    compile weighs them.  -> free | write | keys | shape"""
    wa = (want[1], want[2], want[3])
    wb = (other[1], other[2], other[3])
    if wa > wb:
        return "free"
    if wa == wb:
        return "write"
    if want[1] != other[1] or want[2] != other[2]:
        return "shape"
    return "keys" if keyed(want[0]) and keyed(other[0]) else "shape"


def names(text):
    return [m.group(1) for m in re.finditer(r"(?<![\w$.])_([A-Za-z]\w*)", text)]


def source_positions(src_lines, def_line, site, first, second):
    """Column of each operand's named leaf in the source line the site comes from (searching the few lines after the
    function's first line + offset), or None."""
    na, nb = names(first[0]), names(second[0])
    if not na or not nb or na[0] == nb[0]:
        return None
    for k in range(max(0, def_line + site["line"] - 2), min(len(src_lines), def_line + site["line"] + 3)):
        t = src_lines[k]
        ma = re.search(r"\b%s\b" % re.escape(na[0]), t)
        mb = re.search(r"\b%s\b" % re.escape(nb[0]), t)
        if ma and mb:
            return ma.start(), mb.start()
    return None


def def_line_of(src_lines, sym):
    """0-based line of the function's definition (its undecorated name followed by '(' and then a '{')."""
    m = re.match(r"\?(\w+)@((\w+)@)?", sym) or re.match(r"_?(\w+)", sym)
    if not m:
        return None                                  # an operator / constructor: not located
    name = m.group(1)
    cls = m.group(3) if m.lastindex and m.lastindex >= 3 else None
    pat = re.compile((r"\b%s::" % re.escape(cls) if cls else r"(?<![\w:])") + r"%s\s*\(" % re.escape(name))
    for i, t in enumerate(src_lines):
        if pat.search(t) and not t.rstrip().endswith(";"):
            return i
    return None


def _measure_one(row):
    import shlex
    src, sym, va, flags = row
    cl = shlex.split(flags) + ["/I" + os.path.join(SPORE, "match", "include"), "/I" + os.path.dirname(src)]
    try:
        ss = sites(src, sym, cl, build="15.00")
    except Exception as e:
        return row, None, str(e)
    lines = open(src, errors="replace").read().split("\n")
    dl = def_line_of(lines, sym)
    res = []
    for s in ss:
        c = "tie" if s["tie"] else "decided"
        pos = source_positions(lines, dl, s, s["first"], s["second"]) if dl is not None else None
        res.append((s["op"], c, pos))
    return row, res, None


SPORE = os.path.join(HERE, "..", "..", "spore-decomp")


def spore_o2_rows():
    import glob, shlex
    out = []
    for m in sorted(glob.glob(os.path.join(SPORE, "match", "slices", "*", "manifest.txt"))):
        for ln in open(m):
            if ln.startswith("#") or not ln.strip():
                continue
            p = ln.split(None, 3)
            fl = p[3].strip() if len(p) > 3 else "/O2 /MD /Gy /EHsc /TP"
            if "/Od" in shlex.split(fl) or "/GL" in shlex.split(fl):
                continue
            out.append((os.path.join(SPORE, "match", p[0]), p[1], p[2], fl))
    return out


def measure(limit=80, jobs=6):
    import collections, random
    from concurrent.futures import ThreadPoolExecutor
    rows = spore_o2_rows()
    rows = sorted(random.Random(7).sample(rows, min(limit, len(rows))), key=lambda r: r[2])
    with ThreadPoolExecutor(jobs) as ex:
        got = list(ex.map(_measure_one, rows))
    cnt = collections.Counter()
    tie_ok = tie_n = dec_left = dec_n = 0
    for row, res, err in got:
        if res is None:
            cnt["error"] += 1
            continue
        cnt["functions"] += 1
        for op, c, pos in res:
            cnt[c] += 1
            if pos is None:
                continue
            if c == "tie":
                tie_n += 1
                tie_ok += pos[0] < pos[1]
            else:
                dec_n += 1
                dec_left += pos[0] < pos[1]
    print("Spore byte-exact /O2 functions sampled: %d (errors %d); pass-2 two-operand sites: %d tie, %d decided by weight"
          % (cnt["functions"], cnt["error"], cnt["tie"], cnt["decided"]))
    print("tie sites with both leaves located in the source: %d; the source writes the first-evaluated operand first in"
          " %d (%.1f%%)" % (tie_n, tie_ok, 100.0 * tie_ok / max(1, tie_n)))
    print("weight-decided sites located: %d; the source happens to write the first-evaluated operand first in %d"
          " (%.1f%%: source order is free there)" % (dec_n, dec_left, 100.0 * dec_left / max(1, dec_n)))
    return got


def main(argv):
    if not argv or argv[0] not in ("classify", "measure"):
        print(__doc__)
        return 1
    import argparse
    if argv[0] == "measure":
        ap = argparse.ArgumentParser()
        ap.add_argument("--limit", type=int, default=80)
        ap.add_argument("--jobs", type=int, default=6)
        a = ap.parse_args(argv[1:])
        measure(a.limit, a.jobs)
        return 0
    rest = argv[1:]
    cl = []
    if "--" in rest:
        i = rest.index("--")
        rest, cl = rest[:i], rest[i + 1:]
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    a = ap.parse_args(rest)
    lines = open(a.src, errors="replace").read().split("\n")
    for s in sites(a.src, a.func, cl):
        sym = a.func
        dl = def_line_of(lines, sym) if sym.startswith("?") else None
        pos = source_positions(lines, dl, s, s["first"], s["second"]) if dl is not None else None
        if pos is None or pos[0] < pos[1]:
            want, other, how = s["first"], s["second"], "kept"
        else:
            want, other, how = s["second"], s["first"], "FLIPPED"
        print("line %3d %-6s %-8s source-left %-28s other %-28s -> %s" % (
            s["line"], s["op"], how, want[0][:28], other[0][:28], classify(want, other)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
