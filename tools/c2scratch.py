#!/usr/bin/env python3
"""Scratch-register (tmpreg) picks for one function, mapped to listing offsets.  Doc: docs/c2/tracer.md (`scratch`)

usage: c2trace.py scratch <src> --func SUBSTR [--why] [--all] [--color] [--export snap.json] [--keep DIR] [-- <cl args>]
       c2trace.py scratch <src> --func SUBSTR --phase ORIG.s [--asm-func DEC] [--by case|block] [--top K]
                      [--no-scan] [--brief] [-- <cl args>]

regasg's RegSimpleAssignBody runs after Color and gives every short-lived temporary a register, one RegAssign call
per tmpreg operand (regalloc.md §6): its preferred register (a copy from / lea of a register) if free, else at /O2 the
next free of eax, ecx, edx from the function-wide cursor _NextIntPos, else first fit. For each pick this prints:

  n      pick number (RegAssign calls in IL order)
  cur    the cursor (_NextIntPos) before the pick: the register a rotation pick starts its search at
  pref   the temp's preferred register, if any
  how    ROT when the pick moved the cursor (CmdSetNextPos ran), else pref/first-fit
  reg    the register given
  off    listing offset of the instruction in the final code (from the encoded lengths at @Encode@4), or
         'gone' when the instruction was folded or deleted after the pick: such a pick still moved the cursor,
         which is how an invisible pick shifts the registers of every later one
  instr  the instruction at pick time (IL dump syntax, see `il`)

After the picks, rows marked C are registers in the code that are NOT scratch picks: values Color allocated (the
same Color / ChoosePhysReg events as `regs`, traced in the same compile), at each listing offset where the final
code writes one. By default only temporaries (t591, $S9: the ones that read like scratch temps; a load into a
Color temp looks exactly like a pick in the listing); --color lists every Color variable. Why such a value got its
register is `regs --why`, not this table.

Rows marked R (cl 13.10): Riscify's GetReg calls, after RegSimpleAssign: the register a memory operand split off into
a new temp gets (block, cursor > register, busy registers, temps made), and under it why the instruction was split
or not and whether the split survived (not split: no memory operand / no free register; split, undone by
RiscifyEarlyUndo or after Schedule; split, kept: what was scheduled in between). Rows marked ?: the other temps of the final
code that no pick, Color pop or GetReg call gave a register, with the phase that did (IL at RewriteAsReal and at
Riscify's entry). A pick whose IL name differs from the final code's ends with [temp tN in the final code].
docs/c2/regalloc.md 6.6.

--why (every c2 build: 14.00, 15.00, 13.10.3077, 13.10.3052; tools/c2builds/*.json hooks.scratch_x) adds what RegAssign saw: the cursor after the pick, the candidate set (which of eax ecx edx were
free in _RegMap and not in the temp's conflict set), and for every pick that is not "the cursor's register, taken
by rotation" the reason (preference taken / preference refused / register busy with which temp or coloured
variable / conflict). --all lists every RegSimpleAssignBody run (EH funclets, then the body) instead of the body
only. --export writes all of it as JSON for `rasim scratch` (predict the picks, what-if: tracer.md).

--phase ORIG.s (the original's disassembly, 'VA mnemonic operands' lines; or a /FAs listing NAME.asm of another
compile, read with --asm-func) reads the original's register for every
pick: our listing is aligned to ORIG.s (regmap's alignment) and the register in the position where our instruction
has the pick's register is the original's pick. Per group (a switch case: the code from one jump-table target to the
next in our layout; --by block: an IL block) it prints our picks against the original's, the cursor phase (how many
rotation steps the original is ahead of us: 0, +1, -1) and the first pick where they differ; then the runs of cases
in one phase, and (unless --no-scan) every single inserted / removed pick ranked by how many groups it re-aligns
(`rasim scratch SNAP --target`: the pick rule replayed with drop:N / ins:N).
"""
import argparse, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace

REGNAME = "10cec7b8"
NEXTINTPOS = "10d0f854"      # _NextIntPos (.data 0003:38996)
NAME = "g10d123ac>0>0>4$"
HOOKS = [
    "@RegSimpleAssignBody@4=" + NAME,                                                      # 0
    "@RegAssign@8=s1>0x1c>0x1c,g%s>0>0~%s$,s2,s2!/xeax>0x1c~%s$,a1>0x1c>0x30>0x1c~%s$" % (
        NEXTINTPOS, REGNAME, REGNAME, REGNAME),                                             # 1  (_IntPhysRegs: dwords)
    "@CmdSetNextPos@8=eax",                                                                 # 2
    "@Encode@4=%s,g10d123ac>0>8>0>0x1c%%" % NAME,                                           # 3
]
HOOKS = c2trace.c2build.hooks("scratch", HOOKS)   # another c2 build: the same 4 events (13.10: @RegSimpleAssign@4)

# What RegAssign decides from (--why / --export), 14.00.50727.762 addresses (regalloc.md §6); the other builds' tables
# carry their own (hooks.scratch_x, below).
REGMAP, REGAVAIL, SPEED, REGSYM = "10d0fd10", "10d0f9f0", "10d0d71c", 0x10D09F70   # _RegMap[92], regAvail[92], RegSym[92]*0x60
INTREGS = ["eax", "ecx", "edx", "esi", "edi", "ebx", "ebp"]      # _IntPhysRegs 10d0cfd0 = 1 2 3 7 8 4 6 0
RNAME = {0: "noreg", 1: "eax", 2: "ecx", 3: "edx", 4: "ebx", 5: "esp", 6: "ebp", 7: "esi", 8: "edi"}
SCR = ["eax", "ecx", "edx"]
GC = "@RegBuildGlobalConflict@4"
HOOKS_X = [
    HOOKS[0] + ",g%s>0" % SPEED,
    # entry 4..10: tmp sym, operand word (+0xa = type), tmp flags (+5 & 0x80 = byte), pref reg set by the conflict
    # builder, _RegMap[0..8], regAvail[0..8], cursor reg number.  exit 2..4: reg number, cursor after, pref reg number
    "@RegAssign@8=s1>0x1c>0x1c,g%s>0>0~%s$,s2,s2!,s1>0x1c,s1>8,s1>0x1c>4,s1>0x1c>0x30>0x1c,g%s#9,g%s#9,g%s>0>0"
    "/xeax>0x1c~%s$,a1>0x1c>0x30>0x1c~%s$,eax>0x1c,g%s>0>0,a1>0x1c>0x30>0x1c" % (
        NEXTINTPOS, REGNAME, REGMAP, REGAVAIL, NEXTINTPOS, REGNAME, REGNAME, NEXTINTPOS),
    "@CmdSetNextPos@8=eax,ecx",
    HOOKS[3],
    "@RegResetRegMap@4=eax>0x20",          # 4  a block starts: _RegMap := the block's live-in physregs
    "@RegSpill@12",                        # 5
    "@RegFixConflict@8=s2",                # 6
    GC,                                    # 7  the /O2 conflict builder ran for this run
    # the builder walks each block backwards with P = live physregs and T = live temps (regalloc.md §6.2)
    GC + "+0x422=esi",                     # 8  def of a bare physreg r:        _BvConflicts[r] |= T
    GC + "+0x5dd=edi",                     # 9  r in a kill set (call):          _BvConflicts[r] |= T
    GC + "+0x7a3=edi",                     # 10 last use of r (temp given r):    _BvConflicts[r] |= T
    GC + "+0xc7d=esi",                     # 11 last use of a coloured r:        _BvConflicts[r] |= T
    GC + "+0x996=edi",                     # 12 last use of temp t: T += t, then one event 13 per r in P
    GC + "+0xa20=eax",                     # 13   _BvConflicts[r] += t
    GC + "+0x335=ebx>0x1c",                # 14 def of temp t (not also a source): T -= t
]


