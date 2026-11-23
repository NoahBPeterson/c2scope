#!/usr/bin/env python3
"""Which memory loads c2 reused or reloaded, and why; which read-modify-writes it formed.  Doc: docs/c2/loadcse.md

usage: loadcse.py SRC --func F [--c2 BUILD] [--all] [--loc RE] [--json OUT] [-- <cl args>]

One traced compile (c2trace.py il, with the PAS index of every memory leaf: C2TRACE_ILPAS=1) dumps the function at
  V   @GdfInsertArtificialAssigns@4 (its last entry before S2: the hash table sweep 2 uses; the first entry, right
                                    after HshBuildTable, is V1. 14.00 enters it twice with the same IL; on 17.00
                                    sweep 1 runs in between and can rejoin values: "sweep 1 rejoined it")
  S2  @GoptLocalCopyPropagation@4   (its first entry after V1: after sweep 2, the CSE sweep; 17.00 also enters it
                                    before HshBuildTable)
  O   @Opeq@4                       (read-modify-write formation; its input)
  C   @Color@4                      (what register allocation gets; x64: @NColor@4)
  E   @EncEmitFunc@4                (final code, after LateGlobalOptimize and MscClean)
and reports, per memory location read more than once (locations are resolved through address temporaries, so
`[t128+0]` with `t128 = _this + 352` is `[_this+352]`, and `[t131+0]` with `t131 = [_pf]` is `[[_pf+0]+0]`):

  REUSED    the later load was deleted in sweep 2: same hash temp, available on every path (opt.md §4)
  RELOADED  it stayed, with the rule that kept it (docs/c2/loadcse.md §2):
      R1 type        the two loads have different IL types (`:2004` null test against `:1004` dereference)
      R2 pas         same address and type, different points-at-set index (alias class): HshFindEntry compares
                     the INDIR leaf's PAS index (TupCmpLeafs, _FTupCmpIndirPasIndex)
      R3 base        the address is spelled through another variable that is not copy-propagated (a variable
                     defined by an address expression: `T& r = p->m`, `T* q = &p->m`, an inline method's `this`
                     on a sub-object), so the hash of the address differs
      R4 dependent   the base pointer itself has another hash temp (it was renumbered, see its own line)
      R5 kill        same hash temp, but a call, a memory-writing intrinsic (memcpy ..) or a possibly aliasing
                     store lies between (opt.md §5), or a variable of the address is assigned (`*++s`: another
                     address that prints the same)
      R6 path        (only without branch targets) same hash temp, nothing in between in stream order
      R? kept        same hash temp, the earlier read dominates and no path between writes the location, yet
                     sweep 2 kept it (not explained)
  and the final count: loads of that location at Color entry and in the final code (a load that LateGlobalOptimize
  or MscClean turned into a register copy is counted as removed after allocation).

RMW: every store `[m] <- t` whose value t is `X op y` computed just before (add sub and or xor shl shr sar) is
listed with FORMED (Opeq turned it into `op [m], y`) or the reason it was not:
      M1 shared-load    the old value of [m] is a temp that is used more than once (it was CSE'd with another read)
      M2 shared-result  the new value t has other uses (e.g. the same `[m] - 4` is also an address)
      M3 operator       the operator has no memory form (mul, neg, not, y - [m] ...; isel.md §7.2)
      M4 not-adjacent   the computation is not the tuple just before the store
      M5 other-location the operand read is another location than the one stored (or a different spelling)
      M6 mixed-chain    the old value is under a chain of two operators (`(m & ~1) | 2`); 17.00 splits some of
                        these into two RMWs before Opeq (OpeqBitAssigns: "bit-assign split")
x64 (17.00) memory leaves print the location's own hash temp before the base (`[t129+t128:1008+0]`); it is dropped
from the address, and allocated names (`rsi=p$1`) are the variable.  A location read more often in the final code
than at Color entry gets a note (a register copy folded back into a memory operand, e.g. `cmova r, [m]`).
Only builds whose dump has block and label lines give block numbers; all four do.
"""
import argparse, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace, c2il  # noqa: E402

PASSES = [("V", "@GdfInsertArtificialAssigns@4"), ("S2", "@GoptLocalCopyPropagation@4"), ("O", "@Opeq@4"),
          ("C", "@Color@4"), ("E", "@EncEmitFunc@4")]
X64 = c2trace.c2build.get("arch") == "x64"
if X64:    # cl 17.00 x64: NColor allocates (no Color); the plain dump has no notes, so take the extended one (`%+`)
    PASSES[3] = ("C", "@NColor@4")
