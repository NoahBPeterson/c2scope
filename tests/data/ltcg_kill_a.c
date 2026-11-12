/* LTCG regression probe (docs/c2/ltcg.md): callers in their own object; a, b live across the call. */
int k_eax(int); int k_std(int); int k_ext(int);
int g;
#define CALLER(name, k) __declspec(dllexport) int name(int *p, int a, int b) { int r = k(a); p[0] = r; p[1] = b; p[2] = a; return r; }
CALLER(c_eax, k_eax)
CALLER(c_std, k_std)
CALLER(c_ext, k_ext)
