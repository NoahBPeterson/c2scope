// slotwant fixture: two address-taken buffers that share one slot only when the first is block-scoped
void ext(char *p);
int g;

void f(int n)
{
    { char a[64];
    ext(a);
    g = a[0]; }
    if (n) {
        char b[64];
        ext(b);
        g += b[1];
    }
    g += n;
}
