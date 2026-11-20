# Probe checks for docs/c2/regalloc_cost.md (priorities, tie-breaks, webs, call clobbers, x87 joins).
# Sources: tests/data/rules/RC_*.cpp. Exec'd by tests/run_tests.py (probe_modules) with its helpers in scope.


def rc_defs(proc):
    """{argument pushed before a call: register the call's eax result is moved into} for `push K; call; mov R, eax`
    sequences (the scheduler may put the next push before the mov, so the mov belongs to the latest call)."""
    out, push, arg = {}, None, None
    for ln in proc.splitlines():
        m = re.search(r"\bpush\s+(\d+)\s*$", ln)
        if m:
            push = int(m.group(1))
        elif re.search(r"\bcall\s", ln):
            arg, push = push, None
        else:
            m = re.search(r"\bmov\s+(e\w\w), eax\s*$", ln)
            if m and arg is not None and arg not in out:
                out[arg] = m.group(1)
    return out


def rc_movs(proc):
    return [m for m in re.findall(r"\bmov\s+(e\w\w), eax\s*$", proc, re.M)]


def rc_regs(out):
    """regs report -> {function prefix: [(var, prio, refs, reg), ...]} in pop order."""
    res = {}
    for m in re.finditer(r"^\?(\w+)@@\S*\s+\(\d+ globregs.*?\n(.*?)(?=^\?|\Z)", out, re.M | re.S):
        res[m.group(1)] = [(r[0], int(r[1]), int(r[2]), r[3]) for r in
                           re.findall(r"^\s+\d+\s+(\S+)\s+\S+\s+(-?\d+)\s+-?\d+\s+(\d+)\s+\d+\s+(\S+)",
                                      m.group(2), re.M)]
    return res


ALL4 = lambda v: {k: v for k in ("14.00", "15.00", "13.10.3077", "13.10.3052")}

# 4.3 / 5: equal priorities pop by defPos descending: earlier def in one block, def in the later block across blocks
asm = rule_compile("RC_tie.cpp")
got = (rc_defs(rule_proc(asm, "?ti_blk@@")), rc_defs(rule_proc(asm, "?ti_xblk@@")))
check("RC_tie: ties: one block a,b,c / across blocks b (later block) first", got,
      ALL4(({1: "esi", 2: "edi", 3: "ebx"}, {1: "edi", 3: "esi", 2: "esi"}))[RKEY])

# 9: declaration order does not matter, def order does
got = tuple(rc_defs(rule_proc(rule_compile("RC_decl.cpp", *f), "?dc@@")) for f in ([], ["/DDECLSWAP"], ["/DINITSWAP"]))
check("RC_decl: a=g(1),b=g(2) regs: plain / decl swapped / init swapped", got,
      ALL4(({1: "esi", 2: "edi"}, {1: "esi", 2: "edi"}, {2: "esi", 1: "edi"}))[RKEY])

# 2: a global is a candidate: loaded before a call-free loop and stored after it; in memory with a call in the loop
asm = rule_compile("RC_global.cpp")
gl, gc = rule_proc(asm, "?gl_loop@@"), rule_proc(asm, "?gl_call@@")
loop = re.search(r"^\$L\w*@?\w*:$(.*?)\bjl\s", gl, re.M | re.S)
got = (re.findall(r"\b(mov)\s+(?:e\w\w, DWORD PTR \?G@@3HA|DWORD PTR \?G@@3HA, e\w\w)", gl),
       bool(loop and "?G@@3HA" in loop.group(1)), bool(re.search(r"\badd\s+DWORD PTR \?G@@3HA", gc)))
check("RC_global: G load+store around loop / G in loop body / G in memory with call", got,
      ALL4((["mov", "mov"], False, True))[RKEY])

# 9 (M&B 6.5): a callee defined in the unit (before or after, static or not) clobbers only what it uses
got = tuple(tuple(rc_movs(rule_proc(rule_compile("RC_callee.cpp", *f), "?cv@@"))[:2])
            for f in ([], ["/DEXT"], ["/DAFTER"], ["/DDECL"]))
