#!/usr/bin/env python3
"""Lift machine code back to c2's IL at Lower's entry: the original's IL after GlobalOptimize, where it differs from
ours, named by our tuples and source lines.  Doc: docs/c2/lift.md.

usage: lift.py <src> --func F [--asm-vs ORIG.s] [--show] [--json OUT] [-- <cl args>]
         one traced compile: our IL at Lower's entry, canonicalised; our final code lifted (the self-check: how much
         of our own code the rules give back); with --asm-vs the original's code lifted and aligned to our IL, and
         each difference reported at our tuple and source line
       lift.py selfcheck --rows ROWS.jsonl [--jobs N] [--top 25] [--out DIR]   self-check over many functions (row: src, func,
         c2, flags): agreement per function and the most frequent unmatched tokens, both sides

Why Lower's entry. The back end after it (instruction selection, register allocation, scheduling, peepholes) is
nearly invertible locally: each instruction comes from one tuple pattern, and what allocation and scheduling erased
(names, order) the IL shape does not need. The optimiser before it is not invertible (CSE, copy propagation,
folding and dead stores are many-to-one), and need not be: a difference at this stage is already the difference
between the two sources' IL, and icount / the IL dump tie each of our tuples to its source line.

Tokens (one per tuple / instruction, the same vocabulary on both sides: `ld`, `st`, `rmw.OP`, `OP:K,K`, `cmp`, `jcc`,
`push`, `call`, `copy`, `f.OP`), what is dropped as back-end artefacts, and how the original is aligned to our IL
through our own code: docs/c2/lift.md. icount prints the same comparison under its runs (no compile more).
"""
import argparse, collections, difflib, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REGS = set("eax ebx ecx edx esi edi ebp esp ax bx cx dx si di bp sp al ah bl bh cl ch dl dh".split())
try:                              # x64 (cl 17.00, "arch": "x64" in the build table): the 64-bit names too
    import c2build
    X64 = c2build.get("arch") == "x64"
except Exception:
    X64 = False
if X64:
    REGS |= set("rax rbx rcx rdx rsi rdi rbp rsp sil dil bpl spl".split())
    REGS |= {"r%d%s" % (k, w) for k in range(8, 16) for w in ("", "d", "w", "b")}
ARGREGS = ("rcx", "rdx", "r8", "r9")       # the Win64 convention's register arguments
_A64 = {"ecx": "rcx", "cx": "rcx", "cl": "rcx", "edx": "rdx", "dx": "rdx", "dl": "rdx", "rcx": "rcx", "rdx": "rdx",
        "r8": "r8", "r8d": "r8", "r8w": "r8", "r8b": "r8", "r9": "r9", "r9d": "r9", "r9w": "r9", "r9b": "r9"}
_SAVED64 = r"(?:rbx|rsi|rdi|rbp|r1[2-5])"
COMM = {"add", "and", "or", "xor", "imul", "mul", "cmp"}
ARITH = {"add", "sub", "and", "or", "xor", "imul", "mul", "shl", "sal", "shr", "sar", "neg", "not", "inc", "dec",
         "adc", "sbb", "idiv", "div", "cdq", "rol", "ror"}
ILOP = {"OPADD": "add", "OPSUB": "sub", "OPAND": "and", "OPOR": "or", "OPXOR": "xor", "OPMUL": "imul",
        "OPSHL": "shl", "OPSHR": "shr", "OPSAR": "sar", "OPNEG": "neg", "OPNOT": "not", "OPDIV": "idiv",
        "OPMOD": "idiv", "OPLEA": "lea"}
CC = {"e": "e", "z": "e", "ne": "ne", "nz": "ne", "l": "l", "nge": "l", "ge": "ge", "nl": "ge", "g": "g", "nle": "g",
      "le": "le", "ng": "le", "b": "b", "c": "b", "nae": "b", "ae": "ae", "nb": "ae", "nc": "ae", "a": "a",
      "nbe": "a", "be": "be", "na": "be", "s": "s", "ns": "ns", "p": "p", "np": "np"}


# ------------------------------------------------------------------------------------------------ machine code side

def split_ops(s):
    out, d, cur = [], 0, ""
    for ch in s:
        d += ch == "["
        d -= ch == "]"
        if ch == "," and d == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


FRAME = {"ebp": False}           # set per function by lift_code: does `ebp` hold the frame (`mov ebp, esp`)?


