#!/usr/bin/env python3
"""Sweep a source template's alternatives and rank the variants by a number read from a c2 trace.

usage: tsweep.py <template.cpp> --func SUBSTR --metric EXPR [--show EXPR]... [--jobs 8] [--max 256] [--top 10]
                 [--asm-vs ORIG.s --asm-func DECORATED] [-- <cl args>]

The template uses sweep.py's syntax: alternatives written `{{first<|>second<|>third}}`. Every combination is
compiled with the traced c2 (tools/c2trace.py) and scored by EXPR, a Python expression; lower is better. Use it
when the byte diff is a poor guide, i.e. when the fix is "make X outrank Y" or "get the cursor to eax at pick 7"
and every intermediate step looks like no progress in bytes. Names available in EXPR (and --show):

  prio(var)          priority of var's first pop in Color (the `regs` table)
  prio(var, k)       var's priority in Spillcosts pass k (largest piece if it was split; None if absent)
  reg(var, n=1)      register var's n-th popped piece got ('spill/defer' if none)
  pops(var)          number of popped pieces of var
  cur(n), sreg(n)    scratch pick n: the cursor before it, the register it got (the `scratch` table)
  rot(n)             1 if pick n moved the cursor
  off(n)             listing offset of pick n (None if 'gone')
  gone()             number of picks whose instruction is gone from the final code
  sat(offset)        register of the scratch pick at a listing offset (None if no scratch pick there, e.g. once
                     the value became a Color variable: use dst(offset) to score the register in the code)
  ins(offset)        our instruction at a listing offset ('mov eax, DWORD PTR [esi+672]'), dst(offset) its first
                     register (both need --asm-func)
  regdiff            instructions (mnemonic + registers) left unmatched by an alignment with --asm-vs, both sides
                     counted (a substitution 2, an insertion 1); posdiff: the positional count over the common length
  hinge(x, t)        max(0, x - t): 0 once x <= t (e.g. hinge(prio('this',2), 79))

A variant whose metric raises (e.g. a name not found) scores as +inf and its error is shown.
Example (Fallout 3 0x008e7d40):
  tsweep.py t.cpp --func FUN_00c7e9a0@SceneOwner \\
      --metric "hinge(prio('this',2), 79) + 10*(sat(0xa8)!='eax')" --show "prio('scene',2)" -- /TP /MT /GS /EHsc /Gy
"""
import argparse, difflib, itertools, math, os, re, shutil, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from image import ROOT
import c2trace, c2regs, c2scratch
ALT = re.compile(r"\{\{(.*?\}*)\}\}(?!\})", re.S)   # a code `}` just before the closing `}}` stays in the alternative

REG = re.compile(r"\b(e?[abcd]x|[abcd][lh]|e?si|e?di|e?bp|e?sp|st\(\d\))\b")


def listing_offsets(path, func):
    """{offset: 'mnemonic operands'} for our listing of func."""
    text = open(path, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)      # any --func form; `NAME<tab>PROC` (C names, cl 13.10) too
    out, pend = {}, None
    for ln in text[i:j].splitlines():
        m = re.match(r"\s+([0-9a-f]{5})\t[0-9a-f ]+(?:\t+ (\w+)\s*(.*))?$", ln)
        c = re.match(r"\t[0-9a-f ]+\t+ (\w+)\s*(.*)", ln)       # continuation line of a long instruction
        if m:
            if m.group(2):
                out[int(m.group(1), 16)] = (m.group(2) + " " + re.sub(r"\s*;.*", "", m.group(3))).strip()
                pend = None
            else:
                pend = int(m.group(1), 16)
        elif c and pend is not None:
            out[pend] = (c.group(1) + " " + re.sub(r"\s*;.*", "", c.group(2))).strip()
            pend = None
    return out


def listing_instrs(path, func):
    text = open(path, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)
    body = text[i:j]
    out = []
    for ln in body.splitlines():
        m = re.match(r"(?:\s+[0-9a-f]{5})?\t[0-9a-f ]+\t+ (\w+)\s*(.*)", ln)
        if m:
            out.append((m.group(1), re.sub(r"\s*;.*", "", m.group(2))))
    return out


def orig_instrs(path):
    """Lines 'VA  mnemonic  operands' (capstone-style disassembly of the original function)."""
    out = []
    for ln in open(path):
        p = ln.split(None, 2)
        if len(p) >= 2:
            out.append((p[1], p[2].strip() if len(p) > 2 else ""))
    return out


