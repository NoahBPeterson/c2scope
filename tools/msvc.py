#!/usr/bin/env python3
"""Run the VS2005 SP1 tools (cl 14.00.50727.762) under this project's own Wine prefix.

usage: msvc.py [--tool cl|link|lib|dumpbin|undname|ml] <MSVC arguments>     (or: msvc.py cl|link|... <arguments>)
Absolute Unix paths (bare, or after /I /Fo /Fa /Fe /OUT: /LIBPATH: /PDB: /MAP: /DEF: /IMPLIB:) become Z:\\ paths.

Toolchain: work/toolchain/vs2005 (VC/{Bin,INCLUDE,LIB} + SDK/{Include,Lib}); currently a symlink to fn3-test's
copy, unpacked from Microsoft's Vista SDK Feb 2007 ISO by f3check's get_vs2005.py. Its VC/Bin is byte-identical to
Bin/, the binaries we decompile. Prefix: work/toolchain/wineprefix (our own, so our waves never share a wineserver
with another project's; see mountandblade-decomp tools/matching/cl.sh). Override with VS8_ROOT / VS8_PREFIX.

Output goes through a temp file, never the caller's pipe: wine's services inherit cl's stdio and would hold the
caller's pipe open until the wine session ends.
"""
import os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import c2build                # C2_BUILD / --c2 selects another compiler build's toolchain copy (docs/c2/port-1310.md)
TC = Path(os.environ.get("VS8_ROOT") or (c2build.toolchain() if c2build.table() else ROOT / "work" / "toolchain" / "vs2005"))
PREFIX = Path(os.environ.get("VS8_PREFIX") or ROOT / "work" / "toolchain" / "wineprefix")
TOOLS = ("cl", "link", "lib", "dumpbin", "undname", "ml")
OPTS = ["/LIBPATH:", "/OUT:", "/DEF:", "/IMPLIB:", "/PDB:", "/MAP:", "/Fo", "/Fa", "/Fe", "/Fd", "/I"]


def winpath(p):
    return "Z:" + str(Path(p).resolve()).replace("/", "\\")


def convert(args):
    out = []
    for a in args:
        for o in OPTS:
            if a.lower().startswith(o.lower()) and a[len(o):].startswith("/"):
                out.append(a[:len(o)] + winpath(a[len(o):]))
                break
        else:
            out.append(winpath(a) if a.startswith("/") and len(a) > 1 and Path(a).exists() else a)
    return out


def env():
    e = os.environ.copy()
    e.update(WINEPREFIX=str(PREFIX), WINEDEBUG="-all", WINEPATH=winpath(TC / "VC/Bin"),
             INCLUDE=";".join(winpath(p) for p in [ROOT / "src" / "include", TC / "VC/INCLUDE", TC / "SDK/Include"]),
             LIB=";".join(winpath(p) for p in [TC / "VC/LIB", TC / "SDK/Lib"]))
    return e


def server_alive():
    """True if the prefix's wineserver accepts connections (its socket is /tmp/.wine-<uid>/server-<dev>-<ino>/socket)."""
    import socket
    st = PREFIX.stat()
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect("/tmp/.wine-%d/server-%x-%x/socket" % (os.getuid(), st.st_dev, st.st_ino))
        return True
    except OSError:
        return False
    finally:
        s.close()


def ensure_wine(e):
    """Start the prefix's wineserver if it is not running, under a lock file.

    Without this, parallel runs from a cold start each made wine start its own server, and one of them could exit
    rc 0 with no output at all (9 parallel compiles from cold: about one in three rounds; this was the "parallel
    traces come back empty" bug). A bare `wineserver` cannot start on this install ("failed to load l_intl.nls"),
    so the server is started through wine itself."""
    import fcntl
    quiet = dict(env=e, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PREFIX.parent.mkdir(parents=True, exist_ok=True)
    with open(str(PREFIX) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if not PREFIX.exists():
            PREFIX.mkdir(parents=True)
            subprocess.run(["wineboot", "-i"], start_new_session=True, **quiet)
        if not server_alive():
            subprocess.run(["wine", "cmd", "/c", "exit"], start_new_session=True, **quiet)


def run(tool, args, capture=False):
    e = env()
    ensure_wine(e)
    with tempfile.TemporaryFile() as out:
        rc = subprocess.run(["wine", str(TC / "VC/Bin" / (tool + ".exe"))] + convert(args), env=e,
                            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT).returncode
        out.seek(0)
        text = out.read().decode("latin-1")
    if capture:
        return rc, text
    sys.stdout.write(text)
    return rc


if __name__ == "__main__":
    a = sys.argv[1:]
    tool = "cl"
    if a[:1] == ["--tool"]:
        tool, a = a[1], a[2:]
    elif a[:1] and a[0].lower() in TOOLS:        # `msvc.py cl /c x.c`, `msvc.py link ...`: a leading tool name
        tool, a = a[0].lower(), a[1:]
    sys.exit(run(tool, a))
