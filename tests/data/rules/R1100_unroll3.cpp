// R1100: "a constant-count loop over 3 floats is not unrolled under /O2" (f3decomp 0x00715d80). LurWorthIt /
// LurDetermineDoUnrollAmount (opt.md §8.7): trip count <= 10, at most 4 blocks, under 45 tuples, no call, step +-1,
// innermost, a `for`/`while` (not `do`) -> completely unrolled.
// Each function's listing is checked for a backward jump (a loop that stayed).
extern float K;
float Kf();
void Sink(float);
struct V3 { float v[3]; void scale(const float* d, float k); };

void u_glob(float* r, const float* d) {            // global scale: unrolled
    for (int i = 0; i < 3; i++) r[i] = d[i] * K;
}
void u_param(float* r, const float* d, float k) {  // parameter scale: unrolled
    for (int i = 0; i < 3; i++) r[i] = d[i] * k;
}
void V3::scale(const float* d, float k) {          // member array: unrolled
    for (int i = 0; i < 3; i++) v[i] = d[i] * k;
}
void u_dbl(double* r, const double* d, double k) { // double: unrolled
    for (int i = 0; i < 3; i++) r[i] = d[i] * k;
}
void u_call(float* r, const float* d) {            // a call in the body: never unrolled (rule 1)
    for (int i = 0; i < 3; i++) r[i] = d[i] * Kf();
}
void u_sink(const float* d, float k) {             // the call is the loop's use: never unrolled
    for (int i = 0; i < 3; i++) Sink(d[i] * k);
}
void u_do(float* r, const float* d, float k) {     // do-while: not unrolled (LurUnrollDoWhileLoops = 0)
    int i = 0;
    do { r[i] = d[i] * k; } while (++i < 3);
}
void u_ptr(float* r, const float* d, float k) {    // pointer bumping, `n > 0` down-count: unrolled
    for (int n = 3; n > 0; --n) *r++ = *d++ * k;
}
void u_ne(float* r, const float* d, float k) {     // exit test `!=`: not unrolled (LurCanonicalBottomTested)
    for (int i = 0; i != 3; i++) r[i] = d[i] * k;
}
void u_end(float* r, const float* d, float k) {    // pointer end test, no int counter: not unrolled
    for (float* e = r + 3; r < e; ) *r++ = *d++ * k;
}
void u_copy(float* r, const float* d) {            // a pure copy (loads == stores, no float op): trip 3 still <= 10
    for (int i = 0; i < 3; i++) r[i] = d[i];
}
