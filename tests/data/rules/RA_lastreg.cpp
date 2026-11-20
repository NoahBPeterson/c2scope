// RA_lastreg: ChoosePhysReg's +100*benefit weight on a neighbour's last free register (regalloc.md 4, 4.2;
// validation.md "Not tested, or open"; tools/rasim/src/choose.rs NbrLast).
// c is a byte value live across calls, so its avail set is {ebx} (the only callee-saved byte register).
// a, b, d are coloured first (more references). Without the term the third of them would take ebx
// (esi, edi, ebx, ebp order); with it that one sees ebx:+100*benefit(c) and takes ebp, and c gets ebx.
// /DINTC: c is an int (avail esi edi ebx ebp): no term, the third takes ebx and c ebp.
// /DINTC /Oy-: no ebp; c's last register is ebx once a and b are coloured, d sees ebx:600 but has only ebx.
void v(int);
int g(int);
#ifdef INTC
typedef int CT;
extern int *gp;
#else
typedef char CT;
extern char *gp;
#endif

void lr(int a, int b, int d)
{
    CT c = (CT)g(0);
    v(a); v(b); v(d);
    v(a); v(b); v(d);
    v(a); v(b); v(d);
    gp[0] = c;
    v(a); v(b); v(d);
    gp[1] = c;
}
