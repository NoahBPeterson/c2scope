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
       ncolsim.py ilmodel | ilfull EXPORT.json [..]        the other inputs from the IL (tied register and window,
                                                           preferences, useable / anti-preference sets, lists, and
                                                           from the IL before NColSubsumption live counts,
                                                           availability, lifetime index) against the trace; replay +
                                                           sim on all of them (docs/c2/ncolor.md 12; with IL_READ:
                                                           the xmm banks, the calls' REGSET probe, the preference
                                                           lists as read, the float flag; and IL_FOLD, 12.5: the
                                                           folds' cost test in processing order, the ties, window
                                                           lists, float flag and frame score as read;
                                                           ilmodel-read / ilfull-read: IL_READ only; ilfull-sub: the
                                                           lifetimes from the IL before NColSubsumption (IL_SUB);
                                                           ilmodel-fit / ilfull-fit: neither, as the functions'
                                                           defaults)
       ncolsim.py whatif EXPORT.json --func F EDIT [EDIT..] the registers after an edit, against the unedited run:
                                                           +app:VAR:B[:N] / -app:VAR:B[:N] (N more / fewer
                                                           appearances of VAR in block B), prio:VAR=P,
                                                           bucket:VAR=local|global|aggr|const|N (another list)

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
        hidden, ghost = _pw_hidden(st), set()
        pof = il_prefs_of(f, f["prefs_src"]) if f.get("prefs_src") is not None else None
        given = {}                            # name -> the traced register (the partners of the IL's preferences)
        for c in f["cands"]:
            regs = c["regions"]
            if not regs or not c.get("ranges"):
                continue
            av.bank = xbank(c)
            got = ALIAS.get(regs[0]["reg"])
            if len(regs) == 1 and got in NUM and NUM[got] in av:
                n += 1
                prefs = pof(c, given) if pof else [regname(n) for n, _t in (c.get("prefs") or [[]])[0]]
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
                _pw_drop(pw, tuple(c["prefwin"]), hidden, ghost)      # anyone else's obstacle
            if regs:
                given[c["name"]] = [(r["first"], r["last"], ALIAS.get(r["reg"])) for r in regs] \
                    if f.get("prefs_by_region") else ALIAS.get(regs[0]["reg"])
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


def _pw_hidden(st):
    """{reg: the windows linked before its list's head} (st's `prefwin_hidden`, il_prefwin_lists), or None when the
    record does not say (the traced lists: the window removal as before)."""
    h = st.get("prefwin_hidden")
    return None if h is None else {int(r): [tuple(w) for w in v] for r, v in h.items()}


def _pw_drop(pw, own, hidden=None, ghost=None):
    """NColProcessCandidate's unlink of the candidate's own window (0x10068528..) [C]: next.prev = prev; prev.next =
    next, or the head f6a0[reg] = next when it has no prev. The users walk from the head by next, so a head unlinked
    while windows linked before it are still there stays on the walk, and so does every node unlinked after it
    (their stale next pointers keep them on it): such a register is a `ghost` and loses no window more."""
    if hidden is not None:
        for r, v in hidden.items():
            if own in v:
                v.remove(own)
                return
    for r in pw:
        if own in pw[r]:
            if hidden is not None and (r in ghost or hidden.get(r) and pw[r][0] == own):
                ghost.add(r)
                return
            pw[r] = [x for x in pw[r] if x != own]
            return


def il_prefwin_lists(cands):
    """The preference-window lists f6a0[reg] as NColPrioritizeCandidates builds them (NColAddRegPrefRange 0x101850c0
    [C]): lifetimes in index order, each tied one with a window inserts {begin, end} before the first node from the
    head whose begin is not below its own, but the head pointer is set only for the first node: a window inserted
    before the head is off the list the users walk (head, next ..). cands: [(index, master reg, (begin, end))].
    -> ({reg: [windows from the head]}, {reg: [windows before the head]})."""
    lists, heads = {}, {}
    for _i, r, w in sorted(cands):
        L = lists.setdefault(r, [])
        h = heads.setdefault(r, 0)
        k = next((j for j in range(h, len(L)) if L[j][0] >= w[0]), len(L))
        L.insert(k, tuple(w))
        if k == h and len(L) > 1:
            heads[r] = h + 1                 # before the head: hidden, the head moves one place on
    return {r: L[heads[r]:] for r, L in lists.items()}, {r: L[:heads[r]] for r, L in lists.items() if heads[r]}


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


def simulate(f, prio_of=None, lists=None, prefs_of=None):
    """Allocate a function's integer single-range-set candidates with the model only: the order from the model's
    priorities within the traced buckets (ties by the traced position), each by preferences then the order walk,
    availability updated by the model's own choices. -> {candidate name: register or None}.
    lists ({name: 0..5}): the model's lists instead of the traced buckets (order_of); prefs_of(c, got): the region's
    preference list computed from the registers given so far (il_prefs) instead of the traced one."""
    st = f["state"]
    av = avail_of(st)
    pw = {int(r): [tuple(x) for x in v] for r, v in (st.get("prefwin") or {}).items()}
    hidden, ghost = _pw_hidden(st), set()
    pr = []
    for i, c in enumerate(f["cands"]):
        p = (prio_of or {}).get(c["name"])
        if p is None:
            p = prio(c, st)
        if p is None:
            p = c["prio"] - (1 << 32) if c["prio"] & 0x80000000 else c["prio"]
        pr.append(p)
    if lists is not None:
        order = order_of(f, pr, lists)
    else:
        bk = buckets(f["cands"])
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
        prefs = prefs_of(c, got) if prefs_of is not None else [regname(n) for n, _t in (c.get("prefs") or [[]])[0]]
        r = next((x for x in prefs if x in NUM and NUM[x] in av and (u is None or x in u) and
                  covers(av[NUM[x]], c["ranges"])), None) or order_walk(av, c["ranges"], **walk_args(c, st, pw))
        got[c["name"]] = r
        if r is not None:
            av[NUM[r]] = cut(av[NUM[r]], c["ranges"])
        if c.get("prefwin"):
            _pw_drop(pw, tuple(c["prefwin"]), hidden, ghost)
    return got


def cmd_sim(fns, show=10):
    n = ok = 0
    bad = []
    for f in fns:
        if not f.get("state") or "avail" not in f["state"]:
            continue
        got = simulate(f, prio_of=f.get("prio_of"), lists=f.get("lists"),
                       prefs_of=il_prefs_of(f, f["prefs_src"]) if f.get("prefs_src") is not None else None)
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
        if e.startswith("bucket:"):
            continue                     # lists_after
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
    np = edited_prio(f, edits)
    lists, lp = lists_after(f, edits)
    np.update(lp)
    base, new = simulate(f), simulate(f, np, lists)
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
    import os, c2build, c2ncol
    os.environ.setdefault("C2TRACE_ILRAW", "12")      # the IL of NColor's entry with each leaf's symbol (T S: the
    os.environ.setdefault("C2TRACE_ILSYMW", "11")     # type word, for the lists: NRun's f['il_fn'])
    hk = list(c2ncol.hooks())
    hk[c2ncol.CAND] += "," + ",".join(c2ncol.why_specs())
    st = c2build.hooks("ncol_state", None) % dict(nblk=c2ncol.NBLK, ntup=c2ncol.NTUP, ninfo=13 * c2ncol.NBLK)
    st += "," + ",".join("tf65c>0>%d*%d" % (4 * r, c2ncol.NAVAIL) for r in c2ncol.AVAIL_REGS)
    st += "," + ",".join("tf6a0>0>%d*%d" % (4 * r, c2ncol.NAVAIL) for r in c2ncol.PREFWIN_REGS)
    st += ",t119c8>0,t119cc>0,t119d0>0"
    st += ",tdccc>0>8>0>0x1c%"         # the IL after prioritization (f['il']) and before NColSubsumption (PRE_HOOK,
    os.environ.setdefault("C2TRACE_ILPAS", "28")        # f['il_pre']): il_predict on a variant whose blocks changed
    hk.append(st)
    il = c2build.hooks("il_spec", None)
    hk.append(c2build.hooks("ncol_fn", None) + ("," + il.split(",", 1)[1] if il and "," in il else ""))
    hk.append(c2build.hooks("ncol_pref", None))
    hk.append(c2ncol.PRE_HOOK)
    if len(hk) == c2ncol.REGSET:          # the calls' clobber sets and NColMdPerFuncInit's inputs (frame score,
        hk.append(c2ncol.REGSET_HOOK)     # FUNC+0xe4) as c2ncol --export --il logs them (records: regsets, perfunc)
        hk.append(c2ncol.PERFUNC_HOOK)
    return hk


def il_line_blocks(text, fn_kind=6):
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


def il_fn_lines(text, func, fn_kind=6):
    """The T lines dumped at NColor's entry event of `func` (trace_hooks' FN spec), without the `T ` prefix."""
    out, cur = [], False
    for ln in text.splitlines():
        if ln.startswith("E "):
            head, _, rest = ln.partition("|")
            cur = int(head.split()[3]) == fn_kind and ('"%s"' % func) in rest
            if cur:
                out = []
            continue
        if cur and ln.startswith("T "):
            out.append(ln[2:])
    return out


class NRun:
    """One traced 17.00 compile of `src`, function `func`: the export record (cands, state), the listing's
    instructions (tsweep.listing_instrs) and the block -> source line map. Raises RuntimeError when the function
    was not allocated (compile error)."""

    def __init__(self, src, func, cl, keep):
        import os, re, c2trace, c2ncol, tsweep
        hk = trace_hooks()
        _, _, rc, out, text = c2trace.run_trace(src, hk, cl or ["/O2"], keep=keep, quiet=True, func=func)
        fns = [f for f in c2ncol.parse_all(text) if f["cands"] and c2trace.func_match(func, f["func"] or "")]
        if c2ncol.REGSET_HOOK in hk:
            for f in fns:                     # the REGSET probe ran: a function without an event has no such leaf
                f.setdefault("regsets", [])
        if not fns:
            err = " ".join(re.findall(r"error C\d+:[^\n]*", out))[:200]
            raise RuntimeError(("compile error: " + err) if err else "function %s not allocated by NColor" % func)
        self.f = fns[0]
        self.func = self.f["func"]
        self.f["il_fn"] = il_fn_lines(text, self.func)
        asm = os.path.join(keep, os.path.splitext(os.path.basename(src))[0] + ".asm")
        self.asm = asm
        try:
            self.instrs = tsweep.listing_instrs(asm, func)
        except Exception:
            self.instrs = []
        # (il_line_blocks: the IL section's il_blocks(tups) took this name over)
        words = il_line_blocks(text).get(self.func) or next(iter(il_line_blocks(text).values()), {})
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
    if fa.get("il_fn") and fb.get("il_fn"):          # a named candidate in another list (class or locality)
        la, lb = lists_of(fa), lists_of(fb)
        out["edits"] += ["bucket:%s=%d" % (v, lb[cb[v]["name"]]) for v in both
                         if la.get(ca[v]["name"]) != lb.get(cb[v]["name"])]
    ia = sorted(both, key=lambda v: ca[v].get("index", 0))
    ib = sorted(both, key=lambda v: cb[v].get("index", 0))
    if ia != ib:
        # the base's index values, permuted into the variant's order
        vals = sorted(ca[v].get("index", 0) for v in both)
        out["edits"] += ["index:%s=%d" % (v, k) for v, k in zip(ib, vals) if ca[v].get("index", 0) != k]
    return out


def whatif_edits(edits):
    """The subset of edits ncolsim's whatif takes."""
    return [e for e in edits if e[:5] in ("+app:", "-app:") or e.startswith(("prio:", "bucket:"))]


def lists_after(f, edits):
    """The lists after `bucket:V=..` edits ({name: list}, or None without such an edit: the traced buckets stay) and
    the priorities they change ({name: prio}: a candidate made local takes the local formula nTuples - length)."""
    bk = [e for e in edits if e.startswith("bucket:")]
    if not bk:
        return None, {}
    lists = lists_of(f)
    pr = {}
    st = f["state"]
    for e in bk:
        nm, L = bucket_edit(f, lists, e)
        lists[nm] = L
        c = _cand(f, nm)
        if L in (0, 2, 4) and blockof(st, c["first"]) != blockof(st, c["last"]) and "=local" in e:
            pr[nm] = st["ntup"] - (c["last"] - c["first"])
    return lists, pr


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
    lists, lp = lists_after(g, w)
    pm = edited_prio(g, w) if w else None
    if lp:
        pm = dict(pm or {}, **lp)
    got = simulate(g, pm, lists)
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
                cur.setdefault("leafaddr", []).append(int(ln.split()[1], 16))
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


def il_inputs(f, key="il", partial_live=None, def_gap=None):
    """NColor's inputs computed from the function's IL (f['il'], or f[key]: 'il_pre' the IL before NColSubsumption):
    dict(blk, ntup, blocks, depth, cands = {name: dict(apps, defs, uses, ranges, first, last)}, live, maxlive).
    partial_live (default: the record's `partlive`, which il_state sets for the use key, else PARTIAL_LIVE): a byte /
    word write of a wider lifetime reads the rest of it in the liveness (it neither kills nor starts the value)."""
    if partial_live is None:
        partial_live = f.get("partlive", PARTIAL_LIVE)
    if def_gap is None:                  # `defsplit`: a pure definition right after the last use starts a range
        def_gap = f.get("defgap", DEF_GAP)
    tups = il_parse(f[key])
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
    cands, live_in, live_out = _lifetimes(occ, blocks, blk, partial, partial_live, def_gap)
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
DEF_GAP = 1           # a pure definition starts a new range when more than this many tuples after the previous one
                      # (NColBuildInfo's backward walk: any pure definition does, `defsplit`: 0)
PARTIAL_LIVE = False  # a partial definition (byte / word view) neither kills nor starts the value (il_inputs(.., partlive))


def _lifetimes(occ, blocks, blk, partial=frozenset(), partial_live=False, def_gap=None):
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
        if partial_live:      # a byte / word write keeps the rest of the value: it reads it, kills nothing
            lst = [(n_, d and (c, n_) not in partial) for n_, d in lst]
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
                elif not u and (c, n_) not in partial and prev is not None and \
                        n_ > prev + (DEF_GAP if def_gap is None else def_gap):
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
    x = il_inputs(f, partial_live="partlive" in use or None, def_gap=0 if "defsplit" in use else None)
    g = copy.deepcopy(f)
    if "partlive" in use:
        g["partlive"] = True
    if "defsplit" in use:
        g["defgap"] = 0
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


