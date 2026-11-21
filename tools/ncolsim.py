#!/usr/bin/env python3
"""A model of NColor, the x64 register allocator of cl 17.00 (docs/c2/ncolor.md), checked against traces.

usage: ncolsim.py prio   EXPORT.json [EXPORT.json ..]      the priority formula against every traced priority
       ncolsim.py order  EXPORT.json [..]                  the processing order (buckets, sort) against the trace
       ncolsim.py replay EXPORT.json [..] [--show N]       the register choice: each candidate, in the traced order,
                                                           against the traced initial availability
       ncolsim.py sim    EXPORT.json [..] [--show N]       the whole run from the traced start: the model's own order
                                                           (within the traced buckets) and choices, against the trace
       ncolsim.py ilin   EXPORT.json [..]                  (`c2ncol.py --export --il`) NColor's inputs computed from
                                                           the IL (tuple numbers, blocks, appearances, ranges,
                                                           live counts, zone depth) against the traced ones
       ncolsim.py ilprio | ilprio+live | ilsim EXPORT.json [..]  priorities / replay + sim on the IL's inputs
                                                           (ilprio+live: the live counts too; docs/c2/ncolor.md 11)
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


# ------------------------------------------------------------------ source levers on x64 (realize / rwsweep /
# helpersweep under a build table with "arch": "x64"; docs/c2/ncolor.md 10)
#
# The x86 source-lever tools predict with Color's model (rasim, c2regs' Spillcosts priorities). On cl 17.00 x64 the
# allocator is NColor and the inputs a source edit moves are the ones prio() and order_walk() read: appearances per
# block (`+app` / `-app`), the liveness counts, the lifetime index (ties), preferences from copies. The helpers below
# trace a compile into the export format (the same hooks as `c2ncol.py --export`, plus the IL at NColor's entry for
# the block -> source line map), diff two such runs as whatif edits, and search the whatif edits that give a wanted
# register.


def is_x64():
    """True under a build table with arch x64 and the NColor hooks (the gate of the three source-lever tools)."""
    import c2build
    return c2build.get("arch") == "x64" and "ncol" in (c2build.get("hooks") or {})


def short(name):
    """A candidate's name without the scope suffix (`p$1` -> `p`): the name the source spells."""
    return (name or "").split("$")[0]


def trace_hooks():
    """c2ncol --export's hook list, with the IL dump (il_spec's second spec) on the function-entry event."""
    import c2build, c2ncol
    hk = list(c2ncol.hooks())
    hk[c2ncol.CAND] += "," + ",".join(c2ncol.why_specs())
    st = c2build.hooks("ncol_state", None) % dict(nblk=c2ncol.NBLK, ntup=c2ncol.NTUP, ninfo=13 * c2ncol.NBLK)
    st += "," + ",".join("tf65c>0>%d*%d" % (4 * r, c2ncol.NAVAIL) for r in c2ncol.AVAIL_REGS)
    st += "," + ",".join("tf6a0>0>%d*%d" % (4 * r, c2ncol.NAVAIL) for r in c2ncol.PREFWIN_REGS)
    st += ",t119c8>0,t119cc>0,t119d0>0"
    hk.append(st)
    il = c2build.hooks("il_spec", None)
    hk.append(c2build.hooks("ncol_fn", None) + ("," + il.split(",", 1)[1] if il and "," in il else ""))
    hk.append(c2build.hooks("ncol_pref", None))
    return hk


def il_blocks(text, fn_kind=6):
    """{function name: {NColor block: [line words]}} from the IL dumped at NColor's entry (T lines after the FN event):
    the `B` lines count the blocks as NColBlockInfo numbers them (block 0 the entry), `I` lines carry the line word
    (offset from the function's first line, plus 1)."""
    out, cur, b = {}, None, -1
    for ln in text.splitlines():
        if ln.startswith("E "):
            head, _, rest = ln.partition("|")
            if int(head.split()[3]) == fn_kind:
                nm = rest.split('"')[1] if '"' in rest else "?"
                cur, b = out.setdefault(nm, {}), -1
            else:
                cur = None                  # the dump is the T lines right after the FN event
            continue
        if cur is None or not ln.startswith("T "):
            continue
        f = ln[2:].split()
        if f[0] == "B":
            b += 1
            if f[1] != "0":
                cur.setdefault(b, [])
        elif f[0] == "I" and len(f) > 3 and b >= 0:
            try:
                cur.setdefault(b, []).append(int(f[3]))
            except ValueError:
                pass
    return out


