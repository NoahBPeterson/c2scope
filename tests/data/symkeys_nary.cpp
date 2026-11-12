// symkeys: n-ary operand sorts (an OPADD of three or four operands is sorted as one list; symkeys.md section 3)
int f1(); int f2(); int f3(); void f4();
int kn3(int a, int b, int c) { return a + b + c; }
int kn4(int a, int b, int c, int d) { return a + b + c + d; }
int kcall3() { int a = f1(); int b = f2(); int c = f3(); f4(); return a + b + c; }
// optimiser temporaries (keys --phase opt, `$o<n>`): the common a[i + 1] is kept in a temporary the optimiser makes
int kcse(int* a, int i, int x, int y) { return a[i + 1] * x + a[i + 1] * y + a[i + 1]; }
