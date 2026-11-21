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
# need/want's fast path (differences from the unedited run, Model::run_diff; single source edits from Uni::quick;
# tracer.md "Speed of `need reg:`") prints exactly what the reference path prints (RASIM_REF=1: a full state copy and
# run per candidate). RASIM_QCHECK=1 also checks every quick edit against the overlay path (it aborts on a difference).
same = []
for args in (["need", snap, "reg:k=esi"], ["need", snap, "reg:k=esi", "reg:h=edi", "--depth", "3"],
             ["want", snap, wantf], ["need", snap, "reg:k=esi", "pref:h:edi"]):
    a = subprocess.run([rasim] + args, capture_output=True, text=True)
    b = subprocess.run([rasim] + args, capture_output=True, text=True, env=dict(os.environ, RASIM_REF="1"))
    c = subprocess.run([rasim] + args, capture_output=True, text=True, env=dict(os.environ, RASIM_QCHECK="1"))
    same.append((a.stdout == b.stdout == c.stdout, a.returncode == b.returncode == c.returncode == 0, len(a.stdout) > 100))
check("rasim need/want: the fast path's output is the reference path's (4 searches on eA, quick edits self-checked)",
      same, [(True, True, True)] * 4)

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
    # split pieces modelled from the round's IL (regalloc.md 3.6.11, tracer.md "rasim pieces"): f4's two rounds
    ps = os.path.join(os.path.dirname(snap), "pieces_f4.json")
    out = tool("regs", SN, "--func", "_f4", "--pieces", "--export", ps)
    r = subprocess.run([rasim, "pieces", ps], capture_output=True, text=True).stdout
    m = re.search(r"piece sequence (\S+); blocks walked (\S+); per-block terms (\S+); .*pieces exact (\S+)", r)
    check("rasim pieces (f4): records, blocks, terms of every piece, from the round's IL",
          m.groups() if m else None, ("2/2", "2/2", "8/9", "8/9") if BUILD.startswith("13.10") else ("2/2", "2/2", "9/9", "9/9"))
    r = subprocess.run([rasim, "pieces", ps, "--chain"], capture_output=True, text=True).stdout
    m2 = re.search(r"piece sequence (\S+); .*pieces exact (\S+)", r)
    r = subprocess.run([rasim, "check", ps, "--predict", "--pieces"], capture_output=True, text=True).stdout
    m3 = re.search(r"pieces: (\d+) of the (\d+) records", r)
    check("rasim pieces --chain and check --predict --pieces (f4): the same from the first round, 8 of 8 records",
          (m2.groups() if m2 else None, m3.groups() if m3 else None),
          (("2/2", "8/9" if BUILD.startswith("13.10") else "9/9"), ("8", "8")))

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
got = {}


def orig_from_listing(asm, func, path):
    """Our own code as the "original": capstone of the listing's code bytes in orig.s's 'VA mnemonic operands' form
    (what an image's disassembly gives; the listing's own text has MASM's operand order)."""
    import tsweep
    lst = tsweep.listing_instrs(asm, func)
    open(path, "w").write("".join("%08x %s %s\n" % (o, d[0], d[1]) for o, d in zip(lst.addrs, lst.decoded)))
    return path


for fn_ in ("_f", "_h"):
    orig = orig_from_listing(os.path.join(kd, "fpclasses.asm"), fn_, os.path.join(kd, fn_ + ".s"))   # every row agrees
    out = tool("regmap", FP, "--func", fn_, "--asm-vs", orig, "--asm-func", fn_, "--all", "--", "/arch:SSE2")
    m = re.search(r"^# by class: (.*)$", out, re.M)
    got[fn_] = dict((c, (int(a), int(b))) for c, a, b in re.findall(r"(\w+) (\d+) of (\d+)", m.group(1))) if m else {}
xm, st = got["_f"].get("xmm", (0, -1)), got["_h"].get("x87", (0, -1))
check("regmap: XMM rows (f) and x87 stack-position rows (h), all agreeing with the same code",
      (xm[0] == xm[1] and xm[1] >= 4, st[0] == st[1] and st[1] >= 4), (True, True))


def testorder_tests():
    """`test al, bl` in the listing is `test bl, al` in capstone (84 c3): regmap compares capstone of our bytes with
    orig.s, so our own code as the original gives no WANTS and no swap (it gave a false WANTS pair and a swap)."""
    import tsweep
    src, d = os.path.join(HERE, "tests", "data", "testorder.c"), tempfile.mkdtemp()
    tool("scratch", src, "--func", "both", "--keep", d)
    asm = os.path.join(d, "testorder.asm")
    orig = orig_from_listing(asm, "_both", os.path.join(d, "both.s"))
    txt = list(tsweep.listing_instrs(asm, "_both"))
    out = tool("regmap", src, "--func", "both", "--asm-vs", orig, "--asm-func", "_both")
    check("regmap: listing `test r8, r8` vs capstone order: no false WANTS / swap (testorder.c)",
          (("test", "al, bl") in txt, "WANTS" in out, "swap:" in out,
           bool(re.search(r"^# (\d+) variables checked, \1 agree", out, re.M))),
          (True, False, False, True))
    check("tsweep regdiff / canon_listing: the listing's test and xchg operand order is capstone's",
          (tsweep.regdiff(tsweep.listing_instrs(asm, "_both"), tsweep.orig_instrs(orig)),
           tsweep.canon_listing("test", "al, bl"), tsweep.canon_listing("xchg", "ecx, DWORD PTR _g"),
           tsweep.canon_listing("test", "eax, 5"), tsweep.canon_listing("fcomip", "ST(1)")),
          (0, ("test", "bl, al"), ("xchg", "DWORD PTR _g, ecx"), ("test", "eax, 5"), ("fcompi", "ST(1)")))


testorder_tests()

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


def symkeys_hash_tests():
    """The optimiser's value-numbering temps (hash class, symkeys.md section 8): numbered in HshBuildTable's walk
    (one per distinct value), then the assignment tail, then later passes; keys come in blocks of 32 of their own
    class (key mod 32 = rank), freed keys go to the temp list; parentheses and casts as code-neutral levers."""
    HS = os.path.join(HERE, "tests", "data", "symkeys_hash.cpp")
    out = symkeys("keys", HS, "--phase", "opt", "--all", *FLAGS)
    cnt, hs, tm, cur = {}, {}, {}, None
    for ln in out.splitlines():
        m = re.match(r"^#### \?(\w+)@@", ln)
        if m:
            cur = m.group(1)
            cnt[cur], hs[cur], tm[cur] = {}, [], set()
            continue
        m = re.match(r"^\s*(\d+) (hash|tmp)\s+\d+\s+\$o(\d+)\s", ln)
        if m and cur:
            if m.group(2) == "hash":
                hs[cur].append((int(m.group(3)), int(m.group(1))))
                ph = re.search(r"/(walk|assign|later)\b", ln)
                ph = ph.group(1) if ph else None
                cnt[cur][ph] = cnt[cur].get(ph, 0) + 1
            else:
                tm[cur].add(int(m.group(1)))
    b = cnt.get("hb", {})
    d = {k: tuple(c.get(x, 0) - b.get(x, 0) for x in ("walk", "assign", "later")) for k, c in cnt.items()}
    check("symkeys hash: per construct (walk, assignment tail, later) against hb",
          [d.get(k) for k in ("hparen1", "hparen3", "hnoparen", "hleaf", "hcast", "hflag", "hdbl")],
          [(1, 0, 0), (3, 0, 0), (-2, 0, 0), (0, 0, 0), (1, 0, 0), (1, 2, 1),
           (3, 0, 0) if BUILD.startswith("13.10") else (1, 0, 0)])
    check("symkeys hash: walk / assign / later counts of hb",
          tuple(b.get(x, 0) for x in ("walk", "assign", "later")), (51, 4, 6) if BUILD.startswith("13.10") else (65, 4, 6))
    ranks = [sorted(v) for v in hs.values() if v]
    check("symkeys hash: key mod 32 = rank among the function's hash temps (own blocks of 32, no reuse in class)",
          (len(ranks) >= 8, all(key % 32 == i % 32 for r in ranks for i, (_, key) in enumerate(r))), (True, True))
    check("symkeys hash: freed hash keys are handed out again as temporaries (temp free list)",
          all(len(tm[k] & {key for _, key in hs[k]}) > 0 for k in hs if hs[k]), True)


def fpcheck_tests():
    """13.10: FPCheckColorability, FPInitialRegPressure and FPUpdateRegisterPressure predicted from the x87 view
    (c2trace.dll's '&' dump), docs/c2/x87.md section 11.1; tests/data/fpcheck.c has one function per rule."""
    FC = os.path.join(HERE, "tests", "data", "fpcheck.c")
    fd = tempfile.mkdtemp()
    tool("regs", FC, "--all-classes", "--export", os.path.join(fd, "%s.json"))
    got, fired = {}, {}
    for f in ("k_call", "k_end", "k_ext", "k_join", "k_depth"):
        o = subprocess.run([rasim, "check", os.path.join(fd, "_%s.json" % f), "--predict"], capture_output=True,
                           text=True).stdout
        m = re.search(r"predicted from the x87 view: (\d+) of (\d+) answers, sub-test calls (\d+) of (\d+), split points "
                      r"(\d+) of (\d+)", o)
        n = re.search(r"FPInitialRegPressure (\d+) of (\d+) \((\d+) non-empty\), FPUpdateRegisterPressure \(after a "
                      r"colouring\) (\d+) of (\d+) \((\d+) non-empty\)", o)
        got[f] = (m.groups() if m else None, n.groups() if n else None)
        w = re.search(r"sub-tests that returned 1 \(trace\): (.*)", o)
        fired[f] = w.group(1) if w else ""
    check("FPCheckColorability predicted from the view: every answer, sub-test call and split point (fpcheck.c)",
          [(f, a[0] == a[1] and a[2] == a[3] and a[4] == a[5] and a[1] != "0") if a else (f, None)
           for f, (a, _) in got.items() if f != "k_call"], [(f, True) for f in got if f != "k_call"])
    check("the split sets: FPInitialRegPressure (k_call: x at g(), its pieces never popped) and "
          "FPUpdateRegisterPressure (k_depth) predicted",
          [(got[f][1][0] == got[f][1][1], got[f][1][2], got[f][1][3] == got[f][1][4], got[f][1][5]) if got[f][1] else None
           for f in ("k_call", "k_depth")], [(True, "1", True, "0"), (True, "0", True, "1")])
    check("the rule each probe makes fire (traced): end, ext (CanExtendLifetime), join (SplitAtMismatchedFlowJoinPoints)",
          [("end" in fired["k_end"]), ("ext" in fired["k_ext"]), ("join" in fired["k_join"])], [True, True, True])
    r = subprocess.run([rasim, "need", os.path.join(fd, "_k_end.json"), "stack:c"], capture_output=True, text=True)
    check("rasim need stack:c (k_end): b ends below c; popped before b, c would stay on the stack (+2 priority)",
          (bool(re.search(r"blocker: b \(web \d+\) ends at src \+2 while c is on the stack above it", r.stdout)),
           "c would be colourable; it needs +2 priority" in r.stdout), (True, True))


def x87stack_tests():
    """14.00 / 15.00: the x87 stack after Color, docs/c2/x87.md section 12: which calls keep x87 values
    (LoAddCallScratchRegs + MscRecordInterProcRegInfo), the GFx -> ST(i) mapper (X87MapGFxToSTx), and `need stack:V`,
    predicted by rasim x87stack.rs from the snapshot key x87 (tools/c2x87.py) and checked against the trace."""
    XS = os.path.join(HERE, "tests", "data", "x87stack.cpp")
    fd = tempfile.mkdtemp()
    tool("regs", XS, "--export", os.path.join(fd, "%s.json"), *FLAGS)
    tot, keep = [0] * 8, {}
    for fn_ in sorted(os.listdir(fd)):
        o = subprocess.run([rasim, "check", os.path.join(fd, fn_), "--predict", "-v"], capture_output=True, text=True).stdout
        m = re.search(r"x87 stack .*?calls (\d+) of (\d+).*?blocks (\d+) of (\d+).*?\((\d+) of (\d+) x87.*?airlocks (\d+) of (\d+)", o)
        if m:
            tot = [a + int(b) for a, b in zip(tot, m.groups())]
        for c in re.findall(r"^  call \?(\w+)@\S*: (x87 values stay|kills GF0..GF7)", o, re.M):
            keep[c[0]] = c[1] == "x87 values stay"
    check("x87 stack: every call's kill set, block and airlock of x87stack.cpp predicted (model = trace)",
          (tot[0] == tot[1] > 0, tot[2] == tot[3] > 0, tot[4] == tot[5] > 100, tot[6] == tot[7] > 0), (True,) * 4)
    check("x87 kept across a call: x87-free same-unit bodies (no push; one push returning float) only",
          [keep.get(k) for k in ("ext", "geti", "getf", "getf2", "voidfp", "gin")], [False, True, True, False, False, False])
    o = subprocess.run([rasim, "check", os.path.join(fd, "_f_join@@YAMPBMHH@Z.json"), "--predict", "-v"],
                       capture_output=True, text=True).stdout
    check("x87 stack: f_join's uninitialised-on-one-path s gets fldz on that edge (airlock), predicted",
          "fldz" in o and "airlocks 2 of 2" in o, True)
    js = json.load(open(os.path.join(fd, "_r_float@@YAMMMM@Z.json")))["x87"]["gf"]
    jd = json.load(open(os.path.join(fd, "_r_double@@YAMMMM@Z.json")))["x87"]["gf"]
    check("rounding: a float variable is written to its home and reloaded (_s <- GF; GF <- _s), a double is not",
          (any(re.search(r"_fmov \d+ \d+ \| _s:5004 <- GF", x) for x in js), any(re.search(r"<- _s:5004", x) for x in js),
           any(" _s:" in x for x in jd)), (True, True, False))
    r = subprocess.run([rasim, "need", os.path.join(fd, "_z_tie@@YAXPAUV@@PBU1@ABUT@@H@Z.json"), "stack:__real@00000000"],
                       capture_output=True, text=True)
    check("rasim need stack:0.0 (z_tie): split when local19 took the last slot; one more reference closes the tie",
          (bool(re.search(r"pass 1: split before its pop", r.stdout)), "blocker: local19" in r.stdout,
           "ref:_real@00000000" in r.stdout), (True, True, True))


