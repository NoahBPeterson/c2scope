// frame.md 4.9: TryEbpAdjust (lea ebp, [esp-N]) for frames whose slot total T > 128 with ebp-based locals.
// Compile with /O1 (WorthitFPO gives the EBP frame). big: a 400-byte int array and hot int locals (no GS buffer);
// gs: the same with a 400-byte char buffer (GS level 3, second candidate); small: T <= 128.
void ext(void*);
int exti(int);
int big(int n)
{
    int a[100], x = exti(n), y = exti(x), z = exti(y);
    ext(a); ext(&x); ext(&y); ext(&z);
    return x + y + z + a[n] + exti(x) + exti(y) + exti(z);
}
int gs(int n)
{
    char a[400]; int x = exti(n), y = exti(x), z = exti(y);
    ext(a); ext(&x); ext(&y); ext(&z);
    return x + y + z + a[n] + exti(x) + exti(y) + exti(z);
}
int small(int n)
{
    int a[20], x = exti(n), y = exti(x), z = exti(y);
    ext(a); ext(&x); ext(&y); ext(&z);
    return x + y + z + a[n] + exti(x) + exti(y) + exti(z);
}
