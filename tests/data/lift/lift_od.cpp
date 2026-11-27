// /Od shape of the lifter (il2c_od.py): each function below is compiled /Od, lifted back from its bytes and must
// recompile to the same bytes. No calls or globals: the object's relocations are not the lifter's input here.
struct S3 { int a, b, c; };
struct V { virtual int f1(); virtual int f2(); virtual int f3(); int x; };
struct T { int a; int b; int tf(int k); void tg(int k); };

int od_sum(int* p, int n) { int s = 0; for (int i = 0; i < n; i++) s += p[i]; return s; }
int od_while(int a, int b) { int c = 0; while (a != b) { a += 4; c++; } return c; }
void od_cp3(S3* d, S3* s) { *d = *s; }
int od_vcall(V** pp) { return (*pp)->f3(); }
int od_ptrwalk(int* p, int* e) { int n = 0; for (; p != e; p += 4) n = n + 1; return n; }
int T::tf(int k) { if (k & 1) a = b; return a; }
void T::tg(int k) { if (k > 3) { a = k; b = a + k; } else { b = 0; } }
int od_ifelse(int a, int b) { int r; if (a < b) r = a; else r = b * 2; return r; }

struct SG3 { float a, b, c; int n; };
void od_fcopy(SG3* d, SG3* s) { d->a = s->a; d->b = s->b; d->c = s->c; d->n = s->n; }
float od_fadd(float* p, float k) { *p = *p + k; return *p; }
