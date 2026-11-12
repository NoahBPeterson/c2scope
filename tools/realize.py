#!/usr/bin/env python3
"""From an abstract allocation edit to the C++ that realises it.  Doc: docs/c2/realize.md

usage: realize.py edit    <tu.cpp> --func F EDIT [EDIT..] [--want-regs v=reg,..] [--asm-vs ORIG.s] [--top N]
                          [--constructs a,b] [--jobs 6] [--limit N] [--list] [--keep DIR] [-- <cl args>]
       realize.py --from-rasim auto|ANSWERS.txt|- <tu.cpp> --func F [--asm-vs ORIG.s] [--want-regs v=reg,..]
                          [--regmap REGMAP.txt] [--max-answers N] [--max-compiles N] [--with-store] [--no-callee]
                          [-- <cl args>]
       realize.py measure <base.cpp> <variant.cpp> --func F [--keys] [-- <cl args>]
       realize.py pairs   <probes.cpp> [--keys] [--only RE] [--diff] [-- <cl args>]
   --c2 13.10 / 13.10.3052 / 15.00 as on every tool.

The models answer in abstract edits; this tool goes the other way.

edit     EDIT is one abstract edit in the syntax `rasim need` / `rasim want` print, or symkeys' form:
           ref:VAR:B[:C]    one more reference to VAR in block B          unref:VAR:B    a reference fewer
           extend:VAR:B     VAR's range reaches block B                   drop:VAR:B     the range leaves B
           var:NAME:B[:C]   a new candidate referenced in B               P:B:+1 / -1    one more / fewer candidate in B
           add:frame@V:n    n more frame symbol keys before V's first reference
         Blocks are numbered as `c2trace.py regs --why` numbers them. The tool maps block B to source lines through
         the trace's line info, generates every catalogued construct for that edit kind at the statement boundaries
         of those lines (CONSTRUCTS below; docs/c2/realize.md has the measured table), compiles and traces each
         variant once, and reports per variant: did the requested edit happen (`exact`: it and nothing else; `yes`:
         it and the side effects listed after `also:`; `no`, with what it did instead), the code cost (`code`:
         aligned diff of the listings with registers masked, operands kept: 0 = code-neutral; `bag`: the same as a
         multiset without frame displacements: 0 = the same instructions in another order; `regs`: the diff with
         registers), the registers of --want-regs, and with --asm-vs the regdiff against the original.
         Several EDITs are realised one by one and then together. --list prints the candidates and stops.
--from-rasim   the pipeline. ANSWERS.txt is the output of `rasim need SNAP reg:..` / `rasim want SNAP regmap.txt`
         (`-`: stdin); `auto` makes it (regmap --all against --asm-vs, then rasim want). Three stages, stopping at
         the first variant that matches (regdiff 0 with --asm-vs, otherwise the wanted registers with bag 0):
           1. each answer made of source-level edits, each edit through its code-neutral constructs;
           2. every structural code-neutral construct that touches a block an answer names (an order answer
              `before:A:B` is turned into blocks by `rasim need 'A>B'`), singly, then those that changed the
              allocation's inputs without changing an instruction together (per family, and all);
           3. stub bodies for the bodiless callees that receive a local's address (docs/c2/callee.md).
         Prints `FOUND ... <variant.cpp>` or `NOT FOUND ... closest: ...`.
measure  what did a source change do, in abstract edits: compiles both files and prints the difference of the two
         traces of F (per block P, references, ranges, new and lost candidates, constants, interference, copies,
         preferences, registers, priorities) and the code cost. With --keys also the symbol-key difference.
pairs    the same for a probe file: every function named X__base is compared with each X__<variant> in the file
         (one compile). This is how the catalogue was measured: tests/data/realize.cpp, work/scratch/realize/.

Variants are kept in work/realize/<tu>[@build]/ (or --keep DIR).
"""
import argparse, difflib, json, os, re, shutil, subprocess, sys, tempfile, threading
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from image import ROOT
import c2trace, c2regs, tsweep

RASIM = os.path.join(ROOT, "tools", "rasim", "target", "release", "rasim")
REG = re.compile(r"\b(e?[abcd]x|[abcd][lh]|e?si|e?di|e?bp|[sd]il?|st\(\d\)|xmm\d)\b")


class _Mute:
    """stdout that a worker thread can switch off for itself (contextlib.redirect_stdout is process-wide)."""

    def __init__(self, real):
        self.real, self.tl = real, threading.local()

    def write(self, text):
        return len(text) if getattr(self.tl, "on", False) else self.real.write(text)

    def __getattr__(self, name):
        return getattr(self.real, name)


# ---------------------------------------------------------------- one traced compile

def _norm(op, fname):
    """Listing operands made comparable between two functions / two compiles: labels, COMDAT-local names."""
    op = re.sub(r"\$L[A-Z]*\d+(?:@[\w@?$]+)?", "$L", op)    # 13.10 writes $L123, later builds $LN12@func
    op = re.sub(r"\$T\d+", "$T", op)
    op = re.sub(r"\$\d+\b", "$", op)                       # _x$1234 block-scoped locals
    if fname:
        op = op.replace(fname, "F")
    m = re.match(r"(.*\b(BYTE|WORD) PTR .*, )(-?\d+)$", op)   # the listing prints a narrow immediate as written
    if m:
        op = m.group(1) + str(int(m.group(3)) & (0xff if m.group(2) == "BYTE" else 0xffff))
    return op


class Fn:
    """One function of a traced compile: c2regs' parse result, the rasim snapshot and the listing."""

    def __init__(self, fn, asm_text, asm_path):
        self.fn = fn
        self.name = fn["name"]
        self.snap = c2regs.export(fn)
        try:
            instrs = tsweep.listing_instrs(asm_path, self.name)
        except Exception:
            instrs = []
        self.instrs = instrs
        self.code = [(m, _norm(o, self.name)) for m, o in instrs]
        self.masked = [(m, REG.sub("r", o)) for m, o in self.code]
        self.bag = sorted((m, re.sub(r"\[(r|esp)[+-]\d+\]", r"[\1]", o)) for m, o in self.masked if m not in ("npad",))
        self.shape = [tsweep._key(x) for x in instrs]
        cls = fn.get("pcls", {})
        ints = [n for n in sorted({k[0] for k in fn.get("blkP", {})}) if cls.get(n) in (0, None)]
        self.np = ints[0] if ints else 1
        self.blocks = []            # (b, w, P, sorted line offsets)
        nb = max([b for (p, b) in fn.get("blkP", {}) if p == self.np] or [0])
        bbof = {b: bb for b, w, P, bb in fn.get("bbs", [])} if self.np == 1 else {}
        for b in range(1, nb + 1):
            w, P = fn["blkP"].get((self.np, b), (0, 0))
            self.blocks.append((b, w, P, sorted(fn["lines"].get(bbof.get(b), ()))))
        # state[key][b] = cost (referenced) or None (live through)
        self.state, self.ntmp = {}, {}
        for (p, b), lst in fn.get("byblock", {}).items():
            if p != self.np:
                continue
            for nm, g, cost, d in lst:
                im = fn.get("immof", {}).get((p, g))
                if im:
                    key = "imm=" + im["text"]
                elif re.match(r"t\d+$", nm) or nm == "?" or re.match(r"(k\d+_|local|imm)\d+$", nm):
                    t = self.ntmp.setdefault(b, [0, 0])
                    t[cost is None] += 1
                    continue
                else:
                    key = nm
                st = self.state.setdefault(key, {})
                if b in st and st[b] is not None and cost is not None:
                    st[b] += cost               # two webs of one name referenced in one block
                elif b not in st or cost is not None:
                    st[b] = cost
        self.regs = {}
        for r in fn["rows"]:
            if r["cls"] in (0, None) and r["npass"]:
                self.regs.setdefault(r["name"], []).append(r["out"] or "-")
        self.prio = {}
        for r in fn["rows"]:
            if r["cls"] in (0, None) and r["npass"]:
                self.prio.setdefault(r["name"], r["prio"])
        # relations among named values (symmetric closure of what ChoosePhysReg saw)
        self.interf, self.copies, self.prefs = set(), set(), {}
        named = lambda n: n and not re.match(r"(t|imm|k\d+_|local)\d+$", n) and n != "?"
        gname = {p["gr"]: p["name"] for p in self.snap.get("pops", [])}
        for p in self.snap.get("pops", []):
            c = p.get("choice")
            if not c or p.get("cls") not in (0, None):
                continue
            for n in c["nbrs"]:
                if named(p["name"]) and named(n["name"]) and n["name"] != p["name"]:
                    self.interf.add(tuple(sorted((p["name"], n["name"]))))
            for n in c["partners"]:
                o = gname.get(n["gr"])
                if named(p["name"]) and named(o) and o != p["name"]:
                    self.copies.add(tuple(sorted((p["name"], o))))
        rn = self.snap.get("regnames", {})
        for e in self.snap.get("pref_events", []):
            nm = gname.get(e["gr"])
            if named(nm) and e.get("src") is None:
                k = (nm, rn.get(str(e["reg"]), str(e["reg"])))
                self.prefs[k] = self.prefs.get(k, 0) + 1

    def lines_of(self, b):
        return self.blocks[b - 1][3] if 0 < b <= len(self.blocks) else []


class Trace:
    def __init__(self, src, cl, keep=None):
        d = keep or tempfile.mkdtemp(prefix="realize_")
        _, _, rc, out, log = c2trace.run_trace(src, c2regs.HOOKS, cl, keep=d, quiet=True)
        self.rc, self.out, self.log = rc, out, log
        self.asm = os.path.join(d, os.path.splitext(os.path.basename(src))[0] + ".asm")
        self.dir, self.tmp = d, keep is None
        self.err = " ".join(re.findall(r"error C\d+:[^\n]*", out))[:200]
        self._asm_text = None

    def funcs(self, pat=""):
        txt = open(self.asm, errors="replace").read() if os.path.exists(self.asm) else ""
        return [Fn(f, txt, self.asm) for f in c2regs.parse(self.log, pat)]

    def func(self, pat):
        f = self.funcs(pat)
        if not f:
            raise RuntimeError("compile error: " + self.err if self.err else "function %s not register-allocated" % pat)
        return f[0]

    def close(self):
        if self.tmp:
            shutil.rmtree(self.dir, ignore_errors=True)


def adiff(a, b):
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return len(a) + len(b) - 2 * sum(m.size for m in sm.get_matching_blocks())


# ---------------------------------------------------------------- the difference of two traces, as abstract edits