_IMGBASE = 0x10B00000

# Where a temp of the final code got its register when it is neither a RegAssign pick nor a Color pop (`origins`):
# IL dumps at RewriteAsReal's entry (end of Color) and, on cl 13.10, around Riscify, plus every GetReg call (the
# register Riscify gives the load it splits off a memory operand; docs/c2/regalloc.md section 6.6). A build table's
# hooks.origin has the same roles at its addresses; 14.00's are here.
ORIGIN_ROLES = ("ra", "rp", "gr", "rm")
ORIGIN = {"ra": "@RewriteAsReal@4=%s,g10d123ac>0>8>0>0x1c%%" % NAME}

# Color's own allocations (the same events as `regs`, at this build's addresses), appended to the hooks of the
# `scratch` table so that it can say which registers in the code are not scratch picks: Color@4 entry (the
# function's name), a globreg just popped (its name), ChoosePhysReg's exit (the register it got).
def _color_hooks():
    import c2regs
    return ["@Color@4=" + c2regs.HOOKS[0].split("=", 1)[1].split(",")[0], c2regs.HOOKS[2], c2regs.HOOKS[3]]
if c2trace.c2build.table() is not None:
    ORIGIN = c2trace.c2build.get("hooks", {}).get("origin", {})
if c2trace.c2build.table() is not None and "scratch_x" in c2trace.c2build.get("hooks", {}):
    # a ported build with its own --why/--export events (tools/c2builds/*.json: hooks.scratch_x, globals._RegSym)
    HOOKS_X = c2trace.c2build.hooks("scratch_x", HOOKS_X)
    REGSYM = int(c2trace.c2build.get("globals")["_RegSym"], 16)
    _IMGBASE = int(c2trace.c2build.get("image_base"), 16)


def origin_hooks():
    """[(role, hook)] of this build: the IL dumps and GetReg events `origins` reads (see ORIGIN)."""
    return [(r, ORIGIN[r]) for r in ORIGIN_ROLES if r in ORIGIN]


def _hex(s):
    try:
        return int(s, 16)
    except (TypeError, ValueError):
        return None


def _words(s):
    return [_hex(x) for x in s.strip("[]").split()] if s and s.startswith("[") else None


