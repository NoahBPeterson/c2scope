#!/usr/bin/env python3
"""Content-addressed cache for compiles and traces.

A key covers the source text, the text of every file it pulls in with `#include "..."` (recursively, resolved
against the including file's directory and the /I directories), the compiler arguments with output paths
removed, and any extra strings the caller adds (a trace config, the traced c2.dll's hash). System headers
(`#include <...>`) are not hashed: they belong to the toolchain, which is fixed.

Entries live in work/cache/<key>/ and hold the files the caller stored plus meta.json. Set CCACHE=0 to bypass,
CCACHE=refresh to recompute and overwrite. `uv run tools/ccache.py stats|clear|prune`.

Size cap: CCACHE_MAX_GB (default 20). At most once a minute (across processes) a store checks the total and, when it
is over the cap, removes the least recently used entries (a hit refreshes an entry's time) down to 80% of the cap.
Trace logs are large (up to hundreds of MB), so an uncapped cache filled the disk within a day.

A compile that died with an internal compiler error (C1001 in the stored output) is never stored, and an entry
holding one is a miss: a traced c2 can crash for reasons the key does not cover (the compile server's copy of the
tracer, a transient failure there). One such crash at 09:24 on 2026-11-19 was replayed by every later run of
icount_tests on 13.10 (`stmt` probe) until the entry was found; the same compile without the cache never crashed.
"""
import hashlib, json, os, re, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from image import ROOT

DIR = os.path.join(ROOT, "work", "cache")
INC = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.M)
OUTPUT_OPTS = ("/Fo", "/Fa", "/Fe", "/Fd", "/Fp", "/FR", "/Fr")      # case-sensitive: /fp:strict is codegen
_file_hash = {}


def mode():
    return os.environ.get("CCACHE", "1")


def file_sha(path):
    st = os.stat(path)
    k = (path, st.st_mtime_ns, st.st_size)
    h = _file_hash.get(k)
    if h is None:
        h = _file_hash[k] = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return h


def include_closure(src, incdirs):
    seen, todo, out = set(), [os.path.abspath(src)], []
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        try:
            text = open(p, errors="replace").read()
        except OSError:
            continue
        for name in INC.findall(text):
            for d in [os.path.dirname(p)] + incdirs:
                q = os.path.abspath(os.path.join(d, name))
                if os.path.isfile(q):
                    todo.append(q)
                    break
    return sorted(out)


def key(src, args, extra=()):
    incdirs = [a[2:] for a in args if a[:2].lower() == "/i" and len(a) > 2]
    incdirs.append(os.path.join(ROOT, "src", "include"))
    h = hashlib.sha256()
    h.update(b"v1\0")
    srcabs = os.path.abspath(src)
    for p in include_closure(src, incdirs):
        rel = "<src>" if p == srcabs else p
        h.update(("%s\0%s\0" % (rel, file_sha(p))).encode())
    for a in args:
        if a.replace("-", "/", 1).startswith(OUTPUT_OPTS) or os.path.abspath(a) == srcabs:
            a = a[:3].lower()
        h.update((a + "\0").encode())
    for x in extra:
        h.update(b"\1" + (x if isinstance(x, bytes) else str(x).encode()))
    return h.hexdigest()[:40]


def get(k):
    """The entry directory if k is cached (and CCACHE allows reading), else None."""
    if mode() in ("0", "refresh"):
        return None
    d = os.path.join(DIR, k)
    if not os.path.exists(os.path.join(d, "meta.json")):
        return None
    try:
        if _ice(meta(d)):
            return None
    except (OSError, ValueError):
        return None
    try:
        os.utime(d)                   # least recently used order for prune()
    except OSError:
        pass
    return d


def meta(d):
    return json.load(open(os.path.join(d, "meta.json")))


def _ice(info):
    """True when info's compiler output reports an internal compiler error (not a function of the key)."""
    out = info.get("out") if isinstance(info, dict) else None
    return isinstance(out, str) and ("C1001" in out or "INTERNAL COMPILER ERROR" in out)


def put(k, files, info):
    """Store files ({name: path or None}) and info (JSON-able) under k. Returns the entry directory, or None when
    nothing was stored (CCACHE=0, or the compile died with an internal compiler error)."""
    if mode() == "0" or _ice(info):
        return None
    d = os.path.join(DIR, k)
    tmp = d + ".tmp%d" % os.getpid()
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for name, path in files.items():
        if path and os.path.exists(path):
            shutil.copy(path, os.path.join(tmp, name))
    json.dump(info, open(os.path.join(tmp, "meta.json"), "w"))
    shutil.rmtree(d, ignore_errors=True)
    try:
        os.rename(tmp, d)
    except OSError:                   # another process stored the same key first
        shutil.rmtree(tmp, ignore_errors=True)
    maybe_prune()
    return d


def max_bytes():
    try:
        return float(os.environ.get("CCACHE_MAX_GB", "20")) * 1e9
    except ValueError:
        return 20e9


def prune(limit=None):
    """Remove least recently used entries until the cache is at most 80% of `limit` (default the cap); returns
    (bytes before, bytes after)."""
    limit = max_bytes() if limit is None else limit
    entries = []
    for name in os.listdir(DIR) if os.path.isdir(DIR) else []:
        d = os.path.join(DIR, name)
        try:
            size = sum(e.stat().st_size for e in os.scandir(d) if e.is_file())
            entries.append((os.stat(d).st_mtime, size, d))
        except OSError:
            continue
    total = before = sum(e[1] for e in entries)
    if total > limit:
        for _t, size, d in sorted(entries):
            if total <= 0.8 * limit:
                break
            shutil.rmtree(d, ignore_errors=True)
            total -= size
    return before, total


def maybe_prune():
    """prune() at most once a minute across processes (the mtime of work/cache/.prune)."""
    import time
    mark = os.path.join(DIR, ".prune")
    try:
        if time.time() - os.path.getmtime(mark) < 60:
            return
    except OSError:
        pass
    try:
        open(mark, "a").close()
        os.utime(mark)
        prune()
    except OSError:
        pass


def main():
    cmd = sys.argv[1:2] or ["stats"]
    if cmd == ["prune"]:
        b, a = prune()
        print("%.1f GB -> %.1f GB (cap %.0f GB)" % (b / 1e9, a / 1e9, max_bytes() / 1e9))
        return
    if cmd == ["clear"]:
        shutil.rmtree(DIR, ignore_errors=True)
        print("cleared", DIR)
        return
    n = size = 0
    for root, _, files in os.walk(DIR):
        n += any(f == "meta.json" for f in files)
        size += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    print("%d entries, %.1f MB in %s" % (n, size / 1e6, DIR))


if __name__ == "__main__":
    main()
