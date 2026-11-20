#!/usr/bin/env python3
"""C++ from c2's IL at Lower's entry: one statement per tuple, so that compiling it gives that IL back (optimised IL is
close to a fixed point of the optimiser) and with it, ideally, the same code.  Doc: docs/c2/il2c.md.

usage: il2c.py emit <src> --func F [-o OUT.cpp] [-- <cl args>]
         one traced compile: the function's IL at Lower's entry, written as C++; the unit with the function's body
         replaced goes to OUT.cpp (default: printed)
       il2c.py test --rows ROWS.jsonl [--limit N] [--jobs 8] [--out DIR] [--start K] [--step S]
         the round trip on functions whose source is known (rows: id, src, func, c2, flags): emit, compile the
         emitted unit, compare the function's code with the original source's (tsweep's bdiff: 0 = the same
         instruction stream up to relocations); per row `ok bdiff N`, `cl-error`, or `unsupported: <construct>`

The body keeps the original signature; every IL temporary and compiler symbol is a local declared at the top with
the type of its IL type code; a variable keeps its source name. Memory operands are `*(T*)((char*)base + idx*S + D)`,
compares and branches `if (a < b) goto L;`, calls by their undecorated names (llvm-undname) with each argument cast
to the parameter's type, member calls qualified (`((C*)p)->C::m(..)`: a direct call, as in the IL).
Not yet written (the function is reported `unsupported`): indirect calls, intrinsics, struct copies, EH state,
address-taken locals of unknown type, string literals, x87 parity branches.
"""
import argparse, collections, json, os, re, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UNDNAME = next((p for p in ("/opt/homebrew/opt/llvm/bin/llvm-undname", shutil.which("llvm-undname") or "")
                if p and os.path.exists(p)), None)
CTYPE = {"1001": "signed char", "1002": "short", "1004": "int", "1008": "__int64", "2001": "unsigned char",
         "2002": "unsigned short", "2004": "unsigned int", "2008": "unsigned __int64", "4004": "float",
         "4008": "double", "5004": "float", "5008": "double", "e004": "float", "e008": "double"}   # e: SSE
BINOP = {"add": "+", "sub": "-", "and": "&", "or": "|", "xor": "^", "imul": "*", "shl": "<<", "shr": ">>",
         "sar": ">>", "OPADD": "+", "OPSUB": "-", "OPAND": "&", "OPOR": "|", "OPXOR": "^", "OPMUL": "*",
         "OPDIV": "/", "OPMOD": "%", "OPSHL": "<<", "OPSHR": ">>"}
UNOP = {"neg": "-", "not": "~", "OPNEG": "-", "OPNOT": "~"}
CC = {"e": "==", "ne": "!=", "l": "<", "le": "<=", "g": ">", "ge": ">=", "b": "<", "be": "<=", "a": ">", "ae": ">="}
UNSIGNED_CC = {"b", "be", "a", "ae"}
NEG = {"e": "ne", "ne": "e", "l": "ge", "ge": "l", "le": "g", "g": "le", "b": "ae", "ae": "b", "be": "a", "a": "be"}


class Unsupported(Exception):
    pass


# ------------------------------------------------------------------------------------------------- undecorated names

_UND = {}


def undname(names):
    """{decorated: undecorated} by llvm-undname, cached."""
    todo = [n for n in names if n not in _UND]
    if todo:
        if not UNDNAME:
            raise Unsupported("llvm-undname not found (brew install llvm)")
        out = subprocess.run([UNDNAME] + todo, capture_output=True, text=True).stdout.splitlines()
        for i in range(0, len(out) - 1, 2):
            if out[i].strip() in todo:
                _UND[out[i].strip()] = out[i + 1].strip()
        for n in todo:
            _UND.setdefault(n, None)
    return {n: _UND.get(n) for n in names}


def split_top(s, sep=","):
    out, d, cur = [], 0, ""
    for ch in s:
        d += ch in "<(["
        d -= ch in ">)]"
        if ch == sep and d == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


FUNC_RE = re.compile(r"^(?:(public|private|protected): )?(?:(static|virtual) )?(.*?)\s*\b(__cdecl|__thiscall|__stdcall|"
                     r"__fastcall)\s+(.+?)\((.*)\)(\s*const)?$")


def parse_func(u):
    """(kind, qualified name, class, params [type text], return type) of an undecorated function, or None."""
    m = FUNC_RE.match(u or "")
    if not m:
        return None
    acc, stat, ret, conv, name, params, _c = m.groups()
    ps = [] if params.strip() in ("", "void") else split_top(params)
    cls = name.rsplit("::", 1)[0] if "::" in name else None
    kind = "member" if conv == "__thiscall" and stat != "static" else "free"
    return dict(kind=kind, name=name, cls=cls, params=ps, ret=ret.strip(), conv=conv, static=stat == "static")


