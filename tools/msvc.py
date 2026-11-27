#!/usr/bin/env python3
"""Run the VS2005 SP1 tools (cl 14.00.50727.762): directly on Windows, elsewhere under this project's own Wine prefix.

usage: msvc.py [--tool cl|link|lib|dumpbin|undname|ml] <MSVC arguments>     (or: msvc.py cl|link|... <arguments>)
Under wine, absolute Unix paths (bare, or after /I /Fo /Fa /Fe /OUT: /LIBPATH: /PDB: /MAP: /DEF: /IMPLIB:) become
Z:\\ paths; on Windows (c2build.WINDOWS) the arguments go to the tool as given and there is no prefix.

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


def tc(rel):
    """A path in the toolchain, matched case-insensitively (c2build.tcpath: a lower-case `vc/bin` unpack on Linux)."""
    return Path(c2build.tcpath(TC, rel))


NATIVE = c2build.WINDOWS


def winpath(p):
    if NATIVE:
        return str(Path(p).resolve())
    return "Z:" + str(Path(p).resolve()).replace("/", "\\")


def convert(args):
    if NATIVE:
        return list(args)
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
    if NATIVE:                   # cl finds its DLLs (c1xx, c2, mspdb80) beside itself; PATH for the ones it spawns
        e["PATH"] = winpath(tc("VC/Bin")) + os.pathsep + e.get("PATH", "")
    else:
        e.update(WINEPREFIX=str(PREFIX), WINEDEBUG="-all", WINEPATH=winpath(tc("VC/Bin")))
    e.update(INCLUDE=";".join(winpath(p) for p in [ROOT / "src" / "include", tc("VC/INCLUDE"), tc("SDK/Include")]),
             LIB=";".join(winpath(p) for p in [tc("VC/LIB"), tc("SDK/Lib")]))
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


KEEPER = PREFIX.parent / (PREFIX.name + ".keeper")
KEEP = os.environ.get("WINE_KEEP", "1") != "0"


def keeper_pid():
    """Pid of this prefix's keeper, if it is still running (0 otherwise)."""
    try:
        pid = int(KEEPER.read_text().strip())
    except (OSError, ValueError):
        return 0
    try:
        os.kill(pid, 0)
    except OSError:
        return 0
    return pid


def start_keeper(e):
    """Hold a resident Wine process in the prefix so the wineserver does not exit between compiles.

    A wineserver with no attached client exits about 3 s after its last process ends; every later compile then pays
    a full cold start. Measured on an M1 Pro (wine-11.16, 32-bit x86 under Rosetta): `cl /c` on tailmerge.c took
    2.37 s with a cold server on every call and 0.74 s with the server resident, and a traced `scratch` compile
    went from 2.49 s to 0.74 s. `wineserver -p` does not help: it cannot start directly on this install
    ("failed to load l_intl.nls"), and started through wine it still exits with the last client. A resident
    A resident `cmd /c ping` has no visible window and is enough for the server to stay up. `regedit /dev/null`
    was used before, but on some Wine builds (Wine Stable 11.0 on macOS) it shows a modal "file not found" dialog.

    The keeper is detached and writes its pid file; it dies on its own when the prefix is reset (`wineserver -k`).
    WINE_KEEP=0 disables it."""
    quiet = dict(env=e, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p = subprocess.Popen(["wine", "cmd", "/c", "ping", "-n", "2147483647", "127.0.0.1"], start_new_session=True, **quiet)
    KEEPER.write_text(str(p.pid))


def ensure_wine(e):
    """Start the prefix's wineserver if it is not running, under a lock file, and keep it resident.

    Without this, parallel runs from a cold start each made wine start its own server, and one of them could exit
    rc 0 with no output at all (9 parallel compiles from cold: about one in three rounds; this was the "parallel
    traces come back empty" bug). A bare `wineserver` cannot start on this install ("failed to load l_intl.nls"),
    so the server is started through wine itself. With the server up, `start_keeper` keeps it up. Windows: nothing."""
    if NATIVE:
        return
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
            if KEEP:
                start_keeper(e)
        elif KEEP and not keeper_pid():
            start_keeper(e)


def run(tool, args, capture=False, timeout=None):
    """timeout (seconds): a compile still running then is killed and fails with rc -9 (2026-11-25: one cl.exe under
    wine ran 2 hours and held a 19,000-function run)"""
    e = env()
    ensure_wine(e)
    with tempfile.TemporaryFile() as out:
        try:
            rc = subprocess.run(([] if NATIVE else ["wine"]) + [str(tc("VC/Bin/" + tool + ".exe"))] + convert(args), env=e,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc = -9
            out.write(b"\nTIMEOUT: %s ran over %d s\n" % (tool.encode(), timeout))
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
