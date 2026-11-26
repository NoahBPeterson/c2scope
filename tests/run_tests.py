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
sys.path.insert(0, os.path.join(HERE, "tools"))
import c2build as _c2b
# what a build cannot run (table key `lacks`: feature -> reason; 13.00.9044, the VC6 front end: docs/c2/port-1300.md);
# a test that needs one prints a skip line instead (not counted)
LACKS = dict((_c2b.get("lacks") or {}) if _c2b.table() is not None else {})
X64 = _c2b.table() is not None and _c2b.get("arch") == "x64"
X64_TESTS = re.compile(r"17|x64|ncol|alias_il")    # test functions that run on an x64 build (cl 17.00)
if X64 and __name__ == "__main__":   # _X64_FILTER: the top-level checks below are x86 (Color, RegAssign, x87, 32-bit)
    import ast
    _src = open(__file__).read()
    _body, _started = [], False
    for _n in ast.parse(_src).body:
        _txt = ast.get_source_segment(_src, _n) or ""
        if "_X64_FILTER" in _txt:
            continue
        if not _started:
            if _txt.startswith('print("regression tests'):
                _started = True
                _body.append(ast.parse('print("regression tests, x64 build: the x64 and build-independent test '
                                       'functions (the x86 top-level checks are skipped)")').body[0])
            else:
                _body.append(_n)
            continue
        if isinstance(_n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
            _body.append(_n)
        elif (isinstance(_n, ast.Expr) and isinstance(_n.value, ast.Call) and isinstance(_n.value.func, ast.Name)
              and _n.value.func.id.endswith("_tests") and X64_TESTS.search(_n.value.func.id)):
            _body.append(_n)
        elif isinstance(_n, ast.Assign) and all(isinstance(t, ast.Name) and t.id.isupper() for t in _n.targets):
            _body.append(_n)                     # module constants (paths, BUILD, expected values): no compile
        elif isinstance(_n, ast.If) and 'get("arch") == "x64"' in _txt:
            _body.append(_n)                     # an arch dispatch (loadcse17 / ltcgstub17)
        elif _txt.startswith('print("%d of %d passed"') or _txt.startswith("sys.exit(0 if all(results)"):
            _body.append(_n)
    exec(compile(ast.Module(body=_body, type_ignores=[]), __file__, "exec"), dict(globals(), __name__="__x64__"))
    sys.exit(0)   # the exec'd module exits with the result


def lacking(*feats):
    """The first of feats this build lacks (its reason), or None."""
    return next((LACKS[f] for f in feats if f in LACKS), None)


def skip(name, why):
    print("  %-58s skipped (%s)" % (name, why), flush=True)


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
if lacking("sse2"):
    skip("regs --all-classes: XMM and x87 values (fpclasses.c)", lacking("sse2"))
else:
    out = tool("regs", FP, "--all-classes", "--", "/arch:SSE2")
    fx, _, hx = out.partition("\n_h ")
    check("regs --all-classes: XMM values coloured in class 6 (4 pops)", len(re.findall(r"\bxmm\d\s.*\[class 6\]", fx)), 4)
    check("regs --all-classes: x87 values reported (Color's x87 class, FPColor on 13.10)", len(re.findall(r"\[x87", hx)) >= 5, True)

if lacking("ltcg"):
    skip('ltcg: Color in link.exe, kill sets, custom convention, regmap (7 checks)', lacking("ltcg"))
else:
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
rrows = re.findall(r"^\s+\d+\s+(\S+)\s+\S+\s+-?\d+\s+-?\d+\s+\d+\s+\d+\s+(e[a-ds][xip])\s", out, re.M)
real = dict(rrows)
# two globregs of one temp (13.00.9044: t162 twice) are one dict key; rasim names them t162@p2 / t162@p4
samerows = sorted((re.sub(r"@p\d+$", "", k), v) for k, v in pred.items()) == sorted(rrows)
check("combined what-if vs a real compile: ref:k:1:6 gives the 6 registers of eA with use(k, k, k) added (k esi, h edi)",
      (pred == real or samerows, pred.get("k"), pred.get("h"), len(rrows)), (True, "esi", "edi", 6))
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
          ("34/34", "34/34", "24/24", "10/10", "2/2", "3/3", "5/5", "46/46") if BUILD.startswith("13.00") else
          ("34/34", "34/34", "24/24", "10/10", "4/4", "4/4", "8/8", "44/44" if BUILD.startswith("13.10") else "45/45"))
    m = re.search(r"state: (\d+) places", r)
    check("rasim check --predict: the model's records never differ from c2's", m.group(1) if m else None, "0")
    m = re.search(r"record numbers .*?: (\d+) of (\d+) records .*\n.*ascending number: (\S+); enterloads at one place in "
                  r"ascending piece number: (\S+)", r)
    check("rasim check --predict: every piece's record number (LIFO free list), parent order, reload order (f3)",
          m.groups() if m else None, ("26", "26", "1/1", "2/2") if BUILD.startswith("13.00") else ("28", "28", "3/3", "4/4"))
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
    if lacking("pieces"):
        skip("rasim pieces (f4) and --chain / --predict --pieces", lacking("pieces"))
    else:
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
if lacking("regs_imm"):
    skip("regs: an immediate candidate shows its constant", lacking("regs_imm"))
else:
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
if not lacking("sse2"):
    tool("regs", FP, "--keep", kd, "--", "/arch:SSE2")
got = {}


def orig_from_listing(asm, func, path):
    """Our own code as the "original": capstone of the listing's code bytes in orig.s's 'VA mnemonic operands' form
    (what an image's disassembly gives; the listing's own text has MASM's operand order)."""
    import tsweep
    lst = tsweep.listing_instrs(asm, func)
    open(path, "w").write("".join("%08x %s %s\n" % (o, d[0], d[1]) for o, d in zip(lst.addrs, lst.decoded)))
    return path


if lacking("sse2"):
    skip("regmap: XMM rows (f) and x87 stack-position rows (h)", lacking("sse2"))
else:
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

if lacking("sse2"):
    skip("rasim need 's>t' / 's>n' in the XMM pass (fpclasses.c)", lacking("sse2"))
else:
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
    # (13.00.9044: VC6's front end emits no body for the unreferenced inline functions, so 6 functions, not 10)
    check("symkeys: the allocator replay reproduces every traced key (all functions)",
          (len(rep) >= (6 if BUILD == "13.00.9044" else 8), all(a == b for a, b in rep)), (True, True))
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
          [(1, 0, 0), (3, 0, 0), (-2, 0, 0), (1 if BUILD == "13.00.9044" else 0, 0, 0), (1, 0, 0), (1, 2, 1),
           (3, 0, 0) if BUILD.startswith("13.10") or BUILD == "13.00.9044" else (1, 0, 0)])   # 9044: VC6's front
    check("symkeys hash: walk / assign / later counts of hb",                                # end parenthesises a load
          tuple(b.get(x, 0) for x in ("walk", "assign", "later")),
          (51, 4, 6) if BUILD.startswith("13.10") or BUILD == "13.00.9044" else (65, 4, 6))
    ranks = [sorted(v) for v in hs.values() if v]
    check("symkeys hash: key mod 32 = rank among the function's hash temps (own blocks of 32, no reuse in class)",
          (len(ranks) >= 8, all(key % 32 == i % 32 for r in ranks for i, (_, key) in enumerate(r))), (True, True))
    check("symkeys hash: freed hash keys are handed out again as temporaries (temp free list)",
          all(len(tm[k] & {key for _, key in hs[k]}) > 0 for k in hs if hs[k]), True)
    if BUILD == "13.00.9044":
        # the immediate table (symkeys.md "cl 13.00.9044"): unused address immediates are freed bucket by bucket
        # (c1 symbol number & 63) and their web numbers handed out again last-freed first, so 19 typedefs in front
        # (gv3's number 301 + 19 = 320 wraps to bucket 0) swap the numbers of the tied pieces of 0 and 32
        dial = open(os.path.join(HERE, "work", "port1300", "dial9044.cpp")).read()
        got = {}
        for n in (0, 19):
            p = os.path.join(tempfile.mkdtemp(), "dial.cpp")
            open(p, "w").write("".join("typedef int d%d;\n" % i for i in range(n)) + dial)
            out = symkeys("keys", p, "--func", "r2", "--imm", "--", "/TP")
            fr = re.findall(r"^&\S+\s+(\d+)\s+(\d+)\s+\d+\s+(\d+) freed", out, re.M)
            m = re.search(r"^# freed in bucket order \(webs\): ([\d ]+);", out, re.M)
            pc = dict(re.findall(r"^   piece of (\S+)\s+web\s+(\d+)", out, re.M))
            got[n] = ([w for _, _, w in sorted(fr, key=lambda x: int(x[1]))] == (m.group(1).split() if m else None),
                      int(pc.get("0", 0)) > int(pc.get("32", 0)))
        check("symkeys 9044 --imm: address immediates freed in bucket order; 19 typedefs swap the tied pieces' numbers",
              (got[0], got[19]), ((True, True), (True, False)))


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
elif lacking("x87view"):
    skip("x87 stack after Color (x87stack.cpp: kill sets, airlocks, rounding, need stack:)", lacking("x87view"))
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


if lacking("noinline"):
    skip("realize pairs/edit on realize.cpp (11 checks)", lacking("noinline"))
else:
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
    like13 = v13 or BUILD.startswith("13.00")      # 13.00.9044 behaves as 13.10 in both (port-1300.md)
    check("realize2: a test of the stack local a in equal arms is uses:_a 2>3 for the packer (code 0 on 14.00/15.00, "
          "the memory test stays on 13.10)",
          ("slots: uses:_a:2>3" in row(("sc", "eqa"))["more"], row(("sc", "eqa"))["code"] == 0), (True, not like13))
    check("realize2: a statement reading a in equal arms on a register value: uses:_a 2>3 (code 0 on 13.10)",
          ("slots: uses:_a:2>3" in row(("sp", "eqb"))["more"], row(("sp", "eqb"))["code"] == 0), (True, like13))
    check("realize2: the statement after an if at the end of both arms: one hidden pick (ins:3), same instructions",
          ("ins:3(gone)" in row(("pt", "dup"))["more"], row(("pt", "dup"))["code"]), (True, 0))
    if lacking("symkeys"):
        skip("realize2: named keys moved (kd early, kp h4)", lacking("symkeys"))
    else:
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
          got, [((1, 0, 0), (3, 0, 0)), ((1, 1, 0), (1, 1, 0)), ((1, 1, 0), (2, 1, 0))] if BUILD.startswith("13.00") else
          [((1, 0, 0), (3, 0, 0)), ((2, 1, 0), (1, 1, 0)), ((1, 1, 0), (2, 1, 0) if v13 else (1, 0, 1))])
    # (13.00.9044: ptrlocal's base already reads p->q once, so the lever has nothing to remove)


realize2_tests()


def realize_ins_tests():
    """realize.py's ins family (docs/c2/realize.md 2.5.1): a statement before an if whose then-arm returns, written at
    the head of that arm and again after the if (headret), is head-merged after RegSimpleAssignBody on 14.00 / 15.00:
    one hidden pick, the same instructions; 13.10 keeps both copies."""
    RZ = os.path.join(HERE, "tests", "data", "realize_ins.cpp")
    fl = ["--", "/TP", "/MT", "/EHsc", "/Gy", "/O2"]
    v13 = BUILD.startswith(("13.10", "13.00"))     # 13.00.9044 keeps both copies too (port-1300.md)
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


def realize_scratch_swap_tests():
    """scratch --phase / realize --from scratch, offline: tests/data/scratchswap/ holds one traced compile of swap.cpp's
    Dlg::f1 (14.00: its picks and final IL as f1.json, the listing, the scratch --export snapshot) and orig.s, that
    listing with the second statement's two loads (`cur`, `list`) issued the other way round and their registers
    exchanged. Each load then aligns with the other value's load, which has the same register, so the pick's own
    instruction says both picks agree; every use has the other register (Mount & Blade 0x406e50, picks 6 / 7, where
    realize said 18 of 18 and regmap a swap). The temp's whole vote finds both; the pair scan seeded at the first
    wrong pick re-aligns them although no single edit does."""
    import argparse, shutil, c2scratch, realize
    D = os.path.join(HERE, "tests", "data", "scratchswap")
    fx = json.load(open(os.path.join(D, "f1.json")))
    pos = c2scratch.offsets(fx["final"])
    asm, orig = os.path.join(D, "swap.lst"), os.path.join(D, "orig.s")
    one, *_ = c2scratch.orig_regs(fx["picks"], pos, asm, fx["func"], orig)
    got, *_ = c2scratch.orig_regs(fx["picks"], pos, asm, fx["func"], orig, fx["final"])
    check("scratch --phase: swapped loads look right at the picks' own instructions (5 ecx, 6 edx)",
          (one[5][0], one[6][0]), ("ecx", "edx"))
    check("scratch --phase: the temps' uses say picks 5 / 6 want edx / ecx; the other 6 picks agree",
          ((got[5][0], got[6][0]), sum(1 for p in fx["picks"] if got[p["n"]][0] == p["reg"])), (("edx", "ecx"), 6))
    d = tempfile.mkdtemp()
    snap = os.path.join(d, "snap.json")
    shutil.copy(os.path.join(D, "snap.json"), snap)
    a = argparse.Namespace(asm_func=fx["func"], func=fx["func"], phase=orig, by="block", brief=True, no_scan=False, top=5)
    import contextlib, io
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        c2scratch.phase(a, fx["picks"], pos, asm, snap, 0, fx["final"])
        wrong = realize.scratch_wrong(snap, os.path.join(d, "snap_target.json"))
        ans = realize.scratch_scan(snap, os.path.join(d, "snap_target.json"), pool=0)
    if VERBOSE:
        print(buf.getvalue()[-3000:])
    check("realize --from scratch: 6 of 8, wrong picks 5 and 6; a pair seeded at pick 5 re-aligns all 8",
          (bool(re.search(r"^# scratch: 6 of 8 known picks", buf.getvalue(), re.M)), [w[0] for w in wrong],
           bool(re.search(r"^#\s+8 of 8 ", buf.getvalue(), re.M)) and len(ans[0]) == 2), (True, [5, 6], True))


realize_scratch_swap_tests()


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
    if lacking("link"):
        skip("bdiff: listing vs its linked image (3 functions)", lacking("link"))
    else:
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



def ncolsim_tests():
    """tools/ncolsim.py on NColor traces of cl 17.00 x64 (tests/data/ncol/*.json: `c2ncol.py --export` of the .c /
    .cpp beside them): the priority formula and the register choice of the order walk, against the trace."""
    import glob, io, contextlib, ncolsim as N
    fns = N.load(sorted(glob.glob(os.path.join(HERE, "tests", "data", "ncol", "*.json"))))
    ok = n = 0
    for f in fns:
        for c in f["cands"]:
            p = N.prio(c, f["state"])
            if p is not None:
                n += 1
                ok += p == (c["prio"] - (1 << 32) if c["prio"] & 0x80000000 else c["prio"])
    check("ncolsim: NColor priorities (x64) of the fixtures as traced", (ok, n), (293, 294))
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        N.cmd_replay(fns, 0)
    lines = b.getvalue().splitlines()
    check("ncolsim: order walk register = traced register, single-region integer candidates",
          lines[0].split(":")[1].split("single")[0].strip(), "128 of 131")
    check("ncolsim: the same for xmm candidates (banks fxmm l / s, masters ymm)",
          lines[1].split(":")[1].split("single")[0].strip(), "159 of 160")


def ncolspill_tests():
    """tools/ncolspill.py offline (docs/c2/ncolor.md 13): an export's whole / split / unallocated counts and the
    frame-access proxy for one or two listings. No compile."""
    import io, contextlib, ncolspill as S
    d = tempfile.mkdtemp()
    exp = os.path.join(d, "x.json")
    json.dump(dict(c2="17.00", src="t.cpp", functions=[dict(
        func="?f@@YAXXZ", given=["rbx"], state=dict(maxlive=12, speed=1),
        cands=[dict(name="a", prio=100, first=1, last=9,
                    regions=[dict(reg="eax", first=1, last=9, benefit="1")]),
               dict(name="b", prio=90, first=2, last=8,
                    regions=[dict(reg="rcx", first=2, last=6, benefit="1"),
                             dict(reg="rsi", first=7, last=8, benefit="1")]),
               dict(name="c", prio=0, first=3, last=4, regions=[])])]), open(exp, "w"))
    one = os.path.join(d, "one.s")
    open(one, "w").write("00000000 mov dword ptr [rbp + 0x10], eax\n"
                         "00000004 mov eax, dword ptr [rbp + 0x10]\n"
                         "00000008 lea rcx, [rbp + 0x18]\n"
                         "0000000c add qword ptr [rsp - 0x8], 1\n")
    two = os.path.join(d, "two.s")
    open(two, "w").write("00000000 push rbp\n"
                         "00000001 mov qword ptr [rsp + 0x20], rcx\n"
                         "00000005 mov rcx, qword ptr [rsp + 0x20]\n"
                         "00000009 ret\n")
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        S.main([exp, "--asm", one, "--asm", two, "--top", "4"])
    out = b.getvalue()
    check("ncolspill: an export's whole / split / unallocated candidates and the given-back registers",
          ("candidates 3: 1 whole, 1 split, 1 unallocated; 3 regions" in out,
           "given back by NColPostspill: rbx" in out), (True, True))
    check("ncolspill: a split candidate's main region and its must-save part",
          ("rcx 2..6*" in out, "rsi 7..8 stolen" in out), (True, True))
    check("ncolspill: frame slots (lea is no access; a read-modify-write is a load and a store)",
          "one.s: 4 insns, 0 push/pop, 2 frame slots, 2 loads / 2 stores" in out, True)
    tail = out.split("## frame side by side")[-1] if "## frame side by side" in out else ""
    check("ncolspill: two listings side by side (the deltas of insns, pushes, loads, stores)",
          ("insns" in tail, "loads" in tail, "stores" in tail), (True, True, True))
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        S.main([os.path.join(HERE, "tests", "data", "ncol", "c1.json")])
    check("ncolspill: the c1 fixture (8 candidates, none split, none unallocated)",
          b.getvalue().splitlines()[1] if len(b.getvalue().splitlines()) > 1 else "",
          "candidates 8: 8 whole, 0 split, 0 unallocated; 8 regions")


