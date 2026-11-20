// frame.md 2.1, 2.2, 3.4: what gets a /GS cookie (__$ArrayPad$) and a $GSCopy$.
// strict_*: #pragma strict_gs_check(on) (entry.flags50 & 0x200000?): does an int array, or any array, get a cookie?
// sp_*: a by-value struct parameter holding a char buffer (sclass 3 in MscGSMakeArrayPadIfNeeded).
// cp_*: a by-value struct parameter with or without a pointer member in a function with a GS buffer, the member's
//       target written: does the struct get a $GSCopy$ (ssr.flags & 0x40000000 = "contains pointers"?).
void ext(void*);
void extc(char*);
extern int g;
struct SB { char b[8]; int n; };
struct SP { int* p; int n; };
struct SI { int a; int n; };
void plain_int(int n) { int x[4]; ext(x); }
#pragma strict_gs_check(push, on)
void strict_int(int n) { int x[4]; ext(x); }
void strict_scalar(int n) { int x; ext(&x); }
void strict_none(int n) { g = n; }
#pragma strict_gs_check(pop)
void sp_buf(SB s) { extc(s.b); }
void sp_bufread(SB s) { g = s.b[3]; }
void cp_ptr(SP s) { char b[8]; extc(b); *s.p = 1; }
void cp_int(SI s) { char b[8]; extc(b); *(int*)s.a = 1; }
void cp_ptrread(SP s) { char b[8]; extc(b); g = *s.p; }
void cp_intplain(SI s) { char b[8]; extc(b); g = s.a + s.n; }
void cp_ptrnouse(SP s) { char b[8]; extc(b); g = s.n; }
void cp_ptrpass(SP s) { char b[8]; extc(b); ext(s.p); }
void cp_none(SI s) { g = s.a + s.n; }
// 2.1: which local arrays are GS buffers (array of 1- or 2-byte elements, > 4 bytes, or an aggregate holding one)
struct QC { char b[16]; };
struct QI { int a; int b[8]; };
void q_c4() { char b[4]; extc(b); }
void q_c5() { char b[5]; extc(b); }
void q_s3() { short b[3]; ext(b); }
void q_i2() { int b[2]; ext(b); }
void q_sc() { QC s; ext(&s); }
void q_si() { QI s; ext(&s); }
