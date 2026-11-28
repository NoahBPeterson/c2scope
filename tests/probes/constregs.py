# Constants as register candidates: the byte flag, ebx, and what takes the register first (docs/c2/immcand.md
# section 6, tools/constregs.py). tests/data/rules/RA_constbyte.cpp, one traced compile on the current C2_BUILD;
# the same outcome on 13.10.3077, 13.10.3052, 14.00 and 15.00 (14.00 / 15.00 count the loop increment's 1 as a
# fourth dword use).
import re as _re, subprocess as _sp, sys as _sys, tempfile as _tf


def _constregs_run(src):
    """({function: {'byte', 'avail', 'outcome', 'steered'}} for the constant 1, the listing text)."""
    d = _tf.mkdtemp()
    r = _sp.run([_sys.executable, os.path.join(HERE, "tools", "constregs.py"), src, "--value", "1", "--keep", d,
                 "--", "/TP"], capture_output=True, text=True, cwd=HERE)
    out, cur = {}, None
    for ln in r.stdout.splitlines():
        m = _re.match(r"^\?(\w+)@@\S+: \d+ constant", ln)
        if m:
            cur = out.setdefault(m.group(1), {})
            continue
        if cur is None:
            continue
        m = _re.match(r"^  const 1 \(const\).*avail at queue \{([^}]*)\}(.*)$", ln)
        if m:
            cur["avail"] = m.group(1)
            cur["byte"] = "BYTE" in m.group(2)
        m = _re.match(r"^    outcome: (pop \d+ -> (\w+)|taken off the list when pop \d+ gave \S+ (\w+))", ln)
        if m:
            cur["outcome"] = m.group(2) if m.group(2) else "off:" + m.group(3)
        if "steered off it: pop" in ln:
            cur["steered"] = True
    asm = os.path.join(d, os.path.splitext(os.path.basename(src))[0] + ".asm")
    return out, (open(asm, errors="replace").read() if os.path.exists(asm) else "")


def constregs_tests():
    if lacking("rules"):
        skip("constregs: constants as register candidates", lacking("rules"))
        return
    C, asm = _constregs_run(os.path.join(RULES, "RA_constbyte.cpp"))
    g = lambda f, k: C.get(f, {}).get(k)
    proc = lambda f: rule_proc(asm, "?%s@@" % f)
    check("constregs: a byte store of the constant flags the web (fl5 & 0x80): across calls ebx is all it may have; "
          "coloured ebx, the store is `mov [m], bl`",
          (g("q_byte", "byte"), g("q_byte", "avail"), g("q_byte", "outcome"),
           bool(_re.search(r"mov\s+ebx, 1\b", proc("q_byte"))), bool(_re.search(r", bl\b", proc("q_byte")))),
          (True, "ebx", "ebx", True, True))
    check("constregs: ... the same store as a dword: no byte flag, avail {ebx,ebp,esi,edi}, still ebx (the first free)",
          (g("q_dword", "byte"), g("q_dword", "avail"), g("q_dword", "outcome")), (False, "ebx,ebp,esi,edi", "ebx"))
    check("constregs: ... the byte store of another value (2): 1 keeps two dword compares, benefit 2, a lower prio: "
          "taken off the list, no register holds 1",
          (g("q_two", "byte"), (g("q_two", "outcome") or "")[:4], bool(_re.search(r"mov\s+ebx, 1\b", proc("q_two")))),
          (False, "off:", False))
    check("constregs: ... a call-crossing address (a reference into the vector) coloured first takes ebx: the byte "
          "constant loses its only register and stays an immediate; the ebx penalty steered one web to ebp",
          (g("q_ref", "byte"), (g("q_ref", "outcome") or "")[:4], g("q_ref", "steered"),
           bool(_re.search(r"mov\s+ebx, 1\b", proc("q_ref")))), (True, "off:", True, False))
    check("constregs: ... the same loop without the reference: the constant is coloured ebx as in q_byte",
          (g("q_noref", "byte"), g("q_noref", "outcome")), (True, "ebx"))


constregs_tests()