class NRun:
    """One traced 17.00 compile of `src`, function `func`: the export record (cands, state), the listing's
    instructions (tsweep.listing_instrs) and the block -> source line map. Raises RuntimeError when the function
    was not allocated (compile error)."""

    def __init__(self, src, func, cl, keep):
        import os, re, c2trace, c2ncol, tsweep
        _, _, rc, out, text = c2trace.run_trace(src, trace_hooks(), cl or ["/O2"], keep=keep, quiet=True, func=func)
        fns = [f for f in c2ncol.parse_all(text) if f["cands"] and c2trace.func_match(func, f["func"] or "")]
        if not fns:
            err = " ".join(re.findall(r"error C\d+:[^\n]*", out))[:200]
            raise RuntimeError(("compile error: " + err) if err else "function %s not allocated by NColor" % func)
        self.f = fns[0]
        self.func = self.f["func"]
        asm = os.path.join(keep, os.path.splitext(os.path.basename(src))[0] + ".asm")
        self.asm = asm
        try:
            self.instrs = tsweep.listing_instrs(asm, func)
        except Exception:
            self.instrs = []
        words = il_blocks(text).get(self.func) or next(iter(il_blocks(text).values()), {})
        line0 = None
        try:
            lt = open(asm, errors="replace").read()
            i, _j = c2trace.listing_proc(lt, func)
            m = re.search(r"^; (\d+)\s+:", lt[i:], re.M)
            line0 = int(m.group(1)) if m else None
        except Exception:
            pass
        # line word w is source line line0 + w - 1 (w = 1: the function's head, where the parameters are homed)
        self.lines = {b: sorted({line0 + w - 1 for w in ws if w > 0}) if line0 else [] for b, ws in words.items()}
        self.nblocks = len(words)

    def regs(self):
        return regs_of(self.f)

    def apps(self):
        return apps_by_block(self.f)

    def prio(self, name):
        return traced_prio(self.f, name)


def _cand(f, name):
    return next((c for c in f["cands"] if c["name"] == name), None) or \
        next((c for c in f["cands"] if short(c["name"]) == name), None)


def traced_prio(f, name):
    c = _cand(f, name)
    if c is None or c["prio"] is None:
        return None
    return c["prio"] - (1 << 32) if c["prio"] & 0x80000000 else c["prio"]


def regs_of(f):
    """{short name: master register of its region(s), joined by / when split; '-' without one}. Temporaries keep
    their tN names (they renumber between compiles: compare named candidates)."""
    out = {}
    for c in f["cands"]:
        rs = [ALIAS.get(r["reg"], r["reg"]) for r in c["regions"]] or ["-"]
        out.setdefault(short(c["name"]), "/".join(dict.fromkeys(rs)))
    return out


def named(n):
    import re
    return not re.match(r"[ts]\d+$", n) and n not in ("?", "-")


def apps_by_block(f):
    """{short name: {block: appearances}} (the count prio() multiplies)."""
    st = f.get("state") or {}
    out = {}
    for c in f["cands"]:
        d = out.setdefault(short(c["name"]), {})
        for a in c.get("apps") or []:
            b = blockof(st, a)
            if b is not None:
                d[b] = d.get(b, 0) + 1
    return out


def app_delta(fa, fb):
    """The whatif edits that turn run A's appearance counts into run B's: ['+app:V:B:N', '-app:V:B:N', ..] for named
    candidates present in both, or None when the block structure differs (other block count: blocks cannot be
    identified). Candidates new / gone in B are listed as `new:V` / `gone:V` (not whatif edits)."""
    sa, sb = fa.get("state") or {}, fb.get("state") or {}
    na = max(sa.get("blk", [0])[:sa.get("ntup", 0)] or [0])
    nb = max(sb.get("blk", [0])[:sb.get("ntup", 0)] or [0])
    if na != nb:
        return None
    A, B = apps_by_block(fa), apps_by_block(fb)
    out = []
    for v in sorted(set(A) | set(B)):
        if not named(v):
            continue
        if v not in A:
            out.append("new:" + v)
            continue
        if v not in B:
            out.append("gone:" + v)
            continue
        for b in sorted(set(A[v]) | set(B[v])):
            d = B[v].get(b, 0) - A[v].get(b, 0)
            if d:
                out.append("%sapp:%s:%d:%d" % ("+" if d > 0 else "-", v, b, abs(d)))
    return out


