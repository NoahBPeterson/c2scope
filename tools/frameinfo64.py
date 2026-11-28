#!/usr/bin/env python3
"""frameinfo64: what each address-taken stack slot of an x64 function is, read from the binary alone.

    python3 tools/frameinfo64.py VA [VA ...]

For every call that gets a stack address in rcx / rdx / r8 / r9 (`lea reg, [rsp+N]` shortly before it), the callee
is read up to its returns:
  return slot   the callee returns that same pointer (`mov rax, <that register>` before a ret): the hidden return
                pointer of a function returning a class by value; the slot is `T x = f(...)`'s temporary. cl's frame
                packer places those after named locals, so lifting it as a named local can swap its slot with one
                of them (seen as a named local's slot traded with a callee-saved register's home).
  out param     the callee writes through it and returns something else: a named local whose address is passed.
Slots are reported as [rsp+N] after the prologue and as home slot h when they are in the caller's home area.
The executable is --binary / LIFT64_BINARY and sizes come from LIFT64_INDEX or .pdata, as for lift2il64."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ehinfo64 as E    # noqa: E402
import lift2il64 as L   # noqa: E402

ARGS = ("rcx", "rdx", "r8", "r9")
_RET = {}


def returns_arg(img, va, depth=0):
    """The argument registers the function at va returns in rax (`mov rax, r` right before a ret / a tail jmp)."""
    if va in _RET:
        return _RET[va]
    _RET[va] = set()
    pd = E.pdata(img)
    end = pd.get(va, (va + 0x200,))[0]
    body = img.insns(va, max(16, min(end - va, 0x800)))
    if body and body[0].mnemonic == "jmp" and body[0].operands[0].type == 2 and depth < 3:
        _RET[va] = returns_arg(img, body[0].operands[0].imm, depth + 1)
        return _RET[va]
    out = set()
    for n, b in enumerate(body[:-1]):
        nx = body[n + 1]
        if nx.mnemonic in ("ret", "repz ret") or (nx.mnemonic == "pop" and n + 2 < len(body) and
                                                  body[n + 2].mnemonic in ("ret", "repz ret")) or \
                (nx.mnemonic == "add" and nx.op_str.startswith("rsp")):
            m = re.match(r"^rax, (rcx|rdx|r8|r9)$", b.op_str) if b.mnemonic == "mov" else None
            if m:
                out.add(m.group(1))
    # `mov rax, rdx` early and rax kept to the end: a saved copy (mov rbx, rdx ... mov rax, rbx)
    saved = {}
    for b in body:
        m = re.match(r"^(r\w+), (rcx|rdx|r8|r9)$", b.op_str) if b.mnemonic == "mov" else None
        if m and m.group(1) not in ARGS and m.group(1) != "rax":
            saved.setdefault(m.group(1), m.group(2))
    for n, b in enumerate(body[:-1]):
        m = re.match(r"^rax, (r\w+)$", b.op_str) if b.mnemonic == "mov" else None
        if m and m.group(1) in saved and any(x.mnemonic in ("ret", "repz ret") for x in body[n + 1:n + 6]):
            out.add(saved[m.group(1)])
    _RET[va] = out
    return out


def frame_total(img, va):
    info = E.funcinfo_of(img, va)
    pd = E.pdata(img)
    if va not in pd:
        return None
    u = E.unwind(img, pd[va][1])
    return E.frame_of(u[2], u[3], u[4])["total"]


def slots(img, va, size):
    """[(call offset, callee, arg register, rsp offset, kind)]."""
    ins = img.insns(va, size)
    out = []
    for k, i in enumerate(ins):
        if i.mnemonic != "call" or i.operands[0].type != 2:
            continue
        t = i.operands[0].imm
        seen = set()
        for j in range(k - 1, max(-1, k - 8), -1):
            m = re.match(r"^(rcx|rdx|r8|r9), \[rsp \+ (0x[0-9a-f]+)\]$", ins[j].op_str) \
                if ins[j].mnemonic == "lea" else None
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                kind = "return slot" if m.group(1) in returns_arg(img, t) else "out param"
                out.append((i.address - va, t, m.group(1), int(m.group(2), 16), kind))
            if ins[j].mnemonic == "call":
                break
    return out


def report(img, va, size):
    tot = frame_total(img, va)
    lines = []
    for off, t, reg, so, kind in slots(img, va, size):
        where = "[rsp+0x%x]" % so
        if tot is not None and so >= tot + 8:
            h = (so - tot - 8) // 8
            if h < 4:
                where += " home %d" % h
        lines.append("  call +0x%x %s(%s=&%s)  %s" % (off, "FUN_%x" % t, reg, where, kind))
    return lines


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("vas", nargs="+")
    ap.add_argument("--binary")
    ap.add_argument("--index")
    a = ap.parse_args()
    L.BINARY, L.INDEX = a.binary or L.BINARY, a.index or L.INDEX
    img = L.Image()
    idx = L.index(img)
    for x in a.vas:
        va = int(x, 16)
        print("0x%x" % va)
        print("\n".join(report(img, va, idx[va]["size"])) or "  no stack addresses passed to calls")
