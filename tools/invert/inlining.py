#!/usr/bin/env python3
"""Inlined call sites -> which callees must be inline / visible / defined first.  Doc: docs/c2/invert.md section 5.

Forward rule (inline.md rules 1-8, [C]): at /O2 c2 compiles callee-first and weighs each direct call to a function
whose body it has: `c = size - B2 - B0 - (10 if the callee has no branch / switch / side-effecting call)`, B2 = 1 +
2 per ordinary argument + 5 per constant or address argument (`this` counts); reject if c > 450; accept at once if
d' < 14 (d' = d/3 for a static callee called once, d - d/(2n) for n < 10 calls); else v = (d*1e8/w + 6d)/7 (w <= 1e8)
or (d*1e8/w + 2d)/3 (in a loop: w = 1e8 x 5 per loop level, x 0.5 in an if arm), inline iff v < 74 (13.10: d' plus
size*loop share/255).  `__forceinline` skips the model; an extern (only declared) callee, an indirect call, a
varargs / noinline / try-catch callee is never inlined.

Inverse, per call site of the original (from its bytes: a `call FUN_x` that stayed, the callee's own out-of-line
size, the arguments pushed, the loop depth of the block):

  called + model says v >= 74        -> free: the callee may be defined in the unit (it stays a call)
  called + model says v < 74         -> the callee must NOT be visible with its body when the caller is compiled:
                                        declared only (the lifter's `FUN_x` externs), defined in another unit,
                                        or __declspec(noinline); defining it ahead of the caller would inline it
  inlined (the callee's code inside)  -> the callee must be defined in the unit and compiled first (defined before
                                        the caller, or later but callee-first by the call graph), and either the
                                        model accepts it at that site or it is __forceinline

usage: inlining.py site SIZE [--args N] [--consts K] [--loop L] [--if] [--nobranch] [--build B]   one site's verdict
       inlining.py known [N]                 N Mount & Blade byte-exact sources traced with c2inline: the verdict from
                                            size / argument count / block weight against the inliner's
       inlining.py detect                    inlined bodies of the project's byte-exact leaf callees inside the
                                            non-exact /O2 benchmark rows (shape match: registers / numbers wildcarded)
       inlining.py measure [--limit N]      Spore /O2 non-exact rows: lifted call sites whose callee the model would
                                            inline if the unit defined it (so the extern declaration is required)
"""
import os, re, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def model(size, nargs=0, nconst=0, loop=0, in_if=False, nobranch=False, static_calls=None, loop_share=0,
          build="14.00"):
    """(inline?, c, v) by inline.md rule 3."""
    b2 = 1 + 2 * (nargs - nconst) + 5 * nconst
    c = size - b2 - (10 if nobranch else 0)
    if c > 450:
        return False, c, None
    d = c
    if static_calls == 1:
        dp = d // 3
    elif static_calls and static_calls < 10:
        dp = d - d // (2 * static_calls)
    else:
        dp = d
    if dp < 14:
        return True, c, dp
    if build.startswith("13"):
        dp = dp + size * loop_share // 255
    w = 1e8 * (5 ** loop) * (0.5 if in_if else 1)
    v = (dp * 1e8 / w + 6 * dp) / 7 if w <= 1e8 else (dp * 1e8 / w + 2 * dp) / 3
    return v < 74, c, v


def inverse(called, size, **kw):
    """Source requirement for one site: called=True when the original kept the call."""
    inl, c, v = model(size, **kw)
    if called:
        return ("free" if not inl else "hide"), c, v
    return ("define-first" if inl else "forceinline"), c, v


CALL = re.compile(r"\bFUN_([0-9a-f]{8})\(([^;]*)\)")


def _args(text):
    """Top-level comma-separated arguments of a call's text (rough: parenthesis depth)."""
    out, d, cur = [], 0, ""
    for ch in text:
        if ch == "," and d == 0:
            out.append(cur)
            cur = ""
            continue
        d += ch in "(["
        d -= ch in ")]"
        cur += ch
    if cur.strip():
        out.append(cur)
    return out


