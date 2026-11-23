// R865 (f3decomp): Globregs puts a register parameter's enter-load (OPENTERLOAD) at the top of the entry block
// only in a function that may get a private convention (static, address not taken); in an ordinary function it
// goes where the register region starts. docs/c2/regalloc.md, ltcg.md section 2.2, rules_xref.md.
// /DSTATIC: late is static noinline with a caller; default: an external function, same body.
void ext1();
int ext2(int);
#ifdef STATIC
#if _MSC_VER < 1300
static
#else
static __declspec(noinline)
#endif
#endif
int late(int a, int* info)
{
    int s = ext2(a);
    ext1();
    s += ext2(s);
    for (int i = 0; i < a; i++)
        s += info[i];
    return s;
}
#ifdef STATIC
int use(int a, int* p) { return late(a, p) + 1; }
#endif
