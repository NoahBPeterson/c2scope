// RA_callee: does a visible same-TU callee's register use shrink the call's kill set (regalloc.md 3.3 item 2,
// 8 "Callee visibility")? Plain /O2 compile, no /GL. The callee is defined before the caller and uses only eax;
// x lives across the call. If the kill set were the callee's, x could stay in ecx or edx; with the full kill set
// it takes esi.
// /DAFTER: the same callee defined after the caller (c2 compiles bottom-up, so it is still seen).
// /DEXT: the callee is only declared (the kill set is eax ecx edx). /DSTATIC: the callee is static (private
// convention possible, ltcg.md 2.5).
#if _MSC_VER < 1300
#define NOINLINE
#else
#define NOINLINE __declspec(noinline)
#endif
#if defined(EXT) || defined(AFTER)
int leaf(int *p);
#elif defined(STATIC)
static NOINLINE int leaf(int *p) { return *p + 1; }
#else
NOINLINE int leaf(int *p) { return *p + 1; }
#endif

int caller(int *p, int *q)
{
    int x = *q * 3;
    int r = leaf(p);
    return r + x * x;
}

#ifdef AFTER
NOINLINE int leaf(int *p) { return *p + 1; }
#endif
