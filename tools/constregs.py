#!/usr/bin/env python3
"""Constants as register candidates, per function: which constants Color saw, what they cost the others, what became
of each, and which source change drops (or keeps) a constant's candidacy.  Doc: docs/c2/immcand.md section 6.

usage: uv run tools/constregs.py SRC --func S [--c2 BUILD] [--value V] [--keep DIR] [--export SNAP] [-- <cl args>]
       uv run tools/constregs.py --from SNAP [--value V]

One traced compile: the hooks of `c2trace.py regs` plus an IL dump at `@Globregs@4` entry, where the constants are
still literal operands (CmdIdentifyImmedCandidates runs first in Globregs and turns each qualifying operand into the
candidate `k0`).  Pairing each `k0` of the IL at Color entry with the literal at the same operand position of the
same instruction gives every reference's value, width, block and instruction.  --export writes the `regs --export`
snapshot plus that dump (key `gil`); --from reads one back (a plain `regs --export` snapshot works too, without the
per-use widths).  --value V: only the constants whose text is V (`1`, `-1`, `&?g_game@@3PAUGameData@@A`).

Per constant (immediate globreg of pass 1):
  refs     its references at Color entry by width (dword / word / byte) and the load (OPENTERLOAD)
  byte     GLOBREG fl5 & 0x80: one of its operands is 1 byte (InitialSplitPoints 10b0e2fb: type size 1 -> the web needs
           a byte register).  Across a call only ebx has a byte form among the callee-saved registers, so the constant's
           avail is {ebx}, and every web coloured while it is queued and interferes sees ebx +100*benefit
           (ChoosePhysReg, regalloc.md 4.2)
  benefit  sum of cost*w over its uses minus the load (1*w): a use costs 1 when the instruction's other operand is
           memory with a symbol or displacement, else 0 (CmdSubCost).  <= 0: never coloured; coloured but < 3 in a
           callee-saved register: CmdPostspill gives the register back after Color (opt.md 7 item 5)
  prio     the Spillcosts terms (same formula as any web: cost*P*w per referencing block, -P*w per live-through block)
  outcome  the pop that coloured it and the webs that lost that register at that moment (its neighbours then), or the
           pop of another web that took its last register (it is then taken off the list: all uses stay immediates)
  levers   from the rule: dropping a byte use, the uses whose removal ends the candidacy (once-only), how far its prio
           must fall to pop after the web it displaced, and which blocks carry that prio
"""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2build  # noqa: F401  (takes --c2 BUILD out of sys.argv, port-1310.md)

REGORDER = ["eax", "ecx", "edx", "esi", "edi", "ebx", "ebp"]
TOK = re.compile(r"\S+")


def _ops(line):
    """IL line -> (tuple id, opcode, [operand tokens: dsts then srcs])."""
    head, _, rest = line.partition(" | ")
    f = head.split()
    if len(f) < 3 or f[0] != "I":
        return None
    rest = rest.split(" ; ")[0]
    d, _, s = rest.partition("<-")
    return f[1], f[2], TOK.findall(d) + ["<-"] + TOK.findall(s)


def _literal(tok):
    """A literal operand token of the IL at Globregs entry -> (value text, type word) or None."""
    m = re.match(r"^(-?\d+):([0-9a-f]+)$", tok)
    if m:
        return int(m.group(1)), int(m.group(2), 16)
    m = re.match(r"^([?_$@A-Za-z][^\[\]:]*):([0-9a-f]+)$", tok)
    if m:
        return m.group(1), int(m.group(2), 16)
    return None


def _ktok(tok):
    m = re.match(r"^k\d+:([0-9a-f]+)$", tok)
    return int(m.group(1), 16) if m else None


def _width(tw):
    n = tw & 0xfff
    return {1: "byte", 2: "word", 4: "dword", 8: "qword"}.get(n, "%d bytes" % n)


