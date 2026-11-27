#!/usr/bin/env python3
"""Where an x87 value is rounded: every rounding point of our code and of the original's, paired by dataflow position,
and why cl made ours.  Doc: docs/c2/x87round.md

usage: x87round.py SRC --func F (--asm-vs ORIG.s | --image EXE --va VA) [--asm-func G] [--all] [--show-il] [--keep DIR]
                   [--no-trace] [-- <cl args>]
       x87round.py --orig-only (--asm-vs ORIG.s | --image EXE --va VA)       the original's points, no compile

A rounding point is where an x87 value (80 bits in the register) is forced to float32 / float64 and can only come back
rounded:

  rt    a dword / qword store of an x87 value that is reloaded later (`fstp dword [m]` ... `fld dword [m]`): the value
        went through memory. (A store nothing reloads is an ordinary output, not a point.)
  arg   a dword / qword store into the argument area before a call (`fstp [esp]`, `push ecx; fstp [esp]`).
  ret   the value returned in ST(0) came back from memory (`fstp tv; fld tv; ret`): the return is rounded.

Both sides are read the same way: the code is run through a symbolic x87 stack (no memory model beyond frame slots and
register+offset addresses with register versions), every value gets a dataflow KEY = the expression that makes it with
the round trips taken out (`fmul(fsub(m,m),m)`; commutative operands sorted; loads are `m`), and the two sides' values
are aligned by key sequence (difflib). A rounded value is then compared with the value at the same dataflow position
on the other side: rounded there too (`both`), or kept on the x87 stack (`ours only` / `original only`). A value that
has no counterpart (the expression differs: another association, a folded constant) is `unpaired`.

For each of OUR points the report adds, from the traced compile:
  home       the symbol the value is stored to (listing name: `_r$` a user variable, `tv123` a compiler temporary)
  source     the line and expression (the listing's `; N : text` comments), for the store and for the consumer
  reason     why cl rounded it, one of the classes in docs/c2/x87round.md (float local, float parameter of an inlined
             helper, call between definition and use, spill under x87 stack pressure, cast to float, argument, return,
             volatile / address-taken, float temporary); the evidence is printed after it.  14.00 / 15.00 also read the
             call's kill set from LoAddCallScratchRegs (c2x87's hook): a call to an x87-free callee does not kill.
"""
import argparse, collections, difflib, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace, c2scratch, tsweep

MEMRX = re.compile(r"\b(byte|word|dword|qword|xword|tbyte|fword) ptr (?:(?:fs|gs|es|ds|cs|ss):)?\[(.*)\]$")
SIZES = {"byte": 1, "word": 2, "dword": 4, "qword": 8, "xword": 10, "tbyte": 10, "fword": 6}
REGRX = re.compile(r"\b(e?[abcd]x|[abcd][lh]|e?si|e?di|e?bp|e?sp)\b")
FREG = re.compile(r"^st\((\d)\)$")
JCC = re.compile(r"^j(?!mp)\w+$")
COMM = {"fadd", "fmul"}


def num(t):
    t = t.strip()
    return int(t, 16) if t.lower().startswith(("0x", "-0x")) else int(t)


class Ins:
    __slots__ = ("i", "addr", "mn", "ops", "lname", "src", "rep")

    def __init__(self, i, addr, mn, ops, lname=None, src=None):
        self.i, self.addr, self.mn, self.ops, self.lname, self.src = i, addr, mn.lower(), ops.lower().strip(), lname, src

    def __repr__(self):
        return "%x %s %s" % (self.addr, self.mn, self.ops)


# ---- instruction sources ---------------------------------------------------------------------------------------

def source_lines(asm, func):
    """{listing offset: (source line number, text)} from the `; N    : text` comments of the function's /FAsc listing."""
    text = open(asm, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)
    out, cur = {}, None
    for ln in text[i:j].splitlines():
        m = re.match(r"; (\d+)\s+: (.*)$", ln)
        if m:
            cur = (int(m.group(1)), m.group(2).strip())
            continue
        m = re.match(r"\s+([0-9a-f]{5})\t", ln)
        if m and cur:
            out[int(m.group(1), 16)] = cur
    return out


def listing_as_orig(asm, func):
    """Our own listing as 'the original': capstone text, no names, no source lines (the tests' original)."""
    return [Ins(k, x.addr, x.mn, x.ops) for k, x in enumerate(our_instrs(asm, func))]


def our_instrs(asm, func):
    lst = tsweep.listing_instrs(asm, func)
    cs = tsweep.cs_text(lst)
    sl = source_lines(asm, func)
    addrs = lst.addrs or []
    if len(addrs) != len(cs):
        sys.exit("x87round: the listing of %s has no code bytes for every instruction" % func)
    out = []
    for k, ((mn, ops), a) in enumerate(zip(cs, addrs)):
        out.append(Ins(k, a, mn, ops, lname=lst[k][1], src=sl.get(a)))
    return out


def orig_ins(path=None, image=None, va=None):
    import c2slots
    rows = c2slots.read_orig(path, image, va)
    rows = sorted(set(rows)) if image else rows
    return [Ins(k, a, mn, ops) for k, (a, mn, ops) in enumerate(rows)]


