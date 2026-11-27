volatile extern int g;
template <class T> struct It { T *p; It(T *a) : p(a) {} It operator+(int n) const { return It(p + n); } T &operator*() const { return *p; } };
struct T { T(); ~T(); int x; };            // ctor/dtor defined in h.cpp (another module)
struct L { L() { x = 1; } ~L() { x = 0; } int x; };   // inline ctor/dtor: everything local
int far_helper(int);                         // defined in h.cpp
int far_tpl(int *a, int n);                  // uses It<int> in h.cpp
