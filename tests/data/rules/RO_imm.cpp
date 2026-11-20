// opt.md §7 rule 5: the same constant in two or more instructions that carry a displacement goes into a
// register; without a displacement never; across a call it takes four uses.
struct S { int a, b, c, d, e[8]; };
void hc();
void hp(int, int, int);
void im_bc(S* p) { p->b = 0x12345; p->c = 0x12345; }                       // register
void im_ab(S* p) { p->a = 0x12345; p->b = 0x12345; }                       // immediates (offset 0 has no disp)
void im_nd(int* a, int* b, int* c) { *a = 0x12345; *b = 0x12345; *c = 0x12345; }   // immediates
void im_abc(S* p) { p->a = 0x12345; p->b = 0x12345; p->c = 0x12345; }     // register
void im_add(S* p) { p->b += 0x12345; p->c += 0x12345; }                    // register (memory destination)
void im_push(S* p) { hp(0x12345, 0x12345, 0x12345); }                      // push imm
void im_call3(S* p) { p->b = 0x12345; hc(); p->c = 0x12345; p->d = 0x12345; }               // immediates
void im_call4(S* p) { p->b = 0x12345; hc(); p->c = 0x12345; p->d = 0x12345; p->e[1] = 0x12345; } // callee-saved
