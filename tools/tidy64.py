#!/usr/bin/env python3
"""tidy64: turn lift2il64's C++ into plain, readable C++ that compiles to the same bytes.

The lifted text is one statement per line with every operand cast. tidy64 parses each line's expressions, drops the
casts C++ would do implicitly anyway, propagates copies, folds single-use temporaries into their use, names the
variables by what they do, writes data as DAT_<address> and declares locals where they are first set. Every rewrite
is checked by compiling: tidy() returns candidate texts from the most rewritten to the least, and the caller keeps
the first that still matches under x64check at /W3 (tidycheck64.py).

    python3 tools/tidy64.py <lifted.cpp>            print the most rewritten text
"""
import re
import sys

# ------------------------------------------------------------------------------------------------ tokens and types

TOK = re.compile(r"\s*(?:(?P<num>0[xX][0-9a-fA-F]+[uUlL]*|\d+\.\d*(?:[eE][+-]?\d+)?[fF]?|\d+[uUlL]*)|"
                 r"(?P<id>[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)|"
                 r"(?P<op>->|<<=|>>=|<<|>>|<=|>=|==|!=|&&|\|\||\+\+|--|[-+*/%&|^]=|[-+*/%&|^~!<>=?:.,()\[\]{}]))")
BASE = {"unsigned", "signed", "char", "short", "int", "long", "__int64", "float", "double", "void", "bool", "const",
        "volatile", "__m128", "__m128i", "__m128d"}
RANK = {"bool": 0, "char": 1, "unsigned char": 1, "short": 2, "unsigned short": 2, "int": 4, "unsigned int": 4,
        "long": 4, "unsigned long": 4, "__int64": 8, "unsigned __int64": 8}
FLOATS = {"float", "double"}


def lex(s):
    out, i = [], 0
    s = s.rstrip()
    while i < len(s):
        m = TOK.match(s, i)
        if not m or m.end() == i:
            if s[i:].strip() == "":
                break
            raise ValueError("lex: %r" % s[i:i + 20])
        i = m.end()
        k = m.lastgroup
        out.append((k, m.group(k)))
    return out


def canon_ty(words, stars):
    w = [x for x in words if x not in ("const",)]
    t = " ".join(w)
    t = {"signed char": "char", "unsigned": "unsigned int", "long long": "__int64",
         "unsigned long long": "unsigned __int64", "signed int": "int"}.get(t, t)
    return t + "*" * stars


def integral(t):
    return t in RANK


def signed(t):
    return not t.startswith("unsigned") and t != "bool"


def promote(t):
    return "int" if integral(t) and RANK[t] < 4 else t


def usual(a, b):
    if a is None or b is None:
        return None
    if a.endswith("*") or b.endswith("*"):
        return None
    if a in FLOATS or b in FLOATS:
        return "double" if "double" in (a, b) else "float"
    if not (integral(a) and integral(b)):
        return None
    a, b = promote(a), promote(b)
    if a == "long":
        a = "int"
    if b == "long":
        b = "int"
    if a == b:
        return a
    ra, rb = RANK[a], RANK[b]
    if signed(a) == signed(b):
        return a if ra >= rb else b
    u, s = (a, b) if not signed(a) else (b, a)
    if RANK[u] >= RANK[s]:
        return u
    return s            # signed type wider: it holds every unsigned value


def lit_value(n):
    t = n.rstrip("uUlL")
    if "." in t or t.endswith(("f", "F")):
        return None
    return int(t, 16) if t.lower().startswith("0x") else int(t)


LIMITS = {"bool": (0, 1), "char": (-128, 127), "unsigned char": (0, 255), "short": (-32768, 32767),
          "unsigned short": (0, 65535), "int": (-2 ** 31, 2 ** 31 - 1), "long": (-2 ** 31, 2 ** 31 - 1),
          "unsigned int": (0, 2 ** 32 - 1), "unsigned long": (0, 2 ** 32 - 1), "__int64": (-2 ** 63, 2 ** 63 - 1),
          "unsigned __int64": (0, 2 ** 64 - 1)}


def fits(v, t):
    lo, hi = LIMITS.get(t, (None, None))
    return lo is not None and lo <= v <= hi


# ------------------------------------------------------------------------------------------------ expressions
# node: ('num', text) ('id', name) ('cast', ty, e) ('un', op, e) ('bin', op, a, b) ('tern', c, a, b)
#       ('call', f, [args]) ('idx', a, i) ('mem', a, op, name)

BIN = {"*": 13, "/": 13, "%": 13, "+": 12, "-": 12, "<<": 11, ">>": 11, "<": 10, ">": 10, "<=": 10, ">=": 10,
       "==": 9, "!=": 9, "&": 8, "^": 7, "|": 6, "&&": 5, "||": 4}
ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}


class Parser:
    def __init__(self, toks, types):
        self.t, self.i, self.types = toks, 0, types

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, None)

    def take(self, v=None):
        tk = self.peek()
        if v is not None and tk[1] != v:
            raise ValueError("expected %s got %s" % (v, tk[1]))
        self.i += 1
        return tk

    def expr(self):
        return self.assign()

    def assign(self):
        lhs = self.tern()
        if self.peek()[1] in ASSIGN:
            op = self.take()[1]
            return ("bin", op, lhs, self.assign())
        return lhs

    def tern(self):
        c = self.binary(4)
        if self.peek()[1] == "?":
            self.take()
            a = self.assign()
            self.take(":")
            b = self.assign()
            return ("tern", c, a, b)
        return c

    def binary(self, minp):
        lhs = self.unary()
        while True:
            op = self.peek()[1]
            p = BIN.get(op) if self.peek()[0] == "op" else None
            if p is None or p < minp:
                return lhs
            self.take()
            rhs = self.binary(p + 1)
            lhs = ("bin", op, lhs, rhs)

    def cast_type(self):
        """At '(': the cast's type when the parenthesis holds a type, else None (position unchanged)."""
        j, words, stars = self.i + 1, [], 0
        while j < len(self.t):
            k, v = self.t[j]
            if k == "id" and (v in BASE or v in self.types) and not stars:
                words.append(v)
            elif v == "*" and words:
                stars += 1
            elif v == ")" and words and any(w not in ("const", "volatile") for w in words):
                self.i = j + 1
                return canon_ty(words, stars)
            else:
                return None
            j += 1
        return None

    def unary(self):
        k, v = self.peek()
        if v == "(":
            ty = self.cast_type()
            if ty is not None:
                return ("cast", ty, self.unary())
        if k == "op" and v in ("-", "!", "~", "*", "&", "+"):
            self.take()
            return ("un", v, self.unary())
        return self.postfix(self.primary())

    def primary(self):
        k, v = self.take()
        if k == "num":
            return ("num", v)
        if k == "id":
            return ("id", v)
        if v == "(":
            e = self.expr()
            self.take(")")
            return e
        raise ValueError("primary: %r" % (v,))

    def postfix(self, e):
        while True:
            v = self.peek()[1]
            if v == "(":
                self.take()
                args = []
                if self.peek()[1] != ")":
                    args.append(self.assign())
                    while self.peek()[1] == ",":
                        self.take()
                        args.append(self.assign())
                self.take(")")
                e = ("call", e, args)
            elif v == "[":
                self.take()
                i = self.expr()
                self.take("]")
                e = ("idx", e, i)
            elif v in ("->", "."):
                self.take()
                e = ("mem", e, v, self.take()[1])
            else:
                return e