# ---- the symbolic x87 stack -------------------------------------------------------------------------------------

class Node:
    """A value on the x87 stack. op: 'ld' (memory load), 'k' (constant), 'u' (unknown: before the code / a call's result),
    'cv' (integer conversion), an x87 mnemonic for an operation, 'rt' (a reload of a stored value: transparent in the
    key, `src` is the stored value)."""
    __slots__ = ("op", "args", "ins", "src", "points", "users", "loc", "event")

    def __init__(self, op, args=(), ins=None, loc=None):
        self.op, self.args, self.ins, self.loc = op, list(args), ins, loc
        self.src = None
        self.points = []         # rounding points of THIS value (store + reload, arg)
        self.users = []          # (kind, instruction) of the first consumers
        self.event = None


def key(n, depth=3):
    """Dataflow key: the expression with round trips removed; loads `m`, constants `k`, unknown `u`."""
    while n.op == "rt":
        n = n.src
    if n.op == "ld":
        return "m"
    if n.op in ("k", "u", "cv"):
        return n.op
    if depth == 0:
        return ".."
    ks = [key(a, depth - 1) for a in n.args]
    if n.op in COMM:
        ks.sort()
    return "%s(%s)" % (n.op, ",".join(ks))


class Point:
    def __init__(self, kind, store, reload=None, node=None):
        self.kind, self.store, self.reload, self.node = kind, store, reload, node
        self.depth = 0
        self.calls = []          # call instructions between the store and the reload
        self.consumer = None
        self.home = None
        self.size = 0
        self.rt = []             # the reload nodes
        self.maybe_arg = None
        self.feeds_arg = False   # its reload is stored into the argument area
        self.ret = False         # the reloaded value is what the function returns in ST(0)


class Side:
    def __init__(self, ins, name):
        self.ins, self.name = ins, name
        self.points = []
        self.events = []         # one per value that an operation made, or that a round trip touches
        self.calls = []
        self.depth_max = 0


def memop(ops):
    m = MEMRX.search(ops)
    return (SIZES[m.group(1)], m.group(2)) if m else None


