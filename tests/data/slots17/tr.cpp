extern int ext(int, int);
struct P2 { int a, b; };
int t1(int a) { int x[2]; x[0] = a; x[1] = a * 3; return ext(x[0], x[1]); }
int t2(int a, int b) { P2 p = { a, b }; P2 q = p; return ext(q.a + 1, q.b); }
int t3(int a, int b, int c, int d) { int x = a; if (b) x = c; else x = d; return ext(x, a); }
int t4(int a) { int k[4] = { a, a + 1, a + 2, a + 3 }; return ext(k[a & 1], k[2]); }
int t5(int a, int b) { volatile int x = a; return ext(b, 1); }