def akind(op):
    o = re.sub(r"^(byte|word|dword|qword|tbyte|xmmword)\s+ptr\s+", "", op.strip())
    if o in REGS or re.match(r"xmm\d$", o):
        return "R"
    if o.startswith("["):
        if re.match(r"\[\s*[er]sp\b", o) or (FRAME["ebp"] and re.match(r"\[\s*[er]bp\b", o)):
            return "R"                                         # a frame slot: a variable
        if re.match(r"\[\s*(0x[0-9a-f]+|\d+)\s*\]$", o):
            return "R"                                         # an absolute address: a global, a variable in the IL
        return "M"
    if re.match(r"^-?(0x[0-9a-f]+|\d+)$", o):
        return "C"
    if o.startswith("st("):
        return "S"
    return "X"


def canon_asm(text):
    """(token, cc) of one instruction `mnemonic operands` (capstone style), or None for a back-end artefact."""
    p = text.strip().split(None, 1)
    if not p:
        return None
    mn, ops = p[0], split_ops(p[1]) if len(p) > 1 else []
    if mn in ("rep", "repe", "repne") and len(p) > 1:
        return ("copy", None)
    mn = re.sub(r"^(lock)\s*", "", mn)
    k = [akind(o) for o in ops]
    if mn in ("nop", "int3", "ret", "leave", "npad", "DB", "db", "align") or mn.startswith("npad"):
        return None
    if mn.startswith("f") or mn.endswith(("ss", "sd")) and mn not in ("cmpss",) or mn.startswith(("cvt", "ucomis",
                                                                                                "comis")):
        return canon_float(mn, ops, k)
    if mn in ("movs", "movsd", "movsb", "movsw", "stosd", "stos", "stosb") or mn.startswith("rep"):
        return ("copy", None)
    if mn == "mov" or mn in ("movzx", "movsx", "movsxd"):
        if k[0] == "M":
            return ("st:" + k[1], None)
        if k[1] == "M":
            return ("ld", None)
        if k[1] == "C":
            return ("movi", None)
        return None                                            # a copy
    if mn == "xor" and len(ops) == 2 and ops[0] == ops[1]:
        return ("movi", None)
    if mn == "test" and len(ops) == 2 and ops[0] == ops[1]:
        return ("cmp:C,R", None)
    if mn in ("cmp", "test"):
        return ("cmp:" + ",".join(sorted(k)), None)
    if mn.startswith("j") and mn != "jmp":
        return ("jcc", CC.get(mn[1:], mn[1:]))
    if mn == "jmp":
        return None                                            # block layout
    if mn == "call":
        return ("call", None)
    if mn == "push":
        return ("push:" + k[0], None)
    if mn == "pop":
        return None
    if mn == "lea":                                            # `&local` for an argument: the IL's OPARG carries it;
        if len(ops) == 2 and re.match(r"\[\s*([er]sp|[er]bp)\b", ops[1]):   # otherwise an addition
            return None
        return ("add:R,R", None)
    if mn.startswith("set") or mn.startswith("cmov"):
        return (mn[:3] if mn.startswith("set") else "cmov", None)
    if mn in ("inc", "dec"):
        mn, k = ("add" if mn == "inc" else "sub"), k + ["C"]
    if mn in ("add", "sub") and len(ops) == 2 and k[1] == "C" and (ops[1].startswith("-") or
                                                                   re.match(r"0xf{4,}", ops[1])):
        mn = "sub" if mn == "add" else "add"                   # add eax, -1 is sub 1
    if mn in ARITH:
        if k and k[0] == "M":
            return ("rmw.%s:%s" % (mn, ",".join(k[1:])), None)
        src = k[1:] if len(k) > 1 else []
        if mn == "imul" and len(k) == 3:
            src = k[2:]                                        # imul r, r/m, imm: r = r/m * imm
        srcs = ["R"] + src if mn not in ("neg", "not", "cdq") else ["R"]
        if mn in COMM:
            srcs = sorted(srcs)
        return ("%s:%s" % (mn, ",".join(srcs)), None)
    if mn.startswith("f"):
        return ("x87." + mn, None)
    return (mn, None)


FOPS = {"add": "add", "sub": "sub", "subr": "sub", "mul": "mul", "div": "div", "divr": "div"}


