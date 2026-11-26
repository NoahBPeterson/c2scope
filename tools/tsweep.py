#!/usr/bin/env python3
"""Sweep a source template's alternatives and rank the variants by a number read from a c2 trace.

usage: tsweep.py <template.cpp> --func SUBSTR [--metric EXPR] [--show EXPR]... [--jobs 8] [--max 256] [--top 10]
                 [--asm-vs ORIG.s [--asm-func DECORATED]] [-- <cl args>]

The template uses sweep.py's syntax: alternatives written `{{first<|>second<|>third}}`. Every combination is
compiled with the traced c2 (tools/c2trace.py) and scored by EXPR, a Python expression; lower is better (default
`(rdiff, bdiff)`, which needs --asm-vs; --asm-func defaults to --func; a tuple compares element by element). Use it
when the byte diff is a poor guide, i.e. when the fix is "make X outrank Y" or "get the cursor to eax at pick 7"
and every intermediate step looks like no progress in bytes. Names available in EXPR (and --show):

  prio(var)          priority of var's first pop in Color (the `regs` table)
  prio(var, k)       var's priority in Spillcosts pass k (largest piece if it was split; None if absent)
  reg(var, n=1)      register var's n-th popped piece got ('spill/defer' if none)
  pops(var)          number of popped pieces of var
  cur(n), sreg(n)    scratch pick n: the cursor before it, the register it got (the `scratch` table)
  rot(n)             1 if pick n moved the cursor
  off(n)             listing offset of pick n (None if 'gone')
  gone()             number of picks whose instruction is gone from the final code
  sat(offset)        register of the scratch pick at a listing offset (None if no scratch pick there, e.g. once
                     the value became a Color variable: use dst(offset) to score the register in the code)
  ins(offset)        our instruction at a listing offset ('mov eax, DWORD PTR [esi+672]'), dst(offset) its first
                     register (both need --asm-func)
  bdiff              the byte-faithful score against --asm-vs: whole instructions (mnemonic, registers,
                     displacements, immediates, operand order and size) left unmatched by an alignment, both sides
                     counted (a substitution 2, an insertion 1; only a displacement/immediate or a jump's target
                     wrong: 1); relocated addresses, call targets and alignment padding are masked
                     (docs/c2/tracer.md, `bdiff`)
  rdiff              bdiff up to register choice, frame-slot choice and scheduling moves: a register or local
                     slot is bound per value and only a read that disagrees with the binding counts; a moved
                     instruction 1; incoming arguments, displacements, immediates, the order of a callee-saved
                     save and base/index order still count. 0 = the original's code up to allocation and order
                     (a renaming ripple after one changed instruction no longer makes every later line count)
  regdiff            the same alignment over mnemonic + registers only (blind to displacements and immediates);
                     posdiff: the positional count over the common length
  hinge(x, t)        max(0, x - t): 0 once x <= t (e.g. hinge(prio('this',2), 79))

A variant whose metric raises (e.g. a name not found) scores as +inf and its error is shown.
Example (Fallout 3 0x008e7d40):
  tsweep.py t.cpp --func FUN_00c7e9a0@SceneOwner \\
      --metric "hinge(prio('this',2), 79) + 10*(sat(0xa8)!='eax')" --show "prio('scene',2)" -- /TP /MT /GS /EHsc /Gy
"""
import argparse, hashlib, difflib, itertools, math, os, re, shutil, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from image import ROOT
import c2trace, c2regs, c2scratch


