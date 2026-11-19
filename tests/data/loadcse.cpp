// tests for tools/loadcse.py and docs/c2/loadcse.md (one function per rule)
struct E { void d(); };
void del(void*);
void h(int);
struct V { E** b; E** e; bool empty() const { return b == e; } E*& back() { return *(e - 1); } void pop_back() { --e; } };
struct S { char pad[0x15c]; E** b; E** e; V& v() { return *(V*)&b; } void drain0(); void drain1(); };
// RMW not formed: the end pointer is one value for the read and the decrement (M1/M2)
void S::drain0() { while (b != e) { E* x = e[-1]; --e; if (x) { x->d(); del(x); } } }
// RMW formed: each inline call on the sub-object has its own `this`, so the reads are separate (R3)
void S::drain1() { while (!v().empty()) { E* x = v().back(); v().pop_back(); if (x) { x->d(); del(x); } } }
struct P { int a; struct Q* q; };
struct Q { int m; };
// R2: the arm stores through p->q: its reads get another alias class than the test's
void twolevel(P* p) { if (!(p->q->m & 3)) p->q->m = (p->q->m & ~1) | 2; }
// the same with a local pointer: one load
void twolevel_local(P* p) { Q* q = p->q; if (!(q->m & 3)) q->m = (q->m & ~1) | 2; }
struct W { int* b; int* e; };
void g2(int*, int*);
// R3: a reference to the member, used twice, is not copy-propagated
int refmember(W* p, const W& x) { int* const& re = x.e; unsigned n = x.e - x.b; if (n > (unsigned)(p->e - p->b)) g2(x.b, re); else g2(re, p->b); return 1; }
// R5: a call between
int killcall(Q* p, int c) { int t = p->m & 255; h(0); if (c) t |= p->m << 4; return t; }
// same chain of one operator: RMW (`or [m], ..`) on 14.00/15.00; 13.10 splits it again after allocation
void samechain(Q* p, int a) { p->m = (p->m | a) | 2; }
