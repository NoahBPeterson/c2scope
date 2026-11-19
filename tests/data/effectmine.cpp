// Regression probe for tools/effectmine.py (docs/c2/effectmine.md): one function, a few mutation classes whose
// effects are known from realize.md (copy: +2 references and one more candidate, same code; an unused initialised
// local: one frame key, same code; a declaration before the function: nothing on 14.00).
//   uv run tools/effectmine.py mine tests/data/effectmine.cpp --func em__base --classes '^(copy|pad.init.top|decl.extvar.before)$' -- /TP /MT /GS /EHsc /Gy
struct S { int a, b, c; S* next; };
extern int g(int);
extern int g2(int, int);

int em__base(S* p, int a)
{
    int n = g(a);
    int s = g(p->a);
    if (a > 2)
        s += g(p->b);
    while (n > 0)
    {
        s += g(n);
        n--;
    }
    return s + p->b;
}
