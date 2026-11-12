#!/usr/bin/env python3
"""Regression tests for the tracer, the reports and rasim. usage: uv run python tests/run_tests.py [-v]

Each test compiles tests/data/*.cpp with the traced c2 (game flags /TP /MT /GS /EHsc /Gy /O2) and compares a
number the tools report with the value measured when the tools were validated. A failure means the setup is
wrong (another c2.dll, a wine problem) or a tool regressed.
"""
import json, os, re, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "tests", "data", "eh_helpers.cpp")
FLAGS = ["--", "/TP", "/MT", "/GS", "/EHsc", "/Gy"]
VERBOSE = "-v" in sys.argv
results = []


def tool(*args):
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2trace.py")] + list(args),
                       capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    return r.stdout + r.stderr


def check(name, got, want):
    good = got == want
    results.append(good)
    print("  %-58s %s%s" % (name, "ok" if good else "FAIL", "" if good else "  (got %r, want %r)" % (got, want)), flush=True)


def globregs(out, func):
    m = re.search(r"^\?%s@@\S*\s+\((\d+) globregs" % func, out, re.M)
    return int(m.group(1)) if m else None


print("regression tests (each line is one traced compile or one rasim run)")
out = tool("regs", DATA, *FLAGS)
check("regs: EH function, plain helper: 6 values", globregs(out, "eA"), 6)
check("regs: EH function, reference-binding helper: 7 values", globregs(out, "eB"), 7)
check("regs: no EH frame, plain helper: 4 values", globregs(out, "nA"), 4)
check("regs: no EH frame, reference-binding helper: 4 values", globregs(out, "nB"), 4)

out = tool("scratch", DATA, "--func", "?eA@@", *FLAGS)
seq = " ".join(m.group(2) + ("" if m.group(1) == "ROT" else "*")
               for m in re.finditer(r"^\s+\d+\s+\S+\s+\S+\s+(ROT|pref|fit)\s+(\S+)", out, re.M))
check("scratch: eA picks (rotation order, * = copy hint/first fit)", seq, "ecx edx eax* ecx edx eax ecx")
out = tool("scratch", DATA, "--func", "?eB@@", *FLAGS)
seq = " ".join(m.group(2) + ("" if m.group(1) == "ROT" else "*")
               for m in re.finditer(r"^\s+\d+\s+\S+\s+\S+\s+(ROT|pref|fit)\s+(\S+)", out, re.M))
check("scratch: eB picks (the binding is pick-neutral)", seq, "ecx edx ecx edx eax ecx")

snap = os.path.join(tempfile.mkdtemp(), "eA.json")
tool("regs", DATA, "--func", "?eA@@", "--export", snap, *FLAGS)
rasim = os.path.join(HERE, "tools", "rasim", "target", "release", "rasim")
r = subprocess.run([rasim, "check", snap], capture_output=True, text=True).stdout
m = re.search(r"(\d+) mismatches; colouring order: (\d+) of (\d+)", r)
check("rasim check: 0 mismatches", int(m.group(1)) if m else None, 0)
check("rasim check: whole colouring order predicted", (m.group(2) == m.group(3)) if m else None, True)
m = re.search(r"registers: (\d+) of (\d+) int colourings predicted from the simulated state .*; (\d+) state divergences", r)
check("rasim check: every register predicted (ChoosePhysReg model)", m.groups() if m else None, ("6", "6", "0"))
r = subprocess.run([rasim, "need", snap, "reg:k=esi"], capture_output=True, text=True).stdout
check("rasim need reg:k=esi: colouring k before h, or no h-k interference", (
    bool(re.search(r"^  before:k:h\s+gap 24\b", r, re.M)), bool(re.search(r"^  nointerf:h:k\s+no other register changes", r, re.M))),
    (True, True))

out = tool("il", DATA, "--func", "?eA@@", "--at", "@GrgIdentifyCandidates@4", "--count", "OPCALL", *FLAGS)
m = re.search(r"@GrgIdentifyCandidates@4\s+(\d+)", out)
check("il: 7 calls (3 use, take, InterlockedIncrement, D ctor and dtor)", int(m.group(1)) if m else None, 7)

ssnap = os.path.join(tempfile.mkdtemp(), "eA_scratch.json")
out = tool("scratch", DATA, "--func", "?eA@@", "--why", "--export", ssnap, *FLAGS)
m = re.search(r"^\s+1\s+eax>edx\s+-\s+ROT\s+ecx\s+ecx,edx\s+\S+\s+\[eax busy phys\]", out, re.M)
check("scratch --why: pick 1 skips eax (held by a coloured value), cursor -> edx", bool(m), True)
r = subprocess.run([rasim, "scratch", ssnap], capture_output=True, text=True).stdout
m = re.search(r"scratch: (\d+) of (\d+) integer picks predicted .*cursor right at (\d+) of \d+; (\d+) not modelled", r)
check("rasim scratch: every pick and cursor predicted from the pick rule", m.groups() if m else None, ("7", "7", "7", "0"))
r = subprocess.run([rasim, "scratch", ssnap, "drop:1"], capture_output=True, text=True).stdout
check("rasim scratch drop:1: only pick 2 moves (edx -> eax), then the rotation is back in step", (
    bool(re.search(r"^\s+2\s+\S+\s+edx -> eax\b", r, re.M)), bool(re.search(r"what-if: 1 of 7 integer picks change", r))), (True, True))

# scratch --phase (tracer.md): our picks against an "original" listing, per switch case, and the re-alignment scan.
# The original is a variant with one more temporary in case 3 (h[1] = g[2]); every case before it must agree, the
# first difference must come after it, and the scan's best single edit must sit in case 3 or the case after it.
PH = os.path.join(HERE, "tests", "data", "scratchphase.c")
phd = tempfile.mkdtemp()
phv = os.path.join(phd, "scratchphase_v.c")
open(phv, "w").write(open(PH).read().replace("case 3: g[a + 12] = p[b + 19] + h[2];", "case 3: g[a + 12] = p[b + 19] + h[2]; h[1] = g[2];"))
tool("scratch", phv, "--func", "big", "--keep", os.path.join(phd, "v"), *FLAGS)
out = tool("scratch", PH, "--func", "big", "--keep", os.path.join(phd, "s"), "--phase", os.path.join(phd, "s", "scratchphase.asm"),
           "--asm-func", "big", "--no-scan", *FLAGS)
