#!/usr/bin/env python3
"""The x64 scheduler of cl 17.00 (x86_amd64 c2.dll), traced: in which order and why each region's instructions
were issued.  Doc: docs/c2/sched17.md (the map: work/port1700/sched_map.md).

usage: c2sched17.py SRC --func F [--ready] [--check] [--predict] [--il] [--raw] [-- <cl args>]     (--c2 17.00)
       c2sched17.py EXPORT.json [--predict]          (an --export file: no compile)
  --check: replay every region with the model (tools/c2sched17.py simulate) against the traced issue order
  --predict: build each region's DAG, latencies, units and static priorities from its IL (predict_dag; the IL is
            logged at DAG construction, --il) and measure them and the schedule against the trace
            (docs/c2/sched17.md 6.x); --il with --export keeps the IL for offline --predict

One pass after register allocation (dop2_PreEncode -> Schedule), K8 model at /O2, the Atom model with /favor:ATOM
(MOD+0x398 bit 6, logged per function; sched17.md 7: 2 issues, dyn = static + 2^25 for a port pair). Per region (a DAG of up to 81
tuples ending in a branch or label), the report lists each issued instruction:

  cyc  slot  #lex  instruction            static = B L nsucc height C S   dyn   est  lat unit
static priority = B<<23 + L<<15 + nsucc<<10 + height<<5 + C<<1 + S (B: sets the final branch's flags, L: reads
memory, C: on the critical path, S: writes memory); dyn = static + 8 when a decoder pair formed this cycle.
Within a cycle, nodes go by dyn descending, then by lexical index; a node is skipped (and a lower one issued)
when its unit is full, a macro-fusible compare is held for its jcc, or its earliest cycle (est) is not reached.
--ready also prints the ready list (lexical index:dyn) at the start of each cycle.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2build   # noqa: E402
import c2trace   # noqa: E402

FN, REGION, SEL, READY, GRAPH, NODE, PASI, MEMB, KILL = range(9)
OPNAME = "100226e8"


def hooks(ready, il=False):
    h = [
        "@Schedule@8=tdccc>0>0>4$,t850>0>0x398",                     # 0 a function; MOD+0x398 (bit 6: Atom)
        "100e5440=ecx",                                              # 1 SchSchedGraph: a region (graph)
        # 2 a node issued: name, lexical index | height, static, dyn, est, cycle, unit (local), latency, flags
        "100e56fb=edi>0x1c>8~%s$,edi>0x38,edi>0x2c,edi>0x30,edi>0x34,ebp>-4,ebp>-0xc,edi>0x3c,edi>0x40,"
        "edi>0x24,edi>0x1c>0x20,edi,edi>0xc*24:0xc:0x14,edi>0x3e" % OPNAME +  # + node, successor edges (to, lat), unit
        ",edi>0x1c>8,edi>0x1c>0x30*8:0xc:0xc:4,edi>0x1c>0x34*8:0xc:0xc:4"    # opcode; src / dst leaf kinds (decoder)
        # 17..20, for the Atom model's port / throughput callbacks (sched17.md 7): the tuple's type word, (+0x10
        # type, +0x28 value) and (+0x2c, +8) of its first four sources, (+0x10, +0x28) of its first two destinations
        ",edi>0x1c>0x10,edi>0x1c>0x30*4:0x10:0x28:4,edi>0x1c>0x30*4:0x2c:8:4,edi>0x1c>0x34*2:0x10:0x28:4",
    ]
    if il:      # every successor edge (a region has at most 82 nodes; the default probe logs 24), and their types
        h[2] = h[2].replace("edi>0xc*24:0xc:0x14", "edi>0xc*90:0xc:0x14") + ",edi>0xc*90:0xc:0x10"
    # 3 the ready list after the per-cycle re-sort: (lexical index | latency, dyn) per node, next at +0x10 (also
    # with il, which keeps the ids of 4 and 5 fixed)
    h.append("100e5649=esi>0x11418*40:0x3a:0x30:0x10,esi>0x11444" if ready or il else None)
    if il:
        # 4 DagBuildGraph entry: first tuple, tail tuple; 5 each tuple node as created (0x100e2bfa: edi = tuple,
        # ebx = node, unit / latency set): opcode, kind|flags|cc dword (+0xc), type word, node, lexical index,
        # (kind, +0x1c flags) of each src / dst leaf, then the tuple itself as an IL line ('!')
        h.append("100e29e0=edx,s1")
        # + (+0x24, +0x28) of each src / dst leaf (a memory leaf's symbol and PAS, a memory-set leaf's PAS, a
        # variable leaf's SYM), and the first 11 words of the SYM of the first src / dst leaves (+8 kind / flags,
        # +0xc root, +0x24 size, +0x28 offset)
        h.append("100e2bfa=edi>8,edi>0xc,edi>0x10,ebx,ebx>0x3a,edi>0x30*8:0xc:0x1c:4,edi>0x34*8:0xc:0x1c:4,edi!,"
                 "edi>0x30*8:0x24:0x28:4,edi>0x34*8:0x24:0x28:4,edi>0x30>0x24#11,edi>0x30>4>0x24#11,"
                 "edi>0x30>4>4>0x24#11,edi>0x34>0x24#11,edi>0x34>4>0x24#11,"
                 # 15, 16: (+0x2c, +8) of each src / dst leaf: a memory leaf's symbol part and address form
                 # (SchmdDepIsBreakable's tests; the IL line prints neither)
                 "edi>0x30*8:0x2c:8:4,edi>0x34*8:0x2c:8:4")
        # 6, 7: the alias facts the DAG builder asks for (ItfPasInterf(pas, pas), ItfIsMemberOfPas(sym, pas)):
        # kept per region from the scheduler's own DagBuildGraph call (sched17.md 6.x)
        h.append("100de4f0=ecx,edx/xeax")
        h.append("100de090=ecx,edx/xeax")
        # 8: a register-set destination (kind 10, a call's clobbers) as DagBuildGraph walks it (0x100e3918):
        # node's lexical index, the sparse bit vector's chunks (base, low dword) and (base, high dword)
        h.append("100e3918=ebp>-8>0x3a,ebp>-0x14>0x24>0*8:0:4:0xc,ebp>-0x14>0x24>0*8:0:8:0xc")
    return [x for x in h if x]


def kinds(t):
    """Leaf kind bytes of an operand list ('*N:0xc:0xc:4' logs each kind twice)."""
    if not t.startswith("["):
        return []
    x = [int(w, 16) & 0xff for w in t.strip("[]").split()]
    return x[::2]


_K8 = None


def k8_decoder(opc, srck, dstk):
    """SchmdDagDecoder on K8 (sched17.md 2.4): OPSCHEDINFO K8 row +4, two bits: shifted by 2 when a source leaf is
    memory (kind 5 / 6), by 4 when a destination leaf is, else 0; bit 6 (a callback) -> None."""
    global _K8
    if _K8 is None:
        import struct, pefile
        pe = pefile.PE(c2build.orig_c2(), fast_load=True)
        img, base = pe.get_memory_mapped_image(), pe.OPTIONAL_HEADER.ImageBase
        va = int(c2build.get("opsched_k8", "10012788"), 16)
        _K8 = [struct.unpack_from("<III", img, va - base + 12 * i) for i in range(0x4f5)]
    if opc is None or not 0 < opc < len(_K8):
        return 1
    d = _K8[opc][1]
    if d & 0x40:
        return None
    sh = 2 if any(k in (5, 6) for k in srck) else 4 if any(k in (5, 6) for k in dstk) else 0
    return d >> sh & 3


def leafwords(t):
    """'[w1 w2 w1 w2 ..]' of a '*N:0xc:0x1c:4' operand walker -> [(kind, +0x1c word), ...]."""
    if not t.startswith("["):
        return []
    x = [int(w, 16) for w in t.strip("[]").split()]
    return [(x[k] & 0xff, x[k + 1]) for k in range(0, len(x) - 1, 2)]


def pairs(t):
    """'[w1 w2 w1 w2 ..]' -> [[w1, w2], ...]."""
    x = [int(w, 16) for w in t.strip("[]").split()] if t.startswith("[") else []
    return [x[k:k + 2] for k in range(0, len(x) - 1, 2)]


def parse(text):
    fns, cur, reg, th = [], {}, {}, 0
    graph, lastnode, stack = {}, {}, {}      # per thread: the DAG being built (DagBuildGraph, also run by head/tail merging)
    for ln in text.splitlines():
        if ln.startswith("# thread "):
            th = int(ln.split()[2])
            continue
        if ln.startswith("T ") and th in lastnode:
            f = ln.split(None, 2)
            if f[1] == "I":
                lastnode[th]["il"] = ln[2:]
            elif f[1] == "L":
                lastnode[th]["il"] = "L"
            continue
        if ln.startswith("X ") and "|" in ln:
            head, _, rest = ln.partition("|")
            kind = int(head.split()[3])
            if kind in (PASI, MEMB) and stack.get(th):
                k2, a, b = stack[th].pop()
                if th in graph and k2 == kind:
                    graph[th]["pas" if kind == PASI else "memb"].append((a, b, int(c2trace._split(rest)[0], 16)))
            continue
        if not ln.startswith("E ") or "|" not in ln:
            continue
        lastnode.pop(th, None)
        head, _, rest = ln.partition("|")
        kind, v = int(head.split()[3]), c2trace._split(rest)
        if kind in (PASI, MEMB):
            stack.setdefault(th, []).append((kind, int(v[0], 16), int(v[1], 16)))
            continue
        if kind == KILL and th in graph:
            lo, hi = pairs(v[1]), pairs(v[2])
            regs = [b + i for (b, w), (_, w2) in zip(lo, hi) for i in range(64) if (w | w2 << 32) >> i & 1]
            graph[th]["kill"].append((int(v[0], 16) & 0xffff, regs))
            continue
        if kind == GRAPH:
            graph[th] = dict(start=int(v[0], 16), tail=int(v[1], 16), nodes=[], pas=[], memb=[], kill=[])
            continue
        if kind == NODE and th in graph:
            hx = lambda x: int(x, 16)
            kd = hx(v[1])
            hq = lambda x: int(x, 16) if x not in ("?", "") else None
            lastnode[th] = dict(opc=hx(v[0]), kind=kd & 0xff, tflags=kd >> 8 & 0xff, cc=kd >> 24 & 0x1f,
                                type=hx(v[2]) & 0xffff, node=hx(v[3]), lex=hx(v[4]) & 0xffff,
                                src=leafwords(v[5]), dst=leafwords(v[6]), il=None)
            if len(v) > 9:     # leaf +0x24 / +0x28 words; SYM kind / flags of src 0..2, dst 0..1
                lastnode[th]["src2"] = pairs(v[8])
                lastnode[th]["dst2"] = pairs(v[9])
                lastnode[th]["symw"] = [[hq(w) for w in x.strip("[]").split()] if x.startswith("[") else None
                                        for x in v[10:15]]
            if len(v) > 16 and v[15].startswith("["):     # leaf (+0x2c, +8) words: a memory leaf's form
                lastnode[th]["src3"] = pairs(v[15])
                lastnode[th]["dst3"] = pairs(v[16])
            graph[th]["nodes"].append(lastnode[th])
            continue
        if kind == FN:
            cur[th] = dict(func=v[0].strip('"'), regions=[])
            if len(v) > 1 and v[1] not in ("?", ""):     # MOD+0x398 bit 6: the Atom model (SchmdInitialize)
                cur[th]["model"] = "atom" if int(v[1], 16) & 0x40 else "k8"
            fns.append(cur[th])
        elif th in cur and kind == REGION:
            reg[th] = dict(sel=[], ready=[])
            if th in graph:      # the IL of this region's DAG (the graph SchSchedGraph is given was built last)
                g = graph.pop(th)
                reg[th]["nodes"] = g["nodes"]
                reg[th]["alias"] = dict(pas=sorted(set(g["pas"])), memb=sorted(set(g["memb"])))
                reg[th]["kill"] = {str(lx): rs for lx, rs in g["kill"]}
            cur[th]["regions"].append(reg[th])
        elif th in reg and kind == SEL:
            hx = lambda x: int(x, 16) if x not in ("?", "") else None
            hi = hx(v[1])
            reg[th]["sel"].append(dict(op=v[0].strip('"'), height=hi & 0xffff, lex=hi >> 16, static=hx(v[2]),
                                       dyn=hx(v[3]), est=hx(v[4]), cycle=hx(v[5]), unit=hx(v[6]),
                                       lat=hx(v[7]) & 0xff, flags=hx(v[8]) & 0xff, line=hx(v[10]) & 0xffff,
                                       node=hx(v[11]) if len(v) > 11 else None,
                                       succ=[(int(a, 16), int(b, 16)) for a, b in
                                             zip(*[iter(v[12].strip("[]").split())] * 2)] if len(v) > 12 and
                                       v[12].startswith("[") else [],
                                       u=(hx(v[13]) & 0xffff) if len(v) > 13 and hx(v[13]) is not None else None,
                                       opc=hx(v[14]) if len(v) > 14 else None,
                                       srck=kinds(v[15]) if len(v) > 15 else [], dstk=kinds(v[16]) if len(v) > 16 else []))
            x = reg[th]["sel"][-1]
            if len(v) > 20 and v[18].startswith("["):    # tuple type word, leaf words (the Atom callbacks)
                x["ttype"] = hx(v[17])
                a, b = pairs(v[18]), pairs(v[19])
                x["srcw"] = [p + q for p, q in zip(a, b)]          # (+0x10, +0x28, +0x2c, +8) per source
                x["dstw"] = pairs(v[20])                            # (+0x10, +0x28) per destination
            et = 21 if len(v) > 21 else 17
            if len(v) > et and v[et].startswith("[") and et == len(v) - 1:   # --il: successor edge types
                x["etype"] = [int(b, 16) for a, b in zip(*[iter(v[et].strip("[]").split())] * 2)]
        elif th in reg and kind == READY:
            x = [int(t, 16) for t in v[0].strip("[]").split()] if v and v[0].startswith("[") else []
            reg[th]["ready"].append([((x[2 * k] & 0xffff), x[2 * k + 1]) for k in range(len(x) // 2)])
    return fns


def fields(p, flags):
    return "%d %d %d %d %d %d" % (p >> 23 & 1, p >> 15 & 1, p >> 10 & 0x1f, p >> 5 & 0x1f, p >> 1 & 1, p & 1)


def report(fns, show_ready=False):
    out = []
    for f in fns:
        regs = [r for r in f["regions"] if r["sel"]]
        if not regs:
            continue
        out.append("## %s: %d regions scheduled" % (f["func"], len(regs)))
        for k, r in enumerate(regs):
            out.append("region %d (%d instructions)" % (k, len(r["sel"])))
            out.append("  cyc  #lex  instruction   line  B L ns ht C S        dyn  est lat unit")
            cyc, slot = None, 0
            ready = iter(r["ready"])
            for s in r["sel"]:
                if s["cycle"] != cyc:
                    cyc, slot = s["cycle"], 0
                    if show_ready:
                        rl = next(ready, None)
                        if rl:
                            out.append("       ready: " + " ".join("#%d:%x" % (i, d) for i, d in rl))
                bonus = s["dyn"] - s["static"] if s["dyn"] is not None and s["static"] is not None else 0
                out.append("  %3s  #%-3d %-13s %4d  %-12s %8x%s %4s %3d %4s" % (
                    cyc if slot == 0 else "", s["lex"], s["op"], s["line"], fields(s["static"], s["flags"]),
                    s["dyn"], " +%d" % bonus if bonus else "   ", s["est"], s["lat"], s["unit"]))
                slot += 1
    return "\n".join(out) + "\n"


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="c2sched17.py", usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func", default="")
    ap.add_argument("--ready", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--check", action="store_true", help="the replay model against the traced issue order")
    ap.add_argument("--export", help="the parsed regions as JSON (decoder classes included): `check` without c2")
    ap.add_argument("--il", action="store_true", help="also log each region's IL (DAG construction) and edge types")
    ap.add_argument("--predict", action="store_true",
                    help="build each region's DAG and static priorities from its IL (predict_dag) and measure them "
                         "and the schedule against the trace")
    ap.add_argument("--keep")
    argv, cl = c2trace.split_cl(argv)
    a = ap.parse_args(argv)
    if a.src.endswith(".json"):       # an export: no compile
        fns = load_export(a.src)
        if a.predict:
            predict_report(fns)
        else:
            check(fns)
        return 0
    if c2build.get("arch") != "x64":
        sys.exit("c2sched17: the x64 scheduler of cl 17.00: run with --c2 17.00 (x86 builds: c2trace.py sched)")
    if a.il or a.predict:   # memory leaves print their points-at-set index (leaf +0x28; ItfPasInterf's input)
        os.environ.setdefault("C2TRACE_ILPAS", "28")
    img, hk, rc, out, text = c2trace.run_trace(a.src, hooks(a.ready, a.il or a.predict), cl or ["/O2"], keep=a.keep, func=a.func)
    if rc:
        sys.stdout.write(out)
        return rc
    if a.raw:
        sys.stdout.write(text)
        return 0
    if a.export:
        import json
        fns = parse(text)
        for f in fns:
            for r in f["regions"]:
                for x in r["sel"]:
                    x["dec"] = k8_decoder(x["opc"], x["srck"], x["dstk"])
                    if f.get("model") == "atom":
                        v = sel_tuple(x)
                        x["port"], x["thr"] = atom_port(v), atom_throughput(v)
        json.dump(dict(c2=c2build.name(), src=os.path.basename(a.src), functions=fns), open(a.export, "w"))
        print("%d functions -> %s" % (len(fns), a.export))
        return 0
    if a.predict:
        predict_report(parse(text))
        return 0
    if a.check:
        check(parse(text))
        return 0
    sys.stdout.write(report(parse(text), a.ready))
    return 0


def load_export(path):
    import json
    fns = json.load(open(path))["functions"]
    for f in fns:
        for r in f["regions"]:
            for x in r["sel"]:
                x["succ"] = [tuple(e) for e in x["succ"]]
    return fns


def predict_report(fns, show=8):
    """--predict: the DAG, latencies, units, static priorities and schedule from the IL against the trace, with
    the alias facts the compiler answered (ItfPasInterf / ItfIsMemberOfPas) and with the IL-only guess."""
    for alias in (True, False):
        c, bad = compare_dag(fns, alias=alias)
        if not c["regions"]:
            print("predict: no regions with IL (trace with --il / --predict)")
            return
        print("predict (%s): %d regions; edges %d of %d traced reproduced (%d predicted; type and latency equal %d); "
              "DAG exact %d; node latency %d, unit %d, height %d, static priority %d of %d; issue order exact: "
              "%d with the predicted DAG, %d with the traced one" % (
                  "IL + alias facts" if alias else "IL only", c["regions"], c["edges both"], c["edges traced"],
                  c["edges predicted"], c["edges same type+lat"], c["dag exact"], c["lat"], c["unit"],
                  c["height"], c["static"], c["nodes"], c["order predicted dag"], c["order traced dag"]))
    for fn, k, r, te, pe in bad[:show]:
        d = sorted(set(te) ^ set(pe) | {x for x in set(te) & set(pe) if te[x] != pe[x]})
        print("  %s region %d: edges differ %s" % (fn, k, ", ".join("%d->%d %s/%s" % (x[0], x[1], te.get(x), pe.get(x))
                                                                    for x in d[:4])))




# ---------------------------------------------------------------------------------------------- the replay model
FUSIBLE = {"cmp", "test", "add", "sub", "and", "inc", "dec"}
CAPS = {0: 10, 1: 4, 2: 5, 3: 5, 4: 5}          # SchmdGetK8FuncUnit's caps per unit (sched17.md 1.6)


def simulate(region, issue_rate=3, traced_bonus=False, breaks=False, model="k8"):
    """The order SchSchedGraph issues the region's nodes in, from the traced DAG (successor edges, latencies,
    static priorities, units) by the rules of docs/c2/sched17.md 1.6 / 1.8. Decoder pairing (+8) is taken from the
    trace (dyn - static at issue). breaks: also the breakable dependences (6.x): an edge whose latency word has a
    break kind (bits 16..20) does not hold its consumer back (ready when npred == nbreak), is broken (removed) when
    the consumer issues first, and a node waits while a producer of an anti break (kind >= 0x10) could issue by
    the next cycle. model "atom": the Atom path (sched17.md 7: SchAtomPairing, SchAtomGetFuncUnit, 2 issues, busy
    = max(busy, throughput), no macro-fusion hold). -> [lexical index, ...]"""
    sel = region["sel"]
    atom = model == "atom"
    if atom:
        issue_rate = 2
    if not sel or any(s["node"] is None for s in sel):
        return None
    by = {s["node"]: s for s in sel}
    npred = {s["node"]: 0 for s in sel}
    nbreak = {s["node"]: 0 for s in sel}
    preds = {s["node"]: [] for s in sel}
    for s in sel:
        for to, lat in s["succ"]:
            if to in npred:
                npred[to] += 1
                kb = lat >> 16 & 0x1f if breaks else 0
                nbreak[to] += kb != 0
                preds[to].append([s["node"], kb, True])
    est = {n: 0 for n in by}
    dyn = {n: by[n]["static"] for n in by}
    if atom:
        port = {n: by[n]["port"] if "port" in by[n] else atom_port(sel_tuple(by[n])) for n in by}
        thr = {n: by[n]["thr"] if "thr" in by[n] else atom_throughput(sel_tuple(by[n])) for n in by}
    else:
        dec = {n: by[n]["dec"] if "dec" in by[n] else k8_decoder(by[n]["opc"], by[n]["srck"], by[n]["dstk"])
               for n in by}
    busy = {}
    ready = [n for n in by if npred[n] == nbreak[n]]
    nlive = {n: len(by[n]["succ"]) for n in by}
    extra = {n: [] for n in by}
    lexorder = sorted(by, key=lambda n: by[n]["lex"])
    issued_set = set()
    out, cycle = [], 0
    jcc = {n for n in by if by[n]["op"] == "_jcc"}

    def key(n):
        if traced_bonus:
            return (-by[n]["dyn"], by[n]["lex"])
        return (-dyn[n], by[n]["lex"])

    def dynamic(cycle):
        """SchmdComputeDynamicPriority, K8 (sched17.md 1.7), over the ready list in its current order."""
        if len(ready) < 2:
            return
        for a in list(ready):
            dyn[a] = by[a]["static"]
            if est[a] > cycle:
                continue
            if by[a]["op"] in ("bswap", "rcl", "rcr", "rol", "ror"):
                dyn[a] = by[a]["static"] + (1 << 30)
                continue
            b = next((x for x in ready if x != a and est[x] <= cycle), None)
            if b is None:
                b = next((to for to, lat in by[a]["succ"] if to in npred and npred[to] == 1 and
                          (lat & 0xffff) == 0 and est[to] <= cycle), None)
            if b is None:
                continue
            if (dec[a] == 3) != (dec[b] == 3):
                continue
            dyn[a] = by[a]["static"] + 8
            dyn[b] = by[b]["static"] + 8

    def can_pair(a, b):
        return atom_can_pair(port[a], port[b])

    def pairing(cycle):
        """SchAtomPairing 0x10226160 (sched17.md 7.2): the first issuable ready node A and one partner B by issue
        port; both get static + 2^25 (dyn is never reset on Atom)."""
        if len(ready) < 2:
            return
        a = next((x for x in ready if est[x] <= cycle), None)
        if a is None or port[a] == 0x30:
            return
        pa, b = port[a], None
        for x in ready[ready.index(a) + 1:]:
            if est[x] > cycle or not can_pair(a, x):
                continue
            px = port[x]
            if pa == 0x40 and px == 0x20:
                if b is None or port[b] == 0x40:
                    b = x
                continue
            if px == {0x40: 0x10, 0x10: 0x20}.get(pa, 0x10):
                b = x
                break
            if b is None and px == 0x40:
                b = x
        if b is None:
            b = next((to for to, lat in by[a]["succ"] if to in npred and npred[to] == 1 and (lat & 0xffff) == 0 and
                      est[to] <= cycle and can_pair(a, to)), None)
        if b is not None:
            dyn[a] = by[a]["static"] + (1 << 25)
            dyn[b] = by[b]["static"] + (1 << 25)

    def atom_unit_busy(n, slot, issued):
        """SchAtomGetFuncUnit 0x10226000: unit u needs busy[u] < cap[u] (caps 1, 5, 1); integer (0) also needs the
        multiplier idle and, unless its latency is 1, unit 2 free; 1 and 2 need the other two free; the second slot
        must pair with the first by port. -> True when refused"""
        u = by[n]["u"] or 0
        av = lambda k: busy.get(k, 0) < ATOM_CAPS.get(k, 0)
        if not av(u):
            return True
        if u == 0:
            if busy.get(1, 0) > 0 or (not av(2) and by[n]["lat"] > 1):
                return True
        elif not (av(0) and av(2 if u == 1 else 1)):
            return True
        return slot != 0 and not can_pair(issued[0], n)

    def held(n):
        if atom:
            return False
        s = by[n]
        if s["op"] not in FUSIBLE or len(s["succ"]) != 1:
            return False
        to = s["succ"][0][0]
        return to in jcc and npred.get(to, 0) != 1

    def waits(n, cycle):
        return breaks and any(live and kb >= 0x10 and p not in issued_set and npred[p] == nbreak[p] and
                              est[p] <= cycle + 1 for p, kb, live in preds[n])
    guard = 0
    while ready and guard < 100000:
        guard += 1
        issued = []
        ready.sort(key=key)
        if not traced_bonus:
            if atom:
                pairing(cycle)
            else:
                dynamic(cycle)
        for slot in range(issue_rate):
            ready.sort(key=key)
            pick = None
            for n in ready:
                u = by[n]["u"] or 0
                d = max(busy.get(u, 0), 0)
                full = atom_unit_busy(n, slot, issued) if atom else busy.get(u, 0) >= CAPS.get(u, 10)
                if full or held(n) or waits(n, cycle) or est[n] > cycle + d:
                    continue
                pick = (n, d)
                break
            if pick is None:
                break
            n, d = pick
            ready.remove(n)
            out.append(by[n]["lex"])
            issued.append(n)
            issued_set.add(n)
            if breaks and npred[n] > 0:          # SchmdBreakDep: the edges from unissued producers go away
                for pe in preds[n]:
                    if pe[2] and pe[0] not in issued_set:
                        pe[2] = False
                        npred[n] -= 1
                        nbreak[n] -= pe[1] != 0
                        p = pe[0]
                        nlive[p] -= 1
                        if nlive[p] == 0:
                            # DagMakeForwardControlDep: a producer left without successors gets a control edge
                            # to the next control point
                            cp = next((m for m in lexorder if by[m]["lex"] > by[p]["lex"] and
                                       (by[m].get("flags") or 0) & 0x20), None)
                            if cp is not None:
                                extra[p].append((cp, 0))
                                npred[cp] += 1
                                preds[cp].append([p, 0, True])
                                nlive[p] += 1
            for to, lat in by[n]["succ"] + extra[n]:
                if to in npred:
                    pe = next((x for x in preds[to] if x[0] == n and x[2]), None)
                    if pe is None:
                        continue
                    pe[2] = False
                    est[to] = max(est[to], (lat & 0xffff) + d + cycle)
                    was = npred[to] == nbreak[to]
                    if pe[1]:
                        nbreak[to] -= 1
                    npred[to] -= 1
                    if not was and npred[to] == nbreak[to]:
                        ready.append(to)
        for n in issued:
            u = by[n]["u"] or 0
            if atom:     # cold 0x102d3a92: busy = max(busy, SchmdDagThroughput), real opcodes only
                if by[n]["opc"] is not None and 0 < by[n]["opc"] < 0x4f5:
                    busy[u] = max(busy.get(u, 0), thr[n])
                continue
            busy[u] = max(busy.get(u, 0), 0) + by[n]["lat"]
        cycle += 1
        for u in busy:
            busy[u] -= 1
    return out


def check(fns, show=10):
    n = ok = 0
    bad = []
    for f in fns:
        for k, r in enumerate(f["regions"]):
            if len(r["sel"]) < 2:
                continue
            sim = simulate(r, model=f.get("model", "k8"))
            if sim is None:
                continue
            tr = [s["lex"] for s in r["sel"]]
            n += 1
            if sim == tr:
                ok += 1
            else:
                bad.append((f["func"], k, tr, sim))
    print("sched: %d of %d regions issued in the model's order" % (ok, n))
    for b in bad[:show]:
        print("  %s region %d\n    traced %s\n    model  %s" % b)
    return ok, n


# ------------------------------------------------------------------------------------------ the Atom model (7)
# MOD+0x398 bit 6 (c2 -atom, from cl /favor:ATOM) selects OpSchedInfoAtom 0x10023ac0, SchAtomGetFuncUnit and
# SchAtomPairing; docs/c2/sched17.md 7. A "tuple view" for the callbacks: dict(opc, size = tuple +0x10 & 0xfff,
# src / dst = [dict(kind, size, cls, lo, hi, w8)]: leaf kind, +0x10 & 0xfff, (+0x10 >> 12) & 0x1f, +0x28, +0x2c,
# +8), from the issue probe (sel_tuple) or from the IL (il_view).
ATOM_CAPS = {0: 1, 1: 5, 2: 1}                  # 0x10016310 + 20: units 0 integer, 1 multiply, 2 SSE
ATOM_MASKS = (2, 1, 4, 0, 0)                    # 0x10016338 + 20 (SchmdInstrFuncUnitID)


def _s8(x):
    x &= 0xff
    return x - 0x100 if x & 0x80 else x


def atom_row(opc):
    """OpSchedInfoAtom row (latency, throughput, port words); 0, 0, 0 for opcode 0 and >= 0x4f5."""
    if opc is None or not 0 < opc < 0x4f5:
        return 0, 0, 0
    return _c2_1700()["atom"][opc]


def _lf(t, side, i):
    x = t[side]
    return x[i] if i < len(x) else dict(kind=None, size=None, cls=None, lo=None, hi=None, w8=None)


def _amem(t, dst=False, src=False):
    """SchmdHasMemoryAccesses(t, dst, src): a leaf of kind 6, 3, 4 or 2."""
    return any(l["kind"] in (2, 3, 4, 6) for l in (t["dst"] if dst else []) + (t["src"] if src else []))


def sel_tuple(s):
    """The tuple view of a node of the issue probe (fields 15..20)."""
    def leaves(kinds, words):
        out = []
        for i, k in enumerate(kinds):
            w = words[i] if i < len(words) else []
            ty = w[0] if w else None
            out.append(dict(kind=k, size=ty & 0xfff if ty is not None else None,
                            cls=ty >> 12 & 0x1f if ty is not None else None, lo=w[1] if len(w) > 1 else None,
                            hi=w[2] if len(w) > 2 else None, w8=w[3] if len(w) > 3 else None))
        return out
    tt = s.get("ttype")
    return dict(opc=s["opc"] or 0, size=tt & 0xfff if tt is not None else None, kind=None,
                src=leaves(s["srck"], s.get("srcw") or []), dst=leaves(s["dstk"], s.get("dstw") or []))


def il_view(t):
    """The tuple view of an il_tuple (+0x2c and +8 are not in the IL dump: hi = 0, w8 unknown)."""
    def lv(l):
        ty = l.get("type") or 0
        lo = l.get("w28")
        if l["kind"] == 7:
            v = _imm(l)
            lo = v & 0xffffffff if v is not None else lo
        return dict(kind=l["kind"], size=ty & 0xfff, cls=ty >> 12 & 0x1f, lo=lo, hi=0, w8=None, reg=l.get("reg"),
                    base=l.get("base"), index=l.get("index"))
    return dict(opc=t["opc"], size=t["type"] & 0xfff, kind=t["kind"], src=[lv(l) for l in t["src"]],
                dst=[lv(l) for l in t["dst"]])


def _imm_kind(l):
    """The src[1] test shared by several callbacks: an ICONST (7), kind 4, kind 3 unless +8 == 0x4fc, kind 5 with
    +8 0x502 / 0x503 (w8 unknown: kind 3 counts, kind 5 does not)."""
    k = l["kind"]
    return k in (7, 4) or (k == 3 and l["w8"] != 0x4fc) or (k == 5 and l["w8"] in (0x502, 0x503))


def _imm1(l):
    return l["kind"] == 7 and l["lo"] == 1 and (l["hi"] or 0) == 0


def atom_port(t):
    """SchmdDagIssuePort 0x10253ab0: 0x10 port 0, 0x20 port 1, 0x40 either, 0x30 not pairable [I for the names]."""
    if t is None:
        return 0x30
    w = atom_row(t["opc"])[2]
    if w == 0:
        return 0x30
    if w & 0x20000:
        return _atom_port_cb(t)
    if _amem(t, True, True) and w & 0xf000:
        return (w >> 8) & 0xf0
    return w & 0xf0


def _atom_port_cb(t):
    """SchmdPortCallbacks 0x10253cd0."""
    opc, s1 = t["opc"], _lf(t, "src", 1)
    if opc == 0x12:                                    # bt: register 0x20; memory: by the bit operand
        if not _amem(t, src=True):
            return 0x20
        return 0x30 if s1["kind"] == 1 else 0x40
    if opc <= 0xf6:                                    # _imul2 / _imul3: 32-bit on port 0, else not pairable
        return 0x10 if t["size"] == 4 else 0x30
    if opc == 0x103:                                   # jmp
        return 0x20 if _lf(t, "src", 0)["kind"] in (4, 1) else 0x30
    if opc == 0x1e2 or opc <= 0x1eb:                   # psll* / psr*: an immediate count on port 0
        return 0x10 if _imm_kind(s1) else 0x30
    return 0x10 if _imm1(s1) else 0x30                 # rcl: by 1


def atom_throughput(t):
    """SchmdDagThroughput 0x10253be0: OpSchedInfoAtom +4 (bit 16: SchmdThroughputCallbacks + 1), else byte 0 + 1
    + byte 1 with a memory leaf; 0 without a tuple."""
    if t is None:
        return 0
    w = atom_row(t["opc"])[1]
    if w & 0x10000:
        return _atom_thr_cb(t) + 1
    return _s8(w) + 1 + (_s8(w >> 8) if _amem(t, True, True) else 0)


def _atom_thr_cb(t):
    """SchmdThroughputCallbacks 0x10253dc0."""
    opc, sz = t["opc"], t["size"]
    s0, s1, d0 = _lf(t, "src", 0), _lf(t, "src", 1), _lf(t, "dst", 0)
    ms, md = int(_amem(t, src=True)), int(_amem(t, dst=True))
    if opc in (0xf5, 0xf6):                            # _imul2 / _imul3
        if sz == 2:
            return ms + 4
        if sz == 4:
            return 0
        return 0xc if _imm_kind(s1) else 9
    if opc == 0x12:                                    # bt
        return 7 if ms and s1["kind"] == 1 else 0
    if opc <= 0x15:                                    # btc btr bts
        if not md:
            return 0
        return 9 + (sz == 2) if s1["kind"] == 1 else 1
    if opc == 0x18:                                    # call
        return 0 if s0["kind"] in (4, 1) else 1
    if opc in (0x69, 0x6f):                            # cvtss2si / cvttss2si
        return (d0["size"] != 4) + 6 + ms
    if opc <= 0xf6:                                    # div / idiv: by the operand size
        return {1: 0x1f, 2: 0x28, 4: 0x37}.get(s0["size"], 0xc3)
    if opc == 0x163:                                   # mul
        return 10 if s0["size"] == 8 else ms + 4
    if opc <= 0x1e7:                                   # psrad / psraw
        return 0 if _imm_kind(s1) else ms
    if opc == 0x200:                                   # rcl
        return 0 if _imm1(s1) else 0xc
    if opc == 0x203:                                   # rcr
        return (5 if md else 3) if _imm1(s1) else 10
    if opc == 0x289:                                   # shld
        return (2 if md else 0) if sz == 4 else 9
    return (2 if md else 0) if sz == 4 else 7          # shrd


def atom_latency(t):
    """SchmdDagLatency with OpSchedInfoAtom: +0 as on K8 (byte 0 + 1 + byte 1 with a memory leaf; bit 16: the Atom
    half of SchmdLatencyCallbacks, cold 0x102df85b, + 1)."""
    w = atom_row(t["opc"])[0]
    if w & 0x10000:
        return _atom_lat_cb(t) + 1
    return _s8(w) + 1 + (_s8(w >> 8) if _amem(t, True, True) else 0)


def _atom_lat_cb(t):
    """SchmdLatencyCallbacks, Atom (cold 0x102df85b)."""
    opc, sz = t["opc"], t["size"]
    s0, s1, d0 = _lf(t, "src", 0), _lf(t, "src", 1), _lf(t, "dst", 0)
    ms, md = int(_amem(t, src=True)), int(_amem(t, dst=True))
    if opc in (0x143, 0x15e):                          # movdqu / movupd
        return 2 if ms else int(bool(md))
    if opc == 0x15f:                                   # movups
        return 1 if md else (2 if ms else 0)
    if opc in (0x18, 0x103):                           # call / jmp
        return 0 if s0["kind"] in (4, 1) else 1
    if opc == 0x12:                                    # bt
        if not ms:
            return 0
        return (s0["size"] == 2) + (8 if s1["kind"] == 1 else 0)
    if opc <= 0x15:                                    # btc btr bts
        if not ms:
            return 0
        return (sz == 2) + (0xa if s1["kind"] == 1 else 1)
    if opc == 0x66:                                    # cvtsi2sd
        return 4 if ms else int(s0["size"] == 4) + 5
    if opc == 0x67:                                    # cvtsi2ss
        return 6 if ms else int(s0["size"] != 4) + 5
    if opc in (0x76, 0xf3):                            # div / idiv
        return {1: 0x20, 2: 0x29, 4: 0x38}.get(s0["size"], 0xc4)
    if opc in (0xf5, 0xf6):                            # _imul2 / _imul3
        if sz == 2:
            return ms + 5
        if sz == 4:
            return 4
        return 0xd if _imm_kind(s1) else 0xa
    if opc <= 0x142 and opc > 0x6f:                    # movd ...: an integer register destination
        return 2 if (d0["cls"] or 0) in (1, 2, 3, 4) and d0["kind"] == 1 else 0
    if opc <= 0x6f:                                    # cvtss2si / cvttss2si
        return (d0["size"] != 4) + 7 + ms
    if opc in (0x15b, 0x160):                          # movsx / movzx
        return ms + 1 if sz == 2 else 0
    if opc == 0x163:                                   # mul
        return 0xb if s0["size"] == 8 else ms + 5
    if opc == 0x200:                                   # rcl
        return 0 if _imm1(s1) else 0xd
    if opc == 0x203:                                   # rcr
        return (6 if md else 4) if _imm1(s1) else 0xb
    if opc in (0x289, 0x28b):                          # shld / shrd
        return (2 if md else 0) + 1 if sz == 4 else (0xa if opc == 0x289 else 8)
    return 0 if _imm_kind(s1) else ms + 1             # psll* / psr*


def atom_unit(t):
    """SchmdInstrFuncUnitID with the Atom masks {2, 1, 4, 0, 0} on the port word."""
    if t is None or not 0 < (t["opc"] or 0) < 0x4f5:
        return 0
    m = atom_row(t["opc"])[2]
    return next((u for u, b in enumerate(ATOM_MASKS) if m & b), 0)


def atom_can_pair(pa, pb):
    """SchAtomCanBePaired 0x10225fb0 on two issue ports."""
    if pa == 0x10:
        return pb in (0x40, 0x20)
    if pa == 0x20:
        return pb in (0x40, 0x10)
    if pa == 0x30:
        return False
    return pb in (0x40, 0x10, 0x20)


def atom_implicit_sp(opc):
    """SchAtomHasImplicitSPUpdate 0x102260a0: call, _epop/_epush/_eret, leave, pop, push, pushf, ret/fatret/retf."""
    return opc in (0x18, 0x80, 0x81, 0x82, 0x115, 0x1d4, 0x1fd, 0x1fe, 0x212, 0x213, 0x214)


RSI, RDI = 7, 8


def atom_agi(pt, ct, reg):
    """SchAtomAgiStall 0x10225d80 (pt, ct: il_view of producer / consumer; reg: the register of the edge): 3 when the
    consumer computes an address from reg (any memory leaf, as base or index) or reads it implicitly, 0 when the
    producer is a lea (or both ends move rsp implicitly)."""
    fam = reginfo(reg)[0]
    lea = pt["opc"] == 0x114
    sp_p = atom_implicit_sp(pt["opc"])

    def found():
        if lea:
            return 0
        if fam == RSP:
            return 0 if sp_p else 3
        return 3
    o = ct["opc"]
    if o in (0x66, 0x67, 0x13e, 0x151, 0x1c8):        # cvtsi2sd/ss, movd, movq, pinsrw: an integer register in
        if 1 <= reg <= 68:
            return found()
    elif o in (0x12f, 0x130, 0x29c):                   # maskmovdqu, maskmovq, stos: rdi
        if fam == RDI and not lea:
            return 3
    elif o == 0x153:                                   # movs: rsi, rdi
        if (fam == RSI or fam == RDI) and not lea:
            return 3
    for l in ct["src"] + ct["dst"]:
        if l["kind"] in (5, 6) and reg in (l.get("base"), l.get("index")):
            return found()
    if fam != RSP or not atom_implicit_sp(o) or sp_p:
        return 0
    return 0 if lea else 3


def atom_true_lat(pt, ct, plat, reg):
    """SchmdAdjustRegTrueDepStall on Atom (and its inline copy in DagBuildGraph, cold 0x10298f98): producer latency
    + SchAtomLoadToUseStall (-1: a 32-bit mov load feeding anything but an address) + SchAtomCCFlagStall (+1: flags
    into a non-branch) + SchAtomAgiStall."""
    agi = atom_agi(pt, ct, reg)
    ltu = 0
    if pt["opc"] == 0x13b and pt["size"] == 4 and pt["src"] and pt["src"][0]["kind"] in (5, 6, 2) and agi == 0:
        ltu = -1
    ccf = 1 if reginfo(reg)[0] == CC and ct["kind"] != 0x11 else 0
    return plat + ltu + ccf + agi


# ------------------------------------------------------------------------------------- the DAG from the IL (6.x)
# predict_dag(region) builds what DagBuildGraph 0x100e29e0 + SchComputeStaticPriority 0x100e65c0 build, from the
# region's IL alone (the `--il` probe: each tuple as created, operands as the plain IL dump prints them, leaf kinds
# and +0x1c flags, memory leaves' points-at-set index) and OpSchedInfoK8; docs/c2/sched17.md 6.x.
_C2 = {}


def have_c2_1700():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return any(os.path.exists(c) for c in (os.path.join(here, "work", "vs2012", "bin", "c2_x64_1700.dll"),
               os.path.join(here, "work", "toolchain", "c2-17.00.61030-x64", "VC", "Bin", "c2.dll")))


_RN = {}


def _regnames17():
    """Register names -> number of c2 17.00 (the build table's `regnames`, else the DLL)."""
    if not _RN:
        import json
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tab = json.load(open(os.path.join(here, "tools", "c2builds", "17.00.61030-x64.json"))).get("regnames")
        _RN.update({n: int(r) for r, n in tab.items()} if tab else _c2_1700()["regs"])
    return _RN


def _c2_1700():
    """OpSchedInfoK8 rows and the register names of c2 17.00 x64, read from the DLL (whatever C2_BUILD is)."""
    if not _C2:
        import struct, pefile
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cands = [os.path.join(here, "work", "vs2012", "bin", "c2_x64_1700.dll"),
                 os.path.join(here, "work", "toolchain", "c2-17.00.61030-x64", "VC", "Bin", "c2.dll")]
        path = next((c for c in cands if os.path.exists(c)), None)
        if path is None:
            raise FileNotFoundError("c2sched17: the 17.00 x64 c2.dll is not set up (c2stage_vs2012.py, then "
                                    "c2build.py setup --c2 17.00)")
        pe = pefile.PE(path, fast_load=True)
        img, base = pe.get_memory_mapped_image(), pe.OPTIONAL_HEADER.ImageBase
        d = lambda va: struct.unpack_from("<I", img, va - base)[0]

        def cstr(va):
            o = va - base
            return img[o:img.index(b"\0", o)].decode("latin1")
        _C2["k8"] = [struct.unpack_from("<III", img, 0x10012788 - base + 12 * i) for i in range(0x4f5)]
        _C2["atom"] = [struct.unpack_from("<III", img, 0x10023ac0 - base + 12 * i) for i in range(0x4f5)]
        _C2["regs"] = {cstr(d(0x10001ff0 + 4 * r)): r for r in range(1, 364)}
    return _C2


def reginfo(r):
    """Register SYM fields DagBuildGraph compares (read off the live register SYMs, [TLS+0x113f4] + 0x64*r):
    (family, size, offset): +0xc (the containing register), +0x24, +0x28. Sizes in bits; the flags are five
    one-unit fields s o p z c (cc 5, cc_sozp 4 at 0, ..., cc_zc 2 at 3, cc_cf 1 at 4)."""
    if 1 <= r <= 16:
        return r, 64, 0
    if 17 <= r <= 64:
        return (r - 1) % 16 + 1, (32, 16, 8)[(r - 17) // 16], 0
    if 65 <= r <= 68:
        return r - 64, 8, 8
    if 112 <= r <= 127:
        return r, 256, 0
    if 128 <= r <= 143:
        return r - 16, 128, 128
    if 144 <= r <= 351:
        n = 112 + (r - 144) % 16
        g = (r - 144) // 16                  # x/dx/ix, x/dx/ix h, x/dx/ix l, x/ix s, fx l, fx s
        return n, (128, 128, 128, 64, 64, 64, 64, 64, 64, 32, 32, 64, 32)[g], (0, 0, 0, 64, 64, 64) [g] if g < 6 else 0
    if 353 <= r <= 362:
        return 353, (5, 4, 3, 2, 1, 1, 1, 2, 1, 1)[r - 353], (0, 0, 0, 0, 0, 1, 2, 3, 3, 4)[r - 353]
    return r, 0, 0


CC, RSP = 353, 5


def _overlap(a, b):
    fa, sa, oa = reginfo(a)
    fb, sb, ob = reginfo(b)
    return fa == fb and (a == b or (ob < oa + sa and oa < ob + sb))


def _covers(a, b):
    """ItfSymsTotalInterf(a, b): a's range holds b's."""
    fa, sa, oa = reginfo(a)
    fb, sb, ob = reginfo(b)
    return fa == fb and oa <= ob and oa + sa >= ob + sb


def _split_ops(t):
    """The operands of an IL line (memory leaves are bracketed; a name may contain '[' but not start with one)."""
    out, cur, lvl = [], "", 0
    for ch in t.strip() + " ":
        if ch == "[" and (lvl or not cur or cur.endswith("+")):
            lvl += 1
        elif ch == "]" and lvl:
            lvl -= 1
        if ch == " " and not lvl:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    return out


def _leaf(text, kind, flags):
    regs = _regnames17()
    body, _, ty = text.rpartition(":")
    op = dict(kind=kind, flags=flags, text=text, type=int(ty, 16) if ty and all(
        c in "0123456789abcdef" for c in ty) else 0, reg=None, sym=None)
    if kind is None:        # beyond the walker's 8 leaves: from the text
        op["kind"] = kind = 6 if body.startswith("[") else 1 if (body.split("=")[0] in regs) else 0
    if kind == 1:
        a, eq, b = body.partition("=")
        if eq and a in regs:
            op["reg"], op["sym"] = regs[a], b
        else:
            op["reg"], op["sym"] = regs.get(body), body
    elif kind in (5, 6) and body.startswith("["):
        inner, _, pas = body[1:].partition("]")
        op["pas"] = int(pas[1:], 16) if pas.startswith("@") else None
        parts = inner.split("+")
        op["disp"] = parts[-1]
        op["base"] = op["index"] = None
        for x in parts[:-1]:
            if "*" in x:
                op["index"] = _leaf(x.rsplit("*", 1)[0], 1, 0)["reg"]
                sc = x.rsplit("*", 1)[1]
                op["scale"] = {"1": 0, "2": 1, "4": 2, "8": 3}.get(sc, 0)
            elif ":" in x:
                op["base"] = _leaf(x, 1, 0)["reg"]
            else:
                op["msym"] = x
    else:
        op["sym"] = body
    return op


def il_tuple(n):
    """A node of the --il probe -> dict(op, opc, kind, dst=[leaf], src=[leaf]) (leaves: kind, flags, reg = the
    physical register number of a REG leaf, pas / base / index of a memory leaf)."""
    il = n.get("il")
    t = dict(opc=n["opc"], kind=n["kind"], instr=bool(n["tflags"] & 1), op="", dst=[], src=[], cc=n["cc"],
             type=n["type"])
    if not il or il == "L":
        return t
    head, _, ops = il.partition(" | ")
    t["op"] = head.split()[2]
    d, _, sr = ops.partition("<-")
    sr = sr[:-3] if sr.endswith(" ->") else sr
    for side, txt, kinds in (("dst", d, n["dst"]), ("src", sr, n["src"])):
        toks = _split_ops(txt)
        t[side] = [_leaf(x, kinds[i][0] if i < len(kinds) else None, kinds[i][1] if i < len(kinds) else 0)
                   for i, x in enumerate(toks)]
        w = n.get(side + "2") or []
        w3 = n.get(side + "3") or []
        sw = n.get("symw") or [None] * 5
        sw = sw[:3] if side == "src" else sw[3:]
        for i, l in enumerate(t[side]):
            if i < len(w):
                l["w24"], l["w28"] = w[i]
                if l["kind"] == 11:
                    l["pas"] = w[i][0]          # a memory-set leaf: its PAS at +0x24
                if l["kind"] == 2:
                    l["symid"] = w[i][0]
            if i < len(w3) and l["kind"] in (5, 6):
                l["w2c"], l["w8"] = w3[i]          # a memory / address leaf's symbol part and form (_aform)
            if l["kind"] == 2 and i < len(sw) and sw[i] and len(sw[i]) >= 11 and None not in \
                    (sw[i][2], sw[i][3], sw[i][9], sw[i][10]):
                l["symkind"], l["addrtaken"] = sw[i][2] & 0xff, bool(sw[i][2] & 0x200)
                l["root"], l["size"], l["off"] = sw[i][3], sw[i][9], sw[i][10]
    return t


def _hasmem(t, dst=False, src=False):
    """SchmdHasMemoryAccesses: a leaf of kind 6, 3, 4 or 2."""
    return any(l["kind"] in (2, 3, 4, 6) for l in (t["dst"] if dst else []) + (t["src"] if src else []))


def k8_latency(t):
    """SchmdDagLatency (K8): OpSchedInfoK8 +0 (bit 16: SchmdLatencyCallbacks 0x100e4a70), + 1."""
    opc = t["opc"]
    if opc >= 0x4f5:
        return 1
    row = _C2_ROW(opc)[0]
    if row & 0x10000:
        return _lat_callback(t) + 1
    mem = _hasmem(t, True, True)
    return ((row & 0xff) ^ 0x80) - 0x80 + 1 + ((((row >> 8) & 0xff) ^ 0x80) - 0x80 if mem else 0)


def _C2_ROW(opc):
    return _c2_1700()["k8"][opc]


def _lat_callback(t):
    """SchmdLatencyCallbacks, K8 cases as read (sched17.md 6.x); m_s / m_d = a memory source / destination."""
    opc, size = t["opc"], (t["dst"][0]["type"] & 0xfff if t["dst"] else t["type"] & 0xfff)
    ms, md = int(_hasmem(t, src=True)), int(_hasmem(t, dst=True))
    sz = t["type"] & 0xfff
    if opc == 0x114:                                   # lea: 3, 4 when the destination size field is 2
        return 3 + (size == 2)
    if 0xf4 <= opc <= 0xf6 or opc == 0x163:            # imul / mul
        return {1: 3 * (ms + 1), 2: 3 * ms + 4, 4: 3 * (ms + 1)}.get(sz, 3 * ms + 5)
    if opc == 0xf3:                                    # idiv
        return ms + 8 * sz + 10
    if opc == 0x76:                                    # div
        return 8 * sz + 8
    if opc <= 0x15 and opc != 0x10:
        return 1 + (opc != 0x12)
    if opc == 0x13c:
        return md + 2
    if 0x13d <= opc <= 0x157:
        k = (0, 1, 6, 6, 0, 2, 3, 6, 4, 6, 6, 4, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 5, 5, 6, 6)[opc - 0x13d]
        if k == 0:
            return 2 * ms + md + 2
        if k == 1:
            if md:
                return 2
            s0 = t["src"][0] if t["src"] else None
            return 9 if s0 and s0["kind"] == 1 and (s0["type"] >> 12 & 0x1f) in (1, 2) else 4
        if k == 2:
            return md + 2
        if k == 3:
            return 5 * ms + 2 * md + 2
        if k == 4:
            return 2 * ms + 2
        if k == 5:
            return ms + 2
    if 0x158 < opc <= 0x15a:
        return ms + 2
    if 0x15b <= opc <= 0x15f or opc == 0x32b:
        return 5 * ms + 2 * md + 2
    # default (mov, _emov, movzx ...): 4 load, 3 store, 1 register
    if ms:
        return 4
    return 3 if md else 1


def k8_unit(t):
    """SchmdInstrFuncUnitID: the first unit u with OPSCHEDINFO +8 & M[u], M = {2, 1, 4, 8, 0x10}; else 0."""
    if t["opc"] >= 0x4f5 or not t["instr"]:
        return 0
    m = _C2_ROW(t["opc"])[2]
    return next((u for u, b in enumerate((2, 1, 4, 8, 0x10)) if m & b), 0)


def _kinds(t, side):
    return [l["kind"] for l in t[side]]


def k8_dec(t):
    if t["opc"] >= 0x4f5 or not t["opc"]:
        return 1
    d = _C2_ROW(t["opc"])[1]
    if d & 0x40:
        return None
    sh = 2 if any(k in (5, 6) for k in _kinds(t, "src")) else 4 if any(k in (5, 6) for k in _kinds(t, "dst")) else 0
    return d >> sh & 3


CONTROL_KINDS = {0xf, 0x11, 0x13, 0x14, 0x17, 0x18, 0x19, 0x1a}


def pas_interf(a, b, facts=None, la=None, lb=None):
    """ItfPasInterf 0x100de4f0 on two points-at-set indices: 2 = never, 3 only with 3, equal or 1 (unknown)
    interfere; two other sets interfere when their bit vectors intersect (TLS+0xebf4, not in the IL dump): from
    `facts` (the compiler's answers, {(a, b): r}) when given, else the guess of pas_guess."""
    if a is None or b is None:
        return True
    if a == 2 or b == 2:
        return False
    if a == 3 or b == 3:
        return a == b
    if a == b or a == 1 or b == 1:
        return True
    if facts is not None:
        r = facts.get((a, b), facts.get((b, a)))
        if r is not None:
            return bool(r)
    return pas_guess(a, b, la, lb)


def _basesym(l):
    """The variable a memory leaf's base register holds (the name after '=' in the dump), or None."""
    body = l.get("text", "")[1:].split("]")[0].split("+")
    b = [x for x in body[:-1] if "*" not in x and ":" in x]
    return b[0].rsplit(":", 1)[0].split("=")[-1] if b else None


def pas_guess(a, b, la=None, lb=None):
    """Without the alias sets (IL only), from zlib's answers: set 6 meets only itself; 7 (the call / escaping set),
    10, 13 and 15 meet every set but 6; two per-access sets (>= 16) meet unless both leaves are based on the same
    variable without an index."""
    lo, hi = min(a, b), max(a, b)
    if lo == 6 or hi == 6:
        return lo == hi
    if lo < 16:
        return True
    if la is None or lb is None:
        return True
    ba, bb = _basesym(la), _basesym(lb)
    return not (ba is not None and ba == bb and la.get("index") is None and lb.get("index") is None)


def memb_interf(sym, pas, facts=None):
    """ItfIsMemberOfPas(sym, pas): an address-taken variable in a points-at set."""
    if pas in (1,):
        return True
    if pas in (2, 3):
        return False
    if facts is not None:
        r = facts.get((sym, pas))
        if r is not None:
            return bool(r)
    return pas in (7, 10, 13, 15)


def mem_interf(e, c, facts=None):
    """ItfPartialInterf 0x100ddb70 for the memory lists (leaf kinds 6 memory, 11 memory set, 2 address-taken
    variable): by PAS (+0x28 of a memory leaf, +0x24 of a memory-set leaf) and set membership; a memory set marked
    by +0x1c bit 8 meets only another marked one."""
    pf, mf = (facts or {}).get("pas"), (facts or {}).get("memb")
    ke, kc = e["kind"], c["kind"]
    if ke == 11 or kc == 11:
        fe, fc = ke == 11 and e["flags"] & 0x100, kc == 11 and c["flags"] & 0x100
        if fe or fc:
            if not (fe and fc):
                return False
    if ke == 2 and kc == 2:
        if e.get("root") is not None and c.get("root") is not None:
            return e["root"] == c["root"] and (e.get("symid") == c.get("symid") or
                                               (c["off"] < e["off"] + e["size"] and e["off"] < c["off"] + c["size"]))
        return e.get("symid") == c.get("symid")
    if ke == 2 or kc == 2:
        v, m = (e, c) if ke == 2 else (c, e)
        return memb_interf(v.get("symid"), m.get("pas"), mf)
    return pas_interf(e.get("pas"), c.get("pas"), pf, e, c)


def predict_dag(region, alias=True, model="k8"):
    """DagBuildGraph + SchmdFindCBranchDep + SchComputeStaticPriority from the region's IL -> list of nodes
    (lexical order, the entry node 0 and the exit node N+1 included) with succ = [(to lex, type, lat)] in
    creation order, lat, unit, decoder, height, B/L/S/C flags, static priority. model "atom": OpSchedInfoAtom's
    latencies, units, ports and throughputs, the Atom true / output edge latencies and breakable cases (7)."""
    atom = model == "atom"
    tups = [il_tuple(n) for n in sorted(region["nodes"], key=lambda n: n["lex"])]
    srcw = {s["lex"]: s.get("srcw") or [] for s in region.get("sel", [])}
    for k, t in enumerate(tups):
        # exports traced before the IL probe logged (+0x2c, +8): those of the first four sources from the issue
        # probe (fields 18 / 19; the same tuple operands)
        for i, l in enumerate(t["src"][:4]):
            if l["kind"] in (5, 6) and "w8" not in l and i < len(srcw.get(k + 1, [])):
                l["w2c"], l["w8"] = srcw[k + 1][i][2:4]
    N = len(tups)
    facts = None
    if alias and region.get("alias") is not None:
        facts = dict(pas={(a, b): r for a, b, r in region["alias"]["pas"]},
                     memb={(a, b): r for a, b, r in region["alias"]["memb"]})
    nodes = [dict(lex=0, t=None, succ=[], pred=[], flags=0x20, lat=0)]
    for k, t in enumerate(tups):
        f = (4 if any(l["kind"] in (6, 2) for l in t["src"]) else 0) | \
            (8 if any(l["kind"] in (6, 2) for l in t["dst"]) else 0)
        if atom:
            v = il_view(t)
            nodes.append(dict(lex=k + 1, t=t, v=v, succ=[], pred=[], flags=f, lat=atom_latency(v) if t["instr"] else 0,
                              u=atom_unit(v) if t["instr"] else 0, dec=None, port=atom_port(v),
                              thr=atom_throughput(v)))
            continue
        nodes.append(dict(lex=k + 1, t=t, succ=[], pred=[], flags=f, lat=k8_latency(t) if t["instr"] else 0,
                          u=k8_unit(t), dec=k8_dec(t)))     # a non-instruction tuple: latency 0 (0x100e3e67)
    edge = {}

    def mk(a, b, ty, lat=0, update=True):
        """DagMakeDep / DagMakeOrUpdateDep: one edge per pair (the producer is found in b's predecessor list)."""
        e = edge.get((a, b)) if update else None
        if e is not None:
            e["type"] |= ty
            return e
        e = dict(frm=a, to=b, type=ty, lat=lat)
        edge[(a, b)] = e
        nodes[a]["succ"].append(e)
        nodes[b]["pred"].append(e)
        return e
    st = dict(cp=0, defs=[], uses=[], mdefs=[], muses=[])

    def control(n):
        """DagMakeControlPoint: a control edge from every node since the last control point without successors."""
        for m in range(st["cp"], n):
            if not nodes[m]["succ"]:
                mk(m, n, 0x80000, update=False)
        t = nodes[n]["t"]
        if t is not None and t["kind"] == 0x1a and nodes[st["cp"]]["t"] is not None and \
                nodes[st["cp"]]["t"]["kind"] == 0x1a:
            mk(st["cp"], n, 0x80000)
        st["cp"] = n
        nodes[n]["flags"] |= 0x20
        st["defs"], st["uses"], st["mdefs"], st["muses"] = [], [], [], []

    def agi(cons, reg):
        """SchmdAgiStall (K8): the consumer's first memory leaf has reg as index or base; or reg is rsp feeding a
        push / pop from a non-push / pop."""
        t = nodes[cons]["t"]
        for l in [x for x in t["src"] if x["kind"] in (5, 6)] + [x for x in t["dst"] if x["kind"] == 6]:
            return 1 if reg in (l.get("index"), l.get("base")) else 0
        return 0

    def true_dep(prod, n, reg):
        e = mk(prod, n, 1)
        pt = nodes[prod]["t"]
        if atom:
            lat = 0 if pt is None or pt["opc"] == 0x517 else \
                atom_true_lat(nodes[prod]["v"], nodes[n]["v"], nodes[prod]["lat"], reg)
            e["lat"] = max(e["lat"], lat)
            return
        lat = 0 if pt is not None and pt["opc"] == 0x517 else nodes[prod]["lat"] + agi(n, reg)
        e["lat"] = max(e["lat"], lat)

    def reg_def(n, r, cc_free=False):
        if not cc_free:
            for (dr, dn) in st["defs"]:
                if not isinstance(dr, int) or not _overlap(dr, r):
                    continue
                oe = mk(dn, n, 4)                      # output: latency max(lat, 0) on K8, max(lat, 1) on Atom
                if atom:
                    oe["lat"] = max(oe["lat"], 1)
                if reginfo(r)[0] == CC:
                    continue                           # a flags definition kills nothing
                if _covers(dr, r):
                    break
        for (ur, un) in st["uses"]:
            if isinstance(ur, int) and _overlap(ur, r):
                mk(un, n, 2)

    def pseudo_cc(n, l):
        """SchmdPseudoCCDef 0x100e51c0 (and the same walk inline at 0x100e3624): an unnamed flags clobber (the leaf's
        own symbol is cc) that a later instruction of the region overwrites before anything reads the flags, with no
        branch or non-instruction between."""
        if l["reg"] != CC or l["sym"] != "cc":
            return False
        for t2 in tups[n:]:
            if not t2["instr"] or t2["kind"] == 0x11:
                return False
            if any(x["kind"] == 1 and x["reg"] and reginfo(x["reg"])[0] == CC for x in t2["src"]):
                return False
            if any(x["kind"] == 1 and x["reg"] and reginfo(x["reg"])[0] == CC for x in t2["dst"]):
                return True
        return False

    def kills(n):
        """The registers of a register-set destination (a call's clobbers, in register-number order): the bit
        vector as logged, else the Win64 volatile set."""
        ks = region.get("kill")
        if ks is not None and str(n) in ks:
            return ks[str(n)]
        return CALL_CLOBBERS

    def is_mem(l):
        """A variable leaf goes to the memory lists when its SYM is not a variable / temporary (kind 3, 4, 5) or
        is address-taken (SYM +9 bit 1)."""
        return l.get("symkind") not in (3, 4, 5, None) or l.get("addrtaken", False)

    def sv(l):
        """A variable in the register lists: (root, offset, size) when its SYM was logged, else its name."""
        if l.get("root") is not None:
            return ("v", l["root"], l["off"], l["size"])
        return ("v", l["sym"], 0, 0)

    def sym_ov(a, b):
        """ItfSymsPartialInterf of two variables: the same root and overlapping ranges (or no sizes known)."""
        return a[1] == b[1] and (a == b or (b[2] < a[2] + a[3] and a[2] < b[2] + b[3]))

    def sym_defs(sym, n, ty):
        """DagChkSymDefs on a variable (a kind-2 leaf that is not address-taken): the register lists, by symbol."""
        for (dr, dn) in st["defs"]:
            if isinstance(dr, tuple) and sym_ov(dr, sym):
                e = mk(dn, n, ty)
                if ty == 0x20:
                    e["lat"] = 1
                if dr[1] == sym[1] and dr[2] <= sym[2] and dr[2] + dr[3] >= sym[2] + sym[3]:
                    break

    def sym_refs(sym, n, ty):
        for (ur, un) in st["uses"]:
            if isinstance(ur, tuple) and sym_ov(ur, sym):
                mk(un, n, ty)
    for n in range(1, N + 1):
        t = nodes[n]["t"]
        k = t["kind"]
        if (k in CONTROL_KINDS and t["opc"] != 0x577 and not (k == 0x14 and t["opc"] == 0x58f)) or k == 0x15 or \
                (k == 0x16 and t["opc"] != 0x59e):
            control(n)
        if t["instr"]:
            srcs, dsts = t["src"], t["dst"]
            zero = (t["opc"] in (0x2b5, 0x2b6, 0x2b7, 0x3e8, 0x3e9) and len(srcs) >= 2 and srcs[0]["kind"] == 1 and
                    srcs[1]["kind"] == 1 and srcs[0]["reg"] is not None and srcs[0]["reg"] == srcs[1]["reg"]) or \
                   (t["opc"] == 0x16d and len(srcs) >= 2 and srcs[0]["kind"] == 1 and _imm(srcs[1]) == -1)
            if zero:     # DagMakeAntiOutputDeps 0x100e4e10: the destinations' output / anti edges only
                for l in dsts:
                    if l["kind"] == 1 and l["reg"] is not None:
                        reg_def(n, l["reg"], pseudo_cc(n, l))
            else:
                for l in srcs:
                    if l["kind"] == 1 and l["reg"] is not None:
                        for (dr, dn) in st["defs"]:
                            if isinstance(dr, int) and _overlap(dr, l["reg"]):
                                true_dep(dn, n, l["reg"])
                                if _covers(dr, l["reg"]):
                                    break
                    elif l["kind"] == 2 and not is_mem(l):
                        sym_defs(sv(l), n, 0x20)
                    elif l["kind"] in (6, 11, 2):
                        if l["kind"] == 6 and l.get("pas") == 2:
                            continue
                        for (ml, mn) in st["mdefs"]:
                            if l["kind"] == 2 and ml["kind"] == 2 and ml.get("symid") == l.get("symid"):
                                mk(mn, n, 0x20)      # ItfInterfVolatileFilter(.., ItfTotalInterf): and stop
                                break
                            if (ml["flags"] & 0x2000 and not (l["kind"] == 6 and l.get("pas") == 3)) or \
                                    mem_interf(ml, l, facts):
                                mk(mn, n, 0x20)
                for i, l in enumerate(dsts):
                    if l["kind"] == 1 and l["reg"] is not None:
                        reg_def(n, l["reg"], pseudo_cc(n, l))
                    elif l["kind"] == 2 and not is_mem(l):
                        sym_defs(sv(l), n, 0x80)
                        sym_refs(sv(l), n, 0x40)
                    elif l["kind"] in (6, 11, 2):
                        if not (l["kind"] == 6 and l.get("pas") == 2):     # DagChkMemrefDefs
                            for (ml, mn) in st["mdefs"]:
                                if l["kind"] == 2 and ml["kind"] == 2 and ml.get("symid") == l.get("symid"):
                                    mk(mn, n, 0x80)
                                    break
                                if (ml["flags"] & 0x2000 and not (l["kind"] == 6 and l.get("pas") == 3)) or \
                                        mem_interf(ml, l, facts):
                                    mk(mn, n, 0x80)
                        for (ml, mn) in st["muses"]:
                            if mem_interf(ml, l, facts):
                                mk(mn, n, 0x40)
                    elif l["kind"] == 10:
                        for r in kills(n):
                            reg_def(n, r)
            if t["opc"] in (0x18, 0x3eb, 0x4f0, 0x4f2, 0x4f3):    # call (and four others): a control point
                control(n)
            # the lists (newest first): register / variable uses and definitions, memory references
            for l in srcs:
                if l["kind"] == 1 and l["reg"] is not None:
                    st["uses"].insert(0, (l["reg"], n))
                elif l["kind"] == 2:      # get_local_base_reg: the frame register is used too
                    st["uses"].insert(0, (RSP, n))
                    if is_mem(l):
                        st["muses"].insert(0, (l, n))
                    else:
                        st["uses"].insert(0, (sv(l), n))
                elif l["kind"] in (6, 11):
                    st["muses"].insert(0, (l, n))
            for l in dsts:
                if l["kind"] == 1 and l["reg"] is not None:
                    st["defs"].insert(0, (l["reg"], n))
                elif l["kind"] == 2:
                    st["uses"].insert(0, (RSP, n))
                    if is_mem(l):
                        st["mdefs"].insert(0, (l, n))
                    else:
                        st["defs"].insert(0, (sv(l), n))
                elif l["kind"] in (6, 11):
                    st["mdefs"].insert(0, (l, n))
                elif l["kind"] == 10:
                    for r in kills(n):
                        st["defs"].insert(0, (r, n))
        if not nodes[n]["pred"]:
            mk(st["cp"], n, 0x80000, update=False)
    nodes.append(dict(lex=N + 1, t=None, succ=[], pred=[], flags=0, lat=0))
    control(N + 1)
    # SchmdFindCBranchDep: the final jcc
    last = nodes[N]
    if last["t"] is not None and 0xff <= last["t"]["opc"] <= 0x102:
        br = last["t"]["src"][1] if len(last["t"]["src"]) > 1 else None
        for e in last["pred"]:
            p = nodes[e["frm"]]
            if e["type"] & 1:
                p["flags"] |= 2
            elif e["type"] & 0x80000 and p["t"] is not None and p["t"]["instr"] and br and br["kind"] == 1:
                for l in p["t"]["dst"]:
                    if l["kind"] == 1 and l["reg"] and br["reg"] and reginfo(l["reg"])[0] == reginfo(br["reg"])[0]:
                        p["flags"] |= 2
                        e["type"] |= 1
                        e["lat"] = 1
                        break
    # heights (backward), then breakable edges, C and the static priority (forward)
    nodes[-1]["height"] = 0
    for nd in reversed(nodes[:-1]):
        nd["height"] = 1 + max([nodes[e["to"]]["height"] + e["lat"] for e in nd["succ"]] or [0])
    nodes[0]["flags"] |= 1
    for nd in nodes:
        nd["nbreak"] = 0
    for nd in nodes[:-1]:
        for e in list(nd["succ"]):
            dep_is_breakable(nodes, e, mk, model)
        if nd["lex"] and len(nd["pred"]) == nd["nbreak"]:
            # DagMakeBackwardControlDep: only breakable predecessors: a control edge from the nearest earlier
            # control point
            cp = next(m for m in range(nd["lex"] - 1, -1, -1) if nodes[m]["flags"] & 0x20)
            mk(cp, nd["lex"], 0x80000, update=False)
        if nd["flags"] & 1:
            for e in nd["succ"]:
                if nd["height"] == nodes[e["to"]]["height"] + e["lat"] + 1:
                    nodes[e["to"]]["flags"] |= 1
        f = nd["flags"]
        p = ((f >> 1 & 1) << 23) + (min(nd["height"], 0xffff) << 5) + (len(nd["succ"]) << 10) + ((f & 1) << 1) + \
            ((f >> 2 & 1) << 15)
        if nd["t"] is not None and (nd["t"]["type"] & 0x1f000) == 0x5000:
            p += f >> 3 & 1
        nd["static"] = p
    return nodes


def _find_agi(a, b):
    """SchmdFindAGI 0x100e7070: a's first register destination that is not a memory leaf or the flags (sym), and
    b's memory leaf (its first memory source, else first memory destination; SchmdFindIndir) addressed through
    exactly that register, unless b reads the flags or names the register as its first source, or as its second
    one other than as that leaf's base / index. -> (leaf, reg) or (None, None)"""
    sym = None
    for l in a["dst"]:
        if l["kind"] == 6:
            return None, None
        if l["kind"] == 1 and l["reg"] and reginfo(l["reg"])[0] != CC:
            sym = l["reg"]
            break
    if sym is None:
        return None, None
    m = next((l for l in b["src"] if l["kind"] == 6), None) or next((l for l in b["dst"] if l["kind"] == 6), None)
    if m is None or sym not in (m.get("index"), m.get("base")):
        return None, None
    if any(l["kind"] == 1 and l["reg"] and reginfo(l["reg"])[0] == CC for l in b["src"]):
        return None, None
    s0 = b["src"][0] if b["src"] else None
    if s0 is not None and s0["kind"] == 1 and s0["reg"] and reginfo(s0["reg"])[0] == reginfo(sym)[0]:
        return None, None
    return m, sym


def _imm(l):
    """An ICONST leaf's value: leaf +0x28 (the plain dump prints +0x24, which is 0 on x64), sign-extended."""
    if l is None or l["kind"] != 7:
        return None
    if l.get("w28") is not None:
        v = l["w28"] & 0xffffffff
        return v - (1 << 32) if v & 0x80000000 else v
    try:
        return int(l["sym"])
    except (TypeError, ValueError):
        return None


def _disp(m):
    try:
        return int(m.get("disp", "0"))
    except ValueError:
        return 0


# memory / address leaf forms (leaf +8, sched17.md 1.6.1): [b + d], [b + i*s + d], [S + i*s + d] (a symbol at +0x2c,
# not printed by the IL dump), [i*s + d]
AF_BD, AF_BISD, AF_SISD, AF_ISD = 0x4fe, 0x4ff, 0x500, 0x501


def _aform(l):
    """A memory / address leaf's form code (+8): logged by the IL probe (field 15 / 16, w8), else from the text
    (an index-only address is taken to be [i*s + d], i.e. no symbol at +0x2c)."""
    if l.get("w8") in (AF_BD, AF_BISD, AF_SISD, AF_ISD):
        return l["w8"]
    if l.get("base") is not None:
        return AF_BISD if l.get("index") is not None else AF_BD
    return AF_ISD if l.get("index") is not None else None


def dep_is_breakable(nodes, e, mk, model="k8"):
    """SchmdDepIsBreakable 0x100e67b0, every case (sched17.md 1.6.1): an edge between an instruction that moves an
    address register by a constant (add / sub / inc / dec imm, mul by a scale, lea) and a memory access through
    that register can be broken by rewriting the access's address. Marks the edge's break kind (latency word bits
    16..20; >= 0x10: an anti edge, the consumer moves the register), counts it in the consumer's nbreak, and for a
    true edge gives the consumer the producer's own true / control predecessors (it will read the register's older
    value). The speed flag TLS+0xdd08 (set at /O2) is taken as set."""
    p, c = nodes[e["frm"]], nodes[e["to"]]
    if p["flags"] & 0x20 or c["flags"] & 0x20 or p["t"] is None or c["t"] is None:
        return False
    pt, ct = p["t"], c["t"]

    def isrsp(t):
        d0 = t["dst"][0] if t["dst"] else None
        return d0 is not None and d0["kind"] == 1 and d0["reg"] == RSP

    def s1imm(t):
        return _imm(t["src"][1]) if len(t["src"]) > 1 else None

    if e["type"] & 1:
        # true edge: the switch on the producer's opcode (0x100e684b)
        m, sym = _find_agi(pt, ct)
        if m is None:
            return False
        op, fm = pt["opc"], _aform(m)
        a = None
        if op == 0x29e:                                   # sub r, imm: kind 3 (not rsp)
            if isrsp(pt) or s1imm(pt) is None:
                return False
            kind, delta = 3, -s1imm(pt)
        elif op == 3:                                     # add r, imm: kind 1; add r, r2: kind 2, always undone
            if s1imm(pt) is None:                         # at 0x100e6c52
                return False
            kind, delta = 1, s1imm(pt)
        elif op == 0xf7:                                  # inc: kind 0xb (no rsp test on this path)
            kind, delta = 0xb, 1
        elif op == 0x75:                                  # dec: kind 0xc (not rsp)
            if isrsp(pt):
                return False
            kind, delta = 0xc, -1
        elif op == 0x163:                                 # mul r, c (cold 0x102df6fe): kind 4, c a scale
            cst = s1imm(pt)
            if cst is None:
                return False
            if m.get("index") is None:
                if cst not in (1, 2, 4, 8):
                    return False                          # (m has a base: rejected at 0x102df743)
            elif m.get("base") is not None or (cst << m.get("scale", 0)) not in (1, 2, 4, 8):
                return False      # the last case reaches 0x100e6c4b with no kind written (not modelled)
            kind, delta = 4, 0
        elif op == 0x114:                                 # lea (0x100e68ce): kind 5, 7 or 9
            if m.get("w2c"):                              # the consumer's address has a symbol part (+0x2c)
                return False
            a = next((l for l in pt["src"] if l["kind"] == 5), None)
            if a is None or (a.get("base") == RSP and a.get("index") is None):
                return False
            fa, ad, md = _aform(a), _disp(a), _disp(m)
            if fm == AF_BD or (fm == AF_ISD and not m.get("scale", 0)):
                # kind 5 (0x100e6c0b): the lea's address replaces the register; refused when a short
                # displacement would become long, unless both are long already
                if (abs(md) < 0x80 or abs(ad) < 0x80) and abs(md + ad) > 0x80:
                    return False
                kind = 5
            elif m.get("base") == sym:
                # kind 7: the register is the consumer's base; the lea's address is [b + d] or [i*1 + d]
                if not (fa == AF_BD or (fa == AF_ISD and not a.get("scale", 0))):
                    return False
                kind = 7
            else:
                # kind 9 (0x100e6db1): the register is the consumer's index; the lea's address is [b + d]
                # (scale 0) or [i*s + d], and the two scales add up to <= 3 ([b + i*s + d]: 99, never)
                sa = 0 if fa == AF_BD else a.get("scale", 0) if fa == AF_ISD else 99
                if sa + m.get("scale", 0) > 3:
                    return False
                kind = 9
            delta = ad
        else:
            return False
        # 0x100e6c4b, every true kind
        dp = _disp(m)
        if abs(dp) < 0x80 and abs(dp + delta) >= 0x80:
            return False
        if e["type"] & 6:
            # also anti / output (0x100e6f0a): only for a lea whose address does not use its own destination; the
            # edge is split into a breakable true edge of kind + 1 (6, 8, 10: folded at issue time by
            # SchmdHasFoldableAGI) and a new plain edge with the other bits (latency 1 when it is an output edge)
            a0 = pt["src"][0] if pt["src"] else None
            if op != 0x114 or a0 is None or sym in (a0.get("base"), a0.get("index")):
                return False
            ne = dict(frm=e["frm"], to=e["to"], type=e["type"] & ~1, lat=1 if e["type"] & 4 else 0)
            p["succ"].append(ne)
            c["pred"].append(ne)
            e["type"], e["brk"] = 1, kind + 1
            c["nbreak"] += 1
            return True
        e["brk"] = kind
        for pe in list(p["pred"]):
            # 0x100e6cb9: a true predecessor of the producer that is not yet one of the consumer
            # (DagBackwardDep: an edge of any type) becomes one with the broken edge's latency (on Atom, cold
            # 0x102df7a5, the producer's own edge is then set to 1); a control bit is copied (DagMakeOrUpdateDep)
            have = any(x["frm"] == pe["frm"] for x in c["pred"])
            if pe["type"] & 1 and not have:
                ne = mk(pe["frm"], c["lex"], 1, update=False)
                ne["lat"] = e["lat"]
                if model == "atom":
                    pe["lat"] = 1
            elif pe["type"] & 0x80000:
                mk(pe["frm"], c["lex"], 0x80000)
        c["nbreak"] += 1
        return True
    if e["type"] & 2:
        # anti edge: the switch on the consumer's opcode (0x100e6810); on Atom only a lea (cold 0x102df7b1)
        op = ct["opc"]
        if model == "atom" and op != 0x114:
            return False
        m, sym = _find_agi(ct, pt)
        if m is None:
            return False
        if op == 3:                                       # add r, imm: kind 0x10 (not rsp)
            if isrsp(ct) or s1imm(ct) is None:
                return False
            kind, delta = 0x10, -s1imm(ct)
        elif op == 0x29e:                                 # sub r, imm: kind 0x11
            if s1imm(ct) is None:
                return False
            kind, delta = 0x11, s1imm(ct)
        elif op == 0xf7:                                  # inc: kind 0x15 (not rsp)
            if isrsp(ct):
                return False
            kind, delta = 0x15, -1
        elif op == 0x75:                                  # dec: kind 0x16
            kind, delta = 0x16, 1
        elif op == 0x114:
            # lea r, [r + d] / [r*1 + d] with no symbol part (0x100e69ae): kind 0x13 when r is the producer's
            # base, 0x14 when it is its index; the new displacement must fit in 32 bits
            a = next((l for l in ct["src"] if l["kind"] == 5), None)
            if a is None or a.get("w2c"):
                return False
            fa = _aform(a)
            if not ((fa == AF_BD and a.get("base") == sym) or
                    (fa == AF_ISD and a.get("index") == sym and not a.get("scale", 0))):
                return False
            ad, md = _disp(a), _disp(m)
            if m.get("base") == sym:
                kind, nd = 0x13, md - ad
            elif m.get("index") == sym:
                kind, nd = 0x14, md - (ad << m.get("scale", 0))
            else:
                return False
            if not -0x80000000 <= nd <= 0x7fffffff:
                return False
            delta = -ad
        else:
            return False
        # 0x100e6a5f: through rsp only when the new displacement stays >= 0; not also an output edge
        if (sym == RSP and _disp(m) + delta < 0) or e["type"] & 4:
            return False
        e["brk"] = kind
        c["nbreak"] += 1
        return True
    return False


# the registers a call's register-set leaf (kind 10) clobbers: the Win64 volatile set (rax rcx rdx r8-r11,
# xmm0-5; the flags are the call's own unnamed cc destination)
CALL_CLOBBERS = [1, 2, 3, 9, 10, 11, 12] + [112 + i for i in range(6)]


def predicted_sel(region, nodes=None, model="k8"):
    """The predicted DAG in the shape simulate() reads (node = lexical index)."""
    nodes = nodes or predict_dag(region, model=model)
    out = []
    for nd in nodes[1:-1]:
        t = nd["t"]
        out.append(dict(op=t["op"], lex=nd["lex"], node=nd["lex"], static=nd["static"], dyn=nd["static"],
                        height=nd["height"], lat=nd["lat"], u=nd["u"], flags=nd["flags"], opc=t["opc"],
                        srck=_kinds(t, "src"), dstk=_kinds(t, "dst"), dec=nd["dec"],
                        succ=[(e["to"], e["lat"] | e.get("brk", 0) << 16) for e in nd["succ"]],
                        etype=[e["type"] for e in nd["succ"]]))
        if "port" in nd:
            out[-1].update(port=nd["port"], thr=nd["thr"])
    return out


def compare_dag(fns, show=0, alias=True):
    """Edges, latencies, units and static priorities of the predicted DAG against the traced one, and the
    schedule from each. -> dict of counts."""
    import collections
    c = collections.Counter()
    bad = []
    for f in fns:
        for k, r in enumerate(f["regions"]):
            if len(r["sel"]) < 2 or not r.get("nodes") or any("etype" not in s for s in r["sel"]):
                continue
            model = f.get("model", "k8")
            nodes = predict_dag(r, alias, model)
            pr = {s["lex"]: s for s in predicted_sel(r, nodes, model)}
            tr = {s["lex"]: s for s in r["sel"]}
            lexof = {s["node"]: s["lex"] for s in r["sel"]}
            exit_lex = len(r["nodes"]) + 1
            te = {}
            for s in r["sel"]:
                for (to, lat), ty in zip(s["succ"], s["etype"]):
                    te[(s["lex"], lexof.get(to, exit_lex))] = (ty, lat & 0xffff)
            pe = {}
            order = {x["lex"]: i for i, x in enumerate(r["sel"])}
            for s in pr.values():
                for (to, lat), ty in zip(s["succ"], s["etype"]):
                    if lat >> 16 and order.get(to, 1 << 30) < order.get(s["lex"], 0):
                        continue      # a breakable edge the traced order broke (the consumer went first)
                    pe[(s["lex"], to)] = (ty, lat & 0xffff)
            c["regions"] += 1
            c["edges traced"] += len(te)
            c["edges predicted"] += len(pe)
            c["edges both"] += len(set(te) & set(pe))
            c["edges same type+lat"] += sum(te[x] == pe[x] for x in set(te) & set(pe))
            c["dag exact"] += te == pe
            for lx, s in tr.items():
                p = pr.get(lx)
                c["nodes"] += 1
                if p is None:
                    continue
                c["lat"] += p["lat"] == s["lat"]
                c["unit"] += p["u"] == s["u"]
                c["static"] += p["static"] == s["static"]
                c["height"] += p["height"] == s["height"]
            ok_tr = simulate(r, model=model) == [s["lex"] for s in r["sel"]]
            ok_pr = simulate(dict(sel=list(pr.values())), breaks=True, model=model) == [s["lex"] for s in r["sel"]]
            c["order traced dag"] += ok_tr
            c["order predicted dag"] += ok_pr
            if te != pe or ok_tr != ok_pr:
                bad.append((f["func"], k, r, te, pe))
    return c, bad


if __name__ == "__main__":
    sys.exit(c2trace.pipe_safe(main, sys.argv[1:]))
