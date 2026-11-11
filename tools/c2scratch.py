#!/usr/bin/env python3
"""Scratch-register (tmpreg) picks for one function, mapped to listing offsets.  Doc: docs/c2/tracer.md (`scratch`)

usage: c2trace.py scratch <src> --func SUBSTR [--keep DIR] [-- <cl args>]

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
"""
import argparse, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace

REGNAME = "10cec7b8"
NEXTINTPOS = "10d0f854"      # _NextIntPos (.data 0003:38996)
NAME = "g10d123ac>0>0>4$"
HOOKS = [
    "@RegSimpleAssignBody@4=" + NAME,                                                      # 0
    "@RegAssign@8=s1>0x1c>0x1c,g%s>0>0~%s$,s2,s2!/xeax>0x1c~%s$,a1>0x1c>0x30>0x1c~%s$" % (
        NEXTINTPOS, REGNAME, REGNAME, REGNAME),                                             # 1  (_IntPhysRegs: bytes)
    "@CmdSetNextPos@8=eax",                                                                 # 2
    "@Encode@4=%s,g10d123ac>0>8>0>0x1c%%" % NAME,                                           # 3
]


def parse(log, funcpat):
    """-> (picks of the function body, final IL at @Encode@4).

    RegSimpleAssignBody runs once for the body and once per EH unwind funclet, all under the function's name (in
    0x008e7d40: 13 funclets first, then the body). The picks of each run are kept apart and the run with the most
    picks is returned: merging them shifted every pick number, cursor and offset."""
    segs, final, on, enc, pend, stack = [], None, False, False, None, []
    picks = []
    for ln in log.splitlines():
        if ln.startswith("T "):
            if enc:
                final.append(ln[2:])
            elif pend is not None and pend.get("text") is None:
                pend["text"] = ln[2:]
            continue
        if "|" not in ln or ln.startswith("#"):
            continue
        head, _, rest = ln.partition("|")
        f = head.split()
        ev, hk = f[0], int(f[3])
        v = c2trace._split(rest)
        if ev == "E" and hk in (0, 3):
            hit = funcpat in (v[0] if v else "")
            if hk == 0:
                on, enc = hit, False
                if hit:
                    picks = []
                    segs.append(picks)
            else:
                enc = hit and final is None
                if enc:
                    final = []
                on = False
            continue
        enc = False if ev == "E" else enc
        if not on:
            continue
        if ev == "E" and hk == 1:
            pend = dict(n=len(picks) + 1, tmp=int(v[0], 16) if v[0] != "?" else None, cur=v[1].strip('"'),
                        pref="", instr=v[2], text=None, rot=False, reg=None)
            picks.append(pend)
            stack.append(pend)
        elif ev == "E" and hk == 2 and stack:
            stack[-1]["rot"] = True
        elif ev == "X" and hk == 1 and stack:
            p = stack.pop()
            p["reg"] = v[0].strip('"') if v else "?"
            p["pref"] = v[1].strip('"') if len(v) > 1 and v[1] != "?" else ""
    body = max(segs, key=len) if segs else []
    for k, p in enumerate(body):
        p["n"] = k + 1
    return body, final or []


def offsets(final):
    off, pos = 0, {}
    for l in final:
        if l.startswith("I "):
            f = l.split()
            pos[f[1]] = (off, f[2])
            off += int(f[4])
    return pos


def main(argv):
    ap = argparse.ArgumentParser(prog="c2trace.py scratch", usage=__doc__)
    ap.add_argument("--func", required=True)
    ap.add_argument("--keep")
    ap.add_argument("src")
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    a = ap.parse_args(argv)
    img, hk, rc, out, log = c2trace.run_trace(a.src, HOOKS, cl, keep=a.keep)
    if rc:
        sys.stdout.write(out)
    picks, final = parse(log, a.func)
    if not picks:
        sys.exit("no scratch picks seen for %r" % a.func)
    pos = offsets(final)
    print("%4s  %-4s %-4s %-4s %-4s %6s  %s" % ("n", "cur", "pref", "how", "reg", "off", "instr at pick time"))
    for p in picks:
        t = (p["text"] or "").split("|", 1)
        op = t[0].split()[2] if len(t[0].split()) > 2 else "?"
        at = pos.get(p["instr"])
        off = "gone" if at is None else ("%05x" % at[0] + ("" if at[1] == op else "~"))
        how = "ROT" if p["rot"] else ("pref" if p["pref"] and p["pref"] == p["reg"] else "fit")
        print("%4d  %-4s %-4s %-4s %-4s %6s  %s %s|%s" % (p["n"], p["cur"], p["pref"] or "-", how, p["reg"] or "?",
                                                      off, op, " line %s " % t[0].split()[3] if len(t[0].split()) > 3 else "",
                                                      t[1] if len(t) > 1 else ""))
    print("# off: listing offset of the instruction in the final code; '~' = its opcode changed later (e.g. mov ->"
          " xor); 'gone' = folded/deleted after the pick (it still moved the cursor if ROT)")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
