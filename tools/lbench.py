#!/usr/bin/env python3
"""Lift benchmark: how many functions of a program the lifter turns into C++ that compiles to the original's bytes.

usage: lbench.py run CORPUS [--status matched,residual] [--sample N] [--seed S] [--max-size B] [--jobs J]
                            [--tag NAME] [--same-as RUNDIR] [--c2 BUILD]
       lbench.py show RUNDIR                     the summary of one run
       lbench.py compare RUNDIR_A RUNDIR_B       what changed: per status counts, new and lost matches

CORPUS is a corpus file (tools/corpus.py; work/corpus/<name>.jsonl). For each sampled function the lifter
(w8_emit.emit_one) writes a self-contained unit from the original's machine code; it is compiled with the
function's own compiler build (C2_BUILD) and flags and scored under spore-decomp's CI rule, with the corpus's mask
and fixups (tools/ci/fingerprint.py::check):
  MATCH      same size; every byte outside the mask equal; each of our relocations on masked bytes; each retail
             .reloc fixup covered by one of our relocations (a number where the original has an address fails)
  DIFF       compiled, differs: bdiff (differing unmasked bytes over the common length), osz/size, ins (share of
             the original's instructions aligned with ours, registers erased: w8_diff.canon_ins)
  CL-ERROR   the unit does not compile          EMIT-ERROR  the lifter raised        NO-SYMBOL  FUN_<va> missing
Functions with a known matching source (status matched) measure the lifter on ground truth: a source that matches
exists, so every DIFF there is the lifter's shortfall, not an impossible function.

The sample is stratified by status and deterministic (seed); --same-as reuses another run's functions exactly, so
two lifter versions are compared on the same set. Output: work/lbench/<corpus>/<tag>/{results.jsonl,summary.json,
src/<va>.cpp}. Nothing of the image is written beyond addresses and counts.
"""
import argparse, collections, difflib, hashlib, json, os, random, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import corpus as corpusmod

BENCH = os.path.join(ROOT, "work", "lbench")


def c2_name(b):
    """corpus build names -> c2build names (C2_BUILD)."""
    return {"15.00": "15.00", "14.00": "14.00", "13.10": "13.10", "13.10.3052": "3052", "13.10.3077": "13.10"}.get(b, b)


def band(n):
    return "a<=64" if n <= 64 else "b65-256" if n <= 256 else "c257-1K" if n <= 1024 else "d>1K"


def pick(corp, status, n, seed, max_size, min_size, bands=False):
    """n per status, or with bands n per (status, size band), so programs compare at the same sizes."""
    rows = corp.select(status=status, max_size=max_size, min_size=min_size)
    by = collections.defaultdict(list)
    for r in rows:
        by[r["status"] + ("/" + band(r["size"]) if bands else "")].append(r)
    out = []
    for st in sorted(by):
        rs = sorted(by[st], key=lambda r: r["va"])
        out += rs if not n or len(rs) <= n else random.Random("%s/%s/%d" % (corp.name, st, seed)).sample(rs, n)
    return sorted(out, key=lambda r: r["va"])


# ---- scoring (spore-decomp tools/ci/fingerprint.py::check, with the corpus's mask) ----

