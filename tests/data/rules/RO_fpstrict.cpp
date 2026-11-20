// opt.md §9 rules 1, 3, 4: float CSE under /fp:precise, /fp:strict (none), /fp:fast (float temp).
float hf(float);
float fp_cse(float a, float b) { return (a * b) + hf(a * b); }
