// RA_immweb: when does a constant become a register candidate (a web counted in P_B)?
// regalloc.md 3.2 (P_B counts immediate candidates), immcand.md 2.3, validation.md "Not tested, or open".
// Rule: two or more qualifying operands with the same value in the function, or one inside a loop (/O2).
// `> 1000` keeps the constant out of the arms (no constant propagation into g's argument).
int g(int);

int i_once(int a)            { if (a > 1000) return g(a); return 0; }                                 // one use: no web
int i_twice(int a, int b)    { if (a > 1000) return g(a); if (b > 1000) return g(b); return 0; }      // two uses: web
int i_loop(int *p, int n)    { int c = 0; for (int i = 0; i < n; i++) if (p[i] > 1000) c++; return c; } // one use in a loop
int i_loopv(int *p, int n, int k) { int c = 0; for (int i = 0; i < n; i++) if (p[i] > k) c++; return c; } // the same with a variable
