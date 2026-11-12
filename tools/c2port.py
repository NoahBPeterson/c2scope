#!/usr/bin/env python3
"""Carry c2.dll function and data names from a build that has a PDB to one that has none.  Doc: docs/c2/port-1310.md

usage: c2port.py names <c2.dll> <c2.pdb> <out.csv>      names of a PDB'd build, at their final addresses
       c2port.py match <ref c2.dll> <ref names.csv> <target c2.dll> <out.csv>
       c2port.py table <target c2.dll> <match.csv> <tools/c2builds/BUILD.json> [--index work/index/BUILD/c2.dll.csv]

`names` reads the PDB's publics and, when the image was block-reordered after linking (BBT: the PDB then carries
OMAP streams and the publics are in the pre-BBT address space), translates each through OMAP_FROM_SRC.
Columns: rva (hex, final image), kind (function|data), symbol.

`match` finds each named reference function in the target build. Both images are explored by recursive descent
from every call target, so a function is its set of reachable instructions wherever BBT put the blocks. Each
instruction is reduced to its bytes with relocated dwords and branch displacements zeroed (branches keep only
"jcc"/"jmp"/"call"), and a function to the multiset of those. Matches come from, in this order of confidence:
  sig     the multiset is identical and unique on both sides
  calls   unique best multiset similarity among the unmatched callees (or callers) of an already matched pair
  near    best similarity overall, at least 0.6 and clearly ahead of the runner-up (not used for hooks unless
          confirmed by reading the code)
`table` writes the functions the tools hook (HOOKED below) into the build table's "functions" (rva, contiguous
size from the entry, confidence, how found); entries whose "how" starts with "manual" are kept. --index also
writes every matched function as an index csv for tools/image.py (so any carried-over name can be hooked).

Data symbols are then mapped by pairing instructions of matched functions that are unique in both and carry a
relocated address; a data row lists the votes. Columns: symbol, kind, ref_rva, rva, how, score, votes.
"""
import bisect, collections, csv, os, re, struct, subprocess, sys

PDBUTIL = next((c for c in (os.environ.get("LLVM_PDBUTIL"), "/opt/homebrew/opt/llvm/bin/llvm-pdbutil",
                            "/usr/local/opt/llvm/bin/llvm-pdbutil") if c and os.path.exists(c)), "llvm-pdbutil")


def msf_streams(path):
    d = open(path, "rb").read()
    if not d.startswith(b"Microsoft C/C++ MSF 7.00"):
        sys.exit("%s: not an MSF 7.00 PDB" % path)
    bs, _fpm, _np, dirsize, _, dirmap = struct.unpack_from("<6I", d, 0x20)
    ndp = (dirsize + bs - 1) // bs
    dr = b"".join(d[p * bs:(p + 1) * bs] for p in struct.unpack_from("<%dI" % ndp, d, dirmap * bs))[:dirsize]
    n = struct.unpack_from("<I", dr, 0)[0]
    sizes = struct.unpack_from("<%dI" % n, dr, 4)
    o, out = 4 + 4 * n, []
    for s in sizes:
        if s == 0xffffffff:
            out.append(b"")
            continue
        k = (s + bs - 1) // bs
        pg = struct.unpack_from("<%dI" % k, dr, o)
        o += 4 * k
        out.append(b"".join(d[p * bs:(p + 1) * bs] for p in pg)[:s])
    return out


