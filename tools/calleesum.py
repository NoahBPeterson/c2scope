#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["capstone==5.0.7", "pefile==2024.8.26"]
# ///
"""calleesum: what a real callee does through each pointer parameter, as the stub a decompiled caller needs.

Why (docs/c2/callee.md): at /O1, /O2, /Ox c2 compiles a same-translation-unit callee before its caller and
records, per parameter, whether the callee reads / writes memory through it and whether the pointer escapes
(ItfSummarizeFuncInterf).  The caller's alias analysis, dead-store removal and register allocation use that
summary.  A bodiless declaration (with or without __declspec(noalias)) means "reads, writes and keeps every
pointer argument", so a draft whose callee is only declared can differ from the original by registers alone.

usage:
  calleesum.py EXE VA [--proto 'void __thiscall C::f(T** a, U* b)'] [--conv C] [--nparams N]
                      [--tu LO-HI] [--depth N] [--pop VA=BYTES] [--pure VA,VA] [--no-stub] [-v]
      table per parameter + calls + globals + a C++ stub with the same summary.
  calleesum.py EXE --caller VA [--tu LO-HI] [...]
      the caller's calls that receive addresses of its locals (where the rule can matter), each direct callee
      summarised.

  --proto    the callee's declaration as it stands in the caller's unit.  Gives names, the convention
             (`__thiscall`/`__stdcall`/`__fastcall`/`__cdecl`, or `C::f` = thiscall) and the stub's signature.
  --conv     thiscall|stdcall|cdecl|fastcall when there is no --proto (default: detected from ecx/edx use and
             the `ret N`).   --nparams N: stack parameters for cdecl (default: highest argument slot read).
  --tu LO-HI VA range to treat as the callee's translation unit.  A parameter handed on to a direct call inside
             the range gets that function's summary (recursively, --depth, default 3), as c2 does.  Outside it,
             and by default, handing a pointer to a call counts as read + written + kept, which is what c2
             assumes for a callee it has no body for.
  --pop      stack bytes an indirect call at VA pops, when the guess (all pushes since the last call) is wrong.
  --pure     direct call targets that are compiler helpers (__ftol2, _allmul ...), not calls in the source.
  --trace    print every instruction reached with the tracked stack depth and what its registers hold
             (param names, `*[p]` = a value loaded through p, `&stack[off]`): the evidence for the table.
  -v         also list where a parameter's value was lost.

x64 (PE32+, machine 0x8664): detected from the header.  One convention: parameters 1..4 in rcx, rdx, r8, r9
(xmm0..3 for floating point), 5+ at [rsp+0x28], [rsp+0x30] ... at entry; spills into the home area [rsp+8..0x20]
are followed like any stack slot.  Parameter numbers are positions (0 = rcx = `this` of a member).  --conv is
ignored; --proto gives names and the count (`this` first for a non-static member, calling-convention keywords
ignored); --nparams N is the total number of parameters.  Without either, the count is the highest parameter
register or stack slot read before it is written (also by a direct callee it is handed to).  A call's arguments
are the registers (and stack slots above its home area) the direct callee reads; for an indirect call or an
unanalysable target, all four registers, an argument register not written since the entry or the last call
reported as possibly stale.

Honest limits: values are followed through registers, stack slots and pointer arithmetic; when a parameter's
value is lost (merged with something else at a join, pushed through arithmetic the tool does not model, kept
in a local whose address escapes) and the function also stores through pointers of unknown origin, the
parameter is reported `unknown`, not guessed.  The tool reads machine code after optimisation, c2 summarises
the IL before it: an access that the optimiser removed (`if (0) *p = 1`, a dead read) is in c2's summary and
not in the code."""
import argparse, os, re, struct, sys

import capstone
from capstone import x86 as X

CALLEE_SAVED = ("ebx", "ebp", "esi", "edi")
FULL = {}
for _f, _subs in {"eax": "al ah ax", "ecx": "cl ch cx", "edx": "dl dh dx", "ebx": "bl bh bx",
                  "esp": "sp spl", "ebp": "bp bpl", "esi": "si sil", "edi": "di dil"}.items():
    FULL[_f] = _f
    for _s in _subs.split():
        FULL[_s] = _f
GPR = ("eax", "ecx", "edx", "ebx", "ebp", "esi", "edi")
FP_STORES = {getattr(X, "X86_INS_" + n) for n in ("FST", "FSTP", "FIST", "FISTP", "FISTTP", "FNSTCW", "FNSTSW", "FBSTP",
                                                "FNSAVE", "FNSTENV", "FXSAVE", "STMXCSR") if hasattr(X, "X86_INS_" + n)}
MAXDEEP = 3
# x64 (PE32+): register families, the Microsoft x64 convention
FULL64 = {}
for _f, _subs in {"rax": "eax ax al ah", "rcx": "ecx cx cl ch", "rdx": "edx dx dl dh", "rbx": "ebx bx bl bh",
                  "rsp": "esp sp spl", "rbp": "ebp bp bpl", "rsi": "esi si sil", "rdi": "edi di dil"}.items():
    FULL64[_f] = _f
    for _s in _subs.split():
        FULL64[_s] = _f
for _n in range(8, 16):
    for _s in ("", "d", "w", "b"):
        FULL64["r%d%s" % (_n, _s)] = "r%d" % _n
GPR64 = ("rax", "rcx", "rdx", "rbx", "rbp", "rsi", "rdi") + tuple("r%d" % _n for _n in range(8, 16))
CALLEE_SAVED64 = ("rbx", "rbp", "rdi", "rsi", "r12", "r13", "r14", "r15")
CALLER_SAVED64 = ("rax", "rcx", "rdx", "r8", "r9", "r10", "r11")
ARGREGS64 = ("rcx", "rdx", "r8", "r9")
HOME64 = 0x20                                       # home area: [rsp+8..0x20] at entry, [rsp..0x18] at a call


