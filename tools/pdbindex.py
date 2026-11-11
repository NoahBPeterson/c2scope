#!/usr/bin/env python3
"""Build a function index for one binary from its public PDB.

usage: pdbindex.py <Bin/name.dll|exe> [--pdb work/pdb/x.pdb]   -> work/index/<file name>.csv (e.g. cl.exe.csv)

The public PDBs (Microsoft symbol server) carry S_PUB32 records (mangled names, section:offset, a "function"
flag), the module list (original .obj paths) and section contributions (each COMDAT's start, size, module), but
no types and no private symbols. For each code public, the row takes the contribution that starts at its
address; if none starts there it falls back to the distance to the next public in the same section.
Columns: va, size, size_src (contrib|next), module, obj, flags (function|none|static), symbol.
Unnamed code contributions are rows with flags "static" and an empty symbol.
"""
import argparse, bisect, csv, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _pdbutil():
    import shutil
    for c in (os.environ.get("LLVM_PDBUTIL"), shutil.which("llvm-pdbutil"), "/opt/homebrew/opt/llvm/bin/llvm-pdbutil",
              "/usr/local/opt/llvm/bin/llvm-pdbutil", shutil.which("llvm-pdbutil-18"), shutil.which("llvm-pdbutil-17")):
        if c and os.path.exists(c):
            return c
    return "llvm-pdbutil"


PDBUTIL = _pdbutil()


def dump(pdb, *what):
    return subprocess.run([PDBUTIL, "dump", *what, pdb], capture_output=True, text=True, check=True).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("binary")
    ap.add_argument("--pdb")
    a = ap.parse_args()
    import pefile
    pe = pefile.PE(a.binary, fast_load=True)
    pe.parse_data_directories(directories=[6])
    pdbname, guid = None, None
    for d in getattr(pe, "DIRECTORY_ENTRY_DEBUG", []):
        if d.entry is not None and hasattr(d.entry, "PdbFileName"):
            pdbname = os.path.basename(d.entry.PdbFileName.rstrip(b"\0").decode().replace("\\", "/"))
            guid = d.entry.Signature_String[:32]
    pdb = a.pdb or os.path.join(ROOT, "work", "pdb", pdbname or "")
    if not os.path.isfile(pdb):
        sys.exit("no PDB at %s" % pdb)
    # same-named PDBs of different binaries exist (vcbuild.exe and VCBuild.dll): the GUID must agree (the age may
    # be higher in a stripped public PDB than in the binary, so it is not compared)
    m = re.search(r"GUID: \{([0-9A-F-]+)\}", dump(pdb, "-summary"))
    if guid and (not m or m.group(1).replace("-", "") != guid):
        sys.exit("%s: %s is another binary's PDB (GUID %s, binary wants %s)" % (a.binary, pdb, m and m.group(1), guid))
    base = pe.OPTIONAL_HEADER.ImageBase
    secs = [(s.VirtualAddress, s.Name.rstrip(b"\0").decode(), s.Characteristics) for s in pe.sections]

    mods = {}
    for m in re.finditer(r"Mod (\d+) \| `([^`]*)`:\s*\n\s*Obj: `([^`]*)`", dump(pdb, "-modules")):
        mods[int(m.group(1))] = (m.group(2), m.group(3))

    contribs = {}   # (sec, off) -> (size, mod)
    code_contribs = set()
    for m in re.finditer(r"SC\[[^\]]*\]\s*\| mod = (\d+), (\d+):(\d+), size = (\d+)[^\n]*\n\s*([^\n]*)",
                         dump(pdb, "-section-contribs")):
        k = (int(m.group(2)), int(m.group(3)))
        contribs[k] = (int(m.group(4)), int(m.group(1)))
        if "IMAGE_SCN_CNT_CODE" in m.group(5):
            code_contribs.add(k)

    pubs = []
    for m in re.finditer(r"S_PUB32 \[size = \d+\] `([^`]*)`\s*\n\s*flags = ([^,]*), addr = (\d+):(\d+)", dump(pdb, "-publics")):
        pubs.append((int(m.group(3)), int(m.group(4)), m.group(2).strip(), m.group(1)))

    by_sec = {}
    for s, o, _f, _n in pubs:
        by_sec.setdefault(s, []).append(o)
    for s in by_sec:
        by_sec[s] = sorted(set(by_sec[s]))

    rows = []
    for s, o, flags, name in pubs:
        if not (1 <= s <= len(secs)) or not secs[s - 1][2] & 0x20:      # code sections only
            continue
        if (s, o) in contribs:
            size, mod = contribs[(s, o)]
            src = "contrib"
        else:
            lst = by_sec[s]
            i = bisect.bisect_right(lst, o)
            size = (lst[i] - o) if i < len(lst) else 0
            mod, src = -1, "next"
            # module of the contribution that contains it
            for (cs, co), (csz, cm) in contribs.items():
                if cs == s and co <= o < co + csz:
                    mod = cm
                    break
        va = base + secs[s - 1][0] + o
        modname, objname = mods.get(mod, ("", ""))
        rows.append((va, size, src, modname, objname, flags, name))
    # code contributions with no public at their start: file-local (static) functions, or LTCG-generated
    # pieces; flags "static", no symbol. Bind them in source with a `// @ 0x<va>` marker (tools/chk.py).
    named = {(s, o) for s, o, _f, _n in pubs}
    for (s, o) in sorted(code_contribs - named):
        if not (1 <= s <= len(secs)) or not secs[s - 1][2] & 0x20:
            continue
        size, mod = contribs[(s, o)]
        if size == 0:
            continue
        modname, objname = mods.get(mod, ("", ""))
        rows.append((base + secs[s - 1][0] + o, size, "contrib", modname, objname, "static", ""))
    rows.sort()
    out = os.path.join(ROOT, "work", "index", os.path.basename(a.binary).lower() + ".csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["va", "size", "size_src", "module", "obj", "flags", "symbol"])
        for va, size, src, mn, on, fl, nm in rows:
            w.writerow(["%08x" % va, size, src, mn, on, fl, nm])
    funcs = [r for r in rows if "function" in r[5]]
    statics = [r for r in rows if r[5] == "static"]
    print("%s: %d named functions, %d unnamed code contributions (%d bytes), %d modules -> %s"
          % (os.path.basename(a.binary), len(funcs), len(statics), sum(r[1] for r in statics), len(mods),
             os.path.relpath(out, ROOT)))


if __name__ == "__main__":
    main()