def pdb_names(pdb):
    """[(final rva, kind, symbol)]: publics, through OMAP_FROM_SRC when the PDB has one."""
    st = msf_streams(pdb)
    dbi = st[3]
    h = struct.unpack_from("<iIIHHHHHHiiiiiIiiHHI", dbi, 0)
    modsz, scsz, secmapsz, filesz, tssz, dbghdrsz, ecsz = h[9], h[10], h[11], h[12], h[13], h[15], h[16]
    idx = struct.unpack_from("<%dH" % (dbghdrsz // 2), dbi, 64 + modsz + scsz + secmapsz + filesz + tssz + ecsz)
    def stream(k):
        return st[idx[k]] if k < len(idx) and idx[k] != 0xffff else b""
    omap = stream(4)
    hdrs = stream(10) or stream(5)          # original section headers if reordered, else the image's
    secs = [struct.unpack_from("<II", hdrs, 40 * k + 8)[::-1] for k in range(len(hdrs) // 40)]   # (va, vsize)
    om = [struct.unpack_from("<II", omap, 8 * k) for k in range(len(omap) // 8)]
    keys = [a for a, _ in om]
    def tr(rva):
        if not om:
            return rva
        k = bisect.bisect_right(keys, rva) - 1
        if k < 0 or om[k][1] == 0:
            return None
        return om[k][1] + (rva - om[k][0])
    txt = subprocess.run([PDBUTIL, "dump", "-publics", pdb], capture_output=True, text=True, check=True).stdout
    out = []
    for m in re.finditer(r"S_PUB32 \[size = \d+\] `([^`]*)`\s*\n\s*flags = ([^,]*), addr = (\d+):(\d+)", txt):
        s, o = int(m.group(3)), int(m.group(4))
        if not 1 <= s <= len(secs):
            continue
        rva = tr(secs[s - 1][0] + o)
        if rva is not None:
            out.append((rva, "function" if "function" in m.group(2) else "data", m.group(1)))
    return sorted(out)


class Img:
    def __init__(self, path):
        import pefile
        pe = pefile.PE(path, fast_load=True)
        self.base = pe.OPTIONAL_HEADER.ImageBase
        self.mem = bytes(pe.get_memory_mapped_image())
        t = pe.sections[0]
        self.tlo, self.thi = t.VirtualAddress, t.VirtualAddress + t.Misc_VirtualSize
        self.entry = pe.OPTIONAL_HEADER.AddressOfEntryPoint
        self.rel = set()
        d = pe.OPTIONAL_HEADER.DATA_DIRECTORY[5]
        raw, o = self.mem[d.VirtualAddress:d.VirtualAddress + d.Size], 0
        while o + 8 <= len(raw):
            page, size = struct.unpack_from("<II", raw, o)
            if size < 8:
                break
            for e in struct.unpack_from("<%dH" % ((size - 8) // 2), raw, o + 8):
                if e >> 12 == 3:
                    self.rel.add(page + (e & 0xfff))
            o += size
        self.relsorted = sorted(self.rel)
        pe.parse_data_directories(directories=[0])
        self.exports = [e.address for e in getattr(pe, "DIRECTORY_ENTRY_EXPORT", None).symbols] \
            if hasattr(pe, "DIRECTORY_ENTRY_EXPORT") else []
        self.ins = {}        # rva -> (size, sig bytes, kind, target)   kind: 0 plain, 1 jcc, 2 jmp, 3 call, 4 ret, 5 ijmp
        import capstone
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)

    def code(self, rva):
        return self.tlo <= rva < self.thi

    def dword(self, rva):
        return struct.unpack_from("<I", self.mem, rva)[0]

    def decode(self, rva):
        """Decode forward from rva until an instruction that ends a block; cache every instruction."""
        out = []
        while self.code(rva):
            if rva in self.ins:
                i = self.ins[rva]
            else:
                g = next(self.md.disasm_lite(self.mem[rva:rva + 16], rva), None)
                if g is None:
                    break
                _a, size, mn, _ops = g
                b = self.mem[rva:rva + size]
                kind, tgt = 0, None
                if b[0] == 0xe8:
                    kind, tgt = 3, rva + 5 + struct.unpack_from("<i", b, 1)[0]
                elif b[0] == 0xe9:
                    kind, tgt = 2, rva + 5 + struct.unpack_from("<i", b, 1)[0]
                elif b[0] == 0xeb:
                    kind, tgt = 2, rva + 2 + struct.unpack_from("<b", b, 1)[0]
                elif 0x70 <= b[0] <= 0x7f or 0xe0 <= b[0] <= 0xe3:
                    kind, tgt = 1, rva + 2 + struct.unpack_from("<b", b, 1)[0]
                elif b[0] == 0x0f and 0x80 <= b[1] <= 0x8f:
                    kind, tgt = 1, rva + 6 + struct.unpack_from("<i", b, 2)[0]
                elif mn in ("ret", "retf", "int3", "iretd"):
                    kind = 4
                elif mn == "jmp":
                    kind = 5
                if kind in (1, 2, 3):
                    sig = (b"J", b"j", b"c")[kind - 1]      # BBT inverts conditions and adds/removes jumps
                else:
                    sb = bytearray(b)
                    k = bisect.bisect_left(self.relsorted, rva)
                    while k < len(self.relsorted) and self.relsorted[k] <= rva + size - 4:
                        o = self.relsorted[k] - rva
                        sb[o:o + 4] = b"\0\0\0\0"
                        k += 1
                    sig = bytes(sb)
                i = self.ins[rva] = (size, sig, kind, tgt)
            out.append((rva, i))
            if i[2] in (2, 4, 5):
                break
            rva += i[0]
        return out

    def jumptable(self, rva, size):
        """Targets of `jmp [reg*4 + table]`: consecutive relocated code pointers at table."""
        b = self.mem[rva:rva + size]
        if size == 7 and b[0] == 0xff and b[1] == 0x24 and (rva + 3) in self.rel:
            t, out, holes = self.dword(rva + 3) - self.base, [], 0
            while holes < 8:                 # a table can have null slots (no relocation, value 0)
                if t in self.rel and self.code(self.dword(t) - self.base):
                    out.append(self.dword(t) - self.base)
                    holes = 0
                elif self.dword(t) == 0 and t not in self.rel:
                    holes += 1
                else:
                    break
                t += 4
            return out
        return []

    def explore(self):
        """Function entries = call targets (found transitively) + exports + relocated code pointers that are not
        jump-table slots. -> {entry: (sig multiset Counter, [callee entries in visit order], set of block rvas)}"""
        entries = set(self.exports) | {self.entry}
        for r in self.rel:                                  # code pointers stored in data (vtables, tables)
            if not self.code(r):
                v = self.dword(r) - self.base
                if self.code(v):
                    entries.add(v)
        work, funcs = list(entries), {}
        seen_entry = set(entries)
        while work:
            e = work.pop()
            if e in funcs:
                continue
            sig, calls, blocks, todo, visited = collections.Counter(), [], set(), [e], set()
            while todo:
                a = todo.pop()
                if a in visited or not self.code(a):
                    continue
                run = self.decode(a)
                blocks.add(a)
                for rva, (size, s, kind, tgt) in run:
                    if rva in visited:
                        break
                    visited.add(rva)
                    sig[s] += 1
                    if kind == 3:
                        calls.append(tgt)
                        if tgt not in seen_entry and self.code(tgt):
                            seen_entry.add(tgt)
                            work.append(tgt)
                    elif kind in (1, 2):
                        if kind == 2 and tgt in seen_entry and tgt != e:
                            calls.append(tgt)               # tail call
                        else:
                            todo.append(tgt)
                    elif kind == 5:
                        todo.extend(self.jumptable(rva, size))
            funcs[e] = (sig, calls, visited)
        # a "function" that is only ever reached as a jump target inside another one is that one's block; keep it
        # anyway: harmless for matching (it never gets a name)
        self.funcs = funcs
        return funcs


def sim(a, b):
    inter = sum((a & b).values())
    tot = max(sum(a.values()), sum(b.values()))
    return inter / tot if tot else 0.0


def match(refp, namesp, tgtp, outp):
    ref, tgt = Img(refp), Img(tgtp)
    names = [(int(r["rva"], 16), r["kind"], r["symbol"]) for r in csv.DictReader(open(namesp))]
    fnames = {}
    for rva, kind, sym in names:
        if kind == "function" and ref.code(rva):
            fnames.setdefault(rva, sym)
    ref.exports = list(set(ref.exports) | set(fnames))
    rf, tf = ref.explore(), tgt.explore()
    key = lambda f: hash(frozenset(f[0].items()))
    rk, tk = collections.defaultdict(list), collections.defaultdict(list)
    for e, f in rf.items():
        rk[key(f)].append(e)
    for e, f in tf.items():
        tk[key(f)].append(e)
    m, how = {}, {}                      # ref entry -> target entry
    for k, es in rk.items():
        if len(es) == 1 and len(tk.get(k, ())) == 1 and sum(rf[es[0]][0].values()) >= 8:
            m[es[0]] = tk[k][0]
            how[es[0]] = ("sig", 1.0)
    used = set(m.values())
    callers_r, callers_t = collections.defaultdict(set), collections.defaultdict(set)
    for e, f in rf.items():
        for c in f[1]:
            callers_r[c].add(e)
    for e, f in tf.items():
        for c in f[1]:
            callers_t[c].add(e)
    changed = True
    while changed:
        changed = False
        for e in list(m):
            for rset, tset in ((set(rf[e][1]), set(tf[m[e]][1])), (callers_r[e], callers_t[m[e]])):
                ru = [x for x in rset if x not in m and x in rf]
                tu = [x for x in tset if x not in used and x in tf]
                if not ru or not tu:
                    continue
                sc = sorted(((sim(rf[a][0], tf[b][0]), a, b) for a in ru for b in tu), reverse=True)
                for s, a, b in sc:
                    if a in m or b in used:
                        continue
                    rival = max([s2 for s2, a2, b2 in sc if (a2 == a) != (b2 == b) and a2 not in m and b2 not in used] or [0])
                    if s >= 0.5 and s - rival >= 0.1 or (len(ru) == 1 and len(tu) == 1 and s >= 0.3):
                        m[a] = b
                        used.add(b)
                        how[a] = ("calls", s)
                        changed = True
    # leftovers among named functions: global best similarity
    left_t = [b for b in tf if b not in used and sum(tf[b][0].values()) >= 12]
    for a in sorted(fnames):
        if a in m or a not in rf or sum(rf[a][0].values()) < 12:
            continue
        na = sum(rf[a][0].values())
        sc = sorted(((sim(rf[a][0], tf[b][0]), b) for b in left_t if b not in used
                     and 0.6 * na <= sum(tf[b][0].values()) <= na / 0.6), reverse=True)[:2]
        if sc and sc[0][0] >= 0.6 and (len(sc) == 1 or sc[0][0] - sc[1][0] >= 0.15):
            m[a] = sc[0][1]
            used.add(sc[0][1])
            how[a] = ("near", sc[0][0])
    # data: unique instructions with relocated operands in matched pairs
    votes = collections.defaultdict(collections.Counter)
    for a, b in m.items():
        ia, ib = collections.defaultdict(list), collections.defaultdict(list)
        for img, e, d in ((ref, a, ia), (tgt, b, ib)):
            for rva in img.funcs[e][2]:
                size, s, kind, _t = img.ins[rva]
                if kind == 0 and size >= 5:
                    d[s].append(rva)
        for s, la in ia.items():
            lb = ib.get(s, ())
            if len(la) == 1 and len(lb) == 1:
                size = ref.ins[la[0]][0]
                for o in range(size - 3):
                    if la[0] + o in ref.rel and lb[0] + o in tgt.rel:
                        votes[ref.dword(la[0] + o) - ref.base][tgt.dword(lb[0] + o) - tgt.base] += 1
    dnames = collections.defaultdict(list)
    for rva, kind, sym in names:
        if not ref.code(rva):
            dnames[rva].append(sym)
    dstarts = sorted(dnames)
    rows = []
    for a in sorted(fnames):
        if a in m:
            rows.append((fnames[a], "function", a, m[a], how[a][0], "%.2f" % how[a][1], ""))
        else:
            rows.append((fnames[a], "function", a, None, "none", "", ""))
    drow = {}
    for v, c in votes.items():
        if ref.code(v):
            continue
        k = bisect.bisect_right(dstarts, v) - 1
        if k < 0:
            continue
        s0 = dstarts[k]
        (t, n), tot = c.most_common(1)[0], sum(c.values())
        if v == s0:
            drow[s0] = (t, n, tot, "exact")
        elif s0 not in drow or drow[s0][3] != "exact" and n > drow[s0][1]:
            drow[s0] = (t - (v - s0), n, tot, "offset+0x%x" % (v - s0))
    for s0 in dstarts:
        for sym in dnames[s0]:
            if s0 in drow:
                t, n, tot, h = drow[s0]
                rows.append((sym, "data", s0, t, h, "%.2f" % (n / tot), "%d/%d" % (n, tot)))
    with open(outp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "kind", "ref_rva", "rva", "how", "score", "votes"])
        for sym, kind, a, b, h, s, v in rows:
            w.writerow([sym, kind, "%x" % a, "%x" % b if b is not None else "", h, s, v])
    c = collections.Counter(r[4] for r in rows if r[1] == "function")
    print("%s: %d ref functions explored, %d target; named functions: %s; data symbols mapped: %d" % (
        os.path.basename(outp), len(rf), len(tf), dict(c), sum(1 for r in rows if r[1] == "data")))


# every c2 function the tracing tools hook by name (c2regs, c2scratch, c2il default passes, c2sched, tests)
HOOKED = """@Color@4 @OrderGlobregs@12 @ChoosePhysReg@12 @SpillOrDefer@12 @HandleCurrUncolorable@8 @Spillcosts@12
@InsertInList@8 @InsertByBenefit@8 @SpillObvious@16 @InsertNewGlobregs@8 @InsertSplitPoints@28
@RegSimpleAssign@4 @RegAssign@8 @CmdSetNextPos@8 @Encode@4 @EncSetOffsets@12
@dop2_PreLowerGlobalOpts@4 @dop2_Codegen@4 @dop2_PostLowerPreRegAllocGlobalOpts@4 @dop2_GlobalRegAlloc@4
@FgEarlyTailDup@4 @Globregs@4 @GrgIdentifyCandidates@4 @dop2_CodegenUpdate@4 @dop2_CodegenFinish@4
@dop2_FinalFlowOpts@4 @FgCodeMotion@4 @FgCoalesceLabels@4 @dop2_PreEncode@4 @MscClean@4 @MdmSuperHack@4
@dop2_EncodeAndRelated@4 @EncEmitFunc@4 @Schedule@8 @SchSchedGraph@4 @SchAddToReadyList@4 @SchSelectInstrs@4
@SchComputeStaticPriority@4 @RegResetRegMap@4 @RegSpill@12 @RegFixConflict@8 @RegBuildGlobalConflict@4
@FPColor@4 @FPSpillcosts@8 @FPAddToPriorityList@8 @FPSpillWorthless@12
@FPCheckColorability@12""".split()


def extent(img, e):
    """Bytes of contiguous code from entry e that belong to the function (its blocks may continue elsewhere)."""
    seen, todo = {}, [e]
    while todo:
        a = todo.pop()
        if a in seen or not img.code(a):
            continue
        for rva, (size, _s, kind, tgt) in img.decode(a):
            if rva in seen:
                break
            seen[rva] = size
            if kind == 1:
                todo.append(tgt)
            elif kind == 2 and abs(tgt - e) < 0x400:
                todo.append(tgt)
            elif kind == 5:
                todo.extend(img.jumptable(rva, size))
    n = 0
    while e + n in seen:
        n += seen[e + n]
    return n


def table(tgtp, matchp, jsonp, index=None):
    import json
    img = Img(tgtp)
    rows = {r["symbol"]: r for r in csv.DictReader(open(matchp)) if r["kind"] == "function" and r["rva"]}
    t = json.load(open(jsonp))
    fn = {k: v for k, v in t.get("functions", {}).items() if str(v[3]).startswith("manual")}
    for name in HOOKED:
        if name in fn:
            continue
        r = rows.get(name)
        if not r:
            print("  not matched: %s" % name)
            continue
        rva = int(r["rva"], 16)
        conf = "high" if r["how"] == "sig" or float(r["score"] or 0) >= 0.9 else "medium"
        fn[name] = ["%x" % rva, extent(img, rva), conf, "%s %s from 6030 rva %s" % (r["how"], r["score"], r["ref_rva"])]
    t["functions"] = dict(sorted(fn.items(), key=lambda kv: int(kv[1][0], 16)))
    json.dump(t, open(jsonp, "w"), indent=1)
    print("%s: %d functions" % (jsonp, len(fn)))
    if index:
        os.makedirs(os.path.dirname(index), exist_ok=True)
        with open(index, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["va", "size", "size_src", "module", "obj", "flags", "symbol"])
            for name, r in sorted(rows.items(), key=lambda kv: int(kv[1]["rva"], 16)):
                if r["how"] == "near":
                    continue
                rva = int(r["rva"], 16)
                w.writerow(["%08x" % (img.base + rva), extent(img, rva), r["how"], "", "", "function", name])
        print("index -> %s" % index)


def main():
    a = sys.argv[1:]
    if a[:1] == ["table"] and len(a) in (4, 6):
        return table(a[1], a[2], a[3], a[5] if len(a) == 6 and a[4] == "--index" else None)
    if a[:1] == ["names"] and len(a) == 4:
        rows = pdb_names(a[2])
        with open(a[3], "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["rva", "kind", "symbol"])
            for rva, kind, sym in rows:
                w.writerow(["%x" % rva, kind, sym])
        print("%d publics -> %s" % (len(rows), a[3]))
    elif a[:1] == ["match"] and len(a) == 5:
        match(a[1], a[2], a[3], a[4])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
