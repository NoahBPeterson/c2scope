// regalloc_cost.md 2 / 9 ("naming a global's re-read raises its register priority"): a global is an aliased
// candidate, its web pays an enter-load and an exit-save; a local copy is an ordinary web.
// gn0 reads G in the loop, gn1 reads a local copy of it (no stores or calls in the loop, so both are legal).
extern int G;
int gn0(const int* p, int n) { int s = 0; for (int i = 0; i < n; i++) s += p[i] * G; return s; }
int gn1(const int* p, int n) { int l = G; int s = 0; for (int i = 0; i < n; i++) s += p[i] * l; return s; }
