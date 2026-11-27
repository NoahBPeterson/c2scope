# tools/regroles.py (docs/c2/regroles.md): the life-range levers for a register role difference, found against the
# original's listing (here rg_orig's own code, tests/data/regroles.cpp)
_RG = os.path.join(HERE, "tests", "data", "regroles.cpp")
_RGF = ["/O2", "/TP", "/MD", "/Gy", "/arch:SSE", "/fp:fast"]
_rd = tempfile.mkdtemp()
tool("scratch", _RG, "--func", "rg_orig", "--keep", _rd, "--", *_RGF, "/FAcs")
_rgs = orig_from_listing(os.path.join(_rd, "regroles.asm"), "?rg_orig@@YGXPAURgElem@@0@Z", os.path.join(_rd, "rg_orig.s"))
_rr = subprocess.run([sys.executable, os.path.join(HERE, "tools", "regroles.py"), _RG, "--func", "rg_hand", "--asm-vs", _rgs,
                      "--asm-func", "?rg_hand@@YGXPAURgElem@@0@Z", "--rounds", "1", "--keep", os.path.join(_rd, "out"),
                      "--", *_RGF], capture_output=True, text=True, cwd=HERE)
if VERBOSE:
    print(_rr.stdout[-3000:], _rr.stderr[-2000:])
_m = re.search(r"^round 1: .*: bdiff (\d+) regdiff (\d+)", _rr.stdout, re.M)
check("regroles: the base differs from the original's code (first in a callee-saved register, not eax)",
      bool(_m) and int(_m.group(1)) > 0, True)
check("regroles: regmap's plurality names the wanted register for `first` (the diagnosis line)",
      bool(re.search(r"^# first: ours e(?:si|di|bx|bp), original's votes .*eax", _rr.stdout, re.M)), True)
check("regroles: the induction-pointer rewrite (`char* rg_p = (char*)first + 36`) gives the original's bytes (bdiff 0)",
      bool(re.search(r"^\s+bdiff 0\s+regdiff 0\s+ptrfield char\* rg_p = \(char\*\)first \+ 36", _rr.stdout, re.M))
      and "MATCH" in _rr.stdout, True)
