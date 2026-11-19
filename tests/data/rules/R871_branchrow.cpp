// R871 (f3decomp 0x009e3540): a loop counter read once per iteration, before calls, under register pressure (a, b,
// c and part take esi/edi/ebx/ebp). `row_shared` computes the row address once above the `if`; `row_arms` writes the
// same expression in both arms. In the final code both have ONE `imul` above the branch, but `row_arms` keeps `i`
// in ecx at the loop head (`xor ecx, ecx` shared by `i = 0` and the count test, a `jmp` past the reload at the back
// edge) while `row_shared` uses `imul eax, [i]`.
// Mechanism (docs/c2/regalloc.md, "Expressions in both arms"): GlobalOptimize does not merge the two arms' copies;
// Color sees two uses of i, so the split head piece of i has 3 refs and benefit > 0 and is coloured; the copies are
// merged after Color by FgTryHeadMerge (dop2_FinalFlowOpts).
// Run: uv run tools/c2trace.py regs tests/data/rules/R871_branchrow.cpp --func row_ --why i -- /TP /O2
//      uv run tools/c2trace.py merges tests/data/rules/R871_branchrow.cpp --func row_arms -- /TP /O2
struct Part {
    unsigned char* data;
    int stride;
    int shortIdx;
    int pad;
    int count;
};

void Use3(int a, int b, int c);
void Hit(int k);

void row_shared(Part* part, int* a, int* b, int* c) {
    for (int i = 0; i < part->count; ++i) {
        const unsigned char* row = part->data + i * part->stride;
        if (part->shortIdx == 1) {
            const unsigned short* idx = (const unsigned short*)row;
            Use3(idx[0], idx[1], idx[2]);
        } else {
            const unsigned int* idx = (const unsigned int*)row;
            Use3(idx[0], idx[1], idx[2]);
        }
        Hit(a[0]); Hit(a[1]); Hit(a[2]); Hit(a[3]); Hit(a[4]); Hit(a[5]);
        Hit(b[0]); Hit(b[1]); Hit(b[2]); Hit(b[3]); Hit(b[4]); Hit(b[5]);
        Hit(c[0]); Hit(c[1]); Hit(c[2]); Hit(c[3]);
    }
}

void row_arms(Part* part, int* a, int* b, int* c) {
    for (int i = 0; i < part->count; ++i) {
        if (part->shortIdx == 1) {
            const unsigned short* idx = (const unsigned short*)(part->data + i * part->stride);
            Use3(idx[0], idx[1], idx[2]);
        } else {
            const unsigned int* idx = (const unsigned int*)(part->data + i * part->stride);
            Use3(idx[0], idx[1], idx[2]);
        }
        Hit(a[0]); Hit(a[1]); Hit(a[2]); Hit(a[3]); Hit(a[4]); Hit(a[5]);
        Hit(b[0]); Hit(b[1]); Hit(b[2]); Hit(b[3]); Hit(b[4]); Hit(b[5]);
        Hit(c[0]); Hit(c[1]); Hit(c[2]); Hit(c[3]);
    }
}
