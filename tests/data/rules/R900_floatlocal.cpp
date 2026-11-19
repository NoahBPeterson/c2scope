// R900: "a named float local is stored and reloaded, even when its initialiser is a plain float load". Pairs:
// an expression into a float local (rounded through its home: fstp/fld) vs a double local (stays on the stack);
// a plain load into a float local, with and without a call between the load and the uses.
extern float G;
extern double GD;
void Tick();
float Sink2(float, float);

float fx_expr(float a, float b, float c) { float s = a * b; return s < c ? s : c; }          // round trip
float dx_expr(float a, float b, float c) { double s = a * b; return s < c ? (float)s : c; }   // none
float fx_load(float a, float b) { const float k = G; return Sink2(a * k, b * k); }            // none: k is G
float fx_loadcall(float a) { const float k = G; Tick(); return a * k + k; }                   // stored across the call
float dx_loadcall(float a) { const double k = GD; Tick(); return (float)(a * k + k); }        // stored across the call too
float dx_load(float a, float b) { const double k = GD; return Sink2((float)(a * k), (float)(b * k)); }  // none
struct S { float f; };
float fx_member(const S* p, float a, float b) { const float k = p->f; return Sink2(a * k, b * k); } // as fx_load
float fx_param(float a, float b) { float k = a; return Sink2(b * k, k + b); }                  // none
