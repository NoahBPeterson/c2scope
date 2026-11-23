#!/usr/bin/env python3
"""Scheduler inverse: which DAG changes give the issue order the original has.  Doc: docs/c2/sched.md

usage: schedwant.py <src> --func F --asm-vs ORIG.s [--asm-func F] [--region K] [--depth 1|2] [--keep DIR | --from-keep DIR] [-- <cl args>]
       schedwant.py <src> --func F --region K --want-lex 1,2,4,3,... [-- <cl args>]

c2sched.py prints what the list scheduler did (heights, successor counts, priorities, issue order). This tool runs
the other way. For each scheduled region whose issue order differs from the original's, it:

  1. finds the original's order: each node's tuple is placed in our listing by its offset in the final IL
     (@Encode@4), that instruction is matched to ORIG.s around the place the two listings align (same text, then
     same text nearby, then same mnemonic in place), and the matched nodes are put in the original's order; when
     that fails, the region's pre-schedule instructions are matched to a window of ORIG.s (mnemonic and the set of
     registers, esp left out); or --want-lex. The pre-schedule IL alone does not find the regions: `_jcc`, a load
     that a broken dependence or a fold into a cmp rewrites later (Mount & Blade, cl 13.10: no region found in any
     function). The last line counts the regions: in the original's order, differing (printed), not located;
  2. replays the scheduler with sched.md rules 5-8 (height = 1 + max(successor height + edge latency), priority =
     (height + successors) << 10 | reads memory << 15 | sets the branch flags << 23, then cycle by cycle the
     unit / busy / earliest-cycle selection c2sched.simulate_p6 checks; a breakable dependence, read off the trace,
     does not hold its consumer back). The traced order is replayed first as a check;
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

x64 (a build table whose `arch` is x64, cl 17.00; docs/c2/sched17.md 6.3): the regions, DAG and replay are
tools/c2sched17.py's (K8 or Atom per function): the DAG predicted from the region's IL (predict_dag, with the
logged alias answers) where it gives the traced order, else the traced one; a perturbation's static priorities are
recomputed as SchComputeStaticPriority does, the replay is c2sched17.simulate with breakable edges. The same edits,
plus
       move:X<Y, move:X>Y   the statement of X moved to just before / after Y: the DAG rebuilt from the moved IL
                        (moves that cross no true register dependence of X; the allocation's anti / output edges
                        between X and the crossed nodes are left out)
"""
import argparse, itertools, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace
import c2sched
import tsweep

REG = r"\b(e[a-d]x|e[sd]i|ebp|[a-d][lhx]|[sd]il?|bp|xmm\d+|mm\d|st\(?\d?\)?|r[a-d]x|r[sd]i|rbp|r(?:8|9|1[0-5])[dwb]?|bpl)\b"
#        ^ the x64 names (r8..r15 and their d / w / b parts, rbp, ...) never occur in 32-bit code
# DAG edge type bits (DagBuildGraph): registers 1 true / 2 anti / 4 output; memory 0x20 store -> load, 0x40 load ->
# store, 0x80 store -> store (read off the traced edges: Spore 0x00598db0 region 2)
ETYPE = {1: "true", 2: "anti", 4: "output", 0x20: "mem-true", 0x40: "mem-anti", 0x80: "mem-output",
         0x2000: "volatile", 0x80000: "control"}
MEM = 0xe0
DELAY_CMP = [int(x, 16) for x in c2trace.c2build.get("sched_delay_cmp", [])]


def tkey(text):
    """(mnemonic, registers) of a pre-schedule tuple as c2sched prints it."""
    op, _, ops = text.partition("|")
    mn = op.split()[0] if op.split() else "?"
    mn = {"_epop": "pop", "_epush": "push"}.get(mn, mn)
    regs = frozenset(re.findall(r"\b(e[a-d]x|e[sd]i|ebp|[a-d][lhx]|xmm\d+|r[a-d]x|r[sd]i|rbp|r(?:8|9|1[0-5])[dwb]?|"
                                r"[sd]il|bpl)[=:]", ops))
    return mn, regs if mn != "call" else frozenset()


def okey(ins):
    mn, ops = ins
    return mn, frozenset(r for r in re.findall(REG, ops) if r not in ("esp", "rsp")) if mn != "call" else frozenset()


def etype(t):
    return "+".join(n for b, n in ETYPE.items() if t & b) or "%x" % t


def _sh(x, n):
    return x << n if n >= 0 else x >> -n


def _prio(ht, ns, fl):
    """Static priority with the build's _SchmdHWT row (c2sched.HWT; 14.00's: (ht + ns) << 10 | L << 15 | B << 23)."""
    w = c2sched.HWT
    return _sh(fl >> 1 & 1, w[0]) + _sh(ht, w[1]) + _sh(ns, w[2]) + _sh(fl & 1, w[3]) + _sh(fl >> 2 & 1, w[5])


