#!/usr/bin/env python3
"""x64check: compile one C++ function with an x64 cl and compare it with the same function in a PE32+ executable,
byte for byte and relocation by relocation. lift2il64 and tidycheck64 use it; it works standalone too.

usage: x64check.py BINARY VA SIZE SOURCE.cpp [--flags /O2,/GS-] [--warn-clean] [--full-diff FILE]

The compiler is a command, set once:
    export C2_CL64="wine /path/to/VC/bin/x86_amd64/cl.exe"     (macOS / Linux; INCLUDE set for the headers)
    export C2_CL64="C:/path/to/VC/bin/x86_amd64/cl.exe"        (Windows)
or any wrapper script that takes cl's arguments. Use the cl that built the executable (cl 17.00 for VS2012, ...).

What counts as the same:
  - every byte outside a relocation field is equal, and the compiled function is not longer (trailing int3 / nop
    padding is fine);
  - every relocation resolves to the address the original uses: a symbol named FUN_<hex> / g_<hex> / DAT_<hex> /
    PTR_<hex> / LAB_<hex> is that address, __ImageBase is the image base, _tls_index the TLS directory's slot, a label inside the function is the same
    offset in the original, and a string or float literal ($SG, ??_C@, __real@, __xmm@) is any copy of the same bytes
    (the linker chooses which pooled copy); thread-local variables compare by their offset in .tls (SECREL).
The target is the defined function whose name carries FUN_<va>, else the one non-inline function the file defines.

Exit codes: 0 match, 1 no match / does not build, 2 usage.
"""
import difflib, os, re, shlex, shutil, struct, subprocess, sys, tempfile

BASE_FLAGS = ["/c", "/nologo", "/Gy"]
PLACEHOLDER = re.compile(r"(?:^|[?@_])(?:FUN|DAT|PTR|LAB|g|thunk_FUN)_([0-9a-fA-F]{5,16})(?![0-9a-fA-F])")
LITERAL = re.compile(r"^(\$SG\d+$|\?\?_C@|__real@[0-9a-f]+$|__xmm@[0-9a-f]+$)")
FUNCLET = re.compile(r"\?(?:dtor|catch|fin|filt|unwind)\$\d+@|__(?:catch|unwindfunclet|tryblocklevelfunclet)\$")
M64 = (1 << 64) - 1


# ------------------------------------------------------------------------------------------------- object and image

class Coff:
    """An AMD64 COFF object: sections (data, relocations, COMDAT selection) and symbols by index."""

    def __init__(self, path):
        d = open(path, "rb").read()
        machine, nsec, _, symoff, nsym, opt, _ = struct.unpack_from("<HHIIIHH", d, 0)
        if machine != 0x8664:
            raise ValueError("not an AMD64 object (machine 0x%x; /GL and /bigobj objects are not supported)" % machine)
        strtab = symoff + 18 * nsym

        def name8(raw):
            if raw[:4] == b"\0\0\0\0":
                (o,) = struct.unpack_from("<I", raw, 4)
                return d[strtab + o:d.index(b"\0", strtab + o)].decode("latin-1")
            return raw.rstrip(b"\0").decode("latin-1")
        self.sections = [None]
        for k in range(nsec):
            h = 20 + opt + 40 * k
            nm = d[h:h + 8]
            _, _, rawsize, rawptr, relptr, _, nrel, _, flags = struct.unpack_from("<IIIIIIHHI", d, h + 8)
            relocs = [struct.unpack_from("<IIH", d, relptr + 10 * j) for j in range(nrel)]
            self.sections.append(dict(name=name8(nm) if not nm.startswith(b"/") else nm.decode("latin-1"),
                                      data=d[rawptr:rawptr + rawsize] if rawptr else b"\0" * rawsize,
                                      relocs=relocs, flags=flags, comdat=None))
        self.symbols = {}
        k = 0
        while k < nsym:
            h = symoff + 18 * k
            value, sec, typ, storage, naux = struct.unpack_from("<IhHBB", d, h + 8)
            sym = dict(name=name8(d[h:h + 8]), value=value, section=sec, type=typ, storage=storage)
            self.symbols[k] = sym
            if storage == 3 and naux and value == 0 and 0 < sec < len(self.sections):   # a section definition
                sel = d[symoff + 18 * (k + 1) + 14]
                if self.sections[sec]["flags"] & 0x1000:
                    self.sections[sec]["comdat"] = sel
            k += 1 + naux

    def functions(self):
        return [s for s in self.symbols.values() if s["section"] > 0 and s["type"] & 0x20 and not FUNCLET.search(s["name"])]


