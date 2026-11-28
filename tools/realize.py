#!/usr/bin/env python3
"""From an abstract allocation edit to the C++ that realises it.  Doc: docs/c2/realize.md

usage: realize.py edit    <tu.cpp> --func F EDIT [EDIT..] [--want-regs v=reg,..] [--asm-vs ORIG.s] [--top N]
                          [--constructs a,b] [--jobs 6] [--limit N] [--list] [--keep DIR] [-- <cl args>]
       realize.py --from-rasim auto|ANSWERS.txt|- <tu.cpp> --func F [--asm-vs ORIG.s] [--want-regs v=reg,..]
                          [--regmap REGMAP.txt] [--max-answers N] [--max-compiles N] [--with-store] [--no-callee]
                          [-- <cl args>]
       realize.py --from slots|scratch <tu.cpp> --func F --asm-vs ORIG.s [--max-compiles N] [--pairs 0|1|2] [-- <cl args>]
       realize.py measure <base.cpp> <variant.cpp> --func F [--keys] [--slots] [--scratch] [-- <cl args>]
       realize.py pairs   <probes.cpp> [--keys] [--slots] [--scratch] [--only RE] [--diff] [-- <cl args>]
   --c2 13.10 / 13.10.3052 / 15.00 as on every tool. --c2 17.00 (a build table with arch x64): NColor, predicted with
   ncolsim: edits +app:V:B[:N] / -app:V:B[:N] / ref / unref / before:A:B, `--from-ncol --want-regs v=reg`, measure,
   pairs (probe files), and a pair search over ncolsim edits (--pairs, --pair-compiles, --pair-top; docs/c2/ncolor.md
   10, docs/c2/realize.md "cl 17.00 x64").
   --asm-func DECORATED: the function of our listing scored
   against --asm-vs when it is not --func itself (as tsweep's; default --func).

The models answer in abstract edits; this tool goes the other way.

edit     EDIT is one abstract edit in the syntax `rasim need` / `rasim want` print, or symkeys' form:
           ref:VAR:B[:C]    one more reference to VAR in block B          unref:VAR:B    a reference fewer
           extend:VAR:B     VAR's range reaches block B                   drop:VAR:B     the range leaves B
           var:NAME:B[:C]   a new candidate referenced in B               P:B:+1 / -1    one more / fewer candidate in B
           add:frame@V:n    n more frame symbol keys before V's first reference;  sub:CLS@V:n  n fewer (V earlier)
           uses:X=N  pos:X=P  conf+:A,B  conf-:A,B    the slot model's what-ifs (c2slots.py what)
           ins:N  drop:N    a scratch pick more before pick N / pick N gone (rasim scratch)
           alias2:CHAIN  ptrlocal:CHAIN  subobj:CHAIN   the load-reuse levers of docs/c2/loadcse.md (they change code)
           byval:NAME       the calls of NAME (`*`: all) that return a class by value, through a by-value inline
                            forwarding template (rwsweep's byval family; it changes code)
         Blocks are numbered as `c2trace.py regs --why` numbers them. The tool maps block B to source lines through
         the trace's line info, generates every catalogued construct for that edit kind at the statement boundaries
         of those lines (CONSTRUCTS below; docs/c2/realize.md has the measured table), compiles and traces each
         variant once, and reports per variant: did the requested edit happen (`exact`: it and nothing else; `yes`:
         it and the side effects listed after `also:`; `no`, with what it did instead), the code cost (`code`:
         aligned diff of the listings with registers masked, operands kept: 0 = code-neutral; `bag`: the same as a
         multiset without frame displacements: 0 = the same instructions in another order; `regs`: the diff with
         registers), the registers of --want-regs, and with --asm-vs the rdiff (the ranking: tsweep.rdiff) and the
         bdiff (0 = the match) against the original.
         Several EDITs are realised one by one and then together. --list prints the candidates and stops.
         `ref:V:B` / `extend:V:B` for a V that is never modified (this, a read-only parameter) has one code-neutral
         form on 14.00 / 15.00: `if ((int)V & 1) S; else S;` around an assignment S of the block (eqif; merged
         after Color). Differences also list `tie:A>B` (equal priorities whose defOrd order flipped).
--from-rasim   the pipeline. ANSWERS.txt is the output of `rasim need SNAP reg:..` / `rasim want SNAP regmap.txt`
         (`-`: stdin); `auto` makes it (regmap --all against --asm-vs, then rasim want). Three stages, stopping at
         the first variant that matches (bdiff 0 with --asm-vs, otherwise the wanted registers with bag 0):
           1. each answer made of source-level edits, each edit through its code-neutral constructs;
           2. every structural code-neutral construct that touches a block an answer names (an order answer
              `before:A:B` is turned into blocks by `rasim need 'A>B'`), singly, then those that changed the
              allocation's inputs without changing an instruction together (per family, and all);
           3. stub bodies for the bodiless callees that receive a local's address (docs/c2/callee.md).
         An answer of several edits is realised as products of each edit's best realisations; an edit without a
         construct (canreg:, pref:, ..) is left out with a note; `before:` becomes `rasim need 'A>B'`'s edits.
         regmap's swaps are asked of `rasim need` too. With no match, the closest variants are combined (--pairs).
         Prints `FOUND ... <variant.cpp>` or `NOT FOUND ... closest: ...`.
--from slots|scratch   the same with the answers of the slot model (single what-ifs and model-ranked pairs against
         the original's slot map) or of the scratch model (rasim's re-alignment scan, singles and pairs).
measure  what did a source change do, in abstract edits: compiles both files and prints the difference of the two
         traces of F (per block P, references, ranges, new and lost candidates, constants, interference, copies,
         preferences, registers, priorities) and the code cost. With --keys also the symbol-key difference.
pairs    the same for a probe file: every function named X__base is compared with each X__<variant> in the file
         (one compile). This is how the catalogue was measured: tests/data/realize.cpp, work/scratch/realize/.

Variants are kept in work/realize/<tu>-<dirhash>[@build]/ (or --keep DIR).
"""
import argparse, difflib, json, os, re, shutil, subprocess, sys, tempfile, threading
import hashlib
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from image import ROOT
import c2trace, c2regs, tsweep

RASIM = os.path.join(ROOT, "tools", "rasim", "target", "release", "rasim") + (".exe" if os.name == "nt" else "")
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
    op = re.sub(r"\btv\d+", "tv", op)                       # the listing's names for compiler temporaries
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
        self.asm_path = asm_path
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
        # BB of each block in the integer pass; `bbs` is pass 1's, so a float function (XMM / x87 passes first, the
        # integer pass later) needs the per-pass map, or every block maps to no source line (Spore 0073afb0)
        bbof = {b: bb for (p, b), bb in fn.get("blkbb", {}).items() if p == self.np}
        if not bbof and self.np == 1:
            bbof = {b: bb for b, w, P, bb in fn.get("bbs", [])}
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
        self.prio, self.dord = {}, {}
        for r in fn["rows"]:
            if r["cls"] in (0, None) and r["npass"]:
                self.prio.setdefault(r["name"], r["prio"])
                self.dord.setdefault(r["name"], r.get("dord"))
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
        # the same relations with the compiler's locals (`local23`: a name that survives a source change), for the
        # answers of rasim that name them; the registers each web may have when it is first queued (noreg / canreg);
        # and the blocks each web is referenced in (where a source construct has to go for a value with no name)
        namedl = lambda n: n and not re.match(r"(t|imm|k\d+_)\d+$", n) and n != "?"
        self.interf_l, self.copies_l, self.prefs_l = set(), set(), {}
        for p in self.snap.get("pops", []):
            c = p.get("choice")
            if not c or p.get("cls") not in (0, None):
                continue
            for n in c["nbrs"]:
                if namedl(p["name"]) and namedl(n["name"]) and n["name"] != p["name"]:
                    self.interf_l.add(tuple(sorted((p["name"], n["name"]))))
            for n in c["partners"]:
                o = gname.get(n["gr"])
                if namedl(p["name"]) and namedl(o) and o != p["name"]:
                    self.copies_l.add(tuple(sorted((p["name"], o))))
        for e in self.snap.get("pref_events", []):
            nm = gname.get(e["gr"])
            if namedl(nm) and e.get("src") is None:
                k = (nm, rn.get(str(e["reg"]), str(e["reg"])))
                self.prefs_l[k] = self.prefs_l.get(k, 0) + 1
        self.avail = {}
        for e in self.snap.get("inserts") or []:
            nm = (e.get("x") or {}).get("name")
            if namedl(nm) and nm not in self.avail and e.get("avail"):
                self.avail[nm] = frozenset(rn.get(str(r_), str(r_)) for r_ in e["avail"])
        self.known = set(self.avail) | {m_.get("name") for m_ in (self.snap.get("missing") or [])}
        self.where = {}
        for (p, b), lst in fn.get("byblock", {}).items():
            if p == self.np:
                for nm, g, cost, d in lst:
                    if cost is not None and namedl(nm):
                        self.where.setdefault(nm, set()).add(b)

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
        # ties among named values of equal priority whose defOrd order flipped (the larger defOrd pops first):
        # what an order-only change (two statements of a block exchanged) does to Color's input
        named = [n for n in a.prio if n in b.prio and not re.match(r"(t|imm|k\d+_|local)\d+$", n) and n != "?"
                 and a.dord.get(n) is not None and b.dord.get(n) is not None]
        self.ties = []
        for i_, x in enumerate(named):
            for y in named[i_ + 1:]:
                if a.prio[x] == a.prio[y] and b.prio[x] == b.prio[y] and \
                        (a.dord[x] > a.dord[y]) != (b.dord[x] > b.dord[y]) and a.dord[x] != a.dord[y] and b.dord[x] != b.dord[y]:
                    self.ties.append((x, y) if b.dord[x] > b.dord[y] else (y, x))
        self.interf = (sorted(b.interf - a.interf), sorted(a.interf - b.interf))
        self.interf_l = (sorted(b.interf_l - a.interf_l), sorted(a.interf_l - b.interf_l))
        self.copies_l = (sorted(b.copies_l - a.copies_l), sorted(a.copies_l - b.copies_l))
        self.prefs_l = {k: b.prefs_l.get(k, 0) - a.prefs_l.get(k, 0) for k in sorted(set(a.prefs_l) | set(b.prefs_l))
                        if a.prefs_l.get(k, 0) != b.prefs_l.get(k, 0)}
        # registers a value may or may not have: lost (noreg: it now lives across something that clobbers it) or
        # gained (canreg), for the caller-saved registers only
        self.noreg, self.canreg = [], []
        full = frozenset(("eax", "ecx", "edx", "ebx", "esi", "edi", "ebp"))
        for n in sorted(set(a.avail) | set(b.avail)):
            if n not in b.avail or (n not in a.avail and n not in a.known):
                continue                           # gone, or a new value: nothing in the base to lose a register from
            av0 = a.avail.get(n, full)             # a value that had no web in the base (a parameter left in memory)
            for r_ in ("eax", "ecx", "edx"):
                if r_ in av0 and r_ not in b.avail[n]:
                    self.noreg.append((n, r_))
                elif r_ not in av0 and r_ in b.avail[n]:
                    self.canreg.append((n, r_))
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
        out += ["noreg:%s:%s" % p for p in self.noreg] + ["canreg:%s:%s" % p for p in self.canreg]
        out += ["tie:%s>%s" % t for t in getattr(self, "ties", [])]
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
    if k in ("drop", "ins") and 2 <= len(f) <= 3 and f[1].isdigit():
        return dict(kind="scratch_" + k, n=int(f[1]), text=s)
    m = re.match(r"(alias2|ptrlocal|subobj|byval):(.+)$", s)
    if m:
        return dict(kind="load", lk=m.group(1), chain=m.group(2).replace(" ", ""), text=s)
    m = re.match(r"(uses|size|pos):([^=]+)=(\d+)\^?$", s)
    if m:
        return dict(kind="slot", sk=m.group(1), var=m.group(2), n=int(m.group(3)), text=s)
    m = re.match(r"(conf[+-]):([^,]+),(.+)$", s)
    if m:
        return dict(kind="slot", sk=m.group(1), var=m.group(2), other=m.group(3), text=s)
    raise SystemExit("cannot parse edit %r" % s)


def verdict(want, d):
    """(exact | yes | no, the side effects as strings) for one requested edit against a measured Delta."""
    k = want["kind"]
    hit, rest = [], []
    split = False
    if k in ("ref", "extend") and d.new and (want["block"] in d.map or want["block"] in d.lost):
        # the block was split (an equal-arms if, a duplicated head or tail made new blocks): V's references in
        # the block (or what replaced it) and the new blocks together, against the block alone
        sa, sb = d.a.state.get(want["var"], {}), d.b.state.get(want["var"], {})
        before = sa.get(want["block"]) or 0
        after = (sb.get(d.map[want["block"]]) or 0 if want["block"] in d.map else 0) + sum(sb.get(nb) or 0 for nb in d.new)
        split = after > before
    for e in d.edits:
        kind, var, blk, amt = e
        mine = False
        if split and var == want["var"] and kind in ("ref", "extend", "unref", "drop", "through+", "through-") and \
                (blk == want["block"] or str(blk).startswith("n")):
            hit.append(e)
            continue
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
        lst = {"interf": d.interf_l[0], "nointerf": d.interf_l[1], "copy": d.copies_l[0], "nocopy": d.copies_l[1]}[k]
        # a rewrite that introduces a variable (`rz_c`, `rz_h`) can only make the relation with the new web: it
        # counts when the other end is one of the wanted names (the new variable takes that role)
        gone = {e[1] for e in d.edits if e[0] == "gone"}
        ok = pair in lst or any(any(n.startswith("rz_") for n in q) and bool(set(q) & set(pair)) and not (set(pair) & gone)
                                for q in lst)
        side = [s for s in side if s != "%s:%s:%s" % ((k,) + pair)]
    elif k == "pref":
        # the preferences for that register summed over every web: a rename (the same value under another name) is 0
        gone = {e[1] for e in d.edits if e[0] == "gone"}
        net = sum(v for (n_, r_), v in d.prefs_l.items() if r_ == want["other"])
        ok = (d.prefs_l.get((want["var"], want["other"]), 0) * want["n"] > 0 and want["var"] not in gone) or net * want["n"] > 0
        side = [s for s in side if not s.startswith("pref:%s:%s:" % (want["var"], want["other"]))]
    elif k in ("noreg", "canreg"):
        ok = (want["var"], want["other"]) in (d.noreg if k == "noreg" else d.canreg)
        side = [s for s in side if s != "%s:%s:%s" % (k, want["var"], want["other"])]
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
        self.ties = getattr(d, "ties", [])
        self.noreg, self.canreg = d.noreg, d.canreg


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
    if a.slots:
        sa_, sb_ = slot_snap_tmp(a.src, fa.name, cl), slot_snap_tmp(a.edits[0], fb.name, cl)
        sd_ = SlotDelta(sa_, sb_)
        print("  slots: %s; offsets %s" % (" ".join(sd_.strings()) or "(no packer-input change)", sd_.where()))
    if a.scratch:
        pa_, pb_ = pick_snap_tmp(a.src, fa.name, cl), pick_snap_tmp(a.edits[0], fb.name, cl)
        pd_ = PickDelta(pa_, pb_)
        print("  picks: %d -> %d; %s" % (len(pd_.pa), len(pd_.pb), " ".join(pd_.strings()) or "(same picks)"))
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
    ss, ps = {}, {}
    if getattr(a, "slots", False):
        import c2slots
        info, sfuncs, sasm, _d = c2slots.trace(a.src, "", cl, keep=os.path.join(t.dir, "slots"))
        for F in sfuncs:
            lst = {}
            try:
                lst = c2slots.attach_listing(F, c2slots.listing_slots(sasm, F["name"]))
            except KeyError:
                pass
            ss[F["name"]] = c2slots.snapshot(F, info, lst)
    if getattr(a, "scratch", False):
        import c2scratch
        sd = os.path.join(t.dir, "scratch")
        _i, _h, _rc, _o, slog = c2trace.run_trace(a.src, c2scratch.HOOKS_X, cl, keep=sd, quiet=True)
        for f in fns:
            segs, final = c2scratch.parse_segments(slog, f.name)
            segs = [x for x in segs if x["picks"]]
            if segs:
                for x in segs:
                    c2scratch.annotate(x)
                p = os.path.join(sd, re.sub(r"\W+", "_", f.name)[:60] + ".json")
                c2scratch.export(p, f.name, segs, c2scratch.offsets(final))
                ps[f.name] = (json.load(open(p)), p)
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
            if base.name in ss and f.name in ss:
                sd_ = SlotDelta(ss[base.name], ss[f.name])
                print("%-26s   slots: %s; offsets %s" % ("", " ".join(sd_.strings()) or "(no packer-input change)", sd_.where()))
            if base.name in ps and f.name in ps:
                pd_ = PickDelta(ps[base.name][0], ps[f.name][0])
                print("%-26s   picks: %d -> %d; %s" % ("", len(pd_.pa), len(pd_.pb), " ".join(pd_.strings()) or "(same picks)"))
            if a.diff and d.ident:
                for ln in difflib.unified_diff(["%s %s" % x for x in base.code], ["%s %s" % x for x in f.code], lineterm="", n=1):
                    print("        " + ln)
    t.close()


# ---------------------------------------------------------------- the other models' inputs: stack slots, scratch picks
#
# The slot model (tools/c2slots.py, docs/c2/slots.md) answers in `uses:X=N`, `pos:X=P`, `conf+:A,B`, `conf-:A,B`;
# the scratch model (`rasim scratch`, tracer.md) in `ins:N` / `drop:N`. A variant is measured against the base by
# tracing it once more with that model's hooks: the elements (or picks) of the two traces are matched by name and
# source line, and the difference is printed in the model's own vocabulary.

SLOT_KINDS = ("uses", "pos", "conf+", "conf-")
PICK_KINDS = ("scratch_ins", "scratch_drop")


def slot_snap(path, func, cl, keep=None):
    """The c2slots snapshot of `func` in the source `path` (a traced compile with the `slots` hooks, kept in
    <dir>/slots/), with `_asm` = that compile's listing."""
    import c2slots
    d = keep or os.path.join(os.path.dirname(os.path.abspath(path)), "slots")
    info, funcs, asm, _d = c2slots.trace(path, func, cl, keep=d)
    if not funcs:
        raise RuntimeError("no stack packing seen for %s" % func)
    F = funcs[0]
    lst = {}
    if os.path.exists(asm):
        try:
            lst = c2slots.attach_listing(F, c2slots.listing_slots(asm, F["name"]))
        except KeyError:
            lst = {}
    s = c2slots.snapshot(F, info, lst)
    s["_asm"] = asm
    return s


def slot_table(s):
    """{stable element key: dict(i, uses, size, vis (rank in its size run), off, lod)}. Keys: the source name without
    its block number (`_iy`), and for compiler temporaries `tv@L<line>/<size>` / `$T@L<line>/<size>` (their names
    are symbol keys and numbers that move with any edit); a repeated key gets `#k`."""
    import c2slots, collections
    order = c2slots.base_order(s)
    offs = {i: o for C in s["obs"]["cols"] for i, o in C["members"]}
    cnt, out, key = collections.Counter(), {}, {}
    lines = [(s["listing"].get(i) or {}).get("line") for i in order]
    L0 = min([x for x in lines if x] or [0])
    for i in order:
        e = s["elems"][i]
        nm = c2slots.ename(s, i) or ""
        L = (s["listing"].get(i) or {}).get("line")
        if not nm or nm.startswith("tv") or nm.startswith("$T") or re.match(r"\$S\d+", nm):
            base = "%s@L%s/%d" % ("$T" if nm.startswith("$T") else "tv", (L - L0) if L else None, e["size"])
        else:
            base = re.sub(r"\$\d*$", "", nm)
        k = base + ("#%d" % cnt[base] if cnt[base] else "")
        cnt[base] += 1
        key[i] = k
    rank = collections.Counter()
    for i in order:
        e = s["elems"][i]
        out[key[i]] = dict(i=i, uses=e["uses"], size=e["size"], vis=rank[e["size"]], ord=order.index(i), off=offs.get(i),
                           param=e.get("param"), name=c2slots.ename(s, i))
        rank[e["size"]] += 1
    for i in order:
        out[key[i]]["lod"] = {key[j] for j in (s["elems"][i].get("lod") or ()) if j in key}
    return out


