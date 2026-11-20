// RA_recnum: GLOBREG record numbers (regalloc.md 3.6.9 "The numbering"): the order in which the compiler first
// met each local and parameter. Every value here lives across calls so that each is one globreg.
void v(int);
int g(int);

// locals initialised from calls, parameters used only later
void n_late(int a, int b)  { int x = g(0); int y = g(1); v(x); v(y); v(a); v(b); v(x); v(y); v(a); v(b); }
// a parameter in the first initializer: does the other parameter come with it?
void n_ab(int a, int b)    { int x = g(b); int y = g(2); v(x); v(y); v(a); v(b); v(x); v(y); v(a); v(b); }
// the second parameter used first
void n_ba(int a, int b)    { v(b); int x = g(0); v(a); int y = g(1); v(x); v(y); v(a); v(b); v(x); v(y); v(a); v(b); }
// parameters declared b-first in the signature
void n_sig(int b, int a)   { int x = g(0); int y = g(1); v(x); v(y); v(a); v(b); v(x); v(y); v(a); v(b); }
// more first statements: a parameter in a call / an expression / a global, a parameter read after the first call
extern int gx;
void n_aa(int a, int b)    { int x = g(a); int y = g(2); v(x); v(y); v(a); v(b); v(x); v(y); v(a); v(b); }
void n_expr(int a, int b)  { int x = b + 1; int y = g(2); v(x); v(y); v(a); v(b); v(x); v(y); v(a); v(b); }
void n_glob(int a, int b)  { int x = gx + 1; int y = g(1); v(x); v(y); v(a); v(b); v(x); v(y); v(a); v(b); }
void n_vb(int a, int b)    { int x = g(0); v(b); int y = g(2); v(x); v(y); v(a); v(b); v(x); v(y); v(a); v(b); }
