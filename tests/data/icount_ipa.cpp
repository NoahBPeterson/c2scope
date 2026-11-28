// probes for icount's interprocedural hint (docs/c2/ipa.md): a store to an address-taken local before a call is
// deleted when the callee's body is in this unit and never reads through the pointer (c2's callee summary,
// callee.md); with the callee in another unit the call may read it and the store stays.
// X_ours / X_orig pairs: ours has the callee in the unit, the original has it extern (run with --vs-func).
struct R { char* url; char* headers; char* body; int len; };
extern int sink;
extern int ext_fetch(const char* u, int n, R* out);          // another unit
__declspec(noinline) int in_fetch(const char* u, int n, R* out)    // this unit, writes *out, never reads it
{ out->body = (char*)u; out->len = n; sink = 1; return sink; }
__declspec(noinline) int in_read(const char* u, int n, R* out)     // this unit, reads *out
{ out->body = out->url; return sink; }
extern int ext_pass(R* out);
__declspec(noinline) int in_pass(const char* u, int n, R* out)     // this unit, hands the pointer to another unit
{ return ext_pass(out); }

#define BODY(CALL) R r; r.url = 0; r.headers = 0; r.body = 0; r.len = 0; if (CALL > 0) return -1; return 0;
int ipa_ours(void) { BODY(in_fetch("a", 1, &r)) }
int ipa_orig(void) { BODY(ext_fetch("a", 1, &r)) }
// the callee reads through the pointer: the stores stay, no hint
int ipa_read_ours(void) { BODY(in_read("a", 1, &r)) }
int ipa_read_orig(void) { BODY(ext_fetch("a", 1, &r)) }
// the callee passes the pointer to another unit: it counts as read, no hint
int ipa_pass_ours(void) { BODY(in_pass("a", 1, &r)) }
int ipa_pass_orig(void) { BODY(ext_fetch("a", 1, &r)) }