def _memdisp(toks):
    """Does the instruction have a memory operand with a symbol or a displacement (CmdSubCost: the use then costs 1)?
    `[base:tw+index:tw*s+disp]:tw`: a nonzero disp, or a global symbol (`?name`) as a term."""
    for t in toks:
        m = re.match(r"^\[(.*)\]:[0-9a-f]+$", t)
        if not m:
            continue
        terms = m.group(1).split("+")
        if re.match(r"^-?\d+$", terms[-1]) and int(terms[-1]) != 0:
            return True
        if any(x.startswith("?") for x in terms):
            return True
    return False


def gil_index(lines):
    """{tuple id: operand tokens} of the IL at Globregs entry."""
    out = {}
    for ln in lines:
        o = _ops(ln)
        if o:
            out[o[0]] = (o[1], o[2])
    return out


def uses(snap):
    """[(block, bb, line text, opcode, value, type word, is_load, memdisp)] for every k-operand of the pass-1 IL."""
    il = snap.get("il") or {}
    gi = gil_index(snap.get("gil") or [])
    bnum = {b["bb"]: b["b"] for b in snap["passes"][0]["blocks"]} if snap.get("passes") else {}
    out = []
    for bb in il.get("order", []):
        for ln in il["blocks"].get(bb, []):
            o = _ops(ln)
            if not o or not any(_ktok(t) is not None for t in o[2]):
                continue
            tid, op, toks = o
            b = bnum.get(bb)
            if op == "OPENTERLOAD":
                src = toks[toks.index("<-") + 1:]
                lit = _literal(src[0]) if src else None
                out.append((b, bb, ln, op, lit[0] if lit else None, lit[1] if lit else None, True, False))
                continue
            g = gi.get(tid)
            ks = [(i, _ktok(t)) for i, t in enumerate(toks) if _ktok(t) is not None]
            pairs = []
            if g and len(g[1]) == len(toks):
                pairs = [(_literal(g[1][i]), tw) for i, tw in ks]
            elif g:                                   # operand lists differ: pair the literals in order
                lits = [_literal(t) for t in g[1] if _literal(t)]
                pairs = list(zip(lits, [tw for _, tw in ks])) if len(lits) == len(ks) else [(None, tw) for _, tw in ks]
            else:
                pairs = [(None, tw) for _, tw in ks]
            for lit, tw in pairs:
                out.append((b, bb, ln, op, lit[0] if lit else None, tw, False, _memdisp(toks)))
    return out


def _match(im, val):
    if val is None or im is None:
        return False
    if isinstance(val, int):
        return im.get("kind") == "const" and (im.get("value", 0) & 0xffffffff) == (val & 0xffffffff)
    return str(val) in im.get("text", "")