def parse_data(u):
    """(type, qualified name) of an undecorated variable."""
    m = re.match(r"^(?:(?:public|private|protected): )?(?:static )?(.*?)\s*\b([\w:<>~]+)$", u or "")
    return (m.group(1), m.group(2)) if m else (None, None)


# --------------------------------------------------------------------------------------------------- the IL lines

def split_ops(side):
    out, d, cur = [], 0, ""
    for ch in side:
        d += ch == "["
        d -= ch == "]"
        if ch == " " and d == 0:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return [o for o in out if o != "->"]


def parse_line(l):
    """dict(op, line, dsts, srcs, notes) of an `I` dump line; the address operands repeated after memory operands
    left out."""
    f = l.split(None, 5)
    op, ln = f[2], int(f[3]) if f[3].lstrip("-").isdigit() else 0
    rest = f[5].split("|", 1)[1] if len(f) > 5 and "|" in f[5] else ""
    rest, _, nt = rest.partition(" ; ")
    d, _, s = rest.partition("<-")
    notes = dict(re.findall(r"(?:^| )([\w]+)=(\S+)", nt))
    dsts, srcs = split_ops(d.strip()), split_ops(s.strip())
    # the dump appends the address registers of every memory operand (both sides) to the sources, once per
    # occurrence: strip exactly those from the end (`_p <- [_p+4] _p`: the destination _p stays)
    inner = collections.Counter()
    for t in dsts + srcs:
        if t.startswith("["):
            inner.update(re.findall(r"([A-Za-z_?$@][\w?$@]*):[0-9a-f]+", t.split("]")[0]))
    srcs = list(srcs)
    while srcs and not srcs[-1].startswith("["):
        n = re.match(r"(?:\w+=)?([^:\[]+)", srcs[-1])
        if re.match(r"^pas\d+", srcs[-1]):
            pas = srcs.pop()
            if srcs and not srcs[-1].startswith("["):
                continue
            srcs.append(pas)
            break
        if n and inner[n.group(1)] > 0:
            inner[n.group(1)] -= 1
            srcs.pop()
        else:
            break
    return dict(op=op, line=ln, dsts=dsts, srcs=srcs, notes=notes, text=l)


def strip_reg(t):
    return t.split("=", 1)[1] if re.match(r"^(e?[a-d]x|e?[sd]i|e?bp|[a-d][lh]|GF\d|ST\(\d\)|xmm\d)=", t) else t


# -------------------------------------------------------------------------------------------------------- emitter

