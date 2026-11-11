#!/usr/bin/env python3
"""Find inline-helper rewrites that fix a colouring-order problem without changing any code.

usage: helpersweep.py <tu.cpp> --func DECORATED --want 'A>B' [--pass K] [--asm-vs ORIG.s] [--jobs 8]
                      [--max-combo 3] [--verify 6] [--include-target] [-- <cl args>]

Rule behind it (docs/c2/frontend.md, probes p10/p11): in a function with a C++ EH frame, an inlined helper that
binds a reference to an address expression (`T& r = p->m; f(&r)`, or a reference parameter used as `&t`) leaves a
variable behind. That variable counts in P of its block, which moves every other value's priority there, and it
usually takes the register the address temporary had through a copy hint, so no rotation pick moves and the code
stays the same. Outside EH frames nothing happens.

Steps:
  1. find the helpers the target function uses (inline function and member bodies whose name occurs in the
     target's body, transitively);
  2. list every rewrite site in them: `&EXPR` where EXPR is a member access or a reference parameter, rewritten
     to `&hs_bind(EXPR)` (`template<class T> T& hs_bind(T& x) { T& r = x; return r; }` is prepended: no types
     needed);
  3. trace the baseline and every single rewrite (regs + scratch; cached): the gap of the constraint A>B at
     Spillcosts pass K (prio(B) - prio(A), as tsweep's prio), whether the code changed (aligned diff against the
     baseline listing) and whether the scratch picks changed;
  4. predict combinations: P enters the priority formula linearly, so the gap of a set of rewrites in different
     places is the sum of their single gaps (rasim's model); rank the code-neutral sets that close the gap,
     fewest rewrites first;
  5. compile the best --verify sets and report the real gap, code change and (with --asm-vs) regdiff.
"""
import argparse, itertools, os, re, shutil, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from image import ROOT
import c2trace, c2regs, c2scratch, tsweep

BIND = "template<class HsT> __forceinline HsT& hs_bind(HsT& x) { HsT& r = x; return r; }\n"
IDENT = re.compile(r"[A-Za-z_]\w*")


def match_brace(text, i):
    """Index just past the brace block starting at text[i] == '{' (ignores braces in strings/comments roughly)."""
    depth = 0
    k = i
    while k < len(text):
        c = text[k]
        if c == '"' or c == "'":
            q = c
            k += 1
            while k < len(text) and text[k] != q:
                k += 2 if text[k] == "\\" else 1
        elif text.startswith("//", k):
            k = text.find("\n", k)
            if k < 0:
                return len(text)
        elif text.startswith("/*", k):
            k = text.find("*/", k) + 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return len(text)


DEF = re.compile(r"(?:(\w+)\s*::\s*)?(~?\w+)\s*\(([^;{}()]*(?:\([^()]*\)[^;{}()]*)*)\)\s*(?:const\s*)?(?::[^{;]*)?\{")


def functions(text):
    """[(name, params text, body start, body end)] for every function-like definition with a body."""
    out = []
    for m in DEF.finditer(text):
        name = m.group(2)
        if name in ("if", "while", "for", "switch", "catch", "return", "sizeof"):
            continue
        b = m.end() - 1
        e = match_brace(text, b)
        out.append((name, m.group(3), b, e, m.start()))
    return out


def body_of(text, func_decorated):
    """The target's definition: match `Class::Name(` from the decorated name ?Name@Class@@..."""
    m = re.match(r"\?(\w+)@(\w+)@@", func_decorated)
    name, cls = (m.group(1), m.group(2)) if m else (func_decorated.strip("?_@"), None)
    for fn in functions(text):
        if fn[0] == name:
            head = text[max(0, fn[4] - 200):fn[2]]
            if cls is None or re.search(r"\b%s\s*::\s*%s\s*\(" % (cls, name), text[fn[4] - len(cls) - 4:fn[2]]) \
                    or cls in head:
                return fn
    sys.exit("definition of %s not found in the TU" % func_decorated)


def ref_params(params):
    """Names of reference parameters in a parameter list text."""
    out = []
    for p in params.split(","):
        m = re.search(r"&\s*(\w+)\s*$", p.strip())
        if m:
            out.append(m.group(1))
    return out


