/* schedwant17.c with q[0] = y moved below the store to s->b (the "original" of sched17_schedwant_tests). */
struct S { int a, b, c, d; };
void swmove(struct S *s, int *q, int x, int y)
{
    s->a = x;
    s->b = s->c * 3 + s->d;
    q[0] = y;
}
