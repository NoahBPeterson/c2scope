/* RA_xmmtie: the defOrd tie-break across blocks for XMM values (Color class 6), regalloc.md 3.4 (the XMM
   cross-block case). Compile as C with /arch:SSE2. c is defined and used in the entry block, b after the if,
   with the same instruction shapes (load, multiply, two uses) and the same pressure (P = 2) in their blocks, so
   both have priority 28. Rule: the def in the later block (b, defOrd 23) is popped before c (defOrd 8).
   a (defined in the entry block, used after the if) has fewer references: 20. */
extern int gx; extern double gd[8];

void tx(const double *p)
{
    double a = p[0] * p[3];
    double c = p[2] * p[5];
    gd[4] = c; gd[5] = c + 1.0;
    if (gx) gd[6] = 0.0;
    {
        double b = p[1] * p[4];
        gd[0] = a + b; gd[1] = b - a;
    }
}
