#!/usr/bin/env python3
"""Register-allocation report for one function, from a traced compile.  Doc: docs/c2/tracer.md, docs/c2/regalloc.md

usage: c2trace.py regs <src> [--func SUBSTR] [--all-classes] [--raw] [--keep DIR] [-- <cl args>]

For every function whose decorated name contains SUBSTR (default: all), prints the globregs (register candidates:
user variables, parameters, multi-def temporaries) in the order c2's Color loop pops them off its priority list,
with the numbers the decision is made from:

  prio    GLOBREG.prio (+0x0c): Σ_blocks (cost of refs in block)·P_B·w_B − Σ_live-through P_B·w_B (regalloc.md §3.2)
  benefit GLOBREG.benefit (+0x40): Σ cost·w; <= 0 means "spill" (with one /O2 exception, §3.1)
  refs    GLOBREG.nrefs (+0x24)
  def     GLOBREG.defOrd (+0x44), the tie-break among equal prio: the LARGER def is popped first, in every
          register class (int, x87, XMM) and on all three c2 builds (InsertInList sorts by prio descending, then
          defOrd descending; 13.10's x87 list, FPAddToPriorityList, compares the same way). defOrd numbers the
          instructions with the blocks in list order and each block walked backwards, so inside one block the
          value defined EARLIER has the larger def and goes first, and across blocks the value defined in the
          LATER block goes first. Same prio and same def (immediates: def 0; split pieces: the parent's def): the
          web inserted last goes first. A web with benefit <= 0 is not in this order: it is deferred
          (InsertByBenefit) and pops a second time showing its list neighbour's prio.
          Probe: tests/data/tieorder.c (regalloc.md section 3.4).
  src     (--why) source line offsets of the block's instructions, counted from the function's first line
          (the line with its name; offset 0 also covers compiler-made code such as the entry)
  cands   the registers ChoosePhysReg considered (only free ones are tried) with their preference weight;
          the lowest weight wins, ties go to the earlier one in eax ecx edx esi edi ebx ebp

Globregs are popped in priority order, so a variable that should have got a register another one got must
outrank it: compare their prio (and the per-block terms that make it up), not the source order. Short-lived
single-def temporaries are not globregs: regasg's scratch pass gives them eax/ecx/edx later (regalloc.md §4).
"""
import argparse, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace

REGNAME = "10cec7b8"
WEIGHTS = "10d14f10"
GR = "ebp>0xc,ebp>0x40,ebp>0x24,ebp>0x44,ebp>0x1c,ebp>0>0x1c,ebp>0>4,ebp>0>0>4$,ebx"
HOOKS = [
    "@Color@4=g10d123ac>0>0>4$,g10d123ac>0>0x44,g10d123ac>0>8>0>0x1c%",  # 0 function, base line, IL dump
    "@OrderGlobregs@12=s1",                                        # 1 class about to be coloured
    "@Color@4+0x401=ebp," + GR,                                    # 2 10b0d341: gr just popped (ebp)
    "@ChoosePhysReg@12=s1/xa1,a1>0x10>0x1c~%s$" % REGNAME,          # 3 colour chosen
    "@ChoosePhysReg@12+0x279=esi~%s$,esi~%s" % (REGNAME, WEIGHTS),  # 4 one candidate register and its weight
    "@SpillOrDefer@12=ecx",                                        # 5 spilled (or deferred: popped again later)
    "@HandleCurrUncolorable@8=s1",                                 # 6 no register left
    "@Spillcosts@12",                                              # 7 a priority pass starts
    "10b11487=edi,ebp,s7",                                         # 8 end of block B: P_B*w_B, w_B, BB*
    "10b11495=eax,eax>0x18,esi,eax>0>0>4$,eax>0>0x1c,eax>0>4",                        # 9 gr died in B: prio += blkcost*P*w
    "10b114c4=eax,eax>0x18,esi,eax>0>0>4$,eax>0>0x1c,eax>0>4",                        # 10 gr live out of B, referenced: same
    "10b114d2=eax,esi,eax>0>0>4$,eax>0>0x1c,eax>0>4",                                 # 11 gr live through B, unreferenced: prio -= P*w
    "10b11ae9=edx",                                                # 12 RemoveSplitGlobregs: gr (fl5 & 2) unlinked
]
_HOOKS14 = HOOKS
HOOKS = c2trace.c2build.hooks("regs", HOOKS)     # another c2 build: the same 13 events at its addresses
# Register choice (regalloc.md section 4): what ChoosePhysReg reads, per colouring.  14.00.50727.762 only; a ported
# build's table has only the 13 events above, and parse/export then simply leave the choice fields out.
_NAME = "%s>0>0>4$,%s>0>0x1c,%s>0>4"
_AV = "%s>0x1c,%s>0x20>0>0,%s>0x20>0>4,%s>0x20>0>8"          # web number; avail SBV first chunk: base, lo, hi
_PF = lambda b, n: ",".join("%s>0x38%s>4>0x1c,%s>0x38%s>8" % (b, ">0" * k, b, ">0" * k) for k in n)
_PE = "%s,%s>0x1c,%s,ebp,ebp>4,ebp>0x1c,%s>0x10"
CHOICE_HOOKS = [
    "@ChoosePhysReg@12+0x62=ebx," + _AV % (("ebx",) * 4) + ",ebx>0x40,ebx>0x10," + _NAME % (("ebx",) * 3),
                                                                   # 13 10b14bd2: one interfering neighbour n
    "@ChoosePhysReg@12+0x73=ebx,esi>4>0x1c,esi>8",                 # 14 10b14be3: one PREF of n (reg, count)
    "@ChoosePhysReg@12+0x1cf=s8," + _AV % (("s8",) * 4),           # 15 10b14d3f: gr's avail when it is coloured
    "@ChoosePhysReg@12+0x1e0=eax>4>0x1c,eax>8",                    # 16 10b14d50: one PREF of gr (reg, count)
    "@ChoosePhysReg@12+0x5e2=esi,esi>0x1c,esi>0x10",               # 17 10b15152: one _UpdatePrefsBv partner
    "@InsertInList@8=eax," + _AV % (("eax",) * 4) + ",eax>0x40," + _PF("eax", range(3)),
                                                                   # 18 gr enters the priority list: avail, prefs 1-3
    "@InsertInList@8+0x6=eax," + _PF("eax", range(3, 7)),          # 19 10b11976: prefs 4-7
    "@InsertByBenefit@8",                                          # 20 a deferred web is re-inserted
    # 21..26: Spillcosts gives globreg G a preference (PREF count += 1).  Logged: G, web number, register, the other
    # side S of the copy (a physical register's REGSYM, kind 1, or a coloured globreg, kind 2), S's kind word, S's
    # number, G's colour (the event counts only when that is 0).
    _PE % ("esi", "esi", "edi>0x1c", "esi"),         # 21 10b1082d use side: mov G, S   (S a coloured globreg)
    _PE % ("esi", "esi", "ebp>0x10>0x1c", "esi"),    # 22 10b10883 use side: lea G, [S] (S a coloured globreg)
    _PE % ("esi", "esi", "ebp>0x1c", "esi"),         # 23 10b10c31 use side: mov G, S   (S a physical register)
    _PE % ("esi", "esi", "edi>0x1c", "esi"),         # 24 10b10d9c def side: lea S, [G] (S a coloured globreg)
    _PE % ("ebx", "ebx", "ebp>0x10>0x1c", "ebx"),    # 25 10b10e5f def side: mov S, G   (S coloured; G new, fl5 & 1)
    _PE % ("ebx", "ebx", "ebp>0x1c", "ebx"),         # 26 10b11238 def side: mov S, G   (S physical; G new, fl5 & 1)
]
CHOICE_HOOKS = ["@Spillcosts@12+0x%x=%s" % (o, h) if h.startswith("e") else h
                for o, h in zip([0] * 8 + [0x40d, 0x463, 0x811, 0x97c, 0xa3f, 0xe18], CHOICE_HOOKS)]