def xdata_tests():
    """tools/xdata.py offline (docs in the tool docstring): .pdata parsing, the fragment tiling walk,
    CHAININFO payload decoding and the ICF twin census on synthetic bytes. No image, no compile."""
    import xdata as X
    ents = [(0x100, 0x110, 0x500), (0x110, 0x112, 0x520), (0x112, 0x130, 0x540), (0x200, 0x210, 0x560)]
    check("xdata: .pdata entries parse (begin, end, unwind)", X.entries(b"".join(
        X.RT.pack(*e) for e in ents)), ents)
    frags = X.body_fragments(ents, 0x100)
    check("xdata: a /GL+LTCG body's fragments tile begin->end (one entry per function is the plain layout)",
          (len(frags), [f[0] for f in frags], X.body_fragments(ents, 0x200)[0][0]),
          (3, [0x100, 0x110, 0x112], 0x200))
    mm = bytearray(0x1000)
    u = 0x400
    mm[u] = 0x20                       # version 0, flags CHAININFO (0x4 in the high 5 bits), no codes
    mm[u + 4:u + 16] = X.RT.pack(0x300, 0x310, 0x600)
    check("xdata: a CHAININFO unwind's payload names the body it chains to",
          X.chain_target(mm, u), 0x300)
    mm[u] = 0x00                       # same bytes, flags 0
    check("xdata: a plain unwind has no chain", X.chain_target(mm, u), None)
    mm[u] = 0x21                       # version 1 + CHAININFO: the version bits do not disturb the flags
    check("xdata: the version bits do not make a chain (or break one)",
          X.chain_target(mm, u), 0x300)
    mm[u] = 0x20                       # count 3 unwind codes: the payload sits behind them, aligned
    mm[u + 2] = 3
    mm[u + 12:u + 24] = X.RT.pack(0x300, 0x310, 0x600)
    check("xdata: CountOfUnwindCodes (byte 2) moves the payload",
          X.chain_target(mm, u), 0x300)
    # ICF: two bodies with an identical 64-byte prefix are one twin; the other two are distinct
    body = bytes(range(0x40)) + b"\xcc" * 0x40
    mm2 = bytearray(0x200)
    mm2[0x000:0x080] = body
    mm2[0x080:0x100] = body                        # entry 1 = entry 0's bytes: one twin
    mm2[0x100:0x180] = bytes(0x80)
    mm2[0x180:0x200] = bytes(range(0x40, 0x80)) + bytes(0x40)
    ic = [(0, 0x80, 0), (0x80, 0x100, 0), (0x100, 0x180, 0), (0x180, 0x200, 0)]
    n, twins = X.icf_twins(mm2, ic, 4)
    check("xdata: the ICF twin census counts bodies sharing an earlier body's 64-byte prefix",
          (n, twins), (4, 1))


def callform_tests():
    """tools/callform.py (docs/c2/callform.md): the call-dispatch-form census of two listings, the
    structure-vs-registers discriminator. Offline, no compile."""
    import io, contextlib, callform as C
    d = tempfile.mkdtemp()

    def listing(name, lines):
        p = os.path.join(d, name)
        open(p, "w").write("\n".join(lines) + "\n")
        return p

    def run(a, b=None, *opts):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            C.main([a] + ([b] if b else []) + list(opts))
        return buf.getvalue()

    orig = listing("orig.s", [
        "00000000 call qword ptr [rax]",                 # slot 0: the teardown pair
        "00000005 call qword ptr [rax]",
        "0000000a call qword ptr [r8 + 0x18]",           # slot 3
        "0000000f call qword ptr [rax + 0x20]",          # slot 4
        "00000014 call qword ptr [rip -> __imp_TlsGetValue]",
        "00000019 call ?f@@YAHXZ",
        "0000001e mov eax, 1",                           # no call: not counted
    ])
    # the 7231 draft: the two teardown calls absent, everything else at the same counts
    missing = listing("missing.s", [
        "0000000a call qword ptr [rax + 0x18]",
        "0000000f call qword ptr [rax + 0x20]",
        "00000014 call qword ptr [rip]",                 # unresolved thunk name
        "00000019 call FUN_1414d7200",                   # unresolved direct name
    ])
    out = run(orig, missing)
    check("callform: a missing teardown block is one slot-0 form delta, not scattered replacements",
          ("call [reg] (slot 0)" in out, "2     0    -2" in out,
           "missing virtual-dtor element teardown" in out, "structural lead" in out),
          (True, True, True, True))
    # the same calls through other registers, with both names unresolved on the second side
    equal = listing("equal.s", [
        "00000000 call qword ptr [r8]",
        "00000005 call qword ptr [rax]",
        "0000000a call qword ptr [rax + 0x18]",
        "0000000f call qword ptr [rax + 0x20]",
        "00000014 call qword ptr [rip]",
        "00000019 call FUN_1414d7200",
    ])
    check("callform: register spelling (rax vs r8) is not a finding",
          "allocator spelling, not a finding" in run(orig, equal, "--targets"), True)
    check("callform: an equal census says the byte gap is the allocator, not structure",
          "the call census is identical in every dispatch form" in run(orig, equal), True)
    # devirtualisation: the same slot-0 calls, but two extra direct dtor calls on the second side
    dev = listing("dev.s", open(equal).read().splitlines() +
                  ["0000001e call ??1Foo@@UEAA@XZ", "00000023 call ??1Foo@@UEAA@XZ"])
    devout = run(orig, dev)
    check("callform: a devirtualised destructor shows as a direct delta naming ??1...",
          ("devirtualised destructor(s): ??1Foo@@UEAA@XZ" in devout,
           "structural lead" in devout), (True, True))
    check("callform: unresolved FUN_ vs mangled names are paired as a listing artifact",
          "pairs with FUN_1414d7200 by count: symbol-resolution artifact" in
          run(orig, dev, "--targets"), True)
    reg = os.path.join(d, "reg.json")
    json.dump({"symbols": {"?callee@@YAXXZ": "0x141507190"}}, open(reg, "w"))
    sa = listing("sa.s", ["00000000 call FUN_141507190", "00000005 call FUN_141507190",
                          "0000000a call ?z@@YAXXZ"])
    sb = listing("sb.s", ["00000000 call FUN_141507190", "00000005 call ?z@@YAXXZ"])
    check("callform: --syms resolves a FUN_ placeholder on both sides to one callee",
          "(both sides resolve to ?callee@@YAXXZ)" in run(sa, sb, "--targets", "--syms", reg), True)
    sc = listing("sc.s", ["00000000 call FUN_141507190", "00000005 call ?other@@YAXXZ"])
    se = listing("se.s", ["00000000 call ?callee@@YAXXZ", "00000005 call ?other@@YAXXZ",
                          "0000000a call ?other@@YAXXZ"])
    check("callform: --syms binds a cross-spelling pair as confirmed, not an artifact",
          "resolves to ?callee@@YAXXZ: the same callee" in run(sc, se, "--targets", "--syms", reg), True)
    dt = run(orig, dev, "--targets", "--syms", reg)
    dtor = [l for l in dt.splitlines() if "??1Foo" in l]
    check("callform: a mangled destructor is a finding, never an artifact partner",
          bool(dtor) and all("pairs with" not in l for l in dtor), True)
    plus = listing("plus.s", ["+0x0000 call qword ptr [rax]", "+0x0005 call [rip -> __imp_TlsGetValue]"])
    check("callform: +0xN listings and a census of one listing",
          ("2 calls, 2 forms" in run(plus), "call [reg] (slot 0)" in run(plus)), (True, True))


def swdiff_tests():
    """tools/swdiff.py (T27): the per-case convergence driver — the case attribution walk, the fix
    classifiers (register / shape / spill / call-shape / vtable), the shared-tail map, the vtable
    table and the region counts, on a synthetic f4check --full-diff fixture. No compile."""
    import io, contextlib, swdiff as S
    # the fixture rendered with f4check's own row format ('%s %-8s %-40s | %s'), so the test also
    # validates the real column widths; ours rows carry a dummy offset (the real ours column prints
    # mnem+ops only, '%s %s' % y[1:] of a (off, mnem, ops) row)
    rows = [
        ("!", ("+0x0000", "mov", "rax, rsp"), ("", "mov", "rbp, rsp")),
        ("!", ("+0x0003", "mov", "qword ptr [rsp + 8], rcx"), ("", "mov", "qword ptr [rbp + 8], rcx")),
        ("!", ("+0x0007", "mov", "rax, qword ptr [rcx + 8]"), ("", "mov", "qword ptr [rbp - 8], rax")),
        ("!", ("+0x0020", "mov", "rbx, rax"), ("", "mov", "r12, rax")),
        ("!", ("+0x0023", "mov", "rdx, r8"), ("", "mov", "rdx, r9")),
        (" ", ("+0x0026", "call", "FUN_1401549f0"), ("", "call", "FUN_1401549f0")),
        ("!", ("+0x002b", "je", "+0x60"), ("", "je", "+0x80")),
        ("!", ("+0x0030", "mov", "eax, dword ptr [rbx]"), ("", "mov", "eax, dword ptr [r12]")),
        (" ", ("+0x0034", "mov", "qword ptr [rsp + 0x20], rax"), ("", "mov", "qword ptr [rsp + 0x20], rax")),
        ("!", ("+0x003c", "call", "qword ptr [rax + 0x198]"), ("", "call", "qword ptr [rcx + 0x198]")),
        ("!", ("+0x0042", "jmp", "+0x90"), ("", "jmp", "+0x90")),
        (" ", ("+0x0050", "test", "rax, rax"), ("", "test", "rax, rax")),
        ("!", ("+0x0053", "je", "+0x60"), ("", "je", "+0x80")),
        (" ", ("+0x0059", "call", "FUN_1401549f0"), ("", "call", "FUN_1401549f0")),
        ("!", ("+0x005e", "jmp", "+0x60"), ("", "jmp", "+0x80")),
        ("!", ("+0x0060", "mov", "rcx, qword ptr [rsp + 0x20]"), None),
        ("!", ("+0x0065", "call", "?Release@NiRefObject@@QEAAXXZ"), None),
        ("!", ("+0x006b", "jmp", "+0x90"), None),
        ("!", None, ("", "mov", "rcx, qword ptr [rsp + 0x20]")),
        ("!", None, ("", "call", "?Release@NiRefObject@@QEAAXXZ")),
        (" ", ("+0x0090", "xor", "eax, eax"), ("", "xor", "eax, eax")),
        (" ", ("+0x0095", "ret", ""), ("", "ret", "")),
    ]
    diff = "\n".join("%s %-8s %-40s | %s" % (
        mark,
        o[0] if o else "",
        ("%s %s" % o[1:]) if o else "",
        ("%s %s" % y[1:]) if y else "") for mark, o, y in rows) + "\n"
    df = os.path.join(tempfile.mkdtemp(), "diff.txt")
    open(df, "w").write(diff)
    cf = os.path.join(os.path.dirname(df), "cases.txt")
    open(cf, "w").write("=== case +0x20\n=== case +0x50\n")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        S.main(["--diff", df, "--cases", cf])
    out = buf.getvalue()
    check("swdiff: the header names the cases, the diffs and the modal common exit",
          ("2 cases" in out, "common exit +0x90" in out), (True, True))
    check("swdiff: the case rows group by ownership with the class mix",
          ("== case +0x20:" in out, "== case +0x50:" in out, "[register]" in out), (True, True, True))
    check("swdiff: a call-through-[reg+imm] diff is a vtable-call, a stack-frame mismatch a spill",
          ("[vtable-call]" in out, "[spill]" in out), (True, True))
    check("swdiff: the shared-tail map separates the original's shared block from ours' inlined copy",
          ("+0x60..+0x6b  cases +0x20, +0x50" in out,
           "ours: NOT shared" in out), (True, True))
    check("swdiff: the vtable table groups by slot with the owning cases",
          ("[rax+0x198]" in out, "cases +0x20" in out), (True, True))
    check("swdiff: the region table counts exact instructions per 0x100 block",
          ("R0 " in out, "exact" in out), (True, True))
def odslots_tests():
    """tools/odslots.py (the /Od frame report): the allotemp log's parse on synthetic text, the
    orig side-by-side, and one live-trace compile under 15.00 (the hook fires only there)."""
    import io, contextlib, odslots as O
    # the raw line shape is c2trace.py's own printf: `E seq depth id eax ecx edx ebx ebp esi edi esp |`
    # regs in hex, spec args after `|`. allotemp on 15.00: entry eax=align, ecx=size; X eax=offset.
    log = ("# fename 1\n"
           "E 0 0 0 4 4 7b258408 1 26ebc8 1079070 4 26eb94 | 4 7b258408\n"
           "X 1 0 0 fffffffc fffffffc | fffffffc\n"
           "E 2 0 0 8 f0 7b258408 1 26ebc8 10790e4 f0 26eb94 | 8 7b258408\n"
           "X 3 0 0 ffffff08 ffffff0c | ffffff08\n"
           "E 4 0 0 1 1 7b258408 1 26ebc8 1079158 1 26eb94 | 1 7b258408\n"
           "X 5 0 0 fffffff7 fffffff7 | fffffff7\n"
           "E 6 0 0 4 4 7b258408 1 26ebc8 10791cc 4 26eb94 | 4 7b258408\n"
           "X 7 0 0 fffffff8 fffffff8 | fffffff8\n")
    a = O.parse_log(log)
    check("odslots: every E/X pair parses (entry align=eax, size=ecx; X's eax is the offset)",
          [(x["seq"], x["size"], x["align"], x["offset"]) for x in a[:2]],
          [(0, 4, 4, -4), (2, 240, 8, -248)])
    check("odslots: an entry without its exit survives (--max cut it)",
          (len(a), a[1]["offset"] is None, a[-1]["offset"]), (4, False, -8))
    d = tempfile.mkdtemp()
    omap = os.path.join(d, "orig.txt")
    open(omap, "w").write("  -424   15 refs\n  -420    4 refs\n   -8    3 refs\n")
    lp = os.path.join(d, "log.txt")
    open(lp, "w").write(log)
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        try:
            O.main(["--parse", lp, "--orig", omap])
        except SystemExit:
            pass
    out = b.getvalue()
    check("odslots: the walk-order report prints size x align per allocation",
          ("240 x 8" in out, "4 x 4" in out), (True, True))
    check("odslots: --orig matches retail offsets against ours (a shared offset is a =, "
          "a retail-only slot a <)",
          ("-424" in out and "<  (no ours)" in out, "= refs" in out), (True, True))
    if os.environ.get("C2TRACE_REMOTE") != "0":
        check("odslots live: skipped (traced compiles run local only)", True, True)
        return
    src = os.path.join(d, "odprobe.c")
    open(src, "w").write("int Sink(void*, int);\nint Probe(int n) {\n    int a = n + 1;\n"
                         "    char b = 1;\n    return a + b + Sink(&a, n);\n}\n")
    env = dict(os.environ, C2_BUILD="15.00", C2TRACE_REMOTE="0")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "odslots.py"), src,
                        "--func", "Probe"], capture_output=True, text=True, env=env, timeout=600)
    check("odslots live: the traced compile prints both automatics in walk order",
          ("2 automatics" in r.stdout, r.stdout.count("x 4") >= 1), (True, True))


def ncolsim_splitwhatif_tests():
    """ncolsim.py's `split:` whatif lever (docs/c2/ncolor.md 9): a candidate's lifetime cut into pieces
    that each get their own order-walk choice. Offline, on the fixtures."""
    import glob, io, contextlib, ncolsim as N
    fns = N.load(sorted(glob.glob(os.path.join(HERE, "tests", "data", "ncol", "c2.json"))))
    f = next(x for x in fns if x['func'] == '?f2@@YAHPEAHHINPEAUS@@@Z')
    c = next(c for c in f["cands"] if c["name"] == "x$1")
    mid = c["ranges"][0][1]
    sp = N.split_edits(f, ["split:x$1=%d" % (mid + 1)])
    check("ncolsim split whatif: the edit parses into one cut inside the lifetime",
          list(sp.keys()), ["x$1"])
    pieces = N._split_ranges(c["ranges"], [mid + 1])
    check("ncolsim split whatif: the cut ends a piece at the tuple before the cut",
          (pieces[0][1] < mid + 1 <= pieces[1][0], len(pieces)), (True, len(c["ranges"])))
    f2, np2 = N.apply_splits(f, sp, {})
    names = [x["name"] for x in f2["cands"] if x["name"].startswith("x$1#")]
    check("ncolsim split whatif: the pieces replace the candidate, pinned at the parent's priority",
          (len(names), np2.get(names[0]), all(x.get("_split") for x in f2["cands"] if x["name"] in names)),
          (len(pieces), c["prio"] - (1 << 32) if c["prio"] & 0x80000000 else c["prio"], True))
    got = N.simulate(f2, np2)
    check("ncolsim split whatif: every piece gets its own register from the model's walk",
          [got.get(k) for k in names], ["xmm7", "xmm6", "xmm0"])
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        N.cmd_whatif(fns, f["func"], ["split:x$1=%d" % (mid + 1)])
    out = b.getvalue()
    check("ncolsim split whatif: whatif prints the pieces with their chosen registers",
          ("#0" in out, "#1" in out, "#2" in out), (True, True, True))
    try:
        N.split_edits(f, ["split:x$1=1"])
        check("ncolsim split whatif: a cut outside the lifetime is refused", False, True)
    except SystemExit:
        check("ncolsim split whatif: a cut outside the lifetime is refused", True, True)
    sp2 = N.split_edits(f, ["split:x$1@blocks"])
    f3, np3 = N.apply_splits(f, sp2, {})
    nblocks = len({N.blockof(f["state"], lo) for lo, hi in c["ranges"]})
    check("ncolsim split whatif: @blocks cuts at every block boundary the candidate is live across",
          len([x for x in f3["cands"] if x["name"].startswith("x$1#")]) >= nblocks, True)


