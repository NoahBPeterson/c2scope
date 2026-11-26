"""Disassemble the first differing bytes of one scored function, my obj vs retail.

usage: python3 tools/w8_diff.py <va>
Compiles the slice once (ccache), locates the function symbol, and prints both disassemblies side
by side; bytes inside the relocation mask print as `..` because addresses may differ freely.
"""
import json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capstone
import ccache, msvc
from w8_score import Orig, parse_coff, undecorate, DEF_FLAGS
from w8_emit import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


SIZE_HINT = {}      # va -> the retail size: keep the catch blocks when the corpus row counts them too


def ours_bytes(obj, va):
    """Our function's bytes in the object, and the start offsets of its relocations (for masking)."""
    secs, syms, _ = parse_coff(obj, sizes=True)
    name = "FUN_%08x" % va
    for nm in syms:
        if undecorate(nm) == name:
            si, off, msz = syms[nm]
            sec = secs[si]
            if msz and "__ehhandler$" + nm in syms:
                # a try's catch blocks follow the body inside the function's debug size, from a `$L<n>` label;
                # retail sizes (the corpora) end at the body
                cut = [o for n2, (s2, o, _z) in syms.items() if s2 == si and re.match(r"^\$L\d+$", n2) and
                       off < o < off + msz]
                if cut:
                    body, hint = min(cut) - off, SIZE_HINT.get(va)
                    # some rows' sizes include the catch blocks: then compare that much
                    msz = min(msz, hint) if hint and hint > body else body
            if msz:
                mine = sec["data"][off:off + msz]
            else:
                hint = SIZE_HINT.get(va)
                starts = sorted(o for n2, (s_, o, sz2) in syms.items() if s_ == si and o > off and
                                not (hint and n2.startswith("__catch$" + nm) and o < off + hint))
                mine = sec["data"][off:starts[0] if starts else len(sec["data"])]
                mine = mine.rstrip(b"\xcc")       # padding, or the int3 cl puts after a noreturn call (a row's
                #                                    size ends at the call: M&B 0x40f776, `throw;`)
                if hint and len(mine) > hint and any(n2.startswith("__catch$" + nm) for n2 in syms):
                    mine = mine[:hint]             # the row counts the catch blocks (15.00: `__catch$` symbols)
            rel = sorted({v - off for v, _s, _t in sec["relocs"] if off <= v < off + len(mine)})
            return mine, rel
    raise RuntimeError("no symbol FUN_%08x in %s" % (va, obj))


_MD = None


def dis(data, va):
    """Disassemble a function's bytes at its address (32-bit x86, ret/imm boundaries natural)."""
    global _MD
    if _MD is None:
        _MD = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        _MD.detail = True             # canon_ins reads operand kinds
    return list(_MD.disasm(data, va))


def canon_ins(ins):
    """A register-erased signature: mnemonic + operand kind (and memory/imm width). Alignment then
    measures shape, not which register the allocator happened to pick."""
    out = [ins.mnemonic]
    for op in ins.operands:
        if op.type == capstone.x86.X86_OP_REG:
            out.append("r")
        elif op.type == capstone.x86.X86_OP_IMM:
            out.append("i%d" % op.size)
        elif op.type == capstone.x86.X86_OP_MEM:
            out.append("m%d" % op.size)
        else:
            out.append("?")
    return " ".join(out)


def fp_of(va_s):
    for line in open(os.path.join(ROOT, "..", "spore-decomp", "tools", "ci", "fingerprints.jsonl")):
        j = json.loads(line)
        if j["va"] == va_s:
            return j
    return None


def main():
    VA = sys.argv[1]
    va = int(VA, 16)
    fp = fp_of(VA)
    size = fp["size"]
    mask = {s + k for s, n in fp["mask"] for k in range(n)}
    src = os.path.join(ROOT, "work/scratch/w8_emit", "%s.cpp" % VA)
    obj = os.path.join(ROOT, "work/scratch/w8_diff", "%s.obj" % VA)
    os.makedirs(os.path.dirname(obj), exist_ok=True)
    if not os.path.exists(obj):
        rc, out = msvc.run("cl", ["/nologo", "/c", *DEF_FLAGS.split(), "/Fo" + obj, os.path.abspath(src)], capture=True)
        assert rc == 0, out
    secs, syms, _ = parse_coff(obj, sizes=True)
    name = "FUN_%s" % VA
    for nm in syms:
        if undecorate(nm) == name:
            si, off, msz = syms[nm]
            break
    else:
        print("symbol not found"); return
    sec = secs[si]
    if msz:
        mine = sec["data"][off:off + msz]
    else:
        starts = sorted(o for s_, o, sz2 in syms.values() if s_ == si and o > off)
        mine = sec["data"][off:starts[0] if starts else len(sec["data"])]
        if starts:
            mine = mine.rstrip(b"\xcc")
    orig = Orig()
    theirs, rmask = orig.bytes_and_mask(va, len(mine))
    cmp_mask = mask | rmask
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    a = 0
    print("== %s retail %d bytes, mine %d, diff-bits from jsonl" % (VA, size, len(mine)))
    while a < max(len(mine), len(theirs or b"")):
        try:
            i_m = next(md.disasm(mine[a:a + 16], a))
        except StopIteration:
            i_m = None
        try:
            i_r = next(md.disasm(theirs[a:a + 16], a)) if theirs and a < len(theirs) else None
        except StopIteration:
            i_r = None
        mb = mine[a:a + (i_m.size if i_m else 0)]
        rb = theirs[a:a + (i_r.size if i_r else 0)] if theirs else b""
        masked_b = any((a + k) in cmp_mask for k in range(len(mb))) or any((a + k) in cmp_mask for k in range(len(rb)))
        l = "%-6s %-30s" % ("+%04x" % a, ("%s %s" % (i_m.mnemonic, i_m.op_str)) if i_m else "(end)")
        r = ("%-30s" % ("%s %s" % (i_r.mnemonic, i_r.op_str))) if i_r else "(end)"
        tag = ""
        if mb != rb:
            if masked_b:
                tag = "[masked]"
            else:
                tag = "<<<"
        print("  %s | %s  %s" % (l, r, tag))
        if mb != rb and not masked_b:
            print("         mine %s" % mb.hex(" "))
            print("         retl %s" % rb.hex(" "))
        step = max(i_m.size if i_m else 1, i_r.size if i_r else 1, 1)
        a += step


if __name__ == "__main__":
    main()
