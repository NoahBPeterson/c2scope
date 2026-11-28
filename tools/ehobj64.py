#!/usr/bin/env python3
"""EH-object lifting: rebuild the objects whose destructors the executable's unwind tables run, instead of wrapping the
lifted body in try/catch (which can't give cl's state machine).

Constructor mode (ctor_text): every EH state of the function destroys `this+off` (ehinfo64 "home 0"). The function is
a constructor; each such state opens right after the call that built the member at this+off. The lifted body is
turned into

    struct M<off>_<va>_FUN_<dtor> { <bytes>; M..(args) { FUN_<ctor>((__int64)this, args); } ~M..(); };
    struct C_<va> { <members at their offsets>; C_<va>(params); };
    C_<va>::C_<va>(params) : m<off>(args), f<off>(value), ... { <the rest of the lifted body> }

The member's inline constructor forwards to the call the original makes (that gives cl's rcx-before-rdx order, as hand-written
forwarding layers do); its destructor is only declared, and the FUN_ placeholder in the class name binds the
unwind funclet's call to the original destructor. Scalar stores to this before the last member are member
initializers.

    from ehobj64 import ctor_text
    text = ctor_text(img, va, size)        # None when the function is not this shape

usage: ehobj64.py VA [VA ...]              print the constructor-mode text
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ehinfo64 as E
import lift2il64 as L


_LAST = {}


class NotShape(Exception):
    pass


def split_args(s):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


CAST = re.compile(r"\((?:unsigned )?(?:__int64|int|short|char|unsigned)\)")


def strip(e):
    """An expression without its integer casts and outer parentheses."""
    e = CAST.sub("", e).strip()
    while e.startswith("(") and e.endswith(")"):
        depth, ok = 0, True
        for k, ch in enumerate(e):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and k < len(e) - 1:
                ok = False
                break
        if not ok:
            break
        e = e[1:-1].strip()
    return e


def alias_of(e, al):
    """(offset) when e is this + constant, through the this-aliases in al."""
    s = strip(e)
    m = re.fullmatch(r"(\w+)", s)
    if m and m.group(1) in al:
        return al[m.group(1)]
    m = re.fullmatch(r"(\w+)\s*\+\s*\(?(-?\d+)\)?", s)
    if m and m.group(1) in al:
        return al[m.group(1)] + int(m.group(2))
    return None


def subst(e, env, al):
    """e with the init-region temps replaced by their values and this-aliases by this + offset."""
    def rep(m):
        w = m.group(0)
        if w in al:
            return "((__int64)this + %d)" % al[w] if al[w] else "((__int64)this)"
        if w in env:
            return "(" + env[w] + ")"
        return w
    for _ in range(6):
        n = re.sub(r"\b[atv]\d+\b|\bv_\w+\b", rep, e)
        if n == e:
            break
        e = n
    return e


class _Store:
    """*(T*)((char*)(BASE) + OFF) = VAL;  ->  groups ty, base, off, val (a balanced-parenthesis parse)."""
    def __init__(self, ty, base, off, val):
        self.g = dict(ty=ty, base=base, off=off, val=val)

    def group(self, k):
        return self.g[k]


class STORE:
    @staticmethod
    def match(s):
        m = re.match(r"^\*\(([\w ]+)\*\)\(\(char\*\)", s)
        if not m or not s.endswith(";"):
            return None
        k = m.end()
        if k >= len(s) or s[k] != "(":
            return None
        depth = 0
        for j in range(k, len(s)):
            depth += s[j] == "("
            depth -= s[j] == ")"
            if depth == 0:
                break
        base = s[k:j + 1]
        rest = s[j + 1:]
        mm = re.match(r"^(?: \+ (\d+))?\) = (.*);$", rest)
        if not mm:
            return None
        return _Store(m.group(1), base, mm.group(1), mm.group(2))


CALL = re.compile(r"^(?:(?P<dst>\w+) = \([\w ]+\))?(?P<fn>FUN_[0-9a-f]+)\((?P<args>.*)\);$")
ASSIGN = re.compile(r"^(?P<dst>\w+) = (?P<val>.*);$")


def ctor_text(img, va, size, opts=None):
    info = E.eh_info(img, va)
    if not info or info["tries"]:
        return None
    states = {}
    for s in info["states"]:
        c = s.get("cleanup")
        if not c or len(c["calls"]) != 1:
            return None
        r = c["calls"][0]["rcx"]
        if not r or r[0] != "home" or r[1] != 0:
            return None
        if r[2] in states:
            return None
        states[r[2]] = c["calls"][0]["target"]
    if not states:
        return None
    o = dict(opts or {}, callee_args=1)
    o.pop("eh", None)
    text, lf = L.unit(img, va, size, o)
    try:
        return build(text, va, states)
    except NotShape:
        return None


def build(text, va, states):
    head, _, rest = text.partition("\n\n")
    if not rest:
        head, rest = "", text
    sigline, body = rest.split("\n", 1)
    m = re.match(r"^(.*?) FUN_%x\((.*)\)$" % va, sigline)
    if not m:
        raise NotShape("signature")
    params = [p.strip() for p in m.group(2).split(",") if p.strip() and p.strip() != "void"]
    if not params or not params[0].endswith(" a0"):
        raise NotShape("no this")
    sigs = {}
    for ln in head.split("\n"):
        mm = re.match(r"^(.*?) (FUN_[0-9a-f]+)\((.*)\);$", ln)
        if mm:
            sigs.setdefault(mm.group(2), [t.strip() for t in mm.group(3).split(",") if t.strip()])
    lines = body.rstrip("\n").split("\n")
    assert lines[0] == "{" and lines[-1] == "}"
    lines = lines[1:-1]
    decls, stmts = [], []
    for ln in lines:
        if not stmts and re.match(r"^    [\w ]+\*? \w+(\[\d+\])?;$", ln):
            decls.append(ln)
        else:
            stmts.append(ln)
    # the init region: through the call that builds the last EH member
    al = {"a0": 0}
    env = {}
    members = {}                    # off -> dict(kind, ...)
    last_eh = max(states)
    k = 0
    built = set()
    while k < len(stmts):
        s = stmts[k].strip()
        k += 1
        if not s:
            continue
        done = built == set(states)
        top = max(members) if members else -1
        if done:                                 # more members while the statements still build them in order
            mc, ms = CALL.match(s), STORE.match(s)
            fits = False
            if mc and not mc.group("dst"):
                a0 = split_args(mc.group("args"))
                off = alias_of(a0[0], al) if a0 else None
                fits = off is not None and off > top
            elif ms:
                b = alias_of(ms.group("base"), al)
                fits = b is not None and b + int(ms.group("off") or 0) > top
            elif ASSIGN.match(s) and not CALL.match(s) and "FUN_" not in s and "))(" not in s:
                fits = True
            if not fits:
                k -= 1
                break
        if s.startswith(("if ", "if(", "}", "goto", "return", "else", "L_", "switch", "do ", "while")) or \
                s.endswith(":") or s.endswith("{"):
            raise NotShape("control flow before the members")
        mc = CALL.match(s)
        if mc:
            args = split_args(mc.group("args"))
            off = alias_of(args[0], al) if args else None
            if off is None or off in members:
                raise NotShape("call before the members that is not a member's constructor")
            fn = mc.group("fn")
            members[off] = dict(kind="eh" if off in states else "obj", fn=fn,
                                args=[subst(a, env, al) for a in args[1:]], ptys=sigs.get(fn, [])[1:],
                                order=len(members))
            if off in states:
                built.add(off)
            if mc.group("dst"):
                env[mc.group("dst")] = "0"
                raise NotShape("member constructor's value used")
            continue
        ms = STORE.match(s)
        if ms:
            base = alias_of(ms.group("base"), al)
            if base is None:
                raise NotShape("store through something other than this")
            off = base + int(ms.group("off") or 0)
            if off in members:
                raise NotShape("second store to a member")
            members[off] = dict(kind="scalar", ty=ms.group("ty").strip(), val=subst(ms.group("val"), env, al),
                                order=len(members))
            continue
        ma = ASSIGN.match(s)
        if ma:
            a = alias_of(ma.group("val"), al)
            if a is not None:
                al[ma.group("dst")] = a
            else:
                env[ma.group("dst")] = subst(ma.group("val"), env, al)
            continue
        raise NotShape("statement " + s[:60])
    if built != set(states):
        raise NotShape("member states not all built")
    offs = sorted(members)
    if [members[o]["order"] for o in offs] != list(range(len(offs))):
        raise NotShape("members not built in offset order")
    tail = stmts[k:]
    # the class
    out_types, fields, inits = [], [], []
    pos = 0
    for n, off in enumerate(offs):
        mb = members[off]
        nxt = offs[n + 1] if n + 1 < len(offs) else None
        if off > pos:
            fields.append("    unsigned char pad%x[%d];" % (pos, off - pos))
        if mb["kind"] == "scalar":
            sz = {"__int64": 8, "unsigned __int64": 8, "int": 4, "unsigned int": 4, "short": 2,
                  "unsigned short": 2, "char": 1, "unsigned char": 1, "float": 4, "double": 8}.get(mb["ty"], 8)
            if off % sz:
                raise NotShape("misaligned scalar")
            fields.append("    %s f%x;" % (mb["ty"], off))
            inits.append("f%x(%s)" % (off, mb["val"]))
            pos = off + sz
            continue
        sz = (nxt - off) if nxt is not None else 8
        tn = "M%x_%x" % (off, va) + ("_FUN_%x" % states[off][0] if False else "")
        if mb["kind"] == "eh":
            tn = "M%x_%x_FUN_%x" % (off, va, states[off])
        else:
            tn = "N%x_%x" % (off, va)
        store = "__int64 d[%d];" % (sz // 8) if off % 8 == 0 and sz % 8 == 0 else "unsigned char d[%d];" % sz
        pt = mb["ptys"] or ["__int64"] * len(mb["args"])
        if len(pt) != len(mb["args"]):
            pt = (pt + ["__int64"] * len(mb["args"]))[:len(mb["args"])]
        ps = ", ".join("%s p%d" % (t, j) for j, t in enumerate(pt))
        call = "%s(%s)" % (mb["fn"], ", ".join(["(__int64)this"] + ["p%d" % j for j in range(len(pt))]))
        decl = "struct %s { %s %s(%s) { %s; }%s };" % (tn, store, tn, ps, call,
                                                        (" ~%s();" % tn) if mb["kind"] == "eh" else "")
        out_types.append(decl)
        fields.append("    %s m%x;" % (tn, off))
        inits.append("m%x(%s)" % (off, ", ".join(mb["args"])))
        pos = off + sz
    cls = "C_%x" % va
    ps = ", ".join(params[1:])
    # the body after the members: this-aliases and init temps that it still reads
    used = set(re.findall(r"\b[atv]\d+\b|\bv_\w+\b", "\n".join(tail)))
    pre = []
    for name, off in al.items():
        if name in used:
            if name == "a0":
                pre.append("    __int64 a0 = (__int64)this%s;" % (" + %d" % off if off else ""))
            else:
                pre.append("    %s = (__int64)this%s;" % (name, " + %d" % off if off else ""))
    for name, val in env.items():
        if name in used:
            pre.append("    %s = %s;" % (name, val))
    for ln in tail:                                  # this-aliases made after the members
        ma = ASSIGN.match(ln.strip())
        if ma and alias_of(ma.group("val"), al) is not None:
            al.setdefault(ma.group("dst"), alias_of(ma.group("val"), al))
    body_lines = []
    for ln in tail:
        st = ln.strip()
        mr = re.match(r"^return(?: (.*))?;$", st)
        if mr:
            if mr.group(1) and alias_of(mr.group(1), al) != 0 and mr.group(1).strip() != "0":
                raise NotShape("returns something other than this")
            body_lines.append(ln[:len(ln) - len(ln.lstrip())] + "return;")
            continue
        body_lines.append(ln)
    while body_lines and body_lines[-1].strip() == "return;":
        body_lines.pop()
    keep_decls = [d for d in decls if re.search(r"\b(\w+);$", d).group(1) in used and
                  re.search(r"\b(\w+);$", d).group(1) != "a0"]
    parts = dict(end=pos, head=head, out_types=out_types, cls=cls, fields=fields, params=params[1:], inits=inits,
                 decls=keep_decls, pre=pre, body=body_lines, va=va)
    _LAST["parts"] = parts
    return render(parts)


def render(p, body=None, names=None, head=None):
    """The constructor text from build()'s parts; body/names/head: a tidied body, the tidied parameter names
    (a1.. -> name) and head."""
    names = names or {}
    ps = []
    for q in p["params"]:
        ty, nm = q.rsplit(" ", 1)
        ps.append("%s %s" % (ty, names.get(nm, nm)))
    ps = ", ".join(ps)
    inits = p["inits"]
    if names:
        inits = [re.sub(r"\b(a\d+)\b", lambda m: names.get(m.group(1), m.group(1)), x) for x in inits]
    src = [(head or p["head"]).rstrip(), ""] if (head or p["head"]).strip() else []
    src += p["out_types"]
    src.append("struct %s {" % p["cls"])
    src += p["fields"]
    src.append("    %s(%s);" % (p["cls"], ps))
    src.append("};")
    src.append("%s::%s(%s)" % (p["cls"], p["cls"], ps or "void"))
    src.append("    : " + ", ".join(inits))
    src.append("{")
    src += body if body is not None else p["decls"] + p["pre"] + p["body"]
    src.append("}")
    text = "\n".join(src) + "\n"
    if head is not None:
        text = re.sub(r"\bg_([0-9a-f]{9})\b", r"DAT_\1", text)
    return text


def unparen(e):
    """Drop parentheses around a lone name or number (not a call's)."""
    for _ in range(12):
        n = re.sub(r"(?<![\w\]])\(\s*([\w.]+)\s*\)", r"\1", e)
        if n == e:
            break
        e = n
    return e


def clean(e):
    """An initializer without its integer casts (the member constructor's parameter types convert anyway)."""
    if "this" in e:
        return e
    e = unparen(strip(e))
    e = re.sub(r"(?<![\w*])(?<!\(__int64\))\b(g_[0-9a-f]{9}|DAT_[0-9a-f]{9})\b", r"(__int64)\1", e)
    e = re.sub(r"\(\(+(\*\([\w ]+\*\)\w+)\)+\)", r"\1", e)
    return e


SZ = {"__int64": 8, "unsigned __int64": 8, "int": 4, "unsigned int": 4, "short": 2, "unsigned short": 2, "char": 1,
      "unsigned char": 1, "float": 4, "double": 8, "bool": 1}
FSTORE = re.compile(r"^(\s*)\*\(([\w ]+)\*\)\(\(char\*\)self \+ (0x[0-9a-f]+|\d+)\) = (.*);$")


def as_fields(p, bl):
    """Stores to self+off past the members become fields: `f3e0 = 0;`. Returns (fields, body)."""
    end = p["end"]
    seen = {}
    for ln in bl:
        m = FSTORE.match(ln)
        if m:
            off = int(m.group(3), 0)
            ty = m.group(2).strip()
            if ty not in SZ or off < end or off % SZ[ty] or seen.get(off, ty) != ty:
                return p["fields"], bl
            seen[off] = ty
    if not seen:
        return p["fields"], bl
    # every other mention of self + those offsets keeps the raw form: give up if any
    for ln in bl:
        if FSTORE.match(ln):
            continue
        for off in seen:
            if re.search(r"self \+ (?:0x%x|%d)\b" % (off, off), ln):
                return p["fields"], bl
    offs = sorted(seen)
    for a, b in zip(offs, offs[1:]):
        if a + SZ[seen[a]] > b:
            return p["fields"], bl
    fields = list(p["fields"])
    pos = end
    for off in offs:
        if off > pos:
            fields.append("    unsigned char pad%x[%d];" % (pos, off - pos))
        fields.append("    %s f%x;" % (seen[off], off))
        pos = off + SZ[seen[off]]
    out = []
    for ln in bl:
        m = FSTORE.match(ln)
        out.append("%sf%x = %s;" % (m.group(1), int(m.group(3), 0), m.group(4)) if m else ln)
    return fields, out


def tidied(p):
    """Constructor texts whose body after the members went through tidy64, most rewritten first."""
    import tidy64
    sig = ", ".join(["__int64 a0"] + p["params"])
    body = [d for d in p["decls"]] + [x.replace("(__int64)this", "a0") for x in p["pre"]] + p["body"]
    out = []
    inits_clean = dict(p, inits=[clean(x) for x in p["inits"]])
    if not any(ln.strip() for ln in p["body"]):
        return [render(inits_clean, body=[], head=p["head"]), render(p, body=[])]
    pseudo = (p["head"] + "\n\n" if p["head"].strip() else "") + "void FUN_%x(%s)\n{\n%s\n}\n" % (
        p["va"], sig, "\n".join(body))
    try:
        cands = tidy64.tidy(pseudo)
    except Exception:
        return []
    for _, t in cands:
        hd, _, rest = t.partition("\n\n")
        if not rest:
            hd, rest = "", t
        m = re.match(r"^void FUN_%x\((.*)\)\n\{\n(.*)\n\}\n?$" % p["va"], rest, re.S)
        if not m:
            continue
        pn = [x.strip().rsplit(" ", 1)[-1] for x in m.group(1).split(",")]
        if len(pn) != len(p["params"]) + 1:
            continue
        pn = [("arg%d" % j if n.startswith("unused") else n) for j, n in enumerate(pn)]
        names = {"a%d" % (j + 1): n for j, n in enumerate(pn[1:])}
        bl = m.group(2).split("\n")
        selfn = pn[0]
        if any(re.search(r"\b%s\b" % re.escape(selfn), ln) for ln in bl):
            bl = ["    __int64 self = (__int64)this;"] + [re.sub(r"\b%s\b" % re.escape(selfn), "self", ln) for ln in bl]
        bl = [ln for ln in bl if not re.match(r"^\s*return( 0)?;$", ln)]
        have = set(re.findall(r"\b(FUN_[0-9a-f]+|DAT_[0-9a-f]+)\b(?=[\[(]|\[\]|\()", hd))
        extra = [ln for ln in p["head"].split("\n") if ln.strip() and not any(h in ln for h in have)]
        extra = [re.sub(r"\bg_([0-9a-f]{9})\b", r"DAT_\1", ln) for ln in extra]
        h2 = "\n".join([ln for ln in hd.split("\n") if ln.strip()] + [x for x in extra if x not in hd])
        fl, bf = as_fields(p, bl)
        if bf is not bl:
            out.append(render(dict(inits_clean, fields=fl), body=bf, names=names, head=h2))
        out.append(render(inits_clean, body=bl, names=names, head=h2))
        out.append(render(p, body=bl, names=names, head=h2))
    return out


def ctor_texts(img, va, size, opts=None):
    """[text]: the tidied constructor texts first, the raw one last ([] when not this shape)."""
    raw = ctor_text(img, va, size, opts)
    if not raw:
        return []
    out = list(dict.fromkeys(tidied(_LAST["parts"]) + [raw]))
    return out

if __name__ == "__main__":
    img = L.Image()
    pd = E.pdata(img)
    for a in sys.argv[1:]:
        va = int(a, 16)
        ts = ctor_texts(img, va, pd[va][0] - va)
        print(ts[0] if ts else "0x%x: not constructor-shaped" % va)