m = re.search(r"^# (\d+) groups with readable picks: (\d+) exact, (\d+) differ", out, re.M)
check("scratch --phase against our own listing: every case exact", (m.group(2) == m.group(1), int(m.group(1)) >= 30) if m else None, (True, True))
out = tool("scratch", PH, "--func", "big", "--phase", os.path.join(phd, "v", "scratchphase_v.asm"), "--asm-func", "big", "--top", "3", *FLAGS)
rows = re.findall(r"^case\[(\d+)\]\s+(exact|DIFF)", out, re.M)
first = next((int(c) for c, st in rows if st == "DIFF"), None)
check("scratch --phase: cases 0-3 agree, the first difference comes after case 3",
      (all(st == "exact" for c, st in rows if int(c) <= 3), first is not None and first > 3), (True, True))
m = re.search(r"^edit (?:ins|drop):\d+ group case\[(\d+)\] \S+\s+net \+(\d+)", out, re.M)
check("scratch --phase scan: the best single pick edit is at case 3 / the first differing case and re-aligns cases",
      (m is not None and int(m.group(1)) in (3, first), m is not None and int(m.group(2)) >= 1), (True, True))

# scheduler, FP/XMM classes and LTCG: the same checks on every c2 build (C2_BUILD), docs/c2/port-1310.md section 10
def script(name, *args):
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", name)] + list(args), capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    return r.stdout + r.stderr


out = script("c2sched.py", DATA, "--func", "?nA@@", "--verify", *FLAGS)
got = tuple(m.group(1) == m.group(2) for m in re.finditer(r"^rule \d.*: (\d+) of (\d+) (?:nodes|picks) match", out, re.M))
check("sched --verify: height, static priority and selection rules all match", got[-3:] if len(got) >= 3 else got, (True, True, True))

FP = os.path.join(HERE, "tests", "data", "fpclasses.c")
out = tool("regs", FP, "--all-classes", "--", "/arch:SSE2")
fx, _, hx = out.partition("\n_h ")
check("regs --all-classes: XMM values coloured in class 6 (4 pops)", len(re.findall(r"\bxmm\d\s.*\[class 6\]", fx)), 4)
check("regs --all-classes: x87 values reported (Color's x87 class, FPColor on 13.10)", len(re.findall(r"\[x87", hx)) >= 5, True)

out = script("c2ltcg.py", "regs", os.path.join(HERE, "tests", "data", "ltcg_a.c"), "--with", os.path.join(HERE, "tests", "data", "ltcg_b.c"))
m = re.search(r"^_helper\s+\((\d+) globregs", out, re.M)
check("ltcg: Color traced inside link.exe (/GL + /LTCG), helper has 7 values", int(m.group(1)) if m else None, 7)

# LTCG rules (docs/c2/ltcg.md): kill sets and the custom convention, in a two-object probe; listing from the image
KA, KB = (os.path.join(HERE, "tests", "data", f) for f in ("ltcg_kill_a.c", "ltcg_kill_b.c"))
kdir = tempfile.mkdtemp()
out = script("c2ltcg.py", "asm", KA, "--with", KB, "--out", kdir)


def lproc(name):
    m = re.search(r"^%s\tPROC NEAR\n(.*?)^%s\tENDP" % (re.escape(name), re.escape(name)), out, re.M | re.S)
    rows = [re.sub(r"\t; .*$", "", l).split("\t") for l in m.group(1).splitlines()] if m else []
    return [(r[-2].strip() + " " + r[-1].strip()).strip() for r in rows if len(r) >= 4]


check("ltcg asm: k_eax, reached only from /GL code, takes its argument in eax (custom convention)",
      lproc("_k_eax")[:1], ["lea eax, [eax + eax*4]"])
check("ltcg asm: k_std, exported, keeps the standard convention (argument on the stack)",
      lproc("_k_std")[:1], ["mov eax, dword ptr [esp + 4]"])
check("ltcg kill set: a stays in edx across k_eax / k_std (they write only eax), in esi across k_ext",
      tuple(any(l == "mov dword ptr [ecx + 8], %s" % r for l in lproc(f)) for f, r in
            (("_c_eax", "edx"), ("_c_std", "edx"), ("_c_ext", "esi"))), (True, True, True))
out = script("c2ltcg.py", "regs", KA, "--with", KB, "--func", "_c_")
cands = {}
for blk in out.split("\n\n"):
    m = re.search(r"^\s+\d+\s+a\s+param\s+(?:-?\d+\s+){4}\S+\s+(.*)$", blk, re.M)
    n = re.search(r"^(_c_\w+)\s+\(", blk, re.M)
    if m and n:
        cands[n.group(1)] = m.group(1)
check("ltcg regs: edx is a candidate for a in c_eax (kill set {eax}), not in c_ext (no body: all killed)",
      ("edx:" in cands.get("_c_eax", ""), "edx:" in cands.get("_c_ext", "x edx:")), (True, False))
out = script("c2ltcg.py", "regmap", KA, "--with", KB, "--func", "_c_std", "--asm-vs", os.path.join(kdir, "_c_std.s"),
             "--asm-func", "_c_std")
m = re.search(r"# (\d+) variables checked, (\d+) agree", out)
check("ltcg regmap: the image listing aligns with our own build's disassembly (all variables agree)",
      bool(m) and m.group(1) == m.group(2) and int(m.group(1)) >= 3, True)

# combined what-ifs (tracer.md "Combined what-ifs"): a source-level edit carried through priority, order and register
r = subprocess.run([rasim, "check", snap], capture_output=True, text=True).stdout
check("rasim check: the order recomputed from the priorities is the traced one (edits can be simulated)",
      "edits: the colouring order recomputed from the priorities is the traced one" in r, True)
