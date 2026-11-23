#!/usr/bin/env python3
"""Static equivalence judge: our compile of a function against the original's code, by value graphs.

usage: judge.py SRC --func DECORATED --asm-vs ORIG.s [--show] -- <cl args>      compile ours, judge
       judge.py --listing OURS.asm --func DECORATED --asm-vs ORIG.s [--show]    judge an existing listing (/FAsc)
       judge.py export ROWS.jsonl -o JOBS.jsonl     rows {id, func, orig, asm}: decode both sides (capstone, here)
       judge.py mutants JOBS.jsonl [--ids FILE] -o MUTANTS.jsonl   single-instruction mutants of the originals
       judge.py hints OUT.jsonl --out DIR           the pairings of a run, one <id>.json each, for another prover
       judge.py run JOBS.jsonl -o OUT.jsonl [--jobs N] [--timeout S] [--budget STEPS]
                                                    judge exported jobs, one subprocess each (standard library only:
                                                    runs on a machine without the project's environment)

Both functions are executed symbolically, one basic block at a time: every register, local stack slot and x87
stack entry holds an expression over the block's entry values instead of a name. What instruction selection,
register allocation and scheduling change does not survive that: `lea` against `add`, `inc` against `add 1`,
`test r, r` against `cmp r, 0`, a folded load against a separate one, a split read-modify-write against one
instruction, another register or frame slot for a value, the order of independent instructions. What is compared,
block by block, is what the code does: stores to memory, calls (target, stack arguments, ecx/edx where the
convention passes them), the branch condition (either sense: block layout inverts it) and the returned value.
The two sides' names meet through one consistent pairing built while comparing: a block's entry value in our code
with the one in the original's (then checked on every incoming edge: induction over the paths, so loops are
covered), our global symbol with the original's address, our frame slot whose address escapes with the original's.

Verdicts:
  EQUIVALENT  every paired block does the same, every entry pairing holds on every paired edge. Sound up to the
              instruction model below (and the pairing of globals and callees it assumes, printed with --show).
  MISMATCH    the first effect, condition or edge value that differs (with both expressions): very likely a real
              difference; the model does not prove it (another pairing or a code motion across blocks could hide
              an equivalence), so confirm with an emulator (M&B / Spore tools/difftest/equiv.py) before acting.
  UNKNOWN     an instruction or structure the model does not cover (the reason is printed): a jump table, a block
              structure that does not pair, a construct outside the instruction subset.
docs/c2/judge.md has the model and the measurements.
"""
import argparse, collections, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

M32 = 0xffffffff
FAM = {}
for _f, _names in dict(eax="eax ax al ah", ecx="ecx cx cl ch", edx="edx dx dl dh", ebx="ebx bx bl bh",
                       esi="esi si", edi="edi di", ebp="ebp bp", esp="esp sp").items():
    for _n in _names.split():
        FAM[_n] = _f
CALLEE_SAVED = ("ebx", "esi", "edi", "ebp")
CC_NEG = {"e": "ne", "ne": "e", "l": "ge", "ge": "l", "le": "g", "g": "le", "b": "ae", "ae": "b", "be": "a",
          "a": "be", "s": "ns", "ns": "s", "p": "np", "np": "p", "o": "no", "no": "o"}
CC_SWAP = {"e": "e", "ne": "ne", "l": "g", "g": "l", "le": "ge", "ge": "le", "b": "a", "a": "b", "be": "ae",
           "ae": "be"}
CC_ALIAS = {"z": "e", "nz": "ne", "nge": "l", "nl": "ge", "ng": "le", "nle": "g", "c": "b", "nae": "b", "nb": "ae",
            "nc": "ae", "na": "be", "nbe": "a", "pe": "p", "po": "np"}


class Unknown(Exception):
    """A construct the model does not cover: the verdict is UNKNOWN."""


# ---------------------------------------------------------------------------------------------------- expressions
# Expressions are tuples. ("c", k) a constant; ("a", kind, key) an atom local to one side (a block's entry value,
# an initial register, a call's result, a symbol, an escaped frame slot); ("sum", const, ((coef, term), ...)) a
# linear form (add, sub, lea, shl by a constant, neg, inc, dec all end here); other nodes ("op", name, args...).

def C(k):
    return ("c", k & M32)


def is_c(e):
    return e[0] == "c"


def lin(e):
    """(const, {term: coef}) of an expression."""
    if e[0] == "c":
        return e[1], {}
    if e[0] == "sum":
        return e[1], dict((t, c) for c, t in e[2])
    return 0, {e: 1}


def mk_sum(const, terms):
    terms = {t: c & M32 for t, c in terms.items() if c & M32}
    const &= M32
    if not terms:
        return ("c", const)
    if const == 0 and len(terms) == 1:
        (t, c), = terms.items()
        if c == 1:
            return t
    return ("sum", const, tuple(sorted(((c, t) for t, c in terms.items()), key=lambda x: (x[0], shape(x[1])))))


def add(*es):
    const, terms = 0, collections.Counter()
    for e in es:
        c, ts = lin(e)
        const += c
        for t, k in ts.items():
            terms[t] = (terms[t] + k) & M32
    return mk_sum(const, terms)


def scale(e, k):
    c, ts = lin(e)
    return mk_sum(c * k, {t: v * k for t, v in ts.items()})


def neg(e):
    return scale(e, -1)


COMMUTATIVE = {"and", "or", "xor", "mul", "fadd", "fmul"}


def op(name, *args):
    if name in ("and", "or", "xor", "mul"):
        flat = []
        for a in args:
            if a[0] == "op" and a[1] == name:
                flat.extend(a[2:])
            else:
                flat.append(a)
        consts = [a[1] for a in flat if is_c(a)]
        rest = [a for a in flat if not is_c(a)]
        if name == "mul" and len(rest) <= 1:
            k = 1
            for c in consts:
                k = (k * c) & M32
            return scale(rest[0], k) if rest else C(k)
        if consts:
            k = consts[0]
            for c in consts[1:]:
                k = (k & c) if name == "and" else (k | c) if name == "or" else (k ^ c) if name == "xor" else k * c
            k &= M32
            if name == "and" and k == M32 or name in ("or", "xor") and k == 0:
                pass
            elif name == "and" and k == 0:
                return C(0)
            else:
                rest.append(C(k))
        if name == "xor":                                   # x ^ x = 0
            cnt = collections.Counter(rest)
            rest = [a for a, n in cnt.items() for _ in range(n % 2)]
        if name in ("and", "or"):
            rest = list(dict.fromkeys(rest))
        if not rest:
            return C(0) if name in ("xor", "or") else C(M32)
        if len(rest) == 1:
            return rest[0]
        return ("op", name) + tuple(sorted(rest, key=shape))
    if name in COMMUTATIVE:
        return ("op", name) + tuple(sorted(args, key=shape))
    if name in ("shl",) and is_c(args[1]):
        return scale(args[0], 1 << (args[1][1] & 31))
    if name in ("shr", "sar") and is_c(args[0]) and is_c(args[1]):
        x, k = args[0][1], args[1][1] & 31
        if name == "shr":
            return C(x >> k)
        return C((x - (1 << 32) if x & 0x80000000 else x) >> k)
    if name == "trunc":                                    # ("trunc", bits, x)
        bits, x = args
        if is_c(x):
            return C(x[1] & ((1 << bits) - 1))
        if x[0] == "op" and x[1] in ("zext", "sext") and x[2] == bits:
            return x[3]
        if x[0] == "op" and x[1] == "trunc" and x[2] >= bits:
            return op("trunc", bits, x[3])
        if x[0] == "op" and x[1] == "merge" and x[2] == bits and x[3] == "lo":
            return x[5]
    if name == "merge" and args[1] == "lo" and is_c(args[2]) and args[2][1] == 0:
        return op("zext", args[0], args[3])               # xor r, r; setcc r8 is movzx
    if name in ("zext", "sext") and is_c(args[1]):
        bits, x = args[0], args[1][1]
        x &= (1 << bits) - 1
        if name == "sext" and x & (1 << (bits - 1)):
            x -= 1 << bits
        return C(x)
    return ("op", name) + tuple(args)


def shape(e):
    """A key that does not depend on atoms' identities (both sides sort operands the same way)."""
    if e[0] == "c":
        return ("c", e[1])
    if e[0] == "a":
        return ("a", e[1])
    if e[0] == "sum":
        return ("sum", e[1], tuple((c, shape(t)) for c, t in e[2]))
    return tuple(shape(x) if isinstance(x, tuple) else x for x in e)


def show(e, depth=0):
    if not isinstance(e, tuple):
        return str(e)
    if depth > 4:
        return "…"
    if e[0] == "c":
        return hex(e[1]) if e[1] > 9 else str(e[1])
    if e[0] == "chain":
        return "%d effects" % (len(e) - 1)
    if e[0] == "mem":
        return "mem@%s" % show(e[2], depth + 1)
    if e[0] == "a":
        return "%s(%s)" % (e[1], ",".join(map(str, e[2])) if isinstance(e[2], tuple) else e[2])
    if e[0] == "sum":
        parts = [("%s*" % hex(c) if c not in (1, M32) else "-" if c == M32 else "") + show(t, depth + 1)
                 for c, t in e[2]]
        if e[1]:
            parts.append(hex(e[1]) if e[1] < 0x80000000 else "-" + hex((1 << 32) - e[1]))
        return "(" + " + ".join(parts) + ")"
    if e[0] == "op":
        return "%s(%s)" % (e[1], ", ".join(show(x, depth + 1) for x in e[2:]))
    return "%s(%s)" % (e[0], ", ".join(show(x, depth + 1) for x in e[1:]))


# --------------------------------------------------------------------------------------------------- instructions

def split_ops(s):
    out, d, cur = [], 0, ""
    for ch in s:
        d += ch == "["
        d -= ch == "]"
        if ch == "," and d == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return out + [cur.strip()] if cur.strip() else out


SIZE = {"byte": 8, "word": 16, "dword": 32, "qword": 64, "tbyte": 80, "xmmword": 128}
REGBITS = {r: (8 if r[-1] in "lh" and len(r) == 2 else 16 if len(r) == 2 else 32) for r in FAM}


class Ins:
    __slots__ = ("addr", "mn", "ops", "sym", "target", "text")

    def __init__(self, addr, mn, ops, sym=None, target=None, text=""):
        self.addr, self.mn, self.ops, self.sym, self.target, self.text = addr, mn, ops, sym, target, text


def _num(t):
    t = t.strip().lower()
    neg_ = t.startswith("-")
    t = t.lstrip("+-").strip()
    v = int(t, 16) if t.startswith("0x") else int(t)
    return (-v if neg_ else v) & M32


LISTING_SYM = re.compile(r"(?:OFFSET\s+(?:FLAT:)?)?([?@$_A-Za-z][\w?@$.]*)")


def _listing_symbol(ops):
    """The first non-frame, non-register symbol in a listing operand string (the relocation's target)."""
    for o in split_ops(ops):
        o = re.sub(r"^(?:BYTE|WORD|DWORD|QWORD|TBYTE|FWORD|XMMWORD)\s+PTR\s+", "", o.strip())
        o = re.sub(r"^(?:FLAT:|[cdefgs]s:)", "", o)
        m = re.match(r"(?:OFFSET\s+(?:FLAT:)?)?([?@$_A-Za-z][\w?@$.]*)\s*(\[[^\]]*\])?", o)
        if not m:
            continue
        name = m.group(1)
        if name.lower() in FAM or re.fullmatch(r"st|xmm\d|SHORT|NEAR|\$LN\w*|\$L\w*", name, re.I):
            continue
        if m.group(2) and re.match(r"\[\s*e[sb]p\b", m.group(2)) and name.endswith("$"):
            continue                                       # a frame variable
        if name.endswith("$") and m.group(2):
            continue
        return name
    return None