def sched17_tests():
    """tools/c2sched17.py's replay of the x64 scheduler (cl 17.00, K8 model) on traced regions
    (tests/data/sched17/*.json: `c2sched17.py --export` of tests/data/ncol's sources)."""
    import glob, json
    os.environ.setdefault("C2_BUILD", "14.00")
    import c2sched17 as S
    fns = []
    for p in sorted(glob.glob(os.path.join(HERE, "tests", "data", "sched17", "*.json"))):
        for f in json.load(open(p))["functions"]:
            for r in f["regions"]:
                for x in r["sel"]:
                    x["succ"] = [tuple(e) for e in x["succ"]]
            fns.append(f)
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        got = S.check(fns, 0)
    check("sched17: x64 regions issued in the replay model's order (static + dynamic priority, units, fusion)",
          got, (60, 61))


def ncol_levers_tests():
    """The x64 source levers (realize / rwsweep / helpersweep on NColor, docs/c2/ncolor.md 10) from exports, no compile:
    tests/data/ncol_levers/levers.json holds the traced base run of each function of levers.cpp and its variants
    (`rz_keep(V);` stores, one appearance each: work of the measurement in ncolor.md 10). What each variant moved is read
    as whatif edits (ncolsim.run_delta), and ncolsim's registers for them are compared with the traced ones; for the
    variants that realised the requested edit exactly, the registers predicted for the request itself."""
    import json, ncolsim as N
    d = json.load(open(os.path.join(HERE, "tests", "data", "ncol_levers", "levers.json")))
    fx = {f["func"]: f for f in d["functions"]}
    same = lambda p, g: all(p[k] == g.get(k, p[k]) for k in p if N.named(k))
    ok = n = rq = rqn = moved = 0
    for f in d["functions"]:
        for v in f["variants"]:
            dl = N.run_delta(f["base"], v["run"])
            if dl["blocks"]:
                continue
            got = N.regs_of(v["run"])
            n += 1
            ok += same(N.predict(f["base"], dl["edits"]), got)
            if v["verdict"] == "exact":
                rqn += 1
                rq += same(N.predict(f["base"], [v["request"]]), got)
                moved += any(got[k] != N.regs_of(f["base"]).get(k) for k in got if N.named(k))
    check("ncol levers: registers predicted from each variant's measured edits = traced", (ok, n), NCOL_LEVERS[0])
    check("ncol levers: exact realisations, registers predicted for the request = traced (of them with a register "
          "change)", (rq, rqn, moved), NCOL_LEVERS[1])
    b1, b2 = fx["swap1"]["base"], fx["swap2"]["base"]
    check("ncol levers: swap1 is a priority tie broken by the lifetime index (p before s)",
          (N.traced_prio(b1, "p") == N.traced_prio(b1, "s"), N.gap(b1, "p", "s"), N.gap(b1, "s", "p")), (True, 0, 1))
    check("ncol levers: need s=rdi in swap1 answers with the order edit before:s:p among the singles",
          ["before:s:p"] in [e for e, _g, _s in N.need(b1, {"s": "rdi"}, limit=20)], True)
    ans = N.need(b2, {"s": "rbx"})
    check("ncol levers: need s=rbx in swap2 (the T16 swap): one appearance more in block 2 first, p gives rbx up",
          (ans[0][0], ans[0][1]["s"], ans[0][1]["p"]), (["+app:s:2:1"], "rbx", "rdi"))
    check("ncol levers: in block 1 (live 11 of 11) s needs two appearances, one is not enough",
          (N.predict(b2, ["+app:s:1:1"])["s"], N.predict(b2, ["+app:s:1:2"])["s"]), ("rdi", "rbx"))
    b4 = fx["four"]["base"]
    check("ncol levers: before:A:B and index:V=K reorder a tie; live:B:+K moves every global's priority (four: 8 more "
          "live in block 1 make u's block-2 and -3 appearances count more than x's in block 1)",
          (N.predict(b1, ["before:s:p"])["s"], N.predict(b1, ["index:s=1"])["s"], N.predict(b4, [])["u"],
           N.predict(b4, ["live:1:+8"])["u"]), ("rdi", "rdi", "rbp", "rsi"))


NCOL_LEVERS = ((36, 36), (25, 25, 11))


def ncolsim_il_tests():
    """tools/ncolsim.py's trace-free inputs (docs/c2/ncolor.md 11): NColor's tuple numbering, blocks, appearances,
    ranges and zone depth computed from the IL of `c2ncol.py --export --il` (tests/data/ncol/il/*.json: c1, c2, sum)
    against the traced ones, and the priorities / register choices on them. No compile."""
    import glob, io, contextlib, ncolsim as N
    fns = N.load(sorted(glob.glob(os.path.join(HERE, "tests", "data", "ncol", "il", "*.json"))))
    with contextlib.redirect_stdout(io.StringIO()):
        c = N.cmd_ilin(fns, 0)
        pr = N.cmd_ilprio(fns)
    check("ncolsim il: tuple numbering / block of every tuple as NColDoLiveness", (c["ntup same"], c["blk same"],
          c["functions"]), (4, 4, 4))
    check("ncolsim il: appearances / ranges from the IL (of 65 candidates)", (c["apps same"], c["ranges same"]),
          (63, 62))
    check("ncolsim il: zone depth per block / live count per block (of 65; the live count is not reproduced)",
          (c["depth same"], c["live same"], c["blocks"]), (62, 50, 65))
    check("ncolsim il: priorities on the IL's inputs (live as traced) / on the traced inputs", pr, (62, 64, 65))
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        N.cmd_ilsim(fns, 0)
    out = b.getvalue()
    check("ncolsim il: replay and sim on the IL's inputs (availability as traced)",
          [ln.split(":")[1].split("single")[0].strip() for ln in out.splitlines()[1:]], ["53 of 54", "9 of 9", "61 of 63"])
    t = N.il_parse(["B 10", "I 20 mov 1 0 | k$1:1004 <- r8d=t130:1004", "R 20 0 0 13b 1010c 1004 0 0 0 1 0 0",
                    "R 21 0 0 4fb 10001 1004 0 0 40 0 30 0", "S 30 0 0 3 30 0 0 0 0 0 4 0",
                    "R 22 0 0 4fb 10001 1004 0 0 40 0 31 0", "S 31 0 0 3 31 0 0 0 0 0 4 0"])
    check("ncolsim il: a tuple's number, leaf kinds and roots; a preassigned register is not a candidate",
          (t[1]["n"], t[1]["dstk"], t[1]["srck"], t[1]["dstr"], N._regleaf("r8d=t130:1004"), N._regleaf("k$1:1004")),
          (2, [1], [1], [0x30], (None, "r8d"), ("k$1", None)))


def ncolsim_ilmodel_tests():
    """ncolsim's remaining NColor inputs from the IL (docs/c2/ncolor.md 12): live counts, availability and lifetime
    index from the IL before NColSubsumption (tests/data/ncol/ilpre: `c2ncol.py --export --il` of c1, c2, sum), the
    tied register / window, preferences, useable / anti-preference sets and the lists; the list lever `bucket:` on
    tests/data/ncol/bucket/scope.cpp; registers from a variant's own IL after equal-arms ifs and value copies
    (tests/data/ncol/eqif, copies: levers.cpp variants, gzipped exports). No compile."""
    import glob, gzip, io, json, contextlib, ncolsim as N
    fs = N.load(sorted(glob.glob(os.path.join(HERE, "tests", "data", "ncol", "ilpre", "*.json"))))
    lv = ix = av = nb = nc = na = 0
    for f in fs:
        tp, st = N.il_parse(f["il_pre"]), f["state"]
        L, I, A = N.il_live(tp), N.il_index(tp), N.il_avail(tp, st["ntup"])
        lv += sum(L[b] == st["live"][b] for b in range(len(L)))
        nb += len(L)
        ix += sum(I.get(c["sym"]) == c.get("index") for c in f["cands"])
        nc += len(f["cands"])
        av += sum([tuple(x) for x in st["avail"][str(r)]] == v for r, v in A.items())
        na += len(A)
    check("ncolsim il_pre: live counts per block / lifetime indices / integer availability lists as traced",
          (lv, nb, ix, nc, av, na), (65, 65, 65, 65, 60, 60))
    with contextlib.redirect_stdout(io.StringIO()):
        c = N.cmd_ilmodel(fs, 0)
    check("ncolsim ilmodel: order, tied register, window, preferences, useable, anti-preference (of 65 candidates)",
          tuple(c[k] for k in ("order same", "tied same", "window same", "prefs same", "useable same", "anti same")),
          (4, 65, 64, 63, 65, 64))
    b = io.StringIO()
    with contextlib.redirect_stdout(b):
        N.cmd_ilfull(fs, 0)
    check("ncolsim ilfull: replay (int, xmm) and sim with every input from the IL",
          [ln.split(":")[1].split("single")[0].strip() for ln in b.getvalue().splitlines()[1:]],
          ["52 of 54", "9 of 9", "60 of 63"])
    bf = {f["func"].split("@")[0][1:]: f for f in N.load([os.path.join(HERE, "tests", "data", "ncol", "bucket",
                                                                     "scope.json")])}
    lists = lambda f: {N.short(k): v for k, v in N.lists_of(f).items() if N.named(N.short(k))}
    check("ncolsim lists: a value held in a one-member struct or a scope object is class 6: list 1 (non-int "
          "global), the plain int is list 3", (lists(bf["boxed"])["s"], lists(bf["scoped"])["s"],
                                               lists(bf["plain"])["saved"]), (1, 1, 3))
    pr, bx = N.predict(bf["plain"], ["bucket:saved=aggr"]), N.regs_of(bf["boxed"])
    check("ncolsim whatif bucket:saved=aggr on plain = the registers of boxed (saved first: rbx, p rdi, r rsi)",
          (pr["saved"], pr["p"], pr["r"], bx["s"], bx["p"], bx["r"], N.regs_of(bf["plain"])["saved"]),
          ("rbx", "rdi", "rsi", "rbx", "rdi", "rsi", "rbp"))
    for tag, want in (("eqif", (20, 20, 0)), ("copies", (12, 12, 2))):
        d = json.loads(gzip.open(os.path.join(HERE, "tests", "data", "ncol", tag, tag + ".json.gz")).read())
        fx = {f["func"].split("@")[0][1:]: f for f in d["functions"]}
        ok = n = edits = 0
        for name, base, _v, _line in d["variants"]:
            fv, fb = fx[name], fx[base]
            got = N.regs_of(fv)
            names = [k for k in got if N.named(k) and "/" not in got[k]]
            p = N.il_predict(fv)
            n += 1
            ok += all(p.get(k) == got[k] for k in names if k in p)
            dl = N.run_delta(fb, fv)
            if not dl["blocks"]:
                pe = N.predict(fb, dl["edits"])
                edits += all(pe.get(k) == got[k] for k in names if k in pe)
        check("ncolsim il_predict: %s variants of levers.cpp with a register change, registers from the variant's "
              "IL / from the base run's measured edits" % tag, (ok, n, edits), want)


def ncolsim_ilread_tests():
    """ncolsim's NColor inputs as read since ncolor.md 12 (IL_READ, docs/c2/ncolor.md 12.4), offline: the xmm
    availability banks and each call's clobber set (c2ncol's REGSET probe) from the IL before NColSubsumption, the
    preference lists, anti-preference sets, the float flag that stops NColMdAdjustPriority, the rbp reservation and the
    load folds; fixtures `c2ncol.py --export --il` of tests/data/ncol/xmm (xmm.c, fp.cpp; xmm.json with
    C2TRACE_ILRAW=16), calls (calls.c: static callees of the same unit), flags (flags.cpp, /O2 /EHsc), gzipped."""
    import gzip, json, io, contextlib, ncolsim as N
    gz = lambda *p: json.loads(gzip.open(os.path.join(HERE, "tests", "data", "ncol", *p)).read())["functions"]
    xm, fp = gz("xmm", "xmm.json.gz"), gz("xmm", "fp.json.gz")
    ca, fl = gz("calls", "calls.json.gz"), gz("flags", "flags.json.gz")

    def lists(fs, probe=True):
        xa = na = ia = ni = 0
        for f in fs:
            tp, st = N.il_parse(f["il_pre"]), f["state"]
            r = N.il_regsets(f) if probe else None
            X = N.il_xavail(tp, st["ntup"], r)
            A = N.il_avail(tp, st["ntup"], regsets=r) if r is not None else N.il_avail(tp, st["ntup"])
            xa += sum([tuple(q) for q in st["avail"][str(k)]] == v for k, v in X.items())
            na += len(X)
            ia += sum([tuple(q) for q in st["avail"][str(k)]] == v for k, v in A.items())
            ni += len(A)
        return xa, na, ia, ni
    check("ncolsim il_xavail / il_avail with the REGSET probe: xmm banks and integer lists as traced (xmm+fp, calls)",
          [lists(xm + fp), lists(ca)], [(800, 800, 375, 375), (384, 384, 180, 180)])
    check("ncolsim the same with the Win64 volatiles for every call (no probe): same-unit callees clobber less",
          [lists(xm + fp, False), lists(ca, False)], [(772, 800, 358, 375), (192, 384, 146, 180)])
    for tag, fs, want, full, fit in (
            ("xmm+fp", xm + fp, (25, 24, 316, 315, 316, 316), ["99 of 101", "212 of 214", "309 of 315"],
             ["97 of 101", "210 of 214", "273 of 315"]),
            ("calls", ca, (12, 12, 75, 75, 75, 75), ["70 of 74", "61 of 74"], ["54 of 74", "47 of 74"]),
            ("flags", fl, (5, 5, 74, 74, 74, 74), ["67 of 70", "4 of 4", "71 of 74"],
             ["67 of 70", "4 of 4", "71 of 74"])):
        with contextlib.redirect_stdout(io.StringIO()):
            c = N.cmd_ilmodel(fs, 0, N.IL_READ)
        check("ncolsim ilmodel (IL_READ) %s: functions, order, candidates, preferences, useable, anti-preference" % tag,
              tuple(c[k] for k in ("functions", "order same", "cands", "prefs same", "useable same", "anti same")), want)
        got = []
        for use in (N.IL_USE + N.IL_MODEL + N.IL_READ, N.IL_USE + N.IL_MODEL):
            b = io.StringIO()
            with contextlib.redirect_stdout(b):
                N.cmd_ilfull(fs, 0, use)
            got.append([ln.split(":")[1].split("single")[0].strip() for ln in b.getvalue().splitlines()[1:]])
        check("ncolsim ilfull %s: replay (int, xmm) and sim, IL_READ / the fitted IL_MODEL" % tag, got, [full, fit])
    sp = lambda p: p - (1 << 32) if p & 0x80000000 else p
    out = []
    for f in fl:
        g = N.il_model_state(f, ("order", "fpflag"))
        pr, I = [sp(c["prio"]) for c in f["cands"]], list(range(len(f["cands"])))
        tp = N.il_parse(f["il"])
        moved = N.order_of(dict(f, no_adjust=False), pr, g["lists"]) != N.order_of(dict(f, no_adjust=True), pr,
                                                                                  g["lists"])
        out.append((f["func"].split("@")[0][1:], moved and N.order_of(dict(f, no_adjust=False), pr, g["lists"]) == I,
                    N.il_uses_float(tp), N.il_has_try(tp)))
    check("ncolsim NColMdAdjustPriority moves a global unless the function uses floating point (FUNC+0xe4 & 0x80), a "
          "try block or not: (function, moved as traced, float, try)", sorted(out),
          [("dtor_int", True, False, False), ("float_int", False, True, False), ("plain_int", True, False, False),
           ("try_float", False, True, True), ("try_int", True, False, True)])


