#!/usr/bin/env python3
"""Benchmark corpora: one program's functions, described so that any tool can be scored on any MSVC-built binary.

usage: corpus.py spore [--out FILE]                 Spore (cl 15.00 + 13.10 rows) from spore-decomp's manifests
       corpus.py mb    [--out FILE]                 Mount & Blade 1.011 (cl 13.10.3052) from mountandblade-decomp
       corpus.py fnv   --image EXE [--out FILE]     Fallout: New Vegas 1.4.0.525 (cl 15.00) from fnv_decomp's units.json
       corpus.py f3    --image EXE [--out FILE]     Fallout 3 1.7.0.3 (cl 14.00) from f3_decomp's units.json
       corpus.py stats FILE                         counts by status, compiler build and size band
       corpus.py export FILE [--status S,S] [--out F]   a JSON VA list (default: the residual, no source at all)
       corpus.py maskcheck FILE [--n N]             generic mask (from the image) against the corpus's own masks

A corpus is a JSONL file (work/corpus/<name>.jsonl). Line 1 is the header:
  {"corpus": NAME, "image": PATH, "arch": "x86", "c2": DEFAULT_BUILD, "flags": DEFAULT_FLAGS}
Every other line is one function:
  {"va": "0092c270", "size": N, "status": S, "c2": BUILD?, "flags": FLAGS?, "src": PATH?, "sym": NAME?,
   "mask": [[off, len], ..], "fixups": [off, ..]}
status: matched (a source compiles to the original's bytes), equivalent (proven equivalent), nonmatching (a full
source that still differs), partial (a stub), residual (no source at all). c2 and flags default to the header's.
mask: the byte offsets the compiler emits as relocations (addresses, calls leaving the function), compared only
for being relocated; fixups: the offsets retail's .reloc table fixes up, which must be relocations in ours too
(a number where the original has an address does not count). Both are the spore-decomp fingerprint rules
(tools/ci/fingerprint.py), computed here from the image for programs that have no fingerprints.

Nothing of the image is written: addresses, sizes and mask offsets only.
"""
import argparse, bisect, collections, glob, json, os, struct, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROJ = os.path.dirname(ROOT)
OUTDIR = os.path.join(ROOT, "work", "corpus")


# ---- the image ----

class Image:
    """A PE image: bytes by VA, the .reloc fixups (RVAs), and the generic relocation mask of a function."""

    def __init__(self, path):
        import pefile
        self.path = path
        self.pe = pefile.PE(path, fast_load=True)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.relocs = []
        d = self.pe.OPTIONAL_HEADER.DATA_DIRECTORY[5]
        if d.VirtualAddress and d.Size:
            raw = self.pe.get_data(d.VirtualAddress, d.Size)
            k = 0
            while k + 8 <= len(raw):                      # IMAGE_BASE_RELOCATION blocks
                page, blk = struct.unpack_from("<II", raw, k)
                if blk < 8:
                    break
                for j in range(k + 8, k + blk, 2):
                    (e,) = struct.unpack_from("<H", raw, j)
                    if e >> 12 == 3:                      # HIGHLOW
                        self.relocs.append(page + (e & 0xFFF))
                k += blk
            self.relocs.sort()
        ends = [s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData) for s in self.pe.sections]
        self.lo, self.hi = self.base, self.base + max(ends)
        self._md = None

    def md(self):
        if self._md is None:
            import capstone
            self._md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
            self._md.detail = True
        return self._md

    def bytes_at(self, va, n):
        return self.pe.get_data(va - self.base, n)

    def fixups(self, va, size):
        rva = va - self.base
        i = bisect.bisect_left(self.relocs, rva)
        out = []
        while i < len(self.relocs) and self.relocs[i] + 4 <= rva + size:
            out.append(self.relocs[i] - rva)
            i += 1
        return out

    def mask(self, va, size):
        """spore-decomp fingerprint.make's mask: .reloc fixups; 4-byte immediates/displacements holding an image
        address or read through fs:; rel32 calls/jumps that leave the function or target its own start."""
        data = self.bytes_at(va, size)
        m = set()
        for o in self.fixups(va, size):
            m |= {o, o + 1, o + 2, o + 3}
        for ins in self.md().disasm(bytes(data), va):
            b = ins.bytes
            fs = 0x64 in ins.prefix
            for o, n in ((ins.disp_offset, ins.disp_size), (ins.imm_offset, ins.imm_size)):
                if o and n == 4:
                    v = int.from_bytes(b[o:o + 4], "little")
                    if self.lo <= v < self.hi or (fs and o == ins.disp_offset):
                        a = ins.address - va + o
                        m |= {a, a + 1, a + 2, a + 3}
            if (b[0] in (0xE8, 0xE9) and ins.size == 5) or (b[0] == 0x0F and 0x80 <= b[1] <= 0x8F and ins.size == 6):
                if not va < int(ins.op_str, 16) < va + size:
                    o = ins.address - va + ins.size - 4
                    m |= {o, o + 1, o + 2, o + 3}
        return m