FRAME_TEXT = re.compile(r"([?@$_A-Za-z][\w?@$]*\$)\[([^\]]*)\]")


def listing_frame(texts):
    """The frame variables of a listing given as its instructions' text (`_n$[esp+4]`: the operand's own offset), as
    {name: value}: `a` for each variable read as a whole. A frame map for a listing that declares none (a synthetic
    text side). The real path is frame_objects' declarations (cl.exe's `_n$ = 8`); `addr_of` adds a declaration to the
    operand's own offset (`_n$[esp]` with `_n$ = 8` is the reader's `[esp + 8]`)."""
    def off(t):
        c = 0
        for sign, x in re.findall(r"([+-]?)\s*([^+-]+)", t):
            x = x.strip()
            if x and x not in FAM:
                c += (-1 if sign == "-" else 1) * (int(x, 16) if x.lower().startswith("0x") else int(x))
        return c
    out = {}
    for t_ in texts:
        for name, t in FRAME_TEXT.findall(str(t_ or "")):
            out.setdefault(name, off(t))
    return out


def frame_objects(asm, func):
    """[(name, value, size, escaped)] of our listing's frame variables (`_x$ = -12 ; size = 12` before the PROC
    line; cl.exe's /FA prints the value alone). The value is the frame offset the declaration states (`_n$ = 8`),
    which the text's operands show as `_n$[esp + 4]` and `addr_of` turns into the reader's +8; without declarations
    the text's own offsets (listing_frame). Escaped: its address is taken (`lea` of it) in the function."""
    import c2trace
    text = open(asm, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)
    head = text[:i].splitlines()[-400:]
    decls = []
    for ln in reversed(head):
        m = re.match(r"([$\w?@]+\$\d*)\s*=\s*(-?\d+)(?:\s*;\s*size\s*=\s*(\d+))?", ln.strip())
        if m:                                              # /FAsc prints the size, cl.exe's /FA the value alone
            decls.append((m.group(1), int(m.group(2)), int(m.group(3) or 4)))
        elif re.search(r"\bSEGMENT\b|\bENDP\b", ln):
            break
    body = text[i:j]
    if decls and not any(re.search(r"\b%s\[" % re.escape(name), body) for name, v, size in decls):
        decls = []                                         # no operand shows a declared variable (a self-check's
    names = listing_frame(body.splitlines())               # synthetic text): the text's own offsets
    out = []
    for name, v, size in decls or [(n, names[n], 4) for n in names]:
        esc = bool(re.search(r"\blea\b[^\n]*\b%s\[" % re.escape(name), body))
        out.append((name, v, size, esc))
    return out


def ours_instrs(lst):
    """[Ins] of our listing (tsweep.listing_instrs: capstone text of the code bytes, relocated fields known)."""
    if lst.decoded is None:
        raise Unknown("our listing has no code bytes (/FAsc needed)")
    out = []
    main = next((k for k in range(1, len(lst.addrs)) if lst.addrs[k] < lst.addrs[k - 1]), len(lst.addrs))
    for k in range(main):                                  # EH funclets after the function are not judged
        mn, ops, imm_sym, mem_sym = lst.decoded[k][:4]
        if mn in ("npad",):
            continue
        if mn == "db":
            raise Unknown("data in our procedure (a jump table)")
        sym = _listing_symbol(lst[k][1]) if (imm_sym or mem_sym or mn == "call") else None
        if mn == "call" and sym is None and re.match(r"^\s*(?:0x)?[0-9a-f]+\s*$", ops):
            sym = "?unnamed@" + ops.strip()
        out.append(Ins(lst.addrs[k], mn, ops, sym=(sym, imm_sym, mem_sym) if sym else None,
                       text=lst[k][0] + " " + lst[k][1]))
    return out


def orig_instrs(lst):
    out = []
    for k, (mn, ops) in enumerate(lst):
        if mn == "npad":
            continue
        out.append(Ins(lst.addrs[k], mn, ops, text=mn + " " + ops))
    return out


# ------------------------------------------------------------------------------------------------------- CFG

JCC = re.compile(r"j(n?[a-z]{1,3})$")


def is_jcc(mn):
    return mn.startswith("j") and mn != "jmp" and not mn.startswith("jecxz")


def blocks_of(ins):
    """{start address: [Ins]} and the entry address. Leaders: the entry, jump targets, the instruction after a
    jump or a ret. A block that is only `jmp X` is folded away by succ()."""
    if not ins:
        raise Unknown("empty function")
    addrs = [i.addr for i in ins]
    at = {a: k for k, a in enumerate(addrs)}
    lead = {addrs[0]}
    for k, i in enumerate(ins):
        if is_jcc(i.mn) or i.mn == "jmp":
            t = target(i)
            if t is None:
                raise Unknown("indirect jump (a jump table): %s" % i.text)
            if t not in at:
                raise Unknown("jump outside the function: %s" % i.text)
            lead.add(t)
            if k + 1 < len(ins):
                lead.add(addrs[k + 1])
        elif i.mn.startswith("ret") and k + 1 < len(ins):
            lead.add(addrs[k + 1])
    out, cur = {}, None
    for i in ins:
        if i.addr in lead:
            cur = i.addr
            out[cur] = []
        out[cur].append(i)
    return out, addrs[0]


def target(i):
    m = re.fullmatch(r"\s*(0x[0-9a-f]+|\d+)\s*", i.ops)
    return _num(m.group(1)) if m else None


# --------------------------------------------------------------------------------------------- symbolic execution

class Side:
    """One function's symbolic execution: per block, the effects and the values at its end."""

    def __init__(self, name, ins, image=None, listing=False):
        self.name = name
        self.listing = listing
                             # our listing's text: symbols, a frame form with no bytes
        self.blocks, self.entry = blocks_of(ins)
        self.order = sorted(self.blocks)
        self.image = image                                 # (lo, hi) image range: constants there are addresses
        self.escaped = self._escaped(ins)
        self.regions = None                                # [(base, size)]: the frame bytes that are memory (pass 2)
        self.escapes = set()                               # frame offsets whose address became a value
        self.fsyms = {}                                    # our listing's frame variable -> its value
        self.fcands = []                                   # entry offset - value of each access: the frame base
        self.npred = collections.Counter()
        for a in self.order:
            for t in self.static_succ(a):
                self.npred[t] += 1
        self.cuts = self._loop_heads()

    def _loop_heads(self):
        """Targets of back edges (a DFS from the entry): every cycle passes one. Execution is cut there only; any
        other join is run through on each path (the two sides merge and duplicate tails differently)."""
        heads, state, stack = set(), {}, [(self.entry, iter(self.static_succ(self.entry)))]
        state[self.entry] = 1
        while stack:
            n, it = stack[-1]
            for t in it:
                if t not in self.blocks:
                    continue
                if state.get(t) == 1:
                    heads.add(t)
                elif t not in state:
                    state[t] = 1
                    stack.append((t, iter(self.static_succ(t))))
                    break
            else:
                state[n] = 2
                stack.pop()
        return heads

    def static_succ(self, a):
        last = self.blocks[a][-1]
        nxt = self.next_addr(a)
        if is_jcc(last.mn):
            return [self.succ(target(last)), self.succ(nxt)]
        if last.mn == "jmp":
            return [self.succ(target(last))]
        if last.mn.startswith("ret"):
            return []
        return [self.succ(nxt)] if nxt is not None else []

    def _escaped(self, ins):
        """Does any frame address escape (lea of an esp/ebp slot, esp copied)? Then local slots are memory."""
        for i in ins:
            if i.mn == "lea" and re.search(r"\[\s*e[sb]p\b", i.ops):
                return True
            if i.mn == "mov" and re.match(r"\s*e(?!sp|bp)\w\w\s*,\s*esp\s*$", i.ops):
                return True
        return False

    def region_of(self, off):
        """(base, size, object index) of the smallest escaped object holding frame offset off, or None (pass 2
        only). The index is our listing's: the same object on both sides."""
        best = None
        for b, n, k in self.regions or ():
            if b <= off < b + n and (best is None or (n, k) < (best[1], best[2])):
                best = (b, n, k)
        return best

    def succ(self, a):
        """The block a control transfer to address a really reaches (a block that is only `jmp X` folded)."""
        seen = set()
        while a in self.blocks and len(self.blocks[a]) == 1 and self.blocks[a][0].mn == "jmp" and a not in seen:
            seen.add(a)
            a = target(self.blocks[a][0])
        return a

    def next_addr(self, a):
        k = self.order.index(a)
        return self.order[k + 1] if k + 1 < len(self.order) else None


class State:
    def __init__(self, side, blk, depth, entry, parent=None, node=None):
        self.side, self.blk = side, blk
        self.node = node if node is not None else blk      # the product node (our block, the original's block)
        self.parent = parent                               # the single predecessor's end state (an extended block)
        self.regs = {}
        self.slots = {}                                    # entry-relative offset -> value (non-escaped frame)
        self.depth = depth                                 # bytes below the entry esp
        self.entry = entry
        self.fst = []                                      # x87 stack, top last
        self.fst_under = 0
        self.flags = None
        self.fsw = None
        self.effects = []
        self.eff_atoms = []                                # one atom per effect: loads name them, not embed them
        self.pushes = []                                   # values pushed since the last call
        self.pushed = set()                                # offsets written by push (state even when escaped)
        self.calls = []
        self.inh = []                                      # (effect, atom) of the predecessors in this extended block
        if parent is not None:
            self.fst = list(parent.fst)
            self.fst_under = parent.fst_under
            self.flags, self.fsw = parent.flags, parent.fsw
            self.pushes = list(parent.pushes)
            self.pushed = set(parent.pushed)
            self.inh = parent.inh + list(zip(parent.effects, parent.eff_atoms))
            if getattr(parent, "fst_ret", None) is not None:
                self.fst_ret = parent.fst_ret

    def phi(self, key):
        if self.parent is not None:                        # no entry values of its own: the predecessor's values
            if key == "mem":
                return self.parent.phi("mem")
            if isinstance(key, tuple) and key[0] == "s":
                return self.parent.slot(key[1])
            if isinstance(key, tuple) and key[0] == "st":
                return ("a", "phi", (self.side.name, self.node, key))
            return self.parent.get(key)
        if self.entry:
            if key in CALLEE_SAVED or key in ("eax", "ecx", "edx", "esp") or isinstance(key, tuple):
                return ("a", "init", key)
            return ("a", "init", key)
        return ("a", "phi", (self.side.name, self.node, key))

    def get(self, r):
        if r not in self.regs:
            self.regs[r] = self.phi(r)
        return self.regs[r]

    def slot(self, off):
        """The value of frame offset off, the reader's bytes: both sides read the frame alike (`addr_of` has already
        put a listing's operand on those bytes, docs/c2/judge.md section 6)."""
        key = off
        return self.slots.setdefault(key, self.phi(("s", key)))


