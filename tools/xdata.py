#!/usr/bin/env python3
"""The image's unwind and ICF state: is a body's .pdata chained (a /GL+/LTCG link), and how many
bodies are byte-identical twins?  (2026-11-23)

usage: xdata.py --image EXE [--va VA] [--icf [N]] [--sample N] [--band VA]

--va VA        the .pdata fragments of one function: begin, size, unwind RVA, and for every fragment
               after the first whether its UNWIND_INFO carries UNW_FLAG_CHAININFO (0x4) and which
               entry it chains to. One .pdata entry per function is plain `cl /O2`; ten chained
               fragments for one logical body is link-time codegen layout — invisible to a
               cl-object byte compare.
--icf N        the /OPT:ICF merge rate: N random bodies, how many share an identical 64-byte prefix
               with an earlier one. (A byte-compare target that is an ICF twin of other bodies can
               match any of them; the pack's twins tooling exploits exactly this.)
--sample N     the random sample size (default 17377).
--band VA      only count .pdata entries whose body begins in the 1 MB band at VA (the same TU).

Offline: pefile + struct only, no capstone, no trace. Measured on a retail x64 game
executable (2026-11-23): 291,114 entries (54,325 bodies < 0x20), 10 chained fragments for one function (every
one after the first UNW_FLAG_CHAININFO, back to the main fragment's unwind), and 9.2% identical-prefix twins in a
17,377-body sample (an earlier count with another entry filter and hash length: 291,080 / 54,291 / 9.3%).
"""
import argparse, os, random, struct, sys

import pefile

RT = struct.Struct("<III")
CHAIN = 0x4                              # UNW_FLAG_CHAININFO, UNWIND_INFO's flags nibble


def entries(pd):
    """The .pdata section's RUNTIME_FUNCTION list [(begin, end, unwind)]."""
    n = len(pd) // RT.size
    return [RT.unpack_from(pd, i * RT.size) for i in range(n)]


def chain_target(mm, u):
    """The body a CHAININFO unwind chains to (the first RUNTIME_FUNCTION's begin RVA), or None.
    UNWIND_INFO byte 0: Version:3 low, Flags:5 high (CHAININFO = 0x4); byte 2 = CountOfUnwindCodes."""
    b = mm[u]
    ver, flags = b & 0x7, b >> 3
    if not flags & CHAIN:
        return None
    off = 4 + 2 * mm[u + 2]              # after the unwind codes
    if flags & 0x3:                      # an exception handler's data comes first
        off += 4
    off = (off + 3) & ~3                 # the payload is aligned
    return RT.unpack_from(mm, u + off)[0]


def body_fragments(ents, va):
    """A body's .pdata fragments: chained unwind tiles [va..next), found by walking begin->end. Plain
    cl /O2 gives one fragment; a /GL+LTCG link tiles one logical body into several, each chained."""
    out, cur = [], va
    by_begin = {}
    for e in ents:
        if e[0] not in by_begin:
            by_begin[e[0]] = e
    while cur in by_begin:
        e = by_begin[cur]
        out.append(e)
        cur = e[1]
    return out


def icf_twins(mm, ents, sample):
    """N random bodies; how many begin with an earlier body's exact 64-byte prefix."""
    random.seed(7)
    pick = random.sample(range(len(ents)), min(sample, len(ents)))
    seen, twins = {}, 0
    for i in pick:
        b, e = ents[i][0], ents[i][1]
        k = bytes(mm[b:min(b + 0x40, e)])
        if k in seen:
            twins += 1
        else:
            seen[k] = i
    return len(pick), twins


def main(argv):
    ap = argparse.ArgumentParser(prog="xdata.py", usage=__doc__)
    ap.add_argument("--image", required=True, help="the PE (the plain game exe)")
    ap.add_argument("--va", type=lambda x: int(x, 0), help="a function VA: print its .pdata fragments")
    ap.add_argument("--icf", type=int, nargs="?", const=17377, metavar="N",
                    help="also measure the ICF twin rate on N bodies (default 17377)")
    ap.add_argument("--sample", type=int, default=17377, help="ICF sample size (default 17377)")
    ap.add_argument("--band", type=lambda x: int(x, 0), help="restrict every count to this 1 MB band")
    a = ap.parse_args()
    if not os.path.exists(a.image):
        sys.exit("xdata: no such image %s" % a.image)
    pe = pefile.PE(a.image, fast_load=True)
    pe.parse_data_directories()
    image = pe.OPTIONAL_HEADER.ImageBase
    try:
        pd = next(s for s in pe.sections if s.Name.rstrip(b"\0") == b".pdata").get_data()
    except StopIteration:
        sys.exit("xdata: %s has no .pdata section" % a.image)
    ents = entries(pd)
    if a.band:
        lo = a.band - image
        ents = [e for e in ents if lo <= e[0] < lo + 0x100000]
    tiny = sum(1 for b, e, _u in ents if 0 < e - b < 0x20)
    print("%s: %d .pdata entries%s, %d bodies smaller than 0x20 bytes" % (
        os.path.basename(a.image), len(ents),
        " in the band at 0x%x" % a.band if a.band else "", tiny))
    mm = pe.get_memory_mapped_image()
    if a.va:
        va = a.va - image
        frags = body_fragments(ents, va)
        if not frags:
            sys.exit("xdata: no .pdata entry begins at 0x%x" % a.va)
        print("body 0x%x: %d .pdata fragment(s), main unwind 0x%x:" % (a.va, len(frags), frags[0][2] + image))
        for b, e, u in frags:
            t = chain_target(mm, u)
            mark = "" if b == va else ("  CHAININFO -> 0x%x" % (t + image) if t is not None else "")
            print("  begin 0x%-9x size 0x%-5x unwind 0x%-10x%s" % (b + image, e - b, u + image, mark))
        if len(frags) > 1:
            print("  (%d fragments for one logical body: a link-time-codegen layout; plain cl /O2 "
                  "emits one, and the byte compare never sees any of it)" % len(frags))
        else:
            print("  (one fragment: the plain cl /O2 layout)")
    if a.icf:
        n, twins = icf_twins(mm, ents, a.icf)
        print("ICF twins: %d of %d sampled bodies share an identical 64-byte prefix with an earlier "
              "one (%.1f%%)" % (twins, n, 100.0 * twins / n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