class Emitter:
    def __init__(self, il, params, ret, is_member):
        self.il = [parse_line(l) for l in il if l.startswith("I ")]
        self.raw = il
        self.params = params                      # source parameter names
        self.ret = ret                            # return type text
        self.is_member = is_member
        self.decl = collections.OrderedDict()     # local name -> C type
        self.flags = {}                           # flags temp -> (a, b, type)
        self.args = {}                            # OPARG temp -> expression
        self.globals = {}
        self.prelude = []                         # declarations the body needs before the function (vtable shapes)
        self.loads = {}                           # temporary -> the token of the memory it was loaded from
        self.argsrc = {}                          # OPARG temporary -> the name it passes

    # --- operands
    def name_of(self, n):
        if n == "_this":
            return "this"
        if n.startswith("_") and n[1:] in self.params:
            return n[1:]
        if n.startswith("_"):
            return "v_" + n[1:]                   # a source local (prefixed: never a keyword or a parameter)
        return n

    def var(self, n, ty):
        """The expression of variable n (an rvalue of type ty)."""
        if n.startswith(("?", "@")):
            u = undname([n])[n]
            t, q = parse_data(u)
            if not q:
                raise Unsupported("global %s" % n)
            if n.startswith("??_C@"):
                raise Unsupported("string literal")
            return "(%s)(%s)" % (CTYPE.get(ty, "int"), q)
        nm = self.name_of(n)
        if nm in ("this",) or (n.startswith("_") and n[1:] in self.params):
            return "(%s)(%s)" % (CTYPE.get(ty, "int"), nm)
        if ty in CTYPE:
            self.decl.setdefault(nm, CTYPE[ty])
            if self.decl[nm] != CTYPE[ty]:
                return "(%s)(%s)" % (CTYPE[ty], nm)
        return nm

    def lvalue(self, tok):
        tok = strip_reg(tok)
        if tok.startswith("["):
            return self.mem(tok)
        m = re.match(r"^([^:]+):([0-9a-f]+)$", tok)
        if not m:
            raise Unsupported("operand %s" % tok)
        n, ty = m.groups()
        if ty not in CTYPE:
            raise Unsupported("type %s" % ty)
        if n.startswith(("?", "@")):
            u = undname([n])[n]
            t, q = parse_data(u)
            if not q:
                raise Unsupported("global %s" % n)
            return "*(%s*)&(%s)" % (CTYPE[ty], q)
        nm = self.name_of(n)
        if nm == "this":
            raise Unsupported("this assigned")
        if n.startswith("_") and n[1:] in self.params:
            return "*(%s*)&(%s)" % (CTYPE[ty], nm)
        self.decl.setdefault(nm, CTYPE[ty])
        return nm if self.decl[nm] == CTYPE[ty] else "*(%s*)&(%s)" % (CTYPE[ty], nm)

    def mem(self, tok, vol=False):
        tok = re.sub(r"\]@[0-9a-f]+:", "]:", tok)       # C2TRACE_ILPAS=1 dumps: the PAS index left out
        m = re.match(r"^\[(.*)\]:([0-9a-f]+)$", tok)
        if not m:
            raise Unsupported("memory %s" % tok)
        inner, ty = m.groups()
        if ty not in CTYPE:
            raise Unsupported("memory type %s" % ty)
        parts = []
        for term in re.split(r"\+(?=[^+]*?(?:[\w?$@]|-))", inner):
            term = term.strip()
            if not term:
                continue
            mm = re.match(r"^([^:*]+):([0-9a-f]+)(?:\*(\d+))?$", term)
            if mm:
                e = self.var(mm.group(1), "1004")
                parts.append(e if not mm.group(3) else "(%s)*%s" % (e, mm.group(3)))
            elif re.match(r"^-?\d+$", term):
                if term not in ("0", "-0"):
                    parts.append(term)
            else:
                raise Unsupported("address term %s" % term)
        base = parts[0] if parts else "0"
        rest = "".join(" + " + p if not p.startswith("-") else " - " + p[1:] for p in parts[1:])
        return "*(%s%s*)((char*)(%s)%s)" % ("volatile " if vol else "", CTYPE[ty], base, rest)

    def rvalue(self, tok, line, want=None):
        tok = strip_reg(tok)
        if tok.startswith("["):
            e = self.mem(tok)
            return "(%s)%s" % (CTYPE[want], e) if want and want in CTYPE else e
        m = re.match(r"^(.+):([0-9a-f]+)$", tok)
        if not m:
            raise Unsupported("operand %s" % tok)
        n, ty = m.groups()
        if n in self.args:
            return self.args[n]
        if n == "fconst":
            v = next((v for k, v in line["notes"].items() if re.match(r"^-?[\d.]+(e[-+]?\d+)?", v)), None)
            if v is None:
                raise Unsupported("float constant")
            v = re.match(r"^-?[\d.]+(e[-+]?\d+)?", v).group(0)
            return ("%sf" % v if "." in v or "e" in v else "%s.0f" % v) if ty in ("5004", "4004", "e004") else (
                v if "." in v or "e" in v else v + ".0")
        if re.match(r"^-?\d+$", n):
            t = CTYPE.get(want or ty, "int")
            return "(%s)%s" % (t, n if not n.startswith("-") else "(%s)" % n)
        if re.match(r"^s\d+$", n) and line["notes"].get(n, "").startswith("L"):
            raise Unsupported("label operand")
        if line["notes"].get("s0") == "&" and line["op"] not in ("lea",):
            if n.startswith("?") and not n.startswith(("??_7", "??_C", "??_R")):
                t, q = parse_data(undname([n])[n])         # the address of a global variable
                if q:
                    return "(int)&%s" % q
            if not (n.startswith("_") or re.match(r"^s\d+$", n)):
                raise Unsupported("address of %s" % ("a vtable" if n.startswith("??_7") else "a string literal"
                                                     if n.startswith("??_C") else n))
            if n.startswith("_") and n[1:] in self.params:
                return "(int)&%s" % n[1:]
            raise Unsupported("address-taken local")
        e = self.var(n, ty)
        if want and want != ty and want in CTYPE:
            return "(%s)(%s)" % (CTYPE[want], e)
        return e

    def ty(self, tok):
        m = re.search(r":([0-9a-f]+)$", strip_reg(tok))
        return m.group(1) if m else None

    # --- statements
    def call(self, L):
        d = [t for t in L["dsts"] if not re.match(r"^pas\d+", t)]
        srcs = [t for t in L["srcs"] if not re.match(r"^pas\d+", t)]
        if not srcs:
            raise Unsupported("call without a target")
        tgt = strip_reg(srcs[0]).rsplit(":", 1)[0]
        if tgt.startswith("[") or re.match(r"^[ts]\d+$", tgt) and not L["notes"].get(tgt, "").startswith(("?", "_")):
            return self.indirect(L, d, srcs)
        if re.match(r"^s\d+$", tgt):
            tgt = L["notes"][tgt]
        u = undname([tgt])[tgt]
        f = parse_func(u)
        if not f:
            if not tgt.startswith("?"):
                raise Unsupported("C function %s" % tgt)
            raise Unsupported("callee %s" % tgt)
        if f["name"].split("::")[-1].startswith(("~", "operator")) or f["cls"] and f["name"].split("::")[-1] == \
                f["cls"].split("::")[-1]:
            raise Unsupported("constructor / destructor / operator call")
        argv = [self.rvalue(a, L) for a in srcs[1:]]
        if f["kind"] == "member":
            if not argv:
                raise Unsupported("member call without this")
            this, argv = argv[0], argv[1:]
        if len(argv) != len(f["params"]):
            raise Unsupported("argument count")
        cast = []
        for a, p in zip(argv, f["params"]):
            if p.endswith("&"):
                cast.append("*(%s*)(%s)" % (p[:-1].strip(), a))
            elif re.match(r"^(class|struct|union) [^*]*$", p):
                raise Unsupported("class argument by value")
            else:
                cast.append("(%s)(%s)" % (p, a))
        if f["kind"] == "member":
            e = "((%s*)(%s))->%s(%s)" % (f["cls"], this, f["name"], ", ".join(cast))
        else:
            e = "%s(%s)" % (f["name"], ", ".join(cast))
        if d:
            if re.match(r"^(class|struct|union) [^*]*$", f["ret"]):
                raise Unsupported("class returned by value")
            return "%s = (%s)%s;" % (self.lvalue(d[0]), CTYPE.get(self.ty(d[0]), "int"), e)
        return e + ";"

    def ctype_of(self, tok):
        return CTYPE.get(self.ty(tok), "int")

    def indirect(self, L, d, srcs):
        """A call through memory or a register. `[vp+D]` where vp was loaded from `[x+0]` and x is the first
        argument: a virtual call, emitted through a class whose virtual function at slot D/4 has the argument and
        result types (`((il2c_vcN*)x)->sK(..)`: the front end loads the vtable pointer and calls [vp+D] itself).
        Otherwise a call through a `__cdecl` function pointer."""
        tok = strip_reg(srcs[0])
        args = srcs[1:]
        rty = self.ctype_of(d[0]) if d else "void"
        m = re.match(r"^\[([ts]\d+):[0-9a-f]+\+(\d+)\]:[0-9a-f]+$", tok)
        if m and args and m.group(1) in self.loads:
            src = self.loads[m.group(1)]
            mm = re.match(r"^\[([^+\]]+):[0-9a-f]+\+0\]:", src)
            a0 = strip_reg(args[0]).rsplit(":", 1)[0]
            a0src = self.argsrc.get(a0, a0)
            if mm and int(m.group(2)) % 4 == 0 and (mm.group(1) == a0src or mm.group(1) == a0):
                slot = int(m.group(2)) // 4
                ps = [self.ctype_of(a) for a in args[1:]]
                cls = "il2c_vc%d" % len(self.prelude)
                decl = "struct %s {\n%s    virtual %s s%d(%s);\n};\n" % (
                    cls, "".join("    virtual void s%d();\n" % k for k in range(slot)), rty, slot, ", ".join(ps))
                self.prelude.append(decl)
                argv = [self.rvalue(a, L) for a in args]
                e = "((%s*)(%s))->s%d(%s)" % (cls, argv[0], slot, ", ".join(
                    "(%s)(%s)" % (p, a) for p, a in zip(ps, argv[1:])))
                if d:
                    return "%s = (%s)%s;" % (self.lvalue(d[0]), rty, e)
                return e + ";"
        argv = [self.rvalue(a, L) for a in args]
        ps = [self.ctype_of(a) for a in args]
        fp = "((%s (__cdecl*)(%s))(%s))" % (rty, ", ".join(ps) or "void",
                                           self.mem(tok) if tok.startswith("[") else self.rvalue(srcs[0], L))
        e = "%s(%s)" % (fp, ", ".join("(%s)(%s)" % (p, a) for p, a in zip(ps, argv)))
        if d:
            return "%s = (%s)%s;" % (self.lvalue(d[0]), rty, e)
        return e + ";"

    def stmt(self, L):
        op = L["op"]
        d, s = L["dsts"], [t for t in L["srcs"] if not re.match(r"^pas\d+", t)]
        if op in ("OPARG",):
            n = strip_reg(d[0]).rsplit(":", 1)[0]
            self.args[n] = self.rvalue(s[0], L)
            self.argsrc[n] = strip_reg(s[0]).rsplit(":", 1)[0]
            return None
        if op in ("OPASSIGN", "mov") and d and s and strip_reg(s[0]).startswith("[") and \
                re.match(r"^[ts]\d+:", strip_reg(d[0])):
            self.loads[strip_reg(d[0]).rsplit(":", 1)[0]] = strip_reg(s[0])
        if op == "OPCALL":
            return self.call(L)
        if op == "OPCMP":
            ty = self.ty(s[0]) or "1004"
            self.flags[strip_reg(d[0]).rsplit(":", 1)[0]] = (s[0], s[1], ty, L)
            return None
        if op == "OPBRANCH":
            f = next((t for t in s if t.endswith((":9000", ":8000"))), None)
            lab = next((v for k, v in L["notes"].items() if v.startswith("L")), None)
            cc = L["notes"].get("cc")
            if not f or not lab or cc not in CC:
                raise Unsupported("branch (cc %s)" % cc)
            a, b, ty, LC = self.flags.pop(strip_reg(f).rsplit(":", 1)[0])
            want = ty                                  # the operands' type decides signedness; b/be/a/ae are
            if ty[0] == "1" and cc in UNSIGNED_CC:     # unsigned whatever the operands (`cc=g` on 2004: `ja`)
                want = "2" + ty[1:]
            x, y = self.rvalue(a, LC, want), self.rvalue(b, LC, want)
            cond = "%s %s %s" % (x, CC[cc], y)
            neg = "%s %s %s" % (x, CC[NEG[cc]], y) if CTYPE.get(ty, "int") not in ("float", "double") else \
                "!(%s)" % cond                       # floats: NaN makes `<` and `>=` not opposites
            return ("br", cond, neg, lab)
        if op == "OPGOTO":
            lab = next((v for k, v in L["notes"].items() if v.startswith("L")), None)
            return ("goto", lab)
        if op == "OPRET":
            if not s:
                return "return;"
            v = s[0]
            if self.ret.strip() == "bool":
                if strip_reg(v).startswith("["):
                    return "return %s;" % re.sub(r"^\*\(unsigned char\*\)", "*(bool*)", self.mem(strip_reg(v)))
                if re.match(r"^[01]:", v):
                    return "return %s;" % ("true" if v[0] == "1" else "false")
            return "return (%s)(%s);" % (self.ret, self.rvalue(v, L))
        if op in ("OPASSIGN", "mov", "movzx", "movsx", "OPPAREN", "OPCONVERT"):
            dt = self.ty(d[0])
            vol = any(re.match(r"^pas\d+", t) for t in L["dsts"] + L["srcs"])
            lhs = self.mem(strip_reg(d[0]), True) if vol and strip_reg(d[0]).startswith("[") else self.lvalue(d[0])
            rhs = self.mem(strip_reg(s[0]), True) if vol and strip_reg(s[0]).startswith("[") else self.rvalue(s[0], L)
            return "%s = (%s)(%s);" % (lhs, CTYPE.get(dt, "int"), rhs)
        if op == "lea":
            m = re.match(r"^\[(.*)\]:", strip_reg(s[0]))
            e = self.mem(strip_reg(s[0]))
            return "%s = (int)&%s;" % (self.lvalue(d[0]), e)
        if op in BINOP and d and strip_reg(d[0]).startswith("[") and s and strip_reg(s[0]) == strip_reg(d[0]) and \
                len(s) > 1:
            dt = self.ty(d[0])                         # Opeq's `[m] <- [m] op x`: the source's `m op= x`;
            vol = any(re.match(r"^pas\d+", t) for t in L["dsts"])   # a `pas` operand on it: a volatile access
            if BINOP[op] in ("<<", ">>"):
                return "%s %s= %s;" % (self.mem(strip_reg(d[0]), vol), BINOP[op], self.rvalue(s[1], L))
            return "%s %s= %s;" % (self.mem(strip_reg(d[0]), vol), BINOP[op], self.rvalue(s[1], L, dt))
        if op in BINOP:
            dt = self.ty(d[0])
            want = dt
            if op == "shr" and dt[0] == "1":
                want = "2" + dt[1:]
            if op == "sar" and dt[0] == "2":
                want = "1" + dt[1:]
            a = self.rvalue(s[0], L, want)
            b = self.rvalue(s[1], L, want if BINOP[op] not in ("<<", ">>") else None) if len(s) > 1 else None
            if b is None:
                raise Unsupported("operation %s" % op)
            return "%s = (%s)(%s %s %s);" % (self.lvalue(d[0]), CTYPE.get(dt, "int"), a, BINOP[op], b)
        if op in UNOP:
            dt = self.ty(d[0])
            return "%s = (%s)(%s%s);" % (self.lvalue(d[0]), CTYPE.get(dt, "int"), UNOP[op], self.rvalue(s[0], L, dt))
        if op in ("OPBLKSTART", "OPBLKEND", "OPNOP"):
            return None
        raise Unsupported("tuple %s" % op)

    def fold_addresses(self):
        """`lea t <- [x+D]` whose t is used only as the base of memory operands (c2's address temporaries, made
        after CSE by AddrModes / hashing): t's uses become [x+D+..] and the lea goes. Two reads through one such
        temporary would otherwise be merged when the emitted source is optimised again."""
        defs, uses = collections.Counter(), collections.Counter()
        lea = {}
        for l in self.raw:
            if not l.startswith("I "):
                continue
            L = parse_line(l)
            for t in L["dsts"]:
                m = re.match(r"^([ts]\d+):", strip_reg(t))
                if m:
                    defs[m.group(1)] += 1
            if L["op"] == "lea" and L["dsts"]:
                m = re.match(r"^([ts]\d+):[0-9a-f]+$", strip_reg(L["dsts"][0]))
                mm = re.match(r"^\[([^+\]]+):[0-9a-f]+\+(-?\d+)\]:[0-9a-f]+$", strip_reg(L["srcs"][0])) if L["srcs"] else None
                if m and mm:
                    lea[m.group(1)] = (mm.group(1), int(mm.group(2)), l)
            for t in L["dsts"][1:] + L["srcs"]:          # plain uses (the address registers repeated after a
                m = re.match(r"^([ts]\d+):", strip_reg(t))     # memory operand are not in the parsed lists)
                if m:
                    uses[m.group(1)] += 1
        fold = {t: v for t, v in lea.items() if defs[t] == 1 and uses[t] == 0}
        if not fold:
            return
        out = []
        for l in self.raw:
            if l.startswith("I ") and any(v[2] == l for v in fold.values()):
                continue
            for t, (x, dd, _l) in fold.items():
                def sub(m):
                    rest = m.group(1)
                    d0 = re.search(r"\+(-?\d+)\]$", "[" + rest + "]")
                    return "[%s:1004+%s" % (x, rest[:-len(d0.group(1))] + str(int(d0.group(1)) + dd) if d0 else rest)
                l = re.sub(r"\[%s:1004\+([^\]]*)" % t, lambda m: sub(m), l)
            out.append(l)
        self.raw = out

    def body(self):
        # self.fold_addresses(): measured worse (folding the temporaries lets the re-optimised code merge reads)
        lines, labels = [], set()
        tups = iter(self.raw)
        out = []
        for l in self.raw:
            if l.startswith("L "):
                lab = "L" + l.split()[1]
                out.append(("label", lab))
            elif l.startswith("I "):
                out.append(("stmt", parse_line(l)))
        items = []
        for kind, x in out:
            if kind == "label":
                items.append(("label", x))
            else:
                st = self.stmt(x)
                if isinstance(st, tuple):
                    items.append(st)
                elif st:
                    items.append(("stmt", st))
        body = structure(items)
        if self.flags:
            raise Unsupported("compare without branch")
        if self.ret.strip() == "bool":
            for b in body:
                m = re.match(r"return \(bool\)\((\w+)\);$", b)
                if m and self.decl.get(m.group(1)) == "unsigned char":
                    self.decl[m.group(1)] = "bool"
            body = [re.sub(r"^return \(bool\)\((\w+)\);$", r"return \1;", b) if
                    self.decl.get(re.sub(r"^return \(bool\)\((\w+)\);$", r"\1", b)) == "bool" else b for b in body]
            body = [re.sub(r"\(unsigned char\)\(\(unsigned char\)([01])\)", lambda m: "true" if m.group(1) == "1"
                           else "false", b) for b in body]
        decl = ["%s %s;" % (t, n) for n, t in self.decl.items() if n not in self.params and n != "this"]
        return "{\n" + "".join("    %s\n" % x for x in decl) + "".join("%s\n" % x for x in body) + "}"


