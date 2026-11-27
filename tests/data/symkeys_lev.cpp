// symkeys: levers on an inline helper's body (one piece of source, expanded three times), key selectors, labels
struct D { float x, y, z; };
inline float sq2(float a, float b) { float s = a + b; return s * s + s * a; }
float kh(float x, float y, float z) { return sq2(x, y) * sq2(y, z) - sq2(z, x); }
float kd(D* d, float k) { return d->x * k + d->y * d->x + d->z * d->x; }
struct Q { float x, y, z, w; };
float ext(float*);
float kv(float s, float t)
{
    Q dir;
    dir.x = s; dir.y = t; dir.z = s * t; dir.w = ext(&dir.x);
    Q d2 = dir;
    return d2.x * dir.y + dir.x * d2.y + d2.x * d2.z;
}
