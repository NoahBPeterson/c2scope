#!/usr/bin/env python3
"""Scheduling -> statement order constraints.  Doc: docs/c2/invert.md section 6.

Forward rule (sched.md, [C] on 13,798 instructions): per region (cut at labels, branches, 81 tuples; calls are
barriers) the list scheduler issues the ready instruction with the highest priority 1024*(height + successors) +
32768*[reads memory] + 2^23*[sets the final jcc's flags], ties to the earlier instruction (lex = the order after
register allocation, which is the order the source's statements and expressions put the tuples in).

Inverse, per region where the original's issue order differs from ours (schedwant.py finds the original's order and
replays the model): at the first difference, the original issues W where we issue X:
  tie      prio(W) = prio(X) and both ready: lex decides, so W's statement / operand must come first in the source
           (a statement-order constraint; for two operands of one commutative node it is operands.py's question)
  dag      prio(W) != prio(X) and a DAG change explains it (schedwant's unalias / alias / cut / ns edits: a source
           construct, not an order)
  other    neither: the pre-schedule order differs (an operand sort, the front end's evaluation order) or the
           cycle model's unit / latency choice decides (prio(W) > prio(X) but W was not ready)

usage: schedorder.py CORPUS VA.. [--jobs J]     classify every differing region of the lifted units
       schedorder.py fix CORPUS VA..              for the `tie` regions: the statement move, one compile, the score
"""
import json, os, re, shlex, subprocess, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
NODE = re.compile(r"^\s+(\d+) ht\s+(\d+) ns (\d+) (\S+)\s+([0-9a-f]+)\s+(.*)$")
FIRST = re.compile(r"first difference at position \d+: the scheduler issues (\d+):.*\(prio ([0-9a-f]+)\), the original "
                   r"(\d+):.*\(prio ([0-9a-f]+)\)")


def classify_text(out):
    """[(class, detail)] per differing region of one schedwant report."""
    res, nodes, works = [], {}, False
    for ln in out.splitlines():
        if ln.startswith("-- region"):
            nodes, works = {}, False
        m = NODE.match(ln)
        if m:
            nodes[int(m.group(1))] = int(m.group(5), 16)
        if re.search(r"\b[1-9]\d* of \d+ perturbations give the wanted order", ln):
            works = True
        m = FIRST.search(ln)
        if m:
            x, px, w, pw = int(m.group(1)), int(m.group(2), 16), int(m.group(3)), int(m.group(4), 16)
            res.append(["tie" if px == pw else "pending", "issued %d (prio %x), original %d (prio %x)" % (x, px, w, pw)])
        if re.search(r"perturbations give the wanted order", ln) and res and res[-1][0] == "pending":
            res[-1][0] = "dag" if works else "other"
    return [tuple(r) for r in res]


def one(args):
    corpus_path, va, outdir = args
    import corpus as corpusmod, lbench, il2c_lift, regfix
    from w8_emit import Image
    corp = corpusmod.Corpus(corpus_path)
    row = next(r for r in corp.rows if r["va"] == va)
    build = lbench.c2_name(row.get("c2") or corp.head["c2"])
    eimg = Image(corp.head["image"])
    v = int(va, 16)
    try:
        text, _ = il2c_lift.unit(eimg, v, row["size"], corpusmod.unpack(row["mask"]), flags=row["flags"])
    except Exception as e:
        return va, [("error", str(e)[:80])]
    d = os.path.join(outdir, va)
    os.makedirs(d, exist_ok=True)
    src, asm = os.path.join(d, "lifted.cpp"), os.path.join(d, "orig.s")
    open(src, "w").write(text + "\n")
    regfix.orig_asm(eimg, v, row["size"], asm)
    r = subprocess.run([sys.executable, os.path.join(HERE, "schedwant.py"), src, "--func", "FUN_%08x" % v,
                        "--asm-vs", asm, "--asm-func", regfix.decorated(text, v), "--"] + shlex.split(row["flags"]),
                       capture_output=True, text=True, env=dict(os.environ, C2_BUILD=build))
    open(os.path.join(d, "schedwant.txt"), "w").write(r.stdout + r.stderr)
    return va, classify_text(r.stdout)


def _defvar(il):
    """The source variable a scheduled node defines (`mov esi=_t1003:1004 <- ..` -> t1003), or None."""
    m = re.match(r"^\S+\s+\w+=_(\w+?):\w+\s*(?:cc\S*\s*)?<-", il)
    return m.group(1) if m else None


def _stmt_line(lines, il):
    """Index of the source statement a node comes from: the definition of its variable, or a store through
    `[base=_VAR+OFF]` written `*(T*)((char*)(..VAR..) + OFF) = ..` (OFF 0: no `+`)."""
    v = _defvar(il)
    if v:
        k = [i for i, l in enumerate(lines) if re.match(r"^\s+%s = " % re.escape(v), l)]
        return k[0] if len(k) == 1 else None
    m = re.match(r"^\S+\s+\[\w+=_(\w+?):\w+(?:\+(\d+))?\]", il)
    if not m:
        return None
    var, off = m.group(1), int(m.group(2) or 0)
    al = {var} | {a.group(1) for l in lines for a in [re.match(r"^\s+(t\d+) = (?:\(int\))?\(*(?:\(int\))?\(*%s\)*;$"
                                                              % re.escape(var), l)] if a}
    vs = "(?:%s)" % "|".join(re.escape(x) for x in sorted(al))
    pat = (r"^\s+\*\([^=]*\b%s\b[^=]*\+ %d\)\s*=\s" % (vs, off)) if off else \
        (r"^\s+\*\([\w ]+\*\)\(char\*\)\(*(?:\(int\))?\(*%s\)*\s*=\s" % vs)
    k = [i for i, l in enumerate(lines) if re.match(pat, l)]
    return k[0] if len(k) == 1 else None