def obj_function(obj, sym):
    """Our function's bytes in an object (by its exact symbol name) and its relocation offsets."""
    from coff import parse_coff
    secs, syms, _ = parse_coff(obj, sizes=True)
    if sym not in syms:
        # spore-decomp manifests may name a substring of the decorated name (run_all.py's rule: a unique match)
        c = [s for s in syms if sym in s and not s.startswith(("__ehhandler", "__unwindfunclet", "__catch", "__tryend", "$"))]
        if len(c) != 1:
            raise RuntimeError(sym)
        sym = c[0]
    si, off, msz = syms[sym]
    sec = secs[si]
    if msz:
        mine = sec["data"][off:off + msz]
    else:
        starts = sorted(o for s_, o, _z in syms.values() if s_ == si and o > off)
        # trailing int3 dropped even when no symbol follows, as spore-decomp's fingerprint.py does (under /Gy a
        # function is alone in its section; cl's int3 after a noreturn call: M&B 0x40f776, a bare `throw;`)
        mine = sec["data"][off:starts[0] if starts else len(sec["data"])].rstrip(b"\xcc")
    # SECREL (11) and SECTION (10) give a section-relative constant (a thread-local variable's offset in .tls) that the
    # linker assigns: retail has no fixup there, and the value is not the compiler's. Returned apart, to be masked.
    rel = sorted({v - off for v, _s, t in sec["relocs"] if off <= v < off + len(mine) and t not in (10, 11)})
    mine_secrel = sorted({v - off for v, _s, t in sec["relocs"] if off <= v < off + len(mine) and t in (10, 11)})
    return mine, rel, mine_secrel, (off, len(sec["data"]))


def score(obj, row, img, sym=None):
    import w8_diff
    va, size = int(row["va"], 16), row["size"]
    if hasattr(w8_diff, "SIZE_HINT"):           # the row's size decides whether a trailing catch funclet is compared
        w8_diff.SIZE_HINT[va] = size
    try:
        mine, rels, secrel, span = obj_function(obj, sym) if sym else w8_diff.ours_bytes(obj, va) + ([], None)
    except RuntimeError:
        return dict(status="NO-SYMBOL")
    mine = bytes(mine).rstrip(b"\xcc")         # fingerprint.py drops our trailing int3 whatever path found the bytes
    theirs = bytes(img.bytes_at(va, size))
    mask = corpusmod.unpack(row["mask"]) | {o + k for o in secrel for k in range(4)}
    n = min(len(mine), size)
    bdiff = sum(1 for i in range(n) if i not in mask and theirs[i] != mine[i])
    # an image without a .reloc table (an unpacked executable) gives no mask for address tables inside the function
    # (switch tables are data, not instructions): where we relocate and retail's dword there is an address in its
    # image, retail has an address too
    if not img.relocs:
        for r in rels:
            if r + 4 <= size and img.lo <= int.from_bytes(theirs[r:r + 4], "little") < img.hi:
                mask |= {r, r + 1, r + 2, r + 3}
        n = min(len(mine), size)
        bdiff = sum(1 for i in range(n) if i not in mask and theirs[i] != mine[i])
    relmask = {r + k for r in rels for k in range(4)}
    stray = sorted(r for r in relmask if r < len(mine) and r not in mask)       # we relocate where retail has none
    bare = [o for o in row.get("fixups", []) if o not in set(rels)]             # a number where retail has an address
    # a rel32 call or jump that leaves the function is masked, but ours must reach it through a relocation too: raw
    # bytes there (`_emit`, a transcription) are a fixed displacement, not a call to a name (2026-11-25: five _emit
    # slices scored MATCH here that spore-decomp's check.py rejects)
    rs = set(rels)
    for i in w8_diff.dis(theirs, va):
        b = bytes(i.bytes)
        if len(b) >= 5 and (b[0] in (0xe8, 0xe9) or (b[0] == 0x0f and 0x80 <= b[1] <= 0x8f and len(b) == 6)):
            t = (i.address + len(b) + int.from_bytes(b[-4:], "little", signed=True)) & 0xffffffff
            o = i.address - va + len(b) - 4
            if not va < t < va + size and img.lo <= t < img.hi and o in mask:   # not a jump table read as code
                # unless ours reaches a place in its own section (a catch funclet after the function's end: no
                # relocation needed, M&B's _Insert_n)
                own = span and o + 4 <= len(mine) and \
                    0 <= span[0] + o + 4 + int.from_bytes(mine[o:o + 4], "little", signed=True) < span[1]
                if o < len(mine) and o not in rs and not own:
                    bare.append(o)
    rk = [w8_diff.canon_ins(i) for i in w8_diff.dis(theirs, va)]
    ok = [w8_diff.canon_ins(i) for i in w8_diff.dis(mine, va)]
    same = sum(b.size for b in difflib.SequenceMatcher(None, rk, ok, autojunk=False).get_matching_blocks())
    st = "MATCH" if (len(mine) == size and bdiff == 0 and not stray and not bare) else "DIFF"
    import diffclass
    if st == "MATCH":
        cls, det = "match", {}
    elif len(mine) == size and bdiff == 0:
        cls, det = "naming", {}                  # the bytes agree; a relocation is missing or extra (a name, not a number)
    else:
        cls, det = diffclass.classify(theirs, mine, va, mask)
    det = det if st != "MATCH" and cls not in ("match", "naming") else {}
    rd = w8_diff.dis(theirs, va)
    nbr = sum(1 for i in rd if i.mnemonic.startswith("j") and i.mnemonic != "jmp")
    return dict(status=st, cls=cls, det=det, bdiff=bdiff, osz=len(mine), ins=round(100.0 * same / max(1, len(rk)), 1),
                rins=len(rk), rbr=nbr,
                stray=len(stray), bare=len(bare))


