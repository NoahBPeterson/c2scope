#!/usr/bin/env python3
"""Stack-slot packing: what c2's packer did, a replay model, what-if edits, and the original's slot map.
Doc: docs/c2/slots.md

usage: c2trace.py slots <src> --func F [--export SNAP.json] [--keep DIR] [--all] [--tried N]
                        [--what EDIT ...] [--orig ORIG.s | --image EXE --va VA] [--search] [--top K] [-- <cl args>]
       c2slots.py what SNAP.json EDIT [EDIT ...]          what-if on an exported snapshot (no compile)
       c2slots.py search SNAP.json (--orig ORIG.s | --image EXE --va VA) [--top K] [--limit N]
       c2slots.py orig (--orig ORIG.s | --image EXE --va VA)   the original's frame slots alone (push depth removed)

The traced compile hooks ColorStackElements, colorElemental and the offset loop (hooks key `slots` in the build
table; 14.00 below). Per colour (slot) the report gives its creation number, sorted position, offset, size, summed
uses, key = 1000*uses/size and the members (listing name, first source line). Per element, in the order the packer
visits them (size ascending, uses descending, first to reach the count), it gives size, uses as the packer counted
them next to the listing's reference count, every colour tried with the reason it was refused (`size` = more than
twice the colour's size; `conf X` = conflicts with member X; `gs` = a /GS rule kept it out of a parameter home) and
where it ended. The model (Model below) then replays the colouring from the recorded elements and conflict sets
(LiveOnDef) and must reproduce the trace exactly; `model:` says whether it did.

EDIT (what-if; NAME = a listing name such as _i$123 / $T456 / tv89, an element name stem, or #IDX):
  uses:NAME=N[^]   N uses; the element moves to the end of its new (size, uses) run (with ^: the front)
  size:NAME=N      N bytes (re-sorted the same way)
  pos:NAME=P       visit it at position P of the visiting order
  conf+:A,B        add a conflict between A and B        conf-:A,B   remove it (both directions)
  add:NAME,SIZE,USES[,POS][,conf=A;B;...]   a new element (conflicting with A, B, ...)
Each prints the new offset of every element that moved.

--orig ORIG.s ('VA mnemonic operands' lines) or --image EXE --va VA: the original function's frame operands, with
push depth removed (esp operands) or from ebp, are aligned with our listing (regmap's alignment) and give each of
our elements the original's offset (side by side). --search then tries single edits (uses +-1..3, visiting position
within the size run, removing a conflict that refused a join, adding one that splits a colour) and ranks them by how
many elements end at the original's offset.
"""
import argparse, bisect, collections, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROLES = ["start", "pnew", "pnew_lod", "ptry", "pjoin", "pjoin_lod", "elem", "ctry", "cjoin", "cjoin_lod",
         "cnew_lod", "gs_join", "gs_new", "done", "redo", "sorted", "col", "mem"]
# {SYM:x}: sym, index, size, uses, kind|fl5|fl6|fl7, key, name, alignCode, ty, SSR+0x28 ($T number)     {LOD:x}: 12 nodes of the SBV at x
SYM9 = "{x},{x}>0x3c,{x}>0x20,{x}>0x38,{x}>4,{x}>0x1c,{x}>0>4$,{x}>0x2c,{x}>0x10,{x}>0>0x28"
# 14.00.50727.762 (addresses: docs/c2/slots.md section 2; frame.md section 4)
HOOKS = [
    "@ColorStackElements@12=g10d123ac>0>0>4$,s1,g10d0faac>0,g10d0faa8>0,s2",
    "10beaf5a={SYM:eax},eax>0>0x1c,eax>0>0x18",       # param colour: SYM in eax
    "10beb009={LOD:eax}",                              # its LiveOnDef (SbvCopy source)
    "10beb143=ebx,edi,esi>4",                          # param colour tried: colour, element index, colour size
    "10beb1d8=ebx,{SYM:ebp}",                          # local joins a param home
    "10beb222={LOD:eax}",
    "@colorElemental@12={SYM:s1},g10d0fa98>0,g10d0faa8>0",
    "10beb791=ebp,edi>4",                              # colour tried (newest first): colour, its size
    "10beb81b=ebp",                                    # joined
    "10beb865={LOD:eax}",
    "10beb992={LOD:eax}",                              # new colour
    "@StkGSBufferLevel@4/xa0",                         # GS level of a joining buffer (exit eax)
    "10bebb02=eax",                                    # GS level of a new colour (al)
    "10beb25e=s5,s6,s7,s9",                            # colouring done: T, param colours offered, bEbp, param-home joins
    "10beb2d7=s5",                                     # undo: T >= 112 with a param home in an ESP frame
    "10beb36e=g10d0fa98>0,s5,s7,g10d0faa8>0,s16>0x5c,s16>0xac,g10d0fa94>0,g10d195cc>0>0x44c,g10d0d728>0,s16>0x48",
    "10beb584=s5,s8,eax",                              # offset loop: colour, base, members
    "10beb618=esi,esi>0x3c,ecx",                       # member: SYM, index, offset
]
SBV_14 = {"bits": 64, "next": 0xc, "words": 4}       # node: base, bits lo, bits hi, next
BUILD = {"14.00": dict(sbv=SBV_14, gs3=True, cap_note="FUNC+0x48")}


def build_info():
    import c2build
    t = c2build.table()
    if t is None:
        return dict(name="14.00", hooks=HOOKS, sbv=SBV_14, v13=False)
    s = t.get("slots")
    if not s or "slots" not in t.get("hooks", {}):
        sys.exit("c2 %s: the slots probes are not ported (no hooks.slots / slots in its build table)" % c2build.name())
    return dict(name=c2build.name(), hooks=t["hooks"]["slots"], sbv=s["sbv"], v13=s.get("v13", False))


def expand(h, sbv):
    def lod(x):
        nx = ">0x%x" % sbv["next"]
        return ",".join("%s>0%s#%d" % (x, nx * k, sbv["words"]) for k in range(12))
    h = re.sub(r"\{LOD:([^}]+)\}", lambda m: lod(m.group(1)), h)
    return re.sub(r"\{SYM:([^}]+)\}", lambda m: SYM9.replace("{x}", m.group(1)), h)


# ---------------------------------------------------------------- trace parsing
def _val(v):
    if v == "?" or v is None:
        return None
    if v.startswith('"'):
        return v.strip('"')
    if v.startswith("["):
        return [None if x == "?" else int(x, 16) for x in v[1:-1].split()]
    return int(v, 16)


def s32(x):
    return None if x is None else (x - (1 << 32) if x >= 1 << 31 else x)


def lod_bits(nodes, sbv):
    """(set of element indices, False or the last element index covered when the set has more than 12 nodes)."""
    out, trunc, last = set(), False, -1
    for k, n in enumerate(nodes):
        if not isinstance(n, list) or n[0] is None:
            break
        if sbv["bits"] == 64:
            base, lo, hi, nxt = n
            bits = (lo or 0) | (hi or 0) << 32
        else:
            base, nxt, bits = n
            bits = bits or 0
        i = 0
        while bits:
            if bits & 1:
                out.add(base + i)
            bits >>= 1
            i += 1
        last = base + sbv["bits"] - 1
        if k == 11 and nxt:
            trunc = True
        if not nxt:
            break
    return out, (last if trunc else False)


