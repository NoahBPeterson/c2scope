# docs/c2/loopopt.md: LoptStrengthReduce runs in rounds (reported by tricko3D, spore-decomp PR #674).  Source:
# tests/data/rules/RL_loptsr.cpp.  A round = one LoptFindInductionVariables call after the first (the first is
# LoptTerm's own), i.e. one candidate expression turned into a new induction variable; 13.10.3077 / 3052 / 14.00 / 15.00.
import re as _re

print("loopopt.md probes (tests/data/rules/RL_loptsr.cpp)")

_LP = "RL_loptsr.cpp"
_FIV = {"15.00": "?LoptFindInductionVariables@@YIHPAUtagLOOP@@@Z"}.get(RKEY, "@LoptFindInductionVariables@4")
_LP_FUNCS = ("idx", "ik", "ivk", "ivv", "next", "two", "jac", "jact", "itaken", "mulc")
# 15.00 unrolls the loop by two before this pass, so its counts are one higher where a pointer is derived
_LP_W13 = (2, 3, 1, 1, 0, 2, 2, 1, 0, 2)
_LP_WANT = {"13.10.3077": _LP_W13, "13.10.3052": _LP_W13, "14.00": _LP_W13, "15.00": (3, 4, 1, 1, 0, 3, 2, 1, 0, 3)}


def _lp_ins(asm, f):
    """The instructions of lp_<f> as 'mnem operands' (inc/dec spelled add/sub 1, as 14.00 prints them)."""
    out = []
    for l in rule_proc(asm, "?lp_%s@@" % f).replace("\r", "").splitlines():
        m = _re.match(r"^(?:\s+[0-9a-f]{5})?\t[0-9a-f ]+\t+ ([a-z][a-z0-9]*)(?:\t+ ?(.*?))?\s*$", l)
        if m:
            i = (m.group(1) + " " + _re.sub(r"\s+", " ", _re.sub(r"\s*;.*$", "", m.group(2) or ""))).strip()
            i = _re.sub(r"^inc (\w+)$", r"add \1, 1", _re.sub(r"^dec (\w+)$", r"sub \1, 1", i))
            out.append(i)
    return out


if RKEY not in _LP_WANT:
    skip("loopopt: strength-reduction rounds", "no table for %s" % RKEY)
else:
    got = tuple(len(rule_trace(_LP, "?lp_%s@@" % f, _FIV)) - 1 for f in _LP_FUNCS)
    check("loopopt: rounds of LoptStrengthReduce per loop: a[i] (i*4, then +a), a[i+k], i+k, i*k, offs=next (live twice), "
          "a[i]*b[i], jac[i] across a call, the same with jac address-taken, a[i*3] with i address-taken, a[i*3]", got, _LP_WANT[RKEY])

    a = rule_compile(_LP)
    idx, mulc, nxt, nxt1, two = (_lp_ins(a, f) for f in ("idx", "mulc", "next", "next1", "two"))
    cnt = lambda ins, rx: sum(1 for i in ins if _re.search(rx, i))
    # a[i]: the pointer IV the rounds build is folded back into [a+i*4] by LoptBuildAddressModes (i is the loop counter
    # and 4 is an address-mode scale); a[i*3]: scale 12 is not, so the pointer walks (add p, 12; 24 once 15.00 unrolled)
    check("loopopt: a[i] keeps the index ([a+i*4], no pointer step); a[i*3] walks a pointer (add p, 12 / 24 unrolled)",
          (cnt(idx, r"\[e..\+e..\*4\]"), cnt(idx, r"^add e.., 4$"), cnt(mulc, r"^add e.., (12|24)$")),
          (2 if RKEY == "15.00" else 1, 0, 1))
    # next = offs + 1 with next used twice: offs is no induction variable (offs = next is a copy): no rounds, lea + mov
    # at the tail; with next used once the copy folds and offs = offs + 1 is a plain pointer IV (add p, 4)
    check("loopopt: offs/next live twice stays lea + mov (no IV); next used once is add p, 4",
          (cnt(nxt, r"^lea e.., DWORD PTR \[e..\+4\]$"), cnt(nxt, r"^mov e.., e..$"),
           cnt(nxt1, r"^lea e.., DWORD PTR \[e..\+4\]$"), cnt(nxt1, r"^add e.., (4|8)$") > 0), (1, 1, 0, True))
    # a[i]*b[i]: the derived IVs are created in the order the candidates are visited; the preheader loads b first and
    # keeps a as the difference a - b ([edx+ecx])
    pos = lambda rx: next((k for k, i in enumerate(two) if _re.search(rx, i)), 99)
    check("loopopt: a[i]*b[i]: b is set up before a, a as a - b",
          (pos(r"_b\$") < pos(r"_a\$"), cnt(two, r"^sub e.., e..$") >= 1), (True, True))
    # jac address-taken and a call in the loop: jac is not a region constant, so it is reloaded each iteration
    got = []
    for f in ("jac", "jact"):
        ins = _lp_ins(a, f)
        k = next(i for i, x in enumerate(ins) if "?ext@@YAHXZ" in x)
        got.append(cnt(ins[k:], r"DWORD PTR _jac\$"))
    check("loopopt: jac reloaded from its stack slot in the loop only when its address is taken", tuple(got), (0, 1))
    # the loop counter itself address-taken: it is no induction variable (no rounds), so a[i*3] is recomputed from i
    # (lea e,[i+i*2]; [a+e*4]) and i is stored back every iteration, where a[i*3] alone walks a pointer
    it = _lp_ins(a, "itaken")
    check("loopopt: i address-taken: no pointer walk, lea [i+i*2] and a store of i in the loop",
          (cnt(it, r"^add e.., (12|24)$"), cnt(it, r"^lea e.., DWORD PTR \[e..\+e..\*2\]$") >= 1,
           cnt(it, r"^mov DWORD PTR _i\$\[esp\+\d+\], e..$") >= 2), (0, True, True))
