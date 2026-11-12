#!/usr/bin/env python3
"""Scratch-register (tmpreg) picks for one function, mapped to listing offsets.  Doc: docs/c2/tracer.md (`scratch`)

usage: c2trace.py scratch <src> --func SUBSTR [--why] [--all] [--export snap.json] [--keep DIR] [-- <cl args>]
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

--why (14.00 only) adds what RegAssign saw: the cursor after the pick, the candidate set (which of eax ecx edx were
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

# 14.00.50727.762 only: what RegAssign decides from (--why / --export). All addresses read in the code (regalloc.md §6).
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
if c2trace.c2build.table() is not None and "scratch_x" in c2trace.c2build.get("hooks", {}):
    # a ported build with its own --why/--export events (tools/c2builds/*.json: hooks.scratch_x, globals._RegSym)
    HOOKS_X = c2trace.c2build.hooks("scratch_x", HOOKS_X)
    REGSYM = int(c2trace.c2build.get("globals")["_RegSym"], 16)
    _IMGBASE = int(c2trace.c2build.get("image_base"), 16)


def _hex(s):
    try:
        return int(s, 16)
    except (TypeError, ValueError):
        return None


def _words(s):
    return [_hex(x) for x in s.strip("[]").split()] if s and s.startswith("[") else None


def parse_segments(log, funcpat):
    """-> (runs, final IL at @Encode@4). One run per RegSimpleAssignBody call for the function (EH funclets, then
    the body): dict(picks=[...], conf={reg number: set of temp ids} or None, blocks, spills, fixes, speed)."""
    segs, final, on, enc, pend, stack = [], None, False, False, None, []
    seg = None
    regsym = REGSYM
    for ln in log.splitlines():
        if ln.startswith("T "):
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
        if ev == "E" and hk in (0, 3):
            hit = c2trace.func_match(funcpat, v[0] if v else "")
            if hk == 0:
                on, enc = hit, False
                if hit:
                    seg = dict(picks=[], conf=None, blocks=0, spills=0, fixes=[], speed=_hex(v[1]) if len(v) > 1 else None,
                               T=set(), last=None, ptr={})
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


def export(path, func, segs, pos):
    body = max(range(len(segs)), key=lambda k: len(segs[k]["picks"])) if segs else None
    out = dict(kind="scratch", func=func, c2=c2trace.c2build.get("version", "14.00.50727.762"), order=INTREGS, scratch=SCR, runs=[])
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
    img, hk, rc, out, log = c2trace.run_trace(a.src, HOOKS_X if ext else HOOKS, cl, keep=a.keep)
    if rc:
        sys.stdout.write(out)
    segs, final = parse_segments(log, a.func)
    segs = [s for s in segs if s["picks"]] or segs[:1]
    if not segs or not any(s["picks"] for s in segs):
        sys.exit("no scratch picks seen for %r" % a.func)
    pos = offsets(final)
    body = max(range(len(segs)), key=lambda k: len(segs[k]["picks"]))
    if ext:
        for s in segs:
            annotate(s)
    if a.export:
        export(a.export, a.func, segs, pos)
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
            if a.why:
                print("%4d  %-8s %-4s %-4s %-4s %-11s %6s  %s%s" % (
                    p["n"], "%s>%s" % (p["cur"], p["cur_after"] or "?"), p["pref"] or "-", p["how"], p["reg"] or "?",
                    ",".join(p["cand"]) or "none", off, ("[%s]  " % p["why"]) if p["why"] else "", ins))
            else:
                print("%4d  %-4s %-4s %-4s %-4s %6s  %s" % (p["n"], p["cur"], p["pref"] or "-", _how(p), p["reg"] or "?", off, ins))
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
    lst = tsweep.listing_offsets(asm, func)
    offs = sorted(lst)
    ours = [regmap.norm((lst[o].split()[0], lst[o].split(None, 1)[1] if " " in lst[o] else "")) for o in offs]
    if orig_path.lower().endswith(".asm"):      # another compile's /FAs listing (a variant draft) as the "original"
        ol = tsweep.listing_offsets(orig_path, func)
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
    sys.exit(main(sys.argv[1:]))