def analyse(snap, want=None):
    rn = {int(k): v for k, v in (snap.get("regnames") or {}).items()}
    p1 = snap["passes"][0] if snap.get("passes") else {"grs": {}, "blocks": []}
    pops = snap.get("pops", [])
    ins = {}
    for e in snap.get("inserts", []):
        if "gr" in e:
            ins.setdefault(e["gr"], e)
    fl5 = {}
    for d in snap.get("decisions", []):
        if d.get("t") == "so" and "fl5" in d:
            fl5.setdefault(d["gr"], d["fl5"])
    for g, e in ins.items():
        if "fl5" in e.get("x", {}):
            fl5.setdefault(g, e["x"]["fl5"])
    U = uses(snap)
    # pass-1 order: prio descending, defOrd descending (InsertInList); immediates have defOrd 0
    rows = []
    for gr, g in p1["grs"].items():
        pr = sum(t["delta"] for t in g["terms"])
        de = (ins.get(gr) or {}).get("x", {}).get("defOrd", 0) or 0
        rows.append((pr, de, gr, g))
    rows.sort(key=lambda r: (-r[0], -r[1]))
    rank = {r[2]: k for k, r in enumerate(rows)}
    disp = {r[2]: r[3]["name"] + ("=" + r[3]["imm"]["text"] if r[3].get("imm") else "") for r in rows}
    prio1 = {r[2]: r[0] for r in rows}
    out = []
    for pr, de, gr, g in rows:
        im = g.get("imm")
        if not im:
            continue
        if want is not None and im.get("text") != want:
            continue
        mine = [u for u in U if _match(im, u[4])]
        e = ins.get(gr) or {}
        f5 = fl5.get(gr, 0)
        my_pops = [p for p in pops if p["gr"] == gr and p.get("npass") == 1]
        ben = e.get("ben", my_pops[0]["ben"] if my_pops else None)
        avail = [rn.get(r, str(r)) for r in e.get("avail", [])]
        c = dict(gr=gr, text=im.get("text"), kind=im.get("kind"), prio=pr, benefit=ben, avail=avail,
                 byte=bool(f5 & 0x80), fl5=f5, terms=g["terms"], uses=mine, rank=rank[gr], nlist=len(rows),
                 blocks={b["b"]: (b["P"], b["w"]) for b in p1["blocks"]})
        # the record can be reused for another web after it is retired: only its pops before the first 'remove'
        gone = min([d["after_pop"] for d in snap.get("decisions", []) if d.get("gr") == gr and d.get("t") == "remove"]
                   or [10 ** 9])
        mp = [p for p in pops if p["gr"] == gr and p["pop"] <= gone]
        c["pops"] = [(p["pop"], p.get("reg"), p.get("prio")) for p in mp]
        col = next((p for p in mp if p.get("reg") and p["reg"] != "spill/defer" and not p["reg"].startswith("uncol")),
                   None)
        c["coloured"] = (col["pop"], col["reg"]) if col else None
        if col:
            lost = []
            for n in (col.get("choice") or {}).get("nbrs", []):
                av = [rn.get(r, str(r)) for r in n.get("avail", [])]
                if col["reg"] in av:
                    lost.append((disp.get(n["gr"], n["name"]), av, n.get("ben"), prio1.get(n["gr"])))
            c["lost"] = lost
        # webs coloured while it was queued that saw its last register as a +100*benefit penalty; "steered": the
        # register was in their own avail set and they took another one that comes later in the order
        pen = []
        for p in pops:
            if p["gr"] == gr or p["pop"] > (col["pop"] if col else gone):
                continue
            for n in (p.get("choice") or {}).get("nbrs", []):
                if n["gr"] == gr and len(n.get("avail", [])) == 1 and (n.get("ben") or 0) > 0:
                    r = rn.get(n["avail"][0])
                    av = [rn.get(x) for x in (p.get("choice") or {}).get("avail", [])]
                    st = r in av and p.get("reg") in REGORDER and r in REGORDER and \
                        REGORDER.index(p["reg"]) > REGORDER.index(r)
                    pen.append((p["pop"], p["name"], p.get("reg"), r, 100 * n["ben"], st))
        c["penalised"] = pen
        # taken off the list: a 'mark nbr' in pass 1 after pop N (UpdateAffectedGlobregs: N took its last register)
        rem = None
        for d in snap.get("decisions", []):
            if d.get("gr") == gr and d.get("t") == "mark" and d.get("site") == "nbr" and d["after_pop"] <= gone:
                rem = d["after_pop"]
                break
        if rem and not col:
            p = next((q for q in pops if q["pop"] == rem), None)
            c["removed_by"] = (rem, p["name"] if p else "?", p.get("reg") if p else "?")
        # below it in the pass-1 list: the webs it pops before
        c["next"] = [(r[3]["name"] + ("=" + r[3]["imm"]["text"] if r[3].get("imm") else ""), r[0])
                     for r in rows[rank[gr] + 1:rank[gr] + 4]]
        out.append(c)
    return out


def _blocktag(b, terms):
    t = next((t for t in terms if t["b"] == b), None)
    return t