r = subprocess.run([rasim, "need", snap, "reg:k=esi"], capture_output=True, text=True).stdout
check("rasim need reg:k=esi: no single source edit; the pair search finds three more references to k",
      bool(re.search(r"^  ref:k:1  \+  ref:k:1:4\s+also moves: h esi->edi", r, re.M)), True)
wantf = os.path.join(os.path.dirname(snap), "want.txt")
open(wantf, "w").write("k=esi\n")
r = subprocess.run([rasim, "want", snap, wantf], capture_output=True, text=True).stdout
check("rasim want (targets from a file): the same answer",
      bool(re.search(r"^  ref:k:1  \+  ref:k:1:4\s+also moves: h esi->edi", r, re.M)), True)
r = subprocess.run([rasim, "regs", snap, "ref:k:1:6"], capture_output=True, text=True).stdout
pred = dict(re.findall(r"^\s+\d+\s+(\S+)\s+(e[a-ds][xip])\s+(?:e[a-ds][xip]|-)\s", r, re.M))
var = os.path.join(os.path.dirname(snap), "eh_var.cpp")
open(var, "w").write(open(DATA).read().replace("void eA(H* h, int k) { BODY(incA) }",
                                               "void eA(H* h, int k) { use(k, k, k); BODY(incA) }"))
out = tool("regs", var, "--func", "?eA@@", *FLAGS)
real = dict(re.findall(r"^\s+\d+\s+(\S+)\s+\S+\s+-?\d+\s+-?\d+\s+\d+\s+\d+\s+(e[a-ds][xip])\s", out, re.M))
check("combined what-if vs a real compile: ref:k:1:6 gives the 6 registers of eA with use(k, k, k) added (k esi, h edi)",
      (pred == real, pred.get("k"), pred.get("h"), len(real)), (True, "esi", "edi", 6))
# need/want's fast path (restart from the unedited run's saved states, stop once converged; tracer.md "Speed of
# `need reg:`") prints exactly what the reference path prints (RASIM_REF=1: a full state copy and run per candidate)
same = []
for args in (["need", snap, "reg:k=esi"], ["need", snap, "reg:k=esi", "reg:h=edi", "--depth", "3"],
             ["want", snap, wantf], ["need", snap, "reg:k=esi", "pref:h:edi"]):
    a = subprocess.run([rasim] + args, capture_output=True, text=True)
    b = subprocess.run([rasim] + args, capture_output=True, text=True, env=dict(os.environ, RASIM_REF="1"))
    same.append((a.stdout == b.stdout, a.returncode == b.returncode, len(a.stdout) > 100))
check("rasim need/want: the fast path's output is the reference path's (4 searches on eA)", same, [(True, True, True)] * 4)

# spill / defer / split decisions predicted (regalloc.md 3.6, tracer.md "rasim check --predict"), on every c2 build
# (13.10 and 15.00: the decision hooks of their build tables, port-1310.md section 15, port-1500.md section 12)
sys.path.insert(0, os.path.join(HERE, "tools"))
import c2build
BUILD = c2build.name()
if "decisions" in json.load(open(snap)):
    r = subprocess.run([rasim, "check", snap], capture_output=True, text=True).stdout
    m = re.search(r"^decisions: (\d+) of (\d+) predicted .*; (\d+) state differences", r, re.M)
    check("rasim check: fourth line, every decision of eA predicted", m.groups() if m else None, ("24", "24", "0"))
    psrc = os.path.join(os.path.dirname(snap), "predict_f3.c")
    open(psrc, "w").write("""extern unsigned char g[4096]; extern int gi[4096]; int h(int, int);
int f3(unsigned char *p, int n, int k) { unsigned char a=p[0],b=p[1],c=p[2],d=p[3]; int x=p[4],y=p[5],z=p[6],w=p[7],i,j;
  for(i=0;i<n;i++){ for(j=0;j<k;j++){ a+=g[i+j]; b^=(unsigned char)(x>>j); c+=(unsigned char)h(y,z); d^=c; x+=a*b; y^=d; z+=w/(j+1); w+=z<<(j&3);} gi[i]=x+y; } return a+b+c+d+x+y+z+w; }
""")
    psnap = os.path.join(os.path.dirname(snap), "predict_f3.json")
    tool("regs", psrc, "--func", "_f3", "--export", psnap)
    r = subprocess.run([rasim, "check", psnap, "--predict"], capture_output=True, text=True).stdout
    m = re.search(r"^\s+all\s+\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", r, re.M)
    check("rasim check --predict: 9 rounds of byte-register code: list head, branch, spill/defer, register, no-reg sets, "
          "cut sets, split rounds, SpillObvious", m.groups() if m else None,
          ("34/34", "34/34", "24/24", "10/10", "4/4", "4/4", "8/8", "44/44" if BUILD.startswith("13.10") else "45/45"))
    m = re.search(r"state: (\d+) places", r)
    check("rasim check --predict: the model's records never differ from c2's", m.group(1) if m else None, "0")
    m = re.search(r"record numbers .*?: (\d+) of (\d+) records .*\n.*ascending number: (\S+); enterloads at one place in "
                  r"ascending piece number: (\S+)", r)
    check("rasim check --predict: every piece's record number (LIFO free list), parent order, reload order (f3)",
          m.groups() if m else None, ("28", "28", "3/3", "4/4"))
    # the reload order after a loop follows the record numbers, which a dead mention of y moves (regalloc.md 3.6.9)
    SN = os.path.join(HERE, "tests", "data", "splitnum.c")
    order = {}
    for fn_ in ("_f4", "_f6"):
        ns = os.path.join(os.path.dirname(snap), "sn%s.json" % fn_)
        tool("regs", SN, "--func", fn_, "--export", ns)
        dec = json.load(open(ns)).get("decisions", [])
        name = {d["num"]: d.get("name") for d in dec if d["t"] == "so" and d["npass"] == 1}
        parent = {d["gr"]: d["pnum"] for d in dec if d["t"] == "piece"}
        order[fn_] = "".join(name.get(parent.get(d["gr"]), "") for d in dec if d["t"] == "reload" and
                             name.get(parent.get(d["gr"])) in ("x", "y"))
        r = subprocess.run([rasim, "check", ns, "--predict"], capture_output=True, text=True).stdout
        m = re.search(r"record numbers .*?: (\d+) of (\d+) records .*\n.*enterloads at one place in ascending piece "
                      r"number: (\S+)", r)
        order[fn_ + "rasim"] = m.groups() if m else None
    check("splitnum.c: reloads after the loop x, y (f4) and y, x (f6); rasim predicts both",
          (order["_f4"], order["_f6"], order["_f4rasim"], order["_f6rasim"]),
          ("xy", "yx", ("9", "9", "1/1"), ("9", "9", "1/1")))

