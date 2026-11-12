#!/usr/bin/env python3
"""ltcgstub: what an /LTCG caller's code depends on in its callees, read from the original executable, as /GL stubs.

Doc: docs/c2/ltcg.md. Under link /LTCG c2 generates every function seeing all /GL objects. A caller's code then
depends on its own body, the bodies of callees that get inlined, and, for callees that stay calls, only:
  1. the memory summary (callee.md: which pointer parameters / globals are read or written, escapes),
  2. the register kill set: the volatile registers the callee's final code writes (MscCollectUsedRegs ->
     CGNODE+0x60 -> LoAddCallScratchRegs: kill = volatile & used), transitively through its own calls,
  3. the calling convention: standard, or the custom one c2 gives a function nothing outside the /GL set reaches
     (MdmFlexParams: a parameter its own entry block loads into a register arrives in that register).
This tool reads 2 and 3 from the original machine code (1 through tools/calleesum.py), writes stubs that give c2
the same three facts, builds a target with them under /LTCG (tools/c2ltcg.py) and compares the result.

usage:
  ltcgstub.py facts EXE VA [VA...]                    kill set, entry registers, ret N, convention of each callee
  ltcgstub.py callees EXE VA                          the direct callees of a caller, with their facts, and the
                                                      registers the caller keeps across each call
  ltcgstub.py stub EXE VA --proto 'DECL' [--regs eax,ecx] [--std] [--vary]
                                                      one stub (calleesum's memory pattern + kill set + convention)
  ltcgstub.py stubs EXE --protos FILE [--out stubs.cpp] [--prelude FILE] [--vary]
                                                      every stub of FILE (lines `VA  DECL  [regs=eax,ecx] [std]`)
  ltcgstub.py cmp EXE VA TARGET.cpp --func NAME [--with a.cpp,b.cpp] [--export SYM,...] [--keep DIR]
                  [--check-stubs FILE] [--no-gl] [--quiet] [-- <cl args>]
                                                      /GL-compile, link /LTCG, compare NAME with the original at VA

Stub properties and what c2 reads from them (docs/c2/ltcg.md section 5):
  noinline                       the callee stays a call (Inline)
  calleesum lines                memory summary, ItfSummarizeFuncInterf (callee.md)
  ltcgstub_k lines               kill set: dst registers of the stub's final IL (a volatile local's `/` writes
                                 eax+edx, `<<` writes eax+ecx; a return value writes eax)
  #pragma comment(linker, "/EXPORT:" __FUNCDNAME__)
                                 standard convention: the linker reports the symbol as referenced from outside,
                                 LtcgMarkExtRefSyms sets SSR+0x20 |= 0x2000 and MdmAllowFlexParams refuses
  no export, --regs              custom convention: the stub's entry block uses its register parameters first, in
                                 the order given, so its own allocation loads them into the registers c2 then
                                 makes the convention (checked by `cmp --check-stubs`)
"""
import argparse, io, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2build                # takes --c2 BUILD out of sys.argv (the compiler `cmp` builds with)
import capstone
from capstone import x86 as X
import calleesum

VOL = ("eax", "ecx", "edx")
XMM = tuple("xmm%d" % k for k in range(8))
ALL = set(VOL) | set(XMM)
SAVED = ("ebx", "esi", "edi", "ebp")


def full(name):
    return calleesum.FULL.get(name, name if name.startswith("xmm") else None)


