// T25's synthetic repro (our own code, per T20): a do-while walker whose "found"
// pointer GlobalOptimize copy-propagates into the walker, so the variable never
// becomes an NColor candidate. Plus one local of every reason family the absent
// report must name: volatile, address-only, and a plain enregisterable local.
struct Node { Node* next; void* data; };

int Find(Node* head, void* key, void** out) {
    Node* found = 0;                       // the vanishing local
    Node* p = head;
    if (p) {
        do {
            if (p->data == key) { found = p; break; }
            p = p->next;
        } while (p);
    }
    if (found) { *out = found->data; return 1; }
    return 0;
}

int Volat(int n) {
    volatile int v = n;
    return v + 1;
}

int AddrOnly(int n) {
    int a = n;
    int* q = &a;
    return *q + n;
}

int Plain(int n) {
    int r = n * 3;
    return r + 1;
}
