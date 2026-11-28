// base + index*size: one in-place chain (imul r; add r, [mem] -> t defined three times) or one lea of two
// temporaries (docs/c2/immcand.md 6.6).  Mount & Blade Mission::spawn 0x4832b0.  Each function: t computed, tested,
// a call, t used again (so t lives across the call).  Classify the entry: `add` (chain) or `lea` (two temporaries).
struct Troop { int flags; char pad[1284]; };                       // 1288 bytes
struct GameData { char pad[0x78]; int main_party; char pad2[0x13a00 - 0x7c]; Troop* troops; };
extern GameData* g_game;
extern Troop* g_troops;
struct Agent { char pad[0x88]; int troop; };
int g(int);
#define TAIL int s = 0; if (t->flags & 16) { s = g(g_game->main_party); } return s + t->flags;

int as_plain(Agent* a)  { Troop* t = g_game->troops + a->troop; TAIL }                      // chain
int as_addr(Agent* a)   { Troop* t = &g_game->troops[a->troop]; TAIL }                      // chain
int as_char(Agent* a)   { Troop* t = (Troop*)((char*)g_game->troops + a->troop * 1288); TAIL }   // chain
int as_uns(Agent* a)    { unsigned i = a->troop; Troop* base = g_game->troops; Troop* t = base + i; TAIL }   // chain
int as_glob(Agent* a)   { Troop* t = g_troops + a->troop; TAIL }                            // chain (base a plain global)
int as_arg(Agent* a, int i) { Troop* t = g_game->troops + i; TAIL }                         // chain
// the same sum evaluated twice before the call: the sum is one common subexpression (a temporary with two users)
int as_two(Agent* a)    { int f0 = g_game->troops[a->troop].flags; Troop* t = g_game->troops + a->troop; g(f0); TAIL }   // lea
int as_cond(Agent* a)   { Troop* t = g_game->troops + a->troop; int s = 0;
                          if (g_game->troops[a->troop].flags & 16) { s = g(g_game->main_party); } return s + t->flags; }  // lea
int as_two_ptr(Agent* a) { Troop* t = g_game->troops + a->troop; Troop* u = g_game->troops + a->troop; int s = 0;
                          if (t->flags & 16) { s = g(g_game->main_party); } return s + t->flags + u->flags; }            // lea
// the second evaluation after a call: the loads are killed, no common subexpression
int as_late(Agent* a)   { Troop* t = g_game->troops + a->troop; int s = 0; if (t->flags & 16) { s = g(g_game->main_party); }
                          return s + t->flags + g_game->troops[a->troop].flags; }                                         // chain
// the product shared (a named product used again): neither: the base is loaded into t and the product added
int as_prod(Agent* a)   { int off = a->troop * 1288; Troop* t = (Troop*)((char*)g_game->troops + off); int s = 0;
                          if (t->flags & 16) { s = g(g_game->main_party); } return s + t->flags + off; }                 // base+add
// a store between the two evaluations kills the base load, not the product: the product is shared (the third form)
int as_store(Agent* a)  { Troop* t = g_game->troops + a->troop; a->pad[0] = 1; int f0 = g_game->troops[a->troop].flags; g(f0); TAIL }
// the same sum used twice through one pointer variable (no second evaluation): still a chain
int as_reuse(Agent* a)  { Troop* t = g_game->troops + a->troop; g(t->flags); TAIL }
