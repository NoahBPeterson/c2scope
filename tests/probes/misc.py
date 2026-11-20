# Probe checks for the [I] rules of docs/c2/x87.md, loadcse.md, isel.md, sched.md, slots.md, symkeys.md (RX_* probes).
# Exec'd by tests/run_tests.py (probe_modules) with check(), rule_compile(), rule_proc(), rule_trace(), RKEY, tool(),
# HERE in scope. Listing checks are untraced; the two traced ones (RX_ehsched /EH*, RX_rmw13 Riscify) are local.
import os
import re


def _all4(v):
    return {k: v for k in ("14.00", "15.00", "13.10.3077", "13.10.3052")}


def rx_misc_tests():
    print("misc rule probes (RX_*: x87, loadcse, isel, sched, slots, symkeys)")
    rules = os.path.join(HERE, "tests", "data", "rules")

    # x87.md 11 step 1 / 12.2 rules 1-3: x87 value kept across a call only when the callee is x87-free (13.10 too)
    asm = rule_compile("RX_x87free.cpp")
    got = tuple(f for f in ("k_geti", "k_getd", "k_void", "k_getd2", "k_ext")
                if not re.search(r"fstp\s+QWORD PTR _y\$", rule_proc(asm, "?%s@" % f)))
    check("RX_x87free kept across x87-free callees", got, _all4(("k_geti", "k_getd"))[RKEY])

    # loadcse.md 3: which RMWs survive; 13.10 = Riscify split, put back by Riscify_undo when still adjacent
    asm = rule_compile("RX_rmw13.cpp")
    rmw = re.compile(r"\s(?:add|sub|or|and|xor|shl|sar|shr)\s+DWORD PTR (?:\?g@|\[)")
    names = re.findall(r"^\S+\s+(r_\w+)\(", open(os.path.join(rules, "RX_rmw13.cpp")).read(), re.M)
    got = tuple(n for n in names if not rmw.search(rule_proc(asm, "?%s@" % n)))
    split13 = ("r_reg", "r_or_reg", "r_shl_reg", "r_glob_reg", "r_deref_reg", "r_imm_call_before",
               "r_imm_read_call", "r_reg_from_mem", "r_vol_imm", "r_loop_imm", "r_imm_call0_before",
               "r_glob_call_before", "r_imm_esi_after", "r_imm_two_bases", "r_reg_ready", "r_reg_ready2",
               "r_imm_result")
    want = {"14.00": ("r_loop_imm",), "15.00": ("r_loop_imm",), "13.10.3077": split13, "13.10.3052": split13}
    check("RX_rmw13 functions without a memory RMW", got, want[RKEY])
    if RKEY.startswith("13.10"):
        # the RMW tuple is at Riscify, split after it, back at MdmSuperHack only when nothing was scheduled between
        def cnt(func):
            out = tool("il", os.path.join(rules, "RX_rmw13.cpp"), "--func", func, "--at",
                       "@Riscify@4,@Schedule@8,@MdmSuperHack@4", "--count", r" (add|or|sub) 1 0 \| \[", "--", "/O2", "/TP")
            return tuple(int(n) for n in re.findall(r"^@\S+\s+(\d+)\s+\(of", out, re.M))
        check("RX_rmw13 13.10 Riscify split/undo r_imm, r_imm_call0_before",
              (cnt("r_imm@"), cnt("r_imm_call0")), ((1, 0, 1), (1, 0, 0)))

    # sched.md rule 1: a try/catch under /EHa is not scheduled; a destructor-only function and a callee's try are
    sched = {}
    for eh in ("/EHsc", "/EHs", "/EHa"):
        out = tool("il", os.path.join(rules, "RX_ehsched.cpp"), "--func", "e_", "--at", "@Schedule@8", "--count",
                   "zzzz", "--", "/O2", "/TP", eh)
        sched[eh] = tuple(sorted(set(re.findall(r"\?(e_\w+?)@@", out))))
    every = ("e_dtor", "e_plain", "e_try_all", "e_try_callee", "e_try_int", "e_try_nocall")
    check("RX_ehsched scheduled functions /EHsc /EHs /EHa", (sched["/EHsc"], sched["/EHs"], sched["/EHa"]),
          _all4((every, every, ("e_dtor", "e_plain", "e_try_callee")))[RKEY])

    # sched.md rule 3: the destructor-only function's state store stays above the loads under /EHa only
    def store_first(eh):
        p = rule_proc(rule_compile("RX_ehsched.cpp", eh), "?e_dtor@")
        m = re.search(r"__\$EHRec\$\[esp\+\d+\], 0\b", p)
        return bool(m) and m.start() < p.find("_p$[")
    check("RX_ehsched e_dtor state store before the loads /EHsc /EHa", (store_first("/EHsc"), store_first("/EHa")),
          _all4((False, True))[RKEY])

    # slots.md 1 consequences: escaping named local lives to its scope's end; a const& temporary to its expression's
    def frame(asm, func):
        m = re.search(r"^(\?%s@\S*)\s+PROC\b" % func, asm, re.M)
        seg = asm[:m.start()] if m else ""
        seg = seg[seg.rfind("_TEXT\tSEGMENT"):]
        return tuple(re.sub(r"\d{3,}$", "", a) + "=" + b for a, b in re.findall(r"^(\S+)\s*=\s*(-?\d+)", seg, re.M))
    asm = rule_compile("RX_cfref.cpp")
    got = tuple(frame(asm, f) for f in ("s_temp", "s_named", "s_block"))
    check("RX_cfref frame slots s_temp, s_named, s_block", got, _all4((
        ("_k$=-8", "$T=-4", "$T=8", "_a$=8", "_b$=12"),
        ("_k$=-12", "_t$=-8", "_u$=-4", "_a$=8", "_b$=12"),
        ("_k$=-8", "_t$=-4", "_u$=8", "_a$=8", "_b$=12")))[RKEY])

    # symkeys.md 1 / slots.md 2, 7: colour creation follows the instruction stream, not the SYM key
    asm = rule_compile("RX_slotseed.cpp")
    got = tuple(frame(asm, f) for f in ("o_use_b_first", "o_use_a_first", "o_args", "o_hoist"))
    check("RX_slotseed slot order", got, _all4((("_b$=-8", "_a$=-4"), ("_a$=-8", "_b$=-4"),
                                                ("_b$=-8", "_a$=-4"), ("_b$=-8", "_a$=-4", "_n$=8")))[RKEY])

    # isel.md 7.4: constant kept in a register when the summed use cost >= 2 (outcomes, all four builds)
    asm = rule_compile("RX_constreg.cpp")
    regs = []
    for f in ("c_p12", "c_p01", "c_gg", "c_z2", "c_z3", "c_k1000", "c_m1x3", "c_push", "st2@K", "c_byte3", "c_p0p0",
              "c_p0g", "c_p0gg"):
        body = rule_proc(asm, "?%s@" % f)
        if re.search(r"(?:PTR [^,\n]+|\?g[12]@@3HA), e?[a-d][lx]\b", body) or re.search(r"\spush\s+e[a-d]x\b", body):
            regs.append(f)
    check("RX_constreg constants in a register", tuple(regs),
          _all4(("c_p12", "c_gg", "c_z3", "c_m1x3", "st2@K", "c_byte3", "c_p0gg"))[RKEY])


rx_misc_tests()
