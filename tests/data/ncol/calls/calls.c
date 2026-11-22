/* Integer functions calling static helpers of the same unit (ncolor.md 12: a callee compiled before the caller
 * clobbers only the registers it uses: interprocedural register usage, the call's REGSET leaf). */
extern int ext1(int);
extern void ext2(int *, int);

static __declspec(noinline) int h_add(int a, int b) { return a + b; }
static __declspec(noinline) int h_mul3(int a, int b, int c) { return a * b * c; }
static __declspec(noinline) void h_store(int *p, int v) { p[0] = v; p[1] = v + 1; }
static __declspec(noinline) int h_loop(const int *p, int n)
{
    int s = 0, i;
    for (i = 0; i < n; i++) s += p[i] ^ (i << 1);
    return s;
}
static __declspec(noinline) int h_ext(int a) { return ext1(a) + 1; }
static __declspec(noinline) long long h_wide(long long a, long long b, int k) { return (a << k) | (b >> (k & 7)); }

int c_two(int x, int y, int z)
{
    int a = h_add(x, y);
    int b = h_add(a, z);
    int c = h_mul3(a, b, x);
    return a + b + c + y;
}

int c_keep(int *p, int n, int k)
{
    int t = n * 3 + k, u = k ^ n, v = n - k;
    h_store(p, t);
    h_store(p + 2, u);
    return t + u + v + h_loop(p, n);
}

int c_loop(int *p, int n)
{
    int i, s = 0, m = n >> 1;
    for (i = 0; i < n; i++) {
        s += h_add(p[i], i);
        if (s > m) s -= h_mul3(s, i, m);
    }
    return s + m;
}

int c_mixed(int *p, int n)
{
    int a = n + 7, b = n * 5;
    int r = h_ext(a);
    ext2(p, b);
    r += h_add(a, b);
    r += ext1(r);
    return r + a + b;
}

long long c_wide(long long a, long long b, int k)
{
    long long x = h_wide(a, b, k), y = h_wide(b, a, k + 1);
    long long z = x ^ y;
    return z + h_wide(z, x, 3) + y + a;
}

int c_many(int a, int b, int c, int d)
{
    int e = a + b, f = c + d, g = a - d, h = b - c;
    int r = h_mul3(e, f, g);
    r += h_add(h, r);
    r ^= h_mul3(r, e, h);
    return r + e + f + g + h;
}
