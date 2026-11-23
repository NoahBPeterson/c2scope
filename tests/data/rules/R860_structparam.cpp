// R860 (f3decomp): a by-value struct parameter never becomes a register parameter of a static helper's private
// convention; the same body with a scalar parameter does. docs/c2/ltcg.md section 2.2, rules_xref.md.
// /DSCALAR: n is a const char* (expected: n arrives in a register, "; _n$ = reg"); default: a one-pointer struct.
// /DENTRY: n is first used in the entry block (before the loop), so its load is at the top either way.
struct NameArg { const char* s; };
int cmp(const char*, const char*);
extern const char* tab[64];
#ifdef SCALAR
typedef const char* NA;
#define S(n) n
#else
typedef NameArg NA;
#define S(n) n.s
#endif
#if _MSC_VER < 1300
#define NOINLINE
#else
#define NOINLINE __declspec(noinline)
#endif
static NOINLINE int find(NA n, int x)
{
#ifdef ENTRY
    if (*S(n) == 0) return -2;
#endif
    for (int i = 0; i < x; i++)
        if (cmp(S(n), tab[i]) == 0) return i;
    return -1;
}
int user(const char* s, int x) { NA n; S(n) = s; return find(n, x) + 1; }