def canon_float(mn, ops, k):
    """x87 and SSE scalar code, coarse: stores to memory (`f.st`), arithmetic (`f.add` ..), compares (`f.cmp`),
    conversions (`f.cvt`); loads and stack shuffles are left out (a load folds into an operation or not)."""
    m = re.match(r"f(i?)(add|subr?|mul|divr?)p?$", mn)
    if m:
        return ("f." + FOPS[m.group(2)], None)
    if mn in ("xorps", "xorpd", "pxor") and len(ops) == 2 and ops[0] == ops[1]:
        return None                                            # a zero
    m = re.match(r"(add|sub|mul|div)(ss|sd)$", mn)
    if m:
        return ("f." + m.group(1), None)
    if re.match(r"f(i?)(st|stp)$|fist(t?)p$", mn):
        return ("f.st", None) if k and k[0] == "M" else None
    if mn in ("movss", "movsd") and k and k[0] == "M":
        return ("f.st", None)
    if re.match(r"f(u?)com(i?)(p{0,2})$|(u?)comis[sd]$|ftst$", mn):
        return ("f.cmp", None)
    if mn in ("fchs",):
        return ("f.neg", None)
    if mn in ("fsqrt", "fabs", "fsin", "fcos", "fpatan", "fptan", "frndint", "fscale", "f2xm1", "fyl2x"):
        return ("f." + mn[1:], None)
    if mn.startswith("cvt"):
        return ("f.cvt", None)
    return None                                                # fld, fxch, fnstsw, fstp st(i), movss xmm, [m]


def prologue_epilogue64(texts):
    """x64: the saves of callee-saved registers in their home slots (`mov qword ptr [rsp + 8], rbx` before the
    frame is set up) and their restores (`mov rbx, qword ptr [rsp + N]` after the frame's release or before a ret),
    push / pop of callee-saved registers, `sub rsp, N` in the prologue, `add rsp, N` before a return or tail jump."""
    drop = set()
    n = len(texts)
    for j, t in enumerate(texts):
        t = t.strip()
        if j < 8 and re.match(r"mov qword ptr \[rsp \+ (?:8|0x10|0x18|0x20)\], %s$" % _SAVED64, t):
            drop.add(j)
        elif re.match(r"(push|pop) %s$" % _SAVED64, t):
            drop.add(j)
        elif re.match(r"sub rsp, \S+$", t) and j < 10:
            drop.add(j)
        elif re.match(r"add rsp, \S+$", t):
            drop.add(j)
        elif re.match(r"mov %s, qword ptr \[rsp \+ [^\]]+\]$" % _SAVED64, t) and any(
                re.match(r"(ret|jmp|add rsp|pop)", texts[k].strip()) for k in range(j + 1, min(n, j + 4))):
            drop.add(j)
    return drop


def prologue_epilogue(texts):
    """Indices of the back end's own instructions: frame setup and teardown, the saves and restores of callee-saved
    registers (as many pushes of a register as there are pops of it, earliest first: a save need not be the first
    instruction, `mov eax, [esp+8]; push esi`), and the stack adjustment after a call."""
    drop = set()
    pops = collections.Counter(m.group(1) for t in texts for m in [re.match(r"pop (e[bcs]x|esi|edi|ebp)$", t.strip())]
                               if m)
    for j, t in enumerate(texts):
        t = t.strip()
        m = re.match(r"push (e[bcs]x|esi|edi|ebp)$", t)
        if m and pops[m.group(1)] > 0:
            pops[m.group(1)] -= 1
            drop.add(j)
        elif re.match(r"(pop (e[bcs]x|esi|edi|ebp)|mov ebp, esp|mov esp, ebp|leave|and esp, \S+)$", t):
            drop.add(j)
        elif re.match(r"sub esp, \S+$", t) and j < 6:
            drop.add(j)
        elif re.match(r"add esp, \S+$", t):
            drop.add(j)                                        # after a call, or the frame's release
    return drop


def norm(tok):
    """The comparison token: a constant and a register are one kind (c2 keeps some constants in registers: the
    IL's immediate candidates), so constant moves are copies; stores and arguments are one token whatever they write;
    `sar` / `shr` and `div` forms by the type, which the token does not carry."""
    if tok is None:
        return None
    op, _, ks = tok.partition(":")
    ks = ",".join(sorted(k.replace("C", "R").replace("F", "R") for k in ks.split(","))) if ks else ""
    if op in ("movi", "cdq"):
        return None
    if op in ("st", "push", "f.st"):
        return "st" if op != "push" else op                   # a float constant may be stored as an integer
    op = {"sar": "shr", "idiv": "div", "mul": "imul"}.get(op, op)
    if op in ("div",):
        return "div"
    if op.startswith("rmw."):
        r = op[4:]
        return "rmw." + {"sar": "shr", "idiv": "div"}.get(r, r)
    return op + (":" + ks if ks else "")


def expand(tok):
    """A token with memory sources as explicit loads: `add:M,R` -> `ld`, `add:R,R` (whether a load folds into its
    use is instruction selection's choice, on either side)."""
    if not tok or tok.startswith(("ld", "st", "push", "copy", "call", "jcc", "f.")) or ":" not in tok:
        return [tok] if tok else []
    op, _, ks = tok.partition(":")
    k = ks.split(",")
    n = k.count("M")
    return ["ld"] * n + ([op + ":" + ",".join(sorted("R" if x == "M" else x for x in k))] if n else [tok])