if BUILD.startswith("13.10"):
    fpcolor_tests()
    fpcheck_tests()
else:
    x87stack_tests()

if c2build.table() is None or "symkeys" in (c2build.get("hooks") or {}):
    symkeys_tests()
    symkeys_more_tests()
    symkeys_hash_tests()
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
    check("realize: equal arms on (int)this & 1 split the block at Color (this referenced in the test and the join) "
          "and are merged away after it: no code",
          (has(("eqt", "eq"), "extend:this:n5", "extend:this:n8", "blocks:-1+4"), rows.get(("eqt", "eq"), (9,))[0]),
          (True, 0))
    check("realize: a statement after the if at the end of both arms counts in the arms (tail merge after Color)",
          (has(("tdup", "dup"), "extend:p:2", "extend:p:3", "ref:p:4:-2"), rows.get(("tdup", "dup"), (9,))[0]),
          (True, 0))
    check("realize: two independent statements exchanged: a defOrd tie flips, same instructions",
          (has(("swp", "swap"), "tie:q>p", "nointerf:n:p"), rows.get(("swp", "swap"), (9, 9))[1]), (True, 0))
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "edit", RZ, "--func", "cpl__base",
                        "ref:n:3", "--keep", os.path.join(tempfile.mkdtemp(), "rz")] + FLAGS,
                       capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    check("realize edit ref:n:3: block 3 mapped to its source line, the copy rewrite realises it with code 0",
          (bool(re.search(r"^## ref:n:3\s+\(block 3 = source lines \+6,", r.stdout, re.M)),
           bool(re.search(r"^\s+(?:exact|yes)\s+copy\s+code 0 bag 0 regs 0\s+int rz_c = n; at line \+6", r.stdout, re.M))),
          (True, True))
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "edit", RZ, "--func", "RzL::eqt__base",
                        "ref:this:5", "--constructs", "eqif", "--keep", os.path.join(tempfile.mkdtemp(), "rz")] + FLAGS,
                       capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    check("realize edit ref:this:5 (this is never modified): the equal-arms construct realises it with code 0",
          bool(re.search(r"^\s+yes\s+eqif\s+code 0 bag 0 regs \d+\s+`if \(\(int\)this & 1\) s \^= k;", r.stdout, re.M)), True)


realize_tests()


def realize2_tests():
    """The slot, scratch-pick and key constructs of realize.py (docs/c2/realize.md 2.5, 2.6, 2.8), with the build
    differences recorded there: equal arms count twice for the packer and are merged after it; a statement at the
    end of both arms hides one scratch pick; an earlier initialised declaration moves a frame key."""
    RZ = os.path.join(HERE, "tests", "data", "realize2.cpp")
    fl = ["--", "/TP", "/MT", "/EHsc", "/Gy", "/O2"]
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "pairs", RZ, "--slots", "--scratch",
                        "--keys"] + fl, capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-4000:], r.stderr[-2000:])
    rows, ctx, cur = {}, None, None
    for ln in r.stdout.splitlines():
        m = re.match(r"## (\w+):", ln)
        if m:
            ctx = m.group(1)
            continue
        m = re.match(r"  (\w+)\s+code (\d+) \(bag (\d+)", ln)
        if m and ctx:
            cur = rows[(ctx, m.group(1))] = dict(code=int(m.group(2)), more="")
            continue
        if cur is not None:
            cur["more"] += ln.strip() + "\n"
    row = lambda k: rows.get(k, dict(code=-1, more=""))
    v13 = BUILD.startswith("13.10")
    check("realize2: a test of the stack local a in equal arms is uses:_a 2>3 for the packer (code 0 on 14.00/15.00, "
          "the memory test stays on 13.10)",
          ("slots: uses:_a:2>3" in row(("sc", "eqa"))["more"], row(("sc", "eqa"))["code"] == 0), (True, not v13))
    check("realize2: a statement reading a in equal arms on a register value: uses:_a 2>3 (code 0 on 13.10)",
          ("slots: uses:_a:2>3" in row(("sp", "eqb"))["more"], row(("sp", "eqb"))["code"] == 0), (True, v13))
    check("realize2: the statement after an if at the end of both arms: one hidden pick (ins:3), same instructions",
          ("ins:3(gone)" in row(("pt", "dup"))["more"], row(("pt", "dup"))["code"]), (True, 0))
    check("realize2: an initialised declaration two statements earlier moves 3 named keys, same code",
          ("3 of 12 named keys moved" in row(("kd", "early"))["more"], row(("kd", "early"))["code"]), (True, 0))
    check("realize2: an empty inline helper with 4 parameters is tmp+4 frame+4 keys, same code",
          ("keys: tmp+4 frame+4" in row(("kp", "h4"))["more"], row(("kp", "h4"))["code"]), (True, 0))
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "edit", RZ, "--func", "pt__base", "ins:3",
                        "--keep", os.path.join(tempfile.mkdtemp(), "rz")] + fl, capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    check("realize2 edit ins:3: taildup realises it, every pick as rasim scratch predicts",
          bool(re.search(r"^\s+exact\s+taildup\s+code 0 bag 0", r.stdout, re.M)) and "every pick as rasim predicts" in r.stdout, True)
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "edit", RZ, "--func", "sc__base", "uses:_a$=3",
                        "--keep", os.path.join(tempfile.mkdtemp(), "rz")] + fl, capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    check("realize2 edit uses:_a$=3: an equal-arms construct gives the packer's count%s" % (" with code 0" if not v13 else ""),
          bool(re.search(r"^\s+(exact|yes)\s+eqif\s+code 0 bag 0", r.stdout, re.M)) if not v13 else "uses:_a:2>3" in r.stdout, True)

    # the load-reuse levers: loads / stores / read-modify-writes of [reg+92] in the variant realize generates
    import tsweep
    def lcount(asm, func):
        l = s = w = 0
        for mn, o in tsweep.listing_instrs(asm, func):
            ins = "%s %s" % (mn, o)
            if "+92]" not in ins:
                continue
            if re.match(r"^(add|sub|and|or|xor|shl|shr|sar|inc|dec)\s+(?:DWORD|WORD|BYTE) PTR \[", ins):
                w += 1
            elif re.match(r"^mov (?:DWORD|WORD|BYTE) PTR \[", ins):
                s += 1
            else:
                l += 1
        return (l, s, w)
    got = []
    for fn, ed in (("la__base", "alias2:p->m"), ("lp__base", "ptrlocal:p->q"), ("lw__base", "subobj:w->e")):
        d = os.path.join(tempfile.mkdtemp(), "rz")
        subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "edit", RZ, "--func", fn, ed, "--keep", d] + fl,
                       capture_output=True, text=True, cwd=HERE)
        try:
            got.append((lcount(os.path.join(d, "base", "base.asm"), fn), lcount(os.path.join(d, "e0_000", "e0_000.asm"), fn)))
        except Exception as e:
            got.append(str(e)[:60])
    check("realize2 load levers: alias2 1 -> 3 loads, ptrlocal 2 -> 1, subobj read+store -> one RMW (13.10: 2 loads)",
          got, [((1, 0, 0), (3, 0, 0)), ((2, 1, 0), (1, 1, 0)), ((1, 1, 0), (2, 1, 0) if v13 else (1, 0, 1))])


realize2_tests()


def realize_ins_tests():
    """realize.py's ins family (docs/c2/realize.md 2.5.1): a statement before an if whose then-arm returns, written at
    the head of that arm and again after the if (headret), is head-merged after RegSimpleAssignBody on 14.00 / 15.00:
    one hidden pick, the same instructions; 13.10 keeps both copies."""
    RZ = os.path.join(HERE, "tests", "data", "realize_ins.cpp")
    fl = ["--", "/TP", "/MT", "/EHsc", "/Gy", "/O2"]
    v13 = BUILD.startswith("13.10")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "pairs", RZ, "--scratch"] + fl,
                       capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    m = re.search(r"^## hr:.*\n\s+dup\s+code (\d+) .*\n\s+picks: (.*)$", r.stdout, re.M)
    m3 = re.search(r"^## h3:.*\n\s+dup\s+code (\d+) ", r.stdout, re.M)
    check("realize_ins: a log call at the head of a returning arm and after the if: one hidden pick (ins:4), same "
          "instructions (13.10: not merged); with 3 picks in the arm the copies differ",
          (bool(m) and "ins:4(gone)" in m.group(2), bool(m) and m.group(1) == "0", bool(m3) and m3.group(1) != "0"),
          (not v13, not v13, True))
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "edit", RZ, "--func", "hr__base", "ins:4",
                        "--keep", os.path.join(tempfile.mkdtemp(), "rz")] + fl, capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    check("realize_ins edit ins:4: headret realises it exactly, every pick as rasim scratch predicts (13.10: no)",
          bool(re.search(r"^\s+exact\s+headret\s+code 0 bag 0", r.stdout, re.M)), not v13)


realize_ins_tests()


def regdiff_norm_tests():
    """tsweep.regdiff: printing differences between the listing and capstone are not differences."""
    import tsweep
    ours = [("push", "OFFSET FLAT:??_C@_01MIFGBAGJ@?$CL?$AA@"), ("imul", "esi, 44"), ("npad", "7"), ("cmp", "edi, 3"),
            ("npad", "2"), ("mov", "eax, esi")]
    path = os.path.join(tempfile.mkdtemp(), "o.s")
    open(path, "w").write("00000000 push 0x59aa9c\n00000005 imul esi, esi, 0x2c\n00000008 jmp 0xd\n0000000a lea ecx, [ecx]\n"
                          "0000000d cmp edi, 3\n00000010 mov edi, edi\n00000012 mov eax, esi\n")
    check("tsweep regdiff: symbol names, imul r, r, imm and alignment padding count as equal",
          (tsweep.regdiff(ours, tsweep.orig_instrs(path)), tsweep.regdiff(ours[:3] + [("cmp", "esi, 3")] + ours[4:], tsweep.orig_instrs(path))),
          (0, 2))
    # capstone names DF F0+i `fcompi` (the listing: fcomip); Spore 009d3410
    open(path, "w").write("00000000 fcompi st(1)\n00000002 fucompi st(2)\n")
    check("tsweep regdiff: capstone's fcompi / fucompi are the listing's fcomip / fucomip",
          tsweep.regdiff([("fcomip", "ST(1)"), ("fucomip", "ST(2)")], tsweep.orig_instrs(path)), 0)


regdiff_norm_tests()


def realize_member_type_tests():
    """realize._member_type: a member declared after a member with a trailing comment (Spore 009d3410)."""
    import types, realize, rwsweep
    c = types.SimpleNamespace(text="struct L {\n    int mCount;              // +0x0c\n    Limb* mLimbsBegin;  // +0x10\n"
                                   "    Limb* mLimbsEnd; /* +0x14 */\n    float f;\n};\n", rw=rwsweep)
    check("realize member type: found behind // and /* */ comments",
          (realize._member_type(c, "mLimbsBegin"), realize._member_type(c, "f")), ("Limb*", "float"))


realize_member_type_tests()


def operator_def_tests():
    """helpersweep.functions / body_of: operator functions (`??4Vec@@` is Vec::operator=; Spore 00c94c30)."""
    import helpersweep
    src = ("struct V { int a; V& operator=(const V& o); bool operator == (V b) { return a == b.a; } int operator()(int x)"
           " const { return x; } };\nV& V::operator=(const V& o) { a = o.a; return *this; }\n")
    fn = helpersweep.body_of(src, "??4V@@QAEAAU0@ABU0@@Z")
    check("helpersweep: operator definitions listed, ??4 found as Class::operator=",
          ([f[0] for f in helpersweep.functions(src)], src[fn[4]:fn[2]].strip()),
          (["operator==", "operator()", "operator="], "V::operator=(const V& o)"))


operator_def_tests()