def _sym(v, k=0):
    kf = v[k + 4] or 0
    if not v[k + 6] and (kf & 0xff) == 4 and isinstance(v[k + 9], int) and v[k] is not None:
        v = list(v)
        v[k + 6] = "$T%d" % v[k + 9]                   # a front-end temporary: the listing calls it $T<SSR+0x28>
    return dict(sym=v[k], idx=v[k + 1], size=s32(v[k + 2]), uses=v[k + 3], kind=kf & 0xff, fl5=kf >> 8 & 0xff,
                fl6=kf >> 16 & 0xff, fl7=kf >> 24 & 0xff, key=v[k + 5], name=v[k + 6] if isinstance(v[k + 6], str) else None,
                align=(v[k + 7] or 0) & 0xff, ty=(v[k + 8] or 0) & 0xffff, tnum=v[k + 9])


def parse(log, info):
    """Per function: elements (by index), the final colouring run, the offsets the packer gave."""
    import c2trace
    hooks = [expand(h, info["sbv"]) for h in info["hooks"]]
    funcs, F, cur, col = [], None, None, None
    for ln in log.splitlines():
        if "|" not in ln or ln[:2] not in ("E ", "X "):
            continue
        head, _, rest = ln.partition("|")
        f = head.split()
        role = ROLES[int(f[3])]
        v = [_val(x) for x in c2trace._split(rest)]
        regs = [int(x, 16) for x in f[4:]]
        if role == "start":
            F = dict(name=v[0], nstack=v[2], nparams=v[3], elems={}, params=[], runs=[[]], cols=[], pend={},
                     done=None, redo=None, sorted=None)
            funcs.append(F)
            cur = col = None
            continue
        if F is None:
            continue
        run = F["runs"][-1]
        if role == "pnew":
            e = _sym(v)
            e.update(ssize=s32(v[10]), poff=s32(v[11]), param=True)
            F["elems"][e["idx"]] = e
            F["params"].append(e["idx"])
            cur = dict(idx=e["idx"])
        elif role in ("pnew_lod", "pjoin_lod", "cjoin_lod", "cnew_lod"):
            if cur is not None:
                bits, tr = lod_bits(v, info["sbv"])
                ce = F["elems"][cur["idx"]]
                ce["lod"], ce["lodtrunc"] = sorted(bits), tr
                if role == "cnew_lod":
                    cur["res"] = ("c", cur["ncol"])
        elif role == "ptry":
            F["pend"].setdefault(v[1], []).append(("p", v[0], s32(v[2])))
        elif role == "pjoin":
            e = _sym(v, 1)
            e["param"] = False
            old = F["elems"].get(e["idx"], {})
            old.update(e)
            F["elems"][e["idx"]] = old
            r = dict(idx=e["idx"], tries=F["pend"].pop(e["idx"], []), res=("p", v[0]), gs=0)
            run.append(r)
            cur = r
        elif role == "elem":
            e = _sym(v)
            e["param"] = False
            old = F["elems"].get(e["idx"], {})
            old.update(e)
            F["elems"][e["idx"]] = old
            r = dict(idx=e["idx"], tries=F["pend"].pop(e["idx"], []), res=None, ncol=v[10], gs=0)
            run.append(r)
            cur = r
        elif role == "ctry":
            if cur is not None:
                cur["tries"].append(("c", v[0], s32(v[1])))
        elif role == "cjoin":
            if cur is not None:
                cur["res"] = ("c", v[0])
        elif role in ("gs_join", "gs_new"):
            if cur is not None and "tries" in cur and (role == "gs_new" or f[0] == "X"):
                lvl = (regs[0] if f[0] == "X" else v[0]) & 0xff
                cur["gs"] = lvl if not info["v13"] else 1
        elif role == "done":
            F["done"] = dict(T=v[0], npc=v[1], ebp=v[2], joins=v[3])
            cur = None
        elif role == "redo":
            F["redo"] = dict(T=v[0])
            F["runs"].append([])
            cur = None
        elif role == "sorted":
            if info["v13"]:
                n, T, ebp, npar, pad, fflags, adj, gsopt, alloc, cap, dalign = v[:11]
                F["sorted"] = dict(ncol=n, T=T, ebp=ebp, nparams=npar, arraypad=pad, fflags=fflags, ebpadj=s32(adj),
                                   gs=bool((gsopt or 0) & 1 and pad), gscopies=False, alloc=s32(alloc), cap=cap,
                                   dalign=dalign)
            else:
                n, T, ebp, npar, pad, fflags, adj, gsopt, alloc, cap = v[:10]
                F["sorted"] = dict(ncol=n, T=T, ebp=ebp, nparams=npar, arraypad=pad, fflags=fflags, ebpadj=s32(adj),
                                   gs=bool((gsopt or 0) & 0x20000 and pad), gscopies=bool((fflags or 0) & 1),
                                   alloc=s32(alloc), cap=cap, dalign=4)
            cur = None
        elif role == "col":
            col = dict(c=v[0], base=s32(v[1]), members=[])
            F["cols"].append(col)
        elif role == "mem":
            if col is not None:
                col["members"].append((v[1], s32(v[2])))
    return funcs


# ---------------------------------------------------------------- the model
def align4(n):
    return (n + 3) & ~3


def roundup(v, a):
    return (v + a - 1) & ~(a - 1) if a > 0 else v


