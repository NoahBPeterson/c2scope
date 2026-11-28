# base + index*size: one in-place chain (`imul r, size; add r, base`, the pointer web defined three times) or one `lea` of
# two temporaries (docs/c2/immcand.md 6.6, Mount & Blade Mission::spawn 0x4832b0). tests/data/rules/RA_addrsum.cpp,
# untraced compiles; the same on 13.10.3077, 13.10.3052, 14.00 and 15.00 (14.00 / 15.00 fold the base load into the add).
import re as _re


def _addrsum_kind(proc):
    """'chain' = imul r, 1288 then add r, <base> in the same register; 'lea' = lea r, [r1+r2] of two temporaries;
    'base' = the base loaded into the pointer's register and the product added to it."""
    if _re.search(r"lea\s+e\w\w, DWORD PTR \[e\w\w\+e\w\w\]", proc):
        return "lea"
    m = _re.search(r"imul\s+(e\w\w), 1288", proc)
    if not m:
        return "?"
    m2 = _re.search(r"\badd\s+(e\w\w), ", proc[m.end():])       # the first add after the imul
    return "chain" if m2 and m2.group(1) == m.group(1) else "base"


def addrsum_tests():
    if lacking("rules"):
        skip("addrsum: base + index*size", lacking("rules"))
        return
    asm = rule_compile("RA_addrsum.cpp")
    kind = lambda f: _addrsum_kind(rule_proc(asm, "?%s@@" % f))
    check("addrsum: a sum evaluated once is a chain, however it is spelled (p + i, &p[i], (char*)p + i * size, a "
          "local base or index, unsigned, a plain global base, a parameter index)",
          [kind(f) for f in ("as_plain", "as_addr", "as_char", "as_uns", "as_glob", "as_arg", "as_reuse")],
          ["chain"] * 7)
    check("addrsum: the same sum evaluated twice before a call is one common subexpression with two users: one lea "
          "of the product and the separately loaded base (a second pointer variable, or the second evaluation "
          "inside an expression)",
          [kind(f) for f in ("as_two", "as_cond", "as_two_ptr")], ["lea"] * 3)
    check("addrsum: the second evaluation after a call is not shared (the loads are killed): a chain",
          kind("as_late"), "chain")
    check("addrsum: a named product used again is a third form: the base loaded into the pointer's register, "
          "the product added to it", kind("as_prod"), "base")
    check("addrsum: ... a store between the two evaluations kills the base load but not the product: the product is the shared one, the third form", kind("as_store"), "base")


addrsum_tests()