def move_line(text, iw, ix):
    """Statement iw moved to just before statement ix (ix < iw): same indentation, only plain statements in between,
    none of them writing what iw reads or reading / writing what iw writes, no store or call in between when iw
    touches memory."""
    lines = text.split("\n")
    if ix >= iw:
        return None
    ind = lambda l: len(l) - len(l.lstrip())
    w = lines[iw]
    if ind(w) != ind(lines[ix]):
        return None
    lhs, rhs = w.split("=", 1) if "=" in w else ("", w)
    names = set(re.findall(r"\b([at]\d+|v_l\d+|self)\b", w))
    mem = "*(" in w or "->" in w or "FUN_" in w
    for j, l in enumerate(lines[ix:iw]):
        t = l.strip()
        if j == 0 and t.startswith("*") and "FUN_" not in t and "->" not in t:
            continue                # X itself: the scheduler found no dependence between W and X (both ready)
        if t.endswith("{") or t.startswith("}") or re.match(r"^L\w+:", t) or t.startswith(("if", "goto", "return")):
            return None
        m = re.match(r"^\s*(\w+) = ", l)
        if m and m.group(1) in names:
            return None
        lw = re.match(r"^\s*(\w+) = ", w)
        if lw and re.search(r"\b%s\b" % lw.group(1), l):
            return None
        if mem and (t.startswith("*") or "FUN_" in t or "->" in t):
            return None
    lines.insert(ix, lines.pop(iw))
    return "\n".join(lines)


def fix(corpus_path, va, outdir):
    """For a `tie` region: move the statement defining the node the original issues first to just before the one
    defining the node we issue first (regfix.move_def: same block, independent), compile once, score."""
    import corpus as corpusmod, lbench, w8_score, regfix
    d = os.path.join(outdir, va)
    txt = open(os.path.join(d, "schedwant.txt")).read()
    ils, out = {}, []
    for ln in txt.splitlines():
        if ln.startswith("-- region"):
            ils = {}
        m = NODE.match(ln)
        if m:
            ils[int(m.group(1))] = m.group(6)
        m = FIRST.search(ln)
        if m and m.group(2) == m.group(4):
            out.append((ils.get(int(m.group(3)), ""), ils.get(int(m.group(1)), "")))
    src = open(os.path.join(d, "lifted.cpp")).read()
    new, moved = src, []
    for w, x in out:
        lines = new.split("\n")
        iw, ix = _stmt_line(lines, w), _stmt_line(lines, x)
        if iw is not None and ix is not None:
            t = move_line(new, iw, ix)
            if t:
                new, moved = t, moved + ["line %d before line %d" % (iw + 1, ix + 1)]
    out = [(w[:40], x[:40]) for w, x in out]
    if not moved:
        return dict(va=va, ties=out, verdict="no statement move applies")
    corp = corpusmod.Corpus(corpus_path)
    row = next(r for r in corp.rows if r["va"] == va)
    os.environ["C2_BUILD"] = lbench.c2_name(row.get("c2") or corp.head["c2"])
    img = corp.image()
    res = dict(va=va, ties=out, moves=moved)
    for name, text in (("before", src), ("after", new)):
        p = os.path.join(d, name + ".cpp")
        open(p, "w").write(text)
        obj, _ = w8_score.compile_one((p, row["flags"] + " /Z7", "so_%s_%s" % (name, va)))
        res[name] = lbench.score(obj, row, img).get("bdiff") if obj else None
    res["verdict"] = "MATCH" if res["after"] == 0 else "kept (closer)" if (res["after"] is not None and res["before"]
                     is not None and res["after"] < res["before"]) else "rejected (not closer)"
    return res


def main(argv):
    if argv and argv[0] == "fix":
        import json as _j
        for va in argv[2:]:
            print(_j.dumps(fix(argv[1], va, os.path.join(HERE, "..", "work", "invert", "sched"))))
        return 0
    import argparse, collections
    from concurrent.futures import ThreadPoolExecutor
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("corpus")
    ap.add_argument("vas", nargs="*")
    ap.add_argument("--vas-file")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "work", "invert", "sched"))
    a = ap.parse_args(argv)
    vas = list(a.vas) + (open(a.vas_file).read().split() if a.vas_file else [])
    os.environ.setdefault("C2_BUILD", "15.00")
    with ThreadPoolExecutor(a.jobs) as ex:
        got = list(ex.map(one, [(a.corpus, v, a.out) for v in vas]))
    cnt = collections.Counter()
    for va, rs in got:
        for c, det in rs:
            cnt[c] += 1
        print(va, " ".join("%s(%s)" % (c, det) for c, det in rs) or "no differing region found")
    print("differing regions: " + ", ".join("%s %d" % kv for kv in cnt.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
