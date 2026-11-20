// RX_rmw13 (loadcse.md section 3): which read-modify-writes Opeq formed survive on 13.10, where the RMW tuple is
// split into load / op / store between @Color@4 and @dop2_CodegenFinish@4. Doc claim [I]: an immediate-operand RMW
// with no other use of the location is kept, the rest split. Each r_* function has one RMW candidate on p->m.
void h(int);
extern int g;
struct S { char pad[92]; int m; int n; volatile int vm; };
void r_imm(S* p)              { p->m += 4; }
void r_reg(S* p, int a)       { p->m += a; }
void r_or_reg(S* p, int a)    { p->m |= a; }
void r_and_imm(S* p)          { p->m &= 0xff00; }
void r_sub_imm(S* p)          { p->m -= 4; }
void r_shl_imm(S* p)          { p->m <<= 2; }
void r_shl_reg(S* p, int a)   { p->m <<= a; }
void r_glob_imm()             { g += 4; }
void r_glob_reg(int a)        { g += a; }
void r_deref_imm(int* q)      { *q += 4; }
void r_deref_reg(int* q, int a) { *q += a; }
int  r_imm_other_read(S* p)   { p->m += 4; return p->n; }
int  r_imm_other_before(S* p) { int t = p->n; p->m += 4; return t; }
void r_imm_two_locs(S* p)     { p->m += 4; p->n += 8; }
void r_imm_call_before(S* p)  { h(p->n); p->m += 4; }
void r_imm_call_after(S* p)   { p->m += 4; h(0); }
void r_imm_read_call(S* p)    { h(p->m); p->m += 4; }
void r_reg_from_mem(S* p)     { p->m += p->n; }
void r_vol_imm(S* p)          { p->vm += 4; }
void r_loop_imm(S* p, int k)  { for (int i = 0; i < k; ++i) p->m += 4; }
void r_imm_twice(S* p)        { p->m += 4; p->m += 8; }
void r_imm_call0_before(S* p) { h(0); p->m += 4; }
void r_glob_call_before()     { h(0); g += 4; }
void r_imm_esi_nocall(S* p, S* q, int k) { for (int i = 0; i < k; ++i) { p->m += 4; q->n = i; } }
void r_imm_esi_after(S* p)    { h(p->n); p->m += 4; h(p->n); }
int  r_imm_two_bases(S* p, S* q) { p->m += 4; q->m += 4; return p->n + q->n; }
void r_reg_ready(S* p, int a) { p->n = a; p->m += a; }
void r_reg_ready2(S* p, int a, int b) { int t = a * b; p->n = t; p->m |= t; }
int  r_imm_result(S* p)       { p->m = p->m + 4; return p->m; }
