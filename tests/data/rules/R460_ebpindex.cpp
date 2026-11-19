// R460: "an ebp base register is moved to the index after allocation" (f3decomp 0x00595130: the IL at stack packing
// had [ebp+eax*1+12], the final code [eax+ebp+12]). Four values live across calls take esi, edi, ebx, ebp by
// priority; `rec` has the fewest references of the four, so it gets ebp and is the base of [rec + k + d].
// Compare the IL operand order (c2trace.py il --at @Color@4) with the SIB bytes of the listing.
void Use(int);

#define PRE  Use(a); Use(b); Use(k); Use(a); Use(b); Use(k); Use((int)rec); Use((int)rec); Use((int)rec)
#define POST Use(a); Use(b); Use(k); Use(a); Use(b); Use(k)

void ebp_idx(const char* rec, int k, int a, int b)  { PRE; Use(*(const int*)(rec + k + 12)); POST; }      // scale 1, disp 12
void ebp_idx0(const char* rec, int k, int a, int b) { PRE; Use(*(const int*)(rec + k)); POST; }           // scale 1, no disp
void ebp_idx4(const char* rec, int k, int a, int b) { PRE; Use(*(const int*)(rec + k * 4 + 12)); POST; }  // scale 4