class SlotDelta:
    """What a source change did to the stack packer's input and result, in c2slots' what-if vocabulary."""

    def __init__(self, sa, sb):
        self.a, self.b = slot_table(sa), slot_table(sb)
        A, Bt = self.a, self.b
        self.uses = {k: (A[k]["uses"], Bt[k]["uses"]) for k in A if k in Bt and A[k]["uses"] != Bt[k]["uses"]}
        self.new = sorted(set(Bt) - set(A))
        self.gone = sorted(set(A) - set(Bt))
        # visiting order among the elements both have, per size run
        both = [k for k in A if k in Bt]
        self.pos = {}
        for sz in {A[k]["size"] for k in both}:
            ka = sorted((k for k in both if A[k]["size"] == sz), key=lambda k: A[k]["vis"])
            kb = sorted((k for k in both if Bt[k]["size"] == sz and A[k]["size"] == sz), key=lambda k: Bt[k]["vis"])
            for n, k in enumerate(kb):
                if ka.index(k) != n and k not in self.uses:
                    self.pos[k] = (ka.index(k), n)
        conf = lambda T, ks: {tuple(sorted((x, y))) for x in ks for y in T[x]["lod"] if y in ks and x != y}
        ca, cb = conf(A, set(both)), conf(Bt, set(both))
        self.conf = (sorted(cb - ca), sorted(ca - cb))
        self.offs = {k: (A[k]["off"], Bt[k]["off"]) for k in both if A[k]["off"] != Bt[k]["off"]}
        self.T = (sa["fn"].get("T"), sb["fn"].get("T"))

    def strings(self):
        out = ["uses:%s:%d>%d" % (k, x, y) for k, (x, y) in sorted(self.uses.items())]
        out += ["pos:%s:%d>%d" % (k, x, y) for k, (x, y) in sorted(self.pos.items())]
        out += ["new:%s" % k for k in self.new] + ["gone:%s" % k for k in self.gone]
        out += ["conf+:%s,%s" % p for p in self.conf[0]] + ["conf-:%s,%s" % p for p in self.conf[1]]
        return out

    def where(self):
        s = " ".join("%s:%s>%s" % (k, x, y) for k, (x, y) in sorted(self.offs.items()))
        return ("T %s>%s " % self.T if self.T[0] != self.T[1] else "") + (s or "no element moved")

    def empty(self):
        return not self.strings()


def slot_key_of(s, name):
    """The slot_table key of an element named as c2slots' what-if names it (`_ny$`, `#4`, `tv310`)."""
    import c2slots
    i = c2slots.resolve(s, name)
    for k, v in slot_table(s).items():
        if v["i"] == i:
            return k
    return name


def slot_agree(s, orig):
    """(elements at the original's offset, elements seen in both): c2slots' map of the original against our listing."""
    import c2slots
    if orig is None or not os.path.exists(s.get("_asm") or ""):
        return None
    try:
        om, _info = c2slots.orig_map(s, s["_asm"], orig)
    except Exception:
        return None
    offs = {i: o for C in s["obs"]["cols"] for i, o in C["members"]}
    return sum(1 for i, (o, n, t) in om.items() if offs.get(i) == o), len(om)


def slot_verdict(w, sa, sb):
    """(exact | yes | no, side effects, notes) for one slot what-if against the variant's snapshot: the asked
    change must be there; `exact` also needs no other packer-input change and every element at the offset the
    replay of the edit predicts."""
    import c2slots
    d = SlotDelta(sa, sb)
    X = slot_key_of(sa, w["var"])
    sk = w["sk"]
    hit = False
    if sk == "uses":
        hit = X in d.uses and d.uses[X][1] == w["n"]
    elif sk == "pos":
        hit = X in d.b and d.b[X]["ord"] == w["n"]
    elif sk in ("conf+", "conf-"):
        pair = tuple(sorted((X, slot_key_of(sa, w["other"]))))
        hit = pair in d.conf[0 if sk == "conf+" else 1]
    mine = {"uses": "uses:%s:" % X, "pos": "pos:%s:" % X}.get(sk)
    side = [s for s in d.strings() if not (mine and s.startswith(mine)) and not (sk.startswith("conf") and s.startswith(sk))]
    if sk == "pos":
        side = [s for s in side if not s.startswith("pos:")]
    try:
        res, _el = c2slots.run_edits(dict(sa, listing=dict(sa["listing"])), [w["text"]])
        tab = slot_table(sa)
        pred = {k: res["offs"].get(v["i"]) for k, v in tab.items()}
        both = [k for k in pred if k in d.b]
        ok = sum(1 for k in both if d.b[k]["off"] == pred[k])
        note = "replay of the edit predicts %d of %d offsets" % (ok, len(both))
        allok = ok == len(both)
    except SystemExit as e:
        note, allok = "replay failed: %s" % e, False
    v = "no" if not hit else ("exact" if not side and allok else "yes")
    return v, side, note, d


def pick_snap(path, func, cl, keep=None):
    """(the `rasim scratch` snapshot of func's picks as a dict, its path): a traced compile with the scratch hooks
    (14.00 and every ported build: hooks.scratch_x), kept in <dir>/scratch/."""
    import c2scratch
    d = keep or os.path.join(os.path.dirname(os.path.abspath(path)), "scratch")
    _img, _hk, rc, out, log = c2trace.run_trace(path, c2scratch.HOOKS_X, cl, keep=d, quiet=True)
    segs, final = c2scratch.parse_segments(log, func)
    segs = [x for x in segs if x["picks"]]
    if not segs:
        raise RuntimeError("no scratch picks seen for %s" % func)
    for x in segs:
        c2scratch.annotate(x)
    p = os.path.join(d, "scratch.json")
    c2scratch.export(p, func, segs, c2scratch.offsets(final))
    js = json.load(open(p))
    for run, seg in zip(js["runs"], segs):            # the pick's source line (relative to the function, as the
        for q, sp in zip(run["picks"], seg["picks"]):  # regs trace numbers lines): where a construct must go
            ln = c2scratch._instr(sp)[1]
            q["line"] = int(ln) if ln and str(ln).lstrip("-").isdigit() else None
    json.dump(js, open(p, "w"), indent=1)
    return js, p


def slot_snap_tmp(path, func, cl):
    return slot_snap(path, func, cl, keep=tempfile.mkdtemp(prefix="realize_slots_"))


def pick_snap_tmp(path, func, cl):
    return pick_snap(path, func, cl, keep=tempfile.mkdtemp(prefix="realize_picks_"))[0]


def _body_picks(snap):
    runs = [r for r in snap["runs"] if r.get("body")] or snap["runs"][-1:]
    return runs[0]["picks"] if runs else []


def _pick_key(p):
    t = re.sub(r"\bt\d+", "t", p.get("instr") or "")
    t = re.sub(r":\d+", "", t)
    t = re.sub(r"\b(e?[abcd]x|[abcd][lh]|e?si|e?di|e?bp)=", "", t)
    return (p.get("op"), t)


class PickDelta:
    """What a source change did to the scratch picks: picks inserted (`ins:N` = before base pick N, `gone` when the
    new pick left no instruction), picks removed (`drop:N`), and aligned picks whose register changed."""

    def __init__(self, sa, sb):
        self.pa, self.pb = _body_picks(sa), _body_picks(sb)
        ka, kb = [_pick_key(p) for p in self.pa], [_pick_key(p) for p in self.pb]
        sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
        self.pairs, self.ins, self.drop = [], [], []
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                self.pairs += list(zip(range(i1, i2), range(j1, j2)))
                continue
            if op == "replace" and i2 - i1 == j2 - j1:
                self.pairs += list(zip(range(i1, i2), range(j1, j2)))       # the same picks, another instruction form
                continue
            nxt = self.pa[i2]["n"] if i2 < len(self.pa) else len(self.pa) + 1
            for j in range(j1, j2):
                self.ins.append((nxt, self.pb[j]))
            for i in range(i1, i2):
                self.drop.append(self.pa[i])
        self.regs = [(self.pa[i], self.pb[j]) for i, j in self.pairs if self.pa[i]["reg"] != self.pb[j]["reg"]]

    def strings(self):
        out = ["ins:%d%s" % (n, "" if p.get("how") == "ROT" else "(%s)" % p.get("how")) +
               ("(gone)" if p.get("off") is None else "") for n, p in self.ins]
        out += ["drop:%d" % p["n"] for p in self.drop]
        if self.regs:
            out.append("regs:%d picks, first %d %s>%s" % (len(self.regs), self.regs[0][0]["n"], self.regs[0][0]["reg"],
                                                          self.regs[0][1]["reg"]))
        return out

    def empty(self):
        return not self.strings()


def rasim_scratch(snap_path, edits):
    """{base pick n: predicted register} after `rasim scratch SNAP EDITS` (only the picks it says change)."""
    r = subprocess.run([RASIM, "scratch", snap_path] + list(edits), capture_output=True, text=True)
    out = {}
    for m in re.finditer(r"^\s+(\d+)\s+\S+\s+(\w+) -> (\w+)\s", r.stdout, re.M):
        out[int(m.group(1))] = m.group(3)
    for m in re.finditer(r"^\s+(\d+)\s+\S+\s+removed\b", r.stdout, re.M):
        out[int(m.group(1))] = None
    return out, r.stdout


def pick_verdict(want_edits, base_snap_path, pd):
    """(exact | yes | no, notes) for scratch edits [`ins:N`, `drop:N`, ..] against a measured PickDelta: the inserted
    and removed picks must be the ones asked for, and every aligned pick must get the register rasim predicts."""
    pred, _ = rasim_scratch(base_snap_path, want_edits)
    wi = sorted(int(e.split(":")[1]) for e in want_edits if e.startswith("ins:"))
    wd = sorted(int(e.split(":")[1]) for e in want_edits if e.startswith("drop:"))
    rot_ins = sorted(n for n, p in pd.ins if p.get("how") == "ROT")
    got_d = sorted(p["n"] for p in pd.drop)
    bad = []
    for i, j in pd.pairs:
        a, b = pd.pa[i], pd.pb[j]
        want = pred.get(a["n"], a["reg"])
        if a["n"] in wd:
            continue
        if want is not None and b["reg"] != want:
            bad.append("%d:%s want %s" % (a["n"], b["reg"], want))
    notes = []
    if rot_ins != wi:
        notes.append("rotating picks inserted before %s, asked %s" % (rot_ins or "-", wi or "-"))
    if got_d != wd:
        notes.append("picks removed %s, asked %s" % (got_d or "-", wd or "-"))
    if bad:
        notes.append("%d picks off the prediction (%s)" % (len(bad), ", ".join(bad[:4])))
    if not bad and not notes:
        return "exact", []
    if not bad:
        return "yes", notes            # the registers come out as predicted by another set of picks
    return "no", notes


# ---------------------------------------------------------------- generators: an abstract edit -> source rewrites

PRELUDE_RZ = ("template<class RzT> struct rz_g { static RzT v; };\n"
              "template<class RzT> __forceinline void rz_keep(RzT x) { rz_g<RzT>::v = x; }\n"
              "template<class RzT> RzT rz_ret();\nextern int rz_ext(int);\n"
              "template<class RzT> struct rz_sub { RzT v; __forceinline RzT& get() { return v; } };\n")
SRC_KINDS = ("ref", "unref", "drop", "extend", "var", "P")
# construct name -> (what it realises, measured code cost on 14.00; docs/c2/realize.md has the table)
CONSTRUCTS = {
    "copy": "`T k = V;` before V is next modified, later uses renamed: +2 on V in that block, one more candidate "
            "there, V's range ends at the copy (code-neutral: the copy is coalesced)",
    "uncopy": "the reverse: `T k = V;` removed, k's uses spelled V",
    "headdup": "the statement before an if/else written at the head of both arms (merged back after Color): its "
               "references leave the block of the test and count in each arm",
    "headhoist": "the reverse: a statement that starts both arms hoisted above the test",
    "headret": "the statement before an if whose then-arm always leaves (return/break/continue) written at the head "
               "of that arm and again after the if: `if (C) { S; A; return; } S; R` (a call too, e.g. a log call "
               "on both paths). The copies are head-merged above the test after RegSimpleAssignBody when they get "
               "the same scratch registers; the second copy's picks are hidden (ins:N before the first pick after "
               "the if). 14.00 / 15.00; 13.10 keeps the copies",
    "callee": "a body for a bodiless callee that only reads through the parameter receiving &V: V stays one web "
              "across the call (docs/c2/callee.md); the true pattern is in the original's callee",
    "wrap": "a call argument through a by-value identity helper with a local, or `&x` through a bound reference: "
            "one more candidate in EH functions (frontend.md); replaces a scratch pick",
    "const": "a narrow constant stored through the other signedness; a for-init hoisted or sunk (immcand.md)",
    "guard": "`delete p; p = 0;` guarded or not: the reset store changes block (frontend.md)",
    "helperbind": "`&p->m` in an inline helper (or the function) through a bound reference: one more candidate in "
                  "functions with an EH frame, no code, pick-neutral (frontend.md, tools/helpersweep.py)",
    "taildup": "the statement after an if written at the end of both arms (an else is made when there is none): "
               "tail merging after Color joins the copies; its references count in the arms instead of the join. "
               "Code-neutral for arithmetic and stores; a call in the copy stops the merge (stack offsets)",
    "tailhoist": "the reverse: a statement that ends both arms written once after the if",
    "eqif": "`if ((int)V & 1) S; else S;` around a register-local assignment S of the block: one more reference to "
            "V there and S's references into the arms, merged away after Color (14.00/15.00; 13.10 when V is in "
            "a register). The construct for a value that is never modified (this, read-only parameters)",
    "swap": "two adjacent independent statements exchanged: the defOrd tie-break and interference change; the "
            "scheduler restores the instruction order only sometimes",
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
                       for nm in re.findall(r"\b((?:hs|rz)_\w+)\s*\(", ln))
            and not any(re.search(r"\bstruct\s+%s\b" % nm, self.text) for nm in re.findall(r"\bstruct\s+(rz_\w+)", ln)))
        # the source's directory enters the name (as in tsweep): two agents' v3.cpp must not share, and rmtree, one directory
        out = a.keep or os.path.join(ROOT, "work", "realize", tsweep.out_name(a.src, a.func))
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
        _more_decls(self.B, rwsweep)
        self.line0 = self.text.count("\n", 0, self.fn[4])
        self.wantregs = dict(x.split("=") for x in a.want_regs.split(",")) if a.want_regs else {}
        self.keybase = None
        self._slotbase = self._pickbase = None
        self._orig_ins = None

    def slotbase(self):
        """The base's stack-packing snapshot (traced once, kept in base/slots/)."""
        if self._slotbase is None:
            self._slotbase = slot_snap(self.base_path, self.func, self.cl)
        return self._slotbase

    def pickbase(self):
        """(the base's scratch-pick snapshot, its path) for `rasim scratch` (traced once, kept in base/scratch/)."""
        if self._pickbase is None:
            self._pickbase = pick_snap(self.base_path, self.func, self.cl)
        return self._pickbase

    def orig_ins(self):
        if self._orig_ins is None and self.a.asm_vs:
            import c2slots
            self._orig_ins = c2slots.read_orig(self.a.asm_vs)
        return self._orig_ins

    def src_lineoff(self, L):
        """A listing source line (1-based line of the compiled file: prelude + text) as a line offset in the
        target's body (what lineoff() returns)."""
        return L - 1 - self.prelude.count("\n") - self.line0

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

    def scored(self, f):
        """The listing instructions scored against the original: F's own, or --asm-func's (as tsweep: the function
        of our listing that corresponds to orig.s, when that is not --func itself)."""
        af = getattr(self.a, "asm_func", None)
        if not af or af == self.a.func:
            return f.instrs
        try:
            return tsweep.listing_instrs(f.asm_path, af)
        except Exception:
            return []

    def regdiff(self, f):       # the score against the original, the sort key: tsweep.rdiff (the old name kept)
        return tsweep.rdiff(self.scored(f), self.orig) if self.orig is not None else None

    def bdiff(self, f):         # byte-faithful: 0 is the match (hit), printed beside the score
        return tsweep.bdiff(self.scored(f), self.orig) if self.orig is not None else None

    def oldreg(self, f):        # the earlier score (aligned mnemonics + registers), printed alongside
        return tsweep.regdiff(self.scored(f), self.orig) if self.orig is not None else None


def _more_decls(B, rw):
    """rwsweep's Body knows one variable per declaration (and none for `int a, b;`). Every declarator of a plain
    declaration statement is added here (declaration token, the base type with the declarator's `*`), with its uses."""
    added = []
    TYQ = {"const", "unsigned", "signed", "struct", "class", "volatile", "long", "short"}
    for i in B.starts:
        q = i
        while q < len(B.toks) and (re.match(r"[A-Za-z_]\w*$", B.toks[q][0]) or B.toks[q][0] in ("*", "&", "::", "<", ">")):
            q += 1
        if q >= len(B.toks) or B.toks[q][0] not in (",", "=", ";", "[") or q - i < 2:
            continue
        if B.toks[i][0] in rw.KEYWORDS - TYQ or B.paren[i]:
            continue
        # the declarator name is the last identifier before q; the type is what precedes it (no stars)
        nm_k = q - 1
        if not re.match(r"[A-Za-z_]\w*$", B.toks[nm_k][0]):
            continue
        tyk = [B.toks[k][0] for k in range(i, nm_k)]
        stars = "".join(x for x in tyk if x in ("*", "&"))
        base = " ".join(x for x in tyk if x not in ("*", "&")).replace(" :: ", "::")
        if not base or not re.match(r"[A-Za-z_]", base):
            continue
        decls = [(nm_k, stars)]
        j, d = q, 0
        while j < len(B.toks) and not (B.toks[j][0] == ";" and d == 0):
            x = B.toks[j][0]
            d += x in ("(", "[", "{")
            d -= x in (")", "]", "}")
            if x == "," and d == 0:
                k, st_ = j + 1, ""
                while k < len(B.toks) and B.toks[k][0] in ("*", "&"):
                    st_ += B.toks[k][0]
                    k += 1
                if k + 1 < len(B.toks) and re.match(r"[A-Za-z_]\w*$", B.toks[k][0]) and B.toks[k + 1][0] in (",", "=", ";", "["):
                    decls.append((k, st_))
            j += 1
        for k, st_ in decls:
            nm = B.toks[k][0]
            if nm not in B.vars and nm not in rw.KEYWORDS:
                B.vars[nm] = ((base + " " + st_).strip(), k)
                added.append(nm)
    for nm in added:
        B.uses[nm] = [k for k, (x, s, _) in enumerate(B.toks) if x == nm and k != B.vars[nm][1]
                      and (k == 0 or B.toks[k - 1][0] not in (".", "->", "::"))]


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
        if V and not _mentions(c, V, i, j):
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
        if V and not _mentions(c, V, s1[0], s1[1]):
            continue
        if lines and not ({c.lineoff(j), c.lineoff(s1[0]), c.lineoff(s2[0])} & lines):
            continue
        if not _cond_safe(B, s1[0], s1[1], j + 1, ie[0], c.rw):
            continue
        yield ("headhoist", "`%s` (lines +%d, +%d) hoisted above the if" % (t1[:40], c.lineoff(s1[0]), c.lineoff(s2[0])),
               [(B.toks[j][1], B.toks[j][1], t1 + " "), (B.toks[s1[0]][1], B.toks[s1[1] - 1][2], ""),
                (B.toks[s2[0]][1], B.toks[s2[1] - 1][2], "")], "")


def _members(c):
    """Field and method names of the target's class and its bases (for `this` used implicitly)."""
    if getattr(c, "_mem", None) is not None:
        return c._mem
    names, todo, seen = set(), [c.cls] if c.cls else [], set()
    while todo:
        cn = todo.pop()
        if cn in seen:
            continue
        seen.add(cn)
        for m in re.finditer(r"\b(?:class|struct)\s+%s\b\s*(?::([^{;]*))?\{" % re.escape(cn), c.text):
            body = c.text[m.end():c.hs.match_brace(c.text, m.end() - 1)]
            body = re.sub(r"\{[^{}]*\}", ";", body)                 # in-class method bodies
            names |= set(re.findall(r"\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*(?=[;,=:)]|\()", body))
            todo += re.findall(r"\b(\w+)\s*(?:,|$)", re.sub(r"\b(public|private|protected|virtual)\b", "", m.group(1) or ""))
    c._mem = names - set(c.rw.KEYWORDS) - {"operator", cn}
    return c._mem


def _mentions(c, V, i, j):
    """Do the tokens [i, j) mention V (for V == 'this' also a member of the class named without `this->`)?"""
    B = c.B
    toks = [B.toks[k][0] for k in range(i, j)]
    if V is None or V in toks:
        return True
    if V != "this" or not c.cls:
        return False
    mem = _members(c)
    return any(t in mem and t not in B.vars and (k == 0 or toks[k - 1] not in (".", "->", "::"))
               for k, t in enumerate(toks))