def breakable_edges(r, N):
    """{(producer, consumer)}: the traced true edges that did not hold their consumer back (sched.md rule 8:
    SchmdDepIsBreakable / SchmdBreakDep). A node enters the ready list when only breakable predecessors are left; its
    predecessor count at SchAddToReadyList (`np`) is then above 0, and those predecessors are the ones not issued
    before the consumer's cycle. The trace carries no break kind on these builds' edges (13.10's edge +0x16 is 0 on
    Mount & Blade 0x4ea030, whose `mov eax,[esi]` issues before the `lea esi,[ecx+edx*8+4]` that feeds it), so an
    edge that could break but did not is not seen; a replay does not need it."""
    icyc = {}
    for c, ts in r["issue"]:
        for t in ts:
            icyc.setdefault(t, c)
    preds = {}
    for a, b, lat, typ, plat, eb in r["edges"]:
        if a in N and b in N:
            preds.setdefault(b, set()).add(a)
    out = set()
    for t, n in N.items():
        if n.get("np", 0) <= 0:
            continue
        late = sorted((p for p in preds.get(t, ()) if icyc.get(p, 1 << 30) >= n["rdy"]), key=lambda p: -icyc.get(p, 1 << 30))
        out.update((p, t) for p in late[:n["np"]])
    return out


