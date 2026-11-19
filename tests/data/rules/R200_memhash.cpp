// R200 vs R450: the hash (low 16 bits of the operand-sort weight, OptHashNode) of a memory leaf [base+d].
// Two offset-0 members multiplied: both operands are plain memory leaves (OPBASED), hash = d + (0x27c - 0x274)
// + (hash(base) << 8) = 8 + (32*key << 8): p (key 2) -> 0x4008, q (key 1) -> 0x2008. A member at d != 0 is still an
// address temporary `p + d` (OPADD) at the sort, hashed h(p) + 2*d + (0x2a5 - 0x274 = 0x31): the R200 constant 0x31.
// Run: uv run tools/symkeys.py sites tests/data/rules/R200_memhash.cpp --func mh_ -- /TP /O2
struct S { int a; int b; int c; };

int mh_zero(S* p, S* q) { return p->a * q->a; }      // [p+0] 0x4008 vs [q+0] 0x2008
int mh_off(S* p, S* q) { return p->c * q->b; }       // address temps: h(p)+2*8+0x31 vs h(q)+2*4+0x31