# immediate candidates (docs/c2/immcand.md) and the --func forms (tracer.md)
IMM = os.path.join(HERE, "tests", "data", "immcand.c")
out = tool("regs", IMM, "--func", "k1", "--block", "1")
check("regs: an immediate candidate shows its constant", len(re.findall(r"imm0 .*= -1 \(const, 1 bytes\)", out)) >= 2, True)
m1 = re.search(r"^\s+\d+\s+x\s+\S+\s+(-?\d+)", out, re.M)
out = tool("regs", IMM, "--func", "k2", "--block", "1")
m2 = re.search(r"^\s+\d+\s+x\s+\S+\s+(-?\d+)", out, re.M)
check("immcand: char -1 shares the pushed -1's candidate (x 24), unsigned char 255 does not (x 16)",
      (m1 and m1.group(1), m2 and m2.group(1), "imm0" in out), ("24", "16", False))
out = tool("regs", DATA, "--func", "::eA", *FLAGS)
check("--func ::name selects the C++ global function", globregs(out, "eA"), 6)

# tie order, every register class in regmap, and rasim's default pass (regalloc.md 3.4, tracer.md `regmap`, `rasim`)
TIE = os.path.join(HERE, "tests", "data", "tieorder.c")
ties = []
for extra in ([], ["--", "/arch:SSE2"]):
    rows = re.findall(r"^\s+\d+\s+\S+\s+\S+\s+(-?\d+)\s+-?\d+\s+\d+\s+(\d+)\s.*\[(int|x87|class 6)", tool("regs", TIE, "--all-classes", *extra), re.M)
    ties += [int(a[1]) > int(b[1]) for a, b in zip(rows, rows[1:]) if a[0] == b[0] and a[2] == b[2]]
check("regs: equal priorities pop the larger def first in the int, x87 and XMM classes (tieorder.c)",
      (all(ties), len(ties) >= 10), (True, True))

kd = tempfile.mkdtemp()
tool("regs", FP, "--keep", kd, "--", "/arch:SSE2")
lst = open(os.path.join(kd, "fpclasses.asm"), errors="replace").read()
got = {}
for fn_ in ("_f", "_h"):
    m = re.search(r"^%s\s+PROC" % fn_, lst, re.M)
    body = lst[m.start():lst.index("ENDP", m.start())] if m else ""
    ins = re.findall(r"^\s+([0-9a-f]{5})\t[0-9a-f ]+\t+ (\w+)[ \t]*([^;\n]*)", body, re.M)
    orig = os.path.join(kd, fn_ + ".s")           # our own listing as the "original": every row must agree
    open(orig, "w").write("".join("%s %s %s\n" % (o, mn, ops.strip()) for o, mn, ops in ins))
    out = tool("regmap", FP, "--func", fn_, "--asm-vs", orig, "--asm-func", fn_, "--all", "--", "/arch:SSE2")
    m = re.search(r"^# by class: (.*)$", out, re.M)
    got[fn_] = dict((c, (int(a), int(b))) for c, a, b in re.findall(r"(\w+) (\d+) of (\d+)", m.group(1))) if m else {}
xm, st = got["_f"].get("xmm", (0, -1)), got["_h"].get("x87", (0, -1))
check("regmap: XMM rows (f) and x87 stack-position rows (h), all agreeing with the same code",
      (xm[0] == xm[1] and xm[1] >= 4, st[0] == st[1] and st[1] >= 4), (True, True))

fsnap = os.path.join(kd, "f.json")
tool("regs", FP, "--func", "_f", "--export", fsnap, "--", "/arch:SSE2")
r = subprocess.run([rasim, "need", fsnap, "s>t"], capture_output=True, text=True)
check("rasim need 's>t' without --pass answers in the XMM pass the two are costed in",
      ("(XMM class) chosen" in r.stdout, bool(re.search(r"^s \(\d+, def \d+\) before t \(", r.stdout, re.M))), (True, True))
r = subprocess.run([rasim, "need", fsnap, "s>n"], capture_output=True, text=True)
check("rasim need 's>n' (XMM against int) says the classes differ instead of answering",
      "different register classes" in r.stdout + r.stderr, True)

# symbol keys (docs/c2/symkeys.md, tools/symkeys.py), on every c2 build: the assignment rules, the exact replay of the
# allocator and a what-if prediction checked against a real compile
def symkeys(*args):
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py")] + list(args),
                       capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    return r.stdout + r.stderr


