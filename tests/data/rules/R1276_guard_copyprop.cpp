// R1276 (f3decomp): GlobalOptimize's copy propagation rewrites a loop's zero-trip guard (`p = h; if (p == 0)`) to test
// the head h. Visible when h stays live in its own register (f_hlive: `mov edi, eax; test edi, edi; mov esi, edi`).
// Lever: make the loop variable the definition and the kept head its copy (l1_rev: `test esi, esi`). Same instruction
// count, so icount reports +0 / no class: a register-only difference (regmap). All four builds.
struct Node { Node* next; int v; };
Node* first(); void use(Node*);
void f_hlive(int k) { Node* h = first(); for (Node* p = h; p; p = p->next) use(p); use(h); }
void l1_rev(int k) { Node* p = first(); Node* h = p; for (; p; p = p->next) use(p); use(h); }
void l2_dow(int k) { Node* h = first(); Node* p = h; if (h) do { use(p); p = p->next; } while (p); use(h); }
void l3_inner(int k) { Node* h = first(); if (h) for (Node* p = h; p; p = p->next) use(p); use(h); }
