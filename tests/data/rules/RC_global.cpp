// regalloc_cost.md 2 (globals are aliased candidates): a global is kept in a register over a region with a load
// before and a store after it. gl_loop: no call in the loop; gl_call: a call in the loop body (h may read G).
extern int G; void h();
void gl_loop(int n) { for (int i = 0; i < n; i++) G += i; }
void gl_call(int n) { for (int i = 0; i < n; i++) { G += i; h(); } }