class Model:
    """The packer of ColorStackElements / colorElemental / QSortColors / the offset loop, for one function.
    elems: {idx: dict(size, uses, param, never, align, gs, ty, fl7, ssize, poff)}; lod: {idx: set};
    fn: dict(npc, ebp, nparams, gs, gscopies, alloc, cap, dalign, ebpadj, v13)."""

    def __init__(self, elems, lod, fn, nall):
        self.e, self.fn, self.n = elems, fn, nall
        allm = (1 << nall) - 1
        self.lod = {}
        for i, x in elems.items():
            m = 0
            for b in lod.get(i, ()):
                m |= 1 << b
            self.lod[i] = allm | m if x.get("never") else m

    def conflict_member(self, E, members):
        """A member of `members` that conflicts with E (E in LOD[m] or m in LOD[E]), or None."""
        le = self.lod[E]
        for m in members:
            if le >> m & 1 or self.lod[m] >> E & 1:
                return m
        return None

    def amin(self, code, cap):
        a = (1 << (code - 1)) if code >= 1 else 0
        c = cap or 4
        return a if a < c else c

    def run(self, order, record=False):
        """order: element indices in visiting order (params included; they are coloured first, in that order)."""
        fn, e = self.fn, self.e
        params = [i for i in order if e[i]["param"]]
        locs = [i for i in order if not e[i]["param"]]
        log = {} if record else None

        def fresh(nparam_colours):
            cols = []
            for p in params[:nparam_colours]:
                cols.append(dict(members=[p], mask=1 << p, interf=self.lod[p], size=align4(e[p].get("ssize") or e[p]["size"]),
                                 uses=e[p]["uses"], poff=e[p].get("poff"), align=0, gs=0, created=len(cols), param=True))
            return cols

        npar = len(params)
        cols = fresh(npar)
        npc = fn["npc"] if fn.get("npc") is not None else npar
        T, joins = 0, 0
        gsfunc = fn.get("gs") or fn.get("gscopies")
        for E in locs:
            x = e[E]
            tries, done = [], False
            for c in range(npc):
                C = cols[c]
                if x["size"] > C["size"]:
                    tries.append((c, "size"))
                    continue
                if fn.get("v13"):
                    if fn.get("gs") and (x.get("fl7", 0) & 0x20 or (x["size"] > 4 and x.get("ty") != 0x4008)):
                        tries.append((c, "gs"))
                        continue
                elif gsfunc:
                    tries.append((c, "gs"))
                    continue
                m = self._cf(E, C)
                if m is not None:
                    tries.append((c, ("conf", m)))
                    continue
                C["members"].append(E)
                C["mask"] |= 1 << E
                C["interf"] |= self.lod[E]
                C["uses"] += x["uses"]
                joins += 1
                done = True
                if record:
                    log[E] = dict(tries=tries, res=c, new=False)
                break
            if not done:
                T = self._elemental(E, cols, npar, T, tries, log)
        undo = False
        if not fn.get("ebp") and joins > 0 and T >= 0x70:
            undo = True
            cols, npar, T = [], 0, 0
            if record:
                log.clear()
            for E in locs:
                T = self._elemental(E, cols, 0, T, [], log)
        nfixed = npar
        created = list(cols)
        sort = T > 0x80 or bool(fn.get("gs")) or bool(fn.get("gscopies") and not fn.get("v13"))
        if sort:
            self._qsort(cols, nfixed, len(cols) - 1, bool(fn.get("ebp")))
        offs, bases = {}, []
        alloc = fn.get("alloc") or 0
        idxs = range(len(cols)) if fn.get("ebp") else range(len(cols) - 1, -1, -1)
        bases = [None] * len(cols)
        for c in idxs:
            C = cols[c]
            if c < nfixed:
                base = C["poff"]
            else:
                alloc = self._allotemp(alloc, C["size"], C["align"])
                base = alloc
            if fn.get("ebp") and fn.get("v13") and c >= nfixed:
                base_shown = base + (fn.get("ebpadj") or 0)
            else:
                base_shown = base
            bases[c] = base_shown
            for m in C["members"]:
                offs[m] = self._member_off(C, c, nfixed, base, e[m])
        for p in params:                    # after the undo, params have no colour and keep their homes
            if p not in offs and e[p].get("poff") is not None:
                offs[p] = e[p]["poff"]
        return dict(cols=cols, created=created, T=T, undo=undo, sort=sort, bases=bases, offs=offs, log=log,
                    nfixed=nfixed, joins=joins)

    def _cf(self, E, C):
        if C["interf"] >> E & 1 or self.lod[E] & C["mask"]:
            m = self.conflict_member(E, C["members"])
            return m if m is not None else C["members"][0]
        return None

    def _elemental(self, E, cols, npar, T, tries, log):
        x, fn = self.e[E], self.fn
        for c in range(len(cols) - 1, npar - 1, -1):
            C = cols[c]
            if x["size"] > 2 * C["size"]:
                tries.append((c, "size"))
                continue
            m = self._cf(E, C)
            if m is not None:
                tries.append((c, ("conf", m)))
                continue
            C["members"].append(E)
            C["mask"] |= 1 << E
            C["interf"] |= self.lod[E]
            C["uses"] += x["uses"]
            if x["size"] > C["size"]:
                T += align4(x["size"]) - C["size"]
                C["size"] = x["size"]
            if x.get("gs"):
                C["gs"] = x["gs"]
            C["align"] = max(C["align"], self.amin(x.get("align", 3), fn.get("cap")))
            if log is not None:
                log[E] = dict(tries=tries, res=c, new=False)
            return T
        cols.append(dict(members=[E], mask=1 << E, interf=self.lod[E], size=x["size"], uses=x["uses"], poff=None,
                         align=self.amin(x.get("align", 3), fn.get("cap")), gs=x.get("gs", 0), created=len(cols),
                         param=False))
        if log is not None:
            log[E] = dict(tries=tries, res=len(cols) - 1, new=True)
        return T + align4(x["size"])

    def _cmp(self, p, x, d):
        if p["gs"] != x["gs"]:
            return (1 if d else -1) if p["gs"] > x["gs"] else (-1 if d else 1)
        kp, kx = 1000 * p["uses"] // p["size"], 1000 * x["uses"] // x["size"]
        return 0 if kp == kx else (1 if kp > kx else -1)

    def _qsort(self, a, lo, hi, d):
        while lo < hi:
            mid = (lo + hi) // 2
            a[lo], a[mid] = a[mid], a[lo]
            last = lo
            for i in range(lo + 1, hi + 1):
                if self._cmp(a[lo], a[i], d) < 0:
                    last += 1
                    a[last], a[i] = a[i], a[last]
            a[lo], a[last] = a[last], a[lo]
            self._qsort(a, lo, last - 1, d)
            lo = last + 1

    def _allotemp(self, alloc, size, align):
        fn = self.fn
        cap = fn.get("cap") or 4
        if align:
            align = min(align, cap)
            size = roundup(size, align)
        if fn.get("v13"):
            d = align if align else (fn.get("dalign") or 4)
            v = size - alloc
            if size == 2:
                v = roundup(v, d) if d <= 1 else (v + 1) & ~1
            elif size != 1:
                v = roundup(v, d)
            return -v
        d = align if align else 4
        return -roundup(size - alloc, d)

    def _member_off(self, C, c, nfixed, base, m):
        fn = self.fn
        if not fn.get("ebp"):
            return base
        a0 = (1 << (m.get("align", 3) - 1)) if m.get("align", 3) >= 1 else 0
        if c < nfixed:
            a = a0 if a0 < 4 else 4
        else:
            cap = fn.get("cap") or 4
            a = a0 if a0 < cap else cap
        t = m["size"] - C["size"] - base
        if fn.get("v13"):
            if m["size"] == 2:
                t = roundup(t, a) if a <= 1 else (t + 1) & ~1
            elif m["size"] != 1:
                t = roundup(t, a)
            return (fn.get("ebpadj") or 0) - t
        return -roundup(t, a)