class Pe:
    """The original PE32+: read by virtual address."""

    def __init__(self, path):
        d = open(path, "rb").read()
        (pe,) = struct.unpack_from("<I", d, 0x3C)
        if d[pe:pe + 4] != b"PE\0\0":
            raise ValueError("%s is not a PE executable" % path)
        machine, nsec, _, _, _, opt, _ = struct.unpack_from("<HHIIIHH", d, pe + 4)
        if machine != 0x8664 or struct.unpack_from("<H", d, pe + 24)[0] != 0x20B:
            raise ValueError("%s is not an x64 (PE32+) executable" % path)
        (self.base,) = struct.unpack_from("<Q", d, pe + 24 + 24)
        self.data, self.secs = d, []
        for k in range(nsec):
            h = pe + 24 + opt + 40 * k
            vsize, rva, rawsize, rawptr = struct.unpack_from("<IIII", d, h + 8)
            self.secs.append((d[h:h + 8].rstrip(b"\0").decode("latin-1"), self.base + rva, vsize, rawptr, rawsize))
        tls_rva, tls_size = struct.unpack_from("<II", d, pe + 24 + 112 + 8 * 9)   # data directory 9: TLS
        self.tls_index = struct.unpack_from("<Q", self.read(self.base + tls_rva, 24), 16)[0] if tls_size else None

    def read(self, va, n):
        for _, v, vs, p, ps in self.secs:
            if v <= va and va + n <= v + max(vs, ps):
                out = self.data[p + va - v:p + min(va - v + n, ps)]
                return out + b"\0" * (n - len(out))
        raise ValueError("0x%x (+%d) is outside the image" % (va, n))

    def secrel(self, va):
        tls = [s for s in self.secs if s[0].startswith(".tls")]
        if not tls or not tls[0][1] <= va < tls[0][1] + max(tls[0][2], 1):
            raise ValueError("SECREL target 0x%x is not in .tls" % va)
        return va - tls[0][1]


# ---------------------------------------------------------------------------------------------------------- compare

def find_target(coff, va, size):
    fns = coff.functions()
    pat = re.compile(r"(?:^|[?@_])FUN_%x(?:@|$)" % va, re.I)
    hits = sorted({s["name"] for s in fns if pat.search(s["name"])})
    if not hits:
        hits = sorted({s["name"] for s in fns if s["storage"] in (2, 3) and coff.sections[s["section"]]["comdat"] != 2})
    if len(hits) != 1:     # templates and inline functions are COMDAT "any": the one function of the target's size
        same = sorted({s["name"] for s in fns if s["value"] == 0 and len(coff.sections[s["section"]]["data"]) == size
                       and sum(1 for x in fns if x["section"] == s["section"]) == 1})
        if len(same) == 1:
            hits = same
    if len(hits) != 1:
        return None, ("FUN_%x is not defined in the source" % va if not hits else
                      "the source defines %d functions that are not inline (%s): define only the target"
                      % (len(hits), ", ".join(hits[:4])))
    return next(s for s in fns if s["name"] == hits[0]), None


def literal_bytes(coff, sym):
    name = sym["name"]
    if name.startswith(("__real@", "__xmm@")):
        return bytes.fromhex(name.split("@", 1)[1])[::-1]
    if sym["section"] > 0:
        sec, start = coff.sections[sym["section"]]["data"], sym["value"]
        nxt = [x["value"] for x in coff.symbols.values() if x["section"] == sym["section"] and x["value"] > start]
        return sec[start:min(nxt) if nxt and not name.startswith("??_C@") else len(sec)]
    return b""


