#!/usr/bin/env python3
"""Scheduler inverse: which DAG changes give the issue order the original has.  Doc: docs/c2/sched.md

usage: schedwant.py <src> --func F --asm-vs ORIG.s [--asm-func F] [--region K] [--depth 1|2] [-- <cl args>]
       schedwant.py <src> --func F --region K --want-lex 1,2,4,3,... [-- <cl args>]

c2sched.py prints what the list scheduler did (heights, successor counts, priorities, issue order). This tool runs
the other way. For each scheduled region whose issue order differs from the original's, it:

  1. finds the original's order: the region's instructions are matched to a window of ORIG.s (mnemonic and the set
     of registers, esp left out) and the window's order is read back as lexical positions; or --want-lex;
  2. replays the scheduler with sched.md rules 5-7 (height = 1 + max(successor height + edge latency), priority =
     (height + successors) << 10 | reads memory << 15 | sets the branch flags << 23, then repeatedly issue the
     ready node with the highest (priority, -lex)). The traced order is replayed first as a check;
  3. tries DAG perturbations and replays each one, keeping those that give the wanted order (or more of its
     prefix than the traced DAG does):
       unalias:X        X loses every memory edge (types 0x20/0x40/0x80): X reads/writes a symbol nothing else in the region
                        may alias (a stack parameter or a local whose address is not taken, rule 4), or goes through
                        the same base register as the other access at a disjoint offset
       alias:X          X, a store, gains memory edges to every other store of the region (store -> store, 0x80): X
                        becomes a store the disambiguation cannot place (sched.md rule 4a: an index that is not a
                        constant when the interference is built, or a store through another pointer symbol such as an
                        inlined constructor's `this`), so earlier stores must issue before it and later ones after it
       cut:X>Y          one DAG edge removed (memory: aliasing as above; anti/output: a register reuse, i.e.
                        allocation; true: a different value)
       ns:X+1, ns:X-1   one more / fewer successor of X (a use added or removed, a value that feeds a push)
     --depth 2 also tries pairs (unalias, alias and ns edits only);
  4. prints, for each one that works, what it means in source and the constructs that are known to do it.

Spore 0x00598db0 (cl 15.00): the original loads `esi=[esi]` before the copy's first load; ours loads key.lo first
(prio c800 > c400: ns 4 against 3). `unalias` of both key loads gives the wanted order: the original copies from the
parameters (a0, a1: not address-taken) rather than from the address-taken local `key`; spelling the copy as
`ItemID k2; k2.instanceID = a0; k2.groupID = a1; vec->mpBegin[a4] = k2;` is a byte match.
"""
import argparse, itertools, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace
import c2sched
import tsweep

REG = r"\b(e[a-d]x|e[sd]i|ebp|[a-d][lhx]|[sd]il?|bp|xmm\d|mm\d|st\(?\d?\)?)\b"
# DAG edge type bits (DagBuildGraph): registers 1 true / 2 anti / 4 output; memory 0x20 store -> load, 0x40 load ->
# store, 0x80 store -> store (read off the traced edges: Spore 0x00598db0 region 2)
ETYPE = {1: "true", 2: "anti", 4: "output", 0x20: "mem-true", 0x40: "mem-anti", 0x80: "mem-output",
         0x2000: "volatile", 0x80000: "control"}
MEM = 0xe0


def tkey(text):
    """(mnemonic, registers) of a pre-schedule tuple as c2sched prints it."""
    op, _, ops = text.partition("|")
    mn = op.split()[0] if op.split() else "?"
    mn = {"_epop": "pop", "_epush": "push"}.get(mn, mn)
    regs = frozenset(re.findall(r"\b(e[a-d]x|e[sd]i|ebp|[a-d][lhx]|xmm\d)[=:]", ops))
    return mn, regs if mn != "call" else frozenset()


def okey(ins):
    mn, ops = ins
    return mn, frozenset(r for r in re.findall(REG, ops) if r != "esp") if mn != "call" else frozenset()


def etype(t):
    return "+".join(n for b, n in ETYPE.items() if t & b) or "%x" % t


def _sh(x, n):
    return x << n if n >= 0 else x >> -n


def _prio(ht, ns, fl):
    """Static priority with the build's _SchmdHWT row (c2sched.HWT; 14.00's: (ht + ns) << 10 | L << 15 | B << 23)."""
    w = c2sched.HWT
    return _sh(fl >> 1 & 1, w[0]) + _sh(ht, w[1]) + _sh(ns, w[2]) + _sh(fl & 1, w[3]) + _sh(fl >> 2 & 1, w[5])