def contract_rmw(texts):
    """{index of the load: 'rmw.op', index of the op: None, index of the store: None} for a load, an operation on the
    loaded register and a store of it back to the same memory operand within 7 instructions: 13.10's Riscify split of
    a read-modify-write (regalloc.md 6), and `x = x | 1` written as three instructions anywhere, both `rmw.op` like
    `or [m], 1` and Opeq's IL tuple."""
    out = {}
    for i, t in enumerate(texts):
        m = re.match(r"mov (e[a-d]x|esi|edi|ebp), (dword ptr \[[^\]]+\])$", t.strip())
        if not m or akind(m.group(2)) != "M":
            continue
        r, mem = m.group(1), m.group(2)
        op = None
        for j in range(i + 1, min(len(texts), i + 8)):
            u = texts[j].strip()
            if op is None:
                mm = re.match(r"(add|sub|and|or|xor|shl|shr|sar|inc|dec|neg|not) %s\b" % r, u)
                if mm:
                    op = (j, {"inc": "add", "dec": "sub"}.get(mm.group(1), mm.group(1)))
                    continue
                if re.search(r"\b%s\b" % r, u):
                    break
            elif u == "mov %s, %s" % (mem, r):
                out[i] = "rmw." + op[1]
                out[op[0]] = out[j] = None
                break
            elif re.search(r"\b%s\b" % r, u):
                break
    return out


def lift_code(texts):
    """[(index, token, cc)] of a function's instructions (capstone text, in address order). A jcc right after an
    arithmetic instruction (the flags of `add eax, -1` tested, the compare with 0 removed by a peephole) gets the
    compare it stands for."""
    FRAME["ebp"] = any(t.strip() in ("mov ebp, esp", "mov rbp, rsp") for t in texts)
    drop = prologue_epilogue64(texts) if X64 else prologue_epilogue(texts)
    args = {}                                                  # x64: argument register -> its last write
    rmw = contract_rmw(texts)
    out, ecx = [], None
    for i, t in enumerate(texts):
        if i in drop:
            continue
        if i in rmw:
            if rmw[i]:
                out.append((i, rmw[i], None))
            continue
        tt = t.strip()
        if X64:
            m = re.match(r"(mov|lea|xor|movsxd|movzx|movsx|or) (\w+), ", tt)
            if m and _A64.get(m.group(2)):
                args[_A64[m.group(2)]] = i
            if tt.startswith("call"):                          # each argument register set for the call: one OPARG
                for r in ARGREGS:
                    if r in args:
                        out.append((args[r], "push", None))
                args = {}
            if re.match(r"mov \w+ ptr \[rsp \+ (0x2[0-9a-f]|0x[3-9a-f][0-9a-f]|0x[1-9a-f][0-9a-f]{2,})\], ", tt) and \
                    any(texts[k].strip().startswith("call") for k in range(i + 1, min(len(texts), i + 12))):
                out.append((i, "push", None))                  # a stack argument above the home area
                continue
        elif re.match(r"(mov|lea|xor) ecx, ", tt):
            ecx = i                                            # `this` (thiscall) or a fastcall argument, maybe;
        elif re.match(r"rep", tt):                             # a constant too (a global object's address)
            ecx = None                                         # a count, consumed
        if tt.startswith("call") and ecx is not None:
            out.append((ecx, "push", None))                    # the IL's last OPARG before a thiscall
            ecx = None
        c = canon_asm(t)
        c = c and (norm(c[0]), c[1])
        if not c or not c[0]:
            continue
        for x in expand(c[0])[:-1]:
            out.append((i, x, None))
        c = (expand(c[0])[-1], c[1])
        if c[0] == "jcc":                                      # the last flag-setting token before the branch
            last = next((x[1] for x in reversed(out) if not re.match(r"(ld|st$|push$|f\.st)", x[1])), "")
            if re.match(r"(add|sub|and|or|xor|neg|shl|shr|sar|rmw\.)", last):
                out.append((i, "cmp:R,R", None))
        out.append((i, c[0], c[1]))
    return out


# ---------------------------------------------------------------------------------------------------------- IL side

def ikind(tok):
    import feshape
    k = feshape.kind(tok)
    if k is None:
        return None
    base, _, ty = k.partition(":")
    if base in ("T", "V", "K") and base == "K":
        return "C"
    if base in ("T", "V"):
        return "R"
    if base in ("C", "M", "F", "L"):
        return base
    return None                                               # cc, esp, regset: implicit