class Region:
    def __init__(self, r, txt):
        self.N = {t: dict(n) for t, n in r["nodes"].items() if t}
        self.txt = txt
        self.edges = [(a, b, lat, typ) for a, b, lat, typ, plat, eb in r["edges"]]
        self.order = [t for c, ts in r["issue"] for t in ts if t]
        self.bylex = {n["lex"]: t for t, n in self.N.items()}
        self.p5 = c2trace.c2build.get("sched_model") == "p5"
        self.ekind = {(a, b): eb & 0x1f for a, b, lat, typ, plat, eb in r["edges"]}
        self.brk = breakable_edges(r, self.N)
        if self.p5:
            # 13.00.9044 carries the marked break kind (c2sched hook 6): every breakable edge (kind != 0) is
            # non-gating, not just the ones that broke in this trace; the address edges (kind >= 0x10) drive AGI
            self.brk = {(a, b) for (a, b), ty in self.ekind.items() if ty and a in self.N and b in self.N}
        # the cycle model needs every node's unit and op (13.00.9044 replays its P5 pairing / AGI model too)
        self.cycles = all("unit" in n and "op" in n for n in self.N.values())

    def name(self, t):
        return "%d:%s" % (self.N[t]["lex"], c2sched.short(self.txt.get(t, "?")).split("<-")[0].strip()[:28])

    def replay(self, edges, nsd=None):
        """Issue order under rules 5-7 for this edge list (nsd: extra successor counts)."""
        N = self.N
        succ, pred = {}, {}
        for a, b, lat, typ in edges:
            succ.setdefault(a, []).append((b, lat))
            if a in N and b in N and (a, b) not in self.brk:     # a breakable edge does not hold b back (rule 8)
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
            # the static priority's bit the rule does not give (an x87 store's +1, HWT[6]) is kept from the trace
            prio[t] = _prio(h(t), ns, n["fl"]) + (n["prio"] - _prio(n["ht"], n["ns"], n["fl"]) if n["prio"] >= _prio(n["ht"], n["ns"], n["fl"]) else 0)
        if not self.cycles:
            return self._shortcut(pred, prio), prio
        lat_in = {}
        for a, b, lat, typ in edges:
            if a in N and b in N:
                lat_in.setdefault(b, []).append((a, lat))
        if self.p5:
            return self._simulate_p5(pred, lat_in, prio), prio
        return self._simulate(pred, lat_in, prio), prio

    def _shortcut(self, pred, prio):
        """sched.md rule 7's shortcut: repeatedly the ready node with the highest (priority, -lex)."""
        N = self.N
        done, out = set(), []
        while len(out) < len(N):
            ready = [t for t in N if t not in done and pred.get(t, set()) <= done]
            if not ready:
                break
            t = min(ready, key=lambda x: (-prio[x], N[x]["lex"]))
            out.append(t)
            done.add(t)
        return out

    def _simulate(self, pred, lat_in, prio):
        """SchSchedGraph / SchSelectInstrs cycle by cycle with the unit model c2sched.simulate_p6 checks against the
        trace (sched.md rule 7): each cycle walk the ready list (priority descending, lex ascending) and issue the
        first node whose unit is free (busy < 3, one node per unit and cycle; unit >= 4 = any, counted on unit 0) and
        whose earliest cycle (the latest issued predecessor's cycle + the edge latency; a node with none: its traced
        est) is <= cycle + busy; then walk again, up to 3 issues. An issue adds the op's cost to the unit's busy
        counter; every cycle subtracts 1. Latency and units matter where integer and x87 code interleave (Mount &
        Blade 0x4dc350) and after imul / idiv, which the shortcut ignores."""
        N = self.N
        issued, out, busy, cyc = {}, [], [0, 0, 0, 0], 0
        while len(out) < len(N) and cyc < 100000:
            used = set()
            for _slot in range(3):
                ready = sorted((t for t in N if t not in issued and pred.get(t, set()) <= issued.keys()),
                               key=lambda x: (-prio[x], N[x]["lex"]))
                pick = None
                for x in ready:
                    u = N[x]["unit"] & 0xff
                    if u >= 4:
                        u = 0 if all(b < 3 for b in busy) else None
                    elif u in used or busy[u] >= 3:
                        u = None
                    if u is None:
                        continue
                    ps = [issued[a] + lat for a, lat in lat_in.get(x, ()) if a in issued]
                    est = max(ps) if ps else N[x]["est"]
                    if est <= cyc + max(busy[u], 0) and not self._delayed(x, pred, issued):
                        pick = (x, u)
                        break
                if pick is None:
                    break
                x, u = pick
                issued[x] = cyc
                out.append(x)
                used.add(u)
                busy[u] = max(busy[u], 0) + c2sched.op_cost(N[x]["op"])[1]
            cyc += 1
            busy = [b - 1 for b in busy]
        return out

    def _simulate_p5(self, pred, lat_in, prio):
        """The 13.00.9044 Pentium replay (c2sched.simulate_p5 / port-1300.md 5), driven by the DAG instead of the
        trace's events: each cycle fills slot 0 then slot 1 from the ready list (priority descending, lex ascending;
        a node is ready once its non-breakable predecessors have issued). Slot 0 needs units u and u+1 free
        (busy < 1), slot 1 must pair with slot 0 (SchmdPairable) and takes u+1; an fdiv waits on its 2-cycle flag,
        an address edge (break kind >= 0x10) whose producer is ready with est <= cycle+1 stalls its consumer
        (SchmdCauseAGI), and right after an fxch an integer node waits for a ready x87 node (SchmdBadForFPSch,
        /Ot). est = the latest issued predecessor's cycle + the edge latency (a node with none: its traced est);
        a pick needs est <= cycle + max(busy[unit], 0). An issue adds the op's cost to busy[unit]; every cycle
        subtracts 1. Slot 1 sees the nodes slot 0's issue freed (the ready list is rebuilt between the slots)."""
        N = self.N
        agi = {}
        for (a, b), ty in self.ekind.items():
            if ty >= 0x10 and a in N and b in N:
                agi.setdefault(b, []).append(a)
        issued, out, busy, cyc, fdiv, last = {}, [], [0, 0, 0, 0], 0, 0, None

        def est(x):
            ps = [issued[a] + lat for a, lat in lat_in.get(x, ()) if a in issued]
            return max([N[x]["est"]] + ps)

        def ready_list():
            return sorted((t for t in N if t not in issued and pred.get(t, set()) <= issued.keys()),
                          key=lambda x: (-prio[x], N[x]["lex"]))

        def badfp(x, ready):
            if last is None or not (N[last]["op"] == 0x5f or
                                    (N[last]["op"] == 0x263 and N[last].get("x", [0, 0, 0])[2] == 0x5f)):
                return False
            if not (1 << (N[x]["x"][0] >> 28) & 0xe):
                return False
            return busy[2] <= 1 and any(N[y]["x"][0] >> 16 & 0xf000 == 0x4000 and est(y) <= cyc + 1 for y in ready)

        while len(out) < len(N) and cyc < 100000:
            first, units = None, []
            for slot in range(2):
                ready = ready_list()
                rset = set(ready)
                pick = None
                for x in ready:
                    X = N[x]
                    if X["op"] == 0x46 and fdiv > 0:
                        continue
                    u = X["unit"] & 0xffff
                    if u >= 4:
                        u = 0 if all(b < 1 for b in busy) else None
                    elif slot == 0:
                        u = u if busy[u] < 1 and busy[u + 1] < 1 else None
                    else:
                        u = u + 1 if first is not None and c2sched._p5_pairable(N[first], X) else None
                    if u is None:
                        continue
                    if any(p in rset and est(p) <= cyc + 1 for p in agi.get(x, ())):
                        continue
                    if badfp(x, ready) or est(x) > cyc + max(busy[u], 0):
                        continue
                    pick = (x, u)
                    break
                if pick is None:
                    break
                x, u = pick
                issued[x] = cyc
                out.append(x)
                units.append((x, u))
                if slot == 0:
                    first = x
            for x, u in units:                   # the costs land after both slots (SchSchedGraph 15e0d)
                if 0 < N[x]["op"] < 0x246:
                    busy[u] = max(busy[u], 0) + c2sched.op_cost(N[x]["op"])[1]
                    if N[x]["op"] == 0x46:
                        fdiv = 2
                last = x
            cyc += 1
            busy = [b - 1 for b in busy]
            fdiv -= 1
        return out

    def _delayed(self, x, pred, issued):
        """cl 15.00 (table key sched_delay_cmp): SchmdShouldDelayCmp keeps a cmp/test whose one successor is a jcc
        back while that jcc has another unissued predecessor (c2sched.simulate_p6)."""
        dly = DELAY_CMP
        N = self.N
        if not dly or N[x]["op"] not in dly[:2]:
            return False
        succ = {b for a, b, _l, _t in self.edges if a == x}
        if len(succ) != 1:
            return False
        j = next(iter(succ))
        if j not in N or N[j]["op"] != dly[2]:
            return False
        xx, jx = N[x].get("x"), N[j].get("x")
        if xx and jx and not (xx[1] & 0xff == 1 or xx[2] & 0xff == 1):
            return False
        if xx and jx and N[x]["op"] == dly[0] and (jx[0] >> 16 & 0x1f) not in (1, 2, 7, 8, 9, 10):
            return False
        return len([p for a, p, _l, _t in self.edges if p == j and a in N and a not in issued]) != 1