class Code:
    """Facts about functions of one PE image (an original executable, or our LTCG DLL)."""

    def __init__(self, exe, span=0x8000):
        self.exe = exe if isinstance(exe, calleesum.Exe) else calleesum.Exe(exe)
        self.span = span
        self.memo = {}

    def ins(self, va):
        return self.exe.ins(va)

    def table(self, ins, va):
        """Targets of `jmp dword ptr [r*4 + table]` that lie in the function's span."""
        out = []
        op = ins.operands[0]
        if op.type != X.X86_OP_MEM or op.mem.scale != 4 or op.mem.base:
            return out
        t = op.mem.disp & 0xffffffff
        for k in range(256):
            v = self.exe.u32(t + 4 * k)
            if v is None or not (va <= v < va + self.span) or not self.exe.in_text(v):
                break
            out.append(v)
        return out

    def walk(self, va):
        """(instruction VAs, direct call targets {site: target}, indirect call sites, tail-jump targets, ret imms)."""
        seen, work = set(), [va]
        calls, ind, tails, rets = {}, [], set(), set()
        while work:
            a = work.pop()
            while a not in seen and self.exe.in_text(a) and len(seen) < 20000:
                i = self.ins(a)
                if i is None:
                    break
                seen.add(a)
                if i.id in (X.X86_INS_RET, X.X86_INS_RETF):
                    rets.add(i.operands[0].imm if i.operands else 0)
                    break
                if i.id == X.X86_INS_INT3:
                    break
                if i.id == X.X86_INS_CALL:
                    op = i.operands[0]
                    if op.type == X.X86_OP_IMM:
                        calls[a] = op.imm & 0xffffffff
                    else:
                        ind.append(a)
                elif i.id == X.X86_INS_JMP:
                    op = i.operands[0]
                    if op.type == X.X86_OP_IMM:
                        t = op.imm & 0xffffffff
                        if va <= t < va + self.span:
                            a = t
                            continue
                        tails.add(t)
                        break
                    tbl = self.table(i, va)
                    if tbl:
                        work.extend(tbl)
                    else:
                        tails.add(None)                     # jmp [iat] / jmp reg: an unknown function
                    break
                elif i.group(X.X86_GRP_JUMP) and i.operands and i.operands[0].type == X.X86_OP_IMM:
                    work.append(i.operands[0].imm & 0xffffffff)
                a += i.size
        return seen, calls, ind, tails, rets

    def written(self, va, _active=None):
        """Volatile registers (eax ecx edx, xmm0-7) the function writes, through its calls and tail jumps: what c2
        records in CGNODE+0x60 for its callers. A call or jump to code without a body (import, pointer) or a
        recursion cycle counts as every volatile register."""
        if va in self.memo:
            return self.memo[va]
        active = _active if _active is not None else set()
        if va in active:
            return set(ALL)
        active.add(va)
        seen, calls, ind, tails, rets = self.walk(va)
        w = set()
        for a in seen:
            i = self.ins(a)
            if i.id == X.X86_INS_CALL:
                continue
            try:
                _, wr = i.regs_access()
            except capstone.CsError:
                continue
            for r in wr:
                f = full(i.reg_name(r))
                if f in ALL:
                    w.add(f)
        if ind or None in tails:
            w |= ALL
        for t in list(calls.values()) + [t for t in tails if t is not None]:
            if t in seen:
                continue
            if self.exe.in_text(t) and self.exe.ins(t) is not None:
                w |= self.written(t, active)
            else:
                w |= ALL
        active.discard(va)
        self.memo[va] = w
        return w

    def inputs(self, va):
        """Registers read on some path from the entry before any write (must-defined dataflow): the register
        arguments. `push reg` of a callee-saved register and `xor r, r` are not reads."""
        seen, _, _, _, _ = self.walk(va)
        order = sorted(seen)
        succ = {}
        for a in order:
            i = self.ins(a)
            nxt = []
            if i.id in (X.X86_INS_RET, X.X86_INS_RETF, X.X86_INS_INT3):
                pass
            elif i.id == X.X86_INS_JMP:
                op = i.operands[0]
                if op.type == X.X86_OP_IMM and (op.imm & 0xffffffff) in seen:
                    nxt = [op.imm & 0xffffffff]
                elif op.type == X.X86_OP_MEM:
                    nxt = [t for t in self.table(i, va) if t in seen]
            else:
                if i.group(X.X86_GRP_JUMP) and i.operands and i.operands[0].type == X.X86_OP_IMM:
                    nxt.append(i.operands[0].imm & 0xffffffff)
                nxt.append(a + i.size)
            succ[a] = [t for t in nxt if t in seen]
        gpr = set(calleesum.GPR)
        IN = {va: set()}
        work = [va]
        reads = set()
        while work:
            a = work.pop()
            d = set(IN[a])
            i = self.ins(a)
            try:
                rd, wr = i.regs_access()
            except capstone.CsError:
                rd, wr = [], []
            rd = {full(i.reg_name(r)) for r in rd} & gpr
            wr = {full(i.reg_name(r)) for r in wr} & gpr
            if i.id in (X.X86_INS_XOR, X.X86_INS_SUB, X.X86_INS_SBB) and len(i.operands) == 2 and \
                    i.operands[0].type == i.operands[1].type == X.X86_OP_REG and i.operands[0].reg == i.operands[1].reg:
                rd = set()
            if i.id == X.X86_INS_PUSH and i.operands[0].type == X.X86_OP_REG and full(i.reg_name(i.operands[0].reg)) in SAVED:
                rd = set()
            if i.id == X.X86_INS_PUSH and i.operands[0].type == X.X86_OP_REG and full(i.reg_name(i.operands[0].reg)) == "ecx" \
                    and "ecx" not in d and a == va:
                rd = set()                                  # `push ecx` as the first instruction reserves a slot
            reads |= rd - d
            if i.id == X.X86_INS_CALL:
                wr |= set(VOL)
            d |= wr
            for t in succ.get(a, []):
                if t not in IN:
                    IN[t] = set(d)
                    work.append(t)
                elif not IN[t] <= d:
                    IN[t] &= d
                    work.append(t)
        return [r for r in ("eax", "ecx", "edx", "ebx", "esi", "edi", "ebp") if r in reads]

    def end(self, va):
        seen, _, _, _, _ = self.walk(va)
        last = max(seen)
        return last + self.ins(last).size

    def facts(self, va):
        seen, calls, ind, tails, rets = self.walk(va)
        inp = self.inputs(va)
        pop = max(rets) if rets else 0
        conv = classify(inp, pop)
        return dict(va=va, kill=sorted(self.written(va), key=regkey), inputs=inp, pop=pop, conv=conv,
                    calls=calls, indirect=ind, tails=tails, end=self.end(va))


