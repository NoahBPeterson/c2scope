// x87round.py (docs/c2/x87round.md): one function per reason class. Compiled twice by tests/probes/x87round.py: as is
// ("ours") and with -DORIG ("the original": the cause removed, so the value stays on the x87 stack). /O2 /TP.
struct V { float x, y, z; };
void ext(void);
void ext2(float);
void ext3(double);
extern double gd[16];
extern float gf[4];

#ifdef ORIG
typedef double FL;
#else
typedef float FL;
#endif

// a float local: Lower stores an assigned float to its home (14.00 / 15.00; 13.10 keeps it on the stack)
float f_local(const V *p, V *q, float s) {
    FL r = p->x - q->x;
    q->y = r * s + r;
    q->z = r * r;
    return s;
}

// a float parameter of an inlined helper
static inline float sq(FL a) { return a * a * 2.0f; }
float f_inl(const V *p, V *q, float s) {
    q->y = sq(p->x - q->x) + s;
    return s;
}

// a call between definition and use (the original's side has no call)
float f_call(const V *p, V *q, float s) {
    double r = p->x * s;
#ifndef ORIG
    ext();
#endif
    q->y = (float)(r * r + r);
    return s;
}

// a cast to float
float f_cast(const V *p, V *q, float s) {
#ifdef ORIG
    double t = p->x * s;
#else
    double t = (float)(p->x * s);
#endif
    q->y = (float)(t * t + t);
    return s;
}

// a float returned from a computed value
FL f_ret(float a, float b) {
    return a * b + a;
}

// a float argument
void f_arg(const V *p, float s) {
#ifdef ORIG
    ext3(p->x * s);
#else
    ext2(p->x * s);
#endif
}

// a volatile float
float f_vol(const V *p, V *q, float s) {
#ifdef ORIG
    double v = p->x * s;
#else
    volatile float v = p->x * s;
#endif
    q->y = v * v + v;
    return s;
}

// nine values live at once: the stack is 8 deep
void f_spill(const double *p) {
    double a = p[0]*p[9], b = p[1]*p[9], c = p[2]*p[9], d = p[3]*p[9], e = p[4]*p[9],
           f = p[5]*p[9], h = p[6]*p[9];
#ifdef ORIG
    double i = 1.5, j = 2.5;
#else
    double i = p[7]*p[9], j = p[8]*p[9];
#endif
    gd[0] = a + b + c + d + e + f + h + i + j;
    gd[1] = j - i - h - f - e - d - c - b - a;
}
