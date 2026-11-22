// realize --from scratch / scratch --phase fixture (tests/run_tests.py realize_scratch_swap_tests).
struct Item { int id; char name[60]; };
struct List { int n; Item** items; int cap; };
void add(Item* p, int k);
void add2(int a, Item* p, int k);

struct Dlg {
    int cur;
    List* list;
    void f1(int k);
    void f2(int k, int j);
    void f3(int k, int j);
};

void Dlg::f1(int k)
{
    add(list->items[cur], k);
    add(list->items[k], cur);
}

void Dlg::f2(int k, int j)
{
    add2(j, list->items[k], k + j);
    add2(k, list->items[j], cur);
}

void Dlg::f3(int k, int j)
{
    for (int i = 0; i < k; i++)
        add2(i, list->items[j], k);
}
