// c2inline --order / body column (docs/c2/inline.md "Compile order"): every caller is defined before its callee
volatile int g;

// 1. no cycle: the callee is defined later, but c2 compiles it first (bottom-up call-graph order)
int late(int);
int caller_late(int x) { return late(x) + 1; }
int late(int a) { g = a; g = a + 1; g = a + 2; return a; }

// 2. a cycle: the function defined first is compiled first, so it sees cyc_b uncompiled (no size, an IL-count
//    guess decides: a call here); cyc_b then sees cyc_a compiled
int cyc_b(int);
int cyc_a(int x) { return x ? cyc_b(x - 1) + 1 : 0; }
int cyc_b(int x) { g = x; g = x + 1; g = x + 2; g = x + 3; return x ? cyc_a(x - 1) + 1 : 0; }

// 3. a virtual call through this has no call-graph edge: the caller is compiled before the callee
struct V { virtual int vf(int a); int u(int x); };
int V::u(int x) { return vf(x) + 1; }
int V::vf(int a) { g = a; return a; }
