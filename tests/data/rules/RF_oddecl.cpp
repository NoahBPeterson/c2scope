// RF_oddecl.cpp -- the /Od slot-order levers, de-confounded (frame.md 1, the AllocateAutomatics bullet).
// Companion to RF_odorder.cpp: 11 functions, one traced compile each set; read each function's
// _name$ = -N table from the /FAsc listing, order by offset descending (first-allocated first).
//
//   v1/v2  frame.md's RF_odorder pair (decl y w z x / x z w y, stmt z x w y):  w x y z / w y x z
//   v3     v1's decls, statements REVERSED                                   :  w x y z  (= v1)
//   v4     16 letters (a b c .. o r), decl a..r,  stmt a..r                  :  h a i f g b k l r d c n2 e o m j
//   v5     SAME stmts, decl REVERSED                                         :  h a i f b g k l c d r e n2 j m o
//   v6     SAME stmts, decl with c<->r exchanged                             :  h a i f g b k l c d r n2 e o m j
//   v7     v4's decls, statements REVERSED                                   :  = v4
//   v8/v9  the Spore real-word set, fillPair between dst and i (v8) vs after i (v9), same stmts: identical
//   v10    fillPair declared FIRST (crossing sum2's declaration)             :  the {fillPair sum2} pair flips
//   v11    fillPair before dst                                               :  = v8 (dst does not collide)
//
// Reading (cl 15.00.30729, /Od): the slot order is the name order, with each hash-collision class
// in REVERSED declaration order (later-declared first); statement order has no effect. The classes
// are {g b}, {r d c}, {n2 e}, {o m j}, {c d r} in the letter set and {fillPair sum2} in the word
// set; dst and i do not collide with fillPair. Compile:
//   C2_BUILD=15.00 uv run tools/c2trace.py --keep D tests/data/rules/RF_oddecl.cpp --func v1 -- /c /Od /Ob1 /MD /Gy /TP /GS- /FAsc
// (one compile logs all functions; the listing's per-COMDAT _name$ tables are read per function).

/* T25/T26 follow-up: the /Od slot-order levers, de-confounded.
   Same statements everywhere unless noted; only the declaration order moves (A), or only the
   statements move (B). Each function: 16 int locals, each defined once via ext() and summed.
   v1/v2: frame.md's RF_odorder pair (4 letters). v3: v1's decls + REVERSED statements (B).
   v4/v5/v6: 16 letters incl. c and r: v4 decl a..r; v5 decl REVERSED; v6 decl with c<->r exchanged.
   v7: v4's decls + REVERSED statements (B on the 16-letter set).
   v8/v9: real-word names (the Spore set), fillPair declared between dst and i (v8) vs after i (v9),
   statements fixed in v8's order for both. */
int ext(int);
int v1(int n) { int y, w, z, x; z = ext(n); x = ext(1); w = ext(2); y = ext(3); return x + y + z + w + n; }
int v2(int n) { int x, z, w, y; z = ext(n); x = ext(1); w = ext(2); y = ext(3); return x + y + z + w + n; }
int v3(int n) { int y, w, z, x; y = ext(3); w = ext(2); x = ext(1); z = ext(n); return x + y + z + w + n; }
int v4(int n) { int a, b, c, d, e, f, g, h, i, j, k, l, m, n2, o, r;
    a = ext(1); b = ext(2); c = ext(3); d = ext(4); e = ext(5); f = ext(6); g = ext(7); h = ext(8);
    i = ext(9); j = ext(10); k = ext(11); l = ext(12); m = ext(13); n2 = ext(14); o = ext(15); r = ext(16);
    return a+b+c+d+e+f+g+h+i+j+k+l+m+n2+o+r+n; }
