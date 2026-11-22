// T22: which dead floating-point expressions set FUNC+0xe4 & 0x80 (and so unfold the TLS address) without code.
template<class RzT> struct rz_g { static RzT v; };
struct Item;
struct Registry {
    virtual ~Registry();
    virtual void Add(Item *it);
};
extern __declspec(thread) unsigned tls_word;
extern Registry *g_reg;
extern Item g_item;
void ensure_registry();
void destroy_item();
int at_exit(void (*f)());

void init_k0()
{
    (void)0.0f;
    unsigned saved = tls_word;
    tls_word = 0x63;
    ensure_registry();
    g_reg->Add(&g_item);
    tls_word = saved;
    at_exit(destroy_item);
}

void init_k1()
{
    float rz_f = 0.0f; (void)rz_f;
    unsigned saved = tls_word;
    tls_word = 0x63;
    ensure_registry();
    g_reg->Add(&g_item);
    tls_word = saved;
    at_exit(destroy_item);
}

void init_k2()
{
    (void)rz_g<float>::v;
    unsigned saved = tls_word;
    tls_word = 0x63;
    ensure_registry();
    g_reg->Add(&g_item);
    tls_word = saved;
    at_exit(destroy_item);
}

void init_k3()
{
    (void)(rz_g<float>::v * 2.0f);
    unsigned saved = tls_word;
    tls_word = 0x63;
    ensure_registry();
    g_reg->Add(&g_item);
    tls_word = saved;
    at_exit(destroy_item);
}

void init_k4()
{
    (void)(double)rz_g<int>::v;
    unsigned saved = tls_word;
    tls_word = 0x63;
    ensure_registry();
    g_reg->Add(&g_item);
    tls_word = saved;
    at_exit(destroy_item);
}

void init_k5()
{
    if (0.0f) {}
    unsigned saved = tls_word;
    tls_word = 0x63;
    ensure_registry();
    g_reg->Add(&g_item);
    tls_word = saved;
    at_exit(destroy_item);
}

void init_k6()
{
    (void)(float)0;
    unsigned saved = tls_word;
    tls_word = 0x63;
    ensure_registry();
    g_reg->Add(&g_item);
    tls_word = saved;
    at_exit(destroy_item);
}
