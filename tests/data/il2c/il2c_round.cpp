// il2c.py probe: functions whose IL at Lower's entry, written back as C++ by il2c, must compile to the same code.
struct Obj {
    virtual void v0();
    virtual int get(int k);
    int n, flags;
    unsigned cap;
    int* items;
    void touch(int k);
    int sum();
    int pick(unsigned u);
    void mark(int k);
    int call_get(int k);
};

int Obj::sum()                       // a loop over a member array
{
    int s = 0;
    for (int i = 0; i < n; i++)
        s += items[i];
    return s;
}

int Obj::pick(unsigned u)            // an unsigned compare, a member call
{
    if (u > cap)
        return 0;
    touch((int)u);
    return items[u];
}

void Obj::mark(int k)                // a read-modify-write
{
    flags |= 1 << k;
}

int Obj::call_get(int k)             // a virtual call
{
    if (!items)
        return -1;
    return get(k + 1);
}