def symkeys_tests():
    SK = os.path.join(HERE, "tests", "data", "symkeys.cpp")
    out = symkeys("keys", SK, "--brief", *FLAGS)
    row = dict((m.group(1), m.group(2)) for m in re.finditer(r"^\?(k\w+)@\S*\s+(.*?)\s+\[", out, re.M))
    check("symkeys: parameters in reverse, locals by first reference (q declared first)",
          " ".join(row.get("kparams", "").split()[1:6]), "_c:1 _b:2 _a:3 _r:4 _q:5")
    check("symkeys: an unused local takes no key, an initialised dead one does",
          " ".join(row.get("kdead", "").split()[1:5]), "_b:1 _a:2 _d:3 _r:4")
    check("symkeys: inlinee this/params take keys 4-6 (freed), its local 7",
          [w for w in row.get("kthis", "").split() if w.startswith("_r@")], ["_r@S::meth#1:7"])
    check("symkeys: the 2nd expansion reuses the freed parameter keys, its local is next",
          [w for w in row.get("kinl", "").split() if w.startswith("_t@")], ["_t@dot#1:8", "_t@dot#2:9"])
    ka = dict(w.split("@")[0].split(":")[0:1] + [w.rsplit(":", 1)[1]] for w in row.get("kargs", "").split() if w.startswith("_t"))
    check("symkeys: call arguments are inlined right to left (dB's local before dA's)",
          int(ka.get("_tB", 99)) < int(ka.get("_tA", 0)), True)
    out = symkeys("keys", SK, *FLAGS)
    rep = re.findall(r"replay: (\d+)/(\d+) keys reproduced", out)
    check("symkeys: the allocator replay reproduces every traced key (all functions)",
          (len(rep) >= 8, all(a == b for a, b in rep)), (True, True))
    out = symkeys("sites", SK, "--func", "kx87", "--pass", "2", *FLAGS)
    check("symkeys sites: [p+0] (p key 3: hash 0x6000+d) is loaded before s (key 1)",
          bool(re.search(r"1st .*h60[0-9a-f]{2}\n\s+2nd .*_s \(?key 1", out)), True)
    skd = tempfile.mkdtemp()
    var = os.path.join(skd, "symkeys_v.cpp")
    open(var, "w").write(open(SK).read().replace("float d = 0; float r = a * b;", "float d = 0; float e = 1; float r = a * b;")
                         .replace("{ float t = a.x * b.x; return", "{ float e = 2; float t = a.x * b.x; return"))
    for src, js in ((SK, "a.json"), (var, "b.json")):
        symkeys("export", src, "--func", "k", "--out", os.path.join(skd, js), *FLAGS)
    out = symkeys("compare", "--from", os.path.join(skd, "a.json"), "--func", "kdead", "add:frame@_r:1",
                  "--vs", os.path.join(skd, "b.json"))
    m = re.search(r"keys: (\d+) symbols in both; predicted = traced for (\d+)", out)
    check("symkeys compare: a dead local before r predicted exactly (add:frame@_r:1)", m and m.group(1) == m.group(2), True)
    out = symkeys("compare", "--from", os.path.join(skd, "a.json"), "--func", "kinl", "add:frame@_t@dot#1:1",
                  "add:frame@_t@dot#2:1", "--vs", os.path.join(skd, "b.json"))
    m = re.search(r"keys: (\d+) symbols in both; predicted = traced for (\d+)", out)
    check("symkeys compare: a dead local in an inline helper (one per expansion) predicted exactly",
          bool(m) and m.group(1) == m.group(2) and int(m.group(1)) >= 6, True)


def symkeys_more_tests():
    """n-ary sorts, optimiser temporaries, whatif --batch, levers --site (symkeys.md sections 3, 5, 7)."""
    NA = os.path.join(HERE, "tests", "data", "symkeys_nary.cpp")
    skd = tempfile.mkdtemp()
    var = os.path.join(skd, "symkeys_nary_v.cpp")
    open(var, "w").write(open(NA).read().replace("int a = f1(); int b = f2(); int c = f3();", "int c = f3(); int a = f1(); int b = f2();"))
    ja, jb = os.path.join(skd, "a.json"), os.path.join(skd, "b.json")
    symkeys("export", NA, "--func", "k", "--out", ja, *FLAGS)
    symkeys("export", var, "--func", "k", "--out", jb, *FLAGS)
    out = symkeys("sites", "--from", ja, "--func", "kn4", "--pass", "2")
    check("symkeys sites: a + b + c + d is one sort of four operands (a, b, c, d by key)",
          re.findall(r"(?:1st|2nd|3rd|4th) (_\w)", out), ["_a", "_b", "_c", "_d"])
    out = symkeys("compare", "--from", ja, "--func", "kcall3", "swap:_c,_a", "--vs", jb)
    m = re.search(r"(\d+) really changed order, (\d+) of them predicted", out)
    check("symkeys compare: c referenced first reorders the 3-operand add, predicted (swap:_c,_a)",
          m.groups() if m else None, ("1", "1"))
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py"), "whatif", "--from", ja, "--func", "kcall3",
                        "--batch", "-", "--compact"], input="swap:_c,_a\n# comment\nadd:frame@_a:1\n", capture_output=True, text=True)
    check("symkeys whatif --batch: one answer per edit set from one load (the swap flips the 3-operand add)",
          re.findall(r"^set \d+: .*?(\d+) orders flip", r.stdout, re.M), ["1", "0"])
    out = symkeys("keys", "--from", ja, "--func", "kcse", "--phase", "opt", "--all")
    names = re.findall(r"^\s+\d+ \w+\s+\d+\s+(\$o\d+)\b.*\(optimiser", out, re.M)
    check("symkeys keys --phase opt: optimiser temporaries listed by their `$o<n>` names", len(names) >= 3, True)
    hs = re.findall(r"^\s+(\d+) hash\s+\d+\s+(\$o\d+)\b", out, re.M)
    out2 = symkeys("whatif", "--from", ja, "--func", "kcse", "add:hash@%s:2" % (hs[0][1] if hs else "?"), "--show", r"\$o")
    m = re.search(r"^\s+%s\s+(\d+) ->\s+(\d+)" % re.escape(hs[0][1]) if hs else "^$x", out2, re.M)
    check("symkeys whatif add:hash@$o..: two more value-numbering temps move that one by 2",
          (int(m.group(2)) - int(m.group(1))) if m else None, 2)
    out = symkeys("levers", "--from", ja, "--func", "kcse", "--var", names[0] if names else "?", "--above", "0")
    check("symkeys levers --var on an optimiser temporary", "want key > 0" in out, True)
    out = symkeys("levers", "--from", ja, "--func", "kcall3", "--site", "*:_b>_c", "--site", "*:_a>_c", "--top", "3")
    check("symkeys levers --site: edit sets giving two wanted orders of the n-ary add at once",
          bool(re.search(r"^2/2 sites\s+0 other flips", out, re.M)), True)
    if BUILD.startswith("13.10"):
        # what a global costs in keys on 13.10 (M&B measurements): extra glob / frame allocations against g0
        out = symkeys("keys", os.path.join(HERE, "tests", "data", "symkeys_glob.cpp"), "--brief", *FLAGS)
        cnt = {}
        for m in re.finditer(r"^\?(g\w+)@\S*\s+.*\[(.*)\]\s*$", out, re.M):
            d = dict((k, int(v)) for k, v in re.findall(r"(\w+) (\d+)", m.group(2)))
            cnt[m.group(1)] = (d.get("glob", 0), d.get("frame", 0))
        b = cnt.get("g0", (0, 0))
        got = {k: (v[0] - b[0], v[1] - b[1]) for k, v in cnt.items() if k != "g0"}
        check("symkeys 13.10: global keys (glob, frame) per construct, against a function without globals",
              [got.get(k) for k in ("gbase", "gref", "gfield0", "gfield4", "gfield44", "gcall", "garr", "garrc", "gvoid", "gint")],
              [(1, 1), (1, 1), (2, 0), (3, 0), (3, 0), (2, 0), (2, 0), (3, 0), (1, 0), (1, 0)])