# ---------------------------------------------------------------- snapshot
def snapshot(F, info, lst=None):
    """The JSON snapshot of one function: everything the model needs plus the observed result."""
    run = F["runs"][-1]
    elems = {}
    for i, x in F["elems"].items():
        y = {k: x.get(k) for k in ("idx", "size", "uses", "param", "kind", "fl6", "fl7", "key", "name", "align", "ty",
                                   "ssize", "poff", "lod", "lodtrunc", "tnum")}
        y["never"] = bool((x.get("fl6") or 0) & 4)
        y["gs"] = 0
        elems[i] = y
    for r in run:
        if r["idx"] in elems:
            elems[r["idx"]]["gs"] = r.get("gs", 0)
    S = F["sorted"] or {}
    infer(F, elems, S, info)
    fn = dict(npc=(F["done"] or {}).get("npc"), ebp=S.get("ebp"), nparams=S.get("nparams"), gs=S.get("gs"),
              gscopies=S.get("gscopies"), alloc=S.get("alloc"), cap=S.get("cap"), dalign=S.get("dalign"),
              ebpadj=S.get("ebpadj"), v13=info["v13"], T=S.get("T"), ncol=S.get("ncol"))
    obs = dict(run=[dict(idx=r["idx"], tries=r["tries"], res=r["res"], ncol=r.get("ncol")) for r in run],
               undo=F["redo"] is not None, cols=[dict(c=c["c"], base=c["base"], members=c["members"]) for c in F["cols"]],
               done=F["done"])
    return dict(build=info["name"], func=F["name"], nstack=F["nstack"], params=F["params"], elems=elems, fn=fn,
                obs=obs, listing=lst or {})


def infer(F, elems, S, info):
    """LiveOnDef sets cut at 12 nodes lose bits, never add any. Where the trace refused a colour for a conflict that
    the recorded sets do not show, the conflict is with a member whose set (or the element's) was cut: those pairs
    are added to `lod` (and listed in `inferred`), except for elements that end in one colour (they are compatible)."""
    if not any(x.get("lodtrunc") is not False and x.get("lodtrunc") is not None for x in elems.values()):
        return
    allb = lambda x: x.get("never")
    L = {i: set(x.get("lod") or ()) for i, x in elems.items()}

    def unknown(x, b):
        t = elems[x].get("lodtrunc")
        return t is not None and t is not False and b > t

    def conf(E, m):
        return allb(elems[E]) or allb(elems[m]) or m in L[E] or E in L[m]
    gsfunc = bool(S.get("gs") or (S.get("gscopies") and not info["v13"]))
    for k, run in enumerate(F["runs"]):
        if not run:
            continue
        members, final = {}, {}
        if k == 0:
            for c, p in enumerate(F["params"]):
                members[c] = [p]
        for r in run:
            if r["res"]:
                final[r["idx"]] = r["res"][1]
        for c, p in enumerate(F["params"] if k == 0 else []):
            final[p] = c
        for r in run:
            E = r["idx"]
            x = elems.get(E)
            if x is None:
                continue
            for kind, c, csz in r["tries"]:
                if r["res"] and (kind == r["res"][0] or kind == "c") and c == r["res"][1]:
                    continue
                if csz is not None and x["size"] > (csz if kind == "p" else 2 * csz):
                    continue
                if kind == "p" and gsfunc and not info["v13"]:
                    continue
                M = members.get(c, [])
                if any(conf(E, m) for m in M):
                    continue
                cand = [m for m in M if (unknown(E, m) or unknown(m, E)) and final.get(m) != final.get(E)]
                for m in cand:
                    L[E].add(m)
                    L[m].add(E)
                    elems[E].setdefault("inferred", []).append(m)
                    elems[m].setdefault("inferred", []).append(E)
            if r["res"]:
                members.setdefault(r["res"][1], []).append(E)
    for i, x in elems.items():
        if x.get("inferred"):
            x["lod"] = sorted(L[i])


def load(path):
    s = json.load(open(path))
    s["elems"] = {int(k): v for k, v in s["elems"].items()}
    s["listing"] = {int(k): v for k, v in s.get("listing", {}).items()}
    return s


def base_order(s):
    """Visiting order = element index order (EnumerateStackElements numbers the sorted list)."""
    return sorted(s["elems"])


def model_of(s, elems=None, lod=None):
    elems = elems or s["elems"]
    lod = lod if lod is not None else {i: set(x.get("lod") or ()) for i, x in elems.items()}
    n = max(list(elems) + [s.get("nstack") or 0]) + 1
    return Model(elems, lod, s["fn"], max(n, s.get("nstack") or 0))


def check(s, res):
    """Differences between a model run and the trace (empty = exact)."""
    obs, bad = s["obs"], []
    if not obs["cols"]:
        return ["no offset events in the trace"]
    if obs["undo"] != res["undo"]:
        bad.append("undo: trace %s, model %s" % (obs["undo"], res["undo"]))
    # colouring decisions (last run)
    log = res["log"]
    for r in obs["run"]:
        m = log.get(r["idx"])
        if m is None:
            bad.append("element %d: not coloured by the model" % r["idx"])
            continue
        if r["res"] is not None:
            want = r["res"][1]
            if want != m["res"]:
                bad.append("element %d: trace colour %s, model %d" % (r["idx"], want, m["res"]))
        tt = [t[1] for t in r["tries"] if t[1] != (r["res"] or (0, -1))[1]]
        mt = [c for c, why in m["tries"]]
        if tt != mt:
            bad.append("element %d: tried %s in the trace, %s in the model" % (r["idx"], tt, mt))
    # sorted order, bases, member offsets
    tc = [sorted(i for i, _ in c["members"]) for c in sorted(obs["cols"], key=lambda c: c["c"])]
    mc = [sorted(C["members"]) for C in res["cols"]]
    if tc != mc:
        k = next((k for k in range(min(len(tc), len(mc))) if tc[k] != mc[k]), min(len(tc), len(mc)))
        bad.append("colour order differs from sorted position %d (%d colours in the trace, %d in the model)"
                   % (k, len(tc), len(mc)))
    for c in obs["cols"]:
        if c["c"] < len(res["bases"]) and res["bases"][c["c"]] != c["base"]:
            bad.append("colour %d: base %s in the trace, %s in the model" % (c["c"], c["base"], res["bases"][c["c"]]))
        for i, off in c["members"]:
            if res["offs"].get(i) != off:
                bad.append("element %d: offset %s in the trace, %s in the model" % (i, off, res["offs"].get(i)))
    return bad


# ---------------------------------------------------------------- listing
SYMLINE = re.compile(r"^(\S+) = (-?\d+)\s*; size = (\d+)")
OPSYM = re.compile(r"([_$A-Za-z0-9?@]+)\[(?:esp|ebp)")


def listing_slots(asm, func):
    """{listing symbol: dict(off, size, refs, line)} for one function of a /FAsc listing."""
    import c2trace
    text = open(asm, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)
    head = text[:i].splitlines()
    syms = {}
    for ln in reversed(head[-4000:]):
        m = SYMLINE.match(ln)
        if not m:
            if syms or ln.strip() == "" or ln.startswith(";") or "PROC" in ln or "ENDP" in ln:
                if syms and not ln.startswith("_") and not ln.startswith("$") and not ln.startswith("tv"):
                    break
                continue
            break
        syms[m.group(1)] = dict(off=int(m.group(2)), size=int(m.group(3)), refs=0, line=None, lines=[])
    line = None
    for ln in text[i:j].splitlines():
        m = re.match(r"^; (\d+)\s*:", ln)
        if m:
            line = int(m.group(1))
            continue
        code = ln.split(";")[0]
        for m in OPSYM.finditer(code):
            s = syms.get(m.group(1))
            if s is not None:
                s["refs"] += 1
                if s["line"] is None:
                    s["line"] = line
    return syms


