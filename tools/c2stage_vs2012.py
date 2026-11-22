#!/usr/bin/env python3
"""Stage an unpacked Visual C++ 2012 (Update 4/5) for `c2build.py setup --c2 17.00` (docs/c2/port-1700.md).

usage: c2stage_vs2012.py ROOT [--out work/vs2012/stage]

ROOT is the folder Microsoft's ISOs were unpacked into (it holds "Program Files/Microsoft Visual Studio 11.0" and
"Windows Kits/8.0"). The stage is symbolic links only, in the layout setup
copies from: vc/bin = VC/bin/x86_amd64 (the 32-bit-hosted x64 cross compiler), vc/include, vc/lib = VC/lib/amd64,
sdk/Include = the kit's shared/ and um/ headers in one folder, sdk/Lib = Lib/win8/um/x64, vc/extra =
Common7/IDE's msobj110.dll (link /LTCG needs it; setup copies extra/ into VC/Bin).
"""
import argparse, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv):
    ap = argparse.ArgumentParser(prog="c2stage_vs2012.py")
    ap.add_argument("root")
    ap.add_argument("--out", default=os.path.join(ROOT, "work", "vs2012", "stage"))
    a = ap.parse_args(argv)
    vs = os.path.join(a.root, "Program Files", "Microsoft Visual Studio 11.0", "VC")
    kit = os.path.join(a.root, "Windows Kits", "8.0")
    need = [os.path.join(vs, "bin", "x86_amd64", "c2.dll"), os.path.join(vs, "include"),
            os.path.join(vs, "lib", "amd64"), os.path.join(kit, "Include", "um"), os.path.join(kit, "Lib", "win8", "um", "x64")]
    miss = [p for p in need if not os.path.exists(p)]
    if miss:
        sys.exit("c2stage_vs2012: not an unpacked VS2012: missing %s" % ", ".join(miss))
    if os.path.exists(a.out):
        sys.exit("c2stage_vs2012: %s exists (remove it to restage)" % a.out)
    os.makedirs(os.path.join(a.out, "vc"))
    os.makedirs(os.path.join(a.out, "sdk", "Include"))
    for name, src in (("bin", os.path.join(vs, "bin", "x86_amd64")), ("include", os.path.join(vs, "include")),
                      ("lib", os.path.join(vs, "lib", "amd64"))):
        os.symlink(os.path.abspath(src), os.path.join(a.out, "vc", name))
    n = 0
    for sub in ("shared", "um"):
        d = os.path.join(kit, "Include", sub)
        for f in sorted(os.listdir(d)):
            dst = os.path.join(a.out, "sdk", "Include", f)
            if not os.path.lexists(dst):
                os.symlink(os.path.abspath(os.path.join(d, f)), dst)
                n += 1
    os.symlink(os.path.abspath(os.path.join(kit, "Lib", "win8", "um", "x64")), os.path.join(a.out, "sdk", "Lib"))
    ide = os.path.join(a.root, "Program Files", "Microsoft Visual Studio 11.0", "Common7", "IDE")
    extra = [f for f in ("msobj110.dll",) if os.path.isfile(os.path.join(ide, f))]   # link /LTCG loads it
    if extra:
        os.makedirs(os.path.join(a.out, "vc", "extra"))
        for f in extra:
            os.symlink(os.path.abspath(os.path.join(ide, f)), os.path.join(a.out, "vc", "extra", f))
    print("staged %s (%d SDK headers); next: uv run tools/c2build.py setup --c2 17.00" % (a.out, n))


if __name__ == "__main__":
    main(sys.argv[1:])
