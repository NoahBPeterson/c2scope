// opt.md §7 rules 1-2, §12 WpaRangeGlobopt: constant facts from branches are used, value ranges are not.
int h(int);
int rg_nested(int a) { if (a > 10) { if (a > 5) return h(1); } return 0; }      // inner compare kept
int rg_nested2(int a) { if (a > 10) return a > 5; return h(a); }                 // a > 5 kept
int rg_eq(int a) { if (a == 5) return a * 2; return h(a); }                      // folded to 10
int rg_ab(int a, int b) { if (a == b) return a - b; return h(0); }               // not folded