def simulate(ins, name):
    """Run the code through the symbolic stack; returns a Side with .points and .events."""
    S = Side(ins, name)
    st = []                                  # x87 stack, top = last
    delta = 0                                # esp relative to its entry value (or to the last `and esp`)
    frame = 0                                # generation of the esp base (`and esp, -8`)
    rv = collections.Counter()               # register versions
    stores = {}                              # location key -> (store ins, node, size)
    since_call = []                          # stores since the last call: (point, esp key offset)
    snaps = {}                               # forward-jump target address -> (stack copy, delta)
    prev_end = False
    pushed = 0                               # bytes pushed since the last call (after the prologue)
    prologue = True

    def callee_pops(k, pending):
        """Does the call at ins[k] pop its own arguments (stdcall / thiscall)? A cdecl call is followed, possibly after
        further pushes and calls (cl delays the cleanup), by an `add esp, N` with N covering the pushes."""
        tot = pending
        for j in range(k + 1, min(len(ins), k + 40)):
            m, o = ins[j].mn, ins[j].ops
            if m == "add" and o.startswith("esp,"):
                return num(o.split(",")[1]) < tot
            if m == "push":
                tot += 4
            elif m == "sub" and o.startswith("esp,"):
                tot += num(o.split(",")[1])
            elif m in ("ret", "jmp"):
                break
        return pending > 0

    def get(i):
        while len(st) <= i:
            st.insert(0, Node("u"))
        return st[-1 - i]

    def newev(node, ix):
        node.ins = ix
        e = [node]
        node.event = e
        S.events.append(e)
        return e

    def loc_of(ops, size):
        """Key of a memory operand: ('esp', offset from the entry esp) for esp frames, register versions otherwise."""
        m = MEMRX.search(ops)
        if not m:
            return None
        a = m.group(2).replace(" ", "")
        mm = re.fullmatch(r"esp(?:([+-])(0x[0-9a-f]+|\d+))?", a)
        if mm:
            off = num(mm.group(2)) * (1 if mm.group(1) != "-" else -1) if mm.group(2) else 0
            return ("esp", frame, off + delta, size), ("esp", off)
        rs = REGRX.findall(a)
        return ("r", a, tuple((r, rv[r]) for r in rs), size), None

    def push(node, ix=None):
        st.append(node)
        S.depth_max = max(S.depth_max, len(st))

    def load(ix, ops):
        size, _ = memop(ops)
        lk = loc_of(ops, size)
        n = Node("ld", ins=ix, loc=lk[0] if lk else None)
        got = stores.get(lk[0]) if lk else None
        if got and size in (4, 8) and got[2] == size:
            # reload of a stored value: a round trip
            sins, snode, _sz, pt = got
            r = Node("rt", ins=ix)
            r.src = snode
            if pt.reload is None:
                pt.reload = ix
            if pt not in snode.points:
                snode.points.append(pt)
            pt.rt.append(r)
            r.loc = ("pt", pt)
            return r
        return n

    for k, ix in enumerate(ins):
        mn, ops = ix.mn, ix.ops
        if prologue and not (mn == "push" or (mn in ("sub", "and") and ops.startswith("esp,")) or
                             (mn == "mov" and ops.replace(" ", "") in ("ebp,esp", "esp,ebp"))):
            prologue = False
        # --- control flow bookkeeping
        if ix.addr in snaps and prev_end:
            st[:] = list(snaps[ix.addr][0])
            delta = snaps[ix.addr][1]
        prev_end = False
        # --- esp / register tracking
        if mn == "push":
            delta -= 4
            pushed += 0 if prologue else 4
        elif mn == "pop":
            delta += 4
        elif mn == "sub" and ops.startswith("esp,"):
            delta -= num(ops.split(",")[1])
            pushed += 0 if prologue else num(ops.split(",")[1])
        elif mn == "add" and ops.startswith("esp,"):
            delta += num(ops.split(",")[1])
            pushed = max(0, pushed - num(ops.split(",")[1]))
        elif mn == "and" and ops.startswith("esp,"):
            frame += 1
            delta = 0
        elif mn == "call":
            S.calls.append(ix)
            if callee_pops(k, pushed):
                delta += pushed
                pushed = 0
            for r in ("eax", "ecx", "edx"):
                rv[r] += 1
            for pt, off in since_call:
                if off is not None and 0 <= off < 64 and pt.kind == "rt" and pt.reload is None:
                    pt.maybe_arg = ix          # an argument if nothing ever reloads it (decided below)
            since_call = []
        elif mn in ("jmp", "ret"):
            pass
        if mn not in ("push", "pop", "cmp", "test", "jmp", "call", "ret", "nop", "fnstsw", "sahf", "lea") and \
                not mn.startswith("f") and not JCC.match(mn):
            d = ops.split(",")[0].strip()
            for r in REGRX.findall(d if "[" not in d else ""):
                rv[{"al": "eax", "ah": "eax", "ax": "eax", "bl": "ebx", "bh": "ebx", "bx": "ebx", "cl": "ecx", "ch": "ecx",
                    "cx": "ecx", "dl": "edx", "dh": "edx", "dx": "edx", "si": "esi", "di": "edi"}.get(r, r)] += 1
        elif mn == "lea" or mn == "pop":
            rv[ops.split(",")[0].strip()] += 1
        # --- x87
        if not mn.startswith("f") or mn in ("fnstsw", "fnstcw", "fldcw", "fwait", "fnclex", "fninit", "fnop", "ffree"):
            if JCC.match(mn):
                t = ops.strip()
                try:
                    tv = int(t, 16)
                    if tv > ix.addr:
                        snaps.setdefault(tv, (list(st), delta))
                except ValueError:
                    pass
            if mn in ("jmp", "ret"):
                if mn == "ret" and st:
                    top = st[-1]
                    if top.op == "rt":
                        top.loc[1].ret = True
                    top.users.append(("ret", ix))
                if mn == "jmp":
                    try:
                        tv = int(ops.strip(), 16)
                        if tv > ix.addr:
                            snaps.setdefault(tv, (list(st), delta))
                    except ValueError:
                        pass
                prev_end = True
                if mn == "ret":
                    st.clear()
            continue
        mem = memop(ops)
        base = mn
        if mn in ("fld", "fild", "fldz", "fld1", "fldpi", "fldl2e", "fldl2t", "fldlg2", "fldln2", "fild"):
            if mn == "fld" and mem:
                if mem[0] in (4, 8):
                    n = load(ix, ops)
                    push(n)
                else:
                    push(Node("u"))
            elif mn == "fld":
                src = get(int(FREG.match(ops).group(1)))
                push(src)
            elif mn == "fild":
                n = Node("cv", ins=ix)
                push(n)
            else:
                n = Node("k", ins=ix)
                push(n)
        elif mn in ("fst", "fstp"):
            if mem:
                size, _ = mem
                top = get(0)
                if size in (4, 8):
                    lk = loc_of(ops, size)
                    pt = Point("rt", ix, None, top)
                    pt.size = size
                    pt.depth = len(st)
                    pt.home = ix.lname if ix.lname else ops
                    S.points.append(pt)
                    stores[lk[0]] = (ix, top, size, pt)
                    since_call.append((pt, lk[1][1] if lk[1] else None))
                    top.users.append(("store", ix))
                else:
                    stores.pop(loc_of(ops, size)[0], None)
                    top.users.append(("store80", ix))
                if mn == "fstp":
                    st.pop()
            else:
                m = FREG.match(ops)
                if m:
                    i = int(m.group(1))
                    if mn == "fstp":
                        v = get(0)
                        get(i)
                        st[-1 - i] = v
                        st.pop()
                    else:
                        st[-1 - i] = get(0)
        elif mn in ("fist", "fistp", "fisttp"):
            get(0).users.append(("istore", ix))
            if mn != "fist":
                st.pop()
        elif mn == "fxch":
            i = int(FREG.match(ops).group(1)) if ops else 1
            a, b = get(0), get(i)
            st[-1], st[-1 - i] = b, a
        elif mn in ("fchs", "fabs", "fsqrt", "fsin", "fcos", "frndint", "f2xm1", "fsincos", "fptan", "fxtract", "fpatan",
                    "fyl2x", "fyl2xp1", "fprem", "fprem1", "fscale", "fldcw"):
            a = get(0)
            n = Node(mn, [a], ins=ix)
            a.users.append((mn, ix))
            newev(n, ix)
            st[-1] = n
            if mn in ("fsincos", "fptan"):
                push(Node("u"))
            if mn in ("fpatan", "fyl2x", "fyl2xp1", "fscale"):
                b = get(1)
                st.pop()
                st[-1] = n
        elif re.match(r"f(i?)(add|sub|mul|div)(r?)(p?)$", mn):
            integer, kind, r, pp = re.match(r"f(i?)(add|sub|mul|div)(r?)(p?)$", mn).groups()
            core = mn
            if mem:
                a = get(0)
                if mem[0] in (4, 8) and not integer:
                    b = load(ix, ops)
                else:
                    b = Node("cv" if integer else "u")
                l, rr = (b, a) if r else (a, b)
                n = Node("f" + kind, [l, rr], ins=ix)
                a.users.append((n.op, ix))
                b.users.append((n.op, ix))
                newev(n, ix)
                st[-1] = n
            else:
                regs = [int(x) for x in re.findall(r"st\((\d)\)", ops)]
                if len(regs) == 1 and not pp:
                    d_i, s_i = 0, regs[0]                 # D8 form: st(0) = st(0) op st(i)
                elif len(regs) == 1:
                    d_i, s_i = regs[0], 0                 # DE form: st(i) = st(i) op st(0), pop
                elif len(regs) == 2:
                    d_i, s_i = regs[0], regs[1]
                else:
                    d_i, s_i = 1, 0
                d, s = get(d_i), get(s_i)
                l, rr = (s, d) if r else (d, s)
                n = Node("f" + kind, [l, rr], ins=ix)
                d.users.append((n.op, ix))
                s.users.append((n.op, ix))
                newev(n, ix)
                st[-1 - d_i] = n
                if pp or core.endswith("p"):
                    st.pop()
        elif mn in ("fcom", "fcomp", "fcompp", "fucom", "fucomp", "fucompp", "fcomi", "fcomip", "fucomi", "fucomip",
                    "ficom", "ficomp", "ftst"):
            a = get(0)
            a.users.append(("cmp", ix))
            if mem and mem[0] in (4, 8) and not mn.startswith("fi"):
                b = load(ix, ops)
                b.users.append(("cmp", ix))
            else:
                regs = [int(x) for x in re.findall(r"st\((\d)\)", ops)]
                if regs:
                    get(regs[-1]).users.append(("cmp", ix))
            if mn.endswith("pp"):
                st.pop(); st.pop()
            elif mn.endswith("p"):
                st.pop()
        elif mn in ("fnstsw", "fnstcw", "fldcw", "fwait", "fnclex", "fninit", "fnop", "ffree"):
            pass
        else:
            pass          # unknown x87 op: ignore
    for pt in S.points:
        if pt.maybe_arg is not None and pt.reload is None:
            pt.kind = "arg"
            pt.calls = [pt.maybe_arg]
    return S


