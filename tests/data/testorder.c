/* regmap / scratch --phase against capstone text: `test r8, r8` is `test reg, r/m` in the listing and `test r/m, reg`
   in capstone (the original's orig.s). Our own code as the "original" must agree everywhere; read from the listing's
   text, a and b each got one agreeing and one swapped vote: two false WANTS and a false swap (tracer.md `regmap`;
   Mount & Blade 0x46bc70). */
unsigned char g1(void);
unsigned char g2(void);

int both(void)
{
    unsigned char a = g1();
    unsigned char b = g2();
    if (a & b)
        g1();
    return 0;
}
