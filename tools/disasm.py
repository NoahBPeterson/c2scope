#!/usr/bin/env python3
"""Disassemble an original function.  usage: disasm.py <bin> <va-hex | mangled-symbol> [len]

Calls and absolute operands are annotated with the index's symbol name when one starts there."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capstone
from image import Image

img = Image(sys.argv[1])
key = sys.argv[2]
try:
    va = int(key, 16)
except ValueError:
    r = img.func(key)
    if not r:
        sys.exit("no symbol %s in %s" % (key, img.name))
    va = r["va"]
row = img.func(va)
n = int(sys.argv[3], 0) if len(sys.argv) > 3 else (row["size"] if row else 256)
if row:
    print("; %s  %08x  size %d  %s" % (row["symbol"], va, row["size"], os.path.basename((row.get("obj") or "").replace("\\", "/"))))
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
md.detail = True
for i in md.disasm(img.read(va, n), va):
    note = ""
    for op in i.operands:
        v = op.imm if op.type == capstone.x86.X86_OP_IMM else (op.mem.disp if op.type == capstone.x86.X86_OP_MEM else None)
        if v is not None and img.name_at(v & 0xffffffff):
            note = "  ; " + img.name_at(v & 0xffffffff)
    print("%08x  %-22s %s %s%s" % (i.address, i.bytes.hex(), i.mnemonic, i.op_str, note))
