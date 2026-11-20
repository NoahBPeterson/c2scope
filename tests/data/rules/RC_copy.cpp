// regalloc_cost.md 6 (SubsumeGlobregs): a copy that survives adds a use of src and a def of dst.
// cp: b = a cannot be propagated (a is redefined while b is live), so the copy reaches Color.
int g(int); void u(int);
void cp(int p) { int a = g(p); int b = a; a = g(a); u(a); u(b); u(b); }