def regkey(r):
    order = list(VOL) + list(XMM)
    return order.index(r) if r in order else 99


def classify(inp, pop):
    s = set(inp)
    if not s:
        return "stdcall" if pop else "cdecl"
    if s == {"ecx"}:
        return "thiscall (or custom ecx)"
    if s == {"ecx", "edx"}:
        return "fastcall (or custom ecx, edx)"
    return "custom (/LTCG register arguments: %s)" % ", ".join(inp)


def show_facts(code, va, out=sys.stdout):
    f = code.facts(va)
    out.write("%08x  conv %-40s ret %-3d entry regs %-16s kill %s\n" % (
        va, f["conv"], f["pop"], ",".join(f["inputs"]) or "-", ",".join(f["kill"]) or "none"))
    return f


def live_across(code, caller, site):
    """Volatile registers the caller reads after the call at `site` before writing them: values it keeps across."""
    i = code.ins(site)
    a, keep, done = site + i.size, set(), set()
    for _ in range(64):
        j = code.ins(a)
        if j is None:
            break
        try:
            rd, wr = j.regs_access()
        except capstone.CsError:
            break
        rd = {full(j.reg_name(r)) for r in rd}
        wr = {full(j.reg_name(r)) for r in wr}
        if j.id in (X.X86_INS_XOR, X.X86_INS_SUB) and len(j.operands) == 2 and \
                j.operands[0].type == j.operands[1].type == X.X86_OP_REG and j.operands[0].reg == j.operands[1].reg:
            rd = set()
        for r in VOL:
            if r in rd and r not in done and r != "eax":
                keep.add(r)
            if r in wr:
                done.add(r)
        if j.id in (X.X86_INS_CALL, X.X86_INS_RET, X.X86_INS_JMP) or j.group(X.X86_GRP_JUMP):
            break
        a += j.size
    return sorted(keep, key=regkey)