# ------------------------------------------------------------------ the six lists (buckets) of NColPrioritizeCandidates
#
# docs/c2/ncolor.md 2 step 3 and 12: class = (SYM type >> 12) & 0x1f, int when (1 << class) & 0x1e (classes 1..4); a
# class-6 symbol (a small struct held whole in an integer register, type 0x6004 / 0x6008) is *not* int there: it lands
# in list 0 / 1 with the xmm candidates and is processed before every integer candidate, whatever its priority.

LISTS = ("xlocal", "xglobal", "local", "global", "clocal", "cglobal")
LIST_ALIAS = dict(aggr=None, struct=None, first=None, const=None)   # by the candidate's locality: 0/1, 4/5


def il_types(tups):
    """{root SYM: its type word} from the IL's T S lines (the SYM's +0)."""
    out = {}
    for t in tups:
        for s in t.get("leafsym") or []:
            if s and len(s) > 1 and (len(s) <= 4 or not s[4] or s[4] == s[0]):
                out.setdefault(s[0], s[1])
    return out


def type_is_int(ty):
    """NColPrioritizeCandidates' int test: (1 << ((type >> 12) & 0x1f)) & 0x1e."""
    return bool((1 << ((ty >> 12) & 0x1f)) & 0x1e)


def il_const_defs(tups):
    """{root SYM: True} for candidates with exactly one definition whose sources are all constants (ICONST leaves:
    symbol and code addresses, leaf kinds 3 / 4: NColUsesOnlyConsts, the lists 4 / 5)."""
    defs = {}
    for t in tups:
        if t["kind"] != "I":
            continue
        for i, (k, r) in enumerate(zip(t["dstk"], t.get("dstr") or [])):
            if k == 1 and r:
                defs.setdefault(r, []).append(t)
    return {r: len(ts) == 1 and bool(ts[0]["srck"]) and all(k in (3, 4, 7) for k in ts[0]["srck"]) for r, ts in defs.items()}


def il_lists(f, tups=None):
    """{candidate name: list 0..5} from the IL: the class of the candidate's SYM type, local = first and last tuple
    in one block (of the IL's lifetime: the traced first / last are read later, after coalescing), constant-defined
    (one definition from constants). Candidates without a SYM type in the IL get the traced-order inference."""
    tups = tups if tups is not None else il_parse(f["il"])
    ty, cd = il_types(tups), il_const_defs(tups)
    x = il_inputs(f)
    out = traced_lists(f)
    for c in f["cands"]:
        s = c.get("sym")
        y = _ilcand(x, c)
        if s not in ty or y is None or not y["ranges"]:
            continue
        loc = x["blk"][y["first"]] == x["blk"][y["last"]]    # at prioritization: the IL's lifetime
        if not type_is_int(ty[s]):
            out[c["name"]] = 0 if loc else 1
        else:
            out[c["name"]] = (2 if loc else 3) + (2 if cd.get(s) else 0)
    return out


def lists_of(f):
    """The lists of a run: from the IL (il_lists) when the record has it, else the class from the IL of NColor's
    entry (NRun: `il_fn`) with the traced locality, else the traced-order inference."""
    if f.get("il"):
        return il_lists(f)
    out = traced_lists(f)
    if f.get("il_fn"):
        tu = il_parse(f["il_fn"])
        ty = il_types(tu)
        byname = {}                  # NColCanon may give a candidate a new SYM after NColor's entry: by name too
        for t in tu:
            for txt, r in list(zip(t["dst"], t.get("dstr") or [])) + list(zip(t["src"], t.get("srcr") or [])):
                if r in ty and not txt.startswith("["):
                    byname.setdefault(short(txt.split(":")[0].split("=")[-1]), ty[r])
        st = f["state"]
        for c in f["cands"]:
            t = ty.get(c.get("sym"), byname.get(short(c["name"])))
            if t is None or not c.get("ranges"):
                continue
            loc = blockof(st, c["first"]) == blockof(st, c["last"])
            if not type_is_int(t):
                out[c["name"]] = 0 if loc else 1
            elif out[c["name"]] < 2:
                out[c["name"]] = 2 if loc else 3
    return out


def traced_lists(f):
    """{candidate name: list 0..5} inferred from the traced order alone (exports without IL): a list ends where the
    sort order (descending priority, then ascending lifetime index) breaks; xmm candidates are in 0 / 1, integer ones
    in 2..5 by locality; an integer global at the head of the integer locals is NColMdAdjustPriority's move (list 2)."""
    st = f.get("state") or {}
    out, cur, prev = {}, -1, None
    cs = f["cands"]
    for i, c in enumerate(cs):
        p = c["prio"] - (1 << 32) if c["prio"] is not None and c["prio"] & 0x80000000 else (c["prio"] or 0)
        loc = not c.get("ranges") or blockof(st, c["first"]) == blockof(st, c["last"])
        allowed = (0, 1) if klass(c) == "xmm" else (2, 3, 4, 5)
        allowed = [L for L in allowed if (L % 2 == 0) == loc]
        brk = prev is not None and (p > prev[0] or (p == prev[0] and c.get("index", 0) < prev[1]))
        nxt = cs[i + 1] if i + 1 < len(cs) else None
        if not loc and klass(c) == "int" and cur < 2 and nxt is not None and klass(nxt) == "int" and \
                blockof(st, nxt["first"]) == blockof(st, nxt["last"]):
            L = 2                                    # NColMdAdjustPriority: the head of the int locals
        else:
            L = next((x for x in allowed if x > cur or (x == cur and not brk)), cur if cur >= 0 else allowed[0])
        out[c["name"]] = L
        cur, prev = L, (p, c.get("index", 0))
    return out


def max_zone_depth(f):
    st = f["state"]
    nb = max(st["blk"][:st["ntup"]] or [0]) + 1
    return max([depth(st, b) for b in range(nb)] or [0])


def order_of(f, pr, lists):
    """NColPrioritizeCandidates' output order: lists 0..5 in turn, each sorted by descending priority, ties by
    ascending lifetime index (NColColPriorityCompare); NColMdAdjustPriority first moves the int-global candidate
    of highest priority to the head of list 2 when the function has a loop and that priority exceeds 16 times
    max(1, the highest int-local priority) (zlib: the traced order of all 97 functions). -> candidate indices."""
    cs = f["cands"]
    L = [lists.get(c["name"], 3) for c in cs]
    head = None
    if max_zone_depth(f) and not f.get("no_adjust"):
        hl = max([pr[i] for i in range(len(cs)) if L[i] == 2] or [0])
        g = [i for i in range(len(cs)) if L[i] == 3 and pr[i] > 16 * max(1, hl)]
        if g:
            # list 3 is built by pushing on its head, scanned with `>`: a tie goes to the higher lifetime index
            head = min(g, key=lambda i: (-pr[i], -cs[i].get("index", i)))
    key = lambda i: (2 if i == head else L[i], i != head, -pr[i], cs[i].get("index", i))
    return sorted(range(len(cs)), key=key)


def bucket_edit(f, lists, e):
    """`bucket:V=local|global|aggr|const|N`: the candidate V moved to another list. local / global keep the class
    (int: 2 / 3, non-int: 0 / 1), aggr (a struct-typed value: class 6) is list 0 / 1 by V's locality, const 4 / 5,
    a number is the list. -> (name, list)."""
    v, _, to = e[7:].partition("=")
    c = _cand(f, v)
    if c is None:
        sys.exit("whatif: no candidate %s" % v)
    st = f["state"]
    cur = lists.get(c["name"], 3)
    loc = blockof(st, c["first"]) == blockof(st, c["last"])
    if to.isdigit():
        return c["name"], int(to)
    base = 0 if cur < 2 else 2
    return c["name"], dict(local=base, global_=base + 1, aggr=0 + (not loc), struct=0 + (not loc),
                           const=4 + (not loc))[to if to != "global" else "global_"]


# ------------------------------------------------------------------ trace-free inputs, part 2 (T17 item 1)
#
# What ncolsim read from the trace besides appearances / ranges / depth, computed from the IL (docs/c2/ncolor.md 12):
# the tied physreg and its preference window (L+0x38 / L+0x54, f6a0), each region's preference list (copies at the
# last use and the defs, partners already processed), the useable set (by size class; rbp / r13 out where the frame
# needs them), the anti-preference set (memory bases), the processing order (lists) and the lifetime index.

PREF_WINDOW = 15      # NColPrioritizeCandidates clips the window of a copy to / from a physreg to 15 tuples


COPY_OPS = ("mov", "movaps", "movapd", "movdqa", "movdqu", "movsdx", "movss")   # NColMdIsCopyInstr (movsd / movss:
                                                                                 # scalar float types only)


def _is_copy(t):
    return t["kind"] == "I" and t["op"] in COPY_OPS and len(t["dst"]) == 1 and len(t["src"]) == 1 and \
        t["dstk"][:1] == [1] and t["srck"][:1] == [1]


def _master_num(reg):
    """The number of a master register as the export keeps ties and windows: rax..r15 1..16, the xmm family by its
    master ymmN (112 + N)."""
    return XMASTER + int(reg[3:]) if reg.startswith("xmm") else NUM[reg]


def _leaf_reg(t, side, i):
    """A REG leaf -> ('phys', master name) / ('cand', root SYM) / None."""
    ops, ks = (t["dst"], t["dstk"]) if side == "d" else (t["src"], t["srck"])
    rs = (t.get("dstr") if side == "d" else t.get("srcr")) or []
    if i >= len(ops) or i >= len(ks) or ks[i] != 1:
        return None
    c, ph = _regleaf(ops[i])
    if ph is not None:
        g = ALIAS.get(ph)
        return ("phys", g) if g and not ph.startswith("cc") else None
    return ("cand", rs[i] if i < len(rs) and rs[i] else c)


def il_tied(tups, x):
    """{root: (master register name, (begin, end))}: L+0x38 / L+0x54 and the window NColAddRegPrefRange gets. A
    `mov cand <- phys` at the begin of one of the lifetime's ranges, or `mov phys <- cand` at the end of one; of
    several, the first in tuple order (the backward walk of NColBuildInfo keeps the last it sees); the window is that
    range clipped to PREF_WINDOW tuples from the copy."""
    out = {}
    for t in tups:
        if not _is_copy(t):
            continue
        d, sr = _leaf_reg(t, "d", 0), _leaf_reg(t, "s", 0)
        if not d or not sr:
            continue
        for me, other, frm in ((d, sr, True), (sr, d, False)):
            if me[0] != "cand" or other[0] != "phys" or me[1] in out:
                continue
            y = x["cands"].get(me[1])
            if not y:
                continue
            n = t["n"]
            for lo, hi in y["ranges"]:
                if frm and lo == n:
                    out[me[1]] = (other[1], (n, min(hi, n + PREF_WINDOW)))
                    break
                if not frm and hi == n:
                    out[me[1]] = (other[1], (max(lo, n - PREF_WINDOW), n))
                    break
    return out


PREF_LOADS = False    # a load at the last use (mov reg <- [cand..]) adds its destination: measured worse
PREF_WHOLE = False    # a def copy counts only between whole symbols (not a view): measured worse
PREF_LEA = False      # an lea def adds its base (NColMdLeaPref, as read): measured worse


# NColMdGetInitialRegisters / NColMdGetRegOrder: the first register number of the bank a type's candidates live in
_BANK = {(5, 8): 320, (5, 4): 336, (0xc, 0x10): 144, (0xc, 8): 240, (0xc, 4): 288, (0xc, 0x20): 112,
         (0xd, 0x10): 160, (0xe, 0x10): 176, (0xe, 8): 272}


def _reg_number(master, ty):
    """The register number a candidate of type ty holds in master (rcx + a dword type -> ecx, 18; xmm3 + a float ->
    fxmm3s, 339), or None."""
    if not master or ty is None:
        return None
    cl, sz = (ty >> 12) & 0x1f, ty & 0xfff
    if master.startswith("xmm"):
        b = _BANK.get((cl, sz))
        return b + int(master[3:]) if b is not None else None
    b = {8: 1, 4: 17, 2: 33, 1: 49}.get(sz)
    return b + FULL.index(master) if b is not None and master in FULL else None


def _useable_nums(c):
    """The register numbers of the region's useable set (its first sparse-BV chunk), or None."""
    u = (c.get("useable") or [None])[0]
    if not u or len(u) < 3:
        return None
    m = (u[2] << 32) | u[1]
    return {u[0] + i for i in range(64) if m >> i & 1}


def il_pref_sources(tups, x, read=False, folded_uses=False):
    """The region preference lists before they are resolved: read=True the rules as read (il_pref_sources_read,
    il_model_state's `prefread`), else the earlier fitted rule (il_pref_sources_fit: PREF_LOADS / PREF_WHOLE /
    PREF_LEA)."""
    return il_pref_sources_read(tups, x, folded_uses) if read else il_pref_sources_fit(tups, x)


