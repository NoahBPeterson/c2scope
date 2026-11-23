/* schedwant.py on x64 (cl 17.00, tools/c2sched17.py's model): q[0] = y is a store through another pointer
   argument, so s->c's load waits for it (their points-at sets meet). schedwant17b.c is the same function with
   that store moved below s->b's: compiled, it is the "original" whose order schedwant finds again (move:2>6). */
struct S { int a, b, c, d; };
void swmove(struct S *s, int *q, int x, int y)
{
    s->a = x;
    q[0] = y;
    s->b = s->c * 3 + s->d;
}
