// opt.md §8.4 step 3, §12 LoptBuildAddressModes: which uses of a strength-reduced pointer can absorb it.
// One canonical pointer (i*4)+a per loop; the use differs.
int am_load(int* a, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i]; return s; }        // [a+i*4]
int am_store(int* a, int n) { for (int i = 0; i < n; i++) a[i] = i; return 0; }                     // [a+i*4]
int am_addr(int* a, int** out, int n) { for (int i = 0; i < n; i++) out[i] = &a[i]; return 0; }      // address as a value
int am_cmp(int* a, int* e, int n) { int s = 0; for (int i = 0; i < n; i++) { if (&a[i] == e) s += 7; s += a[i]; } return s; }
int am_two(int* a, int* b, int n) { int s = 0; for (int i = 0; i < n; i++) s += a[i] + b[i]; return s; } // difference use
