#!/usr/bin/env python3
"""l2c_search: a rewrite search on il2c_lift's near misses.

usage: l2c_search.py CORPUS.jsonl VA... [--rounds N] [--jobs N] [--json OUT] [--min-ins P]
       l2c_search.py CORPUS.jsonl --from CHECK.jsonl ...     (the DIFF rows of an `il2c_lift.py check --json`)

Lifts each function, then hill-climbs on the C text: every single rewrite of the current text is compiled at
the corpus flags and scored with lbench's CI scorer; the best variant that is strictly closer to retail
(MATCH, then more aligned instructions, then a closer size) becomes the current text, for up to N rounds.
The rewrites change what cl sees, not what the function computes, except where noted:
  swap     two adjacent statements of one block that are independent (no value of one read or written by the
           other, not two memory accesses of which one is a store, no call): statement order steers cl's
           scheduler and its register choice
  cmp      a comparison's operands exchanged (`a < b` -> `b > a`)
  comm     a commutative operator's operands exchanged (+ * & | ^ at the top of an assignment's value)
  decl     two adjacent declarations exchanged; sign: a temporary's int <-> unsigned int
  alias    (tried first) a symbol-key lever on one of the first temporaries / arguments: `int& x_r = x;` after its
           definition with the later uses renamed (ref), or one use through a by-reference identity helper (bind).
           A reference allocates a frame symbol early and shifts every SYM.key after it, which decides register
           ties (docs/c2/symkeys.md). On 442 register / copy / schedule near-misses (2026-11-26) the search matched
           47 (M&B 31 of 231, Spore 16 of 211), about half of them by an alias
Nothing proves a variant equivalent; a MATCH is retail's bytes, so it is the function. A DIFF that only
the search produced is a lead, not a lift. Prints one line per function; with --json, the winning text too.
"""
import argparse, json, multiprocessing as mp, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

STMT = re.compile(r"^(\s*)(.*;)\s*$")
CTRL = re.compile(r"^\s*(if|else|while|do|for|switch|case|default|return|goto|break|continue|L\w+:|\}|\{)")
ID = re.compile(r"\b(t\d+|v_l\d+|a\d+|self)\b")
CALL = re.compile(r"\bFUN_|->|\b__lift|\b(memcpy|memset|strlen|_Interlocked\w+|__rdtsc|sqrt|fabs|sin|cos|atan2?|"
                  r"exp|log|pow|fmod)\s*\(|\)\)\(|\(\*\(il2c|\bil2c_\w+\*")


def rank(res):
    return (res.get("status") == "MATCH", res.get("exact", -1), res.get("ins", -1),
            -abs(res.get("osz", 0) - res.get("size", 0)))


