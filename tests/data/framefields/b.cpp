// ... and as the map's value_type: the key in front of the 16-byte value (b.cpp, the "original").
struct Ev { int f0, f4, f8, fc; };
struct Pr { unsigned first; Ev second; Pr(const unsigned& k, const Ev& v) : first(k), second(v) {} };
struct Map { void Insert(const Pr* v); };
void ins16(Map* m, unsigned k)
{
    Pr value(k, Ev());
    m->Insert(&value);
}
