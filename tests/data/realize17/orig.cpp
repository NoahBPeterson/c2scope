// realize pairs on cl 17.00 x64 (docs/c2/realize.md, "cl 17.00 x64: pairs"). Two pairs of values live across calls
// with equal NColor priorities: the lower lifetime index (first reference) of each pair is processed first and takes
// the earlier callee-saved register. pert.cpp has both declaration pairs exchanged: each single exchange back fixes
// one pair; only the pair of them gives this file's registers (and code).
extern void g(int);
extern int h(int *);

int pr2(int *a, int n, int m) {
    int *p = a + 1;
    int s = n * 3;
    int *q = a + 5;
    int t = m * 5;
    g(p[0]);
    g(s);
    g(p[1]);
    g(s + 1);
    g(q[0]);
    g(t);
    if (n > 2) {
        g(p[2]);
        g(s + 2);
    }
    g(h(p));
    g(h(q));
    return s + t;
}
