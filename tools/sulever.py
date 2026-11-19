#!/usr/bin/env python3
"""Levers for the weight components of a commutative operand sort that key edits cannot move (SU and count).

usage: sulever.py <src> --func F --site LINE:A>B [--pass 2] [-- <cl args>]                 (diagnosis, 1 traced compile)
       sulever.py <src> --func F --site LINE:A>B --spell TEXT [--src-line N] [--asm-vs ORIG.s [--asm-func F]]
                  [--jobs 8] [-- <cl args>]                                        (+ catalogue spellings, compiled)
       sulever.py --from EXPORT.json --func F --site LINE:A>B                       (symkeys.py export; no compile)

OptLeftWeightTree sorts the operands of OPADD/OPMUL/OPAND/OPOR/OPXOR/OPEQ/OPNE by the weight
`SU << 24 | count << 16 | hash`, descending (docs/c2/x87.md rule 2, symkeys.md section 3). `symkeys.py levers` searches
only edits that move symbol keys, i.e. the hash. When the wanted order loses on SU or count, no key edit can give it.
This tool says which component decides and by how much, and what would satisfy the order:

  1. the decision: for --site LINE:A>B (as `symkeys.py sites` prints the operands; LINE an offset, LO-HI or *), the
     weights of A and B split into SU / count / hash, which component decides, and the smallest change of A (raise)
     or of B (lower) that gives A>B;
  2. regrouping (sorts of 3 or more operands, i.e. a flattened chain): for every operand C, the weight the pair
     (A, C) would have as one subtree (SU by OptNodeComplexity: the first operand's SU, then +1 for each later one
     with an equal SU, its SU + 1 for a larger one; count 1 + both counts), and whether the outer sort then puts it
     before B. The pair becomes one operand only under another opcode (the chain flattens through equal opcodes);
  3. the catalogue of source constructs that change an operand's SU or count, with what they do to the code
     (CATALOGUE below; [C] = seen in a compile);
  4. --spell TEXT: TEXT is the source text of operand A (or B), e.g. `(uint32)lo`, on --src-line N (default: the one
     line of <src> that holds it). Every catalogue spelling that applies is written into a tsweep template
     (`work/sulever/<stem>-<hash>/sweep.cpp`); each is traced again to read the site's new order, and with --asm-vs
     tsweep ranks them by bdiff. A spelling that satisfies the site with bdiff 0 is a match.

Example (Spore 0x0068c6d0, cl 15.00; the 3-operand OR `(hi<<8) | lo | (mGroup & 0xffff0000)`; lo, a zero-extended
byte parameter, is SU0 and the mask term SU1, so lo sorts last whatever its key):
  sulever.py --c2 15.00 f.cpp --func SetKeyKind --site '*:_lo>$o' --spell '(uint32)lo' --asm-vs orig.s -- /O2 ...
gives `(*(uint32*)&lo & 0xff)` (SU1: an AND of a 32-bit view and a constant; instruction selection turns
`and m32, 0xff` back into `movzx r32, byte ptr`), bdiff 0.
"""
import argparse, hashlib, json, os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace
import symkeys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parts(w):
    return (w >> 24, (w >> 16) & 0xff, w & 0xffff)


def wstr(w):
    if w is None:
        return "?"
    s, n, h = parts(w)
    return "SU%d n%d h%04x" % (s, n, h)


def node_su(sus):
    """OptNodeComplexity over operands already in sorted order (x87.md rule 2)."""
    run = sus[0]
    for s in sus[1:]:
        if s == run:
            run += 1
        elif s > run:
            run = s + 1
    return run