# ---- analysis ----------------------------------------------------------------------------------------------------

def consumer(pt):
    """The first thing that used the reloaded value: (description, instruction)."""
    for r in pt.rt:
        if r.users:
            kind, ix = r.users[0]
            return kind, ix
    return None, None


def finish(S):
    """Fill point.consumer, calls between, and mark the nodes."""
    ins = S.ins
    argst = {p.store.i for p in S.points if p.kind == "arg"}
    calls = [c.i for c in S.calls]
    for pt in S.points:
        if pt.kind == "rt" and pt.reload is not None:
            pt.calls = [ins[c] for c in calls if pt.store.i < c < pt.reload.i]
        pt.consumer = consumer(pt)
        pt.feeds_arg = bool(pt.consumer[1] is not None and pt.consumer[0] == "store" and pt.consumer[1].i in argst)
    return S


def rounded_set(S):
    """[(node, [points])] of the events (value-making operations) plus rounded loads, in program order, with the points."""
    out = []
    seen = set()
    for pt in S.points:
        if pt.kind == "rt" and pt.reload is None:
            continue
    for e in S.events:
        n = e[0]
        pts = [p for p in n.points if p.kind in ("rt", "arg") and (p.reload is not None or p.kind == "arg")]
        out.append((n, pts))
        seen.add(id(n))
    for pt in S.points:
        n = pt.node
        if id(n) not in seen and (pt.reload is not None or pt.kind == "arg"):
            seen.add(id(n))
            out.append((n, [p for p in n.points if p.kind in ("rt", "arg") and (p.reload is not None or p.kind == "arg")]))
    out.sort(key=lambda t: (t[0].ins.i if t[0].ins else 0))
    return out


