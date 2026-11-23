#!/usr/bin/env python3
"""Every tail and head merge of the late flow optimiser, and why each candidate pair stopped.  Doc: docs/c2/il_fg.md §7.5

usage: c2trace.py merges <src> --func SUBSTR [--pairs] [--off HEX,..] [--all] [--keep DIR] [--max N] [-- <cl args>]
       c2trace.py merges --from DIR/c2trace.log --func SUBSTR [...]   (a log kept by --keep: no compile)
       c2trace.py il <src> --func SUBSTR --merges [...]          (the same report)

FgDoLateFlowOpts (dop2_FinalFlowOpts, mode 2) tries, over the final machine instructions:
  tail  FgTryCommonTailMerge(label) -> FgDoCommonTailMerge(J1, J2) for each pair of `jmp label`: walk back from
        both jumps comparing instructions (FgMdInstrsMatch), then the size rule (layout.md §6)
  ftail FgTryTailMerge(jmp L): the jump's block against the code that falls into L (DAG match, no size rule)
  head  FgTryCommonHeadMerge(label) -> FgDoCommonHeadMerge, and FgTryHeadMerge(jcc): identical leading
        instructions hoisted above the branch
  cross FgCrossJumpRemove(jmp): `jcc; d = s1; jmp; L: d = s2` (il_fg.md §7.3)
For each tail pair it prints the two jumps (tuple, block number in the dump at the pass, source line relative to
the function, listing offset in the final code or `gone`), how many instructions matched, and the verdict:
  merged                      the duplicate was removed (`size` = the size rule's count + credit when it ran)
                              (17.00: `(roles swapped: J2's copy removed)` when J1's walk reached J2's label)
  stop: call target f2 / f1   the first differing instruction is a call to another symbol (the compare is by
                              symbol, not by body: two functions that /OPT:ICF folds later still differ here)
  stop: register ecx / eax    the instructions differ only in a register (scratch rotation, allocation)
  stop: operand X / Y         another operand differs (constant, memory, label)
  stop: instruction A / B     different opcodes
  stop: same text             equal in the dump; refused for what it does not show: an esp reference while
                              _FCantMergeStackRef is set (a jump with tuple+9 bit 8 in a function without a frame
                              pointer), type bits
  stop: other jump / start    the walk reached the other jump or ran out of instructions
  size N <= 20                matched, but the size rule refused (whole-block exception not met)
--pairs lists every pair (default: merged pairs, and refused pairs with at least one matched instruction or a
call/register stop, which are the ones a source edit can change); --off keeps the pairs and merges that involve an
instruction at one of these listing offsets (a jump, or a compared instruction: what to pass for one case of a big
switch); --all also prints FgDoLateFlowOpts runs other than mode 2.
Builds: 14.00 (default), 13.10.3077, 13.10.3052, 15.00, 17.00 x64.
"""
import argparse, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace, c2il, c2scratch
from c2trace import c2build