def anchor_map(ours, orig, text=False):
    """{our listing index: orig index} over the equal blocks of an alignment on (mnemonic, registers); text: on the
    whole instruction text first, the gaps between its blocks then on (mnemonic, registers) (x64: in a short
    function `mov [rcx], r8d` and `mov [rcx+4], r8d` have one key, and a store moved past them anchors wrongly)."""
    import difflib
    a = [okey(x) for x in ours]
    b = [okey(x) for x in orig]
    m = {}
    if not text:
        for i, j, n in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
            for k in range(n):
                m[i + k] = j + k
        return m
    ta, tb = [_norm(x) for x in ours], [_norm(x) for x in orig]
    pi = pj = 0
    for i, j, n in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_matching_blocks():
        for i2, j2, n2 in difflib.SequenceMatcher(None, a[pi:i], b[pj:j], autojunk=False).get_matching_blocks():
            for k in range(n2):
                m[pi + i2 + k] = pj + j2 + k
        for k in range(n):
            m[i + k] = j + k
        pi, pj = i + n, j + n
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


def final_offsets(log, func, hk):
    """{tuple id: listing offset} from the final IL at @Encode@4 (hook index hk), as regmap / scratch read it."""
    import c2scratch
    final, on = [], False
    for ln in log.splitlines():
        if ln.startswith("T "):
            if on:
                final.append(ln[2:])
            continue
        if "|" not in ln or ln.startswith("#"):
            continue
        head, _, rest = ln.partition("|")
        f = head.split()
        on = False
        if f[0] == "E" and int(f[3]) == hk:
            name = c2trace._split(rest)[0].strip('"')
            if c2trace.func_match(func, name):
                final, on = [], True
    return {int(k, 16): o for k, (o, _op) in c2scratch.offsets(final).items()}


def _norm(ins):
    return ins[0].lower(), re.sub(r"\s+", " ", ins[1].lower()).strip()


def want_from_listing(reg, offmap, ours, orig, amap, near=8, loose=False):
    """The original's order of this region's instructions, read through our listing: each node's tuple is placed at
    its offset in our final code (offmap, from @Encode@4) and that instruction is matched to one of the original's,
    around the place anchor_map aligns it with (the nearest anchored neighbour's diagonal): the same text there; else
    the same text at most `near` instructions away (an instruction the original issues elsewhere); else the
    instruction there when it has the same mnemonic (other registers: an allocation difference, not the order);
    else the same mnemonic and registers nearby. The matched nodes are put in the original's order in the slots they
    have in the traced order; nodes the final code dropped or changed (a load folded into a cmp, a label) keep their
    slot. -> (lex order, orig window start, nodes matched) or None when the region cannot be placed."""
    at = {o: i for i, o in enumerate(ours.addrs)}
    pos = {t: at[offmap[t]] for t in reg.order if offmap.get(t) in at}
    if len(pos) < 2 or not amap:
        return None
    anchors = sorted(amap)
    import bisect

    def diag(p):
        k = bisect.bisect_left(anchors, p)
        cand = [anchors[x] for x in (k - 1, k) if 0 <= x < len(anchors)]
        a = min(cand, key=lambda x: abs(x - p))
        return p + amap[a] - a if abs(a - p) <= 2 * near else None
    on = [_norm(x) for x in orig]
    mine = {t: _norm(ours.cs[pos[t]]) for t in pos}
    dj = {t: diag(pos[t]) for t in pos}
    used, match = set(), {}

    def take(t, j):
        match[t] = j
        used.add(j)
    ts = sorted((t for t in pos if dj[t] is not None), key=lambda t: pos[t])
    for t in ts:                                        # 1. the same text in place
        if 0 <= dj[t] < len(on) and on[dj[t]] == mine[t] and dj[t] not in used:
            take(t, dj[t])
    for key, far in ((lambda x: x, True), (None, False), (okey, True)):
        for t in ts:
            if t in match:
                continue
            if not far:                                 # 3. the same mnemonic in place
                if 0 <= dj[t] < len(on) and dj[t] not in used and on[dj[t]][0] == mine[t][0]:
                    take(t, dj[t])
                continue
            js = [j for j in range(max(0, dj[t] - near), min(len(on), dj[t] + near + 1))
                  if j not in used and key(on[j]) == key(mine[t])]
            if js:                                      # 2. the same text nearby; 4. same mnemonic and registers
                take(t, min(js, key=lambda j: abs(j - dj[t])))
    if loose:       # 5. (x64) the same mnemonic nearby: another allocation (edx / r8d) in a reordered stretch
        for t in ts:
            js = [j for j in range(max(0, dj[t] - near), min(len(on), dj[t] + near + 1))
                  if j not in used and on[j][0] == mine[t][0]] if t not in match else []
            if js:
                take(t, min(js, key=lambda j: abs(j - dj[t])))
    if len(match) < 2:
        return None
    slots = [k for k, t in enumerate(reg.order) if t in match]
    order = list(reg.order)
    for k, t in zip(slots, sorted(match, key=lambda t: match[t])):
        order[k] = t
    return order, min(match.values()), len(match)


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
    "move": "(x64) the statement of X moved to just before (<) / after (>) Y in the source: the DAG is rebuilt "
            "from the moved IL; only moves that cross none of X's register dependences are tried",
}