def ncolsim_fold_tests():
    """ncolsim's NColor inputs read since ncolor.md 12.4 (IL_FOLD, docs/c2/ncolor.md 12.5), offline: the frame score
    and FUNC+0xe4 from c2ncol's probes (`perfunc`, `fflags`), NColFoldTooCheap's folds (il_fold_too_cheap), the
    processing-time folds by NColMdNoFoldBenefit's cost test in processing order (il_proc_folds, il_apply_folds), the
    preference-window lists as linked (il_prefwin_lists); fixtures `c2ncol.py --export --il` (C2TRACE_ILRAW=16) of
    tests/data/ncol/fold: fold.c, tests/data/tls17/fp.cpp, tests/data/ncol/flags/flags.cpp (/EHsc), tests/data/ncol/fp.cpp,
    gzipped."""
    import gzip, json, io, contextlib, ncolsim as N
    gz = lambda n: json.loads(gzip.open(os.path.join(HERE, "tests", "data", "ncol", "fold", n)).read())["functions"]
    fs = [f for n in ("fold.json.gz", "tlsfp.json.gz", "flags.json.gz", "fp.json.gz") for f in gz(n)]
    short = lambda f: f["func"].split("@")[0].lstrip("?")
    score = sum(N.il_rbp_score(N.il_parse(f["il_pre"]), "local") == f["perfunc"]["rbpscore"] for f in fs)
    hidden = sorted(short(f) for f in fs if f["fflags"] & 0x80 and not N.il_uses_float(N.il_parse(f["il_pre"])))
    check("ncolsim the frame score (unknown SYMs: OPLOCALSCALED leaves only) as read at NColMdPerFuncInit; the float "
          "flag set by a float leaf NColor's IL no longer has", (score, len(fs), hidden), (26, 26, ["addr_fp", "init_c"]))
    ft = fn = 0
    for f in fs:
        pre, post = N.il_parse(f["il_pre"]), N.il_parse(f["il"])
        truth = {a["n"] for a, b in zip(pre, post) if a["kind"] == "I" and a["op"] == "lea" and b["op"] == "op577"}
        k = 0 if f["fflags"] & 0x80 else 1
        pred = {n for _T, d, _s in N.il_fold_too_cheap(pre, N.il_inputs(f, "il_pre"), k) for n in d}
        ft += len(truth & pred)
        fn += len(truth ^ pred)
    check("ncolsim il_fold_too_cheap: the leas NColFoldTooCheap removes (found, wrong)", (ft, fn), (67, 0))
    pw = sum(
        {int(r): [tuple(w) for w in v] for r, v in f["state"]["prefwin"].items() if v} ==
        {r: v for r, v in N.il_model_state(f, N.IL_USE + N.IL_MODEL + N.IL_READ + N.IL_FOLD)["state"]["prefwin"].items()
         if v}
        for f in fs)
    check("ncolsim il_prefwin_lists: the preference-window lists as the state probe reads them", (pw, len(fs)),
          (26, 26))
    got = {}
    for tag, use in (("read", N.IL_READ), ("fold", N.IL_READ + N.IL_FOLD)):
        with contextlib.redirect_stdout(io.StringIO()):
            c = N.cmd_ilmodel(fs, 0, use)
        b = io.StringIO()
        with contextlib.redirect_stdout(b):
            N.cmd_ilfull(fs, 0, N.IL_USE + N.IL_MODEL + use)
        got[tag] = (tuple(c[k] for k in ("tied same", "window same", "prefs same", "anti same")),
                    [ln.split(":")[1].split("single")[0].strip() for ln in b.getvalue().splitlines()[1:]])
    check("ncolsim ilmodel / ilfull on the fold fixtures (381 candidates): tied, window, preferences, anti; replay "
          "(int, xmm) and sim, IL_READ / IL_READ + IL_FOLD", (got["read"], got["fold"]),
          (((381, 380, 380, 379), ["209 of 219", "154 of 156", "360 of 375"]),
           ((381, 381, 381, 381), ["212 of 219", "155 of 156", "363 of 375"])))
    ap = rg = n = 0
    for f in gz("fold.json.gz"):
        g = N.il_model_state(f, N.IL_USE + N.IL_MODEL + N.IL_READ + N.IL_FOLD)
        for c0, c1 in zip(f["cands"], g["cands"]):
            if c0.get("ranges"):
                n += 1
                ap += sorted(c0["apps"]) == sorted(c1["apps"])
                rg += [list(v) for v in c0["ranges"]] == [list(v) for v in c1["ranges"]]
    check("ncolsim il_apply_folds: fold.c's lists as NColor holds them at processing (apps, ranges of 55)", (ap, rg, n),
          (55, 54, 55))


def ncolsim_walk_tests():
    """ncolsim's order walk as read (WALK_READ, docs/c2/ncolor.md 4), offline: NColIdentifyExtraPhysAvail's joined
    windows, NColUpdatePhysregAvail's piece boundaries, NColInsertUse's extensions, the used callee-saved registers in
    NColMdGetPrefRegOrder, the strict window overlap, the preference windows as linked lists, NColDetermineCovered's
    boundary tests and NColBestCoverage, against the earlier walk; fixtures tests/data/ncol/walk (walk.c: `c2ncol.py
    --export --il --cov --dpa`, gzipped: BestCoverage's per-register scores) and the fold / xmm / calls / flags ones."""
    import gzip, json, io, contextlib, ncolsim as N
    d = os.path.join(HERE, "tests", "data", "ncol")
    gz = lambda *p: json.loads(gzip.open(os.path.join(d, *p)).read())["functions"]
    sets = (("walk", lambda: gz("walk", "walk.json.gz")),
            ("fold", lambda: [f for n in ("fold.json.gz", "tlsfp.json.gz", "flags.json.gz", "fp.json.gz")
                              for f in gz("fold", n)]),
            ("xmm", lambda: gz("xmm", "xmm.json.gz") + gz("xmm", "fp.json.gz")), ("calls", lambda: gz("calls", "calls.json.gz")),
            ("flags", lambda: gz("flags", "flags.json.gz")))
    lines = lambda b: [ln.split(":")[1].split("single")[0].strip() for ln in b.getvalue().splitlines() if " of " in ln]
    saved = N.WALK
    got = {}
    try:
        for walk in ((), N.WALK_READ):
            N.WALK = walk
            for tag, fs in sets:
                b = io.StringIO()
                with contextlib.redirect_stdout(b):
                    N.cmd_replay(fs(), 0)
                    N.cmd_sim(fs(), 0)
                    N.cmd_ilfull(fs(), 0, N.IL_USE + N.IL_MODEL + N.IL_READ + N.IL_FOLD)
                got[(tag, bool(walk))] = lines(b)
        N.WALK = N.WALK_READ
        n = ok = 0
        sx = lambda v: None if v is None or v == 0x80000000 else (v - (1 << 32) if v & 0x80000000 else v)
        for f in gz("walk", "walk.json.gz"):        # BestCoverage's first call per candidate: every register's score
            st = f["state"]
            av, pw, done = N.avail_of(st), N.pw_init(f, st), {}
            for c in f["cands"]:
                regs = c["regions"]
                if not c.get("ranges") or not regs:
                    continue
                extra = N.extra_avail(N.il_tupmap(f), c, av)
                if c.get("cov") and len(c["cov"]) >= 15:
                    wa = N.walk_args(c, st, pw)
                    a_ = wa.get("avoid") or ()
                    order = N.used_order(f)
                    order = [x for x in order if x not in a_] + [x for x in order if x in a_]
                    sc = []
                    N.best_coverage(f, c, av, c["ranges"], order, N.useable(c), sc, done=done)
                    mine = {x[0]: x[1] for x in sc}
                    for e in c["cov"][:15]:
                        n += 1
                        ok += mine.get(N.regname(e["reg"])) == sx(e["score"])
                if c.get("prefwin"):
                    N.pw_drop(pw, tuple(c["prefwin"]))
                for r in regs:
                    g = N.ALIAS.get(r["reg"])
                    if g in N.NUM and N.NUM[g] in av:
                        rr = N.region_ranges(c, r)
                        av[N.NUM[g]] = N.cut_of(f, c, av[N.NUM[g]], rr)
                        done.setdefault(c.get("sym"), []).append([g, list(rr)])
                N.restore_extra(av, extra)
    finally:
        N.WALK = saved
    check("ncolsim walk: walk.c replay, sim, ilfull (replay, sim): the earlier walk / WALK_READ",
          (got[("walk", False)], got[("walk", True)]),
          (["42 of 44", "42 of 44", "42 of 44", "42 of 44"], ["44 of 44", "44 of 44", "44 of 44", "44 of 44"]))
    check("ncolsim walk: fold fixtures (replay int / xmm, sim, ilfull) the earlier walk / WALK_READ",
          (got[("fold", False)], got[("fold", True)]),
          (["212 of 219", "155 of 156", "363 of 375", "212 of 219", "155 of 156", "363 of 375"],
           ["219 of 219", "156 of 156", "374 of 375", "219 of 219", "156 of 156", "374 of 375"]))
    check("ncolsim walk: xmm + fp.cpp, calls, flags with WALK_READ (replay, sim, ilfull)",
          (got[("xmm", True)], got[("calls", True)], got[("flags", True)]),
          (["101 of 101", "214 of 214", "314 of 315", "101 of 101", "214 of 214", "314 of 315"],
           ["74 of 74", "72 of 74", "74 of 74", "74 of 74"],
           ["70 of 70", "4 of 4", "74 of 74", "70 of 70", "4 of 4", "74 of 74"]))
    check("ncolsim best_coverage: walk.c's NColBestCoverage scores per register as c2ncol --cov read them", (ok, n),
          (59, 60))


def ncolsim_split_tests():
    """ncolsim's split regions and the rest of IL_SIM (docs/c2/ncolor.md 4.9, 12.6), offline, on our own sources
    tests/data/ncol/split (vm.c, tok.c, sortx.c, lz.c, fmt.c, hmap.c: switch loops, two switches in one loop, recursion,
    parameters copied from memory, macro-local values; `c2ncol.py --export --il --cov`, gzipped, with the kept / rest
    probes of NColSeparateProcessedRegion): NColRangesToMinTupleRegions / NColMinimizeRangeLiveness against the probes,
    the zone depth at a switch, and replay / sim / ilfull with WALK_READ + IL_FOLD against WALK_FULL + IL_SIM."""
    import gzip, json, io, contextlib, ncolsim as N
    d = os.path.join(HERE, "tests", "data", "ncol", "split")
    fs = lambda: [f for n in ("vm", "tok", "sortx", "lz", "fmt", "hmap")
                  for f in json.loads(gzip.open(os.path.join(d, n + ".json.gz")).read())["functions"]]
    lines = lambda b: [ln.split(":")[1].split("single")[0].strip() for ln in b.getvalue().splitlines() if " of " in ln]
    saved = N.WALK
    got = {}
    try:
        for tag, walk, use in (("before", N.WALK_READ, N.IL_FOLD), ("after", N.WALK_FULL, N.IL_FOLD + N.IL_SIM)):
            N.WALK = walk
            b = io.StringIO()
            with contextlib.redirect_stdout(b):
                N.cmd_replay(fs(), 0)
                N.cmd_sim(fs(), 0)
                N.cmd_ilfull(fs(), 0, N.IL_USE + N.IL_MODEL + N.IL_READ + use)
            got[tag] = lines(b)
        N.WALK = N.WALK_FULL
        nk = ok = okr = 0
        for f in fs():
            for c in f["cands"]:
                cur = [tuple(x) for x in c.get("ranges") or []]
                for s in c.get("separate") or []:
                    if "kept" in s and len(s["ranges"]) >= s["n"]:
                        N._drop_loaded_mem(f, c)
                        kept, rest = N.separate(f, c, cur, [list(w[:6]) for w in s["ranges"]])
                        nk += 1
                        ok += kept == [tuple(x) for x in s["kept"]]
                        okr += (rest or None) == ([tuple(x) for x in s.get("rest") or []] or None)
                    cur = [tuple(x) for x in s.get("rest") or []]
        zn = z0 = z1 = 0
        for f in fs():
            x0, x1 = N.il_inputs(f), N.il_inputs(dict(f, zoneswitch=True, swlive=True))
            for b in range(len(x0["depth"]) - 1):
                zn += 1
                z0 += x0["depth"][b] == N.depth(f["state"], b)
                z1 += x1["depth"][b] == N.depth(f["state"], b)
    finally:
        N.WALK = saved
    check("ncolsim split: replay, sim, ilfull (replay, sim) of tests/data/ncol/split, WALK_READ + IL_FOLD / WALK_FULL + "
          "IL_SIM", (got["before"], got["after"]),
          (["634 of 635", "620 of 635", "629 of 635", "593 of 635"], ["635 of 635", "631 of 635", "635 of 635", "635 of 635"]))
    check("ncolsim split: NColSeparateProcessedRegion's kept pieces and rest (min_regions) as probed; zone depth per "
          "block (the size bound / the switch dominance)", (ok, okr, nk, z0, z1, zn), (48, 48, 48, 641, 724, 724))


def sched17_predict_tests():
    """tools/c2sched17.py --predict offline: each region's DAG, latencies, units and static priorities built from
    its IL (tests/data/sched17/il/*.json: `c2sched17.py --il --export` of tests/data/ncol's c1 / c2 / sum and zlib's
    adler32 compress uncompr zutil inftrees; docs/c2/sched17.md 6.x) against the traced DAG, and the schedule."""
    import glob
    os.environ.setdefault("C2_BUILD", "14.00")
    import c2sched17 as S
    if not S.have_c2_1700():
        print("  sched17 predict: skipped (reads OpSchedInfoK8 from the 17.00 c2.dll, which is not set up here)")
        return
    fns = []
    for p in sorted(glob.glob(os.path.join(HERE, "tests", "data", "sched17", "il", "*.json"))):
        fns += S.load_export(p)
    c, _ = S.compare_dag(fns)
    check("sched17 predict: traced edges reproduced from the IL (+ alias facts)",
          (c["edges both"], c["edges traced"], c["edges predicted"]), (1241, 1242, 1241))
    check("sched17 predict: node latency, unit, height, static priority from the IL and OpSchedInfoK8",
          (c["lat"], c["unit"], c["height"], c["static"], c["nodes"]), (565, 565, 565, 565, 565))
    check("sched17 predict: DAG exact / issue order with the predicted DAG / with the traced DAG",
          (c["dag exact"], c["order predicted dag"], c["order traced dag"], c["regions"]), (91, 89, 89, 92))
    c, _ = S.compare_dag(fns, alias=False)
    check("sched17 predict, IL only (PAS guess): edges, static priorities, issue order",
          (c["edges both"], c["static"], c["order predicted dag"]), (1235, 557, 88))
    # the IL leaf parser: register families, a memory leaf with base / index / PAS
    r = S._c2_1700()["regs"]
    check("sched17 predict: register families (eax in rax, ah at 8, cc_zf in cc)",
          (S.reginfo(r["eax"]), S.reginfo(r["ah"]), S.reginfo(r["cc_zf"])), ((1, 32, 0), (1, 8, 8), (353, 1, 3)))
    lf = S._leaf("[rsi=a$1:1008+rdi=n$1:1008*4+-8]@22:1004", 6, 0)
    check("sched17 predict: memory leaf text", (lf["base"], lf["index"], lf["scale"], lf["pas"], lf["disp"]),
          (r["rsi"], r["rdi"], 2, 0x22, "-8"))


def sched17_atom_tests():
    """tools/c2sched17.py's Atom model (/favor:ATOM, docs/c2/sched17.md 7) offline: tests/data/sched17/atom/*.json are
    `c2sched17.py --il --export` of tests/data/ncol's c1 / c2 / sum and zlib's adler32 compress uncompr zutil
    inftrees inffast under /O2 /favor:ATOM. The replay from the traced DAG needs no DLL (ports and throughputs are in
    the export); the prediction from the IL reads OpSchedInfoAtom from the 17.00 c2.dll."""
    import glob, io, contextlib
    os.environ.setdefault("C2_BUILD", "14.00")
    import c2sched17 as S
    fns = []
    for p in sorted(glob.glob(os.path.join(HERE, "tests", "data", "sched17", "atom", "*.json"))):
        fns += S.load_export(p)
    check("sched17 atom: every exported function traced with the Atom model (MOD+0x398 bit 6)",
          sorted({f.get("model") for f in fns}), ["atom"])
    with contextlib.redirect_stdout(io.StringIO()):
        got = S.check(fns, 0)
    regs = [r for f in fns for r in f["regions"] if len(r["sel"]) >= 2]
    order = lambda r, **kw: S.simulate(r, **kw) == [s["lex"] for s in r["sel"]]
    check("sched17 atom: regions in the traced order (Atom replay / with breakable edges / K8 rules)",
          (got, sum(order(r, model="atom", breaks=True) for r in regs), sum(order(r) for r in regs)),
          ((127, 132), 132, 81))
    if not S.have_c2_1700():
        print("  sched17 atom predict: skipped (reads OpSchedInfoAtom from the 17.00 c2.dll, which is not set up here)")
        return
    c, _ = S.compare_dag(fns)
    check("sched17 atom predict: traced edges reproduced from the IL (+ alias facts)",
          (c["edges both"], c["edges traced"], c["edges predicted"]), (1836, 1840, 1836))
    check("sched17 atom predict: node latency, unit, height, static priority from the IL and OpSchedInfoAtom",
          (c["lat"], c["unit"], c["height"], c["static"], c["nodes"]), (877, 877, 877, 877, 877))
    check("sched17 atom predict: DAG exact / issue order with the predicted DAG / with the traced DAG",
          (c["dag exact"], c["order predicted dag"], c["order traced dag"], c["regions"]), (128, 132, 127, 132))
    # a 32-bit _imul3 (callbacks): port 0, throughput 1, latency 5, the multiply unit
    v = S.sel_tuple(dict(opc=0xf6, ttype=0x1004, srck=[1, 7], dstk=[1], srcw=[[0x1004, 0, 0, 0], [0x1004, 5, 0, 0]],
                         dstw=[[0x1004, 0]]))
    check("sched17 atom: _imul3 r32, r32, imm port / throughput / latency / unit",
          (S.atom_port(v), S.atom_throughput(v), S.atom_latency(v), S.atom_unit(v)), (0x10, 1, 5, 1))
    sched17_atom_break_tests()


def sched17_atom_break_tests():
    """SchmdDepIsBreakable's address-form tests (docs/c2/sched17.md 1.6) on tests/data/sched17/atom/breaks/trees.json:
    zlib's gen_codes and gen_bitlen (`c2sched17.py --il --export`, /O2 /favor:ATOM; the IL probe logs each memory
    leaf's +0x2c / +8). gen_codes region 1: `lea rax, [r8 + r10*2]` feeds `add r9w, [rax*1 + 0]`, an address with a
    symbol part (form 0x500, +0x2c = the local array next_code, not in the IL dump): not breakable; taken as
    [rax*1 + 0] without it, it would be a kind-5 fold. gen_bitlen region 18: the lea -> movzx edge split (kind 6)."""
    import c2sched17 as S
    fns = S.load_export(os.path.join(HERE, "tests", "data", "sched17", "atom", "breaks", "trees.json"))
    c, _ = S.compare_dag(fns)
    check("sched17 atom breaks: gen_codes + gen_bitlen: regions, DAG exact, order with the predicted / traced DAG",
          (c["regions"], c["dag exact"], c["order predicted dag"], c["order traced dag"]), (28, 27, 28, 28))
    fn = {f["func"]: f for f in fns}
    r = fn["gen_codes"]["regions"][1]

    def kinds(r, lex):
        return [(e["to"], e["type"], e.get("brk")) for e in S.predict_dag(r, True, "atom")[lex]["succ"]]
    plain = kinds(r, 1)
    for nd in r["nodes"]:
        nd.pop("src3", None)
        nd.pop("dst3", None)
    for s in r["sel"]:
        s.pop("srcw", None)
    check("sched17 atom breaks: gen_codes lea -> add [S + rax*1]: not breakable; without +0x2c a kind-5 fold",
          (plain, kinds(r, 1)), ([(2, 1, None), (5, 2, None)], [(2, 1, 5), (5, 2, None)]))
    check("sched17 atom breaks: gen_bitlen region 18 lea -> movzx [r10 + 2]: split, kind 6 + a plain anti edge",
          kinds(fn["gen_bitlen"]["regions"][18], 1), [(2, 1, 6), (2, 2, None)])


