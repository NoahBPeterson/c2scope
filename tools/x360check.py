#!/usr/bin/env python3
"""x360check: compile one C++ function with the Xbox 360 cl and compare it with the same function in a game image,
word by word and relocation by relocation.  Doc: docs/c2/port-x360.md

usage: x360check.py IMAGE.pe VA SIZE SOURCE.cpp [--flags /O2] [--diff] [--func NAME]

IMAGE is a PE image (x360img.py pe GAME.xex OUT.pe); VA its virtual address (0x82..., as the game sees it); SIZE in
bytes (a multiple of 4). The compiler is the c2 build 15.00.8153-xbox360 (tools/c2builds), run by msvc.py.

What counts as the same: every 32-bit instruction word is equal, except the words a relocation patches, which must
resolve to what the original uses:
  - REL24 (`bl` / `b` to a symbol): a symbol named FUN_<hex> must be the original branch's target; any other name
    is only checked to be a branch of the same kind (no symbol table for the game);
  - REFHI / REFLO (the `lis` + `addi` / `lwz` / `lfs` .. pair of a global's address): a g_ / DAT_ / FUN_ <hex> symbol
    must give the original's 16-bit halves (hi = (A + 0x8000) >> 16, lo = A & 0xffff); another name (a string or float
    literal) is compared as "some address" only;
  - ADDR32 (a word holding an address, jump tables): the same rule for whole words.
The compiled function may end with padding; it must not be longer. Exit codes: 0 match, 1 no match / no build, 2 usage.
"""
import os, re, struct, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
if not os.environ.get("C2_BUILD", "").startswith(("15.00.8153", "xbox360", "8153", "xenon")):
    os.environ["C2_BUILD"] = "15.00.8153-xbox360"      # before msvc / c2build are imported: they read it then

PLACEHOLDER = re.compile(r"(?:^|[?@_])(?:FUN|DAT|PTR|LAB|g)_([0-9a-fA-F]{8})(?![0-9a-fA-F])")
REL24, REL14, ADDR32, REFHI, REFLO, PAIR = 0x6, 0x7, 0x2, 0x10, 0x11, 0x12
MD = None


def md():
    global MD
    if MD is None:
        import capstone
        MD = capstone.Cs(capstone.CS_ARCH_PPC, capstone.CS_MODE_32 | capstone.CS_MODE_BIG_ENDIAN)
    return MD


def dis_word(w, addr):
    """One instruction word as text; VMX128 (Xbox 360 vector, primary opcode 4/5/6) and unknown words as .long."""
    g = next(md().disasm_lite(w, addr), None)
    return "%s %s" % (g[2], g[3]) if g else ".long 0x%08x" % struct.unpack(">I", w)[0]


class Obj:
    """A PowerPC (big-endian code) COFF object: sections, relocations by section, symbols by index."""

    def __init__(self, path):
        d = open(path, "rb").read()
        machine, nsec, _t, symoff, nsym, opt, _c = struct.unpack_from("<HHIIIHH", d, 0)
        if machine != 0x1F2:
            raise ValueError("not an Xbox 360 (POWERPCBE) object: machine 0x%x" % machine)
        strtab = symoff + 18 * nsym

        def name8(raw):
            if raw[:4] == b"\0\0\0\0":
                o = strtab + struct.unpack_from("<I", raw, 4)[0]
                return d[o:d.index(b"\0", o)].decode("latin-1")
            return raw.rstrip(b"\0").decode("latin-1")
        self.sections = [None]
        for k in range(nsec):
            h = 20 + opt + 40 * k
            _vs, _va, size, rawp, relp, _lp, nrel, _nl, ch = struct.unpack_from("<IIIIIIHHI", d, h + 8)
            rel = [struct.unpack_from("<IIH", d, relp + 10 * j) for j in range(nrel)]
            self.sections.append(dict(data=d[rawp:rawp + size] if rawp else bytes(size), relocs=rel, flags=ch))
        self.syms, self.defs, k = {}, [], 0
        while k < nsym:
            o = symoff + 18 * k
            nm = name8(d[o:o + 8])
            val, sec, typ, cls, naux = struct.unpack_from("<IhHBB", d, o + 8)
            self.syms[k] = (nm, sec, val, cls)
            if sec > 0 and cls in (2, 3) and typ & 0x20 and self.sections[sec]["flags"] & 0x20:
                self.defs.append((nm, sec, val))
            k += 1 + naux

    def function(self, va=None, name=None):
        c = [f for f in self.defs if (name and name in f[0]) or (va and re.search(r"FUN_%08x" % va, f[0], re.I))]
        if not c:
            c = [f for f in self.defs if f[2] == 0] if len(self.defs) == 1 else []
        if len(c) != 1:
            raise SystemExit("x360check: %d candidate functions in the object (%s); name it FUN_%08x or pass --func"
                             % (len(c), ", ".join(f[0] for f in self.defs), va or 0))
        nm, sec, off = c[0]
        s = self.sections[sec]
        rel = {}
        for o, si, t in s["relocs"]:
            if t == PAIR:
                continue
            rel[o - (o % 4)] = (t, self.syms.get(si, ("?",))[0], o)
        return nm, s["data"][off:], {o - off: r for o, r in rel.items() if o >= off}