RMW_OPS = {"OPADD": "add", "OPSUB": "sub", "OPAND": "and", "OPOR": "or", "OPXOR": "xor", "OPSHL": "shl",
           "OPSHR": "shr", "OPSAR": "sar"}
NO_RMW = {"OPMUL", "OPDIV", "OPMOD", "OPNEG", "OPNOT", "OPUDIV", "OPUMOD", "OPROL", "OPROR"}
LOADS = {"OPLOAD", "mov", "OPASSIGN", "movzx", "movsx"}
M6_X64 = ("; 17.00's OpeqBitAssigns splits `([m] & c) | y` into two RMWs only when the old value is a memory "
          "operand and [m] is register- or parameter-based (not through a loaded pointer or a global; loadcse.md 8)"
          if X64 else "")
REGNAME = re.compile(r"[re]?(ax|bx|cx|dx|si|di|sp|bp)|r\d+[dwb]?|[abcd][lh]|[sd]il|[sb]pl|x?mm\d+|cc\w*")


def split_ops(s):
    """Operand list text -> tokens (memory operands contain no spaces, but be safe about brackets)."""
    out, cur, depth = [], "", 0
    for ch in s.strip():
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == " " and depth == 0:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


MEM = re.compile(r"^\[(.*)\](?:@([0-9a-f]+))?:([0-9a-f]+)$")
LEAF = re.compile(r"^(?:(\w+)=)?([^:\[\]]+):([0-9a-f]+)$")


def mem_parts(tok):
    """'[t128:1004+0]@7:1004' -> (names, disp, pas, type) or None.  Names keep their order; registers dropped."""
    m = MEM.match(tok)
    if not m:
        return None
    inner, pas, ty = m.group(1), m.group(2), m.group(3)
    terms, cur, depth = [], "", 0
    for ch in inner:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "+" and depth == 0 and cur and not cur.endswith(("e", "E")) or (ch == "+" and depth == 0 and cur):
            terms.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        terms.append(cur)
    disp, names = 0, []
    for k, t in enumerate(terms):
        if k == len(terms) - 1 and re.fullmatch(r"-?\d+", t):
            disp = int(t)
            continue
        if re.fullmatch(r"t\d+", t) and len(terms) > 1:
            # x64 (17.00): the leaf's +0x24 symbol, printed untyped before the base (`[t129+t128:1008+0]`), is the
            # hash temp of the location itself after HshBuildTable, not an address term
            continue
        scale = None
        if "*" in t:
            t, scale = t.rsplit("*", 1)
        lm = LEAF.match(t)
        nm = re.sub(r"\$\d+$", "", lm.group(2) if lm else t)
        names.append(nm + ("*" + scale if scale else ""))
    return names, disp, pas, ty


def leaf_name(tok):
    m = LEAF.match(tok)
    n = m.group(2) if m else tok
    return re.sub(r"\$\d+$", "", n)     # x64 allocated names: `rsi=p$1` is the variable p


def leaf_type(tok):
    m = MEM.match(tok)
    if m:
        return m.group(3)
    m = LEAF.match(tok)
    return m.group(3) if m else ""


def is_const(tok):
    return re.fullmatch(r"-?\d+:[0-9a-f]+", tok) is not None


def is_temp(n):
    return re.fullmatch(r"t\d+", n) is not None


class Ins:
    __slots__ = ("idx", "tup", "op", "dsts", "srcs", "block", "notes", "line")

    def __init__(self, idx, tup, op, dsts, srcs, block, notes, line):
        self.idx, self.tup, self.op, self.dsts, self.srcs, self.block, self.notes, self.line = \
            idx, tup, op, dsts, srcs, block, notes, line


def parse_stage(lines):
    """-> (instructions, cfg): cfg = {"succ": {block: [blocks]}, "known": bool} from B / L lines and the branch
    notes `s0=L<tuple>` (the extended dump; without notes the targets are unknown and known is False)"""
    ins, block, lab, last = [], 0, {}, {}
    order = [0]
    for ln in lines:
        if ln.startswith("B "):
            block += 1
            order.append(block)
            continue
        if ln.startswith("L "):
            lab[ln.split()[1]] = block
            continue
        if not ln.startswith("I "):
            continue
        head, _, rest = ln.partition(" | ")
        hp = head.split()
        notes = ""
        if " ; " in rest:
            rest, _, notes = rest.partition(" ; ")
        d, _, s = rest.partition("<-")
        ins.append(Ins(len(ins), hp[1], hp[2], split_ops(d), split_ops(s), block, notes, ln))
        last[block] = ins[-1]
    succ, known = {}, True
    for k, b in enumerate(order):
        nxt = order[k + 1] if k + 1 < len(order) else None
        i = last.get(b)
        out = []
        if i is not None and i.op in ("OPGOTO", "jmp", "OPBRANCH", "_jcc", "OPSWITCH"):
            m = re.search(r"s0=L([0-9a-f]+)", i.notes)
            if m and m.group(1) in lab:
                out.append(lab[m.group(1)])
            else:
                known = False
            if i.op in ("OPBRANCH", "_jcc") and nxt is not None:
                out.append(nxt)
        elif i is not None and i.op in ("OPRET", "ret", "_eret"):
            pass
        elif nxt is not None:
            out.append(nxt)
        succ[b] = out
    return ins, {"succ": succ, "known": known, "order": order}