def il_pref_sources_read(tups, x, folded_uses=False):
    """{root: [(tuple, (kind, partner, n))]}: NColDeterminePhysregUseable's preference list before it is resolved
    (0x10068eb8..) [C]. At the *last use* (the last node of the use list): if the tuple is a copy (NColMdIsCopyInstr:
    mov, movapd / movaps, movdqa / movdqu and their v-forms by opcode; movsd / movss / vmovsd / vmovss of a scalar float
    type), or an lea whose source is a memory operand (opcode 0x4fe / 0x501), its first destination; then for every
    *definition* in order: a copy's first source, put at the head, or an lea's base (NColMdLeaPref 0x101a3080: the
    memory leaf's +0x30 for 0x4fe [base+disp], +0x34 for 0x501 [index*scale+disp]), appended. NColAddToPreferences (0x100691f0) takes REG operands only: a
    physical register as it is (`rcx=t..`, `eax`), a candidate by the register of its region covering that tuple
    (processed candidates only), and adds the register only when its *number* is in the useable set: a partner of
    another width or bank (an ecx for a qword candidate, an fxmm0l for a float) is no preference. The entry carries
    the register number (phys) or the partner's type (cand) for that test, and the list is kept as the sequence of
    calls (`head` / `tail`): a register already in the list stays where it is (il_prefs_of builds it)."""
    by = {t["n"]: t for t in tups}
    ty = il_types(tups)
    regs = _regnames()
    out = {}

    def leaf(t, side, i):
        q = _leaf_reg(t, side, i)
        if not q:
            return None
        if q[0] == "phys":
            ops = t["dst"] if side == "d" else t["src"]
            ph = _regleaf(ops[i])[1]
            return ("phys", q[1], regs.get(ph))
        return ("cand", q[1], ty.get(q[1]))

    def copy(t):
        if t["kind"] != "I" or t["op"] not in COPY_OPS + ("vmovapd", "vmovaps", "vmovdqa", "vmovdqu", "vmovsd",
                                                          "vmovss"):
            return False
        if t["op"] in ("movsdx", "movss", "vmovsd", "vmovss"):
            w = t.get("words") or []
            return len(w) > 4 and (w[4] & 0x1f000) == 0x5000
        return True

    def memsrc(t):
        w = (t.get("srcw") or [None])[0]
        return bool(w) and len(w) > 2 and w[2] in (0x4fe, 0x501)
    for r, y in x["cands"].items():
        lst = []
        uses = [n for n in y["apps"] if r in (by[n].get("srcr") or []) or
                folded_uses and r not in (by[n].get("dstr") or [])]     # a use a fold gave it (il_apply_folds)
        defs = sorted({n for n in y["apps"] if r in (by[n].get("dstr") or [])})
        if uses:
            n = max(uses)
            t = by[n]
            if (copy(t) or t["kind"] == "I" and t["op"] == "lea" and memsrc(t)) and t["dst"]:
                q = leaf(t, "d", 0)
                if q and q[1] != r:
                    lst.append((n, q, "tail"))
        for n in defs:
            t = by[n]
            if copy(t):
                q = leaf(t, "s", 0) if t["src"] else None
                if q and q[1] != r:
                    lst.append((n, q, "head"))
            elif t["kind"] == "I" and t["op"] == "lea" and memsrc(t):
                # 0x4fe [base+disp]: the base (+0x30); 0x501 [index*scale+disp]: the index (+0x34); either is the
                # memory leaf's only register leaf, the next source (0x4ff base+index and 0x500 give none)
                q = leaf(t, "s", 1) if len(t["src"]) > 1 else None
                if q and q[1] != r:
                    lst.append((n, q, "tail"))
        if lst:
            out[r] = lst
    return out


def il_pref_sources_fit(tups, x):
    """{root: [(tuple, ('phys', reg) / ('cand', root))]}: NColDeterminePhysregUseable's preference list before it
    is resolved: at the last use, a mov (NColMdIsCopyInstr is the opcode alone) into a REG leaf: its destination
    (appended); then every mov defining the whole candidate from a whole REG leaf: the source, put at the head (the
    last def's source comes first); an lea defining it: its base (NColMdLeaPref, appended)."""
    by = {t["n"]: t for t in tups}
    out = {}
    own = lambda t, j: (t.get("leafsym") or [None] * 99)[j] if j < len(t.get("leafsym") or []) else None
    whole = lambda x: not PREF_WHOLE or x is None or len(x) < 5 or not x[4] or x[4] == x[0]
    for r, y in x["cands"].items():
        lst = []
        uses = sorted({n for n in y["apps"] if r in (by[n].get("srcr") or [])})
        defs = sorted({n for n in y["apps"] if r in (by[n].get("dstr") or [])})
        if uses:
            t = by[uses[-1]]
            m0 = (t.get("src") or [""])[0]
            direct = (t.get("srcr") or [None])[0] == r and t["srck"][:1] == [1] or PREF_LOADS and \
                m0.startswith("[") and m0[1:].split("+")[0].split("]")[0].rsplit(":", 1)[0] == \
                next((x.rsplit(":", 1)[0] for x, rr in zip(t["src"], t.get("srcr") or []) if rr == r), None)
            if t["kind"] == "I" and t["op"] == "mov" and len(t["dst"]) == 1 and t["dstk"][:1] == [1] and direct:
                q = _leaf_reg(t, "d", 0)
                if q and q[1] != r:
                    lst.append((uses[-1], q))
        for n in defs:
            t = by[n]
            if (t.get("dstr") or [None])[0] != r:
                continue
            if _is_copy(t) and whole(own(t, 0)) and whole(own(t, len(t["dst"]))):
                q = _leaf_reg(t, "s", 0)
                if q:
                    lst.insert(0, (n, q))
            elif PREF_LEA and t["kind"] == "I" and t["op"] == "lea" and t["src"] and t["src"][0].startswith("["):
                q = _leaf_reg(t, "s", 1) if len(t["src"]) > 1 else None
                if q:
                    lst.append((n, q))
        if lst:
            out[r] = lst
    return out

def il_prefs_of(f, src):
    """The prefs_of(c, got) of simulate(): the region's preference list with the partners' registers as given so far
    (got: name -> register), filtered by the useable set and without repeats."""
    names = {c.get("sym"): c["name"] for c in f["cands"]}

    def prefs_of(c, got):
        out = []
        u = useable(c)
        un = _useable_nums(c)
        for e in src.get(c.get("sym"), []):
            q = e[1]
            k, v = q[0], q[1]
            r = v if k == "phys" else got.get(names.get(v))
            if isinstance(r, list):          # [(first, last, register)]: the partner's region covering the tuple
                r = next((g for lo, hi, g in r if lo <= e[0] <= hi), None)
            if len(q) > 2 and un is not None:   # il_pref_sources_read: the register's number must be useable
                num = q[2] if k == "phys" else _reg_number(r, q[2])
                if num is not None and num not in un:
                    continue
            if r and r not in out and (u is None or r in u):
                if len(e) > 2 and e[2] == "head":    # NColAddToPreferences: prepend (a def copy) / append
                    out.insert(0, r)
                else:
                    out.append(r)
        return out
    return prefs_of


INT_MASTERS = [r for r in FULL if r != "rsp"]


def il_useable(c, ty, frame_rbp=False, size=None):
    """The first sparse-BV chunk of the useable set NColMdGetInitialRegisters gives for the candidate's type (the
    integer class master sets: every register but rsp, of the candidate's width; rbp out when the frame uses it), in
    the export's shape [base, low, high, next]; None for a non-integer candidate (xmm: the trace's banks stay)."""
    if ty is None or not ((1 << ((ty >> 12) & 0x1f)) & 0x5e):
        return None
    sz = size or (ty & 0xfff)
    base = {8: 1, 4: 17, 2: 33, 1: 49}.get(sz)
    if base is None:
        return None
    bits = 0
    for r in INT_MASTERS:
        if frame_rbp and r == "rbp":
            continue
        n = base + FULL.index(r)
        bits |= 1 << n
    return [0, bits & 0xffffffff, (bits >> 32) & 0xffffffff, 0]


def il_anti(tups):
    """{name: 0 / 1 / 2}: the anti-preference set NColMdAddToPreferences (0x10069010) gives a candidate that is the
    base of a memory operand: in a mov (opcode 0x13b) the first destination's memory operand, else the first source's
    (or the second's when the first is a memory operand with another base); in an lea its memory source. A base
    without index avoids r12 (SIB byte), without displacement rbp / r13 (disp8); the union over the appearances
    (TLS+0x119c8 r12 / cc rbp,r13 / d0 all three: the index into ANTI)."""
    out = {}

    def mem(txt):
        if not txt.startswith("[") or "]" not in txt:
            return None
        parts = txt[1:txt.index("]")].split("+")
        base = parts[0].rsplit(":", 1)[0]
        idx = next((q for q in parts[1:] if "*" in q), None)
        disp = parts[-1] if len(parts) > 1 and parts[-1].lstrip("-").isdigit() else "0"
        return base, idx, disp
    for t in tups:
        if t["kind"] != "I" or t["op"] not in ("mov", "lea"):
            continue
        ms = []
        if t["op"] == "mov":
            if t["dst"] and mem(t["dst"][0]):
                ms = [mem(t["dst"][0])]
            elif t["src"] and mem(t["src"][0]):
                ms = [mem(t["src"][0])]
        elif t["src"] and mem(t["src"][0]):
            ms = [mem(t["src"][0])]
        for base, idx, disp in ms:
            if not base or base in _regnames() or base.lstrip("-").isdigit():
                continue
            out[base] = out.get(base, 0) | (1 if idx is None else 0) | (2 if disp == "0" else 0)
    return {r: {1: 0, 2: 1, 3: 2}[m_] for r, m_ in out.items() if m_}

def _mem_fields(t, i):
    """A memory leaf's (base, index, disp, scale shift) as NColMdAddToPreferences reads them: with the raw words of
    +0x30 / +0x34 / +0x38 (C2TRACE_ILRAW >= 15) base / index are the addresses of their REG leaves; otherwise from the
    text, base / index as the position of their leaf (the memory leaf's next leaves: base, then index)."""
    w = t["leafwords"][i] if i < len(t["leafwords"]) else []
    if len(w) >= 15:
        return w[12], w[13], w[14], w[7] & 0xf
    ops = t["dst"] + t["src"]
    txt = ops[i] if i < len(ops) else ""
    if not txt.startswith("[") or "]" not in txt:
        return None
    parts = txt[1:txt.index("]")].split("+")
    has_b = bool(parts) and "*" not in parts[0] and not parts[0].lstrip("-").isdigit()
    ix = next((q for q in parts if "*" in q), None)
    sc = {"1": 0, "2": 1, "4": 2, "8": 3}.get(ix.rsplit("*", 1)[1], 0) if ix else 0
    d = parts[-1] if len(parts) > 1 and parts[-1].lstrip("-").isdigit() else "0"
    nm = lambda q: q.rsplit(":", 1)[0]
    pos = [k for k in range(len(t["dst"]), len(ops)) if k != i]   # the source leaves: a memory operand's base and
    if i >= len(t["dst"]):                                         # index follow it; a stored value comes first
        pos = [k for k in pos if k > i]
    bn = nm(parts[0]) if has_b else None
    inm = nm(ix.rsplit("*", 1)[0]) if ix else None
    bpos = [k for k in pos if nm(ops[k]) == bn] if has_b else []
    ipos = [k for k in pos if nm(ops[k]) == inm] if ix else []
    if i >= len(t["dst"]):
        base = ("pos", bpos[0]) if bpos else 0
        index = ("pos", next((k for k in ipos if not bpos or k > bpos[0]), ipos[0])) if ipos else 0
    else:
        index = ("pos", ipos[-1]) if ipos else 0
        base = ("pos", next((k for k in reversed(bpos) if not ipos or k < ipos[-1]), bpos[-1])) if bpos else 0
    return base or (("pos", -1) if has_b else 0), index or (("pos", -1) if ix else 0), int(d) != 0, sc


def il_anti_read(tups, apps, addr=None, when=None):
    """{root: 0 / 1 / 2}: NColMdAddToPreferences (0x10069010) as read [C], called by NColDeterminePhysregUseable for
    every appearance with the candidate's leaf in that tuple (a use: the first source REG leaf of the lifetime's root;
    the anti set goes to that leaf's own SYM +0x44, so a view's is not the candidate's). The memory operand's base must
    be *that leaf* (the field +0x30 points to it): mov (0x13b): the first destination if it is memory, else the first
    source; lea (0x114, the leaf not its destination): the memory source (kind 5) by base, or with no base by an index
    of scale 2 (`[i*2]` is encoded [i+i]: an index present). No index -> r12, no displacement (+0x38) -> rbp / r13;
    the union over the lifetime's appearances (apps: {root: tuple numbers}). A definition never matches (its leaf is a
    destination)."""
    out = {}
    byn = {}
    for r, ns in apps.items():
        for n in ns:
            byn.setdefault(n, set()).add(r)
    for t in tups:
        if t["kind"] != "I" or t["op"] not in ("mov", "lea"):
            continue
        nd = len(t["dst"])
        ks = list(t["dstk"]) + list(t["srck"])
        ls = t.get("leafsym") or []
        la = t.get("leafaddr") or []
        roots = list(t.get("dstr") or []) + list(t.get("srcr") or [])
        first = {}
        for j in range(nd, len(ks)):
            if ks[j] in (1, 2) and j < len(roots) and roots[j]:
                first.setdefault(roots[j], j)
        j0 = nd if t["op"] == "lea" else (0 if nd and ks[0] in (5, 6) else nd)
        hist = (addr or {}).get((t["n"], j0)) or []   # the operand as the lea folds made before the candidate's
        eff = lambda r: next((v for k, v in reversed(hist) if when is None or k < when.get(r, -1)), None)
        for r in byn.get(t["n"], ()) if hist else ():   # processing rewrote it (il_apply_folds): the folded lea's
            ov = eff(r)                                   # base is the candidate's leaf there
            if ov is not None:
                if ov["base"] == r or t["op"] == "lea" and ov["base"] is None and ov["index"] == r and \
                        ov["scale"] == 1:
                    b0, b1 = ov["index"] is None and ov["base"] == r, not ov["disp"]
                    out[r] = out.get(r, 0) | (1 if b0 else 0) | (2 if b1 else 0)
        for r, j in first.items():
            if t["n"] not in apps.get(r, ()):
                continue
            x = ls[j] if j < len(ls) else None
            if x and len(x) > 4 and x[4] and x[4] != x[0]:
                continue                              # a view: the set lands on the view's SYM
            me = la[j] if j < len(la) else None
            hit = lambda v: v != 0 and (v == ("pos", j) if isinstance(v, tuple) else me is not None and v == me)
            bits = None
            if hist and eff(r) is not None:
                continue                         # rewritten by a fold: above
            if t["op"] == "lea":
                if nd < len(ks) and ks[nd] == 5:
                    m = _mem_fields(t, nd)
                    if m and hit(m[0]):
                        bits = (m[1] == 0, not m[2])
                    elif m and m[0] == 0 and hit(m[1]) and m[3] == 1:
                        bits = (False, not m[2])
            else:
                m = _mem_fields(t, 0) if nd and ks[0] in (5, 6) else None
                if m and hit(m[0]):
                    bits = (m[1] == 0, not m[2])
                elif nd < len(ks) and ks[nd] in (5, 6):
                    m = _mem_fields(t, nd)
                    if m and hit(m[0]):
                        bits = (m[1] == 0, not m[2])
            if bits:
                out[r] = out.get(r, 0) | (1 if bits[0] else 0) | (2 if bits[1] else 0)
    return {r: {1: 0, 2: 1, 3: 2}[v] for r, v in out.items() if v}