ASM_RE = re.compile(r"\b(?:__asm|_asm|_emit)\b|__declspec\s*\(\s*naked\s*\)")


def asm_body(src, va, sym):
    """Whether the function's source is inline assembly (__asm, _emit, naked): a transcription, which no project
    counts as a decompile. The function's text runs from its `// @ 0x<va>` marker (spore-decomp) or its name to the
    next marker or the end of the file; without either, assembly before the file's first marker counts."""
    try:
        text = open(src, errors="replace").read()
    except OSError:
        return False
    m = re.search(r"//\s*@\w*\s+0x0*%s\b" % va.lstrip("0"), text, re.I)
    if not m and sym:
        nm = re.sub(r"^[?_@]+", "", sym).split("@")[0]
        m = re.search(r"\b%s\s*\(" % re.escape(nm), text) if nm else None
    if not m:                                     # only text outside every other function's marker region counts
        first = re.search(r"//\s*@\w*\s+0x[0-9a-f]+", text, re.I)
        return bool(ASM_RE.search(text[:first.start()] if first else text))
    nxt = re.search(r"//\s*@\w*\s+0x[0-9a-f]+", text[m.end():], re.I)
    return bool(ASM_RE.search(text[m.start():m.end() + (nxt.start() if nxt else len(text))]))


def run_src(a, corp, rows, outdir):
    """--task src: compile each function's own source (once per source and flags) and score it."""
    import w8_score
    img = corp.image()
    units = collections.OrderedDict()
    for r in rows:
        if not r.get("src"):
            continue
        fl = " ".join([r["flags"]] + r.get("inc", []) + corp.head.get("src_inc", []))
        units.setdefault((r["src"], fl), []).append(r)
    keys = list(units)
    tasks = [(s, fl, "lbs_" + hashlib.md5((s + fl + r_c2(units[(s, fl)])).encode()).hexdigest()[:12]) for s, fl in keys]
    t0 = time.time()
    with ThreadPoolExecutor(a.jobs) as ex:
        built = list(ex.map(w8_score.compile_one, tasks))
    need = [(r, obj) for k, (obj, _l) in zip(keys, built) if obj for r in units[k] if not r.get("sym")]
    found = dict(zip([r["va"] for r, _o in need], corpusmod.resolve_symbols(corp.head, need)))
    res = {}
    for k, (obj, log) in zip(keys, built):
        for r in units[k]:
            if obj is None:
                lines = (log or "").strip().splitlines()
                err = next((l.strip() for l in lines if " error " in l), lines[0] if lines else "cl failed")
                res[r["va"]] = dict(status="CL-ERROR", err=err[:160])
            else:
                sym = r.get("sym") or found.get(r["va"])
                res[r["va"]] = dict(score(obj, r, img, sym=sym), sym=sym) if sym else dict(status="NO-SYMBOL")
                if res[r["va"]]["status"] == "MATCH" and asm_body(r["src"], r["va"], sym):
                    res[r["va"]]["status"] = "ASM"        # the bytes match, but the source is assembly, not C++
    out = [dict(va=r["va"], size=r["size"], fstatus=r["status"], c2=r["c2"], src=r.get("src"),
                flags=" ".join([r["flags"]] + r.get("inc", []) + corp.head.get("src_inc", [])),
                **res.get(r["va"], dict(status="NO-SOURCE"))) for r in rows]
    print("  c2 %s: %d functions, %d units in %.0f s" % (rows[0]["c2"] if rows else "-", len(rows), len(keys),
                                                         time.time() - t0), file=sys.stderr)
    return out


