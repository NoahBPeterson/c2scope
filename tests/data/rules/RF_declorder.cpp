// frame.md 6 ("Contradicted or refined", handlematch): is the order of hoisted local declarations an input?
// Same body, declarations permuted with /DORDER=1..3. Compare the code with the local names' numbers removed.
// press(): more live ints than registers (some spill, frame slots); slots(): address-taken locals of mixed size.
int ext(int);
void use(void*);
#ifndef ORDER
#define ORDER 1
#endif
#if ORDER == 1
#define DECLS int a, b, c, d, e, f, g, h;
#define DECLS2 int x; short s; char k[12]; int y;
#elif ORDER == 2
#define DECLS int h, g, f, e, d, c, b, a;
#define DECLS2 int y; char k[12]; short s; int x;
#else
#define DECLS int e, a, h, c, g, b, f, d;
#define DECLS2 char k[12]; int y; int x; short s;
#endif
int press(int n, int* p)
{
    DECLS
    a = ext(1); b = ext(a); c = ext(b); d = ext(c); e = ext(d); f = ext(e); g = ext(f); h = ext(g);
    for (int i = 0; i < n; i++) p[i] = a * p[i] + b * c - d + (e ^ f) + g * h;
    return a + b + c + d + e + f + g + h;
}
int slots(int n)
{
    DECLS2
    x = n; y = ext(n); s = (short)ext(y);
    use(&x); use(&s); use(k); use(&y);
    return x + y + s + k[3];
}
