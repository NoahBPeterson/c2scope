#!/usr/bin/env python3
"""lbench64: a fixed bench of functions for x64 lifter work. Lift each (lift2il64's variants), compile and compare
(lift2il64.check: C2_CL64 and x64check), and report matches, lift failures by reason and x64metrics' edit metrics.

    python3 tools/lbench64.py make NAME --min N --max N --count N [--seed S] [--from LIST]   pick functions by size
    python3 tools/lbench64.py run NAME [--tag T] [--jobs J] [--vs N] [--set NAME=VALUE]      lift + check; summary
    python3 tools/lbench64.py show NAME [--tag T]                                            the summary again
    python3 tools/lbench64.py compare NAME TAG_A TAG_B                                       row by row

The executable and index are --binary / --index (or LIFT64_BINARY / LIFT64_INDEX) as for lift2il64. `make` picks from
the index (or .pdata), or from --from LIST, a JSON list of [address, size] (e.g. the functions still open in a
project). A bench is work/lbench64/NAME.json; a run writes work/lbench64/NAME.TAG.json (one row per function), so
two runs compare row by row, on the local checker (x64check)."""
import argparse, collections, json, os, random, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lift2il64 as L    # noqa: E402

WORK = os.path.join(os.path.dirname(HERE), "work", "lbench64")


def make(name, lo, hi, count, seed, src=None):
    if src:
        fs = [(a, s) for a, s in json.load(open(src)) if lo <= s <= hi]
    else:
        fs = sorted(("0x%x" % v, e["size"]) for v, e in L.index(L.Image()).items() if lo <= e["size"] <= hi)
    random.Random(seed).shuffle(fs)
    os.makedirs(WORK, exist_ok=True)
    json.dump(fs[:count], open(os.path.join(WORK, name + ".json"), "w"))
    print(len(fs[:count]), "functions")


def _one(args):
    """One function: the lifter's first `vs` variants under the first two flag sets, the best result with metrics."""
    va, entry, work, binary, index, sets, vs = args
    L.BINARY, L.INDEX = binary, index
    for k, v in sets.items():
        setattr(L, k, v)
    row = dict(size=entry["size"])
    try:
        variants = L.variants(L.Image(), va, entry["size"])[:vs]
    except Exception as e:
        return "%x" % va, dict(row, status="lift", why=("%s: %s" % (type(e).__name__, e))[:90])
    best = None
    for o, text in variants:
        for fl in L.flag_sets(va, entry)[:2]:
            r = L.check(va, entry["size"], text, work, [fl])
            m = r.get("metrics") or {}
            r = dict(status=r["status"], edits=m.get("edits"), shape=m.get("edits_noreg"), mapcost=m.get("mapcost"),
                     n=m.get("n_orig"), ops=(m.get("ops") or [])[:20], flags=fl, opts=o,
                     why="" if r["status"] == "match" else str((r.get("errors") or [""])[0])[:120])
            if r["status"] == "match":
                return "%x" % va, dict(row, **r)
            key = r["edits"] if r["edits"] is not None else 1 << 30
            if best is None or key < (best["edits"] if best["edits"] is not None else 1 << 30):
                best = r
    return "%x" % va, dict(row, **best)


def run_laptop(fs, idx, vs):
    """The bench on the Windows compile machine (lift2il64.laptop_backend): lifts here, compiles there, one upload
    per chunk of 120 functions; rows as _one's."""
    lp = L.laptop_backend()
    img = L.Image()
    jobs, rows = [], {}
    for a, s in fs:
        va = int(a, 16)
        entry = dict(idx.get(va) or {}, size=s)
        try:
            texts = [t for _, t in L.variants(img, va, s)][:vs]
        except Exception as e:
            rows["%x" % va] = dict(size=s, status="lift", why=("%s: %s" % (type(e).__name__, e))[:90])
            continue
        jobs.append({"key": "%x" % va, "addr": va, "size": s, "cands": texts, "flagsets": L.flag_sets(va, entry)[:2]})
    res = lp.run_batch(jobs, chunk=120)
    for j in jobs:
        r = res.get(j["key"]) or {"status": "error"}
        rows[j["key"]] = dict(size=j["size"], status=r.get("status"), edits=r.get("edits"), shape=r.get("edits_noreg"),
                              mapcost=r.get("mapcost"), n=r.get("n_orig"), ops=(r.get("ops") or [])[:20],
                              flags=r.get("flags"),
                              why="" if r.get("status") == "match" else str((r.get("errors") or [""])[0])[:120])
    return rows