PREF_SITE = {21: "mov G,gr", 22: "lea G,[gr]", 23: "mov G,reg", 24: "lea gr,[G]", 25: "mov gr,G", 26: "mov reg,G"}
if HOOKS is _HOOKS14:
    HOOKS = HOOKS + CHOICE_HOOKS
# Spill / defer / split decisions (regalloc.md section 3.6), for `--export` and `rasim check --predict` only; the
# report is unchanged.  14.00.50727.762 only (DEC0 is None on a ported build, and the snapshot then has no
# `decisions`).  Event k below is hook DEC0 + k.
_GX = "%s>4,%s>0x24,%s>0x28,%s>0x2c,%s>0x34"      # kind/fl5/fl6 dword, nrefs, firstBlk, lastBlk, nEnter
DEC_HOOKS = [
    "10b115b2=eax,eax>0x1c,eax>0x24,eax>0x40,eax>0x3c>4,eax>4," + _NAME % (("eax",) * 3),
                                                # 0 SpillObvious looks at one new globreg: nrefs, benefit, lastDef op
    "@Spill@8=s2,s2>0x1c",                      # 1 a globreg is spilled (SpillObvious, SpillOrDefer, ...)
    "@CmdSubstDef@12=s1/xa1",                   # 2 SpillObvious, nrefs == 2: try to fold the def into the use
    "@ColRemoveGlobregFromHashTable@8=edi,edi>0x1c",   # 3 a globreg record is retired (folded, substituted, spilled)
    "@RestrictToReg@4=s1/xa1",                  # 4 SpillOrDefer: pin to the preferred register?  (exit eax = 1: yes)
    "@ForceSingleBlockLives@4=s1",              # 5 SpillOrDefer, second time: cut at every block entry
    "@ForceNoAirlocks@4=s1",                    # 6 the same for a web with fl6 & 1
    "@BetterToDefer@4=eax,ecx/xeax",            # 7 a foldable def: re-queue behind its components, or fold (exit eax)
    "@FoldAwaySplitGlobreg@4=s1/xa1",           # 8 UpdateAffectedGlobregs: fold a web instead of splitting it
    "10b156de=esi",                             # 9 UpdateAffectedGlobregs: member of InsertSplitPoints' split set
    "10b158aa=esi",                             # 10 ... a byte-register neighbour left without a register
    "10b15ab2=esi",                             # 11 ... a neighbour left without a register
    "@InsertInList@8+0xd=eax," + _GX % (("eax",) * 5) + ",eax>0xc,eax>0x44,eax>0x3c>4," + _NAME % (("eax",) * 3),
                                                # 12 10b1197d: the record of a globreg entering the priority list
    "10b0d350=ebp," + _GX % (("ebp",) * 5) + ",ebp>0x20>0,ebp>0x3c>4,ebp>0x3c>4~10ce1e60,ebp>0x3c>0x2c>0x10,g10d0d71c>0",
                                                # 13 the record of the globreg just popped (after event 2); _Speed
    "@TryToRecalc@4=s1",                        # 14 rematerialise instead of reload
    "10b1788f=eax",                             # 15 Spill deleted a dead def: a web it used is re-split (_NeedNewLives)
    # 16..19 CmdUpdateCost 10c242d0: an instruction lost a register operand (its web was spilled or cut), so the
    # benefit of the other globreg in that instruction changes: the globreg and the amount, just before the add
    "10c2435a=esi,ebx", "10c24387=esi,ebx", "10c243c2=esi", "10c243f9=esi>0x1c,eax",
    "10c25147=esi,g10d0dd54>0>0>0,g10d0dd54>0>0>4,g10d0dd54>0>0>8",
                                                # 20 CmdProcessBytePressure: a queued web is restricted to the
                                                #    registers without a byte form (_UnByteAbleRegs)
    "10b148ec=s36,g10d0dd54>0>0>0,g10d0dd54>0>0>4,g10d0dd54>0>0>8",
                                                # 21 InsertSplitPoints: the same for the web being coloured
]
DEC0 = None
if len(HOOKS) == len(_HOOKS14) + len(CHOICE_HOOKS) and HOOKS[len(_HOOKS14):] == CHOICE_HOOKS:   # 14.00
    DEC0 = len(HOOKS)
    HOOKS = HOOKS + DEC_HOOKS


