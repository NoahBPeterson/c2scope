/* FPCheckColorability (cl 13.10): one function per rule of docs/c2/x87.md section 11.1.  Plain /O2 C.
   Every variable is used at least twice (a single-use value is a temporary, not a web). */
void g(void);
extern double gd[16];

/* a double live across a call: FPInitialRegPressure splits it there */
double k_call(double a) { double x = a * 2.0; g(); return x * x + x; }

/* the end rule: b ends while c (defined later) is above it -> c is split where b ends (InsertColoredLifetimeEndSplit) */
void k_end(const double *p) { double a = p[0] * p[3], b = p[1] * p[4], c = p[2] * p[5];
  gd[0] = a + b; gd[1] = b + c; gd[2] = c + a; }

/* CanExtendLifetime: a dies while c is above it, nothing in between defines a globreg: a lives on to c's end */
void k_ext(const double *p) { double a = p[0] * p[1], c = p[2] * p[3];
  gd[4] = a * a; gd[0] = a + c; gd[1] = c * 2.0; gd[2] = c * 3.0; gd[3] = c * c; }

/* SplitAtMismatchedFlowJoinPoints: w above c in one arm, below it in the other; both end in one expression */
void k_join(const double *p, int n) { double c, w;
  if (n) { c = p[0] * p[1]; w = p[2] * p[3]; gd[0] = c * c; }
  else { w = p[4] * p[5]; c = p[6] * p[7]; gd[1] = c * c * c; }
  gd[2] = p[8] * p[9] + p[10]; gd[3] = p[11] * p[12] - p[13]; gd[4] = w + c; }

/* nine values live at once: after seven colourings the stack is 8 deep (FPUpdateRegisterPressure splits i, j) */
void k_depth(const double *p) { double a = p[0]*p[9], b = p[1]*p[9], c = p[2]*p[9], d = p[3]*p[9], e = p[4]*p[9],
  f = p[5]*p[9], h = p[6]*p[9], i = p[7]*p[9], j = p[8]*p[9];
  gd[0] = a + b + c + d + e + f + h + i + j; gd[1] = j - i - h - f - e - d - c - b - a; }