def pair(sa, sb):
    """Align the two sides' values by dataflow key. -> list of (a or None, b or None) over (node, points) items."""
    A, B = rounded_set(sa), rounded_set(sb)
    # keep: every operation value, and any value that is rounded
    ka = [key(n) for n, _ in A]
    kb = [key(n) for n, _ in B]
    sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            out += [(A[i], B[j]) for i, j in zip(range(i1, i2), range(j1, j2))]
        elif op == "replace" and i2 - i1 == j2 - j1:
            # same length: pair the ones that agree in leading operator
            for i, j in zip(range(i1, i2), range(j1, j2)):
                out.append((A[i], B[j]) if ka[i].split("(")[0] == kb[j].split("(")[0] else (A[i], None))
                if ka[i].split("(")[0] != kb[j].split("(")[0]:
                    out.append((None, B[j]))
        else:
            out += [(A[i], None) for i in range(i1, i2)] + [(None, B[j]) for j in range(j1, j2)]
    return out


def where(ix):
    if ix is None:
        return "?"
    if ix.src:
        return "line %d `%s`" % ix.src
    return "%x" % ix.addr


def home_kind(home):
    """('user', name) for `_r$[esp-4]`, ('temp', tv123) for compiler temporaries, ('other', text)."""
    if not home:
        return ("other", "")
    m = re.search(r"(\$T\d+|\btv\d+|\bt\d+)\b", home)
    if m:
        return ("temp", m.group(1))
    m = re.search(r"\b_?(\w+?)\$", home)
    if m:
        return ("user", m.group(1))
    return ("other", home)


# ---- source facts ----------------------------------------------------------------------------------------------------

class Source:
    def __init__(self, path, func):
        self.lines = open(path, errors="replace").read().split("\n") if path and os.path.isfile(path) else []
        self.span = self._span(func)

    def _span(self, func):
        """(first, last) 1-based line of the function's body, by its undecorated name and braces; None when unknown."""
        if not self.lines:
            return None
        m = re.match(r"\??(?:\?\?\w)?([^@?]+)@", func or "") or re.match(r"_?([A-Za-z_]\w*)", func or "")
        nm = m.group(1) if m else func
        rx = re.compile(r"(?<![\w.])(?:\w+::)?%s\s*\(" % re.escape(nm))
        for i, ln in enumerate(self.lines):
            if rx.search(ln) and not ln.rstrip().endswith(";"):
                depth, seen = 0, False
                for j in range(i, len(self.lines)):
                    for ch in self.lines[j]:
                        if ch == "{":
                            depth += 1
                            seen = True
                        elif ch == "}":
                            depth -= 1
                    if seen and depth <= 0:
                        return (i + 1, j + 1)
        return None

    def inside(self, ln):
        return self.span is not None and self.span[0] <= ln <= self.span[1]

    def decl(self, name):
        """Where `name` is declared: {'in'|'out': (line, text)} (inside the function's span or elsewhere in the file)."""
        n = re.escape(name)
        loc = re.compile(r"^\s*(?:(?:static|const|volatile|register|inline)\s+)*[A-Za-z_][\w:<>]*\s*[&*]*\s*(?:[\w]+\s*(?:=[^,;]*)?,\s*)*%s\b\s*(?:=|;|,|\[|$)" % n)
        par = re.compile(r"[(,]\s*(?:(?:const|volatile)\s+)*[A-Za-z_][\w:<>]*\s*[&*]*\s*%s\b\s*[,)]" % n)
        res = {}
        for i, ln in enumerate(self.lines):
            if re.match(r"\s*(return|else|case|goto|delete|throw)\b", ln):
                continue
            if loc.search(ln) or par.search(ln):
                res.setdefault("in" if self.inside(i + 1) else "out", []).append((i + 1, ln.strip()))
        return res

    def addr_taken(self, name):
        rx = re.compile(r"(?<![&\w])&\s*%s\b" % re.escape(name))
        for i, ln in enumerate(self.lines):
            if self.inside(i + 1) and rx.search(ln) and not re.search(r"&\s*%s\s*[,)]?\s*&&" % name, ln):
                return (i + 1, ln.strip())
        return None


