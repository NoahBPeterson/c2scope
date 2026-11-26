#!/usr/bin/env python3
"""Check tidy64 candidates with x64check at /W3: a candidate counts only when it matches and builds without warnings.

    python3 tools/tidycheck64.py one <lifted.cpp> <pack dir> [--flags a,b,c] [--binary EXE]
    python3 tools/tidycheck64.py bench <bench.json> <lifted root> <out dir> [--jobs N] [--limit N] [--binary EXE]

A pack dir is the per-function folder lift2il64 writes (pack.json: address, size, flags, accept_also). The compiler is
C2_CL64 and the executable LIFT64_BINARY (or --binary), as for lift2il64.
`one` prints the first candidate that matches (most rewritten first) and writes it to <pack dir>/tidy.cpp."""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tidy64  # noqa: E402
import x64check  # noqa: E402

_pe = None


def image():
    global _pe
    if _pe is None:
        path = os.environ.get("LIFT64_BINARY")
        if not path:
            raise SystemExit("tidycheck64: give the executable with --binary (or LIFT64_BINARY)")
        _pe = x64check.Pe(path)
    return _pe


def flag_sets(pack):
    pj = json.load(open(os.path.join(pack, "pack.json")))
    sets = [pj["flags"]] + [list(x) for x in pj.get("accept_also") or []]
    return sets


def check_text(text, pack, flags=None):
    """The x64check result for one candidate under each accepted flag set; status 'match' only when it builds clean
    at /W3."""
    pe = image()
    pj = json.load(open(os.path.join(pack, "pack.json")))
    src = os.path.join(pack, "tidy_try.cpp")
    open(src, "w").write(text)
    best = None
    for fl in ([flags] if flags else flag_sets(pack)):
        r = x64check.check(pe, int(pj["address"], 16), pj["size"], src, list(fl), warn_clean=True)
        if r["status"] == "match":
            return r
        if best is None:
            best = r
    return best


def one(lifted_text, pack, flags=None):
    """First tidy candidate that matches: (opts, text, result) or (None, None, last result / error)."""
    try:
        cands = tidy64.tidy(lifted_text)
    except ValueError as e:
        return None, None, {"status": "tidy-refused", "why": str(e)}
    last = None
    for o, t in cands:
        r = check_text(t, pack, flags)
        if r["status"] == "match":
            return o, t, r
        last = dict(r, opts=o)
    return None, None, last


def greedy(lifted_text, pack, o, flags=None, budget=40):
    """From a matching rung o, add copy / inline moves one at a time, keeping each that still matches."""
    o = dict(o, inline=1, copy=1, copy_allow=set(o.get("copy_allow") or ()), inline_allow=set(o.get(
        "inline_allow") or ()))
    best_t, best_r = None, None
    tried = set()
    n = 0
    while n < budget:
        moves = [m for m in tidy64.candidates(lifted_text, o) if m not in tried]
        if not moves:
            break
        progressed = False
        for m in moves:
            tried.add(m)
            o2 = dict(o, copy_allow=set(o["copy_allow"]), inline_allow=set(o["inline_allow"]))
            (o2["copy_allow"] if m[0] == "copy" else o2["inline_allow"]).add(m[1])
            try:
                t = tidy64.tidy_one(lifted_text, o2)
            except Exception:
                continue
            n += 1
            r = check_text(t, pack, flags)
            if r["status"] == "match":
                o, best_t, best_r, progressed = o2, t, r, True
                break
            if n >= budget:
                break
        if not progressed:
            break
    return o, best_t, best_r


def _bench_one(a):
    va, flags, root, out = a
    t0 = time.time()
    src = os.path.join(root, va, "lifted.cpp")
    d = os.path.join(out, va)
    os.makedirs(d, exist_ok=True)
    pk = json.load(open(os.path.join(root, va, "pack.json")))
    pk["flags"] = flags
    json.dump(pk, open(os.path.join(d, "pack.json"), "w"))
    o, t, r = one(open(src).read(), d)
    if t and not o.get("inline"):
        go, gt, gr = greedy(open(src).read(), d, dict(o, inline_allow=set()))
        if gt:
            o, t, r = dict(go, inline_allow=sorted(go["inline_allow"]), copy_allow=sorted(go["copy_allow"]),
                           greedy=1), gt, gr
    row = dict(va=va, secs=round(time.time() - t0, 1))
    if t:
        open(os.path.join(d, "tidy.cpp"), "w").write(t)
        row.update(status="MATCH", opts=o)
    else:
        row.update(status=(r or {}).get("status", "?"), why=str((r or {}).get("why") or (r or {}).get("errors") or
                                                                 (r or {}).get("differing_instructions"))[:200])
    return row


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("one", "bench"))
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("c", nargs="?")
    ap.add_argument("--flags")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--binary")
    a = ap.parse_args(argv)
    if a.binary:
        os.environ["LIFT64_BINARY"] = os.path.abspath(a.binary)
    if a.cmd == "one":
        o, t, r = one(open(a.a).read(), a.b, a.flags.split(",") if a.flags else None)
        if t:
            open(os.path.join(a.b, "tidy.cpp"), "w").write(t)
            print("MATCH", o)
            print(t)
            return 0
        print("NO", json.dumps(r, default=str)[:3000])
        return 1
    rows = [r for r in json.load(open(a.a)) if r["status"] == "MATCH"]
    root, out = a.b, a.c
    todo = []
    for r in rows:
        p = os.path.join(root, r["va"], "lifted.cpp")
        if not os.path.isfile(p):
            continue
        txt = open(p).read()
        todo.append((r["va"], r["flags"].split(), root, out))
    if a.limit:
        todo = todo[:a.limit]
    from concurrent.futures import ProcessPoolExecutor
    import collections
    res = []
    with ProcessPoolExecutor(a.jobs) as ex:
        for row in ex.map(_bench_one, todo):
            res.append(row)
            print(json.dumps(row), flush=True)
    print("summary:", dict(collections.Counter(r["status"] for r in res)), "of", len(res))
    print("rungs:", dict(collections.Counter(json.dumps(r.get("opts")) for r in res if r["status"] == "MATCH")))
    json.dump(res, open(os.path.join(out, "tidybench.json"), "w"), indent=0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
