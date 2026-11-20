// regalloc_cost.md 9 (vs2005 "declaration order of locals moved registers"): with equal priorities the order is
// defPos (def position), not declaration order. /DDECLSWAP swaps the declarations only, /DINITSWAP the defs only.
int g(int);
int dc() {
#ifdef DECLSWAP
    int b, a;
#else
    int a, b;
#endif
#ifdef INITSWAP
    b = g(2); a = g(1);
#else
    a = g(1); b = g(2);
#endif
    g(0); g(a); g(b); g(a); g(b);
    return 0;
}