# ---------------------------------------------------------------------------------------------------- structuring

def structure(items):
    """Statement lines from items (`stmt` text, `label` L, `br` cond neg L, `goto` L). The front end writes
    `if (c) { S }` as OPBRANCH(!c) -> Lend; S; Lend, and a loop's back edge as a branch to its head; `if (c) goto L`
    is two branches, and the flow passes lay those out otherwise. So: a forward branch over a region no outside jump
    enters is `if (neg) { region }`; that region ending in a goto over a second such region is `if .. else`; a
    backward branch to a label only it uses, over a region no outside jump enters, is `do { .. } while (cond);`.
    Anything else stays a goto."""
    refs0 = collections.Counter(it[-1] for it in items if it[0] in ("br", "goto"))
    refs = collections.Counter(refs0)
    pos = {it[1]: k for k, it in enumerate(items) if it[0] == "label"}

    def closed(lo, hi):
        """No jump from outside items[lo:hi] to a label inside it."""
        inner = collections.Counter(it[-1] for it in items[lo:hi] if it[0] in ("br", "goto"))
        return all(inner[it[1]] == refs0[it[1]] for it in items[lo:hi] if it[0] == "label")

    def emit(lo, hi, ind):
        out, k, mark = [], lo, {}
        while k < hi:
            it = items[k]
            if it[0] == "stmt":
                out.append(ind + it[1])
            elif it[0] == "goto":
                out.append(ind + "goto %s;" % it[1])
            elif it[0] == "label":
                mark[k] = len(out)
                if refs[it[1]]:
                    out.append(ind + it[1] + ":;")
            else:
                _b, cond, neg, lab = it
                j = pos.get(lab)
                if j is not None and k < j <= hi and closed(k + 1, j):
                    last = items[j - 1] if j - 1 > k else None
                    e = pos.get(last[1]) if last is not None and last[0] == "goto" else None
                    if e is not None and j < e <= hi and refs0[lab] == 1 and refs0[last[1]] == 1 and closed(j + 1, e):
                        refs[lab] -= 1
                        refs[last[1]] -= 1
                        out.append(ind + "if (%s) {" % neg)
                        out += emit(k + 1, j - 1, ind + "    ")
                        out.append(ind + "} else {")
                        out += emit(j + 1, e, ind + "    ")
                        out.append(ind + "}")
                        k = e
                        continue
                    refs[lab] -= 1
                    out.append(ind + "if (%s) {" % neg)
                    out += emit(k + 1, j, ind + "    ")
                    out.append(ind + "}")
                    k = j
                    continue
                if j is not None and lo <= j < k and j in mark and refs0[lab] == 1 and closed(j + 1, k):
                    m = mark[j]
                    body = out[m + 1:] if refs[lab] else out[m:]
                    refs[lab] -= 1
                    out = out[:m] + [ind + "do {"] + ["    " + x for x in body] + [ind + "} while (%s);" % cond]
                else:
                    out.append(ind + "if (%s) goto %s;" % (cond, lab))
            k += 1
        return out
    return emit(0, len(items), "    ")