def bdiff_tests():
    """tsweep.bdiff: displacements and immediates count, relocations / call targets / padding do not (synthetic
    listing); a compile's listing against the linked image of its own object scores 0 (every c2 build)."""
    import tsweep, c2ltcg
    d = tempfile.mkdtemp()
    lst = os.path.join(d, "f.asm")
    open(lst, "w").write(
        "_a$ = 8\n_f\tPROC NEAR\n"
        "  00000\t8b 44 24 04\t mov\t eax, DWORD PTR _a$[esp-4]\n"
        "  00004\t8b 0d 00 00 00\n\t00\t\t mov\t ecx, DWORD PTR _g\n"
        "  0000a\td9 40 08\t fld\t DWORD PTR [eax+8]\n"
        "  0000d\te8 00 00 00 00\t call\t _ext\n"
        "  00012\t68 00 00 00 00\t push\t OFFSET $SG1\n"
        "  00017\t8d 49 00\t npad\t 3\n"
        "  0001a\t68 00 00 00 00\t push\t __ehhandler$_f\n"
        "  0001f\tc3\t\t ret\t 0\n_f\tENDP\n")
    orig = ["00401000 mov eax, dword ptr [esp + 4]", "00401004 mov ecx, dword ptr [0x5e2930]",
            "0040100a fld dword ptr [eax + 8]", "0040100d call 0x478f30", "00401012 push 0x5a0010",
            "00401017 lea ecx, [ecx]", "0040101a push 0x5a1000", "0040101f ret"]

    def score(lines):
        p = os.path.join(d, "o.s")
        open(p, "w").write("\n".join(lines) + "\n")
        o = tsweep.orig_instrs(p)
        ours = tsweep.listing_instrs(lst, "_f")
        return tsweep.bdiff(ours, o), tsweep.regdiff(ours, o)
    check("bdiff: relocations, call, OFFSET, bare symbol, frame var, npad",
          score(orig), (0, 0))
    check("bdiff: a displacement counts (regdiff blind to it)",
          score(orig[:2] + ["0040100a fld dword ptr [eax + 0xc]"] + orig[3:]), (1, 0))
    check("bdiff: another immediate counts", score(orig[:4] + ["00401012 push 5"] + orig[5:]), (1, 0))
    jl = os.path.join(d, "j.asm")       # a jump to another (aligned) instruction than the original's
    open(jl, "w").write("_j\tPROC NEAR\n  00000\t85 c0\t\t test\t eax, eax\n  00002\t74 03\t\t je\t SHORT $LN1@j\n"
                        "  00004\t33 c0\t\t xor\t eax, eax\n  00006\t40\t\t inc\t eax\n$LN1@j:\n  00007\tc3\t\t ret\t 0\n_j\tENDP\n")
    jo = os.path.join(d, "j.s")
    got = []
    for t in ("0x401007", "0x401006"):
        open(jo, "w").write("00401000 test eax, eax\n00401002 je %s\n00401004 xor eax, eax\n00401006 inc eax\n"
                            "00401007 ret\n" % t)
        got.append(tsweep.bdiff(tsweep.listing_instrs(jl, "_j"), tsweep.orig_instrs(jo)))
    check("bdiff: a jump's target counts (same target, another one)", got, [0, 1])
    pl = os.path.join(d, "p.asm")       # npad 14 against the image's two-jump form of it
    open(pl, "w").write("_p\tPROC NEAR\n  00000\t57\t\t push\t edi\n  00001\teb 0c 8d a4 24\n\t00 00 00 00 eb 03\n"
                        "\t8d 49 00\t\t npad\t 14\n$LN2@p:\n  0000f\t5f\t\t pop\t edi\n  00010\tc3\t\t ret\t 0\n_p\tENDP\n")
    po = os.path.join(d, "p.s")
    open(po, "w").write("00401000 push edi\n00401001 jmp 0x40100f\n00401003 lea esp, [esp]\n0040100a jmp 0x40100f\n"
                        "0040100c lea ecx, [ecx]\n0040100f pop edi\n00401010 ret\n")
    check("bdiff: a pad in several jumps (npad 14) is one pad",
          (tsweep.bdiff(tsweep.listing_instrs(pl, "_p"), tsweep.orig_instrs(po)),
           tsweep.regdiff(tsweep.listing_instrs(pl, "_p"), tsweep.orig_instrs(po))), (0, 0))
    # the real thing: cl's listing (relocations unresolved) against the image link makes of the same object
    src = os.path.join(HERE, "tests", "data", "bdiff.c")
    ms = [sys.executable, os.path.join(HERE, "tools", "msvc.py")]
    subprocess.run(ms + ["cl", "/nologo", "/c", "/O2", "/Gy", "/FAsc", "/Fa" + os.path.join(d, "b.asm"),
                         "/Fo" + os.path.join(d, "b.obj"), src], capture_output=True, text=True, cwd=HERE)
    subprocess.run(ms + ["link", "/nologo", "/DLL", "/NOENTRY", "/NODEFAULTLIB", "/FORCE:UNRESOLVED", "/OPT:NOREF",
                         "/OPT:NOICF", "/MAP:" + os.path.join(d, "b.map"), "/OUT:" + os.path.join(d, "b.dll"),
                         os.path.join(d, "b.obj")], capture_output=True, text=True, cwd=HERE)
    got = []
    try:
        c2ltcg.write_listing(os.path.join(d, "b.dll"), os.path.join(d, "b.map"), os.path.join(d, "img.asm"),
                             os.path.join(d, "s"))
        for f in ("_sw", "_fsum", "_big"):
            got.append(tsweep.bdiff(tsweep.listing_instrs(os.path.join(d, "b.asm"), f),
                                    tsweep.orig_instrs(os.path.join(d, "s", f + ".s"))))
    except Exception as e:
        got = "%s: %s" % (type(e).__name__, e)
    check("bdiff: listing vs its linked image: 0 (3 functions)", got, [0, 0, 0])


def rdiff_tests():
    """tsweep.rdiff: register renaming, frame-slot renaming and scheduling moves are not distance; a read through
    the wrong register, a moved incoming argument, another displacement and a swapped base/index are."""
    import tsweep

    def sc(ours, orig):
        k = lambda xs: tsweep.bkeys([tuple(x.split(" ", 1)) if " " in x else (x, "") for x in xs])
        r = tsweep._rscore(k(ours), k(orig))
        return r["n"], r["renamed"]
    body = ["mov eax, dword ptr [esp + 4]", "mov ecx, dword ptr [eax]", "push ecx", "push eax", "call 0x401000",
            "ret"]
    ren = [x.replace("ecx", "edx").replace("eax", "ecx") for x in body]
    check("rdiff: the same code with other registers is 0 (two values renamed)", sc(body, ren), (0, 2))
    check("rdiff: a read through the register that holds the other value counts",
          sc(["mov eax, dword ptr [esp + 4]", "mov ecx, dword ptr [esp + 8]", "push eax", "ret"],
             ["mov eax, dword ptr [esp + 4]", "mov ecx, dword ptr [esp + 8]", "push ecx", "ret"])[0], 1)
    fr = ["push ebp", "mov ebp, esp", "mov dword ptr [ebp - 8], eax", "mov dword ptr [ebp - 4], ecx",
          "mov edx, dword ptr [ebp - 8]", "push edx", "mov edx, dword ptr [ebp - 4]", "push edx", "pop ebp", "ret"]
    check("rdiff: a frame laid out at other offsets is 0 (two slots renamed)",
          sc(fr, [x.replace("ebp - 8", "ebp - 12").replace("ebp - 4", "ebp - 8") for x in fr]), (0, 2))
    check("rdiff: another incoming argument counts (arguments are not renamed)",
          sc(["mov eax, dword ptr [esp + 4]", "ret"], ["mov eax, dword ptr [esp + 8]", "ret"])[0], 1)
    check("rdiff: a moved instruction counts 1, not 2",
          sc(["mov ecx, 5", "mov edx, 6", "mov eax, 7", "ret"], ["mov edx, 6", "mov eax, 7", "mov ecx, 5", "ret"])[0], 1)
    check("rdiff: base and index swapped count 1 (the IL's operand order), not a register cascade",
          sc(["mov eax, dword ptr [esp + 4]", "mov ecx, dword ptr [esp + 8]", "mov edx, dword ptr [eax + ecx + 8]",
              "push edx", "ret"],
             ["mov eax, dword ptr [esp + 4]", "mov ecx, dword ptr [esp + 8]", "mov edx, dword ptr [ecx + eax + 8]",
              "push edx", "ret"])[0], 1)
    check("rdiff: a callee-saved register saved in another order is one move, not a wrong read",
          sc(["push esi", "push edi", "mov esi, ecx", "pop edi", "pop esi", "ret"],
             ["push edi", "push esi", "mov esi, ecx", "pop esi", "pop edi", "ret"])[0], 1)



def judge_tests():
    """tools/judge.py on synthetic code: what instruction selection, allocation, scheduling and layout change is
    EQUIVALENT; a changed store, branch or loop body is a MISMATCH."""
    import collections, judge as J

    def ins(lines, base):           # `call ?name`: our side's callee by name (the listing's), as a relocation
        out = []
        for k, l in enumerate(lines):
            l, _, text = l.partition(" ;; ")           # `capstone ;; listing text` (a frame variable's name)
            mn, _, ops = l.partition(" ")
            sym = None
            if mn == "call" and ops.startswith("?"):
                sym, ops = (ops.strip(), False, False), hex(base + 4 * k + 5)
            out.append(J.Ins(base + 4 * k, mn, ops.strip(), sym=sym, text=text or l))
        return out

    def addr(lines, base):          # jump targets written as L<k>: the k-th instruction
        return [re.sub(r"\bL(\d+)", lambda m: hex(base + 4 * int(m.group(1))), l) for l in lines]

    def v(a, b, func="?f@@YAHPAH@Z"):
        return J.judge(ins(addr(a, 0), 0), ins(addr(b, 0x401000), 0x401000), func,
                       image=(0x400000, 0x4000000))["verdict"]
    vj = v
    p = ["mov eax, dword ptr [esp + 4]"]
    check("judge: other registers, lea against add, inc against add 1: EQUIVALENT",
          v(p + ["lea ecx, [eax + 8]", "inc dword ptr [ecx]", "mov eax, ecx", "ret"],
            ["mov edx, dword ptr [esp + 4]", "add edx, 8", "add dword ptr [edx], 1", "mov eax, edx", "ret"]),
          "EQUIVALENT")
    check("judge: a split read-modify-write against one instruction: EQUIVALENT",
          v(p + ["mov ecx, dword ptr [eax + 0x184]", "or ecx, 1", "mov dword ptr [eax + 0x184], ecx", "ret"],
            p + ["or dword ptr [eax + 0x184], 1", "ret"]), "EQUIVALENT")
    check("judge: `= 1` against `|= 1`: MISMATCH",
          v(p + ["mov dword ptr [eax + 0x184], 1", "ret"], p + ["or dword ptr [eax + 0x184], 1", "ret"]),
          "MISMATCH")
    check("judge: two independent stores in either order: EQUIVALENT",
          v(p + ["mov dword ptr [eax + 4], 1", "mov dword ptr [eax + 8], 2", "ret"],
            p + ["mov dword ptr [eax + 8], 2", "mov dword ptr [eax + 4], 1", "ret"]), "EQUIVALENT")
    check("judge: a store moved across a load of the same field: MISMATCH",
          v(p + ["mov ecx, dword ptr [eax + 4]", "mov dword ptr [eax + 4], 1", "mov eax, ecx", "ret"],
            p + ["mov dword ptr [eax + 4], 1", "mov ecx, dword ptr [eax + 4]", "mov eax, ecx", "ret"]), "MISMATCH")
    br = ["mov eax, dword ptr [esp + 4]", "test eax, eax", "je L5", "mov eax, 1", "ret", "mov eax, 2", "ret"]
    inv = ["mov ecx, dword ptr [esp + 4]", "cmp ecx, 0", "jne L5", "mov eax, 2", "ret", "mov eax, 1", "ret"]
    check("judge: test r, r against cmp r, 0 with the branch inverted and the blocks swapped: EQUIVALENT",
          vj(br, inv), "EQUIVALENT")
    check("judge: the same with the returned values exchanged: MISMATCH",
          vj(br, [x.replace("eax, 2", "eax, 9").replace("eax, 1", "eax, 2").replace("eax, 9", "eax, 1") for x in inv]),
          "MISMATCH")
    loop = ["mov ecx, dword ptr [esp + 4]", "xor eax, eax", "add eax, dword ptr [ecx]", "add ecx, 4",
            "cmp ecx, dword ptr [esp + 8]", "jb L2", "ret"]
    loop2 = ["mov edx, dword ptr [esp + 4]", "xor eax, eax", "mov ecx, dword ptr [edx]", "add eax, ecx",
             "add edx, 4", "cmp edx, dword ptr [esp + 8]", "jb L2", "ret"]
    check("judge: a loop in other registers, the load folded or not: EQUIVALENT", vj(loop, loop2), "EQUIVALENT")
    hl = J.judge(ins(addr(loop, 0), 0), ins(addr(loop2, 0x401000), 0x401000), "?f@@YAHPAH@Z",
                 image=(0x400000, 0x4000000))["hints"]
    check("judge hints: at the loop head our ecx is the original's edx, eax is eax",
          (hl["regs"].get("ours:ecx"), hl["regs"].get("ours:eax"), list(hl["at"])), ("orig:edx", "orig:eax", ["0x401008"]))
    check("judge: the loop stepping 8 instead of 4: MISMATCH",
          vj(loop, [x.replace("edx, 4", "edx, 8") for x in loop2]), "MISMATCH")
    check("judge: a call's arguments and its result",
          (v(["push 5", "push dword ptr [esp + 8]", "call ?g@@YAHHH@Z", "add esp, 8", "ret"],
             ["push 5", "push dword ptr [esp + 8]", "call 0x478f30", "add esp, 8", "ret"]),
           v(["push 5", "push dword ptr [esp + 8]", "call ?g@@YAHHH@Z", "add esp, 8", "ret"],
             ["push 6", "push dword ptr [esp + 8]", "call 0x478f30", "add esp, 8", "ret"])),
          ("EQUIVALENT", "MISMATCH"))
    fx = ["mov eax, dword ptr [esp + 4]", "fld dword ptr [eax]", "fld dword ptr [eax + 4]", "fmul dword ptr [eax + 8]",
          "faddp st(1)", "fstp dword ptr [eax + 12]", "ret"]
    check("judge: faddp st(1) is st(1) + st(0), popped: a changed operand of the lost half is a MISMATCH",
          (v(fx, fx), v(fx, [x.replace("[eax + 8]", "[eax + 0xc]") for x in fx])), ("EQUIVALENT", "MISMATCH"))
    fm = ["mov eax, dword ptr [esp + 4]", "fld dword ptr [eax]", "fld dword ptr [eax + 4]", "fmul st(1)",
          "fstp dword ptr [eax + 12]", "fstp st(0)", "ret"]
    check("judge: fmul st(1) (no pop) is st(0) = st(0) * st(1): the other factor changed is a MISMATCH",
          (v(fm, fm), v(fm, [x.replace("fld dword ptr [eax]", "fld dword ptr [eax + 8]") for x in fm])),
          ("EQUIVALENT", "MISMATCH"))
    two = ["mov ecx, dword ptr [esp + 4]", "test ecx, ecx", "je L5", "call ?a@@YAXXZ", "ret", "call ?b@@YAXXZ", "ret"]
    two_r = ["mov ecx, dword ptr [esp + 4]", "test ecx, ecx", "jne L5", "call 0x478f30", "ret", "call 0x478f80", "ret"]
    def vk(a, b, known):
        return J.judge(ins(addr(a, 0), 0), ins(addr(b, 0x401000), 0x401000), "?f@@YAXPAH@Z",
                       image=(0x400000, 0x4000000), known=known)["verdict"]
    check("judge: two callees swapped between the paths: EQUIVALENT under a free pairing, MISMATCH once the "
          "project's symbol map anchors it",
          (vk(two, two_r, {}), vk(two, two_r, {"?a@@YAXXZ": 0x478f30, "?b@@YAXXZ": 0x478f80})),
          ("EQUIVALENT", "MISMATCH"))
    keep = ["push esi", "mov esi, ecx", "sub esp, 4", "lea eax, [esp] ;; lea eax, _v$[esp+8]", "push eax",
            "call ?g@@YAXPAH@Z", "add esp, 4", "mov eax, esi", "add esp, 4", "pop esi", "ret"]
    spill = ["sub esp, 8", "mov dword ptr [esp + 4], ecx", "lea eax, [esp]", "push eax", "call 0x478f30",
             "add esp, 4", "mov eax, dword ptr [esp + 4]", "add esp, 8", "ret"]
    def vf(a, b, frame):
        return J.judge(ins(addr(a, 0), 0), ins(addr(b, 0x401000), 0x401000), "?f@@YAHXZ",
                       image=(0x400000, 0x4000000), frame=frame, frame_objects=True)["verdict"]
    check("judge (--frame-objects, experimental): a local's address escapes and the original spills a value we keep "
          "in a register: MISMATCH with every local as memory, EQUIVALENT with the listing's frame objects",
          (vf(keep, spill, None), vf(keep, spill, [("_v$", -8, 4, True)]),
           vf(keep, [x.replace("ecx", "edx") if "esp + 4], ecx" in x else x for x in spill], [("_v$", -8, 4, True)])),
          ("MISMATCH", "EQUIVALENT", "MISMATCH"))
    job = dict(id="t", func="?f@@YAHPAH@Z", image=[0x400000, 0x4000000],
               ours=[[i.addr, i.mn, i.ops, None] for i in ins(addr(loop, 0), 0)],
               orig=[[i.addr, i.mn, i.ops] for i in ins(addr(loop2, 0x401000), 0x401000)])
    ms = J.mutants(job, per_kind=4)
    got = collections.Counter(J.judge_job(m)["verdict"] for m in ms)
    check("judge: no mutant of the loop's original is EQUIVALENT (%d mutants)" % len(ms), got["EQUIVALENT"], 0)