def parse(s, types):
    p = Parser(lex(s), types)
    e = p.expr()
    if p.i != len(p.t):
        raise ValueError("trailing %r" % (p.t[p.i:],))
    return e


PREC = {"num": 17, "id": 17, "call": 16, "idx": 16, "mem": 16, "un": 15, "cast": 15, "tern": 3}


def prec(e):
    if e[0] == "bin":
        return 2 if e[1] in ASSIGN else BIN[e[1]]
    return PREC[e[0]]


BITS = {"&", "|", "^", "<<", ">>"}
COMPOUND = [1]


def show(e, need=0):
    k = e[0]
    if k in ("num", "id"):
        s = e[1]
    elif k == "cast":
        inner = e[2]
        s = "(%s)%s" % (e[1], show(inner, 15))
    elif k == "un":
        inner = show(e[2], 15)
        if e[1] in ("-", "&", "*", "+") and inner.startswith(e[1]):
            inner = "(" + inner + ")"
        s = e[1] + inner
    elif k == "bin":
        op = e[1]
        if op in ASSIGN:
            r = e[3]
            if COMPOUND[0] and op == "=" and r[0] == "bin" and r[1] in ("+", "-", "|", "&", "^", "<<", ">>", "*") \
                    and r[2] == e[2] and e[2][0] == "id":
                if r[1] in "+-" and r[3] == ("num", "1"):
                    s = ("++" if r[1] == "+" else "--") + e[2][1]
                else:
                    s = "%s %s= %s" % (show(e[2], 3), r[1], show(r[3], 2))
            else:
                s = "%s %s %s" % (show(e[2], 3), op, show(e[3], 2))
        else:
            p = BIN[op]
            a, b = show(e[2], p), show(e[3], p + 1)
            if op in "+-" and e[3][0] == "num" and e[2][0] == "cast" and e[2][1] == "char*":
                v = lit_value(e[3][1])
                if v is not None and v >= 10:
                    b = hex(v)
            # clarity (and C4554): parenthesise a shift or bitwise operand mixed with another operator
            if e[2][0] == "bin" and e[2][1] not in ASSIGN and e[2][1] != op and (
                    (e[2][1] in BITS) != (op in BITS) or (op in BITS and e[2][1] in BITS)) and prec(e[2]) >= p:
                a = "(" + a + ")"
            if e[3][0] == "bin" and e[3][1] not in ASSIGN and e[3][1] != op and (
                    (e[3][1] in BITS) != (op in BITS) or (op in BITS and e[3][1] in BITS)) and prec(e[3]) > p:
                b = "(" + b + ")"
            s = "%s %s %s" % (a, op, b)
    elif k == "tern":
        s = "%s ? %s : %s" % (show(e[1], 4), show(e[2], 3), show(e[3], 3))
    elif k == "call":
        s = "%s(%s)" % (show(e[1], 16), ", ".join(show(a, 2) for a in e[2]))
    elif k == "idx":
        s = "%s[%s]" % (show(e[1], 16), show(e[2]))
    elif k == "mem":
        s = "%s%s%s" % (show(e[1], 16), e[2], e[3])
    else:
        raise ValueError(k)
    return "(" + s + ")" if prec(e) < need else s


def walk(e):
    yield e
    for c in kids(e):
        yield from walk(c)


def kids(e):
    k = e[0]
    if k == "cast":
        return [e[2]]
    if k == "un":
        return [e[2]]
    if k == "bin":
        return [e[2], e[3]]
    if k == "tern":
        return [e[1], e[2], e[3]]
    if k == "call":
        return [e[1]] + list(e[2])
    if k == "idx":
        return [e[1], e[2]]
    if k == "mem":
        return [e[1]]
    return []


def remap(e, f):
    """Bottom-up rebuild: f(node with rebuilt kids) -> node."""
    k = e[0]
    if k == "cast":
        e = (k, e[1], remap(e[2], f))
    elif k == "un":
        e = (k, e[1], remap(e[2], f))
    elif k == "bin":
        e = (k, e[1], remap(e[2], f), remap(e[3], f))
    elif k == "tern":
        e = (k, remap(e[1], f), remap(e[2], f), remap(e[3], f))
    elif k == "call":
        e = (k, remap(e[1], f), [remap(a, f) for a in e[2]])
    elif k == "idx":
        e = (k, remap(e[1], f), remap(e[2], f))
    elif k == "mem":
        e = (k, remap(e[1], f), e[2], e[3])
    return f(e)


def ids(e):
    return [n[1] for n in walk(e) if n[0] == "id"]


def has_side(e):
    """Reads memory, calls, or assigns."""
    for n in walk(e):
        if n[0] == "call" or (n[0] == "un" and n[1] == "*") or n[0] == "idx" or n[0] == "mem" or (
                n[0] == "bin" and n[1] in ASSIGN):
            return True
    return False


def has_call(e):
    return any(n[0] == "call" or (n[0] == "bin" and n[1] in ASSIGN) for n in walk(e))


# ------------------------------------------------------------------------------------------------ the unit


