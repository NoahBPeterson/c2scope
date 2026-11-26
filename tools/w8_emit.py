#!/usr/bin/env python3
"""Lift a function's retail machine code to compilable C++ (route 1 of the residual pipeline).

usage: w8_emit.py VA [VA..] [--out DIR]
       w8_emit.py --list FILE [--limit N]      (FILE: a JSON list of "0040a0f0" strings)

1. Disassemble the retail bytes at VA (capstone, 32-bit, detail on).
2. Recover the frame: `mov ebp, esp` gives a frame pointer; `[ebp-d]`/`[esp+d]` slots become typed
   locals, `[ebp+d]` slots become parameters. A slot's type comes from the widths used on it
   (movss -> float, movsd -> double, movzx byte -> unsigned char, lea -> pointer).
3. Translate instruction by instruction with a symbolic expression per register, so
   `mov eax,[ebp-4]; add eax,1; mov [ebp-8],eax` becomes `l_8 = l_4 + 1;` — folded by construction.
   Memory writes are emitted where they happen, in program order.
4. Recover control flow: `cmp`/`test` (or the arithmetic that set the flags) pair with the following
   jcc to give a real condition; targets get labels, and forward branches over a single-exit region
   become `if (...) { }` blocks. Anything else stays `if (...) goto L;`.
5. Emit a TU: `g_<va>` externs for the globals it touches, `FUN_<va>` for callees.

Transcription-grade C++: real statements, types, control flow and callees. Not byte-exact — a
compiling, checkable starting point.
"""
import argparse, collections, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import bisect
import capstone, pefile

IMAGE = os.path.join(ROOT, "work", "SporeApp.analysis.bin")
SPORE = os.path.join(ROOT, "..", "spore-decomp")
FINGERPRINTS = os.path.join(SPORE, "tools", "ci", "fingerprints.jsonl")


def load_masks():
    """Per function, the byte offsets whose 4-byte value is an address: the fingerprints' `mask` (the
    retail .reloc fixups, every immediate/displacement holding an image address or an fs: offset, and
    the displacements of calls that leave the function). A 4-byte immediate there is a name in the
    source, not a number, so the emitter spells it `g_<address>`."""
    out = {}
    with open(FINGERPRINTS) as f:
        for line in f:
            j = json.loads(line)
            out[int(j["va"], 16)] = {s + k for s, n in j["mask"] for k in range(n)}
    return out


class Image:
    def __init__(self, path=IMAGE):
        self.pe = pefile.PE(path, fast_load=True)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.data = open(path, "rb").read()
        self.secs = [(s.VirtualAddress + self.base, s.SizeOfRawData, s.PointerToRawData) for s in self.pe.sections]
        # the dword slots the loader fixes up: a 4-byte operand there is an address in the source, not
        # a number, so it must be spelled as a named global (retail's relocations decide, not a guess)
        self.relocs = []
        d = self.pe.OPTIONAL_HEADER.DATA_DIRECTORY[5]
        if d.VirtualAddress and d.Size:
            import struct as _st
            raw = self.pe.get_data(d.VirtualAddress, d.Size)
            for k in range(0, len(raw) - 8, 8):
                rva, typ = _st.unpack_from("<II", raw, k)
                if typ == 3:
                    self.relocs.append(rva + self.base)
            self.relocs.sort()
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        self.md.detail = True

    def reloc_at(self, va):
        i = bisect.bisect_left(self.relocs, va)
        return i < len(self.relocs) and self.relocs[i] == va

    def off(self, va):
        for b, sz, raw in self.secs:
            if b <= va < b + sz:
                return raw + va - b
        return None

    def insns(self, va, n):
        o = self.off(va)
        return list(self.md.disasm(self.data[o:o + n], va)) if o is not None else []


def g(va):
    return "g_%08x" % va


def fun(va):
    return "FUN_%08x" % va


REGS8 = {"al": "eax", "ah": "eax", "bl": "ebx", "bh": "ebx", "cl": "ecx", "ch": "ecx",
         "dl": "edx", "dh": "edx"}
REGS16 = {"ax": "eax", "bx": "ebx", "cx": "ecx", "dx": "edx", "si": "esi", "di": "edi"}
CT = {1: "unsigned char", 2: "unsigned short", 4: "unsigned int"}
CC = {"e": "==", "ne": "!=", "l": "<", "g": ">", "le": "<=", "ge": ">=", "b": "<", "a": ">",
      "be": "<=", "ae": ">="}
UNSIGNED = {"b", "a", "be", "ae"}
SSE_SET = {"movaps", "movups", "movdqa", "movdqu", "xorps", "xorpd", "andps", "andpd", "andnps",
           "andnpd", "orps", "orpd", "addss", "subss", "mulss", "divss", "addsd", "subsd", "mulsd",
           "divsd", "minss", "maxss", "minsd", "maxsd", "sqrtss", "sqrtsd", "addps", "subps", "mulps",
           "divps", "addpd", "subpd", "mulpd", "divpd", "comiss", "ucomiss", "comisd", "ucomisd",
           "movd", "movq", "cvtsi2ss", "cvtsi2sd", "cvttss2si", "cvttsd2si", "cvtss2si", "cvtsd2si",
           "cvtss2sd", "cvtsd2ss", "shufps"}
ARITH_OP = {"sal": "<<", "shl": "<<", "shr": ">>", "sar": ">>", "imul": "*", "and": "&", "or": "|",
            "xor": "^", "add": "+", "sub": "-"}


MASKS = None


