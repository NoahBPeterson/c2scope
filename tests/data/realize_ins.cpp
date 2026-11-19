// Regression probe for realize.py's `ins` family (docs/c2/realize.md 2.5.1): a statement before an if whose then-arm
// returns, written at the head of that arm and again after the if (`headret`). The copies get the same scratch
// register and are head-merged above the test after RegSimpleAssignBody (14.00, 15.00): the copy after the if
// leaves one hidden rotation pick (ins:4), the three later picks rotate one step, the instructions stay.
// 13.10 keeps the copies. With 3 picks in the arm the copies get different registers and nothing merges (h3).
//   uv run tools/realize.py pairs tests/data/realize_ins.cpp --scratch -- /TP /MT /EHsc /Gy /O2
//   uv run tools/realize.py edit tests/data/realize_ins.cpp --func hr__base ins:4 -- /TP /MT /EHsc /Gy /O2
struct S { int a, b, c; };
extern int gi, gj, gk, gl;
void lg(int);
void __stdcall sg(int);
void hr__base(S* p, bool arg)
{
    lg(gl);
    if (arg) { gi = p->a; sg(1); gj = p->b; return; }
    gk = p->c; gi = p->b; sg(0); gj = p->a;
}
void hr__dup(S* p, bool arg)
{
    if (arg) { lg(gl); gi = p->a; sg(1); gj = p->b; return; }
    lg(gl);
    gk = p->c; gi = p->b; sg(0); gj = p->a;
}
void h3__base(S* p, bool arg)
{
    lg(gl);
    if (arg) { gi = p->a; sg(1); gj = p->b; gk = p->c; return; }
    gk = p->c; gi = p->b; sg(0); gj = p->a;
}
void h3__dup(S* p, bool arg)
{
    if (arg) { lg(gl); gi = p->a; sg(1); gj = p->b; gk = p->c; return; }
    lg(gl);
    gk = p->c; gi = p->b; sg(0); gj = p->a;
}
