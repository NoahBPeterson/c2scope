// RE_file_static: file-scope dynamic initialisers on 13.10 (_$E<n>): docs/c2/port-1310.md "File-scope static initialisers".
// cl /c /O2 /FAsc; read the _$E<n> PROCs, CRT$XCU, _DATA/_BSS.
int f(int);
struct A { int x, y; A() : x(1), y(2) {} };              // inline constructor
struct B { int x; B(); };                                 // out-of-line constructor
struct C { int x; C() : x(3) {} ~C(); };                  // inline ctor + destructor
struct V { int x; V(int v) : x(v) {} };                   // constructor with an argument
A ga; B gb; C gc; V gv(7);
int gi = gb.x + 1;                                        // a dynamic scalar
struct D { int x; D(); ~D(); };                          // out-of-line ctor + dtor
struct E { int x, y; E() : x(f(1)), y(2) {} };            // inline ctor with a call
D gd; E ge;
