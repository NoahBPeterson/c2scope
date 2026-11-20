// opt.md §7 rule 7: a constant-size memcpy written at the site is a block assignment before c2's first pass.
// Mechanism probe: is it c2's reader (RdrMemcpy) or the front end?
#include <string.h>
struct R { int a, b; };
void cp_site(R* c, const R* src) { memcpy(c, src, sizeof(R)); }   // OPMBASSIGN at the first dump
void cp_var(R* c, const R* src, unsigned n) { memcpy(c, src, n); } // OPINTRINSIC / call
void cp_assign(R* c, const R* src) { *c = *src; }                 // block assignment from the front end
