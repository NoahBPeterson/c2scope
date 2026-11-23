/* A printf-style formatter: a state machine over the format string, integer conversion, padding. */
typedef struct Buf { char *p; int n; int cap; int over; } Buf;
extern long long arg_int(void *ap, int idx);
extern const char *arg_str(void *ap, int idx);

static void put(Buf *b, int c)
{
    if (b->n < b->cap) b->p[b->n] = (char)c;
    else b->over++;
    b->n++;
}

static int utoa(unsigned long long v, int base, int upper, char *out)
{
    const char *dig = upper ? "0123456789ABCDEF" : "0123456789abcdef";
    char tmp[24];
    int n = 0, i;
    do { tmp[n++] = dig[v % (unsigned)base]; v /= (unsigned)base; } while (v);
    for (i = 0; i < n; i++) out[i] = tmp[n - 1 - i];
    return n;
}

int format(Buf *b, const char *f, void *ap)
{
    int idx = 0, written = 0;
    while (*f) {
        int c = *f++, width = 0, left = 0, zero = 0, prec = -1, lng = 0;
        if (c != '%') { put(b, c); written++; continue; }
        for (;;) {
            c = *f;
            if (c == '-') { left = 1; f++; }
            else if (c == '0') { zero = 1; f++; }
            else break;
        }
        while (*f >= '0' && *f <= '9') width = width * 10 + (*f++ - '0');
        if (*f == '.') { f++; prec = 0; while (*f >= '0' && *f <= '9') prec = prec * 10 + (*f++ - '0'); }
        while (*f == 'l') { lng++; f++; }
        c = *f++;
        switch (c) {
        case 'd': case 'i': case 'u': case 'x': case 'X': case 'o': {
            char num[24];
            long long v = arg_int(ap, idx++);
            int neg = 0, n, pad, base = c == 'x' || c == 'X' ? 16 : c == 'o' ? 8 : 10;
            unsigned long long u;
            if ((c == 'd' || c == 'i') && v < 0) { neg = 1; u = (unsigned long long)-v; }
            else u = (unsigned long long)v;
            if (!lng) u &= 0xffffffffu;
            n = utoa(u, base, c == 'X', num);
            if (prec > n) pad = prec - n; else pad = 0;
            width -= n + pad + neg;
            if (!left && !zero) while (width-- > 0) { put(b, ' '); written++; }
            if (neg) { put(b, '-'); written++; }
            if (!left && zero) while (width-- > 0) { put(b, '0'); written++; }
            while (pad-- > 0) { put(b, '0'); written++; }
            { int i; for (i = 0; i < n; i++) put(b, num[i]); }
            written += n;
            while (left && width-- > 0) { put(b, ' '); written++; }
            break;
        }
        case 's': {
            const char *s = arg_str(ap, idx++);
            int n = 0;
            while (s[n] && (prec < 0 || n < prec)) n++;
            width -= n;
            while (!left && width-- > 0) { put(b, ' '); written++; }
            while (n-- > 0) { put(b, *s++); written++; }
            while (left && width-- > 0) { put(b, ' '); written++; }
            break;
        }
        case 'c': put(b, (int)arg_int(ap, idx++)); written++; break;
        case '%': put(b, '%'); written++; break;
        case 0: f--; break;
        default: put(b, '%'); put(b, c); written += 2; break;
        }
    }
    put(b, 0);
    return written;
}