def blockmap(a, b):
    """base block -> variant block. Identity when the block lists agree in length and weights; otherwise an
    alignment on (w, lines), and the unmatched blocks are reported."""
    if len(a.blocks) == len(b.blocks) and all(x[1] == y[1] for x, y in zip(a.blocks, b.blocks)):
        return {x[0]: x[0] for x in a.blocks}, [], []
    ka = [(x[1], tuple(x[3])) for x in a.blocks]
    kb = [(x[1], tuple(x[3])) for x in b.blocks]
    sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
    m = {}
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            m[blk.a + k + 1] = blk.b + k + 1
    return m, [x[0] for x in a.blocks if x[0] not in m], [y[0] for y in b.blocks if y[0] not in m.values()]


class Delta:
    """What a source change did between two traces of one function, as a list of abstract edits."""

    def __init__(self, a, b):
        self.a, self.b = a, b
        self.map, self.lost, self.new = blockmap(a, b)
        inv = {v: k for k, v in self.map.items()}
        e = self.edits = []            # (kind, var, block (base numbering, or 'n<variant block>'), amount)
        self.P = {}
        for (bb, w, P, _l) in a.blocks:
            if bb in self.map:
                P2 = b.blocks[self.map[bb] - 1][2]
                if P2 != P:
                    self.P[bb] = P2 - P
        bname = lambda vb: inv.get(vb, "n%d" % vb)
        for key in sorted(set(a.state) | set(b.state)):
            sa = a.state.get(key, {})
            sb = {bname(k): v for k, v in b.state.get(key, {}).items()}
            imm = key.startswith("imm=")
            if not sa and sb:
                refs = sorted(((k, v) for k, v in sb.items() if v is not None), key=lambda t: str(t[0]))
                e.append(("imm+" if imm else "var", key, [k for k, _ in refs], [v for _, v in refs]))
                continue
            if sa and not sb:
                e.append(("imm-" if imm else "gone", key, sorted(k for k, v in sa.items() if v is not None), None))
                continue
            for blk in sorted(set(sa) | set(sb), key=str):
                if blk in self.lost:
                    continue
                x, y = sa.get(blk, "absent"), sb.get(blk, "absent")
                if x == y:
                    continue
                if x == "absent":
                    e.append(("extend" if y is not None else "through+", key, blk, y))
                elif y == "absent":
                    e.append(("drop" if x is not None else "through-", key, blk, x))
                elif x is None:
                    e.append(("ref", key, blk, y))
                elif y is None:
                    e.append(("unref", key, blk, x))
                else:
                    e.append(("ref", key, blk, y - x))
        self.tmp = {}
        for bb in self.map:
            x, y = a.ntmp.get(bb, [0, 0]), b.ntmp.get(self.map[bb], [0, 0])
            if x[0] != y[0]:
                self.tmp[bb] = y[0] - x[0]
        self.regs = {n: (a.regs.get(n), b.regs.get(n)) for n in sorted(set(a.regs) | set(b.regs))
                     if a.regs.get(n) != b.regs.get(n) and not re.match(r"(t|imm|local)\d+$", n)}
        self.prio = {n: (a.prio.get(n), b.prio.get(n)) for n in sorted(set(a.prio) & set(b.prio))
                     if a.prio[n] != b.prio[n] and not re.match(r"(t|imm)\d+$", n)}
        self.interf = (sorted(b.interf - a.interf), sorted(a.interf - b.interf))
        self.copies = (sorted(b.copies - a.copies), sorted(a.copies - b.copies))
        self.prefs = {k: b.prefs.get(k, 0) - a.prefs.get(k, 0) for k in sorted(set(a.prefs) | set(b.prefs))
                      if a.prefs.get(k, 0) != b.prefs.get(k, 0)}
        self.code = adiff(a.masked, b.masked)
        self.ident = adiff(a.code, b.code)
        self.shape = adiff([(m, tuple("r" for _ in r) if isinstance(r, tuple) else r) for m, r in a.shape],
                           [(m, tuple("r" for _ in r) if isinstance(r, tuple) else r) for m, r in b.shape])
        self.dn = len(b.code) - len(a.code)
        self.bag = adiff(a.bag, b.bag)       # the same instructions in any order, frame offsets aside: 0

    def strings(self, through=False):
        out = []
        for kind, var, blk, amt in self.edits:
            if kind in ("through+", "through-") and not through:
                continue
            if kind in ("var", "imm+"):
                out.append("%s:%s:%s" % (kind, var, ",".join("%s:%s" % (k, c) for k, c in zip(blk, amt)) or "-"))
            elif kind in ("gone", "imm-"):
                out.append("%s:%s:%s" % (kind, var, ",".join(map(str, blk)) or "-"))
            elif kind == "ref":
                out.append("ref:%s:%s:%+d" % (var, blk, amt))
            else:
                out.append("%s:%s:%s" % (kind, var, blk))
        out += ["tmp:%s:%+d" % kv for kv in sorted(self.tmp.items())]
        out += ["P:%s:%+d" % kv for kv in sorted(self.P.items())]
        if self.lost or self.new:
            out.append("blocks:-%d+%d" % (len(self.lost), len(self.new)))
        out += ["interf:%s:%s" % p for p in self.interf[0]] + ["nointerf:%s:%s" % p for p in self.interf[1]]
        out += ["copy:%s:%s" % p for p in self.copies[0]] + ["nocopy:%s:%s" % p for p in self.copies[1]]
        out += ["pref:%s:%s:%+d" % (k[0], k[1], v) for k, v in self.prefs.items()]
        return out

    def empty(self):
        return not self.strings(True)

    def summary(self, width=150):
        s = " ".join(self.strings()) or "(no allocation-input change)"
        if len(s) > width:
            s = s[:width - 4] + " ..."
        return s

    def regstr(self):
        return " ".join("%s:%s->%s" % (n, "/".join(x or ["-"]), "/".join(y or ["-"])) for n, (x, y) in self.regs.items())


def parse_edit(s):
    """-> dict(kind, var, block, amount) for the edit syntaxes of rasim and symkeys."""
    m = re.match(r"(add|sub):(frame|tmp|glob|other|hash)@(.+?)(?::(\d+))?$", s)
    if m:
        return dict(kind="key", sign=1 if m.group(1) == "add" else -1, cls=m.group(2), var=m.group(3),
                    n=int(m.group(4) or 1), text=s)
    f = s.split(":")
    k = f[0]
    if k == "P" and len(f) == 3:
        return dict(kind="P", block=int(f[1]), n=int(f[2]), text=s)
    if k in ("ref", "unref", "drop", "extend", "var") and len(f) >= 3 and re.match(r"\d+$", f[2]):
        return dict(kind=k, var=f[1].split("@p")[0].split("#")[0], block=int(f[2]),
                    cost=int(f[3]) if len(f) > 3 and re.match(r"-?\d+$", f[3]) else None, text=s)
    if k in ("before", "after", "interf", "nointerf", "copy", "nocopy", "pref", "noreg", "canreg") and len(f) >= 3:
        return dict(kind=k, var=f[1].split("@p")[0].split("#")[0], other=f[2].split("@p")[0].split("#")[0],
                    n=int(f[3]) if len(f) > 3 and re.match(r"-?\d+$", f[3]) else 1, text=s)
    if k in ("drop", "ins") and len(f) == 2 and f[1].isdigit():
        return dict(kind="scratch_" + k, n=int(f[1]), text=s)
    raise SystemExit("cannot parse edit %r" % s)


def verdict(want, d):
    """(exact | yes | no, the side effects as strings) for one requested edit against a measured Delta."""
    k = want["kind"]
    hit, rest = [], []
    for e in d.edits:
        kind, var, blk, amt = e
        mine = False
        if k in ("ref", "extend") and kind in ("ref", "extend") and var == want["var"] and blk == want["block"] \
                and (amt or 0) > 0:
            mine = kind == k or k == "ref"
        elif k == "unref" and var == want["var"] and blk == want["block"] and (
                kind == "unref" or (kind == "ref" and amt < 0)):
            mine = True
        elif k == "drop" and kind == "drop" and var == want["var"] and blk == want["block"]:
            mine = True
        elif k == "var" and kind in ("var", "imm+") and want["block"] in blk:
            mine = True
        elif k == "P" and want["n"] > 0 and kind in ("var", "imm+") and blk == [want["block"]]:
            mine = True
        elif k == "P" and want["n"] > 0 and kind in ("ref", "extend") and blk == want["block"] and (amt or 0) > 0 \
                and d.a.state.get(var, {}).get(blk) is None:
            mine = True
        elif k == "P" and want["n"] < 0 and (kind in ("gone", "imm-") and blk == [want["block"]]
                                               or kind in ("unref", "drop") and blk == want["block"]):
            mine = True
        (hit if mine else rest).append(e)
    side = Delta.strings(_Sub(d, rest), through=False)
    if k == "P":
        ok = d.P.get(want["block"], 0) == want["n"]
        if ok and not hit and d.tmp.get(want["block"]):
            hit = [1]                 # an anonymous temporary did it
        side = [s for s in side if s != "P:%d:%+d" % (want["block"], want["n"])
                and not (s.startswith("tmp:%d:" % want["block"]))]
    elif k in ("ref", "extend", "var", "unref", "drop"):
        ok = bool(hit)
        dp = d.P.get(want["block"], 0)
        implied = (1 if k in ("ref", "extend", "var") else -1) if dp else 0
        side = [s for s in side if s != "P:%d:%+d" % (want["block"], implied)]
    elif k in ("interf", "nointerf", "copy", "nocopy"):
        pair = tuple(sorted((want["var"], want["other"])))
        lst = {"interf": d.interf[0], "nointerf": d.interf[1], "copy": d.copies[0], "nocopy": d.copies[1]}[k]
        ok = pair in lst
        side = [s for s in side if s != "%s:%s:%s" % ((k,) + pair)]
    elif k == "pref":
        ok = d.prefs.get((want["var"], want["other"]), 0) * want["n"] > 0
        side = [s for s in side if not s.startswith("pref:%s:%s:" % (want["var"], want["other"]))]
    else:
        ok = not d.empty()
    if not ok:
        return "no", side
    return ("exact" if not side else "yes"), side


class _Sub:
    """A Delta restricted to some of its edits (for printing side effects)."""

    def __init__(self, d, edits):
        self.edits, self.tmp, self.P, self.lost, self.new = edits, d.tmp, d.P, d.lost, d.new
        self.interf, self.copies, self.prefs = d.interf, d.copies, d.prefs


