#!/usr/bin/env python3
"""Register roles: which source-level life-range lever gives a variable the original's register.  Doc: docs/c2/regroles.md

usage: regroles.py SRC --func F --asm-vs ORIG.s [--asm-func DECORATED] [--max-compiles N] [--jobs N] [--rounds N]
                   [--span K] [--no-ptrfield] [--no-diag] [--keep DIR] [-- <cl args>]

The `regmap` -> `rasim want` -> `realize` pipeline stops on a role difference of this kind: regmap says `mixed` (the
original's first instructions align badly), rasim's only answer is `canreg:V:eax` / `interf:V:W`, and realize has no
construct for those. They are life-range edits: V lives across a clobber (`mul` / `div` / a call: eax, edx, ecx), or
across the definition of the value that would have shared its register, and the colouring order does the rest
(regalloc.md 5, docs/c2/regroles.md). The source-level levers:

  move       one simple statement moved up or down past independent ones (up to --span statements; the swap family
             of rwsweep is the distance-1 case): ends V's life before the clobber, or lets it span another value's
             definition. A declaration with an initialiser moves too.
  ptrfield   an explicit induction pointer to the field a loop walks (`M* rg_p = &V->m;` before a chosen statement,
             the loop's `V->x` read as `((T*)((char*)rg_p - offsetof(T, m)))->x`, `++V` as `rg_p` advanced): the
             allocator then sees the pointer's definition where the source puts it, not after the trip count where
             it invents its own induction temporary (Spore 0x009e0b90: first dies before the `mul`).

Every candidate is compiled with the traced c2 and scored against ORIG.s (tsweep's bdiff, then regdiff); the best one
becomes the base of the next round. The report first prints, per variable whose register disagrees with the
original's plurality, what `rasim need reg:V=R` says closes it (the diagnosis), then the ranked candidates, and writes
the winner to DIR/best.cpp. A MATCH (bdiff 0) stops the search.
"""
import argparse, concurrent.futures as cf, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import c2trace, helpersweep, rwsweep, realize, tsweep  # noqa: E402

RASIM = os.path.join(ROOT, "tools", "rasim", "target", "release", "rasim")
ROW = re.compile(r"^(\S+)\s+(\S+)\s+(\w+):\d+[^\n]*?\s{2,}((?:\w+:\d+\s*)+)\s{2,}(ok|WANTS \w+|mixed)")


# ---------------------------------------------------------------- the diagnosis

def regmap_rows(src, func, asm_vs, asm_func, cl):
    """[(IL name, regs name, our register, plurality register or None, verdict, votes)] from regmap --all."""
    r = subprocess.run([sys.executable, os.path.join(HERE, "c2trace.py"), "regmap", src, "--func", func, "--asm-vs", asm_vs,
                        "--asm-func", asm_func or func, "--all", "--"] + cl, capture_output=True, text=True, cwd=ROOT)
    rows = []
    for ln in r.stdout.splitlines():
        m = ROW.match(ln)
        if not m:
            continue
        votes = {}
        for k, v in re.findall(r"(\w+):(\d+)", m.group(4)):
            votes[k] = int(v)
        top = max(votes.items(), key=lambda kv: kv[1])
        tot = sum(votes.values())
        rows.append((m.group(1), m.group(2), m.group(3), top[0] if top[1] * 2 > tot else None, m.group(5), votes))
    return rows


def diagnose(c, rows, cl):
    """Print, per disagreeing variable, the single edits rasim says give the original's register."""
    snap = os.path.join(c.out, "base.json")
    subprocess.run([sys.executable, os.path.join(HERE, "c2trace.py"), "regs", c.base_path, "--func", c.func,
                    "--export", snap, "--"] + cl, capture_output=True, text=True, cwd=ROOT)
    bad = [r for r in rows if r[4] != "ok" and r[3] and r[3] != r[2] and not re.match(r"t\d+$", r[0])]
    wants = []
    for il, nm, ours, want, verdict, votes in bad:
        print("# %s: ours %s, original's votes %s (%s)" % (nm, ours, " ".join("%s:%d" % kv for kv in votes.items()), verdict))
        if not os.path.exists(snap):
            continue
        q = subprocess.run([RASIM, "need", snap, "reg:%s=%s" % (nm, want), "--top", "6"], capture_output=True, text=True).stdout
        for ln in q.splitlines():
            if "give every target" in ln or ln.startswith("  ") and re.match(r"\s+\S+:\S+", ln):
                print("#   " + ln.strip()[:150])
        wants.append((nm, want))
    return wants


# ---------------------------------------------------------------- candidates

