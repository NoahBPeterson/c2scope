// ltcgstub x64 probe (docs/c2/ltcg.md 12, cl 17.00): callers in their own object, values kept across each call in
// the volatile registers the callee does not write
#define EXP __declspec(dllexport)
int f12(int unused, int b);
int kc(int a, int k);
int kv(int a, int k);
int fdiv(int a, int b);
int fshl(int a, int b);
int fx(int a);
int fxmm(double *p, int n);
EXP int c12(int x, int y) { return f12(x * 3, y); }
EXP int ck1(int x) { return kc(x, 5) + kv(x, 5); }
EXP int cdiv(int *p, int a, int b, int c) { int r = fdiv(a, b); p[0] = r; p[1] = b; p[2] = a; p[3] = c; return r; }
EXP int cshl(int *p, int a, int b, int c) { int r = fshl(a, b); p[0] = r; p[1] = b; p[2] = a; p[3] = c; return r; }
EXP int cx(int *p, int a, int b, int c, int d, int e) { int r = fx(a); p[0] = r; p[1] = b; p[2] = a; p[3] = c; p[4] = d; p[5] = e; return r; }
EXP double cxm(double *p, int n, double y) { int r = fxmm(p, n); return y * r + y; }