def fpcolor_tests():
    """13.10: FPColor (x87 values outside Color's class loop), docs/c2/port-1310.md section 12."""
    out = tool("regs", FP, "--func", "_h", "--all-classes", "--why", "*", "--", "/arch:SSE2")
    sums = re.findall(r"^  why \S+ \(pop \d+\): prio (-?\d+) = (-?\d+)(.*)$", out, re.M)
    check("FPColor: --why terms of every x87 row of h add up to its prio",
          (len(sums), all(a == b and not c for a, b, c in sums)), (5, True))
    check("FPColor: float uses cost 0, defs 2, loads -1 (h: x, y, z worthless; u, v kept at 2)",
          re.findall(r"^\s+\d+\s+(\w)\s+\w+\s+(-?\d+)\s+-?\d+\s+\d+\s+\d+\s+(x87 stack|spill \(worthless\))", out, re.M),
          [("x", "-1", "spill (worthless)"), ("y", "-1", "spill (worthless)"), ("z", "-1", "spill (worthless)"),
           ("u", "2", "x87 stack"), ("v", "2", "x87 stack")])
    fd = tempfile.mkdtemp()
    snaps = {}
    for f in ("tf_blk", "tf_xblk"):
        snaps[f] = os.path.join(fd, f + ".json")
        out = tool("regs", TIE, "--func", f, "--all-classes", "--why", "c", "--export", snaps[f])
        if f == "tf_blk":
            check("FPColor: tf_blk's c is not colourable because b ends below it (the reason is traced)",
                  "not colourable: coloured web b ends at src +2 while c is on the x87 stack" in out, True)
    r = subprocess.run([rasim, "check", snaps["tf_blk"]], capture_output=True, text=True)
    m = re.search(r"x87 \(FPColor.*: (\d+) costing passes, (\d+) webs; terms (\d+) \((\d+) mismatches.*priorities (\d+) "
                  r"of (\d+), pops (\d+) of (\d+) in model order, worthless drops (\d+) of (\d+)", r.stdout)
    check("rasim check: FPColor terms, priorities, pops and drops of tf_blk (2 passes, a split)",
          m.groups() if m else None, ("2", "5", "11", "0", "5", "5", "3", "3", "2", "2"))
    r = subprocess.run([rasim, "need", snaps["tf_xblk"], "c>a"], capture_output=True, text=True)
    check("rasim need 'c>a' on x87 webs: the tie on prio 5 is lost on defOrd; single edits close it",
          (bool(re.search(r"^c \(5, def 8, web \d+\) before a \(5, def 11, web \d+\): slack -1  \(VIOLATED\)", r.stdout, re.M)),
           "ref:c:2:1" in r.stdout), (True, True))


if BUILD.startswith("13.10"):
    fpcolor_tests()

if c2build.table() is None or "symkeys" in (c2build.get("hooks") or {}):
    symkeys_tests()
    symkeys_more_tests()
else:
    print("  symkeys: no symkeys hooks in this build table (not ported): skipped")


# realize (docs/c2/realize.md, tools/realize.py), on every c2 build: what a source change does to the allocation's
# inputs, measured from two traces (the catalogue's probes), and one abstract edit turned into a source rewrite
def realize_tests():
    RZ = os.path.join(HERE, "tests", "data", "realize.cpp")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "pairs", RZ] + FLAGS,
                       capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-4000:], r.stderr[-2000:])
    rows, ctx = {}, None
    for ln in r.stdout.splitlines():
        m = re.match(r"## (\w+):", ln)
        if m:
            ctx = m.group(1)
            continue
        m = re.match(r"  (\w+)\s+code (\d+) \(bag (\d+), regs (\d+), ([+-]\d+) instr\)\s+(.*)$", ln)
        if m and ctx:
            rows[(ctx, m.group(1))] = (int(m.group(2)), int(m.group(3)), int(m.group(5)), m.group(6).split())
    def has(key, *edits):
        row = rows.get(key)
        return row is not None and all(e in row[3] for e in edits)
    check("realize: a copy before the next modification is +2 on n and one more candidate in ITS block, same code",
          (has(("cpl", "c2"), "ref:n:1:+2", "P:1:+1", "copy:k:n"), has(("cpl", "c3"), "ref:n:3:+2", "P:3:+1"),
           rows.get(("cpl", "c2"), (9,))[0], rows.get(("cpl", "c3"), (9,))[0]), (True, True, 0, 0))
    check("realize: the same through an inline helper that walks its by-value parameter",
          (has(("wk", "helper"), "ref:p:1:+2", "P:1:+1"), rows.get(("wk", "helper"), (9,))[0]), (True, 0))
    check("realize: a read at the head of both arms counts in the arms (merged after Color: same instructions)",
          (has(("hdup", "dup"), "drop:p:1", "ref:p:2:+2", "ref:p:3:+2"), rows.get(("hdup", "dup"), (9, 9))[1]), (True, 0))
    check("realize: a narrow -1 through the other signedness is one constant candidate fewer, same code",
          (has(("imm", "resp"), "imm-:imm=-1:1", "P:1:-1"), rows.get(("imm", "resp"), (9,))[0]), (True, 0))
    check("realize: dead uses leave nothing at Color; a store is ref:p:1:+2 and one instruction",
          (rows.get(("tch", "void"), (9, 9, 9, ["x"]))[3], has(("tch", "store"), "ref:p:1:+2"),
           rows.get(("tch", "store"), (0, 0, 0))[2]), (["(no", "allocation-input", "change)"], True, 1))
    check("realize: a callee body that only reads keeps the address-taken local one web (extend:x:1)",
          has(("ext", "body"), "extend:x:1"), True)
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "edit", RZ, "--func", "cpl__base",
                        "ref:n:3", "--keep", os.path.join(tempfile.mkdtemp(), "rz")] + FLAGS,
                       capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    check("realize edit ref:n:3: block 3 mapped to its source line, the copy rewrite realises it with code 0",
          (bool(re.search(r"^## ref:n:3\s+\(block 3 = source lines \+6,", r.stdout, re.M)),
           bool(re.search(r"^\s+(?:exact|yes)\s+copy\s+code 0 bag 0 regs 0\s+int rz_c = n; at line \+6", r.stdout, re.M))),
          (True, True))


