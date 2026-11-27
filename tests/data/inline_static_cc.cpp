// 13.10 /O2, one TU, no /GL: a static function whose address is not taken gets register arguments (and is compiled
// first); extern, address-taken, anonymous-namespace and class-member functions keep the stack convention
volatile int g;
#define BODY int r = 0; for (int i = 0; i < a; i++) { r += b * i; g = r + c; } return r + g;
static int f_static(int a, int b, int c) { BODY }
static int f_addr(int a, int b, int c) { BODY }
static int __stdcall f_std(int a, int b, int c) { BODY }
static int f_rec(int a, int b, int c) { if (a > 100) return f_rec(a - 1, b, c); BODY }
namespace { int f_anon(int a, int b, int c) { BODY } }
int f_extern(int a, int b, int c) { BODY }
struct S { static int sm(int a, int b, int c) { BODY } };
int (*pf)(int, int, int) = f_addr;
int use(int x, int y) {
  return f_static(x, y, 1) + f_static(y, x, 2) + f_addr(x, y, 1) + f_addr(y, x, 2) + f_std(x, y, 1) + f_std(y, x, 2)
       + f_rec(x, y, 1) + f_rec(y, x, 2) + f_anon(x, y, 1) + f_anon(y, x, 2) + f_extern(x, y, 1) + f_extern(y, x, 2)
       + S::sm(x, y, 1) + S::sm(y, x, 2);
}
