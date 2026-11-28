// A reference release (lock xadd of -1, then the count with a mask) inside a loop on one branch and once on the
// other, cl 17.00 x64. The -1 is loop-invariant and lives in one web (callee-saved) with the xadd's result when the
// chain has no named variable for the xadd's own result. docs/c2/port-1700.md 13 (probe tests/probes/xadd17.py).
#include <intrin.h>
struct Obj { virtual void f(); virtual void last(); int pad[4]; volatile long rc; };
void use(Obj *);
Obj *pick(Obj *);

#define LOOP(REL) \
    if (c) { \
        for (; b != e; ++b) { \
            Obj *o = *b; \
            use(o); \
            if (((_InterlockedExchangeAdd(&o->rc, -1) - 1) & 0x3ff) == 0) \
                o->last(); \
        } \
        return; \
    } \
    x = pick(x); \
    use(x); \
    REL \
    if ((n & 0x3ff) == 0) \
        x->last();

// the release's result used inline: the second release's -1 and xadd result share the hoisted register (ebp)
void rel_inline(Obj **b, Obj **e, Obj *x, int c) { unsigned n; LOOP(n = _InterlockedExchangeAdd(&x->rc, -1) - 1;) }
// the same through _InterlockedDecrement: ebp
void rel_decr(Obj **b, Obj **e, Obj *x, int c) { unsigned n; LOOP(n = _InterlockedDecrement(&x->rc);) }
// the xadd's result in a named variable, decremented afterwards: the second release takes a volatile register
void rel_named(Obj **b, Obj **e, Obj *x, int c)
{
    unsigned n;
    LOOP(long old = _InterlockedExchangeAdd(&x->rc, -1); n = old - 1;)
}
