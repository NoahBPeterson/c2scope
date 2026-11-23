/* An LZ77-style matcher with hash chains and a bit writer: send_bits-like macros declare block-local values. */
typedef unsigned char u8;
typedef unsigned short u16;
typedef struct Out { u8 *buf; int pos; unsigned bits; int nbits; } Out;
#define PUT(o, v, n) { int val = (v); int len = (n); (o)->bits |= (unsigned)val << (o)->nbits; (o)->nbits += len; \
    while ((o)->nbits >= 8) { (o)->buf[(o)->pos++] = (u8)(o)->bits; (o)->bits >>= 8; (o)->nbits -= 8; } }
extern void lz_flush(Out *o);

static int longest(const u8 *win, int cur, int prev, int lim, int maxlen, const u16 *chain, int nice)
{
    int best = 0, chainlen = 64;
    const u8 *scan = win + cur;
    while (prev > lim && chainlen-- > 0) {
        const u8 *m = win + prev;
        if (m[best] == scan[best] && m[0] == scan[0] && m[1] == scan[1]) {
            int len = 2;
            while (len < maxlen && m[len] == scan[len])
                len++;
            if (len > best) {
                best = len;
                if (len >= nice)
                    break;
            }
        }
        prev = chain[prev & 0x7fff];
    }
    return best;
}

int lz_compress(const u8 *in, int n, Out *o, u16 *head, u16 *chain, int level)
{
    int i = 0, lit = 0, matches = 0, h = 0;
    int nice = level > 5 ? 128 : 16;
    while (i < n - 3) {
        int prev, len;
        h = ((h << 5) ^ in[i + 2]) & 0xfff;
        prev = head[h];
        chain[i & 0x7fff] = (u16)prev;
        head[h] = (u16)i;
        len = (prev && i - prev < 32768) ? longest(in, i, prev, i - 32768, n - i < 258 ? n - i : 258, chain, nice) : 0;
        if (len >= 3) {
            int dist = i - prev;
            PUT(o, 1, 1);
            PUT(o, len - 3, 8);
            PUT(o, dist, 15);
            matches++;
            while (--len > 0) {
                i++;
                h = ((h << 5) ^ in[i + 2]) & 0xfff;
                chain[i & 0x7fff] = head[h];
                head[h] = (u16)i;
            }
            i++;
        } else {
            PUT(o, 0, 1);
            PUT(o, in[i], 8);
            lit++;
            i++;
        }
    }
    while (i < n) {
        PUT(o, 0, 1);
        PUT(o, in[i], 8);
        i++;
    }
    lz_flush(o);
    return matches * 65536 + lit;
}

unsigned lz_adler(const u8 *p, int n, unsigned a, unsigned b)
{
    while (n > 0) {
        int k = n < 3800 ? n : 3800;
        n -= k;
        while (k >= 4) {
            a += p[0]; b += a; a += p[1]; b += a; a += p[2]; b += a; a += p[3]; b += a;
            p += 4; k -= 4;
        }
        while (k--) { a += *p++; b += a; }
        a %= 65521; b %= 65521;
    }
    return (b << 16) | a;
}
