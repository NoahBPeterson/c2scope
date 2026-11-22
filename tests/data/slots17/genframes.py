"""genframes.py SEED N OUT.cpp: N random functions for the x64 frame kinds of docs/c2/slots.md 6 (cl 17.00): locals of
mixed sizes, over-aligned locals (__declspec(align(32 / 64)): dynamic alignment), _alloca (an rbp frame), calls with
many stack arguments (an argument area above 0xf0: B = min(A, 0xf0) < A), parameters whose address is taken, and
call-free functions ending in a tail call with locals in the parameter homes (the argument area is removed)."""
import random, sys
seed, n, out = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
R = random.Random(seed)
TY = [("char", 1), ("short", 2), ("int", 4), ("double", 8), ("S12", 12), ("S40", 40), ("S200", 200), ("int", 4),
      ("V32", 32), ("V64", 64)]
L = ["#include <malloc.h>", "struct S12 { int v[3]; }; struct S40 { int v[10]; }; struct S200 { int v[50]; };",
     "struct __declspec(align(32)) V32 { int v[8]; }; struct __declspec(align(64)) V64 { int v[16]; };",
     "void ext(void *); int ext2(int); int ext3(int, int); int many(%s); int g;" % ", ".join(["int"] * 34),
     "struct P2 { int a, b; };"]
for f in range(n):
    if R.random() < 0.2:      # call-free but for a tail call: the argument area goes (TryRemoveArgumentAreaFromStackFrame)
        params = R.randint(2, 4)
        ps = ", ".join("int p%d" % k for k in range(params))
        body = R.choice(["    P2 q = { p0, p1 };\n    P2 r = q;\n    return ext3(r.a + %d, r.b);" % R.randint(1, 9),
                         "    volatile int x = p%d;\n    return ext3(p1, %d);" % (R.randrange(params), R.randint(1, 9)),
                         "    volatile short x = (short)p0;\n    volatile char y = (char)p1;\n    return ext3(p1, p0);"])
        L.append("int f%d(%s) {\n%s\n}" % (f, ps, body))
        continue
    params = R.randint(1, 4)
    ps = ", ".join("int p%d" % k for k in range(params))
    body, k = [], 0
    alloca, aligned, bigcall = R.random() < 0.6, R.random() < 0.5, R.random() < 0.4
    for _ in range(R.randint(1, 6)):
        t, sz = R.choice(TY if aligned else TY[:8])
        v = "v%d" % k
        k += 1
        body += ["    %s %s;" % (t, v), "    ext(&%s);" % v]
        body += ["    g += %s;" % v if t in ("char", "short", "int") else "    g += (int)%s;" % v if t == "double" else
                 "    g += %s.v[%d];" % (v, R.randint(0, 2))] * R.randint(0, 2)
    if alloca:
        body += ["    int *a = (int *)_alloca(p0 * 4 + 4);", "    a[0] = g;", "    ext(a);"]
    if R.random() < 0.4:
        body.append("    ext(&p%d);" % R.randrange(params))
    if bigcall:
        body.append("    g += many(%s);" % ", ".join("p%d + %d" % (R.randrange(params), i) for i in range(34)))
    L.append("int f%d(%s) {\n%s\n    return g;\n}" % (f, ps, "\n".join(body)))
open(out, "w").write("\n".join(L) + "\n")
