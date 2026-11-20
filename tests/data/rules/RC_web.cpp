// regalloc_cost.md 9 ("x = q(x) starts a new web"; "two-address results are defs of the same web"): webs are
// lifetimes. nw: x = q(x) gives x two globregs. tw: a += g(2) is a use and a def of the one web of a.
int q(int); void u(int); int g(int);
void nw(int p) { int x = q(p); u(x); u(x); x = q(x); u(x); u(x); u(x); }
void tw(int p) { int a = g(p); u(a); a += g(2); u(a); u(a); }