def _gx(v):
    """Fields of a GLOBREG record logged with _GX (v = the five values)."""
    w = v[0] if isinstance(v[0], int) else 0
    return dict(fl5=w >> 8 & 0xff, fl6=w >> 16 & 0xff, nrefs=v[1] if isinstance(v[1], int) else None,
                single=v[2] == v[3], nenter=v[4] & 0xff if isinstance(v[4], int) else None)


def _decision(cur, ev, k, f, v):
    """One decision event -> cur['dec'] (ordered), or extra fields on the last insert / pop row."""
    dec = cur.setdefault("dec", [])
    at = dict(npass=cur.get("_npass", 0), after_pop=len(cur["rows"]))
    op = lambda x: x if isinstance(x, int) else None
    if ev == "X":
        ret = int(f[4], 16)
        t = {2: "subst", 4: "restrict", 7: "btd", 8: "fold"}.get(k)
        for e in reversed(dec):
            if e["t"] == t and "ret" not in e:
                e["ret"] = ret
                if t == "subst":             # folded iff the record was retired inside the call
                    e["ok"] = any(r["t"] == "remove" and r["gr"] == e["gr"] for r in dec[dec.index(e):])
                break
        return
    if k == 0:
        dec.append(dict(at, t="so", gr=v[0], num=v[1], nrefs=v[2], ben=s32(v[3]), lastop=op(v[4]),
                        fl5=(v[5] >> 8 & 0xff) if isinstance(v[5], int) else None, name=_grname(*v[6:9])))
    elif k in (1, 3):
        dec.append(dict(at, t="spill" if k == 1 else "remove", gr=v[0], num=v[1]))
    elif k in (2, 4, 5, 6, 8, 14):
        dec.append(dict(at, t={2: "subst", 4: "restrict", 5: "force1", 6: "forceair", 8: "fold", 14: "recalc"}[k], gr=v[0]))
    elif k == 7:
        dec.append(dict(at, t="btd", gr=v[0], cls=v[1]))
    elif k in (9, 10, 11, 15):
        dec.append(dict(at, t="mark", site={9: "isp", 10: "nbr8", 11: "nbr", 15: "needlives"}[k], gr=v[0]))
    elif k in (16, 17, 18, 19):
        dec.append(dict(at, t="bencost", gr=v[0], delta=2 if k == 18 else s32(v[1])))
    elif k in (20, 21):
        dec.append(dict(at, t="bytepress", gr=v[0], regs=_bits(*v[1:4])))
    elif k == 12:
        ins = cur.get("inserts") or []
        if ins and ins[-1].get("gr") == v[0]:
            ins[-1]["x"] = dict(_gx(v[1:6]), prio=s32(v[6]), defOrd=v[7], lastop=op(v[8]), name=_grname(*v[9:12]),
                                kind=KIND.get(v[11] & 0xff, "k%d" % (v[11] & 0xff)) if isinstance(v[11], int) else None)
    elif k == 13:
        if cur["rows"] and cur["rows"][-1]["gr"] == v[0]:
            cur["rows"][-1]["x"] = dict(_gx(v[1:6]), avail_empty=v[6] == 0, lastop=op(v[7]), dope=op(v[8]),
                                        dst10=op(v[9]), speed=op(v[10]))


