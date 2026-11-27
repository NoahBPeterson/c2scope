#!/usr/bin/env python3
"""Which c2.dll build the tracing tools target.  Doc: docs/c2/port-1310.md

The default is cl 14.00.50727.762 (VS2005 SP1), for which nothing here is used: every tool keeps its built-in
addresses and offsets. Another build is selected with the environment variable C2_BUILD or with `--c2 BUILD` on
the command line of any tool that imports this module (c2trace.py and its subcommands, helpersweep.py, tsweep.py,
msvc.py); the option is removed from sys.argv here, before the tool parses its arguments, and exported as
C2_BUILD so that child processes (msvc.py) see it. `13.10` and `15.00` are aliases (ALIASES; 15.00 = cl 15.00.30729.01,
VS2008 SP1: docs/c2/port-1500.md).

A non-default build is described by tools/c2builds/<build>.json:
  version, c2_sha256      the pristine c2.dll this table was made for (checked by setup and install)
  image_base              c2.dll's preferred base (hex)
  patch                   cave_rva, text_vsize, entry_rva, iat_loadlibrarya (hex RVAs) for the entry-point cave
                          optional: file_delta (.text RVA minus file offset, hex; 0 when the image is
                          section-aligned on disk), nsec (number of sections, default 5), loadlib "w" (the IAT slot
                          is LoadLibraryW: the cave passes a UTF-16 name; cl 17.00 imports no LoadLibraryA)
  dll_build               optional: the build whose cl compiles c2trace.dll (a cross compiler's own cl makes a DLL
                          for its target, which its 32-bit c2.dll cannot load: 17.00 x64 uses "14.00")
  setup                   optional defaults of `setup`: {"from", "sdk", "sal"} directories relative to the repository
  dll                     c2trace.dll settings written to the trace config: regname, opname (hex VAs) and tuple /
                          symbol layout overrides (see DLL_SRC in c2trace.py, directive `set`)
  functions               {decorated name: [rva hex, contiguous size, confidence, how it was found]}
  probes                  {va hex: label} mid-function hook addresses that are legal for this build
  globals                 {name: va hex}
  hooks                   {tool key: hook list or spec} replacing the 14.00 lists (regs, scratch, sched, il_spec,
                          il_default, default; scratch_x = c2scratch.HOOKS_X for scratch --why/--export;
                          regs_dec = c2regs.DEC_HOOKS, the decision events of `regs --export` / `rasim check
                          --predict`, appended after regs; regs_dec_exit = {regs hook index: decision event} for
                          an event read at that hook's exit); origin = {ra, rp, gr, rm}: the IL at RewriteAsReal
                          and (13.10) at Riscify / MdmAvoidMOBStalls, and every GetReg call (c2scratch.ORIGIN)
  regs_events             {regs hook index: extra event} (13.10: the FPColor events)
  regs_ops                {enterload, last}: OPENTERLOAD and the first pseudo-opcode (hex), which the decision
                          rules compare with (copied into the snapshot as `c2.ops`)
  opsched, sched_delay_cmp  c2sched.py: [cost table VA, rows]; [cmp, test, jcc opcodes] when the build's
                          SchSelectInstrs has the cmp-delay rule (cl 15.00, docs/c2/port-1500.md)
The toolchain lives in work/toolchain/c2-<build> (VS2005-style layout VC/Bin, VC/INCLUDE, VC/LIB, SDK/Include,
SDK/Lib, so tools/msvc.py runs it unchanged) and work/toolchain/c2-<build>-trace (patched c2.dll + c2trace.dll).
Create both with:  uv run tools/c2build.py setup --c2 13.10 [--from DIR] [--sdk DIR] [--sal DIR]
A full function index (every name carried over by tools/c2port.py, or built from the build's own public PDB with
tools/pdbindex.py --out) is optional: work/index/<build>/c2.dll.csv.
"""
import csv, hashlib, json, os, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = "14.00"
ALIASES = {"14.00": DEFAULT, "14.00.50727.762": DEFAULT, "vs2005": DEFAULT,
           "13.10": "13.10.3077", "vc71": "13.10.3077", "3077": "13.10.3077", "3052": "13.10.3052",
           "15.00": "15.00.30729.1", "15.00.30729.01": "15.00.30729.1", "vc9sp1": "15.00.30729.1",
           "vs2008sp1": "15.00.30729.1", "30729": "15.00.30729.1",
           "17.00": "17.00.61030-x64", "17.00-x64": "17.00.61030-x64", "vs2012": "17.00.61030-x64",
           "vc11": "17.00.61030-x64", "61030": "17.00.61030-x64",
           "9044": "13.00.9044", "13.00": "13.00.9044", "vc6pp": "13.00.9044",
           "8153": "15.00.8153-xbox360", "xbox360": "15.00.8153-xbox360", "xenon": "15.00.8153-xbox360"}
