// frame.md 4.4: scope-entry kills (ItfRemoveScopes / MscRemoveAcyclicScopeEntryKills).
// Two block-scoped address-taken locals, each only passed to ext(), inside a loop (loop<N>) or not (flat<N>);
// N = size in bytes. Do the two share one slot (frame = N) or not (frame = 2N)?
void ext(void*);
template <int N> struct B { int v[N / 4]; };
#define L(N) void loop##N(int n) { for (int i = 0; i < n; i++) { { B<N> a; ext(&a); } { B<N> b; ext(&b); } } } \
                 void flat##N() { { B<N> a; ext(&a); } { B<N> b; ext(&b); } }
L(4) L(8) L(12) L(16) L(20) L(24) L(32) L(64)
// the same with plain arrays
#define A(N) void aloop##N(int n) { for (int i = 0; i < n; i++) { { int a[N / 4]; ext(a); } { int b[N / 4]; ext(b); } } }
A(4) A(8) A(16) A(20) A(64)