def _imm_specs(hooks):
    """Append the immediate-candidate specs to the pop event (2) and the three end-of-block events (9, 10, 11).
    An immediate candidate's SYM (kind 0xd) keeps a copy of the constant's leaf tuple at +0x28 (GrgRewriteImmedAsReg
    10b56700; the same offsets on 13.10 and 15.00): leaf +0x18 = the value (ICONST, kind 7) or the symbol (DATAADDR 3,
    CODEADDR 4), leaf +8 = kind byte, flags, type word.  Added after the existing specs, so their positions keep
    their meaning; IMMPOS[event] = index of the first added value.  The pop event has room for two specs only
    (12 per hook), so the symbol's name, read at the block events, is carried over by symbol pointer."""
    out, pos = list(hooks), {}
    for ev, full in ((2, False), (9, True), (10, True), (11, True)):
        if ev >= len(out) or "/x" in out[ev]:
            continue
        head, _, specs = out[ev].partition("=")
        sp = specs.split(",")
        sym = next((x[:-2] for x in sp if x.endswith(">0>4")), None)      # "<gr>>0" = the candidate SYM
        if sym is None:
            continue
        leaf = sym + ">0x28"
        add = [leaf + ">0x18", leaf + ">8"]
        if full:                # data symbol: name of its base and offset inside it; code symbol: name at +4
            add += [leaf + ">0x18>8>0>4$", leaf + ">0x18>4$", leaf + ">0x18>0x24"]
        if len(sp) + len(add) > 12:
            continue
        pos[ev] = len(sp)
        out[ev] = head + "=" + ",".join(sp + add)
    return out, pos


IMMPOS = {}
if c2trace.c2build.get("regs_imm", True):
    HOOKS, IMMPOS = _imm_specs(HOOKS)
REGNUM = {1: "eax", 2: "ecx", 3: "edx", 4: "ebx", 5: "esp", 6: "ebp", 7: "esi", 8: "edi"}   # _RegName 10cec7b8
FPEV = {int(k): e for k, e in (c2trace.c2build.get("regs_events") or {}).items()}   # hook index -> extra event
KIND = {3: "temp", 4: "local", 5: "param", 0xd: "imm"}
CLASS = {0: "int", 1: "x87 (GFx)"}


def _strlit(nm):
    """'??_C@_03FIKCJHKP@abc?$AA@' -> '"abc"' when the literal's text is spelled out plainly; else None."""
    m = re.match(r"\?\?_C@_0(?:[0-9]|[A-P]+@)[A-P]+@(.*)@$", nm)
    if not m:
        return None
    t = m.group(1)
    if t.endswith("?$AA"):
        t = t[:-4]
    return '"%s"' % t if re.fullmatch(r"[A-Za-z0-9_ ]*", t) else None


def immtext(val, kw, names=None, nm=None, off=None):
    """Describe an immediate candidate from its leaf: dict(text, kind, size[, value]) or None.
    text: '1000', '-1', '0x12345', '&ga', '&arr+12', '&"abc"' (string literal), '&g' (function)."""
    if not isinstance(val, int) or not isinstance(kw, int):
        return None
    kind, size = kw & 0xff, (kw >> 16) & 0xfff
    if kind == 7:
        sv = s32(val)
        return dict(text="%d" % sv if -4096 < sv < 4096 else "0x%x" % val, kind="const", size=size, value=sv)
    if kind in (3, 4):
        if nm is None and names is not None:
            nm = names.get(val)
        if nm is None:
            nm = "sym@%x" % val
        return dict(text="&" + nm, kind="addr" if kind == 3 else "code", size=size, sym="%x" % val)
    return dict(text="leaf%d@%x" % (kind, val), kind="k%d" % kind, size=size)


def _immname(v):
    """Symbol name from the three name specs of a block event (base name, code name, offset)."""
    dn, cn, off = v[0], v[1], v[2]
    for x in (dn, cn):
        if isinstance(x, str) and x.startswith('"') and x.strip('"'):
            nm = x.strip('"')
            nm = _strlit(nm) or (nm[1:] if nm.startswith("_") else nm)
            return nm + ("+%d" % off if x is dn and isinstance(off, int) and 0 < off < 1 << 31 else "")
    return None


def sval(v):
    return v if v.startswith('"') or v in ("?", "IL") else int(v, 16)


def _bits(base, lo, hi):
    """Register numbers in one SBV chunk (base, lo, hi); an empty set has no chunk, so its words read as '?'."""
    if not isinstance(base, int):
        return []
    w = (lo if isinstance(lo, int) else 0) | ((hi if isinstance(hi, int) else 0) << 32)
    return [base + i for i in range(64) if w >> i & 1]


def _prefs(v):
    """(reg, count) pairs from an unrolled PREF chain; the chain ends at the first unreadable ('?') entry."""
    out = []
    for i in range(0, len(v) - 1, 2):
        if not isinstance(v[i], int) or not isinstance(v[i + 1], int):
            break
        out.append([v[i], s32(v[i + 1])])
    return out


def _grname(sn, key, knd):
    nm = (lambda x: x[1:] if x.startswith("_") else x)(sn.strip('"')) if isinstance(sn, str) and sn.startswith('"') else ""
    if not nm and isinstance(key, int):
        k = knd & 0xff if isinstance(knd, int) else None
        nm = ("t%d" % key) if k == 3 else ("%s%d" % (KIND.get(k, "k%s_" % k), key))
    return nm or "?"