# ---------------------------------------------------------------- symbol keys

def key_delta(src_a, src_b, func_a, func_b, cl, want=None):
    """Symbol-key difference of two traced functions: allocation counts per class, named symbols whose key moved,
    and (with a requested add:/sub: edit) whether symkeys' replay of base + edit gives the variant's keys."""
    import symkeys
    fa = [f for f in symkeys.trace(src_a, cl) if f.name and c2trace.func_match(func_a, f.name)]
    fb = fa if (src_b == src_a and func_b == func_a) else \
        [f for f in symkeys.trace(src_b, cl) if f.name and c2trace.func_match(func_b, f.name)]
    if src_a == src_b:
        allf = symkeys.trace(src_a, cl)
        fa = [f for f in allf if f.name and c2trace.func_match(func_a, f.name)]
        fb = [f for f in allf if f.name and c2trace.func_match(func_b, f.name)]
    if not fa or not fb:
        return None
    return key_delta_f(fa[0], fb[0], want)


def key_delta_f(a, b, want=None):
    import symkeys
    for f in (a, b):
        symkeys.name_syms(f)

    def counts(f):
        n = {}
        for s in f.syms:
            if f.optidx is None or s.idx < f.optidx:
                n[s.cls] = n.get(s.cls, 0) + 1
        return n
    ca, cb = counts(a), counts(b)
    na, nb = symkeys.stable_names(a), symkeys.stable_names(b)
    kb = {nb[id(s)]: s.key for s in b.syms if id(s) in nb}
    ka = {na[id(s)]: s.key for s in a.syms if id(s) in na}
    moved = [(n, ka[n], kb[n]) for n in ka if n in kb and ka[n] != kb[n]]
    res = dict(counts={c: cb.get(c, 0) - ca.get(c, 0) for c in symkeys.CLASSES if cb.get(c, 0) != ca.get(c, 0)},
               moved=moved, new=sorted(set(kb) - set(ka)), lost=sorted(set(ka) - set(kb)), both=len(set(ka) & set(kb)))
    if want:
        try:
            newkey = symkeys.keys_after(a, symkeys.apply_edits(a, [want["text"]]))
            pred = {na[id(s)]: newkey.get(id(s), s.key) for s in a.syms if id(s) in na}
            both = [n for n in pred if n in kb]
            res["pred_ok"] = sum(1 for n in both if pred[n] == kb[n])
            res["pred_n"] = len(both)
            sa, sb = symkeys.sort_sig(a, newkey), symkeys.sort_sig(b)
            common = [k for k in sa if k in sb and set(sa[k][:2]) == set(sb[k][:2])]
            res["sort_ok"] = sum(1 for k in common if sa[k][:2] == sb[k][:2])
            res["sort_n"] = len(common)
        except (SystemExit, KeyError, StopIteration) as e:
            res["pred_err"] = str(e)
    sa0, sb0 = symkeys.sort_sig(a), symkeys.sort_sig(b)
    common = [k for k in sa0 if k in sb0 and set(sa0[k][:2]) == set(sb0[k][:2])]
    res["flips"] = sum(1 for k in common if sa0[k][:2] != sb0[k][:2])
    return res


def key_str(r):
    if r is None:
        return "keys: not traced"
    s = "keys: " + (" ".join("%s%+d" % kv for kv in r["counts"].items()) or "same allocation counts")
    s += "; %d of %d named keys moved" % (len(r["moved"]), r["both"])
    if r["new"]:
        s += "; new %s" % ",".join(n.split("~")[0] for n in r["new"][:6]) + ("..." if len(r["new"]) > 6 else "")
    if "pred_n" in r:
        s += "; replay of the edit predicts %d/%d keys, %d/%d operand orders" % (
            r["pred_ok"], r["pred_n"], r["sort_ok"], r["sort_n"])
    s += "; %d operand orders flipped" % r["flips"]
    return s


# ---------------------------------------------------------------- measure / pairs

def print_delta(tag, d, keys=None, want=None, extra=""):
    v = ""
    if want:
        v, side = verdict(want, d)
        v = "%-5s " % v
    print("%-26s %scode %d (bag %d, regs %d, %+d instr)  %s%s" % (tag, v, d.code, d.bag, d.ident, d.dn, d.summary(), extra))
    if d.regs:
        print("%-26s   registers: %s" % ("", d.regstr()))
    if keys is not None:
        print("%-26s   %s" % ("", key_str(keys)))


def cmd_measure(a, cl):
    ta, tb = Trace(a.src, cl), Trace(a.edits[0], cl)
    fa, fb = ta.func(a.func), tb.func(a.func)
    d = Delta(fa, fb)
    print("%s: %d blocks, %d instructions; variant %d blocks, %d instructions" % (
        fa.name, len(fa.blocks), len(fa.code), len(fb.blocks), len(fb.code)))
    keys = key_delta(a.src, a.edits[0], a.func, a.func, cl) if a.keys else None
    print_delta("variant", d, keys)
    for s in d.strings(True):
        print("    " + s)
    for n, (x, y) in d.prio.items():
        print("    prio %s %s -> %s" % (n, x, y))
    ta.close(), tb.close()


def cmd_pairs(a, cl):
    t = Trace(a.src, cl)
    if t.err:
        sys.exit("compile error: " + t.err)
    fns = t.funcs("")
    by = {}
    for f in fns:
        m = re.search(r"\??_?(\w+?)__(\w+?)(?:@|$)", f.name)
        if m:
            by.setdefault(m.group(1), {})[m.group(2)] = f
    kf = {}
    if a.keys:
        import symkeys
        for f in symkeys.trace(a.src, cl):
            m = re.search(r"\??_?(\w+?)__(\w+?)(?:@|$)", f.name or "")
            if m:
                kf[(m.group(1), m.group(2))] = f
    rx = re.compile(a.only) if a.only else None
    for ctx in by:
        base = by[ctx].get("base")
        if base is None or (rx and not rx.search(ctx)):
            continue
        print("## %s: %d blocks, %d instructions; P per block %s" % (
            ctx, len(base.blocks), len(base.code), " ".join("%d:%d" % (b[0], b[2]) for b in base.blocks)))
        for v, f in by[ctx].items():
            if v == "base":
                continue
            d = Delta(base, f)
            keys = key_delta_f(kf[(ctx, "base")], kf[(ctx, v)]) if (ctx, "base") in kf and (ctx, v) in kf else None
            print_delta("  " + v, d, keys)
            if a.diff and d.ident:
                for ln in difflib.unified_diff(["%s %s" % x for x in base.code], ["%s %s" % x for x in f.code], lineterm="", n=1):
                    print("        " + ln)
    t.close()


# ---------------------------------------------------------------- generators: an abstract edit -> source rewrites

PRELUDE_RZ = ("template<class RzT> struct rz_g { static RzT v; };\n"
              "template<class RzT> __forceinline void rz_keep(RzT x) { rz_g<RzT>::v = x; }\n"
              "template<class RzT> RzT rz_ret();\nextern int rz_ext(int);\n")
SRC_KINDS = ("ref", "unref", "drop", "extend", "var", "P")
# construct name -> (what it realises, measured code cost on 14.00; docs/c2/realize.md has the table)
CONSTRUCTS = {
    "copy": "`T k = V;` before V is next modified, later uses renamed: +2 on V in that block, one more candidate "
            "there, V's range ends at the copy (code-neutral: the copy is coalesced)",
    "uncopy": "the reverse: `T k = V;` removed, k's uses spelled V",
    "headdup": "the statement before an if/else written at the head of both arms (merged back after Color): its "
               "references leave the block of the test and count in each arm",
    "headhoist": "the reverse: a statement that starts both arms hoisted above the test",
    "callee": "a body for a bodiless callee that only reads through the parameter receiving &V: V stays one web "
              "across the call (docs/c2/callee.md); the true pattern is in the original's callee",
    "wrap": "a call argument through a by-value identity helper with a local, or `&x` through a bound reference: "
            "one more candidate in EH functions (frontend.md); replaces a scratch pick",
    "const": "a narrow constant stored through the other signedness; a for-init hoisted or sunk (immcand.md)",
    "guard": "`delete p; p = 0;` guarded or not: the reset store changes block (frontend.md)",
    "helperbind": "`&p->m` in an inline helper (or the function) through a bound reference: one more candidate in "
                  "functions with an EH frame, no code, pick-neutral (frontend.md, tools/helpersweep.py)",
    "store": "`rz_keep(V);`: a real store of V. One instruction of code; shows what the reference would do",
    "pad": "n unused locals with initialisers (one frame key each, no code): exact, but padding",
    "helperlocal": "an expression of the window through a by-value inline helper with a local: +1 frame key per use",
    "resultlocal": "an inline helper that returns an expression, with the result named first: +1 frame key per "
                   "expansion, no code (two locals: +2)",
}


class Ctx:
    """The source, the target's body and the base trace."""

    def __init__(self, a, cl):
        import rwsweep, helpersweep
        self.rw, self.hs = rwsweep, helpersweep
        self.a, self.cl = a, cl
        raw = open(a.src).read()
        hdr = os.path.dirname(os.path.abspath(a.src))
        self.text = re.sub(r'#include "([^"/][^"]*)"', lambda m: '#include "%s"' % os.path.join(hdr, m.group(1))
                           if os.path.exists(os.path.join(hdr, m.group(1))) else m.group(0), raw)
        # the helpers the rewrites use; one the source already defines (a probe file, an earlier variant) is left out
        self.prelude = "".join(
            ln + "\n" for ln in (rwsweep.PRELUDE + PRELUDE_RZ).splitlines()
            if not any(re.search(r"(?:inline|__forceinline)[^;{]*\b%s\s*\(" % nm, self.text)
                       for nm in re.findall(r"\b((?:hs|rz)_\w+)\s*\(", ln)))
        out = a.keep or os.path.join(ROOT, "work", "realize", os.path.splitext(os.path.basename(a.src))[0])
        if c2trace.c2build.table() and not a.keep:
            out += "@" + c2trace.c2build.name()
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out)
        self.out = out
        self.orig = tsweep.orig_instrs(a.asm_vs) if a.asm_vs else None
        self.n = 0
        self.base, self.base_path = self.build("base", [], "")
        self.func = self.base.name
        self.fn, self.cls = find_def(self.text, self.func, helpersweep, rwsweep)
        self.B = rwsweep.Body(self.text, self.fn, self.cls)
        self.line0 = self.text.count("\n", 0, self.fn[4])
        self.wantregs = dict(x.split("=") for x in a.want_regs.split(",")) if a.want_regs else {}
        self.keybase = None

    def variant_text(self, edits, tail):
        t = self.text
        for s_, e_, r in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
            t = t[:s_] + r + t[e_:]
        return self.prelude + t + tail

    def build(self, tag, edits, tail):
        d = os.path.join(self.out, tag)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, tag + ".cpp")
        open(p, "w").write(self.variant_text(edits, tail))
        mute = getattr(sys.stdout, "tl", None)             # a variant that does not compile is just dropped:
        if mute is not None and tag != "base":             # cl's error text is not printed (per thread: _Mute)
            mute.on = True
        try:
            t = Trace(p, self.cl, keep=d)
        finally:
            if mute is not None:
                mute.on = False
        return t.func(getattr(self, "func", None) or c2trace.func_decl(self.a.func)), p

    def lineoff(self, tok):
        return self.text.count("\n", 0, self.B.toks[tok][1]) - self.line0

    def stmts(self, block, slack=0):
        ls = self.base.lines_of(block)
        ls = [x for x in ls if x > 0] or ls
        if not ls:
            return []
        want = set(ls)
        hit = [st for st in self.B.starts if self.lineoff(st) in want]
        if not hit or slack:
            lo, hi = min(ls) - slack, max(ls) + slack
            hit = [st for st in self.B.starts if lo <= self.lineoff(st) <= hi]
        return hit

    def regs_ok(self, f):
        if not self.wantregs:
            return None
        return all(reg in f.regs.get(v, []) and (len(self.base.regs.get(v, [])) != 1 or f.regs.get(v) == [reg])
                   for v, reg in self.wantregs.items())

    def regdiff(self, f):
        return tsweep.regdiff(f.instrs, self.orig) if self.orig is not None else None