def reason(pt, S, srcinfo, kills):
    """(class, evidence) for one of our points."""
    ev = []
    hk, hname = home_kind(pt.home)
    store_src = pt.store.src
    line_txt = store_src[1] if store_src else ""
    sline = store_src[0] if store_src else None
    if pt.kind == "arg":
        c = pt.calls[0] if pt.calls else None
        return "argument", "stored into the argument area at %x for the call at %x (%s)" % (pt.store.addr, c.addr if c else 0, c.ops if c else "")
    if pt.ret and hk != "user" and not pt.calls:
        return "return", "the value returned in ST(0) is reloaded from %s (x87.md 12.2 rule 7: a float return of a computed value is rounded through memory)" % pt.home
    if pt.calls:
        hit = []
        for c in pt.calls:
            kk = kills.get(c.addr) if kills else None
            hit.append("%s%s" % ((c.lname or c.ops)[:36], " (kill set not traced on this build: a call kills the x87 stack unless its callee is x87-free)" if kk is None else " (kills all of GF0..GF7)" if kk else " (kills nothing: x87-free callee)"))
        allfree = kills and all(kills.get(c.addr) is False for c in pt.calls)
        if allfree:
            ev.append("call(s) %s do not kill the stack, so the call is not the cause" % "; ".join(hit))
        else:
            tail = ""
            if hk == "user":
                tail = "; `%s` is also a named variable" % hname
            return "call between def and use", "x87 registers are caller-saved: %s between the store at line %s and the reload%s" % (
                "; ".join(hit), sline, tail)
    if hk == "temp" and hname.startswith("$T"):
        return "float parameter of an inlined helper", "%s is the argument temporary of an inlined call (line `%s`): the parameter's float home" % (
            hname, line_txt[:70])
    if pt.depth >= 7 and not pt.feeds_arg:
        return "spill under x87 pressure", "%s stored with the x87 stack %d deep of 8 (live values + temporaries over the limit: InsertSplitPoints / RegSpill, x87.md 5, 12.1)" % (
            pt.home, pt.depth)
    if pt.feeds_arg:
        return "argument", "%s rounds the value to the float parameter before the argument is stored (line `%s`)" % (hname, line_txt[:60])
    if hk == "temp":
        if re.search(r"\(\s*(float|double)\s*\)|static_cast\s*<\s*(float|double)\s*>|\b(float|double)\s*\(", line_txt):
            return "cast to float", "compiler temporary %s on the line `%s` which converts explicitly" % (hname, line_txt[:70])
        if srcinfo and sline is not None and srcinfo.span and not srcinfo.inside(sline):
            return "inlined helper (float return/result)", "temporary %s made at line %d, which is outside %s (x87.md 12.2 rule 7)" % (hname, sline, "the function")
        return "float temporary", "temporary %s, no call between, depth %d: a float-typed expression (`%s`) rounded through its temp, or a return/argument of an inlined helper" % (
            hname, pt.depth, line_txt[:60])
    if hk == "user" and srcinfo and srcinfo.lines:
        at = srcinfo.addr_taken(hname)
        d = srcinfo.decl(hname)
        text = " ".join(x[1] for v in d.values() for x in v)
        if re.search(r"\bvolatile\b[^;=]*\b%s\b" % re.escape(hname), text):
            return "volatile / address-taken", "`%s` is declared volatile: %s" % (hname, text[:70])
        if at:
            return "volatile / address-taken", "`%s` has its address taken at line %d: `%s`" % (hname, at[0], at[1][:60])
        if "in" in d:
            dl, dt = d["in"][0]
            what = "parameter" if re.search(r"[(,]\s*(?:const\s+)?\w+\s*[&*]?\s*%s\b" % re.escape(hname), dt) else "local"
            ty = re.search(r"([A-Za-z_][\w:<>]*)\s*[&*]*\s*(?:\w+\s*(?:=[^,;]*)?,\s*)*%s\b" % re.escape(hname),
                           re.sub(r"\b(const|volatile|static|register)\b", "", dt))
            ty = ty.group(1) if ty else "?"
            if ty not in ("float", "double", "FL", "real", "long"):
                return "float member of an aggregate " + what, "`%s` (type %s, line %d: `%s`) is a struct / class %s; one of its float fields is " \
                    "stored to its home and reloaded: Lower's per-field store of a float assigned to a variable that survives to Globregs " \
                    "(x87.md 12.2 rule 7)" % (hname, ty, dl, dt[:60], what)
            return "float " + what, "`%s` is a %d-byte %s (declared line %d: `%s`); Lower stores an assigned float to its home (x87.md 12.2 rule 7)" % (
                hname, pt.size, what, dl, dt[:60])
        if "out" in d:
            return "float parameter of an inlined helper", "`%s` is declared outside %s (line %d: `%s`): an inlined callee's parameter or local" % (
                hname, "the function", d["out"][0][0], d["out"][0][1][:60])
        return "float local (unlocated)", "home `%s`; no declaration of `%s` found in the source" % (pt.home, hname)
    if hk == "user":
        return "float local", "home `%s` (no source given: name only)" % pt.home
    return "unclassified", "home %s" % (pt.home or "?")


# ---- trace ------------------------------------------------------------------------------------------------------------

def bind_kills(kills, ins):
    """{listing offset of a call: kills?} from the per-target lists (matched by the callee's name in the listing's call
    operand, in order); a call the trace did not name stays unknown."""
    out, used = {}, collections.Counter()
    for ix in ins:
        if ix.mn != "call" or not ix.lname:
            continue
        for tgt, lst in kills.items():
            if tgt and re.search(r"(?<![\w@])%s(?![\w])" % re.escape(tgt.lstrip("_?").split("@")[0]), ix.lname):
                if used[tgt] < len(lst):
                    out[ix.addr] = lst[used[tgt]]
                    used[tgt] += 1
                break
    return out


