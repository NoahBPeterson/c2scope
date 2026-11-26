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


def canon(n):
    return CANON.get(n, n)


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
        for (k, r), ds in self.reach_defs.items():
            if r in self.maybe_reads(k) and r not in self.acc[k][0] and (k, r) not in self.real_maybe:
                continue
            ds = sorted(ds, key=str)
            find(ds[0])
            for d in ds[1:]:
                union(ds[0], d)
        for k, (rd, wr) in enumerate(self.acc):
            for r in wr:
                find((k, r))
        names = {}
        for key in list(parent):
            root = find(key)
            if root in names:
                continue
            names[root] = None
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
        ds = self.reach_defs.get((k, r))
        if not ds:
            return "_u_" + r
        return self.names.get(self.find(sorted(ds, key=str)[0]), "_u_" + r)

    def var_written(self, k, r):
        return self.names.get(self.find((k, r)), "_w_" + r)

    # ---- the frame
    def prologue(self):
        """Indices that leave no tuples: callee-saved pushes / pops, rsp adjustment, saves of callee-saved
        registers (and of the incoming arguments) into the frame and their restores. Returns the set; fills
        self.rsp_at (index -> rsp delta from the entry before it)."""
        out = set()
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
                        self.md.reg_name(op.mem.base) in ("rsp", "rbp"):
                    b = self.md.reg_name(op.mem.base)
                    fo = (self.rsp_at[k] if b == "rsp" else self.rbp_at[k] or 0) + op.mem.disp
                    leas.add(fo)
        if not leas:
            return
        lo = min(leas)
        # up to the saved registers: the object's size is not in the code (a pointer to it is all a callee gets);
        # slots above it the code uses are fields (spilled in the original too)
        hi = min([fo for fo in self.saves if lo < fo < npush_top] + [npush_top])
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
            "xorps", "xorpd", "cvtsi2ss", "cvtsi2sd", "cvttss2si", "cvttsd2si", "cvtss2sd", "cvtsd2ss", "movd", "movq")

    def sse_ty(self, i):
        m = i.mnemonic
        return "e008" if m.endswith(("sd", "pd")) or m in ("cvtsi2sd", "cvtss2sd", "movapd") else "e004"

    def gaddr(self, i, m):
        return i.address + i.size + m.disp

    def opnd(self, k, op, ty_hint=None):
        i = self.ins[k]
        rsp, rbp = self.rsp_at[k], self.rbp_at[k]
        if op.type == X.X86_OP_REG:
            n = self.md.reg_name(op.reg)
            if n.startswith("xmm"):
                v = self.var_read(k, n)
                return "%s:%s" % (v, ty_hint or self.sse_ty(i))
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
                if len(vals) == 1 and len(ds) == 1:
                    return "?gt_%x@@3HA:%s" % (self.img.tls_start() + vals.pop() + m.disp, ty)
                raise Unsupported("TLS access through a computed offset")
        if base == "rip":
            return "?g_%x@@3HA:%s" % (self.gaddr(i, m), ty)
        fo = self.frame_off(k, op, rsp, rbp)
        if fo is not None:
            return self.frame_name(fo, ty)
        if base in ("rsp", "rbp") and idx:
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
                toks.append("%s:%s" % (self.var_read(k, r), self.xmm_ty_at(k, r)))
            else:
                toks.append("%s:%s" % (self.var_read(k, r), self.width_at(k, r)))
        return toks

    def passes_entry(self, k, r):
        """The caller's argument still in its register at a call: passed on when some earlier call in the same
        function passed an explicit value in that position or the callee is this function's own tail; else not."""
        return False

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
                    if pend:
                        self.out_slots[k] = dict(pend)
                        self.arg_store |= set(pend.values())
                    pend = {}
                    continue
                for n, op in enumerate(i.operands):
                    if op.type == X.X86_OP_MEM and op.mem.base and self.md.reg_name(op.mem.base) == "rsp" and \
                            not op.mem.index and op.mem.disp >= 0x20 and k not in self.pro:
                        j = (op.mem.disp - 0x20) // 8
                        fo = self.rsp_at[k] + op.mem.disp
                        if n == 0 and i.mnemonic in ("mov", "movss", "movsd") and len(i.operands) == 2 and \
                                (op.mem.disp - 0x20) % 8 == 0 and i.operands[1].type != X.X86_OP_MEM and \
                                fo not in local:
                            pend[j] = k
                        elif j in pend:
                            del pend[j]

    def find_tls(self):
        """`mov e, [rip+_tls_index]; mov b, gs:[0x58]; mov c, [b+e*8]`: c is this thread's TLS block; [c+D] is the
        __declspec(thread) variable at .tls+D."""
        self.tls_base, self.tls_skip, self.tls_addr = set(), set(), {}
        ins = self.ins
        for k, i in enumerate(ins):
            if not (i.mnemonic == "mov" and "gs:[0x58]" in i.op_str):
                continue
            b = i.operands[0].reg
            adds = [q for q in range(k + 1, min(k + 8, len(ins))) if ins[q].mnemonic == "add" and
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
            j = next((q for q in range(k + 1, min(k + 6, len(ins))) if ins[q].mnemonic == "mov" and
                      ins[q].operands[1].type == X.X86_OP_MEM and ins[q].operands[1].mem.base == b and
                      ins[q].operands[1].mem.scale == 8), None)
            if j is None and adds:
                q0 = min(adds)
                ir = ins[q0].operands[1].mem.index
                q = next((q for q in range(max(0, k - 6), q0) if ins[q].mnemonic == "mov" and
                          ins[q].operands[0].type == X.X86_OP_REG and canon(self.md.reg_name(ins[q].operands[0].reg))
                          == canon(self.md.reg_name(ir)) and "rip" in ins[q].op_str), None)
                if q is None:
                    raise Unsupported("TLS index load")
                self.tls_skip |= {q, k}
                continue
            if j is None:
                raise Unsupported("TLS access shape")
            ir = ins[j].operands[1].mem.index
            q = next((q for q in range(max(0, k - 6), j) if ins[q].mnemonic == "mov" and
                      ins[q].operands[0].type == X.X86_OP_REG and canon(self.md.reg_name(ins[q].operands[0].reg)) ==
                      canon(self.md.reg_name(ir)) and "rip" in ins[q].op_str), None)
            if q is None:
                raise Unsupported("TLS index load")
            self.tls_skip |= {q, k, j}
            self.tls_base.add(j)

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
                        self.args_read.setdefault(p, self.sse_ty(i) if self.is_sse(i) else "e004")
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
        self.webs()
        for k in self.call_toks:
            self.call_toks[k] = self.call_args(k, tail=self.ins[k].mnemonic == "jmp")
        self.lift_body()
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
                if k in self.pro or k in self.arg_store or k in self.tls_skip:
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
                        self.emit(k, "OPRET", [], ["%s:%s" % (self.var_read(k, "rax"), w)])
                    elif self.ret_reg == "xmm0":
                        self.value_ret = True
                        t = self.xmm_ty_at(k, "xmm0")
                        self.float_ret = t
                        self.emit(k, "OPRET", [], ["%s:%s" % (self.var_read(k, "xmm0"), t)])
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
                    cc = (FCC.get(m) if fl == "f" else CCOF[m])
                    if cc is None:
                        raise Unsupported("float compare with %s" % m)
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
                    self.emit(k, m, [d], [self.opnd(k, ops[0])])
                    flags = (d, "0:%s" % d.rsplit(":", 1)[1], "i")
                    continue
                if self.is_sse(i):
                    flags = self.sse(k, i, flags)
                    continue
                if m.startswith("set") and ("j" + m[3:]) in CCOF or m.startswith("cmov") and ("j" + m[4:]) in CCOF:
                    jm = "j" + (m[3:] if m.startswith("set") else m[4:])
                    a, b2, fl = flags or ("0:1004", "0:1004", "i")
                    cc = FCC.get(jm) if fl == "f" else CCOF[jm]
                    if cc is None:
                        raise Unsupported("float compare with %s" % m)
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
                    flags = self.atomic_flags if m in ("lock cmpxchg", "lock inc", "lock dec") else None
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
            raise Unsupported("xorps with a constant (a negation)")
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
        if m in ("cvtss2sd", "cvtsd2ss"):
            dty, sty = ("e008", "e004") if m == "cvtss2sd" else ("e004", "e008")
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
            rank = ["2001", "1001", "2002", "1002", "1004", "2004", "e004", "e008", "1008"]
            wide = {}
            for l in il:
                for n, t in re.findall(r"(?<![\w?@$])(t\d+|_l\d+|_lh\d+(?:_\d+)?):([0-9a-f]{4})\b", l):
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
            if line["notes"].get("s0") == "&" and re.match(r"^_(?:a|l|lh)\d+(?:_\d+)?:", t):
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
                e = "_%s((%s volatile*)(%s)%s)" % (nm, pt, argv[0], "".join(", (%s)(%s)" % (pt, x) for x in argv[1:]))
                if d:
                    return "%s = (%s)%s;" % (self.lvalue(d[0]), rty, e)
                return e + ";"
            m = re.match(r"^\?FUN_([0-9a-f]+)@@YAXXZ$", tgt)
            if m:
                va = int(m.group(1), 16)
                c = self.calls.setdefault(va, {"sigs": set(), "ret": None})
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


def variants(img, va, size):
    """[(opts, C++ text)]: the default lifting first, then the readings the bytes leave open (whether rax / xmm0
    at a ret is a value), each text once."""
    out, seen = [], set()
    text, lf = unit(img, va, size)
    out.append(({}, text))
    seen.add(text)
    alts = []
    rets = [{"ret": "void"}] if lf.ret_reg else [{"ret": "rax"}]
    frames = [{"frame": "wide"}] if lf.frame_region else []
    alts = rets + frames + [dict(r, **f) for r in rets for f in frames]
    for o in alts:
        try:
            t, _ = unit(img, va, size, o)
        except Exception:
            continue
        if t not in seen:
            seen.add(t)
            out.append((o, t))
    return out


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
    try:
        import il2c_lift
        body = il2c_lift.drop_dead(body)
    except Exception:
        pass
    if ret != "void":
        body = re.sub(r"\breturn;", "return 0;", body)
    typed = ["%s a%d" % (CT.get(lf.args_read.get(p, "1008"), "__int64"), p) for p in range(nargs)]
    head = ["#include <intrin.h>"] if em.intrin else []
    if em.prelude:
        head.append("".join(em.prelude))
    head += ["extern unsigned char g_%x[];" % g for g in sorted(em.gl)]
    head += ["extern __declspec(thread) unsigned char g_%x[];" % g for g in sorted(em.tls)]
    for cva, c in sorted(em.calls.items()):
        r = c["ret"] or "void"
        for sig in sorted(c["sigs"]):
            head.append("%s FUN_%x(%s);" % (r, cva, ", ".join(sig)))
    sig = "%s FUN_%x(%s)\n" % (ret, va, ", ".join(typed) or "void")
    return "\n".join(head) + ("\n\n" if head else "") + sig + body + "\n", lf


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


def flag_sets(va, entry=None, flags=None):
    if flags:
        return flags
    return [list(f) for f in (entry or {}).get("flags") or []] or DEFAULT_FLAGS


def check(va, size, text, workdir, flags=None, entry=None):
    import x64check
    path = BINARY
    if path not in _pe:
        _pe[path] = x64check.Pe(path)
    d = os.path.join(workdir, "%x" % va)
    os.makedirs(d, exist_ok=True)
    sets = flag_sets(va, entry, flags)
    json.dump(dict(address="0x%x" % va, size=size, name="FUN_%x" % va, flags=sets[0], accept_also=sets[1:]),
              open(os.path.join(d, "pack.json"), "w"))
    src = os.path.join(d, "lifted.cpp")
    open(src, "w").write(text)
    best = None
    for fl in sets:
        res = x64check.check(_pe[path], va, size, src, fl, full_diff=os.path.join(d, "diff.txt"))
        if res["status"] == "match":
            return res
        if best is None or res.get("differing_instructions", 1 << 30) < best.get("differing_instructions", 1 << 30):
            best = res
    return best


def _one(args):
    global BINARY
    va, entry, workdir, BINARY, flags = args
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
    if st == "does not build":
        out["why"] = "; ".join(res.get("errors") or [])[:160]
    if st == "no match" and res.get("differing_instructions") is None:
        out["why"] = "; ".join(str(e) for e in res.get("errors") or [])[:160]
    return out


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
    a = ap.parse_args(argv)
    BINARY, INDEX = a.binary or BINARY, a.index or INDEX
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
        print(res["status"].upper(), res.get("differing_instructions", ""), " ".join(res.get("flags") or []))
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
    from concurrent.futures import ProcessPoolExecutor
    rows = []
    with ProcessPoolExecutor(a.jobs) as ex:
        for r in ex.map(_one, [(v, idx[v], a.work, BINARY, flags) for v in vas]):
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
