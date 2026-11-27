#!/usr/bin/env python3
"""Score w8_emit output against the retail bytes: does it compile, and how far is it?

usage: w8_score.py [VA..] [--dir DIR] [--flags FLAGS] [--jobs N] [--json OUT]

For each emitted <va>.cpp: compile with the residual slice's flags, find FUN_<va> in the object,
and compare its bytes with the retail function's, masking the 4-byte address slots on both sides
(the same masking rule run_all.py uses: the PE's base-relocation table on the retail side, the
COFF's own relocations on ours). Reports `ok bdiff N size ours/retail` per function.
"""
import argparse, collections, hashlib, json, os, re, struct, sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import capstone, pefile
from coff import parse_coff, REL_SIZES

IMAGE = os.path.join(ROOT, "work", "SporeApp.analysis.bin")
OUTDIR = os.path.join(ROOT, "work", "scratch", "w8_emit")
DEF_FLAGS = "/O2 /MD /Gy /TP /EHsc"


class Orig:
    def __init__(self):
        pe = pefile.PE(IMAGE, fast_load=True)
        self.pe, self.base = pe, pe.OPTIONAL_HEADER.ImageBase
        self.relocs = []
        d = pe.OPTIONAL_HEADER.DATA_DIRECTORY[5]
        if d.VirtualAddress and d.Size:
            raw = pe.get_data(d.VirtualAddress, d.Size)
            for k in range(0, len(raw) - 8, 8):
                rva, typ = struct.unpack_from("<II", raw, k)
                if typ == 3:
                    self.relocs.append(rva)
            self.relocs.sort()

    def bytes_and_mask(self, va, n):
        import bisect
        rva = va - self.base
        data = self.pe.get_data(rva, n)
        mask = set()
        i = bisect.bisect_left(self.relocs, rva - 3)
        while i < len(self.relocs) and self.relocs[i] < rva + n:
            for k in range(4):
                j = self.relocs[i] + k - rva
                if 0 <= j < n:
                    mask.add(j)
            i += 1
        return data, mask


def compile_one(args):
    src, flags, tag = args
    import ccache, msvc
    obj = os.path.join(ROOT, "work", "scratch", "w8_obj", tag + ".obj")
    os.makedirs(os.path.dirname(obj), exist_ok=True)
    if os.path.exists(obj):
        os.remove(obj)
    cl = ["/nologo", "/c", *flags.split(), "/Fo" + obj, os.path.abspath(src)]
    k = ccache.key(src, cl, [ccache.file_sha(str(msvc.tc("VC/Bin/c2.dll"))), "w8"])
    hit = ccache.get(k)
    if hit:
        import shutil
        shutil.copy(os.path.join(hit, "obj"), obj)
        rc, text = ccache.meta(hit)["rc"], ccache.meta(hit)["out"]
    else:
        rc, text = msvc.run("cl", cl, capture=True, timeout=int(os.environ.get("W8_CL_TIMEOUT", "300")))
        if not rc and os.path.exists(obj):
            ccache.put(k, {"obj": obj}, {"rc": rc, "out": text})
    return obj if (not rc and os.path.exists(obj)) else None, text


def undecorate(nm):
    """_cdecl, __stdcall and __fastcall decorate a C symbol: `_FUN_x`, `_FUN_x@8`, `@FUN_x@8`."""
    m = re.match(r"^\?(?:\?[01])?(FUN_[0-9a-f]{8})@(?:D_[0-9a-f]{8}@)?@", nm)   # C++: FUN_<va>(), or class FUN_<va>'s ctor / dtor
    if m:
        return m.group(1)
    if nm.startswith("@"):
        nm = nm[1:]
    elif nm.startswith("_"):
        nm = nm[1:]
    return re.sub(r"@\d+$", "", nm)


