// /O2 shapes of the lifter (docs/c2/lift2il.md): compiled /O2 /arch:SSE, lifted back from its bytes, recompiled
struct P { float v; int k; };
int o2_fsel(P* a, int i, int n) { if (a[i + 1].v > a[i].v) i = i - 1; return a[i].k + n; }
int o2_sx(const signed char* t, int i) { return t[i] * 3; }