def il_operands(side):
    """Operand tokens of one side of a dump line, with the address registers of memory operands left out."""
    toks = side.split()
    inner = set()
    for t in toks:
        if t.startswith("[") or "=[" in t:
            inner.update(re.findall(r"([A-Za-z_]\w*):[0-9a-f]+", t.split("]")[0]))
    out = []
    for t in toks:
        name = re.match(r"(?:\w+=)?([^:\[]+)", t)
        if not t.startswith("[") and "=[" not in t and name and name.group(1) in inner:
            continue
        out.append(t)
    return out


def canon_il(line):
    """(token, cc, source line) of one `I` line of an IL dump at Lower's entry, or None."""
    s = line.strip()
    if not s.startswith("I "):
        return None
    f = s.split(None, 5)
    op, src_line = f[2], int(f[3]) if f[3].lstrip("-").isdigit() else None
    rest = f[5] if len(f) > 5 else ""
    rest = rest.split("|", 1)[1] if "|" in rest else ""
    rest, _, notes = rest.partition(" ; ")
    d, _, sr = rest.partition("<-")
    dk = [k for k in (ikind(t) for t in il_operands(d)) if k]
    sk = [k for k in (ikind(t) for t in il_operands(sr)) if k]
    cc = re.search(r"(?:^| )cc=(\w+)", notes)
    cc = CC.get(cc.group(1), cc.group(1)) if cc else None
    ty = re.findall(r":([0-9a-f]{4})\b", rest)
    flt = any(t[0] in "45" for t in ty)                       # float types: 5xxx on 14.00 / 15.00, 4xxx on 13.10
    if op == "OPBRANCH":
        return ("jcc", cc, src_line)
    if op == "OPGOTO":
        return None                                           # block layout
    if op == "OPMBASSIGN":
        return ("copy", None, src_line)
    if op in ("OPCALL",):
        return ("call", None, src_line)
    if op == "OPARG":
        k0 = sk[0] if sk else "R"
        return ("push:" + ("C" if k0 in ("F", "L") else k0), None, src_line)    # an address: an immediate
    if op == "OPCMP" or op in ("cmp", "test"):
        if flt:
            return ("f.cmp", None, src_line)
        return ("cmp:" + ",".join(sorted(sk[:2])), None, src_line)
    if op in ("OPASSIGN", "mov", "movzx", "movsx", "OPCONVERT"):
        if flt:
            if op == "OPCONVERT" and len(set(t[0] for t in ty if t[0] in "12345")) > 1:
                return ("f.cvt", None, src_line) if any(t[0] in "12" for t in ty[:1]) else None
            return ("f.st", None, src_line) if dk and dk[0] == "M" else None
        if dk and dk[0] == "M":
            return ("st:" + (sk[0] if sk else "R"), None, src_line)
        if "M" in sk:
            return ("ld", None, src_line)
        if sk and sk[0] == "C":
            return ("movi", None, src_line)
        return None
    if op in ("lea", "OPLEA"):
        return ("lea", None, src_line)                         # lift_il decides: an address or an addition
    mn = ILOP.get(op, op)
    if mn in ("inc", "dec"):
        mn, sk = ("add" if mn == "inc" else "sub"), sk + ["C"]
    if mn in ARITH:
        if flt:
            return ("f." + {"imul": "mul", "idiv": "div"}.get(mn, mn), None, src_line)
        if dk and dk[0] == "M":
            srcs = [k for k in sk if k != "M"] or ["R"]
            return ("rmw.%s:%s" % (mn, ",".join(srcs[-1:])), None, src_line)
        srcs = sk[:2] if mn not in ("neg", "not") else ["R"]
        if len(srcs) == 1 and mn not in ("neg", "not"):
            srcs = ["R"] + srcs
        if mn in COMM:
            srcs = sorted(srcs)
        return ("%s:%s" % (mn, ",".join(srcs)), None, src_line)
    if op.startswith("OP"):
        return None                                           # OPBLKSTART, OPRET, OPPHI, ...
    return (op, None, src_line)


def lea_is_value(lines, i):
    """Is the result of the `lea` at lines[i] used as a value later (a plain operand), not only inside brackets?"""
    m = re.search(r"\|\s*(\w+):[0-9a-f]+\s*<-\s*\[([^\]]*)\]", lines[i])
    if not m or re.fullmatch(r"\w+:[0-9a-f]+\+0", m.group(2).strip()):
        return False                                           # `&x`: an address, not an addition
    name = m.group(1)
    for l in lines[i + 1:i + 40]:
        rest = l.split("|", 1)[1] if "|" in l else ""
        rest = rest.split(" ; ")[0]
        d, _, sr = rest.partition("<-")
        for t in il_operands(d) + il_operands(sr):          # the address registers of [..] operands left out
            if not t.startswith("[") and re.match(r"(?:\w+=)?%s:" % re.escape(name), t):
                return True
    return False