bdiff_tests()
rdiff_tests()
judge_tests()



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

def loadcse_tests():
    """Load reuse and read-modify-write (docs/c2/loadcse.md): the rule loadcse.py names for each probe of
    tests/data/loadcse.cpp, the PAS-index dump it needs (C2TRACE_ILPAS) and the rwsweep `unalias` family."""
    src = os.path.join(HERE, "tests", "data", "loadcse.cpp")
    d = tempfile.mkdtemp()

    def rep(fn_):
        js = os.path.join(d, re.sub(r"\W", "_", fn_) + ".json")
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "loadcse.py"), src, "--func", fn_, "--json", js,
                            "--", "/TP", "/MT", "/GS", "/EHsc", "/Gy", "/O2"], capture_output=True, text=True, cwd=HERE)
        if VERBOSE:
            print(r.stdout[-3000:], r.stderr[-2000:])
        return json.load(open(js)) if os.path.exists(js) else {"locations": [], "rmw": []}

    def statuses(j):
        return [ld["status"] for loc in j["locations"] for ld in loc["loads"]]

    old13 = BUILD.startswith("13.10")
    j = rep("drain0")
    check("loadcse: one end pointer for read and decrement: no RMW (M2 shared result)",
          [("M2" in r["status"], r["status"].startswith("NOT FORMED")) for r in j["rmw"]], [(True, True)])
    j = rep("drain1")
    check("loadcse: through an inline accessor on the sub-object: RMW formed%s" % (", split again on 13.10" if old13 else ""),
          [r["status"] for r in j["rmw"]], ["FORMED, SPLIT after allocation" if old13 else "FORMED"])
    j = rep("::twolevel")
    check("loadcse: store through p->q in the arm: its reads get another alias class (R2)",
          any(s_.startswith("RELOADED R2") for s_ in statuses(j)), True)
    j = rep("::twolevel_local")
    check("loadcse: the same with Q* q = p->q: nothing reloaded", any(s_.startswith("RELOADED") for s_ in statuses(j)), False)
    j = rep("::refmember")
    check("loadcse: a reference to the member, used twice, is another base (R3 through _re)",
          any(ld["status"].startswith("RELOADED R3") and "_re" in ld["why"] for loc in j["locations"] for ld in loc["loads"]),
          True)
    j = rep("::killcall")
    check("loadcse: a call between: R5 kill", any(s_.startswith("RELOADED R5") for s_ in statuses(j)), True)
    j = rep("::samechain")
    check("loadcse: (m | a) | 2 is one operator chain: `or [m]`%s" % (", split again on 13.10" if old13 else ""),
          [r["status"] for r in j["rmw"]], ["FORMED, SPLIT after allocation" if old13 else "FORMED"])
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "rwsweep.py"), src, "--func", "::refmember",
                        "--families", "unalias", "--list", "--", "/TP", "/O2"], capture_output=True, text=True, cwd=HERE)
    check("rwsweep unalias: the reference `re` is offered for removal (2 uses)",
          "unalias ref &re = x.e removed" in r.stdout and "(2 uses)" in r.stdout, True)


loadcse_tests()


def sulever_tests():
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "sulever.py"),
                        os.path.join(HERE, "tests", "data", "sulever.cpp"), "--func", "kind3", "--site", "*:_lo>+0]",
                        "--spell", "(unsigned int)lo", "--jobs", "3", "--", "/TP", "/O2"],
                       capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    check("sulever: 3-operand OR, lo after the mask term: decided by SU", "A>B is not satisfied; decided by SU" in r.stdout,
          True)
    m = re.search(r"^view-mask\s.*order: \S+ \S+ SU\d n\d h\w+ \| \((\S+)", r.stdout, re.M)
    check("sulever: the view-mask spelling puts lo second", bool(m and m.group(1).startswith("_lo+0")), True)


sulever_tests()


def toolgap_tests():
    """Tool gaps of the second benchmark round (work/bench/spore/RESULTS2.md "Tool bugs" 8-12, mb item 5, the stray
    b/ directory): realize alias2 with two reads and --asm-func, rwsweep byval / wrap / touch / combinations, the
    case-safe output names, the original's prologue in c2slots, c2trace's keep directory."""
    import types, hashlib, rwsweep, tsweep, c2slots, c2trace, realize
    TG = os.path.join(HERE, "tests", "data", "toolgaps.cpp")
    fl = ["--", "/TP", "/MT", "/EHsc", "/Gy", "/O2"]
    # realize alias2 on a pointer member read only twice: one later read through the reference, reloaded
    d = os.path.join(tempfile.mkdtemp(), "rz")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "realize.py"), "edit", TG, "--func", "lt__base",
                        "alias2:p->m", "--asm-func", "lt__base", "--keep", d] + fl, capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])

    def loads(asm, func):
        try:
            return sum("+92]" in o for _, o in tsweep.listing_instrs(asm, func))
        except Exception as e:
            return str(e)[:40]
    v13 = c2trace.c2build.name().startswith("13.10")
    check("realize alias2:p->m with two reads: a construct, 1 -> %d loads" % (1 if v13 else 2),
          ("read 2 of 2 through it (one use)" in r.stdout, loads(os.path.join(d, "base", "base.asm"), "lt__base"),
           loads(os.path.join(d, "e0_000", "e0_000.asm"), "lt__base")), (True, 1, 1 if v13 else 2))
    # --asm-func: the score reads that function of our listing
    asm = os.path.join(d, "base", "base.asm")
    f = types.SimpleNamespace(instrs=[("nop", "")], asm_path=asm)
    ctx = types.SimpleNamespace(a=types.SimpleNamespace(func="lt__base", asm_func="lt__other"))
    got = realize.Ctx.scored(ctx, f) if os.path.exists(asm) else None
    check("realize --asm-func: the scored instructions are that function's",
          got == tsweep.listing_instrs(asm, "lt__other") if got is not None else None, True)

    text = open(TG).read()
    fn = rwsweep.body_of(text, "?get@BvUser@@QAEPAXH@Z")
    B = rwsweep.Body(text, fn, "BvUser")
    call = text.index("touch_ref(k, any)") - B.b
    check("rwsweep wrap: an argument to a `bool&` parameter only through hs_bind (C2664 otherwise)",
          sorted(v[1].split("(")[0] for v in rwsweep.gen_wrap(B) if "(any) @%d" % (call + 13) in v[1]), ["hs_bind"])
    t2 = "struct S { int a; }; void f(const int c, S s) { int x = c; x++; (void)x; }"
    forms = {tuple(v[1].split()[:2]) for v in rwsweep.gen_touch(rwsweep.Body(t2, rwsweep.body_of(t2, "f"), None))}
    check("rwsweep touch: no `c = c` for a const c, no if/__assume on a struct",
          (("self", "c") in forms, ("if", "s") in forms, ("assume", "s") in forms, ("self", "x") in forms,
           ("if", "c") in forms), (False, False, False, True, True))
    t3 = ("int f(float a, float b, int* p) { if (a < b) return 1; if (p[0] >= (int)b && a) return 2; "
          "while (p[1] > 4) p++; return 0; }")
    B3 = rwsweep.Body(t3, rwsweep.body_of(t3, "f"), None)
    check("rwsweep cmp: `a < b` and `p[1] > 4` mirrored and negated; a condition with && left alone",
          [v[2][0][2] for v in rwsweep.gen_cmp(B3)], ["b > a", "!(a >= b)", "4 < p[1]", "!(p[1] <= 4)"])
    bv = list(rwsweep.gen_byval(B))
    check("rwsweep byval: map.find(&k) through `typename M::It rz_bv_find1(M&, const A0&)` before the definition",
          (len(bv), bv[0][2][1][2] if bv else None, bv[0][2][0][2].strip() if bv else None,
           text[bv[0][2][0][0]:].startswith("void* BvUser::get") if bv else None),
          (1, "rz_bv_find1(map, &k)", "template <class M, class A0> inline typename M::It rz_bv_find1(M& m, const A0& a0) "
           "{ return m.find(a0); }", True))
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "rwsweep.py"), TG, "--func", "?get@BvUser@@QAEPAXH@Z",
                        "--families", "byval,wrap", "--vars", "any,map"] + fl, capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    m = re.search(r"(\d+) measured, (\d+) failed to compile", r.stdout)
    check("rwsweep byval and wrap: every variant compiles", (int(m.group(2)), int(m.group(1)) > 2) if m else None, (0, True))
    # combinations: with --asm-vs, code-changing singles that lower the bdiff are combined (Spore 0099f110)
    rows = [dict(i=0, shape=0, bd=9, rv=5, dgap=None), dict(i=1, shape=4, bd=8, rv=5, dgap=None),
            dict(i=2, shape=2, bd=7, rv=5, dgap=None), dict(i=3, shape=3, bd=12, rv=5, dgap=None)]
    var = [("x", "", [(k * 10, k * 10 + 1, "")]) for k in range(4)]
    good, better, combos = rwsweep.plan_combos(rows, var, (5, 10), None, 2)
    check("rwsweep combinations: improving code-changing singles are combined too, best gain first",
          ([x["i"] for x in good], [x["i"] for x in better], [c[2] for c in combos]), ([0], [2, 1], [[2, 1], [0, 2], [0, 1]]))
    rows[3]["rv"] = 3       # a higher bdiff with a lower rdiff (a register-renaming ripple): better, and first
    check("rwsweep combinations: rdiff ranks before bdiff",
          [x["i"] for x in rwsweep.plan_combos(rows, var, (5, 10), None, 2)[1]], [3, 2, 1])
    check("rwsweep combinations: without --asm-vs only code-neutral ones (none here)",
          rwsweep.plan_combos(rows, var, None, None, 2)[2], [])
    # output names that differ only in case (macOS: one directory)
    dd = tempfile.mkdtemp()
    names = [tsweep.out_name(os.path.join(dd, n)) for n in ("r2_D.cpp", "r2_d.cpp", "R2_d.cpp")]
    okey = os.path.abspath(dd) + ("\0" + tsweep.c2trace.c2build.name() if tsweep.c2trace.c2build.table() else "")
    check("out_name: r2_D / r2_d / R2_d differ ignoring case; lowercase keeps <stem>-<dirhash> (+ the build if not 14.00)",
          (len({n.lower() for n in names}), names[1] == "r2_d-" + hashlib.md5(okey.encode()).hexdigest()[:6]),
          (3, True))
    # c2slots: an instruction scheduled into the prologue (M&B 004362e0) does not end it
    ins = [(0, "mov", "eax, dword ptr [esp + 0x18]"), (4, "sub", "esp, 8"), (7, "push", "ebx"),
           (8, "mov", "ebx, dword ptr [esp + 0x20]"), (12, "push", "ebp"), (13, "push", "esi"), (14, "push", "edi"),
           (15, "push", "ecx"), (16, "lea", "edx, [esp + 0x14]"), (20, "push", "edx"), (21, "mov", "ecx, esi"),
           (23, "call", "0x432790"), (28, "mov", "ecx, dword ptr [esp + 0x20]"), (32, "ret", "0x18")]
    keys = dict(c2slots.orig_slots(ins)[0])
    check("c2slots orig_slots: prologue pushes after a scheduled load are not call arguments",
          (keys.get(0), keys.get(3), keys.get(8), keys.get(12)), ([24], [20], [-8], [8]))
    # c2trace keep directories: from the repository root a relative --keep outside work/ goes under work/keep/
    cwd = os.getcwd()
    try:
        os.chdir(HERE)
        got = (c2trace.keep_path("b/k_x"), c2trace.keep_path("work/scratch/k"), c2trace.keep_path("/tmp/k"))
    finally:
        os.chdir(cwd)
    check("c2trace keep_path: root-relative b/k_x -> work/keep/b/k_x; work/.. and absolute unchanged", got,
          (os.path.join(HERE, "work", "keep", "b", "k_x"), os.path.join(HERE, "work", "scratch", "k"), "/tmp/k"))
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2trace.py"), "no_such_file.cpp", "--keep",
                        os.path.join(dd, "k")], capture_output=True, text=True, cwd=dd)
    check("c2trace: a missing source stops before the keep directory is made",
          (r.returncode != 0, os.path.exists(os.path.join(dd, "k"))), (True, False))


toolgap_tests()