def out_name(path, func=None):
    """The per-source variant directory name of tsweep, rwsweep, helpersweep and realize: `<stem>-<hash6>`, the hash
    of the source's directory, so that two agents' v3.cpp in different directories do not share (and rmtree) one
    directory. A stem with capitals also enters the hash: on a case-insensitive file system (macOS) `r2_D.cpp` and
    `r2_d.cpp` of one directory would otherwise be one directory. All-lowercase stems keep their old names. Another
    compiler build than 14.00 enters the hash too, so runs of one source on two builds do not share a directory.
    `func` (rwsweep, helpersweep, realize: one function per run) enters the hash as well: two functions of one unit
    swept at the same time overwrote each other's base/ and orig/ ("function not register-allocated", 2026-11-20)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    key = os.path.dirname(os.path.abspath(path))
    if stem != stem.lower():
        key += "\0" + stem
    if c2trace.c2build.table():
        key += "\0" + c2trace.c2build.name()
    if func:
        key += "\0f" + func
    return stem + "-" + hashlib.md5(key.encode()).hexdigest()[:6]


ALT = re.compile(r"\{\{(.*?\}*)\}\}(?!\})", re.S)   # a code `}` just before the closing `}}` stays in the alternative


def cl_errors(text, n=300):
    """The compiler's error lines from a failed compile's output, warnings dropped (a C4288 flood buries the C2143);
    the plain tail when no line says error."""
    errs = [l.strip() for l in text.splitlines() if re.search(r"\b(fatal )?error\b", l) and not re.search(r"\bwarning C\d+", l)]
    return "; ".join(errs)[:n] if errs else text[-n:]

# x64 (a build table with "arch": "x64", cl 17.00 x86_amd64): the 64-bit registers, r8..r15 with their parts, the
# low bytes sil/dil/bpl/spl; capstone decodes in 64-bit mode; frame slots are rsp-relative (docs/c2/port-1700.md)
X64 = c2trace.c2build.get("arch") == "x64"
_X64REG = r"r[abcd]x|r[sd]i|r[sb]p|r(?:[89]|1[0-5])[dwb]?|[sd]il|[sb]pl|"
REG = re.compile(r"\b(" + (_X64REG if X64 else "") + r"e?[abcd]x|[abcd][lh]|e?si|e?di|e?bp|e?sp|st\(\d\)|xmm\d+)\b"
                 if X64 else r"\b(e?[abcd]x|[abcd][lh]|e?si|e?di|e?bp|e?sp|st\(\d\))\b")


def listing_offsets(path, func):
    """{offset: 'mnemonic operands'} for our listing of func."""
    text = open(path, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)      # any --func form; `NAME<tab>PROC` (C names, cl 13.10) too
    out, pend = {}, None
    for ln in text[i:j].splitlines():
        m = re.match(r"\s+([0-9a-f]{5})\t[0-9a-f ]+(?:\t+ (\w+)\s*(.*))?$", ln)
        c = re.match(r"\t[0-9a-f ]+\t+ (\w+)\s*(.*)", ln)       # continuation line of a long instruction
        if m:
            if m.group(2):
                out[int(m.group(1), 16)] = (m.group(2) + " " + re.sub(r"\s*;.*", "", m.group(3))).strip()
                pend = None
            else:
                pend = int(m.group(1), 16)
        elif c and pend is not None:
            out[pend] = (c.group(1) + " " + re.sub(r"\s*;.*", "", c.group(2))).strip()
            pend = None
    return out


class Instrs(list):
    """listing_instrs' result: [(mnemonic, operands)] as the listing prints them, plus `decoded`: per instruction the
    capstone text of the listing's code bytes and which fields hold a relocation (bdiff's input; None when the
    listing has no code bytes)."""
    decoded = None
    addrs = None
    va0 = None
    listing = False         # True: our /FAsc listing (MASM operand order), not capstone text of the original


def listing_instrs(path, func):
    text = open(path, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)
    body = text[i:j]
    out, dec, buf, off = Instrs(), [], [], 0
    for ln in body.splitlines():
        m = re.match(r"(?:\s+([0-9a-f]{5}))?\t([0-9a-f]{2}(?: [0-9a-f]{2})*) *(?:\t+ (\w+)\s*(.*))?$", ln)
        if not m:
            continue
        if m.group(1):
            buf, off = [], int(m.group(1), 16)
        buf += m.group(2).split()
        if m.group(3):
            ops = re.sub(r"\s*;.*", "", m.group(4))
            out.append((m.group(3), ops))
            dec.append((off, bytes.fromhex("".join(buf)), m.group(3), ops))
            buf = []
    if out and all(d[1] for d in dec):
        rel = _obj_relocs(path, re.match(r"(\S+)", text[i:]).group(1))
        main = next((k for k in range(1, len(dec)) if dec[k][0] < dec[k - 1][0]), len(dec))   # EH funclets after
        out.decoded = [_decode(*d, rel=rel if k < main else None) for k, d in enumerate(dec)]
        out.addrs = [d[0] for d in dec]
    out.listing = True
    return out


# The listing (MASM) and capstone write some instructions' operands in another order for the same bytes: `test` and
# `xchg` are `test reg, r/m` in the listing and `test r/m, reg` in capstone (85 c2 is the listing's `test eax, edx`,
# capstone's `test edx, eax`). Comparing the listing's text position by position with the original's capstone text
# then pairs eax with edx: false WANTS in regmap (Mount & Blade 0x46bc70: 4 of 5), false scratch phases. Checked on
# 14.00 and 13.10 listings: imul (2 and 3 operand), cmpxchg, xadd, shld/shrd, bt/bsf and the x87 two-stack forms
# (regmap.norm) keep the order; fcomip/fucomip are capstone's fcompi/fucompi (CS_MNEMONIC).
_SWAPPED = ("test", "xchg")
_REGOP = re.compile(r"(?:" + (_X64REG if X64 else "") + r"e?[abcd]x|[abcd][lh]|e?[sd]i|e?[sb]p)", re.I)


def canon_listing(mn, ops):
    """(mnemonic, operands) of one listing instruction in capstone's operand order and mnemonic (text only: for a
    listing without code bytes, or where the operands' symbol names are needed). Prefer cs_text: the bytes."""
    if mn.lower() in _SWAPPED:
        o = [x.strip() for x in _split_ops(ops)]
        if len(o) == 2 and _REGOP.fullmatch(o[0]) and (_REGOP.fullmatch(o[1]) or re.search(r"\[|\bPTR\b", o[1], re.I)):
            return (mn, "%s, %s" % (o[1], o[0]))
    return ({v: k for k, v in CS_MNEMONIC.items()}.get(mn.lower(), mn), ops)


def cs_text(lst):
    """[(mnemonic, operands)] of a listing_instrs result as capstone writes the listing's code bytes (the form of the
    original's orig.s), split like orig_instrs splits a line (`rep movsd ...` is ("rep", "movsd ...")). Alignment
    pads and data (a jump table) keep the listing's text; a listing without code bytes is canonicalised from its text
    (canon_listing). Any other list (capstone text already) is returned as it is."""
    if not getattr(lst, "listing", False):
        return list(lst)
    dec = lst.decoded
    out = []
    for k, x in enumerate(lst):
        d = dec[k] if dec else None
        if d is None or d[0] in ("npad", "db"):
            out.append(tuple(x) if d is not None else canon_listing(*x))
        else:
            t = (d[0] + " " + d[1]).split(None, 1)
            out.append((t[0], t[1].strip() if len(t) > 1 else ""))
    return out


def listing_cs_offsets(path, func):
    """{offset: 'mnemonic operands'} like listing_offsets, but the text is capstone's of the listing's code bytes
    (cs_text): what regmap, scratch --phase and every other position-by-position register comparison with the
    original's orig.s reads. Without code bytes (a /FAs listing), the listing's text in capstone's operand order."""
    lst = listing_instrs(path, func)
    if lst and lst.addrs:
        return {a: (mn + " " + ops).strip() for a, (mn, ops) in zip(lst.addrs, cs_text(lst))}
    out = {}
    for o, t in listing_offsets(path, func).items():
        p = t.split(None, 1)
        mn, ops = canon_listing(p[0], p[1] if len(p) > 1 else "")
        out[o] = (mn + " " + ops).strip()
    return out


def _obj_relocs(asm, name):
    """Function-relative byte offsets covered by a relocation in the object next to the listing (same stem), or
    None without one: a relocated field reads 0 (or its addend) whatever the listing text shows."""
    obj = os.path.splitext(asm)[0] + ".obj"
    if not os.path.exists(obj):
        return None
    try:
        import coff
        secs, syms, _ = coff.parse_coff(obj)
        if name not in syms:
            return None
        si, val = syms[name]
        return {o + k - val for o, _s, typ in secs[si]["relocs"] for k in range(coff.REL_SIZES.get(typ, 4))}
    except Exception:
        return None


_CS = None


