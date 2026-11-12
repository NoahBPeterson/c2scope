#!/usr/bin/env python3
"""Trace c2.dll while link.exe drives it: /GL objects, code generation at link /LTCG.  Docs: docs/c2/ltcg.md,
docs/c2/port-1500.md section 8

usage: c2ltcg.py [--c2 BUILD] regs|scratch|il|raw|regmap|tsweep|rwsweep|helpersweep <main.c> --with other.c[,more.c]
                 [--export sym[,sym]] [the subcommand's own options, e.g. --func S --why a,b] [-- <cl args>]
       c2ltcg.py [--c2 BUILD] asm <main.c> --with other.c [--export sym] [--out DIR] [--func S] [-- <cl args>]

Under /GL the compiler proper writes IL objects and c2.dll generates no code; `link /LTCG` loads c2.dll and runs
the whole back end over all objects at once (cross-module inlining, custom register conventions for functions
nothing outside can reach). The patched c2.dll's entry point loads c2trace.dll in whichever process loads it, so
the same hooks work inside link.exe. This wrapper compiles <main.c> and every --with source with /GL (plus the
usual /O2 and the cl args after --), links them into a throw-away DLL with /LTCG under the trace, and hands the
log to the ordinary report (`c2trace.py regs|scratch|il`, or the decoded raw log with `raw`, which takes -H).

--export names the roots (C names; `_name@N`/C++ names as the linker wants them). Without it nothing is exported and
the link runs with /OPT:NOREF, so every function is internal: all may get custom conventions or be inlined away.

Listing. cl writes no listing under /GL, so the link also writes a map file (ltcg.map) and a listing made from the
output image (ltcg.asm, and the <main>.asm path a traced compile would have written): every function of the map
(publics and statics) disassembled with capstone between PROC and ENDP lines, in the /FAsc line format that
tsweep.listing_instrs, tsweep.listing_offsets and regmap read (offsets are from the function's start, so they
equal the IL's encoded offsets). With that, `regmap`, `tsweep`, `rwsweep` and `helpersweep` work on an LTCG build
through this wrapper: the template / TU is the <main> source, the --with sources stay fixed. `asm` builds without
the trace and prints the listing (or one function with --func); --out DIR keeps the objects, the map, the listing
and one `<name>.s` per function in the 'VA mnemonic operands' format --asm-vs takes. The compile cache is bypassed.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace, msvc

SUBS = ("regs", "scratch", "il", "raw", "regmap", "tsweep", "rwsweep", "helpersweep", "asm")
MODS = {"regs": "c2regs", "scratch": "c2scratch", "il": "c2il", "regmap": "regmap", "tsweep": "tsweep",
        "rwsweep": "rwsweep", "helpersweep": "helpersweep"}


def read_map(path):
    """[(name, va, obj)] for the functions of a link map (publics and statics with the `f` flag), by address."""
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="latin-1"):
        m = re.match(r"\s*([0-9a-f]{4}):([0-9a-f]{8})\s+(\S+)\s+([0-9a-f]{8})\s+f\s+(?:i\s+)?(\S+)", line)
        if m and m.group(3) not in [n for n, _, _ in out]:
            out.append((m.group(3), int(m.group(4), 16), m.group(5)))
    return sorted(out, key=lambda t: t[1])


def image_functions(dll, mp):
    """[(name, va, code bytes, obj)]: each map function up to the next one, trailing int3/zero padding removed."""
    import pefile
    pe = pefile.PE(dll, fast_load=True)
    base = pe.OPTIONAL_HEADER.ImageBase
    funcs = read_map(mp)
    out = []
    for k, (name, va, obj) in enumerate(funcs):
        sec = next((s for s in pe.sections if s.VirtualAddress <= va - base < s.VirtualAddress + s.Misc_VirtualSize), None)
        if sec is None:
            continue
        end = sec.VirtualAddress + base + sec.Misc_VirtualSize
        if k + 1 < len(funcs) and funcs[k + 1][1] < end:
            end = funcs[k + 1][1]
        code = pe.get_data(va - base, end - va)
        out.append((name, va, code[:trim(code)], obj))
    return out


def trim(code):
    """Length of `code` without the padding after its last instruction: trailing instructions made only of int3
    (cc) or zero bytes (`add byte ptr [eax], al` = 00 00) are dropped. Bytes are never cut inside an instruction
    (a `jmp rel32` or `ret 4` often ends in 00)."""
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    ends = [(i.address, i.address + i.size, i.bytes) for i in md.disasm(code, 0)]
    n = ends[-1][1] if ends else len(code)
    while ends and (set(ends[-1][2]) <= {0xcc} or set(ends[-1][2]) <= {0}):
        n = ends.pop()[0]
    if code[n:].strip(b"\xcc\x00"):          # undecodable bytes after the last instruction: keep them (data)
        n = len(code.rstrip(b"\xcc"))
    return max(n, 1) if code else 0


def write_listing(dll, mp, asm, s_dir=None):
    """Write the /FAsc-format listing of every function in the image (and per-function .s files into s_dir)."""
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    funcs = image_functions(dll, mp)
    names = {va: n for n, va, _, _ in funcs}
    lines = ["; listing made by tools/c2ltcg.py from %s (link /LTCG)\n" % os.path.basename(dll)]
    for name, va, code, obj in funcs:
        lines.append("; %08x %s\n%s\tPROC NEAR\n" % (va, obj, name))
        s = []
        for i in md.disasm(code, va):
            note = ""
            if i.mnemonic in ("call", "jmp") and i.op_str.startswith("0x"):
                t = int(i.op_str, 16)
                note = "\t; " + names[t] if t in names and t != va else ""
            lines.append("  %05x\t%s\t %s\t %s%s\n" % (i.address - va, " ".join("%02x" % b for b in i.bytes),
                                                      i.mnemonic, i.op_str, note))
            s.append("%08x  %s %s\n" % (i.address, i.mnemonic, i.op_str))
        lines.append("%s\tENDP\n" % name)
        if s_dir:
            os.makedirs(s_dir, exist_ok=True)
            open(os.path.join(s_dir, re.sub(r"[^\w@$?.-]", "_", name) + ".s"), "w").writelines(s)
    open(asm, "w").writelines(lines)
    return funcs


def build(srcs, cl, d, exports, plain, trace=True, extra_env=None, asm=None, gl=True):
    """/GL-compile every source into d, link /LTCG (traced if `trace`) with a map, write the image listing.
    gl=False: the same without /GL and /LTCG (ordinary objects, for a baseline). Returns (rc, output, dll path)."""
    objs, text = [], ""
    for s in srcs:
        o = os.path.join(d, "%d_%s.obj" % (len(objs), os.path.splitext(os.path.basename(s))[0]))
        if os.path.exists(o):
            os.remove(o)
        rc, out = plain(cl + (["/GL"] if gl else []) + ["/Fo" + o, s], trace=False)
        text += out
        if rc or not os.path.exists(o):
            return rc or 1, text, None
        objs.append(o)
    dll = os.path.join(d, "ltcg.dll")
    for p in (dll, dll[:-4] + ".map"):
        if os.path.exists(p):
            os.remove(p)
    rc, out = plain(["--tool", "link", "/nologo", "/DLL", "/NOENTRY"] + (["/LTCG"] if gl else []) + ["/NODEFAULTLIB", "/FORCE:UNRESOLVED",
                     "/OPT:REF" if exports else "/OPT:NOREF", "/OPT:NOICF", "/MAP:" + dll[:-4] + ".map", "/OUT:" + dll]
                    + ["/EXPORT:" + e for e in exports] + objs, trace=trace, extra_env=extra_env)
    text += "\n".join(l for l in out.splitlines() if "LNK4088" not in l and "LNK2001" not in l and "LNK2019" not in l
                      and "LNK4001" not in l and "Creating library" not in l)
    if not os.path.exists(dll):
        return rc or 1, text, None
    write_listing(dll, dll[:-4] + ".map", dll[:-4] + ".asm")
    if asm:
        import shutil
        shutil.copy(dll[:-4] + ".asm", asm)
    return 0, text, dll


def main(argv):
    if not argv or argv[0] not in SUBS:
        sys.exit(__doc__)
    sub, argv = argv[0], argv[1:]
    cut = argv.index("--") if "--" in argv else len(argv)
    head, tail = argv[:cut], argv[cut:]
    more, exports, rest, k = [], [], [], 0
    outdir = func = None
    while k < len(head):
        if head[k] in ("--with", "--export") and k + 1 < len(head):
            (more if head[k] == "--with" else exports).extend(x for x in head[k + 1].split(",") if x)
            k += 2
        elif sub == "asm" and head[k] in ("--out", "--func") and k + 1 < len(head):
            if head[k] == "--out":
                outdir = head[k + 1]
            else:
                func = head[k + 1]
            k += 2
        else:
            rest.append(head[k])
            k += 1
    os.environ["CCACHE"] = "0"
    # The /GL compiles and the traced link run here: a remote compile server (c2trace.run_remote, on by default for
    # some builds) would compile the source as one ordinary object, bypassing ltcg_tool. "0" is the off switch;
    # run_remote is also replaced, so a later change to its default cannot route an LTCG run away.
    os.environ["C2TRACE_REMOTE"] = "0"
    if hasattr(c2trace, "run_remote"):
        c2trace.run_remote = lambda *a, **k: None
    plain = c2trace.run_tool
    more = [os.path.abspath(m) for m in more]

    if sub == "asm":
        import tempfile
        if len(rest) != 1:
            sys.exit(__doc__)
        d = os.path.abspath(outdir) if outdir else tempfile.mkdtemp(prefix="c2ltcg_")
        os.makedirs(d, exist_ok=True)
        cl = ["/nologo", "/c", "/O2"] + tail[1:]
        rc, text, dll = build([os.path.abspath(rest[0])] + more, cl, d, exports, plain, trace=False)
        if dll is None:
            sys.stdout.write(text)
            return rc
        if outdir:
            write_listing(dll, dll[:-4] + ".map", dll[:-4] + ".asm", s_dir=d)
        lst = open(dll[:-4] + ".asm").read()
        if func:
            i, j = c2trace.listing_proc(lst, func)
            lst = lst[i:lst.index("\n", j) + 1]
        sys.stdout.write(lst)
        if outdir:
            print("# kept %s (ltcg.map, ltcg.asm, <function>.s)" % d)
        return 0

    def ltcg_tool(args, trace=True, extra_env=None):
        """Stands in for the one traced `cl /c` of c2trace.run_trace: /GL compiles, then the traced link."""
        src = args[-1]
        obj = next(a[3:] for a in args if a.startswith("/Fo"))
        asm = next((a[3:] for a in args if a.startswith("/Fa")), None)
        cl = [a for a in args[:-1] if not a.startswith(("/FA", "/Fa", "/Fo"))]
        rc, text, dll = build([src] + more, cl, os.path.dirname(obj), exports, plain, trace, extra_env, asm)
        return rc, text

    c2trace.run_tool = ltcg_tool
    if sub == "raw":
        sys.argv = [sys.argv[0]] + rest + tail[1:]
        return c2trace.main()
    import importlib
    mod = importlib.import_module(MODS[sub])
    if sub in ("tsweep", "rwsweep", "helpersweep"):      # these read sys.argv themselves
        sys.argv = [sub + ".py"] + rest + tail
        return mod.main()
    return mod.main(rest + tail)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