def dominators(cfg):
    order, succ = cfg["order"], cfg["succ"]
    preds = {b: [] for b in order}
    for b, ss in succ.items():
        for x in ss:
            preds.setdefault(x, []).append(b)
    allb = set(order)
    dom = {b: set(allb) for b in order}
    dom[order[0]] = {order[0]}
    changed = True
    while changed:
        changed = False
        for b in order[1:]:
            ps = [dom[p] for p in preds.get(b, []) if p in dom]
            nd = (set.intersection(*ps) if ps else set()) | {b}
            if nd != dom[b]:
                dom[b], changed = nd, True
    return dom


class Stage:
    def __init__(self, lines):
        self.ins, self.cfg = parse_stage(lines)
        self.dom = dominators(self.cfg) if self.cfg["known"] and self.ins else None
        self.by_tup = {i.tup: i for i in self.ins}
        self.defs = {}
        for i in self.ins:
            for d in i.dsts:
                if not d.startswith("["):
                    self.defs.setdefault(leaf_name(d), []).append(i)
        self.uses = {}
        for i in self.ins:
            for s in i.srcs:
                if s.startswith("["):
                    mp = mem_parts(s)
                    for n in (mp[0] if mp else []):
                        self.uses.setdefault(n.split("*")[0], []).append(i)
                else:
                    self.uses.setdefault(leaf_name(s), []).append(i)
            for d in i.dsts:
                if d.startswith("["):
                    mp = mem_parts(d)
                    for n in (mp[0] if mp else []):
                        self.uses.setdefault(n.split("*")[0], []).append(i)

    def last_def(self, name, idx):
        best = None
        for i in self.defs.get(name, ()):
            if i.idx < idx:
                best = i
        if best is None and len(self.defs.get(name, ())) == 1:
            best = self.defs[name][0]          # a loop-carried or hoisted definition
        return best

    def resolve(self, name, idx, depth=0, via=None):
        """name -> (base text, offset, [alias variables passed through])"""
        via = via if via is not None else []
        if depth > 12 or is_const(name + ":0"):
            return name, 0, via
        if not is_temp(name):
            ds = self.defs.get(name, ())
            # variables: `_p` on x86; x64 prints them bare (`p`, `re`): anything that is not a register name
            if (name.startswith("_") or X64 and not REGNAME.fullmatch(name)) and len(ds) == 1:
                b, o, v2 = self._resolve_def(ds[0], depth, via)
                if b is not None and (b != name) and ("+" in b or b.startswith("[") or o or b.startswith("_")):
                    if b.startswith("[") or o or b != name:
                        return b, o, v2 + [name]
            return name, 0, via
        d = self.last_def(name, idx)
        if d is None or not d.srcs:
            # x64 after lowering: an incoming-register temp (`rcx=t135 <-` of the entry pseudo-op) is the parameter
            # it is copied into (`p <- rcx=t135`)
            for u in self.uses.get(name, ()):
                if u.op in ("mov", "OPASSIGN") and u.dsts and len(u.srcs) == 1 and \
                        leaf_name(u.srcs[0]) == name and not u.dsts[0].startswith("[") and \
                        not is_temp(leaf_name(u.dsts[0])):
                    return leaf_name(u.dsts[0]), 0, via
            return name, 0, via
        b, o, v2 = self._resolve_def(d, depth, via)
        return (b, o, v2) if b is not None else (name, 0, via)

    def _resolve_def(self, d, depth, via):
        op, s = d.op, d.srcs
        if op in ("OPADD", "add") and len(s) >= 2 and is_const(s[1]) and s[0].startswith("[") and "s0=&" not in d.notes:
            # a load folded into the add (x64 at Opeq: `t131 <- [rq+0] + 92`)
            return "{" + self.memkey(s[0], d.idx, depth + 1)[0] + "}", int(s[1].split(":")[0]), via
        if op in ("OPADD", "add") and len(s) >= 2 and is_const(s[1]) and not s[0].startswith("["):
            b, o, v = self.resolve(leaf_name(s[0]), d.idx, depth + 1, via)
            return b, o + int(s[1].split(":")[0]), v
        if op in ("OPSUB", "sub") and len(s) >= 2 and is_const(s[1]) and not s[0].startswith("["):
            b, o, v = self.resolve(leaf_name(s[0]), d.idx, depth + 1, via)
            return b, o - int(s[1].split(":")[0]), v
        if op == "lea" and s and s[0].startswith("["):
            mp = mem_parts(s[0])
            if mp and len(mp[0]) == 1 and "*" not in mp[0][0]:
                b, o, v = self.resolve(mp[0][0], d.idx, depth + 1, via)
                return b, o + mp[1], v
            return None, 0, via
        if op in ("OPASSIGN", "mov", "OPLOAD") and len(s) >= 1:
            if s[0].startswith("["):
                if "s0=&" in d.notes:          # an address: `_pf <- _flags ; s0=&`
                    return None, 0, via
                return "{" + self.memkey(s[0], d.idx, depth + 1)[0] + "}", 0, via
            if "s0=&" in d.notes:
                return "&" + leaf_name(s[0]), 0, via
            if is_const(s[0]):
                return None, 0, via
            return self.resolve(leaf_name(s[0]), d.idx, depth + 1, via)
        return None, 0, via

    def memkey(self, tok, idx, depth=0):
        """memory operand -> (canonical key, alias variables passed through, pas, type)"""
        mp = mem_parts(tok)
        if not mp:
            return tok, [], None, ""
        names, disp, pas, ty = mp
        parts, off, via = [], disp, []
        for n in names:
            if "*" in n:
                b, s = n.split("*")
                rb, ro, v = self.resolve(b, idx, depth + 1)
                parts.append("%s*%s" % (rb, s))
                off += ro * int(s)
                via += v
            else:
                rb, ro, v = self.resolve(n, idx, depth + 1)
                parts.append(rb)
                off += ro
                via += v
        return "[%s%+d]" % ("+".join(parts), off), via, pas, ty


