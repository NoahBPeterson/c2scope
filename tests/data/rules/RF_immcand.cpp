// immcand.md 2.3 / 2.4 / 5: which constants are immediate candidates, and where they are referenced.
// One qualifying use of K = 1000 (a push) per function unless said otherwise. 2.3: a once-only constant is a
// candidate only in a block with BLOCK+0x16c != 0 ("in a loop"?) and only at /O2 (_Speed); here every loop form,
// a use after a loop, a use in an if arm inside a loop. k64: 64-bit constants (only the low dword is hashed).
// dnest: K in a nested if arm and after the outer join: where does the load go (2.4, AvoidAirlocks)?
void g(int); void g2(int, int); int h(void);
struct L { long long v; int w; };
void l_for(int n) { for (int i = 0; i < n; i++) g(1000); }
void l_while(int n) { while (n--) g(1000); }
void l_do(int n) { do g(1000); while (--n); }
void l_goto(int n) { top: g(1000); if (--n) goto top; }
void l_nested(int n, int m) { for (int i = 0; i < n; i++) { for (int j = 0; j < m; j++) g(1000); g(i); } }
void l_after(int n) { for (int i = 0; i < n; i++) g(i); g(1000); }
void l_ifin(int n) { for (int i = 0; i < n; i++) if (i & 1) g(1000); }
void l_none(int n) { g(1000); }
void k64a(L* p) { p->v = 0x0000000700000007LL; }
void k64b(L* p) { p->v = 0x0000000500000007LL; p->w = 7; }
void k64c(L* p, L* q) { p->v = 0x0000000500000007LL; q->v = 0x0000000600000007LL; }
void k64d(L* p) { p->v = 0x0000000700000005LL; p->w = 7; }
void dnest(int a, int b) { if (a) { h(); if (b) g(1000); } h(); g(1000); }
// 2.5: b9 (once in each arm: no candidate left) against b8 (an arm and after the join: kept). Which pass drops b9's?
void b8(int n) { if (n) g(1000); h(); g(1000); }
void b9(int n) { if (n) g(1000); else g2(1000, 1); h(); }
