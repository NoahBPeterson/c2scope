// x64 (cl 17.00) parameter-home probes, docs/c2/slots.md 6.2 and tests/probes/slots17home.py. `a` is home p0 (offset 8),
// `b` p1 (16), the two dummies p2 (24), p3 (32). A local whose address escapes is a memory element.
void ext(long long *);
void ext3(int *, int *, long long *);
void use(long long);
void use3(long long, long long, long long);
long long ld(long long *);
extern long long g;
struct S { S(const S &); long long v; };
S mk(long long);

// a is used only as a call argument: its home conflicts with every local
void arg_only(long long a, long long b) { long long x; ext(&x); use(x); use(a); }
// a is used in arithmetic only: x joins a's home
void arith(long long a, long long b) { long long x; ext(&x); use(a + x); }
// a is stored: x joins a's home
void store(long long a, long long b) { long long x; ext(&x); use(x); g = a; }
// a is passed to a call and also used in arithmetic: x joins a's home
void arg_arith(long long a, long long b) { long long x; use(a); ext(&x); use(a + x); }
// a is dereferenced and then passed to a call: x joins a's home
void deref_arg(long long a, long long b) { long long x; g = *(long long *)a; ext(&x); use(x); use3(a, 0, 0); }
// a and b are never used
void unused(long long a, long long b) { long long x; ext(&x); use(x); }
// the same with a hidden return slot (a class returned by value) in place of x, and a plain local for comparison
void retslot(long long a, long long b) { S s = mk(0); use(s.v); use(a); }
void plain(long long a, long long b) { long long s; ext(&s); use(s); use(a); }
// three locals live together, a refuses every local (call argument only), b's home is free (dereferenced):
// longs, the one with more uses first
void order8(long long a, long long b)
{
    long long i1, i2, l;
    g = *(long long *)b;
    ext(&i1); ext(&i2); ext(&l);
    use(l); use(l); use(i1 + i2 + l);
    use(a);
}
// the same with i1 and i2 an int each: the smaller are visited first whatever the uses
void order4(long long a, long long b)
{
    int i1, i2;
    long long l;
    g = *(long long *)b;
    ext3(&i1, &i2, &l);
    use(l); use(l); use(i1 + i2 + l);
    use(a);
}
// shrink-wrapped saves of callee-saved registers go to the free homes, lowest first
long long sw1(long long *p)
{
    if (!p) return 0;
    long long v1 = ld(p), v2 = ld(p + 1), v3 = ld(p + 2), v4 = ld(p + 3);
    use(v1); use(v2); use(v3); use(v4);
    return v1 + v2 + v3 + v4;
}
long long sw2(long long *p, long long b)
{
    long long x;
    if (!p) return 0;
    long long v1 = ld(p), v2 = ld(p + 1), v3 = ld(p + 2), v4 = ld(p + 3);
    ext(&x);
    use(v1); use(v2); use(v3); use(v4);
    return v1 + v2 + v3 + v4 + x;
}
