# tools/il2c.py (C++ from c2's IL at Lower's entry; docs/c2/il2c.md)
import sys as _sys
_sys.path.insert(0, os.path.join(HERE, "tools"))
import il2c as _il2c

if _il2c.UNDNAME:
    _src = os.path.join(HERE, "tests", "data", "il2c", "il2c_round.cpp")
    _res = [_il2c.one(dict(id=f, src=_src, func=f, flags=["/O2", "/TP"]))
            for f in ("?sum@Obj@@QAEHXZ", "?pick@Obj@@QAEHI@Z", "?mark@Obj@@QAEXH@Z", "?call_get@Obj@@QAEHH@Z")]
    # 15.00 unrolls loops of unknown length before Lower (opt.md): the IL there is already unrolled, and the
    # emitted copy is unrolled again, so its loop does not round-trip (docs/c2/il2c.md, limits)
    _loop = RKEY != "15.00"
    check("il2c: a loop%s, an unsigned compare with a member call, an RMW and a virtual call round-trip byte for byte"
          % ("" if _loop else " (15.00: unrolled twice, bdiff > 0)"),
          [(r["func"], r["result"], (r.get("bdiff") == 0) == (_loop or r["func"] != "?sum@Obj@@QAEHXZ")) for r in _res],
          [(f, "ok", True) for f in ("?sum@Obj@@QAEHXZ", "?pick@Obj@@QAEHI@Z", "?mark@Obj@@QAEXH@Z",
                                     "?call_get@Obj@@QAEHH@Z")])
else:
    print("  il2c: llvm-undname not found, probe skipped")
