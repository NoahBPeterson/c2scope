// RX_cfref (slots.md section 1, consequences): a temporary bound to `const float&` against a named float local whose
// address escapes. Each function makes two such floats in sequence, with an int local live across both.
void use(const float&); void usep(const float*); void usei(int*);
void s_temp(float a, float b)  { int k = 0; usei(&k); use(a * b); use(a + b); usei(&k); }
void s_named(float a, float b) { int k = 0; usei(&k); float t = a * b; usep(&t); float u = a + b; usep(&u); usei(&k); }
void s_named_ref(float a, float b) { int k = 0; usei(&k); float t = a * b; use(t); float u = a + b; use(u); usei(&k); }
void s_block(float a, float b) { int k = 0; usei(&k); { float t = a * b; usep(&t); } { float u = a + b; usep(&u); } usei(&k); }