# The catalogue: constructs that change an operand's SU or count.  `kind` selects the operands it applies to
# (see applies()); `spell` maps the variable name X (and the narrow type's mask) to a spelling; `neutral` says what it
# does to the code.
CATALOGUE = [
    dict(id="view-mask", effect="SU0 -> SU1 (count 2): AND(32-bit view of X, MASK)", applies="narrow",
         neutral="yes when X lives in memory (a parameter, an address-taken local): instruction selection folds "
                 "`and m32, 0xff` / `0xffff` into `movzx r32, byte/word ptr` [C: Spore 0x0068c6d0, bdiff 2 -> 0]",
         spell=lambda x, m: ["(*(unsigned int*)&%s & %s)" % (x, m)]),
    dict(id="wide-mask", effect="SU0 -> SU1: AND(zero-extension of X, a mask wider than X's type)", applies="narrow",
         neutral="no: the `and` stays (0xffff on a byte: +1 instruction) [C: 0x0068c6d0, bdiff 2]",
         spell=lambda x, m: ["((unsigned int)%s & %s)" % (x, "0xffff" if m == "0xff" else "0xffffff")]),
    dict(id="same-width-mask", effect="none: `(T)X & 0xff` on a byte X is folded before pass 1", applies="narrow",
         neutral="(listed so that it is not tried again) [C: 0x0068c6d0 r2_t1]", spell=None),
    dict(id="regroup-op", effect="the pair (A, C) becomes one operand (SU/count as `regrouping` prints)",
         applies="nary",
         neutral="no: the inner operator is another opcode (`+` or `^` for disjoint bits: `add` against `or`) "
                 "[C: 0x0068c6d0 r2, bdiff 8 -> 2]", spell=None),
    dict(id="local-copy", effect="none: a single-use local (`uint32 v = ..; x = v | ..`, also `v |= ..` chains, "
         "`g = m & k; g |= ..`) is substituted before the sort", applies="any",
         neutral="(listed so that it is not tried again) [C: 0x0068c6d0 r3_t1, 8 spellings]", spell=None),
    dict(id="member-offset", effect="a member at offset 0 is a memory leaf (SU0 n1); at offset d != 0 it is reached "
         "through an address temporary and weighs as OPADD(p, d) (SU1 n2)", applies="mem",
         neutral="only through the layout (which member is at 0), x87.md rule 2 [C]", spell=None),
    dict(id="call", effect="a call adds 7 to SU", applies="any", neutral="no", spell=None),
]


def applies(o, what):
    if what == "any":
        return True
    if what == "narrow":           # a conversion of a variable (kind 13 with no memory source): `(T)x`
        return o["kind"] == 13 and not o.get("mem") and o["w"] is not None and parts(o["w"])[0] == 0
    if what == "mem":
        return o["kind"] in (6, 13) and bool(o.get("mem"))
    return False