def mem_alias(a1, s1, a2, s2):
    """May the accesses [a1, s1 bits) and [a2, s2 bits) overlap? Same linear base, disjoint ranges: no. Frame
    addresses of the original layout against heap addresses: no."""
    c1, t1 = lin(a1)
    c2, t2 = lin(a2)
    if t1 == t2:
        d = (c1 - c2) & M32
        d = d - (1 << 32) if d & 0x80000000 else d
        return -(s1 // 8) < d < s2 // 8
    f1 = any(t[0] == "a" and t[1] in ("fa", "fobj") for t in t1)
    f2 = any(t[0] == "a" and t[1] in ("fa", "fobj") for t in t2)
    s1_ = any(t[0] == "a" and t[1] == "sym" for t in t1) and len(t1) == 1
    s2_ = any(t[0] == "a" and t[1] == "sym" for t in t2) and len(t2) == 1
    if s1_ and s2_ and t1 != t2:
        return False                                       # two different globals
    if f1 and f2 and t1 != t2:
        return False                                       # two different escaped frame objects
    return True


class Exec:
    """Executes one block of one side."""

    def __init__(self, side, st, cleanups=None):
        self.side, self.st = side, st
        self.cleanups = cleanups                           # the callee stack pops of the paired block's calls

    # operands ---------------------------------------------------------------------------------------------
    def addr_of(self, o, ins):
        """Address expression of a memory operand (capstone text), or ("frame", offset) for a non-escaped local. Our
        side of a /FAsc listing is also capstone text (the code bytes), so its frame operands are the reader's bytes;
        a text listing's operand may instead name a declared variable (`name[esp + N]`), whose frame offset is N plus
        the declaration (docs/c2/judge.md section 6)."""
        extra = 0
        if self.side.listing and self.side.fsyms:          # a declared variable's operand is `name[esp + N]`
            m = FRAME_TEXT.search(o)
            if m and m.group(1) in self.side.fsyms:
                extra = self.side.fsyms[m.group(1)]         # the name's declaration value
                o = re.sub(re.escape(m.group(1)) + r"\s*(?=\[)", "", o, count=1)
        c = re.sub(r"^(\w+)\s+ptr\s+", "", o.strip(), flags=re.I)
        m = re.match(r"^(?:(\w+)\s+ptr\s+)?(?:[cdefgs]s:)?(?:[$\w?@]+\$)?\[(.*)\](?:\s*\[\s*(?:e?[a-d]x|[abcd][lh]|e?[sd]i|e?[sb]p)\s*\])?$",
                     c, re.I)
        if not m:
            raise Unknown("operand %r" % o)
        parts = []
        for sign, t in re.findall(r"([+-]?)\s*([^+-]+)", m.group(2)):
            t = t.strip()
            if not t:
                continue
            k = -1 if sign == "-" else 1
            if "*" in t:
                r, s = t.split("*")
                parts.append(scale(self.reg(r.strip()), int(s) * k))
            elif t in FAM:
                parts.append(scale(self.reg(t), k))
            else:
                parts.append(C(_num(t) * k))
        a = add(*parts)
        if extra:                                          # name[N] -> [N + the declaration]
            a = add(a, C(extra))
        if self.side.fsyms and not self.side.listing:      # measure the frame base (pass 2, capstone texts): our
            mm = re.search(r"([$\w?@]+\$\d*)\[", ins.text or "")   # access minus the variable's declared value
            off = self.frame_off(a) if mm and mm.group(1) in self.side.fsyms else None
            if off is not None:
                self.side.fcands.append(off - self.side.fsyms[mm.group(1)])
        if ins.sym and ins.sym[2]:                         # a relocated displacement: our symbol
            c, ts = lin(a)
            a = mk_sum(c, {**ts, ("a", "sym", ins.sym[0]): 1})
        elif self.side.image:
            c, ts = lin(a)
            if self.side.image[0] <= c < self.side.image[1]:
                a = mk_sum(0, {**ts, ("a", "sym", c): 1})
        return a

    def frame_off(self, a):
        """The entry-relative offset of a frame address (esp-based), or None."""
        c, ts = lin(a)
        if len(ts) == 1:
            (t, k), = ts.items()
            if t == ("a", "init", "esp") and k == 1:
                d = c - (1 << 32) if c & 0x80000000 else c
                return d
        return None

    def fa(self, off):
        """An escaped local's address: an atom paired with the other side's (frame layouts differ)."""
        return ("a", "fa", off)

    def frame_value(self, a):
        """Frame addresses that escape as values become fa atoms (pass 1: one per offset, incoming arguments keep
        their offset; pass 2: the escaped region's base atom plus the offset in it)."""
        off = self.frame_off(a)
        if off is None:
            return a
        if self.side.regions is not None:
            r = self.side.region_of(off)
            if r is not None:
                return add(("a", "fobj", r[2]), C(off - r[0]))
            return self.fa(off) if off <= 0 else a
        if off <= 0:
            self.side.escapes.add(off)
            return self.fa(off)
        return a

    def frame_mem(self, off):
        """The memory address of frame offset off if it is memory, else None (a variable of the state)."""
        if off in self.st.pushed:
            return None
        if self.side.regions is not None:
            r = self.side.region_of(off)
            return add(("a", "fobj", r[2]), C(off - r[0])) if r is not None else None
        if off > 0 or not self.side.escaped:
            return None
        return self.fa(off)

    def size(self, o, default=32):
        m = re.match(r"^\s*(\w+)\s+ptr\b", o)
        return SIZE.get(m.group(1), default) if m else default

    def reg(self, r):
        f = FAM[r]
        v = self.st.get(f) if f != "esp" else add(("a", "init", "esp"), C(-self.st.depth))
        b = REGBITS[r]
        if b == 32:
            return v
        if r[-1] == "h" and len(r) == 2:
            return op("trunc", 8, op("shr", v, C(8)))
        return op("trunc", b, v)

    def set_reg(self, r, v):
        f = FAM[r]
        if f == "esp":
            off = self.frame_off(v)
            if off is None:
                raise Unknown("esp set to %s" % show(v))
            self.st.depth = -off
            return
        b = REGBITS[r]
        if b == 32:
            self.st.regs[f] = v
        elif r[-1] == "h" and len(r) == 2:
            self.st.regs[f] = op("merge", 8, "hi", self.st.get(f), v)
        else:
            old = self.st.get(f)
            self.st.regs[f] = op("merge", b, "lo", old, v)

    def load(self, a, bits):
        off = self.frame_off(a)
        fm = self.frame_mem(off) if off is not None else None
        if off is not None and fm is None:
            if bits != 32:
                v = self.st.slot(off & ~3) if off % 4 == 0 else None
                if v is None:
                    raise Unknown("unaligned frame access")
                return op("trunc", bits, v)
            return self.st.slot(off)
        if fm is not None:
            a = fm
        chain = ("chain",) + tuple(t for e, t in self.st.inh + list(zip(self.st.effects, self.st.eff_atoms))
                                   if self._may_alias(e, a, bits))
        return ("ld", bits, a, ("mem", self.st.phi("mem"), chain))

    def effect(self, e):
        st = self.st
        st.effects.append(e)
        st.eff_atoms.append(("a", "eff", (self.side.name, st.blk, len(st.effects))))

    def _may_alias(self, e, a, bits):
        if e[0] == "st":
            return mem_alias(e[1], e[3], a, bits)
        return True                                        # a call, a rep string operation

    def store(self, a, v, bits):
        off = self.frame_off(a)
        fm = self.frame_mem(off) if off is not None else None
        if off is not None and fm is None:
            if bits != 32:
                if off % 4:
                    raise Unknown("unaligned frame store")
                self.st.slots[off] = op("merge", bits, "lo", self.st.slot(off), v)
            else:
                self.st.slots[off] = v
            return
        if fm is not None:
            a = fm
        self.effect(("st", a, v, bits))

    def val(self, o, ins, bits=None):
        """The value of a source operand."""
        o = o.strip()
        if o in FAM:
            return self.reg(o)
        if "[" in o:
            return self.load(self.addr_of(o, ins), self.size(o, bits or 32))
        if re.fullmatch(r"-?(0x[0-9a-f]+|\d+)", o):
            v = _num(o)
            if ins.sym and ins.sym[1]:
                return mk_sum(v, {("a", "sym", ins.sym[0]): 1})
            if self.side.image and self.side.image[0] <= v < self.side.image[1]:
                return ("a", "sym", v)
            return C(v)
        raise Unknown("operand %r in %s" % (o, ins.text))

    def write(self, o, v, ins):
        o = o.strip()
        if o in FAM:
            self.set_reg(o, v)
        elif "[" in o:
            self.store(self.addr_of(o, ins), v, self.size(o))
        else:
            raise Unknown("destination %r" % o)

    def bits(self, o):
        o = o.strip()
        return REGBITS[o] if o in FAM else self.size(o)

    # flags ------------------------------------------------------------------------------------------------
    def cond(self, cc):
        cc = CC_ALIAS.get(cc, cc)
        f = self.st.flags
        if f is None:
            raise Unknown("a branch on flags set before the block")
        kind = f[0]
        if kind == "cmp":
            a, b = f[1], f[2]
            return canon_cond(cc, a, b)
        if kind == "test":
            a, b = f[1], f[2]
            if a == b and cc in ("e", "ne", "s", "ns", "le", "g", "l", "ge"):
                return canon_cond(cc, a, C(0))             # test r, r is cmp r, 0 for these
            return ("cond", cc, "test", a, b)
        if kind == "res":
            r = f[1]
            if cc in ("e", "ne", "s", "ns"):
                return canon_cond(cc, r, C(0))
            return ("cond", cc, "res", f[2], r)
        if kind == "fcmp":
            return ("cond", cc, "fcmp", f[1], f[2])
        raise Unknown("flags %s" % kind)

    # one instruction --------------------------------------------------------------------------------------
    def step(self, i):
        st, mn = self.st, i.mn
        if mn in ("rep", "repe", "repne", "repz", "repnz") and i.ops.strip():
            w = i.ops.split()[0]                           # capstone: `rep` + `movsd dword ptr es:[edi], ...`
            mn, i = mn + " " + w, Ins(i.addr, mn + " " + w, "", i.sym, text=i.text)
        ops = split_ops(i.ops)
        mn = mn.replace("lock ", "")
        if self.side.listing and mn in ("offset", "fword", "tbyte", "xmmword") and len(ops) == 2:
            i = Ins(i.addr, mn, ops[1], i.sym, i.target, i.text)   # a listing's operand prefix: a type, not an
            ops = [ops[1]]                                         # instruction
        if mn in ("nop", "npad", "int3", "fwait", "wait") or (mn == "mov" and len(ops) == 2 and ops[0] == ops[1]
                                                               and ops[0] in FAM):
            return
        if mn == "lea" and len(ops) == 2 and "[" in ops[1] and ops[0] in FAM:
            a = self.addr_of(ops[1], i)
            if ops[0] == "esp":
                self.set_reg("esp", a)
            else:
                self.set_reg(ops[0], self.frame_value(a) if FAM[ops[0]] != "ebp" else a)
            return
        if mn == "mov":
            b = self.bits(ops[0])
            v = self.val(ops[1], i, b)
            if ops[0] == "esp" or ops[0] == "ebp" and ops[1] == "esp":
                self.set_reg(ops[0], v)
                return
            self.write(ops[0], self.frame_value(v) if ops[0] not in ("ebp",) else v, i)
            return
        if mn in ("movzx", "movsx"):
            b = self.bits(ops[1])
            v = self.val(ops[1], i, b)
            self.write(ops[0], op("zext" if mn == "movzx" else "sext", b, v), i)
            return
        if mn == "push":                                   # below every local: state, not memory, even when a
            if self.side.listing and ops[0] in FAM:
                v = self.reg(ops[0])                       # a listing without bytes: a saved register is a save
            else:                                          # (the capstone flag `PTR reg` is the other side's)
                v = self.frame_value(self.val(ops[0], i))  # frame address escapes (call arguments, saves)
            st.depth += 4
            st.slots[-st.depth] = v
            st.pushed.add(-st.depth)
            if not (v[0] == "a" and any(v[1] == k for k in ("init", "phi", "clob")) and
                    (v[2] in CALLEE_SAVED or (isinstance(v[2], tuple) and v[2][0] == "reg" and v[2][1] in CALLEE_SAVED))):
                st.pushes.append(v)                        # a save of a callee-saved register is no argument
            return
        if mn == "pop":
            v = st.slot(-st.depth)
            st.depth -= 4
            self.write(ops[0], v, i)
            return
        if mn == "xchg":
            a, b = self.val(ops[0], i), self.val(ops[1], i)
            self.write(ops[0], b, i)
            self.write(ops[1], a, i)
            return
        if mn in ("add", "sub", "adc", "sbb", "and", "or", "xor", "imul", "shl", "sal", "shr", "sar", "inc", "dec",
                  "neg", "not", "rol", "ror") and not (mn == "imul" and len(ops) == 1):
            return self.arith(mn, ops, i)
        if mn in ("cmp", "test"):
            b = self.bits(ops[0])
            a, c = self.val(ops[0], i, b), self.val(ops[1], i, b)
            st.flags = (mn, a, c)
            return
        if mn in ("cdq",):
            st.regs["edx"] = op("sar", self.reg("eax"), C(31))
            return
        if mn in ("idiv", "div", "mul") or (mn == "imul" and len(ops) == 1):
            d = self.val(ops[0], i)
            hi, lo = self.reg("edx"), self.reg("eax")
            if mn in ("idiv", "div"):
                st.regs["eax"] = op("quot", mn, hi, lo, d)
                st.regs["edx"] = op("rem", mn, hi, lo, d)
            else:
                st.regs["eax"] = op("mul", lo, d)
                st.regs["edx"] = op("mulhi", mn, lo, d)
            st.flags = None
            return
        if mn.startswith("set"):
            self.write(ops[0], op("bool", self.cond(mn[3:])), i)
            return
        if mn.startswith("cmov"):
            cnd = self.cond(mn[4:])
            self.write(ops[0], op("sel", cnd, self.val(ops[1], i), self.val(ops[0], i)), i)
            return
        if mn == "call":
            return self.call(i, ops)
        if mn.startswith("rep") or re.fullmatch(r"(movs|stos|lods|scas|cmps)[bwd]", mn.split()[-1]) and (
                not ops or "es:" in i.ops):
            return self.string(re.sub(r"\s+", " ", mn), i)
        if mn in ("cld", "std"):
            return
        if mn == "leave":
            self.set_reg("esp", self.reg("ebp"))
            v = self.load(add(("a", "init", "esp"), C(-st.depth)), 32)
            st.depth -= 4
            st.regs["ebp"] = v
            return
        if mn.startswith("f") or mn.endswith(("ss", "sd")) or mn.startswith(("cvt", "ucomis", "comis", "xorp",
                                                                              "movap", "movup", "pxor", "movd",
                                                                              "movq")):
            return self.flt(mn, ops, i)
        if mn == "sahf":
            st.flags = ("fcmp",) + tuple(st.fsw[1:]) if st.fsw else None
            if st.flags is None:
                raise Unknown("sahf without fnstsw")
            return
        raise Unknown("instruction %s" % i.text)

    def arith(self, mn, ops, i):
        st = self.st
        b = self.bits(ops[0])
        if mn in ("inc", "dec", "neg", "not"):
            x = self.val(ops[0], i, b)
            r = {"inc": lambda: add(x, C(1)), "dec": lambda: add(x, C(-1)), "neg": lambda: neg(x),
                 "not": lambda: add(neg(x), C(-1))}[mn]()
            if b != 32:
                r = op("trunc", b, r)
            self.write(ops[0], r, i)
            if mn != "not":
                st.flags = ("res", r, mn)
            return
        if mn == "imul" and len(ops) == 3:
            x, y = self.val(ops[1], i, b), self.val(ops[2], i, b)
            r = op("mul", x, y)
            self.write(ops[0], r, i)
            st.flags = None
            return
        x, y = self.val(ops[0], i, b), self.val(ops[1], i, b)
        if mn == "xor" and ops[0] == ops[1]:
            r = C(0)
        elif mn == "sub" and ops[0] == ops[1]:
            r = C(0)
        elif mn == "add":
            r = add(x, y)
        elif mn == "sub":
            r = add(x, neg(y))
        elif mn in ("and", "or", "xor"):
            r = op(mn, x, y)
        elif mn == "imul":
            r = op("mul", x, y)
        elif mn in ("shl", "sal"):
            r = op("shl", x, y)
        elif mn in ("shr", "sar", "rol", "ror"):
            r = op(mn, x, y)
        elif mn in ("adc", "sbb"):
            cf = op("bool", self.cond("b"))
            r = add(x, y, cf) if mn == "adc" else add(x, neg(y), neg(cf))
        else:
            raise Unknown(mn)
        if b != 32:
            r = op("trunc", b, r)
        self.write(ops[0], r, i)
        if mn == "sub":
            st.flags = ("cmp", x, y)                     # the flags of a - b are cmp a, b's
        elif mn in ("and",):
            st.flags = ("test", x, y) if False else ("res", r, mn)
        else:
            st.flags = ("res", r, mn)

    def call(self, i, ops):
        st = self.st
        # a direct call: `0x401000` (capstone) or `near ptr 401000` (a listing; cl prints hex without 0x)
        m = re.fullmatch(r"\s*(?:near ptr\s+)?(0x[0-9a-f]+|[0-9a-f]*\d[0-9a-f]*)\s*", i.ops, re.I)
        if m:
            name = i.sym[0] if i.sym else None
            tgt = ("a", "sym", name if name else _num(m.group(1)))
        else:                                              # through a register or memory (a virtual call, an import)
            tgt = self.val(ops[0], i)
            name = i.sym[0] if i.sym else None
        conv, pop = convention(name, len(st.pushes), indirect=tgt[0] != "a")
        own = False
        if tgt[0] != "a" and name is None:                 # an indirect call: virtual (thiscall) only through a
            c_, ts_ = lin(tgt[2]) if tgt[0] == "ld" else (0, {})   # vtable, `call [[obj] + k]`; else a function
            virtual = len(ts_) == 1 and next(iter(ts_))[0] == "ld"     # pointer: cdecl when the caller cleans up
            nxt = getattr(self, "next_ins", None)
            if not virtual:
                cdecl = nxt is not None and nxt.mn == "add" and re.match(r"\s*esp\s*,", nxt.ops)
                conv, pop = ("cdecl", 0) if cdecl else ("stdcall", 4 * len(st.pushes))
                own = True                                 # each side reads its own cleanup
        k = len(st.calls)
        if (name or "").lstrip("_") in ("chkstk", "alloca_probe", "alloca_probe_16") or (
                self.cleanups is not None and k < len(self.cleanups) and self.cleanups[k][0] == "chkstk"):
            size = self.reg("eax")                         # the stack probe: esp -= eax, nothing else observable
            if not is_c(size):
                raise Unknown("a stack probe of a non-constant size")
            st.depth += size[1]
            st.calls.append(("chkstk", 0))
            st.regs["eax"] = ("a", "clob", (self.side.name, st.blk, len(st.effects), "chk"))
            return
        if self.cleanups is not None and not own:          # the original's side: the paired call's cleanup
            if k >= len(self.cleanups):
                pop = 4 * len(st.pushes) if tgt[0] != "a" else 0
            else:
                conv, pop = self.cleanups[k]
        args = ("args",) + tuple(reversed(st.pushes))

        regs = ("regs",)
        if conv in ("thiscall", "fastcall"):
            regs += (self.reg("ecx"),)
        if conv == "fastcall":
            regs += (self.reg("edx"),)
        eff = ("call", tgt, args, regs)
        self.effect(eff)
        st.calls.append((conv, pop))
        n = len(st.effects)
        st.regs["eax"] = ("a", "ret", (self.side.name, st.blk, n))
        st.regs["edx"] = ("a", "ret2", (self.side.name, st.blk, n))
        st.regs["ecx"] = ("a", "clob", (self.side.name, st.blk, n))
        st.depth -= pop
        st.pushes = []
        st.flags = None
        st.fst_ret = ("a", "fret", (self.side.name, st.blk, n))

    def string(self, mn, i):
        st = self.st
        self.effect(("rep", mn, self.reg("ecx"), self.reg("esi") if "movs" in mn else self.reg("eax"),
                     self.reg("edi")))
        st.regs["ecx"] = C(0) if mn.split()[0] == "rep" else ("a", "strend", (self.side.name, st.blk, len(st.effects), "ecx")) if mn.startswith("rep") else st.get("ecx")
        st.flags = ("res", ("a", "strend", (self.side.name, st.blk, len(st.effects), "flags")), mn) if "cmps" in mn or "scas" in mn else st.flags
        n = len(st.effects)
        st.regs["esi"] = ("a", "strend", (self.side.name, st.blk, n, "esi"))
        st.regs["edi"] = ("a", "strend", (self.side.name, st.blk, n, "edi"))

    # x87 and SSE scalar -------------------------------------------------------------------------------------
    def fpush(self, v):
        self.st.fst.append(v)

    def fpop(self):
        if not self.st.fst:
            if getattr(self.st, "fst_ret", None) is not None and self.st.fst_under == 0:
                self.st.fst_under += 1
                return self.st.fst_ret                     # a float callee's result in st(0)
            self.st.fst_under += 1
            return self.st.phi(("st", self.st.fst_under))
        return self.st.fst.pop()

    def fget(self, k):
        while len(self.st.fst) <= k:
            self.st.fst.insert(0, self.st.phi(("st", "below", len(self.st.fst))))
        return self.st.fst[-1 - k]

    def fset(self, k, v):
        self.fget(k)
        self.st.fst[-1 - k] = v

    def fsrc(self, o, i, integer=False):
        o = o.strip()
        m = re.fullmatch(r"st\((\d)\)", o)
        if m:
            return self.fget(int(m.group(1)))
        if o == "st":
            return self.fget(0)
        v = self.load(self.addr_of(o, i), self.size(o))
        return op("i2f", v) if integer else op("fext", self.size(o), v)

    def flt(self, mn, ops, i):
        st = self.st
        m = re.fullmatch(r"f(i?)ld", mn)
        if m:
            self.fpush(self.fsrc(ops[0], i, integer=bool(m.group(1))))
            return
        if mn in ("fldz", "fld1"):
            self.fpush(("fc", "0" if mn == "fldz" else "1"))
            return
        m = re.fullmatch(r"f(i?)(st|stp)", mn) or re.fullmatch(r"f(i)(sttp)", mn)
        if m:
            v = self.fget(0)
            o = ops[0]
            if re.fullmatch(r"st\(\d\)|st", o.strip()):
                k = int(re.sub(r"\D", "", o) or 0)
                self.fset(k, v)
            else:
                bits = self.size(o)
                self.store(self.addr_of(o, i), op("f2i", bits, v) if m.group(1) else op("fround", bits, v), bits)
            if "p" in m.group(2):
                self.fpop()
            return
        m = re.fullmatch(r"f(i?)(add|sub|subr|mul|div|divr)(p?)", mn)
        if m:
            integer, o, pop = bool(m.group(1)), m.group(2), m.group(3)
            if len(ops) == 2:                              # fop st(i), st(0) / st(0), st(i)
                d = int(re.sub(r"\D", "", ops[0]) or 0)
                s = int(re.sub(r"\D", "", ops[1]) or 0)
                a, b = self.fget(d), self.fget(s)
            elif len(ops) == 1 and re.fullmatch(r"st\(\d\)", ops[0].strip()):
                k = int(re.sub(r"\D", "", ops[0]))           # capstone: `faddp st(i)` is st(i) = st(i) op st(0),
                if pop:                                    # popped; `fadd st(i)` is st(0) = st(0) op st(i)
                    d, a, b = k, self.fget(k), self.fget(0)
                else:
                    d, a, b = 0, self.fget(0), self.fget(k)
            elif len(ops) == 1:
                d, a, b = 0, self.fget(0), self.fsrc(ops[0], i, integer)
            else:
                d, a, b = 1, self.fget(1), self.fget(0)
                pop = "p"
            if o.endswith("r"):
                a, b = b, a
                o = o[:-1]
            r = op("f" + o, a, b) if o in ("add", "mul") else op("f" + o, a, b)
            self.fset(d, r)
            if pop:
                self.fpop()
            return
        if mn in ("fchs", "fabs", "fsqrt", "fsin", "fcos", "frndint"):
            self.fset(0, op(mn, self.fget(0)))
            return
        if mn == "fxch":
            k = int(re.sub(r"\D", "", ops[0]) or 1) if ops else 1
            a, b = self.fget(0), self.fget(k)
            self.fset(0, b)
            self.fset(k, a)
            return
        m = re.fullmatch(r"f(u?)com(i?)(p{0,2})", mn)
        if m:
            b = self.fsrc(ops[-1], i) if ops and not re.fullmatch(r"st\(\d\)", ops[-1].strip()) or not ops else None
            if ops and re.fullmatch(r"st\(\d\)", ops[-1].strip()):
                b = self.fget(int(re.sub(r"\D", "", ops[-1])))
            elif not ops:
                b = self.fget(1)
            a = self.fget(0)
            if m.group(2):
                st.flags = ("fcmp", a, b)
            else:
                st.fsw = ("fsw", a, b)
            for _ in m.group(3):
                self.fpop()
            return
        if mn == "ftst":
            st.fsw = ("fsw", self.fget(0), ("fc", "0"))
            return
        if mn in ("fnstsw", "fstsw"):
            if not st.fsw:
                raise Unknown("fnstsw without a compare")
            self.set_reg("ax", op("fswbits", st.fsw[1], st.fsw[2]))
            return
        if mn in ("fstp",) and not ops:
            self.fpop()
            return
        if mn == "fnclex" or mn == "fldcw" or mn == "fnstcw":
            raise Unknown("x87 control word (%s)" % mn)
        # SSE scalar
        m = re.fullmatch(r"(add|sub|mul|div|min|max|sqrt)(ss|sd)", mn)
        if m:
            a, b = self.xval(ops[0], i), self.xval(ops[1], i)
            self.xset(ops[0], op("f" + m.group(1), a, b), i)
            return
        if mn in ("movss", "movsd", "movaps", "movups", "movapd", "movd", "movq") and len(ops) == 2:
            self.xset(ops[0], self.xval(ops[1], i), i)
            return
        if mn in ("xorps", "xorpd", "pxor") and ops[0] == ops[1]:
            self.xset(ops[0], ("fc", "0"), i)
            return
        if mn in ("comiss", "ucomiss", "comisd", "ucomisd"):
            st.flags = ("fcmp", self.xval(ops[0], i), self.xval(ops[1], i))
            return
        if mn.startswith("cvt"):
            self.xset(ops[0], op(mn, self.xval(ops[1], i)), i)
            return
        raise Unknown("float instruction %s" % i.text)

    def xval(self, o, i):
        o = o.strip()
        if re.fullmatch(r"xmm\d", o):
            return self.st.get(o)
        if o in FAM:
            return self.reg(o)
        v = self.load(self.addr_of(o, i), self.size(o))
        return op("fext", self.size(o), v)

    def xset(self, o, v, i):
        o = o.strip()
        if re.fullmatch(r"xmm\d", o):
            self.st.regs[o] = v
        elif o in FAM:
            self.set_reg(o, v)
        else:
            self.store(self.addr_of(o, i), op("fround", self.size(o), v), self.size(o))

    # a block ----------------------------------------------------------------------------------------------
    def run(self, insns):
        """Execute a block and every block it alone enters by an unconditional transfer (a superblock: the other
        side may not split there); returns the terminator: ("jcc", cond, taken, fallthrough) / ("jmp", t) /
        ("ret", n). The blocks taken in are listed in st.absorbed."""
        blk = self.st.blk
        self.st.absorbed = []
        while True:
            term = None
            for q, i in enumerate(insns):
                self.next_ins = insns[q + 1] if q + 1 < len(insns) else None
                if is_jcc(i.mn):
                    term = ("jcc", self.cond(i.mn[1:]), target(i), self.side.next_addr(blk))
                    break
                if i.mn == "jmp":
                    term = ("jmp", target(i))
                    break
                if i.mn.startswith("ret"):
                    return ("ret", _num(i.ops) if i.ops.strip() else 0)
                self.step(i)
            if term is None:
                nxt = self.side.next_addr(blk)
                if nxt is None:
                    if insns and insns[-1].mn == "call":
                        return ("noret", 0)                # a call that does not return (a throw, exit)
                    raise Unknown("falls off the end of the function (%s's disassembly ends without ret/jmp: "
                                  "truncated?)" % ("our listing" if self.side.name == "o" else "the original"))
                term = ("jmp", nxt)
            if term[0] == "jmp":
                t = self.side.succ(term[1])
                if t not in self.side.cuts and t != self.side.entry and t not in self.st.absorbed and t != self.st.blk:
                    self.st.absorbed.append(t)
                    blk, insns = t, self.side.blocks[t]
                    continue
                return ("jmp", t)
            return term


def canon_cond(cc, a, b):
    if shape(a) > shape(b) and cc in CC_SWAP:
        a, b, cc = b, a, CC_SWAP[cc]
    return ("cond", cc, "cmp", a, b)


def negate(c):
    return ("cond", CC_NEG.get(c[1], "!" + c[1])) + c[2:]


def convention(name, npush, indirect=False):
    """(convention, bytes the callee pops) from a decorated name (our side)."""
    if name is None:
        if indirect:
            return "thiscall", 4 * npush                  # a virtual call
        return "cdecl", 0
    n = name
    if n.startswith("__imp_"):
        n = n[6:]
    if n.startswith("?"):
        k = n.find("@@")
        if k < 0 or k + 2 >= len(n):
            return "cdecl", 0
        code = n[k + 2]
        if code == "Y" or code in "CDKLST":
            cc = n[k + 3] if k + 3 < len(n) else "A"
        else:
            cc = n[k + 4] if k + 4 < len(n) else "A"
        if cc in "EF":
            return "thiscall", 4 * npush
        if cc in "GH":
            return "stdcall", 4 * npush
        if cc in "IJ":
            return "fastcall", 4 * npush
        return "cdecl", 0
    m = re.fullmatch(r"_\w+@(\d+)", n)
    if m:
        return "stdcall", int(m.group(1))
    m = re.fullmatch(r"@\w+@(\d+)", n)
    if m:
        return "fastcall", int(m.group(1))
    return "cdecl", 0


def returns(name):
    """'void', 'int', 'float', 'int64' or None (unknown) from a decorated name."""
    if not name or not name.startswith("?"):
        return None
    k = name.find("@@")
    if k < 0:
        return None
    code = name[k + 2]
    j = k + 3 if (code == "Y" or code in "CDKLST") else k + 4
    j += 1                                                 # the convention letter
    if j < len(name) and name[j] == "?":
        j += 2                                             # a cv-qualified return (?B)
    t = name[j:j + 2]
    if t[:1] == "X":
        return "void"
    if t[:1] in "MNO":
        return "float"
    if t[:2] in ("_J", "_K"):
        return "int64"
    return "int"


# ---------------------------------------------------------------------------------------------------- comparison

class Pairing:
    """The bijection between our atoms and the original's, with a trail for undo."""

    def __init__(self, budget=2000000):
        self.fw, self.bw, self.trail = {}, {}, []
        self.symoff = {}                                   # our symbol -> the original's address
        self.memo = {}                                     # (id(x), id(y)) -> trail length when proved equal
        self.consts = {}                                   # entry atom -> the constant it is on every path
        self.known = {}                                    # our symbol -> its address in the original (the project's)
        self.steps, self.budget = 0, budget

    def mark(self):
        return len(self.trail)

    def undo(self, m):
        if len(self.trail) > m:
            self.memo = {k: v for k, v in self.memo.items() if v[0] <= m}
        while len(self.trail) > m:
            kind, a, b = self.trail.pop()
            if kind == "atom":
                del self.fw[a]
                del self.bw[b]
            elif kind == "const":
                del self.consts[a]
            else:
                del self.symoff[a]

    def constphi(self, a, c):
        """a block entry value (either side) claimed equal to the constant c: checked on every incoming edge."""
        if a in self.consts:
            return self.consts[a] == c
        if a in self.fw or a in self.bw:
            return False
        self.consts[a] = c
        self.trail.append(("const", a, None))
        return True

    def bind(self, a, b):
        if a in self.fw:
            return self.fw[a] == b
        if b in self.bw:
            return False
        if a[1] != b[1] and not (a[1] in ("phi", "init") and b[1] in ("phi", "init")):
            return False
        if a[1] in ("init", "fobj") or b[1] in ("init", "fobj"):
            if a != b:
                return False                               # entry values (the convention), our frame object k
        if a[1] in ("ret", "ret2", "clob", "fret", "strend") and False:
            return False
        self.fw[a], self.bw[b] = b, a
        self.trail.append(("atom", a, b))
        return True


def eq(x, y, P):
    """Are our x and the original's y the same value under the pairing (extended as needed)?"""
    k = (id(x), id(y))
    if k in P.memo:
        return True
    P.steps += 1
    if P.steps > P.budget:
        raise Unknown("comparison budget exhausted (%d steps)" % P.budget)
    r = _eq(x, y, P)
    if r:
        P.memo[k] = (len(P.trail), x, y)                   # x, y kept alive: an id is reused once freed
    return r


def _eq(x, y, P):
    if x is y:
        return True
    for p_, c_, side in ((x, y, "o"), (y, x, "r")):     # a block's entry value that is a constant on every path
        if p_[0] == "a" and p_[1] == "phi" and c_[0] == "c":
            return P.constphi(p_, c_)
    if x[0] != y[0]:
        if x[0] == "sum" or y[0] == "sum" or x[0] == "a" and x[1] == "sym" or y[0] == "a" and y[1] == "sym":
            return eq_sum(x, y, P)
        return False
    t = x[0]
    if t == "c":
        return x[1] == y[1]
    if t == "a":
        if x[1] == "sym" and y[1] == "sym":
            return eq_sum(x, y, P)
        return P.bind(x, y)
    if t == "sum":
        return eq_sum(x, y, P)
    if len(x) != len(y):
        return False
    if t == "chain":                                   # the aliasing effects, already paired in order
        return eq_bag(list(x[1:]), list(y[1:]), P)
    if t == "op" and x[1] in COMMUTATIVE:
        if x[1] != y[1]:
            return False
        return eq_bag(list(x[2:]), list(y[2:]), P)
    if t == "cond" and x[1] in ("e", "ne") and x[2] == "cmp":
        m = P.mark()
        if x[1] == y[1] and eq(x[3], y[3], P) and eq(x[4], y[4], P):
            return True
        P.undo(m)
        return x[1] == y[1] and eq(x[3], y[4], P) and eq(x[4], y[3], P)
    if t == "cond" and x[2] == "cmp" and y[2] == "cmp" and x[1] != y[1] and CC_SWAP.get(x[1]) == y[1]:
        return eq(x[3], y[4], P) and eq(x[4], y[3], P)
    for a, b in zip(x[1:], y[1:]):
        if isinstance(a, tuple) and isinstance(b, tuple):
            if not eq(a, b, P):
                return False
        elif a != b:
            return False
    return True


def eq_bag(xs, ys, P):
    if len(xs) != len(ys):
        return False
    if not xs:
        return True
    x = xs[0]
    for k, y in enumerate(ys):
        if shape_compatible(x, y):
            m = P.mark()
            if eq(x, y, P) and eq_bag(xs[1:], ys[:k] + ys[k + 1:], P):
                return True
            P.undo(m)
    return False


def shape_compatible(x, y):
    if x[0] != y[0]:
        return x[0] == "a" or y[0] == "a" or x[0] == "sum" or y[0] == "sum"
    if x[0] == "op":
        return x[1] == y[1]
    if x[0] == "c":
        return x == y
    return True


def eq_sum(x, y, P):
    """Linear forms: the constants equal, the terms paired with equal coefficients; one symbol term on each side
    pairs our symbol with the original's address (the constants then differ by the symbol's offset)."""
    cx, tx = lin(x)
    cy, ty = lin(y)
    sx = [t for t in tx if t[0] == "a" and t[1] == "sym"]
    sy = [t for t in ty if t[0] == "a" and t[1] == "sym"]
    m = P.mark()
    if not sx and len(sy) == 1 and isinstance(sy[0][2], int) and ty[sy[0]] == 1:
        ty = {t: c for t, c in ty.items() if t != sy[0]}       # our plain constant that is that address
        cy = (cy + sy[0][2]) & M32
        sy = []
    if sx or sy:
        if len(sx) != 1 or len(sy) != 1 or tx[sx[0]] != 1 or ty[sy[0]] != 1:
            return False
        a, b = sx[0][2], sy[0][2]
        if isinstance(a, int) and isinstance(b, int):          # both sides addresses (a self-check)
            if (a + cx - b - cy) & M32:
                return False
        else:
            base = (b + cy - cx) & M32 if isinstance(b, int) else None
            if a in P.symoff:
                if P.symoff[a] != base:
                    return False
            elif base is not None:
                if base in P.symoff.values() or (P.known and P.known.get(a, base) != base):
                    P.undo(m)
                    return False                           # one-to-one, and the project's own map where known
                P.symoff[a] = base
                P.trail.append(("sym", a, None))
            else:
                if a != b:
                    return False
        tx = {t: c for t, c in tx.items() if t != sx[0]}
        ty = {t: c for t, c in ty.items() if t != sy[0]}
    elif cx != cy:
        return False
    if len(tx) != len(ty):
        P.undo(m)
        return False
    if not tx:
        return True
    by_coef_x = sorted(tx.items(), key=lambda kv: (kv[1], shape(kv[0])))
    ok = _match_terms(by_coef_x, list(ty.items()), P)
    if not ok:
        P.undo(m)
    return ok


def _match_terms(xs, ys, P):
    if not xs:
        return not ys
    (t, c) = xs[0]
    for k, (u, d) in enumerate(ys):
        if c == d and shape_compatible(t, u):
            m = P.mark()
            if eq(t, u, P) and _match_terms(xs[1:], ys[:k] + ys[k + 1:], P):
                return True
            P.undo(m)
    return False


def _store_key(e):
    """A store's canonical order key: its address as a linear form, terms keyed by shape and (for a symbol atom) by the
    symbol as a string (13.00.9044's symbol atoms hold an address, an int: `shape(t), k` sorts unlike types),
    then the constant and the size."""
    c, ts = lin(e[1])
    return (repr(sorted((shape(t), t[2] if t[0] == "a" and t[1] == "sym" else 0, k) for t, k in ts.items())), c, e[3])


def canon_effects(effs):
    """Indices of the effects with runs of stores that cannot alias each other in a canonical order (the
    scheduler's freedom)."""
    out = list(range(len(effs)))
    key = lambda k: _store_key(effs[k])
    k = 0
    while k < len(out):
        j = k
        while j < len(out) and effs[out[j]][0] == "st" and all(
                not mem_alias(effs[out[j]][1], effs[out[j]][3], effs[out[q]][1], effs[out[q]][3]) for q in range(k, j)):
            j += 1
        if j - k > 1:
            out[k:j] = sorted(out[k:j], key=key)
        k = max(j, k + 1)
    return out


# --------------------------------------------------------------------------------------------------- the judge

class Verdict(Exception):
    pass


def judge_pair(ours, orig, func=None, image=None, show_pairs=False, budget=2000000, known=None, frame=None,
               frame_objects=False, listing=True):
    """Judge two instruction lists ([Ins]). With `listing` (the file paths: judge_files, judge_job), our side is read
    as a listing's text (`Side` in listing mode: a `DWORD PTR`/`_n$[esp]` frame form), its frame variables from `frame`
    (judge_files' frame_objects) or, without one, from the text's own `_n$[esp+N]` offsets (listing_frame). With
    `listing=False` (the synthetic tests) both sides are capstone texts. With `frame` and a local whose address
    escapes: pass 1 treats every local as memory (and pairs the escaped addresses), pass 2 only the escaped objects'
    bytes (ours from the listing; the original's: the paired address, our object's size). Pass 1's EQUIVALENT stands;
    otherwise pass 2's verdict."""
    fsyms = {name: v for name, v, n, esc in frame or ()}
    if not frame_objects:                                  # pass 2 is off by default: it used our object's size
        frame = None                                       # for the original's (docs/c2/judge.md section 4)
    if listing and not fsyms:                              # no map declared: the listing text's own offsets
        fsyms = listing_frame([str(i.text or "") for i in ours])
    r1 = _judge(ours, orig, func, image, show_pairs, budget, known, collect=True, listing=listing, fsyms=fsyms)
    keys = ("_fa", "_escaped", "_orig_escapes", "_our_escapes", "_F")
    if r1["verdict"] == "EQUIVALENT" or not frame or not r1.get("_escaped") or r1.get("_F") is None:
        for k in keys:
            r1.pop(k, None)
        return r1
    F = r1["_F"]
    objs = [(v + F, n, k) for k, (name, v, n, esc) in enumerate(frame) if esc]
    pairs_fa = r1["_fa"]                                  # our escaped offset -> the original's
    ours_regions = objs
    orig_regions = []
    for o, n, k in objs:
        if o > 0:                                          # an incoming argument: the convention fixes it
            orig_regions.append((o, n, k))
            continue
        e = next((e for e in sorted(pairs_fa) if o <= e < o + n), None)   # any paired address inside it
        if e is not None:
            orig_regions.append((pairs_fa[e] - (e - o), n, k))
    inside = lambda e, rs: any(b <= e < b + n for b, n, _ in rs)
    used = {k for o, n, k in objs if any(o <= e < o + n for e in r1["_our_escapes"])}
    if any(not inside(e, orig_regions) for e in r1["_orig_escapes"]) or \
            any(not inside(e, ours_regions) for e in r1["_our_escapes"]) or \
            not used <= {k for _, _, k in orig_regions}:
        r1["reason"] += " (pass 2 not run: escaped frame objects that do not pair)"
        for k in keys:
            r1.pop(k, None)
        return r1
    r2 = _judge(ours, orig, func, image, show_pairs, budget, known, regions=(ours_regions, orig_regions),
                listing=listing)
    for k in keys:
        r2.pop(k, None)
    r2["hints"] = r2.get("hints") or r1.get("hints")
    r2["frame"] = "objects"
    return r2


def _merge(regions):
    out = []
    for b, n in sorted(regions):
        if out and b <= out[-1][0] + out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][0] + out[-1][1], b + n) - out[-1][0])
        else:
            out.append((b, n))
    return out


