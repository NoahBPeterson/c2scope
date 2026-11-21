#include <string.h>
extern int g(int, int);
extern double h(double);
struct S { virtual int v(int); int a, b; ~S(); };
int f2(int *a, int n, unsigned k, double d, S *s) {
    int r = n / (int)k + n % 7 + (n << (k & 3)) + (int)(k >> 2) - (n >> 1) + (n & 0x55) + (n | 8);
    r += -n + ~r;
    double x = d * 2.5 + h(d) / 3.0 - (double)n;
    if (x < d) r++;
    r += s->v(r);
    long long q = (long long)n * k;
    r += (int)(q >> 32);
    memcpy(a, a + 4, k);
    for (int i = 0; i < n; i++) a[i] = a[i] > 0 ? a[i] : -a[i];
    try { r += g(r, n); } catch (...) { r = 0; }
    S t; r += t.a;
    return r + (int)x;
}
