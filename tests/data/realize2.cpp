// Regression probes for the slot, scratch-pick and key constructs of tools/realize.py (docs/c2/realize.md 2.5, 2.6,
// 2.8): X__base against X__<variant>, one compile per model.
//   uv run tools/realize.py pairs tests/data/realize2.cpp --slots --scratch --keys -- /TP /MT /EHsc /Gy /O2
//   uv run tools/realize.py edit tests/data/realize2.cpp --func pt__base ins:3 -- /TP /MT /EHsc /Gy /O2
struct S { int a, b, c; S* next; };
extern void __stdcall ext(void*);
extern int __stdcall ext2(int);
extern int __stdcall g(int);
extern int __stdcall g2(int, int);
extern int gi, gj, gk;
// slots: a test of the stack local a in equal arms is one more use of a for the packer and no code on 14.00 / 15.00
// (13.10 keeps the memory test); a statement that reads a written in equal arms on a register value: a counted
// twice, no code on 13.10 (14.00 / 15.00 select the add differently here)
int sc__base(int n)
{
    int a, b;
    int s = ext2(n);
    ext(&a);
    s += a;
    ext(&b);
    s ^= b;
    s = ext2(s);
    return s;
}
int sc__eqa(int n)
{
    int a, b;
    int s = ext2(n);
    ext(&a);
    s += a;
    ext(&b);
    s ^= b;
    if ((int)a & 1) s = ext2(s); else s = ext2(s);
    return s;
}
int sp__base(int n)
{
    int a, b;
    ext(&a);
    ext(&b);
    int s = ext2(n);
    s += a;
    s ^= b;
    return ext2(s);
}
int sp__eqb(int n)
{
    int a, b;
    ext(&a);
    ext(&b);
    int s = ext2(n);
    if ((int)s & 1) s += a; else s += a;
    s ^= b;
    return ext2(s);
}
// picks: the statement after an if written at the end of both arms: the copies get the same registers and are
// tail-merged after RegSimpleAssignBody, the second copy's pick is hidden (ins:3), later picks rotate one step
int pt__base(S* p, S* q, int n)
{
    int s = g(n);
    if (s > 3) { gj = q->b; s++; } else { gj = q->a; gk = q->c; s--; }
    gi = p->a;
    return g2(s, p->c) + q->a;
}
int pt__dup(S* p, S* q, int n)
{
    int s = g(n);
    if (s > 3) { gj = q->b; s++; gi = p->a; } else { gj = q->a; gk = q->c; s--; gi = p->a; }
    return g2(s, p->c) + q->a;
}
// keys: an initialised declaration two statements earlier moves m's frame key (Spore 00bc9980); an empty inline
// helper with 4 parameters is +4 frame and +4 tmp keys at one statement
unsigned kd__base(int* p, int n)
{
    unsigned s = 0;
    for (int i = 0; i < n; i++)
    {
        int id = p[i];
        int w = g(id);
        unsigned m = (unsigned)p[i + 1] | (unsigned)w;
        s += m;
    }
    return s;
}
unsigned kd__early(int* p, int n)
{
    unsigned s = 0;
    for (int i = 0; i < n; i++)
    {
        unsigned m = 0;
        int id = p[i];
        int w = g(id);
        m = (unsigned)p[i + 1] | (unsigned)w;
        s += m;
    }
    return s;
}
__forceinline void rz_k4(int a0, int a1, int a2, int a3) {}
float kp__base(float* p, int n)
{
    float s = p[0] * p[1];
    s += p[2] * (float)g(n);
    return s * p[3] + p[4];
}
float kp__h4(float* p, int n)
{
    float s = p[0] * p[1];
    rz_k4(0, 0, 0, 0);
    s += p[2] * (float)g(n);
    return s * p[3] + p[4];
}
// load-reuse levers (docs/c2/loadcse.md): realize.py alias2:p->m, ptrlocal:p->q, subobj:w->e on these
struct LS { char pad[92]; int m; int n; };
struct LQ { char pad[92]; int m; };
struct LP { int a; LQ* q; };
struct LE { int z; };
struct LW { char pad[92]; LE** e; LE** b2; };
int la__base(LS* p, int c) { int t = p->m & 255; if (c) t |= p->m << 4; else t ^= p->m; return t; }
void lp__base(LP* p) { if (!(p->q->m & 3)) p->q->m = (p->q->m & ~1) | 2; }
LE* lw__base(LW* w) { LE* x = w->e[-1]; --w->e; return x; }
