"""lift2il: retail x86 machine code -> c2's IL at Lower's entry, in c2il's dump format (milestone 1).

usage: lift2il.py il CORPUS.jsonl VA            print the lifted IL of one function
       lift2il.py check CORPUS.jsonl [--sample N] [--max-size B] [--jobs J] [--json OUT]
             lifted IL vs the real source's traced IL at Lower's entry (il2c.trace_il), compared as lift.lift_il
             tokens: per function the agreement (matched tokens / the longer stream), and the unmatched runs

The pipeline (docs/c2/lift2il.md):
  1. capstone, then the CFG: leaders at the entry, every branch target, and after every jcc / jmp / ret;
  2. the stack: esp tracked from the entry (push / pop / sub / add esp / call cleanup), so `[esp+d]` and `[ebp+d]`
     name one frame offset: >= 4 an incoming argument `_aN`, < 0 a local `_lN`;
  3. register webs: reaching definitions over the CFG, a use unions every definition that reaches it; one web is
     one IL variable (`_vN`; ecx read from the entry is `_this`). Calls define eax, ecx and edx;
  4. one tuple per instruction pattern: OPASSIGN (mov, movzx, movsx), add/sub/and/or/xor/shl/shr/sar, OPMUL,
     neg/not, inc/dec as add/sub 1, lea as an add, OPCMP + OPBRANCH (cmp, test, or the arithmetic that set the
     flags), OPGOTO, OPARG + OPCALL (the pushes since the previous call, ecx when it was written for the call),
     OPRET (eax's web). The prologue and epilogue (callee-saved pushes and pops, frame setup) leave no tuples.
Globals and callees are named by address (`?g_<va>@@3HA`, `?FUN_<va>@@YAXXZ`): token class F, as real IL's named
globals are. Types: 1004 for registers; memory by operand size (1 -> 2001, 2 -> 2002, 4 -> 1004, 8 -> 1008).
"""
import argparse, collections, json, os, re, sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import capstone
from capstone import x86 as X

GPR = ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi")
XMM = tuple("xmm%d" % n for n in range(8))
SSE_OPS = {"addss": ("add", "e004"), "subss": ("sub", "e004"), "mulss": ("OPMUL", "e004"), "divss": ("OPDIV", "e004"),
           "addsd": ("add", "e008"), "subsd": ("sub", "e008"), "mulsd": ("OPMUL", "e008"), "divsd": ("OPDIV", "e008")}
SSE_MOVES = {"movss": "e004", "movsd": "e008", "movaps": "e004", "movapd": "e008", "movups": "e004"}
SUB = {"al": "eax", "ah": "eax", "ax": "eax", "cl": "ecx", "ch": "ecx", "cx": "ecx", "dl": "edx", "dh": "edx",
       "dx": "edx", "bl": "ebx", "bh": "ebx", "bx": "ebx", "si": "esi", "di": "edi", "bp": "ebp", "sp": "esp"}
CALLEE_SAVED = ("ebx", "esi", "edi", "ebp")
INVCC = {"e": "ne", "ne": "e", "l": "ge", "ge": "l", "le": "g", "g": "le", "b": "ae", "ae": "b", "be": "a", "a": "be"}
CCOF = {"je": "e", "jz": "e", "jne": "ne", "jnz": "ne", "jl": "l", "jle": "le", "jg": "g", "jge": "ge",
        "jb": "b", "jbe": "be", "ja": "a", "jae": "ae", "js": "l", "jns": "ge"}
ARITH = {"add": "add", "sub": "sub", "and": "and", "or": "or", "xor": "xor", "shl": "shl", "sal": "shl",
         "shr": "shr", "sar": "sar"}
MTY = {1: "2001", 2: "2002", 4: "1004", 8: "1008"}
# x87 at Lower's entry is plain IL with float types: 5004/5008 on 14.00 and 15.00, 4004/4008 on 13.10 (canon_il)
F4, F8 = ("4004", "4008") if os.environ.get("C2_BUILD", "").startswith("13") else ("5004", "5008")
FOPS = {"fadd": "add", "fsub": "sub", "fmul": "OPMUL", "fdiv": "OPDIV", "fsubr": "subr", "fdivr": "divr"}
# fcom* + fnstsw ax + test ah, MASK + jcc: (mask, jcc) -> the IL condition of `st0 cmp operand`
# (C0 = st0 < x, C3 = st0 == x; C2 = unordered)
# a branch taken on unordered (NaN sets C0, C2 and C3) is the negation of an ordered compare, "!": cl writes
# `if (x >= c)` as `test ah, 1; jne else` (checked by compiling every form, cl 13.10 and 15.00)
FCC = {(0x41, "jne"): "!g", (0x41, "je"): "g", (0x01, "jne"): "!ge", (0x01, "je"): "ge",
       (0x40, "jne"): "e", (0x40, "je"): "ne", (0x05, "jnp"): "l", (0x05, "jp"): "!l",
       (0x44, "jp"): "ne", (0x44, "jnp"): "e",
       # `test ah, 0x41; jp` (C0 and C3 alike: greater, or unordered) is !(st0 <= x): cl's `if (a >= b)` with the
       # operands loaded the other way; `jnp` is st0 <= x, false on NaN. "!" marks the NaN-inclusive negation
       (0x41, "jp"): "!le", (0x41, "jnp"): "le",
       # fcomi / fcomip / fucomip set ZF, PF, CF as an unsigned compare of st0 with x; unordered sets all three
       ("fcomi", "ja"): "g", ("fcomi", "jae"): "ge", ("fcomi", "jb"): "!ge", ("fcomi", "jbe"): "!g",
       ("fcomi", "jnb"): "ge", ("fcomi", "jnbe"): "g", ("fcomi", "jna"): "!g", ("fcomi", "jnae"): "!ge"}
FIOPS = {"fiadd": "fadd", "fisub": "fsub", "fimul": "fmul", "fidiv": "fdiv", "fisubr": "fsubr", "fidivr": "fdivr"}
FUNARY = {"fsin": "sin", "fcos": "cos"}


def full(name):
    return SUB.get(name, name)


class Unsupported(Exception):
    pass


