#!/usr/bin/env python3
"""/Od frame layout -> local names and declaration order.  Doc: docs/c2/invert.md section 1.

The forward rule [C: code + M: probes on 13.10.3052, 13.10.3077, 14.00, 15.00]:
  * c1xx gives every identifier a 16-bit hash when it first meets the spelling (`GetIdForString`, c1xx 14.00
    0x10520370: a per-dword/per-byte mix seeded 0xb170a1bf, then `h*0x19660d + 0x3c6ef35f`, folded `h ^ h>>16`;
    `Id_t+8`).  A block scope keeps its symbols in 16 buckets (`BlockScope_t::EnterSymbol` 0x10585200: bucket =
    hash & mask, mask 15 for a function body or a nested block; pushed at the bucket's head) and its symbols are
    written to the IL by walking buckets 0..15, each chain head first (`ForEachSymbolDo` 0x10631977).
  * c2 at /Od allocates the automatics as it reads them (`getsym` -> `datalloc` -> `allotemp`), downward from ebp, so
    a scope's named locals take slots in (bucket ascending, later declaration first) order; nested block scopes come
    after their parent, in source preorder.  Statement order and first use play no part.
  * `allotemp` places each local at align_down(cur - size, align): align 1 for a 1-byte object, 8 for an object of
    64 bytes or more or one holding a double / __int64, else 4 (a short or a char[3] takes a 4-aligned slot).
    Unreferenced locals keep their slot (they are only missing from the listing).

The inverse: given the original's named slots (offset, size, type) in one scope, choose names whose buckets do not
decrease from the ebp-4 side down and declare each bucket's members in reverse slot order; dead room between slots
(inlined callees' locals) becomes unreferenced filler locals that the same solve places.  Every frame is solvable
(all names may share one bucket); the solver keeps the lifter's names where their bucket already fits and otherwise
appends the shortest suffix that does.

usage: odframe.py hash NAME..                 bucket and hash of each name
       odframe.py order NAME..                /Od slot order of names declared in this order (one scope)
       odframe.py solve OFF:SIZE[:TYPE] ..    names + declaration order reproducing those slots (OFF negative)
       odframe.py measure [--limit N] [--jobs J]   forward rule on Spore's byte-exact /Od functions (compiles)
"""
import os, re, sys

M32 = 0xffffffff
MASK = 15                                   # a block scope's buckets - 1 (c1xx 13.10 / 14.00 / 15.00)


def idhash(name):
    """c1xx's 16-bit identifier hash (Id_t+8)."""
    b = name.encode()
    h, i = 0xb170a1bf, 0
    while len(b) - i >= 4:
        x = ((int.from_bytes(b[i:i + 4], "little") + h) * 0x401) & M32
        h = x ^ (x >> 6)
        i += 4
    while i < len(b):
        c = b[i] - 256 if b[i] > 127 else b[i]
        x = ((c + h) * 0x401) & M32
        h = x ^ (x >> 6)
        i += 1
    h = (h * 0x19660d + 0x3c6ef35f) & M32
    return (h ^ (h >> 16)) & 0xffff


def bucket(name):
    return idhash(name) & MASK


def scope_order(decls, first_ref=None):
    """Allocation order (first = nearest ebp) of one scope's named automatics, given in declaration order.
    first_ref: the names in first-reference order, for a frame realigned to 16 (a local of alignment 16: `and
    esp, -16`), where c2 allocates the automatics as the tuples first reference them instead [M: probes a20]."""
    if first_ref is not None:
        return [n for n in first_ref if n in decls] + [n for n in decls if n not in first_ref]
    return [n for _b, _i, n in sorted((bucket(n), -i, n) for i, n in enumerate(decls))]


def align_of(size, ty=""):
    """allotemp's alignment for an /Od automatic (probes work/od al1/al2: 14.00 and 15.00; 13.10 agrees on scalars)."""
    if size == 1:
        return 1
    if size >= 64 or re.search(r"\b(double|__int64)\b", ty or ""):
        return 8
    return 4


