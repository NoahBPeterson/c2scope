// Float-heavy functions for the x64 allocator model (tools/ncolsim.py): xmm candidates, calls that clobber
// xmm0..5, xmm6+ callee-saved, float/int mixes.
#include <math.h>

struct V3 { float x, y, z; };
struct M3 { V3 r[3]; };

float dot(const V3 &a, const V3 &b) { return a.x * b.x + a.y * b.y + a.z * b.z; }

V3 cross(const V3 &a, const V3 &b)
{
    V3 c = { a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x };
    return c;
}

float len(const V3 &a) { return sqrtf(dot(a, a)); }

V3 norm(const V3 &a)
{
    float l = len(a);
    V3 r = a;
    if (l > 1e-6f) { float k = 1.0f / l; r.x *= k; r.y *= k; r.z *= k; }
    return r;
}

V3 mul(const M3 &m, const V3 &v)
{
    V3 r = { dot(m.r[0], v), dot(m.r[1], v), dot(m.r[2], v) };
    return r;
}

float lerp(float a, float b, float t) { return a + (b - a) * t; }

float smooth(const float *p, int n, float w)
{
    float acc = 0, prev = p[0];
    for (int i = 1; i < n; i++) {
        float d = p[i] - prev;
        acc += d * d * w + fabsf(d);
        prev = lerp(prev, p[i], w);
    }
    return acc / (n > 1 ? n - 1 : 1);
}

double poly(double x, const double *c, int n)
{
    double r = 0;
    for (int i = n - 1; i >= 0; i--) r = r * x + c[i];
    return r;
}

extern float ext(float, float);

float callmix(float a, float b, int k)
{
    float s = a * b;
    float t = ext(a, s);
    float u = ext(b, t) + s;
    return k > 0 ? u * (float)k : u - t;
}

int classify(const V3 &p, const V3 *planes, const float *d, int n)
{
    int mask = 0;
    for (int i = 0; i < n; i++) {
        float e = dot(p, planes[i]) - d[i];
        if (e > 0.001f) mask |= 1 << i;
        else if (e < -0.001f) mask |= 1 << (i + 16);
    }
    return mask;
}

void integrate(V3 *pos, V3 *vel, const V3 *acc, int n, float dt, float damp)
{
    for (int i = 0; i < n; i++) {
        vel[i].x = (vel[i].x + acc[i].x * dt) * damp;
        vel[i].y = (vel[i].y + acc[i].y * dt) * damp;
        vel[i].z = (vel[i].z + acc[i].z * dt) * damp;
        pos[i].x += vel[i].x * dt;
        pos[i].y += vel[i].y * dt;
        pos[i].z += vel[i].z * dt;
    }
}

float spring(V3 &x, V3 &v, const V3 &anchor, float k, float c, float dt)
{
    V3 d = { x.x - anchor.x, x.y - anchor.y, x.z - anchor.z };
    float l = len(d);
    float f = -k * (l - 1.0f) - c * dot(v, d) / (l + 1e-5f);
    V3 n = norm(d);
    v.x += n.x * f * dt; v.y += n.y * f * dt; v.z += n.z * f * dt;
    x.x += v.x * dt; x.y += v.y * dt; x.z += v.z * dt;
    return f;
}

double quad(double a, double b, double c, double *r0, double *r1)
{
    double disc = b * b - 4 * a * c;
    if (disc < 0) return disc;
    double s = sqrt(disc);
    double q = b < 0 ? -0.5 * (b - s) : -0.5 * (b + s);
    *r0 = q / a;
    *r1 = c / q;
    return disc;
}