def diagnose(f, site, pass_):
    evs = symkeys.site_events(f, site, pass_)
    if not evs:
        print("no pass-%d sort on lines %s with operands matching %r and %r (`symkeys.py sites` prints them)"
              % (pass_, site["text"].split(":")[0], site["A"], site["B"]))
        return []
    out = []
    for e, ia, ib in evs:
        ops = e[4]
        A, B = ops[ia], ops[ib]
        print("pass %d line %d %s, %d operands, sorted: %s" % (e[1], e[3], e[2], len(ops),
              " | ".join("%s %s" % (symkeys.opname(o), wstr(o["w"])) for o in ops)))
        if A["w"] is None or B["w"] is None:
            print("  an operand's weight is not logged (5th or later); nothing to say")
            continue
        sa, na, ha = parts(A["w"])
        sb, nb, hb = parts(B["w"])
        state = "satisfied" if ia < ib else "not satisfied"
        comp = "SU" if sa != sb else "count" if na != nb else "hash" if ha != hb else "nothing (equal: input order)"
        print("  A %-28s %s" % (symkeys.opname(A), wstr(A["w"])))
        print("  B %-28s %s" % (symkeys.opname(B), wstr(B["w"])))
        print("  A>B is %s; decided by %s" % (state, comp))
        if ia > ib:
            if comp == "hash":
                print("  -> a key edit can do it: symkeys.py levers --site '%s'" % site["text"])
            else:
                need = []
                if comp == "SU":
                    need.append("raise A to SU%d (count/hash then decide: B is n%d h%04x)" % (sb, nb, hb))
                    need.append("raise A to SU%d (wins outright)" % (sb + 1))
                    need.append("lower B to SU%d" % sa + (" (then count: A n%d vs B n%d at the same SU)" % (na, nb)))
                else:
                    need.append("raise A's count to %d or more (or the same count and a larger hash)" % nb)
                    need.append("lower B's count below %d" % na)
                print("  -> no key edit can do it (symkeys levers searches keys, i.e. hash); needed, one of:")
                for x in need:
                    print("       %s" % x)
        if len(ops) > 2:
            print("  regrouping (A with one neighbour C as one subtree, under another opcode):")
            for ic, C in enumerate(ops):
                if ic in (ia, ib) or C["w"] is None:
                    continue
                pair = sorted([A, C], key=lambda o: -o["w"])
                su = node_su([parts(o["w"])[0] for o in pair])
                n = 1 + parts(A["w"])[1] + parts(C["w"])[1]
                rest = [o for k, o in enumerate(ops) if k not in (ia, ic)]
                gw = (su << 24) | (n << 16)
                before = [o for o in rest if o["w"] > gw + 0xffff]
                ties = [o for o in rest if gw <= o["w"] <= gw + 0xffff]
                pos = "first" if not before and not ties else "after %s" % ", ".join(symkeys.opname(o) for o in before) \
                    if before else "tied with %s on SU/count (hash decides)" % ", ".join(symkeys.opname(o) for o in ties)
                okb = B in rest and (B["w"] < gw)
                print("    (A, %s): SU%d n%d -> sorts %s; before B: %s" % (symkeys.opname(C), su, n, pos,
                      "yes" if okb else "on hash" if gw <= B["w"] <= gw + 0xffff else "no"))
        print("  catalogue (constructs that move SU or count):")
        for c in CATALOGUE:
            tgt = [nm for nm, o in (("A", A), ("B", B)) if applies(o, c["applies"])]
            if c["applies"] == "nary":
                tgt = ["A"] if len(ops) > 2 else []
            if tgt:
                print("    %-15s on %-3s %s\n%20s code: %s" % (c["id"], "/".join(tgt), c["effect"], "", c["neutral"]))
        out.append((e, ia, ib))
    return out


def var_of(text):
    """The variable in an operand's source text: `(uint32)lo`, `lo`, `(unsigned)(x)` -> name; None otherwise."""
    m = re.fullmatch(r"\s*\(*\s*(?:\(\s*[A-Za-z_][\w ]*\)\s*)?\(?\s*([A-Za-z_]\w*)\s*\)*\s*", text)
    return m.group(1) if m else None


def narrow_mask(src, x):
    """0xff or 0xffff from X's declared type in the source (a parameter or local); 0xff by default."""
    for m in re.finditer(r"([A-Za-z_][\w ]*?)\s+%s\s*[,);=]" % re.escape(x), src):
        t = m.group(1).split()[-1:] or [""]
        t = " ".join(m.group(1).split()[-2:])
        if re.search(r"short|16|word|WORD", t):
            return "0xffff"
        if re.search(r"char|8|byte|BYTE|bool", t):
            return "0xff"
    return "0xff"


