#!/usr/bin/env python3
"""A model of NColor, the x64 register allocator of cl 17.00 (docs/c2/ncolor.md), checked against traces.

usage: ncolsim.py prio   EXPORT.json [EXPORT.json ..]      the priority formula against every traced priority
       ncolsim.py order  EXPORT.json [..]                  the processing order (buckets, sort) against the trace
       ncolsim.py replay EXPORT.json [..] [--show N]       the register choice: each candidate, in the traced order,
                                                           against the traced initial availability
       ncolsim.py sim    EXPORT.json [..] [--show N]       the whole run from the traced start: the model's own order
                                                           (within the traced buckets) and choices, against the trace
       ncolsim.py whatif EXPORT.json --func F EDIT [EDIT..] the registers after an edit, against the unedited run:
                                                           +app:VAR:B[:N] / -app:VAR:B[:N] (N more / fewer
                                                           appearances of VAR in block B), prio:VAR=P

EXPORT.json: `tools/c2ncol.py SRC --export FILE -- FLAGS` (candidates with appearances and ranges, the liveness
arrays, the initial availability lists of rax..r15).
"""
import json, sys

# the default integer order (NColMdGetPrefRegOrder: T2's scratch half, then T1's callee-saved, T2's callee-saved)
INT_ORDER = ["rax", "rcx", "rdx", "r8", "r9", "r10", "r11", "rbx", "rdi", "rsi", "rbp", "r14", "r15", "r12", "r13"]
NUM = {n: i + 1 for i, n in enumerate("rax rcx rdx rbx rsp rbp rsi rdi r8 r9 r10 r11 r12 r13 r14 r15".split())}
SAVED = {"rbx", "rdi", "rsi", "rbp", "r12", "r13", "r14", "r15"}
ALIAS = {}
for full, parts in dict(rax="eax ax al ah", rcx="ecx cx cl ch", rdx="edx dx dl dh", rbx="ebx bx bl bh",
                        rsi="esi si sil", rdi="edi di dil", rbp="ebp bp bpl", rsp="esp sp spl").items():
    for p in [full] + parts.split():
        ALIAS[p] = full
for k in range(8, 16):
    for w in ("", "d", "w", "b"):
        ALIAS["r%d%s" % (k, w)] = "r%d" % k
XMM_ORDER = ["xmm%d" % k for k in range(16)]          # the xmm family's order is numeric (docs/c2/ncolor.md 4.2)
for k in range(16):
    NUM["xmm%d" % k] = 144 + k
    for pre in ("", "d", "i", "f"):
        for suf in ("", "h", "l", "s"):
            ALIAS["%sxmm%d%s" % (pre, k, suf)] = "xmm%d" % k
    ALIAS["ymm%d" % k] = ALIAS["ymm%dh" % k] = "xmm%d" % k
SAVED |= {"xmm%d" % k for k in range(6, 16)}            # xmm6..15 are callee-saved on Win64
XBANKS = (320, 336)          # fxmmNl (doubles), fxmmNs (floats): the availability lists the export has
XMASTER = 112                # ymmN: the master of the xmm family (preference windows are kept per master)


FULL = "rax rcx rdx rbx rsp rbp rsi rdi r8 r9 r10 r11 r12 r13 r14 r15".split()


def regname(n):
    """_RegName number -> the full register: 1..16 rax..r15, 17..32 their 32-bit names, 33..48 16-bit, 49..64
    8-bit; 112..351 the ymm / xmm family (xmm, dxmm, ixmm, fxmm and their h / l / s parts) -> xmmN, N = (n - 144)
    mod 16 (docs/c2/port-1700.md 3)."""
    if 1 <= n <= 64:
        return FULL[(n - 1) % 16]
    if 112 <= n <= 351:
        return "xmm%d" % ((n - 144) % 16)
    return None


def load(paths):
    out = []
    for p in paths:
        d = json.load(open(p))
        for f in d["functions"]:
            f["file"] = p
            out.append(f)
    return out


def blockof(st, t):
    return st["blk"][t] if 0 <= t < len(st["blk"]) else None


def depth(st, b):
    i = b * 13 + 11
    return st["info"][i] if i < len(st["info"]) else 0


