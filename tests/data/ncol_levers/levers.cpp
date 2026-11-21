// Benchmarks of the source levers on cl 17.00 x64 (NColor; docs/c2/ncolor.md 10).
extern void g(int);
extern int h(int *);

// p and s are live across calls: both go to callee-saved registers. Equal priorities (one appearance more of each
// in block 1 than elsewhere): the lower lifetime index (first reference) is processed first and takes rdi.
int swap1(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    if (n > 2) {
        g(p[1]);
        g(s + 1);
    }
    g(h(p));
    return s;
}

// The swap of docs/c2/ncolor.md 9 / T16: the pointer temporary p has more appearances in block 1 than s, so p is
// processed first and takes rbx; s needs more appearances in the first block to take rbx.
int swap2(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    g(s);
    if (s > 2) {
        g(p[3]);
        g(s + 1);
    }
    g(h(p));
    return s;
}

// A loop: q is used in the loop body (depth 1: its appearances there weigh 4x), t only before and after it. q is
// processed first and takes rbx; t needs appearances in the loop (or q fewer there) to take it.
int loop1(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    g(t);
    g(h(q));
    return t;
}

// Four values across calls: rbx rdi rsi r14 in priority order; the wanted change moves u up one place.
int four(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}

// helpersweep: an inline helper that takes member addresses, in a function with an EH frame (a local with a
// destructor). k and s are live across the calls.
struct Pair { int a, b; };
struct Guard { ~Guard(); };
__forceinline int rd(const int *p) { return *p; }
inline int sum2(Pair *s) { return rd(&s->a) + rd(&s->b); }
int hsw(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    g(sum2(s));
    g(k);
    g(s->a);
    if (n) {
        g(k + 1);
    }
    g(sum2(s));
    return k;
}
