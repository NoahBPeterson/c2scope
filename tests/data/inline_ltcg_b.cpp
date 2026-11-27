#include "inline_ltcg.h"
T::T() { x = 3; g = 1; }
T::~T() { x = 0; g = 2; }
int far_helper(int a) { g = a; g = a + 2; return a; }
int far_tpl(int *a, int n) { It<int> i(a); return *(i + n); }
