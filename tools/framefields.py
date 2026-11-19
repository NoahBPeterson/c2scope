#!/usr/bin/env python3
"""Field layout of each frame element, ours against the original's: which byte of a local or parameter each aligned
access touches, so that a local whose type differs (another size, a field at another offset) shows as such.

usage: framefields.py <src> --func F (--orig ORIG.s | --orig-src VARIANT.cpp) [--all] [-- <cl args>]

--orig-src compares against another source's compile (two drafts; or a test).

`c2trace.py slots --orig` maps every element to the original's offset by majority vote over its references; an
aggregate whose references vote for several offsets (`_value$ ... 2/6`) is one whose fields sit elsewhere in the
original. This tool lists, per element, every aligned reference as `ours +d -> original +d'` relative to the element's
address (the key of a `lea` of it when there is one, else the shift most of its references agree on), with the instruction, and sums up:
the extent ours and the original's accesses cover, the fields that moved, and stack bytes the original touches that no
element of ours accounts for.

Spore 0x00e2c840 (r2_j.cpp): `_value$ (16 bytes)` ours +8 +0 +4 +12 (zeros) and +8 (k) against the original's +4 +8
+12 +16 and +0: the original's object is 20 bytes, k at +0 and the 16-byte value-init after it, i.e. a
pair<key, FeedbackEvent> (map value_type) built as `FeedbackPair value(k, FeedbackEvent())`, a byte match.
"""
import argparse, collections, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace  # noqa: F401  (build selection)
import c2slots, regmap, tsweep


def our_instrs(asm, func):
    lst = tsweep.listing_instrs(asm, func)
    dec = getattr(lst, "decoded", None)
    addrs = getattr(lst, "addrs", None)
    if not dec:
        sys.exit("no decodable listing for %s" % func)
    cs = [(addrs[k], d[0], d[1]) for k, d in enumerate(dec)]
    return list(lst), cs


def element_rows(s, ours_txt, ours_cs, orig):
    okeys = dict(c2slots.orig_slots(orig)[0])
    mkeys = dict(c2slots.orig_slots(ours_cs)[0])
    a = [regmap.norm(tsweep.canon_listing(*x)) for x in ours_txt]     # capstone's operand order, as orig
    b = [(mn, ops) for _, mn, ops in orig]
    pairs = regmap.align(a, b)
    rows = collections.defaultdict(list)          # element name -> [(our key, orig key, our text, orig text, lea)]
    seen_orig = set()
    for i, j in pairs:
        ns = [m.group(1) for m in c2slots.OPSYM.finditer(a[i][1])]
        ko, kg = mkeys.get(i, []), okeys.get(j, [])
        if not ns or len(ns) != len(ko) or len(ko) != len(kg):
            continue
        for n, x, y in zip(ns, ko, kg):
            rows[n].append((x, y, "%s %s" % ours_txt[i], "%s %s" % b[j], a[i][0] == "lea"))
            seen_orig.add(y)
    allorig = collections.Counter(k for ks in okeys.values() for k in ks)
    return rows, allorig, seen_orig, len(pairs)