def _decode(off, code, mn, ops, rel=None):
    """(capstone mnemonic, capstone operands, imm_sym, mem_sym) of one listing instruction's bytes: the listing's
    text says which fields hold a relocation (`OFFSET sym` an immediate; a symbol that is not a frame variable a
    displacement), since a relocated field is 0 (or the addend) in an object."""
    global _CS
    if mn == "npad":
        return ("npad", "", False, False)
    if _CS is None:
        import capstone
        _CS = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64 if X64 else capstone.CS_MODE_32)
        _CS.detail = True
    ins = list(_CS.disasm(code, off))
    if len(ins) != 1 or ins[0].size != len(code):
        return ("db", code.hex(), False, False)         # data in the procedure (a jump table) or a decode failure
    imm_sym = bool(re.search(r"\bOFFSET\b", ops))
    mem_sym = False
    if mn in ("lock", "rep", "repe", "repne", "repz", "repnz"):
        ops = re.sub(r"^\s*\w+\s*", "", ops)            # the listing's `lock inc DWORD PTR [rax+8]`: operands after
        #                                                 the instruction's own mnemonic
    if rel:                                             # the object's relocations, where there is one
        x = ins[0]
        imm_sym |= bool(x.imm_size and off + x.imm_offset in rel)
        mem_sym |= bool(x.disp_size and off + x.disp_offset in rel)
    for o in _split_ops(ops):
        mem = bool(re.search(r"\bPTR\b|\[|^\s*[cdefgs]s:", o))
        o = re.sub(r"^(?:BYTE|WORD|DWORD|QWORD|TBYTE|FWORD|XMMWORD|MMWORD|OWORD)\s+PTR\s+", "", o.strip())
        o = re.sub(r"^FLAT:", "", re.sub(r"^[cdefgs]s:", "", o))
        m = re.match(r"([\w?@$.]+)\s*(\[[^\]]*\])?", o)
        if not m or "OFFSET" in o or re.fullmatch(r"(?:" + (_X64REG if X64 else "") +
                                                    r"e?[a-d]x|[a-d][lh]|e?[sd]i|e?[sb]p|st|st\(\d\)|xmm\d+|mm\d|"
                                                    r"-?[0-9][0-9a-f]*h?)", m.group(1).lower()):
            continue
        if m.group(2) and re.match(r"\[\s*[er][sb]p\b", m.group(2)):
            continue                                    # a frame variable: _x$[esp+8], the bytes have its offset
        if mem:
            mem_sym = True                              # a global, constant, import or jump table
        else:
            imm_sym = True                              # a bare symbol as an immediate (`push __ehhandler$f`)
    return (ins[0].mnemonic, ins[0].op_str, imm_sym, mem_sym)


def _split_ops(ops):
    out, depth, cur = [], 0, ""
    for ch in ops:
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        depth += ch == "["
        depth -= ch == "]"
        cur += ch
    return out + [cur] if cur.strip() else out


# capstone's names for instructions the listing (MASM) spells otherwise: DF F0+i is `fcomip` in the listing and
# `fcompi` in capstone (the same for fucomip, DF E8+i). For regdiff's keys of a plain list of listing text (regdiff
# itself reads our side through cs_text: capstone of the listing's bytes); bdiff decodes both sides with capstone.
CS_MNEMONIC = {"fcompi": "fcomip", "fucompi": "fucomip"}


_OVERRUN_SAID = set()


def orig_instrs(path):
    """The original's listing (see _orig_instrs), with a warning when it runs past the function's end."""
    out = _orig_instrs(path)
    if path not in _OVERRUN_SAID:
        k = listing_overrun(out)
        if k is not None:
            _OVERRUN_SAID.add(path)
            sys.stderr.write("warning: %s runs past the function's end: a ret at instruction %d is followed by %s "
                             "(%d of %d instructions are another function or padding; bdiff and alignment score "
                             "against them). Regenerate the listing for this function's size.\n"
                             % (path, k, out[k + 1][0] if out[k + 1][0] != "push" else "a new prologue",
                                len(out) - k - 1, len(out)))
    return out


def listing_overrun(ins):
    """Index of a `ret` followed by inter-function padding (int3) or a new `push ebp; mov ebp, esp` prologue: the
    listing ran into the next function (M&B 0x4b9110: a 4000-byte cached listing of a 1464-byte function). Mid-
    function alignment pads are nop/lea/npad and never follow with int3, so they don't trigger it."""
    for k in range(len(ins) - 1):
        if ins[k][0] != "ret":
            continue
        nx = ins[k + 1]
        if nx[0] == "int3" or (nx[0] == "push" and nx[1] == "ebp" and k + 2 < len(ins) and
                               ins[k + 2][0] == "mov" and ins[k + 2][1].replace(" ", "") == "ebp,esp"):
            return k
    return None


def _orig_instrs(path):
    """Lines 'VA  mnemonic  operands' (capstone-style disassembly of the original function)."""
    out, va = [], []
    for ln in open(path):
        p = ln.split(None, 2)
        if len(p) >= 2 and p[0].startswith("+0x"):     # offset form: "+0xOFF  mn  ops", jumps to "+0xOFF"
            p[0] = p[0][1:]
            if len(p) > 2:
                p[2] = re.sub(r"(?<![\w\]])\+(0x[0-9a-f]+)\b", r"\1", p[2]) if p[1].startswith(("j", "loop")) else p[2]
        elif len(p) >= 2 and not re.fullmatch(r"(?:0x)?[0-9a-fA-F]+:?", p[0]):
            continue                                    # a header line ("FUN_<va>  (N bytes, ...)")
        if len(p) >= 2:
            out.append((p[1], p[2].strip() if len(p) > 2 else ""))
            va.append(p[0].lower().rstrip(":"))
    # a long alignment pad is `jmp` over the padding (the listing's npad 5..): `jmp X; lea r, [r]; X:` is one npad,
    # also in several parts (npad 14: `jmp X; lea esp, [esp]; jmp X; lea ecx, [ecx]; X:`): a jmp followed by pad
    # instructions and further jmps, each jmp landing at or before the end X of the run, X the first jmp's target
    def _va(t):
        t = t.lower().strip()
        return int(t, 16) if re.fullmatch(r"(?:0x)?[0-9a-f]+", t) else None
    vn = [_va(v) for v in va]
    for i in range(len(out) - 2):
        if out[i][0] != "jmp" or _key(out[i + 1]) != ("npad", ()):
            continue
        x = _va(out[i][1])
        k = i + 1
        while k < len(out) and vn[k] != x and (_key(out[k]) == ("npad", ()) or (
                out[k][0] == "jmp" and _va(out[k][1]) is not None and vn[k] is not None and x is not None and
                vn[k] < _va(out[k][1]) <= x)):
            k += 1
        if x is not None and k < len(out) and vn[k] == x:
            for q in range(i, k):
                if out[q][0] == "jmp":
                    out[q] = ("npad", "")
    out = Instrs(out)
    out.decoded = [x + (False, False) for x in out]
    if X64:   # a TLS variable: `mov rax, gs:[0x58]` (the TLS array) then `mov r32, OFFSET var` (SECREL in ours, the
        #       resolved offset here): the immediate is a relocation
        for i, (mn, ops) in enumerate(out):
            if mn == "mov" and re.fullmatch(r"\w+, (?:0x[0-9a-f]+|\d+)", ops) and \
                    any("gs:[0x58]" in out[k][1] for k in range(max(0, i - 4), i)):
                out.decoded[i] = (mn, ops, True, False)
    try:
        vas = [int(v, 16) for v in va]
    except ValueError:
        return out
    out.va0 = vas[0] if vas else None
    out.addrs = vas
    # a jump table (or byte index table) after the code decodes as garbage: bdiff stops at the first table the code
    # indexes inside the function's range (our listing's table entries are data, not instructions)
    tabs = []
    for mn, ops in out:
        m = re.search(r"\[\w+\*4 \+ (0x[0-9a-f]+)\]$", ops) if mn == "jmp" else \
            re.search(r"^\w+, byte ptr \[\w+ \+ (0x[0-9a-f]+)\]$", ops) if mn == "movzx" else None
        if m and vas[0] < int(m.group(1), 16) <= vas[-1]:
            tabs.append(int(m.group(1), 16))
    if tabs:
        cut = next((k for k, v in enumerate(vas) if v >= min(tabs)), len(vas))
        out.decoded, out.addrs = out.decoded[:cut], vas[:cut]
    return out


