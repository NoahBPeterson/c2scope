/* cl 13.10's Riscify (after RegSimpleAssign): a memory operand split off into a load to a new temp, which gets its
   register from GetReg (a rotation over eax ecx edx esi edi ebx ebp restarted at eax in every block, the block
   walked from its last instruction up). Neither a Color globreg nor a RegAssign pick (tracer.md `scratch`).
   The split stays when the scheduler moves something between the load and its use (Riscify_undo puts it back
   otherwise; regalloc.md 6.6, riscify2.c).
   Shape of Mount & Blade 0x529c30: `mov esi, [edx+ecx*4]; add eax, esi`. */
int ext(int);
struct P { int pad[20]; int *tbl; };
int pick(struct P *p, int k, int i)
{
    int j, s = 0;
    for (j = 0; j < k; j++)
        s += ext(j);
    return (k & 15) * 23240 + p->tbl[i >> 4] + s;
}
int pick2(int *tbl, int k, int i, int *o)
{
    int a = ext(k), b = ext(i);
    o[0] = a;
    o[1] = b;
    return (k & 15) * 23240 + tbl[i >> 4];
}
