// RA_callres: is a call result that is never named a globreg (regalloc.md 8, M&B rule 2 "naming a call result
// adds its copy")? cr_bare keeps g(a)'s result across g(b) without a name, cr_named names it r.
int g(int);

int cr_bare(int a, int b)  { return g(a) - g(b); }
int cr_named(int a, int b) { int r = g(a); return r - g(b); }
int cr_used2(int a, int b) { int r = g(a); return r * g(b) + r; }
