/* NColor's order walk (docs/c2/ncolor.md 4): shift counts copied to cl (NColIdentifyExtraPhysAvail), a carry
   materialised after a dead write (NColUpdatePhysregAvail's piece boundaries), a block clear through rdi (a used
   callee-saved register first in the order), more values live across calls than callee-saved registers
   (NColBestCoverage), call results copied out of rax (preference windows). cl 17.00 x64 /O2. */
#include <string.h>

typedef struct S { unsigned hold, bits, n, flag; unsigned char *next; unsigned avail; } S;
extern unsigned g(unsigned);
extern void h(void *, unsigned);
extern int k(S *, int);

unsigned shiftcnt(S *s, const unsigned char *lens, int n)
{
    unsigned out = 0;
    int i;
    for (i = 0; i < n; i++) {
        unsigned op = lens[i];
        s->hold >>= op;
        s->bits -= op;
        out += s->hold & ((1u << op) - 1);
    }
    return out;
}

int carry(S *s)
{
    int t = (s->n != 0) ? -1 : 0;
    s->flag = t & -3;
    return s->flag;
}

unsigned clear(S *s, unsigned v)
{
    unsigned char buf[256];
    unsigned r;
    memset(buf, 0, sizeof buf);
    h(buf, v);
    r = g(v);
    h(buf, r);
    return r + v;
}

unsigned many(S *s, unsigned a, unsigned b, unsigned c, unsigned d)
{
    unsigned e = a * 3, f = b * 5, x = c * 7, y = d * 9, z = a ^ d, w = b ^ c, q = a + c, r = b + d, u = 0;
    int i;
    for (i = 0; i < (int)s->n; i++) {
        u += g(e + i) + g(f) + g(x);
        u ^= g(y) + g(z);
        if (u & 1)
            u += g(w) + g(q) + g(r);
    }
    return u + e + f + x + y + z + w + q + r;
}

int params(S *s, int level)
{
    int err = 0;
    int bstate = k(s, level);
    if ((unsigned)(bstate - 2) > 1)
        err = k(s, 0);
    if (bstate == 3)
        err += k(s, bstate);
    s->avail = (unsigned)err;
    return err;
}
