int helper(int *a, int n, int k);
int g;
int __stdcall DllMain(void *h, unsigned long r, void *p) { return 1; }
__declspec(dllexport) int entry(int *a, int n) { return helper(a, n, 3) + helper(a, n / 2, 5); }
