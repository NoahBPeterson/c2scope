# A constant in a register or rebuilt as an immediate on cl 17.00 x64 (docs/c2/immcand.md section 7,
# tools/constregs.py --c2 17.00): tests/data/constfold17/constfold.cpp, a traced compile per check; skipped when the
# x64 traced toolchain is not set up. NColor folds a constant into its uses when MdmDefCost >= the sum of
# NColMdNoFoldBenefit (zero stored through memory counts 1 a use, any other value 0, a 16-bit store or an imm64 INF);
# a register that stays is then used by the late peepholes for every remaining immediate of the value.
import re as _re, subprocess as _sp, sys as _sys, tempfile as _tf
_sys.path.insert(0, os.path.join(HERE, "tools"))
import c2sched17 as _s17


def _cf17_asm(src, func):
    """The listing (/FAsc text) of `func` from one traced compile under 17.00."""
    d = _tf.mkdtemp()
    _sp.run([_sys.executable, os.path.join(HERE, "tools", "c2trace.py"), "slots", src, "--func", func, "--c2", "17.00",
             "--keep", d, "--", "/GS-", "/O2", "/TP"], capture_output=True, text=True, cwd=HERE)
    asm = os.path.join(d, os.path.splitext(os.path.basename(src))[0] + ".asm")
    txt = open(asm, errors="replace").read() if os.path.exists(asm) else ""
    m = _re.search(r"^\?%s@@\S+\s+PROC.*?^\?%s@@\S+\s+ENDP" % (func, func), txt, _re.M | _re.S)
    return m.group(0) if m else ""


def _cf17_regs(src, func):
    """(outcome words) of tools/constregs.py --c2 17.00 for the first constant: fold / register, sum, cost."""
    r = _sp.run([_sys.executable, os.path.join(HERE, "tools", "constregs.py"), src, "--func", func, "--c2", "17.00",
                 "--", "/GS-", "/O2", "/TP"], capture_output=True, text=True, cwd=HERE).stdout
    out = "folded" if "folded away" in r else ("register" if "outcome: register" in r else "none")
    m = _re.search(r"MdmDefCost (\d+); NColBenefitOfUsesInRange (\w+)", r)
    return out, (m.group(1), m.group(2)) if m else None, "INF" in r


def constfold17_tests():
    if not _s17.have_c2_1700():
        skip("constfold17: constants in registers or immediates on cl 17.00 x64",
             "the 17.00 x64 traced toolchain is not set up here")
        return
    src = os.path.join(HERE, "tests", "data", "constfold17", "constfold.cpp")
    L = {f: _cf17_asm(src, f) for f in ("z8", "z1", "n7", "n7w", "big", "pair_two", "pair_one")}
    imm = lambda t: len(_re.findall(r"mov\s+(?:DWORD|QWORD) PTR \[[^\]]*\], (?:0|7)\b", t))
    regs = lambda t: len(_re.findall(r"mov\s+(?:DWORD|QWORD) PTR \[[^\]]*\], (?:e|r)(?:ax|bp|si|di|bx|\d+d?)\b", t))
    check("constfold17: zero stored 8 times: a register (xor, 8 stores `mov [m], reg`), no immediate store",
          (regs(L["z8"]), imm(L["z8"]), bool(_re.search(r"xor\s+e(?:ax|bp|si|di)", L["z8"]))), (8, 0, True))
    check("constfold17: zero stored once: sum 1 against MdmDefCost 1, folded: an immediate",
          (regs(L["z1"]), imm(L["z1"])), (0, 1))
    check("constfold17: 7 stored 8 times: every benefit 0, folded: eight immediates, no `mov reg, 7`",
          (imm(L["n7"]), bool(_re.search(r"mov\s+e\w+, 7\b", L["n7"]))), (8, False))
    check("constfold17: ... with one 16-bit store (INF): the web is a register, and the late peephole uses it for the "
          "dword stores, the compare and the argument",
          (bool(_re.search(r"mov\s+e\w+, 7\b", L["n7w"])), imm(L["n7w"]),
           bool(_re.search(r"cmp\s+DWORD PTR \[\w+\], e\w+", L["n7w"])), bool(_re.search(r"mov\s+edx, e\w+", L["n7w"]))),
          (True, 0, True, True))
    check("constfold17: an imm64 is a register (movabs) for every store",
          (bool(_re.search(r"mov\s+r\w+, 1311768467463790320", L["big"])), len(_re.findall(r"QWORD PTR \[\w+\+(?:64|72|80)\], r\w+", L["big"]))),
          (True, 3))
    check("constfold17: the pair of dword zero stores after a join: two zero definitions reach it: one immediate qword "
          "store; with one definition: the register",
          (bool(_re.search(r"mov\s+QWORD PTR \[\w+\+16\], 0\b", L["pair_two"])),
           bool(_re.search(r"mov\s+QWORD PTR \[\w+\+16\], r\w+", L["pair_one"]))), (True, True))
    r = {f: _cf17_regs(src, f) for f in ("z8", "n7", "n7w")}
    check("constfold17: constregs --c2 17.00: zero x8 sum 8 > cost 1 register; 7 x8 sum 0 folded; 7 with a 16-bit store INF",
          (r["z8"][:2], r["n7"][:2], r["n7w"][0], r["n7w"][2]),
          (("register", ("1", "8")), ("folded", ("1", "0")), "register", True))


constfold17_tests()
