// R1050 (f3decomp): /O2 deletes `if (c)` after *c was read or written on the same path; which copy forms count.
// One function per copy form; the check is whether its listing still has `test eax, eax` (the null test).
// docs/c2/opt.md (GdfInsertAssertions), rules_xref.md.
#include <string.h>
struct R { void* f; char r; };
void* Alloc(unsigned);
void Add(R*);
inline void MemCopy(void* d, const void* s, unsigned n) { memcpy(d, s, n); }
__forceinline void MemCopyF(void* d, const void* s, unsigned n) { memcpy(d, s, n); }
inline void Copy(R* d, const R* s) { *d = *s; }
inline void CopyK(void* d, const void* s) { memcpy(d, s, sizeof(R)); }
inline void MemSet(void* d, int v, unsigned n) { memset(d, v, n); }

void x_assign(R* src)    { R* c = (R*)Alloc(8); *c = *src;                    if (c) Add(c); }
void x_memcpy(R* src)    { R* c = (R*)Alloc(8); memcpy(c, src, sizeof(R));    if (c) Add(c); }
void x_copy(R* src)      { R* c = (R*)Alloc(8); Copy(c, src);                 if (c) Add(c); }
void x_copyk(R* src)     { R* c = (R*)Alloc(8); CopyK(c, src);                if (c) Add(c); }
void x_readf(R* src)     { R* c = (R*)Alloc(8); src->f = c->f;                if (c) Add(c); }
void x_writer(R* src)    { R* c = (R*)Alloc(8); c->r = 1;                     if (c) Add(c); }
void x_memcopy(R* src)   { R* c = (R*)Alloc(8); MemCopy(c, src, sizeof(R));   if (c) Add(c); }
void x_memcopyf(R* src)  { R* c = (R*)Alloc(8); MemCopyF(c, src, sizeof(R));  if (c) Add(c); }
void x_memset(R* src)    { R* c = (R*)Alloc(8); memset(c, 0, sizeof(R));      if (c) Add(c); }
void x_memsetw(R* src)   { R* c = (R*)Alloc(8); MemSet(c, 0, sizeof(R));      if (c) Add(c); }
void x_memcpy4(R* src)   { R* c = (R*)Alloc(8); memcpy(c, src, 4);            if (c) Add(c); }
void x_memset4(R* src)   { R* c = (R*)Alloc(8); memset(c, 0, 4);              if (c) Add(c); }
void x_memcpyv(R* src, unsigned n) { R* c = (R*)Alloc(8); memcpy(c, src, n);  if (c) Add(c); }