check("RC_callee: a, b across leaf(): static / extern / defined after / declared", got,
      ALL4((("ecx", "edx"),) * 3 + (("esi", "edi"),))[RKEY])

# 2 / 9: naming a global's read: the local copy outranks the global's own web
asm = rule_compile("RC_gname.cpp")
got = tuple((re.findall(r"\bmov\s+(e\w\w), DWORD PTR \?G@@3HA", rule_proc(asm, p)) or [None])[0]
            for p in ("?gn0@@", "?gn1@@"))
check("RC_gname: register of G: read in the loop / local copy", got,
      {"14.00": ("edi", "edx"), "15.00": ("edx", "ecx"), "13.10.3077": ("edi", "edx"),
       "13.10.3052": ("edi", "edx")}[RKEY])

# 8: x87 join takes the fall-through arm's layout; the other arm gets the fxch (14.00/15.00; 13.10 maps with FPColor)
p = rule_proc(rule_compile("RC_x87join.cpp"), "?j1@@")
ins = re.findall(r"^.*?\t (\w+)(?:\t (.*?))?\s*(?:;.*)?$", p, re.M)
k1 = [i for i, x in enumerate(ins) if "?g1@@3NA" in x[1]]
k2 = [i for i, x in enumerate(ins) if "?g2@@3NA" in x[1]]
got = (" ".join(ins[k1[0] - 1]).strip() if k1 else None, " ".join(ins[k2[0] + 1]).strip() if k2 else None)
check("RC_x87join: before the g1 store / after the g2 store", got,
      {"14.00": ("fxch ST(1)", "fxch ST(1)"), "15.00": ("fxch ST(1)", "fxch ST(1)"),
       "13.10.3077": ("fld ST(1)", "fld ST(0)"), "13.10.3052": ("fld ST(1)", "fld ST(0)")}[RKEY])

# traced (regs): loop weight, webs per lifetime, surviving copies, named global read
R = lambda f: os.path.join(HERE, "tests", "data", "rules", f)
out = tool("regs", R("RC_loopw.cpp"), "--func", "loopw", "--why", "b", "--", "/O2", "/TP")
got = re.findall(r"^\s+block\s+\d+\s+w=(\d+)\s+P=(\d+)\s+refs cost 2", out, re.M)
check("RC_loopw: b's referenced blocks (w, P): entry, loop body", got,
      {"14.00": [("1", "5"), ("2", "3")], "15.00": [("1", "5"), ("2", "3")],
       "13.10.3077": [("1", "4"), ("2", "2")], "13.10.3052": [("1", "4"), ("2", "2")]}[RKEY])
rg = rc_regs(tool("regs", R("RC_web.cpp"), "--", "/O2", "/TP"))
got = ([(v, n) for v, _, n, _ in rg.get("nw", [])], [(v, n) for v, _, n, _ in rg.get("tw", [])])
check("RC_web: x = q(x) two webs / a += t one web (var, refs)", got,
      ALL4(([("x", 4), ("x", 4)], [("a", 6)]))[RKEY])
rg = rc_regs(tool("regs", R("RC_copy.cpp"), "--", "/O2", "/TP"))
check("RC_copy: b = a survives: webs (var, prio, refs)", [(v, p, n) for v, p, n, _ in rg.get("cp", [])],
      ALL4([("a", 18, 3), ("b", 18, 3), ("a", 12, 2)])[RKEY])
rg = rc_regs(tool("regs", R("RC_gname.cpp"), "--", "/O2", "/TP"))
pr = lambda f, v: [(p, n) for w, p, n, _ in rg.get(f, []) if w == v]
check("RC_gname: (prio, refs) of G's web in gn0 / of l in gn1", (pr("gn0", "?G@@3HA"), pr("gn1", "l")),
      {"14.00": ([(24, 1)], [(32, 2)]), "15.00": ([(60, 3)], [(78, 4)]),
       "13.10.3077": ([(20, 1)], [(29, 2)]), "13.10.3052": ([(20, 1)], [(29, 2)])}[RKEY])
