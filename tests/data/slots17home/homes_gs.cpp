// with /GS: a function with a char buffer has a cookie and offers no home; without one nothing changes
void ext(long long *);
void extc(char *);
void use(long long);
void gs_nobuf(long long a, long long b) { long long x; ext(&x); use(x); }
void gs_buf(long long a, long long b) { char buf[16]; long long x; extc(buf); ext(&x); use(x); }
