// R1150 (f3decomp): shift idioms never become bswap; _byteswap_ulong is bswap; _byteswap_ushort is
// `mov cx, [p]; mov ah, cl; mov al, ch`; _rotl and the shift spelling of a rotate are `rol` on 14.00/15.00 (also ror);
// 13.10 recognises only _rotl (rotsh/rotr stay shl/shr/or).
#include <stdlib.h>
unsigned shiftswap(unsigned x) { return (x >> 24) | ((x >> 8) & 0xff00) | ((x << 8) & 0xff0000) | (x << 24); }
unsigned maskswap(unsigned x) { return ((x & 0xff000000) >> 24) | ((x & 0xff0000) >> 8) | ((x & 0xff00) << 8) | ((x & 0xff) << 24); }
#if _MSC_VER >= 1300
unsigned bsw(unsigned x) { return _byteswap_ulong(x); }
unsigned short bsw16(unsigned short* p) { return _byteswap_ushort(*p); }
#endif
unsigned short sw16(unsigned short x) { return (unsigned short)((x >> 8) | (x << 8)); }
unsigned rotl8(unsigned x) { return _rotl(x, 8); }
unsigned rotsh(unsigned x) { return (x << 8) | (x >> 24); }
unsigned rotr(unsigned x) { return (x >> 8) | (x << 24); }
unsigned bytes(unsigned x) { unsigned r; unsigned char* s = (unsigned char*)&x; unsigned char* d = (unsigned char*)&r; d[0] = s[3]; d[1] = s[2]; d[2] = s[1]; d[3] = s[0]; return r; }
