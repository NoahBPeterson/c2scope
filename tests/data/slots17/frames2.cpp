#include <malloc.h>
extern void use(void *);
extern int many(int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int);
struct __declspec(align(32)) V32 { float f[8]; };
// alloca and an outgoing-argument area above 0xf0: B = min(A, 0xf0) < A
int big1(int n) { int *p = (int *)_alloca(n * 4); int a[3]; a[0] = n; p[0] = n; use(p); use(a); return many(n + 0, n + 1, n + 2, n + 3, n + 4, n + 5, n + 6, n + 7, n + 8, n + 9, n + 10, n + 11, n + 12, n + 13, n + 14, n + 15, n + 16, n + 17, n + 18, n + 19, n + 20, n + 21, n + 22, n + 23, n + 24, n + 25, n + 26, n + 27, n + 28, n + 29, n + 30, n + 31, n + 32, n + 33) + a[1]; }
// the same with a local in a parameter home and a sorted frame (T > 128)
int big2(int n, int m) { int *p = (int *)_alloca(n); char b[100]; int x = m; b[0] = (char)n; use(b); use(&x); p[0] = x; return many(x + 0, x + 1, x + 2, x + 3, x + 4, x + 5, x + 6, x + 7, x + 8, x + 9, x + 10, x + 11, x + 12, x + 13, x + 14, x + 15, x + 16, x + 17, x + 18, x + 19, x + 20, x + 21, x + 22, x + 23, x + 24, x + 25, x + 26, x + 27, x + 28, x + 29, x + 30, x + 31, x + 32, x + 33) + b[3] + p[1]; }
// alloca, an over-aligned local and a large argument area
int big3(int n) { V32 v; int *p = (int *)_alloca(n * 4); v.f[0] = (float)n; p[0] = n; use(p); use(&v); return many(n + 0, n + 1, n + 2, n + 3, n + 4, n + 5, n + 6, n + 7, n + 8, n + 9, n + 10, n + 11, n + 12, n + 13, n + 14, n + 15, n + 16, n + 17, n + 18, n + 19, n + 20, n + 21, n + 22, n + 23, n + 24, n + 25, n + 26, n + 27, n + 28, n + 29, n + 30, n + 31, n + 32, n + 33); }
// no alloca, large argument area, an over-aligned local
int big4(int n) { V32 v; int a[2]; v.f[0] = (float)n; a[0] = n; use(&v); use(a); return many(n + 0, n + 1, n + 2, n + 3, n + 4, n + 5, n + 6, n + 7, n + 8, n + 9, n + 10, n + 11, n + 12, n + 13, n + 14, n + 15, n + 16, n + 17, n + 18, n + 19, n + 20, n + 21, n + 22, n + 23, n + 24, n + 25, n + 26, n + 27, n + 28, n + 29, n + 30, n + 31, n + 32, n + 33) + a[1]; }
// many locals beyond 128 bytes with alloca (sorted colours)
int big5(int n) { char s1[50], s2[70]; int k = n, *p = (int *)_alloca(n); s1[0] = s2[0] = (char)n; use(s1); use(s2); use(&k); p[0] = k; return s1[2] + s2[3] + k; }