def prio(c, st):
    """NColPrioritizeCandidates' L+0x48 (docs/c2/ncolor.md 2), or None without the data. Global: per range, every
    block from the range's first to its last: k appearances of the range in it give ((maxLive+4) - live) * k * 8,
    none -live; each shifted by 2 * zone depth when optimizing for speed; a hole between two ranges inside one block
    costs that block's -live once more (measured: 1044 of 1053 globals of zlib exact; the rest have an appearance
    listed twice for one tuple). Local (first and last tuple in one block): nTuples - length."""
    if not c.get("ranges") or st is None:
        return None
    blk = lambda t: blockof(st, t)
    if blk(c["first"]) is None or blk(c["last"]) is None:
        return None
    if blk(c["first"]) == blk(c["last"]):
        return st["ntup"] - (c["last"] - c["first"])
    sh = lambda b: 2 * depth(st, b) if st["speed"] else 0
    live = lambda b: st["live"][b] if b < len(st["live"]) else 0
    total, rg = 0, c["ranges"]
    for i, (lo, hi) in enumerate(rg):
        if blk(lo) is None or blk(hi) is None:
            return None
        for b in range(blk(lo), blk(hi) + 1):
            k = sum(1 for a in c["apps"] if blk(a) == b and lo <= a <= hi)
            total += (((st["maxlive"] + 4) - live(b)) * k * 8 << sh(b)) if k else -(live(b) << sh(b))
        if i + 1 < len(rg) and blk(hi) == blk(rg[i + 1][0]):
            total -= live(blk(hi)) << sh(blk(hi))
    return total


def cmd_prio(fns):
    n = ok = skipped = 0
    bad = []
    for f in fns:
        st = f.get("state")
        for c in f["cands"]:
            p = prio(c, st)
            if p is None or c["prio"] is None:
                skipped += 1
                continue
            n += 1
            # the trace logs the 32-bit word as unsigned hex
            tp = c["prio"] - (1 << 32) if c["prio"] & 0x80000000 else c["prio"]
            if p == tp:
                ok += 1
            else:
                bad.append((f["func"], c["name"], tp, p))
    print("priority: %d of %d candidates as traced (%d without data)" % (ok, n, skipped))
    for b in bad[:20]:
        print("  %s %s: traced %d, model %d" % b)
    return 0 if ok == n else 1


def covers(av, ranges):
    """Every range [lo, hi] of the candidate inside one available interval of the register."""
    return all(any(b <= lo and hi <= e for b, e in av) for lo, hi in ranges)


def cut(av, ranges):
    """The availability with the candidate's ranges taken out."""
    out = list(av)
    for lo, hi in ranges:
        nxt = []
        for b, e in out:
            if hi < b or e < lo:
                nxt.append((b, e))
                continue
            if b < lo:
                nxt.append((b, lo))
            if hi < e:
                nxt.append((hi, e))
        out = nxt
    return out


WINDOW_GOES = True   # a lifetime's preference window stops counting once it has been processed
STRICT = False       # a window must overlap the range by more than a shared end tuple
RECENT = 1     # a scratch register must have been free this many tuples before the region (non-int: 3)


def free_since(av, lo):
    """How long before tuple lo the register has been free (the start of the interval holding lo), or None."""
    for b, e in av:
        if b <= lo <= e:
            return lo - b
    return None


ANTI = {0: ("r12",), 1: ("rbp", "r13"), 2: ("r12", "rbp", "r13")}   # TLS+0x119c8 / cc / d0


