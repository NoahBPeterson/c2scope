#!/usr/bin/env python3
"""x360img: Xbox 360 game files -> the PE images our tools read.  Doc: docs/c2/port-x360.md

usage: x360img.py stfs PKG [--out DIR]               list (or extract) an STFS package (LIVE / PIRS / CON: XBLA,
                                                    arcade and marketplace content, the console's file names)
       x360img.py iso ISO [--get NAME OUT]...       list (or extract files of) a disc image (XDVDFS, XGD1/2/3)
       x360img.py pe XEX OUT.pe                     the PE image inside a decrypted XEX2 (basic or no compression;
                                                    LZX is refused; an encrypted XEX must be decrypted first, with
                                                    your own tools: this one carries no keys)
       x360img.py stage XDK OUT                     a compiler stage for `c2build.py setup --c2 xbox360 --from OUT`:
                                                    OUT/bin, include, lib -> XDK/bin/win32, include/xbox, lib/xbox
                                                    (XDK = the XDK directory of an extracted XDK installer)
       x360img.py info FILE                         a XEX's XDK library versions and file format, or a PE's Rich
                                                    header (the compiler build of every object the linker saw:
                                                    build 8153 = the XDK 9328 cl, tools/c2builds/15.00.8153-xbox360)

A game is a byte-matching target for a compiler only if its Rich header names that compiler's build: XBLIG
(XNA) packages hold .NET assemblies, no native code. Nothing here writes outside the given output paths.
"""
import os, struct, sys

RICH_TOOLS = {0x6d: "C 14.00", 0x6e: "C++ 14.00", 0x83: "C 15.00", 0x84: "C++ 15.00", 0x91: "linker 9.00", 0x92: "export 9.00", 0x93: "implib 9.00", 0x94: "cvtres 9.00",
              0x95: "masm 9.00", 0xaa: "C 16.00", 0xab: "C++ 16.00", 0x78: "linker 8.00", 0x7d: "masm 8.00",
              0x7b: "implib 8.00", 0x01: "imports"}


