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
int od_elif(int* p, int a, int b) { int r = 0; if (a < b) { r = a; p[0] = r; } else if (a > b) { for (int i = 0; i < a; i++) r += p[i]; } else { r = b * 2; p[1] = r; } return r + 1; }
struct BV3 { float x, y, z; BV3(float a, float b, float c) { x = a; y = b; z = c; } BV3(const BV3& o); };
struct VB { virtual int f(BV3 v, float w); };
struct TB { VB* p; int bvinplace(float a, float b, float c, float w); };
int TB::bvinplace(float a, float b, float c, float w) { int r = p->f(BV3(a, b, c), w); return r + 1; }
struct BS2 { int w[2]; bool test(unsigned i) const { if (i < 60) return (w[i >> 5] & (1 << (i % 32))) != 0; return false; } };
int od_bitk(BS2* b, int* p, int n) { int c = 0; for (int i = 0; i < n; i++) { if (b->test(31)) { c = p[i]; break; } } return c; }
struct VF { virtual float g(); };
float od_ftail(VF** v, int n) { for (int i = 0; i < n; i++) { if (v[i]) return v[i]->g(); } return 1.0f; }
int od_sw(int* p, int n) { int f = 0; switch (p[2]) { case 1203833171: case -1712771323: case 1925484929: case 7: f = 1; } if (f) n = n + 1; return n; }
int od_andelse(int* p, int a) { int r; if (a != p[1] && p[0] < a) r = p[2]; else r = p[3]; return r + a; }
struct BFS { unsigned a : 8, b : 8, c : 8, d : 8; void setc(unsigned x) { c = x; } };
void od_bfset(BFS* s, int n) { BFS t = *s; t.setc(97); if (n) t.setc(98); *s = t; }
