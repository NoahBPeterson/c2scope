// docs/c2/loopopt.md: strength reduction in rounds (LoptStrengthReduce).  Reported by tricko3D (spore-decomp PR #674).
// Each function is one counted loop; the probe counts rounds (LoptFindCandidates mode 1 calls that find a candidate)
// and reads the listing of the loop.
extern void sink(int *);
extern int *sink2(int **);
extern int ext(void);
int lp_idx(int *a, int n)            { int i, s = 0; for (i = 0; i < n; i++) s += a[i]; return s; }          // i*4, then (i*4)+a
int lp_mulc(int *a, int n)           { int i, s = 0; for (i = 0; i < n; i++) s += a[i * 3]; return s; }      // i*12 ...
int lp_ik(int *a, int n, int k)      { int i, s = 0; for (i = 0; i < n; i++) s += a[i + k]; return s; }      // i+k, i*4, 4*k ...
int lp_ivk(int n, int k)             { int i, s = 0; for (i = 0; i < n; i++) s += (i + k) ^ s; return s; }   // one round: i+k
int lp_ivv(int n, int k)             { int i, s = 0; for (i = 0; i < n; i++) s += (i * k) ^ s; return s; }   // one round: i*k
int lp_ptr(int *p, int *e)           { int s = 0; for (; p < e; p++) s += *p; return s; }                  // already an IV: no round
int lp_next(int *offs, int n)        { int s = 0; int *e = offs + n; while (offs < e) { int *next = offs + 1; s += *offs; if (s & 1) s += *next; offs = next; } return s; }
int lp_next1(int *offs, int n)       { int s = 0; int *e = offs + n; while (offs < e) { int *next = offs + 1; s += *offs; offs = next; } return s; }
int lp_two(int *a, int *b, int n)    { int i, s = 0; for (i = 0; i < n; i++) s += a[i] * b[i]; return s; }  // preheader: b then a
int lp_jac(int *a, int n)            { int i, s = 0; int *jac = a; for (i = 0; i < n; i++) s += jac[i] + ext(); return s; }
int lp_jact(int *a, int n)           { int i, s = 0; int *jac = a; sink2(&jac); for (i = 0; i < n; i++) s += jac[i] + ext(); return s; }
int lp_itaken(int *a, int n)         { int i, s = 0; sink(&i); for (i = 0; i < n; i++) s += a[i * 3]; return s; }   // i address-taken: not an IV
