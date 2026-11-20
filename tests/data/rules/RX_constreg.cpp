// RX_constreg (isel.md 7.4): a constant stored several times is kept in a register when the sum over its uses is
// >= 2, a use counting 1 if the other operand is a global/stack variable or a memory operand with a nonzero
// displacement, else 0 (a push counts 0). Register = the function has `mov/xor/or reg, ..` and stores from it.
extern int g1, g2; void two(int, int);
struct K { int a, b, c; void st2(); };
void c_p12(int* p)    { p[1] = 5; p[2] = 5; }             // 1+1: register
void c_p01(int* p)    { p[0] = 5; p[1] = 5; }             // 0+1: immediates
void c_gg()           { g1 = 5; g2 = 5; }                 // 1+1: register
void c_z2(int* p)     { p[0] = 0; p[1] = 0; }             // 0+1: immediates
void c_z3(int* p)     { p[0] = 0; p[1] = 0; p[2] = 0; }   // 0+1+1: register
void c_k1000(int* p)  { p[0] = 1000; p[1] = 1000; }       // 0+1: immediates
void c_m1x3(int* p)   { p[0] = -1; p[1] = -1; p[2] = -1; } // register (or reg, -1)
void c_push()         { two(5, 5); }                      // pushes count 0: immediates
void K::st2()         { b = 0; c = 0; }                   // [this+4], [this+8]: register
void c_byte3(char* p) { p[0] = 0; p[1] = 0; p[2] = 0; }   // byte register
void c_p0p0(int* p, int* q) { p[0] = 7; q[0] = 7; }       // 0+0: immediates
void c_p0g(int* p)    { p[0] = 7; g1 = 7; }               // 0+1: immediates
void c_p0gg(int* p)   { p[0] = 7; g1 = 7; g2 = 7; }       // 0+1+1: register
