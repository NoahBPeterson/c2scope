// tools/regroles.py (docs/c2/regroles.md; Spore 0x009e0b90's shape). rg_hand walks a range with `++first` and its trip
// count is computed from `first` (a `mul`) before the loop: first lives across the mul, so it cannot take eax and
// ends up in a callee-saved register shared with the induction temporary cl makes after the count. rg_orig defines
// the pointer to the walked field before the count (`&first->v24`): first dies before the mul, stays in eax, the
// pointer takes esi. The tool finds that rewrite for rg_hand against rg_orig's code.
typedef unsigned int uint32;
void operator delete[](void* p);
struct RgElem40;
struct RgVec { RgElem40* mpBegin; RgElem40* mpEnd; RgElem40* mpCap; void DestroyRange(RgElem40* first, RgElem40* last); };
struct RgElem { char pad0[0x10]; RgElem40* v10b; char pad1[0x10]; RgVec v24; char pad2[0xa4 - 0x24 - 12]; };

void __stdcall rg_hand(RgElem* first, RgElem* last)
{
    if (first >= last) return;
    uint32 count = ((uint32)((char*)last - (char*)first) - 1) / 0xa4 + 1;
    do {
        first->v24.DestroyRange(first->v24.mpBegin, first->v24.mpEnd);
        if (first->v24.mpBegin && ((int*)first->v24.mpBegin)[-1] != 0) operator delete[](first->v24.mpBegin);
        if (first->v10b && ((int*)first->v10b)[-1] != 0) operator delete[](first->v10b);
        ++first; --count;
    } while (count != 0);
}

void __stdcall rg_orig(RgElem* first, RgElem* last)
{
    if (first >= last) return;
    RgVec* p = &first->v24;
    uint32 count = ((uint32)((char*)last - (char*)first) - 1) / 0xa4 + 1;
    do {
        p->DestroyRange(p->mpBegin, p->mpEnd);
        if (p->mpBegin && ((int*)p->mpBegin)[-1] != 0) operator delete[](p->mpBegin);
        RgElem40* q = *(RgElem40**)((char*)p - 0x14);
        if (q && ((int*)q)[-1] != 0) operator delete[](q);
        p = (RgVec*)((char*)p + 0xa4); --count;
    } while (count != 0);
}
