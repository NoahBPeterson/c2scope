// frontend_il.md rule 1.13: does a sparse switch (split by c2's reader into compares around smaller
// OPSWITCHes, rule 1.12) depend on the source order of its cases?  Same case set, three orders each.
// p*: cases 1, 100, 1000, 5000 (four singleton cases, no table); q*: 1..4 dense plus 1000 and 5000.
int h(int);
int p1(int x) { switch (x) { case 1: return h(1); case 100: return h(2); case 1000: return h(3); case 5000: return h(4); } return 0; }
int p2(int x) { switch (x) { case 5000: return h(4); case 1000: return h(3); case 100: return h(2); case 1: return h(1); } return 0; }
int p3(int x) { switch (x) { case 1000: return h(3); case 1: return h(1); case 5000: return h(4); case 100: return h(2); } return 0; }
int q1(int x) { switch (x) { case 1: return h(1); case 2: return h(2); case 3: return h(3); case 4: return h(5); case 1000: return h(6); case 5000: return h(7); default: return h(0); } }
int q2(int x) { switch (x) { case 5000: return h(7); case 1000: return h(6); case 4: return h(5); case 3: return h(3); case 2: return h(2); case 1: return h(1); default: return h(0); } }
int q3(int x) { switch (x) { case 3: return h(3); case 1000: return h(6); default: return h(0); case 1: return h(1); case 5000: return h(7); case 2: return h(2); case 4: return h(5); } }
// r*: two sparse cases plus default (a compare chain only)
int r1(int x) { switch (x) { case 7: return h(1); case 900: return h(2); default: return h(0); } }
int r2(int x) { switch (x) { case 900: return h(2); case 7: return h(1); default: return h(0); } }
int r3(int x) { switch (x) { default: return h(0); case 900: return h(2); case 7: return h(1); } }