def lift_il(lines):
    out = []
    for i, l in enumerate(lines):
        c = canon_il(l)
        if c and c[0] == "lea":
            c = ("add:R,R", None, c[2]) if lea_is_value(lines, i) else None
        t = c and norm(c[0])
        for x in expand(t):
            out.append((i, x, c[1], c[2]))
    return out


# -------------------------------------------------------------------------------------------------------- alignment

def align(a, b):
    """difflib opcodes between two token lists."""
    return difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()


def match(a, b, window=8):
    """{index in a: index in b}: the sequence alignment, then each still unmatched token paired with an equal unmatched
    one within `window` positions of where the alignment puts it (the scheduler moved it: a load issued before a
    store of the same block)."""
    pairs, where = {}, {}
    for tag, i1, i2, j1, j2 in align(a, b):
        for k, i in enumerate(range(i1, i2)):
            where[i] = j1 + min(k, max(j2 - j1 - 1, 0))
            if tag == "equal":
                pairs[i] = j1 + k
    used = set(pairs.values())
    for i in range(len(a)):
        if i in pairs:
            continue
        w = where.get(i, i)
        best = None
        for j in range(max(0, w - window), min(len(b), w + window + 1)):
            if j not in used and b[j] == a[i] and (best is None or abs(j - w) < abs(best - w)):
                best = j
        if best is not None:
            pairs[i] = best
            used.add(best)
    return pairs


def agreement(a, b):
    m = len(match(a, b))
    return m / max(len(a), len(b), 1), m


def runs(a, b, pairs):
    """The unmatched stretches: [(a indices, b indices)], each a's run with the b tokens between its neighbours'
    partners."""
    out, i, n = [], 0, len(a)
    inv = {j: i for i, j in pairs.items()}
    jprev = -1
    while i < n:
        if i in pairs:
            jprev = max(jprev, pairs[i]); i += 1
            continue
        k = i
        while k < n and k not in pairs:
            k += 1
        jnext = next((pairs[q] for q in range(k, n) if q in pairs), len(b))
        out.append((list(range(i, k)), [j for j in range(jprev + 1, jnext) if j not in inv]))
        i = k
    claimed = {j for _a, bs in out for j in bs}
    rest = [j for j in range(len(b)) if j not in inv and j not in claimed]
    grp = []
    for j in rest:                                             # b tokens between two matched a tokens, by run
        if grp and j == grp[-1] + 1:
            grp.append(j)
        else:
            if grp:
                out.append(([], grp))
            grp = [j]
    if grp:
        out.append(([], grp))
    return out


def near(pairs, j):
    """The a index paired with the nearest matched b token before b index j (or after it)."""
    inv = {jj: i for i, jj in pairs.items()}
    before = [jj for jj in inv if jj < j]
    after = [jj for jj in inv if jj > j]
    if after:                                                  # a load and an op belong to the store after them
        return inv[min(after)]
    return inv[max(before)] if before else None


# ---------------------------------------------------------------------------------------------------------- traces

def listing_lines(asm, func):
    """{offset: source line} of the function's instructions, from the listing's `; N :` markers."""
    import c2trace
    text = open(asm, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)
    out, cur = {}, None
    for ln in text[i:j].splitlines():
        m = re.match(r";\s*(\d+)\s*:", ln)
        if m:
            cur = int(m.group(1)); continue
        m = re.match(r"\s+([0-9a-f]{5})\t", ln)
        if m and cur is not None:
            out.setdefault(int(m.group(1), 16), cur)
    return out


def lower_va():
    """Lower's address: the build table's `frontend.lower` pass (13.00.9044: @Lower@4), else the Lower symbol."""
    import c2trace, icount
    from image import Image
    img = Image("c2")
    fe = c2trace.c2build.get("frontend") or {}
    if fe.get("lower"):
        row = c2trace.c2build.get("functions", {}).get(fe["lower"])
        r = img.by_sym.get(row[3]) if row and len(row) > 3 else None      # the table row's PDB name
        if r:
            try:
                c2trace.parse_hook(img, "%x" % r["va"])
                return r["va"]
            except SystemExit:
                pass
    got = dict((n, va) for n, va, _ in icount.resolve(img, ["Lower"]))
    if "Lower" not in got:
        raise RuntimeError("Lower is not hookable on this build")
    return got["Lower"]