def run_delta(fa, fb):
    """What changed in NColor's inputs between two runs of one function: dict(edits = the whatif edits (+app / -app
    per block, index:V=K when the lifetime-index order of named candidates changed), blocks = the block count
    changed (no edit list then), ranges = named candidates whose range list moved by more than the uniform shift
    of the tuple numbering, live = the per-block live counts changed, new / gone = named candidates)."""
    out = dict(edits=[], blocks=False, ranges=[], live=False, new=[], gone=[])
    na = max(fa["state"].get("blk", [0])[:fa["state"].get("ntup", 0)] or [0])
    d = app_delta(fa, fb)
    if d is None:
        out["blocks"] = True
        return out
    out["new"] = [e[4:] for e in d if e.startswith("new:")]
    out["gone"] = [e[5:] for e in d if e.startswith("gone:")]
    out["edits"] = [e for e in d if e[:5] in ("+app:", "-app:")]
    sa, sb = fa["state"], fb["state"]
    nblk = na + 1
    out["live"] = sa["live"][:nblk] != sb["live"][:nblk] or sa["maxlive"] != sb["maxlive"]
    out["edits"] += ["live:%d:%+d" % (b, sb["live"][b] - sa["live"][b]) for b in range(min(nblk, len(sa["live"]),
                     len(sb["live"]))) if sb["live"][b] != sa["live"][b]]
    ca = {short(c["name"]): c for c in fa["cands"] if named(short(c["name"]))}
    cb = {short(c["name"]): c for c in fb["cands"] if named(short(c["name"]))}
    both = [v for v in ca if v in cb]
    for v in both:
        a, b = ca[v], cb[v]
        if [blockof(sa, t) for r in a.get("ranges") or [] for t in r] != \
                [blockof(sb, t) for r in b.get("ranges") or [] for t in r] or \
                (blockof(sa, a["first"]) == blockof(sa, a["last"])) and (a["last"] - a["first"]) != (b["last"] - b["first"]):
            out["ranges"].append(v)
    ia = sorted(both, key=lambda v: ca[v].get("index", 0))
    ib = sorted(both, key=lambda v: cb[v].get("index", 0))
    if ia != ib:
        # the base's index values, permuted into the variant's order
        vals = sorted(ca[v].get("index", 0) for v in both)
        out["edits"] += ["index:%s=%d" % (v, k) for v, k in zip(ib, vals) if ca[v].get("index", 0) != k]
    return out


def whatif_edits(edits):
    """The subset of edits ncolsim's whatif takes."""
    return [e for e in edits if e[:5] in ("+app:", "-app:") or e.startswith("prio:")]


def with_live(f, edits):
    """f with the per-block live counts of `live:B:+K` edits (a candidate more or fewer live in block B: every
    global's priority moves, through live[b] and the maximum); f itself when there is none."""
    lv = [e for e in edits if e.startswith("live:")]
    if not lv:
        return f
    st = dict(f["state"])
    st["live"] = list(st["live"])
    for e in lv:
        _k, b, k = e.split(":")
        b = int(b)
        if b < len(st["live"]):
            st["live"][b] += int(k)
    nblk = max(st["blk"][:st["ntup"]] or [0]) + 1
    st["maxlive"] = max(st["live"][:nblk] or [0])
    g = dict(f)
    g["state"] = st
    return g


def with_index(f, edits):
    """f with the lifetime indices of `index:V=K` edits (ties of NColColPriorityCompare go to the lower index) and of
    `before:A:B` (A gets B's index and B A's, when A is after B); f itself when there is none."""
    ix = [e for e in edits if e.startswith(("index:", "before:"))]
    if not ix:
        return f
    g = dict(f)
    g["cands"] = [dict(c) for c in f["cands"]]
    for e in ix:
        if e.startswith("index:"):
            v, _, k = e[6:].partition("=")
            c = _cand(g, v)
            if c is not None:
                c["index"] = int(k)
        else:
            _k, x, y = e.split(":")[:3]
            cx, cy = _cand(g, x), _cand(g, y)
            if cx is not None and cy is not None and cx.get("index", 0) > cy.get("index", 0):
                cx["index"], cy["index"] = cy["index"], cx["index"]
    return g


def predict(f, edits):
    """{short name: register} the model gives after the edits: +app / -app / prio (whatif's priorities) and index /
    before (lifetime-index order, the tie-break), from the traced start of `f` (simulate)."""
    w = whatif_edits(edits)
    g = with_live(with_index(f, edits), edits)
    got = simulate(g, edited_prio(g, w) if w else None)
    return {short(k): v for k, v in got.items()}


def gap(f, a, b):
    """prio(b) - prio(a) as traced, NColColPriorityCompare's tie by lifetime index folded in: <= 0 when a is processed
    before b (both in one bucket). None when either is missing."""
    ca, cb = _cand(f, a), _cand(f, b)
    pa, pb = traced_prio(f, a), traced_prio(f, b)
    if ca is None or cb is None or pa is None or pb is None:
        return None
    g = pb - pa
    if g == 0 and ca.get("index", 0) > cb.get("index", 0):
        g = 1
    return g


def edit_ok(f, e):
    """A whatif edit is applicable (the candidate is live in the block; -app: it appears there)."""
    try:
        edited_prio(f, [e])
        return True
    except SystemExit:
        return False
    except (KeyError, ValueError, IndexError):
        return False