def schedwant_tests():
    src = os.path.join(HERE, "tests", "data", "schedwant.cpp")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2sched.py"), src, "--func", "?kcopy@", "--all",
                        "--", "/TP", "/O2"], capture_output=True, text=True, cwd=HERE).stdout
    reg, lex, region, issued = None, {}, None, []
    for ln in r.splitlines():
        m = re.match(r"-- region (\d+)", ln)
        if m:
            reg = int(m.group(1))
            lex, issued = {}, []
            continue
        m = re.match(r"\s+(\d+)\s+\d+\s+\d+\s+\S+\s+[0-9a-f]+\s+\d+\s+\d+\s+(.*)$", ln)
        if m:
            lex[int(m.group(1))] = m.group(2)
        m = re.match(r"\s+cyc\s+\d+ .? lex\s+(\d+)", ln)
        if m:
            issued.append(int(m.group(1)))
        sv = {k: int(m_.group(1)) for k, t in lex.items() for m_ in [re.search(r"^mov .*<- s(\d+):\d+\s*$", t)] if m_}
        ld = sorted(sv, key=lambda k: -sv[k])[:1] if len(sv) >= 2 else []     # key.b: the field view made later
        st = [k for k, t in lex.items() if "*8+0]" in t.split("<-")[0]]
        if ld and st and issued and len(issued) == len(lex):
            region, L, S, order = reg, ld[0], st[0], list(issued)
    ok = region is not None and order.index(S) < order.index(L)
    check("schedwant: key.b's load waits for the first element store", ok, True)
    if not ok:
        return
    order.remove(L)
    order.insert(order.index(S), L)
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "schedwant.py"), src, "--func", "?kcopy@",
                        "--region", str(region), "--want-lex", ",".join(map(str, order)), "--", "/TP", "/O2"],
                       capture_output=True, text=True, cwd=HERE).stdout
    if VERBOSE:
        print(r[-3000:])
    check("schedwant: the replay reproduces the traced order", "(= traced)" in r, True)
    check("schedwant: unalias of that load gives the wanted order",
          bool(re.search(r"^\s+WANTED\s+unalias:%d\s*$" % L, r, re.M)), True)


schedwant_tests()


def framefields_tests():
    d = os.path.join(HERE, "tests", "data", "framefields")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "framefields.py"), os.path.join(d, "a.cpp"),
                        "--func", "ins16", "--orig-src", os.path.join(d, "b.cpp"), "--", "/TP", "/O2"],
                       capture_output=True, text=True, cwd=HERE).stdout
    if VERBOSE:
        print(r[-3000:])
    check("framefields: a 16-byte local whose original is larger (pair<key, value>)",
          bool(re.search(r"_value\$: \d+ of its references touch another field .* the original's type is larger", r)), True)


framefields_tests()


# effectmine (docs/c2/effectmine.md, tools/effectmine.py), on every c2 build: mutations generated, compiled traced,
# their effects read in each model's vocabulary and stored; a scratch database (EFFECTMINE_DIR), not work/effectmine
def effectmine_tests():
    EM = os.path.join(HERE, "tests", "data", "effectmine.cpp")
    tool_ = os.path.join(HERE, "tools", "effectmine.py")
    env = dict(os.environ, EFFECTMINE_DIR=tempfile.mkdtemp(prefix="emtest_"))
    r = subprocess.run([sys.executable, tool_, "mine", EM, "--func", "em__base", "--list"] + FLAGS,
                       capture_output=True, text=True, cwd=HERE, env=env)
    classes = set(re.findall(r"^\s+\d+ (\S+)", r.stdout, re.M))
    check("effectmine --list: rwsweep, realize and its own classes generated",
          {"copy", "wrap.var.hs_id", "pad.init.top", "decl.extvar.before", "commute", "move.end"} <= classes, True)
    r = subprocess.run([sys.executable, tool_, "mine", EM, "--func", "em__base", "--features", "regs,keys", "--eager-feats", "keys",
                        "--classes", r"^(copy|pad\.init\.top|decl\.extvar\.before)$", "--max-compiles", "60",
                        "--jobs", "8"] + FLAGS, capture_output=True, text=True, cwd=HERE, env=env)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    db = [json.loads(l) for l in open(os.path.join(env["EFFECTMINE_DIR"], "effects.jsonl"))] \
        if os.path.exists(os.path.join(env["EFFECTMINE_DIR"], "effects.jsonl")) else []
    cp = [x for x in db if x["cls"] == "copy" and x["ok"]]
    check("effectmine: a copy of n is +2 on n and one more candidate in the copy's block, instructions unchanged",
          any(any(re.match(r"ref:n:\d+:\+2$", e) for e in x["eff"]["R"]) and any(e.startswith("P:") for e in x["eff"]["R"])
              and x["code"]["code"] == 0 for x in cp), True)
    pad = [x for x in db if x["cls"] == "pad.init.top" and x["meta"].get("n") == 1 and x["ok"]]
    check("effectmine: one unused initialised local is one frame key, code identical",
          [("keys:frame:+1" in x["eff"].get("K", []), x["code"]["ident"]) for x in pad], [(True, 0)])
    dec = [x for x in db if x["cls"] == "decl.extvar.before" and x["ok"]]
    check("effectmine: dummy extern declarations before the function: compiled, effects recorded",
          bool(dec) and all("types" in x for x in dec), True)
    r = subprocess.run([sys.executable, tool_, "table", "--min-n", "1"], capture_output=True, text=True, cwd=HERE, env=env)
    check("effectmine table: the copy class shows R.ref+ with the instructions unchanged",
          bool(re.search(r"^copy\s+\d+\s+1\s.*\d+\(\s*\d+\)", r.stdout, re.M)) and "R.ref+" in r.stdout, True)


effectmine_tests()


def untraced_tests():
    """Temps with no Color pop and no RegAssign pick (Mount & Blade 0x529c30 t437, 0x43c9c0 t385/t386, 0x524420 t814),
    commutative two-address votes in regmap (0x524420 t728), and a closed stdout (rasim / c2trace | head)."""
    import tsweep
    d = tempfile.mkdtemp()
    # 1. cl 13.10's Riscify: `add eax, [m]` split into a load to a new temp, given its register by GetReg (riscify.c)
    rsnap = os.path.join(d, "r.json")
    out = tool("scratch", os.path.join(HERE, "tests", "data", "riscify.c"), "--func", "pick2", "--export", rsnap)
    rrows = re.findall(r"^\s+R\s+(\d+) blk \S+\s+\S+\s+(\S+)\s+(\S+)\s+busy \S+\s+made (t\d+)", out, re.M)
    rs = subprocess.run([rasim, "scratch", rsnap], capture_output=True, text=True).stdout
    m = re.search(r"riscify: (\d+) of (\d+) GetReg calls predicted \(register\); cursor right at (\d+) of", rs)
    unm = re.findall(r"^\s+\?\s+(t\d+)\s+\S+\s+\S+\s+.*\[(.+)\]$", out, re.M)
    if BUILD.startswith("13.10"):
        check("scratch: Riscify's GetReg call makes the load temp (edx), offset known; rasim replays every call",
              (len(rrows), rrows[0][1] if rrows else None, rrows[0][2] != "gone" if rrows else None,
               m and m.group(1) == m.group(2) == m.group(3)), (1, "edx", True, True))
        t = rrows[0][3] if rrows else "t0"
        w = subprocess.run([rasim, "scratch", rsnap, "want:%s=eax" % t], capture_output=True, text=True).stdout
        check("rasim scratch want: on a Riscify temp names the GetReg call and scans rdrop/rins", 
              ("made by Riscify (GetReg call R%s" % rrows[0][0] if rrows else "?") in w, True)
    else:
        check("scratch: no Riscify on this build; the temps without a pick are named with their phase",
              (len(rrows), m is None, bool(unm) and all(ph for _, ph in unm)), (0, True, True))
    if unm:
        w = subprocess.run([rasim, "scratch", rsnap, "want:%s=edi" % unm[0][0]], capture_output=True, text=True).stdout
        check("rasim scratch want: on a temp no modelled phase assigned says which phase did", "rasim does not model that phase" in w, True)
    # 2. a pick whose IL name at pick time is not its final-code name (0x524420 t814 was pick 25 as t728)
    src = os.path.join(HERE, "tests", "data", "twoaddr.c")
    k = os.path.join(d, "k")
    out = tool("scratch", src, "--func", "addsum", "--keep", k)
    check("scratch: a pick under another IL name shows the temp the final code calls it",
          bool(re.search(r"^\s+\d+\s.*\[temp t\d+ in the final code\]$", out, re.M)), True)
    # 3. regmap: `add eax, esi` against an original `add esi, eax` (the same operands, the sum in the other register)
    asm = os.path.join(k, "twoaddr.asm")
    lst = tsweep.listing_instrs(asm, "_addsum")
    rows = [list(x) for x in zip(lst.addrs, (dd[0] for dd in lst.decoded), (dd[1] for dd in lst.decoded))]
    for i, (o, mn, ops) in enumerate(rows):
        mm = re.fullmatch(r"(e\w\w), (e\w\w)", ops)
        if mn == "add" and mm and i and rows[i - 1][1] in ("call", "mov"):
            a, b = mm.groups()
            rows[i][2] = "%s, %s" % (b, a)
            for r in rows[i + 1:]:
                if r[1] == "mov" and r[2].endswith(", " + a) and "0x9c" in r[2]:
                    r[2] = r[2][:-len(a)] + b
                    break
            break
    orig = os.path.join(d, "addsum.s")
    open(orig, "w").write("".join("%08x %s %s\n" % tuple(r) for r in rows))
    out = tool("regmap", src, "--func", "addsum", "--asm-vs", orig, "--asm-func", "_addsum")
    wants = re.findall(r"^(\S+)\s+\S+\s+\S+\s+\S+\s+(WANTS \S+|mixed.*)$", out, re.M)
    check("regmap: commutative two-address op in the other order: only the sum WANTS, noted as operand order",
          (len(wants), all(w[1].startswith("WANTS") for w in wants), "commutative two-address op" in out,
           "      from: scratch pick" in out), (1, True, True, True))
    # base/index order inside an address and two nearby instructions issued the other way round are not register votes
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import regmap
    rb, fl = regmap.addr_order("dword ptr [edi + edx + 0x30], eax", "dword ptr [edx + edi + 0x30], eax",
                               ["edx", "edi", "eax"])
    ours = [("mov", "ebx, dword ptr [esp + 0x58]"), ("fmul", "dword ptr [esp + 0x30]"), ("mov", "eax, dword ptr [esp + 0x24]")]
    orig = [ours[2], ours[1], ours[0]]
    check("regmap: base/index order counted as agreeing; a load pair issued the other way round paired crosswise",
          (rb, sorted(fl), regmap.reorder([(0, 0), (1, 1), (2, 2)], ours, orig, [])), (["edi", "edx", "eax"],
           ["edi", "edx"], [(0, 2), (1, 1), (2, 0)]))
    # 4. a reader that goes away: no panic, no traceback (status 141 / SIGPIPE)
    bad = []
    for cmd in ([rasim, "scratch", rsnap, "-v"], [rasim, "need", snap, "reg:k=esi"],
                [sys.executable, os.path.join(HERE, "tools", "c2trace.py"), "--help"],
                [sys.executable, os.path.join(HERE, "tools", "regmap.py"), "--help"]):
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdout.close()
        err = p.stderr.read().decode(errors="replace")
        rc = p.wait()
        if "panicked" in err or "Traceback" in err or "BrokenPipeError" in err or rc not in (0, 2, 141, -13):
            bad.append((os.path.basename(cmd[0]), cmd[1], rc, err[-200:]))
    check("closed stdout: rasim and c2trace/regmap end quietly (EPIPE)", bad, [])


untraced_tests()


def mbrequest_tests():
    """Mount & Blade session requests: several scratch wants answered jointly (0x4b1930: one ins for five picks), the
    terms behind `need`'s weights (0x4ea290: ebx:400), the locals and parameters with no `regs` row and why
    (0x4bd4d0: line), and why Riscify split an instruction or not (0x43c9c0: R14, R15)."""
    d = tempfile.mkdtemp()
    # 1. joint wants: the last three rotation picks of eA each want the next register: one ins:K before them
    js = json.load(open(ssnap))
    run = max(js["runs"], key=lambda r: len(r["picks"]))
    rot = [p for p in run["picks"] if p["how"] == "ROT" and p["cls"] == "int"][-3:]
    nxt = {"eax": "ecx", "ecx": "edx", "edx": "eax"}
    r = subprocess.run([rasim, "scratch", ssnap] + ["want:%s=%s" % (p["tmp"], nxt[p["reg"]]) for p in rot],
                       capture_output=True, text=True).stdout
    m = re.search(r"best: (\d+) of (\d+) with (\d+) edit", r)
    first = re.search(r"^  (\S+)\s+\d+ of \d+;", r, re.M)
    check("rasim scratch: three wants, one joint edit (ins:K / drop:K), before the per-want answers",
          (m.groups() if m else None, bool(first and first.group(1).startswith(("ins:", "drop:"))),
           "per want:" in r and r.index("joint:") < r.index("per want:")), (("3", "3", "1"), True, True))
    # 2. need: each nonzero weight with its terms (here the value's own preference)
    r = subprocess.run([rasim, "need", snap, "pref:k:edi", "reg:k=esi"], capture_output=True, text=True).stdout
    check("rasim need: a weight is explained term by term (own PREF -1)",
          bool(re.search(r"^  k weights \(wanted esi\):\n    edi:-1 = -1 its own preference for edi", r, re.M)), True)
    # 3. regs: the locals and parameters with no row, and the phase that dropped each
    src = os.path.join(HERE, "tests", "data", "regmissing.c")
    msnap = os.path.join(d, "m.json")
    out = tool("regs", src, "--func", "f", "--export", msnap)
    why = dict(re.findall(r"^    (\w+)\s+(?:local|param|\?)\s+(\S+)", out, re.M))
    check("regs: volatile, address-taken and propagated-away locals listed with their reason",
          (why.get("v"), why.get("x"), why.get("copy")), ("volatile", "address-taken:", "every"))
    r = subprocess.run([rasim, "need", msnap, "reg:v=esi"], capture_output=True, text=True)
    check("rasim need on a variable with no row prints its reason", "has no row in the regs table: volatile" in r.stdout + r.stderr, True)
    # 4. Riscify (cl 13.10): split and kept (the scheduler separated load and store), split and undone, not split
    if BUILD.startswith("13.10"):
        src = os.path.join(HERE, "tests", "data", "riscify2.c")
        res = []
        for fn in ("rmw", "rmw0"):
            out = tool("scratch", src, "--func", fn)
            res.append(re.findall(r"^\s+R\s+\d+ .*\n\s+(split, kept|split, undone after Schedule|not split: no memory operand)", out, re.M))
        check("scratch: Riscify split reasons (rmw kept, rmw0 undone after Schedule, register forms not split)",
              (res[0][:1], "not split: no memory operand" in res[0], res[1][:1]),
              (["split, kept"], True, ["split, undone after Schedule"]))