# ---- stubs ------------------------------------------------------------------------------------------------------
KILL_DECL = "    volatile int ltcgstub_k[2];"
KILL_LINES = {
    "edx": "    ltcgstub_k[0] = ltcgstub_k[0] / ltcgstub_k[1];   // kill set: idiv writes eax, edx",
    "ecx": "    ltcgstub_k[0] = ltcgstub_k[0] << ltcgstub_k[1];  // kill set: shl r, cl writes ecx (and eax)",
    "eax": "    ltcgstub_k[0] = ltcgstub_k[1];                   // kill set: a load into eax",
}
EXPORT = '#pragma comment(linker, "/EXPORT:" __FUNCDNAME__)   // standard convention: referenced from outside the /GL set'


def make_stub(exe, code, va, proto_text, regs=None, std=None, out=sys.stdout, vary=False, tu=None):
    """calleesum's memory-pattern stub, plus the kill set and the convention of the original at va."""
    f = code.facts(va)
    proto = calleesum.parse_proto(proto_text)
    conv = proto["conv"]
    custom = f["conv"].startswith("custom") or (regs and regs != (["ecx"] if conv == "thiscall" else []))
    if regs is None and f["conv"].startswith("custom"):
        regs = f["inputs"]
    if std is None:
        std = not custom
    # under /LTCG every direct callee with a body is summarised (the whole /GL set is one unit): calleesum's --tu
    an = calleesum.Analyzer(exe, tu=tu if tu is not None else (exe.base, exe.base + exe.size))
    nparams = sum(sl for _, _, sl in proto["params"]) - (min(2, len(proto["params"])) if conv == "fastcall" else 0)
    if custom and regs:
        nparams = max(0, len(proto["params"]) + (1 if conv == "thiscall" else 0) - len(regs))
        s = an.summarize(va, "cdecl", nparams, regs=regs)
    else:
        s = an.summarize(va, conv, nparams)
    s.returns = proto["ret"] != "void"
    buf = io.StringIO()
    calleesum.stub(exe, s, proto, out=buf)
    text = buf.getvalue()
    kill = [r for r in f["kill"] if r in VOL]
    want = set(kill)
    if "calleesum_ext(" in text or "calleesum_pass(" in text:
        want = set()                                        # an unsummarised call in the stub kills every register
    lines = []
    if "edx" in want:
        lines.append(KILL_LINES["edx"])
    if "ecx" in want:
        lines.append(KILL_LINES["ecx"])
    if want == {"eax"} and proto["ret"] == "void":
        lines.append(KILL_LINES["eax"])
    fold = []                   # kill-set operations folded into the returned value (the scheduler cannot hoist them
    if proto["ret"] != "void" and lines:    # apart from the value's own load): `<<` adds ecx, `/` adds edx
        fold = [("<<" if "<<" in l else "/") for l in lines]
        lines = []
    xmm = [r for r in f["kill"] if r.startswith("xmm")]
    head = ["// ---- ltcgstub for %08x: kill set %s; convention %s (ret %d) ----" % (
        va, ",".join(f["kill"]) or "none", f["conv"], f["pop"])]
    if xmm:
        head.append("// NOTE: the original also writes %s; a stub does that only with a float computation under "
                    "/arch:SSE (not generated)" % ",".join(xmm))
    if custom:
        head.append("// custom convention: register arguments %s (in the declaration's parameter order, `this` first); "
                    "the stub must not be exported or have its address taken" % ",".join(regs or []))
    if not kill:
        head.append("// NOTE: the original writes no volatile register; a stub that loads a stack parameter or returns "
                    "a value writes eax: check with `cmp --check-stubs`")
    if s.grd and proto["ret"] != "void" and not s.returned:
        # the original returns something it read from a global (a getter): return a global read, not the constant 0
        # (c2 would otherwise see `return 0` and fold the caller's use of the result)
        text = re.sub(r"(?m)^(\s+return \(([^;]*)\))(?:\(void\*\))?cs;$",
                      lambda m: "%s(void*)ltcgstub_gread;   // a value read from a global, as the original" % m.group(1),
                      text)
        if "ltcgstub_gread" in text:
            head.append("extern void* ltcgstub_gread;")
    if fold:
        def refold(m):
            ty, e = m.group(1), m.group(2)
            v = "(int)(%s)" % e
            for op in fold:
                v = "(%s %s ltcgstub_k[1])" % (v, op)
            ptr = "*" in ty
            return "    return (%s)%s%s;   // kill set: the returned value goes through %s" % (
                ty, "(void*)" if ptr else "", v, " and ".join({"<<": "shl r, cl (ecx)", "/": "idiv (edx)"}[o] for o in fold))
        text = re.sub(r"(?m)^    return \(([^()]*)\)(.*?);(?:\s*//.*)?$", refold, text, count=1)
    uses = []
    if custom:
        # every parameter the original reads must stay live, or MscSetDeadParamMap drops it and callers stop passing
        # it; register parameters first, in the scratch rotation order (eax, ecx, edx), so that the stub's own entry
        # block loads them into those registers (MdmFlexParams then makes them the convention)
        allp = (["this"] if conv == "thiscall" else []) + [nm for _, nm, _ in proto["params"]]
        direct = getattr(getattr(s, "func", None), "direct", set())
        rot = ["eax", "ecx", "edx", "ebx", "esi", "edi", "ebp"]
        reg_of = {allp[k]: r for k, r in enumerate(regs or []) if k < len(allp)}
        for nm in sorted(reg_of, key=lambda n: rot.index(reg_of[n]) if reg_of[n] in rot else 9):
            uses.append("    ltcgstub_k[0] = (int)%s;%s// %s arrives in %s" % (nm, " " * max(1, 22 - len(nm)), nm, reg_of[nm]))
        for k, nm in enumerate(allp[len(reg_of):]):
            if 4 + 4 * k in direct:
                uses.append("    ltcgstub_k[1] = (int)%s;%s// stack parameter the original reads (not dead)" % (
                    nm, " " * max(1, 22 - len(nm))))
            else:
                head.append("// NOTE: %s is never read by the original: c2 removes it (dead parameter, the callers do "
                            "not pass it); the stub does not use it either" % nm)
    body = []
    for ln in text.splitlines():
        body.append(ln)
        if ln == "{":
            if std:
                body.append(EXPORT)
            if lines or uses or fold:
                body.append(KILL_DECL)
            body.extend(uses)
            body.extend(lines)          # first, while eax/ecx/edx are free: the scratch picks then land on them
            lines = []
    out_lines = []
    for ln in body:
        if ln.startswith("    return ") or ln == "}":
            out_lines.extend(lines)
            lines = []
        out_lines.append(ln)
    if vary and not proto["cls"] and all(re.fullmatch(r"(const\s+)?(unsigned\s+|signed\s+)?(int|long|short|char|bool|"
                                                    r"float|double|\w+\s*\*+)", t.strip()) for t, _, _ in proto["params"]):
        # cl 14.00 / 15.00 WPA (WpaGenConstParamInfoUse): a parameter every caller passes the same constant is folded
        # into the callee and dropped from the calls. A second caller with unknown values keeps the stub general.
        n = re.sub(r"\W", "_", proto["name"])
        out_lines.append('extern "C" __declspec(dllexport) void ltcgstub_vary_%s(void) { volatile int v = 0; %s(%s); }'
                         % (n, proto["name"], ", ".join("(%s)v" % t for t, _, _ in proto["params"])))
    out.write("\n".join(head + out_lines) + "\n")
    return f


