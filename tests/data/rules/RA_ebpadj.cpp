// RA_ebpadj: the EBP adjustment (frame.md 4.9, TryEbpAdjust): in an EBP frame with more than 0x80 bytes of
// locals, ebp is set below the frame top (`lea ebp, [esp-N]` after `push ebp`) so that the busiest slots are in
// disp8 reach of ebp. validation.md "Not tested, or open". Compile /O1 (the frame pointer is kept once there are
// many frame operands). /DNB=<n>: the char buffer's size; /DNI=<n>: an int array (not a GS buffer) of n ints.
int g(int);
void h(char *, int *);
#ifndef NB
#define NB 512
#endif

int ea(int a, int b, int c)
{
    char buf[NB];
#ifdef NI
    int arr[NI];
    h(buf, arr);
#endif
    int x = a, y = b, z = c;
    h(buf, &x); h(buf, &y); h(buf, &z);
    x += g(x); y += g(y); z += g(z);
    x += g(y); y += g(z); z += g(x);
    return x + y + z + buf[3] + a + b + c;
}
