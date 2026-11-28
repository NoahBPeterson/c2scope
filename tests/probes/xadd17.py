# The register of a reference release's -1 / xadd result on cl 17.00 x64 (docs/c2/port-1700.md 13):
# tests/data/xadd17/xadd.cpp, one traced compile; skipped when the x64 traced toolchain is not set up.
# In each function the loop-invariant -1 is hoisted into a callee-saved register (ebp); what the second release
# (after the loop) does with it depends on whether the xadd's own result is a named variable.
import re as _re, subprocess as _sp, sys as _sys, tempfile as _tf
_sys.path.insert(0, os.path.join(HERE, "tools"))
import c2sched17 as _s17


def _xadd17_regs(src):
    """{function: [the register operand of each `lock xadd`, in listing order]} from one traced compile's listing."""
    d = _tf.mkdtemp()
    _sp.run([_sys.executable, os.path.join(HERE, "tools", "c2trace.py"), "slots", src, "--func", "", "--all",
             "--c2", "17.00", "--keep", d, "--", "/GS-", "/O2", "/TP"], capture_output=True, text=True, cwd=HERE)
    asm = os.path.join(d, os.path.splitext(os.path.basename(src))[0] + ".asm")
    txt = open(asm, errors="replace").read() if os.path.exists(asm) else ""
    out = {}
    for m in _re.finditer(r"^\?(\w+)@@\S+\s+PROC.*?^\?\1@@\S+\s+ENDP", txt, _re.M | _re.S):
        out[m.group(1)] = _re.findall(r"lock xadd\s+DWORD PTR \[[^\]]*\],\s*(\w+)", m.group(0))
    return out


def xadd17_tests():
    if not _s17.have_c2_1700():
        skip("xadd17: the register of a release's -1 / xadd result", "the 17.00 x64 traced toolchain is not set up here")
        return
    R = _xadd17_regs(os.path.join(HERE, "tests", "data", "xadd17", "xadd.cpp"))
    second = lambda f: (R.get(f) or [None, None])[-1]
    first = lambda f: (R.get(f) or [None])[0]
    check("xadd17: the loop's release takes the xadd result in eax (the hoisted -1 is copied in, mov eax, ebp)",
          {first(f) for f in ("rel_inline", "rel_decr", "rel_named")}, {"eax"})
    check("xadd17: second release, result used inline (n = xadd(p, -1) - 1): the -1 and the result are the hoisted "
          "web, ebp", second("rel_inline"), "ebp")
    check("xadd17: ... _InterlockedDecrement(p) is the same chain: ebp", second("rel_decr"), "ebp")
    check("xadd17: ... the xadd's result in a named variable (old = xadd(p, -1); n = old - 1): the second "
          "release's -1 is a local temporary, a volatile register", second("rel_named") in ("ecx", "eax", "edx"), True)


xadd17_tests()
