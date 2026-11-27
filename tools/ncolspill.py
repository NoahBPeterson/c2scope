#!/usr/bin/env python3
"""Splits and spills of an NColor run, side by side with assembly listings.
Doc: docs/c2/ncolor.md 13

usage: ncolspill.py [EXPORT.json] [--func F] [--asm FILE ...] [--top K]

EXPORT.json is `tools/c2ncol.py SRC --export FILE -- ...` (cl 17.00 x86_amd64): every candidate's regions,
priority, lifetime and register. The report counts what NColor did to each candidate:

  whole        one region, the whole lifetime in one register
  split        two or more regions: NColBestCoverage separated a part that another register covers
  unallocated  no region (folded, dead, or spilled back by NColPostspill)

and lists the split candidates in priority order, so a cascade (one candidate taking a register that displaces the
next) is visible as a run of splits. The `main` region is the longest one; `stolen` marks a split whose other part
got a must-save register.

--asm FILE ...: one or more listings (`VA mnemonic operands` lines, the form orig_va.s / my_va.s have; the `+0xN`
form of an offset-keyed aligned diff is accepted too). For each it reports the frame accesses as a proxy for that build's spills:
per [rbp/rsp+-0xN] slot the loads and stores, the slots reloaded or re-stored three or more times, and the top
slots by access count. `lea` is an address, not an access, and is not counted; a read-modify-write counts once as a
load and once as a store. Two listings also get a side-by-side summary: an original and a draft, or two drafts.

The original has no NColor trace, so its half is the frame-access proxy; the tool never claims the original's
candidate decisions.
"""
import argparse, collections, os, re, sys


LINE = re.compile(r"^\s*(?:[0-9a-f]{8,16}|\+?0x[0-9a-f]+):?\s+([a-z][a-z0-9]*)\s*(.*)$", re.I)
MEM = re.compile(r"\[\s*(r?)(bp|sp)\s*(?:([+-])\s*(?:0x([0-9a-f]+)|(\d+)))?\s*\]", re.I)
RMW = {"add", "sub", "and", "or", "xor", "inc", "dec", "not", "neg", "shl", "shr", "sar", "rol", "ror", "adc", "sbb"}
GP64 = {"rax", "rbx", "rcx", "rdx", "rsi", "rdi", "rbp", "rsp"} | {"r%d" % k for k in range(8, 16)}
GP32 = {"eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp"} | {"r%dd" % k for k in range(8, 16)}
GP8 = {"al", "bl", "cl", "dl", "ah", "bh", "ch", "dh", "sil", "dil", "bpl", "spl"} | \
      {"r%db" % k for k in range(8, 16)}
CALLEE = {"rbx", "rbp", "rsi", "rdi", "r12", "r13", "r14", "r15"} | \
         {"ebx", "ebp", "esi", "edi", "r12d", "r13d", "r14d", "r15d"} | \
         {"bl", "bh", "bpl", "sil", "dil", "r12b", "r13b", "r14b", "r15b"}


def regclass(r):
    """A region register as a bank: gp64 / gp32 / gp8 / sse / none."""
    x = (r or "").strip().strip('"').lower()
    if x in ("", "-", "?"):
        return "none"
    if x.startswith(("xmm", "fxmm", "ymm")):
        return "sse"
    if x in GP64:
        return "gp64"
    if x in GP32:
        return "gp32"
    if x in GP8:
        return "gp8"
    return "other"


def slotname(key):
    base, off = key
    return "%s%+d" % (base, off)


def slots(path):
    """A listing's frame accesses: (insns, pushes, {slot: [loads, stores, first, last]})."""
    n = push = 0
    out = collections.defaultdict(lambda: [0, 0, None, None])
    for line in open(path):
        m = LINE.match(line.rstrip())
        if not m:
            continue
        mn, ops = m.group(1).lower(), m.group(2)
        n += 1
        if mn in ("push", "pop"):
            push += 1
        if mn == "lea":                       # an address computation, not a frame access
            continue
        f = MEM.search(ops)
        if not f:
            continue
        base = ("r" if f.group(1) else "e") + f.group(2).lower()
        sign = f.group(3) or "+"
        val = f.group(4) or f.group(5) or "0"
        off = int(val, 16) if f.group(4) else int(val)
        if sign == "-":
            off = -off
        store = ops[f.end():].lstrip().startswith(",")
        s = out[(base, off)]
        if store:
            s[1] += 1
            if mn in RMW:                     # read-modify-write reads it too
                s[0] += 1
        else:
            s[0] += 1
        s[2] = n - 1 if s[2] is None else s[2]
        s[3] = n - 1
    return n, push, out


def pick_function(doc, func):
    fns = [f for f in doc.get("functions", []) if f.get("cands")]
    if func:
        hit = [f for f in fns if f.get("func") == func] or [f for f in fns if func in (f.get("func") or "")]
        if not hit:
            sys.exit("ncolspill: no function %r (have: %s)" % (
                func, ", ".join(f.get("func") or "?" for f in fns) or "none"))
        return hit[0]
    if len(fns) != 1:
        sys.exit("ncolspill: %d functions in the export; pass --func (%s)" % (
            len(fns), ", ".join(f.get("func") or "?" for f in fns) or "none"))
    return fns[0]


def sprio(p):
    """The priority as NColor compares it (int32, descending)."""
    if p is None:
        return None
    return p - (1 << 32) if p >= (1 << 31) else p


