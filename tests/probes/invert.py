# Probe checks for docs/c2/invert.md (tools/invert/): the inverses of the compiler's rules.
# Exec'd by tests/run_tests.py (check, rule_compile, rule_proc, RKEY, HERE in scope).
import os as _os, re as _re, sys as _sys
_sys.path.insert(0, _os.path.join(HERE, "tools"))
from invert import odframe as _odf

print("invert.md probes (tools/invert)")

# 1. /Od frame: c1xx's block-scope hash walk (16 buckets, later declaration first in a bucket), no compile:
#    the orders RF_oddecl.cpp / RE_odframe.cpp measured.
check("invert odframe: hash walk gives RF_oddecl v4 / v5 / v6 and RE_odframe orders",
      (" ".join(_odf.scope_order("a b c d e f g h i j k l m n2 o r".split())),
       " ".join(_odf.scope_order("r o n2 m l k j i h g f e d c b a".split())),
       " ".join(_odf.scope_order("a b r d e f g h i j k l m n2 o c".split())),
       " ".join(_odf.scope_order("x y z".split())), " ".join(_odf.scope_order("q1 q2 q3 q4 q5".split()))),
      ("h a i f g b k l r d c n2 e o m j", "h a i f b g k l c d r e n2 j m o", "h a i f g b k l c d r n2 e o m j",
       "y x z", "q3 q2 q5 q4 q1"))

# the solver's layout (names, declaration order, filler for dead room, an 8-aligned array split) compiled at /Od
if RKEY != "13.00.9044":                     # VC6's front end orders /Od locals alphabetically (frame.py)
    _asm = rule_compile("RI_odsolve.cpp", "/Od")
    _got = dict((n, int(o)) for n, o in _re.findall(r"^_(\w+)\$ = (-?\d+)", _asm, _re.M))
    check("invert odframe: solved frame lands at the wanted offsets (RI_odsolve.cpp)", _got,
          {"q": -4, "c": -5, "arr_f": -24, "s_5": -28, "d": -40, "big": -136})

# a frame realigned to 16 is allocated in first-reference order (f1: VC's constructor references it at its
# declaration; f2: a 16-aligned POD); f3 without one keeps the hash walk
if RKEY != "13.00.9044":
    _asm = rule_compile("RI_odalign.cpp", "/Od", "/GS-")
    _fr = {}
    for _f in ("f1", "f2", "f3"):
        _m = _re.search(r"_TEXT\tSEGMENT\s*\n(.*?)^\?%s@@" % _f, _asm, _re.M | _re.S)
        _seg = _m.group(1).rsplit("SEGMENT", 1)[-1] if _m else ""
        _fr[_f] = " ".join(n for n, o in sorted(_re.findall(r"^_(\w+)\$ = (-?\d+)", _seg, _re.M), key=lambda t: -int(t[1])))
    check("invert odframe: realigned frames take first-reference order (RI_odalign.cpp f1 / f2 / f3)",
          (_fr["f1"], _fr["f2"], _fr["f3"]), ("ab ao a ad aa", "ao a ab ad aa", "aa ad a ao"))

# 2. commutative operands: the weight decides, a tie keeps the source order (symkeys.md 3)
from invert import operands as _ops, regorder as _ro, x87names as _x87, inlining as _inl
check("invert operands: free / write / keys / shape",
      (_ops.classify(("_a (key 2)", 0, 1, 0x40), ("_b (key 1)", 0, 1, 0x20)),
       _ops.classify(("t64", 8, 3, 0x4a), ("t65", 8, 3, 0x4a)),
       _ops.classify(("_b (key 1)", 0, 1, 0x20), ("_a (key 2)", 0, 1, 0x40)),
       _ops.classify(("iconst", 0, 0, 1), ("_a (key 2)", 0, 1, 0x40)),
       _ops.classify(("mem", 0, 1, 0x4008), ("(_a key 2 ..)", 1, 2, 0x73))),
      ("free", "write", "keys", "shape", "shape"))

# 3. callee-saved roles: equal priority -> definition order; a priority gap -> not an order fact
_pops = [(1, "a", 24, 23), (2, "b", 24, 19), (3, "c", 24, 15), (4, "d", 10, 9)]
_c = _ro.constraints(_pops, {"a": "esi", "b": "edi", "c": "ebx", "d": "ebp"},
                     {"c": "esi", "b": "edi", "a": "ebx", "d": "esi"})
check("invert regorder: c before a / b is a definition order, d before a / b a priority gap (d and c want one register: no order)",
      sorted((x["u"], x["v"], x["kind"]) for x in _c),
      sorted([("b", "a", "def"), ("c", "a", "def"), ("c", "b", "def"), ("d", "a", "prio"), ("d", "b", "prio")]))

# 4. x87 rounding points: the property each one needs, per build
class _P:
    def __init__(self, kind="rt", calls=(), depth=2, ret=False, feeds=False):
        self.kind, self.calls, self.depth, self.ret, self.feeds_arg = kind, list(calls), depth, ret, feeds
