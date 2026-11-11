#!/usr/bin/env python3
"""Which register each of our variables should have, read off the original's code (inverse constraints for rasim).

usage: c2trace.py regmap <src> --func SUBSTR --asm-vs ORIG.s --asm-func DECORATED [--all] [-- <cl args>]

Compiles src with the traced c2 and takes the final IL at @Encode@4, where every allocated operand is written
`reg=var` (e.g. `edi=s386:1004`). Each instruction is placed in our listing by its encoded offset, our listing is
aligned to the original's disassembly (--asm-vs: 'VA mnemonic operands' lines, as tsweep takes), and for every
aligned pair with the same mnemonic and the same number of register operands, the register the original has in
the position where we have `var`'s register is a vote for `var`. Per variable it prints our register(s), the
original's votes, and a verdict:

  ok        every vote agrees with our register
  WANTS r   the votes say r (majority)
  mixed     votes for several registers: a split range, or the alignment is off there

Then it lists swaps (A has what B wants and B has what A wants), which in rasim terms are usually ordering
constraints: of two interfering values with the same candidates, the one coloured first gets the first free
register (regalloc.md section 5). The names are the IL's (`_this`, `s386` = regs' `local386`, `t591`), and the
`regs` names are shown next to them for `rasim need`.
"""
import argparse, collections, difflib, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace, c2scratch, tsweep

REG = tsweep.REG
OPREG = re.compile(r"\b(e[abcd]x|e[sd]i|ebp|[abcd][lhx]|[sd]i|bp)=([^\s:\]]+):")
WIDE = {"al": "eax", "ah": "eax", "ax": "eax", "bl": "ebx", "bh": "ebx", "bx": "ebx", "cl": "ecx", "ch": "ecx",
        "cx": "ecx", "dl": "edx", "dh": "edx", "dx": "edx", "si": "esi", "di": "edi", "bp": "ebp"}


def wide(r):
    return WIDE.get(r, r)


def regs_name(il_name):
    if il_name.startswith("_"):
        return il_name[1:]
    m = re.fullmatch(r"s(\d+)", il_name)
    return "local" + m.group(1) if m else il_name


def final_ops(final):
    """{offset: [(reg, var)]} from the final IL (reg written once per distinct (reg, var) in the tuple)."""
    out, off = {}, 0
    for l in final:
        if not l.startswith("I "):
            continue
        f = l.split()
        pairs = []
        for r, v in OPREG.findall(l.split("|", 1)[1] if "|" in l else ""):
            if (wide(r), v) not in pairs:
                pairs.append((wide(r), v))
        if pairs:
            out.setdefault(off, []).extend(p for p in pairs if p not in out.get(off, []))
        off += int(f[4])
    return out


def shape(ins):
    """Operand shape: registers -> R, memory operands -> M, everything else -> I ('mov M,R')."""
    ops = re.sub(r"\b(dword|word|byte|qword|short)\s+ptr\b|\bshort\b|\boffset\b", "", ins[1].lower())
    ops = re.sub(r"[^,]*\[[^\]]*\][^,]*", "M", ops)
    parts = []
    for o in ops.split(","):
        o = o.strip()
        parts.append("M" if o == "M" else "R" if REG.fullmatch(o) else "I" if o else "")
    return ins[0] + " " + ",".join(parts)


def align(ours, orig):
    """[(our index, orig index)] for aligned instructions with the same mnemonic and operand shape."""
    a = [shape(x) for x in ours]
    b = [shape(y) for y in orig]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    pairs = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            pairs.extend(zip(range(i1, i2), range(j1, j2)))
        elif op == "replace" and i2 - i1 == j2 - j1:
            pairs.extend((i, j) for i, j in zip(range(i1, i2), range(j1, j2)) if a[i] == b[j])
    return pairs