int v5(int n) { int r, o, n2, m, l, k, j, i, h, g, f, e, d, c, b, a;
    a = ext(1); b = ext(2); c = ext(3); d = ext(4); e = ext(5); f = ext(6); g = ext(7); h = ext(8);
    i = ext(9); j = ext(10); k = ext(11); l = ext(12); m = ext(13); n2 = ext(14); o = ext(15); r = ext(16);
    return a+b+c+d+e+f+g+h+i+j+k+l+m+n2+o+r+n; }
int v6(int n) { int a, b, r, d, e, f, g, h, i, j, k, l, m, n2, o, c;
    a = ext(1); b = ext(2); c = ext(3); d = ext(4); e = ext(5); f = ext(6); g = ext(7); h = ext(8);
    i = ext(9); j = ext(10); k = ext(11); l = ext(12); m = ext(13); n2 = ext(14); o = ext(15); r = ext(16);
    return a+b+c+d+e+f+g+h+i+j+k+l+m+n2+o+r+n; }
int v7(int n) { int a, b, c, d, e, f, g, h, i, j, k, l, m, n2, o, r;
    r = ext(16); o = ext(15); n2 = ext(14); m = ext(13); l = ext(12); k = ext(11); j = ext(10); i = ext(9);
    h = ext(8); g = ext(7); f = ext(6); e = ext(5); d = ext(4); c = ext(3); b = ext(2); a = ext(1);
    return a+b+c+d+e+f+g+h+i+j+k+l+m+n2+o+r+n; }
int v8(int n) { int sum2, hole, t10, dst, fillPair, i, el, fill, fillCopy, tri, nodeId, k, corner, slot, id, nHdr;
    sum2 = ext(1); hole = ext(2); t10 = ext(3); dst = ext(4); fillPair = ext(5); i = ext(6); el = ext(7);
    fill = ext(8); fillCopy = ext(9); tri = ext(10); nodeId = ext(11); k = ext(12); corner = ext(13);
    slot = ext(14); id = ext(15); nHdr = ext(16);
    return sum2+hole+t10+dst+fillPair+i+el+fill+fillCopy+tri+nodeId+k+corner+slot+id+nHdr+n; }
int v9(int n) { int sum2, hole, t10, dst, i, fillPair, el, fill, fillCopy, tri, nodeId, k, corner, slot, id, nHdr;
    sum2 = ext(1); hole = ext(2); t10 = ext(3); dst = ext(4); fillPair = ext(5); i = ext(6); el = ext(7);
    fill = ext(8); fillCopy = ext(9); tri = ext(10); nodeId = ext(11); k = ext(12); corner = ext(13);
    slot = ext(14); id = ext(15); nHdr = ext(16);
    return sum2+hole+t10+dst+fillPair+i+el+fill+fillCopy+tri+nodeId+k+corner+slot+id+nHdr+n; }
int v10(int n) { int fillPair, sum2, hole, t10, dst, i, el, fill, fillCopy, tri, nodeId, k, corner, slot, id, nHdr;
    sum2 = ext(1); hole = ext(2); t10 = ext(3); dst = ext(4); fillPair = ext(5); i = ext(6); el = ext(7);
    fill = ext(8); fillCopy = ext(9); tri = ext(10); nodeId = ext(11); k = ext(12); corner = ext(13);
    slot = ext(14); id = ext(15); nHdr = ext(16);
    return sum2+hole+t10+dst+fillPair+i+el+fill+fillCopy+tri+nodeId+k+corner+slot+id+nHdr+n; }
int v11(int n) { int sum2, hole, t10, fillPair, dst, i, el, fill, fillCopy, tri, nodeId, k, corner, slot, id, nHdr;
    sum2 = ext(1); hole = ext(2); t10 = ext(3); dst = ext(4); fillPair = ext(5); i = ext(6); el = ext(7);
    fill = ext(8); fillCopy = ext(9); tri = ext(10); nodeId = ext(11); k = ext(12); corner = ext(13);
    slot = ext(14); id = ext(15); nHdr = ext(16);
    return sum2+hole+t10+dst+fillPair+i+el+fill+fillCopy+tri+nodeId+k+corner+slot+id+nHdr+n; }