def analyse(reg, want, a):
    L = lambda t: reg.N[t]["lex"]
    base, prio = reg.replay(reg.edges)
    print("   traced order : %s" % " ".join(str(L(t)) for t in reg.order))
    print("   replayed     : %s%s" % (" ".join(str(L(t)) for t in base),
                                       "  (= traced)" if base == reg.order else "  (model differs from the trace%s: its answers are not reliable)" % (
                                           "" if reg.cycles else "; replayed by the priority shortcut, the build's "
                                           "P5 pairing / AGI model is not")))
    print("   wanted       : %s" % " ".join(str(L(t)) for t in want))
    p0 = prefix(base, want)
    if base == want:
        print("   the replay already gives the wanted order")
        return
    if p0 >= len(base):
        print("   the replay stops after %d of %d nodes" % (len(base), len(want)))
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
    for label, o in (reg.moves() if hasattr(reg, "moves") else []):
        res.append((o == want, prefix(o, want), -1, ((label, "move", [], {}),), o))
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
            print("     %-11s %s" % (k, getattr(reg, "meaning", MEANING)[k]))
    if a.edges:
        print("   edges (from > to: type, latency):")
        for e in reg.edges:
            if e[0] in reg.N:
                print("     %s > %s: %s, %d" % (reg.name(e[0]), reg.name(e[1]) if e[1] in reg.N else "exit", etype(e[3]), e[2]))


# ------------------------------------------------------------------------------------------ x64 (cl 17.00)
# The table's `arch` is x64: the regions, DAG and replay are tools/c2sched17.py's (docs/c2/sched17.md): its probes
# (with the IL, `--il`), the DAG predicted from the IL (predict_dag, 6.1; exact on zlib with the logged alias
# answers) or, where that one does not give the traced order, the traced DAG, and its replay (simulate, K8 or Atom
# per function, breakable edges). A perturbation edits the DAG's edge list; heights, successor counts and the
# critical-path bit are recomputed as SchComputeStaticPriority does (1.5), B / L / S are kept.

LATE, FWD = 1 << 30, 1 << 29     # edge type bits of our own: made in the forward pass (no height), after the
#                                  producer's priority (no nsucc, no critical path either); c2sched17.predict_dag


def _eflags(e):
    return (LATE if e.get("late") else 0) | (FWD if e.get("fwd") else 0)


