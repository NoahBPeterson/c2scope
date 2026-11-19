// R1320 (f3decomp): members built in an inlined constructor that contains a call (even folded: n = 0) get EH states:
// f1 `mov [state], 1` and two unwind funclets; f2 (no call) `mov [state], eax` (0) and one. State stores whose
// old and new values share the upper bytes are byte stores (f3). All builds.
void use(void*);
struct Arr  { int* d; Arr(unsigned n = 0) : d(0) { if (n) Grow(n); } void Grow(unsigned); ~Arr(); };
struct ArrT { int* d; ArrT() : d(0) {} ~ArrT(); };
struct Pair  { Arr a, b;  Pair() {}  ~Pair(); };
struct PairT { ArrT a, b; PairT() {} ~PairT(); };
void f1() { Pair p; use(&p); }
void f2() { PairT p; use(&p); }
void f3() { Arr a; Arr b; use(&a); use(&b); }
void f4() { Pair p; use(&p); use(0); }
