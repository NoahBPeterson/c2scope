/* When a reload folds into the named variable (docs/c2/regalloc.md 3.6.10).  params is address-taken, so its dwords
   are aliased candidates: Enterloads gives a dword a reload (OPENTERLOAD) web only when the stretch after the reload
   holds two or more references.  fa: the copy into t and the copy-propagated read for use(t) before the call ->
   `mov eax,[params]; mov esi,eax`.  fb: the copy is the only read before mark() (the call may write params, so the
   optimizer cannot propagate the dword past it) -> `mov esi,[params]`.  Same on 13.10, 14.00, 15.00. */
int chk(int); void use(int); void fill(__int64 *); void mark();
void fa(int *q)
{
    __int64 params[8];
    fill(params);
    if (chk((int)params[2])) {
        int t = (int)params[2];
        use(t);
        mark();
        use(t);
    }
}
void fb(int *q)
{
    __int64 params[8];
    fill(params);
    if (chk((int)params[2])) {
        int t = (int)params[2];
        mark();
        use(t);
        use(t);
    }
}
