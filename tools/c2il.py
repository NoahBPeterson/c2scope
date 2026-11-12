#!/usr/bin/env python3
"""Dump one function's IL at the entry of chosen c2 passes.  Doc: docs/c2/tracer.md (`il`), docs/c2/il_tuple.md

usage: c2trace.py il <src> --func SUBSTR [--at PASS,PASS..] [--grep REGEX] [--count REGEX] [--plain] [-- <cl args>]
       c2trace.py il <src> --func SUBSTR --diff PASS_A,PASS_B[,PASS_C..] [--addr] [-- <cl args>]

Each pass is a c2 function name from work/index/c2.dll.csv (e.g. @Color@4, @FgCodeMotion@4); the default is
the per-function phases of il_pipeline.md §5.3 plus the passes around register allocation. At each pass entry
the tuple stream of the function being compiled (_CurFunc -> FUNC.fg -> first block -> +0x1c) is printed:

  B <bb>                       block start (BB pointer)
  L <tuple> [name]             label (name: user labels `$out`, `__catch$f$0`, `__unwindfunclet$f$2` ...)
  P <tuple> <opcode> <word>    a tuple that is not an instruction: OPENTRY/OPEXIT, pragmas (OPBLKSTART/OPBLKEND
                               scope markers, OPPROLOGEND, OPINLINE, OPUSERLABEL <word> <name> ...); word = tuple
                               +0x20 (a matching OPBLKSTART/OPBLKEND pair carries the same word)
  I <tuple> <opcode> <line> <len> | dsts <- srcs [; notes]   (len = tuple +0x1c byte: the encoded length once
                               EncSetOffsets has run, a sequence number before)
                               operands: names from the SSR, t<key> temps, k<key> immediate candidates,
                               phys=pseudo for allocated registers, [sym+base+index*s+disp] memory, :<type word>
     notes, after " ; ":       cc=<e ne l g le ge b a be ae s ns p np o>   condition of a branch / OPQUESTION
                               sN=.. / dN=..  about the Nth (0-based) src / dst operand as printed:
                                 &            the operand is the ADDRESS of the printed symbol / memory expression
                                 L<tuple>     code address of that label (branch target, jump-table entry)
                                 <name>       code address of that function (call / tail-jump target, &f)
                                 <number>     value of an `fconst`; `0.10000000149011612(0.1f)` = exactly the float 0.1f
                               intrin=<n>     OPINTRINSIC number
                               default=L<tuple> cases=<n>   OPSWITCH; its case list follows as C lines
                               eh{..}         EH record of OPPUSHSTATE / OPPOPSTATE / OPDTORACTION / OPNEWSTATE /
                                              OPCATCH / OPEHTRY (fields: docs/c2/tracer.md)
  C <switch tuple> <low> <high> L<tuple>   one OPSWITCH case: selector values low..high go to that label
  A <line>                     a line (B/L/I/P as above) of the unwind action of the OPPUSHSTATE just above: the
                               destructor call the state stands for (after EH canonicalisation: its funclet)

Only the I lines exist with --plain (the dump as it was before the notes; other c2 builds until their tables say
`%+` in il_spec). --grep keeps only instruction (I, A, C, P) lines whose text matches (plus a per-pass header);
--count prints one line per pass with the number of matching I lines, which is the quickest way to see which pass
changes something. --diff prints, per function, a unified diff of the IL between each consecutive pair of the
listed passes (tuple addresses, line and length fields dropped unless --addr), i.e. what the passes from A up to
(not including) B did.
"""
import argparse, difflib, re, struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace

DEFAULT = ("@dop2_PreLowerGlobalOpts@4 @dop2_Codegen@4 @dop2_PostLowerPreRegAllocGlobalOpts@4 @FgEarlyTailDup@4 "
           "@Globregs@4 @Color@4 @dop2_CodegenUpdate@4 @dop2_PostCodegenFlowOpts@4 @dop2_CodegenFinish@4 "
           "@LateGlobalOptimize@16 @dop2_FinalFlowOpts@4 @FgCodeMotion@4 @FgCoalesceLabels@4 @dop2_PreEncode@4 "
           "@MscClean@4 @MdmSuperHack@4 @dop2_EncodeAndRelated@4 @EncEmitFunc@4").split()
SPEC = "=g10d123ac>0>0>4$,g10d123ac>0>8>0>0x1c%+"       # %+ = the extended dump (c2trace.dll's dumpilx)
DEFAULT = c2trace.c2build.hooks("il_default", DEFAULT)    # another c2 build: its pass names and _CurFunc
SPEC = c2trace.c2build.hooks("il_spec", SPEC)             # (extended only if its table's il_spec ends in %+)
FNOTE = re.compile(r"(?<= )([sd]\d+)=(-?(?:\d[\d.]*(?:e[-+]?\d+)?|1\.#\w+|inf|nan))(?= |$)")


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
        cur = (hooks[hk]["name"], [], name) if c2trace.func_match(funcpat, name) else None
        if cur is not None:
            out.append(cur)
    return out