mbrequest_tests()


def icount_tests():
    """tools/icount.py (docs/c2/icount.md): the verdict class of each probe pair of tests/data/icount.cpp (X_ours
    against X_orig of the same compile), and the tracer's function filter (C2TRACE_FUNC / `func`)."""
    src = os.path.join(HERE, "tests", "data", "icount.cpp")
    d = tempfile.mkdtemp()

    def classes(p):
        js = os.path.join(d, p + ".json")
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "icount.py"), src, "--func", "::%s_ours" % p,
                            "--vs-func", "::%s_orig" % p, "--json", js, "--", "/TP", "/O2"],
                           capture_output=True, text=True, cwd=HERE)
        if VERBOSE:
            print(r.stdout[-3000:], r.stderr[-2000:])
        j = json.load(open(js)) if os.path.exists(js) else {}
        hints[p] = j.get("hints", [])
        return j.get("ours", 0) - j.get("orig", 0), sorted(j.get("classes", {}))
    hints = {}

    old13 = BUILD.startswith("13.10")
    check("icount: one load where the original reloads: removed-by:GlobalOptimize/CSE", classes("cse"),
          (-1, ["removed-by:GlobalOptimize/CSE"]))
    dl, cl = classes("alias")
    check("icount: a reference used twice keeps a reload: kept:alias", (dl, cl == ["kept:alias"]), (1, True))
    dl, cl = classes("kill")
    check("icount: a store through a loaded pointer between two reads: kept:kill", (dl, "kept:kill" in cl), (2, True))
    dl, cl = classes("range")
    check("icount: the struct copy's field tested twice: %s" % ("folded:range-check (13.10)" if old13 else
          "folded in both variants (14.00, 15.00)"), (dl, "folded:range-check" in cl) if old13 else (dl, cl),
          (-5, True) if old13 else (0, []))
    check("icount: the first of two stores is dead: removed-by:GlobalOptimize/DeadStores", classes("dead"),
          (-1, ["removed-by:GlobalOptimize/DeadStores"]))
    dl, cl = classes("tail")
    check("icount: equal tails merged (twins from the merge events): merged:FgDoLateFlowOpts",
          (dl, "merged:FgDoLateFlowOpts" in cl), (-8, True))
    check("icount: a statement the original has: structure", classes("stmt"), (-1, ["structure"]))
    classes("fp")
    check("icount: a float rounded through its home where the original keeps it: %s" % (
          "no /fp hint (13.10)" if old13 else "flags hint /fp:fast"),
          [("/fp:fast" in h) for h in hints.get("fp", [])], [] if old13 else [True])
    # the function filter: with C2TRACE_FUNC the DLL logs only while that function is compiled
    env = dict(os.environ, C2TRACE_FUNC="?dead_ours@@")
    kd = os.path.join(d, "ff")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2trace.py"), "--keep", kd, src, "--", "/TP"],
                       capture_output=True, text=True, cwd=HERE, env=env)
    log = open(os.path.join(kd, "c2trace.log"), errors="replace").read() if os.path.exists(os.path.join(kd, "c2trace.log")) else ""
    ev = [l for l in log.splitlines() if l.startswith("E ")]
    check("tracer func filter: `func` logged, only the named function's Color/Spillcosts events",
          ("# func 1" in log, len(ev) > 0 and all("dead_ours" in l for l in ev if "@Color@" in l or '"' in l)),
          (True, True))


def ccache_ice_tests():
    """tools/ccache.py never stores, nor replays, a compile that died with C1001: a traced c2's crash depends on
    things outside the key (the compile server's tracer), and one cached crash failed icount `stmt` on 13.10."""
    import ccache
    old = ccache.DIR
    ccache.DIR = tempfile.mkdtemp()
    try:
        ice = {"rc": 2, "out": "x.cpp\nfatal error C1001: INTERNAL COMPILER ERROR\n"}
        stored = ccache.put("k_ice", {}, ice)
        os.makedirs(os.path.join(ccache.DIR, "k_old"))
        json.dump(ice, open(os.path.join(ccache.DIR, "k_old", "meta.json"), "w"))       # an entry from before
        ok = ccache.put("k_ok", {}, {"rc": 0, "out": "x.cpp\n"})
        check("ccache: a C1001 compile is not stored, an old C1001 entry is a miss, a good one is a hit",
              (stored, ccache.get("k_ice"), ccache.get("k_old"), ccache.get("k_ok") == ok and ok is not None),
              (None, None, None, ccache.mode() not in ("0", "refresh")))
    finally:
        ccache.DIR = old


ccache_ice_tests()
icount_tests()


# External compiler rules (docs/c2/rules_xref.md, "how to add a rule probe"): each probe is
# tests/data/rules/R<id>_<name>.cpp, compiled on the current C2_BUILD; expectations are dicts keyed by RKEY.
RKEY = BUILD[:5] if BUILD.startswith(("14.00", "15.00")) else BUILD      # 14.00, 15.00, 13.10.3077, 13.10.3052
RULES = os.path.join(HERE, "tests", "data", "rules")


def rule_compile(name, *flags):
    """Untraced /O2 /TP /Gy /FAsc compile of tests/data/rules/<name> (+ cl flags, e.g. /DVARIANT); the listing text."""
    d = tempfile.mkdtemp()
    subprocess.run([sys.executable, os.path.join(HERE, "tools", "msvc.py"), "cl", "/nologo", "/c", "/O2", "/TP", "/Gy",
                    "/FAsc", "/Fa" + os.path.join(d, "x.asm"), "/Fo" + os.path.join(d, "x.obj")] + list(flags)
                   + [os.path.join(RULES, name)], capture_output=True, text=True, cwd=HERE)
    p = os.path.join(d, "x.asm")
    return open(p, errors="replace").read() if os.path.exists(p) else ""


def rule_proc(asm, prefix):
    """The listing of the function whose decorated name starts with `prefix` ('' if absent)."""
    m = re.search(r"^(%s\S*)\s+PROC\b(.*?)^\1\s+ENDP" % re.escape(prefix), asm, re.M | re.S)
    return m.group(2) if m else ""


def rule_trace(name, func, hook, *flags):
    """Traced compile (compile server) of a rule probe, logging only while `func` is compiled: the logged events."""
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2trace.py"), "-H", hook, os.path.join(RULES, name),
                        "--", "/TP"] + list(flags), capture_output=True, text=True, cwd=HERE,
                       env=dict(os.environ, C2TRACE_FUNC=func))
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    return re.findall(r"^\s+\d+ > (.*)$", r.stdout, re.M)


def rules_calls_tests():
    """T02-T04: R850 (_FHasMustAlign), R860 (struct parameter), R865 (entry enter-loads), R1050 (null test after a copy)."""
    regp = lambda proc: dict(re.findall(r"^; _(\w+)\$ = (\w+)$", proc, re.M))
    # R850: does m (loaded into ebx at helper's entry) arrive in ebx?  14.00/15.00: module-wide _FHasMustAlign (any
    # compiled function with an aligned local, or /arch:SSE2); 13.10: only callers with an aligned local (CGNODE 0x1000)
    hp = "?helper@@YAHPAHHHH@Z"
    got = tuple(regp(rule_proc(rule_compile("R850_mustalign.cpp", *f), hp)).get("m")
                for f in ([], ["/DBEFORE"], ["/DAFTER"], ["/DCALLER"], ["/arch:SSE2"]))
    want = {"14.00": ("ebx", None, None, None, None), "15.00": ("ebx", None, None, None, None),
            "13.10.3077": ("ebx", "ebx", "ebx", None, "ebx"), "13.10.3052": ("ebx", "ebx", "ebx", None, "ebx")}
    check("R850: ebx register parameter: declared / before / after / caller / SSE2", got, want[RKEY])
    hook = {"14.00": "@MdmFlexParams@4=g10d08fe0>0", "15.00": "512306d0=g512470d8>0",        # _FHasMustAlign
            "13.10.3077": "1074b268=eax>0x99>0x50", "13.10.3052": "1074c0c7=eax>0x99>0x50"}  # CGNODE+0x50
    ev = [rule_trace("R850_mustalign.cpp", "?helper@@", hook[RKEY], f) for f in ("/DAFTER", "/DCALLER")]
    val = [int(e[0].rsplit("=", 1)[1], 16) if e else None for e in ev]
    if RKEY.startswith("13.10"):
        got = [v & 0x1000 if v is not None else None for v in val]
        check("R850 trace: CGNODE+0x50 bit 0x1000 (caller has must-align): after 0, caller set", got, [0, 0x1000])
    else:
        check("R850 trace: _FHasMustAlign at MdmFlexParams: after 1, caller 1", val, [1, 1])
    # R860: a one-pointer struct parameter stays on the stack (all builds); the scalar arrives in ebx
    got = tuple(regp(rule_proc(rule_compile("R860_structparam.cpp", *f), p)).get("n")
                for f, p in (([], "?find@@YAHUNameArg@@H@Z"), (["/DSCALAR"], "?find@@YAHPBDH@Z"),
                             (["/DENTRY"], "?find@@YAHUNameArg@@H@Z")))
    check("R860: struct n on the stack (loop use, entry use), scalar n in ebx", got, (None, "ebx", None))
    # R860/R865: ReachingOccurGenKill calls CmdInsertEnter (the entry enter-load) for each register-typed parameter
    # of a may-be-flexible function (SSR+0xb4); probe on that call
    site = {"14.00": "10b56fb0", "15.00": "510a55ae", "13.10.3077": "1077e8e7", "13.10.3052": "10782217"}[RKEY]
    got = (len(rule_trace("R860_structparam.cpp", "?find@@", site)),
           len(rule_trace("R860_structparam.cpp", "?find@@", site, "/DSCALAR")),
           len(rule_trace("R865_enterload.cpp", "?late@@", site)),
           len(rule_trace("R865_enterload.cpp", "?late@@", site, "/DSTATIC")))
    check("R860/R865 trace: entry enter-loads: struct 1, scalar 2, extern 0, static 2", got, (1, 2, 0, 2))
    # R865: info's load in the entry block (static, becomes a register parameter) or in the loop's preheader (extern)
    ext = rule_proc(rule_compile("R865_enterload.cpp"), "?late@@")
    st = rule_proc(rule_compile("R865_enterload.cpp", "/DSTATIC"), "?late@@")
    early = re.findall(r"^\s+0000[0-9a-f]\s.*\smov\s+(\w+), DWORD PTR _info\$", st, re.M)   # in the first 16 bytes
    got = (bool(re.search(r"mov\s+\w+, DWORD PTR _info\$", ext)), regp(st).get("info") or "load " + "".join(early))
    want = {"14.00": (True, "ebx"), "13.10.3077": (True, "ebx"), "13.10.3052": (True, "ebx"),
            "15.00": (True, "load ebp")}     # 15.00 unrolls the loop: ebp at the top, never a register parameter
    check("R865: extern loads info at its use; static: ebx parameter (15.00: early ebp load)", got, want[RKEY])
    # R1050: which copy forms delete `if (c)` (no test left in the function)
    asm = rule_compile("R1050_nonnull_copy.cpp")
    forms = ("assign", "memcpy", "copy", "copyk", "readf", "writer", "memcopy", "memcopyf", "memset", "memsetw",
             "memcpy4", "memset4", "memcpyv")
    got = " ".join(f for f in forms if re.search(r"\stest\s", rule_proc(asm, "?x_%s@@" % f)))
    same = "memcopy memcopyf memset memsetw memset4 memcpyv"
    want = {"14.00": same, "15.00": same, "13.10.3077": same, "13.10.3052": same}
    check("R1050: null test kept after (inline size-parameter memcpy, memset, call)", got, want[RKEY])


rules_calls_tests()


