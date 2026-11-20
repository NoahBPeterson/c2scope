// frontend_il.md 5 (0x00487210 check): in a function with an EH frame, `new D(a)` with a throwing constructor gets
// its own state; the state byte is set to that state between operator new and the constructor call, and back
// after the constructor. Compile with /EHsc.
struct D { D(); D(int); ~D(); int v; };
void h();
D* f(int a) { D x; D* p = new D(a); h(); return p; }
D* g(int a) { D* p = new D(a); D* q = new D(a + 1); h(); p->v = q->v; return p; }