class Unit:
    """Header lines, signature, body statements."""
    DECL = re.compile(r"^((?:unsigned |signed )?(?:__int64|int|char|short|long|float|double|bool|__m128i|__m128d|__m128)) (\w+);$")

    def __init__(self, text):
        lines = text.rstrip("\n").split("\n")
        k = next(i for i, l in enumerate(lines) if re.match(r"^\S.*\bFUN_[0-9a-f]+\(.*\)$", l)
                 and not l.rstrip().endswith(";"))
        self.head = lines[:k]
        self.sig = lines[k]
        assert lines[k + 1] == "{" and lines[-1] == "}"
        body = lines[k + 2:-1]
        self.fptrs = {}              # typedef name -> (ret, args text)
        fp = re.compile(r"\(((?:unsigned )?[\w]+\**) \(\*\)\(([^()]*)\)\)")

        def fpname(m):
            key = (m.group(1), m.group(2))
            for n, kv in self.fptrs.items():
                if kv == key:
                    return "(%s)" % n
            n = "Callback" if not self.fptrs else "Callback%d" % (len(self.fptrs) + 1)
            self.fptrs[n] = key
            return "(%s)" % n
        body = [fp.sub(fpname, l) for l in body]
        self.structs = {}            # private vtable classes: name -> {slot: (ret, [args])}
        cur = None
        for l in self.head:
            m = re.match(r"^struct (\w+) \{$", l)
            if m:
                cur = self.structs.setdefault(m.group(1), {})
                continue
            m = re.match(r"^\s+virtual (.+?) (\w+)\((.*)\);$", l)
            if m and cur is not None:
                cur[m.group(2)] = m.group(1)
            if l.startswith("};"):
                cur = None
        self.typenames = set(self.structs) | set(self.fptrs)
        self.globs = {}              # g_ name -> (tls, declared type)
        self.protos = {}             # FUN_ name -> (ret, [arg types])
        for l in self.head:
            m = re.match(r"^extern (__declspec\(thread\) )?unsigned char (g_[0-9a-f]+)\[\];$", l)
            if m:
                self.globs[m.group(2)] = bool(m.group(1))
                continue
            m = re.match(r"^(.+?) (FUN_[0-9a-f]+)\((.*)\);$", l)
            if m:
                args = [a.strip() for a in m.group(3).split(",") if a.strip() and a.strip() != "void"]
                self.protos.setdefault(m.group(2), (m.group(1), args))
        m = re.match(r"^(.+?) (FUN_[0-9a-f]+)\((.*)\)$", self.sig)
        self.ret, self.name = m.group(1), m.group(2)
        self.params = []
        for p in m.group(3).split(","):
            p = p.strip()
            if p and p != "void":
                t, n = p.rsplit(" ", 1)
                self.params.append((t, n))
        self.vars = {n: t for t, n in self.params}
        self.locals = []             # declared locals in order
        self.frame = 0               # lift2il64's frame object: struct { __int64 q[N]; } lframe
        self.stmts = []              # (indent, kind, text-parts...)
        for l in body:
            ind = len(l) - len(l.lstrip())
            s = l.strip()
            m = self.DECL.match(s)
            if m and ind == 4 and not self.stmts:
                self.vars[m.group(2)] = canon_ty(m.group(1).split(), 0)
                self.locals.append(m.group(2))
                continue
            m = re.match(r"^struct \{ __int64 q\[(\d+)\]; \} lframe;$", s)
            if m and not self.stmts:
                self.frame = int(m.group(1))
                self.vars["lframe"] = "Locals"
                continue
            if s.startswith("struct {"):
                raise ValueError("frame object")
            self.stmts.append(self.parse_line(ind, s))

    def parse_line(self, ind, s):
        P = lambda x: parse(x, self.typenames)
        for kind, rx in (("if", r"^if \((.*)\) \{$"), ("elif", r"^\} else if \((.*)\) \{$"),
                         ("while", r"^while \((.*)\) \{$"), ("dowhile", r"^\} while \((.*)\);$"),
                         ("switch", r"^switch \((.*)\) \{$"), ("return", r"^return (.+);$")):
            m = re.match(rx, s)
            if m:
                return [ind, kind, P(m.group(1))]
        for kind, rx in (("else", r"^\} else \{$"), ("close", r"^\}$"), ("do", r"^do \{$"), ("ret0", r"^return;$"),
                         ("break", r"^break;$"), ("continue", r"^continue;$")):
            if re.match(rx, s):
                return [ind, kind, None]
        m = re.match(r"^(case .+|default):$", s)
        if m:
            return [ind, "case", m.group(1)]
        m = re.match(r"^(L[0-9a-f]+):;?$", s)
        if m:
            return [ind, "label", m.group(1)]
        m = re.match(r"^goto (L[0-9a-f]+);$", s)
        if m:
            return [ind, "goto", None, m.group(1)]
        m = re.match(r"^if \((.*)\) goto (L[0-9a-f]+);$", s)
        if m:
            return [ind, "ifgoto", P(m.group(1)), m.group(2)]
        if re.match(r"^goto \w+;$", s) or re.match(r"^\w+:$", s):
            raise ValueError("goto")
        if s.endswith(";"):
            return [ind, "expr", P(s[:-1])]
        raise ValueError("line: %r" % s)

    # --------------------------------------------------------------------------------------- types

    def ty(self, e):
        k = e[0]
        if k == "num":
            v = lit_value(e[1])
            if v is None:
                return "float" if e[1].endswith(("f", "F")) else "double"
            if fits(v, "int"):
                return "int"
            if e[1].lower().startswith("0x") and fits(v, "unsigned int"):
                return "unsigned int"
            return "__int64" if fits(v, "__int64") else "unsigned __int64"
        if k == "id":
            if e[1] in self.vars:
                return self.vars[e[1]]
            return None
        if k == "cast":
            return e[1]
        if k == "un":
            t = self.ty(e[2])
            if e[1] == "*":
                return t[:-1] if t and t.endswith("*") else None
            if e[1] == "&":
                return t + "*" if t else None
            if e[1] == "!":
                return "bool"
            return promote(t) if t else None
        if k == "bin":
            op = e[1]
            if op in ASSIGN:
                return self.ty(e[2])
            if op in ("==", "!=", "<", ">", "<=", ">=", "&&", "||"):
                return "bool"
            a, b = self.ty(e[2]), self.ty(e[3])
            if op in ("<<", ">>"):
                return promote(a) if a and integral(a) else None
            if a and a.endswith("*") and b and integral(b) and op in "+-":
                return a
            return usual(a, b)
        if k == "tern":
            a, b = self.ty(e[2]), self.ty(e[3])
            return a if a == b else usual(a, b)
        if k == "call":
            f = e[1]
            if f[0] == "id" and f[1] in self.protos:
                return self.protos[f[1]][0]
            if f[0] == "id" and f[1].startswith("_Interlocked"):
                return "__int64" if f[1].endswith("64") else "long"
            if f[0] == "cast" and f[1] in self.fptrs:
                return canon_ty(self.fptrs[f[1]][0].split(), 0)
            if f[0] == "mem" and f[1][0] == "cast":
                st = self.structs.get(f[1][1].rstrip("*"), {})
                r = st.get(f[3])
                return canon_ty(r.split(), 0) if r else None
            return None
        return None

    # --------------------------------------------------------------------------------------- casts

    def implicit_ok(self, x, L):
        """Converting x to L implicitly means the same as an explicit cast and draws no /W3 warning."""
        tx = self.ty(x)
        if tx is None or L is None:
            return False
        if tx == L:
            return True
        if x[0] == "num":
            v = lit_value(x[1])
            if v is None:
                return False
            return integral(L) and fits(v, L)
        if integral(tx) and integral(L):
            if RANK[tx] < RANK[L]:
                return True                        # widening
            if RANK[tx] == RANK[L]:
                return True                        # sign change, same size: no /W3 warning
            return False                           # narrowing: C4244
        if tx in FLOATS and L in FLOATS:
            return tx == "float" or L == "double"
        return False

    def fits_in(self, x, T):
        """Every value x can take is a value of T (so a cast to T changes nothing)."""
        if not integral(T):
            return False
        if x[0] == "num":
            v = lit_value(x[1])
            return v is not None and fits(v, T)
        tx = self.ty(x)
        if tx and integral(tx) and (tx == T or (RANK[tx] < RANK[T] and (signed(T) or not signed(tx))) or (
                RANK[tx] <= RANK[T] and signed(tx) == signed(T))):
            return True
        if x[0] == "cast" and integral(x[1]):
            return self.fits_in(("cast", x[1], ("num", "0")), T) if False else (
                x[1] == T or (RANK[x[1]] < RANK[T] and (signed(T) or not signed(x[1]))))
        if x[0] == "bin" and x[1] == "&" and not signed(T):
            return self.fits_in(x[2], T) or self.fits_in(x[3], T)
        return False

    def strip(self, e, want=None):
        """Simplify casts. want: the type the context converts this expression to implicitly (assignment,
        argument, return), or None."""
        k = e[0]
        if k == "cast":
            T, x = e[1], self.strip(e[2])
            if x[0] == "cast" and T.endswith("*") and x[1].endswith("*") and x[1] != "char*" or (
                    x[0] == "cast" and T.endswith("*") and x[1] == "char*"):
                x = x[2]                           # (T*)(U*)p -> (T*)p
            if x[0] == "un" and x[1] == "&" and x[2][0] == "un" and x[2][1] == "*":
                x = x[2][2]                        # &*p -> p
                return self.strip(("cast", T, x), want)
            while x[0] == "cast" and integral(x[1]) and x[2][0] == "num" and lit_value(x[2][1]) is not None and \
                    fits(lit_value(x[2][1]), x[1]):
                x = x[2]                           # (T)(U)lit -> (T)lit
            if T.endswith("*") and x[0] == "cast" and x[1] in ("__int64", "unsigned __int64") and \
                    (self.ty(x[2]) or "").endswith("*"):
                return self.strip(("cast", T, x[2]), want)   # (T*)(__int64)(U*)p -> (T*)(U*)p
            tx = self.ty(x)
            if tx == T and x[0] != "num":
                return x
            if x[0] == "num" and want and integral(T) and integral(want) and lit_value(x[1]) is not None and \
                    fits(lit_value(x[1]), T) and fits(lit_value(x[1]), want):
                return x
            if x[0] == "num" and integral(T) and lit_value(x[1]) is not None and fits(lit_value(x[1]), T) and (
                    self.ty(x) == T or want == T):
                return x
            if want == T and self.implicit_ok(x, T):
                return x
            return ("cast", T, x)
        if k == "un":
            x = self.strip(e[2])
            if e[1] == "*" and x[0] == "un" and x[1] == "&":
                return x[2]
            if e[1] == "&" and x[0] == "un" and x[1] == "*":
                return x[2]
            if e[1] == "-" and x[0] == "num" and lit_value(x[1]) == 2 ** 31:
                return ("bin", "-", ("un", "-", ("num", "2147483647")), ("num", "1"))   # INT_MIN
            if e[1] == "-" and x[0] == "num" and lit_value(x[1]) == 2 ** 63:
                return ("bin", "-", ("un", "-", ("num", "9223372036854775807")), ("num", "1"))
            if e[1] == "-" and (self.ty(x) or "").startswith("unsigned"):
                return ("bin", "-", ("num", "0"), x)      # C4146
            return ("un", e[1], x)
        if k == "bin":
            op = e[1]
            if op in ASSIGN:
                a = self.strip(e[2])
                L = self.ty(a)
                return ("bin", op, a, self.strip(e[3], L if op == "=" else None))
            a, b = self.strip(e[2]), self.strip(e[3])
            if op in ("<<", ">>"):
                if b[0] == "cast" and integral(b[1]) and self.ty(b[2]) and integral(self.ty(b[2])):
                    b = b[2]
                return ("bin", op, a, b)
            if op in ("&&", "||"):
                return ("bin", op, a, b)
            C = usual(self.ty(a), self.ty(b))
            rel = op in ("<", ">", "<=", ">=")
            for side in (0, 1):
                me, other = (a, b) if side == 0 else (b, a)
                if me[0] != "cast" or C is None or me[1] != C:
                    continue
                x = me[2]
                tx, to = self.ty(x), self.ty(other)
                if tx is None or usual(tx, to) != C:
                    continue
                if not self.implicit_ok(x, C):
                    continue
                if rel and x[0] != "num" and integral(tx) and integral(to) and signed(tx) != signed(to) and \
                        other[0] != "num":
                    continue                       # C4018
                if side == 0:
                    a = x
                else:
                    b = x
            for side in (0, 1):
                me, other = (a, b) if side == 0 else (b, a)
                if me[0] == "cast" and me[2][0] == "num" and C is not None and integral(C) and \
                        lit_value(me[2][1]) is not None and fits(lit_value(me[2][1]), C) and \
                        fits(lit_value(me[2][1]), me[1]) and usual(self.ty(me[2]), self.ty(other)) == C:
                    if side == 0:
                        a = me[2]
                    else:
                        b = me[2]
            for side in (0, 1):
                me = a if side == 0 else b
                if me[0] == "cast" and integral(me[1]) and C is not None and promote(me[1]) == C and \
                        self.fits_in(me[2], me[1]) and self.ty(me[2]) and integral(self.ty(me[2])) and \
                        usual(self.ty(me[2]), self.ty(b if side == 0 else a)) == C:
                    if side == 0:
                        a = me[2]
                    else:
                        b = me[2]
            return ("bin", op, a, b)
        if k == "tern":
            return ("tern", self.strip(e[1]), self.strip(e[2], want), self.strip(e[3], want))
        if k == "call":
            f = self.strip(e[1]) if e[1][0] != "id" else e[1]
            pts = None
            if e[1][0] == "id" and e[1][1] in self.protos:
                pts = [canon_ty(t.split(), t.count("*")) if "*" not in t else t.replace(" *", "*")
                       for t in self.protos[e[1][1]][1]]
            args = []
            for i, a in enumerate(e[2]):
                w = canon_ty(pts[i].replace("*", "").split(), pts[i].count("*")) if pts and i < len(pts) else None
                args.append(self.strip(a, w))
            if e[1][0] == "id" and e[1][1].startswith("_Interlocked"):
                args = [self.nonvolatile(args[0])] + args[1:]
            return ("call", f, args)
        if k == "idx":
            return ("idx", self.strip(e[1]), self.strip(e[2]))
        if k == "mem":
            return ("mem", self.strip(e[1]), e[2], e[3])
        return e

    def frame_addr(self, e):
        """Byte offset into lframe when e is &lframe (+ k) through integer or char* casts, else None."""
        while e[0] == "cast" and (e[1] in ("__int64", "unsigned __int64", "char*") or e[1].endswith("*") and
                                  e[2][0] == "un"):
            e = e[2]
        if e == ("un", "&", ("id", "lframe")):
            return 0
        if e[0] == "bin" and e[1] == "+" and e[3][0] == "num" and lit_value(e[3][1]) is not None:
            a = e[2]
            ta = a[1] if a[0] == "cast" else self.ty(a)
            if ta in ("__int64", "unsigned __int64", "char*"):
                k = self.frame_addr(a)
                if k is not None:
                    return k + lit_value(e[3][1])
        return None

    def do_frame(self):
        """lframe as a named struct: each offset the code reads or writes becomes a field."""
        if not self.frame:
            return
        fields = {}

        def scan(e):
            if e[0] == "un" and e[1] == "*" and e[2][0] == "cast" and e[2][1].endswith("*"):
                k = self.frame_addr(e[2][2])
                if k is not None:
                    t = e[2][1][:-1]
                    if fields.get(k, t) != t:
                        raise ValueError("frame field typed twice")
                    fields[k] = t
                    return
            for c in kids(e):
                scan(c)
        for s in self.stmts:
            for e in self.exprs(s):
                scan(e)
        size = {"__int64": 8, "unsigned __int64": 8, "int": 4, "unsigned int": 4, "float": 4, "double": 8,
                "short": 2, "unsigned short": 2, "char": 1, "unsigned char": 1}
        pos, layout = 0, []
        for k in sorted(fields):
            t = fields[k]
            if t not in size or k < pos or k % size[t]:
                raise ValueError("frame layout")
            while pos < k:
                step = 8 if pos % 8 == 0 and k - pos >= 8 else 4 if pos % 4 == 0 and k - pos >= 4 else 1
                layout.append((pos, {8: "__int64", 4: "int", 1: "char"}[step]))
                pos += step
            layout.append((k, t))
            pos = k + size[t]
        while pos < self.frame * 8:
            step = 8 if pos % 8 == 0 else 4 if pos % 4 == 0 else 1
            layout.append((pos, {8: "__int64", 4: "int", 1: "char"}[step]))
            pos += step
        # offsets the code names (read, written or address-taken) are fields; runs of the rest are padding arrays
        used = set(fields)

        def scan_addr(e):
            k = self.frame_addr(e) if e[0] in ("cast", "bin", "un") else None
            if k is not None:
                used.add(k)
                return
            for c in kids(e):
                scan_addr(c)
        for st in self.stmts:
            for e in self.exprs(st):
                scan_addr(e)
        groups = []                                   # (offset, type, count, is padding)
        for off, t in layout:
            pad = off not in used
            if pad and groups and groups[-1][3] and groups[-1][1] == t:
                g = groups[-1]
                groups[-1] = (g[0], t, g[2] + 1, True)
            else:
                groups.append((off, t, 1, pad))
        ords = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
                "eleventh", "twelfth"]
        names, self.frame_layout = {}, []
        nf = npad = 0
        for off, t, cnt, pad in groups:
            if pad:
                npad += 1
                nm = "unused%d" % npad if sum(1 for g in groups if g[3]) > 1 else "unused"
                self.frame_layout.append((t, nm + ("[%d]" % cnt if cnt > 1 else "")))
                names[off] = nm
                continue
            nm = ords[nf] if nf < len(ords) else "item%d" % (nf + 1)
            nf += 1
            names[off] = nm
            self.frame_layout.append((t, nm))

        def rw(e):
            if e[0] == "un" and e[1] == "*" and e[2][0] == "cast" and e[2][1].endswith("*"):
                k = self.frame_addr(e[2][2])
                if k is not None and k in fields:
                    return ("mem", ("id", "locals"), ".", names[k])
            k = self.frame_addr(e) if e[0] in ("cast", "bin", "un") else None
            if k is not None:
                if k not in names:
                    raise ValueError("frame address between fields")
                tgt = ("un", "&", ("id", "locals")) if k == 0 else ("un", "&", ("mem", ("id", "locals"), ".",
                                                                                names[k]))
                return ("cast", "__int64", tgt)
            if e[0] in ("cast", "un"):
                return (e[0], e[1], rw(e[2]))
            if e[0] == "bin":
                return (e[0], e[1], rw(e[2]), rw(e[3]))
            if e[0] == "tern":
                return (e[0], rw(e[1]), rw(e[2]), rw(e[3]))
            if e[0] == "call":
                return (e[0], rw(e[1]), [rw(a) for a in e[2]])
            if e[0] == "idx":
                return (e[0], rw(e[1]), rw(e[2]))
            if e[0] == "mem":
                return (e[0], rw(e[1]), e[2], e[3])
            if e == ("id", "lframe"):
                raise ValueError("frame used whole")
            return e
        for s in self.stmts:
            if s[1] in ("if", "elif", "while", "dowhile", "switch", "return", "expr", "ifgoto"):
                s[2] = rw(s[2])
        self.vars["locals"] = "Locals"
        self.vars.pop("lframe", None)

    def nonvolatile(self, a):
        """_Interlocked*((long volatile*)p): long* converts implicitly; a volatile in a function body is Q1."""
        if a[0] == "cast" and "volatile" in a[1]:
            return ("cast", a[1].replace("volatile", "").replace("  ", " ").replace(" *", "*").strip(), a[2])
        return a

    # --------------------------------------------------------------------------------------- statements

    def exprs(self, s):
        return [s[2]] if s[1] in ("if", "elif", "while", "dowhile", "switch", "return", "expr", "ifgoto") else []

    def strip_all(self):
        for s in self.stmts:
            if s[1] == "return":
                s[2] = self.strip(s[2], self.ret)
            elif s[1] in ("if", "elif", "while", "dowhile", "switch", "expr", "ifgoto"):
                s[2] = self.strip(s[2])

    def defs_uses(self):
        defs, uses, addr = {}, {}, set()
        for i, s in enumerate(self.stmts):
            for e in self.exprs(s):
                lhs = None
                if s[1] == "expr" and e[0] == "bin" and e[1] in ASSIGN and e[2][0] == "id":
                    lhs = e[2][1]
                    defs.setdefault(lhs, []).append(i)
                    if e[1] != "=":
                        uses.setdefault(lhs, []).append(i)
                    walkon = [e[3]]
                else:
                    walkon = [e]
                for w in walkon:
                    for n in walk(w):
                        if n[0] == "id":
                            uses.setdefault(n[1], []).append(i)
                        if n[0] == "un" and n[1] == "&" and n[2][0] == "id":
                            addr.add(n[2][1])
        return defs, uses, addr

    def stable(self, e, defs):
        """Pure and invariant: literals, data addresses, parameters never assigned, arithmetic on those."""
        for n in walk(e):
            if n[0] == "id":
                v = n[1]
                if v in self.vars and (v in [p for _, p in self.params]) and not defs.get(v):
                    continue
                if v in self.globs or v.startswith("DAT_"):
                    continue
                return False
            if n[0] in ("call", "idx", "mem") or (n[0] == "un" and n[1] == "*") or (
                    n[0] == "bin" and n[1] in ASSIGN):
                return False
        return True

    def atom(self, r):
        while r[0] == "cast":
            r = r[2]
        return r[0] in ("id", "num")

    def subst(self, i, v, r):
        s = self.stmts[i]

        def f(n):
            return r if n == ("id", v) else n
        if s[1] == "expr" and s[2][0] == "bin" and s[2][1] in ASSIGN and s[2][2][0] == "id":
            e = s[2]
            lhs = e[2] if e[2] != ("id", v) else e[2]
            s[2] = ("bin", e[1], lhs, remap(e[3], f))
        elif s[1] in ("if", "elif", "while", "dowhile", "switch", "return", "expr", "ifgoto"):
            s[2] = remap(s[2], f)

    def local(self, v):
        return v in self.locals

    def copyprop(self, allow=None, done=None):
        """allow: extra locals whose single copy of another single-def local may be propagated (checked one at a
        time by the caller); done: collects the names of those propagated."""
        allow = allow or set()
        changed = True
        while changed:
            changed = False
            defs, uses, addr = self.defs_uses()
            for v in list(self.locals):
                d = defs.get(v, [])
                if len(d) != 1 or v in addr or v in self.block:
                    continue
                e = self.stmts[d[0]][2]
                if e[1] != "=":
                    continue
                r = e[3]
                ok = self.stable(r, defs) and self.atom(r)
                rr = r
                while rr[0] == "cast":
                    rr = rr[2]
                cand = not ok and rr[0] == "id" and rr[1] in self.locals and len(defs.get(rr[1], [])) == 1 and \
                    rr[1] not in addr
                if cand and done is not None and v not in allow:
                    self.cands.add(("copy", v))
                if not ok and not (cand and v in allow):
                    continue
                if cand and done is not None:
                    done.add(v)
                self.applied.add(v)
                if self.ty(r) != self.vars[v] and not (r[0] == "num" or self.implicit_ok(r, self.vars[v])):
                    r = ("cast", self.vars[v], r)
                elif self.ty(r) != self.vars[v]:
                    r = ("cast", self.vars[v], r) if r[0] != "num" else r
                for i in sorted(set(uses.get(v, []))):
                    self.subst(i, v, r)
                del self.stmts[d[0]]
                changed = True
                break
            if not changed:
                changed = self.dead()

    def dead(self):
        defs, uses, addr = self.defs_uses()
        for v in self.locals:
            if uses.get(v) or v in addr:
                continue
            for i in defs.get(v, []):
                e = self.stmts[i][2]
                if has_call(e[3]) or has_side(e[3]):
                    self.stmts[i][2] = e[3]
                else:
                    del self.stmts[i]
                return True
        return False

    def nextstmt(self, i):
        """The statement that runs right after i at the same level, if it is the next line."""
        if i + 1 >= len(self.stmts):
            return None
        a, b = self.stmts[i], self.stmts[i + 1]
        if b[1] in ("expr", "return", "if", "switch", "ifgoto") and b[0] == a[0]:
            return i + 1
        if b[1] == "dowhile" and b[0] == a[0] - 4:
            return i + 1
        return None

    def inline(self, allow=None, done=None):
        """allow None: every eligible temporary; else only those named (the caller adds them one at a time)."""
        changed = True
        while changed:
            changed = False
            defs, uses, addr = self.defs_uses()
            for v in list(self.locals):
                d, u = defs.get(v, []), uses.get(v, [])
                if len(d) != 1 or len(u) != 1 or v in addr or v in self.block:
                    continue
                i = d[0]
                e = self.stmts[i][2]
                if e[1] != "=":
                    continue
                j = self.nextstmt(i)
                if j is None or u[0] != j:
                    continue
                if allow is not None and v not in allow:
                    self.cands.add(("inline", v))
                    continue
                if done is not None:
                    done.add(v)
                if sum(1 for n in walk(self.stmts[j][2]) if n == ("id", v)) != 1:
                    continue
                # the use's own target must not be read by the inlined expression
                tgt = self.stmts[j][2]
                if self.stmts[j][1] == "expr" and tgt[0] == "bin" and tgt[1] in ASSIGN and tgt[2][0] == "id" \
                        and tgt[2][1] in ids(e[3]) and tgt[2][1] != v:
                    pass
                r = e[3]
                if CALLLOADS[0] == 0 and any(n[0] == "call" for n in walk(self.stmts[j][2])) and \
                        any(n[0] == "un" and n[1] == "*" or n[0] in ("idx", "mem") for n in walk(r)):
                    # a load kept as its own statement before a call: cl loads the arguments in statement order
                    # (folded into the argument list, it picks its own order)
                    continue
                self.applied.add(v)
                if self.ty(r) != self.vars[v] and not self.implicit_ok(r, self.vars[v]):
                    r = ("cast", self.vars[v], r)
                elif self.ty(r) != self.vars[v] and r[0] != "num":
                    r = ("cast", self.vars[v], r)
                self.subst(j, v, r)
                del self.stmts[i]
                changed = True
                break

    # --------------------------------------------------------------------------------------- naming

    def ptrish(self, v, seen=None):
        seen = seen or set()
        seen.add(v)
        for s in self.stmts:
            e = s[2] if s[1] == "expr" else None
            if e and e[0] == "bin" and e[1] == "=" and e[2][0] == "id" and e[2][1] not in seen:
                r = e[3]
                if r[0] == "bin" and r[1] in "+-" and r[3][0] == "num":
                    r = r[2]
                if r == ("id", v) and self.ptrish(e[2][1], seen):
                    return True
        for s in self.stmts:
            for e in self.exprs(s):
                for n in walk(e):
                    if n[0] == "cast" and n[1].endswith("*"):
                        b = n[2]
                        while b[0] == "bin" and b[1] in ("+", "-"):
                            b = b[2]
                        if b == ("id", v):
                            return True
                    if n[0] == "bin" and n[1] in "+-" and n[2] == ("id", v) and n[3][0] == "num" and \
                            self.vars.get(v) in ("__int64", "unsigned __int64"):
                        pass
        return False

    def role(self, v):
        t = self.vars[v]
        stepped = counted = returned = False
        for s in self.stmts:
            for e in self.exprs(s):
                if e[0] == "bin" and e[1] == "=" and e[2] == ("id", v) and e[3][0] == "bin" and e[3][2] == (
                        "id", v) and e[3][3][0] == "num":
                    if e[3][1] == "-" and e[3][3][1] == "1":
                        counted = True
                    elif e[3][1] in "+-":
                        stepped = True
            if s[1] == "return" and s[2] == ("id", v):
                returned = True
        if returned:
            return "result"
        if counted:
            return "count"
        if stepped and self.ptrish(v):
            return "cursor"
        if self.ptrish(v):
            return "ptr"
        if t in FLOATS:
            return "f"
        if t in ("unsigned char", "bool"):
            return "flag"
        if stepped:
            return "index"
        return "value"

    def rename(self):
        names, used = {}, set()

        def pick(base):
            n, k = base, 2
            while n in used:
                n, k = "%s%d" % (base, k), k + 1
            used.add(n)
            return n
        pnames = {"ptr": "obj", "flag": "flag", "f": "x", "value": "value"}
        defs, uses, addr = self.defs_uses()
        for t, p in self.params:
            if not uses.get(p) and not defs.get(p):
                names[p] = pick("unused")
                continue
            r = "ptr" if self.ptrish(p) else ("f" if t in FLOATS else ("flag" if t == "unsigned char" else "value"))
            names[p] = pick(pnames[r])
        for v in self.locals:
            if v in uses or v in defs:
                names[v] = pick(self.role(v))
        self.names = names

        def f(n):
            if n[0] == "id" and n[1] in names:
                return ("id", names[n[1]])
            if n[0] == "id" and n[1].startswith("g_") and n[1] in self.globs:
                return ("id", "DAT_" + n[1][2:])
            if n[0] == "cast" and n[1].rstrip("*") in self.snames:
                return ("cast", self.snames[n[1].rstrip("*")] + n[1][len(n[1].rstrip("*")):], n[2])
            if n[0] == "mem" and re.match(r"^s\d+$", n[3]):
                return ("mem", n[1], n[2], "Unk_%02X" % int(n[3][1:]))
            return n
        self.lnames = {}
        cnt = {}
        for i, s in enumerate(self.stmts):
            if s[1] == "label":
                back = any(t[1] in ("goto", "ifgoto") and t[3] == s[2] and j > i for j, t in enumerate(self.stmts))
                base = "again" if back else "done"
                cnt[base] = cnt.get(base, 0) + 1
                self.lnames[s[2]] = base if cnt[base] == 1 else "%s%d" % (base, cnt[base])
        self.snames = {}
        for k, s in enumerate(sorted(self.structs)):
            self.snames[s] = "VObject" if k == 0 else "VObject%d" % (k + 1)
        for s in self.stmts:
            if s[2] is not None and not isinstance(s[2], str):
                s[2] = remap(s[2], f)
        self.vars = {names.get(v, v): t for v, t in self.vars.items()}
        self.params = [(t, names.get(p, p)) for t, p in self.params]
        self.locals = [names[v] for v in self.locals if v in names]

    # --------------------------------------------------------------------------------------- globals

    def type_globals(self):
        """DAT_x used only as *(T*)DAT_x with one T: declare it extern T and use it by name."""
        uses = {}
        for s in self.stmts:
            for e in self.exprs(s):
                for n in walk(e):
                    if n[0] == "un" and n[1] == "*" and n[2][0] == "cast" and n[2][2][0] == "id" and \
                            n[2][2][1].startswith("DAT_"):
                        uses.setdefault(n[2][2][1], set()).add(n[2][1][:-1])
                for n in walk(e):
                    if n[0] == "id" and n[1].startswith("DAT_"):
                        uses.setdefault(n[1], set()).add("@")
        loads = {}
        for s in self.stmts:
            for e in self.exprs(s):
                for n in walk(e):
                    if n[0] == "un" and n[1] == "*" and n[2][0] == "cast" and n[2][2][0] == "id":
                        loads[n[2][2][1]] = loads.get(n[2][2][1], 0) + 1
        typed = {}
        for g, ts in uses.items():
            tys = ts - {"@"}
            refs = sum(1 for s in self.stmts for e in self.exprs(s) for n in walk(e) if n == ("id", g))
            if len(tys) == 1 and refs == loads.get(g, 0) and not next(iter(tys)).endswith("*") is None:
                typed[g] = next(iter(tys))

        def f(n):
            if n[0] == "un" and n[1] == "*" and n[2][0] == "cast" and n[2][2][0] == "id" and n[2][2][1] in typed:
                return n[2][2]
            return n
        for s in self.stmts:
            if s[2] is not None and not isinstance(s[2], str):
                s[2] = remap(s[2], f)
        for g, t in typed.items():
            self.vars[g] = t
        self.typed = typed

    # --------------------------------------------------------------------------------------- output

    def declinit(self):
        """Declare a local where it is first set, when that is at the top level of the body and nothing reads it
        before."""
        self.initd = set()
        seen = set()
        if any(s[1] in ("label", "goto", "ifgoto") for s in self.stmts):
            return                                  # a jump past an initialisation does not compile
        for i, s in enumerate(self.stmts):
            if s[1] == "expr" and s[0] == 4 and s[2][0] == "bin" and s[2][1] == "=" and s[2][2][0] == "id":
                v = s[2][2][1]
                if v in self.locals and v not in seen and v not in self.initd and v not in ids(s[2][3]):
                    self.initd.add(v)
                    s.append("decl")
            for e in self.exprs(s):
                seen.update(ids(e))

    def text(self):
        out = []
        sk = None
        for l in self.head:
            m = re.match(r"^extern (__declspec\(thread\) )?unsigned char (g_[0-9a-f]+)\[\];$", l)
            if m:
                g = "DAT_" + m.group(2)[2:]
                t = getattr(self, "typed", {}).get(g)
                out.append("extern %s%s %s%s;" % (m.group(1) or "", t or "unsigned char", g, "" if t else "[]"))
                continue
            m = re.match(r"^struct (\w+) \{$", l)
            if m:
                out.append("struct %s {" % self.snames.get(m.group(1), m.group(1)))
                continue
            m = re.match(r"^(\s+virtual .+? )s(\d+)(\(.*\);)$", l)
            if m:
                out.append("%sUnk_%02X%s" % (m.group(1), int(m.group(2)), m.group(3)))
                continue
            out.append(l)
        if self.fptrs:
            k = max(i for i, l in enumerate(out + [""]) if i == 0 or (i <= len(out) and out[i - 1].startswith(
                ("#include", "};")))) if any(l.startswith(("#include", "};")) for l in out) else 0
            for n, (r, a) in self.fptrs.items():
                out.insert(k, "typedef %s (*%s)(%s);" % (r, n, a))
                k += 1
        ps = ", ".join("%s %s" % (t, p) for t, p in self.params) or "void"
        out.append("%s %s(%s)" % (self.ret, self.name, ps))
        out.append("{")
        initd = getattr(self, "initd", set())
        used = set()
        for s in self.stmts:
            for e in self.exprs(s):
                used.update(ids(e))
        if getattr(self, "frame_layout", None):
            out.append("    struct Locals {")
            for t, nm in self.frame_layout:
                out.append("        %s %s;" % (t, nm))
            out.append("    } locals;")
        for v in self.locals:
            if v in used and v not in initd:
                out.append("    %s %s;" % (self.vars[v], v))
        for s in self.stmts:
            ind, k, e = s[0], s[1], s[2]
            pad = " " * ind
            if k == "if":
                out.append(pad + "if (%s) {" % show(e))
            elif k == "elif":
                out.append(pad + "} else if (%s) {" % show(e))
            elif k == "while":
                out.append(pad + "while (%s) {" % show(e))
            elif k == "dowhile":
                out.append(pad + "} while (%s);" % show(e))
            elif k == "switch":
                out.append(pad + "switch (%s) {" % show(e))
            elif k == "return":
                out.append(pad + "return %s;" % show(e))
            elif k == "else":
                out.append(pad + "} else {")
            elif k == "close":
                out.append(pad + "}")
            elif k == "do":
                out.append(pad + "do {")
            elif k == "ret0":
                out.append(pad + "return;")
            elif k in ("break", "continue"):
                out.append(pad + k + ";")
            elif k == "case":
                out.append(pad + e + ":")
            elif k == "label":
                out.append(pad + self.lnames.get(e, e) + ":")
            elif k == "goto":
                out.append(pad + "goto %s;" % self.lnames.get(s[3], s[3]))
            elif k == "ifgoto":
                out.append(pad + "if (%s) goto %s;" % (show(e), self.lnames.get(s[3], s[3])))
            elif k == "expr":
                if s[-1] == "decl":
                    out.append(pad + "%s %s;" % (self.vars[e[2][1]], show(e)))
                else:
                    out.append(pad + show(e) + ";")
        # a void function's trailing return is noise
        if self.ret == "void" and out[-1] == "    return;" and not out[-2].rstrip().endswith(":"):
            out.pop()
        out.append("}")
        for i in range(len(out) - 1):              # a label needs a statement after it
            if re.match(r"^\s+\w+:$", out[i]) and not out[i].strip().startswith(("case", "default")) and \
                    out[i + 1].strip().startswith("}"):
                out[i] += ";"
        t = "\n".join(out) + "\n"
        return re.sub(r"\n{3,}", "\n\n", t)