def _ifstmt(B, i, rw):
    """For an `if` at token i (not an `else if`): (cond close, then-start, then-end, else-start, else-end) with the
    ends one past the arm; else-start/end None when there is no else."""
    if i >= len(B.toks) - 3 or B.toks[i][0] != "if" or B.toks[i + 1][0] != "(":
        return None
    pc = c_pclose(B, i + 1)
    if pc is None or pc + 1 >= len(B.toks):
        return None
    try:
        e1 = rw.stmt_end(B, pc + 1)
        if e1 < len(B.toks) and B.toks[e1][0] == "else":
            return pc, pc + 1, e1, e1 + 1, rw.stmt_end(B, e1 + 1)
    except (IndexError, KeyError):
        return None
    return pc, pc + 1, e1, None, None


def _arm(B, a, z):
    """The arm [a, z) as the text between its braces (a braced block) or the statement itself."""
    if B.toks[a][0] == "{" and B.close.get(a) == z - 1:
        return B.text[B.toks[a][2]:B.toks[z - 1][1]].strip()
    return B.text[B.toks[a][1]:B.toks[z - 1][2]]


def _simple(c, i, j):
    """A statement that may be copied into arms: no declaration, no jump, no case label, no braces."""
    B = c.B
    t0 = B.toks[i][0]
    if t0 in c.rw.KEYWORDS - {"this"} or c.rw.DECL.match(B.text, B.toks[i][1]):
        return False
    return not any(B.toks[k][0] in ("return", "break", "continue", "goto", "case", "{", "}") for k in range(i, j))


def _later_uses_inside(c, name, lo, hi):
    return all(lo <= u < hi for u in c.B.uses.get(name, []))


def g_headdup2(c, V, lines):
    """The statement before an if (with or without else, braced or not) at the head of both arms; an if without
    else gets an else holding the copy. Head merging runs after Color (il_fg.md rule 12), so the copies merge
    back: the statement's references count in the arms. A declaration may move when every use of the declared
    name is inside the if."""
    B, rw = c.B, c.rw
    for k in range(len(B.starts)):
        s = rw._stmt(B, k)
        if not s:
            continue
        i, j = s
        ie = _ifstmt(B, j, rw) if j < len(B.toks) else None
        if not ie or B.toks[i][0] in ("case", "default", "else", "return"):
            continue
        pc, a1, z1, a2, z2 = ie
        dm = rw.DECL.match(B.text, B.toks[i][1])
        end = z2 if a2 is not None else z1
        if dm and not (dm.group(2) in B.uses and _later_uses_inside(c, dm.group(2), j, end)):
            continue
        if not dm and not _simple(c, i, j):
            continue
        if not _mentions(c, V, i, j):
            continue
        near = {c.lineoff(i), c.lineoff(a1)} | ({c.lineoff(a2)} if a2 is not None else set())
        if lines and not (near & lines):
            continue
        if not _cond_safe(B, i, j, j + 1, pc, rw):
            continue
        if a2 is not None and B.toks[a1][0] == "{" and B.toks[a2][0] == "{":
            continue                                   # the plain braced if/else: g_headdup's rewrite
        txt = B.text[B.toks[i][1]:B.toks[j - 1][2]]
        cond = B.text[B.toks[j][1]:B.toks[pc][2]]
        s2 = _arm(B, a2, z2) if a2 is not None else ""
        new = "%s { %s %s } else { %s %s }" % (cond, txt, _arm(B, a1, z1), txt, s2)
        yield ("headdup", "`%s` (line +%d) into both arms of the if that follows%s" % (
            txt[:40], c.lineoff(i), "" if a2 is not None else " (an else arm is made for it)"),
               [(B.toks[i][1], B.toks[end - 1][2], new + "\n" * B.text.count("\n", B.toks[i][1], B.toks[end - 1][2]))], "")


LEAVE = ("return", "break", "continue", "goto", "throw")


def _leaves(B, a, z):
    """Does the arm [a, z) always leave: its last statement (a braced arm: the last one directly inside the braces)
    is a return, break, continue, goto or throw."""
    if B.toks[a][0] == "{" and B.close.get(a) == z - 1:
        inner = [s for s in B.starts if a < s < z - 1 and B.block[s] == a]
        return bool(inner) and B.toks[inner[-1]][0] in LEAVE
    return B.toks[a][0] in LEAVE


def _plain_cond(B, a, z, rw):
    """A condition that reads no memory and calls nothing (a call statement may move across it)."""
    return not any(B.toks[k][0] in ("->", "[", "*", "(", ".", "++", "--") or B.toks[k][0] in rw.ASSIGN
                   for k in range(a + 1, z))


def g_headret(c, V, lines):
    """`S; if (C) { A; return; } R` as `if (C) { S; A; return; } S; R`: the statement before an if whose then-arm
    always leaves, written at the head of that arm and again after the if (the natural spelling of a call made on
    both paths, a log call per branch). Head merging after RegSimpleAssignBody hoists the copies back above the
    test when they got the same scratch registers, so the code is the source's and the copy after the if leaves its
    picks behind: a hidden pick (`ins:N`) before the first pick after the if. S may be a call when the condition
    reads no memory; a declaration only into a braced arm."""
    B, rw = c.B, c.rw
    for k in range(len(B.starts)):
        s = rw._stmt(B, k)
        if not s:
            continue
        i, j = s
        ie = _ifstmt(B, j, rw) if j < len(B.toks) else None
        if not ie or ie[3] is not None or B.toks[i][0] in ("case", "default", "else") + LEAVE:
            continue
        pc, a1, z1 = ie[:3]
        if not _leaves(B, a1, z1):
            continue
        dm = rw.DECL.match(B.text, B.toks[i][1])
        if dm and (B.toks[a1][0] != "{" or dm.group(2) in [B.toks[q][0] for q in range(j, pc)]):
            continue
        if not dm and not _simple(c, i, j):
            continue
        if not _mentions(c, V, i, j):
            continue
        near = {c.lineoff(i), c.lineoff(a1), c.lineoff(z1 - 1)}
        if lines and not (near & lines):
            continue
        if not (_cond_safe(B, i, j, j + 1, pc, rw) or _plain_cond(B, j + 1, pc, rw)):
            continue
        txt = B.text[B.toks[i][1]:B.toks[j - 1][2]]
        cond = B.text[B.toks[j][1]:B.toks[pc][2]]
        arm = _arm(B, a1, z1)
        new = "%s { %s %s } %s" % (cond, txt, arm, txt)
        yield ("headret", "`%s` (line +%d) at the head of the leaving then-arm and again after the if" % (
            txt[:40], c.lineoff(i)),
               [(B.toks[i][1], B.toks[z1 - 1][2], new + "\n" * B.text.count("\n", B.toks[i][1], B.toks[z1 - 1][2]))], "")


def _second_arm_heads(c, picks, n):
    """Line offsets of the statements S right before an if whose second arm (the else arm, or the code after an if
    whose then-arm leaves) starts with pick n: where a headdup / headret copy of S hides one pick before pick n."""
    B, rw = c.B, c.rw
    byn = {p["n"]: p.get("line") for p in picks}
    ln = byn.get(n)
    if ln is None:
        return set()
    out = set()
    for k in range(1, len(B.starts)):
        j = B.starts[k]
        if B.toks[j][0] != "if":
            continue
        ie = _ifstmt(B, j, rw)
        if not ie:
            continue
        pc, a1, z1, a2, z2 = ie
        if a2 is not None:
            lo, hi = c.lineoff(a2), c.lineoff(z2 - 1)
        elif _leaves(B, a1, z1) and z1 < len(B.toks):
            end = B.close.get(B.block[j], len(B.toks) - 1)
            if z1 >= end:
                continue
            lo, hi = c.lineoff(z1), c.lineoff(end)
        else:
            continue
        prev = byn.get(n - 1)
        if not (lo <= ln <= hi) or (prev is not None and lo <= prev <= hi and prev > c.lineoff(z1 - 1)):
            continue
        i = B.starts[k - 1]
        try:
            if rw.stmt_end(B, i) == j:
                out.add(c.lineoff(i))
        except (IndexError, KeyError):
            pass
    return out


def g_taildup(c, V, lines):
    """The statement after an if (with or without else) at the end of both arms (an if without else gets an else
    holding the copy). Tail merging runs after Color (FgTryTailMerge: fall-through against the jump, no size
    limit), so the copies merge back when they got the same registers and do not address the stack: the
    statement's references count in the arms instead of the join. Calls and pushes in the copy stop the merge
    (the arms' stack offsets differ): those variants change code and are reported as such."""
    B, rw = c.B, c.rw
    for i in range(1, len(B.toks) - 1):
        if B.toks[i][0] != "if" or B.toks[i - 1][0] == "else" or B.paren[i]:
            continue
        ie = _ifstmt(B, i, rw)
        if not ie:
            continue
        pc, a1, z1, a2, z2 = ie
        end = z2 if a2 is not None else z1
        if end >= len(B.toks) or end not in B.starts:
            continue
        k = B.starts.index(end)
        s = rw._stmt(B, k)
        if not s or not _simple(c, *s) or not _mentions(c, V, *s):
            continue
        ti, tj = s
        near = {c.lineoff(ti), c.lineoff(a1)} | ({c.lineoff(a2)} if a2 is not None else set())
        if lines and not (near & lines):
            continue
        cond = B.text[B.toks[i][1]:B.toks[pc][2]]
        tail = B.text[B.toks[ti][1]:B.toks[tj - 1][2]]
        s1 = _arm(B, a1, z1)
        s2 = _arm(B, a2, z2) if a2 is not None else ""
        if any(B.toks[q][0] in ("return", "break", "continue", "goto") for q in range(a1, end)):
            continue                                   # an arm that leaves does not reach the statement
        new = "%s { %s %s } else { %s %s }" % (cond, s1, tail, s2, tail)
        yield ("taildup", "`%s` (line +%d) into both arms of the if before it%s" % (
            tail[:40], c.lineoff(ti), "" if a2 is not None else " (an else arm is made for it)"),
               [(B.toks[i][1], B.toks[tj - 1][2], new + "\n" * B.text.count("\n", B.toks[i][1], B.toks[tj - 1][2]))], "")


def g_tailhoist(c, V, lines):
    """The reverse of taildup: both braced arms of an if/else end with the same statement; it is written once
    after the if."""
    B, rw = c.B, c.rw
    for i in range(1, len(B.toks) - 1):
        if B.toks[i][0] != "if" or B.toks[i - 1][0] == "else" or B.paren[i]:
            continue
        ie = _ifstmt(B, i, rw)
        if not ie or ie[3] is None or B.toks[ie[1]][0] != "{" or B.toks[ie[3]][0] != "{":
            continue
        pc, a1, z1, a2, z2 = ie
        lasts = []
        for a, z in ((a1, z1), (a2, z2)):
            st = [x for x in B.starts if a < x < z - 1 and B.block[x] == a]
            if not st:
                break
            k = B.starts.index(st[-1])
            s = rw._stmt(B, k)
            if not s or s[1] != z - 1:
                break
            lasts.append(s)
        if len(lasts) != 2:
            continue
        t1, t2 = (B.text[B.toks[x][1]:B.toks[y - 1][2]] for x, y in lasts)
        if re.sub(r"\s+", "", t1) != re.sub(r"\s+", "", t2) or not _simple(c, *lasts[0]) or not _mentions(c, V, *lasts[0]):
            continue
        if lines and not ({c.lineoff(lasts[0][0]), c.lineoff(lasts[1][0]), c.lineoff(z2 - 1)} & lines):
            continue
        yield ("tailhoist", "`%s` (lines +%d, +%d) written once after the if" % (t1[:40], c.lineoff(lasts[0][0]), c.lineoff(lasts[1][0])),
               [(B.toks[lasts[0][0]][1], B.toks[lasts[0][1] - 1][2], ""), (B.toks[lasts[1][0]][1], B.toks[lasts[1][1] - 1][2], ""),
                (B.toks[z2 - 1][2], B.toks[z2 - 1][2], " " + t1)], "")


SCALAR = r"\b(int|long|short|char|bool|unsigned|signed|size_t|__int64|DWORD|UINT|WORD|BYTE|BOOL|float|double)\b"


def _eqif_cond(c, V):
    """A condition on V that the optimiser cannot fold and that sets no equality fact: `((int)V & 1)`."""
    if V == "this":
        return "((int)this & 1)" if c.cls else None
    if V not in c.B.vars:
        return None
    ty = c.B.vars[V][0]
    if "[" in ty or "&" in ty:
        return None
    if "*" in ty:
        return "((int)%s & 1)" % V
    if re.search(SCALAR, ty) and not re.search(r"\b(float|double)\b", ty):
        return "((int)%s & 1)" % V
    return None


def g_eqif(c, V, sts):
    """`if ((int)V & 1) S; else S;` around a statement S of the block: two equal arms. They reach Color as three
    new blocks (the test reads V) and are merged away after it (head merge, the jumps to the next instruction,
    then the dead test: 14.00 / 15.00 always, 13.10 when the test is register-only). So V gets one more
    reference at the block with no code, also when V is never modified (`this`, a read-only parameter); S's
    references move into the arms (one more each). S must reach Color: an assignment to a register local (a
    store to memory is merged before Color and the construct vanishes), and must not sit between a call and the
    next call of its block (the block boundary keeps the first call's `add esp` apart: one more instruction)."""
    B = c.B
    cond = _eqif_cond(c, V)
    if not cond:
        return
    for st in sts:
        k = B.starts.index(st) if st in B.starts else None
        s = c.rw._stmt(B, k) if k is not None else None
        if not s or B.paren[st]:
            continue
        i, j = s
        dm = c.rw.DECL.match(B.text, B.toks[i][1])
        if dm and dm.group(3) == "=" and "," not in B.text[dm.end():B.toks[j - 1][2]] and not \
                re.search(r"\bconst\b|&|\[", dm.group(1)) and (re.search(SCALAR, dm.group(1)) or "*" in dm.group(1)):
            # `T x = e;`: declared first, then assigned in both arms
            e_ = B.text[dm.end():B.toks[j - 1][1]].strip()
            yield ("eqif", "`%s %s; if %s %s = .. else ..` at line +%d (equal arms, merged after Color)" % (
                dm.group(1).strip(), dm.group(2), cond, dm.group(2), c.lineoff(i)),
                   [(B.toks[i][1], B.toks[j - 1][2], "%s %s; if %s { %s = %s; } else { %s = %s; }" % (
                       dm.group(1).strip(), dm.group(2), cond, dm.group(2), e_, dm.group(2), e_))], "")
            continue
        if not _simple(c, i, j):
            continue
        toks = [B.toks[q][0] for q in range(i, j)]
        txt = B.text[B.toks[i][1]:B.toks[j - 1][2]]
        if len(toks) > 2 and all(re.match(r"[A-Za-z_]\w*$", x) and x not in c.rw.KEYWORDS for x in toks[:2]) and \
                toks[2] in ("(", ";", "=", "["):
            continue                                   # `T v(..);`: a declaration, not a statement for an arm
        calls = "new" in toks or any(re.match(r"[A-Za-z_]", t) and t not in c.rw.KEYWORDS and toks[q + 1] == "("
                                     for q, t in enumerate(toks[:-1]))
        eq = next((q for q, t in enumerate(toks) if t in c.rw.ASSIGN or t in ("++", "--")), None)
        if calls and (eq is None or len(toks[:eq]) != 1 or toks[0] not in B.vars):
            # a call statement, or a store whose value comes from a call / new: the call keeps the arms apart
            # until after Color (a plain store is merged before it and the construct vanishes)
            yield ("eqif", "`if %s %s else ..` at line +%d (equal arms with a call, merged after Color)" % (
                cond, txt[:40], c.lineoff(i)),
                   [(B.toks[i][1], B.toks[j - 1][2], "if %s { %s } else { %s }" % (cond, txt, txt))], "")
            continue
        if eq is None:
            continue
        lhs = toks[:eq] if toks[eq] not in ("++", "--") or eq else toks[1:2]
        if len(lhs) != 1 or lhs[0] not in B.vars or lhs[0] == "this":
            continue
        defs = [u for u in B.uses.get(lhs[0], []) if B.written(u, u + 1)]
        dpos = B.vars[lhs[0]][1]
        if dpos >= 0:
            dd = c.rw.DECL.match(B.text, B.toks[max(x for x in B.starts if x <= dpos)][1])
            defs += [dpos] if dd and dd.group(3) == "=" else []
        else:
            defs += [-1]                               # a parameter: defined at entry
        if toks[eq] == "=" and len(defs) < 2:
            continue                                   # a single-definition copy is propagated away, arms and all
        ty = B.vars[lhs[0]][0]
        if "&" in ty or "[" in ty or not (re.search(SCALAR, ty) or "*" in ty):
            continue
        if any(B.written(q, q + 1) and B.toks[q - 1][0] == "&" for q in B.uses.get(lhs[0], [])):
            continue                                   # address-taken: the store would stay a store
        yield ("eqif", "`if %s %s else %s` at line +%d (equal arms, merged after Color)" % (cond, txt[:30], txt[:20], c.lineoff(i)),
               [(B.toks[i][1], B.toks[j - 1][2], "if %s { %s } else { %s }" % (cond, txt, txt))], "")


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
    _decl_cache = {}                 # callee name -> its declarations in the unit (one regex pass per name)
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
        decls = _decl_cache.get(t) if t in _decl_cache else None
        if decls is None:
          decls = _decl_cache[t] = [m for m in re.finditer(r"^[ \t]*((?:__declspec\(\w+\)\s*)*(?:[\w:<>]+[ \t\*&]+)+)%s\s*\(([^;{}()]*?)\)\s*(const)?\s*(?:throw\s*\(\s*\))?\s*;"
                                        % re.escape(t), text, re.M)
                 if not re.search(r"[-+/=.]|^\s*[&*\d]|,\s*[&*\d]", m.group(2))]
        own = lambda m: [cn for cn, a_, z_ in classes if a_ < m.start() < z_]
        member = i > 0 and B.toks[i - 1][0] in (".", "->")
        nsof = lambda pos: [nn for nn, a_, z_ in spaces if a_ < pos < z_]
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


def _stmt_span(c, st):
    k = c.B.starts.index(st) if st in c.B.starts else None
    return c.rw._stmt(c.B, k) if k is not None else None


def _cond_vars(c, limit=3):
    """Values to test in equal arms without a memory read: `this` and int / pointer variables that Color put in a
    callee-saved register for their whole life in the base (the test is then register-only on every build)."""
    out = []
    keep = ("esi", "edi", "ebx", "ebp", "rsi", "rdi", "rbx", "rbp", "r12", "r13", "r14", "r15")   # x64: effectmine
    cand = (["this"] if c.cls else []) + [v for v in c.B.vars if v != "this"]
    for v in cand:
        regs = c.base.regs.get(v) or []
        if regs and all(r in keep for r in regs) and _eqif_cond(c, v):
            out.append(v)
    return out[:limit]


def _mention_stmts(c, V):
    """Statement starts whose statement mentions V (directly; `this` also through members)."""
    out = []
    for st in c.B.starts:
        s = _stmt_span(c, st)
        if s and _mentions(c, V, *s) and (V != "this" or True):
            if V in [c.B.toks[q][0] for q in range(*s)] or V == "this":
                out.append(st)
    return out


def g_uneqif(c, V, lines):
    """The reverse of eqif: `if (C) S; else S;` with equal arms already in the source written as S (the packer and
    Color then see S once)."""
    B, rw = c.B, c.rw
    for i in range(1, len(B.toks) - 1):
        if B.toks[i][0] != "if" or B.toks[i - 1][0] == "else" or B.paren[i]:
            continue
        ie = _ifstmt(B, i, rw)
        if not ie or ie[3] is None:
            continue
        pc, a1, z1, a2, z2 = ie
        s1, s2 = _arm(B, a1, z1), _arm(B, a2, z2)
        if re.sub(r"\s+", "", s1) != re.sub(r"\s+", "", s2):
            continue
        if V and not _mentions(c, V, a1, z1) and not _mentions(c, V, i, pc + 1):
            continue
        if lines and c.lineoff(i) not in lines:
            continue
        old = B.text[B.toks[i][1]:B.toks[z2 - 1][2]]
        body = s1 if s1.rstrip().endswith((";", "}")) else s1 + ";"
        yield ("uneqif", "equal arms `if %s` at line +%d written once" % (B.text[B.toks[i + 1][1]:B.toks[pc][2]][:30], c.lineoff(i)),
               [(B.toks[i][1], B.toks[z2 - 1][2], "{ %s }" % body + "\n" * old.count("\n"))], "")


