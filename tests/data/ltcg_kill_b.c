/* LTCG regression probe (docs/c2/ltcg.md): callees in their own object. */
#define NI __declspec(noinline)
int ext(int);                                   /* no body anywhere: a call to it kills eax, ecx, edx */
extern int g;
NI int k_eax(int a) { return a * 5; }           /* nothing outside reaches it: custom convention, a in eax; writes eax */
NI int k_std(int a)
{
#pragma comment(linker, "/EXPORT:" __FUNCDNAME__)
    return a * 5;                               /* exported: standard convention, still writes only eax */
}
NI int k_ext(int a) { return ext(a) + 2; }      /* calls code without a body: kills everything */