def find_def(text, deco, hs, rw):
    """The definition of a decorated name in the source: (helpersweep function tuple, class or None). Handles
    namespaces (`?f@C@ns@@QAE..`), in-class definitions and several classes with a method of the same name."""
    mt = re.match(r"\?\?\$(\w+)@", deco)
    if mt:                                      # a function template instance: `template <> R name<T>(..) {` or the template
        name = mt.group(1)
        for pat in (r"template\s*<\s*>[^;{}()]*?\b%s\s*<[^;{}()]*>\s*\(([^;{}]*)\)\s*(?:const\s*)?\{",
                    r"template\s*<[^;{}()]+>[^;{}()]*?\b%s\s*\(([^;{}]*)\)\s*(?:const\s*)?\{"):
            hits = list(re.finditer(pat % re.escape(name), text))
            if hits:
                d = hits[0]
                k = text.rindex(name, d.start(), d.end())
                return (name, d.group(1), d.end() - 1, hs.match_brace(text, d.end() - 1), k), None
        sys.exit("definition of the template function %s not found in the TU" % name)
    m = re.match(r"\?(\w+)((?:@\w+)*)@@(\w)", deco)
    if not m:
        fn = rw.body_of(text, deco if deco.startswith("?") else deco.lstrip("_").split("@")[0])
        return fn, None
    name, quals, kind = m.group(1), [q for q in m.group(2).split("@") if q], m.group(3)
    cls = quals[0] if quals and kind not in "YZ" else None
    cands = [fn for fn in hs.functions(text) if fn[0] == name]
    if not cands:
        sys.exit("definition of %s not found in the TU" % deco)
    spaces, classes = _spaces(text, hs), _classes(text, hs)

    def score(fn):
        head = text[max(0, fn[4] - 120):fn[2]]
        sc = 0
        if cls and re.search(r"\b%s\s*::\s*%s\s*\(" % (re.escape(cls), re.escape(name)), head):
            sc += 4
        if cls and any(cn == cls and a_ < fn[4] < z_ for cn, a_, z_ in classes):
            sc += 4
        if not cls and not re.search(r"::\s*%s\s*\($" % re.escape(name), text[max(0, fn[4] - 40):fn[4] + len(name) + 1].split("\n")[-1]):
            sc += 1
        ns = [nn for nn, a_, z_ in spaces if a_ < fn[4] < z_]
        return sc + sum(1 for q in quals if q in ns)
    return max(cands, key=score), cls


def _loophead(B, o):
    prev = B.toks[o - 1][0] if o else ""
    if prev == "do":
        return True
    if prev == ")":
        d, k = 0, o - 1
        while k >= 0:
            d += B.toks[k][0] == ")"
            d -= B.toks[k][0] == "("
            if d == 0:
                break
            k -= 1
        return k > 0 and B.toks[k - 1][0] in ("for", "while")
    return False


def _in_loop(B, st):
    o = B.block[st]
    while o:
        if _loophead(B, o):
            return True
        o = B.block[o]
    return False


def _scope_ok(B, st, uses):
    blk = B.block[st]
    return not blk or all(blk < i < B.close.get(blk, 1 << 30) for i in uses)


def g_copy(c, V, sts):
    B = c.B
    if V not in B.vars or V == "this":
        return
    ty, decl = B.vars[V]
    if "&" in ty or "[" in ty:
        return
    if "*" not in ty and not re.search(r"\b(int|long|short|char|float|double|bool|unsigned|signed|size_t|__int64|DWORD|UINT)\b", ty):
        return                                             # a class value: the copy would be a constructor call
    for st in sts:
        if decl >= st or B.toks[st][0] in ("case", "default", "else") or _in_loop(B, st):
            continue
        later = [i for i in B.uses[V] if i >= st]
        if not later or not any(B.written(i, i + 1) for i in later) or not _scope_ok(B, st, later):
            continue
        if decl >= 0 and not any(i < st for i in B.uses[V]):
            dm = c.rw.DECL.match(B.text, B.toks[max(s for s in B.starts if s <= decl)][1])
            if not dm or dm.group(3) != "=":
                continue                                   # not assigned yet
        pos = B.toks[st][1]
        yield ("copy", "%s rz_c = %s; at line +%d, %d later uses renamed" % (ty, V, c.lineoff(st), len(later)),
               [(pos, pos, "%s rz_c = %s; " % (re.sub(r"\bconst\s+(?=\w+$)|\s*\bconst$", "", ty), V))] +
               [(B.toks[i][1], B.toks[i][2], "rz_c") for i in later], "")


def g_uncopy(c, V, sts):
    B = c.B
    for st in sts:
        m = c.rw.DECL.match(B.text, B.toks[st][1])
        if not m or m.group(3) != "=":
            continue
        k = m.group(2)
        mm = re.match(r"\s*([A-Za-z_]\w*)\s*;", B.text[m.end():m.end() + 80])
        if not mm or mm.group(1) not in B.vars or mm.group(1) == "this" or k not in B.uses:
            continue
        src = mm.group(1)
        if V and src != V:
            continue
        if any(i > st for i in B.uses[src] if B.toks[i][1] > m.end() + mm.end()):
            continue
        if B.vars[src][0].replace(" ", "") != m.group(1).strip().replace(" ", ""):
            continue
        yield ("uncopy", "`%s %s = %s;` removed at line +%d, %s spelled %s" % (m.group(1).strip(), k, src, c.lineoff(st), k, src),
               [(B.toks[st][1], m.end() + mm.end(), "")] + [(B.toks[i][1], B.toks[i][2], src) for i in B.uses[k]], "")


def _ifelse(B, j):
    """For an `if` at token j: (cond close, then {, then }, else {, else }) or None."""
    if j >= len(B.toks) - 4 or B.toks[j][0] != "if" or B.toks[j + 1][0] != "(":
        return None
    cl_ = c_pclose(B, j + 1)
    if cl_ is None or B.toks[cl_ + 1][0] != "{":
        return None
    e1 = B.close.get(cl_ + 1)
    if e1 is None or e1 + 2 >= len(B.toks) or B.toks[e1 + 1][0] != "else" or B.toks[e1 + 2][0] != "{":
        return None
    return cl_, cl_ + 1, e1, e1 + 2, B.close.get(e1 + 2)


def c_pclose(B, i):
    d = 0
    for k in range(i, len(B.toks)):
        d += B.toks[k][0] == "("
        d -= B.toks[k][0] == ")"
        if d == 0:
            return k
    return None


def _cond_safe(B, i, j, a, z, rw):
    """May the simple statement [i, j) move across the evaluation of the condition tokens (a, z)?"""
    cond = [B.toks[k][0] for k in range(a + 1, z)]
    st = [B.toks[k][0] for k in range(i, j)]
    plain = not any(t in ("->", "[", "*", "(", ".", "++", "--") or t in rw.ASSIGN for t in cond)
    eq = next((k for k, t in enumerate(st) if t in rw.ASSIGN), None)
    if eq is None:
        return False
    lhs = st[:eq]
    if len(lhs) == 1:
        return lhs[0] not in cond and ("(" not in st or plain)
    return plain


def g_headdup(c, V, sts, lines):
    B = c.B
    for k in range(len(B.starts)):
        s = c.rw._stmt(B, k)
        if not s:
            continue
        i, j = s
        ie = _ifelse(B, j)
        if not ie or c.rw.DECL.match(B.text, B.toks[i][1]):
            continue
        if V and V not in [B.toks[x][0] for x in range(i, j)]:
            continue
        near = {c.lineoff(i), c.lineoff(ie[1] + 1), c.lineoff(ie[3] + 1)}
        if lines and not (near & lines):
            continue
        if not _cond_safe(B, i, j, j + 1, ie[0], c.rw):
            continue
        txt = B.text[B.toks[i][1]:B.toks[j - 1][2]]
        yield ("headdup", "`%s` (line +%d) into both arms of the if that follows" % (txt[:40], c.lineoff(i)),
               [(B.toks[i][1], B.toks[j - 1][2], ""), (B.toks[ie[1]][2], B.toks[ie[1]][2], " " + txt),
                (B.toks[ie[3]][2], B.toks[ie[3]][2], " " + txt)], "")