class Region17:
    """One x64 region in the shape analyse() reads: nodes keyed by lexical index (1..N; 0 the entry, N+1 the exit
    are not nodes), edges (from, to, latency word, type) with the entry / exit ones, the traced issue order."""

    def __init__(self, S, r, model, info):
        self.S, self.model, self.r, self.info = S, model, r, info
        self.order = [s["lex"] for s in r["sel"]]
        self.txt = {n["lex"]: (n["il"].split(None, 2)[2] if n.get("il") and n["il"] != "L" else "label | ")
                    for n in r.get("nodes") or []}
        self.tid = {n["lex"]: n["il"].split()[1] for n in r.get("nodes") or [] if n.get("il") and n["il"] != "L"}
        self.nn = len(r.get("nodes") or [])
        self.how, sel = None, None
        if r.get("nodes") and all("etype" in s for s in r["sel"]):
            nodes = S.predict_dag(r, True, model, info)
            ps = S.predicted_sel(r, nodes, model)
            if S.simulate(dict(sel=ps), breaks=True, model=model) == self.order:
                self.how, sel = "predicted from the IL", ps
                self.entry = [(0, e["to"], e["lat"] | e.get("brk", 0) << 16, e["type"] | _eflags(e)) for e in
                              nodes[0]["succ"]]
                for x in sel:
                    x["etype"] = [t | LATE * lt | FWD * fw for t, lt, fw in zip(x["etype"], x["late"], x["fwd"])]
        if sel is None:            # the traced DAG (a broken breakable edge is not in it)
            lexof = {s["node"]: s["lex"] for s in r["sel"]}
            ex = (self.nn or len(r["sel"])) + 1
            sel = []
            for s in r["sel"]:
                x = dict(s)
                x["node"] = s["lex"]
                x["succ"] = [(lexof.get(to, ex), lat) for to, lat in s["succ"]]
                x["etype"] = s.get("etype") or [0] * len(s["succ"])
                sel.append(x)
            have = {to for s in sel for to, _ in s["succ"]}
            self.entry = [(0, s["lex"], 0, 0x80000) for s in sel if s["lex"] not in have]
            ok = S.simulate(dict(sel=sel), breaks=True, model=model) == self.order
            self.how = "traced" + ("" if ok else " (replay differs)")
        self.nn = max(self.nn, len(sel))
        self.sel = {s["lex"]: s for s in sel}
        self.edges = list(self.entry) + [(s["lex"], to, lat, ty) for s in sel for (to, lat), ty in
                                         zip(s["succ"], s.get("etype") or [0] * len(s["succ"]))]
        self.N = {}
        for s in sel:
            st = s["static"]
            self.N[s["lex"]] = dict(lex=s["lex"], ht=s["height"], ns=len(s["succ"]), prio=st, fl=s["flags"] & 0xff,
                                    bls=st & (1 << 23 | 1 << 15 | 1))
        self.bylex = {t: t for t in self.N}
        self.brk = set()
        self.cycles = True
        self.meaning = dict(MEANING, **MEANING17)
        base, prio = self.replay(self.edges)
        self.static_same = sum(prio[t] == self.N[t]["prio"] for t in self.N)

    def moves(self, limit=40):
        """Statement moves: each node X moved to just before / after another node Y in the IL, the DAG rebuilt
        from the moved IL (predict_dag) and scheduled. Only when the region's DAG is the predicted one, and only
        moves that cross no register dependence of X (true, anti, output: the moved code computes the same
        values); memory edges may be crossed, which is the source edit (a store or load moved past others). ->
        [(label, order in this region's numbering)]"""
        if not self.how.startswith("predicted") or not self.r.get("nodes"):
            return []
        reg_e = {}
        for a, b, lat, ty in self.edges:
            if ty & 1 and a in self.N and b in self.N:       # a true register dependence: the value itself
                reg_e.setdefault(a, set()).add(b)
                reg_e.setdefault(b, set()).add(a)
        nodes = sorted(self.r["nodes"], key=lambda n: n["lex"])
        last = nodes[-1]["lex"]
        if len(nodes) > 2 * limit:      # a long region: moves of at most limit / 3 nodes (cost: one DAG each)
            limit = max(4, limit // 3)
        out, seen = [], set()
        for x in sorted(self.N):
            if x == last:
                continue
            for y in sorted(self.N):
                if y == x or y == last and y < x:
                    continue
                for where in ("<", ">"):
                    lo, hi = (y, x - 1) if y < x else (x + 1, y)
                    if where == "<" and y > x:
                        hi = y - 1
                    if where == ">" and y < x:
                        lo = y + 1
                    if lo > hi or hi - lo + 1 > limit or y == last and where == ">":
                        continue
                    if any(z in reg_e.get(x, ()) for z in range(lo, hi + 1)):
                        continue
                    seq = [n["lex"] for n in nodes if n["lex"] != x]
                    k = seq.index(y)
                    seq.insert(k if where == "<" else k + 1, x)
                    if seq == [n["lex"] for n in nodes] or tuple(seq) in seen:
                        continue
                    seen.add(tuple(seq))
                    o = self._moved(seq, x, set(range(lo, hi + 1)))
                    if o:
                        out.append(("move:%d%s%d" % (x, where, y), o))
        return out

    def _moved(self, seq, x, crossed):
        """The issue order of the region with its IL tuples in the order `seq` (old lexical indices), X moved past
        `crossed`. The registers stay the traced allocation's, so the register edges the move creates between X
        and the crossed nodes (anti / output reuses, which another allocation would not have) are left out."""
        S = self.S
        new = {old: k + 1 for k, old in enumerate(seq)}
        back = {v: k for k, v in new.items()}
        r = dict(self.r)
        r["nodes"] = [dict(n, lex=new[n["lex"]]) for n in self.r["nodes"]]
        r["sel"] = [dict(x, lex=new.get(x["lex"], x["lex"])) for x in self.r["sel"]]
        if self.r.get("kill"):
            r["kill"] = {str(new.get(int(k), int(k))): v for k, v in self.r["kill"].items()}
        try:
            nodes = S.predict_dag(r, True, self.model, self.info)
            ps = {e["lex"]: e for e in S.predicted_sel(r, nodes, self.model)}
        except (KeyError, IndexError, ValueError):
            return None
        xs, cs = new[x], {new[z] for z in crossed}
        edges = []
        for nd in nodes[:-1]:
            for e in nd["succ"]:
                a, b = nd["lex"], e["to"]
                if ((a == xs and b in cs) or (b == xs and a in cs)) and e["type"] & 7 and not e["type"] & ~7:
                    continue
                edges.append((a, b, e["lat"] | e.get("brk", 0) << 16, e["type"] | _eflags(e)))
        o, _ = replay17(S, self.model, ps, edges, len(nodes) - 2)
        return [back[t] for t in o] if o else None

    def name(self, t):
        return "%d:%s" % (t, c2sched.short(self.txt.get(t, "?")).split("<-")[0].strip()[:28])

    def replay(self, edges, nsd=None):
        """SchComputeStaticPriority over this edge list (heights backward from the exit, the critical path from the
        entry, nsucc = out-edges + nsd), then c2sched17.simulate with breakable edges. -> (order, static)"""
        return replay17(self.S, self.model, self.sel, edges, self.nn, nsd)


def replay17(S, model, sel, edges, nn, nsd=None):
    """Static priorities (1.5) of the nodes `sel` ({lex: issue-probe / predicted_sel entry}) over `edges` (from,
    to, latency word, type; 0 = entry, nn + 1 = exit), B / L / S kept from each entry; then the replay.
    -> (order, {lex: static})"""
    ex = nn + 1
    succ = {}
    for a, b, lat, ty in edges:
        succ.setdefault(a, []).append((b, lat, ty))
    ht = {ex: 0}
    for t in sorted(set(sel) | {0}, reverse=True):
        ht[t] = 1 + max([ht.get(b, 0) + (lat & 0xffff) for b, lat, ty in succ.get(t, ()) if not ty & FWD] or [0])
    crit = {0}
    for t in sorted(set(sel) | {0}):
        if t in crit:
            for b, lat, ty in succ.get(t, ()):
                if not ty & LATE and ht[t] == ht.get(b, 0) + (lat & 0xffff) + 1:
                    crit.add(b)
    prio, out = {}, []
    for t, x0 in sel.items():
        ns = max(0, sum(not ty & LATE for b, lat, ty in succ.get(t, ())) + (nsd or {}).get(t, 0))
        f = x0["flags"]      # B, L from the node's flags, S (a store of type class 5) from its static priority;
        #                      the fields are added, not or-ed: 32 successors carry into L as in the DLL
        p = ((f >> 1 & 1) << 23) + ((f >> 2 & 1) << 15) + (x0["static"] & 1) + (min(ht[t], 0xffff) << 5) + \
            (ns << 10) + ((t in crit) << 1)
        prio[t] = p
        x = dict(x0)
        x.update(static=p, dyn=p, height=ht[t], node=t, flags=(x["flags"] & ~1) | (t in crit),
                 succ=[(b, lat) for b, lat, ty in succ.get(t, ())],
                 etype=[ty & ~(LATE | FWD) for b, lat, ty in succ.get(t, ())])
        out.append(x)
    out.sort(key=lambda x: x["lex"])
    return S.simulate(dict(sel=out), breaks=True, model=model) or [], prio


def final_offsets17(log, func, hk):
    """{IL tuple id: listing offset} from the final IL at @Encode@4 (hook index hk; the log may interleave worker
    threads, `# thread N`)."""
    import c2scratch
    final, cur, th = {}, {}, 0
    for ln in log.splitlines():
        if ln.startswith("# thread "):
            th = int(ln.split()[2])
            continue
        if ln.startswith("T "):
            if th in cur:
                cur[th].append(ln[2:])
            continue
        if "|" not in ln or ln.startswith("#"):
            continue
        head, _, rest = ln.partition("|")
        f = head.split()
        cur.pop(th, None)
        if f[0] == "E" and int(f[3]) == hk:
            if c2trace.func_match(func, c2trace._split(rest)[0].strip('"')):
                final[th] = cur[th] = []
    out = {}
    for lines in final.values():
        out.update({int(k, 16): o for k, (o, _op) in c2scratch.offsets(lines).items()})
    return out


MEANING17 = {
    "unalias": "X stops interfering with the region's other memory accesses (x64: their points-at sets do not meet, "
               "sched17.md 6.1 / 6.2): reach both through the same base pointer at disjoint offsets, read a local "
               "whose address is never taken, or memory through a pointer argument against a local",
    "alias": "X becomes a store whose points-at set meets the region's other stores: through another pointer "
             "variable (a copy loaded from memory, a pointer advanced in a loop) or an index that is not a "
             "constant, which pins the earlier stores above it and the later ones below it",
}


def main17(a, cl):
    """schedwant on the x64 scheduler (c2sched17)."""
    import tempfile, shutil
    import c2sched17 as S, c2il
    os.environ.setdefault("C2TRACE_ILPAS", "28")
    hooks = S.hooks(False, il=True)
    hk = len(hooks)
    hooks = hooks + ["@Encode@4" + c2il.SPEC]
    cl = cl or ["/O2"]
    if a.from_keep:
        kd, rc, out = a.from_keep, 0, ""
        log = open(os.path.join(kd, "c2trace.log"), errors="replace").read()
    else:
        kd = a.keep or tempfile.mkdtemp(prefix="schedwant_")
        img, _hk, rc, out, log = c2trace.run_trace(a.src, hooks, cl, keep=kd, quiet=True, func=a.func)
    ours = None
    try:
        ours = tsweep.listing_instrs(os.path.join(kd, os.path.splitext(os.path.basename(a.src))[0] + ".asm"),
                                     a.asm_func or a.func)
    except (KeyError, OSError):
        pass
    if not a.keep and not a.from_keep:
        shutil.rmtree(kd, ignore_errors=True)
    if rc:
        sys.stdout.write(out)
    fns = [f for f in S.parse(log) if c2trace.func_match(a.func, f["func"]) and any(r["sel"] for r in f["regions"])]
    if not fns:
        sys.exit("function %r never reached Schedule" % a.func)
    orig = tsweep.orig_instrs(a.asm_vs) if a.asm_vs else None
    if a.want_src:
        d = tempfile.mkdtemp(prefix="schedwant_")
        c2trace.run_trace(a.want_src, S.hooks(False), cl, keep=d, quiet=True, func=a.func)
        asm = os.path.join(d, os.path.splitext(os.path.basename(a.want_src))[0] + ".asm")
        try:
            orig = [tuple((x.split(None, 1) + [""])[:2]) for o, x in
                    sorted(tsweep.listing_cs_offsets(asm, a.asm_func or a.func).items())]
        except KeyError:
            orig = None
        shutil.rmtree(d, ignore_errors=True)
        if not orig:
            sys.exit("--want-src: %s not in the variant's listing" % a.func)
    if ours:
        ours.cs = tsweep.cs_text(ours)
    amap = anchor_map(ours.cs, orig, text=True) if ours and orig else None
    offid = final_offsets17(log, a.func, hk) if ours and getattr(ours, "addrs", None) else {}
    stats = dict(same=0, differ=0, lost=0)
    f = fns[0]
    model = f.get("model", "k8")
    info = S.fn_alias_info(f)
    regions = [r for r in f["regions"] if r["sel"]]
    print("== %s: %d regions (x64, %s model)" % (f["func"], len(regions), model))
    for k, r in enumerate(regions):
        if a.region is not None and k != a.region:
            continue
        if len(r["sel"]) < 2:
            continue
        reg = Region17(S, r, model, info)
        if a.want_lex:
            want = [int(x) for x in a.want_lex.split(",") if int(x) in reg.N]
            want += [t for t in sorted(reg.N) if t not in want]
            print("-- region %d (%d instrs)" % (k, len(reg.N)))
        else:
            offmap = {t: offid[int(i, 16)] for t, i in reg.tid.items() if int(i, 16) in offid}
            w = want_from_listing(reg, offmap, ours, orig, amap, loose=True) if offmap and amap else None
            how = "through our listing"
            if w is None:
                w = want_from_orig(reg, orig, ours if amap else None, amap)
                how = "by instruction keys"
            if w is None:
                stats["lost"] += 1
                if a.region is not None:
                    print("-- region %d: no window of the original matches its instructions" % k)
                continue
            want, s, sc = w
            if want == reg.order:
                stats["same"] += 1
                if a.region is not None:
                    print("-- region %d: the original has the traced order (%d of %d instructions matched %s)" % (
                        k, sc, len(reg.N), how))
                continue
            stats["differ"] += 1
            print("-- region %d (%d instrs; original window at line %d, %d of %d instructions matched %s)" % (
                k, len(reg.N), s + 1, sc, len(reg.N), how))
        print("   DAG: %s; static priorities recomputed from it: %d of %d equal" % (reg.how, reg.static_same,
                                                                                   len(reg.N)))
        for t in sorted(reg.N):
            n = reg.N[t]
            print("   %3d ht %2d ns %d %-4s %8x  %s" % (t, n["ht"], n["ns"], c2sched.flags(n["fl"]), n["prio"],
                                                     c2sched.short(reg.txt.get(t, "?"))))
        analyse(reg, want, a)
    if not a.want_lex:
        print("# %d regions with 2+ instructions: %d in the original's order, %d differ%s, %d not located in the original%s" % (
            sum(stats.values()), stats["same"], stats["differ"], " (above)" if stats["differ"] else "", stats["lost"],
            "" if offid else " (no final-IL offsets: matched by pre-schedule instruction keys only)"))
    return rc


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
    ap.add_argument("--keep", help="keep the traced compile (log, listing) in this directory")
    ap.add_argument("--from-keep", metavar="DIR", help="no compile: read the trace and listing a --keep run left in DIR")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    if not a.asm_vs and not a.want_lex and not a.want_src:
        ap.error("need --asm-vs ORIG.s, --want-src VARIANT.cpp or --region K --want-lex ...")
    if c2trace.c2build.get("arch") == "x64":
        return main17(a, cl)
    import tempfile, shutil, c2scratch
    # the final IL at @Encode@4 too (scratch's hook 3): it places every scheduled tuple at its offset in our listing
    enc = [h for h in c2scratch.HOOKS if h.startswith("@Encode@")]
    hooks = c2sched.HOOKS + enc[:1]
    if a.from_keep:
        kd, rc, out = a.from_keep, 0, ""
        log = open(os.path.join(kd, "c2trace.log"), errors="replace").read()
    else:
        kd = a.keep or tempfile.mkdtemp(prefix="schedwant_")
        img, hk, rc, out, log = c2trace.run_trace(a.src, hooks, cl, keep=kd, quiet=True)
    ours = None
    try:
        ours = tsweep.listing_instrs(os.path.join(kd, os.path.splitext(os.path.basename(a.src))[0] + ".asm"),
                                     a.asm_func or a.func)
    except (KeyError, OSError):
        pass
    if not a.keep and not a.from_keep:
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
        try:
            orig = [tuple((x.split(None, 1) + [""])[:2]) for o, x in sorted(tsweep.listing_cs_offsets(asm, a.asm_func or a.func).items())]
        except KeyError:
            orig = None
        if not orig:
            sys.exit("--want-src: %s not in the variant's listing" % a.func)
    if ours:
        ours.cs = tsweep.cs_text(ours)          # capstone's text of our bytes: the form of orig.s
    amap = anchor_map(ours.cs, orig) if ours and orig else None
    offmap = final_offsets(log, a.func, len(c2sched.HOOKS)) if enc and ours and getattr(ours, "addrs", None) else {}
    stats = dict(same=0, differ=0, lost=0)
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
                w = want_from_listing(reg, offmap, ours, orig, amap) if offmap and amap else None
                how = "through our listing"
                if w is None:
                    w = want_from_orig(reg, orig, ours if amap else None, amap)
                    how = "by instruction keys"
                if w is None:
                    stats["lost"] += 1
                    if a.region is not None:
                        print("-- region %d: no window of the original matches its instructions" % k)
                    continue
                want, s, sc = w
                if want == reg.order:
                    stats["same"] += 1
                    if a.region is not None:
                        print("-- region %d: the original has the traced order (%d of %d instructions matched %s)" % (
                            k, sc, len(reg.N), how))
                    continue
                stats["differ"] += 1
                print("-- region %d (%d instrs; original window at line %d, %d of %d instructions matched %s)" % (
                    k, len(reg.N), s + 1, sc, len(reg.N), how))
            if a.want_lex:
                print("-- region %d (%d instrs)" % (k, len(reg.N)))
            for t in sorted(reg.N, key=lambda t: reg.N[t]["lex"]):
                n = reg.N[t]
                print("   %3d ht %2d ns %d %-4s %6x  %s" % (n["lex"], n["ht"], n["ns"], c2sched.flags(n["fl"]), n["prio"],
                                                        c2sched.short(txt.get(t, "?"))))
            analyse(reg, want, a)
    if not a.want_lex:
        print("# %d regions with 2+ instructions: %d in the original's order, %d differ%s, %d not located in the original%s" % (
            sum(stats.values()), stats["same"], stats["differ"], " (above)" if stats["differ"] else "", stats["lost"],
            "" if offmap else " (no final-IL offsets: matched by pre-schedule instruction keys only)"))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
