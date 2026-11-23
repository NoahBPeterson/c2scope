// tests for tools/loadcse.py on cl 17.00 x64 (docs/c2/loadcse.md 8): the rules that differ from the x86 builds
void h(int);
extern "C" void* memcpy(void*, const void*, unsigned long long);
#pragma intrinsic(memcpy)
struct S { char pad[92]; int m; int n; };
struct Q { char pad[92]; int m; };
struct P { int a; Q* q; };
// OpeqBitAssigns: ([m] & c) | y becomes `and [m], c` + `or [m], y` (14.00: load / and / or / store)
void bit_and_or(S* p) { p->m = (p->m & ~1) | 2; }
// ... not through a loaded pointer: load / and / or / store
void bit_loaded_ptr(P* p) { p->q->m = (p->q->m & ~1) | 2; }
// the arm's reads after a call: R5 (the call), not the alias class
void call_arm(P* p) { if (!(p->q->m & 3)) { h(0); p->q->m = (p->q->m & ~1) | 2; } }
// a memcpy intrinsic between two reads writes memory: R5
int intrin_kill(S* p, char* d, const char* s, unsigned n) { int t = p->m; memcpy(d, s, n); return t + p->m; }
// the pointer moves between the reads (`*++s`): another address, not a missed CSE
int moved(const unsigned char* s, const unsigned char* m, int n) { do { if (*++s != *++m) break; if (*++s != *++m) break; } while (--n); return n; }
