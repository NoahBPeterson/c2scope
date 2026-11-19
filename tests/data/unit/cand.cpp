#include <windows.h>
#include <string.h>
#define UNIT_UNUSED 1
// candidate for the composed-unit test: windows.h and string.h move to the top, the #define is dropped
int Unit_Sum(Pair *p, int n)
{
    int a = 0;
    Ext(&a);
    Pair q;
    memset(&q, 0, sizeof q);
    Ext(&q);
    for (int i = 0; i < n; i++)
        a += p[i].a + q.b;
    g_unit_total += a;
    return a + (int)GetTickCount();
}
