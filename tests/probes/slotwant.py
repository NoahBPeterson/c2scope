# slotwant.py (docs/c2/slotwant.md): the frame-colouring inverse. Run by run_tests.py's probe_modules() in its namespace
# (check, orig_from_listing, HERE, VERBOSE). Offline: the source model and the levers' text on fixtures; compiled:
# our own compile of a variant is "the original" and the tool must name the edit that undoes the perturbation.
import os, re, subprocess, sys, tempfile


def slotwant_offline_tests():
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import slotwant as SW
    D = os.path.join(HERE, "tests", "data", "slotwant")
    src = SW.Source(os.path.join(D, "scope.cpp"))
    body = src.body(5)
    sts = [src.text[a:b].strip().split("\n")[0] for a, b in src.statements(*body)]
    check("slotwant source: the body's statements (an if with its block is one)",
          sts, ["char a[64];", "ext(a);", "g = a[0];", "if (n) {", "g += n;"])
    da, db = SW.find_decl(src, body, "a", 8), SW.find_decl(src, body, "b", 12)
    check("slotwant source: declarations found by name and line (type, dims, initializer)",
          (da.type, da.dims, da.init, db.type, src.line_of(db.st)), ("char", "[64]", False, "char", 11))
    n = SW.lever_narrow(src, da)
    out = n.apply(src.text).split("\n")
    check("slotwant lever narrow: `{` on the declaration's line, `}` after the last use; no line moves",
          (out[6].strip(), out[8].strip(), len(out) == len(src.text.split("\n"))),
          ("{ char a[64];", "g = a[0]; }", True))
    w = SW.lever_widen(src, body, db)
    out = w.apply(src.text).split("\n")
    check("slotwant lever widen: the block's declaration moved to the function body's first line",
          (out[5].strip(), out[10].strip()), ("{ char b[64];", ""))
    sh = SW.lever_share(src, body, da, db)
    out = sh.apply(src.text)
    check("slotwant lever share: b's declaration gone, its uses renamed to a (a already in the enclosing block)",
          (out.count("char b[64]"), "ext(a);\n        g += a[1];" in out, out.split("\n")[6].strip()),
          (0, True, "char a[64];"))
    src2 = SW.Source(os.path.join(D, "size.cpp"))
    b2 = src2.body(9)
    dh = SW.find_decl(src2, b2, "h", 13)
    z = SW.lever_size(src2, dh, 64, 256)
    check("slotwant lever size: an array's dimension from the element's byte size", (z.name, z.what),
          ("size:h@L12=64", "`h[256]` -> `h[64]`"))


slotwant_offline_tests()


def slotwant_compile_tests():
    """Perturbation round trips (13.10 and 14.00 alike): the original is our compile of X_orig, ours the perturbed
    source; the top verified lever must give the original's packing (verdict WANT)."""
    D = os.path.join(HERE, "tests", "data", "slotwant")
    d = tempfile.mkdtemp()
    sw = [sys.executable, os.path.join(HERE, "tools", "slotwant.py")]
    for ours, orig, func, lever in (("scope.cpp", "scope_orig.cpp", "f", "narrow:a@L7"),
                                    ("size.cpp", "size64.cpp", "f", "size:h@L12=64")):
        kd = os.path.join(d, orig)
        # the original's listing: a plain traced compile of the variant (slotwant's base compile keeps it)
        subprocess.run(sw + [os.path.join(D, orig), "--func", func, "--diag", "--keep", kd,
                             "--", "/O2"], capture_output=True, text=True, cwd=HERE)
        asm = os.path.join(kd, "base", os.path.splitext(orig)[0] + ".asm")
        if not os.path.exists(asm):
            check("slotwant %s: the original's compile" % ours, False, True)
            continue
        o = orig_from_listing(asm, func, os.path.join(d, orig + ".s"))
        r = subprocess.run(sw + [os.path.join(D, ours), "--func", func, "--asm-vs", o, "--keep", os.path.join(d, ours),
                                 "--jobs", "4", "--", "/O2"], capture_output=True, text=True, cwd=HERE).stdout
        if VERBOSE:
            print(r[-4000:])
        top = re.findall(r"^  \[\d+\] (\S+)\s+(WANT|better|no|worse)", r, re.M)
        check("slotwant %s: the original's frame wanted; top verified lever %s, every want met" % (ours, lever),
              (top[0] if top else None), (lever, "WANT"))


slotwant_compile_tests()