# ---------------------------------------------------------------- STFS
def stfs_entries(d):
    assert d[:4] in (b"LIVE", b"PIRS", b"CON "), "not an STFS package (%r)" % d[:4]
    hdr_size = struct.unpack_from(">I", d, 0x340)[0]
    vd = 0x379
    flags = d[vd + 2]
    ft_blocks = struct.unpack_from("<H", d, vd + 3)[0]
    ft_start = int.from_bytes(d[vd + 5:vd + 8], "little")
    first = (hdr_size + 0xFFF) & ~0xFFF
    shift = 0 if flags & 1 else 1           # read-write packages keep two copies of each hash table

    def block_off(n):
        r = (((n + 0xAA) // 0xAA) << shift) + n
        if n >= 0xAA:
            r += ((n + 0x70E4) // 0x70E4) << shift
            if n >= 0x70E4:
                r += 1 << shift
        return (r << 12) + first
    ents = []
    for i in range(ft_blocks):
        base = block_off(ft_start + i)
        for k in range(0x1000 // 0x40):
            e = d[base + k * 0x40: base + (k + 1) * 0x40]
            name = e[:0x28].split(b"\0")[0]
            if not name:
                continue
            nl = e[0x28]
            ents.append(dict(name=name.decode(errors="replace"), dir=bool(nl & 0x80), contig=bool(nl & 0x40),
                             blocks=int.from_bytes(e[0x29:0x2C], "little"), start=int.from_bytes(e[0x2F:0x32], "little"),
                             path=struct.unpack_from(">h", e, 0x32)[0], size=struct.unpack_from(">I", e, 0x34)[0]))

    def full(i):
        e = ents[i]
        return e["name"] if e["path"] < 0 else full(e["path"]) + "/" + e["name"]
    for i, e in enumerate(ents):
        e["full"] = full(i)
    return ents, block_off


def stfs_read(d, e, block_off):
    if not e["contig"]:
        raise RuntimeError("%s: non-contiguous file (the hash-chain walk is not implemented)" % e["full"])
    buf = bytearray()
    for b in range(e["start"], e["start"] + e["blocks"]):
        o = block_off(b)
        buf += d[o:o + 0x1000]
    return bytes(buf[:e["size"]])


# ---------------------------------------------------------------- XDVDFS
def iso_files(path):
    f = open(path, "rb")
    for part in (0, 0xFD90000, 0x2080000, 0x18300000):     # plain, XGD2 game partition, XGD3, XGD1
        f.seek(part + 32 * 2048)
        if f.read(20) == b"MICROSOFT*XBOX*MEDIA":
            break
    else:
        raise RuntimeError("no XDVDFS volume descriptor in %s" % path)
    root, rsize = struct.unpack("<II", f.read(8))

    def read(sector, size):
        f.seek(part + sector * 2048)
        return f.read(size)
    out = []

    def walk(sector, size, prefix):
        d = read(sector, size)
        seen = set()

        def node(o):                        # a directory is a binary tree of entries (offsets in dwords)
            if o * 4 + 14 > len(d) or o in seen:
                return
            seen.add(o)
            left, right, start, sz, attr, nl = struct.unpack_from("<HHIIBB", d, o * 4)
            if left == 0xFFFF:
                return
            name = d[o * 4 + 14: o * 4 + 14 + nl].decode("latin-1")
            if left:
                node(left)
            if attr & 0x10:
                out.append((prefix + name + "/", start, 0))
                if sz:
                    walk(start, sz, prefix + name + "/")
            else:
                out.append((prefix + name, start, sz))
            if right:
                node(right)
        node(0)
    walk(root, rsize, "")
    return out, read


# ---------------------------------------------------------------- XEX2
def xex_headers(d):
    assert d[:4] == b"XEX2", "not a XEX2"
    _mf, pe_off, _r, sec, n = struct.unpack_from(">IIIII", d, 4)
    hdr = dict(struct.unpack_from(">II", d, 0x18 + 8 * i) for i in range(n))
    return pe_off, sec, hdr


def xex_pe(d):
    pe_off, sec, hdr = xex_headers(d)
    ff = hdr[0x000003FF]
    ffsz, enc, comp = struct.unpack_from(">IHH", d, ff)
    image_size = struct.unpack_from(">I", d, sec + 4)[0]
    data = d[pe_off:]
    if enc != 0:
        raise RuntimeError("the XEX is encrypted: decrypt it first (x360img carries no keys)")
    if comp == 1:                           # basic: (data size, zero size) pairs
        out, off = bytearray(), 0
        for o in range(ff + 8, ff + ffsz, 8):
            ds, zs = struct.unpack_from(">II", d, o)
            out += data[off:off + ds] + bytes(zs)
            off += ds
        data = bytes(out)
    elif comp != 0:
        raise RuntimeError("XEX compression %d (LZX) is not handled" % comp)
    if data[:2] != b"MZ":
        raise RuntimeError("the image is not a PE")
    return data[:image_size]


def xex_libs(d):
    _p, _s, hdr = xex_headers(d)
    v = hdr.get(0x000200FF)
    if v is None:
        return []
    sz = struct.unpack_from(">I", d, v)[0]
    return [(d[o:o + 8].split(b"\0")[0].decode(), "%d.%d.%d" % struct.unpack_from(">HHH", d, o + 8))
            for o in range(v + 4, v + sz, 16)]


def rich(pe):
    e = pe.find(b"Rich")
    if e < 0:
        return []
    key = struct.unpack_from("<I", pe, e + 4)[0]
    s = pe.find(struct.pack("<I", 0x536e6144 ^ key))
    out = []
    for o in range(s + 16, e, 8):
        cid, cnt = struct.unpack_from("<II", pe, o)
        cid ^= key
        if cid:
            out.append((cid >> 16, cid & 0xffff, cnt ^ key))
    return out


def main(argv):
    if not argv or argv[0] not in ("stfs", "iso", "pe", "info", "stage"):
        sys.exit(__doc__)
    cmd, a = argv[0], argv[1:]
    if cmd == "stfs":
        d = open(a[0], "rb").read()
        ents, bo = stfs_entries(d)
        out = a[a.index("--out") + 1] if "--out" in a else None
        for e in ents:
            print("%-4s %10d  %s" % ("DIR" if e["dir"] else "", e["size"], e["full"]))
            if out:
                p = os.path.join(out, e["full"])
                if e["dir"]:
                    os.makedirs(p, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    open(p, "wb").write(stfs_read(d, e, bo))
    elif cmd == "iso":
        files, read = iso_files(a[0])
        gets = [(a[i + 1], a[i + 2]) for i in range(1, len(a)) if a[i] == "--get"]
        if not gets:
            for p, _s, z in files:
                print("%12d  %s" % (z, p))
        for name, out in gets:
            m = [x for x in files if x[0].lower() == name.lower()]
            if not m:
                sys.exit("no %s in %s" % (name, a[0]))
            open(out, "wb").write(read(m[0][1], m[0][2]))
            print("%s -> %s (%d bytes)" % (m[0][0], out, m[0][2]))
    elif cmd == "stage":
        xdk, out = os.path.abspath(a[0]), a[1]
        os.makedirs(out, exist_ok=True)
        for name, sub in (("bin", "bin/win32"), ("include", "include/xbox"), ("lib", "lib/xbox")):
            src = os.path.join(xdk, sub)
            if not os.path.isdir(src):
                sys.exit("x360img: no %s (pass the XDK directory: the one holding bin/win32)" % src)
            dst = os.path.join(out, name)
            if os.path.lexists(dst):
                os.remove(dst)
            os.symlink(src, dst)
        print("stage %s; next: uv run tools/c2build.py setup --c2 xbox360 --from %s" % (out, out))
    elif cmd == "pe":
        pe = xex_pe(open(a[0], "rb").read())
        open(a[1], "wb").write(pe)
        print("%s: PE image, %d bytes" % (a[1], len(pe)))
    else:
        d = open(a[0], "rb").read()
        if d[:4] == b"XEX2":
            print("XDK library versions:", ", ".join(sorted({v for _n, v in xex_libs(d)})))
            d = xex_pe(d)
        for prod, build, cnt in rich(d):
            print("  %-12s (prodid 0x%02x) build %5d: %d objects" % (RICH_TOOLS.get(prod, "?"), prod, build, cnt))


if __name__ == "__main__":
    main(sys.argv[1:])