def need(f, want, maxn=4, pairs=True, limit=12):
    """Whatif edits that give the wanted registers: want = {short name: register}. Singles +app / -app of every
    named global candidate in every block it is live in (N = 1..maxn), then pairs of the best singles. ->
    [(edits, predicted {name: reg}, side effects [(name, before, after)])], fewest appearances moved first, then
    fewest side effects."""
    import itertools
    st = f["state"]
    base = predict(f, [])
    singles = []
    for c in f["cands"]:
        v = short(c["name"])
        if not named(v) or not c.get("ranges") or prio(c, st) is None:
            continue
        blocks = sorted({blockof(st, t) for lo, hi in c["ranges"] for t in range(lo, hi + 1)} - {None})
        cnt = apps_by_block(dict(cands=[c], state=st)).get(v, {})
        for b in blocks:
            for n in range(1, maxn + 1):
                singles.append(["+app:%s:%d:%d" % (v, b, n)])
                if cnt.get(b, 0) >= n:
                    singles.append(["-app:%s:%d:%d" % (v, b, n)])
    # equal priorities: the lifetime index decides (first reference earlier: a statement or declaration moved up)
    for x in f["cands"]:
        for y in f["cands"]:
            vx, vy = short(x["name"]), short(y["name"])
            if named(vx) and named(vy) and vx != vy and traced_prio(f, vx) == traced_prio(f, vy) and \
                    x.get("index", 0) > y.get("index", 0):
                singles.append(["before:%s:%s" % (vx, vy)])
    out, near = [], []

    def score(ed):
        try:
            got = predict(f, ed)
        except SystemExit:
            return None
        side = [(k, base.get(k), got.get(k)) for k in got if named(k) and got.get(k) != base.get(k) and k not in want]
        return got, side

    for ed in singles:
        r = score(ed)
        if r is None:
            continue
        got, side = r
        ok = all(got.get(k) == w for k, w in want.items())
        cost = sum(int(e.rsplit(":", 1)[1]) if e[:5] in ("+app:", "-app:") else 1 for e in ed)
        (out if ok else near).append((cost, len(side), ed, got, side))
    if pairs and not out:
        pool = sorted(near, key=lambda x: (-sum(x[3].get(k) == w for k, w in want.items()), x[0]))[:40]
        for x, y in itertools.combinations(pool, 2):
            if x[2][0].split(":")[1:3] == y[2][0].split(":")[1:3]:
                continue
            ed = x[2] + y[2]
            r = score(ed)
            if r and all(r[0].get(k) == w for k, w in want.items()):
                out.append((x[0] + y[0], len(r[1]), ed, r[0], r[1]))
    out.sort(key=lambda x: (x[0], x[1], x[2]))
    return [(ed, got, side) for _c, _n, ed, got, side in out[:limit]]


def check_prediction(base_f, edits, run_f, names=None):
    """(wanted-names hit, all-named hit, {name: (predicted, traced)} of the mismatches): the model's registers after
    `edits` on the base run against a traced run of the edited source."""
    pred = predict(base_f, edits)
    got = regs_of(run_f)
    keys = [k for k in pred if named(k) and k in got]
    bad = {k: (pred[k], got[k]) for k in keys if pred[k] != got[k]}
    want_hit = all(pred.get(k) == got.get(k) for k in (names or []))
    return want_hit, not bad, bad


# ------------------------------------------------------------------ trace-free inputs (docs/c2/ncolor.md 11)
#
# `c2ncol.py --export --il` adds each function's IL as NColor numbered it (dumped after NColPrioritizeCandidates;
# T R raw words give the branch targets). From that IL alone: the tuple numbering and blocks (blk), every candidate's
# appearances, ranges, first / last, the live count per block (live, maxLive) and the loop depth (from the flow
# graph), and the initial availability of the integer registers. il_state(f) builds a `state` + candidate list in
# the export's shape, so prio(), replay and simulate() run on it unchanged.

REGNAMES = None


def _regnames():
    """Register names -> number (_RegName 0x10001ff0 of c2 17.00), for telling a physical register from a candidate."""
    global REGNAMES
    if REGNAMES is None:
        import os, struct, json
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tab = json.load(open(os.path.join(here, "tools", "c2builds", "17.00.61030-x64.json"))).get("regnames")
        if tab:
            REGNAMES = {n: int(r) for r, n in tab.items()}
            return REGNAMES
        import pefile
        path = next(p for p in (os.path.join(here, "work", "vs2012", "bin", "c2_x64_1700.dll"),
                                os.path.join(here, "work", "toolchain", "c2-17.00.61030-x64", "VC", "Bin", "c2.dll"))
                    if os.path.exists(p))
        pe = pefile.PE(path, fast_load=True)
        img, base = pe.get_memory_mapped_image(), pe.OPTIONAL_HEADER.ImageBase
        d = lambda va: struct.unpack_from("<I", img, va - base)[0]
        REGNAMES = {}
        for r in range(1, 364):
            o = d(0x10001ff0 + 4 * r) - base
            REGNAMES[img[o:img.index(b"\0", o)].decode("latin1")] = r
    return REGNAMES