judge = judge_pair       # the old name (the synthetic tests' helper); judge_pair's `listing` names the listing mode


def _judge(ours, orig, func=None, image=None, show_pairs=False, budget=2000000, known=None, regions=None,
            collect=False, fsyms=None, listing=True):
    try:
        A = Side("o", ours, listing=listing)
        B = Side("r", orig, image=image)
    except Unknown as e:
        return dict(verdict="UNKNOWN", reason=str(e))
    if regions is not None:
        A.regions, B.regions = regions
    A.fsyms = dict(fsyms or {})
    P = Pairing(budget)
    P.known = dict(known or {})
    ret = returns(func)
    nodes = {}                                             # (our block, the original's) -> entry depths
    ends = {}                                              # (side, node) -> that side's State at the node's end
    edges = set()                                          # (our predecessor node, the original's, node)
    work = [(A.entry, B.entry, None, None)]

    def new_state(side, blk, pred, node):
        if pred is not None and blk not in side.cuts and blk != side.entry:
            pe = ends[(side.name, pred)]
            return State(side, blk, pe.depth, False, parent=pe, node=node)
        pe = ends[(side.name, pred)] if pred is not None else None
        return State(side, blk, pe.depth if pe else 0, pred is None, node=node)

    first = []                                             # collect mode: the first mismatch, the walk goes on

    def fail(msg):
        if not collect:
            raise Verdict(("MISMATCH", msg))
        if not first:
            first.append(msg)

    def compare_effects(a, b, sa, sb):
        ia, ib = canon_effects(sa.effects), canon_effects(sb.effects)
        if len(ia) != len(ib):
            if collect:                                    # still pair what lines up (the escaped addresses),
                import difflib                             # aligned by kind: calls with calls
                ka = [sa.effects[k][0] for k in ia]
                kb = [sb.effects[k][0] for k in ib]
                for blk_ in difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_matching_blocks():
                    for q in range(blk_.size):
                        m = P.mark()
                        if not eq(sa.effects[ia[blk_.a + q]], sb.effects[ib[blk_.b + q]], P):
                            P.undo(m)
            raise Verdict(("MISMATCH", "block %#x / %#x: %d effects against %d: ours %s; original %s" % (
                a, b, len(ia), len(ib), "; ".join(show(sa.effects[k]) for k in ia[:6]),
                "; ".join(show(sb.effects[k]) for k in ib[:6]))))
        for kx, ky in zip(ia, ib):
            x, y = sa.effects[kx], sb.effects[ky]
            m = P.mark()
            if not eq(x, y, P) or not P.bind(sa.eff_atoms[kx], sb.eff_atoms[ky]):
                P.undo(m)
                P.bind(sa.eff_atoms[kx], sb.eff_atoms[ky])
                fail("block %#x / %#x: ours %s; original %s" % (a, b, show(x), show(y)))

    pairs = nodes
    try:
        steps = 0
        while work:
            steps += 1
            if steps > 20000 or len(nodes) > 4000:
                raise Unknown("too many block pairings")
            a, b, po, pr = work.pop(0)
            da = ends[("o", po)].depth if po is not None else 0
            db = ends[("r", pr)].depth if pr is not None else 0
            cut_a = a in A.cuts or a == A.entry
            cut_b = b in B.cuts or b == B.entry
            if cut_a != cut_b:                             # one side reaches its loop head through a block that
                side, S_, blk, pred = (("o", A, a, po) if not cut_a else ("r", B, b, pr))   # only moves values
                st = new_state(S_, blk, pred, ("fwd", a, b, steps))
                t = Exec(S_, st).run(S_.blocks[blk])
                if st.effects or t[0] != "jmp" or S_.succ(t[1]) not in S_.cuts:
                    raise Verdict(("UNKNOWN", "loop structure differs at %#x / %#x" % (a, b)))
                ends[(side, st.node)] = st
                work.insert(0, (S_.succ(t[1]), b, st.node, pr) if side == "o" else (a, S_.succ(t[1]), po, st.node))
                continue
            node = (a, b) if cut_a else ("path", a, b, steps)
            if node in nodes:
                if nodes[node] != (da, db):
                    raise Verdict(("UNKNOWN", "stack depth differs at block %#x / %#x on two paths" % node))
                edges.add((po, pr, node))
                continue
            sa, sb = new_state(A, a, po, node), new_state(B, b, pr, node)
            ta = Exec(A, sa).run(A.blocks[a])
            tb = Exec(B, sb, cleanups=sa.calls).run(B.blocks[b])
            nodes[node] = (da, db)
            edges.add((po, pr, node))
            ends[("o", node)], ends[("r", node)] = sa, sb
            compare_effects(a, b, sa, sb)
            if ta[0] != tb[0]:
                raise Verdict(("UNKNOWN", "block %#x ends in %s, the original's %#x in %s" % (a, ta[0], b, tb[0])))
            if node[0] != "path":
                pass
            if ta[0] == "noret":
                continue
            if ta[0] == "ret":
                if sa.depth != 0 or sb.depth != 0:
                    raise Verdict(("UNKNOWN", "stack depth at ret %d / %d" % (sa.depth, sb.depth)))
                if ta[1] != tb[1]:
                    raise Verdict(("MISMATCH", "ret %d against ret %d" % (ta[1], tb[1])))
                if ret in (None, "int", "int64"):
                    m = P.mark()
                    if not eq(sa.get("eax"), sb.get("eax"), P):
                        P.undo(m)
                        why = "returns %s; original %s" % (show(sa.get("eax")), show(sb.get("eax")))
                        raise Verdict(("MISMATCH" if ret else "UNKNOWN",
                                       why + ("" if ret else " (the return type is unknown: void?)")))
                if ret == "int64" and not eq(sa.get("edx"), sb.get("edx"), P):
                    raise Verdict(("MISMATCH", "returns edx %s; original %s" % (show(sa.get("edx")), show(sb.get("edx")))))
                if ret == "float":
                    fa_ = sa.fst[-1] if sa.fst else None
                    fb_ = sb.fst[-1] if sb.fst else None
                    if fa_ is None or fb_ is None or not eq(fa_, fb_, P):
                        raise Verdict(("MISMATCH", "returns st(0) %s; original %s" % (
                            fa_ and show(fa_), fb_ and show(fb_))))
                continue
            if ta[0] == "jmp":
                work.append((A.succ(ta[1]), B.succ(tb[1]), node, node))
                continue
            ca, cb = ta[1], tb[1]
            sa_t, sa_f = A.succ(ta[2]), A.succ(ta[3])
            sb_t, sb_f = B.succ(tb[2]), B.succ(tb[3])
            m = P.mark()
            if eq(ca, cb, P):
                nxt = [(sa_t, sb_t), (sa_f, sb_f)]
            else:
                P.undo(m)
                if eq(ca, negate(cb), P):
                    nxt = [(sa_t, sb_f), (sa_f, sb_t)]
                else:
                    P.undo(m)
                    fail("block %#x / %#x branch: ours %s; original %s" % (a, b, show(ca), show(cb)))
                    nxt = [(sa_t, sb_t), (sa_f, sb_f)]
            for x, y in nxt:
                work.append((x, y, node, node))
        # every edge into a product node: the node's entry pairings hold on it (induction over the paths)
        into = collections.defaultdict(set)
        for po, pr, node in edges:
            into[node].add((po, pr))
        checked = set()
        changed = True
        while changed:
            changed = False
            for x, c in list(P.consts.items()):
                if ("const", x) in checked:
                    continue
                checked.add(("const", x))
                changed = True
                side, node, key = x[2]
                es = into[node]
                for po, pr in es:
                    p = po if side == "o" else pr
                    if p is None:
                        raise Verdict(("MISMATCH", "an entry value of the function is not %s" % show(c)))
                    v = end_value(ends[(side, p)], key)
                    if v[0] == "a" and v[1] == "phi":
                        if not P.constphi(v, c):
                            raise Verdict(("MISMATCH", "entering %#x / %#x: %s is not %s" % (
                                node + (name_of(key), show(c)))))
                    elif v != c:
                        raise Verdict(("MISMATCH" if v[0] == "c" else "UNKNOWN",
                                       "entering %#x / %#x: %s is %s, not the constant %s" % (
                                           node + (name_of(key), show(v), show(c)))))
            for x, y in list(P.fw.items()):
                if x[1] != "phi" or (x, y) in checked:
                    continue
                checked.add((x, y))
                changed = True
                _, node, key_a = x[2]
                _, node_b, key_b = y[2]
                if node != node_b:
                    raise Verdict(("MISMATCH", "an entry value of %#x / %#x paired with one of %#x / %#x" % (
                        node + node_b)))
                for po, pr in into[node]:
                    if po is None or pr is None:
                        raise Verdict(("UNKNOWN", "an entry value of the function's first block"))
                    va = end_value(ends[("o", po)], key_a)
                    vb = end_value(ends[("r", pr)], key_b)
                    if not eq(va, vb, P):
                        raise Verdict(("MISMATCH", "entering %#x / %#x: our %s is %s; the original's %s is %s" % (
                            node + (name_of(key_a), show(va), name_of(key_b), show(vb)))))
        if first:
            raise Verdict(("MISMATCH", first[0]))
        res = dict(verdict="EQUIVALENT", reason="%d paths and loop heads paired" % len(pairs), blocks=len(pairs))
    except Verdict as v:
        res = dict(verdict=v.args[0][0], reason=v.args[0][1], blocks=len(pairs))
    except Unknown as e:
        res = dict(verdict="UNKNOWN", reason=str(e), blocks=len(pairs))
    except RecursionError:
        res = dict(verdict="UNKNOWN", reason="expression too deep", blocks=len(pairs))
    if first and res["verdict"] != "MISMATCH":             # collect mode: the first mismatch is the verdict
        res = dict(verdict="MISMATCH", reason=first[0], blocks=len(pairs))
    res["symbols"] = {a: "%#x" % b for a, b in P.symoff.items() if b is not None} if show_pairs else len(P.symoff)
    res["unanchored"] = sum(1 for a in P.symoff if a not in P.known)   # pairings assumed, not from the project's map
    res["_escaped"] = A.escaped or B.escaped
    res["_fa"] = {x[2]: y[2] for x, y in P.fw.items() if x[1] == "fa"}
    res["_orig_escapes"] = sorted(B.escapes)
    res["_our_escapes"] = sorted(A.escapes)
    res["_F"] = min(A.fcands) if A.fcands else None
    res["hints"] = hints(P, B)
    return res