class Exe:
    def __init__(self, path):
        import pefile
        self.path = path
        self.pe = pefile.PE(path, fast_load=True)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.size = self.pe.OPTIONAL_HEADER.SizeOfImage
        self.data = self.pe.get_memory_mapped_image()
        self.x64 = self.pe.FILE_HEADER.Machine == 0x8664
        self.text = [(self.base + s.VirtualAddress, self.base + s.VirtualAddress + max(s.Misc_VirtualSize, s.SizeOfRawData))
                     for s in self.pe.sections if s.Characteristics & 0x20000000]
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64 if self.x64 else capstone.CS_MODE_32)
        self.md.detail = True
        self.cache = {}
        self.imports = {}
        try:
            self.pe.parse_data_directories(directories=[1])
            for e in getattr(self.pe, "DIRECTORY_ENTRY_IMPORT", []):
                for i in e.imports:
                    self.imports[i.address] = "%s!%s" % (e.dll.decode().lower().rsplit(".", 1)[0],
                                                         i.name.decode() if i.name else "ord%d" % i.ordinal)
        except Exception:
            pass

    def in_image(self, va):
        return self.base + 0x1000 <= va < self.base + self.size

    def in_text(self, va):
        return any(lo <= va < hi for lo, hi in self.text)

    def read(self, va, n):
        o = va - self.base
        return bytes(self.data[o:o + n]) if 0 <= o < len(self.data) else b""

    def u32(self, va):
        b = self.read(va, 4)
        return struct.unpack("<I", b)[0] if len(b) == 4 else None

    def ins(self, va):
        i = self.cache.get(va)
        if i is None:
            i = next(self.md.disasm(self.read(va, 16), va, 1), None)
            self.cache[va] = i
        return i

    def is_cookie_check(self, va):
        """__security_check_cookie: cmp ecx,[cookie]; jne; rep ret.  c2 adds the call after it has summarised."""
        b = self.read(va, 10)
        if getattr(self, "x64", False):                     # cmp rcx, [rip+cookie]; jne (or bnd jne)
            return b[:3] == b"\x48\x3b\x0d" and b[7:8] in (b"\x75", b"\xf2")
        return b[:2] == b"\x3b\x0d" and b[6:7] == b"\x75"

    def is_chkstk64(self, va):
        """x64 __chkstk: sub rsp,10h; mov [rsp],r10; mov [rsp+8],r11.  Probes, moves nothing: rax is kept."""
        return self.read(va, 13) == b"\x48\x83\xec\x10\x4c\x89\x14\x24\x4c\x89\x5c\x24\x08"

    def quick64(self, va, depth=0):
        """x64: (parameter positions read before written, stack parameters read, a ret was reached) from a walk
        of the function's code that follows only which argument registers were written (must-written at joins)
        and rsp.  A direct call counts as reading the argument registers its target reads (depth 3)."""
        key = ("q64", va)
        if key in self.cache:
            return self.cache[key]
        act = self.cache.setdefault("q64active", set())
        if depth > 3 or va in act:
            return frozenset(), 0, False
        act.add(va)
        argn = {r: k for k, r in enumerate(ARGREGS64)}
        argn.update({"xmm%d" % k: k for k in range(4)})
        allw = frozenset(argn) | frozenset("xmm%d" % k for k in range(4, 6))
        used, nstk, ok, n = set(), 0, False, 0
        states, work = {va: (frozenset(), 0)}, [va]
        while work and n < 6000:
            a = work.pop()
            wr, d = states[a]
            while n < 6000:
                n += 1
                i = self.ins(a) if self.in_text(a) else None
                if i is None or i.id in (X.X86_INS_INT3, X.X86_INS_HLT, X.X86_INS_UD2):
                    break
                if i.id in (X.X86_INS_RET, X.X86_INS_RETF):
                    ok = True
                    break
                ops = i.operands
                if i.id == X.X86_INS_CALL:
                    if ops[0].type == X.X86_OP_IMM:
                        u = self.quick64(ops[0].imm, depth + 1)[0]
                        used |= {k for k in u if k < 4 and ARGREGS64[k] not in wr and "xmm%d" % k not in wr}
                    wr = allw                                # what is read after a call is not a parameter
                    a += i.size
                    continue
                succ = []
                if i.id == X.X86_INS_JMP:
                    if ops[0].type != X.X86_OP_IMM:         # switch or indirect tail jump: not followed
                        break
                    succ = [ops[0].imm]
                else:
                    try:
                        rd, wrr = i.regs_access()
                    except capstone.CsError:
                        rd, wrr = [], []
                    same = len(ops) == 2 and ops[0].type == ops[1].type == X.X86_OP_REG and ops[0].reg == ops[1].reg \
                        and i.id in (X.X86_INS_XOR, X.X86_INS_SUB, X.X86_INS_SBB, X.X86_INS_PXOR, X.X86_INS_XORPS,
                                     X.X86_INS_XORPD)
                    same = same or (len(ops) == 2 and ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_IMM and
                                    ((i.id == X.X86_INS_OR and (ops[1].imm + 1) % (1 << 8 * ops[0].size) == 0) or
                                     (i.id == X.X86_INS_AND and ops[1].imm == 0)))
                    if not same:
                        for r in rd:
                            nm = i.reg_name(r)
                            f = FULL64.get(nm, nm)
                            if f in argn and f not in wr:
                                used.add(argn[f])
                    for op in ops:
                        if op.type == X.X86_OP_MEM and op.mem.base == X.X86_REG_RSP and not op.mem.index and \
                                (not op.access or op.access & capstone.CS_AC_READ):
                            off = d + op.mem.disp
                            if off >= 8 + HOME64:
                                nstk = max(nstk, (off - 8 - HOME64) // 8 + 1)
                    wr = wr | {FULL64.get(i.reg_name(r), i.reg_name(r)) for r in wrr} & allw
                    if i.id == X.X86_INS_PUSH:
                        d -= 8
                    elif i.id == X.X86_INS_POP:
                        d += 8
                    elif i.id in (X.X86_INS_ADD, X.X86_INS_SUB) and ops[0].type == X.X86_OP_REG and \
                            ops[0].reg == X.X86_REG_RSP and ops[1].type == X.X86_OP_IMM:
                        d += ops[1].imm if i.id == X.X86_INS_ADD else -ops[1].imm
                    if i.group(X.X86_GRP_JUMP) and ops and ops[0].type == X.X86_OP_IMM:
                        succ = [a + i.size, ops[0].imm]
                    else:
                        a += i.size
                        continue
                for t in succ[1:]:
                    if t not in states:
                        states[t] = (wr, d)
                        work.append(t)
                    elif not states[t][0] <= wr:
                        states[t] = (states[t][0] & wr, states[t][1])
                        work.append(t)
                t = succ[0]
                if t in states:
                    if not states[t][0] <= wr:
                        states[t] = (states[t][0] & wr, states[t][1])
                        work.append(t)
                    break
                states[t] = (wr, d)
                a = t
        act.discard(va)
        r = (frozenset(used), nstk, ok)
        self.cache[key] = r
        return r

    def quick(self, va, _seen=None):
        """(bytes popped by `ret`, reads ecx before writing it, same for edx) from a walk of the function's
        code without data flow.  None when no `ret` is found."""
        key = ("q", va)
        if key in self.cache:
            return self.cache[key]
        pop, seen, work, n = None, set(), [va], 0
        while work and n < 6000:
            a = work.pop()
            while a not in seen and self.in_text(a) and n < 6000:
                seen.add(a)
                n += 1
                i = self.ins(a)
                if i is None:
                    break
                if i.id in (X.X86_INS_RET, X.X86_INS_RETF):
                    if pop is None:
                        pop = i.operands[0].imm if i.operands else 0
                    break
                if i.id == X.X86_INS_JMP:
                    if i.operands[0].type == X.X86_OP_IMM:
                        a = i.operands[0].imm
                        continue
                    break
                if i.id == X.X86_INS_INT3:
                    break
                if i.group(X.X86_GRP_JUMP) and i.operands and i.operands[0].type == X.X86_OP_IMM:
                    work.append(i.operands[0].imm)
                a += i.size
        use = {"ecx": False, "edx": False}
        done, a = set(), va
        for _ in range(40):                                # straight line from the entry
            i = self.ins(a)
            if i is None or i.group(X.X86_GRP_JUMP) or i.id in (X.X86_INS_CALL, X.X86_INS_RET):
                break
            rd, wr = i.regs_access()
            rd = {FULL.get(i.reg_name(r)) for r in rd}
            wr = {FULL.get(i.reg_name(r)) for r in wr}
            plain_push = i.id == X.X86_INS_PUSH and i.operands[0].type == X.X86_OP_REG   # `push ecx` reserves a slot
            if i.id in (X.X86_INS_XOR, X.X86_INS_SUB, X.X86_INS_SBB) and len(i.operands) == 2 and \
                    i.operands[0].type == i.operands[1].type == X.X86_OP_REG and i.operands[0].reg == i.operands[1].reg:
                rd = set()                                   # xor r,r: no read
            for r in use:
                if r in rd and r not in done and not plain_push:
                    use[r] = True
                if r in wr:
                    done.add(r)
            a += i.size
        r = (pop, use["ecx"], use["edx"])
        self.cache[key] = r
        return r


class State:
    __slots__ = ("regs", "slots", "esp", "pend", "esc", "aw")

    def __init__(self):
        self.regs, self.slots, self.esp, self.pend, self.esc = {}, {}, 0, 0, frozenset()
        self.aw = frozenset()                       # x64: argument registers / stack offsets written since a call

    def copy(self):
        s = State()
        s.regs, s.slots, s.esp, s.pend, s.esc = dict(self.regs), dict(self.slots), self.esp, self.pend, self.esc
        s.aw = self.aw
        return s


def root(v):
    return v is not None and v[0] in ("p", "d")


class Summary:
    """Result for one function.  Parameter indices: registers first (this / fastcall ecx, edx), then stack."""

    def __init__(self, va):
        self.va = va
        self.nreg, self.nstack, self.pop, self.conv = 0, 0, None, "?"
        self.rd, self.wr = {}, {}                   # param -> {(offset|None, size)}
        self.drd, self.dwr = {}, {}                 # param -> sites: access through a pointer loaded from *param
        self.passed = {}                            # param -> [(site, target|None, pos, weak)]
        self.dpassed = {}                           # param -> [(site, target|None, pos)]   value loaded from *param
        self.stored = {}                            # param -> [(site, where)]
        self.returned, self.touched, self.lost, self.shared = set(), set(), {}, {}
        self.grd, self.gwr = {}, {}                 # global VA -> sites
        self.unk_rd, self.unk_wr = [], []           # sites: access through a pointer of unknown origin
        self.calls = []                             # (site, target|None, text, kind)  kind: unknown|tu|pure|self
        self.localargs = []                         # caller mode: (site, target|None, pos, local offset, callee param)
        self.warn = []
        self.bad_stack = False
        self.returns = None                         # True/False from --proto; None: unknown
        self.regs = None                            # custom register parameters (ltcgstub.py), else None

    def nparams(self):
        return self.nreg + self.nstack


class Analyzer:
    def __init__(self, exe, tu=None, depth=3, pops=None, pure=(), verbose=False):
        self.exe, self.tu, self.depth, self.pops, self.pure, self.verbose = exe, tu, depth, pops or {}, set(pure), verbose
        self.done, self.active = {}, set()

    def in_tu(self, va):
        return self.tu is not None and self.tu[0] <= va < self.tu[1]

    def summarize(self, va, conv=None, nparams=None, level=0, regs=None):
        key = (va, conv, nparams, tuple(regs or ()))
        if key in self.done:
            return self.done[key]
        if va in self.active:
            return None                                     # recursion: c2 has no summary for it yet either
        self.active.add(va)
        try:
            if getattr(self.exe, "x64", False):
                conv = "x64"                                # one convention; --conv / keywords do not apply
            if conv is None:
                pop, ecx, edx = self.exe.quick(va)
                conv = "fastcall" if (ecx and edx) else "thiscall" if ecx else "stdcall" if pop else "cdecl"
            f = Func(self, va, conv, nparams, level, regs)
            f.run()
            f.run()                                         # second run: the first found which slots are separate objects
            if f.s.bad_stack and f.guessed:                 # an indirect call's cleanup was guessed: try the alternatives
                saved, sites = dict(self.pops), sorted(f.guessed)
                for trial in [sites] + [[x] for x in sites]:
                    self.pops = dict(saved)
                    self.pops.update({x: 0 for x in trial})
                    g = Func(self, va, conv, nparams, level, regs)
                    g.run()
                    g.run()
                    if not g.s.bad_stack:
                        g.s.warn.append("indirect call(s) at %s taken as caller-cleanup (cdecl): the callee-cleanup guess "
                                        "left the stack unbalanced" % ", ".join("%08x" % x for x in trial))
                        f = g
                        break
                self.pops = saved
        finally:
            self.active.discard(va)
        f.s.func = f
        self.done[key] = f.s
        return f.s


class Func:
    def __init__(self, an, va, conv, nparams, level, regs=None):
        self.an, self.exe, self.va, self.conv, self.level = an, an.exe, va, conv, level
        self.x64 = x64 = bool(getattr(self.exe, "x64", False))
        self.FULL, self.GPR, self.CS = (FULL64, GPR64, CALLEE_SAVED64) if x64 else (FULL, GPR, CALLEE_SAVED)
        self.SP, self.BP, self.AX = ("rsp", "rbp", "rax") if x64 else ("esp", "ebp", "eax")
        self.W = 8 if x64 else 4                            # pointer / stack slot size
        if x64:
            regs = None
        self.regs = list(regs) if regs else None            # custom (/LTCG) register parameters, in order: ltcgstub.py
        self.s = Summary(va)
        self.s.conv = conv
        self.s.nreg = len(self.regs) if self.regs else {"thiscall": 1, "fastcall": 2}.get(conv, 0)
        self.s.regs = self.regs
        self.want_nstack = nparams
        self.direct = set()                                 # stack offsets accessed directly: object boundaries
        self.homes = set()                                  # local slots a parameter was reloaded from (spill homes)
        self.guessed = set()                                # indirect calls whose stack cleanup was guessed
        self.maxarg = 0
        self.tables = {}                                    # x64: register -> (site, switch table VA) of a table load

    # ---- values -------------------------------------------------------------------------------------------
    def reg(self, st, name):
        name = self.FULL.get(name)
        if name == self.SP:
            return ("sp", st.esp)
        return st.regs.get(name)

    def setreg(self, st, name, v):
        f = self.FULL.get(name)
        if f is None:
            return
        if f == self.SP:
            if v is not None and v[0] == "sp" and v[1] is not None:
                st.esp = v[1]
            else:
                self.warn("%s set to an untracked value" % self.SP)
            return
        st.regs[f] = v if name == f else None                # a write to al/ax makes eax unknown

    def warn(self, text, site=None):
        t = ("%08x: " % site if site else "") + text
        if t not in self.s.warn:
            self.s.warn.append(t)

    def lose(self, v, site, why):
        if v is not None and v[0] == "p":
            self.s.lost.setdefault(v[1], set()).add("%08x %s" % (site, why))
        elif v is not None and v[0] == "x":                 # x64: a value merged from a parameter, used here
            for k in v[1]:
                self.s.lost.setdefault(k, set()).add("%08x merged with another value, used here" % site)

    def xuse(self, v, site):
        """x64: a value merged from a parameter is used (dereferenced, stored, passed, returned, computed with)."""
        if v is not None and v[0] == "x":
            self.lose(v, site, "")

    def addr(self, st, ins, op):
        m = op.mem
        if m.segment in (X.X86_REG_FS, X.X86_REG_GS):
            return ("seg",)
        disp = m.disp
        if self.x64:
            if m.base == X.X86_REG_RIP:                     # [rip + disp]: a global
                return ("abs", (ins.address + ins.size + disp) & 0xffffffffffffffff)
            if not m.base and not m.index:
                return ("abs", disp & 0xffffffffffffffff)
            bv = self.reg(st, ins.reg_name(m.base)) if m.base else None
            self.xuse(bv, ins.address)
            if m.index:
                self.xuse(self.reg(st, ins.reg_name(m.index)), ins.address)
            if bv is not None and bv[0] == "g":             # base = an address taken with lea [rip+..] (__ImageBase)
                return ("abs", (bv[1] + disp) & 0xffffffffffffffff)
        if not m.base and not m.index:
            return ("abs", disp & 0xffffffff)
        bv = self.reg(st, ins.reg_name(m.base)) if m.base else None
        xv = self.reg(st, ins.reg_name(m.index)) if m.index else None
        if m.base and bv is not None and bv[0] in ("sp", "p", "d"):
            if m.index:
                self.lose(xv, ins.address, "used as an index")
            if bv[0] == "sp":
                return ("sp", bv[1] + disp if (bv[1] is not None and not m.index) else None)
            if bv[0] == "p":
                return ("p", bv[1], bv[2] + disp if (bv[2] is not None and not m.index) else None)
            return bv
        if m.index and m.scale == 1 and xv is not None and xv[0] in ("sp", "p", "d"):
            return (xv[0], xv[1], None) if xv[0] == "p" else ("sp", None) if xv[0] == "sp" else xv
        if m.index:
            self.lose(xv, ins.address, "scaled")
        if self.exe.in_image(disp & 0xffffffff):
            return ("abs", disp & 0xffffffff)               # table[reg]
        return None

    def load(self, st, a, size, site):
        s = self.s
        if a is None:
            s.unk_rd.append(site)
            return None
        k = a[0]
        if k == "sp":
            if a[1] is None:
                return None
            self.direct.add(a[1])
            if a[1] >= (8 + HOME64 if self.x64 else 4):
                self.maxarg = max(self.maxarg, a[1])
            v = st.slots.get(a[1]) if size == self.W else None
            if v is not None and v[0] == "p":
                s.touched.add(v[1])
                self.homes.add(a[1])
            return v
        if k == "p":
            s.rd.setdefault(a[1], set()).add((a[2], size))
            s.touched.add(a[1])
            return ("d", a[1], 1) if size == self.W else None
        if k == "d":
            s.drd.setdefault(a[1], set()).add(site)
            return ("d", a[1], min(a[2] + 1, MAXDEEP)) if size == self.W else None
        if k == "abs":
            s.grd.setdefault(a[1], set()).add(site)
        return None

    def escape(self, st, v, site, where):
        if v is None:
            return
        self.xuse(v, site)
        if v[0] == "p":
            self.s.stored.setdefault(v[1], []).append((site, where))
        elif v[0] == "d":
            self.s.dpassed.setdefault(v[1], []).append((site, None, "stored to " + where))
        elif v[0] == "sp" and v[1] is not None:
            st.esc = st.esc | {v[1]}

    def store(self, st, a, v, size, site):
        s = self.s
        if a is None:
            s.unk_wr.append(site)
            self.escape(st, v, site, "memory of unknown origin")
            return
        k = a[0]
        if k == "sp":
            if a[1] is None:                                # indexed store into a local array
                for o in [o for o, w in st.slots.items() if o < 0 and not (w and w[0] == "init")]:
                    self.lose(st.slots[o], site, "local overwritten by an indexed store")
                    del st.slots[o]
                return
            self.direct.add(a[1])
            W = self.W
            for o in [o for o in range(a[1] - W + 1, a[1] + W) if o != a[1]]:
                if o in st.slots and (o > a[1] - W and o < a[1] + size):
                    del st.slots[o]
            if self.x64:
                st.aw = st.aw | {a[1] - st.esp}
            if size == W:
                st.slots[a[1]] = v
            else:
                st.slots.pop(a[1], None)
        elif k == "p":
            s.wr.setdefault(a[1], set()).add((a[2], size))
            s.touched.add(a[1])
            self.escape(st, v, site, "*param%d" % a[1])
        elif k == "d":
            s.dwr.setdefault(a[1], set()).add(site)
            self.escape(st, v, site, "memory reached through param%d" % a[1])
        elif k == "abs":
            s.gwr.setdefault(a[1], set()).add(site)
            self.escape(st, v, site, "global %08x" % a[1])

    # ---- one instruction ------------------------------------------------------------------------------------
    def opval(self, st, ins, op, site):
        if op.type == X.X86_OP_REG:
            name = ins.reg_name(op.reg)
            v = self.reg(st, name)
            if v is not None and v[0] == "p":
                self.s.touched.add(v[1])
            return v if self.FULL.get(name) == name else None
        if op.type == X.X86_OP_MEM:
            return self.load(st, self.addr(st, ins, op), op.size, site)
        if op.type == X.X86_OP_IMM and op.size == 4:
            return ("c", op.imm & 0xffffffff)
        if op.type == X.X86_OP_IMM and op.size == 8 and self.x64:
            return ("c", op.imm & 0xffffffffffffffff)
        return None

    def step(self, ins, st):
        """Apply ins to st in place; return the successor addresses."""
        if not self.x64:
            return self.step1(ins, st)
        ops = ins.operands
        if ins.id in (X.X86_INS_MOV, X.X86_INS_MOVSXD) and len(ops) == 2 and ops[0].type == X.X86_OP_REG and \
                ops[1].type == X.X86_OP_MEM and ops[1].size == 4 and ops[1].mem.index and ops[1].mem.scale == 4:
            bv = self.reg(st, ins.reg_name(ops[1].mem.base)) if ops[1].mem.base else None
            if bv == ("g", self.exe.base):                  # mov ecx, [__ImageBase + idx*4 + table]: a switch
                self.tables[self.FULL.get(ins.reg_name(ops[0].reg))] = (ins.address, self.exe.base + ops[1].mem.disp)
        if ins.id in (X.X86_INS_CALL, X.X86_INS_JMP, X.X86_INS_RET):
            return self.step1(ins, st)
        try:
            wr = ins.regs_access()[1]
        except capstone.CsError:
            wr = []
        out = self.step1(ins, st)
        aw = {self.FULL.get(ins.reg_name(r)) for r in wr} & set(ARGREGS64)
        if aw:
            st.aw = st.aw | aw
        return out

    def step1(self, ins, st):
        s, site, ops, nxt = self.s, ins.address, ins.operands, ins.address + ins.size
        i = ins.id
        if i == X.X86_INS_PUSH:
            v = self.opval(st, ins, ops[0], site)
            st.esp -= self.W
            st.slots[st.esp] = v
            st.pend = 0 if (v is not None and v[0] == "init" and v[1] in self.CS) else st.pend + 1
            return [nxt]
        if i == X.X86_INS_POP:
            v = st.slots.pop(st.esp, None)
            st.esp += self.W
            st.pend = max(st.pend - 1, 0)
            if ops[0].type == X.X86_OP_REG:
                self.setreg(st, ins.reg_name(ops[0].reg), v)
            else:
                self.store(st, self.addr(st, ins, ops[0]), v, self.W, site)
            return [nxt]
        if i in (X.X86_INS_MOV, X.X86_INS_MOVZX, X.X86_INS_MOVSX):
            if self.x64 and i == X.X86_INS_MOV and ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_IMM \
                    and ops[0].size in (4, 8) and self.FULL.get(ins.reg_name(ops[0].reg)) not in (None, self.SP):
                st.regs[self.FULL[ins.reg_name(ops[0].reg)]] = ("c", ops[1].imm & ((1 << 8 * ops[0].size) - 1))
                return [nxt]                                 # mov r32, imm zero-extends: an exact constant
            v = self.opval(st, ins, ops[1], site)
            if i != X.X86_INS_MOV or ops[1].size != self.W:
                if not (self.x64 and ops[1].type == X.X86_OP_REG and ops[1].size < 8):   # x64: a 32-bit value is
                    self.lose(self.reg(st, ins.reg_name(ops[1].reg)) if ops[1].type == X.X86_OP_REG else None,  # no address
                              site, "narrowed")
                v = None
            if ops[0].type == X.X86_OP_REG:
                self.setreg(st, ins.reg_name(ops[0].reg), v)
            else:
                self.store(st, self.addr(st, ins, ops[0]), v, ops[0].size, site)
            return [nxt]
        if i == X.X86_INS_LEA:
            a = self.addr(st, ins, ops[1])
            v = a if (a is not None and a[0] in ("sp", "p", "d")) else None
            if self.x64 and ops[1].mem.base == X.X86_REG_RIP and ops[0].size == 8:
                v = ("g", a[1])                              # lea r, [rip+..]: a global's address
            for r in (ops[1].mem.base, ops[1].mem.index):
                rv = self.reg(st, ins.reg_name(r)) if r else None
                if rv is not None and rv[0] == "p":
                    s.touched.add(rv[1])
            self.setreg(st, ins.reg_name(ops[0].reg), v)
            return [nxt]
        if i in (X.X86_INS_ADD, X.X86_INS_SUB) and ops[0].type == X.X86_OP_REG and self.FULL.get(ins.reg_name(ops[0].reg)) == ins.reg_name(ops[0].reg):
            name = ins.reg_name(ops[0].reg)
            d = self.reg(st, name)
            self.xuse(d, site)
            kc = None
            if self.x64 and name == self.SP and ops[1].type == X.X86_OP_REG:     # sub rsp, rax after __chkstk
                c = st.regs.get(self.FULL.get(ins.reg_name(ops[1].reg)))
                kc = c[1] if (c is not None and c[0] == "c") else None
            if ops[1].type == X.X86_OP_IMM or kc is not None:
                k = ops[1].imm if kc is None else kc
                k = k if i == X.X86_INS_ADD else -k
                if name == self.SP:
                    st.esp += k
                    st.pend = max(st.pend - k // 4, 0) if k > 0 else 0
                    for o in [o for o in st.slots if o < st.esp]:
                        del st.slots[o]
                    return [nxt]
                if d is not None and d[0] == "sp":
                    d = ("sp", d[1] + k if d[1] is not None else None)
                elif d is not None and d[0] == "p":
                    d = ("p", d[1], d[2] + k if d[2] is not None else None)
                elif d is not None and d[0] != "d":
                    d = None
                st.regs[name] = d
                return [nxt]
            v = self.opval(st, ins, ops[1], site)
            if name == self.SP:
                self.warn("%s changed by a variable amount (alloca?)" % self.SP, site)
                s.bad_stack = True
                return [nxt]
            if d is not None and d[0] in ("sp", "p", "d"):
                if v is not None and v[0] == d[0] and v[1] == d[1]:
                    d = None                                 # pointer difference
                else:
                    self.lose(v, site, "added to another pointer")
                    d = ("sp", None) if d[0] == "sp" else ("p", d[1], None) if d[0] == "p" else d
            elif i == X.X86_INS_ADD and v is not None and v[0] in ("sp", "p", "d"):
                d = ("sp", None) if v[0] == "sp" else ("p", v[1], None) if v[0] == "p" else v
            else:
                self.lose(v, site, "subtracted")
                d = None
            st.regs[name] = d
            return [nxt]
        if i in (X.X86_INS_INC, X.X86_INS_DEC) and ops[0].type == X.X86_OP_REG and ins.reg_name(ops[0].reg) in self.GPR:
            name = ins.reg_name(ops[0].reg)
            d, k = st.regs.get(name), (1 if i == X.X86_INS_INC else -1)
            self.xuse(d, site)
            if d is not None and d[0] == "sp":
                d = ("sp", d[1] + k if d[1] is not None else None)
            elif d is not None and d[0] == "p":
                d = ("p", d[1], d[2] + k if d[2] is not None else None)
            elif d is not None and d[0] != "d":
                d = None
            st.regs[name] = d
            return [nxt]
        if i == X.X86_INS_XCHG and ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_REG:
            a, b = ins.reg_name(ops[0].reg), ins.reg_name(ops[1].reg)
            va, vb = self.reg(st, a), self.reg(st, b)
            self.setreg(st, a, vb)
            self.setreg(st, b, va)
            return [nxt]
        if i == X.X86_INS_LEAVE:
            b = st.regs.get(self.BP)
            if b is None or b[0] != "sp" or b[1] is None:
                self.warn("leave with an untracked %s" % self.BP, site)
                s.bad_stack = True
                return [nxt]
            st.esp = b[1]
            st.regs[self.BP] = st.slots.pop(st.esp, None)
            st.esp += self.W
            return [nxt]
        if i == X.X86_INS_CALL:
            self.call(ins, st, False)
            return [nxt]
        if i in (X.X86_INS_RET, X.X86_INS_RETF):
            self.ret(ins, st)
            return []
        if i == X.X86_INS_JMP:
            if ops[0].type == X.X86_OP_IMM:
                t = ops[0].imm
                if st.esp == 0 and t != self.va and (t < self.va or self.exe.read(t - 1, 1) == b"\xcc") \
                        and all(st.regs.get(r) == ("init", r) for r in self.CS):
                    self.call(ins, st, True)
                    return []
                return [t]
            tbl = self.switch64(ins, st) if self.x64 else self.switch(ins, st)
            if tbl is not None:
                return tbl
            if ops[0].type == X.X86_OP_MEM:
                self.load(st, self.addr(st, ins, ops[0]), self.W, site)
            self.call(ins, st, True)
            return []
        if ins.group(X.X86_GRP_JUMP):                        # jcc, loop, jecxz
            return [nxt, ops[0].imm] if ops and ops[0].type == X.X86_OP_IMM else [nxt]
        if i in (X.X86_INS_INT3, X.X86_INS_HLT, X.X86_INS_UD2):
            return []
        if i == X.X86_INS_AND and ops[0].type == X.X86_OP_REG and ins.reg_name(ops[0].reg) == self.SP:
            self.warn("stack realigned (and %s): local offsets after it are approximate" % self.SP, site)
            return [nxt]
        # generic: memory operands by access, then every written register becomes unknown
        cmov = ins.group(X.X86_GRP_CMOV)
        srcv = []
        for op in ops:
            if op.type == X.X86_OP_MEM:
                a = self.addr(st, ins, op)
                if i in FP_STORES:
                    self.store(st, a, None, op.size, site)
                    continue
                if not op.access:                            # no access info: a load (x87 arithmetic, compares)
                    srcv.append(self.load(st, a, op.size, site))
                    continue
                if op.access & capstone.CS_AC_READ:
                    srcv.append(self.load(st, a, op.size, site))
                if op.access & capstone.CS_AC_WRITE:
                    self.store(st, a, None, op.size, site)
            elif op.type == X.X86_OP_REG:
                v = self.reg(st, ins.reg_name(op.reg))
                if v is not None and v[0] == "p":
                    s.touched.add(v[1])
                if op.access & capstone.CS_AC_READ and not (self.x64 and op.size < 8):
                    srcv.append(v)
        rd, wr = ins.regs_access()
        compare = i in (X.X86_INS_CMP, X.X86_INS_TEST)
        for r in wr:
            name = self.FULL.get(ins.reg_name(r))
            if name in self.GPR:
                old = st.regs.get(name)
                if cmov and len(srcv) and srcv[-1] == old:
                    continue
                if old is not None and old[0] in ("p", "d") and i in (X.X86_INS_MOVSD, X.X86_INS_MOVSB, X.X86_INS_MOVSW,
                                                                    X.X86_INS_STOSD, X.X86_INS_STOSB, X.X86_INS_STOSW,
                                                                    X.X86_INS_LODSD, X.X86_INS_LODSB, X.X86_INS_CMPSB,
                                                                    X.X86_INS_SCASB, X.X86_INS_CMPSD, X.X86_INS_SCASD,
                                                                    X.X86_INS_MOVSQ, X.X86_INS_STOSQ, X.X86_INS_LODSQ,
                                                                    X.X86_INS_CMPSQ, X.X86_INS_SCASQ):
                    st.regs[name] = ("p", old[1], None) if old[0] == "p" else old
                    continue
                st.regs[name] = None
        if not compare and not cmov:
            for v in srcv:
                self.xuse(v, site)
                if v is not None and v[0] == "p" and not any(st.regs.get(r) is not None and st.regs[r][:2] == v[:2] for r in self.GPR) \
                        and not any(w is not None and w[:2] == v[:2] for w in st.slots.values()):
                    self.lose(v, site, "used by `%s`" % ins.mnemonic)
        return [nxt]

    def switch(self, ins, st):
        op = ins.operands[0]
        if op.type != X.X86_OP_MEM or not op.mem.index or op.mem.scale != 4 or op.mem.base:
            return None
        tbl = op.mem.disp & 0xffffffff
        if not self.exe.in_image(tbl):
            return None
        out = []
        for k in range(1024):
            t = self.exe.u32(tbl + 4 * k)
            if t is None or not self.exe.in_text(t) or not (self.va <= t < self.va + 0x8000):
                break
            if k and (tbl + 4 * k) in out:                   # ran into code that is a case target
                break
            out.append(t)
        return sorted(set(out)) if out else None

    def switch64(self, ins, st):
        """x64 `jmp reg` after `mov reg32, [__ImageBase + idx*4 + table]; add reg, __ImageBase`: the table holds
        image-relative offsets."""
        op = ins.operands[0]
        if op.type != X.X86_OP_REG:
            return None
        t = self.tables.get(self.FULL.get(ins.reg_name(op.reg)))
        if t is None or not (0 < ins.address - t[0] < 32):
            return None
        out = []
        for k in range(1024):
            r = self.exe.u32(t[1] + 4 * k)
            if r is None:
                break
            a = self.exe.base + r
            if not self.exe.in_text(a) or not (self.va <= a < self.va + 0x8000):
                break
            out.append(a)
        return sorted(set(out)) if out else None

    def ret(self, ins, st):
        s = self.s
        pop = ins.operands[0].imm if ins.operands else 0
        if s.pop is None:
            s.pop = pop
        if st.esp != 0:
            self.warn(self.SP + " is entry%+d at this ret: stack tracking failed (an indirect call's cleanup was guessed "
                      "wrong? use --pop VA=BYTES)" % st.esp, ins.address)
            s.bad_stack = True
        v = st.regs.get(self.AX)
        self.xuse(v, ins.address)
        if v is not None and v[0] == "p":
            s.returned.add(v[1])
        elif v is not None and v[0] == "d":
            s.dpassed.setdefault(v[1], []).append((ins.address, None, "returned"))

    def call(self, ins, st, tail):
        if self.x64:
            return self.call64(ins, st, tail)
        s, site, op = self.s, ins.address, ins.operands[0]
        target = op.imm if op.type == X.X86_OP_IMM else None
        text = "%08x" % target if target is not None else ins.op_str.replace("dword ptr ", "")
        if op.type == X.X86_OP_MEM and not op.mem.base and not op.mem.index:
            text = self.exe.imports.get(op.mem.disp & 0xffffffff, text)
        base = st.esp + (4 if tail else 0)
        if target is not None and self.exe.read(target, 6) == b"\x51\x3d\x00\x10\x00\x00":      # __chkstk / _alloca_probe
            v = st.regs.get("eax")
            if v is not None and v[0] == "c":
                st.esp -= v[1]
            else:
                self.warn("alloca of a variable size: stack tracking stops being exact", site)
                s.bad_stack = True
            st.regs["eax"] = None
            st.pend = 0
            return
        if target is not None and (self.exe.is_cookie_check(target) or target in self.an.pure):
            s.calls.append((site, target, text, "pure"))
            for r in ("eax", "ecx", "edx"):
                st.regs[r] = None
            return
        ecx_in = edx_in = False
        peek = self.exe.ins(site + ins.size)
        addesp = None
        if peek is not None and peek.id == X.X86_INS_ADD and peek.operands[0].type == X.X86_OP_REG and \
                peek.reg_name(peek.operands[0].reg) == "esp" and peek.operands[1].type == X.X86_OP_IMM:
            addesp = peek.operands[1].imm
        if tail:                                            # the target sees our own argument area
            pop = 0
            if target is not None:
                q = self.exe.quick(target)
                ecx_in, edx_in = q[1], q[2]
                if q[0]:
                    n = q[0] // 4
                else:
                    sub = self.an.summarize(target, level=self.level + 1) if self.level <= self.an.depth + 1 else None
                    n = sub.nstack if sub is not None else 0
                    if sub is None:
                        self.warn("tail jump to %08x not followed: its arguments were not checked" % target, site)
                if s.pop is None:
                    s.pop = q[0] or 0
                self.maxarg = max(self.maxarg, 4 * n)
            else:
                ecx_in = True
                n = 24                                       # unknown: every incoming argument may be the target's
        elif target is not None:
            q = self.exe.quick(target)
            ecx_in, edx_in = q[1], q[2]
            n = q[0] // 4 if q[0] else st.pend              # cdecl: every push still outstanding (cleanup may be merged)
            pop = q[0] or 0
            if q[0] is None:                                # a thunk that ends in an indirect jump: cleanup unknown
                if site in self.an.pops:
                    pop = self.an.pops[site]
                elif addesp is None:
                    pop = 4 * n
                    self.guessed.add(site)
        else:
            ecx_in = True                                   # `this` of a virtual call; stale otherwise (reported weak)
            if op.type == X.X86_OP_MEM and not op.mem.base and not op.mem.index:
                imp = self.exe.imports.get(op.mem.disp & 0xffffffff)
                if imp is not None and not imp.split("!", 1)[-1].startswith("?"):
                    ecx_in = False                          # a C import (stdcall/cdecl) takes nothing in ecx: stale
            n = st.pend
            if site in self.an.pops:
                pop = self.an.pops[site]
            elif addesp is not None:
                pop = 0
            else:
                pop = 4 * n
                self.guessed.add(site)
        args = [("stack", k, st.slots.get(base + 4 * k)) for k in range(n)]
        nreg = (1 if ecx_in else 0) + (1 if (ecx_in and edx_in) else 0)
        regargs = ([("ecx", 0, st.regs.get("ecx"))] if ecx_in else []) + ([("edx", 1, st.regs.get("edx"))] if ecx_in and edx_in else [])
        inner = None
        kind = "unknown"
        if target == self.va:
            kind = "self"
        elif target is not None and self.an.in_tu(target) and self.level < self.an.depth:
            inner = self.an.summarize(target, level=self.level + 1)
            kind = "tu" if inner is not None else "self"
        s.calls.append((site, target, text, kind))
        for where, k, v in regargs + args:
            pos = k if where != "stack" else nreg + k
            weak = where == "ecx" and target is None
            if v is None:
                continue
            if v[0] == "sp" and v[1] is not None:
                s.localargs.append((site, target, where if where != "stack" else "arg%d" % k, v[1], pos))
                st.esc = st.esc | {v[1]}
            elif v[0] == "p":
                s.touched.add(v[1])
                if inner is not None:
                    self.merge_inner(inner, pos, v[1], 0, site, target)
                elif kind != "self":
                    s.passed.setdefault(v[1], []).append((site, target, pos, weak))
            elif v[0] == "d":
                if inner is not None:
                    self.merge_inner(inner, pos, v[1], v[2], site, target)
                elif kind != "self":
                    s.dpassed.setdefault(v[1], []).append((site, target, "passed"))
        self.inner_effects(inner, site, target, text)        # the inner function's own effects are ours too
        self.clobber_escaped(st, site, target, kind)
        for r in ("eax", "ecx", "edx"):
            st.regs[r] = None
        if not tail:
            st.esp += pop
            st.pend = max(st.pend - pop // 4, 0)
            for o in [o for o in st.slots if o < st.esp]:
                del st.slots[o]
        elif st.esp != 0:
            self.warn("tail jump with esp at entry%+d" % st.esp, site)
            s.bad_stack = True

    def call64(self, ins, st, tail):
        """x64 call or tail jump: arguments in rcx, rdx, r8, r9 and the stack slots above the callee's home area."""
        s, site, op = self.s, ins.address, ins.operands[0]
        target = op.imm if op.type == X.X86_OP_IMM else None
        text = "%08x" % target if target is not None else ins.op_str.replace("qword ptr ", "")
        if op.type == X.X86_OP_MEM:
            a = self.addr(st, ins, op)
            if a is not None and a[0] == "abs" and (op.mem.base == X.X86_REG_RIP or not (op.mem.base or op.mem.index)):
                text = self.exe.imports.get(a[1], "[%08x]" % a[1])
        if target is not None and self.exe.is_chkstk64(target):
            for r in ("r10", "r11"):
                st.regs[r] = None
            return
        if target is not None and (self.exe.is_cookie_check(target) or target in self.an.pure):
            s.calls.append((site, target, text, "pure"))
            for r in CALLER_SAVED64:
                st.regs[r] = None
            st.aw = frozenset()
            return
        q = self.exe.quick64(target) if target is not None else None
        reliable = q is not None and q[2]
        if reliable:                                        # the registers and stack slots the target reads
            nreg = max(q[0]) + 1 if q[0] else 0
            nstk = q[1]
        else:                                               # unknown: every register; the slots written since
            nreg, nstk = 4, 0                               # the last call
            while ((8 if tail else 0) + HOME64 + 8 * nstk) in st.aw or \
                    (tail and (st.slots.get(8 + HOME64 + 8 * nstk) or ("?",))[0] == "p"):
                nstk += 1
                if nstk > 24:
                    break
        if tail and target is None and not reliable:
            nstk = 24                                       # an indirect tail jump sees all incoming arguments
        if tail and s.pop is None:
            s.pop = 0                                       # a tail jump is an exit (ret has no pop on x64)
        base = st.esp + (8 if tail else 0) + HOME64
        regargs = [(ARGREGS64[k], k, st.regs.get(ARGREGS64[k])) for k in range(min(nreg, 4))]
        args = [("stack", k, st.slots.get(base + 8 * k)) for k in range(nstk)]
        inner = None
        kind = "unknown"
        if target == self.va:
            kind = "self"
        elif target is not None and self.an.in_tu(target) and self.level < self.an.depth:
            inner = self.an.summarize(target, level=self.level + 1)
            kind = "tu" if inner is not None else "self"
        s.calls.append((site, target, text, kind))
        for where, k, v in regargs + args:
            pos = k if where != "stack" else 4 + k
            weak = where != "stack" and not reliable and where not in st.aw
            if v is None:
                continue
            self.xuse(v, site)
            if v[0] == "sp" and v[1] is not None:
                s.localargs.append((site, target, where if where != "stack" else "arg%d" % (4 + k), v[1], pos))
                st.esc = st.esc | {v[1]}
            elif v[0] == "p":
                s.touched.add(v[1])
                if inner is not None:
                    self.merge_inner(inner, pos, v[1], 0, site, target)
                elif kind != "self":
                    s.passed.setdefault(v[1], []).append((site, target, pos, weak))
            elif v[0] == "d":
                if inner is not None:
                    self.merge_inner(inner, pos, v[1], v[2], site, target)
                elif kind != "self":
                    s.dpassed.setdefault(v[1], []).append((site, target, "passed"))
        self.inner_effects(inner, site, target, text)
        self.clobber_escaped(st, site, target, kind)
        for r in CALLER_SAVED64:
            st.regs[r] = None
        st.aw = frozenset()
        if not tail:                                        # the callee owns its home area and stack arguments
            for o in [o for o in st.slots if st.esp <= o < base + 8 * max(nstk, 0)]:
                del st.slots[o]
        elif st.esp != 0:
            self.warn("tail jump with rsp at entry%+d" % st.esp, site)
            s.bad_stack = True

    def inner_effects(self, inner, site, target, text):
        """The effects of a summarised (--tu) callee on globals and unknown memory are the caller's too."""
        s = self.s
        if inner is None:
            return
        for a, sites in inner.grd.items():
            s.grd.setdefault(a, set()).add(site)
        for a, sites in inner.gwr.items():
            s.gwr.setdefault(a, set()).add(site)
        if inner.unk_rd:
            s.unk_rd.append(site)
        if inner.unk_wr:
            s.unk_wr.append(site)
        for c in inner.calls:
            if c[3] == "unknown":
                s.calls.append((site, c[1], c[2] + " (via %08x)" % target, "unknown"))
        if inner.bad_stack:
            self.warn("callee %08x could not be analysed reliably" % target, site)
            s.calls.append((site, target, text + " (unreliable)", "unknown"))

    def clobber_escaped(self, st, site, target, kind):
        """Locals whose address a callee holds may be rewritten by any call."""
        s = self.s
        if not st.esc:
            return
        bounds = sorted(self.direct | {0})
        for a in st.esc:
            hi = next((b for b in bounds if b > a), a + self.W)
            for o in [o for o in st.slots if a <= o < hi]:
                v = st.slots[o]
                if v is not None and v[0] == "p":
                    if o in self.homes:                  # stack packing: a dead spill slot reused for a local
                        s.shared.setdefault(v[1], set()).add(site)
                    elif kind == "unknown":
                        s.passed.setdefault(v[1], []).append((site, target, -1, False))
                    self.lose(v, site, "kept in a local whose address a callee has")
                elif v is not None and v[0] == "d" and kind == "unknown":
                    s.dpassed.setdefault(v[1], []).append((site, target, "inside a local object"))
                else:
                    self.xuse(v, site)
                if not (v is not None and v[0] == "init"):
                    del st.slots[o]

    def merge_inner(self, inner, pos, i, deep, site, target):
        s = self.s
        if pos >= inner.nparams() and inner.conv == "cdecl" and pos < 16:
            pass                                             # an argument the callee never looks at
        if deep == 0:
            if inner.rd.get(pos):
                s.rd.setdefault(i, set()).add((None, 0))
            if inner.wr.get(pos):
                s.wr.setdefault(i, set()).add((None, 0))
            if inner.drd.get(pos) or [x for x in inner.dpassed.get(pos, []) if x[2].startswith(("passed", "inside"))]:
                s.drd.setdefault(i, set()).add(site)
            if inner.dwr.get(pos):
                s.dwr.setdefault(i, set()).add(site)
            for x in inner.passed.get(pos, []):
                s.passed.setdefault(i, []).append((site, x[1], x[2], x[3]))
            for x in inner.stored.get(pos, []):
                s.stored.setdefault(i, []).append((site, x[1] + " (in %08x)" % target))
            if pos in inner.lost:
                s.lost.setdefault(i, set()).add("%08x lost inside %08x" % (site, target))
        else:
            if inner.rd.get(pos) or inner.drd.get(pos):
                s.drd.setdefault(i, set()).add(site)
            if inner.wr.get(pos) or inner.dwr.get(pos):
                s.dwr.setdefault(i, set()).add(site)
            if inner.passed.get(pos) or inner.stored.get(pos) or inner.dpassed.get(pos):
                s.dpassed.setdefault(i, []).append((site, target, "passed on"))

    def prologue_end(self):
        """First address after the entry's frame set-up (register saves, EH registration, /GS cookie): pushes
        up to there are not call arguments."""
        a, end = self.va, self.va
        if self.x64:
            return end                                      # x64 passes no argument with push
        for _ in range(24):
            i = self.exe.ins(a)
            if i is None:
                break
            ops = i.operands
            nx = self.exe.ins(a + i.size)
            setup = False
            if i.id == X.X86_INS_PUSH:                       # register saves and slots; `push -1; push handler` of EH
                if ops[0].type == X.X86_OP_REG:
                    setup = True
                elif ops[0].type == X.X86_OP_IMM and ops[0].imm == -1 and nx is not None and nx.id == X.X86_INS_PUSH \
                        and nx.operands[0].type == X.X86_OP_IMM and self.exe.in_text(nx.operands[0].imm & 0xffffffff):
                    setup = True
                elif ops[0].type == X.X86_OP_IMM and self.exe.in_text(ops[0].imm & 0xffffffff) and a > self.va:
                    pv = self.exe.read(a - 2, 2)
                    setup = pv == b"\x6a\xff"
            elif i.id in (X.X86_INS_SUB, X.X86_INS_AND) and ops[0].type == X.X86_OP_REG and i.reg_name(ops[0].reg) == "esp":
                setup = True
            elif i.id == X.X86_INS_MOV and ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_REG and \
                    {i.reg_name(ops[0].reg), i.reg_name(ops[1].reg)} == {"ebp", "esp"}:
                setup = True
            elif i.id == X.X86_INS_MOV and any(o.type == X.X86_OP_MEM and o.mem.segment == X.X86_REG_FS for o in ops):
                setup = True
            elif i.id == X.X86_INS_MOV and ops[0].type == X.X86_OP_REG and i.reg_name(ops[0].reg) == "eax" and \
                    ops[1].type == X.X86_OP_MEM and not ops[1].mem.base and not ops[1].mem.index:
                setup = nx is not None and nx.id == X.X86_INS_XOR    # mov eax, [___security_cookie]; xor eax, esp
            elif i.id == X.X86_INS_XOR and ops[1].type == X.X86_OP_REG and i.reg_name(ops[1].reg) in ("esp", "ebp"):
                setup = True
            elif i.id == X.X86_INS_LEA and i.reg_name(ops[0].reg) == "eax" and i.reg_name(ops[1].mem.base) in ("esp", "ebp"):
                setup = nx is not None and nx.id == X.X86_INS_MOV and any(
                    o.type == X.X86_OP_MEM and o.mem.segment == X.X86_REG_FS for o in nx.operands)
            if not setup:
                break
            a += i.size
            if i.id != X.X86_INS_LEA:
                end = a
        return end

    # ---- fixpoint -----------------------------------------------------------------------------------------
    def entry_state(self):
        st = State()
        if self.x64:                                        # rcx rdx r8 r9 = params 0..3, [rsp+0x28+8k] = 4+k
            for r in GPR64:
                st.regs[r] = ("init", r)
            for k, r in enumerate(ARGREGS64):
                st.regs[r] = ("p", k, 0)
            for k in range((self.want_nstack - 4) if self.want_nstack is not None else 20):
                st.slots[8 + HOME64 + 8 * k] = ("p", 4 + k, 0)
            return st
        for r in GPR:
            st.regs[r] = ("init", r)
        nreg = self.s.nreg
        if self.regs:
            for k, r in enumerate(self.regs):
                st.regs[r] = ("p", k, 0)
        elif nreg >= 1:
            st.regs["ecx"] = ("p", 0, 0)
        if nreg >= 2 and not self.regs:
            st.regs["edx"] = ("p", 1, 0)
        for k in range(self.want_nstack if self.want_nstack is not None else 24):
            st.slots[4 + 4 * k] = ("p", nreg + k, 0)
        return st

    def join(self, a, b, site):
        """Join b into a; True when a changed."""
        ch = False
        if a.esp != b.esp:
            if not self.s.bad_stack:
                self.warn("paths reach here with different stack depths (%d, %d)" % (a.esp, b.esp), site)
            self.s.bad_stack = True
        if b.pend != a.pend:
            m = min(a.pend, b.pend)
            ch |= m != a.pend
            a.pend = m
        if not b.esc <= a.esc:
            a.esc = a.esc | b.esc
            ch = True
        if not b.aw <= a.aw:
            a.aw = a.aw | b.aw
            ch = True
        if self.x64:
            return self.join64(a, b, site) or ch
        for d, e in ((a.regs, b.regs), (a.slots, b.slots)):
            for k in list(d):
                v, w = d[k], e.get(k)
                if v == w or v is None:
                    if v is None and d is a.slots:
                        pass
                    continue
                if w is not None and v[0] == w[0] == "p" and v[1] == w[1]:
                    n = ("p", v[1], None)
                elif w is not None and v[0] == w[0] == "sp":
                    n = ("sp", None)
                elif w is not None and v[0] == w[0] == "d" and v[1] == w[1]:
                    n = ("d", v[1], max(v[2], w[2]))
                else:
                    n = None
                    self.lose(v, site, "merged with another value")
                    self.lose(w, site, "merged with another value")
                if n != v:
                    d[k] = n
                    ch = True
        return ch

    def join64(self, a, b, site):
        """x64 registers and slots: a parameter merged with another value becomes ("x", {params}), lost only
        where that value is used (lose() at the use), so a dead copy (rcx after a call on one path) costs nothing."""
        ch = False
        tags = lambda v: {v[1]} if (v is not None and v[0] == "p") else set(v[1]) if (v is not None and v[0] == "x") else set()
        for d, e in ((a.regs, b.regs), (a.slots, b.slots)):
            for k in set(d) | set(e):
                v, w = d.get(k), e.get(k)
                if v == w:
                    continue
                if v is not None and w is not None and v[0] == w[0] == "p" and v[1] == w[1]:
                    n = ("p", v[1], None)
                elif v is not None and w is not None and v[0] == w[0] == "sp":
                    n = ("sp", None)
                elif v is not None and w is not None and v[0] == w[0] == "d" and v[1] == w[1]:
                    n = ("d", v[1], max(v[2], w[2]))
                else:
                    t = tags(v) | tags(w)
                    n = ("x", frozenset(t)) if t else None
                if n != v and not (n is None and k not in d):
                    d[k] = n
                    ch = True
        return ch

    def run(self):
        s = self.s
        keep = (s.warn, s.bad_stack)
        self.s = s = Summary(self.va)
        s.conv, s.nreg = keep and self.conv, {"thiscall": 1, "fastcall": 2}.get(self.conv, 0)
        if self.regs:
            s.nreg, s.regs = len(self.regs), self.regs
        states = {self.va: self.entry_state()}
        prolog = self.prologue_end()
        work, steps = [self.va], 0
        while work:
            a = work.pop()
            st = states[a].copy()
            while True:
                steps += 1
                if steps > 400000:
                    self.warn("analysis did not settle")
                    s.bad_stack = True
                    work = []
                    break
                ins = self.exe.ins(a)
                if ins is None or not self.exe.in_text(a):
                    self.warn("undecodable code", a)
                    s.bad_stack = True
                    break
                succ = self.step(ins, st)
                if a < prolog:
                    st.pend = 0
                if len(succ) == 1 and succ[0] not in states:
                    a = succ[0]
                    states[a] = st.copy()
                    continue
                for t in succ:
                    if t not in states:
                        states[t] = st.copy()
                        work.append(t)
                    elif self.join(states[t], st, t):
                        work.append(t)
                break
        self.states = states
        if s.pop is None:
            s.pop = 0
            self.warn("no ret reached (the function ends in a jump to code that was not followed)")
        if self.x64:                                        # count: given, or the highest position read
            if self.want_nstack is not None:
                n = self.want_nstack
            else:
                q = self.exe.quick64(self.va)
                n = max([k + 1 for k in q[0]] + [4 + q[1] if q[1] else 0] +
                        [4 + (self.maxarg - 8 - HOME64) // 8 + 1 if self.maxarg else 0])
            s.nreg, s.nstack = min(n, 4), max(n - 4, 0)
        elif self.want_nstack is not None:
            s.nstack = self.want_nstack
        elif self.conv == "cdecl":
            s.nstack = self.maxarg // 4
        else:
            s.nstack = s.pop // 4
        n = s.nparams()
        for d in (s.rd, s.wr, s.drd, s.dwr, s.passed, s.dpassed, s.stored, s.lost, s.shared):
            for k in [k for k in d if k >= n]:
                del d[k]
        for d in (s.passed, s.dpassed, s.stored):
            for k in d:
                d[k] = sorted(set(d[k]), key=lambda x: tuple(str(y) for y in x))
        s.localargs = sorted(set(s.localargs), key=lambda x: (x[0], x[4]))
        seen, calls = set(), []
        for c in s.calls:
            if c not in seen:
                seen.add(c)
                calls.append(c)
        s.calls = sorted(calls, key=lambda c: c[0])
        s.unk_rd, s.unk_wr = sorted(set(s.unk_rd)), sorted(set(s.unk_wr))
        s.touched = {k for k in s.touched if k < n}
        s.returned = {k for k in s.returned if k < n}


# ---- reporting ------------------------------------------------------------------------------------------------
def offs(acc):
    o = sorted({a for a, _ in acc if a is not None})
    t = ",".join("%+d" % x for x in o[:6]) + (",.." if len(o) > 6 else "")
    if any(a is None for a, _ in acc):
        t = (t + "," if t else "") + "+var"
    return t


def verdicts(s, i):
    """(reads, writes, escapes, deep reads, deep writes) as 'yes' / 'no' / 'unknown' for parameter i."""
    lost = bool(s.lost.get(i))
    passed = bool(s.passed.get(i))
    rd = "yes" if (s.rd.get(i) or passed) else "unknown" if (lost and s.unk_rd) else "no"
    wr = "yes" if (s.wr.get(i) or passed) else "unknown" if (lost and s.unk_wr) else "no"
    esc = "yes" if (s.stored.get(i) or passed or (i in s.returned and s.returns)) else "unknown" if lost and s.unk_wr else "no"
    dp = [x for x in s.dpassed.get(i, []) if x[2].startswith(("passed", "inside"))]
    drd = "yes" if (s.drd.get(i) or dp or passed) else "no"
    dwr = "yes" if (s.dwr.get(i) or dp or passed) else "no"
    return rd, wr, esc, drd, dwr


def describe(s, i, verbose=False):
    rd, wr, esc, drd, dwr = verdicts(s, i)
    notes = []
    if s.rd.get(i):
        notes.append("loads [p%s]" % offs(s.rd[i]))
    if s.wr.get(i):
        notes.append("stores [p%s]" % offs(s.wr[i]))
    weaktext = " (still in its register since the entry or the last call: an argument, or a stale register)" \
        if s.conv == "x64" else " (in ecx: `this`, or a stale register)"
    for site, target, pos, weak in s.passed.get(i, [])[:4]:
        notes.append("%s to %s at %08x%s" % ("in a local object passed" if pos < 0 else "passed",
                                              "%08x" % target if target is not None else "an indirect call", site,
                                              weaktext if weak else ""))
    for site, where in s.stored.get(i, [])[:3]:
        notes.append("pointer stored to %s at %08x" % (where, site))
    if i in s.returned and s.returns is not False:
        notes.append("returned" if s.returns else "in %s at a ret: returned, if the function returns a value"
                     % ("rax" if s.conv == "x64" else "eax"))
    if s.drd.get(i) or s.dwr.get(i):
        notes.append("through the loaded pointer: %s" % "+".join(
            x for x, c in (("reads", s.drd.get(i)), ("writes", s.dwr.get(i))) if c))
    dp = s.dpassed.get(i, [])
    for kindtxt in sorted({x[2] for x in dp}):
        xs = [x for x in dp if x[2] == kindtxt]
        notes.append("a value loaded from it is %s%s (%d site(s), first %08x)" % (
            kindtxt, " to %08x" % xs[0][1] if xs[0][1] is not None else "", len(xs), xs[0][0]))
    for site in sorted(s.shared.get(i, []))[:1]:
        notes.append("its spill slot is reused for a local whose address is passed at %08x (ignored: the value is "
                     "dead there if the slot is shared; a real escape if the source stores it into that local)" % site)
    if verbose or "unknown" in (rd, wr, esc):
        for x in sorted(s.lost.get(i, []))[:4]:
            notes.append("flow lost: " + x)
    if i not in s.touched and not notes:
        cls = "unused"
    elif "unknown" in (rd, wr):
        cls = "UNKNOWN"
    elif rd == "no" and wr == "no":
        cls = "not dereferenced" + (", escapes" if esc == "yes" else "")
    elif esc == "yes" and not s.stored.get(i) and not s.passed.get(i):      # only reason: it is the return value
        cls = {("yes", "no"): "reads only", ("no", "yes"): "writes only", ("yes", "yes"): "reads+writes"}[(rd, wr)]
        cls += ", returned (not kept: it escapes only if the caller keeps the result)"
    else:
        cls = {("yes", "no"): "reads only", ("no", "yes"): "writes only", ("yes", "yes"): "reads+writes"}[(rd, wr)]
        if s.passed.get(i) and not s.rd.get(i) and not s.wr.get(i):
            cls = "handed to another function (c2: reads+writes+kept)"
        elif esc == "yes":
            cls += ", escapes"
    return cls, (rd, wr, esc, drd, dwr), notes


def show(v, names=None):
    if v is None:
        return "?"
    if v[0] == "p":
        n = names[v[1]][0] if names and v[1] in names else "param%d" % v[1]
        return n + ("" if v[2] == 0 else "+var" if v[2] is None else "%+d" % v[2])
    if v[0] == "d":
        n = names[v[1]][0] if names and v[1] in names else "param%d" % v[1]
        return "%s[%s]" % ("*" * v[2], n)
    if v[0] == "sp":
        return "&stack[%s]" % ("var" if v[1] is None else "%+d" % v[1])
    if v[0] == "init":
        return "entry " + v[1]
    if v[0] == "g":
        return "&global %08x" % v[1]
    return "0x%x" % v[1] if v[0] == "c" else "?"


def trace(exe, s, proto, out=sys.stdout):
    """Every instruction reached, with the stack depth and what each register operand holds."""
    f, names = s.func, param_names(s, proto)
    full, sp = f.FULL, f.SP
    for a in sorted(f.states):
        st, ins = f.states[a], exe.ins(a)
        if ins is None:
            continue
        notes = []
        for k, op in enumerate(ins.operands):
            if k == 0 and op.type == X.X86_OP_REG and ins.id in (X.X86_INS_MOV, X.X86_INS_LEA, X.X86_INS_MOVZX,
                                                                 X.X86_INS_MOVSX, X.X86_INS_POP):
                continue                                     # a register that is only written
            regs = [op.reg] if op.type == X.X86_OP_REG else [op.mem.base, op.mem.index] if op.type == X.X86_OP_MEM else []
            for r in regs:
                name = full.get(ins.reg_name(r)) if r else None
                v = st.regs.get(name) if name and name != sp else None
                if v is not None and v[0] in ("p", "d", "sp", "g"):
                    t = "%s=%s" % (name, show(v, names))
                    if t not in notes:
                        notes.append(t)
            if op.type == X.X86_OP_MEM and op.mem.base and ins.reg_name(op.mem.base) == sp and not op.mem.index:
                off = st.esp + op.mem.disp
                v = st.slots.get(off)
                notes.append("[%s%+d]=stack[%+d]%s" % (sp, op.mem.disp, off, " holds " + show(v, names) if v is not None else ""))
        out.write("%08x  %s%+-5d %-8s %-34s %s\n" % (a, sp, st.esp, ins.mnemonic, ins.op_str[:34], "; " + ", ".join(notes) if notes else ""))


def parse_proto(proto):
    """-> dict(ret, name, cls, conv, params=[(type, name, slots)], text)"""
    p = " ".join(proto.strip().rstrip(";").split())
    p = re.sub(r"__declspec\s*\(\s*\w+\s*\)\s*", "", p)
    m = re.match(r"^(.*?)([A-Za-z_~][\w:~]*)\s*\((.*)\)\s*(const)?\s*(throw\s*\(\s*\))?$", p)
    if not m:
        raise SystemExit("cannot parse --proto: " + proto)
    head, name, plist = m.group(1).strip(), m.group(2), m.group(3).strip()
    conv = None
    for c in ("thiscall", "stdcall", "fastcall", "cdecl"):
        if "__" + c in head:
            conv = c
            head = head.replace("__" + c, "").strip()
    static = bool(re.search(r"\bstatic\b", head))
    head = re.sub(r"\b(static|virtual|inline|extern)\b|__declspec\s*\(\s*\w+\s*\)|__forceinline", "", head).strip()
    cls = name.rsplit("::", 1)[0] if "::" in name else None
    if conv is None:
        conv = "thiscall" if (cls and not static) else "cdecl"
    params, depth, cur = [], 0, ""
    for ch in plist:
        if ch == "," and depth == 0:
            params.append(cur.strip())
            cur = ""
        else:
            depth += ch in "(<["
            depth -= ch in ")>]"
            cur += ch
    if cur.strip() and cur.strip() != "void":
        params.append(cur.strip())
    out = []
    for k, t in enumerate(params):
        if t == "...":
            continue
        t = t.split("=")[0].strip()
        mm = re.match(r"^(.*?[\s\*&])([A-Za-z_]\w*)$", t)
        if mm and mm.group(2) not in ("int", "char", "short", "long", "float", "double", "unsigned", "signed", "bool", "void", "const"):
            ty, nm = mm.group(1).strip(), mm.group(2)
        else:
            ty, nm = t, "a%d" % k
        ptr = "*" in ty or "&" in ty
        slots = 2 if (not ptr and re.search(r"\b(double|__int64|long long)\b", ty)) else 1
        out.append((ty, nm, slots))
    return dict(ret=head or "void", name=name, cls=cls, conv=conv, params=out, member=bool(cls and not static))


def param_names(s, proto):
    """name and declared type of every summary parameter index."""
    names = {}
    if s.conv == "x64":                                     # positions: rcx rdx r8 r9, then the stack
        allp = ([("this", (proto["cls"] or "?") + "*")] if proto and proto.get("member") else []) + \
            ([(nm, ty) for ty, nm, _ in proto["params"]] if proto else [])
        for i in range(max(s.nparams(), len(allp))):
            names[i] = allp[i] if i < len(allp) else (ARGREGS64[i] if i < 4 else "arg%d" % i, "?")
        return names
    if proto and getattr(s, "regs", None):                  # custom register convention: the first len(regs)
        allp = ([("this", (proto["cls"] or "?") + "*")] if proto["conv"] == "thiscall" else []) + \
            [(nm, ty) for ty, nm, _ in proto["params"]]     # parameters (this first) are the registers, in order
        stack = s.nreg
        for k, (nm, ty) in enumerate(allp):
            if k < s.nreg:
                names[k] = (nm, ty)
            else:
                names[stack] = (nm, ty)
                stack += 1
    elif proto:
        k = 0
        if proto["conv"] == "thiscall":
            names[0] = ("this", (proto["cls"] or "?") + "*")
            k = 1
        regs_left = 2 if proto["conv"] == "fastcall" else 0
        stack = s.nreg
        for ty, nm, slots in proto["params"]:
            if regs_left and slots == 1:
                names[2 - regs_left] = (nm, ty)
                regs_left -= 1
            else:
                names[stack] = (nm, ty)
                for extra in range(1, slots):
                    names[stack + extra] = (nm + " (high half)", ty)
                stack += slots
    for i in range(s.nparams()):
        if i not in names:
            reg = (s.regs[i] if s.regs else ("ecx", "edx")[i]) if i < s.nreg else None
            names[i] = (("this" if s.conv == "thiscall" else reg) if reg else "arg%d" % (i - s.nreg), "?")
    return names


def report(exe, s, proto, out=sys.stdout, verbose=False):
    names = param_names(s, proto)
    w = out.write
    if s.conv == "x64":
        w("callee %08x  x64, %d parameter(s) (%d in registers, %d on the stack)%s\n" % (
            s.va, s.nparams(), s.nreg, s.nstack, "  [%s]" % proto["name"] if proto else ""))
    else:
        w("callee %08x  %s, %d stack parameter(s), ret %d%s\n" % (
            s.va, s.conv, s.nstack, s.pop or 0, "  [%s]" % proto["name"] if proto else ""))
    if proto and s.conv == "x64":
        q = s.func.exe.quick64(s.va) if getattr(s, "func", None) else None
        if q is not None and q[0] and max(q[0]) + 1 > s.nparams():
            w("  WARNING: the code reads parameter register(s) %s but --proto has %d parameter(s): check the "
              "prototype (hidden return pointer? `this`?)\n" % (", ".join(ARGREGS64[k] for k in sorted(q[0]) if k >= s.nparams()),
                                                                s.nparams()))
    elif proto:
        want = sum(sl for _, _, sl in proto["params"]) - (min(2, len(proto["params"])) if proto["conv"] == "fastcall" else 0)
        if proto["conv"] != "cdecl" and want * 4 != (s.pop or 0):
            w("  WARNING: --proto has %d stack slot(s) but the code pops %d bytes: check the prototype "
              "(struct by value? hidden return pointer?)\n" % (want, s.pop or 0))
    w("  %-3s %-12s %-22s %-7s %-7s %-7s %s\n" % ("#", "name", "type", "reads", "writes", "escapes", "summary"))
    for i in range(s.nparams()):
        cls, (rd, wr, esc, drd, dwr), notes = describe(s, i, verbose)
        nm, ty = names[i]
        w("  %-3d %-12s %-22s %-7s %-7s %-7s %s\n" % (i, nm[:12], ty[:22], rd, wr, esc, cls))
        for n in notes:
            w("        - %s\n" % n)
    unknown = [c for c in s.calls if c[3] == "unknown"]
    w("  calls: %d (%d to code c2 has no summary for)\n" % (len(s.calls), len(unknown)))
    seen = set()
    for site, target, text, kind in s.calls:
        if (text, kind) in seen:
            continue
        seen.add((text, kind))
        w("        %08x  %-28s %s\n" % (site, text, {"unknown": "no summary: may read/write any global and anything that escaped",
                                                    "tu": "inside --tu: its summary is merged", "pure": "compiler helper",
                                                    "self": "recursive: no summary yet"}[kind]))
    if s.gwr or s.grd:
        w("  globals written: %s\n" % (" ".join("%08x" % a for a in sorted(s.gwr)) or "none"))
        w("  globals read:    %s\n" % (" ".join("%08x" % a for a in sorted(s.grd)[:24]) or "none") +
          (" .." if len(s.grd) > 24 else ""))
    if s.unk_wr or s.unk_rd:
        w("  through pointers of unknown origin (globals' contents, call results): %d store(s), %d load(s)\n"
          % (len(set(s.unk_wr)), len(set(s.unk_rd))))
    for t in s.warn:
        w("  WARNING: %s\n" % t)
    if s.bad_stack:
        w("  UNRELIABLE: stack tracking failed; every 'no' above may be wrong.\n")


def stub(exe, s, proto, out=sys.stdout):
    """A body whose c2 summary equals the table: U/D/escape per parameter, unknown calls, unknown-pointer access."""
    names = param_names(s, proto)
    w = out.write
    unknown_call = any(c[3] == "unknown" for c in s.calls)
    lines, notes, need = [], [], set()
    reads = False
    for i in range(s.nparams()):
        cls, (rd, wr, esc, drd, dwr), _ = describe(s, i)
        nm = names[i][0]
        if "high half" in nm:
            continue
        ty = names[i][1]
        if ty != "?" and "*" not in ty and "&" not in ty and not (s.rd.get(i) or s.wr.get(i) or s.passed.get(i)):
            continue                                         # a value parameter that is never dereferenced
        e = "&" + nm if (ty != "?" and "&" in ty and "*" not in ty.split("&")[-1]) else nm
        tag = "%s: %s" % (nm, cls)
        if "unknown" in (rd, wr):
            notes.append("%s is UNKNOWN (data flow lost): the stub treats it as read+written; also try it read-only" % nm)
            rd = wr = "yes"
        if s.passed.get(i) and not (s.rd.get(i) or s.wr.get(i) or s.stored.get(i)):
            need.add("pass")
            lines.append("    calleesum_pass((void*)%s);%s// %s" % (e, " " * max(1, 22 - len(e)), tag))
            continue
        if rd == "yes" and wr == "yes":
            lines.append("    *(char*)%s += 1;%s// %s" % (e, " " * max(1, 29 - len(e)), tag))
        elif rd == "yes":
            reads = True
            lines.append("    @V@ += *(char*)%s;%s// %s" % (e, " " * max(1, 28 - len(e)), tag))
        elif wr == "yes":
            lines.append("    *(char*)%s = (char)@V@;%s// %s" % (e, " " * max(1, 24 - len(e)), tag))
        if (drd == "yes" or dwr == "yes") and not unknown_call and not s.passed.get(i):
            if dwr == "yes":
                lines.append("    **(char**)%s = (char)@V@;%s// %s: writes memory reached through it" % (e, " " * max(1, 21 - len(e)), nm))
            else:
                reads = True
                lines.append("    @V@ += **(char**)%s;%s// %s: reads memory reached through it" % (e, " " * max(1, 26 - len(e)), nm))
        if s.passed.get(i):
            need.add("pass")
            lines.append("    calleesum_pass((void*)%s);%s// %s: also handed to another function" % (e, " " * max(1, 22 - len(e)), nm))
        elif s.stored.get(i):
            need.add("keep")
            lines.append("    calleesum_keep = (void*)%s;%s// %s: the pointer is stored (escapes)" % (e, " " * max(1, 20 - len(e)), nm))
    ret = proto["ret"] if proto else "void"
    retv = None
    if unknown_call:
        need.add("ext")
        lines.append("    calleesum_ext(@V@);                // calls code c2 has no summary for")
    else:
        if s.unk_wr:
            need.add("keep")
            lines.append("    *(char*)calleesum_keep = (char)@V@;  // stores through a pointer of unknown origin")
        if s.unk_rd:
            need.add("keep")
            reads = True
            lines.append("    @V@ += *(char*)calleesum_keep;     // loads through a pointer of unknown origin")
        if s.gwr or s.grd:
            notes.append("no unknown call: c2 has the exact lists of globals this callee writes (%s) and reads (%s). "
                         "If the caller uses one of them around the call, add the same access to the stub."
                         % (" ".join("%08x" % a for a in sorted(s.gwr)) or "none",
                            " ".join("%08x" % a for a in sorted(s.grd)[:8]) or "none"))
        wrote = any("= (char)@V@" in l or "+= 1" in l for l in lines)
        if ret != "void" and re.fullmatch(r"(unsigned |signed )?(int|long|short|char|bool|float|double)|.*\*", ret):
            retv = "(%s)@V@" % ret if "*" not in ret else "(%s)(void*)@V@" % ret
        elif reads and not wrote:
            need.add("sink")
            lines.append("    calleesum_sink = @V@;              // keeps the reads alive (adds one global write)")
    if retv is None and ret != "void":
        retv = "(%s)@V@" % ret if re.fullmatch(r"(unsigned |signed )?(int|long|short|char|bool|float|double)", ret) else \
            "(%s)(void*)@V@" % ret if "*" in ret else "calleesum_ret<%s >()" % ret
        if "calleesum_ret" in retv:
            need.add("ret")
    for i in sorted(s.returned if ret != "void" else []):
        notes.append("%s is returned by the real callee (c2 then treats the result as that pointer)" % names[i][0])
        if "*" in ret:
            retv = "(%s)%s" % (ret, names[i][0])
        elif ret.rstrip().endswith("&"):                    # `T& f()` returning *this or *p (operator= shape)
            retv = "*(%s*)(void*)%s" % (ret.rstrip()[:-1].replace("const ", "").strip(), names[i][0])
            need.discard("ret")
    w("// ---- calleesum stub for %08x: same read/write/escape pattern, not a decompilation ----\n" % s.va)
    for n in notes:
        w("// NOTE: %s\n" % n)
    if s.bad_stack:
        w("// UNRELIABLE: the analysis lost track of the stack; check the table by hand.\n")
    decl = {"ext": "void calleesum_ext(int);", "pass": "void calleesum_pass(void*);", "keep": "extern void* calleesum_keep;",
            "sink": "extern int calleesum_sink;", "ret": "template <class T> T calleesum_ret();"}
    for k in ("ext", "pass", "keep", "sink", "ret"):
        if k in need:
            w(decl[k] + "\n")
    if proto and s.conv == "x64":                           # one convention: no keyword
        sig = "%s %s(%s)" % (proto["ret"], proto["name"], ", ".join("%s %s" % (t, n) for t, n, _ in proto["params"]))
    elif s.conv == "x64":
        sig = "void FUN_%08x(%s)" % (s.va, ", ".join("void* %s" % names[i][0] for i in range(s.nparams())))
    elif proto:
        conv = " __%s" % proto["conv"] if proto["conv"] in ("stdcall", "fastcall", "cdecl") and not proto["cls"] and proto["conv"] != "cdecl" else ""
        sig = "%s%s %s(%s)" % (proto["ret"], conv, proto["name"], ", ".join("%s %s" % (t, n) for t, n, _ in proto["params"]))
    else:
        conv = {"stdcall": " __stdcall", "fastcall": " __fastcall"}.get(s.conv, "")
        ps = ["void* %s" % names[i][0] for i in range(1 if s.conv == "thiscall" else 0, s.nparams())]
        sig = "void%s %sFUN_%08x(%s)" % (conv, "CLASS::" if s.conv == "thiscall" else "", s.va, ", ".join(ps))
    acc = "cs"
    while acc in {names[i][0] for i in names}:
        acc += "_"
    w("__declspec(noinline) %s\n{\n    int %s = 0;\n" % (sig, acc))
    for l in lines:
        code, _, com = l.partition("//")
        w(("%-44s// %s" % (code.rstrip().replace("@V@", acc), com.strip())).rstrip() + "\n")
    if retv:
        w("    return %s;\n" % retv.replace("@V@", acc))
    w("}\n")


def caller_mode(exe, an, va, out=sys.stdout):
    s = an.summarize(va)
    w = out.write
    if s.conv == "x64":
        w("caller %08x  x64\n" % va)
    else:
        w("caller %08x  %s, ret %d\n" % (va, s.conv, s.pop or 0))
    for t in s.warn:
        w("  WARNING: %s\n" % t)
    if s.bad_stack:
        w("  UNRELIABLE: stack tracking failed in the caller.\n")
    bysite = {}
    for site, target, where, off, pos in s.localargs:
        bysite.setdefault((site, target), []).append((where, off, pos))
    if not bysite:
        w("  no call receives the address of a local: the callee-summary rule cannot change this function's locals\n"
          "  (it can still matter for globals and for memory the caller reads through pointers around a call).\n")
    texts = {c[0]: c[2] for c in s.calls}
    for (site, target), lst in sorted(bysite.items()):
        w("  %08x  call %s\n" % (site, texts.get(site, "?")))
        inner = None
        if target is None:
            w("        indirect: c2 never has a summary for it (every argument counts as read+written+kept)\n")
        else:
            sub = Analyzer(exe, tu=an.tu, depth=an.depth, pops=an.pops, pure=an.pure)
            inner = sub.summarize(target)
        for where, off, pos in sorted(lst, key=lambda x: x[2]):
            t = "        %-5s = &%s[entry %s%+d]" % (where, "local" if off < 0 else "param home",
                                                  "rsp" if s.conv == "x64" else "esp", off)
            if inner is not None and pos < inner.nparams():
                cls, v, _ = describe(inner, pos)
                t += "   callee #%d: %s" % (pos, cls)
            w(t + "\n")
        if inner is not None:
            if inner.bad_stack:
                w("        (callee analysis unreliable: run calleesum on %08x by itself)\n" % target)
            w("        -> if %08x is in the caller's translation unit, its body (or a stub: calleesum.py EXE %08x "
              "--proto ...) belongs in the unit\n" % (target, target))
    others = [c for c in s.calls if c[0] not in {k[0] for k in bysite} and c[3] != "pure"]
    if others:
        w("  other calls (no local's address passed): %s\n" % " ".join(sorted({c[2] for c in others}))[:400])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 2)[2])
    ap.add_argument("exe")
    ap.add_argument("va", nargs="?")
    ap.add_argument("--caller")
    ap.add_argument("--proto")
    ap.add_argument("--conv", choices=["thiscall", "stdcall", "cdecl", "fastcall"])
    ap.add_argument("--nparams", type=int)
    ap.add_argument("--tu")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--pop", action="append", default=[])
    ap.add_argument("--pure", default="")
    ap.add_argument("--no-stub", action="store_true")
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(a.exe):
        sys.exit("no such file: " + a.exe)
    exe = Exe(a.exe)
    tu = None
    if a.tu:
        lo, hi = a.tu.split("-")
        tu = (int(lo, 16), int(hi, 16))
    pops = {int(k, 16): int(v, 0) for k, v in (p.split("=") for p in a.pop)}
    pure = [int(x, 16) for x in a.pure.split(",") if x]
    an = Analyzer(exe, tu=tu, depth=a.depth, pops=pops, pure=pure, verbose=a.v)
    if a.caller:
        caller_mode(exe, an, int(a.caller, 16))
        return
    if not a.va:
        ap.error("give a callee VA or --caller VA")
    va = int(a.va, 16)
    proto = parse_proto(a.proto) if a.proto else None
    if exe.x64:
        if a.conv:
            print("note: --conv is ignored on x64 (one calling convention)", file=sys.stderr)
        if proto and re.search(r"__(thiscall|stdcall|fastcall|cdecl)\b", a.proto):
            print("note: calling-convention keywords in --proto are ignored on x64", file=sys.stderr)
        nparams = a.nparams
        if proto and nparams is None:
            nparams = len(proto["params"]) + (1 if proto["member"] else 0)
        s = an.summarize(va, None, nparams)
        s.returns = (proto["ret"] != "void") if proto else None
        if a.trace:
            trace(exe, s, proto)
            print()
        report(exe, s, proto, verbose=a.v)
        if not a.no_stub:
            print()
            stub(exe, s, proto)
        return
    conv = a.conv or (proto["conv"] if proto else None)
    nparams = a.nparams
    if proto and nparams is None:
        nparams = sum(sl for _, _, sl in proto["params"]) - (min(2, len(proto["params"])) if conv == "fastcall" else 0)
    s = an.summarize(va, conv, nparams)
    s.returns = (proto["ret"] != "void") if proto else None
    if a.trace:
        trace(exe, s, proto)
        print()
    report(exe, s, proto, verbose=a.v)
    if not a.no_stub:
        print()
        stub(exe, s, proto)


if __name__ == "__main__":
    main()