def il2c_one(corpus_path, va, outdir):
    """Worker (one process per function, C2_BUILD set): the real source's IL at Lower's entry written back as C++ by
    il2c.emit_unit, compiled with the function's real flags, scored. Returns a result dict."""
    import il2c, shutil, tempfile, w8_score
    corp = corpusmod.Corpus(corpus_path)
    r = next(x for x in corp.rows if x["va"] == va)
    cl = r["flags"].split() + r.get("inc", []) + corp.head.get("src_inc", [])
    keep = tempfile.mkdtemp(prefix="lb-il2c-")
    try:
        try:
            unit, il, _body = il2c.emit_unit(r["src"], r["sym"], cl, keep)
        except il2c.Unsupported as e:
            return dict(status="UNSUPPORTED", err=str(e)[:120])
        except SystemExit as e:
            return dict(status="UNSUPPORTED", err=("exit: " + str(e))[:120])
        except Exception as e:
            return dict(status="EMIT-ERROR", err=("%s: %s" % (type(e).__name__, e))[:160])
    finally:
        shutil.rmtree(keep, ignore_errors=True)
    p = os.path.join(outdir, "src", va + ".cpp")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(unit)
    fl = " ".join(cl + ["/I" + os.path.dirname(os.path.abspath(r["src"]))])
    obj, log = w8_score.compile_one((p, fl, "lbi_" + hashlib.md5((va + fl + unit).encode()).hexdigest()[:12]))
    if obj is None:
        lines = (log or "").strip().splitlines()
        return dict(status="CL-ERROR", err=next((l.strip() for l in lines if " error " in l), "cl failed")[:160])
    return dict(score(obj, r, corp.image(), sym=r["sym"]), tuples=sum(1 for l in il if l.startswith("I ")))


def run_il2c(a, corp, rows, outdir):
    """--task il2c: the ceiling of a bytes -> IL -> C++ route (exact IL from the real source, written back by il2c)."""
    rows = [r for r in rows if r.get("src") and r.get("sym")]
    t0 = time.time()

    def job(r):
        p = subprocess.run([sys.executable, os.path.abspath(__file__), "_il2c", a.corpus, r["va"], outdir],
                           capture_output=True, text=True, env=dict(os.environ), timeout=900)
        try:
            return json.loads(p.stdout.strip().splitlines()[-1])
        except Exception:
            return dict(status="EMIT-ERROR", err=(p.stderr or p.stdout or "no output")[-160:])
    with ThreadPoolExecutor(a.jobs) as ex:
        got = list(ex.map(job, rows))
    print("  c2 %s: %d functions in %.0f s" % (rows[0]["c2"] if rows else "-", len(rows), time.time() - t0),
          file=sys.stderr)
    return [dict(va=r["va"], size=r["size"], fstatus=r["status"], c2=r["c2"], **g) for r, g in zip(rows, got)]


def r_c2(rs):
    return rs[0]["c2"]


