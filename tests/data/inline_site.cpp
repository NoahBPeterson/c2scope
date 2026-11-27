// per-caller / per-site reasons why one caller calls a helper that its sibling inlines (docs/c2/inline.md, 13.10, /EHsc)
volatile int g;
int ext(int);
struct Dt { Dt(); ~Dt(); int q; };

// 1. a callee with unwind state (a local with a destructor) is a candidate only for a caller that has EH state itself
int k_dtor(int a) { Dt d; return a + d.q; }
int e_none(int x)  { return k_dtor(x) + 1; }                 // no EH state: not a candidate, call
int e_local(int x) { Dt d; return k_dtor(x) + d.q; }         // object with a destructor in the caller: inlined

// 2. block weight: h has c = 42 (no early accept); each if-arm halves the weight, v = (d*1e8/w + 6d)/7: 3 arms call
int h(int a) { g = a; g = a + 1; g = a + 2; g = a + 3; g = a + 4; g = a + 5; return a; }
int w0(int x) { return h(x); }
int w2(int x) { if (x > 0) { if (x > 1) { return h(x); } } return 0; }
int w3(int x) { if (x > 0) { if (x > 1) { if (x > 2) { return h(x); } } } return 0; }

// 3. a site inside a catch handler stays a call unless d < 14 (h: call; the tiny t: inlined); __try bodies take size <= 10 only
int t(int a) { return a + 1; }
int c_hand(int x)  { try { return ext(x); } catch (...) { return h(x); } }
int c_hand_t(int x){ try { return ext(x); } catch (...) { return t(x); } }
int c_body(int x)  { try { return h(x); } catch (...) { return 0; } }
int s1(int a) { g = a; return a; }
int s_try_h(int x) { __try { return h(x); } __except (1) { return 0; } }
int s_try_s(int x) { __try { return s1(x); } __except (1) { return 0; } }

// 4. #pragma inline_depth(2): the third nesting level stays a call
#pragma inline_depth(2)
inline int d0(int a) { g = a; return a; }
inline int d1(int a) { return d0(a) + 1; }
inline int d2(int a) { return d1(a) + 1; }
inline int d3(int a) { return d2(a) + 1; }
int d_top(int x) { return d3(x); }

// 5. unwind state comes from the class layout: the ctor/dtor of a class with a member that has a destructor has it
//    (no EH code in the helper itself); the compiler-generated ??_G that calls ~NavLike has no EH state: no site
struct NavLike { Dt a; int x; };
int l_nav(int x)  { NavLike n; return n.x + x; }
int d_nav(NavLike *p) { delete p; return 1; }
