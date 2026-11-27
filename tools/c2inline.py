#!/usr/bin/env python3
"""c2inline: why c2 inlined a call here (or not), per call site, from one traced compile.  Doc: docs/c2/inline.md

usage: c2inline.py SRC [--func F] [--callee C] -- <cl args>       (C2_BUILD=13.10.3052 / 13.10.3077)

For every call site the inliner weighs (InlCallGraphDecision, the /O2 cost model of inline.md rule 3) it prints the
function being compiled, the callee, the inlining depth, and the model's numbers:

  size   the callee's compiled size in bytes
  c      size - benefits (+ bloat once caller + callee pass 9000 instructions); c > 450 rejects
  d      c - the weighted benefit terms; d < SizeLimit + 5 (14) accepts at once
  d'     d + size*B/255 (13.10), B = the callee's loop share in 255ths (FUNC node byte +0x4e; 0 for a loop-free
         callee; shown as loop%): on 13.10 a callee that is mostly loop is less likely to be inlined
  w      the call block's static weight (1e8 straight line, x5 per loop level, x0.5 in an if arm)
  v      (d'*1e8/w + (D-1)*d') / D, D = 7 when w <= 1e8 and 3 when w > 1e8 (a hotter block takes another
         branch, with no 64-bit divide); inline iff v < SpeedLimit (74). The tool recomputes v from d', w and
         flags a site where it disagrees with the traced value (another branch of the model).

and the verdict with the step that decided it. A site the model never reached (a __forceinline callee, a callee
that is not compiled yet, a veto) shows only the verdict. InlCallGraphDecision's "yes" is the cost model's; the
vetoes after it (InlBadCandidate, inline.md 8a) can still keep the call.

The probes are found by byte signature in the build's c2.dll (taken from 13.10.3052, where the function is split
into chunks: the size load at +0x2a, the c > 450 test, the early-accept compare, the v compare), so both 13.10
builds work without an address table. 14.00 has its own probe set: work/scratch/inline/hooks.sh.

Built for Mount & Blade's inline questions: ??_H / ??_E called in the original but inlined in ours.
"""
import argparse, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# (name, signature bytes, offset of the probed instruction in the signature, specs)
PROBES = [
    ("c", bytes.fromhex("81fec2010000"), 0, "esi,edi"),               # cmp esi, 0x1c2: esi = c, edi = size
    ("d", bytes.fromhex("83c0053bd8"), 3, "ebx,eax"),                  # add eax, 5; cmp ebx, eax: ebx = d
    ("B", bytes.fromhex("8a424e84c0"), 0, "edx>0x4c"),                # mov al, [edx+0x4e]: the callee's loop share
    ("v", bytes.fromhex("f7f18b4c241c3b05"), 6, "eax,ebp,ebx,esi,ecx>0"),  # div ecx; ..; cmp eax, [SpeedLimit]: v, w, d'

]


def find_probes(dll):
    import pefile
    pe = pefile.PE(dll, fast_load=True)
    ib, data = pe.OPTIONAL_HEADER.ImageBase, pe.get_memory_mapped_image()
    out = {}
    for name, sig, off, spec in PROBES:
        hits = [m.start() for m in re.finditer(re.escape(sig), data)]
        if len(hits) != 1:
            sys.exit("c2inline: probe %s: %d matches of its signature in %s (want 1); this build needs a table"
                     % (name, len(hits), dll))
        out[name] = ("%x" % (ib + hits[0] + off), spec)
    return out