CALLLOADS = [1]
LADDER = [dict(copy=1, inline=1, decl=1, typed=1, comp=1), dict(copy=1, inline=1, decl=1, typed=1, comp=1, callloads=0),
          dict(copy=1, inline=1, decl=1, typed=1),
          dict(copy=1, inline=1, decl=0, typed=1),
          dict(copy=1, inline=1, decl=1, typed=0), dict(copy=1, inline=0, decl=1, typed=0),
          dict(copy=0, inline=0, decl=0, typed=0)]


EH_TAIL = {"try": ["    } catch (...) {", "        throw;", "    }"], "swallow": ["    } catch (...) {", "    }"]}


def eh_unwrap(text):
    """lift2il64's EH body (try { ... } catch (...) { throw; } or { }) -> (plain text, mode) or (text, None)."""
    L = text.split("\n")
    if "    try {" not in L:
        return text, None
    i = L.index("    try {")
    for mode, tail in EH_TAIL.items():
        for j in range(i + 1, len(L)):
            if L[j:j + len(tail)] == tail:
                rest = L[j + len(tail):]
                if rest[:1] == ["    return 0;"]:
                    rest = rest[1:]
                if rest[:1] != ["}"]:
                    break
                inner = [l[4:] if l.startswith("    ") else l for l in L[i + 1:j]]
                return "\n".join(L[:i] + inner + rest), mode
    raise ValueError("try block")