class Fn:
    def __init__(self, img, va, size):
        self.img, self.va, self.size = img, va, size
        self.ins = img.insns(va, size)
        self.slots, self.args, self.slot_ty = {}, {}, {}
        self.reg = {}
        self.stack = []
        self.out = []                     # (address, text)
        self.labels, self.targets = set(), set()
        self.cmp = None
        self.cf = None                    # the carry flag as an expression, for sbb/adc
        self.globals, self.callees = set(), set()
        self.frame_ptr, self.ret_pop = False, 0
        self.skipped = collections.Counter()
        self.fpu = []                      # the x87 stack, as expressions
        self.fpu_ty = []                   # parallel to fpu: "float" / "double" of each entry
        self.xmm = set()                   # xmm registers used (declared as float locals)
        self.temps = []                    # (name, type) for materialised registers
        self.thisreg = None                # 'ecx' / 'edx' when the function takes them as arguments
        self.fastreg = None
        self.ptype = {}                    # frame offset -> char* / float / double (from the pre-pass)
        self.tail = False                  # the body ends in a jmp out of the function (a tail call)
        self.ret_void = False              # the epilogue returns no value
        self.ret_entries = {}              # jmp-into-epilogue address -> (start_k, jmp_k)
        self.ret_temp = None               # the return value temp, when a merged epilogue needs one
        self.ret_tyx = None                # its type, when both paths return a float/double
        self._ret_ty = None                # the declared return type, inferred in ret_type()
        self.slot_signed = {}              # frame offset -> True when a movsx loads it
        self.any_value_ret = False         # a `return expr;` ran (a bare ret on one path does not make the fn void)
        self.needs_math = False
        self.needs_intrin = False          # fs:[N] reads / writes: <intrin.h>'s __readfsdword / __writefsdword
        self.cur_va = self.cur_size = self.cur_k = 0
        self.delta = {}
        global MASKS
        if MASKS is None:
            MASKS = load_masks()
        self.mask = MASKS.get(va, set())
        self.calleesig = {}                # callee va -> (set of argument counts, return value used)
        self.thiscallees = {}              # callee va -> {"arities": set, "ret": bool}: __thiscall stand-ins
        self.float_callees = set()         # callees whose result is read from ST0

    # ---- operands
    def esp_disp(self, disp, k=None):
        """Entry-relative offset of `[esp+disp]`: >= 4 is an argument, below it a local. Kept clear of
        the argument slots a local can coincide with once esp has moved."""
        k = self.cur_k if k is None else k
        rel = disp - self.delta.get(k, 0)
        return rel if rel >= 4 else rel - 4

    def memkind(self, op):
        m = op.mem
        rbase = self.img.md.reg_name(m.base) if m.base else None
        if rbase == "esp" and not m.index:
            return ("slot", self.esp_disp(m.disp))
        # `[ebp+d]` is a frame slot only when a real ebp frame exists (`push ebp; mov ebp, esp`). In a
        # frameless function ebp is a callee-saved register holding a caller's value, and a disp like
        # 0x142fcd0 is a static-table offset (`mov cl,[ebp+0x142fcd0]` in 008a39c0): minting an
        # argument for it made signature() fill 5.3M contiguous parameters — an 80 MB C++ line.
        if rbase == "ebp" and not m.index and self.frame_ptr:
            return ("slot", m.disp)
        if not m.base and not m.index:
            return ("global", m.disp)
        base = self.regval(rbase) if rbase else "0"
        index = self.regval(self.img.md.reg_name(m.index)) if m.index else None
        return ("ptr", base, index, m.scale, m.disp)

    def disp_name(self, op):
        """The disp32 of a memory operand spelled as a named address, or None when the displacement
        is not a relocated address field (a stack slot offset, a small struct offset, ...)."""
        v = op.mem.disp & 0xFFFFFFFF
        if not 0x00401000 <= v < 0x02000000:
            return None
        b = self.cur_ins.bytes
        d = v.to_bytes(4, "little")
        # the disp32 sits at the end of the instruction, or 4 bytes earlier when an immediate
        # operand follows it (`mov dword ptr [eax + disp32], imm32`)
        for pos in (len(b) - 4, len(b) - 8):
            if pos >= 0 and b[pos:pos + 4] == d and (self.cur_va - self.va + pos) in self.mask:
                self.globals.add(v)
                return "((unsigned int)%s)" % g(v)
        return None

    def slot(self, disp, ty="unsigned int"):
        if disp >= 0:
            name = self.args.setdefault(disp, "a_%d" % disp)
        else:
            name = self.slots.get(disp)
            if name is None:
                name = self.slots[disp] = "l_%d" % -disp
        self.slot_ty[disp] = self.ptype.get(disp) or self.slot_ty.get(disp) or ty
        if self.slot_signed.get(disp):
            if self.slot_ty[disp] == "unsigned char":
                self.slot_ty[disp] = "char"
            elif self.slot_ty[disp] == "unsigned short":
                self.slot_ty[disp] = "short"
        return name

    FSREF = re.compile(r"__FS\((\d+)\)")

    def lval(self, op, ty=None):
        if op.mem.segment == capstone.x86.X86_REG_FS:
            # `fs:[N]` (the SEH chain at fs:[0], TLS at fs:[0x2c]) is not memory a C pointer reaches. Spelled
            # as a plain [N] it became a store to address N (f3 00402fb0: `*(unsigned int*)0 = &l_16;`). A
            # marker here; emit() and render() turn it into __readfsdword / __writefsdword.
            self.needs_intrin = True
            return "__FS(%d)" % (op.mem.disp & 0xFFFFFFFF)
        kind = self.memkind(op)
        ty = ty or CT.get(op.size, "unsigned int")
        if kind[0] == "slot":
            name = self.slot(kind[1], ty)
            if ty in ("float", "double") and self.slot_ty.get(kind[1]) not in ("float", "double"):
                return "*(%s*)(&%s)" % (ty, name)
            return name
        if kind[0] == "global":
            va = kind[1] & 0xFFFFFFFF
            if not 0x00401000 <= va < 0x02000000:
                return "*(%s*)%d" % (ty, va)
            self.globals.add(va)
            return "*(%s*)%s" % (ty, g(va))
        base, index, scale, disp = kind[1:]
        e = base
        if index:
            e = "%s + %s * %d" % (base, index, scale) if scale > 1 else "%s + %s" % (base, index)
        if disp:
            # where retail has a relocation the disp is a named address, not a number (the check
            # fails "a number at offset N where retail has an address"): 011c6bc0's eight
            # `movzx reg, byte ptr [reg + disp32]` loads of one table wrote 21958952 bare.
            dn = self.disp_name(op)
            if dn is not None:
                e = "%s + %s" % (e, dn)
            else:
                e = "%s + %d" % (e, disp) if disp > 0 else "%s - %d" % (e, -disp)
        return "*(%s*)(%s)" % (ty, e)

    def rv(self, op, ty=None):
        if op.type == capstone.x86.X86_OP_REG:
            n = self.img.md.reg_name(op.reg)
            if n in REGS8:
                return "((unsigned char)(%s))" % self.regval(REGS8[n])
            if n in REGS16:
                return "((unsigned short)(%s))" % self.regval(REGS16[n])
            if n.startswith("xmm"):
                self.xmm.add(n)
                return self.reg.get(n, n)
            return self.regval(n)
        if op.type == capstone.x86.X86_OP_IMM:
            v = op.imm & 0xFFFFFFFF
            # a dword immediate occupying a relocated slot is an address the source named; anything
            # else (an id, a float bit pattern, a small constant) stays a number
            off = self.cur_va - self.va + self.cur_size - 4
            if op.size == 4 and (off in self.mask or self.img.reloc_at(self.cur_va + self.cur_size - 4)):
                self.globals.add(v)
                return "((unsigned int)%s)" % g(v)
            return str(v - (1 << 32) if v & 0x80000000 else v)
        if op.type == capstone.x86.X86_OP_MEM:
            lv = self.lval(op, ty)
            # an integer operation on a float-typed slot is a bit test on the storage: keep the
            # integer view of the same bytes, not a float&int expression (which is a C error)
            if op.size in (4, 2, 1) and self.slot_ty.get(self.disp_of(op)) in ("float", "double"):
                return "(*(unsigned %s*)&(%s))" % ("int" if op.size == 4 else "short" if op.size == 2 else "char", lv)
            return lv
        return "0"

    def setreg(self, name, expr):
        base = REGS8.get(name, name)
        self.reg[REGS16.get(base, base)] = expr

    def newtemp(self, ty="int"):
        name = "t_%d" % len(self.temps)
        self.temps.append((name, ty))
        return name

    # A register's expression is built by substitution, so an instruction that reads the destination
    # twice (`add ecx, ecx`, `imul ecx, ecx`, `lea ecx, [ecx + ecx*4]`) embeds the whole expression
    # twice and doubles it. Retail contains chains of those, and an uncapped chain reaches gigabytes
    # in a few milliseconds (one 5055-byte function built a multi-gigabyte string). Spilling to a
    # temporary once an expression outgrows EXPR_CAP keeps the model linear and the C++ readable.
    EXPR_CAP = 4000

    def regval(self, name):
        """A register as an expression; an unassigned one becomes a declared temporary, never a bare name.
        A long expression is stored into a temporary first, so nothing downstream can double it."""
        base = REGS16.get(REGS8.get(name, name), name)
        if base in self.reg and len(self.reg[base]) > self.EXPR_CAP:
            t = self.newtemp()
            self.emit(self.cur_va, "%s = %s;" % (t, self.reg[base]))
            self.reg[base] = t
        if base not in self.reg:
            if base in ("ecx", "edx") and base == self.thisreg:
                self.reg[base] = "((unsigned int)self)"
            elif base == "edx" and base == self.fastreg:
                self.reg[base] = "a_edx"
            else:
                self.reg[base] = self.newtemp()
        return self.reg[base]

    MEMSTORE = re.compile(r"^\(?\*\(")

    def emit(self, addr, text):
        lhs, eq, _rhs = text.partition(" = ")
        if eq and self.MEMSTORE.match(lhs):
            text = self.spill_loads(addr, text)
        m = self.FSREF.fullmatch(lhs.strip()) if eq else None
        if m:
            text = "__writefsdword(%s, (unsigned long)(%s));" % (m.group(1), self.FSREF.sub(
                r"__readfsdword(\1)", text.partition(" = ")[2].rstrip().rstrip(";")))
        self.out.append((addr, self.FSREF.sub(r"__readfsdword(\1)", text)))

    def spill_loads(self, addr, text):
        """Before a store to memory, a register whose value is a memory read holds the value read
        BEFORE the store: fix it in a temp. Left symbolic, the read happened after the store
        (00de52d0: `mov eax,[edx-4]; ...; add [ecx+0x114],-4; ret` returned the slot below the new
        top). The store's own text was built from the old expressions already."""
        for r, e in list(self.reg.items()):
            if e is None or "*(" not in e or r in ("esp", "ebp"):
                continue
            t = self.newtemp("unsigned int")
            self.out.append((addr, "%s = %s;" % (t, e)))
            self.reg[r] = t
            lhs, eq, rhs = text.partition(" = ")
            if e in rhs:                 # the stored value is that register: store the temp (retail stores the reg)
                text = lhs + eq + rhs.replace(e, t)
        return text

    def lab(self, va):
        self.targets.add(va)
        return "L_%08x" % va

    # ---- driver
    def run(self):
        drop = self.prologue()
        self.entry_regs()
        self.prepass()
        self.find_ret_entries()
        self.pending = {}
        self._dropped = {self.ins[j].address for j in drop}
        for k, ins in enumerate(self.ins):
            if k in drop:
                continue
            if ins.address in self.targets:
                self.labels.add(ins.address)
            self.restore_state(ins, k)
            self.cur_addr = ins.address
            if os.environ.get("W8_PROG"):
                print("k=%d %08x %-8s %s" % (k, ins.address, ins.mnemonic, ins.op_str), flush=True)
            self.one(ins, k)

    def epilogue_start(self):
        """The lowest address of the function's shared epilogue run (`mov esp,ebp` of
        `mov esp,ebp; pop ebp; ret 4`), or None when there is no `ret`-ending run."""
        if not hasattr(self, "epi"):
            self.is_epilogue(self.va)   # force the epi set
        rets = [ins.address for ins in self.ins if ins.mnemonic == "ret"]
        if not rets:
            return None
        for ins in self.ins:
            if ins.mnemonic == "mov" and ins.op_str == "esp, ebp" and \
                    self.is_epilogue(ins.address):
                return ins.address
        return None

    def find_ret_entries(self):
        """Paths into a MERGED epilogue (one the failing path also falls into) are mid-body returns
        whose value comes from the block in front of them; a linear scan loses the value to the
        last eax write before the `ret`. Each entry parks eax into a return temp and returns the
        temp, so every path returns its own value:

        - `jmp <epilogue>`: the jmp is the return (the source text ends there).
        - the instruction before the epilogue (fall-through): process it, then park — the value is
          whatever it left in eax (0044aa10's `xor eax,eax` of the failing path).
        """
        epi_start = self.epilogue_start()
        if epi_start is None:
            return
        for ins in self.ins:
            if ins.mnemonic == "jmp" and ins.operands and \
                    ins.operands[0].type == capstone.x86.X86_OP_IMM and \
                    ins.operands[0].imm == epi_start and ins.address not in self.epi:
                self.ret_entries[ins.address] = "jmp"
        # the fall-through predecessor of the epilogue, when it can fall through
        prev = None
        for ins in self.ins:
            if ins.address >= epi_start:
                break
            prev = ins
        if prev is not None and prev.address not in self.ret_entries and \
                prev.mnemonic not in ("jmp", "ret") and prev.address not in self.epi:
            self.ret_entries[prev.address] = "fall"

    def entry_regs(self):
        """ecx / edx read before any write are incoming arguments: __thiscall `this`, __fastcall's second
        register. A read includes being the base or the index of a memory operand, since
        `movss xmm0,[ecx+0x284]` reads the object without naming ecx as an operand."""
        WRITEONLY = {"mov", "movzx", "movsx", "movsxd", "lea", "pop", "sete", "setne", "setl", "setg",
                     "setle", "setge", "setb", "seta", "setbe", "setae", "movss", "movsd", "movaps"}
        written, seen = set(), set()
        # `push ecx` right after the frame setup reserves a local (MSVC's `push reg` red-zone); it
        # does not pass the incoming ecx anywhere. A function whose prologue is
        # `push ebp; mov ebp,esp; push ecx` takes its arguments on the stack, so counting that push
        # as a read minted a phantom `self` and a __fastcall signature retail never used (005497e0).
        pro_end, saw_frame = 0, False
        for ins in self.ins:
            m = ins.mnemonic
            if m in ("push", "nop"):
                if m == "push" and ins.op_str == "ebp":
                    saw_frame = True
                pro_end += 1
            elif (m == "mov" and ins.op_str.startswith("ebp, esp")) or \
                    (m == "sub" and ins.op_str.startswith("esp,")):
                pro_end += 1
            else:
                break
        for idx, ins in enumerate(self.ins):
            ops, m = ins.operands, ins.mnemonic
            prologue_push = saw_frame and idx < pro_end and m == "push"
            if not ops:
                continue
            dst_reg = self.img.md.reg_name(ops[0].reg) if ops[0].type == capstone.x86.X86_OP_REG else None
            dest_only = dst_reg is not None and m in WRITEONLY
            reads = []
            # `xor ecx,ecx` reads nothing: both operands are the register being cleared, and
            # taking the second one as a read minted a spurious __fastcall `self` — a function
            # that zeroes ecx first, then takes its argument on the stack (0085f190, 011be4c0).
            same_xor = m == "xor" and len(ops) == 2 and \
                ops[0].type == capstone.x86.X86_OP_REG and ops[1].type == capstone.x86.X86_OP_REG \
                and ops[0].reg == ops[1].reg
            for k, op in enumerate(ops):
                if op.type == capstone.x86.X86_OP_REG:
                    n = self.img.md.reg_name(op.reg)
                    if k == 0 and dest_only:
                        continue
                    if same_xor and op.reg == ops[0].reg:
                        continue
                    if prologue_push and n in ("ecx", "edx"):
                        continue
                    reads.append(n)
                elif op.type == capstone.x86.X86_OP_MEM:
                    for r in (op.mem.base, op.mem.index):
                        if r:
                            reads.append(self.img.md.reg_name(r))
            for n in reads:
                if n not in ("ecx", "edx") or n in written or n in seen:
                    continue
                if n == "ecx" and self.thisreg is None:
                    self.thisreg = "ecx"
                elif n == "edx" and self.fastreg is None:
                    self.fastreg = "edx"
                else:
                    continue
                seen.add(n)
            if dst_reg:
                written.add(dst_reg)
                # a 32-bit write initialises the whole register, so a later read is not an argument

    def prologue(self):
        drop = set()
        pops = collections.Counter()
        for ins in self.ins:
            if ins.mnemonic == "pop" and ins.op_str in ("ebp", "ebx", "esi", "edi"):
                pops[ins.op_str] += 1
        for k, ins in enumerate(self.ins):
            m, o = ins.mnemonic, ins.op_str
            if m == "push" and o in pops and pops[o] > 0:
                pops[o] -= 1
                drop.add(k)
            elif m == "leave" or (m == "mov" and o in ("ebp, esp", "esp, ebp")):
                drop.add(k)
            elif m == "pop" and o in ("ebp", "ebx", "esi", "edi"):
                drop.add(k)
            elif m == "sub" and o.startswith("esp, ") and k < 6:
                drop.add(k)
            elif m == "add" and o.startswith("esp, "):
                drop.add(k)
            if m == "mov" and o == "ebp, esp":
                self.frame_ptr = True
            if m == "ret" and o:
                self.ret_pop = int(o, 0)
        # how far esp has moved from its entry value at each instruction: `[esp+d]` is then an entry
        # offset of d - delta, which is an argument when it is >= 4 and a local below it
        delta, cur = {}, 0
        for k, ins in enumerate(self.ins):
            delta[k] = cur
            m, o = ins.mnemonic, ins.op_str
            try:
                if m == "push":
                    cur += 4
                elif m == "pop":
                    cur -= 4
                elif m in ("sub", "add") and o.startswith("esp, "):
                    n = int(o.split(",")[1], 0)
                    cur += n if m == "sub" else -n
            except ValueError:
                pass
        self.delta = delta
        return drop

    def disp_of(self, op):
        """The frame offset an [ebp+/-d] / [esp+d] operand addresses, or None."""
        if op.type != capstone.x86.X86_OP_MEM or op.mem.index:
            return None
        b = self.img.md.reg_name(op.mem.base) if op.mem.base else None
        if b == "ebp":
            return op.mem.disp
        if b == "esp":
            return self.esp_disp(op.mem.disp)
        return None

    def prepass(self):
        """Decide each frame slot's type before translating.

        A slot is `float`/`double` only when every reference to it is a movss/movsd and it is never
        used as an address; anything else stays an integer of the width its store writes, so pointer
        traffic is `unsigned int` arithmetic with a cast at the dereference
        (`*(unsigned int*)(l_60 + 588)`), which always compiles. A slot that is a float temporary and
        also an argument or pointer (retail reuses an argument slot as scratch) stays an integer and
        the float accesses read it through `*(float*)(&slot)`, because one C++ local cannot be both."""
        base_slots = self.base_slots()
        # A movsx load names the slot's source type: a byte load sign-extended is a `char`, not an
        # `unsigned char`. `return c - 48` on an `unsigned char` parameter emits movzx; retail's
        # movsx is only reproducible when the parameter is declared signed.
        for ins in self.ins:
            if ins.mnemonic == "movsx" and len(ins.operands) == 2 and \
                    ins.operands[1].type == capstone.x86.X86_OP_MEM:
                d = self.disp_of(ins.operands[1])
                if d is not None:
                    self.slot_signed[d] = True
        fref, other = collections.Counter(), collections.Counter()
        for ins in self.ins:
            f = ins.mnemonic in ("movss", "movsd")
            for op in ins.operands:
                d = self.disp_of(op) if op.type == capstone.x86.X86_OP_MEM else None
                if d is None:
                    continue
                if f:
                    fref[d] += 1
                else:
                    other[d] += 1
        if self.thisreg:
            for ins in self.ins:
                if ins.mnemonic == "mov" and len(ins.operands) == 2:
                    d, sc = ins.operands
                    if d.type == capstone.x86.X86_OP_MEM and sc.type == capstone.x86.X86_OP_REG \
                            and self.img.md.reg_name(sc.reg) == self.thisreg:
                        dd = self.disp_of(d)
                        if dd is not None:
                            other[dd] += 1
        for d in fref:
            if not other.get(d) and d not in base_slots:
                self.ptype[d] = "float"

    def base_slots(self):
        """Frame offsets whose value is used as an address somewhere: `mov eax,[ebp-8]; mov ecx,[eax+4]`
        makes -8 one. A slot that is dereferenced is a word, never a float, however it is loaded."""
        origin, out = {}, set()

        def note(op):
            if op.type != capstone.x86.X86_OP_MEM:
                return
            for r in (op.mem.base, op.mem.index):
                if r:
                    n = self.img.md.reg_name(r)
                    if origin.get(n) is not None:
                        out.add(origin[n])

        for ins in self.ins:
            m, ops = ins.mnemonic, ins.operands
            if not ops:
                continue
            d = ops[0]
            dn = self.img.md.reg_name(d.reg) if d.type == capstone.x86.X86_OP_REG else None
            for op in ops:
                note(op)
            if dn is None:
                continue
            if m == "lea":
                o = self.disp_of(ops[1]) if ops[1].type == capstone.x86.X86_OP_MEM else None
                origin[dn] = o
            elif m in ("mov", "movzx", "movsx", "movsxd") and len(ops) > 1:
                src = ops[1]
                if src.type == capstone.x86.X86_OP_MEM:
                    origin[dn] = self.disp_of(src)
                elif src.type == capstone.x86.X86_OP_REG:
                    origin[dn] = origin.get(self.img.md.reg_name(src.reg))
                else:
                    origin[dn] = None
            elif m in ("add", "sub", "and", "or", "xor", "shl", "shr", "sar", "imul"):
                pass                      # pointer arithmetic keeps the slot it came from
            else:
                origin[dn] = None
        return out

    def one(self, ins, k=None):
        m, ops, a = ins.mnemonic, ins.operands, ins.address
        self.cur_va, self.cur_size, self.cur_k, self.cur_ins = ins.address, ins.size, k, ins
        if a in self.ret_entries:
            # a path into the merged epilogue whose value the linear scan would lose: park eax
            # now, return the temp, and skip the jmp itself (the source text ends with the return)
            if self.fpu and self.ret_entries[a] == "jmp":
                # the value is on the x87 stack, not in eax (`fld [esp]` then `jmp epilogue`)
                self._ret_ty = self.fpu_ty[-1] if self.fpu_ty else "float"
                self.emit(a, "return %s;" % self.fpu[-1])
                self.any_value_ret = True
                if self.fpu_ty:
                    self.ret_tyx = self.fpu_ty[-1]
                return
            if self.ret_temp is None:
                ty = (self.fpu_ty[-1] if self.fpu_ty else "unsigned int")
                self.ret_temp = self.newtemp(ty)
                self.ret_tyx = ty if ty in ("float", "double") else None
            if self.fpu:
                self.emit(a, "%s = %s;" % (self.ret_temp, self.fpu[-1]))
                self.fpu.pop()
                if self.fpu_ty:
                    self.fpu_ty.pop()
            else:
                self.emit(a, "%s = %s;" % (self.ret_temp, self.regval("eax")))
            self.emit(a, "return %s;" % self.ret_temp)
            self.any_value_ret = True
            if self.ret_entries[a] == "jmp":
                return
        if m in ("nop", "int3", "leave", "pop", "pusha", "popa", "cdq") or m.startswith("npad"):
            if m == "cdq":
                self.setreg("edx", "((int)(%s) < 0 ? -1 : 0)" % self.regval("eax"))
            return
        if m == "ret":
            self.ret_stmt()
        elif m in ("mov", "movzx", "movsx", "movsxd"):
            self.do_mov(ins)
        elif m in ("movss", "movsd"):
            self.do_movf(ins)
        elif m == "lea":
            self.do_lea(ins)
        elif m in ARITH_OP or m in ("neg", "not", "inc", "dec", "rol", "ror"):
            self.do_arith(ins)
        elif m in ("cmp", "test"):
            self.do_cmp(ins)
        elif m.startswith("j") and m != "jmp":
            self.do_jcc(ins)
        elif m == "jmp":
            if ops[0].type == capstone.x86.X86_OP_IMM and self.va <= ops[0].imm < self.va + self.size:
                if self.is_epilogue(ops[0].imm):
                    if self.reg.get("eax") is not None:
                        # `mov eax, X; jmp <shared epilogue>` is one of the source's early `return
                        # X`s: the epilogue is shared because the compiler merged the returns.
                        # Emitting the return here is what keeps the value: a `goto` would read
                        # whatever the linear scan left in eax after this point (the `xor eax,eax`
                        # of the failing path).
                        self.emit(a, "return %s;" % self.ret_expr())
                    else:
                        # a `jmp` into the epilogue whose value was NOT written in front of it:
                        # this is the multi-block return of c2's SSA (0044aa10: the table load's
                        # eax is still live from the load block, and the source text just left it
                        # there). Make the value explicit: park it in a return temp and merge the
                        # temp out of every path of the epilogue.
                        if self.ret_temp is None:
                            self.ret_temp = self.newtemp("unsigned int")
                        self.emit(a, "%s = %s;" % (self.ret_temp, self.regval("eax")))
                        self.emit(a, "goto %s;" % self.lab(ops[0].imm))
                else:
                    self.emit(a, "goto %s;" % self.lab(ops[0].imm))
                    self.branch_state(ops[0].imm)
            else:
                self.do_tail(ins)
        elif m == "call":
            self.do_call(ins, k)
        elif m == "push":
            self.stack.append(self.rv(ops[0]))
        elif m == "xchg":
            self.do_xchg(ins)
        elif m in ("sbb", "adc"):
            self.do_adc_sbb(ins, m == "adc")
        elif m in ("div", "idiv"):
            self.do_div(ins)
        elif m.startswith("set") and m[3:] in CC:
            self.setreg(self.img.md.reg_name(ops[0].reg), "(%s)" % self.cond(m[3:]))
        elif m in SSE_SET:
            self.do_sse(ins)
        elif m.startswith("f"):
            self.do_fpu(ins)
        else:
            self.skipped[m] += 1
            self.emit(a, "/* %s %s */" % (m, ins.op_str))

    # ---- instruction families
    def do_mov(self, ins):
        ops = ins.operands
        src = self.rv(ops[1])
        if ops[0].type == capstone.x86.X86_OP_REG:
            if ins.mnemonic == "movsx":
                src = "((int)(%s))" % src
            elif ins.mnemonic in ("movzx", "movsxd"):
                src = "((unsigned int)(%s))" % src
            self.setreg(self.img.md.reg_name(ops[0].reg), src)
            return
        self.emit(ins.address, "%s = %s;" % (self.lval(ops[0]), src))

    def do_movf(self, ins):
        ops = ins.operands
        ty = "float" if ins.mnemonic == "movss" else "double"
        d, s = ops[0], ops[1]
        if s.type == capstone.x86.X86_OP_REG and self.img.md.reg_name(s.reg).startswith("xmm"):
            src = "%s.f[0]" % self.xmm_name(s)
        elif s.type == capstone.x86.X86_OP_MEM:
            src = self.lval(s, ty)
        else:
            src = self.rv(s)
        if d.type == capstone.x86.X86_OP_REG and self.img.md.reg_name(d.reg).startswith("xmm"):
            # an xmm register is an XmmWord: lane 0 is the scalar, written where it is produced
            self.emit(ins.address, "%s.f[0] = %s;" % (self.xmm_name(d), src))
            return
        if d.type == capstone.x86.X86_OP_REG:
            self.setreg(self.img.md.reg_name(d.reg), src)
            if self.img.md.reg_name(d.reg) == "eax":
                # a new eax value: an older integer condition no longer describes it, and leaving
                # the setcc-derived cmp in place made a later comiss test the stale one (00efc4c0)
                self.cmp = None
                self.cf = None
            return
        self.emit(ins.address, "%s = %s;" % (self.lval(d, ty), src))

    def do_lea(self, ins):
        ops = ins.operands
        kind = self.memkind(ops[1])
        if kind[0] == "slot":
            self.setreg(self.img.md.reg_name(ops[0].reg), "((unsigned int)(&%s))" % self.slot(kind[1]))
            return
        if kind[0] == "global":
            va = kind[1] & 0xFFFFFFFF
            if 0x00401000 <= va < 0x02000000:
                self.globals.add(va)
                self.setreg(self.img.md.reg_name(ops[0].reg), "((unsigned int)%s)" % g(va))
            else:
                self.setreg(self.img.md.reg_name(ops[0].reg), str(va))
            return
        base, index, scale, disp = kind[1:]
        e = base
        if index:
            e = "%s + %s * %d" % (base, index, scale) if scale > 1 else "%s + %s" % (base, index)
        if disp:
            e = "%s + %d" % (e, disp) if disp > 0 else "%s - %d" % (e, -disp)
        self.setreg(self.img.md.reg_name(ops[0].reg), "(%s)" % e)

    def do_arith(self, ins):
        m, ops = ins.mnemonic, ins.operands
        if m == "xor" and len(ops) == 2 and ops[0].type == ops[1].type == capstone.x86.X86_OP_REG \
                and ops[0].reg == ops[1].reg:
            self.setreg(self.img.md.reg_name(ops[0].reg), "0")
            self.cmp = ("0", "0", "e")
            return
        # `or r, -1` / `and r, 0` set r whatever it held: c2's spelling of `r = -1` / `r = 0` (a 3-byte
        # `or eax,-1` is shorter than `mov eax,-1`). Reading r first used a value nothing had written
        # (00946900: `return t_0 | -1;`).
        if m in ("or", "and") and len(ops) == 2 and ops[0].type == capstone.x86.X86_OP_REG and \
                ops[1].type == capstone.x86.X86_OP_IMM and ops[0].size == 4 and \
                (ops[1].imm & 0xFFFFFFFF) == (0xFFFFFFFF if m == "or" else 0):
            v = "-1" if m == "or" else "0"
            self.setreg(self.img.md.reg_name(ops[0].reg), v)
            self.cmp = (v, "0", "e")
            return
        if m in ("neg", "not", "inc", "dec"):
            if ops[0].type == capstone.x86.X86_OP_REG:
                cur = self.rv(ops[0])
                e = {"neg": "-((int)(%s))" % cur, "not": "~(%s)" % cur,
                     "inc": "(%s) + 1" % cur, "dec": "(%s) - 1" % cur}[m]
                self.setreg(self.img.md.reg_name(ops[0].reg), e)
                self.cmp = (e, "0", "e" if m == "dec" else "ne")
            else:
                d = self.lval(ops[0])
                e = {"neg": "-((int)(%s))" % d, "not": "~(%s)" % d,
                     "inc": "(%s) + 1" % d, "dec": "(%s) - 1" % d}[m]
                self.emit(ins.address, "%s = %s;" % (d, e))
                self.cmp = (d, "0", "e")
            return
        if m in ("rol", "ror"):
            # c2 folds `(x << n) | (x >> (32-n))` into a constant rotate (probed at /O2); the
            # variable-count form is not folded (it compiles to neg/and/shl/shr/or), so only an
            # immediate count is spelled this way.
            n = ops[1]
            if n.type == capstone.x86.X86_OP_IMM:
                w = ops[0].size * 8
                k = n.imm % w
                ut = {4: "unsigned int", 2: "unsigned short", 1: "unsigned char"}[ops[0].size]
                if k:
                    if ops[0].type == capstone.x86.X86_OP_REG:
                        reg = self.img.md.reg_name(ops[0].reg)
                        cur = self.regval(reg)
                        e = ("((%s)(((%s)(%s) << %d) | ((%s)(%s) >> %d)))" % (ut, ut, cur, k, ut, cur, w - k)
                             if m == "rol" else
                             "((%s)(((%s)(%s) >> %d) | ((%s)(%s) << %d)))" % (ut, ut, cur, k, ut, cur, w - k))
                        self.setreg(reg, e)
                        self.cmp = (self.regval(reg), "0", "e")
                    else:
                        d = self.lval(ops[0])
                        e = ("((%s)(((%s)(%s) << %d) | ((%s)(%s) >> %d)))" % (ut, ut, d, k, ut, d, w - k)
                             if m == "rol" else
                             "((%s)(((%s)(%s) >> %d) | ((%s)(%s) << %d)))" % (ut, ut, d, k, ut, d, w - k))
                        self.emit(ins.address, "%s = %s;" % (d, e))
                        self.cmp = (d, "0", "e")
                    return
            self.skipped["rol/ror-var"] += 1
            self.emit(ins.address, "/* %s: variable count not spelled */" % m)
            return
        if m == "imul":
            if len(ops) == 3:
                self.setreg(self.img.md.reg_name(ops[0].reg), "%s * %s" % (self.rv(ops[1]), self.rv(ops[2])))
            elif len(ops) == 2:
                self.setreg(self.img.md.reg_name(ops[0].reg), "%s * %s" % (self.rv(ops[0]), self.rv(ops[1])))
            else:
                self.setreg("eax", "%s * %s" % (self.reg.get("eax", "eax"), self.rv(ops[0])))
            return
        op = ARITH_OP[m]
        if m in ("sar", "shr"):
            # the arithmetic expressions are `unsigned int`; `sar` is the *signed* shift, `shr` the
            # unsigned one. Spelling both as `>>` made every signed shift an unsigned one.
            if len(ops) == 1:
                if m == "sar":
                    self.setreg("eax", "((int)(%s) >> %s)" % (self.regval("eax"), self.rv(ops[0])))
                else:
                    self.setreg("eax", "((unsigned int)(%s) >> %s)" % (self.regval("eax"), self.rv(ops[0])))
                return
            if ops[0].type == capstone.x86.X86_OP_REG:
                n = self.img.md.reg_name(ops[0].reg)
                cur = self.regval(n)
                cast = "int" if m == "sar" else "unsigned int"
                self.setreg(n, "((%s)(%s) >> %s)" % (cast, cur, self.rv(ops[1])))
                self.cmp = (self.regval(n), "0", "e")
            else:
                d = self.lval(ops[0])
                cast = "int" if m == "sar" else "unsigned int"
                self.emit(ins.address, "%s = ((%s)(%s) >> %s);" % (d, cast, d, self.rv(ops[1])))
                self.cmp = (d, "0", "e")
            return
        if len(ops) == 2:
            if ops[0].type == capstone.x86.X86_OP_REG:
                n = self.img.md.reg_name(ops[0].reg)
                cur = self.regval(n)
                rhs = self.rv(ops[1])
                e = "(%s %s %s)" % (cur, op, rhs)
                self.setreg(n, e)
                if m == "sub":
                    # `sub a, b` is the compare `a vs b`: CF, ZF and SF are the ones `cmp a, b` sets,
                    # which is how retail spells an unsigned compare (`sub eax, ecx; jb`)
                    self.cmp = (self.regval(n), rhs, "e")
                    self.cf = "((unsigned)(%s) < (unsigned)(%s))" % (cur, rhs)
                elif m == "add":
                    self.cmp = (e, "0", "e")
                    self.cf = "((unsigned)(%s) < (unsigned)(%s))" % (e, cur)
                elif m in ("and", "or", "xor"):
                    self.cmp = (e, "0", "e")
                    self.cf = "0"
                else:
                    self.cmp = (e, "0", "e")
            else:
                d = self.lval(ops[0])
                rhs = self.rv(ops[1])
                # an integer op on a float-typed slot is a bit test on the storage: go through the
                # integer view of the same bytes, or `l | 2` with a float l is C2296 (00d4f2e0)
                if self.slot_ty.get(self.disp_of(ops[0])) in ("float", "double") and \
                        self.memkind(ops[0])[0] == "slot":
                    d = "(*(unsigned int*)&(%s))" % d
                self.emit(ins.address, "%s = (%s %s %s);" % (d, d, op, rhs))
                if m == "sub":
                    self.cmp = (d, rhs, "e")
                    self.cf = "((unsigned)(%s) < (unsigned)(%s))" % (d, rhs)
                else:
                    self.cmp = (d, "0", "e")
        elif len(ops) == 1:
            self.setreg("eax", "(%s %s %s)" % (self.regval("eax"), op, self.rv(ops[0])))

    def do_cmp(self, ins):
        ops = ins.operands
        if ins.mnemonic in ("comiss", "ucomiss", "comisd", "ucomisd"):
            # a float compare, not an integer one: `comiss a, b` then `jbe` is `!(a > b)` on the
            # values themselves (unordered included), never on their bit patterns
            a = self.rv(ops[0])
            b = self.rv(ops[1]) if len(ops) > 1 else a
            self.cmp = (a, b, "f")
            return
        a = self.rv(ops[0])
        b = self.rv(ops[1]) if len(ops) > 1 else a
        self.zero_reg = None
        if ins.mnemonic == "test" and len(ops) == 2 and ops[0].type == ops[1].type == capstone.x86.X86_OP_REG \
                and ops[0].reg == ops[1].reg and ops[0].size == 4:
            self.zero_reg = self.img.md.reg_name(ops[0].reg)    # `test r, r`: r == 0 where je lands
        if ins.mnemonic == "test":
            # `test` is an AND that sets only ZF/SF/PF: it is not `cmp a, b`
            self.cmp = (a, b, "t")
            self.cf = "0"
        else:
            self.cmp = (a, b, "e")
            self.cf = "((unsigned)(%s) < (unsigned)(%s))" % (a, b)

    def cond(self, cc):
        if cc in ("p", "np", "o", "no"):
            return "0"
        if self.cmp is None:
            return "1"
        lhs, rhs, kind = self.cmp
        if kind == "f":
            # comiss/ucomiss set CF/ZF the same way for ordered and unordered operands, so the
            # negated forms are what `jb`/`jbe` test: `!(a >= b)` and `!(a > b)`
            rel = {"a": ">", "ae": ">=", "b": None, "be": None, "e": "==", "ne": "!=",
                   "z": "==", "nz": "!="}
            if cc in ("b", "be"):
                base = ">=" if cc == "b" else ">"
                return "!((%s) %s (%s))" % (lhs, base, rhs)
            op = rel.get(cc)
            if op is None:
                return "0"
            return "((%s) %s (%s))" % (lhs, op, rhs)
        if kind == "t":
            e = "(%s & %s)" % (lhs, rhs)
            if cc in ("e", "z"):
                return "(%s) == 0" % e
            if cc in ("ne", "nz"):
                return "(%s) != 0" % e
            if cc == "s":
                return "((int)%s) < 0" % e
            if cc == "ns":
                return "((int)%s) >= 0" % e
            return "0"
        if cc in ("s", "ns"):
            return "((int)(%s) %s 0)" % (lhs, "<" if cc == "s" else ">=")
        op = CC.get(cc, "==")
        if cc in UNSIGNED:
            # the arithmetic expressions are `unsigned int`; a `jb` is the unsigned relation
            return "((unsigned)(%s) %s (unsigned)(%s))" % (lhs, op, rhs)
        # `jl` on two `unsigned int` expressions would compare them unsigned: the cast is what
        # makes it the signed relation retail's jcc tests
        return "((int)(%s) %s (int)(%s))" % (lhs, op, rhs)

    def do_jcc(self, ins):
        tgt = int(ins.op_str, 16)
        if not (self.va <= tgt < self.va + self.size):
            return
        self.emit(ins.address, "if (%s) goto %s;" % (self.cond(ins.mnemonic[1:]), self.lab(tgt)))
        self.branch_state(tgt)
        zr = getattr(self, "zero_reg", None)
        if zr and ins.mnemonic in ("je", "jz"):
            # the taken path knows the tested register is 0: two null checks into one `ret`
            # (008773b0) then agree on eax == 0 instead of disagreeing on two expressions
            self.pending[tgt][-1][0][zr] = "0"

    def branch_state(self, tgt):
        """Remember the registers a branch carries to its target (see restore_state)."""
        self.pending.setdefault(tgt, []).append((dict(self.reg), list(self.fpu), list(self.fpu_ty)))

    def restore_state(self, ins, k):
        """At a jump target no instruction falls into (the one before is a `ret` or `jmp`), the
        registers are what the branches carried, not what the linear scan left from the path above:
        `cmp eax,edx; jne L; ...; lea eax,[ecx+0x90]; ret; L: add eax,-0x30; ret` returned the
        fall-through path's eax plus -48 (0092ea70). A register on which the branches disagree is
        dropped (no merge temps yet), as before."""
        states = self.pending.get(ins.address)
        if not states:
            return
        prev = next((self.ins[j] for j in range(k - 1, -1, -1) if self.ins[j].address not in self._dropped), None)
        if prev is None or prev.mnemonic not in ("ret", "jmp"):
            return
        regs = {}
        for r in set().union(*(st[0] for st in states)):
            vals = {st[0].get(r) for st in states}
            if len(vals) == 1 and None not in vals:
                regs[r] = vals.pop()
        self.reg = regs
        # the x87 stack likewise: what the branches carried, if they agree; else empty (a value
        # loaded on the path above is not on the stack here: 00805200's `fld [eax+0x14]` path)
        fp = {tuple(st[1]) for st in states}
        if len(fp) == 1:
            self.fpu, self.fpu_ty = list(states[0][1]), list(states[0][2])
        else:
            self.fpu, self.fpu_ty = [], []

    def do_tail(self, ins):
        """A `jmp` out of the function is a tail call: an adjustor thunk (`add ecx,-0x20c; jmp`)
        forwards to the real body, and a plain jmp forwards the arguments it was given. The incoming
        ecx/edx are passed first because the target reads them from the registers retail left them
        in; the remaining arguments are the pushes still outstanding."""
        ops = ins.operands
        args = list(self.stack)
        self.stack = []
        pre = []
        if self.thisreg == "ecx":
            pre.append(self.reg.get("ecx", "self"))
        if self.fastreg == "edx":
            pre.append(self.reg.get("edx", "a_edx"))
        args = pre + args
        self.tail = True
        if ops[0].type == capstone.x86.X86_OP_IMM:
            tgt = ops[0].imm & 0xFFFFFFFF
            self.callees.add(tgt)
            counts, _ = self.calleesig.get(tgt, (set(), False))
            self.calleesig[tgt] = (counts, True)
            call = "%s(%s)" % (fun(tgt), ", ".join(args))
        else:
            call = "((int (__cdecl *)(...))%s)(%s)" % (self.rv(ops[0]), ", ".join(args))
        self.emit(ins.address, "return %s;" % call)

    def do_call(self, ins, k=None):
        ops = ins.operands
        args = self.stack
        self.stack = []
        live = self.eax_live(k) if k is not None else False
        fret = self.float_ret(k)
        if fret:
            live = False
        ty = "float" if fret else "int" if live else "void"
        if ops and ops[0].type == capstone.x86.X86_OP_IMM and ops[0].imm != self.va and \
                self.is_thiscall(k, len(args)):
            # `mov ecx, X; push args; call f` with no caller cleanup: f is a __thiscall member and X its
            # `this`. A free (...) declaration dropped X (the code computing it was dead) and made the
            # caller pop the arguments the callee pops (2,834 of 3,712 residual functions have one).
            target = ops[0].imm
            ts = self.thiscallees.setdefault(target, {"arities": set(), "ret": False})
            ts["arities"].add(len(args))
            ts["ret"] = ts["ret"] or live
            if fret:
                ts["float"] = True
            rhscall = "((C_%08x*)(%s))->%s(%s)" % (target, self.reg["ecx"], fun(target), ", ".join(args))
        elif ops and ops[0].type == capstone.x86.X86_OP_IMM:
            target = ops[0].imm
            self.callees.add(target)
            self.calleesig.setdefault(target, (set(), False))
            self.calleesig[target] = (self.calleesig[target][0] | {len(args)}, self.calleesig[target][1] or live)
            if fret:
                self.float_callees.add(target)
            rhscall = "%s(%s)" % (fun(target), ", ".join(args)) if args else "%s()" % fun(target)
        else:
            ptr = "((%s (__cdecl *)(...))%s)" % (ty, self.rv(ops[0]))
            rhscall = "%s(%s)" % (ptr, ", ".join(args))
        for r in ("eax", "ecx", "edx"):
            self.reg.pop(r, None)
        if fret:
            # the result is ST0: it goes on the x87 stack, where the next fadd/fstp/... reads it
            t = self.newtemp("float")
            self.emit(ins.address, "%s = %s;" % (t, rhscall))
            self.fpu.append(t)
            self.fpu_ty.append("float")
            return
        if live:
            t = self.newtemp()
            self.emit(ins.address, "%s = %s;" % (t, rhscall))
            self.reg["eax"] = t
        else:
            self.emit(ins.address, "%s;" % rhscall)

    X87_READS_ST0 = ("fadd", "fsub", "fsubr", "fmul", "fdiv", "fdivr", "fstp", "fst", "fcom", "fcomp",
                     "fucom", "fucomp", "fucomi", "fucomip", "fcomi", "fcomip", "fchs", "fabs", "fistp",
                     "fist", "fxch", "faddp", "fmulp", "fsubp", "fsubrp", "fdivp", "fdivrp", "ftst", "fsqrt")

    def float_ret(self, k):
        """Does the call at index k return a float in ST0? Yes when the next x87 instruction after it
        reads ST0 without loading first (`call edx; fadd [c]`, 00d1dd70), before any branch or call."""
        if k is None:
            return False
        for ins in self.ins[k + 1:]:
            m = ins.mnemonic
            if m.startswith("f"):
                return m in self.X87_READS_ST0
            if m in ("call", "ret", "jmp") or m.startswith("j"):
                return False
        return False

    def is_thiscall(self, k, nargs):
        """ecx written since the previous call (do_call forgets ecx at every call), and the caller does
        not pop the pushed arguments after it (a __thiscall callee pops its own)."""
        if k is None or "ecx" not in self.reg:
            return False
        nxt = self.ins[k + 1] if k + 1 < len(self.ins) else None
        if nargs and nxt is not None and (
                (nxt.mnemonic == "add" and nxt.op_str.startswith("esp, ")) or
                (nxt.mnemonic == "pop" and nxt.op_str in ("ecx", "edx") and nargs == 1)):
            return False
        return True

    def eax_live(self, k):
        """Does the code after the call at index k read eax before writing it?"""
        # `call f; pop ecx / add esp, N; ret` as the function's only ret, with no x87 value: the source
        # discarded the result (a void function). The int spelling `t = f(g); return t;` compiles to
        # `add esp, 4` where the void one gives retail's `pop ecx` (spore 012aee00, 012ee220, 01369e40,
        # 013aa660; f3 00d83a30: all void in their real sources, lbench band1c vs e1c5).
        nxt = self.ins[k + 1:]
        j = 0
        while j < len(nxt) and (nxt[j].mnemonic == "pop" and nxt[j].op_str in ("ecx", "edx") or
                                nxt[j].mnemonic == "add" and nxt[j].op_str.startswith("esp, ")):
            j += 1
        if j < len(nxt) and nxt[j].mnemonic == "ret" and \
                sum(1 for i in self.ins if i.mnemonic == "ret") == 1 and \
                not any(i.mnemonic.startswith("fld") or i.mnemonic == "fild" for i in self.ins):
            return False
        for ins in self.ins[k + 1:]:
            ops = ins.operands
            if ops and ops[0].type == capstone.x86.X86_OP_REG and ins.mnemonic in (
                    "mov", "movzx", "movsx", "lea", "pop", "xor", "add", "sub", "and", "or", "imul",
                    "movss", "movsd", "shl", "shr", "sar", "neg", "not", "inc", "dec") and \
                    self.img.md.reg_name(ops[0].reg) in ("eax", "ax", "al"):
                return False
            for op in ops:
                if op.type == capstone.x86.X86_OP_REG and self.img.md.reg_name(op.reg) in ("eax", "ax", "al"):
                    return True
                if op.type == capstone.x86.X86_OP_MEM:
                    for r in (op.mem.base, op.mem.index):
                        if r and self.img.md.reg_name(r) in ("eax", "ax", "al"):
                            return True
            if ins.mnemonic in ("jmp", "call") or ins.mnemonic.startswith("j"):
                return False
            if ins.mnemonic == "ret":
                return True
        return False

    # ---- SSE
    def addr_of(self, op):
        """The address an operand refers to, as an expression (the lvalue without the dereference)."""
        kind = self.memkind(op)
        if kind[0] == "slot":
            return "(&%s)" % self.slot(kind[1])
        if kind[0] == "global":
            va = kind[1] & 0xFFFFFFFF
            if 0x00401000 <= va < 0x02000000:
                self.globals.add(va)
                return "((unsigned int)%s)" % g(va)
            return str(va)
        base, index, scale, disp = kind[1:]
        e = base
        if index:
            e = "%s + %s * %d" % (base, index, scale) if scale > 1 else "%s + %s" % (base, index)
        if disp:
            e = "%s + %d" % (e, disp) if disp > 0 else "%s - %d" % (e, -disp)
        return "(%s)" % e

    def xmm_name(self, op):
        n = self.img.md.reg_name(op.reg)
        self.xmm.add(n)
        return n

    def xf(self, op, ty="float"):
        """A scalar SSE operand: lane 0 of an xmm register, or a float read from memory."""
        if op.type == capstone.x86.X86_OP_REG:
            return "%s.f[0]" % self.xmm_name(op)
        if op.type == capstone.x86.X86_OP_MEM:
            return "*(%s*)(%s)" % (ty, self.addr_of(op))
        return self.rv(op)

    def do_sse(self, ins):
        m, ops = ins.mnemonic, ins.operands
        ty = "double" if m.endswith("sd") else "float"
        if m in ("movaps", "movups", "movdqa", "movdqu"):
            d, s = ops
            if d.type == capstone.x86.X86_OP_REG and s.type == capstone.x86.X86_OP_REG:
                self.emit(ins.address, "%s = %s;" % (self.xmm_name(d), self.xmm_name(s)))
            elif d.type == capstone.x86.X86_OP_REG:
                self.emit(ins.address, "%s = *(XmmWord*)(%s);" % (self.xmm_name(d), self.addr_of(s)))
            else:
                self.emit(ins.address, "*(XmmWord*)(%s) = %s;" % (self.addr_of(d), self.xmm_name(s)))
            return
        if m in ("xorps", "xorpd", "andps", "andnpd", "orps", "andps", "andpd", "orpd", "andnps"):
            d, s = ops
            dn = self.xmm_name(d)
            if s.type == capstone.x86.X86_OP_REG and s.reg == d.reg:
                # a register XORed with itself is zero: the chained store retail emits
                self.emit(ins.address, "%s.f[0] = %s.f[1] = %s.f[2] = %s.f[3] = 0.0f;"
                          % (dn, dn, dn, dn))
                return
            op = {"xorps": "^", "xorpd": "^", "andps": "&", "andpd": "&", "andnps": "&~",
                  "orps": "|", "orpd": "|"}.get(m, "^")
            src = ("*(unsigned int*)(%s)" % self.addr_of(s)) if s.type == capstone.x86.X86_OP_MEM \
                else "%s.u[0]" % self.xmm_name(s)
            for lane in range(4):
                lhs = "%s.u[%d]" % (dn, lane)
                if s.type == capstone.x86.X86_OP_MEM:
                    rhs = "((unsigned int*)(%s))[%d]" % (self.addr_of(s), lane)
                else:
                    rhs = "%s.u[%d]" % (self.xmm_name(s), lane)
                if op == "&~":
                    self.emit(ins.address, "%s = (%s) & ~(%s);" % (lhs, lhs, rhs))
                else:
                    self.emit(ins.address, "%s = %s %s %s;" % (lhs, lhs, op, rhs))
            return
        if m in ("addss", "subss", "mulss", "divss", "addsd", "subsd", "mulsd", "divsd",
                 "minss", "maxss", "minsd", "maxsd", "sqrtss", "sqrtsd"):
            d, s = ops
            a = self.xf(d, ty)
            b = self.xf(s, ty)
            if m.startswith("sqrt"):
                self.needs_math = True
                expr = "((%s)sqrt((double)(%s)))" % (ty, b)
            elif m.startswith("min"):
                expr = "((%s) < (%s) ? (%s) : (%s))" % (a, b, a, b)
            elif m.startswith("max"):
                expr = "((%s) > (%s) ? (%s) : (%s))" % (a, b, a, b)
            else:
                expr = "(%s %s %s)" % (a, {"add": "+", "sub": "-", "mul": "*", "div": "/"}[m[:3]], b)
            self.emit(ins.address, "%s = %s;" % (a, expr))
            return
        if m in ("addps", "subps", "mulps", "divps", "addpd", "subpd", "mulpd", "divpd"):
            d, s = ops
            dn = self.xmm_name(d)
            op = {"add": "+", "sub": "-", "mul": "*", "div": "/"}[m[:3]]
            for lane in range(4):
                if s.type == capstone.x86.X86_OP_MEM:
                    rhs = "((float*)(%s))[%d]" % (self.addr_of(s), lane)
                else:
                    rhs = "%s.f[%d]" % (self.xmm_name(s), lane)
                lhs = "%s.f[%d]" % (dn, lane)
                self.emit(ins.address, "%s = (%s %s %s);" % (lhs, lhs, op, rhs))
            return
        if m in ("comiss", "ucomiss", "comisd", "ucomisd"):
            a = self.xf(ops[0], ty)
            b = self.xf(ops[1], ty)
            self.cmp = (a, b, "f")
            return
        if m == "movd" or m == "movq":
            d, s = ops
            if d.type == capstone.x86.X86_OP_REG and self.img.md.reg_name(d.reg).startswith("xmm"):
                src = self.xf(s) if s.type == capstone.x86.X86_OP_MEM else self.rv(s)
                self.emit(ins.address, "%s.u[0] = (unsigned int)(%s);" % (self.xmm_name(d), src))
            else:
                self.setreg(self.img.md.reg_name(d.reg), "%s.u[0]" % self.xmm_name(s))
            return
        if m in ("cvtsi2ss", "cvtsi2sd"):
            d, s = ops
            self.emit(ins.address, "%s.f[0] = (%s)(%s);" % (self.xmm_name(d), ty, self.rv(s)))
            return
        if m in ("cvttss2si", "cvttsd2si", "cvtss2si", "cvtsd2si"):
            d, s = ops
            src = self.xf(s, "double" if m.endswith("sd2si") else "float")
            self.setreg(self.img.md.reg_name(d.reg), "((int)(%s))" % src)
            return
        if m in ("cvtss2sd", "cvtsd2ss"):
            d, s = ops
            self.emit(ins.address, "%s.f[0] = (%s)(%s);"
                      % (self.xmm_name(d), "float" if m == "cvtsd2ss" else "double",
                         self.xf(s, "float" if m == "cvtss2sd" else "double")))
            return
        if m == "shufps":
            d, s, imm = ops
            dn, sn = self.xmm_name(d), self.xmm_name(s)
            k = imm.imm
            lanes = [(dn, "f", k & 3), (dn, "f", (k >> 2) & 3), (sn, "f", (k >> 4) & 3),
                     (sn, "f", (k >> 6) & 3)]
            tmp = [self.newtemp("float") for _ in range(4)]
            for t, (base, lane_kind, i) in zip(tmp, lanes):
                self.emit(ins.address, "%s = %s.%s[%d];" % (t, base, lane_kind, i))
            for i, t in enumerate(tmp):
                self.emit(ins.address, "%s.f[%d] = %s;" % (dn, i, t))
            return
        self.skipped[m] += 1
        self.emit(ins.address, "/* %s %s */" % (m, ins.op_str))

    def do_fpu(self, ins):
        """The x87 stack as a list of expressions: fld pushes, the arithmetic rewrites the top, fstp
        stores and pops. `fabs dword ptr [mem]` (the 2-operand form) loads, applies and stores in one."""
        m, ops = ins.mnemonic, ins.operands
        def fty(op):
            return "double" if getattr(op, "size", 4) == 8 else "float"
        def ismem(op):
            return op.type == capstone.x86.X86_OP_MEM
        if m in ("fld1", "fldz"):
            # the x87 constants: `fld1; pop edi; ret` is a function's `return 1.0f;` (00805200)
            self.fpu.append("1.0f" if m == "fld1" else "0.0f")
            self.fpu_ty.append("float")
            return
        if m in ("fld", "fild"):
            if not ops:
                return
            if ismem(ops[0]):
                ty = "double" if m == "fild" else fty(ops[0])
                e = self.lval(ops[0], ty)
                self.fpu.append("((double)(%s))" % e if m == "fild" else e)
                self.fpu_ty.append(ty)
            else:
                self.fpu.append(self.rv(ops[0]))
                self.fpu_ty.append("double")
            return
        if m in ("fstp", "fst", "fistp", "fist"):
            if not ops:
                if self.fpu:
                    self.fpu.pop()
                    if self.fpu_ty:
                        self.fpu_ty.pop()
                return
            top = self.fpu.pop() if self.fpu else "0"
            top_ty = self.fpu_ty.pop() if self.fpu_ty else "float"
            if ismem(ops[0]):
                if m.startswith("fist"):
                    self.emit(ins.address, "%s = (int)(%s);" % (self.lval(ops[0], "int"), top))
                else:
                    self.emit(ins.address, "%s = (%s)(%s);" % (self.lval(ops[0], fty(ops[0])), fty(ops[0]), top))
            else:
                self.setreg(self.img.md.reg_name(ops[0].reg), top)
            if m == "fst" and top:
                self.fpu.append(top)
                self.fpu_ty.append(top_ty)
            return
        if m in ("fabs", "fchs") and len(ops) == 1 and ismem(ops[0]):
            ty = fty(ops[0])
            d = self.lval(ops[0], ty)
            if m == "fabs":
                self.needs_math = True
                self.emit(ins.address, "%s = (%s)fabs((double)(%s));" % (d, ty, d))
            else:
                self.emit(ins.address, "%s = (%s)(-(%s));" % (d, ty, d))
            return
        if m in ("fabs", "fchs") and not ops:
            if self.fpu:
                self.needs_math = self.needs_math or m == "fabs"
                self.fpu[-1] = "fabs((double)(%s))" % self.fpu[-1] if m == "fabs" else "(-(%s))" % self.fpu[-1]
            return
        if m in ("fadd", "fsub", "fsubr", "fmul", "fdiv", "fdivr"):
            sym = {"fadd": "+", "fsub": "-", "fsubr": "-", "fmul": "*", "fdiv": "/", "fdivr": "/"}[m]
            if not ops:
                return
            src = self.lval(ops[0], fty(ops[0])) if ismem(ops[0]) else self.rv(ops[0])
            if self.fpu:
                a, b = (src, self.fpu[-1]) if m in ("fsubr", "fdivr") else (self.fpu[-1], src)
                self.fpu[-1] = "((%s) %s (%s))" % (a, sym, b)
                if self.fpu_ty and fty(ops[0]) == "double":
                    self.fpu_ty[-1] = "double"
            else:
                self.fpu.append(src)
                self.fpu_ty.append(fty(ops[0]))
            return
        if m == "fxch":
            if len(self.fpu) >= 2:
                self.fpu[-1], self.fpu[-2] = self.fpu[-2], self.fpu[-1]
                if len(self.fpu_ty) >= 2:
                    self.fpu_ty[-1], self.fpu_ty[-2] = self.fpu_ty[-2], self.fpu_ty[-1]
            return
        self.skipped[m] += 1
        self.emit(ins.address, "/* %s %s */" % (m, ins.op_str))

    def do_div(self, ins):
        d = self.rv(ins.operands[0])
        # is the divisor a spelled-out constant 0 (possibly cast- and paren-wrapped)?
        core = d
        for _ in range(8):
            c = core.strip()
            m = re.match(r"^\(\s*(?:unsigned\s+|signed\s+)?(?:char|short|int|long|__int64)\s*\)(.*)$", c)
            if m:
                core = m.group(1).strip()
                continue
            if c.startswith("(") and c.endswith(")") and c.count("(") == c.count(")") and \
                    not re.search(r"[+\-*/%&|^<>,!=]", c[1:-1]):
                core = c[1:-1].strip()
                continue
            break
        if core in ("0", "0u", "0U"):
            # A constant-zero divisor does not compile (`x / 0` is C2124); retail computes the
            # division at runtime (the path is unreachable — 0fcf600). A temp the compiler cannot
            # fold keeps the statement compilable and the runtime behaviour identical.
            t = self.newtemp()
            self.emit(self.cur_va, "%s = 0;" % t)
            d = t
        num = self.reg.get("eax", "eax")
        if ins.mnemonic == "idiv":
            self.setreg("eax", "((int)%s / (int)%s)" % (num, d))
            self.setreg("edx", "((int)%s %% (int)%s)" % (num, d))
        else:
            self.setreg("eax", "(%s / %s)" % (num, d))
            self.setreg("edx", "(%s %% %s)" % (num, d))

    def ret_stmt(self):
        e = self.ret_expr()
        if self.fpu and self.ret_temp is None:
            # a value left on the x87 stack at `ret` IS the return (ST0): eax is whatever the path
            # last wrote, such as a call result tested above (00805200 returned the call's pointer)
            e = None
        if self.ret_temp is not None and e is not None:
            # this ret shares the epilogue with a mid-body return temp: merge through the temp so
            # both paths return the same C++ object
            self.emit(self.ret_addr(), "return %s;" % self.ret_temp)
            self.any_value_ret = True
            return
        if self.ret_temp is not None and self.fpu:
            # the temp's value is on the x87 stack: retail loads the ST0 return from the epilogue's
            # reserved slot, which is what a float return type makes the second path emit
            self._ret_ty = self.ret_tyx or (self.fpu_ty[-1] if self.fpu_ty else "float")
            if self.ret_tyx:
                self.emit(self.ret_addr(), "return %s;" % self.ret_temp)
            else:
                self.emit(self.ret_addr(), "return (%s)(%s);" % (self._ret_ty, self.ret_temp))
            self.any_value_ret = True
            return
        if e is None and self.fpu:
            # a float return rides the x87 stack: retail stores the xmm result into the slot its
            # prologue reserved and loads it (`movss [esp],xmm2; fld [esp]; pop ecx; ret`). A void
            # declaration would let c2 delete the whole body, since the value is the only effect.
            self._ret_ty = self.fpu_ty[-1] if self.fpu_ty else "float"
            self.emit(self.ret_addr(), "return %s;" % self.fpu[-1])
            self.any_value_ret = True
            return
        if e is None and not self.tail:
            # Voidness is a property of the function, not of one ret: a function whose branch paths
            # disagree (one path returned a value, another none — 011e08d0) declared void and made
            # every `return expr;` a C2562. Recorded here, resolved in signature() once all rets ran.
            self.ret_void = True
            self.emit(self.ret_addr(), "return;")
            return
        # A bare ret on a path beside a tail call (0075dc10) has no eax value: returning 0 keeps the
        # int signature honest, and printing `None` was a leak.
        self.emit(self.ret_addr(), "return %s;" % (e if e is not None else "0"))
        self.any_value_ret = True

    def ret_addr(self):
        """Where a `return` goes: at its own `ret`. Filed under the function's end address, every return
        rendered after all the code and labels, so a mid-body `ret` (`test eax,eax; jne L; ret; L: ...`)
        returned on both paths and c2 deleted the code behind L (0086aeb0, 00c3f560)."""
        return getattr(self, "cur_addr", self.va + self.size)

    def ret_expr(self):
        """The expression the function returns: eax, with an address cast to the return type."""
        e = self.reg.get("eax")
        if e is not None and e.startswith("(&"):
            e = "((unsigned int)%s)" % e
        # a bool return reads the condition itself; the byte cast rv() put on the `mov al, dl`
        # source is what an int-returning source compiles to instead (`mov eax, edx`)
        if e is not None and self.ret_type() == "bool" and \
                e.startswith("((unsigned char)(") and e.endswith("))"):
            e = e[len("((unsigned char)("):-2]
        return e

    def ret_type(self):
        """The return type the source must have had, read off the write into eax before the ret.

        A byte-width write there (`mov al, dl` after a `sete dl`) is c2's bool materialisation:
        only a bool-returning source reproduces `33 d2 ... 0f 94 c2 8a c2`, an int one gives
        `8b c2`. With several rets the write that reaches each one is not recoverable by a linear
        scan, so those stay int — conservative, and the population this serves is small leaves."""
        if self._ret_ty is not None:
            return self._ret_ty
        self._ret_ty = "int"
        rets = [i for i, ins in enumerate(self.ins) if ins.mnemonic == "ret"]
        if len(rets) != 1:
            return self._ret_ty
        last_write, w = {}, None
        for ins in self.ins[:rets[0]]:
            m, ops = ins.mnemonic, ins.operands
            if not ops or ops[0].type != capstone.x86.X86_OP_REG:
                continue
            n = self.img.md.reg_name(ops[0].reg)
            src = self.img.md.reg_name(ops[1].reg) \
                if len(ops) > 1 and ops[1].type == capstone.x86.X86_OP_REG else None
            if n in ("al", "ah", "ax", "eax"):
                w = (n, m, src)
            last_write[n] = m
        if w is None:
            return self._ret_ty
        n, m, src = w
        if n in ("al", "ah"):
            prov = last_write.get(src) if src else None
            if m == "mov" and prov is not None and prov.startswith("set") and prov[3:] in CC:
                self._ret_ty = "bool"
            elif m.startswith("set") and m[3:] in CC:
                self._ret_ty = "bool"
            else:
                self._ret_ty = "unsigned char"
        elif n == "ax":
            self._ret_ty = "unsigned short"
        return self._ret_ty

    def is_epilogue(self, addr):
        """Is `addr` inside the restore-and-return tail of one of the function's `ret`s?

        Retail shares one epilogue between all the returns it merged, so a `jmp` to it is a
        return, not a loop back-edge. Only instructions that touch nothing but the frame and the
        return count, and only in a run that ends at a `ret`.
        """
        if not hasattr(self, "epi"):
            epi, run = set(), []
            for ins in self.ins:
                m, o = ins.mnemonic, ins.op_str
                if m == "ret":
                    epi.update(run)
                    epi.add(ins.address)
                    run = []
                elif m in ("leave", "nop", "int3") or \
                        (m == "mov" and o in ("esp, ebp", "ebp, esp")) or \
                        (m in ("add", "sub") and o.startswith("esp, ")):
                    run.append(ins.address)
                else:
                    run = []
            self.epi = epi
        return addr in self.epi

    def do_xchg(self, ins):
        """`xchg` is a store and a load in one instruction: both halves matter (the loaded value
        stays live in the register), so it cannot be dropped as a plain store."""
        a, b = ins.operands
        REG = capstone.x86.X86_OP_REG
        MEM = capstone.x86.X86_OP_MEM
        if a.type == REG and b.type == REG:
            na, nb = self.img.md.reg_name(a.reg), self.img.md.reg_name(b.reg)
            va_, vb_ = self.regval(na), self.regval(nb)
            self.setreg(na, vb_)
            self.setreg(nb, va_)
            return
        if a.type == MEM or b.type == MEM:
            mem, reg = (a, b) if a.type == MEM else (b, a)
            rn = self.img.md.reg_name(reg.reg)
            d = self.lval(mem)
            cur = self.regval(rn)
            t = self.newtemp()
            self.emit(ins.address, "%s = %s;" % (t, d))
            self.emit(ins.address, "%s = %s;" % (d, cur))
            self.setreg(rn, t)
            return
        self.skipped["xchg"] += 1

    def do_adc_sbb(self, ins, plus):
        """`adc`/`sbb` are the carry-consuming forms; the carry comes from the last compare or the
        last subtraction (`sbb eax, eax` is `-(x < y)`, which is how the compare idioms are spelled)."""
        n = self.img.md.reg_name(ins.operands[0].reg)
        cur = self.regval(n)
        rhs = self.rv(ins.operands[1])
        cf = self.cf or "0"
        op = "+" if plus else "-"
        e = "(%s %s %s %s ((unsigned int)(%s)))" % (cur, op, rhs, op, cf)
        self.setreg(n, e)
        self.cf = "((unsigned)(%s) %s (unsigned)(%s))" % (cur, "<" if plus else "<", rhs)
        self.cmp = (e, "0", "e")

    # ---- render
    def render(self):
        self.run()
        # Self-recursion whose call site pushes a different word count than the definition takes
        # (009b05b0 calls itself with 2 of its 8 params) is C2660 as a direct call. Spell it through
        # a __cdecl pointer cast, which keeps the call site's pushes on the stack like retail.
        nparams = len(self.args) + (1 if self.thisreg == "ecx" else 0) + (1 if self.fastreg == "edx" else 0)
        if nparams:
            out2 = []
            for addr, t in self.out:
                m = re.search(r"FUN_%08x\((.*)\)" % self.va, t)
                if m:
                    args = m.group(1).split(", ") if m.group(1) else []
                    # rewrite when the direct call cannot compile: a different arg count than the
                    # definition (009b05b0 calls itself with 2 of its 8 params), or the first arg
                    # is not the `self` pointer the definition opens with (0069e6c0 passes an int
                    # temp, 00ea30b0 a slot).
                    bad_first = self.thisreg == "ecx" and (
                        not args or re.fullmatch(r"[A-Za-z_]\w*", args[0]) is not None or
                        args[0].startswith("((unsigned int)"))
                    if len(args) != nparams or bad_first:
                        ty = "int" if (re.match(r"^\s*\w+ = ", t) or t.startswith("return")) else "void"
                        t = t.replace("FUN_%08x(" % self.va,
                                      "((%s (__cdecl *)(...))FUN_%08x)(" % (ty, self.va), 1)
                out2.append((addr, t))
            self.out = out2
        # An int function may not contain a bare `return;`: MSVC drops code after one as unreachable,
        # which makes even the value-returns behind it vanish (C2561, 011e08d0). The void decision
        # needs every ret, so settle it here — after the body ran, before the text is built.
        if self.ret_void and self.any_value_ret and not self.tail:
            self.out = [(a, "return 0;" if t == "return;" else t) for a, t in self.out]
        body = "\n".join(t for _, t in self.out)
        refs = set(re.findall(r"L_[0-9a-f]{8}", body))
        lines, emitted = [], set()
        # group statements by address, then walk the instruction list to place labels in order
        by_addr = collections.defaultdict(list)
        for addr, t in self.out:
            by_addr[addr].append(t)
        addr_set = {i.address for i in self.ins}
        # a branch can land in the int3 padding after a call or past the last instruction: the label
        # belongs before the next instruction, which is where the padding would run into
        starts = sorted(addr_set)
        before = collections.defaultdict(list)
        tail_labels = []
        for lab in sorted(refs):
            a = int(lab[2:], 16)
            if a in addr_set:
                continue
            i = bisect.bisect_right(starts, a)
            if i < len(starts):
                before[starts[i]].append(lab)
            else:
                tail_labels.append(lab)
        for ins in self.ins:
            a = ins.address
            for lab in before.get(a, ()):
                lines.append("%s:;" % lab)
            if ("L_%08x" % a) in refs and a not in emitted:
                lines.append("L_%08x:;" % a)
                emitted.add(a)
            for t in by_addr.get(a, ()):
                lines.append(t)
        for lab in tail_labels:
            lines.append("%s:;" % lab)
        for addr, ts in by_addr.items():
            if addr not in addr_set:
                for t in ts:
                    lines.append(t)
        return "\n".join("    " + l if not l.startswith(("L_", "}", "{", "    ")) else l for l in lines)

    def signature(self):
        # A parameter list is contiguous: if the body reads [ebp+0x10] it is the fourth argument,
        # and the three before it must be declared too or the fourth would land in the first slot.
        # An ebp frame starts the arguments at +8 (the saved ebp and the return address are above),
        # an esp frame at +4.
        if self.args:
            base = 8 if self.frame_ptr else 4
            if max(self.args) < base + 4096:
                for d in range(base, max(self.args) + 1, 4):
                    self.args.setdefault(d, "a_%d" % d)
        params = ["%s %s" % (self.slot_ty.get(d, "int"), self.args[d]) for d in sorted(self.args)]
        # A `ret N` callee pops N bytes of stack arguments whether the body reads them or not: a
        # source taking one word and ignoring it still compiles to `mov eax, 5; ret 4`. The register
        # parameters a __fastcall stand-in adds sit in ecx/edx and are not part of that count, so the
        # stack side is padded before they go in front (00964770: ecx read, one stacked argument,
        # `ret 0xc` — two more declared arguments the body never reads).
        while len(params) < self.ret_pop // 4:
            params.append("int a_unused%d" % len(params))
        # __thiscall is not available to a free function, so the incoming ecx/edx are spelled as a
        # __fastcall's register parameters stand in for the incoming ecx/edx: ecx is `self`, edx is
        # `a_edx`. The stack parameters keep retail's offsets either way, because __fastcall puts the
        # first stacked argument where __thiscall does ([ebp+8] with an ebp frame).
        if self.thisreg == "ecx":
            # The register ABI retail actually uses decides the spelling. With `ret N` the callee
            # pops the stack arguments itself — that is __thiscall (this in ecx, args on the stack,
            # callee-cleans), whose stand-in must put NO register parameter before the stack ones.
            # A plain __fastcall stand-in would move the first stack argument into edx.
            # (0044aa10: ecx read, a_8 read at [ebp+8], `ret 4` — retail passes a_8 on the stack.)
            # `a_edx_dummy` occupies edx's register-parameter slot so the first stack argument
            # stays on the stack; when edx is genuinely read the slot holds the real a_edx. Pad
            # the stack side to ret_pop/4 so the callee-cleanup count matches.
            conv = "__fastcall"
            edx_slot = "int a_edx" if self.fastreg == "edx" else "int a_edx_dummy"
            params[:0] = ["char* self", edx_slot]
        elif self.fastreg == "edx":
            conv = "__fastcall"
            params[:0] = ["int a_ecx", "int a_edx"]
        else:
            conv = "__stdcall" if self.ret_pop else "__cdecl"
        if self.ret_void and not self.any_value_ret and not self.tail:
            ret = "void"
        else:
            ret = self.ret_type()
        return 'extern "C" %s %s %s(%s)' % (ret, conv, fun(self.va), ", ".join(params))