def _f32(v):
    return struct.unpack("<f", struct.pack("<f", v))[0]


def _fnote(m):
    """Shorten a float-constant note: the shortest text that reads back as the same double, and for a value
    that is exactly a float the shortest float spelling too (0.10000000149011612 is the literal 0.1f)."""
    try:
        v = float(m.group(2))
    except ValueError:
        return m.group(0)
    txt = repr(v)
    try:
        if v == v and abs(v) != float("inf") and _f32(v) == v:
            short = next(s for s in ("%.*g" % (n, v) for n in range(1, 10)) if _f32(float(s)) == v)
            if float(short) != v:
                txt += "(%sf)" % short
    except (OverflowError, StopIteration):
        pass
    return "%s=%s" % (m.group(1), txt)


def pretty(line):
    """Display form of a dump line (only the notes after ' ; ' change)."""
    if " ; " not in line or line[:1] not in "IA":
        return line
    head, _, notes = line.rpartition(" ; ")
    return head + " ; " + FNOTE.sub(_fnote, " " + notes)[1:]


def _diffkey(line, addr):
    """A dump line with what always differs between two passes removed."""
    if addr:
        return line
    m = re.match(r"((?:A )?I) \S+ (\S+) \d+ \d+ \|(.*)$", line)
    if m:
        return "%s %s |%s" % (m.group(1), m.group(2), m.group(3))
    return line


def diff(passes, order, addr):
    """Unified diffs between consecutive passes of `order`, per function (and per occurrence when a function is
    compiled several times: inline expansions, EH funclets)."""
    seen, rc = {}, 0
    for name, lines, fn in passes:
        seen.setdefault(fn, {}).setdefault(name, []).append([_diffkey(pretty(l), addr) for l in lines if not l.startswith(("B ", "A B "))])
    for fn, by in seen.items():
        for a, b in zip(order, order[1:]):
            if a not in by or b not in by:
                print("#### %s: %s -> %s: not seen at %s" % (fn, a, b, " and ".join(p for p in (a, b) if p not in by)))
                continue
            if len(by[a]) != len(by[b]):
                print("#### %s: compiled %d times at %s, %d times at %s; comparing the last of each" % (
                    fn, len(by[a]), a, len(by[b]), b))
                pairs = [(by[a][-1], by[b][-1], "")]
            else:
                pairs = [(x, y, " (#%d)" % (k + 1) if len(by[a]) > 1 else "") for k, (x, y) in enumerate(zip(by[a], by[b]))]
            for x, y, tag in pairs:
                d = [l for l in difflib.unified_diff(x, y, lineterm="", n=1) if not l.startswith(("---", "+++"))]
                print("#### %s%s: %s -> %s: %s" % (fn, tag, a, b, "%d -> %d lines" % (len(x), len(y)) if d else "identical"))
                if d:
                    print("\n".join(d))
                    rc = 1
    return rc


def main(argv):
    ap = argparse.ArgumentParser(prog="c2trace.py il", usage=__doc__.replace("%", "%%"))
    ap.add_argument("--func", required=True)
    ap.add_argument("--at", default=",".join(DEFAULT))
    ap.add_argument("--grep")
    ap.add_argument("--count")
    ap.add_argument("--diff")
    ap.add_argument("--addr", action="store_true")
    ap.add_argument("--plain", action="store_true")
    ap.add_argument("--keep")
    ap.add_argument("src")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    at = [p for p in (a.diff or a.at).split(",") if p]
    if a.diff and len(at) < 2:
        sys.exit("--diff needs at least two passes: --diff PASS_A,PASS_B")
    spec = SPEC[:-1] if a.plain and SPEC.endswith("%+") else SPEC
    img, hk, rc, out, log = c2trace.run_trace(a.src, [p + spec for p in at], cl, keep=a.keep)
    if rc:
        sys.stdout.write(out)
    passes = parse(log, hk, a.func)
    if not passes:
        sys.exit("function %r not seen at any of the passes" % a.func)
    if a.diff:
        diff(passes, [h["name"] for h in hk], a.addr)
        return rc
    for name, lines, fn in passes:
        if a.count:
            n = sum(1 for l in lines if l.startswith("I ") and re.search(a.count, l))
            print("%-44s %4d  (of %d instrs)  %s" % (name, n, sum(1 for l in lines if l.startswith("I ")), fn))
            continue
        print("== %s  %s" % (name, fn))
        for l in lines:
            l = pretty(l)
            if a.grep and not (l[:2] in ("I ", "A ", "C ", "P ") and re.search(a.grep, l)):
                continue
            print("  " + l)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
