#!/usr/bin/env python3
"""One-time setup for c2scope (VS2005 SP1 c2.dll 14.00.50727.762; other builds: tools/c2build.py setup --c2 BUILD).

usage: uv run python setup.py --vs2005 /path/to/vs2005 [--pdb /path/to/c2.pdb] [--no-download] [--skip-tests]

--vs2005 is a VS2005 SP1 toolchain directory with VC/Bin (cl.exe, c1xx.dll, c2.dll, ...), VC/INCLUDE, VC/LIB and
SDK/Include, SDK/Lib (the layout f3decomp's get_vs2005.py unpacks). The tools never modify it.

Every step is idempotent: run it again after fixing whatever it complained about and it carries on.
Windows runs the compilers directly; macOS and Linux run them under wine.
  1. checks the host tools: wine (not on Windows), cargo (Rust), llvm-pdbutil
  2. checks the toolchain and that its c2.dll is exactly the build the tools were written against
  3. links it as work/toolchain/vs2005 and copies c2.dll to Bin/c2.dll (the tools read symbols and code from there)
  4. gets c2.pdb: --pdb, or work/pdb/c2.pdb, or a download from the Microsoft public symbol server
     (https://msdl.microsoft.com/download/symbols/...; skipped with --no-download)
  5. builds work/index/c2.dll.csv (function names and addresses) from the PDB
  6. creates this checkout's own wine prefix (work/toolchain/wineprefix) and runs cl in it (Windows: runs cl)
  7. builds the traced toolchain work/toolchain/vs2005-trace: a copy of VC/Bin whose c2.dll gets a small entry-point
     patch that loads c2trace.dll (built here with the toolchain's own cl); everything else is linked (a symlink;
     on Windows without the symlink right, a directory junction)
  8. builds rasim (cargo build --release)
  9. runs the regression tests (tests/run_tests.py)
"""
import argparse, hashlib, os, shutil, subprocess, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "tools"))
C2_SHA256 = "497d1ffe57f75db8b458167bdea475f3962829e139b355a2672a4ef51c716413"   # c2.dll 14.00.50727.762
WORK = os.path.join(HERE, "work")
TC = os.path.join(WORK, "toolchain")
WINDOWS = os.name == "nt"
RASIM = os.path.join(HERE, "tools", "rasim", "target", "release", "rasim" + (".exe" if WINDOWS else ""))


def is_link(p):
    return os.path.islink(p) or getattr(os.path, "isjunction", lambda q: False)(p)


def step(n, text):
    print("\n[%d/9] %s" % (n, text), flush=True)


def ok(text):
    print("      ok: " + text, flush=True)


def fail(text, hint=None):
    print("      PROBLEM: " + text)
    if hint:
        print("      fix: " + hint)
    print("\nSetup stopped. Fix the problem above and run the same command again; finished steps are skipped.")
    sys.exit(1)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def check_host():
    step(1, "host tools")
    if WINDOWS:
        ok("Windows: the compilers run directly (no wine)")
    elif not shutil.which("wine"):
        fail("wine not found on PATH", "macOS: brew install --cask wine-stable (or brew install wine); "
             "Linux: your distribution's wine package (32-bit support needed)")
    else:
        ok("wine: " + subprocess.run(["wine", "--version"], capture_output=True, text=True).stdout.strip())
    if shutil.which("cargo"):
        ok("cargo: " + subprocess.run(["cargo", "--version"], capture_output=True, text=True).stdout.strip())
    elif os.path.isfile(RASIM):
        ok("no cargo; using the rasim already built at " + os.path.relpath(RASIM, HERE))
    else:
        fail("cargo not found (rasim is written in Rust)", "install Rust from https://rustup.rs and open a new shell"
             + ("; on Windows rustup's default toolchain needs the Visual Studio C++ build tools, or use "
                "`rustup default stable-x86_64-pc-windows-gnu`" if WINDOWS else ""))
    import pdbindex
    have_index = os.path.getsize(os.path.join(WORK, "index", "c2.dll.csv")) > 100000 \
        if os.path.isfile(os.path.join(WORK, "index", "c2.dll.csv")) else False
    if not os.path.exists(pdbindex.PDBUTIL) and not shutil.which(pdbindex.PDBUTIL):
        if have_index:
            ok("no llvm-pdbutil; work/index/c2.dll.csv is already built")
            return
        fail("llvm-pdbutil not found", "macOS: brew install llvm; Linux: install llvm (llvm-pdbutil ships with it); "
             "Windows: an LLVM release for Windows (winget install LLVM.LLVM) with llvm-pdbutil.exe on PATH; "
             "or set LLVM_PDBUTIL=/path/to/llvm-pdbutil")
    ok("llvm-pdbutil: " + pdbindex.PDBUTIL)


