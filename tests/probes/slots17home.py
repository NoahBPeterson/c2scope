# Which locals take a parameter's home on cl 17.00 x64 (docs/c2/slots.md 6.2): tests/data/slots17home/homes.cpp and
# homes_gs.cpp under the traced 17.00 compile (two compiles; skipped when the x64 traced toolchain is not set up).
# Homes: a = 8, b = 16, the two dummy parameters 24 and 32 (offsets in the packer's coordinates).
import re as _re, subprocess as _sp, sys as _sys, tempfile as _tf
_sys.path.insert(0, os.path.join(HERE, "tools"))
import c2sched17 as _s17


def _slots17home_run(src, *flags):
    """({function name: {member name: offset}}, the listing text) of every function of SRC, one traced compile."""
    d = _tf.mkdtemp()
    r = _sp.run([_sys.executable, os.path.join(HERE, "tools", "c2trace.py"), "slots", src, "--func", "", "--all",
                 "--c2", "17.00", "--keep", d, "--"] + list(flags) + ["/O2", "/TP"],
                capture_output=True, text=True, cwd=HERE)
    out, cur = {}, None
    for ln in r.stdout.splitlines():
        m = _re.match(r"^# \?(\w+)@@\S+\s+\(c2 17", ln)
        if m:
            cur = out.setdefault(m.group(1), {})
        m = _re.match(r"^\s+\d+\s+p?\d+\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+(.*)$", ln)
        if m and cur is not None:
            for mem in m.group(2).split(", "):
                cur[mem.split(":")[0].rstrip("$")] = int(m.group(1))
    asm = os.path.join(d, os.path.splitext(os.path.basename(src))[0] + ".asm")
    return out, (open(asm, errors="replace").read() if os.path.exists(asm) else "")


def _slots17home_saves(asm, func):
    """The [rsp+N] of the callee-saved register saves in FUNC's listing (the first store of each register)."""
    m = _re.search(r"^\?%s@@\S+\s+PROC.*?^\?%s@@\S+\s+ENDP" % (func, func), asm, _re.M | _re.S)
    seen = {}
    for n, reg in _re.findall(r"mov\s+QWORD PTR \[rsp\+(\d+)\], (rbp|rsi|rdi|r1[2-5])\b", m.group(0) if m else ""):
        seen.setdefault(reg, int(n))
    return sorted(seen.values())


def slots17home_tests():
    if not _s17.have_c2_1700():
        skip("slots17home: parameter homes on x64", "the 17.00 x64 traced toolchain is not set up here")
        return
    P, asm = _slots17home_run(os.path.join(HERE, "tests", "data", "slots17home", "homes.cpp"), "/GS-")
    x = lambda f, n="x": P.get(f, {}).get(n)
    check("slots17home: a parameter used only as a call argument keeps its home from every local (x -> a dummy home)",
          (x("arg_only"), x("arg_only", "a")), (24, 8))
    check("slots17home: ... a parameter that is never used does too",
          (x("unused"), x("unused", "a"), x("unused", "b")), (24, 8, 16))
    check("slots17home: a parameter used in arithmetic / stored / passed and used in arithmetic / dereferenced and "
          "passed leaves its home to x",
          (x("arith"), x("store"), x("arg_arith"), x("deref_arg")), (8, 8, 8, 8))
    check("slots17home: a hidden return slot (class returned by value) packs like a plain local",
          (x("retslot", "s"), x("plain", "s")), (24, 24))
    check("slots17home: three locals live together, the home of a refused (a call argument only): the longer-used "
          "8-byte local first, p1 (b's home), then 24, 32",
          (x("order8", "l"), sorted((x("order8", "i1"), x("order8", "i2")))), (16, [24, 32]))
    check("slots17home: ... with two int locals they are visited first (size), so the long one is last (p3)",
          (x("order4", "l"), sorted((x("order4", "i1"), x("order4", "i2")))), (32, [16, 24]))
    check("slots17home: callee-saved saves go to the free homes after the packing, lowest first (sw1: all free; "
          "sw2: x took p0)", (_slots17home_saves(asm, "sw1"), _slots17home_saves(asm, "sw2")),
          ([48, 56, 64], [56, 64, 72]))
    G, _ = _slots17home_run(os.path.join(HERE, "tests", "data", "slots17home", "homes_gs.cpp"), "/GS")
    check("slots17home: /GS: without a buffer nothing changes (x in a dummy home), with a cookie no home is offered",
          (G.get("gs_nobuf", {}).get("x"), G.get("gs_buf", {}).get("x")), (24, 0))


slots17home_tests()
