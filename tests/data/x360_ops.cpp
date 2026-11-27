extern "C" {
int o_sub(int a, int b) { return a - b; }
int o_div(int a, int b) { return a / b; }
unsigned o_udiv(unsigned a, unsigned b) { return a / b; }
int o_mod(int a, int b) { return a % b; }
int o_and(int a, int b) { return a & b; }
int o_or(int a, int b) { return a | b; }
int o_xor(int a, int b) { return a ^ b; }
int o_shl(int a, int b) { return a << b; }
int o_shr(int a, int b) { return a >> b; }
unsigned o_ushr(unsigned a, unsigned b) { return a >> b; }
int o_neg(int a) { return -a; }
int o_not(int a) { return ~a; }
int o_lnot(int a) { return !a; }
int o_lt(int a, int b) { return a < b; }
float o_fdiv(float a, float b) { return a / b; }
float o_fsub(float a, float b) { return a - b; }
double o_f2d(float a) { return a; }
int o_f2i(float a) { return (int)a; }
float o_i2f(int a) { return (float)a; }
int o_ld(int* p) { return p[3]; }
void o_st(int* p, int v) { p[2] = v; }
char o_ldb(char* p) { return p[1]; }
short o_lds(short* p) { return p[1]; }
int o_q(int a, int b, int c) { return a ? b : c; }
int g(int);
int o_if(int a) { if (a > 5) return g(a); return 0; }
int o_loop(int* p, int n) { int s = 0; for (int i = 0; i < n; i++) s += p[i]; return s; }
void o_vcall(struct V* v);
}
struct V { virtual int f(int); };
extern "C" int o_virt(V* v) { return v->f(3); }