def check_toolchain(vs):
    step(2, "VS2005 toolchain")
    if not vs:
        fail("no toolchain given", "pass --vs2005 /path/to/vs2005 (the directory that contains VC/ and SDK/)")
    vs = os.path.abspath(os.path.expanduser(vs))
    need = ["VC/Bin/cl.exe", "VC/Bin/c1xx.dll", "VC/Bin/c2.dll", "VC/INCLUDE", "VC/LIB", "SDK/Include", "SDK/Lib"]
    from c2build import tcpath   # case-insensitive: a lower-case unpack (vc/bin, ...) on Linux/WSL
    missing = [n for n in need if not os.path.exists(tcpath(vs, n))]
    if missing:
        fail("%s lacks %s" % (vs, ", ".join(missing)),
             "point --vs2005 at the directory holding VC/ and SDK/ (f3decomp: the vs2005 directory its setup unpacks)")
    got = sha256(tcpath(vs, "VC/Bin/c2.dll"))
    if got != C2_SHA256:
        fail("VC/Bin/c2.dll is not the expected build (sha256 %s...)" % got[:16],
             "the tools need VS2005 SP1 c2.dll 14.00.50727.762 (sha256 %s)" % C2_SHA256)
    ok("%s (c2.dll 14.00.50727.762)" % vs)
    return vs


def link_toolchain(vs):
    step(3, "link the toolchain into work/")
    os.makedirs(TC, exist_ok=True)
    link = os.path.join(TC, "vs2005")
    if is_link(link) and os.path.realpath(link) != os.path.realpath(vs):
        os.rmdir(link) if WINDOWS else os.remove(link)       # a link only: the toolchain itself is never touched
    if not os.path.exists(link):
        from c2build import link_dir
        link_dir(vs, link)
    os.makedirs(os.path.join(HERE, "Bin"), exist_ok=True)
    dst = os.path.join(HERE, "Bin", "c2.dll")
    if not os.path.exists(dst) or sha256(dst) != C2_SHA256:
        from c2build import tcpath
        shutil.copy(tcpath(vs, "VC/Bin/c2.dll"), dst)
    ok("work/toolchain/vs2005 -> %s; Bin/c2.dll" % vs)


def get_pdb(pdb_arg, download):
    step(4, "c2.pdb (public symbols)")
    import pefile
    pe = pefile.PE(os.path.join(HERE, "Bin", "c2.dll"), fast_load=True)
    pe.parse_data_directories(directories=[6])
    entry = [d.entry for d in pe.DIRECTORY_ENTRY_DEBUG if d.entry is not None and hasattr(d.entry, "PdbFileName")][0]
    name = os.path.basename(entry.PdbFileName.rstrip(b"\0").decode().replace("\\", "/"))
    key = entry.Signature_String[:32] + "%X" % entry.Age
    dst = os.path.join(WORK, "pdb", name)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if pdb_arg:
        shutil.copy(os.path.expanduser(pdb_arg), dst)
    if not os.path.exists(dst):
        if not download:
            fail("no %s" % dst, "pass --pdb /path/to/c2.pdb, or drop --no-download")
        url = "https://msdl.microsoft.com/download/symbols/%s/%s/%s" % (name, key, name)
        print("      downloading %s" % url, flush=True)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Microsoft-Symbol-Server/10.0.0.0"})
            data = urllib.request.urlopen(req, timeout=120).read()
        except Exception as e:
            fail("download failed: %s" % e, "download it yourself from that URL and pass --pdb")
        if data[:4] == b"MSCF":
            fail("the server sent a compressed .pd_ file", "expand it (cabextract) and pass --pdb")
        open(dst, "wb").write(data)
    ok(dst)