def _hname(key):
    """A pairing key as a hint name: a register, or `esp+0x18` / `esp-0x1c` (offset from esp at entry)."""
    if isinstance(key, tuple) and key[0] == "s":
        return "esp%s%#x" % ("+" if key[1] >= 0 else "-", abs(key[1]))
    if isinstance(key, tuple) and key[0] == "a" and key[1] == "sym":   # a call target: ours a name, the original an
        return re.sub(r"[^\w?@$]", "_", str(key[2]))                  # address (`?f@@...` -> `_f___...`)
    return str(key)


def hints(P, B):
    """What the judge paired, for another prover: per loop head, our register / stack slot holding the same value as
    the original's (`at`); the pairings that hold at every loop head (`regs`, `slots`); escaped stack addresses;
    our symbols with the original's addresses (`globals`), those no project map anchored listed in `assumed`."""
    at = collections.defaultdict(lambda: dict(regs={}, slots={}))
    for x, y in P.fw.items():
        if x[1] != "phi" or y[1] != "phi" or not isinstance(x[2][1], tuple) or x[2][1][0] == "path":
            continue
        node, kx, ky = x[2][1], x[2][2], y[2][2]
        if kx == "mem" or (isinstance(kx, tuple) and kx[0] == "st"):
            continue
        kind = "slots" if isinstance(kx, tuple) else "regs"
        at["%#x" % B.blocks[node[1]][0].addr if node[1] in B.blocks else str(node)][kind]["ours:" + _hname(kx)] = \
            "orig:" + _hname(ky)
    flat = dict(regs={}, slots={})
    clash = set()
    for m in at.values():
        for kind in ("regs", "slots"):
            for a, b in m[kind].items():
                if flat[kind].get(a, b) != b:
                    clash.add((kind, a))
                flat[kind][a] = b
    for kind, a in clash:
        flat[kind].pop(a, None)
    for x, y in P.fw.items():                              # escaped stack addresses
        if x[1] == "fa" and y[1] == "fa":
            flat["slots"].setdefault("ours:" + _hname(("s", x[2])), "orig:" + _hname(("s", y[2])))
    glob = {"ours:" + a: "orig:%#x" % b for a, b in P.symoff.items() if b is not None}
    return dict(regs=flat["regs"], slots=flat["slots"], globals=glob,
                assumed=sorted("ours:" + a for a in P.symoff if a not in P.known),
                at={k: v for k, v in sorted(at.items())})


