// A mode word saved, set and restored around work on a pointer (ncolsim's list lever, docs/c2/ncolor.md 12): in
// plain() `saved` is an int global candidate of low priority; held in a one-member struct (boxed) or in a scope
// object whose constructor saves and destructor restores (scoped) it is a class-6 symbol, processed before every
// integer candidate. All callees are throw(): no EH frame. Build: /O2 /GS- /EHsc.
__declspec(thread) int g_mode;
struct Node { int a; Node* next; int cnt; void touch(int k) throw(); };
void visit(Node* n) throw();
int probe(Node* n, int k) throw();

struct ModeScope {
    int saved;
    ModeScope(int m) : saved(g_mode) { g_mode = m; }
    ~ModeScope() { g_mode = saved; }
};

struct Saved { int v; };

int plain(Node* o, int m) {
    int saved = g_mode;
    g_mode = m;
    Node* p = o->next;
    int r = 0;
    if (p->a) {
        visit(p);
        p->touch(1);
    }
    r = probe(p, 2);
    if (r > 3)
        visit(p);
    p->cnt += r;
    g_mode = saved;
    return r + p->cnt;
}

int boxed(Node* o, int m) {
    Saved s = { g_mode };
    g_mode = m;
    Node* p = o->next;
    int r = 0;
    if (p->a) {
        visit(p);
        p->touch(1);
    }
    r = probe(p, 2);
    if (r > 3)
        visit(p);
    p->cnt += r;
    g_mode = s.v;
    return r + p->cnt;
}

int scoped(Node* o, int m) {
    ModeScope s(m);
    Node* p = o->next;
    int r = 0;
    if (p->a) {
        visit(p);
        p->touch(1);
    }
    r = probe(p, 2);
    if (r > 3)
        visit(p);
    p->cnt += r;
    return r + p->cnt;
}