def g_headhoist(c, V, sts, lines):
    B = c.B
    for j in range(len(B.toks)):
        ie = _ifelse(B, j)
        if not ie or j not in B.starts:
            continue
        k1 = B.starts.index(ie[1] + 1) if ie[1] + 1 in B.starts else None
        k2 = B.starts.index(ie[3] + 1) if ie[3] + 1 in B.starts else None
        if k1 is None or k2 is None:
            continue
        s1, s2 = c.rw._stmt(B, k1), c.rw._stmt(B, k2)
        if not s1 or not s2:
            continue
        t1, t2 = B.text[B.toks[s1[0]][1]:B.toks[s1[1] - 1][2]], B.text[B.toks[s2[0]][1]:B.toks[s2[1] - 1][2]]
        if re.sub(r"\s+", "", t1) != re.sub(r"\s+", "", t2) or c.rw.DECL.match(B.text, B.toks[s1[0]][1]):
            continue
        if V and V not in [B.toks[x][0] for x in range(s1[0], s1[1])]:
            continue
        if lines and not ({c.lineoff(j), c.lineoff(s1[0]), c.lineoff(s2[0])} & lines):
            continue
        if not _cond_safe(B, s1[0], s1[1], j + 1, ie[0], c.rw):
            continue
        yield ("headhoist", "`%s` (lines +%d, +%d) hoisted above the if" % (t1[:40], c.lineoff(s1[0]), c.lineoff(s2[0])),
               [(B.toks[j][1], B.toks[j][1], t1 + " "), (B.toks[s1[0]][1], B.toks[s1[1] - 1][2], ""),
                (B.toks[s2[0]][1], B.toks[s2[1] - 1][2], "")], "")


def g_store(c, V, sts):
    B = c.B
    if V not in B.vars:
        return
    ty, decl = B.vars[V]
    ok = [st for st in sts if decl < st and B.toks[st][0] not in ("case", "default", "else")
          and (decl < 0 or any(i < st for i in B.uses[V])) and _scope_ok(B, st, []) and
          (decl < 0 or not B.block[decl] or B.block[decl] < st < B.close.get(B.block[decl], 1 << 30))]
    for st in ok[:1] + ok[-1:] if len(ok) > 1 else ok:
        pos = B.toks[st][1]
        yield ("store", "rz_keep(%s); at line +%d" % (V, c.lineoff(st)), [(pos, pos, "rz_keep(%s); " % V)], "")


G_OPEN = re.compile(r"delete\s*(\[\s*\])?\s*((?:\([^()]*\)\s*)?)([\w.>\-\[\]]+?)\s*;(\s*)([\w.>\-\[\]]+?)\s*=\s*(0|NULL)\s*;")
G_SHUT = re.compile(r"if\s*\(\s*([\w.>\-\[\]]+?)\s*(?:!=\s*(?:0|NULL)\s*)?\)\s*\{\s*delete\s*(\[\s*\])?\s*((?:\([^()]*\)\s*)?)"
                    r"([\w.>\-\[\]]+?)\s*;\s*([\w.>\-\[\]]+?)\s*=\s*(0|NULL)\s*;\s*\}")


def g_guard(c, lines):
    """`delete p; p = 0;` <-> `if (p) { delete p; p = 0; }` (rwsweep's guard family, also with a cast on the
    deleted pointer: `delete[] (T*)p; p = 0;`). Newlines inside the rewritten text are kept, so line numbers hold."""
    B = c.B
    body = B.text[B.b:B.e]
    shut = [m.span() for m in G_SHUT.finditer(body)]
    keepnl = lambda old, new: new + "\n" * old.count("\n")
    for m in G_SHUT.finditer(body):
        if m.group(1) == m.group(4) == m.group(5):
            ln = c.text.count("\n", 0, B.b + m.start()) - c.line0
            if lines and not ({ln, ln + 1, ln + 2} & lines):
                continue
            yield ("guard", "unguard delete%s %s at line +%d" % ("[]" if m.group(2) else "", m.group(1), ln),
                   [(B.b + m.start(), B.b + m.end(), keepnl(m.group(0), "delete%s %s%s; %s = %s;" % (
                       "[]" if m.group(2) else "", m.group(3), m.group(4), m.group(5), m.group(6))))], "")
    for m in G_OPEN.finditer(body):
        if any(a_ <= m.start() < z_ for a_, z_ in shut) or m.group(3) != m.group(5):
            continue
        ln = c.text.count("\n", 0, B.b + m.start()) - c.line0
        if lines and not ({ln, ln + 1} & lines):
            continue
        x = m.group(3)
        yield ("guard", "guard delete%s %s at line +%d" % ("[]" if m.group(1) else "", x, ln),
               [(B.b + m.start(), B.b + m.end(), keepnl(m.group(0), "if (%s) { delete%s %s%s; %s = %s; }" % (
                   x, "[]" if m.group(1) else "", m.group(2), x, x, m.group(6))))], "")


def g_helperbind(c):
    """`&p->m` / `&refparam` in the inline helpers the target uses (and in the target) bound through a reference:
    in a function with an EH frame the binding survives inlining as a no-code candidate (frontend.md; this is
    tools/helpersweep.py's rewrite)."""
    fns = c.hs.functions(c.text)
    target = next((f for f in fns if f[2] == c.fn[2]), c.fn)
    seen = set()
    for h in [target] + c.hs.used_helpers(c.text, target, fns):
        for s_, e_, expr in c.hs.sites_in(c.text, h):
            if (s_, e_) in seen:
                continue
            seen.add((s_, e_))
            yield ("helperbind", "&hs_bind(%s) in %s" % (expr[:40], h[0]), [(s_, e_, "&hs_bind(%s)" % expr)], "")


def g_rw(c, fam, lines, pick=None):
    """A family of tools/rwsweep.py, restricted to rewrites that touch the block's lines."""
    B = c.B
    for f, desc, edits in c.rw.FAMILIES[fam](B):
        if pick and not pick(desc):
            continue
        ls = {c.text.count("\n", 0, e[0]) - c.line0 for e in edits}
        if lines and not (ls & lines):
            continue
        yield (fam, desc, edits, "")


def _classes(text, hs):
    out = []
    for m in re.finditer(r"\b(?:class|struct)\s+(\w+)[^;{()]*\{", text):
        out.append((m.group(1), m.end() - 1, hs.match_brace(text, m.end() - 1)))
    return out


def _spaces(text, hs):
    out = []
    for m in re.finditer(r"\bnamespace\s+(\w+)\s*\{", text):
        out.append((m.group(1), m.end() - 1, hs.match_brace(text, m.end() - 1)))
    return out


def g_callee(c, V):
    """Stub bodies for the bodiless callees that receive &V (V None: the address of any local or parameter).
    A declaration means: reads, writes and keeps every pointer argument. A body can differ from it per parameter
    (docs/c2/callee.md): only reads the pointee (V stays one web across the call), or writes it without keeping
    the pointer (V does not escape, so later external calls do not clobber it)."""
    B, text = c.B, c.text
    fns_by = {}
    for f in c.hs.functions(text):
        fns_by.setdefault(f[0], []).append(f)
    spaces, classes = _spaces(text, c.hs), _classes(text, c.hs)

    def has_body(name, owner, ns):
        """Is `owner::name` (or the free function) defined in the same namespace as its declaration?"""
        for f in fns_by.get(name, []):
            if [nn for nn, a_, z_ in sorted(spaces, key=lambda x: x[1]) if a_ < f[4] < z_] != ns:
                continue
            incls = [cn for cn, a_, z_ in classes if a_ < f[4] < z_]
            qual = re.search(r"(\w+)\s*::\s*%s\s*\($" % re.escape(name), text[max(0, f[4] - 4):f[4] + 200].split("(")[0] + "(")
            if owner and ((qual and qual.group(1) == owner) or (not qual and incls[-1:] == [owner])):
                return True
            if not owner and not qual and not incls:
                return True
        return False
    sites = {}                       # callee -> dict(m, owner, sig, names, params, ptr, ret, hits={arg index: var})
    for i, (t, s, e) in enumerate(B.toks[:-1]):
        if not re.match(r"[A-Za-z_]", t) or t in c.rw.KEYWORDS or B.toks[i + 1][0] != "(":
            continue
        cl_ = c_pclose(B, i + 1)
        if cl_ is None:
            continue
        args, d, a0 = [], 0, i + 2
        for k in range(i + 1, cl_ + 1):
            x = B.toks[k][0]
            d += x in ("(", "[")
            d -= x in (")", "]")
            if (x == "," and d == 1) or d == 0:
                args.append([B.toks[q][0] for q in range(a0, k)])
                a0 = k + 1
        hits = {n: ar[1] for n, ar in enumerate(args) if len(ar) == 2 and ar[0] == "&" and ar[1] in B.vars
                and (V is None or ar[1] == V)}
        byref = {n: ar[0] for n, ar in enumerate(args) if len(ar) == 1 and ar[0] in B.vars and ar[0] != "this"
                 and (V is None or ar[0] == V)}
        obj = i > 1 and B.toks[i - 1][0] in (".", "->") and B.toks[i - 2][0] in B.vars and B.toks[i - 1][0] == "."
        if obj and (V is None or B.toks[i - 2][0] == V):
            hits[-1] = B.toks[i - 2][0]            # v.method(..): `this` of the callee is &v
        if not hits and not byref:
            continue
        if t in sites:
            sites[t]["hits"].update(hits)
            sites[t]["hits"].update({n: v for n, v in byref.items() if "&" in sites[t]["params"][n]})
            continue
        decls = [m for m in re.finditer(r"^[ \t]*((?:__declspec\(\w+\)\s*)*(?:[\w:<>]+[ \t\*&]+)+)%s\s*\(([^;{}()]*?)\)\s*(const)?\s*(?:throw\s*\(\s*\))?\s*;"
                                        % re.escape(t), text, re.M)
                 if not re.search(r"[-+/=.]|^\s*[&*\d]|,\s*[&*\d]", m.group(2))]
        own = lambda m: [cn for cn, a_, z_ in _classes(text, c.hs) if a_ < m.start() < z_]
        member = i > 0 and B.toks[i - 1][0] in (".", "->")
        nsof = lambda pos: [nn for nn, a_, z_ in _spaces(text, c.hs) if a_ < pos < z_]
        myns = nsof(c.fn[4])
        decls.sort(key=lambda m: (nsof(m.start()) != myns,
                                  0 if (own(m)[-1:] == [c.cls] and not member) else 1 if bool(own(m)) == member else 2))
        for m in decls:
            ret = re.sub(r"__declspec\(\w+\)|\b(virtual|static|inline|extern|__forceinline)\b", "", m.group(1)).strip()
            if not ret or ret.split()[0] in ("return", "delete", "new", "else", "case", "goto", "typedef", "friend"):
                continue
            params = [p.strip() for p in m.group(2).split(",") if p.strip() and p.strip() != "void"]
            if len(params) != len(args):
                continue
            names, sig = [], []
            for n, p in enumerate(params):
                p = re.sub(r"\s*=\s*[^,]+$", "", p)
                pm = re.match(r"(.*?[\s\*&])(\w+)$", p)
                if pm and pm.group(1).strip() and pm.group(2) not in ("int", "char", "long", "short", "float", "double", "unsigned", "const"):
                    names.append(pm.group(2))
                    sig.append(p)
                else:
                    names.append("rz_a%d" % n)
                    sig.append(p + " rz_a%d" % n)
            hits.update({n: v for n, v in byref.items() if "&" in params[n]})   # a local bound to a reference parameter
            if not hits:
                break
            o = own(m)
            ns = [(a_, nn) for nn, a_, z_ in spaces if a_ < m.start() < z_]
            if has_body(t, o[-1] if o else None, [nn for _a, nn in sorted(ns)]):
                break
            sites[t] = dict(ns=[nn for _a, nn in sorted(ns)], name=t, owner=o[-1] if o else None, sig=sig, names=names, params=params, ret=ret,
                            const=bool(m.group(3)), hits=hits, virt="virtual" in m.group(1),
                            ptr=[n for n, p in enumerate(params) if "*" in p or "&" in p] + ([-1] if o else []))
            break

    def stub(si, ro):
        ref = lambda n: "this" if n < 0 else ("&" if "&" in si["params"][n] and "*" not in si["params"][n] else "") + si["names"][n]
        body = ["    int rz = 0;"] + ["    rz += *(char*)(void*)%s;" % ref(n) for n in si["ptr"]]
        body += ["    *(char*)(void*)%s = (char)rz;" % ref(n) for n in si["ptr"] if n not in ro and not (n < 0 and si["const"])]
        body.append("    rz_ext(rz);")
        if si["ret"] != "void":
            body.append("    return %s;" % ("*this" if si["ret"].endswith("&") and si["owner"] and si["owner"] in si["ret"]
                                           else "rz_ret<%s >()" % si["ret"]))
        return "\n%s__declspec(noinline) %s %s%s(%s)%s\n{\n%s\n}\n%s" % (
            "".join("namespace %s { " % nn for nn in si["ns"]) + ("\n" if si["ns"] else ""),
            si["ret"], si["owner"] + "::" if si["owner"] else "", si["name"], ", ".join(si["sig"]),
            " const" if si["const"] else "", "\n".join(body), "}" * len(si["ns"]) + ("\n" if si["ns"] else ""))
    sl = [si for si in sites.values() if not si["virt"] and si.get("hits")]
    for si in sl:
        for h, var in sorted(si["hits"].items()):
            yield ("callee", "body for %s: only reads through %s (= &%s), writes the other pointees, keeps none" % (
                si["name"], "this" if h < 0 else si["names"][h], var), [], stub(si, {h}))
        yield ("callee", "body for %s: reads and writes every pointee, keeps none" % si["name"], [], stub(si, set()))
        if len(si["ptr"]) > 1:
            yield ("callee", "body for %s: reads every pointee, writes none" % si["name"], [], stub(si, set(si["ptr"])))
    if len(sl) > 1:
        yield ("callee", "bodies for %s: each reads and writes every pointee, keeps none" % ", ".join(si["name"] for si in sl),
               [], "".join(stub(si, set()) for si in sl))
        yield ("callee", "bodies for %s: each only reads through the parameter that receives the local's address" % ", ".join(si["name"] for si in sl),
               [], "".join(stub(si, set(si["hits"])) for si in sl))


