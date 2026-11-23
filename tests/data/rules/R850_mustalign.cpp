// R850 (f3decomp): one function with a 16-byte-aligned local anywhere in the unit takes ebx away from every
// static helper's register parameters (_FHasMustAlign). docs/c2/ltcg.md section 2.2, rules_xref.md.
// helper's entry block loads m into ebx; with ebx allowed, m arrives in ebx (listing: "; _m$ = ebx").
// Variants (/D): none = the aligned type only declared; BEFORE / AFTER = an aligned-local function before / after
// the helper; CALLER = the aligned-local function is the helper's caller (13.10's per-call-graph rule).
// /arch:SSE2 without any variant: 14.00 and 15.00 set the flag for the whole unit too (CgBuildCallGraph).
struct __declspec(align(16)) A16 { float x, y, z, w; };
#ifdef BEFORE
float UseA16(float f) { A16 a; a.x = f; a.y = f; return a.x + a.y; }
#endif
extern int g[100];
void ext(int);
#if _MSC_VER < 1300
#define NOINLINE
#else
#define NOINLINE __declspec(noinline)
#endif
static NOINLINE int helper(int* p, int n, int k, int m)
{
    int s = 0;
    for (int i = 0; i < n; i++) { s += p[i] * k; ext(m); s ^= m; }
    return s + k + m;
}
#ifdef CALLER
float UseA16(int* p, int n) { A16 a; a.x = (float)n; a.y = (float)g[3]; return a.x + a.y + helper(p, n, g[1], g[2]); }
#else
int caller(int* p, int n) { return helper(p, n, g[1], g[2]) + 1; }
#endif
#ifdef AFTER
float UseA16(float f) { A16 a; a.x = f; a.y = f; return a.x + a.y; }
#endif
