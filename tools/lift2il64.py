#!/usr/bin/env python3
"""lift2il64: retail x64 machine code (MSVC, written against cl 17.00 / VS2012) -> c2-style IL -> C++ (il2c's
emitter) -> x64check (compile and compare with the executable).

The x64 front end of lift2il (docs/c2/lift2il.md); lift2il.py decodes 32-bit x86 only.

usage: lift2il64.py il VA --binary EXE [--size N]       print the lifted IL of one function
       lift2il64.py unit VA --binary EXE [--size N]     print the C++ unit
       lift2il64.py check VA --binary EXE [--flags F]   lift, emit, compile with C2_CL64 and compare (x64check.py)
       lift2il64.py bench --binary EXE [--sample N] [--max-size B] [--min-size B] [--seed S] [--jobs J] [--json OUT]
                                                       the same on a random sample of the function index

EXE can also come from LIFT64_BINARY. Function sizes come from --index FILE (LIFT64_INDEX; JSON
{"functions": [{"address": "0x...", "size": N, "flags": [["/O1", ...], ...]}]}, "flags" optional: the flag sets to
try for that function), else from the executable's .pdata (functions with unwind data; leaf functions without it need
--size or an index). --flags is one set, comma-separated; several sets are separated by ';' (default /O2,/GS-).

What differs from x86 (the IL dialect is the same: lift2il's tokens, so il2c's Emitter writes the body):
  - registers: the 16 64-bit GPRs, one web per value (a 32-bit write zero-extends: a full write); sizes give the
    token types (8 -> 1008, 4 -> 2004 (zero-extending), 2 -> 2002, 1 -> 2001);
  - arguments: positions 0..3 in rcx / rdx / r8 / r9 or xmm0..3, then stack slots [rsp+0x28+8k] at entry
    (`_aK`); a home slot [rsp+8+8k] at entry is argument k's memory;
  - a call's arity: the highest position whose register (or outgoing slot [rsp+0x20+8j]) the code writes for
    it; calls clobber rax rcx rdx r8-r11 xmm0-5; one calling convention;
  - frame: pushes of callee-saved registers, `sub rsp, N`, saves into the home area (`mov [rsp+8], rbx`) and
    their restores leave no tuples;
  - rip-relative operands are globals by address (`g_<va>`), which x64check resolves to the original address.
"""
import argparse, collections, json, os, random, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import capstone
from capstone import x86 as X

GPR = ("rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi",
       "r8", "r9", "r10", "r11", "r12", "r13", "r14", "r15")
XMM = tuple("xmm%d" % n for n in range(16))
ARGR = ("rcx", "rdx", "r8", "r9")
ARGX = ("xmm0", "xmm1", "xmm2", "xmm3")
CLOBBER = {"rax", "rcx", "rdx", "r8", "r9", "r10", "r11"} | {"xmm%d" % n for n in range(6)}
CALLEE_SAVED = {"rbx", "rbp", "rdi", "rsi", "r12", "r13", "r14", "r15"} | {"xmm%d" % n for n in range(6, 16)}
CANON = {r: r for r in GPR + XMM}
for _a, _b in (("eax", "rax"), ("ecx", "rcx"), ("edx", "rdx"), ("ebx", "rbx"), ("esp", "rsp"), ("ebp", "rbp"),
               ("esi", "rsi"), ("edi", "rdi"), ("ax", "rax"), ("cx", "rcx"), ("dx", "rdx"), ("bx", "rbx"),
               ("sp", "rsp"), ("bp", "rbp"), ("si", "rsi"), ("di", "rdi"), ("al", "rax"), ("cl", "rcx"),
               ("dl", "rdx"), ("bl", "rbx"), ("ah", "rax"), ("ch", "rcx"), ("dh", "rdx"), ("bh", "rbx"),
               ("spl", "rsp"), ("bpl", "rbp"), ("sil", "rsi"), ("dil", "rdi")):
    CANON[_a] = _b
for _k in range(8, 16):
    for _s in ("d", "w", "b"):
        CANON["r%d%s" % (_k, _s)] = "r%d" % _k
CCOF = {"je": "e", "jne": "ne", "jl": "l", "jle": "le", "jg": "g", "jge": "ge",
        "jb": "b", "jbe": "be", "ja": "a", "jae": "ae", "js": "l", "jns": "ge"}
ARITH = {"add": "add", "sub": "sub", "and": "and", "or": "or", "xor": "xor", "shl": "shl", "sal": "shl",
         "shr": "shr", "sar": "sar"}
RTY = {1: "2001", 2: "2002", 4: "2004", 8: "1008"}          # register operands
MTY = {1: "2001", 2: "2002", 4: "1004", 8: "1008"}          # memory operands
SSE_OPS = {"addss": ("add", "e004"), "subss": ("sub", "e004"), "mulss": ("OPMUL", "e004"), "divss": ("OPDIV", "e004"),
           "addsd": ("add", "e008"), "subsd": ("sub", "e008"), "mulsd": ("OPMUL", "e008"), "divsd": ("OPDIV", "e008")}
SSE_MOVES = {"movss": "e004", "movsd": "e008", "movaps": "e004", "movapd": "e008", "movups": "e004"}
SSE_CMP = ("comiss", "ucomiss", "comisd", "ucomisd")
# a comiss / ucomiss sets CF / ZF like an unsigned compare
FCC = {"jb": "l", "jbe": "le", "ja": "g", "jae": "ge", "je": "e", "jne": "ne"}

BINARY = os.environ.get("LIFT64_BINARY")
INDEX = os.environ.get("LIFT64_INDEX")
DEFAULT_FLAGS = [["/O2", "/GS-"]]

# packed SSE: mnemonic -> (intrinsic, form, kind read, kind written); kinds: f = __m128, i = __m128i.
# forms: bin d = f(d, s); bini d = f(d, s, imm); un d = f(s); uni d = f(s, imm); shi d = f(d, imm)
VT = {"f": "a016", "i": "b016"}
PACKED = {}
for _m, _f in (("mulps", "mul"), ("addps", "add"), ("subps", "sub"), ("divps", "div"), ("minps", "min"),
               ("maxps", "max"), ("andps", "and"), ("andnps", "andnot"), ("orps", "or"), ("xorps", "xor"),
               ("unpcklps", "unpacklo"), ("unpckhps", "unpackhi"), ("cmpeqps", "cmpeq"), ("cmpltps", "cmplt"),
               ("cmpleps", "cmple"), ("cmpneqps", "cmpneq"), ("cmpnltps", "cmpnlt"), ("cmpnleps", "cmpnle"),
               ("cmpordps", "cmpord"), ("cmpunordps", "cmpunord")):
    PACKED[_m] = ("_mm_%s_ps" % _f, "bin", "f", "f")
PACKED["movhlps"] = ("_mm_movehl_ps", "bin", "f", "f")
PACKED["movlhps"] = ("_mm_movelh_ps", "bin", "f", "f")
PACKED["shufps"] = ("_mm_shuffle_ps", "bini", "f", "f")
for _m, _f in (("sqrtps", "sqrt"), ("rsqrtps", "rsqrt"), ("rcpps", "rcp")):
    PACKED[_m] = ("_mm_%s_ps" % _f, "un", "f", "f")
PACKED["cvtdq2ps"] = ("_mm_cvtepi32_ps", "un", "i", "f")
PACKED["cvttps2dq"] = ("_mm_cvttps_epi32", "un", "f", "i")
PACKED["cvtps2dq"] = ("_mm_cvtps_epi32", "un", "f", "i")
for _m, _f in (("pand", "and_si128"), ("por", "or_si128"), ("pxor", "xor_si128"), ("pandn", "andnot_si128"),
               ("paddd", "add_epi32"), ("psubd", "sub_epi32"), ("paddw", "add_epi16"), ("psubw", "sub_epi16"),
               ("paddb", "add_epi8"), ("psubb", "sub_epi8"), ("paddq", "add_epi64"), ("psubq", "sub_epi64"),
               ("pcmpeqd", "cmpeq_epi32"), ("pcmpgtd", "cmpgt_epi32"), ("pcmpeqw", "cmpeq_epi16"),
               ("pcmpeqb", "cmpeq_epi8"), ("punpcklwd", "unpacklo_epi16"), ("punpckhwd", "unpackhi_epi16"),
               ("punpckldq", "unpacklo_epi32"), ("punpckhdq", "unpackhi_epi32"), ("punpcklbw", "unpacklo_epi8"),
               ("punpckhbw", "unpackhi_epi8"), ("punpcklqdq", "unpacklo_epi64"), ("punpckhqdq", "unpackhi_epi64"),
               ("packssdw", "packs_epi32"), ("packsswb", "packs_epi16"), ("packuswb", "packus_epi16"),
               ("pmullw", "mullo_epi16"), ("psadbw", "sad_epu8")):
    PACKED[_m] = ("_mm_" + _f, "bin", "i", "i")
PACKED["pshufd"] = ("_mm_shuffle_epi32", "uni", "i", "i")
PACKED["pshuflw"] = ("_mm_shufflelo_epi16", "uni", "i", "i")
PACKED["pshufhw"] = ("_mm_shufflehi_epi16", "uni", "i", "i")
for _m, _f in (("pslld", "slli_epi32"), ("psrld", "srli_epi32"), ("psrad", "srai_epi32"), ("psllq", "slli_epi64"),
               ("psrlq", "srli_epi64"), ("psllw", "slli_epi16"), ("psrlw", "srli_epi16"), ("psraw", "srai_epi16"),
               ("pslldq", "slli_si128"), ("psrldq", "srli_si128")):
    PACKED[_m] = ("_mm_" + _f, "shi", "i", "i")
# scalar ops on a value that is a vector elsewhere: the _ss intrinsics
SS_ON_VEC = {"addss": "_mm_add_ss", "subss": "_mm_sub_ss", "mulss": "_mm_mul_ss", "divss": "_mm_div_ss",
             "minss": "_mm_min_ss", "maxss": "_mm_max_ss"}
SCALAR_DEF = ("cvtsi2ss", "addss", "subss", "mulss", "divss", "cvtsd2ss", "minss", "maxss")
VEC_MOVES = ("movaps", "movups", "movdqa", "movdqu", "movss", "movd", "movq", "movmskps", "pinsrw", "pextrw",
             "sqrtss", "rsqrtss", "rcpss")
NEUTRAL = ("andps", "andnps", "orps", "xorps", "andpd", "xorpd", "pand", "pxor", "por")



def canon(n):
    return CANON.get(n, n)


_EH = None
RETVAR = False           # the return value as its own variable (see webs); opts["retvar"] overrides
MARKS = False            # lsearch64: each statement ends in //@<address of its instruction>


class Unsupported(Exception):
    pass


# ---------------------------------------------------------------------------------------------------- the image

class Image:
    def __init__(self, path=None):
        import pefile
        path = path or BINARY
        if not path:
            raise SystemExit("lift2il64: give the executable with --binary (or LIFT64_BINARY)")
        self.pe = pefile.PE(path, fast_load=True)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        self.md.detail = True
        self.data = self.pe.__data__
        self.secs = [(self.base + s.VirtualAddress, s.Misc_VirtualSize, s.PointerToRawData, s.SizeOfRawData)
                     for s in self.pe.sections]
        self.dir64 = set()
        d = self.pe.OPTIONAL_HEADER.DATA_DIRECTORY[5]
        if d.VirtualAddress and d.Size:
            import struct
            raw = self.pe.get_data(d.VirtualAddress, d.Size)
            k = 0
            while k + 8 <= len(raw):
                page, blk = struct.unpack_from("<II", raw, k)
                if blk < 8:
                    break
                for j in range(k + 8, k + blk, 2):
                    (e,) = struct.unpack_from("<H", raw, j)
                    if e >> 12 == 10:
                        self.dir64.add(self.base + page + (e & 0xFFF))
                k += blk

    def off(self, va):
        for v, vs, p, ps in self.secs:
            if v <= va < v + min(vs, ps):
                return p + va - v
        return None

    def read(self, va, n):
        o = self.off(va)
        return bytes(self.data[o:o + n]) if o is not None else b""

    def insns(self, va, size):
        return list(self.md.disasm(self.read(va, size), va))

    def tls_start(self):
        for sec in self.pe.sections:
            if sec.Name.rstrip(b"\0").startswith(b".tls"):
                return self.base + sec.VirtualAddress
        return None

    def is_code(self, va):
        s = self.pe.sections[0]
        return self.base + s.VirtualAddress <= va < self.base + s.VirtualAddress + s.Misc_VirtualSize


# --------------------------------------------------------------------------------------------------- the lifter

