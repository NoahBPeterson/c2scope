#include <malloc.h>
extern void use(void *);
extern int use2(void *, int);
extern int ext(int, int);
struct __declspec(align(32)) V32 { float f[8]; };
struct __declspec(align(64)) V64 { int k[16]; };
// dynamic alignment: one over-aligned local, one plain local
int al32(int n) { V32 v; int a[4]; for (int i = 0; i < 8; i++) v.f[i] = (float)(n + i); a[0] = n; use(a); use(&v); return a[1]; }
// two over-aligned locals and a buffer under /GS
int al64(int n) { V64 w; V32 v; char buf[40]; w.k[0] = n; v.f[0] = 1; buf[0] = (char)n; use(&w); use(&v); use(buf); return w.k[3]; }
// over-aligned local and a parameter whose address is taken
int alp(int n, int m) { V32 v; v.f[1] = (float)m; use(&v); use(&n); return n; }
// alloca: rbp frame
int ac(int n) { int *p = (int *)_alloca(n * 4); int a[3]; a[0] = n; p[0] = n; use(p); use(a); return a[2] + p[1]; }
// alloca with more outgoing arguments than 0xf0 / 8: B = min(A, 0xf0)
int ac2(int n) { int *p = (int *)_alloca(n); int x = n; p[0] = 1; use2(p, x); use(&x); return use2(&x, ext(x, n)) + x; }
// alloca and an over-aligned local
int aca(int n) { V32 v; int *p = (int *)_alloca(n * 4); v.f[0] = (float)n; p[0] = n; use(p); use(&v); return p[1]; }
// call-free, only a tail call: the argument area of the call that became a jump
int tc(int a, int b) { return ext(a + b, a - b); }
// call-free with register pressure into the parameter homes, then a tail call
int tcp(int a, int b, int c, int d, int *p) {
    int x0 = p[0] * a, x1 = p[1] * b, x2 = p[2] * c, x3 = p[3] * d, x4 = p[4] + a, x5 = p[5] + b, x6 = p[6] + c;
    int x7 = p[7] + d, x8 = p[8] ^ a, x9 = p[9] ^ b, x10 = p[10] ^ c, x11 = p[11] ^ d, x12 = p[12] - a, x13 = p[13] - b;
    int x14 = p[14] - c, x15 = p[15] - d;
    for (int i = 0; i < a; i++) { x0 += x1 * x2; x1 += x2 * x3; x2 += x3 * x4; x3 += x4 * x5; x4 += x5 * x6; x5 += x6 * x7;
        x6 += x7 * x8; x7 += x8 * x9; x8 += x9 * x10; x9 += x10 * x11; x10 += x11 * x12; x11 += x12 * x13; x12 += x13 * x14;
        x13 += x14 * x15; x14 += x15 * x0; x15 += x0 * x1; p[i & 15] = x0 + x15; }
    return ext(x0 + x1 + x2 + x3 + x4 + x5 + x6 + x7, x8 + x9 + x10 + x11 + x12 + x13 + x14 + x15);
}