def sched17_alias_il_tests():
    """c2sched17's alias facts from the IL (ILAlias, docs/c2/sched17.md 6.2) offline on tests/data/sched17/il and
    atom: the compiler's logged answers (ItfPasInterf / ItfIsMemberOfPas) reproduced, and --predict with them in
    place of the logged ones (the logged-facts and PAS-guess numbers of sched17_predict_tests stay as they are)."""
    import glob
    os.environ.setdefault("C2_BUILD", "14.00")
    import c2sched17 as S
    if not S.have_c2_1700():
        print("  sched17 alias il: skipped (reads OpSchedInfoK8 from the 17.00 c2.dll, which is not set up here)")
        return
    got = []
    for d in ("il", "atom"):
        fns = []
        for p in sorted(glob.glob(os.path.join(HERE, "tests", "data", "sched17", d, "*.json"))):
            fns += S.load_export(p)
        right = [0, 0, 0, 0]
        for f in fns:
            info = S.fn_alias_info(f)
            for r in f["regions"]:
                if not r.get("nodes") or not r.get("alias"):
                    continue
                x = S.ILAlias([S.il_tuple(n) for n in sorted(r["nodes"], key=lambda n: n["lex"])], info)
                for a, b, t in r["alias"]["pas"]:
                    right[bool(S.pas_interf(a, b, x.pas)) != bool(t)] += 1
                for a, b, t in r["alias"]["memb"]:
                    right[2 + (bool(S.memb_interf(a, b, x.memb)) != bool(t))] += 1
        c, _ = S.compare_dag(fns, alias="il")
        got.append((tuple(right), (c["edges both"], c["static"], c["order predicted dag"], c["dag exact"])))
    check("sched17 alias il: answers right / wrong (pas, member) and predict (edges, static, order, DAG exact), K8",
          got[0], ((43, 6, 40, 1), (1241, 563, 89, 89)))
    check("sched17 alias il: the same on the Atom exports",
          got[1], ((84, 6, 43, 1), (1836, 875, 132, 125)))
    # the rules on a hand-made region: s->a / s->b through one pointer, q[0] through another argument, a local
    # array t[] whose address is never taken
    t = [S.il_tuple(dict(opc=0, kind=0, tflags=1, cc=0, type=0, lex=k + 1, src=[], dst=[], il=il)) for k, il in
         enumerate(["I 1 mov 1 0 | [rcx=s$1:1008+0]@20:1004 <- r8d=x$1:1004 rcx=s$1:1008",
                    "I 2 mov 1 0 | [rcx=s$1:1008+4]@21:1004 <- r8d=x$1:1004 rcx=s$1:1008",
                    "I 3 mov 1 0 | [rdx=q$1:1008+0]@22:1004 <- r9d=y$1:1004 rdx=q$1:1008",
                    "I 4 mov 1 0 | [rcx=s$1:1008+2]@23:1004 <- r8d=x$1:1004 rcx=s$1:1008"])]
    x = S.ILAlias(t, dict(esc=set(), params={"s$1", "q$1"}, pnames=set(), stored=set()))
    check("sched17 alias il: disjoint fields of one pointer / two pointer arguments / overlapping fields",
          (x.pas.get((0x20, 0x21)), x.pas.get((0x20, 0x22)), x.pas.get((0x21, 0x23)), x.pas.get((0x20, 7))),
          (False, True, True, None))


def sched17_schedwant_tests():
    """schedwant.py on the x64 scheduler (C2_BUILD=17.00 only; docs/c2/sched.md, sched17.md 6.3): our own compile of
    tests/data/sched17/schedwant17b.c is the original, schedwant17.c the same function with q[0] = y above the
    store to s->b. Auto mode finds the region through our listing, the replay is the traced order, and the one
    perturbation that gives the original's order is that statement moved back (move:2>6); with the K8 and the
    Atom model. Against its own listing every region is in the original's order."""
    if _c2b.get("arch") != "x64":
        skip("schedwant x64: a moved store found again (K8, Atom)", "x64 builds only (C2_BUILD=17.00)")
        return
    d = tempfile.mkdtemp()
    a = os.path.join(HERE, "tests", "data", "sched17", "schedwant17.c")
    b = os.path.join(HERE, "tests", "data", "sched17", "schedwant17b.c")
    sw = [sys.executable, os.path.join(HERE, "tools", "schedwant.py")]
    for model, flags in (("k8", ["/O2"]), ("atom", ["/O2", "/favor:ATOM"])):
        kb, ka = os.path.join(d, "b" + model), os.path.join(d, "a" + model)
        subprocess.run(sw + [b, "--func", "swmove", "--region", "0", "--want-lex", "1", "--keep", kb, "--"] + flags,
                       capture_output=True, text=True, cwd=HERE)
        orig = orig_from_listing(os.path.join(kb, "schedwant17b.asm"), "swmove", os.path.join(d, model + ".s"))
        r = subprocess.run(sw + [a, "--func", "swmove", "--asm-vs", orig, "--keep", ka, "--"] + flags,
                           capture_output=True, text=True, cwd=HERE).stdout
        if VERBOSE:
            print(r[-3000:])
        m = re.search(r"^\s+wanted\s+: (.*)$", r, re.M)
        check("schedwant x64 (%s): region through our listing, replay = traced, wanted, the move that gives it" % model,
              ("(x64, %s model)" % model in r, "matched through our listing" in r, "(= traced)" in r,
               m.group(1).split() if m else None, re.findall(r"^\s+WANTED\s+(\S+)", r, re.M)),
              (True, True, True, "3 1 4 5 6 2 7".split(), ["move:2>6"]))
        own = orig_from_listing(os.path.join(ka, "schedwant17.asm"), "swmove", os.path.join(d, model + "_own.s"))
        r = subprocess.run(sw + [a, "--func", "swmove", "--asm-vs", own, "--from-keep", ka],
                           capture_output=True, text=True, cwd=HERE).stdout
        check("schedwant x64 (%s): against our own listing every region is in the original's order" % model,
              bool(re.search(r"^# (\d+) regions with 2\+ instructions: \1 in the original's order, 0 differ", r, re.M)),
              True)


def symkeys17_tests():
    """symkeys on cl 17.00 x64 from an export (tests/data/symkeys17/symkeys.json: `symkeys.py export` of
    tests/data/symkeys.cpp under --c2 17.00, docs/c2/symkeys.md 10): replay without a compile, x64 spellings."""
    js = os.path.join(HERE, "tests", "data", "symkeys17", "symkeys.json")
    env = dict(os.environ, C2_BUILD="17.00")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py"), "keys", "--from", js, "--func", ""],
                       capture_output=True, text=True, cwd=HERE, env=env)
    rep = re.findall(r"replay: (\d+)/(\d+) keys reproduced", r.stdout)
    check("symkeys17: every traced key of the 10 functions reproduced by the replay",
          (len(rep), all(a == b for a, b in rep)), (10, True))
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py"), "keys", "--from", js, "--func",
                        "kparams", "--brief"], capture_output=True, text=True, cwd=HERE, env=env)
    m = re.search(r"^\?kparams@\S*\s+(.*?)\s+\[", r.stdout, re.M)
    check("symkeys17: parameters in reverse, locals by first reference; two dummy globals",
          " ".join(m.group(1).split()[1:8]) if m else "", "c:1 b:2 a:3 r:4 q:5 DummyGlobal:32 DummySafeGlobal:33")


def slots17_frames_tests():
    """x64 frame kinds (docs/c2/slots.md 6.1) from exports, no compile: tests/data/slots17/snap/*.json are `c2trace.py
    slots --all --export` of tests/data/slots17/{frames,frames2,tr}.cpp under --c2 17.00 /TP /O2 /GS. Dynamic
    alignment (locals from the aligned rbp), alloca (rbp = rsp + B, B = min(A, 0xf0)), the argument area removed by
    TryRemoveArgumentAreaFromStackFrame (homes - F): the model's decisions and the final offsets against the listing."""
    import glob, c2slots as C
    n = ok = agree = named = 0
    kinds = dict(alloca=0, dyn=0, removed=0, b_below_a=0)
    for p in sorted(glob.glob(os.path.join(HERE, "tests", "data", "slots17", "snap", "*.json"))):
        s = C.load(p)
        res = C.model_of(s).run(C.base_order(s), record=True)
        a, b, _miss = C.listing_agree(s, C.final_offsets(s, res))
        fr = s["fn"]["frame"]
        kinds["alloca"] += bool(fr.get("alloca"))
        kinds["dyn"] += bool(fr.get("dyn"))
        kinds["removed"] += fr.get("Fend") == 0 and fr["F"] != 0
        kinds["b_below_a"] += fr["B"] < fr["A"]
        n, ok, agree, named = n + 1, ok + (not C.check(s, res)), agree + a, named + b
    check("slots17: model exact on the 15 frame fixtures; listing offsets reproduced", (n, ok, agree, named),
          (15, 15, 56, 56))
    check("slots17: fixture frame kinds (alloca, dynamic alignment, argument area removed, B < A)",
          tuple(kinds.values()), (7, 6, 2, 3))
    fr = dict(A=32, F=64, pushed=8, B=32)
    check("slots17: frame_deltas: rsp frame / alloca (rbp = rsp + B) / dynamic alignment / argument area removed",
          (C.frame_deltas(fr), C.frame_deltas(dict(fr, lrsp=False, prsp=False)), C.frame_deltas(dict(fr, lrsp=False)),
           C.frame_deltas(dict(A=32, F=40, pushed=0, B=32, Fend=0))), ((32, 72), (0, 40), (0, 72), (32, 0)))


slots17_frames_tests()


def slots17_orig_tests():
    """The x64 original-side frame walk (docs/c2/slots.md 6.2), no compile: our own 17.00 compile as "the original"
    (tests/data/slots17orig/NAME.s: capstone of its listing's bytes), the perturbed source's snapshot and listing
    (NAME.json, NAME.lst) as ours. `c2slots.py search` runs under C2_BUILD=17.00 (the listing's bytes decode as x64)
    whatever the build of this run. Plus orig_slots64 on hand-written prologues."""
    import c2slots as C
    D = os.path.join(HERE, "tests", "data", "slots17orig")
    env = dict(os.environ, C2_BUILD="17.00")

    def search(n, *extra):
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2slots.py"), "search", os.path.join(D, n + ".json"),
                            "--asm", os.path.join(D, n + ".lst"), "--orig", os.path.join(D, n + ".s")] + list(extra),
                           capture_output=True, text=True, cwd=HERE, env=env)
        if VERBOSE:
            print(r.stdout[-3000:], r.stderr[-2000:])
        m = re.search(r"^# (\d+) of (\d+) elements seen in both", r.stdout, re.M)
        cols = dict((a, int(b)) for a, b in re.findall(r"^(\S+)\s+\d+\s+\d+\s+-?\d+\s+(-?\d+) \d+/\d+", r.stdout, re.M))
        top = re.search(r"^\s+\+(\d+)\s+(\d+)\s+(\S+)", r.stdout, re.M)
        return (m.groups() if m else None), cols, (top.groups() if top else None), r.stdout
    # gen.py seed 1 f46, a second read of v7 (`ext(0); g += v7;`): 8 of 16 elements moved; one edit puts all back
    m, _, top, _ = search("gen_use_f46")
    check("slots17 --search: one more use of v7 (gen.py f46): uses:v7=2 puts 16 of 16 back", (m, top),
          (("8", "16"), ("8", "16", "uses:#5=2")))
    # zlib inflate_table with a `volatile char` in front: 7 of 12 moved; the original's offsets (its own trace)
    _, cols, _, out = search("inftrees_pad")
    want = {"this$": 0, "low$1$": 4, "sym$2$": 8, "used$1$": 12, "base$1$": 16, "type$1$": 24, "bits$": 40,
            "extra$1$": 32, "work$": 48, "mask$1$": 40, "count$": 80, "offs$": 112}
    check("slots17 --orig: zlib inflate_table padded, the original's offset of all 12 elements", cols, want)
    # genframes seed 1 f76 (alloca + dynamic alignment: rbp aligned, r13, B < A) padded; gzprintf (__chkstk, va_start)
    m1, _, _, out1 = search("gf_padarr_f76")
    m2, _, _, out2 = search("gzprintf_id")
    check("slots17 --orig: alloca + dynamic alignment (r13, aligned rbp) 5/5; gzprintf (__chkstk 0x1030) 2/2",
          (m1, "frame registers r13 rbp" in out1, m2, "sub rsp 4144" in out2), (("5", "5"), True, ("2", "2"), True))

    def ins(lines):
        return [(4 * k, l.split(" ", 1)[0], l.split(" ", 1)[1] if " " in l else "") for k, l in enumerate(lines)]
    a = C.orig_slots64(ins(["mov qword ptr [rsp + 8], rbx", "push rdi", "sub rsp, 0x30", "mov dword ptr [rsp + 0x20], eax",
                            "mov dword ptr [rsp + 0x50], ecx", "add rsp, 0x30", "pop rdi", "ret"]))
    b = C.orig_slots64(ins(["push rbp", "mov eax, 0x1010", "call 0x100", "sub rsp, rax", "lea rbp, [rsp + 0x20]",
                            "mov dword ptr [rbp + 0x10], eax", "lea rsp, [rbp + 0xff0]", "pop rbp", "ret"]))
    c = C.orig_slots64(ins(["push rbp", "sub rsp, 0x80", "lea rbp, [rsp + 0x5f]", "and rbp, 0xffffffffffffffc0",
                            "mov dword ptr [rbp + 4], eax", "mov dword ptr [rsp + 0x98], ecx", "add rsp, 0x80", "pop rbp", "ret"]))
    check("slots17 orig_slots64: home save / push + sub; __chkstk + rbp = rsp + 0x20; aligned rbp",
          ([k for _, k in a[0]], a[1]["body"], dict(b[0]).get(5), b[1]["body"], dict(c[0]).get(4), dict(c[0]).get(5)),
          ([[8], [-24], [24]], 56, [-4072], 4120, [C.ALIGNED + 4], [16]))


slots17_orig_tests()


def x64_refusal_tests():
    """Tools without an x64 version refuse under --c2 17.00 with one line naming the replacement (docs/c2/port-1700.md,
    tool status); no compile runs (each exits before it)."""
    env = dict(os.environ, C2_BUILD="17.00")
    src = os.path.join(HERE, "tests", "data", "tailmerge.c")
    runs = {"regs": ["c2trace.py", "regs", src, "--func", "c1"], "scratch": ["c2trace.py", "scratch", src, "--func", "c1"],
            "regmap": ["c2trace.py", "regmap", src, "--func", "c1", "--asm-vs", "x", "--asm-func", "y"],
            "x87": ["c2x87.py", src, "--func", "c1"],
            "judge": ["judge.py", src, "--func", "c1", "--asm-vs", "x"]}
    # merges is ported to 17.00 (c2merges.py; x64_merges_tests) and no longer refuses
    # ltcgstub is ported to x64 images (ltcgstub17_tests) and no longer refuses
    want = {"regs": "c2ncol.py", "scratch": "c2ncol.py", "regmap": "c2ncol.py",
            "x87": "uses SSE", "judge": "x86-only"}
    got = {}
    for k, argv in runs.items():
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", argv[0])] + argv[1:], capture_output=True,
                           text=True, cwd=HERE, env=env)
        last = (r.stderr.strip().splitlines() or [""])[-1]
        got[k] = r.returncode != 0 and want[k] in last
    check("x64 refusals: regs, scratch, regmap, c2x87, judge name their replacement",
          sorted(k for k, v in got.items() if not v), [])


