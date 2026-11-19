// framefields.py: the inserted value as a 16-byte struct with the key inside (a draft) ...
struct Ev { int f0, f4, f8, fc; };
struct Map { void Insert(const Ev* v); };
void ins16(Map* m, unsigned k)
{
    Ev value = Ev();
    value.f8 = (int)k;
    m->Insert(&value);
}
