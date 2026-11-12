/* docs/c2/immcand.md: the same byte stored through a signed or an unsigned lvalue. In k1 the char -1 shares a
   constant candidate with the pushed -1 (one more value in the block's pressure P); in k2 it does not. The code
   is the same. */
void g(int); int h(void);
int k1(char *p, int a, int b) { int x = h() + a, y = h() + b; *p = -1; g(-1); return x ^ y; }
int k2(char *p, int a, int b) { int x = h() + a, y = h() + b; *(unsigned char*)p = 255; g(-1); return x ^ y; }
