#!/usr/bin/env python3
"""The x64 scheduler of cl 17.00 (x86_amd64 c2.dll), traced: in which order and why each region's instructions
were issued.  Doc: docs/c2/sched17.md (the map: work/port1700/sched_map.md).

usage: c2sched17.py SRC --func F [--ready] [--check] [--raw] [-- <cl args>]     (--c2 17.00)
  --check: replay every region with the model (tools/c2sched17.py simulate) against the traced issue order

One pass after register allocation (dop2_PreEncode -> Schedule), K8 model at /O2. Per region (a DAG of up to 81
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

FN, REGION, SEL, READY = range(4)
OPNAME = "100226e8"


def hooks(ready):
    h = [
        "@Schedule@8=tdccc>0>0>4$",                                  # 0 a function
        "100e5440=ecx",                                              # 1 SchSchedGraph: a region (graph)
        # 2 a node issued: name, lexical index | height, static, dyn, est, cycle, unit (local), latency, flags
        "100e56fb=edi>0x1c>8~%s$,edi>0x38,edi>0x2c,edi>0x30,edi>0x34,ebp>-4,ebp>-0xc,edi>0x3c,edi>0x40,"
        "edi>0x24,edi>0x1c>0x20,edi,edi>0xc*24:0xc:0x14,edi>0x3e" % OPNAME +  # + node, successor edges (to, lat), unit
        ",edi>0x1c>8,edi>0x1c>0x30*8:0xc:0xc:4,edi>0x1c>0x34*8:0xc:0xc:4",   # opcode; src / dst leaf kinds (decoder)
    ]
    if ready:   # 3 the ready list after the per-cycle re-sort: (lexical index | latency, dyn) per node, next at +0x10
        h.append("100e5649=esi>0x11418*40:0x3a:0x30:0x10,esi>0x11444")
    return h


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


def parse(text):
    fns, cur, reg, th = [], {}, {}, 0
    for ln in text.splitlines():
        if ln.startswith("# thread "):
            th = int(ln.split()[2])
            continue
        if not ln.startswith("E ") or "|" not in ln:
            continue
        head, _, rest = ln.partition("|")
        kind, v = int(head.split()[3]), c2trace._split(rest)
        if kind == FN:
            cur[th] = dict(func=v[0].strip('"'), regions=[])
            fns.append(cur[th])
        elif th in cur and kind == REGION:
            reg[th] = dict(sel=[], ready=[])
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
    ap.add_argument("--keep")
    argv, cl = c2trace.split_cl(argv)
    a = ap.parse_args(argv)
    if c2build.get("arch") != "x64":
        sys.exit("c2sched17: the x64 scheduler of cl 17.00: run with --c2 17.00 (x86 builds: c2trace.py sched)")
    img, hk, rc, out, text = c2trace.run_trace(a.src, hooks(a.ready), cl or ["/O2"], keep=a.keep, func=a.func)
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
        json.dump(dict(c2=c2build.name(), src=os.path.basename(a.src), functions=fns), open(a.export, "w"))
        print("%d functions -> %s" % (len(fns), a.export))
        return 0
    if a.check:
        check(parse(text))
        return 0
    sys.stdout.write(report(parse(text), a.ready))
    return 0




# ---------------------------------------------------------------------------------------------- the replay model
FUSIBLE = {"cmp", "test", "add", "sub", "and", "inc", "dec"}
CAPS = {0: 10, 1: 4, 2: 5, 3: 5, 4: 5}          # SchmdGetK8FuncUnit's caps per unit (sched17.md 1.6)


def simulate(region, issue_rate=3, traced_bonus=False):
    """The order SchSchedGraph issues the region's nodes in, from the traced DAG (successor edges, latencies,
    static priorities, units) by the rules of docs/c2/sched17.md 1.6 / 1.8. Decoder pairing (+8) is taken from the
    trace (dyn - static at issue). -> [lexical index, ...]"""
    sel = region["sel"]
    if not sel or any(s["node"] is None for s in sel):
        return None
    by = {s["node"]: s for s in sel}
    npred = {s["node"]: 0 for s in sel}
    for s in sel:
        for to, lat in s["succ"]:
            if to in npred:
                npred[to] += 1
    est = {n: 0 for n in by}
    dyn = {n: by[n]["static"] for n in by}
    dec = {n: by[n]["dec"] if "dec" in by[n] else k8_decoder(by[n]["opc"], by[n]["srck"], by[n]["dstk"]) for n in by}
    busy = {}
    ready = [n for n in by if npred[n] == 0]
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

    def held(n):
        s = by[n]
        if s["op"] not in FUSIBLE or len(s["succ"]) != 1:
            return False
        to = s["succ"][0][0]
        return to in jcc and npred.get(to, 0) != 1
    guard = 0
    while ready and guard < 100000:
        guard += 1
        issued = []
        ready.sort(key=key)
        if not traced_bonus:
            dynamic(cycle)
        for slot in range(issue_rate):
            ready.sort(key=key)
            pick = None
            for n in ready:
                u = by[n]["u"] or 0
                d = max(busy.get(u, 0), 0)
                if busy.get(u, 0) >= CAPS.get(u, 10) or held(n) or est[n] > cycle + d:
                    continue
                pick = (n, d)
                break
            if pick is None:
                break
            n, d = pick
            ready.remove(n)
            out.append(by[n]["lex"])
            issued.append(n)
            for to, lat in by[n]["succ"]:
                if to in npred:
                    est[to] = max(est[to], (lat & 0xffff) + d + cycle)
                    npred[to] -= 1
                    if npred[to] == 0:
                        ready.append(to)
        for n in issued:
            u = by[n]["u"] or 0
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
            sim = simulate(r)
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


if __name__ == "__main__":
    sys.exit(c2trace.pipe_safe(main, sys.argv[1:]))