def attach_listing(F, syms):
    """{element idx: dict(lname, loff, lsize, lrefs, line)} by name stem (or tv<key>) and final offset."""
    offs = {}
    for c in F["cols"]:
        for i, off in c["members"]:
            offs[i] = off
    used, out = set(), {}
    by = collections.defaultdict(list)
    for n, s in syms.items():
        by[n.split("$")[0] if not n.startswith("$") else n].append(n)
    for i, e in F["elems"].items():
        cands = []
        if e.get("name"):
            nm = e["name"]
            stem = nm if nm.startswith("$") else nm.split("$")[0]
            cands = [n for n in by.get(stem, []) + by.get("_" + stem, []) + ([nm] if nm in syms else []) if n not in used]
        else:
            t = "tv%d" % (e.get("key") or -1)
            cands = [t] if t in syms else []
        pick = [n for n in cands if syms[n]["off"] == offs.get(i)] or ([cands[0]] if len(cands) == 1 else [])
        if pick:
            n = pick[0]
            used.add(n)
            s = syms[n]
            out[i] = dict(lname=n, loff=s["off"], lsize=s["size"], lrefs=s["refs"], line=s["line"])
    return out


def ename(s, i):
    L = s["listing"].get(i)
    if L:
        return L["lname"]
    e = s["elems"][i]
    return e.get("name") or "tv%s" % e.get("key")


def eline(s, i):
    L = s["listing"].get(i)
    return ("L%d" % L["line"]) if L and L.get("line") else ""


# ---------------------------------------------------------------- report
def report(s, res, bad, tried_max=8, out=sys.stdout):
    fn, E = s["fn"], s["elems"]
    w = out.write
    cols = res["cols"]
    why = []
    if res["sort"]:
        why.append("T > 128" if res["T"] > 0x80 else "GS")
    w("# %s  (c2 %s)\n" % (s["func"], s["build"]))
    w("# %d elements (%d params), %d colours, T=%d%s, %s frame%s%s\n" % (
        len(E), len(s["params"]), len(cols), res["T"], " sorted (%s)" % ", ".join(why) if res["sort"] else
        " (creation order: T <= 128, no GS)", "EBP" if fn.get("ebp") else "ESP",
        ", EbpAdj %d" % fn["ebpadj"] if fn.get("ebpadj") else "", ", undo: param homes dropped (T >= 112)" if res["undo"] else ""))
    w("model: %s\n" % ("reproduces the trace exactly (decisions, colour order, offsets)" if not bad else
                       "%d differences: %s" % (len(bad), "; ".join(bad[:6]))))
    w("\n## colours, in final (sorted) order; offsets as the packer gave them\n")
    w("%4s %4s %7s %5s %6s %8s  %s\n" % ("pos", "made", "offset", "size", "uses", "key", "members (name:packer uses/listing refs line)"))
    for k, C in enumerate(cols):
        key = 1000 * C["uses"] // C["size"] if C["size"] else 0
        mem = []
        for m in C["members"]:
            L = s["listing"].get(m)
            mem.append("%s:%d/%s%s" % (ename(s, m), E[m]["uses"], L["lrefs"] if L else "-",
                                       (" " + eline(s, m)) if eline(s, m) else ""))
        w("%4d %4s %7s %5d %6d %8d  %s%s\n" % (k, ("p" if C.get("param") else "") + str(C["created"]),
                                             res["bases"][k], C["size"], C["uses"], key,
                                             ", ".join(mem[:12]), " ... +%d" % (len(mem) - 12) if len(mem) > 12 else ""))
    w("\n## elements in visiting order (size asc, uses desc, first to reach the count)\n")
    w("%4s %5s %4s %5s %5s  %-26s %-14s %s\n" % ("vis", "idx", "size", "uses", "lst", "name", "-> colour/off", "tried (newest first): reason"))
    created_pos = {id(C): k for k, C in enumerate(cols)}
    made = {C["created"]: C for C in res["created"]}
    order = base_order(s)
    for v, i in enumerate(order):
        x = E[i]
        L = s["listing"].get(i)
        lg = res["log"].get(i) if not x["param"] else None
        if x["param"]:
            dest = "param home"
            tr = ""
        elif lg is None:
            dest, tr = "?", ""
        else:
            C = made[lg["res"]] if lg["res"] in made else None
            dest = "%s%d%s" % ("new " if lg["new"] else "", lg["res"], "" if C is None else "@%s" % res["offs"].get(i))
            parts = []
            for c, r in lg["tries"][:tried_max]:
                if r == "size" or r == "gs":
                    parts.append("%s%d:%s" % ("p" if c < res["nfixed"] else "", c, r))
                else:
                    parts.append("%s%d:conf %s" % ("p" if c < res["nfixed"] else "", c, ename(s, r[1])))
            if len(lg["tries"]) > tried_max:
                parts.append("+%d more" % (len(lg["tries"]) - tried_max))
            tr = " ".join(parts)
        nm = ename(s, i) + ((" " + eline(s, i)) if eline(s, i) else "")
        w("%4d %5d %4d %5d %5s  %-26s %-14s %s%s\n" % (v, i, x["size"], x["uses"], L["lrefs"] if L else "-", nm[:26], dest,
                                                     tr, ("  [LOD cut at #%d%s]" % (x["lodtrunc"], ", %d inferred" % len(x["inferred"]) if x.get("inferred") else "")) if x.get("lodtrunc") not in (None, False) else ""))


# ---------------------------------------------------------------- what-if
def resolve(s, name):
    if name.startswith("#"):
        return int(name[1:])
    hits = [i for i in s["elems"] if ename(s, i) == name]
    if not hits:
        hits = [i for i in s["elems"] if (s["elems"][i].get("name") or "") == name or ename(s, i).split("$")[0] == name]
    if len(hits) != 1:
        sys.exit("what-if: %r names %d elements%s" % (name, len(hits), (": " + ", ".join("#%d=%s" % (i, ename(s, i)) for i in hits[:8])) if hits else ""))
    return hits[0]


