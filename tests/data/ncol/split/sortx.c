/* Sorting with records and index arithmetic: heap sort, insertion sort, a merge and a median-of-three quicksort. */
typedef struct Rec { int key; int sub; short tag; char name[10]; } Rec;
extern int rec_cmp_hook(const Rec *a, const Rec *b);

static int cmp(const Rec *a, const Rec *b)
{
    if (a->key != b->key) return a->key < b->key ? -1 : 1;
    if (a->sub != b->sub) return a->sub < b->sub ? -1 : 1;
    return a->tag - b->tag;
}

static void sift(Rec *v, int start, int end)
{
    int root = start;
    while (2 * root + 1 <= end) {
        int child = 2 * root + 1, sw = root;
        if (cmp(&v[sw], &v[child]) < 0) sw = child;
        if (child + 1 <= end && cmp(&v[sw], &v[child + 1]) < 0) sw = child + 1;
        if (sw == root) return;
        { Rec t = v[root]; v[root] = v[sw]; v[sw] = t; }
        root = sw;
    }
}

void heapsort_recs(Rec *v, int n)
{
    int start, end;
    for (start = (n - 2) / 2; start >= 0; start--)
        sift(v, start, n - 1);
    for (end = n - 1; end > 0; end--) {
        Rec t = v[end]; v[end] = v[0]; v[0] = t;
        sift(v, 0, end - 1);
    }
}

void isort(int *a, int lo, int hi, int *moves)
{
    int i, j, m = 0;
    for (i = lo + 1; i <= hi; i++) {
        int x = a[i];
        for (j = i - 1; j >= lo && a[j] > x; j--) {
            a[j + 1] = a[j];
            m++;
        }
        a[j + 1] = x;
    }
    *moves += m;
}

void qsort_int(int *a, int lo, int hi, int depth, int *moves)
{
    while (hi - lo > 16) {
        int mid = lo + (hi - lo) / 2, i = lo, j = hi, p;
        if (a[mid] < a[lo]) { int t = a[mid]; a[mid] = a[lo]; a[lo] = t; }
        if (a[hi] < a[lo]) { int t = a[hi]; a[hi] = a[lo]; a[lo] = t; }
        if (a[hi] < a[mid]) { int t = a[hi]; a[hi] = a[mid]; a[mid] = t; }
        p = a[mid];
        while (i <= j) {
            while (a[i] < p) i++;
            while (a[j] > p) j--;
            if (i <= j) { int t = a[i]; a[i] = a[j]; a[j] = t; i++; j--; (*moves)++; }
        }
        if (depth > 40) { isort(a, lo, hi, moves); return; }
        if (j - lo < hi - i) { qsort_int(a, lo, j, depth + 1, moves); lo = i; }
        else { qsort_int(a, i, hi, depth + 1, moves); hi = j; }
    }
    isort(a, lo, hi, moves);
}

int merge_runs(const int *a, int na, const int *b, int nb, int *out, int dedup)
{
    int i = 0, j = 0, k = 0, last = 0, have = 0;
    while (i < na && j < nb) {
        int x = a[i] <= b[j] ? a[i++] : b[j++];
        if (dedup && have && x == last) continue;
        out[k++] = x; last = x; have = 1;
    }
    while (i < na) { int x = a[i++]; if (!(dedup && have && x == last)) { out[k++] = x; last = x; have = 1; } }
    while (j < nb) { int x = b[j++]; if (!(dedup && have && x == last)) { out[k++] = x; last = x; have = 1; } }
    return k;
}