def parse_segments(log, funcpat, cbase=None, obase=None, oroles=()):
    """-> (runs, final IL at @Encode@4). One run per RegSimpleAssignBody call for the function (EH funclets, then
    the body): dict(picks=[...], conf={reg number: set of temp ids} or None, blocks, spills, fixes, speed, and with
    cbase (the index of _color_hooks() in the hook list) color={regs name: set of registers Color gave it}). With
    obase (the index of origin_hooks() in the hook list; oroles their roles) every run also gets origin={'ra' / 'rp'
    / 'rm': the function's IL at RewriteAsReal / Riscify / MdmAvoidMOBStalls, 'gr': GetReg calls}."""
    segs, final, on, enc, pend, stack = [], None, False, False, None, []
    seg = None
    regsym = REGSYM
    cfunc, cname, color, grname = False, None, {}, {}
    oname, ofunc, origin, odump, onames = None, False, {}, None, {}
    for ln in log.splitlines():
        if ln.startswith("T "):
            if odump is not None:
                odump.append(ln[2:])
                continue
            if enc:
                final.append(ln[2:])
            elif pend is not None and pend.get("text") is None:
                pend["text"] = ln[2:]
            continue
        if ln.startswith("# c2trace base "):
            regsym = REGSYM - _IMGBASE + int(ln.split()[3], 16)     # where _RegSym is in this run
        if "|" not in ln or ln.startswith("#"):
            continue
        head, _, rest = ln.partition("|")
        f = head.split()
        ev, hk = f[0], int(f[3])
        v = c2trace._split(rest)
        odump = None
        if obase is not None and hk >= obase:              # IL dumps / GetReg calls (see origins)
            role = oroles[hk - obase]
            if role == "gr":
                if ofunc and ev == "E" and len(v) >= 7:
                    w = _words(v[0]) or []
                    busy = w[2] if len(w) >= 3 and w[0] == 0 else 0
                    origin.setdefault("gr", []).append(dict(busy=[RNAME[r] for r in sorted(RNAME) if r and busy >> r & 1],
                        byte=bool(_hex(v[1])), cur=_hex(v[2]), tup=v[3], order=_words(v[4]) or [], count=_hex(v[5]),
                        used=_words(v[6]) or [], reg=None))
                elif ofunc and ev == "X" and origin.get("gr"):
                    r = _hex(v[0]) if v else None
                    origin["gr"][-1]["reg"] = None if r == 0 else RNAME.get(r, "?")      # 0: no register free
            elif ev == "E":
                nm = v[0].strip('"') if v else ""
                if nm != oname:
                    oname, ofunc = nm, c2trace.func_match(funcpat, v[0] if v else "")
                    origin = onames.setdefault(nm, {}) if ofunc else {}
                if ofunc:
                    odump = origin[role] = []
            continue
        if cbase is not None and hk >= cbase:              # Color (before the picks of the same function)
            c = hk - cbase
            if ev == "E" and c == 0:
                nm = v[0] if v else ""
                if nm != cname:
                    cname, cfunc, color, grname = nm, c2trace.func_match(funcpat, nm), {}, {}
            elif ev == "E" and c == 1 and cfunc and len(v) >= 9:
                grname[_hex(v[0])] = _grname(v)
            elif ev == "X" and c == 2 and cfunc and len(v) >= 2 and grname.get(_hex(v[0])):
                r = v[1].strip('"')
                if r and r != "?" and not r.startswith("noreg"):
                    color.setdefault(grname[_hex(v[0])], set()).add(r)
            continue
        if ev == "E" and hk in (0, 3):
            hit = c2trace.func_match(funcpat, v[0] if v else "")
            if hk == 0:
                on, enc = hit, False
                if hit:
                    seg = dict(picks=[], conf=None, blocks=0, spills=0, fixes=[], speed=_hex(v[1]) if len(v) > 1 else None,
                               T=set(), last=None, ptr={}, color=color if cfunc else {},
                               origin=onames.setdefault(v[0].strip('"'), {}) if obase is not None else None)
                    segs.append(seg)
            else:
                enc = hit and final is None
                if enc:
                    final = []
                on = False
            continue
        enc = False if ev == "E" else enc
        if not on:
            continue
        picks = seg["picks"]
        if ev == "E" and hk == 1:
            pend = dict(n=len(picks) + 1, tmp=int(v[0], 16) if v[0] != "?" else None, cur=v[1].strip('"'),
                        pref="", instr=v[2], text=None, rot=False, reg=None)
            if len(v) > 10:                                  # HOOKS_X
                rm, av = _words(v[8]) or [], _words(v[9]) or []
                busy = {}
                for r, w in enumerate(rm):
                    if w and r in RNAME and r:
                        busy[RNAME[r]] = "phys" if regsym <= w < regsym + 92 * 0x60 else seg["ptr"].get(w, "t?")
                ty = (_hex(v[5]) or 0) >> 16
                pend.update(block=seg["blocks"], type=ty, cls="int" if ty >> 12 in (1, 2, 6) else "c%x" % (ty >> 12),
                            byte=bool((_hex(v[6]) or 0) & 0x8000), pref_pre=RNAME.get(_hex(v[7]), "") if v[7] != "?" else "",
                            busy=busy, avail=[RNAME[r] for r, w in enumerate(av) if w and r in RNAME and r],
                            cur_after=None, spill=False)
                if _hex(v[4]):
                    seg["ptr"][_hex(v[4])] = "t%d" % pend["tmp"] if pend["tmp"] is not None else "t?"
            picks.append(pend)
            stack.append(pend)
        elif ev == "E" and hk == 2 and stack:
            stack[-1]["rot"] = True
        elif ev == "X" and hk == 1 and stack:
            p = stack.pop()
            p["reg"] = v[0].strip('"') if v else "?"
            p["pref"] = v[1].strip('"') if len(v) > 1 and v[1] != "?" else ""
            if len(v) > 3:
                p["cur_after"] = RNAME.get(_hex(v[3]), "eax" if _hex(v[3]) is not None else None)
        elif ev != "E":
            continue
        elif hk == 4:
            seg["blocks"] += 1
        elif hk == 5:
            seg["spills"] += 1
            if stack:
                stack[-1]["spill"] = True
        elif hk == 6:
            seg["fixes"].append(dict(after_pick=len(picks), reg=RNAME.get(_hex(v[0]) if v else None, "?")))
        elif hk == 7:
            seg["conf"] = {}
        elif 8 <= hk <= 11 and seg["conf"] is not None:
            seg["conf"].setdefault(_hex(v[0]), set()).update(seg["T"])
        elif hk == 12:
            seg["last"] = _hex(v[0])
            seg["T"].add(seg["last"])
        elif hk == 13 and seg["conf"] is not None:
            seg["conf"].setdefault(_hex(v[0]), set()).add(seg["last"])
        elif hk == 14:
            seg["T"].discard(_hex(v[0]))
    for s in segs:
        for k in ("T", "last", "ptr"):
            del s[k]
    return segs, final or []


def _grname(v):
    """The `regs` name of the globreg in a Color pop event (c2regs.parse: `this`, `local386`, `t591`, `$S9`)."""
    import c2regs
    skey, skind, sname = (c2regs.sval(x) if x else "?" for x in v[6:9])
    if isinstance(sname, str) and sname.startswith('"') and sname.strip('"'):
        nm = sname.strip('"')
        return nm[1:] if nm.startswith("_") else nm
    kind = skind & 0xff if isinstance(skind, int) else None
    if not isinstance(skey, int):
        return None
    return "t%d" % skey if kind == 3 else "%s%d" % (c2regs.KIND.get(kind, "k%s_" % kind), skey)


DSTREG = re.compile(r"\b(e[abcd]x|e[sd]i|ebp|[abcd][lhx]|[sd]i|bp)=([^\s:\]]+):")


def color_rows(seg, final, picked, show_all=False):
    """[(offset, register, regs name, instruction text)] for the final IL's destination operands that hold a value
    Color allocated (not a scratch pick): temporaries only (t591, $S9: the ones that read like scratch temps),
    every Color variable with show_all. Also the number of named-variable rows left out."""
    import regmap
    color, rows, hidden, off = seg.get("color") or {}, [], 0, 0
    for l in final:
        if not l.startswith("I "):
            continue
        f = l.split()
        body = l.split("|", 1)[1] if "|" in l else ""
        dst = body.split("<-", 1)[0]
        for r, il in DSTREG.findall(dst):
            nm = regmap.regs_name(il)
            if il in picked or nm not in color:
                continue
            if show_all or re.fullmatch(r"t\d+|\$.*", nm):
                rows.append((off, r, nm, "%s | %s" % (f[2], body.strip())))
            else:
                hidden += 1
        off += int(f[4])
    return rows, hidden


TREG = re.compile(r"\b(e[abcd]x|e[sd]i|ebp|[abcd][lhx]|[sd]i|bp)=(t\d+):")
_WIDE = {"al": "eax", "ah": "eax", "ax": "eax", "bl": "ebx", "bh": "ebx", "bx": "ebx", "cl": "ecx", "ch": "ecx",
         "cx": "ecx", "dl": "edx", "dh": "edx", "dx": "edx", "si": "esi", "di": "edi", "bp": "ebp"}


