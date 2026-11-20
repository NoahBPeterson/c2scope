// opt.md §3 rule 4a: in a function with a C++ EH frame a single-use named local defined where an EH region
// can still be entered stays a variable (a Color candidate); without the EH frame it is substituted.
// Compile with /EHsc. Mechanism probe: GoptSingleUseCopyPropLegal's answer for `t`.
struct D { D(); ~D(); int v; };
int h(int);
void hv(int*);
struct P { int pad[9]; int m; };
int su_eh(P* p) { D d; if (h(7) > 3) hv(&d.v); int t = p->m; return t + 1; }     // _t stays
int su_ctl(P* p) { if (h(7) > 3) hv(0); int t = p->m; return t + 1; }             // _t substituted
int su_after(P* p) { { D d; if (h(7) > 3) hv(&d.v); } int t = p->m; return t + 1; } // after the scope: substituted
