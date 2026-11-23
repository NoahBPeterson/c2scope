// tests/data/effectmine.cpp's em__base with a reference alias of n (effectmine's class alias.ref, applied by hand):
// on cl 17.00 x64 it exchanges n's and s's registers (rbx / rsi) and changes 4 instructions. The recovery test
// (run_tests.py x64_mine_pairs_tests): effectmine mine on this file with effectmine.cpp's own compile as the original
// (--asm-vs) finds the class unalias, bdiff 0.
struct S { int a, b, c; S* next; };
extern int g(int);
extern int g2(int, int);

int em__base(S* p, int a)
{
    int n = g(a);
    int s = g(p->a);
    int& rw_a = n;
    if (a > 2)
        s += g(p->b);
    while (rw_a > 0)
    {
        s += g(rw_a);
        rw_a--;
    }
    return s + p->b;
}