x64_refusal_tests()


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
        return J.judge_pair(ins(addr(a, 0), 0), ins(addr(b, 0x401000), 0x401000), func,
                       image=(0x400000, 0x4000000), listing=False)["verdict"]
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
    hl = J.judge_pair(ins(addr(loop, 0), 0), ins(addr(loop2, 0x401000), 0x401000), "?f@@YAHPAH@Z",
                 image=(0x400000, 0x4000000), listing=False)["hints"]
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
        return J.judge_pair(ins(addr(a, 0), 0), ins(addr(b, 0x401000), 0x401000), "?f@@YAXPAH@Z",
                       image=(0x400000, 0x4000000), known=known, listing=False)["verdict"]
    check("judge: two callees swapped between the paths: EQUIVALENT under a free pairing, MISMATCH once the "
          "project's symbol map anchors it",
          (vk(two, two_r, {}), vk(two, two_r, {"?a@@YAXXZ": 0x478f30, "?b@@YAXXZ": 0x478f80})),
          ("EQUIVALENT", "MISMATCH"))
    keep = ["push esi", "mov esi, ecx", "sub esp, 4", "lea eax, [esp] ;; lea eax, _v$[esp+8]", "push eax",
            "call ?g@@YAXPAH@Z", "add esp, 4", "mov eax, esi", "add esp, 4", "pop esi", "ret"]
    spill = ["sub esp, 8", "mov dword ptr [esp + 4], ecx", "lea eax, [esp]", "push eax", "call 0x478f30",
             "add esp, 4", "mov eax, dword ptr [esp + 4]", "add esp, 8", "ret"]
    def vf(a, b, frame):
        return J.judge_pair(ins(addr(a, 0), 0), ins(addr(b, 0x401000), 0x401000), "?f@@YAHXZ",
                       image=(0x400000, 0x4000000), frame=frame, frame_objects=True, listing=False)["verdict"]
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
ncolsim_tests()
ncolspill_tests()
callform_tests()
xdata_tests()
odslots_tests()
swdiff_tests()
ncolsim_splitwhatif_tests()
ncol_levers_tests()
sched17_tests()
sched17_predict_tests()
sched17_atom_tests()
sched17_alias_il_tests()
sched17_schedwant_tests()
ncolsim_il_tests()
ncolsim_ilmodel_tests()
ncolsim_ilread_tests()
ncolsim_fold_tests()
ncolsim_walk_tests()
ncolsim_split_tests()
symkeys17_tests()



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
    if lacking("merges"):
        skip("merges: tail-merge walks (c1, c2, c3, r1, r2: 4 checks)", lacking("merges"))
    else:
        check("merges: twins f1/f2 stop the walk at the call (call target _f2 / _f1)",
              "stop: call target _f2 / _f1" in got["c2"][1], True)
        check("merges: a forwarder to f1 merges like one callee (c3 = c1)", got["c3"][0], got["c1"][0])
        check("merges: one scratch pick per arm: every pair stops at a register, none merged",
              (got["r1"][0], len(re.findall(r"stop: register", got["r1"][1].split("-- iteration 2")[0])) >= 3), (0, True))
        check("merges: three picks per arm: the same lea register, both pairs merged", got["r2"][0], 2)
    if lacking("noinline"):
        skip("il: every call target printed by its own name (callnames.cpp)", lacking("noinline"))
    else:
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
    want = 8 if BUILD.startswith(("13.10", "13.00")) else -24       # 13.00.9044: no /GS at all (cl ignores it)
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


def loadcse17_tests():
    """loadcse.py on cl 17.00 x64 (docs/c2/loadcse.md 8): every verdict checked against the /FAsc listing. The pass
    entries (V = the last GdfInsertArtificialAssigns entry before sweep 2, S2 = the GoptLocalCopyPropagation entry
    after it), the x64 memory leaf's hash symbol, sweep 1 rejoining the alias classes (no R2 in twolevel),
    OpeqBitAssigns, and the kills a call at an arm's head, a memcpy intrinsic and a moved pointer make."""
    d = tempfile.mkdtemp()

    def rep(src, fn_):
        js = os.path.join(d, re.sub(r"\W", "_", fn_) + ".json")
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "loadcse.py"), os.path.join(HERE, "tests", "data", src),
                            "--func", fn_, "--json", js, "--", "/TP", "/MT", "/GS", "/EHsc", "/Gy", "/O2"],
                           capture_output=True, text=True, cwd=HERE)
        if VERBOSE:
            print(r.stdout[-3000:], r.stderr[-2000:])
        return json.load(open(js)) if os.path.exists(js) else {"locations": [], "rmw": []}

    def loads(j):
        return [(ld["status"], ld["why"]) for loc in j["locations"] for ld in loc["loads"]]

    j = rep("loadcse.cpp", "drain0")
    check("loadcse17: drain0: one end pointer for read and decrement: no RMW (M2), as 14.00",
          [("M2" in r["status"], r["status"].startswith("NOT FORMED")) for r in j["rmw"]], [(True, True)])
    j = rep("loadcse.cpp", "drain1")
    check("loadcse17: drain1: RMW formed through the sub-object accessor (`add [rbx+360], -8`)",
          [r["status"] for r in j["rmw"]], ["FORMED"])
    j = rep("loadcse.cpp", "::twolevel")
    check("loadcse17: twolevel: one load each (sweep 1 rejoins the arm's alias class; 14.00: R2)",
          (any(s_.startswith("RELOADED") for s_, _ in loads(j)), sum("sweep 1 rejoined" in w for _, w in loads(j)),
           [loc["final"] for loc in j["locations"]]), (False, 3, [1, 1]))
    j = rep("loadcse.cpp", "::twolevel_local")
    check("loadcse17: twolevel_local: nothing reloaded", any(s_.startswith("RELOADED") for s_, _ in loads(j)), False)
    j = rep("loadcse.cpp", "::refmember")
    check("loadcse17: refmember: x.e read through `re` in both arms (R3), 3 loads in the code",
          (sum(s_.startswith("RELOADED R3") and "through re" in w for s_, w in loads(j)),
           [loc["final"] for loc in j["locations"] if loc["location"] == "[x+8]"]), (2, [3]))
    j = rep("loadcse.cpp", "::killcall")
    check("loadcse17: killcall: reloaded after the call (R5), 2 loads in the code",
          ([s_ for s_, _ in loads(j)], [loc["final"] for loc in j["locations"]]), (["FIRST", "RELOADED R5 kill"], [2]))
    j = rep("loadcse.cpp", "::samechain")
    check("loadcse17: samechain: `or [m], ..`", [r["status"] for r in j["rmw"]], ["FORMED"])
    j = rep("loadcse17.cpp", "::bit_and_or")
    check("loadcse17: (m & ~1) | 2 is two RMWs (OpeqBitAssigns)",
          [(r["status"], "bit-assign" in r.get("code", "")) for r in j["rmw"]], [("FORMED", True)])
    j = rep("loadcse17.cpp", "::bit_loaded_ptr")
    check("loadcse17: the same through a loaded pointer is load / op / store (M6)",
          [r["status"] for r in j["rmw"]], ["NOT FORMED M6 mixed-chain"])
    j = rep("loadcse17.cpp", "::call_arm")
    check("loadcse17: reads after a call at the arm's head: R5 (the call), both locations",
          sum(s_.startswith("RELOADED R5") and "call ?h@" in w for s_, w in loads(j)), 2)
    j = rep("loadcse17.cpp", "::intrin_kill")
    check("loadcse17: a memcpy intrinsic between the reads: R5",
          [s_ for s_, w in loads(j) if "intrinsic" in w], ["RELOADED R5 kill"])
    j = rep("loadcse17.cpp", "::moved")
    check("loadcse17: `*++s` twice: the address moved (R5, not a missed CSE)",
          sorted(w.split(" (")[0] for s_, w in loads(j) if s_.startswith("RELOADED")),
          ["assignment to m", "assignment to s"])


if lacking("loadcse"):
    skip("loadcse: load reuse / RMW rules on loadcse.cpp (8 checks)", lacking("loadcse"))
elif c2build.get("arch") == "x64":
    loadcse17_tests()                   # the x86 verdicts (R2 in twolevel, `_re`) are not 17.00's
else:
    loadcse_tests()


def ltcgstub17_tests():
    """ltcgstub.py on an x64 /LTCG image (docs/c2/ltcg.md 12, cl 17.00): the facts of the callees in
    tests/data/ltcg17_b.cpp (Win64 kill sets, entry registers, a dead parameter), stubs generated from them, and the
    callers rebuilt with the stubs instead of the bodies: byte-exact, every stub's facts the original's."""
    import c2ltcg
    d = tempfile.mkdtemp()
    A, B = (os.path.join(HERE, "tests", "data", f) for f in ("ltcg17_a.cpp", "ltcg17_b.cpp"))
    script("c2ltcg.py", "asm", A, "--with", B, "--out", d)
    dll = os.path.join(d, "ltcg.dll")
    va = {re.sub(r"^\?|@@.*$", "", n): v for n, v, _ in c2ltcg.read_map(os.path.join(d, "ltcg.map"))}
    if "fdiv" not in va:
        check("ltcgstub17: the LTCG image builds", sorted(va), ["..."])
        return
    out = script("ltcgstub.py", "facts", dll, *("%x" % va[n] for n in ("fdiv", "fshl", "fx", "fxmm", "f12")))
    got = [(m.group(1), m.group(2)) for m in re.finditer(r"entry regs (\S+)\s+kill (\S+)", out)]
    check("ltcgstub17 facts: Win64 kill sets and entry registers (f12's dead parameter not read)", got,
          [("rcx,rdx", "rax,rdx,r8"), ("rcx,rdx", "rax,rcx"), ("rcx", "rax,rcx,rdx,r8,r9,r10,r11"),
           ("rcx,rdx", "xmm0,rax,rdx,r8,r9"), ("rdx", "rax,rdx")])
    protos = os.path.join(d, "protos")
    with open(protos, "w") as f:
        for n, decl in (("f12", "int f12(int unused, int b)"), ("kc", "int kc(int a, int k)"), ("kv", "int kv(int a, int k)"),
                        ("fdiv", "int fdiv(int a, int b)"), ("fshl", "int fshl(int a, int b)"), ("fx", "int fx(int a)"),
                        ("fxmm", "int fxmm(double *p, int n)")):
            f.write("%x  %s\n" % (va[n], decl))
    stubs = os.path.join(d, "stubs.cpp")
    script("ltcgstub.py", "stubs", dll, "--protos", protos, "--out", stubs, "--vary")
    res, okn = [], None
    for fn_ in ("cdiv", "cx", "cxm", "ck1"):
        out = script("ltcgstub.py", "cmp", dll, "%x" % va[fn_], A, "--with", stubs, "--func", fn_, "--quiet",
                     *(["--check-stubs", protos] if fn_ == "cdiv" else []), "--", "/TP")
        res.append("BYTE-EXACT" in out)
        if fn_ == "cdiv":
            okn = (len(re.findall(r"^  stub .*  ok$", out, re.M)), len(re.findall(r"^  stub ", out, re.M)))
    check("ltcgstub17 cmp: cdiv, cx, cxm, ck1 rebuilt against stubs are byte-exact", res, [True] * 4)
    check("ltcgstub17 cmp --check-stubs: stubs with the original's kill set, entry registers (f12, fxmm differ)",
          okn, (5, 7))


if c2build.get("arch") == "x64":
    ltcgstub17_tests()


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


if lacking("symkeys"):
    skip("sulever: SU-order decisions (2 checks)", lacking("symkeys"))
else:
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
    v13 = c2trace.c2build.name().startswith(("13.10", "13.00"))      # 13.00.9044 as 13.10: one load
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
    if lacking("schedwant"):
        skip("schedwant: replay = traced order; unalias gives the wanted order", lacking("schedwant"))
        return
    check("schedwant: the replay reproduces the traced order", "(= traced)" in r, True)
    check("schedwant: unalias of that load gives the wanted order",
          bool(re.search(r"^\s+WANTED\s+unalias:%d\s*$" % L, r, re.M)), True)


schedwant_tests()


def schedwant_break_tests():
    """schedwant auto mode and its replay on a breakable dependence (sched.md rule 8), every build: Table::take2's
    `mov esi, [eax]` issues before the `lea eax, [ecx+edx*8+4]` that feeds it. Mount & Blade 0x4ea030 (13.10): the
    replay ignored the break ("model differs from the trace"), and auto mode matched pre-schedule IL keys to the
    original (`_jcc`, the load before its break, a load folded into a cmp) and printed nothing for any function.
    Against our own listing every region is in the original's order; against it with the element store moved
    above `push eax` the region is placed through our listing and the wanted order moves that store."""
    if lacking("schedwant"):
        skip("schedwant: breakable dependence replayed, auto mode through our listing", lacking("schedwant"))
        return
    src = os.path.join(HERE, "tests", "data", "schedbreak.cpp")
    d = tempfile.mkdtemp()
    kd = os.path.join(d, "k")
    sw = [sys.executable, os.path.join(HERE, "tools", "schedwant.py"), src, "--func", "?take2@Table@"]
    r = subprocess.run(sw + ["--region", "0", "--want-lex", "1", "--keep", kd, "--", "/TP", "/O2"],
                       capture_output=True, text=True, cwd=HERE).stdout
    if VERBOSE:
        print(r[-3000:])
    m = re.search(r"^\s+traced order : (.*)$", r, re.M)
    tr = m.group(1).split() if m else []
    check("schedwant: take2's load issues before its lea (a broken dependence); the replay = traced",
          (bool(tr) and tr.index("5") < tr.index("4"), "(= traced)" in r), (True, True))
    orig = orig_from_listing(os.path.join(kd, "schedbreak.asm"), "?take2@Table@", os.path.join(d, "same.s"))
    r = subprocess.run(sw + ["--asm-vs", orig, "--from-keep", kd], capture_output=True, text=True, cwd=HERE).stdout
    check("schedwant auto: against our own code every region is in the original's order (and it says so)",
          bool(re.search(r"^# (\d+) regions with 2\+ instructions: \1 in the original's order, 0 differ", r, re.M)), True)
    ls = open(orig).read().splitlines()
    i = next(k for k, l in enumerate(ls) if l.split(None, 1)[1] == "push eax")
    j = next(k for k, l in enumerate(ls) if l.split(None, 1)[1] == "mov dword ptr [ecx + edx*8], edi")
    ls[i], ls[j] = ls[i].split()[0] + " " + ls[j].split(None, 1)[1], ls[j].split()[0] + " " + ls[i].split(None, 1)[1]
    open(os.path.join(d, "moved.s"), "w").write("\n".join(ls) + "\n")
    r = subprocess.run(sw + ["--asm-vs", os.path.join(d, "moved.s"), "--from-keep", kd], capture_output=True, text=True,
                       cwd=HERE).stdout
    if VERBOSE:
        print(r[-3000:])
    m = re.search(r"^\s+wanted\s+: (.*)$", r, re.M)
    w = m.group(1).split() if m else []
    check("schedwant auto: the moved store is found through our listing; wanted: the store (7) before push eax (9)",
          ("matched through our listing" in r, bool(w) and w.index("7") < w.index("9"), "(= traced)" in r),
          (True, True, True))


schedwant_break_tests()


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
    if lacking("symkeys"):
        skip("effectmine: one unused initialised local is one frame key", lacking("symkeys"))
    else:
        check("effectmine: one unused initialised local is one frame key, code identical",
              [("keys:frame:+1" in x["eff"].get("K", []), x["code"]["ident"]) for x in pad], [(True, 0)])
    dec = [x for x in db if x["cls"] == "decl.extvar.before" and x["ok"]]
    check("effectmine: dummy extern declarations before the function: compiled, effects recorded",
          bool(dec) and all("types" in x for x in dec), True)
    r = subprocess.run([sys.executable, tool_, "table", "--min-n", "1"], capture_output=True, text=True, cwd=HERE, env=env)
    check("effectmine table: the copy class shows R.ref+ with the instructions unchanged",
          bool(re.search(r"^copy\s+\d+\s+1\s.*\d+\(\s*\d+\)", r.stdout, re.M)) and "R.ref+" in r.stdout, True)


effectmine_tests()


def incsnap_tests():
    """tools/incsnap.py: rwsweep and effectmine compile every variant against a snapshot of the unit's include
    closure taken at the start, so an edit of the tree during a run does not reach them (Mount & Blade: stream.h
    edited during a 22-minute rwsweep run broke every later variant). tests/data/incsnap: unit.cpp includes
    "inc/a.h", which includes "../inc/b.h" and <c.h> from the /I dir sys/. The tree is copied to a temp dir; its
    headers are broken (#error) once the snapshot exists: rwsweep when it prints its variant count (before any
    compile), effectmine once its base source is written, the direct compile after the snapshot was made."""
    import incsnap, shutil
    d = tempfile.mkdtemp(prefix="incsnap_t_")
    live = os.path.join(d, "live")
    shutil.copytree(os.path.join(HERE, "tests", "data", "incsnap"), live)
    tu, sysd = os.path.join(live, "unit.cpp"), os.path.join(live, "sys")
    cl = ["/TP", "/O2", "/I" + sysd]
    far = os.path.join(HERE, "tests", "data", "incsnap", "inc")   # headers in the checkout, the unit in the temp dir:
    ftu = os.path.join(d, "far.c")                                 # the common root is "/" (each header must still be
    open(ftu, "w").write('#include "a.h"\nint f(void) { return A; }\n')   # copied, not mapped to itself)
    fs = incsnap.Snapshot(ftu, ["/I" + far], os.path.join(d, "farsnap"))
    check("incsnap: headers on another root than the unit (common root /) are copied into the snapshot",
          (len(fs.map) >= 1, all(v.startswith(os.path.join(d, "farsnap") + os.sep) for v in fs.map.values())),
          (True, True))

    def breakit():
        for h in ("inc/a.h", "inc/b.h", "sys/c.h"):
            open(os.path.join(live, h), "w").write("#error the live tree was edited during the run\n")

    def restore():
        for h in ("inc/a.h", "inc/b.h", "sys/c.h"):
            shutil.copy(os.path.join(HERE, "tests", "data", "incsnap", h), os.path.join(live, h))
    text = open(tu).read().replace('"inc/a.h"', '"%s"' % os.path.join(live, "inc", "a.h"))     # as rwsweep writes it
    sn = incsnap.Snapshot(tu, cl, os.path.join(d, "snap"), text=text)
    st = sn.text(text)
    check("incsnap: the closure (quoted, ../ relative, <> from /I), the /I dir and the unit's include point at copies",
          (sorted(os.path.relpath(f, live) for f in sn.files), sn.cl[:2], sn.cl[2] == "/I" + sn.path(sysd),
           os.path.join(d, "snap") in st and live + os.sep + "inc" not in st),
          (["inc/a.h", "inc/b.h", "sys/c.h"], ["/TP", "/O2"], True, True))
    breakit()
    p = os.path.join(d, "v.cpp")
    open(p, "w").write(st)
    out = tool("regs", p, "--func", "sum_pair", "--", *sn.cl)
    open(p, "w").write(text)
    bad = tool("regs", p, "--func", "sum_pair", "--", *cl)
    check("incsnap: with the live headers broken, the snapshot compiles and the live tree does not",
          (bool(re.search(r"^\?sum_pair@@\S*\s+\(\d+ globregs", out, re.M)), "the live tree was edited" in bad),
          (True, True))
    restore()
    r = subprocess.Popen([sys.executable, os.path.join(HERE, "tools", "rwsweep.py"), tu, "--func", "sum_pair",
                          "--families", "split,const", "--jobs", "2", "--verify", "0", "--"] + cl,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=HERE)
    lines = []
    for ln in r.stdout:
        lines.append(ln)
        if re.match(r"\d+ variables, \d+ statements", ln):        # printed before any compile
            breakit()
    r.wait()
    o = "".join(lines)
    if VERBOSE:
        print(o[-3000:])
    m = re.search(r"^(\d+) measured, (\d+) failed to compile", o, re.M)
    check("rwsweep: the tree broken right after the snapshot: baseline and every variant still compile",
          (bool(re.search(r"^baseline: \d+ instructions", o, re.M)), m and int(m.group(1)) >= 2, m and m.group(2)),
          (True, True, "0"))
    restore()
    env = dict(os.environ, EFFECTMINE_DIR=os.path.join(d, "em"), EFFECTMINE_ALLOW_LOCAL="1")   # wine is fine for a test
    em = subprocess.Popen([sys.executable, os.path.join(HERE, "tools", "effectmine.py"), "mine", tu, "--func", "sum_pair",
                           "--features", "regs", "--classes", r"^copy$", "--max-compiles", "3", "--depth", "1", "--jobs", "1", "--"] + cl,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=HERE, env=env)
    import time
    t0 = time.time()
    while time.time() - t0 < 600 and em.poll() is None and not glob_any(os.path.join(d, "em", "runs", "*", "*", "base", "base.cpp")):
        time.sleep(0.2)
    breakit()                        # the snapshot is made before the base source is written
    o = em.communicate()[0]
    if VERBOSE:
        print(o[-3000:])
    db = [json.loads(l) for l in open(os.path.join(d, "em", "effects.jsonl"))] \
        if os.path.exists(os.path.join(d, "em", "effects.jsonl")) else []
    check("effectmine: the tree broken after the snapshot: the copies compile; the database keeps the live paths",
          (bool(db) and all(x["ok"] for x in db), [x.get("err") for x in db if "edited" in (x.get("err") or "")],
           bool(db) and all(x.get("src") == tu for x in db)), (True, [], True))