def image(path):
    import pefile
    pe = pefile.PE(path, fast_load=True)
    return pe.OPTIONAL_HEADER.ImageBase, pe.get_memory_mapped_image()


def compare(code, rel, orig, va):
    """-> list of (offset, kind, ours text, orig text) for every word that differs; [] = match."""
    out = []
    n = len(orig)
    if len(code.rstrip(b"\0")) > n:
        out.append((n, "longer", "%d bytes" % len(code.rstrip(b"\0")), "%d bytes" % n))
    for o in range(0, min(len(code), n), 4):
        a, b = code[o:o + 4], orig[o:o + 4]
        if o in rel:
            t, sym, _ro = rel[o]
            m = PLACEHOLDER.search(sym)
            want = int(m.group(1), 16) if m else None
            ow = struct.unpack(">I", b)[0]
            if (struct.unpack(">I", a)[0] ^ ow) & 0xFC000000:
                out.append((o, "opcode", dis_word(a, va + o), dis_word(b, va + o)))
                continue
            if t in (REL24,):
                disp = ow & 0x03FFFFFC
                disp = disp - (1 << 26) if disp & 0x02000000 else disp
                tgt = (va + o + disp) if not (ow & 2) else disp
                if want is not None and tgt != want:
                    out.append((o, "call target", "%s -> %08x" % (sym, want), "-> %08x" % tgt))
            elif t == REFHI:
                if want is not None and (ow & 0xffff) != ((want + 0x8000) >> 16) & 0xffff:
                    out.append((o, "address hi", "%s hi %04x" % (sym, ((want + 0x8000) >> 16) & 0xffff), "hi %04x" % (ow & 0xffff)))
                elif (struct.unpack(">I", a)[0] ^ ow) & 0xFFFF0000:
                    out.append((o, "register", dis_word(a, va + o), dis_word(b, va + o)))
            elif t == REFLO:
                if want is not None and (ow & 0xffff) != want & 0xffff:
                    out.append((o, "address lo", "%s lo %04x" % (sym, want & 0xffff), "lo %04x" % (ow & 0xffff)))
                elif (struct.unpack(">I", a)[0] ^ ow) & 0xFFFF0000:
                    out.append((o, "register", dis_word(a, va + o), dis_word(b, va + o)))
            elif t == ADDR32:
                if want is not None and ow != want:
                    out.append((o, "address", "%s %08x" % (sym, want), "%08x" % ow))
            continue
        if a != b:
            out.append((o, "word", dis_word(a, va + o), dis_word(b, va + o)))
    return out


def listing(code, rel, orig, va, diffs):
    bad = {d[0] for d in diffs}
    lines = []
    for o in range(0, max(len(code), len(orig)), 4):
        a, b = code[o:o + 4], orig[o:o + 4]
        r = rel.get(o)
        lines.append("%s %08x  %-36s | %-36s%s" % ("!" if o in bad else " ", va + o,
                     dis_word(b, va + o) if len(b) == 4 else "", dis_word(a, va + o) if len(a) == 4 else "",
                     "  ; %s" % r[1] if r else ""))
    return "\n".join(lines)


def check(img, va, size, src, flags, func=None, diff=False):
    import msvc
    base, mem = image(img)
    orig = bytes(mem[va - base: va - base + size])
    with tempfile.TemporaryDirectory() as d:
        obj = os.path.join(d, "f.obj")
        rc, out = msvc.run("cl", ["/nologo", "/c", *flags, "/Fo" + obj, os.path.abspath(src)], capture=True)
        if rc or not os.path.exists(obj):
            print(out)
            return 1, None
        nm, code, rel = Obj(obj).function(va, func)
    code = code[:max(size, len(code.rstrip(b"\0")))]
    diffs = compare(code, rel, orig, va)
    if diff or diffs:
        print(listing(code, rel, orig, va, diffs))
    same = sum(1 for o in range(0, min(len(code), size), 4) if o not in {x[0] for x in diffs})
    print("%s %s: %s (%d of %d words agree)" % (nm, "%08x" % va, "MATCH" if not diffs else "DIFF", same, size // 4))
    for o, k, a, b in diffs[:12]:
        print("  +%03x %-12s ours %-30s original %s" % (o, k, a, b))
    return (0 if not diffs else 1), diffs


def main(argv):
    if len(argv) < 4:
        sys.exit(__doc__)
    flags = ["/O2"]
    if "--flags" in argv:
        flags = argv[argv.index("--flags") + 1].split(",")
    func = argv[argv.index("--func") + 1] if "--func" in argv else None
    rc, _ = check(argv[0], int(argv[1], 16), int(argv[2], 0), argv[3], flags, func, "--diff" in argv)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
