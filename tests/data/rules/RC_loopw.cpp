// regalloc_cost.md 4.1 (block weight 1 << loop depth at /O2) and 10 (did 13.10 weight loops?): t2.c loopw.
// b: def in the entry block (P=5), one use in the loop body (P=3, w=2), live through the loop test.
int f(int); int g(int);
int loopw(int n) { int a = f(1), b = f(2), i; f(0); g(a); g(a); g(a);
    for (i = 0; i < n; i++) { g(b); } f(0); return 0; }
