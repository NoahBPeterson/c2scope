#!/usr/bin/env python3
"""Which register each of our variables should have, read off the original's code (inverse constraints for rasim).

usage: c2trace.py regmap <src> --func SUBSTR --asm-vs ORIG.s --asm-func DECORATED [--all] [-- <cl args>]

Compiles src with the traced c2 and takes the final IL at @Encode@4, where every allocated operand is written
`reg=var` (e.g. `edi=s386:1004`). Each instruction is placed in our listing by its encoded offset, our listing is
aligned to the original's disassembly (--asm-vs: 'VA mnemonic operands' lines, as tsweep takes; our side is read as
capstone decodes the listing's code bytes, so operand order is the original's: tsweep.cs_text), and for every
aligned pair with the same mnemonic and the same number of register operands, the register the original has in
the position where we have `var`'s register is a vote for `var`. Per variable it prints our register(s), the
original's votes, and a verdict:

  ok        every vote agrees with our register
  WANTS r   the votes say r (majority)
  mixed     votes for several registers: a split range, or the alignment is off there

Two kinds of difference are not counted as register votes and are reported on their own `#` lines: an address
with the same registers in the other base/index order (`[edx + edi]` against our `[edi + edx]`: the address's
operand order), and two nearby instructions (at most four aligned pairs apart) that the original issues the other
way round (scheduling); the latter are paired crosswise, so their registers are compared with each other.

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


def final_dsts(final):
    """{offset: set of the variables the instruction writes} (the `reg=var` operands left of `<-` in the final IL)."""
    out, off = {}, 0
    for l in final:
        if not l.startswith("I "):
            continue
        f = l.split()
        body = l.split("|", 1)[1] if "|" in l else ""
        out.setdefault(off, set()).update(v for _r, v in OPREG.findall(body.split("<-", 1)[0]))
        off += int(f[4])
    return out


# Two-address ops whose two register operands can be written in either order for the same value: `add ebp, eax`
# and `add eax, ebp` both compute ebp + eax; only where the result goes differs. regmap votes on them by role: the
# result (the destination's value) by the destination's position; a source by its own register when the original
# has it in either position (the other order of the same operands), else by position.
COMMUTATIVE = {"add", "adc", "and", "or", "xor", "imul", "test"}


def shape(ins):
    """Operand shape: registers -> R, memory operands -> M, everything else -> I ('mov M,R')."""
    ops = re.sub(r"\b(xmmword|mmword|tbyte|dword|qword|word|byte|short)\s+ptr\b|\bshort\b|\boffset\b", "", ins[1].lower())
    ops = re.sub(r"[^,]*\[[^\]]*\][^,]*", "M", ops)
    # A global without brackets in listing text (`DWORD PTR _g`, `__real@3f80..`) stays "I" although capstone writes
    # the original's as [0x..] = "M": calling it M aligned fewer instructions on the one large function tried
    # (Spore 0x00f7d9d0: 1800 against 2068 of 8379), so it is left as it was. analyse reads our side as capstone
    # text of the listing's bytes (tsweep.listing_cs_offsets), where a global is [0] = "M" on both sides.
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


MEM = re.compile(r"\[([^\]]*)\]")


def addr_order(a_ops, b_ops, rb):
    """The original's register list with each memory operand's registers put in our order when the two name the same
    registers in another order (`[edx + edi + 0x30]` against our `[edi + edx + 0x30]`): which value is the base and
    which the index is the IL's operand order (the CSE temp of the address), not a register choice. Returns
    (rb, registers whose vote was an order difference)."""
    ma, mb = MEM.findall(a_ops.lower()), MEM.findall(b_ops.lower())
    if len(ma) != len(mb) or not ma:
        return rb, []
    out, flipped = list(rb), []
    for x, y in zip(ma, mb):
        ra = [wide(r) for r in REG.findall(x)]
        ry = [wide(r) for r in REG.findall(y)]
        if ra == ry or sorted(ra) != sorted(ry):
            continue
        # position of this operand's registers in the whole list: the registers before the bracket
        pre = len(REG.findall(b_ops.lower()[:b_ops.lower().index("[" + y + "]")]))
        out[pre:pre + len(ry)] = ra
        flipped += [r for r, s in zip(ra, ry) if r != s]
    return out, flipped


def reorder(pairs, ours, orig, swapped, window=4):
    """Two aligned pairs at most `window` pairs apart whose instructions are each other's (ours i = orig j2 and ours
    i2 = orig j, the same text with registers; other instructions, e.g. x87 ones, may sit between them): the same
    two instructions issued in the other order, a scheduling difference. Paired crosswise so that their registers
    vote as equal; each such pair is recorded in `swapped`."""
    pairs = list(pairs)
    used = set()
    for k in range(len(pairs)):
        i, j = pairs[k]
        if k in used or ours[i] == orig[j]:
            continue
        for k2 in range(k + 1, min(len(pairs), k + 1 + window)):
            i2, j2 = pairs[k2]
            if k2 not in used and ours[i2] != orig[j2] and ours[i] == orig[j2] and ours[i2] == orig[j]:
                pairs[k], pairs[k2] = (i, j2), (i2, j)
                used.update((k, k2))
                swapped.append(i)
                break
    return pairs


def analyse(final, asm, asm_func, orig):
    """Votes per (variable, register class): a name can be an int value in one place and an XMM value in another
    (inlined locals), and the classes never compete for a register."""
    ops = final_ops(final)
    dsts = final_dsts(final)
    lst = tsweep.listing_cs_offsets(asm, asm_func)         # {offset: 'mnemonic operands'}, capstone of our bytes:
    # the listing's own text has `test reg, r/m` where orig.s (capstone) has `test r/m, reg` (false WANTS)
    offs = sorted(lst)
    shown = [lst[o] for o in offs]
    ours = [norm((lst[o].split()[0], lst[o].split(None, 1)[1] if " " in lst[o] else "")) for o in offs]
    orig_shown = [" ".join(x) for x in orig]
    orig = [norm(x) for x in orig]
    votes = collections.defaultdict(collections.Counter)
    have = collections.defaultdict(collections.Counter)
    where = collections.defaultdict(list)
    agree = collections.Counter()
    order = collections.Counter()           # result votes on a commutative op with the same operands, other order
    notes = dict(addr=collections.Counter(), swapped=[])
    for i, j in reorder(align(ours, orig), ours, orig, notes["swapped"]):
        o = offs[i]
        if o not in ops:
            continue
        ra = [wide(r) for r in REG.findall(ours[i][1])]
        rb = [wide(r) for r in REG.findall(orig[j][1])]
        if len(ra) != len(rb):
            continue
        rb, flipped = addr_order(ours[i][1], orig[j][1], rb)
        for r in flipped:
            for reg, var in ops[o]:
                if reg == r:
                    notes["addr"][var] += 1
        comm = ours[i][0] in COMMUTATIVE and shape(ours[i]) == ours[i][0] + " R,R" and shape(orig[j]) == orig[j][0] + " R,R"
        for reg, var in ops[o]:
            key = (var, rclass(reg))
            hit = False
            for k, r in enumerate(ra):
                if r == reg and rclass(rb[k]) == key[1]:
                    hit = True
                    vote = rb[k]
                    if comm and var not in dsts.get(o, ()) and rb[1 - k] == reg:
                        vote = reg              # a source the original has in the other position: same operands
                    elif comm and vote != reg and sorted(ra) == sorted(rb):
                        order[key] += 1         # the result went to the other operand's register
                    votes[key][vote] += 1
                    if vote != reg:
                        where[key].append((o, shown[i], orig_shown[j], reg, vote))
                    else:
                        agree[key] += 1
                    if comm:
                        break                   # `add eax=t2 <- eax=t1 ebp`: one vote per role, not per position
            if hit or key[1] != "x87":      # an x87 operand the listing leaves implicit (ST(0)) is not a sighting
                have[key][reg] += 1
    return have, votes, where, agree, order, notes


def sources(log, func, nhooks):
    """{IL temp or variable name: where its register comes from} for the function's body run: RegAssign picks (by
    their temp and their IL name at pick time), Color pops, Riscify's GetReg calls (cl 13.10) and, for the other
    temps of the final code, the phase that gave the register (c2scratch.origins). nhooks: c2scratch.HOOKS' length.
    Also returns the final IL."""
    oh = c2scratch.origin_hooks()
    segs, final = c2scratch.parse_segments(log, func, cbase=nhooks, obase=nhooks + len(c2scratch._color_hooks()),
                                           oroles=[r for r, _ in oh])
    segs = [x for x in segs if x["picks"]] or segs[:1]
    if not segs:
        return {}, final
    body = max(segs, key=lambda x: len(x["picks"]))
    out = {}
    for nm in body.get("color") or {}:
        out["_" + nm] = out[nm] = "Color (a globreg: `regs --why`, `rasim want` on a regs --export snapshot)"
    named = set()
    for p in body["picks"]:
        d = "scratch pick %d (RegAssign: %s; `rasim scratch` on a scratch --export snapshot, want:%%s=REG)" % (
            p["n"], "rotation" if p["rot"] else "preferred register" if p["pref"] and p["pref"] == p["reg"] else "first fit")
        m = re.match(r"\s*(t\d+):", c2scratch._instr(p)[2])
        for t in ["t%d" % p["tmp"] if p["tmp"] is not None else None, m.group(1) if m else None]:
            if t:
                out.setdefault(t, d)
                named.add(t)
    rc = c2scratch.riscify_calls(body, c2scratch.offsets(final))
    for c in rc:
        for t in c["made"]:
            out.setdefault(t, "Riscify, GetReg call R%d (cl 13.10, after RegSimpleAssign; `rasim scratch` on a scratch "
                              "--export snapshot, want:%%s=REG)" % c["n"])
    for t, _r, _o, ph, _i in c2scratch.origins(body, final, named, rc):
        out.setdefault(t, ph + " (not modelled by rasim)")
    return out, final


def report(have, votes, where, agree, show_all, src=None, order=None, notes=None):
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
        if order and order.get((var, cls)) and verdict != "ok":
            print("      note: %d of these votes are a commutative two-address op with the same operands in the other order:"
                  " the original's result is in the other operand's register; that is the IL's operand order (which"
                  " operand is the destination; symkeys.py), not a register choice" % order[(var, cls)])
        if src is not None and verdict != "ok" and cls == "int":
            print("      from: %s" % (src.get(var, "not found in the trace (an untraced phase)").replace("%s", var)))
    nok = sum(1 for r in rows if r[3] == "ok")
    print("# %d variables checked, %d agree with the original everywhere" % (len(rows), nok))
    if notes and notes["addr"]:
        print("# base/index order: %d votes had the same registers in another order inside an address (%s), counted as "
              "agreeing: which value is the base is the address's operand order, not a register choice (Mount & Blade "
              "PartyWindow::frame_update: keyed by the CSE temp of the address)" % (
                  sum(notes["addr"].values()), " ".join("%s:%d" % kv for kv in notes["addr"].most_common(6))))
    if notes and notes["swapped"]:
        print("# issue order: %d pairs of nearby instructions the original issues the other way round, paired "
              "crosswise (registers agree): scheduling, not allocation (c2sched.py, schedwant.py)" % len(notes["swapped"]))
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
    if c2trace.c2build.get("arch") == "x64":     # x86 allocator only (docs/c2/port-1700.md, tool status)
        sys.exit("c2trace.py regmap: votes for the x86 allocator's rasim; "
                 "on %s use tools/c2ncol.py (NColor's registers) and tools/ncolsim.py whatif" % c2trace.c2build.name())
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
    hooks = c2scratch.HOOKS + c2scratch._color_hooks() + [h for _, h in c2scratch.origin_hooks()]
    _, _, rc, out, log = c2trace.run_trace(a.src, hooks, cl, keep=keep, quiet=True)
    src, final = sources(log, a.func, len(c2scratch.HOOKS))
    if not final:
        sys.exit("no final IL for %r (compile error?)\n%s" % (a.func, out[-500:]))
    asm = os.path.join(keep, os.path.splitext(os.path.basename(a.src))[0] + ".asm")
    have, votes, where, agree, order, notes = analyse(final, asm, a.asm_func, tsweep.orig_instrs(a.asm_vs))
    report(have, votes, where, agree, a.all, src, order, notes)
    return 0


if __name__ == "__main__":
    sys.exit(c2trace.pipe_safe(main, sys.argv[1:]))