def il_index(tups):
    """NColInitializeSyms (0x1017a350): {root SYM: lifetime index}. Tuples forward, in each instruction the destinations
    then the sources, REG leaves only: an untied leaf numbers its root as a candidate, a tied one (a preassigned
    register whose root is not itself a physreg) numbers it in the nPhys sequence; one field (SYM+0x38) for both,
    first seen wins. Exact only on the IL before NColSubsumption (`il_pre`: the coalesced copies are still there)."""
    cand, phys, seen = {}, set(), set()
    for t in tups:
        if t["kind"] != "I":
            continue
        for ops, ks, rs in ((t["dst"], t["dstk"], t.get("dstr") or []), (t["src"], t["srck"], t.get("srcr") or [])):
            for i, (k, x) in enumerate(zip(ks, ops)):
                if k != 1:
                    continue
                c, ph = _regleaf(x)
                r = rs[i] if i < len(rs) and rs[i] else (c or x)
                if r in seen:
                    continue
                if c is not None:
                    seen.add(r)
                    cand[r] = len(cand) + 1
                elif ph is not None and "=" in x:
                    seen.add(r)
                    phys.add(r)
    return cand


def _live_leaves(t, side):
    """(kind 'c' candidate / 't' tied lifetime, root, the leaf's own SYM) of an instruction's REG leaves."""
    ops, ks = (t["dst"], t["dstk"]) if side == "d" else (t["src"], t["srck"])
    rs = (t.get("dstr") if side == "d" else t.get("srcr")) or []
    ls = t.get("leafsym") or []
    nd = len(t["dst"])
    out = []
    for i, (k, x) in enumerate(zip(ks, ops)):
        if k != 1:
            continue
        c, ph = _regleaf(x)
        j = i if side == "d" else nd + i
        sym = ls[j][0] if j < len(ls) and ls[j] else None
        root = rs[i] if i < len(rs) and rs[i] else (c or x)
        if c is not None:
            out.append(("c", root, sym))
        elif ph is not None and "=" in x and not ph.startswith("cc"):
            out.append(("t", root, sym))
    return out


def il_live(tups):
    """NColBuildInfo's live count per block (f69c) and its maximum f698 [C: docs/c2/ncolor.md 12]. Liveness of the
    global lifetimes only (first and last appearance in different blocks; candidates and tied lifetimes), pruned to
    where a definition reaches (NColDoLiveness' second fixpoint: in = live-in & U pred out, out = live-out & (appears
    | in - defs)); then backwards through each block: the count starts at |out| and adds one per source whose lifetime
    is not in the live set (a birth: a value used again after it died counts again); a definition never adds, it
    takes the lifetime out unless the tuple also reads it or it is a partial view (a view not live in and not
    defined earlier in the block). Block 0 stores 0. Exact on the IL before NColSubsumption (il_pre). -> list."""
    blocks = il_blocks(tups)
    nb = len(blocks)
    blk = {}
    for i, b in enumerate(blocks):
        for t in b["tups"]:
            blk[t["n"]] = i
    first, last = {}, {}
    for t in tups:
        if t["kind"] != "I":
            continue
        for sd in "ds":
            for k, r, _s in _live_leaves(t, sd):
                first.setdefault((k, r), t["n"])
                last[(k, r)] = t["n"]
    glob = {k for k in first if blk[first[k]] != blk[last[k]]}
    gen = [set() for _ in range(nb)]
    dfs = [set() for _ in range(nb)]
    app = [set() for _ in range(nb)]
    for i, b in enumerate(blocks):
        for t in reversed(b["tups"]):
            if t["kind"] != "I":
                continue
            for k, r, _s in _live_leaves(t, "d"):
                if (k, r) in glob:
                    dfs[i].add((k, r))
                    app[i].add((k, r))
                    gen[i].discard((k, r))
            for k, r, _s in _live_leaves(t, "s"):
                if (k, r) in glob:
                    app[i].add((k, r))
                    gen[i].add((k, r))
    lin, lout = [set(g) for g in gen], [set() for _ in range(nb)]
    ch = True
    while ch:
        ch = False
        for i in range(nb - 1, -1, -1):
            o = set().union(*[lin[x] for x in blocks[i]["succ"]]) if blocks[i]["succ"] else set()
            if o != lout[i]:
                lout[i], lin[i], ch = o, gen[i] | (o - dfs[i]), True
    preds = [[] for _ in range(nb)]
    for i, b in enumerate(blocks):
        for x in b["succ"]:
            preds[x].append(i)
    r0, r4 = [set() for _ in range(nb)], [app[i] & lout[i] for i in range(nb)]
    ch = True
    while ch:
        ch = False
        for i in range(nb):
            tmp = (set().union(*[r4[p] for p in preds[i]]) if preds[i] else set()) & lin[i]
            if tmp != r0[i]:
                r0[i], r4[i], ch = tmp, lout[i] & (app[i] | (tmp - dfs[i])), True
    live = [0] * nb
    for i, b in enumerate(blocks):
        L = set(r4[i])
        cnt = len(L)
        tl = [t for t in b["tups"] if t["kind"] == "I"]
        for j in range(len(tl) - 1, -1, -1):
            t = tl[j]
            srcsyms = {sym for _k, _r, sym in _live_leaves(t, "s")}
            for k, r, sym in _live_leaves(t, "d"):
                key = (k, r)
                L.add(key)
                if sym in srcsyms:
                    continue
                if sym != r and (key in r0[i] or any(rr == r for u in tl[:j] for _kk, rr, _x in _live_leaves(u, "d"))):
                    continue
                L.discard(key)
            for k, r, _s in _live_leaves(t, "s"):
                if (k, r) not in L:
                    L.add((k, r))
                    cnt += 1
        live[i] = cnt if i > 0 else 0
    return live


CALL_CLOBBER = ("rax", "rcx", "rdx", "r8", "r9", "r10", "r11")   # a call's kind-0xa destination (Win64 volatiles) [I]


def il_regsets(f):
    """{tuple address: set of master names} from c2ncol's REGSET probe (f['regsets']: every kind-0xa leaf NColBuildInfo
    reads, a call's clobber set; a callee of the same unit compiled before clobbers only what it uses), or None when the
    export has no probe. Register numbers -> masters (ymmN -> xmmN); rsp and the not-allocatable ones dropped."""
    if "regsets" not in f:
        return None
    out = {}
    for r in f["regsets"]:
        out.setdefault(r["tup"], set()).update(
            m for m in (regname(n) for n in r["regs"] if not _upper_half(n)) if m and m != "rsp" and (m in NUM))
    return out


def _upper_half(n):
    """A register number of an upper half of the xmm family (ymmNh 128..143, xmmNh / dxmmNh / ixmmNh 196..243): it
    does not overlap the low 64 bits the fxmm banks hold (a call lists ymm0h, ymm6h..15h, the Win64 volatile upper
    halves, among its destinations: they leave the banks alone)."""
    return 128 <= n < 144 or 196 <= n < 244


def _phys_upper(ph):
    return ph in _regnames() and _upper_half(_regnames()[ph])


def il_avail(tups, ntup, regs=FULL, regsets=None):
    """NColBuildInfo's initial availability lists (NColInitializeAvailability / NColStartAvailability /
    NColTerminateAvailability, docs/c2/ncolor.md 12), integer masters: built backwards; a register is available at a
    defining tuple and at the last use of the value defined there and busy strictly between; a definition or clobber
    with no tied use later splits [.., t] / [t+1, ..]; a tied value read by the tuple that clobbers is not cut there.
    regsets ({tuple address: masters}, il_regsets): the call's clobber set read by the REGSET probe instead of
    CALL_CLOBBER (any tuple with such a leaf); regs may include xmm0..15 (the xmm family by master: il_xavail).
    -> {register number: [(begin, end)]} with the [0, 0] / [n, n] sentinels, as the export's state['avail']."""
    nodes = {r: [] for r in regs}          # finished nodes, backwards
    head = {r: [None, ntup] for r in regs}  # the open node: [begin (None: pending), end]
    live_t = {}                            # tied root -> master (live in the backward walk)
    busy = set()                           # B: masters holding a live tied value
    tl = [t for t in tups if t["kind"] == "I"]

    def setbeg(r, v):
        if r in head and head[r][0] is None:
            head[r][0] = v

    def start(A, t, T):
        rd = {ALIAS.get(_regleaf(x)[1]) for k, x in zip(T["srck"], T["src"]) if k == 1 and _regleaf(x)[1]}
        for r in A:
            if r in busy or r in rd or r not in head:
                continue
            nodes[r].append(head[r])
            head[r] = [None, t]
    for T in reversed(tl):
        t = T["n"]
        A = set()
        srcroots = set(T.get("srcr") or [])
        if regsets is not None:
            A |= regsets.get(T["text"].split()[1], set()) & set(regs)
        elif T["op"] == "call":
            # the Win64 volatiles; a callee of this unit compiled before may clobber fewer (interprocedural register
            # usage: zlib _tr_init -> init_block keeps rcx, r9..r11): read by c2ncol's REGSET probe (regsets)
            A |= set(CALL_CLOBBER)
        for i, (k, x) in enumerate(zip(T["dstk"], T["dst"])):
            if k != 1:
                continue
            c, ph = _regleaf(x)
            if ph is None or ph.startswith("cc") or _phys_upper(ph):
                continue
            m = ALIAS.get(ph)
            if m is None or m not in head:
                continue
            r = (T.get("dstr") or [None] * 9)[i]
            if "=" not in x:
                A.add(m)                   # a physreg-root destination (eax <- x): kills
            elif r not in srcroots:
                live_t.pop(r, None)
                busy.discard(m)
                A.add(m)
        for r in A:
            setbeg(r, t + 1)
        if A:
            start(A, t, T)
        for i, (k, x) in enumerate(zip(T["srck"], T["src"])):
            if k != 1 or "=" not in x:
                continue
            c, ph = _regleaf(x)
            m = ALIAS.get(ph) if ph else None
            if m is None or ph.startswith("cc") or _phys_upper(ph) or m not in head:
                continue
            r = (T.get("srcr") or [None] * 9)[i]
            if r not in live_t:
                live_t[r] = m
                setbeg(m, t)
                busy.add(m)
    out = {}
    for r in regs:
        if r == "rsp":
            continue
        lst = [head[r]] + nodes[r][::-1]   # forward order: the open head first
        out[NUM[r]] = [(0, 0)] + [(b or 0, e) for b, e in lst] + [(ntup, ntup)]
    return out


def il_xavail(tups, ntup, regsets=None):
    """The xmm availability banks (fxmm0l..15l 320.., fxmm0s..15s 336..: one list per master xmmK, both banks the same)
    by il_avail's rules over xmm0..15: a tied xmm leaf (xmm0=t..), a physreg destination, a call's REGSET (its ymm
    registers). Without the probe, as measured on the probe's sets of calls to other units: the Win64 volatiles
    xmm0..5 in a function that uses floating point (il_uses_float), all of xmm0..15 in one that does not (zlib's
    calls list ymm0..15: nothing of the xmm family lives across a call there) [I]."""
    xs = list(XMM_ORDER)
    if regsets is None:
        regsets = {}
        vol = {"xmm%d" % k for k in range(6 if il_uses_float(tups) else 16)}
        for T in tups:
            if T["kind"] == "I" and T["op"] == "call":
                regsets[T["text"].split()[1]] = set(CALL_CLOBBER) | vol
    av = il_avail(tups, ntup, xs, regsets)
    out = {}
    for k in range(16):
        out[XBANKS[0] + k] = out[XBANKS[1] + k] = av[NUM["xmm%d" % k]]
    return out


def il_premap(pre, post):
    """{pre-Subsumption root: post root} from the two dumps of one stream (a removed copy is an empty op577 in the
    same slot; the absorbed lifetime's leaves are renamed to views of the survivor)."""
    out = {}
    for a, b in zip(pre, post):
        if a["kind"] != "I" or b["kind"] != "I" or b["op"] == "op577" and not b["dst"] and not b["src"]:
            continue
        for ra, rb in list(zip(a.get("dstr") or [], b.get("dstr") or [])) + list(zip(a.get("srcr") or [],
                                                                                  b.get("srcr") or [])):
            if ra and rb:
                out.setdefault(ra, rb)
    return out


SUBSUME_ADJACENT = True   # il_subsume: the union joins ranges that only touch (in one block or across an edge);
                          # "dead-first" (`deadfirst`): only a dead lifetime's range right before a survivor's


