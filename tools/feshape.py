#!/usr/bin/env python3
"""Front-end IL shapes: what the front end (c1/c1xx) handed c2 for one function, as a multiset of tuple shapes.
Doc: docs/c2/effectmine.md section 7 (model F).

usage: feshape.py <src> --func SUBSTR [--stage reader|inlined] [-- <cl args>]   the function's shapes, counted

A tuple's shape is its opcode and the kinds of its operands, without names or numbers, so it means the same thing
in any function: `OPASSIGN(V:1004<-T:1004)` (a variable written from a temporary, 4-byte int), `OPPAREN(T:5004<-T:5004)`
(a float parenthesis), `OPBRANCH(<-T:9000)`. Kinds: T temporary, V named variable (local, parameter, global), K an
immediate candidate, C a literal, M a memory operand, F a function (call target), and implicit operands by their
name (cc, esp, regset). Physical registers (`eax=t3`) are dropped to the value. PAS operands are left out.

Two stages, one traced compile: `reader` (the IL at MscRVO's entry, on 15.00 and 13.10 at Inline's: what the front
end wrote, before same-unit callees are expanded) and `inlined` (at
FgSimpleFoldCmps' entry: after Inline expanded same-unit callees). effectmine records the change of these multisets
per mutation as model F (`fe:SHAPE:+n` for the reader stage, `fi:SHAPE:+n` after inlining), so the database is a
measured map from source mutations to front-end IL; icount's `structure` verdict looks its tuple's shape up there.
"""
import argparse, collections, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace

STAGES = (("reader", ("MscRVO", "Inline")), ("inlined", ("FgSimpleFoldCmps",)))   # 15.00 / 13.10: no MscRVO hook
IMPLICIT = re.compile(r"^(cc|fcc|esp|ebp|regset|fcw|cc_\w+|st|mem)\b")
TYPED = re.compile(r"^(.*):([0-9a-f]+)$")


def kind(tok, notes=None):
    """One operand token of the dump (`edi=s386:1004`, `t64:5004`, `[_p+t3+8]:1004`, `400945028:2004`) -> kind:type.
    notes: {sN: note} of the line (`s0=?f@@YAXXZ` a call target, `s0=Ld1d258` a label)."""
    tok = tok.split("=", 1)[1] if re.match(r"^(e?[a-d]x|e?[sd]i|e?bp|[a-d][lh]|GF\d|ST\(\d\)|xmm\d\w?|mm\d)=", tok) else tok
    if tok == "->":
        return None
    m = TYPED.match(tok)
    body, ty = (m.group(1), m.group(2)) if m else (tok, "")
    if re.match(r"^pas\d+$", body):
        return None
    note = (notes or {}).get(body) if re.match(r"^s\d+$", body) else None
    if note is not None:
        k = "L" if re.match(r"^L[0-9a-f]+$", note) else "F" if note.startswith(("?", "@", "_")) or "@@" in note else "S"
        return k
    if body.startswith("["):
        k = "M"
    elif re.match(r"^t\d+$", body):
        k = "T"
    elif re.match(r"^k\d+$", body):
        k = "K"
    elif body == "fconst" or re.match(r"^-?\d[\d.]*(e[-+]?\d+)?$", body):
        k = "C"
    elif IMPLICIT.match(body):
        k = body.split(":")[0]
    elif body.startswith("?") or body.startswith("@") or "@@" in body:
        k = "F"
    else:
        k = "V"
    return k + (":" + ty if ty else "")


def shape(line):
    """The shape of one dump line: `I <tup> <op> <line> <len> | dsts <- srcs ; notes`, an icount `born_as` text
    `<op> | dsts <- srcs`, or a `P <tup> <op> <word>` pragma line. None for block / label lines. A branch keeps its
    condition (`OPBRANCH[l](<-L,T:9000)`): which way the front end wrote a compare is part of the structure."""
    s = line.strip()
    if s.startswith(("B ", "L ", "C ", "A ")) or not s:
        return None
    if s.startswith("P "):
        f = s.split()
        return f[2] if len(f) > 2 else None
    if s.startswith("I "):
        f = s.split(None, 5)
        op, rest = f[2], (f[5] if len(f) > 5 else "")
        rest = rest.split("|", 1)[1] if "|" in rest else ""
    else:
        op, _, rest = s.partition("|")
        op = op.strip()
    rest, _, nt = rest.partition(" ; ")
    notes = dict(re.findall(r"(?:^| )([sd]\d+)=(\S+)", nt))
    cc = re.search(r"(?:^| )cc=(\w+)", nt)
    d, _, sr = rest.partition("<-")
    ks = lambda t: [k for k in (kind(x, notes) for x in t.split()) if k]
    return "%s%s(%s<-%s)" % (op, "[%s]" % cc.group(1) if cc else "", ",".join(ks(d)), ",".join(ks(sr)))


def shapes(lines):
    return collections.Counter(s for s in (shape(l) for l in lines) if s)