def run(name, tag, jobs, vs, sets, laptop=False):
    from concurrent.futures import ProcessPoolExecutor
    fs = json.load(open(os.path.join(WORK, name + ".json")))
    idx = L.index(L.Image())
    work = os.path.join(WORK, "%s.%s" % (name, tag))
    args = []
    for a, s in fs:
        va = int(a, 16)
        args.append((va, dict(idx.get(va) or {}, size=s), work, L.BINARY, L.INDEX, sets, vs))
    t0 = time.time()
    rows = {}
    if laptop:
        for k, v in sets.items():
            setattr(L, k, v)
        rows = run_laptop(fs, idx, vs)
    else:
        with ProcessPoolExecutor(min(jobs, 6)) as ex:
            for k, r in ex.map(_one, args):
                rows[k] = r
    json.dump(rows, open(os.path.join(WORK, "%s.%s.json" % (name, tag)), "w"), indent=0)
    summary(rows, time.time() - t0)


def summary(rows, secs=0):
    st = collections.Counter(r["status"] for r in rows.values())
    print("functions %d  %s  (%.0fs)" % (len(rows), dict(st), secs))
    ok = [r for r in rows.values() if r.get("shape") is not None and r.get("n")]
    if ok:
        sh = sum(r["shape"] for r in ok) / sum(r["n"] for r in ok)
        ed = sum(r["edits"] for r in ok) / sum(r["n"] for r in ok)
        mc = sum(r["mapcost"] or 0 for r in ok) / len(ok)
        near = sum(1 for r in ok if r["shape"] <= 2)
        print("compared %d: edits %.1f%%  shape %.1f%% of instructions, mapcost %.1f/function, shape<=2: %d"
              % (len(ok), 100 * ed, 100 * sh, mc, near))
    why = collections.Counter(r["why"].split(":")[0] + ":" + r["why"].split(":")[1][:50] if r["status"] == "lift"
                              and ":" in r["why"] else r["why"][:60] for r in rows.values() if r["status"] in
                              ("lift", "does not build", "error"))
    for w, n in why.most_common(14):
        print("  %3d  %s" % (n, w))


def compare(name, a, b):
    """Row by row: what changed between two runs of one bench."""
    ra = json.load(open(os.path.join(WORK, "%s.%s.json" % (name, a))))
    rb = json.load(open(os.path.join(WORK, "%s.%s.json" % (name, b))))
    better = worse = 0
    for k in ra:
        x, y = ra[k], rb.get(k, {})
        if y.get("status") == "match" and x.get("status") != "match":
            print("new match", k)
        if x.get("status") == "match" and y.get("status") != "match":
            print("LOST match", k)
        if x.get("shape") is not None and y.get("shape") is not None:
            better += y["shape"] < x["shape"]
            worse += y["shape"] > x["shape"]
    print("shape better %d, worse %d" % (better, worse))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("make", "run", "show", "compare"))
    ap.add_argument("name")
    ap.add_argument("other", nargs="?")
    ap.add_argument("--binary")
    ap.add_argument("--index")
    ap.add_argument("--from", dest="src", help="JSON list of [address, size] to pick from (make)")
    ap.add_argument("--min", type=int, default=200)
    ap.add_argument("--max", type=int, default=1200)
    ap.add_argument("--count", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--tag", default="base")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--set", action="append", default=[], help="NAME=VALUE: a lift2il64 module default, e.g. RETVAR=1")
    ap.add_argument("--vs", type=int, default=2, help="lifter variants tried per function")
    ap.add_argument("--laptop", action="store_true", help="compile on the Windows compile machine (tools/laptop64.py)")
    a = ap.parse_args()
    L.BINARY, L.INDEX = a.binary or L.BINARY, a.index or L.INDEX
    sets = {}
    for kv in a.set:
        k, v = kv.split("=", 1)
        sets[k] = int(v) if v.isdigit() else v
    if a.cmd == "make":
        make(a.name, a.min, a.max, a.count, a.seed, a.src)
    elif a.cmd == "run":
        run(a.name, a.tag, a.jobs, a.vs, sets, a.laptop)
    elif a.cmd == "compare":
        compare(a.name, a.tag, a.other)
    else:
        summary(json.load(open(os.path.join(WORK, "%s.%s.json" % (a.name, a.tag)))))
