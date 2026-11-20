// feshape probe (docs/c2/effectmine.md section 7): the same test written two ways; /DASSIGN_IN_COND assigns in the
// condition, which adds an OPPAREN of the variable and turns the branch condition around in the reader IL
float fe_src();
int fe_f()
{
    float f;
#ifdef ASSIGN_IN_COND
    if ((f = fe_src()) >= 0.5f)
        return 1;
#else
    f = fe_src();
    if (f >= 0.5f)
        return 1;
#endif
    return f >= -0.5f ? 2 : 3;
}