def _key(ins):
    if ins[0] in ("rep", "repe", "repne"):          # capstone spells out the implicit operands of rep movsd/stosd
        return (ins[0], ins[1].split()[0].lower() if ins[1] else "")
    if re.fullmatch(r"(movs|stos|lods|scas|cmps)[bwd]", ins[0]) and "xmm" not in ins[1].lower():
        return (ins[0], ())                         # ... and of a bare movsb/movsw (the listing writes none)
    regs = tuple(REG.findall(re.sub(r"[\w?@$]*[?@$][\w?@$]*", " ", ins[1]).lower()))   # not inside symbol names
    if ins[0] == "imul" and len(regs) == 2 and regs[0] == regs[1] and ins[1].count(",") == 2:
        regs = regs[:1]                             # capstone `imul r, r, imm` is the listing's `imul r, imm`
    if ins[0] == "npad" or (ins[0] == "lea" and len(regs) == 2 and regs[0] == regs[1] and
                            re.fullmatch(r"\s*\w+\s*,\s*(?:dword ptr\s*)?\[\s*\w+\s*(?:\+\s*0x0*0?\s*|\+\s*0\s*)?\]\s*",
                                         ins[1].lower())):
        return ("npad", ())                         # alignment padding: the listing's npad, decoded as lea r, [r]
    if ins[0] == "nop" or ins[0] == "mov" and regs == ("edi", "edi"):
        return ("npad", ())                         # npad 2
    return (CS_MNEMONIC.get(ins[0], ins[0]), regs)


def regdiff(ours, orig):
    """Aligned difference: instructions (mnemonic + registers) not matched by a longest-common-subsequence style
    alignment, counted on both sides (a substitution counts 2, an insertion 1). Our trailing lines past the last
    match are ignored: our listing may carry EH funclets the original's function range lacks."""
    def keys(lst):                                  # one alignment pad may decode as several instructions
        out = []
        for x in lst:
            k = _key(x)
            if not (k == ("npad", ()) and out and out[-1] == k):
                out.append(k)
        return out
    a, b = keys(cs_text(ours)), keys(orig)       # our listing as capstone reads its bytes: operand order (test)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    blocks = [m for m in sm.get_matching_blocks() if m.size]
    if not blocks:
        return len(a) + len(b)
    last = blocks[-1]
    matched = sum(m.size for m in blocks)
    return (last.a + last.size - matched) + (len(b) - matched)


# bdiff: the byte-faithful score. Both sides are capstone text (the original's orig.s; ours decoded from the
# listing's code bytes), so displacements, immediates, operand order and operand sizes compare exactly; masked:
# relocated fields (by the listing's symbols on our side, by image-address range on both), call and jump targets,
# alignment padding.
ADDR_LO, ADDR_HI = 0x00400000, 0x04000000       # image addresses for a function below 0x04000000; else see _addr_range


def _addr_range(va0):
    if va0 is None or va0 < ADDR_HI:
        return ADDR_LO, ADDR_HI
    return va0 & 0xff000000, (va0 & 0xff000000) + 0x04000000


def _num(t):
    t = t.strip().lower()
    neg = t.startswith("-")
    t = t.lstrip("+-").strip()
    v = int(t, 16) if t.startswith("0x") else int(t[:-1], 16) if t.endswith("h") else int(t)
    return (-v if neg else v) & 0xffffffff


def _bop(o, mn, imm_sym, mem_sym, rng):
    o = o.strip()
    m = re.match(r"((?:\w+ ptr )?(?:[cdefgs]s:)?)\[(.*)\]$", o)
    if m:
        regs, disp = [], 0
        for sign, t in re.findall(r"([+-]?)\s*([^+-]+)", m.group(2)):
            t = t.strip()
            if re.fullmatch(r"(?:0x[0-9a-f]+|\d+)", t):
                disp = (disp + (-_num(t) if sign == "-" else _num(t))) & 0xffffffff
            else:
                regs.append(t.replace(" ", ""))
        if "rip" in regs or any(re.search(r"[A-Za-z_?@$][\w?@$]*", r) and not REG.fullmatch(r) and
                                not re.fullmatch(r"\w+\*\d", r) for r in regs):
            regs = [r for r in regs if r != "rip" and (REG.fullmatch(r) or re.fullmatch(r"\w+\*\d", r))]
            mem_sym = True                              # x64: rip-relative (a global), or a name the disassembler printed
        if mem_sym or not regs or rng[0] <= disp < rng[1]:
            d = "+A" if regs else "A"
        else:
            d = "%+d" % (disp - (1 << 32) if disp & 0x80000000 else disp) if disp else ""
        return "%s[%s%s]" % (m.group(1), "+".join(regs), d)
    if mn in ("call", "jmp") and re.fullmatch(r"[A-Za-z_?@$][\w?@$:<>.]*", o) and not REG.fullmatch(o):
        return "T"                                      # a call (or tail-jump) target printed by name
    if re.fullmatch(r"-?(?:0x[0-9a-f]+|\d+)", o):
        if mn == "call":
            return "T"                                  # a call target (relocated in ours)
        if mn.startswith(("j", "loop")):
            return "L"                                  # a jump target: the instruction stream shows the layout
        v = _num(o)
        return "A" if imm_sym or rng[0] <= v < rng[1] else "%#x" % v
    return o