def pack(mask):
    out = []
    for i in sorted(mask):
        if out and out[-1][0] + out[-1][1] == i:
            out[-1][1] += 1
        else:
            out.append([i, 1])
    return out


def unpack(runs):
    return {s + k for s, n in runs for k in range(n)}


# ---- reading a corpus ----

class Corpus:
    def __init__(self, path):
        with open(path) as f:
            self.head = json.loads(f.readline())
            self.rows = [json.loads(l) for l in f if l.strip()]
        self.path, self.name = path, self.head["corpus"]
        for r in self.rows:
            r.setdefault("c2", self.head["c2"])
            r.setdefault("flags", self.head["flags"])
        self._img = None

    def image(self):
        if self._img is None:
            self._img = Image(self.head["image"])
        return self._img

    def select(self, status=None, max_size=None, min_size=None, c2=None):
        out = []
        for r in self.rows:
            if status and r["status"] not in status:
                continue
            if max_size and r["size"] > max_size or min_size and r["size"] < min_size:
                continue
            if c2 and r["c2"] != c2:
                continue
            out.append(r)
        return out


def write(path, head, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dflt_c2, dflt_fl = head["c2"], head["flags"]
    with open(path, "w") as f:
        f.write(json.dumps(head) + "\n")
        for r in sorted(rows, key=lambda r: r["va"]):
            r = dict(r)
            if r.get("c2") == dflt_c2:
                r.pop("c2")
            if r.get("flags") == dflt_fl:
                r.pop("flags")
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    print("wrote %s: %d functions" % (path, len(rows)))


def inc_flags(flags, bases):
    """The /I options of a manifest row, made absolute against the first base where the directory exists."""
    out = []
    for f in flags:
        if f[:2] in ("/I", "-I") and len(f) > 2:
            rel = f[2:]
            for b in bases:
                c = rel if os.path.isabs(rel) else os.path.join(b, rel)
                if os.path.isdir(c):
                    out.append("/I" + os.path.abspath(c))
                    break
    return out


def norm_flags(flags):
    """(c2 build override or None, flags string): /vc71 selects cl 13.10; include paths do not matter to a lifted
    unit (it is self-contained) and are dropped."""
    c2, keep = None, []
    for f in flags:
        if f == "/vc71":
            c2 = "13.10"
        elif f[:2] in ("/I", "-I"):
            continue
        else:
            keep.append(f)
    return c2, " ".join(keep)


# ---- Spore ----

def build_spore(a):
    sp = os.path.join(PROJ, "spore-decomp")
    dflt = ["/O2", "/MD", "/Gy", "/EHsc", "/TP"]
    fps = {}
    for line in open(os.path.join(sp, "tools", "ci", "fingerprints.jsonl")):
        j = json.loads(line)
        fps[j["va"]] = j
    status, info = {}, {}
    rank = {"matched": 4, "equivalent": 3, "nonmatching": 2, "partial": 1}

    def note(va, st, src=None, sym=None, flags=None):
        if rank[st] > rank.get(status.get(va), 0):
            status[va] = st
            info[va] = (src, sym, flags)

    mans = glob.glob(os.path.join(sp, "match", "**", "manifest.txt"), recursive=True)
    mans += glob.glob(os.path.join(sp, "src", "game", "**", "manifest.txt"), recursive=True)
    mans += [p for p in [os.path.join(sp, "match", "clones.txt")] if os.path.exists(p)]
    mans += glob.glob(os.path.join(sp, "match", "synth", "*.manifest"))
    for m in mans:
        for line in open(m):
            p = line.split("#", 1)[0].split()
            if len(p) >= 3:
                note("%08x" % int(p[2], 16), "matched", os.path.normpath(os.path.join(sp, "match", p[0])), p[1],
                     tuple(p[3:]) or tuple(dflt))
    for m in glob.glob(os.path.join(sp, "match", "**", "equivalent.txt"), recursive=True) + \
            glob.glob(os.path.join(sp, "src", "game", "**", "equivalent.txt"), recursive=True):
        for line in open(m):                       # manifest format: source symbol va [flags]
            p = line.split("#", 1)[0].split()
            if len(p) >= 3:
                note("%08x" % int(p[2], 16), "equivalent")
    for name, st in (("nonmatching.txt", "nonmatching"), ("partial.txt", "partial")):
        for m in glob.glob(os.path.join(sp, "match", "**", name), recursive=True) + \
                glob.glob(os.path.join(sp, "src", "game", "**", name), recursive=True):
            for line in open(m):
                p = line.split("#", 1)[0].split()
                if p and all(c in "0123456789abcdefABCDEFx" for c in p[0]):
                    try:
                        va = "%08x" % int(p[0], 16)
                    except ValueError:
                        continue
                    note(va, st)
    # the open-source libraries are rebuilt from their sources, not decompiled: out of the benchmark
    # the open-source libraries are rebuilt from their sources, not decompiled: out of the benchmark.
    # modules.json: "modules" names, "runs" [start VA, module index] (a run lasts to the next start)
    oss = []
    mj = os.path.join(sp, "tools", "site", "modules.json")
    if os.path.exists(mj):
        d = json.load(open(mj))
        names, runs = d["modules"], d["runs"]
        for (s0, k), nxt in zip(runs, runs[1:] + [["ffffffff", -1]]):
            if "open-source" in names[k].lower():
                oss.append((int(s0, 16), int(nxt[0], 16)))
    markers = {int(k, 16): v for k, v in spore_resolver("markers").items()}
    rows = []
    for va, fp in fps.items():
        st = status.get(va, "residual")
        v = int(va, 16)
        if any(lo <= v < hi for lo, hi in oss):
            continue
        r = {"va": va, "size": fp["size"], "status": st, "mask": fp["mask"], "fixups": fp.get("fixups", [])}
        src, sym, flags = info.get(va, (None, None, None))
        c2, fl = norm_flags(flags or dflt)
        if c2:
            r["c2"] = c2
        r["flags"] = fl
        if src:
            r["src"], r["sym"] = src, sym
            inc = inc_flags(flags or (), [os.path.join(sp, "work", "match"), sp])
            if inc:
                r["inc"] = inc
        elif st in ("nonmatching", "partial", "equivalent") and v in markers:
            # a source without a manifest row: the marker's file, its flags as check.py chooses them; the symbol is
            # found in the object at scoring time (spore-decomp slice.find_symbol: resolver "spore")
            f, ident, fl = markers[v]
            fl = fl or dflt
            c2, flt = norm_flags(fl)
            r["flags"] = flt
            if c2:
                r["c2"] = c2
            r["src"], r["ident"] = f, ident
            inc = inc_flags(fl, [os.path.join(sp, "work", "match"), sp])
            if inc:
                r["inc"] = inc
        rows.append(r)
    head = {"corpus": "spore", "resolver": "spore", "src_inc": ["/I" + os.path.join(sp, "match", "include")], "image": os.path.join(sp, "work", "SporeApp.analysis.bin"), "arch": "x86",
            "c2": "15.00", "flags": " ".join(dflt), "oss_runs": len(oss)}
    write(a.out or os.path.join(OUTDIR, "spore.jsonl"), head, rows)


def spore_resolver(cmd, stdin=None):
    import subprocess
    py = os.path.join(PROJ, "spore-decomp", ".venv", "bin", "python")
    r = subprocess.run([py if os.path.exists(py) else sys.executable, os.path.join(HERE, "spore_resolve.py"), cmd],
                       input=stdin, capture_output=True, text=True, cwd=os.path.join(PROJ, "spore-decomp"))
    if r.returncode:
        sys.exit("spore_resolve.py %s failed:\n%s" % (cmd, r.stderr[-2000:]))
    return json.loads(r.stdout)


def resolve_symbols(head, jobs):
    """Decorated names for rows that name none: [(row, obj)] -> [sym|None] (resolver hook per corpus)."""
    if head.get("resolver") == "spore" and jobs:
        return spore_resolver("symbols", json.dumps([[o, r["ident"], r["va"], r["src"], r["size"]] for r, o in jobs]))
    return [None] * len(jobs)


# ---- Fallout 3 / Fallout: New Vegas (momusdigital's build/units.json recipes) ----

UNIT_REPOS = {
    "f3": ("f3_decomp", {"vs2005": "14.00"}, "14.00"),
    "fnv": ("fnv_decomp", {"sp1": "15.00", "rtm": "15.00-rtm", "vc8": "14.00"}, "15.00"),
}


def build_units(a):
    """Every function of every unit: matched by construction (the recipe lists only byte-exact ones)."""
    repo, c2map, dflt_c2 = UNIT_REPOS[a.cmd]
    fd = os.path.join(PROJ, repo)
    units = json.load(open(os.path.join(fd, "build", "units.json")))["units"]
    img = Image(a.image)
    rows, seen = [], set()
    for u in units:
        _c2, fl = norm_flags(u.get("flags", []))
        c2 = c2map.get(u.get("compiler"), u.get("compiler"))
        for f in u["functions"]:
            v = int(f["address"], 16)
            if v in seen:
                continue
            seen.add(v)
            rows.append({"va": "%08x" % v, "size": f["size"], "status": "matched", "c2": c2, "flags": fl,
                         "src": os.path.join(fd, u["source"]), "sym": f["symbol"],
                         "mask": pack(img.mask(v, f["size"])), "fixups": img.fixups(v, f["size"])})
    head = {"corpus": a.cmd, "src_inc": ["/I" + os.path.join(fd, "include")], "image": os.path.abspath(a.image), "arch": "x86", "c2": dflt_c2,
            "flags": "/MT /O2 /GS /EHsc /Gy"}
    write(a.out or os.path.join(OUTDIR, a.cmd + ".jsonl"), head, rows)


# ---- Mount & Blade ----

def build_mb(a):
    """Every function start in Ghidra's index (minus symbols/not_functions.txt), sized to the next start with the
    int3 padding dropped; status from the manifests, nonmatching.txt and partial.txt."""
    import csv
    md = os.path.join(PROJ, "mountandblade-decomp")
    image = os.path.join(md, "work", "mb.analysis.bin")
    img = Image(image)
    notf = set()
    for line in open(os.path.join(md, "symbols", "not_functions.txt")):
        p = line.split("#", 1)[0].split()
        if p:
            notf.add(int(p[0], 16))
    starts = sorted({int(r["address"], 16) for r in csv.DictReader(open(os.path.join(md, "work", "index", "functions.csv")))
                     if r.get("block", ".text") == ".text"} - notf)
    dflt = ["/O2", "/MD", "/Gy", "/EHsc", "/TP"]
    status, info = {}, {}
    rank = {"matched": 4, "nonmatching": 2, "partial": 1}
    for m in glob.glob(os.path.join(md, "match", "**", "manifest.txt"), recursive=True):
        for line in open(m):
            p = line.split("#", 1)[0].split()
            if len(p) >= 3:
                try:
                    v = int(p[2], 16)
                except ValueError:
                    continue
                status[v] = "matched"
                info[v] = (os.path.normpath(os.path.join(md, "match", p[0])), p[1], tuple(p[3:]) or tuple(dflt))
    for name, st in (("nonmatching.txt", "nonmatching"), ("partial.txt", "partial")):
        for m in glob.glob(os.path.join(md, "match", "**", name), recursive=True):
            for line in open(m):
                p = line.split("#", 1)[0].split()
                try:
                    v = int(p[0], 16) if p else None
                except ValueError:
                    v = None
                if v is not None and rank[st] > rank.get(status.get(v), 0):
                    status[v] = st
    rows = []
    for v, nxt in zip(starts, starts[1:] + [None]):
        if nxt is None or nxt - v > 0x40000:
            continue
        d = bytes(img.bytes_at(v, nxt - v)).rstrip(b"\xcc")
        n = len(d)
        if n == 0:
            continue
        r = {"va": "%08x" % v, "size": n, "status": status.get(v, "residual"),
             "mask": pack(img.mask(v, n)), "fixups": img.fixups(v, n)}
        if v in info:
            src, sym, flags = info[v]
            r["flags"] = norm_flags(flags)[1]
            r["src"], r["sym"] = src, sym
        rows.append(r)
    # mountandblade-decomp tools/matching/cl.sh's include order; the DirectX SDK last (only d3dx9*.h comes from it)
    inc = [os.path.join(md, "src"), os.path.join(md, "match", "include"), os.path.join(md, "work", "toolchain", "sal"),
           os.path.join(md, "work", "ext", "dxsdk_jun10", "DXSDK", "Include")]
    head = {"corpus": "mb", "src_inc": ["/I" + p for p in inc if os.path.isdir(p)], "image": image, "arch": "x86", "c2": "13.10.3052", "flags": " ".join(dflt)}
    write(a.out or os.path.join(OUTDIR, "mb.jsonl"), head, rows)


# ---- reports ----

def stats(a):
    c = Corpus(a.file)
    by = collections.Counter((r["status"], r["c2"]) for r in c.rows)
    band = collections.Counter()
    for r in c.rows:
        n = r["size"]
        b = "<=64" if n <= 64 else "65-256" if n <= 256 else "257-1K" if n <= 1024 else ">1K"
        band[(r["status"], b)] += 1
    print("%s: %d functions, image %s" % (c.name, len(c.rows), c.head["image"]))
    for (st, c2), n in sorted(by.items(), key=lambda x: -x[1]):
        print("  %-12s c2 %-12s %6d" % (st, c2, n))
    for st in sorted({k[0] for k in band}):
        print("  %-12s " % st + "  ".join("%s:%d" % (b, band[(st, b)]) for b in ("<=64", "65-256", "257-1K", ">1K")))


def export(a):
    """A JSON list of VAs (8 hex digits) with the given statuses, for tools that take a VA list (w8_emit --list)."""
    c = Corpus(a.file)
    want = set((a.status or "residual").split(","))
    vas = [r["va"] for r in c.rows if r["status"] in want]
    out = a.out or os.path.join(OUTDIR, "%s.%s.json" % (c.name, "+".join(sorted(want))))
    with open(out, "w") as f:
        json.dump(vas, f)
    print("wrote %s: %d VAs" % (out, len(vas)))


def maskcheck(a):
    """The generic mask must reproduce a corpus's own masks (Spore's fingerprints) before it is trusted elsewhere."""
    import random
    c = Corpus(a.file)
    img = c.image()
    rows = random.Random(1).sample(c.rows, min(a.n, len(c.rows)))
    same = 0
    for r in rows:
        v = int(r["va"], 16)
        g, f = img.mask(v, r["size"]), unpack(r["mask"])
        if g == f and img.fixups(v, r["size"]) == r["fixups"]:
            same += 1
        elif a.verbose:
            print("%s size %d: generic-only %s, corpus-only %s" % (r["va"], r["size"], sorted(g - f)[:8], sorted(f - g)[:8]))
    print("%d / %d masks identical" % (same, len(rows)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["spore", "mb", "fnv", "f3", "stats", "maskcheck", "export"])
    ap.add_argument("file", nargs="?")
    ap.add_argument("--out")
    ap.add_argument("--image")
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--status", help="export: statuses, comma-separated (default residual)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    {"spore": build_spore, "mb": build_mb, "fnv": build_units, "f3": build_units, "stats": stats, "maskcheck": maskcheck, "export": export}[a.cmd](a)


if __name__ == "__main__":
    main()
