// frontend_il.md 4.13 (cycles): emission order of the members of a call cycle. Reading in the doc: the edge
// ignored as recursive is the one into the LAST-defined member (the walk starts there); the callee of that edge
// is then a leaf, and the others follow one sweep per link (a function placed earlier in the same sweep counts).
// One cycle per /D variant, nothing else in the unit. Edges written E(a,b) = "a calls b".
#define F(a, b) int a(int n) { return n > 0 ? b(n - 1) + 1 : 0; }
#define F2(a, b, c) int a(int n) { return n > 0 ? b(n - 1) + c(n - 2) : 0; }
int c1(int); int c2(int); int c3(int); int c4(int); int c5(int);
#if defined(C3)            // c1->c2->c3->c1, defined c1 c2 c3; predicted c2 c1 c3 (v4b)
F(c1, c2) F(c2, c3) F(c3, c1)
#elif defined(C3X)         // c1->c3->c2->c1; predicted c1 c2 c3
F(c1, c3) F(c2, c1) F(c3, c2)
#elif defined(C4)          // c1->c2->c3->c4->c1; predicted c3 c2 c1 c4
F(c1, c2) F(c2, c3) F(c3, c4) F(c4, c1)
#elif defined(C4X)         // c1->c3->c2->c4->c1; predicted c2 c3 c1 c4
F(c1, c3) F(c2, c4) F(c3, c2) F(c4, c1)
#elif defined(C5)          // c1->c2->c3->c4->c5->c1; predicted c4 c3 c2 c1 c5
F(c1, c2) F(c2, c3) F(c3, c4) F(c4, c5) F(c5, c1)
#elif defined(C2)          // c1<->c2; predicted (A) order: c1 c2
F(c1, c2) F(c2, c1)
#elif defined(CC)          // c1->c2->c3->c1 plus c1->c3; predicted ?
F2(c1, c2, c3) F(c2, c3) F(c3, c1)
#endif
