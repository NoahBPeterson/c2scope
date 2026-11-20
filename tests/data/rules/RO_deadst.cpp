// opt.md §6 rules 4 and 6: a store through a pointer is removed only when a later store to the same address
// follows in the same block with no possible read in between (GdfUpdateDeadIndirList, per block).
struct S { int pad[9]; int m; int n; };
void ds_same(S* p) { p->m = 1; p->m = 2; }                        // one store
void ds_other(S* p, S* q) { p->m = 1; q->n = 5; p->m = 2; }      // a store through q is not a read: one store to m
void ds_read(S* p, S* q, int* o) { p->m = 1; *o = q->n; p->m = 2; } // a load through q is a read: two stores
void ds_arms(S* p, int c) { p->m = 1; if (c) p->m = 2; else p->m = 3; }   // first store kept
void ds_blk(S* p, S* q, int c) { p->m = 1; if (c) q->n = 5; p->m = 2; }   // block boundary only: first store kept?
