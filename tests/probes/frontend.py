# Probe checks for docs/c2/frontend_il.md, frontend.md and il_sym.md (the [I] rules re-tested as [M]).
# Sources: tests/data/rules/RE_*.cpp. Untraced listings only; exec'd by tests/run_tests.py (check, rule_compile,
# rule_proc, RKEY in scope).
import re as _re


def fe_procs(asm):
    """[(decorated name, body)] in listing order."""
    return _re.findall(r"^(\S+)\s+PROC\b(.*?)^\1\s+ENDP", asm, _re.M | _re.S)


def fe_norm(body, *names):
    """A function's instructions (and DD table entries) with labels renumbered and `names` blanked, for equality."""
    out, labs = [], {}
    lab = lambda m: labs.setdefault(m.group(0), "L%d" % len(labs))
    lre = r"\$L[A-Z]*\d+(?:@\w+)?|\$T\d+"
    for ln in body.split("\n"):
        if _re.match(r"^\$L[A-Z]*\d+(?:@\w+)?:", ln):
            out.append(_re.sub(lre, lab, ln))
            continue
        m = _re.search(r"\t ([A-Za-z].*)$", ln)
        if ln.startswith(";") or not m:
            continue
        s = _re.sub(lre, lab, _re.sub(r";.*", "", m.group(1)).strip())
        for n in names:
            s = s.replace(n, "#")
        out.append(_re.sub(r"\s+", " ", s))
    return out


def fe_same(asm, a, b, *names):
    """Do functions `a` and `b` (decorated-name prefixes) give the same normalised code?"""
    pa, pb = rule_proc(asm, a), rule_proc(asm, b)
    return bool(pa) and fe_norm(pa, *names) == fe_norm(pb, *names)


def fe_short(n):
    return _re.sub(r"@.*", "", n.lstrip("?")) if not n.startswith("??") else n.split("@@")[0]


