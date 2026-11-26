// slotwant fixture: h's size decides the frame (576 bytes as h[256], 384 as h[64] in size64.cpp, "the original"): h
// conflicts with a (escaped, then live across use(&a)) and with nb, so it is a colour of its own of its own size
struct Mat { float m[16]; };
void use(Mat *p);
void ext(char *p);
void put(const char *p);
int g;

void f(int n)
{
    if (n) {
        char h[256];
        ext(h);
        put(h);
    }
    Mat a;
    use(&a);
    g = (int)a.m[0] + (int)a.m[1] + (int)a.m[2];
    char nb[256];
    ext(nb);
    put(nb);
}