def _key(ins):
    if ins[0] in ("rep", "repe", "repne"):          # capstone spells out the implicit operands of rep movsd/stosd
        return (ins[0], ins[1].split()[0].lower() if ins[1] else "")
    return (ins[0], tuple(REG.findall(ins[1].lower())))


def regdiff(ours, orig):
    """Aligned difference: instructions (mnemonic + registers) not matched by a longest-common-subsequence style
    alignment, counted on both sides (a substitution counts 2, an insertion 1). Our trailing lines past the last
    match are ignored: our listing may carry EH funclets the original's function range lacks."""
    a, b = [_key(x) for x in ours], [_key(y) for y in orig]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    blocks = [m for m in sm.get_matching_blocks() if m.size]
    if not blocks:
        return len(a) + len(b)
    last = blocks[-1]
    matched = sum(m.size for m in blocks)
    return (last.a + last.size - matched) + (len(b) - matched)


def posdiff(ours, orig):
    """Positional difference over the common length (the old regdiff)."""
    return sum(1 for x, y in zip(ours, orig) if _key(x) != _key(y))


class Ctx:
    def __init__(self, src, func, cl, need_regs, need_scratch, orig, asm_func, work):
        self.func = func
        self.regs = self.picks = None
        self.ns = {}
        if need_regs:
            _, _, rc, out, log = c2trace.run_trace(src, c2regs.HOOKS, cl, keep=os.path.join(work, "r"), quiet=True)
            fns = c2regs.parse(log, func)
            if not fns:
                raise RuntimeError("function not register-allocated (compile error?): " + out[-300:])
            self.regs = fns[0]
        if need_scratch or orig:
            _, _, rc, out, log = c2trace.run_trace(src, c2scratch.HOOKS, cl, keep=os.path.join(work, "s"), quiet=True)
            picks, final = c2scratch.parse(log, func)
            pos = c2scratch.offsets(final)
            for p in picks:
                at = pos.get(p["instr"])
                p["off"] = at[0] if at else None
            self.picks = picks
            stem = os.path.splitext(os.path.basename(src))[0]
            asm = os.path.join(work, "s", stem + ".asm")
            if orig:
                ours = listing_instrs(asm, asm_func)
                self.ns["regdiff"] = regdiff(ours, orig)
                self.ns["posdiff"] = posdiff(ours, orig)
            self.lst = listing_offsets(asm, asm_func) if asm_func else {}
        self.ns.update(prio=self.prio, reg=self.reg, pops=self.pops, cur=self.cur, sreg=self.sreg, rot=self.rot,
                       off=self.off, gone=self.gone, sat=self.sat, ins=self.ins, dst=self.dst, hinge=lambda x, t: max(0, x - t),
                       max=max, min=min, abs=abs, inf=math.inf)

    def _rows(self, var):
        return [r for r in self.regs["rows"] if r["name"] == (var[1:] if var.startswith("_") else var)]

    def prio(self, var, k=None):
        if k is None:
            rows = self._rows(var)
            if not rows:
                raise KeyError("no globreg %r" % var)
            return rows[0]["prio"]
        passes = self.regs.get("passes", {})
        for kk in range(k, 0, -1):          # later passes only re-cost new pieces: fall back to the latest earlier one
            vals = [t for (nm, g), t in passes.get(kk, {}).items() if nm == (var[1:] if var.startswith("_") else var)]
            if vals:
                return max(vals)
        return None

    def reg(self, var, n=1):
        rows = self._rows(var)
        return rows[n - 1]["out"] if len(rows) >= n else None

    def pops(self, var):
        return len(self._rows(var))

    def _pick(self, n):
        return self.picks[n - 1]

    def cur(self, n):
        return self._pick(n)["cur"]

    def sreg(self, n):
        return self._pick(n)["reg"]

    def rot(self, n):
        return int(self._pick(n)["rot"])

    def off(self, n):
        return self._pick(n)["off"]

    def gone(self):
        return sum(1 for p in self.picks if p["off"] is None)

    def ins(self, o):
        return self.lst.get(o)

    def dst(self, o):
        t = self.lst.get(o)
        m = REG.search(t.split(None, 1)[1].lower()) if t and " " in t else None
        return m.group(1) if m else None

    def sat(self, o):
        for p in self.picks:
            if p["off"] == o:
                return p["reg"]
        return None


