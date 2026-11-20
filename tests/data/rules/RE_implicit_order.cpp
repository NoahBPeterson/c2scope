// frontend_il.md 4.11: order of the implicit members of one class in the object file. Claim: constructor,
// destructor, copy constructor, operator=, whatever the order of first use. Here the first use is operator=,
// then the copy constructor, then the default constructor (and the destructor). /DREV uses them in another order.
// Also 4.13: a global with a destructor defined first still has its ??__E / ??__F at the end.
struct M { M(); M(const M&); ~M(); M& operator=(const M&); int v; };
struct Imp { M m; int k; };
struct G { G(); ~G(); };
G gfirst;
void before(int* p) { *p = 1; }
#ifndef REV
void u(Imp& a, Imp& b) { a = b; Imp c(b); Imp d; d.k = c.k; a.k = d.k; }
#else
void u(Imp& a, Imp& b) { Imp d; Imp c(b); a = b; d.k = c.k; a.k = d.k; }
#endif
void after(int* p) { *p = 2; }