def rules_question_tests():
    """T10 (rules_xref.md, questionable items): R871 (both-arm expression), R1100 (unrolling), R900 (float locals),
    R460 (ebp base -> index), R200/R450 (memory-leaf hash), on the current C2_BUILD. Listings come from a traced
    compile on the compile server (`c2trace.py --keep`, logging only a function that does not exist)."""
    def listing(name, *flags):
        d = tempfile.mkdtemp()
        subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2trace.py"), "--keep", d,
                        os.path.join(RULES, name), "--", "/TP", "/O2"] + list(flags), capture_output=True, text=True,
                       cwd=HERE, env=dict(os.environ, C2TRACE_FUNC="?no_such_function@@"))
        p = os.path.join(d, os.path.splitext(name)[0] + ".asm")
        return open(p, errors="replace").read() if os.path.exists(p) else ""

    def looped(proc):
        """True when the function's listing has a backward jump (a loop that stayed)."""
        lab, cur, jumps = {}, None, []
        for ln in proc.splitlines():
            m = re.match(r"^(\$\w+(?:@\w+)?):", ln)
            if m:
                cur = m.group(1)
                continue
            m = re.match(r"\s+([0-9a-f]{5})\t", ln)
            if m and cur:
                lab.setdefault(cur, int(m.group(1), 16))
            m = re.match(r"\s+([0-9a-f]{5})\t.*\sj\w+\s+(?:SHORT )?(\$\w+(?:@\w+)?)", ln)
            if m:
                jumps.append((int(m.group(1), 16), m.group(2)))
        return any(t in lab and lab[t] <= o for o, t in jumps)

    # R871: i's split head piece is coloured only when the row expression is written in both arms (two uses at Color)
    kd = tempfile.mkdtemp()
    out = tool("regs", os.path.join(RULES, "R871_branchrow.cpp"), "--func", "row_", "--keep", kd, "--", "/TP", "/O2")
    parts = re.split(r"^(?=\?row_)", out, flags=re.M)
    pieces = lambda f: [(int(p), r) for blk in parts if blk.startswith("?" + f)
                        for p, r in re.findall(r"^\s+\d+\s+i\s+local\s+(-?\d+)\s+\S+\s+\S+\s+\S+\s+(\S+)", blk, re.M)]
    pc = ([(40, "eax"), (15, "spill/defer")], [(40, "eax"), (25, "ecx")])
    want = {"14.00": pc, "15.00": pc, "13.10.3077": pc, "13.10.3052": pc}
    check("R871: i's pieces (prio, reg): shared 40 eax / 15 spill, arms 40 eax / 25 ecx",
          (pieces("row_shared")[:2], pieces("row_arms")[:2]), want[RKEY])
    asm = open(os.path.join(kd, "R871_branchrow.asm"), errors="replace").read() if \
        os.path.exists(os.path.join(kd, "R871_branchrow.asm")) else ""
    sh, ar = rule_proc(asm, "?row_shared@@"), rule_proc(asm, "?row_arms@@")
    got = (bool(re.search(r"imul\s+eax, DWORD PTR _i\$", sh)), bool(re.search(r"xor\s+ecx, ecx", ar)),
           len(re.findall(r"imul\s+eax, ecx", ar)))
    want = {"14.00": (True, True, 1), "15.00": (True, True, 1),       # FgTryHeadMerge merges the arms after Color
            "13.10.3077": (True, True, 2), "13.10.3052": (True, True, 2)}  # the scheduled arms differ at the imul
    check("R871 listing: shared imul [i]; arms xor ecx,ecx, imul eax,ecx (13.10: in both arms)", got, want[RKEY])
    out = tool("il", os.path.join(RULES, "R871_branchrow.cpp"), "--func", "row_arms", "--at", "@Color@4",
               "--count", "imul", "--", "/TP", "/O2")
    m = re.search(r"@Color@4\s+(\d+)", out)
    want = {"14.00": 2, "15.00": 2, "13.10.3077": 2, "13.10.3052": 2}
    check("R871 il: two imuls of i in row_arms at Color (no CSE across the arms)", int(m.group(1)) if m else None,
          want[RKEY])
    # R1100: completely unrolled unless a call, do-while, `!=` exit test or no integer counter (all builds)
    asm = listing("R1100_unroll3.cpp")
    fs = ("u_glob", "u_param", "scale@V3", "u_dbl", "u_call", "u_sink", "u_do", "u_ptr", "u_ne", "u_end", "u_copy")
    got = " ".join(f for f in fs if looped(rule_proc(asm, "?" + f + "@")))
    kept = "u_call u_sink u_do u_ne u_end"
    want = {"14.00": kept, "15.00": kept, "13.10.3077": kept, "13.10.3052": kept}
    check("R1100: loops kept: call, call use, do-while, `!=` test, pointer end test", got, want[RKEY])
    # R900: a float local's home round trip (fstp/fld or a store across a call)
    asm = listing("R900_floatlocal.cpp")
    fs = ("fx_expr", "dx_expr", "fx_load", "dx_load", "fx_member", "fx_loadcall", "dx_loadcall", "fx_param")
    got = " ".join(f for f in fs if re.search(r"(?:fstp|mov)\s+[DQ]WORD PTR _[ks]\$", rule_proc(asm, "?" + f + "@")))
    new, old = "fx_expr fx_load fx_member fx_loadcall dx_loadcall", "fx_loadcall dx_loadcall"
    want = {"14.00": new, "15.00": new, "13.10.3077": old, "13.10.3052": old}
    check("R900: locals stored to their home (float: expression or load; 13.10 and double: only across a call)",
          got, want[RKEY])
    # R460: an ebp base with scale 1 becomes the SIB index (LoRewriteEBPOpindexed); scale 4 keeps ebp as the base
    asm = listing("R460_ebpindex.cpp")
    got = tuple((re.findall(r"DWORD PTR (\[[^\]]*ebp[^\]]*\])", rule_proc(asm, "?" + f + "@")) or [None])[0]
                for f in ("ebp_idx", "ebp_idx0", "ebp_idx4"))
    sib = ("[esi+ebp+12]", "[esi+ebp]", "[ebp+esi*4+12]")
    want = {"14.00": sib, "15.00": sib, "13.10.3077": sib, "13.10.3052": sib}
    check("R460: [esi+ebp+12], [esi+ebp], [ebp+esi*4+12]", got, want[RKEY])
    # R200/R450: memory leaf [p+0] hashes d + 8 + (h(p) << 8); a member at d != 0 is OPADD(p, d): h(p) + 2d + 0x31
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py"), "sites",
                        os.path.join(RULES, "R200_memhash.cpp"), "--func", "mh_", "--", "/TP", "/O2"],
                       capture_output=True, text=True, cwd=HERE)
    hs = re.findall(r"^pass 2 line\s+\d+ OPMUL\s+1st .*\bh([0-9a-f]{4})\n\s+2nd .*\bh([0-9a-f]{4})", r.stdout, re.M)
    new, old = [("4008", "2008"), ("0081", "0059")], [("4008", "2008"), ("007e", "0056")]
    want = {"14.00": new, "15.00": new, "13.10.3077": old, "13.10.3052": old}
    check("R200/R450: OPMUL sort hashes [p+0]/[q+0] 4008/2008; p->c, q->b 81/59 (13.10: 7e/56)", hs, want[RKEY])


rules_question_tests()


def rules_misc_tests():
    """T05-T09 (rules_xref.md, layout / x87 narrowing / scheduling / flow): R1300 (loop-head npad, AlignmentWins), R420
    (float compare narrowing, OptDoubleConstMakeSingle), R1270/R1275 (stores pinned by an unplaceable store; schedwant
    alias:X), R1276/R1280 (guard copy propagation, jump threading; icount), R310, R470, R480, R1150, R1250, R1320,
    R1310. Listings from the compile server (`c2trace.py --keep`, logging only a function that does not exist)."""
    def listing(name, *flags):
        d = tempfile.mkdtemp()
        subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2trace.py"), "--keep", d,
                        os.path.join(RULES, name), "--", "/TP", "/O2"] + list(flags), capture_output=True, text=True,
                       cwd=HERE, env=dict(os.environ, C2TRACE_FUNC="?no_such_function@@"))
        p = os.path.join(d, os.path.splitext(name)[0] + ".asm")
        return open(p, errors="replace").read() if os.path.exists(p) else ""

    def instrs(proc):
        """[(offset, length, mnemonic, text)] and labels [(index, name)] of one function's listing."""
        out, labels, lines = [], [], proc.splitlines()
        for i, l in enumerate(lines):
            m = re.match(r"^(\$\w+(?:@\w+)?):", l)
            if m:
                labels.append((len(out), m.group(1)))
            m = re.match(r"^  ([0-9a-f]{5})\t((?:[0-9a-f]{2} ?)+)(?:\t\s*(.*))?$", l)
            if m:
                n, txt, k = len(m.group(2).split()), m.group(3) or "", i + 1
                while k < len(lines) and re.match(r"^\t(?:[0-9a-f]{2} ?)+(\t.*)?$", lines[k]):
                    mm = re.match(r"^\t((?:[0-9a-f]{2} ?)+)(\t\s*(.*))?$", lines[k])
                    n += len(mm.group(1).split())
                    txt = mm.group(3) or txt
                    k += 1
                txt = re.sub(r"\s+", " ", txt.split(";")[0]).strip()
                out.append((int(m.group(1), 16), n, txt.split(" ")[0] if txt else "", txt))
        return out, labels

    def procs(asm):
        return {m.group(1): m.group(2) for m in re.finditer(r"^(\S+)\s+PROC\b[^\n]*\n(.*?)^\1\s+ENDP", asm, re.M | re.S)}

    # R1300: AlignmentWins (encode.obj; the same code in all four builds). A candidate loop head (14.00/15.00: the
    # listing's $LL/$LC labels, i.e. label symbol flag +0x43 bit 2; 13.10: every backward-jump target) at offset o
    # (mod 16) is padded iff o >= 9, or else fewer than 3 of its first instructions fit in the 16-o bytes left in the
    # line and fewer than fit in 16 bytes from an aligned head (walk stops after a branch / call / ret)
    def pad_wins(o16, lens):
        if o16 > 8:
            return True
        cum = a = f = 0
        for ln, mn in lens:
            cum += ln
            if cum > 16:
                break
            a += 1
            f += o16 + cum <= 16
            if mn.startswith("j") or mn in ("call", "ret") or cum >= 16:
                break
        return f < 3 and f < a
    rows = []
    for name, proc in procs(listing("R1300_loop_pad.cpp")).items():
        ins, labels = instrs(proc)
        for k, lab in labels:
            back = any(t.startswith("j") and x.split()[-1] == lab for _, _, t, x in ins[k:])
            cand = lab.startswith(("$LL", "$LC")) if not RKEY.startswith("13.10") else back
            if not back or not cand or k == 0 or k >= len(ins):
                continue
            pad = int(ins[k - 1][3].split()[1]) if ins[k - 1][2] == "npad" else 0
            o = ins[k][0] - pad
            pred = bool(o & 15) and pad_wins(o & 15, [(x[1], x[2]) for x in ins[k:]])
            rows.append((bool(pad), pred, (-o) & 15))
    check("R1300: AlignmentWins model = every candidate loop head's npad (>= 150 heads)",
          (len(rows) >= 150, sum(p != q for p, q, _ in rows)), (True, 0))
    check("R1300: needs of 9 to 15 are padded too (not only <= 7 / 8)", len({n for p, _, n in rows if p and n >= 9}) >= 3,
          True)

    # R420: OPCMP narrowing (OptPeeps -> OptDoubleConstMakeSingle) reads the 80-bit constant's significand as a double
    # in its +-FLT_MAX check: on 14.00/15.00 a float constant on the right narrows iff its significand < 1+0x47f/2048
    import struct
    asm = listing("R420_float_cmp_narrow.cpp")
    vals = dict(re.findall(r"^int (c\d+)\(\) \{ return get\(\) > \S+; \} // (\S+)$",
                           open(os.path.join(RULES, "R420_float_cmp_narrow.cpp")).read(), re.M))
    bad = []
    for fn, proc in procs(asm).items():
        m = re.match(r"\?(c\d+)@@", fn)
        w = re.search(r"fcomp\s+(DWORD|QWORD) PTR __real@", proc)
        if not m or m.group(1) not in vals or not w:
            continue
        bits = struct.unpack("<I", struct.pack("<f", float(vals[m.group(1)])))[0]
        narrow = RKEY.startswith("13.10") or (bits & 0x7fffff) >> 12 <= 0x47e
        if (w.group(1) == "DWORD") != narrow:
            bad.append(vals[m.group(1)])
    check("R420: every right-hand constant's width follows the significand rule (13.10: all DWORD)",
          (len(vals) >= 40, bad), (True, []))
    p = procs(asm)
    width = lambda f: (re.findall(r"(?:fld|fcomp|fmul)\s+(DWORD|QWORD) PTR __real@", p.get(f, "")) or ["-"])[0]
    got = tuple(width(f) for f in ("?l99@@YAHXZ", "?m99@@YAHPAUP@@@Z", "?m100@@YAHPAUP@@@Z", "?mul6@@YAMPAUP@@@Z"))
    want = {"14.00": ("QWORD", "DWORD", "QWORD", "QWORD"), "15.00": ("QWORD", "DWORD", "QWORD", "QWORD"),
            "13.10.3077": ("DWORD",) * 4, "13.10.3052": ("DWORD",) * 4}
    check("R420: constant on the left / member 99 / member 100 / multiply", got, want[RKEY])

    # R1270: is the res store (+24) issued right before the call (sunk) or right after the size store (pinned)?
    p = procs(listing("R1270_store_index.cpp"))
    def pinned(f):
        ins = [x[3] for x in instrs(next((v for k, v in p.items() if k.startswith("?%s@@" % f)), ""))[0]]
        k = next((i for i, t in enumerate(ins) if "+24], 15" in t), None)
        return k is not None and k > 0 and "+20]" in ins[k - 1]
    fs = ("a_plain", "a_rev", "a_const", "a_eos", "b_inplace", "b_var", "b_reread", "b_ptr")
    got = " ".join(f for f in fs if pinned(f))
    new, old = "b_inplace b_var b_reread b_ptr", "b_var b_reread b_ptr"
    check("R1270: res store pinned after the size store by a non-literal index / pointer copy",
          got, {"14.00": new, "15.00": new, "13.10.3077": old, "13.10.3052": old}[RKEY])
    # R1275: the vtable store issues before the EH state store when a member has its own inlined constructor
    p = procs(listing("R1275_member_ctor.cpp", "/EHsc"))
    def early(cls):
        ins = [x[3] for x in instrs(p.get("??0%s@@QAE@PAX@Z" % cls, ""))[0]]
        v = next((i for i, t in enumerate(ins) if "OFFSET" in t and "??_7" in t), 99)
        s = next((i for i, t in enumerate(ins) if "__$EHRec$" in t and t.startswith("mov DWORD PTR __$EHRec$")), -1)
        return v < s
    check("R1275: vtable store above the state store: WithH, WithH2 yes; Plain, WithZ (Zero(int&)), WithC (Clear()) no",
          [early(c) for c in ("WithH", "WithH2", "Plain", "WithZ", "WithC")], [True, True, False, False, False])

    # schedwant alias:X (sched.md rule 4a): the order of the pinned variant, asked of the plain one, is one alias edit
    def sched_order(src, func, *flags):
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2sched.py"), os.path.join(RULES, src), "--func",
                            func, "--all", "--", "/TP", "/O2"] + list(flags), capture_output=True, text=True, cwd=HERE).stdout
        regs = re.split(r"^-- region ", r, flags=re.M)[1:]
        best = max(regs, key=len) if regs else ""
        k = int(best.split()[0]) if best else -1
        return k, [int(x) for x in re.findall(r"^\s+cyc\s+\d+ .? lex\s+(\d+)", best, re.M)]

    def alias_wanted(src, plain, pinned_, *flags):
        k, want = sched_order(src, pinned_, *flags)
        k0, have = sched_order(src, plain, *flags)
        if k < 0 or len(want) != len(have):
            return "regions differ: %d/%d instrs" % (len(want), len(have))
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "schedwant.py"), os.path.join(RULES, src),
                            "--func", plain, "--region", str(k0), "--want-lex", ",".join(map(str, want)), "--", "/TP",
                            "/O2"] + list(flags), capture_output=True, text=True, cwd=HERE).stdout
        if VERBOSE:
            print(r[-3000:])
        return bool(re.search(r"^\s+WANTED\s+alias:\d+\s*$", r, re.M)) if want != have else "same order"
    got = (alias_wanted("R1270_store_index.cpp", "a_plain", "b_var"),
           alias_wanted("R1275_member_ctor.cpp", "??0Plain@@", "??0WithH@@", "/EHsc"))
    check("schedwant: alias:X alone gives the pinned order (R1270 b_var from a_plain, R1275 WithH from Plain)",
          got, (True, True))

    # R1276: the zero-trip guard tests the head's register (f_hlive: test edi; mov esi, edi), the lever swaps the copy
    p = procs(listing("R1276_guard_copyprop.cpp"))
    def guard(f):
        ins = [x[3] for x in instrs(p.get("?%s@@YAXH@Z" % f, ""))[0]]
        t = next((x for x in ins if x.startswith("test ")), "")
        loopvar = next((x.split()[-1] for i, x in enumerate(ins) if i > 3 and x.startswith("push ")
                        and x.split()[-1] in ("esi", "edi", "ebx")), None)     # the argument of use(p)
        return t.split()[-1] == loopvar
    check("R1276: the guard tests the loop variable: f_hlive no (head), l1_rev yes (lever)",
          (guard("f_hlive"), guard("l1_rev")), (False, True))

    # R1280: the copy keeps the join (one more instruction) on every build; icount names it structure, not flow
    p = procs(listing("R1280_jump_thread.cpp"))
    n = lambda f: len(instrs(p.get("?%s@@YAXPAUMap@@@Z" % f, ""))[0])
    check("R1280: copy forms keep the `xor; test; je` join: +1 instruction against the direct forms",
          (n("copy_ours") - n("direct_orig"), n("copy2_ours") - n("sep_orig"), n("one_orig") - n("direct_orig")), (1, 1, 0))
    js = os.path.join(tempfile.mkdtemp(), "ic.json")
    subprocess.run([sys.executable, os.path.join(HERE, "tools", "icount.py"), os.path.join(RULES, "R1280_jump_thread.cpp"),
                    "--func", "::copy_ours", "--vs-func", "::direct_orig", "--json", js, "--", "/TP", "/O2"],
                   capture_output=True, text=True, cwd=HERE)
    j = json.load(open(js)) if os.path.exists(js) else {}
    check("R1280 icount: +1, classes structure (+ structure:inline / regalloc:callee-saved)",
          (j.get("ours", 0) - j.get("orig", 0), any(c.startswith("structure") for c in j.get("classes", {}))), (1, True))

    # R310: the copy's +0 field view is made in the first operator= expansion; +12/+24 by the optimiser (13.10: all
    # elements get reader-time views first)
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py"), "keys", os.path.join(RULES, "R310_copy_elem0.cpp"),
                        "--func", "?g@@", "--", "/TP", "/O2"], capture_output=True, text=True, cwd=HERE).stdout
    first = {}
    for m in re.finditer(r"^\s+(\d+) frame \d+\s+_m\+(0|12|24)(?:\.f4)?\s+(\S*)", r, re.M):
        first.setdefault(m.group(2), (int(m.group(1)), m.group(3)))
    got = (first.get("0", (0, ""))[1].startswith("V::operator=#1"), first.get("0", (99,))[0] < first.get("12", (0,))[0])
    want = {"14.00": (True, True), "15.00": (True, True), "13.10.3077": (False, True), "13.10.3052": (False, True)}
    check("R310: element 0's view made in operator=#1 with a lower key than element 1's", got, want[RKEY])
    # R470: the double accumulator's first add sorts the accumulator first by count
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py"), "sites", os.path.join(RULES, "R470_double_acc.cpp"),
                        "--func", "?dacc@@", "--pass", "2", "--", "/TP", "/O2"], capture_output=True, text=True, cwd=HERE).stdout
    m = re.search(r"OPADD\s+1st .*SU1 n(\d+) .*\n\s+2nd .*SU1 n(\d+)", r)
    check("R470: dacc's first SU1 add: the accumulator's count is one more", (int(m.group(1)) - int(m.group(2))) if m else None, 1)
    # R480: js / jl, and the bit test's extra keys
    p = procs(listing("R480_js_jl.cpp"))
    got = tuple(next((x[2] for x in instrs(p.get("?%s@@YAHH@Z" % f, ""))[0] if x[2].startswith("j")), None)
                for f in ("sgn", "bit", "sgnl", "bitl"))
    check("R480: x >= 0 jl, bit test js, x < 0 jge, bit set jns", got, ("jl", "js", "jge", "jns"))
    keys = []
    for f in ("sgn", "bit"):
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py"), "keys", os.path.join(RULES, "R480_js_jl.cpp"),
                            "--func", "?%s@@" % f, "--all", "--", "/TP", "/O2"], capture_output=True, text=True, cwd=HERE).stdout
        m = re.search(r"\((\d+) symbols allocated before OptFuncExpr, \d+ events; replay: (\d+)/", r)
        keys.append(tuple(map(int, m.groups())) if m else None)
    check("R480: the bit test: +1 symbol before OptFuncExpr, +7 keys", (keys[1][0] - keys[0][0], keys[1][1] - keys[0][1])
          if None not in keys else keys, (1, 7))
    # R1150: bswap / rol
    p = procs(listing("R1150_bswap_rot.cpp"))
    has = lambda f, mn: any(x[2] == mn for x in instrs(next((v for k, v in p.items() if k.startswith("?%s@@" % f)), ""))[0])
    got = (has("shiftswap", "bswap"), has("maskswap", "bswap"), has("bsw", "bswap"), has("rotl8", "rol"),
           has("rotsh", "rol"), has("rotr", "ror"))
    new, old = (False, False, True, True, True, True), (False, False, True, True, False, False)
    check("R1150: shift swaps no bswap; intrinsic bswap; _rotl rol; shift rotates rol/ror (13.10: no)", got,
          {"14.00": new, "15.00": new, "13.10.3077": old, "13.10.3052": old}[RKEY])
    # R1250: two tag temporaries are two loads, one tag twice one load
    p = procs(listing("R1250_tag_temps.cpp", "/EHsc"))
    loads = lambda f: sum(1 for x in instrs(next((v for k, v in p.items() if k.startswith("?%s@@" % f)), ""))[0]
                          if re.match(r"mov e\wx, DWORD PTR (\$T|_t\$)", x[3]))
    check("R1250: tag loads: two 2, twotmp 2, twosame 2, once 1", tuple(loads(f) for f in ("two", "twotmp", "twosame", "once")),
          (2, 2, 2, 1))
    # R1320: EH state 1 after a member-calling inlined constructor, 0 (eax) without a call
    asm = listing("R1320_eh_member_states.cpp", "/EHsc")
    p = procs(asm)
    st = lambda f: re.findall(r"mov\s+DWORD PTR __\$EHRec\$\[esp\+\d+\], (\w+)", p.get("?%s@@YAXXZ" % f, ""))[:1]
    check("R1320: f1 (Arr with a folded Grow call) state 1, f2 (no call) eax (0)", (st("f1"), st("f2")), (["1"], ["eax"]))
    check("R1320: byte state stores between states that share the upper bytes (f3)",
          bool(re.search(r"mov\s+BYTE PTR __\$EHRec\$", p.get("?f3@@YAXXZ", ""))), True)
    # R1310: the catch block follows the parent's ret inside the same function
    p = procs(listing("R1310_catch_after_ret.cpp", "/EHsc"))
    body = p.get("?f@@YAXPAUV@@H@Z", "")
    k_ret, k_catch = body.find("ret\t 0"), body.find("Tidy@V@@")
    check("R1310: f's catch block (Tidy, rethrow) after its ret, in the same function", 0 < k_ret < k_catch, True)


