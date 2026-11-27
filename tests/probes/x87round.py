# x87round.py (docs/c2/x87round.md): one function per reason class in tests/data/x87round.cpp, compiled as is ("ours") and
# with -DORIG (the cause removed: the original keeps the value on the x87 stack). Run by run_tests.py's probe_modules() in
# its namespace (check, HERE, RKEY, tool(), VERBOSE). The listings are untraced compiles of the build under test; one
# traced run (below) checks the trace path: IL line, and on 14.00 / 15.00 the call's kill set read from the compiler.
import os, re, subprocess, sys, tempfile

X87R_SRC = os.path.join(HERE, "tests", "data", "x87round.cpp")

# (pair kind, reason classes of our points) of each row that is not rounded on both sides, per function and build
# (the paired-both count is not checked). 13.10 rounds a named float local only across a call (x87.md 12.2 rule 7).
X87R_FUNCS = ("f_local", "f_inl", "f_call", "f_cast", "f_ret", "f_arg", "f_vol", "f_spill")
X87R_NEW = {
    "f_local": [("ours", ["float local"])],
    "f_inl": [("ours", ["float parameter of an inlined helper"])],
    "f_call": [("ours", ["call between def and use"])],
    "f_cast": [("ours", ["cast to float"])],
    "f_ret": [("ours", ["return"])],
    "f_arg": [("ours", ["argument"])],
    "f_vol": [("ours", ["volatile / address-taken"])],
    "f_spill": [("ours", ["spill under x87 pressure"])] * 3,
}
X87R_OLD = {
    "f_local": [], "f_inl": [], "f_cast": [], "f_ret": [], "f_arg": [],
    "f_call": [("ours", ["call between def and use"])],
    "f_vol": [("ours", ["volatile / address-taken"])],
    "f_spill": [("ours", ["spill under x87 pressure"])] * 2 + [("unpaired-orig", [])],
}
X87R_WANT = {k: (X87R_OLD if k.startswith("13.10") else X87R_NEW) for k in ("14.00", "15.00", "13.10.3077", "13.10.3052")}


def x87round_tests():
    print("x87round: reason classes (docs/c2/x87round.md)")
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import x87round as XR
    d = tempfile.mkdtemp()
    asm = {}
    for v, fl in (("ours", []), ("orig", ["/DORIG"])):
        p = os.path.join(d, v + ".asm")
        subprocess.run([sys.executable, os.path.join(HERE, "tools", "msvc.py"), "cl", "/nologo", "/c", "/O2", "/TP", "/FAsc",
                        "/Fa" + p, "/Fo" + os.path.join(d, v + ".obj")] + fl + [X87R_SRC], capture_output=True, text=True, cwd=HERE)
        asm[v] = p
    if not all(os.path.exists(p) for p in asm.values()):
        check("x87round: compiles", False, True)
        return
    texts = {v: open(p, errors="replace").read() for v, p in asm.items()}
    got = {}
    for fn in X87R_FUNCS:
        names = [re.search(r"^(\?%s@\S+)\s+PROC" % fn, texts[v], re.M) for v in ("ours", "orig")]
        if not all(names):
            got[fn] = None
            continue
        rows, _ = XR.summarize(asm["ours"], names[0].group(1), asm["orig"], names[1].group(1), X87R_SRC)
        got[fn] = rows
    for fn in X87R_FUNCS:
        check("x87round %s: our rounding points that the original does not have, and why" % fn,
              got.get(fn), X87R_WANT[RKEY][fn])

    # one traced run: the IL of the store (the home symbol), and on 14.00 / 15.00 LoAddCallScratchRegs's kill set for the call
    orig_s = os.path.join(d, "orig_f_call.s")
    nm = re.search(r"^(\?f_call@\S+)\s+PROC", texts["ours"], re.M).group(1)
    XR_rows = XR.our_instrs(asm["orig"], re.search(r"^(\?f_call@\S+)\s+PROC", texts["orig"], re.M).group(1))
    open(orig_s, "w").write("".join("%08x %s %s\n" % (i.addr, i.mn, i.ops) for i in XR_rows))
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "x87round.py"), X87R_SRC, "--func", "f_call", "--asm-vs", orig_s,
                        "--asm-func", nm, "--show-il", "--", "/TP"], capture_output=True, text=True, cwd=HERE)
    out = r.stdout
    if VERBOSE:
        print(out[-3000:], r.stderr[-2000:])
    il = re.search(r"IL: _r:\d008", out) is not None
    kill = "(kills all of GF0..GF7)" in out
    want_kill = RKEY in ("14.00", "15.00")
    check("x87round traced f_call: store's IL names the home _r; the call's kill set read from c2 (14.00 / 15.00)",
          (il, kill), (True, want_kill))


x87round_tests()
