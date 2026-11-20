// frontend_il.md 3A rule 33 / open questions: a function-local static with a dynamic initialiser is guarded by a
// plain bit test and set, with no lock, no interlocked instruction and no helper call (no thread-safe statics).
int h(int);
struct K { K(int); ~K(); int v; };
int f1(int a) { static int s = h(a); return s; }
int f2(int a) { static K k(a); return k.v; }
