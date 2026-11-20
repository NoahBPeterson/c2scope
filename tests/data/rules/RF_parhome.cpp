// frame.md 0 rule 9 / 4.5: a dead parameter home takes a local only without a /GS buffer and GS copies.
// V9 shape (p read by an add, so its cdecl home is "safe"): nobuf has no buffer, buf adds char b[8] (compile /GS).
void ext(void*);
void ext2(int);
void extc(char*);
int nobuf(int p) { int x; ext2(p + 1); ext(&x); return x; }
int buf(int p) { char b[8]; int x; ext2(p + 1); extc(b); ext(&x); return x; }
