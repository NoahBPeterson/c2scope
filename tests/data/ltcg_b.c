extern int g;
int helper(int *a, int n, int k) { int i, s = 0, t = 1; for (i = 0; i < n; i++) { s += a[i] * k; t ^= a[i] + s; } g = t; return s + t; }
