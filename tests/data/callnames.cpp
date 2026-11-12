/* call-target names in the 13.10 IL dump (tracer.md, `il`): free functions, a member, a same-unit callee */
struct A { void m(int); static int s(int); };
void g1(int); int g2(int, int);
static int __declspec(noinline) loc(int x) { return x * 3 + 1; }
int call_names(A *a, int x) { g1(x); a->m(x); int r = A::s(x) + g2(x, 1); return r + loc(r); }
