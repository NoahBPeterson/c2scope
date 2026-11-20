// frontend_il.md 3B rule 10, last sentence: `x(v)` in the mem-initialiser list against `x = v;` as the first body
// statement. Claim: same stream position for trailing scalars; different when a class member (or a vptr store)
// lies between. Pairs a/b have the same layout and differ only in that spelling.
struct M { M(); int m; };
void h(int);
struct T1a { int a; int x; T1a(int v); };           T1a::T1a(int v) : a(1), x(v) {}
struct T1b { int a; int x; T1b(int v); };           T1b::T1b(int v) : a(1) { x = v; }
struct T2a { int x; M m; T2a(int v); };             T2a::T2a(int v) : x(v) {}
struct T2b { int x; M m; T2b(int v); };             T2b::T2b(int v) { x = v; }
struct T3a { int x; virtual void f(); T3a(int v); }; T3a::T3a(int v) : x(v) { h(0); }
struct T3b { int x; virtual void f(); T3b(int v); }; T3b::T3b(int v) { x = v; h(0); }
struct T4a { M m; int x; int y; T4a(int v); };      T4a::T4a(int v) : x(v), y(2) { h(1); }
struct T4b { M m; int x; int y; T4b(int v); };      T4b::T4b(int v) : y(2) { x = v; h(1); }
struct T5a { int x; int y; T5a(int v); };           T5a::T5a(int v) : x(v), y(v + 1) { h(y); }
struct T5b { int x; int y; T5b(int v); };           T5b::T5b(int v) : x(v) { y = v + 1; h(y); }
