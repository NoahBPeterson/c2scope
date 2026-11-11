// Regression data (from vs2005-cpp probe p10): inline-helper shapes in EH and non-EH functions.
#include <windows.h>
struct D { D(); ~D(); };
struct Obj { int pad; long refs; int a, b; };
struct Big { int m[13]; };
void use(int, int, int);
void take(const Big*);
__forceinline void incA(Obj* p) { InterlockedIncrement(&p->refs); }
__forceinline void incB(Obj* p) { long& r = p->refs; InterlockedIncrement(&r); }
__forceinline void tkA(const Big& t) { take(&t); }
struct H { Big* big; Obj* o; };
#define BODY(INC) D d; use(h->o->a, h->o->b, k); INC(h->o); use(h->o->b, k, h->o->a); tkA(*h->big); use(k, h->o->a, 1);
#define NBODY(INC) use(h->o->a, h->o->b, k); INC(h->o); use(h->o->b, k, h->o->a); tkA(*h->big); use(k, h->o->a, 1);
void eA(H* h, int k) { BODY(incA) }
void eB(H* h, int k) { BODY(incB) }
void nA(H* h, int k) { NBODY(incA) }
void nB(H* h, int k) { NBODY(incB) }
