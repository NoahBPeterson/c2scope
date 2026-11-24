#!/usr/bin/env python3
"""The /Od frame, live: every automatic's allocation as c2 lays it out (cl 15.00).

usage: c2trace.py odslots SRC --func F [--orig FILE] [-- <cl args>]
       odslots.py --parse LOG [--orig FILE]

At /Od the slot packer (StackPacking) never runs, so c2slots.py's what-if and --search have
nothing to model there (frame.md 4: the packer is on the globopt branch only). The /Od walk is
StkAllocateLocals's first-reference pass over named automatics; this tool traces it live with
the allotemp hook (0x51228020: entry align = eax, size = ecx, exit eax = the offset) and
prints, per allocation in the walk order:

  seq  size x align  offset      (offset goes downward, ebp-N; exit eax)

--orig FILE reads a retail frame map — `offset refs` lines (od_names-style, negative ebp-N) or
the listing form (`[ebp-N]` operands) — and matches ours against it by offset: each retail slot
that ours also produced is a `=`, each slot ours lacks a `<`, each of ours with no retail twin
a `>`. The first mismatching slot is where the frames diverge.

The walk order is the frame.md /Od rule (first-reference order for named automatics, size only
stretches the offsets below a local), so the report is the /Od counterpart of what
c2slots.py --orig gives the /O2 packer: a per-element original/target comparison.
"""
import argparse, os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace

ENTRY = re.compile(r"^E (\d+) (\d+) (\d+) ([0-9a-f]+) ([0-9a-f]+) ([0-9a-f]+) ([0-9a-f]+) "
                   r"([0-9a-f]+) ([0-9a-f]+) ([0-9a-f]+) ([0-9a-f]+) \|( (.*))?$")
#         seq  d    id   eax   ecx   edx   ebx   ebp   esi   edi   esp
XLINE = re.compile(r"^X (\d+) (\d+) (\d+) ([0-9a-f]+) ([0-9a-f]+) \|( (.*))?$")
#         seq  d    id   eax   edx


def parse_log(text):
    """The allotemp log -> [{seq, size, align, offset}]. Entry regs: eax = align, ecx = size
    (verified: a 0xf0 struct logs `8 f0 7b258408`); X's eax = the offset the allocation returned."""
    out, pending = [], None
    for ln in text.splitlines():
        m = ENTRY.match(ln)
        if m:
            pending = dict(seq=int(m.group(1)), size=int(m.group(5), 16),
                           align=int(m.group(4), 16), offset=None)
            continue
        m = XLINE.match(ln)
        if m and pending is not None:
            v = int(m.group(4), 16)
            pending["offset"] = v - 0x100000000 if v >= 0x80000000 else v
            out.append(pending)
            pending = None
    if pending:                      # an allocation whose exit was cut off (--max)
        out.append(pending)
    return out


def parse_orig(path):
    """A retail frame map: `offset refs` lines (od_names/c2slots --orig style, negative ebp-N)
    or the listing form (each frame operand's displacement one per line)."""
    out = []
    for ln in open(path, errors="replace"):
        m = re.match(r"^\s*(-(0x)?[0-9a-f]+)\s+(\d+)\s*refs?", ln, re.I)
        if m:
            out.append(dict(off=int(m.group(1), 0), refs=int(m.group(3))))
            continue
        m = re.match(r"^\s*\[ebp\s*-\s*(0x[0-9a-f]+|\d+)\]", ln, re.I)
        if m:
            out.append(dict(off=-int(m.group(1), 0), refs=None))
    return out


def main(argv):
    argv, cl = c2trace.split_cl(argv)
    ap = argparse.ArgumentParser(prog="odslots.py", usage=__doc__)
    ap.add_argument("src", nargs="?", help="the source to compile and trace")
    ap.add_argument("--func", help="the function (C2TRACE_FUNC's form; default: the only one)")
    ap.add_argument("--orig", help="retail frame map (od_names `offset refs` lines or the listing form)")
    ap.add_argument("--parse", dest="parse", help="re-report from a saved --raw c2trace log")
    a = ap.parse_args(argv)
    if a.parse:
        text = open(a.parse, errors="replace").read()
    elif a.src:
        if not a.func:
            sys.exit("odslots: --func is required when compiling")
        hook = "0x51228020=eax,edx/xeax"
        flags = cl or ["/c", "/Od", "/Ob1", "/MD", "/Gy", "/TP", "/GS-"]
        env = dict(os.environ, C2TRACE_FUNC=a.func)
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                         "c2trace.py"), "--raw", "-H", hook,
                            a.src, "--"] + flags,
                           capture_output=True, text=True, env=env)
        text = r.stdout
        if not text.strip():
            sys.exit("odslots: no trace output; %s" % (r.stderr or "")[-400:])
    else:
        ap.error("need SRC or --parse LOG")
    allocs = parse_log(text)
    if not allocs:
        sys.exit("odslots: no allotemp events found (the hook fires only under /Od; check the flags)")
    print("# %d automatics, walk order (first-reference; frame.md /Od rules):" % len(allocs))
    print("#  seq  size x align   offset")
    for x in allocs:
        print("  %3d  %5d x %-5d  %s" % (x["seq"], x["size"], x["align"],
                                         hex(x["offset"]) if x["offset"] is not None else "?"))
    if a.orig:
        orig = parse_orig(a.orig)
        ours = {x["offset"] for x in allocs if x["offset"] is not None}
        hits = [o for o in orig if o["off"] in ours]
        print("\n# --orig %s: %d retail slots, %d matched, %d ours-only; first retail miss:" %
              (os.path.basename(a.orig), len(orig), len(hits), len(ours) - len(hits)))
        for o in orig:
            hit = "= refs %d" % o["refs"] if o["off"] in ours else "<  (no ours)"
            print("  %8d  %s" % (o["off"], hit))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