def _ops(t):
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


def il_parse(lines):
    """The --il lines -> tuples in list order: dict(n = tuple number (1-based, every tuple of the stream counts),
    kind 'B' / 'L' / 'K' / 'I', op, dst / src = [(leaf kind, text)], words = the tuple's raw words, leafwords)."""
    tups = []
    cur = None
    for ln in lines:
        k = ln[:1]
        if k in "BLKI":
            cur = dict(n=len(tups) + 1, kind=k, op="", dst=[], src=[], words=None, leafwords=[], text=ln)
            if k == "I":
                head, _, ops = ln.partition(" | ")
                cur["op"] = head.split()[2]
                d, _, sr = ops.partition("<-")
                sr = sr[:-3] if sr.endswith(" ->") else sr
                cur["dst"], cur["src"] = _ops(d), _ops(sr)
            elif k == "K":
                cur["op"] = ln.split()[-1]
            tups.append(cur)
        elif k == "R" and cur is not None:
            w = [int(x, 16) for x in ln.split()[2:]]
            if cur["words"] is None:
                cur["words"] = w
            else:
                cur["leafwords"].append(w)
                cur.setdefault("leafsym", []).append(None)
        elif k == "S" and cur is not None and cur.get("leafsym"):
            x = [int(y, 16) for y in ln.split()[1:]]
            cur["leafsym"][-1] = x            # [sym, its words +0 ..]: +0xc root = x[4], +0x24 size, +0x28 offset
    for t in tups:      # leaf kinds (word 3, low byte), in dump order: destinations, then sources
        lw = t["leafwords"]
        nd = len(t["dst"])
        t["dstk"] = [w[3] & 0xff if len(w) > 3 else None for w in lw[:nd]]
        t["srck"] = [w[3] & 0xff if len(w) > 3 else None for w in lw[nd:]]
        t["srcw"] = lw[nd:]
        ls = t.get("leafsym") or [None] * len(lw)
        root = lambda x: (x[4] or x[0]) if x and len(x) > 4 else (x[0] if x else None)
        t["dstr"] = [root(x) for x in ls[:nd]]
        t["srcr"] = [root(x) for x in ls[nd:]]
        # (is the symbol its own root, its size): a narrower view of a lifetime written is a partial definition
        view = lambda x: (x[0] == root(x), x[10]) if x and len(x) > 10 else (True, None)
        t["dstv"] = [view(x) for x in ls[:nd]]
    return tups


def _regleaf(text):
    """A REG leaf's (candidate symbol or None, preassigned physreg name or None)."""
    body = text.rsplit(":", 1)[0]
    a, eq, b = body.partition("=")
    regs = _regnames()
    if eq and a in regs:
        return None, a                     # already a physical register (NColInitializeSyms' separate numbering)
    if body in regs:
        return None, body
    return body, None


def il_blocks(tups):
    """Blocks in list order (each starts at a B tuple): [dict(first, last, tups, succ)]. Successors: a branch's
    target (its code-address leaf +0x24 = the LABEL tuple's +0x2c), the next block unless the block ends in an
    unconditional jump / return / switch (a switch: the labelled blocks that no branch names, as no case list is
    dumped)."""
    blocks = []
    for t in tups:
        if t["kind"] == "B":
            blocks.append(dict(first=t["n"], last=t["n"], tups=[t]))
        elif blocks:
            blocks[-1]["tups"].append(t)
            blocks[-1]["last"] = t["n"]
    lab = {}
    for i, b in enumerate(blocks):
        for t in b["tups"]:
            if t["kind"] == "L" and t["words"] and len(t["words"]) > 11:
                lab[t["words"][11]] = i
    refd = set()                  # labels a branch names
    for b in blocks:
        for t in b["tups"]:
            if t["kind"] == "I" and t["srck"][:1] == [4] and t["op"] != "call" and len(t["srcw"][0]) > 9:
                refd.add(t["srcw"][0][9])
    cases = sorted({i for w, i in lab.items() if w not in refd})
    for i, b in enumerate(blocks):
        ins = [t for t in b["tups"] if t["kind"] == "I"]
        last = ins[-1] if ins else None
        succ = []
        op = last["op"] if last else ""
        if last is not None and last["srck"][:1] == [4] and op not in ("call",):
            w = last["srcw"][0]
            tgt = lab.get(w[9]) if len(w) > 9 else None
            if tgt is not None:
                succ.append(tgt)
        if op in ("OPSWITCH",):
            succ = cases             # no case list in the dump: the labels no branch names
        elif op not in ("jmp", "_eret", "ret") and i + 1 < len(blocks):
            succ.append(i + 1)
        b["succ"] = list(dict.fromkeys(succ))
    return blocks