class Region:
    def __init__(self, r, txt):
        self.N = {t: dict(n) for t, n in r["nodes"].items() if t}
        self.txt = txt
        self.edges = [(a, b, lat, typ) for a, b, lat, typ, plat, eb in r["edges"]]
        self.order = [t for c, ts in r["issue"] for t in ts if t]
        self.bylex = {n["lex"]: t for t, n in self.N.items()}

    def name(self, t):
        return "%d:%s" % (self.N[t]["lex"], c2sched.short(self.txt.get(t, "?")).split("<-")[0].strip()[:28])

    def replay(self, edges, nsd=None):
        """Issue order under rules 5-7 for this edge list (nsd: extra successor counts)."""
        N = self.N
        succ, pred = {}, {}
        for a, b, lat, typ in edges:
            succ.setdefault(a, []).append((b, lat))
            if a in N and b in N:
                pred.setdefault(b, set()).add(a)
        ht = {}

        def h(t):
            if t not in N:
                return 0
            if t not in ht:
                s = succ.get(t)
                ht[t] = 1 + max(h(b) + lat for b, lat in s) if s else N[t]["ht"]
            return ht[t]
        prio = {}
        for t, n in N.items():
            ns = len(succ.get(t, ())) + (nsd or {}).get(t, 0)
            prio[t] = _prio(h(t), ns, n["fl"])
        done, out = set(), []
        while len(out) < len(N):
            ready = [t for t in N if t not in done and pred.get(t, set()) <= done]
            if not ready:
                break
            t = min(ready, key=lambda x: (-prio[x], N[x]["lex"]))
            out.append(t)
            done.add(t)
        return out, prio


def anchor_map(ours, orig):
    """{our listing index: orig index} over the equal blocks of an alignment on (mnemonic, registers)."""
    import difflib
    a = [okey(x) for x in ours]
    b = [okey(x) for x in orig]
    m = {}
    for i, j, n in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for k in range(n):
            m[i + k] = j + k
    return m


def want_from_orig(reg, orig, ours=None, amap=None):
    """The original's order of this region's instructions: (lex order, orig window start, instructions matched) or
    None. With our listing (`ours`, `amap` from anchor_map) the region is found in our code (its issued order) and the
    window is the original's code aligned with it; otherwise the closest window by (mnemonic, registers) multiset."""
    from collections import Counter
    keys = {t: tkey(reg.txt.get(t, "?")) for t in reg.N}
    n = len(keys)
    ok = [okey(i) for i in orig]
    want = Counter(keys.values())
    starts = range(0, max(1, len(ok) - n + 1))
    if ours is not None:
        seq = [keys[t] for t in reg.order]
        ko = [okey(x) for x in ours]
        cand = []
        for s in range(len(ko) - n + 1):
            if sum((Counter(ko[s:s + n]) & want).values()) >= n - 1:
                cand.append((sum(x == y for x, y in zip(ko[s:s + n], seq)), s))
        if not cand:
            return None
        cand.sort(reverse=True)
        if len(cand) > 1 and cand[1][0] == cand[0][0]:
            return None                           # two equally good places in our code: ambiguous
        s0 = cand[0][1]
        near = [amap[k] - (k - s0) for k in range(max(0, s0 - 8), min(len(ko), s0 + n + 8)) if k in amap]
        if not near:
            return None
        c = Counter(near).most_common(1)[0][0]
        starts = [x for x in range(c - 2, c + 3) if 0 <= x <= len(ok) - n]
    best = None
    for s in starts:
        sc = sum((Counter(ok[s:s + n]) & want).values())
        if best is None or sc > best[0]:
            best = (sc, s)
    unk = sum(1 for k in keys.values() if k[0] == "?")     # nodes without IL text (labels, pseudo-ops)
    if best is None or best[0] < n - unk:
        return None
    s = best[1]
    pool = sorted(reg.N, key=lambda t: reg.N[t]["lex"])
    order = []
    for k in ok[s:s + n]:
        m = next((t for t in pool if keys[t] == k and t not in order), None)
        if m is not None:
            order.append(m)
    order += [t for t in pool if t not in order]
    return order, s, best[0]


def prefix(a, b):
    k = 0
    while k < min(len(a), len(b)) and a[k] == b[k]:
        k += 1
    return k


