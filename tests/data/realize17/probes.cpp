// Probes for `realize.py pairs` on cl 17.00 x64: pr2__base is orig.cpp's pr2; each variant exchanges declarations
// (p / s, q / t, both = pert.cpp). Expected: before-order flips only (index:), registers predicted by ncolsim.
extern void g(int);
extern int h(int *);

int pr2__base(int *a, int n, int m) {
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

int pr2__ps(int *a, int n, int m) {
    int s = n * 3;
    int *p = a + 1;
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

int pr2__qt(int *a, int n, int m) {
    int *p = a + 1;
    int s = n * 3;
    int t = m * 5;
    int *q = a + 5;
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

int pr2__both(int *a, int n, int m) {
    int s = n * 3;
    int *p = a + 1;
    int t = m * 5;
    int *q = a + 5;
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
