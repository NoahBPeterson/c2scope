// R1275 (f3decomp): a member subobject's inlined constructor stores through its own `this` (a new pointer symbol at
// ItfBuildInterference: `s3 = this+12; [s3] = 0`), so its store depends on the vtable store and pins it above the
// argument push and the EH state store (sched.md rule 4a). Plain/WithZ/WithC: plain member stores, the vtable store
// sinks after the push and the state store. WithH/WithH2: `mov [esi], vtbl` right after `xor eax, eax`. All builds.
void use(void*);
struct Base { virtual ~Base(); Base(void* a); float earlyOut; int b0; };
struct Handle { unsigned value; Handle() : value(0) {} };
struct Plain : Base { Plain(void* a); int unk70; int unk74; virtual ~Plain(); };
struct WithH : Base { WithH(void* a); Handle h; int unk74; virtual ~WithH(); };
struct WithH2 : Base { WithH2(void* a); int unk70; Handle h; virtual ~WithH2(); };
Plain::Plain(void* a) : Base(a) { unk70 = 0; unk74 = 0; use(a); }
WithH::WithH(void* a) : Base(a) { unk74 = 0; use(a); }
WithH2::WithH2(void* a) : Base(a) { unk70 = 0; use(a); }
inline void Zero(int& x) { x = 0; }
struct Clr { unsigned value; void Clear() { value = 0; } };
struct WithZ : Base { WithZ(void* a); int unk70; int unk74; virtual ~WithZ(); };
WithZ::WithZ(void* a) : Base(a) { Zero(unk70); unk74 = 0; use(a); }
struct WithC : Base { WithC(void* a); Clr c; int unk74; virtual ~WithC(); };
WithC::WithC(void* a) : Base(a) { c.Clear(); unk74 = 0; use(a); }