class Lifter:
    def __init__(self, img, va, size, mask=None, partial=False):
        self.img, self.va, self.size = img, va, size
        self.partial = partial              # an unsupported instruction becomes an OPUNSUP marker, not an error
        self.ins = img.insns(va, size)
        if not self.ins:
            raise Unsupported("no code")
        self.md = img.md
        self.chkstk = None                 # (index of the __chkstk call, frame size): run() fills it
        self.indexed = {}                  # frame offset of a register-indexed local array -> its scale
        self.switches = {}                 # jmp address -> dict(idx=reg, cases=[(value, target)], default, skip)
        self.find_switches()
        self.eh = None                     # the C++ EH frame: node offset, state slot, unwind map (find_eh)
        self.eh_ins = set()                # instruction indices of the EH registration / unregistration
        self.eh_slot_vals = {}             # frame offset a funclet reads -> the token the code stored there
        self.consts = {}                   # name -> its constant value (one constant definition), else None
        self.skip = set()                  # instructions folded into an earlier one (a division by a constant)
        self.tls_base = set()
        self.esp_alias = {}                # instruction index of `mov r, esp` -> esp's delta there
        self.alias_written = set()         # outgoing offsets stored through such a copy (a by-value struct)
        self.frame_addr = {}               # variable -> the frame offset it holds the address of (a lea)
        self.aggregates = set()            # (frame offset, bytes): one object (a block copy's source / destination)
        self.bvctor = {}                   # outgoing offset -> (copy constructor va, bytes): a by-value class
        self.load_src = {}                 # temp -> (base token, disp) of the load that defined it
        self.bv_block = {}                 # outgoing offset -> bytes of a struct copied there by rep movsd
        self.byval = []                    # the last call's by-value structs: (first argument index, address, bytes)
        # catch funclets inside the function's bytes are not its code; a resume target only a handler reaches
        # starts the catch block's own code (`catch (...) { return false; }`)
        self.resumes = set()
        self.try_esp = None                # esp at the first state store (a catch resumes with that frame)
        self.eh_unsupported = None
        try:
            self.find_eh()
        except Unsupported as ex:
            if not partial:
                raise
            self.eh, self.eh_ins, self.eh_unsupported = None, set(), str(ex)   # lifted without its EH frame
        if self.eh:
            for k, i in enumerate(self.ins):
                if any(lo <= i.address < hi for lo, hi in getattr(self, "handler_spans", [])):
                    self.skip.add(k)
            for t in self.eh["tries"]:
                for c in t["catches"]:
                    if c["handler"]["kind"] == "resume":
                        self.resumes.add(c["handler"]["to"])
        self.mask = mask                   # byte offsets inside the function that retail relocates (an address)
        self.max_ret_pop = max([i.operands[0].imm for i in self.ins if i.mnemonic == "ret" and i.operands] + [0])
        self.this_at_entry = False         # set after webs(): `_this` read somewhere
        self.lines = []
        self.ntemp = 0
        self.out_args, self.out_top = {}, None   # outgoing argument area: offset -> (token, bytes); its top
        self.args_read = set()             # incoming argument offsets read (4 = the first)
        self.arg_types = {}                # argument offset -> the type of its first use (a float read: 5004)
        self.fpu = []                      # the x87 stack: operand tokens, top last
        self.fcmp = None                   # (st0, operand) of the last fcom*
        self.fmask = None                  # the `test ah, MASK` after fnstsw
        self.float_ret = False             # some ret returns st0
        self.byte_rets = self.value_rets = 0     # rets whose eax came from byte writes (al): a bool function
        self.ret_pop = 0                   # `ret N`
        self.value_ret = False             # some OPRET returns eax
        self.i64_ret = False               # some OPRET returns edx:eax as one __int64 (find_i64)

    # ---- registers read / written (full 32-bit names)
    def disp_reloc(self, i):
        """Is i's 4-byte memory displacement a relocated address?"""
        if getattr(i, "disp_size", 0) != 4 or not getattr(i, "disp_offset", 0):
            return False
        off = i.address + i.disp_offset - self.va
        return off in self.mask if self.mask is not None else bool(self.img.reloc_at(i.address + i.disp_offset))

    def disp_relocs(self):
        """The relocated displacements of indexed / based memory operands in the function (cached)."""
        if getattr(self, "_disp_relocs", None) is None:
            self._disp_relocs = sorted({o.mem.disp & 0xFFFFFFFF for i in self.ins for o in i.operands
                                        if o.type == X.X86_OP_MEM and (o.mem.base or o.mem.index) and self.disp_reloc(i)})
        return self._disp_relocs

    def adc_follows(self, k):
        """Does an adc / sbb read the flags this instruction sets (before another flag write, in its block)?"""
        for j in range(k + 1, min(k + 12, len(self.ins))):
            m = self.ins[j].mnemonic
            if m in ("adc", "sbb"):
                return True
            if m.startswith(("j", "call", "ret")) or m in ARITH or m in ("cmp", "test", "inc", "dec", "neg", "imul"):
                return False
        return False

    def find_chkstk(self):
        """A frame over a page: `mov eax, N; call __chkstk` (the stack probe moves esp down by N, keeps the other
        registers) at entry, with `add esp, N` before a ret (M&B Operation::execute 0x49fa10: 0x1ec0). -> (index of
        the call, N) or None."""
        if len(self.ins) < 3:
            return None
        a, b = self.ins[0], self.ins[1]
        if not (a.mnemonic == "mov" and a.op_str.startswith("eax, ") and a.operands[1].type == X.X86_OP_IMM and
                b.mnemonic == "call" and b.operands[0].type == X.X86_OP_IMM):
            return None
        n = a.operands[1].imm
        if n < 0x1000 or not any(i.mnemonic == "add" and i.op_str == "esp, %s" % hex(n) for i in self.ins):
            return None
        return (1, n)

    def regs(self, i):
        if getattr(self, "chkstk", None) and i.address == self.ins[self.chkstk[0]].address:
            return set(), set()                        # the stack probe: esp only
        r, w = i.regs_access()
        rn = {full(self.md.reg_name(x)) for x in r}
        wn = {full(self.md.reg_name(x)) for x in w}
        m = i.mnemonic
        if m == "xor" and len(i.operands) == 2 and i.operands[0].type == X.X86_OP_REG and \
                i.operands[0].reg == i.operands[1].reg:
            rn.discard(full(self.md.reg_name(i.operands[0].reg)))        # xor r, r reads nothing
        if m == "cdq":
            wn.discard("eax")                          # capstone lists eax as written; cdq only sets edx
        if m in ("test", "cmp"):                       # capstone drops the register of `test [m], r` (M&B 00403ab0)
            rn |= {full(self.md.reg_name(op.reg)) for op in i.operands if op.type == X.X86_OP_REG}
        if m == "ret":
            rn.add("eax")                              # the return value: returned when defined in the function
        if m == "call" or self.is_tailjmp(i):
            if m == "call":
                wn |= {"eax", "ecx", "edx"}
            rn.add("ecx")                              # maybe `this`: decided from the definitions (thiscall())
        # a partial write (mov al, x) keeps the rest of the register: a read as well
        for op in i.operands:
            if op.type == X.X86_OP_REG and op.access & capstone.CS_AC_WRITE and op.size < 4:
                rn.add(full(self.md.reg_name(op.reg)))
        keep = (set(GPR) - {"esp"}) | set(XMM)
        return rn & keep, wn & keep

    def dword(self, va):
        o = self.img.off(va)
        return int.from_bytes(self.img.data[o:o + 4], "little") if o is not None else None

    def find_switches(self):
        """`cmp r, N; ja D; [movzx r, byte [r + B];] jmp dword ptr [r*4 + T]`: a switch on r with N+1 cases. The
        tables sit after the code inside the function's bytes, so the instruction list stops at the first one."""
        cut = None
        for k, i in enumerate(self.ins):
            if i.mnemonic != "jmp" or not i.operands or i.operands[0].type != X.X86_OP_MEM:
                continue
            m = i.operands[0].mem
            if m.base or not m.index or m.scale != 4:
                continue
            T = m.disp & 0xFFFFFFFF
            j = k - 1
            btab = None
            if j >= 0 and self.ins[j].mnemonic == "movzx" and self.ins[j].operands[1].type == X.X86_OP_MEM:
                bm = self.ins[j].operands[1].mem
                btab = bm.disp & 0xFFFFFFFF
                idxreg = self.md.reg_name(bm.base) if bm.base else None
                j -= 1
            else:
                idxreg = self.md.reg_name(m.index)
            if j >= 1 and self.ins[j].mnemonic == "mov" and self.ins[j].operands[0].type == X.X86_OP_REG and \
                    self.ins[j].operands[1].type == X.X86_OP_MEM and self.ins[j - 1].mnemonic == "ja":
                j -= 1                                 # /Od: the index reloaded from its slot after the ja
            off = 0                                    # `cmp r, N; ja D; dec r; jmp [r*4+T]`: cases from 1 (an
            dj = self.ins[j] if j >= 1 else None       # __int64 switch, M&B Operation::execute 0x4ac2af)
            if dj is not None and btab is None and full(self.md.reg_name(dj.operands[0].reg)) == full(idxreg) \
                    if dj.operands and dj.operands[0].type == X.X86_OP_REG else False:
                if dj.mnemonic == "dec":
                    off, j = 1, j - 1
                elif dj.mnemonic == "sub" and dj.operands[1].type == X.X86_OP_IMM:
                    off, j = dj.operands[1].imm, j - 1
            if j < 1 or self.ins[j].mnemonic != "ja":
                continue
            c = j - 1                                  # the cmp, maybe with a flag-neutral mov / lea scheduled
            while c > 0 and j - c < 4 and self.ins[c].mnemonic in ("mov", "lea", "push", "pop", "movzx"):
                c -= 1                                 # between it and the ja (M&B 00403770)
            if self.ins[c].mnemonic != "cmp":
                continue
            cop = self.ins[c].operands[1]
            if cop.type == X.X86_OP_IMM:
                n = cop.imm + 1 - off
            elif cop.type == X.X86_OP_REG and not off:             # `mov ebx, 4; ..; cmp eax, ebx`: the bound in a register
                rn = self.md.reg_name(cop.reg)
                mv = next((self.ins[q] for q in range(c - 1, max(-1, c - 60), -1)   # the last write to it (a call
                           if self.ins[q].operands and self.ins[q].operands[0].type == X.X86_OP_REG and   # keeps
                           self.md.reg_name(self.ins[q].operands[0].reg) == rn), None)   # ebx / esi / edi)
                if rn not in ("ebx", "esi", "edi", "ebp") and mv is not None and any(
                        self.ins[q].mnemonic == "call" for q in range(self.ins.index(mv), c)):
                    mv = None                          # a scratch register does not survive a call
                if mv is None or mv.mnemonic != "mov" or mv.operands[1].type != X.X86_OP_IMM:
                    continue
                n = mv.operands[1].imm + 1
            else:
                continue
            default = self.ins[j].operands[0].imm
            if btab is not None:
                o = self.img.off(btab)
                bvals = list(self.img.data[o:o + n])
                tgts = [self.dword(T + 4 * b) for b in bvals]
                lo = min(T, btab)
            else:
                tgts = [self.dword(T + 4 * v) for v in range(n)]
                lo = T
            if any(t is None or not self.va <= t < self.va + self.size for t in tgts):
                continue
            if n <= 0:
                continue
            self.switches[i.address] = dict(idx=full(idxreg), cases=list(enumerate(tgts)), default=default, off=off,
                                            cmp=self.ins[c].address, ja=self.ins[j].address,
                                            movzx=self.ins[k - 1].address if btab is not None else None)
            if self.va <= lo < self.va + self.size:
                cut = lo if cut is None else min(cut, lo)
        if cut is not None:
            self.ins = [i for i in self.ins if i.address < cut]

    # ---- C++ EH frames (__CxxFrameHandler3, destructor states only)
    def find_eh(self):
        """`push -1; push H; mov r, fs:[0]; push r; ...; [mov r, [cookie]; xor r, esp; push r]; lea r, [esp+K];
        mov fs:[0], r` registers a frame; `mov r, [node]; mov fs:[0], r; pop ecx` unregisters it, and stores of a
        constant into node+8 are the state changes. H is `[cookie checks;] mov eax, FuncInfo; jmp handler`: its
        unwind map gives, per state, the state it unwinds to and the funclet that destroys the object
        (`mov ecx, G` / `mov ecx, [ebp+d]` (+ `add ecx, K`) / `lea ecx, [ebp+d]`; `jmp dtor`, or `push [ebp+d];
        call delete`). The funclet's ebp is node+12."""
        ins = self.ins
        kfs = next((k for k, i in enumerate(ins[:14]) if i.mnemonic == "mov" and "fs:[0]" in i.op_str and
                    i.operands[0].type == X.X86_OP_REG), None)
        if kfs is None:
            return
        # `push -1; push H` before or after the load of fs:[0] (15.00 schedules the load first)
        near = [k for k in range(max(0, kfs - 3), min(len(ins), kfs + 3)) if ins[k].mnemonic == "push" and
                ins[k].operands and ins[k].operands[0].type == X.X86_OP_IMM]
        km1 = next((k for k in near if (ins[k].operands[0].imm & 0xFFFFFFFF) == 0xFFFFFFFF), None)
        kh = next((k for k in near if km1 is not None and k == km1 + 1), None)
        if km1 is None or kh is None:
            raise Unsupported("EH: registration shape")
        H = ins[kh].operands[0].imm & 0xFFFFFFFF
        r = ins[kfs].op_str.split(",")[0]
        eh = {km1, kh, kfs}
        kpush = next((k for k in range(max(kfs, kh) + 1, min(max(kfs, kh) + 4, len(ins))) if ins[k].mnemonic == "push"
                      and ins[k].op_str == r), None)
        if kpush is None:
            raise Unsupported("EH: node push")
        eh.add(kpush)
        esp = 0
        for i in ins[:kpush + 1]:
            esp, _e = self.stack_effect(i, esp, None)
        node = esp
        kreg = None
        for k in range(kpush + 1, min(kpush + 16, len(ins))):
            i = ins[k]
            if i.mnemonic == "mov" and i.op_str.startswith("dword ptr fs:[0], "):
                kreg = k
                break
        if kreg is None:
            raise Unsupported("EH: fs:[0] store")
        if ins[kreg].op_str.endswith(", esp"):         # no cookie: `push eax; mov fs:[0], esp`
            eh.add(kreg)
        elif ins[kreg - 1].mnemonic == "lea":
            eh |= {kreg - 1, kreg}
        else:
            raise Unsupported("EH: fs:[0] store")
        for k in range(kpush + 1, kreg - 1):         # the cookie xor'd with esp, pushed or stored below the node
            i = ins[k]
            if i.mnemonic == "mov" and i.operands[1].type == X.X86_OP_MEM and not i.operands[1].mem.base and \
                    k + 1 < kreg and ins[k + 1].mnemonic == "xor" and ins[k + 1].op_str.endswith(("esp", "ebp")):
                eh |= {k, k + 1}
                nx = ins[k + 2]
                if (nx.mnemonic == "push" or nx.mnemonic == "mov") and nx.op_str.endswith(i.op_str.split(",")[0]):
                    eh.add(k + 2)
        for k, i in enumerate(ins):                   # unregistration: `mov r, [node]; mov fs:[0], r; pop ecx`
            if k > kreg and i.mnemonic == "mov" and i.op_str.startswith("dword ptr fs:[0], "):
                r2 = i.op_str.split(", ")[1]
                j = next((q for q in range(k - 1, max(k - 12, 0), -1) if ins[q].mnemonic == "mov" and
                          ins[q].op_str.startswith(r2 + ", dword ptr [")), None)
                if j is None:
                    raise Unsupported("EH: unregistration shape")
                eh |= {j, k}
                if k + 1 < len(ins) and ins[k + 1].mnemonic == "pop" and ins[k + 1].op_str in ("ecx", "edx", "eax"):
                    eh.add(k + 1)
        for k, i in enumerate(ins):
            if k not in eh and any(o.type == X.X86_OP_MEM and o.mem.segment == X.X86_REG_FS and
                                   (o.mem.disp & 0xFFFFFFFF) != 0x2c for o in i.operands):
                raise Unsupported("fs: segment outside the EH frame (TLS?)")
        hi = self.img.insns(H, 64)
        j = next((q for q in range(len(hi) - 1) if hi[q].mnemonic == "mov" and hi[q].op_str.startswith("eax, 0x")
                  and hi[q + 1].mnemonic == "jmp"), None)
        if j is None:
            raise Unsupported("EH: handler shape")
        fi = hi[j].operands[1].imm & 0xFFFFFFFF
        maxs, um, ntry, tm = self.dword(fi + 4), self.dword(fi + 8), self.dword(fi + 12), self.dword(fi + 16)
        tries = []
        for t in range(ntry):                         # TryBlockMapEntry: low, high, catchHigh, nCatches, pCatches
            b = tm + 20 * t
            cats = []
            for c in range(self.dword(b + 12)):
                e = self.dword(b + 16) + 16 * c       # HandlerType: adjectives, pType, dispCatchObj, handler
                cats.append(dict(adj=self.dword(e), type=self.dword(e + 4), disp=self.dword(e + 8),
                                 handler=self.catch_body(self.dword(e + 12), node + 12)))
            tries.append(dict(low=self.dword(b), high=self.dword(b + 4), catch_high=self.dword(b + 8), catches=cats))
        if tries:                                     # `mov [ebp-0x10], esp`: the esp a catch resumes with
            for k in range(kreg + 1, min(kreg + 10, len(ins))):
                i = ins[k]
                if i.mnemonic == "mov" and i.op_str.endswith(", esp") and i.operands[0].type == X.X86_OP_MEM:
                    eh.add(k)
                    break
            else:
                raise Unsupported("EH: try without a saved esp")
        states = []
        for st in range(maxs):
            to, a = self.dword(um + 8 * st), self.dword(um + 8 * st + 4)
            to = to - (1 << 32) if to & 0x80000000 else to
            states.append(dict(to=to, **self.funclet(a, node + 12)))
        self.eh = dict(node=node, state=node + 8, states=states, ins=eh, tries=tries)
        self.eh_ins = eh

    def catch_body(self, a, fbp):
        """A catch handler funclet, read up to its end: `push 0; push 0; call _CxxThrowException` (`throw;`) or
        `mov eax, L; ret` (resume at L). Its code is not in the function's bytes; what is, is the frame: the
        slots it reads ([ebp+d], entry-relative fbp+d) must be the source variables' memory homes."""
        f = self.img.insns(a, 512)
        reads = []
        self.handler_spans = getattr(self, "handler_spans", [])
        for q, i in enumerate(f):
            if i.mnemonic == "ret" or (i.mnemonic == "call" and q >= 2 and f[q - 1].mnemonic == "push" and
                                       f[q - 1].op_str == f[q - 2].op_str):
                self.handler_spans.append((a, i.address + i.size))
            for o in i.operands:
                if o.type == X.X86_OP_MEM and o.mem.base and self.md.reg_name(o.mem.base) == "ebp" and not o.mem.index:
                    off = fbp + o.mem.disp
                    if off not in reads:
                        reads.append(off)
            if i.mnemonic == "call" and q >= 2 and f[q - 1].mnemonic == f[q - 2].mnemonic == "push" and \
                    f[q - 1].op_str == f[q - 2].op_str and (f[q - 1].op_str == "0" or any(
                        x.mnemonic == "xor" and x.op_str == "%s, %s" % (f[q - 1].op_str, f[q - 1].op_str)
                        for x in f[max(0, q - 4):q - 2])):
                return dict(kind="rethrow", reads=reads)     # _CxxThrowException(0, 0), zeros or a zeroed reg
            if i.mnemonic == "ret":
                if q and f[q - 1].mnemonic == "mov" and f[q - 1].op_str.startswith("eax, 0x"):
                    return dict(kind="resume", reads=reads, to=f[q - 1].operands[1].imm & 0xFFFFFFFF)
                break
            if i.mnemonic == "jmp" and i.operands[0].type == X.X86_OP_IMM and i.operands[0].imm < a:
                pass
        raise Unsupported("EH: catch handler end")

    def funclet(self, a, fbp):
        """An unwind funclet's object and destructor; frame offsets are entry-relative (fbp = its ebp)."""
        if not a:
            return dict(kind="none")
        f = self.img.insns(a, 32)
        def fr(op):
            return fbp + op.mem.disp if op.type == X.X86_OP_MEM and self.md.reg_name(op.mem.base) == "ebp" and \
                not op.mem.index else None
        m0 = f[0]
        if m0.mnemonic == "mov" and m0.op_str.startswith("ecx, 0x") and f[1].mnemonic == "jmp":
            return dict(kind="glob", addr=m0.operands[1].imm & 0xFFFFFFFF, add=0, dtor=f[1].operands[0].imm)
        if m0.mnemonic in ("mov", "lea") and m0.op_str.startswith("ecx, ") and fr(m0.operands[1]) is not None:
            add, n = 0, 1
            if f[1].mnemonic == "add" and f[1].op_str.startswith("ecx, "):
                add, n = f[1].operands[1].imm, 2
            if f[n].mnemonic == "jmp" and f[n].operands[0].type == X.X86_OP_IMM:
                return dict(kind="slot" if m0.mnemonic == "mov" else "local", off=fr(m0.operands[1]), add=add,
                            dtor=f[n].operands[0].imm)
        if m0.mnemonic == "mov" and fr(m0.operands[1]) is not None and len(f) > 4 and f[1].mnemonic == "push" and \
                f[2].mnemonic == "mov" and fr(f[2].operands[1]) is not None and f[3].mnemonic == "push" and \
                f[4].mnemonic == "call":
            # `push [ebp+d1]; push [ebp+d2]; call D; add esp, 8`: a placement delete (p at d2, where at d1)
            return dict(kind="pdelete", off=fr(f[2].operands[1]), off2=fr(m0.operands[1]), dtor=f[4].operands[0].imm)
        # constant pushes, then `mov eax, [ebp+d]; push eax; call D`: an operator delete with extra arguments
        # (Spore's `operator delete(p, name, flags, ..)`), the pointer at d
        q = 0
        while q < len(f) and f[q].mnemonic == "push" and f[q].operands[0].type == X.X86_OP_IMM:
            q += 1
        if q and q + 2 < len(f) and f[q].mnemonic == "mov" and fr(f[q].operands[1]) is not None and \
                f[q + 1].mnemonic == "push" and f[q + 2].mnemonic == "call":
            return dict(kind="delete", off=fr(f[q].operands[1]), dtor=f[q + 2].operands[0].imm, extra=q)
        if m0.mnemonic == "mov" and m0.op_str.startswith("eax, ") and fr(m0.operands[1]) is not None and \
                f[1].mnemonic == "push" and f[1].op_str == "eax" and f[2].mnemonic == "call":
            return dict(kind="delete", off=fr(m0.operands[1]), dtor=f[2].operands[0].imm)
        raise Unsupported("EH: funclet shape %s %s" % (m0.mnemonic, m0.op_str))

    def is_tailjmp(self, i):
        if i.mnemonic != "jmp" or not i.operands:
            return False
        o = i.operands[0]
        if o.type == X.X86_OP_IMM:
            return not self.va <= o.imm < self.va + self.size
        # `jmp eax` / `jmp [edx+0x18]` (a virtual or pointer tail call), not a switch's `jmp [r*4+T]`
        return i.address not in self.switches and not (o.type == X.X86_OP_MEM and o.mem.index)

    def thiscall(self, k, esp=None, top=None):
        """Does the call / tail jmp at k pass `this` in ecx? Yes when every definition of ecx reaching it is an
        explicit write in this function (`mov / lea / add ecx, ..`), or the function's own incoming `this` while
        the function reads `this` elsewhere too (a member forwarding it: Fallout 3 ilcmp family 1, 39 functions).
        A call's clobber or an unread incoming ecx is no argument."""
        defs = self.reach_defs.get((k, "ecx"), set())
        if not defs:
            return False
        for d in defs:
            if d[0] == "entry":
                if not self.this_used:
                    return False
                op = self.ins[k].operands[0] if self.ins[k].operands else None
                if op is not None and op.type == X.X86_OP_MEM and not op.mem.base and not op.mem.index:
                    return False             # `call [import]` with the caller's this left in ecx (InterlockedDecrement)
            elif d[0] == "undef" or self.ins[d[0]].mnemonic == "call":
                return False
            elif self.ecx_is_data(d[0], k, esp, top):
                return False
        return True

    def ecx_is_data(self, d, k, esp=None, top=None):
        """Between ecx's def d and the call k (straight-line code), is ecx's value stored or pushed as data
        (`mov ecx, [s+8]; mov [eax+8], ecx; call`: the third dword of a struct argument, not `this`)?
        With the call's esp and the top of its outgoing area known, a store through esp counts only when it lands in
        that area: `lea ecx, [esi+8]; push x; mov [esp+0xc], ecx; call` spills `this` to a local (Spore 0068f0b0)."""
        if not (isinstance(d, int) and d < k) or k - d > 40:
            return False
        below = {}                                       # q -> esp at q, relative to the call's esp (when known)
        if esp is not None and top is not None:
            at = 0
            for q in range(k - 1, d, -1):
                below[q] = at
                i = self.ins[q]
                if i.mnemonic == "push":
                    at += 4
                elif i.mnemonic == "pop":
                    at -= 4
                elif i.mnemonic in ("add", "sub") and i.operands and i.operands[0].type == X.X86_OP_REG and \
                        self.md.reg_name(i.operands[0].reg) == "esp" and i.operands[1].type == X.X86_OP_IMM:
                    at += i.operands[1].imm if i.mnemonic == "sub" else -i.operands[1].imm
                else:
                    continue
                below[q] = at
        for q in range(d + 1, k):
            i = self.ins[q]
            if i.mnemonic.startswith(("j", "ret", "call")):
                return False
            ops = i.operands
            if i.mnemonic == "mov" and len(ops) == 2 and ops[1].type == X.X86_OP_REG and \
                    self.md.reg_name(ops[1].reg) == "ecx" and ops[0].type == X.X86_OP_MEM and ops[0].mem.base:
                b = self.md.reg_name(ops[0].mem.base)
                if b == "esp":
                    if q in below and esp + below[q] + ops[0].mem.disp >= top:
                        continue                         # above the outgoing area: a local, not an argument
                    return True                          # into the outgoing area
                bd = self.reach_defs.get((q, full(b)), set())
                if len(bd) == 1 and next(iter(bd))[0] in self.esp_alias:
                    return True                          # through `mov eax, esp`: a by-value struct's dword
        return False

    # ---- the CFG
    def blocks(self):
        addrs = [i.address for i in self.ins]
        lead = {addrs[0]}
        for k, i in enumerate(self.ins):
            if i.mnemonic.startswith("j") or i.mnemonic == "ret":
                if k + 1 < len(self.ins):
                    lead.add(addrs[k + 1])
                op = i.operands[0] if i.operands else None
                if op is not None and op.type == X.X86_OP_IMM and self.va <= op.imm < self.va + self.size:
                    lead.add(op.imm)
        for sw in self.switches.values():
            lead.update(t for _v, t in sw["cases"])
            lead.add(sw["default"])
        starts = sorted(a for a in lead if a in set(addrs))
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
            if m == "ret":
                continue
            if last.address in self.switches:
                sw = self.switches[last.address]
                for t in sorted({t for _v, t in sw["cases"]} | {sw["default"]}):
                    if t in bstart:
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
        return bl

    # ---- reaching definitions -> webs (union-find over definition sites)
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

        acc = [self.regs(i) for i in self.ins]
        n = len(self.bl)
        gen = []
        for b in self.bl:
            g = {}
            for k in b:
                for r in acc[k][1]:
                    g[r] = {(k, r)}
            gen.append(g)
        IN = [dict() for _ in range(n)]
        IN[0] = {r: {("entry", r)} for r in GPR + XMM if r != "esp"}
        OUT = [None] * n
        preds = collections.defaultdict(list)
        for a, ss in self.succ.items():
            for s in ss:
                preds[s].append(a)
        changed = True
        while changed:
            changed = False
            for bn in range(n):
                inn = dict(IN[0]) if bn == 0 else {}
                if bn != 0:
                    for p in preds[bn]:
                        for r, ds in (OUT[p] or {}).items():
                            inn.setdefault(r, set()).update(ds)
                out = {r: set(ds) for r, ds in inn.items()}
                out.update({r: set(ds) for r, ds in gen[bn].items()})
                if inn != IN[bn] or out != OUT[bn]:
                    IN[bn], OUT[bn], changed = inn, out, True
        # walk each block: a use joins every definition reaching it
        self.reach = {}                                    # (insn index, reg) -> web root of the value read
        self.reach_defs = {}                               # (insn index, reg) -> every definition reaching it
        for bn, b in enumerate(self.bl):
            cur = {r: set(ds) for r, ds in IN[bn].items()}
            for k in b:
                rd, wr = acc[k]
                for r in rd:
                    ds = cur.get(r) or {("undef", r)}
                    self.reach_defs[(k, r)] = set(ds)
                    ds = list(ds)
                    find(ds[0])
                    # a call's (or tail jmp's) ecx read only asks "is this a __thiscall"; when several defs reach it
                    # (entry this, a scratch value, an earlier call's clobber) it must not join their webs
                    if not (r == "ecx" and len(ds) > 1 and (self.ins[k].mnemonic == "call" or
                                                            self.is_tailjmp(self.ins[k]))):
                        for d in ds[1:]:
                            union(ds[0], d)
                    self.reach[(k, r)] = ds[0]
                for r in wr:
                    cur[r] = {(k, r)}
                    find((k, r))
        self.find = find
        self.acc = acc
        names, self.webname = {}, {}
        for key in list(parent):
            root = find(key)
            if root not in names:
                ent = [x for x in parent if find(x) == root and x[0] == "entry"]
                if ent and ent[0][1] == "ecx":
                    names[root] = "_this"
                elif ent and ent[0][1] in ("edx",):
                    names[root] = "_aedx"
                else:
                    names[root] = "t%d" % (1000 + len(names))
        self.names = names

    def var_read(self, k, r):
        root = self.find(self.reach.get((k, r), ("undef", r)))
        return self.names.get(root, "_u_" + r)

    def var_written(self, k, r):
        return self.names.get(self.find((k, r)), "_w_%s" % r)

    # ---- stack and operands
    def frame_name(self, off, ty):
        if off >= 4:
            self.args_read.add(off)
            # the first use's type, except that a float read wins over an integer copy of the same size (pushed
            # on to a callee first, `fadd [esp+x]` after: the argument is a float, M&B 0048e5d0)
            if self.arg_types.get(off) == "1004" and ty == F4:
                self.arg_types[off] = ty
            self.arg_types.setdefault(off, ty)
            return "_a%d:%s" % (off, ty)
        if ty in ("1008", "2008"):
            self.aggregates.add((off, 8))                # an __int64 slot: its two dwords are one object
        return "_l%d:%s" % (-off, ty)

    def opnd(self, k, op, esp, ebp):
        """An operand token of instruction k (esp: the delta from the entry before k; ebp: its delta when set)."""
        i = self.ins[k]
        if op.type == X.X86_OP_REG:
            n = self.md.reg_name(op.reg)
            if n.startswith("xmm"):
                return "%s:%s" % (self.var_read(k, n), self.sse_ty(i))
            return "%s:%s" % (self.var_read(k, full(n)), "1004" if op.size == 4 else MTY.get(op.size, "1004"))
        if op.type == X.X86_OP_IMM:
            v = op.imm & 0xFFFFFFFF
            off = i.address + i.size - 4 - self.va
            if op.size == 4 and getattr(i, "imm_size", 4) == 4 and \
                    (off in self.mask if self.mask is not None else self.img.reloc_at(i.address + i.size - 4)):
                return "?ga_%08x@@3HA:1004" % v                        # an address constant: &global
            v = v - (1 << 32) if v & 0x80000000 else v
            return "%d:1004" % v
        m = op.mem
        ty = MTY.get(op.size, "1004")
        if i.mnemonic.startswith("f") and not i.mnemonic.startswith(("fi", "fnst", "fst" + "sw")):
            ty = F8 if op.size == 8 else F4
        elif self.is_sse(i) and i.mnemonic not in ("cvtsi2ss", "cvtsi2sd"):
            ty = "e008" if op.size == 8 else "e004"
        base = self.md.reg_name(m.base) if m.base else None
        idx = self.md.reg_name(m.index) if m.index else None
        if m.segment == X.X86_REG_FS:
            raise Unsupported("fs: segment (EH frame / TLS)")
        if base and not idx and self.tls_base:
            ds = self.reach_defs.get((k, full(base)), set())
            if ds and all(d[0] in self.tls_base for d in ds):
                return "?gt_%x@@3HA:%s" % (m.disp & 0xFFFFFFFF, ty)    # a __declspec(thread) variable
        if base and base not in ("esp", "ebp") and not idx:
            ds = self.reach_defs.get((k, full(base)), set())
            if len(ds) == 1:
                d = next(iter(ds))[0]
                if isinstance(d, int) and d in self.esp_alias:
                    self.alias_written.add(self.esp_alias[d] + m.disp)
                    return self.frame_name(self.esp_alias[d] + m.disp, ty)   # `mov eax, esp; mov [eax+4], x`
        if base == "esp" and not idx:
            return self.frame_name(esp + m.disp, ty)
        if base == "ebp" and not idx and ebp is not None:
            return self.frame_name(ebp + m.disp, ty)
        if not base and not idx:
            return "?g_%08x@@3HA:%s" % (m.disp & 0xFFFFFFFF, ty)          # a named global, read as an operand
        if idx and (base == "esp" or (base == "ebp" and ebp is not None)):
            # a local array indexed by a register (`mov eax, [esp + esi*4 + 0xc]`, M&B 00403ab0): the slot's
            # address, then the index; the slots from there to the frame's top are one object (cl keeps their
            # layout only for an array)
            o = (esp if base == "esp" else ebp) + m.disp
            if o >= 0:
                raise Unsupported("an indexed argument")
            t = self.temp()
            self.emit(k, "OPASSIGN", [t], [self.frame_name(o, "1004")], "s0=&")
            self.indexed.setdefault(o, m.scale)        # sized after the walk (size_indexed)
            return "[%s+%s:1004%s]:%s" % (t, self.var_read(k, full(idx)), "*%d" % m.scale if m.scale > 1 else "", ty)
        terms = []
        if base:
            terms.append("%s:1004" % self.var_read(k, full(base)))
        if idx:
            terms.append("%s:1004%s" % (self.var_read(k, full(idx)), "*%d" % m.scale if m.scale > 1 else ""))
        if m.disp:
            v = m.disp & 0xFFFFFFFF
            off = i.address + getattr(i, "disp_offset", 0) - self.va
            if self.disp_reloc(i):
                b = min((x for x in self.disp_relocs() if v - 12 <= x <= v), default=v)
                terms.append("?ga_%08x@@3HA:1004" % b)              # a global array's address: `[eax*4 + table]`
                if v != b:                                          # the same table's next field (`[edx*8 + T]`,
                    terms.append("%d" % (v - b))                    # `[edx*8 + T+4]`): one base, as in the source
            else:
                terms.append("%d" % m.disp)
        return "[%s]:%s" % ("+".join(terms) or "0", ty)

    def is_sse(self, i):
        m = i.mnemonic
        return m in SSE_OPS or m in SSE_MOVES or m in ("comiss", "ucomiss", "comisd", "ucomisd", "xorps", "xorpd",
                                                       "cvtsi2ss", "cvtsi2sd", "cvttss2si", "cvttsd2si", "cvtss2sd",
                                                       "cvtsd2ss", "cvtss2si", "maxss", "minss")

    def sse_ty(self, i):
        m = i.mnemonic
        return "e008" if m.endswith(("sd", "pd")) or m in ("cvtsi2sd", "cvtss2sd") else "e004"

    def dst(self, k, op, esp, ebp):
        if op.type == X.X86_OP_REG:
            n = self.md.reg_name(op.reg)
            if n.startswith("xmm"):
                return "%s:%s" % (self.var_written(k, n), self.sse_ty(self.ins[k]))
            return "%s:%s" % (self.var_written(k, full(n)), "1004" if op.size == 4 else MTY.get(op.size, "1004"))
        return self.opnd(k, op, esp, ebp)

    def temp(self, ty="1004"):
        self.ntemp += 1
        return "t%d:%s" % (self.ntemp, ty)

    def emit(self, k, op, dsts, srcs, notes=""):
        a = self.ins[k].address
        if op == "OPASSIGN" and "s0=&" in notes and len(dsts) == 1 and srcs:
            mf = re.match(r"^_l(\d+):", srcs[0])            # `lea esi, [esp+8]`: esi holds a frame address
            if mf:
                self.frame_addr[dsts[0].rsplit(":", 1)[0]] = -int(mf.group(1))
        if op == "OPASSIGN" and len(dsts) == 1 and len(srcs) == 1:
            ml = re.match(r"^\[([\w]+):1004(?:\+(-?\d+))?\]:1004$", srcs[0])
            dn = dsts[0].rsplit(":", 1)[0]
            if ml and re.match(r"^t\d+$", dn):
                self.load_src[dn] = (ml.group(1), int(ml.group(2) or 0))
            elif re.match(r"^t\d+$", dn):
                self.load_src.pop(dn, None)
        if srcs and any(t.startswith("[") for t in list(dsts) + list(srcs)):
            # real IL lists the address registers of every memory operand (both sides, once per occurrence) after
            # the sources, and il2c strips exactly those from the end. lift2il lists none, so a last source that
            # is itself one of them (`mov [eax], eax`, `and byte [eax], al`: al is eax's web) would be stripped:
            # then list them all, as real IL does
            inner = []
            for t in list(dsts) + list(srcs):
                if t.startswith("["):
                    inner += re.findall(r"([A-Za-z_?$@][\w?$@]*):[0-9a-f]+", t.split("]")[0])
            if srcs[-1].rsplit(":", 1)[0] in inner:
                srcs = list(srcs) + ["%s:1004" % n for n in inner]
        if self.out_args:                       # a pending argument slot read back: it was a local after all
            for t in list(srcs) + [x for x in dsts if x.startswith("[")]:
                for mm in re.finditer(r"_l(\d+):", t):
                    o = -int(mm.group(1))
                    if o in self.out_args:
                        tok, _n = self.out_args.pop(o)
                        self.lines.append("I %x OPASSIGN 0 0 | %s <- %s" % (a, "_l%d:%s" % (-o, tok.rsplit(":", 1)[1]),
                                                                          tok))
        if op == "OPASSIGN" and dsts and srcs:         # constants by name (a state stored through a register)
            dn = dsts[0].rsplit(":", 1)[0]
            mc = re.match(r"^(-?\d+):", srcs[0])
            prev = self.consts.get(dn, "unset")
            val = int(mc.group(1)) if mc else self.consts.get(srcs[0].rsplit(":", 1)[0]) if \
                re.match(r"^t\d+:", srcs[0]) else None
            self.consts[dn] = val if prev in ("unset", val) else None
        elif dsts:
            for d_ in dsts:
                self.consts[d_.rsplit(":", 1)[0]] = None
        if self.eh and op == "OPASSIGN" and dsts and srcs and re.match(r"^_l%d:" % -self.eh["state"], dsts[0]):
            mc = re.match(r"^(-?\d+):", srcs[0])
            v = int(mc.group(1)) if mc else self.consts.get(srcs[0].rsplit(":", 1)[0])
            if v is None:
                raise Unsupported("EH: a state stored from a non-constant")
            v = {0xff: -1, 0xffff: -1}.get(v, v) if dsts[0].endswith((":2001", ":2002")) else v
            self.lines.append("I %x OPEHSTATE 0 0 | <- %d:1004" % (a, v))
            return
        if self.eh and op == "OPASSIGN" and dsts and srcs:
            m = re.match(r"^_l(\d+):", dsts[0])
            if m and any(st.get("off") == -int(m.group(1)) and st["kind"] in ("slot", "delete", "pdelete")
                         for st in self.eh["states"]):
                self.eh_slot_vals.setdefault(-int(m.group(1)), []).append(srcs[0])
                return
            ma = re.match(r"^_a(\d+):", dsts[0])
            if ma and any(st.get("off2") == int(ma.group(1)) for st in self.eh["states"]):
                return                                # the placement address saved for the funclet
        if op == "OPASSIGN" and self.out_top is not None and dsts and srcs:
            m = re.match(r"^_l(\d+):([0-9a-f]+)$", dsts[0])
            if m and -int(m.group(1)) < self.out_top:   # a store below where argument setup began: an argument
                ty = m.group(2)
                v = srcs[0]
                if ty in (F4, F8) and v.rsplit(":", 1)[-1] in (F4, F8) and v.rsplit(":", 1)[-1] != ty:
                    t = "%s:%s" % (self.temp(ty).rsplit(":", 1)[0], ty)   # `fstp dword [esp]` of an x87 value:
                    self.lines.append("I %x OPASSIGN 0 0 | %s <- %s" % (a, t, v))   # the argument is a float
                    v = t
                self.out_args[-int(m.group(1))] = (v, 8 if ty in ("1008", F8, "e008") else 4)
                return
        self.lines.append("I %x %s 0 0 | %s <- %s%s" % (a, op, " ".join(dsts), " ".join(srcs),
                                                        (" ; " + notes) if notes else ""))

    # ---- the driver
    def run(self):
        self.blocks()
        self.chkstk = self.find_chkstk()
        self.webs()
        pro = self.prologue()
        self.pro = pro
        self.find_tls()
        self.divs = self.find_divs()
        self.strlens = self.find_strlens()
        self.pair_end = set()
        self.pair_pieces = set()                       # instructions folded into an __int64 add / sub
        self.call64 = set()                            # calls whose edx:eax an __int64 add / sub reads
        self.at = {}                                   # insn index -> (esp, ebp) before it
        self.pairs = self.find_pairs()
        self.chains64 = self.find_i64()
        # (a prologue `push ecx` only reserves a slot: no read of this)
        self.this_used = any(r == "ecx" and ("entry", "ecx") in ds and self.ins[k].mnemonic != "call" and
                             not self.is_tailjmp(self.ins[k]) and k not in pro
                             for (k, r), ds in self.reach_defs.items())
        self.fpu_at = {}
        self.fpu_pend = {}                 # branch target -> [[IL position, x87 stack]] (fpu_merge)
        self.fn_value = any(self.used_after(None, k=k, reg="eax") for k, i in enumerate(self.ins) if i.mnemonic == "ret")
        # a value is returned on every path: a ret that eax can reach unwritten (the caller's eax) means the
        # function is void, and the eax of a call before another ret is that call's leftover
        if self.fn_value and any(i.mnemonic == "ret" and any(d[0] in ("entry", "undef") for d in
                                                              self.reach_defs.get((k, "eax"), ()))
                                 for k, i in enumerate(self.ins)):
            self.fn_value = False
        # a ret that eax can reach holding a piece of an __int64 stored to memory (`sbb eax, edx; mov [esi+0xc], eax; ret`):
        # the leftover of a void function's store, and the other paths' eax is no value either (M&B 00432da0)
        if self.fn_value and any(i.mnemonic == "ret" and k not in self.ret64 and self.reach_defs.get((k, "eax")) and
                                 any(d[0] in self.pair_pieces for d in self.reach_defs[(k, "eax")])
                                 for k, i in enumerate(self.ins)):
            self.fn_value = False
        if self.ret64:
            self.fn_value = True                       # edx:eax returned as one __int64 (find_i64)
        esp, ebp = 0, None
        pushes = []
        flags = None                                   # (operand a, operand b) of the last flag-setting instruction
        self.carry = None                              # what CF means now: (a, b, cc), `cmp a, b` -> a <u b
        self.bform = {}                                # name -> ("mask", c) for 0 / -1, ("bool", c) for 0 / 1
        ecx_set = False
        labels = {self.ins[b[0]].address for n, b in enumerate(self.bl) if n and
                  any(n in s for s in self.succ.values())}
        depth = {}                                     # block start -> esp delta on entry (propagated along edges)
        depth[self.ins[0].address] = 0
        for bn, b in enumerate(self.bl):
            a0 = self.ins[b[0]].address
            if a0 in self.resumes and a0 not in depth:
                esp = self.try_esp if self.try_esp is not None else esp
                self.lines.append("I %x OPEHRESUME 0 0 | <-" % a0)
            if a0 in depth:
                esp = depth[a0]
            if a0 in self.fpu_at:
                prev = self.ins[self.bl[bn - 1][-1]] if bn else None
                falls = prev is not None and prev.mnemonic not in ("ret", "jmp")
                self.fpu = self.fpu_merge(a0, list(self.fpu) if falls else None, b[0])
            if a0 in labels:
                self.lines.append("L %x" % a0)
            for k in b:
                try:
                    i = self.ins[k]
                    m, ops = i.mnemonic, i.operands
                    self.at[k] = (esp, ebp)
                    if k in pro:
                        esp, ebp = self.stack_effect(i, esp, ebp)
                        continue
                    if k in self.pairs:
                        op, dv, av, bv = self.pairs[k]
                        self.emit(k, op, [self.pair_tok(dv)], [self.pair_tok(av), self.pair_tok(bv)])
                        flags = self.carry = None
                        if k in self.split64:          # the halves other code reads (find_i64)
                            tok, kl_, rl_, kh_, rh_, wl, wh = self.split64[k]
                            if wl:
                                self.emit(k, "OPASSIGN", ["%s:1004" % self.var_written(kl_, rl_)], [tok])
                            if wh:
                                t2 = self.temp("1008")
                                self.emit(k, "sar", [t2], [tok, "32:1004"])
                                self.emit(k, "OPASSIGN", ["%s:1004" % self.var_written(kh_, rh_)], [t2])
                        if k in self.skip or i.mnemonic in ("adc", "sbb", "mov"):
                            continue
                    if k in self.st64:                # a register pair stored as one __int64 (find_i64)
                        dv, vv = self.st64[k]
                        self.emit(k, "OPASSIGN", [self.pair_tok(dv)], [self.pair_tok(vv)])
                        continue
                    if k in self.chains64:            # a 64-bit compare chain: one branch (find_i64)
                        ch = self.chains64[k]
                        for q in ch["pieces"]:            # cmp / jcc leave esp alone: the lo cmp's operands
                            self.at.setdefault(q, (esp, ebp))
                        t = self.temp("9000")
                        self.emit(k, "OPCMP", [t], [self.pair_tok(ch["vals"][0]), self.pair_tok(ch["vals"][1])])
                        self.emit(k, "OPBRANCH", [], ["s0:0", t, "->"], "cc=%s s0=L%x" % (ch["cc"], ch["T"]))
                        depth.setdefault(ch["T"], esp)
                        self.fpu_at.setdefault(ch["T"], list(self.fpu))
                        self.fpu_branch(ch["T"], len(self.lines) - 2)
                        flags = self.carry = None
                        continue
                    if k in self.skip:
                        continue
                    if k in self.strlens:
                        kl, p = self.strlens[k]
                        t = self.temp()
                        self.emit(k, "OPARG", [t], ["%s:1004" % self.var_read(kl, p)])
                        self.emit(k, "OPCALL", ["%s:1004" % self.var_written(k, p)],
                                  ["?__lift_strlen@@YAXXZ:1004", t], "s0=?__lift_strlen@@YAXXZ")
                        flags = ("%s:1004" % self.var_written(k, p), "0:1004")   # the sub's ZF: length == 0
                        continue
                    if k in self.divs:
                        xop, dvs, sg, kend, dreg = self.divs[k]
                        ty = "1004" if sg else "2004"
                        x = self.opnd(k, xop, esp, ebp).rsplit(":", 1)[0] + ":" + ty
                        self.emit(k, "OPDIV", ["%s:%s" % (self.var_written(kend, dreg), ty)], [x, "%d:%s" % (dvs, ty)])
                        flags = None
                        continue
                    if self.eh and m == "mov" and ops[0].type == X.X86_OP_MEM and ops[1].type == X.X86_OP_IMM:
                        mm = ops[0].mem
                        br = self.md.reg_name(mm.base) if mm.base else None
                        fo = esp + mm.disp if br == "esp" else ebp + mm.disp if br == "ebp" and ebp is not None else None
                        if fo == self.eh["state"] and not mm.index:
                            if self.try_esp is None:
                                self.try_esp = esp
                            v = ops[1].imm & 0xFFFFFFFF
                            v = v - (1 << 32) if v & 0x80000000 else v
                            if ops[0].size == 1 and v == 0xff:
                                v = -1
                            self.lines.append("I %x OPEHSTATE 0 0 | <- %d:1004" % (i.address, v))
                            continue
                    sw = next((x for x in self.switches.values() if i.address in (x["cmp"], x["ja"], x["movzx"])), None)
                    if sw is not None:                   # the switch's own range check / byte-table load
                        if i.address == sw["ja"]:
                            depth.setdefault(sw["default"], esp)
                            self.fpu_at.setdefault(sw["default"], list(self.fpu))
                        if i.address == sw["cmp"]:
                            sw["idxtok"] = "%s:1004" % self.var_read(k, sw["idx"])
                        continue
                    if i.address in self.switches:
                        sw = self.switches[i.address]
                        cases = ",".join("%d:L%x" % (v, t) for v, t in sw["cases"])
                        idx = sw.get("idxtok") if not sw.get("off") else None   # an adjusted index: its value here
                        self.emit(k, "OPSWITCH", [], [idx or "%s:1004" % self.var_read(k, sw["idx"])],
                                  "sw=%s def=L%x" % (cases, sw["default"]))
                        for _v, t in sw["cases"]:
                            depth.setdefault(t, esp)
                            self.fpu_at.setdefault(t, list(self.fpu))
                        continue
                    if m == "push":
                        if self.out_top is None:
                            self.out_top = esp
                        self.out_args[esp - 4] = (self.opnd(k, ops[0], esp, ebp), 4)
                        esp -= 4
                    elif m == "pop":
                        esp += 4
                    elif m in ("add", "sub") and ops[0].type == X.X86_OP_REG and self.md.reg_name(ops[0].reg) == "esp":
                        if m == "sub" and self.out_top is None:
                            self.out_top = esp                 # `sub esp, 8; fstp [esp+4]; movss [esp], x`: arguments
                        esp += (ops[1].imm if m == "add" else -ops[1].imm)
                    elif m == "lahf" and self.fcmp is not None:
                        pass                                   # after fcomi: ZF / PF / CF land where fnstsw puts
                        #                                        C3 / C2 / C0, so `test ah, MASK` reads the same
                    elif m == "call" and self.fpu and not self.out_args and ops[0].type == X.X86_OP_IMM and \
                            self.crt_math(ops[0].imm & 0xFFFFFFFF):
                        # a statically linked CRT math helper (crtfp.py): st0 (st1, st0) in, st0 out: `sqrt(x)`
                        name, n = self.crt_math(ops[0].imm & 0xFFFFFFFF)
                        if len(self.fpu) < n:
                            raise Unsupported("x87 stack underflow (a value from the caller or a call)")
                        argv = self.fpu[-n:]
                        del self.fpu[-n:]
                        f = "?__lift_%s@@YAXXZ" % name
                        t = self.temp(F8)
                        self.emit(k, "OPCALL", [t], [f + ":1004"] + argv, "s0=" + f)
                        self.fpu.append(t)
                        flags = None
                    elif m == "call" and self.fpu and not self.out_args and ops[0].type == X.X86_OP_IMM:
                        # a call with x87 values live: the CRT's _ftol (a normal call needs an empty x87 stack).
                        # It converts st0 to an int in eax and pops it: `(int)x`
                        t = "%s:1004" % self.var_written(k, "eax")
                        self.emit(k, "OPASSIGN", [t], [self.fpu.pop()])
                        flags = None
                    elif m == "call" and self.bv_ctor(k, esp):
                        esp += 4                               # the copy constructor popped its source argument
                        continue
                    elif m == "call":
                        top = self.out_top
                        args = self.take_args(esp)
                        nxt = self.ins[k + 1] if k + 1 < len(self.ins) else None
                        caller_pops = nxt is not None and ((nxt.mnemonic == "add" and nxt.op_str.startswith("esp, ")) or
                                                           (nxt.mnemonic == "pop" and nxt.op_str in ("ecx", "edx")))
                        if top is not None and not caller_pops:
                            esp_after = top                    # __stdcall / __thiscall: the callee popped them
                        else:
                            esp_after = esp
                        thiscall = self.thiscall(k, esp, top)
                        ds_ = self.reach_defs.get((k, "ecx"), ())
                        pushed = any(isinstance(d[0], int) and any(
                            self.ins[q].mnemonic == "push" and self.ins[q].op_str == "ecx" for q in range(d[0] + 1, k))
                            for d in ds_)
                        if thiscall and args and (all(d[0] == "entry" for d in ds_) or pushed) and \
                                self.cleans_before_next_call(k):
                            thiscall = False                   # ecx is only the caller's this left over, and the
                            #                                    caller pops the arguments: a cdecl call
                        if thiscall:                           # `mov ecx, X; call`: X is the callee's `this`, first
                            args = ["%s:1004" % self.var_read(k, "ecx")] + args
                        pushes, ecx_set = [], False
                        toks = []
                        for x in args:
                            t = self.temp()
                            self.emit(k, "OPARG", [t], [x])
                            toks.append(t)
                        tgt = ops[0]
                        if tgt.type == X.X86_OP_IMM:
                            fn = "?FUN_%08x@@YAXXZ" % (tgt.imm & 0xFFFFFFFF)
                            callee, note = fn + ":1004", "s0=" + fn
                        else:
                            callee, note = self.opnd(k, tgt, esp, ebp), ""
                        if k == len(self.ins) - 1 or all(self.ins[q].mnemonic in ("int3", "nop") or
                                                          self.ins[q].mnemonic.startswith("npad")
                                                          for q in range(k + 1, len(self.ins))):
                            note = (note + " noreturn=1").strip()   # nothing after it: the callee never returns
                        if self.byval:
                            sh = 1 if thiscall else 0
                            note = (note + " " + " ".join(("bv%d=%d" % (i + sh, n)) if n > 0 else
                                                          ("bc%d=%d.%x" % (i + sh, -n, _d))
                                                          for i, _b, _d, n in self.byval)).strip()
                        if thiscall:
                            note = (note + " thiscall=1").strip()
                        elif args and not self.caller_cleans(k):
                            note = (note + " stdcall=1").strip()   # no `add esp` / `pop` for its arguments before the
                            #                                     next call or branch: the callee popped them
                        if self.st0_after(k):               # x87 code after it reads st0: a float result,
                            t = self.temp(F8)                  # at x87 precision (cl's code for a `double`
                            #                                    return; a float one adds a rounding store)
                            self.emit(k, "OPCALL", [t], [callee] + toks, note)
                            self.fpu.append(t)
                            flags = None
                            esp = esp_after
                            continue
                        used = self.used_after((k, "eax"))
                        d = ["%s:1004" % self.var_written(k, "eax")] if used else []
                        if k in self.call64:                # its edx:eax is one __int64 an add / sub reads
                            d = ["%s:1008" % self.var_written(k, "eax")]
                        self.emit(k, "OPCALL", d, [callee] + toks, note)
                        esp = esp_after
                        flags = None
                    elif m == "ret":
                        if ops:
                            self.ret_pop = ops[0].imm
                        if k in self.ret64:                 # edx:eax: an __int64 (find_i64)
                            self.value_ret = self.i64_ret = True
                            self.value_rets += 1
                            self.emit(k, "OPRET", [], [self.ret64[k]])
                        elif self.fpu:                      # st0 at ret: a float return
                            self.float_ret = True
                            self.value_ret = True
                            self.emit(k, "OPRET", [], [self.fpu[-1]])
                        elif self.fn_value and self.used_after(None, k=k, reg="eax"):
                            self.value_ret = True
                            w = self.ret_width(k)
                            if w == 1:
                                self.byte_rets += 1
                            self.value_rets += 1
                            self.emit(k, "OPRET", [], ["%s:%s" % (self.var_read(k, "eax"), "2001" if w == 1 else "1004")])
                        else:
                            self.emit(k, "OPRET", [], [])
                    elif m == "jmp":
                        tgt = ops[0]
                        if tgt.type == X.X86_OP_IMM and self.va <= tgt.imm < self.va + self.size:
                            self.emit(k, "OPGOTO", [], ["s0:0", "->"], "s0=L%x" % tgt.imm)
                            depth.setdefault(tgt.imm, esp)
                        elif tgt.type == X.X86_OP_IMM or self.is_tailjmp(i):
                            # a tail call: real IL has a plain OPCALL here (the jmp is chosen in codegen). Its stack
                            # arguments are the caller's own argument slots, as this function left them (f3 0056b620:
                            # `mov [esp+8], 1; jmp` passes key and 1): the current value of each `_aN` variable.
                            arity = max([(o - 4) // 4 + 1 for o in self.args_read] + [self.max_ret_pop // 4])
                            args = ["_a%d:1004" % (4 + 4 * j) for j in range(arity)]
                            for j in range(arity):
                                self.args_read.add(4 + 4 * j)
                            thiscall = self.thiscall(k)
                            note = "s0=?FUN_%08x@@YAXXZ" % (tgt.imm & 0xFFFFFFFF) if tgt.type == X.X86_OP_IMM else ""
                            if thiscall:
                                args = ["%s:1004" % self.var_read(k, "ecx")] + args
                                note = (note + " thiscall=1").strip()
                            toks = []
                            for x in args:
                                t = self.temp()
                                self.emit(k, "OPARG", [t], [x])
                                toks.append(t)
                            fn = "?FUN_%08x@@YAXXZ:1004" % (tgt.imm & 0xFFFFFFFF) if tgt.type == X.X86_OP_IMM else \
                                self.opnd(k, tgt, esp, ebp)   # `jmp [edx+0x18]`: the callee is the vtable slot
                            if self.fn_value:              # a value function: the tail call's result is the return
                                r = self.temp()
                                self.emit(k, "OPCALL", [r], [fn] + toks, note)
                                self.emit(k, "OPRET", [], [r])
                            else:
                                self.emit(k, "OPCALL", [], [fn] + toks, note)
                                self.emit(k, "OPRET", [], [])
                            pushes, ecx_set = [], False
                        else:
                            raise Unsupported("indirect jmp (a switch table?)")
                    elif m.startswith("f") or m == "wait":
                        self.x87(k, i, esp, ebp)
                    elif self.is_sse(i):
                        flags = self.sse(k, i, esp, ebp, flags)
                    elif m == "test" and self.fcmp is not None and ops[0].type == X.X86_OP_REG and \
                            self.md.reg_name(ops[0].reg) == "ah" and ops[1].type == X.X86_OP_IMM:
                        self.fmask = ops[1].imm
                    elif m in CCOF or m in ("jp", "jnp", "jpe", "jpo"):
                        if self.fmask is not None:          # a float compare: fcom* / fnstsw / test ah, MASK / jcc
                            jm = {"jpe": "jp", "jpo": "jnp", "jz": "je", "jnz": "jne"}.get(m, m)
                            cc = FCC.get((self.fmask, jm))
                            if cc is None:
                                raise Unsupported("float compare %s with %s" % (
                                    self.fmask if isinstance(self.fmask, str) else "mask 0x%x" % self.fmask, m))
                            t = self.temp("9000")
                            fa, fb = self.fcmp
                            if fa.endswith(":" + F8) or fb.endswith(":" + F8):   # fldz / fld1 against a double:
                                fa, fb = (re.sub(r"^(-?\d+):%s$" % F4, r"\1:%s" % F8, x) for x in (fa, fb))
                            self.emit(k, "OPCMP", [t], [fa, fb])
                            fneg = cc.startswith("!")
                            self.emit(k, "OPBRANCH", [], ["s0:0", t, "->"], "cc=%s s0=L%x%s" % (
                                cc.lstrip("!"), ops[0].imm, " fneg=1" if fneg else ""))
                            depth.setdefault(ops[0].imm, esp)
                            self.fpu_at.setdefault(ops[0].imm, list(self.fpu))
                            self.fpu_branch(ops[0].imm, len(self.lines) - 2)
                            self.fcmp = self.fmask = None
                            continue
                        if m not in CCOF:
                            raise Unsupported("parity branch")
                        tgt = ops[0].imm
                        t = self.temp("9000")
                        a, b2 = flags or ("0:1004", "0:1004")
                        self.emit(k, "OPCMP", [t], [a, b2])
                        self.emit(k, "OPBRANCH", [], ["s0:0", t, "->"], "cc=%s s0=L%x" % (CCOF[m], tgt))
                        depth.setdefault(tgt, esp)
                        self.fpu_at.setdefault(tgt, list(self.fpu))
                        self.fpu_branch(tgt, len(self.lines) - 2)
                    elif m == "sbb" and len(ops) == 2 and ops[0].type == ops[1].type == X.X86_OP_REG and \
                            ops[0].reg == ops[1].reg:
                        # `sbb r, r` = -CF: 0 or -1 from the last compare (`cmp a, b`: a <u b; `neg x`: x != 0)
                        if self.carry is None:
                            raise Unsupported("sbb after an unknown carry")
                        a, b2, cc = self.carry
                        dn = self.var_written(k, full(self.md.reg_name(ops[0].reg)))
                        self.emit(k, "OPSETCC", ["%s:1004" % dn], [a, b2], "cc=%s neg=1" % cc)
                        self.bform[dn] = ("mask", (a, b2, cc))
                        flags, self.carry = None, None
                    elif m in ("adc", "sbb") and len(ops) == 2 and self.carry is not None:
                        # adc / sbb outside an __int64 pair the pre-pass folded (a pair kept in registers, pushed as
                        # an argument: M&B Operation::execute): d = x +/- y +/- CF, CF as the 0 / 1 of its compare
                        ca, cb, cc = self.carry
                        x, y = self.opnd(k, ops[0], esp, ebp), self.opnd(k, ops[1], esp, ebp)
                        d = self.dst(k, ops[0], esp, ebp)
                        c, t = "%s:1004" % self.temp().rsplit(":", 1)[0], self.temp()
                        self.emit(k, "OPSETCC", [c], [ca, cb], "cc=%s" % cc)
                        self.emit(k, "add" if m == "adc" else "sub", [t], [x, y])
                        self.emit(k, "add" if m == "adc" else "sub", [d], [t, c])
                        flags, self.carry = (d, "0:1004"), None
                    elif m.startswith("set") and ("j" + m[3:]) in CCOF and ops and ops[0].type == X.X86_OP_REG:
                        # setcc r8 after cmp / test: the comparison's value (0 / 1) in a byte register
                        a, b2 = flags or ("0:1004", "0:1004")
                        if flags is None:
                            raise Unsupported("set%s without a compare" % m[3:])
                        self.emit(k, "OPSETCC", ["%s:2001" % self.var_written(k, full(self.md.reg_name(ops[0].reg)))],
                                  [a, b2], "cc=%s" % CCOF["j" + m[3:]])
                    elif m.startswith("set") and ("j" + m[3:]) in CCOF and ops and ops[0].type == X.X86_OP_MEM:
                        # setcc byte [m]: the comparison's value stored (a bool local: M&B Operation::execute)
                        if flags is None:
                            raise Unsupported("set%s without a compare" % m[3:])
                        a, b2 = flags
                        t = "%s:2001" % self.temp().rsplit(":", 1)[0]
                        self.emit(k, "OPSETCC", [t], [a, b2], "cc=%s" % CCOF["j" + m[3:]])
                        self.emit(k, "OPASSIGN", [self.dst(k, ops[0], esp, ebp)], [t])
                    elif m in ("cmp", "test"):
                        a, b2 = self.opnd(k, ops[0], esp, ebp), self.opnd(k, ops[1], esp, ebp)
                        if m == "test" and a == b2:
                            b2 = "0:1004"
                        elif m == "test":                      # test a, b: the flags of a & b (not of a - b)
                            t = "%s:%s" % (self.temp().rsplit(":", 1)[0], a.rsplit(":", 1)[-1])
                            self.emit(k, "and", [t], [a, b2])
                            a, b2 = t, "0:%s" % a.rsplit(":", 1)[-1]
                        flags = (a, b2)
                        self.carry = (a, b2, "b") if m == "cmp" else None
                    elif m == "mov" and ops[0].type == X.X86_OP_REG and ops[1].type == X.X86_OP_REG and \
                            self.md.reg_name(ops[1].reg) == "esp" and self.md.reg_name(ops[0].reg) != "ebp":
                        self.esp_alias[k] = esp                # `mov eax, esp`: eax points at the outgoing area
                    elif m in ("mov", "movzx", "movsx"):
                        if ops[0].type == X.X86_OP_REG and self.md.reg_name(ops[0].reg) == "ecx":
                            ecx_set = True
                        self.emit(k, "OPASSIGN", [self.dst(k, ops[0], esp, ebp)], [self.opnd(k, ops[1], esp, ebp)])
                        flags = flags
                    elif m == "lea" and ops[1].mem.base == ops[0].reg and not ops[1].mem.index and not ops[1].mem.disp:
                        pass                                   # `lea esp, [esp]` / `lea ecx, [ecx]`: alignment filler
                    elif m == "lea":
                        self.lea(k, ops, esp, ebp)
                    elif m in ARITH:
                        d = self.dst(k, ops[0], esp, ebp)
                        if m == "xor" and ops[0].type == X.X86_OP_REG and len(ops) == 2 and ops[1].type == X.X86_OP_REG \
                                and ops[0].reg == ops[1].reg:
                            self.emit(k, "OPASSIGN", [d], ["0:1004"])
                            flags = (d, "0:1004")
                            continue
                        if ops[0].type == X.X86_OP_REG and self.md.reg_name(ops[0].reg) == "ecx":
                            ecx_set = True
                        a = self.opnd(k, ops[0], esp, ebp)
                        b2 = self.opnd(k, ops[1], esp, ebp) if len(ops) > 1 else "1:1004"
                        bf = self.bform.get(a.rsplit(":", 1)[0])
                        if bf and bf[0] == "mask" and m == "add" and b2 == "1:1004":
                            c = bf[1]                          # -(c) + 1 = !c as 0 / 1
                            nc = (c[0], c[1], INVCC[c[2]])
                            self.emit(k, "OPSETCC", [d], [nc[0], nc[1]], "cc=%s" % nc[2])
                            self.bform[d.rsplit(":", 1)[0]] = ("bool", nc)
                            flags, self.carry = (d, "0:1004"), None
                            continue
                        if bf and bf[0] == "mask" and m == "and":
                            c = bf[1]                          # mask(c) & X = c ? X : 0 (cl's branch-free select)
                            self.emit(k, "OPSELECT", [d], [c[0], c[1], b2, "0:1004"], "cc=%s" % c[2])
                            flags, self.carry = (d, "0:1004"), None
                            continue
                        if m == "add" and d.rsplit(":", 1)[0] == a.rsplit(":", 1)[0] and self.adc_follows(k):
                            t0 = self.temp()                   # the old value, for the carry an adc reads
                            self.emit(k, "OPASSIGN", [t0], [a])
                            a_old = t0
                        else:
                            a_old = a
                        self.emit(k, ARITH[m], [d], [a, b2])
                        flags = (d, "0:1004")
                        # CF: sub a, b -> a <u b; add a, b -> the sum <u a (unsigned overflow), for an adc / sbb
                        self.carry = (a, b2, "b") if m == "sub" else (d, a_old, "b") if m == "add" and \
                            not d.startswith("[") else None
                    elif m == "imul":
                        d = self.dst(k, ops[0], esp, ebp)
                        srcs = [self.opnd(k, o, esp, ebp) for o in (ops[1:] if len(ops) > 1 else ops)]
                        if len(ops) == 2:
                            srcs = [self.opnd(k, ops[0], esp, ebp)] + srcs
                        self.emit(k, "OPMUL", [d], srcs[:2])
                    elif m in ("inc", "dec"):
                        d = self.dst(k, ops[0], esp, ebp)
                        self.emit(k, "add" if m == "inc" else "sub", [d], [self.opnd(k, ops[0], esp, ebp), "1:1004"])
                        flags = (d, "0:1004")
                    elif m in ("neg", "not"):
                        d = self.dst(k, ops[0], esp, ebp)
                        x = self.opnd(k, ops[0], esp, ebp)
                        bf = self.bform.get(x.rsplit(":", 1)[0]) if m == "neg" else None
                        if bf:                                 # neg of bool(c) = mask(c); of mask(c) = bool(c);
                            c = bf[1]                          # either way CF = (value != 0) = c
                            self.emit(k, "OPSETCC", [d], [c[0], c[1]], "cc=%s%s" % (c[2], " neg=1" if bf[0] == "bool"
                                                                                     else ""))
                            self.bform[d.rsplit(":", 1)[0]] = ("mask" if bf[0] == "bool" else "bool", c)
                            flags, self.carry = (d, "0:1004"), c
                            continue
                        self.emit(k, m, [d], [x])
                        flags = (d, "0:1004")
                        self.carry = (x, "0:1004", "ne") if m == "neg" else self.carry
                    elif m == "cdq":
                        pass                                   # edx = sign of eax: implied by the idiv that follows
                    elif m in ("idiv", "div"):
                        ty = "1004" if m == "idiv" else "2004"
                        num = "%s:%s" % (self.var_read(k, "eax"), ty)
                        den = self.opnd(k, ops[0], esp, ebp).rsplit(":", 1)[0] + ":" + ty
                        self.emit(k, "OPDIV", ["%s:%s" % (self.var_written(k, "eax"), ty)], [num, den])
                        self.emit(k, "OPMOD", ["%s:%s" % (self.var_written(k, "edx"), ty)], [num, den])
                    elif m in ("rep movsd", "rep movsb") or (m == "rep" and i.op_str.startswith("movs")) or \
                            i.op_str.startswith(("movsd", "movsb")) and m == "rep":
                        self.block_copy(k, i)
                    elif m in ("rep stosd", "rep stosb"):
                        self.block_fill(k, i)
                    elif m in ("movsd", "movsb") and not ops:  # a single unrolled element of a copy
                        raise Unsupported("string move without rep")
                    elif m in ("lock xadd", "xadd", "xchg", "lock xchg") and len(ops) == 2 and \
                            X.X86_OP_MEM in (ops[0].type, ops[1].type):
                        flags = self.interlocked(k, i, esp, ebp)
                    elif m == "rdtsc":
                        if not self.readers((k, "edx")):  # the low half only: (unsigned)__rdtsc()
                            self.emit(k, "OPCALL", ["%s:2004" % self.var_written(k, "eax")],
                                      ["?__lift_rdtsc@@YAXXZ:1004"], "s0=?__lift_rdtsc@@YAXXZ")
                            continue
                        t = self.temp("2008")
                        self.emit(k, "OPCALL", [t], ["?__lift_rdtsc@@YAXXZ:1004"], "s0=?__lift_rdtsc@@YAXXZ")
                        n = t.rsplit(":", 1)[0]
                        self.emit(k, "OPASSIGN", ["%s:2004" % self.var_written(k, "eax")], ["%s:2004" % n])
                        if True:
                            h = self.temp("2008")
                            self.emit(k, "shr", [h], [t, "32:1004"])
                            self.emit(k, "OPASSIGN", ["%s:2004" % self.var_written(k, "edx")],
                                      ["%s:2004" % h.rsplit(":", 1)[0]])
                    elif m in ("nop", "int3") or m.startswith("npad"):
                        pass
                    else:
                        raise Unsupported("instruction %s" % m)
                except Exception as ex:             # partial: any failure here (an x87 stack left short by an
                    if not self.partial or not isinstance(ex, (Unsupported, IndexError, KeyError, ValueError,
                                                                AttributeError, TypeError)):   # earlier stub, ..)
                        raise
                    # partial mode: keep going; the construct becomes a marker the emitter writes as a comment
                    self.lines.append("I %x OPUNSUP 0 0 | <- ; why=%s" % (self.ins[k].address,
                                      re.sub(r"\s+", "_", str(ex))[:80]))
            last = self.ins[b[-1]]
            if last.mnemonic not in ("jmp", "ret") and bn + 1 < len(self.bl):
                depth.setdefault(self.ins[self.bl[bn + 1][0]].address, esp)
        self.size_indexed()
        return self.lines

    def size_indexed(self):
        """Each register-indexed local array (`[esp + esi*8 + 0x2c]`) as one object of its own extent: from its base
        to the next address-taken slot past its run of directly accessed elements (or the next indexed base), rounded
        down to a power of two of elements (M&B Operation::execute: __int64 params[16], int types[16]). Bases one
        stride apart are one array (an __int64's two halves). With no boundary, the slots up to the frame's top: the
        layout is then kept, at the cost of every local above living in memory."""
        if not self.indexed:
            return
        acc, lea = set(), set()
        for k, i in enumerate(self.ins):
            if k not in self.at:
                continue
            esp, ebp = self.at[k]
            for o in i.operands:
                if o.type != X.X86_OP_MEM or not o.mem.base or o.mem.index:
                    continue
                b = self.md.reg_name(o.mem.base)
                if b == "esp" or (b == "ebp" and ebp is not None):
                    off = (esp if b == "esp" else ebp) + o.mem.disp
                    (lea if i.mnemonic == "lea" else acc).add(off)
        bases = sorted(self.indexed)
        groups = []
        for o in bases:
            if groups and o - groups[-1][0] < groups[-1][1]:
                continue                                     # the other half of the same elements
            groups.append((o, self.indexed[o]))
        for n, (o, sc) in enumerate(groups):
            end = o
            while end + sc in acc or end + sc in lea:         # the run of elements accessed one by one
                end += sc
            nxt = [x for x in lea if x > end] + ([groups[n + 1][0]] if n + 1 < len(groups) else [])
            if not nxt or min(nxt) >= 0:
                self.aggregates.add((o, -o))
                continue
            cnt = (min(nxt) - o) // sc
            p2 = 1
            while p2 * 2 <= cnt:
                p2 *= 2
            size = max(p2 * sc, end + sc - o)
            self.aggregates.add((o, min(size, -o)))

    def sse(self, k, i, esp, ebp, flags):
        """SSE scalars as typed IL (e004 / e008 at Lower's entry on /arch:SSE): moves, the *ss / *sd arithmetic,
        xorps (0.0 when both operands are one register, a negation by a sign-mask constant otherwise), comiss
        compares (their jcc reads CF/ZF: jbe is <=, ja is >), and the int <-> float conversions."""
        m, ops = i.mnemonic, i.operands
        if m in SSE_MOVES:
            self.emit(k, "OPASSIGN", [self.dst(k, ops[0], esp, ebp)], [self.opnd(k, ops[1], esp, ebp)])
            return flags
        if m in SSE_OPS:
            op, ty = SSE_OPS[m]
            self.emit(k, op, [self.dst(k, ops[0], esp, ebp)], [self.opnd(k, ops[0], esp, ebp),
                                                              self.opnd(k, ops[1], esp, ebp)])
            return flags
        if m in ("xorps", "xorpd"):
            if ops[1].type == X.X86_OP_REG and ops[1].reg == ops[0].reg:
                self.emit(k, "OPASSIGN", [self.dst(k, ops[0], esp, ebp)], ["0:%s" % self.sse_ty(i)])
            else:
                self.emit(k, "neg", [self.dst(k, ops[0], esp, ebp)], [self.opnd(k, ops[0], esp, ebp)])
            return flags
        if m in ("comiss", "ucomiss", "comisd", "ucomisd"):
            ab = (self.opnd(k, ops[0], esp, ebp), self.opnd(k, ops[1], esp, ebp))
            if k + 1 < len(self.ins) and self.ins[k + 1].mnemonic == "lahf":
                # `ucomiss; lahf; test ah, MASK; jcc`: ZF / PF / CF land where fnstsw puts C3 / C2 / C0, with the
                # same meaning (unordered sets all three), so the x87 table (FCC) reads it
                self.fcmp = ab
            return ab
        if m in ("cvtsi2ss", "cvtsi2sd"):
            src = self.opnd(k, ops[1], esp, ebp)
            self.emit(k, "OPASSIGN", [self.dst(k, ops[0], esp, ebp)], [src])
            return flags
        if m == "cvtss2si" or (m in ("maxss", "minss") and ops[0].type == X.X86_OP_REG):
            return self.sse_helper(k, i, esp, ebp, flags)
        if m in ("cvttss2si", "cvttsd2si", "cvtss2sd", "cvtsd2ss"):
            d = self.dst(k, ops[0], esp, ebp)
            if m.startswith("cvtt"):
                d = d.rsplit(":", 1)[0] + ":1004"
            elif m == "cvtsd2ss":
                d = d.rsplit(":", 1)[0] + ":e004"
            src = self.opnd(k, ops[1], esp, ebp)
            if m == "cvtsd2ss" and src.endswith(":e004") and ops[1].type == X.X86_OP_REG:
                src = src.rsplit(":", 1)[0] + ":e008"
            self.emit(k, "OPASSIGN", [d], [src])
            return flags
        raise Unsupported("sse %s" % m)

    def sse_helper(self, k, i, esp, ebp, flags):
        """Spore's vetted inline-asm float helpers (spore-decomp match/include/sse_helpers.h): VC++ 2008 emits none of
        these shapes from C++, so the lift calls the helper the original's source used.
          cvtss2si r, x                               -> r = SseRoundToInt(x)
          xorps d, d; maxss d, x [; minss d, hi]      -> d = SseMaxZero(x) / SseSaturate(x, hi)
          maxss d, lo [; minss d, hi]                 -> d = SseMax(d, lo) / SseClamp(d, lo, hi)
        A lone minss has no helper (refused)."""
        m, ops = i.mnemonic, i.operands
        blk = next((b for b in self.bl if k in b), [k])

        def fwd(j, op):
            """A frame operand of the helper is the helper's own parameter slot: the value stored there earlier in
            the block (`movss [esp], xmm0` after `movss xmm0, [one]`) is the argument (Spore 005a6e00)."""
            tok = self.opnd(j, op, esp, ebp)
            if op.type != X.X86_OP_MEM or not re.match(r"^_[la]\d+:", tok):
                return tok
            for q in range(j - 1, blk[0] - 1, -1):
                x = self.ins[q]
                if x.mnemonic == "movss" and x.operands[0].type == X.X86_OP_MEM and x.operands[1].type == X.X86_OP_REG \
                        and q in self.at and self.opnd(q, x.operands[0], *self.at[q]) == tok:
                    return self.opnd(q, x.operands[1], *self.at[q])
                if x.mnemonic in ("call",) or (x.operands and x.operands[0].type == X.X86_OP_MEM and
                                               q in self.at and self.opnd(q, x.operands[0], *self.at[q]) == tok):
                    break
            return tok

        def one(tok):
            mg = re.match(r"^\?g_([0-9a-f]{8})@@3HA:", tok)
            return bool(mg) and self.dword(int(mg.group(1), 16)) == 0x3f800000

        def call(fn, d, args, ty):
            if fn == "SseSaturate" and one(args[1]):
                fn, args = "SseClamp01", args[:1]               # min(max(x, 0), 1): the helper's own `one`
            a = [self.temp(ty) for _ in args]
            for t, x in zip(a, args):
                self.emit(k, "OPARG", [t], [x.rsplit(":", 1)[0] + ":e004" if x.endswith((":e004", ":5004", ":4004"))
                                            else x])
            f = "?__lift_%s@@YAXXZ" % fn
            self.emit(k, "OPCALL", [d], [f + ":1004"] + a, "s0=" + f)
        if m == "cvtss2si":
            # SseFloorToInt / SseCeilToInt: `cvtss2si eax, xmm0; cvtsi2ss xmm1, eax; mov ecx, eax; sub / add ecx, 1;
            # ucomiss xmm0, xmm1 (xmm1, xmm0); cmovb eax, ecx`
            q = self.ins[k + 1:k + 6]
            sh = [x.mnemonic for x in q]
            if len(q) == 5 and sh[:2] == ["cvtsi2ss", "mov"] and sh[2] in ("sub", "add") and sh[3] == "ucomiss" and \
                    sh[4] == "cmovb" and ops[0].type == X.X86_OP_REG:
                r = self.md.reg_name(ops[0].reg)
                fl = q[2].mnemonic == "sub"
                d = "%s:1004" % self.var_written(k + 5, full(r))
                self.skip.update(range(k + 1, k + 6))
                call("SseFloorToInt" if fl else "SseCeilToInt", d, [self.opnd(k, ops[1], esp, ebp)], "e004")
                return None
            d = self.dst(k, ops[0], esp, ebp).rsplit(":", 1)[0] + ":1004"
            call("SseRoundToInt", d, [self.opnd(k, ops[1], esp, ebp)], "e004")
            return flags
        reg = ops[0].reg
        nx = self.ins[k + 1] if k + 1 < len(self.ins) else None
        hi = None
        if m == "maxss" and nx is not None and nx.mnemonic == "minss" and nx.operands[0].type == X.X86_OP_REG and \
                nx.operands[0].reg == reg:
            hi = fwd(k + 1, nx.operands[1])
            self.skip.add(k + 1)
        if m == "minss":
            raise Unsupported("minss without a maxss (no helper)")
        pv = self.ins[k - 1] if k else None
        zero = pv is not None and pv.mnemonic == "xorps" and pv.operands[0].reg == reg == pv.operands[1].reg
        d = self.dst(k + (1 if hi else 0), ops[0], esp, ebp) if not hi else \
            "%s:e004" % self.var_written(k + 1, self.md.reg_name(reg))
        x = fwd(k, ops[1])
        if zero:
            call("SseSaturate" if hi else "SseMaxZero", d, [x] + ([hi] if hi else []), "e004")
        else:
            cur = self.opnd(k, ops[0], esp, ebp)
            call("SseClamp" if hi else "SseMax", d, [cur, x] + ([hi] if hi else []), "e004")
        return flags

    def interlocked(self, k, i, esp, ebp):
        """`lock xadd [m], r` is r = _InterlockedExchangeAdd(&m, r), `xchg [m], r` r = _InterlockedExchange(&m, r)
        (the intrinsics compile to exactly these; a refcount's ++ / -- is xadd with 1 / -1)."""
        ops = i.operands
        mo, ro = (ops[0], ops[1]) if ops[0].type == X.X86_OP_MEM else (ops[1], ops[0])
        if ro.type != X.X86_OP_REG or ro.size != 4:
            raise Unsupported("interlocked operand")
        mm = mo.mem
        base = self.md.reg_name(mm.base) if mm.base else None
        if not base or mm.index or base in ("esp", "ebp"):
            raise Unsupported("interlocked address")
        addr = "%s:1004" % self.var_read(k, full(base))
        if mm.disp:
            t = self.temp()
            self.emit(k, "add", [t], [addr, "%d:1004" % mm.disp])
            addr = t
        r = full(self.md.reg_name(ro.reg))
        fn = "?__lift_%s@@YAXXZ" % ("xadd" if "xadd" in i.mnemonic else "xchg")
        v = self.consts.get(self.var_read(k, r))
        d = self.one_def(k, r)
        if v is None and d is not None and self.ins[d].mnemonic in ("mov", "or") and \
                self.ins[d].operands[1].type == X.X86_OP_IMM and (self.ins[d].mnemonic == "mov" or
                                                                  self.ins[d].operands[1].imm & 0xFFFFFFFF == 0xFFFFFFFF):
            v = self.ins[d].operands[1].imm & 0xFFFFFFFF       # `or r, -1`: cl's short way to load -1
        nx = self.ins[k + 1] if k + 1 < len(self.ins) else None
        if "xadd" in i.mnemonic and v is not None and (v & 0xFFFFFFFF) in (1, 0xFFFFFFFF) and nx is not None and \
                nx.mnemonic == ("inc" if v & 0xFFFFFFFF == 1 else "dec") and nx.op_str == r:
            # `mov r, 1; lock xadd [m], r; inc r`: _InterlockedIncrement's new value (or -1 / dec: Decrement)
            fn = "?__lift_%s@@YAXXZ" % ("inc" if v & 0xFFFFFFFF == 1 else "dec")
            self.skip.add(k + 1)
            a = [self.temp()]
            self.emit(k, "OPARG", [a[0]], [addr])
            res = "%s:1004" % self.var_written(k + 1, r)
            self.emit(k, "OPCALL", [res], [fn + ":1004"] + a, "s0=" + fn)
            return (res, "0:1004")                        # the inc / dec's flags: the new value against 0
        a = [self.temp(), self.temp()]
        self.emit(k, "OPARG", [a[0]], [addr])
        self.emit(k, "OPARG", [a[1]], ["%s:1004" % self.var_read(k, r)])
        self.emit(k, "OPCALL", ["%s:1004" % self.var_written(k, r)], [fn + ":1004"] + a, "s0=" + fn)
        return None

    def block_fill(self, k, i):
        """`rep stosd` (edi, eax, ecx dwords): memset when eax is 0 or one byte repeated (cl's zeroing of a struct
        or array, the memset intrinsic); another fill value is no memset."""
        v = self.consts.get(self.var_read(k, "eax"))
        if v is None:
            raise Unsupported("rep stos of a non-constant")
        v &= 0xFFFFFFFF
        unit = 4 if i.mnemonic.endswith("d") else 1
        if unit == 4 and v not in (0, 0xFFFFFFFF) and len({(v >> s_) & 0xFF for s_ in (0, 8, 16, 24)}) != 1:
            raise Unsupported("rep stosd of 0x%x" % v)
        n = "%s:1004" % self.var_read(k, "ecx")
        self.note_aggregate(k, ("edi",), n, unit)
        c = self.consts.get(n.rsplit(":", 1)[0])
        if c and c > 0:
            n, unit = "%d:1004" % (c * unit), 1
        if unit > 1:
            t = self.temp()
            self.emit(k, "shl", [t], [n, "2:1004"])
            n = t
        a = [self.temp(), self.temp(), self.temp()]
        self.emit(k, "OPARG", [a[0]], ["%s:1004" % self.var_read(k, "edi")])
        self.emit(k, "OPARG", [a[1]], ["%d:1004" % (v & 0xFF)])
        self.emit(k, "OPARG", [a[2]], [n])
        self.emit(k, "OPCALL", [], ["?__lift_memset@@YAXXZ:1004"] + a, "s0=?__lift_memset@@YAXXZ")

    def crt_math(self, va):
        """(name, argument count) when va is a CRT x87 math helper (`__CIsqrt` ..), by fingerprint."""
        if not hasattr(self, "_crt"):
            self._crt = {}
        if va not in self._crt:
            import crtfp
            self._crt[va] = crtfp.match(self.img, va)
        return self._crt[va]

    def note_aggregate(self, k, regs, n, unit):
        """A block copy / fill of a constant count on a frame address: those bytes are one object (an array or a
        struct), not separate scalars; a memcpy from one scalar's address would read past it, and cl drops it."""
        c = self.consts.get(n.rsplit(":", 1)[0])
        if not c or c <= 0:
            return
        for r in regs:
            off = self.frame_addr.get(self.var_read(k, r))
            if off is not None and off < 0:
                self.aggregates.add((off, c * unit))

    def block_copy(self, k, i):
        """`rep movsd` (edi <- esi, ecx dwords): the IL of a struct copy is OPMBASSIGN, which il2c does not write;
        a memcpy of the same size is what cl /O2 turns back into `mov ecx, N; rep movsd` (an intrinsic)."""
        unit = 4 if "movsd" in (i.mnemonic + " " + i.op_str) else 1
        n = "%s:1004" % self.var_read(k, "ecx")
        c = self.consts.get(n.rsplit(":", 1)[0])
        ds = self.reach_defs.get((k, "edi"), set())
        d = next(iter(ds))[0] if len(ds) == 1 else None
        if isinstance(d, int) and d in self.esp_alias and c and c > 0 and self.out_top is not None:
            # `sub esp, 32; mov edi, esp; mov ecx, 8; lea esi, [x]; rep movsd`: a struct passed by value, copied
            # into the argument area (M&B 00411920): one argument, the source's address, written *(S32*)addr
            off = self.esp_alias[d]
            self.note_aggregate(k, ("esi",), n, unit)
            self.out_args[off] = ("%s:1004" % self.var_read(k, "esi"), c * unit)
            self.bv_block[off] = c * unit
            return
        self.note_aggregate(k, ("esi", "edi"), n, unit)
        nb = self.var_tail(k, unit, c)
        if nb is not None:                                 # memcpy(d, s, n) of a variable n: one copy
            n, unit = nb, 1
        elif c and c > 0:                                  # a constant count: the literal byte size, so cl
            n, unit = "%d:1004" % (c * unit), 1               # inlines the copy again (`mov ecx, 9; rep movsd`)
        if unit > 1:
            t = self.temp()
            self.emit(k, "shl", [t], [n, "2:1004"])
            n = t
        a = [self.temp(), self.temp(), self.temp()]
        n0 = len(self.lines)
        self.emit(k, "OPARG", [a[0]], ["%s:1004" % self.var_read(k, "edi")])
        self.emit(k, "OPARG", [a[1]], ["%s:1004" % self.var_read(k, "esi")])
        self.emit(k, "OPARG", [a[2]], [n])
        self.emit(k, "OPCALL", [], ["?__lift_memcpy@@YAXXZ:1004"] + a, "s0=?__lift_memcpy@@YAXXZ")
        if unit == 1 and len(self.lines) == n0 + 4:
            self.hoist_copy(n0)

    def copy_root(self, k, r):
        """The value r holds at k, through register copies (`mov eax, ecx`): the first non-copy definition."""
        for _ in range(8):
            d = self.one_def(k, r)
            if d is None:
                return None
            i = self.ins[d]
            if i.mnemonic == "mov" and i.operands[1].type == X.X86_OP_REG and i.operands[1].size == 4:
                k, r = d, self.md.reg_name(i.operands[1].reg)
                continue
            return (d, r)
        return None

    def var_tail(self, k, unit, c):
        """cl's memcpy of a variable byte count n: `shr ecx, 2; rep movsd; [mov ecx, n;] and ecx, 3; rep movsb`
        (ecx = n both times). -> n's IL token, the tail in self.skip; None when it is not that shape."""
        if unit != 4 or c:
            return None
        ds = self.one_def(k, "ecx")
        if ds is None or self.ins[ds].mnemonic != "shr" or self.ins[ds].op_str != "ecx, 2":
            return None
        root = self.copy_root(ds, "ecx")
        for j in range(k + 1, min(k + 4, len(self.ins))):
            i = self.ins[j]
            if i.mnemonic == "and" and i.op_str == "ecx, 3":
                nx = self.ins[j + 1] if j + 1 < len(self.ins) else None
                if root is None or self.copy_root(j, "ecx") != root or nx is None or \
                        "movsb" not in nx.mnemonic + " " + nx.op_str:
                    return None
                if any("esi" in self.regs(self.ins[x])[1] or "edi" in self.regs(self.ins[x])[1]
                       for x in range(k + 1, j)):
                    return None
                self.skip.update(range(k + 1, j + 2))
                return "%s:1004" % self.var_read(ds, "ecx")
            if i.mnemonic != "mov" or i.operands[0].type != X.X86_OP_REG:
                return None
        return None

    def hoist_copy(self, n0):
        """The copy's IL (lines n0..) goes up to just after its operands' last definition: cl schedules a loop's
        pointer increments and the count's `mov ecx, N` ahead of `rep movsd`, but the source copies first
        (`*d = *s; ++s; ++d;`: M&B 0040fcf0), and the copy's place in the source decides that order. Only past
        register arithmetic in the same block: no label, branch, call or memory operand."""
        used = set()
        for l in self.lines[n0:n0 + 3]:
            used |= set(re.findall(r"([A-Za-z_?$@][\w?$@]*):[0-9a-f]+", l.split("<-", 1)[1].split(";")[0]))
        j = n0
        while j > 0:
            l = self.lines[j - 1]
            if not l.startswith("I ") or "[" in l or re.match(r"^I \w+ (OPCALL|OPBRANCH|OPRET|OPARG|OPCMP)", l):
                break
            ds = set(re.findall(r"([A-Za-z_?$@][\w?$@]*):[0-9a-f]+", l.split("|", 1)[1].split("<-", 1)[0]))
            if ds & used or not ds:
                break
            j -= 1
        if j < n0:
            self.lines[j:n0 + 4] = self.lines[n0:n0 + 4] + self.lines[j:n0]

    def ret_width(self, k):
        """The width of the writes reaching the ret at k in eax: 1 when every one writes al (`mov al, 1`, `xor al,
        al`, `setcc al`, `and al, 1`): c2's spelling of a bool / char return; else 4."""
        ws = set()
        for d in self.reach_defs.get((k, "eax"), set()):
            if d[0] in ("entry", "undef"):
                continue
            i = self.ins[d[0]]
            op = i.operands[0] if i.operands else None
            ws.add(op.size if op is not None and op.type == X.X86_OP_REG and full(self.md.reg_name(op.reg)) == "eax"
                   else 4)
        return 1 if ws == {1} else 4

    X87_LOADS = ("fld", "fild", "fldz", "fld1", "fldpi", "fldl2e", "fldln2", "fldlg2", "fldl2t")

    def fpu_branch(self, tgt, pos):
        """A conditional branch to tgt with x87 values live: where its copies go if tgt turns out to be a join
        (the IL position before its compare)."""
        if self.fpu:
            self.fpu_pend.setdefault(tgt, []).append([pos, list(self.fpu)])

    def fpu_merge(self, a0, fall, k):
        """The x87 stack at block a0: the branch's, or, where the fall-through path holds another value in a
        slot (`if (x > max) x = max;`: st0 is x on one edge, max on the other), a variable assigned on both
        edges, before the branch and here."""
        st = list(self.fpu_at[a0])
        pend = self.fpu_pend.pop(a0, [])
        if fall is None or len(fall) != len(st) or not pend or any(len(p[1]) != len(st) for p in pend):
            return st
        out = list(st)
        for j in range(len(st)):
            if all(p[1][j] == fall[j] for p in pend):
                continue
            ty = F8 if any(x.endswith(F8) for x in [fall[j]] + [p[1][j] for p in pend]) else F4
            same = {p[1][j] for p in pend}
            if len(same) == 1 and re.match(r"^_a\d+:", next(iter(same))):
                v = next(iter(same))                      # `if (x > max) { ..; x = max; }`: the parameter itself
                self.lines.append("I %x OPASSIGN 0 0 | %s <- %s" % (self.ins[k].address, v, fall[j]))
                out[j] = v
                continue
            v = self.temp(ty)
            for p in sorted(pend, key=lambda p: -p[0]):
                self.lines.insert(p[0], "I %x OPASSIGN 0 0 | %s <- %s" % (self.ins[k].address, v, p[1][j]))
                c = self.lines[p[0] + 1] if p[0] + 1 < len(self.lines) else ""
                if " OPCMP " in c and not p[1][j].startswith("["):   # the branch compares the variable itself:
                    hd, _, tl = c.partition(" <- ")                 # `x = a; if (x > max) ..`
                    self.lines[p[0] + 1] = hd + " <- " + " ".join(v if t == p[1][j] else t for t in tl.split(" "))
                for q in [x for xs in self.fpu_pend.values() for x in xs] + [x for x in pend if x is not p]:
                    if q[0] >= p[0]:
                        q[0] += 1
            self.lines.append("I %x OPASSIGN 0 0 | %s <- %s" % (self.ins[k].address, v, fall[j]))
            out[j] = v
        return out

    def x87_need(self, i):
        """(entries read, stack change) of an x87 instruction; None when not modelled."""
        m, ops = i.mnemonic, i.operands
        regs = [int(self.md.reg_name(o.reg)[3:-1]) if self.md.reg_name(o.reg).startswith("st(") else 0
                for o in ops if o.type == X.X86_OP_REG]
        deep = max(regs) + 1 if regs else 0
        mem = any(o.type == X.X86_OP_MEM for o in ops)
        if m in ("fnstsw", "fstsw", "fwait", "wait", "fnstcw", "fldcw", "fnclex"):
            return 0, 0
        if m in ("fldz", "fld1", "fldpi", "fldl2e", "fldln2", "fild") or (m == "fld" and mem):
            return 0, 1
        if m == "fld":
            return deep, 1
        if m in ("fstp", "fistp", "fisttp", "fbstp"):
            return max(1, deep), -1
        if m in ("fst", "fist"):
            return max(1, deep), 0
        if m in ("fchs", "fabs", "fsqrt", "fsin", "fcos", "frndint", "ftst", "fxam") or m in FIOPS:
            return 1, 0
        if m in ("fsincos", "fptan"):
            return 1, 1
        if m == "fpatan" or m == "fyl2x":
            return 2, -1
        if m == "fxch":
            return max(2, deep), 0
        if m in ("fcompp", "fucompp"):
            return 2, -2
        if m in ("fcom", "fucom", "fcomi", "fucomi"):
            return max(1, deep) if (mem or regs) else 2, 0
        if m in ("fcomp", "fucomp", "fcomip", "fucomip", "fcompi", "fucompi"):
            return max(1, deep) if (mem or regs) else 2, -1
        base = m[:-1] if m.endswith("p") and m[:-1] in FOPS else m
        if base in FOPS:
            if mem:
                return 1, 0
            if base != m:                                 # faddp [st(i)]: st(i) op= st0, pop
                return max(2, deep), -1
            return max(1, deep), 0
        return None

    def st0_after(self, k):
        """Does the call at k return a value in st0? Yes when x87 code after it (before the next call or branch)
        reads deeper than what it pushed itself (`call get; fld [a]; fmul [b]; faddp st(1)` reads the result)."""
        d = 0
        for i in self.ins[k + 1:]:
            if i.mnemonic in ("call", "ret", "jmp") or i.mnemonic.startswith("j"):
                return False
            if i.mnemonic.startswith("f"):
                r = self.x87_need(i)
                if r is None:                             # not modelled: the old test, the first x87 op only
                    return d == 0 and i.mnemonic not in self.X87_LOADS and \
                        not i.mnemonic.startswith(("fnst", "fstsw", "fwait"))
                need, delta = r
                if need > d:
                    return True
                d += delta
        return False

    def st(self, op):
        """The x87 stack entry an st(i) operand names."""
        n = self.md.reg_name(op.reg)
        j = int(n[3:-1]) if n.startswith("st(") else 0
        if j >= len(self.fpu):
            raise Unsupported("x87 stack underflow (a value from the caller or a call)")
        return len(self.fpu) - 1 - j

    def fild_type(self, k):
        """The precision of a fild's value: the source converted the integer to float when the first float memory
        operand the value meets is a dword (`(float)d * 1e-5f` is `fild; fmul dword`; as a double cl multiplies by
        a qword constant), else to double."""
        for x in self.ins[k + 1:k + 12]:
            if x.mnemonic.startswith("j") or x.mnemonic in ("call", "ret", "fild", "fld", "fldz", "fld1"):
                break
            if x.mnemonic.startswith("f") and not x.mnemonic.startswith(("fi", "fnst", "fstsw", "fx")) and \
                    any(o.type == X.X86_OP_MEM for o in x.operands):
                op = next(o for o in x.operands if o.type == X.X86_OP_MEM)
                return F4 if op.size == 4 else F8
        return F8

    def x87(self, k, i, esp, ebp):
        m, ops = i.mnemonic, i.operands
        if m in ("fld", "fild"):
            if ops[0].type == X.X86_OP_REG:
                self.fpu.append(self.fpu[self.st(ops[0])])
            elif m == "fild":
                t = self.temp(self.fild_type(k))
                src = self.opnd(k, ops[0], esp, ebp).rsplit(":", 1)[0] + ":" + MTY.get(ops[0].size, "1004")
                self.emit(k, "OPASSIGN", [t], [src])
                self.fpu.append(t)
            else:
                self.fpu.append(self.opnd(k, ops[0], esp, ebp))
            return
        if m in ("fldz", "fld1"):
            self.fpu.append(("0:%s" if m == "fldz" else "1:%s") % F4)
            return
        if m in ("fstp", "fst"):
            if not self.fpu:
                raise Unsupported("x87 stack underflow")
            if ops[0].type == X.X86_OP_REG:
                j = self.st(ops[0])
                self.fpu[j] = self.fpu[-1]
            else:
                self.emit(k, "OPASSIGN", [self.opnd(k, ops[0], esp, ebp)], [self.fpu[-1]])
            if m == "fstp":
                self.fpu.pop()
            return
        if m in FIOPS:                                    # fimul dword [m]: st0 = st0 * (double)int
            if not self.fpu:
                raise Unsupported("x87 stack underflow")
            c = self.temp(F8)
            self.emit(k, "OPASSIGN", [c], [self.opnd(k, ops[0], esp, ebp).rsplit(":", 1)[0] + ":" +
                                           MTY.get(ops[0].size, "1004")])
            op = FOPS[FIOPS[m]]
            a, b = self.fpu[-1], c
            if op in ("subr", "divr"):
                op, a, b = ("sub" if op == "subr" else "OPDIV"), b, a
            t = self.temp(F8)
            self.emit(k, op, [t], [a, b])
            self.fpu[-1] = t
            return
        if m in FUNARY or m in ("fpatan", "fsincos", "fptan"):
            if not self.fpu or (m == "fpatan" and len(self.fpu) < 2):
                raise Unsupported("x87 stack underflow")
            def lib(fn, args):
                f = "?__lift_%s@@YAXXZ" % fn
                t = self.temp(F8)
                self.emit(k, "OPCALL", [t], [f + ":1004"] + args, "s0=" + f)
                return t
            if m in FUNARY:
                self.fpu[-1] = lib(FUNARY[m], [self.fpu[-1]])
            elif m == "fpatan":                           # st1 = atan2(st1, st0), pop
                t = lib("atan2", [self.fpu[-2], self.fpu[-1]])
                self.fpu.pop()
                self.fpu[-1] = t
            elif m == "fptan":                            # st0 = tan(st0), then push 1.0 (usually popped at once)
                self.fpu[-1] = lib("tan", [self.fpu[-1]])
                self.fpu.append("1:%s" % F4)
            else:                                         # fsincos: st0 = sin, then push cos
                x = self.fpu[-1]
                self.fpu[-1] = lib("sin", [x])
                self.fpu.append(lib("cos", [x]))
            return
        if m in ("fcomi", "fcomip", "fucomi", "fucomip", "fcompi", "fucompi"):
            if not self.fpu:
                raise Unsupported("x87 stack underflow")
            other = self.fpu[self.st(ops[1] if len(ops) > 1 else ops[0])] if ops else self.fpu[-2]
            self.fcmp = (self.fpu[-1], other)
            self.fmask = "fcomi"                          # the flags themselves: the next jcc reads them
            if m.endswith("p"):
                self.fpu.pop()
            return
        base = m[:-1] if m.endswith("p") and m[:-1] in FOPS else m
        if base in FOPS:
            if not ops:                                   # faddp: st1 = st1 op st0, pop
                dst, src = len(self.fpu) - 2, len(self.fpu) - 1
                a, b = self.fpu[dst], self.fpu[src]
            elif len(ops) == 1 and ops[0].type == X.X86_OP_MEM:
                dst = len(self.fpu) - 1
                a, b = self.fpu[dst], self.opnd(k, ops[0], esp, ebp)
            elif len(ops) == 1 and base != m:             # faddp st(i): st(i) = st(i) op st0, pop
                dst, src = self.st(ops[0]), len(self.fpu) - 1
                a, b = self.fpu[dst], self.fpu[src]
            elif len(ops) == 1:                           # fmul st(i) (capstone's `fmul st(0), st(i)`): st0 op= st(i)
                dst, src = len(self.fpu) - 1, self.st(ops[0])
                a, b = self.fpu[dst], self.fpu[src]
            else:                                         # fadd st(a), st(b): st(a) = st(a) op st(b)
                dst, src = self.st(ops[0]), self.st(ops[1])
                a, b = self.fpu[dst], self.fpu[src]
            if dst < 0:
                raise Unsupported("x87 stack underflow")
            op = FOPS[base]
            if op in ("subr", "divr"):
                op, a, b = ("sub" if op == "subr" else "OPDIV"), b, a
            t = self.temp(F8 if any(x.endswith(F8) for x in (a, b)) else F4)
            self.emit(k, op, [t], [a, b])
            self.fpu[dst] = t
            if m.endswith("p") and base != m:
                self.fpu.pop()
            return
        if m in ("fsqrt", "fabs"):
            fn = "?__lift_%s@@YAXXZ" % m[1:]
            t = self.temp(F8)
            self.emit(k, "OPCALL", [t], [fn + ":1004", self.fpu[-1]], "s0=" + fn)
            self.fpu[-1] = t
            return
        if m == "fchs":
            t = self.temp(F4)
            self.emit(k, "neg", [t], [self.fpu[-1]])
            self.fpu[-1] = t
            return
        if m == "fxch":
            j = self.st(ops[-1]) if ops else len(self.fpu) - 2   # capstone: fxch st(1) is [st(0), st(1)]
            self.fpu[-1], self.fpu[j] = self.fpu[j], self.fpu[-1]
            return
        if m in ("fcom", "fcomp", "fcompp", "fucom", "fucomp", "fucompp"):
            if not self.fpu:
                raise Unsupported("x87 stack underflow")
            if ops and ops[0].type == X.X86_OP_MEM:
                other = self.opnd(k, ops[0], esp, ebp)
            else:
                other = self.fpu[self.st(ops[0])] if ops else self.fpu[-2]
            self.fcmp = (self.fpu[-1], other)
            for _ in range(2 if m.endswith("pp") else 1 if m.endswith("p") else 0):
                self.fpu.pop()
            return
        if m in ("fnstsw", "fstsw", "wait", "fwait"):
            return
        if m == "fistp" and ops and ops[0].type == X.X86_OP_MEM and ops[0].size == 8 and \
                os.environ.get("C2_BUILD", "").startswith("15"):
            # `fld dword [f]; fistp qword [r]`: Spore's vetted X87RoundToInt64 (sse_helpers.h), round to nearest
            # into an __int64 (a C cast would call _ftol2, truncating)
            if not self.fpu:
                raise Unsupported("x87 stack underflow")
            x = self.fpu.pop()
            a = self.temp(F4)
            self.emit(k, "OPARG", [a], [x])
            f = "?__lift_X87RoundToInt64@@YAXXZ"
            self.emit(k, "OPCALL", [self.dst(k, ops[0], esp, ebp)], [f + ":1004", a], "s0=" + f)
            return
        raise Unsupported("x87 %s" % m)

    def lea(self, k, ops, esp, ebp):
        """`lea d, [b + i*s + D]` as IL arithmetic: a shift for the scale, then adds (lift.md: a lea that is not a
        frame address is an add). A frame slot's address is IL's `s0=&` operand (an address-taken local)."""
        d = self.dst(k, ops[0], esp, ebp)
        m = ops[1].mem
        base = self.md.reg_name(m.base) if m.base else None
        idx = self.md.reg_name(m.index) if m.index else None
        if (base == "esp" and not idx) or (base == "ebp" and not idx and ebp is not None):
            slot = self.frame_name((esp if base == "esp" else ebp) + m.disp, "1004")
            self.emit(k, "OPASSIGN", [d], [slot], "s0=&")
            return
        if idx and (base == "esp" or (base == "ebp" and ebp is not None)):
            o = (esp if base == "esp" else ebp) + m.disp     # `lea eax, [esp + esi*4 + 0x2c]`: &types[i]
            if o >= 0:
                raise Unsupported("an indexed argument")
            t = self.temp()
            self.emit(k, "OPASSIGN", [t], [self.frame_name(o, "1004")], "s0=&")
            self.indexed.setdefault(o, m.scale)
            iv = "%s:1004" % self.var_read(k, full(idx))
            if m.scale > 1:
                t2 = self.temp()
                self.emit(k, "shl", [t2], [iv, "%d:1004" % {2: 1, 4: 2, 8: 3}[m.scale]])
                iv = t2
            self.emit(k, "add", [d], [t, iv])
            return
        terms = []
        if base:
            terms.append("%s:1004" % self.var_read(k, full(base)))
        if idx:
            iv = "%s:1004" % self.var_read(k, full(idx))
            if m.scale > 1:
                t = self.temp()
                self.emit(k, "shl", [t], [iv, "%d:1004" % {2: 1, 4: 2, 8: 3}[m.scale]])
                iv = t
            terms.append(iv)
        if m.disp:
            v = m.disp & 0xFFFFFFFF
            i = self.ins[k]
            off = i.address + i.size - 4 - self.va
            if self.mask is not None and off in self.mask and getattr(i, "disp_size", 4) == 4 and \
                    0x400000 <= v < 0x7fffffff:
                terms.append("?ga_%08x@@3HA:1004" % v)             # a relocated displacement: an address
            else:
                terms.append("%d:1004" % (m.disp))
        if not terms:
            terms = ["0:1004"]
        if len(terms) == 1:
            self.emit(k, "OPASSIGN", [d], terms)
            return
        acc = terms[0]
        for n, t in enumerate(terms[1:]):
            out = d if n == len(terms) - 2 else self.temp()
            self.emit(k, "add", [out], [acc, t])
            acc = out

    def find_tls(self):
        """`mov a, [_tls_index]; mov b, fs:[0x2c]; mov c, [b + a*4]`: c is this thread's TLS block; `[c+D]` is the
        __declspec(thread) variable at D (a SECREL displacement)."""
        self.tls_base = set()                 # instruction indices whose written register is a TLS block base
        ins = self.ins
        for k in range(len(ins) - 1):
            i = ins[k]
            if not (i.mnemonic == "mov" and "fs:[0x2c]" in i.op_str):
                continue
            b = i.op_str.split(",")[0]
            j = next((q for q in range(k + 1, min(k + 4, len(ins))) if ins[q].mnemonic == "mov" and
                      ins[q].operands[1].type == X.X86_OP_MEM and ins[q].operands[1].mem.base and
                      self.md.reg_name(ins[q].operands[1].mem.base) == b and ins[q].operands[1].mem.scale == 4), None)
            if j is None:
                continue
            a = self.md.reg_name(ins[j].operands[1].mem.index)
            q = next((q for q in range(max(0, k - 3), j) if ins[q].mnemonic == "mov" and
                      ins[q].op_str.startswith(a + ", dword ptr [0x")), None)
            if q is None:
                continue
            self.skip.update({q, k})
            self.tls_base.add(j)
            self.skip.add(j)

    def find_divs(self):
        """cl's division by a constant, found before the walk: `mov eax, M; imul x` (signed) or `mul x` (unsigned),
        then the fix-ups on edx: [add / sub edx, x;] [sar / shr edx, S;] and for a signed quotient `mov t, edx;
        shr t, 31; add t, edx` (or `add edx, t`). cl's scheduler may put unrelated instructions between them.
        D is recovered from M, the fix-up and S and checked on sample dividends (D < 0 too: cl divides by -D and
        negates in one step). -> {index of the mul: (operand x, D, signed, index of the last step, result reg)};
        the mov and the fix-ups go in self.skip."""
        out = {}
        blk = {}
        for n, b in enumerate(self.bl):
            for k in b:
                blk[k] = n
        ins = self.ins
        for k, i in enumerate(ins):
            if i.mnemonic not in ("imul", "mul") or len(i.operands) != 1:
                continue
            defs = self.reach_defs.get((k, "eax"), set())
            if len(defs) != 1:
                continue
            (km, _), = defs
            if not isinstance(km, int) or blk.get(km) != blk.get(k):
                continue
            mi = ins[km]
            if not (mi.mnemonic == "mov" and mi.op_str.startswith("eax, ") and mi.operands[1].type == X.X86_OP_IMM):
                continue
            M = mi.operands[1].imm & 0xFFFFFFFF
            xop = i.operands[0]
            src = full(self.md.reg_name(xop.reg)) if xop.type == X.X86_OP_REG else None
            if src == "eax":
                continue
            if src == "edx":
                src = None                                # `imul edx`: x is gone after it (a fix-up reads a copy)
            signed = i.mnemonic == "imul"
            watch = {"edx"}                               # eax (the low half) and x may be reused by other code
            steps = []                                    # the chain, in order, skipping unrelated instructions
            j, t, fix, S, dreg = k + 1, None, 0, 0, "edx"
            want = ["fix", "shift", "mov", "shr31", "add"] if signed else ["fix", "shift"]
            while j < len(ins) and blk.get(j) == blk.get(k) and want:
                x = ins[j]
                o = x.op_str
                rd, wr = self.acc[j]
                if want[0] == "fix" and x.mnemonic in ("add", "sub") and o.startswith("edx, ") and \
                        x.operands[1].type in (X.X86_OP_REG, X.X86_OP_MEM) and (o == "edx, " + src if src else
                                                                                x.operands[1].type == X.X86_OP_MEM):
                    fix, steps = (1 if x.mnemonic == "add" else -1), steps + [j]
                    want = want[1:]
                elif want[0] in ("fix", "shift") and x.mnemonic == ("sar" if signed else "shr") and \
                        o.startswith((t if (not signed and t) else "edx") + ", ") and x.operands[1].type == X.X86_OP_IMM:
                    S, steps, want = x.operands[1].imm, steps + [j], want[want.index("shift") + 1:]
                    if not signed and t:
                        dreg = t
                elif not signed and not t and want and want[0] in ("fix", "shift") and x.mnemonic == "mov" and \
                        o.endswith(", edx") and x.operands[0].type == X.X86_OP_REG:
                    # `mul ecx; mov esi, edx; shr esi, 4`: the quotient moved out of edx before its shift (Spore)
                    t = full(self.md.reg_name(x.operands[0].reg))
                    watch.add(t)
                    steps = steps + [j]
                elif signed and want[0] in ("fix", "shift", "mov") and x.mnemonic == "mov" and o.endswith(", edx") \
                        and x.operands[0].type == X.X86_OP_REG:
                    t = full(self.md.reg_name(x.operands[0].reg))
                    watch.add(t)
                    steps, want = steps + [j], want[want.index("mov") + 1:]
                elif signed and want[0] == "shr31" and x.mnemonic == "shr" and o == t + ", 0x1f":
                    steps, want = steps + [j], want[1:]
                elif signed and want[0] == "add" and x.mnemonic == "add" and \
                        sorted(o.replace(" ", "").split(",")) == sorted([t, "edx"]):
                    dreg = o.split(",")[0]
                    steps, want = steps + [j], []
                elif x.mnemonic in ("call", "ret") or x.mnemonic.startswith("j") or (rd | wr) & watch:
                    break                                 # something else touches the chain's registers
                elif src in wr and want and want[0] == "fix":
                    want = want[1:]                       # x overwritten: no fix-up from it
                j += 1
            if signed and want:
                continue
            if not signed and not steps:
                pass                                      # `mul x` with edx used as is: S = 0
            kend = steps[-1] if steps else k
            # the chain's other registers must die: only dreg's value is read later
            chain = {k} | set(steps)
            others = ({"eax", "edx", t} - {dreg, None}) if signed else ({"eax", "edx"} - {dreg})

            def read_outside(d):
                return any(kk not in chain and self.ins[kk].mnemonic != "ret" for kk in self.readers(d))
            if any(read_outside((s2, r)) for s2 in chain for r in others & self.acc[s2][1]):
                continue
            if signed and dreg not in self.acc[kend][1]:
                continue
            if signed:
                Meff = (M - (1 << 32) if M & 0x80000000 else M) + fix * (1 << 32)
                if not Meff:
                    continue
                D = round((1 << (32 + S)) / Meff)

                def q(x):
                    hi = (x * (M - (1 << 32) if M & 0x80000000 else M)) >> 32
                    hi = (hi + fix * x) >> S
                    return hi + (1 if hi < 0 else 0)

                def tdiv(x, d):
                    r = abs(x) // abs(d)
                    return r if (x < 0) == (d < 0) else -r
                samples = (0, 1, abs(D) - 1, abs(D), abs(D) + 1, 12345, 7 * abs(D) + 3, (1 << 31) - 1, -1, -abs(D),
                           -(1 << 31), 99991, -77777, 3 * abs(D) - 1, -(5 * abs(D)) - 1)
                if abs(D) < 2 or any(q(x) != tdiv(x, D) for x in samples):
                    continue
            else:
                if fix:
                    continue
                D = round((1 << (32 + S)) / M) if M else 0
                samples = (0, 1, D - 1, D, D + 1, 12345, 7 * D + 3, (1 << 32) - 1, (1 << 31), 99991, 3 * D - 1)
                if D < 2 or any(((x * M) >> 32 >> S) != x // D for x in samples):
                    continue
            out[k] = (xop, D, signed, kend, dreg)
            self.skip.add(km)
            self.skip.update(steps)
        return out

    # ---- __int64 arithmetic: register / memory pairs
    def mem_pair(self, k1, o1, k2, o2):
        """Are o1 (at k1) and o2 (at k2) the low and high dwords of one 8-byte memory operand?"""
        if o1.type != X.X86_OP_MEM or o2.type != X.X86_OP_MEM or o1.size != 4 or o2.size != 4:
            return False
        m1, m2 = o1.mem, o2.mem
        if (m1.base, m1.index, m1.scale, m1.segment) != (m2.base, m2.index, m2.scale, m2.segment) or \
                m2.disp != m1.disp + 4:
            return False
        for r in (m1.base, m1.index):
            if r and self.md.reg_name(r) not in ("esp", "ebp"):
                n = full(self.md.reg_name(r))
                if self.reach_defs.get((k1, n)) != self.reach_defs.get((k2, n)):
                    return False
        return True

    def const_of(self, k, op):
        """The 32-bit value of an immediate, or of a register whose one definition is `mov r, imm` / `xor r, r`."""
        if op.type == X.X86_OP_IMM:
            return op.imm & 0xFFFFFFFF
        if op.type != X.X86_OP_REG:
            return None
        d = self.one_def(k, full(self.md.reg_name(op.reg)))
        if d is None:
            return None
        i = self.ins[d]
        if i.mnemonic == "mov" and i.operands[1].type == X.X86_OP_IMM and i.operands[0].size == 4:
            return i.operands[1].imm & 0xFFFFFFFF
        if i.mnemonic == "xor" and i.operands[1].type == X.X86_OP_REG and i.operands[0].reg == i.operands[1].reg:
            return 0
        return None

    def find_strlens(self):
        """cl's strlen intrinsic: `lea d, [p+1]; L: mov c8, byte [p]; inc p (add p, 1); test c8, c8; jne L;
        sub p, d` -> p = strlen(p). -> {index of the sub: (index of the lea, p)}; the lea and the loop go in
        self.skip (the loop's label stays, unused). Refused when d, the 0 left in c8 or the pointer past the
        terminator is read anywhere else."""
        out = {}
        ins = self.ins
        for b in self.bl:
            if len(b) != 4:
                continue
            k0 = b[0]
            i0, i1, i2, i3 = (ins[k] for k in b)
            if not (i0.mnemonic == "mov" and len(i0.operands) == 2 and i0.operands[0].type == X.X86_OP_REG and
                    i0.operands[0].size == 1 and i0.operands[1].type == X.X86_OP_MEM and i0.operands[1].size == 1):
                continue
            mm = i0.operands[1].mem
            if not mm.base or mm.index or mm.disp:
                continue
            p, c = self.md.reg_name(mm.base), self.md.reg_name(i0.operands[0].reg)
            if not ((i1.mnemonic == "inc" and i1.op_str == p) or (i1.mnemonic == "add" and i1.op_str == p + ", 1")) \
                    or i2.mnemonic != "test" or i2.op_str != "%s, %s" % (c, c) or i3.mnemonic != "jne" or \
                    i3.operands[0].type != X.X86_OP_IMM or i3.operands[0].imm != i0.address or b[-1] + 1 >= len(ins):
                continue
            ks = b[-1] + 1
            sb = ins[ks]
            if sb.mnemonic != "sub" or not sb.op_str.startswith(p + ", ") or sb.operands[1].type != X.X86_OP_REG:
                continue
            dreg = self.md.reg_name(sb.operands[1].reg)
            kl = self.one_def(ks, dreg)
            if kl is None or kl >= k0 or ins[kl].mnemonic != "lea" or ins[kl].op_str != "%s, [%s + 1]" % (dreg, p):
                continue
            if any(p in self.regs(ins[j])[1] for j in range(kl + 1, k0)):     # p moves between the lea and the loop
                continue
            if set(self.readers((kl, dreg))) != {ks} or [x for x in self.readers((k0, full(c))) if x != b[2]] or \
                    [x for x in self.readers((b[1], p)) if x not in (k0, b[1], ks)]:
                continue
            out[ks] = (kl, p)
            self.skip.update([kl] + b)
        return out

    def readers(self, d):
        """Instructions that read the value defined at d = (index, reg); a partial write (`fnstsw ax`,
        `mov al, 1`) keeps the rest of the register but does not read the value."""
        r = d[1]
        out = []
        for (kk, rr), ds in self.reach_defs.items():
            if rr == r and d in ds:
                rd = {full(self.md.reg_name(x)) for x in self.ins[kk].regs_access()[0]}
                # a call's ecx read only asks whether it is a __thiscall: a sub / sbb result in ecx is no `this`
                if r in rd or self.ins[kk].mnemonic == "ret" or (r != "ecx" and (
                        self.ins[kk].mnemonic == "call" or self.is_tailjmp(self.ins[kk]))):
                    out.append(kk)
        return out

    def one_def(self, k, r):
        ds = self.reach_defs.get((k, r), set())
        if len(ds) != 1:
            return None
        (d, _), = ds
        return d if isinstance(d, int) else None

    def pair_value(self, kl, ol, kh, oh, used, depth=0):
        """The 64-bit value of the pair (ol at kl, oh at kh): ("mem", k, op) for an 8-byte memory operand (read at
        k), ("imm", v), ("sx", k, op) / ("zx", k, op) for a 32-bit value sign- / zero-extended. Registers loaded
        for it go in used (index, reg)."""
        cl_, ch_ = self.const_of(kl, ol), self.const_of(kh, oh)
        if cl_ is not None and ch_ is not None:          # constants, also kept in registers (`xor ebx, ebx`)
            v = (ch_ << 32) | cl_
            return ("imm", v - (1 << 64) if v >> 63 else v)
        if ch_ == 0 and ol.type in (X.X86_OP_REG, X.X86_OP_MEM) and oh.type == X.X86_OP_REG:
            return ("zx", kl, ol)
        if self.mem_pair(kl, ol, kh, oh):
            return ("mem", kl, ol)
        if oh.type == X.X86_OP_IMM and oh.imm & 0xFFFFFFFF == 0 and ol.type in (X.X86_OP_REG, X.X86_OP_MEM):
            return ("zx", kl, ol)
        if (ol.type == X.X86_OP_REG) != (oh.type == X.X86_OP_REG) and X.X86_OP_IMM not in (ol.type, oh.type):
            # one half loaded into a register first: `mov edx, [m]; ...; sub ecx, edx; sbb eax, [m+4]`
            kr, orr = (kl, ol) if ol.type == X.X86_OP_REG else (kh, oh)
            r = full(self.md.reg_name(orr.reg))
            d = self.one_def(kr, r)
            if d is None or self.ins[d].mnemonic != "mov" or self.ins[d].operands[1].type != X.X86_OP_MEM:
                return None
            ml = (d, self.ins[d].operands[1]) if orr is ol else (kl, ol)
            mh = (d, self.ins[d].operands[1]) if orr is oh else (kh, oh)
            if not self.mem_pair(ml[0], ml[1], mh[0], mh[1]):
                return None
            used.append((d, r))
            return ("mem", ml[0], ml[1])
        if ol.type == X.X86_OP_REG and oh.type == X.X86_OP_REG:
            rl, rh = full(self.md.reg_name(ol.reg)), full(self.md.reg_name(oh.reg))
            dh = self.one_def(kh, rh)
            if dh is not None and self.ins[dh].mnemonic == "cdq" and rl == "eax" and rh == "edx" and \
                    self.reach_defs.get((dh, "eax")) == self.reach_defs.get((kl, "eax")):
                return ("sx", kl, ol)
            dl, dh = self.one_def(kl, rl), self.one_def(kh, rh)
            if dl is None or dh is None:
                return None
            if (dl, dh) in getattr(self, "preg", {}):    # an __int64 add / sub kept in registers (find_i64)
                return ("ptemp", self.preg[(dl, dh)])
            il, ih = self.ins[dl], self.ins[dh]
            if il.mnemonic == ih.mnemonic == "mov" and il.operands[1].type == ih.operands[1].type == X.X86_OP_REG \
                    and depth < 4:                       # a copy of a pair: `mov edi, eax; mov esi, edx`
                u2 = []
                v = self.pair_value(dl, il.operands[1], dh, ih.operands[1], u2, depth + 1)
                if v is not None and v[0] in ("call", "ptemp", "mem"):
                    used += [(dl, rl), (dh, rh)] + u2
                    return v
            if il.mnemonic == ih.mnemonic == "mov" and self.mem_pair(dl, il.operands[1], dh, ih.operands[1]):
                used += [(dl, rl), (dh, rh)]
                return ("mem", dl, il.operands[1])
            if dl == dh and il.mnemonic == "call" and rl == "eax" and rh == "edx":
                return ("call", dl)                       # an __int64 returned in edx:eax (`_allmul`, a callee)
            if ih.mnemonic == "cdq" and rl == "eax" and rh == "edx" and self.one_def(dh, "eax") == dl:
                return ("sx", kl, ol)
            if ih.mnemonic == "xor" and ih.operands[0].reg == ih.operands[1].reg:
                return ("zx", kl, ol)
            ml, mh = self.origin(kl, ol), self.origin(kh, oh)  # each half copied from one load of a memory pair
            if ml and mh and self.mem_pair(ml[0], ml[1], mh[0], mh[1]) and self.esp_still(ml[0], mh[0]):
                return ("mem", ml[0], ml[1])
        return None

    def pair_regs(self, kl, rl, kh, rh, used, depth=0):
        """pair_value for two registers named, not operands (a ret's edx:eax): a ptemp or a call, through copies."""
        def dof(k, r):
            d = self.one_def(k, r)
            if d is not None or self.ins[k].mnemonic != "ret":
                return d
            blk = next((b for b in self.bl if k in b), [])   # a ret records only eax as read: edx's def in its block
            for q in range(k - 1, blk[0] - 1 if blk else k, -1):
                if r in self.regs(self.ins[q])[1]:
                    return q
            return None
        dl, dh = dof(kl, rl), dof(kh, rh)
        if dl is None or dh is None:
            return None
        if (dl, dh) in getattr(self, "preg", {}):
            return ("ptemp", self.preg[(dl, dh)])
        il, ih = self.ins[dl], self.ins[dh]
        if dl == dh and il.mnemonic == "call" and (rl, rh) == ("eax", "edx"):
            return ("call", dl)
        if il.mnemonic == ih.mnemonic == "mov" and il.operands[1].type == ih.operands[1].type == X.X86_OP_REG and \
                depth < 4:
            u2 = []
            v = self.pair_regs(dl, full(self.md.reg_name(il.operands[1].reg)), dh,
                               full(self.md.reg_name(ih.operands[1].reg)), u2, depth + 1)
            if v is not None:
                used += [(dl, rl), (dh, rh)] + u2
                return v
        return None

    def origin(self, k, op):
        """The memory load a register's value was copied from: (index, operand), through `mov r, r2` copies."""
        for _ in range(4):
            if op.type != X.X86_OP_REG:
                return None
            d = self.one_def(k, full(self.md.reg_name(op.reg)))
            if d is None or self.ins[d].mnemonic != "mov" or self.ins[d].operands[0].size != 4:
                return None
            k, op = d, self.ins[d].operands[1]
            if op.type == X.X86_OP_MEM:
                return (k, op)
        return None

    def esp_still(self, ka, kb):
        """No esp change between instructions ka and kb (one block): an esp-relative pair is one object."""
        a, b = sorted((ka, kb))
        blk = next((x for x in self.bl if a in x), [])
        if b not in blk:
            return all(not (self.ins[x].operands and any(o.type == X.X86_OP_MEM and o.mem.base and
                                                         self.md.reg_name(o.mem.base) == "esp"
                                                         for o in self.ins[x].operands)) for x in (ka, kb))
        return not any(self.ins[x].mnemonic in ("push", "pop", "call") or (self.ins[x].operands and
                       self.ins[x].operands[0].type == X.X86_OP_REG and self.md.reg_name(self.ins[x].operands[0].reg)
                       == "esp") for x in range(a + 1, b))

    def find_pairs(self):
        """cl's __int64 add / sub: `add lo, x; adc hi, y` (`sub` / `sbb`) on a register pair loaded from memory or
        on memory in place, the result stored to memory. -> {index of the last instruction: (op, dst, a, b)};
        the pieces go in self.skip."""
        out = {}
        blk = {k: n for n, b in enumerate(self.bl) for k in b}
        ins = self.ins
        for kh, ih in enumerate(ins):
            if ih.mnemonic not in ("adc", "sbb") or len(ih.operands) != 2:
                continue
            if ih.operands[0].type == X.X86_OP_REG and ih.operands[1].type == X.X86_OP_REG and \
                    ih.operands[0].reg == ih.operands[1].reg:
                continue                                  # `sbb r, r`: a carry mask (setcc)
            kl = kh - 1
            while kl >= 0 and blk.get(kl) == blk.get(kh) and (ins[kl].mnemonic in ("mov", "lea", "push", "pop") or (
                    ins[kl].mnemonic.startswith("f") and not ins[kl].mnemonic.startswith(("fcomi", "fucomi")))):
                kl -= 1                                   # nothing that writes EFLAGS (x87 code keeps them)
            want = "add" if ih.mnemonic == "adc" else "sub"
            if kl < 0 or blk.get(kl) != blk.get(kh) or ins[kl].mnemonic != want or len(ins[kl].operands) != 2:
                continue
            il = ins[kl]
            Lo, Ho = il.operands[0], ih.operands[0]
            used = []
            b = self.pair_value(kl, il.operands[1], kh, ih.operands[1], used)
            if b is None:
                continue
            pieces = [kl, kh]
            if Lo.type == X.X86_OP_MEM:                   # in place: `sub [m], x; sbb [m+4], y`
                if not self.mem_pair(kl, Lo, kh, Ho):
                    continue
                dst = a = ("mem", kl, Lo)
                kend = kh
            else:
                if Ho.type != X.X86_OP_REG:
                    continue
                rl, rh = full(self.md.reg_name(Lo.reg)), full(self.md.reg_name(Ho.reg))
                a = self.pair_value(kl, Lo, kh, Ho, used)
                if a is None or a[0] != "mem":
                    continue
                # the result: read only by `mov [p], lo` and `mov [p+4], hi`
                # (a ret's read of eax is the conservative "maybe returned": a store's register is not the result)
                rdl = [x for x in self.readers((kl, rl)) if x != kh and ins[x].mnemonic != "ret"]
                rdh = [x for x in self.readers((kh, rh)) if ins[x].mnemonic != "ret"]
                if len(rdl) != 1 or len(rdh) != 1:
                    continue
                sl, sh = ins[rdl[0]], ins[rdh[0]]
                if not (sl.mnemonic == sh.mnemonic == "mov" and sl.operands[1].type == sh.operands[1].type ==
                        X.X86_OP_REG and full(self.md.reg_name(sl.operands[1].reg)) == rl and
                        full(self.md.reg_name(sh.operands[1].reg)) == rh and
                        self.mem_pair(rdl[0], sl.operands[0], rdh[0], sh.operands[0])):
                    continue
                if blk.get(rdl[0]) != blk.get(kh) or blk.get(rdh[0]) != blk.get(kh):
                    continue
                dst = ("mem", rdl[0], sl.operands[0])
                kend = max(rdl[0], rdh[0], kh)
                pieces += [rdl[0], rdh[0]]
            # the loaded registers must serve only this operation
            ok = True
            for d, r in used:
                if any(x not in (kl, kh) for x in self.readers((d, r))):
                    ok = False
            if not ok:
                continue
            out[kend] = (ARITH[want], dst, a, b)
            self.call64.update(v[1] for v in (a, b) if v[0] == "call")
            self.skip.update(set(pieces) - {kend} | {d for d, _ in used})
            self.pair_pieces.update(pieces)
            self.pair_end.add(kend)
        return out

    def find_i64(self):
        """__int64 values kept in registers, beyond find_pairs' stored results (cl at /O2, 13.10 and 15.00):
          - `add lo, x; adc hi, y` (sub / sbb) whose result stays in a register pair: one __int64 temp (a ptemp),
            its operands any pair_value (a call's edx:eax, copies of it, memory, cdq, constants, another ptemp);
          - the 64-bit compare `cmp hiA, hiB; jl T / jg F (either order; jb / ja unsigned); cmp loA, loB; jb T`
            (jbe / ja / jae): one `if (A cc B) goto T`, its three blocks reached only through the chain.
        A fold is kept only when every reader of every register it drops is another kept fold (a fixpoint), so a
        half also read as an int keeps the 32-bit lifting. `__int64 s = tm() + k; while (tm() < s);` is Spore
        0094c9d0's loop. -> {k: chain} (index of the hi cmp); the ptemps go into self.pairs."""
        ins = self.ins
        blk = {k: n for n, b in enumerate(self.bl) for k in b}
        preds = collections.defaultdict(set)
        for n, ss in self.succ.items():
            for t in ss:
                preds[t].add(n)
        self.preg = {}
        self.st64 = {}
        self.ret64 = {}
        items = []
        for _round in range(3):                       # a fold can read one found later in the scan
            for kh, ih in enumerate(ins):
                if kh in self.pair_pieces or ih.mnemonic not in ("adc", "sbb") or len(ih.operands) != 2 or \
                        ih.operands[0].type != X.X86_OP_REG:
                    continue
                if ih.operands[1].type == X.X86_OP_REG and ih.operands[0].reg == ih.operands[1].reg:
                    continue
                kl = kh - 1
                while kl >= 0 and blk.get(kl) == blk.get(kh) and ins[kl].mnemonic in ("mov", "lea", "push", "cdq") and \
                        not (ins[kl].operands and ins[kl].operands[0].type == X.X86_OP_REG and
                             ins[kl].operands[0].reg in (ih.operands[0].reg,)):
                    kl -= 1
                want = "add" if ih.mnemonic == "adc" else "sub"
                if kl < 0 or blk.get(kl) != blk.get(kh) or ins[kl].mnemonic != want or len(ins[kl].operands) != 2 or \
                        ins[kl].operands[0].type != X.X86_OP_REG:
                    continue
                il = ins[kl]
                used = []
                b = self.pair_value(kl, il.operands[1], kh, ih.operands[1], used)
                a = self.pair_value(kl, il.operands[0], kh, ih.operands[0], used)
                if a is None or b is None or a[0] == "imm":
                    continue
                if (kl, kh) in self.preg:
                    continue
                tok = self.temp("1008")
                self.preg[(kl, kh)] = tok
                items.append(dict(kind="op", op=ARITH[want], kl=kl, kh=kh, tok=tok, vals=[a, b], used=used,
                                  pieces={kl, kh}, rl=full(self.md.reg_name(il.operands[0].reg)),
                                  rh=full(self.md.reg_name(ih.operands[0].reg))))
            # constant shifts: `shld hi, lo, n; .. shl lo, n` (<<), `shrd lo, hi, n; .. shr / sar hi, n` (>> / signed)
            for k1, i1 in enumerate(ins):
                if i1.mnemonic not in ("shld", "shrd") or len(i1.operands) != 3 or i1.operands[2].type != X.X86_OP_IMM or \
                        i1.operands[0].type != X.X86_OP_REG or i1.operands[1].type != X.X86_OP_REG:
                    continue
                n_ = i1.operands[2].imm & 31
                r0, r1 = (full(self.md.reg_name(o.reg)) for o in i1.operands[:2])     # shld: hi, lo; shrd: lo, hi
                k2 = k1 + 1
                while k2 < len(ins) and blk.get(k2) == blk.get(k1) and not ins[k2].mnemonic.startswith(("j", "call")) \
                        and not ({r1} & (self.regs(ins[k2])[0] | self.regs(ins[k2])[1])):
                    k2 += 1
                if k2 >= len(ins) or blk.get(k2) != blk.get(k1):
                    continue
                i2 = ins[k2]
                want = ("shl",) if i1.mnemonic == "shld" else ("shr", "sar")
                if i2.mnemonic not in want or len(i2.operands) != 2 or i2.operands[0].type != X.X86_OP_REG or \
                        full(self.md.reg_name(i2.operands[0].reg)) != r1 or i2.operands[1].type != X.X86_OP_IMM or \
                        i2.operands[1].imm & 31 != n_:
                    continue
                if self.reach_defs.get((k1, r1)) != self.reach_defs.get((k2, r1)):
                    continue
                used = []
                if i1.mnemonic == "shld":                     # hi = r0, lo = r1; lo's new value at k2, hi's at k1
                    v = self.pair_value(k1, i1.operands[1], k1, i1.operands[0], used)
                    kl, kh, rl, rh = k2, k1, r1, r0
                else:                                         # lo = r0, hi = r1
                    v = self.pair_value(k1, i1.operands[0], k1, i1.operands[1], used)
                    kl, kh, rl, rh = k1, k2, r0, r1
                if v is None:
                    if os.environ.get("I64_DEBUG"):
                        sys.stderr.write("i64: %x %s: no 64-bit value for the input\n" % (i1.address, i1.mnemonic))
                    continue
                op = "shl" if i1.mnemonic == "shld" else i2.mnemonic
                if op == "shr" and v[0] == "sx":
                    continue                                  # (unsigned)(sign-extended) >> n: not this IL's shr
                if (kl, kh) in self.preg:
                    continue
                tok = self.temp("1008")
                self.preg[(kl, kh)] = tok
                items.append(dict(kind="op", op=op, kl=kl, kh=kh, tok=tok, vals=[v, ("imm", n_)], used=used,
                                  pieces={k1, k2}, rl=rl, rh=rh, at=max(k1, k2), shift=True))
            # bitwise pairs: `or lo, x; .. or hi, y` (and / xor), one block, the halves in either order
            for b in self.bl:
                bw = [k for k in b if ins[k].mnemonic in ("or", "and", "xor") and len(ins[k].operands) == 2 and
                      ins[k].operands[0].type == X.X86_OP_REG and ins[k].operands[0].size == 4 and
                      not (ins[k].operands[1].type == X.X86_OP_REG and ins[k].operands[1].reg == ins[k].operands[0].reg)]
                for ka in bw:
                    for kb in bw:
                        if ka == kb or ins[ka].mnemonic != ins[kb].mnemonic:
                            continue
                        lo, hi = ins[ka], ins[kb]
                        rl, rh = (full(self.md.reg_name(x.operands[0].reg)) for x in (lo, hi))
                        if rl == rh or (kb > ka and rl in self.regs(hi)[1] | self.regs(hi)[0] - {rh}) or \
                                (ka > kb and rh in self.regs(lo)[1] | self.regs(lo)[0] - {rl}):
                            continue
                        used = []
                        a = self.pair_value(ka, lo.operands[0], kb, hi.operands[0], used)
                        b_ = self.pair_value(ka, lo.operands[1], kb, hi.operands[1], used) if a is not None else None
                        if a is None or b_ is None or not ({a[0], b_[0]} & {"ptemp", "call", "mem"}):
                            continue
                        if (ka, kb) in self.preg:
                            continue
                        tok = self.temp("1008")
                        self.preg[(ka, kb)] = tok
                        items.append(dict(kind="op", op=lo.mnemonic, kl=ka, kh=kb, tok=tok, vals=[a, b_], used=used,
                                          pieces={ka, kb}, rl=rl, rh=rh, at=max(ka, kb), shift=True))
        # stores of a register pair: `mov [m], lo; .. mov [m+4], hi` (either order, one block) of a ptemp
        for b in self.bl:
            st = [k for k in b if ins[k].mnemonic == "mov" and len(ins[k].operands) == 2 and
                  ins[k].operands[0].type == X.X86_OP_MEM and ins[k].operands[0].size == 4 and
                  ins[k].operands[1].type == X.X86_OP_REG]
            for ka in st:
                for kb in st:
                    if ka == kb or not self.mem_pair(ka, ins[ka].operands[0], kb, ins[kb].operands[0]):
                        continue
                    used = []
                    v = self.pair_value(ka, ins[ka].operands[1], kb, ins[kb].operands[1], used)
                    if v is None or v[0] != "ptemp":
                        continue
                    items.append(dict(kind="st", ka=ka, kb=kb, vals=[v], used=used, pieces={ka, kb}))
        # a ret of edx:eax holding one ptemp (through copies): an __int64 return
        for k, i in enumerate(ins):
            if i.mnemonic != "ret":
                continue
            used = []
            v = self.pair_regs(k, "eax", k, "edx", used)
            if v is not None and v[0] == "ptemp":
                items.append(dict(kind="ret", k=k, vals=[v], used=used, pieces={k}))
        tgt = lambda j: j.operands[0].imm if j.operands and j.operands[0].type == X.X86_OP_IMM else None
        for n, b in enumerate(self.bl):
            if len(b) < 2 or n + 2 >= len(self.bl):
                continue
            k1, c1, j1 = b[-2], ins[b[-2]], ins[b[-1]]
            b2, b3 = self.bl[n + 1], self.bl[n + 2]
            if c1.mnemonic != "cmp" or j1.mnemonic not in ("jl", "jg", "jb", "ja") or len(b2) != 1 or len(b3) != 2:
                continue
            j2, c3, j3 = ins[b2[0]], ins[b3[0]], ins[b3[1]]
            signed = j1.mnemonic in ("jl", "jg")
            if {j1.mnemonic, j2.mnemonic} != ({"jl", "jg"} if signed else {"jb", "ja"}) or c3.mnemonic != "cmp" or \
                    j3.mnemonic not in ("jb", "jae", "jbe", "ja") or preds[n + 1] != {n} or preds[n + 2] != {n + 1}:
                continue
            TL = tgt(j1) if j1.mnemonic in ("jl", "jb") else tgt(j2)
            TG = tgt(j1) if j1.mnemonic in ("jg", "ja") else tgt(j2)
            T3 = tgt(j3)
            F3 = ins[b3[1] + 1].address if b3[1] + 1 < len(ins) else None
            if None in (TL, TG, T3, F3):
                continue
            m3 = j3.mnemonic
            if TL != (T3 if m3 in ("jb", "jbe") else F3) or TG != (T3 if m3 in ("ja", "jae") else F3):
                continue
            used = []
            A = self.pair_value(b3[0], c3.operands[0], k1, c1.operands[0], used)
            B = self.pair_value(b3[0], c3.operands[1], k1, c1.operands[1], used)
            if A is None or B is None:
                continue
            cc = {"jb": "l", "jbe": "le", "ja": "g", "jae": "ge"}[m3] if signed else m3[1:]
            items.append(dict(kind="cmp", k1=k1, cc=cc, T=T3, vals=[A, B], used=used,
                              pieces={k1, b[-1], b2[0], b3[0], b3[1]}))
        for it in items:
            it["ok"] = True
        changed = True
        while changed:
            changed = False
            live = [it for it in items if it["ok"]]
            toks = {it["tok"] for it in live if it["kind"] == "op"}
            covered = lambda its: set().union(*[it["pieces"] | {d for d, _ in it["used"]} for it in its]) if its \
                else set()
            for it in live:
                bad = any(v[0] == "ptemp" and v[1] not in toks for v in it["vals"])
                if not bad:                               # the copies it reads serve only it
                    mine = it["pieces"] | {d for d, _ in it["used"]}
                    bad = any(set(x for x in self.readers((d, r)) if ins[x].mnemonic != "ret") - mine
                              for d, r in it["used"])
                if not bad and it["kind"] == "op":        # its result: read only by folds that take the ptemp
                    users = [u for u in live if ("ptemp", it["tok"]) in u["vals"]]
                    rd = (set(self.readers((it["kl"], it["rl"]))) - {it["kh"]}) | set(self.readers((it["kh"], it["rh"])))
                    rd = {x for x in rd if ins[x].mnemonic != "ret" or x in covered(users)}
                    other = rd - covered(users)
                    # other readers take the halves split back out (`lo = (int)t`, `hi = (int)(t >> 32)`: cl
                    # compiles a split to the registers, unlike a join): for a shift (refused otherwise) or a fold
                    # that has a 64-bit user
                    em = max(it["pieces"])
                    if other and (users or it.get("shift")) and all(x > em or blk.get(x) != blk.get(em)
                                                                    for x in other):
                        it["split"] = (bool(set(self.readers((it["kl"], it["rl"]))) - {it["kh"]} - covered(users)),
                                       bool(set(self.readers((it["kh"], it["rh"]))) - covered(users)))
                        bad = False
                    else:
                        it.pop("split", None)
                        bad = not users or bool(other)
                if not bad:                               # a call's edx:eax: read only by folds
                    for v in it["vals"]:
                        if v[0] == "call":
                            users = [u for u in live if v in u["vals"]]
                            rd = set(self.readers((v[1], "eax"))) | set(self.readers((v[1], "edx")))
                            rd = {x for x in rd if ins[x].mnemonic not in ("ret", "call")}
                            if rd - covered(users):
                                bad = True
                if bad:
                    if os.environ.get("I64_DEBUG"):
                        sys.stderr.write("i64: drop %s at %x\n" % (it["kind"], ins[min(it["pieces"])].address))
                    it["ok"] = False
                    changed = True
        chains = {}
        self.split64 = {}
        for it in items:
            if it["ok"] and it.get("split"):
                self.split64[max(it["pieces"]) if it.get("shift") else it["kh"]] = (
                    it["tok"], it["kl"], it["rl"], it["kh"], it["rh"]) + it["split"]
            if not it["ok"]:
                if it["kind"] == "op":
                    self.preg.pop((it["kl"], it["kh"]), None)
                continue
            self.skip.update({d for d, _ in it["used"]})
            self.call64.update(v[1] for v in it["vals"] if v[0] == "call")
            if it["kind"] == "op" and it.get("shift"):
                self.pairs[it["at"]] = (it["op"], ("ptemp", it["tok"]), it["vals"][0], it["vals"][1])
                self.skip.update(it["pieces"])
                self.pair_pieces.update(it["pieces"])
            elif it["kind"] == "op":
                self.pairs[it["kh"]] = (it["op"], ("ptemp", it["tok"]), it["vals"][0], it["vals"][1])
                self.skip.add(it["kl"])
                self.pair_pieces.update(it["pieces"])
            elif it["kind"] == "ret":
                self.ret64[it["k"]] = it["vals"][0][1]
            elif it["kind"] == "st":
                ka, kb = it["ka"], it["kb"]
                self.st64[max(ka, kb)] = (("mem", ka, ins[ka].operands[0]), it["vals"][0])
                self.skip.update(it["pieces"])
                self.pair_pieces.update(it["pieces"])
            else:
                chains[it["k1"]] = it
                self.skip.update(it["pieces"] - {it["k1"]})
        return chains

    def pair_tok(self, v):
        if v[0] == "ptemp":
            return v[1]
        if v[0] == "imm":
            return "%d:1008" % v[1]
        if v[0] == "call":
            return "%s:1008" % self.var_written(v[1], "eax")
        k, op = v[1], v[2]
        esp, ebp = self.at[k]
        t = self.opnd(k, op, esp, ebp)
        base, ty = t.rsplit(":", 1)
        if v[0] == "mem":
            return base + ":1008"
        return base + (":1004" if v[0] == "sx" else ":2004")

    def frame_offsets(self):
        """Entry-relative offsets of the frame slots the lifted code names (`_lN` is -N)."""
        return {-int(m) for l in self.lines for m in re.findall(r"\b_l(\d+):", l)}

    def cleans_before_next_call(self, k):
        """An `add esp, N` / `pop ecx` cleanup after the call at k and before the next call, branch or ret (cl may
        schedule a `mov esi, eax` in between), not the epilogue's: this call's own arguments popped by the caller.
        Unlike caller_cleans it does not look past later calls (their merged cleanup is not this call's)."""
        pro = getattr(self, "pro", set())
        self.caller_cleans(k)                       # sets self.frame_alloc (the prologue's reservation)
        for q in range(k + 1, len(self.ins)):
            i = self.ins[q]
            if i.mnemonic in ("call", "ret") or i.mnemonic.startswith("j"):
                return False
            if q in self.eh_ins or (self.eh and q in pro):
                continue                            # the EH frame's unregistration / teardown
            if q in pro and not (i.mnemonic == "add" and i.op_str.startswith("esp, ") and
                                 i.operands[1].type == X.X86_OP_IMM and i.operands[1].imm > self.frame_alloc):
                continue                            # the epilogue's own teardown (an add larger is a cleanup)
            if (i.mnemonic == "add" and i.op_str.startswith("esp, ")) or (i.mnemonic == "pop" and
                                                                          i.op_str in ("ecx", "edx")):
                return True
        return False

    def caller_cleans(self, k):
        """Does the caller pop this call's arguments? Yes when an `add esp, N` / `pop ecx` (not the epilogue's)
        follows before the next branch or ret: cl merges the cleanup of consecutive cdecl calls
        (`push; call a; push; call b; add esp, 8`), so intervening calls do not end the search."""
        pro = getattr(self, "pro", set())
        if not hasattr(self, "frame_alloc"):       # bytes the prologue reserves (sub esp, a `push ecx` slot)
            self.frame_alloc = sum(i.operands[1].imm if i.mnemonic == "sub" else 4 for q, i in enumerate(self.ins)
                                   if q in pro and q < (self.first_flow() if hasattr(self, "first_flow") else len(self.ins))
                                   and ((i.mnemonic == "sub" and i.op_str.startswith("esp, ")) or
                                        (i.mnemonic == "push" and i.op_str in ("ecx", "eax", "edx"))))
        op = self.ins[k].operands[0] if self.ins[k].operands else None
        imp = op is not None and op.type == X.X86_OP_MEM and not op.mem.base and not op.mem.index
        for j in range(k + 1, len(self.ins)):
            i = self.ins[j]
            m = i.mnemonic
            if imp and m == "call":
                return False                       # `call [import]`: its cleanup would come before the next call
            if j in pro:
                if m == "add" and i.op_str.startswith("esp, ") and i.operands[1].imm > self.frame_alloc:
                    return True                    # the epilogue's `add esp` pops this call's arguments too
                continue
            if (m == "add" and i.op_str.startswith("esp, ")) or (m == "pop" and i.op_str in ("ecx", "edx", "eax")):
                return True
            if m == "ret" or m.startswith("j"):
                return False
        return False

    def bv_ctor(self, k, esp):
        """`sub esp, N; mov ecx, esp; push src; call C`: C copy-constructs a class argument passed by value in the
        outgoing area. The N bytes become one argument (src's address, the class FUN_<C>); no call is emitted."""
        i = self.ins[k]
        if not i.operands or i.operands[0].type != X.X86_OP_IMM:
            return False
        ds = self.reach_defs.get((k, "ecx"), set())
        if len(ds) != 1:
            return False
        d = next(iter(ds))[0]
        if not (isinstance(d, int) and d in self.esp_alias and d >= 1):
            return False
        p = None
        for q in range(d - 1, max(-1, d - 5), -1):     # cl may schedule a `lea` / `mov` in between
            c = self.ins[q]
            if c.mnemonic == "sub" and c.op_str.startswith("esp, ") and c.operands[1].type == X.X86_OP_IMM:
                p = c
                break
            if c.mnemonic in ("push", "pop", "call") or c.mnemonic.startswith("j") or "esp" in c.op_str.split(",")[0]:
                return False
        if p is None:
            return False
        n, a = p.operands[1].imm, self.esp_alias[d]
        if esp not in self.out_args or a != esp + 4 or n % 4:
            return False
        src, _ = self.out_args.pop(esp)
        t = self.temp()
        self.lines.append("I %x OPASSIGN 0 0 | %s <- %s" % (i.address, t, src))
        self.out_args[a] = (t, n)
        self.bvctor[a] = (i.operands[0].imm & 0xFFFFFFFF, n)
        return True

    def take_args(self, esp):
        """The call's stack arguments: the slots from esp up to where argument setup began, in address order,
        each written by a push or by a store (`fstp [esp+4]`; an 8-byte store is one argument)."""
        args, offs = [], []
        if self.out_top is not None:
            o = esp
            while o < self.out_top:
                if o not in self.out_args:
                    raise Unsupported("an argument slot the code does not write (esp%+d)" % (o - esp))
                tok, n = self.out_args[o]
                args.append(tok)
                offs.append(o)
                o += n
        # a struct passed by value (`sub esp, 12; mov eax, esp; mov [eax], ..; mov [eax+4], ..`): a run of dwords
        # stored through the esp copy, each loaded from consecutive offsets of one base, is `*(S12*)(base + off)`
        self.byval = []
        for q, o in enumerate(offs):
            if o in self.bv_block:
                self.byval.append((q, None, 0, self.bv_block.pop(o)))   # copied in by rep movsd: args[q] is the source
            if o in self.bvctor:
                cv, n = self.bvctor.pop(o)
                self.byval.append((q, None, cv, -n))        # a class with a copy constructor (FUN_<cv>)
        i = 0
        while i < len(args):
            j = i
            while j < len(args) and offs[j] in self.alias_written and args[j].endswith(":1004"):
                j += 1
            if j - i >= 2:
                src = [self.load_src.get(args[q].rsplit(":", 1)[0]) for q in range(i, j)]
                if all(src) and len({b for b, _ in src}) == 1 and \
                        all(src[q][1] == src[0][1] + 4 * q for q in range(len(src))):
                    self.byval.append((i, src[0][0], src[0][1], 4 * (j - i)))
            i = max(j, i + 1)
        if any(n > 0 for *_r, n in self.byval):
            for i, b, disp, n in reversed([x for x in self.byval if x[3] > 0 and x[1] is not None]):
                t = self.temp()
                self.lines.append("I 0 add 0 0 | %s <- %s:1004 %d:1004" % (t, b, disp))
                args[i:i + n // 4] = [t]
        self.alias_written = set()
        self.out_args, self.out_top = {}, None
        return args

    def written_before(self, k):
        """Registers written by some instruction before index k (in address order: a conservative 'maybe')."""
        out = set()
        for j in range(k):
            out |= self.acc[j][1]
        return out

    def used_after(self, d, k=None, reg=None):
        """Is the value defined at d (insn index, reg) read anywhere? With k/reg: is reg's value at k defined
        inside the function (not the caller's leftover eax)?"""
        if d is None:
            # eax at a ret is the return value when some instruction of the function wrote it, except a call's
            # clobber: `call f; ret` leaves f's result there, which a void source does too (0056b620, 00c8c950)
            defs = self.reach_defs.get((k, reg), set())
            def arg_only(j):                 # `mov eax, x; push eax`: loaded to be pushed as an argument
                nx = self.ins[j + 1] if j + 1 < len(self.ins) else None
                return nx is not None and nx.mnemonic == "push" and nx.op_str == reg
            def zero_for_stores(j):          # EH units: `xor eax, eax` that zeroes fields (`mov [esi+4], eax`)
                i = self.ins[j]
                if not self.eh or i.mnemonic != "xor" or i.op_str != "%s, %s" % (reg, reg):
                    return False
                return any(u.mnemonic == "mov" and u.operands[0].type == X.X86_OP_MEM and
                           u.op_str.endswith(", " + reg) for u in self.ins[j + 1:j + 8])
            # `fnstsw ax` / `lahf`: a float compare's status word, never a return value (M&B 0048e5d0)
            return any(x[0] not in ("entry", "undef") and self.ins[x[0]].mnemonic not in ("call", "fnstsw", "lahf")
                       and not arg_only(x[0])
                       and not zero_for_stores(x[0]) and x[0] not in self.pair_pieces for x in defs)   # (folded
            #                                                         into an __int64 store: not a return value)
        root = self.find(d)
        return any(self.find(v) == root for (kk, rr), v in self.reach.items() if rr == d[1])

    def prologue(self):
        """Indices of the frame setup and the callee-saved pushes / pops around it (they leave no tuples)."""
        out = set()
        # the frame setup: everything before the first call or branch that only saves registers or makes room.
        # cl interleaves the callee-saved pushes with the body (`push ebx; push esi; mov esi, [ebp+8]; ...; push
        # edi`), and an aligned frame has `and esp, -8` (M&B 00403b40), so the setup is not one leading run.
        ck = self.chkstk[0] if self.chkstk else -1      # the stack probe is frame setup, not the first call
        if self.chkstk:
            out |= {0, ck}
        first_flow = next((k for k, i in enumerate(self.ins) if (i.mnemonic == "call" and k != ck) or i.mnemonic.startswith("j")
                           or i.mnemonic == "ret"), len(self.ins))
        restored = set()                            # registers an epilogue pops before a ret
        for k, i in enumerate(self.ins):
            if i.mnemonic == "ret":
                j = k - 1
                while j >= 0 and (self.ins[j].mnemonic in ("pop", "add", "mov", "leave") or j in self.eh_ins):
                    if self.ins[j].mnemonic == "pop":
                        restored.add(self.ins[j].op_str)
                    j -= 1
        out |= self.eh_ins
        # `add esp, N` in an epilogue (before a ret, among the pops): a `sub esp, N` before the first call or branch
        # is the frame, wherever cl scheduled it (`mov ecx, [ecx]; sub esp, 0x200`: M&B 0049ccc0)
        frame_adds = set()
        for k, i in enumerate(self.ins):
            if i.mnemonic == "ret":
                j = k - 1
                while j >= 0 and self.ins[j].mnemonic in ("pop", "add", "mov", "leave"):
                    if self.ins[j].mnemonic == "add" and self.ins[j].op_str.startswith("esp, "):
                        frame_adds.add(self.ins[j].op_str[5:])
                    j -= 1
        lead, aligned = True, False
        saved, written = set(), set()
        for k, i in enumerate(self.ins[:first_flow]):
            m, o = i.mnemonic, i.op_str
            if k in self.eh_ins:
                continue
            if m == "push" and o in CALLEE_SAVED and o in restored and (
                    (o not in saved and o not in written) or (o == "ebp" and aligned)):   # an aligned frame saves
                #                                                                         ebp again after `and esp`
                saved.add(o)
                out.add(k)                          # saved here (once, before any write), restored before the ret
                continue
            if i.operands and i.operands[0].type == X.X86_OP_REG and m not in ("push", "cmp", "test"):
                written.add(full(self.md.reg_name(i.operands[0].reg)))
            if False:
                pass
            elif m == "push" and o in ("ecx", "eax", "edx") and lead:
                out.add(k)                          # `push ecx` at entry: a local slot reserved, not an argument
            elif m == "mov" and o == "ebp, esp":
                out.add(k)
            elif m == "sub" and o.startswith("esp, ") and (lead or any(
                    self.ins[q].mnemonic == "push" for q in range(k + 1, first_flow)) or o[5:] in frame_adds):
                out.add(k)                          # also a late one: pushes follow before the first call, so
                #                                     the room is the frame (a local passed by address), not arguments
            elif m == "and" and o.startswith("esp, "):
                aligned = True
                out.add(k)                          # an aligned frame: offsets stay relative to the aligned esp
            else:
                lead = False
        # a shrink-wrapped save: cl pushes a callee-saved register only on the path that needs it (`jp L; ...;
        # ret; L: push esi`). A push past the first branch of a register still holding the caller's value, popped
        # before a ret, is a save, not an argument
        # The pop need not be in the run before the ret: when the epilogue calls an inlined destructor first, cl pops
        # the registers only the body used before that call (`pop edi; mov [esp+N], -1; pop ebx; ...; call;
        # pop esi; pop ebp; ret`: Spore 0068f0b0); such a pop goes with its push
        for k in range(first_flow, len(self.ins)):
            i = self.ins[k]
            if i.mnemonic == "push" and i.op_str in CALLEE_SAVED and i.op_str not in saved:
                ds = self.reach_defs.get((k, i.op_str), set())
                if not (ds and all(d[0] == "entry" for d in ds)):
                    continue
                if i.op_str in restored:
                    out.add(k)
                    continue
                later = [q for q in range(k + 1, len(self.ins))
                         if self.ins[q].mnemonic == "pop" and self.ins[q].op_str == i.op_str]
                if later and not any(self.ins[q].mnemonic == "ret" for q in range(k + 1, later[0])) and \
                        not self.consumed(k, later[0]):
                    out.add(k)
                    out.add(later[0])
        for k, i in enumerate(self.ins):
            if i.mnemonic != "ret":
                continue
            j = k - 1
            while j >= 0 and (j in self.eh_ins or (self.ins[j].mnemonic == "pop" and self.ins[j].op_str in CALLEE_SAVED) or
                              self.ins[j].op_str in ("esp, ebp",) or self.ins[j].mnemonic == "leave" or
                              (self.ins[j].mnemonic == "add" and self.ins[j].op_str.startswith("esp, "))):
                out.add(j)
                j -= 1
        return out

    def consumed(self, k, q):
        """Does code between the push at k and the pop at q take the pushed dword off the stack: a cdecl call's
        `add esp, N` (or pops) reaching below it (the push was an argument, the later pop an unrelated one)?"""
        depth = 4                                    # bytes on the stack from the push at k up
        for j in range(k + 1, q):
            i = self.ins[j]
            if i.mnemonic == "push":
                depth += 4
            elif i.mnemonic == "pop":
                depth -= 4
            elif i.mnemonic in ("add", "sub") and i.op_str.startswith("esp, ") and i.operands[1].type == X.X86_OP_IMM:
                depth += i.operands[1].imm if i.mnemonic == "sub" else -i.operands[1].imm
            if depth <= 0:
                return True
        return False

    def stack_effect(self, i, esp, ebp):
        m, o = i.mnemonic, i.op_str
        if self.chkstk and i.address == self.ins[self.chkstk[0]].address:
            return esp - self.chkstk[1], ebp
        if m == "push":
            return esp - 4, ebp
        if m == "pop":
            return esp + 4, ebp
        if m == "mov" and o == "ebp, esp":
            return esp, esp
        if m == "sub" and o.startswith("esp, "):
            return esp - i.operands[1].imm, ebp
        if m == "add" and o.startswith("esp, "):
            return esp + i.operands[1].imm, ebp
        return esp, ebp                             # `and esp, -8`: no change to the offsets


def lift(img, va, size, mask=None):
    return Lifter(img, va, size, mask).run()


# --------------------------------------------------------------------------------------------------------- check

def _check_one(args):
    corpus_head, r = args
    import il2c, lift as L
    from w8_emit import Image
    os.environ["C2_BUILD"] = corpus_head["c2"]
    va = int(r["va"], 16)
    res = dict(va=r["va"], size=r["size"])
    try:
        img = Image(corpus_head["image"])
        import corpus as corpusmod
        mine = lift(img, va, r["size"], corpusmod.unpack(r["mask"]))
    except Unsupported as e:
        res["status"] = "unsupported: %s" % e
        return res
    except Exception as e:
        res["status"] = "error: %s" % (str(e)[:120])
        return res
    try:
        flags = corpus_head["flags"].split() + corpus_head.get("src_inc", []) + ["/c"]
        real = il2c.trace_il(r["src"], r["sym"], flags, None)
    except Exception as e:
        res["status"] = "no real IL: %s" % (str(e)[:100])
        return res
    a = [x[1] for x in L.lift_il(mine)]
    b = [x[1] for x in L.lift_il(real)]
    pairs = L.match(a, b)
    res.update(status="ok", ours=len(a), real=len(b), agree=round(len(pairs) / max(len(a), len(b), 1), 3))
    miss_a = collections.Counter(a[i] for i in range(len(a)) if i not in pairs)
    got = set(pairs.values())
    miss_b = collections.Counter(b[j] for j in range(len(b)) if j not in got)
    res["lifted_only"] = dict(miss_a.most_common(6))
    res["real_only"] = dict(miss_b.most_common(6))
    return res


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("il", "check"))
    ap.add_argument("corpus")
    ap.add_argument("va", nargs="?")
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--max-size", type=int, default=256)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    f = open(a.corpus)
    head = json.loads(f.readline())
    rows = [json.loads(l) for l in f]
    if a.cmd == "il":
        from w8_emit import Image
        r = next(x for x in rows if x["va"] == a.va.lower().replace("0x", "").zfill(8))
        for l in lift(Image(head["image"]), int(r["va"], 16), r["size"]):
            print(l)
        return 0
    pool = [r for r in rows if r.get("status") == "matched" and r["size"] <= a.max_size and r.get("src")]
    step = max(1, len(pool) // a.sample)
    pick = pool[::step][:a.sample]
    with ProcessPoolExecutor(a.jobs) as ex:
        res = list(ex.map(_check_one, [(head, r) for r in pick]))
    st = collections.Counter(r["status"].split(":")[0] for r in res)
    ok = [r for r in res if r["status"] == "ok"]
    print("%d functions: %s" % (len(res), dict(st)))
    if ok:
        ag = sorted(r["agree"] for r in ok)
        print("token agreement: median %.0f%%, mean %.0f%%, >=90%%: %d, 100%%: %d" % (
            100 * ag[len(ag) // 2], 100 * sum(ag) / len(ag), sum(x >= 0.9 for x in ag), sum(x == 1 for x in ag)))
        la, rb = collections.Counter(), collections.Counter()
        for r in ok:
            la.update(r["lifted_only"])
            rb.update(r["real_only"])
        print("tokens only in the lifted IL:", la.most_common(10))
        print("tokens only in the real IL:  ", rb.most_common(10))
    un = collections.Counter(r["status"] for r in res if r["status"] != "ok")
    for s, n in un.most_common(10):
        print("  %3d  %s" % (n, s))
    if a.json:
        with open(a.json, "w") as fh:
            for r in res:
                fh.write(json.dumps(r) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
