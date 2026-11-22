// FUNC flags NColor reads (ncolor.md 12.4): FUNC+0xe4 & 0x80 (a floating-point leaf, OtlScanFuncLeaves: no
// NColMdAdjustPriority), FUNC+0xe4 & 0x4801000 / +0xe8 & 0x100 (rbp out of the useable sets: an EH frame).
// /O2 /EHsc. Each function has an adler32-like loop: a hot int global that NColMdAdjustPriority moves when allowed.
extern int g(int);
extern void h(const unsigned char *, int);
struct D { int v; ~D(); };

int try_int(const unsigned char *b, int n)          // try, no float: adjust; rbp out
{
    unsigned a = 1, c = 0;
    try {
    while (n-- > 0) {
        a += b[0]; c += a; a += b[1]; c += a; a += b[2]; c += a; a += b[3]; c += a;
        a += b[4]; c += a; a += b[5]; c += a; a += b[6]; c += a; a += b[7]; c += a;
        b += 8;
    }
        c += g(a);
    } catch (...) { c = 0; }
    return (int)(a ^ c);
}

int dtor_int(const unsigned char *b, int n)         // a destructor-only unwind, no float
{
    D d = { n };
    unsigned a = 1, c = 0;
    while (n-- > 0) {
        a += b[0]; c += a; a += b[1]; c += a; a += b[2]; c += a; a += b[3]; c += a;
        a += b[4]; c += a; a += b[5]; c += a; a += b[6]; c += a; a += b[7]; c += a;
        b += 8;
    }
    h(b, a);
    return (int)(a ^ c) + d.v;
}

int float_int(const unsigned char *b, int n, float w)   // a float, no EH: no adjust
{
    unsigned a = 1, c = 0;
    while (n-- > 0) {
        a += b[0]; c += a; a += b[1]; c += a; a += b[2]; c += a; a += b[3]; c += a;
        a += b[4]; c += a; a += b[5]; c += a; a += b[6]; c += a; a += b[7]; c += a;
        b += 8;
    }
    return (int)(a ^ c) + (int)(w * a);
}

int plain_int(const unsigned char *b, int n, int w)     // neither: adjust
{
    unsigned a = 1, c = 0;
    while (n-- > 0) {
        a += b[0]; c += a; a += b[1]; c += a; a += b[2]; c += a; a += b[3]; c += a;
        a += b[4]; c += a; a += b[5]; c += a; a += b[6]; c += a; a += b[7]; c += a;
        b += 8;
    }
    return (int)(a ^ c) + w * (int)a;
}

int try_float(const unsigned char *b, int n, double w)  // both (as tests/data/ncol c2.cpp's f2)
{
    unsigned a = 1, c = 0;
    try {
    while (n-- > 0) {
        a += b[0]; c += a; a += b[1]; c += a; a += b[2]; c += a; a += b[3]; c += a;
        a += b[4]; c += a; a += b[5]; c += a; a += b[6]; c += a; a += b[7]; c += a;
        b += 8;
    }
        c += g((int)(a * w));
    } catch (...) { c = 0; }
    return (int)(a ^ c);
}
