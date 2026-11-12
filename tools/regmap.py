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

Every register class the final IL names is covered: integer rows first (the format `rasim want` reads), then XMM
(Color class 6), MMX, and x87. For x87 the "register" is the stack position ST(i) the value has at each aligned
instruction (the final IL writes `ST(2)=var`), so a row says whether the original's stack has the value at the same
depth there; it is a layout check, not a colour to ask rasim for. Each non-integer class starts with a `#` line.

Then it lists swaps (A has what B wants and B has what A wants), which in rasim terms are usually ordering
constraints: of two interfering values with the same candidates, the one coloured first gets the first free
register (regalloc.md section 5). The names are the IL's (`_this`, `s386` = regs' `local386`, `t591`), and the
`regs` names are shown next to them for `rasim need`.
"""
import argparse, collections, difflib, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace, c2scratch, tsweep

# Every register class the final IL names: int, XMM (15.00 also writes the scalar views xmm0s/xmm0d/xmm0l), MMX and
# the x87 stack positions ST(i). tsweep.REG (regdiff's key) is left as it is; this is regmap's own superset.
# (tsweep.REG's `st\(\d\)` alternative never matches: a `\b` cannot follow the closing parenthesis.)
REG = re.compile(r"\b(?:e?[abcd]x|[abcd][lh]|e?si|e?di|e?bp|e?sp|xmm[0-7]|mm[0-7])\b|\bst\(\d\)")
OPREG = re.compile(r"\b(e[abcd]x|e[sd]i|ebp|[abcd][lhx]|[sd]i|bp|xmm[0-7][sdl]?|mm[0-7]|ST\(\d\))=([^\s:\]]+):")
WIDE = {"al": "eax", "ah": "eax", "ax": "eax", "bl": "ebx", "bh": "ebx", "bx": "ebx", "cl": "ecx", "ch": "ecx",
        "cx": "ecx", "dl": "edx", "dh": "edx", "dx": "edx", "si": "esi", "di": "edi", "bp": "ebp"}
CLASSES = ["int", "xmm", "mmx", "x87"]
CLASS_NOTE = {
    "xmm": ("XMM values (Color class 6)", ""),
    "mmx": ("MMX values (Color class 5)", ""),
    "x87": ("x87 values, by stack position",
            "#   the columns are the positions ST(i) a value has at each aligned instruction, not colours; a difference "
            "means the\n#   original's stack holds other values, or another order, at that point (x87.md rules 5, 8, "
            "11; `regs --all-classes`)\n"),
}


def wide(r):
    r = r.lower()
    if r.startswith("xmm"):
        return r[:4]
    return WIDE.get(r, r)


def rclass(r):
    return "xmm" if r.startswith("xmm") else "mmx" if r.startswith("mm") else "x87" if r.startswith("st(") else "int"


X87_TWO = re.compile(r"^\s*(st\(\d\))\s*,\s*(st\(\d\))\s*$")


def norm(ins):
    """(mnemonic, operands) in lower case. An x87 instruction with two stack operands keeps only the one that is not
    ST(0): MASM writes `faddp ST(1), ST(0)` / `fmul ST(0), ST(2)`, capstone `faddp st(1)` / `fmul st(2)`."""
    mn, ops = ins[0].lower(), ins[1].lower()
    if mn.startswith("f"):
        m = X87_TWO.match(ops)
        if m:
            a, b = m.groups()
            ops = b if a == "st(0)" else a
    return (mn, ops)


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
            if (wide(r), v) not in pairs and not re.fullmatch(r"ST\(\d\)", v):     # 13.10 writes `ST(1)=ST(0)`
                pairs.append((wide(r), v))
        if pairs:
            out.setdefault(off, []).extend(p for p in pairs if p not in out.get(off, []))
        off += int(f[4])
    return out


def shape(ins):
    """Operand shape: registers -> R, memory operands -> M, everything else -> I ('mov M,R')."""
    ops = re.sub(r"\b(xmmword|mmword|tbyte|dword|qword|word|byte|short)\s+ptr\b|\bshort\b|\boffset\b", "", ins[1].lower())
    ops = re.sub(r"[^,]*\[[^\]]*\][^,]*", "M", ops)
    # A global without brackets in our listing (`DWORD PTR _g`, `__real@3f80..`) stays "I" although capstone writes
    # the original's as [0x..] = "M": calling it M aligned fewer instructions on the one large function tried
    # (Spore 0x00f7d9d0: 1800 against 2068 of 8379), so it is left as it was.
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
    """Votes per (variable, register class): a name can be an int value in one place and an XMM value in another
    (inlined locals), and the classes never compete for a register."""
    ops = final_ops(final)
    lst = tsweep.listing_offsets(asm, asm_func)            # {offset: 'mnemonic operands'}
    offs = sorted(lst)
    shown = [lst[o] for o in offs]
    ours = [norm((lst[o].split()[0], lst[o].split(None, 1)[1] if " " in lst[o] else "")) for o in offs]
    orig_shown = [" ".join(x) for x in orig]
    orig = [norm(x) for x in orig]
    votes = collections.defaultdict(collections.Counter)
    have = collections.defaultdict(collections.Counter)
    where = collections.defaultdict(list)
    agree = collections.Counter()
    for i, j in align(ours, orig):
        o = offs[i]
        if o not in ops:
            continue
        ra = [wide(r) for r in REG.findall(ours[i][1])]
        rb = [wide(r) for r in REG.findall(orig[j][1])]
        if len(ra) != len(rb):
            continue
        for reg, var in ops[o]:
            key = (var, rclass(reg))
            hit = False
            for k, r in enumerate(ra):
                if r == reg and rclass(rb[k]) == key[1]:
                    hit = True
                    votes[key][rb[k]] += 1
                    if rb[k] != reg:
                        where[key].append((o, shown[i], orig_shown[j], reg, rb[k]))
                    else:
                        agree[key] += 1
            if hit or key[1] != "x87":      # an x87 operand the listing leaves implicit (ST(0)) is not a sighting
                have[key][reg] += 1
    return have, votes, where, agree


def report(have, votes, where, agree, show_all):
    """A vote disagrees when the original's register differs from ours at that same instruction (so a name
    shared by several inlined locals, or a split range, is judged instruction by instruction). Integer rows come
    first, in the format `rasim want` reads; the other classes follow, each under a `#` header line."""
    rows = []
    for key in sorted(have, key=lambda v: (CLASSES.index(v[1]), -sum(have[v].values()))):
        var, cls = key
        mine = have[key].most_common()
        vt = votes[key].most_common()
        if not vt:
            continue
        bad = collections.Counter((w[3], w[4]) for w in where[key])
        nbad = sum(bad.values())
        if not nbad:
            verdict = "ok"
        elif len(bad) == 1 and nbad >= agree[key]:
            (fr, to), = bad
            verdict = "WANTS %s" % to if len(mine) == 1 else "WANTS %s for %s" % (to, fr)
        else:
            verdict = "mixed (%d of %d votes differ: %s)" % (nbad, nbad + agree[key],
                                                            " ".join("%s->%s:%d" % (a, b, c) for (a, b), c in bad.most_common(3)))
        rows.append((var, mine, vt, verdict, cls))
    print("%-12s %-14s %-24s %-28s %s" % ("var (IL)", "regs name", "ours", "original's votes", "verdict"))
    cur = "int"
    for var, mine, vt, verdict, cls in rows:
        if cls != cur:
            cur = cls
            n = [r for r in rows if r[4] == cls]
            sys.stdout.write("# %s: %d checked, %d agree everywhere\n%s" % (
                CLASS_NOTE[cls][0], len(n), sum(1 for r in n if r[3] == "ok"), CLASS_NOTE[cls][1]))
        if verdict == "ok" and not show_all:
            continue
        print("%-12s %-14s %-24s %-28s %s" % (var, regs_name(var), " ".join("%s:%d" % x for x in mine),
                                              " ".join("%s:%d" % x for x in vt), verdict))
        for o, a, b, _, _ in where[(var, cls)][:3] if verdict != "ok" else []:
            print("      %05x  ours %-36s orig %s" % (o, a[:36], b))
    nok = sum(1 for r in rows if r[3] == "ok")
    print("# %d variables checked, %d agree with the original everywhere" % (len(rows), nok))
    per = ["%s %d of %d" % (c, sum(1 for r in rows if r[4] == c and r[3] == "ok"), sum(1 for r in rows if r[4] == c))
           for c in CLASSES if any(r[4] == c for r in rows)]
    if len(per) > 1 or (per and not per[0].startswith("int")):
        print("# by class: " + ", ".join(per))
    wants = {(r[0], r[4]): (r[1][0][0], r[3].split()[1]) for r in rows
             if r[3].startswith("WANTS") and len(r[1]) == 1 and r[4] != "x87"}
    swaps = [(a, b) for a in wants for b in wants if a < b and a[1] == b[1] and wants[a][0] == wants[b][1]
             and wants[b][0] == wants[a][1]]
    for cls in CLASSES[1:]:                 # a large SSE function has thousands of XMM pairs: list the first few
        n = [p for p in swaps if p[0][1] == cls]
        if len(n) > 12:
            print("# %s: %d swap pairs, the first 12 listed" % (cls, len(n)))
            swaps = [p for p in swaps if p[0][1] != cls] + n[:12]
    for a, b in swaps:
        print("swap: %s (%s, wants %s) <-> %s (%s, wants %s): try rasim need ... '%s>%s' or '%s>%s'" % (
            a[0], wants[a][0], wants[a][1], b[0], wants[b][0], wants[b][1], regs_name(a[0]), regs_name(b[0]),
            regs_name(b[0]), regs_name(a[0])))
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