def _ilregs(lines):
    """{temp: register} for every `reg=tN` operand of the I lines of an IL dump (the first register seen)."""
    out = {}
    for l in lines or []:
        if l.startswith("I ") and "|" in l:
            for r, t in TREG.findall(l.split("|", 1)[1]):
                out.setdefault(t, _WIDE.get(r, r))
    return out


_PHYS = {"eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "al", "ah", "ax", "bl", "bh", "bx", "cl", "ch", "cx",
         "dl", "dh", "dx", "si", "di", "bp", "sp", "cc", "fcc"}


def _memops(ln):
    """The memory operands of an IL line at Riscify's entry: `[..]` (kind 6) and memory variables (kind 2: a name
    without `reg=`; after RegSimpleAssign every register operand is written reg=tN or as a register)."""
    if not ln or "|" not in ln:
        return []
    ops, out, depth, cur = ln.split("|", 1)[1].replace("<-", " "), [], 0, ""
    for ch in ops:
        depth += ch == "["
        depth -= ch == "]"
        if ch == " " and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    mem = []
    for t in out:
        t = t.strip()
        if not t or t == "->" or t.startswith(";"):
            continue
        if t.startswith("["):
            mem.append(t)
            continue
        nm = t.rsplit(":", 1)[0]
        if "=" in nm or nm in _PHYS or re.fullmatch(r"-?\d+|pas\d+|regset|fconst|k\d+|L\w+", nm) or nm.startswith(("?", "@")):
            continue
        mem.append(t)
    return mem


def split_reason(c, rp_line, final):
    """Why Riscify split (or did not split) the instruction of GetReg call c, and whether the split survived to the
    final code (docs/c2/regalloc.md 6.6): a GetReg call is made for every instruction whose opcode has
    _Opcode_dope & 0x400 (add adc and cmp or sbb sub test xor inc dec neg not push shifts rotates xchg; never
    mov, lea, imul); the first memory operand (dst list first) is split unless GetReg found no register; a split is
    undone by RiscifyEarlyUndo when the operation is 16-bit, and by Riscify_undo (PreEncode, after Schedule) when the
    load is still right before its use (and the op right before the store)."""
    if c["reg"] is None:
        return "not split: GetReg found no free register"
    mem = _memops(rp_line)
    if not c["made"]:
        if not mem:
            return "not split: no memory operand (GetReg is called anyway: the cursor moves)"
        return "not split: its memory operand %s was refused (type class 0x4000, leaf flag 0x20, or a byte operand with no byte register)" % mem[0][:40]
    fin = "\n".join(final or [])
    alive = [t for t in c["made"] if re.search(r"=%s:" % t, fin)]
    if not alive:
        if re.search(r"=(%s):[0-9a-f]*002\b" % "|".join(c["made"]), c.get("rmline") or ""):
            return "split, undone by RiscifyEarlyUndo (a 16-bit operation)"
        return "split, undone after Schedule (Riscify_undo: the load was still right before its use)"
    # kept: what the scheduler put between the load and the use (or the op and the store)
    il = [l for l in final or [] if l.startswith("I ") and "|" in l]
    t = alive[0]
    defs = [i for i, l in enumerate(il) if re.search(r"=%s:" % t, l.split("|", 1)[1].split("<-")[0])]
    uses = [i for i, l in enumerate(il) if re.search(r"=%s:" % t, l.split("|", 1)[1].split("<-", 1)[-1])]
    gaps = []
    if defs and uses:
        first = min([u for u in uses if u > defs[0]] or [defs[0]])
        gaps = [il[i].split()[2] for i in range(defs[0] + 1, first)]          # between the load and its use
        stores = [i for i in uses if i > first and il[i].split()[2] == "mov"]  # read-modify-write: the store
        if stores:
            gaps += [il[i].split()[2] for i in range(first + 1, stores[-1])]  # between the op and the store
    if gaps:
        return "split, kept: %s scheduled between the load and its use or store, so Riscify_undo leaves it" % " ".join(gaps[:4])
    return "split, kept: the use no longer names %s as the split register (narrowed, e.g. test ecx,0x100 -> test ch,1), so Riscify_undo does not match it" % c["reg"]


def riscify_calls(seg, pos, final=None):
    """Riscify's GetReg calls for this function (cl 13.10; regalloc.md 6.6), in call order: dict(n, block (the
    tuple's block in the IL after Riscify, counted from 1), busy, byte, cur (the cursor's register, None at a block
    start), reg, made (the new temps of the riscified instruction that have reg), off, instr). Riscify walks every
    block from its last instruction up; the cursor restarts at the block's start, and every call moves it, whether or
    not the instruction is then split."""
    o = seg.get("origin") or {}
    calls, rp, rm = o.get("gr") or [], o.get("rp"), o.get("rm")
    if not calls or rm is None:
        return []
    pre = set(re.findall(r"\b(t\d+):", "\n".join(rp or [])))
    pre_l = {l.split()[1]: l for l in rp or [] if l.startswith("I ")}
    lines, blk = {}, 0
    for l in rm:
        if l.startswith("B "):
            blk += 1
        elif l.startswith("I "):
            lines[l.split()[1]] = (l, blk)
    out = []
    for k, c in enumerate(calls, 1):
        ln, b = lines.get(c["tup"], (None, None))
        made = []
        if ln and c["reg"] and "|" in ln:
            for r, t in TREG.findall(ln.split("|", 1)[1]):
                if _WIDE.get(r, r) == c["reg"] and t not in pre and t not in made:
                    made.append(t)
        order = [RNAME.get(x, "?") if x else None for x in c["order"]]
        cnt = c["count"] if c["count"] else len(order)
        cur = c["cur"]
        curr = order[cur] if cur is not None and 0 <= cur < len(order) and cur < cnt - 1 else None
        at = pos.get(c["tup"])
        h = ln.split() if ln else []
        out.append(dict(n=k, block=b, busy=[r for r in c["busy"] if r != "esp"], byte=c["byte"], cur=curr, cur_idx=cur,
                        reg=c["reg"], made=made, order=order, count=cnt,
                        used=[RNAME[r] for r, w in enumerate(c["used"]) if w and r in RNAME and r and r != 5],
                        off=None if at is None else at[0], op=h[2] if len(h) > 2 else "?",
                        instr=ln.split("|", 1)[1].strip() if ln and "|" in ln else ""))
        out[-1]["rmline"] = ln
        out[-1]["why"] = split_reason(out[-1], pre_l.get(c["tup"]), final)
        del out[-1]["rmline"]
    return out


