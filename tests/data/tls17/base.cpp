// T22 baseline: save a thread-local word, set it, call, virtual call, restore, tail call atexit-like.
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

void init_plain()
{
    unsigned saved = tls_word;
    tls_word = 0x63;
    ensure_registry();
    g_reg->Add(&g_item);
    tls_word = saved;
    at_exit(destroy_item);
}
