#!/usr/bin/env python3
"""Rank candidate /Od slot names from a cl /FAs listing (vs2005-cpp's odslots companion).

The /Od walk orders named automatics by a name hash (frame.md's /Od rules). This tool
reads the list of `_name$ = -offset` lines a cl /FAs listing emits for a compiled probe
(one flat function holding every candidate as `volatile int NAME = distinct const`),
sorts by offset, and prints the ranks: rank 1 = the highest offset (the ebp-4 side,
allocated first). The draft's working names go in as anchors so ranks are directly
comparable to od_name_rank.json's pool.

usage: od_rankf.py LISTING.ASM --names NAMES.txt [--json OUT.json]
       (the listing comes from `c2trace.py --keep D SRC -- /c /FAsc` or plain cl /Fa)
"""
import argparse, json, re, sys


def ranks(listing, func):
    """{name: offset} for the function's locals, from the listing's `_name$ = -N` lines
    (they sit between _TEXT SEGMENT and the PROC line, one per automatic, /FAs form)."""
    lines = open(listing, errors="replace").read().splitlines()
    # the assignments precede the PROC: bound them by the segment opener and the PROC
    starts = [i for i, l in enumerate(lines) if l.startswith("_TEXT\tSEGMENT")]
    proc = next((i for i, l in enumerate(lines) if re.match(r"\?\w+@@.*PROC", l)), None)
    lo = starts[0] if starts else 0
    hi = proc if proc is not None else len(lines)
    # a COMDAT listing can hold several functions: prefer the segment that names `func`
    seg = None
    for i, l in enumerate(lines):
        m = re.match(r"\?(\w+)@@", l)
        if m and (not func or m.group(1) == func):
            seg = i
            break
    if seg is not None:
        hi = seg
    offs = {}
    for ln in lines[lo:hi]:
        m = re.match(r"_([A-Za-z_]\w*)\$ = (-\d+)", ln)
        if m:
            offs[m.group(1)] = int(m.group(2))
    return offs


def main(argv):
    ap = argparse.ArgumentParser(prog="od_rankf.py", usage=__doc__)
    ap.add_argument("listing")
    ap.add_argument("--names", required=True, help="the probe's name list, one per line")
    ap.add_argument("--func", help="restrict to this function's COMDAT (when several)")
    ap.add_argument("--json", help="also write {name: offset} here")
    a = ap.parse_args(argv)
    want = open(a.names).read().split()
    offs = ranks(a.listing, a.func)
    missing = [n for n in want if n not in offs]
    if missing:
        sys.exit("od_rankf: not in the listing's frame (check --func): " + " ".join(missing))
    order = sorted(((offs[n], n) for n in want), reverse=True)   # first-allocated first
    for rank, (off, n) in enumerate(order, 1):
        print("  %2d  %-9s %5d" % (rank, n, off))
    if a.json:
        json.dump({n: off for off, n in order}, open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