def _slot_elem(c, name):
    """(stable key, base element, source variable or None, listing line offset or None) for a c2slots name."""
    import c2slots
    s = c.slotbase()
    try:
        i = c2slots.resolve(s, name)
    except SystemExit as e:
        raise RuntimeError(str(e))
    tab = slot_table(s)
    key = next(k for k, v in tab.items() if v["i"] == i)
    nm = c2slots.ename(s, i) or ""
    var = re.sub(r"\$.*$", "", nm).lstrip("_") if nm and not nm.startswith(("tv", "$")) else None
    if var not in c.B.vars:
        var = None
    L = (s["listing"].get(i) or {}).get("line")
    return key, tab[key], var, (c.src_lineoff(L) if L else None)


def g_slot(c, w):
    """Stack-slot what-ifs. The packer counts uses before FinalFlowOpts merges equal arms (slots.md rule 3), so a
    statement written in two equal arms counts its slot references twice and a test that reads a stack local
    counts as one more use and extends its life, while the code (after the merge) is unchanged."""
    sk = w["sk"]
    key, e, var, lo = _slot_elem(c, w["var"])
    B = c.B
    if var:
        sts = _mention_stmts(c, var)
    elif lo is not None:
        sts = [st for st in B.starts if abs(c.lineoff(st) - lo) <= 0]
    else:
        sts = []
    conds = _cond_vars(c)
    out = []
    if sk == "uses" and w["n"] > e["uses"]:
        for W in conds:                                         # S (mentions X) twice: X's uses + its mentions
            out += [g for g in g_eqif(c, W, sts) if g]
        if var:                                                 # the test reads X: one use, 14.00 / 15.00 only
            out += list(g_eqif(c, var, [st for st in B.starts if st > min(B.uses.get(var) or [0])][:8]))
            out += list(g_headdup2(c, var, None)) + list(g_taildup(c, var, None)) + list(g_headdup(c, var, B.starts, None))
    elif sk == "uses":
        out += list(g_uneqif(c, var, None)) + list(g_headhoist(c, var, B.starts, None)) + list(g_tailhoist(c, var, None))
    elif sk == "pos":
        lines = {c.lineoff(st) for st in sts}
        out += list(g_rw(c, "swap", lines))
        for W in conds:
            out += list(g_eqif(c, W, sts))
    elif sk in ("conf+", "conf-"):
        key2, e2, var2, lo2 = _slot_elem(c, w["other"])
        if sk == "conf+":
            for A, Bv in ((var, var2), (var2, var)):
                if not A:
                    continue
                after = [st for st in B.starts if Bv and B.uses.get(Bv) and st > min(B.uses[Bv])]
                out += list(g_eqif(c, A, after[:10]))           # a test of A after B's definition: A live there
                for W in conds:
                    out += list(g_eqif(c, W, [st for st in after if st in _mention_stmts(c, A)][:6]))
        else:
            for A in (var, var2):
                if A:
                    out += list(g_uneqif(c, A, None)) + list(g_tailhoist(c, A, None)) + list(g_headhoist(c, A, B.starts, None))
    seen, res = set(), []
    for g in out:
        k_ = repr(g[2])
        if k_ not in seen:
            seen.add(k_)
            res.append(g)
    return res


def g_pick(c, w):
    """Scratch picks. `ins:N`: a statement written at the head or tail of both arms of an if (or in equal arms) whose
    copies get the same scratch registers is merged after RegSimpleAssignBody: the second copy's picks moved the
    cursor and left no instruction (a hidden pick before the first pick of the arm laid out second). `drop:N`: the
    reverse (a merged copy written once), or the value given a name (a Color variable instead of a pick)."""
    snap, _path = c.pickbase()
    picks = _body_picks(snap)
    byn = {p["n"]: p for p in picks}
    n = w["n"]
    near = [byn[k].get("line") for k in (n - 1, n, n + 1) if k in byn and byn[k].get("line") is not None]
    lines = set()
    for ln in near:
        lines |= {ln - 1, ln, ln + 1}
    out = []
    if w["kind"] == "scratch_ins":
        # the copy that hides the pick is the one in the arm laid out second: a statement right before an if whose
        # second arm starts with pick n can sit far from the picks' lines (Fallout 3 0x00783280: a log call before
        # `if (arg) { ...; return; }`, pick n the first one after the if). Natural spellings first: headret
        hl = lines | _second_arm_heads(c, picks, n)
        out += list(g_headret(c, None, hl))
        out += list(g_headdup2(c, None, hl)) + list(g_headdup(c, None, c.B.starts, hl)) + list(g_taildup(c, None, lines))
        for W in _cond_vars(c, 2):
            out += list(g_eqif(c, W, [st for st in c.B.starts if c.lineoff(st) in lines]))
        out += list(g_uneqif(c, None, lines))
    else:
        out += list(g_headhoist(c, None, c.B.starts, lines)) + list(g_tailhoist(c, None, lines)) + list(g_uneqif(c, None, lines))
        out += list(g_rw(c, "wrap", lines, lambda d: d.startswith("arg hs_cp")))
    seen, res = set(), []
    for g in out:
        k_ = repr(g[2])
        if k_ not in seen:
            seen.add(k_)
            res.append(g)
    return res


def _member_type(c, name):
    """The declared type of a struct member (or variable) `name` in the unit: `E** e;` -> `E**` (first declaration
    found; a heuristic, a wrong type fails to compile and the variant is dropped). Comments are blanked first: a
    trailing `// +0x0c` after the previous member would otherwise sit between its `;` and this declaration."""
    text = re.sub(r"//[^\n]*|/\*.*?\*/", " ", c.text, flags=re.S)
    for m in re.finditer(r"(?:^|[;{}])\s*((?:const\s+|unsigned\s+|signed\s+|struct\s+)*[A-Za-z_][\w:<>]*(?:\s*\*+)?)\s*"
                         r"((?:\*?\s*\w+\s*,\s*)*)\*?\s*\b%s\s*(?:;|,|\[)" % re.escape(name), text):
        ty = m.group(1).strip()
        if ty.split()[0] not in c.rw.KEYWORDS - {"const", "unsigned", "signed", "struct"} and ty not in ("return", "else"):
            return ty
    return None


def _chain_occ(c, chain):
    """Token index ranges [i, j) of the target's occurrences of a member chain (`p->m`, `x.e`, `this->m`)."""
    B = c.B
    parts = re.findall(r"->|\.|[A-Za-z_]\w*", chain)
    out, n = [], len(parts)
    for i in range(len(B.toks) - n + 1):
        if [B.toks[i + k][0] for k in range(n)] == parts and (i == 0 or B.toks[i - 1][0] not in (".", "->", "::")):
            j = i + n
            if j < len(B.toks) and B.toks[j][0] == "(":
                continue                                   # a method call, not the member
            out.append((i, j))
    return out


def g_load(c, w):
    """The load-reuse levers of docs/c2/loadcse.md, which change code on purpose (the score decides):
    alias2:CHAIN   two of CHAIN's later reads through one reference `T& rz_r = CHAIN;` (R3: a reference used twice
                   never shares a load with the direct reads: one load becomes two or three);
    ptrlocal:CHAIN the pointer member loaded into a local before the statement (`T* rz_q = p->q;`, R2: its
                   dereferences keep one alias class: two loads become one);
    subobj:CHAIN   the member read and updated through an inline method of a sub-object (`((rz_sub<T>*)&CHAIN)
                   ->get()`): the update becomes `op [m], y` on 14.00 / 15.00 (C1 -> C2)."""
    B = c.B
    chain = w["chain"]
    if w["lk"] == "byval":
        # rwsweep's byval family: the calls of NAME (`*`: every call) returning a class by value, each through a
        # generic by-value inline forwarding template (Spore 005f0a60)
        out = [(f, d, e, "") for f, d, e in c.rw.gen_byval(B)
               if chain == "*" or re.search(r"(?:^|[^\w])%s\s*\(" % re.escape(chain), d)]
        if not out:
            print("  %s: no call of it returning a class by value in the target" % chain)
        return out[:12]
    occ = _chain_occ(c, chain)
    name = re.findall(r"\w+", chain)[-1]
    ty = _member_type(c, name)
    if not occ or not ty:
        print("  %s: %s" % (chain, "no occurrence in the target" if not occ else "the type of %s was not found" % name))
        return
    st_of = lambda i: max(s for s in B.starts if s <= i)
    out = []
    if w["lk"] == "alias2":
        import itertools
        for (a_, b_) in itertools.combinations(range(1, len(occ)), 2):
            st = st_of(occ[0][0])
            if B.paren[st] or B.toks[st][0] in ("case", "default", "else"):
                continue
            eds = [(B.toks[st][1], B.toks[st][1], "%s& rz_r = %s; " % (ty, chain))]
            eds += [(B.toks[occ[k][0]][1], B.toks[occ[k][1] - 1][2], "rz_r") for k in (a_, b_)]
            out.append(("alias2", "`%s& rz_r = %s;` at line +%d, reads %d and %d of %d through it" % (
                ty, chain, c.lineoff(st), a_ + 1, b_ + 1, len(occ)), eds, ""))
        # one later read through it: a chain read only twice has no pair. The single use is substituted back for an
        # int member, but a pointer member read through a reference bound in an earlier block is reloaded (probes
        # work/scratch/a2probe lc__c, lc__g; Spore 009d3410 `Limb* const& lb`, 00724100); `(void)rz_r;` adds no use
        for k in range(1, len(occ)):
            st = st_of(occ[0][0])
            if B.paren[st] or B.toks[st][0] in ("case", "default", "else") or st_of(occ[k][0]) == st:
                continue
            eds = [(B.toks[st][1], B.toks[st][1], "%s& rz_r = %s; " % (ty, chain)),
                   (B.toks[occ[k][0]][1], B.toks[occ[k][1] - 1][2], "rz_r")]
            out.append(("alias2", "`%s& rz_r = %s;` at line +%d, read %d of %d through it (one use)" % (
                ty, chain, c.lineoff(st), k + 1, len(occ)), eds, ""))
    elif w["lk"] == "ptrlocal":
        seen = set()
        for k, (i, j) in enumerate(occ):
            st = st_of(i)
            if st in seen or B.paren[st] or B.toks[st][0] in ("case", "default", "else"):
                continue
            seen.add(st)
            blk = B.block[st]
            end = B.close.get(blk, len(B.toks)) if blk else len(B.toks)
            later = [(x, y) for x, y in occ if st <= x < end]
            stop = next((x for x, y in later if y < len(B.toks) and B.toks[y][0] in c.rw.ASSIGN), None)
            later = [(x, y) for x, y in later if stop is None or x < stop]
            if len(later) < 2:
                continue
            eds = [(B.toks[st][1], B.toks[st][1], "%s rz_q = %s; " % (ty, chain))]
            eds += [(B.toks[x][1], B.toks[y - 1][2], "rz_q") for x, y in later]
            out.append(("ptrlocal", "`%s rz_q = %s;` at line +%d, %d uses through it" % (ty, chain, c.lineoff(st), len(later)), eds, ""))
    elif w["lk"] == "subobj":
        acc = lambda x, y: (B.toks[x][1], B.toks[y - 1][2], "((rz_sub<%s >*)&%s)->get()" % (ty, chain))
        if len(occ) > 1:
            out.append(("subobj", "every %s (%d) through an inline accessor of a sub-object" % (chain, len(occ)),
                        [acc(x, y) for x, y in occ], ""))
        for k in range(len(occ) - 1):
            out.append(("subobj", "%s at lines +%d and +%d through an inline accessor of a sub-object" % (
                chain, c.lineoff(occ[k][0]), c.lineoff(occ[k + 1][0])), [acc(*occ[k]), acc(*occ[k + 1])], ""))
    return out[:12]



# ---------------------------------------------------------------- register-role answers: interf / copy / pref / canreg
#
# rasim's `interf:A:B`, `nointerf:A:B`, `copy:A:B`, `nocopy:A:B`, `pref:V:REG[:N]`, `noreg:V:REG` and `canreg:V:REG`
# describe the colouring's input, not the IL (regalloc.md 3.3, 4). The constructs below are the source-level ways to
# make that relation appear or go away (docs/c2/realize.md 2.4, measured in tests/data/realize_rel.cpp); every
# candidate is compiled and the Delta of the two traces decides (verdict): the pair, the preference or the register
# set has to change. Names are the trace's: a source variable (the rewrites go to its statements), or a compiler
# local / temporary (`local23`: the statements of the blocks it is referenced in).

REL_KINDS = ("interf", "nointerf", "copy", "nocopy", "pref", "noreg", "canreg")
REG_NAMES = ("eax", "ebx", "ecx", "edx", "esi", "edi", "ebp")


def _rel_sites(c, names):
    """(variables, statement starts): the source variables among `names`, and the statements that mention them or
    sit in the blocks an unnamed value is referenced in (every statement when nothing is known)."""
    B = c.B
    vs = [n for n in names if n in B.vars and n != "this" or n == "this" and c.cls]
    sts = []
    for v in vs:
        sts += _mention_stmts(c, v)
    for n in names:
        if n not in B.vars:
            for blk in sorted(c.base.where.get(n, ())):
                sts += c.stmts(blk)
    sts = sorted(set(sts))
    return vs, sts or list(B.starts)


def g_move(c, sts, vs, span=3):
    """regroles' `move`: one simple statement moved past up to `span` independent neighbours. It ends a value's life
    before a clobber or another value's definition (nointerf / canreg), or lets it reach past it (interf / noreg)."""
    import regroles
    keep = {c.B.toks[st][1] for st in sts}
    pats = [re.compile(r"\b%s\b" % re.escape(v)) for v in vs]
    for fam, desc, edits in regroles.gen_move(c.B, span):
        a_, b_, _r = edits[0]
        txt = c.text[a_:b_]
        if any(p_.search(txt) for p_ in pats) or any(a_ <= x <= b_ for x in keep):
            yield (fam, desc, edits, "")


def g_ptrfield(c):
    """regroles' `ptrfield`: an induction pointer defined before the trip count of the loop that steps a pointer."""
    import regroles
    if not c.a.asm_vs:
        return
    for fam, desc, edits in regroles.gen_ptrfield(c.B, regroles.orig_offsets(c.a.asm_vs)):
        yield (fam, desc, edits, "")


def g_hoist(c, vs, span=4):
    """`T rz_h = V->m;` a few statements above the statement that reads `V->m` (no call between them in the source
    it is read from): V's last use moves up, ahead of the calls and divisions V would otherwise live across (canreg),
    and `rz_h` takes the value's place. Changes the meaning if a call or store in between writes that member, so it is
    only tried with --hoist-mem; rwsweep's `reread` is its reverse."""
    B = c.B
    if not getattr(c.a, "hoist_mem", False):
        return
    for V in vs:
        ty, decl = B.vars.get(V, ("", -1))
        if V == "this" or "*" not in ty:
            continue
        for i in B.uses.get(V, []):
            if i + 2 >= len(B.toks) or B.toks[i + 1][0] != "->" or B.toks[i + 3][0] == "(" or B.written(i, i + 3):
                continue
            m = B.toks[i + 2][0]
            mty = _member_type(c, m)
            if not mty or "[" in mty:
                continue
            st = max((x for x in B.starts if x <= i), default=None)
            if st is None or B.paren[i]:
                continue
            prev = [x for x in B.starts if x < st and B.block[x] == B.block[st] and x > max(decl, 0)
                    and B.toks[x][0] not in ("case", "default", "else")]
            for h in prev[-span:]:
                if _in_loop(B, h):
                    continue
                pos = B.toks[h][1]
                yield ("hoist", "`%s rz_h = %s->%s;` before line +%d, read at line +%d" % (mty, V, m, c.lineoff(h), c.lineoff(st)),
                       [(pos, pos, "%s rz_h = %s->%s; " % (mty, V, m)), (B.toks[i][1], B.toks[i + 2][2], "rz_h")], "")


def _has_call(B, i, j):
    return any(re.match(r"[A-Za-z_]\w*$", B.toks[q][0]) and B.toks[q][0] not in c_KEYWORDS(B) and B.toks[q + 1][0] == "("
               for q in range(i, j - 1))


def c_KEYWORDS(B):
    import rwsweep
    return rwsweep.KEYWORDS | rwsweep.NOCALL


def g_callsplit(c, sts):
    """`X op= f(..);` -> `T rz_r = f(..); X op= rz_r;`: the call's result gets a web of its own, one `mov web, eax`
    copy: `pref:rz_r:eax:+1` (call results are the only copies with eax that Spillcosts counts on the def side).
    The same value, the same order of evaluation; one instruction more on 14.00 / 15.00 (measured, p2 `sep`)."""
    B = c.B
    for st in sts:
        s = _stmt_span(c, st)
        if not s or B.paren[st]:
            continue
        i, j = s
        if B.toks[i][0] not in B.vars or B.toks[i + 1][0] not in ("+=", "-=", "*=", "|=", "&=", "^=") or not _has_call(B, i + 2, j):
            continue
        ty = B.vars[B.toks[i][0]][0]
        if "&" in ty or "[" in ty or not (re.search(SCALAR, ty) or "*" in ty):
            continue
        ty = re.sub(r"\bconst\b", "", ty).strip()
        rhs = B.src(i + 2, j - 1)
        yield ("callsplit", "`%s rz_r = %s;` before `%s %s rz_r` at line +%d" % (ty, rhs[:30], B.toks[i][0], B.toks[i + 1][0], c.lineoff(i)),
               [(B.toks[i][1], B.toks[j - 1][2], "%s rz_r = %s; %s %s rz_r;" % (ty, rhs, B.toks[i][0], B.toks[i + 1][0]))], "")


def g_uncallsplit(c, sts):
    """`T v = f(..); X op= v;` -> `X op= f(..);` (the reverse: v's web and its `mov v, eax` go). v is used once, in
    the next statement, which has no other call."""
    B = c.B
    for st in sts:
        s = _stmt_span(c, st)
        if not s or B.paren[st]:
            continue
        i, j = s
        m = c.rw.DECL.match(B.text, B.toks[i][1])
        if not m or m.group(3) != "=" or m.group(2) not in B.vars or "&" in m.group(1) or not _has_call(B, i, j):
            continue
        v = m.group(2)
        uses = B.uses.get(v, [])
        nxt = _stmt_span(c, j) if j in B.starts else None
        if len(uses) != 1 or not nxt or not (nxt[0] <= uses[0] < nxt[1]) or B.written(uses[0], uses[0] + 1) or \
                _has_call(B, nxt[0], nxt[1]):
            continue
        eq = next(q for q in range(i, j) if B.toks[q][1] >= m.end() - 1)
        rhs = B.src(eq + 1, j - 1)
        yield ("uncallsplit", "`%s %s = %s;` folded into its one use at line +%d" % (m.group(1).strip(), v, rhs[:30], c.lineoff(nxt[0])),
               [(B.toks[i][1], B.toks[j - 1][2], ""), (B.toks[uses[0]][1], B.toks[uses[0]][2], "(%s)" % rhs)], "")


def g_rel(c, w):
    k = w["kind"]
    if k in ("noreg", "canreg") and w["other"] not in ("eax", "ecx", "edx"):
        return []                  # only caller-saved registers are lost to a clobber (a call, shift, division)
    names = [w["var"]] + ([w["other"]] if k != "pref" and k not in ("noreg", "canreg") else [])
    vs, sts = _rel_sites(c, names)
    lines = {c.lineoff(st) for st in sts}
    out = []
    grow = k in ("interf", "noreg")                      # the range of a value must grow
    if k in ("interf", "noreg", "pref", "nointerf", "canreg", "copy", "nocopy"):
        out += list(g_move(c, sts, vs))
    if grow:
        for v in vs:
            first = min(c.B.uses.get(v) or [-1])
            out += list(g_eqif(c, v, [st for st in c.B.starts if st > first]))
    if k in ("nointerf", "canreg"):
        out += list(g_hoist(c, vs))
        for v in vs:
            out += list(g_uneqif(c, v, lines))
        out += list(g_rw(c, "subst", lines)) + list(g_rw(c, "reread", lines)) + list(g_rw(c, "recompute", lines))
        out += list(g_ptrfield(c))
    if k in ("interf", "noreg"):
        out += list(g_ptrfield(c))
        if vs:
            out += list(g_rw(c, "touch", lines, lambda d: any(d.endswith(" %s at body line %s" % (v, d.rsplit(" ", 1)[-1])) for v in vs)))
    if k in ("copy", "pref"):
        for v in (vs or [x for x in c.B.vars if x != "this"]):
            out += list(g_copy(c, v, list(c.B.starts)))
    if k in ("nocopy", "pref"):
        out += list(g_uncopy(c, None, list(c.B.starts)))
    if k == "pref":
        out += list(g_callsplit(c, list(c.B.starts)) if w.get("n", 1) > 0 else g_uncallsplit(c, list(c.B.starts)))
        out += list(g_rw(c, "wrap", lines)) + list(g_rw(c, "subst", lines))
    seen, res = set(), []
    for g in out:
        key = repr((sorted(g[2]), g[3]))
        if key not in seen:
            seen.add(key)
            res.append(g)
    return res