def _simple(B):
    """[(first token, one past the `;`, block)] of the simple statements, in order."""
    out = []
    for k in range(len(B.starts)):
        s = rwsweep._stmt(B, k)
        if s and B.block[s[0]] == B.block[B.starts[k]]:
            out.append((s[0], s[1], B.block[s[0]]))
    return out


def gen_move(B, span):
    """Every statement moved past up to `span` neighbouring simple statements of its block (both directions)."""
    sts = _simple(B)
    facts = [rwsweep._facts(B, a, b) for a, b, _ in sts]
    for i, (a, b, blk) in enumerate(sts):
        for d in [x for k in range(1, span + 1) for x in (-k, k)]:
            j = i + d
            if not 0 <= j < len(sts) or sts[j][2] != blk:
                continue
            lo, hi = (j, i - 1) if d < 0 else (i + 1, j)
            run = range(lo, hi + 1)
            # the crossed statements must be consecutive in the token stream (nothing structural between them)
            if any(sts[q][1] != sts[q + 1][0] for q in range(min(lo, i), max(hi, i))):
                continue
            ia, wa, ma, mwa = facts[i]
            ok = True
            for q in run:
                ib, wb, mb, mwb = facts[q]
                if wa & ib or wb & ia or (ma and mb and (mwa or mwb)):
                    ok = False
                    break
            if not ok:
                continue
            src = B.src(a, b)
            at = B.toks[sts[j][0]][1] if d < 0 else B.toks[sts[j][1] - 1][2]
            line = B.text.count("\n", B.b, B.toks[a][1])
            yield ("move", "body line %d: `%s` %s %d" % (line, src[:34], "up" if d < 0 else "down", abs(d)),
                   [(B.toks[a][1], B.toks[b - 1][2], ""), (at, at, " " + src + " " if d > 0 else src + " ")])


def orig_offsets(asm_vs):
    """Small displacements the original materialises in a register (`lea r, [r2 + N]`, `add r, N`): the offsets an
    induction pointer may start at."""
    out = []
    for ln in open(asm_vs):
        m = re.search(r"\blea \w+, \[\w+ \+ (0x[0-9a-f]+)\]|\badd e\w\w, (0x[0-9a-f]+)$", ln.strip())
        if m:
            v = int(m.group(1) or m.group(2), 16)
            if 4 <= v < 0x400 and v not in out:
                out.append(v)
    return out


def _step(B, i, V):
    """If token i (a use of V) starts a statement stepping V (`V++`, `++V`, `V += N`, `V = V + N`,
    `V = (T*)((char*)V + N)`): (first token, last token, None for one element | (byte-or-element expression,
    in elements?)). Else None."""
    t = B.toks
    n = len(t)
    edge = (";", "{", "}")
    if t[i - 1][0] == "++" and t[i + 1][0] == ";" and (i < 2 or t[i - 2][0] in edge):
        return i - 1, i, None
    if i + 2 < n and t[i + 1][0] == "++" and t[i + 2][0] == ";" and t[i - 1][0] in edge:
        return i, i + 1, None
    if i + 1 < n and t[i + 1][0] in ("+=", "-=") and t[i - 1][0] in edge:
        j = i + 2
        while j < n and t[j][0] != ";":
            j += 1
        return i, j - 1, ("%s(%s)" % ("" if t[i + 1][0] == "+=" else "-", B.src(i + 2, j)), True)
    if i + 1 < n and t[i + 1][0] == "=" and t[i - 1][0] in edge:
        j = i + 2
        while j < n and t[j][0] != ";":
            j += 1
        rhs = B.src(i + 2, j)
        cast = r"\(\s*[\w: ]+\*\s*\)\s*"
        m = re.match(r"^%s\(\s*%s%s\s*\+\s*([^;]+?)\s*\)$" % (cast, cast, re.escape(V)), rhs)
        if m:
            return i, j - 1, ("(%s)" % m.group(1), False)
        m = re.match(r"^%s\s*\+\s*([^;]+)$" % re.escape(V), rhs)
        if m:
            return i, j - 1, ("(%s)" % m.group(1), True)
    return None