def read_protos(path):
    """Lines `VA  DECL  [regs=eax,ecx] [std|custom]` (# comments)."""
    out = []
    for ln in open(path):
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        m = re.match(r"([0-9a-fA-Fx]+)\s+(.*?)(?:\s+regs=([\w,]+))?(?:\s+(std|custom))?\s*$", ln)
        if not m:
            sys.exit("bad line in %s: %s" % (path, ln))
        out.append((int(m.group(1), 16), m.group(2), m.group(3).split(",") if m.group(3) else None,
                    {"std": True, "custom": False}.get(m.group(4))))
    return out


# ---- compare ----------------------------------------------------------------------------------------------------
def masked(code, va, n):
    """(bytes, mask offsets) of [va, va+n): in-image absolute operands and rel32 targets leaving the range."""
    exe = code.exe
    b = exe.read(va, n)
    m = set()
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    for i in md.disasm(b, va):
        o = i.address - va
        for at, size in ((i.disp_offset, i.disp_size), (i.imm_offset, i.imm_size)):
            if at and size == 4:
                v = int.from_bytes(b[o + at:o + at + 4], "little")
                if exe.in_image(v):
                    m.update(range(o + at, o + at + 4))
        if i.id in (X.X86_INS_CALL, X.X86_INS_JMP) or i.group(X.X86_GRP_JUMP):
            if i.operands and i.operands[0].type == X.X86_OP_IMM and i.size >= 5:
                t = i.operands[0].imm & 0xffffffff
                if not (va <= t < va + n):
                    m.update(range(o + i.size - 4, o + i.size))
    # jump tables: capstone's sweep stops in them or decodes them as code, so their entries (image addresses, which
    # differ between the two images) were compared raw. Mask every aligned run of 2+ image-address dwords.
    img = lambda o: o + 4 <= n and exe.in_image(int.from_bytes(b[o:o + 4], "little"))
    o = (-va) % 4
    while o + 8 <= n:
        if img(o) and img(o + 4):
            while img(o):
                m.update(range(o, o + 4))
                o += 4
        else:
            o += 4
    return b, m


