// regalloc_cost.md 4.3 / 5 (defPos tie-break): Color pops equal priorities by defPos descending. defPos counts
// instructions with blocks in list order, each block walked backwards, so inside one block the EARLIER def wins,
// across blocks the def in the LATER block wins. Every web here lives across calls (esi, edi, ebx, ebp order).
// ti_blk: a, b, c tie in one block. ti_xblk: a, b, c all prio 12; b is defined after the if.
int g(int); extern int gx;

int ti_blk() { int a = g(1), b = g(2), c = g(3); g(0); g(a); g(b); g(c); g(a); g(b); g(c); return 0; }

int ti_xblk() { int a = g(1); int c = g(3); g(c); g(c); if (gx) g(5);
  { int b = g(2); g(a); g(b); g(a); g(b); } return 0; }