_cache = {}


def _argv():
    """Take `--c2 BUILD` / `--c2=BUILD` (before any `--`) out of sys.argv and into the environment."""
    a = sys.argv
    end = a.index("--") if "--" in a else len(a)
    for k in range(1, end):
        if a[k] == "--c2" and k + 1 < end:
            os.environ["C2_BUILD"] = a[k + 1]
            del a[k:k + 2]
            return
        if a[k].startswith("--c2="):
            os.environ["C2_BUILD"] = a[k][5:]
            del a[k]
            return


_argv()


def name():
    b = (os.environ.get("C2_BUILD") or DEFAULT).strip()
    return ALIASES.get(b, b)


def table():
    """The build table, or None for the default build."""
    n = name()
    if n == DEFAULT:
        return None
    if n not in _cache:
        p = os.path.join(ROOT, "tools", "c2builds", n + ".json")
        if not os.path.exists(p):
            sys.exit("C2_BUILD=%s: no build table %s" % (n, os.path.relpath(p, ROOT)))
        _cache[n] = json.load(open(p))
    return _cache[n]


def get(key, default=None):
    t = table()
    return default if t is None else t.get(key, default)


class Unported(list):
    """A hook list a partly ported build (table "partial": true, e.g. cl 17.00 x64) does not have: an empty list, so
    the tools import and compile (tsweep's bdiff / rdiff need no trace); what they would have read from those events
    is missing (a metric such as prio() then fails per variant). A note goes to stderr once per key."""
    _said = set()

    def __init__(self, key):
        super().__init__()
        self.key = key
        if key not in Unported._said:
            Unported._said.add(key)
            sys.stderr.write("c2 %s: '%s' events are not ported to this build (docs/c2/port-1700.md); tools that "
                             "read them have no data\n" % (name(), key))

    def __getitem__(self, i):             # import-time code that builds on an element (HOOKS[0] + ",...") gets ""
        return "" if isinstance(i, int) else Unported(self.key)


def hooks(key, default):
    """The hook list (or spec string) `key` for the selected build; `default` is the 14.00 one."""
    t = table()
    if t is None:
        return default
    h = t.get("hooks", {})
    if key not in h:
        if t.get("partial") and default is not None and not isinstance(default, str):
            return Unported(key)
        sys.exit("c2 %s: no '%s' hooks in the build table (not ported; see docs/c2/port-1310.md)" % (name(), key))
    return h[key]


def tcpath(base, *parts):
    """`base/parts...` with each part looked up case-insensitively when the exact spelling is missing: a toolchain
    unpacked in lower case (`vc/bin`, `vc/include`, ...) on a case-sensitive filesystem (Linux, WSL) is found under
    the canonical names VC/Bin, VC/INCLUDE, VC/LIB, SDK/Include, SDK/Lib. On a case-insensitive filesystem (macOS)
    the exact spelling always exists. A part found in no spelling is kept as written (the path does not exist)."""
    p = str(base)
    for part in (q for x in parts for q in str(x).replace("\\", "/").split("/") if q):
        q = os.path.join(p, part)
        if not os.path.exists(q) and os.path.isdir(p):
            low = part.lower()
            hit = sorted(n for n in os.listdir(p) if n.lower() == low)
            if hit:
                q = os.path.join(p, hit[0])
        p = q
    return p


def toolchain(trace=False):
    return os.path.join(ROOT, "work", "toolchain", "c2-" + name() + ("-trace" if trace else ""))


def orig_c2():
    return tcpath(toolchain(), "VC", "Bin", "c2.dll")


def index_rows():
    """Function rows for image.Image: the table's functions and probes, then the optional full index."""
    t = table()
    base = int(t["image_base"], 16)
    rows, seen = [], set()
    for sym, v in t.get("functions", {}).items():
        rows.append(dict(va=base + int(v[0], 16), size=int(v[1]), symbol=sym, flags="function"))
        seen.add(rows[-1]["va"])
    for va, label in t.get("probes", {}).items():
        rows.append(dict(va=int(va, 16), size=16, symbol=label, flags="probe"))
        seen.add(rows[-1]["va"])
    full = os.path.join(ROOT, "work", "index", name(), "c2.dll.csv")
    if os.path.exists(full):
        for r in csv.DictReader(open(full)):
            r["va"], r["size"] = int(r["va"], 16), int(r["size"])
            if r["va"] not in seen:
                rows.append(r)
    return rows


