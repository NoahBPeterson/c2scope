// incsnap fixture (tests/run_tests.py incsnap_tests): a unit whose quoted header includes another one by a
// relative path and an angle-bracket header from an /I dir.
#include "inc/a.h"

int sum_pair(Pair* p, int n)
{
    int s = 0;
    for (int i = 0; i < n; i++)
        s += p[i].a * PAIR_SCALE + p[i].b;
    return s;
}