def origins(seg, final, picked, rcalls):
    """[(temp, register, offset, phase)] for every temp the final code gives a register that is neither a RegAssign
    pick (picked: their temp names), a Color pop (seg['color']) nor a temp made by a Riscify call: where it got the
    register, read from the IL dumps at RewriteAsReal (end of Color) and Riscify's entry (cl 13.10)."""
    o = seg.get("origin") or {}
    color = seg.get("color") or {}
    made = {t for c in rcalls for t in c["made"]}
    ra, rp, rm = _ilregs(o.get("ra")), _ilregs(o.get("rp")) if "rp" in o else None, _ilregs(o.get("rm")) if "rm" in o else None
    out, seen, off = [], set(), 0
    for l in final:
        if not l.startswith("I "):
            continue
        f = l.split()
        for r, t in TREG.findall(l.split("|", 1)[1] if "|" in l else ""):
            if t in seen or t in picked or t in color or t in made:
                continue
            seen.add(t)
            if t in ra:
                ph = "Color: given %s before RewriteAsReal without a ChoosePhysReg pop (a temp tied to a fixed register)" % ra[t]
            elif rp is not None and t in rp:
                ph = "RewriteAsReal or RegSimpleAssign without a RegAssign call (a destination that takes its source's register)"
            elif rp is not None and rm is not None and t in rm:
                ph = "Riscify (no GetReg call of it matched)"
            elif rp is not None:
                ph = "after Riscify (RiscifyEarlyUndo, MdmAvoidMOBStalls or a later pass)"
            else:
                ph = "RewriteAsReal, RegSimpleAssign without a RegAssign call, or a later pass"
            out.append((t, _WIDE.get(r, r), off, ph, "%s | %s" % (f[2], l.split("|", 1)[1].strip() if "|" in l else "")))
        off += int(f[4])
    return out


def parse(log, funcpat):
    """-> (picks of the function body, final IL at @Encode@4).

    RegSimpleAssignBody runs once for the body and once per EH unwind funclet, all under the function's name (in
    0x008e7d40: 13 funclets first, then the body). The picks of each run are kept apart and the run with the most
    picks is returned: merging them shifted every pick number, cursor and offset."""
    segs, final = parse_segments(log, funcpat)
    return (max(segs, key=lambda s: len(s["picks"]))["picks"] if segs else []), final


def offsets(final):
    off, pos = 0, {}
    for l in final:
        if l.startswith("I "):
            f = l.split()
            pos[f[1]] = (off, f[2])
            off += int(f[4])
    return pos


def _how(p):
    return "ROT" if p["rot"] else ("pref" if p["pref"] and p["pref"] == p["reg"] else "fit")


def _instr(p):
    t = (p["text"] or "").split("|", 1)
    h = t[0].split()
    return (h[2] if len(h) > 2 else "?"), (h[3] if len(h) > 3 else None), (t[1] if len(t) > 1 else "")


def _pref_src(p):
    """The temp whose register the preference copies, if RegAssign derived the preference itself from a source
    operand that an earlier pick had just turned into a register (`t2 <- eax=t1`, `lea t2 <- [eax=t1+8]`)."""
    if p.get("pref_pre") or not p["pref"]:
        return None
    op, _, body = _instr(p)
    src = body.split("<-", 1)[1].split() if "<-" in body else []
    if not src:
        return None
    m = re.match(r"\[?%s=(t\d+):" % re.escape(p["pref"]), src[0])
    return m.group(1) if m else None


def annotate(seg):
    """Add to every pick of a --why/--export run: conf (registers its temp conflicts with), state of eax ecx edx,
    cand (the free, non-conflicting ones), how, and why (empty for a plain rotation pick that got the cursor's
    register). This restates RegAssign 10bda7c0 on the traced inputs; `rasim scratch` replays it with its own state."""
    conf = seg["conf"]
    ilname = {}         # IL name of a pick's destination -> its temp ("t<id>" of the tmpreg RegAssign was given). The
    for p in seg["picks"]:      # two differ when several IL temps share one tmpreg symbol.
        if "busy" not in p:
            continue
        p["how"] = _how(p)
        p["conf"] = None if conf is None else [RNAME[r] for r in sorted(conf) if r in RNAME and p["tmp"] in conf[r]]
        if p["byte"]:                                 # a byte temp cannot use esi edi ebp (RegAssign: reg >= 6)
            p["conf"] = sorted(set(p["conf"] or []) | {"esi", "edi", "ebp"})
        cf = set(p["conf"] or [])
        st = {r: ("busy " + p["busy"][r] if r in p["busy"] else "conflict" if r in cf else "free") for r in INTREGS}
        p["cand"] = [r for r in SCR if st[r] == "free"]
        p["pref_src"] = ilname.get(_pref_src(p))
        m = re.match(r"\s*(\w+):", _instr(p)[2])
        if m:
            ilname[m.group(1)] = "t%s" % p["tmp"]
        why = []
        if p["cls"] != "int":
            why.append("not an integer temp (type %04x): another register list and cursor" % p["type"])
        elif p["how"] == "pref":
            why.append("preferred %s (%s)" % (p["reg"], "set by the conflict builder: copy from/to that register" if p["pref_pre"]
                       else "source register of this %s%s" % (_instr(p)[0], ", held by " + p["pref_src"] if p["pref_src"] else "")))
        else:
            if p["pref"]:
                r = p["pref"]
                why.append("preference %s refused: %s" % (r, st.get(r, "?") if st.get(r) != "free" else
                           "not available" if r not in p["avail"] else "?"))
            if p["how"] == "ROT":
                k = SCR.index(p["cur"]) if p["cur"] in SCR else 0
                for r in SCR[k:] + SCR[:k]:
                    if r == p["reg"]:
                        break
                    why.append("%s %s" % (r, st[r]))
            else:
                why.append("no scratch register free (%s): %s" % (", ".join("%s %s" % (r, st[r]) for r in SCR),
                           "spill" if p["spill"] else "first fit over eax ecx edx esi edi ebx ebp"))
        p["why"] = "; ".join(why)


