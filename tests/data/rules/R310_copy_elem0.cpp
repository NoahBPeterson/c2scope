// R310 (f3decomp): a struct copied element by element through an inlined operator=: on 14.00/15.00 the +0 field view
// of the copy is made inside the first operator= expansion (low key), the other elements' views later by the optimiser
// (symkeys keys g: _m+0.f4 in V::operator=#1, _m+12.f4 / _m+24.f4 by the optimiser). 13.10: every element gets a
// reader-time view first (keys 4, 5, 6), so element 0 is not special. docs/c2/symkeys.md.
struct V { float x, y, z; V& operator=(const V& o) { x = o.x; y = o.y; z = o.z; return *this; } };
struct M { V c[3]; const V& col(int i) const { return c[i]; } };
inline float dx(const V& v, float b) { return v.x * b; }
float f(const M* src, const float* b) { M m; for (int i = 0; i < 3; i++) m.c[i] = src->c[i];
  return dx(m.col(0), b[0]) + dx(m.col(1), b[1]) + dx(m.col(2), b[2]); }
float g(const M* src, const float* b) { M m; m.c[0] = src->c[0]; m.c[1] = src->c[1]; m.c[2] = src->c[2];
  return dx(m.col(0), b[0]) + dx(m.col(1), b[1]) + dx(m.col(2), b[2]); }
