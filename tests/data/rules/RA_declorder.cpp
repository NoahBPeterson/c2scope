// RA_declorder: declaration order is not an input of Color; moving a declaration that has an initializer moves
// the def, and with it defOrd (regalloc.md 3.4, 8 "Declaration order of locals moved registers").
// x and y have equal priority (same references, one block), so the tie goes to the earlier def (esi).
void v(int);
int g(int);

void d_xy(int a) { int x, y;  x = g(a); y = g(a + 1); v(x); v(y); v(x); v(y); }   // x defined first
void d_yx(int a) { int y, x;  x = g(a); y = g(a + 1); v(x); v(y); v(x); v(y); }   // same statements, decl swapped
void d_ix(int a) { int x = g(a); int y = g(a + 1); v(x); v(y); v(x); v(y); }        // initializers, x first
void d_iy(int a) { int y = g(a + 1); int x = g(a); v(x); v(y); v(x); v(y); }        // initializers, y first