def generate(c, w):
    """Candidate rewrites for one parsed edit, most specific construct first."""
    k = w["kind"]
    out = []
    if k in ("ref", "extend", "var", "unref", "drop", "P"):
        blk = w["block"]
        sts = c.stmts(blk)
        lines = set(c.base.lines_of(blk))
        V = w.get("var") if k != "var" else None
        up = k in ("ref", "extend", "var") or (k == "P" and w["n"] > 0)
        if up:
            if k == "extend" and V:
                out += list(g_callee(c, V))
            for v in ([V] if V else [x for x in c.B.vars if x != "this"]):
                out += list(g_copy(c, v, sts))
            out += list(g_headhoist(c, V, sts, lines)) + list(g_headdup(c, V, sts, lines))
            if not V:
                out += list(g_rw(c, "const", lines)) + list(g_guard(c, lines))
                out += list(g_rw(c, "wrap", lines, lambda d: d.startswith("arg hs_cp") or d.startswith("hs_bind")))
            if V:
                out += list(g_store(c, V, sts or c.stmts(blk, 1)))
        else:
            out += list(g_uncopy(c, V, sts or c.stmts(blk, 1)))
            out += list(g_headdup(c, V, sts, lines)) + list(g_headhoist(c, V, sts, lines))
            if not V or k == "P":
                out += list(g_rw(c, "const", lines)) + list(g_guard(c, lines))
    elif k == "key":
        out += list(g_keys(c, w))
    pick = set(c.a.constructs.split(",")) if c.a.constructs else None
    out = [v for v in out if not pick or v[0] in pick]
    return out[:c.a.limit] if c.a.limit else out


