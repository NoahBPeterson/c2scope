#!/usr/bin/env python3
"""What c2's list scheduler (sched.obj, Schedule@8) did to one function.  Doc: docs/c2/sched.md

usage: uv run tools/c2sched.py <src> --func SUBSTR [--keep DIR] [--all] [--verify [-v]] [-- <cl args>]

For every scheduling region (a run of at most 81 tuples ending at a branch/label/switch; regions with fewer than 2
instructions are skipped) it prints the instructions in their pre-schedule order, each with its DAG data, then the
order the scheduler issued them in:

  lex    position in the region (DAGNODE +0x3a; the entry pseudo-node is 0)
  ht     height: longest latency path to the region's exit (+0x38)
  ns     number of DAG successors (+0x26)
  fl     node flags (+0x40): L = reads memory (a SYM/INDIR source, bit 2), S = writes memory (bit 3),
         C = on the critical path (bit 0), B = bit 1
  prio   static priority (+0x2c) = (ht + ns) << 10 | L << 15 | B << 23 (+1 for an x87 store); sched.md rule 3
  rdy    cycle at which the node became ready (entered the ready list), est = earliest issue cycle (+0x34)
  cyc    issue cycle; '*' marks an instruction issued out of its original order

Only functions that Schedule runs on appear (not /Od, not SEH, not C++-EH functions unless -EHs: il_pipeline).
--all prints regions the scheduler left in order too. --verify instead checks sched.md rules 5-7 (height, static
priority, selection with the unit/busy model) on every region of the compile; -v lists the misses.
"""
import argparse, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace

NAME = "g10d123ac>0>0>4$"
IL = "g10d123ac>0>8>0>0x1c%"
ISSUE = "10d0fb60"          # _issueArray: DAGNODE* [3]
CCC = "g10d0fb4c>0"         # _SCHED_ccc: current cycle
HOOKS = [
    "@Schedule@8=%s,%s" % (NAME, IL),                                                    # 0 pre-schedule IL
    "@SchSchedGraph@4=" + CCC,                                                            # 1 region start
    "@SchAddToReadyList@4=ecx>0x1c,ecx>0x2c,ecx>0x38,ecx>0x24,ecx>0x40,ecx>0x34," + CCC + ",ecx>0x3c,ecx>0x1c>4",   # 2 node ready
    "@SchSelectInstrs@4=%s/xeax,%s,g%x>0>0x1c,g%x>0>0x1c,g%x>0>0x1c" % (
        CCC, CCC, int(ISSUE, 16), int(ISSUE, 16) + 4, int(ISSUE, 16) + 8),                # 3 issue group
    "@MdmSuperHack@4=%s,%s" % (NAME, IL),                                                 # 4 post-schedule IL
    "@SchComputeStaticPriority@4+0x40=esi>0x1c,eax>0xc>0x1c,eax>0x14,eax>0x10,esi>0x3c",  # 5 one DAG edge
]


def parse(log, funcpat):
    """-> [(funcname, pre IL lines, post IL lines, regions)] ; region = dict(nodes={tuple: info}, issue=[(cyc, [t..])])"""
    funcs, cur, il_target, region, pend_edges = [], None, None, None, None
    for ln in log.splitlines():
        if ln.startswith("T "):
            if il_target is not None:
                il_target.append(ln[2:])
            continue
        if "|" not in ln or ln.startswith("#"):
            continue
        head, _, rest = ln.partition("|")
        f = head.split()
        ev, hk = f[0], int(f[3])
        v = c2trace._split(rest)
        il_target = None
        if hk == 0 and ev == "E":
            name = v[0].strip('"')
            cur = (name, [], [], []) if funcpat in name else None
            if cur:
                funcs.append(cur)
                il_target = cur[1]
        elif hk == 4 and ev == "E":
            if cur and cur[0] == v[0].strip('"'):
                il_target = cur[2]
            cur = None
        elif cur is None:
            continue
        elif hk == 5:
            if pend_edges is None:
                pend_edges = []
            a, b, lat, typ, plat = [int(x, 16) if x != "?" else 0 for x in v[:5]]
            pend_edges.append((a, b, lat & 0xffff, typ, plat & 0xff, lat >> 16 & 0xff))
        elif hk == 1:
            region = dict(nodes={}, issue=[], ready=[], events=[], edges=pend_edges or [])
            pend_edges = None
            cur[3].append(region)
        elif hk == 2 and region is not None:
            t, prio, hl, pn, fl, est, cyc, lu, op = [int(x, 16) if x != "?" else 0 for x in v[:9]]
            region["nodes"][t] = dict(prio=prio, ht=hl & 0xffff, lex=hl >> 16, ns=pn >> 16, np=pn & 0xffff,
                                      fl=fl & 0xff, est=est, rdy=cyc, lat=lu & 0xff, unit=lu >> 16, op=op)
            region["ready"].append(t)
            region["events"].append(("add", t))
        elif hk == 3 and ev == "E" and region is not None:
            region["events"].append(("sel", int(v[0], 16)))
        elif hk == 3 and ev == "X" and region is not None:
            n = int(v[0], 16)
            cyc = int(v[1], 16)
            region["issue"].append((cyc, [int(x, 16) for x in v[2:2 + n]]))
            region["events"].append(("iss", cyc, [int(x, 16) for x in v[2:2 + n]]))
    return funcs