def end_value(st, key):
    if key == "mem":
        return ("memend", ("chain",) + tuple(t for _, t in st.inh) + tuple(st.eff_atoms), st.phi("mem"))
    if isinstance(key, tuple) and key[0] == "s":
        return st.slot(key[1])
    if isinstance(key, tuple) and key[0] == "st":
        raise Unknown("x87 stack live across blocks")
    return st.get(key)


def name_of(key):
    if isinstance(key, tuple) and key[0] == "s":
        return "slot %+d" % key[1]
    return str(key)


# --------------------------------------------------------------------------------------------------------- CLI

def judge_files(asm, func, orig_s, show_pairs=False, frame_objects=False):
    import tsweep
    lst = tsweep.listing_instrs(asm, func)
    org = tsweep.orig_instrs(orig_s)
    try:
        ours = ours_instrs(lst)
    except Unknown as e:
        return dict(verdict="UNKNOWN", reason=str(e))
    lo, hi = tsweep._addr_range(org.va0)
    org_ins = orig_instrs(_trim_orig(org))
    return judge_pair(ours, org_ins, func, image=(lo, max(hi, lo + 0x10000000)), show_pairs=show_pairs,
                 frame=globals()["frame_objects"](asm, func), frame_objects=frame_objects)


def _trim_orig(org):
    """The original without its jump / index tables after the code (tsweep.orig_instrs keeps addrs aligned)."""
    return org