def _bkey(d, rng):
    mn, ops, imm_sym, mem_sym = d[:4]
    if mn in ("rep", "repe", "repne", "repz", "repnz", "lock") and ops:     # capstone prints a prefix either way
        w, _, ops = ops.partition(" ")
        mn += " " + w
    if re.fullmatch(r"(?:rep\w* )?(?:movs|stos|lods|scas|cmps)[bwd]", mn):
        return (mn,)                                    # implicit operands: printed by capstone, not the listing
    k = _key((mn, ops))
    if k == ("npad", ()) or (mn == "add" and re.fullmatch(r"eax,\s*0(?:x0+)?", ops)):
        return ("npad",)
    return (mn,) + tuple(_bop(o, mn, imm_sym, mem_sym, rng) for o in _split_ops(ops))


def bkeys(lst, va0=None):
    """bdiff's keys of one side: instructions as normalised capstone text with the masked fields replaced (`A` an
    address, `T` a call target, `L` a jump target), a run of alignment pads as one ("npad",).  `lst` is a
    listing_instrs or orig_instrs result; a plain [(mnemonic, operands)] list is read as capstone text."""
    return _bside(lst, va0)[0]


def _bside(lst, va0=None):
    """(keys, {address: key index}, {key index: jump target address}) of one side."""
    dec = getattr(lst, "decoded", None)
    if dec is None:
        dec = [tuple(x) + (False, False) for x in lst]
    addrs = getattr(lst, "addrs", None) or [None] * len(dec)
    rng = _addr_range(va0 if va0 is not None else getattr(lst, "va0", None))
    out, at, tg = [], {}, {}
    for d, va in zip(dec, addrs):
        if d[0] == "db":
            continue                                    # data in our procedure (a jump table)
        k = _bkey(d, rng)
        if not (k == ("npad",) and out and out[-1] == k):
            out.append(k)
        if va is not None:
            at.setdefault(va, len(out) - 1)
            if k[-1:] == ("L",):
                try:
                    tg[len(out) - 1] = _num(d[1])
                except ValueError:
                    pass
    return out, at, tg