def run(src, func, cl, keep=None):
    hk_texts = [p for _, p in PASSES]
    spec = c2il.SPEC + ("+" if X64 and c2il.SPEC.endswith("%") else "")
    os.environ["C2TRACE_ILPAS"] = "1"
    img, hk, rc, out, log = c2trace.run_trace(src, [p + spec for p in hk_texts], cl, keep=keep)
    if rc:
        sys.stdout.write(out)
    passes = c2il.parse(log, hk, func)
    if not passes:
        sys.exit("function %r not seen at any of the passes" % func)
    got = {}
    fn = passes[0][2]
    # each pass is the first entry *after* the previous pass's: 17.00 also runs GoptLocalCopyPropagation in sweep 1,
    # before HshBuildTable / GdfInsertArtificialAssigns, and that entry is not S2 (on 14.00 the order is the same
    # either way)
    def hit(name, p):
        core = re.sub(r"^@|@\d+$", "", p)
        return name == p or re.search(r"\b\??%s@" % re.escape(core), name) or name.strip("@?").startswith(core)

    k = 0
    for tag, p in PASSES:
        for j in range(k, len(passes)):
            name, lines, f = passes[j]
            if f == fn and hit(name, p):
                got[tag] = lines
                k = j + 1
                break
    # V is the hash table sweep 2 works with: the *last* GdfInsertArtificialAssigns entry before S2.  14.00 enters it
    # twice with the same IL; on 17.00 sweep 1 runs in between and can rejoin values (the first entry is kept as V1)
    vs = [j for j, (name, _, f) in enumerate(passes) if f == fn and hit(name, PASSES[0][1])]
    s2 = next((j for j, (name, _, f) in enumerate(passes) if f == fn and vs and j > vs[0] and hit(name, PASSES[1][1])),
              None)
    if vs:
        got["V1"] = passes[vs[0]][1]
        before = [j for j in vs if s2 is None or j < s2]
        got["V"] = passes[before[-1]][1]
    return fn, got


