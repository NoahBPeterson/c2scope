// invert.md 1: a frame realigned to 16 (a local of alignment 16, `and esp, -16`) is allocated in first-reference
// order, not by the name hash (f1, f2); without one the hash walk (f3).  tests/probes/invert.py.
void h(void*);
struct __declspec(align(16)) VC { float x[4]; VC(); };
struct __declspec(align(16)) V { float x[4]; };
// buckets of the names: aa 0, ad 1, a 2, ao 3, af 4, ab 5
void f1(void) { int aa; int ad; int a; int ao; VC ab; h(&ao); h(&a); h(&ad); h(&aa); }
void f2(void) { int aa; int ad; int a; int ao; V ab; h(&ao); h(&a); h(&ab); h(&ad); h(&aa); }
void f3(void) { int aa; int ad; int a; int ao; h(&ao); h(&a); h(&ad); h(&aa); }
