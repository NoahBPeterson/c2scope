// schedwant.py: copying an address-taken struct local into an array element (Spore 0x00598db0's tail).
// The loads of key.a / key.b alias the element stores (key's address goes to use()), so key.b waits for the
// first store: schedwant.py's `unalias` of that load lets it issue before the store.
struct ID { unsigned a, b; };
struct Vec { ID* p; };
void use(ID* k);
void kcopy(Vec* v, int i, unsigned a0, unsigned a1)
{
    ID key;
    key.a = a0;
    key.b = a1;
    use(&key);
    v->p[i] = key;
    use(&key);
}