# ---------------------------------------------------------------------------------------------------- the unit

def find_def(text, func):
    """The definition of func; among overloads, the one with the decorated name's parameter count."""
    import rwsweep, helpersweep
    fn = rwsweep.body_of(text, func)
    f = parse_func(undname([func])[func]) if func.startswith("?") else None
    if f is None:
        return fn
    want = len(f["params"])
    def count(p):
        p = p.strip()
        return 0 if p in ("", "void") else len(split_top(p))
    if count(fn[1]) == want:
        return fn
    for g in helpersweep.functions(text):
        if g[0] == fn[0] and count(g[1]) == want:
            return g
    return fn


def signature_info(text, fn, func):
    """(source parameter names, return type text, member?) of the function definition fn (helpersweep tuple)."""
    name, params, b, e, d = fn
    ps = []
    for p in split_top(params):
        p = re.sub(r"=.*$", "", p).strip()
        m = re.search(r"(\w+)\s*(\[[^\]]*\])?$", p)
        if m and p not in ("void", "...") and not re.fullmatch(r"(const\s+)?\w+(\s*[*&]+)?", p):
            ps.append(m.group(1))
    u = undname([func])[func] if func.startswith("?") else None
    f = parse_func(u) if u else None
    ret = f["ret"] if f else "int"
    for k in ("public: ", "private: ", "protected: ", "static ", "virtual "):
        ret = ret.replace(k, "")
    return ps, ret, bool(f and f["kind"] == "member")


