// invert.md 6: evaluation orders the list scheduler's ties fall back on (lex order). Arguments are evaluated last
// first (a1, a5), member stores keep the source's statement order (s1, s2), a constant return value is set at the
// return, after the statement's stores, whatever the spelling (r1..r3), and hoisted to the entry when it is a
// variable merged across a join (t1).  tests/probes/invert.py.
struct S { int a, b, c; float f; bool r1(); bool r2(); bool r3(); bool t1(); void f1(int); };
void g(int, int, int); int h1(); int h2();
bool S::r1() { b = -1; return false; }
bool S::r2() { bool r = false; b = -1; return r; }
bool S::r3() { return b = -1, false; }
bool S::t1() { bool ok = false; if (c) { f1(c); ok = true; } else b = -1; return ok; }
void a1(S* p) { g(p->a, p->b, p->c); }
void a5() { g(h1(), h2(), 3); }
void s1(S* p, int x, int y) { p->a = x; p->b = y; }
void s2(S* p, int x, int y) { p->b = y; p->a = x; }
