/* Tie order in Color's priority list: equal prio -> the LARGER defOrd is popped first (InsertInList: prio descending,
   then defOrd descending, unsigned). defOrd counts instructions with blocks in list order and each block walked
   backwards, so inside one block the EARLIER def has the larger defOrd; across blocks the def in the LATER block has.
   Compile as C: default flags (float/double = x87 class) and /arch:SSE2 (float/double = XMM class 6).
     uv run tools/c2trace.py regs work/scratch/tieorder/tie.c --all-classes [--c2 13.10|15.00] [-- /arch:SSE2]   */
int g(int); extern int gx; extern double gd[8];

/* int, one block: a, b, c have 3 references each */
int ti_blk(void) { int a = g(1), b = g(2), c = g(3); g(0); g(a); g(b); g(c); g(a); g(b); g(c); return 0; }

/* int, across blocks (regalloc.md t5.c): a and c are defined in the entry block, b after the if */
int ti_xblk(void) { int a = g(1); int c = g(3); g(c); g(c); if (gx) g(5);
  { int b = g(2); g(a); g(b); g(a); g(b); } return 0; }

/* floating point, one block: a, b, c have 3 references each */
void tf_blk(const double *p) { double a = p[0] * p[3], b = p[1] * p[4], c = p[2] * p[5];
  gd[0] = a + b; gd[1] = b + c; gd[2] = c + a; }

/* floating point, across blocks: a and c in the entry block, b after the if */
void tf_xblk(const double *p) { double a = p[0] * p[3]; double c = p[2] * p[5]; gd[4] = c; gd[5] = c + 1.0;
  if (gx) gd[6] = 0.0;
  { double b = p[1] * p[4]; gd[0] = a + b; gd[1] = a - b; } }
