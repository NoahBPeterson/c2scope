// symkeys: the optimiser's value-numbering temps (hash class, symkeys.md section 8).  Each hN is hb plus one
// code-neutral construct; the test counts hash temps made in HshBuildTable's walk / its assignment tail / later.
extern "C" double __cdecl fabs(double);
struct P { float x, y, z; float Dot(const P& o) const { return x * o.x + y * o.y + z * o.z; } };
float hb(const float* e, const P& a, const P& b, float m, float* out)
{
    float d = a.Dot(b);
    float r = (e[0] * (float)fabs(a.x)) + (e[1] * (float)fabs(a.y));
    if (d > r) { if (d + (m * d) > r) return 0; *out = (r - d) / m; return 1; }
    *out = -(r + d) / m;
    return 2;
}
float hparen1(const float* e, const P& a, const P& b, float m, float* out)     // one more pair of parentheses
{
    float d = a.Dot(b);
    float r = ((e[0] * (float)fabs(a.x)) + (e[1] * (float)fabs(a.y)));
    if (d > r) { if (d + (m * d) > r) return 0; *out = (r - d) / m; return 1; }
    *out = -(r + d) / m;
    return 2;
}
float hparen3(const float* e, const P& a, const P& b, float m, float* out)     // three more
{
    float d = a.Dot(b);
    float r = ((((e[0] * (float)fabs(a.x)) + (e[1] * (float)fabs(a.y)))));
    if (d > r) { if (d + (m * d) > r) return 0; *out = (r - d) / m; return 1; }
    *out = -(r + d) / m;
    return 2;
}
float hnoparen(const float* e, const P& a, const P& b, float m, float* out)    // two pairs fewer
{
    float d = a.Dot(b);
    float r = e[0] * (float)fabs(a.x) + e[1] * (float)fabs(a.y);
    if (d > r) { if (d + (m * d) > r) return 0; *out = (r - d) / m; return 1; }
    *out = -(r + d) / m;
    return 2;
}
float hleaf(const float* e, const P& a, const P& b, float m, float* out)       // parentheses around variables
{
    float d = a.Dot(b);
    float r = ((e[0]) * (float)fabs(a.x)) + ((e[1]) * (float)fabs(a.y));
    if ((d) > (r)) { if ((d) + (m * d) > (r)) return 0; *out = ((r) - (d)) / (m); return 1; }
    *out = -((r) + (d)) / (m);
    return 2;
}
float hcast(const float* e, const P& a, const P& b, float m, float* out)       // (float) of a float sum
{
    float d = a.Dot(b);
    float r = (float)((e[0] * (float)fabs(a.x)) + (e[1] * (float)fabs(a.y)));
    if (d > r) { if (d + (m * d) > r) return 0; *out = (r - d) / m; return 1; }
    *out = -(r + d) / m;
    return 2;
}
float hflag(const float* e, const P& a, const P& b, float m, float* out)       // bool flag for the compare
{
    float d = a.Dot(b);
    float r = (e[0] * (float)fabs(a.x)) + (e[1] * (float)fabs(a.y));
    bool hit = d > r;
    if (hit) { if (d + (m * d) > r) return 0; *out = (r - d) / m; return 1; }
    *out = -(r + d) / m;
    return 2;
}
float hdbl(const float* e, const P& a, const P& b, float m, float* out)        // the sum through a double local
{
    float d = a.Dot(b);
    double rr = (e[0] * (float)fabs(a.x)) + (e[1] * (float)fabs(a.y));
    float r = (float)rr;
    if (d > r) { if (d + (m * d) > r) return 0; *out = (r - d) / m; return 1; }
    *out = -(r + d) / m;
    return 2;
}