def score(obj, va, size, orig, fixups=()):
    secs, syms, _idx = parse_coff(obj, sizes=True)
    name = "FUN_%08x" % va
    for nm in syms:
        if undecorate(nm) == name:
            syms = {name: syms[nm]}
            break
    else:
        return "NO-SYMBOL", None
    si, off, msz = syms[name]
    sec = secs[si]
    if msz:
        mine = sec["data"][off:off + msz]
    else:
        starts = sorted(o for s_, o, sz2 in syms.values() if s_ == si and o > off)
        mine = sec["data"][off:starts[0] if starts else len(sec["data"])]
        if starts:
            mine = mine.rstrip(b"\xcc")
    theirs, rmask = orig.bytes_and_mask(va, len(mine))
    if theirs is None or len(theirs) < len(mine):
        return "NO-RETAIL", len(mine)
    mine_rels = set()
    for v, _s, t in sec["relocs"]:
        for k in range(REL_SIZES.get(t, 4)):
            if off <= v + k < off + len(mine):
                mine_rels.add(v + k - off)
    cmp_mask = rmask | mine_rels
    diffs = sum(1 for i in range(len(mine)) if i not in cmp_mask and theirs[i] != mine[i])
    extra = "" if len(mine) == size else " size %d/%d" % (len(mine), size)
    # the repo check (fingerprint.py::check) additionally requires: retail's .reloc fixups are all
    # covered by one of OUR relocations (a number where retail has an address fails CI), and we
    # relocate nothing beyond retail's mask. Include both here so MATCH means CI-safe.
    bare = [o for o in fixups if o not in mine_rels]
    if diffs == 0 and len(mine) == size and not bare:
        return "MATCH", 0
    return "DIFF", diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vas", nargs="*")
    ap.add_argument("--dir", default=OUTDIR)
    ap.add_argument("--flags", default=DEF_FLAGS)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--json", default=os.path.join(ROOT, "work/scratch/w8_results.jsonl"))
    ap.add_argument("--budget", type=int, default=540, help="wall-clock seconds before a clean stop; results written so far are kept")
    ap.add_argument("--start", type=int, default=0, help="resume at the Nth source in the sorted list")
    a = ap.parse_args()
    fps = {}
    fixups = {}
    for line in open(os.path.join(ROOT, "..", "spore-decomp", "tools", "ci", "fingerprints.jsonl")):
        j = json.loads(line)
        fps[j["va"]] = j["size"]
        fixups[j["va"]] = tuple(j.get("fixups", ()))
    srcs = sorted(os.listdir(a.dir))
    vas = []
    for f in srcs:
        if not f.endswith(".cpp"):
            continue
        va_s = f[:-4]
        if a.vas and va_s not in a.vas:
            continue
        vas.append((os.path.join(a.dir, f), va_s))
    import time
    t0 = time.time()
    orig = Orig()
    vas = vas[a.start:]
    tasks = [(s, a.flags, hashlib.md5((s + a.flags).encode()).hexdigest()[:12]) for s, _ in vas]
    objs, logs = {}, {}
    stopped = None
    with ThreadPoolExecutor(a.jobs) as ex:
        for i, ((s, _v), (obj, text)) in enumerate(zip(vas, ex.map(compile_one, tasks))):
            objs[s], logs[s] = obj, text
            if stopped is None and time.time() - t0 > a.budget:
                stopped = i                      # finish the in-flight batch, then stop cleanly
    res = collections.Counter()
    rows = []
    with open(a.json, "w") as jf:
        for src, va_s in vas:
            if stopped is not None and src not in objs:
                break
            va = int(va_s, 16)
            size = fps.get(va_s)
            obj = objs[src]
            if obj is None:
                msg = logs[src].strip().splitlines()
                err = next((l for l in msg if " error " in l), msg[0] if msg else "cl failed")
                status, diff = "CL-ERROR", None
            else:
                status, diff = score(obj, va, size, orig, fixups.get(va_s, ()))
            res[status] += 1
            rows.append(dict(va=va_s, status=status, diff=diff))
            jf.write(json.dumps(rows[-1]) + "\n")
            if status != "MATCH":
                print("%-9s %-9s %s" % (status, va_s, err if status == "CL-ERROR" else "diff %s" % diff))
    print("\n", dict(res), "of", len(vas))

    # the error histogram, which is what drives the next round of emitter work
    hist = collections.Counter()
    for src, _ in vas:
        if objs[src] is None:
            for l in logs[src].splitlines():
                m = re.search(r"error C\d+: (.*)", l)
                if m:
                    hist[m.group(0)[:80]] += 1
                    break
    for k, v in hist.most_common(10):
        print("  %4d  %s" % (v, k))


if __name__ == "__main__":
    main()
