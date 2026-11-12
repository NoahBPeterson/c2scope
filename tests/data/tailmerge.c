/* Tail merging across a call and across a scratch register (docs/c2/il_fg.md 7.5). Each function has three arms
 * that jump to one join; the join is longer than 20 bytes so FgCodeMotion does not copy it back into the arms. */
extern int v1, w1, w2, w3, w4, gv, gx, gy; extern short s1; extern char b1;
void f(int *); void f1(int *); void f2(int *); void h(int);
static __inline void f2fwd(int *p) { f1(p); }      /* a forwarder: arm 2 calls f1 too */
#define JOIN h(0); h(w4); v1 = 3; w1 = 4; h(2);
/* c1: the same callee in every arm: the walk goes back past the call: s1 9 + push 5 + call 5 + add 3 + b1 7 > 20 */
void c1(int x) { if (x == 1) { w1 = 11; s1 = 7; f(&gv); b1 = 21; } else if (x == 2) { w2 = 11; s1 = 7; f(&gv); b1 = 21; }
                 else if (x == 3) { w3 = 11; s1 = 7; f(&gv); b1 = 21; } else w4 = 9; JOIN }
/* c2: twins f1 / f2 (identical bodies elsewhere, folded by /OPT:ICF): the walk stops at the call, add 3 + b1 7 + jmp 5 */
void c2(int x) { if (x == 1) { w1 = 11; s1 = 7; f1(&gv); b1 = 21; } else if (x == 2) { w2 = 11; s1 = 7; f2(&gv); b1 = 21; }
                 else if (x == 3) { w3 = 11; s1 = 7; f1(&gv); b1 = 21; } else w4 = 9; JOIN }
/* c3: c2 with arm 2 calling through the forwarder: one symbol again, merged like c1 */
void c3(int x) { if (x == 1) { w1 = 11; s1 = 7; f1(&gv); b1 = 21; } else if (x == 2) { w2 = 11; s1 = 7; f2fwd(&gv); b1 = 21; }
                 else if (x == 3) { w3 = 11; s1 = 7; f1(&gv); b1 = 21; } else w4 = 9; JOIN }
/* r1: the address of a local in every arm: `lea REG, a; push REG`; one scratch pick per arm, so the rotation gives
 * the arms eax, ecx, edx and every pair stops at the push */
void r1(int x) { int a = x; if (x == 1) { w1 = 11; s1 = 7; f(&a); b1 = 21; } else if (x == 2) { w2 = 11; s1 = 7; f(&a); b1 = 21; }
                 else if (x == 3) { w3 = 11; s1 = 7; f(&a); b1 = 21; } else w4 = 9; h(a); JOIN }
/* r2: r1 with two more scratch picks before the lea in every arm: three picks per arm, so each lea gets the same
 * register and the walk goes on to the lea */
void r2(int x) { int a = x; if (x == 1) { w1 = gx + 1; w2 = gy + 1; f(&a); b1 = 21; } else if (x == 2) { w2 = gx + 2; w3 = gy + 2; f(&a); b1 = 21; }
                 else if (x == 3) { w3 = gx + 3; w1 = gy + 3; f(&a); b1 = 21; } else w4 = 9; h(a); JOIN }
