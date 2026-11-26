"""il2c_lift: C++ from LIFTED IL (lift2il.py), with no source to read a signature or names from (T31, milestone 2).

usage: il2c_lift.py unit CORPUS.jsonl VA       print the C++ unit for one function (lift2il -> il2c's emitter)

il2c.Emitter writes the body; this module supplies what il2c otherwise reads from the original source:
  - names: incoming arguments `_aN` -> parameters aN, `_this` -> self, register webs `_vN` -> locals;
  - globals by address: `?g_<va>@@3HA` -> `extern "C" unsigned char g_<va>[]`, read and written as `*(T*)g_<va>`;
  - callees by address: `?FUN_<va>@@YAXXZ` -> `extern "C" int __cdecl FUN_<va>(...)`; a call whose `this` was set
    in ecx (lift2il's `thiscall=1` note) -> a stand-in struct member `((C_<va>*)(this))->FUN_<va>(args)`, one fixed-arity
    member per argument count (w8_emit's convention, so the callee pops its arguments);
  - the signature from the bytes: `ret N` and the argument slots read give the parameter count; `this` read from ecx
    makes it `__fastcall(char* self, int edx_unused, ...)` (thiscall's code under a free function's name), else
    `__stdcall` with `ret N`, else `__cdecl`; int when some ret returns eax, else void.
il2c.py itself is not changed.
"""
import collections, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import il2c
import lift2il
from il2c import CTYPE, Unsupported, strip_reg


