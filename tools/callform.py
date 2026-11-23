#!/usr/bin/env python3
"""Call census by dispatch form, original against draft: is a byte gap a missing block?
Doc: docs/c2/callform.md  (the triage `docs/c2/icount.py` cannot do without a compile)

usage: callform.py FIRST.s SECOND.s [--targets] [--top K]
       callform.py ONE.s                        (census only, no diff)

Both listings are one instruction per line, `VA mnemonic operands` (what orig.s / my_va.s are;
f4check's `+0xN` form is accepted too). Every call is grouped by its dispatch form, the register
names and the callee spelling normalised away, because those are the two things the allocator and
the symbol resolution change while the call's *kind* stays:

  call [reg] (slot N)     memory-indirect through a register: the vtable slot (displacement / 8)
  call [reg+d] (other)    displacement that is not a vtable slot (base+index, not 8-aligned)
  call [rip] (thunk)      rip-relative indirect: IAT thunks (__imp_) and function-pointer globals
  direct                  a direct call (named ?sym / X@@... or an unresolved FUN_<addr>)
  call reg                register-indirect
  other                   anything else (far call, segment forms)

The census is the fast discriminator the instruction-alignment view hides (`docs/c2/ncolor.md` 13
measured the same case): a missing whole basic block almost always changes one form's multiset --
an element-teardown tail of virtual destructors is two `call [reg]` slot-0 calls that vanish --
while pure register-allocation or scheduling divergence leaves every form equal. A devirtualised
call shows the same way, in the other direction: a direct `??1Class@@UEAA@XZ` where the other
side dispatches through the vtable.

Verdict: any nonzero form delta is a structural lead (missing/extra block, devirtualisation,
wrong vtable slot); a census equal on every form means the byte gap is *not* a missing block --
take it to the allocator (`tools/ncolspill.py`) and the alignment (`icount.py`).

--targets diffs the callees of every form as well (the direct/rip rows' detail). It cannot match
unresolved placeholders across listings: the original's image often shows `FUN_<addr>` where the
draft listing has the mangled name, and the draft may show `[rip]` where the original resolves the
IAT name, so identical counts with different spellings there are a listing artifact, not a finding.
"""
import argparse, collections, os, re, sys

LINE = re.compile(r"^\s*(?:[0-9a-f]{8,16}|\+?0x[0-9a-f]+):?\s+([a-z][a-z0-9]*)\s*(.*)$", re.I)
REG = r"(?:[re](?:ax|bx|cx|dx|si|di|bp|sp)|r(?:[0-9]|1[0-5])[dwb]?)"
MEMREG = re.compile(r"^\[\s*(%s)\s*((?:[+-]\s*0x[0-9a-f]+)?)\s*\]$" % REG, re.I)
RIP = re.compile(r"^\[\s*rip(?:\s*->\s*([^\]]*))?\s*\]$", re.I)
DTOR = re.compile(r"^\?\?1")          # ?E~T@@  MSVC mangled destructor: ??1...@@UEAA@XZ