# per build: function hooks with their argument specs, and the two probes inside FgDoCommonTailMerge
#   walk: where the backward walk ended: (match count, J1-side tuple, J2-side tuple, whole-block point J1, J2)
#   size: the size rule's total (size + credit) and _Speed (limit = 20 if _Speed else 2)
BUILDS = {
    "14.00": dict(
        late="@FgDoLateFlowOpts@8=eax,ecx>0>4$", once="@FgDoLateFlowOptsOnce@20",
        trytail="@FgTryCommonTailMerge@8=s1/xeax", dotail="@FgDoCommonTailMerge@20=ecx,edx,s1/xeax",
        walk="10b30a1f=s9,edi,esi,s12,s13", size="10b30b6a=ebp,ebx",
        match="@FgMdInstrsMatch@8=eax,ebx,eax>0x28>0x18>4$,ebx>0x28>0x18>4$/xeax", ftail="@FgTryTailMerge@16=s1/xeax",
        cross="@FgCrossJumpRemove@4=s1/xeax", tryhead="@FgTryCommonHeadMerge@4=edx/xeax",
        dohead="@FgDoCommonHeadMerge@8=s1,s2/xeax", jcchead="@FgTryHeadMerge@16=s1/xeax"),
    # 13.10: read in the BBT-scattered code (il_fg.md §7.5); 3052 found by the unique masked byte context of 3077
    "13.10.3077": dict(
        late="@FgDoLateFlowOpts@8=eax,ecx>0>4$", once="@FgDoLateFlowOptsOnce@8",
        trytail="@FgTryCommonTailMerge@4=s1/xeax", dotail="@FgDoCommonTailMerge@16=s1,s2,s3/xeax",
        walk="107b8aac=s6,esi,ebx,s9,s10", size="1079f7c5=ebp,edi",
        match="@FgMdInstrsMatch@8=eax,s1,eax>0x28>0x18>4$,s1>0x28>0x18>4$/xeax", ftail="@FgTryTailMerge@4=s1/xeax",
        cross="@FgCrossJumpRemove@4=s1/xeax", tryhead="@FgTryCommonHeadMerge@4=edx/xeax",
        dohead="@FgDoCommonHeadMerge@8=s1,s2/xeax", jcchead="@FgTryHeadMerge@4=s1/xeax"),
}
BUILDS["13.10.3052"] = dict(BUILDS["13.10.3077"], walk="107ac90f=s6,esi,ebx,s9,s10", size="107afab8=ebp,edi")
# 15.00 (PDB names, ebp frames): FgDoCommonTailMerge(ecx = A, edx = B, [pass, &out]); the walk keeps B in esi
BUILDS["15.00.30729.1"] = dict(
    late="?FgDoLateFlowOpts@@YIXPAUtagFUNC@@W4FG_MERGE_ENUM@@@Z=eax,ecx>0>4$",
    once="?FgDoLateFlowOptsOnce@@YIHPAUtagFUNC@@W4FG_MERGE_ENUM@@PAUtagFGDAGGRAPH@@2PAH@Z",
    trytail="?FgTryCommonTailMerge@@YIHPAUtagFUNC@@PAUtagTUPLE@@@Z=s1/xeax",
    dotail="?FgDoCommonTailMerge@@YIHPAUtagFUNC@@PAUtagTUPLE@@1KPAH@Z=edx,ecx,s1/xeax",
    walk="5107d4bd=ebp>-0x5c,esi,ebx,ebp>-0x74,ebp>-0x6c", size="5107d609=ecx,edi",
    match="?FgMdInstrsMatch@@YIEPAUtagTUPLE@@0@Z=eax,ebx,eax>0x28>0x18>4$,ebx>0x28>0x18>4$/xeax",
    ftail="?FgTryTailMerge@@YIHPAUtagTUPLE@@PAUtagFGDAGGRAPH@@1PAH@Z=s1/xeax",
    cross="?FgCrossJumpRemove@@YIPAUtagTUPLE@@PAU1@@Z=s1/xeax",
    tryhead="?FgTryCommonHeadMerge@@YIHPAUtagTUPLE@@@Z=edx/xeax",
    dohead="?FgDoCommonHeadMerge@@YIHPAUtagTUPLE@@0@Z=s1,s2/xeax",
    jcchead="?FgTryHeadMerge@@YIHPAUtagTUPLE@@PAUtagFGDAGGRAPH@@1PAH@Z=s1/xeax")