def il_subsume(pre, xpre, index=None, adjacent=None):
    """NColSubsumption (0x1009ab40) + NColCombineLifetimes (0x101475b0) on the IL before it (docs/c2/ncolor.md 12):
    lifetimes in index order; for each copy `mov L <- S` defining the whole L from an untied S at offset 0: no def of
    L inside S's ranges (but at a range end, the copy L <- S itself or a tuple reading S where S dies), and no def of
    S inside L's (unless S has one use and one def, L is not used inside S and S's def is in the copy's block); the
    wider survives, at equal size S when its leaf is a view or L is a temporary and S is not, else L. The copy goes
    (an empty op577), the dead lifetime's lists join the survivor's. xpre: il_inputs(f, "il_pre"). adjacent: whether
    the union joins ranges that only touch (SUBSUME_ADJACENT; "dead-first": only a dead lifetime's range right before
    one of the survivor's, measured: zlib's code$1 / huff$1 keep [.., n] [n + 1, ..] apart where the survivor's range
    comes first). -> ({root: survivor root}, {survivor: lifetime})."""
    adj = SUBSUME_ADJACENT if adjacent is None else adjacent
    index = index or il_index(pre)
    by = {t["n"]: t for t in pre}
    syms = {}
    for t in pre:
        for x in t.get("leafsym") or []:
            if x:
                syms.setdefault(x[0], x)
    blocks = il_blocks(pre)
    blk = {}
    for i, b in enumerate(blocks):
        for t in b["tups"]:
            blk[t["n"]] = i
    L = {}
    for r, y in xpre["cands"].items():
        defs = sorted({n for n in y["apps"] if r in (by[n].get("dstr") or [])})
        uses = sorted({n for n in y["apps"] if r in (by[n].get("srcr") or [])})
        L[r] = dict(ranges=[list(q) for q in y["ranges"]], apps=list(y["apps"]), defs=defs, uses=uses)
    alias = {r: r for r in L}
    find = lambda r: r if alias.get(r, r) == r else find(alias[r])
    temp = lambda r: r in syms and (syms[r][3] & 0xff) == 3 and (len(syms[r]) <= 7 or syms[r][7] == 0)
    size = lambda r: syms[r][10] if r in syms and len(syms[r]) > 10 else 8

    def reads(n, r):
        return any(find(q) == r for q in (by[n].get("srcr") or []) if q)

    def modified(A, Bk, skip):
        a, b = L[A], L[Bk]
        rg = b["ranges"]
        for d in a["defs"]:
            for k, (lo, hi) in enumerate(rg):
                if lo <= d <= hi and d != skip:
                    if d != hi:
                        return True
                    t = by[d]
                    if _is_copy(t) and find((t.get("dstr") or [None])[0]) == A and \
                            find((t.get("srcr") or [None])[0]) == Bk:
                        continue
                    if reads(d, Bk) and (k + 1 >= len(rg) or rg[k + 1][0] > d + 1):
                        continue
                    return True
        return False

    def merge_ranges(a, b):
        out = []
        src = []                                  # which list each joined range came from
        for lo, hi, w in sorted([(lo, hi, 0) for lo, hi in a] + [(lo, hi, 1) for lo, hi in b]):
            touch = out and lo == out[-1][1] + 1 and (
                blk.get(lo) == blk.get(out[-1][1]) or blk.get(lo) in blocks[blk.get(out[-1][1], 0)]["succ"])
            if touch and adj == "dead-first":     # only a dead range right before a survivor's joins
                touch = src[-1] == 1 and w == 0
            if out and (lo <= out[-1][1] or adj and touch):
                out[-1][1] = max(out[-1][1], hi)
                src[-1] = w if hi >= out[-1][1] else src[-1]
            else:
                out.append([lo, hi])
                src.append(w)
        return out
    order = sorted(L, key=lambda r: index.get(r, 1 << 30))
    i = 0
    while i < len(order):
        r = order[i]
        if find(r) != r:
            i += 1
            continue
        restart = False
        for t_n in list(L[r]["defs"]):
            T = by[t_n]
            if not _is_copy(T) or find((T.get("dstr") or [None])[0]) != r:
                continue
            dsym = (T.get("leafsym") or [None])[0]
            if dsym and len(dsym) > 4 and dsym[4] and dsym[4] != dsym[0]:
                continue                                  # a view of L: not a whole-symbol def
            sl = _leaf_reg(T, "s", 0)
            if not sl or sl[0] != "cand":
                continue
            S = find(sl[1])
            ssym = (T.get("leafsym") or [None, None])[len(T["dst"])] if len(T.get("leafsym") or []) > len(T["dst"]) else None
            if S == r or S not in L or (ssym and len(ssym) > 11 and ssym[11]):
                continue
            if modified(r, S, t_n):
                continue
            if modified(S, r, 0):
                a, b = L[S], L[r]
                if not (len(a["uses"]) == 1 and len(a["defs"]) == 1 and blk.get(a["defs"][0]) == blk.get(t_n) and
                        not any(a["ranges"][0][0] < u <= a["ranges"][-1][1] for u in b["uses"])):
                    continue
            dsz = (T.get("dstv") or [(True, None)])[0][1] or size(r)
            ssz = size(S)
            if dsz > ssz:
                surv, dead = r, S
            elif dsz < ssz:
                surv, dead = S, r
            elif ssym and len(ssym) > 4 and ssym[4] and ssym[4] != ssym[0]:
                surv, dead = S, r
            elif temp(r) and not temp(S):
                surv, dead = S, r
            else:
                surv, dead = r, S
            for x, key in ((L[r], "defs"), (L[r], "apps"), (L[S], "uses"), (L[S], "apps")):
                if t_n in x[key]:
                    x[key].remove(t_n)
            a, b = L[surv], L[dead]
            a["apps"] = sorted(a["apps"] + b["apps"])
            a["defs"] = sorted(a["defs"] + b["defs"])
            a["uses"] = sorted(a["uses"] + b["uses"])
            a["ranges"] = merge_ranges(a["ranges"], b["ranges"])
            L[dead] = dict(ranges=[], apps=[], defs=[], uses=[])
            alias[dead] = surv
            restart = surv == r
            break
        if not restart:
            i += 1
    return {q: find(q) for q in L}, {q: v for q, v in L.items() if find(q) == q}


def _copy_op(t):
    """NColMdIsCopyInstr 0x10069170 [C]: by opcode only (mov, movapd / movaps, movdqa / movdqu and their v-forms;
    movsd / movss / vmovsd / vmovss of a scalar float tuple type), whatever the operands."""
    if t["kind"] != "I" or t["op"] not in COPY_OPS + ("vmovapd", "vmovaps", "vmovdqa", "vmovdqu", "vmovsd", "vmovss"):
        return False
    if t["op"] in ("movsdx", "movss", "vmovsd", "vmovss"):
        w = t.get("words") or []
        return len(w) > 4 and (w[4] & 0x1f000) == 0x5000
    return True


def il_tied_pre(pre, xpre, xpost, premap, read=False, folded=()):
    """L+0x38 / L+0x54 as NColBuildInfo sets them on the stream before NColSubsumption (backward walk, both sites
    overwrite: the qualifying copy with the smallest tuple number is kept): a copy `mov phys <- x` where the use is
    the end of one of x's ranges, or any copy `mov x <- phys`; NColCombineLifetimes gives the survivor the absorbed
    lifetime's tie when it has none; the window comes from the survivor's ranges at prioritization (xpost)."""
    tie = {}
    for t in pre:
        if read and _copy_op(t) and t["dst"]:
            # 0x100d7cf8..0x100d7d2d [C]: where a use starts a new range in the backward walk (the tuple ends one
            # of the candidate's ranges), a copy by opcode whose first destination is a physreg ties *every*
            # candidate it reads there, an address component of a memory source too (`mov rdx <- [t223+32]`)
            d = _leaf_reg(t, "d", 0)
            if d and d[0] == "phys":
                for i in range(len(t["src"])):
                    sr = _leaf_reg(t, "s", i)
                    if not sr or sr[0] != "cand":
                        continue
                    y = xpre["cands"].get(sr[1])
                    if y and any(hi == t["n"] for _lo, hi in y["ranges"]):
                        tie.setdefault(sr[1], (d[1], t["n"]))
        if not _is_copy(t):
            continue
        d, sr = _leaf_reg(t, "d", 0), _leaf_reg(t, "s", 0)
        if not d or not sr:
            continue
        n = t["n"]
        if d[0] == "cand" and sr[0] == "phys":
            tie.setdefault(d[1], (sr[1], n))
        elif sr[0] == "cand" and d[0] == "phys":
            y = xpre["cands"].get(sr[1])
            if y and any(hi == n for _lo, hi in y["ranges"]):
                tie.setdefault(sr[1], (d[1], n))
    post = {}
    for r in folded:          # NColFoldTooCheap zeroes a folded lifetime (memset): its tie goes with it, although its
        tie.pop(r, None)      # leaves are renamed to the source like an absorbed copy's
    for r, v in sorted(tie.items(), key=lambda kv: (premap.get(kv[0]) != kv[0], kv[1][1])):
        q = premap.get(r, r)          # the survivor's own tie first, then an absorbed one
        post.setdefault(q, v)
    out = {}
    for q, (reg, n) in post.items():
        y = xpost["cands"].get(q)
        if not y:
            continue
        for lo, hi in y["ranges"]:
            if lo == n:
                out[q] = (reg, (n, min(hi, n + PREF_WINDOW)))
                break
            if hi == n:
                out[q] = (reg, (lo if not (n > PREF_WINDOW and lo <= n - PREF_WINDOW) else n - PREF_WINDOW, n))
                break
        else:
            out[q] = (reg, None)
    return out


def il_rbp_reserved(tups, speed=True):
    """NColMdPerFuncInit's rbp reservation (0x1009271d..: rbp becomes a frame base and leaves every useable set) when
    the frame-reference score NColCanon accumulates (TLS+0x1196c) reaches 6 * 24 (speed; 6 * 12 otherwise). The score
    as fitted on zlib [I]: 6 per leaf of a stack symbol (kind-2 leaf of a SYM of kind 4 / 5) plus 1 per kind-5 memory
    operand; zlib: inflate, inflateBack and inflate_table (146) reserve it, the next highest scores 54."""
    sc = 0
    for t in tups:
        if t["kind"] != "I":
            continue
        ls = t.get("leafsym") or []
        for i, k in enumerate(t["dstk"] + t["srck"]):
            x = ls[i] if i < len(ls) else None
            if k == 2 and x and len(x) > 3 and (x[3] & 0xff) in (4, 5):
                sc += 6
            elif k == 5:
                sc += 1
    return sc >= 6 * (24 if speed else 12)


def il_addr_folds(tups, x):
    """The address temporaries NColor folds away while allocating [C for the place, measured for the rule]: an
    `lea T <- [S + disp]` (OPBASED source, 0x4fe) that is T's only definition, every other appearance of T a base or
    index of a memory operand. NColProcessCandidate (single def, NColMdIsFoldableDef, NColCanRecalc) finds recomputing
    cheaper than a register (NColFoldAwayRegion): T gets none, the address goes into its uses, S's lists lose the lea
    and gain T's uses, S's range grows over them (zlib: 550 of 550 such temps, none of the 128 with a plain register
    use). -> [(T, S, lea tuple)] in tuple order."""
    by = {t["n"]: t for t in tups}
    out = []
    for t in tups:
        if t["kind"] != "I" or t["op"] != "lea" or len(t["src"]) != 2 or not t.get("srcw"):
            continue
        w = t["srcw"][0]
        if len(w) < 3 or w[2] != 0x4fe:
            continue
        T = (t.get("dstr") or [None])[0]
        S = (t.get("srcr") or [None, None])[1]
        y = x["cands"].get(T)
        if not T or not S or T == S or not y or S not in x["cands"]:
            continue
        if sorted(set(y.get("defs") or [n for n in y["apps"] if T in (by[n].get("dstr") or [])])) != [t["n"]]:
            continue
        ok = True
        for n in set(y["apps"]) - {t["n"]}:
            u = by[n]
            if T in (u.get("dstr") or []):
                ok = False
                break
            names = {o.rsplit(":", 1)[0] for o, r in zip(u["src"], u.get("srcr") or []) if r == T}
            inside = set()
            for o in u["dst"] + u["src"]:
                if o.startswith("[") and "]" in o:
                    inside |= {q.split("*")[0].rsplit(":", 1)[0] for q in o[1:o.index("]")].split("+")}
            if not names or not names <= inside:
                ok = False
                break
        if ok:
            out.append((T, S, t["n"]))
    return out


LOADFOLD_ALU = ("add", "sub", "xor", "and", "or", "adc", "sbb")
LOADFOLD_EXT = ("movsxd", "movzx", "_movzxw", "movsx", "_movsxw", "test")


def il_load_folds(tups, x):
    """Loads NColor folds into their only use while allocating (NColSourceOfLoadCanBeFolded / NColDoLoadFold, read
    by name only; the rule is measured on zlib): `mov T <- [mem]` is T's only definition, T has one use u, and u can
    take the memory operand in T's place: a two-operand ALU op (add sub xor and or adc sbb) reading T second into a
    register that is also its first source, or an extension / test reading T first; u in the load's block (a use in
    another block is left alone: measured). T gets no register; the memory
    operand's base and index lifetimes appear at u instead of the load and live until u. -> [(T, load, u, [base and
    index roots])]."""
    by = {t["n"]: t for t in tups}
    out = []
    for r, y in x["cands"].items():
        defs = sorted({n for n in y["apps"] if r in (by[n].get("dstr") or [])})
        uses = sorted({n for n in y["apps"] if r in (by[n].get("srcr") or [])} - set(defs))
        if len(defs) != 1 or len(uses) != 1 or uses[0] < defs[0]:
            continue
        d, u = by[defs[0]], by[uses[0]]
        if d["op"] != "mov" or len(d["dst"]) != 1 or not d["src"] or (d["srck"] or [0])[0] not in (5, 6):
            continue
        sr = u.get("srcr") or []
        if sr.count(r) != 1:
            continue
        pos = sr.index(r)
        mem_dst = any(o.startswith("[") for o in u["dst"])
        if u["op"] in LOADFOLD_ALU:
            ok = pos == 1 and not mem_dst and (u.get("dstr") or [None])[0] == sr[0] and not u["src"][0].startswith("[")
        elif u["op"] in LOADFOLD_EXT:
            ok = pos == 0 and not mem_dst and not any(o.startswith("[") for o in u["src"][1:])
        else:
            ok = False
        if ok and x["blk"][d["n"]] == x["blk"][u["n"]]:
            regs = [q for q in (d.get("srcr") or [])[1:] if q and q in x["cands"]]
            out.append((r, d["n"], u["n"], regs))
    return out


def il_apply_load_folds(x, folds):
    """x with the load folds applied: each base / index lifetime has the use tuple for the load and lives until it."""
    import copy
    cands = copy.deepcopy(x["cands"])
    for _T, d, u, regs in folds:
        for q in regs:
            a = cands.get(q)
            if a is None:
                continue
            apps = list(a["apps"])
            if d in apps:
                apps[apps.index(d)] = u
            a["apps"] = sorted(apps)
            rg = [list(v) for v in a["ranges"]]
            for v in rg:
                if v[0] <= d <= v[1] and v[1] < u:
                    v[1] = u
            merged = []
            for lo, hi in sorted(rg):
                if merged and lo <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], hi)
                else:
                    merged.append([lo, hi])
            a["ranges"] = [tuple(v) for v in merged]
            a["first"], a["last"] = merged[0][0], merged[-1][1]
    return dict(x, cands=cands)


