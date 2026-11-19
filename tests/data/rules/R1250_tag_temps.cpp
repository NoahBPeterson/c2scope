// R1250 (f3decomp): two empty tag temporaries passed by value are two byte stores and two loads; one tag object passed
// twice is one load (14.00/15.00: plus a register copy; 13.10: the same register pushed twice). An inline function
// returning the first tag (two) gives the same code as a plain temporary (twotmp). All builds.
struct tagA {}; struct tagB {};
void callee(int* f, int* l, int* d, tagA, tagB);
void callee2(int* f, int* l, int* d, tagA, tagA);
inline tagA cat(int*, int*) { return tagA(); }
void two(int* f, int* l, int* d) { callee(f, l, d, cat(f, d), tagB()); }
void twotmp(int* f, int* l, int* d) { callee(f, l, d, tagA(), tagB()); }
void once(int* f, int* l, int* d) { tagA t; callee2(f, l, d, t, t); }
void twosame(int* f, int* l, int* d) { callee2(f, l, d, tagA(), tagA()); }