def run_plain(a, cl):
    """Untraced compile (the build's own cl, msvc.py): the listing only."""
    import subprocess, tempfile
    keep = a.keep or tempfile.mkdtemp(prefix="x87round_")
    os.makedirs(keep, exist_ok=True)
    stem = os.path.splitext(os.path.basename(a.src))[0]
    asm = os.path.join(keep, stem + ".asm")
    r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "msvc.py"), "cl", "/nologo",
                        "/c", "/O2"] + cl + ["/FAsc", "/Fa" + asm, "/Fo" + os.path.join(keep, stem + ".obj"),
                                             os.path.abspath(a.src)], capture_output=True, text=True)
    if not os.path.exists(asm):
        sys.exit("x87round: no listing (compile error?)\n%s" % (r.stdout + r.stderr)[-500:])
    return asm, [], keep


def x87_hooks():
    """c2x87's hooks (the call kill sets: LoAddCallScratchRegs) on 14.00 / 15.00; [] elsewhere (13.10 has no X87MapGFxToSTx)."""
    import c2x87
    b = c2trace.c2build.name()
    if c2trace.c2build.get("arch") == "x64" or not (b.startswith("15.00") or b == c2trace.c2build.DEFAULT):
        return []
    return c2x87.hooks()


def run_ours(a, cl):
    """-> (listing path, final IL lines, keep dir, kills): kills = {target name: [bool per call in IL order]} (True: the
    call kills GF0..GF7), or None when the build has no x87 mapper hooks."""
    import tempfile
    if a.no_trace:
        asm, final, keep = run_plain(a, cl)
        return asm, final, keep, None
    keep = a.keep or tempfile.mkdtemp(prefix="x87round_")
    xh = x87_hooks()
    hooks = xh + [c2scratch.HOOKS[3]]          # c2x87.parse reads hooks 0..3 by index: ours goes last
    enc = len(xh)
    _, _, rc, out, log = c2trace.run_trace(a.src, hooks, cl, keep=keep, quiet=True)
    asm = os.path.join(keep, os.path.splitext(os.path.basename(a.src))[0] + ".asm")
    if not os.path.exists(asm):
        sys.exit("x87round: no listing (compile error?)\n%s" % out[-500:])
    final = []
    on = False
    for ln in log.splitlines():
        if ln.startswith("E ") and "|" in ln:
            on = ln.split()[3] == str(enc) and '"' in ln and c2trace.func_match(a.func, ln.split('"')[1])
            if on:
                final = []
        elif ln.startswith("T ") and on:
            final.append(ln[2:])
        elif ln[:2] in ("X ", "E "):
            on = False
    kills = None
    if xh:
        import c2x87
        funcs, _callees = c2x87.parse(log, a.func)
        kills = collections.defaultdict(list)
        for fn in funcs:
            for c in fn["calls"]:
                k = c.get("kill")
                kills[c["target"] or ""].append(None if k is None else bool(k["gf"]))
    return asm, final, keep, kills


def il_by_offset(final):
    out, off = {}, 0
    for l in final:
        if l.startswith("I "):
            f = l.split()
            out[off] = l.split("|", 1)[1].strip() if "|" in l else l
            off += int(f[4])
    return out


# ---- report ---------------------------------------------------------------------------------------------------------------

def fmt_pt(pt, side, il=None):
    cons = ""
    if pt.consumer and pt.consumer[0]:
        k, ix = pt.consumer
        cons = "  consumed by %s at %s" % (k, where(ix))
    return "%s %s %d-byte %s at %s%s%s" % (pt.kind, side, pt.size, pt.home or "", where(pt.store), cons,
                                          "  (the returned value)" if pt.ret else "")


def compare(sa, sb):
    """-> (rows, counts): rows = [(kind, a, b)] with kind both / ours / orig / unpaired-ours / unpaired-orig, a and b the
    (node, [points]) items of the two sides."""
    rows = []
    cnt = collections.Counter()
    for a, b in pair(sa, sb):
        pa = a[1] if a else []
        pb = b[1] if b else []
        if a and b:
            if pa and pb:
                rows.append(("both", a, b))
            elif pa:
                rows.append(("ours", a, b))
            elif pb:
                rows.append(("orig", a, b))
        elif a and pa:
            rows.append(("unpaired-ours", a, None))
        elif b and pb:
            rows.append(("unpaired-orig", None, b))
    for r in rows:
        cnt[r[0]] += 1
    return rows, cnt