def generate(c, w):
    """Candidate rewrites for one parsed edit, most specific construct first."""
    k = w["kind"]
    out = []
    if k == "load":
        out = g_load(c, w) or []
    elif k == "slot":
        out = g_slot(c, w)
    elif k in PICK_KINDS:
        out = g_pick(c, w)
    elif k in ("ref", "extend", "var", "unref", "drop", "P"):
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
            out += list(g_headdup2(c, V, lines)) + list(g_taildup(c, V, lines))
            if V and k in ("ref", "extend"):
                ls_ = [x for x in c.base.lines_of(blk) if x > 0]
                rng = [st for st in c.B.starts if ls_ and min(ls_) <= c.lineoff(st) <= max(ls_)]
                out += list(g_eqif(c, V, list(dict.fromkeys(sts + rng)) or c.stmts(blk, 1)))
                # the same arms with the test on a value already in a callee-saved register, around a statement of
                # the block that mentions V: V's reference comes from S's second copy, the test reads no memory
                # (for a stack parameter on 13.10, where `(int)V & 1` keeps a memory test)
                msts = [st for st in list(dict.fromkeys(sts + rng)) if st in _mention_stmts(c, V)]
                for W in _cond_vars(c):
                    if W != V:
                        out += [("eqif2",) + g[1:] for g in g_eqif(c, W, msts)]
            if not V:
                out += list(g_rw(c, "const", lines)) + list(g_guard(c, lines))
                out += list(g_rw(c, "wrap", lines, lambda d: d.startswith("arg hs_cp") or d.startswith("hs_bind")))
            if V:
                out += list(g_store(c, V, sts or c.stmts(blk, 1)))
        else:
            out += list(g_uncopy(c, V, sts or c.stmts(blk, 1)))
            out += list(g_uneqif(c, V, lines))
            out += list(g_headdup(c, V, sts, lines)) + list(g_headhoist(c, V, sts, lines))
            out += list(g_tailhoist(c, V, lines))
            if not V or k == "P":
                out += list(g_rw(c, "const", lines)) + list(g_guard(c, lines))
    elif k == "key":
        out += list(g_keys(c, w))
    elif k in REL_KINDS:
        out = g_rel(c, w)
    pick = set(c.a.constructs.split(",")) if c.a.constructs else None
    out = [v for v in out if not pick or v[0] in pick]
    return out[:c.a.limit] if c.a.limit else out


def g_keys_earlier(c, w, nm):
    """sub:CLS@V:n: n fewer keys of that class before V. V's key follows its first IL mention, not its declaration
    (a bare `T V;` moves nothing): written as an initialised declaration `T V = 0;` some statements earlier in its
    block, with the original `T V = e;` turned into `V = e;`, V is allocated before what those statements make
    (the dead initialisation is removed; Spore 00bc9980). One variant per earlier statement of the block."""
    B, rw = c.B, c.rw
    if nm not in B.vars or B.vars[nm][1] < 0:
        print("  %s is not a local declared in the target (a parameter, a temporary or an inline symbol): no construct" % nm)
        return
    ty, dk = B.vars[nm]
    st0 = max(s for s in B.starts if s <= dk)
    sp = _stmt_span(c, st0)
    if not sp or "&" in ty or "[" in ty:
        return
    i, j = sp
    toks = [B.toks[q][0] for q in range(i, j)]
    if toks.count(",") and B.paren[i] == 0 and any(B.toks[q][0] == "," and B.paren[q] == 0 for q in range(i, j)):
        return                                           # several declarators: not rewritten
    init = re.search(SCALAR, ty) or "*" in ty or re.search(r"\b(u?int\d+_t|uint|uint\d+|int\d+|U?INT\d*|LONG|ULONG|HRESULT|ptrdiff_t|intptr_t|uintptr_t)\b", ty)
    if not init:
        return
    dtext = B.text[B.toks[i][1]:B.toks[j - 1][2]]
    m = re.match(r"(.*?)\b%s\b\s*(=\s*(.*?))?;$" % re.escape(nm), dtext, re.S)
    if not m:
        return
    newdecl = ("%s = %s;" % (nm, m.group(3).strip())) if m.group(2) else ""
    zero = "0"
    tyt = re.sub(r"\bconst\b", "", m.group(1)).strip()
    earlier = [s for s in B.starts if s < st0 and B.block[s] == B.block[st0] and B.toks[s][0] not in ("case", "default", "else")]
    for s in reversed(earlier[-8:]):
        pos = B.toks[s][1]
        yield ("early", "`%s %s = %s;` at line +%d (%d statements earlier), the declaration at line +%d made %s" % (
            tyt, nm, zero, c.lineoff(s), len([x for x in earlier if x >= s]), c.lineoff(i), "`%s`" % newdecl if newdecl else "nothing"),
               [(pos, pos, "%s %s = %s; " % (tyt, nm, zero)), (B.toks[i][1], B.toks[j - 1][2], newdecl)], "")


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
    if w["sign"] < 0:
        yield from g_keys_earlier(c, w, nm)
        return
    if w["cls"] != "frame":
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
        # one statement that makes n frame keys at once: an empty inline helper with n by-value parameters
        # (work/scratch/realize2/k1.cpp: +n frame and +n tmp keys, no code, all four builds)
        h0 = c.text.rfind("\n", 0, c.fn[4]) + 1
        hdef = "__forceinline void rz_k%d(%s) {} " % (n, ", ".join("int rz_a%d" % q for q in range(n)))
        yield ("helperparams", "an empty inline helper with %d parameters called at line +%d (+%d frame, +%d tmp keys)" % (
            n, c.lineoff(st), n, n), [(h0, h0, hdef), (pos, pos, "rz_k%d(%s); " % (n, ", ".join("0" * n)))], "")
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
        r = dict(k=k, fam=fam, desc=desc, d=d, f=f, path=path, regs=c.regs_ok(f), rd=c.regdiff(f), bd=c.bdiff(f))
        if want["kind"] == "load":
            r["v"], r["side"] = ("yes" if d.code else "no"), []
            r["note"] = "load-reuse lever: the instructions changed by %d (it is meant to); the score decides" % d.code \
                if d.code else "no instruction changed"
            return r
        if want["kind"] == "slot":
            try:
                sv = slot_snap(path, c.func, c.cl)
            except Exception as e:
                return dict(k=k, err="slots: " + str(e)[-140:], fam=fam, desc=desc)
            r["v"], r["side"], r["note"], r["sd"] = slot_verdict(want, c.slotbase(), sv)
            if r["v"] != "no" and d.code:
                r["v"] = "no"
                r["note"] += "; the instructions changed (code %d)" % d.code
            elif r["v"] == "exact" and d.ident:
                r["v"] = "yes"
                r["note"] += "; registers moved (regs %d)" % d.ident
            ag = slot_agree(sv, c.orig_ins())
            if ag:
                r["note"] += "; %d of %d elements at the original's offset (base %d)" % (ag + (slot_agree(c.slotbase(), c.orig_ins())[0],))
                r["slot_agree"] = ag
            return r
        if want["kind"] in PICK_KINDS:
            try:
                pv, _pp = pick_snap(path, c.func, c.cl)
            except Exception as e:
                return dict(k=k, err="picks: " + str(e)[-140:], fam=fam, desc=desc)
            pd_ = PickDelta(c.pickbase()[0], pv)
            v, notes = pick_verdict(want.get("edits") or [want["text"]], c.pickbase()[1], pd_)
            r["v"], r["side"] = v, pd_.strings()
            r["note"] = "; ".join(notes) or "every pick as rasim predicts"
            if v != "no" and d.code and not d.bag:
                # the same instructions in another order: the scheduler's ties follow the new registers (Fallout 3
                # 0x00783280: the hidden pick also gave the original's argument-load order)
                r["v"] = "yes"
                r["note"] += "; the same instructions in another order (code %d, bag 0)" % d.code
            elif v != "no" and d.code:
                r["v"] = "no"
                r["note"] += "; the instructions changed (code %d)" % d.code
            return r
        if want["kind"] == "key":
            import symkeys
            fs = [x for x in symkeys.trace(path, c.cl) if x.name == c.func]
            r["keys"] = key_delta_f(c.keybase, fs[0], want) if fs else None
            kd = r["keys"]
            ok = kd and kd.get("pred_n") and kd["pred_ok"] == kd["pred_n"]
            r["v"], r["side"] = ("exact" if ok and kd["sort_ok"] == kd["sort_n"] else "yes" if ok else "no"), []
            if want["sign"] < 0 and kd and kd.get("sort_n") is not None:
                # a sub: is realised by moving V, not by deleting symbols: the replay's keys differ by
                # construction, the operand orders are what must come out
                r["v"] = "yes" if kd["sort_ok"] == kd["sort_n"] and kd["flips"] else "no"
            if r["v"] != "no" and d.code:
                r["v"] = "no"
        else:
            r["v"], r["side"] = verdict(want, d)
        return r
    with ThreadPoolExecutor(c.a.jobs) as ex:
        res = list(ex.map(one, range(len(cands))))
    order = {"exact": 0, "yes": 1, "no": 2}
    good = [r for r in res if "err" not in r]
    good.sort(key=lambda r: (r["v"] == "no", r["d"].bag > 0, order[r["v"]], r["d"].bag, r["d"].code, len(r["side"]), r["k"]))
    return good, [r for r in res if "err" in r]


def show(c, r, n=None):
    d = r["d"]
    extra = ""
    if r["regs"] is not None:
        extra += "  wanted registers: %s" % ("YES" if r["regs"] else "no")
    if r["rd"] is not None:
        extra += "  rdiff %d bdiff %s" % (r["rd"], r.get("bd"))
        if r.get("f") is not None:
            extra += " (regdiff %s)" % c.oldreg(r["f"])
    print("  %-5s %-9s code %d bag %d regs %d  %s%s" % (r["v"], r["fam"], d.code, d.bag, d.ident, r["desc"][:90], extra))
    side = r["side"] if r["v"] != "no" else d.strings()
    if side:
        s = " ".join(side)
        print("        %s %s" % ("also:" if r["v"] != "no" else "did:", s if len(s) < 200 else s[:196] + " ..."))
    if d.regs:
        print("        registers: %s" % d.regstr())
    if r.get("keys") is not None:
        print("        %s" % key_str(r["keys"]))
    if r.get("sd") is not None:
        print("        slots: %s; offsets %s" % (" ".join(r["sd"].strings()) or "-", r["sd"].where()[:160]))
    if r.get("note"):
        print("        %s" % r["note"])
    print("        %s" % c2trace.c2build.relpath(r["path"], ROOT))


def hit(c, r):
    if r.get("bd") is not None:
        return r["bd"] == 0
    return bool(r["regs"]) and r["d"].bag == 0


def cmd_edit(c, a):
    print("%s: %d blocks, %d instructions%s" % (c.func, len(c.base.blocks), len(c.base.code),
                                               "" if c.orig is None else ", rdiff %d, bdiff %d (regdiff %d)" % (c.regdiff(c.base), c.bdiff(c.base), c.oldreg(c.base))))
    per = []
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
        for r in good:
            r["edits"], r["tail"], r["for"] = cands[r["k"]][2], cands[r["k"]][3], e
        per.append([r for r in good if r["v"] != "no"][:3])
    if len(per) > 1 and all(per):
        import itertools
        combos = sorted(itertools.product(*per), key=lambda rs: sum(_score(r) for r in rs))
        print("## the %d edits together: %d combinations of the best realisations of each (budget %d)" % (
            len(per), len(combos), a.max_compiles))
        st = dict(ncomp=0, closest=None, done=set(), pool=[])
        for k, rs in enumerate(combos[:a.max_compiles]):
            r = build_combo(c, list(rs), "combined%02d" % k, st)
            if r is not None:
                show(c, r)
                if c.orig is not None and hit(c, r):
                    print("FOUND: %s" % c2trace.c2build.relpath(r["path"], ROOT))
                    break
    print("variants: %s" % c2trace.c2build.relpath(c.out, ROOT))


def parse_answers(text):
    """rasim need / want output -> (wanted registers, [[edit, ..], ..] in the order printed)."""
    wants, answers = {}, []
    pat = re.compile(r"\b((?:ref|unref|drop|extend|var|P|before|after|interf|nointerf|pref|copy|nocopy|noreg|canreg):[^\s,;]+"
                     r"|(?:add|sub):(?:frame|tmp|glob|other|hash)@[^\s,;]+|(?:uses|pos):[^\s,;=]+=\d+\^?|conf[+-]:[^\s,;]+,[^\s;]+"
                     r"|(?:ins|drop):\d+(?::\d+)?\b)")
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
        # an answer of several edits is printed `A  +  B  also moves: ..`: everything before the notes, not only
        # the first column (which dropped the second half: Fallout 3 0x008d5510's `.. + canreg:input:ebx`)
        head = re.split(r"\s{2,}(?!\+\s)(?<!\s\+\s\s)", ln.strip().split("also moves")[0].split("[I:")[0])
        head = " ".join(h for h in head if not re.match(r"(deficit|block \d|\(|prio)", h)) if " + " in ln else head[0]
        eds = pat.findall(head)
        if eds and not ln.strip().startswith("not decidable"):
            answers.append(eds)
    return wants, answers


def partial_answers(c, snap, text, nvars=8, nrel=3, nsrc=2):
    """No answer gives every wanted register: the answers for each wanted register on its own (`rasim need SNAP
    reg:V=R`), the abstract relation kinds first (interf / nointerf / copy / nocopy / pref / noreg / canreg have
    constructs now: realize.md 2.4a), appended in the format parse_answers reads. Each realises one value's colouring
    and moves others, which the measurement against the original judges."""
    pat = re.compile(r"^(\S+) \(pop \d+, prio -?\d+\): now (\w+), wanted (\w+)", re.M)
    out, done = "", 0
    for m in pat.finditer(text):
        v, now, want = m.group(1), m.group(2), m.group(3)
        base = v.split("@p")[0].split("#")[0]
        if re.match(r"(t|imm)\d+$", base) or done >= nvars:
            continue
        r = subprocess.run([RASIM, "need", snap, "reg:%s=%s" % (v, want), "--top", "10"], capture_output=True, text=True).stdout
        _w, ans = parse_answers(r)
        rel = [a_ for a_ in ans if len(a_) == 1 and a_[0].split(":")[0] in REL_KINDS][:nrel]
        src = [a_ for a_ in ans if a_ not in rel and len(a_) == 1][:nsrc]
        if not rel and not src:
            continue
        done += 1
        out += "\n# partial answers for %s -> %s\n" % (v, want)
        for a_ in rel + src:
            out += "  %-40s\n" % a_[0]
    return out


def auto_answers(c, a):
    """regmap against the original, then `rasim want`: the answer list for --from-rasim auto."""
    if not a.asm_vs:
        sys.exit("--from-rasim auto needs --asm-vs (the original's disassembly) to know which registers are wanted")
    snap = os.path.join(c.out, "base.json")
    json.dump(c.base.snap, open(snap, "w"))
    rm = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "c2trace.py"), "regmap", c.base_path, "--func", c.func,
                         "--asm-vs", a.asm_vs, "--asm-func", a.asm_func or c.func, "--all", "--"] + c.cl, capture_output=True, text=True, cwd=ROOT)
    open(os.path.join(c.out, "regmap.txt"), "w").write(rm.stdout)
    r = subprocess.run([RASIM, "want", snap, os.path.join(c.out, "regmap.txt")], capture_output=True, text=True)
    text = r.stdout
    # regmap names swaps (two values in each other's registers): ask the order model for the direction that is
    # violated; its source-level answers go after rasim want's (Fallout 3 0x004ddab0: auto never asked)
    for m in re.finditer(r"^swap: _?(\S+) \((\w+), wants (\w+)\) <-> _?(\S+) \((\w+), wants (\w+)\)", rm.stdout, re.M):
        x, y = m.group(1), m.group(4)
        for first, second in ((x, y), (y, x)):
            q = subprocess.run([RASIM, "need", snap, "%s>%s" % (first, second)], capture_output=True, text=True)
            if "VIOLATED" in q.stdout:
                print("# regmap swap %s <-> %s: rasim need '%s>%s'" % (x, y, first, second))
                text += "\n# rasim need '%s>%s'\n" % (first, second) + q.stdout
    if "nothing found" in text and not a.no_partial:
        text += partial_answers(c, snap, text)
    open(os.path.join(c.out, "answers.txt"), "w").write(text)
    print("# regmap: %s" % (rm.stdout.strip().splitlines() or ["(no output)"])[-1])
    return text


# ---------------------------------------------------------------- answers of several edits, pair search

ABSTRACT = ("before", "after", "interf", "nointerf", "copy", "nocopy", "pref", "noreg", "canreg")


def answer_alternatives(c, ans, snap):
    """An answer (a list of edits) -> ([[parsed edit alternatives] per realisable edit], [edits with no construct]).
    An order edit `before:A:B` / `after:A:B` is replaced by the source-level edits `rasim need 'A>B'` says close
    it (the first three); an edit of another abstract kind (canreg, pref, interf, ..) has no construct and is left
    out: the rest of the answer is still realised (and reported as a half), since the model may be wrong about it
    and a compile decides."""
    alts, unreal = [], []
    for e in ans:
        try:
            w = parse_edit(e)
        except SystemExit:
            unreal.append(e)
            continue
        if w["kind"] in ("before", "after"):
            x, y = (w["var"], w["other"]) if w["kind"] == "before" else (w["other"], w["var"])
            r = subprocess.run([RASIM, "need", snap, "%s>%s" % (x, y)], capture_output=True, text=True)
            subs = [m.group(2) for m in re.finditer(r"^\s+deficit\s+(-?\d+)\s+(\S+)", r.stdout, re.M) if int(m.group(1)) <= 0]
            ws = []
            for s_ in subs[:3]:
                try:
                    ws.append(parse_edit(s_))
                except SystemExit:
                    pass
            if ws:
                alts.append(ws)
            else:
                unreal.append(e)
        elif w["kind"] in ABSTRACT and w["kind"] not in REL_KINDS:
            unreal.append(e)
        else:
            alts.append([w])
    return alts, unreal


def _score(r):
    if r.get("rd") is not None:
        return r["rd"]
    return (0 if r.get("regs") else 1) * 100 + r["d"].bag


def _note_closest(st, r):
    sc = (_score(r), r["path"], r["desc"])
    if st["closest"] is None or sc < st["closest"]:
        st["closest"] = sc


def measure_edit(c, a, w, tag, st):
    """Every construct for one parsed edit, measured once per run (cached by the edit's text)."""
    cache = c.__dict__.setdefault("_meas", {})
    if w["text"] in cache:
        return cache[w["text"]]
    cands = [v for v in generate(c, w) if v[0] != "store" or a.with_store]
    room = max(0, a.max_compiles - st["ncomp"])
    cands = cands[:room]
    if not cands:
        cache[w["text"]] = ([], [])
        return cache[w["text"]]
    st["done"].update(repr((sorted(g[2]), g[3])) for g in cands)
    good, bad = measure(c, cands, w, tag)
    st["ncomp"] += len(cands)
    for r in good:
        r["edits"], r["tail"] = cands[r["k"]][2], cands[r["k"]][3]
        r["for"] = w["text"]
        _note_closest(st, r)
        st["pool"].append(r)
    print("  %s: %d candidates, %d realise it (%d with the same instructions)" % (
        w["text"], len(cands), sum(1 for r in good if r["v"] != "no"), sum(1 for r in good if r["v"] != "no" and r["d"].bag == 0)))
    for r in good[:2]:
        show(c, r)
    cache[w["text"]] = (good, cands)
    return cache[w["text"]]


