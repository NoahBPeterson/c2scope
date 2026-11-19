/* Two-address commutative ops (regmap: votes by role, not operand position). TroopFile::read_file's tail
   (Mount & Blade 0x524420): base lives across a call in a callee-saved register, the call's result comes back in
   eax, and the sum is stored: ours `add eax, ebp; mov [esi+N], eax`, the original `add ebp, eax; mov [esi+N], ebp`.
   The same value; the sum's register differs, the operands' do not. */
int g_xp[64];
int roll(int);
struct T { int lv; int pad[38]; int exp; int x2; };
void addsum(struct T *t, int lv)
{
    int base = g_xp[lv];
    base += roll(g_xp[lv + 1] - base);
    t->exp = base;
}
void orsum(struct T *t, int lv)
{
    int base = g_xp[lv];
    t->x2 = base | roll(lv);
    t->exp = base ^ roll(lv + 1);
}
