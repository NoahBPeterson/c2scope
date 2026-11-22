// T22: a registered global object whose inline constructor stores its fields, then saves / sets a thread-local
// word, registers itself and restores the word.  Float-valued against int-valued objects.
struct Entry;
struct Registry {
    virtual ~Registry();
    virtual void Add(Entry *e);
};
extern __declspec(thread) unsigned tls_word;
extern Registry *g_reg;
void ensure_registry();

struct Entry {
    virtual ~Entry() {}
    const char *name;
};
struct WordScope {
    unsigned saved;
    explicit WordScope(unsigned w) : saved(tls_word) { tls_word = w; }
    ~WordScope() { tls_word = saved; }
};
struct FEntry : Entry {
    float value;
    FEntry(const char *n, float v) { name = n; value = v; WordScope s(0x63); ensure_registry(); g_reg->Add(this); }
    ~FEntry() {}
};
struct IEntry : Entry {
    int value;
    IEntry(const char *n, int v) { name = n; value = v; WordScope s(0x63); ensure_registry(); g_reg->Add(this); }
    ~IEntry() {}
};

FEntry fSpeed("fSpeed:Test", 1.5f);
IEntry iCount("iCount:Test", 7);