def g_keys(c, w):
    """add:frame@V:n: n frame keys before V's first reference."""
    import symkeys
    B = c.B
    if c.keybase is None:
        if c.a.keys_from:                    # a `symkeys.py export` of the same source: no second traced compile
            fs = [f for f in symkeys.from_json(json.load(open(c.a.keys_from))) if f.name == c.func]
        else:
            fs = [f for f in symkeys.trace(c.base_path, c.cl) if f.name == c.func]
        if not fs:
            return
        c.keybase = fs[0]
        symkeys.name_syms(c.keybase)
    if w["cls"] == "hash":
        print("  hash class (optimiser value-numbering temps, only key & 3 counts): no construct is catalogued. Dead "
              "expressions make none (removed before value numbering); the known source is a loop the compiler "
              "generates at that point, e.g. the constructor loop of a class array declared there "
              "(docs/c2/realize.md 2.6, work/scratch/f3/keywall/RESULTS.md)")
        return
    try:
        sym = symkeys.find(c.keybase, w["var"])
    except SystemExit:
        print("  no symbol %s in the base trace (names as `symkeys.py keys` prints them)" % w["var"])
        return
    line = sym.line - 1                 # symkeys counts the function's first line as 1, the regs trace as 0
    ok = lambda st: B.toks[st][0] not in ("case", "default", "else")
    nm = w["var"].lstrip("_")
    first = min(B.uses.get(nm) or [1 << 30]) if not sym.inl else 1 << 30
    if nm in B.vars and B.vars[nm][1] >= 0:
        first = min(first, B.vars[nm][1])
    if first < 1 << 30:                 # a user variable of the function: the statement of its first mention
        cand = [max(st for st in B.starts if st <= first and ok(st))]
    else:                               # a temporary, a field view, a symbol of an inline expansion: by line
        before = [st for st in B.starts if c.lineoff(st) <= line and ok(st)]
        inblk = [st for st in B.starts if c.lineoff(st) == line and ok(st)]
        cand = (inblk[:1] or before[-1:])
    if w["cls"] != "frame" or w["sign"] < 0:
        return
    n = w["n"]
    if sym.inl:
        # The anchor was made inside an inline expansion (`_r@Cls::Helper#2`). Its line is the callee's, so there
        # is no statement of the target to map it to. Two places can carry the keys, and they are not the same
        # edit: (a) the callee's body: every expansion gets them, so expansion #k sees k times the per-body
        # count (the expansions before it included); (b) the call site in the target: reader-phase keys, made
        # before every inline expansion, which also shift whatever is allocated between the site and the anchor.
        hm = re.match(r"(?:(.*)::)?(~?\w+)#(\d+)$", symkeys.short(sym.inl))
        if hm:
            hname, k = hm.group(2), int(hm.group(3))
            fns = [f for f in c.hs.functions(c.text) if f[0] == hname]
            if hm.group(1):
                own = hm.group(1).split("::")[-1]
                fns = [f for f in fns if re.search(r"\b%s\s*::\s*%s\s*\($" % (re.escape(own), re.escape(hname)),
                                                   c.text[max(0, f[4] - 2):f[4] + 200].split("(")[0] + "(")
                       or any(cn == own and a_ < f[4] < z_ for cn, a_, z_ in _classes(c.text, c.hs))] or fns
            for f in fns[:1]:
                for per in sorted({n // k if n % k == 0 else 0, n} - {0}):
                    yield ("pad", "in the callee %s's body: %d unused initialised locals (every expansion gets them: #%d "
                           "then sees %d before its own symbols%s)" % (hname, per, k, per * k,
                                                                      "" if per * k == n else ", not the %d asked" % n),
                           [(f[2] + 1, f[2] + 1, " int " + ", ".join("rz_p%d = 0" % q for q in range(per)) + ";")], "")
            calls = [i for i, t in enumerate(B.toks[:-1]) if t[0] == hname and B.toks[i + 1][0] == "("]
            if len(calls) >= k:
                st = max((x for x in B.starts if x <= calls[k - 1] and ok(x)), default=None)
                if st is not None and not B.paren[st]:
                    pos = B.toks[st][1]
                    yield ("pad", "at the call site of %s #%d (line +%d, textual order; reader-phase keys, before every "
                           "inline expansion): %d unused initialised locals" % (hname, k, c.lineoff(st), n),
                           [(pos, pos, "int " + ", ".join("rz_p%d = 0" % q for q in range(n)) + "; ")], "")
        cand = []
    for st in cand:
        pos = B.toks[st][1]
        n = w["n"]
        yield ("pad", "%d unused locals with initialisers at line +%d" % (n, c.lineoff(st)),
               [(pos, pos, "int " + ", ".join("rz_p%d = 0" % q for q in range(n)) + "; ")], "")
        yield ("pad", "%d unused locals in a nested scope at line +%d" % (n, c.lineoff(st)),
               [(pos, pos, "{ int " + ", ".join("rz_p%d = 0" % q for q in range(n)) + "; } ")], "")
    # an inline helper that only returns an expression, written with a named result: one frame key per expansion
    # (two with a second local). It counts for V only through the expansions made before V's own allocation.
    fns = c.hs.functions(c.text)
    target = next((f for f in fns if f[2] == c.fn[2]), c.fn)
    for h in c.hs.used_helpers(c.text, target, fns):
        m = re.fullmatch(r"\{\s*return\s+([^;{}]+);\s*\}", c.text[h[2]:h[3]])
        rt = re.search(r"\b((?:const\s+)?(?:unsigned\s+|signed\s+)?(?:float|double|int|long|short|char|bool)(?:\s*\*)?)\s*$",
                       c.text[max(0, h[4] - 80):h[4]])
        if not m or not rt or h is target:
            continue
        T, E = rt.group(1), m.group(1).strip()
        yield ("resultlocal", "%s: `%s rz_r = ...; return rz_r;` (+1 frame key per expansion)" % (h[0], T),
               [(h[2], h[3], "{ %s rz_r = %s; return rz_r; }" % (T, E))], "")
        yield ("resultlocal", "%s: the result through two locals (+2 per expansion)" % h[0],
               [(h[2], h[3], "{ %s rz_r = %s; %s rz_s = rz_r; return rz_s; }" % (T, E, T))], "")
    # natural units: call arguments of the statements just before V's first reference through hs_cp (one helper
    # local each). Their sum is searched for n.
    units = []
    lo = line - 12
    for f, desc, edits in c.rw.gen_wrap(B):
        if not desc.startswith("arg hs_cp"):
            continue
        ln = c.text.count("\n", 0, edits[0][0]) - c.line0
        if lo <= ln <= line:
            units.append((ln, desc, edits))
    units.sort(key=lambda u: -u[0])
    if len(units) >= w["n"]:
        take = units[:w["n"]]
        if not any(c.rw.overlap(x[2], y[2]) for i_, x in enumerate(take) for y in take[i_ + 1:]):
            yield ("helperlocal", "%d call arguments through hs_cp on lines +%d..+%d" % (w["n"], take[-1][0], take[0][0]),
                   [e for u in take for e in u[2]], "")


# ---------------------------------------------------------------- run: measure every candidate

def measure(c, cands, want, tag0):
    def one(k):
        fam, desc, edits, tail = cands[k]
        tag = "%s%03d" % (tag0, k)
        try:
            f, path = c.build(tag, edits, tail)
        except (Exception, SystemExit) as e:
            return dict(k=k, err=str(e)[-160:], fam=fam, desc=desc)
        d = Delta(c.base, f)
        r = dict(k=k, fam=fam, desc=desc, d=d, f=f, path=path, regs=c.regs_ok(f), rd=c.regdiff(f))
        if want["kind"] == "key":
            import symkeys
            fs = [x for x in symkeys.trace(path, c.cl) if x.name == c.func]
            r["keys"] = key_delta_f(c.keybase, fs[0], want) if fs else None
            kd = r["keys"]
            ok = kd and kd.get("pred_n") and kd["pred_ok"] == kd["pred_n"]
            r["v"], r["side"] = ("exact" if ok and kd["sort_ok"] == kd["sort_n"] else "yes" if ok else "no"), []
        else:
            r["v"], r["side"] = verdict(want, d)
        return r
    with ThreadPoolExecutor(c.a.jobs) as ex:
        res = list(ex.map(one, range(len(cands))))
    order = {"exact": 0, "yes": 1, "no": 2}
    good = [r for r in res if "err" not in r]
    good.sort(key=lambda r: (order[r["v"]], r["d"].bag, r["d"].code, len(r["side"]), r["k"]))
    return good, [r for r in res if "err" in r]


def show(c, r, n=None):
    d = r["d"]
    extra = ""
    if r["regs"] is not None:
        extra += "  wanted registers: %s" % ("YES" if r["regs"] else "no")
    if r["rd"] is not None:
        extra += "  regdiff %d" % r["rd"]
    print("  %-5s %-9s code %d bag %d regs %d  %s%s" % (r["v"], r["fam"], d.code, d.bag, d.ident, r["desc"][:90], extra))
    side = r["side"] if r["v"] != "no" else d.strings()
    if side:
        s = " ".join(side)
        print("        %s %s" % ("also:" if r["v"] != "no" else "did:", s if len(s) < 200 else s[:196] + " ..."))
    if d.regs:
        print("        registers: %s" % d.regstr())
    if r.get("keys") is not None:
        print("        %s" % key_str(r["keys"]))
    print("        %s" % os.path.relpath(r["path"], ROOT))


def hit(c, r):
    if r["rd"] is not None:
        return r["rd"] == 0
    return bool(r["regs"]) and r["d"].bag == 0


def cmd_edit(c, a):
    print("%s: %d blocks, %d instructions%s" % (c.func, len(c.base.blocks), len(c.base.code),
                                               "" if c.orig is None else ", regdiff %d" % c.regdiff(c.base)))
    best = []
    for n, e in enumerate(a.edits):
        w = parse_edit(e)
        if w["kind"] in SRC_KINDS:
            ls = c.base.lines_of(w["block"])
            print("## %s   (block %d = source lines +%s, P=%d)" % (e, w["block"], ",".join(map(str, ls)) or "?",
                                                                c.base.blocks[w["block"] - 1][2] if 0 < w["block"] <= len(c.base.blocks) else -1))
        else:
            print("## %s" % e)
        cands = generate(c, w)
        if not cands:
            print("  no catalogued construct applies here (docs/c2/realize.md, section 2, lists what each kind needs)")
            continue
        if a.list:
            for fam, desc, _e, _t in cands:
                print("  %-9s %s" % (fam, desc))
            continue
        good, bad = measure(c, cands, w, "e%d_" % n)
        print("  %d candidates, %d compiled; %d realise the edit, %d of them with the same instructions" % (
            len(cands), len(good), sum(1 for r in good if r["v"] != "no"),
            sum(1 for r in good if r["v"] != "no" and r["d"].bag == 0)))
        for r in good[:a.top]:
            show(c, r)
        if good and good[0]["v"] != "no":
            best.append(cands[good[0]["k"]])
    if len(best) > 1 and not any(c.rw.overlap(x[2], y[2]) for i_, x in enumerate(best) for y in best[i_ + 1:] if x[2] and y[2]):
        print("## all %d edits together (the best realisation of each)" % len(best))
        try:
            f, path = c.build("combined", [e for b in best for e in b[2]], "".join(b[3] for b in best))
            d = Delta(c.base, f)
            print_delta("  combined", d, extra="  %s" % os.path.relpath(path, ROOT))
        except (Exception, SystemExit) as e:
            print("  failed: %s" % str(e)[-160:])
    print("variants: %s" % os.path.relpath(c.out, ROOT))


def parse_answers(text):
    """rasim need / want output -> (wanted registers, [[edit, ..], ..] in the order printed)."""
    wants, answers = {}, []
    pat = re.compile(r"\b((?:ref|unref|drop|extend|var|P|before|after|interf|nointerf|pref|copy|nocopy|noreg|canreg):[^\s,;]+)")
    for ln in text.splitlines():
        m = re.match(r"(\S+) \(pop \d+, prio -?\d+\): now \S+, wanted (\w+)", ln)
        if m:
            wants[m.group(1).split("@p")[0].split("#")[0]] = m.group(2)
            continue
        if not ln.startswith("  ") or ln.startswith("      "):
            continue
        m = re.match(r"\s+deficit\s+(-?\d+)\s+(\S+)", ln)
        if m:
            if int(m.group(1)) <= 0:
                answers.append([m.group(2)])
            continue
        head = re.split(r"\s{2,}", ln.strip())[0]
        eds = pat.findall(head)
        if eds and not ln.strip().startswith("not decidable"):
            answers.append(eds)
    return wants, answers


def auto_answers(c, a):
    """regmap against the original, then `rasim want`: the answer list for --from-rasim auto."""
    if not a.asm_vs:
        sys.exit("--from-rasim auto needs --asm-vs (the original's disassembly) to know which registers are wanted")
    snap = os.path.join(c.out, "base.json")
    json.dump(c.base.snap, open(snap, "w"))
    rm = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "c2trace.py"), "regmap", c.base_path, "--func", c.func,
                         "--asm-vs", a.asm_vs, "--asm-func", c.func, "--all", "--"] + c.cl, capture_output=True, text=True, cwd=ROOT)
    open(os.path.join(c.out, "regmap.txt"), "w").write(rm.stdout)
    r = subprocess.run([RASIM, "want", snap, os.path.join(c.out, "regmap.txt")], capture_output=True, text=True)
    open(os.path.join(c.out, "answers.txt"), "w").write(r.stdout)
    print("# regmap: %s" % (rm.stdout.strip().splitlines() or ["(no output)"])[-1])
    return r.stdout


