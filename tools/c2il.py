#!/usr/bin/env python3
"""Dump one function's IL at the entry of chosen c2 passes.  Doc: docs/c2/tracer.md (`il`), docs/c2/il_tuple.md

usage: c2trace.py il <src> --func SUBSTR [--at PASS,PASS..] [--grep REGEX] [--count REGEX] [-- <cl args>]

Each pass is a c2 function name from work/index/c2.dll.csv (e.g. @Color@4, @FgCodeMotion@4); the default is
the per-function phases of il_pipeline.md §5.3 plus the passes around register allocation. At each pass entry
the tuple stream of the function being compiled (_CurFunc -> FUNC.fg -> first block -> +0x1c) is printed:

  B <bb>                       block start (BB pointer)
  L <tuple>                    label
  I <tuple> <opcode> <line> <len> | dsts <- srcs   (len = tuple +0x1c byte: the encoded length once
                               EncSetOffsets has run, a sequence number before)
                               operands: names from the SSR, t<key> temps, k<key> immediate candidates,
                               phys=pseudo for allocated registers, [sym+base+index*s+disp] memory, :<type word>

--grep keeps only instructions whose text matches (plus a per-pass header); --count prints one line per pass with
the number of matching instructions, which is the quickest way to see which pass changes something.
"""
import argparse, re, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace

DEFAULT = ("@dop2_PreLowerGlobalOpts@4 @dop2_Codegen@4 @dop2_PostLowerPreRegAllocGlobalOpts@4 @FgEarlyTailDup@4 "
           "@Globregs@4 @Color@4 @dop2_CodegenUpdate@4 @dop2_PostCodegenFlowOpts@4 @dop2_CodegenFinish@4 "
           "@LateGlobalOptimize@16 @dop2_FinalFlowOpts@4 @FgCodeMotion@4 @FgCoalesceLabels@4 @dop2_PreEncode@4 "
           "@MscClean@4 @MdmSuperHack@4 @dop2_EncodeAndRelated@4 @EncEmitFunc@4").split()
SPEC = "=g10d123ac>0>0>4$,g10d123ac>0>8>0>0x1c%"


def parse(log, hooks, funcpat):
    """-> [(pass name, [IL lines])] for the matching function, in event order."""
    out, cur = [], None
    for ln in log.splitlines():
        if ln.startswith("T "):
            if cur is not None:
                cur[1].append(ln[2:])
            continue
        if not ln.startswith("E "):
            continue
        head, _, rest = ln.partition("|")
        hk = int(head.split()[3])
        name = rest.split('"')[1] if '"' in rest else "?"
        cur = (hooks[hk]["name"], [], name) if funcpat in name else None
        if cur is not None:
            out.append(cur)
    return out


def main(argv):
    ap = argparse.ArgumentParser(prog="c2trace.py il", usage=__doc__)
    ap.add_argument("--func", required=True)
    ap.add_argument("--at", default=",".join(DEFAULT))
    ap.add_argument("--grep")
    ap.add_argument("--count")
    ap.add_argument("--keep")
    ap.add_argument("src")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    hooks = [p + SPEC for p in a.at.split(",") if p]
    img, hk, rc, out, log = c2trace.run_trace(a.src, hooks, cl, keep=a.keep)
    if rc:
        sys.stdout.write(out)
    passes = parse(log, hk, a.func)
    if not passes:
        sys.exit("function %r not seen at any of the passes" % a.func)
    for name, lines, fn in passes:
        if a.count:
            n = sum(1 for l in lines if l.startswith("I ") and re.search(a.count, l))
            print("%-44s %4d  (of %d instrs)  %s" % (name, n, sum(1 for l in lines if l.startswith("I ")), fn))
            continue
        print("== %s  %s" % (name, fn))
        for l in lines:
            if a.grep and not (l.startswith("I ") and re.search(a.grep, l)):
                continue
            print("  " + l)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