def build_index():
    step(5, "function index work/index/c2.dll.csv")
    out = os.path.join(WORK, "index", "c2.dll.csv")
    if os.path.exists(out) and os.path.getsize(out) > 100000:
        ok("exists")
        return
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "pdbindex.py"), os.path.join(HERE, "Bin", "c2.dll")],
                       capture_output=True, text=True)
    if r.returncode or not os.path.exists(out):
        fail("pdbindex failed:\n" + (r.stdout + r.stderr)[-1500:],
             "a GUID mismatch means the PDB belongs to another build; delete work/pdb/c2.pdb and re-run")
    ok(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else out)


def wine_prefix():
    step(6, "cl.exe runs" if WINDOWS else "wine prefix work/toolchain/wineprefix (the first time takes a minute)")
    import msvc
    msvc.ensure_wine(msvc.env())
    rc, out = msvc.run("cl", [], capture=True)
    if "Version 14.00.50727.762" not in out:
        fail("cl.exe did not run%s:\n" % ("" if WINDOWS else " under wine") + out[-800:],
             "run `uv run tools/msvc.py` by hand to see the error" + ("" if WINDOWS else " (32-bit wine support is required)"))
    ok("cl 14.00.50727.762 runs")


def traced_toolchain(vs):
    step(7, "traced toolchain work/toolchain/vs2005-trace")
    from c2build import tcpath   # the copy uses the canonical names; the source may be a lower-case unpack
    tr = os.path.join(TC, "vs2005-trace")
    binsrc, bindst = tcpath(vs, "VC", "Bin"), os.path.join(tr, "VC", "Bin")
    if not os.path.isdir(bindst):
        shutil.copytree(binsrc, bindst, symlinks=False)
    for sub in ("VC/INCLUDE", "VC/LIB", "SDK"):
        p = os.path.join(tr, sub)
        if not os.path.exists(p):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            from c2build import link_dir
            link_dir(tcpath(vs, sub), p)
    import c2trace
    patched = os.path.exists(os.path.join(bindst, "c2trace.dll")) and sha256(os.path.join(bindst, "c2.dll")) != C2_SHA256
    if not patched:
        c2trace.install()
    ok("c2.dll patched, c2trace.dll built (the untraced toolchain is untouched)")


def build_rasim():
    step(8, "rasim (Rust)")
    if not shutil.which("cargo"):
        ok("no cargo; keeping " + os.path.relpath(RASIM, HERE))
        return
    r = subprocess.run(["cargo", "build", "--release", "--quiet", "--manifest-path",
                        os.path.join(HERE, "tools", "rasim", "Cargo.toml")], capture_output=True, text=True)
    if r.returncode:
        fail("cargo build failed:\n" + r.stderr[-1500:])
    ok(os.path.relpath(RASIM, HERE))


def tests():
    step(9, "regression tests")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tests", "run_tests.py")])
    if r.returncode:
        fail("some tests failed (see above)", "re-run `uv run python tests/run_tests.py -v` and report the output")


def main():
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("--vs2005", help="VS2005 SP1 toolchain directory (contains VC/ and SDK/)")
    ap.add_argument("--pdb", help="c2.pdb to use instead of downloading it")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    if not a.vs2005 and is_link(os.path.join(TC, "vs2005")):
        a.vs2005 = os.path.realpath(os.path.join(TC, "vs2005"))      # re-run without repeating the path
    check_host()
    vs = check_toolchain(a.vs2005)
    link_toolchain(vs)
    get_pdb(a.pdb, not a.no_download)
    build_index()
    wine_prefix()
    traced_toolchain(vs)
    build_rasim()
    if not a.skip_tests:
        tests()
    print("\nSetup complete. Try:\n"
          "  uv run tools/c2trace.py regs tests/data/eh_helpers.cpp --func eB -- /TP /MT /GS /EHsc /Gy\n"
          "and read README.md (Quick start) and docs/c2/tracer.md.")


if __name__ == "__main__":
    main()