def _loop_depth(blocks):
    """Loop nesting depth per block from the flow graph (natural loops of the back edges to a dominator), capped
    at 5 like NColDoLiveness (blockinfo +0x28), and the zone depth the priorities use (+0x2c): the same, but 0 for
    a block whose innermost loop is too large to be a hot zone (zlib: only inflate's and inflateBack's state-machine
    loops, 279 and 200 blocks, are not zones; the next largest loop has 35; their inner loops keep their full
    depth). -> (depth, zone)"""
    n = len(blocks)
    preds = [[] for _ in range(n)]
    for i, b in enumerate(blocks):
        for s in b["succ"]:
            preds[s].append(i)
    dom = [set(range(n)) for _ in range(n)]
    dom[0] = {0}
    changed = True
    while changed:
        changed = False
        for i in range(1, n):
            ps = [dom[p] for p in preds[i]]
            nd = ({i} | set.intersection(*ps)) if ps else {i}
            if nd != dom[i]:
                dom[i], changed = nd, True
    depth = [0] * n
    zone = [0] * n
    loops = {}
    for i in range(n):
        for s in blocks[i]["succ"]:
            if s in dom[i]:            # back edge i -> s
                body = loops.setdefault(s, {s})
                stack = [i]
                while stack:
                    x = stack.pop()
                    if x not in body:
                        body.add(x)
                        stack.extend(preds[x])
    inner = {}
    for hdr, body in sorted(loops.items(), key=lambda hb: len(hb[1]), reverse=True):
        big = len(body) > ZONE_MAX_BLOCKS
        for x in body:
            depth[x] += 1
            inner[x] = big and x != hdr   # the innermost loop is the smallest body: processed last; a header keeps it
    zone = [0 if inner.get(x) else depth[x] for x in range(n)]
    return [min(d, 5) for d in depth], [min(d, 5) for d in zone]


def il_inputs(f):
    """NColor's inputs computed from the function's IL (f['il']): dict(blk, ntup, blocks, depth, cands = {name:
    dict(apps, defs, uses, ranges, first, last)}, live, maxlive)."""
    tups = il_parse(f["il"])
    blocks = il_blocks(tups)
    ntup = tups[-1]["n"] if tups else 0
    blk = [0] * (ntup + 2)
    for i, b in enumerate(blocks):
        for t in b["tups"]:
            blk[t["n"]] = i
    occ = {}                      # candidate (its root SYM, else its name) -> [(tup#, is_def)]
    names = {}
    tied = {}                     # lifetimes already in a physical register (counted in the live numbers)
    partial = set()               # (candidate, tup#): a byte / word write of a wider lifetime (keeps the rest: no hole)
    rsize = {}
    for t in tups:
        for r, (own, sz) in zip(t.get("dstr") or [], t.get("dstv") or []):
            if r and own and sz:
                rsize[r] = max(rsize.get(r, 0), sz)
    for t in tups:
        if t["kind"] != "I":
            continue
        for side, ops, ks, rs in (("d", t["dst"], t["dstk"], t.get("dstr") or []),
                                  ("u", t["src"], t["srck"], t.get("srcr") or [])):
            for i, (k, x) in enumerate(zip(ks, ops)):
                if k != 1:
                    continue
                c, phys = _regleaf(x)
                key = rs[i] if i < len(rs) and rs[i] else c
                if c is not None:
                    occ.setdefault(key, []).append((t["n"], side == "d"))
                    names.setdefault(key, c)
                    if side == "d":
                        own, sz = (t.get("dstv") or [])[i] if i < len(t.get("dstv") or []) else (True, None)
                        if not own and sz and sz < rsize.get(key, 8) and sz < 4:   # a dword write zero-extends
                            partial.add((key, t["n"]))
                elif phys is not None and "=" in x and not phys.startswith("cc"):
                    tied.setdefault(rs[i] if i < len(rs) and rs[i] else x.split("=")[1].rsplit(":", 1)[0],
                                    []).append((t["n"], side == "d"))
    cands, live_in, live_out = _lifetimes(occ, blocks, blk, partial)
    tl, _, _ = _lifetimes(tied, blocks, blk)
    nb = len(blocks)
    live = [0] * (nb + 1)
    for x in list(cands.values()) + (list(tl.values()) if LIVE_TIED else []):
        for b in range(nb):
            if any(lo <= blocks[b]["last"] and blocks[b]["first"] <= hi for lo, hi in x["ranges"]):
                live[b] += 1
    for k, v in cands.items():
        v["name"] = names.get(k)
    ldepth, zone = _loop_depth(blocks)
    return dict(blk=blk, ntup=ntup, blocks=blocks, depth=zone, loopdepth=ldepth, cands=cands, live=live,
                maxlive=max(live) if live else 0, live_in=live_in, live_out=live_out, tied=tl)