def analyse(fn, got, all_locs=False, locre=None):
    V, S2 = Stage(got.get("V", [])), Stage(got.get("S2", []))
    V1 = Stage(got.get("V1", []))
    O, C, E = Stage(got.get("O", [])), Stage(got.get("C", [])), Stage(got.get("E", []))
    rep = {"function": fn, "locations": [], "rmw": []}
    # ---- loads at V
    loads = []
    for i in V.ins:
        if i.op == "OPLOAD" and i.srcs and i.srcs[0].startswith("[") and i.dsts:
            s2 = S2.by_tup.get(i.tup)
            alive = s2 is not None and s2.op == "OPLOAD"
            key_stage, idx = (S2, s2.idx) if alive else (V, i.idx)
            key, via, pas, ty = key_stage.memkey(s2.srcs[0] if alive else i.srcs[0], idx)
            vkey, vvia, vpas, vty = V.memkey(i.srcs[0], i.idx)
            mp = mem_parts(i.srcs[0])
            v1 = V1.by_tup.get(i.tup)
            first = None          # (hash, pas) at the first hash table when sweep 1 changed them (17.00)
            if v1 is not None and v1.op == "OPLOAD" and v1.dsts and v1.srcs and v1.srcs[0].startswith("["):
                f1 = (leaf_name(v1.dsts[0]), mem_parts(v1.srcs[0])[2] if mem_parts(v1.srcs[0]) else None)
                if f1 != (leaf_name(i.dsts[0]), vpas):
                    first = f1
            loads.append(dict(first_hash=first, tup=i.tup, idx=i.idx, block=i.block, hash=leaf_name(i.dsts[0]), key=key, vkey=vkey,
                              raw=re.sub(r":[0-9a-f]+|@[0-9a-f]+", "", i.srcs[0]),
                              via=vvia or via, pas=vpas, type=vty, alive=alive,
                              base=[n.split("*")[0] for n in mp[0]] if mp else []))
    # canonical location: the S2 key when the load survived; for a deleted load, the key of a surviving load with
    # the same hash, else its V key
    by_hash = {}
    for ld in loads:
        if ld["alive"]:
            by_hash.setdefault(ld["hash"], ld["key"])
    for ld in loads:
        if not ld["alive"]:
            ld["key"] = by_hash.get(ld["hash"], ld["key"])
    # V-stage keys can still name `_pf` where S2 says `_flags`: merge keys that share a hash or a V key
    groups = {}
    alias = {}
    for ld in loads:
        k = alias.get(ld["vkey"], ld["key"])
        alias.setdefault(ld["vkey"], k)
        ld["key"] = k
        groups.setdefault(k, []).append(ld)
    hash_of = {ld["hash"]: ld for ld in loads}

    reach = {}
    if V.dom is not None:
        for b0 in V.cfg["order"]:
            seen, todo = set(), [b0]
            while todo:
                x = todo.pop()
                for y in V.cfg["succ"].get(x, ()):
                    if y not in seen:
                        seen.add(y)
                        todo.append(y)
            reach[b0] = seen

    def on_path(i, a, b):
        if not reach or (a["block"] == b["block"] and a["idx"] < b["idx"]):
            # stream order; and within one block the path from a to b is the tuples between them (not around a loop)
            return a["idx"] < i.idx < b["idx"]
        if i.block == a["block"] and i.idx <= a["idx"] and a["block"] != b["block"]:
            return False
        if i.block == b["block"] and i.idx >= b["idx"]:
            return i.block in reach.get(i.block, ()) and i.block in reach.get(a["block"], ()) and a["block"] != b["block"]
        if i.block == a["block"]:
            return i.idx > a["idx"] and (i.idx < b["idx"] or a["block"] != b["block"])
        if i.block == b["block"]:          # before b in b's own block (e.g. a call at the head of the arm)
            return b["block"] in reach.get(a["block"], ())
        return i.block in reach.get(a["block"], ()) and b["block"] in reach.get(i.block, ())

    def kills_between(a, b):
        out = []
        # variables the address is computed from: assigning one between the reads makes it another address
        # (`*++scan` in a loop: the location prints as [scan+0] both times, but scan moved)
        avars = {n for n in re.findall(r"(?<![\w$])([A-Za-z_][\w$]*)", a["key"]) if not is_temp(n)}
        for i in V.ins:
            if not on_path(i, a, b):
                continue
            hit = [leaf_name(d) for d in i.dsts if not d.startswith("[") and leaf_name(d) in avars]
            if hit:
                out.append("assignment to %s (the address moved)" % hit[0])
            elif i.op in ("OPCALL", "call"):
                out.append("call %s" % (i.notes.replace("s0=", "") or "?"))
            elif i.op == "OPINTRINSIC" and any(d.startswith("pas") for d in i.dsts):
                # an intrinsic that writes memory (its points-at set is a destination): memcpy / memset ..
                m = re.search(r"intrin=(\d+)", i.notes)
                out.append("intrinsic %s" % (m.group(1) if m else "?"))
            elif i.dsts and i.dsts[0].startswith("[") and i.op in ("OPASSIGN", "OPVOLATILEASSIGN"):
                sk, _, spas, _ = V.memkey(i.dsts[0], i.idx)
                same_base = sk.rsplit("+", 1)[0] == a["key"].rsplit("+", 1)[0] and sk != a["key"] and \
                    not re.search(r"\*", sk + a["key"])
                if not same_base:
                    out.append("store %s" % sk)
        return out

    def why(a, b):
        """b is a later load of the same location as a, and b was not deleted"""
        kills = kills_between(a, b)
        extra = "" if a["hash"] == b["hash"] else " (and a new hash temp: %s / %s, PAS %s / %s)" % (
            a["hash"], b["hash"], a["pas"], b["pas"])
        if kills:
            return "R5 kill", "; ".join(kills[:3]) + (" ..." if len(kills) > 3 else "") + " in between" + extra
        if a["hash"] != b["hash"]:
            if a["base"] != b["base"]:
                dep = [n for n in b["base"] if n in hash_of]
                ra = [hash_of[n]["key"] if n in hash_of else n for n in a["base"]]
                rb = [hash_of[n]["key"] if n in hash_of else n for n in b["base"]]
                if dep and ra == rb:
                    return "R4 dependent", "its base %s is a reload of %s (hash %s, not %s): see that location" % (
                        "+".join(b["base"]), rb[0], "+".join(b["base"]), "+".join(a["base"]))
                vv = sorted(set(a["via"]) ^ set(b["via"]))
                return "R3 base", "address spelled %s vs %s%s" % (
                    a["raw"], b["raw"], " (through %s, not copy-propagated)" % ", ".join(vv) if vv else "")
            if a["pas"] != b["pas"]:
                return "R2 pas", "alias class (PAS index) %s / %s, nothing in between" % (a["pas"], b["pas"])
            if a["type"] != b["type"]:
                return "R1 type", "IL types %s / %s" % (a["type"], b["type"])
            return "R? hash", "different hash temp (%s / %s), operands print the same" % (a["hash"], b["hash"])
        if V.dom is not None:
            # with branch targets the earlier read dominates this one (only dominating reads are paired) and no path
            # between them writes the location: by the availability rule sweep 2 should have deleted it
            return "R? kept", "same hash temp %s, the read in block %d dominates and nothing on the paths between " \
                              "writes it, yet sweep 2 kept it (not explained: seen at a join after an if-converted " \
                              "arm, zlib updatewindow, on 14.00 and 17.00)" % (b["hash"], a["block"])
        return "R6 path", "same hash temp %s, nothing between in stream order: killed or absent on another path " \
                          "into block %d (other arm, loop back edge; no PRE)" % (b["hash"], b["block"])

    def final_count(stage):
        n = 0
        for i in stage.ins:
            for s in i.srcs:
                if s.startswith("[") and "s0=&" not in i.notes:
                    k = stage.memkey(s, i.idx)[0]
                    if k == key or k.replace("{", "[").replace("}", "]") == key:
                        n += 1
        return n

    for key, lds in groups.items():
        if locre and not re.search(locre, key):
            continue
        if len(lds) < 2 and not all_locs:
            continue
        ent = {"location": key, "loads": [], "cfg": V.dom is not None}
        first = None
        for ld in lds:
            if first is None:
                st, why_ = "FIRST", ""
                first = ld
            elif not ld["alive"]:
                st, why_ = "REUSED", "same hash temp %s, available" % ld["hash"]
                if ld["first_hash"]:
                    why_ += "; at the first hash table it was %s PAS %s: sweep 1 rejoined it" % ld["first_hash"]
            else:
                prev = [x for x in lds if x["idx"] < ld["idx"]]
                if V.dom is not None:
                    prev = [x for x in prev if x["block"] in V.dom.get(ld["block"], ())]
                if not prev:
                    ent["loads"].append(dict(tuple=ld["tup"], block=ld["block"], hash=ld["hash"], pas=ld["pas"],
                                             type=ld["type"], spelled=ld["raw"], status="FIRST",
                                             why="no earlier load of it dominates block %d" % ld["block"]))
                    continue
                r, d = why(prev[-1], ld)
                st, why_ = "RELOADED " + r, d
            ent["loads"].append(dict(tuple=ld["tup"], block=ld["block"], hash=ld["hash"], pas=ld["pas"],
                                     type=ld["type"], spelled=ld["raw"], status=st, why=why_))
        ent["at_color"] = final_count(C) if C.ins else None
        ent["final"] = final_count(E) if E.ins else None
        if ent["final"] is not None and ent["at_color"] is not None and ent["final"] > ent["at_color"]:
            ent["note"] = ("the final code reads it %d more time(s) than Color's input: a register copy of the value was "
                           "folded back into a memory operand after allocation (e.g. `cmova r, [m]` of an "
                           "if-converted `x = m`), not a reload decision" % (ent["final"] - ent["at_color"]))
        rep["locations"].append(ent)
    # ---- RMW at O
    for k, i in enumerate(O.ins):
        if i.op not in ("OPASSIGN",) or not i.dsts or not i.dsts[0].startswith("[") or not i.srcs:
            continue
        val = leaf_name(i.srcs[0])
        if not is_temp(val):
            continue
        dkey = O.memkey(i.dsts[0], i.idx)[0]
        d = O.last_def(val, i.idx)
        ent = {"store": i.tup, "location": dkey, "value": val}
        formed = False
        for c in C.ins:          # Opeq's result at Color entry: `op [m] <- [m] y`
            if c.dsts and c.dsts[0].startswith("[") and c.op in RMW_OPS.values() and \
                    any(s.startswith("[") and C.memkey(s, c.idx)[0] == C.memkey(c.dsts[0], c.idx)[0] for s in c.srcs) \
                    and C.memkey(c.dsts[0], c.idx)[0] == C.memkey(c.dsts[0], c.idx)[0] and \
                    (c.tup == i.tup or c.tup == (d.tup if d else None)):
                formed = True
                ent.update(status="FORMED", code="%s %s" % (c.op, " ".join(c.srcs[1:2])), tup=c.tup)
        if not formed:
            for c in C.ins:
                if c.dsts and c.dsts[0].startswith("[") and c.op in RMW_OPS.values() and \
                        C.memkey(c.dsts[0], c.idx)[0] == C.memkey(c.dsts[0], c.idx)[0] and \
                        any(s.startswith("[") for s in c.srcs) and \
                        _same_loc(C, c, dkey, O):
                    formed = True
                    ent.update(status="FORMED", code="%s %s" % (c.op, " ".join(c.srcs[1:2])), tup=c.tup)
                    break
        pre = O.ins[d.idx - 1] if d is not None and d.idx > 0 else None
        if pre is not None and pre.op in RMW_OPS and pre.dsts and pre.dsts[0].startswith("[") and \
                O.memkey(pre.dsts[0], pre.idx)[0] == dkey:
            # 17.00 x64: OpeqBitAssigns (OpeqTryBitAssignRewrite) already split `[m] = ([m] op1 c1) op2 c2` into
            # `[m] op1= c1` before Opeq, which then makes `[m] op2= c2` of the rest: two RMWs
            c1 = C.by_tup.get(pre.tup)
            first = "%s %s" % (RMW_OPS[pre.op], " ".join((c1 or pre).srcs[1:2]))
            nxt = [c for c in C.ins[c1.idx + 1:c1.idx + 4] if c.op in RMW_OPS.values() and c.dsts and
                   c.dsts[0].startswith("[") and _same_loc(C, c, dkey, O)] if c1 is not None else []
            if nxt:
                ent.update(status="FORMED", tup=nxt[0].tup,
                           code="%s, then %s %s; bit-assign split by OpeqBitAssigns before Opeq" % (
                               first, nxt[0].op, " ".join(nxt[0].srcs[1:2])))
            elif formed:
                ent["code"] = "%s, then %s; bit-assign split by OpeqBitAssigns before Opeq" % (first, ent["code"])
            else:
                formed = True
                ent.update(status="FORMED", code="%s (bit-assign split by OpeqBitAssigns), the rest stays %s" % (
                    first, d.op), tup=pre.tup)
        if formed:
            rt = ent.get("tup")
            e = E.by_tup.get(rt) if rt else None
            if E.ins and e is not None and not (e.dsts and e.dsts[0].startswith("[")):
                ent["status"] = "FORMED, SPLIT after allocation"
                ent["why"] = ("Opeq made `%s [m], ..` (tuple %s at Color entry), the final code has it as load / op / "
                              "store (13.10: between dop2_CodegenUpdate and dop2_CodegenFinish)" % (e.op, rt))
                ent.pop("code", None)
            rep["rmw"].append(ent)
            continue
        if d is None:
            continue
        if d.op not in RMW_OPS and d.op not in NO_RMW:
            continue
        if d.op in NO_RMW:
            ent.update(status="NOT FORMED M3 operator", why="%s has no memory-destination form" % d.op)
        elif d.idx != i.idx - 1:
            nu = len(O.uses.get(val, ()))
            ent.update(status="NOT FORMED M4 not-adjacent" + (", M2 shared-result" if nu > 1 else ""),
                       why="%s is %d tuples before the store%s" % (d.op, i.idx - d.idx,
                           "; the new value %s has %d uses" % (val, nu) if nu > 1 else ""))
        else:
            reasons = []
            reads = False

            def walk(t, depth, top_op):
                nonlocal reads
                for x in t.srcs:
                    if x.startswith("["):
                        if O.memkey(x, t.idx)[0] == dkey:
                            reads = True
                            if t.op != top_op:
                                reasons.append(("M6 mixed-chain", "%s over %s of %s: Opeq follows only a chain of "
                                                "one operator (deep_opeq)%s" % (top_op, t.op, dkey, M6_X64)))
                        continue
                    n = leaf_name(x)
                    if not is_temp(n) or depth > 6:
                        continue
                    sd = O.last_def(n, t.idx)
                    if not sd or not sd.srcs:
                        continue
                    if sd.op in LOADS and sd.srcs[0].startswith("[") and "s0=&" not in sd.notes:
                        if O.memkey(sd.srcs[0], sd.idx)[0] == dkey:
                            reads = True
                            reasons.append(("M1 shared-load", "the old value is %s = %s, a temp with %d uses (shared "
                                            "with another read, so it is not a memory operand)" % (
                                                n, dkey, len(O.uses.get(n, ())))))
                            if t.op != top_op:
                                reasons.append(("M6 mixed-chain", "%s over %s of %s: Opeq follows only a chain of "
                                                "one operator (deep_opeq)%s" % (top_op, t.op, dkey, M6_X64)))
                    elif sd.op in RMW_OPS or sd.op in NO_RMW:
                        walk(sd, depth + 1, top_op)
            walk(d, 0, d.op)
            nu = len(O.uses.get(val, ()))
            if nu > 1:
                reasons.append(("M2 shared-result", "the new value %s has %d uses" % (val, nu)))
            if not reads:
                reasons.append(("M5 other-location", "no operand of %s reads %s as the store spells it" % (d.op, dkey)))
            if not reasons:
                reasons.append(("M? unknown", "the operand equals the destination; not formed"))
            ent.update(status="NOT FORMED " + ", ".join(r[0] for r in reasons), why="; ".join(r[1] for r in reasons))
        ent["op"] = d.op
        rep["rmw"].append(ent)
    return rep


