// R1310 (f3decomp): a catch block is emitted right after the parent's ret, inside the same function (COMDAT): f's
// `__catch$...$0` (13.10: $Lnnn) follows its ret. All builds.
struct V { int* p; void Tidy(); void Build(int); };
void f(V* v, int n) { try { v->Build(n); } catch (...) { v->Tidy(); throw; } }
int g(V* v, int n) { try { v->Build(n); } catch (...) { v->Tidy(); return 0; } return 1; }