def order_walk(av, ranges, recent=RECENT, prefwin=None, own=None, tied=None, avoid=(), use=None, order=None):
    """NColDeterminePhysregAvail's order loop (docs/c2/ncolor.md 4.3) over registers that cover every range:
    a register (not the candidate's tied one) inside another lifetime's preference window overlapping the first
    range is fallback A (callee-saved) or B (scratch, first only); a callee-saved register returns fallback C if
    there is one, else itself; a scratch register free for `recent` tuples before the region returns at once, else
    it becomes fallback C when its gap is larger. At the end: C, else B, else A."""
    fa = fb = fc = None
    fcgap = -1
    lo, hi = ranges[0]
    tied = NUM.get(regname(tied)) if tied else None
    order = order or INT_ORDER
    for r in [x for x in order if x not in avoid] + [x for x in order if x in avoid]:
        n = NUM[r]
        if n not in av:
            continue
        if use is not None and r not in use:
            continue
        if not covers(av[n], ranges):
            continue
        if prefwin is not None and n != tied and any(
                (b < hi and lo < e if STRICT else b <= hi and lo <= e) and (b, e) != own for b, e in prefwin.get(n, [])):
            if r in SAVED:
                fa = fa or r
            else:
                fb = fb or r
            continue
        if r in SAVED:
            return fc or r
        gap = free_since(av[n], lo)
        if gap is not None and gap >= recent:
            return r
        if gap is not None and gap > fcgap:
            fc, fcgap = r, gap
    return fc or fb or fa


def useable(c):
    """The master registers of the region's useable set (the first chunk of the sparse bit vector
    NColDeterminePhysregUseable returns: {base, low, high, next}, bit i = register number base + i), or None."""
    u = (c.get("useable") or [None])[0]
    if not u or len(u) < 3:
        return None
    mask = (u[2] << 32) | u[1]
    return {regname(u[0] + i) for i in range(64) if mask >> i & 1} - {None}


def xbank(c):
    """The bank an xmm candidate's useable set is in (320 or 336), from its first chunk; None for others."""
    u = (c.get("useable") or [None])[0]
    if not u or len(u) < 3:
        return None
    mask = (u[2] << 32) | u[1]
    nums = [u[0] + i for i in range(64) if mask >> i & 1]
    return next((b for b in XBANKS if nums and b <= nums[0] < b + 16), None)


def klass(c):
    """'xmm' or 'int' (the traced register's family: a candidate's class does not change under an edit)."""
    g = ALIAS.get((c.get("regions") or [{}])[0].get("reg", ""), "")
    return "xmm" if g.startswith("xmm") else "int"


def walk_args(c, st, pw):
    x = klass(c) == "xmm"
    if x and pw is not None:
        pw = {(144 + n - XMASTER if XMASTER <= n < XMASTER + 16 else n): v for n, v in pw.items()}
    return dict(recent=3 if x else RECENT, prefwin=pw, own=tuple(c["prefwin"]) if c.get("prefwin") else None,
                tied=c.get("tied"), avoid=() if x else avoid(c, st), use=useable(c),
                order=XMM_ORDER if x else INT_ORDER)


class Avail(dict):
    """number -> available intervals, with the xmm family seen through one bank (`bank`): av[NUM['xmmK']] is the
    list of bank + K, and cutting it cuts every bank of xmmK (an assignment takes the whole register)."""
    bank = None

    def _k(self, n):
        return (self.bank or XBANKS[0]) + (n - 144) if 144 <= n < 160 else n

    def __getitem__(self, n):
        return dict.__getitem__(self, self._k(n))

    def __contains__(self, n):
        return dict.__contains__(self, self._k(n))

    def __setitem__(self, n, v):
        if 144 <= n < 160:
            for b in XBANKS:
                if dict.__contains__(self, b + n - 144):
                    dict.__setitem__(self, b + n - 144, cut_like(dict.__getitem__(self, b + n - 144), v))
        else:
            dict.__setitem__(self, n, v)


def cut_like(old, new):
    """Apply to another bank's list the cut that turned one list into `new` (the busy intervals of `new`)."""
    return new


def avail_of(st):
    a = Avail()
    for r, v in st["avail"].items():
        dict.__setitem__(a, int(r), [tuple(x) for x in v])
    return a


def avoid(c, st):
    """The registers the candidate's anti-preference set names (a memory base avoids r12 / rbp, r13)."""
    a = (c.get("anti_r") or [0])[0]
    an = st.get("anti") or []
    return next((ANTI[i] for i, x in enumerate(an) if a and x == a), ())