def cmd_pipeline(c, a):
    if a.from_rasim == "auto":
        text = auto_answers(c, a)
    else:
        text = sys.stdin.read() if a.from_rasim == "-" else open(a.from_rasim).read()
    wants, answers = parse_answers(text)
    if a.regmap:
        for ln in open(a.regmap):
            m = re.match(r"(\S+)\s+(\S+)\s+.*WANTS (\w+)", ln)
            if m:
                wants[m.group(2)] = m.group(3)
    for v, r in wants.items():
        c.wantregs.setdefault(v, r)
    print("%s: %d blocks, %d instructions%s; wanted: %s; %d answers read" % (
        c.func, len(c.base.blocks), len(c.base.code), "" if c.orig is None else ", regdiff %d" % c.regdiff(c.base),
        " ".join("%s=%s" % kv for kv in c.wantregs.items()) or "(none: give --want-regs or --asm-vs)", len(answers)))
    tried, ncomp, closest = 0, 0, None
    seen, nocon, done = set(), [], set()
    for ai, ans in enumerate(answers):
        if tried >= a.max_answers:
            break
        key = tuple(ans)
        if key in seen:
            continue
        seen.add(key)
        ws = [parse_edit(e) for e in ans]
        if any(w["kind"] not in SRC_KINDS for w in ws):
            continue                       # an abstract change: ask rasim for the source-level form (need 'A>B')
        gen = [[v for v in generate(c, w) if v[0] != "store" or a.with_store] for w in ws]
        if not all(gen):
            nocon.append(" + ".join(ans))
            continue
        if ncomp + sum(map(len, gen)) > a.max_compiles:
            print("## stopping at --max-compiles %d" % a.max_compiles)
            break
        tried += 1
        print("## answer %d: %s" % (tried, " + ".join(ans)))
        per = []
        for n, w in enumerate(ws):
            cands = gen[n]
            done.update(repr((sorted(g[2]), g[3])) for g in cands)
            good, bad = measure(c, cands, w, "a%d_%d_" % (tried, n))
            ncomp += len(cands)
            print("  %s: %d candidates, %d realise it (%d with the same instructions)" % (
                w["text"], len(cands), sum(1 for r in good if r["v"] != "no"),
                sum(1 for r in good if r["v"] != "no" and r["d"].bag == 0)))
            for r in good[:3 if len(ws) == 1 else 1]:
                show(c, r)
            per.append((good, cands))
            for r in good:
                if len(ws) == 1 and hit(c, r):
                    print("FOUND after %d answers, %d compiles: %s (%s)" % (tried, ncomp, os.path.relpath(r["path"], ROOT), r["desc"]))
                    if r not in good[:3]:
                        show(c, r)
                    return 0
                sc = (r["rd"] if r["rd"] is not None else (0 if r["regs"] else 1) * 100 + r["d"].bag, r["path"], r["desc"])
                if closest is None or sc < closest:
                    closest = sc
        if per and len(ws) > 1:
            pick = [g[0] for g, _ in per if g and g[0]["v"] != "no"]
            if len(pick) == len(ws):
                ed = [e for r, (g, cands) in zip(pick, per) for e in cands[r["k"]][2]]
                tail = "".join(cands[r["k"]][3] for r, (g, cands) in zip(pick, per))
                try:
                    f, path = c.build("a%d_all" % tried, ed, tail)
                    ncomp += 1
                    r = dict(k=0, fam="+".join(x["fam"] for x in pick), desc="the best realisation of each edit",
                             d=Delta(c.base, f), f=f, path=path, regs=c.regs_ok(f), rd=c.regdiff(f), v="yes", side=[])
                    show(c, r)
                    if hit(c, r):
                        print("FOUND after %d answers, %d compiles: %s" % (tried, ncomp, os.path.relpath(path, ROOT)))
                        return 0
                except (Exception, SystemExit) as e:
                    print("  combination failed: %s" % str(e)[-160:])
    if nocon:
        print("## %d answers have no code-neutral construct in this source (docs/c2/realize.md): %s%s" % (
            len(nocon), ", ".join(nocon[:10]), " ..." if len(nocon) > 10 else ""))
    # second stage: the answers name the blocks that matter. Every structural code-neutral construct that touches
    # one of them is tried on its own (guard, const, head duplication, copies of any variable), then the ones that
    # changed the allocation's inputs without changing an instruction are applied together, per family and all at
    # once: priorities add up (Mount & Blade load_game_data needed all four guards).
    blocks = []
    snap = os.path.join(c.out, "base.json")
    if not os.path.exists(snap):
        json.dump(c.base.snap, open(snap, "w"))
    norder = 0
    for ans in answers:
        if len(ans) == 1 and ans[0].split(":")[0] in ("before", "after") and norder < 3:
            # an order answer: ask the priority model where a reference more or fewer would close the gap
            k, x, y = ans[0].split(":")[:3]
            first, second = (x, y) if k == "before" else (y, x)
            r = subprocess.run([RASIM, "need", snap, "%s>%s" % (first, second)], capture_output=True, text=True)
            got = [int(m.group(1)) for m in re.finditer(r"^\s+deficit\s+-?\d+\s+\w+:[^:\s]+:(\d+)", r.stdout, re.M)]
            norder += 1
            new = [b for b in dict.fromkeys(got) if b not in blocks][:8]
            print("## order answer %s: `rasim need '%s>%s'` names blocks %s" % (ans[0], first, second, ",".join(map(str, new)) or "-"))
            blocks += new
    for ans in answers[:60]:
        for e in ans:
            try:
                w = parse_edit(e)
            except SystemExit:
                continue
            if w["kind"] in SRC_KINDS and w["block"] not in blocks:
                blocks.append(w["block"])
    cands, seen_e = [], set(done)
    for b in blocks[:16]:
        lines, sts = set(c.base.lines_of(b)), c.stmts(b)
        gens = list(g_guard(c, lines)) + list(g_rw(c, "const", lines)) + list(g_headhoist(c, None, sts, lines)) + \
            list(g_headdup(c, None, sts, lines)) + list(g_uncopy(c, None, sts))
        for v in c.B.vars:
            gens += list(g_copy(c, v, sts[:3]))
        for g in gens:
            k = repr((sorted(g[2]), g[3]))
            if k not in seen_e:
                seen_e.add(k)
                cands.append(g)
    for g in list(g_guard(c, None)):
        k = repr((sorted(g[2]), g[3]))
        if k not in seen_e:
            seen_e.add(k)
            cands.insert(0, g)
    hb = list(g_helperbind(c))[:16]
    room = max(0, a.max_compiles - ncomp)
    keep = max(room - len(hb), room // 2 + room % 2)       # the block-directed rewrites first; bindings get the rest
    cands = cands[:keep] + hb[:max(0, room - min(len(cands), keep))]
    if a.constructs:
        cands = [g for g in cands if g[0] in a.constructs.split(",")]
    if cands:
        print("## structural rewrites in the blocks the answers name (%s): %d candidates" % (
            ",".join(map(str, blocks[:16])), len(cands)))
        good, bad = measure(c, cands, dict(kind="any", text="any"), "st_")
        ncomp += len(cands)
        good.sort(key=lambda r: (r["rd"] if r["rd"] is not None else 0, not r["regs"], r["d"].bag, r["d"].empty()))
        for r in good[:4]:
            r["v"], r["side"] = "-", []
            show(c, r)
        for r in good:
            if hit(c, r):
                print("FOUND by a structural rewrite after %d answers, %d compiles: %s (%s)" % (tried, ncomp, os.path.relpath(r["path"], ROOT), r["desc"]))
                return 0
        neutral = [r for r in good if r["d"].bag == 0 and not r["d"].empty()]
        fams = {}
        for r in neutral:
            fams.setdefault(r["fam"], []).append(r)
        sets = [(f, rs) for f, rs in fams.items() if len(rs) > 1] + ([("all", neutral)] if len(fams) > 1 else [])
        # with an original to compare against: the rewrites that each brought the regdiff down, together (a
        # rewrite that moves a store changes code on its own; whether it is right shows only in the regdiff)
        base_rd = c.regdiff(c.base)
        better = [r for r in good if r["rd"] is not None and r["rd"] < base_rd]
        bf = {}
        for r in better:
            bf.setdefault(r["fam"], []).append(r)
        for f, rs in bf.items():
            fam_all = [r for r in good if r["fam"] == f and not r["d"].empty()]
            if len(fam_all) > len(rs):
                sets.append((f + "_every", fam_all))        # every site of a family of which some sites helped
            if len(rs) > 1:
                sets.append((f + "_better", rs))
        if len(bf) > 1:
            sets.append(("better", better))
        for f, rs in sets:
            pick = []
            for r in rs:
                if not any(c.rw.overlap(cands[r["k"]][2], cands[q["k"]][2]) for q in pick):
                    pick.append(r)
            try:
                fn, path = c.build("st_all_" + f, [e for r in pick for e in cands[r["k"]][2]], "".join(cands[r["k"]][3] for r in pick))
            except (Exception, SystemExit) as e:
                print("  %d %s rewrites together: failed (%s)" % (len(pick), f, str(e)[-100:]))
                continue
            ncomp += 1
            r = dict(k=0, fam=f, desc="%d code-neutral %s rewrites together: %s" % (len(pick), f, "; ".join(x["desc"][:40] for x in pick[:4])),
                     d=Delta(c.base, fn), f=fn, path=path, regs=c.regs_ok(fn), rd=c.regdiff(fn), v="-", side=[])
            show(c, r)
            if hit(c, r):
                print("FOUND by %d %s rewrites together after %d answers, %d compiles: %s" % (len(pick), f, tried, ncomp, os.path.relpath(path, ROOT)))
                return 0
            sc = (r["rd"] if r["rd"] is not None else (0 if r["regs"] else 1) * 100 + r["d"].bag, r["path"], r["desc"])
            if closest is None or sc < closest:
                closest = sc
    # last stage, independent of the answers: bodies for the bodiless callees that get a local's address. The model
    # asks for it as `extend:` on that local, or on a value that only meets it (Fallout 3 0x004da900: `extend:o:6`).
    cands = [v for v in g_callee(c, None)]
    if cands and not a.no_callee:
        print("## callee bodies (docs/c2/callee.md): %d stubs for callees that receive a local's address" % len(cands))
        good, bad = measure(c, cands, dict(kind="callee", text="callee"), "cb_")
        ncomp += len(cands)
        good.sort(key=lambda r: (r["rd"] if r["rd"] is not None else 0, not r["regs"], r["d"].bag))
        for r in good[:4]:
            r["v"], r["side"] = "-", []
            show(c, r)
        for r in good:
            if hit(c, r):
                print("FOUND by a callee body after %d answers, %d compiles: %s (%s)" % (tried, ncomp, os.path.relpath(r["path"], ROOT), r["desc"]))
                return 0
            sc = (r["rd"] if r["rd"] is not None else (0 if r["regs"] else 1) * 100 + r["d"].bag, r["path"], r["desc"])
            if closest is None or sc < closest:
                closest = sc
    print("NOT FOUND in %d answers, %d compiles%s" % (tried, ncomp, "" if closest is None else
                                                    "; closest: %s (%s)" % (os.path.relpath(closest[1], ROOT), closest[2])))
    return 1


def run(a, cl):
    c = Ctx(a, cl)
    if a.from_rasim:
        return cmd_pipeline(c, a)
    return cmd_edit(c, a)


def main():
    sys.stdout.reconfigure(line_buffering=True)
    sys.stdout = _Mute(sys.stdout)
    argv = sys.argv[1:]
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("cmd", nargs="?", choices=("edit", "measure", "pairs"), default="edit")
    ap.add_argument("src")
    ap.add_argument("edits", nargs="*")
    ap.add_argument("--func", default="")
    ap.add_argument("--from-rasim")
    ap.add_argument("--want-regs")
    ap.add_argument("--regmap")
    ap.add_argument("--asm-vs")
    ap.add_argument("--keys", action="store_true")
    ap.add_argument("--keys-from", help="edit add:..: a `symkeys.py export` of the base source (saves the keys trace)")
    ap.add_argument("--only")
    ap.add_argument("--diff", action="store_true", help="pairs / measure: print the listing diff of variants whose code changed")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--max-answers", type=int, default=12)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep")
    ap.add_argument("--with-store", action="store_true", help="--from-rasim: also try `rz_keep(V);` (one instruction of code: a diagnosis, never a match)")
    ap.add_argument("--max-compiles", type=int, default=60)
    ap.add_argument("--no-callee", action="store_true", help="--from-rasim: skip the final callee-body stage")
    ap.add_argument("--constructs", help="comma-separated construct names (default: all catalogued for the edit kind)")
    a = ap.parse_intermixed_args(argv)
    if a.cmd == "measure":
        return cmd_measure(a, cl)
    if a.cmd == "pairs":
        return cmd_pairs(a, cl)
    return run(a, cl)


if __name__ == "__main__":
    main()
