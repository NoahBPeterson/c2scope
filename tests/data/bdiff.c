/* tsweep.bdiff: a compile's listing against the linked image of the same object must score 0 (relocated globals,
   string literals, float constants, call targets, a jump table, frame variables, OFFSET immediates). */
int g_tab[16];
int g_n;
float g_f;
extern int ext(int, const char *);
typedef int (*fp_t)(int, const char *);
fp_t g_fp;

int sw(int k, int *p)
{
    switch (k) {
    case 0: return p[3] + g_tab[k];
    case 1: return ext(k, "one");
    case 2: return p[100] * 7;
    case 3: g_n = -1; return 0;
    case 4: return (signed char)p[2] & 0x7f;
    case 5: return g_tab[p[1] & 15];
    }
    return -16;
}

float fsum(float *v, int n)
{
    float s = 0.0f, t[4];
    int i;
    for (i = 0; i < 4; i++)
        t[i] = v[i] * 1.5f;
    for (i = 0; i < n; i++)
        s += v[i] * g_f + t[i & 3];
    return s * 0.25f;
}

int big(int a)
{
    char buf[64];
    int i;
    g_fp = ext;
    for (i = 0; i < 64; i++)
        buf[i] = (char)(a + i);
    return ext(buf[a & 63], buf) + g_fp(a, "x") + 0x12345;
}