def _same_loc(C, c, dkey, O):
    k = C.memkey(c.dsts[0], c.idx)[0]
    return k == dkey or k.replace("{", "[").replace("}", "]") == dkey.replace("{", "[").replace("}", "]")


def show(rep):
    print("## %s" % rep["function"])
    if not rep["locations"]:
        print("no location is read twice")
    for ent in rep["locations"]:
        print("%s   loads at Color entry: %s, in the final code: %s%s" % (ent["location"], ent["at_color"], ent["final"],
              "" if ent.get("cfg") else "   (no branch targets in this build's dump: paired in stream order)"))
        if ent.get("note"):
            print("  note: " + ent["note"])
        for ld in ent["loads"]:
            print("  %-22s blk %-3s %-6s pas %-3s :%s  %s%s" % (ld["status"].split(" (")[0], ld["block"], ld["hash"],
                  ld["pas"], ld["type"], ld["spelled"], ("   " + ld["why"]) if ld["why"] else ""))
    if rep["rmw"]:
        print("read-modify-write:")
    for r in rep["rmw"]:
        print("  store %s %s: %s%s" % (r["store"], r["location"], r["status"],
                                       (" (%s)" % r.get("code")) if r.get("code") else ("   " + r.get("why", ""))))


def main(argv):
    ap = argparse.ArgumentParser(prog="loadcse.py", usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    ap.add_argument("--all", action="store_true", help="also list locations read once")
    ap.add_argument("--loc", help="only locations matching this regex")
    ap.add_argument("--json")
    ap.add_argument("--keep")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    fn, got = run(a.src, a.func, cl, keep=a.keep)
    missing = [t for t, _ in PASSES if t not in got]
    if missing:
        print("# not seen at: %s" % ", ".join(missing))
    rep = analyse(fn, got, a.all, a.loc)
    show(rep)
    if a.json:
        json.dump(rep, open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
