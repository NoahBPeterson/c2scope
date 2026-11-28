// tests/probes/realize_rel.py: the constructs for rasim's interf / nointerf / copy / nocopy / pref / noreg / canreg
// answers (docs/c2/realize.md 2.4a).  uv run tools/realize.py pairs tests/data/realize_rel.cpp -- /O2 /TP /MT /GS /EHsc /Gy
struct S { int a, b, c; S* next; };
extern int g(int);
extern int g2(int, int);

// interf:a:s / nointerf:a:s: the statement `t = a + 1` moved past the call that defines s
int itf__base(S* p, int n) { int a = g(n); int t = a + 1; int s = g(p->a); return g2(t, s) + g(s); }
int itf__late(S* p, int n) { int a = g(n); int s = g(p->a); int t = a + 1; return g2(t, s) + g(s); }
// canreg:p:eax: p's last use ahead of the call (the member read into a local first); the reverse is noreg
int cr__base(S* p, int n) { int s = g(p->a); return s + n + g(s) + p->b; }
int cr__early(S* p, int n) { int t = p->b + n; int s = g(p->a); return s + g(s) + t; }
// noreg:n:eax: n read after the calls (equal arms on a statement of the tail: no code, one more use of n)
int nr__base(S* p, int n) { int t = n * 3 + n; int s = g(p->a); s += g(t); s += p->c; return s + p->b; }
int nr__eqif(S* p, int n) { int t = n * 3 + n; int s = g(p->a); s += g(t); if ((int)n & 1) s += p->c; else s += p->c; return s + p->b; }
// copy:k:n / nocopy:k:n: a parameter copied into a local that the loop steps
int cp__base(S* p, int n) { int s = g(p->a); while (n > 0) { s += g(n); n--; } return s + p->b; }
int cp__copy(S* p, int n) { int s = g(p->a); int k = n; while (k > 0) { s += g(k); k--; } return s + p->b; }
// pref:V:eax:+1 / -1: a call result in a web of its own (one `mov web, eax` copy)
int pf__base(S* p, int n) { int s = g(n); s += g(p->a); return s + p->b; }
int pf__split(S* p, int n) { int s = g(n); int t = g(p->a); s += t; return s + p->b; }