def report(s, rows, allorig, seen_orig, npairs, show_all):
    size = {}
    for i, L in s.get("listing", {}).items():
        if L.get("lname"):
            size[L["lname"].lower()] = s["elems"][i]["size"]
    print("# %d aligned instruction pairs; %d elements referenced in both" % (npairs, len(rows)))
    moved = []
    covered = set()
    for n in sorted(rows, key=lambda n: min(r[0] for r in rows[n])):
        rs = rows[n]
        lea = [r for r in rs if r[4]]
        bo = lea[0][0] if lea else min(r[0] for r in rs)
        if lea:
            bg = lea[0][1]
        else:                                     # no address taken in both: the shift most references agree on
            # (the original's accesses all at or above the base, if some shift with agreement allows it)
            sh = collections.Counter(r[1] - r[0] for r in rs).most_common()
            sh.sort(key=lambda c: (-c[1], abs(c[0])))
            fit = [c for c in sh if min(r[1] - (bo + c[0]) for r in rs) >= 0]
            bg = bo + (fit or sh)[0][0]
        d = [(r[0] - bo, r[1] - bg) for r in rs]
        sz = size.get(n, None)
        if sz is not None and sz <= 4 and not show_all:
            continue                              # a scalar: its slot is `slots --orig`'s business
        lim = sz if sz is not None else 1 << 20
        # a pair outside the element on our side, or far outside on the original's, is an alignment or push-depth
        # error, not a field: shown with '?' and left out of the summary
        noise = [not (0 <= x[0] < lim and -8 <= x[1] < lim + 8) for x in d]
        bad = [x for x, z in zip(d, noise) if x[0] != x[1] and not z]
        go = [x[1] for x, z in zip(d, noise) if not z] or [0]
        for k in range(bg + min(go), bg + max(go) + 4):
            covered.add(k)
        if not bad and not show_all:
            continue
        do = [x[0] for x, z in zip(d, noise) if not z] or [0]
        ext_o = (min(do), max(do) + 4)
        ext_g = (min(go), max(go) + 4)
        print("\n## %s (%s bytes ours)%s" % (n, sz if sz is not None else "?", "" if bad else "  fields agree"))
        print("   accesses cover ours +%d..+%d, the original's +%d..+%d (4-byte accesses assumed for the end)" % (
            ext_o[0], ext_o[1] - 1, ext_g[0], ext_g[1] - 1))
        for (x, y), r, z in zip(d, rs, noise):
            print("   ours %+4d -> orig %+4d %s  %-40s | %s" % (x, y, "?" if z else " " if x == y else "*", r[2][:40],
                                                             r[3][:40]))
        same = {x[0] for x, z in zip(d, noise) if not z} == {x[1] for x, z in zip(d, noise) if not z}
        if bad and same:
            print("   the same fields on both sides, paired differently: an order difference (which store comes "
                  "first), not a layout one")
        elif bad:
            moved.append((n, sz, ext_o, ext_g, bad))
    stray = sorted(k for k in allorig if k not in seen_orig and k not in covered and k < 0)
    if moved:
        print("\n## summary")
        for n, sz, eo, eg, bad in moved:
            grow = (eg[1] - eg[0]) - (eo[1] - eo[0])
            print("   %s: %d of its references touch another field in the original; extent %d bytes ours, %d the "
                  "original's%s" % (n, len(bad), eo[1] - eo[0], eg[1] - eg[0],
                                    " (+%d: the original's type is larger, e.g. a key or header field in front)" % grow
                                    if grow > 0 else ""))
            m = collections.Counter(bad)
            print("     field map ours -> original: %s" % ", ".join("+%d->+%d" % k for k in sorted(m)))
    if stray:
        print("\n# original frame keys referenced by no aligned access of ours: %s" % ", ".join("%d" % k for k in stray))
    if not moved:
        print("\n# no layout difference found (every aggregate touches the same fields in both)")


def main(argv):
    ap = argparse.ArgumentParser(prog="framefields.py", usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    ap.add_argument("--orig")
    ap.add_argument("--orig-src")
    ap.add_argument("--all", action="store_true", help="also list the elements whose fields agree")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    info, funcs, asm, d = c2slots.trace(a.src, a.func, cl)
    if not funcs:
        sys.exit("no frame elements for %r (or the compile failed)" % a.func)
    F = funcs[0]
    lst = c2slots.attach_listing(F, c2slots.listing_slots(asm, F["name"])) if os.path.exists(asm) else {}
    s = c2slots.snapshot(F, info, lst)
    ours_txt, ours_cs = our_instrs(asm, F["name"])
    if a.orig_src:
        _, f2, asm2, d2 = c2slots.trace(a.orig_src, a.func, cl)
        if not f2:
            sys.exit("--orig-src: no frame elements for %r" % a.func)
        orig = our_instrs(asm2, f2[0]["name"])[1]
        import shutil
        shutil.rmtree(d2, ignore_errors=True)
    elif a.orig:
        orig = c2slots.read_orig(a.orig)
    else:
        ap.error("need --orig or --orig-src")
    rows, allorig, seen, npairs = element_rows(s, ours_txt, ours_cs, orig)
    print("# %s" % F["name"])
    report(s, rows, allorig, seen, npairs, a.all)
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
