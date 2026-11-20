// frame.md 4.6 / 6 (StkSetStackAlignment, FUNC+0x48 caps and raises slot alignment): what raises the frame
// alignment (and esp, -N), and how the other slots are aligned under it. Packing order is size ascending, an ESP
// frame allocates the largest first (highest), so: a16: v (16, align 16) -16, s12 (12) -28, then d (8): -36 if
// slots stay 4-aligned, -40 if a double gets 8 under the raised cap. plain12: the same without v.
// d8loop / d8loop2: one or two address-taken doubles in a loop; s8loop2: two 8-byte int structs in a loop.
void ext(void*);
struct __declspec(align(16)) V { float f[4]; };
struct S12 { int a, b, c; };
struct S8 { int a, b; };
void a16() { V v; S12 s; double d; ext(&v); ext(&s); ext(&d); }
void plain12() { S12 s; double d; ext(&s); ext(&d); }
void d8loop(int n) { for (int k = 0; k < n; k++) { double d; ext(&d); } }
void d8loop2(int n) { for (int k = 0; k < n; k++) { double d, e; ext(&d); ext(&e); } }
void s8loop2(int n) { for (int k = 0; k < n; k++) { S8 s, t; ext(&s); ext(&t); } }
void s8flat2() { S8 s, t; ext(&s); ext(&t); }