def apply_edits(s, edits):
    """(elems, lod, order) after the edits."""
    elems = {i: dict(x) for i, x in s["elems"].items()}
    lod = {i: set(x.get("lod") or ()) for i, x in elems.items()}
    order = base_order(s)
    for ed in edits:
        kind, _, arg = ed.partition(":")
        if kind in ("uses", "size"):
            nm, _, val = arg.partition("=")
            front = val.endswith("^")
            i = resolve(s, nm)
            elems[i][kind] = int(val.rstrip("^"))
            order.remove(i)
            k = _slot_for(order, elems, i, front)
            order.insert(k, i)
        elif kind == "pos":
            nm, _, val = arg.partition("=")
            i = resolve(s, nm)
            order.remove(i)
            order.insert(int(val), i)
        elif kind in ("conf+", "conf-"):
            a, b = (resolve(s, x) for x in arg.split(","))
            if kind == "conf+":
                lod[a].add(b)
                lod[b].add(a)
            else:
                lod[a].discard(b)
                lod[b].discard(a)
        elif kind == "add":
            parts = arg.split(",")
            nm, size, uses = parts[0], int(parts[1]), int(parts[2])
            pos, confs = None, []
            for p in parts[3:]:
                if p.startswith("conf="):
                    confs = [resolve(s, q) for q in p[5:].split(";") if q]
                else:
                    pos = int(p)
            i = max(elems) + 1
            elems[i] = dict(idx=i, size=size, uses=uses, param=False, name=nm, align=3 if size >= 4 else 1 if size == 1 else 2,
                            never=False, gs=0)
            lod[i] = set(confs)
            for c in confs:
                lod[c].add(i)
            s["listing"][i] = dict(lname=nm, loff=None, lsize=size, lrefs=None, line=None)
            order.insert(pos if pos is not None else _slot_for(order, elems, i, False), i)
        else:
            sys.exit("unknown edit %r (uses: size: pos: conf+: conf-: add:)" % ed)
    return elems, lod, order


def _slot_for(order, elems, i, front):
    """Where AddStackElement's order puts i: size ascending, uses descending; among equals at the end (or front)."""
    x = elems[i]
    for k, j in enumerate(order):
        y = elems[j]
        if (y["size"], -y["uses"]) > (x["size"], -x["uses"]) or (front and (y["size"], -y["uses"]) == (x["size"], -x["uses"])):
            return k
    return len(order)


def run_edits(s, edits):
    elems, lod, order = apply_edits(s, edits)
    mdl = Model(elems, lod, s["fn"], max(max(elems) + 1, s.get("nstack") or 0))
    return mdl.run(order, record=True), elems


def what(s, edits, out=sys.stdout):
    base = model_of(s).run(base_order(s), record=True)
    res, elems = run_edits(s, edits)
    moved = [(i, base["offs"].get(i), res["offs"].get(i)) for i in sorted(set(base["offs"]) | set(res["offs"]))
             if base["offs"].get(i) != res["offs"].get(i)]
    out.write("# what-if %s: T %d -> %d, %d colours -> %d%s; %d elements moved\n" % (
        " ".join(edits), base["T"], res["T"], len(base["cols"]), len(res["cols"]),
        "" if base["sort"] == res["sort"] else " (sorted %s -> %s)" % (base["sort"], res["sort"]), len(moved)))
    for i, a, b in moved:
        out.write("  %-28s %6s -> %s\n" % (ename(s, i) if i in s["elems"] or i in s["listing"] else "#%d" % i, a, b))
    return res


# ---------------------------------------------------------------- the original's frame slots
FRAMEOP = re.compile(r"\[(esp|ebp)(?:\s*\+\s*e[a-z]{2}(?:\s*\*\s*\d)?)?(?:\s*([+-])\s*(0x[0-9a-f]+|\d+))?\]")


def _num(t):
    return int(t, 16) if t.startswith("0x") else int(t)


def read_orig(path=None, image=None, va=None):
    """[(va, mnemonic, operands)] of the original function: orig.s lines, or recursive descent in an image."""
    if path:
        out = []
        for ln in open(path):
            p = ln.split(None, 2)
            if len(p) >= 2 and re.fullmatch(r"(0x)?[0-9a-fA-F]+", p[0]):
                if len(p) > 2 and re.fullmatch(r"[0-9a-f]{2,}", p[1]) and not re.fullmatch(r"[a-z]+", p[1]):
                    p = [p[0]] + p[2].split(None, 1)              # 'VA bytes mnemonic operands'
                out.append((int(p[0], 16), p[1].lower(), (p[2].strip() if len(p) > 2 else "").lower()))
        return out
    import capstone, pefile
    pe = pefile.PE(image, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)

    def rd(a, n):
        return pe.get_data(a - base, n)
    seen, work = {}, [va]
    while work:
        a = work.pop()
        while a not in seen:
            i = next(md.disasm(rd(a, 16), a), None)
            if i is None:
                break
            seen[a] = (i.address, i.mnemonic, i.op_str)
            m = i.mnemonic
            if m == "ret" or (m == "jmp" and not i.op_str.startswith("0x")):
                if m == "jmp" and "*4 +" in i.op_str:          # jump table: jmp dword ptr [reg*4 + T]
                    t = re.search(r"\+ (0x[0-9a-f]+)\]", i.op_str)
                    if t:
                        T = int(t.group(1), 16)
                        for k in range(4096):
                            v = int.from_bytes(rd(T + 4 * k, 4), "little")
                            if not (va <= v < va + 0x100000):
                                break
                            work.append(v)
                break
            if m.startswith("j") and i.op_str.startswith("0x"):
                work.append(int(i.op_str, 16))
                if m == "jmp":
                    break
            a += i.size
    return [seen[a] for a in sorted(seen)]