def build_combo(c, rs, tag, st):
    """One variant with the rewrites of several measured results; None when they overlap or do not compile."""
    eds = [x for r in rs for x in r["edits"]]
    for i_, r in enumerate(rs):
        for q in rs[i_ + 1:]:
            if r["edits"] and q["edits"] and c.rw.overlap(r["edits"], q["edits"]):
                return None
    try:
        f, path = c.build(tag, eds, "".join(r["tail"] for r in rs))
    except (Exception, SystemExit) as e:
        print("  combination failed: %s" % str(e)[-120:])
        st["ncomp"] += 1
        return None
    st["ncomp"] += 1
    r = dict(k=0, fam="+".join(x["fam"] for x in rs), desc=" + ".join(x["desc"][:50] for x in rs), d=Delta(c.base, f),
             f=f, path=path, regs=c.regs_ok(f), rd=c.regdiff(f), bd=c.bdiff(f), v="-", side=[], edits=eds, tail="".join(x["tail"] for x in rs))
    _note_closest(st, r)
    return r


def realise_answer(c, a, alts, tag, st):
    """Realise an answer made of several edits: every edit through its constructs (each alternative of an order
    edit as well), then, for an answer of two or more edits, the products of the best realisations of each
    (three per edit, ranked by their summed scores), within --max-compiles. -> the matching result or None."""
    per = []
    for n, ws in enumerate(alts):
        got = []
        for m, w in enumerate(ws):
            good, cands = measure_edit(c, a, w, "%s%d_%d_" % (tag, n, m), st)
            got += [r for r in good if r["v"] != "no"] or ([r for r in good if r["d"].bag == 0 and not r["d"].empty()][:1])
            if len(alts) == 1:
                for r in good:
                    if hit(c, r):
                        return r
        got.sort(key=lambda r: (_score(r), r["d"].bag))
        if got or len(alts) == 1 or not any(w["kind"] in REL_KINDS for w in ws):
            per.append(got[:3])
        else:
            print("  (no variant realises %s: the rest of the answer is realised without it)" % " / ".join(w["text"] for w in ws))
    if len(per) < 2 or not all(per):
        return None
    import itertools
    combos = sorted(itertools.product(*per), key=lambda rs: sum(_score(r) for r in rs))
    k = 0
    for rs in combos:
        if st["ncomp"] >= a.max_compiles:
            print("  (combinations stopped at --max-compiles %d)" % a.max_compiles)
            break
        r = build_combo(c, list(rs), "%sx%d" % (tag, k), st)
        k += 1
        if r is None:
            continue
        show(c, r)
        if hit(c, r):
            return r
    return None


def pair_search(c, a, st, top=8):
    """No single answer matched: pairs (then triples) of the closest variants measured so far, each of which changed
    the allocation's inputs, ranked by how much each lowered the score on its own (with an original) or by the
    instructions they kept, within --max-compiles. Pairs of rewrites of one edit are skipped (they are alternatives)."""
    if a.pairs == 0 or st["ncomp"] >= a.max_compiles:
        return None
    base = c.regdiff(c.base)
    pool = [r for r in st["pool"] if not r["d"].empty() and r["d"].code <= (a.pair_code if base is not None else 0)]
    if base is not None:
        pool = [r for r in pool if r["rd"] is not None and r["rd"] <= base + a.pair_slack]
    pool.sort(key=lambda r: (_score(r), r["d"].bag))
    uniq, seen = [], set()
    for r in pool:                       # one rewrite measured for several edits counts once
        k_ = repr((r["edits"], r["tail"]))
        if k_ not in seen:
            seen.add(k_)
            uniq.append(r)
    pool = uniq[:top]
    if len(pool) < 2:
        return None
    import itertools
    gain = lambda r: (base - r["rd"]) if base is not None else -r["d"].bag
    combos = []
    for n in (2, 3)[:a.pairs]:
        for rs in itertools.combinations(pool, n):
            if len({r["for"] for r in rs}) < n:
                continue
            combos.append((-sum(gain(r) for r in rs), n, rs))
    combos.sort(key=lambda x: (x[0], x[1]))
    if combos:
        print("## pair search: %d combinations of the %d closest variants (budget %d compiles)" % (
            len(combos), len(pool), max(0, a.max_compiles - st["ncomp"])))
    for k, (_g, n, rs) in enumerate(combos):
        if st["ncomp"] >= a.max_compiles:
            print("  (pair search stopped at --max-compiles %d)" % a.max_compiles)
            break
        r = build_combo(c, list(rs), "pr%03d" % k, st)
        if r is None:
            continue
        show(c, r)
        if hit(c, r):
            return r
    return None


def slot_answers(c, a, top=12, pool=40):
    """The slot model's answers against the original: c2slots' single edits, then pairs of the `pool` best-scoring
    single edits with every single edit (the replay is a few ms), ranked by elements at the original's offset."""
    import c2slots, itertools, io
    s = c.slotbase()
    om, info = c2slots.orig_map(s, s["_asm"], c.orig_ins())
    target = {i: o for i, (o, n, t_) in om.items()}
    score = lambda res: sum(1 for i, o in target.items() if res["offs"].get(i) == o)
    base = c2slots.model_of(s).run(c2slots.base_order(s), record=True)
    b0 = score(base)
    print("# slots: %d of %d elements seen in both at the original's offset" % (b0, len(target)))
    E = s["elems"]
    singles = []
    for i in E:
        if E[i]["param"]:
            continue
        nm = "#%d" % i
        for d in (-3, -2, -1, 1, 2, 3):
            u = E[i]["uses"] + d
            if u >= 1:
                singles += ["uses:%s=%d" % (nm, u), "uses:%s=%d^" % (nm, u)]
        lg = base["log"].get(i) or {}
        for _c, why in lg.get("tries", []):
            if isinstance(why, (list, tuple)) and why[0] == "conf":
                singles.append("conf-:%s,#%d" % (nm, why[1]))
        if lg and not lg.get("new"):
            for m in base["created"][lg["res"]]["members"][:4]:
                if m != i:
                    singles.append("conf+:%s,#%d" % (nm, m))
        order = c2slots.base_order(s)
        run = [k for k, j in enumerate(order) if E[j]["size"] == E[i]["size"]]
        singles += ["pos:%s=%d" % (nm, k) for k in run[:: max(1, len(run) // 12)]]

    def sc(eds):
        try:
            res, _ = c2slots.run_edits(dict(s, listing=dict(s["listing"])), eds)
        except (SystemExit, Exception):
            return -1
        return score(res)
    one = sorted(((sc([e]), e) for e in singles), key=lambda x: -x[0])
    rows = [(v, [e]) for v, e in one if v > b0]
    for (v1, e1), e2 in itertools.product(one[:pool], singles):
        if e2 == e1 or e2.split("=")[0].split(",")[0] == e1.split("=")[0].split(",")[0]:
            continue
        v = sc([e1, e2])
        if v > max(b0, v1):
            rows.append((v, sorted([e1, e2])))
    best, seen = [], set()
    for v, eds in sorted(rows, key=lambda r: (-r[0], len(r[1]))):
        if tuple(eds) in seen:
            continue
        seen.add(tuple(eds))
        best.append((v, eds))
    for v, eds in best[:top]:
        print("#   %3d of %d  %s   (%s)" % (v, len(target), " ".join(eds), ", ".join(
            c2slots.ename(s, int(x[1:])) for x in re.findall(r"#\d+", " ".join(eds)))))
    return [eds for v, eds in best[:top]]


def scratch_wrong(snap, tgt):
    """[(pick n, temp, our register, the original's)] for the body run's picks whose register differs from the
    target's (c2trace.py scratch --phase), in pick order."""
    try:
        s, t = json.load(open(snap)), json.load(open(tgt))
    except (OSError, ValueError):
        return []
    runs = s.get("runs") or []
    body = next((r for r in runs if r.get("body")), runs[0] if runs else None)
    if body is None:
        return []
    ours = {p["n"]: p for p in body.get("picks", [])}
    return [(k, ours[k].get("tmp") or "?", ours[k].get("reg"), r) for k, r in t.get("want", [])
            if k in ours and ours[k].get("reg") != r]


def scratch_answers(c, a, top=12, pool=8):
    """The scratch model's answers against the original: `c2trace.py scratch --phase` (our picks against the
    original's registers) and rasim's re-alignment scan of single inserted / removed picks, then the scan again on
    top of each of the best `pool` singles (pairs), ranked by known picks on the original's register."""
    d = os.path.join(c.out, "base", "phase")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "c2trace.py"), "scratch", c.base_path, "--func", c.func,
                        "--phase", a.asm_vs, "--asm-func", a.asm_func or c.func, "--by", "block", "--keep", d, "--brief", "--top", "60", "--"] + c.cl,
                       capture_output=True, text=True, cwd=ROOT)
    open(os.path.join(c.out, "phase.txt"), "w").write(r.stdout + r.stderr)
    snap, tgt = os.path.join(d, "scratch_snap.json"), os.path.join(d, "scratch_snap_target.json")
    if not os.path.exists(tgt):
        print("# scratch: no phase report (%s)" % (r.stderr.strip().splitlines() or ["?"])[-1])
        return []
    return scratch_scan(snap, tgt, top, pool)


def scratch_scan(snap, tgt, top=12, pool=8):
    """rasim's re-alignment scan of a `scratch --phase` snapshot and target (singles, then pairs), printed and
    returned as answers ranked by known picks on the original's register (scratch_answers)."""
    def scan(given):
        q = subprocess.run([RASIM, "scratch", snap, "--target", tgt, "--top", "60"] + given, capture_output=True, text=True).stdout
        m = re.search(r"baseline: (\d+) of (\d+) known picks", q)
        b = int(m.group(1)) if m else 0
        n = int(m.group(2)) if m else 0
        out = [(b + int(x.group(2)), x.group(1)) for x in re.finditer(r"^edit (\S+) .*?picks ([+-]\d+)", q, re.M)]
        return b, n, out
    b0, n, one = scan([])
    wrong = scratch_wrong(snap, tgt)
    print("# scratch: %d of %d known picks on the original's register%s" % (b0, n, "; wrong: " + ", ".join(
        "pick %d %s (ours %s, original %s)" % w for w in wrong[:6]) if wrong else ""))
    rows = [(v, [e]) for v, e in one if v > b0]
    # pairs: on top of the best `pool` singles, and of every single at the first wrong pick. An inserted pick there
    # fixes that pick but shifts the rotation of every later one, so on its own it scores low (Mount & Blade
    # 0x406e50: ins:6 = 7 of 18, ins:6 + ins:8 = 18 of 18), and the best singles are late edits that cannot fix it.
    near = set() if not wrong else {"%s:%d" % (k, m) for k in ("ins", "drop")
                                    for m in range(max(1, wrong[0][0] - 1), wrong[0][0] + 2)}
    seeds = sorted(one, key=lambda x: -x[0])[:pool]
    seeds += [x for x in one if x[1] in near and x not in seeds]
    for v1, e1 in seeds:
        _b, _n, two = scan([e1])
        delta = v1 - _b
        rows += [(v + delta, sorted([e1, e2])) for v, e2 in two if v + delta > max(b0, v1)]
    best, seen = [], set()
    for v, eds in sorted(rows, key=lambda r: (-r[0], len(r[1]))):
        if tuple(eds) not in seen:
            seen.add(tuple(eds))
            best.append((v, eds))
    for v, eds in best[:top]:
        print("#   %3d of %d  %s" % (v, n, " ".join(eds)))
    return [eds for v, eds in best[:top]]


def cmd_pipeline(c, a):
    if a.from_model:
        if not a.asm_vs:
            sys.exit("--from %s needs --asm-vs (the original's disassembly)" % a.from_model)
        text = ""
        answers0 = slot_answers(c, a) if a.from_model == "slots" else scratch_answers(c, a)
    elif a.from_rasim == "auto":
        text = auto_answers(c, a)
    else:
        text = sys.stdin.read() if a.from_rasim == "-" else open(a.from_rasim).read()
    wants, answers = parse_answers(text)
    if a.from_model:
        answers = answers0
    else:
        # source-level edits first, then the relation answers (they have constructs: 2.4a), the order answers last
        # (each needs a second query to become source edits)
        kind_rank = lambda ans: 2 if any(e.split(":")[0] in ("before", "after") for e in ans) else \
            1 if any(e.split(":")[0] in REL_KINDS for e in ans) else 0
        answers = sorted(answers, key=kind_rank)
    if a.from_rasim == "auto" and os.path.exists(os.path.join(c.out, "regmap.txt")):
        for ln in open(os.path.join(c.out, "regmap.txt")):
            m = re.match(r"(\S+)\s+(\S+)\s+.*WANTS (\w+)", ln)
            if m:
                wants.setdefault(m.group(2), m.group(3))
    if a.regmap:
        for ln in open(a.regmap):
            m = re.match(r"(\S+)\s+(\S+)\s+.*WANTS (\w+)", ln)
            if m:
                wants[m.group(2)] = m.group(3)
    for v, r in wants.items():
        c.wantregs.setdefault(v, r)
    print("%s: %d blocks, %d instructions%s; wanted: %s; %d answers read" % (
        c.func, len(c.base.blocks), len(c.base.code), "" if c.orig is None else ", rdiff %d, bdiff %d (regdiff %d)" % (c.regdiff(c.base), c.bdiff(c.base), c.oldreg(c.base)),
        " ".join("%s=%s" % kv for kv in c.wantregs.items()) or ("the %s model's target (above)" % a.from_model if a.from_model
                                                                 else "(none: give --want-regs or --asm-vs)"), len(answers)))
    st = dict(ncomp=0, closest=None, done=set(), pool=[])
    tried, nocon, seen = 0, [], set()
    snap = os.path.join(c.out, "base.json")
    if not os.path.exists(snap):
        json.dump(c.base.snap, open(snap, "w"))
    for ai, ans in enumerate(answers):
        if tried >= a.max_answers:
            break
        if tuple(ans) in seen:
            continue
        seen.add(tuple(ans))
        alts, unreal = answer_alternatives(c, ans, snap)
        if not alts:
            nocon.append(" + ".join(ans))
            continue
        if st["ncomp"] >= a.max_compiles:
            print("## stopping at --max-compiles %d" % a.max_compiles)
            break
        tried += 1
        print("## answer %d: %s%s" % (tried, " + ".join(ans), "   (no construct for %s: realised without it)" % ", ".join(unreal)
                                      if unreal else ""))
        found = realise_answer(c, a, alts, "a%d_" % tried, st)
        if found:
            print("FOUND after %d answers, %d compiles: %s (%s)" % (tried, st["ncomp"], c2trace.c2build.relpath(found["path"], ROOT), found["desc"]))
            return 0
    found = pair_search(c, a, st)
    if found:
        print("FOUND by a pair of realisations after %d answers, %d compiles: %s (%s)" % (
            tried, st["ncomp"], c2trace.c2build.relpath(found["path"], ROOT), found["desc"]))
        return 0
    ncomp, closest, done = st["ncomp"], st["closest"], st["done"]
    if a.from_model:
        print("NOT FOUND in %d answers, %d compiles%s" % (tried, ncomp, "" if closest is None else
                                                        "; closest: %s (%s)" % (c2trace.c2build.relpath(closest[1], ROOT), closest[2])))
        return 1
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
    norder, refwant = 0, []
    for ans in answers:
        if len(ans) == 1 and ans[0].split(":")[0] in ("before", "after") and norder < 3:
            # an order answer: ask the priority model where a reference more or fewer would close the gap
            k, x, y = ans[0].split(":")[:3]
            first, second = (x, y) if k == "before" else (y, x)
            r = subprocess.run([RASIM, "need", snap, "%s>%s" % (first, second)], capture_output=True, text=True)
            got = [int(m.group(1)) for m in re.finditer(r"^\s+deficit\s+-?\d+\s+\w+:[^:\s]+:(\d+)", r.stdout, re.M)]
            refwant += [(m.group(1).split("@p")[0].split("#")[0], int(m.group(2))) for m in
                        re.finditer(r"^\s+deficit\s+-?\d+\s+(?:ref|extend):([^:\s]+):(\d+)", r.stdout, re.M)]
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
            if w["kind"] in ("ref", "extend") and (w["var"], w["block"]) not in refwant:
                refwant.append((w["var"], w["block"]))
    cands, seen_e = [], set(done)
    for b in blocks[:16]:
        lines, sts = set(c.base.lines_of(b)), c.stmts(b)
        gens = list(g_guard(c, lines)) + list(g_rw(c, "const", lines)) + list(g_headhoist(c, None, sts, lines)) + \
            list(g_headdup(c, None, sts, lines)) + list(g_uncopy(c, None, sts)) + list(g_headdup2(c, None, lines)) + \
            list(g_taildup(c, None, lines)) + list(g_tailhoist(c, None, lines)) + list(g_rw(c, "swap", lines))
        for v in [v for v, bb in refwant if bb == b][:3]:
            gens += list(g_eqif(c, v, sts[:2]))
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
                print("FOUND by a structural rewrite after %d answers, %d compiles: %s (%s)" % (tried, ncomp, c2trace.c2build.relpath(r["path"], ROOT), r["desc"]))
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
            # each `copy` rewrite declares `rz_c`: number them apart, or the combination redeclares one name and
            # renames every copied variable's later uses to it
            edits, nc = [], 0
            for r in pick:
                es = cands[r["k"]][2]
                if any(re.search(r"\brz_c\b", e[2]) for e in es):
                    if nc:
                        es = [(a, b, re.sub(r"\brz_c\b", "rz_c%d" % nc, t)) for a, b, t in es]
                    nc += 1
                edits += es
            try:
                fn, path = c.build("st_all_" + f, edits, "".join(cands[r["k"]][3] for r in pick))
            except (Exception, SystemExit) as e:
                print("  %d %s rewrites together: failed (%s)" % (len(pick), f, str(e)[-100:]))
                continue
            ncomp += 1
            r = dict(k=0, fam=f, desc="%d code-neutral %s rewrites together: %s" % (len(pick), f, "; ".join(x["desc"][:40] for x in pick[:4])),
                     d=Delta(c.base, fn), f=fn, path=path, regs=c.regs_ok(fn), rd=c.regdiff(fn), bd=c.bdiff(fn), v="-", side=[])
            show(c, r)
            if hit(c, r):
                print("FOUND by %d %s rewrites together after %d answers, %d compiles: %s" % (len(pick), f, tried, ncomp, c2trace.c2build.relpath(path, ROOT)))
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
                print("FOUND by a callee body after %d answers, %d compiles: %s (%s)" % (tried, ncomp, c2trace.c2build.relpath(r["path"], ROOT), r["desc"]))
                return 0
            sc = (r["rd"] if r["rd"] is not None else (0 if r["regs"] else 1) * 100 + r["d"].bag, r["path"], r["desc"])
            if closest is None or sc < closest:
                closest = sc
    print("NOT FOUND in %d answers, %d compiles%s" % (tried, ncomp, "" if closest is None else
                                                    "; closest: %s (%s)" % (c2trace.c2build.relpath(closest[1], ROOT), closest[2])))
    return 1


# ---------------------------------------------------------------- cl 17.00 x64: NColor (docs/c2/ncolor.md 10)
#
# On x64 the abstract edits are ncolsim's whatif edits: +app:V:B[:N] / -app:V:B[:N] (appearances of V in NColor
# block B: the priority's k), before:A:B (A's first reference before B's: the lifetime index that breaks a priority
# tie). `ref:V:B` / `unref:V:B` are read as +app / -app of 1. The model that answers is ncolsim (need: the edits that
# give --want-regs; predict: the registers after an edit); the constructs are rwsweep's families (they need no
# x86 trace) at the statements of block B's source lines, plus an equal-arms if; each is compiled, traced, and its
# measured effect compared with the request and its registers with the prediction.

X64_FAMS = ("wrap", "alias", "swap", "split", "cflow", "cmp", "paren", "subst", "unalias", "touch", "const", "guard",
            "reread")


def x64_edit(s):
    """An edit of the command line -> ncolsim edits (None: not an x64 edit)."""
    f = s.split(":")
    if s[:5] in ("+app:", "-app:") and len(f) >= 3:
        return ["%s:%s:%s:%s" % (f[0], f[1], f[2], f[3] if len(f) > 3 else "1")]
    if f[0] in ("ref", "unref") and len(f) >= 3 and f[2].isdigit():
        return ["%sapp:%s:%s:1" % ("+" if f[0] == "ref" else "-", f[1].split("@p")[0].split("#")[0], f[2])]
    if f[0] == "before" and len(f) >= 3:
        return ["before:%s:%s" % (f[1], f[2])]
    if s.startswith("index:") or s.startswith("prio:") or s.startswith("bucket:"):
        return [s]
    if s in ("fpflag", "fpflag:1"):
        return ["fpflag:1"]
    return None