def instr_text(il):
    out = {}
    for l in il:
        if l.startswith("I "):
            p = l.split(None, 2)
            out[int(p[1], 16)] = p[2]
    return out


def flags(fl):
    return "".join(c for b, c in ((2, "L"), (3, "S"), (0, "C"), (1, "B")) if fl >> b & 1) or "-"


def short(text):
    op, _, ops = text.partition("|")
    return "%-7s %s" % (op.split()[0], ops.strip())


def verify(funcs, verbose=False):
    """Check sched.md rules 5 (height), 6 (static priority) and 7 (selection) against every scheduled region."""
    np_ = nbad_p = nsel = nbad_sel = nbad_noest = 0
    for name, pre, post, regions in funcs:
        txt = instr_text(pre)
        for k, r in enumerate(regions):
            N = r["nodes"]
            for t, n in N.items():
                if not t:
                    continue
                L, B = n["fl"] >> 2 & 1, n["fl"] >> 1 & 1
                want = ((n["ht"] + n["ns"]) << 10) + (L << 15) + (B << 23)
                np_ += 1
                if n["prio"] not in (want, want + 1):
                    nbad_p += 1
                    if verbose:
                        print("PRIO %s r%d lex %d: %x want %x  %s" % (name, k, n["lex"], n["prio"], want,
                                                                      short(txt.get(t, "?"))))
            ready, issued, cyc = [], set(), None
            for e in r["events"]:
                if e[0] == "add":
                    ready.append(e[1])
                elif e[0] == "sel":
                    cyc, snap = e[1], [t for t in ready if t not in issued]
                else:
                    ts = [t for t in e[2] if t]
                    if ts and cyc is not None:
                        cand = [t for t in snap if t and N[t]["est"] <= cyc]
                        key = lambda t: (-N[t]["prio"], N[t]["lex"])
                        nsel += 1
                        if cand and min(cand, key=key) != ts[0]:
                            nbad_sel += 1
                            if verbose:
                                print("SEL  %s r%d cyc %d: issued lex %d (prio %x), rule says lex %d (prio %x)" % (
                                    name, k, cyc, N[ts[0]]["lex"], N[ts[0]]["prio"], N[min(cand, key=key)]["lex"],
                                    N[min(cand, key=key)]["prio"]))
                        c2 = [t for t in snap if t]
                        if c2 and min(c2, key=key) != ts[0]:
                            nbad_noest += 1
                    issued.update(e[2])
    nh = nbad_h = 0
    from collections import Counter
    lat_by_type = Counter()
    for name, pre, post, regions in funcs:
        txt = instr_text(pre)
        for k, r in enumerate(regions):
            N = r["nodes"]
            succ = {}
            for a, b, lat, typ, plat, eb in r["edges"]:
                succ.setdefault(a, []).append((b, lat))
                lat_by_type[("type %x" % typ, "0" if lat == 0 else "=prod" if lat == plat else
                             "prod%+d" % (lat - plat))] += 1
            for t, n in N.items():
                if not t or t not in succ:
                    continue
                want = 1 + max((N[b]["ht"] if b in N else 0) + lat for b, lat in succ[t])
                nh += 1
                if want != n["ht"]:
                    nbad_h += 1
                    if verbose:
                        print("HT   %s r%d lex %d: ht %d want %d  %s" % (name, k, n["lex"], n["ht"], want,
                                                                    short(txt.get(t, "?"))))
    print("rule 5 (height = 1 + max(succ height + edge latency)): %d of %d nodes match" % (nh - nbad_h, nh))
    print("edge latency by dep type (0 / = producer latency / other):")
    for ty in sorted({a for a, b in lat_by_type}):
        print("   %-12s %s" % (ty, "  ".join("%s:%d" % (b, c) for (a, b), c in sorted(lat_by_type.items()) if a == ty)))
    print("rule 6 (static priority): %d of %d nodes match" % (np_ - nbad_p, np_))
    print("rule 7 with the unit/busy model (simulate): %d of %d picks match" % simulate_check(funcs, verbose))
    print("rule 7, simplified (pick max (prio, -lex) among ready with est <= cycle): %d of %d picks match" % (
        nsel - nbad_sel, nsel))
    print("        (ignoring est, i.e. latency: %d of %d)" % (nsel - nbad_noest, nsel))


