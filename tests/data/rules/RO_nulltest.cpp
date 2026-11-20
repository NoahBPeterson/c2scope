// opt.md §4 rule 8: a pointer member that is null-tested and dereferenced twice is loaded twice.
struct E { int a, b; };
struct C { int pad[36]; E* one; };
int nt_twice(C* c) { if (c->one) return c->one->b + c->one->a; return 0; }    // cmp [x+144],0; je; mov [x+144]
int nt_once(C* c) { if (c->one) return c->one->b; return 0; }                 // one load
int nt_local(C* c) { E* e = c->one; if (e) return e->b + e->a; return 0; }    // one load
int nt_twoblk(C* c) { if (c->one && c->one->a) return c->one->b; return 0; } // one load
