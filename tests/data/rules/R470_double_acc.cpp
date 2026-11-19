// R470 (f3decomp): `double s = x*x; s += y*y; s += z*z;` sorts the accumulator operand first by count (symkeys sites
// pass 2: n4 against n3 on 14.00/15.00, n5 against n4 on 13.10), whatever the keys; a plain float sum has equal counts.
struct V { float x, y, z; };
float dacc(const V* v) { double s = v->x * v->x; s += v->y * v->y; s += v->z * v->z; return (float)s; }
float facc(const V* v) { return v->x * v->x + v->y * v->y + v->z * v->z; }
float dexp(const V* v) { double s = v->x * v->x; s += double(v->y * v->y); s += double(v->z * v->z); return (float)s; }
