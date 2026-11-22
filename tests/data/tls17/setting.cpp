// T22: natural spellings of a static setting initializer whose setting object is extern (defined elsewhere),
// measured for FUNC+0xe4 & 0x80 (a float leaf at OtlScanFuncLeaves) and the TLS form.  init_ref is the steering
// spelling (a dead float local); init_none is the integer-only control.  Results: docs/c2/tls17.md.
extern __declspec(thread) unsigned tls_word;

struct Setting {
    virtual ~Setting();
    union { float f; int i; unsigned u; const char *s; } value;
    const char *key;

    float GetFloat() const { return value.f; }
    bool IsDefault(float def) const { return value.f == def; }
    void Register(float def);                       // inline, below
    void RegisterUsing(float def);                  // inline, below: def only in an accessor
    void Reset(float def) { float v = def; (void)v; }
    template<class T> void RegisterT(T def);
};
struct SettingCollection {
    virtual ~SettingCollection();
    virtual void Add(Setting *s);
};
extern SettingCollection *g_settings;
void EnsureSettings();
void __cdecl Setting_dtor();
extern "C" int __cdecl atexit(void (__cdecl *)());

inline void AddSetting(Setting *s) { EnsureSettings(); g_settings->Add(s); }
inline void AddSettingDef(Setting *s, float def = 0.0f) { (void)def; EnsureSettings(); g_settings->Add(s); }
inline void AddSettingDef2(Setting *s, float def = 0.0f) { EnsureSettings(); g_settings->Add(s); }
inline void Setting::Register(float)
{
    unsigned saved = tls_word; tls_word = 0x63;
    AddSetting(this);
    tls_word = saved;
}
inline void Setting::RegisterUsing(float def)
{
    unsigned saved = tls_word; tls_word = 0x63;
    if (!IsDefault(def)) {}
    AddSetting(this);
    tls_word = saved;
}
template<class T> inline void Setting::RegisterT(T)
{
    unsigned saved = tls_word; tls_word = 0x63;
    AddSetting(this);
    tls_word = saved;
}
template<class T> inline void InitSetting(Setting &s, T)
{
    unsigned saved = tls_word; tls_word = 0x63;
    AddSetting(&s);
    tls_word = saved;
}
inline float DefaultValue() { return 2.0f; }
struct FloatDefault { static const float kDefault; };

template<class T> struct TSetting : Setting {
    T Get() const { return *(const T *)&value; }
    void Register()
    {
        unsigned saved = tls_word; tls_word = 0x63;
        AddSetting(this);
        tls_word = saved;
    }
};

// registers the setting and restores the memory-context word on scope exit
struct SettingRegistrar {
    unsigned saved;
    float def;
    SettingRegistrar(Setting *s, float d) : saved(tls_word), def(d) { tls_word = 0x63; AddSetting(s); }
    ~SettingRegistrar() { tls_word = saved; }
};

extern Setting fA;
extern TSetting<float> fT;

#define BODY  { unsigned saved = tls_word; tls_word = 0x63; EnsureSettings(); g_settings->Add(&fA); tls_word = saved; }

void init_none() { BODY atexit(Setting_dtor); }
void init_ref() { float f = 0.0f; (void)f; BODY atexit(Setting_dtor); }

// member Register(float) taking the default, unused
void init_reg() { fA.Register(2.0f); atexit(Setting_dtor); }
// default used only in an inline accessor's comparison
void init_regusing() { fA.RegisterUsing(2.0f); atexit(Setting_dtor); }
// default argument of float type on an inline wrapper
void init_defarg() { unsigned saved = tls_word; tls_word = 0x63; AddSettingDef(&fA); tls_word = saved; atexit(Setting_dtor); }
void init_defarg2() { unsigned saved = tls_word; tls_word = 0x63; AddSettingDef2(&fA); tls_word = saved; atexit(Setting_dtor); }
void init_defarg3() { unsigned saved = tls_word; tls_word = 0x63; AddSettingDef2(&fA, 2.0f); tls_word = saved; atexit(Setting_dtor); }
// template member / free template with a float default
void init_regt() { fA.RegisterT(2.0f); atexit(Setting_dtor); }
void init_inits() { InitSetting(fA, 2.0f); atexit(Setting_dtor); }
// Reset(float) writing into a local only
void init_reset() { fA.Reset(2.0f); BODY atexit(Setting_dtor); }
// accessor results unused
void init_getf() { (void)fA.GetFloat(); BODY atexit(Setting_dtor); }
void init_getf2() { fA.GetFloat(); BODY atexit(Setting_dtor); }
void init_isdef() { if (fA.IsDefault(2.0f)) {} BODY atexit(Setting_dtor); }
// inline returning float, discarded
void init_defval() { DefaultValue(); BODY atexit(Setting_dtor); }
// const float static member referenced
void init_kdef() { fA.Register(FloatDefault::kDefault); atexit(Setting_dtor); }
// float in a template argument (TSetting<float>), no float expression
void init_tset() { fT.Register(); atexit(Setting_dtor); }
void init_tget() { (void)fT.Get(); fT.Register(); atexit(Setting_dtor); }
// scope object holding the default
void init_raii() { { SettingRegistrar r(&fA, 2.0f); } atexit(Setting_dtor); }
// the default named as a local, passed to Register
void init_local() { float def = 2.0f; fA.Register(def); atexit(Setting_dtor); }
// __noop with a float argument
void init_noop() { __noop(2.0f); BODY atexit(Setting_dtor); }