def _lcs_count(a, b):
    """Both sides' unmatched count of an LCS-style alignment; a's lines past its last match are ignored (our listing
    may carry EH funclets the original's function range lacks)."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    blocks = [m for m in sm.get_matching_blocks() if m.size]
    if not blocks:
        return len(a) + len(b)
    last = blocks[-1]
    matched = sum(m.size for m in blocks)
    return (last.a + last.size - matched) + (len(b) - matched)


def _bshape(k):
    return (k[0],) + tuple(tuple(REG.findall(o)) for o in k[1:])


def bdiff(ours, orig):
    """The default score: instructions as the bytes encode them (mnemonic, registers, displacements, immediates,
    operand order and size), relocated addresses, call targets and padding masked. Aligned like regdiff, on the
    instruction shapes (mnemonic + registers per operand, so a moved stack slot does not break the alignment);
    unmatched instructions count 1 per side, an aligned pair that differs in a displacement or immediate 1, an
    aligned jump to another (aligned) instruction than the original's 1, and each change of the byte offset between
    consecutive aligned pairs 1 (where the code moves against the original's: a longer or shorter encoding, an
    inserted or dropped instruction). 0 means the same instruction stream as the original up to those masks. Needs listing code bytes (/FAsc) on our side; without them it falls back to
    regdiff."""
    if getattr(ours, "decoded", None) is None and isinstance(ours, Instrs):
        return regdiff(ours, orig)
    return _bparts(ours, orig)["n"]


def _bparts(ours, orig):
    """bdiff and its parts: n (the score), fields (aligned pairs differing in a field), jumps (aligned jumps to
    another target), shifts (changes of the byte offset between aligned pairs: where the code moves), moved
    (aligned pairs at another byte offset than the original's). n = unmatched + fields + jumps + shifts; moved
    is not part of n."""
    va0 = getattr(orig, "va0", None)
    a, at_a, tg_a = _bside(ours, va0)
    b, at_b, tg_b = _bside(orig, va0)
    sa, sb = [_bshape(x) for x in a], [_bshape(x) for x in b]
    sm = difflib.SequenceMatcher(None, sa, sb, autojunk=False)
    blocks = [m for m in sm.get_matching_blocks() if m.size]
    if not blocks:
        return dict(n=len(a) + len(b), fields=0, jumps=0, shifts=0, moved=0)
    last = blocks[-1]
    matched = sum(m.size for m in blocks)
    n = (last.a + last.size - matched) + (len(b) - matched)   # our lines past the last match (EH funclets) do not count
    amap = {m.a + k: m.b + k for m in blocks for k in range(m.size)}
    fields = sum(1 for i, j in amap.items() if a[i] != b[j])
    bmatched = set(amap.values())
    jumps = 0
    for i, j in amap.items():
        if i in tg_a and j in tg_b:
            ti, tj = at_a.get(tg_a[i]), at_b.get(tg_b[j])
            if ti is not None and tj is not None and ti in amap and tj in bmatched and amap[ti] != tj:
                jumps += 1
    ka = {k: va for va, k in sorted(at_a.items(), reverse=True)}
    kb = {k: va for va, k in sorted(at_b.items(), reverse=True)}
    shifts, moved, prev = 0, 0, None
    for i in sorted(amap):
        if i in ka and amap[i] in kb:
            dlt = ka[i] - (kb[amap[i]] - (va0 or 0))
            shifts += prev is not None and dlt != prev
            moved += dlt != 0
            prev = dlt
    return dict(n=n + fields + jumps + shifts, fields=fields, jumps=jumps, shifts=shifts, moved=moved)


# rdiff: bdiff's keys with register allocation and scheduling factored out. The registers of the general families
# (eax..ebp and their parts; esp stays literal) are replaced by their width in the alignment key, so a renamed
# register no longer unmatches an instruction; register agreement is then checked per value: at an aligned pair that
# writes a new value (mov/lea/movzx/movsx/pop/setcc into a full register, xor r,r) the two registers are bound to
# each other, and a later read that disagrees with the binding counts 1. A call ends ecx/edx and binds eax to eax.
_RFAM = {n: f for f, names in dict(a="eax ax al ah", c="ecx cx cl ch", d="edx dx dl dh", b="ebx bx bl bh",
                                   si="esi si", di="edi di", bp="ebp bp").items() for n in names.split()}
_RREG = re.compile(r"\b(e?[abcd]x|[abcd][lh]|e?si|e?di|e?bp)\b")
if X64:   # x64: rax eax ax al ah, rsi esi si sil, r8 r8d r8w r8b, ...; rsp stays literal
    _RFAM = {n: f for f, names in dict(a="rax eax ax al ah", c="rcx ecx cx cl ch", d="rdx edx dx dl dh",
                                       b="rbx ebx bx bl bh", si="rsi esi si sil", di="rdi edi di dil",
                                       bp="rbp ebp bp bpl").items() for n in names.split()}
    _RFAM.update({"r%d%s" % (k, w): "r%d" % k for k in range(8, 16) for w in ("", "d", "w", "b")})
    _RREG = re.compile(r"\b(r[abcd]x|e?[abcd]x|[abcd][lh]|r[sd]i|e?[sd]i|[sd]il|r[b]p|e?bp|bpl|r(?:[89]|1[0-5])[dwb]?)\b")


def _rwidth(r):
    """The width class of a general register name in the alignment key."""
    if r[-1] in "lh" and r not in ("sil", "dil", "bpl") or r[-1] == "b" and r[0] == "r" or r in ("sil", "dil", "bpl"):
        return "r8"
    if len(r) == 2 or r[-1] == "w" and r[0] == "r" and r[1:-1].isdigit():
        return "r16"
    if X64 and (r[0] == "r" and not r[-1] in "dwb" or r[0] == "r" and r[1:].isdigit()):
        return "r64"
    return "r"
_RPURE = {"mov", "lea", "movzx", "movsx", "movsxd", "pop"}
_RSTORE = {"mov", "fstp", "fst", "fistp", "fist", "movss", "movsd", "movq", "movd", "movaps", "movups"}
_RNUM = re.compile(r"[+-]?(?:0x[0-9a-f]+|\d+)\b")
_RSLOT = re.compile(r"\[(esp|ebp|rsp|rbp)([+-]\d+)?\]")
_SAVED = ("b", "si", "di", "bp") + (("r12", "r13", "r14", "r15") if X64 else ())   # callee-saved families


def _rview(keys):
    """Per instruction: (alignment key, register written as a new value, registers read, slot stored, slots read).
    A frame slot is named by its offset from the entry esp (pushes, pops and esp adjustments since entry counted;
    a callee's own stack cleanup is not, which both sides share at an aligned call) or from ebp when ebp holds the
    frame; in the key a local is `[slot]`, an incoming argument `[arg+N]` (not renamed)."""
    frame = any(k[:3] in (("mov", "ebp", "esp"), ("mov", "rbp", "rsp")) for k in keys[:8])
    fp, word = ("rbp", 8) if X64 else ("ebp", 4)
    out, depth, written = [], 0, set()
    for k in keys:
        slots = []

        def sub(m):
            base, d = m.group(1), int(m.group(2) or 0)
            if base in ("ebp", "rbp") and not frame:
                return m.group(0)
            name = ("b", d) if base in ("ebp", "rbp") else ("s", d - depth)
            if name[1] > 0:                             # an incoming argument: fixed by the calling convention
                return "[arg%+d]" % (d if base in ("ebp", "rbp") else d - depth + word)
            slots.append(name)
            return "[slot]"
        ops = [_RSLOT.sub(sub, o) for o in k[1:]]
        mn = k[0]
        shape = (mn,) + tuple(_RREG.sub(lambda m: _rwidth(m.group(1)), o) if not (frame and o == fp) else o
                              for o in ops)
        regs = [[_RFAM[r] for r in _RREG.findall(o) if not (frame and r == fp)] for o in ops]
        for q, o in enumerate(ops):                     # [r1+r2+D]: base and index unscaled commute
            if len(regs[q]) == 2 and "*" not in o and "[" in o:
                regs[q] = [tuple(regs[q])]
        wreg = None
        m = _RREG.fullmatch(ops[0].strip()) if ops else None
        if m and not (frame and m.group(1) == fp):
            r = m.group(1)
            if (mn in _RPURE and (_rwidth(r) in ("r", "r64") or mn in ("movzx", "movsx", "movsxd"))) or \
                    mn.startswith("set"):
                wreg = _RFAM[r]
            elif mn in ("xor", "sub") and len(ops) == 2 and ops[0].strip() == ops[1].strip():
                wreg, regs = _RFAM[r], []
        rregs = [x for o in (regs[1:] if wreg else regs) for x in o]
        if mn == "push" and rregs and rregs[0] in _SAVED and rregs[0] not in written:
            rregs, shape = [], ("push", "save " + k[1])    # saving a callee-saved register: not a value; c2's
            #                                             order is fixed, so another order is a difference
        elif X64 and mn == "mov" and depth == 0 and len(ops) == 2 and "[arg" in shape[1] and rregs and \
                rregs[0] in _SAVED and rregs[0] not in written:
            rregs, shape, slots = [], ("mov", shape[1], "save " + k[2]), []   # x64: saved into its home slot
        if m and mn not in ("push", "cmp", "test"):
            written.add(_RFAM[m.group(1)])
        wslot = None
        if slots and ops and "[slot]" in ops[0] and mn in _RSTORE and not (len(ops) > 1 and "[slot]" in ops[1]):
            wslot, slots = slots[0], slots[1:]
        out.append((shape, wreg, rregs, wslot, slots))
        if mn == "push":
            depth += word
        elif mn == "pop":
            depth -= word
        elif mn in ("sub", "add") and len(k) == 3 and k[1] in ("esp", "rsp") and re.fullmatch(r"0x[0-9a-f]+|\d+", k[2]):
            depth += (1 if mn == "sub" else -1) * int(k[2], 0)
    return out


def rdiff(ours, orig):
    """bdiff up to register renaming and scheduling (the comment above _rshape): unmatched instructions 1 per side,
    an instruction the scheduler moved (an unmatched pair of one key within 8 positions) 1, a pair differing only in
    a displacement or immediate 1, a read of a register that disagrees with the value binding 1. 0: the original's
    instruction stream up to register choice and the order of independent instructions."""
    if getattr(ours, "decoded", None) is None and isinstance(ours, Instrs):
        return regdiff(ours, orig)
    return _rparts(ours, orig)["n"]


def _rparts(ours, orig, win=8, why=None):
    """rdiff and its parts: unmatched, moved, fields, reads (register and frame-slot reads that disagree with the
    value binding), renamed (aligned value definitions in another register or slot: not part of n). `why`, a list,
    gets (ours index, original index, ours name, original name, ours bound to, original bound to) per counted read."""
    va0 = getattr(orig, "va0", None)
    return _rscore(_bside(ours, va0)[0], _bside(orig, va0)[0], win, why)


def _rscore(ka, kb, win=8, why=None):
    """_rparts on two sides' bdiff keys (bkeys)."""
    best = None
    for keyed in (False, True):         # aligned register-blind, and as bdiff aligns (difflib's greedy blocks can
        w = [] if why is not None else None             # land worse either way): the lower score
        r = _rparts1(ka, kb, win, w, keyed)
        if best is None or r["n"] < best[0]["n"]:
            best = r, w
    if why is not None:
        why.extend(best[1])
    return best[0]


def _rparts1(ka, kb, win, why, keyed):
    import bisect
    va, vb = _rview(ka), _rview(kb)
    sa, sb = [x[0] for x in va], [x[0] for x in vb]
    al_a, al_b = ([_bshape(x) for x in ka], [_bshape(x) for x in kb]) if keyed else (sa, sb)
    blocks = [m for m in difflib.SequenceMatcher(None, al_a, al_b, autojunk=False).get_matching_blocks() if m.size]
    if not blocks:
        return dict(n=len(sa) + len(sb), unmatched=len(sa) + len(sb), moved=0, fields=0, reads=0, order=0, renamed=0)
    last = blocks[-1]
    amap = {m.a + k: m.b + k for m in blocks for k in range(m.size)}
    aligned_fields = sum(1 for i, j in amap.items() if sa[i] != sb[j])  # bdiff's alignment: another displacement
    keys = sorted(amap)
    ua = [i for i in range(last.a + last.size) if i not in amap]   # our lines past the last match: EH funclets
    taken = set(amap.values())
    ub = [j for j in range(len(sb)) if j not in taken]

    def near(i):                                    # where our unmatched i sits on the original's side
        p = bisect.bisect_left(keys, i)
        return amap[keys[p - 1]] if p else 0

    def pair(same):
        n = 0
        for i in list(ua):
            p = near(i)
            j = next((j for j in ub if abs(j - p) <= win and same(sa[i], sb[j])), None)
            if j is not None:
                ua.remove(i); ub.remove(j); amap[i] = j; n += 1
        return n
    moved = pair(lambda x, y: x == y)
    fields = aligned_fields + pair(lambda x, y: len(x) == len(y) and x[0] == y[0] and
                                   all(_RNUM.sub("#", p) == _RNUM.sub("#", q) for p, q in zip(x[1:], y[1:])))
    fw, bw = {}, {}

    def bind(x, y):
        if x in fw:
            bw.pop(fw[x], None)
        if y in bw:
            fw.pop(bw[y], None)
        fw[x], bw[y] = y, x

    def drop(x):
        bw.pop(fw.pop(x, None), None)
        fw.pop(bw.pop(x, None), None)
    reads = renamed = order = 0
    for i in sorted(amap, key=amap.get):
        (ka, wa, ra, sa_w, sa_r), (kb, wb, rb, sb_w, sb_r) = va[i], vb[amap[i]]
        pairs = []
        for x, y in zip(ra, rb):
            if isinstance(x, tuple) and isinstance(y, tuple):
                if fw.get(x[0]) != y[0] and (fw.get(x[0]) == y[1] or fw.get(x[1]) == y[0]):
                    y = (y[1], y[0])
                    order += 1                          # the same address, base and index swapped: IL order
                pairs += zip(x, y)
            elif not isinstance(x, tuple) and not isinstance(y, tuple):
                pairs.append((x, y))
            else:
                pairs += zip(x if isinstance(x, tuple) else (x,), y if isinstance(y, tuple) else (y,))
        for x, y in pairs + list(zip(sa_r, sb_r)):
            if fw.get(x) == y or (x == y and x not in fw and y not in bw):
                fw.setdefault(x, y); bw.setdefault(y, x)
                continue
            reads += 1
            if why is not None:
                why.append((i, amap[i], x, y, fw.get(x), bw.get(y)))
            bind(x, y)
        for x, y in ((wa, wb), (sa_w, sb_w)):
            if x is not None and y is not None:
                renamed += x != y
                bind(x, y)
        if ka[0] == "call" and kb[0] == "call":
            drop("c"); drop("d")
            bind("a", "a")
    unmatched = len(ua) + len(ub)
    return dict(n=unmatched + moved + fields + reads + order, unmatched=unmatched, moved=moved, fields=fields,
                reads=reads, order=order, renamed=renamed)


def posdiff(ours, orig):
    """Positional difference over the common length (the old regdiff)."""
    return sum(1 for x, y in zip(cs_text(ours), orig) if _key(x) != _key(y))


class Ctx:
    def __init__(self, src, func, cl, need_regs, need_scratch, orig, asm_func, work):
        self.func = func
        self.regs = self.picks = None
        self.ns = {}
        if need_regs:
            _, _, rc, out, log = c2trace.run_trace(src, c2regs.HOOKS, cl, keep=os.path.join(work, "r"), quiet=True)
            fns = c2regs.parse(log, func)
            if not fns:
                raise RuntimeError("function not register-allocated (compile error?): " + cl_errors(out))
            self.regs = fns[0]
        if need_scratch or orig:
            _, _, rc, out, log = c2trace.run_trace(src, c2scratch.HOOKS, cl, keep=os.path.join(work, "s"), quiet=True)
            picks, final = c2scratch.parse(log, func)
            pos = c2scratch.offsets(final)
            for p in picks:
                at = pos.get(p["instr"])
                p["off"] = at[0] if at else None
            self.picks = picks
            stem = os.path.splitext(os.path.basename(src))[0]
            asm = os.path.join(work, "s", stem + ".asm")
            if orig:
                ours = listing_instrs(asm, asm_func)
                self.ns["regdiff"] = regdiff(ours, orig)
                self.ns["bdiff"] = bdiff(ours, orig)
                self.ns["rdiff"] = rdiff(ours, orig)
                self.ns["posdiff"] = posdiff(ours, orig)
            self.lst = listing_offsets(asm, asm_func) if asm_func else {}
        self.ns.update(prio=self.prio, reg=self.reg, pops=self.pops, cur=self.cur, sreg=self.sreg, rot=self.rot,
                       off=self.off, gone=self.gone, sat=self.sat, ins=self.ins, dst=self.dst, hinge=lambda x, t: max(0, x - t),
                       max=max, min=min, abs=abs, inf=math.inf)

    def _rows(self, var):
        return [r for r in self.regs["rows"] if r["name"] == (var[1:] if var.startswith("_") else var)]

    def prio(self, var, k=None):
        if k is None:
            rows = self._rows(var)
            if not rows:
                raise KeyError("no globreg %r" % var)
            return rows[0]["prio"]
        passes = self.regs.get("passes", {})
        for kk in range(k, 0, -1):          # later passes only re-cost new pieces: fall back to the latest earlier one
            vals = [t for (nm, g), t in passes.get(kk, {}).items() if nm == (var[1:] if var.startswith("_") else var)]
            if vals:
                return max(vals)
        return None

    def reg(self, var, n=1):
        rows = self._rows(var)
        return rows[n - 1]["out"] if len(rows) >= n else None

    def pops(self, var):
        return len(self._rows(var))

    def _pick(self, n):
        return self.picks[n - 1]

    def cur(self, n):
        return self._pick(n)["cur"]

    def sreg(self, n):
        return self._pick(n)["reg"]

    def rot(self, n):
        return int(self._pick(n)["rot"])

    def off(self, n):
        return self._pick(n)["off"]

    def gone(self):
        return sum(1 for p in self.picks if p["off"] is None)

    def ins(self, o):
        return self.lst.get(o)

    def dst(self, o):
        t = self.lst.get(o)
        m = REG.search(t.split(None, 1)[1].lower()) if t and " " in t else None
        return m.group(1) if m else None

    def sat(self, o):
        for p in self.picks:
            if p["off"] == o:
                return p["reg"]
        return None


def main():
    argv = sys.argv[1:]
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("template")
    ap.add_argument("--func", required=True)
    ap.add_argument("--metric", default="(rdiff, bdiff)")
    ap.add_argument("--show", action="append", default=[])
    ap.add_argument("--asm-vs", help="the original function's disassembly ('VA mnemonic operands' lines)")
    ap.add_argument("--asm-func", help="the function in our listing (for bdiff/regdiff/ins/dst; default --func)")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--max", type=int, default=256)
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    a.asm_func = a.asm_func or a.func
    exprs = [a.metric] + a.show
    need_regs = any(re.search(r"\b(prio|reg|pops)\(", e) for e in exprs)
    need_scratch = any(re.search(r"\b(cur|sreg|rot|off|gone|sat|ins|dst)\(", e) for e in exprs)
    if re.search(r"\b(bdiff|rdiff|regdiff|posdiff)\b", " ".join(exprs)) and not a.asm_vs:
        sys.exit("bdiff/rdiff/regdiff need --asm-vs (the original's disassembly); give --metric for a trace-only score")
    orig = orig_instrs(a.asm_vs) if a.asm_vs else None

    text = open(a.template).read()
    if any("{{" in m.group(1) and "<|>" in m.group(1) for m in ALT.finditer(text)):
        sys.exit("nested {{..}} alternatives are not supported: flatten the grid (every variant would fail to compile)")
    # a `{{..}}` without `<|>` is code, not a spot: a nested brace initialiser (`int f[2][2]={{0,1},{2,3}};`) in a
    # pack unit used to be taken as a one-way spot and lost its outer braces in every variant
    spots = [m.group(1).split("<|>") for m in ALT.finditer(text) if "<|>" in m.group(1)]
    # a `{{a|b}}` (one bar) is a mistyped spot, not code: it would run as one variant with the braces reaching cl
    for m in ALT.finditer(text):
        if "<|>" not in m.group(1) and re.search(r"(?<![|<])\|(?![|>])", m.group(1)) and "{" not in m.group(1):
            print("warning: line %d: {{%s}} has a `|` but no `<|>`: alternatives are separated by `<|>`; it stays code"
                  % (text.count("\n", 0, m.start()) + 1, m.group(1)[:60]), file=sys.stderr, flush=True)
    combos = list(itertools.product(*[range(len(s)) for s in spots]))
    if len(combos) > a.max:
        sys.exit("%d combinations (> --max %d): split the grid" % (len(combos), a.max))
    # the template's directory enters the name: two templates called tmpl.cpp elsewhere (two agents) must not share,
    # and rmtree, one variant directory
    out = os.path.join(ROOT, "work", "tsweep", out_name(a.template))
    if c2trace.c2build.table():       # another c2 build (--c2 / C2_BUILD): its own variant directory
        out += "@" + c2trace.c2build.name()
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    hdr_dir = os.path.dirname(os.path.abspath(a.template))
    jobs = []
    for k, combo in enumerate(combos):
        it = iter(combo)
        body = ALT.sub(lambda m: m.group(1).split("<|>")[next(it)] if "<|>" in m.group(1) else m.group(0), text)
        body = re.sub(r'#include "([^"]+)"', lambda m: '#include "%s"' % os.path.join(hdr_dir, m.group(1))
                  if os.path.exists(os.path.join(hdr_dir, m.group(1))) else m.group(0), body)
        d = os.path.join(out, "v%03d" % k)
        os.makedirs(d)
        p = os.path.join(d, "v%03d.cpp" % k)
        open(p, "w").write(body)
        jobs.append((p, combo, d))
    print("%d spots, %d combinations, %d jobs (traces: %s)" % (len(spots), len(combos), a.jobs,
          "+".join(n for n, f in (("regs", need_regs), ("scratch", need_scratch or orig)) if f)), flush=True)

    def run(job):
        p, combo, d = job
        try:
            ctx = Ctx(p, a.func, cl, need_regs, need_scratch, orig, a.asm_func, d)
            vals = [eval(e, {"__builtins__": {}}, ctx.ns) for e in exprs]
            return job, vals, None
        except SystemExit as e:
            return job, None, "trace failed: %s" % cl_errors(str(e), 200)
        except Exception as e:
            return job, None, "%s: %s" % (type(e).__name__, e)

    # one line per finished combination: n/N, elapsed, its metric, the best so far (a long sweep is not silent)
    t0, res, best = time.time(), [], None
    with ThreadPoolExecutor(a.jobs) as ex:
        for n, r in enumerate(ex.map(run, jobs), 1):
            res.append(r)
            v = r[1][0] if r[1] is not None else None
            if v is not None and (best is None or v < best[0]):
                best = (v, os.path.basename(r[0][0]))
            print("  [%d/%d %4.0fs] %s: %s   best %s" % (n, len(jobs), time.time() - t0, os.path.basename(r[0][0]),
                  v if v is not None else (r[2] or "?")[:60], "%s (%s)" % best if best else "-"), file=sys.stderr,
                  flush=True)
    ok = sorted(((v[0], j) + (v,) for j, v, err in res if v is not None and v[0] is not None),
                key=lambda t: t[0])
    bad = [(j, err) for j, v, err in res if v is None or v[0] is None]
    hdr = "metric" + "".join("  | %s" % s for s in a.show)
    print(hdr)
    for m, (p, combo, d), vals in ok[:a.top]:
        print("%8s%s  %s  %s" % (m, "".join("  | %s" % (x,) for x in vals[1:]), os.path.basename(p),
              " | ".join("%d:%s" % (i, spots[i][c].strip().replace("\n", " ")[:40]) for i, c in enumerate(combo))))
    if bad:
        print("%d variants unscored, e.g. %s: %s" % (len(bad), os.path.basename(bad[0][0][0]), bad[0][1]))
    if ok and (ok[0][0] == 0 or (isinstance(ok[0][0], tuple) and not any(ok[0][0]))):
        print("METRIC 0: %s" % ok[0][1][0])


if __name__ == "__main__":
    main()
