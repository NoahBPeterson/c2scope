// R480 (f3decomp): `x >= 0` -> test; jl, `(x & 0x80000000) == 0` -> test; js (all builds). The bit test costs one
// more symbol before OptFuncExpr and 7 more keys in total here (symkeys keys --all: 10/42 against 11/49).
int g(int); extern int gv;
int sgn(int x) { int r = 0; if (x >= 0) r = g(x); return r + gv; }
int bit(int x) { int r = 0; if ((x & 0x80000000) == 0) r = g(x); return r + gv; }
int sgnl(int x) { int r = 0; if (x < 0) r = g(x); return r + gv; }
int bitl(int x) { int r = 0; if (x & 0x80000000) r = g(x); return r + gv; }
