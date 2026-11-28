// /O2 shapes of the lifter (docs/c2/lift2il.md): compiled /O2 /arch:SSE, lifted back from its bytes, recompiled
struct P { float v; int k; };
int o2_fsel(P* a, int i, int n) { if (a[i + 1].v > a[i].v) i = i - 1; return a[i].k + n; }
int o2_sx(const signed char* t, int i) { return t[i] * 3; }
// fucomip / fcomip (capstone: fucompi / fcompi) pop the x87 stack, so the second compare reads the right values
int o2_fcomip(double* p, int n) { double z = p[0] - p[2]; if (z != p[4]) n = n + 2; return n + (p[1] < p[3] ? 1 : 0); }
// a virtual call on a member object: the vtable is read through the owner's base before `this` is formed, and a
// store comes between (`((V*)(p + 288))->f()` at the call, not a named `this` computed first: Spore 00971b60)
struct L2M { virtual int f0(); virtual int f1(); };
struct L2O { char pad[288]; L2M m; int k; };
int o2_vmem(L2O* o, int a) { o->k = a; return o->m.f1() + 1; }
// two cdecl calls through pointers whose arguments one `add esp, 0x18` pops, then an esp frame's pops and its
// `add esp, 0xc`: the cleanups are the calls', the second call's ecx is its target, not `this` (Spore 00656b80)
struct L2FP { void (*c3)(int*, int, int); };
void o2_cfp(L2FP* f, int a, int b) { int loc[3]; loc[0] = a; loc[1] = b; loc[2] = a + b; f->c3(loc, a, b); f->c3(loc, b, a); }
// a bool variable returned as is (`mov al, bl`), also a bool callee's result kept in one (Spore 006bd430)
struct L2B { virtual bool t(); virtual void u(); };
bool o2_bret(L2B* p, L2B* q) { bool r = true; if (!p->t()) r = false; q->u(); return r; }
bool o2_bret2(L2B* p, L2B* q) { bool r = p->t(); q->u(); return r; }
// a switch with two sparse cases: `sub eax, K; je; sub eax, 8; je`, a chain of differences only the next sub reads
// (Spore 00ea2670)
struct L2S { virtual int s0(); };
int o2_subsw(L2S* p, int* q) { switch (p->s0()) { case 84027824: return q[1]; case 84027832: return q[2]; } return 0; }
