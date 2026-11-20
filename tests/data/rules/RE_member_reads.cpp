// frontend.md "What c2 does to it before allocation": (1) the address temporary `t = this+off` that GlobalOptimize
// makes for two reads of one member is folded back by Color, so no `lea` survives; (2) a by-value accessor and a
// by-reference accessor give the same code, even with a call or a store through another pointer between the reads.
void h();
struct C {
    int pad; int x; int y;
    int get() const { return x; }
    const int& getr() const { return x; }
    int two();     int twocall();     int loop(int n);
};
int C::two()      { return x + y + x; }
int C::twocall()  { int a = x; h(); return a + x; }
int C::loop(int n) { int s = 0; for (int i = 0; i < n; i++) { s += x; h(); } return s; }
int v_plain(C* c)      { return c->get() + c->get(); }
int r_plain(C* c)      { return c->getr() + c->getr(); }
int v_call(C* c)       { int a = c->get(); h(); return a + c->get(); }
int r_call(C* c)       { int a = c->getr(); h(); return a + c->getr(); }
int v_alias(C* c, int* q) { int a = c->get(); *q = 5; return a + c->get(); }
int r_alias(C* c, int* q) { int a = c->getr(); *q = 5; return a + c->getr(); }
int v_loop(C* c, int n) { int s = 0; for (int i = 0; i < n; i++) { s += c->get(); h(); } return s; }
int r_loop(C* c, int n) { int s = 0; for (int i = 0; i < n; i++) { s += c->getr(); h(); } return s; }
void hv(int); void hr(const int&);
void v_pass(C* c)      { hv(c->get()); h(); hv(c->get()); }
void r_pass(C* c)      { hv(c->getr()); h(); hv(c->getr()); }
// the same pairs in a function with a C++ EH frame (a local with a destructor; compile with /EHsc)
struct E { E(); ~E(); };
void hp(int, int);
int v_eh(C* c)         { E e; int a = c->get(); h(); return a + c->get(); }
int r_eh(C* c)         { E e; int a = c->getr(); h(); return a + c->getr(); }
void v_ehp(C* c)       { E e; hp(c->get(), c->y); }
void r_ehp(C* c)       { E e; hp(c->getr(), c->y); }
void v_p2(C* c)        { hp(c->get(), c->y); }
void r_p2(C* c)        { hp(c->getr(), c->y); }
