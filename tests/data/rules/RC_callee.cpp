// regalloc_cost.md 9 (M&B 6.5 "callee visibility sets the global register map"): does a value that lives across a
// call to a callee defined in the same unit keep a caller-saved register when the callee does not touch it?
// default: static noinline leaf defined above; /DEXT: an external noinline function defined above;
// /DAFTER: an external noinline function defined below the caller; /DDECL: only declared.
int g(int);
#if defined(DECL) || defined(AFTER)
int leaf(int x);
#elif defined(EXT)
__declspec(noinline) int leaf(int x) { return x * 3 + 1; }
#else
static __declspec(noinline) int leaf(int x) { return x * 3 + 1; }
#endif
int cv(int p) { int a = g(p); int b = leaf(a); int c = leaf(b); return a + b + c; }
#ifdef AFTER
__declspec(noinline) int leaf(int x) { return x * 3 + 1; }
#endif
