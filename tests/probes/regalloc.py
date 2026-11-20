# Probe checks for docs/c2/regalloc.md ([I] rules turned [M]/[C]) and the regalloc items of docs/c2/validation.md
# "Not tested, or open". Sources: tests/data/rules/RA_*. Exec'd by tests/run_tests.py (probe_modules) with its
# helpers in scope (check, rule_compile, rule_proc, tool, RKEY, RULES, HERE).

RA_ALL4 = lambda v: {k: v for k in ("14.00", "15.00", "13.10.3077", "13.10.3052")}


def ra_regs(out):
    """regs report -> {function name: [(var, prio, defOrd, reg), ...]} in pop order (also C names `_f`)."""
    res = {}
    for m in re.finditer(r"^[?_](\w+?)(?:@@\S*)?\s+\((\d+) globregs[^\n]*\n(.*?)(?=^[?_]\w|\Z)", out, re.M | re.S):
        res[m.group(1)] = [(r[0], int(r[1]), int(r[2]), r[3]) for r in
                           re.findall(r"^\s+\d+\s+(\S+)\s+\S+\s+(-?\d+)\s+-?\d+\s+\d+\s+(\d+)\s+(\S+)",
                                      m.group(3), re.M)]
    return res


def ra_ngr(out):
    return {m.group(1): int(m.group(2)) for m in re.finditer(r"^\?(\w+)@@\S*\s+\((\d+) globregs", out, re.M)}


def ra_lea_ebp(asm):
    """N of `lea ebp, [esp-N]` (the EBP adjustment), None for `mov ebp, esp`."""
    m = re.search(r"\blea\s+ebp, DWORD PTR \[esp-(\d+)\]", asm)
    return int(m.group(1)) if m else None


# 3.2 / immcand.md 2.3 (validation.md open item): a constant used once outside loops is no web; used twice it is;
# used once in a loop it is costed (counts in P like a variable would) but never popped.
out = tool("regs", os.path.join(RULES, "RA_immweb.cpp"), "--func", "i_", "--why", "i", "--", "/O2", "/TP")
imm = lambda f: len(re.findall(r"^\s+\d+\s+imm0 .*= 1000 ", out.split("?" + f + "@@")[1].split("\n?")[0], re.M))
r = ra_regs(out)
pr = lambda f: [x[1] for x in r.get(f, []) if x[0] == "i"]
check("RA_immweb: 1000 webs once / twice / once in a loop; prio(i) with 1000 vs k",
      (imm("i_once"), imm("i_twice"), imm("i_loop"), pr("i_loop"), pr("i_loopv")),
      {"14.00": (0, 1, 0, [37], [37]), "15.00": (0, 1, 0, [37], [37]),
       "13.10.3077": (0, 1, 0, [32], [32]), "13.10.3052": (0, 1, 0, [32], [32])}[RKEY])

# 4 / 4.2 (validation.md open item): +100*benefit on a neighbour's last free register. c (a byte value live
# across calls) has only ebx; the third call-crossing value then takes ebp instead of ebx. /DINTC: no such term.
asm, asmi, asmo = (rule_proc(rule_compile("RA_lastreg.cpp", *f), "?lr@@") for f in ([], ["/DINTC"], ["/DINTC", "/Oy-"]))
dreg = lambda p: (re.findall(r"\bmov\s+(e\w\w), DWORD PTR _d\$", p) or [None])[0]
check("RA_lastreg: d's register: byte c / int c / int c with /Oy- (no ebp colour)",
      (dreg(asm), dreg(asmi), dreg(asmo), bool(re.search(r"\bmov\s+bl, al", asm))),
      RA_ALL4(("ebp", "ebx", "ebx", True))[RKEY])
out = tool("regs", os.path.join(RULES, "RA_lastreg.cpp"), "--func", "lr", "--", "/O2", "/TP")
check("RA_lastreg: the third pop sees ebx:600 (100 x c's benefit 6)",
      bool(re.search(r"^\s+3\s+d\s.*\bebx:600\b", out, re.M)), RA_ALL4(True)[RKEY])

# 3.3 item 2: a same-TU callee's kill set is its written registers (ltcg.md 3), also outside /GL and when the
# callee is defined after the caller: x stays in ecx across the call; with an external callee it takes esi.
got = tuple((re.findall(r"\blea\s+(e\w\w), DWORD PTR \[eax\+eax\*2\]",
                        rule_proc(rule_compile("RA_callee.cpp", "/D" + v), "?caller@@")) or [None])[0]
            for v in ("EXT", "PUB", "STATIC", "AFTER"))
