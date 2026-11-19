"""COFF i386 object reader: sections with their relocations, function symbols (used by chk.py and tsweep.bdiff)."""
import struct

REL_SIZES = {0x06: 4, 0x07: 4, 0x14: 4, 0x0A: 2, 0x0B: 4}


def parse_coff(path):
    """sections [{name, data, relocs[(off, symidx, type)], comdat}], symbols {name: (secidx0, value)}."""
    b = open(path, "rb").read()
    mach, nsec, _ts, symoff, nsym, opt, _ch = struct.unpack_from("<HHIIIHH", b, 0)
    strtab = symoff + nsym * 18

    def name8(raw):
        if raw[:4] == b"\0\0\0\0":
            o = strtab + struct.unpack_from("<I", raw, 4)[0]
            return b[o:b.index(b"\0", o)].decode("latin-1")
        return raw.rstrip(b"\0").decode("latin-1")
    secs = []
    for i in range(nsec):
        h = 20 + opt + i * 40
        nm = b[h:h + 8]
        _vs, _va, size, rawp, relp, _lp, nrel, _nl, ch = struct.unpack_from("<IIIIIIHHI", b, h + 8)
        if nm.startswith(b"/"):
            o = strtab + int(nm[1:].rstrip(b"\0"))
            nm_s = b[o:b.index(b"\0", o)].decode("latin-1")
        else:
            nm_s = nm.rstrip(b"\0").decode("latin-1")
        data = b[rawp:rawp + size] if rawp else b""
        rel = [struct.unpack_from("<IIH", b, relp + k * 10) for k in range(nrel)]
        secs.append(dict(name=nm_s, data=data, relocs=rel, code=bool(ch & 0x20)))
    syms, idx2name, i = {}, {}, 0
    while i < nsym:
        o = symoff + i * 18
        raw = b[o:o + 8]
        val, sec, typ, cls, naux = struct.unpack_from("<IhHBB", b, o + 8)
        nm = name8(raw)
        idx2name[i] = nm
        if sec > 0 and cls in (2, 3) and (typ & 0x20) and secs[sec - 1]["code"]:   # function symbols
            syms.setdefault(nm, (sec - 1, val))
        i += 1 + naux
    return secs, syms, idx2name