class Lifter:
    def __init__(self, img, va, size, opts=None):
        self.img, self.va, self.size = img, va, size
        self.opts = opts or {}
        self.md = img.md
        self.ins = img.insns(va, size)
        self.switches = {}
        if not self.ins:
            raise Unsupported("no code")
        for i in self.ins[:4]:                 # an unwind / catch funclet: rbp is the parent's frame (rdx)
            if i.mnemonic == "mov" and i.op_str == "rbp, rdx":
                raise Unsupported("funclet (rbp = the parent frame)")
        if len(self.ins) == 2 and self.ins[0].mnemonic in ("sub", "add") and self.ins[0].op_str.startswith("rcx, ") \
                and self.is_tailjmp(self.ins[1]):
            raise Unsupported("adjustor thunk (compiler-made)")
        end = self.ins[-1].address + self.ins[-1].size
        self.switches = {}
        self.find_switches()
        self.lines = []
        self.ntemp = 0
        self.args_read = {}                 # argument position -> type of its first read
        self.float_ret = None               # "e004" / "e008" when xmm0 carries the result
        self.ret_ty = None                  # "1008" / "1004" / "2001" when rax does
        self.value_ret = False
        self.calls = []                     # (index, arity, stack slots)
        if end - va < size and not self.switches:
            pass

    # ---- registers read / written
    def regs(self, i):
        r, w = i.regs_access()
        rn = {canon(self.md.reg_name(x)) for x in r}
        wn = {canon(self.md.reg_name(x)) for x in w}
        m = i.mnemonic
        if m in ("xor", "sub", "xorps", "xorpd", "pxor") and len(i.operands) == 2 and \
                i.operands[0].type == X.X86_OP_REG and i.operands[1].type == X.X86_OP_REG and \
                i.operands[0].reg == i.operands[1].reg:
            rn.discard(canon(self.md.reg_name(i.operands[0].reg)))
        if m.startswith("cmov") and i.operands and i.operands[0].type == X.X86_OP_REG:
            rn.add(canon(self.md.reg_name(i.operands[0].reg)))     # the value kept when the condition fails
        if m in ("cmp", "test", "bt", "comiss", "ucomiss", "comisd", "ucomisd", "push") or m.startswith("j"):
            wn = set()                       # (capstone lists the first operand of test / cmp as written)
        if m == "call":
            wn |= CLOBBER
        for op in i.operands:                # a partial write keeps the rest of the register
            if op.type == X.X86_OP_REG and op.access & capstone.CS_AC_WRITE:
                n = self.md.reg_name(op.reg)
                if (op.size < 4 and not n.startswith("xmm")) or (n.startswith("xmm") and m in ("movss", "movsd")
                                                                  and i.operands[1].type == X.X86_OP_REG):
                    rn.add(canon(n))
        keep = (set(GPR) - {"rsp", "rip"}) | set(XMM)
        return rn & keep, wn & keep

    def maybe_reads(self, k):
        """Registers an instruction may read as a call argument / return value: decided after the webs."""
        i = self.ins[k]
        if i.mnemonic == "call" or self.is_tailjmp(i):
            return set(ARGR) | set(ARGX)
        if self.is_ret(i):
            return {"rax", "xmm0"}
        return set()

    @staticmethod
    def is_ret(i):
        return i.mnemonic in ("ret", "repz ret", "rep ret")

    def is_tailjmp(self, i):
        if i.mnemonic != "jmp" or not i.operands:
            return False
        if i.operands[0].type == X.X86_OP_IMM:
            return not self.va <= i.operands[0].imm < self.va + self.size
        return i.address not in self.switches

    # ---- switches: `cmp r, N; ja D; lea rB, [__ImageBase]; [movzx r, byte [rB+r+BT];] mov r, [rB+r*4+T];
    #      add r, rB; jmp r`
    def find_switches(self):
        cut = None
        ins = self.ins
        for k, i in enumerate(ins):
            if i.mnemonic != "jmp" or not i.operands or i.operands[0].type != X.X86_OP_REG or k < 3:
                continue
            jr = i.operands[0].reg
            a = ins[k - 1]
            if a.mnemonic != "add" or a.operands[0].type != X.X86_OP_REG or a.operands[0].reg != jr:
                continue
            ld = ins[k - 2]
            if ld.mnemonic != "mov" or ld.operands[1].type != X.X86_OP_MEM or ld.operands[1].mem.scale != 4:
                continue
            T = self.img.base + (ld.operands[1].mem.disp & 0xFFFFFFFF)
            idx = canon(self.md.reg_name(ld.operands[1].mem.index))
            j = k - 3
            btab = None
            if ins[j].mnemonic == "movzx" and ins[j].operands[1].type == X.X86_OP_MEM:
                bm = ins[j].operands[1].mem
                btab = self.img.base + (bm.disp & 0xFFFFFFFF)
                idx = canon(self.md.reg_name(bm.index))
                j -= 1
            skip = {ins[k - 1].address, ins[k - 2].address}
            if btab is not None:
                skip.add(ins[k - 3].address)
            while j >= 0 and ins[j].mnemonic in ("lea", "movsxd", "mov") and not (
                    ins[j].mnemonic == "lea" and "rip" in ins[j].op_str):
                if ins[j].mnemonic == "lea":
                    break
                j -= 1
            if j >= 0 and ins[j].mnemonic == "lea" and "rip" in ins[j].op_str:
                skip.add(ins[j].address)
            q = next((q for q in range(k - 1, max(0, k - 10), -1) if ins[q].mnemonic == "ja"), None)
            if q is None or ins[q - 1].mnemonic != "cmp" or ins[q - 1].operands[1].type != X.X86_OP_IMM:
                continue
            n = ins[q - 1].operands[1].imm + 1
            default = ins[q].operands[0].imm
            if btab is not None:
                bvals = list(self.img.read(btab, n))
                ntab = max(bvals) + 1
                tgts_t = [self.img.base + int.from_bytes(self.img.read(T + 4 * b, 4), "little") for b in range(ntab)]
                tgts = [tgts_t[b] for b in bvals]
                lo = min(T, btab)
            else:
                tgts = [self.img.base + int.from_bytes(self.img.read(T + 4 * v, 4), "little") for v in range(n)]
                lo = T
            if any(not self.va <= t < self.va + self.size for t in tgts):
                continue
            self.switches[i.address] = dict(idx=canon(self.md.reg_name(ins[q - 1].operands[0].reg)),
                                            cases=list(enumerate(tgts)), default=default,
                                            cmp=ins[q - 1].address, ja=ins[q].address, skip=skip)
            if self.va <= lo < self.va + self.size:
                cut = lo if cut is None else min(cut, lo)
        if cut is not None:
            self.ins = [i for i in self.ins if i.address < cut]

    # ---- the CFG
    def blocks(self):
        addrs = [i.address for i in self.ins]
        aset = set(addrs)
        lead = {addrs[0]}
        for k, i in enumerate(self.ins):
            if i.mnemonic.startswith("j") or self.is_ret(i):
                if k + 1 < len(self.ins):
                    lead.add(addrs[k + 1])
                op = i.operands[0] if i.operands else None
                if op is not None and op.type == X.X86_OP_IMM and self.va <= op.imm < self.va + self.size:
                    if op.imm not in aset:
                        raise Unsupported("branch into an instruction")
                    lead.add(op.imm)
        for sw in self.switches.values():
            lead.update(t for _v, t in sw["cases"])
            lead.add(sw["default"])
        starts = sorted(a for a in lead if a in aset)
        idx = {a: k for k, a in enumerate(addrs)}
        bl = []
        for n, s in enumerate(starts):
            e = idx[starts[n + 1]] if n + 1 < len(starts) else len(self.ins)
            bl.append(list(range(idx[s], e)))
        succ = collections.defaultdict(list)
        bstart = {self.ins[b[0]].address: n for n, b in enumerate(bl)}
        for n, b in enumerate(bl):
            last = self.ins[b[-1]]
            m = last.mnemonic
            tgt = last.operands[0].imm if last.operands and last.operands[0].type == X.X86_OP_IMM else None
            if self.is_ret(last):
                continue
            if last.address in self.switches:
                sw = self.switches[last.address]
                for t in sorted({t for _v, t in sw["cases"]} | {sw["default"]}):
                    succ[n].append(bstart[t])
                continue
            if m.startswith("j"):
                if tgt in bstart:
                    succ[n].append(bstart[tgt])
                if m == "jmp":
                    continue
            if n + 1 < len(bl):
                succ[n].append(n + 1)
        self.bl, self.succ, self.bstart = bl, succ, bstart

    # ---- reaching definitions; webs after the call arities are known
    def reaching(self):
        self.acc = [self.regs(i) for i in self.ins]
        n = len(self.bl)
        gen = []
        for b in self.bl:
            g = {}
            for k in b:
                for r in self.acc[k][1]:
                    g[r] = {(k, r)}
            gen.append(g)
        entry = {r: {("entry", r)} for r in GPR + XMM if r != "rsp"}
        IN = [dict() for _ in range(n)]
        OUT = [None] * n
        preds = collections.defaultdict(list)
        for a, ss in self.succ.items():
            for s in ss:
                preds[s].append(a)
        changed = True
        while changed:
            changed = False
            for bn in range(n):
                inn = {r: set(ds) for r, ds in entry.items()} if bn == 0 else {}
                for p in preds[bn]:
                    for r, ds in (OUT[p] or {}).items():
                        inn.setdefault(r, set()).update(ds)
                out = {r: set(ds) for r, ds in inn.items()}
                out.update({r: set(ds) for r, ds in gen[bn].items()})
                if inn != IN[bn] or out != OUT[bn]:
                    IN[bn], OUT[bn], changed = inn, out, True
        self.reach_defs = {}
        for bn, b in enumerate(self.bl):
            cur = {r: set(ds) for r, ds in IN[bn].items()}
            for k in b:
                rd, wr = self.acc[k]
                for r in rd | self.maybe_reads(k):
                    self.reach_defs[(k, r)] = set(cur.get(r) or {("undef", r)})
                for r in wr:
                    cur[r] = {(k, r)}

    def explicit(self, k, r):
        """Is r's value at k written by this function for that use (not a call's clobber, not the caller's, not
        the TLS block lookup's scratch)?"""
        ds = self.reach_defs.get((k, r), set())
        skip = getattr(self, "tls_skip", set())
        return bool(ds) and all(d[0] not in ("entry", "undef") and self.ins[d[0]].mnemonic != "call" and
                                d[0] not in skip for d in ds)

    def entry_only(self, k, r):
        ds = self.reach_defs.get((k, r), set())
        return ds == {("entry", r)}

    def webs(self):
        parent = {}

        def find(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
        self.find = find
        self.rv_defs = set()
        rv = self.ret_reg == "rax" and self.opts.get("retvar", RETVAR)
        for (k, r), ds in self.reach_defs.items():
            if r in self.maybe_reads(k) and r not in self.acc[k][0] and (k, r) not in self.real_maybe:
                continue
            if rv and r == "rax" and k != "exit" and isinstance(k, int) and self.is_ret(self.ins[k]):
                # the return value: its own variable, set after each def that reaches a ret (one `return x;`
                # each), not one web through every value that ever sits in rax at a ret
                self.rv_defs |= {d for d in ds if d[0] not in ("entry", "undef")}
                for d in ds:
                    find(d)
                continue
            ds = sorted(ds, key=str)
            find(ds[0])
            for d in ds[1:]:
                union(ds[0], d)
        for k, (rd, wr) in enumerate(self.acc):
            for r in wr:
                find((k, r))
        # webs in address order of their first definition (then register name): the names, and so the
        # declarations, must not follow set iteration order (the hash seed)
        def order(d):
            return (d[0] if isinstance(d[0], int) else -1, str(d))
        first = {}
        for key in parent:
            root = find(key)
            if root not in first or order(key) < order(first[root]):
                first[root] = key
        names = {root: None for root in sorted(first, key=lambda r: order(first[r]))}
        for key in list(parent):
            root = find(key)
            if key[0] == "entry":
                p = ARGR.index(key[1]) if key[1] in ARGR else ARGX.index(key[1]) if key[1] in ARGX else None
                if p is not None:
                    names[root] = "_a%d" % p
        n = 0
        for root in names:
            if names[root] is None:
                n += 1
                names[root] = "t%d" % (1000 + n)
        self.names = names

    def var_read(self, k, r):
        if self.opts.get("zeroconst") and isinstance(k, int) and self.zero_reg(k, r):
            return "0"                          # a register holding 0 (xor r, r): the source's literal 0 (opt-in:
                                                # on five near-misses of a retail x64 game it changed nothing)
        ds = self.reach_defs.get((k, r))
        if not ds:
            return "_u_" + r
        return self.names.get(self.find(sorted(ds, key=str)[0]), "_u_" + r)

    def var_written(self, k, r):
        return self.names.get(self.find((k, r)), "_w_" + r)

    # ---- the frame
    def is_eh(self):
        """The executable's unwind data gives this function a C++ exception frame (a __CxxFrameHandler3 FuncInfo:
        ehinfo64.funcinfo_of)."""
        global _EH
        if _EH is None:
            _EH = {}
        if self.va not in _EH:
            try:
                import ehinfo64
                _EH[self.va] = ehinfo64.funcinfo_of(self.img, self.va) is not None
            except Exception:
                _EH[self.va] = False
        return _EH[self.va]

    def prologue(self):
        """Indices that leave no tuples: callee-saved pushes / pops, rsp adjustment, saves of callee-saved
        registers (and of the incoming arguments) into the frame and their restores. Returns the set; fills
        self.rsp_at (index -> rsp delta from the entry before it)."""
        out = set()
        self.r11_frame = self.ins[0].mnemonic == "mov" and self.ins[0].op_str == "r11, rsp"
        self.eh_help = None
        if self.r11_frame:
            out.add(0)
        saves = {}                                   # frame offset -> register saved there
        self.spilled = set()
        rsp, rbp = 0, None
        self.rsp_at, self.rbp_at = {}, {}
        # rsp is the same along every path inside the body (x64 frames are fixed); propagate along the CFG
        at = {self.ins[0].address: (0, None)}
        for bn, b in enumerate(self.bl):
            a0 = self.ins[b[0]].address
            if a0 in at:
                rsp, rbp = at[a0]
            for k in b:
                i = self.ins[k]
                self.rsp_at[k], self.rbp_at[k] = rsp, rbp
                m, ops = i.mnemonic, i.operands
                if m == "push":
                    r = canon(self.md.reg_name(ops[0].reg)) if ops[0].type == X.X86_OP_REG else None
                    if r in CALLEE_SAVED and self.entry_only(k, r):
                        out.add(k)
                    elif r is None:
                        raise Unsupported("push of a non-register")
                    rsp -= 8
                elif m == "pop":
                    r = canon(self.md.reg_name(ops[0].reg))
                    if r not in CALLEE_SAVED:
                        raise Unsupported("pop of a scratch register")
                    out.add(k)
                    rsp += 8
                elif m in ("sub", "add") and ops[0].type == X.X86_OP_REG and self.md.reg_name(ops[0].reg) == "rsp":
                    if ops[1].type != X.X86_OP_IMM:
                        raise Unsupported("rsp adjusted by a register (alloca / __chkstk)")
                    rsp += -ops[1].imm if m == "sub" else ops[1].imm
                    out.add(k)
                elif m == "lea" and ops[0].type == X.X86_OP_REG and self.md.reg_name(ops[0].reg) == "rbp" and \
                        ops[1].mem.base and self.md.reg_name(ops[1].mem.base) == "rsp" and not ops[1].mem.index:
                    rbp = rsp + ops[1].mem.disp
                    out.add(k)
                elif m == "mov" and i.op_str == "rbp, rsp":
                    rbp = rsp
                    out.add(k)
                elif m == "mov" and ops[0].type == X.X86_OP_MEM and ops[0].size == 8 and bn == 0 and \
                        ops[1].type == X.X86_OP_IMM and ops[1].imm == -2 and self.is_eh() and \
                        self.frame_off(k, ops[0], rsp, rbp) is not None:
                    self.eh_help = self.frame_off(k, ops[0], rsp, rbp)    # /EHsc's UnwindHelp slot: cl writes it
                    out.add(k)
                elif m in ("mov", "movaps", "movdqa", "movups") and ops[0].type == X.X86_OP_MEM and \
                        ops[1].type == X.X86_OP_REG:
                    r = canon(self.md.reg_name(ops[1].reg))
                    fo = self.frame_off(k, ops[0], rsp, rbp)
                    if fo is not None and r in CALLEE_SAVED and self.entry_only(k, r):
                        saves[fo] = r
                        out.add(k)
                    elif fo is not None and 8 <= fo < 0x28 and r in ARGR + ARGX and (fo - 8) // 8 == \
                            (ARGR + ARGX).index(r) % 4 and self.entry_only(k, r):
                        out.add(k)                  # an argument spilled to its own home: the argument itself
                        self.spilled.add((fo - 8) // 8)
                elif m in ("mov", "movaps", "movdqa", "movups") and ops[0].type == X.X86_OP_REG and \
                        ops[1].type == X.X86_OP_MEM:
                    r = canon(self.md.reg_name(ops[0].reg))
                    fo = self.frame_off(k, ops[1], rsp, rbp)
                    if fo is not None and saves.get(fo) == r:
                        out.add(k)
                if m == "jmp" and ops[0].type == X.X86_OP_IMM and ops[0].imm in self.bstart_addr():
                    at.setdefault(ops[0].imm, (rsp, rbp))
                elif m in CCOF or m.startswith("j"):
                    if ops and ops[0].type == X.X86_OP_IMM:
                        at.setdefault(ops[0].imm, (rsp, rbp))
                if i.address in self.switches:
                    for _v, t in self.switches[i.address]["cases"]:
                        at.setdefault(t, (rsp, rbp))
                    at.setdefault(self.switches[i.address]["default"], (rsp, rbp))
            last = self.ins[b[-1]]
            if not (last.mnemonic == "jmp" or self.is_ret(last)) and bn + 1 < len(self.bl):
                at.setdefault(self.ins[self.bl[bn + 1][0]].address, (rsp, rbp))
        self.saves = saves
        for k, i in enumerate(self.ins):            # a spill later in the body (before a lea of the slot)
            ops = i.operands
            if i.mnemonic in ("mov", "movss", "movsd") and len(ops) == 2 and ops[0].type == X.X86_OP_MEM and \
                    ops[1].type == X.X86_OP_REG:
                r = canon(self.md.reg_name(ops[1].reg))
                fo = self.frame_off(k, ops[0], self.rsp_at[k], self.rbp_at[k])
                if fo is not None and 8 <= fo < 0x28 and r in ARGR + ARGX and (fo - 8) // 8 == \
                        (ARGR + ARGX).index(r) % 4 and self.entry_only(k, r):
                    self.spilled.add((fo - 8) // 8)
        return out

    def bstart_addr(self):
        return self.bstart

    def frame_off(self, k, op, rsp, rbp):
        if op.type != X.X86_OP_MEM or op.mem.index:
            return None
        b = self.md.reg_name(op.mem.base) if op.mem.base else None
        if b == "rsp":
            return rsp + op.mem.disp
        if b == "rbp" and rbp is not None:
            return rbp + op.mem.disp
        if b == "r11" and self.r11_frame and self.reach_defs.get((k, "r11")) == {(0, "r11")}:
            return op.mem.disp                       # `mov r11, rsp` first: r11 is rsp at the entry
        return None

    def frame_name(self, off, ty):
        if 8 <= off < 0x28 and (off - 8) // 8 not in self.spilled and (self.opts.get("home") != "param" or
                                                                    not self.reg_param((off - 8) // 8)):
            # the home slot of an argument the function does not take: cl's storage for a local
            return "_lh%d:%s" % ((off - 8) // 8, ty) if not (off - 8) % 8 else "_lh%d_%d:%s" % ((off - 8) // 8,
                                                                                              (off - 8) % 8, ty)
        if off >= 8:
            p = (off - 8) // 8 if off < 0x28 else 4 + (off - 0x28) // 8
            if (off - 8) % 8:
                raise Unsupported("unaligned argument slot")
            self.args_read.setdefault(p, ty)
            return "_a%d:%s" % (p, ty)
        if off == 0:
            raise Unsupported("the return address read")
        fr = getattr(self, "frame_region", None)
        if fr and fr[0] <= off < fr[1]:
            return "[_frame:1008+%d]:%s" % (off - fr[0], ty)
        if ty in ("a016", "b016", "c016"):
            return "_l%dv:%s" % (-off, ty)          # cl reuses a slot for a vector and a scalar: two variables
        return "_l%d:%s" % (-off, ty)

    def find_frame_region(self):
        """Address-taken locals: one aggregate from the lowest lea'd slot up to the last slot accessed below the
        saved registers (cl lays out one local object; scalar spill slots are its own)."""
        self.frame_region = None
        if self.opts.get("noframe"):
            return
        leas, acc = set(), []
        npush_top = min([self.rsp_at[k] for k in self.pro if self.ins[k].mnemonic == "push"] + [0])
        for k, i in enumerate(self.ins):
            if k in self.pro:
                continue
            for op in i.operands:
                if op.type != X.X86_OP_MEM:
                    continue
                fo = self.frame_off(k, op, self.rsp_at[k], self.rbp_at[k])
                if fo is None or fo >= npush_top or fo in self.saves:
                    continue
                if i.mnemonic == "lea":
                    leas.add(fo)
                else:
                    acc.append((fo, op.size))
            for op in i.operands:                     # a local array: [rsp + i*s + D]
                if op.type == X.X86_OP_MEM and op.mem.index and op.mem.base and \
                        (self.md.reg_name(op.mem.base) == "rsp" or
                         self.md.reg_name(op.mem.base) == "rbp" and self.rbp_at.get(k) is not None):
                    b = self.md.reg_name(op.mem.base)
                    fo = (self.rsp_at[k] if b == "rsp" else self.rbp_at[k]) + op.mem.disp
                    leas.add(fo)
                    acc.append((fo, op.size))
        if not leas:
            return
        lo = min(leas)
        # up to the saved registers: the object's size is not in the code (a pointer to it is all a callee gets);
        # slots above it the code uses are fields (spilled in the original too)
        hi = min([fo for fo in self.saves if lo < fo < npush_top] + [npush_top])
        if hi <= lo and 8 <= lo < 0x28:              # a small local array in the home slots (no frame below)
            hi = 0x28
            for p in range((lo - 8) // 8, 4):
                self.spilled.discard(p)
        if self.opts.get("frame") != "wide":
            top = max([fo + n for fo, n in acc if fo >= lo] + [lo + 8])
            hi = min(lo + ((top - lo + 7) // 8) * 8, hi)
        self.frame_region = (lo, hi)
        self.lines.append("F frame %d" % (hi - lo))

    def reg_param(self, p):
        """Is argument p read from its register somewhere (the caller's value)?"""
        return p in getattr(self, "reg_params", set())

    # ---- operands
    def is_sse(self, i):
        m = i.mnemonic
        return m in SSE_OPS or m in SSE_MOVES or m in SSE_CMP or m in (
            "xorps", "xorpd", "cvtsi2ss", "cvtsi2sd", "cvttss2si", "cvttsd2si", "cvtss2sd", "cvtsd2ss", "movd", "movq",
            "cvtps2pd", "cvtpd2ps", "sqrtss", "maxss", "minss")

    def src_ty(self, i):
        """The type an SSE instruction reads its source as (a conversion reads the other type)."""
        m = i.mnemonic
        if m in ("cvtsd2ss", "cvtpd2ps", "cvttsd2si", "cvtsd2si"):
            return "e008"
        if m in ("cvtss2sd", "cvtps2pd", "cvttss2si", "cvtss2si"):
            return "e004"
        return self.sse_ty(i)

    def sse_ty(self, i):
        m = i.mnemonic
        if m in ("cvtpd2ps",):
            return "e004"
        return "e008" if m.endswith(("sd", "pd")) or m in ("cvtsi2sd", "cvtss2sd", "movapd") else "e004"

    def gaddr(self, i, m):
        return i.address + i.size + m.disp

    def opnd(self, k, op, ty_hint=None):
        i = self.ins[k]
        rsp, rbp = self.rsp_at[k], self.rbp_at[k]
        if op.type == X.X86_OP_REG:
            n = self.md.reg_name(op.reg)
            if n.startswith("xmm"):
                return self.xread(k, n, ty_hint or self.sse_ty(i))
            v = self.var_read(k, canon(n))
            return "%s:%s" % (v, RTY.get(op.size, "1008"))
        if op.type == X.X86_OP_IMM:
            v = op.imm
            if op.size == 8 and (i.address + i.size - 8) in self.img.dir64:
                return "?ga_%x@@3HA:1008" % (v & 0xFFFFFFFFFFFFFFFF)
            ty = RTY.get(op.size, "1008")
            bits = 8 * op.size
            v &= (1 << bits) - 1
            if ty == "1008" and v >> 63:
                v -= 1 << 64
            if ty == "2004":
                ty = "1004"
                if v >> 31:
                    v -= 1 << 32
            return "%d:%s" % (v, ty)
        m = op.mem
        ty = ty_hint or MTY.get(op.size, "1008")
        if self.is_sse(i) and not ty_hint and i.mnemonic not in ("cvtsi2ss", "cvtsi2sd", "movd", "movq"):
            ty = "e008" if op.size == 8 else "e004"
        if m.segment != X.X86_REG_INVALID:
            raise Unsupported("segment operand (TLS)")
        base = self.md.reg_name(m.base) if m.base else None
        idx = self.md.reg_name(m.index) if m.index else None
        if base and self.tls_base:
            tb = lambda r: (lambda ds: bool(ds) and all(d[0] in self.tls_base for d in ds))(
                self.reach_defs.get((k, canon(r)), set()))
            if not idx and tb(base):
                return "?gt_%x@@3HA:%s" % (self.img.tls_start() + m.disp, ty)     # a __declspec(thread) variable
            if idx and m.scale == 1 and (tb(base) or tb(idx)):
                off = idx if tb(base) else base             # [block + r], r = SECREL(var) loaded as a constant
                ds = self.reach_defs.get((k, canon(off)), set())
                vals = {self.ins[d[0]].operands[1].imm for d in ds if d[0] not in ("entry", "undef") and
                        self.ins[d[0]].mnemonic == "mov" and self.ins[d[0]].operands[1].type == X.X86_OP_IMM}
                if len(vals) == 1 and ds and all(d[0] not in ("entry", "undef") and self.ins[d[0]].mnemonic == "mov"
                                                 and self.ins[d[0]].operands[1].type == X.X86_OP_IMM for d in ds):
                    return "?gt_%x@@3HA:%s" % (self.img.tls_start() + vals.pop() + m.disp, ty)
                raise Unsupported("TLS access through a computed offset")
        if base == "rip":
            return "?g_%x@@3HA:%s" % (self.gaddr(i, m), ty)
        fo = self.frame_off(k, op, rsp, rbp)
        if fo is not None:
            return self.frame_name(fo, ty)
        if idx and (base == "rsp" or base == "rbp" and rbp is not None):
            fr = self.frame_region
            fo = (rsp if base == "rsp" else rbp) + m.disp if (base == "rsp" or rbp is not None) else None
            if fr is None or fo is None or not fr[0] <= fo < fr[1]:
                raise Unsupported("indexed frame access (a local array)")
            return "[_frame:1008+%s:1008%s+%d]:%s" % (self.var_read(k, canon(idx)), "*%d" % m.scale if m.scale > 1
                                                      else "", fo - fr[0], ty)
        terms = []
        if base:
            terms.append("%s:1008" % self.var_read(k, canon(base)))
        if idx:
            terms.append("%s:1008%s" % (self.var_read(k, canon(idx)), "*%d" % m.scale if m.scale > 1 else ""))
        if m.disp:
            terms.append("%d" % m.disp)
        if not base and not idx:
            return "?g_%x@@3HA:%s" % (m.disp & 0xFFFFFFFFFFFFFFFF, ty)
        return "[%s]:%s" % ("+".join(terms), ty)

    def dst(self, k, op, ty_hint=None):
        i = self.ins[k]
        if op.type == X.X86_OP_REG:
            n = self.md.reg_name(op.reg)
            if n.startswith("xmm"):
                return "%s:%s" % (self.var_written(k, n), ty_hint or self.sse_ty(i))
            return "%s:%s" % (self.var_written(k, canon(n)), RTY.get(op.size, "1008"))
        return self.opnd(k, op, ty_hint)

    def temp(self, ty="1008"):
        self.ntemp += 1
        return "t%d:%s" % (self.ntemp, ty)

    def emit(self, k, op, dsts, srcs, notes=""):
        # il2c's parser drops trailing sources that repeat an address base (the traced dump lists them after a
        # store): `mov [rdx+8], rdx` needs its value listed twice to survive
        bases = re.findall(r"([\w?@$]+:[0-9a-f]{4})", " ".join(t.split("]")[0] for t in dsts + srcs
                                                               if t.startswith("[")))
        if srcs and not srcs[-1].startswith("[") and any(srcs[-1].split(":")[0] == b.split(":")[0] for b in bases):
            srcs = list(srcs) + [srcs[-1]]
        self.lines.append("I %x %s 0 0 | %s <- %s%s" % (self.ins[k].address, op, " ".join(dsts), " ".join(srcs),
                                                        (" ; " + notes) if notes else ""))

    # ---- calls
    def call_args(self, k, tail=False, decide=False):
        """The argument tokens of the call / tail jmp at k (decide: only mark the argument registers as read)."""
        rsp = self.rsp_at[k]
        stack = self.out_slots.get(k, {})
        nstack = max([j + 1 for j in stack] + [0])
        arity = 0
        kinds = []
        for p in range(4):
            r, x = ARGR[p], ARGX[p]
            if self.explicit(k, x):
                kinds.append("x")
                arity = p + 1
            elif self.explicit(k, r):
                kinds.append("r")
                arity = p + 1
            elif tail and (self.entry_only(k, r) or self.entry_only(k, x)) and p < self.own_arity:
                kinds.append("x" if self.args_read.get(p, "")[:1] == "e" else "r")
                arity = p + 1
            elif self.entry_only(k, r) and p in self.args_read and self.args_read[p][:1] != "e" and \
                    self.passes_entry(k, r):
                kinds.append("r")
                arity = p + 1
            else:
                kinds.append(None)
        forced = (self.opts.get("callsig") or {}).get(self.ins[k].operands[0].imm) \
            if self.ins[k].operands and self.ins[k].operands[0].type == X.X86_OP_IMM else None
        if forced is not None:                   # one signature per callee (another call site decided it)
            kinds = list(forced[:4]) + [None] * (4 - min(4, len(forced)))
            arity = max(arity, min(4, len(forced))) if nstack else min(4, len(forced))
        if nstack:
            arity = 4 + nstack
        toks = []
        for p in range(arity):
            if p >= 4:
                j = p - 4
                if j not in stack:
                    if tail:
                        toks.append("_a%d:1008" % p)
                        continue
                    raise Unsupported("an outgoing argument slot the code does not write")
                if not decide:
                    kk = stack[j]
                    so = self.ins[kk].operands
                    ty = MTY.get(so[0].size, "1008")
                    if self.is_sse(self.ins[kk]):
                        ty = "e008" if so[0].size == 8 else "e004"
                    toks.append(self.opnd(kk, so[1], ty if ty[0] == "e" else None))
                    if ty[0] != "e" and toks[-1].endswith(":2004"):
                        toks[-1] = toks[-1][:-5] + ":1004"
                continue
            kd = kinds[p]
            r = ARGX[p] if kd == "x" else ARGR[p]
            self.real_maybe.add((k, r))
            if decide:
                continue
            if kd == "x":
                toks.append(self.xread(k, r, self.xmm_ty_at(k, r)))
            else:
                toks.append("%s:%s" % (self.var_read(k, r), self.width_at(k, r)))
        return toks

    def passes_entry(self, k, r):
        """The caller's argument still in its register at a call: passed on when the callee reads that register
        before writing it (opts["callee_args"]: a constructor passing `this` and `other` straight through)."""
        if not self.opts.get("callee_args"):
            return False
        i = self.ins[k]
        if not i.operands or i.operands[0].type != X.X86_OP_IMM:
            return False
        return ARGR.index(r) < callee_arity(self.img, i.operands[0].imm)

    def width_at(self, k, r):
        """The type a register value has at k: the width of the writes that reach it."""
        ws = set()
        for d in self.reach_defs.get((k, r), set()):
            if d[0] in ("entry", "undef"):
                p = ARGR.index(r) if r in ARGR else None
                ws.add(self.args_read.get(p, "1008") if p is not None else "1008")
                continue
            i = self.ins[d[0]]
            op = i.operands[0] if i.operands else None
            if op is not None and op.type == X.X86_OP_REG and canon(self.md.reg_name(op.reg)) == r:
                ws.add(RTY.get(op.size, "1008"))
            else:
                ws.add("1008")
        if ws == {"2004"} or ws == {"1004"}:
            return "1004"
        if ws == {"2001"}:
            return "2001"
        if ws == {"2002"}:
            return "2002"
        return "1008"

    def xmm_ty_at(self, k, x):
        ts = set()
        for d in self.reach_defs.get((k, x), set()):
            if d[0] in ("entry", "undef"):
                p = ARGX.index(x) if x in ARGX else None
                ts.add(self.args_read.get(p, "e004") if p is not None else "e004")
                continue
            i = self.ins[d[0]]
            m = i.mnemonic
            if m in ("cvtss2sd", "cvtsi2sd"):
                ts.add("e008")
            elif m in ("cvtsd2ss", "cvtsi2ss"):
                ts.add("e004")
            else:
                ts.add(self.sse_ty(i))
        return "e008" if ts == {"e008"} else "e004"

    def find_out_slots(self):
        """Stores into [rsp+0x20+8j] (relative to rsp at the call) between a call and the previous one, in the
        same block, not read before the call: stack arguments. out_slots: call index -> {j: store index}."""
        self.out_slots = {}
        self.arg_store = set()
        # frame offsets read or address-taken anywhere: a store there is to a local, not an argument
        local = set()
        for k, i in enumerate(self.ins):
            if k in self.pro:
                continue
            for n, op in enumerate(i.operands):
                if op.type != X.X86_OP_MEM:
                    continue
                fo = self.frame_off(k, op, self.rsp_at[k], self.rbp_at[k])
                if fo is None:
                    continue
                if i.mnemonic == "lea" or op.access & capstone.CS_AC_READ or n > 0:
                    for d in range(0, max(op.size, 1) if i.mnemonic != "lea" else 1):
                        local.add(fo + d)
                    if i.mnemonic == "lea":
                        local |= {fo + d for d in range(0, 64)}
        for b in self.bl:
            pend = {}
            for k in b:
                i = self.ins[k]
                if i.mnemonic == "call":
                    # stack arguments are slots 0..n-1 with no gap: a store further up is to a local (an EH
                    # state, a spill into the home area, a local the callee never sees)
                    n = 0
                    while n in pend:
                        n += 1
                    if n:
                        self.out_slots[k] = {j: pend[j] for j in range(n)}
                        self.arg_store |= set(self.out_slots[k].values())
                    pend = {}
                    continue
                for n, op in enumerate(i.operands):
                    fo = self.frame_off(k, op, self.rsp_at[k], self.rbp_at[k]) if k not in self.pro else None
                    disp = fo - self.rsp_at[k] if fo is not None else None
                    if fo is not None and disp >= 0x20 and fo < 0:
                        j = (disp - 0x20) // 8
                        if n == 0 and i.mnemonic in ("mov", "movss", "movsd") and len(i.operands) == 2 and \
                                (disp - 0x20) % 8 == 0 and i.operands[1].type != X.X86_OP_MEM and \
                                fo not in local:
                            pend[j] = k
                        elif j in pend:
                            del pend[j]

    def tls_index_def(self, j, ir, k):
        """The `mov r, [rip + _tls_index]` that the index register of the access at j comes from: the one def that
        reaches j (cl loads _tls_index once and reuses it), else the nearest such load before k."""
        r = canon(self.md.reg_name(ir))
        ds = self.reach_defs.get((j, r), set())
        if len(ds) == 1:
            d = next(iter(ds))
            if d[0] not in ("entry", "undef") and self.ins[d[0]].mnemonic == "mov" and "rip" in self.ins[d[0]].op_str:
                return d[0]
        return next((q for q in range(max(0, k - 6), j) if self.ins[q].mnemonic == "mov" and
                     self.ins[q].operands[0].type == X.X86_OP_REG and
                     canon(self.md.reg_name(self.ins[q].operands[0].reg)) == r and "rip" in self.ins[q].op_str), None)

    def find_tls(self):
        """`mov e, [rip+_tls_index]; mov b, gs:[0x58]; mov c, [b+e*8]`: c is this thread's TLS block; [c+D] is the
        __declspec(thread) variable at .tls+D."""
        self.tls_base, self.tls_skip, self.tls_addr = set(), set(), {}
        ins = self.ins
        for k, i in enumerate(ins):
            if not (i.mnemonic == "mov" and "gs:[0x58]" in i.op_str):
                continue
            b = i.operands[0].reg
            adds = [q for q in range(k + 1, min(k + 14, len(ins))) if ins[q].mnemonic == "add" and
                    ins[q].operands[1].type == X.X86_OP_MEM and ins[q].operands[1].mem.base == b and
                    ins[q].operands[1].mem.scale == 8]
            for q in adds:                         # `mov r, SECREL(v); add r, [block + e*8]`: r = &v
                r = canon(self.md.reg_name(ins[q].operands[0].reg))
                ds = self.reach_defs.get((q, r), set())
                d = next(iter(ds)) if len(ds) == 1 else None
                if d is None or d[0] in ("entry", "undef") or ins[d[0]].mnemonic != "mov" or \
                        ins[d[0]].operands[1].type != X.X86_OP_IMM:
                    raise Unsupported("TLS address shape")
                self.tls_addr[q] = self.img.tls_start() + ins[d[0]].operands[1].imm
                self.tls_skip.add(d[0])
            j = next((q for q in range(k + 1, min(k + 14, len(ins))) if ins[q].mnemonic == "mov" and
                      ins[q].operands[1].type == X.X86_OP_MEM and ins[q].operands[1].mem.base == b and
                      ins[q].operands[1].mem.scale == 8), None)
            if j is None and adds:
                q0 = min(adds)
                ir = ins[q0].operands[1].mem.index
                q = self.tls_index_def(q0, ir, k)
                if q is None:
                    raise Unsupported("TLS index load")
                self.tls_skip |= {q, k}
                continue
            if j is None:
                raise Unsupported("TLS access shape")
            ir = ins[j].operands[1].mem.index
            q = self.tls_index_def(j, ir, k)
            if q is None:
                raise Unsupported("TLS index load")
            self.tls_skip |= {q, k, j}
            self.tls_base.add(j)

    @staticmethod
    def cc_of(jm, fl):
        """The IL condition of jcc / setcc / cmovcc mnemonic jm (as a j-form) after flags of kind fl."""
        if fl == "f":
            return FCC.get(jm)
        if fl == "bt":                         # CF is the tested bit
            return {"jb": "ne", "jae": "e"}.get(jm)
        if fl == "neg":                        # after neg x: CF = (x != 0), ZF = (x == 0)
            return {"jb": "ne", "jae": "e", "je": "e", "jne": "ne"}.get(jm)
        return CCOF.get(jm)

    # ---- division by a constant: cl's multiply-high sequences
    def find_divs(self):
        """`mov rax, M; imul x` (or `mul x`) and the shifts / adds after it that only feed each other: x / D.
        The value reached is found by running those instructions on sample inputs. div_at: mul index -> (x token
        reader, D, type, temp); div_res: index of the instruction whose result is x / D -> its register;
        div_skip: everything else in the sequence."""
        self.div_at, self.div_res, self.div_skip = {}, {}, set()
        users = collections.defaultdict(set)
        for (kk, r), ds in self.reach_defs.items():
            if r in self.acc[kk][0]:
                for d in ds:
                    users[d].add((kk, r))
        for k, i in enumerate(self.ins):
            if i.mnemonic not in ("imul", "mul") or len(i.operands) != 1:
                continue
            try:
                self.div_one(k, i, users)
            except Unsupported:
                pass

    def div_one(self, k, i, users):
        w = i.operands[0].size
        if w not in (4, 8):
            raise Unsupported("multiply-high width")
        signed = i.mnemonic == "imul"
        bits = 8 * w
        mask = (1 << bits) - 1
        chain = []                           # the constant's movs: rax <- r10 <- M
        kk, r = k, "rax"
        while True:
            ds = self.reach_defs.get((kk, r), set())
            if len(ds) != 1:
                raise Unsupported("multiplier")
            dm = next(iter(ds))
            if dm[0] in ("entry", "undef"):
                raise Unsupported("multiplier")
            mi = self.ins[dm[0]]
            if mi.mnemonic not in ("mov", "movabs") or mi.operands[0].type != X.X86_OP_REG:
                raise Unsupported("multiplier")
            chain.append(dm)
            if mi.operands[1].type == X.X86_OP_IMM:
                break
            if mi.operands[1].type != X.X86_OP_REG or len(chain) > 3:
                raise Unsupported("multiplier")
            kk, r = dm[0], canon(self.md.reg_name(mi.operands[1].reg))
        M = mi.operands[1].imm & mask
        xop = i.operands[0]
        xreg = canon(self.md.reg_name(xop.reg)) if xop.type == X.X86_OP_REG else None
        xdefs = self.reach_defs.get((k, xreg)) if xreg else None
        if xreg == "rax":
            raise Unsupported("x in rax")
        if users.get((k, "rax")):
            raise Unsupported("low half used")
        # the multiplier's movs leave no code when they feed only this multiply (one mov each)
        mskip = set()
        for n, d in enumerate(chain):
            want = {(k, "rax")} if n == 0 else {(chain[n - 1][0], d[1])}
            if set(users.get(d, ())) != want:
                break
            mskip.add(d[0])

        def sx(v, b):
            v &= (1 << b) - 1
            return v - (1 << b) if v >> (b - 1) else v
        tests = [1, 2, 3, 7, 100, 12345, 99999, (1 << (bits - 2)) + 12345, mask >> 1, (1 << (bits - 1)) - 7]
        if signed:
            tests += [-t for t in tests] + [-(1 << (bits - 1))]
        S = {(k, "rdx"): None}               # def -> instruction producing it (None: the multiply-high itself)
        order = [(k, "rdx")]

        def hi(x):
            prod = (sx(x, bits) if signed else x & mask) * (sx(M, bits) if signed else M)
            return (prod >> bits) & mask

        def value(d, x, memo):
            if d in memo:
                return memo[d]
            if d == (k, "rdx"):
                v = hi(x)
            else:
                v = run1(d[0], x, memo)
            memo[d] = v
            return v

        def src_val(kk, op, x, memo):
            if op.type == X.X86_OP_IMM:
                return op.imm & 0xFFFFFFFFFFFFFFFF
            if op.type != X.X86_OP_REG:
                raise Unsupported("memory in the sequence")
            r = canon(self.md.reg_name(op.reg))
            dd = self.reach_defs.get((kk, r), set())
            if xreg and dd == xdefs:
                v = x & mask
            elif len(dd) == 1 and next(iter(dd)) in S:
                v = value(next(iter(dd)), x, memo)
            else:
                raise Unsupported("outside input")
            return v & ((1 << (8 * op.size)) - 1)

        def run1(kk, x, memo):
            ii = self.ins[kk]
            m, ops = ii.mnemonic, ii.operands
            sz = ops[0].size
            b = 8 * sz
            if m == "mov":
                v = src_val(kk, ops[1], x, memo)
            elif m in ("sar", "shr", "shl"):
                a = src_val(kk, ops[0], x, memo)
                c = ops[1].imm if len(ops) > 1 else 1
                v = (sx(a, b) >> c) if m == "sar" else (a >> c) if m == "shr" else a << c
            elif m in ("add", "sub"):
                a, c = src_val(kk, ops[0], x, memo), src_val(kk, ops[1], x, memo)
                v = a + c if m == "add" else a - c
            else:
                raise Unsupported("op %s" % m)
            return v & ((1 << b) - 1)

        def quotient(d):
            """D when the value d holds is x / D for every sample, else None."""
            try:
                fx = {x: value(d, x, {}) for x in tests}
            except Unsupported:
                return None
            sz = 8 if d == (k, "rdx") else self.ins[d[0]].operands[0].size
            big = tests[-1] if not signed else tests[len(tests) // 2 - 1]
            q = sx(fx[big], 8 * sz) if signed else fx[big]
            if q <= 0:
                return None
            for D in {big // q, big // q + 1, big // (q + 1) + 1, (big + q - 1) // q}:
                if D < 2:
                    continue
                ok = True
                for x, v in fx.items():
                    xv = sx(x, bits) if signed else x & mask
                    want = int(xv / D) if signed and abs(xv) < (1 << 52) else (abs(xv) // D) * (1 if xv >= 0 else -1)
                    if (want & ((1 << (8 * sz)) - 1)) != v:
                        ok = False
                        break
                if ok:
                    return D
            return None

        res = None
        frontier = [(k, "rdx")]
        while frontier:
            frontier.sort(key=lambda d: d[0])
            d = frontier.pop(0)                # in instruction order: a def's inputs are in S before it
            D = quotient(d) if d != (k, "rdx") else None
            if D:
                if res and res[0] != d:
                    raise Unsupported("two results")
                res = (d, D)
                continue
            for (kk, r) in sorted(users.get(d, ())):
                ii = self.ins[kk]
                wr = self.acc[kk][1]
                if ii.mnemonic not in ("mov", "sar", "shr", "shl", "add", "sub") or len(wr) != 1 or \
                        ii.operands[0].type != X.X86_OP_REG:
                    continue                   # an outside user: allowed of the result only (checked below)
                nd = (kk, next(iter(wr)))
                if nd not in S:
                    S[nd] = kk
                    order.append(nd)
                    frontier.append(nd)
        if not res:
            raise Unsupported("no quotient")
        rd, D = res
        # only what the result needs; every def but the result is used only inside the sequence
        need, todo = set(), [rd]
        while todo:
            d = todo.pop()
            if d in need or d == (k, "rdx"):
                continue
            need.add(d)
            for r in self.acc[d[0]][0]:
                for dd in self.reach_defs.get((d[0], r), set()):
                    if dd in S:
                        todo.append(dd)
        S = {d: S[d] for d in S if d in need or d == (k, "rdx")}
        for d in S:
            if d == rd:
                continue
            for (kk, r) in users.get(d, ()):
                if not any(kk == dd[0] for dd in S if dd != (k, "rdx")):
                    raise Unsupported("intermediate used outside")
        ty = ("1" if signed else "2") + ("008" if w == 8 else "004")
        self.div_at[k] = (D, ty)
        self.div_res[rd[0]] = (rd[1], k)
        self.div_skip |= {dd[0] for dd in S if dd[0] != rd[0] and dd != (k, "rdx")} | mskip

    # ---- packed SSE: which xmm webs hold vectors
    def root_r(self, k, r):
        ds = self.reach_defs.get((k, r))
        return self.find(sorted(ds, key=str)[0]) if ds else None

    def xmm_ops(self, i):
        return [n for n, op in enumerate(i.operands) if op.type == X.X86_OP_REG and
                self.md.reg_name(op.reg).startswith("xmm")]

    def mark_vectors(self):
        """vk: web root -> "f" / "i" for xmm webs a packed instruction reads or writes (a web holding the
        caller's argument or a call's result stays scalar: it converts where a packed instruction reads it)."""
        self.vk = {}
        self.vtemps = 0
        copy16 = lambda k, i: i.mnemonic in ("movdqa", "movdqu", "movaps", "movups") and k not in self.pro and \
            any(op.type == X.X86_OP_MEM and op.size == 16 for op in i.operands)
        self.vec_used = any(i.mnemonic in PACKED or copy16(k, i) for k, i in enumerate(self.ins))
        if not self.vec_used:
            return
        origin = set()
        for (k, r), ds in self.reach_defs.items():
            for d in ds:
                if d[0] in ("entry", "undef") or self.ins[d[0]].mnemonic == "call":
                    origin.add(self.find(d))

        def mark(root, kind):
            if root is None or root in self.vk:
                return False
            if root in origin:
                return False
            self.vk[root] = kind
            return True
        for k, i in enumerate(self.ins):
            m = i.mnemonic
            if m not in PACKED or k in self.pro:
                continue
            if m in NEUTRAL and i.operands[1].type == X.X86_OP_MEM or m == "sqrtps" or \
                    m in ("xorps", "pxor") and i.operands[1].type == X.X86_OP_REG and \
                    i.operands[0].reg == i.operands[1].reg:
                continue                              # decided by the other uses of its register (cl's
                                                      # sqrtf is sqrtps)
            _f, form, kin, kout = PACKED[m]
            if m == "shufps" and i.operands[1].type == X.X86_OP_REG and i.operands[0].reg == i.operands[1].reg and \
                    i.operands[2].imm == 0:
                r = self.md.reg_name(i.operands[0].reg)
                ds = self.reach_defs.get((k, r), set())
                if ds and all(d[0] not in ("entry", "undef") and self.ins[d[0]].mnemonic in SCALAR_DEF and
                              not (self.ins[d[0]].mnemonic == "movss" and self.ins[d[0]].operands[1].type ==
                                   X.X86_OP_REG) for d in ds):
                    mark(self.find((k, r)), "f")             # _mm_set1_ps(x): x stays a float
                    continue
            for n in self.xmm_ops(i):
                r = self.md.reg_name(i.operands[n].reg)
                if n == 0:
                    if (k, r) in self.reach_defs and form in ("bin", "bini", "shi"):
                        mark(self.root_r(k, r), kin)
                    if self.find((k, r)) in origin:
                        raise Unsupported("a vector written into a scalar argument's web")
                    mark(self.find((k, r)), kout)
                else:
                    mark(self.root_r(k, r), kin)
        for k, i in enumerate(self.ins):               # a 16-byte load / store: a vector (a struct copy)
            if copy16(k, i):
                kind = "i" if i.mnemonic.startswith("movdq") else "f"
                r = self.md.reg_name(i.operands[0].reg if i.operands[0].type == X.X86_OP_REG else i.operands[1].reg)
                if i.operands[0].type == X.X86_OP_REG:
                    mark(self.find((k, r)), kind)
                else:
                    mark(self.root_r(k, r), kind)
        changed = True
        while changed:                                # copies and register-only logic ops carry the kind
            changed = False
            for k, i in enumerate(self.ins):
                m = i.mnemonic
                xs = self.xmm_ops(i)
                if k in self.pro or len(xs) != 2 or not (m in ("movaps", "movups", "movdqa", "movdqu", "movss") or
                                                         m in NEUTRAL):
                    continue
                rd = self.md.reg_name(i.operands[0].reg)
                rs = self.md.reg_name(i.operands[1].reg)
                a, b = self.find((k, rd)), self.root_r(k, rs)
                kind = self.vk.get(a) or self.vk.get(b)
                if kind:
                    changed |= mark(a, kind) | mark(b, kind)
            for k, i in enumerate(self.ins):          # a scalar op on a vector's low lane keeps a vector
                m = i.mnemonic
                if k in self.pro or not (m in SS_ON_VEC or m in ("sqrtss", "rsqrtss", "rcpss")) or \
                        i.operands[0].type != X.X86_OP_REG:
                    continue
                rd = self.md.reg_name(i.operands[0].reg)
                srcs = [self.root_r(k, rd)]
                if i.operands[1].type == X.X86_OP_REG:
                    srcs.append(self.root_r(k, self.md.reg_name(i.operands[1].reg)))
                kind = next((self.vk[x] for x in srcs if x in self.vk), None)
                if kind:
                    changed |= mark(self.find((k, rd)), "f")
            for k, i in enumerate(self.ins):          # sqrtps of a vector is a vector
                if i.mnemonic == "sqrtps" and i.operands[1].type == X.X86_OP_REG:
                    a = self.find((k, self.md.reg_name(i.operands[0].reg)))
                    b = self.root_r(k, self.md.reg_name(i.operands[1].reg))
                    kind = self.vk.get(a) or self.vk.get(b)
                    if kind:
                        changed |= mark(a, kind) | mark(b, kind)
            for k, i in enumerate(self.ins):          # a logic op with a constant on a vector web
                if i.mnemonic in NEUTRAL and len(i.operands) == 2 and i.operands[1].type == X.X86_OP_MEM and \
                        i.operands[0].type == X.X86_OP_REG:
                    r = self.md.reg_name(i.operands[0].reg)
                    a, b = self.find((k, r)), self.root_r(k, r)
                    kind = self.vk.get(a) or self.vk.get(b)
                    if kind:
                        changed |= mark(a, kind) | mark(b, kind)

    def vtemp(self, ty):
        self.vtemps += 1
        return "t%d:%s" % (5000 + self.vtemps, ty)

    def intrin(self, k, name, dst, args):
        """dst = name(args); args: (token, cast) where cast is "" or a pointer type for an address token."""
        temps, pc = [], []
        for n, (tok, cast) in enumerate(args):
            t = self.temp(tok.rsplit(":", 1)[1] if not cast else "1008")
            self.emit(k, "OPARG", [t], [tok], "s0=&" if cast else "")
            temps.append(t)
            if cast:
                pc.append("%d%s" % (n, cast))
        fn = "?__lift_fn_%s@@YAXXZ" % name
        self.emit(k, "OPCALL", [dst] if dst else [], [fn + ":1008"] + temps,
                  "s0=%s%s" % (fn, (" pc=" + ",".join(pc)) if pc else ""))
        self.intrin_used = True

    def vkind_r(self, k, r):
        return self.vk.get(self.root_r(k, r))

    def vkind_w(self, k, r):
        return self.vk.get(self.find((k, r)))

    def vread(self, k, op, kind, bcast=False):
        """A vector operand of kind f / i."""
        if op.type == X.X86_OP_MEM:
            return self.opnd(k, op, VT[kind])
        if op.type == X.X86_OP_IMM:
            return "%d:1004" % op.imm
        r = self.md.reg_name(op.reg)
        have = self.vkind_r(k, r)
        if have is None:                              # a scalar float (an argument, a call's result)
            f = "%s:e004" % self.var_read(k, r)
            t = self.vtemp(VT["f"])
            self.intrin(k, "_mm_set1_ps" if bcast else "_mm_set_ss", t, [(f, "")])
            have, tok = "f", t
        else:
            tok = "%s:%s" % (self.var_read(k, r), VT[have])
        if have != kind:
            t = self.vtemp(VT[kind])
            self.intrin(k, "_mm_castsi128_ps" if kind == "f" else "_mm_castps_si128", t, [(tok, "")])
            tok = t
        return tok

    def vwrite(self, k, op, kind, emit_value):
        """Write a vector of kind into register op: emit_value(dst token) emits it; a web of the other kind gets
        a cast."""
        r = self.md.reg_name(op.reg)
        have = self.vkind_w(k, r)
        if have is None:
            raise Unsupported("vector written to a scalar web")
        d = "%s:%s" % (self.var_written(k, r), VT[have])
        if have == kind:
            emit_value(d)
            return
        t = self.vtemp(VT[kind])
        emit_value(t)
        self.intrin(k, "_mm_castsi128_ps" if have == "f" else "_mm_castps_si128", d, [(t, "")])

    def xread(self, k, r, ty):
        """xmm register r read as a scalar of type ty: a vector web gives its low lane."""
        kind = self.vkind_r(k, r) if self.vk else None
        if not kind:
            return "%s:%s" % (self.var_read(k, r), ty)
        if ty != "e004":
            raise Unsupported("double from a vector")
        v = self.vread(k, self.fake_reg(r), "f")
        t = self.vtemp("e004")
        self.intrin(k, "_mm_cvtss_f32", t, [(v, "")])
        return t

    def fake_reg(self, r):
        class O:
            pass
        o = O()
        o.type, o.reg, o.size = X.X86_OP_REG, next(x for x in range(1, 300) if self.md.reg_name(x) == r), 16
        return o

    def vec_insn(self, k, i):
        """Lift i as vector code when it is packed or touches a vector web; False: the scalar path takes it."""
        m, ops = i.mnemonic, i.operands
        if not self.vk:
            return False
        xs = self.xmm_ops(i)
        touch = any((n == 0 and self.vkind_w(k, self.md.reg_name(ops[n].reg))) or
                    ((k, self.md.reg_name(ops[n].reg)) in self.reach_defs and
                     self.vkind_r(k, self.md.reg_name(ops[n].reg))) for n in xs)
        zero = m in ("xorps", "pxor") and ops[1].type == X.X86_OP_REG and ops[0].reg == ops[1].reg
        if m in PACKED and not ((m in NEUTRAL and ops[1].type == X.X86_OP_MEM or m == "sqrtps" or zero) and
                                not touch):
            name, form, kin, kout = PACKED[m]
            if m in ("xorps", "pxor") and ops[1].type == X.X86_OP_REG and ops[0].reg == ops[1].reg:
                self.vwrite(k, ops[0], kout, lambda d: self.intrin(
                    k, "_mm_setzero_ps" if kout == "f" else "_mm_setzero_si128", d, []))
                return True
            if form == "bin":
                a, b = self.vread(k, ops[0], kin), self.vread(k, ops[1], kin)
                self.vwrite(k, ops[0], kout, lambda d: self.intrin(k, name, d, [(a, ""), (b, "")]))
            elif form == "bini":
                bc = m == "shufps" and ops[1].type == X.X86_OP_REG and ops[0].reg == ops[1].reg
                a = self.vread(k, ops[0], kin, bcast=bc and ops[2].imm == 0)
                b = a if bc else self.vread(k, ops[1], kin)
                if bc and ops[2].imm == 0 and not self.vkind_r(k, self.md.reg_name(ops[0].reg)):
                    self.vwrite(k, ops[0], kout, lambda d: self.emit(k, "OPASSIGN", [d], [a]))
                    return True
                self.vwrite(k, ops[0], kout, lambda d: self.intrin(k, name, d, [(a, ""), (b, ""),
                                                                                 ("%d:1004" % ops[2].imm, "")]))
            elif form == "un":
                a = self.vread(k, ops[1], kin)
                self.vwrite(k, ops[0], kout, lambda d: self.intrin(k, name, d, [(a, "")]))
            elif form == "uni":
                a = self.vread(k, ops[1], kin)
                self.vwrite(k, ops[0], kout, lambda d: self.intrin(k, name, d, [(a, ""),
                                                                                 ("%d:1004" % ops[2].imm, "")]))
            elif form == "shi":
                if ops[1].type != X.X86_OP_IMM:
                    raise Unsupported("vector shift by a register")
                a = self.vread(k, ops[0], kin)
                self.vwrite(k, ops[0], kout, lambda d: self.intrin(k, name, d, [(a, ""),
                                                                                 ("%d:1004" % ops[1].imm, "")]))
            return True
        if not touch:
            return False
        if m in ("movaps", "movups", "movdqa", "movdqu"):
            kind = "i" if m.startswith("movdq") else "f"
            una = m in ("movups", "movdqu")
            if ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_REG:
                have = self.vkind_w(k, self.md.reg_name(ops[0].reg)) or kind
                a = self.vread(k, ops[1], have)
                self.vwrite(k, ops[0], have, lambda d: self.emit(k, "OPASSIGN", [d], [a]))
            elif ops[0].type == X.X86_OP_REG:
                have = self.vkind_w(k, self.md.reg_name(ops[0].reg)) or kind
                if una:
                    mt = self.opnd(k, ops[1], "e004" if kind == "f" else VT["i"])
                    self.vwrite(k, ops[0], kind, lambda d: self.intrin(
                        k, "_mm_loadu_ps" if kind == "f" else "_mm_loadu_si128", d,
                        [(mt, "f" if kind == "f" else "i")]))
                else:
                    mt = self.opnd(k, ops[1], VT[have])
                    self.vwrite(k, ops[0], have, lambda d: self.emit(k, "OPASSIGN", [d], [mt]))
            else:
                have = self.vkind_r(k, self.md.reg_name(ops[1].reg)) or kind
                if una:
                    v = self.vread(k, ops[1], kind)
                    mt = self.opnd(k, ops[0], "e004" if kind == "f" else VT["i"])
                    self.intrin(k, "_mm_storeu_ps" if kind == "f" else "_mm_storeu_si128", None,
                                [(mt, "f" if kind == "f" else "i"), (v, "")])
                else:
                    v = self.vread(k, ops[1], have)
                    self.emit(k, "OPASSIGN", [self.opnd(k, ops[0], VT[have])], [v])
            return True
        if m == "movss":
            if ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_REG:
                a, b = self.vread(k, ops[0], "f"), self.vread(k, ops[1], "f")
                self.vwrite(k, ops[0], "f", lambda d: self.intrin(k, "_mm_move_ss", d, [(a, ""), (b, "")]))
            elif ops[0].type == X.X86_OP_REG:
                mt = self.opnd(k, ops[1], "e004")
                self.vwrite(k, ops[0], "f", lambda d: self.intrin(k, "_mm_load_ss", d, [(mt, "f")]))
            else:
                v = self.vread(k, ops[1], "f")
                self.intrin(k, "_mm_store_ss", None, [(self.opnd(k, ops[0], "e004"), "f"), (v, "")])
            return True
        if m == "movsd" and (ops[0].type == X.X86_OP_MEM) != (ops[1].type == X.X86_OP_MEM):
            if ops[0].type == X.X86_OP_MEM:                 # the low two lanes stored
                v = self.vread(k, ops[1], "f")
                t = self.vtemp("c016")
                self.intrin(k, "_mm_castps_pd", t, [(v, "")])
                self.intrin(k, "_mm_store_sd", None, [(self.opnd(k, ops[0], "e008"), "d"), (t, "")])
            else:
                have = self.vkind_w(k, self.md.reg_name(ops[0].reg)) or "f"
                t = self.vtemp("c016")
                self.intrin(k, "_mm_load_sd", t, [(self.opnd(k, ops[1], "e008"), "d")])
                self.vwrite(k, ops[0], have, lambda d: self.intrin(
                    k, "_mm_castpd_ps" if have == "f" else "_mm_castpd_si128", d, [(t, "")]))
            return True
        if m in SS_ON_VEC:
            a = self.vread(k, ops[0], "f")
            if ops[1].type == X.X86_OP_MEM:
                b = self.vtemp(VT["f"])
                self.intrin(k, "_mm_load_ss", b, [(self.opnd(k, ops[1], "e004"), "f")])
            else:
                b = self.vread(k, ops[1], "f")
            self.vwrite(k, ops[0], "f", lambda d: self.intrin(k, SS_ON_VEC[m], d, [(a, ""), (b, "")]))
            return True
        if m in ("sqrtss", "rsqrtss", "rcpss"):
            if ops[1].type == X.X86_OP_REG and ops[1].reg == ops[0].reg:
                a = self.vread(k, ops[1], "f")
                self.vwrite(k, ops[0], "f", lambda d: self.intrin(k, "_mm_%s_ss" % m[:-2], d, [(a, "")]))
                return True
            # another source: its low lane's result under the destination's upper lanes
            # (_mm_move_ss(dst, _mm_sqrt_ss(src)), which cl folds into the one instruction)
            if ops[1].type == X.X86_OP_MEM:
                b = self.vtemp(VT["f"])
                self.intrin(k, "_mm_load_ss", b, [(self.opnd(k, ops[1], "e004"), "f")])
            else:
                b = self.vread(k, ops[1], "f")
            a = self.vread(k, ops[0], "f")
            t = self.vtemp(VT["f"])
            self.intrin(k, "_mm_%s_ss" % m[:-2], t, [(b, "")])
            self.vwrite(k, ops[0], "f", lambda d: self.intrin(k, "_mm_move_ss", d, [(a, ""), (t, "")]))
            return True
        if m == "movmskps":
            v = self.vread(k, ops[1], "f")
            self.intrin(k, "_mm_movemask_ps", self.dst(k, ops[0]).rsplit(":", 1)[0] + ":1004", [(v, "")])
            return True
        if m in ("movd", "movq"):
            w = 4 if m == "movd" else 8
            if ops[0].type == X.X86_OP_REG and self.md.reg_name(ops[0].reg).startswith("xmm"):
                src = self.opnd(k, ops[1])
                src = src.rsplit(":", 1)[0] + (":1004" if w == 4 else ":1008")
                if ops[1].type == X.X86_OP_MEM and w == 8:
                    self.vwrite(k, ops[0], "i", lambda d: self.intrin(k, "_mm_loadl_epi64", d,
                                                                      [(self.opnd(k, ops[1], VT["i"]), "i")]))
                    return True
                self.vwrite(k, ops[0], "i", lambda d: self.intrin(
                    k, "_mm_cvtsi32_si128" if w == 4 else "_mm_cvtsi64_si128", d, [(src, "")]))
            else:
                v = self.vread(k, ops[1], "i")
                if ops[0].type == X.X86_OP_MEM:
                    if w == 8:
                        self.intrin(k, "_mm_storel_epi64", None, [(self.opnd(k, ops[0], VT["i"]), "i"), (v, "")])
                        return True
                    t = self.vtemp("1004")
                    self.intrin(k, "_mm_cvtsi128_si32", t, [(v, "")])
                    self.emit(k, "OPASSIGN", [self.opnd(k, ops[0], "1004")], [t])
                    return True
                d = self.dst(k, ops[0]).rsplit(":", 1)[0] + (":1004" if w == 4 else ":1008")
                self.intrin(k, "_mm_cvtsi128_si32" if w == 4 else "_mm_cvtsi128_si64", d, [(v, "")])
            return True
        if m == "pinsrw":
            a = self.vread(k, ops[0], "i")
            src = self.opnd(k, ops[1]).rsplit(":", 1)[0] + ":1004"
            self.vwrite(k, ops[0], "i", lambda d: self.intrin(k, "_mm_insert_epi16", d,
                                                              [(a, ""), (src, ""), ("%d:1004" % ops[2].imm, "")]))
            return True
        if m == "pextrw":
            v = self.vread(k, ops[1], "i")
            self.intrin(k, "_mm_extract_epi16", self.dst(k, ops[0]).rsplit(":", 1)[0] + ":1004",
                        [(v, ""), ("%d:1004" % ops[2].imm, "")])
            return True
        if m in ("comiss", "ucomiss", "cvtss2sd", "cvttss2si", "cvtss2si") or m in SSE_CMP:
            return False                              # scalar: the operand reads convert (xread)
        raise Unsupported("%s on a vector" % m)

    # ---- the driver
    def run(self):
        self.blocks()
        self.reaching()
        self.real_maybe = set()
        self.find_tls()
        pro = self.prologue()
        self.pro = pro
        self.find_out_slots()
        self.find_frame_region()
        # what the function reads of its own arguments (before the bodies are lifted: arity of tail calls)
        self.reg_params = set()
        for k, i in enumerate(self.ins):
            if k in pro:
                continue
            for r in self.acc[k][0]:
                if (r in ARGR or r in ARGX) and ("entry", r) in self.reach_defs.get((k, r), set()):
                    self.reg_params.add(ARGR.index(r) if r in ARGR else ARGX.index(r))
        for k, i in enumerate(self.ins):
            if k in pro:
                continue
            for r in self.acc[k][0]:
                if (r in ARGR or r in ARGX) and ("entry", r) in self.reach_defs.get((k, r), set()):
                    p = (ARGR.index(r) if r in ARGR else ARGX.index(r))
                    op = next((o for o in i.operands if o.type == X.X86_OP_REG and
                               canon(self.md.reg_name(o.reg)) == r), None)
                    if r in ARGX:
                        self.args_read.setdefault(p, self.src_ty(i) if self.is_sse(i) else "e004")
                    else:
                        sz = op.size if op is not None else 8
                        if op is None:          # an address register: a pointer
                            sz = 8
                        self.args_read.setdefault(p, "1008" if sz == 8 else "1004" if sz == 4 else
                                                  RTY.get(sz, "1008"))
            for op in i.operands:
                if op.type == X.X86_OP_MEM:
                    fo = self.frame_off(k, op, self.rsp_at[k], self.rbp_at[k])
                    if fo is not None and fo >= 8 and k not in pro:
                        p = (fo - 8) // 8 if fo < 0x28 else 4 + (fo - 0x28) // 8
                        if fo >= 0x28 or p in self.spilled or p in self.reg_params:
                            self.args_read.setdefault(p, MTY.get(op.size, "1008"))
        self.own_arity = max(list(self.args_read) + [-1]) + 1
        # pass 1: decide call arguments and returns (which maybe-reads are real), then the webs
        self.call_toks = {}
        for k, i in enumerate(self.ins):
            if i.mnemonic == "call" or self.is_tailjmp(i):
                self.call_toks[k] = None
        self.decide_calls()
        # an argument passed on untouched (the caller's rdx still in rdx at a call that takes 3): a parameter
        for (k, r) in self.real_maybe:
            if (r in ARGR or r in ARGX) and self.entry_only(k, r):
                p = ARGR.index(r) if r in ARGR else ARGX.index(r)
                self.args_read.setdefault(p, "1008" if r in ARGR else self.xmm_ty_at(k, r))
                self.reg_params.add(p)
        self.own_arity = max(list(self.args_read) + [-1]) + 1
        self.decide_ret()
        self.find_divs()
        self.webs()
        self.mark_vectors()
        for k in self.call_toks:
            self.call_toks[k] = self.call_args(k, tail=self.ins[k].mnemonic == "jmp")
        self.lift_body()
        self.rv_copies()
        # cl gives an address-taken local the first home slot no parameter has: a local in home slot h means
        # the source declared h parameters (the ones past what the code reads are unused)
        hs = sorted({int(m) for l in self.lines for m in re.findall(r"\b_lh(\d+)", l)})
        if hs and hs[0] > self.own_arity and not self.opts.get("nopad"):
            self.own_arity = hs[0]
        return self.lines

    def decide_calls(self):
        for k in self.call_toks:
            self.call_args(k, tail=self.ins[k].mnemonic == "jmp", decide=True)

    def decide_ret(self):
        """rax / xmm0 at a ret is the return value when every path writes it in this function (a call's result
        counts: `call f; ret` is `return f()` when some other ret returns a value of its own)."""
        rets = [k for k, i in enumerate(self.ins) if self.is_ret(i)]
        tails = [k for k in self.call_toks if self.ins[k].mnemonic == "jmp"]
        own = lambda k, r: any(d[0] not in ("entry", "undef") and self.ins[d[0]].mnemonic != "call"
                               for d in self.reach_defs.get((k, r), set()))
        any_x = any(own(k, "xmm0") for k in rets)
        any_r = any(own(k, "rax") for k in rets)
        self.ret_reg = "xmm0" if any_x and not any_r else "rax" if any_r else None
        self.ret_both = any_x and any_r
        # ambiguous: rax left over from a call on some path, or own defs that are read elsewhere too
        clob = any(any(d[0] not in ("entry", "undef") and self.ins[d[0]].mnemonic == "call"
                       for d in self.reach_defs.get((k, "rax"), set())) for k in rets) or bool(tails)
        self.ret_ambiguous = clob or self.ret_reg is not None
        mode = self.opts.get("ret")
        if mode == "void":
            self.ret_reg = None
        elif mode == "rax":
            self.ret_reg = "rax"
        elif mode == "xmm0":
            self.ret_reg = "xmm0"
        if self.ret_reg:
            for k in rets:
                self.real_maybe.add((k, self.ret_reg))

    def rv_copies(self):
        """`t1999 = <def>` after the IL of every instruction whose rax value reaches a ret (retvar)."""
        if not getattr(self, "rv_defs", None):
            return
        w = self.ret_ty or "1008"
        last = {}
        for n, l in enumerate(self.lines):
            if l.startswith("I "):
                last[int(l.split()[1], 16)] = n
        ins_at = []
        for k, r in sorted(self.rv_defs):
            a = self.ins[k].address
            if a not in last:
                continue
            i = self.ins[k]
            dw = RTY.get(i.operands[0].size, w) if i.operands and i.operands[0].type == X.X86_OP_REG and \
                i.mnemonic != "call" else w
            src = "%s:%s" % (self.var_written(k, "rax"), dw)
            ins_at.append((last[a], "I %x OPASSIGN 0 0 | t1999:%s <- %s" % (a, w, src)))
        for n, l in sorted(ins_at, reverse=True):
            at = n + 1
            # not after a branch or goto the def's instruction ended with (a call's result, then its jmp)
            self.lines.insert(at, l)

    def byte_webs(self):
        """Web names (t10xx) whose every def writes a byte (mov r8, .. / xor r8, r8 / movzx r, byte / setcc) and
        whose every read is an 8-bit register read: the source's unsigned char / bool. Typed wider, cl allocates the
        web as a dword (a zero def becomes `mov ecx, edi` instead of `xor cl, cl`; docs/c2/immcand.md 7)."""
        B8 = {"al", "bl", "cl", "dl", "sil", "dil", "bpl", "spl"} | {"r%db" % n for n in range(8, 16)}
        def byte_def(k, r):
            i = self.ins[k]
            o = i.operands
            if not o or o[0].type != X.X86_OP_REG or canon(self.md.reg_name(o[0].reg)) != r:
                return False
            if o[0].size == 1:
                return True
            return i.mnemonic == "movzx" and len(o) == 2 and o[1].size == 1
        defs, ok = {}, {}
        for key in list(self.names):
            if not isinstance(key[0], int):
                continue
            root = self.find(key)
            nm = self.names.get(root)
            if not nm or not nm.startswith("t1"):
                continue
            defs.setdefault(nm, []).append(key)
            ok[nm] = ok.get(nm, True) and byte_def(*key)
        for (k, r), ds in self.reach_defs.items():
            if not isinstance(k, int) or not ds:
                continue
            nms = {self.names.get(self.find(d)) for d in ds}
            for nm in nms:
                if nm not in ok or not ok[nm]:
                    continue
                i = self.ins[k]
                rd = [x for x in i.operands if x.type == X.X86_OP_REG and canon(self.md.reg_name(x.reg)) == r]
                mem = [x for x in i.operands if x.type == X.X86_OP_MEM and
                       r in (canon(self.md.reg_name(x.mem.base)) if x.mem.base else None,
                             canon(self.md.reg_name(x.mem.index)) if x.mem.index else None)]
                if mem or not rd or any(self.md.reg_name(x.reg) not in B8 for x in rd):
                    if r in self.acc[k][0] or rd or mem:
                        ok[nm] = False
        return sorted(n for n, v in ok.items() if v)

    def zero_reg(self, k, r):
        """Register r at k holds 0: its one reaching def is `xor r, r` (any width)."""
        ds = self.reach_defs.get((k, r), set())
        if len(ds) != 1:
            return False
        d = next(iter(ds))[0]
        if d in ("entry", "undef") or not isinstance(d, int):
            return False
        z = self.ins[d]
        return z.mnemonic == "xor" and len(z.operands) == 2 and z.operands[0].type == X.X86_OP_REG and \
            z.operands[1].type == X.X86_OP_REG and z.operands[0].reg == z.operands[1].reg

    def is_zero(self, k, op):
        """op (a register operand at k) holds 0: `xor z, z` is its one reaching def."""
        if op.type != X.X86_OP_REG:
            return False
        ds = self.reach_defs.get((k, canon(self.md.reg_name(op.reg))), set())
        if len(ds) != 1:
            return False
        d = next(iter(ds))[0]
        if d in ("entry", "undef"):
            return False
        z = self.ins[d]
        return z.mnemonic == "xor" and z.operands[0].type == X.X86_OP_REG and z.operands[1].type == X.X86_OP_REG \
            and z.operands[0].reg == z.operands[1].reg

    def zeroes(self, k, r):
        """The instruction at k sets register r to 0 (xor r, r / mov r32, zero-register)."""
        i = self.ins[k]
        if not i.operands or i.operands[0].type != X.X86_OP_REG or canon(self.md.reg_name(i.operands[0].reg)) != r:
            return False
        if i.mnemonic == "xor" and i.operands[1].type == X.X86_OP_REG and i.operands[1].reg == i.operands[0].reg:
            return True
        return i.mnemonic == "mov" and self.is_zero(k, i.operands[1])

    def nullcast_at(self, k):
        """cl's null-checked pointer adjustment (a static_cast between classes at different offsets):
             lea r, [x+D]; test x, x; jne L; <r = 0>; L:            or
             test x, x; je Z; lea r, [x+D]; jmp L; Z: <r = 0>; L:
        -> (r, x, D, instructions to skip)."""
        if self.opts.get("nonullcast"):
            return None
        ins, n = self.ins, len(self.ins)
        def lea(q):
            i = ins[q]
            if i.mnemonic != "lea" or i.operands[1].mem.index or not i.operands[1].mem.base:
                return None
            x = canon(self.md.reg_name(i.operands[1].mem.base))
            r = canon(self.md.reg_name(i.operands[0].reg))
            d = i.operands[1].mem.disp
            return (r, x, d) if d and r != x and x not in ("rsp", "rbp", "rip") and i.operands[0].size == 8 else None
        def tests(q, x):
            i = ins[q]
            return i.mnemonic == "test" and all(o.type == X.X86_OP_REG and canon(self.md.reg_name(o.reg)) == x
                                                and o.size == 8 for o in i.operands)
        if k + 3 < n:
            l = lea(k)
            if l and tests(k + 1, l[1]) and ins[k + 2].mnemonic == "jne" and \
                    ins[k + 2].operands[0].imm == ins[k + 4].address if k + 4 < n else False:
                if self.zeroes(k + 3, l[0]):
                    return l[0], l[1], l[2], [k + 1, k + 2, k + 3]
        if k + 5 < n and ins[k].mnemonic == "test" and ins[k + 1].mnemonic == "je":
            l = lea(k + 2)
            if l and tests(k, l[1]) and ins[k + 3].mnemonic == "jmp" and ins[k + 1].operands[0].imm == \
                    ins[k + 4].address and ins[k + 3].operands[0].imm == ins[k + 5].address and self.zeroes(k + 4, l[0]):
                return l[0], l[1], l[2], [k + 1, k + 2, k + 3, k + 4]
        return None

    def lift_body(self):
        flags = None
        labels = {self.ins[b[0]].address for n, b in enumerate(self.bl) if n and
                  any(n in s for s in self.succ.values())}
        sw_skip = set()
        for sw in self.switches.values():
            sw_skip |= sw["skip"] | {sw["ja"]}
        for bn, b in enumerate(self.bl):
            a0 = self.ins[b[0]].address
            if a0 in labels:
                self.lines.append("L %x" % a0)
            for k in b:
                i = self.ins[k]
                m, ops = i.mnemonic, i.operands
                if k in self.pro or k in self.arg_store or k in self.tls_skip or k in self.div_skip:
                    continue
                nc = self.nullcast_at(k)
                if nc:
                    r, x, delta, skip = nc
                    nm = "nullcast_%s%d" % ("down" if delta < 0 else "up", abs(delta))
                    self.intrin(k, nm, "%s:1008" % self.var_written(k, r), [("%s:1008" % self.var_read(k, x), "")])
                    self.div_skip |= set(skip)
                    flags = None
                    continue
                if k in self.div_at:
                    D, ty = self.div_at[k]
                    x = self.opnd(k, ops[0])
                    q = self.temp(ty)
                    self.emit(k, "OPDIV", [q], [x.rsplit(":", 1)[0] + ":" + ty, "%d:%s" % (D, ty)])
                    self.div_q = getattr(self, "div_q", {})
                    self.div_q[k] = q
                    continue
                if k in self.div_res:
                    r, mk = self.div_res[k]
                    q = getattr(self, "div_q", {}).get(mk)
                    if q is None:
                        raise Unsupported("division result before its multiply")
                    sz = ops[0].size
                    self.emit(k, "OPASSIGN", ["%s:%s" % (self.var_written(k, r), RTY.get(sz, "1008"))], [q])
                    flags = None
                    continue
                if i.address in sw_skip:
                    continue
                sw = next((x for x in self.switches.values() if i.address == x["cmp"]), None)
                if sw is not None:
                    sw["idxtok"] = "%s:2004" % self.var_read(k, sw["idx"])
                    continue
                if i.address in self.switches:
                    sw = self.switches[i.address]
                    cases = ",".join("%d:L%x" % (v, t) for v, t in sw["cases"])
                    self.emit(k, "OPSWITCH", [], [sw["idxtok"]], "sw=%s def=L%x" % (cases, sw["default"]))
                    continue
                if m == "call" or self.is_tailjmp(i):
                    self.lift_call(k, i)
                    flags = None
                    continue
                if self.is_ret(i):
                    if self.ret_reg == "rax":
                        self.value_ret = True
                        w = self.width_at(k, "rax")
                        self.ret_ty = w if self.ret_ty in (None, w) else "1008"
                        if self.rv_defs:
                            self.emit(k, "OPRET", [], ["t1999:%s" % w])
                        else:
                            self.emit(k, "OPRET", [], ["%s:%s" % (self.var_read(k, "rax"), w)])
                    elif self.ret_reg == "xmm0":
                        self.value_ret = True
                        t = self.xmm_ty_at(k, "xmm0")
                        self.float_ret = t
                        self.emit(k, "OPRET", [], [self.xread(k, "xmm0", t)])
                    else:
                        self.emit(k, "OPRET", [], [])
                    continue
                if m == "jmp":
                    tgt = ops[0]
                    if tgt.type == X.X86_OP_IMM:
                        self.emit(k, "OPGOTO", [], ["s0:0", "->"], "s0=L%x" % tgt.imm)
                        continue
                    raise Unsupported("indirect jmp")
                if m in CCOF or m in ("jp", "jnp"):
                    if m in ("jp", "jnp"):
                        raise Unsupported("parity branch (a float compare)")
                    t = self.temp("9000")
                    a, b2, fl = flags or ("0:1004", "0:1004", "i")
                    cc = self.cc_of(m, fl)
                    if cc is None:
                        raise Unsupported("%s after %s" % (m, {"f": "a float compare", "bt": "bt"}.get(fl, fl)))
                    self.emit(k, "OPCMP", [t], [a, b2])
                    self.emit(k, "OPBRANCH", [], ["s0:0", t, "->"], "cc=%s s0=L%x" % (cc, ops[0].imm))
                    continue
                if m in ("cmp", "test"):
                    a, b2 = self.opnd(k, ops[0]), self.opnd(k, ops[1])
                    if m == "test":
                        if a == b2:
                            b2 = "0:%s" % a.rsplit(":", 1)[1]
                        else:
                            t = self.temp(a.rsplit(":", 1)[1])
                            self.emit(k, "and", [t], [a, b2])
                            a, b2 = t, "0:%s" % a.rsplit(":", 1)[1]
                    flags = (a, b2, "i")
                    continue
                if m in SSE_CMP:
                    flags = (self.opnd(k, ops[0]), self.opnd(k, ops[1]), "f")
                    continue
                if m in ("mov", "movzx", "movabs"):
                    s = self.opnd(k, ops[1])
                    self.emit(k, "OPASSIGN", [self.dst(k, ops[0])], [s])
                    continue
                if m in ("movsx", "movsxd"):
                    s = self.opnd(k, ops[1])
                    sty = {1: "1001", 2: "1002", 4: "1004"}[ops[1].size]
                    self.emit(k, "OPASSIGN", [self.dst(k, ops[0])], [s.rsplit(":", 1)[0] + ":" + sty])
                    continue
                if m in ("cdqe",):
                    self.emit(k, "OPASSIGN", ["%s:1008" % self.var_written(k, "rax")],
                              ["%s:1004" % self.var_read(k, "rax")])
                    continue
                if k in self.tls_addr:
                    self.emit(k, "OPASSIGN", [self.dst(k, ops[0])], ["?gta_%x@@3HA:1008" % self.tls_addr[k]])
                    continue
                if m == "lea":
                    self.lea(k, ops)
                    continue
                if m in ARITH:
                    d = self.dst(k, ops[0])
                    if m in ("xor", "sub") and ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_REG and \
                            ops[0].reg == ops[1].reg:
                        self.emit(k, "OPASSIGN", [d], ["0:%s" % d.rsplit(":", 1)[1]])
                        flags = (d, "0:%s" % d.rsplit(":", 1)[1], "i")
                        continue
                    if m == "or" and ops[0].type == X.X86_OP_REG and len(ops) > 1 and ops[1].type == X.X86_OP_IMM and \
                            (ops[1].imm & ((1 << (8 * ops[0].size)) - 1)) == (1 << (8 * ops[0].size)) - 1 and \
                            not self.opts.get("orm1"):
                        # `or r, -1`: cl's short load of the constant -1 (the register's old value is not read)
                        ty = d.rsplit(":", 1)[1]
                        self.emit(k, "OPASSIGN", [d], ["-1:%s" % ("1004" if ty == "2004" else ty)])
                        flags = (d, "0:%s" % ty, "i")
                        continue
                    a = self.opnd(k, ops[0])
                    b2 = self.opnd(k, ops[1]) if len(ops) > 1 else "1:1004"
                    if m in ("shl", "shr", "sar") and len(ops) > 1 and ops[1].type == X.X86_OP_REG:
                        b2 = "%s:1004" % self.var_read(k, "rcx")    # a C shift count is an int (cl)
                    self.emit(k, ARITH[m], [d], [a, b2])
                    flags = (d, "0:%s" % d.rsplit(":", 1)[1], "i")
                    if m in ("sub",):
                        flags = (a, b2, "i")
                    continue
                if m == "imul":
                    d = self.dst(k, ops[0])
                    if len(ops) == 3:
                        srcs = [self.opnd(k, ops[1]), self.opnd(k, ops[2])]
                    elif len(ops) == 2:
                        srcs = [self.opnd(k, ops[0]), self.opnd(k, ops[1])]
                    else:
                        raise Unsupported("one-operand imul")
                    self.emit(k, "OPMUL", [d], srcs)
                    continue
                if m in ("inc", "dec"):
                    d = self.dst(k, ops[0])
                    self.emit(k, "add" if m == "inc" else "sub", [d], [self.opnd(k, ops[0]), "1:1004"])
                    flags = (d, "0:%s" % d.rsplit(":", 1)[1], "i")
                    continue
                if m in ("neg", "not"):
                    d = self.dst(k, ops[0])
                    a = self.opnd(k, ops[0])
                    self.emit(k, m, [d], [a])
                    flags = (d, "0:%s" % d.rsplit(":", 1)[1], "i")
                    if m == "neg":                  # CF = (x != 0), ZF = (x == 0)
                        flags = (a, "0:%s" % a.rsplit(":", 1)[1], "neg")
                    continue
                if m == "sbb" and ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_REG and \
                        ops[0].reg == ops[1].reg:
                    # r = -(CF): all ones when the carry is set
                    a, b2, fl = flags or (None, None, None)
                    cc = self.cc_of("jb", fl) if flags else None
                    if cc is None:
                        raise Unsupported("sbb without a compare")
                    d = self.dst(k, ops[0])
                    t = self.temp("9000")
                    self.emit(k, "OPCMP", [t], [a, b2])
                    if self.opts.get("sbb") == "neg":
                        self.emit(k, "OPSETCC", [d], [t], "cc=%s" % cc)
                        self.emit(k, "neg", [d], [d])
                    else:                           # cond ? -1 : 0, cl's own reading of `c ? x : x - 1`
                        ty = d.rsplit(":", 1)[1]
                        lt = "1004" if ty == "2004" else ty
                        self.emit(k, "OPCMOV", [d], [t, "-1:%s" % lt, "0:%s" % lt], "cc=%s" % cc)
                    flags = (d, "0:%s" % d.rsplit(":", 1)[1], "i")
                    continue
                if m == "bt" and ops[0].type == X.X86_OP_REG or m == "bt" and ops[1].type == X.X86_OP_IMM:
                    a = self.opnd(k, ops[0])
                    ty = a.rsplit(":", 1)[1]
                    lt = "1008" if ty == "1008" else "1004"
                    if ops[1].type == X.X86_OP_IMM:
                        t = self.temp(ty)
                        self.emit(k, "and", [t], [a, "%d:%s" % (1 << ops[1].imm, "1008" if ops[1].imm >= 31 else lt)])
                    else:
                        b2 = "%s:1004" % self.var_read(k, canon(self.md.reg_name(ops[1].reg)))
                        t0 = self.temp(ty)
                        if self.opts.get("bt") == "mask":
                            self.emit(k, "shl", [t0], ["1:%s" % lt, b2])
                            t = self.temp(ty)
                            self.emit(k, "and", [t], [a, t0])
                        else:
                            self.emit(k, "shr" if ty[0] == "2" else "sar", [t0], [a, b2])
                            t = self.temp(ty)
                            self.emit(k, "and", [t], [t0, "1:%s" % lt])
                    flags = (t, "0:%s" % ty, "bt")
                    continue
                if self.vec_insn(k, i):
                    continue
                if m in ("movq", "movd") and ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_REG and \
                        canon(self.md.reg_name(ops[0].reg)) in ARGR and self.md.reg_name(ops[1].reg) in ARGX and \
                        ARGR.index(canon(self.md.reg_name(ops[0].reg))) == ARGX.index(self.md.reg_name(ops[1].reg)):
                    g = canon(self.md.reg_name(ops[0].reg))
                    real = [kk for (kk, r), ds in self.reach_defs.items() if r == g and (k, g) in ds and
                            r in self.acc[kk][0]]
                    nxt = next((kk for kk in range(k + 1, len(self.ins)) if self.ins[kk].mnemonic == "call"), None)
                    if not real and nxt is not None and (k, g) in self.reach_defs.get((nxt, g), set()):
                        self.vararg = getattr(self, "vararg", {})   # a variadic call: the double goes in both
                        p = ARGR.index(g)
                        self.vararg[nxt] = min(self.vararg.get(nxt, p), p)
                        continue
                if m == "sqrtps" and ops[1].type == X.X86_OP_REG and self.opts.get("sqrt") == "f":
                    self.intrin(k, "sqrtf", self.dst(k, ops[0], "e004"), [(self.opnd(k, ops[1], "e004"), "")])
                    continue                        # /fp:fast: sqrtf is the bare sqrtps
                if m == "sqrtps" and ops[1].type == X.X86_OP_REG:     # cl's sqrtf
                    # sqrtf(x) with errno checks becomes a call; _mm_sqrt_ps of the scalar is the bare sqrtps
                    a = self.opnd(k, ops[1], "e004")
                    t1, t2 = self.vtemp(VT["f"]), self.vtemp(VT["f"])
                    self.intrin(k, "_mm_set_ss", t1, [(a, "")])
                    self.intrin(k, "_mm_sqrt_ps", t2, [(t1, "")])
                    self.intrin(k, "_mm_cvtss_f32", self.dst(k, ops[0], "e004"), [(t2, "")])
                    continue
                if self.is_sse(i) or m in NEUTRAL:
                    flags = self.sse(k, i, flags)
                    continue
                if m.startswith("set") and ("j" + m[3:]) in CCOF or m.startswith("cmov") and ("j" + m[4:]) in CCOF:
                    jm = "j" + (m[3:] if m.startswith("set") else m[4:])
                    a, b2, fl = flags or ("0:1004", "0:1004", "i")
                    cc = self.cc_of(jm, fl)
                    if cc is None:
                        raise Unsupported("%s after %s" % (m, {"f": "a float compare", "bt": "bt"}.get(fl, fl)))
                    t = self.temp("9000")
                    self.emit(k, "OPCMP", [t], [a, b2])
                    if m.startswith("set"):
                        self.emit(k, "OPSETCC", [self.dst(k, ops[0])], [t], "cc=%s" % cc)
                    else:
                        self.emit(k, "OPCMOV", [self.dst(k, ops[0])], [t, self.opnd(k, ops[1]), self.opnd(k, ops[0])],
                                  "cc=%s" % cc)
                    continue
                if m.startswith("set") or m.startswith("cmov"):
                    raise Unsupported("condition %s" % m)
                if m in ("lock inc", "lock dec", "lock xadd", "lock cmpxchg", "xchg", "lock add", "lock or",
                         "lock and", "lock xchg"):
                    self.atomic(k, i, m, ops)
                    flags = self.atomic_flags if m in ("lock cmpxchg", "lock inc", "lock dec") or \
                        getattr(self, "xadd_fused", False) else None
                    continue
                if m in ("cdq", "cqo"):
                    continue
                if m in ("idiv", "div"):
                    w = ops[0].size
                    ty = ("1" if m == "idiv" else "2") + ("008" if w == 8 else "004")
                    num = "%s:%s" % (self.var_read(k, "rax"), ty)
                    den = self.opnd(k, ops[0]).rsplit(":", 1)[0] + ":" + ty
                    self.emit(k, "OPDIV", ["%s:%s" % (self.var_written(k, "rax"), ty)], [num, den])
                    self.emit(k, "OPMOD", ["%s:%s" % (self.var_written(k, "rdx"), ty)], [num, den])
                    continue
                if m == "rdtsc":
                    # cl's __rdtsc(): rdtsc; shl rdx, 32; or rax, rdx
                    n1 = self.ins[k + 1] if k + 1 < len(self.ins) else None
                    n2 = self.ins[k + 2] if k + 2 < len(self.ins) else None
                    if n1 is None or n2 is None or (n1.mnemonic, n1.op_str) != ("shl", "rdx, 0x20") or \
                            (n2.mnemonic, n2.op_str) != ("or", "rax, rdx"):
                        raise Unsupported("rdtsc without its combine")
                    self.intrin(k, "__rdtsc", "%s:2008" % self.var_written(k + 2, "rax"), [])
                    self.div_skip |= {k + 1, k + 2}
                    continue
                if m in ("rol", "ror") and ops[0].type == X.X86_OP_REG and ops[0].size in (1, 2, 4, 8):
                    nm = {1: "_rot%s8", 2: "_rot%s16", 4: "_rot%s", 8: "_rot%s64"}[ops[0].size] % m[2]
                    ty = {1: "2001", 2: "2002", 4: "2004", 8: "2008"}[ops[0].size]
                    src = self.opnd(k, ops[0], ty)
                    if ops[1].type == X.X86_OP_IMM:
                        cnt = "%d:2001" % ops[1].imm
                    elif ops[1].type == X.X86_OP_REG and self.md.reg_name(ops[1].reg) == "cl":
                        cnt = "%s:2001" % self.var_read(k, "rcx")
                    else:
                        raise Unsupported("rotate count")
                    self.intrin(k, nm, self.dst(k, ops[0], ty), [(src, ""), (cnt, "")])
                    flags = None
                    continue
                if m in ("nop", "int3", "pause") or m.startswith(("npad", "prefetch")):
                    continue
                if m in ("mfence", "lfence", "sfence"):
                    fn = "?__lift_mm_%s@@YAXXZ" % m
                    self.emit(k, "OPCALL", [], [fn + ":1008"], "s0=" + fn)
                    continue
                if m in ("bts", "btr", "btc") and ops[1].type == X.X86_OP_IMM:
                    d = self.dst(k, ops[0])
                    ty = d.rsplit(":", 1)[1]
                    bit = 1 << ops[1].imm
                    if m == "btr":
                        mask = ~bit & ((1 << (8 * ops[0].size)) - 1)
                        mask = mask - (1 << 64) if ops[0].size == 8 and mask >> 63 else mask
                        mask = mask - (1 << 32) if ops[0].size == 4 and mask >> 31 else mask
                        self.emit(k, "and", [d], [self.opnd(k, ops[0]), "%d:%s" % (mask, ty if ty != "2004" else "1004")])
                    else:
                        self.emit(k, "or" if m == "bts" else "xor", [d],
                                  [self.opnd(k, ops[0]), "%d:%s" % (bit, ty if ty != "2004" else "1004")])
                    flags = None
                    continue
                raise Unsupported("instruction %s" % m)

    def const_of(self, k, r, depth=0):
        """The constant register r holds at k: every reaching def is `mov r, imm` / `or r, -1` of one value, or a
        register copy of such a value (cl hoists a -1 into a callee-saved register and copies it in a loop)."""
        ds = self.reach_defs.get((k, r), set())
        vals = set()
        for d in ds:
            if d[0] in ("entry", "undef"):
                return None
            i = self.ins[d[0]]
            o = i.operands
            if i.mnemonic == "or" and len(o) == 2 and o[1].type == X.X86_OP_IMM and \
                    (o[1].imm & ((1 << (8 * o[0].size)) - 1)) == (1 << (8 * o[0].size)) - 1:
                vals.add(-1)
            elif i.mnemonic == "mov" and len(o) == 2 and o[1].type == X.X86_OP_IMM:
                v = o[1].imm & ((1 << (8 * o[0].size)) - 1)
                vals.add(v - (1 << (8 * o[0].size)) if v >> (8 * o[0].size - 1) else v)
            elif i.mnemonic == "mov" and len(o) == 2 and o[1].type == X.X86_OP_REG and depth < 2:
                vals.add(self.const_of(d[0], canon(self.md.reg_name(o[1].reg)), depth + 1))
            else:
                return None
        return vals.pop() if len(vals) == 1 else None

    def atomic(self, k, i, m, ops):
        """lock-prefixed read-modify-writes: the _Interlocked* intrinsics."""
        if ops[0].type != X.X86_OP_MEM:
            if m == "xchg" and ops[1].type == X.X86_OP_REG:
                raise Unsupported("xchg of registers")
            ops = list(reversed(ops)) if m == "xchg" else ops
            if ops[0].type != X.X86_OP_MEM:
                raise Unsupported("atomic on a register")
        w = ops[0].size
        sfx = {4: "", 8: "64", 2: "16", 1: "8"}[w]
        ty = {4: "1004", 8: "1008", 2: "1002", 1: "1001"}[w]
        mt = self.opnd(k, ops[0])
        addr = self.temp("1008")
        self.emit(k, "OPASSIGN", [addr], [mt], "s0=&")
        a1 = self.temp("1008")
        self.emit(k, "OPARG", [a1], [addr])
        if m in ("lock inc", "lock dec"):
            fn = "?__lift_Interlocked%s%s@@YAXXZ" % ("Increment" if m == "lock inc" else "Decrement", sfx)
            res = self.temp(ty)                       # the new value: what the flags test (`lock dec; jne`)
            self.emit(k, "OPCALL", [res], [fn + ":1008", a1], "s0=" + fn)
            self.atomic_flags = (res, "0:%s" % ty, "i")
            return
        if m in ("lock add", "lock or", "lock and") and ops[1].type == X.X86_OP_IMM or m in ("lock add", "lock or",
                                                                                             "lock and"):
            fn = "?__lift_Interlocked%s%s@@YAXXZ" % ({"lock add": "ExchangeAdd", "lock or": "Or",
                                                     "lock and": "And"}[m], sfx)
            a2 = self.temp(ty)
            self.emit(k, "OPARG", [a2], [self.opnd(k, ops[1]).rsplit(":", 1)[0] + ":" + ty])
            self.emit(k, "OPCALL", [], [fn + ":1008", a1, a2], "s0=" + fn)
            return
        self.xadd_fused = False
        if m == "lock xadd" and ops[1].type == X.X86_OP_REG:
            r = canon(self.md.reg_name(ops[1].reg))
            c = self.const_of(k, r)
            nx = self.ins[k + 1] if k + 1 < len(self.ins) else None
            step = None
            if nx is not None and nx.operands and nx.operands[0].type == X.X86_OP_REG and \
                    canon(self.md.reg_name(nx.operands[0].reg)) == r and nx.operands[0].size == w:
                if nx.mnemonic in ("dec", "inc") and len(nx.operands) == 1:
                    step = -1 if nx.mnemonic == "dec" else 1
                elif nx.mnemonic in ("add", "sub") and len(nx.operands) == 2 and nx.operands[1].type == X.X86_OP_IMM:
                    v = nx.operands[1].imm
                    v = v - (1 << 64) if v >> 63 else v
                    step = v if nx.mnemonic == "add" else -v
            if c in (-1, 1) and step == c and not self.opts.get("noxaddfuse"):
                # cl's _InterlockedDecrement / _InterlockedIncrement: t = -1; lock xadd [m], t; t = t - 1. With the
                # xadd's result as a variable of its own, the chain splits from a hoisted -1 and takes another
                # register (docs/c2/port-1700.md 13)
                fn = "?__lift_Interlocked%s%s@@YAXXZ" % ("Decrement" if c < 0 else "Increment", sfx)
                res = "%s:%s" % (self.var_written(k + 1, r), ty)
                self.emit(k, "OPCALL", [res], [fn + ":1008", a1], "s0=" + fn)
                self.div_skip.add(k + 1)
                self.atomic_flags = (res, "0:%s" % ty, "i")
                self.xadd_fused = True
                return
        if m in ("lock xadd", "xchg", "lock xchg"):
            fn = "?__lift_Interlocked%s%s@@YAXXZ" % ("ExchangeAdd" if m == "lock xadd" else "Exchange", sfx)
            r = canon(self.md.reg_name(ops[1].reg))
            a2 = self.temp(ty)
            self.emit(k, "OPARG", [a2], ["%s:%s" % (self.var_read(k, r), ty)])
            self.emit(k, "OPCALL", ["%s:%s" % (self.var_written(k, r), ty)], [fn + ":1008", a1, a2], "s0=" + fn)
            return
        if m == "lock cmpxchg":
            fn = "?__lift_InterlockedCompareExchange%s@@YAXXZ" % sfx
            r = canon(self.md.reg_name(ops[1].reg))
            a2, a3 = self.temp(ty), self.temp(ty)
            old = "%s:%s" % (self.var_read(k, "rax"), ty)
            self.emit(k, "OPARG", [a2], ["%s:%s" % (self.var_read(k, r), ty)])
            self.emit(k, "OPARG", [a3], [old])
            new = "%s:%s" % (self.var_written(k, "rax"), ty)
            self.emit(k, "OPCALL", [new], [fn + ":1008", a1, a2, a3], "s0=" + fn)
            self.atomic_flags = (new, old, "i")
            return
        raise Unsupported("atomic %s" % m)

    def lift_call(self, k, i):
        toks = self.call_toks[k]
        tgt = i.operands[0]
        targs = []
        for x in toks:
            t = self.temp(x.rsplit(":", 1)[1])
            self.emit(k, "OPARG", [t], [x])
            targs.append(t)
        if tgt.type == X.X86_OP_IMM:
            fn = "?FUN_%x@@YAXXZ" % tgt.imm
            callee, note = fn + ":1008", "s0=" + fn
            if k in getattr(self, "vararg", {}):
                note += " va=%d" % self.vararg[k]
        else:
            callee, note = self.opnd(k, tgt), ""
        tail = i.mnemonic == "jmp"
        d = []
        if tail:
            if self.ret_reg:
                ty = (self.ret_ty or "1008") if self.ret_reg == "rax" else (self.float_ret or "e004")
                r = self.temp(ty)
                self.emit(k, "OPCALL", [r], [callee] + targs, note)
                self.emit(k, "OPRET", [], [r])
                self.value_ret = True
            else:
                self.emit(k, "OPCALL", [], [callee] + targs, note)
                self.emit(k, "OPRET", [], [])
            return
        used_r = self.result_used(k, "rax")
        used_x = self.result_used(k, "xmm0")
        if used_x and not used_r:
            d = ["%s:%s" % (self.var_written(k, "xmm0"), used_x)]
        elif used_r:
            d = ["%s:%s" % (self.var_written(k, "rax"), used_r)]
        self.emit(k, "OPCALL", d, [callee] + targs, note)

    def result_used(self, k, r):
        """The type a call's result is read with, when something reads it (None when nothing does)."""
        tys = set()
        for (kk, rr), ds in self.reach_defs.items():
            if rr != r or (k, r) not in ds:
                continue
            if r in self.maybe_reads(kk) and r not in self.acc[kk][0] and (kk, r) not in self.real_maybe:
                continue
            i = self.ins[kk]
            op = next((o for o in i.operands if o.type == X.X86_OP_REG and canon(self.md.reg_name(o.reg)) == r),
                      None)
            if r == "xmm0":
                tys.add(self.sse_ty(i) if self.is_sse(i) else "e004")
            else:
                tys.add(RTY.get(op.size, "1008") if op is not None else "1008")
        if not tys:
            return None
        if r == "xmm0":
            return "e008" if tys == {"e008"} else "e004"
        if tys <= {"2001"}:
            return "2001"
        if tys <= {"2004", "2001", "2002"}:
            return "1004"
        return "1008"

    def lea(self, k, ops):
        i = self.ins[k]
        d = self.dst(k, ops[0])
        m = ops[1].mem
        base = self.md.reg_name(m.base) if m.base else None
        idx = self.md.reg_name(m.index) if m.index else None
        if base == "rip":
            self.emit(k, "OPASSIGN", [d], ["?ga_%x@@3HA:1008" % self.gaddr(i, m)])
            return
        fo = self.frame_off(k, ops[1], self.rsp_at[k], self.rbp_at[k])
        fr = self.frame_region
        if fo is not None and fr and fr[0] <= fo < fr[1]:
            if fo == fr[0]:
                self.emit(k, "OPASSIGN", [d], ["_frame:1008"])
            else:
                self.emit(k, "add", [d], ["_frame:1008", "%d:1008" % (fo - fr[0])])
            return
        if fo is not None:
            self.emit(k, "OPASSIGN", [d], [self.frame_name(fo, "1008")], "s0=&")
            return
        ty = d.rsplit(":", 1)[1]
        terms = []
        if base:
            terms.append("%s:%s" % (self.var_read(k, canon(base)), ty))
        if idx:
            iv = "%s:%s" % (self.var_read(k, canon(idx)), ty)
            if m.scale > 1:
                t = self.temp(ty)
                self.emit(k, "shl", [t], [iv, "%d:1004" % {2: 1, 4: 2, 8: 3}[m.scale]])
                iv = t
            terms.append(iv)
        if m.disp:
            terms.append("%d:%s" % (m.disp, "1004" if ty == "2004" else ty))
        if not terms:
            terms = ["0:1004"]
        if len(terms) == 1:
            self.emit(k, "OPASSIGN", [d], terms)
            return
        acc = terms[0]
        for n, t in enumerate(terms[1:]):
            out = d if n == len(terms) - 2 else self.temp(ty)
            self.emit(k, "add", [out], [acc, t])
            acc = out

    def mask_idiom(self, k, i, flags):
        """xorps x, [sign mask] is -x; andps x, [abs mask] is fabsf(x) (both on a scalar float)."""
        m, ops = i.mnemonic, i.operands
        mem = ops[1].mem
        if ops[1].type != X.X86_OP_MEM or not mem.base or self.md.reg_name(mem.base) != "rip":
            raise Unsupported("%s with a non-constant" % m)
        b = self.img.read(self.gaddr(i, mem), 8)
        dbl = m.endswith("pd")
        lo = int.from_bytes(b[:8] if dbl else b[:4], "little")
        ty = "e008" if dbl else "e004"
        sign = 1 << (63 if dbl else 31)
        d, a = self.dst(k, ops[0], ty), self.opnd(k, ops[0], ty)
        if m.startswith("xor") and lo == sign:
            self.emit(k, "neg", [d], [a])
            return flags
        if m.startswith("and") and lo == sign - 1:
            self.intrin(k, "fabs" if dbl else "fabsf", d, [(a, "")])
            self.math_used = True
            return flags
        raise Unsupported("%s with a constant 0x%x" % (m, lo))

    def sse(self, k, i, flags):
        m, ops = i.mnemonic, i.operands
        if m in SSE_MOVES:
            ty = SSE_MOVES[m] if m in ("movss", "movsd") else None
            if ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_REG and m in ("movss", "movsd"):
                raise Unsupported("movss between registers (a lane merge)")
            self.emit(k, "OPASSIGN", [self.dst(k, ops[0], ty)], [self.opnd(k, ops[1], ty)])
            return flags
        if m in SSE_OPS:
            op, ty = SSE_OPS[m]
            self.emit(k, op, [self.dst(k, ops[0], ty)], [self.opnd(k, ops[0], ty), self.opnd(k, ops[1], ty)])
            return flags
        if m in ("xorps", "xorpd"):
            if ops[1].type == X.X86_OP_REG and ops[1].reg == ops[0].reg:
                self.emit(k, "OPASSIGN", [self.dst(k, ops[0])], ["0:%s" % self.sse_ty(i)])
                return flags
            return self.mask_idiom(k, i, flags)
        if m in ("andps", "andpd") and ops[1].type == X.X86_OP_MEM:
            return self.mask_idiom(k, i, flags)
        if m in ("cvtsi2ss", "cvtsi2sd"):
            ty = "e004" if m == "cvtsi2ss" else "e008"
            s = self.opnd(k, ops[1])
            s = s.rsplit(":", 1)[0] + ":" + ("1008" if ops[1].size == 8 else "1004")
            self.emit(k, "OPASSIGN", [self.dst(k, ops[0], ty)], [s])
            return flags
        if m in ("cvttss2si", "cvttsd2si"):
            sty = "e004" if m == "cvttss2si" else "e008"
            d = self.dst(k, ops[0])
            d = d.rsplit(":", 1)[0] + (":1008" if ops[0].size == 8 else ":1004")
            self.emit(k, "OPASSIGN", [d], [self.opnd(k, ops[1], sty)])
            return flags
        if m in ("sqrtss", "maxss", "minss"):       # scalar: through the _ss intrinsics (or sqrtf, /fp:fast)
            a = self.opnd(k, ops[1], "e004") if m == "sqrtss" else self.opnd(k, ops[0], "e004")
            d = self.dst(k, ops[0], "e004")
            if m == "sqrtss" and self.opts.get("sqrt") == "f":
                self.intrin(k, "sqrtf", d, [(a, "")])
                self.math_used = True
                return flags
            t1 = self.vtemp(VT["f"])
            self.intrin(k, "_mm_set_ss", t1, [(a, "")])
            if m == "sqrtss":
                t3 = self.vtemp(VT["f"])
                self.intrin(k, "_mm_sqrt_ss", t3, [(t1, "")])
            else:
                t2 = self.vtemp(VT["f"])
                if ops[1].type == X.X86_OP_MEM:
                    self.intrin(k, "_mm_load_ss", t2, [(self.opnd(k, ops[1], "e004"), "f")])
                else:
                    self.intrin(k, "_mm_set_ss", t2, [(self.opnd(k, ops[1], "e004"), "")])
                t3 = self.vtemp(VT["f"])
                self.intrin(k, "_mm_%s_ss" % m[:3], t3, [(t1, ""), (t2, "")])
            self.intrin(k, "_mm_cvtss_f32", d, [(t3, "")])
            return flags
        if m == "movd" and all(op.type == X.X86_OP_REG for op in ops):     # a float's bits
            if self.md.reg_name(ops[0].reg).startswith("xmm"):
                src = self.opnd(k, ops[1]).rsplit(":", 1)[0] + ":1004"
                t1, t2 = self.vtemp(VT["i"]), self.vtemp(VT["f"])
                self.intrin(k, "_mm_cvtsi32_si128", t1, [(src, "")])
                self.intrin(k, "_mm_castsi128_ps", t2, [(t1, "")])
                self.intrin(k, "_mm_cvtss_f32", self.dst(k, ops[0], "e004"), [(t2, "")])
            else:
                f = self.opnd(k, ops[1], "e004")
                t1, t2 = self.vtemp(VT["f"]), self.vtemp(VT["i"])
                self.intrin(k, "_mm_set_ss", t1, [(f, "")])
                self.intrin(k, "_mm_castps_si128", t2, [(t1, "")])
                self.intrin(k, "_mm_cvtsi128_si32", self.dst(k, ops[0]).rsplit(":", 1)[0] + ":1004", [(t2, "")])
            return flags
        if m in ("cvtss2sd", "cvtsd2ss", "cvtps2pd", "cvtpd2ps"):     # the packed forms on a scalar: cl's
            dty, sty = ("e008", "e004") if m in ("cvtss2sd", "cvtps2pd") else ("e004", "e008")   # conversions
            self.emit(k, "OPASSIGN", [self.dst(k, ops[0], dty)], [self.opnd(k, ops[1], sty)])
            return flags
        raise Unsupported("sse %s" % m)


# ----------------------------------------------------------------------------------------------------- C++ side

def emitter_mod():
    os.environ.pop("C2_BUILD", None)          # il2c's parse_line: lift2il's dialect, not traced x64 IL
    import il2c
    return il2c


def make_emitter(il, params, ret):
    il2c = emitter_mod()
    il2c.CTYPE.setdefault("a016", "__m128")
    il2c.CTYPE.setdefault("b016", "__m128i")
    il2c.CTYPE.setdefault("c016", "__m128d")
    CTYPE, strip_reg = il2c.CTYPE, il2c.strip_reg

    class E(il2c.Emitter):
        def __init__(self):
            super().__init__(il, params, ret, False)
            self.gl = set()
            self.calls = {}                   # va -> {"sigs": set of param tuples, "ret": type or None}
            self.argty = {}
            self.intrin = False
            self.tls = set()
            for l in il:
                if l.startswith("F frame "):
                    n = int(l.split()[2])
                    self.decl["lframe"] = "struct { __int64 q[%d]; }" % ((n + 7) // 8)
            # one declaration per variable, of its widest type: a narrower write converts (a 32-bit write
            # zero-extends), never `*(T*)&v`, which would take the variable's address and give it a stack home
            rank = ["2001", "1001", "2002", "1002", "1004", "2004", "e004", "e008", "1008", "a016", "b016", "c016"]
            wide = {}
            for l in il:
                for n, t in re.findall(r"(?<![\w?@$])(t\d+|_l\d+v?|_lh\d+(?:_\d+)?):([0-9a-f]{4})\b", l):
                    if t in rank and (n not in wide or rank.index(t) > rank.index(wide[n])):
                        wide[n] = t
            for n, t in wide.items():
                if re.match(r"^t\d+$", n) and int(n[1:]) < 1000:
                    continue                  # OPARG / compare temporaries: not variables
                self.decl[self.name_of(n)] = CTYPE[t if t != "2004" else "2004"]

        def gva(self, n):
            m = re.match(r"^\?g_([0-9a-f]+)@@3HA$", n)
            return int(m.group(1), 16) if m else None

        def var(self, n, ty):
            if n == "_frame":
                return "(%s)&lframe" % CTYPE.get(ty, "__int64")
            mta = re.match(r"^\?gta_([0-9a-f]+)@@3HA$", n)
            if mta:
                self.tls.add(int(mta.group(1), 16))
                return "(%s)g_%s" % (CTYPE.get(ty, "__int64"), mta.group(1))
            mt = re.match(r"^\?gt_([0-9a-f]+)@@3HA$", n)
            if mt:
                va = int(mt.group(1), 16)
                self.tls.add(va)
                return "(%s)(*(%s*)g_%x)" % (CTYPE.get(ty, "int"), CTYPE.get(ty, "int"), va)
            m = re.match(r"^\?ga_([0-9a-f]+)@@3HA$", n)
            if m:
                va = int(m.group(1), 16)
                self.gl.add(va)
                return "(%s)g_%x" % (CTYPE.get(ty, "__int64"), va)
            va = self.gva(n)
            if va is not None:
                self.gl.add(va)
                return "(%s)(*(%s*)g_%x)" % (CTYPE.get(ty, "int"), CTYPE.get(ty, "int"), va)
            return super().var(n, ty)

        def lvalue(self, tok):
            t = strip_reg(tok)
            m = re.match(r"^([^:\[]+):([0-9a-f]+)$", t)
            if m:
                va = self.gva(m.group(1))
                if va is not None:
                    self.gl.add(va)
                    return "*(%s*)g_%x" % (CTYPE.get(m.group(2), "int"), va)
                mt = re.match(r"^\?gt_([0-9a-f]+)@@3HA$", m.group(1))
                if mt:
                    self.tls.add(int(mt.group(1), 16))
                    return "*(%s*)g_%s" % (CTYPE.get(m.group(2), "int"), mt.group(1))
                n = m.group(1)
                nm = self.name_of(n)
                if (n.startswith("_a") and n[1:] in self.params) or nm in self.decl:
                    return nm                 # the assignment converts
            return super().lvalue(tok)

        def mem(self, tok, vol=False):
            m = re.match(r"^\[(.*)\]:([0-9a-f]+)$", tok)
            if not m:
                raise il2c.Unsupported("memory %s" % tok)
            inner, ty = m.groups()
            parts = []
            for term in re.split(r"\+(?=[^+]*?(?:[\w?$@]|-))", inner):
                term = term.strip()
                mm = re.match(r"^([^:*]+):([0-9a-f]+)(?:\*(\d+))?$", term)
                if mm:
                    e = self.var(mm.group(1), "1008")
                    parts.append(e if not mm.group(3) else "(%s)*%s" % (e, mm.group(3)))
                elif re.match(r"^-?\d+$", term):
                    if term not in ("0", "-0"):
                        parts.append(term)
                else:
                    raise il2c.Unsupported("address term %s" % term)
            base = parts[0] if parts else "0"
            rest = "".join(" + " + p if not p.startswith("-") else " - " + p[1:] for p in parts[1:])
            return "*(%s%s*)((char*)(%s)%s)" % ("volatile " if vol else "", CTYPE[ty], base, rest)

        def rvalue(self, tok, line, want=None):
            t = strip_reg(tok)
            if line["notes"].get("s0") == "&" and t.startswith("["):
                return "(%s)&%s" % (CTYPE.get(want, "__int64"), self.mem(t))
            if line["notes"].get("s0") == "&" and t.startswith("?g_"):
                va = self.gva(t.rsplit(":", 1)[0])
                self.gl.add(va)
                return "(%s)g_%x" % (CTYPE.get(want, "__int64"), va)
            if line["notes"].get("s0") == "&" and re.match(r"^_(?:a|l|lh)\d+(?:_\d+|v)?:", t):
                n = t.rsplit(":", 1)[0]
                nm = self.name_of(n)
                if n.startswith("_l"):
                    self.decl.setdefault(nm, "__int64")
                return "(%s)&%s" % (CTYPE.get(want, "__int64"), nm)
            return super().rvalue(tok, line, want)

        def stmt(self, L):
            if L["op"] == "OPARG" and L["dsts"] and L["srcs"]:
                self.argty[strip_reg(L["dsts"][0]).rsplit(":", 1)[0]] = self.ty(strip_reg(L["srcs"][0]))
            if L["op"] == "OPBRANCH":
                # 2004 operands of a signed condition: compare as int (a 32-bit register is typed unsigned)
                f = next((t for t in L["srcs"] if t.endswith(":9000")), None)
                cc = L["notes"].get("cc")
                if f and cc in ("l", "le", "g", "ge"):
                    key = strip_reg(f).rsplit(":", 1)[0]
                    if key in self.flags:
                        a, b, ty, LC = self.flags[key]
                        if ty[0] == "2":
                            self.flags[key] = (a, b, "1" + ty[1:], LC)
            if L["op"] in ("OPSETCC", "OPCMOV"):
                f = strip_reg(L["srcs"][0]).rsplit(":", 1)[0]
                a, b, ty, LC = self.flags.pop(f)
                cc = L["notes"]["cc"]
                want = ty
                if ty[0] == "1" and cc in il2c.UNSIGNED_CC:
                    want = "2" + ty[1:]
                if ty[0] == "2" and cc in ("l", "le", "g", "ge"):
                    want = "1" + ty[1:]
                cond = "%s %s %s" % (self.rvalue(a, LC, want), il2c.CC[cc], self.rvalue(b, LC, want))
                dt = self.ty(L["dsts"][0])
                if L["op"] == "OPSETCC":
                    return "%s = (%s)(%s);" % (self.lvalue(L["dsts"][0]), CTYPE.get(dt, "int"), cond)
                return "%s = (%s)((%s) ? %s : %s);" % (self.lvalue(L["dsts"][0]), CTYPE.get(dt, "int"), cond,
                                                       self.rvalue(L["srcs"][1], L, dt),
                                                       self.rvalue(L["srcs"][2], L, dt))
            if L["op"] == "OPSWITCH":
                idx = self.rvalue(L["srcs"][0], L, "2004")
                cases = [c.split(":") for c in L["notes"]["sw"].split(",")]
                byt = collections.OrderedDict()
                for v, lab in cases:
                    byt.setdefault(lab, []).append(int(v))
                lines = ["switch (%s) {" % idx]
                for lab, vs in byt.items():
                    lines.append("".join("case %d: " % v for v in sorted(vs)) + "goto %s;" % lab)
                lines.append("default: goto %s;" % L["notes"]["def"])
                lines.append("}")
                return ("switch", " ".join(lines), sorted(set(byt) | {L["notes"]["def"]}))
            return super().stmt(L)

        def body(self):
            orig = self.stmt
            pending = []

            def stmt2(L):
                r = orig(L)
                if MARKS and isinstance(r, str) and r and "\n" not in r:
                    r = r + "  //@" + L["text"].split()[1]       # the instruction it came from (lsearch64)
                if isinstance(r, tuple) and r[0] == "switch":
                    pending.append(r)
                    return ("stmt", "__SWITCH%d__" % (len(pending) - 1))
                return r
            self.stmt = stmt2
            saved = il2c.structure

            def structure2(items):
                out = []
                for it in items:
                    if it[0] == "stmt" and isinstance(it[1], str) and it[1].startswith("__SWITCH"):
                        n = int(it[1][8:-2])
                        out.append(("stmt", pending[n][1]))
                        out += [("goto", lab) for lab in pending[n][2]]
                    else:
                        out.append(it)
                return saved(out)
            il2c.structure = structure2
            try:
                return super().body()
            finally:
                il2c.structure = saved
                self.stmt = orig

        def arg_ctype(self, tok):
            base = strip_reg(tok).rsplit(":", 1)[0]
            t = self.argty.get(base) or self.ty(strip_reg(tok)) or "1008"
            return {"e004": "float", "e008": "double", "1008": "__int64", "1004": "int", "2004": "unsigned int",
                    "2001": "unsigned char", "1001": "signed char", "2002": "unsigned short",
                    "1002": "short"}.get(t, "__int64")

        def ret_ctype(self, d):
            return CTYPE.get(self.ty(d[0]), "__int64") if d else "void"

        def call(self, L):
            d = [t for t in L["dsts"] if not re.match(r"^pas\d+", t)]
            srcs = [t for t in L["srcs"] if not re.match(r"^pas\d+", t)]
            tgt = strip_reg(srcs[0]).rsplit(":", 1)[0]
            rty = self.ret_ctype(d)
            argv = [self.rvalue(a, L) for a in srcs[1:]]
            tys = [self.arg_ctype(a) for a in srcs[1:]]
            mfn = re.match(r"^\?__lift_fn_(\w+)@@YAXXZ$", tgt)
            if mfn:
                self.intrin = True
                nm = mfn.group(1)
                if nm in ("fabs", "fabsf", "sqrtf", "sqrt"):
                    self.mathh = True
                pc = dict((x[:-1], x[-1]) for x in L["notes"].get("pc", "").split(",") if x)
                av = []
                for n, a in enumerate(argv):
                    c = {"f": "float*", "i": "__m128i*", "m": "__m128*", "d": "double*"}.get(pc.get(str(n)))
                    av.append("(%s)(%s)" % (c, a) if c else a)
                mnc = re.match(r"^nullcast_(down|up)(\d+)$", nm)
                if mnc:
                    K = int(mnc.group(2))
                    if not any("BaseAt%d " % K in p for p in self.prelude):
                        self.prelude.append(
                            "struct PadTo%d { char pad[%d]; };\nstruct BaseAt%d { char b; };\n"
                            "struct DerivedWithBaseAt%d : PadTo%d, BaseAt%d { };\n"
                            "inline __int64 nullcast_down%d(__int64 p) { return (__int64)static_cast<DerivedWithBaseAt%d*>((BaseAt%d*)p); }\n"
                            "inline __int64 nullcast_up%d(__int64 p) { return (__int64)static_cast<BaseAt%d*>((DerivedWithBaseAt%d*)p); }\n"
                            % ((K,) * 12))
                e = "%s(%s)" % (nm, ", ".join(av))
                if d:
                    return "%s = %s;" % (self.lvalue(d[0]), e)
                return e + ";"
            mf = re.match(r"^\?__lift_mm_(\w+)@@YAXXZ$", tgt)
            if mf:
                self.intrin = True
                return "_mm_%s();" % mf.group(1)
            mi = re.match(r"^\?__lift_(Interlocked\w+)@@YAXXZ$", tgt)
            if mi:
                self.intrin = True
                nm = mi.group(1)
                w = re.search(r"(64|16|8)$", nm)
                pt = {"64": "__int64", "16": "short", "8": "char"}.get(w.group(1) if w else "", "long")
                # (T*) not (T volatile*): the same code, and a volatile in a function body is refused at submit (Q1)
                e = "_%s((%s*)(%s)%s)" % (nm, pt, argv[0], "".join(", (%s)(%s)" % (pt, x) for x in argv[1:]))
                if d:
                    return "%s = (%s)%s;" % (self.lvalue(d[0]), rty, e)
                return e + ";"
            m = re.match(r"^\?FUN_([0-9a-f]+)@@YAXXZ$", tgt)
            if m:
                va = int(m.group(1), 16)
                c = self.calls.setdefault(va, {"sigs": set(), "ret": None})
                if "va" in L["notes"]:
                    c["sigs"].add(tuple(tys[:int(L["notes"]["va"])]) + ("...",))
                else:
                    c["sigs"].add(tuple(tys))
                if d:
                    c["ret"] = rty if c["ret"] in (None, rty) else "__int64"
                e = "FUN_%x(%s)" % (va, ", ".join("(%s)(%s)" % (t, a) for t, a in zip(tys, argv)))
            else:
                tk = strip_reg(srcs[0])
                mv = re.match(r"^\[(t\d+):1008\+?(\d*)\]:1008$", tk)
                fp = None
                if mv and argv and mv.group(1) in self.loads:
                    vp_src = self.loads[mv.group(1)]
                    mo = re.match(r"^\[([^+\]]+):1008\]:1008$", vp_src)
                    a0 = strip_reg(srcs[1]).rsplit(":", 1)[0]
                    a0 = self.argsrc.get(a0, a0)
                    disp = int(mv.group(2) or 0)
                    if mo and mo.group(1) == a0 and disp % 8 == 0:
                        slot = disp // 8
                        cls = "vc%d" % len(self.prelude)
                        self.prelude.append("struct %s {\n%s    virtual %s s%d(%s);\n};\n" % (
                            cls, "".join("    virtual void s%d();\n" % q for q in range(slot)), rty, slot,
                            ", ".join(tys[1:])))
                        e = "((%s*)(%s))->s%d(%s)" % (cls, argv[0], slot, ", ".join(
                            "(%s)(%s)" % (t, a) for t, a in zip(tys[1:], argv[1:])))
                        return ("%s = (%s)%s;" % (self.lvalue(d[0]), rty, e)) if d else e + ";"
                if tk.startswith("["):
                    fp = self.mem(tk)
                else:
                    fp = self.rvalue(srcs[0], L, "1008")
                e = "((%s (*)(%s))(%s))(%s)" % (rty, ", ".join(tys) or "void", fp, ", ".join(
                    "(%s)(%s)" % (t, a) for t, a in zip(tys, argv)))
            if d:
                return "%s = (%s)%s;" % (self.lvalue(d[0]), rty, e)
            return e + ";"

    return E()


CT = {"e004": "float", "e008": "double", "1008": "__int64", "1004": "int", "2004": "unsigned int",
      "2001": "unsigned char", "1001": "signed char", "2002": "unsigned short", "1002": "short"}


RV_SIZES = {"__int64": 8, "unsigned __int64": 8, "int": 4, "unsigned int": 4}


def _split_args(s):
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


PCOPY = False            # param_copies by default (opts pcopy overrides; a variant, see variants: on 300 functions of a
                         # retail x64 game shape better 14 / worse 16, so not the default)
OUTSIZE = False          # outparam_sizes by default (opts outsize overrides)


def param_copies(text, va):
    """A parameter copied into a variable at entry (`v1009 = (__int64)((__int64)(a0));`, cl's move to a callee-saved
    register) and the copy never set again: the parameter itself. The source passes `this` / a parameter to calls
    directly; with the copy the parameter's home is free and a local takes it (cl 17.00's packer: a parameter used
    only as call arguments keeps its home, docs/c2/slots.md 6.3)."""
    head, sep, body = text.partition("\n{\n")
    if not sep or not re.search(r"^\S[^\n]* FUN_%x\(" % va, head, re.M):
        return text
    for _ in range(8):
        hit = None
        for m in re.finditer(r"^\s*(v\d+) = \(__int64\)\(\(__int64\)\((a\d+)\)\);$", body, re.M):
            v, a = m.group(1), m.group(2)
            defs = re.findall(r"^\s*%s = " % v, body, re.M)
            if len(defs) == 1 and not re.search(r"^\s*%s = " % a, body, re.M) and \
                    not re.search(r"&%s\b" % a, body) and not re.search(r"&%s\b" % v, body):
                hit = (m, v, a)
                break
        if not hit:
            break
        m, v, a = hit
        body = body[:m.start()] + body[m.end() + 1:]
        body = re.sub(r"^    [\w ]+ %s;\n" % v, "", body, flags=re.M)
        body = re.sub(r"\b%s\b" % v, a, body)
    return head + sep + body


def store_width(img, va, reg, depth=0):
    """Bytes of the first store the function at va makes through its pointer argument `reg` (following copies of it
    into other registers), or None."""
    pd = None
    try:
        import ehinfo64
        pd = ehinfo64.pdata(img)
    except Exception:
        pass
    end = pd.get(va, (va + 0x200,))[0] if pd else va + 0x200
    alias = {reg}
    for i in img.insns(va, max(16, min(end - va, 0x400))):
        if i.mnemonic in ("ret", "jmp") and depth == 0 and i.operands and i.operands[0].type == X.X86_OP_IMM and \
                i.mnemonic == "jmp" and i is not None:
            break
        ops = i.operands
        if i.mnemonic == "mov" and len(ops) == 2 and ops[0].type == X.X86_OP_MEM and ops[0].mem.base and \
                not ops[0].mem.index and ops[0].mem.disp == 0 and canon(i.reg_name(ops[0].mem.base)) in alias:
            return ops[0].size
        if i.mnemonic == "mov" and len(ops) == 2 and ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_REG:
            d, s_ = canon(i.reg_name(ops[0].reg)), canon(i.reg_name(ops[1].reg))
            if s_ in alias and ops[0].size == 8:
                alias.add(d)
            elif d in alias:
                alias.discard(d)
        elif ops and ops[0].type == X.X86_OP_REG and canon(i.reg_name(ops[0].reg)) in alias and \
                i.mnemonic not in ("cmp", "test", "push"):
            alias.discard(canon(i.reg_name(ops[0].reg)))
        if i.mnemonic == "call":
            alias &= {"rbx", "rbp", "rsi", "rdi", "r12", "r13", "r14", "r15"}
        if not alias or i.mnemonic in ("ret", "repz ret"):
            break
    return None


def outparam_sizes(text, img):
    """A local whose address is only passed to calls (never read or written here): typed by the width of the
    callee's first store through it (`int` for a 4-byte store). cl 17.00's packer visits frame locals by size first,
    so a 4-byte out parameter goes before an 8-byte local with more uses (docs/c2/slots.md 6.3)."""
    import frameinfo64
    lines = text.split("\n")
    decl = {}
    for n, ln in enumerate(lines):
        m = re.match(r"^    (__int64) (v_\w+);$", ln)
        if m:
            decl[m.group(2)] = n
    want = {}
    for name, dn in decl.items():
        uses = [x for k, x in enumerate(lines) if k != dn and re.search(r"\b%s\b" % name, x)]
        if not uses or not all(re.match(r"^\s*(v\d+) = \(__int64\)\(\(__int64\)&%s\);$" % name, x) for x in uses):
            continue
        widths = set()
        for x in uses:
            tmp = re.match(r"^\s*(v\d+) = ", x).group(1)
            for c in lines:
                m = re.match(r"^\s*(?:v\d+ = \([\w ]+\))?FUN_([0-9a-f]+)\((.*)\);$", c)
                if not m:
                    continue
                args = _split_args(m.group(2))
                for p, a in enumerate(args[:4]):
                    if re.sub(r"[()]|__int64", "", a) == tmp:
                        callee = int(m.group(1), 16)
                        if ("rcx", "rdx", "r8", "r9")[p] in frameinfo64.returns_arg(img, callee):
                            widths.add(None)
                        else:
                            widths.add(store_width(img, callee, ("rcx", "rdx", "r8", "r9")[p]))
        if len(widths) == 1 and list(widths)[0] in (1, 2, 4):
            want[dn] = {1: "unsigned char", 2: "unsigned short", 4: "int"}[list(widths)[0]]
    for dn, ty in want.items():
        lines[dn] = re.sub(r"^    __int64 ", "    %s " % ty, lines[dn])
    return "\n".join(lines)


ZSTORE = re.compile(r"^(\s*)\*\(__int64\*\)\(\(char\*\)(.+?)( \+ (\d+))?\) = \(__int64\)\(\(__int64\)0\);$")


def drop_unused_decls(text, va):
    """Locals declared and never referenced (cl /W3 warns C4101, refused at submit as Q11)."""
    m = re.search(r"^\S[^\n]* FUN_%x\([^\n]*\)\n\{\n" % va, text, re.M)
    if not m:
        return text
    body = text[m.end():]
    out = []
    for ln in body.split("\n"):
        d = re.match(r"^    [\w ]+ (\w+);$", ln)
        if d and len(re.findall(r"\b%s\b" % re.escape(d.group(1)), body)) == 1:
            continue
        out.append(ln)
    return text[:m.end()] + "\n".join(out)


def zero_pairs(text):
    """A literal 8-byte zero store (the original's `mov qword ptr [m], 0`) as two 4-byte zero stores. cl 17.00 keeps
    a known-zero register for zero stores (`mov [m], rbp`) and writes `mov qword ptr [m], 0` only when it combines two
    adjacent int stores of 0 reached by two zero defs (docs/c2/immcand.md 7)."""
    out = []
    for ln in text.split("\n"):
        m = ZSTORE.match(ln)
        if not m:
            out.append(ln)
            continue
        ind, base, off = m.group(1), m.group(2), int(m.group(4) or 0)
        for o in (off, off + 4):
            out.append("%s*(int*)((char*)%s%s) = (int)0;" % (ind, base, " + %d" % o if o else ""))
    return "\n".join(out)


def retslots(text, img, va):
    """A call that fills a local through the callee's hidden return pointer (the callee returns that same pointer:
    frameinfo64.returns_arg) as a class returned by value, initialised in place:
        v1063 = (__int64)&v_lh3;  v1012 = FUN_X(this, v1063);  ... v_lh3 ...
    ->  RetVal8 v_lh3 = ((RvObj_FUN_X*)(this))->call();  v1012 = (__int64)&v_lh3;  ... v_lh3.v ...
    RetVal<N> has a declared copy constructor, so cl returns it through the hidden pointer (a POD of 1/2/4/8 bytes
    would come back in rax). Only when every later use of the local stays inside the call's block; the slot in rcx is
    a free function FUN_X_rv(...), the slot in rdx a member function of its rcx.
    Opt-in (opts retslot=1), not a default variant: on 150 open functions of a retail cl 17.00 x64 game that pass a
    return slot it gave no match, shape better 2 / worse 28 / same 113 (2026-11-27), and the frame packer still
    visits the object by its uses like the plain local (0x1406e7170's home-slot order unchanged)."""
    import frameinfo64
    lines = text.split("\n")
    decl = {}
    for n, ln in enumerate(lines):
        m = re.match(r"^    ([\w ]+) (v_\w+);$", ln)
        if m and m.group(1) in RV_SIZES:
            decl[m.group(2)] = (n, m.group(1))
    addr = {}                                      # temp -> local whose address it holds
    for n, ln in enumerate(lines):
        m = re.match(r"^\s*(v\d+) = \(__int64\)\(\(__int64\)&(v_\w+)\);$", ln)
        if m and m.group(2) in decl:
            addr[m.group(1)] = (n, m.group(2))
    types, structs, drop = {}, [], set()
    for n, ln in enumerate(lines):
        m = re.match(r"^(\s*)(?:(v\d+) = \([\w ]+\))?FUN_([0-9a-f]+)\((.*)\);$", ln)
        if not m:
            continue
        ind, dst, callee, args = m.group(1), m.group(2), int(m.group(3), 16), _split_args(m.group(4))
        slot = [(p, a) for p, a in enumerate(args[:2]) if re.sub(r"[()]|__int64", "", a) in addr]
        if len(slot) != 1:
            continue
        p, a = slot[0]
        tmp = re.sub(r"[()]|__int64", "", a)
        an, local = addr[tmp]
        if ("rcx", "rdx")[p] not in frameinfo64.returns_arg(img, callee):
            continue
        uses = [k for k, x in enumerate(lines) if k not in (decl[local][0], an) and re.search(r"\b%s\b" % local, x)]
        tdecl = [k for k, x in enumerate(lines) if re.match(r"^    [\w ]+ %s;$" % tmp, x)]
        if not uses or any(k < n for k in uses) or any(re.search(r"\b%s\b" % tmp, x) for k, x in enumerate(lines)
                                                         if k not in [an, n] + tdecl):
            continue
        depth, ok = 0, True                        # the uses stay inside the call's block
        for k in range(n + 1, max(uses) + 1):
            depth += lines[k].count("{") - lines[k].count("}")
            if depth < 0:
                ok = False
                break
        if not ok:
            continue
        ty = decl[local][1]
        sz = RV_SIZES[ty]
        rv = "RetVal%d" % sz
        types[rv] = "struct %s { %s v; %s(const %s&); };" % (rv, ty, rv, rv)
        rest = [x for k, x in enumerate(args) if k != p]
        if p == 1:
            obj = "RvObj_FUN_%x" % callee
            structs.append("struct %s { %s call(%s); };" % (obj, rv, ", ".join("__int64" for _ in rest[1:])))
            call = "((%s*)(%s))->call(%s)" % (obj, rest[0], ", ".join(rest[1:]))
        else:
            fn = "FUN_%x_rv" % callee
            structs.append("%s %s(%s);" % (rv, fn, ", ".join("__int64" for _ in rest)))
            call = "%s(%s)" % (fn, ", ".join(rest))
        new = ["%s%s %s = %s;" % (ind, rv, local, call)]
        if dst:
            new.append("%s%s = (__int64)&%s;" % (ind, dst, local))
        lines[n] = "\n".join(new)
        drop |= {an, decl[local][0]} | set(tdecl)
        for k in uses:
            lines[k] = re.sub(r"\b%s\b(?!\.)" % local, local + ".v", lines[k])
    if not types:
        return text
    lines = [x for k, x in enumerate(lines) if k not in drop]
    out = "\n".join(lines)
    for c in sorted(set(re.findall(r"^\S[^\n]* FUN_([0-9a-f]+)\([^\n]*\);$", out, re.M))):
        if not re.search(r"\bFUN_%s\(" % c, out.split("\n{", 1)[-1]):
            out = re.sub(r"^\S[^\n]* FUN_%s\([^\n]*\);\n" % c, "", out, flags=re.M)
    sig = re.search(r"^\S[^\n]* FUN_%x\(" % va, out, re.M)
    extra = "\n".join(sorted(types.values()) + sorted(set(structs))) + "\n"
    return out[:sig.start()] + extra + out[sig.start():]


def variants(img, va, size):
    """[(opts, C++ text)]: the default lifting first, then the readings the bytes leave open (whether rax / xmm0
    at a ret is a value), each text once."""
    out, seen = [], set()
    text, lf = unit(img, va, size)
    out.append(({}, text))
    seen.add(text)
    alts = []
    rets = [{"ret": "void"}] if lf.ret_reg else [{"ret": "rax"}]
    if getattr(lf, "ret_both", False):
        rets.append({"ret": "xmm0"})
    frames = [{"frame": "wide"}] if lf.frame_region else []
    alts = rets + frames + [dict(r, **f) for r in rets for f in frames]
    if any(i.mnemonic == "bt" and i.operands[1].type == X.X86_OP_REG for i in lf.ins):
        alts.append({"bt": "mask"})
    if any(i.mnemonic in ("sqrtps", "sqrtss") for i in lf.ins):
        alts.append({"sqrt": "f"})
    if any(i.mnemonic == "sbb" for i in lf.ins):
        alts.append({"sbb": "neg"})
    try:
        if lf.byte_webs():
            alts = alts + [dict(a, bytewebs=1) for a in [{}] + alts]   # byte-wide webs as unsigned char
    except Exception:
        pass
    if any(ZSTORE.match(x) for x in text.split("\n")):
        alts = alts + [dict(a, zeropair=1) for a in [{}] + alts]   # a literal qword 0 store as two int stores
    if re.search(r"^\s*v\d+ = \(__int64\)\(\(__int64\)\(a\d+\)\);$", text, re.M):
        alts.append({"pcopy": 1})                # a parameter copied at entry: passed itself (14 better / 16 worse)

    if lf.eh_help is not None:                   # the game has a C++ exception frame here: only an EH body can match
        alts = [dict(a, eh=m) for m in ("try", "swallow") for a in [{}] + alts]
        out, seen = [], set()
    for o in alts:
        try:
            t, _ = unit(img, va, size, o)
        except Exception:
            continue
        if t not in seen:
            seen.add(t)
            out.append((o, t))
    return out


_CALLEE_AR = {}
SUBREG = {"rcx": ("rcx", "ecx", "cx", "cl", "ch"), "rdx": ("rdx", "edx", "dx", "dl", "dh"),
          "r8": ("r8", "r8d", "r8w", "r8b"), "r9": ("r9", "r9d", "r9w", "r9b")}


def callee_arity(img, va, depth=0):
    """How many of rcx, rdx, r8, r9 the function at va reads before writing them (the highest such position + 1),
    following jmp thunks and up to its first call / ret / indirect jump; 0 when unknown."""
    if va in _CALLEE_AR:
        return _CALLEE_AR[va]
    _CALLEE_AR[va] = 0
    read, dead = set(), set()
    n = 0
    try:
        for i in img.md.disasm(img.read(va, 0x200), va):
            n += 1
            if n > 80:
                break
            if i.mnemonic == "jmp" and i.operands[0].type == X.X86_OP_IMM and depth < 3 and n == 1:
                _CALLEE_AR[va] = callee_arity(img, i.operands[0].imm, depth + 1)
                return _CALLEE_AR[va]
            try:
                rr, ww = i.regs_access()
            except Exception:
                break
            rn = {i.reg_name(x) for x in rr}
            wn = {i.reg_name(x) for x in ww}
            if i.mnemonic == "xor" and len(i.operands) == 2 and i.op_str.split(",")[0].strip() == \
                    i.op_str.split(",")[1].strip():
                rn = set()
            for p, r in enumerate(ARGR):
                if r in dead:
                    continue
                if rn & set(SUBREG[r]):
                    read.add(p)
                if wn & set(SUBREG[r]):
                    dead.add(r)
            if i.mnemonic in ("call", "ret", "jmp"):
                break
    except Exception:
        pass
    _CALLEE_AR[va] = max(read) + 1 if read else 0
    return _CALLEE_AR[va]


def eh_wrap(body, mode, ret):
    """The body inside try { } catch (...): rethrown ("try") or swallowed ("swallow"). cl gives the function a C++
    exception frame (the UnwindHelp store, no tail calls inside the try) and keeps the catch in a funclet."""
    lines = body.split("\n")
    assert lines[0] == "{" and lines[-1] == "}"
    k = 1
    while k < len(lines) - 1 and (re.match(r"^    [\w ]+\*? \w+(\[\d+\])?;$", lines[k]) or
                                  lines[k].startswith("    struct {")):
        k += 1
    inner = ["    " + l if l.strip() else l for l in lines[k:-1]]
    if mode == "try":
        tail = ["    } catch (...) {", "        throw;", "    }"]
    else:
        tail = ["    } catch (...) {", "    }"] + (["    return 0;"] if ret != "void" else [])
    return "\n".join(lines[:k] + ["    try {"] + inner + tail + ["}"])


def unit(img, va, size, opts=None):
    lf = Lifter(img, va, size, opts)
    il = lf.run()
    nargs = lf.own_arity
    params = ["a%d" % p for p in range(nargs)]
    if lf.float_ret:
        ret = CT[lf.float_ret]
    elif lf.value_ret:
        ret = {"2001": "unsigned char", "1004": "int", "2004": "int", "2002": "unsigned short"}.get(lf.ret_ty,
                                                                                                 "__int64")
    else:
        ret = "void"
    em = make_emitter(il, params, ret)
    body = em.body()
    conflict = {cva: c for cva, c in em.calls.items() if len(c["sigs"]) > 1}
    if conflict and not lf.opts.get("callsig"):
        # one callee, two signatures: a stale xmm / integer register read as an argument at some call site. Take
        # an integer register wherever the call sites disagree and lift again.
        fs = {}
        for cva, c in conflict.items():
            sigs = sorted(c["sigs"], key=len)
            n = max(len(x) for x in sigs)
            kinds = []
            for p in range(n):
                ts = {x[p] for x in sigs if p < len(x)}
                kinds.append("x" if ts and all(t in ("float", "double") for t in ts) else "r")
            fs[cva] = kinds
        return unit(img, va, size, dict(lf.opts, callsig=fs))
    try:
        import il2c_lift
        body = il2c_lift.drop_dead(body)
    except Exception:
        pass
    if ret != "void":
        body = re.sub(r"\breturn;", "return 0;", body)
    # the register webs (t1001..) are variables, not compiler temporaries: named v1001.. so that tools keying on
    # `t\d+` for cl's own temporaries (ncolsim.named, realize --from-ncol) keep them; after drop_dead, which
    # works on the t-names
    body = re.sub(r"\bt(1\d{3,})\b", r"v\1", body)
    if lf.opts.get("eh"):
        body = eh_wrap(body, lf.opts["eh"], ret)
    typed = ["%s a%d" % (CT.get(lf.args_read.get(p, "1008"), "__int64"), p) for p in range(nargs)]
    head = ["#include <intrin.h>"] if em.intrin or "__m128" in body else []
    if getattr(em, "mathh", False):
        head.insert(0, "#include <math.h>")
    if em.prelude:
        head.append("".join(em.prelude))
    head += ["extern unsigned char g_%x[];" % g for g in sorted(em.gl)]
    head += ["extern __declspec(thread) unsigned char g_%x[];" % g for g in sorted(em.tls)]
    for cva, c in sorted(em.calls.items()):
        r = c["ret"] or "void"
        for sig in sorted(c["sigs"]):
            head.append("%s FUN_%x(%s);" % (r, cva, ", ".join(sig)))
    sig = "%s FUN_%x(%s)\n" % (ret, va, ", ".join(typed) or "void")
    text = "\n".join(head) + ("\n\n" if head else "") + sig + body + "\n"
    if lf.opts.get("pcopy", PCOPY):
        text = param_copies(text, va)
    if lf.opts.get("outsize", OUTSIZE):
        text = outparam_sizes(text, img)
    if lf.opts.get("retslot"):
        text = retslots(text, img, va)
    if lf.opts.get("zeropair"):
        text = zero_pairs(text)
    text = drop_unused_decls(text, va)
    if lf.opts.get("bytewebs"):
        for n in lf.byte_webs():
            text = re.sub(r"^    (?:unsigned int|int|unsigned short) v%s;$" % n[1:], "    unsigned char v%s;" % n[1:],
                          text, flags=re.M)
    return text, lf


# ------------------------------------------------------------------------------------------------------ checking

_pe = {}


def index(img=None):
    """va -> {"size": N, "flags": [...]} from INDEX, else from the image's .pdata (unchained entries)."""
    if INDEX:
        d = json.load(open(INDEX))
        return {int(f["address"], 16): f for f in d["functions"]}
    import struct
    img = img or Image()
    dd = img.pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
    raw = img.pe.get_data(dd.VirtualAddress, dd.Size) if dd.Size else b""
    out = {}
    for k in range(0, len(raw) - 11, 12):
        b, e, u = struct.unpack_from("<III", raw, k)
        try:
            flags = img.pe.get_data(u, 1)[0] >> 3
        except Exception:
            continue
        if b and not flags & 4:                    # UNW_FLAG_CHAININFO: a later part of a split function
            out[img.base + b] = {"size": e - b}
    return out


EXTRA_SETS = []      # --extra-set: whole flag sets tried after the usual ones
ONLY_EXTRA = [False]  # --only-extra: try those alone
# also try /fp:fast where the bytes do not say which float model (a site that accepts /fp:fast for every function)
FP_FALLBACK = os.environ.get("LIFT64_FPFAST") == "1"


def flag_sets(va, entry=None, flags=None):
    """The flag sets to try: --flags, else the index entry's, else /O2 /GS-; each with /fp:fast first when the
    function's bytes say /fp:fast (fp_mode), never when they say /fp:precise; then --extra-set's."""
    if flags:
        return flags
    extra = [list(x) for x in EXTRA_SETS]
    if ONLY_EXTRA[0]:
        return extra
    sets = [list(f) for f in (entry or {}).get("flags") or []] or [list(f) for f in DEFAULT_FLAGS]
    if any(f.startswith("/fp:") for s in sets for f in s):
        return sets + extra
    fast = [s + ["/fp:fast"] for s in sets]
    fp = fp_mode(va, (entry or {}).get("size"))
    if fp == "fast":                             # the bytes say /fp:fast: try it first
        return fast + sets + extra
    if fp == "precise" or not FP_FALLBACK:
        return sets + extra
    return sets + fast + extra


_FP = {}


def fp_mode(va, size=None):
    """"fast" when a float compare's equality branch has no parity check (je/jne right after ucomiss/comiss),
    "precise" when it has one (jp/jnp), else None. Reads `size` bytes (default 0x400)."""
    if va in _FP:
        return _FP[va]
    try:
        ins = Image().insns(va, size or 0x400)
    except Exception:
        ins = []
    mode = None
    for k, i in enumerate(ins):
        if i.mnemonic not in ("ucomiss", "comiss", "ucomisd", "comisd"):
            continue
        nxt = [x.mnemonic for x in ins[k + 1:k + 3]]
        if nxt and nxt[0] in ("jp", "jnp"):
            mode = "precise"
            break
        if nxt and nxt[0] in ("je", "jne", "sete", "setne"):
            mode = "fast"
    _FP[va] = mode
    return mode


def check(va, size, text, workdir, flags=None, entry=None):
    import x64check
    path = BINARY
    if path not in _pe:
        _pe[path] = x64check.Pe(path)
    d = os.path.join(workdir, "%x" % va)
    os.makedirs(d, exist_ok=True)
    sets = flag_sets(va, entry, flags)
    if not flags and not re.search(r"\b(float|double|__m128\w*)\b", text):   # /fp:fast changes only float code
        sets = [f for f in sets if "/fp:fast" not in f or f in EXTRA_SETS] or sets
    json.dump(dict(address="0x%x" % va, size=size, name="FUN_%x" % va, flags=sets[0], accept_also=sets[1:]),
              open(os.path.join(d, "pack.json"), "w"))
    src = os.path.join(d, "lifted.cpp")
    open(src, "w").write(text)
    best = None
    for fl in sets:
        res = x64check.check(_pe[path], va, size, src, fl, full_diff=os.path.join(d, "diff.txt"))
        if res["status"] == "match":
            return res
        if res.get("rows"):
            import x64metrics
            res["metrics"] = x64metrics.metrics_of(res)       # edits, shape, mapcost, regpairs (x64metrics.py)
            del res["rows"]
        if best is None or res.get("differing_instructions", 1 << 30) < best.get("differing_instructions", 1 << 30):
            best = res
    return best


def _one(args):
    global BINARY
    va, entry, workdir, BINARY, flags, EXTRA_SETS[:], ONLY_EXTRA[0] = args
    size = entry["size"]
    t0 = time.time()
    out = dict(va="%x" % va, size=size)
    try:
        img = Image()
        vs = variants(img, va, size)
    except Unsupported as e:
        out.update(status="UNSUPPORTED", why=str(e)[:100])
        return out
    except Exception as e:
        if type(e).__name__ == "Unsupported":
            out.update(status="UNSUPPORTED", why="emit: " + str(e)[:100])
        else:
            out.update(status="ERROR", why=("%s: %s" % (type(e).__name__, e))[:140])
        return out
    res = None
    try:
        for n, (o, text) in enumerate(vs):
            r = check(va, size, text, workdir, flags, entry)
            r["opts"] = o
            if res is None or r["status"] == "match" or (res["status"] != "match" and r.get(
                    "differing_instructions", 1 << 30) < res.get("differing_instructions", 1 << 30)):
                res = r
                if n:
                    open(os.path.join(workdir, "%x" % va, "lifted.cpp"), "w").write(text)
            if r["status"] == "match":
                break
        if res["opts"] != vs[-1][0] and res["status"] != "match":
            open(os.path.join(workdir, "%x" % va, "lifted.cpp"), "w").write(dict((repr(o), t) for o, t in vs)[
                repr(res["opts"])])
    except Exception as e:
        out.update(status="CHECK-ERROR", why=("%s: %s" % (type(e).__name__, e))[:140])
        return out
    out["opts"] = res.get("opts")
    st = res["status"]
    ninst = len(Image().insns(va, size))
    out.update(status={"match": "MATCH", "no match": "DIFF", "does not build": "CL-ERROR"}.get(st, st.upper()),
               ninst=ninst, diff=res.get("differing_instructions"), flags=" ".join(res.get("flags") or []),
               secs=round(time.time() - t0, 1))
    if res.get("metrics"):
        m = res["metrics"]
        out.update(edits=m["edits"], shape=m["edits_noreg"], mapcost=m["mapcost"], n_orig=m["n_orig"])
    if st == "does not build":
        out["why"] = "; ".join(res.get("errors") or [])[:160]
    if st == "no match" and res.get("differing_instructions") is None:
        out["why"] = "; ".join(str(e) for e in res.get("errors") or [])[:160]
    return out


def laptop_backend():
    """tools/laptop64.py, the optional Windows compile machine (not part of every checkout): its module, or a clear
    exit when it is absent."""
    try:
        import laptop64
    except ImportError:
        raise SystemExit("--laptop needs tools/laptop64.py (the Windows compile-machine client), which this checkout "
                         "does not have; run without --laptop to compile here")
    return laptop64


def bench_laptop(img, vas, idx, flags=None, nvar=4):
    """The bench on the Windows compile machine (tools/laptop64.py: one upload per chunk of 120 functions; lint off,
    /W0). Each function: its first `nvar` lifter variants under its flag sets, the best kept. Rows as _one's."""
    laptop64 = laptop_backend()
    jobs, rows = [], []
    for va in vas:
        size = idx[va]["size"]
        try:
            vs = variants(img, va, size)
        except Exception as e:
            st = "UNSUPPORTED" if type(e).__name__ == "Unsupported" else "ERROR"
            rows.append(dict(va="%x" % va, size=size, status=st, why=str(e)[:100]))
            continue
        fl = flags or flag_sets(va, idx[va])
        jobs.append({"key": "%x" % va, "addr": va, "size": size, "cands": [t for _, t in vs][:nvar], "flagsets": fl})
    res = laptop64.run_batch(jobs, chunk=120)
    for j in jobs:
        r = res.get(j["key"]) or {"status": "error"}
        st = {"match": "MATCH", "no match": "DIFF", "does not build": "CL-ERROR"}.get(r.get("status"),
                                                                                   str(r.get("status")).upper())
        rows.append(dict(va=j["key"], size=j["size"], status=st, flags=" ".join(r.get("flags") or []),
                         edits=r.get("edits"), shape=r.get("edits_noreg"), mapcost=r.get("mapcost"),
                         why="" if st == "MATCH" else "; ".join(map(str, r.get("errors") or []))[:160]))
    for r in rows:
        print(json.dumps(r), flush=True)
    return rows


def main(argv):
    global BINARY, INDEX
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("il", "unit", "check", "bench"))
    ap.add_argument("va", nargs="?")
    ap.add_argument("--binary", help="the x64 executable (or LIFT64_BINARY)")
    ap.add_argument("--index", help="function index JSON (or LIFT64_INDEX); default: the executable's .pdata")
    ap.add_argument("--size", type=int, help="the function's size in bytes (il / unit / check)")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--max-size", type=int, default=256)
    ap.add_argument("--min-size", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--flags", help="one flag set, comma-separated; several separated by ';'")
    ap.add_argument("--json")
    ap.add_argument("--work", default=os.path.join(os.path.dirname(HERE), "work", "lift64"))
    ap.add_argument("--vas", help="file of addresses (one per line) instead of a random sample")
    ap.add_argument("--extra-set", action="append", default=[],
                    help="one more whole flag set, comma-separated (repeatable), e.g. a proven PDB unit set")
    ap.add_argument("--only-extra", action="store_true", help="try only the --extra-set flag sets")
    ap.add_argument("--laptop", action="store_true",
                    help="bench: compile and compare on the Windows compile machine (tools/laptop64.py) instead of here")
    a = ap.parse_args(argv)
    BINARY, INDEX = a.binary or BINARY, a.index or INDEX
    EXTRA_SETS[:] = [x.split(",") for x in a.extra_set]
    ONLY_EXTRA[0] = a.only_extra
    flags = [f.split(",") for f in a.flags.split(";")] if a.flags else None
    img = Image()
    idx = {} if a.size and a.cmd != "bench" else index(img)
    if a.cmd in ("il", "unit", "check"):
        va = int(a.va, 16)
        if a.size:
            idx[va] = dict(idx.get(va) or {}, size=a.size)
        if va not in idx:
            sys.exit("lift2il64: no size for 0x%x (not in the index or .pdata): give --size" % va)
        size = idx[va]["size"]
        if a.cmd == "il":
            print("\n".join(Lifter(img, va, size).run()))
            return 0
        text, lf = unit(img, va, size)
        if a.cmd == "unit":
            print(text)
            return 0
        res = check(va, size, text, a.work, flags, idx[va])
        m = res.get("metrics")
        print(res["status"].upper(), res.get("differing_instructions", ""), " ".join(res.get("flags") or []),
              "  edits %d shape %d mapcost %d" % (m["edits"], m["edits_noreg"], m["mapcost"]) if m else "")
        if res.get("diff"):
            print(res["diff"])
        if res.get("errors"):
            print("\n".join(map(str, res["errors"]))[:2000])
        return 0 if res["status"] == "match" else 1
    if a.vas:
        vas = [int(l.split()[0], 16) for l in open(a.vas) if l.strip()]
    else:
        pool = sorted(v for v, f in idx.items() if a.min_size <= f["size"] <= a.max_size)
        random.Random(a.seed).shuffle(pool)
        vas = pool[:a.sample]
    os.makedirs(a.work, exist_ok=True)
    if a.laptop:
        rows = bench_laptop(img, vas, idx, flags)
    else:
        from concurrent.futures import ProcessPoolExecutor
        rows = []
        with ProcessPoolExecutor(a.jobs) as ex:
            for r in ex.map(_one, [(v, idx[v], a.work, BINARY, flags, EXTRA_SETS, a.only_extra) for v in vas]):
                rows.append(r)
                print(json.dumps(r), flush=True)
    c = collections.Counter(r["status"] for r in rows)
    print("summary:", dict(c), "of", len(rows))
    why = collections.Counter(re.sub(r"0x[0-9a-f]+|\d+", "N", r.get("why", "")) for r in rows if r["status"] != "MATCH"
                              and r.get("why"))
    for w, n in why.most_common(25):
        print("  %4d  %s" % (n, w))
    if a.json:
        json.dump(rows, open(a.json, "w"), indent=0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