def trace_il(src, func, cl, keep):
    import c2trace, c2il, icount
    from image import Image
    img = Image("c2")
    got = dict((n, va) for n, va, _ in icount.resolve(img, ["Lower"]))
    _i, hk, rc, out, log = c2trace.run_trace(src, ["%x%s" % (got["Lower"], c2il.SPEC)], cl, keep=keep, quiet=True,
                                             func=func)
    for _p, lines, _f in c2il.parse(log, hk, func):
        return lines
    raise Unsupported("function not seen at Lower (rc %s)" % rc)


def emit_unit(src, func, cl, keep):
    il = trace_il(src, func, cl, keep)
    text = open(src, errors="replace").read()
    fn = find_def(text, func)
    ps, ret, member = signature_info(text, fn, func)
    em = Emitter(il, ps, ret, member)
    body = em.body()
    name, params, b, e, d = fn
    start = line_start(text, d)
    pre = "".join(em.prelude)
    return text[:start] + pre + text[start:b] + body + text[e + 1:], il, pre + body


def line_start(text, d):
    """The start of the definition's first line (its return type may precede the name; a template header too)."""
    k = text.rfind("\n", 0, d) + 1
    while True:
        p = text.rfind("\n", 0, max(k - 1, 0)) + 1
        prev = text[p:k].strip()
        if k == 0 or not prev or prev.endswith((";", "}", "{")) or prev.startswith(("//", "#", "/*")):
            return k
        k = p


