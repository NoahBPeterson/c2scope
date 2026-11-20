# Probe checks for docs/c2/frame.md and docs/c2/immcand.md (the [I] rules re-tested as [M] or [C]).
# Sources: tests/data/rules/RF_*.cpp. Listings are untraced; the immcand checks use `regs --export` and one hooked
# compile. Exec'd by tests/run_tests.py (check, rule_compile, rule_proc, RKEY, tool, HERE, RULES in scope).
import json as _json, os as _os, re as _re, subprocess as _sp, sys as _sys, tempfile as _tf

print("frame.md / immcand.md probes (tests/data/rules/RF_*)")


def rf_syms(asm, prefix):
    """{frame symbol: offset} declared for the function whose decorated name starts with `prefix`."""
    m = _re.search(r"^%s\S*\s+PROC\b" % _re.escape(prefix), asm, _re.M)
    if not m:
        return {}
    pre = asm[:m.start()].rsplit("SEGMENT", 1)[-1]
    return {n: int(o) for n, o in _re.findall(r"^_(\S+?)\$(?:\d+)? = (-?\d+)", pre, _re.M)}


def rf_frame(proc):
    """Bytes of locals: `sub esp, N`, or 4 per `push ecx` before the first other instruction that is not a push."""
    m = _re.search(r"sub\s+esp, (\d+)", proc)
    if m:
        return int(m.group(1))
    return 4 * len(_re.findall(r"push\s+ecx", proc.split("call")[0].split("lea")[0]))


def rf_norm(proc):
    """Instruction text with symbol numbers ($1234) and listing columns removed."""
    out = []
    for ln in proc.split("\n"):
        m = _re.search(r"\t ([A-Za-z].*)$", ln)
        if ln.startswith(";") or not m:
            continue
        out.append(_re.sub(r"\s+", " ", _re.sub(r"\$\d+|;.*", "", m.group(1))).strip())
    return out


# frame.md 4.4: scope-entry kills only for address-taken block locals > 16 bytes, and only kept in loops.
# 13.10: two block locals in a loop never share, at any size.
asm = rule_compile("RF_scopekill.cpp")
got = tuple(rf_frame(rule_proc(asm, "?%s@@" % f)) for f in ("loop16", "loop20", "loop64", "flat16", "flat20"))
want = {"14.00": (32, 20, 64, 16, 20), "15.00": (32, 20, 64, 16, 20),
        "13.10.3077": (32, 40, 128, 16, 20), "13.10.3052": (32, 40, 128, 16, 20)}
check("RF scopekill: block locals share: loop16/20/64, flat16/20", got, want[RKEY])

# frame.md 1: /Od automatics come in _LocalSyms order, which follows the names (not declaration or first use).
got = tuple(tuple(sorted(rf_syms(rule_compile("RF_odorder.cpp", "/Od", *f), "?od@@").items(), key=lambda t: -t[1]))
            for f in ([], ["/DREV"]))
o1 = (("n", 8), ("w", -4), ("x", -8), ("y", -12), ("z", -16), ("d", -20), ("e", -24))
o2 = (("n", 8), ("w", -4), ("y", -8), ("x", -12), ("z", -16), ("d", -20), ("e", -24))
check("RF odorder: /Od slots, declared y w z x / x z w y", got, {k: (o1, o2) for k in
      ("14.00", "15.00", "13.10.3077", "13.10.3052")}[RKEY])

# frame.md 6: permuting hoisted declarations changes nothing (registers, spills and slots).
lst = [rule_compile("RF_declorder.cpp", "/DORDER=%d" % o) for o in (1, 2, 3)]
got = tuple(all(rf_norm(rule_proc(a, f)) == rf_norm(rule_proc(lst[0], f)) and rf_norm(rule_proc(a, f)) for a in lst)
            for f in ("?press@@", "?slots@@"))
check("RF declorder: three declaration orders, same code", got, (True, True))

# frame.md 2.1-3.4 (/GS): strict_gs_check, by-value struct parameters with a buffer, struct parameter copies.
# 13.10: the pragma is unknown, a struct parameter is never a GS buffer, no $GSCopy$ at all (no GSCopy in c2.dll).
asm = rule_compile("RF_gsbuf.cpp", "/GS")
fs = ("strict_int", "strict_scalar", "strict_none", "sp_buf", "sp_bufread", "cp_ptr", "cp_int", "cp_ptrread",
      "cp_intplain", "cp_ptrnouse", "cp_ptrpass")
got = tuple(("C" if "_$ArrayPad" in rf_syms(asm, "?%s@@" % f) else "-") + ("S" if "s$GSCopy" in rf_syms(asm, "?%s@@" % f)
            else "-") for f in fs)
w14 = ("C-", "C-", "--", "CS", "CS", "CS", "CS", "CS", "C-", "C-", "CS")
w13 = ("--", "--", "--", "--", "--", "C-", "C-", "C-", "C-", "C-", "C-")
check("RF gsbuf: cookie / struct copy per function", got,
      {"14.00": w14, "15.00": w14, "13.10.3077": w13, "13.10.3052": w13}[RKEY])
got = tuple("_$ArrayPad" in rf_syms(asm, "?%s@@" % f) for f in ("q_c4", "q_c5", "q_s3", "q_i2", "q_sc", "q_si"))
check("RF gsbuf: GS buffer: char[4] char[5] short[3] int[2] {char[16]} {int,int[8]}", got,
      (False, True, True, False, True, False))