MEANING = {
    "unalias": "X stops aliasing the region's other memory accesses: read/write a stack parameter or a local whose "
               "address is not taken instead (e.g. copy from the parameters, not from an address-taken struct "
               "local built from them), or reach both accesses through the same base register at disjoint "
               "offsets (sched.md rule 4)",
    "alias": "X becomes a store the scheduler cannot separate from the object's other stores, which pins the earlier "
             "stores above it and the later ones below it (sched.md rule 4a). Natural spellings: an index that is "
             "not a literal before the global optimiser, e.g. VC8's `s->buf[s->size = 0] = 0` (R1270; 14.00/15.00), "
             "`int n = 0; s->size = n; s->buf[n] = 0;`, `s->buf[s->size] = 0` after `s->size = 0`; a store through "
             "a pointer copy `char* q = s->buf; q[0] = 0;`; or, for a vtable store in a constructor, a member "
             "subobject with its own (inlined) constructor, `struct Handle { unsigned v; Handle() : v(0) {} };` "
             "(R1275: the inlinee's `this` is another pointer symbol)",
    "cut-memory": "as unalias, for this pair only",
    "cut-anti": "a register reuse: another allocation (c2trace regs / scratch), not a source edit as such",
    "cut-output": "a register reuse: another allocation",
    "cut-true": "X does not feed Y: Y computes from another value",
    "ns+1": "one more use of X's result inside the region (or X feeding a push)",
    "ns-1": "one use fewer of X's result inside the region",
}


def analyse(reg, want, a):
    L = lambda t: reg.N[t]["lex"]
    base, prio = reg.replay(reg.edges)
    print("   traced order : %s" % " ".join(str(L(t)) for t in reg.order))
    print("   replayed     : %s%s" % (" ".join(str(L(t)) for t in base),
                                       "  (= traced)" if base == reg.order else "  (model differs from the trace)"))
    print("   wanted       : %s" % " ".join(str(L(t)) for t in want))
    p0 = prefix(base, want)
    if base == want:
        print("   the replay already gives the wanted order")
        return
    x, y = base[p0], want[p0]
    print("   first difference at position %d: the scheduler issues %s (prio %x), the original %s (prio %x)" % (
        p0, reg.name(x), prio[x], reg.name(y), prio[y]))
    if prio[x] == prio[y]:
        print("   equal priority: the lexical (pre-schedule) order decides. Besides the perturbations below, the fix is\n"
              "   that order itself: statement order, or for register-allocator reloads / spill stores (operands like\n"
              "   `t883` with no source name) the order the allocator inserts them (regalloc.md 3.6.9, rasim --predict)")
    nodes = sorted(reg.N, key=L)
    istore = lambda t: bool(reg.N[t]["fl"] >> 3 & 1) and "[" in reg.txt.get(t, "").partition("<-")[0]
    edits = []
    for t in nodes:
        if any((e[0] == t or e[1] == t) and e[3] & MEM for e in reg.edges):
            edits.append(("unalias:%d" % L(t), "unalias", [e for e in reg.edges if (e[0] == t or e[1] == t) and e[3] & MEM], {}))
        if istore(t):                                   # a store through a pointer: pinned against the others
            have = {(e[0], e[1]) for e in reg.edges}
            add = [(o, t, 0, 0x80) if L(o) < L(t) else (t, o, 0, 0x80) for o in nodes if o != t and istore(o)]
            add = [e for e in add if (e[0], e[1]) not in have]
            if add:
                edits.append(("alias:%d" % L(t), "alias", [], {}, add))
        edits.append(("ns:%d+1" % L(t), "ns+1", [], {t: 1}))
        edits.append(("ns:%d-1" % L(t), "ns-1", [], {t: -1}))
    singles = list(edits)
    for e in reg.edges:
        if e[0] in reg.N and e[1] in reg.N:
            kind = "cut-" + ("memory" if e[3] & MEM else "true" if e[3] & 1 else "anti" if e[3] & 2 else "output")
            edits.append(("cut:%d>%d(%s)" % (L(e[0]), L(e[1]), etype(e[3])), kind, [e], {}))
    combos = [(e,) for e in edits]
    if a.depth >= 2:
        combos += [c for c in itertools.combinations(singles, 2)
                   if {c[0][1], c[1][1]} != {"alias", "unalias"} or c[0][0].partition(":")[2] != c[1][0].partition(":")[2]]
    res = []
    for c in combos:
        drop = set()
        nsd = {}
        extra = []
        for x in c:
            drop.update(x[2])
            extra += x[4] if len(x) > 4 else []
            for k, v in x[3].items():
                nsd[k] = nsd.get(k, 0) + v
        o, _ = reg.replay([e for e in reg.edges if e not in drop] + extra, nsd)
        res.append((o == want, prefix(o, want), -len(c), c, o))
    res.sort(key=lambda r: (not r[0], -r[1], -r[2]))
    full = [r for r in res if r[0]]
    print("   %d of %d perturbations give the wanted order; best partial: %d of %d positions (traced DAG: %d)" % (
        len(full), len(res), max(r[1] for r in res), len(want), p0))
    if not full:
        print("   (no DAG change of this size explains it: the pre-schedule order itself may differ, i.e. an operand sort\n"
              "    (symkeys.py sites, sulever.py) or the front end's statement order; compare c2trace.py il --at @Schedule@8)")
    shown = 0
    for ok, pf, _, c, o in res:
        if shown >= a.top or (not ok and pf <= p0):
            break
        shown += 1
        print("   %s %-40s %s" % ("WANTED" if ok else "%2d/%-3d" % (pf, len(want)), " + ".join(x[0] for x in c),
                                   "" if ok else "-> " + " ".join(str(L(t)) for t in o)))
    kinds = []
    for ok, pf, _, c, o in res[:shown]:
        for x in c:
            if x[1] not in kinds:
                kinds.append(x[1])
    if kinds:
        print("   meaning:")
        for k in kinds:
            print("     %-11s %s" % (k, MEANING[k]))
    if a.edges:
        print("   edges (from > to: type, latency):")
        for e in reg.edges:
            if e[0] in reg.N:
                print("     %s > %s: %s, %d" % (reg.name(e[0]), reg.name(e[1]) if e[1] in reg.N else "exit", etype(e[3]), e[2]))


