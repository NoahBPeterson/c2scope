#!/usr/bin/env python3
"""Callee-saved register roles -> definition order / priority constraints.  Doc: docs/c2/invert.md section 3.

Forward rule (regalloc.md sections 0, 3.4, 4.2, 5; [C], replayed by rasim): globregs are coloured in priority order
(Spillcosts: prio = sum 2*refs*P*w - lived-through P*w), equal priorities by defOrd descending (within a block the
value defined first, across blocks the one defined in the later block); ChoosePhysReg gives each the first free
register of eax ecx edx esi edi ebx ebp with the lowest weight (0 without preferences or neighbour penalties), and a
web live across a call has only esi edi ebx ebp.  So among interfering call-crossing webs with zero weights the
k-th one coloured gets the k-th free callee-saved register.

Inverse, from the original's register of each web (regmap) and one model state of our compile (a `regs --export`
snapshot; rasim's what-ifs are the derivative used, no compile search):
  1. Every pair of competing webs our compile gives each other's registers (a swap) or that take callee-saved
     registers in the wrong order is a required colouring order `u before v` (u wants the earlier register).
  2. prio(u) = prio(v): only defOrd decides, so the source must DEFINE u before v (same block) or define u in a
     later block: a statement / declaration-with-initialiser order fact (`def` constraint).
  3. prio(u) < prio(v): definition order cannot do it; the gap is a priority difference (references x P x w):
     a `prio` constraint with the gap, for `rasim need before:u:v` / realize's constructs (more uses, a scope).
  4. Each answer is checked with the model: `rasim regs SNAP before:u:v ...` must give every wanted register.

usage: regorder.py solve SNAP.json VAR=REG ...         constraints and the model's check
       regorder.py measure [--limit N] [--jobs J]      on Spore's byte-exact /O2 functions: how often definition
                                                       order (a priority tie) decides a callee-saved register
"""
import os, re, subprocess, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RASIM = os.path.join(HERE, "rasim", "target", "release", "rasim")
CSAVED = ["esi", "edi", "ebx", "ebp"]
POP = re.compile(r"^\s+(\d+)\s+(\S+?)(?:#\w+)?\s+prio\s+(-?\d+)\s+def\s+(\d+)")


def rasim(*args):
    return subprocess.run([RASIM] + list(args), capture_output=True, text=True).stdout


def order(snap, *edits):
    """[(pop, var, prio, defOrd)] of the first pass."""
    out = []
    for ln in rasim("order", snap, *edits).splitlines():
        m = POP.match(ln)
        if m:
            out.append((int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))))
    return out


def regs(snap, *edits):
    """{var: register} per rasim's ChoosePhysReg model (the first colouring of each name)."""
    out = {}
    for ln in rasim("regs", snap, *edits).splitlines():
        m = re.match(r"^\s+\d+\s+(\S+?)(?:#\w+|@p\d+)?\s+(\w+)\s+\w+\s", ln)
        if m and m.group(1) not in out and m.group(1) != "var":
            out[m.group(1)] = m.group(2)
    return out


def constraints(pops, ours, want):
    """Required colouring orders from the wanted callee-saved registers, each classified def / prio."""
    info = {v: (p, pr, d) for p, v, pr, d in pops}
    cs = [v for v in want if want[v] in CSAVED and v in info]
    out = []
    for u in cs:
        for v in cs:
            if u == v or CSAVED.index(want[u]) >= CSAVED.index(want[v]):
                continue
            swap = ours.get(u) == want[v] and ours.get(v) == want[u]
            wrong = info[u][0] > info[v][0] and ours.get(u) in CSAVED and ours.get(v) in CSAVED and \
                CSAVED.index(ours[u]) > CSAVED.index(ours[v])
            if not (swap or wrong):
                continue
            pu, pv = info[u][1], info[v][1]
            if pu == pv:
                kind = "def"
                why = "equal priority %d: define %s before %s (same block; across blocks %s's definition in the " \
                      "later block)" % (pu, u, v, u)
            elif pu > pv:
                kind = "ok-prio"
                why = "priority %d > %d already orders %s first: the register comes from elsewhere (an " \
                      "interference, a preference)" % (pu, pv, u)
            else:
                kind = "prio"
                why = "priority %d < %d: definition order cannot; %s needs %d more priority (references x P x w) " \
                      "or %s less" % (pu, pv, u, pv - pu, v)
            out.append(dict(u=u, v=v, kind=kind, why=why))
    return out