ZONE_MAX_BLOCKS = 100   # a loop of more blocks is not a hot zone (NColBuildHotZones; measured bound: 35 < x < 200)
LIVE_TIED = True     # the per-block live count includes the lifetimes already in a physical register
REACHING = True      # liveness pruned to where a definition reaches


def _lifetimes(occ, blocks, blk, partial=frozenset()):
    """Liveness (backward, per block: a use before a def is live in) and NColBuildInfo's range lists: within a
    block from the block's first tuple (live in) or the first appearance, to the block's last tuple (live out) or
    the last appearance; a block's range continues the previous block's when that one falls through into it and
    the candidate is live across. -> ({key: dict(apps, ranges, first, last)}, live_in, live_out)"""
    nb = len(blocks)
    gen = [set() for _ in range(nb)]
    kill = [set() for _ in range(nb)]
    byblk = {}
    for c, lst in occ.items():
        for n_, d in lst:
            byblk.setdefault((blk[n_], c), []).append((n_, d))
    for (b, c), lst in byblk.items():
        lst.sort(key=lambda x: (x[0], x[1]))   # a use and a def in one tuple: the use first
        if not lst[0][1]:
            gen[b].add(c)
        if any(d for _n, d in lst):
            kill[b].add(c)
    live_in = [set() for _ in range(nb)]
    live_out = [set() for _ in range(nb)]
    changed = True
    while changed:
        changed = False
        for b in range(nb - 1, -1, -1):
            lo = set().union(*[live_in[s] for s in blocks[b]["succ"]]) if blocks[b]["succ"] else set()
            li = gen[b] | (lo - kill[b])
            if lo != live_out[b] or li != live_in[b]:
                live_out[b], live_in[b], changed = lo, li, True
    # live only where a definition reaches (forward): a use with no definition on some path does not stretch
    # the lifetime back to the entry
    preds = [[] for _ in range(nb)]
    for b in range(nb):
        for x in blocks[b]["succ"]:
            preds[x].append(b)
    rin = [set() for _ in range(nb)]
    rout = [set() for _ in range(nb)]
    changed = True
    while changed:
        changed = False
        for b in range(nb):
            ri = set().union(*[rout[p] for p in preds[b]]) if preds[b] else set()
            ro = ri | kill[b]
            if ri != rin[b] or ro != rout[b]:
                rin[b], rout[b], changed = ri, ro, True
    if REACHING:
        live_in = [live_in[b] & rin[b] for b in range(nb)]
        live_out = [live_out[b] & rout[b] for b in range(nb)]
    out = {}
    for c, lst in occ.items():
        segs = []
        for b in range(nb):
            ev = byblk.get((b, c), [])
            if c not in live_in[b] and c not in live_out[b] and not ev:
                continue
            # per tuple: does it read the candidate (then it is live just before), does it write it
            pts = {}
            for n_, d in ev:
                u, w = pts.get(n_, (False, False))
                pts[n_] = (u or not d, w or d)
            ts = sorted(pts)
            start = blocks[b]["first"] if c in live_in[b] else None
            prev = None
            for i, n_ in enumerate(ts):
                u, w = pts[n_]
                if start is None:
                    start = n_
                elif not u and (c, n_) not in partial and prev is not None and n_ > prev + 1:
                    # a pure definition: the old value died at the previous appearance (a hole of >= 1 tuple)
                    segs.append([start, prev, b])
                    start = n_
                prev = n_
            if c in live_out[b]:
                segs.append([start if start is not None else blocks[b]["first"], blocks[b]["last"], b])
            elif ts:
                segs.append([start, ts[-1], b])
        ranges = []
        for lo, hi, b in segs:
            if ranges and ranges[-1][2] == b - 1 and c in live_out[b - 1] and c in live_in[b] and \
                    b in blocks[b - 1]["succ"] and ranges[-1][1] == blocks[b - 1]["last"]:
                ranges[-1][1], ranges[-1][2] = hi, b
            else:
                ranges.append([lo, hi, b])
        out[c] = dict(apps=sorted(n_ for n_, _d in lst), ranges=[[lo, hi] for lo, hi, _b in ranges],
                      first=ranges[0][0] if ranges else None, last=ranges[-1][1] if ranges else None)
    return out, live_in, live_out


def _ilcand(x, c):
    """A traced candidate's IL lifetime: by its SYM (the root of its leaves' symbols), else by name."""
    if c.get("sym") is not None and c["sym"] in x["cands"]:
        return x["cands"][c["sym"]]
    return next((y for y in x["cands"].values() if y.get("name") == c["name"]), None)