class X64Ctx:
    """The source, the target's body (rwsweep.Body) and the base run (ncolsim.NRun) on cl 17.00 x64."""

    def __init__(self, a, cl):
        import rwsweep, helpersweep, ncolsim
        self.rw, self.N, self.a, self.cl = rwsweep, ncolsim, a, cl
        raw = open(a.src).read()
        hdr = os.path.dirname(os.path.abspath(a.src))
        self.text = re.sub(r'#include "([^"/][^"]*)"', lambda m: '#include "%s"' % os.path.join(hdr, m.group(1))
                           if os.path.exists(os.path.join(hdr, m.group(1))) else m.group(0), raw)
        self.prelude = rwsweep.prelude_for(self.text) + "".join(
            ln + "\n" for ln in PRELUDE_RZ.splitlines()[:2] if not re.search(r"\brz_(g|keep)\b", self.text))
        self.plines = self.prelude.count("\n")
        out = a.keep or os.path.join(ROOT, "work", "realize", tsweep.out_name(a.src, a.func)) + "@" + c2trace.c2build.name()
        shutil.rmtree(out, ignore_errors=True)
        os.makedirs(out)
        self.out, self.n = out, 0
        self.orig = tsweep.orig_instrs(a.asm_vs) if a.asm_vs else None
        self.qfunc = c2trace.func_decl(a.func)
        self.base, _ = self.build("base", [])
        self.func = self.base.func
        self.fn, self.cls = find_def(self.text, self.func, helpersweep, rwsweep)
        self.B = rwsweep.Body(self.text, self.fn, self.cls)
        self.shape0 = [rwsweep._mask(x) for x in self.base.instrs]
        self.keys0 = [tsweep._key(x) for x in self.base.instrs]
        self.breg = ncolsim.regs_of(self.base.f)
        self._cons = None

    def build(self, tag, edits):
        d = os.path.join(self.out, tag)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, tag + ".cpp")
        t = self.text
        for s_, e_, r in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
            t = t[:s_] + r + t[e_:]
        open(p, "w").write(self.prelude + t)
        return self.N.NRun(p, self.qfunc, self.cl, os.path.join(d, "r")), p

    def text_lines(self, b):
        """Block b's source lines in the text's numbering (1-based; the listing's minus the prelude)."""
        return {L - self.plines for L in self.base.lines.get(b, [])}

    def line_of(self, pos):
        return self.text.count("\n", 0, pos) + 1

    def constructs(self):
        """Every rewrite of the families and the x64 equal-arms if: [(family, description, edits)]."""
        if self._cons is None:
            fams = (self.a.constructs or ",".join(X64_FAMS)).split(",")
            out = []
            for fam in fams:
                if fam == "eqif":
                    continue
                if fam in self.rw.FAMILIES:
                    out += list(self.rw.FAMILIES[fam](self.B))
            if "eqif" in fams or not self.a.constructs:
                out += list(self.eqif())
            if "box" in fams or not self.a.constructs:
                out += list(self.box())
            if "fpleaf" in fams or not self.a.constructs:
                out += list(self.fpleaf())
            if "store" in fams or (not self.a.constructs and (self.a.cmd == "edit" and not (self.a.from_ncol or
                                                                self.a.from_rasim) or self.a.with_store)):
                out += list(self.store())
            self._cons = out
        return self._cons

    def eqif(self):
        """`if ((__int64)(V) & 1) { S } else { S }` around a simple statement S of the body, for a scalar or pointer
        V assigned before it: one more appearance of V; on x86 the arms merge after Color (eqif). On x64 the arms
        are still two blocks at NColor: the block structure changes (measured, not predicted)."""
        B = self.B
        for v, (ty, decl) in B.vars.items():
            if v == "this" or ty.rstrip().endswith("&") or not ("*" in ty or
                                   re.search(r"\b(int|long|short|char|unsigned|signed|size_t|__int64|bool)\b", ty)):
                continue
            for st in B.starts:
                if decl >= st or B.toks[st][0] in KEYS_NOSTMT:
                    continue
                if decl >= 0 and not any(i < st for i in B.uses[v]) and not _x64_inited(B, decl):
                    continue
                j = st
                while j < len(B.toks) and not (B.toks[j][0] == ";" and B.paren[j] == B.paren[st]):
                    if B.toks[j][0] in ("{", "}"):
                        break
                    j += 1
                if j >= len(B.toks) or B.toks[j][0] != ";" or self.rw.DECL.match(B.text, B.toks[st][1]):
                    continue
                s_, e_ = B.toks[st][1], B.toks[j][2]
                S = B.text[s_:e_]
                yield ("eqif", "eqif %s at line %d" % (v, self.line_of(s_)),
                       [(s_, e_, "if ((__int64)(%s) & 1) { %s } else { %s }" % (v, S, S))])

    def box(self):
        """`struct { T rz_v; } V = { E };` for a scalar or pointer `T V = E;`, every use V -> V.rz_v: V's symbol is
        then class 6 (an aggregate held in an integer register), which NColPrioritizeCandidates puts in list 0 / 1
        with the xmm candidates: processed before every integer candidate whatever its priority (`bucket:V=aggr`,
        docs/c2/ncolor.md 12; a scope object whose constructor saves and destructor restores does the same). The
        code is unchanged when the struct is not address-taken."""
        B = self.B
        for v, (ty, decl) in B.vars.items():
            if decl < 0 or v == "this" or ty.rstrip().endswith("&") or "[" in ty or not (
                    "*" in ty or re.search(r"\b(int|long|short|char|unsigned|signed|size_t|__int64|bool)\b", ty)):
                continue
            st = max([i for i in B.starts if i <= decl] or [-1])
            if st < 0:
                continue
            j = decl
            while j < len(B.toks) and not (B.toks[j][0] == ";" and B.paren[j] == B.paren[st]):
                if B.toks[j][0] == "," and B.paren[j] == B.paren[st]:
                    break
                j += 1
            if j >= len(B.toks) or B.toks[j][0] != ";":
                continue
            ed = [(B.toks[st][1], B.toks[decl][2], "struct { %s rz_v; } %s" % (ty, v))]
            if decl + 1 < j and B.toks[decl + 1][0] == "=":
                ed.append((B.toks[decl + 1][1], B.toks[j][1], "= { %s }" % B.text[B.toks[decl + 2][1]:B.toks[j][1]].strip()))
            ed += [(B.toks[i][1], B.toks[i][2], "%s.rz_v" % v) for i in B.uses[v]]
            yield ("box", "%s held in a one-member struct (line %d)" % (v, self.line_of(B.toks[st][1])), ed)

    def fpleaf(self):
        """`float rz_f = 0.0f; (void)rz_f;` before the body's first statement: a float leaf that the optimiser
        removes before code generation, but OtlScanFuncLeaves has already set FUNC+0xe4 & 0x80 ("uses floating
        point"). With that flag NColMdNoFoldBenefit no longer counts its +1 when it weighs folding an `lea T <- [B +
        I]` into T's address uses, so NColFoldTooCheap folds the lea away: B and I stay live in two registers and the
        uses address [B+I] (a TLS variable: [base+secrel], the secrel in a callee-saved register;
        docs/c2/tls17.md). The same flag stops NColMdAdjustPriority (ncolor.md 12.4). `(void)0.0f;` and other
        statements without an assignment are dropped by the front end and do nothing; `if (0.0f) {}` works too."""
        B = self.B
        if not B.starts:
            return
        pos = B.toks[B.starts[0]][1]
        yield ("fpleaf", "dead float local before line %d (FUNC+0xe4 & 0x80)" % self.line_of(pos),
               [(pos, pos, "float rz_f = 0.0f; (void)rz_f; ")])

    def store(self):
        """`rz_keep(V);` (a store of V to a global) at every statement where V is in scope and assigned: exactly one
        appearance of V in that statement's block and nothing else at NColor when V is live there. One instruction
        of code: a diagnosis (the original references V in that block), never a match."""
        B = self.B
        for v, (ty, decl) in B.vars.items():
            if v == "this" or ty.rstrip().endswith("&") or "[" in ty:
                continue
            for st in B.starts:
                if decl >= st or B.toks[st][0] in ("case", "default", "else"):
                    continue
                if decl >= 0 and not any(i < st for i in B.uses[v]) and not _x64_inited(B, decl):
                    continue
                if decl >= 0 and B.block[decl] and not (B.block[decl] < st < B.close.get(B.block[decl], 1 << 30)):
                    continue
                pos = B.toks[st][1]
                yield ("store", "rz_keep(%s); at line %d" % (v, self.line_of(pos)), [(pos, pos, "rz_keep(%s); " % v)])

    def mentions(self, c, names):
        pat = re.compile(r"\b(%s)\b" % "|".join(map(re.escape, names)))
        return any(pat.search(r) or pat.search(self.text[s_:e_]) for s_, e_, r in c[2])

    def select(self, edit):
        """The constructs that may realise one ncolsim edit: +app / -app V B: rewrites that mention V on a source
        line of block B; before:A:B: rewrites that mention A or B (statement order)."""
        f = edit.split(":")
        if f[0] in ("+app", "-app"):
            v, b = f[1], int(f[2])
            lines = self.text_lines(b)
            sel = [c for c in self.constructs()
                   if self.mentions(c, [v]) and any(self.line_of(s_) in lines for s_, _e, _r in c[2])
                   and c[0] != "box"
                   and (c[0] not in ("eqif", "store") or re.match(r"(eqif |rz_keep\()%s\b" % re.escape(v), c[1]))]
            # a source line shared by several blocks (`if (m) { g(u); }` on one line, a for header): lines of B alone
            # first; on a line B shares with an earlier block the later positions, with a later block the earlier
            share = lambda L: [k for k in self.base.lines if k != b and L in self.text_lines(k)]

            def rank(c):
                pos = min(s_ for s_, _e, _r in c[2])
                sh = share(self.line_of(pos))
                return (len(sh), -pos if sh and min(sh) < b else pos)
            return sorted(sel, key=rank)
        if f[0] == "before":
            # the statement / declaration order first: it moves the first references (the lifetime index) and is
            # code-neutral more often than the wrappers
            fam = ("swap", "split", "alias", "eqif", "wrap")
            return sorted([c for c in self.constructs() if c[0] in fam and self.mentions(c, f[1:3])],
                          key=lambda c: fam.index(c[0]))
        if f[0] == "bucket":                     # bucket:V=..: the box of V
            v = f[1].split("=")[0]
            return [c for c in self.constructs() if c[0] == "box" and c[1].split()[0] == v]
        if f[0] == "fpflag":
            return [c for c in self.constructs() if c[0] == "fpleaf"]
        return []

    def measure(self, tag, edits):
        r, p = self.build(tag, edits)
        d = self.N.run_delta(self.base.f, r.f)
        shape = adiff([self.rw._mask(x) for x in r.instrs], self.shape0)
        regs = adiff([tsweep._key(x) for x in r.instrs], self.keys0)
        return dict(r=r, path=p, d=d, shape=shape, regs=regs, got=self.N.regs_of(r.f),
                    sc=(tsweep.rdiff(r.instrs, self.orig), tsweep.bdiff(r.instrs, self.orig)) if self.orig is not None else None)


def x64_lea_folds(f):
    """The lea definitions NColFoldTooCheap folded into their uses: tuples that are an `lea` in the IL at
    NColSubsumption's entry (il_pre) and the empty op577 in the IL after it (il), matched by position (NColTupRemove
    puts a new tuple in the slot; the numbering is the same in both dumps)."""
    ops = lambda k: [ln.split()[2] for ln in f.get(k) or [] if ln.startswith("I ") and len(ln.split()) > 2]
    return sum(1 for a, b in zip(ops("il_pre"), ops("il")) if a == "lea" and b == "op577")


def _x64_inited(B, decl):
    """The declaration at token decl has an initialiser (`T v = E;`): v is assigned from the next statement on."""
    return decl + 1 < len(B.toks) and B.toks[decl + 1][0] == "="


KEYS_NOSTMT = ("case", "default", "return", "break", "continue", "goto", "if", "while", "for", "do", "switch", "else",
               "try", "catch", "throw")


def x64_verdict(req, m):
    """exact (the requested edits and no other input change), yes (them and others: `also`), blocks (the block
    structure changed: not comparable), no."""
    d = m["d"]
    if d["blocks"]:
        return "blocks", []
    got = list(d["edits"])
    ok = True
    for e in req:
        if e.startswith("before:"):
            _k, x, y = e.split(":")[:3]
            order = [c["name"].split("$")[0] for c in sorted(m["r"].f["cands"], key=lambda c: c.get("index", 0))]
            ok = ok and x in order and y in order and order.index(x) < order.index(y)
            got = [g for g in got if not g.startswith("index:")]
        elif e == "fpflag:1":
            # measured by its effect: one more lea folded away by NColFoldTooCheap than in the base
            ok = ok and x64_lea_folds(m["r"].f) > m.get("lea_folds0", 0)
        elif e.startswith("bucket:"):
            # requested by name (aggr / local / ..), measured as the variant's list number
            v = e[7:].split("=")[0]
            want = m.get("want_list", {}).get(e)
            hit = [g for g in got if g.startswith("bucket:%s=" % v) and (want is None or g.endswith("=%d" % want))]
            ok = ok and bool(hit)
            got = [g for g in got if g not in hit]
        elif e in got:
            got.remove(e)
        else:
            ok = False
    if not ok:
        return "no", got
    also = got + ["ranges:" + ",".join(d["ranges"])] * bool(d["ranges"]) + ["live"] * d["live"] + \
        ["new:" + v for v in d["new"]] + ["gone:" + v for v in d["gone"]]
    return ("exact" if not also else "yes"), also


def x64_inputs(c, m):
    """What a variant moved in NColor's inputs, and whether ncolsim predicts its registers from that."""
    d = m["d"]
    if d["blocks"]:
        if m["r"].f.get("il_pre"):         # recomputed from the variant's IL (ncolsim.il_predict)
            pm = c.N.il_predict(m["r"].f)
            ok = all(pm[k] == m["got"].get(k, pm[k]) for k in pm if c.N.named(k))
            return "the block structure (ncolsim from the variant's IL: %s)" % ("as traced" if ok else "differs")
        return "the block structure (not predicted)"
    pm = c.N.predict(c.base.f, d["edits"])
    ok = all(pm[k] == m["got"].get(k, pm[k]) for k in pm if c.N.named(k))
    return "%s (ncolsim from them: %s)" % (" ".join(d["edits"]) or "no appearance / order change",
                                          "as traced" if ok else "differs")


def x64_show(c, tag, desc, m, verdict, also, pred):
    N = c.N
    got = m["got"]
    chg = " ".join("%s:%s->%s" % (k, c.breg[k], got[k]) for k in c.breg if N.named(k) and k in got and got[k] != c.breg[k])
    pm = N.predict(c.base.f, m["d"]["edits"]) if not m["d"]["blocks"] else \
        N.il_predict(m["r"].f) if m["r"].f.get("il_pre") else None
    okreq = None if pred is None else all(pred[k] == got.get(k, pred[k]) for k in pred if N.named(k))
    okm = None if pm is None else all(pm[k] == got.get(k, pm[k]) for k in pm if N.named(k))
    print("  %-8s %-6s code %d regs %d%s  %-6s pred(req) %-4s pred(meas) %-4s  %s  | %s%s" % (
        tag, verdict, m["shape"], m["regs"], "" if m["sc"] is None else "  rdiff %d bdiff %d" % m["sc"],
        "", {True: "ok", False: "MISS", None: "-"}[okreq], {True: "ok", False: "MISS", None: "-"}[okm],
        chg or "(registers unchanged)", desc, ("  also: " + " ".join(also)) if also else ""))
    return okreq, okm


def x64_cmd_edit(c, a):
    reqs = []
    for s in a.edits:
        e = x64_edit(s)
        if e is None:
            sys.exit("realize (x64): %r is not an NColor edit (+app:V:B[:N], -app:V:B[:N], ref:V:B, unref:V:B, "
                     "before:A:B, bucket:V=aggr|local|global|const|N, fpflag:1)" % s)
        reqs.append((s, e))
    N = c.N
    print("base: registers %s" % " ".join("%s=%s" % kv for kv in c.breg.items() if N.named(kv[0])))
    print("blocks -> source lines: %s" % "  ".join("B%d:%s" % (b, ",".join(map(str, sorted(c.text_lines(b))))) for b in
                                                  sorted(c.base.lines) if c.base.lines[b]))
    stats = dict(n=0, exact=0, req_ok=0, req_n=0, meas_ok=0, meas_n=0)
    meas, tried = [], {}
    for s, e in reqs:
        pred = N.predict(c.base.f, e)
        print("## %s  (ncolsim: %s)" % (s, " ".join("%s=%s" % (k, pred[k]) for k in pred if N.named(k) and
                                                       pred[k] != c.breg.get(k)) or "no register change predicted"))
        cons = []
        for x in e:
            cons += [k for k in c.select(x) if k not in cons]
        if a.limit:
            cons = cons[:a.limit]
        if a.list:
            for k in cons:
                print("  %-6s %s" % (k[0], k[1]))
            continue
        print("  %d constructs" % len(cons))
        for k in cons:
            c.n += 1
            tag = "v%04d" % c.n
            try:
                m = c.measure(tag, k[2])
            except (Exception, SystemExit) as ex:
                print("  %-8s failed: %s | %s" % (tag, str(ex)[-100:], k[1]))
                continue
            m["want_list"] = {x: N.bucket_edit(c.base.f, N.lists_of(c.base.f), x)[1] for x in e if x.startswith("bucket:")}
            m["lea_folds0"] = x64_lea_folds(c.base.f)
            v, also = x64_verdict(e, m)
            m.update(src_edits=list(k[2]), tag=tag, desc="%s %s" % (k[0], k[1]), verdict=v)
            m["for"] = s
            tried[tuple(sorted(k[2]))] = m
            meas.append(m)
            if v == "no" and not a.all_variants and m["regs"] == 0 and not m["d"]["edits"]:
                continue
            okreq, okm = x64_show(c, tag, "%s %s" % (k[0], k[1]), m, v, also, pred if v == "exact" else None)
            stats["n"] += 1
            stats["exact"] += v == "exact"
            if okreq is not None:
                stats["req_n"] += 1
                stats["req_ok"] += okreq
            if okm is not None and (m["d"]["edits"] or m["regs"]):
                stats["meas_n"] += 1
                stats["meas_ok"] += okm
    if not a.list:
        print("summary: %d variants shown, %d exact; predicted registers of the requested edit = traced on %d of %d "
              "exact variants; of the measured edits on %d of %d variants that moved an input or a register" % (
                  stats["n"], stats["exact"], stats["req_ok"], stats["req_n"], stats["meas_ok"], stats["meas_n"]))
    if not a.list and len(reqs) >= 2 and a.pairs:
        # several edits: their realisations combined (one per requested edit), ranked by ncolsim (x64_pair_search)
        want = c.rw.parse_want_regs(a.want_regs) if a.want_regs else {}

        def build(tag, edits):
            key = tuple(sorted(edits))
            if key not in tried:
                try:
                    m = c.measure(tag, edits)
                    m["src_edits"], m["tag"] = list(edits), tag
                except (Exception, SystemExit) as ex:
                    print("  %-8s failed: %s" % (tag, str(ex)[-100:]))
                    m = None
                tried[key] = m
            return tried[key]
        r = x64_pair_search(c, a, [m for m in meas if m["verdict"] in ("exact", "yes")], want, build, lim=a.pair_code)
        stats["pair"] = r and r["path"]
        if r is not None:
            print("FOUND (pair search): %s -> %s" % (r["desc"], r["path"]))
    return stats


