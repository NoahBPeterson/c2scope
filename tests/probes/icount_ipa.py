# tools/icount.py's interprocedural hint (docs/c2/ipa.md). Run by run_tests.py's probe_modules() in its namespace
# (check, skip, lacking, BUILD, X64, HERE, VERBOSE). tests/data/icount_ipa.cpp: the same caller (a store to an
# address-taken local struct before a call) with the callee in the unit (c2's callee summary says it does not read
# the struct: the stores go) against the callee declared only (they stay), plus callees that do read.
import json, os, subprocess, sys, tempfile


def icount_ipa_tests():
    why = lacking("gopt") or ("x64 builds: not probed" if X64 else None)
    if why:
        skip("icount ipa: callee-summary hint (5 checks)", why)
        return
    src = os.path.join(HERE, "tests", "data", "icount_ipa.cpp")
    d = tempfile.mkdtemp()

    def run(p):
        js = os.path.join(d, p + ".json")
        r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "icount.py"), src, "--func", "::%s_ours" % p,
                            "--vs-func", "::%s_orig" % p, "--json", js, "--", "/O2", "/MT", "/Gy", "/TP"],
                           capture_output=True, text=True, cwd=HERE)
        if VERBOSE:
            print(r.stdout[-3000:], r.stderr[-2000:])
        j = json.load(open(js)) if os.path.exists(js) else {}
        whys = [it.get("why", "") for r_ in j.get("runs", []) for it in r_["items"]]
        return j.get("ours", 0) - j.get("orig", 0), sorted(j.get("classes", {})), any("interprocedural" in w for w in whys)

    # in-unit noinline callee that writes *out and never reads it: all four stores of the extern variant are gone
    check("icount ipa: callee body in the unit (noinline), never reads *out: 4 stores + xor gone, hint",
          run("ipa"), (-5, ["removed-by:DeadStores:callee-summary"], True))
    # the callee reads *out: same code as the extern call, nothing to explain
    check("icount ipa: callee body reads *out: same count, no hint", run("ipa_read"), (0, [], False))
    # the callee hands the pointer to another unit: counts as read
    check("icount ipa: callee passes the pointer to an extern: same count, no hint", run("ipa_pass"), (0, [], False))


icount_ipa_tests()
