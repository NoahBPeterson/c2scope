// R1270 (f3decomp): the index form of a store decides whether earlier stores to the object may sink past it.
// a_*: the res store (+24) sinks to just before the call; b_*: it stays right after the size store (+20), because the
// byte store gets mem-output edges from both earlier stores (sched.md rule 4a): at ItfBuildInterference its address is
// `s+4 + X` with X a variable or an unfolded temporary (b_var: _n; b_inplace: t = 1*0), or a store through another
// pointer symbol (b_ptr: _q). a_eos: the inlined argument is the literal 0 there, so it behaves like a constant index.
// 13.10: b_inplace sinks too (the in-place spelling folds earlier); b_var/b_reread/b_ptr pin on every build.
struct S { void* vp; char buf[16]; int size; int res; void Assign(const void*, int, int); };
inline void Eos(S* s, int n) { s->size = n; s->buf[n] = 0; }
void* a_plain(S* s, const void* r) { s->res = 15; s->size = 0; s->buf[0] = 0; s->Assign(r, 0, -1); return s; }
void* a_rev(S* s, const void* r) { s->res = 15; s->buf[0] = 0; s->size = 0; s->Assign(r, 0, -1); return s; }
void* a_const(S* s, const void* r) { s->res = 15; const int n = 0; s->size = n; s->buf[n] = 0; s->Assign(r, 0, -1); return s; }
void* a_eos(S* s, const void* r) { s->res = 15; Eos(s, 0); s->Assign(r, 0, -1); return s; }
void* b_inplace(S* s, const void* r) { s->res = 15; s->buf[s->size = 0] = 0; s->Assign(r, 0, -1); return s; }
void* b_var(S* s, const void* r) { s->res = 15; int n = 0; s->size = n; s->buf[n] = 0; s->Assign(r, 0, -1); return s; }
void* b_reread(S* s, const void* r) { s->res = 15; s->size = 0; s->buf[s->size] = 0; s->Assign(r, 0, -1); return s; }
void* b_ptr(S* s, const void* r) { s->res = 15; s->size = 0; char* q = s->buf; q[0] = 0; s->Assign(r, 0, -1); return s; }