def il_spec():
    """The IL dump spec at Lower's entry: the table's (13.00.9044: the plain dump plus its front-end probe hooks),
    extended on the x64 builds that keep il_spec plain."""
    import c2trace, c2il
    return c2il.SPEC + ("+" if c2trace.c2build.get("arch") == "x64" and c2il.SPEC.endswith("%") else "")


def trace(src, func, cl):
    """(IL lines at Lower's entry, our final code as capstone texts, the source line of each instruction) from one
    traced compile."""
    import tempfile, shutil, tsweep, c2trace, c2il
    va = lower_va()
    keep = tempfile.mkdtemp(prefix="lift-")
    try:
        _i, hk, rc, out, log = c2trace.run_trace(src, ["%x%s" % (va, il_spec())], cl, keep=keep, quiet=True,
                                                 func=func)
        il = None
        for _pname, lines, _fname in c2il.parse(log, hk, func):
            il = lines
            break
        if il is None:
            raise RuntimeError("function %r not seen at Lower (rc %s)" % (func, rc))
        asm = next((os.path.join(keep, f) for f in os.listdir(keep) if f.endswith(".asm")), None)
        if not asm:
            raise RuntimeError("no listing")
        code = tsweep.listing_cs_offsets(asm, func)
        srcl = listing_lines(asm, func)
    finally:
        shutil.rmtree(keep, ignore_errors=True)
    offs = sorted(code)
    return il, [code[o] for o in offs], [srcl.get(o) for o in offs]


def read_orig(path):
    out = []
    for l in open(path, errors="replace"):
        p = l.split(None, 1)
        if len(p) == 2 and re.match(r"^[0-9a-fA-F]+$", p[0]):
            out.append(p[1].strip())
    return out


# ---------------------------------------------------------------------------------------------------------- reports

def report(il, ours, ours_lines=None, orig=None, show=False, src_lines=None):
    I = lift_il(il)
    O = lift_code(ours)
    ti, to = [x[1] for x in I], [x[1] for x in O]
    pairs = match(ti, to)
    ag = len(pairs) / max(len(ti), len(to), 1)
    base = collections.Counter(ours_lines[O[j][0]] - I[i][3] for i, j in pairs.items()
                               if ours_lines and I[i][3] and ours_lines[O[j][0]]).most_common(1)
    base = base[0][0] if base else 0
    print("our IL at Lower's entry: %d tokens; our code lifted: %d tokens; self-check %.0f%% (%d matched)" % (
        len(ti), len(to), 100 * ag, len(pairs)))
    unI, unO = [], []
    for ia, jb in runs(ti, to, pairs):
        unI += [ti[i] for i in ia]
        unO += [to[j] for j in jb]
        if show and (ia or jb):
            print("  self   IL %s | code %s" % (" ".join(ti[i] for i in ia) or "-", " ".join(
                "%s(%s)" % (to[j], ours[O[j][0]]) for j in jb) or "-"))
    res = dict(self=ag, il_tokens=len(ti), code_tokens=len(to), unmatched_il=unI, unmatched_code=unO)
    if orig is None:
        return res
    R = lift_code(orig)
    tr = [x[1] for x in R]
    # the original against our IL THROUGH our code: the two codes come from one compiler, so their alignment follows
    # the real instruction order (two equal stores are not swapped), and the self-check ties our code to our IL
    p3 = match(to, tr)
    p2 = {i: p3[j] for i, j in pairs.items() if j in p3}
    for i, j in match(ti, tr).items():                     # IL tokens our code does not give back: directly
        if i not in pairs and i not in p2 and j not in set(p2.values()):
            p2[i] = j
    ag2 = len(p2) / max(len(ti), len(tr), 1)
    print("original's code lifted: %d tokens; against our IL %.0f%% (%d matched)" % (len(tr), 100 * ag2, len(p2)))
    # an original token that pairs with one of OUR code's tokens which the self-check left without IL is a gap of
    # the rules (our code has it too), not a difference of the sources: left out
    self_un = {j for j in range(len(to)) if j not in set(pairs.values())}
    gap = {jj for j, jj in p3.items() if j in self_un}
    diffs, hidden = [], 0
    for ia, jb in runs(ti, tr, p2):
        hidden += sum(1 for j in jb if j in gap) + sum(1 for i in ia if i not in pairs)
        jb = [j for j in jb if j not in gap]
        ia = [i for i in ia if i in pairs]                     # our IL token our own code lacks too: a rule gap
        if not ia and not jb:
            continue
        ln = sorted({I[i][3] + base for i in ia if I[i][3]})
        approx = False
        if not ia and jb:
            k = near(p2, jb[0])
            if k is not None and I[k][3]:
                ln, approx = [I[k][3] + base], True
        diffs.append(dict(lines=ln, near=approx, ours=[ti[i] for i in ia], orig=[tr[j] for j in jb],
                          orig_code=[orig[R[j][0]] for j in jb]))
    diffs.sort(key=lambda d: (d["lines"][0] if d["lines"] else 1 << 30))
    for d in diffs:
        where = ("%sline %s" % ("~" if d["near"] else "", ",".join(map(str, d["lines"])))) if d["lines"] else "-"
        txt = (src_lines[d["lines"][0] - 1].strip()[:64] if src_lines and d["lines"] and
               0 < d["lines"][0] <= len(src_lines) else "")
        print("%-11s ours %-26s orig %-26s %s" % (where, " ".join(d["ours"])[:26] or "-",
                                                 " ".join(d["orig"])[:26] or "-", ("`%s`" % txt) if txt else ""))
        if d["orig_code"]:
            print("            original: %s" % "; ".join(d["orig_code"])[:140])
    if hidden:
        print("(%d tokens left out: our own code shows the same token unlifted, a gap of the rules)" % hidden)
    res.update(orig=ag2, diffs=diffs, hidden=hidden)
    return res