def exact(theirs, mine, va):
    """Aligned share of instructions with registers kept (canon_ins erases them: a register choice is a
    difference here), addresses masked."""
    import difflib, w8_diff
    f = lambda b: [re.sub(r"0x[0-9a-f]{5,}", "A", "%s %s" % (i.mnemonic, i.op_str)) for i in w8_diff.dis(b, va)]
    a, b = f(theirs), f(mine)
    same = sum(m.size for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return round(100.0 * same / max(1, len(a), len(b)), 2)


def stmt_info(s):
    """(defined name or None, names read, writes memory, reads memory, has a call) of one simple statement."""
    m = re.match(r"^\s*([A-Za-z_]\w*) = (.*);$", s)
    if m and ID.fullmatch(m.group(1)):
        lhs, rhs, store = m.group(1), m.group(2), False
    else:
        m = re.match(r"^\s*(\*.*?) = (.*);$", s)
        if not m:
            return None
        lhs, rhs, store = None, m.group(1) + " " + m.group(2), True
    return lhs, set(ID.findall(rhs)), store, "*(" in rhs or "g_" in rhs or store, bool(CALL.search(s))


def swaps(lines):
    for n in range(len(lines) - 1):
        a, b = lines[n], lines[n + 1]
        if CTRL.match(a) or CTRL.match(b) or not STMT.match(a) or not STMT.match(b):
            continue
        if len(a) - len(a.lstrip()) != len(b) - len(b.lstrip()):
            continue
        ia, ib = stmt_info(a), stmt_info(b)
        if not ia or not ib or ia[4] or ib[4]:
            continue
        if (ia[0] and (ia[0] in ib[1] or ia[0] == ib[0])) or (ib[0] and ib[0] in ia[1]):
            continue
        if (ia[2] and ib[3]) or (ib[2] and ia[3]):
            continue
        yield ("swap", n), lines[:n] + [b, a] + lines[n + 2:]


def top_split(e, ops):
    """Split e at its last top-level binary operator among ops: (left, op, right) or None."""
    depth, best = 0, None
    i = 0
    while i < len(e):
        c = e[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0:
            for op in ops:
                if e.startswith(" %s " % op, i):
                    best = (i, op)
                    break
        i += 1
    if not best:
        return None
    i, op = best
    return e[:i], op, e[i + len(op) + 2:]


FLIP = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "!=": "!="}


def commutes(lines):
    for n, l in enumerate(lines):
        m = re.match(r"^(\s*(?:if|while|\} while) \()(.*)(\)(?: goto \w+;| \{|;)?)\s*$", l)
        if m:
            sp = top_split(m.group(2), ["<=", ">=", "==", "!=", "<", ">"])
            if sp and "&&" not in m.group(2) and "||" not in m.group(2):
                a, op, b = sp
                yield ("cmp", n), lines[:n] + ["%s%s %s %s%s" % (m.group(1), b, FLIP[op], a, m.group(3))] + lines[n + 1:]
            continue
        m = re.match(r"^(\s*\S.* = \(\w[\w ]*\)\()(.*)(\);)$", l)
        if m:
            sp = top_split(m.group(2), ["+", "*", "&", "|", "^"])
            if sp and not sp[0].endswith(("<", ">", "=")):
                a, op, b = sp
                yield ("comm", n), lines[:n] + ["%s%s %s %s%s" % (m.group(1), b, op, a, m.group(3))] + lines[n + 1:]


DECL = re.compile(r"^    ((?:unsigned )?(?:int|char|short|bool|float|double|__int64)) (t\d+|v_l\d+)(\[\d+\])?;$")


def decls(lines):
    """Declaration order (cl numbers symbols in it, and register ties go by it) and a temp's signedness."""
    ds = [n for n, l in enumerate(lines) if DECL.match(l)]
    for x, y in zip(ds, ds[1:]):
        if y == x + 1:
            yield ("decl", x), lines[:x] + [lines[y], lines[x]] + lines[y + 1:]
    for n in ds:
        m = DECL.match(lines[n])
        if m.group(1) in ("int", "unsigned int") and not m.group(3) and m.group(2).startswith("t"):
            ty = "unsigned int" if m.group(1) == "int" else "int"
            yield ("sign", n), lines[:n] + ["    %s %s;" % (ty, m.group(2))] + lines[n + 1:]


BIND = "template<class HsT> __forceinline HsT& hs_bind(HsT& x) { HsT& r = x; return r; }\n"


def aliases(lines, first=4):
    """Symbol-key levers (docs/c2/symkeys.md: a reference allocates a frame symbol early and shifts every key after
    it, which decides register ties; rwsweep's `alias` / `wrap` hits on M&B 0040f270, 004428a0, Spore 00c5b530):
    for each of the first temporaries and arguments, `int& r = x;` after its definition with the later uses renamed
    (ref), or one use passed through a by-reference identity helper (bind)."""
    names = []
    for l in lines:
        for x in re.findall(r"\b(t\d{4,}|a\d+)\b", l):
            if x not in names:
                names.append(x)
    for x in names[:first]:
        d = next((n for n, l in enumerate(lines) if re.match(r"^\s+%s = " % x, l)), None)
        if d is not None and re.match(r"^    \S", lines[d]):          # a top-level definition: a ref after it
            later = [re.sub(r"\b%s\b" % x, x + "_r", l) for l in lines[d + 1:]]
            yield ("ref", x), lines[:d + 1] + ["    int& %s_r = %s;" % (x, x)] + later
        u = next((n for n, l in enumerate(lines) if re.search(r"\(%s\)" % x, l) and not re.match(r"^\s+%s = " % x, l)), None)
        if u is not None:
            yield ("bind", x), lines[:u] + [lines[u].replace("(%s)" % x, "(hs_bind(%s))" % x, 1)] + lines[u + 1:]


def variants(text, families=("swap", "cmp", "decl", "alias")):
    head, sep, body = text.partition("\n{\n")
    if not sep:
        return
    lines = body.split("\n")
    gens = []
    if "swap" in families:
        gens += list(swaps(lines))
    if "cmp" in families:
        gens += list(commutes(lines))
    if "decl" in families:
        gens += list(decls(lines))
    if "alias" in families:
        gens = list(aliases(lines)) + gens                          # cheapest, most often right: first
    for tag, vl in gens:
        h = head
        if tag[0] == "bind":
            i = h.rfind("\n", 0, h.rfind("FUN_"))                  # before the function's signature line
            h = h[:i + 1] + BIND + h[i + 1:]
        yield tag, h + sep + "\n".join(vl)


def score_text(text, path, head, r, tag):
    import corpus as corpusmod, lbench, w8_score
    out = os.path.join(os.path.dirname(HERE), "work", "scratch", "l2c_search")
    os.makedirs(out, exist_ok=True)
    src = os.path.join(out, "%s_%s.cpp" % (r["va"], tag))
    open(src, "w").write(text)
    obj, log = w8_score.compile_one((src, (r.get("flags") or head["flags"]) + " /Z7", "l2s_%s_%s_%s" % (
        head["corpus"], r["va"], tag)))
    if not obj:
        return dict(status="CL-ERROR", size=r["size"])
    img = corpusmod.Corpus(path).image()
    res = lbench.score(obj, r, img)
    res["size"] = r["size"]
    try:
        import w8_diff
        va = int(r["va"], 16)
        res["exact"] = exact(bytes(img.bytes_at(va, r["size"])), bytes(w8_diff.ours_bytes(obj, va)[0]), va)
    except Exception:
        pass
    return res


def search(args):
    path, head, r, rounds, budget, families = args
    os.environ["C2_BUILD"] = r.get("c2") or head["c2"]     # a row may name its own build (Spore's 13.10 units)
    import corpus as corpusmod, il2c_lift
    from w8_emit import Image
    try:
        text, _ = il2c_lift.unit(Image(head["image"]), int(r["va"], 16), r["size"], corpusmod.unpack(r["mask"]),
                                 r.get("flags") or head["flags"])
    except Exception as e:
        return dict(va=r["va"], status="UNSUPPORTED", why=str(e)[:100])
    best = score_text(text, path, head, r, "base")
    start = dict(best)
    steps, tried = [], 0
    for _ in range(rounds):
        if best.get("status") == "MATCH" or tried >= budget:
            break
        cand = None
        for k, (tag, vt) in enumerate(variants(text, families)):
            if tried >= budget:
                break
            tried += 1
            res = score_text(vt, path, head, r, "v%d" % k)
            if rank(res) > rank(best) and (cand is None or rank(res) > rank(cand[0])):
                cand = (res, vt, tag)
                if res.get("status") == "MATCH":
                    break
        if cand is None:
            break
        best, text = cand[0], cand[1]
        steps.append("%s@%s" % cand[2])
    return dict(va=r["va"], size=r["size"], start=start.get("status"), start_ins=start.get("ins"),
                status=best.get("status"), ins=best.get("ins"), osz=best.get("osz"),
                start_exact=start.get("exact"), exact=best.get("exact"), steps=steps, tried=tried,
                text=text if steps else None)


FAMILIES = ("swap", "cmp", "decl", "alias")


def main(argv):
    global FAMILIES
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("vas", nargs="*")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--budget", type=int, default=120, help="compiles per function")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--min-ins", type=float, default=0)
    ap.add_argument("--json")
    ap.add_argument("--families", default="alias,swap,cmp,decl")
    a = ap.parse_args(argv)
    FAMILIES = tuple(a.families.split(","))
    f = open(a.corpus)
    head = json.loads(f.readline())
    rows = {json.loads(l)["va"]: json.loads(l) for l in f}
    vas = list(a.vas)
    if a.frm:
        for l in open(a.frm):
            x = json.loads(l)
            if x.get("status") == "DIFF" and x.get("ins", 0) >= a.min_ins and x["va"] in rows:
                vas.append(x["va"])
    jobs = [(a.corpus, head, rows[v], a.rounds, a.budget, FAMILIES) for v in vas]   # (spawned workers: not a global)
    out = open(a.json, "w") if a.json else None
    won = 0
    builds = sorted({j[2].get("c2") or head["c2"] for j in jobs})
    def run_all():                                   # one pool per build: a worker keeps its first job's toolchain
        for b in builds:
            with mp.Pool(a.jobs) as pool:
                for res in pool.imap_unordered(search, [j for j in jobs if (j[2].get("c2") or head["c2"]) == b]):
                    yield res
    if True:
        for res in run_all():
            won += res.get("status") == "MATCH" and res.get("start") != "MATCH"
            print("%s %-6s -> %-6s exact %s -> %s (%d tried) %s" % (
                res["va"], res.get("start"), res.get("status"), res.get("start_exact"), res.get("exact"),
                res.get("tried", 0), " ".join(res.get("steps", []))),
                  flush=True)
            if out:
                out.write(json.dumps(res) + "\n")
                out.flush()
    print("%d of %d now MATCH" % (won, len(jobs)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