def il_apply_addr_folds(x, folds, blocks=None):
    """x (il_inputs) with the folds of il_addr_folds applied to the sources' lifetimes (a copy of x['cands'];
    a fold of a temporary that is itself folded goes to its source)."""
    import copy
    cands = copy.deepcopy(x["cands"])
    to = {}
    for T, S, n in folds:
        while S in to:
            S = to[S]
        a, b = cands.get(S), cands.get(T)
        if a is None or b is None:
            continue
        apps = list(a["apps"])
        if n in apps:
            apps.remove(n)
        apps += [q for q in b["apps"] if q != n]
        a["apps"] = sorted(apps)
        rg = sorted([list(q) for q in a["ranges"]] + [list(q) for q in b["ranges"]])
        merged = []
        for lo, hi in rg:
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        a["ranges"] = [tuple(q) for q in merged]
        a["first"], a["last"] = merged[0][0], merged[-1][1]
        pass                                   # T keeps its lists: it is still processed (and folded)
        to[T] = S
    return dict(x, cands=cands), to


INF = 0x7fffffff


def _leaf_root(t, j):
    """The root SYM of leaf j of a tuple (destinations then sources), or None."""
    rs = list(t.get("dstr") or []) + list(t.get("srcr") or [])
    return rs[j] if 0 <= j < len(rs) else None


def _mem(t, j):
    """Memory / address leaf j of a tuple as NColMdNoFoldBenefit reads it: dict(op = leaf opcode (+8: 0x4fe OPBASED,
    0x4ff base+index, 0x500 OPLOCALSCALED, 0x501 OPSCALED), base / index = the root SYM of the REG leaf +0x30 / +0x34
    point to (None: no such field), scale = +0x1c & 0xf (log2), sym = +0x2c, disp = +0x38). With the raw words of
    C2TRACE_ILRAW >= 15 the fields are the words; else from the text (_mem_fields)."""
    w = t["leafwords"][j] if j < len(t["leafwords"]) else []
    if len(w) < 12:
        return None
    la = t.get("leafaddr") or []
    if len(w) >= 15:
        def at(a):
            if not a:
                return None
            k = la.index(a) if a in la else -1
            return _leaf_root(t, k) or ("leaf", a)
        b, ix, d = at(w[12]), at(w[13]), w[14]
        d = d - (1 << 32) if d & 0x80000000 else d
    else:
        m = _mem_fields(t, j)
        if m is None:
            return None
        pos = lambda v: (_leaf_root(t, v[1]) or ("leaf", v)) if isinstance(v, tuple) else None
        b, ix, d = pos(m[0]), pos(m[1]), int(m[2])
    return dict(op=w[2], kind=w[3] & 0xff, base=b, index=ix, scale=w[7] & 0xf, sym=w[11], disp=d)


def _plain_src(t, T):
    """Does the tuple read T as a plain REG source (a leaf whose +0x1c lacks 0x80, the address-component bit)?"""
    nd = len(t["dst"])
    for j, (k, r) in enumerate(zip(t["srck"], t.get("srcr") or [])):
        w = t["leafwords"][nd + j] if nd + j < len(t["leafwords"]) else []
        if k == 1 and r == T and len(w) > 7 and not w[7] & 0x80:
            return True
    return False


def _use_mem(u, T, side=False):
    """The use's memory operand holding T (NColMdNoFoldBenefit 0x100cb24f..: the first source of kind 5 / 6 with T as
    its base or index, else the first destination); side=True: (its leaf index, the operand)."""
    nd = len(u["dst"])
    for j, k in enumerate(u["srck"]):
        if k in (5, 6):
            m = _mem(u, nd + j)
            if m and T in (m["base"], m["index"]):
                return (nd + j, m) if side else m
    m = _mem(u, 0) if nd else None
    return (0, m) if side else m


def _fold_address(m, a, T):
    """The address NColMdFoldDef leaves in a use whose operand m holds T when T = lea a is folded into it (base,
    index, scale, disp; None if the model has no form for it): T as the base gives a's registers with m's index (an
    index-only a beside m's index moves m's index of scale 1 to the base); T as an index of scale 1 takes a's
    register(s) the same way; the displacements add."""
    out = dict(m, disp=(m["disp"] or 0) + (a["disp"] or 0))
    ab, ai = a["base"], a["index"]
    if m["base"] == T and m["index"] != T:
        if ai is None:
            out.update(base=ab)
        elif ab is None and m["index"] is None:
            out.update(base=None, index=ai, scale=a["scale"])
        elif ab is None and m["scale"] == 0:
            out.update(base=m["index"], index=ai, scale=a["scale"])
        elif m["index"] is None:
            out.update(base=ab, index=ai, scale=a["scale"])
        else:
            return None
    elif m["index"] == T and m["base"] != T:
        if ab is not None and ai is None and m["base"] is None:
            out.update(base=ab if m["scale"] == 0 else None, index=None if m["scale"] == 0 else ab)
        elif ab is not None and ai is None:
            out.update(index=ab)
        elif ab is None and m["scale"] == 0:
            out.update(index=ai, scale=a["scale"])
        else:
            return None
    else:
        return None
    return out


def il_nofold_benefit(u, d, T, k=0, can_mem=None):
    """NColMdNoFoldBenefit (0x100cb140; ecx = use, edx = def, T's SYM, the speed flag) [C for the lea and load
    paths]: what keeping T in a register is worth at the use u, INF (0x7fffffff) when the definition cannot be folded
    into it. lea def (0x100cb229..0x100cb403): INF when u reads T as a plain register; else the use's memory operand
    holding T: occ = T's places in it (base, index); regs = the lea address's base + index (+1 for OPLOCALSCALED);
    others = the use address's other registers (+1 for OPLOCALSCALED); n = regs * occ + k + others, INF when n > 2,
    when both carry a scale or both a symbol (+0x2c), when T twice needs an unscaled lea address without symbol, or T
    a scaled index a one-register lea address without symbol; else 0. k (the last argument): NColFoldTooCheap passes
    1 when favoring speed, without floating point (FUNC+0xe4 & 0x80) and TLS+0x1073c != 1 (0x100cb318..); the
    processing-time call passes 0. A load (`mov T <- [mem]`, kind 6, 0x100cb1c1..): INF when u's first source is wider
    than the memory, else 1 when u can take the memory operand in T's place (NColMdCanReplaceCandWithMemWithAlignment:
    `can_mem(u, T)`, the measured shapes of il_load_folds by default), else INF. Other definitions: None (not
    modelled: constants, symbol addresses)."""
    if d["op"] == "lea":
        if _plain_src(u, T):
            return INF
        m = _use_mem(u, T)
        a = _mem(d, len(d["dst"])) if d["srck"][:1] == [5] else None
        if m is None or a is None:
            return INF
        occ = (m["base"] == T) + (m["index"] == T)
        if occ > 1 and (a["scale"] or a["sym"]):
            return INF
        regs = (a["base"] is not None) + (a["index"] is not None) + (a["op"] == 0x500)
        others = 0
        if m["base"] is not None and m["base"] != T:
            others = 1
        elif m["base"] == T and a["scale"] and occ > 1:
            return INF
        if m["index"] is not None:
            if m["index"] != T:
                others += 1
            elif m["scale"] and (regs > 1 or a["sym"]):
                return INF
        others += m["op"] == 0x500
        if regs * occ + k + others > 2:
            return INF
        if m["scale"] and a["scale"] or m["sym"] and a["sym"]:
            return INF
        return 0
    if d["op"] == "mov" and d["srck"][:1] == [6]:
        nd = len(d["dst"])
        su = u["srcw"][0] if u.get("srcw") else []
        sd = d["srcw"][0] if d.get("srcw") else []
        if len(su) > 4 and len(sd) > 4 and (su[4] & 0xfff) > (sd[4] & 0xfff):
            return INF
        return 1 if (can_mem or _can_mem)(u, T) else INF
    return None


def _can_mem(u, T):
    """NColMdCanReplaceCandWithMemWithAlignment, by the shapes il_load_folds measured: a two-operand ALU op reading T
    second into a register that is its first source, or an extension / test reading T first."""
    sr = u.get("srcr") or []
    if sr.count(T) != 1:
        return False
    pos = sr.index(T)
    mem_dst = any(o.startswith("[") for o in u["dst"])
    if u["op"] in LOADFOLD_ALU:
        return pos == 1 and not mem_dst and (u.get("dstr") or [None])[0] == sr[0] and not u["src"][0].startswith("[")
    if u["op"] in LOADFOLD_EXT:
        return pos == 0 and not mem_dst and not any(o.startswith("[") for o in u["src"][1:])
    if u["op"] == "cmp" and CMP_FOLD:          # cmp r/m, r / imm (T first) or cmp r, r/m (T second), the other
        other = u["src"][1 - pos] if pos < 2 and len(u["src"]) > 1 else "["     # operand not memory
        return not other.startswith("[")
    return False


MEM_MODIFIED = True  # _can_recalc: a load's memory counts as modified by a call or store inside the lifetime
CMP_FOLD = True     # _can_mem: a cmp reading T first takes the memory operand (zlib: 5 of 5 such loads folded)


def _foldable_def(d):
    """NColMdIsFoldableDef 0x10066310 [C]: an lea of a kind-5 address with a 64-bit type; a mov / movapd / movaps /
    movdqa (and movsd / movss under 16 bytes) whose first source is not a register."""
    if d["kind"] != "I" or not d["src"]:
        return False
    ty = (d.get("words") or [0] * 5)[4] if len(d.get("words") or []) > 4 else 0
    if d["op"] == "lea":
        return d["srck"][:1] == [5] and (ty & 0xfff) == 8
    if d["op"] in ("mov", "movapd", "movaps", "movdqa") or d["op"] in ("movsdx", "movss") and (ty & 0xfff) < 0x10:
        return d["srck"][:1] != [1]
    return False


def _can_recalc(d, T, lives, by, own=None):
    """NColCanRecalc 0x1009ae10 [C, the memory test approximated]: one destination; every REG source untied (no
    preassigned register) and not redefined inside T's lifetime (NColCandModifiedInLife: a definition of the source
    strictly inside one of T's ranges); at most one memory source, not written inside T's lifetime
    (NColMemOprModifiedInLife: approximated by: T's uses in the definition's block, no call or store between)."""
    if len(d["dst"]) != 1:
        return False
    own = own or {d["n"]}
    y = lives.get(T)
    if not y:
        return False
    for j, (k, r) in enumerate(zip(d["srck"], d.get("srcr") or [])):
        if k == 1:
            c, ph = _regleaf(d["src"][j])
            if ph is not None:
                return False
            q = lives.get(r)
            if q is None:
                continue
            for n in q.get("defs") or [n for n in q["apps"] if r in (by[n].get("dstr") or [])]:
                if n not in own and any(lo < n < hi for lo, hi in y["ranges"]):
                    return False
        elif k == 0 or k is None or k > 7:
            return False
    if 6 in d["srck"] and MEM_MODIFIED:            # NColMemOprModifiedInLife, approximated: a call or a store
        for lo, hi in y["ranges"]:                 # strictly inside T's lifetime
            for n in range(lo + 1, hi):
                t = by.get(n)
                if t is not None and t["kind"] == "I" and (t["op"] == "call" or t["dstk"][:1] in ([5], [6])):
                    return False
    return True


def il_fold_too_cheap(pre, xpre, k=1):
    """NColFoldTooCheap (0x10066040, after NColSubsumption, before the priorities) [C]: lifetimes in index order;
    one definition (or several identical ones, CmpInstrs) that NColMdIsFoldableDef accepts, NColCanRecalc agrees, with
    at least one REG source and no memory (kind 6) source (so: an lea), and NColMdNoFoldBenefit(use, def, T, k)
    finite at every use: the definition goes (op577), its uses address the lea's operands, the sources' use and
    appearance lists gain T's uses. k = 1 favoring speed in a function without floating point (FUNC+0xe4 & 0x80) and
    TLS+0x1073c != 1, else 0 (docs/c2/tls17.md). -> [(T, [def tuples], source roots)]."""
    by = {t["n"]: t for t in pre}
    out = []
    for T, y in xpre["cands"].items():
        defs = sorted({n for n in y["apps"] if T in (by[n].get("dstr") or [])})
        uses = sorted({n for n in y["apps"] if T in (by[n].get("srcr") or [])} - set(defs))
        same = lambda t: (t["op"], t["text"].split(" | ")[-1])
        if not defs or not uses or any(same(by[n]) != same(by[defs[0]]) for n in defs[1:]):
            continue
        d = by[defs[0]]
        if not _foldable_def(d) or not any(kk == 1 for kk in d["srck"]) or 6 in d["srck"]:
            continue
        if not _can_recalc(d, T, xpre["cands"], by, set(defs)):
            continue
        if all((il_nofold_benefit(by[n], d, T, k) or 0) != INF for n in uses):
            out.append((T, defs, [r for kk, r in zip(d["srck"], d.get("srcr") or []) if kk == 1 and r]))
    return out


def il_proc_folds(tups, x, can_mem=None):
    """The folds NColProcessCandidate makes while allocating (NColFoldAwayRegion; docs/c2/ncolor.md 3.1, 12.5) [C for
    the test, the memory tests approximated]: a candidate with exactly one definition that NColMdIsFoldableDef accepts
    and NColCanRecalc agrees with gets a benefit per use, NColMdNoFoldBenefit(use, def, T, 0): an lea 0 at every use
    whose address can take its operands (n = regs * occ + others <= 2, k = 0 here), a load 1 at a use that can take
    the memory operand; NColBenefitOfUsesInRange sums them shifted by twice the use's zone depth (INF if any is);
    NColProcessCandidateRegion folds when MdmDefCost (1 favoring speed) << 2 * the definition's zone depth, times the
    number of definitions, is not below that sum. -> [(T, def tuple, [use tuples], [source roots], kind 'lea' /
    'load')] in tuple order."""
    by = {t["n"]: t for t in tups}
    blk, depth = x["blk"], x["depth"]
    zd = lambda n: depth[blk[n]] if blk[n] < len(depth) else 0
    out = []
    for T, y in x["cands"].items():
        defs = sorted({n for n in y["apps"] if T in (by[n].get("dstr") or [])})
        uses = sorted({n for n in y["apps"] if T in (by[n].get("srcr") or [])} - set(defs))
        if len(defs) != 1 or not uses:
            continue
        d = by[defs[0]]
        if not _foldable_def(d) or not _can_recalc(d, T, x["cands"], by):
            continue
        kind = "lea" if d["op"] == "lea" else "load" if d["srck"][:1] == [6] else None
        if kind is None:
            continue
        tot = 0
        for n in uses:
            b = il_nofold_benefit(by[n], d, T, 0, can_mem)
            if b is None or b == INF:
                tot = INF
                break
            tot += b << 2 * zd(n)
        if tot != INF and (1 << 2 * zd(d["n"])) >= tot:
            srcs = [r for kk, r in zip(d["srck"], d.get("srcr") or []) if kk == 1 and r and r != T]
            out.append((T, d["n"], uses, srcs, kind))
    return sorted(out, key=lambda v: v[1])