def run_group(a, corp, rows, outdir):
    if getattr(a, "task", "lift") == "src":
        return run_src(a, corp, rows, outdir)
    if getattr(a, "task", "lift") == "il2c":
        return run_il2c(a, corp, rows, outdir)
    lift2il = getattr(a, "task", "lift") == "lift2il"
    import w8_emit, w8_score
    img = corp.image()
    w8_emit.MASKS = {int(r["va"], 16): corpusmod.unpack(r["mask"]) for r in rows}
    eimg = w8_emit.Image(corp.head["image"])
    srcdir = os.path.join(outdir, "src")
    os.makedirs(srcdir, exist_ok=True)
    tasks, res = [], {}
    import signal

    class EmitTimeout(Exception):
        pass

    def _alarm(_sig, _frm):
        raise EmitTimeout()
    signal.signal(signal.SIGALRM, _alarm)
    for r in rows:
        va = int(r["va"], 16)
        signal.alarm(a.emit_timeout)              # one function's lifting may not stall the run (2026-11-25: a quadratic pass)
        try:
            if lift2il:                           # bytes -> full IL at Lower's entry (lift2il) -> C++ (il2c)
                import il2c_lift
                text, _lf = il2c_lift.unit(eimg, va, r["size"], corpusmod.unpack(r["mask"]), flags=r["flags"])
            else:
                text, _fn = w8_emit.emit_one(eimg, va, r["size"])
        except EmitTimeout:
            res[r["va"]] = dict(status="TIMEOUT", err="lifting took over %d s" % a.emit_timeout)
            continue
        except Exception as e:                    # the lifter's own failures are a result, not a crash
            st = "UNSUPPORTED" if type(e).__name__ == "Unsupported" else "EMIT-ERROR"
            res[r["va"]] = dict(status=st, err="%s: %s" % (type(e).__name__, str(e)[:120]))
            continue
        finally:
            signal.alarm(0)
        p = os.path.join(srcdir, r["va"] + ".cpp")
        with open(p, "w") as f:
            f.write(text + "\n")
        fl = r["flags"] + (" /Z7" if lift2il else "")
        tag = "lb_" + hashlib.md5((corp.name + r["va"] + fl + r["c2"] + text).encode()).hexdigest()[:12]
        tasks.append((r, (p, fl, tag)))
    t0 = time.time()
    with ThreadPoolExecutor(a.jobs) as ex:
        built = list(ex.map(lambda t: w8_score.compile_one(t[1]), tasks))
    for (r, _t), (obj, log) in zip(tasks, built):
        if obj is None:
            lines = (log or "").strip().splitlines()
            err = next((l.strip() for l in lines if " error " in l), lines[0] if lines else "cl failed")
            res[r["va"]] = dict(status="CL-ERROR", err=err[:160])
        else:
            res[r["va"]] = score(obj, r, img)
    out = []
    for r in rows:
        x = dict(va=r["va"], size=r["size"], fstatus=r["status"], c2=r["c2"], **res[r["va"]])
        out.append(x)
    print("  c2 %s: %d functions in %.0f s" % (r["c2"], len(rows), time.time() - t0), file=sys.stderr)
    return out


