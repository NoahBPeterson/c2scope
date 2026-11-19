// x87 stack decisions after Color on cl 14.00 / 15.00 (docs/c2/x87.md section 12; tools/c2x87.py, rasim x87stack.rs)
double g[4];
void ext(void);
// callees with bodies in this unit (generated before their callers: code generation is bottom-up)
__declspec(noinline) int geti(int *p) { return p[1] * 3 + p[2]; }        // pushes no x87 value: x87-free
__declspec(noinline) float getf(float *p) { return p[1]; }               // one push, returns float: x87-free
__declspec(noinline) float getf2(float *p) { return p[1] + p[2]; }       // computed float result: rounded (2 pushes)
__declspec(noinline) void voidfp(double *p) { p[0] = p[1]; }             // one push, returns void: kills
struct S { float a; __declspec(noinline) float gin() const { return a; } };   // in-class: generated after its callers
double k_ext(double a, double b) { double x = a * b; g[0] = x; ext(); return x + a; }
double k_geti(double a, double b, int *p) { double x = a * b; g[0] = x; g[1] = geti(p); return x + a; }
double k_getf(double a, double b, float *p) { double x = a * b; g[0] = x; return x + a * getf(p); }
double k_getf2(double a, double b, float *p) { double x = a * b; g[0] = x; return x + a * getf2(p); }
double k_voidfp(double a, double b, double *p) { double x = a * b; g[0] = x; voidfp(p); return x + a; }
double k_gin(double a, double b, S *p) { double x = a * b; g[0] = x; return x + a * p->gin(); }
// the constant 0.0 kept across a loop: it ties with the copied matrix's single-use elements and, with defOrd 0,
// loses every tie; one more reference where its range starts (m.t.w = 0 before the loop) wins it the slot
struct V { float x, y, z, w; };
struct T { V c0, c1, c2, t; };
__forceinline void xf(V &o, const T &m, const V &v) {
    float a = v.x, b = v.y, c = v.z;
    o.x = m.c0.x * a + m.c1.x * b + m.c2.x * c + m.t.x;
    o.y = m.c0.y * a + m.c1.y * b + m.c2.y * c + m.t.y;
    o.z = m.c0.z * a + m.c1.z * b + m.c2.z * c + m.t.z;
    o.w = 0.0f;
}
void z_tie(V *out, const V *in, const T &t0, int n) { T m = t0; int i = n - 1; do { xf(out[i], m, in[i]); } while (--i >= 0); }
void z_ref(V *out, const V *in, const T &t0, int n) { T m = t0; m.t.w = 0.0f; int i = n - 1; do { xf(out[i], m, in[i]); } while (--i >= 0); }
// a value live into a loop on one path only: fldz on the other (AppendFldZeroToBlock / airlock)
float f_join(const float *p, int n, int k) { float s; if (k) s = p[0] * 2.5f; float t = 0; for (int i = 0; i < n; i++) t += p[i] * s; return t; }
// a float variable assigned from an expression is rounded through its home (fstp _s; fld _s); a double is not
float r_float(float a, float b, float c) { float s = a * b; return s < c ? s : c; }
float r_double(float a, float b, float c) { double s = a * b; return s < c ? (float)s : c; }