check("RA_callee: x across leaf(): extern / defined before / static / defined after", got,
      RA_ALL4(("esi", "ecx", "ecx", "ecx"))[RKEY])

# 8 (M&B rule 2): an unnamed call result kept across a call is not a globreg; naming it makes one (same code here)
out = tool("regs", os.path.join(RULES, "RA_callres.cpp"), "--func", "cr_", "--", "/O2", "/TP")
asm = rule_compile("RA_callres.cpp")
ins = lambda p: re.findall(r"^\s+[0-9a-f]{5}\s+[0-9a-f ]+\s+(\w+\s.*?)\s*(?:;.*)?$", p, re.M)
g = ra_ngr(out)
check("RA_callres: globregs bare / named; same instructions", (g.get("cr_bare"), g.get("cr_named"),
      ins(rule_proc(asm, "?cr_bare@@")) == ins(rule_proc(asm, "?cr_named@@"))), RA_ALL4((0, 1, True))[RKEY])

# 3.4 / 8 ("Declaration order of locals"): declaration order is no input, the order of the defs is
asm = rule_compile("RA_declorder.cpp")


def xreg(f):
    """the register pushed for the first v(x) call"""
    ops = re.findall(r"\t (push|call)\t (\S+)", rule_proc(asm, "?" + f + "@@"))
    k = next((i for i, o in enumerate(ops) if o == ("call", "?v@@YAXH@Z")), None)
    return ops[k - 1][1] if k else None


check("RA_declorder: x's register: x,y / y,x declared / x first init / y first init",
      tuple(xreg(f) for f in ("d_xy", "d_yx", "d_ix", "d_iy")), RA_ALL4(("esi", "esi", "esi", "edi"))[RKEY])

# 3.4: the cross-block defOrd tie for XMM values (class 6): b (later block) before c, both prio 28
out = tool("regs", os.path.join(RULES, "RA_xmmtie.c"), "--func", "tx", "--all-classes", "--", "/O2", "/arch:SSE2")
r = ra_regs(out).get("tx", [])
check("RA_xmmtie: first two XMM pops (prio, defOrd)", [(x[1], x[2]) for x in r[:2]],
      RA_ALL4([(28, 23), (28, 8)])[RKEY])

# 3.6.9 "The numbering": record numbers are not first-mention order for parameters
d = tempfile.mkdtemp()
tool("regs", os.path.join(RULES, "RA_recnum.cpp"), "--func", "n_", "--export", os.path.join(d, "%s.json"),
     "--", "/O2", "/TP")
nums = {}
import glob
for fn in glob.glob(os.path.join(d, "*.json")):
    j = json.load(open(fn))
    nums[j["func"].split("@@")[0][1:]] = "".join(n for _, n in sorted({(x["num"], x["name"]) for x in j["decisions"]
                                                                          if x["t"] == "so" and x["npass"] == 1 and x["name"] in "abxy"}))
check("RA_recnum: record-number order in n_late n_ab n_ba n_sig n_aa n_expr n_glob n_vb",
      tuple(nums.get(f) for f in ("n_late", "n_ab", "n_ba", "n_sig", "n_aa", "n_expr", "n_glob", "n_vb")),
      RA_ALL4(("abxy", "baxy", "abxy", "baxy", "baxy", "baxy", "baxy", "abxy"))[RKEY])

# validation.md open item, frame.md 4.9: the EBP adjustment. /O1. With /GS: ebp so that the last parameter is at
# [ebp+0x7c] (N = 0x78 - 12), or ebp = esp+0x80 after the frame is allocated, or the GS candidate (600+400 case).
got = tuple(ra_lea_ebp(rule_proc(rule_compile("RA_ebpadj.cpp", "/O1", "/GS", *f.split()), "?ea@@"))
            for f in ("/DNB=200", "/DNB=512", "/DNB=4000", "/DNB=200 /DNI=100", "/DNB=600 /DNI=100"))
check("RA_ebpadj: lea ebp N for buf 200 / 512 / 4000 / 200+ints / 600+ints (/GS)", got,
      {"14.00": (108, 400, 3888, 108, 736), "15.00": (108, 400, 3888, 108, 736),
       "13.10.3077": (108, None, None, 108, 736), "13.10.3052": (108, None, None, 108, 736)}[RKEY])
got = tuple(ra_lea_ebp(rule_proc(rule_compile("RA_ebpadj2.cpp", "/O1", "/GS-", "/DNP=%d" % n), "?ec@@"))
            for n in (0, 1, 5, 20))
check("RA_ebpadj2: lea ebp N with 0 / 1 / 5 / 20 parameters (0x78 - 4n; none at 20)", got,
      RA_ALL4((120, 116, 100, None))[RKEY])
