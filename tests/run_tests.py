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

# spill / defer / split decisions predicted (regalloc.md 3.6, tracer.md "rasim check --predict"); cl 14.00 only:
# another build's snapshot has no decision events
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
          ("34/34", "34/34", "24/24", "10/10", "4/4", "4/4", "8/8", "45/45"))
    m = re.search(r"state: (\d+) places", r)
    check("rasim check --predict: the model's records never differ from c2's", m.group(1) if m else None, "0")

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

print("%d of %d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
