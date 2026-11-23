// RX_x87free (x87.md section 11 step 1, section 12.2 rules 1-3): an x87 value live across a call stays on the x87
// stack only when the callee is x87-free (its body generated earlier, no x87 push, or one push and an FP return).
// 13.10 FPInitialRegPressure skips the split at a call whose target has sym+0x20 & 0x10000; this checks that the
// bit means the same "x87-free" there as on 14.00/15.00 (MscRecordInterProcRegInfo).
// y = x * *d is computed before the call (each callee stores through an int*, which may alias *d).
// Kept on the stack = no `fstp _y$` before the call.
#if _MSC_VER < 1300
#define NOINLINE
#else
#define NOINLINE __declspec(noinline)
#endif
NOINLINE int geti(int* p) { p[0] = 1; return p[1] + 3; }        // no x87 at all: free
NOINLINE double getd(int* p, double* q) { p[0] = 1; return q[1]; } // one push, returns double: free
NOINLINE void voidfp(int* p, double* q) { p[0] = 1; q[2] = q[1]; } // one push, void: kills
NOINLINE double getd2(int* p, double* q) { p[0] = 1; return q[1] * q[2] + q[3] * q[4]; } // two pushes: kills
int ext(int*);                                                              // no body: kills
double k_geti(double x, double* d, int* p) { double y = x * *d; int i = geti(p); return y * x + i; }
double k_getd(double x, double* d, int* p) { double y = x * *d; double e = getd(p, d); return y * x + e; }
double k_void(double x, double* d, int* p) { double y = x * *d; voidfp(p, d); return y * x + d[2]; }
double k_getd2(double x, double* d, int* p) { double y = x * *d; double e = getd2(p, d); return y * x + e; }
double k_ext(double x, double* d, int* p) { double y = x * *d; int i = ext(p); return y * x + i; }