def cmd_run(a):
    corp = corpusmod.Corpus(a.corpus)
    if a.same_as:
        vas = [json.loads(l)["va"] for l in open(os.path.join(a.same_as, "results.jsonl"))]
        byva = {r["va"]: r for r in corp.rows}
        rows = [byva[v] for v in vas if v in byva]
    elif a.vas:                                   # an explicit VA list (one hex VA per line), optionally narrowed
        want = set(w.lower().replace("0x", "").zfill(8) for w in open(a.vas).read().split())
        rows = [r for r in corp.rows if r["va"] in want and (not a.status or r["status"] in a.status.split(","))
                and (not a.max_size or r["size"] <= a.max_size) and (not a.min_size or r["size"] >= a.min_size)
                and (not a.flag or a.flag in r["flags"].split())]
        if a.sample and len(rows) > a.sample:
            rows = random.Random(a.seed).sample(rows, a.sample)
        rows.sort(key=lambda r: r["va"])
    else:
        rows = pick(corp, a.status.split(",") if a.status else None, a.sample, a.seed, a.max_size, a.min_size, a.bands)
    tag = a.tag or time.strftime("%m%d-%H%M")
    outdir = a.out or os.path.join(BENCH, corp.name, tag)
    os.makedirs(outdir, exist_ok=True)
    if a.c2:                                      # child: one build
        rows = [r for r in rows if r["c2"] == a.c2]
        out = run_group(a, corp, rows, outdir)
        with open(os.path.join(outdir, "part-%s.jsonl" % a.c2), "w") as f:
            for x in out:
                f.write(json.dumps(x) + "\n")
        return
    builds = sorted({r["c2"] for r in rows})
    ecommit, edirty = emitter_state()
    h_start = lifter_hash()
    if edirty and a.task in ("lift", "lift2il"):
        print("WARNING: the lifters have uncommitted edits; this run measures a working tree, not commit %s"
              % ecommit, file=sys.stderr)
    print("%s: %d functions (%s), builds %s -> %s" % (corp.name, len(rows),
          dict(collections.Counter(r["status"] for r in rows)), builds, outdir), file=sys.stderr)
    with open(os.path.join(outdir, "selection.json"), "w") as f:
        json.dump([r["va"] for r in rows], f)
    for b in builds:                              # msvc.py picks its toolchain at import: one process per build
        env = dict(os.environ, C2_BUILD=c2_name(b))
        argv = [sys.executable, os.path.abspath(__file__), "run", a.corpus, "--c2", b, "--out", outdir, "--task", a.task,
                "--emit-timeout", str(a.emit_timeout),
                "--jobs", str(a.jobs), "--same-as-list", os.path.join(outdir, "selection.json")]
        subprocess.run(argv, env=env, check=True)
    hashes = {lifter_hash()}
    for b in builds:
        hp = os.path.join(outdir, "part-%s.hash" % b)
        if os.path.exists(hp):
            hashes |= set(open(hp).read().split())
    if len(hashes | {h_start}) > 1 or emitter_state() != (ecommit, edirty):
        print("WARNING: the lifters changed during this run (%s -> %s): its functions mix versions" %
              (ecommit, emitter_state()[0]), file=sys.stderr)
        ecommit = ecommit + "+changed"
    allr = []
    for b in builds:
        allr += [json.loads(l) for l in open(os.path.join(outdir, "part-%s.jsonl" % b))]
    allr.sort(key=lambda x: x["va"])
    with open(os.path.join(outdir, "results.jsonl"), "w") as f:
        for x in allr:
            f.write(json.dumps(x) + "\n")
    summ = summarize(allr)
    summ.update(task=a.task, corpus=corp.name, tag=tag, seed=a.seed, sample=a.sample, max_size=a.max_size, bands=a.bands,
                emitter=file_sha(os.path.join(HERE, "w8_emit.py")), emitter_commit=ecommit, emitter_dirty=edirty, when=time.strftime("%Y-%m-%d %H:%M"))
    with open(os.path.join(outdir, "summary.json"), "w") as f:
        json.dump(summ, f, indent=1)
    show(summ)


def emitter_state():
    """(commit of the last change to the lifters, True when they have uncommitted edits): a run of a half-edited emitter
    is not comparable with anything (2026-11-25: band1c measured a rule that was reverted before its commit)."""
    files = ["tools/w8_emit.py", "tools/lift2il.py", "tools/il2c_lift.py", "tools/il2c.py", "tools/w8_diff.py",
             "tools/w8_score.py"]
    head = subprocess.run(["git", "log", "-1", "--format=%h", "--"] + files, cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--"] + files, cwd=ROOT, capture_output=True,
                                text=True).stdout.strip())
    return head, dirty


LIFTER_FILES = ["w8_emit.py", "lift2il.py", "il2c_lift.py", "il2c.py", "w8_diff.py", "w8_score.py", "lift.py"]