def il_state(f, use=("apps", "ranges", "live", "depth", "blk")):
    """The function record with the inputs named in `use` replaced by the IL's (il_inputs): a copy of f whose
    `state` and candidates prio(), cmd_replay() and simulate() read."""
    import copy
    x = il_inputs(f)
    g = copy.deepcopy(f)
    st = g["state"]
    if "blk" in use:
        st["blk"] = x["blk"]
        st["ntup"] = x["ntup"]
    if "live" in use:
        st["live"] = x["live"]
        st["maxlive"] = x["maxlive"]
    if "depth" in use:
        info = list(st["info"])
        for b, d in enumerate(x["depth"]):
            if b * 13 + 11 < len(info):
                info[b * 13 + 11] = d
        st["info"] = info
    for c in g["cands"]:
        y = _ilcand(x, c)
        if y is None or not y["ranges"]:
            continue
        if "apps" in use:
            c["apps"] = y["apps"]
        if "ranges" in use:
            c["ranges"] = [tuple(r) for r in y["ranges"]]
            c["first"], c["last"] = y["first"], y["last"]
    return g


def cmd_ilin(fns, show=10):
    """The IL-computed inputs against the traced ones, and the priorities / choices from them."""
    import collections
    c = collections.Counter()
    bad = collections.defaultdict(list)
    for f in fns:
        if not f.get("il") or not f.get("state"):
            continue
        x = il_inputs(f)
        st = f["state"]
        c["functions"] += 1
        nt = st["ntup"]
        c["tuples"] += 1
        c["ntup same"] += x["ntup"] == nt
        c["blk same"] += x["blk"][:nt] == st["blk"][:nt]
        nb = len(x["blocks"])
        c["blocks"] += nb
        c["live same"] += sum(x["live"][b] == st["live"][b] for b in range(min(nb, len(st["live"]))))
        c["depth same"] += sum(x["depth"][b] == depth(st, b) for b in range(nb))
        c["maxlive same"] += x["maxlive"] == st["maxlive"]
        for b in range(nb):
            if b < len(st["live"]) and x["live"][b] != st["live"][b]:
                bad["live"].append((f["func"], b, st["live"][b], x["live"][b]))
            if x["depth"][b] != depth(st, b):
                bad["depth"].append((f["func"], b, depth(st, b), x["depth"][b]))
        for cd in f["cands"]:
            if not cd.get("ranges"):
                continue
            c["cands"] += 1
            y = _ilcand(x, cd)
            if y is None:
                bad["missing"].append((f["func"], cd["name"]))
                continue
            c["apps same"] += sorted(cd["apps"]) == y["apps"]
            rs = [list(r) for r in cd["ranges"]]
            c["ranges same"] += rs == y["ranges"]
            if sorted(cd["apps"]) != y["apps"]:
                bad["apps"].append((f["func"], cd["name"], cd["apps"][:8], y["apps"][:8]))
            elif rs != y["ranges"]:
                bad["ranges"].append((f["func"], cd["name"], rs[:5], y["ranges"][:5]))
    for k in ("functions", "ntup same", "blk same", "blocks", "live same", "depth same", "maxlive same", "cands",
              "apps same", "ranges same"):
        print("ilin: %-13s %d" % (k, c[k]))
    for k, v in bad.items():
        print("  %s: %d" % (k, len(v)))
        for b in v[:show]:
            print("    ", b)
    return c


IL_USE = ("apps", "ranges", "depth", "blk")      # what the trace-free commands take from the IL (live: traced)


def cmd_ilsim(fns, show=10, use=IL_USE):
    """replay and sim (the register choice, availability as traced) on the IL's inputs."""
    gs = [il_state(f, use) if f.get("il") and f.get("state") else f for f in fns]
    print("on the IL's inputs (%s):" % "+".join(use))
    cmd_replay(gs, show)
    cmd_sim(gs, show)
    return 0


def cmd_ilprio(fns, use=IL_USE):
    """Priorities from IL inputs against the traced priorities (and the traced-input model's count)."""
    n = ok = ok0 = 0
    for f in fns:
        if not f.get("il") or not f.get("state"):
            continue
        g = il_state(f, use)
        for c0, c in zip(f["cands"], g["cands"]):
            p0, p = prio(c0, f["state"]), prio(c, g["state"])
            if p0 is None or c0["prio"] is None:
                continue
            tp = c0["prio"] - (1 << 32) if c0["prio"] & 0x80000000 else c0["prio"]
            n += 1
            ok0 += p0 == tp
            ok += p == tp
    print("ilprio: %d of %d priorities as traced from the IL's inputs (%s); %d from the traced inputs" % (
        ok, n, "+".join(use), ok0))
    return ok, ok0, n


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
    return {"prio": cmd_prio, "replay": lambda f: cmd_replay(f, show), "sim": lambda f: cmd_sim(f, show),
            "ilin": lambda f: cmd_ilin(f, show) and 0, "ilprio": lambda f: cmd_ilprio(f) and 0,
            "ilprio+live": lambda f: cmd_ilprio(f, IL_USE + ("live",)) and 0,
            "ilsim": lambda f: cmd_ilsim(f, show)}[argv[0]](fns)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
