// R420 (f3decomp): which float constants narrow `(double)f > c` back to a float compare (fcomp DWORD) on 14.00/15.00.
// Deciding code: OptPeeps' OPCMP case -> OptDoubleConstMakeSingle (14.00 10bb12a0, 15.00 510fe3d0). It range-checks the
// constant against +-FLT_MAX by reading the low 8 bytes of the 80-bit constant (its 64-bit significand) as a double,
// so a float-exact constant narrows iff its significand 1.f is below 1 + 0x47f/2048 = 1.56201171875 (top 11 fraction
// bits <= 0x47e). 13.10: the front end compares floats as floats (every constant is DWORD). docs/c2/x87.md rule 10.
// cN: constant on the right; l*: on the left; m*: member operand; mul*: multiply (never narrowed on 14.00/15.00).
float get();
int c0() { return get() > 0.0f; } // 0
int c1() { return get() > 1.0f; } // 1
int c2() { return get() > 2.0f; } // 2
int c3() { return get() > 3.0f; } // 3
int c4() { return get() > 4.0f; } // 4
int c5() { return get() > 5.0f; } // 5
int c6() { return get() > 6.0f; } // 6
int c7() { return get() > 7.0f; } // 7
int c8() { return get() > 8.0f; } // 8
int c9() { return get() > 9.0f; } // 9
int c10() { return get() > 10.0f; } // 10
int c11() { return get() > 12.0f; } // 12
int c12() { return get() > 25.0f; } // 25
int c13() { return get() > 50.0f; } // 50
int c14() { return get() > 64.0f; } // 64
int c15() { return get() > 99.0f; } // 99
int c16() { return get() > 100.0f; } // 100
int c17() { return get() > 101.0f; } // 101
int c18() { return get() > 128.0f; } // 128
int c19() { return get() > 200.0f; } // 200
int c20() { return get() > 255.0f; } // 255
int c21() { return get() > 1000.0f; } // 1000
int c22() { return get() > 16777216.0f; } // 16777216
int c23() { return get() > -1.0f; } // -1
int c24() { return get() > -100.0f; } // -100
int c25() { return get() > 0.5f; } // 0.5
int c26() { return get() > 0.25f; } // 0.25
int c27() { return get() > 0.125f; } // 0.125
int c28() { return get() > 0.75f; } // 0.75
int c29() { return get() > 1.5f; } // 1.5
int c30() { return get() > 0.3f; } // 0.3
int c31() { return get() > 0.15f; } // 0.15
int c32() { return get() > 0.01f; } // 0.01
int c33() { return get() > 0.001f; } // 0.001
int c34() { return get() > 1.1f; } // 1.1
int c35() { return get() > 1e10f; } // 1e10
int c36() { return get() > 0.1f; } // 0.1
int c37() { return get() > -0.1f; } // -0.1
int c38() { return get() > 0.2f; } // 0.2
int c39() { return get() > 0.9f; } // 0.9
int c40() { return get() > 1.5625f; } // 1.5625
int c41() { return get() > 1.5620115995407104f; } // 1.5620115995407104
int c42() { return get() > 1.56201171875f; } // 1.56201171875
int c43() { return get() > 1.56152343750f; } // 1.56152343750
int c44() { return get() > 3.1240234375f; } // 3.1240234375
int c45() { return get() > 3.123046875f; } // 3.123046875
struct P { float x; };
int l99() { return 99.0f < get(); }
int l1() { return 1.0f < get(); }
int m99(P* p) { return p->x < 99.0f; }
int m100(P* p) { return p->x < 100.0f; }
float mul6(P* p) { return p->x * 6.0f; }
