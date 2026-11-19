/* cl 13.10's Riscify: which splits survive (docs/c2/regalloc.md 6.6). Both functions split `p[i] |= b` into
   load, or, store. In rmw0 the three stay next to each other through Schedule, so Riscify_undo (PreEncode) puts
   the memory operand back; in rmw the scheduler moves other instructions between them, so the split stays. */
int ext(int);
void rmw(int *p, int k, int i, int *o)
{
    int a = ext(k), b = ext(i);
    o[0] = a;
    p[i] |= b;
}
void rmw0(int *p, int i, int b)
{
    p[i] |= b;
}