def export(path, func, segs, pos, rcalls=(), orig=()):
    body = max(range(len(segs)), key=lambda k: len(segs[k]["picks"])) if segs else None
    out = dict(kind="scratch", func=func, c2=c2trace.c2build.get("version", "14.00.50727.762"), order=INTREGS, scratch=SCR, runs=[])
    if rcalls:          # Riscify's GetReg calls (cl 13.10): `rasim scratch` replays them
        out["riscify"] = [dict(n=c["n"], block=c["block"], busy=c["busy"], byte=c["byte"], cur=c["cur_idx"],
                               order=[r or "" for r in c["order"]], count=c["count"], used=c["used"], reg=c["reg"],
                               made=c["made"], off=c["off"], op=c["op"], instr=c["instr"], why=c["why"]) for c in rcalls]
    if orig:            # temps whose register no modelled phase gave
        out["unmodelled"] = [dict(tmp=t, reg=r, off=o, phase=ph, instr=ins) for t, r, o, ph, ins in orig]
    for k, s in enumerate(segs):
        ps = []
        for p in s["picks"]:
            op, line, body_txt = _instr(p)
            at = pos.get(p["instr"])
            ps.append(dict(n=p["n"], tmp="t%s" % p["tmp"], cls=p["cls"], byte=p["byte"], block=p["block"], cur=p["cur"],
                           cur_after=p["cur_after"], pref=p["pref"] or None, pref_pre=p["pref_pre"] or None,
                           pref_src=p["pref_src"], how=p["how"], reg=p["reg"], busy=p["busy"], avail=p["avail"],
                           conf=p["conf"], cand=p["cand"], why=p["why"], spill=p["spill"],
                           off=None if at is None else at[0], op=op, instr=body_txt.strip()))
        out["runs"].append(dict(run=k, body=k == body, speed=s["speed"], global_conflicts=s["conf"] is not None,
                                blocks=s["blocks"], spills=s["spills"], fixes=s["fixes"], picks=ps))
    with open(path, "w") as f:
        json.dump(out, f, indent=1)


