#!/usr/bin/env python3
"""A snapshot of a unit's include closure, so that a long run (rwsweep, effectmine) compiles every variant against
the headers as they were when it started, not the live tree's (Mount & Blade: stream.h edited during a 22-minute
rwsweep made every later variant fail to compile).

  snap = incsnap.Snapshot(tu_path, cl, dest, text=None)
  open(variant, "w").write(snap.text(variant_text))      # `#include "/abs/live/path"` -> the snapshot's copy
  c2trace.run_trace(variant, hooks, snap.cl, ...)         # /I dirs (and /FI files) -> the snapshot's copies

The closure is found the way the remote compile client finds what to ship (tools/remote/c2s_client.py scan): from
the unit (its text as the tool will compile it), every `#include` / `#import` line whatever the conditionals
(a superset), a quoted name next to the includer and then its includers, then the /I dirs in order, then msvc.py's
project INCLUDE dir (src/include); a name found in none of them is the toolchain's (work/toolchain, left alone). The
files keep their layout relative to the common directory of everything copied, so a `../x.h` between them still
resolves. Every /I dir is given as its snapshot copy, in the same order; when headers came from src/include an /I of
its copy goes last (cl searches /I before INCLUDE, so the order of the search is unchanged). An include the scan
cannot see (a macro-computed name) is not in the snapshot; such a header would then come from the toolchain or fail.
"""
import os, re, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INC = re.compile(r'^[ \t]*#[ \t]*(?:include|import)[ \t]*([<"])([^>"\r\n]+)[>"]', re.M)
ABSINC = re.compile(r'^([ \t]*#[ \t]*(?:include|import)[ \t]*")(/[^"\r\n]+)(")', re.M)
TOOLDIRS = (os.path.join(ROOT, "work", "toolchain") + os.sep,)
SYSDIRS = [os.path.join(ROOT, "src", "include")]


def _idir(a):
    return os.path.normpath(os.path.abspath(a[2:])) if a[:2] in ("/I", "-I") and len(a) > 2 else None


def _tool(p):
    return p.startswith(TOOLDIRS) or p + os.sep in TOOLDIRS


def closure(tu, text, idirs):
    """[absolute paths] of every file the unit can include outside the toolchain (the unit itself first)."""
    found, seen = [], set()
    todo = [(os.path.normpath(os.path.abspath(tu)), [], text)]
    dirs_all = [d for d in idirs if not _tool(d)] + SYSDIRS
    while todo:
        f, chain, body = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        found.append(f)
        if body is None:
            try:
                body = open(f, "rb").read().decode("latin-1")
            except OSError:
                continue
        here = chain + [os.path.dirname(f)]
        for kind, name in INC.findall(body):
            name = name.strip().replace("\\", "/")
            if os.path.isabs(name):
                cands = [os.path.normpath(name)]
            else:
                cands = [os.path.normpath(os.path.join(d, name))
                         for d in (list(reversed(here)) if kind == '"' else []) + dirs_all]
            for c in cands:
                if os.path.isfile(c):
                    if not _tool(c):
                        todo.append((c, here, None))
                    break
    return found


class Snapshot:
    def __init__(self, tu, cl, dest, text=None):
        tu = os.path.normpath(os.path.abspath(tu))
        if text is None:
            text = open(tu, "rb").read().decode("latin-1")
        idirs = [d for d in (_idir(a) for a in cl) if d is not None]
        fis = [a[3:] for a in cl if a[:3] == "/FI" and len(a) > 3]
        files = closure(tu, text, idirs)
        for fi in fis:                                  # a forced include: searched like <name>
            p = fi if os.path.isabs(fi) else next((os.path.join(d, fi) for d in idirs + SYSDIRS
                                                   if os.path.isfile(os.path.join(d, fi))), None)
            if p and os.path.isfile(p) and not _tool(p):
                files += [x for x in closure(p, None, idirs) if x not in files]
        files = [f for f in files if f != tu]
        used_sys = [d for d in SYSDIRS if any(f.startswith(d + os.sep) for f in files)]
        keep = [d for d in idirs if not _tool(d)] + used_sys
        self.root = os.path.commonpath([os.path.dirname(tu)] + [os.path.dirname(f) for f in files] + keep) \
            if files or keep else os.path.dirname(tu)
        self.dest = os.path.abspath(dest)
        self.map = {}
        for f in files:
            to = self.path(f)
            os.makedirs(os.path.dirname(to), exist_ok=True)
            shutil.copy2(f, to)
            self.map[f] = to
        for d in keep:
            os.makedirs(self.path(d), exist_ok=True)
        out = []
        for a in cl:
            d = _idir(a)
            if d is not None and not _tool(d):
                out.append(a[:2] + self.path(d))
            elif a[:3] == "/FI" and len(a) > 3 and os.path.isabs(a[3:]) and os.path.normpath(a[3:]) in self.map:
                out.append("/FI" + self.map[os.path.normpath(a[3:])])
            else:
                out.append(a)
        self.cl = out + ["/I" + self.path(d) for d in used_sys if d not in idirs]
        self.files = files

    def path(self, p):
        """The snapshot's copy of a live path under the snapshot's root (the path itself outside it)."""
        p = os.path.normpath(os.path.abspath(p))
        if p != self.root and not p.startswith(self.root + os.sep):
            return p
        return os.path.join(self.dest, os.path.relpath(p, self.root))

    def text(self, t):
        """A unit's text with each `#include "/abs/path"` of a snapshotted file pointed at its copy."""
        def sub(m):
            to = self.map.get(os.path.normpath(m.group(2)))
            return m.group(0) if to is None else m.group(1) + to + m.group(3)
        return ABSINC.sub(sub, t)

    def summary(self):
        return "%d headers snapshotted under %s" % (len(self.files), self.dest)
