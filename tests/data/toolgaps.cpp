// tests/run_tests.py toolgap_tests: realize alias2 on a chain read twice, rwsweep byval / wrap / touch
struct LT { char pad[92]; int* m; };
int lt__base(LT* p, int c) { int n = (int)(p->m - (int*)0); if (n) n += *p->m; return n; }
int lt__other(LT* p) { return *p->m + 1; }

// a class returned by value, read through a member of the temporary (Spore 006b19e0 / 005f0a60 shape)
struct BvMap {
    struct It { void* node; It() : node(0) {} It(const It& x) : node(x.node) {} };
    It find(const int* key);
    int size;
};
void touch_ref(int a, bool& any);
struct BvUser {
    BvMap map;
    void* get(int k);
};
void* BvUser::get(int k)
{
    bool any = false;
    touch_ref(k, any);
    void* n = map.find(&k).node;
    return any ? n : 0;
}
