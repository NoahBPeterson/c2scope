extern int g(int, int);
extern int G;
int f(int *a, int n, int k) {
    int s = 0;
    if (n > k) s = g(n, k);
    switch (k) { case 1: s += 3; break; case 2: s -= 7; break; case 3: s ^= 9; break; case 4: s *= 5; break; default: G = s; }
    return s + a[n];
}