# 17.00 x64 (PDB names; plain fastcall: ecx, edx, then the stack; tuples: +4 next, +0x18 prev, +8 opcode, +0xc kind):
# FgDoCommonTailMerge(ecx = FUNC, edx = B (J1, its copy is removed), [esp+4] = A (J2), [pass, &out]), `ret 0xc`.
# The walk (from B in esi, from A in edi, both via +0x18) ends at 0x100b4fb0 (every stop: the compare failed, the
# other jump or a label reached, a branch / return on J1's side): [ebp-8] matches, esi / edi, [ebp-0x30] / [ebp-0x34]
# the whole-block point; the size rule at 0x100b5140: ebx = csize total + the larger credit (+0x24), Speed = TLS +0xdd08.
# At the exit *out (a3) is 1 when the roles swapped (J1's walk reached J2's label: J2's copy is the one removed)
_F17 = "?%s@@YI%sZ"
BUILDS["17.00.61030-x64"] = dict(
    late=_F17 % ("FgDoLateFlowOpts", "XPAUtagFUNC@@W4FG_MERGE_ENUM@@@") + "=edx,ecx>0>4$",
    once=_F17 % ("FgDoLateFlowOptsOnce", "HPAUtagFUNC@@W4FG_MERGE_ENUM@@PAUtagFGDAGGRAPH@@2PAH@"),
    trytail=_F17 % ("FgTryCommonTailMerge", "HPAUtagFUNC@@PAVTuple@@@") + "=edx/xeax",
    dotail=_F17 % ("FgDoCommonTailMerge", "HPAUtagFUNC@@PAVTuple@@1KPAH@") + "=edx,s1,s2/xeax,a3>0",   # + *out
    walk="100b4fb0=ebp>-8,esi,edi,ebp>-0x30,ebp>-0x34", size="100b5140=ebx,esi>0xdd08",
    match=_F17 % ("FgMdInstrsMatch", "EPAVTuple@@0@") + "=ecx,edx,ecx>0x30>0x24>4$,edx>0x30>0x24>4$/xeax",
    ftail=_F17 % ("FgTryTailMerge", "HPAVTuple@@PAUtagFGDAGGRAPH@@1PAH@") + "=ecx/xeax",
    cross=_F17 % ("FgCrossJumpRemove", "PAVTuple@@PAV1@@") + "=ecx/xeax",
    tryhead=_F17 % ("FgTryCommonHeadMerge", "HPAUtagFUNC@@PAVTuple@@@") + "=edx/xeax",
    dohead=_F17 % ("FgDoCommonHeadMerge", "HPAUtagFUNC@@PAVTuple@@1@") + "=edx,s1/xeax",
    jcchead=_F17 % ("FgTryHeadMerge", "HPAVTuple@@PAUtagFGDAGGRAPH@@1PAH@") + "=ecx/xeax")
KEYS = ["late", "once", "trytail", "dotail", "walk", "size", "match", "ftail", "cross", "tryhead", "dohead",
        "jcchead", "final"]
REGS = {"eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi", "al", "cl", "dl", "bl", "ah", "ch", "dh", "bh",
        "ax", "cx", "dx", "bx", "si", "di", "bp", "sp"}