def collect(path, func, cl, keep=None):
    """{stage: Counter of shapes} of the function from one traced compile (the IL dump at each stage's pass)."""
    import c2il, icount
    from image import Image
    img = Image("c2")
    got = dict((n, va) for n, va, _ in icount.resolve(img, [p for _, ps in STAGES for p in ps]))
    texts, names = [], []
    for st, ps in STAGES:
        p = next((p for p in ps if p in got), None)
        if p is not None and got[p] not in [va for _s, va in names]:
            texts.append("%x%s" % (got[p], c2il.SPEC))
            names.append((st, got[p]))
    if not texts:
        raise RuntimeError("no front-end passes on this build")
    _img, hk, rc, out, log = c2trace.run_trace(path, texts, cl, keep=keep, quiet=True, func=func)
    base = lambda n: re.match(r"[@_?]*(\w+)", n).group(1)
    by_name = {base(hk[k]["name"]): st for k, (st, _va) in enumerate(names)}
    out_d = {}
    for pname, lines, _fname in c2il.parse(log, hk, func):
        st = by_name.get(base(pname))
        if st and st not in out_d:             # the first event: the function's own entry to that pass
            out_d[st] = shapes(lines)
    if not out_d:
        raise RuntimeError("function %r not seen at the front-end passes (rc %s)" % (func, rc))
    return out_d


def delta(a, b, prefix):
    """Effect strings for Counter a -> Counter b: `<prefix>:SHAPE:+n` / `:-n`."""
    out = []
    for s in sorted(set(a) | set(b)):
        d = b.get(s, 0) - a.get(s, 0)
        if d:
            out.append("%s:%s:%+d" % (prefix, s, d))
    return out


def effects(base, var):
    """Model F effect strings between two collect() results (reader stage `fe:`, after inlining `fi:`; an `fi:`
    string is left out when the same change is already in the reader stage)."""
    fe = delta(base.get("reader", {}), var.get("reader", {}), "fe")
    fi = delta(base.get("inlined", {}), var.get("inlined", {}), "fi")
    seen = {s[3:] for s in fe}
    return fe + [s for s in fi if s[3:] not in seen]


def opcode(shp):
    """`OPBRANCH[e](<-L,T:8000)` -> `OPBRANCH[e]`."""
    return shp.split("(", 1)[0]


def hit(eff, effect):
    """The model F string of `eff` (a record's F list) that realises the wanted `effect` (`fe:SHAPE:-1`), or None.
    The exact shape must move by at least n in the wanted direction AND the opcode's net count at that stage must
    move the same way by at least n: wrapping `this` in a helper turns `OPADD(T<-V,C)` into `OPADD(T<-T,C)`, which
    is -1 of the wanted shape but no tuple fewer (a rename, measured on 20 of 24 Mount & Blade / Spore rows).
    For `fi:` the net also counts the `fe:` strings, since effects() leaves an `fi:` change out when the reader stage
    already has it."""
    st, _, rest = effect.partition(":")
    shp, _, n = rest.rpartition(":")
    n = int(n)
    got, net = None, 0
    for s in eff:
        a, _, b = s.partition(":")
        sh, _, c = b.rpartition(":")
        if a != st and not (st == "fi" and a == "fe"):
            continue
        if opcode(sh) == opcode(shp):
            net += int(c)
        if a == st and sh == shp and (int(c) > 0) == (n > 0) and abs(int(c)) >= abs(n):
            got = s
    return got if got and (net > 0) == (n > 0) and abs(net) >= abs(n) else None


_DB = {}


def lookup(effect, db=None, top=5):
    """Mutation classes of the effectmine database (model F) whose variants produced `effect` (`fe:SHAPE:-1`: that
    shape removed; the sign matters, the count is a minimum): [(class, hits, measured variants of the class)], most
    hits first. [] without a database."""
    import json
    if db is None:
        import effectmine
        db = effectmine.DB
    if db not in _DB:
        recs = []
        if os.path.exists(db):
            for ln in open(db, errors="replace"):
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if r.get("ok"):
                    recs.append((r.get("cls"), r.get("eff", {}).get("F", []), "frontend" in (r.get("traced") or [])))
        _DB[db] = recs
    hits, seen = collections.Counter(), collections.Counter()
    for cls, eff, traced in _DB[db]:
        if not traced:
            continue
        seen[cls] += 1
        if hit(eff, effect):
            hits[cls] += 1
    return [(c, h, seen[c]) for c, h in hits.most_common(top)]


def main(argv):
    ap = argparse.ArgumentParser(prog="feshape.py", usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    ap.add_argument("--stage", default="reader", choices=[s for s, _ in STAGES])
    argv, cl = c2trace.split_cl(argv)
    a = ap.parse_args(argv)
    d = collect(a.src, a.func, cl)
    for s, n in sorted(d.get(a.stage, {}).items(), key=lambda kv: (-kv[1], kv[0])):
        print("%4d  %s" % (n, s))
    return 0


if __name__ == "__main__":
    sys.exit(c2trace.pipe_safe(main, sys.argv[1:]))