def spell(a, f, site, evs, cl):
    src = open(a.src).read()
    lines = src.split("\n")
    if a.src_line:
        ln = a.src_line - 1
        if a.spell not in lines[ln]:
            sys.exit("%r is not on line %d" % (a.spell, a.src_line))
    else:
        hit = [i for i, l in enumerate(lines) if a.spell in l]
        if len(hit) != 1:
            sys.exit("%r is on %d lines of %s; give --src-line" % (a.spell, len(hit), a.src))
        ln = hit[0]
    x = var_of(a.spell)
    if not x:
        sys.exit("--spell %r: not a variable or a conversion of one" % a.spell)
    mask = narrow_mask(src, x)
    alts = []
    for c in CATALOGUE:
        if c["spell"] is None:
            continue
        if not any(applies(e[4][i], c["applies"]) for e, ia, ib in evs for i in (ia, ib)):
            continue
        for s in c["spell"](x, mask):
            alts.append((c["id"], s))
    if not alts:
        print("no catalogue spelling applies to these operands")
        return
    h = hashlib.sha1((os.path.abspath(a.src) + a.spell).encode()).hexdigest()[:6]
    d = os.path.join(HERE, "work", "sulever", "%s-%s" % (os.path.splitext(os.path.basename(a.src))[0], h))
    os.makedirs(d, exist_ok=True)
    tl = lines[ln].replace(a.spell, "{{" + "<|>".join([a.spell] + [s for _, s in alts]) + "}}", 1)
    tmpl = os.path.join(d, "sweep.cpp")
    open(tmpl, "w").write("\n".join(lines[:ln] + [tl] + lines[ln + 1:]))
    print("\n#### spellings of %r on line %d (template %s)" % (a.spell, ln + 1, tmpl))
    rows = []
    for k, (cid, s) in enumerate([("draft", a.spell)] + alts):
        v = os.path.join(d, "s%02d.cpp" % k)
        open(v, "w").write("\n".join(lines[:ln] + [lines[ln].replace(a.spell, s, 1)] + lines[ln + 1:]))
        rows.append((cid, s, v))

    def one(r):
        try:
            fs = [g for g in symkeys.trace(r[2], cl) if g.name and c2trace.func_match(a.func, g.name)]
        except SystemExit as ex:
            return "compile failed (%s)" % ex
        if not fs:
            return "function not found"
        ev = symkeys.site_events(fs[0], site, a.pass_)
        if ev:
            return "; ".join("A>B satisfied" if ia < ib else "not satisfied (%s vs %s)" % (wstr(e[4][ia]["w"]), wstr(e[4][ib]["w"]))
                             for e, ia, ib in ev)
        # the operands' text changed with the spelling: show the sort of the same opcode and arity on those lines
        e0 = evs[0][0]
        same = [e for e in fs[0].events if e[0] == "sort" and e[1] == a.pass_ and e[2] == e0[2]
                and len(e[4]) == len(e0[4]) and site["lo"] <= e[3] <= site["hi"]]
        if not same:
            return "no %d-operand %s sort left" % (len(e0[4]), e0[2])
        return "order: " + " | ".join("%s %s" % (symkeys.opname(o), wstr(o["w"])) for o in same[0][4])
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(a.jobs) as ex:
        states = list(ex.map(one, rows))
    bd = {}
    if a.asm_vs:
        cmd = ["uv", "run", os.path.join(HERE, "tools", "tsweep.py"), tmpl, "--func", a.func, "--asm-vs", a.asm_vs,
               "--asm-func", a.asm_func or a.func, "--jobs", str(a.jobs), "--top", "99", "--"] + cl
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
        for m in re.finditer(r"^\s*(\S+)\s+v(\d+)\.cpp", r.stdout, re.M):
            bd[int(m.group(2))] = m.group(1)
        if not bd:
            sys.stdout.write(r.stdout[-2000:] + r.stderr[-2000:])
    print("%-16s %-44s %-8s %s" % ("construct", "spelling", "bdiff" if a.asm_vs else "", "site order"))
    for k, ((cid, s, v), st) in enumerate(zip(rows, states)):
        print("%-16s %-44s %-8s %s" % (cid, s, bd.get(k, ""), st))
    print("(variants: %s/s*.cpp; s00 is the draft)" % d)


def main():
    argv = sys.argv[1:]
    cl = []
    if "--" in argv:
        i = argv.index("--")
        argv, cl = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("src", nargs="?")
    ap.add_argument("--from", dest="from_")
    ap.add_argument("--func", required=True)
    ap.add_argument("--site", required=True)
    ap.add_argument("--pass", dest="pass_", type=int, default=2)
    ap.add_argument("--spell")
    ap.add_argument("--src-line", type=int)
    ap.add_argument("--asm-vs")
    ap.add_argument("--asm-func")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args(argv)
    if a.from_:
        funcs = symkeys.from_json(json.load(open(a.from_)))
    elif a.src:
        funcs = symkeys.trace(a.src, cl)
    else:
        ap.error("need <src> or --from")
    sel = [f for f in funcs if f.name and c2trace.func_match(a.func, f.name)]
    if not sel:
        sys.exit("no function matching %s" % a.func)
    site = symkeys.parse_site(a.site)
    for f in sel:
        print("#### %s" % f.name)
        evs = diagnose(f, site, a.pass_)
        if a.spell and evs:
            if not a.src:
                sys.exit("--spell needs <src>")
            spell(a, f, site, evs, cl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
