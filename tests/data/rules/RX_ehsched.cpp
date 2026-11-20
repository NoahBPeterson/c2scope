// RX_ehsched (sched.md rule 1 and rule 3): which EH functions the list scheduler skips (FUNC flag 0x100000 without
// flag 8), and whether a destructor-only function's EH state store is a scheduling barrier. Compile with /EHsc,
// /EHs and /EHa. Scheduled e_try_*: the tail `h(x * 3 + y * 5);` has its second load hoisted above
// the first store (unscheduled: load, op, store, load, op, store). e_dtor: the state store `__$EHRec$ = 0` after
// the constructor call sinks below the loads of p[0], p[1] when the store is not a barrier.
void g(); void h(int);
struct D { int v; D(); ~D(); };
void e_try_all(int* p, int* q, int x, int y)    { try { g(); } catch (...) { h(1); } h(x * 3 + y * 5); }
void e_try_int(int* p, int* q, int x, int y)    { try { g(); } catch (int) { h(2); } h(x * 3 + y * 5); }
void e_try_nocall(int* p, int* q, int x, int y) { try { q[2] = p[2]; } catch (...) { h(3); } h(x * 3 + y * 5); }
inline void tryer() { try { g(); } catch (...) { h(4); } }
void e_try_callee(int* p, int* q, int x, int y) { tryer(); h(x * 3 + y * 5); }
void e_plain(int* p, int* q, int x, int y)      { g(); h(x * 3 + y * 5); }
void e_dtor(int* p)               { D d; h(p[0] + p[1]); }