realize_tests()



def blocknum_tests():
    """regs: block numbers are per Spillcosts pass; --why marks the pass-1 number, --blocks --pass N maps back."""
    SN = os.path.join(HERE, "tests", "data", "splitnum.c")
    out = tool("regs", SN, "--func", "_f4", "--why", "x")
    m = re.search(r"\(block numbers of Spillcosts pass (\d+).*\n(?:.*\n)*?\s+block\s+(\d+) .*\[p1 block (\d+)\]", out)
    if not m:
        check("regs --why: a pass-2 web's block carries its pass-1 number", None, "found")
        return
    p, b, b1 = m.groups()
    o1 = tool("regs", SN, "--func", "_f4", "--blocks", "%s" % b, "--pass", p)
    o2 = tool("regs", SN, "--func", "_f4", "--blocks", "%s" % b1)
    check("regs --blocks --pass: a --why block number maps to the same BB as its pass-1 number",
          (bool(re.search(r"^  block %s of pass %s .*\(BB (\w+); pass 1: block %s\)" % (b, p, b1), o1, re.M)),
           bool(re.search(r"^  block %s .*pass %s: block %s\b" % (b1, p, b), o2, re.M))), (True, True))


def relocmask_tests():
    """The checker trap (tracer.md): an original without .reloc, ours an .obj with relocations, a switch whose jump
    table entries are relocated in ours. Unit test, no compile."""
    import struct
    sys.path.insert(0, os.path.join(HERE, "tools"))
    from image import Image
    try:
        import chk                                   # the cl.exe decompilation's checker: not in the c2scope release
    except ImportError:
        print("  relocmask: skipped (tools/chk.py is not in this checkout)")
        return
    import ltcgstub, types
    BASE, VA = 0x400000, 0x401000

    def build(addr):
        d = lambda a: struct.pack("<I", addr(a))
        c = bytearray(b"\x83\xf8\x02\x77\x00\xff\x24\x85") + d(0)
        rel, cases = [8], []
        for k in range(3):                                   # case k: mov ecx, offset g_k; jmp join
            cases.append(len(c)); c += b"\xb9" + d(0x405000 + 4 * k); rel.append(len(c) - 4); c += b"\xe9\0\0\0\0"
        join = len(c); c += b"\xc3"
        for k in range(3):
            o = cases[k] + 5; c[o + 1:o + 5] = struct.pack("<i", join - (o + 5))
        c[4] = join - 5
        while (VA + len(c)) % 4:
            c += b"\xcc"
        c[8:12] = d(VA + len(c))
        for k in range(3):
            c += d(VA + cases[k]); rel.append(len(c) - 4)
        c += b"\xff\xff\x00\x01"                           # a byte index table after it
        return bytes(c), {r + j for r in rel for j in range(4)}
    theirs, _ = build(lambda a: a)
    mine, rmask = build(lambda a: 0)
    img = Image.__new__(Image)
    img.base, img.size, img.relocs = BASE, 0x10000, set()
    img.data = bytes(VA - BASE) + theirs + bytes(0x10000 - (VA - BASE) - len(theirs))
    before = chk.align(theirs, img.reloc_mask(VA, len(theirs)), mine, rmask)[0]
    after = chk.align(theirs, img.paired_mask(VA, len(theirs), mine, rmask), mine, rmask)[0]
    check("checker trap: jump table masked on our side only differs; paired_mask fixes it", (before > 0, after), (True, 0))
    img.relocs = {VA + 9}                                   # a real .reloc table: the plain mask, nothing guessed
    check("checker trap: with a .reloc table paired_mask is reloc_mask",
          img.paired_mask(VA, len(theirs), mine, rmask) == img.reloc_mask(VA, len(theirs)), True)
    ex = types.SimpleNamespace(read=img.read, in_image=img.in_image)
    _, m = ltcgstub.masked(types.SimpleNamespace(exe=ex), VA, len(theirs))
    tab = len(theirs) - 16
    check("ltcgstub cmp: jump-table entries (image addresses) are masked", all(tab + k in m for k in range(12)), True)


blocknum_tests()
relocmask_tests()


def fold_tests():
    """A reload folds into the named variable only when its stretch holds one reference (regalloc.md 3.6.10)."""
    src = os.path.join(HERE, "tests", "data", "fold.cpp")
    rasim_ = os.path.join(HERE, "tools", "rasim", "target", "release", "rasim")
    d = tempfile.mkdtemp()
    got = {}
    for fn_ in ("fa", "fb"):
        sn = os.path.join(d, "fold_%s.json" % fn_)
        out = tool("regs", src, "--func", "?%s@@" % fn_, "--export", sn, *FLAGS)
        got[fn_] = (globregs(out, fn_), subprocess.run([rasim_, "fold", sn, "t"], capture_output=True, text=True).stdout)
    fa, fb = got["fa"][1], got["fb"][1]
    check("fold.cpp: fa has the dword's reload web, fb does not (2 and 1 globregs)", (got["fa"][0], got["fb"][0]), (2, 1))
    check("rasim fold: fa's reload has 2 references in its stretch, one edit folds it",
          ("stretch: 2 references" in fa, "not folded: one reference too many" in fa), (True, True))
    check("rasim fold: fa's exact tie goes to the reload (defOrd one above the copy), not a fold lever",
          bool(re.search(r"tie: local5 and t both prio \d+; local5 pops first \(defOrd (\d+) vs \d+: the reload is", fa)), True)
    check("rasim fold: fb's copy reads memory (no reload web)", "folded, t is loaded from memory" in fb, True)


