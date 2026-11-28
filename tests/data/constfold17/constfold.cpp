// A constant held in a register or rebuilt as an immediate on cl 17.00 x64 (docs/c2/immcand.md section 7,
// tests/probes/constfold17.py). Each function isolates one decision.
struct S { int a[16]; __int64 q[8]; short h; char c; bool b; int pad; };
void h();
void sink(S*, int);

// zero: NColBenefitOfUsesInRange 8 against MdmDefCost 1: a register, every store `mov [m], reg`
void z8(S* s) { h(); s->a[0] = 0; s->a[2] = 0; s->a[4] = 0; s->a[6] = 0; s->a[8] = 0; s->a[10] = 0; s->a[12] = 0; s->a[14] = 0; }
// zero, one use: sum 1 against cost 1: folded, an immediate
void z1(S* s) { h(); s->a[0] = 0; h(); }
// a nonzero value: every use's benefit is 0, folded however many there are
void n7(S* s) { h(); s->a[0] = 7; s->a[2] = 7; s->a[4] = 7; s->a[6] = 7; s->a[8] = 7; s->a[10] = 7; s->a[12] = 7; s->a[14] = 7; }
// the same value with one 16-bit store: that use cannot take an immediate (INF), so the web is never folded; the
// late peephole then uses the register for the compare, the argument and the dword stores
void n7w(S* s, int* p) { h(); s->a[0] = 7; s->h = 7; s->a[2] = 7; h(); if (*p == 7) h(); sink(s, 7); s->a[4] = 7; }
// a constant that is no imm32: a register by necessity
void big(S* s) { h(); s->q[0] = 0x123456789abcdef0LL; h(); s->q[1] = 0x123456789abcdef0LL; s->q[2] = 0x123456789abcdef0LL; }
// two adjacent dword zero stores after a join: when the zero has two definitions reaching the join (one in each arm)
// the combined qword store is an immediate; with one definition (pair_one) it is the register
struct T { int a, b, c, d, e, f, g, h; void* p; };
void k(void*); void g2(T*, int);
void pair_two(T* s) {
    void* t = s->p;
    if (t) { k(t); g2(s, 1); s->c = 0; s->p = 0; } else { s->c = 0; }
    s->e = 0; s->f = 0; h();
}
void pair_one(T* s) {
    void* t = s->p;
    if (t) { k(t); g2(s, 1); s->c = 0; s->p = 0; }
    s->e = 0; s->f = 0; h();
}
// the same join with a qword store of a literal zero: the register (the qword imm store of the pair is not source)
void qword_zero(T* s) {
    void* t = s->p;
    if (t) { k(t); g2(s, 1); s->c = 0; s->p = 0; } else { s->c = 0; }
    *(__int64*)&s->e = 0; h();
}