def x64_pipeline(c, a):
    """--from-ncol: ncolsim.need's answers for --want-regs, each realised by the constructs of its edits (singles,
    then the realising variants of every unit of the answer combined), until a code-neutral variant has the wanted
    registers (FOUND)."""
    import itertools
    N = c.N
    want = c.rw.parse_want_regs(a.want_regs)
    if not want:
        sys.exit("realize (x64): --from-ncol needs --want-regs v=reg,..")
    print("base: registers %s; wanted %s" % (" ".join("%s=%s" % kv for kv in c.breg.items() if N.named(kv[0])),
                                             " ".join("%s=%s" % kv for kv in want.items())))
    # order answers (before: / index:, the lifetime index) first: a statement or declaration order realises them
    # code-neutrally and ncolsim predicts them exactly, while an appearance edit (+app / -app) had no code-neutral
    # construct in the lever benchmarks (docs/c2/ncolor.md 10.3); need() ranks by edit count alone
    answers = N.need(c.base.f, want, limit=8 * max(a.max_answers, 12))
    answers = sorted(answers, key=lambda x: sum(e[:5] in ("+app:", "-app:") for e in x[0]))[:a.max_answers]
    if not answers:
        print("NOT FOUND: ncolsim has no appearance / order edit (singles, pairs) that gives the wanted registers")
        return dict(answers=0, compiled=0, found=None, pred=(0, 0))
    for ed, got, side in answers:
        print("  answer: %-34s predicted %s%s" % (" ".join(ed), " ".join("%s=%s" % (k, got[k]) for k in want),
                                                 ("  side: " + " ".join("%s %s->%s" % s for s in side)) if side else ""))
    compiled, pok, pn = 0, 0, 0
    tried = {}

    def run(tag, edits):
        nonlocal compiled
        key = tuple(sorted(edits))
        if key in tried:
            return tried[key]
        if compiled >= a.max_compiles:
            return None
        compiled += 1
        try:
            m = c.measure(tag, edits)
        except (Exception, SystemExit):
            m = None
        if m is not None:
            m["src_edits"], m["tag"] = list(edits), tag
        tried[key] = m
        return m

    def build(tag, edits):                  # combinations of an answer's realisations and the pair search's
        key = tuple(sorted(edits))          # compiles: outside --max-compiles (one per answer, --pair-compiles)
        if key not in tried:
            try:
                m = c.measure(tag, edits)
                m["src_edits"], m["tag"] = list(edits), tag
            except (Exception, SystemExit) as ex:
                print("  %-8s failed: %s" % (tag, str(ex)[-100:]))
                m = None
            tried[key] = m
        return tried[key]

    def hit(m):                              # code-neutral with the wanted registers, or the original's bytes
        return m is not None and (m["shape"] == 0 and all(m["got"].get(k) == v for k, v in want.items()) or
                                  m["sc"] is not None and m["sc"][1] == 0)

    for ai, (ed, got, side) in enumerate(answers):
        units = []                               # (edit of one appearance / one order, count)
        for e in ed:
            f = e.split(":")
            units.append(("%s:%s:%s:1" % (f[0], f[1], f[2]), int(f[3])) if f[0] in ("+app", "-app") else (e, 1))
        real = {}
        for u, _n in units:
            real[u] = []
            for k in c.select(u):
                if len(real[u]) >= 3 or any(v_ == "exact" and m_["shape"] == 0 for _k, m_, _g, v_ in real[u]):
                    break                    # realised (exactly and code-neutrally, or three times): the budget goes
                    #                          to the next unit and the combination
                c.n += 1
                m = run("v%04d" % c.n, k[2])
                if m is None:
                    continue
                v, also = x64_verdict([u], m)
                if v in ("exact", "yes"):
                    gained = 1
                    if u[:5] in ("+app:", "-app:"):
                        _s, var, b, _one = u.split(":")
                        gained = sum(int(x.split(":")[3]) for x in m["d"]["edits"]
                                     if x.split(":")[:3] == [u[:4], var, b])
                    real[u].append((k, m, gained, v))
                    if len(ed) == 1 and _n == 1:
                        pr = N.predict(c.base.f, ed)
                        ok = all(pr[q] == m["got"].get(q, pr[q]) for q in pr if N.named(q))
                        pn += 1
                        pok += ok
                        x64_show(c, "v%04d" % c.n, "%s %s" % (k[0], k[1]), m, v, also, pr if v == "exact" else None)
                if hit(m):
                    print("FOUND (while realising answer %d, %s): %s %s; measured %s -> %s" % (
                        ai, " ".join(ed), k[0], k[1], x64_inputs(c, m), m["path"]))
                    return dict(answers=len(answers), compiled=compiled, found=m["path"], pred=(pok, pn))
        # combine: per unit the realisations, enough of them for its count, members not overlapping
        if all(real[u] for u, _n in units):
            pick = []
            for u, n in units:
                acc, got_n = [], 0
                for k, m, g, _v in sorted(real[u], key=lambda x: (x[1]["shape"], -x[2])):
                    if g <= 0 or any(c.rw.overlap(k[2], q[2]) for q in pick + acc):
                        continue
                    acc.append(k)
                    got_n += g
                    if got_n >= n:
                        break
                pick += acc
            if pick and (len(pick) > 1 or len(units) > 1 or units[0][1] > 1):
                c.n += 1
                m = build("c%04d" % c.n, [e for k in pick for e in k[2]])     # one per answer, beyond --max-compiles
                if m is not None:
                    pr = N.predict(c.base.f, ed)
                    v, also = x64_verdict(ed, m)
                    ok = all(pr[q] == m["got"].get(q, pr[q]) for q in pr if N.named(q))
                    pn += 1
                    pok += ok
                    x64_show(c, "c%04d" % c.n, " + ".join("%s %s" % (k[0], k[1]) for k in pick), m, v, also,
                             pr if v == "exact" else None)
                    if hit(m):
                        print("FOUND answer %d (%s): %s; measured %s -> %s" % (
                            ai, " ".join(ed), " + ".join(k[1] for k in pick), x64_inputs(c, m), m["path"]))
                        return dict(answers=len(answers), compiled=compiled, found=m["path"], pred=(pok, pn))
        else:
            print("  answer %d: no construct realises %s" % (ai, ", ".join(u for u, _n in units if not real[u])))
    descs = {}
    for ed in answers:
        for e in ed[0]:
            f = e.split(":")
            u = "%s:%s:%s:1" % (f[0], f[1], f[2]) if f[0] in ("+app", "-app") else e
            for k in c.select(u):
                descs.setdefault(tuple(sorted(k[2])), "%s %s" % (k[0], k[1]))
    pool = [dict(m, desc=descs.get(key, m["tag"])) for key, m in tried.items() if m is not None]
    r = x64_pair_search(c, a, pool, want, build)
    if r is not None:
        print("FOUND (pair search): %s; measured %s -> %s" % (r["desc"], x64_inputs(c, r), r["path"]))
        return dict(answers=len(answers), compiled=compiled, found=r["path"], pred=(pok, pn))
    print("NOT FOUND: %d answers, %d compiles; predictions of the realised answers: %d of %d as traced" % (
        len(answers), compiled, pok, pn))
    return dict(answers=len(answers), compiled=compiled, found=None, pred=(pok, pn))


def x64_pair_search(c, a, pool, want, build, lim=None):
    """Pairs (--pairs 2: and triples) of the variants measured so far, as on the x86 builds (pair_search), ranked by
    ncolsim: a member that kept the block structure contributes the NColor edits it measurably made (run_delta), and
    the pair's registers are predicted by adding them up and simulating from the base (predict); members that changed
    the blocks are ranked by what they did on their own. Pool: variants that moved an input or a register, changed at
    most --pair-code instructions with --asm-vs (0 without: the target is a code-neutral variant) and were no worse
    than the base (rdiff + --pair-slack). Ranking: wanted registers predicted (or measured), then the members' summed
    rdiff gain, then fewest other registers moved. The best --pair-compiles are compiled; FOUND is a code-neutral
    variant with the wanted registers, or bdiff 0 against the original. Members realising the same requested edit
    (the `for` key) are alternatives and never paired. -> the found variant (m, with desc) or None."""
    import itertools
    N = c.N
    if not a.pairs or a.pair_compiles <= 0:
        return None
    want = want or {}
    b0 = (tsweep.rdiff(c.base.instrs, c.orig), tsweep.bdiff(c.base.instrs, c.orig)) if c.orig is not None else None
    named_chg = lambda got: [k for k in c.breg if N.named(k) and k in got and got[k] != c.breg[k]]
    if lim is None:
        lim = a.pair_code if c.orig is not None else 0
    P, seen = [], set()
    for m in sorted(pool, key=lambda m: ((m["sc"] or (0, 0)), m["shape"], m["tag"])):
        key = tuple(sorted(m["src_edits"]))
        if key in seen or m["shape"] > lim or not (m["d"]["edits"] or named_chg(m["got"])):
            continue
        if b0 is not None and m["sc"][0] > b0[0] + a.pair_slack:
            continue
        seen.add(key)
        P.append(m)
    whit = lambda got: sum(got.get(k) == v for k, v in want.items())
    P.sort(key=lambda m: (-(whit(m["got"]) - whit(c.breg)), (m["sc"] or (0, 0)), m["shape"]))
    P = P[:a.pair_top]
    combos = []
    for n in (2, 3)[:a.pairs]:
        for ms in itertools.combinations(P, n):
            if any(c.rw.overlap(x["src_edits"], y["src_edits"]) for x, y in itertools.combinations(ms, 2)):
                continue
            fors = [m.get("for") for m in ms if m.get("for")]
            if len(set(fors)) < len(fors):
                continue
            pred = None
            if not any(m["d"]["blocks"] for m in ms):
                try:
                    pred = N.predict(c.base.f, [e for m in ms for e in m["d"]["edits"]])
                except SystemExit:
                    pred = None
            if pred is not None:
                nw = whit(pred)
                side = sum(1 for k in pred if N.named(k) and k not in want and pred[k] != c.breg.get(k))
            else:                         # measured only: a wanted register any member got on its own
                nw = sum(any(m["got"].get(k) == v for m in ms) for k, v in want.items())
                side = len({k for m in ms for k in named_chg(m["got"]) if k not in want})
            gain = sum(b0[0] - m["sc"][0] for m in ms) if b0 is not None else 0
            combos.append(((-nw, -gain, side, pred is None, n, sum(m["shape"] for m in ms)), ms, pred))
    combos.sort(key=lambda t: t[0])
    print("## pair search: %d combinations of the %d closest variants (%d predicted by ncolsim), compiling the best %d" % (
        len(combos), len(P), sum(1 for t in combos if t[2] is not None), min(a.pair_compiles, len(combos))))
    st = dict(n=0, ok=0, pn=0)
    for k, (_key, ms, pred) in enumerate(combos[:a.pair_compiles]):
        m = build("pr%03d" % k, [e for x in ms for e in x["src_edits"]])
        st["n"] += 1
        if m is None:
            continue
        got = m["got"]
        ok = None if pred is None else all(pred[q] == got.get(q, pred[q]) for q in pred if N.named(q))
        if ok is not None:
            st["pn"] += 1
            st["ok"] += ok
        print("  %-6s code %d regs %d%s  pred %-4s  %s  | %s" % (
            "pr%03d" % k, m["shape"], m["regs"], "" if m["sc"] is None else "  rdiff %d bdiff %d" % m["sc"],
            {True: "ok", False: "MISS", None: "-"}[ok],
            " ".join("%s:%s->%s" % (q, c.breg[q], got[q]) for q in named_chg(got)) or "(registers unchanged)",
            " + ".join(x["desc"] for x in ms)))
        if (want and m["shape"] == 0 and all(got.get(q) == v for q, v in want.items())) or \
                (m["sc"] is not None and m["sc"][1] == 0):
            print("  pair search: %d compiled, ncolsim's registers for the pair = traced on %d of %d" % (st["n"], st["ok"], st["pn"]))
            return dict(m, desc=" + ".join(x["desc"] for x in ms))
    if combos:
        print("  pair search: %d compiled, ncolsim's registers for the pair = traced on %d of %d; no match" % (
            st["n"], st["ok"], st["pn"]))
    return None


def x64_cmd_pairs(a, cl):
    """`pairs PROBES.cpp` on x64: one traced compile of the probe file (ncolsim's hooks, every function), and every
    function X__base against each X__<variant>: NColor's inputs moved (run_delta), the registers, whether ncolsim
    predicts the variant's registers from those edits, and the code cost."""
    import rwsweep, c2ncol, ncolsim as N
    d = a.keep or tempfile.mkdtemp(prefix="realize_x64p_")
    os.makedirs(d, exist_ok=True)
    _i, _h, rc, out, text = c2trace.run_trace(a.src, N.trace_hooks(), cl or ["/O2"], keep=d, quiet=True)
    asm = os.path.join(d, os.path.splitext(os.path.basename(a.src))[0] + ".asm")
    fns = {}
    for f in c2ncol.parse_all(text):
        if not f["cands"] or not f["func"] or f["func"] in fns:
            continue
        f.setdefault("regsets", [])
        f["il_fn"] = N.il_fn_lines(text, f["func"])
        fns[f["func"]] = f
    if not fns:
        err = " ".join(re.findall(r"error C\d+:[^\n]*", out))[:200]
        sys.exit("realize (x64) pairs: %s" % (("compile error: " + err) if err else "no function allocated by NColor"))
    by = {}
    for name, f in fns.items():
        m = re.search(r"\??_?(\w+?)__(\w+?)(?:@|$)", name)
        if m:
            by.setdefault(m.group(1), {})[m.group(2)] = f
    rx = re.compile(a.only) if a.only else None
    code = lambda f: (lambda ins: ([rwsweep._mask(x) for x in ins], [tsweep._key(x) for x in ins], ins))(
        tsweep.listing_instrs(asm, f["func"]) if os.path.exists(asm) else [])
    stats = dict(n=0, moved=0, pn=0, pok=0, blocks=0)
    for ctx in by:
        base = by[ctx].get("base")
        if base is None or (rx and not rx.search(ctx)):
            continue
        cb = code(base)
        ga = N.regs_of(base)
        print("## %s: %d candidates, %d instructions; registers %s" % (
            ctx, len(base["cands"]), len(cb[2]), " ".join("%s=%s" % kv for kv in ga.items() if N.named(kv[0]))))
        for v, f in by[ctx].items():
            if v == "base":
                continue
            dl = N.run_delta(base, f)
            gb = N.regs_of(f)
            chg = " ".join("%s:%s->%s" % (k, ga[k], gb[k]) for k in ga if N.named(k) and k in gb and ga[k] != gb[k])
            cv = code(f)
            pred = "-"
            if dl["blocks"]:
                stats["blocks"] += 1
            elif dl["edits"] or chg:
                pm = N.predict(base, dl["edits"])
                ok = all(pm[k] == gb.get(k, pm[k]) for k in pm if N.named(k))
                pred = "ok" if ok else "MISS"
                stats["pn"] += 1
                stats["pok"] += ok
            stats["n"] += 1
            stats["moved"] += bool(dl["edits"] or chg or dl["blocks"])
            flags = ",".join(k for k in ("blocks", "ranges", "live", "new", "gone") if dl[k]) or "-"
            print("  %-10s code %d (regs %d, %+d instr)  inputs %s  [%s]  pred %-4s  %s" % (
                v, adiff(cb[0], cv[0]), adiff(cb[1], cv[1]), len(cv[2]) - len(cb[2]),
                " ".join(dl["edits"]) or "(no appearance / order change)", flags, pred, chg or "(registers unchanged)"))
            if a.diff and adiff(cb[1], cv[1]):
                for ln in difflib.unified_diff(["%s %s" % x for x in cb[2]], ["%s %s" % x for x in cv[2]], lineterm="", n=1):
                    print("        " + ln)
    print("summary: %d variants, %d moved an input or a register (%d the block structure); ncolsim's registers from "
          "the measured edits = traced on %d of %d" % (stats["n"], stats["moved"], stats["blocks"], stats["pok"], stats["pn"]))
    if not a.keep:
        shutil.rmtree(d, ignore_errors=True)
    return stats


def x64_cmd_measure(a, cl):
    import rwsweep, ncolsim as N
    d = tempfile.mkdtemp(prefix="realize_x64_")
    func = c2trace.func_decl(a.func)
    ra = N.NRun(a.src, func, cl, os.path.join(d, "a"))
    rb = N.NRun(a.edits[0], func, cl, os.path.join(d, "b"))
    dl = N.run_delta(ra.f, rb.f)
    print("inputs: %s%s" % (" ".join(dl["edits"]) or "(no appearance / order change)",
                            "".join("  %s: %s" % (k, dl[k]) for k in ("blocks", "ranges", "live", "new", "gone") if dl[k])))
    ga, gb = N.regs_of(ra.f), N.regs_of(rb.f)
    print("registers: %s" % (" ".join("%s:%s->%s" % (k, ga[k], gb[k]) for k in ga if N.named(k) and k in gb and
                                      ga[k] != gb[k]) or "unchanged"))
    if not dl["blocks"]:
        pm = N.predict(ra.f, dl["edits"])
        bad = {k: (pm[k], gb.get(k)) for k in pm if N.named(k) and k in gb and pm[k] != gb[k]}
        print("ncolsim from the measured edits: %s" % ("as traced" if not bad else "differs: " + " ".join(
            "%s model %s traced %s" % (k, x, y) for k, (x, y) in bad.items())))
    pa, pb = {N.short(c["name"]): N.traced_prio(ra.f, c["name"]) for c in ra.f["cands"]}, \
        {N.short(c["name"]): N.traced_prio(rb.f, c["name"]) for c in rb.f["cands"]}
    print("priorities: %s" % (" ".join("%s:%s->%s" % (k, pa[k], pb[k]) for k in pa if N.named(k) and k in pb and
                                       pa[k] != pb[k]) or "unchanged"))
    print("code: shape %d regs %d" % (adiff([rwsweep._mask(x) for x in ra.instrs], [rwsweep._mask(x) for x in rb.instrs]),
                                      adiff([tsweep._key(x) for x in ra.instrs], [tsweep._key(x) for x in rb.instrs])))
    shutil.rmtree(d, ignore_errors=True)
    return dl


def main_x64(a, cl):
    if a.cmd == "measure":
        if len(a.edits) != 1:
            sys.exit("usage: realize.py measure BASE.cpp VARIANT.cpp --func F")
        return x64_cmd_measure(a, cl)
    if a.cmd == "pairs":
        return x64_cmd_pairs(a, cl)
    c = X64Ctx(a, cl)
    if a.from_ncol or a.from_rasim:
        return x64_pipeline(c, a)
    return x64_cmd_edit(c, a)


def run(a, cl):
    c = Ctx(a, cl)
    if a.from_rasim or a.from_model:
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
    ap.add_argument("--asm-func", help="the function in our listing scored against --asm-vs (default --func), as in tsweep")
    ap.add_argument("--keys", action="store_true")
    ap.add_argument("--slots", action="store_true", help="pairs / measure: also trace the stack packer and print what moved")
    ap.add_argument("--scratch", action="store_true", help="pairs / measure: also trace the scratch picks and print what moved")
    ap.add_argument("--keys-from", help="edit add:..: a `symkeys.py export` of the base source (saves the keys trace)")
    ap.add_argument("--only")
    ap.add_argument("--diff", action="store_true", help="pairs / measure: print the listing diff of variants whose code changed")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--max-answers", type=int, default=12)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--keep")
    ap.add_argument("--no-partial", action="store_true", help="--from-rasim auto: do not add the per-register answers when no answer gives every wanted register")
    ap.add_argument("--hoist-mem", action="store_true", help="also try `T rz_h = V->m;` hoisted above the calls (rel answers; changes the meaning if a call or store in between writes V->m)")
    ap.add_argument("--with-store", action="store_true", help="--from-rasim: also try `rz_keep(V);` (one instruction of code: a diagnosis, never a match)")
    ap.add_argument("--max-compiles", type=int, default=60)
    ap.add_argument("--pairs", type=int, default=1, help="pipeline: after the answers, combine the closest variants: "
                    "0 off, 1 pairs, 2 pairs and triples (default 1)")
    ap.add_argument("--pair-code", type=int, default=4, help="pair search: largest code change of a member (default 4)")
    ap.add_argument("--pair-slack", type=int, default=0, help="pair search: a member may be this much worse than the base")
    ap.add_argument("--pair-compiles", type=int, default=8, help="x64 pair search: its own budget of compiles (default 8)")
    ap.add_argument("--pair-top", type=int, default=10, help="x64 pair search: the closest variants combined (default 10)")
    ap.add_argument("--from", dest="from_model", choices=("slots", "scratch"),
                    help="answers from the slot model (c2slots search, single edits and model-ranked pairs) or the "
                         "scratch model (rasim scratch --target scan, singles and pairs); needs --asm-vs")
    ap.add_argument("--no-callee", action="store_true", help="--from-rasim: skip the final callee-body stage")
    ap.add_argument("--constructs", help="comma-separated construct names (default: all catalogued for the edit kind)")
    ap.add_argument("--from-ncol", action="store_true", help="x64 (cl 17.00, NColor): the pipeline with ncolsim's "
                    "answers for --want-regs")
    ap.add_argument("--all-variants", action="store_true", help="x64 edit: also print variants that did nothing")
    a = ap.parse_intermixed_args(argv)
    import ncolsim
    if ncolsim.is_x64():               # cl 17.00 x64: NColor, predicted with ncolsim (docs/c2/ncolor.md 10)
        return main_x64(a, cl)
    if a.cmd == "measure":
        return cmd_measure(a, cl)
    if a.cmd == "pairs":
        return cmd_pairs(a, cl)
    return run(a, cl)


if __name__ == "__main__":
    main()