def measure(rundir="work/lbench/spore/sb_base_spore_o2", corpus="work/corpus/spore.jsonl", limit=None):
    import json, collections
    root = os.path.dirname(HERE)
    f = open(os.path.join(root, corpus))
    f.readline()
    size = {json.loads(l)["va"]: json.loads(l)["size"] for l in f}
    src = os.path.join(root, rundir, "src")
    files = sorted(os.listdir(src))[:limit] if limit else sorted(os.listdir(src))
    cnt = collections.Counter()
    for fn in files:
        t = open(os.path.join(src, fn), errors="replace").read()
        body = t[t.find("\n{"):] if "\n{" in t else t
        for m in CALL.finditer(body):
            va = m.group(1)
            if va == fn[:8] or va not in size:
                cnt["unknown"] += 1
                continue
            args = _args(m.group(2))
            consts = sum(1 for a in args if re.fullmatch(r"\s*(\(\w+[\w ]*\*?\))?\s*-?(0x[0-9a-f]+|\d+)\s*", a)
                         or "&" in a or "g_" in a)
            pre = body[:m.start()]
            loop = len(re.findall(r"\bdo \{|\bwhile \(|\bfor \(", pre)) - len(re.findall(r"\} while \(", pre))
            verdict, c, v = inverse(True, size[va], nargs=len(args) + ("->FUN_" in body[max(0, m.start() - 3):m.start()]),
                                    nconst=consts, loop=max(0, min(loop, 1)))
            cnt[verdict] += 1
    tot = cnt["free"] + cnt["hide"]
    print("Spore /O2 lifted units (%s): %d direct call sites with a known callee size; the model would inline %d "
          "(%.1f%%) if the callee were defined ahead (hide: the extern declaration is required), %d stay calls "
          "anyway (free); %d calls without a size (recursion, imports)" %
          (os.path.basename(rundir), tot, cnt["hide"], 100.0 * cnt["hide"] / max(1, tot), cnt["free"], cnt["unknown"]))
    return cnt


UNDNAME = "/opt/homebrew/opt/llvm/bin/llvm-undname"
ROW = re.compile(r"^(?:in )?(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s+(?:(\d+)%\s+)?(?:(-?\d+)\s+)?"
                 r"(?:(\d+(?:\.\d+)?(?:e\+?\d+)?)\s+)?(?:(-?\d+)\s+)?(yes|no|never)\s+(INLINE|CALL|.*)")


def nparams(sym):
    """Arguments the call passes, from the callee's decorated name: its parameters, plus `this` for a member."""
    import shutil, subprocess
    if not sym.startswith("?"):
        return None
    r = subprocess.run([UNDNAME if os.path.exists(UNDNAME) else shutil.which("llvm-undname") or "llvm-undname"],
                       input=sym + "\n", capture_output=True, text=True).stdout.strip().split("\n")[-1]
    m = re.search(r"\((.*)\)", r)
    if not m:
        return None
    inner = m.group(1).strip()
    n = 0 if inner in ("", "void") else inner.count(",") + 1 - inner.count("<") * 0
    return n + (1 if "__thiscall" in r else 0)


def _known_one(args):
    import shlex, subprocess
    (src, flags), inc, build = args
    r = subprocess.run([sys.executable, os.path.join(HERE, "c2inline.py"), src, "--"] + shlex.split(flags) + inc,
                       capture_output=True, text=True, env=dict(os.environ, C2_BUILD=build))
    out = []
    for ln in r.stdout.splitlines():
        m = ROW.match(ln)
        if not m or m.group(11) != "yes" or int(m.group(3)) != 1:
            continue
        verdict = m.group(12).startswith("INLINE")
        out.append((m.group(2), int(m.group(4)), int(m.group(5)), float(m.group(9)) if m.group(9) else 1e8,
                    int(m.group(7) or 0), verdict))
    return args[0], out


def known(limit=40, jobs=6):
    """Spore-free known answers: Mount & Blade byte-exact sources, traced with c2inline (13.10.3052): for each
    depth-1 site whose callee was compiled, the inverse's verdict from what the bytes give (the callee's compiled
    size, its argument count from its decorated name, the call block's weight) against the inliner's real one."""
    import collections, json, random
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from concurrent.futures import ThreadPoolExecutor
    import x87names
    rows, head = x87names.mb_rows()
    srcs = sorted({(r[0], r[3]) for r in rows})
    srcs = sorted(random.Random(5).sample(srcs, min(limit, len(srcs))))
    with ThreadPoolExecutor(jobs) as ex:
        got = list(ex.map(_known_one, [(s, head.get("src_inc", []), head.get("c2", "13.10.3052")) for s in srcs]))
    cnt = collections.Counter()
    cache = {}
    for _src, sites in got:
        for callee, size, c, w, loop, verdict in sites:
            if callee not in cache:
                cache[callee] = nparams(callee)
            n = cache[callee]
            if n is None:
                cnt["no-signature"] += 1        # a C name: no parameter list in it; taken as 0 arguments
                n = 0
            lv = 0 if w <= 1e8 else max(1, round(__import__("math").log(w / 1e8, 5)))
            pred, _c, _v = model(size, nargs=n, loop=lv, in_if=w < 1e8, loop_share=loop * 255 // 100,
                                 build="13.10")
            cnt[(pred, verdict)] += 1
    tot = sum(v for k, v in cnt.items() if isinstance(k, tuple))
    ok = cnt[(True, True)] + cnt[(False, False)]
    print("Mount & Blade sources traced: %d; depth-1 sites with a compiled callee: %d; the model's verdict from the "
          "callee's size, argument count and the block weight agrees with the inliner for %d (%.1f%%): inline/inline %d,"
          " call/call %d, predicted inline but called %d, predicted call but inlined %d; %d callees without a "
          "decorated signature (taken as 0 arguments)" % (len(got), tot, ok, 100.0 * ok / max(1, tot), cnt[(True, True)],
                                    cnt[(False, False)], cnt[(True, False)], cnt[(False, True)], cnt["no-signature"]))
    return cnt


