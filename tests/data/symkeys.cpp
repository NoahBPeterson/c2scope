// symkeys.py regression (docs/c2/symkeys.md): key assignment rules and the replay / what-if model
struct V { float x, y, z; };
struct S { float m, n; float meth(float a, float b) { float r = a * m; return r * b + n; } };
inline float dot(const V& a, const V& b) { float t = a.x * b.x; return t + a.y * b.y + a.z * b.z; }
inline float dA(const V& a, const V& b) { float tA = a.x * b.x; return tA + a.y * b.y; }
inline float dB(const V& a, const V& b) { float tB = a.x * b.y; return tB + a.y * b.x; }
float ext2(float, float);
float kparams(float a, float b, float c) { float q; float r = a * b; q = r + c; return q * b; }
float kdead(float a, float b) { float u; float d = 0; float r = a * b; return r * a; }
float kthis(S* s, float a, float b) { return s->meth(a, b); }
float kinl(V* a, V* b, float s) { float r = dot(*a, *b) * s; float q = dot(*b, *a) + s; return r * q; }
float kargs(V* a, V* b) { return ext2(dA(*a, *b), dB(*a, *b)); }
float kx87(V* p, V* q, float s) { float u = p->x * s; float w = q->y * s; return u * w + p->y * q->x; }
