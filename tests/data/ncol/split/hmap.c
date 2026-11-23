/* Open-addressing hash map with tombstones, resizing, iteration and a string interner. */
typedef struct Ent { unsigned h; int key; int val; int state; } Ent;
typedef struct Map { Ent *e; int cap; int n; int dead; } Map;
extern void *xalloc(int n);
extern void xfree(void *p);
extern int str_eq(const char *a, const char *b, int n);

static unsigned mix(unsigned x)
{
    x ^= x >> 16; x *= 0x7feb352d; x ^= x >> 15; x *= 0x846ca68b; x ^= x >> 16;
    return x;
}

static int probe(const Map *m, int key, unsigned h, int *slot)
{
    int mask = m->cap - 1, i = (int)(h & (unsigned)mask), first = -1, step = 1;
    for (;;) {
        Ent *e = &m->e[i];
        if (e->state == 0) { *slot = first >= 0 ? first : i; return 0; }
        if (e->state == 2) { if (first < 0) first = i; }
        else if (e->h == h && e->key == key) { *slot = i; return 1; }
        i = (i + step++) & mask;
    }
}

int map_grow(Map *m, int cap)
{
    Ent *old = m->e;
    int oc = m->cap, i, moved = 0;
    m->e = (Ent *)xalloc(cap * (int)sizeof(Ent));
    if (!m->e) { m->e = old; return -1; }
    for (i = 0; i < cap; i++) m->e[i].state = 0;
    m->cap = cap; m->n = 0; m->dead = 0;
    for (i = 0; i < oc; i++) {
        if (old[i].state == 1) {
            int s;
            probe(m, old[i].key, old[i].h, &s);
            m->e[s] = old[i];
            m->n++; moved++;
        }
    }
    xfree(old);
    return moved;
}

int map_put(Map *m, int key, int val, int *oldval)
{
    unsigned h = mix((unsigned)key);
    int s, found;
    if ((m->n + m->dead + 1) * 4 > m->cap * 3 && map_grow(m, m->cap * 2) < 0)
        return -1;
    found = probe(m, key, h, &s);
    if (found) { if (oldval) *oldval = m->e[s].val; m->e[s].val = val; return 1; }
    if (m->e[s].state == 2) m->dead--;
    m->e[s].h = h; m->e[s].key = key; m->e[s].val = val; m->e[s].state = 1;
    m->n++;
    return 0;
}

int map_del(Map *m, int key)
{
    int s;
    if (!probe(m, key, mix((unsigned)key), &s)) return 0;
    m->e[s].state = 2; m->n--; m->dead++;
    return 1;
}

long long map_fold(const Map *m, int op, long long acc)
{
    int i;
    for (i = 0; i < m->cap; i++) {
        const Ent *e = &m->e[i];
        if (e->state != 1) continue;
        switch (op) {
        case 0: acc += e->val; break;
        case 1: acc ^= (long long)e->key << (e->val & 31); break;
        case 2: if (e->val > acc) acc = e->val; break;
        case 3: acc = acc * 31 + e->key; break;
        default: acc -= e->h; break;
        }
    }
    return acc;
}

int intern(const char **tab, unsigned *hs, int *n, int cap, const char *s, int len)
{
    unsigned h = 5381;
    int i, k;
    for (i = 0; i < len; i++) h = h * 33 + (unsigned char)s[i];
    for (k = 0; k < *n; k++)
        if (hs[k] == h && str_eq(tab[k], s, len)) return k;
    if (*n >= cap) return -1;
    tab[*n] = s; hs[*n] = h;
    return (*n)++;
}
