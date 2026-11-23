// probes for tools/icount.py on cl 17.00 x64 (docs/c2/icount.md, 17.00): each X_ours / X_orig pair differs by one
// source construct; icount X_ours --vs-func X_orig names the pass interval that removed or kept the difference.
void h(int);
int g(int);
extern int g_n;
struct P { int a; struct Q* q; int b; };
struct Q { int m; };

// a repeated expression: ours computes p[1] * 3 twice, the original two different products
int cse_ours(int* p, int c) { int t = p[1] * 3; if (c) t += p[1] * 3; h(t); return p[1] * 3; }
int cse_orig(int* p, int c) { int t = p[1] * 3; if (c) t += p[2] * 3; h(t); return p[3] * 3; }

// a compare decided by a dominating test of the same value: ours tests x twice, the original x and y
void dom_ours(int x, int y) { if (x > 5) { h(1); if (x > 3) h(2); } }
void dom_orig(int x, int y) { if (x > 5) { h(1); if (y > 3) h(2); } }

// a loop-invariant product: ours multiplies the loop-invariant k * m inside the loop, the original k * i
int inv_ours(int* p, int n, int k, int m) { int s = 0; for (int i = 0; i < n; i++) s += p[i] * (k * m); return s; }
int inv_orig(int* p, int n, int k, int m) { int s = 0; for (int i = 0; i < n; i++) s += p[i] * (k * i); return s; }

// a store read back through the same pointer: ours reloads p->a after a store through p->q (may alias), the
// original after a store to p->b (cannot alias p->a): one load fewer
int kill_ours(P* p) { int a = p->a; p->q->m = 0; return a + p->a; }
int kill_orig(P* p) { int a = p->a; p->b = 0; return a + p->a; }

// a partially redundant expression: computed on one arm and after the join
int pre_ours(int a, int b, int c) { int t = 0; if (c) t = a * b; h(t); return a * b; }
int pre_orig(int a, int b, int c) { int t = 0; if (c) t = a * b; h(t); return a * c; }

// a value copied through locals: ours copies x through two locals before the call, the original reads a global
int copy_ours(int x) { int y = x; int z = y; h(z); return z + y; }
int copy_orig(int x) { int y = x; int z = g_n; h(z); return z + y; }