FOLD_ADJACENT = False    # il_apply_folds: an lea fold also joins ranges that only touch (il_apply_addr_folds does)


def _merge_ranges(rg, adjacent):
    out = []
    for lo, hi in sorted([list(v) for v in rg]):
        if out and lo <= out[-1][1] + (1 if adjacent else 0):
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [tuple(v) for v in out]


def il_apply_folds(x, folds, order=None, ext=None, tups=None):
    """x (il_inputs) with the processing-time folds of il_proc_folds applied in processing order (docs/c2/ncolor.md
    12.5): the folds of T change only the lists of lifetimes processed after T (`order`: [root] in processing order;
    None: every source, as il_apply_load_folds / il_apply_addr_folds do); a source loses the definition, gains T's
    uses (a load's base / index: they replace the load) and T's ranges; a source already processed keeps its lists
    and must be extended to the uses instead (NColLoadCanBeRecalcd -> NColCanExtendAlreadyProcessed): ext(T, q, uses,
    lists) says whether it can, and a fold with a source that cannot is not made. A source that is itself a folded
    temporary stands for its own sources. With the tuples, x' also gets `addr`: {(tuple, leaf index): the address
    an lea fold left in that operand, as [(the folded T's position in the order, address)]} (_fold_address;
    il_anti_read's override). -> (x', {T: [the sources
    extended]}, the folds made)."""
    import copy
    tups_by = {t["n"]: t for t in tups} if tups is not None else None
    cands = copy.deepcopy(x["cands"])
    pos = {r: i for i, r in enumerate(order)} if order is not None else None
    later = lambda q, T: pos is None or pos.get(q, -1) > pos.get(T, -1)
    by_t = {f[0]: f for f in folds}
    seq = [r for r in order if r in by_t] if order is not None else [f[0] for f in sorted(folds, key=lambda v: v[1])]
    seq += [f[0] for f in folds if f[0] not in seq]
    to, made, extended = {}, [], {}
    addr = {}                    # (tuple, leaf index) -> the address a lea fold left there
    for T in seq:
        _T, d, uses, srcs, kind = by_t[T]
        b = cands.get(T)
        if b is None:
            continue
        qs = []
        for q in srcs:
            stack, seen = [q], set()
            while stack:
                v = stack.pop()
                if v in to and v not in seen:
                    seen.add(v)
                    stack.extend(to[v])
                elif v != T:
                    qs.append(v)
        qs = list(dict.fromkeys(qs))
        tu = [n for n in b["apps"] if n != d]
        before = [q for q in qs if q in cands and not later(q, T)]
        if ext is not None and not all(ext(T, q, tu, cands) for q in before):
            continue
        for q in qs:
            a = cands.get(q)
            if a is None or not later(q, T):
                continue
            apps = list(a["apps"])
            if d in apps:
                apps.remove(d)
            a["apps"] = sorted(apps + tu)
            a["ranges"] = _merge_ranges(list(a["ranges"]) + list(b["ranges"]), FOLD_ADJACENT and kind == "lea")
            a["first"], a["last"] = a["ranges"][0][0], a["ranges"][-1][1]
        to[T] = qs
        extended[T] = before
        made.append(by_t[T])
        if kind == "lea" and tups_by is not None:
            dt = tups_by[d]
            a = (addr.get((d, len(dt["dst"]))) or [(0, None)])[-1][1] or _mem(dt, len(dt["dst"]))
            for n in tu:
                u = tups_by.get(n)
                if u is None or a is None:
                    continue
                jm = next(((j, v[-1][1]) for (k, j), v in addr.items() if k == n and
                           T in (v[-1][1]["base"], v[-1][1]["index"])), None) or _use_mem(u, T, side=True)
                if jm and jm[1]:
                    na = _fold_address(jm[1], a, T)
                    if na is not None:
                        addr.setdefault((n, jm[0]), []).append((pos.get(T, -1) if pos else len(made), na))
    return dict(x, cands=cands, addr=addr), extended, made


FTC_ADJACENT = False    # il_apply_fold_too_cheap: the sources' ranges join T's when they only touch


def il_apply_fold_too_cheap(lives, smap, folds):
    """The lifetimes of il_subsume with NColFoldTooCheap's folds applied (0x10066169..0x100662c2 [C for the lists]):
    every source of the folded definition gets T's uses in its use and appearance lists (NColInsertInNumList, last
    extended), loses the definition, and its ranges grow over T's (NColUpdateLiveness: modelled as the union); T is
    zeroed. smap: {root: survivor} of il_subsume."""
    import copy
    out = copy.deepcopy(lives)
    for T, defs, srcs in folds:
        T = smap.get(T, T)
        b = out.get(T)
        if not b or not b["apps"]:
            continue
        tu = [n for n in b["apps"] if n not in defs]
        for q in dict.fromkeys(smap.get(r, r) for r in srcs):
            a = out.get(q)
            if a is None or q == T:
                continue
            a["apps"] = sorted([n for n in a["apps"] if n not in defs] + tu)
            a["uses"] = sorted([n for n in a.get("uses", []) if n not in defs] + tu)
            a["ranges"] = [list(v) for v in _merge_ranges(list(a["ranges"]) + list(b["ranges"]), FTC_ADJACENT)]
        out[T] = dict(ranges=[], apps=[], defs=[], uses=[])
    return out


def il_rbp_score(tups, unknown=None):
    """The frame-reference score NColCanon accumulates in TLS+0x1196c (0x10090040..0x10090147, 0x1009049b) [C]: for
    every instruction tuple, every source and destination leaf: a memory leaf (kind 5 / 6) whose +0x2c SYM is a
    temporary / variable (SYM kinds 3..6) scores 6; otherwise a leaf of opcode 0x4fc (a symbol leaf: `s7:1008`,
    `stream:1008`) or 0x500 ([index*scale]) scores 6 when its root SYM has the +9 bit 2, else 1. The +0x2c SYM's own
    kind is in the dump only when that SYM is also some leaf's symbol; an unknown one counts as 6 [I: zlib: only then
    does inflate_table reach the threshold, which its traced useable sets say it does]. unknown="local" (`rbplocal`):
    an unknown one scores 6 only on an OPLOCALSCALED leaf (0x500: an index into a local array, whose SYM is that stack
    local), else the leaf rules apply [I: zlib, against the score read at NColMdPerFuncInit (c2ncol's PERFUNC probe):
    97 / 97 functions; 91 with every unknown 6]."""
    syms = {}
    for t in tups:
        for x in t.get("leafsym") or []:
            if x and len(x) > 4:
                syms.setdefault(x[0], x)
    sc = 0
    for t in tups:
        if t["kind"] != "I":
            continue
        ls = t.get("leafsym") or []
        for i, w in enumerate(t["leafwords"]):
            if len(w) < 12:
                continue
            if (w[3] & 0xff) in (5, 6) and w[11]:
                x = syms.get(w[11])
                if x is None and (unknown != "local" or w[2] == 0x500) or \
                        x is not None and (x[3] & 0xff) in (3, 4, 5, 6):
                    sc += 6
                    continue
            if w[2] in (0x4fc, 0x500):
                x = ls[i] if i < len(ls) else None
                r = syms.get(x[4] or x[0], x) if x and len(x) > 4 else None
                sc += 6 if r is not None and (r[3] >> 8) & 2 else 1
    return sc


def il_rbp_reserved_read(tups, speed=True, score=None, unknown=None):
    """NColMdPerFuncInit 0x10092788..0x100927d6 [C]: rbp is reserved as a frame base (FUNC+0x58 = 6, out of the
    useable sets) when il_rbp_score reaches 6 * 24 (favoring speed; 6 * 12 otherwise), rbp is in the initial integer
    set and not yet used (P+9 & 0x10) and the frame registers checked before are the default ones (not modelled).
    score: the score as read (the export's `perfunc`, TLS+0x1196c at NColMdPerFuncInit's entry) instead of computed."""
    if score is None:
        score = il_rbp_score(tups, unknown)
    return score >= 6 * (24 if speed else 12)


def il_has_try(tups):
    """A try block (OPEHTRY in the IL): the frame keeps rbp (FUNC+0xe4 / +0xe8 flags of an EH frame: rbp leaves the
    useable sets) and NColMdAdjustPriority does not move a global (FUNC+0xe4 & 0x80) [I: tests/data/ncol c2.cpp's f2;
    a destructor-only unwind (op579 / op581) keeps both]."""
    return any(t["kind"] in "IK" and t["op"] == "OPEHTRY" for t in tups)


def il_uses_float(tups):
    """FUNC+0xe4 & 0x80, which stops NColMdAdjustPriority: set by OtlScanFuncLeaves (0x101df4e0) for a leaf of type
    class 5 (float / double; also by RdrFinalCleanup and PtInlProcessInstr, not read) [C for the scan]: any REG,
    memory or constant leaf typed 5xxx in the IL (tests/data/ncol c2.cpp's f2, which also has a try block, and the
    float loops of tests/data/ncol/xmm, fp.cpp)."""
    for t in tups:
        if t["kind"] != "I":
            continue
        for x in t["dst"] + t["src"]:
            ty = x.rsplit(":", 1)[1] if ":" in x else ""
            if len(ty) == 4 and ty[0] == "5":
                return True
    return False


IL_MODEL = ("tied", "prefs", "useable", "anti", "order", "live", "avail", "index")   # il_model_state's default
# the rules read since (docs/c2/ncolor.md 12.4): the xmm banks from the IL, each call's clobber set from c2ncol's REGSET
# probe (avail / xavail; without the probe the Win64 volatiles), the preference lists as read, NColMdAdjustPriority off
# in a function that uses floating point (FUNC+0xe4 & 0x80), the anti-preference set by the leaf a memory operand points
# to, the rbp reservation by NColCanon's score as read. The CLI's ilmodel / ilfull use them; the functions' defaults (and
# il_predict) keep IL_MODEL.
IL_READ = ("xavail", "regsets", "prefread", "fpflag", "antiread", "rbpread", "loadfold")
# read since (docs/c2/ncolor.md 12.5): the liveness of a partial definition, the tie of a copy reading the candidate
# anywhere, the float flag and the frame score from c2ncol's probes, the fold cost test in processing order
IL_FOLD = ("partlive", "defsplit", "tieread", "fpread", "rbpscore", "foldcost", "foldorder", "foldext", "foldaddr",
           "pwlist", "prefregion")
# the lifetimes from the IL before NColSubsumption instead of after it (12.5): il_subsume with the dead-first union and
# NColFoldTooCheap's folds; zlib's priorities and lists all exact with IL_FOLD, but fp.cpp's sim 306 / 315 (309 without)
IL_SUB = ("subsume", "deadfirst", "ftc")


