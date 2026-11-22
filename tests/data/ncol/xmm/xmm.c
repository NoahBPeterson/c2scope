/* Float-heavy functions for the xmm availability banks (ncolor.md 12): xmm parameters and returns, extern calls
 * (xmm0..5 volatile, xmm6..15 callee-saved), static callees of the same unit, conversions, loops. */
extern float xf(float);
extern double xd(double, double);
extern void xv(float *);

static __declspec(noinline) float s_sq(float a) { return a * a; }
static __declspec(noinline) double s_lin(double a, double b, double t) { return a + (b - a) * t; }
static __declspec(noinline) float s_sum(const float *p, int n)
{
    float s = 0;
    int i;
    for (i = 0; i < n; i++) s += p[i];
    return s;
}

float f_keep(float a, float b, float c)
{
    float s = a * b + c, t = a - c, u = b * c;
    float r = xf(s);
    r += xf(t) * u;
    return r + s + t + u;
}

float f_static(float a, float b)
{
    float x = s_sq(a), y = s_sq(b);
    float z = s_sq(x + y);
    return x * y + z - a;
}

double f_lin(double a, double b, int n)
{
    double acc = 0, t = 1.0 / n;
    int i;
    for (i = 0; i < n; i++) acc += s_lin(a, b, t * i) * t;
    return acc + a * b;
}

double f_ext(double a, double b, double c)
{
    double p = a * b, q = b * c, r = a * c;
    double s = xd(p, q);
    s += xd(r, s) * p;
    return s + q * r;
}

float f_arr(float *v, int n, float k)
{
    float m = k * 0.5f, s;
    xv(v);
    s = s_sum(v, n) * m;
    xv(v);
    return s + s_sum(v, n) * k + m;
}

float f_mix(const float *p, const int *q, int n, float w)
{
    float acc = 0, prev = 0;
    int i, cnt = 0;
    for (i = 0; i < n; i++) {
        float d = p[i] * w - prev;
        if (d > 0) { acc += d; cnt += q[i]; }
        prev = d;
    }
    return acc / (cnt ? cnt : 1) + prev;
}

double f_conv(int a, float b, double c, long long d)
{
    double x = a * c, y = b + (double)d;
    float z = (float)(x - y);
    double r = xd(x, y);
    return r + z * c + (double)(a + d);
}

float f_many(float a, float b, float c, float d)
{
    float e = a + b, f = c + d, g = a * d, h = b * c, i = e * f, j = g - h;
    float r = xf(e);
    r += xf(f) + i;
    r *= xf(g) + j;
    return r + e + f + g + h + i + j;
}
