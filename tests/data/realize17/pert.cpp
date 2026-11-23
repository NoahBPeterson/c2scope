// orig.cpp with the declarations of p / s and of q / t exchanged (realize pairs, cl 17.00 x64).
extern void g(int);
extern int h(int *);

int pr2(int *a, int n, int m) {
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