def analyse(final, asm, asm_func, orig):
    ops = final_ops(final)
    lst = tsweep.listing_offsets(asm, asm_func)            # {offset: 'mnemonic operands'}
    offs = sorted(lst)
    ours = [(lst[o].split()[0], lst[o].split(None, 1)[1] if " " in lst[o] else "") for o in offs]
    votes = collections.defaultdict(collections.Counter)
    have = collections.defaultdict(collections.Counter)
    where = collections.defaultdict(list)
    agree = collections.Counter()
    for i, j in align(ours, orig):
        o = offs[i]
        if o not in ops:
            continue
        ra = [wide(r) for r in REG.findall(ours[i][1].lower())]
        rb = [wide(r) for r in REG.findall(orig[j][1].lower())]
        if len(ra) != len(rb):
            continue
        for reg, var in ops[o]:
            have[var][reg] += 1
            for k, r in enumerate(ra):
                if r == reg:
                    votes[var][rb[k]] += 1
                    if rb[k] != reg:
                        where[var].append((o, " ".join(ours[i]), " ".join(orig[j]), reg, rb[k]))
                    else:
                        agree[var] += 1
    return have, votes, where, agree


def report(have, votes, where, agree, show_all):
    """A vote disagrees when the original's register differs from ours at that same instruction (so a name
    shared by several inlined locals, or a split range, is judged instruction by instruction)."""
    rows = []
    for var in sorted(have, key=lambda v: -sum(have[v].values())):
        mine = have[var].most_common()
        vt = votes[var].most_common()
        if not vt:
            continue
        bad = collections.Counter((w[3], w[4]) for w in where[var])
        nbad = sum(bad.values())
        if not nbad:
            verdict = "ok"
        elif len(bad) == 1 and nbad >= agree[var]:
            (fr, to), = bad
            verdict = "WANTS %s" % to if len(mine) == 1 else "WANTS %s for %s" % (to, fr)
        else:
            verdict = "mixed (%d of %d votes differ: %s)" % (nbad, nbad + agree[var],
                                                            " ".join("%s->%s:%d" % (a, b, c) for (a, b), c in bad.most_common(3)))
        rows.append((var, mine, vt, verdict))
    print("%-12s %-14s %-24s %-28s %s" % ("var (IL)", "regs name", "ours", "original's votes", "verdict"))
    for var, mine, vt, verdict in rows:
        if verdict == "ok" and not show_all:
            continue
        print("%-12s %-14s %-24s %-28s %s" % (var, regs_name(var), " ".join("%s:%d" % x for x in mine),
                                              " ".join("%s:%d" % x for x in vt), verdict))
        for o, a, b, _, _ in where[var][:3] if verdict != "ok" else []:
            print("      %05x  ours %-36s orig %s" % (o, a[:36], b))
    nok = sum(1 for r in rows if r[3] == "ok")
    print("# %d variables checked, %d agree with the original everywhere" % (len(rows), nok))
    wants = {r[0]: (r[1][0][0], r[3].split()[1]) for r in rows if r[3].startswith("WANTS") and len(r[1]) == 1}
    swaps = [(a, b) for a in wants for b in wants if a < b and wants[a][0] == wants[b][1] and wants[b][0] == wants[a][1]]
    for a, b in swaps:
        print("swap: %s (%s, wants %s) <-> %s (%s, wants %s): try rasim need ... '%s>%s' or '%s>%s'" % (
            a, wants[a][0], wants[a][1], b, wants[b][0], wants[b][1], regs_name(a), regs_name(b), regs_name(b),
            regs_name(a)))
    return rows


def main(argv):
    ap = argparse.ArgumentParser(prog="c2trace.py regmap", usage=__doc__)
    ap.add_argument("--func", required=True)
    ap.add_argument("--asm-vs", required=True)
    ap.add_argument("--asm-func", required=True)
    ap.add_argument("--all", action="store_true", help="also list the variables that agree")
    ap.add_argument("--keep")
    ap.add_argument("src")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    import tempfile
    keep = a.keep or tempfile.mkdtemp(prefix="regmap_")
    _, _, rc, out, log = c2trace.run_trace(a.src, c2scratch.HOOKS, cl, keep=keep, quiet=True)
    picks, final = c2scratch.parse(log, a.func)
    if not final:
        sys.exit("no final IL for %r (compile error?)\n%s" % (a.func, out[-500:]))
    asm = os.path.join(keep, os.path.splitext(os.path.basename(a.src))[0] + ".asm")
    have, votes, where, agree = analyse(final, asm, a.asm_func, tsweep.orig_instrs(a.asm_vs))
    report(have, votes, where, agree, a.all)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
