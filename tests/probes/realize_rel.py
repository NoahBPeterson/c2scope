# tools/realize.py: constructs for the register-role answers of rasim (interf / nointerf / copy / nocopy / pref /
# noreg / canreg), docs/c2/realize.md 2.4a.  Sources: tests/data/realize_rel.cpp (the pairs), the same file's X__base
# functions (the edits).
_RR = os.path.join(HERE, "tests", "data", "realize_rel.cpp")
_RRF = ["/TP", "/MT", "/GS", "/EHsc", "/Gy", "/O2"]
_r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "pairs", _RR, "--"] + _RRF,
                    capture_output=True, text=True, cwd=HERE)
if VERBOSE:
    print(_r.stdout[-4000:], _r.stderr[-2000:])
_rows, _ctx = {}, None
for _ln in _r.stdout.splitlines():
    _m = re.match(r"## (\w+):", _ln)
    if _m:
        _ctx = _m.group(1)
        continue
    _m = re.match(r"  (\w+)\s+code (\d+) \(bag (\d+), regs (\d+), ([+-]\d+) instr\)\s+(.*)$", _ln)
    if _m and _ctx:
        _rows[(_ctx, _m.group(1))] = (int(_m.group(2)), int(_m.group(3)), _m.group(6).split())
def _has(key, *edits):
    row = _rows.get(key)
    return row is not None and all(e in row[2] for e in edits)
check("realize rel: a statement moved past a call changes who interferes (interf:a:s, nointerf:s:t), same instructions",
      (_has(("itf", "late"), "interf:a:s", "nointerf:s:t"), _rows.get(("itf", "late"), (0, 9))[1]), (True, 0))
check("realize rel: p's last use ahead of the call gives p eax, ecx, edx (canreg:p:eax)",
      _has(("cr", "early"), "canreg:p:eax", "canreg:p:ecx", "canreg:p:edx"), True)
check("realize rel: n read after the calls (equal arms) now interferes with p and s",
      (_has(("nr", "eqif"), "interf:n:p", "interf:n:s"), _rows.get(("nr", "eqif"), (0, 9))[1] <= 2), (True, True))
check("realize rel: noreg: / canreg: are listed (a value now live across a call loses eax; one that no longer is, gains it)",
      (_has(("itf", "late"), "noreg:a:eax", "noreg:a:ecx"), _has(("cp", "copy"), "canreg:n:eax")), (True, True))
check("realize rel: a parameter copied into the loop's variable is a copy partner and code 0 (copy:k:n)",
      (_has(("cp", "copy"), "copy:k:n"), _rows.get(("cp", "copy"), (9,))[0]), (True, 0))
check("realize rel: a call result in its own web is one preference for eax (pref:t:eax:+1)",
      _has(("pf", "split"), "pref:t:eax:+1"), True)

_RZ = [sys.executable, os.path.join(HERE, "tools", "realize.py"), "edit", _RR]
def _edit(func, edit, cons, *more):
    r_ = subprocess.run(_RZ + ["--func", func, edit, "--constructs", cons, "--jobs", "3"] + list(more) + ["--"] + _RRF,
                        capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r_.stdout[-3000:], r_.stderr[-1500:])
    return r_.stdout
def _yes(out, fam):
    return bool(re.search(r"^  (?:yes|exact)\s+%s\b" % fam, out, re.M))
check("realize edit interf:a:s: moving `t = a + 1` down realises it (move)", _yes(_edit("itf__base", "interf:a:s", "move"), "move"), True)
check("realize edit nointerf:a:s: moving the statement up realises it (move)", _yes(_edit("itf__late", "nointerf:a:s", "move"), "move"), True)
check("realize edit canreg:p:eax: the member read into a local first (hoist, only with --hoist-mem)",
      (_yes(_edit("cr__base", "canreg:p:eax", "hoist", "--hoist-mem"), "hoist"), _yes(_edit("cr__base", "canreg:p:eax", "hoist"), "hoist")), (True, False))
check("realize edit noreg:n:eax: equal arms on a later statement realise it (eqif)", _yes(_edit("nr__base", "noreg:n:eax", "eqif"), "eqif"), True)
check("realize edit copy:k:n: a copy of the parameter realises it (copy)", _yes(_edit("cp__base", "copy:k:n", "copy"), "copy"), True)
check("realize edit pref:s:eax:+1: the call result in its own web (callsplit)", _yes(_edit("pf__base", "pref:s:eax:+1", "callsplit"), "callsplit"), True)
check("realize edit pref:t:eax:-1: the call result folded into its use (uncallsplit)", _yes(_edit("pf__split", "pref:t:eax:-1", "uncallsplit"), "uncallsplit"), True)
check("realize edit nocopy:k:n: the copy removed (uncopy)", _yes(_edit("cp__copy", "nocopy:k:n", "uncopy"), "uncopy"), True)
check("realize edit nointerf:n:s: equal arms written once (uneqif)", _yes(_edit("nr__eqif", "nointerf:n:s", "uneqif"), "uneqif"), True)
