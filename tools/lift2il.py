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
FCC = {(0x41, "jne"): "le", (0x41, "je"): "g", (0x01, "jne"): "l", (0x01, "je"): "ge",
       (0x40, "jne"): "e", (0x40, "je"): "ne", (0x05, "jnp"): "l", (0x05, "jp"): "ge",
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
        self.bvctor = {}                   # outgoing offset -> (copy constructor va, bytes): a by-value class
        self.load_src = {}                 # temp -> (base token, disp) of the load that defined it
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

    # ---- registers read / written (full 32-bit names)
    def regs(self, i):
        r, w = i.regs_access()
        rn = {full(self.md.reg_name(x)) for x in r}
        wn = {full(self.md.reg_name(x)) for x in w}
        m = i.mnemonic
        if m == "xor" and len(i.operands) == 2 and i.operands[0].type == X.X86_OP_REG and \
                i.operands[0].reg == i.operands[1].reg:
            rn.discard(full(self.md.reg_name(i.operands[0].reg)))        # xor r, r reads nothing
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
            if j < 1 or self.ins[j].mnemonic != "ja" or self.ins[j - 1].mnemonic != "cmp" or \
                    self.ins[j - 1].operands[1].type != X.X86_OP_IMM:
                continue
            n = self.ins[j - 1].operands[1].imm + 1
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
            self.switches[i.address] = dict(idx=full(idxreg), cases=list(enumerate(tgts)), default=default,
                                            cmp=self.ins[j - 1].address, ja=self.ins[j].address,
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
        return i.mnemonic == "jmp" and i.operands and i.operands[0].type == X.X86_OP_IMM and \
            not self.va <= i.operands[0].imm < self.va + self.size

    def thiscall(self, k):
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
            elif self.ecx_is_data(d[0], k):
                return False
        return True

    def ecx_is_data(self, d, k):
        """Between ecx's def d and the call k (straight-line code), is ecx's value stored or pushed as data
        (`mov ecx, [s+8]; mov [eax+8], ecx; call`: the third dword of a struct argument, not `this`)?"""
        if not (isinstance(d, int) and d < k) or k - d > 40:
            return False
        for q in range(d + 1, k):
            i = self.ins[q]
            if i.mnemonic.startswith(("j", "ret", "call")):
                return False
            ops = i.operands
            if i.mnemonic == "mov" and len(ops) == 2 and ops[1].type == X.X86_OP_REG and \
                    self.md.reg_name(ops[1].reg) == "ecx" and ops[0].type == X.X86_OP_MEM and ops[0].mem.base:
                b = self.md.reg_name(ops[0].mem.base)
                if b == "esp":
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
            self.arg_types.setdefault(off, ty)
            return "_a%d:%s" % (off, ty)
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
        terms = []
        if base:
            terms.append("%s:1004" % self.var_read(k, full(base)))
        if idx:
            terms.append("%s:1004%s" % (self.var_read(k, full(idx)), "*%d" % m.scale if m.scale > 1 else ""))
        if m.disp:
            terms.append("%d" % m.disp)
        return "[%s]:%s" % ("+".join(terms) or "0", ty)

    def is_sse(self, i):
        m = i.mnemonic
        return m in SSE_OPS or m in SSE_MOVES or m in ("comiss", "ucomiss", "comisd", "ucomisd", "xorps", "xorpd",
                                                       "cvtsi2ss", "cvtsi2sd", "cvttss2si", "cvttsd2si", "cvtss2sd",
                                                       "cvtsd2ss")

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
        self.webs()
        pro = self.prologue()
        self.pro = pro
        self.find_tls()
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
                    if k in pro:
                        esp, ebp = self.stack_effect(i, esp, ebp)
                        continue
                    if k in self.skip:
                        continue
                    dv = self.magic_div(k)
                    if dv:
                        src, dvs, kend, dreg = dv
                        self.emit(k, "OPDIV", ["%s:1004" % self.var_written(kend, dreg)],
                                  ["%s:1004" % self.var_read(k + 1, src), "%d:1004" % dvs])
                        self.skip.update(range(k + 1, kend + 1))
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
                        self.emit(k, "OPSWITCH", [], [sw.get("idxtok", "%s:1004" % self.var_read(k, sw["idx"]))],
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
                        thiscall = self.thiscall(k)
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
                        self.emit(k, "OPCALL", d, [callee] + toks, note)
                        esp = esp_after
                        flags = None
                    elif m == "ret":
                        if ops:
                            self.ret_pop = ops[0].imm
                        if self.fpu:                        # st0 at ret: a float return
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
                        elif tgt.type == X.X86_OP_IMM:
                            # a tail call: real IL has a plain OPCALL here (the jmp is chosen in codegen). Its stack
                            # arguments are the caller's own argument slots, as this function left them (f3 0056b620:
                            # `mov [esp+8], 1; jmp` passes key and 1): the current value of each `_aN` variable.
                            arity = max([(o - 4) // 4 + 1 for o in self.args_read] + [self.max_ret_pop // 4])
                            args = ["_a%d:1004" % (4 + 4 * j) for j in range(arity)]
                            for j in range(arity):
                                self.args_read.add(4 + 4 * j)
                            thiscall = self.thiscall(k)
                            note = "s0=?FUN_%08x@@YAXXZ" % (tgt.imm & 0xFFFFFFFF)
                            if thiscall:
                                args = ["%s:1004" % self.var_read(k, "ecx")] + args
                                note += " thiscall=1"
                            toks = []
                            for x in args:
                                t = self.temp()
                                self.emit(k, "OPARG", [t], [x])
                                toks.append(t)
                            fn = "?FUN_%08x@@YAXXZ:1004" % (tgt.imm & 0xFFFFFFFF)
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
                                fa, fb = (re.sub(r"^(-?[\d.]+):%s$" % F4, r"\1:%s" % F8, x) for x in (fa, fb))
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
                    elif m.startswith("set") and ("j" + m[3:]) in CCOF and ops and ops[0].type == X.X86_OP_REG:
                        # setcc r8 after cmp / test: the comparison's value (0 / 1) in a byte register
                        a, b2 = flags or ("0:1004", "0:1004")
                        if flags is None:
                            raise Unsupported("set%s without a compare" % m[3:])
                        self.emit(k, "OPSETCC", ["%s:2001" % self.var_written(k, full(self.md.reg_name(ops[0].reg)))],
                                  [a, b2], "cc=%s" % CCOF["j" + m[3:]])
                    elif m in ("cmp", "test"):
                        a, b2 = self.opnd(k, ops[0], esp, ebp), self.opnd(k, ops[1], esp, ebp)
                        if m == "test" and a == b2:
                            b2 = "0:1004"
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
                        self.emit(k, ARITH[m], [d], [a, b2])
                        flags = (d, "0:1004")
                        self.carry = (a, b2, "b") if m == "sub" else None
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
        return self.lines

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
            return (self.opnd(k, ops[0], esp, ebp), self.opnd(k, ops[1], esp, ebp))
        if m in ("cvtsi2ss", "cvtsi2sd"):
            src = self.opnd(k, ops[1], esp, ebp)
            self.emit(k, "OPASSIGN", [self.dst(k, ops[0], esp, ebp)], [src])
            return flags
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
        if unit > 1:
            t = self.temp()
            self.emit(k, "shl", [t], [n, "2:1004"])
            n = t
        a = [self.temp(), self.temp(), self.temp()]
        self.emit(k, "OPARG", [a[0]], ["%s:1004" % self.var_read(k, "edi")])
        self.emit(k, "OPARG", [a[1]], ["%d:1004" % (v & 0xFF)])
        self.emit(k, "OPARG", [a[2]], [n])
        self.emit(k, "OPCALL", [], ["?__lift_memset@@YAXXZ:1004"] + a, "s0=?__lift_memset@@YAXXZ")

    def block_copy(self, k, i):
        """`rep movsd` (edi <- esi, ecx dwords): the IL of a struct copy is OPMBASSIGN, which il2c does not write;
        a memcpy of the same size is what cl /O2 turns back into `mov ecx, N; rep movsd` (an intrinsic)."""
        unit = 4 if "movsd" in (i.mnemonic + " " + i.op_str) else 1
        n = "%s:1004" % self.var_read(k, "ecx")
        if unit > 1:
            t = self.temp()
            self.emit(k, "shl", [t], [n, "2:1004"])
            n = t
        a = [self.temp(), self.temp(), self.temp()]
        self.emit(k, "OPARG", [a[0]], ["%s:1004" % self.var_read(k, "edi")])
        self.emit(k, "OPARG", [a[1]], ["%s:1004" % self.var_read(k, "esi")])
        self.emit(k, "OPARG", [a[2]], [n])
        self.emit(k, "OPCALL", [], ["?__lift_memcpy@@YAXXZ:1004"] + a, "s0=?__lift_memcpy@@YAXXZ")

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
        if m == "fsincos":
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

    def x87(self, k, i, esp, ebp):
        m, ops = i.mnemonic, i.operands
        if m in ("fld", "fild"):
            if ops[0].type == X.X86_OP_REG:
                self.fpu.append(self.fpu[self.st(ops[0])])
            elif m == "fild":
                t = self.temp(F8)
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
        if m in FUNARY or m in ("fpatan", "fsincos"):
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

    def magic_div(self, k):
        """cl's signed division by a constant: `mov eax, M; imul r; [sar edx, S;] mov t, edx; shr t, 31;
        add d, t` (d, t = eax/edx in either order) is r / D. D is recovered from M and S and checked on
        sample dividends. Returns (r, D, index of the add, d)."""
        ins = self.ins
        if k + 4 >= len(ins):
            return None
        i0, i1 = ins[k], ins[k + 1]
        if not (i0.mnemonic == "mov" and i0.op_str.startswith("eax, ") and i0.operands[1].type == X.X86_OP_IMM and
                i1.mnemonic == "imul" and len(i1.operands) == 1 and i1.operands[0].type == X.X86_OP_REG):
            return None
        M = i0.operands[1].imm & 0xFFFFFFFF
        j, S = k + 2, 0
        src = self.md.reg_name(i1.operands[0].reg)
        addback = ins[j].mnemonic == "add" and ins[j].op_str == "edx, " + src   # a magic >= 2^31: add x back
        if addback:
            j += 1
        if ins[j].mnemonic == "sar" and ins[j].op_str.startswith("edx, "):
            S, j = ins[j].operands[1].imm, j + 1
        a, b, c = ins[j], ins[j + 1] if j + 1 < len(ins) else None, ins[j + 2] if j + 2 < len(ins) else None
        if not (a.mnemonic == "mov" and a.op_str.endswith(", edx") and b is not None and b.mnemonic == "shr" and
                b.op_str == a.op_str.split(",")[0] + ", 0x1f" and c is not None and c.mnemonic == "add"):
            return None
        t = a.op_str.split(",")[0]
        dreg = c.op_str.split(",")[0]
        if sorted(c.op_str.replace(" ", "").split(",")) != sorted([t, "edx"]) or dreg not in ("eax", "edx", t):
            return None
        Ms = M - (1 << 32) if M & 0x80000000 else M
        D = round((1 << (32 + S)) / M) if M else 0
        if D < 2:
            return None
        def q(x):
            hi = (x * Ms) >> 32
            if addback:
                hi += x
            hi >>= S
            return hi + (1 if hi < 0 else 0)
        for x in (0, 1, D - 1, D, D + 1, 12345, 7 * D + 3, (1 << 31) - 1, -1, -D, -(1 << 31), 99991, -77777):
            if q(x) != int(x / D):
                return None
        return full(src), D, j + 2, full(dreg)

    def frame_offsets(self):
        """Entry-relative offsets of the frame slots the lifted code names (`_lN` is -N)."""
        return {-int(m) for l in self.lines for m in re.findall(r"\b_l(\d+):", l)}

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
            for i, b, disp, n in reversed([x for x in self.byval if x[3] > 0]):
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
            return any(x[0] not in ("entry", "undef") and self.ins[x[0]].mnemonic != "call" and not arg_only(x[0])
                       and not zero_for_stores(x[0]) for x in defs)
        root = self.find(d)
        return any(self.find(v) == root for (kk, rr), v in self.reach.items() if rr == d[1])

    def prologue(self):
        """Indices of the frame setup and the callee-saved pushes / pops around it (they leave no tuples)."""
        out = set()
        # the frame setup: everything before the first call or branch that only saves registers or makes room.
        # cl interleaves the callee-saved pushes with the body (`push ebx; push esi; mov esi, [ebp+8]; ...; push
        # edi`), and an aligned frame has `and esp, -8` (M&B 00403b40), so the setup is not one leading run.
        first_flow = next((k for k, i in enumerate(self.ins) if i.mnemonic == "call" or i.mnemonic.startswith("j")
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
        lead = True
        saved, written = set(), set()
        for k, i in enumerate(self.ins[:first_flow]):
            m, o = i.mnemonic, i.op_str
            if k in self.eh_ins:
                continue
            if m == "push" and o in CALLEE_SAVED and o in restored and o not in saved and o not in written:
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
                    self.ins[q].mnemonic == "push" for q in range(k + 1, first_flow))):
                out.add(k)                          # also a late one: pushes follow before the first call, so
                #                                     the room is the frame (a local passed by address), not arguments
            elif m == "and" and o.startswith("esp, "):
                out.add(k)                          # an aligned frame: offsets stay relative to the aligned esp
            else:
                lead = False
        # a shrink-wrapped save: cl pushes a callee-saved register only on the path that needs it (`jp L; ...;
        # ret; L: push esi`). A push past the first branch of a register still holding the caller's value, popped
        # before a ret, is a save, not an argument
        for k in range(first_flow, len(self.ins)):
            i = self.ins[k]
            if i.mnemonic == "push" and i.op_str in CALLEE_SAVED and i.op_str in restored and i.op_str not in saved:
                ds = self.reach_defs.get((k, i.op_str), set())
                if ds and all(d[0] == "entry" for d in ds):
                    out.add(k)
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

    def stack_effect(self, i, esp, ebp):
        m, o = i.mnemonic, i.op_str
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