def main(argv):
    if "--" in argv:
        i = argv.index("--")
        argv, cl = argv[:i], argv[i + 1:]
    else:
        cl = []
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--func", help="only sites in functions whose decorated name contains this")
    ap.add_argument("--callee", help="only sites whose callee's name contains this")
    ap.add_argument("--keep")
    a = ap.parse_args(argv)
    import c2build, c2trace
    if not c2build.name().startswith("13.10"):
        sys.exit("c2inline: 13.10 builds only (C2_BUILD=13.10.3052 / 13.10.3077); 14.00: work/scratch/inline/hooks.sh")
    cf = c2trace.curfunc_va()
    pr = find_probes(os.path.join(c2trace.TBIN, "c2.dll"))
    hooks = ["@Inline@4=g%s>0>0>4$" % cf,          # hook order is the log's hook index (parsed below)
             "@InlCallGraphDecision@12=s1>4>4$,s1>4>0x81/xeax"] + ["%s=%s" % pr[n] for n in ("c", "d", "v", "B")]
    img, hk, rc, out, log = c2trace.run_trace(a.src, hooks, cl, keep=a.keep, func=False)
    if rc:
        sys.stdout.write(out)
        return rc
    # raw log: `E seq nest hook eax ecx edx ebx ebp esi edi esp | specs` (entry / probe), `X seq nest hook eax .. | specs`
    # hooks: 0 Inline (one per function, all before the decisions: its ebx is the FUNC), 1 ICGD (ebp = the caller's
    # FUNC), 2 c, 3 d, 4 v, 5 B
    REG = ("eax", "ecx", "edx", "ebx", "ebp", "esi", "edi", "esp")
    s32 = lambda x: (int(x, 16) ^ 0x80000000) - 0x80000000
    names, rows, cur = {}, [], None
    for line in log.splitlines():
        if not line[:2] in ("E ", "X ") or "|" not in line:
            continue
        head, tail = line.split("|", 1)
        f = head.split()
        kind, hook = f[0], int(f[3])
        regs = dict(zip(REG, f[4:]))
        v = re.findall(r'"[^"]*"|\S+', tail)
        if kind == "E" and hook == 0:
            names[regs.get("ebx")] = v[0].strip('"') if v else "?"
        elif kind == "E" and hook == 1:
            dp = int(regs["edx"], 16)
            par = next((r["callee"] for r in reversed(rows) if r["depth"] == dp - 1), None) if dp > 1 else None
            cur = dict(func=("in " + par) if par else names.get(regs.get("ebp"), "?"), callee=v[0].strip('"'), depth=dp,
                       size=int(v[1], 16) if len(v) > 1 and v[1] != "?" else 0)
            rows.append(cur)
        elif kind == "X" and hook == 1 and rows:
            rows[-1]["yes"] = int(f[4], 16) != 0
        elif cur is not None and hook == 2:
            cur["c"] = s32(v[0])
        elif cur is not None and hook == 3:
            cur["d"], cur["dlim"] = s32(v[0]), s32(v[1])
        elif cur is not None and hook == 5:
            cur["B"] = (int(v[0], 16) >> 16) & 0xFF
        elif cur is not None and hook == 4:
            cur["v"], cur["w"], cur["dp"] = s32(v[0]), (int(v[2], 16) << 32) | int(v[1], 16), s32(v[3])
    shown = 0
    print("%-34s %-34s %2s %5s %5s %5s %5s %5s %10s %4s  %s" % ("caller", "callee", "dp", "size", "c", "d", "loop%",
                                                              "d'", "w", "v", "verdict"))
    for r in rows:
        if a.func and a.func not in (r["func"] or ""):
            continue
        if a.callee and a.callee not in r["callee"]:
            continue
        if "c" not in r:
            why = "decided before the cost model"
        elif r["c"] > 450:
            why = "c > 450"
        elif "v" not in r and "d" in r and r["d"] < r["dlim"]:
            why = "d < %d: accepted at once" % r["dlim"]
        elif "v" in r:
            why = "v %s 74" % ("<" if r.get("yes") else ">=")
        else:
            why = "rejected after d (a flag test)"
        if "v" in r and r["w"]:
            D = 7 if r["w"] <= 100000000 else 3
            pv = (r["dp"] * 100000000 // r["w"] + (D - 1) * r["dp"]) // D
            if pv != r["v"]:
                why += "; model gives v=%d: another branch" % pv
        print("%-34s %-34s %2d %5d %5s %5s %5s %5s %10s %4s  %s (%s)" % (
            (r["func"] or "?")[:34], r["callee"][:34], r["depth"], r["size"], r.get("c", ""), r.get("d", ""),
            ("%d" % round(r["B"] * 100 / 255)) if "B" in r else "",
            r.get("dp", ""), r.get("w", ""), r.get("v", ""), "INLINE" if r.get("yes") else "call", why))
        shown += 1
    if not shown:
        print("(no call site matched)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
