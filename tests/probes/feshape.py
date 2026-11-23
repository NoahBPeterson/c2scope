# tools/feshape.py (front-end IL shapes, effectmine model F; docs/c2/effectmine.md section 7)
import sys as _sys
_sys.path.insert(0, os.path.join(HERE, "tools"))
import feshape as _fe

check("feshape: shapes of reader-IL lines (labels, call targets, literals, a branch's condition)",
      [_fe.shape(l) for l in (
          "I d1d028 OPCALL 3 0 | t64:5004 <- s0:1004 ; s0=?FUN_00d386a0@@YAMXZ",
          "I d1d208 OPCMP 3 0 | t67:9000 <- t66:5008 fconst:5008 ; s1=0.3333333432674408(0.33333334f)",
          "I d1d290 OPBRANCH 3 0 | <- s0:0 t67:9000 -> ; cc=l s0=Ld1d258",
          "I d1d0b4 OPASSIGN 3 0 | _f:5004 <- t64:5004",
          "OPASSIGN | [t64:1004+0]:1004 <- 2:1004 t64:1004",
          "P 103e024 OPPROLOGEND 0", "B 103e944")],
      ["OPCALL(T:5004<-F)", "OPCMP(T:9000<-T:5008,C:5008)", "OPBRANCH[l](<-L,T:9000)", "OPASSIGN(V:5004<-T:5004)",
       "OPASSIGN(M:1004<-C:1004,T:1004)", "OPPROLOGEND", None])
_src = os.path.join(HERE, "tests", "data", "fe", "fe_cond.cpp")
_a = _fe.collect(_src, "?fe_f@@", ["/O2", "/TP"])
_b = _fe.collect(_src, "?fe_f@@", ["/O2", "/TP", "/DASSIGN_IN_COND"])
_e = _fe.effects(_a, _b)
check("feshape: assignment in the condition adds an OPPAREN of the variable in the reader IL",
      any(s.startswith("fe:OPPAREN(") and s.endswith(":+1") for s in _e), True)
if BUILD == "13.00.9044":    # port-1300.md: both passes run before Lower, so the inlined stage shows the same change
    # (effects() leaves an `fi:` out when the reader stage already has the same change)
    check("feshape 13.00.9044: the reader and inlined stages (Inline, OptFuncExpr) both see the OPPAREN",
          (sorted(_a), _b["inlined"].get("OPPAREN(T:4004<-V:4004)"),
           "fe:OPPAREN(T:4004<-V:4004):+1" in _e),
          (["inlined", "reader"], 1, True))
check("feshape: a wanted removal needs the opcode's net count to drop (an operand-kind change is no removal)",
      [_fe.hit(["fe:OPADD(T:1004<-V:1004,C:1004):-1", "fe:OPADD(T:1004<-T:1004,C:1004):+1"],
               "fe:OPADD(T:1004<-V:1004,C:1004):-1"),
       _fe.hit(["fe:OPADD(T:1004<-V:1004,C:1004):-1", "fe:OPARG(T:1004<-V:1004):+1"],
               "fe:OPADD(T:1004<-V:1004,C:1004):-1"),
       _fe.hit(["fe:OPBRANCH[e](<-L,T:8000):-1"], "fi:OPBRANCH[e](<-L,T:8000):-1")],
      [None, "fe:OPADD(T:1004<-V:1004,C:1004):-1", None])
import effectmine as _em
check("effectmine: model F types by net count per opcode (`F.fe:kind` for a rename)",
      sorted(_em.f_types(["fe:OPADD(T:1004<-V:1004,C:1004):-1", "fe:OPADD(T:1004<-T:1004,C:1004):+1",
                          "fe:OPARG(T:1004<-V:1004):+1"])), ["F.fe:OPARG+", "F.fe:kind"])
import rwsweep as _rw
_t = ("int g(int);\nint f(int* p, int n) { int k = p[n] + 1; int a = n * 2; int c = g(a); int q = n; n++; "
      "int s = c + 1; return k + q + s + a; }\n")
_B = _rw.Body(_t, _rw.body_of(_t, "f"), None)
check("rwsweep subst: a single-use local substituted; not across a call, a store, nor when used twice",
      [v[1].split(" substituted")[0] for v in _rw.gen_subst(_B)], ["int s = c + 1"])
_t = ("int g(int);\nvoid f(int* p, int n, unsigned* q) { if (!p) return; int k = n; unsigned u = 3; p[1] = 1; "
      "*q |= 4; int a = --n; if (k >= n) g(k); g(a + (int)u); }\n")
_B = _rw.Body(_t, _rw.body_of(_t, "f"), None)
check("rwsweep repair: drop, rmw, sign, step and edge rewrites at the expected sites",
      [v[1] for v in _rw.gen_repair(_B)],
      ["`if (!p) return;` removed at body line 0", "`int k` as `unsigned int k` at body line 0",
       "`unsigned u` as `int u` at body line 0", "`p[1] = 1;` removed at body line 0",
       "`p[1] = 1` as `p[1] |= 1` at body line 0", "`p[1] = 1` as `p[1] &= 1` at body line 0",
       "`*q |= 4;` removed at body line 0", "`*q |= 4` as `*q = 4` at body line 0",
       "`int a` as `unsigned int a` at body line 0", "`a = --n` as `a = n - 1` at body line 0",
       "`g(a + (int)u);` removed at body line 0", "if at body line 0: `>=` as `>`"])