def compare(coff, pe, target, va, size):
    """-> list of errors (empty: the same function)."""
    sec = coff.sections[target["section"]]
    start = target["value"]
    mine, orig = sec["data"][start:start + size], pe.read(va, size)
    errors, masked = [], set()
    nxt = [s["value"] for s in coff.functions() if s["section"] == target["section"] and s["value"] > start]
    end = min(nxt) if nxt else len(sec["data"])
    if end - start < size or any(b not in (0xCC, 0x90) for b in sec["data"][start + size:end]):
        errors.append("compiled function is %d bytes, the original %d" % (end - start, size))

    def resolve(sym, addend):
        if sym["name"] == "__ImageBase":
            return pe.base + addend
        if sym["name"] == "_tls_index" and pe.tls_index is not None:     # from the image's TLS directory
            return pe.tls_index + addend
        m = PLACEHOLDER.search(sym["name"])
        if m and sym["section"] <= 0:
            return int(m.group(1), 16) + addend
        if sym["section"] == target["section"] and start <= sym["value"] + addend < start + size:
            return va + sym["value"] + addend - start             # a label or table inside the function
        if sym["section"] == -1:
            return sym["value"] + addend
        raise ValueError("cannot resolve %s: name globals g_<hex> and functions FUN_<hex>" % sym["name"])

    for off, ix, kind in sec["relocs"]:
        if not start <= off < start + size:
            continue
        rel = off - start
        width = 8 if kind == 0x01 else 4
        masked.update(range(rel, rel + width))
        sym = coff.symbols.get(ix, dict(name="?", section=0, value=0))
        try:
            if rel + width > size:
                raise ValueError("relocation crosses the function end")
            if 0x04 <= kind <= 0x09:                                # REL32, REL32_1 .. REL32_5
                (addend,) = struct.unpack_from("<i", mine, rel)
                observed = (va + rel + 4 + (kind - 4) + struct.unpack_from("<i", orig, rel)[0]) & M64
                if LITERAL.match(sym["name"]):
                    data = literal_bytes(coff, sym)
                    if data and pe.read((observed - addend) & M64, len(data)) == data:
                        continue
                    raise ValueError("literal %s: the original reads other bytes" % sym["name"])
                expected = resolve(sym, addend) & M64
            elif kind == 0x01:                                       # ADDR64
                expected = resolve(sym, struct.unpack_from("<q", mine, rel)[0]) & M64
                (observed,) = struct.unpack_from("<Q", orig, rel)
            elif kind == 0x02:                                       # ADDR32
                expected = resolve(sym, struct.unpack_from("<I", mine, rel)[0]) & 0xFFFFFFFF
                (observed,) = struct.unpack_from("<I", orig, rel)
            elif kind == 0x03:                                       # ADDR32NB (image-relative)
                expected = resolve(sym, struct.unpack_from("<I", mine, rel)[0]) & M64
                observed = (struct.unpack_from("<I", orig, rel)[0] + pe.base) & M64
            elif kind == 0x0B:                                       # SECREL (thread-local offset)
                expected = pe.secrel(resolve(sym, struct.unpack_from("<I", mine, rel)[0]))
                (observed,) = struct.unpack_from("<I", orig, rel)
            else:
                raise ValueError("relocation kind %d" % kind)
            if expected != observed:
                raise ValueError("%s resolves to 0x%x, the original uses 0x%x" % (sym["name"], expected, observed))
        except ValueError as e:
            errors.append("+0x%x: %s" % (rel, e))
    bad = [k for k in range(min(len(mine), size)) if k not in masked and mine[k] != orig[k]]
    if bad or len(mine) < size:
        errors.insert(0, "%d bytes differ (first at +0x%x)" % (len(bad), bad[0] if bad else len(mine)))
    return errors


def listing(code, base, reloc_names=None):
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    out = []
    for i in md.disasm(code, base):
        o = re.sub(r"\[rip [+-] 0x[0-9a-f]+\]", "[rip]", i.op_str)
        if (i.mnemonic == "call" or i.mnemonic.startswith("j")) and re.fullmatch(r"0x[0-9a-f]+", o):
            t = int(o, 16)
            o = "+0x%x" % (t - base) if base <= t < base + len(code) else "<target>"
        out.append(("+0x%04x" % (i.address - base), "%s %s" % (i.mnemonic, o)))
    return out