def gen_ptrfield(B, offs):
    """`char* rg_p = (char*)V + OFF;` before a statement, for a pointer V stepped in a loop body: the loop's uses of V
    read `((T*)(rg_p - OFF))` and the step advances rg_p (see the module doc). OFF is one of `offs`."""
    for V, (ty, decl) in B.vars.items():
        if not ty.rstrip().endswith("*") or V == "this":
            continue
        T = ty.replace("const", "").replace("*", "").strip()
        for i in B.uses[V]:
            st = _step(B, i, V)
            blk = B.block[i]
            if not st or not blk or B.toks[blk - 1][0] not in ("do", ")"):
                continue
            s0, s1, how = st
            end = B.close.get(blk, len(B.toks) - 1)
            inside = [k for k in B.uses[V] if blk < k < end and not s0 <= k <= s1]
            homes = [a for a, b, bl in _simple(B) if b <= blk and a > max(decl, 0) and
                     (bl == B.block[blk] or bl < blk and B.close.get(bl, 0) > blk)]
            stepx = ("sizeof(%s)" % T if how is None else "(%s) * sizeof(%s)" % (how[0], T) if how[1] else how[0])
            for off in offs:
                rep = [(B.toks[k][1], B.toks[k][2], "((%s*)(rg_p - %d))" % (T, off)) for k in inside]
                rep.append((B.toks[s0][1], B.toks[s1][2], "rg_p += %s" % stepx))
                for h in homes[-4:]:
                    at = B.toks[h][1]
                    yield ("ptrfield", "char* rg_p = (char*)%s + %d before body line %d; loop reads/steps through it" % (
                        V, off, B.text.count("\n", B.b, at)),
                        rep + [(at, at, "char* rg_p = (char*)%s + %d; " % (V, off))])
            break


def candidates(c, span, offs):
    out = list(gen_move(c.B, span))
    if offs and "rg_p" not in c.text:
        out += list(gen_ptrfield(c.B, offs))
    seen, res = set(), []
    for g in out:
        k = repr(sorted(g[2]))
        if k not in seen:
            seen.add(k)
            res.append(g)
    return res


def score(c, f):
    return (c.bdiff(f), c.regdiff(f), c.oldreg(f))


def measure(c, cands, tag, jobs):
    def one(k):
        fam, desc, edits = cands[k]
        try:
            f, path = c.build("%s%03d" % (tag, k), edits, "")
            return dict(k=k, fam=fam, desc=desc, f=f, path=path, sc=score(c, f), edits=edits)
        except (Exception, SystemExit) as e:
            return dict(k=k, fam=fam, desc=desc, err=str(e)[-100:])
    with cf.ThreadPoolExecutor(jobs) as ex:
        rs = list(ex.map(one, range(len(cands))))
    return [r for r in rs if "err" not in r], [r for r in rs if "err" in r]


def main():
    sys.stdout.reconfigure(line_buffering=True)
    argv, cl = sys.argv[1:], []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    ap.add_argument("--asm-vs", required=True)
    ap.add_argument("--asm-func")
    ap.add_argument("--max-compiles", type=int, default=60)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--span", type=int, default=4)
    ap.add_argument("--no-ptrfield", action="store_true")
    ap.add_argument("--no-diag", action="store_true", help="skip the regmap / rasim diagnosis")
    ap.add_argument("--keep")
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args(argv)
    a.asm_vs = os.path.abspath(a.asm_vs)
    a.want_regs = None
    cur, budget = os.path.abspath(a.src), a.max_compiles
    best_path = None
    for rnd in range(a.rounds):
        a.src = cur
        c = realize.Ctx(a, cl)
        base = score(c, c.base)
        print("round %d: %s: bdiff %s regdiff %s (%s)" % (rnd + 1, os.path.basename(cur), base[0], base[1], c.func))
        if base[0] == 0:
            print("MATCH")
            break
        if rnd == 0 and not a.no_diag:
            diagnose(c, regmap_rows(cur, a.func, a.asm_vs, a.asm_func, cl), cl)
        cands = candidates(c, a.span, [] if a.no_ptrfield else orig_offsets(a.asm_vs))[:budget]
        if not cands:
            print("no candidates")
            break
        good, bad = measure(c, cands, "r%d_" % rnd, a.jobs)
        budget -= len(cands)
        good.sort(key=lambda r: r["sc"])
        print("%d candidates, %d compiled" % (len(cands), len(good)))
        for r in good[:a.top]:
            print("  bdiff %-4s regdiff %-3s %-8s %s\n        %s" % (r["sc"][0], r["sc"][1], r["fam"], r["desc"],
                                                                 c2trace.c2build.relpath(r["path"], ROOT)))
        if not good or good[0]["sc"] >= base:
            print("no candidate improves the base")
            break
        cur = best_path = good[0]["path"]
        if good[0]["sc"][0] == 0:
            print("MATCH: %s" % c2trace.c2build.relpath(cur, ROOT))
            break
        if budget <= 0:
            break
    if best_path:
        dst = os.path.join(c.out, "best.cpp")
        open(dst, "w").write(open(best_path).read())
        print("best: %s" % c2trace.c2build.relpath(dst, ROOT))


if __name__ == "__main__":
    main()
