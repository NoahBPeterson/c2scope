#!/usr/bin/env python3
"""x64metrics: how far a compiled function is from the original, beyond x64check's count of differing lines.

    from x64metrics import metrics
    m = metrics(orig_rows, our_rows, our_extent)      # rows: x64check.listing() [(offset, "mnem ops"), ...]
    m = metrics_of(result)                            # from an x64check.check() result ("rows", "extent")

m = {"edits": Levenshtein distance over the instructions,
     "edits_noreg": the same over their shapes (every register R, every [rsp/rbp+off] [S]): what is left once
                    register choice and frame layout cost nothing,
     "mapcost": register choice and stack layout as a mapping (see mapping_cost), about 1 per different choice,
     "regpairs": {our register: the original's} over the shape-aligned instructions (full 64-bit names),
     "n_orig", "n_ours": instruction counts, "bad": original offsets of the instructions that differ,
     "ops": the most common shape edits ("orig => ours", count)}

edits_noreg 0 with mapcost > 0: the code is right and only registers / slots differ (regfix64's case). Computed over
x64check's listings."""
import collections
import difflib
import re

REGS = re.compile(r"\b(?:[re]?(?:ax|bx|cx|dx|si|di|bp|sp)|[abcd]l|[abcd]h|sil|dil|bpl|spl|r(?:8|9|1[0-5])[dwb]?|"
                  r"xmm1[0-5]|xmm[0-9])\b")
SLOT = re.compile(r"\[(?:rsp|rbp)(?: ([+-]) (0x[0-9a-f]+|\d+))?\]")
FULL = {}
for _n, _ws in (("rax", "eax ax al ah"), ("rbx", "ebx bx bl bh"), ("rcx", "ecx cx cl ch"), ("rdx", "edx dx dl dh"),
                ("rsi", "esi si sil"), ("rdi", "edi di dil"), ("rbp", "ebp bp bpl"), ("rsp", "esp sp spl")):
    for _w in [_n] + _ws.split():
        FULL[_w] = _n
for _k in range(8, 16):
    for _w in ("r%d" % _k, "r%dd" % _k, "r%dw" % _k, "r%db" % _k):
        FULL[_w] = "r%d" % _k


def lev(a, b):
    """Levenshtein distance between two sequences (unit cost insert / delete / substitute)."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def keys(rows):
    """Diff keys: jumps inside the function position-free (+L: the order already says where); direct calls one key
    (our object's call rel32 is 0 before linking: it reads as a call to the next instruction)."""
    out = []
    for _off, k in rows:
        if re.match(r"^call (\+0x[0-9a-f]+|<target>)$", k):
            out.append("call <target>")
            continue
        m = re.match(r"^(j\w+|loop\w*) (\+0x[0-9a-f]+|<target>)$", k)    # (a tail jmp's rel32 is 0 in ours too)
        out.append(m.group(1) + " +L" if m else k)
    return out


def shape(seq):
    """Every register R, every stack slot [S]."""
    return [REGS.sub("R", re.sub(r"\[(rsp|rbp)( [+-] 0x[0-9a-f]+| [+-] \d+)?\]", "[S]", x)) for x in seq]


def operands(ins):
    """(registers in order as full names, stack slots in order as (base, offset)) of one diff key."""
    regs = [FULL.get(x, x) for x in REGS.findall(SLOT.sub("[S]", ins))]
    offs = []
    for m in SLOT.finditer(ins):
        v = int(m.group(2), 0) if m.group(2) else 0
        offs.append((ins[m.start() + 1:m.start() + 4], -v if m.group(1) == "-" else v))
    return regs, offs


def mapping_cost(a, b, sa=None, sb=None):
    """Register choice and stack layout as a mapping: over the instructions whose shape matches, pair each
    original register (and stack slot) with ours. Cost = pairs that break the majority mapping + mappings that
    are not the identity. One different allocation then costs about 1, not one edit per instruction it touches;
    0 together with shape 0 means the code is the same. -> (cost, {our register: original register})."""
    sa, sb = sa or shape(a), sb or shape(b)
    pr, po = collections.Counter(), collections.Counter()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes():
        if op != "equal":
            continue
        for k in range(i2 - i1):
            ra, oa = operands(a[i1 + k])
            rb, ob = operands(b[j1 + k])
            for x, y in zip(ra, rb):
                pr[(x, y)] += 1
            for x, y in zip(oa, ob):
                po[(x, y)] += 1
    cost, maps = 0, []
    for pairs in (pr, po):
        best = {}
        for (x, y), n in pairs.most_common():
            best.setdefault(x, y)
        cost += sum(n for (x, y), n in pairs.items() if best[x] != y)
        cost += sum(1 for x, y in best.items() if x != y)
        maps.append(best)
    return cost, {y: x for x, y in maps[0].items()}


def metrics(orig, mine, extent=None):
    """orig / mine: [(offset "+0x..", key)] listings (x64check.listing); extent: our function's length in bytes
    (rows past it are the next function's, the listing reads the original's length)."""
    if extent is not None:
        mine = [r for r in mine if int(r[0].lstrip("+"), 16) < extent]
    a, b = keys(orig), keys(mine)
    sa, sb = shape(a), shape(b)
    bad = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op != "equal":
            bad += [orig[i][0] for i in range(i1, max(i2, i1 + 1)) if i < len(orig)]
    ops = collections.Counter()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        for k in range(max(i2 - i1, j2 - j1)):
            x = sa[i1 + k] if i1 + k < i2 else "-"
            y = sb[j1 + k] if j1 + k < j2 else "-"
            ops["%s => %s" % (x[:48], y[:48])] += 1
    cost, pairs = mapping_cost(a, b, sa, sb)
    return dict(edits=lev(a, b), edits_noreg=lev(sa, sb), mapcost=cost, regpairs=pairs, n_orig=len(a),
                n_ours=len(b), bad=bad[:3000], ops=ops.most_common(60))


def metrics_of(res):
    """metrics() of an x64check.check() result that carries its listings (status 'no match' after a compare);
    None otherwise."""
    if not res.get("rows"):
        return None
    return metrics(res["rows"][0], res["rows"][1], res.get("extent"))
