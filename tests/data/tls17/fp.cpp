// T22: the same save / set / call / restore shape, with and without a floating-point leaf in the function.
struct Item;
struct Registry {
    virtual ~Registry();
    virtual void Add(Item *it);
};
struct FItem { void *vt; const char *name; float value; };
extern __declspec(thread) unsigned tls_word;
extern Registry *g_reg;
extern Item g_item;
extern FItem g_fitem;
extern float g_f;
void ensure_registry();
void destroy_item();
int at_exit(void (*f)());

// a: no float anywhere (control: folded, 94 bytes)
void init_a()
{
    unsigned saved = tls_word;
    tls_word = 0x63;
    ensure_registry();
    g_reg->Add(&g_item);
    tls_word = saved;
    at_exit(destroy_item);
}

// b: a float constant stored (the float leaf stays in the code)
void init_b()
{
    unsigned saved = tls_word;
    tls_word = 0x63;
    g_fitem.value = 1.5f;
    ensure_registry();
    g_reg->Add((Item *)&g_fitem);
    tls_word = saved;
    at_exit(destroy_item);
}

// c: a dead float local (removed by the optimizer)
void init_c()
{
    unsigned saved = tls_word;
    float dead = g_f * 2.0f;
    tls_word = 0x63;
    ensure_registry();
    g_reg->Add(&g_item);
    tls_word = saved;
    (void)dead;
    at_exit(destroy_item);
}
