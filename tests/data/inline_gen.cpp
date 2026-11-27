// c2inline: sites nested in an inlinee show for the outer function (--func), a static callee that is only expanded
// is body "never", and /Og- (no call graph) puts the compiler-generated ??_E after its caller
#pragma optimize("g", on)
struct T { T(); ~T(); int x; };
static int sh(int a) { return a + 1; }
int usesh(int x) { return sh(x); }
int build(int n) { T *p = new T[n]; int r = p[0].x; delete[] p; return r; }
struct B { virtual ~B(); int v; };
struct W : B { W(); ~W(); int y; };
int copy(W &a) { W b(a); return b.y; }       // the implicit W(const W&) is expanded, and inside it B(const B&): depth 2
struct DM { DM(int); ~DM(); int q; };
struct Base { Base(); virtual ~Base(); };
struct Der : Base { DM m; Der(); ~Der(); };
Der::Der() : m(1) {}
Der::~Der() {}
int vdel(int x) { Der *d = new Der[x]; delete[] d; return 1; }   // delete[] of a virtual-dtor class: ??_E is reached through the vtable, no call-graph edge: compiled after its caller
