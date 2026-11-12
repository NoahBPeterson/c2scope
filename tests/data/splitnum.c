/* GLOBREG record numbers and the reload order after a loop (docs/c2/regalloc.md 3.6.9).  f4 and f6 compile to
   the same instructions; f6 only mentions y once before x is assigned (a dead read), which gives y the lower
   record number, so after the loop y is reloaded first (and takes x's register).  Same on 13.10, 14.00, 15.00. */
int h(int);
int f4(int *a, int n)
{
    int x, y, z, w;
    int i, s = 0;
    x = a[0] + 1; y = a[1] + 2; z = a[2] + 3; w = a[3] + 4;
    for (i = 0; i < n; i++) {
        int p = a[i] * 3, q = a[i + 1] * 5, r = a[i + 2] * 7, t = a[i + 3] * 9;
        s += h(p + q) ^ (p * q) ^ (r * t) ^ (r + t);
    }
    return x * y + z * w + s;
}
int f6(int *a, int n)
{
    int x, y, z, w;
    int i, s = 0;
    (void)y; x = a[0] + 1; y = a[1] + 2; z = a[2] + 3; w = a[3] + 4;
    for (i = 0; i < n; i++) {
        int p = a[i] * 3, q = a[i + 1] * 5, r = a[i + 2] * 7, t = a[i + 3] * 9;
        s += h(p + q) ^ (p * q) ^ (r * t) ^ (r + t);
    }
    return x * y + z * w + s;
}
