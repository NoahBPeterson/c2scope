// opt.md §5 rule 2, §12 cq16: a store and a load through two spellings of one address are not forwarded.
struct P { int m, n; };
int fw_alias(P* p) { int* q = &p->n; *q = 1; return p->n; }            // store + reload
int fw_back(P* p) { int* q = &p->n; p->n = 1; return *q; }             // store + reload
int fw_same(P* p) { p->n = 1; return p->n; }                           // forwarded
int fw_star(P* p) { int* q = &p->n; *q = 1; return *q; }               // forwarded
int fw_off0(P* p) { int* q = &p->m; *q = 1; return p->m; }             // forwarded (q = p is a plain copy)
int fw_ref(P* p) { int& r = p->n; r = 1; return p->n; }                // reference: like fw_alias?
int fw_copy(P* p) { P* r = p; r->n = 1; return p->n; }                 // pointer copy: forwarded?
int fw_cast(P* p) { int* q = (int*)((char*)p + 4); *q = 1; return p->n; }
