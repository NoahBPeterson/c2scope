/* stack-slot packing probes (docs/c2/slots.md; tests/run_tests.py slots_tests) */
void ext(void *);
int ext2(int);
int g;

int small(int n)                 /* T <= 128: slots keep creation order */
{
    int a, b, c;
    ext(&a); g = a;
    { int x; ext(&x); g += x; }
    ext(&b); ext(&c);
    return a + b + c;
}

int big(int n)                   /* T > 128: sorted by 1000*uses/size; param home undone (T >= 112) */
{
    struct { int v[40]; } s;
    int i, k = 0;
    ext(&s);
    ext(&i); ext(&i); ext(&i);
    for (i = 0; i < n; i++) k += s.v[i & 31];
    { double d; ext(&d); k += (int)d; }
    { char ch; ext(&ch); k += ch; }
    return k + i;
}

int home(int p)                  /* a dead cdecl param read by an add: its home holds x */
{
    int x;
    ext2(p + 1);
    ext(&x);
    return x;
}

int share(int n)                 /* disjoint block locals share one slot; the newest colour is tried first */
{
    { int a; ext(&a); g += a; }
    { int b; ext(&b); g += b; }
    { short s; ext(&s); g += s; }
    { char c; ext(&c); g += c; }
    return g;
}

int gsbuf(int n)                 /* /GS buffer: sorted, no param home */
{
    char buf[16];
    int a;
    ext(buf); ext(&a);
    return buf[n] + a;
}

int gshome(int p)                /* /GS: 13.10 still lets an int use p's home, 14.00 and 15.00 do not */
{
    char buf[16];
    int x;
    ext2(p + 1);
    ext(buf);
    ext(&x);
    return x + buf[1];
}
