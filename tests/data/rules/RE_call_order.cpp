// frontend_il.md rule 2.4: which operand of a binary operator is evaluated first at /O2 when both are calls.
// IL order is left to right (also checked: c2trace il); the doc's wording "an operand that is itself a binary
// operation on a call result (multiply, shift, divide, &) or an address goes first" is tested by the shapes below.
// The function order is read from the listing's `call ?gX` sequence.
int ga(); int gb(); int gc(); int gd();
struct S { int x; int y; };
S* gs(); int* gp();
void h2(int, int);
int g1;
int o_and(void)    { return ga() + (gb() & 3); }
int o_or(void)     { return ga() + (gb() | 3); }
int o_xor(void)    { return ga() + (gb() ^ 3); }
int o_sub(void)    { return ga() + (gb() - 1); }
int o_add(void)    { return ga() + (gb() + 1); }
int o_sar(void)    { return ga() + (gb() >> 1); }
int o_shl(void)    { return ga() + (gb() << 2); }
int o_div(void)    { return ga() + gb() / 3; }
int o_mod(void)    { return ga() + gb() % 3; }
int o_mul(void)    { return ga() + gb() * 2; }
int o_not(void)    { return ga() + ~gb(); }
int o_neg(void)    { return ga() + -gb(); }
int o_cmp(void)    { return ga() + (gb() == 3); }
int o_deref(void)  { return ga() + *gp(); }
int o_index(void)  { return ga() + gp()[2]; }
int o_memb(void)   { return ga() + gs()->y; }
int o_add2(void)   { return ga() + (gb() + gc()); }
int o_sub2(void)   { return ga() - (gb() - gc()); }
int o_and2(void)   { return ga() + (gb() & gc()); }
int o_mulL(void)   { return ga() * 2 + gb(); }
int o_andL(void)   { return (ga() & 1) + gb(); }
int o_mulB(void)   { return ga() * 2 + gb() * 2; }
int o_sub3(void)   { return ga() - gb(); }
int o_mulc(void)   { return ga() * gb(); }
int o_cvt(void)    { return ga() + (char)gb(); }
int o_lt(void)     { return ga() < gb(); }
int o_ltm(void)    { return ga() < gb() * 2; }
void o_arg(void)   { h2(ga(), gb() * gc()); }
void o_arg2(void)  { h2(ga() * 2, gb()); }
void o_st(void)    { g1 = ga() + gb() * 2; }
// named locals and separate statements (the doc's advice for pinning an order)
int n_sub(void)    { int a = ga(); int b = gb(); return a - b; }
int n_and(void)    { int a = ga(); int b = gb() & 3; return a + b; }
int n_mul3(void)   { int a = ga(); int b = gb(); int c = gc(); return a + b * c; }
int s_seq(void)    { g1 = ga(); return g1 - gb(); }
