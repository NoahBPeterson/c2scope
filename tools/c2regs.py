#!/usr/bin/env python3
"""Register-allocation report for one function, from a traced compile.  Doc: docs/c2/tracer.md, docs/c2/regalloc.md

usage: c2trace.py regs <src> [--func SUBSTR] [--all-classes] [--raw] [--keep DIR] [-- <cl args>]

For every function whose decorated name contains SUBSTR (default: all), prints the globregs (register candidates:
user variables, parameters, multi-def temporaries) in the order c2's Color loop pops them off its priority list,
with the numbers the decision is made from:

  prio    GLOBREG.prio (+0x0c): Σ_blocks (cost of refs in block)·P_B·w_B − Σ_live-through P_B·w_B (regalloc.md §3.2)
  benefit GLOBREG.benefit (+0x40): Σ cost·w; <= 0 means "spill" (with one /O2 exception, §3.1)
  refs    GLOBREG.nrefs (+0x24), def = GLOBREG.defOrd (+0x44, tie-break among equal prio: lower first)
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
KIND = {3: "temp", 4: "local", 5: "param", 0xd: "imm"}
CLASS = {0: "int", 1: "x87 (GFx)"}


def sval(v):
    return v if v.startswith('"') or v in ("?", "IL") else int(v, 16)


def parse(log, funcpat):
    funcs, cur, cls, grs, pend = [], None, None, {}, None
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
                if (funcpat is None or funcpat in name) else None
            if cur is not None:
                funcs.append(cur)
            grs, terms, npass = {}, {}, 0
            continue
        if cur is None:
            continue
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
            first = grs.get(gp)
            if first is not None and first["out"] in ("spill/defer",):
                row["note"] = "re-popped"
            grs[gp] = row
            cur["rows"].append(row)
            pend = row
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
            sn, key, knd = v[-3], v[-2], v[-1]
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
    for np_ in sorted({k[0] for k in fn.get("blkP", {})}):
        blocks = [dict(b=b, w=w, P=P) for (pp, b), (w, P) in sorted(fn["blkP"].items()) if pp == np_]
        grs = {}
        for (pp, b), lst in fn.get("byblock", {}).items():
            if pp != np_:
                continue
            for nm, g, cost, d in lst:
                e = grs.setdefault("%x" % g, dict(name=nm, terms=[]))
                e["terms"].append(dict(b=b, cost=cost, delta=d))
        for e in grs.values():
            e["terms"].sort(key=lambda t: t["b"])
        passes.append(dict(n=np_, cls=fn.get("pcls", {}).get(np_), blocks=blocks, grs=grs,
                           split_removed=["%x" % g for g in fn.get("splitrm", {}).get(np_, [])]))
    pops = [dict(pop=r["pop"], name=r["name"], gr="%x" % r["gr"], prio=r["prio"], defOrd=r["dord"], reg=r["out"],
                 npass=r["npass"], cls=r["cls"], ben=r["ben"], kind=r["kind"], num=r["id"]) for r in fn["rows"]]
    return dict(func=fn["name"], passes=passes, pops=pops)


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
            out.append("      %-28s gr %-8x %s" % (nm, g, ("cost %d -> %+d" % (cost, d)) if cost is not None
                                                   else "live through -> %+d" % d))
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
                " ".join(r["cands"]), "  [%s]" % CLASS.get(r["cls"], "class %s" % r["cls"]) if all_classes else ""))
        for r in rows:
            if r["name"] not in why and "*" not in why:
                continue
            tot = sum(t[4] for t in r["terms"])
            out.append("  why %s (pop %d): prio %s = %s%s" % (r["name"], r["pop"], r["prio"], tot,
                       "" if tot == r["prio"] else "  (MISMATCH: terms do not add up; split or recomputed range)"))
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
