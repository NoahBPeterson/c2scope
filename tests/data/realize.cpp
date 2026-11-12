// Regression probes for tools/realize.py (docs/c2/realize.md): X__base against X__<variant>, one compile.
//   uv run tools/realize.py pairs tests/data/realize.cpp -- /TP /MT /GS /EHsc /Gy
//   uv run tools/realize.py edit tests/data/realize.cpp --func cpl__base ref:n:3 -- /TP /MT /GS /EHsc /Gy
struct S { int a, b, c; S* next; };
struct C { char c; unsigned char u; short h; int i; };
extern int g(int);
extern int g2(int, int);
extern int gi;
extern S* g_sink;

// A copy made before the value is next modified survives to Color and is coalesced away: +2 on n in the block of
// the copy, one more candidate there (k), n's range ends at the copy. c2: the copy in block 1; c3: in block 3.
int cpl__base(S* p, int a)
{
    int n = g(a);
    int s = g(p->a);
    if (a > 2)
        s += g(p->b);
    while (n > 0)
    {
        s += g(n);
        n--;
    }
    return s + p->b;
}
int cpl__c2(S* p, int a) { int n = g(a); int s = g(p->a); int k = n; if (a > 2) s += g(p->b); while (k > 0) { s += g(k); k--; } return s + p->b; }
int cpl__c3(S* p, int a) { int n = g(a); int s = g(p->a); if (a > 2) { s += g(p->b); } int k = n; while (k > 0) { s += g(k); k--; } return s + p->b; }
// The same through an inline helper that walks its by-value parameter.
__forceinline int walk(S* q) { int t = 0; while (q) { t += g(q->a); q = q->next; } return t; }
int wk__base(S* p, int n) { int s = g(n); int t = 0; while (p) { t += g(p->a); p = p->next; } return s + t; }
int wk__helper(S* p, int n) { int s = g(n); return s + walk(p); }
// A read before an if / at the head of both arms: merged back after Color, so the references count in the arms.
int hdup__base(S* p, int n) { int s = g(n); int x = p->a; if (n > 3) s += g(x); else s -= g2(x, n); return s + p->b; }
int hdup__dup(S* p, int n) { int s = g(n); if (n > 3) { int x = p->a; s += g(x); } else { int x = p->a; s -= g2(x, n); } return s + p->b; }
// A narrow constant through the other signedness: one constant candidate fewer, same bytes (immcand.md).
void imm__base(C* p, int n) { p->i = -1; g(n); p->c = -1; }
void imm__resp(C* p, int n) { p->i = -1; g(n); *(unsigned char*)&p->c = 255; }
// Do-nothing uses leave nothing; a store is a reference with one instruction of code.
int tch__base(S* p, S* q, int n) { int s = g(n); s += p->a; s += g(q->b); return s + p->c + q->a; }
int tch__void(S* p, S* q, int n) { int s = g(n); s += p->a; (void)p; if (p) { } __assume(p != 0); s += g(q->b); return s + p->c + q->a; }
int tch__store(S* p, S* q, int n) { int s = g(n); s += p->a; g_sink = p; s += g(q->b); return s + p->c + q->a; }
// An address-taken local: callee declared / callee with a body that only reads (callee.md): one web across the call.
extern int cal__decl(const int* q);
__declspec(noinline) int cal__body(const int* q) { return *q + gi; }
int ext__base(S* p, int n) { int x = p->a; int s = cal__decl(&x); for (int i = 0; i < n; i++) s += g(i); return s + x; }
int ext__body(S* p, int n) { int x = p->a; int s = cal__body(&x); for (int i = 0; i < n; i++) s += g(i); return s + x; }
// Equal arms on a condition of a never-modified value (`this`): three blocks at Color, merged away after it. this
// gets one more reference in the split block (the test) and S's references move into the arms; no code.
struct RzL : S { int eqt__base(int n); int eqt__eq(int n); };
int RzL::eqt__base(int n)
{
    int s = 0;
    int k = g(n);
    S* q = next;
    while (q)
    {
        s += g(q->a);
        q = q->next;
    }
    s ^= k;
    return g(s) + b;
}
int RzL::eqt__eq(int n)
{
    int s = 0;
    int k = g(n);
    S* q = next;
    while (q)
    {
        s += g(q->a);
        q = q->next;
    }
    if ((int)this & 1) s ^= k; else s ^= k;
    return g(s) + b;
}
// A statement after an if/else written at the end of both arms: tail-merged after Color, the references count in the arms.
int tdup__base(S* p, int n) { int s = g(n); if (n > 3) s += g(1); else s -= g2(n, 2); s += p->a; return g(s) + p->b; }
int tdup__dup(S* p, int n) { int s = g(n); if (n > 3) { s += g(1); s += p->a; } else { s -= g2(n, 2); s += p->a; } return g(s) + p->b; }
// Two independent statements exchanged: no reference changes, a defOrd tie flips (and here an interference goes).
int swp__base(S* p, S* q, int n) { int s = g(n); int t = n * 3; s += p->a; t ^= q->b; return g2(s, t) + p->c + q->c; }
int swp__swap(S* p, S* q, int n) { int s = g(n); int t = n * 3; t ^= q->b; s += p->a; return g2(s, t) + p->c + q->c; }