# x64 (17.00): the 64-bit names, r8..r15 with their d / w / b parts, the byte registers of rsi .. rsp, xmm0..15
REGS |= {"r" + x for x in ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp")} | {"sil", "dil", "bpl", "spl"} | \
    {"r%d%s" % (n, w) for n in range(8, 16) for w in ("", "d", "w", "b")} | {"xmm%d" % n for n in range(16)}
MEMREG = re.compile(r"\b(e[abcd]x|e[sd]i|e[bs]p|r[abcd]x|r[sd]i|r[bs]p|r(?:8|9|1[0-5])[dwb]?)\b")
ILINE = re.compile(r"^I (\S+) (\S+) (\d+) (\d+) \|(.*?)(?: ; (.*))?$")


def hooks_for(build):
    b = BUILDS.get(build)
    if not b:
        sys.exit("merges: c2 build %s not ported (14.00, 13.10.3077, 13.10.3052, 15.00.30729.1, 17.00.61030-x64)" % build)
    h = dict(b)
    h["once"] = b["once"] + c2il.SPEC                 # name + IL dump at each FgDoLateFlowOptsOnce
    # @Encode@4: the final IL with encoded lengths (offsets); on x64 the scratch hooks are not ported, so the same
    # event is written from the build's IL spec
    h["final"] = "@Encode@4" + c2il.SPEC if c2build.get("arch") == "x64" else c2scratch.HOOKS[3]
    return [h[k] for k in KEYS]


def _ints(vals):
    out = []
    for v in vals:
        try:
            out.append(int(v, 16))
        except ValueError:
            out.append(v)
    return out


def _demux(log):
    """The log's lines, one thread after the other: cl 17.00's c2 compiles functions on worker threads and the
    tracer writes `# thread N` before an event of another thread (tracer.md); a function's events, from
    FgDoLateFlowOpts to Encode, stay on one thread. A single-threaded log comes back as it is."""
    if "\n# thread " not in log:
        return log.splitlines()
    per, thr = {}, "0"
    for ln in log.splitlines():
        if ln.startswith("# thread "):
            thr = ln.split()[2]
            continue
        per.setdefault(thr, []).append(ln)
    return [ln for t in per for ln in per[t]]


def parse(log, hooks):
    """-> list of functions: {name, base, dumps: {tuple: (line, block#)}, final: {tuple: off}, events: [...]}."""
    kind = {i: k for i, k in enumerate(KEYS)}
    funcs, cur, il, ildst = [], None, None, None
    stack = []                                         # open entries: (kind, record)
    for ln in _demux(log):
        if ln.startswith("T "):
            if ildst is not None:
                ildst.append(ln[2:])
            continue
        if "|" not in ln or ln[:2] not in ("E ", "X "):
            continue
        head, _, rest = ln.partition("|")
        f = head.split()
        k = kind[int(f[3])]
        vals = c2trace._split(rest)
        if f[0] == "E":
            ildst = None
            if k == "late":
                cur = dict(name=vals[1].strip('"'), mode=int(vals[0], 16), lines={}, final={}, ev=[], it=0)
                funcs.append(cur)
                stack = []
                continue
            if k == "final":
                name = vals[0].strip('"')
                tgt = next((g for g in reversed(funcs) if g["name"] == name), None)
                ildst = [] if tgt is not None else None
                if tgt is not None:
                    tgt["_final"] = ildst
                continue
            if cur is None:
                continue
            if k == "once":
                cur["it"] += 1
                ildst = []
                cur.setdefault("_dumps", []).append(ildst)
                continue
            rec = dict(kind=k, args=_ints(vals), seq=int(f[1]), depth=int(f[2]), sub=[], ret=None, it=cur["it"])
            # attach to the innermost open record that encloses it (depth from the DLL's shadow stack)
            while stack and stack[-1]["depth"] >= rec["depth"] and stack[-1]["kind"] not in ("walk", "size"):
                stack.pop()
            parent = stack[-1] if stack else None
            if k in ("walk", "size"):                  # probes: no exit, belong to the open FgDoCommonTailMerge
                p = next((s for s in reversed(stack) if s["kind"] == "dotail"), None)
                if p is not None:
                    p[k] = rec["args"]
                continue
            (parent["sub"] if parent else cur["ev"]).append(rec)
            stack.append(rec)
        else:
            if cur is None:
                continue
            d = int(f[2])
            while stack and stack[-1]["depth"] > d:
                stack.pop()
            if stack and stack[-1]["depth"] == d and stack[-1]["kind"] == k:
                stack[-1]["ret"] = int(vals[0], 16) if vals and vals[0] != "?" else None
                stack[-1]["xvals"] = vals
                stack.pop()
    for g in funcs:
        lines, blk = {}, 0
        g["nblk"] = max([sum(1 for l in d if l.startswith("B ")) for d in g.get("_dumps", [])] or [0])
        for dump in g.pop("_dumps", []):
            blk = 0
            for l in dump:
                if l.startswith("B "):
                    blk += 1
                    continue
                m = ILINE.match(l)
                if m:
                    lines[m.group(1)] = dict(text=l, op=m.group(2), line=int(m.group(3)), blk=blk,
                                            ops=m.group(5), notes=m.group(6) or "")
                elif l.startswith("L "):
                    lines[l.split()[1]] = dict(text=l, op="label", line=None, blk=blk, ops="", notes="")
        g["lines"] = lines
        g["final"] = c2scratch.offsets(g.pop("_final", []) or [])
    return funcs


def _tokens(ops):
    """Operands of an IL line, split at the top level (memory operands stay whole)."""
    out, cur, lvl = [], "", 0
    for ch in ops:
        lvl += ch == "["
        lvl -= ch == "]"
        if ch == " " and not lvl:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _bare(tok):
    """eax=t12:1004 -> eax; the register part of a phys=pseudo operand, the text without the type otherwise."""
    t = tok.split(":")[0]
    return t.split("=")[0] if "=" in t else t


def classify(a, b, names=None):
    """Why FgMdInstrsMatch said no to IL lines a (J1 side) and b (J2 side): (reason, detail). `names`: the two
    instructions' first-source symbol names read at the compare (the call target of a call)."""
    if a is None or b is None:
        return "instruction", "(not in the dump)"
    if a["op"] != b["op"]:
        return "instruction", "%s / %s" % (a["op"], b["op"])
    ta, tb = _tokens(a["ops"]), _tokens(b["ops"])
    na, nb = a["notes"], b["notes"]
    call = "call" in a["op"].lower()
    if call and names and len(names) == 2 and all(isinstance(x, str) and x.strip('"') for x in names) \
            and names[0] != names[1]:
        return "call target", "%s / %s" % (names[0].strip('"'), names[1].strip('"'))
    if call:
        ca = re.search(r"\bs0=(\S+)", na)
        cb = re.search(r"\bs0=(\S+)", nb)
        if ca and cb and ca.group(1) != cb.group(1):
            return "call target", "%s / %s" % (ca.group(1), cb.group(1))
    for x, y in zip(ta, tb):
        if x == y:
            continue
        bx, by = _bare(x), _bare(y)
        if bx in REGS and by in REGS and bx != by:
            return "register", "%s / %s" % (bx, by)
        if call and x != y and "[" not in x and not x.startswith(("s", "t", "k")) or call and bx != by and "[" not in x:
            return "call target", "%s / %s" % (bx, by)
        mx = MEMREG.findall(x)
        my = MEMREG.findall(y)
        if mx != my and mx and my:
            return "register", "%s / %s" % (x, y)
        return "operand", "%s / %s" % (x, y)
    if len(ta) != len(tb):
        return "operand", "%d / %d operands" % (len(ta), len(tb))
    if na != nb:
        return "operand", "%s / %s" % (na or "-", nb or "-")
    if re.search(r"\b[er]sp\b", a["ops"]):
        return "same text", "(uses esp / rsp: refused under _FCantMergeStackRef, or a field the dump does not show)"
    return "same text", "(a field the dump does not show: type bits +0xa, +0x3e)"


def where(g, t):
    """`tuple B<block> l<line> @off` for a tuple."""
    key = "%x" % t if isinstance(t, int) else str(t)
    li = g["lines"].get(key)
    off = g["final"].get(key)
    s = key
    if li:
        s += "" if li["line"] is None else " l%d" % li["line"]
    s += " @%05x" % off[0] if off else " gone"
    return s


def short(g, t):
    li = g["lines"].get("%x" % t if isinstance(t, int) else str(t))
    if not li:
        return "?"
    return li["op"] + " " + li["ops"].strip() + (" ; " + li["notes"] if li["notes"] else "")


def tail_pair(g, r):
    j1, j2 = r["args"][0], r["args"][1]
    cmps = [s for s in r["sub"] if s["kind"] == "match"]
    walk = r.get("walk")
    nmatch = walk[0] if walk and isinstance(walk[0], int) else sum(1 for c in cmps if c["ret"] and c["ret"] & 0xff)
    verdict = ""
    last = cmps[-1] if cmps else None
    if r["ret"]:
        verdict = "merged"
        if len(r.get("xvals") or []) > 1 and r["xvals"][1] == "1":     # *out (17.00's hook): the roles swapped
            verdict = "merged (roles swapped: J2's copy removed)"
    elif last is not None and last["ret"] is not None and not last["ret"] & 0xff:
        why, det = classify(g["lines"].get("%x" % last["args"][0]), g["lines"].get("%x" % last["args"][1]),
                            last["args"][2:4])
        verdict = "stop: %s %s" % (why, det)
    elif walk and walk[3]:
        verdict = "stop: branch/return on J1's side (whole block)"
    else:
        verdict = "stop: other jump / start"
    if r.get("size"):
        tot, speed = r["size"][0], r["size"][1]
        lim = 20 if speed else 2
        verdict += "; size %d %s %d" % (tot, ">" if tot > lim else "<=", lim)
    elif not r["ret"] and nmatch and verdict.startswith("stop"):
        pass
    return dict(j1=j1, j2=j2, n=nmatch, verdict=verdict, merged=bool(r["ret"]), cmps=cmps, last=last)


def _offs(g, ts):
    out = set()
    for t in ts:
        o = g["final"].get("%x" % t if isinstance(t, int) else str(t))
        if o:
            out.add(o[0])
    return out


def report(g, pairs_all, offs=None):
    """Report lines per FgDoLateFlowOptsOnce iteration; an iteration whose lines equal the previous one's is folded."""
    out, prev = [], None
    for it in sorted({r["it"] for r in g["ev"]}):
        lines = _report(g, [r for r in g["ev"] if r["it"] == it], pairs_all, offs)
        if not lines:
            continue
        if lines == prev:
            out.append("-- iteration %d of FgDoLateFlowOptsOnce: the same" % it)
            continue
        out.append("-- iteration %d of FgDoLateFlowOptsOnce" % it)
        out.extend(lines)
        prev = lines
    return out


def _report(g, evs, pairs_all, offs=None):
    out = []
    for r in evs:
        k = r["kind"]
        if k == "trytail":
            pairs = [tail_pair(g, s) for s in r["sub"] if s["kind"] == "dotail"]
            if not pairs:
                continue
            shown = [p for p in pairs if pairs_all or p["merged"] or p["n"] or
                     re.search(r"call target|register", p["verdict"])]
            if offs:
                shown = [p for p in shown if _offs(g, [p["j1"], p["j2"]] + [x for c in p["cmps"] for x in c["args"][:2]]) & offs]
                if not shown:
                    continue
            out.append("tail L%x: %d pair(s)%s%s" % (r["args"][0], len(pairs),
                       ", %d merged" % sum(p["merged"] for p in pairs) if any(p["merged"] for p in pairs) else "",
                       "" if len(shown) == len(pairs) else ", %d shown (--pairs: all)" % len(shown)))
            for p in shown:
                out.append("  J1 %s  J2 %s: %d matched: %s" % (where(g, p["j1"]), where(g, p["j2"]), p["n"], p["verdict"]))
                if p["last"] is not None and p["verdict"].startswith("stop") and p["last"]["ret"] is not None \
                        and not p["last"]["ret"] & 0xff:
                    a, b = p["last"]["args"][0], p["last"]["args"][1]
                    out.append("      J1 side %s: %s" % (where(g, a), short(g, a)))
                    out.append("      J2 side %s: %s" % (where(g, b), short(g, b)))
        elif k in ("ftail", "cross", "jcchead") and (r["ret"] or pairs_all) and (not offs or _offs(g, r["args"][:1]) & offs):
            n = sum(1 for s in r["sub"] if s["kind"] == "match" and s["ret"] and s["ret"] & 0xff)
            name = {"ftail": "fall-through tail (FgTryTailMerge)", "cross": "cross jump (FgCrossJumpRemove)",
                    "jcchead": "head of a jcc (FgTryHeadMerge)"}[k]
            out.append("%s at %s: %s%s" % (name, where(g, r["args"][0]), "merged" if r["ret"] else "no",
                                           ", %d instruction(s) matched" % n if k != "cross" else ""))
        elif k == "tryhead":
            subs = [s for s in r["sub"] if s["kind"] == "dohead"]
            for s in subs:
                if not (s["ret"] or pairs_all) or offs and not _offs(g, s["args"][:2]) & offs:
                    continue
                n = sum(1 for c in s["sub"] if c["kind"] == "match" and c["ret"] and c["ret"] & 0xff)
                out.append("head L%x: %s  %s: %s, %d matched" % (r["args"][0], where(g, s["args"][0]),
                           where(g, s["args"][1]), "merged" if s["ret"] else "no", n))
    return out


def main(argv):
    ap = argparse.ArgumentParser(prog="c2trace.py merges", usage=__doc__.replace("%", "%%"))
    ap.add_argument("--func", required=True)
    ap.add_argument("--pairs", action="store_true")
    ap.add_argument("--off", help="listing offsets (hex, comma-separated): only what involves them")
    ap.add_argument("--all", action="store_true", help="every FgDoLateFlowOpts run (also mode 0 / funclets)")
    ap.add_argument("--keep")
    ap.add_argument("--max", type=int)
    ap.add_argument("--merges", action="store_true", help=argparse.SUPPRESS)   # `il --merges` forwards here
    ap.add_argument("--from", dest="fromlog", action="store_true",
                    help="SRC is a c2trace.log kept (--keep) by this tool: report it without compiling")
    ap.add_argument("src")
    argv, cl = c2trace.split_cl(argv)
    a = ap.parse_args(argv)
    if a.fromlog:
        rc, log, hk = 0, open(a.src, errors="replace").read(), None
    else:
        hk_texts = hooks_for(c2build.name())
        img, hk, rc, out, log = c2trace.run_trace(a.src, hk_texts, cl, maxn=a.max, keep=a.keep)
        if rc:
            sys.stdout.write(out)
    funcs = [g for g in parse(log, hk) if c2trace.func_match(a.func, g["name"])]
    if not funcs:
        sys.exit("function %r not seen at FgDoLateFlowOpts" % a.func)
    for g in funcs:
        if g["mode"] != 2 and not a.all:
            continue
        print("== %s  FgDoLateFlowOpts mode %d (a tuple is shown as: address, l<n> source line offset in the "
              "function, @off listing offset or gone)" % (g["name"], g["mode"]))
        lines = report(g, a.pairs, {int(x, 16) for x in a.off.split(",")} if a.off else None)
        print("\n".join(lines) if lines else "  (no merge candidates)")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
