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
int swap1_e1(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    if ((__int64)(p) & 1) { g(p[0]); } else { g(p[0]); }
    g(s);
    if (n > 2) {
        g(p[1]);
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap1_e2(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    if ((__int64)(p) & 1) { g(s); } else { g(s); }
    if (n > 2) {
        g(p[1]);
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap1_e3(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    if (n > 2) {
        if ((__int64)(p) & 1) { g(p[1]); } else { g(p[1]); }
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap1_e4(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    if (n > 2) {
        g(p[1]);
        if ((__int64)(p) & 1) { g(s + 1); } else { g(s + 1); }
    }
    g(h(p));
    return s;
}
int swap1_e5(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    if (n > 2) {
        g(p[1]);
        g(s + 1);
    }
    if ((__int64)(p) & 1) { g(h(p)); } else { g(h(p)); }
    return s;
}
int swap1_e6(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    if ((__int64)(s) & 1) { g(p[0]); } else { g(p[0]); }
    g(s);
    if (n > 2) {
        g(p[1]);
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap1_e7(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    if ((__int64)(s) & 1) { g(s); } else { g(s); }
    if (n > 2) {
        g(p[1]);
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap1_e8(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    if (n > 2) {
        if ((__int64)(s) & 1) { g(p[1]); } else { g(p[1]); }
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap1_e9(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    if (n > 2) {
        g(p[1]);
        if ((__int64)(s) & 1) { g(s + 1); } else { g(s + 1); }
    }
    g(h(p));
    return s;
}
int swap1_e10(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    if (n > 2) {
        g(p[1]);
        g(s + 1);
    }
    if ((__int64)(s) & 1) { g(h(p)); } else { g(h(p)); }
    return s;
}
int swap2_e11(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    if ((__int64)(p) & 1) { g(p[0]); } else { g(p[0]); }
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
int swap2_e12(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    if ((__int64)(p) & 1) { g(p[1]); } else { g(p[1]); }
    g(p[2]);
    g(s);
    if (s > 2) {
        g(p[3]);
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap2_e13(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    if ((__int64)(p) & 1) { g(p[2]); } else { g(p[2]); }
    g(s);
    if (s > 2) {
        g(p[3]);
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap2_e14(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    if ((__int64)(p) & 1) { g(s); } else { g(s); }
    if (s > 2) {
        g(p[3]);
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap2_e15(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    g(s);
    if (s > 2) {
        if ((__int64)(p) & 1) { g(p[3]); } else { g(p[3]); }
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap2_e16(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    g(s);
    if (s > 2) {
        g(p[3]);
        if ((__int64)(p) & 1) { g(s + 1); } else { g(s + 1); }
    }
    g(h(p));
    return s;
}
int swap2_e17(int *a, int n) {
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
    if ((__int64)(p) & 1) { g(h(p)); } else { g(h(p)); }
    return s;
}
int swap2_e18(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    if ((__int64)(s) & 1) { g(p[0]); } else { g(p[0]); }
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
int swap2_e19(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    if ((__int64)(s) & 1) { g(p[1]); } else { g(p[1]); }
    g(p[2]);
    g(s);
    if (s > 2) {
        g(p[3]);
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap2_e20(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    if ((__int64)(s) & 1) { g(p[2]); } else { g(p[2]); }
    g(s);
    if (s > 2) {
        g(p[3]);
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap2_e21(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    if ((__int64)(s) & 1) { g(s); } else { g(s); }
    if (s > 2) {
        g(p[3]);
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap2_e22(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    g(s);
    if (s > 2) {
        if ((__int64)(s) & 1) { g(p[3]); } else { g(p[3]); }
        g(s + 1);
    }
    g(h(p));
    return s;
}
int swap2_e23(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    g(s);
    if (s > 2) {
        g(p[3]);
        if ((__int64)(s) & 1) { g(s + 1); } else { g(s + 1); }
    }
    g(h(p));
    return s;
}
int swap2_e24(int *a, int n) {
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
    if ((__int64)(s) & 1) { g(h(p)); } else { g(h(p)); }
    return s;
}
int loop1_e25(int *a, int n) {
    int t = n + 7;
    int *q = a;
    if ((__int64)(t) & 1) { g(t); } else { g(t); }
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    g(t);
    g(h(q));
    return t;
}
int loop1_e26(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        if ((__int64)(t) & 1) { g(q[i]); } else { g(q[i]); }
    }
    g(t);
    g(h(q));
    return t;
}
int loop1_e27(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    if ((__int64)(t) & 1) { g(t); } else { g(t); }
    g(h(q));
    return t;
}
int loop1_e28(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    g(t);
    if ((__int64)(t) & 1) { g(h(q)); } else { g(h(q)); }
    return t;
}
int loop1_e29(int *a, int n) {
    int t = n + 7;
    int *q = a;
    if ((__int64)(q) & 1) { g(t); } else { g(t); }
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    g(t);
    g(h(q));
    return t;
}
int loop1_e30(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        if ((__int64)(q) & 1) { g(q[i]); } else { g(q[i]); }
    }
    g(t);
    g(h(q));
    return t;
}
int loop1_e31(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    if ((__int64)(q) & 1) { g(t); } else { g(t); }
    g(h(q));
    return t;
}
int loop1_e32(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    g(t);
    if ((__int64)(q) & 1) { g(h(q)); } else { g(h(q)); }
    return t;
}
int four_e33(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    if ((__int64)(u) & 1) { g(v); g(v); } else { g(v); g(v); }
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e34(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    if ((__int64)(u) & 1) { g(w[0]); g(w[1]); } else { g(w[0]); g(w[1]); }
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e35(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    if ((__int64)(u) & 1) { g(x); g(x); g(x); } else { g(x); g(x); g(x); }
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e36(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    if ((__int64)(u) & 1) { g(u); } else { g(u); }
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e37(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    if ((__int64)(u) & 1) { g(h(w)); } else { g(h(w)); }
    return u + v + x;
}
int four_e38(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    if ((__int64)(v) & 1) { g(v); g(v); } else { g(v); g(v); }
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e39(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    if ((__int64)(v) & 1) { g(w[0]); g(w[1]); } else { g(w[0]); g(w[1]); }
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e40(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    if ((__int64)(v) & 1) { g(x); g(x); g(x); } else { g(x); g(x); g(x); }
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e41(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    if ((__int64)(v) & 1) { g(u); } else { g(u); }
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e42(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    if ((__int64)(v) & 1) { g(h(w)); } else { g(h(w)); }
    return u + v + x;
}
int four_e43(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    if ((__int64)(w) & 1) { g(v); g(v); } else { g(v); g(v); }
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e44(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    if ((__int64)(w) & 1) { g(w[0]); g(w[1]); } else { g(w[0]); g(w[1]); }
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e45(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    if ((__int64)(w) & 1) { g(x); g(x); g(x); } else { g(x); g(x); g(x); }
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e46(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    if ((__int64)(w) & 1) { g(u); } else { g(u); }
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e47(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    if ((__int64)(w) & 1) { g(h(w)); } else { g(h(w)); }
    return u + v + x;
}
int four_e48(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    if ((__int64)(x) & 1) { g(v); g(v); } else { g(v); g(v); }
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e49(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    if ((__int64)(x) & 1) { g(w[0]); g(w[1]); } else { g(w[0]); g(w[1]); }
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e50(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    if ((__int64)(x) & 1) { g(x); g(x); g(x); } else { g(x); g(x); g(x); }
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e51(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    if ((__int64)(x) & 1) { g(u); } else { g(u); }
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x;
}
int four_e52(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    if ((__int64)(x) & 1) { g(h(w)); } else { g(h(w)); }
    return u + v + x;
}
int hsw_e53(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    if ((__int64)(k) & 1) { g(sum2(s)); } else { g(sum2(s)); }
    g(k);
    g(s->a);
    if (n) {
        g(k + 1);
    }
    g(sum2(s));
    return k;
}
int hsw_e54(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    g(sum2(s));
    if ((__int64)(k) & 1) { g(k); } else { g(k); }
    g(s->a);
    if (n) {
        g(k + 1);
    }
    g(sum2(s));
    return k;
}
int hsw_e55(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    g(sum2(s));
    g(k);
    if ((__int64)(k) & 1) { g(s->a); } else { g(s->a); }
    if (n) {
        g(k + 1);
    }
    g(sum2(s));
    return k;
}
int hsw_e56(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    g(sum2(s));
    g(k);
    g(s->a);
    if (n) {
        if ((__int64)(k) & 1) { g(k + 1); } else { g(k + 1); }
    }
    g(sum2(s));
    return k;
}
int hsw_e57(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    g(sum2(s));
    g(k);
    g(s->a);
    if (n) {
        g(k + 1);
    }
    if ((__int64)(k) & 1) { g(sum2(s)); } else { g(sum2(s)); }
    return k;
}