def compile_listing(src, func, cl, work):
    import c2trace, c2regs
    os.makedirs(work, exist_ok=True)
    _, _, _, out, log = c2trace.run_trace(os.path.abspath(src), c2regs.HOOKS, list(cl) + ["/FAsc"], keep=work,
                                          quiet=True)
    asm = os.path.join(work, os.path.splitext(os.path.basename(src))[0] + ".asm")
    if not os.path.exists(asm):
        raise SystemExit("compile failed: " + out[-400:])
    return asm


def load_symbols(paths):
    """{decorated name: address} from a project's symbol files (lines `VA name ...`; a directory: its *.txt)."""
    import glob
    out = {}
    for p in paths:
        for f in sorted(glob.glob(os.path.join(p, "**", "*.txt"), recursive=True)) if os.path.isdir(p) else [p]:
            for ln in open(f, errors="replace"):
                m = re.match(r"\s*(?:0x)?([0-9a-fA-F]{6,8})\s+([?@_A-Za-z$][^\s#]*)", ln)
                if m:
                    out.setdefault(m.group(2), int(m.group(1), 16))
    return out


def export_row(r, symbols=None):
    """A job: both sides decoded to plain lists (no capstone, no listing parser needed to judge it)."""
    import tsweep
    job = dict(id=r["id"], func=r["func"])
    try:
        lst = tsweep.listing_instrs(r["asm"], r["func"])
        org = tsweep.orig_instrs(r["orig"])
        lo, hi = tsweep._addr_range(org.va0)
        job["image"] = [lo, max(hi, lo + 0x10000000)]      # .bss can lie far above the code (Mount & Blade 0x8ee....)
        job["ours"] = [[i.addr, i.mn, i.ops, list(i.sym) if i.sym else None, i.text] for i in ours_instrs(lst)]
        job["frame"] = frame_objects(r["asm"], r["func"])
        job["orig"] = [[i.addr, i.mn, i.ops] for i in orig_instrs(org)]
        if symbols:                                        # the project's addresses of the names our code uses
            names = {x[3][0] for x in job["ours"] if x[3]}
            job["known"] = {n: symbols[n] for n in names if n in symbols}
    except Unknown as e:
        job["verdict"], job["reason"] = "UNKNOWN", str(e)
    except Exception as e:
        job["verdict"], job["reason"] = "ERROR", "%s: %s" % (type(e).__name__, e)
    return job


