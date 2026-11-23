# tools/lift.py (machine code lifted to c2's IL at Lower's entry; docs/c2/lift.md)
import sys as _sys
_sys.path.insert(0, os.path.join(HERE, "tools"))
import lift as _lift

check("lift: instruction tokens (read-modify-write, add of a negative constant, a frame slot, a global, a zero)",
      [_lift.norm((_lift.canon_asm(t) or (None,))[0]) for t in (
          "or dword ptr [eax + 0x184], 1", "add eax, -1", "mov eax, dword ptr [esp + 8]",
          "mov dword ptr [0x4a1000], ecx", "xor eax, eax", "mov ecx, dword ptr [esi + 4]", "test eax, eax",
          "imul eax, ecx, 0x1c", "fstp dword ptr [esi + 8]", "push 0x3d4ccccd")],
      ["rmw.or", "sub:R,R", None, None, None, "ld", "cmp:R,R", "imul:R,R", "st", "push"])
check("lift: IL tokens at Lower's entry (a store of a constant, a compare, an argument, an RMW from Opeq)",
      [_lift.norm((_lift.canon_il(l) or (None,))[0]) for l in (
          "I 1cafe34 OPASSIGN 5 0 | [t130:1004+0]:1004 <- 0:1004 t130:1004",
          "I 1cae25c OPCMP 8 0 | t74:9000 <- _n:1004 0:1004",
          "I 1cae720 OPARG 10 0 | t79:1004 <- 1:1004",
          "I 1cae000 OPOR 4 0 | [_p:1004+16]:1004 <- [_p:1004+16]:1004 1:1004 _p:1004",
          "I 1caf528 OPASSIGN 3 0 | _q:1004 <- _p:1004")],
      ["st", "cmp:R,R", "push", "rmw.or", None])
_src = os.path.join(HERE, "tests", "data", "lift", "lift_rmw.cpp")
_il, _code, _ln = _lift.trace(_src, "?f_set@@", ["/O2", "/TP"])
_il2, _code2, _ln2 = _lift.trace(_src, "?f_or@@", ["/O2", "/TP"])
import io as _io, contextlib as _cl
with _cl.redirect_stdout(_io.StringIO()):
    _r = _lift.report(_il, _code, _ln, _code2, False, open(_src).read().splitlines())
check("lift: the self-check gives back all of a small function's own code", _r["self"] >= 0.99, True)
# 13.00.9044's listing attributes the `or al, 1` to the next statement (line 11), 14.00's to line 10
_ln_rmw = [11] if BUILD == "13.00.9044" else [10]
check("lift: `= 1` against `|= 1` is named at that statement: ours st, original rmw.or",
      [(d["lines"], d["ours"], d["orig"]) for d in _r["diffs"]], [(_ln_rmw, [], ["ld"]), (_ln_rmw, [], ["or:R,R"])]
      if BUILD == "13.00.9044" else [([10], ["st"], ["rmw.or"])])