def _mnems(img, va, size):
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    o = img.off(va)
    return [(i.mnemonic, i.op_str) for i in md.disasm(img.data[o:o + size], va)] if o is not None else []


def _shape(ins):
    """An instruction with registers and numbers wildcarded (an inlined copy gets other registers and offsets)."""
    m, ops = ins
    ops = re.sub(r"\[esp[^]]*\]", "[S]", ops)               # stack slots move; member offsets stay
    ops = re.sub(r"\b(e?[abcd]x|[abcd][lh]|e?[sd]i|ebp)\b", "R", ops)
    return m + " " + ops


def detect(corpus="work/corpus/spore.jsonl", sel="work/staticbench/sel_spore_o2.txt", lo=6, hi=40):
    """Inlined callee bodies in the non-exact rows: every byte-exact leaf function of the project (no call) with
    lo..hi instructions is turned into its body's shape sequence (prologue pushes, the stack-argument loads and the
    ret dropped); a row whose own shape sequence contains it is a site where the original inlined that callee."""
    import collections
    root = os.path.dirname(HERE)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, HERE)
    import operands, json
    from w8_emit import Image
    f = open(os.path.join(root, corpus))
    head = json.loads(f.readline())
    size = {json.loads(l)["va"]: json.loads(l)["size"] for l in f}
    img = Image(head["image"])
    exact = sorted({r[2] for r in operands.spore_o2_rows() if r[2] in size})
    bodies = {}
    for va in exact:
        ins = _mnems(img, int(va, 16), size[va])
        if any(m == "call" for m, _ in ins):
            continue
        body = [x for x in ins if not (x[0] in ("push", "pop", "ret") or
                                        (x[0] == "mov" and re.search(r"\[esp \+ (?:0x)?[0-9a-f]+\]$", x[1])))]
        if lo <= len(body) <= hi:
            bodies.setdefault(tuple(_shape(x) for x in body), va)
    hits, rows_hit = collections.Counter(), 0
    rows = [l.split()[0] for l in open(os.path.join(root, sel)) if l.strip()]
    for va in rows:
        sh = [_shape(x) for x in _mnems(img, int(va, 16), size.get(va, 0))]
        txt = "\n".join(sh) + "\n"
        found = [b for b in bodies if "\n".join(b) + "\n" in txt]
        if found:
            rows_hit += 1
            for b in found:
                hits[bodies[b]] += 1
    print("%d byte-exact leaf callees of %d..%d instructions; %d of %d non-exact /O2 rows contain one of their bodies "
          "(%d sites, %d distinct callees; shape = mnemonics and operands, registers and stack slots wildcarded);"
          " most inlined: %s" % (
              len(bodies), lo, hi, rows_hit, len(rows), sum(hits.values()), len(hits),
              ", ".join("%s x%d" % kv for kv in hits.most_common(5))))
    return hits


def main(argv):
    import argparse
    if argv and argv[0] == "detect":
        detect()
        return 0
    if argv and argv[0] == "known":
        known(int(argv[1]) if len(argv) > 1 else 40)
        return 0
    if not argv or argv[0] not in ("site", "measure"):
        print(__doc__)
        return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("size", nargs="?", type=int)
    ap.add_argument("--args", type=int, default=0)
    ap.add_argument("--consts", type=int, default=0)
    ap.add_argument("--loop", type=int, default=0)
    ap.add_argument("--if", dest="in_if", action="store_true")
    ap.add_argument("--nobranch", action="store_true")
    ap.add_argument("--build", default="14.00")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "measure":
        measure(limit=a.limit)
        return 0
    inl, c, v = model(a.size, a.args, a.consts, a.loop, a.in_if, a.nobranch, build=a.build)
    print("c %d  v %s  -> %s; if the original kept the call: %s; if it inlined it: %s" % (
        c, "%.1f" % v if v is not None else "-", "inline" if inl else "call",
        inverse(True, a.size, nargs=a.args, nconst=a.consts, loop=a.loop, in_if=a.in_if, nobranch=a.nobranch)[0],
        inverse(False, a.size, nargs=a.args, nconst=a.consts, loop=a.loop, in_if=a.in_if, nobranch=a.nobranch)[0]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
