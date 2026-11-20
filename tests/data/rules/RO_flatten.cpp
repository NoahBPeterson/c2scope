// opt.md §2 rules 2-3, §0 item 4: OptFuncExpr flattens add chains before CSE (a sum inside a sum is not
// shared) and pulls common factors out. One function per form; checks in tests/probes/opt.py.
int h(int);
int sum_twice(int a, int b) { return h(a + b) + (a + b); }        // a + b computed twice
int mul_twice(int a, int b) { return h(a * b) + (a * b); }        // one imul
int fac_same(int a) { return a * 3 + a * 5; }                     // a*8
int fac_common(int a, int b) { return a * 4 + b * 4; }            // (a + b)*4
int fac_load(int* p) { return p[1] + p[1]; }                      // [p+4]*2: one load
int fac_and(int a, int b) { return (a & b) + ((a & b) << 3); }    // (a & b)*9
