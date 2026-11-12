#!/usr/bin/env python3
"""Trace c2.dll while link.exe drives it: /GL objects, code generation at link /LTCG.  Doc: docs/c2/port-1500.md

usage: c2ltcg.py [--c2 BUILD] regs|scratch|il|raw <main.c> --with other.c[,more.c] [--export sym[,sym]]
                 [the subcommand's own options, e.g. --func S --why a,b] [-- <cl args>]

Under /GL the compiler proper writes IL objects and c2.dll generates no code; `link /LTCG` loads c2.dll and runs
the whole back end over all objects at once (cross-module inlining, custom register conventions for functions
nothing outside can reach). The patched c2.dll's entry point loads c2trace.dll in whichever process loads it, so
the same hooks work inside link.exe. This wrapper compiles <main.c> and every --with source with /GL (plus the
usual /O2 and the cl args after --), links them into a throw-away DLL with /LTCG under the trace, and hands the
log to the ordinary report (`c2trace.py regs|scratch|il`, or the decoded raw log with `raw`, which takes -H).

--export names the roots (C names; `_name@N`/C++ names as the linker wants them). Without it nothing is exported and
the link runs with /OPT:NOREF, so every function is internal: all may get custom conventions or be inlined away.
There is no listing in this mode (cl writes none under /GL), so `regmap` and `scratch`'s listing offsets come from
the IL's encoded lengths only. The compile cache is bypassed.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace, msvc


def main(argv):
    if not argv or argv[0] not in ("regs", "scratch", "il", "raw"):
        sys.exit(__doc__)
    sub, argv = argv[0], argv[1:]
    cut = argv.index("--") if "--" in argv else len(argv)
    head, tail = argv[:cut], argv[cut:]
    more, exports, rest, k = [], [], [], 0
    while k < len(head):
        if head[k] in ("--with", "--export") and k + 1 < len(head):
            (more if head[k] == "--with" else exports).extend(x for x in head[k + 1].split(",") if x)
            k += 2
        else:
            rest.append(head[k])
            k += 1
    os.environ["CCACHE"] = "0"
    plain = c2trace.run_tool

    def ltcg_tool(args, trace=True, extra_env=None):
        """Stands in for the one traced `cl /c` of c2trace.run_trace: /GL compiles, then the traced link."""
        src = args[-1]
        obj = next(a[3:] for a in args if a.startswith("/Fo"))
        d = os.path.dirname(obj)
        cl = [a for a in args[:-1] if not a.startswith(("/FA", "/Fa", "/Fo"))]
        objs, text = [], ""
        for s in [src] + [os.path.abspath(m) for m in more]:
            o = os.path.join(d, "%d_%s.obj" % (len(objs), os.path.splitext(os.path.basename(s))[0]))
            rc, out = plain(cl + ["/GL", "/Fo" + o, s], trace=False)
            text += out
            if rc or not os.path.exists(o):
                return rc or 1, text
            objs.append(o)
        dll = os.path.join(d, "ltcg.dll")
        rc, out = plain(["--tool", "link", "/nologo", "/DLL", "/NOENTRY", "/LTCG", "/NODEFAULTLIB", "/FORCE:UNRESOLVED",
                         "/OPT:REF" if exports else "/OPT:NOREF", "/MAP:" + dll[:-4] + ".map", "/OUT:" + dll]
                        + ["/EXPORT:" + e for e in exports] + objs, trace=trace, extra_env=extra_env)
        text += "\n".join(l for l in out.splitlines() if "LNK4088" not in l and "LNK2001" not in l and "LNK2019" not in l
                          and "LNK4001" not in l and "Creating library" not in l)
        return (0 if os.path.exists(dll) else rc or 1), text

    c2trace.run_tool = ltcg_tool
    if sub == "raw":
        sys.argv = [sys.argv[0]] + rest + tail[1:]
        return c2trace.main()
    import importlib
    mod = importlib.import_module({"regs": "c2regs", "scratch": "c2scratch", "il": "c2il"}[sub])
    return mod.main(rest + tail)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