fold_tests()


def merges_tests():
    """Tail merging compares call targets by symbol and registers by number (il_fg.md 7.5); call-target names in the
    plain IL dump (13.10, 15.00: `set fename`); c2trace options anywhere before `--`, relative --keep (tracer.md)."""
    src = os.path.join(HERE, "tests", "data", "tailmerge.c")
    got = {}
    for fn_ in ("c1", "c2", "c3", "r1", "r2"):
        out = tool("merges", src, "--func", fn_, "--", "/O2")
        m = re.search(r"^tail \S+: (\d+) pair\(s\)(?:, (\d+) merged)?", out, re.M)
        got[fn_] = (int(m.group(2) or 0) if m else None, out)
    check("merges: twins f1/f2 stop the walk at the call (call target _f2 / _f1)",
          "stop: call target _f2 / _f1" in got["c2"][1], True)
    check("merges: a forwarder to f1 merges like one callee (c3 = c1)", got["c3"][0], got["c1"][0])
    check("merges: one scratch pick per arm: every pair stops at a register, none merged",
          (got["r1"][0], len(re.findall(r"stop: register", got["r1"][1].split("-- iteration 2")[0])) >= 3), (0, True))
    check("merges: three picks per arm: the same lea register, both pairs merged", got["r2"][0], 2)
    out = tool("il", os.path.join(HERE, "tests", "data", "callnames.cpp"), "--func", "call_names", "--at",
               "@Encode@4", "--grep", "call",
               "--", "/TP", "/O2")
    names = [n for n in ("?g1@@YAXH@Z", "?m@A@@QAEXH@Z", "?g2@@YAHHH@Z", "?s@A@@SAHH@Z", "?loc@@YAHH@Z") if n in out]
    check("il: every call target printed by its own name", len(names), 5)
    d = tempfile.mkdtemp()
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2trace.py"), src, "--keep", "kk", "--max", "3",
                        "-H", "@Color@4", "--", "/O2"],
                       capture_output=True, text=True, cwd=d)
    check("c2trace: --keep/--max after the source, relative --keep from the caller's cwd",
          (r.returncode, os.path.exists(os.path.join(d, "kk", "c2trace.log"))), (0, True))


merges_tests()


def slots_tests():
    """Stack-slot packing (docs/c2/slots.md): the replay model reproduces every traced decision and offset; creation
    order below 128 bytes; the sort and the param-home undo above; what-if; the 13.10 /GS param-home rule; the
    original's slot map against our own listing."""
    src = os.path.join(HERE, "tests", "data", "slots.c")
    d = tempfile.mkdtemp()
    out = tool("slots", src, "--func", "_", "--all", "--quiet", "--keep", d, "--export", os.path.join(d, "s.json"), "--", "/GS")
    rows = dict(re.findall(r"^# (_\w+): .*; model: (\S+)", out, re.M))
    check("slots: the model reproduces all 6 functions of slots.c exactly", (len(rows), set(rows.values())), (6, {"exact"}))
    snap = lambda f: json.load(open(os.path.join(d, "s.%s.json" % f)))
    offs = lambda s_: {(s_["elems"][str(i)]["name"] or "") : o for c in s_["obs"]["cols"] for i, o in c["members"]}
    sm = offs(snap("small"))
    check("slots: T <= 128 keeps creation order (a, x, b, c at -16 .. -4)",
          (sm.get("_a"), sm.get("_x"), sm.get("_b"), sm.get("_c")), (-16, -12, -8, -4))
    bg = snap("big")
    check("slots: T > 128 sorts by key (char, int, double, struct) and drops the param home (undo)",
          ([sorted(i for i, _ in c["members"]) for c in sorted(bg["obs"]["cols"], key=lambda c: c["c"])], bg["obs"]["undo"]),
          ([[0], [1], [3], [4]], True))
    sh = snap("share")
    check("slots: char, short and two ints of disjoint blocks share one slot (growth by 2x)", len(sh["obs"]["cols"]), 1)
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2slots.py"), "what", os.path.join(d, "s.share.json"),
                        "conf+:_a,_b"], capture_output=True, text=True, cwd=HERE).stdout
    m = re.search(r"1 colours -> (\d+); (\d+) elements moved", r)
    check("slots what-if: a conflict between a and b: b opens a 2nd slot at the top, c, s, a move down (3 moved)",
          m.groups() if m else None, ("2", "3"))
    gh = offs(snap("gshome"))
    want = 8 if BUILD.startswith("13.10") else -24
    check("slots /GS: x in p's home on 13.10, a new slot on 14.00/15.00 (x at %d)" % want, gh.get("_x"), want)
    # the original's slot map, with our own listing as the original: every element agrees
    import c2slots, tsweep
    asm = os.path.join(d, "slots.asm")
    syms = c2slots.listing_slots(asm, "_big")
    lst = tsweep.listing_offsets(asm, "_big")
    fake = os.path.join(d, "big_orig.s")
    def num(m):
        v = syms[m.group(1)]["off"] + (int(m.group(3) or 0))
        return "[%s %s 0x%x]" % (m.group(2), "+" if v >= 0 else "-", abs(v))
    with open(fake, "w") as f:
        for o in sorted(lst):
            f.write("%08x %s\n" % (0x401000 + o, re.sub(r"([_$A-Za-z0-9?@]+)\[(esp|ebp)([+-]\d+)?\]", num, lst[o])))
    out = tool("slots", src, "--func", "_big", "--orig", fake, "--", "/GS")
    m = re.search(r"^# (\d+) of (\d+) elements seen in both are at the original's offset", out, re.M)
    check("slots --orig: our own listing as the original, every element at its offset",
          (m.group(1) == m.group(2), int(m.group(2)) >= 4) if m else None, (True, True))


slots_tests()

print("%d of %d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