def levers(c):
    L = []
    nuse = [u for u in c["uses"] if not u[6]]
    nloads = len([u for u in c["uses"] if u[6]])
    byteuses = [u for u in nuse if u[5] is not None and (u[5] & 0xfff) == 1]
    wide = [u for u in nuse if u[5] is not None and (u[5] & 0xfff) != 1]
    if c["byte"] and byteuses:
        bl = sorted({u[0] for u in byteuses})
        others = sorted({u[0] for u in wide})
        est, exact = 0, True
        for b in bl:
            t = _blocktag(b, c["terms"])
            if not t or t["cost"] is None or b in others:
                continue
            P, w = c["blocks"].get(b, (None, None))
            loop = w and w > 1 and any(c["blocks"].get(o, (0, 1))[1] >= w for o in others)
            if (others and others[0] < b < others[-1]) or loop:
                est += -t["delta"] - (P - 1) * w          # still inside its region: now only live through
            else:
                est += -t["delta"]; exact = False          # the region ends earlier: the blocks between drop out too
        L.append("byte: %d byte use(s) in block %s make this web need a byte register (avail {%s}). Without them (store "
                 "another value, a non-constant, or the other signedness for 128..255 / negative values, immcand.md R2) "
                 "it keeps %d use(s) of other widths and needs no byte register; the +100*benefit penalty on %s goes "
                 "away; prio %s%+d (%s)" % (
                     len(byteuses), ",".join(map(str, bl)), ",".join(c["avail"]), len(wide),
                     ("%s for %d web(s)" % (c["penalised"][0][3], len(c["penalised"]))) if c["penalised"] else "nobody",
                     c["prio"], est, "about %d" % (c["prio"] + est) if exact else "at most %d: its region also ends "
                     "earlier" % (c["prio"] + est)))
    if c["benefit"] is not None:
        if c["benefit"] <= 0:
            L.append("benefit %d <= 0: never coloured (spilled or deferred at its pop); it still counts in P of every "
                     "block that references it" % c["benefit"])
        else:
            L.append("benefit %d > 0: coloured at its pop if a register is left (a callee-saved register is kept in the "
                     "code only at benefit >= 3, CmdPostspill). To make it <= 0, remove uses that carry a memory operand "
                     "with a displacement (each is worth w of its block) or move them out of loops" % c["benefit"])
    if len(nuse) == 2 and nloads <= 1:
        L.append("two uses: with one of them gone (outside loops) the constant is no candidate at all (immcand.md 2.3)")
    last = [x for x in c.get("lost", []) if len(x[1]) == 1]
    if last:
        reg = c["coloured"][1]
        L.append("its colouring took %s, the last register of %s. For one of them to keep %s: the constant must pop "
                 "after it (prio %d must fall below %s), or lose the candidacy (benefit <= 0, once-only, or no byte use "
                 "if only a byte register is left to it), or a call-crossing web coloured before it must take %s (then "
                 "it is taken off the list and every use stays an immediate)" % (
                     reg, ", ".join("%s (prio %s)" % (x[0], x[3]) for x in last), reg, c["prio"],
                     "/".join(str(x[3]) for x in last if x[3] is not None) or "theirs", reg))
    pos = sorted((t for t in c["terms"] if t["delta"] > 0), key=lambda t: -t["delta"])
    if pos:
        L.append("prio %d is carried by block(s) %s: a use removed there takes that term away and adds -P*w if the block "
                 "stays inside its region" % (c["prio"], ", ".join("%d (%+d)" % (t["b"], t["delta"]) for t in pos[:4])))
    return L


