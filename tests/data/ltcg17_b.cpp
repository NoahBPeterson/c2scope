// ltcgstub x64 probe (docs/c2/ltcg.md 12, cl 17.00): callees in their own object
#define NI __declspec(noinline)
int g;
NI int f12(int unused, int b) { return b * 7 + g; }         // dead parameter: callers do not compute `unused`
NI int kc(int a, int k) { return a * k + g; }               // every caller passes k = 5: folded (WPA)
NI int kv(int a, int k) { return a * k + g; }
NI int fdiv(int a, int b) { return a / b; }                 // writes rax, rdx, r8
NI int fshl(int a, int b) { return a << b; }                // writes rax, rcx
NI int fx(int a) { int i, s = 0; for (i = 0; i < a; i++) s += i * i ^ a; return s; }       // every volatile GPR
NI int fxmm(double *p, int n) { int i; double s = 0; for (i = 0; i < n; i++) s += p[i]; return (int)s; }  // xmm0 + GPRs
