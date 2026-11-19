// R1280 (f3decomp): an inlined lookup ending in `return 0` feeds a `while (cursor)` test. Assigned straight to the tested
// variable (direct_orig, sep_orig, one_orig), GlobalOptimize threads the 0 path to the loop exit; with a copy in between
// (copy_ours, copy2_ours) the join block holds `cursor = start` before the compare and the join stays:
// `xor eax, eax; test eax, eax; mov esi, eax; je`, one more instruction. icount copy_ours --vs-func direct_orig: +1,
// classes structure / structure:inline / regalloc:callee-saved (it does not name the threading). All four builds.
struct Node { Node* next; int v; };
struct Map { Node** buckets; int n; void* First() { for (int i = 0; i < n; i++) if (buckets[i]) return buckets[i]; return 0; } };
void use(Node*);
void copy_ours(Map* m) { void* start = m->First(); Node* cursor = (Node*)start; while (cursor) { use(cursor); cursor = cursor->next; } }
void direct_orig(Map* m) { Node* cursor = (Node*)m->First(); while (cursor) { use(cursor); cursor = cursor->next; } }
void copy2_ours(Map* m) { Node* start = (Node*)m->First(); Node* cursor = start; while (cursor) { use(cursor); cursor = cursor->next; } }
void sep_orig(Map* m) { Node* cursor; cursor = (Node*)m->First(); while (cursor) { use(cursor); cursor = cursor->next; } }
void one_orig(Map* m) { void* start = m->First(); while (start) { use((Node*)start); start = ((Node*)start)->next; } }