def listing(src, cl, keep, func):
    """The function's instructions (tsweep Instrs) from a plain traced compile with a listing."""
    import c2trace, tsweep
    os.makedirs(keep, exist_ok=True)
    _i, hk, rc, out, log = c2trace.run_trace(src, [], cl + ["/FAsc"], keep=keep, quiet=True, func="__none__")
    asm = next((os.path.join(keep, f) for f in os.listdir(keep) if f.endswith(".asm")), None)
    if not asm:
        return None, out
    return tsweep.listing_instrs(asm, func), out


def one(r, out_dir=None):
    import tsweep
    res = dict(id=r.get("id"), func=r["func"])
    tmp = tempfile.mkdtemp(prefix="il2c-")
    try:
        try:
            unit, il, body = emit_unit(r["src"], r["func"], r["flags"], os.path.join(tmp, "t"))
        except Unsupported as e:
            res["result"] = "unsupported: %s" % e
            return res
        except SystemExit as e:
            res["result"] = "unsupported: %s" % str(e)[:80]
            return res
        res["tuples"] = sum(1 for l in il if l.startswith("I "))
        # the emitted unit in a scratch directory (never next to the source: a peer repository is read-only), with
        # the source's directory on the include path for its relative includes
        em = os.path.join(tmp, os.path.basename(r["src"]))
        with open(em, "w") as f:
            f.write(unit)
        a, outa = listing(em, r["flags"] + ["/I" + os.path.dirname(os.path.abspath(r["src"]))], os.path.join(tmp, "e"),
                          r["func"])
        if out_dir:
            open(os.path.join(out_dir, "%s.cpp" % r.get("id")), "w").write(body)
        if a is None or not a:
            errs = re.findall(r"error C\d+:[^\n]*", outa or "")
            res["result"] = "cl-error"
            res["error"] = (errs[0] if errs else (outa or "")[-200:])[:200]
            return res
        b, _ = listing(r["src"], r["flags"], os.path.join(tmp, "o"), r["func"])
        if not b:
            res["result"] = "no original listing"
            return res
        res["bdiff"] = tsweep.bdiff(a, b)
        res["n_orig"], res["n_emit"] = len(b), len(a)
        res["result"] = "ok"
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    import c2trace
    if not argv or argv[0] not in ("emit", "test", "one"):
        sys.exit(__doc__)
    cmd, argv = argv[0], argv[1:]
    if cmd == "emit":
        ap = argparse.ArgumentParser(prog="il2c.py emit")
        ap.add_argument("src")
        ap.add_argument("--func", required=True)
        ap.add_argument("-o")
        argv, cl = c2trace.split_cl(argv)
        a = ap.parse_args(argv)
        tmp = tempfile.mkdtemp(prefix="il2c-")
        try:
            unit, il, body = emit_unit(a.src, a.func, cl, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if a.o:
            open(a.o, "w").write(unit)
        print(body)
        return 0
    if cmd == "one":                                  # a worker: one row (JSON) -> one result line
        r = json.loads(argv[0])
        print(json.dumps(one(r, argv[1] if len(argv) > 1 else None)))
        return 0
    ap = argparse.ArgumentParser(prog="il2c.py test")
    ap.add_argument("--rows", required=True)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    rows = [json.loads(l) for l in open(a.rows) if l.strip()][a.start::a.step][:a.limit]
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    from concurrent.futures import ThreadPoolExecutor

    def run(r):
        env = dict(os.environ, C2_BUILD=r.get("c2", os.environ.get("C2_BUILD", "")))
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "one", json.dumps(r)] + ([a.out] if a.out else []),
                           capture_output=True, text=True, env=env)
        try:
            return json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return dict(id=r.get("id"), func=r["func"], result="crash", error=(p.stderr.strip().splitlines() or
                                                                                ["?"])[-1][:200])
    tally = collections.Counter()
    bd = []
    with ThreadPoolExecutor(a.jobs) as ex:
        for res in ex.map(run, rows):
            print(json.dumps(res), flush=True)
            k = res["result"]
            tally[k if not k.startswith("unsupported") else "unsupported: " + k.split(": ", 1)[1].split()[0]] += 1
            if k == "ok":
                bd.append(res["bdiff"])
    print("\n%d rows: %s" % (len(rows), ", ".join("%s %d" % kv for kv in tally.most_common())))
    if bd:
        print("compiled: %d; byte-identical (bdiff 0): %d; bdiff <= 2: %d; median bdiff %d" % (
            len(bd), sum(1 for x in bd if x == 0), sum(1 for x in bd if x <= 2), sorted(bd)[len(bd) // 2]))
    return 0


if __name__ == "__main__":
    import c2trace
    sys.exit(c2trace.pipe_safe(main, sys.argv[1:]))