def cmd_replay(fns, show=10):
    """Each integer candidate whose traced result is one register over its whole range: is the model's first
    covering register in INT_ORDER the traced one? Availability: the traced initial lists, minus the ranges of
    every candidate processed before (as traced)."""
    n = ok = 0
    bad = []
    kinds = {"int": [0, 0], "xmm": [0, 0]}
    for f in fns:
        st = f.get("state")
        if not st or "avail" not in st:
            continue
        av = avail_of(st)
        pw = {int(r): [tuple(x) for x in v] for r, v in (st.get("prefwin") or {}).items()} if st.get("prefwin") else None
        for c in f["cands"]:
            regs = c["regions"]
            if not regs or not c.get("ranges"):
                continue
            av.bank = xbank(c)
            got = ALIAS.get(regs[0]["reg"])
            if len(regs) == 1 and got in NUM and NUM[got] in av:
                n += 1
                prefs = [regname(n) for n, _t in (c.get("prefs") or [[]])[0]]
                u = useable(c)
                model = next((r for r in prefs if r in NUM and NUM[r] in av and (u is None or r in u) and
                              covers(av[NUM[r]], c["ranges"])), None) or \
                    order_walk(av, c["ranges"], **walk_args(c, st, pw))
                kinds[klass(c)][0] += 1
                kinds[klass(c)][1] += model == got
                if model == got:
                    ok += 1
                else:
                    bad.append((f["func"], c["name"], got, model, c["ranges"][:3]))
            if WINDOW_GOES and pw is not None and c.get("prefwin"):   # its own preference window is no longer
                own = tuple(c["prefwin"])                            # anyone else's obstacle
                for r in pw:
                    if own in pw[r]:
                        pw[r] = [x for x in pw[r] if x != own]
                        break
            for r in regs:                        # what the trace did is what the next candidates see
                g = ALIAS.get(r["reg"])
                if g in NUM and NUM[g] in av:
                    rr = [(lo, hi) for lo, hi in c["ranges"] if lo <= r["last"] and r["first"] <= hi] or \
                         [(r["first"], r["last"])]
                    av[NUM[g]] = cut(av[NUM[g]], rr)
    print("replay: %d of %d single-region integer candidates got the model's register" % (
        kinds["int"][1], kinds["int"][0]))
    if kinds["xmm"][0]:
        print("replay: %d of %d single-region xmm candidates" % (kinds["xmm"][1], kinds["xmm"][0]))
    for b in bad[:show]:
        print("  %s %s: traced %s, model %s  ranges %s" % b)
    return 0


def buckets(cands):
    """The traced order cut into its buckets: maximal runs of non-increasing priority (each bucket of
    NColPrioritizeCandidates is sorted on its own, docs/c2/ncolor.md 2). -> bucket index per candidate."""
    out, b, prev = [], 0, None
    for c in cands:
        p = c["prio"] - (1 << 32) if c["prio"] & 0x80000000 else c["prio"]
        if prev is not None and p > prev:
            b += 1
        out.append(b)
        prev = p
    return out


def simulate(f, prio_of=None):
    """Allocate a function's integer single-range-set candidates with the model only: the order from the model's
    priorities within the traced buckets (ties by the traced position), each by preferences then the order walk,
    availability updated by the model's own choices. -> {candidate name: register or None}."""
    st = f["state"]
    av = avail_of(st)
    pw = {int(r): [tuple(x) for x in v] for r, v in (st.get("prefwin") or {}).items()}
    bk = buckets(f["cands"])
    pr = []
    for i, c in enumerate(f["cands"]):
        p = (prio_of or {}).get(c["name"])
        if p is None:
            p = prio(c, st)
        if p is None:
            p = c["prio"] - (1 << 32) if c["prio"] & 0x80000000 else c["prio"]
        pr.append(p)
    order = sorted(range(len(f["cands"])), key=lambda i: (bk[i], -pr[i], f["cands"][i].get("index", i)))
    got = {}
    for i in order:
        c = f["cands"][i]
        av.bank = xbank(c)
        if not c.get("ranges") or not c["regions"] or ALIAS.get(c["regions"][0]["reg"]) not in NUM or \
                NUM[ALIAS[c["regions"][0]["reg"]]] not in av:
            for r in c["regions"]:              # not modelled: what the trace did
                g = ALIAS.get(r["reg"])
                if g in NUM and NUM[g] in av:
                    av[NUM[g]] = cut(av[NUM[g]], [(r["first"], r["last"])])
            continue
        u = useable(c)
        prefs = [regname(n) for n, _t in (c.get("prefs") or [[]])[0]]
        r = next((x for x in prefs if x in NUM and NUM[x] in av and (u is None or x in u) and
                  covers(av[NUM[x]], c["ranges"])), None) or order_walk(av, c["ranges"], **walk_args(c, st, pw))
        got[c["name"]] = r
        if r is not None:
            av[NUM[r]] = cut(av[NUM[r]], c["ranges"])
        if c.get("prefwin"):
            own = tuple(c["prefwin"])
            for q in pw:
                if own in pw[q]:
                    pw[q] = [x for x in pw[q] if x != own]
                    break
    return got


