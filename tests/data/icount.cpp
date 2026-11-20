// probes for tools/icount.py (docs/c2/icount.md): each X_ours / X_orig pair differs by one source construct whose
// effect on the instruction count is known; icount X_ours --vs-func X_orig must name that class.
void h(int);
int h2(int);
int g(int);
extern int g_n;
extern int g_tab[];
struct P { int a; struct Q* q; int b; };
struct Q { int m; };

// removed-by:GlobalOptimize/CSE: ours reads p->q three times and sweep 2 keeps one load; the original reads it in
// the arms through a reference used twice, which is another base for the value numbering (loadcse R3): it reloads
int cse_ours(P* p, int c) { int t = p->q->m; if (c) t += p->q[1].m * 3; else t ^= p->q[1].m; return t; }
int cse_orig(P* p, int c) { Q*& r = p->q; int t = p->q->m; if (c) t += r[1].m * 3; else t ^= r[1].m; return t; }

// kept:alias: the same pair the other way round (ours has the reference, the original does not)
int alias_ours(P* p, int c) { Q*& r = p->q; int t = p->q->m; if (c) t += r[1].m * 3; else t ^= r[1].m; return t; }
int alias_orig(P* p, int c) { int t = p->q->m; if (c) t += p->q[1].m * 3; else t ^= p->q[1].m; return t; }

// kept:kill (R5): ours stores through the loaded pointer p->q between two reads of p->a, which may alias; the
// original stores to p->b (the same base at another offset does not kill p->a)
int kill_ours(P* p) { int a = p->a; p->q->m = 0; return a + p->a; }
int kill_orig(P* p) { int a = p->a; p->b = 0; return a + p->a; }

// folded:range-check: ours tests the struct copy's field twice, so the inner `>= 0` is decided by the outer test
// (sweep 1); the original's outer test reads the field through the pointer, which sweep 1 (no CSE yet) cannot
// relate to the copy (Mount & Blade 0x47cfe0)
struct R { int item; int m4; };
int range_ours(R* it) { R ir = *it; if (ir.item >= 0) { if (ir.item >= 0 && ir.item < g_n) return g(g_tab[ir.item] + ir.m4); } return 0; }
int range_orig(R* it) { R ir = *it; if (it->item >= 0) { if (ir.item >= 0 && ir.item < g_n) return g(g_tab[ir.item] + ir.m4); } return 0; }

// removed-by:GlobalOptimize/DeadStores: ours stores p->a twice with no read in between (the first is dead); the
// original reads through another pointer between the stores
int dead_ours(P* p, int* q) { int t = *q; p->a = 1; p->a = 2; return t; }
int dead_orig(P* p, int* q) { p->a = 1; int t = *q; p->a = 2; return t; }

// merged:FgDoLateFlowOpts: ours ends both arms with the same call cleanup and stores, which the late flow optimiser
// merges into one tail (FgTryTailMerge); the original's arms store different values last, so both tails stay
void tail_ours(int a, int* p) { if (a) { h(1); p[1] = 7; p[2] = 8; } else { h(2); p[1] = 7; p[2] = 8; } h(3); }
void tail_orig(int a, int* p) { if (a) { h(1); p[1] = 7; p[2] = 8; } else { h(2); p[1] = 7; p[2] = 9; } h(3); }

// structure: the original has a statement ours lacks
void stmt_ours(P* p) { p->a = 1; h(0); }
void stmt_orig(P* p) { p->a = 1; p->b = 2; h(0); }

// flags hint (/fp:fast): under /fp:precise ours rounds f through its home (fstp / fld); the original, compiled with
// precise semantics off (as /fp:fast would), keeps f on the x87 stack. No hint on 13.10 (no /fp switch).
float fp_src();
int fp_ours() { float f; if ((f = fp_src()) >= 0.5f) return 1; if (f >= -0.5f) return 2; return 3; }
#pragma float_control(precise, off, push)
int fp_orig() { float f; if ((f = fp_src()) >= 0.5f) return 1; if (f >= -0.5f) return 2; return 3; }
#pragma float_control(pop)
