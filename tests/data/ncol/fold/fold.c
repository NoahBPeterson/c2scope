/* ncolsim fold fixtures (docs/c2/ncolor.md 12.5): the folds NColor makes while allocating and before it.
   cl 17.00 x64 /O2; exported with c2ncol.py --export --il (C2TRACE_ILRAW=16). */
struct S { int a, b, c; int t[64]; unsigned short h[32]; };
extern int g(int);
extern int sink(void *);

/* loads folded into their one use (add / cmp / movzx), and one kept: two uses */
int loads(struct S *s, int x)
{
    int r = x + s->a, k;
    if (s->b > r)
        r -= s->c;
    r += s->h[3];
    k = s->t[5];
    return g(r) + k * k;
}

/* an address in two registers used as an address once (folded: n = 2) and inside a loop deeper than its definition
   (its index must be extended to the loop: kept when it cannot be) */
int lea2(struct S *s, int i, int n)
{
    int *p = &s->t[i];
    int r = *p, j;
    unsigned short *q = &s->h[i & 31];
    for (j = 0; j < n; j++)
        r += q[0] + g(j);
    return r;
}

/* many stack array accesses: NColCanon's frame score passes 6 * 24 and rbp is reserved as a frame base */
int frame(const unsigned short *len, int n)
{
    unsigned short count[16], offs[16];
    int i, max = 0;
    for (i = 0; i < 16; i++) count[i] = 0;
    for (i = 0; i < n; i++) count[len[i] & 15]++;
    for (i = 15; i > 0; i--) if (count[i]) { max = i; break; }
    offs[1] = 0;
    for (i = 1; i < 15; i++) offs[i + 1] = offs[i] + count[i];
    for (i = 0; i < n; i++) if (len[i]) count[offs[len[i] & 15]++ & 15] = (unsigned short)i;
    return max + sink(count) + sink(offs) + count[max] + offs[max] + count[n & 15] + offs[n & 15];
}

/* a float leaf the optimizer removes: FUNC+0xe4 & 0x80 is set (NColor's IL has no float) */
extern float g_f;
int addr_fp(struct S *s, int i)
{
    float dead = g_f * 2.0f;
    int r;
    r = s->t[i] + g(0);
    (void)dead;
    return r + s->t[i + 1];
}