# frame.md 0 rule 9: a GS function never reuses a parameter home on 14.00/15.00; 13.10 does.
asm = rule_compile("RF_parhome.cpp", "/GS")
got = (rf_syms(asm, "?nobuf@@").get("x"), rf_syms(asm, "?buf@@").get("x"))
check("RF parhome: x offset without / with a GS buffer", got,
      {"14.00": (8, -16), "15.00": (8, -16), "13.10.3077": (8, 8), "13.10.3052": (8, 8)}[RKEY])

# frame.md 4.9 (/O1 /GS): EBP adjustment puts the lowest slot at ebp-0x80 in the GS function; none elsewhere.
asm = rule_compile("RF_ebpadj.cpp", "/O1", "/GS")
lea = lambda f: (_re.findall(r"lea\s+ebp, DWORD PTR \[esp-(\d+)\]", rule_proc(asm, "?%s@@" % f)) or [None])[0]
got = tuple(lea(f) for f in ("big", "gs", "small")) + (rf_frame(rule_proc(asm, "?gs@@")),)
check("RF ebpadj: lea ebp in big / gs / small, gs frame", got,
      {"14.00": (None, "288", None, 416), "15.00": (None, "288", None, 416),
       "13.10.3077": (None, "284", None, 412), "13.10.3052": (None, "284", None, 412)}[RKEY])

# frame.md 4.6 / 6: an align(16) local raises the cap (a double then gets 8); two 8-byte locals in a loop: and esp,-8.
asm = rule_compile("RF_align.cpp")
andesp = lambda f: (_re.findall(r"and\s+esp, (-\d+)", rule_proc(asm, "?%s@@" % f)) or [None])[0]
got = (rf_syms(asm, "?a16@@").get("d"), rf_syms(asm, "?plain12@@").get("d")) + \
      tuple(andesp(f) for f in ("a16", "d8loop", "d8loop2", "s8loop2", "s8flat2"))
check("RF align: double offset with/without align(16); and esp", got, (-40, -20, "-16", None, "-8", "-8", None))


# immcand.md 2.3-2.5: immediate candidates from `regs --export` (one snapshot per function).
def rf_imms(flags):
    d = _tf.mkdtemp()
    tool("regs", _os.path.join(RULES, "RF_immcand.cpp"), "--export", _os.path.join(d, "%s.json"), "--", "/TP", *flags)
    res = {}
    for f in _os.listdir(d):
        s = _json.load(open(_os.path.join(d, f)))
        fn = _re.sub(r"@.*", "", s["func"].lstrip("?"))
        p = ([x for x in s["passes"] if x["cls"] == 0] or [{"grs": {}}])[0]
        res[fn] = {g["imm"]["text"]: (tuple(t["b"] for t in g["terms"] if t["cost"] is not None),
                                      tuple(t["b"] for t in g["terms"] if t["cost"] is None))
                   for g in p["grs"].values() if "imm" in g}
    return res


im2, im1 = rf_imms(["/O2"]), rf_imms(["/O1"])
loops = ("l_for", "l_while", "l_do", "l_goto", "l_nested", "l_ifin", "l_after", "l_none")
got = tuple("1000" in im2.get(f, {}) for f in loops) + tuple("1000" in im1.get(f, {}) for f in loops[:4])
check("RF immcand: once-only 1000 a candidate: loop forms, after, none; /O1", got,
      (True,) * 6 + (False, False) + (False,) * 4)
got = tuple(sorted(im2.get("k64d", {}))) + (im2.get("dnest", {}).get("1000"),)
check("RF immcand: 64-bit halves are 32-bit values; nested-if load block", got, ("7", ((1, 3, 4), (2,))))

# immcand.md 2.5: b9's 1000 is removed by Enterloads' UndoRegCandidate -> GrgChangeRegToImmed (both uses), before
# CreateGlobregs; b8's is not (15.00 and 13.10 put b8's back later, after CreateGlobregs).
hk = {"14.00": ("@UndoRegCandidate@12", "@GrgChangeRegToImmed@12", "@CreateGlobregs@4"),
      "15.00": ("510a7d10", "510a7ef0", "510a9120"),
      "13.10.3077": ("@UndoRegCandidate@12", "@GrgChangeRegToImmed@12", "@CreateGlobregs@4"),
      "13.10.3052": ("@UndoRegCandidate@12", "@GrgChangeRegToImmed@12", "@CreateGlobregs@4")}[RKEY]


def rf_undo(func):
    r = _sp.run([_sys.executable, _os.path.join(HERE, "tools", "c2trace.py"), "-H", hk[0], "-H", hk[1] + "=eax>4",
                 "-H", hk[2], _os.path.join(RULES, "RF_immcand.cpp"), "--", "/TP"], capture_output=True,
                text=True, cwd=HERE, env=dict(_os.environ, C2TRACE_FUNC="?%s@@" % func))
    ev = _re.findall(r"^\s+\d+ > (.*)$", r.stdout, _re.M)
    n, undo = 0, False
    for e in ev:                       # an immediate seen twice (kind 0xd, fl 0x10: eax>4 = 100d) put back
        if "Create" in e:
            return n
        if "Undo" in e:
            undo = True
        elif undo and "eax>4=100d" in e:
            n += 1
    return None


check("RF immcand: constants put back after an undo, before CreateGlobregs: b8 / b9", (rf_undo("b8"), rf_undo("b9")),
      (0, 2))
