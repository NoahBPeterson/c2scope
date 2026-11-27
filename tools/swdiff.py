#!/usr/bin/env python3
"""swdiff: the per-case convergence driver for big switch functions.

Reads an aligned full diff of the function (original | ours rows), the switch's case table (cases.txt), and
optionally the /FAsc listing of the candidate, and prints the work list: the common-code diffs
(prologue, loop, epilogue), the per-case diffs classified (call-shape / register / shape / spill /
vtable), the shared-tail map (which case bodies share which block, original vs ours), the
vtable-call debt grouped by slot, and per-0x100-region exact counts computed from the diff itself.

usage: swdiff.py --diff DIFF.txt --cases CASES.txt [--lst OURS.cod] [--case +0x...]
                 [--top N] [--out FILE]
       DIFF: one row per aligned pair, '%s %-8s %-40s | %s' (mark, +0xOFF, original mnemonic and operands,
             ours), from your project's compare tool
       CASES: the `=== case +0x...` table (one line per case: its value and +0x offset)
       --lst: the /FAsc listing of the same compile (source-line join for our rows)

The classification needs no c2 hooks: everything is listing machinery, so the loop
(edit -> compare -> swdiff) is free and offline.
"""
import argparse, re, sys
from collections import defaultdict

DIFF_ROW = re.compile(r"^([! ]) (.{8}) (.{40}) \| (.*)$")
CASE_HDR = re.compile(r"^=== case (\+0x[0-9a-f]+)")
LST_LINE = re.compile(r"^;\s*(\d+)\s*:")
LST_ROW = re.compile(r"^\s+([0-9a-fA-F]{4,6})\s+(?:[0-9A-Fa-f]{2} )+\s*([a-z.]+)\s*(.*)$")

REG64 = {"rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp",
         "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15"}
