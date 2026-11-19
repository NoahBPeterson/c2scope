/* c2trace.py regs: the "not in the table" lines (locals and parameters with no globreg row, and why) */
int g(int);
int h(int *);
struct S { int a, b, c, d; };
int f(int a, int b, int n)
{
    volatile int v = a;     /* volatile: never a candidate */
    int copy = a;           /* a copy GlobalOptimize propagates away */
    int x = 0;              /* address-taken */
    int i, sum = 0;
    struct S s;             /* an aggregate */
    s.a = a; s.b = b; s.c = g(a); s.d = n;
    h(&x);
    h(&s.a);
    for (i = 0; i < n; i++)
        sum += g(i + copy);
    return sum + v + x + s.c;
}
