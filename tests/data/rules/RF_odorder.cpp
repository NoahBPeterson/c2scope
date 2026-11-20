// frame.md 1 (StkStackAlloc, the /Od path): AllocateAutomatics gives automatics their slots in _LocalSyms order.
// Compile with /Od. Which order is it: declaration, first use, name? Four ints, each defined once and read once:
// declared y w z x (/DREV: x z w y), defined z x w y; then two disjoint nested blocks' locals.
int ext(int);
int od(int n)
{
#ifdef REV
    int x, z, w, y;
#else
    int y, w, z, x;
#endif
    z = ext(n);
    x = ext(1);
    w = ext(2);
    y = ext(3);
    { int d = ext(4); n += d; }
    { int e = ext(5); n += e; }
    return x + y + z + w + n;
}