class LiftEmitter(il2c.Emitter):
    def __init__(self, il, params, ret, glob, calls):
        super().__init__(il, params, ret, False)
        self.gl = glob                       # set of global VAs referenced
        self.calls = calls                   # callee va -> {"arities": set, "this": bool, "ret": bool}
        self.needs_string = False            # a memcpy: <string.h>
        self.needs_math = False              # sqrt: <math.h>
        self.intrin = set()                  # compiler intrinsics used (declared and #pragma intrinsic)
        self.lang_c = False                  # a /TC unit: no extern "C", no member stand-ins
        self.argty = {}                      # OPARG temporary -> the type code of the value it carries
        self.copyof = {}                     # temporary -> the temporary it copies (`mov ecx, esi`)
        self.tls = set()                     # __declspec(thread) offsets read or written
        self.ptypes = {}                     # parameter name -> its C type (unit() fills it)

    def name_of(self, n):
        if n == "_this":
            return "self"
        if n.startswith("_a") and n[1:] in self.params:
            return n[1:]
        return super().name_of(n)

    def _gva(self, n):
        m = re.match(r"^\?g_([0-9a-f]{8})@@3HA$", n)
        return int(m.group(1), 16) if m else None

    def var(self, n, ty):
        mt = re.match(r"^\?gt_([0-9a-f]+)@@3HA$", n)
        if mt:                                   # a __declspec(thread) variable at that TLS offset
            o = int(mt.group(1), 16)
            self.tls.add(o)
            return "(%s)(*(%s*)&tls_%x)" % (CTYPE.get(ty, "int"), CTYPE.get(ty, "int"), o)
        m = re.match(r"^\?ga_([0-9a-f]{8})@@3HA$", n)
        if m:                                    # an address constant (lift2il: a relocated immediate)
            va = int(m.group(1), 16)
            self.gl.add(va)
            return "(%s)g_%08x" % (CTYPE.get(ty, "int"), va)
        va = self._gva(n)
        if va is not None:
            self.gl.add(va)
            return "(%s)(*(%s*)g_%08x)" % (CTYPE.get(ty, "int"), CTYPE.get(ty, "int"), va)
        nm = self.name_of(n)
        if nm == "self":
            return "(%s)(self)" % CTYPE.get(ty, "int")
        if n.startswith("_a") and ty in ("1004", "2004") and self.ptypes.get(nm) in ("float",):
            return "(%s)(*(%s*)&%s)" % (CTYPE[ty], CTYPE[ty], nm)     # a float argument's bits, copied as a dword
        return super().var(n, ty)

    def lvalue(self, tok):
        t = strip_reg(tok)
        m = re.match(r"^([^:\[]+):([0-9a-f]+)$", t)
        mt = re.match(r"^\?gt_([0-9a-f]+)@@3HA:([0-9a-f]+)$", t)
        if mt:
            self.tls.add(int(mt.group(1), 16))
            return "*(%s*)&tls_%x" % (CTYPE.get(mt.group(2), "int"), int(mt.group(1), 16))
        if m:
            va = self._gva(m.group(1))
            if va is not None:
                self.gl.add(va)
                return "*(%s*)g_%08x" % (CTYPE.get(m.group(2), "int"), va)
            if m.group(1) == "_this":                   # `ecx = ecx->next`: this is a cursor (a plain int parameter)
                self.self_assigned = True
                return "self"
        return super().lvalue(tok)

    def rvalue(self, tok, line, want=None):
        t = strip_reg(tok)
        if line["notes"].get("s0") == "&" and re.match(r"^_[al]\d+:", t):
            n = t.rsplit(":", 1)[0]
            nm = self.name_of(n)
            if n.startswith("_l"):
                self.decl.setdefault(nm, "int")
            return "(%s)&%s" % (CTYPE.get(want, "int"), nm)          # a frame slot's address (lift2il's lea)
        return super().rvalue(tok, line, want)

    def stmt(self, L):
        if not getattr(self, "partial", False):
            return self.stmt1(L)
        try:
            return self.stmt1(L)
        except Exception as ex:                  # partial mode: a tuple il2c cannot write becomes a comment
            return ("stmt", "/* lift2il: not emitted (%s: %s) at %s */" % (
                type(ex).__name__, str(ex)[:60].replace("*/", "* /"), L["text"].split()[1]))

    def stmt1(self, L):
        if L["op"] == "OPBRANCH" and L["notes"].get("fneg") == "1":   # jump when !(x cc y) (a float, NaN included)
            r = super().stmt(L)
            if isinstance(r, tuple) and r[0] == "br":
                return ("br", r[2], r[1], r[3])
        if L["op"] == "OPASSIGN" and L["dsts"] and L["srcs"] and len(L["srcs"]) == 1:
            dn, sn = strip_reg(L["dsts"][0]).rsplit(":", 1)[0], strip_reg(L["srcs"][0]).rsplit(":", 1)[0]
            if re.match(r"^(t\d+|_this)$", sn) and re.match(r"^t\d+$", dn):
                self.copyof[dn] = sn
        if L["op"] == "OPEHSTATE":
            return ("stmt", "__EH(%s);" % L["srcs"][0].split(":")[0])
        if L["op"] == "OPEHRESUME":
            return ("stmt", "__EHRESUME;")
        if L["op"] == "OPSETCC":                 # setcc: the comparison's value
            cc = L["notes"]["cc"]
            uns = cc in ("b", "be", "a", "ae")
            ty = "unsigned int" if uns else "int"
            a, b = (self.rvalue(x, L) for x in L["srcs"][:2])
            op = {"e": "==", "ne": "!=", "l": "<", "le": "<=", "g": ">", "ge": ">=", "b": "<", "be": "<=", "a": ">",
                  "ae": ">="}[cc]
            if L["notes"].get("neg") == "1":     # sbb r, r: 0 or -1
                return ("stmt", "%s = (int)(-(int)((%s)(%s) %s (%s)(%s)));" % (self.lvalue(L["dsts"][0]), ty, a, op,
                                                                             ty, b))
            return ("stmt", "%s = (unsigned char)((%s)(%s) %s (%s)(%s));" % (self.lvalue(L["dsts"][0]), ty, a, op, ty, b))
        if L["op"] == "OPSELECT":                # c ? x : y (cl's sbb / and select)
            cc = L["notes"]["cc"]
            ty = "unsigned int" if cc in ("b", "be", "a", "ae") else "int"
            a, b, x, y = (self.rvalue(t, L) for t in L["srcs"][:4])
            op = {"e": "==", "ne": "!=", "l": "<", "le": "<=", "g": ">", "ge": ">=", "b": "<", "be": "<=", "a": ">",
                  "ae": ">="}[cc]
            return ("stmt", "%s = (int)(((%s)(%s) %s (%s)(%s)) ? (int)(%s) : (int)(%s));" % (
                self.lvalue(L["dsts"][0]), ty, a, op, ty, b, x, y))
        if L["op"] == "OPUNSUP":                 # partial mode: what lift2il could not lift, as a comment
            return ("stmt", "/* lift2il: unsupported %s at %x */" % (L["notes"].get("why", "?").replace("*/", "* /"),
                                                                   int(L["text"].split()[1], 16)))
        if L["op"] == "OPARG" and L["dsts"] and L["srcs"]:
            self.argty[strip_reg(L["dsts"][0]).rsplit(":", 1)[0]] = self.ty(strip_reg(L["srcs"][0])) or "1004"
        if L["op"] == "OPSWITCH":
            idx = self.rvalue(L["srcs"][0], L, "2004")
            cases = [c.split(":") for c in L["notes"]["sw"].split(",")]
            byt = collections.defaultdict(list)
            for v, lab in cases:
                byt[lab].append(int(v))
            lines = ["switch (%s) {" % idx]
            for lab, vs in byt.items():
                lines.append("".join("case %d: " % v for v in sorted(vs)) + "goto %s;" % lab)
            lines.append("default: goto %s;" % L["notes"]["def"])
            lines.append("}")
            return ("switch", " ".join(lines), sorted(set(byt) | {L["notes"]["def"]}))
        return super().stmt(L)

    def body(self):
        """il2c's body(), with a switch: the statement, then one unreachable `goto` per case label, so il2c's
        structuring counts those labels as referenced (it reads references only from branches and gotos)."""
        orig = self.stmt
        pending = []

        def stmt2(L):
            r = orig(L)
            if isinstance(r, tuple) and r[0] == "switch":
                pending.append(r)
                return ("stmt", "__SWITCH%d__" % (len(pending) - 1))
            return r
        self.stmt = stmt2
        saved = il2c.structure

        def structure2(items):
            out = []
            for it in items:
                if it[0] == "stmt" and it[1].startswith("__SWITCH"):
                    n = int(it[1][8:-2])
                    out.append(("stmt", pending[n][1]))
                    out += [("goto", lab) for lab in pending[n][2]]
                elif it == ("stmt", ("stmt", it[1][1] if isinstance(it[1], tuple) else None)):
                    out.append(it)
                else:
                    out.append(it)
            return saved(out)
        il2c.structure = structure2
        try:
            return super().body()
        finally:
            il2c.structure = saved
            self.stmt = orig

    def arg_ctype(self, tok):
        t = self.ty(strip_reg(tok)) or "1004"
        if t in ("args",):
            return "unsigned int"
        base = strip_reg(tok).rsplit(":", 1)[0]
        if base in self.args:                       # an OPARG temporary: the type of what it carries
            t = self.argty.get(base, t)
        return {"5004": "float", "4004": "float", "e004": "float", "5008": "double", "4008": "double",
                "e008": "double", "1008": "__int64"}.get(t, "unsigned int")

    def call(self, L):
        d = [t for t in L["dsts"] if not re.match(r"^pas\d+", t)]
        srcs = [t for t in L["srcs"] if not re.match(r"^pas\d+", t)]
        tgt = strip_reg(srcs[0]).rsplit(":", 1)[0]
        mmem = re.match(r"^\[(t\d+|_this):1004(?:\+(\d+))?\]$", tgt)
        if mmem and L["notes"].get("thiscall") == "1" and mmem.group(1) in self.loads and not self.lang_c:
            # `call [eax+D]` with eax = [this]: a virtual call in real IL's form once the load reads `+0`
            vp = mmem.group(1)
            mv = re.match(r"^\[([^+\]]+):1004\]:1004$", self.loads[vp])
            if mv:
                self.loads[vp] = "[%s:1004+0]:1004" % mv.group(1)
            base = re.match(r"^\[([^+\]]+):", self.loads[vp]).group(1)
            if len(srcs) > 1:
                a0 = strip_reg(srcs[1]).rsplit(":", 1)[0]
                x, seen = self.argsrc.get(a0, a0), set()
                while x in self.copyof and x not in seen and x != base:
                    seen.add(x)
                    x = self.copyof[x]
                if x == base:
                    self.argsrc[a0] = base
            r = super().indirect(L, d, ["[%s:1004+%s]:1004" % (vp, mmem.group(2) or "0")] + srcs[1:])
            if "il2c_vc" in r:
                return r
        if re.match(r"^t\d+$", tgt) and L["notes"].get("thiscall") == "1" and tgt in self.loads and \
                not self.lang_c:
            # `mov eax, [ecx]; mov edx, [eax+D]; call edx` with ecx = this: a virtual call. il2c writes one from
            # real IL's form (the callee `[vp+D]`, vp loaded from `[this+0]`); lift2il loads the slot into a register
            mm = re.match(r"^\[(t\d+|_this):1004(?:\+(\d+))?\]:1004$", self.loads[tgt])
            if mm and mm.group(1) in self.loads:
                vp, disp = mm.group(1), mm.group(2) or "0"
                mv = re.match(r"^\[([^+\]]+):1004\]:1004$", self.loads[vp])
                if mv:
                    self.loads[vp] = "[%s:1004+0]:1004" % mv.group(1)
                base = re.match(r"^\[([^+\]]+):", self.loads[vp]).group(1)
                if len(srcs) > 1:                     # `this` passed through a register copy of the object
                    a0 = strip_reg(srcs[1]).rsplit(":", 1)[0]
                    x = self.argsrc.get(a0, a0)
                    seen = set()
                    while x in self.copyof and x not in seen and x != base:
                        seen.add(x)
                        x = self.copyof[x]
                    if x == base:
                        self.argsrc[a0] = base
                r = super().indirect(L, d, ["[%s:1004+%s]:1004" % (vp, disp)] + srcs[1:])
                if "il2c_vc" in r:
                    return r
        g = self._gva(tgt)
        if g is not None and L["notes"].get("thiscall") == "1":
            # `mov ecx, X; call [g]`: a __thiscall through a global pointer. C++ has no __thiscall function pointer;
            # a pointer to member of a single-inheritance class is 4 bytes and calls the same way.
            if self.lang_c:
                raise Unsupported("thiscall through a global pointer in a C unit")
            self.gl.add(g)
            argv = [self.rvalue(a, L) for a in srcs[1:]]
            tys = [self.arg_ctype(a) for a in srcs[2:]]
            rty = self.ctype_of(d[0]) if d else "void"
            n = len(self.prelude)
            self.prelude.append("struct il2c_gm%d { %s m(%s); };\ntypedef %s (il2c_gm%d::*il2c_gp%d)(%s);\n" % (
                n, rty, ", ".join(tys), rty, n, n, ", ".join(tys)))
            e = "(((il2c_gm%d*)(%s))->*(*(il2c_gp%d*)g_%08x))(%s)" % (n, argv[0], n, g, ", ".join(
                "(%s)(%s)" % (t, a) for t, a in zip(tys, argv[1:])))
            return ("%s = (%s)%s;" % (self.lvalue(d[0]), rty, e)) if d else e + ";"
        mf = re.match(r"^\?__lift_(sqrt|abs|sin|cos|tan|atan2|exp|log|log10|asin|acos|atan|sinh|cosh|tanh|pow|"
                      r"fmod)@@YAXXZ$", tgt)
        if mf:                                   # x87 fsqrt / fabs / fsin / fcos / fpatan: intrinsics at /O2
            self.needs_math = True
            fn = {"abs": "fabs"}.get(mf.group(1), mf.group(1))
            vs = ", ".join("(double)(%s)" % self.rvalue(x, L) for x in srcs[1:])
            return "%s = (%s)%s(%s);" % (self.lvalue(d[0]), self.ctype_of(d[0]), fn, vs)
        if g is not None and L["notes"].get("stdcall") == "1":   # `call [import]`, the callee pops: __stdcall
            self.gl.add(g)
            argv = [self.rvalue(a, L) for a in srcs[1:]]
            tys = [self.arg_ctype(a) for a in srcs[1:]]
            rty = self.ctype_of(d[0]) if d else "void"
            e = "((%s (__stdcall*)(%s))(*(int*)g_%08x))(%s)" % (rty, ", ".join(tys), g, ", ".join(
                "(%s)(%s)" % (t, a) for t, a in zip(tys, argv)))
            return ("%s = (%s)%s;" % (self.lvalue(d[0]), rty, e)) if d else e + ";"
        if g is not None:                    # `call [g]`: through a global function pointer (an import, a hook)
            self.gl.add(g)
            argv = [self.rvalue(a, L) for a in srcs[1:]]
            rty = self.ctype_of(d[0]) if d else "void"
            e = "((%s (__cdecl*)(...))(*(int*)g_%08x))(%s)" % (rty, g, ", ".join(argv))
            return ("%s = (%s)%s;" % (self.lvalue(d[0]), rty, e)) if d else e + ";"
        if tgt == "?__lift_strlen@@YAXXZ":       # lift2il's inlined strlen loop: the strlen intrinsic
            self.needs_string = True
            return "%s = (int)strlen((const char*)(%s));" % (self.lvalue(d[0]), self.rvalue(srcs[1], L))
        if tgt == "?__lift_memcpy@@YAXXZ":       # lift2il's block copy (rep movsd): the memcpy intrinsic
            argv = [self.rvalue(a, L) for a in srcs[1:]]
            self.needs_string = True
            return "memcpy((void*)(%s), (const void*)(%s), (unsigned int)(%s));" % tuple(argv)
        mi = re.match(r"^\?__lift_(xadd|xchg|rdtsc|inc|dec)@@YAXXZ$", tgt)
        if mi:                                   # lift2il's lock xadd / xchg / rdtsc: the compiler intrinsics
            argv = [self.rvalue(a, L) for a in srcs[1:]]
            fn = {"xadd": "_InterlockedExchangeAdd", "xchg": "_InterlockedExchange", "rdtsc": "__rdtsc",
                  "inc": "_InterlockedIncrement", "dec": "_InterlockedDecrement"}[mi.group(1)]
            self.intrin.add(fn)
            e = "%s(%s)" % (fn, "(long volatile*)(%s), (long)(%s)" % tuple(argv) if len(argv) == 2 else
                            "(long volatile*)(%s)" % argv[0] if argv else "")
            return ("%s = (%s)%s;" % (self.lvalue(d[0]), self.ctype_of(d[0]), e)) if d else e + ";"
        if tgt == "?__lift_memset@@YAXXZ":       # rep stos: the memset intrinsic
            argv = [self.rvalue(a, L) for a in srcs[1:]]
            self.needs_string = True
            return "memset((void*)(%s), (int)(%s), (unsigned int)(%s));" % tuple(argv)
        m = re.match(r"^\?FUN_([0-9a-f]{8})@@YAXXZ$", tgt)
        if not m:
            return super().call(L)
        va = int(m.group(1), 16)
        if L["notes"].get("noreturn") == "1" and L["notes"].get("stdcall") == "1" and not d and not self.lang_c \
                and len(srcs) == 3 and all(self.rvalue(a, L).strip("()").replace("int", "").strip("() ") == "0"
                                           for a in srcs[1:]):
            return "throw;"                      # `push 0; push 0; call _CxxThrowException`: a rethrow
        argv = [self.rvalue(a, L) for a in srcs[1:]]
        # parameter types from the argument tokens: a float stays a float (an `unsigned int` parameter would convert
        # it, a `...` one would promote it to double)
        tys = [self.arg_ctype(a) for a in srcs[1:]]
        for key, n in L["notes"].items():        # a by-value class: FUN_<ctor> with that copy constructor
            mb = re.match(r"^bc(\d+)$", key)
            if mb and int(mb.group(1)) < len(argv):
                q = int(mb.group(1))
                nb, cv = n.split(".")
                st = "FUN_%08x" % int(cv, 16)
                if self.lang_c:
                    raise Unsupported("a by-value class argument in a C unit")
                if not any(("struct %s " % st) in x for x in self.prelude):
                    self.prelude.append("struct %s { int d[%d]; %s(const %s&); };\n" % (st, int(nb) // 4, st, st))
                tys[q] = st
                argv[q] = "*(%s*)(%s)" % (st, argv[q])
        for key, n in L["notes"].items():        # lift2il's by-value struct: its address, passed as `*(S12*)addr`
            mb = re.match(r"^bv(\d+)$", key)
            if mb and int(mb.group(1)) < len(argv):
                q, n = int(mb.group(1)), int(n)
                st = "il2c_bv%d" % n
                if not any(("struct %s " % st) in x for x in self.prelude):
                    self.prelude.append("struct %s { int d[%d]; };\n" % (st, n // 4))
                tys[q] = st
                argv[q] = "*(%s*)(%s)" % (st, argv[q])
        c = self.calls.setdefault(va, {"sigs": set(), "this_sigs": set(), "ret": False})
        if L["notes"].get("noreturn") == "1":
            c["noreturn"] = True
        if L["notes"].get("thiscall") == "1":
            if not argv:
                raise Unsupported("thiscall without this")
            if self.lang_c:
                raise Unsupported("thiscall in a C unit")
            this, rest = argv[0], argv[1:]
            c["this_sigs"].add(tuple(tys[1:]))
            e = "((C_%08x*)(%s))->FUN_%08x(%s)" % (va, this, va, ", ".join(
                "(%s)(%s)" % (t, a) for t, a in zip(tys[1:], rest)))
        else:
            if L["notes"].get("stdcall") == "1":
                c["std"] = True
            c["sigs"].add(tuple(tys))
            e = "FUN_%08x(%s)" % (va, ", ".join("(%s)(%s)" % (t, a) for t, a in zip(tys, argv)))
            if va == getattr(self, "self_va", None) and len(argv) != len(self.params):
                # a recursive call with another argument count than the definition reads: through a cast (cl
                # still calls it directly)
                e = "((%s (__cdecl*)(%s))&FUN_%08x)(%s)" % (CTYPE.get(self.ty(d[0]), "int") if d else "void",
                                                          ", ".join(tys), va, ", ".join(
                    "(%s)(%s)" % (t, a) for t, a in zip(tys, argv)))
        for st in set(t for t in tys if t.startswith(("il2c_bv", "FUN_"))):
            e = e.replace("(%s)(*(%s*)(" % (st, st), "(*(%s*)(" % st)   # the object itself, no functional cast
        if d:
            rt = CTYPE.get(self.ty(d[0]), "int")
            c["ret"] = True
            if rt in ("float", "double"):        # an x87 result (lift2il saw st0 read after the call)
                c["fret"] = rt
            return "%s = (%s)%s;" % (self.lvalue(d[0]), rt, e)
        return e + ";"


def alias_this(il):
    """EH units: a temporary that only copies `_this` (`mov esi, ecx`) is `_this` itself, so the constructor /
    destructor body reads `self` and a subobject's address is visibly `self + K`."""
    defs = collections.Counter()
    for l in il:
        if l.startswith("I ") and "|" in l:
            for d in l.split("|", 1)[1].split("<-")[0].split():
                defs[d.split(":")[0]] += 1
    al = set()
    for l in il:
        m = re.match(r"^I \S+ OPASSIGN \S+ \S+ \| (t\d+):1004 <- _this:1004\s*$", l)
        if m and defs[m.group(1)] == 1:
            al.add(m.group(1))
    out = []
    for l in il:
        m = re.match(r"^I \S+ OPASSIGN \S+ \S+ \| (t\d+):1004 <- _this:1004\s*$", l)
        if m and m.group(1) in al:
            continue
        out.append(re.sub(r"\b(t\d+):", lambda mm: "_this:" if mm.group(1) in al else mm.group(0), l))
    return out, al


PURE_CALL = re.compile(r"[A-Za-z_]\w*\(|->")


def pure(rhs):
    """No call in the expression: a named call, a member call, or a call through a function-pointer expression
    (`((int (__cdecl*)(int))(p))(x)`: after the plain casts go, a `)(` is left)."""
    r = rhs
    if "__cdecl" in r or "__stdcall" in r or "__thiscall" in r or "->*" in r:
        return False
    for c in ("(int)", "(char*)", "(unsigned short)", "(unsigned char)", "(unsigned int)", "(float)", "(double)",
              "(short)", "(int*)", "(float*)", "(double*)", "(unsigned short*)", "(unsigned char*)", "(short*)",
              "(char)", "(bool)", "(void*)", "(signed char)", "(__int64)", "(unsigned __int64)"):
        r = r.replace(c, "")
    return not PURE_CALL.search(r) and not re.search(r"\)\s*\(", r)


def drop_unused_decls(body):
    """A declared local nothing reads or writes (a call's dropped result): at /Od every declared local gets a
    frame slot (`push ecx`, an `mov esp, ebp` epilogue: Spore 012538b0), so leave none behind."""
    for m in list(re.finditer(r"(?m)^    [\w ]+?\*? ((?:t|v_l)\d+)(?:\[\d+\])?;\n", body)):
        if len(re.findall(r"\b%s\b" % m.group(1), body)) == 1:
            body = body.replace(m.group(0), "", 1)
    return body


def fold_aggregates(body, lf):
    """lift2il's aggregates (a block copy's frame source / destination of a known size): the slots inside become
    elements of one array declared at the lowest one (`int v_l36[9]`; `v_l32` -> `*(float*)&v_l36[1]`)."""
    for off, n in sorted(getattr(lf, "aggregates", ())):
        N = -off
        if n % 4 or n > N:
            continue
        base = "v_l%d" % N
        decl = dict((m.group(2), m.group(1)) for m in re.finditer(r"(?m)^    ([\w ]+?\*?) (v_l\d+);$", body))
        if base not in decl:
            continue
        inner = [M for M in range(N, N - n, -4) if "v_l%d" % M in decl]
        lines = body.split("\n")
        out = []
        for l in lines:
            m = re.match(r"^    [\w ]+?\*? (v_l\d+);$", l)
            if m and m.group(1) in ["v_l%d" % M for M in inner]:
                if m.group(1) == base:
                    out.append("    int %s[%d];" % (base, n // 4))
                continue
            for M in inner:
                v, k, ty = "v_l%d" % M, (N - M) // 4, decl["v_l%d" % M]
                el = "%s[%d]" % (base, k)
                l = re.sub(r"\*\(([\w ]+\*?)\*\)&\(%s\)" % v, r"*(\1*)&%s" % el, l)
                if M == N:
                    l = re.sub(r"(?<![\w&])%s\b(?!\[)" % v, "(*(%s*)&%s)" % (ty, el), l)
                else:
                    l = re.sub(r"&%s\b" % v, "&%s" % el, l)
                    l = re.sub(r"(?<![\w&])%s\b(?!\[)" % v, "(*(%s*)&%s)" % (ty, el), l)
            out.append(l)
        body = "\n".join(out)
    return body


def size_addressed_locals(body, lf):
    """A local used only through its address (`&v_lN`: a buffer or out-structure passed to a callee) is as big as
    the room up to the next frame item above it; declared `int v_lN;` it would shrink the frame."""
    offs = sorted(lf.frame_offsets())
    pro = sorted(getattr(lf, "pro", ()))
    sub = next((q for q in pro if lf.ins[q].mnemonic == "sub"), None)
    top = -4 * sum(1 for q in pro if lf.ins[q].mnemonic == "push" and lf.ins[q].op_str in lift2il.CALLEE_SAVED
                   and (sub is None or q < sub))   # pushes after the `sub esp` are below the locals
    for m in re.finditer(r"(?m)^    int (v_l(\d+));$", body):
        name, n = m.group(1), int(m.group(2))
        uses = re.findall(r"(&?)\b%s\b" % name, body)
        if len(uses) < 2 or any(u != "&" for u in uses[1:]):
            continue
        above = [o for o in offs if o > -n] + [top]
        size = min(above) + n
        if size > 4 and size % 4 == 0:
            body = body.replace("    int %s;" % name, "    int %s[%d];" % (name, size // 4), 1)
    return body


def drop_dead(text):
    """Assignments of a pure value to a temporary nobody reads (lift2il's register copies, and the vtable loads
    a virtual call rewrites away), with their declarations: they can sway the register allocator. A call whose
    result nobody reads keeps the call and loses the assignment. Linear: token counts kept per temporary
    (dead stays dead, so the fixpoint does not depend on the order)."""
    lines = text.split("\n")
    alive = [True] * len(lines)
    TOK = re.compile(r"\bt\d+\b")
    DEF = re.compile(r"^\s+(t\d+) = ([^;]*);$")
    CALLDEF = re.compile(r"^(\s+)(t\d+) = \([\w ]+\*?\)(.*\(.*\));$")
    DCL = re.compile(r"^\s+[\w ]+\*? (t\d+);$")
    toks = [TOK.findall(l) for l in lines]
    occ, defs, decl = collections.Counter(), collections.Counter(), collections.defaultdict(list)
    for k, l in enumerate(lines):
        occ.update(toks[k])
        m = re.match(r"^\s+(t\d+) = ", l)
        if m:
            defs[m.group(1)] += 1
        md = DCL.match(l)
        if md:
            decl[md.group(1)].append(k)
    dead = lambda t: occ[t] == defs[t] + len([q for q in decl[t] if alive[q]])
    while True:
        changed = False
        for k, l in enumerate(lines):
            if not alive[k]:
                continue
            m = DEF.match(l)
            if not m or not pure(m.group(2)) or not dead(m.group(1)):
                continue
            t = m.group(1)
            alive[k] = False
            occ.subtract(toks[k])
            defs[t] -= 1
            if defs[t] == 0:
                for q in decl[t]:
                    if alive[q]:
                        alive[q] = False
                        occ[t] -= 1
            changed = True
        if changed:
            continue
        for k, l in enumerate(lines):
            if not alive[k]:
                continue
            m = CALLDEF.match(l)
            if m and not pure(m.group(3)) and dead(m.group(2)):
                lines[k] = m.group(1) + m.group(3) + ";"
                occ[m.group(2)] -= 1
                defs[m.group(2)] -= 1
                toks[k] = TOK.findall(lines[k])
                changed = True
        if not changed:
            return "\n".join(l for k, l in enumerate(lines) if alive[k])


def eh_tidy(stmts):
    """Undo what scheduling does around the state stores: a temporary assigned once from a constant or from
    `self` is that value; a pure load into a temporary just before a state store, all of whose uses follow it,
    moves after the store (cl hoists loads over the store; the source reads them later)."""
    text = "\n".join(stmts)
    once = lambda t: len(re.findall(r"\b%s = " % t, text)) == 1
    for _ in range(6):
        for l in list(stmts):
            mk = re.match(r"^\s+(t\d+) = \(([\w ]+)\)\(\(int\)(-?\d+)\);$", l)   # a constant of any type
            if mk and mk.group(2) != "int" and once(mk.group(1)):
                stmts = [re.sub(r"\b%s\b" % mk.group(1), "(%s)(%s)" % (mk.group(2), mk.group(3)), x)
                         for x in stmts if x is not l]
                text = "\n".join(stmts)
                continue
            m = re.match(r"^\s+(t\d+) = \(int\)\(\(int\)(\(self\)|\(a\d+\)|g_[0-9a-f]{8}|-?\d+)\);$", l)
            mc = re.match(r"^\s+(t\d+) = \(int\)\((t\d+)\);$", l)        # a copy of a temporary
            if mc and once(mc.group(1)) and once(mc.group(2)):
                stmts = [re.sub(r"\b%s\b" % mc.group(1), mc.group(2), x) for x in stmts if x is not l]
                text = "\n".join(stmts)
                continue
            if not m or not once(m.group(1)):
                continue
            val = "(int)%s" % m.group(2) if m.group(2).startswith(("(", "g_")) else "(int)(%s)" % m.group(2)
            stmts = [re.sub(r"\b%s\b" % m.group(1), val, x) for x in stmts if x is not l]
            text = "\n".join(stmts)
    out = list(stmts)
    changed = True
    while changed:
        changed = False
        for k in range(len(out) - 1, 0, -1):
            if not re.match(r"^    __EH\(-?\d+\);$", out[k]):
                continue
            m = re.match(r"^    (t\d+) = ([^;]*);$", out[k - 1])
            if not m or re.search(r"[A-Za-z_]\w*\(", m.group(2).replace("(int)", "").replace("(char*)", "")
                                  .replace("(unsigned short)", "").replace("(unsigned char)", "")):
                continue
            t = m.group(1)
            if any(re.search(r"\b%s\b" % t, x) for x in out[:k - 1]):
                continue
            out[k - 1], out[k] = out[k], out[k - 1]
            changed = True
    return out


DECL = re.compile(r"^    (?:unsigned |signed )?(?:int|char|short|float|double|bool|__int64)\*? (t\d+|v_l\d+);$")


def eh_unit(lf, em, va, params, typed, head):
    """A function with a C++ EH frame (destructor states only), as the C++ that makes one. All states name
    subobjects of one object: a global (a static initialiser: the inline constructor of a stand-in class called
    on it) or `this` (the function is that class's constructor, or its destructor). The body splits at the state
    stores: in a constructor the code before store s builds subobject s (one out-of-line constructor call, or
    inline code), what follows the last store is the body; in a destructor the body comes first and each store
    after it is followed by the teardown of one subobject. Each subobject is a stand-in member at the unwind
    funclet's offset whose destructor (out of line) is what the funclet calls."""
    st = lf.eh["states"]
    kinds = {x["kind"] for x in st}
    if lf.eh["tries"]:
        return eh_try(lf, em, va, params, typed, head)
    if kinds == {"local"}:
        return eh_local(lf, em, va, params, typed, head)
    if kinds == {"delete"}:
        return eh_new(lf, em, va, params, typed, head)
    if kinds == {"pdelete"}:
        return eh_placement(lf, em, va, params, typed, head)
    if kinds == {"glob"} and len({x["addr"] for x in st}) == 1:
        obj = "glob"
    elif kinds == {"slot"} and len({x["off"] for x in st}) == 1:
        vals = lf.eh_slot_vals.get(st[0]["off"], [])
        if not vals or any(v.split(":")[0] not in ("_this",) and v.split(":")[0] not in em.this_alias for v in vals):
            raise Unsupported("EH: the funclet's object is not this")
        obj = "this"
    else:
        raise Unsupported("EH: states on %s" % "/".join(sorted(kinds)))
    if any(x["to"] != n - 1 for n, x in enumerate(st)):
        raise Unsupported("EH: unwind map is not a chain")
    adds = [x["add"] for x in st]
    if any(b <= a for a, b in zip(adds, adds[1:])):
        raise Unsupported("EH: subobject offsets not increasing")
    body = drop_dead(em.body())
    lines = body.split("\n")
    assert lines[0] == "{" and lines[-1] == "}", body[:40]
    lines = lines[1:-1]
    decls = [l for l in lines if DECL.match(l)]
    stmts = eh_tidy([l for l in lines if not DECL.match(l)])
    segs, marks = [[]], []
    for l in stmts:
        m = re.match(r"^(\s*)__EH\((-?\d+)\);$", l)
        if m:
            if len(m.group(1)) != 4:
                raise Unsupported("EH: a state change inside a branch")
            marks.append(int(m.group(2)))
            segs.append([])
        else:
            segs[-1].append(l)
    n = len(st)
    if marks == list(range(n)):
        mode = "ctor"
    elif obj == "this" and marks == list(range(n - 1, -2, -1)):
        mode = "dtor"
    else:
        raise Unsupported("EH: state sequence %s" % marks)
    if obj == "glob" and mode != "ctor":
        raise Unsupported("EH: a global's destructor")
    decl_of = {DECL.match(l).group(1): l for l in decls}
    temps_in = lambda seg: set(re.findall(r"\b(t\d+|v_l\d+)\b", "\n".join(seg)))
    G = "g_%08x" % st[0]["addr"] if obj == "glob" else None
    if G:
        em.gl.add(st[0]["addr"])
    base_e = "(int)(self)" if obj == "this" else "(int)%s" % G

    def single_call(seg, add):
        """`[tN = self + K;] ((C_x*)(E))->FUN_x(args);` on the subobject: (callee va, args text, its decl)."""
        seg = [l for l in seg if l.strip()]
        alias = None
        if len(seg) >= 2 and not seg[0].lstrip().startswith("(("):
            m = re.match(r"^\s+(t\d+) = \(int\)\(%s \+ \(int\)%d\);$" % (re.escape(base_e), add), seg[0])
            if not m:
                return None
            alias, seg = m.group(1), seg[1:]
        if len(seg) != 1:
            return None
        m = re.match(r"^\s+(?:(t\d+) = \(int\))?\(\(C_([0-9a-f]{8})\*\)\((.+?)\)\)->FUN_\2\((.*)\);$", seg[0])
        if not m or (m.group(1) and used[m.group(1)] > 1):   # a result assigned to a temporary nobody reads
            return None
        callee, e, args = m.group(2), m.group(3), m.group(4)
        if not (e == alias or (add == 0 and e == base_e)):
            return None
        if re.search(r"\b(t\d+|v_l\d+)\b", args):
            return None
        return int(callee, 16), args
    # segments: which belong to which subobject
    poly = False
    if mode == "ctor":

        build = {s: segs[s] for s in range(n)}
        bodyseg, tear = segs[n], {}
    else:
        pre = [l for l in segs[0] if l.strip()]
        VP = re.compile(r"^\s+\*\(int\*\)\(\(char\*\)\(%s\)\) = \(int\)\(\(int\)g_[0-9a-f]{8}\);$"
                        % re.escape(base_e))
        if len(pre) == 1 and VP.match(pre[0]) and adds[0] == 0:
            poly = True                      # cl's own vptr store at a polymorphic class's destructor entry
            segs[0] = []
        elif pre:
            raise Unsupported("EH: code before a destructor's first state")
        bodyseg = segs[1]
        last = segs[-1]
        while last and re.match(r"^    return[^;]*;$", last[-1]):
            last.pop()                       # a destructor returns nothing (eax at its ret is left over)
        segs = [[re.sub(r"^(\s+)return [^;]*;$", r"\1return;", l) for l in sg] for sg in segs]
        bodyseg = segs[1]
        tear = {n - 1 - j: segs[2 + j] for j in range(n)}
        build = {}
    used = collections.Counter()
    for sg in segs:
        for t in temps_in(sg):
            used[t] += 1
    plain = ", ".join(typed)
    pnames = ", ".join(params)
    cls = "FUN_%08x" % va if obj == "this" else "EW_%08x" % va
    sub, inits = [], []
    for s in range(n):
        add, nxt = adds[s], (adds[s + 1] if s + 1 < n else adds[s] + 4)
        name = "S%d_%08x" % (s, va)
        parts = ["char pad[%d];" % (nxt - add)]
        b = build.get(s, [])
        if any(l.strip() for l in b):
            sc = single_call(b, add)
            if sc:
                cva, args = sc
                nargs = len([a for a in re.split(r",\s*(?![^()]*\))", args) if a.strip()]) if args.strip() else 0
                parts.append("%s(%s);" % (name, ", ".join(["unsigned int"] * nargs)))
                inits.append("m%d(%s)" % (s, args))
                pass                                     # (an unused callee declaration is harmless)
            else:
                if any(used[t] > 1 for t in temps_in(b)):
                    raise Unsupported("EH: a temporary crosses a subobject's construction")
                ds = [decl_of[t] for t in sorted(temps_in(b)) if t in decl_of]
                parts.append("__forceinline %s(%s) {\n%s\n%s\n    }" % (
                    name, ", ".join(["char* self"] + typed), "\n".join("    " + d for d in ds),
                    "\n".join("    " + l for l in b)))
                inits.append("m%d(%s)" % (s, ", ".join(["(char*)this"] + params)))
        t = tear.get(s)
        if t is not None and any(l.strip() for l in t) and not single_call(t, add):
            if any(used[x] > 1 for x in temps_in(t)):
                raise Unsupported("EH: a temporary crosses a subobject's destruction")
            ds = [decl_of[x] for x in sorted(temps_in(t)) if x in decl_of]
            parts.append("__forceinline ~%s() {\n        char* self = (char*)this - %d;\n%s\n%s\n    }" % (
                name, add, "\n".join("    " + d for d in ds), "\n".join("    " + l for l in t)))
        else:
            if t is not None and any(l.strip() for l in t):
                pass                                     # (an unused callee declaration is harmless)
            parts.append("~%s();" % name)
        if poly and s == 0:                  # the vptr is the first 4 bytes
            if nxt - add < 4:
                raise Unsupported("EH: a polymorphic base with no room")
            parts[0] = "char pad[%d];" % (nxt - add - 4) if nxt - add > 4 else ""
            parts.insert(0, "virtual void v0();")
        sub.append("struct %s { %s };" % (name, " ".join(parts)))
    owned = set()
    for sgs in list(build.values()) + list(tear.values()):
        owned |= temps_in(sgs) if any(l.strip() for l in sgs) else set()
    bdecls = [d for d in decls if DECL.match(d).group(1) not in owned or used[DECL.match(d).group(1)] > 1]
    blines = [re.sub(r"^(\s+)return [^;]*;$", r"\1return;", l) for l in bodyseg]
    members = (["char p0[%d];" % adds[0]] if adds[0] else []) + ["S%d_%08x m%d;" % (s, va, s) for s in range(n)]
    bases = ""
    if poly:
        members = ["virtual void w0();"] + members[1:]
        bases = " : public S0_%08x" % va
        inits = [("S0_%08x" % va) + x[2:] if x.startswith("m0(") else x for x in inits]
    inner = "\n".join(["    char* self = (char*)this;"] * (obj == "this") + bdecls + blines)
    init = (" : " + ", ".join(inits)) if inits else ""
    if obj == "glob":
        if params:
            raise Unsupported("EH: a static initialiser with parameters")
        cdecl = "struct %s%s { %s __forceinline %s()%s {\n%s\n    } };" % (cls, bases, " ".join(members), cls, init,
                                                                       inner)
        fn = 'void __cdecl FUN_%08x(void)\n{\n    ((%s*)%s)->%s::%s();\n}' % (va, cls, G, cls, cls)
    elif mode == "ctor":
        cdecl = "struct %s%s { %s %s(%s); };" % (cls, bases, " ".join(members), cls, plain)
        fn = "%s::%s(%s)%s\n{\n%s\n}" % (cls, cls, plain, init, inner)
    else:
        if params:
            raise Unsupported("EH: a destructor with parameters")
        cdecl = "struct %s%s { %s ~%s(); };" % (cls, bases, " ".join(members), cls)
        fn = "%s::~%s()\n{\n%s\n}" % (cls, cls, inner)
    return "\n".join(head(em) + sub + [cdecl]) + "\n\n" + fn + "\n"


def eh_local(lf, em, va, params, typed, head):
    """One state on a local object: `((C_x*)&v_lN)->ctor(args); [state 0] ... [state -1] ((C_y*)&v_lN)->dtor();
    return;` is `{ L v_lN(args); ... }` with L's constructor and destructor out of line (stand-ins whose calls
    are relocations). The object's size is the gap to the next frame item above it."""
    st = lf.eh["states"]
    if len(st) != 1 or st[0]["off"] >= 0:
        raise Unsupported("EH: %d local states%s" % (len(st), " (a by-value argument)" if st[0]["off"] >= 0 else ""))
    off = st[0]["off"]
    name = "v_l%d" % -off
    body = drop_dead(em.body())
    lines = body.split("\n")[1:-1]
    decls = [l for l in lines if DECL.match(l)]
    stmts = eh_tidy([l for l in lines if not DECL.match(l)])
    text = "\n".join(stmts)
    # temporaries that only hold &v_lN are the object's address
    for m in re.finditer(r"(?m)^\s+(t\d+) = \(int\)\(\(int\)&%s\);$" % name, text):
        t = m.group(1)
        if len(re.findall(r"\b%s = " % t, text)) == 1:
            stmts = [x for x in stmts if not re.match(r"^\s+%s = \(int\)\(\(int\)&%s\);$" % (t, name), x)]
            stmts = [re.sub(r"\b%s\b" % t, "(int)(&%s)" % name, x) for x in stmts]
    marks = [k for k, l in enumerate(stmts) if re.match(r"^    __EH\(-?\d+\);$", l)]
    if [stmts[k].strip() for k in marks] == ["__EH(0);"] or (
            [stmts[k].strip() for k in marks] == ["__EH(0);", "__EH(-1);"] and
            not re.match(r"^    (?:(t\d+) = \(int\))?\(\(C_", stmts[marks[1] + 1] if marks[1] + 1 < len(stmts) else "")):
        return eh_local_inline(lf, em, va, params, typed, head, stmts, decls, marks, off)
    if [stmts[k].strip() for k in marks] != ["__EH(0);", "__EH(-1);"]:
        raise Unsupported("EH: local state sequence")
    k0, k1 = marks
    CALL = r"^    (?:(t\d+) = \(int\))?\(\(C_([0-9a-f]{8})\*\)\(\(int\)\(&%s\)\)\)->FUN_\2\((.*)\);$" % name
    ctor = re.match(CALL, stmts[k0 - 1]) if k0 else None
    if not ctor or re.search(r"\b(t\d+|v_l\d+)\b", ctor.group(3)):
        raise Unsupported("EH: a local's construction is not one call")
    dtor = re.match(CALL, stmts[k1 + 1]) if k1 + 1 < len(stmts) else None
    rest = [l for l in stmts[k1 + 2:] if l.strip()]
    if not dtor or dtor.group(3).strip() or any(not re.match(r"^    return[^;]*;$", l) for l in rest):
        raise Unsupported("EH: a local's destruction is not one call before the return")
    above = sorted(o for o in lf.frame_offsets() if o > off) + [lf.eh["node"]]
    size = min(o for o in above if o > off) - off
    nargs = len([a for a in re.split(r",\s*(?![^()]*\))", ctor.group(3)) if a.strip()]) if ctor.group(3).strip() else 0
    cls = "L_%08x" % va
    pass                                     # (an unused callee declaration is harmless)
    pass                                     # (an unused callee declaration is harmless)
    # an int array: a char array in a local is a /GS buffer (cl adds a cookie)
    pad = "int pad[%d];" % (size // 4) if size % 4 == 0 else "short pad[%d];" % (size // 2) if size % 2 == 0 else \
        "struct { int a; } pad[%d]; char pb[%d];" % (size // 4, size % 4)
    sub = "struct %s { %s %s(%s); ~%s(); };" % (cls, pad, cls, ", ".join(["unsigned int"] * nargs), cls)
    decl = "    %s %s%s;" % (cls, name, "(%s)" % ctor.group(3) if nargs else "")
    out = stmts[:k0 - 1] + [decl] + stmts[k0 + 1:k1] + rest
    decls = [d for d in decls if DECL.match(d).group(1) != name]
    sig = _eh_sig(lf, va, typed)
    return "\n".join(head(em) + [sub]) + "\n\n" + sig + "{\n" + "\n".join(decls + out) + "\n}\n"


def eh_new(lf, em, va, params, typed, head):
    """One state on a `delete` of a frame slot: `p = H->Alloc(SIZE); [state 0] if (p) { r = p->ctor(args);
    [*(int*)p = &vtable; r = p;] } else r = 0; [state -1]` is `r = new N(args)`, N having an inline class
    `operator new` that calls the allocator (cl passes sizeof(N) = SIZE), an out-of-line `operator delete` (the
    funclet's callee; cl calls it if the constructor throws), and an out-of-line constructor (with a derived
    class's inline constructor storing the vptr, when one follows the call)."""
    st = lf.eh["states"]
    if len(st) != 1:
        raise Unsupported("EH: %d delete states" % len(st))
    body = drop_dead(em.body())
    lines = body.split("\n")[1:-1]
    decls = [l for l in lines if DECL.match(l)]
    stmts = eh_tidy([l for l in lines if not DECL.match(l)])
    marks = [k for k, l in enumerate(stmts) if re.match(r"^    __EH\(-?\d+\);$", l)]
    if not marks or stmts[marks[0]].strip() != "__EH(0);" or \
            [stmts[k].strip() for k in marks[1:]] not in ([], ["__EH(-1);"]):
        raise Unsupported("EH: new state sequence")
    k0 = marks[0]
    k1 = marks[1] if len(marks) > 1 else len(stmts)
    A = re.match(r"^    (t\d+) = \(int\)\(\(C_([0-9a-f]{8})\*\)\((\(int\)g_[0-9a-f]{8})\)\)->FUN_\2\("
                 r"\(unsigned int\)\(\(int\)(\d+)\)\);$", stmts[k0 - 1]) if k0 else None
    # or a direct allocator call with the size first and extra arguments: `new (extra..) N(..)`, N's operator
    # new / delete out of line with that many extra parameters
    B = re.match(r"^    (t\d+) = \(int\)FUN_([0-9a-f]{8})\(\(unsigned int\)\(\(int\)\(?(\d+)\)?\)((?:, [^;]*)?)\);$",
                 stmts[k0 - 1]) if k0 and not A else None
    extras = []
    if A:
        p, alloc, heap, size = A.group(1), int(A.group(2), 16), A.group(3), int(A.group(4))
    elif B and st[0].get("extra") is not None:
        p, alloc, heap, size = B.group(1), int(B.group(2), 16), None, int(B.group(3))
        extras = [x.strip() for x in re.split(r",\s*(?![^()]*\))", B.group(4)[2:]) if x.strip()] if B.group(4) else []
        if len(extras) != st[0]["extra"] or re.search(r"\bt\d+\b", B.group(4) or ""):
            raise Unsupported("EH: new's allocator arguments")
        pass                                     # (an unused callee declaration is harmless)
    else:
        raise Unsupported("EH: new without a class allocator call before state 0")
    reg = stmts[k0 + 1:k1]
    txt = "\n".join(reg)
    P = re.escape(p)
    CT = r"(?:(t\d+) = \(int\))?\(\(C_(?P<cv>[0-9a-f]{8})\*\)\(%s\)\)->FUN_(?P=cv)\(([^;]*)\);" % P
    VPS = r"\*\(int\*\)\(\(char\*\)\(%s\)\) = \(int\)\(\(int\)(g_[0-9a-f]{8})\);" % P
    forms = [  # (regex over the region, kind)
        (r"^    (t\d+) = \(int\)\(\(int\)0\);\n    if \(%s != \1\) \{\n        %s\n(?:        %s\n)?"
         r"        \1 = \(int\)\(%s\);\n    \}(?:\n|$)" % (P, CT, VPS, P), "zero"),
        (r"^    if \(%s != \(int\)0\) \{\n        %s\n(?:        %s\n)?(?:        (t\d+) = \(int\)\(%s\);\n)?"
         r"    \} else \{\n        (t\d+) = \(int\)\(\(int\)0\);\n    \}(?:\n|$)" % (P, CT, VPS, P), "else"),
        (r"^    if \(%s != \(int\)0\) \{\n(?:        (t\d+) = \(int\)\(%s\);\n)?        %s\n    \}(?:\n|$)"
         % (P, P, CT.replace("\\(%s\\)\\)->" % P, "\\((?:%s|t\\d+)\\)\\)->" % P)), "stmt"),
        (r"^    if \(%s != \(int\)0\) \{\n        %s\n(?:        %s\n)?        return;\n    \}\n"
         r"    return \(int\)\(\(int\)\(0\)\);(?:\n|$)" % (P, CT, VPS), "ret"),
    ]
    for rx, kind in forms:
        m = re.match(rx, txt)
        if m:
            break
    else:
        raise Unsupported("EH: new region shape")
    g = m.groups()
    if kind == "zero":
        res, cres, ctor, args, vt = g[0], g[1], g[2], g[3], g[4]
    elif kind == "else":
        cres, ctor, args, vt, res2, res = g[0], g[1], g[2], g[3], g[4], g[5]
        if (res2 or cres) != res:
            raise Unsupported("EH: new result")
    elif kind == "stmt":                     # `if (p) p->ctor(..);`, the result unused: a new statement
        cres, ctor, args, vt, res = g[1], g[2], g[3], None, None
    else:
        cres, ctor, args, vt, res = g[0], g[1], g[2], g[3], None
    if re.search(r"\bt\d+\b", args):
        raise Unsupported("EH: new arguments read temporaries")
    nargs = len([a for a in re.split(r",\s*(?![^()]*\))", args) if a.strip()]) if args.strip() else 0
    pass                                     # (an unused callee declaration is harmless)
    n_ = "N_%08x" % va
    ps = ", ".join(["unsigned int"] * nargs)
    if heap:
        if not any(re.search(r"->FUN_%08x\(" % alloc, l) for l in stmts[:k0 - 1] + stmts[k1:]):
            em.calls[alloc] = em.calls.get(alloc) or {"sigs": set(), "this_sigs": set(), "ret": True}
        em.calls[alloc]["this_sigs"].add(("unsigned int",))
        em.calls[alloc]["ret"] = True
        sub = ["struct %s { int pad[%d]; %s(%s); __forceinline static void* operator new(unsigned int n) { return "
               "(void*)((C_%08x*)(%s))->FUN_%08x(n); } static void operator delete(void*); };" % (
                   n_, size // 4, n_, ps, alloc, heap, alloc)]
    else:
        xp = ", ".join(["unsigned int"] * len(extras))
        sub = ["struct %s { int pad[%d]; %s(%s); static void* operator new(unsigned int, %s); "
               "static void operator delete(void*, %s); };" % (n_, size // 4, n_, ps, xp, xp)]
    if size % 4:
        raise Unsupported("EH: new of %d bytes" % size)
    cls = n_
    if vt:
        cls = "D_%08x" % va
        pl = ", ".join("unsigned int x%d" % q for q in range(nargs))
        sub.append("struct %s : %s { __forceinline %s(%s) : %s(%s) { *(int*)this = (int)%s; } };" % (
            cls, n_, cls, pl, n_, ", ".join("x%d" % q for q in range(nargs)), vt))
        em.gl.add(int(vt[2:], 16))
    e = "(int)(new %s%s(%s))" % ("(%s) " % ", ".join("(unsigned int)(%s)" % x for x in extras) if extras else "",
                                 cls, args)
    new_lines = ["    return %s;" % e] if kind == "ret" else ["    %s;" % e] if kind == "stmt" else \
        ["    %s = %s;" % (res, e)]
    tail = [l for l in txt[m.end():].split("\n") if l.strip()]
    out = stmts[:k0 - 1] + new_lines + tail + stmts[k1 + 1:]
    sig = _eh_sig(lf, va, typed)
    used = set(re.findall(r"\b(t\d+)\b", "\n".join(out)))
    decls = [d for d in decls if DECL.match(d).group(1) in used]
    return "\n".join(head(em) + sub) + "\n\n" + sig + "{\n" + "\n".join(decls + out) + "\n}\n"


def eh_try(lf, em, va, params, typed, head):
    """try / catch without destructor states: the code between the store of the try's state and the store of -1
    is `try { .. } catch (...) { handler }`. Only rethrowing catch(...) handlers so far (`throw;`)."""
    tries, st = lf.eh["tries"], lf.eh["states"]
    if len(tries) != 1 or any(x["kind"] != "none" for x in st):
        raise Unsupported("EH: %d try blocks with %s states" % (len(tries), "/".join(sorted({x["kind"] for x in st}))))
    t = tries[0]
    # the caught type is in FuncInfo, not in the function's bytes: any catch is written catch (...); a bound
    # catch object (dispCatchObj) would be a frame slot of its own
    if t["low"] != t["high"] or len(t["catches"]) != 1 or t["catches"][0]["disp"]:
        raise Unsupported("EH: a try other than one catch without an object")
    hd = t["catches"][0]["handler"]
    body = drop_dead(em.body())
    lines = body.split("\n")[1:-1]
    decls = [l for l in lines if DECL.match(l)]
    stmts = eh_tidy([l for l in lines if not DECL.match(l)])
    # a catch that resumes at code of its own (`catch (...) { return false; }`): that code is the catch body
    catch_code = []
    rm = next((k for k, l in enumerate(stmts) if l.strip() == "__EHRESUME;"), None)
    if rm is not None:
        if stmts[rm] != "    __EHRESUME;" or not any(re.match(r"^    return\b", l) for l in stmts[rm + 1:]):
            raise Unsupported("EH: a catch's own code that does not return")
        catch_code = [l for l in stmts[rm + 1:] if l.strip()]
        stmts = stmts[:rm]
        if not any(l.strip() == "__EH(-1);" for l in stmts):
            stmts = stmts + ["    __EH(-1);"]       # the try returns from inside: it ends with the function
    marks = [k for k, l in enumerate(stmts) if re.match(r"^\s*__EH\(-?\d+\);$", l)]
    if [stmts[k].strip() for k in marks] != ["__EH(%d);" % t["low"], "__EH(-1);"]:
        raise Unsupported("EH: try state sequence %s" % [stmts[k].strip() for k in marks])
    ind = lambda l: len(l) - len(l.lstrip())
    k0, k1 = marks
    if ind(stmts[k1]) < ind(stmts[k0]):
        # the try is inside a block and cl merged its -1 store to after the join (`if (..) { try {..} catch }`):
        # end the try with that block, when only closing braces lie between
        j = next(q for q in range(k0 + 1, len(stmts)) if ind(stmts[q]) < ind(stmts[k0]))
        if any(stmts[q].strip() != "}" for q in range(j, k1)):
            raise Unsupported("EH: a try's end is not its block's end")
        stmts = stmts[:j] + [" " * ind(stmts[k0]) + "__EH(-1);"] + stmts[j:k1] + stmts[k1 + 1:]
    elif ind(stmts[k1]) != ind(stmts[k0]):
        raise Unsupported("EH: try state stores at different depths")
    # a variable live across the try lives in memory; a copy of a parameter written back to the parameter's
    # home (`*(int*)&(a8) = t1008;`) is the parameter itself in the source
    text = "\n".join(stmts)
    for m in re.finditer(r"(?m)^\s+\*\(int\*\)&\((a\d+)\) = \(int\)\((t\d+)\);$", text):
        a, tx = m.group(1), m.group(2)
        if re.search(r"(?m)^\s+%s = \(int\)\(\(int\)\(%s\)\);$" % (tx, a), text):
            stmts = [x for x in stmts if not re.match(r"^\s+(\*\(int\*\)&\(%s\) = \(int\)\(%s\)|%s = \(int\)\(\(int\)"
                                                     r"\(%s\)\));$" % (a, tx, tx, a), x)]
            stmts = [re.sub(r"\b%s\b" % tx, a, x) for x in stmts]
    # `v_lN = self;` once: cl's own spill of `this` for the handler, not a source variable
    text = "\n".join(stmts)
    for m in re.finditer(r"(?m)^\s+(v_l\d+) = \(int\)\(\(int\)\(self\)\);$", text):
        v = m.group(1)
        if len(re.findall(r"\b%s = " % v, text)) == 1 and not re.search(r"&\(?%s\b" % v, text):
            stmts = [x for x in stmts if x.strip() != m.group(0).strip()]
            stmts = [re.sub(r"\b%s\b" % v, "(int)(self)", x) for x in stmts]
            decls = [d for d in decls if not re.search(r" %s;$" % v, d)]
            lf.eh["self_spill"] = -int(v[3:])
    stmts, decls = _inline_single(stmts, decls)
    marks = [k for k, l in enumerate(stmts) if re.match(r"^\s*__EH\(-?\d+\);$", l)]
    k0, k1 = marks
    used = set(re.findall(r"\b(t\d+)\b", "\n".join(stmts + catch_code)))
    decls = [d for d in decls if DECL.match(d).group(1) in used or not DECL.match(d).group(1).startswith("t")]
    pad = " " * (len(stmts[k0]) - len(stmts[k0].lstrip()))
    # a temporary assigned in the try, not read after it, is a variable of its own there (lift2il's webs join
    # the zero register with a call result that reaches the same ret): one variable live across the try would
    # get a memory home
    region, after = stmts[k0 + 1:k1], "\n".join(stmts[k1 + 1:])
    for tq in sorted(set(re.findall(r"(?m)^\s+(t\d+) = ", "\n".join(region)))):
        if re.search(r"\b%s\b" % tq, after):
            continue
        first = next(q for q, l in enumerate(region) if re.search(r"\b%s\b" % tq, l))
        if not re.match(r"^\s+%s = " % tq, region[first]) or re.search(r"\b%s\b" % tq,
                                                                      region[first].split("=", 1)[1]):
            continue                         # read before written in the try: the outer value
        region = [re.sub(r"\b%s\b" % tq, tq + "e", l) for l in region]
        decls.append("    int %se;" % tq)
    # `t = load; v1 = t; v2 = t; .. f(.., t)` in a try: the source's values are the frame copies (an iterator
    # returned, then passed by value): `v1 = load; v2 = v1; f(.., v2)`. A variable t would get its own home.
    changed = True
    while changed:
        changed = False
        for q, l in enumerate(region):
            m = re.match(r"^(\s+)(t\d+e?) = (\(int\)\(\*\(int\*\)[^;]*\));$", l)
            if not m or not pure(m.group(3)):
                continue
            tq = m.group(2)
            if re.search(r"\b%s\b" % tq, after) or len(re.findall(r"(?m)^\s+%s = " % tq, "\n".join(region))) != 1:
                continue
            j, copies = q + 1, []
            while j < len(region) and re.match(r"^%s(v_l\d+) = \(int\)\(%s\);$" % (re.escape(m.group(1)), tq), region[j]):
                copies.append(re.match(r"^\s+(v_l\d+)", region[j]).group(1))
                j += 1
            if not copies:
                continue
            new = [m.group(1) + "%s = %s;" % (copies[0], m.group(3))] + \
                [m.group(1) + "%s = (int)(%s);" % (c, p_) for p_, c in zip(copies, copies[1:])]
            region = region[:q] + new + [re.sub(r"\b%s\b" % tq, copies[-1], x) for x in region[j:]]
            decls = [d for d in decls if not re.search(r" %s;$" % tq, d)]
            changed = True
            break
    stmts = stmts[:k0 + 1] + region + stmts[k1:]
    stmts, decls = _inline_single(stmts, decls)
    marks = [k for k, l in enumerate(stmts) if re.match(r"^\s*__EH\(-?\d+\);$", l)]
    k0, k1 = marks
    inner = ["    " + l for l in stmts[k0 + 1:k1]]
    # the handler reads these frame slots: the variables whose memory homes they are (EH internals excluded)
    internal = {lf.eh["node"] + d for d in (-4, 0, 4, 8, 12)}
    alltext = "\n".join(stmts + decls)
    names, own = [], []
    for off in hd["reads"]:
        if off in internal:
            continue
        nm = "a%d" % off if off >= 4 else "v_l%d" % -off
        if off == lf.eh.get("self_spill"):
            names.append("(int)(self)")
        elif nm in params or re.search(r"\b%s\b" % nm, alltext):
            names.append("(int)(%s)" % nm)
        elif off < 0:
            own.append(nm)                   # a slot only the handler uses: the catch block's own local
            names.append("(int)&%s" % nm)
        else:
            raise Unsupported("EH: the catch reads an argument slot %d" % off)
    use = ["        int %s;" % n for n in own] + \
        (["        __lift_catch_use(%s);" % ", ".join(names)] if names else [])
    handler = use + (["        throw;"] if hd["kind"] == "rethrow" else ["    " + l for l in catch_code])
    handler = [pad[4:] + h for h in handler]
    out = stmts[:k0] + [pad + "try {"] + inner + [pad + "} catch (...) {"] + handler + [pad + "}"] + stmts[k1 + 1:]
    em_head = head(em) + (["void __lift_catch_use(...);"] if names else [])
    sig = _eh_sig(lf, va, typed)
    text = "\n".join(decls + out)
    # named automatics are laid out in name order, the first name lowest (frame.md): name each frame variable so
    # the order of names is the order of the retail slots, lowest address first
    text = re.sub(r"\bv_l(\d+)\b", lambda m: "vf_%04d" % (5000 - int(m.group(1))), text)
    return "\n".join(em_head) + "\n\n" + sig + "{\n" + text + "\n}\n"
    sig = _eh_sig(lf, va, typed)
    return "\n".join(head(em)) + "\n\n" + sig + "{\n" + "\n".join(decls + out) + "\n}\n"


def _inline_single(stmts, decls):
    """A temporary written and then read once, by the next statement at the same depth: the source has the
    expression there (a temporary live in a try gets a memory home). A pure value goes into any use; a call only
    into a store of exactly its result."""
    changed = True
    while changed:
        changed = False
        text = "\n".join(stmts)
        for q in range(len(stmts) - 1):
            m = re.match(r"^(\s+)(t\d+e?) = (\(int\)[^;]*);$", stmts[q])
            if not m:
                continue
            tq = m.group(2)
            nxt = stmts[q + 1]
            if len(re.findall(r"\b%s\b" % tq, text)) != 2 + len(re.findall(r"(?m)^\s+int %s;$" % tq, text)) or \
                    len(re.findall(r"\b%s\b" % tq, nxt)) != 1 or len(nxt) - len(nxt.lstrip()) != len(m.group(1)):
                continue
            if pure(m.group(3)):
                if nxt.lstrip().startswith("*") and re.search(r"\b%s\b" % tq, nxt.split("=", 1)[0]):
                    continue
            elif not (nxt.lstrip().startswith("*") and re.search(r"= \(int\)\(%s\);$" % tq, nxt)):
                continue                     # a call: only into a store of exactly its result
            stmts[q + 1] = re.sub(r"\b%s\b" % tq, m.group(3), stmts[q + 1])
            stmts.pop(q)
            decls = [d for d in decls if not re.search(r" %s;$" % tq, d)]
            changed = True
            break
    return stmts, decls


def eh_local_inline(lf, em, va, params, typed, head, stmts, decls, marks, off):
    """A local whose constructor and destructor are inline (an RAII guard: `saved = x; x = v; .. x = saved;`).
    The code before state 0 is the constructor, the object's frame slots its members; the stores at the end
    that read what the constructor saved (after the -1 store if there is one, else the last statements before
    the return: cl drops the -1 store when nothing after can throw) are the destructor."""
    k0 = marks[0]
    ctor = [l for l in stmts[:k0] if l.strip()]
    if not ctor or any(not l.startswith("    ") or l.startswith("     ") for l in ctor):
        raise Unsupported("EH: an inline local's construction is not straight-line")
    above = sorted(o for o in lf.frame_offsets() if o > off) + [lf.eh["node"]]
    size = min(o for o in above if o > off) - off
    fields = sorted({-int(n) for n in re.findall(r"\bv_l(\d+)\b", "\n".join(stmts)) if off <= -int(n) < off + size})
    if not fields or any((f - off) % 4 for f in fields):
        raise Unsupported("EH: an inline local's members")
    saved = {}                                # constructor temporary -> the member it was stored in
    for l in ctor:
        m = re.match(r"^    v_l(\d+) = \(int\)\((t\d+)\);$", l)
        if m:
            saved[m.group(2)] = -int(m.group(1))
    rest = stmts[k0 + 1:]
    if len(marks) > 1:
        k1 = marks[1] - k0 - 1
        tail = [l for l in rest[k1 + 1:] if l.strip()]
        body = rest[:k1]
        dtor = [l for l in tail if not re.match(r"^    return\b", l)]
        ret = [l for l in tail if re.match(r"^    return\b", l)]
    else:
        if not rest or not re.match(r"^    return\b", rest[-1]):
            raise Unsupported("EH: an inline local without a final return")
        ret, body, dtor = [rest[-1]], rest[:-1], []
        refs = set(saved) | {"v_l%d" % -f for f in fields}
        while body and body[-1].startswith("    *") and not body[-1].startswith("     ") and \
                any(re.search(r"\b%s\b" % x, body[-1].split("=", 1)[1]) for x in refs) and \
                not PURE_CALL.search(body[-1].split("=", 1)[1]):
            dtor.insert(0, body.pop())
    if not dtor:
        raise Unsupported("EH: an inline local's destruction not found")
    mem = lambda f: "m%d" % (f - off)
    # the destructor's own temporaries: fresh names, declared in it
    dt = sorted(set(re.findall(r"\b(t\d+)\b", "\n".join(dtor))) - set(saved))
    dtor = [re.sub(r"\b(%s)\b" % "|".join(dt), r"\1d", l) for l in dtor] if dt else dtor
    ddecls = ["        int %sd;" % t for t in dt]
    def to_member(l):
        for t, f in saved.items():
            l = re.sub(r"\b%s\b" % t, mem(f), l)
        return re.sub(r"\bv_l(\d+)\b", lambda m: mem(-int(m.group(1))) if off <= -int(m.group(1)) < off + size
                      else m.group(0), l)
    ctemps = set(re.findall(r"\b(t\d+)\b", "\n".join(ctor)))
    outside = "\n".join(body + ret)
    if any(re.search(r"\b%s\b" % t, outside) for t in ctemps) or re.search(r"\b(a\d+|self)\b", "\n".join(ctor + dtor)):
        raise Unsupported("EH: an inline local's code reads the function's values")
    cdecls = ["        int %s;" % t for t in sorted(ctemps - set(saved))] if ctemps - set(saved) else []
    cls = "L_%08x" % va
    members = []
    for q in range(0, size, 4):
        members.append("int %s;" % mem(off + q))
    sub = "struct %s {\n    %s\n    __forceinline %s() {\n%s\n    }\n    __forceinline ~%s() {\n%s\n    }\n};" % (
        cls, " ".join(members), cls, "\n".join(cdecls + ["    " + to_member(l) for l in ctor if not
                                                         re.match(r"^    v_l\d+ = \(int\)\(t\d+\);$", l) or True]),
        cls, "\n".join(ddecls + ["    " + to_member(l) for l in dtor]))
    obj = "v_l%d" % -off
    body = [re.sub(r"\bv_l(\d+)\b", lambda m: "%s.%s" % (obj, mem(-int(m.group(1)))) if off <= -int(m.group(1)) <
                   off + size else m.group(0), l) for l in body + ret]
    used = set(re.findall(r"\b(t\d+)\b", "\n".join(body)))
    decls = [d for d in decls if (DECL.match(d).group(1) in used) or (DECL.match(d).group(1).startswith("v_l") and
             not off <= -int(DECL.match(d).group(1)[3:]) < off + size)]
    sig = _eh_sig(lf, va, typed)
    return "\n".join(head(em) + [sub]) + "\n\n" + sig + "{\n" + "\n".join(decls + ["    %s %s;" % (cls, obj)] + body) + \
        "\n}\n"


def eh_placement(lf, em, va, params, typed, head):
    """One state on a placement delete (`push where; push p; call D`): `[state 0] if (p) p->ctor(args);` is
    `new ((void*)p) P(args)`, P with an inline placement operator new (throw() when cl null-checks the result)
    and an out-of-line placement operator delete (the funclet's callee: cl calls it if the constructor
    throws)."""
    st = lf.eh["states"]
    if len(st) != 1:
        raise Unsupported("EH: %d placement states" % len(st))
    body = drop_dead(em.body())
    lines = body.split("\n")[1:-1]
    decls = [l for l in lines if DECL.match(l)]
    stmts = eh_tidy([l for l in lines if not DECL.match(l)])
    stmts, decls = _params_for_copies(stmts, decls)
    marks = [k for k, l in enumerate(stmts) if re.match(r"^\s*__EH\(-?\d+\);$", l)]
    if not marks or stmts[marks[0]].strip() != "__EH(0);" or \
            [stmts[k].strip() for k in marks[1:]] not in ([], ["__EH(-1);"]):
        raise Unsupported("EH: placement state sequence")
    k0 = marks[0]
    I = re.match(r"^(\s*)", stmts[k0]).group(1)
    CT = r"^%s\(\(C_(?P<cv>[0-9a-f]{8})\*\)\((?P<p>t\d+|a\d+|\(int\)\(a\d+\))\)\)->FUN_(?P=cv)\((?P<args>[^;]*)\);$" % I
    nxt = stmts[k0 + 1:k0 + 4]
    m0 = re.match(r"^%sif \((t\d+|a\d+|\(int\)\(a\d+\)) == \(int\)0\) goto (L\w+);$" % I, nxt[0]) if nxt else None
    m1 = re.match(r"^%sif \((t\d+|a\d+|\(int\)\(a\d+\)) != \(int\)0\) \{$" % I, nxt[0]) if nxt else None
    if m0 and len(nxt) > 1 and re.match(CT, nxt[1]) and re.match(CT, nxt[1]).group("p") == m0.group(1):
        c, n_used, check = re.match(CT, nxt[1]), 2, True
    elif m1:
        # `if (p) { [t = p;] t->ctor(args); }`
        inner = stmts[k0 + 2:]
        cp = re.match(r"^%s    (t\d+) = \(int\)\(%s\);$" % (I, m1.group(1)), inner[0]) if inner else None
        ctl = inner[1] if cp else inner[0] if inner else ""
        c = re.match(CT.replace("^%s" % I, "^%s    " % I, 1), ctl)
        n_used = (3 if cp else 2) + 1
        if not c or c.group("p") not in (m1.group(1), cp.group(1) if cp else None) or \
                stmts[k0 + n_used] != I + "}":
            raise Unsupported("EH: placement shape")
        check = True
        if cp:                               # write it on p itself
            c = re.match(CT.replace("^%s" % I, "^%s    " % I, 1), ctl.replace("(%s)" % cp.group(1), "(%s)" % m1.group(1)))
    elif nxt and re.match(CT, nxt[0]):
        c, n_used, check = re.match(CT, nxt[0]), 1, False
    else:
        raise Unsupported("EH: placement shape")
    args = c.group("args")
    if re.search(r"\bt\d+\b", args):
        raise Unsupported("EH: placement arguments read temporaries")
    nargs = len([a for a in re.split(r",\s*(?![^()]*\))", args) if a.strip()]) if args.strip() else 0
    pass                                     # (an unused callee declaration is harmless)
    cls = "P_%08x" % va
    sub = "struct %s { int pad[1]; %s(%s); __forceinline static void* operator new(unsigned int, void* w)%s " \
          "{ return w; } static void operator delete(void*, void*); };" % (
              cls, cls, ", ".join(["unsigned int"] * nargs), " throw()" if check else "")
    out = stmts[:k0] + [I + "new ((void*)(%s)) %s(%s);" % (c.group("p"), cls, args)] + \
        [l for l in stmts[k0 + 1 + n_used:] if l.strip() != "__EH(-1);"]
    sig = _eh_sig(lf, va, typed)
    used = set(re.findall(r"\b(t\d+)\b", "\n".join(out)))
    decls = [d for d in decls if DECL.match(d).group(1) in used or not DECL.match(d).group(1).startswith("t")]
    return "\n".join(head(em) + [sub]) + "\n\n" + sig + "{\n" + "\n".join(decls + out) + "\n}\n"


def _params_for_copies(stmts, decls):
    """`t = aN;` with aN never read again: t is the parameter (the source's loop advances the parameter; a
    separate variable gets its own home where EH keeps values in memory)."""
    text = "\n".join(stmts)
    for m in list(re.finditer(r"(?m)^\s+(t\d+) = \(int\)\(\(int\)\((a\d+)\)\);$", text)):
        t, a = m.group(1), m.group(2)
        if len(re.findall(r"\b%s\b" % a, text)) != 1 or re.search(r"\b%s\b" % a, "\n".join(decls)):
            continue
        stmts = [x for x in stmts if x.strip() != m.group(0).strip()]
        stmts = [re.sub(r"\b%s\b" % t, a, x) for x in stmts]
        decls = [d for d in decls if not re.search(r" %s;$" % t, d)]
        text = "\n".join(stmts)
    return stmts, decls


def _eh_sig(lf, va, typed):
    """A plain function's signature in an EH unit: C++ linkage (an extern "C" function is nothrow under /EHsc)."""
    ret = "float" if lf.float_ret else "bool" if (lf.value_rets and lf.byte_rets == lf.value_rets) else \
        "int" if lf.value_ret else "void"
    if any(re.search(r"\b_this:", l) for l in lf.lines):
        return "%s __fastcall FUN_%08x(%s)\n" % (ret, va, ", ".join(["char* self", "int edx_unused"] + typed))
    return "%s %s FUN_%08x(%s)\n" % (ret, "__stdcall" if lf.ret_pop else "__cdecl", va, ", ".join(typed) or "void")


def unit(img, va, size, mask=None, flags=None, partial=None):
    """(C++ text, lifter) for one function: lift2il, then il2c's emitter with the names and signature above.
    mask: the byte offsets retail relocates inside the function (a corpus row's `mask`), to tell addresses from
    numbers; None reads the image's relocation table."""
    if partial is None:
        partial = os.environ.get("L2C_PARTIAL") == "1"
    lf = lift2il.Lifter(img, va, size, mask, partial=partial)
    il = lf.run()
    this = any(re.search(r"\b_this:", l) for l in il)
    nargs = max([(o - 4) // 4 + 1 for o in lf.args_read] + [lf.ret_pop // 4])
    params = ["a%d" % (4 + 4 * k) for k in range(nargs)]
    ret = "float" if lf.float_ret else "bool" if (lf.value_rets and lf.byte_rets == lf.value_rets) else \
        "int" if lf.value_ret else "void"
    lang_c = bool(flags) and "/TC" in flags.split()
    this_alias = set()
    if lf.eh:
        if lang_c:
            raise Unsupported("EH frame in a C unit")
        il, this_alias = alias_this(il)
    em = LiftEmitter(il, params, ret, set(), {})
    em.lang_c = lang_c
    em.this_alias = this_alias
    em.partial = partial
    em.self_va = va
    em.ptypes = {p: CTYPE.get(lf.arg_types.get(int(p[1:]), "1004"), "int") for p in params}
    typed0 = ["%s %s" % (CTYPE.get(lf.arg_types.get(int(p[1:]), "1004"), "int") if
                         lf.arg_types.get(int(p[1:]), "1004")[0] in "45e" else "int", p) for p in params]
    if lf.eh:
        try:
            return eh_unit(lf, em, va, params, typed0, lambda e: heads(e, False, cxx=True)), lf
        except Unsupported as ex:
            if not partial:
                raise
            em = LiftEmitter(il, params, ret, set(), {})   # partial: the plain body, EH shape as comments
            em.lang_c, em.this_alias, em.partial = lang_c, this_alias, True
            lf.eh_unsupported = str(ex)
    body = em.body()
    if partial:
        body = re.sub(r"(?m)^(\s*)__EH\((-?\d+)\);$", r"\1/* EH state \2 */", body).replace("__EHRESUME;", "/* catch resumes here */")
        if lf.eh_unsupported:
            body = body.replace("{", "{\n    /* lift2il: EH frame not reproduced: %s */" % lf.eh_unsupported.replace("*/", "* /"), 1)
    if os.environ.get("L2C_DROP_DEAD", "1") == "1":
        body = drop_dead(body)
    body = drop_unused_decls(size_addressed_locals(fold_aggregates(body, lf), lf))
    pty = lambda p: CTYPE.get(lf.arg_types.get(int(p[1:]), "1004"), "int")
    if pty("a4") is None:
        pass
    typed = ["%s %s" % (CTYPE.get(lf.arg_types.get(int(p[1:]), "1004"), "int") if
                        lf.arg_types.get(int(p[1:]), "1004")[0] in "45e" else "int", p) for p in params]
    if this:
        conv = "__fastcall"
        plist = ["%s self" % ("int" if getattr(em, "self_assigned", False) else "char*"), "int edx_unused"] + typed
    elif lf.ret_pop:
        conv, plist = "__stdcall", typed
    else:
        conv, plist = "__cdecl", typed
    head = heads(em, lang_c)
    ext = "" if lang_c else 'extern "C" '
    # 2. a path that returns nothing in a function that returns a value (a void tail call next to value rets)
    if ret != "void":
        body = re.sub(r"\breturn;", "return 0;", body)
    sig = '%s%s %s FUN_%08x(%s)\n' % (ext, ret if not (lang_c and ret == "bool") else "unsigned char", conv, va,
                                     ", ".join(plist) or "void")
    body = inline_x87(body)
    if lang_c:
        body = body.replace("(bool)", "(unsigned char)")
    elif ret == "bool":
        body = bool_flags(body)
    return "\n".join(head) + ("\n\n" if head else "") + sig + body + "\n", lf


def inline_x87(body):
    """An x87 value the lift held in a float temp (`t2 = (float)(a * b); t1007 = (int)(t2);`) that the code kept
    on the FPU stack: the source had no float variable there, and the temp's store / reload (`fstp; fld`) is
    the rounding cl adds for one (M&B 00432da0; on 13.10 even a `(float)` cast of the value rounds it).
    One use: inlined past plain temp assignments. Two uses (`m = v < m ? m : v`, a max macro: M&B 0048e5d0):
    the expression at both, which cl merges again, when no call, label or loop falls between and its operands
    are not redefined."""
    lines = body.split("\n")
    for x in sorted(set(re.findall(r"(?m)^    (?:float|double) (t\d{1,3});$", body)), key=lambda t: int(t[1:])):
        occ = [n for n, l in enumerate(lines) if re.search(r"\b%s\b" % x, l)]
        if len(occ) not in (3, 4):
            continue
        a, us = occ[1], occ[2:]
        m = re.match(r"^(\s*)%s = \((?:float|double)\)(\(.*\));$" % x, lines[a])
        if not m or re.search(r"\b(FUN_|__lift|memcpy|memset|strlen)\w*\(|->", m.group(2)):
            continue
        mid = lines[a + 1:us[-1]]
        if len(us) == 1:
            if any(not re.match(r"^\s*t\d+ = \(\w+\)\([^;]*\);$", l) or re.search(r"FUN_|->|__lift|\bg_\w+\)\(", l)
                   for l in mid):
                continue
        elif any(re.search(r"FUN_|->|__lift|\bg_\w+\)\(|memcpy|memset|strlen|^\s*L\w+:|\b(while|do|for|goto)\b", l)
                 for l in mid):
            continue
        names = set(re.findall(r"\b(t\d+|a\d+|v_l\d+|self)\b", m.group(2)))      # not redefined in between
        if names and any(re.match(r"^\s*(%s) = " % "|".join(names), l) for l in mid):
            continue
        if any(len(re.findall(r"\b%s\b" % x, lines[u])) != 1 or re.match(r"^\s*%s = " % x, lines[u]) for u in us):
            continue                                   # (a second assignment is no use)
        for u in us:
            lines[u] = re.sub(r"\b%s\b" % x, lambda _m: m.group(2), lines[u])
        del lines[a]
        lines = [l for l in lines if l.strip() not in ("float %s;" % x, "double %s;" % x)]
    return "\n".join(lines)


def bool_flags(body):
    """A byte variable that holds only 0 and 1 and is only returned (`mov al, 1 ... xor al, al ... ret` from a bool
    function): the source's `return true/false` through one register. As unsigned char, `return (bool)(x)` costs
    cl a `test; setne` (M&B 0049cee0); as bool it is free."""
    for x in set(re.findall(r"\breturn \(bool\)\((t\d+|v_l\d+)\);", body)):
        uses = re.findall(r"[^\n]*\b%s\b[^\n]*" % x, body)
        dec = "    unsigned char %s;" % x
        sets = re.compile(r"^\s*%s = \(unsigned char\)\(\(int\)([01])\);$" % x)
        if all(u == dec or sets.match(u) or u.strip() == "return (bool)(%s);" % x for u in uses) and dec in uses:
            body = body.replace(dec, "    bool %s;" % x)
            body = re.sub(r"\b%s = \(unsigned char\)\(\(int\)([01])\);" % x,
                          lambda m: "%s = %s;" % (x, "true" if m.group(1) == "1" else "false"), body)
    return body


INTRIN = {
    "_InterlockedExchangeAdd": 'extern "C" long __cdecl _InterlockedExchangeAdd(long volatile*, long);\n'
                               "#pragma intrinsic(_InterlockedExchangeAdd)",
    "_InterlockedExchange": 'extern "C" long __cdecl _InterlockedExchange(long volatile*, long);\n'
                            "#pragma intrinsic(_InterlockedExchange)",
    "__rdtsc": 'extern "C" unsigned __int64 __rdtsc(void);\n#pragma intrinsic(__rdtsc)',
    "_InterlockedIncrement": 'extern "C" long __cdecl _InterlockedIncrement(long volatile*);\n'
                             "#pragma intrinsic(_InterlockedIncrement)",
    "_InterlockedDecrement": 'extern "C" long __cdecl _InterlockedDecrement(long volatile*);\n'
                             "#pragma intrinsic(_InterlockedDecrement)"}


def heads(em, lang_c, cxx=False):
    """The declarations a unit needs: headers, il2c's prelude, the globals, the callees. cxx: callees get C++
    linkage (in an EH unit an extern "C" callee is assumed not to throw, and cl would drop the state stores)."""
    head = (["#include <string.h>"] if em.needs_string else []) + (["#include <math.h>"] if em.needs_math else []) + \
        ["".join(em.prelude)] * bool(em.prelude) + \
        [INTRIN[f] for f in sorted(em.intrin)] + \
        ["extern __declspec(thread) int tls_%x;" % o for o in sorted(em.tls)] + \
        ['extern "C" unsigned char g_%08x[];' % g for g in sorted(em.gl)]
    ext = "" if lang_c or cxx else 'extern "C" '
    for cva, c in sorted(em.calls.items()):
        if cva == getattr(em, "self_va", None) and not c["this_sigs"]:
            continue                             # a recursive call: the definition declares it
        r = c.get("fret") or ("int" if c["ret"] else "void")
        if c["this_sigs"]:
            nr = "__declspec(noreturn) " if c.get("noreturn") else ""
            mem = " ".join("%s%s FUN_%08x(%s);" % (nr, r, cva, ", ".join(sig)) for sig in sorted(c["this_sigs"]))
            head.append("struct C_%08x { %s };" % (cva, mem))
        if (c["sigs"] or not c["this_sigs"]) and cva != getattr(em, "self_va", None):
            sigs = sorted(c["sigs"])
            if len(sigs) > 1 and any(t != "unsigned int" for sg in sigs for t in sg):
                if not getattr(em, "partial", False):
                    raise Unsupported("callee FUN_%08x called with different float signatures" % cva)
                sigs = [()]                          # partial: declared variadic (`...`)
            cc = "__cdecl"
            if c.get("std"):                         # the callee pops: __stdcall, one fixed signature
                if len(sigs) != 1:
                    raise Unsupported("__stdcall callee FUN_%08x with two signatures" % cva)
                cc, plist_c = "__stdcall", ", ".join(sigs[0])
            elif len(sigs) == 1 and any(t != "unsigned int" for t in sigs[0]):
                plist_c = ", ".join(sigs[0])         # fixed: a float argument must not be promoted
            else:
                plist_c = "..." if not lang_c else ""
            head.append('%s%s%s %s FUN_%08x(%s);' % (ext, "__declspec(noreturn) " if c.get("noreturn") else "",
                                                      r, cc, cva, plist_c))
    return [h.replace('extern "C" unsigned char', 'extern unsigned char') if lang_c else h for h in head]


def outline(img, va, size, mask=None):
    """A big function's shape without C++: per basic block its address, size, memory reads / writes, calls (by
    callee: FUN_<va>, an import slot, a vtable slot), and how it ends (the compare and the branch targets, or a
    return); loop heads (blocks a later block jumps back to) and the EH frame. For rows too big to read as code."""
    lf = lift2il.Lifter(img, va, size, mask, partial=True)
    lf.blocks()
    ins, md = lf.ins, lf.md
    out = ["outline of FUN_%08x: %d bytes, %d instructions, %d blocks" % (va, size, len(ins), len(lf.bl))]
    if lf.eh:
        out.append("EH frame: %d states (%s)%s" % (len(lf.eh["states"]), ", ".join(sorted({x["kind"] for x in
                   lf.eh["states"]})), "; %d try blocks" % len(lf.eh["tries"]) if lf.eh["tries"] else ""))
    starts = [ins[b[0]].address for b in lf.bl]
    heads = set()
    for n, ss in lf.succ.items():
        for t in ss:
            if starts[t] <= starts[n]:
                heads.add(t)
    for n, b in enumerate(lf.bl):
        a0, last = ins[b[0]].address, ins[b[-1]]
        nbytes = last.address + last.size - a0
        rd = wr = 0
        calls = []
        for k in b:
            i = ins[k]
            for q, o in enumerate(i.operands):
                if o.type == lift2il.X.X86_OP_MEM and i.mnemonic not in ("lea", "nop") and \
                        not (o.mem.base and md.reg_name(o.mem.base) == "esp" and i.mnemonic in ("push", "pop")):
                    if q == 0 and ((i.mnemonic not in ("cmp", "test", "push", "call", "jmp") and len(i.operands) > 1)
                                   or i.mnemonic.startswith(("fst", "fist", "fbstp"))):
                        wr += 1
                    else:
                        rd += 1
            if i.mnemonic == "call":
                o = i.operands[0]
                if o.type == lift2il.X.X86_OP_IMM:
                    calls.append("FUN_%08x" % (o.imm & 0xFFFFFFFF))
                elif o.type == lift2il.X.X86_OP_MEM and not o.mem.base and not o.mem.index:
                    calls.append("[import %08x]" % (o.mem.disp & 0xFFFFFFFF))
                elif o.type == lift2il.X.X86_OP_MEM:
                    calls.append("virtual slot %d" % (o.mem.disp // 4))
                else:
                    calls.append("through %s" % md.reg_name(o.reg))
        m = last.mnemonic
        if m == "ret":
            end = "return"
        elif m == "jmp":
            end = "goto %x" % last.operands[0].imm if last.operands[0].type == lift2il.X.X86_OP_IMM else \
                ("switch" if last.address in lf.switches else "jmp %s" % last.op_str)
        elif m.startswith("j"):
            cmp = next((ins[k] for k in reversed(b[:-1]) if ins[k].mnemonic in ("cmp", "test")), None)
            end = "if %s(%s) goto %x else %x" % (m[1:], "%s %s" % (cmp.mnemonic, cmp.op_str) if cmp else "flags",
                                                 last.operands[0].imm, last.address + last.size)
        else:
            end = "falls into %x" % (last.address + last.size)
        out.append("%s%x  %4d B %3d ins  rd %-2d wr %-2d %s-> %s" % (
            "loop " if n in heads else "     ", a0, nbytes, len(b), rd, wr,
            ("calls " + ", ".join(calls) + " ") if calls else "", end))
    return "\n".join(out)


def _check_one(args):
    """Lift, emit, compile at the corpus flags and score with lbench's CI scorer (read-only use of lbench)."""
    path, head, r, outdir = args
    os.environ["C2_BUILD"] = head["c2"]
    import corpus as corpusmod, lbench, w8_score
    from w8_emit import Image
    res = dict(va=r["va"], size=r["size"])
    try:
        text, _ = unit(Image(head["image"]), int(r["va"], 16), r["size"], corpusmod.unpack(r["mask"]),
                       r.get("flags") or head["flags"])
    except (Unsupported, lift2il.Unsupported) as e:
        res["status"] = "UNSUPPORTED"
        res["why"] = str(e)[:100]
        return res
    except Exception as e:
        res["status"] = "ERROR"
        res["why"] = ("%s: %s" % (type(e).__name__, e))[:120]
        return res
    src = os.path.join(outdir, "%s.cpp" % r["va"])
    open(src, "w").write(text)
    flags = (r.get("flags") or head["flags"]) + " /Z7"          # a corpus row may carry its own flags (Spore)
    obj, log = w8_score.compile_one((src, flags, "l2c_%s_%s" % (head["corpus"], r["va"])))
    if not obj:
        res["status"] = "CL-ERROR"
        res["why"] = next((l.strip() for l in log.splitlines() if " error " in l), log.strip()[-100:])[:140]
        return res
    img = corpusmod.Corpus(path).image()
    import w8_diff
    w8_diff.SIZE_HINT[int(r["va"], 16)] = r["size"]
    res.update(lbench.score(obj, r, img))
    return res


def lift_main(argv):
    """`il2c_lift.py lift BINARY VA --size N`: lift one function of any x86 PE to C++ (no corpus needed)."""
    import argparse, importlib
    ap = argparse.ArgumentParser(prog="il2c_lift.py lift", description="lift one function of an x86 PE "
                                 "(32-bit MSVC 7.1 / 8 / 9 output) to C++ that cl should compile back to its bytes")
    ap.add_argument("binary", help="the PE (exe or dll)")
    ap.add_argument("va", help="the function's address, hex")
    ap.add_argument("--size", required=True, type=lambda x: int(x, 0), help="the function's size in bytes")
    ap.add_argument("--c2", help="the c2 build the binary came from: 13.10.3052 / 13.10.3077 (VS2003), "
                    "14.00 (VS2005), 15.00 (VS2008); sets the IL float types (or set C2_BUILD)")
    ap.add_argument("--partial", action="store_true", help="emit what lifts, with a comment at each unsupported "
                    "construct (a structural draft of a big function)")
    ap.add_argument("--outline", action="store_true", help="one line per basic block instead of C++")
    a = ap.parse_args(argv)
    if a.c2:
        os.environ["C2_BUILD"] = a.c2
        importlib.reload(lift2il)
    from w8_emit import Image
    img, va = Image(a.binary), int(a.va, 16)
    if a.outline:
        print(outline(img, va, a.size))
        return 0
    try:
        text, _ = unit(img, va, a.size, None, partial=a.partial)
    except (lift2il.Unsupported, Unsupported) as ex:
        sys.exit("not lifted: %s (--partial drafts around it)" % ex)
    sys.stdout.write(text)
    return 0


def main(argv):
    if argv and argv[0] == "lift":
        return lift_main(argv[1:])
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("unit", "check", "outline"))
    ap.add_argument("corpus")
    ap.add_argument("va", nargs="?")
    ap.add_argument("--sample", type=int, default=100)
    ap.add_argument("--max-size", type=int, default=256)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--json")
    ap.add_argument("--partial", action="store_true", help="unit: emit what lifts, with a comment at each "
                    "unsupported construct (a structural draft of a big function); never scored as a match")
    a = ap.parse_args(argv)
    from w8_emit import Image
    import corpus as corpusmod
    f = open(a.corpus)
    head = json.loads(f.readline())
    rows = [json.loads(l) for l in f]
    if a.cmd == "outline":
        f = open(a.corpus)
        head = json.loads(f.readline())
        r = next(json.loads(l) for l in f if json.loads(l)["va"] == a.va)
        print(outline(Image(head["image"]), int(a.va, 16), r["size"], corpusmod.unpack(r["mask"])))
        return 0
    if a.cmd == "unit":
        va = a.va.lower().replace("0x", "").zfill(8)
        r = next(x for x in rows if x["va"] == va)
        text, _ = unit(Image(head["image"]), int(r["va"], 16), r["size"], corpusmod.unpack(r["mask"]),
                       partial=a.partial or None)
        sys.stdout.write(text)
        return 0
    from concurrent.futures import ProcessPoolExecutor
    pool = [r for r in rows if r.get("status") == "matched" and r["size"] <= a.max_size]
    step = max(1, len(pool) // a.sample)
    pick = pool[::step][:a.sample]
    outdir = os.path.join(os.path.dirname(HERE), "work", "scratch", "l2c_%s" % head["corpus"])
    os.makedirs(outdir, exist_ok=True)
    with ProcessPoolExecutor(a.jobs) as ex:
        res = list(ex.map(_check_one, [(a.corpus, head, r, outdir) for r in pick]))
    st = collections.Counter(r["status"] for r in res)
    print("%d functions: %s" % (len(res), dict(st)))
    why = collections.Counter(r.get("why", "")[:60] for r in res if r["status"] in ("UNSUPPORTED", "CL-ERROR", "ERROR"))
    for w, n in why.most_common(14):
        print("  %3d  %s" % (n, w))
    d = [r for r in res if r["status"] == "DIFF"]
    if d:
        print("DIFF: median ins %.0f%%, classes %s" % (sorted(r["ins"] for r in d)[len(d) // 2],
                                                      dict(collections.Counter(r.get("cls") for r in d))))
    if a.json:
        with open(a.json, "w") as fh:
            for r in res:
                fh.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