def parse(log, funcpat):
    funcs, cur, cls, grs, pend = [], None, None, {}, None
    ch = None                             # the row ChoosePhysReg is colouring (choice events 13..17)
    npass, blk, terms = 0, None, {}       # terms[gr] = (pass, [(block, w, P, blkcost, delta)])
    for ln in log.splitlines():
        if ln.startswith("T ") and cur is not None and cur.get("il") is not None:
            f = ln.split()
            if f[1] == "B":
                cur["bb"] = f[2]
            elif f[1] == "I" and cur.get("bb"):
                cur["lines"].setdefault(cur["bb"], set()).add(int(f[4]))
                cur["ilb"].setdefault(cur["bb"], []).append(ln[2:])
            continue
        if "|" not in ln or ln.startswith("#") or ln.startswith("T "):
            continue
        head, _, rest = ln.partition("|")
        f = head.split()
        ev, hk = f[0], int(f[3])
        v = [sval(x) for x in c2trace._split(rest)]
        if ev == "E" and hk == 0:
            name = v[0].strip('"') if isinstance(v[0], str) else "?"
            cur = dict(name=name, rows=[], il=True, lines={}, bb=None, ilb={}, bbs=[],
                       base=v[1] if len(v) > 1 and isinstance(v[1], int) else 0) \
                if c2trace.func_match(funcpat, name) else None
            if cur is not None:
                funcs.append(cur)
            grs, terms, npass = {}, {}, 0
            continue
        if cur is None:
            continue
        if DEC0 is not None and hk >= DEC0:           # spill / defer / split decisions
            cur["_npass"] = npass
            _decision(cur, ev, hk - DEC0, f, v)
            continue
        x = FPEV.get(hk) if ev == "E" else None      # build-table events (13.10: x87 values go through FPColor)
        if x and x[0] == "out":
            if grs.get(v[0]) is not None:
                grs[v[0]]["out"] = x[1]
            continue
        if x:
            cls, hk = x[1], 2                        # a pop of class x[1], with the outcome x[2] if it is final
        if ev == "E" and hk == 1:
            cls = v[0]
            cur.setdefault("pcls", {})[npass] = cls
        elif ev == "E" and hk == 2:
            gp, prio, ben, refs, dord, gid, skey, skind, sname = v[:9]
            kind = skind & 0xff if isinstance(skind, int) else None
            if isinstance(sname, str) and sname.startswith('"') and sname.strip('"'):
                nm = sname.strip('"')
                nm = nm[1:] if nm.startswith("_") else nm
            else:
                nm = ("t%d" % skey if kind == 3 else "%s%d" % (KIND.get(kind, "k%s_" % kind), skey)) \
                    if isinstance(skey, int) else "?"
            row = dict(gr=gp, cls=cls, name=nm, kind=KIND.get(kind, "k%s" % kind), prio=s32(prio), ben=s32(ben),
                       refs=refs, dord=dord, id=gid, cands=[], out=None, pop=len(cur["rows"]) + 1, npass=npass,
                       terms=terms.get(gp, (None, []))[1])
            ip = IMMPOS.get(2)
            if kind == 0xd and ip is not None and not x and len(v) > ip + 1:
                im = immtext(v[ip], v[ip + 1], cur.setdefault("immnames", {}))
                if im:
                    row["imm"] = im
            first = grs.get(gp)
            if first is not None and first["out"] in ("spill/defer",):
                row["note"] = "re-popped"
            grs[gp] = row
            cur["rows"].append(row)
            pend = row
            if x and x[2]:
                row["out"] = x[2]
        elif ev == "E" and hk == 7:
            npass += 1
            nblk = 0
            cur.setdefault("pcls", {})[npass] = cls       # re-split passes run inside the current class's loop
        elif ev == "E" and hk == 8:
            nblk += 1
            blk = (nblk, v[1], v[0] // v[1] if v[1] else 0, "%x" % v[2] if len(v) > 2 and isinstance(v[2], int) else None)
            if npass == 1:
                cur["bbs"].append(blk)
            cur.setdefault("blkP", {})[(npass, nblk)] = (blk[1], blk[2])
        elif ev == "E" and hk in (9, 10, 11) and blk:
            g = v[0]
            delta = s32(v[1]) * v[2] if hk != 11 else -v[1]
            cost = s32(v[1]) if hk != 11 else 0
            p, lst = terms.get(g, (None, None))
            if p != npass:
                lst = []
                terms[g] = (npass, lst)
            lst.append((blk[0], blk[1], blk[2], cost if hk != 11 else None, delta, blk[3]))
            ip = IMMPOS.get(hk, len(v))
            sn, key, knd = v[ip - 3], v[ip - 2], v[ip - 1]
            if isinstance(knd, int) and knd & 0xff == 0xd and len(v) >= ip + 5:
                inm = _immname(v[ip + 2:ip + 5])
                if inm and isinstance(v[ip], int):
                    # two symbols can print alike (a struct and its first member are different candidates
                    # with one address): the second, third.. symbol of a name gets #2, #3..
                    names = cur.setdefault("immnames", {})
                    if v[ip] not in names:
                        dup = cur.setdefault("immdup", {}).setdefault(inm, [])
                        dup.append(v[ip])
                        names[v[ip]] = inm + ("#%d" % len(dup) if len(dup) > 1 else "")
                    inm = names[v[ip]]
                im = immtext(v[ip], v[ip + 1], nm=inm)
                if im:
                    cur.setdefault("immof", {})[(npass, g)] = im
            nm = (lambda x: x[1:] if x.startswith("_") else x)(sn.strip('"')) if isinstance(sn, str) and sn.startswith('"') else ""
            if not nm and isinstance(key, int):
                k = knd & 0xff if isinstance(knd, int) else None
                nm = ("t%d" % key) if k == 3 else ("%s%d" % (KIND.get(k, "k%s_" % k), key))
            cur.setdefault("byblock", {}).setdefault((npass, blk[0]), []).append(
                (nm or "?", g, cost if hk != 11 else None, delta))
            if nm:
                cur.setdefault("passes", {}).setdefault(npass, {}).setdefault((nm, g), 0)
                cur["passes"][npass][(nm, g)] += delta
                cur.setdefault("pterms", {}).setdefault((npass, nm, g), []).append((blk[0], blk[2], cost if hk != 11 else None, delta))
        elif ev == "E" and hk == 12:
            cur.setdefault("splitrm", {}).setdefault(npass, []).append(v[0])
        elif ev == "E" and hk == 3:
            ch = grs.get(v[0])
            if ch is not None:
                ch["choice"] = dict(nbrs=[], prefs=[], partners=[], avail=None)
        elif ev == "E" and hk == 13 and ch is not None and "choice" in ch:
            ch["choice"]["nbrs"].append(dict(gr=v[0], num=v[1], avail=_bits(*v[2:5]), ben=s32(v[5]),
                                             coloured=bool(v[6]) if isinstance(v[6], int) else None,
                                             name=_grname(*v[7:10]), prefs=[]))
        elif ev == "E" and hk == 14 and ch is not None and ch.get("choice", {}).get("nbrs"):
            if isinstance(v[1], int) and isinstance(v[2], int):
                ch["choice"]["nbrs"][-1]["prefs"].append([v[1], s32(v[2])])
        elif ev == "E" and hk == 15 and ch is not None and "choice" in ch:
            ch["choice"]["avail"] = _bits(*v[2:5])
        elif ev == "E" and hk == 16 and ch is not None and "choice" in ch:
            if isinstance(v[0], int) and isinstance(v[1], int):
                ch["choice"]["prefs"].append([v[0], s32(v[1])])
        elif ev == "E" and hk == 17 and ch is not None and "choice" in ch:
            ch["choice"]["partners"].append(dict(gr=v[0], num=v[1], coloured=bool(v[2]) if isinstance(v[2], int) else None))
        elif ev == "E" and hk in (18, 19):
            ins = cur.setdefault("inserts", [])
            if hk == 18:
                ins.append(dict(npass=npass, after_pop=len(cur["rows"]), gr=v[0], num=v[1], avail=_bits(*v[2:5]),
                                ben=s32(v[5]), prefs=_prefs(v[6:]), _more=len(_prefs(v[6:])) == 3))
            elif ins and ins[-1]["gr"] == v[0] and ins[-1].pop("_more", False):
                ins[-1]["prefs"] += _prefs(v[1:])
        elif ev == "E" and hk in PREF_SITE:
            if v[6] == 0 and isinstance(v[2], int):
                k = v[4] & 0xff if isinstance(v[4], int) else None
                cur.setdefault("prefev", []).append(dict(
                    npass=npass, after_pop=len(cur["rows"]), gr=v[0], num=v[1], reg=v[2], site=PREF_SITE[hk],
                    src=v[3] if k == 2 else None, src_num=v[5] if k == 2 else None))
        elif ev == "E" and hk == 20:
            cur.setdefault("inserts", []).append(dict(npass=npass, after_pop=len(cur["rows"]), deferred=True))
        elif ev == "E" and hk == 4 and pend is not None:
            pend["cands"].append("%s:%d" % (v[0].strip('"'), s32(v[1])))
        elif ev == "X" and hk == 3:
            r = grs.get(v[0])
            if r is not None:
                r["out"] = v[1].strip('"')
        elif ev == "E" and hk in (5, 6):
            r = grs.get(v[0])
            if r is not None:
                r["out"] = "spill/defer" if hk == 5 else "uncolourable"
    return funcs


def s32(x):
    return x - (1 << 32) if isinstance(x, int) and x & 0x80000000 else x


def export(fn):
    """JSON-able snapshot for tools/rasim: every Spillcosts pass (its class, blocks with w and the true P, and each
    uncoloured globreg's per-block terms) plus the actual pops (order, prio, defOrd, register)."""
    passes = []
    nfp = sum(1 for r in fn["rows"] if not r["npass"])           # rows popped before any Spillcosts pass: FPColor
    fn = dict(fn, rows=[r for r in fn["rows"] if r["npass"]])   # (13.10); left out, pops renumbered from 1
    for np_ in sorted({k[0] for k in fn.get("blkP", {})}):
        blocks = [dict(b=b, w=w, P=P) for (pp, b), (w, P) in sorted(fn["blkP"].items()) if pp == np_]
        grs = {}
        for (pp, b), lst in fn.get("byblock", {}).items():
            if pp != np_:
                continue
            for nm, g, cost, d in lst:
                e = grs.setdefault("%x" % g, dict(name=nm, terms=[]))
                e["terms"].append(dict(b=b, cost=cost, delta=d))
                im = fn.get("immof", {}).get((np_, g))
                if im:
                    e["imm"] = im          # immediate candidate: {text, kind: const|addr|code, size, value}
        for e in grs.values():
            e["terms"].sort(key=lambda t: t["b"])
        passes.append(dict(n=np_, cls=fn.get("pcls", {}).get(np_), blocks=blocks, grs=grs,
                           split_removed=["%x" % g for g in fn.get("splitrm", {}).get(np_, [])]))
    pops = [dict(pop=r["pop"] - nfp, name=r["name"], gr="%x" % r["gr"], prio=r["prio"], defOrd=r["dord"], reg=r["out"],
                 npass=r["npass"], cls=r["cls"], ben=r["ben"], kind=r["kind"], num=r["id"]) for r in fn["rows"]]
    for r, q in zip(fn["rows"], pops):
        if r.get("imm"):
            q["imm"] = r["imm"]
    snap = dict(func=fn["name"], passes=passes, pops=pops)
    if any("choice" in r for r in fn["rows"]) or fn.get("inserts"):
        # Register choice (regalloc.md section 4).  Register numbers are c2's (regnames maps the integer ones).
        #   pops[i].choice: what ChoosePhysReg saw when it coloured this globreg: its avail set and PREF list
        #     [reg, count], every member of nbrs (the uncoloured interfering globregs InsertSplitPoints found) with
        #     that neighbour's avail, benefit and PREFs, and the _UpdatePrefsBv partners (uncoloured ones get a
        #     preference for the chosen register).
        #   inserts: every InsertInList call in order (npass = Spillcosts pass, after_pop = pops made before it):
        #     the globreg's avail set and PREF list at that moment.  The first one after a globreg's Spillcosts
        #     pass is its initial state; a preference that shows up later was added by a partner's colouring.
        for r, q in zip(fn["rows"], pops):
            c = r.get("choice")
            if c is not None:
                q["choice"] = dict(avail=c["avail"], prefs=c["prefs"],
                                   nbrs=[dict(n, gr="%x" % n["gr"]) for n in c["nbrs"]],
                                   partners=[dict(n, gr="%x" % n["gr"]) for n in c["partners"]])
        snap["inserts"] = [dict({k: v for k, v in e.items() if k != "_more"}, gr="%x" % e["gr"]) if "gr" in e else e
                           for e in fn.get("inserts", [])]
        #   pref_events: every preference Spillcosts added (section 4.1), in order: the globreg, the register, the
        #     kind of copy, and src = the coloured globreg on the other side (absent: a physical register).
        snap["pref_events"] = [dict(e, gr="%x" % e["gr"], src="%x" % e["src"] if e["src"] is not None else None)
                               for e in fn.get("prefev", [])]
        snap["regnames"] = {str(k): v for k, v in REGNUM.items()}
        for e in snap["inserts"] + snap["pref_events"]:
            e["after_pop"] -= nfp
    if fn.get("dec") is not None or any("x" in r for r in fn["rows"]):
        # Spill / defer / split decisions (regalloc.md section 3.6), in order.  t = so (SpillObvious looks at a new
        # globreg), spill, subst (CmdSubstDef; ok = folded), remove (record retired), restrict (ret 1 = pinned),
        # force1 / forceair, btd (BetterToDefer; ret 1 = re-queued or folded), fold (FoldAwaySplitGlobreg; ret),
        # mark (UpdateAffectedGlobregs sets fl5 |= 2: site isp = InsertSplitPoints' set, nbr / nbr8 = a neighbour
        # left without a register, needlives = Spill deleted a dead def that used it), recalc, bencost (CmdUpdateCost: benefit += delta), bytepress (CmdProcessBytePressure: avail &= regs).  pops[i].x and inserts[i].x: the GLOBREG record at that moment.
        snap["decisions"] = [dict(e, gr="%x" % e["gr"], after_pop=e["after_pop"] - nfp) for e in fn.get("dec", [])]
        for r, q in zip(fn["rows"], pops):
            if "x" in r:
                q["x"] = r["x"]
        # every RemoveSplitGlobregs call, also of a round whose Spillcosts pass walks no block (such a pass has no
        # entry in `passes`): npass = the pass being coloured when the split set was returned
        snap["split_rounds"] = [dict(npass=n, removed=["%x" % g for g in lst])
                                for n, lst in sorted(fn.get("splitrm", {}).items())]
        snap["npasses"] = max(fn.get("pcls") or [0])     # Spillcosts calls, also those that cost nothing
    return snap


def _immnote(im):
    """'   = 1000 (const, 4 bytes)' for an immediate candidate's description, '' otherwise."""
    return "   = %s (%s, %s bytes)" % (im["text"], im["kind"], im["size"]) if im else ""


def _whysel(r, why):
    """--why selects a row by name, by * or, for an immediate candidate, by its value: imm=1000, imm=&ga."""
    return r["name"] in why or "*" in why or (r.get("imm") is not None and "imm=" + r["imm"]["text"] in why)


def blocks(fn, lo, hi):
    out = []
    for b, wb, pb, bb in fn["bbs"]:
        if lo <= b <= hi:
            out.append("  block %d  w=%d P=%d  (BB %s)" % (b, wb, pb, bb))
            out.extend("      " + l for l in fn["ilb"].get(bb, []))
    return "\n".join(out)


def block_view(fn, want):
    """Every globreg with a term in block `want`, per Spillcosts pass: the values competing there (P counts the
    referenced ones), their cost in the block and the term they get."""
    out = []
    for (np_, b), lst in sorted(fn.get("byblock", {}).items()):
        if b != want:
            continue
        refd = [t for t in lst if t[2] is not None]
        w, P = fn.get("blkP", {}).get((np_, b), (0, 0))
        out.append("  block %d, Spillcosts pass %d: w=%d P=%d (P counts every referenced globreg, coloured ones too; "
                   "listed: the %d uncoloured referenced + %d live-through ones whose prio changes)" % (
                       b, np_, w, P, len(refd), len(lst) - len(refd)))
        for nm, g, cost, d in sorted(lst, key=lambda t: (t[2] is None, -(t[3] or 0))):
            im = fn.get("immof", {}).get((np_, g))
            out.append("      %-28s gr %-8x %s%s" % (nm, g, ("cost %d -> %+d" % (cost, d)) if cost is not None
                                                     else "live through -> %+d" % d, _immnote(im)))
    return "\n".join(out) or "  (no terms recorded for block %d)" % want


def report(funcs, all_classes, why=()):
    out = []
    for fn in funcs:
        rows = [r for r in fn["rows"] if all_classes or r["cls"] in (0, None)]
        out.append("%s  (%d globregs%s)" % (fn["name"], len(rows), "" if all_classes else ", int class"))
        if not rows:
            continue
        w = max(4, max(len(r["name"]) for r in rows))
        out.append("  pop  %-*s %-5s %6s %7s %4s %4s  %-12s  cands (weight)" % (w, "var", "kind", "prio", "benefit",
                                                                            "refs", "def", "-> reg"))
        for r in rows:
            out.append("  %3d  %-*s %-5s %6s %7s %4s %4s  %-12s  %s%s" % (
                r["pop"], w, r["name"], r["kind"], r["prio"], r["ben"], r["refs"], r["dord"], r["out"] or "-",
                " ".join(r["cands"]), "  [%s]" % CLASS.get(r["cls"], "class %s" % r["cls"]) if all_classes else "")
                + _immnote(r.get("imm")))
        for r in rows:
            if not _whysel(r, why):
                continue
            tot = sum(t[4] for t in r["terms"])
            out.append("  why %s%s (pop %d): prio %s = %s%s" % (r["name"], _immnote(r.get("imm")).replace("   = ", " = "), r["pop"], r["prio"], tot,
                       "" if tot == r["prio"] else "  (FPColor: per-block terms are not traced)" if not r["npass"] else
                       "  (MISMATCH: terms do not add up; split or recomputed range)"))
            for b, wb, pb, cost, d, bb in r["terms"]:
                ls = sorted(fn["lines"].get(bb, ()))
                where = ("src +%d..+%d" % (ls[0], ls[-1]) if ls[0] != ls[-1] else "src +%d" % ls[0]) if ls else ""
                out.append("      block %3d  w=%-2d P=%-2d  %-44s %s" % (b, wb, pb, "refs cost %d -> %+d" % (cost, d)
                                                                if cost is not None else "live through, unreferenced -> %+d" % d, where))
        for var in sorted(why):
            for np_, tot in sorted(fn.get("passes", {}).items()):
                hits = ["%d (gr %x)" % (t, g) for (nm, g), t in tot.items() if nm == var]
                if hits:
                    out.append("  %s: Spillcosts pass %d prio %s" % (var, np_, ", ".join(hits)))
        out.append("")
    return "\n".join(out)


def main(argv):
    ap = argparse.ArgumentParser(prog="c2trace.py regs", usage=__doc__)
    ap.add_argument("--func")
    ap.add_argument("--all-classes", action="store_true")
    ap.add_argument("--block", type=int, help="N: list every value competing in block N (per Spillcosts pass)")
    ap.add_argument("--blocks", help="LO-HI: print the IL (at Color entry) of blocks LO..HI as numbered by --why")
    ap.add_argument("--why", default="", help="comma-separated variable names (or *) to break their prio down by block")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--export", help="write a JSON snapshot of the allocation for tools/rasim")
    ap.add_argument("--keep")
    ap.add_argument("src")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    a.cl = cl
    img, hooks, rc, out, log = c2trace.run_trace(a.src, HOOKS, a.cl, keep=a.keep)
    if rc:
        sys.stdout.write(out)
    if a.raw:
        sys.stdout.write(c2trace.decode(log, hooks, False))
    funcs = parse(log, a.func)
    if not funcs:
        sys.exit("no function matching %r was register-allocated" % a.func)
    print(report(funcs, a.all_classes, set(filter(None, a.why.split(",")))))
    if a.export:
        import json
        json.dump(export(funcs[0]), open(a.export, "w"), indent=0)
        print("# exported %s (%s)" % (a.export, funcs[0]["name"]))
    if a.block is not None:
        for fn in funcs:
            print(block_view(fn, a.block))
    if a.blocks:
        lo, _, hi = a.blocks.partition("-")
        for fn in funcs:
            print(blocks(fn, int(lo), int(hi or lo)))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
