# Probe checks for docs/c2/opt.md (the [I] rules re-tested as [M] or [C]). Sources: tests/data/rules/RO_*.cpp.
# Listings are untraced; three checks use one hooked compile each. Exec'd by tests/run_tests.py (check,
# rule_compile, rule_proc, rule_trace, RKEY in scope).
import re as _re

print("opt.md probes (tests/data/rules/RO_*)")


def ro_ins(asm, prefix):
    """The instructions of one function's listing as 'mnem operands' strings (labels as '$L..:')."""
    out = []
    for l in rule_proc(asm, prefix).replace("\r", "").splitlines():
        if _re.match(r"^\$\S+:", l):
            out.append(l.strip())
            continue
        m = _re.match(r"^(?:\s+[0-9a-f]{5})?\t[0-9a-f ]+\t+ ([a-z][a-z0-9]*)(?:\t+ ?(.*?))?\s*$", l)
        if m:
            out.append((m.group(1) + " " + _re.sub(r"\s+", " ", _re.sub(r"\s*;.*$", "", m.group(2) or ""))).strip())
    return out


def ro_count(ins, rx):
    return sum(1 for i in ins if _re.search(rx, i))


ALL4 = lambda v: {"14.00": v, "15.00": v, "13.10.3077": v, "13.10.3052": v}

# §2 rules 2-3 / §0 item 4: a + b inside a sum is computed twice; a * b once; factoring.
a = rule_compile("RO_flatten.cpp")
got = (ro_count(ro_ins(a, "?sum_twice@@"), r"^(add|lea) (?!esp)"), ro_count(ro_ins(a, "?mul_twice@@"), r"^imul"),
       tuple(i for i in ro_ins(a, "?fac_same@@") if not i.startswith(("mov", "ret"))),
       tuple(i for i in ro_ins(a, "?fac_common@@") if not i.startswith(("mov", "ret"))),
       ro_count(ro_ins(a, "?fac_load@@"), r"\[eax\+4\]"), tuple(i for i in ro_ins(a, "?fac_and@@") if i.startswith("lea")))
w14 = (3, 1, ("add eax, eax",) * 3, ("add eax, ecx",) + ("add eax, eax",) * 2, 1, ("lea eax, DWORD PTR [eax+eax*8]",))
w13 = (3, 1, ("shl eax, 3",), ("add eax, ecx", "shl eax, 2"), 1, ("lea eax, DWORD PTR [eax+eax*8]",))
check("RO_flatten: a+b twice, a*b once, a*3+a*5 = a*8, a*4+b*4 = (a+b)*4, p[1]+p[1] one load, (a&b)*9",
      got, {"14.00": w14, "15.00": w14, "13.10.3077": w13, "13.10.3052": w13}[RKEY])

# §6 rules 4 and 6: stores to p->m ([reg+36]) left; ds_blk = a block boundary alone keeps the first store.
a = rule_compile("RO_deadst.cpp")
got = tuple(ro_count(ro_ins(a, "?%s@@" % f), r"^mov DWORD PTR \[e..\+36\], ")
            for f in ("ds_same", "ds_other", "ds_read", "ds_arms", "ds_blk"))
check("RO_deadst: stores to p->m: same block / other-pointer store between / load between / arms / block boundary",
      got, ALL4((1, 1, 2, 2, 2))[RKEY])

# §7 rule 5: register or immediates for the constant 0x12345.
a = rule_compile("RO_imm.cpp")
fs = ("im_bc", "im_ab", "im_nd", "im_abc", "im_add", "im_push", "im_call3", "im_call4")
got = tuple("reg" if ro_count(ro_ins(a, "?%s@@" % f), r"^mov e[a-z]{2}, 74565$") else "imm" for f in fs)
check("RO_imm: bc / ab / no-disp / abc / add-to-mem / push / 3 uses across a call / 4 uses across a call",
      got, ALL4(("reg", "imm", "imm", "reg", "reg", "imm", "imm", "reg"))[RKEY])
check("RO_imm: im_add keeps both memory-destination adds (13.10 splits the first: load, add, store)",
      ro_count(ro_ins(a, "?im_add@@"), r"^add DWORD PTR"),
      {"14.00": 2, "15.00": 2, "13.10.3077": 1, "13.10.3052": 1}[RKEY])
# The decision across a call is CmdPostspill's: a callee-saved register whose value (record +0x38) is <= 2 is spilled.
hook = {"14.00": "10c25abf=eax,esi>0x38", "15.00": "511fb331=eax,ebx>0x38",
        "13.10.3077": "1078535d=eax,esi>0x38", "13.10.3052": "10786e7d=eax,esi>0x38"}[RKEY]
