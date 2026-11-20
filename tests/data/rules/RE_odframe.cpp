// il_sym.md 8 ("Front-end keys versus source order"): at /Od the frame order of locals is not first use. Is it
// declaration order, reversed, or name-dependent (a hash bucket order)?  Same body, different names.
// Compile with /Od; read the `_name$ = -N` lines.
void h(int*);
void fa(void) { int x[2]; int y[2]; int z[2]; h(x); h(y); h(z); }
void fb(void) { int a[2]; int b[2]; int c[2]; h(a); h(b); h(c); }
void fc(void) { int z[2]; int y[2]; int x[2]; h(z); h(y); h(x); }
void fd(void) { int q1[2]; int q2[2]; int q3[2]; int q4[2]; int q5[2]; h(q1); h(q2); h(q3); h(q4); h(q5); }
void fe(void) { int m[2]; int k[2]; int w[2]; int d[2]; int p[2]; h(m); h(k); h(w); h(d); h(p); }
void ff(void) { int x[2]; int y[2]; int z[2]; h(z); h(y); h(x); }