rules_misc_tests()


def tool_portability_tests():
    """T11: --func expansion (`Class::method` matches any enclosing namespace) and the case-insensitive toolchain
    lookup (c2build.tcpath) for a lower-case unpack on a case-sensitive filesystem. Pure Python, no compile."""
    import c2trace, helpersweep
    got = [c2trace.func_pat(p) for p in ("C::m", "N::C::m", "C::C", "C::~C", "::f", "m@C")]
    check("func_pat: ?m@C@, ?m@C@N@, ??0C@, ??1C@, ?f@@, unchanged", got,
          ["?m@C@", "?m@C@N@", "??0C@", "??1C@", "?f@@", "m@C"])
    got = [c2trace.func_match("C::m", n) for n in ("?m@C@@QAEXXZ", "?m@C@N@@QAEXXZ", "?m@C@O@N@@QAEXXZ", "?m@CC@@QAEXXZ",
                                                   "?m@D@C@@QAEXXZ")]
    check("func_match C::m: C, N::C, N::O::C yes; CC, C::D no", got, [True, True, True, False, False])
    src = "namespace N { struct C { int m(); }; }\nint N::C::m() { return 1; }\nint g() { return 2; }\n"
    fn = helpersweep.body_of(src, c2trace.func_decl("C::m"))
    check("helpersweep.body_of(func_decl('C::m')) finds N::C::m's definition", (fn[0], src[fn[2]:fn[3] + 1].strip()),
          ("m", "{ return 1; }"))
    d = tempfile.mkdtemp()
    for p in ("vc/bin", "vc/include", "sdk/lib"):
        os.makedirs(os.path.join(d, p))
    open(os.path.join(d, "vc", "bin", "c2.dll"), "w").close()
    real_exists = os.path.exists

    def cs_exists(p):                 # a case-sensitive filesystem: every component must match exactly
        p = os.path.abspath(p)
        if not p.startswith(d):
            return real_exists(p)
        cur = d
        for part in os.path.relpath(p, d).split(os.sep):
            if part == "." or part not in os.listdir(cur):
                return part == "."
            cur = os.path.join(cur, part)
        return True
    os.path.exists = cs_exists
    try:
        got = [os.path.relpath(c2build.tcpath(d, *a), d) for a in (("VC/Bin/c2.dll",), ("VC", "INCLUDE"),
                                                                     ("SDK/Lib",), ("SDK/Include",))]
    finally:
        os.path.exists = real_exists
    check("tcpath on a case-sensitive lower-case unpack: vc/bin/c2.dll, vc/include, sdk/lib, missing kept as written",
          got, ["vc/bin/c2.dll", "vc/include", "sdk/lib", "sdk/Include"])
    got = os.path.relpath(c2build.tcpath(d, "VC/Bin/c2.dll"), d)      # this filesystem (macOS: case-insensitive)
    check("tcpath on this filesystem finds c2.dll", os.path.isfile(os.path.join(d, got)), True)


tool_portability_tests()


def unit_tests():
    """T14: composed units (tools/unitcompose.py, tracer.md "Composed units") on a tiny fake f3decomp pack
    (tests/data/unit/pack) and a JSON spec: the composed text, the argv rewrite, the line map, one traced compile."""
    import unitcompose
    U = os.path.join(HERE, "tests", "data", "unit")
    cand, pack = os.path.join(U, "cand.cpp"), os.path.join(U, "pack")
    text, m = unitcompose.compose(unitcompose.load_spec("f3pack:" + pack), cand)
    ls = text.splitlines()
    want = ["#include <windows.h>", "#include <string.h>", '#include "%s"' % os.path.join(pack, "h_types.h"),
            '#include "%s"' % os.path.join(pack, "h_ext.h"), "", "int g_unit_total;", '#pragma optimize("", on)']
    check("f3pack: windows.h, hoisted <string.h>, pack headers in order, unit source, pragma, candidate",
          (ls[:7], ls[7].startswith("// candidate"), "#define UNIT_UNUSED" in text), (want, True, False))
    check("f3pack: candidate line 8 (Ext(&a)) is unit line 12; 12 maps back; a dropped line has none",
          (unitcompose.unit_line(8, m), unitcompose.cand_line(12, m), unitcompose.unit_line(3, m),
           unitcompose.label(12, m), unitcompose.label(3, m)), (12, 8, None, "8", "tu:3"))
    text2, m2 = unitcompose.compose(unitcompose.load_spec(os.path.join(U, "spec.json")), cand)
    check("spec: an inlined header maps to its own lines (h_types.h:4), the candidate after it",
          (unitcompose.label(5, m2), text2.count("#include"), unitcompose.offsets(m2)["delta"]),
          ("h_types.h:4", 2, 5))
    d = tempfile.mkdtemp()
    argv = ["tool.py", "slots", cand, "--f3pack", pack, "--unit-out", os.path.join(d, "u.cpp"), "--", "/W3"]
    env, act = os.environ.pop("C2TRACE_FUNC", None), unitcompose.ACTIVE
    try:
        unitcompose.take_argv(argv, quiet=True)
        got = (argv, os.environ.get("C2TRACE_FUNC"), os.path.isfile(os.path.join(d, "u.json")))
    finally:
        os.environ.pop("C2TRACE_FUNC", None)
        if env is not None:
            os.environ["C2TRACE_FUNC"] = env
        unitcompose.ACTIVE = act
    check("--f3pack: SRC -> the unit, --func and C2TRACE_FUNC from the symbol, pack flags before the user's",
          got, (["tool.py", "slots", os.path.join(d, "u.cpp"), "--func", "Unit_Sum", "--", "/MT", "/O2", "/GS",
                 "/EHsc", "/Gy", "/W3"], "Unit_Sum", True))
    # traced: slots prints the candidate's lines (a: first referenced on candidate line 8, q on 11), the composed
    # unit is kept next to the outputs
    out = tool("slots", cand, "--f3pack", pack, "--keep", os.path.join(d, "k"))
    got = sorted(re.findall(r"^\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(_[aq]\$) (L\d+)", out, re.M))
    check("slots --f3pack: candidate lines (_a$ L8, _q$ L11), unit kept with --keep", (got, os.path.isfile(
        os.path.join(d, "k", "cand.unit.cpp"))), ([("_a$", "L8"), ("_q$", "L11")], True))


unit_tests()


def probe_modules():
    """tests/probes/*.py: probe checks kept in their own files (one per doc or topic), run in this module's namespace
    so they use check(), rule_compile(), rule_proc(), rule_trace(), RKEY and the other helpers directly."""
    import glob
    for f in sorted(glob.glob(os.path.join(HERE, "tests", "probes", "*.py"))):
        exec(compile(open(f).read(), f, "exec"), dict(globals(), __file__=f))


probe_modules()

print("%d of %d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
