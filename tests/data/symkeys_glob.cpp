// symkeys: what a global costs in keys (symkeys.md section 7), measured as allocations per class against g0
struct S { int a; int b; int arr[8]; int m(); };
extern S gs;
extern int gi;
int g0(int i) { return i; }                                   // baseline
int gbase(int i) { S* p = &gs; return p->b + i; }             // address of the global taken, field through a pointer local
int gref(int i) { S& r = gs; return r.b + i; }                // the same through a reference local
int gfield0(int i) { return gs.a + i; }                       // offset-0 field
int gfield4(int i) { return gs.b + i; }                       // field at a non-zero offset
int gfield44(int i) { return gs.b + gs.b + i; }               // the same field twice
int gcall(int i) { return gs.m() + i; }                       // member call on the global
int garr(int i) { return gs.arr[i] + i; }                     // array element, variable index
int garrc(int i) { return gs.arr[3] + i; }                    // array element, constant index
int gvoid(int i) { (void)gs; return i; }                      // bare mention
int gint(int i) { return gi + i; }                            // a scalar global
