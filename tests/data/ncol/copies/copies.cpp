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
int swap1_a1(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    int *p_a = p;
    g(p_a[0]);
    g(s);
    if (n > 2) {
        g(p_a[1]);
        g(s + 1);
    }
    g(h(p_a));
    return s;
}
int swap1_a2(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    int *p_a = p;
    g(s);
    if (n > 2) {
        g(p_a[1]);
        g(s + 1);
    }
    g(h(p_a));
    return s;
}
int swap1_a3(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    int *p_a = p;
    if (n > 2) {
        g(p_a[1]);
        g(s + 1);
    }
    g(h(p_a));
    return s;
}
int swap1_a4(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    if (n > 2) {
        g(p[1]);
        g(s + 1);
    }
    int *p_a = p;
    g(h(p_a));
    return s;
}
int swap1_a5(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    int s_a = s;
    g(p[0]);
    g(s_a);
    if (n > 2) {
        g(p[1]);
        g(s_a + 1);
    }
    g(h(p));
    return s_a;
}
int swap1_a6(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    int s_a = s;
    g(s_a);
    if (n > 2) {
        g(p[1]);
        g(s_a + 1);
    }
    g(h(p));
    return s_a;
}
int swap1_a7(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    int s_a = s;
    if (n > 2) {
        g(p[1]);
        g(s_a + 1);
    }
    g(h(p));
    return s_a;
}
int swap1_a8(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    if (n > 2) {
        g(p[1]);
        g(s + 1);
    }
    int s_a = s;
    g(h(p));
    return s_a;
}
int swap1_a9(int *a, int *b, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(s);
    if (n > 2) {
        g(p[1]);
        g(s + 1);
    }
    g(h(p));
    int s_a = s;
    return s_a;
}
int swap2_a10(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    int *p_a = p;
    g(p_a[0]);
    g(p_a[1]);
    g(p_a[2]);
    g(s);
    if (s > 2) {
        g(p_a[3]);
        g(s + 1);
    }
    g(h(p_a));
    return s;
}
int swap2_a11(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    int *p_a = p;
    g(p_a[1]);
    g(p_a[2]);
    g(s);
    if (s > 2) {
        g(p_a[3]);
        g(s + 1);
    }
    g(h(p_a));
    return s;
}
int swap2_a12(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    int *p_a = p;
    g(p_a[2]);
    g(s);
    if (s > 2) {
        g(p_a[3]);
        g(s + 1);
    }
    g(h(p_a));
    return s;
}
int swap2_a13(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    int *p_a = p;
    g(s);
    if (s > 2) {
        g(p_a[3]);
        g(s + 1);
    }
    g(h(p_a));
    return s;
}
int swap2_a14(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    g(s);
    int *p_a = p;
    if (s > 2) {
        g(p_a[3]);
        g(s + 1);
    }
    g(h(p_a));
    return s;
}
int swap2_a15(int *a, int n) {
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
    int *p_a = p;
    g(h(p_a));
    return s;
}
int swap2_a16(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    int s_a = s;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    g(s_a);
    if (s_a > 2) {
        g(p[3]);
        g(s_a + 1);
    }
    g(h(p));
    return s_a;
}
int swap2_a17(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    int s_a = s;
    g(p[1]);
    g(p[2]);
    g(s_a);
    if (s_a > 2) {
        g(p[3]);
        g(s_a + 1);
    }
    g(h(p));
    return s_a;
}
int swap2_a18(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    int s_a = s;
    g(p[2]);
    g(s_a);
    if (s_a > 2) {
        g(p[3]);
        g(s_a + 1);
    }
    g(h(p));
    return s_a;
}
int swap2_a19(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    int s_a = s;
    g(s_a);
    if (s_a > 2) {
        g(p[3]);
        g(s_a + 1);
    }
    g(h(p));
    return s_a;
}
int swap2_a20(int *a, int n) {
    int *p = a + 1;
    int s = n * 3;
    g(p[0]);
    g(p[1]);
    g(p[2]);
    g(s);
    int s_a = s;
    if (s_a > 2) {
        g(p[3]);
        g(s_a + 1);
    }
    g(h(p));
    return s_a;
}
int swap2_a21(int *a, int n) {
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
    int s_a = s;
    g(h(p));
    return s_a;
}
int swap2_a22(int *a, int n) {
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
    int s_a = s;
    return s_a;
}
int loop1_a23(int *a, int n) {
    int t = n + 7;
    int *q = a;
    int t_a = t;
    g(t_a);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    g(t_a);
    g(h(q));
    return t_a;
}
int loop1_a24(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    int t_a = t;
    g(t_a);
    g(h(q));
    return t_a;
}
int loop1_a25(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    g(t);
    int t_a = t;
    g(h(q));
    return t_a;
}
int loop1_a26(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    g(t);
    g(h(q));
    int t_a = t;
    return t_a;
}
int loop1_a27(int *a, int n) {
    int t = n + 7;
    int *q = a;
    int *q_a = q;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q_a[i]);
    }
    g(t);
    g(h(q_a));
    return t;
}
int loop1_a28(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    int *q_a = q;
    g(t);
    g(h(q_a));
    return t;
}
int loop1_a29(int *a, int n) {
    int t = n + 7;
    int *q = a;
    g(t);
    for (int i = 0; i < n; i++) {
        g(q[i]);
    }
    g(t);
    int *q_a = q;
    g(h(q_a));
    return t;
}
int four_a30(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    int u_a = u;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u_a);
    if (m) { g(u_a); g(v); }
    g(h(w));
    return u_a + v + x;
}
int four_a31(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    int u_a = u;
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u_a);
    if (m) { g(u_a); g(v); }
    g(h(w));
    return u_a + v + x;
}
int four_a32(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    int u_a = u;
    g(x); g(x); g(x);
    g(u_a);
    if (m) { g(u_a); g(v); }
    g(h(w));
    return u_a + v + x;
}
int four_a33(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    int u_a = u;
    g(u_a);
    if (m) { g(u_a); g(v); }
    g(h(w));
    return u_a + v + x;
}
int four_a34(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    int u_a = u;
    if (m) { g(u_a); g(v); }
    g(h(w));
    return u_a + v + x;
}
int four_a35(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    int u_a = u;
    g(h(w));
    return u_a + v + x;
}
int four_a36(int *a, int n, int m) {
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
    int u_a = u;
    return u_a + v + x;
}
int four_a37(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    int v_a = v;
    g(v_a); g(v_a);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v_a); }
    g(h(w));
    return u + v_a + x;
}
int four_a38(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    int v_a = v;
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v_a); }
    g(h(w));
    return u + v_a + x;
}
int four_a39(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    int v_a = v;
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v_a); }
    g(h(w));
    return u + v_a + x;
}
int four_a40(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    int v_a = v;
    g(u);
    if (m) { g(u); g(v_a); }
    g(h(w));
    return u + v_a + x;
}
int four_a41(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    int v_a = v;
    if (m) { g(u); g(v_a); }
    g(h(w));
    return u + v_a + x;
}
int four_a42(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    int v_a = v;
    g(h(w));
    return u + v_a + x;
}
int four_a43(int *a, int n, int m) {
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
    int v_a = v;
    return u + v_a + x;
}
int four_a44(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    int *w_a = w;
    g(v); g(v);
    g(w_a[0]); g(w_a[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w_a));
    return u + v + x;
}
int four_a45(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    int *w_a = w;
    g(w_a[0]); g(w_a[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w_a));
    return u + v + x;
}
int four_a46(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    int *w_a = w;
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w_a));
    return u + v + x;
}
int four_a47(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    int *w_a = w;
    g(u);
    if (m) { g(u); g(v); }
    g(h(w_a));
    return u + v + x;
}
int four_a48(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    int *w_a = w;
    if (m) { g(u); g(v); }
    g(h(w_a));
    return u + v + x;
}
int four_a49(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    int *w_a = w;
    g(h(w_a));
    return u + v + x;
}
int four_a50(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    int x_a = x;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x_a); g(x_a); g(x_a);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x_a;
}
int four_a51(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    int x_a = x;
    g(w[0]); g(w[1]);
    g(x_a); g(x_a); g(x_a);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x_a;
}
int four_a52(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    int x_a = x;
    g(x_a); g(x_a); g(x_a);
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x_a;
}
int four_a53(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    int x_a = x;
    g(u);
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x_a;
}
int four_a54(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    int x_a = x;
    if (m) { g(u); g(v); }
    g(h(w));
    return u + v + x_a;
}
int four_a55(int *a, int n, int m) {
    int u = n + m;
    int v = n - m;
    int *w = a + 2;
    int x = n * m;
    g(v); g(v);
    g(w[0]); g(w[1]);
    g(x); g(x); g(x);
    g(u);
    if (m) { g(u); g(v); }
    int x_a = x;
    g(h(w));
    return u + v + x_a;
}
int four_a56(int *a, int n, int m) {
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
    int x_a = x;
    return u + v + x_a;
}
int hsw_a57(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    int k_a = k;
    g(sum2(s));
    g(k_a);
    g(s->a);
    if (n) {
        g(k_a + 1);
    }
    g(sum2(s));
    return k_a;
}
int hsw_a58(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    g(sum2(s));
    int k_a = k;
    g(k_a);
    g(s->a);
    if (n) {
        g(k_a + 1);
    }
    g(sum2(s));
    return k_a;
}
int hsw_a59(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    g(sum2(s));
    g(k);
    int k_a = k;
    g(s->a);
    if (n) {
        g(k_a + 1);
    }
    g(sum2(s));
    return k_a;
}
int hsw_a60(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    g(sum2(s));
    g(k);
    g(s->a);
    int k_a = k;
    if (n) {
        g(k_a + 1);
    }
    g(sum2(s));
    return k_a;
}
int hsw_a61(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    g(sum2(s));
    g(k);
    g(s->a);
    if (n) {
        g(k + 1);
    }
    int k_a = k;
    g(sum2(s));
    return k_a;
}
int hsw_a62(Pair *s, int n) {
    Guard gd;
    int k = n * 2;
    g(sum2(s));
    g(k);
    g(s->a);
    if (n) {
        g(k + 1);
    }
    g(sum2(s));
    int k_a = k;
    return k_a;
}