def judge_job(job, budget=2000000, frame_objects=False):
    if "verdict" in job:
        return dict(id=job["id"], verdict=job["verdict"], reason=job["reason"])
    ours = [Ins(x[0], x[1], x[2], sym=tuple(x[3]) if x[3] else None, text=x[4] if len(x) > 4 else "%s %s" % (x[1], x[2]))
            for x in job["ours"]]
    orig = [Ins(a, mn, ops, text="%s %s" % (mn, ops)) for a, mn, ops in job["orig"]]
    sys.setrecursionlimit(20000)
    res = judge_pair(ours, orig, job["func"], image=tuple(job["image"]), budget=budget, known=job.get("known"),
                     frame=job.get("frame"), frame_objects=frame_objects or job.get("frame_objects", False))
    res["id"] = job["id"]
    return res


def run_jobs(path, out, jobs, timeout, budget):
    """One subprocess per job (a runaway comparison is killed at the timeout, its memory with it)."""
    import subprocess, threading
    lines = [l for l in open(path) if l.strip()]
    res, lock = [], threading.Lock()
    todo = list(enumerate(lines))

    def worker():
        while True:
            with lock:
                if not todo:
                    return
                k, line = todo.pop(0)
            jid = json.loads(line)["id"]
            try:
                p = subprocess.run([sys.executable, os.path.abspath(__file__), "one", "--budget", str(budget)],
                                   input=line, capture_output=True, text=True, timeout=timeout)
                r = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else dict(
                    id=jid, verdict="ERROR", reason=(p.stderr or "no output")[-300:])
            except subprocess.TimeoutExpired:
                r = dict(id=jid, verdict="UNKNOWN", reason="timeout (%d s)" % timeout)
            with lock:
                res.append(r)
                with open(out, "a") as f:
                    f.write(json.dumps(r) + "\n")
    open(out, "w").close()
    ts = [threading.Thread(target=worker) for _ in range(jobs)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    c = collections.Counter(r["verdict"] for r in res)
    print(" ".join("%s %d" % kv for kv in sorted(c.items())))


INV = {"je": "jne", "jne": "je", "jl": "jge", "jge": "jl", "jle": "jg", "jg": "jle", "jb": "jae", "jae": "jb",
       "jbe": "ja", "ja": "jbe", "js": "jns", "jns": "js", "jp": "jnp", "jnp": "jp"}


def mutants(job, per_kind=3):
    """Single-instruction mutants of the original's side of a job: an immediate + 1, a displacement + 4, a branch
    inverted, an instruction dropped, the operands of a sub / cmp swapped. A judge that calls a mutant EQUIVALENT
    is unsound there (unless the mutated instruction is dead)."""
    out = []
    orig = job["orig"]
    by = collections.defaultdict(list)
    for k, (a, mn, ops) in enumerate(orig):
        o = split_ops(ops)
        if mn in INV:
            by["jcc"].append((k, [a, INV[mn], ops]))
        if mn not in ("call", "ret", "push", "pop") and not mn.startswith("j") and len(o) == 2 and o[0] != "esp" and re.fullmatch(
                r"(0x[0-9a-f]+|\d+)", o[1].strip()) and not (job["image"][0] <= _num(o[1]) < job["image"][1]):
            by["imm"].append((k, [a, mn, "%s, %#x" % (o[0], (_num(o[1]) + 1) & M32)]))
        m = re.search(r"\[([^\]]*?)([+-]\s*)(0x[0-9a-f]+|\d+)\]", ops)
        if m and "esp" not in m.group(1) and "ebp" not in m.group(1) and mn != "lea":
            d = _num(m.group(3)) + 4
            by["disp"].append((k, [a, mn, ops[:m.start(3)] + "%#x" % d + ops[m.end(3):]]))
        if mn in ("mov", "add", "sub", "and", "or", "xor", "inc", "dec", "shl", "sar", "shr", "imul") and \
                "esp" not in ops:
            by["drop"].append((k, None))
        nxt = orig[k + 1][1] if k + 1 < len(orig) else ""
        if mn in ("sub", "cmp") and len(o) == 2 and all(x.strip() in FAM for x in o) and o[0] != o[1] and \
                nxt not in ("je", "jne"):                  # an equality test does not see the order
            by["swap"].append((k, [a, mn, "%s, %s" % (o[1], o[0])]))
    for kind, cands in sorted(by.items()):
        step = max(1, len(cands) // per_kind)
        for n, (k, rep_) in enumerate(cands[::step][:per_kind]):
            m = dict(job, id="%s#%s%d" % (job["id"], kind, n), orig=list(orig))
            if rep_ is None:
                m["orig"] = orig[:k] + orig[k + 1:]
            else:
                m["orig"][k] = rep_
            m["mutant"] = dict(kind=kind, at=orig[k][0], was="%s %s" % (orig[k][1], orig[k][2]))
            out.append(m)
    return out


def main(argv):
    if argv and argv[0] == "mutants":
        ap = argparse.ArgumentParser(prog="judge.py mutants")
        ap.add_argument("jobs_file")
        ap.add_argument("--ids", help="file of job ids to mutate (default: all)")
        ap.add_argument("-o", required=True)
        a = ap.parse_args(argv[1:])
        ids = set(open(a.ids).read().split()) if a.ids else None
        n = 0
        with open(a.o, "w") as f:
            for l in open(a.jobs_file):
                j = json.loads(l)
                if "verdict" in j or (ids is not None and j["id"] not in ids):
                    continue
                for m in mutants(j):
                    f.write(json.dumps(m) + "\n")
                    n += 1
        print(n, "mutants")
        return 0
    if argv and argv[0] == "export":
        ap = argparse.ArgumentParser(prog="judge.py export")
        ap.add_argument("rows")
        ap.add_argument("-o", required=True)
        ap.add_argument("--symbols", action="append", default=[],
                        help="PROJECT=PATH: a project's symbol file or directory (`VA name` lines), for the rows of "
                             "that project (id prefix `PROJECT_` or the row's `game`): anchors the pairing of our "
                             "symbols with the original's addresses. A bare PATH applies to every row")
        a = ap.parse_args(argv[1:])
        maps = collections.defaultdict(list)
        for x in a.symbols:
            proj, _, path = x.rpartition("=") if "=" in x and not os.path.exists(x) else ("", "", x)
            maps[proj].append(path)
        syms = {proj: load_symbols(paths) for proj, paths in maps.items()}
        with open(a.o, "w") as f:
            for l in open(a.rows):
                if not l.strip():
                    continue
                r = json.loads(l)
                proj = r.get("game") or r["id"].split("_")[0]
                m = dict(syms.get("", {}), **syms.get(proj, {}))      # one project's map never anchors another's
                f.write(json.dumps(export_row(r, m or None)) + "\n")
        return 0
    if argv and argv[0] == "run":
        ap = argparse.ArgumentParser(prog="judge.py run")
        ap.add_argument("jobs_file")
        ap.add_argument("-o", required=True)
        ap.add_argument("--jobs", type=int, default=4)
        ap.add_argument("--timeout", type=int, default=300)
        ap.add_argument("--budget", type=int, default=2000000)
        a = ap.parse_args(argv[1:])
        run_jobs(a.jobs_file, a.o, a.jobs, a.timeout, a.budget)
        return 0
    if argv and argv[0] == "hints":
        ap = argparse.ArgumentParser(prog="judge.py hints")
        ap.add_argument("results", help="judge.py run output (each line carries `hints`)")
        ap.add_argument("--out", required=True, help="directory: one <va>.json per row")
        ap.add_argument("--project", help="only rows of this project (id prefix PROJECT_)")
        a = ap.parse_args(argv[1:])
        os.makedirs(a.out, exist_ok=True)
        n = 0
        for l in open(a.results):
            r = json.loads(l)
            proj, _, hexva = r["id"].partition("_")
            if not r.get("hints") or (a.project and proj != a.project):
                continue
            h = dict(va="0x" + (hexva.lstrip("0") or "0"), verdict=r["verdict"], **r["hints"])
            json.dump(h, open(os.path.join(a.out, "%s.json" % (hexva or r["id"])), "w"), indent=1)
            n += 1
        print(n, "hint files in", a.out)
        return 0
    if argv and argv[0] == "one":
        budget = int(argv[2]) if len(argv) > 2 and argv[1] == "--budget" else 2000000
        print(json.dumps(judge_job(json.loads(sys.stdin.read()), budget)))
        return 0
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    if "--c2" in argv:                   # --c2 BUILD as on every tool (c2build takes it from sys.argv, not argv)
        k = argv.index("--c2")
        os.environ["C2_BUILD"] = argv[k + 1] if k + 1 < len(argv) else ""
        del argv[k:k + 2]
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import c2build
    if c2build.get("arch") == "x64":
        sys.exit("judge: x86-only equivalence for now; %s is x64 (no judge on x64 yet: docs/c2/port-1700.md)"
                 % c2build.name())
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("src", nargs="?")
    ap.add_argument("--func", required=True)
    ap.add_argument("--asm-vs", required=True)
    ap.add_argument("--listing")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--frame-objects", action="store_true",
                    help="experimental pass 2: only escaped listing objects' bytes are memory (not sound: section 4)")
    a = ap.parse_args(argv)
    if a.listing:
        asm = a.listing
    else:
        import tsweep
        asm = compile_listing(a.src, a.func, cl, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                                              "work", "judge", tsweep.out_name(a.src, a.func)))
    res = judge_files(asm, a.func, a.asm_vs, show_pairs=a.show, frame_objects=a.frame_objects)
    print("%s: %s%s" % (res["verdict"], res["reason"], "; %d symbol pairings assumed (no project map entry: --show)"
                                                       % res["unanchored"] if res.get("unanchored") else ""))
    if a.show and isinstance(res.get("symbols"), dict):
        for k, v in sorted(res["symbols"].items()):
            print("  %s = %s" % (k, v))
    return 0 if res["verdict"] == "EQUIVALENT" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