def lvalue_end(body, k):
    """Parse an lvalue expression starting at body[k]; return its end index or None."""
    n = len(body)
    while k < n and body[k] == " ":
        k += 1
    start = k
    if k < n and body[k] == "(":
        depth = 0
        while k < n:
            if body[k] == "(":
                depth += 1
            elif body[k] == ")":
                depth -= 1
                if depth == 0:
                    k += 1
                    break
            k += 1
    else:
        m = IDENT.match(body, k)
        if not m:
            return None, start
        k = m.end()
    while k < n:
        if body.startswith("->", k):
            m = IDENT.match(body, k + 2)
            if not m:
                break
            k = m.end()
        elif body[k] == "." and k + 1 < n and (body[k + 1].isalpha() or body[k + 1] == "_"):
            m = IDENT.match(body, k + 1)
            k = m.end()
        elif body[k] == "[":
            depth = 0
            while k < n:
                if body[k] == "[":
                    depth += 1
                elif body[k] == "]":
                    depth -= 1
                    if depth == 0:
                        k += 1
                        break
                k += 1
        else:
            break
    if k < n and body[k] == "(":          # a call result: not an lvalue we can bind
        return None, start
    return k, start


def sites_in(text, fn):
    """Rewrite sites in one function body: [(abs start of '&', abs end of expr, expr text)]."""
    name, params, b, e, _ = fn
    body = text[b:e]
    refs = set(ref_params(params))
    out = []
    for m in re.finditer(r"&", body):
        k = m.start()
        prev = body[:k].rstrip()[-1:] if body[:k].rstrip() else ""
        if body[k + 1:k + 2] == "&" or prev in ("&",) or (prev and (prev.isalnum() or prev in ")]_")):
            continue                     # '&&', binary '&', or a reference declarator `T& x`
        end, st = lvalue_end(body, k + 1)
        if end is None:
            continue
        expr = body[st:end].strip()
        if not expr or expr.startswith("hs_bind"):
            continue
        member = "->" in expr or re.search(r"\.\w", expr)
        if member or expr in refs:
            out.append((b + k, b + end, expr))
    return out


def used_helpers(text, target, fns):
    by_name = {}
    for fn in fns:
        by_name.setdefault(fn[0], []).append(fn)
    seen, todo, out = set(), [target], []
    while todo:
        fn = todo.pop()
        body = text[fn[2]:fn[3]]
        for ident in set(IDENT.findall(body)):
            for cand in by_name.get(ident, []):
                if cand[2] not in seen and cand is not target:
                    seen.add(cand[2])
                    out.append(cand)
                    todo.append(cand)
    return out


def apply_sites(text, sites):
    """Rewrite the chosen sites (back to front) and prepend hs_bind."""
    for s, e, expr in sorted(sites, reverse=True):
        text = text[:s] + "&hs_bind(" + expr + ")" + text[e:]
    return BIND + text


def prio_at(snap, name, k):
    for kk in range(k, 0, -1):
        p = [q for q in snap["passes"] if q["n"] == kk]
        if not p:
            continue
        vals = [sum(t["delta"] for t in g["terms"]) for g in p[0]["grs"].values()
                if g["name"] == (name[1:] if name.startswith("_") else name)]
        if vals:
            return max(vals)
    return None


def defpos(snap, name):
    d = [q["defOrd"] for q in snap["pops"] if q["name"] == name]
    return max(d) if d else 0


class Run:
    def __init__(self, src, func, cl, work):
        _, _, _, out, log = c2trace.run_trace(src, c2regs.HOOKS, cl, keep=os.path.join(work, "r"), quiet=True)
        fns = c2regs.parse(log, func)
        if not fns:
            raise RuntimeError("not register-allocated (compile error?): " + out[-400:])
        self.snap = c2regs.export(fns[0])
        _, _, _, out, log = c2trace.run_trace(src, c2scratch.HOOKS, cl, keep=os.path.join(work, "s"), quiet=True)
        picks, _ = c2scratch.parse(log, func)
        self.picks = [(p["reg"], p["rot"]) for p in picks]
        asm = os.path.join(work, "s", os.path.splitext(os.path.basename(src))[0] + ".asm")
        self.instrs = tsweep.listing_instrs(asm, func)

    def gap(self, a, b, k):
        pa, pb = prio_at(self.snap, a, k), prio_at(self.snap, b, k)
        if pa is None or pb is None:
            return None
        g = pb - pa                      # <= 0: A is coloured first (above B, or tied with the larger defOrd)
        if g == 0 and defpos(self.snap, a) <= defpos(self.snap, b):
            g = 1
        return g


