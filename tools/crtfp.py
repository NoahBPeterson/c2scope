#!/usr/bin/env python3
"""Fingerprints of the CRT's x87 math helpers (`__CIsqrt`, `__CIexp`, ...), so lift2il can name a statically linked
one in a retail binary: cl calls them with the argument in st0 (st1, st0 for pow / atan2 / fmod) and the result in
st0, e.g. `fld qword [x]; call __CIsqrt` for `sqrt(x)` at /O2 (F3 0xcf74a0).

usage: crtfp.py build [--out tools/crtfp.json]     link one test exe per function (/MT, the toolchain's libcmt) and
                                                    record each helper's stub and the two routines it calls
       crtfp.py match BINARY VA                     which helper VA is, if any

A fingerprint is the stub's first 6 instructions and the first 14 of each routine it calls, with addresses
(4+ hex digits) erased: the library code is the same in every binary linked against that CRT.
"""
import json, os, re, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
FUNCS = {"sqrt": 1, "exp": 1, "log": 1, "log10": 1, "sin": 1, "cos": 1, "tan": 1, "asin": 1, "acos": 1,
         "atan": 1, "sinh": 1, "cosh": 1, "tanh": 1, "pow": 2, "atan2": 2, "fmod": 2}
DB = os.path.join(HERE, "crtfp.json")


def canon(img, va, n):
    return [re.sub(r"0x[0-9a-f]{4,}", "A", "%s %s" % (i.mnemonic, i.op_str)) for i in img.insns(va, 160)[:n]]


def callees(img, va):
    return [int(i.op_str, 16) for i in img.insns(va, 40)[:6] if i.mnemonic == "call" and i.op_str.startswith("0x")]


def fingerprint(img, va):
    return {"stub": canon(img, va, 6), "cores": [canon(img, c, 14) for c in callees(img, va)]}


def build(out):
    from w8_emit import Image
    d = tempfile.mkdtemp(prefix="crtfp")
    db = {}
    for f, n in FUNCS.items():
        src = os.path.join(d, "t_%s.cpp" % f)
        open(src, "w").write("#include <math.h>\nvolatile double p = 1.5;\nint main() { double x = p; "
                             "volatile double r = %s(x%s); return (int)r; }\n" % (f, ", (double)p" if n == 2 else ""))
        obj, exe, mp = (os.path.join(d, "t_%s.%s" % (f, e)) for e in ("obj", "exe", "map"))
        msvc = [sys.executable, os.path.join(HERE, "msvc.py")]
        subprocess.run(msvc + ["cl", "/nologo", "/O2", "/MT", "/c", src, "/Fo" + obj], capture_output=True)
        subprocess.run(msvc + ["link", "/nologo", "/MAP:" + mp, "/OUT:" + exe, obj], capture_output=True)
        m = os.path.exists(mp) and re.search(r"\s(__CI%s)\s+([0-9a-f]{8})" % f, open(mp, errors="replace").read())
        if not m or not os.path.exists(exe):
            print("%s: no __CI%s (inlined?)" % (f, f))
            continue
        db[f] = dict(fingerprint(Image(exe), int(m.group(2), 16)), args=n)
        print("%s: %s" % (f, m.group(1)))
    json.dump({"vs2005-libcmt": db}, open(out, "w"), indent=1)
    print("wrote", out)


_DB = None


def import_thunk(img, va):
    """The imported name an import thunk (`jmp dword ptr [IAT]`, a /MD binary's `__CIsqrt`) jumps through, else None."""
    try:
        i = img.insns(va, 8)[0]
    except Exception:
        return None
    m = re.match(r"^dword ptr \[(0x[0-9a-f]+)\]$", i.op_str)
    pe = getattr(img, "pe", None)
    if i.mnemonic != "jmp" or not m or pe is None:
        return None
    if not hasattr(img, "_iat"):
        import pefile
        img._iat = {}
        try:
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
            for e in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
                for imp in e.imports:
                    if imp.name:
                        img._iat[imp.address] = imp.name.decode()
        except Exception:
            pass
    return img._iat.get(int(m.group(1), 16))


def match(img, va):
    """(name, args) of the CRT math helper at va, or None."""
    global _DB
    if _DB is None:
        _DB = json.load(open(DB)) if os.path.exists(DB) else {}
    imp = import_thunk(img, va)
    if imp is not None:
        f = imp[3:] if imp.startswith("_CI") else None
        return (f, FUNCS[f]) if f in FUNCS else None
    try:
        fp = fingerprint(img, va)
    except Exception:
        return None
    for crt in _DB.values():
        for name, e in crt.items():
            if fp["stub"] == e["stub"] and fp["cores"] == e["cores"]:
                return name, e["args"]
    return None


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build(sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else DB)
    elif len(sys.argv) == 4 and sys.argv[1] == "match":
        from w8_emit import Image
        print(match(Image(sys.argv[2]), int(sys.argv[3], 16)))
    else:
        sys.exit(__doc__)