def report(sa, sb, srcinfo, ils, kills, show_all):
    rows, cnt = compare(sa, sb)
    nboth, nours, norig = cnt["both"], cnt["ours"], cnt["orig"]
    nun = cnt["unpaired-ours"] + cnt["unpaired-orig"]
    if not show_all:
        rows = [r for r in rows if r[0] != "both"]
    pts = lambda S: [p for p in S.points if p.reload is not None or p.kind == "arg"]
    print("# x87 rounding points: ours %d (rt %d, arg %d, of which returned %d), original %d (rt %d, arg %d, returned %d)" % (
        len(pts(sa)), sum(p.kind == "rt" for p in pts(sa)), sum(p.kind == "arg" for p in pts(sa)), sum(p.ret for p in pts(sa)),
        len(pts(sb)), sum(p.kind == "rt" for p in pts(sb)), sum(p.kind == "arg" for p in pts(sb)), sum(p.ret for p in pts(sb))))
    print("# paired by dataflow position: %d rounded on both sides, %d ours only, %d original only, %d unpaired" % (
        nboth, nours, norig, nun))
    for kind, a, b in rows:
        n = a[0] if a else b[0]
        print()
        label = {"both": "BOTH", "ours": "OURS ONLY", "orig": "ORIGINAL ONLY", "unpaired-ours": "UNPAIRED (ours)",
                 "unpaired-orig": "UNPAIRED (original)"}[kind]
        print("%s  value %s" % (label, key(n, 3)))
        if a:
            ix = a[0].ins
            print("  ours     produced at %s: %s %s" % (where(ix), ix.mn, ix.lname or ix.ops) if ix else "  ours")
            for pt in a[1]:
                rc, ev = reason(pt, sa, srcinfo, kills)
                print("    %s" % fmt_pt(pt, "ours", ils))
                if ils and pt.store.addr in ils:
                    print("      IL: %s" % ils[pt.store.addr][:110])
                print("      why: %s -- %s" % (rc, ev))
            if not a[1]:
                print("    kept on the x87 stack")
        if b:
            ix = b[0].ins
            print("  original produced at %x: %s %s" % (ix.addr, ix.mn, ix.ops) if ix else "  original")
            for pt in b[1]:
                print("    %s" % fmt_pt(pt, "orig"))
            if not b[1]:
                print("    kept on the x87 stack")


def summarize(ours_asm, ours_func, orig_asm, orig_func, src_path=None):
    """The tests' view: [(pair kind, [reason class of each of our points])] over the rows that are not `both`, plus both's
    count. Both sides come from /FAsc listings (the original's without names)."""
    sa = finish(simulate(our_instrs(ours_asm, ours_func), "ours"))
    sb = finish(simulate(listing_as_orig(orig_asm, orig_func), "original"))
    srcinfo = Source(src_path, ours_func)
    rows, cnt = compare(sa, sb)
    out = []
    for kind, a, b in rows:
        if kind != "both":
            out.append((kind, [reason(p, sa, srcinfo, None)[0] for p in (a[1] if a else [])]))
    return out, cnt["both"]


def footer(sa, sb, srcinfo, kills, cl):
    """One-line flag hints under the report."""
    rows, _ = compare(sa, sb)
    ours = [p for kind, a, b in rows if kind in ("ours", "unpaired-ours") and a for p in a[1]]
    cls = collections.Counter(reason(p, sa, srcinfo, kills)[0] for p in ours)
    if cls and not c2trace.c2build.name().startswith("13.10") and \
            not any(c.lower().startswith(("/fp:fast", "-fp:fast")) for c in cl) and \
            any(k.startswith(("float local", "float parameter", "float member", "float temporary")) for k in cls):
        print("\n# note: %d of our points are float-typed homes. /fp:precise (the default on %s) rounds every float assignment "
              "through its home; if the original keeps such values on the x87 stack, try /fp:fast (icount.py's hint)" % (
                  sum(v for k, v in cls.items() if k.startswith("float")), c2trace.c2build.name()))


def main(argv):
    ap = argparse.ArgumentParser(prog="x87round.py", usage=__doc__.replace("%", "%%"))
    ap.add_argument("src", nargs="?")
    ap.add_argument("--func")
    ap.add_argument("--asm-vs")
    ap.add_argument("--asm-func")
    ap.add_argument("--image")
    ap.add_argument("--va")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show-il", action="store_true")
    ap.add_argument("--keep")
    ap.add_argument("--orig-only", action="store_true")
    ap.add_argument("--no-trace", action="store_true", help="plain compile (listing only): no IL, no kill sets; fast")
    ap.add_argument("--source", help="the source file the reasons read declarations from (default: SRC)")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    if not a.asm_vs and not (a.image and a.va):
        sys.exit("x87round: give the original: --asm-vs ORIG.s or --image EXE --va VA")
    oi = orig_ins(a.asm_vs, a.image, int(a.va, 16) if a.va else None)
    sb = finish(simulate(oi, "original"))
    if a.orig_only:
        for pt in sb.points:
            if pt.reload is not None or pt.kind == "arg":
                print(fmt_pt(pt, "orig"))
        return 0
    if not a.src or not a.func:
        sys.exit("x87round: SRC and --func are needed (or --orig-only)")
    asm, final, keep, kills = run_ours(a, cl)
    oi_ours = our_instrs(asm, a.asm_func or a.func)
    if kills is not None:
        kills = bind_kills(kills, oi_ours)
    sa = finish(simulate(oi_ours, "ours"))
    ils = il_by_offset(final) if a.show_il else None
    srcinfo = Source(a.source or a.src, a.func)
    report(sa, sb, srcinfo, ils, kills, a.all)
    footer(sa, sb, srcinfo, kills, cl)
    return 0


if __name__ == "__main__":
    sys.exit(c2trace.pipe_safe(main, sys.argv[1:]))
