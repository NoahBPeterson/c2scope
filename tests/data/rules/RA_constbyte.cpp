// A constant shared by dword compares and a byte store, across calls: the byte flag, ebx and what takes it first
// (docs/c2/immcand.md section 6; tools/constregs.py; checks in tests/probes/constregs.py).
struct R { int pad; int type; int a; char dirty; char e; short s; int w; };
R* look(int);
void done(int);
// K = 1: a dword compare before the loop, one in it and a byte store in it; v, n, k and id cross calls
void q_byte(int id, int n, int* v)
{
    if (look(id)->type != 1) return;
    for (int k = 0; k < n; k++) {
        if (look(v[k])->type != 1) continue;
        look(v[k])->a = -1;
        look(v[k])->dirty = 1;
    }
    done(id); done(id); done(id);
}
// the byte store of another value: K = 1 keeps its two dword compares only
void q_two(int id, int n, int* v)
{
    if (look(id)->type != 1) return;
    for (int k = 0; k < n; k++) {
        if (look(v[k])->type != 1) continue;
        look(v[k])->a = -1;
        look(v[k])->dirty = 2;
    }
    done(id); done(id); done(id);
}
// a dword store of 1 instead of the byte store: K = 1 has three dword uses, no byte flag
void q_dword(int id, int n, int* v)
{
    if (look(id)->type != 1) return;
    for (int k = 0; k < n; k++) {
        if (look(v[k])->type != 1) continue;
        look(v[k])->a = -1;
        look(v[k])->w = 1;
    }
    done(id); done(id); done(id);
}
// as q_byte, but the element's address is kept across the first call (a reference into a vector whose base is
// re-read after every call): that address is a call-crossing web in the loop, coloured before K = 1, and takes ebx,
// K = 1's only register; K = 1 is then taken off the list and every use stays an immediate
struct V { int* b; int* e; };
void q_ref(int id, int n, V* vv)
{
    if (look(id)->type != 1) return;
    for (int k = 0; k < n; k++) {
        int& e = vv->b[k];
        if (look(e)->type != 1) continue;
        look(e)->a = -1;
        look(vv->b[k])->dirty = 1;
    }
    done(id); done(id); done(id);
}
// the same loop without the reference: no call-crossing address, K = 1 takes ebx as in q_byte
void q_noref(int id, int n, V* vv)
{
    if (look(id)->type != 1) return;
    for (int k = 0; k < n; k++) {
        if (look(vv->b[k])->type != 1) continue;
        look(vv->b[k])->a = -1;
        look(vv->b[k])->dirty = 1;
    }
    done(id); done(id); done(id);
}