def main(argv):
    ap = argparse.ArgumentParser(prog="c2trace.py scratch", usage=__doc__)
    ap.add_argument("--func", required=True)
    ap.add_argument("--keep")
    ap.add_argument("--why", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--export")
    ap.add_argument("--phase", metavar="ORIG.s")
    ap.add_argument("--asm-func")
    ap.add_argument("--by", choices=("case", "block"), default="case")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--no-scan", action="store_true")
    ap.add_argument("--brief", action="store_true", help="--phase: only groups that differ")
    ap.add_argument("--color", action="store_true", help="list every Color variable written in the code, not only temps")
    ap.add_argument("src")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    if a.phase:
        import tempfile
        a.keep = a.keep or tempfile.mkdtemp(prefix="scratch_phase_")
        a.export = a.export or os.path.join(a.keep, "scratch_snap.json")
    ext = a.why or a.all or bool(a.export)
    if ext and c2trace.c2build.table() is not None and "scratch_x" not in c2trace.c2build.get("hooks", {}):
        sys.exit("scratch --why/--all/--export: the probes are not ported to c2 %s (no hooks.scratch_x in its table)"
                 % c2trace.c2build.name())
    hooks = HOOKS_X if ext else HOOKS
    oh = origin_hooks()
    img, hk, rc, out, log = c2trace.run_trace(a.src, hooks + _color_hooks() + [h for _, h in oh], cl, keep=a.keep)
    if rc:
        sys.stdout.write(out)
    segs, final = parse_segments(log, a.func, cbase=len(hooks), obase=len(hooks) + len(_color_hooks()),
                                 oroles=[r for r, _ in oh])
    segs = [s for s in segs if s["picks"]] or segs[:1]
    if not segs or not any(s["picks"] for s in segs):
        sys.exit("no scratch picks seen for %r" % a.func)
    pos = offsets(final)
    body = max(range(len(segs)), key=lambda k: len(segs[k]["picks"]))
    if ext:
        for s in segs:
            annotate(s)
    picked = {"t%d" % p["tmp"] for x in segs for p in x["picks"] if p["tmp"] is not None}
    named = picked | {m.group(1) for x in segs for p in x["picks"] for m in [re.match(r"\s*(t\d+):", _instr(p)[2])] if m}
    rcalls = riscify_calls(segs[body], pos, final)
    orig = origins(segs[body], final, named, rcalls)
    if a.export:
        export(a.export, a.func, segs, pos, rcalls, orig)
    if a.phase:
        asm = os.path.join(a.keep, os.path.splitext(os.path.basename(a.src))[0] + ".asm")
        return phase(a, segs[body]["picks"], pos, asm, a.export, body)
    for k, s in enumerate(segs):
        if k != body and not a.all:
            continue
        if a.all:
            print("# run %d of %d%s: %d picks, %d blocks" % (k + 1, len(segs), " (body)" if k == body else " (EH funclet)",
                                                          len(s["picks"]), s["blocks"]))
        if a.why:
            print("%4s  %-8s %-4s %-4s %-4s %-11s %6s  %s" % ("n", "cur>next", "pref", "how", "reg", "candidates", "off", "why / instr at pick time"))
        else:
            print("%4s  %-4s %-4s %-4s %-4s %6s  %s" % ("n", "cur", "pref", "how", "reg", "off", "instr at pick time"))
        for p in s["picks"]:
            op, line, txt = _instr(p)
            at = pos.get(p["instr"])
            off = "gone" if at is None else ("%05x" % at[0] + ("" if at[1] == op else "~"))
            ins = "%s %s|%s" % (op, " line %s " % line if line else "", txt)
            m = re.match(r"\s*(t\d+):", txt)
            if p["tmp"] is not None and (not m or m.group(1) != "t%d" % p["tmp"]):
                ins += "   [temp t%d in the final code]" % p["tmp"]      # several IL names share one tmpreg
            if a.why:
                print("%4d  %-8s %-4s %-4s %-4s %-11s %6s  %s%s" % (
                    p["n"], "%s>%s" % (p["cur"], p["cur_after"] or "?"), p["pref"] or "-", p["how"], p["reg"] or "?",
                    ",".join(p["cand"]) or "none", off, ("[%s]  " % p["why"]) if p["why"] else "", ins))
            else:
                print("%4d  %-4s %-4s %-4s %-4s %6s  %s" % (p["n"], p["cur"], p["pref"] or "-", _how(p), p["reg"] or "?", off, ins))
        if k == body:
            crow, hidden = color_rows(s, final, picked, a.color)
            print("# C: not a scratch pick. Color gave the value its register (`regs --why` says why); the numbered rows")
            print("#    are RegAssign's scratch picks. Where the code writes it: %s" % (
                "every Color variable" if a.color else "temporaries only, --color adds the named variables (%d rows)" % hidden))
            for o, r, nm, ins in crow:
                print("%4s  %-14s %-4s %6s  %s" % ("C", "Color " + nm, r, "%05x" % o, ins))
            if rcalls:
                print("# R: Riscify's GetReg calls (cl 13.10, after RegSimpleAssign; regalloc.md 6.6): it splits a memory operand"
                      " off into a load to a new\n#    temp; GetReg rotates over %s, restarting at the top of"
                      " each block, which Riscify walks from its last\n#    instruction up; every call moves the cursor,"
                      " also when the instruction stays as it was. `rasim scratch` replays it (rdrop:N, rins:N)"
                      % " ".join(r for r in rcalls[0]["order"] if r))
                for c in rcalls:
                    print("%4s  %3d blk %-3s %-9s %-4s %6s  busy %-23s %s | %s" % (
                        "R", c["n"], c["block"] if c["block"] is not None else "?",
                        "%s>%s" % (c["cur"] or "start", c["reg"] or "none"), c["reg"] or "none",
                        "gone" if c["off"] is None else "%05x" % c["off"], ",".join(c["busy"]) or "-",
                        ("made " + " ".join(c["made"]) if c["made"] else "(not split)") + (" byte" if c["byte"] else ""),
                        "%s %s" % (c["op"], c["instr"][:90])))
                    print("%4s  %3s %s" % ("", "", c["why"]))
            if orig:
                print("# ?: temps of the final code with no RegAssign pick, no Color pop and no Riscify call: where they"
                      " got their register\n#    (from the IL at RewriteAsReal%s); `rasim` does not model these phases"
                      % (" and at Riscify's entry" if "rp" in (segs[body].get("origin") or {}) else ""))
                for t, r, o, ph, ins in orig:
                    print("%4s  %-14s %-4s %6s  %s  [%s]" % ("?", t, r, "%05x" % o, ins[:80], ph))
        if ext and (s["spills"] or s["fixes"]):
            print("# this run: %d RegSpill, %d RegFixConflict (not modelled by rasim scratch)" % (s["spills"], len(s["fixes"])))
        if ext and s["conf"] is None:
            print("# this run did not use RegBuildGlobalConflict (not /O2): no conflict sets, candidates show _RegMap only")
    print("# off: listing offset of the instruction in the final code; '~' = its opcode changed later (e.g. mov ->"
          " xor); 'gone' = folded/deleted after the pick (it still moved the cursor if ROT)")
    if a.export:
        print("# wrote %s (%d runs, %d picks): rasim scratch %s" % (a.export, len(segs), sum(len(s["picks"]) for s in segs), a.export))
    return rc


# ---------------------------------------------------------------- --phase: our picks against the original's

PREG = re.compile(r"\b(?:e?[abcd]x|[abcd][lh]|e?si|e?di|e?bp|e?sp)\b")


def listing_groups(asm, func):
    """-> ([(offset, name)] case starts in layout order, from the jump tables of our listing; source line text per
    offset {offset: 'NN: text'}).  A case is named by its jump-table entries ('case[3]', or the byte-table indices
    that lead to it when the switch uses a byte table first) and its first source line."""
    text = open(asm, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)
    lines = text[i:j].splitlines()
    labels, pending, tables, cur_tab, srcl = {}, [], {}, None, {}
    last_src, src_pending = None, None
    for ln in lines:
        m = re.match(r"^(\$?[\w@$?]+):\s*$", ln)
        if m:
            pending.append(m.group(1))
            cur_tab = m.group(1)
            continue
        m = re.match(r"^; (\d+)\s*:\s?(.*)$", ln)
        if m:
            src_pending = "%s: %s" % (m.group(1), m.group(2).strip())
            continue
        m = re.match(r"\s+([0-9a-f]{5})\t[0-9a-f ]+\t+ (\w+)\s*(.*)$", ln)
        if not m:
            continue
        off, mn, ops = int(m.group(1), 16), m.group(2), re.sub(r"\s*;.*", "", m.group(3)).strip()
        if mn in ("DD", "DB"):
            if cur_tab is not None:
                tables.setdefault(cur_tab, (mn, []))[1].append(ops)
            pending = []
            continue
        cur_tab = None
        for l in pending:
            labels[l] = off
        pending = []
        if src_pending:
            srcl[off] = src_pending
            src_pending = None
    dd = {k: v[1] for k, v in tables.items() if v[0] == "DD" and all(x in labels for x in v[1])}
    db = {k: [int(x, 0) if not x.endswith("H") else int(x[:-1], 16) for x in v[1]] for k, v in tables.items() if v[0] == "DB"}
    starts = {}
    for tab, ents in dd.items():
        names = {}
        for k, l in enumerate(ents):
            names.setdefault(l, []).append(k)
        via = None
        for btab, idx in db.items():          # a byte table indexing this DD table: name cases by its indices
            if idx and max(idx) < len(ents):
                via = btab
        for l, ks in names.items():
            if via is not None:
                ks = [b for b, k in enumerate(db[via]) if k in ks]
            starts.setdefault(labels[l], []).extend(ks)
    out = []
    for off in sorted(starts):
        ks = sorted(set(starts[off]))
        nm = "case[%s]" % _ranges(ks)
        out.append((off, nm))
    return out, srcl


def _ranges(ks):
    r, k = [], 0
    while k < len(ks):
        e = k
        while e + 1 < len(ks) and ks[e + 1] == ks[e] + 1:
            e += 1
        r.append("%d" % ks[k] if e == k else "%d-%d" % (ks[k], ks[e]))
        k = e + 1
    if len(r) > 4:
        r = r[:4] + ["+%d" % (len(r) - 4)]
    return ",".join(r)


def orig_regs(picks, pos, asm, func, orig_path):
    """{pick n: (the original's register at the aligned instruction or None, why)}."""
    import regmap, tsweep
    lst = tsweep.listing_cs_offsets(asm, func)     # capstone of our bytes: the operand order of orig.s (test, xchg)
    offs = sorted(lst)
    ours = [regmap.norm((lst[o].split()[0], lst[o].split(None, 1)[1] if " " in lst[o] else "")) for o in offs]
    if orig_path.lower().endswith(".asm"):      # another compile's /FAs listing (a variant draft) as the "original"
        ol = tsweep.listing_cs_offsets(orig_path, func)
        src = [(ol[o].split()[0], ol[o].split(None, 1)[1] if " " in ol[o] else "") for o in sorted(ol)]
        src = [x for x in src if x[0] not in ("DD", "DB")]
    else:
        src = tsweep.orig_instrs(orig_path)
    orig = [regmap.norm(x) for x in src]
    pair = dict(regmap.align(ours, orig))
    at = {o: k for k, o in enumerate(offs)}
    res = {}
    for p in picks:
        x = pos.get(p["instr"])
        if x is None:
            res[p["n"]] = (None, "gone")
            continue
        i = at.get(x[0])
        if i is None or i not in pair:
            res[p["n"]] = (None, "unaligned")
            continue
        ra = [regmap.wide(r) for r in PREG.findall(ours[i][1])]
        rb = [regmap.wide(r) for r in PREG.findall(orig[pair[i]][1])]
        if len(ra) != len(rb) or p["reg"] not in ra:
            res[p["n"]] = (None, "no register operand to compare")
            continue
        res[p["n"]] = (rb[ra.index(p["reg"])], "")
    return res, len(pair), len(ours), len(orig)


def phase(a, picks, pos, asm, snap, body):
    func = a.asm_func or a.func
    got, npair, nours, norig = orig_regs(picks, pos, asm, func, a.phase)
    starts, srcl = listing_groups(asm, func) if a.by == "case" else ([], {})
    by = a.by if starts else "block"
    names, gof = [], {}
    import bisect
    soffs = [o for o, _ in starts]
    for p in picks:
        x = pos.get(p["instr"])
        if by == "case":
            k = bisect.bisect_right(soffs, x[0]) - 1 if x is not None else -1
            g = starts[k][1] if k >= 0 else "entry"
        else:
            g = "block %d" % p.get("block", 0)
        if g not in names:
            names.append(g)
        gof[p["n"]] = names.index(g)
    # first source line of each case
    first = {}
    if by == "case":
        so = sorted(srcl)
        for off, nm in starts:
            k = bisect.bisect_left(so, off)
            if k < len(so) and (k + 1 >= len(so) or True):
                first[nm] = srcl[so[k]][:60]
    print("# %s: %d picks; %d of our %d instructions aligned to %d of the original's; groups: %s (%d)" % (
        a.func, len(picks), npair, nours, norig, "switch cases (jump-table targets, layout order)" if by == "case"
        else "IL blocks", len(names)))
    print("# per group: our picks / the original's ('.' = not readable there), cursor phase of the rotation picks "
          "(original minus ours, in steps; ? = unknown), first pick that differs")
    print("%-18s %-6s %-30s %-30s %-6s %s" % ("group", "status", "ours", "original", "phase", "first difference"))
    want, rows = [], []
    for gi, nm in enumerate(names):
        ps = [p for p in picks if gof[p["n"]] == gi and p.get("cls", "int") == "int"]
        if not ps:
            continue
        ours = [p["reg"] or "?" for p in ps]
        theirs = [got[p["n"]][0] for p in ps]
        ph = []
        diff = None
        for p, o in zip(ps, theirs):
            if o is None:
                continue
            if o in SCR:
                want.append((p["n"], o))
            if o != p["reg"] and diff is None:
                diff = p
            if p.get("how", _how(p)) == "ROT" and o in SCR and p["reg"] in SCR:
                ph.append((SCR.index(o) - SCR.index(p["reg"])) % 3)
        known = sum(1 for o in theirs if o is not None)
        st = "-" if not known else ("exact" if diff is None else "DIFF")
        if ph:
            c = collections_counter(ph)
            top, n = c.most_common(1)[0]
            phs = ["0", "+1", "-1"][top] + ("" if n == len(ph) else "~")
        else:
            phs = "?"
        rows.append((nm, st, phs))
        if a.brief and st != "DIFF":
            continue
        fd = "" if diff is None else "pick %d %s block %d: ours %s, original %s" % (
            diff["n"], "+%05x" % pos[diff["instr"]][0] if pos.get(diff["instr"]) else "gone", diff.get("block", 0),
            diff["reg"], got[diff["n"]][0])
        print("%-18s %-6s %-30s %-30s %-6s %s" % (nm[:18], st, _seq(ours), _seq([t or "." for t in theirs]), phs, fd))
        if first.get(nm) and not a.brief:
            print("%18s line %s" % ("", first[nm]))
    known = [r for r in rows if r[1] != "-"]
    print("# %d groups with readable picks: %d exact, %d differ" % (len(known), sum(1 for r in known if r[1] == "exact"),
                                                                 sum(1 for r in known if r[1] == "DIFF")))
    runs, cur = [], None
    for nm, st, phs in known:
        p0 = phs.rstrip("~")
        if cur and cur[0] == p0:
            cur[2], cur[3] = nm, cur[3] + 1
        else:
            cur = [p0, nm, nm, 1]
            runs.append(cur)
    print("# phase runs (consecutive groups in one cursor phase): " + "; ".join(
        "%s %s..%s (%d)" % (r[0], r[1], r[2], r[3]) for r in runs[:30]) + (" ..." if len(runs) > 30 else ""))
    fdiff = next((r for r in known if r[1] == "DIFF"), None)
    if fdiff:
        print("# first group that differs: %s" % fdiff[0])
    if a.no_scan:
        return 0
    tgt = os.path.splitext(snap)[0] + "_target.json"
    json.dump(dict(want=want, group=[(p["n"], gof[p["n"]]) for p in picks], groups=names), open(tgt, "w"))
    rasim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rasim", "target", "release", "rasim")
    import subprocess
    r = subprocess.run([rasim, "scratch", snap, "--run", str(body + 1), "--target", tgt, "--top", str(a.top)],
                       capture_output=True, text=True)
    print("# re-alignment: every single inserted (ins:N) or removed (drop:N) pick, replayed with the pick rule")
    sys.stdout.write(r.stdout + r.stderr)
    print("# snapshot %s, target %s: rasim scratch %s --target %s [EDIT..] scans on top of EDITs" % (snap, tgt, snap, tgt))
    return 0


def collections_counter(x):
    import collections
    return collections.Counter(x)


def _seq(rs):
    s = " ".join(r[1] if r in SCR else r for r in rs)       # eax ecx edx -> a c d
    return s if len(s) <= 30 else s[:27] + "..."


if __name__ == "__main__":
    sys.exit(c2trace.pipe_safe(main, sys.argv[1:]))