def lifter_hash():
    """One hash over the lifter files' bytes as they are now (catches an edit that comes and goes during a run)."""
    h = hashlib.sha256()
    for f in LIFTER_FILES:
        p = os.path.join(HERE, f)
        if os.path.exists(p):
            h.update(open(p, "rb").read())
    return h.hexdigest()[:12]


def file_sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()[:12]


def biggest(rs):
    """The largest MATCH by the original's instruction count, and the largest with at least two conditional branches
    (straight-line code, a run of calls or stores, flatters a lifter)."""
    m = [r for r in rs if r.get("status") == "MATCH" and r.get("rins")]
    top = max(m, key=lambda r: r["rins"], default=None)
    top_cf = max((r for r in m if r.get("rbr", 0) >= 2), key=lambda r: r["rins"], default=None)
    f = lambda r: r and dict(va=r["va"], ins=r["rins"], branches=r["rbr"], size=r["size"])
    return dict(any=f(top), branching=f(top_cf))


def summarize(rows):
    out = {}
    keys = sorted({r["fstatus"] for r in rows}) + sorted({r["fstatus"] + "/" + band(r["size"]) for r in rows}) + ["all"]
    for st in keys:
        rs = [r for r in rows if st == "all" or r["fstatus"] == st or r["fstatus"] + "/" + band(r["size"]) == st]
        c = collections.Counter(r["status"] for r in rs)
        d = [r for r in rs if r["status"] == "DIFF"]
        med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None
        out[st] = dict(n=len(rs), **{k: c.get(k, 0) for k in ("MATCH", "DIFF", "CL-ERROR", "EMIT-ERROR", "NO-SYMBOL", "NO-SOURCE", "UNSUPPORTED", "TIMEOUT")},
                       match_pct=round(100.0 * c.get("MATCH", 0) / max(1, len(rs)), 2),
                       med_ins=med([r["ins"] for r in d]), med_bdiff=med([r["bdiff"] for r in d]),
                       short=sum(1 for r in d if r["osz"] * 2 < r["size"]),
                       same_size=sum(1 for r in d if r["osz"] == r["size"]),
                       near=sum(1 for r in d if r["bdiff"] <= 4 and r["osz"] == r["size"]),
                       cls=dict(collections.Counter(r.get("cls", "?") for r in d).most_common()),
                       biggest=biggest(rs))
    return out


def show(summ):
    print("%s %s (lifter %s, commit %s%s)" % (summ.get("corpus"), summ.get("tag"), summ.get("emitter"),
                                            summ.get("emitter_commit"), ", UNCOMMITTED EDITS" if summ.get("emitter_dirty") else ""))
    print("  %-20s %6s %6s %6s %6s %6s %7s  %6s %6s %6s %6s %6s" % (
        "status", "n", "MATCH", "%", "DIFF", "CLERR", "EMITERR/UNS", "medins", "medbd", "short", "same", "near4"))
    for st, s in summ.items():
        if not isinstance(s, dict):
            continue
        print("  %-20s %6d %6d %6.2f %6d %6d %7d  %6s %6s %6d %6d %6d" % (
            st, s["n"], s["MATCH"], s["match_pct"], s["DIFF"], s["CL-ERROR"], s["EMIT-ERROR"] + s.get("UNSUPPORTED", 0), s["med_ins"],
            s["med_bdiff"], s["short"], s["same_size"], s["near"]))
        if s.get("cls"):
            print("  %-20s   " % "" + "  ".join("%s %d" % kv for kv in s["cls"].items()))
        b = s.get("biggest") or {}
        if st == "all" and b.get("any"):
            x, y = b["any"], b.get("branching")
            print("  biggest MATCH: %s, %d instructions (%d branches, %d B)%s" % (
                x["va"], x["ins"], x["branches"], x["size"],
                "; with control flow: %s, %d instructions (%d branches)" % (y["va"], y["ins"], y["branches"]) if y else ""))


def cmd_show(a):
    show(json.load(open(os.path.join(a.run, "summary.json"))))