def summary(f):
    """NColor's split/spill summary for one function."""
    cs = f.get("cands") or []
    whole = [c for c in cs if len(c.get("regions") or []) == 1]
    split = [(i, c) for i, c in enumerate(cs) if len(c.get("regions") or []) > 1]
    none = [c for c in cs if not c.get("regions")]
    regions = sum(len(c.get("regions") or []) for c in cs)
    banks = collections.Counter(regclass(r.get("reg")) for c in cs for r in (c.get("regions") or []))
    out = ["candidates %d: %d whole, %d split, %d unallocated; %d regions" % (
        len(cs), len(whole), len(split), len(none), regions)]
    st = f.get("state")
    if st:
        out.append("max live %s tuple%s%s" % (
            st.get("maxlive"), "" if st.get("maxlive") == 1 else "s",
            "; speed mode" if st.get("speed") else ""))
    if f.get("given"):
        out.append("given back by NColPostspill: %s" % ", ".join(f["given"]))
    if banks:
        out.append("regions by bank: " + ", ".join("%s %d" % (b, banks[b]) for b in
                                                   ("gp64", "gp32", "gp8", "sse", "none", "other") if banks[b]))
    if split:
        w = max(len(c.get("name") or "?") for _, c in split)
        out.append("")
        out.append("split candidates (processing order: the export's order, NColor's buckets concatenated):")
        for k, (idx, c) in enumerate(split):
            rs = sorted(c["regions"], key=lambda r: r["first"])
            span = max(r["last"] - r["first"] for r in rs)
            main = max(rs, key=lambda r: r["last"] - r["first"]) if span > 0 else None
            parts = []
            for r in rs:
                mark = "*" if r is main else ""
                if r is not main and regclass(r.get("reg")) in ("gp64", "gp32", "gp8") and \
                        (r.get("reg") or "").lower() in CALLEE:
                    mark += " stolen"
                parts.append("%s %x..%x%s" % (r.get("reg") or "-", r["first"], r["last"], mark))
            out.append("  %2d  #%-3d %-*s  prio %-8s  %4x..%-4x  %s" % (
                k + 1, idx, w, c.get("name") or "?",
                sprio(c.get("prio")) if c.get("prio") is not None else "?", c.get("first"), c.get("last"),
                " | ".join(parts)))
        out.append("  (# = position in the trace; * = the main (longest) region; "
                   "stolen = a part in a must-save register)")
    if none:
        names = ", ".join((c.get("name") or "?") for c in none[:8])
        out.append("")
        out.append("unallocated (trace order): %s%s" % (
            names, "" if len(none) <= 8 else ", ... (%d more)" % (len(none) - 8)))
    return out


def frame_report(label, path, top):
    n, push, sl = slots(path)
    loads = sum(v[0] for v in sl.values())
    stores = sum(v[1] for v in sl.values())
    hot_l = sum(1 for v in sl.values() if v[0] >= 3)
    hot_s = sum(1 for v in sl.values() if v[1] >= 3)
    out = ["%s: %d insns, %d push/pop, %d frame slots, %d loads / %d stores" % (
        label, n, push, len(sl), loads, stores)]
    out.append("  reloaded (>=3 loads) %d slots; re-stored (>=3 stores) %d slots" % (hot_l, hot_s))
    out.append("  %-10s %5s %6s  %-13s %s" % ("slot", "loads", "stores", "insn span", "pattern"))
    for (key, v) in sorted(sl.items(), key=lambda kv: (-(kv[1][0] + kv[1][1]), kv[0])):
        if top <= 0:
            break
        top -= 1
        ld, st, first, last = v
        pat = "reload" if ld > st else ("spill" if st > ld else "even")
        out.append("  %-10s %5d %6d  %6d..%-6d %s" % (slotname(key), ld, st, first, last, pat))
    return dict(insns=n, push=push, slots=len(sl), loads=loads, stores=stores, hot_l=hot_l, hot_s=hot_s), out


def side_by_side(a, b):
    """Two frame summaries as a table (a = the original, b = the draft)."""
    rows = [("insns", "insns"), ("push/pop", "push"), ("frame slots", "slots"), ("loads", "loads"),
            ("stores", "stores"), ("slots >=3 loads", "hot_l"), ("slots >=3 stores", "hot_s")]
    w = max(len(r[0]) for r in rows)
    out = ["  %-*s  %8s  %8s  %8s" % (w, "", "first", "second", "delta")]
    for title, key in rows:
        x, y = a[key], b[key]
        out.append("  %-*s  %8d  %8d  %+8d" % (w, title, x, y, y - x))
    return out


def main(argv):
    ap = argparse.ArgumentParser(prog="ncolspill.py", usage=__doc__)
    ap.add_argument("export", nargs="?", help="c2ncol.py --export JSON")
    ap.add_argument("--func", help="the function (decorated name, or a part of it)")
    ap.add_argument("--asm", action="append", default=[], metavar="FILE", help="a listing (repeatable)")
    ap.add_argument("--top", type=int, default=12, help="frame slots per listing (default 12)")
    a = ap.parse_args(argv)
    if not a.export and not a.asm:
        ap.error("need an export JSON, a listing (--asm), or both")
    if a.export:
        import json
        f = pick_function(json.load(open(a.export)), a.func)
        print("## NColor: %s" % (f.get("func") or "?"))
        for line in summary(f):
            print(line)
        print()
    stats = []
    for path in a.asm:
        if not os.path.exists(path):
            sys.exit("ncolspill: no such listing %s" % path)
        s, out = frame_report(os.path.basename(path), path, a.top)
        stats.append(s)
        print("## frame: %s" % os.path.basename(path))
        for line in out:
            print(line)
        print()
    if len(stats) == 2:
        print("## frame side by side (first vs second)")
        for line in side_by_side(*stats):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