def sha256(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def setup(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="c2build.py setup")
    mb = os.path.join(os.path.dirname(ROOT), "mountandblade-decomp", "work", "toolchain")
    t = table()
    # a build table may name its own defaults: "setup": {"from": DIR, "sdk": DIR, "sal": DIR or ""}, relative to ROOT
    d = {k: (os.path.normpath(os.path.join(ROOT, v)) if v else "") for k, v in ((t or {}).get("setup") or {}).items()}
    ap.add_argument("--from", dest="src", default=d.get("from", os.path.join(mb, "vc71tk")),
                    help="directory with bin/ include/ lib/ of the compiler (default: the M&B project's vc71tk)")
    ap.add_argument("--sdk", default=d.get("sdk", os.path.join(mb, "vc9sp1", "WinSDK")), help="Windows SDK root (Include/, Lib/)")
    ap.add_argument("--sal", default=d.get("sal", os.path.join(mb, "sal")), help="directory holding a sal.h for SDK 6.0A headers")
    a = ap.parse_args(argv)
    if t is None:
        sys.exit("setup is for non-default builds: pass --c2 13.10 (the 14.00 toolchain is set up by release/setup.py)")
    c2 = tcpath(a.src, "bin", "c2.dll")
    if not os.path.isfile(c2):
        sys.exit("no %s" % c2)
    got = sha256(c2)
    if got != t["c2_sha256"]:
        other = [n for n in os.listdir(os.path.join(ROOT, "tools", "c2builds")) if n.endswith(".json")
                 and json.load(open(os.path.join(ROOT, "tools", "c2builds", n))).get("c2_sha256") == got]
        sys.exit("%s is not c2.dll %s (sha256 %s)%s" % (c2, t["version"], got,
                 "; it is %s: use --c2 %s" % (other[0][:-5], other[0][:-5]) if other else ""))
    tc = toolchain()
    if not os.path.isdir(tc):
        os.makedirs(os.path.join(tc, "VC"))
        shutil.copytree(tcpath(a.src, "bin"), os.path.join(tc, "VC", "Bin"))
        ex = tcpath(a.src, "extra")
        if os.path.isdir(ex):            # files the stage keeps beside bin/ (c2stage_vs2012: msobj110.dll for /LTCG)
            for f in os.listdir(ex):
                shutil.copy(os.path.join(ex, f), os.path.join(tc, "VC", "Bin", f))
        shutil.copytree(tcpath(a.src, "include"), os.path.join(tc, "VC", "INCLUDE"))
        shutil.copytree(tcpath(a.src, "lib"), os.path.join(tc, "VC", "LIB"))
        if a.sal and os.path.isfile(os.path.join(a.sal, "sal.h")):
            shutil.copy(os.path.join(a.sal, "sal.h"), os.path.join(tc, "VC", "INCLUDE", "sal.h"))
        os.makedirs(os.path.join(tc, "SDK"))
        for sub in ("Include", "Lib"):
            src = os.path.realpath(tcpath(a.sdk, sub))
            if os.path.isdir(src):
                os.symlink(src, os.path.join(tc, "SDK", sub))
            else:
                os.makedirs(os.path.join(tc, "SDK", sub))
                print("note: no %s; <windows.h> will not be found" % src)
        print("toolchain %s (c2.dll %s)" % (os.path.relpath(tc, ROOT), t["version"]))
    tr = toolchain(True)
    if not os.path.isdir(tr):
        os.makedirs(os.path.join(tr, "VC"))
        shutil.copytree(os.path.join(tc, "VC", "Bin"), os.path.join(tr, "VC", "Bin"))
        for rel in ("VC/INCLUDE", "VC/LIB", "SDK"):
            os.symlink(os.path.join(tc, rel), os.path.join(tr, rel))
    import c2trace
    c2trace.install()


if __name__ == "__main__":
    if sys.argv[1:2] == ["setup"]:
        setup(sys.argv[2:])
    else:
        t = table()
        print("c2 build: %s%s" % (name(), "" if t is None else "  (table tools/c2builds/%s.json, toolchain %s)" % (
            name(), os.path.relpath(toolchain(), ROOT))))