def main():
    argv = sys.argv[1:]
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("template")
    ap.add_argument("--func", required=True)
    ap.add_argument("--metric", required=True)
    ap.add_argument("--show", action="append", default=[])
    ap.add_argument("--asm-vs", help="the original function's disassembly ('VA mnemonic operands' lines)")
    ap.add_argument("--asm-func", help="decorated name of the function in our listing (for regdiff)")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--max", type=int, default=256)
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    exprs = [a.metric] + a.show
    need_regs = any(re.search(r"\b(prio|reg|pops)\(", e) for e in exprs)
    need_scratch = any(re.search(r"\b(cur|sreg|rot|off|gone|sat|ins|dst)\(", e) for e in exprs)
    if any(re.search(r"\b(ins|dst)\(", e) for e in exprs) and not a.asm_func:
        sys.exit("ins()/dst() need --asm-func (the function's decorated name in our listing)")
    if re.search(r"\b(regdiff|posdiff)\b", " ".join(exprs)) and not (a.asm_vs and a.asm_func):
        sys.exit("regdiff needs --asm-vs and --asm-func")
    orig = orig_instrs(a.asm_vs) if a.asm_vs else None

    text = open(a.template).read()
    spots = [m.group(1).split("<|>") for m in ALT.finditer(text)]
    combos = list(itertools.product(*[range(len(s)) for s in spots]))
    if len(combos) > a.max:
        sys.exit("%d combinations (> --max %d): split the grid" % (len(combos), a.max))
    out = os.path.join(ROOT, "work", "tsweep", os.path.basename(a.template).rsplit(".", 1)[0])
    if c2trace.c2build.table():       # another c2 build (--c2 / C2_BUILD): its own variant directory
        out += "@" + c2trace.c2build.name()
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    hdr_dir = os.path.dirname(os.path.abspath(a.template))
    jobs = []
    for k, combo in enumerate(combos):
        it = iter(combo)
        body = ALT.sub(lambda m: m.group(1).split("<|>")[next(it)], text)
        body = re.sub(r'#include "([^"]+)"', lambda m: '#include "%s"' % os.path.join(hdr_dir, m.group(1))
                  if os.path.exists(os.path.join(hdr_dir, m.group(1))) else m.group(0), body)
        d = os.path.join(out, "v%03d" % k)
        os.makedirs(d)
        p = os.path.join(d, "v%03d.cpp" % k)
        open(p, "w").write(body)
        jobs.append((p, combo, d))
    print("%d spots, %d combinations, %d jobs (traces: %s)" % (len(spots), len(combos), a.jobs,
          "+".join(n for n, f in (("regs", need_regs), ("scratch", need_scratch or orig)) if f)), flush=True)

    def run(job):
        p, combo, d = job
        try:
            ctx = Ctx(p, a.func, cl, need_regs, need_scratch, orig, a.asm_func, d)
            vals = [eval(e, {"__builtins__": {}}, ctx.ns) for e in exprs]
            return job, vals, None
        except SystemExit as e:
            return job, None, "trace failed: %s" % str(e)[-200:]
        except Exception as e:
            return job, None, "%s: %s" % (type(e).__name__, e)

    with ThreadPoolExecutor(a.jobs) as ex:
        res = list(ex.map(run, jobs))
    ok = sorted(((v[0], j) + (v,) for j, v, err in res if v is not None and v[0] is not None),
                key=lambda t: t[0])
    bad = [(j, err) for j, v, err in res if v is None or v[0] is None]
    hdr = "metric" + "".join("  | %s" % s for s in a.show)
    print(hdr)
    for m, (p, combo, d), vals in ok[:a.top]:
        print("%8s%s  %s  %s" % (m, "".join("  | %s" % (x,) for x in vals[1:]), os.path.basename(p),
              " | ".join("%d:%s" % (i, spots[i][c].strip().replace("\n", " ")[:40]) for i, c in enumerate(combo))))
    if bad:
        print("%d variants unscored, e.g. %s: %s" % (len(bad), os.path.basename(bad[0][0][0]), bad[0][1]))
    if ok and ok[0][0] == 0:
        print("METRIC 0: %s" % ok[0][1][0])


if __name__ == "__main__":
    main()