def main(argv):
    import c2trace
    if argv and argv[0] == "selfcheck":
        return selfcheck(argv[1:])
    ap = argparse.ArgumentParser(prog="lift.py", usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    ap.add_argument("--asm-vs")
    ap.add_argument("--show", action="store_true", help="the self-check's unmatched tokens")
    ap.add_argument("--json")
    argv, cl = c2trace.split_cl(argv)
    a = ap.parse_args(argv)
    il, ours, ours_lines = trace(a.src, a.func, cl)
    orig = read_orig(a.asm_vs) if a.asm_vs else None
    try:
        src_lines = open(a.src, errors="replace").read().splitlines()
    except OSError:
        src_lines = None
    r = report(il, ours, ours_lines, orig, a.show, src_lines)
    if a.json:
        json.dump(r, open(a.json, "w"), indent=1)
    return 0


def selfcheck(argv):
    ap = argparse.ArgumentParser(prog="lift.py selfcheck")
    ap.add_argument("--rows", required=True)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    import subprocess
    from concurrent.futures import ThreadPoolExecutor
    rows = [json.loads(l) for l in open(a.rows) if l.strip()]

    def one(r):
        import tempfile
        js = os.path.join(a.out or tempfile.gettempdir(), "lift_%s.json" % r.get("id", abs(hash(r["func"]))))
        cmd = [sys.executable, os.path.abspath(__file__), r["src"], "--func", r["func"], "--show", "--json", js]
        if r.get("orig"):
            cmd += ["--asm-vs", r["orig"]]
        env = dict(os.environ, C2_BUILD=r.get("c2", ""))
        p = subprocess.run(cmd + ["--"] + r["flags"], capture_output=True, text=True, env=env)
        return r, p.stdout, p.stderr, js

    unI, unO, ags = collections.Counter(), collections.Counter(), []
    if a.out:
        os.makedirs(a.out, exist_ok=True)
    with ThreadPoolExecutor(a.jobs) as ex:
        for r, out, err, js in ex.map(one, rows):
            m = re.search(r"self-check (\d+)%", out)
            if not m or not os.path.exists(js):
                print("%-14s failed: %s" % (r.get("id"), (err.strip().splitlines() or ["?"])[-1][:120]))
                continue
            res = json.load(open(js))
            ags.append(int(m.group(1)))
            unI.update(res.get("unmatched_il", []))
            unO.update(res.get("unmatched_code", []))
            o = re.search(r"against our IL (\d+)%", out)
            print("%-14s self %3s%%  orig %s" % (r.get("id"), m.group(1), o.group(1) + "%" if o else "-"))
            if a.out:
                open(os.path.join(a.out, "%s.txt" % r.get("id")), "w").write(out)
    if ags:
        ags.sort()
        print("\n%d functions: self-check median %d%%, mean %.0f%%, worst %s" % (len(ags), ags[len(ags) // 2],
                                                                          sum(ags) / len(ags), ags[:5]))
    print("IL tokens the code did not give back:", ", ".join("%s %d" % kv for kv in unI.most_common(a.top)))
    print("code tokens with no IL token:", ", ".join("%s %d" % kv for kv in unO.most_common(a.top)))
    return 0


if __name__ == "__main__":
    import c2trace
    sys.exit(c2trace.pipe_safe(main, sys.argv[1:]))
