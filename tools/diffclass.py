#!/usr/bin/env python3
"""Why two compiles of one function differ, read from the two instruction streams: the convergence driver's dispatch.

classify(theirs, mine, va, mask) -> (cls, detail)
  match       identical outside the mask
  regalloc    the same instructions in the same order; only register choices differ
  frame       the same instructions in the same order; only stack offsets (esp/ebp displacements, the frame size)
              differ, maybe with register choices too
  schedule    the same instructions (register-erased) in another order
  copies      the only extra or missing instructions are moves, pushes, pops, lea, xchg (spills, copies, saved
              registers): the allocator's footprint, usually a regalloc or frame cause, not other arithmetic
  structure   other instructions: more, fewer or different operations (the source computes it another way)
  lost        ours is under half the original's size (a value, a store or a branch went missing)
The categories are spore-decomp's nonmatching.txt causes (AGENTS.md step 7), so a class picks the tool family:
regalloc -> sulever / rwsweep / symkeys, frame -> framefields / odslots, schedule -> schedwant, structure -> icount /
lift.py (and the emitter's rules, for lifted code).
"""
import collections, difflib

import capstone
from capstone import x86

_MD = None


def dis(data, va):
    global _MD
    if _MD is None:
        _MD = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        _MD.detail = True
    return list(_MD.disasm(bytes(data), va))


STACK = {x86.X86_REG_ESP, x86.X86_REG_EBP}


def canon(ins):
    out = [ins.mnemonic]
    for op in ins.operands:
        if op.type == x86.X86_OP_REG:
            out.append("r")
        elif op.type == x86.X86_OP_IMM:
            out.append("i%d" % op.size)
        elif op.type == x86.X86_OP_MEM:
            out.append("m%d" % op.size)
    return " ".join(out)


def pair_kinds(a, b, va_a, va_b, mask_a):
    """What differs between two instructions with the same canonical form: a set of reg / stack / imm / other."""
    if a.bytes == b.bytes:
        return set()
    k = set()
    off = a.address - va_a
    if all(off + i in mask_a for i in range(len(a.bytes)) if i < len(b.bytes) and a.bytes[i] != b.bytes[i]):
        return set()                                   # only masked (address) bytes differ
    for oa, ob in zip(a.operands, b.operands):
        if oa.type == x86.X86_OP_REG:
            if oa.reg != ob.reg:
                k.add("reg")
        elif oa.type == x86.X86_OP_IMM:
            if oa.imm != ob.imm:
                # sub/add esp, N is the frame size; push imm and the rest are values
                k.add("stack" if a.mnemonic in ("sub", "add") and a.operands[0].type == x86.X86_OP_REG and
                      a.operands[0].reg == x86.X86_REG_ESP else "imm")
        elif oa.type == x86.X86_OP_MEM:
            ma, mb = oa.mem, ob.mem
            if (ma.base, ma.index) != (mb.base, mb.index) or ma.scale != mb.scale:
                k.add("reg")
            if ma.disp != mb.disp:
                k.add("stack" if ma.base in STACK or mb.base in STACK else "imm")
    if not k:
        k.add("other")                                 # the same operands, other encoding (a jump length, a prefix)
    return k


def classify(theirs, mine, va, mask=()):
    mask = set(mask)
    n = min(len(theirs), len(mine))
    if len(theirs) == len(mine) and all(i in mask or theirs[i] == mine[i] for i in range(n)):
        return "match", {}
    if len(mine) * 2 < len(theirs):
        return "lost", {}
    A, B = dis(theirs, va), dis(mine, va)
    ca, cb = [canon(i) for i in A], [canon(i) for i in B]
    if ca == cb:
        kinds = collections.Counter()
        for a, b in zip(A, B):
            kinds.update(pair_kinds(a, b, va, va, mask))
        if kinds.get("imm") or kinds.get("other"):
            # same shape with other values: a jump encoding follows another difference; a constant is source
            if set(kinds) <= {"other", "reg", "stack"} and kinds["other"]:
                cls = "frame" if kinds.get("stack") else "regalloc" if kinds.get("reg") else "structure"
            else:
                cls = "structure"
        elif kinds.get("stack"):
            cls = "frame"
        elif kinds.get("reg"):
            cls = "regalloc"
        else:
            cls = "match"
        return cls, dict(kinds)
    if sorted(ca) == sorted(cb):
        return "schedule", {}
    # the multiset difference: what one side has that the other lacks (register-erased)
    da, db = collections.Counter(ca), collections.Counter(cb)
    missing, extra = da - db, db - da
    ops = {k.split()[0] for k in list(missing) + list(extra)}
    detail = dict(missing=sum(missing.values()), extra=sum(extra.values()), ops=sorted(ops)[:8])
    if ops <= COPY_OPS:
        return "copies", detail
    return "structure", detail


COPY_OPS = {"mov", "push", "pop", "lea", "xchg", "movzx", "movsx", "movss", "movsd", "movaps", "fld", "fstp", "fxch"}
