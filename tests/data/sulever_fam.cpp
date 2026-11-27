// sulever.py catalogue (tools/sulever.py CATALOGUE): which rewrites of an operand change its SU / count.  n04 is the
// view-mask (SU1: code-neutral on 14.00 / 15.00 only), n05 the wide mask (SU1, and keeps an `and`); the others are
// the same expression spelled differently and must leave every sort weight alone (the "identity" and "local-copy"
// families).  Groups: n* a narrow variable, p* a load, q* members, m* locals / references.
struct Q { int a; int b; unsigned char c; short s; };
int n00(unsigned char c, int* p) { return c + *p; }
int n02(unsigned char c, int* p) { return (c + 0) + *p; }
int n03(unsigned char c, int* p) { return (c | 0) + *p; }
int n04(unsigned char c, int* p) { return (*(unsigned*)&c & 0xff) + *p; }
int n05(unsigned char c, int* p) { return ((unsigned)c & 0xffff) + *p; }
int n06(unsigned char c, int* p) { return (int)(unsigned char)(c) + *p; }
int n07(unsigned char c, int* p) { return +c + *p; }
int n08(unsigned char c, int* p) { return (c * 1) + *p; }
int n09(unsigned char c, int* p) { return (c ^ 0) + *p; }
int n10(unsigned char c, int* p) { return (c & 255) + *p; }
int n13(unsigned char c, int* p) { return (c << 0) + *p; }
int n16(unsigned char c, int* p) { return ((c)) + *p; }
int n19(unsigned char c, int* p) { return (int)(unsigned int)c + *p; }
int p00(int x, int* p) { return x + *p; }
int p01(int x, int* p) { return (unsigned)x + *p; }
int p02(int x, int* p) { return x + p[0]; }
int p03(int x, int* p) { return x + *(p + 0); }
int p04(int x, int* p) { return (x & -1) + *p; }
int p05(int x, int* p) { return x + (*p | 0); }
int p06(int x, int* p) { return x + (0 ? 1 : *p); }
int p07(int x, int* p) { return x + -(-*p); }
int p08(int x, int* p) { return x + ~~*p; }
int p09(int x, int* p) { return (x, x) + *p; }
int p10(int x, int* p) { return x + (*p * 1); }
int p11(int x, int* p) { return (x ^ 0) + *p; }
int q00(int x, Q* q) { return x + q->b; }
int q01(int x, Q* q) { return x + *(int*)((char*)q + 4); }
int q02(int x, Q* q) { return x + (&q->a)[1]; }
int q03(int x, Q* q) { return x + q[0].b; }
int q04(int x, Q* q) { return x + (*q).b; }
int q05(int x, Q* q) { return x + q->c; }
int q06(int x, Q* q) { return x + (unsigned char)q->c; }
int q07(int x, Q* q) { return x + (int)q->c; }
int m00(int x, Q* q) { return x + q->b; }
int m01(int x, Q* q) { int* r = &q->b; return x + *r; }
int m02(int x, Q* q) { int& r = q->b; return x + r; }
int m03(int x, Q* q) { int b = q->b; return x + b; }
int m04(int x, Q* q) { return x + q->b + q->b; }
int m05(int x, Q* q) { int b = q->b; return x + b + b; }