def solve(snap, want):
    pops = order(snap)
    ours = regs(snap)
    cons = constraints(pops, ours, want)
    edits = ["before:%s:%s" % (c["u"], c["v"]) for c in cons if c["kind"] in ("def", "prio")]
    after = regs(snap, *edits) if edits else ours
    ok = all(after.get(v) == r for v, r in want.items())
    return dict(ours=ours, cons=cons, edits=edits, after=after, model_ok=ok)


def _measure_one(row):
    import shlex, tempfile
    src, sym, va, flags = row
    spore = os.path.join(HERE, "..", "..", "spore-decomp")
    cl = shlex.split(flags) + ["/I" + os.path.join(spore, "match", "include"), "/I" + os.path.dirname(src)]
    r = subprocess.run([sys.executable, os.path.join(HERE, "c2trace.py"), "regs", src, "--func", sym, "--"] + cl,
                       capture_output=True, text=True, env=dict(os.environ, C2_BUILD="15.00"))
    rows = []
    for ln in r.stdout.splitlines():
        m = re.match(r"^\s+(\d+)\s+(\S+)\s+\w+\s+(-?\d+)\s+-?\d+\s+\d+\s+(\d+)\s+(\S+)", ln)
        if m:
            rows.append((int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4)), m.group(5)))
        elif rows and ln.startswith("?"):
            break                                      # the first function's report only
    return row, rows


def measure(limit=60, jobs=6):
    import random
    from concurrent.futures import ThreadPoolExecutor
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import operands
    rows = operands.spore_o2_rows()
    rows = sorted(random.Random(11).sample(rows, min(limit, len(rows))), key=lambda r: r[2])
    with ThreadPoolExecutor(jobs) as ex:
        got = list(ex.map(_measure_one, rows))
    nf = pairs = ties = 0
    tie_rule = 0
    for row, rs in got:
        if not rs:
            continue
        nf += 1
        cs = [r for r in rs if r[4] in CSAVED]
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                a, b = cs[i], cs[j]                     # a popped before b
                pairs += 1
                if a[2] == b[2]:
                    ties += 1
                    tie_rule += a[3] > b[3] or a[3] == b[3]
    print("Spore byte-exact /O2 functions with a regs report: %d; pairs of callee-saved webs: %d; equal priority "
          "(definition order decides): %d (%.1f%%); of those the larger defOrd was coloured first: %d" %
          (nf, pairs, ties, 100.0 * ties / max(1, pairs), tie_rule))
    return got


def main(argv):
    if not argv or argv[0] not in ("solve", "measure"):
        print(__doc__)
        return 1
    if argv[0] == "measure":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--limit", type=int, default=60)
        ap.add_argument("--jobs", type=int, default=6)
        a = ap.parse_args(argv[1:])
        measure(a.limit, a.jobs)
        return 0
    snap, want = argv[1], dict(x.split("=", 1) for x in argv[2:])
    r = solve(snap, want)
    print("ours:   " + " ".join("%s=%s" % kv for kv in sorted(r["ours"].items())))
    for c in r["cons"]:
        print("  %-7s %s before %s: %s" % (c["kind"], c["u"], c["v"], c["why"]))
    print("model with %s: %s -> %s" % (" ".join(r["edits"]) or "no edit",
                                       " ".join("%s=%s" % kv for kv in sorted(r["after"].items())),
                                       "every wanted register" if r["model_ok"] else "NOT all wanted registers"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