_COST = None


def op_cost(op):
    """OpSchedInfoP5[op] = (latency, cost, unit) (c2 0x10cdd328, 12-byte rows)."""
    global _COST
    if _COST is None:
        import struct
        from image import Image
        im = Image("c2")
        _COST = [struct.unpack("<iii", im.read(0x10cdd328 + 12 * k, 12)) for k in range(0x273)]
    return _COST[op] if op < 0x273 else (0, 0, 0x300)


def simulate_check(funcs, verbose=False):
    """Replay each region's picks with SchSelectInstrs' unit model: 4 units, each takes one instruction per
    cycle while its busy counter is below 3; an issue adds the op's cost to its unit's counter, every cycle
    subtracts 1; a node is eligible once its est <= cycle + max(busy, 0). Units >= 4 mean 'any' (unit 0)."""
    n = bad = 0
    for name, pre, post, regions in funcs:
        for k, r in enumerate(regions):
            N = r["nodes"]
            busy = [0, 0, 0, 0]
            ready, issued, cyc, last = [], set(), None, None
            for e in r["events"]:
                if e[0] == "add":
                    ready.append(e[1])
                elif e[0] == "sel":
                    if last is not None:
                        for c in range(last, e[1]):
                            busy = [b - 1 for b in busy]
                    cyc, last, snap = e[1], e[1], [t for t in ready if t not in issued]
                else:
                    used = set()
                    for j, t in enumerate(x for x in e[2] if x):
                        pool = snap if j == 0 else [x for x in ready if x not in issued]

                        def unit_ok(x):
                            u = N[x]["unit"] & 0xff
                            if u >= 4:
                                return 0 if all(b < 3 for b in busy) else None
                            return u if u not in used and busy[u] < 3 else None

                        cand = [x for x in pool if x and unit_ok(x) is not None and
                                N[x]["est"] <= cyc + max(busy[unit_ok(x)], 0)]
                        n += 1
                        best = min(cand, key=lambda x: (-N[x]["prio"], N[x]["lex"])) if cand else None
                        if best != t:
                            bad += 1
                            if verbose:
                                print("SIM  %s r%d cyc %d: issued lex %d (prio %x), model says %s" % (
                                    name, k, cyc, N[t]["lex"], N[t]["prio"],
                                    "lex %d (prio %x)" % (N[best]["lex"], N[best]["prio"]) if best else "nothing"))
                        u = unit_ok(t)
                        u = 0 if u is None else u
                        used.add(u)
                        busy[u] = max(busy[u], 0) + op_cost(N[t]["op"])[1]
                        issued.add(t)
                    issued.update(e[2])
    return n - bad, n


def report(funcs, show_all=False):
    for name, pre, post, regions in funcs:
        print("== %s: %d regions scheduled" % (name, len(regions)))
        txt = instr_text(pre)
        for k, r in enumerate(regions):
            order = sorted((n for t, n in r["nodes"].items() if t), key=lambda n: n["lex"])
            tups = sorted((t for t in r["nodes"] if t), key=lambda t: r["nodes"][t]["lex"])
            issued = [(c, t) for c, ts in r["issue"] for t in ts if t]
            moved = [t for (c, t), u in zip(issued, tups) if t != u]
            if not moved and not show_all:
                continue
            print("-- region %d (%d instrs)%s" % (k, len(tups), "" if moved else "  [unchanged]"))
            print("   %3s %3s %3s %-4s %7s %3s %3s  %s" % ("lex", "ht", "ns", "fl", "prio", "rdy", "est", "instr"))
            for t in tups:
                n = r["nodes"][t]
                print("   %3d %3d %3d %-4s %7x %3d %3d  %s" % (n["lex"], n["ht"], n["ns"], flags(n["fl"]), n["prio"],
                                                            n["rdy"], n["est"], short(txt.get(t, "?%x" % t))))
            print("   issued:")
            for (c, t), u in zip(issued, tups):
                n = r["nodes"][t]
                print("   cyc %2d %s lex %2d  %s" % (c, "*" if t != u else " ", n["lex"], short(txt.get(t, "?%x" % t))))


def main(argv):
    ap = argparse.ArgumentParser(prog="c2sched.py", usage=__doc__)
    ap.add_argument("--func", required=True)
    ap.add_argument("--keep")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--verify", action="store_true", help="check the priority and selection rules; -v to list misses")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("src")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    img, hk, rc, out, log = c2trace.run_trace(a.src, HOOKS, cl, keep=a.keep)
    if rc:
        sys.stdout.write(out)
    funcs = parse(log, a.func)
    if not funcs:
        sys.exit("function %r never reached Schedule (not optimised, SEH, or C++ EH without -EHs?)" % a.func)
    if a.verify:
        verify(funcs, a.v)
    else:
        report(funcs, a.all)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