def main(argv):
    ap = argparse.ArgumentParser(prog="schedwant.py", usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    ap.add_argument("--asm-vs")
    ap.add_argument("--asm-func")
    ap.add_argument("--want-src", help="take the wanted order from this variant's compile instead of ORIG.s")
    ap.add_argument("--region", type=int)
    ap.add_argument("--want-lex")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--edges", action="store_true", help="also print the region's DAG edges")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    if not a.asm_vs and not a.want_lex and not a.want_src:
        ap.error("need --asm-vs ORIG.s, --want-src VARIANT.cpp or --region K --want-lex ...")
    import tempfile, shutil
    kd = tempfile.mkdtemp(prefix="schedwant_")
    img, hk, rc, out, log = c2trace.run_trace(a.src, c2sched.HOOKS, cl, keep=kd, quiet=True)
    ours = None
    try:
        ours = list(tsweep.listing_instrs(os.path.join(kd, os.path.splitext(os.path.basename(a.src))[0] + ".asm"),
                                          a.asm_func or a.func))
    except (KeyError, OSError):
        pass
    shutil.rmtree(kd, ignore_errors=True)
    if rc:
        sys.stdout.write(out)
    funcs = c2sched.parse(log, a.func)
    if not funcs:
        sys.exit("function %r never reached Schedule" % a.func)
    orig = tsweep.orig_instrs(a.asm_vs) if a.asm_vs else None
    if a.want_src:
        import tempfile
        d = tempfile.mkdtemp(prefix="schedwant_")
        c2trace.run_trace(a.want_src, c2sched.HOOKS, cl, keep=d, quiet=True)
        asm = os.path.join(d, os.path.splitext(os.path.basename(a.want_src))[0] + ".asm")
        orig = list(tsweep.listing_instrs(asm, a.asm_func or a.func))
        if not orig:
            sys.exit("--want-src: %s not in the variant's listing" % a.func)
    amap = anchor_map(ours, orig) if ours and orig else None
    for name, pre, post, regions in funcs[:1]:
        txt = c2sched.instr_text(pre)
        print("== %s: %d regions" % (name, len(regions)))
        for k, r in enumerate(regions):
            if a.region is not None and k != a.region:
                continue
            reg = Region(r, txt)
            if len(reg.N) < 2:
                continue
            if a.want_lex:
                want = [reg.bylex[int(x)] for x in a.want_lex.split(",")]
                want += [t for t in sorted(reg.N, key=lambda t: reg.N[t]["lex"]) if t not in want]
            else:
                w = want_from_orig(reg, orig, ours if amap else None, amap)
                if w is None:
                    if a.region is not None:
                        print("-- region %d: no window of the original matches its instructions" % k)
                    continue
                want, s, sc = w
                if want == reg.order:
                    continue
                print("-- region %d (%d instrs; original window at line %d, %d of %d instructions matched)" % (
                    k, len(reg.N), s + 1, sc, len(reg.N)))
            if a.want_lex:
                print("-- region %d (%d instrs)" % (k, len(reg.N)))
            for t in sorted(reg.N, key=lambda t: reg.N[t]["lex"]):
                n = reg.N[t]
                print("   %3d ht %2d ns %d %-4s %6x  %s" % (n["lex"], n["ht"], n["ns"], c2sched.flags(n["fl"]), n["prio"],
                                                        c2sched.short(txt.get(t, "?"))))
            analyse(reg, want, a)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