def callee_decl(sig):
    """A callee's forward declaration. Route 1 has no source for a callee's signature, so the
    parameters are left open (`(...)`), which accepts however many words each call site pushes and
    keeps the argument expressions exactly as retail computes them. Return type: int when any call
    site reads eax afterwards, void otherwise."""
    _counts, uses_ret = sig if sig else (set(), False)
    return ("int" if uses_ret else "void"), "(...)"


def emit_one(img, va, size):
    fn = Fn(img, va, size)
    body = fn.render()
    head = []
    for va_g in sorted(fn.globals):
        head.append("extern \"C\" unsigned char %s[];" % g(va_g))
    for va_c in sorted(fn.callees):
        if va_c == fn.va:
            continue    # self-recursion: the definition below is the declaration
        ret, plist = callee_decl(fn.calleesig.get(va_c))
        if va_c in fn.float_callees:
            ret = "float"
        head.append('extern "C" %s __cdecl %s%s;' % (ret, fun(va_c), plist))
    for va_c in sorted(fn.thiscallees):
        ts = fn.thiscallees[va_c]
        ret = "float" if ts.get("float") else "int" if ts["ret"] else "void"
        members = " ".join("%s %s(%s);" % (ret, fun(va_c), ", ".join(["unsigned int"] * n))
                           for n in sorted(ts["arities"]))
        head.append("struct C_%08x { %s };" % (va_c, members))
    locals_ = ["    %s %s;" % (fn.slot_ty.get(d, "unsigned int"), fn.slots[d]) for d in sorted(fn.slots)]
    locals_ += ["    %s %s;" % (ty, n) for n, ty in fn.temps]
    locals_ += ["    XmmWord %s;" % x for x in sorted(fn.xmm)]
    # An int function must not fall off the end without returning (C4716): happens when the only
    # ret was folded into a dropped epilogue (00842a90). Append one; void functions may fall off.
    if not (fn.ret_void and not fn.any_value_ret and not fn.tail) and \
            not any(t.startswith("return") for _, t in fn.out):
        body += "\n    return 0;"
    parts = []
    if fn.xmm or fn.needs_math:
        parts.append("#include <math.h>")
        parts.append("")
    if fn.needs_intrin:
        parts.append("#include <intrin.h>")
        parts.append("")
    if fn.xmm:
        parts.append("union XmmWord { float f[4]; unsigned int u[4]; };")
        parts.append("")
    if head:
        parts.append("\n".join(head))
        parts.append("")
    parts.append(fn.signature())
    parts.append("{")
    parts.extend(locals_)
    if locals_:
        parts.append("")
    parts.append(body if body else "    ")
    parts.append("}")
    return "\n".join(parts), fn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vas", nargs="*")
    ap.add_argument("--list")
    ap.add_argument("--out", default=os.path.join(ROOT, "work/scratch/w8_emit"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--residual", help="JSON list of VAs to emit from")
    ap.add_argument("--max-calls", type=int, help="keep only functions with at most N call instructions")
    ap.add_argument("--min-calls", type=int, default=0)
    a = ap.parse_args()
    img = Image()
    fps = {}
    for line in open(os.path.join(SPORE, "tools/ci/fingerprints.jsonl")):
        j = json.loads(line)
        fps[j["va"]] = j["size"]
    vas = list(a.vas)
    if a.list:
        vas += json.load(open(a.list))
    if a.residual:
        vas += json.load(open(a.residual))
    if a.max_calls is not None:
        kept = []
        for va_s in vas:
            va = int(va_s, 16)
            size = fps.get(va_s)
            if size is None:
                continue
            n = sum(1 for i in img.insns(va, size) if i.mnemonic == "call")
            if a.min_calls <= n <= a.max_calls:
                kept.append(va_s)
        vas = sorted(kept, key=lambda v: (fps[v], v))
    if a.limit:
        vas = vas[:a.limit]
    # --out is a directory; accept a .cpp path too (single-VA re-emit convenience)
    out = a.out
    if out.endswith(".cpp"):
        out = os.path.dirname(out) or "."
    os.makedirs(out, exist_ok=True)
    tot_skip = collections.Counter()
    for va_s in vas:
        va = int(va_s, 16) if isinstance(va_s, str) else va_s
        size = fps.get("%08x" % va)
        if size is None:
            print("%08x: no fingerprint" % va)
            continue
        src, fn = emit_one(img, va, size)
        dst = os.path.join(a.out, "%08x.cpp" % va)
        if os.path.isdir(dst):
            import shutil
            shutil.rmtree(dst, ignore_errors=True)
        open(dst, "w").write(src + "\n")
        tot_skip.update(fn.skipped)
        if os.environ.get("W8_RSS"):
            import resource
            print("RSS %4d MB after %08x" % (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 2 ** 20, va))
        print("%08x %4dB  %3d stmts %2d slots %2d calls %2d globals  skip=%s" % (
            va, size, len(fn.out), len(fn.slots), len(fn.callees), len(fn.globals),
            dict(fn.skipped) if fn.skipped else "-"))
    if tot_skip:
        print("skipped constructs:", dict(tot_skip))


if __name__ == "__main__":
    main()