def form_of(ops):
    """A call's operands -> (form, slot, target). Slot = vtable entry index (disp/8) or None."""
    o = ops.strip()
    if o.startswith(("qword ptr", "dword ptr", "word ptr", "byte ptr")):
        o = o[o.index("ptr") + 3:].strip()
    m = RIP.match(o)
    if m:
        t = (m.group(1) or "").strip()
        return "call [rip] (thunk)", None, t
    m = MEMREG.match(o)
    if m:
        disp = m.group(2).replace(" ", "").replace("+", "")
        d = int(disp, 16) if disp else 0
        if d % 8 == 0:
            return "call [reg] (slot %d)" % (d // 8), d // 8, m.group(1)
        return "call [reg+d] (other)", None, "+%s" % disp
    if re.match(r"^%s$" % REG, o):
        return "call reg", None, o
    if o.startswith(("?", "FUN_", "DAT_", "off_", "0x")) or re.match(r"^0x[0-9a-f]+$", o, re.I) \
            or re.match(r"^@\S+@\S+", o) or re.match(r"^\?\?\S+", o):
        return "direct", None, o
    return "other", None, o


def census(path):
    """{form: {target: n}}, plus total calls; misses no-call lines (push/jmp/lea do not call)."""
    out = collections.OrderedDict()
    total = 0
    for line in open(path):
        m = LINE.match(line.rstrip())
        if not m or m.group(1).lower() != "call":
            continue
        f, _, t = form_of(m.group(2))
        total += 1
        out.setdefault(f, collections.Counter())[t] += 1
    return out, total


def note(rows, forms, names):
    """One hint per nonzero delta row (rules only, read as leads, not answers). Delta = second - first:
    d < 0 = only the first side has them; d > 0 = the second side added them."""
    out = []
    for f, x, y, d in rows:
        if d == 0:
            continue
        if f.startswith("call [reg] (slot"):
            slot = f.rsplit(" ", 1)[1][:-1]
            hint = ("a block the second side does not emit (teardown/devirtualisation)" if d < 0
                    else "calls the second side makes that the first does not")
            if slot == "0":
                hint = ("vtable slot 0: a missing virtual-dtor element teardown (look for the destroy "
                        "loops near the function's end)" if d < 0 else
                        "extra slot-0 calls on the second side (a dtor the first side devirtualises?)")
            elif d < 0 and slot == "3":
                hint += " (slot 3 is often deleteThis/removeReference)"
        elif f == "direct":
            dt = [t for t, c in (names[f][0] if d < 0 else names[f][1]).items() if DTOR.match(t)]
            hint = "devirtualised destructor(s): %s" % ", ".join(sorted(dt)) if dt else \
                   ("a direct-calling block the second side lacks" if d < 0 else
                    "direct calls the second side adds (devirtualisation, or an extra block)")
        elif f == "call [rip] (thunk)":
            hint = "different thunk/global-call set -- see --targets (unresolved [rip] hides the name)"
        else:
            hint = "different %s set" % f
        out.append("%-38s %5d %5d %+5d   %s" % (f, x, y, d, hint))
    return out


def main(argv):
    ap = argparse.ArgumentParser(prog="callform.py", usage=__doc__)
    ap.add_argument("listings", nargs="+", help="listings: FIRST.s SECOND.s (census only with one)")
    ap.add_argument("--targets", action="store_true", help="diff the callees of every form too")
    a = ap.parse_args(argv)
    for p in a.listings:
        if not os.path.exists(p):
            sys.exit("callform: no such listing %s" % p)
    cs, ts = [], []
    for p in a.listings:
        c, total = census(p)
        forms = collections.OrderedDict((f, sum(t.values())) for f, t in c.items())
        cs.append((c, forms, total))
        ts.append(p)
        print("## %s: %d calls, %d forms" % (os.path.basename(p), total, len(forms)))
        for f in forms:
            print("  %-24s %5d" % (f, forms[f]))
        print()
    if len(cs) != 2:
        return 0
    (c1, f1, t1), (c2, f2, t2) = cs
    rows = []
    for f in sorted(set(f1) | set(f2), key=lambda f: (-abs(f1.get(f, 0) - f2.get(f, 0)),
                                                     -(f1.get(f, 0) + f2.get(f, 0)), f)):
        rows.append((f, f1.get(f, 0), f2.get(f, 0), f2.get(f, 0) - f1.get(f, 0)))
    print("## form census (%s vs %s)" % (os.path.basename(ts[0]), os.path.basename(ts[1])))
    names = dict((f, (collections.Counter(c1.get(f) or {}), collections.Counter(c2.get(f) or {})))
                 for f, _, _, _ in rows)
    for line in note(rows, cs, names):
        print("  " + line)
    if not any(r[3] for r in rows):
        print("  (every form equal)")
    bad = [r for r in rows if r[3]]
    if not bad:
        print("## verdict: the call census is identical in every dispatch form -- the byte gap is NOT a "
              "missing block; take it to the allocator (tools/ncolspill.py) and the alignment (icount.py)")
    else:
        print("## verdict: %d form(s) differ -> structural lead (missing/extra block, devirtualisation, "
              "wrong vtable slot). See the flagged rows above; then the block should be found by "
              "address (the first side's calls that have no second-side counterpart)." % len(bad))
    if a.targets:
        for f, _, _, _ in rows:
            tgt1, tgt2 = c1.get(f) or {}, c2.get(f) or {}
            if not tgt1 and not tgt2:
                continue
            tot1, tot2 = sum(tgt1.values()), sum(tgt2.values())
            spelling = f.startswith("call [reg") or f == "call reg"   # targets are registers
            if tot1 == tot2:
                if spelling and set(tgt1) != set(tgt2):
                    print("\n## targets of %s (%d vs %d): the same calls through different registers "
                          "(allocator spelling, not a finding)" % (f, tot1, tot2))
                continue
            print("\n## targets of %s (%d vs %d)" % (f, tot1, tot2))
            all_t = sorted(set(tgt1) | set(tgt2),
                           key=lambda t: (-abs(tgt1.get(t, 0) - tgt2.get(t, 0)),
                                          -(tgt1.get(t, 0) + tgt2.get(t, 0)), t))
            only1 = [s for s in tgt1 if s not in tgt2]
            only2 = [s for s in tgt2 if s not in tgt1]
            partner, used = {}, set()
            for s in sorted(only1, key=lambda s: (-tgt1[s], s)):
                for u in sorted(only2, key=lambda u: (-tgt2[u], u)):
                    if u not in used and tgt2[u] == tgt1[s]:
                        partner[s] = u
                        partner[u] = s
                        used.add(u)
                        break
            for t in all_t:
                x, y = tgt1.get(t, 0), tgt2.get(t, 0)
                if x == y:
                    continue
                if spelling:
                    pair = "   (registers are allocator spelling; the delta is the call count)"
                elif t in partner:
                    pair = ("   (pairs with %s by count: symbol-resolution artifact, not a finding)"
                            % partner[t])
                else:
                    pair = ""
                print("  %-52s %5d %5d %+5d%s" % (t or "(unresolved)", x, y, y - x, pair))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