def cmd_sim(fns, show=10):
    n = ok = 0
    bad = []
    for f in fns:
        if not f.get("state") or "avail" not in f["state"]:
            continue
        got = simulate(f)
        for c in f["cands"]:
            if c["name"] not in got or len(c["regions"]) != 1:
                continue
            n += 1
            t = ALIAS.get(c["regions"][0]["reg"])
            if got[c["name"]] == t:
                ok += 1
            else:
                bad.append((f["func"], c["name"], t, got[c["name"]]))
    print("sim: %d of %d single-region candidates as traced (model order and choices from the start)" % (ok, n))
    for b in bad[:show]:
        print("  %s %s: traced %s, model %s" % b)
    return 0


def edited_prio(f, edits):
    """{name: new priority} for edits +app:VAR:B[:N] / -app:VAR:B[:N] / prio:VAR=P."""
    st = f["state"]
    out = {}
    byname = {c["name"].split("$")[0]: c for c in f["cands"]}
    byname.update({c["name"]: c for c in f["cands"]})
    for e in edits:
        if e.startswith("prio:"):
            v, _, p = e[5:].partition("=")
            out[byname[v]["name"]] = int(p)
            continue
        sign, rest = e[0], e[1:]
        kind, v, b, *n = rest.split(":")
        c = dict(byname[v])
        b, n = int(b), int(n[0]) if n else 1
        tups = [t for t in range(st["ntup"]) if blockof(st, t) == b and any(lo <= t <= hi for lo, hi in c["ranges"])]
        if not tups:
            sys.exit("whatif: %s is not live in block %d" % (v, b))
        apps = list(c["apps"])
        for _ in range(n):
            if sign == "+":
                apps.append(tups[0])
            else:
                inb = [a for a in apps if blockof(st, a) == b]
                if not inb:
                    sys.exit("whatif: %s has no appearance left in block %d" % (v, b))
                apps.remove(inb[-1])
        c["apps"] = apps
        out[c["name"]] = prio(c, st)
    return out


def cmd_whatif(fns, func, edits):
    fs = [f for f in fns if func in (f["func"] or "")]
    if not fs:
        sys.exit("whatif: no function %s" % func)
    f = fs[0]
    base, new = simulate(f), simulate(f, edited_prio(f, edits))
    np = edited_prio(f, edits)
    for c in f["cands"]:
        if c["name"] not in base:
            continue
        p0 = prio(c, f["state"])
        print("  %-14s prio %-8s%s  %-5s -> %-5s%s" % (c["name"], p0, (" -> %d" % np[c["name"]]) if c["name"] in np else "",
              base[c["name"]], new[c["name"]], "   *" if base[c["name"]] != new[c["name"]] else ""))
    return 0


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    show = 10
    if "--show" in argv:
        i = argv.index("--show")
        show = int(argv[i + 1])
        del argv[i:i + 2]
    if argv[0] == "whatif":
        i = argv.index("--func")
        func = argv[i + 1]
        rest = argv[1:i] + argv[i + 2:]
        paths = [x for x in rest if x.endswith(".json")]
        return cmd_whatif(load(paths), func, [x for x in rest if not x.endswith(".json")])
    fns = load(argv[1:])
    return {"prio": cmd_prio, "replay": lambda f: cmd_replay(f, show), "sim": lambda f: cmd_sim(f, show)}[argv[0]](fns)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