def frontend_probe_tests():
    # frontend_il.md 1.13: a sparse switch (compare chain around smaller OPSWITCHes) does not depend on case order;
    # a dense run inside it that becomes a jump table lays its bodies out in source order. 13.10 / 9044 need 5 dense
    # values inside a sparse set for a table (14.00: 4), so q* are compare chains and order-independent.
    asm = rule_compile("RE_switch_sparse.cpp")
    got = tuple(fe_same(asm, "?%s1@" % k, "?%s%d@" % (k, i)) for k in "pqr" for i in (2, 3))
    want = {"14.00": (True, True, False, False, True, True), "15.00": (True, True, False, False, True, True),
            "13.10.3077": (True,) * 6, "13.10.3052": (True,) * 6, "13.00.9044": (True,) * 6}
    check("RE switch: p1==p2, p1==p3, q1==q2, q1==q3, r1==r2, r1==r3", got, want[RKEY])

    # frontend_il.md 2.4: /O2 call order for two calls under one binary operator (IL order is left to right).
    asm = rule_compile("RE_call_order.cpp")
    order = dict((fe_short(n), "".join(_re.findall(r"call\s+\?g(\w)@", b))) for n, b in fe_procs(asm))
    keys = ("o_and o_or o_xor o_sub o_add o_sar o_shl o_div o_mod o_mul o_not o_neg o_cmp o_deref o_index o_memb "
            "o_add2 o_sub2 o_and2 o_mulL o_andL o_mulB o_sub3 o_mulc o_cvt o_lt o_ltm o_arg o_arg2 o_st "
            "n_sub n_and n_mul3 s_seq").split()
    got = " ".join(order.get(k, "?") for k in keys)
    w14 = "ba ba ba ab ab ba ba ba ba ba ba ab ba pa pa sa abc abc bca ab ab ab ab ab ba ab ba bca ba ba ab ab abc ab"
    w13 = "ba ba ba ab ba ba ba ba ba ba ba ba ba pa pa sa cba bac cba ab ab ba ba ba ba ba ba cba ba ba ab ab abc ab"
    w90 = "ba ba ba ba ba ba ba ba ba ba ba ba ba pa pa sa cba bac cba ab ab ba ba ba ba ba ba cba ba ba ab ab abc ab"
    want = {"14.00": w14, "15.00": w14, "13.10.3077": w13, "13.10.3052": w13, "13.00.9044": w90}
    check("RE call order: 34 shapes (o_and .. s_seq)", got, want[RKEY])

    # il_sym.md 8: /Od frame order of locals depends on their names, not on declaration or first use.
    asm = rule_compile("RE_odframe.cpp", "/Od")
    got = []
    for n, b in fe_procs(asm):
        offs = dict((v, int(o)) for v, o in _re.findall(r"^_(\w+)\$ = (-?\d+)", asm.split(n + " PROC")[0]
                                                         .rsplit("_TEXT\tSEGMENT", 1)[-1], _re.M))
        got.append("".join(v for v, o in sorted(offs.items(), key=lambda x: -x[1])))
    want = "yxz abc xyz q3q2q5q4q1 pwkdm yxz"
    check("RE /Od frame order (from -8 down): fa fb fc fd fe ff", " ".join(got),
          {"14.00": want, "15.00": want, "13.10.3077": want, "13.10.3052": want,
           "13.00.9044": "xyz abc xyz q5q1q2q3q4 pdwkm xyz"}[RKEY])

    # frontend_il.md 3B rule 10: x(v) in the list == `x = v;` first in the body iff no member declared after x is
    # initialised (a class member counts even without an initialiser; VC6 puts the vptr store after mem-inits).
    asm = rule_compile("RE_ctor_init.cpp")
    got = tuple(fe_same(asm, "??0T%da@" % i, "??0T%db@" % i, "T%da" % i, "T%db" % i) for i in range(1, 6))
    want = (True, False, True, False, True)
    check("RE ctor: T1..T5 list == body (trailing, member after, vptr, scalar after, trailing+use)", got,
          {"14.00": want, "15.00": want, "13.10.3077": want, "13.10.3052": want,
           "13.00.9044": (True, False, False, False, True)}[RKEY])

    # frontend_il.md 3A rule 33: no thread-safe statics: a bit test and `or` on the guard, no lock, no helper.
    # VC6 uses a 1-byte guard (`or cl, al; mov BYTE PTR ?$S..., cl`) rather than a memory `or DWORD PTR ?$S...`.
    asm = rule_compile("RE_static_local.cpp", "/EHsc")
    f = rule_proc(asm, "?f1@") + rule_proc(asm, "?f2@")
    calls = sorted(set(c for c in _re.findall(r"\bcall\s+(\S+)", f) if "security_check" not in c))
    ors = len(_re.findall(r"\bor\s+DWORD PTR \?\$S", f)) or len(_re.findall(r"\bmov\s+BYTE PTR \?\$S", f))
    got = (bool(_re.search(r"\b(lock|xchg|cmpxchg)\b", f)), ors, calls)
    want = (False, 2, ["??0K@@QAE@H@Z", "?h@@YAHH@Z", "_atexit"])
    check("RE static local: lock/xchg, guard `or`s, calls", got,
          {"14.00": want, "15.00": want, "13.10.3077": want, "13.10.3052": want, "13.00.9044": want}[RKEY])

    # frontend_il.md 5 (0x00487210): `new D(a)` state byte = 1 between operator new and the constructor, 0 after.
    asm = rule_compile("RE_new_state.cpp", "/EHsc")
    seq = []
    for ln in fe_norm(rule_proc(asm, "?f@")):
        m = _re.match(r"call (\S+)|mov BYTE PTR __\$EHRec\$\[esp\+\d+\], (\d+)", ln)
        if m:
            seq.append(m.group(1) or "s" + m.group(2))
    want = ["??0D@@QAE@XZ", "??2@YAPAXI@Z", "s1", "??0D@@QAE@H@Z", "s0", "?h@@YAXXZ"]
    check("RE new state: ctor, new, state 1, ctor(int), state 0, h", seq[:6],
          {"14.00": want, "15.00": want, "13.10.3077": want, "13.10.3052": want, "13.00.9044": want}[RKEY])

    # frontend_il.md 4.11: implicit members come out in order of first need (a destructor right after the first
    # constructor that needs it), not ctor/dtor/copy/assign; a global's ??__E/??__F stay at the end (VC6 emits its
    # _$E* stubs at the top and inlines the implicit members at /O2; with /Ob0 their order is first-need after u).
    got = []
    imp_fl = ["/Ob0"] if RKEY == "13.00.9044" else []
    for fl in ([], ["/DREV"]):
        names = [n for n, b in fe_procs(rule_compile("RE_implicit_order.cpp", *(imp_fl + fl)))]
        got.append(" ".join({"??4Imp@@QAEAAU0@ABU0@@Z": "asg", "??0Imp@@QAE@ABU0@@Z": "cpy", "??1Imp@@QAE@XZ": "dtor",
                             "??0Imp@@QAE@XZ": "ctor"}.get(n, fe_short(n)) for n in names))
    if RKEY == "13.00.9044":
        want = ["_$E4 _$E1 _$E3 _$E2 before u asg cpy dtor ctor after",
                "_$E4 _$E1 _$E3 _$E2 before u ctor dtor cpy asg after"]
    else:
        e = {"14.00": "??__Egfirst ??__Fgfirst", "15.00": "??__Egfirst ??__Fgfirst", "13.10.3077": "_$E1 _$E2",
             "13.10.3052": "_$E1 _$E2"}[RKEY]
        want = ["before asg cpy dtor ctor after u " + e, "before ctor dtor cpy asg after u " + e]
    check("RE implicit member order: use asg,cpy,ctor / ctor,cpy,asg", got, want)

    # frontend_il.md 4.13: call cycles; the edges into the last-defined member are ignored (VC6: definition order).
    got = []
    for v in ("C2", "C3", "C3X", "C4", "C4X", "C5", "CC"):
        got.append(" ".join(fe_short(n) for n, b in fe_procs(rule_compile("RE_cycle.cpp", "/D" + v))))
    want = ["c1 c2", "c2 c1 c3", "c1 c2 c3", "c3 c2 c1 c4", "c2 c3 c1 c4", "c4 c3 c2 c1 c5", "c2 c1 c3"]
    w90 = ["c1 c2", "c1 c2 c3", "c1 c2 c3", "c1 c2 c3 c4", "c1 c2 c3 c4", "c1 c2 c3 c4 c5", "c1 c2 c3"]
    check("RE cycles: C2 C3 C3X C4 C4X C5 CC", got,
          {"14.00": want, "15.00": want, "13.10.3077": want, "13.10.3052": want, "13.00.9044": w90}[RKEY])

    # frontend_il.md 3A rule 8: NRVO iff exactly one `return` statement and it names one non-static local. 13.10 / VC6
    # have no NRVO.
    asm = rule_compile("RE_nrvo.cpp", "/EHsc")
    got = "".join("c" if "??0K@@QAE@ABU0@@Z" in b else "e" for n, b in fe_procs(asm) if _re.match(r"\?n\d+@", n))
    w = "ecccecceeccce"
    check("RE NRVO n1..n13 (e = elided, c = copy ctor)", got,
          {"14.00": w, "15.00": w, "13.10.3077": "c" * 13, "13.10.3052": "c" * 13, "13.00.9044": "c" * 13}[RKEY])

    # frontend.md: no `lea [this+4]` survives from the member address temporary; by-value and by-reference accessors
    # give the same code (calls, stores, loops between the reads) except next to a local with a destructor: without
    # /EHsc the by-value sum is computed after the destructor call (v_eh), with /EHsc the argument order differs
    # (v_ehp).
    for fl in ([], ["/EHsc"]):
        asm = rule_compile("RE_member_reads.cpp", *fl)
        lea = sum(len(_re.findall(r"\blea\s+\w+, DWORD PTR \[\w+\+4\]", rule_proc(asm, "?%s@C@" % f))) for f in ("two", "twocall", "loop"))
        got = (lea,) + tuple(fe_same(asm, "?v_%s@" % k, "?r_%s@" % k, "v_" + k, "r_" + k)
                             for k in ("plain", "call", "alias", "loop", "pass", "p2", "eh", "ehp"))
        want = (0, True, True, True, True, True, True, bool(fl), not fl)
        check("RE member reads %s: lea, v==r plain call alias loop pass p2 eh ehp" % " ".join(fl), got,
              {"14.00": want, "15.00": want, "13.10.3077": want, "13.10.3052": want, "13.00.9044": want}[RKEY])


frontend_probe_tests()