check("invert x87names: argument / return / call / pressure / named (15.00) / member (13.10)",
      (_x87.require(_P("arg"), "15.00"), _x87.require(_P(ret=True), "15.00"), _x87.require(_P(calls=[1]), "13.10"),
       _x87.require(_P(depth=7), "15.00"), _x87.require(_P(), "15.00"), _x87.require(_P(), "13.10.3052")),
      ("argument", "return", "call", "pressure", "named", "member"))
check("invert x87names: a float frame slot becomes a one-field structure on 13.10",
      _x87.members_13("{\n    float v_l12;\n    v_l12 = 1.0f;\n    g(&v_l12, v_l12);\n}"),
      "{\n    struct { float f; } v_l12;\n    v_l12.f = 1.0f;\n    g(&v_l12.f, v_l12.f);\n}")

# 5. inlining: straight-line c < 74 inlines, so a kept call needs the callee hidden; a loop lowers v
check("invert inlining: verdicts (60 B / 2 args straight; 120 B / 1 arg two loops deep; 500 B)",
      (_inl.inverse(True, 60, nargs=2)[0], _inl.inverse(True, 120, nargs=1, loop=2)[0],
       _inl.inverse(False, 120, nargs=1, loop=2)[0], _inl.inverse(False, 500)[0]),
      ("hide", "free", "forceinline", "forceinline"))

# 6. scheduling: schedwant's first difference classified tie / dag / other; the node's defined variable
from invert import schedorder as _so
_rep = """-- region 0 (5 instrs)
     1 ht  2 ns 1 S       c00  mov     [ecx=_self:1004+52]:1004 <- -1:1004 ecx=_self:1004
     2 ht  2 ns 1 -       c00  xor     al=t71:2001 cc:9000 <- al=t71:2001 al=t71:2001
   first difference at position 1: the scheduler issues 1:mov     [ecx=_self:1004+52]: (prio c00), the original 2:xor     al=t71:2001 cc:9000 (prio c00)
   0 of 40 perturbations give the wanted order; best partial: 1 of 5 positions (traced DAG: 1)
-- region 1 (3 instrs)
   first difference at position 0: the scheduler issues 3:push    esp:1004 (prio 2400), the original 2:mov     eax=_t9:1004 (prio 9800)
   2 of 40 perturbations give the wanted order; best partial: 3 of 3 positions (traced DAG: 1)
"""
check("invert schedorder: tie / dag classes, defined variable of a node",
      ([c for c, _d in _so.classify_text(_rep)], _so._defvar("mov     esi=_t1016:1004 <- [esi=_t1016:1004+0]:1004"),
       _so._defvar("push    esp:1004 <- ecx:1004 esp:1004")),
      (["tie", "dag"], "t1016", None))
check("invert inlining: body shape keeps member offsets, wildcards registers and stack slots",
      (_inl._shape(("mov", "edx, dword ptr [eax + 0xd5c]")), _inl._shape(("mov", "eax, dword ptr [esp + 4]"))),
      ("mov R, dword ptr [R + 0xd5c]", "mov R, dword ptr [S]"))
check("invert odframe: a realigned frame's order is first reference",
      _odf.scope_order(["aa", "ad", "a", "ao"], first_ref=["ao", "a", "ad", "aa"]), ["ao", "a", "ad", "aa"])

# 6. evaluation orders (RI_evalorder.cpp): last argument first, stores in statement order, the return value last
if RKEY != "13.00.9044":
    _asm = rule_compile("RI_evalorder.cpp")
    def _ins(f):
        return [_re.sub(r"\s+", " ", l.strip().split(";")[0]) for l in rule_proc(_asm, f).split("\n")
                if _re.match(r"^\s+(?:[0-9a-f]{5}\s+[0-9a-f ]+\s+)?[a-z]", l)]
    _o = lambda f, pats: [next((i for i, x in enumerate(_ins(f)) if _re.search(p, x)), -1) for p in pats]
    check("invert evalorder: a1 loads c, b, a; a5 calls h2 before h1; s1 / s2 store in statement order",
          (_o("?a1@@", [r"\+8\]", r"\+4\]", r"mov eax, DWORD PTR \[eax\]"]) == sorted(_o("?a1@@", [r"\+8\]", r"\+4\]", r"mov eax, DWORD PTR \[eax\]"])),
           _o("?a5@@", ["h2@@", "h1@@"])[0] < _o("?a5@@", ["h2@@", "h1@@"])[1],
           _o("?s1@@", [r"PTR \[eax\], ", r"PTR \[eax\+4\], "])[0] < _o("?s1@@", [r"PTR \[eax\], ", r"PTR \[eax\+4\], "])[1],
           _o("?s2@@", [r"PTR \[eax\+4\], ", r"PTR \[eax\], "])[0] < _o("?s2@@", [r"PTR \[eax\+4\], ", r"PTR \[eax\], "])[1]),
          (True, True, True, True))
    check("invert evalorder: r1 / r2 / r3 store before `xor al, al`; t1 sets al before its test",
          tuple(_o(f, [r"\], -1", r"xor al, al"])[0] < _o(f, [r"\], -1", r"xor al, al"])[1] for f in
                ("?r1@S@@", "?r2@S@@", "?r3@S@@")) + (_o("?t1@S@@", [r"xor al, al", r"test "])[0] <
                                                     _o("?t1@S@@", [r"xor al, al", r"test "])[1],),
          (True, True, True, True))
