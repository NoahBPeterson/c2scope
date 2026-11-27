#include "inline_ltcg.h"
volatile int g;
int late_helper(int);
int a_late(int x)   { return late_helper(x) + 1; }            // same module, helper defined after its caller
int late_helper(int a) { g = a; g = a + 1; return a; }
int a_far(int x)    { return far_helper(x) + 1; }              // helper in the other module
int a_tpl(int *a, int n) { It<int> i(a); return *(i + n); }     // template operator+ in this module
int a_tfar(int *a, int n) { return far_tpl(a, n) + 1; }
int a_vec(int n)    { T *p = new T[n]; int r = p[0].x; delete[] p; return r; }   // ??_ET, ??_H-like vector ctor loop: T's ctor/dtor are in h.cpp
int a_lvec(int n)   { L *p = new L[n]; int r = p[0].x; delete[] p; return r; }   // everything in this module
