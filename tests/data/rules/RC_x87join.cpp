// regalloc_cost.md 8 (X87MapGFxToSTx, 14.00/15.00; x87.md 11): at a join the first predecessor mapped (the
// fall-through arm) passes its exit stack layout on as the join's layout; the other arm gets the fxch's on its edge.
// j1: each arm stores a different value, which the mapper brings to ST0 first; the tail is long enough not to be
// duplicated. 13.10 has no X87MapGFxToSTx (FPColor maps the stack).
extern double g1, g2, gd[16];
void j1(double a, double b, int k)
{
    double x = a + 1.0, y = b + 2.0;
    if (k) { g1 = x; }
    else   { g2 = y; }
    gd[0] = x * y; gd[1] = x - y; gd[2] = y - x; gd[3] = x / y; gd[4] = y / x; gd[5] = x * x; gd[6] = y * y;
}