def place(cur, size, ty=""):
    """allotemp: the size rounded up to the alignment, the offset aligned down to it (slots.md rule 8)."""
    a = align_of(size, ty)
    size = (size + a - 1) // a * a
    return -((-(cur - size) + a - 1) // a * a)


def layout(items, start=0):
    """[(name, size, type)] in allocation order -> {name: offset} (negative, ebp-relative), and the final depth."""
    cur, out = start, {}
    for name, size, ty in items:
        cur = place(cur, size, ty)
        out[name] = cur
    return out, cur


def _fill(cur, top, k, prefix):
    """Filler locals taking the frame from `cur` down to `top` exactly (both <= 0, top <= cur): chars to a 4-aligned
    depth, int arrays under 64 bytes (so they stay 4-aligned), chars for the rest.  [(name, size, type, decl)]"""
    out = []
    while cur > top and cur % 4:
        out.append(("%s%d" % (prefix, k), 1, "char", "char %s%d;" % (prefix, k)))
        cur -= 1
        k += 1
    while cur - top >= 4:
        n = min((cur - top) // 4, 15)
        out.append(("%s%d" % (prefix, k), 4 * n, "int", "int %s%d[%d];" % (prefix, k, n) if n > 1 else
                    "int %s%d;" % (prefix, k)))
        cur -= 4 * n
        k += 1
    while cur > top:
        out.append(("%s%d" % (prefix, k), 1, "char", "char %s%d;" % (prefix, k)))
        cur -= 1
        k += 1
    return out, k


SUFFIXES = [""] + ["_" + c for c in "abcdefghijklmnopqrstuvwxyz"] + ["_%d" % i for i in range(2, 200)]


def pick_name(base, lo, hi, taken):
    """`base` if its bucket is in [lo, hi], else the first suffixed spelling whose bucket is (the lowest such bucket
    among the first few candidates, so later slots keep room).  Deterministic."""
    best = None
    for s in SUFFIXES:
        n = base + s
        if n in taken:
            continue
        b = bucket(n)
        if lo <= b <= hi and (best is None or b < best[0]):
            best = (b, n)
            if b == lo or s == "":
                break
    return best


def _keep(bs):
    """Indices of a longest non-decreasing subsequence of bs (the names that keep their spelling)."""
    import bisect
    tails, tidx, prev = [], [], [None] * len(bs)
    for i, b in enumerate(bs):
        k = bisect.bisect_right(tails, b)
        if k == len(tails):
            tails.append(b); tidx.append(i)
        else:
            tails[k] = b; tidx[k] = i
        prev[i] = tidx[k - 1] if k else None
    out, i = set(), tidx[-1] if tidx else None
    while i is not None:
        out.add(i)
        i = prev[i]
    return out


def solve(slots, bottom=None, filler="dead"):
    """slots: [(name, offset, size, type, decl)] of one scope, offset negative (the slot's low end), decl the
    declaration text with `name` in it.  Returns {"decls": [declaration text in declaration order], "rename":
    {old: new}, "order": [names in allocation order], "offsets": {new name: offset}} or None when no layout exists
    (a slot the alignment rule cannot place there, overlapping slots).  bottom: the named block's low end (trailing
    dead room becomes filler too)."""
    slots = sorted(slots, key=lambda s: -s[1])
    seq, cur, k = [], 0, 0
    for name, off, size, ty, decl in slots:
        top = off + size
        if top > cur:
            return None                                  # overlaps the slot above it
        if place(cur, size, ty) != off:                  # not where it would land by itself: dead room above it
            f, k = _fill(cur, top, k, filler)
            seq += [(n, s, t, d, None) for n, s, t, d in f]
            cur = top
            m = re.match(r"^(.*?)\[(\d+)\];$", decl)
            if place(cur, size, ty) != off and m and size >= 64 and size % 4 == 0 and off % 4 == 0 and \
                    SIZES.get(ty) == 4:
                # an array of 64 bytes or more is 8-aligned with its size rounded to 8: the original's object at
                # a 4-aligned offset is several; the code only takes its base address, so the lower 60 bytes keep
                # the name and the rest above it is filler
                f, k = _fill(cur, off + 60, k, filler)
                seq += [(n, s, t, d, None) for n, s, t, d in f]
                cur, size, decl = off + 60, 60, "%s[15];" % m.group(1)
            if place(cur, size, ty) != off:
                return None
        seq.append((name, size, ty, decl, off))
        cur = off
    if bottom is not None and bottom < cur:
        f, k = _fill(cur, bottom, k, filler)
        seq += [(n, s, t, d, None) for n, s, t, d in f]
    # names: buckets non-decreasing along the allocation order
    taken = {s[0] for s in slots} | {s[0] for s in seq}
    keep = _keep([bucket(x[0]) for x in seq])
    lo, names, rename = 0, [], {}
    for i, (name, size, ty, decl, off) in enumerate(seq):
        hi = min([bucket(seq[j][0]) for j in keep if j > i] + [MASK])
        b, n = pick_name(name, lo, hi, taken - {name})
        taken.add(n)
        names.append((b, n, name, decl))
        if n != name:
            rename[name] = n
        lo = b
    # declarations: each bucket's members in reverse allocation order (c1xx pushes at the bucket head)
    order = [n for _b, n, _o, _d in names]
    groups = {}
    for i, (b, n, old, decl) in enumerate(names):
        groups.setdefault(b, []).append((i, n, old, decl))
    decls = []
    for b in sorted(groups):
        for i, n, old, decl in reversed(groups[b]):
            decls.append(re.sub(r"\b%s\b" % re.escape(old), n, decl, count=1))
    # check: the forward rule must give the slots back
    got, _ = layout([(n, s, t) for (_b, n, _o, _d), (_n, s, t, _dd, _off) in zip(names, seq)])
    declared = [re.search(r"\b(\w+)\s*(?:\[\d+\])?\s*;\s*$", d).group(1) for d in decls]
    assert scope_order(declared) == order, (declared, order)
    offs = {}
    for (_b, n, old, _d), (_n, s, t, _dd, off) in zip(names, seq):
        if off is not None:
            assert got[n] == off, (n, got[n], off)
            offs[n] = off
    return {"decls": decls, "rename": rename, "order": order, "offsets": offs}


# ---------------------------------------------------------------------------------------------------------------
# the lifter's call site (il2c_lift.py, /Od units): separate named locals instead of a packed frame structure

SIZES = {"int": 4, "unsigned int": 4, "unsigned char": 1, "char": 1, "unsigned short": 2, "short": 2, "float": 4,
         "double": 8, "__int64": 8, "char*": 4, "int*": 4, "bool": 1}
DECL = re.compile(r"(?m)^    (?!return\b)([\w ]+?\*?) ((?:v_l|t5)(\d+))(?:\[(\d+)\])?;\n")


def place_locals(body, named):
    """The lifted /Od body's frame slots (`v_lN`: ebp-(N-4); `t5xxx`, a temporary the lifter pinned to slot xxx-5000)
    as separate locals named and declared so the hash walk puts each at its offset.  `named`: the named block's
    bytes.  Returns the new body, or None (a slot the rule cannot place: overlap, a scalar wider than its room, a
    declared local of unknown slot)."""
    first = re.search(r"(?m)^    (?!(?:[\w ]+?\*?) \w+(?:\[\d+\])?;$).", body)
    head = body[:first.start()] if first else body
    if re.search(r"(?m)^    (?!return\b)[\w ]+?\*? (?!(?:v_l|t5)\d+\b)\w+(?:\[\d+\])?;$", head):
        return None                                      # another named local: its slot is not known
    slots = []
    for m in DECL.finditer(head):
        ty, name, n, cnt = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if ty not in SIZES:
            return None
        depth = n - 4 if name.startswith("v_l") else n - 5000 - 4
        slots.append([name, -depth, SIZES[ty], ty, int(cnt) if cnt else None, m.group(0)])
    if not slots:
        return None
    slots.sort(key=lambda s: -s[1])
    cur = 0
    for s in slots:                                       # an address-only array sized past its room: shrink it
        room = cur - s[1]
        if s[4] and s[2] * s[4] > room and room >= s[2]:
            s[4] = room // s[2]
        size = s[2] * (s[4] or 1)
        if size > room or s[1] < -named:
            return None
        cur = s[1]
    plan = solve([(nm, off, sz * (cnt or 1), ty, "%s %s%s;" % (ty, nm, "[%d]" % cnt if cnt else ""))
                  for nm, off, sz, ty, cnt, _t in slots], bottom=-named, filler="dead_")
    if plan is None:
        return None
    for s in slots:
        body = body.replace(s[5], "", 1)
    for old, new in sorted(plan["rename"].items(), key=lambda t: -len(t[0])):
        body = re.sub(r"(?<![\w.])%s\b" % re.escape(old), new, body)
    return body.replace("{\n", "{\n" + "".join("    %s\n" % d for d in plan["decls"]), 1)


# ---------------------------------------------------------------------------------------------------------------
# measurement on Spore's byte-exact /Od functions


SPORE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "..",
                     "spore-decomp")


def spore_od_rows():
    """(source path, symbol, va, flags) for every byte-exact /Od row of Spore's slice manifests."""
    import glob, shlex
    out = []
    for m in sorted(glob.glob(os.path.join(SPORE, "match", "slices", "*", "manifest.txt"))):
        for ln in open(m):
            if ln.startswith("#") or not ln.strip():
                continue
            p = ln.split(None, 3)
            if len(p) < 4 or "/Od" not in shlex.split(p[3]):
                continue
            out.append((os.path.join(SPORE, "match", p[0]), p[1], p[2], p[3].strip()))
    return out


def listing_frames(asm):
    """{decorated function: [(name, offset, block)]} from a /FAs listing's `_name$[NNN] = -N` lines (block: the
    name had a $NNN suffix, a nested block's local)."""
    out = {}
    for seg in re.split(r"^_TEXT\tSEGMENT\s*$", asm, flags=re.M)[1:]:
        m = re.search(r"^(\?\S+|_\w+)\s+PROC\b", seg, re.M)
        if not m:
            continue
        pre = seg[:m.start()]
        out[m.group(1)] = [(n, int(o), bool(s)) for n, s, o in
                           re.findall(r"^_(\w+?)\$(\d*) = (-\d+)", pre, re.M)]
    return out


def source_lines(asm, func):
    """(first, last) source line numbers the listing shows inside `func`'s PROC."""
    m = re.search(r"^%s\s+PROC\b(.*?)^%s\s+ENDP" % (re.escape(func), re.escape(func)), asm, re.M | re.S)
    if not m:
        return None
    nums = [int(x) for x in re.findall(r"^; (\d+)\s+:", m.group(1), re.M)]
    return (min(nums), max(nums)) if nums else None


BUILTIN = {"int", "unsigned", "signed", "char", "short", "long", "float", "double", "bool", "__int64", "uint32",
           "int32", "uint8", "uint16", "int8", "int16", "size_t", "DWORD", "BOOL", "BYTE", "WORD", "const", "volatile"}


def _compile_listing(key):
    import subprocess, tempfile, shlex
    src, flags = key
    d = tempfile.mkdtemp(prefix="odm_")
    asm, obj = os.path.join(d, "x.asm"), os.path.join(d, "x.obj")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    inc = [x for x in ("/I" + os.path.join(SPORE, "match", "include"), "/I" + os.path.dirname(src))]
    r = subprocess.run([sys.executable, os.path.join(here, "msvc.py"), "cl", "/nologo", "/c"] + shlex.split(flags) +
                       inc + ["/FAs", "/Fa" + asm, "/Fo" + obj, src], capture_output=True, text=True)
    return open(asm, errors="replace").read() if os.path.exists(asm) else None


def _measure_one(row, text):
    src, sym, va, flags = row
    if text is None:
        return (row, "compile", None)
    fr = listing_frames(text).get(sym)
    if fr is None:
        return (row, "nofunc", None)
    fr = [(n, o, b) for n, o, b in fr if n != "this"]   # `this`'s home is a compiler temporary, below the block
    lines0 = source_lines(text, sym)
    sig = ""
    if lines0:
        sl = open(src, errors="replace").read().split("\n")[max(0, lines0[0] - 4):lines0[1]]
        sig = "\n".join(sl).split("{", 1)[0]
    # a register parameter's home (fastcall ecx / edx) is allocated after the body's locals [M: 0x00428130]
    fr = [(n, o, b) for n, o, b in fr if not re.search(r"\b%s\b" % re.escape(n), sig)]
    top = [(n, o) for n, o, blk in fr if not blk]
    blk = [(n, o) for n, o, b in fr if b]
    if len(top) < 2:
        return (row, "few", None)
    lines = source_lines(text, sym)
    srcl = open(src, errors="replace").read().split("\n")
    body = "\n".join(srcl[lines[0] - 1:lines[1]]) if lines else ""
    body = re.sub(r"//[^\n]*|/\*.*?\*/", "", body, flags=re.S)       # comments name locals too
    pos = {}
    for n, _o in top:
        m = re.search(r"\b%s\b" % re.escape(n), body)
        pos[n] = m.start() if m else 10 ** 9
    decl = sorted((n for n, _o in top), key=lambda n: pos[n])
    want = [n for n, _o in sorted(top, key=lambda t: -t[1])]
    proc = re.search(r"^%s\s+PROC\b(.*?)^%s\s+ENDP" % (re.escape(sym), re.escape(sym)), text, re.M | re.S)
    realigned = bool(proc and re.search(r"\band\s+esp, -16\b", proc.group(1)))
    if realigned:                       # first reference: the declaration when it initialises, else the next use
        ref = {}
        for n in pos:
            ms = list(re.finditer(r"(?<![.>])\b%s\b" % re.escape(n), body))
            init = ms and re.match(r"\s*(?:\(|=|\[[^]]*\]\s*=|\{)", body[ms[0].end():])
            ln = body[:ms[0].start()].rsplit("\n", 1)[-1] if ms else ""
            ty = re.search(r"([A-Za-z_][\w:<>]*)\s*([*&]*)\s*(?:\w+\s*(?:=[^,;]*)?,\s*)*$", ln)
            if ty and not ty.group(2) and ty.group(1) not in BUILTIN:
                init = True             # a class object: its constructor references it at the declaration
            ref[n] = ms[0].start() if init or len(ms) < 2 else ms[1].start()
        got = scope_order(decl, first_ref=[n for n in sorted(ref, key=lambda n: ref[n])])
    else:
        got = scope_order(decl)
    ties = sum(1 for i in range(len(want) - 1) if bucket(want[i]) == bucket(want[i + 1]))
    below = (not blk) or max(o for _n, o in blk) < min(o for _n, o in top)
    return (row, "ok", dict(n=len(top), exact=got == want, pairs=sum(1 for i in range(len(want)) for j in
                            range(i + 1, len(want)) if got.index(want[i]) < got.index(want[j])),
                            npairs=len(want) * (len(want) - 1) // 2, ties=ties, block_below=below,
                            want=want, got=got, realigned=realigned))


def measure(limit=None, jobs=6):
    from concurrent.futures import ThreadPoolExecutor
    rows = spore_od_rows()
    if limit:
        rows = rows[:limit]
    keys = sorted({(r[0], r[3]) for r in rows})
    with ThreadPoolExecutor(jobs) as ex:
        lst = dict(zip(keys, ex.map(_compile_listing, keys)))
    res = [_measure_one(r, lst[(r[0], r[3])]) for r in rows]
    ok = [r for r in res if r[1] == "ok"]
    ex_ = sum(1 for r in ok if r[2]["exact"])
    pr = sum(r[2]["pairs"] for r in ok), sum(r[2]["npairs"] for r in ok)
    print("%d /Od byte-exact rows; %d with 2+ function-scope locals; frame order predicted exactly for %d (%.1f%%);"
          " ordered pairs %d/%d; nested-block locals below the function scope's in %d/%d" %
          (len(res), len(ok), ex_, 100.0 * ex_ / max(1, len(ok)), pr[0], pr[1],
           sum(1 for r in ok if r[2]["block_below"]), len(ok)))
    ra = [r for r in ok if r[2]["realigned"]]
    print("realigned (and esp, -16) frames: %d, first-reference order exact for %d" %
          (len(ra), sum(1 for r in ra if r[2]["exact"])))
    import collections
    print("skipped:", dict(collections.Counter(r[1] for r in res if r[1] != "ok")))
    for r in ok:
        if not r[2]["exact"]:
            print("  MISS %s %s want %s got %s" % (r[0][2], r[0][1][:40], " ".join(r[2]["want"]), " ".join(r[2]["got"])))
    return res


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd, rest = argv[0], argv[1:]
    if cmd == "hash":
        for n in rest:
            print("%-20s bucket %2d  hash %04x" % (n, bucket(n), idhash(n)))
    elif cmd == "order":
        print(" ".join(scope_order(rest)))
    elif cmd == "solve":
        slots = []
        for i, s in enumerate(rest):
            p = s.split(":")
            off, size = int(p[0]), int(p[1])
            ty = p[2] if len(p) > 2 else ("char" if size == 1 else "int")
            nm = "v%d" % (-off)
            decl = "%s %s;" % (ty, nm) if size in (1, 2, 4, 8) and ty != "int" or size == 4 else \
                "int %s[%d];" % (nm, size // 4)
            slots.append((nm, off, size, ty, decl))
        r = solve(slots)
        if not r:
            print("no layout")
            return 1
        print("\n".join(r["decls"]))
    elif cmd == "measure":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--limit", type=int)
        ap.add_argument("--jobs", type=int, default=6)
        a = ap.parse_args(rest)
        measure(a.limit, a.jobs)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
