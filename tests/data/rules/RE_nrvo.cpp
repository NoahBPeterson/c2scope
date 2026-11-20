// frontend_il.md 3A rule 8: MscRVO's acceptance test. Inferred criterion: the copy into the return slot is elided
// iff the function has exactly one OPRET and its value is one non-static local. Each n* is a shape; elided = no
// call to the copy constructor ??0K@@QAE@ABV0@@Z in its listing. Compile with /EHsc.
struct K { K(int); K(const K&); ~K(); int v; };
void h(); int h2();
K n1(int a)  { K r(a); h(); return r; }                                         // one return: yes
K n2(int a)  { K r(a); if (a) return r; h(); return r; }                        // two returns, same local
K n3(int a)  { K r(a); K s(a); if (a) return r; return s; }                     // two locals
K n4(int a)  { K r(a); if (a) return K(0); return r; }                          // temporary on one path
K n5(int a)  { K r(a); if (a) throw 1; return r; }                              // throw path: yes
K n6(int a)  { K r(a); return a ? r : r; }                                      // one return, value a ?: temp
K n7(int a)  { K r(a); return (h(), r); }                                       // comma expression
K n8(int a)  { K r(a); switch (a) { case 1: h(); break; case 2: r.v = 3; break; } return r; }
K n9(int a)  { K r(a); while (a--) { if (h2()) goto out; } out: return r; }
K n10(int a) { K r(a); K& q = r; return q; }                                    // through a reference
K n11(int a) { K r(a); return K(r); }                                           // explicit copy
K n12(int a) { if (a) { K r(a); h(); return r; } K s(1); return s; }            // two locals, disjoint scopes
K n13(int a) { K r(a); do { if (h2()) break; h(); } while (a--); return r; }