REG32 = {r + "e" for r in ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp")} | \
        {r + "d" for r in ("r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15")}
REG16 = {"ax", "bx", "cx", "dx", "si", "di", "bp", "sp"} | \
        {r + "w" for r in ("r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15")}
REG8 = {"al", "bl", "cl", "dl", "ah", "bh", "ch", "dh", "sil", "dil", "bpl", "spl"} | \
       {r + "b" for r in ("r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15")}


def regcls(tok):
    if tok in REG64: return "R64"
    if tok in REG32: return "R32"
    if tok in REG16: return "R16"
    if tok in REG8: return "R8"
    if tok.startswith(("xmm", "ymm")): return tok[:3].upper()
    return None


def normops(ops):
    """Loose-normalize an operand string: spacing, ptr keyword case, hex case."""
    t = re.sub(r"\s+", " ", ops.strip().lower())
    t = re.sub(r"\s*([,+*])\s*", r"\1", t)
    return t


def opsig(ops):
    """The operand string with register names replaced by classes: the register-diff test."""
    t = normops(ops)
    out = []
    for m in re.finditer(r"\[[^\]]*\]|[A-Za-z_]\w*|\S", t):
        tok = m.group(0)
        if tok.startswith("["):
            inner = " ".join(regcls(x) or x for x in re.findall(r"[A-Za-z_]\w*|\S", tok[1:-1]))
            out.append("[" + inner + "]")
        elif regcls(tok):
            out.append(regcls(tok))
        else:
            out.append(tok)
    return " ".join(out)


def parse_row(field):
    """'mov     qword ptr [rax + 0x10], rdx' -> (mnemonic, ops)."""
    f = field.strip()
    if not f:
        return None
    m = re.match(r"(\S+)\s*(.*)$", f)
    return (m.group(1), m.group(2))


def parse_diff(text):
    """[(mark, orig, ours)]; rows = (offset-int or None, mnemonic, ops)."""
    out = []
    for ln in text.splitlines():
        m = DIFF_ROW.match(ln.rstrip("\n"))
        if not m:
            continue
        mark, of, ofield, ours = m.groups()
        orig = None
        if ofield.strip():
            off = int(of.strip(), 16) if of.strip() else None
            r = parse_row(ofield)
            orig = (off, r[0], r[1]) if r else None
        y = parse_row(ours)
        ours = (None, y[0], y[1]) if y else None
        if orig or ours:
            out.append((mark, orig, ours))
    return out


def parse_cases(text):
    return [int(m.group(1), 16) for m in map(CASE_HDR.match, text.splitlines()) if m]


def parse_lst(text):
    """The /FAsc listing -> ({off: line}, [(off, mnemonic, ops)])."""
    lines, rows, cur, off2line = {}, [], None, {}
    for ln in text.splitlines():
        m = LST_LINE.match(ln)
        if m:
            cur = int(m.group(1))
            continue
        m = LST_ROW.match(ln)
        if m:
            off, mnem, ops = int(m.group(1), 16), m.group(2), m.group(3).strip()
            rows.append((off, mnem, ops))
            if cur is not None:
                off2line[off] = cur
    return off2line, rows


def walk(orig_rows, entries, stop):
    """offset -> set(case index): forward walks from each case entry. A conditional jump both falls
    through and targets; an unconditional jmp targets (no fall-through) — attribution is the
    business: the walk reaches THROUGH nothing special, it stops only when the jmp leaves the
    function, and the tail rows are owned by every case that reaches them. `stop` only guards the
    walk from following the case's exit jmp into the epilogue: a jmp to the modal target ends the
    body there (the epilogue is not attributed to the case)."""
    succ = {off: (mnem, ops) for off, mnem, ops in orig_rows}
    order = [off for off, _, _ in orig_rows]
    nxt = {order[i]: order[i + 1] for i in range(len(order) - 1)}
    own = defaultdict(set)
    for ci, ent in enumerate(entries):
        work, seen = [ent], set()
        while work:
            off = work.pop()
            if off in seen or off not in succ:
                continue
            seen.add(off)
            own[off].add(ci)
            mnem, ops = succ[off]
            tgts = [int(t, 16) for t in re.findall(r"\+0x([0-9a-f]+)", ops)] if mnem.startswith("j") else []
            if mnem == "jmp":
                if tgts and tgts[0] == stop:
                    continue                    # the body ends at the common exit (no epilogue attribution)
                if tgts and tgts[0] in succ:
                    work.append(tgts[0])
                continue                        # a jmp has no fall-through
            for t in tgts:
                if t in succ:                   # the tail rows ARE attributed: the walk does not stop at `stop` for them
                    work.append(t)
            if off in nxt:
                work.append(nxt[off])
    return own


def classify(orig, ours):
    """One differing pair -> (class, note) or None when the pair is equal after normalization."""
    if orig is None:
        return "ours-only"
    if ours is None:
        return "orig-only"
    om, oo = orig[1], orig[2]
    ym, yo = ours[1], ours[2]
    if normops(oo) == normops(yo) and om == ym:
        return None
    if om == "call" or ym == "call":
        vt_o = vtable_target(om + " " + oo) if om == "call" else None
        vt_y = vtable_target(ym + " " + yo) if ym == "call" else None
        if vt_o or vt_y:
            return "vtable-call"
        return "call-shape"
    if om.startswith("j") or ym.startswith("j"):
        return "shape"
    if om == ym and opsig(oo) == opsig(yo):
        return "register"
    a, b = has_stack(oo), has_stack(yo)
    if a != b:
        return "spill"
    return "other"


def has_stack(ops):
    return bool(re.search(r"\[(rspb|rbp)\b|\[(rsp|rbp) [\+*-]", normops(ops)))


def vtable_target(ops):
    m = re.search(r"call\s+(?:qword|word) ptr \[(\w+)(?: ?([\+-]) ?(0x[0-9a-f]+|\d+))?\]", ops)
    if m:
        return (m.group(1), int(m.group(3), 0) * (1 if m.group(2) != "-" else -1) if m.group(3) else 0)
    m = re.search(r"call\s+(?:qword|word) ptr \[", ops)
    return ("[indirect]", 0) if m else None


def main(argv):
    ap = argparse.ArgumentParser(prog="swdiff.py", usage=__doc__)
    ap.add_argument("--diff", required=True, help="the aligned full diff FILE (one original | ours pair per row)")
    ap.add_argument("--cases", required=True, help="the `=== case +0x...` table")
    ap.add_argument("--lst", help="the /FAsc listing of the same compile (the source-line join)")
    ap.add_argument("--case", help="only this case (+0x...)")
    ap.add_argument("--top", type=int, default=8, help="diff rows shown per case (default 8)")
    ap.add_argument("--out", help="write the work list here as well")
    a = ap.parse_args(argv)

    diff = parse_diff(open(a.diff, errors="replace").read())
    entries = parse_cases(open(a.cases, errors="replace").read())
    lst_off2line, lst_rows = parse_lst(open(a.lst, errors="replace").read()) if a.lst else ({}, [])

    orig_rows = [(d[1][0], d[1][1], d[1][2]) for d in diff if d[1]]
    # the modal unconditional-jmp target: the switch's common exit (the loop-continue)
    tgts = defaultdict(int)
    for off, mnem, ops in orig_rows:
        if mnem == "jmp":
            m = re.match(r"\+0x([0-9a-f]+)", ops)
            if m:
                tgts[int(m.group(1), 16)] += 1
    stop = max(tgts, key=tgts.get) if tgts else None
    own = walk(orig_rows, entries, stop)

    # ours offsets: the diff holds every ours row in compile order; the /FAsc listing holds the same
    # sequence with offsets — zip them for the source-line join (and a sanity count)
    ours_seq = [(i, d[2]) for i, d in enumerate(diff) if d[2]]
    yoff = {}
    if lst_rows and len(lst_rows) == len(ours_seq):
        for (i, y), (off, _, _) in zip(ours_seq, lst_rows):
            yoff[i] = off
    elif lst_rows:
        print("# note: --lst has %d instructions, the diff has %d ours rows; source lines skipped"
              % (len(lst_rows), len(ours_seq)), file=sys.stderr)

    # classify every differing row
    diffs = []                       # (case-or-None, off, class, orig, ours, yoff, mark)
    counts = defaultdict(lambda: defaultdict(int))
    for i, (mark, orig, ours) in enumerate(diff):
        if mark == " ":
            continue
        cls = classify(orig, ours)
        if cls is None:
            continue
        off = orig[0] if orig else None
        ci = min(own.get(off, {None})) if off is not None and own.get(off) else None
        yl = None
        if i in yoff:
            yl = lst_off2line.get(yoff[i])
        diffs.append((ci, off, cls, orig, ours, yl))
        counts[ci][cls] += 1

    out = []
    w = out.append
    ncases = len(entries)
    ndiff = len(diffs)
    w("# swdiff: %d cases, %d differing rows, common exit +0x%x" % (ncases, ndiff, stop or 0))

    # the common code first (prologue, loop head, epilogue): not owned by any case
    common = [d for d in diffs if d[0] is None]
    if common and (a.case is None):
        w("")
        w("== common (prologue / loop head / epilogue): %d diffs" % len(common))
        for d in common[:a.top]:
            w(_row(d, yoff))
        if len(common) > a.top:
            w("   ... %d more" % (len(common) - a.top))

    for ci, ent in enumerate(entries):
        if a.case is not None and ("+0x%x" % ent) != a.case:
            continue
        rows = [d for d in diffs if d[0] == ci]
        if not rows:
            continue
        cs = counts[ci]
        summ = ", ".join("%d %s" % (n, c) for c, n in sorted(cs.items(), key=lambda kv: -kv[1]))
        w("")
        w("== case +0x%x: %d diffs (%s)" % (ent, len(rows), summ))
        for d in rows[:a.top]:
            w(_row(d, yoff))
        if len(rows) > a.top:
            w("   ... %d more" % (len(rows) - a.top))

    # the shared tails: original offsets owned by >= 2 cases, grouped into contiguous runs
    shared = {off for off, cs in own.items() if len(cs) >= 2}
    runs, start, prev = [], None, None
    order = [o for o, _, _ in orig_rows]
    for off in order:
        if off not in shared:
            continue
        if start is None:
            start = prev = off
            continue
        gap = off - prev
        if gap <= 16:                      # same run (a jump/pointer row between two shared rows)
            prev = off
        else:
            runs.append((start, prev)); start = prev = off
    if start is not None:
        runs.append((start, prev))
    w("")
    w("== shared tails (original blocks owned by >= 2 cases):")
    for start, end in runs:
        cs = set()
        for off in shared:
            if start <= off <= end:
                cs |= own[off]
        cs_s = ", ".join("+0x%x" % entries[c] for c in sorted(cs) if c is not None and c < len(entries))
        # ours' reproduction of the block: the diff rows aligned inside the range. Rows marked ' '
        # (equal) across the whole range = ours reproduced the block; ANY '!' row inside the range
        # (with or without an ours partner: an ours-only copy elsewhere is still not shared here).
        equal = sum(1 for mark, orig, ours in diff
                    if orig and start <= orig[0] <= end and mark == " " and ours)
        differ = sum(1 for mark, orig, ours in diff
                     if orig and start <= orig[0] <= end and mark == "!")
        if equal and not differ:
            state = "ours: shared too"
        elif differ:
            state = "ours: NOT shared - the case shape inlines it (%d row(s) differ)" % differ
        else:
            state = "ours: ? (the block's rows never aligned; check the case shapes)"
        w("  +0x%x..+0x%x  cases %s   [%s]" % (start, end, cs_s, state))
    # the vtable debt
    vt = defaultdict(list)
    for off, mnem, ops in orig_rows:
        if mnem == "call":
            t = vtable_target(mnem + " " + ops)
            if t:
                cs = own.get(off) or {None}
                vt[t].append((off, sorted(cs)))
    if vt:
        w("")
        w("== vtable calls (original), by slot:")
        for (reg, imm), uses in sorted(vt.items(), key=lambda kv: -len(kv[1])):
            cs = sorted({c for _, cs in uses for c in cs if c is not None})
            w("  [%s%+#x] x%d  (cases %s; offsets %s)" % (
                reg, imm, len(uses), ", ".join("+0x%x" % entries[c] for c in cs[:8]),
                " ".join("+0x%x" % off for off, _ in uses[:6])))
    # regions
    regs = defaultdict(lambda: [0, 0])
    for mark, orig, ours in diff:
        if not orig:
            continue
        r = orig[0] // 0x100
        regs[r][0] += 1
        if mark != " " and classify(orig, ours) is not None:
            regs[r][1] += 1
    w("")
    w("== regions of 0x100 bytes (instructions exact / total):")
    for r in sorted(regs):
        t, d = regs[r]
        w("  R%-2d +0x%04x..%04x  %d/%d exact%s" % (r, r * 0x100, r * 0x100 + 0xff, t - d, t,
                                                    "   <-- work here" if d else ""))
    text = "\n".join(out) + "\n"
    sys.stdout.write(text)
    if a.out:
        open(a.out, "w").write(text)
    return 0


def next_off_gap(off, orig_rows):
    """The byte length of the instruction at `off` (for the tail-run grouping)."""
    order = [o for o, _, _ in orig_rows]
    i = order.index(off)
    return order[i + 1] - off if i + 1 < len(order) else None


def _tail_inlined(diff, start, end):
    """True when the original's shared block is NOT shared in ours: some of its instructions have
    ours-side rows that differ (the case inlined its own copy)."""
    for mark, orig, ours in diff:
        if orig and start <= orig[0] <= end and mark == "!" and ours:
            return True
    return False


def _row(d, yoff):
    _, off, cls, orig, ours, yl = d
    o = "%s %s" % (orig[1], orig[2]) if orig else "(none)"
    y = "%s %s" % (ours[1], ours[2]) if ours else "(none)"
    tag = " L%d" % yl if yl else ""
    return "  ! %s  %-28s | %s%s   [%s]" % (("+0x%04x" % off) if off is not None else "        ",
                                            o[:28], y[:44], tag, cls)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