def instrs(code, va, n):
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    return [(i.address - va, i.size, i.mnemonic, i.op_str) for i in md.disasm(code.exe.read(va, n), va)]


def compare(orig, ova, ours, uva, out=sys.stdout, show=True):
    """Byte and register comparison of our function at uva (our DLL) with the original at ova."""
    import tsweep
    on = orig.end(ova) - ova
    un = ours.end(uva) - uva
    ob, om = masked(orig, ova, on)
    ub, um = masked(ours, uva, un)
    oi, ui = instrs(orig, ova, on), instrs(ours, uva, un)
    rd = tsweep.regdiff([(x[2], x[3]) for x in ui], [(x[2], x[3]) for x in oi])
    nb = sum(1 for k in range(min(on, un)) if k not in om and k not in um and ob[k] != ub[k]) + abs(on - un)
    exact = on == un and nb == 0
    ndiff = 0
    lines = []
    for k in range(max(len(oi), len(ui))):
        x = oi[k] if k < len(oi) else None
        y = ui[k] if k < len(ui) else None
        same = x and y and x[0] == y[0] and x[1] == y[1] and all(
            ob[x[0] + j] == ub[y[0] + j] or (x[0] + j) in om or (y[0] + j) in um for j in range(x[1]))
        if not same:
            ndiff += 1
            lines.append("  %04x  orig %-34s ours %s" % (x[0] if x else y[0], ("%s %s" % (x[2], x[3]))[:34] if x else "-",
                                                         "%s %s" % (y[2], y[3]) if y else "-"))
    out.write("compare: original %08x %d bytes, ours %d bytes; %s; %d bytes differ (masked), %d of %d instructions "
              "differ positionally; regdiff %d\n" % (ova, on, un, "BYTE-EXACT (relocations masked)" if exact else "not exact",
                                                     nb, ndiff, len(oi), rd))
    if show:
        out.writelines(l + "\n" for l in lines[:40])
    return dict(exact=exact, bytes=nb, instrs=ndiff, regdiff=rd, orig_len=on, our_len=un)


