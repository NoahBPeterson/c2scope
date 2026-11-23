/* A tokenizer and a tiny expression evaluator: character-class switches, a symbol table, recursion. */
typedef struct Tok { int kind; int val; int start; int len; } Tok;
typedef struct Lex { const char *s; int pos; int line; Tok cur; int nsyms; int symval[64]; unsigned symhash[64]; } Lex;
extern void lex_error(Lex *lx, int code, int pos);

static unsigned hash(const char *s, int n)
{
    unsigned h = 2166136261u;
    int i;
    for (i = 0; i < n; i++) h = (h ^ (unsigned char)s[i]) * 16777619u;
    return h;
}

int lex_next(Lex *lx)
{
    const char *s = lx->s;
    int p = lx->pos, c, start, v;
again:
    c = s[p];
    switch (c) {
    case ' ': case '\t': case '\r': p++; goto again;
    case '\n': lx->line++; p++; goto again;
    case 0: lx->cur.kind = 0; break;
    case '+': case '-': case '*': case '/': case '(': case ')': case '%': case '^':
        lx->cur.kind = c; p++; break;
    case '0': case '1': case '2': case '3': case '4': case '5': case '6': case '7': case '8': case '9':
        v = 0; start = p;
        while (s[p] >= '0' && s[p] <= '9') { v = v * 10 + (s[p] - '0'); p++; }
        if (s[p] == 'x' && v == 0 && p == start + 1) {
            p++;
            for (;;) {
                int d = s[p];
                if (d >= '0' && d <= '9') d -= '0';
                else if (d >= 'a' && d <= 'f') d -= 'a' - 10;
                else break;
                v = v * 16 + d; p++;
            }
        }
        lx->cur.kind = 'n'; lx->cur.val = v; lx->cur.start = start; lx->cur.len = p - start;
        break;
    default:
        if ((c >= 'a' && c <= 'z') || c == '_') {
            unsigned h;
            int k, n;
            start = p;
            while ((s[p] >= 'a' && s[p] <= 'z') || s[p] == '_' || (s[p] >= '0' && s[p] <= '9')) p++;
            n = p - start;
            h = hash(s + start, n);
            for (k = 0; k < lx->nsyms; k++)
                if (lx->symhash[k] == h) break;
            if (k == lx->nsyms && k < 64) { lx->symhash[k] = h; lx->symval[k] = 0; lx->nsyms++; }
            lx->cur.kind = 'i'; lx->cur.val = k; lx->cur.start = start; lx->cur.len = n;
        } else {
            lex_error(lx, 1, p);
            lx->cur.kind = -1; p++;
        }
        break;
    }
    lx->pos = p;
    return lx->cur.kind;
}

static int expr(Lex *lx, int prec);

static int atom(Lex *lx)
{
    int v, k = lx->cur.kind;
    if (k == 'n') { v = lx->cur.val; lex_next(lx); return v; }
    if (k == 'i') { v = lx->symval[lx->cur.val & 63]; lex_next(lx); return v; }
    if (k == '-') { lex_next(lx); return -atom(lx); }
    if (k == '(') { lex_next(lx); v = expr(lx, 0); if (lx->cur.kind == ')') lex_next(lx); else lex_error(lx, 2, lx->pos); return v; }
    lex_error(lx, 3, lx->pos);
    return 0;
}

static int expr(Lex *lx, int prec)
{
    int lhs = atom(lx);
    for (;;) {
        int op = lx->cur.kind, p, rhs;
        switch (op) {
        case '+': case '-': p = 1; break;
        case '*': case '/': case '%': p = 2; break;
        case '^': p = 3; break;
        default: return lhs;
        }
        if (p < prec) return lhs;
        lex_next(lx);
        rhs = expr(lx, op == '^' ? p : p + 1);
        switch (op) {
        case '+': lhs += rhs; break;
        case '-': lhs -= rhs; break;
        case '*': lhs *= rhs; break;
        case '/': lhs = rhs ? lhs / rhs : 0; break;
        case '%': lhs = rhs ? lhs % rhs : 0; break;
        case '^': { int r = 1; while (rhs-- > 0) r *= lhs; lhs = r; } break;
        }
    }
}

int lex_eval(Lex *lx)
{
    lex_next(lx);
    return expr(lx, 0);
}
