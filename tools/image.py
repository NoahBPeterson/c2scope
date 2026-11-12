"""Shared access to an original binary in Bin/ and its PDB index (work/index/<file name>.csv).

Image(name)       name is the file stem, e.g. "cl", "c2", "c1xx", "link"
  .read(va, n)    bytes at a VA
  .func(key)      index row for a VA (int) or an exact mangled symbol (str); None if absent
  .relocs         set of VAs covered by base relocations (DLLs have them; the exes don't)
  .reloc_mask(va, n)  offsets in [0, n) whose bytes a base relocation (or, without .reloc, an image-address
                  displacement/immediate operand) covers: the bytes an object file leaves as relocations
"""
import bisect, csv, glob, os, struct

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_binary(name):
    """Bin/ file by full name (c2.dll) or by stem (c2) when the stem is unique (vcbuild needs .exe or .dll)."""
    files = [p for p in glob.glob(os.path.join(ROOT, "Bin", "*")) if os.path.isfile(p)]
    hit = [p for p in files if os.path.basename(p).lower() == name.lower()]
    hit = hit or [p for p in files if os.path.basename(p).lower().rsplit(".", 1)[0] == name.lower()
                  and p.lower().endswith((".exe", ".dll"))]
    if len(hit) != 1:
        raise SystemExit("Bin/%s: %s" % (name, "ambiguous, give the extension" if hit else "not found"))
    return hit[0]


class Image:
    def __init__(self, name):
        import pefile
        import c2build
        tbl = c2build.table() if name.lower() in ("c2", "c2.dll") else None      # another c2 build (port-1310.md)
        self.path = c2build.orig_c2() if tbl else find_binary(name)
        self.name = os.path.basename(self.path).lower()
        self.pe = pefile.PE(self.path, fast_load=True)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.size = self.pe.OPTIONAL_HEADER.SizeOfImage
        self.data = self.pe.get_memory_mapped_image()
        self.rows, self.by_va, self.by_sym = [], {}, {}
        idx = os.path.join(ROOT, "work", "index", self.name + ".csv")
        if tbl:
            for r in c2build.index_rows():
                self.rows.append(r)
                self.by_va.setdefault(r["va"], r)
                self.by_sym.setdefault(r["symbol"], r)
        elif os.path.exists(idx):
            for r in csv.DictReader(open(idx)):
                r["va"] = int(r["va"], 16)
                r["size"] = int(r["size"])
                self.rows.append(r)
                self.by_va.setdefault(r["va"], r)
                self.by_sym.setdefault(r["symbol"], r)
        self.starts = sorted(self.by_va)
        self.relocs = self._relocs()
        self.imports = {}
        try:
            self.pe.parse_data_directories(directories=[1])
            for e in getattr(self.pe, "DIRECTORY_ENTRY_IMPORT", []):
                for i in e.imports:
                    self.imports[i.address] = "%s!%s" % (e.dll.decode().lower().rsplit(".", 1)[0],
                                                         i.name.decode() if i.name else "ord%d" % i.ordinal)
        except Exception:
            pass

    def name_at(self, va):
        """Symbol or import name at a VA, else None."""
        r = self.by_va.get(va)
        return r["symbol"] if r else self.imports.get(va)

    def _relocs(self):
        d = self.pe.OPTIONAL_HEADER.DATA_DIRECTORY[5]
        out = set()
        if not d.Size:
            return out
        raw = self.data[d.VirtualAddress:d.VirtualAddress + d.Size]
        o = 0
        while o + 8 <= len(raw):
            page, size = struct.unpack_from("<II", raw, o)
            if size < 8:
                break
            for e in struct.unpack_from("<%dH" % ((size - 8) // 2), raw, o + 8):
                if e >> 12 == 3:
                    out.add(self.base + page + (e & 0xFFF))
            o += size
        return out

    def read(self, va, n):
        o = va - self.base
        return bytes(self.data[o:o + n])

    def func(self, key):
        if isinstance(key, int):
            return self.by_va.get(key)
        r = self.by_sym.get(key)
        if r is None and key[:1] in "@_" and any(k.startswith("?") for k in self.by_sym):
            # a build whose PDB has C++ names (cl 15.00: ?Color@@YIXPAUtagFUNC@@@Z): accept the 14.00 spelling
            # (@Color@4, _Name) when exactly one function has that base name
            base = key[1:].split("@")[0]
            hit = [v for k, v in self.by_sym.items() if k.startswith("?%s@@Y" % base)]
            r = hit[0] if len(hit) == 1 else None
        return r

    def in_image(self, v):
        return self.base <= v < self.base + self.size

    def reloc_mask(self, va, n):
        m = set()
        if self.relocs:
            for k in range(-3, n):
                if va + k in self.relocs:
                    m.update(x for x in range(k, k + 4) if 0 <= x < n)
            return m
        import capstone
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        md.detail = True
        code = self.read(va, n)
        for i in md.disasm(code, 0):
            for at, size in ((i.disp_offset, i.disp_size), (i.imm_offset, i.imm_size)):
                if at and size == 4:
                    v = int.from_bytes(code[i.address + at:i.address + at + 4], "little")
                    if self.in_image(v):
                        m.update(range(i.address + at, i.address + at + 4))
        return m

    def paired_mask(self, va, n, mine, rmask):
        """reloc_mask(va, n), widened against our code `mine` whose relocated byte offsets are `rmask` (an .obj's
        relocations, or a linked image's .reloc). Without a .reloc table the original's mask is only a guess from
        image-address operands, and masking our side but not theirs makes an instruction (or a jump-table entry)
        look different although it is the same relocation: every case of a switch that jumps into a shared block,
        a jump table capstone cannot reach. So an original instruction is treated as relocated when it holds an
        image address and ours has a relocated instruction of the same masked shape, and a run of two or more
        image-address dwords (a jump table) is masked when ours has a run of relocated dwords. With a real .reloc
        table the plain mask is returned. docs/c2/tracer.md (checker trap)."""
        m = self.reloc_mask(va, n)
        if self.relocs or not rmask:
            return m
        import capstone
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        theirs = self.read(va, n)

        def sweep(code):                      # linear sweep that steps over undecodable bytes
            pos = 0
            while pos < len(code):
                i = next(md.disasm(bytes(code[pos:pos + 16]), pos, 1), None)
                k = i.size if i is not None else 1
                yield pos, k
                pos += k

        shapes = set()
        for pos, k in sweep(mine):
            if any(pos + j in rmask for j in range(k)):
                shapes.add(bytes(0 if pos + j in rmask else mine[pos + j] for j in range(k)))
        if not shapes:
            return m
        img = lambda o: o + 4 <= n and self.in_image(int.from_bytes(theirs[o:o + 4], "little"))
        for pos, k in sweep(theirs):
            win, j = set(), 0
            while j + 4 <= k:
                if img(pos + j):
                    win.update(range(pos + j, pos + j + 4))
                    j += 4
                else:
                    j += 1
            if win - m and bytes(0 if pos + j in win or pos + j in m else theirs[pos + j] for j in range(k)) in shapes:
                m |= win
        if any(all(x + d in rmask for d in range(8)) for x in rmask):          # ours has a table of relocated dwords
            o = (-(va + 0)) % 4
            while o + 8 <= n:
                if img(o) and img(o + 4):
                    while o + 4 <= n and img(o):
                        m.update(range(o, o + 4))
                        o += 4
                else:
                    o += 4
        return m
