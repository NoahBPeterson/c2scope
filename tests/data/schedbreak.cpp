// schedwant fixture (tests/run_tests.py schedwant_break_tests): a pointer made by a lea is used by a load
// right after it and passed on; the load can issue before the lea that feeds it (a breakable dependence,
// sched.md rule 8: SchmdBreakDep rewrites the load's address). Mount & Blade 0x4ea030 has the same shape.
struct Slot { int key; int val; };
void keep(const int& v);
void keep2(int* p, int v);

struct Table {
    Slot tab[64];
    int* rows[16];
    void take(int slot);
    void take2(int slot, int k);
    int take3(int* p, int n);
};

void Table::take(int slot)
{
    int* pv = &tab[slot].val;
    int idx = *pv;
    tab[slot].key = -1;
    *pv = -1;
    rows[idx >> 4][idx & 15] = 0;
    keep(idx);
}

void Table::take2(int slot, int k)
{
    int* pv = &tab[slot].val;
    int idx = *pv;
    tab[slot].key = k;
    keep2(pv, idx);
}

int Table::take3(int* p, int n)
{
    int* q = p + 3;
    int a = *q;
    rows[n][0] = a;
    keep2(q, a + n);
    return a;
}