def cmd_compare(a):
    A = {json.loads(l)["va"]: json.loads(l) for l in open(os.path.join(a.a, "results.jsonl"))}
    B = {json.loads(l)["va"]: json.loads(l) for l in open(os.path.join(a.b, "results.jsonl"))}
    both = sorted(set(A) & set(B))
    show(dict(json.load(open(os.path.join(a.a, "summary.json")))))
    show(dict(json.load(open(os.path.join(a.b, "summary.json")))))
    gained = [v for v in both if B[v]["status"] == "MATCH" and A[v]["status"] != "MATCH"]
    lost = [v for v in both if A[v]["status"] == "MATCH" and B[v]["status"] != "MATCH"]
    trans = collections.Counter((A[v]["status"], B[v]["status"]) for v in both if A[v]["status"] != B[v]["status"])
    print("on %d common functions: +%d MATCH, -%d MATCH (regressions)" % (len(both), len(gained), len(lost)))
    for (x, y), n in trans.most_common():
        print("  %-10s -> %-10s %d" % (x, y, n))
    if lost:
        print("  lost:", " ".join(lost[:30]))
    closer = sum(1 for v in both if A[v]["status"] == B[v]["status"] == "DIFF" and B[v]["ins"] > A[v]["ins"])
    farther = sum(1 for v in both if A[v]["status"] == B[v]["status"] == "DIFF" and B[v]["ins"] < A[v]["ins"])
    print("  DIFF both: ins up %d, down %d" % (closer, farther))
    return 1 if lost else 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("corpus")
    r.add_argument("--status")
    r.add_argument("--sample", type=int, default=300, help="functions per status (0 = all)")
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--max-size", type=int)
    r.add_argument("--min-size", type=int)
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--task", choices=["lift", "src", "il2c", "lift2il"], default="lift",
                   help="lift: the lifter's unit; src: the function's own source (checks the corpus, classifies its misses)")
    r.add_argument("--emit-timeout", type=int, default=60, help="seconds one function's lifting may take (TIMEOUT)")
    r.add_argument("--bands", action="store_true", help="sample per (status, size band): <=64, 65-256, 257-1K, >1K")
    r.add_argument("--tag")
    r.add_argument("--out")
    r.add_argument("--same-as", help="a run directory: lift exactly its functions")
    r.add_argument("--vas", help="a file of VAs (whitespace separated): run exactly those rows (--status / --max-size "
                   "/ --flag narrow them, --sample N takes a random N of them, 0 = all)")
    r.add_argument("--flag", help="with --vas: keep rows whose flags contain this word (e.g. /Od)")
    r.add_argument("--same-as-list", help=argparse.SUPPRESS)
    r.add_argument("--c2", help=argparse.SUPPRESS)
    s = sub.add_parser("show")
    s.add_argument("run")
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    if sys.argv[1:2] == ["_il2c"]:                # worker: python lbench.py _il2c CORPUS VA OUTDIR
        print(json.dumps(il2c_one(*sys.argv[2:5])))
        return
    a = ap.parse_args()
    if a.cmd == "run" and a.same_as_list:
        corp = corpusmod.Corpus(a.corpus)
        byva = {x["va"]: x for x in corp.rows}
        rows = [byva[v] for v in json.load(open(a.same_as_list))]
        rows = [x for x in rows if x["c2"] == a.c2]
        h0 = lifter_hash()
        out = run_group(a, corp, rows, a.out)
        with open(os.path.join(a.out, "part-%s.jsonl" % a.c2), "w") as f:
            for x in out:
                f.write(json.dumps(x) + "\n")
        with open(os.path.join(a.out, "part-%s.hash" % a.c2), "w") as f:
            f.write("%s %s\n" % (h0, lifter_hash()))
        return
    sys.exit({"run": cmd_run, "show": cmd_show, "compare": cmd_compare}[a.cmd](a) or 0)


if __name__ == "__main__":
    main()