def show(fname, cs, P1):
    print("%s: %d constant candidate(s)" % (fname, len(cs)))
    bw = {b["b"]: b for b in P1["blocks"]}
    for c in cs:
        nuse = [u for u in c["uses"] if not u[6]]
        wd = {}
        for u in nuse:
            k = _width(u[5]) if u[5] is not None else "?"
            wd[k] = wd.get(k, 0) + 1
        print("\n  const %s (%s)  prio %d (list position %d of %d)  benefit %s  refs %s%s  avail at queue {%s}%s" % (
            c["text"], c["kind"], c["prio"], c["rank"] + 1, c["nlist"], c["benefit"],
            ", ".join("%s %d" % kv for kv in sorted(wd.items())) or "?",
            " + %d load(s)" % len([u for u in c["uses"] if u[6]]), ",".join(c["avail"]),
            "  BYTE (fl5 & 0x80)" if c["byte"] else ""))
        if c.get("coloured"):
            print("    outcome: pop %d -> %s" % c["coloured"])
            for name, av, ben, pr in c.get("lost", []):
                print("      took it from %s (pass-1 prio %s, avail then {%s}, benefit %s)%s" % (
                    name, pr, ",".join(av), ben,
                    ": its last register, so it is split or left without one" if len(av) == 1 else ""))
        elif c.get("removed_by"):
            print("    outcome: taken off the list when pop %d gave %s %s (its last register): every use stays an "
                  "immediate" % c["removed_by"])
        else:
            print("    outcome: %s" % (", ".join("pop %d %s" % (p, r) for p, r, _ in c["pops"]) or "never popped"))
        if c["penalised"]:
            st = [p for p in c["penalised"] if p[5]]
            print("    %d web(s) coloured while it was queued saw %s +%d (its only register, 100*benefit)%s" % (
                len(c["penalised"]), c["penalised"][0][3], c["penalised"][0][4],
                ("; steered off it: " + ", ".join("pop %d %s -> %s" % p[:3] for p in st)) if st else
                "; none was steered off it (it was not their best register anyway)"))
        print("    pops before: %s" % ", ".join("%s %d" % n for n in c["next"]))
        print("    uses (block w P | instruction at Color entry):")
        for u in c["uses"]:
            b = bw.get(u[0], {})
            ins = re.sub(r"^I \S+ ", "", u[2])
            print("      %4s w=%s P=%-3s %-6s %-5s %s" % (u[0], b.get("w", "?"), b.get("P", "?"),
                                                    "load" if u[6] else (_width(u[5]) if u[5] is not None else "?"),
                                                    "" if u[6] else ("mem" if u[7] else "reg"), ins[:110]))
        print("    prio terms: " + " ".join("%d:%+d" % (t["b"], t["delta"]) for t in c["terms"]))
        for l in levers(c):
            print("    lever: " + l)


def main(argv):
    frm = None
    if "--from" in argv:
        k = argv.index("--from"); frm = argv[k + 1]; del argv[k:k + 2]
    want = None
    if "--value" in argv:
        k = argv.index("--value"); want = argv[k + 1]; del argv[k:k + 2]
    if frm:
        snap = json.load(open(frm))
        show(snap.get("func", "?"), analyse(snap, want), snap["passes"][0])
        return 0
    import argparse
    import c2trace, c2regs, c2il
    cl = []
    if "--" in argv:
        k = argv.index("--"); argv, cl = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(prog="constregs.py", usage=__doc__)
    ap.add_argument("src"); ap.add_argument("--func"); ap.add_argument("--keep"); ap.add_argument("--export")
    a = ap.parse_args(argv)
    gname = c2trace.c2build.hooks("il_default", ["@Globregs@4"])
    gname = [n for n in gname if "Globregs" in n][0] if isinstance(gname, list) else "@Globregs@4"
    hooks = c2regs.HOOKS + [gname + c2il.SPEC]
    GI = len(c2regs.HOOKS)
    img, hk, rc, out, log = c2trace.run_trace(a.src, hooks, cl, keep=a.keep)
    if rc:
        sys.stdout.write(out)
    keep, gil, cur = [], {}, None
    for ln in log.splitlines():
        if ln.startswith("E ") and "|" in ln and int(ln.split("|")[0].split()[3]) == GI:
            rest = ln.partition("|")[2]
            name = rest.split('"')[1] if '"' in rest else "?"
            cur = gil.setdefault(name, []) if name not in gil else None
            continue
        if ln.startswith("T ") and cur is not None:
            cur.append(ln[2:])
            continue
        if not ln.startswith("T "):
            cur = None
        keep.append(ln)
    funcs = c2regs.parse("\n".join(keep), a.func)
    if not funcs:
        sys.exit("no function matching %r was register-allocated" % a.func)
    for fn in funcs:
        snap = c2regs.export(fn)
        snap["gil"] = [l for l in gil.get(fn["name"], []) if l.startswith("I ")]
        if a.export:
            path = a.export % re.sub(r"[^\w@$]+", "_", fn["name"])[:120] if "%s" in a.export else a.export
            json.dump(snap, open(path, "w"), indent=0)
            print("# exported %s" % path)
        if not snap.get("passes"):
            continue
        show(fn["name"], analyse(snap, want), snap["passes"][0])
        print()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