def glob_any(pat):
    import glob
    return bool(glob.glob(pat))


incsnap_tests()


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
    # 9044 sets the cdecl cleanup `add esp, 4` between the call and the sum; 14.00 / 13.10 / 15.00 have the sum
    # right after the call (or a mov). The sum is the same commutative two-address op either way: `add eax, esi`.
    prev = ("call", "mov", "add") if BUILD == "13.00.9044" else ("call", "mov")
    for i, (o, mn, ops) in enumerate(rows):
        mm = re.fullmatch(r"(e\w\w), (e\w\w)", ops)
        if mn == "add" and mm and i and rows[i - 1][1] in prev:
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
    if lacking("codeshape"):
        skip("regmap: commutative two-address op in the other order", lacking("codeshape"))
    else:
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

    old13 = BUILD.startswith(("13.10", "13.00"))      # 13.00.9044: no /fp option either
    if lacking("gopt"):
        skip("icount: pass attribution (cse, alias, kill, range, dead, tail: 6 checks)", lacking("gopt"))
    else:
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
    # compiled function with an aligned local, or /arch:SSE2); 13.10: only callers with an aligned local (CGNODE 0x1000);
    # 13.00.9044: without LTCG (0x107b110c == 0) SSR+0x14 bit 0x2000 is set on every function, disabling MdmFlexParams
    hp = "?helper@@YAHPAHHHH@Z"
    got = tuple(regp(rule_proc(rule_compile("R850_mustalign.cpp", *f), hp)).get("m")
                for f in ([], ["/DBEFORE"], ["/DAFTER"], ["/DCALLER"], ["/arch:SSE2"]))
    want = {"14.00": ("ebx", None, None, None, None), "15.00": ("ebx", None, None, None, None),
            "13.10.3077": ("ebx", "ebx", "ebx", None, "ebx"), "13.10.3052": ("ebx", "ebx", "ebx", None, "ebx"),
            "13.00.9044": (None,) * 5}
    check("R850: ebx register parameter: declared / before / after / caller / SSE2", got, want[RKEY])
    hook = {"14.00": "@MdmFlexParams@4=g10d08fe0>0", "15.00": "512306d0=g512470d8>0",        # _FHasMustAlign
            "13.10.3077": "1074b268=eax>0x99>0x50", "13.10.3052": "1074c0c7=eax>0x99>0x50",  # CGNODE+0x50
            "13.00.9044": "@MdmFlexParams@4=ecx>0>0x14"}                                     # SSR+0x14 (0x2000 = no flex)
    ev = [rule_trace("R850_mustalign.cpp", "?helper@@", hook[RKEY], f) for f in ("/DAFTER", "/DCALLER")]
    val = [int(e[0].rsplit("=", 1)[1], 16) if e else None for e in ev]
    if RKEY.startswith("13.10"):
        got = [v & 0x1000 if v is not None else None for v in val]
        check("R850 trace: CGNODE+0x50 bit 0x1000 (caller has must-align): after 0, caller set", got, [0, 0x1000])
    elif RKEY == "13.00.9044":
        got = [v & 0x2000 if v is not None else None for v in val]
        check("R850 trace: SSR+0x14 bit 0x2000 at MdmFlexParams (no private convention without LTCG)", got, [0x2000, 0x2000])
    else:
        check("R850 trace: _FHasMustAlign at MdmFlexParams: after 1, caller 1", val, [1, 1])
    # R860: a one-pointer struct parameter stays on the stack (all builds); the scalar arrives in ebx (9044: stack)
    got = tuple(regp(rule_proc(rule_compile("R860_structparam.cpp", *f), p)).get("n")
                for f, p in (([], "?find@@YAHUNameArg@@H@Z"), (["/DSCALAR"], "?find@@YAHPBDH@Z"),
                             (["/DENTRY"], "?find@@YAHUNameArg@@H@Z")))
    check("R860: struct n on the stack (loop use, entry use), scalar n in ebx", got,
          (None, None, None) if RKEY == "13.00.9044" else (None, "ebx", None))
    # R860/R865: ReachingOccurGenKill calls CmdInsertEnter (the entry enter-load) for each register-typed parameter
    # of a may-be-flexible function (SSR+0xb4); probe on that call (9044: 0 without LTCG)
    site = {"14.00": "10b56fb0", "15.00": "510a55ae", "13.10.3077": "1077e8e7", "13.10.3052": "10782217",
            "13.00.9044": "1077b8df"}[RKEY]
    got = (len(rule_trace("R860_structparam.cpp", "?find@@", site)),
           len(rule_trace("R860_structparam.cpp", "?find@@", site, "/DSCALAR")),
           len(rule_trace("R865_enterload.cpp", "?late@@", site)),
           len(rule_trace("R865_enterload.cpp", "?late@@", site, "/DSTATIC")))
    check("R860/R865 trace: entry enter-loads: struct 1, scalar 2, extern 0, static 2", got,
          (0, 0, 0, 0) if RKEY == "13.00.9044" else (1, 2, 0, 2))
    # R865: info's load in the entry block (static, becomes a register parameter) or in the loop's preheader (extern)
    ext = rule_proc(rule_compile("R865_enterload.cpp"), "?late@@")
    st = rule_proc(rule_compile("R865_enterload.cpp", "/DSTATIC"), "?late@@")
    early = re.findall(r"^\s+0000[0-9a-f]\s.*\smov\s+(\w+), DWORD PTR _info\$", st, re.M)   # in the first 16 bytes
    got = (bool(re.search(r"mov\s+\w+, DWORD PTR _info\$", ext)), regp(st).get("info") or "load " + "".join(early))
    want = {"14.00": (True, "ebx"), "13.10.3077": (True, "ebx"), "13.10.3052": (True, "ebx"),
            "15.00": (True, "load ebp"),    # 15.00 unrolls the loop: ebp at the top, never a register parameter
            "13.00.9044": (True, "load ")}  # 9044: static is like extern without LTCG (loaded in the preheader)
    check("R865: extern loads info at its use; static: ebx parameter (15.00: early ebp load)", got, want[RKEY])
    # R1050: which copy forms delete `if (c)` (no test left in the function)
    asm = rule_compile("R1050_nonnull_copy.cpp")
    forms = ("assign", "memcpy", "copy", "copyk", "readf", "writer", "memcopy", "memcopyf", "memset", "memsetw",
             "memcpy4", "memset4", "memcpyv")
    got = " ".join(f for f in forms if re.search(r"\stest\s", rule_proc(asm, "?x_%s@@" % f)))
    same = "memcopy memcopyf memset memsetw memset4 memcpyv"
    want = {"14.00": same, "15.00": same, "13.10.3077": same, "13.10.3052": same, "13.00.9044": same}
    check("R1050: null test kept after (inline size-parameter memcpy, memset, call)", got, want[RKEY])


if lacking("rules"):
    skip("rules_calls_tests: rule probes (tests/data/rules)", lacking("rules"))
else:
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
    want = {"14.00": pc, "15.00": pc, "13.10.3077": pc, "13.10.3052": pc, "13.00.9044": pc}
    check("R871: i's pieces (prio, reg): shared 40 eax / 15 spill, arms 40 eax / 25 ecx",
          (pieces("row_shared")[:2], pieces("row_arms")[:2]), want[RKEY])
    asm = open(os.path.join(kd, "R871_branchrow.asm"), errors="replace").read() if \
        os.path.exists(os.path.join(kd, "R871_branchrow.asm")) else ""
    sh, ar = rule_proc(asm, "?row_shared@@"), rule_proc(asm, "?row_arms@@")
    got = (bool(re.search(r"imul\s+eax, DWORD PTR _i\$", sh)), bool(re.search(r"xor\s+ecx, ecx", ar)),
           len(re.findall(r"imul\s+eax, ecx", ar)))
    want = {"14.00": (True, True, 1), "15.00": (True, True, 1),       # FgTryHeadMerge merges the arms after Color
            "13.10.3077": (True, True, 2), "13.10.3052": (True, True, 2),  # the scheduled arms differ at the imul
            "13.00.9044": (True, True, 2)}
    check("R871 listing: shared imul [i]; arms xor ecx,ecx, imul eax,ecx (13.10: in both arms)", got, want[RKEY])
    out = tool("il", os.path.join(RULES, "R871_branchrow.cpp"), "--func", "row_arms", "--at", "@Color@4",
               "--count", "imul", "--", "/TP", "/O2")
    m = re.search(r"@Color@4\s+(\d+)", out)
    want = {"14.00": 2, "15.00": 2, "13.10.3077": 2, "13.10.3052": 2, "13.00.9044": 2}
    check("R871 il: two imuls of i in row_arms at Color (no CSE across the arms)", int(m.group(1)) if m else None,
          want[RKEY])
    # R1100: completely unrolled unless a call, do-while, `!=` exit test or no integer counter (9044: none unrolled)
    asm = listing("R1100_unroll3.cpp")
    fs = ("u_glob", "u_param", "scale@V3", "u_dbl", "u_call", "u_sink", "u_do", "u_ptr", "u_ne", "u_end", "u_copy")
    got = " ".join(f for f in fs if looped(rule_proc(asm, "?" + f + "@")))
    kept = "u_call u_sink u_do u_ne u_end"
    want = {"14.00": kept, "15.00": kept, "13.10.3077": kept, "13.10.3052": kept, "13.00.9044": " ".join(fs)}
    check("R1100: loops kept: call, call use, do-while, `!=` test, pointer end test", got, want[RKEY])
    # R900: a float local's home round trip (fstp/fld or a store across a call)
    asm = listing("R900_floatlocal.cpp")
    fs = ("fx_expr", "dx_expr", "fx_load", "dx_load", "fx_member", "fx_loadcall", "dx_loadcall", "fx_param")
    got = " ".join(f for f in fs if re.search(r"(?:fstp|mov)\s+[DQ]WORD PTR _[ks]\$", rule_proc(asm, "?" + f + "@")))
    new, old = "fx_expr fx_load fx_member fx_loadcall dx_loadcall", "fx_loadcall dx_loadcall"
    want = {"14.00": new, "15.00": new, "13.10.3077": old, "13.10.3052": old, "13.00.9044": old}
    check("R900: locals stored to their home (float: expression or load; 13.10 and double: only across a call)",
          got, want[RKEY])
    # R460: an ebp base with scale 1 becomes the SIB index (LoRewriteEBPOpindexed); scale 4 keeps ebp as the base
    asm = listing("R460_ebpindex.cpp")
    got = tuple((re.findall(r"DWORD PTR (\[[^\]]*ebp[^\]]*\])", rule_proc(asm, "?" + f + "@")) or [None])[0]
                for f in ("ebp_idx", "ebp_idx0", "ebp_idx4"))
    sib = ("[esi+ebp+12]", "[esi+ebp]", "[ebp+esi*4+12]")
    want = {"14.00": sib, "15.00": sib, "13.10.3077": sib, "13.10.3052": sib, "13.00.9044": sib}
    check("R460: [esi+ebp+12], [esi+ebp], [ebp+esi*4+12]", got, want[RKEY])
    # R200/R450: memory leaf [p+0] hashes d + (OPLOAD - OPILLEGAL) + (h(p) << 8); a member at d != 0 is OPADD(p, d)
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py"), "sites",
                        os.path.join(RULES, "R200_memhash.cpp"), "--func", "mh_", "--", "/TP", "/O2"],
                       capture_output=True, text=True, cwd=HERE)
    hs = re.findall(r"^pass 2 line\s+\d+ OPMUL\s+1st .*\bh([0-9a-f]{4})\n\s+2nd .*\bh([0-9a-f]{4})", r.stdout, re.M)
    new, old = [("4008", "2008"), ("0081", "0059")], [("4008", "2008"), ("007e", "0056")]
    want = {"14.00": new, "15.00": new, "13.10.3077": old, "13.10.3052": old,
            "13.00.9044": [("4007", "2007"), ("0077", "004f")]}
    check("R200/R450: OPMUL sort hashes [p+0]/[q+0] 4008/2008; p->c, q->b 81/59 (13.10: 7e/56)", hs, want[RKEY])


if lacking("rules"):
    skip("rules_question_tests: rule probes (tests/data/rules)", lacking("rules"))
else:
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
            cand = lab.startswith(("$LL", "$LC")) if not RKEY.startswith(("13.10", "13.00")) else back
            if not back or not cand or k == 0 or k >= len(ins):
                continue
            pad = int(ins[k - 1][3].split()[1]) if ins[k - 1][2] == "npad" else 0
            o = ins[k][0] - pad
            pred = False if RKEY == "13.00.9044" else (bool(o & 15) and pad_wins(o & 15, [(x[1], x[2]) for x in ins[k:]]))
            rows.append((bool(pad), pred, (-o) & 15))
    check("R1300: AlignmentWins model = every candidate loop head's npad (>= 150 heads)",
          (len(rows) >= 150, sum(p != q for p, q, _ in rows)), (True, 0))
    check("R1300: needs of 9 to 15 are padded too (not only <= 7 / 8)", len({n for p, _, n in rows if p and n >= 9}) >= 3,
          False if RKEY == "13.00.9044" else True)

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
        narrow = RKEY.startswith(("13.10", "13.00")) or (bits & 0x7fffff) >> 12 <= 0x47e
        if (w.group(1) == "DWORD") != narrow:
            bad.append(vals[m.group(1)])
    check("R420: every right-hand constant's width follows the significand rule (13.10: all DWORD)",
          (len(vals) >= 40, bad), (True, []))
    p = procs(asm)
    width = lambda f: (re.findall(r"(?:fld|fcomp|fmul)\s+(DWORD|QWORD) PTR __real@", p.get(f, "")) or ["-"])[0]
    got = tuple(width(f) for f in ("?l99@@YAHXZ", "?m99@@YAHPAUP@@@Z", "?m100@@YAHPAUP@@@Z", "?mul6@@YAMPAUP@@@Z"))
    want = {"14.00": ("QWORD", "DWORD", "QWORD", "QWORD"), "15.00": ("QWORD", "DWORD", "QWORD", "QWORD"),
            "13.10.3077": ("DWORD",) * 4, "13.10.3052": ("DWORD",) * 4, "13.00.9044": ("DWORD",) * 4}
    check("R420: constant on the left / member 99 / member 100 / multiply", got, want[RKEY])

    # R1270: is the res store (+24) issued right before the call (sunk) or before the size store (pinned)?
    # On 13.00.9044 `push -1` issues before `mov [esi+24], 15`, so +24 precedes +20 (not after +20).
    p = procs(listing("R1270_store_index.cpp"))
    def pinned(f):
        ins = [x[3] for x in instrs(next((v for k, v in p.items() if k.startswith("?%s@@" % f)), ""))[0]]
        k = next((i for i, t in enumerate(ins) if "+24], 15" in t), None)
        s = next((i for i, t in enumerate(ins) if "+20]" in t), None)
        if RKEY == "13.00.9044":
            return k is not None and s is not None and k + 1 == s
        return k is not None and k > 0 and "+20]" in ins[k - 1]
    fs = ("a_plain", "a_rev", "a_const", "a_eos", "b_inplace", "b_var", "b_reread", "b_ptr")
    got = " ".join(f for f in fs if pinned(f))
    new, old = "b_inplace b_var b_reread b_ptr", "b_var b_reread b_ptr"
    check("R1270: res store pinned after the size store by a non-literal index / pointer copy",
          got, {"14.00": new, "15.00": new, "13.10.3077": old, "13.10.3052": old, "13.00.9044": old}[RKEY])
    # R1275: the vtable store issues before the EH state store when a member has its own inlined constructor
    # (9044's P5 scheduler pairs the member store +12/+16 with the state store, so vtable issues after on all five)
    p = procs(listing("R1275_member_ctor.cpp", "/EHsc"))
    def early(cls):
        ins = [x[3] for x in instrs(p.get("??0%s@@QAE@PAX@Z" % cls, ""))[0]]
        v = next((i for i, t in enumerate(ins) if "OFFSET" in t and "??_7" in t), 99)
        s = next((i for i, t in enumerate(ins) if "__$EHRec$" in t and t.startswith("mov DWORD PTR __$EHRec$")), -1)
        return v < s
    check("R1275: vtable store above the state store: WithH, WithH2 yes; Plain, WithZ (Zero(int&)), WithC (Clear()) no",
          [early(c) for c in ("WithH", "WithH2", "Plain", "WithZ", "WithC")],
          [False] * 5 if RKEY == "13.00.9044" else [True, True, False, False, False])

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
    if lacking("schedwant"):
        skip("schedwant: alias:X alone gives the pinned order", lacking("schedwant"))
    else:
        got = (alias_wanted("R1270_store_index.cpp", "a_plain", "b_var"),
               alias_wanted("R1275_member_ctor.cpp", "??0Plain@@", "??0WithH@@", "/EHsc"))
        check("schedwant: alias:X alone gives the pinned order (R1270 b_var from a_plain, R1275 WithH from Plain)",
              got, (True, True) if RKEY != "13.00.9044" else
              # 9044: the P5 scheduler pairs the member store +12/+16 with the state store (the R1275
              # vtable check above is [False]*5 here too), so the pinned-order question for WithH is
              # not an alias edit yet — R1270's store-index alias answers. The rules lane enabled this
              # check by removing "rules" from the build's lacks; schedwant's P5 model is next.
              (True, False))

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
    # elements get reader-time views first; 9044: each operator= expansion makes its element's view)
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py"), "keys", os.path.join(RULES, "R310_copy_elem0.cpp"),
                        "--func", "?g@@", "--", "/TP", "/O2"], capture_output=True, text=True, cwd=HERE).stdout
    first = {}
    for m in re.finditer(r"^\s+(\d+) frame \d+\s+_m\+(0|12|24)(?:\.\w+)?\s+(\S*)", r, re.M):
        first.setdefault(m.group(2), (int(m.group(1)), m.group(3)))
    got = (first.get("0", (0, ""))[1].startswith("V::operator=#1"), first.get("0", (99,))[0] < first.get("12", (0,))[0])
    want = {"14.00": (True, True), "15.00": (True, True), "13.10.3077": (False, True), "13.10.3052": (False, True),
            "13.00.9044": (False, True)}
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
    # R1150: bswap / rol (VC6 has no _byteswap_ulong / _byteswap_ushort intrinsic)
    p = procs(listing("R1150_bswap_rot.cpp"))
    has = lambda f, mn: any(x[2] == mn for x in instrs(next((v for k, v in p.items() if k.startswith("?%s@@" % f)), ""))[0])
    got = (has("shiftswap", "bswap"), has("maskswap", "bswap"), has("bsw", "bswap"), has("rotl8", "rol"),
           has("rotsh", "rol"), has("rotr", "ror"))
    new, old = (False, False, True, True, True, True), (False, False, True, True, False, False)
    check("R1150: shift swaps no bswap; intrinsic bswap; _rotl rol; shift rotates rol/ror (13.10: no)", got,
          {"14.00": new, "15.00": new, "13.10.3077": old, "13.10.3052": old,
           "13.00.9044": (False, False, False, True, False, False)}[RKEY])
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