got = []
for f in ("im_call3", "im_call4"):
    ev = rule_trace("RO_imm.cpp", "?%s@@" % f, hook)
    got.append(dict(_re.findall(r"eax=(\w+)\s.*?>0x38=(\w+)", "\n".join(ev))).get("8"))
check("RO_imm: CmdPostspill's value for edi: im_call3 / im_call4 (<= 2 spills it)", tuple(got), ALL4(("2", "3"))[RKEY])

# §7 rules 1-2, §12 WpaRangeGlobopt: branch facts against constants fold; ranges do not.
a = rule_compile("RO_range.cpp")
got = (ro_count(ro_ins(a, "?rg_nested@@"), r"^cmp "), ro_count(ro_ins(a, "?rg_nested2@@"), r"^cmp "),
       ro_count(ro_ins(a, "?rg_eq@@"), r"^mov eax, 10$"), ro_count(ro_ins(a, "?rg_ab@@"), r"^sub eax"))
check("RO_range: nested range compare kept (2 cmp, 2 cmp), a==5 folds, a==b does not", got, ALL4((2, 2, 1, 1))[RKEY])

# §5 rule 2, §12 cq16: store and reload through two spellings of one address.
a = rule_compile("RO_fwd.cpp")
fs = ("fw_alias", "fw_back", "fw_same", "fw_star", "fw_off0", "fw_ref", "fw_copy", "fw_cast")
got = tuple("reload" if ro_count(ro_ins(a, "?%s@@" % f), r"^mov eax, DWORD PTR \[eax") else "fwd" for f in fs)
check("RO_fwd: &p->n alias / back / same / *q / offset 0 / reference / pointer copy / cast",
      got, ALL4(("reload", "reload", "fwd", "fwd", "fwd", "reload", "fwd", "reload"))[RKEY])

# §4 rule 8: loads of c->one ([reg+144]).
a = rule_compile("RO_nulltest.cpp")
got = tuple(ro_count(ro_ins(a, "?%s@@" % f), r"\+144\]") for f in ("nt_twice", "nt_once", "nt_local", "nt_twoblk"))
check("RO_nulltest: c->one read: tested and dereferenced twice / once / named local / two blocks",
      got, {"14.00": (2, 1, 1, 1), "15.00": (2, 1, 1, 1), "13.10.3077": (1, 1, 1, 1), "13.10.3052": (1, 1, 1, 1)}[RKEY])

# §9 rules 1, 3, 4: float CSE (fmul count; 13.10 has no /fp: switch, D4002).
got = []
for fl in ([], ["/fp:strict"], ["/fp:fast"]):
    ins = ro_ins(rule_compile("RO_fpstrict.cpp", *fl), "?fp_cse@@")
    got.append((ro_count(ins, r"^fmul"), ro_count(ins, r"^fst[p]? QWORD PTR tv")))
w14 = ((1, 1), (2, 0), (1, 0))
check("RO_fpstrict: (fmul, double temps) under precise / strict / fast", tuple(got),
      {"14.00": w14, "15.00": w14, "13.10.3077": ((1, 0),) * 3, "13.10.3052": ((1, 0),) * 3}[RKEY])

# §8.4 step 3: which uses keep the pointer (listing shape per loop).
a = rule_compile("RO_amode.cpp")
got = tuple(bool(ro_count(ro_ins(a, "?%s@@" % f), r"\[e..\+e..\*4")) for f in ("am_load", "am_store", "am_addr",
                                                                                  "am_cmp", "am_two"))
w = (True, True, False, False, False)
check("RO_amode: [base+i*4] kept: load / store / address as a value / address compared / two arrays",
      got, {"14.00": w, "15.00": (True, True, False, False, True), "13.10.3077": w, "13.10.3052": w}[RKEY])
check("RO_amode: int reduction of unknown n unrolled by 2 (two accumulators) only on 15.00",
      ro_count(ro_ins(a, "?am_load@@"), r"^add eax, 2$"), {"14.00": 0, "15.00": 1, "13.10.3077": 0, "13.10.3052": 0}[RKEY])

# §3 rule 4a: the EH-frame local `t` stays (IL at Color) and GoptSingleUseCopyPropLegal is never asked about it.
legal = {"14.00": "@GoptSingleUseCopyPropLegal@12", "15.00": "5109db30", "13.10.3077": "107aa742",
         "13.10.3052": "107b326a"}[RKEY]
got = tuple(len(rule_trace("RO_ehsu.cpp", "?%s@@" % f, legal, "/EHsc")) for f in ("su_eh", "su_ctl", "su_after"))
check("RO_ehsu: GoptSingleUseCopyPropLegal calls: EH region ahead / no EH frame / after the scope",
      got, ALL4((0, 1, 1))[RKEY])