def image_callee_pop(image):
    """callee_pop(va) for direct calls into `image`: the N of the callee's first `ret N` (after a jmp thunk)."""
    import capstone, pefile
    pe = pefile.PE(image, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    memo = {}

    def pop(t, hop=0):
        if t in memo:
            return memo[t]
        r = 0
        try:
            code = pe.get_data(t - base, 0x4000)
            for i in md.disasm(code, t):
                if i.mnemonic == "ret":
                    r = int(i.op_str, 0) if i.op_str else 0
                    break
                if i.mnemonic == "jmp" and i.address == t and i.op_str.startswith("0x") and hop < 4:
                    r = pop(int(i.op_str, 16), hop + 1)
                    break
        except Exception:
            r = 0
        memo[t] = r
        return r
    return pop


def orig_slots(ins, callee_pop=None):
    """[(index into ins, [frame keys])] and the frame summary. A key is the operand's address relative to the
    return-address slot at entry (0 = the return address, +4 the first stack argument; esp operands: displacement
    minus push depth; ebp operands: displacement plus where ebp points). Depth: push/pop/sub/add esp, calls (callee pops when no `add esp` follows: the pushes since
    the previous call), `mov eax, N` + call at the start (__chkstk)."""
    by = {a: k for k, (a, _, _) in enumerate(ins)}
    nxt = {ins[k][0]: ins[k + 1][0] for k in range(len(ins) - 1)}
    depth, work, conflicts = {}, [(ins[0][0], 0, 0, None)], 0      # bytes below the return-address slot
    frame = None
    m0 = re.match(r"eax, (0x[0-9a-f]+|\d+)$", ins[0][2]) if ins[0][1] == "mov" else None
    if m0:
        frame = _num(m0.group(1))
    # prologue: callee-saved pushes, sub esp / __chkstk, push ebp; mov ebp, esp: not call arguments
    pro = 0
    while pro < len(ins) and pro < 12:
        mn, ops = ins[pro][1], ins[pro][2]
        if (mn == "push" and ops in ("ebx", "ebp", "esi", "edi", "ecx")) or (mn == "sub" and ops.startswith("esp,")) \
                or (mn == "mov" and ops == "ebp, esp") or (pro == 0 and m0) or (pro == 1 and m0 and mn == "call") \
                or (mn == "and" and ops.startswith("esp,")):
            pro += 1
        else:
            break
    prologue = {ins[k][0] for k in range(pro)}
    # call targets the caller cleans up after (an `add esp, N` within a few instructions of some call to them): cdecl
    cdecl = set()
    for k, (_, mn, ops) in enumerate(ins):
        if mn != "call":
            continue
        for k2 in range(k + 1, min(k + 6, len(ins))):
            m2, o2 = ins[k2][1], ins[k2][2]
            if m2 == "add" and o2.startswith("esp,"):
                cdecl.add(ops)
                break
            if m2 in ("call", "ret", "push") or m2.startswith("j"):
                break
    seeded = False
    while work or not seeded:
        if not work:
            # code the descent did not reach (switch cases through jump tables): walk it at the body depth
            seeded = True
            if not depth:
                break
            body = collections.Counter(d for d, _ in depth.values()).most_common(1)[0][0]
            ebps = collections.Counter(e for _, e in depth.values() if e is not None)
            eb = ebps.most_common(1)[0][0] if ebps else None
            work = [(a, body, 0, eb) for a, _, _ in ins if a not in depth][::-1]
            if not work:
                break
        a, d, since, ebp = work.pop()
        if seeded and a in depth:
            continue
        while a in by:
            k = by[a]
            if a in depth:
                if depth[a][0] != d:
                    conflicts += 1
                break
            depth[a] = (d, ebp)
            _, mn, ops = ins[k]
            if mn == "push":
                d += 4
                if a not in prologue:
                    since += 4
            elif mn == "pop":
                d -= 4
            elif mn in ("sub", "add") and ops.startswith("esp, "):
                try:
                    n = _num(ops[5:])
                    d += n if mn == "sub" else -n
                    if mn == "add":
                        since = 0
                except ValueError:
                    pass
            elif mn == "mov" and ops == "ebp, esp":
                ebp = d
            elif mn == "lea" and ops.startswith("ebp, [esp"):
                mm = FRAMEOP.search(ops)
                ebp = d - (_num(mm.group(3)) * (1 if mm.group(2) == "+" else -1) if mm and mm.group(3) else 0)
            elif mn == "call":
                if frame is not None and a == ins[min(1, len(ins) - 1)][0] and depth.get(ins[0][0], (1,))[0] == 0:
                    d += frame
                else:
                    if callee_pop is not None and ops.startswith("0x"):
                        d -= callee_pop(int(ops, 16))
                    elif ops not in cdecl:
                        d -= since                      # stdcall / thiscall / virtual: the callee pops its pushes
                since = 0
            if mn == "ret":
                break
            if mn.startswith("j") and ops.startswith("0x"):
                work.append((int(ops, 16), d, since, ebp))
                if mn == "jmp":
                    break
            elif mn == "jmp":
                break
            a = nxt.get(a)
    out = []
    for k, (a, mn, ops) in enumerate(ins):
        if a not in depth:
            continue
        d, ebp = depth[a]
        keys = []
        for m in FRAMEOP.finditer(ops):
            disp = _num(m.group(3)) * (1 if m.group(2) == "+" else -1) if m.group(3) else 0
            if m.group(1) == "esp":
                keys.append(disp - d)
            elif ebp is not None:
                keys.append(disp - ebp)
        if keys and not mn.startswith("lea") or keys and mn == "lea":
            out.append((k, keys))
    return out, dict(reached=len(depth), total=len(ins), conflicts=conflicts, frame=frame)


def orig_map(s, asm, ins, callee_pop=None):
    """{element idx: (orig offset in our packer's coordinates, votes, total)} by aligning our listing with ins."""
    import regmap, tsweep
    lst = tsweep.listing_offsets(asm, s["func"])
    offs = sorted(lst)
    ours = [regmap.norm((lst[o].split()[0], lst[o].split(None, 1)[1] if " " in lst[o] else "")) for o in offs]
    orig = [(mn, ops) for _, mn, ops in ins]
    slots, info = orig_slots(ins, callee_pop)
    okeys = dict(slots)
    name2i = {L["lname"]: i for i, L in s["listing"].items()}
    pairs = regmap.align(ours, orig)
    votes = collections.defaultdict(collections.Counter)
    diffs = {True: collections.Counter(), False: collections.Counter()}     # keyed by "above the return address"
    for i, j in pairs:
        ns = [m.group(1) for m in OPSYM.finditer(ours[i][1])]
        ks = okeys.get(j, [])
        if not ns or len(ns) != len(ks):
            continue
        for n, key in zip(ns, ks):
            e = name2i.get(n)
            if e is None:
                continue
            votes[e][key] += 1
            L = s["listing"][e]
            if L.get("loff") is not None:
                diffs[key > 0][key - L["loff"]] += 1
    # the listing gives locals relative to the return-address slot in an ESP frame and params relative to 4 below
    # it (ebp-style), so the two regions get their own shift
    CL = diffs[False].most_common(1)[0][0] if diffs[False] else 0
    CP = diffs[True].most_common(1)[0][0] if diffs[True] else (CL if s["fn"].get("ebp") else CL - 4)
    out = {}
    for e, v in votes.items():
        key, n = v.most_common(1)[0]
        out[e] = (key - (CP if key > 0 else CL), n, sum(v.values()))
    return out, dict(info, pairs=len(pairs), ours=len(ours), orig=len(orig), shift=CL, pshift=CP)


def side_by_side(s, om, res, out=sys.stdout):
    w = out.write
    agree = sum(1 for i, (o, n, t) in om.items() if res["offs"].get(i) == o)
    w("\n## ours against the original (offsets in our packer's coordinates)\n")
    w("# %d of %d elements seen in both are at the original's offset\n" % (agree, len(om)))
    w("%-28s %5s %5s %7s %7s %s\n" % ("element", "size", "uses", "ours", "orig", "votes"))
    for i in sorted(om, key=lambda i: (res["offs"].get(i) or 0)):
        o, n, t = om[i]
        mine = res["offs"].get(i)
        w("%-28s %5d %5d %7s %7s %d/%d%s\n" % (ename(s, i)[:28], s["elems"][i]["size"], s["elems"][i]["uses"], mine, o, n, t,
                                              "" if mine == o else "  <-"))
    return agree


def search(s, om, top=15, limit=800, out=sys.stdout):
    """Single edits ranked by how many elements end at the original's offset."""
    target = {i: o for i, (o, n, t) in om.items()}
    base = model_of(s).run(base_order(s), record=True)

    def score(res):
        return sum(1 for i, o in target.items() if res["offs"].get(i) == o)
    b0 = score(base)
    cands = []
    E = s["elems"]
    wrong = {i for i, o in target.items() if base["offs"].get(i) != o}
    mates = set()
    for C in base["cols"]:
        if any(m in wrong for m in C["members"]):
            mates.update(C["members"])
    nonp = {i for i in E if not E[i]["param"]}
    focus = sorted(wrong & nonp) + sorted((mates - wrong) & nonp)      # the misplaced elements first
    for i in focus:
        nm = "#%d" % i
        for d in (-3, -2, -1, 1, 2, 3):
            u = E[i]["uses"] + d
            if u >= 1:
                cands.append(["uses:%s=%d" % (nm, u)])
                cands.append(["uses:%s=%d^" % (nm, u)])
        order = base_order(s)
        run = [k for k, j in enumerate(order) if E[j]["size"] == E[i]["size"]]
        for k in run[:: max(1, len(run) // 24)]:
            cands.append(["pos:%s=%d" % (nm, k)])
        lg = base["log"].get(i) or {}
        for c, why in lg.get("tries", []):
            if isinstance(why, (list, tuple)) and why[0] == "conf":
                cands.append(["conf-:%s,#%d" % (nm, why[1])])
        if lg and not lg.get("new"):
            for m in base["created"][lg["res"]]["members"][:4]:
                if m != i:
                    cands.append(["conf+:%s,#%d" % (nm, m)])
    cands = cands[:limit]
    out.write("\n## search: %d single edits over %d elements (base: %d of %d at the original's offset)\n" % (
        len(cands), len(focus), b0, len(target)))
    rows = []
    for ed in cands:
        try:
            res, _ = run_edits(dict(s, listing=dict(s["listing"])), ed)
        except SystemExit:
            continue
        sc = score(res)
        if sc > b0:
            rows.append((sc, ed))
    rows.sort(key=lambda r: -r[0])
    for sc, ed in rows[:top]:
        out.write("  %+4d  %4d  %s   (%s)\n" % (sc - b0, sc, " ".join(ed), ", ".join(
            ename(s, int(x[1:])) for x in re.findall(r"#\d+", " ".join(ed)))))
    if not rows:
        out.write("  no single edit improves the match\n")
    return rows


# ---------------------------------------------------------------- driver
def trace(src, func, cl, keep=None):
    import c2trace, tempfile
    info = build_info()
    hooks = [expand(h, info["sbv"]) for h in info["hooks"]]
    d = keep or tempfile.mkdtemp(prefix="c2slots_")
    img, hk, rc, out, log = c2trace.run_trace(src, hooks, cl, keep=d, quiet=True)
    if rc:
        sys.stdout.write(out)
    funcs = [F for F in parse(log, info) if c2trace.func_match(func, F["name"] or "")]
    asm = os.path.join(d, os.path.splitext(os.path.basename(src))[0] + ".asm")
    return info, funcs, asm, d


def main(argv):
    if argv[:1] in (["what"], ["search"], ["orig"]):
        return offline(argv)
    ap = argparse.ArgumentParser(prog="c2trace.py slots", usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    ap.add_argument("--keep")
    ap.add_argument("--export")
    ap.add_argument("--all", action="store_true", help="every function --func selects (default: the first)")
    ap.add_argument("--tried", type=int, default=8, help="colours shown per element (default 8)")
    ap.add_argument("--what", nargs="+", default=[], metavar="EDIT")
    ap.add_argument("--orig")
    ap.add_argument("--image")
    ap.add_argument("--va")
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--quiet", action="store_true", help="only the summary and model lines")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    info, funcs, asm, d = trace(a.src, a.func, cl, a.keep)
    if not funcs:
        sys.exit("no stack packing seen for %r (no frame elements, or the compile failed)" % a.func)
    rc = 0
    for F in (funcs if a.all else funcs[:1]):
        s = run_one(F, info, asm, a)
        rc |= 1 if s.get("_bad") else 0
    if not a.keep:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    return rc


def run_one(F, info, asm, a):
    lst = {}
    if os.path.exists(asm):
        try:
            lst = attach_listing(F, listing_slots(asm, F["name"]))
        except KeyError:
            lst = {}
    s = snapshot(F, info, lst)
    res = model_of(s).run(base_order(s), record=True)
    bad = check(s, res)
    s["_bad"] = bad
    if a.quiet:
        print("# %s: %d elements, %d colours, T=%d; model: %s" % (s["func"], len(s["elems"]), len(res["cols"]), res["T"],
              "exact" if not bad else "%d differences: %s" % (len(bad), "; ".join(bad[:4]))))
    else:
        report(s, res, bad, a.tried)
    if a.export:
        path = a.export
        if a.all:                                   # one snapshot per function: SNAP.<name>.json
            path = re.sub(r"(\.json)?$", "." + re.sub(r"[^A-Za-z0-9_]+", "_", s["func"] or "f").strip("_") + ".json",
                          a.export, count=1)
        json.dump({k: v for k, v in s.items() if k != "_bad"}, open(path, "w"), indent=0)
    if a.what:
        print()
        what(s, a.what)
    if a.orig or a.image:
        ins = read_orig(a.orig, a.image, int(a.va, 16) if a.va else None)
        om, oi = orig_map(s, asm, ins, image_callee_pop(a.image) if a.image else None)
        print("\n# original: %d instructions, %d reached (%d depth conflicts), %d aligned with ours, shift %d (locals) %d (params)"
              % (oi["total"], oi["reached"], oi["conflicts"], oi["pairs"], oi["shift"], oi["pshift"]))
        side_by_side(s, om, res)
        if a.search:
            search(s, om, a.top, a.limit)
    return s


def offline(argv):
    ap = argparse.ArgumentParser(prog="c2slots.py " + argv[0])
    ap.add_argument("snap", nargs="?" if argv[0] == "orig" else None)
    ap.add_argument("edits", nargs="*")
    ap.add_argument("--orig")
    ap.add_argument("--image")
    ap.add_argument("--va")
    ap.add_argument("--asm", help="our listing (default: the snapshot's listing names only)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--limit", type=int, default=800)
    a = ap.parse_args(argv[1:])
    if argv[0] == "orig":
        ins = read_orig(a.orig, a.image, int(a.va, 16) if a.va else None)
        slots, info = orig_slots(ins)
        cnt = collections.Counter(k for _, ks in slots for k in ks)
        print("# %d instructions, %d reached, %d depth conflicts%s" % (info["total"], info["reached"], info["conflicts"],
              ", frame %#x" % info["frame"] if info["frame"] else ""))
        for k in sorted(cnt):
            print("%6d  %4d refs" % (k, cnt[k]))
        return 0
    s = load(a.snap)
    if argv[0] == "what":
        what(s, a.edits)
        return 0
    if not a.asm:
        sys.exit("search needs --asm OUR.asm (the listing the snapshot was made from; c2trace.py slots --keep DIR)")
    ins = read_orig(a.orig, a.image, int(a.va, 16) if a.va else None)
    om, oi = orig_map(s, a.asm, ins)
    res = model_of(s).run(base_order(s), record=True)
    side_by_side(s, om, res)
    search(s, om, a.top, a.limit)
    return 0


if __name__ == "__main__":
    import c2build  # noqa: F401  (takes --c2 out of argv)
    sys.exit(main(sys.argv[1:]))