if lacking("rules"):
    skip("rules_misc_tests: rule probes (tests/data/rules)", lacking("rules"))
else:
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


def x64_mine_pairs_tests():
    """cl 17.00 x64 (NColor): effectmine mine / query read every variant through ncolsim (docs/c2/effectmine.md 8) and
    realize pairs / the x64 pair search (docs/c2/realize.md, "cl 17.00 x64"). On every build: the x64 effect types
    (offline). Under an x64 build table: effectmine on tests/data/effectmine.cpp (a copy is one candidate more live,
    an unused local one frame key, both code-neutral), the recovery of tests/data/effectmine17/pert_alias.cpp against
    effectmine.cpp's own compile (unalias: bdiff 0; query finds it in the database), `realize pairs` on
    tests/data/realize17/probes.cpp and the pair search of `realize edit` on realize17/pert.cpp against orig.cpp."""
    em = [sys.executable, os.path.join(HERE, "tools", "effectmine.py")]
    r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import effectmine as E; print(' '.join("
                        "E.etype('R', s) for s in sys.argv[1:]))" % os.path.join(HERE, "tools"), "+app:n:1:2", "-app:s:3:1",
                        "live:3:-1", "live:1:+2", "index:p=5", "bucket:v=0", "blocks:9>12", "range:a", "new:rz_c",
                        "reg:n:rbx>rsi", "before:s:a"], capture_output=True, text=True, cwd=HERE)
    check("effectmine: the x64 effect strings' types (ncolsim's vocabulary)", r.stdout.split(),
          ["R.app+", "R.app-", "R.live-", "R.live+", "R.index", "R.bucket", "R.blocks", "R.range", "R.new", "R.reg",
           "R.before"])
    if _c2b.get("arch") != "x64":
        skip("effectmine / realize pairs on NColor (5 checks)", "x64 only")
        return
    import ncolsim, tsweep, c2trace

    def orig_of(src, func, flags, d):          # our own compile as the original: capstone of the listing's bytes
        run = ncolsim.NRun(src, c2trace.func_decl(func), flags, d)
        lst = tsweep.listing_instrs(run.asm, run.func)
        p = os.path.join(d, func + ".s")
        with open(p, "w") as f:
            for a_, (mn, ops) in zip(lst.addrs, tsweep.cs_text(lst)):
                f.write("+0x%x  %s  %s\n" % (a_, mn, ops))
        return p
    env = dict(os.environ, EFFECTMINE_DIR=tempfile.mkdtemp(prefix="em17test_"))
    EMS = os.path.join(HERE, "tests", "data", "effectmine.cpp")
    r = subprocess.run(em + ["mine", EMS, "--func", "em__base", "--features", "regs,keys", "--eager-feats", "keys",
                             "--classes", r"^(copy|pad\.init\.top|decl\.extvar\.before)$", "--max-compiles", "40",
                             "--depth", "1", "--pairs", "0", "--jobs", "1"] + FLAGS,
                       capture_output=True, text=True, cwd=HERE, env=env)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    dbp = os.path.join(env["EFFECTMINE_DIR"], "effects.jsonl")
    db = [json.loads(l) for l in open(dbp)] if os.path.exists(dbp) else []
    cp = [x for x in db if x["cls"] == "copy" and x["ok"]]
    pad = [x for x in db if x["cls"] == "pad.init.top" and x["meta"].get("n") == 1 and x["ok"]]
    dec = [x for x in db if x["cls"] == "decl.extvar.before" and x["ok"]]
    check("effectmine x64: a copy is a new candidate kept live (live:B:+1), code 0; 1 unused local = 1 frame key; "
          "declarations before the function: null",
          (any("new:rz_c" in x["eff"]["R"] and any(re.match(r"live:\d+:\+1$", e) for e in x["eff"]["R"])
               and x["code"]["code"] == 0 for x in cp),
           [("keys:frame:+1" in x["eff"].get("K", []), x["code"]["ident"]) for x in pad],
           bool(dec) and all(x["null"] for x in dec)), (True, [(True, 0)], True))
    d = tempfile.mkdtemp(prefix="em17orig_")
    orig = orig_of(EMS, "em__base", FLAGS[1:], d)
    PA = os.path.join(HERE, "tests", "data", "effectmine17", "pert_alias.cpp")
    r = subprocess.run(em + ["mine", PA, "--func", "em__base", "--asm-vs", orig, "--want", "bdiff", "--features", "regs",
                             "--classes", r"^unalias$", "--max-compiles", "10", "--depth", "1", "--pairs", "0",
                             "--jobs", "1"] + FLAGS, capture_output=True, text=True, cwd=HERE, env=env)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    db = [json.loads(l) for l in open(dbp)] if os.path.exists(dbp) else []
    un = [x for x in db if x["cls"] == "unalias" and x["ok"] and x["src"] == PA]
    m = re.search(r"base rdiff (\d+), bdiff (\d+)\)", r.stdout)
    check("effectmine x64: the alias perturbation recovered against our own compile (unalias, bdiff 0)",
          (m and int(m.group(2)) > 0, any(x["code"].get("bdo") == 0 and "C.match" in x["types"] for x in un)), (True, True))
    r = subprocess.run(em + ["query", PA, "--func", "em__base", "--asm-vs", orig, "--want", "bdiff", "--features",
                             "regs"] + FLAGS, capture_output=True, text=True, cwd=HERE, env=env)
    check("effectmine x64 query: the database's unalias record answers --want bdiff",
          bool(re.search(r"^\s+unalias\s+code\s+\d+ bag\s+\d+ rdiff 0 bdiff 0", r.stdout, re.M)), True)
    RZ = os.path.join(HERE, "tests", "data", "realize17")
    rz = [sys.executable, os.path.join(HERE, "tools", "realize.py")]
    r = subprocess.run(rz + ["pairs", os.path.join(RZ, "probes.cpp"), "--", "/O2", "/EHsc"], capture_output=True,
                       text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    rows = dict(re.findall(r"^  (\w+)\s+code \d+ .*?inputs ((?:index:\S+ ?)+)\s+\[", r.stdout, re.M))
    check("realize pairs x64: declaration swaps are lifetime-index edits; ncolsim's registers = traced (3 of 3)",
          (rows.get("ps", "").split(), bool(re.search(r"traced on 3 of 3$", r.stdout, re.M))),
          (["index:s=4", "index:p=6"], True))
    d = tempfile.mkdtemp(prefix="rz17_")
    orig = orig_of(os.path.join(RZ, "orig.cpp"), "pr2", ["/O2", "/EHsc"], d)
    r = subprocess.run(rz + ["edit", os.path.join(RZ, "pert.cpp"), "--func", "pr2", "before:p:s", "before:q:t",
                             "--constructs", "swap,split", "--want-regs", "p=rdi,s=rsi,q=r14,t=r15", "--asm-vs", orig,
                             "--keep", os.path.join(d, "k"), "--", "/O2", "/EHsc"], capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    check("realize edit x64, two order edits: the pair search combines both swaps, predicted, bdiff 0 (pert -> orig)",
          (bool(re.search(r"^  pr\d+\s+code \d+ regs \d+  rdiff 0 bdiff 0  pred ok ", r.stdout, re.M)),
           "FOUND (pair search)" in r.stdout), (True, True))


x64_mine_pairs_tests()


def x64_merges_tests():
    """c2merges.py on cl 17.00 x64 from a kept log (tests/data/merges17/tailmerge.log: `C2TRACE_FUNC= c2trace.py merges
    tests/data/tailmerge.c --func c1 --keep DIR -- /O2` under --c2 17.00; the worker threads' events interleave and are
    demuxed by `# thread N`); docs/c2/il_fg.md 7.5, port-1700.md 6."""
    env = dict(os.environ, C2_BUILD="17.00")
    log = os.path.join(HERE, "tests", "data", "merges17", "tailmerge.log")
    got = {}
    for fn_ in ("c1", "c2", "c3", "r1", "r2"):
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2trace.py"), "merges", "--from", log,
                            "--func", fn_], capture_output=True, text=True, cwd=HERE, env=env)
        m = re.search(r"^tail \S+: (\d+) pair\(s\)(?:, (\d+) merged)?", r.stdout, re.M)
        got[fn_] = (int(m.group(2) or 0) if m else None, r.stdout)
    check("merges x64: merged pairs per function (c1 c2 c3 r1 r2; r1: `lea rcx` is fixed by the convention)",
          [got[k][0] for k in ("c1", "c2", "c3", "r1", "r2")], [2, 1, 2, 2, 2])
    check("merges x64: twins f1/f2 stop the walk at the call; the size rule's total read at its probe",
          ("1 matched: stop: call target f2 / f1; size 12 <= 20" in got["c2"][1],
           "5 matched: merged; size 24 > 20" in got["c1"][1]), (True, True))


x64_merges_tests()


def symkeys17_levers_tests():
    """symkeys levers on cl 17.00 x64, checked against compiles (docs/c2/symkeys.md 10.3): tests/data/symkeys17_levers/
    *.cpp are tests/data/symkeys.cpp with the top lever applied as a source change, *.json their exports; the base is
    tests/data/symkeys17/symkeys.json. No compile."""
    env = dict(os.environ, C2_BUILD="17.00")
    base = os.path.join(HERE, "tests", "data", "symkeys17", "symkeys.json")
    L = os.path.join(HERE, "tests", "data", "symkeys17_levers")

    def sk(*args):
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "symkeys.py")] + list(args), capture_output=True,
                           text=True, cwd=HERE, env=env)
        return r.stdout + r.stderr
    rates = []
    for fn_, edits in (("kparams", ["add:frame@r:28"]), ("kx87", ["add:frame@s:1", "add:frame@q:4"]),
                       ("kdead", ["sub:frame@r:1"])):
        out = sk("compare", "--from", base, "--func", fn_, *edits, "--vs", os.path.join(L, fn_ + ".json"))
        k = re.search(r"keys: (\d+) symbols in both; predicted = traced for (\d+)", out)
        s_ = re.search(r"sorts .*?: (\d+); predicted order = traced for (\d+)", out)
        rates.append(bool(k and s_ and k.group(1) == k.group(2) and s_.group(1) == s_.group(2)))
    check("symkeys17 levers: every key and pass-2 order of the 3 applied levers predicted (kparams, kx87, kdead)",
          rates, [True, True, True])
    out = sk("levers", "--from", base, "--func", "kparams", "--var", "r", "--above", "40", "--max", "30")
    traced = sk("keys", "--from", os.path.join(L, "kparams.json"), "--func", "kparams", "--brief")
    check("symkeys17 levers --var: 28 dead locals before r move it to the next frame block (128), as compiled",
          (bool(re.search(r"^add:frame@r:28\n    -> r key 128 ", out, re.M)), " r:128 " in traced), (True, True))
    out = sk("levers", "--from", base, "--func", "kdead", "--var", "r", "--below", "4", "--max", "3")
    check("symkeys17 levers: a sub edit names the local it removes (kdead: the dead `float d = 0;`)",
          bool(re.search(r"^sub:frame@r:1\n.*\n.*\n    removes: d ", out, re.M)), True)
    out = sk("levers", "--from", base, "--func", "kx87", "--site", "1:s>[p", "--max", "4", "--top", "3")
    check("symkeys17 levers --site: no edit set removes a symbol the code reads (kx87: s, p, q are operands)",
          (bool(re.search(r"^# \d+ sub edits dropped", out, re.M)), bool(re.search(r"^1/1 sites", out, re.M)),
           "removes:" in out), (True, True, False))


symkeys17_levers_tests()


def x64_compile_tests():
    """cl 17.00 x64 tools that need a traced compile (local wine or the compile server): c2ltcg regs routed to
    c2ncol inside link.exe (docs/c2/ltcg.md), icount's pass attribution (docs/c2/icount.md 17.00)."""
    if _c2b.get("arch") != "x64":
        skip("ltcg regs / icount on cl 17.00 x64 (4 checks)", "x64 builds only (C2_BUILD=17.00)")
        return
    env = dict(os.environ, C2_BUILD="17.00")

    def run17(*args):
        r = subprocess.run([sys.executable] + [os.path.join(HERE, "tools", args[0])] + list(args[1:]),
                           capture_output=True, text=True, cwd=HERE, env=env)
        if VERBOSE:
            print(r.stdout[-3000:], r.stderr[-2000:])
        return r.stdout + r.stderr
    KA, KB = (os.path.join(HERE, "tests", "data", f) for f in ("ltcg_kill_a.c", "ltcg_kill_b.c"))
    reg = {}
    for fn_ in ("c_eax", "c_ext"):
        out = run17("c2ltcg.py", "regs", KA, "--with", KB, "--func", fn_)
        m = re.search(r"^\s+\d+\s+a\$1\s+prio \d+\s+\S+\s+-> (\w+)", out, re.M)
        reg[fn_] = m.group(1) if m else None
    check("ltcg regs x64 (NColor in link.exe): a in a volatile register across k_eax (uses only eax), esi across k_ext",
          (reg["c_eax"] in ("ecx", "edx", "r8d", "r9d", "r10d", "r11d"), reg["c_ext"]), (True, "esi"))
    d = tempfile.mkdtemp()

    def classes(src, p):
        js = os.path.join(d, p + ".json")
        run17("icount.py", os.path.join(HERE, "tests", "data", src), "--func", "::%s_ours" % p, "--vs-func",
              "::%s_orig" % p, "--allow-local", "--json", js, "--", "/TP", "/O2")
        j = json.load(open(js)) if os.path.exists(js) else {}
        return j.get("ours", 0) - j.get("orig", 0), sorted(j.get("classes", {}))
    dl, cl = classes("icount17.cpp", "kill")
    check("icount x64: a reload after a store through another pointer: kept:kill (the load SizeOpt re-made)",
          (dl, "kept:kill" in cl), (2, True))
    dl, cl = classes("icount.cpp", "tail")
    check("icount x64: equal tails merged (the 17.00 merge events): merged:FgDoLateFlowOpts",
          "merged:FgDoLateFlowOpts" in cl, True)
    dl, cl = classes("icount.cpp", "dead")
    check("icount x64: the first of two stores is dead: removed-by:GlobalOptimize/DeadStores",
          (dl, cl), (-1, ["removed-by:GlobalOptimize/DeadStores"]))


x64_compile_tests()


def probe_modules():
    """tests/probes/*.py: probe checks kept in their own files (one per doc or topic), run in this module's namespace
    so they use check(), rule_compile(), rule_proc(), rule_trace(), RKEY and the other helpers directly."""
    import glob
    for f in sorted(glob.glob(os.path.join(HERE, "tests", "probes", "*.py"))):
        why = lacking("probe:" + os.path.basename(f))
        if why:
            skip("probes/%s" % os.path.basename(f), why)
            continue
        exec(compile(open(f).read(), f, "exec"), dict(globals(), __file__=f))


probe_modules()

print("%d of %d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