def build(target, more, exports, cl, keep, gl=True):
    import c2ltcg, c2trace, tempfile
    os.environ["CCACHE"] = "0"
    d = os.path.abspath(keep) if keep else tempfile.mkdtemp(prefix="ltcgstub_")
    os.makedirs(d, exist_ok=True)
    rc, text, dll = c2ltcg.build([os.path.abspath(target)] + [os.path.abspath(m) for m in more],
                                 ["/nologo", "/c", "/O2"] + cl, d, exports, c2trace.run_tool, trace=False, gl=gl)
    if dll is None:
        sys.stdout.write(text)
        sys.exit("build failed")
    return dll, c2ltcg.read_map(dll[:-4] + ".map")


def main():
    argv = sys.argv[1:]
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    if not argv or argv[0] not in ("facts", "callees", "stub", "stubs", "cmp"):
        sys.exit(__doc__)
    sub = argv.pop(0)
    ap = argparse.ArgumentParser(prog="ltcgstub.py " + sub)
    ap.add_argument("exe")
    if sub == "facts":
        ap.add_argument("va", nargs="+")
    elif sub in ("callees", "stub"):
        ap.add_argument("va")
    elif sub == "cmp":
        ap.add_argument("va")
        ap.add_argument("target")
        ap.add_argument("--func", required=True)
        ap.add_argument("--with", dest="more", default="")
        ap.add_argument("--export", default="")
        ap.add_argument("--keep")
        ap.add_argument("--check-stubs", help="the protos file of the stubs: compare each stub's facts with the original's")
        ap.add_argument("--quiet", action="store_true")
        ap.add_argument("--no-gl", action="store_true", help="baseline: ordinary objects, no /GL and no /LTCG")
    if sub in ("stub", "stubs"):
        ap.add_argument("--vary", action="store_true", help="add an exported caller with unknown arguments (cl 14.00 / "
                        "15.00: keeps WPA from folding a constant argument into the stub)")
        ap.add_argument("--tu", help="LO-HI: the VA range of the /GL code (direct callees there count as summarised); "
                        "default the whole image")
    ap.add_argument("--proto")
    ap.add_argument("--regs")
    ap.add_argument("--std", action="store_true")
    ap.add_argument("--protos")
    ap.add_argument("--out")
    ap.add_argument("--prelude")
    a = ap.parse_args(argv)
    exe = calleesum.Exe(a.exe)
    code = Code(exe)
    if sub == "facts":
        for v in a.va:
            show_facts(code, int(v, 16))
        return 0
    if sub == "callees":
        va = int(a.va, 16)
        f = show_facts(code, va)
        for site, t in sorted(f["calls"].items()):
            keep = live_across(code, va, site)
            sys.stdout.write("  %08x call %08x  keeps %-10s | " % (site, t, ",".join(keep) or "-"))
            g = show_facts(code, t)
            bad = [r for r in keep if r in g["kill"]]
            if bad:
                print("      ! the caller reads %s after the call, which the callee writes: a return value, or the "
                      "facts are wrong" % ",".join(bad))
        for site in f["indirect"]:
            print("  %08x indirect call: every volatile register is killed" % site)
        return 0
    if sub == "stub":
        if not a.proto:
            sys.exit("stub needs --proto")
        make_stub(exe, code, int(a.va, 16), a.proto, a.regs.split(",") if a.regs else None, True if a.std else None,
                  vary=a.vary, tu=tuple(int(x, 16) for x in a.tu.split("-")) if a.tu else None)
        return 0
    if sub == "stubs":
        if not a.protos:
            sys.exit("stubs needs --protos FILE")
        buf = io.StringIO()
        buf.write("// generated by tools/ltcgstub.py stubs %s --protos %s: compile-only, /GL, not decompilations\n" % (
            os.path.basename(a.exe), os.path.basename(a.protos)))
        if a.prelude:
            buf.write(open(a.prelude).read() + "\n")
        for va, proto, regs, std in read_protos(a.protos):
            make_stub(exe, code, va, proto, regs, std, out=buf, vary=a.vary,
                      tu=tuple(int(x, 16) for x in a.tu.split("-")) if a.tu else None)
            buf.write("\n")
        if a.out:
            open(a.out, "w").write(buf.getvalue())
            print("wrote %s" % a.out)
        else:
            sys.stdout.write(buf.getvalue())
        return 0
    # cmp
    import c2trace
    exports = [x for x in a.export.split(",") if x]
    more = [x for x in a.more.split(",") if x]
    dll, funcs = build(a.target, more, exports, cl, a.keep, gl=not a.no_gl)
    hit = [(n, v) for n, v, _ in funcs if c2trace.func_match(a.func, n)]
    if not hit:
        sys.exit("no function matching %r in the map: %s" % (a.func, " ".join(n for n, _, _ in funcs)[:600]))
    name, uva = hit[0]
    ours = Code(dll)
    print("ours: %s at %08x (%s)" % (name, uva, os.path.dirname(dll)))
    r = compare(code, int(a.va, 16), ours, uva, show=not a.quiet)
    # the calls: callee facts on both sides
    of, uf = code.facts(int(a.va, 16)), ours.facts(uva)
    oc, uc = sorted(of["calls"].items()), sorted(uf["calls"].items())
    byaddr = {v: n for n, v, _ in funcs}
    for k in range(max(len(oc), len(uc))):
        o = oc[k] if k < len(oc) else None
        u = uc[k] if k < len(uc) else None
        og = code.facts(o[1]) if o else None
        ug = ours.facts(u[1]) if u and u[1] in byaddr else None
        print("  call %d: orig %s kill %-12s entry %-10s | ours %s kill %-12s entry %s" % (
            k + 1, "%08x" % o[1] if o else "-", ",".join(og["kill"]) if og else "-", ",".join(og["inputs"]) if og else "-",
            byaddr.get(u[1], "%08x" % u[1])[:40] if u else "-", ",".join(ug["kill"]) if ug else "(no body: all)",
            ",".join(ug["inputs"]) if ug else "-"))
    if a.check_stubs:
        for va, proto, regs, std in read_protos(a.check_stubs):
            nm = calleesum.parse_proto(proto)["name"].split("::")[-1]
            mine = [(n, v) for n, v, _ in funcs if re.search(r"(^|[?_@])%s(@|$)" % re.escape(nm), n)]
            if not mine:
                print("  stub %08x %s: not in the image (inlined? add noinline)" % (va, nm))
                continue
            g, h = code.facts(va), ours.facts(mine[0][1])
            ok = [r for r in VOL if r in g["kill"]] == [r for r in VOL if r in h["kill"]] and g["inputs"] == h["inputs"] \
                and g["pop"] == h["pop"]
            print("  stub %08x %-28s orig kill %-10s entry %-8s ret %d | stub kill %-10s entry %-8s ret %d  %s" % (
                va, nm[:28], ",".join(x for x in g["kill"] if x in VOL), ",".join(g["inputs"]) or "-", g["pop"],
                ",".join(x for x in h["kill"] if x in VOL), ",".join(h["inputs"]) or "-", h["pop"], "ok" if ok else "DIFFERS"))
    return 0 if r["exact"] else 1


if __name__ == "__main__":
    sys.exit(main())