def eh_rewrap(text, mode):
    """The tidied text with its body back inside the try block."""
    L = text.split("\n")
    k = L.index("{") + 1
    void = L[k - 2].startswith("void ")
    while k < len(L):
        if L[k].startswith("    struct ") and L[k].endswith("{"):
            while not re.match(r"^    \} \w+;$", L[k]):
                k += 1
            k += 1
        elif re.match(r"^    [\w ]+\*? \w+(\[\d+\])?;$", L[k]):
            k += 1
        else:
            break
    end = max(n for n, l in enumerate(L) if l == "}")
    inner = ["    " + l if l.strip() else l for l in L[k:end]]
    tail = EH_TAIL[mode] + (["    return 0;"] if mode == "swallow" and not void else [])
    return "\n".join(L[:k] + ["    try {"] + inner + tail + L[end:])


def tidy_one(text, o, applied=None):
    plain, mode = eh_unwrap(text)
    t = _tidy_one(plain, o, applied)
    return eh_rewrap(t, mode) if mode else t


def _tidy_one(text, o, applied=None):
    """o: a LADDER rung, optionally copy_allow / inline_allow (greedy, adding folds) or block (variables never
    folded away: the reverse search). applied: collects the variables the folds removed."""
    COMPOUND[0] = o.get("comp", 0)
    CALLLOADS[0] = o.get("callloads", 1)
    u = Unit(text)
    u.block = set(o.get("block") or ())
    u.applied = applied if applied is not None else set()
    u.strip_all()
    u.do_frame()
    u.strip_all()
    u.cands = set()
    if o["copy"]:
        u.copyprop(o.get("copy_allow"), set() if "copy_allow" in o else None)
        u.strip_all()
    if o["inline"]:
        u.inline(o.get("inline_allow"))
        u.strip_all()
    u.dead()
    u.rename()
    if o["typed"]:
        u.type_globals()
        u.strip_all()
    if o["decl"]:
        u.declinit()
    return u.text()


def candidates(text, o):
    """The moves (('inline'|'copy', var)) that o leaves out but could still make."""
    u = Unit(text)
    u.block, u.applied = set(o.get("block") or ()), set()
    u.strip_all()
    u.do_frame()
    u.strip_all()
    u.cands = set()
    if o["copy"]:
        u.copyprop(o.get("copy_allow"), set())
        u.strip_all()
    u.inline(o.get("inline_allow", set()))
    return sorted(u.cands)


def tidy(text):
    """[(opts, text)] most rewritten first, each text once. Raises ValueError for shapes tidy64 leaves alone
    (frame objects, gotos)."""
    out, seen = [], set()
    err = None
    for o in LADDER:
        try:
            t = tidy_one(text, o)
        except (ValueError, KeyError, IndexError, AssertionError, StopIteration) as e:
            err = e
            continue
        if t not in seen:
            seen.add(t)
            out.append((o, t))
    if not out:
        raise ValueError(str(err))
    return out


if __name__ == "__main__":
    print(tidy(open(sys.argv[1]).read())[0][1], end="")