def il_model_state(f, use=IL_USE + IL_MODEL):
    """il_state() plus the inputs of part 2 named in `use`: tied / prefwin (il_tied, or il_tied_pre), prefs
    (il_pref_sources: the record gets `prefs_src`, resolved in simulate), useable, anti, order (the record gets
    `lists`: simulate orders by order_of instead of the traced buckets); with the IL before NColSubsumption
    (`il_pre`, c2ncol --il) also live (il_live), avail (il_avail: the integer registers; xmm banks stay traced) and
    index (il_index). IL_READ's keys (docs/c2/ncolor.md 12.4): xavail (il_xavail: the xmm banks), regsets (the calls'
    REGSET sets of c2ncol's probe for avail / xavail), prefread (il_pref_sources_read), fpflag (no NColMdAdjustPriority
    in a function with floating point), antiread (il_anti_read), rbpread (il_rbp_reserved_read), loadfold / addrfold
    (il_load_folds / il_addr_folds: the lists as NColor holds them at processing; the record gets `prio_of`, the
    priorities before the folds). IL_FOLD's keys (12.5): partlive (a partial definition reads the value: il_inputs),
    tieread (il_tied_pre's second site as read), fpread / rbpscore (FUNC+0xe4 and the frame score as c2ncol's probes
    read them, else il_uses_float / il_rbp_score(unknown="local")), foldcost (il_proc_folds: the folds by
    NColMdNoFoldBenefit and the cost test instead of loadfold / addrfold's shapes), foldorder (a fold changes only
    the lists of lifetimes processed after the folded one: the model's order), foldext (a source processed before
    must be extendable to the uses in its register: the registers of the earlier candidates as traced)."""
    g = il_state(f, tuple(u for u in use if u in ("apps", "ranges", "live", "depth", "blk", "partlive", "defsplit")))
    tups = il_parse(f["il"])
    x = il_inputs(g)
    ty = il_types(tups)
    st = g["state"]
    pre = il_parse(f["il_pre"]) if f.get("il_pre") else None
    xpre = il_inputs(g, "il_pre") if pre is not None else None
    e4 = f.get("fflags") if f.get("fflags") is not None else (f.get("perfunc") or {}).get("e4")
    if "fpread" in use and e4 is not None:
        uses_fp = bool(e4 & 0x80)                  # FUNC+0xe4 & 0x80 as read (NColor's entry, NColMdPerFuncInit's)
    else:
        uses_fp = il_uses_float(pre if pre is not None else tups) or il_uses_float(tups)
    if "tied" in use:
        ftc = [T for T, _d, _s in il_fold_too_cheap(pre, xpre, 0 if uses_fp or not st.get("speed", 1) else 1)] \
            if pre is not None and "tieread" in use else ()
        td = il_tied(tups, x) if pre is None else \
            il_tied_pre(pre, xpre, x, il_premap(pre, tups), read="tieread" in use, folded=ftc)
        pw = {r: [] for r in (st.get("prefwin") or {})}
        for c in g["cands"]:
            v = td.get(c.get("sym"))
            c["tied"] = _master_num(v[0]) if v else None
            c["prefwin"] = v[1] if v else None
            if v and v[1] and c.get("apps"):
                pw.setdefault(_master_num(v[0]), []).append(v[1])
        st["prefwin"] = {r: sorted(w) for r, w in pw.items()}
        if "pwlist" in use:                         # the lists as linked (head walk), not every window
            ix = il_index(pre) if pre is not None else {}
            vis, hid = il_prefwin_lists([(ix.get(c.get("sym"), c.get("index", 0)), c["tied"], tuple(c["prefwin"]))
                                         for c in g["cands"] if c.get("tied") and c.get("prefwin") and c.get("apps")])
            st["prefwin"] = {r: vis.get(r, []) for r in sorted({int(q) for q in pw} | set(vis))}
            st["prefwin_hidden"] = hid
    if "useable" in use:
        src = pre if pre is not None else tups
        if "rbpscore" in use:
            sc = (f.get("perfunc") or {}).get("rbpscore")
            rbp = il_rbp_reserved_read(src, st.get("speed", 1), score=sc, unknown="local")
        else:
            rbp = (il_rbp_reserved_read if "rbpread" in use else il_rbp_reserved)(src, st.get("speed", 1))
        rbp = rbp or il_has_try(tups)
        for c in g["cands"]:
            t = ty.get(c.get("sym"))
            u = il_useable(c, t, frame_rbp=rbp)
            if u is not None and c.get("useable"):
                c["useable"] = [u]
    if pre is not None and "live" in use:          # exact from the stream NColBuildInfo walked
        lv = il_live(pre)
        st["live"] = lv + [0] * max(0, len(st["live"]) - len(lv))
        st["maxlive"] = max(lv or [0])
    rsets = il_regsets(f) if "regsets" in use else None
    if pre is not None and "avail" in use:
        av = il_avail(pre, st["ntup"], regsets=rsets) if rsets is not None else il_avail(pre, st["ntup"])
        st["avail"] = {int(k): v for k, v in st["avail"].items()}
        st["avail"].update(av)
    if pre is not None and "xavail" in use:        # the xmm banks (the calls' sets: the REGSET probe, else Win64)
        st["avail"] = {int(k): v for k, v in st["avail"].items()}
        st["avail"].update(il_xavail(pre, st["ntup"], rsets))
    if pre is not None and "index" in use:
        ix = il_index(pre)
        for c in g["cands"]:
            if c.get("sym") in ix:
                c["index"] = ix[c["sym"]]
    base = x                                       # the lifetimes at prioritization (the IL after the folds)
    if pre is not None and "subsume" in use:      # the lifetimes from the IL before NColSubsumption, coalesced by
        smap, lives = il_subsume(pre, xpre,         # the model (il_subsume) instead of read after it
                                 adjacent="dead-first" if "deadfirst" in use else None)
        if "ftc" in use:                           # and NColFoldTooCheap's folds applied to them (il_fold_too_cheap)
            lives = il_apply_fold_too_cheap(lives, smap, il_fold_too_cheap(
                pre, xpre, 0 if uses_fp or not st.get("speed", 1) else 1))
            base = dict(x, cands={r: dict(v, first=v["ranges"][0][0] if v["ranges"] else None,
                                          last=v["ranges"][-1][1] if v["ranges"] else None)
                                  for r, v in lives.items() if v["ranges"]})
        for c in g["cands"]:
            v = lives.get(c.get("sym"))
            if v and v["apps"] and v["ranges"]:
                c["apps"], c["ranges"] = list(v["apps"]), [tuple(q) for q in v["ranges"]]
                c["first"], c["last"] = v["ranges"][0][0], v["ranges"][-1][1]
    no_adjust = uses_fp if "fpflag" in use or "fpread" in use else il_has_try(tups)
    xf = base                                   # the lists as NColor holds them when the candidate is processed
    folding = "foldcost" in use or "addrfold" in use or "loadfold" in use
    if folding:
        g["prio_of"] = {c["name"]: prio(c, st) for c in g["cands"]}    # the priorities are those before the folds
    if "foldcost" in use:
        order = None
        if "foldorder" in use or "foldext" in use:
            sp = lambda p: p - (1 << 32) if p & 0x80000000 else p
            pr = []
            for c in g["cands"]:                   # the priorities of the IL's lists before the folds (also when
                y = _ilcand(x, c)                  # the record's lists are the traced ones, read after them)
                cc = dict(c, apps=y["apps"], ranges=[tuple(q) for q in y["ranges"]], first=y["first"],
                          last=y["last"]) if y is not None and y["ranges"] and "subsume" not in use else c
                p = prio(cc, st) if cc.get("ranges") else None
                pr.append(p if p is not None else sp(c["prio"] or 0))
            o = order_of(dict(g, no_adjust=no_adjust), pr, il_lists(g, tups))
            order = [g["cands"][i].get("sym") for i in o]
        g["_order"] = order
        xf, _ext, _made = il_apply_folds(base, il_proc_folds(tups, x), order if "foldorder" in use else None,
                                         _fold_ext(g, x, order) if "foldext" in use else None,
                                         tups if "foldaddr" in use else None)
    else:
        if "addrfold" in use:
            xf, _to = il_apply_addr_folds(x, il_addr_folds(tups, x))
        if "loadfold" in use:
            xf = il_apply_load_folds(xf, il_load_folds(tups, x))
    if "prefs" in use:
        g["prefs_src"] = il_pref_sources(tups, xf, read="prefread" in use, folded_uses="foldcost" in use)
        if "prefregion" in use:           # cmd_ilmodel: a partner's register is its region's covering the tuple
            g["prefs_by_region"] = True
    if "anti" in use and "antiread" in use:      # as read: by the leaf the memory operand points to
        od = g.pop("_order", None)
        an = il_anti_read(tups, {r: set(y["apps"]) for r, y in xf["cands"].items()}, xf.get("addr"),
                          {r: i for i, r in enumerate(od)} if od and "foldorder" in use else None)
        for c in g["cands"]:
            k = an.get(c.get("sym"))
            c["anti_r"] = [st["anti"][k] if k is not None and st.get("anti") else 0]
    elif "anti" in use:
        an = il_anti(tups)
        for c in g["cands"]:
            k = an.get(c["name"])
            c["anti_r"] = [st["anti"][k] if k is not None and st.get("anti") else 0]
    if folding:                                    # the lists of the lifetimes the folds touch change
        for c in g["cands"]:
            y = xf["cands"].get(c.get("sym"))
            if y is not None and y["ranges"] and c.get("ranges") and (base is not x or
                                                                      y["ranges"] != x["cands"][c["sym"]]["ranges"]):
                c["ranges"] = [tuple(q) for q in y["ranges"]]
                c["apps"] = list(y["apps"])
    if "order" in use:
        g["lists"] = il_lists(g, tups)
        g["no_adjust"] = no_adjust
    return g


def _fold_ext(g, x, order):
    """foldext's test (NColCanExtendAlreadyProcessed 0x100c8a70 [C], with the registers of the earlier candidates
    as traced): a source q processed before T whose ranges do not reach a use u of T needs, from the end of its range
    before u (NColRegionPrior) to u: a register at that end (the traced region's), the same loop depth at both ends
    (the blocks' +0xac [I]) and that register free over the whole window (NColFindFirstOverlappingAvailRegion) in the
    availability left by the candidates processed before T (st's initial lists minus their traced regions over their
    ranges, as replay cuts them)."""
    st = g["state"]
    by = {c.get("sym"): c for c in g["cands"]}
    pos = {r: i for i, r in enumerate(order or [])}
    cache = {}

    def avail_before(T):
        k = pos.get(T)
        if k in cache:
            return cache[k]
        av = avail_of(st)
        for r in (order or [])[:k]:
            c = by.get(r)
            if not c or not c.get("ranges"):
                continue
            av.bank = xbank(c)
            for rg in c["regions"]:
                q = ALIAS.get(rg["reg"])
                if q in NUM and NUM[q] in av:
                    rr = [(lo, hi) for lo, hi in c["ranges"] if lo <= rg["last"] and rg["first"] <= hi] or \
                         [(rg["first"], rg["last"])]
                    av[NUM[q]] = cut(av[NUM[q]], rr)
        cache[k] = av
        return av

    def ok(T, q, uses, cands):
        c = by.get(q)
        if c is None or not c.get("regions") or q not in cands:
            return True
        qr = cands[q]["ranges"]
        for u in uses:
            if any(lo <= u <= hi for lo, hi in qr):
                continue
            prior = max([hi for _lo, hi in qr if hi < u] or [0])
            if not prior:
                return False
            r = next((ALIAS.get(rg["reg"]) for rg in c["regions"] if rg["first"] <= prior <= rg["last"]), None)
            if r not in NUM or x["loopdepth"][x["blk"][prior]] != x["loopdepth"][x["blk"][u]]:
                return False
            av = avail_before(T)
            av.bank = xbank(c)
            if NUM[r] not in av or not any(b <= prior and u <= e for b, e in av[NUM[r]]):
                return False
        return True
    return ok


def cmd_ilmodel(fns, show=10, extra=()):
    """Part 2's inputs from the IL against the traced ones: per candidate the tied register and window, the preference
    list (partners' registers as traced), the useable set, the anti-preference set; per function the order (lists,
    priorities and indices as traced) and the lists. -> Counter."""
    import collections
    c = collections.Counter()
    bad = collections.defaultdict(list)
    sp = lambda p: p - (1 << 32) if p & 0x80000000 else p
    for f in fns:
        if not f.get("il") or not f.get("state"):
            continue
        g = il_model_state(f, ("tied", "prefs", "useable", "anti", "order") + tuple(extra))
        pof = il_prefs_of(g, g["prefs_src"])
        given = {}
        c["functions"] += 1
        o = order_of(dict(f, no_adjust=g.get("no_adjust")), [sp(x["prio"]) for x in f["cands"]], g["lists"])
        c["order same"] += o == list(range(len(f["cands"])))
        if o != list(range(len(f["cands"]))):
            bad["order"].append(f["func"])
        for c0, c1 in zip(f["cands"], g["cands"]):
            if not c0.get("ranges"):
                continue
            c["cands"] += 1
            t0 = regname(c0["tied"]) if c0.get("tied") else None
            t1 = regname(c1["tied"]) if c1.get("tied") else None
            c["tied same"] += t0 == t1
            w0 = tuple(c0["prefwin"]) if c0.get("prefwin") else None
            w1 = tuple(c1["prefwin"]) if c1.get("prefwin") else None
            c["window same"] += (t0, w0) == (t1, w1)
            if (t0, w0) != (t1, w1):
                bad["tied"].append((f["func"], c0["name"], t0, w0, t1, w1))
            p0 = [regname(n) for n, _t in (c0.get("prefs") or [[]])[0]]
            p1 = pof(c1, given)
            c["prefs same"] += p0 == p1
            if p0 != p1:
                bad["prefs"].append((f["func"], c0["name"], p0, p1))
            if c0["regions"]:
                given[c0["name"]] = [(r["first"], r["last"], ALIAS.get(r["reg"])) for r in c0["regions"]] \
                    if g.get("prefs_by_region") else ALIAS.get(c0["regions"][0]["reg"])
            if c0.get("useable"):
                c["useable n"] += 1
                c["useable same"] += useable(c0) == useable(c1)
                if useable(c0) != useable(c1):
                    bad["useable"].append((f["func"], c0["name"], sorted((useable(c0) or set()) ^ (useable(c1) or set()))))
            a0, a1 = avoid(c0, f["state"]), avoid(c1, g["state"])
            c["anti same"] += a0 == a1
            if a0 != a1:
                bad["anti"].append((f["func"], c0["name"], a0, a1))
    for k in ("functions", "order same", "cands", "tied same", "window same", "prefs same", "useable n", "useable same",
              "anti same"):
        print("ilmodel: %-13s %d" % (k, c[k]))
    for k, v in bad.items():
        print("  %s: %d" % (k, len(v)))
        for b in v[:show]:
            print("    ", b)
    return c


def il_predict(f, use=None):
    """{short name: register} the model gives from the function's IL alone (il_model_state + simulate): for a compile
    of an edited source whose block structure changed (eqif, duplicated tails: docs/c2/ncolor.md 12), NColor's inputs
    are recomputed from that compile's IL instead of being edited on the base run. Default: the read rules
    (IL_READ + IL_FOLD, ncolor.md 12.4, 12.5); `use=IL_USE + IL_MODEL` gives the earlier fitted model,
    `IL_USE + IL_MODEL + IL_READ` the rules of 12.4 alone."""
    g = il_model_state(f, use or IL_USE + IL_MODEL + IL_READ + IL_FOLD)
    got = simulate(g, prio_of=g.get("prio_of"), lists=g.get("lists"),
                   prefs_of=il_prefs_of(g, g["prefs_src"]) if g.get("prefs_src") else None)
    return {short(k): v for k, v in got.items()}


def cmd_ilfull(fns, show=10, use=None):
    """replay and sim with every input from the IL (il_model_state: IL_USE + IL_MODEL; exports of c2ncol --il carry
    the IL before NColSubsumption that live, avail and index need; xmm availability stays traced)."""
    use = use or IL_USE + IL_MODEL
    gs = [il_model_state(f, use) if f.get("il") and f.get("state") else f for f in fns]
    print("on the IL's inputs (%s):" % "+".join(use))
    cmd_replay(gs, show)
    cmd_sim(gs, show)
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
    return {"prio": cmd_prio, "replay": lambda f: cmd_replay(f, show), "sim": lambda f: cmd_sim(f, show),
            "ilin": lambda f: cmd_ilin(f, show) and 0, "ilprio": lambda f: cmd_ilprio(f) and 0,
            "ilprio+live": lambda f: cmd_ilprio(f, IL_USE + ("live",)) and 0,
            "ilmodel": lambda f: cmd_ilmodel(f, show, IL_READ + IL_FOLD) and 0,
            "ilfull": lambda f: cmd_ilfull(f, show, IL_USE + IL_MODEL + IL_READ + IL_FOLD),
            "ilmodel-read": lambda f: cmd_ilmodel(f, show, IL_READ) and 0,
            "ilfull-read": lambda f: cmd_ilfull(f, show, IL_USE + IL_MODEL + IL_READ),
            "ilfull-sub": lambda f: cmd_ilfull(f, show, IL_USE + IL_MODEL + IL_READ + IL_FOLD + IL_SUB),
            "ilmodel-fit": lambda f: cmd_ilmodel(f, show) and 0, "ilfull-fit": lambda f: cmd_ilfull(f, show),
            "ilsim": lambda f: cmd_ilsim(f, show)}[argv[0]](fns)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
