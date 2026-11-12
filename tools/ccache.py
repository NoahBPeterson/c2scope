#!/usr/bin/env python3
"""Content-addressed cache for compiles and traces.

A key covers the source text, the text of every file it pulls in with `#include "..."` (recursively, resolved
against the including file's directory and the /I directories), the compiler arguments with output paths
removed, and any extra strings the caller adds (a trace config, the traced c2.dll's hash). System headers
(`#include <...>`) are not hashed: they belong to the toolchain, which is fixed.

Entries live in work/cache/<key>/ and hold the files the caller stored plus meta.json. Set CCACHE=0 to bypass,
CCACHE=refresh to recompute and overwrite. `uv run tools/ccache.py stats|clear`.
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
    return d if os.path.exists(os.path.join(d, "meta.json")) else None


def meta(d):
    return json.load(open(os.path.join(d, "meta.json")))


def put(k, files, info):
    """Store files ({name: path or None}) and info (JSON-able) under k. Returns the entry directory."""
    if mode() == "0":
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
    return d


def main():
    cmd = sys.argv[1:2] or ["stats"]
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
