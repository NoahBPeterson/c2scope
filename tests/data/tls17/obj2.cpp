// T22: registered global objects whose inline constructors store their fields (made static data by c2), then
// save / set a thread-local word, register the object and restore the word.  The float-valued object's
// initializer keeps the TLS base and the section offset in two registers ([rdi+rsi]); the int-valued one folds
// them (add rdi,rdx; [rdi]).  Rule: NColMdNoFoldBenefit, docs/c2/tls17.md.
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
struct FEntry : Entry {
    float value;
    FEntry(const char *n, float v)
    {
        name = n; value = v;
        unsigned saved = tls_word; tls_word = 0x63;
        ensure_registry(); g_reg->Add(this);
        tls_word = saved;
    }
};
struct IEntry : Entry {
    int value;
    IEntry(const char *n, int v)
    {
        name = n; value = v;
        unsigned saved = tls_word; tls_word = 0x63;
        ensure_registry(); g_reg->Add(this);
        tls_word = saved;
    }
};
struct DEntry : Entry {
    double value;
    DEntry(const char *n, double v)
    {
        name = n; value = v;
        unsigned saved = tls_word; tls_word = 0x63;
        ensure_registry(); g_reg->Add(this);
        tls_word = saved;
    }
};

FEntry fSpeed("fSpeed:Test", 1.5f);
IEntry iCount("iCount:Test", 7);
DEntry dScale("dScale:Test", 0.25);
FEntry fZero("fZero:Test", 0.0f);