def run_cl(args, cwd):
    cmd = os.environ.get("C2_CL64")
    if not cmd:
        raise SystemExit("x64check: set C2_CL64 to the x64 compiler command (see x64check.py's usage)")
    p = subprocess.run(shlex.split(cmd) + args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def check(pe, va, size, src, flags, warn_clean=False, full_diff=None):
    """Compile src (a path) under flags and compare with the function at va. -> dict(status=match / no match /
    does not build, flags, errors, differing_instructions, diff)."""
    res = dict(address="0x%x" % va, flags=list(flags))
    with tempfile.TemporaryDirectory(prefix="x64check") as t:
        cpp, obj = os.path.join(t, "candidate.cpp"), os.path.join(t, "candidate.obj")
        shutil.copy(src, cpp)
        rc, out = run_cl(BASE_FLAGS + list(flags) + (["/W3"] if warn_clean else ["/W0"]) + ["/Fo" + obj,
                                                                                            "candidate.cpp"], t)
        errors = [l.strip() for l in out.splitlines() if re.search(r"\b(error|fatal error) C\d+", l)]
        if rc or not os.path.exists(obj):
            res.update(status="does not build", errors=errors or out.splitlines()[-10:])
            return res
        warnings = [l.strip() for l in out.splitlines() if re.search(r"candidate\.cpp\(\d+\) ?: warning C\d+", l)]
        if warn_clean and warnings:
            res.update(status="does not build", errors=["warning: " + w for w in warnings])
            return res
        try:
            coff = Coff(obj)
        except ValueError as e:
            res.update(status="no match", errors=[str(e)])
            return res
        target, why = find_target(coff, va, size)
        if not target:
            res.update(status="no match", errors=[why])
            return res
        res["target"] = target["name"]
        errs = compare(coff, pe, target, va, size)
        if not errs:
            res["status"] = "match"
            return res
        sec = coff.sections[target["section"]]["data"]
        a, b = listing(pe.read(va, size), va), listing(sec[target["value"]:target["value"] + max(size, 1)], 0)
        lines = []
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [x[1] for x in a], [x[1] for x in b],
                                                          autojunk=False).get_opcodes():
            for k in range(max(i2 - i1, j2 - j1)):
                o = a[i1 + k] if i1 + k < i2 else ("", "")
                y = b[j1 + k] if j1 + k < j2 else ("", "")
                lines.append("%s %-8s %-40s | %s" % (" " if op == "equal" else "!", o[0], o[1], y[1]))
        nxt = [s["value"] for s in coff.functions() if s["section"] == target["section"] and s["value"] > target["value"]]
        res["rows"] = [a, b]                        # the two listings (x64metrics) and our function's length
        res["extent"] = (min(nxt) if nxt else len(sec)) - target["value"]
        res.update(status="no match", errors=errs, differing_instructions=sum(1 for l in lines if l[0] == "!"),
                   diff="\n".join(lines[:120]) + ("\n... %d more lines" % (len(lines) - 120) if len(lines) > 120 else ""))
        if full_diff:
            open(full_diff, "w").write("\n".join(lines) + "\n")
        return res


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("binary")
    ap.add_argument("va")
    ap.add_argument("size", type=int)
    ap.add_argument("source")
    ap.add_argument("--flags", default="/O2,/GS-")
    ap.add_argument("--warn-clean", action="store_true", help="fail on /W3 warnings")
    ap.add_argument("--full-diff")
    a = ap.parse_args(argv)
    r = check(Pe(a.binary), int(a.va, 16), a.size, a.source, a.flags.split(","), a.warn_clean, a.full_diff)
    print(r["status"].upper(), r.get("differing_instructions", ""), " ".join(r["flags"]))
    for e in r.get("errors") or []:
        print("  " + e)
    if r.get("diff"):
        print(r["diff"])
    return 0 if r["status"] == "match" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