def main():
    argv = sys.argv[1:]
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("tu")
    ap.add_argument("--func", required=True, help="decorated name, e.g. ?FUN_00c7e9a0@SceneOwner@@QAEXPAUArgument@@PBD@Z")
    ap.add_argument("--want", required=True, help="A>B: A must be coloured before B")
    ap.add_argument("--pass", dest="k", type=int, default=2)
    ap.add_argument("--asm-vs")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--max-combo", type=int, default=3)
    ap.add_argument("--verify", type=int, default=6)
    ap.add_argument("--include-target", action="store_true", help="also rewrite sites in the target's own body")
    a = ap.parse_args(argv)
    A, B = a.want.split(">")
    text = open(a.tu).read()
    hdr_dir = os.path.dirname(os.path.abspath(a.tu))
    text = re.sub(r'#include "([^"/][^"]*)"', lambda m: '#include "%s"' % os.path.join(hdr_dir, m.group(1)), text)
    fns = functions(text)
    target = body_of(text, a.func)
    helpers = used_helpers(text, target, fns)
    sites = []
    for fn in helpers + ([target] if a.include_target else []):
        for s in sites_in(text, fn):
            sites.append((fn[0],) + s)
    sites = sorted(set(sites), key=lambda x: x[1])
    print("%d helpers used by the target, %d rewrite sites" % (len(helpers), len(sites)), flush=True)
    out = os.path.join(ROOT, "work", "helpersweep", os.path.splitext(os.path.basename(a.tu))[0])
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)

    def build(tag, chosen):
        d = os.path.join(out, tag)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, tag + ".cpp")
        open(p, "w").write(apply_sites(text, [(s, e, x) for _, s, e, x in chosen]))
        return Run(p, a.func, cl, d)

    base = build("base", [])
    g0 = base.gap(A, B, a.k)
    print("baseline: gap %s (prio %s %s, %s %s at pass %d)" % (g0, A, prio_at(base.snap, A, a.k), B,
                                                              prio_at(base.snap, B, a.k), a.k))

    def measure(item):
        i, site = item
        try:
            r = build("s%03d" % i, [site])
            return i, r, None
        except Exception as e:
            return i, None, str(e)[-200:]

    with ThreadPoolExecutor(a.jobs) as ex:
        singles = list(ex.map(measure, list(enumerate(sites))))
    rows = []
    for i, r, err in singles:
        fn, s, e, expr = sites[i]
        if r is None:
            print("  s%03d %-22s %-40s  failed: %s" % (i, fn[:22], expr[:40], err))
            continue
        dg = (r.gap(A, B, a.k) or 0) - (g0 or 0)
        code = tsweep.regdiff(r.instrs, base.instrs)
        picks = r.picks != base.picks
        rows.append((i, dg, code, picks))
        print("  s%03d %-22s %-40s  dgap %+4d  code %s  picks %s" % (i, fn[:22], expr[:40], dg,
              "same" if code == 0 else "+%d" % code, "moved" if picks else "same"), flush=True)
    neutral = [r for r in rows if r[2] == 0 and r[1] < 0]
    print("%d of %d singles are code-neutral and reduce the gap" % (len(neutral), len(rows)))
    combos = []
    for n in range(1, a.max_combo + 1):
        for c in itertools.combinations(neutral, n):
            pred = g0 + sum(r[1] for r in c)
            if pred <= 0:
                combos.append((n, -pred, [r[0] for r in c], pred))
    combos.sort()
    print("%d combinations predicted to close the gap; verifying %d" % (len(combos), min(a.verify, len(combos))))
    orig = tsweep.orig_instrs(a.asm_vs) if a.asm_vs else None
    best = None
    for n, _, ids, pred in combos[:a.verify]:
        r = build("c_" + "_".join("%03d" % i for i in ids), [sites[i] for i in ids])
        gap = r.gap(A, B, a.k)
        code = tsweep.regdiff(r.instrs, base.instrs)
        rd = tsweep.regdiff(r.instrs, orig) if orig else None
        print("  %-24s predicted gap %+d  real %+d  code vs base %d%s" % (
            "+".join("s%03d" % i for i in ids), pred, gap, code, "" if rd is None else "  regdiff vs original %d" % rd))
        if rd is not None and (best is None or rd < best[0]):
            best = (rd, ids)
    if best:
        print("best: %s (regdiff %d) -> %s" % ("+".join("s%03d" % i for i in best[1]), best[0],
                                               os.path.join(out, "c_" + "_".join("%03d" % i for i in best[1]))))
    for i in sorted({i for c in combos[:a.verify] for i in c[2]}):
        fn, s, e, expr = sites[i]
        print("  s%03d in %s: &%s -> &hs_bind(%s)" % (i, fn, expr, expr))


if __name__ == "__main__":
    main()
