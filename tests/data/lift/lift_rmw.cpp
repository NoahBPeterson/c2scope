// lift.py probe: the same function with `= 1` and `|= 1` on a member; lifting the second one's code against the
// first one's IL at Lower's entry must name the difference at that statement (ours st, original rmw.or).
struct S { int a, b, c; };
int g(int);

void f_set(S* p, int n)
{
    if (n > 3)
        p->b = g(n);
    p->a = 1;
    p->c = n + 2;
}

void f_or(S* p, int n)
{
    if (n > 3)
        p->b = g(n);
    p->a |= 1;
    p->c = n + 2;
}
