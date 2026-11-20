// RX_slotseed (symkeys.md section 1 "frame slot order", slots.md rules 2 and 7): with T <= 128 the colours keep
// creation order, and creation follows the element list (size, then uses, then the first to reach its count in the
// instruction stream). Does the SYM key (declaration order) decide anything? a is declared first, b used first.
void usei(int*);
void o_use_b_first()  { int a, b; usei(&b); usei(&a); }
void o_use_a_first()  { int a, b; usei(&a); usei(&b); }
void o_decl_b_first() { int b, a; usei(&b); usei(&a); }
void use2(int*, int*); int geti();
void o_args()         { int a, b; use2(&a, &b); }
void o_store_then(int c) { int a, b; a = c; b = geti(); usei(&b); usei(&a); }
void o_hoist(int n)   { int a = 0, b = 0; for (int i = 0; i < n; ++i) { usei(&b); } usei(&a); }
