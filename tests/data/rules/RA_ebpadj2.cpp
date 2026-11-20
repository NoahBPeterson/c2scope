// RA_ebpadj2: the EBP adjustment without /GS (frame.md 4.9, TryEbpAdjust's first candidate). 40 address-taken
// int locals (160 bytes) are too many for ebp's disp8 window below ebp; ebp is moved down until the last
// parameter dword is at [ebp+0x7c]: `lea ebp, [esp-(0x78 - parameter bytes)]` after `push ebp`.
// Compile /O1 /GS-. /DNP=<n>: n int parameters (0..40).
void h(int *);
int g(int);
extern int ga;
#ifndef NP
#define NP 1
#endif
#define V(i) int v##i = A + i;
#define U(i) h(&v##i);
#define R(i) + g(v##i)
#define X10(M, d) M(d##0) M(d##1) M(d##2) M(d##3) M(d##4) M(d##5) M(d##6) M(d##7) M(d##8) M(d##9)
#define X40(M) X10(M, 1) X10(M, 2) X10(M, 3) X10(M, 4)
#if NP == 0
#define PARAMS void
#define A ga
#elif NP == 1
#define PARAMS int a
#define A a
#elif NP == 5
#define PARAMS int a, int b, int c, int d, int e
#define A (a + b + c + d + e)
#elif NP == 20
#define PARAMS int a, int b, int c, int d, int e, int f0, int f1, int f2, int f3, int f4, int f5, int f6, int f7, \
    int f8, int f9, int f10, int f11, int f12, int f13, int f14
#define A (a + b + c + d + e + f14)
#elif NP == 31
#define PARAMS int a, int b, int c, int d, int e, int f0, int f1, int f2, int f3, int f4, int f5, int f6, int f7, \
    int f8, int f9, int f10, int f11, int f12, int f13, int f14, int f15, int f16, int f17, int f18, int f19, \
    int f20, int f21, int f22, int f23, int f24, int f25
#define A (a + b + c + d + e + f25)
#endif

int ec(PARAMS)
{
    X40(V)
    X40(U)
    return 0 X40(R);
}
