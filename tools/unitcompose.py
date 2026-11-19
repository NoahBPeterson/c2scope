#!/usr/bin/env python3
"""Composed translation units: trace a candidate inside the unit a checker really compiles.  Doc: docs/c2/tracer.md
("Composed units")

A candidate function is often checked inside a larger translation unit: a header hoisted first, the project's
unit headers, a pragma, then the candidate with some of its own lines removed. A spec describes that unit; every
tool built on c2trace.py then takes `--unit SPEC` (or an adapter, `--f3pack DIR`) and compiles the composed unit
where it would have compiled SRC. SRC (the tool's source argument) is the candidate.

usage: unitcompose.py compose SPEC [CANDIDATE] [-o OUT]   write the composed unit (default: print it)
       unitcompose.py line OUT N..                         composed line N -> candidate (or header) line
       unitcompose.py extract OUT [VARIANT]                the candidate's part of OUT (or of an edited copy of it)
SPEC is a JSON file or `f3pack:DIR` (the f3decomp pack adapter, below).

Spec (JSON; relative paths are taken against the spec's directory):
  {"parts": [PART, ..], "flags": ["/MT", ..], "func": "SYMBOL", "name": "STEM"}
  PART is one of
    {"text": "LINE\\nLINE"}                 literal lines
    {"include": "PATH"}                     an `#include "/absolute/PATH"` line (the file ships to a compile server
                                            like any header the unit includes)
    {"inline": "PATH", "drop": [RE..]}      the file's text, without the lines a `drop` regex finds
    {"candidate": true | "PATH", "drop": [RE..], "hoist": [RE..]}
                                            the candidate: SRC on a tool's command line (or PATH); lines found by a
                                            `hoist` regex are moved to the {"hoisted": true} part (or to the top
                                            when there is none), other lines found by a `drop` regex are removed
    {"hoisted": true}                       where hoisted candidate lines go
  `drop` and `hoist` are Python regular expressions searched in each line (without its newline).
  flags: cl flags put before the ones given after `--` (so a later flag of yours wins); func: the function's symbol
  (any --func form): `--func` for the tool when none is given, and C2TRACE_FUNC unless it is in the environment
  (`C2TRACE_FUNC=` empty: no filter) or --func is given.

On any tool (taken out of sys.argv when c2trace is imported, like --c2):
  --unit SPEC | --unit f3pack:DIR | --f3pack DIR    compose; the tool runs on the composed unit
  --unit-out PATH                                    where to write it (default: the --keep directory, else next
                                                     to --export, else work/units/<hash>/<stem>.unit.cpp)
The composed unit is written with a line map (<out minus .cpp>.json). Reports that print absolute source lines
(slots, scratch --phase, icount) print candidate lines; a line outside the candidate prints as `tu:N`.

f3pack:DIR builds the spec from an f3decomp pack (pack.json: headers, flags, symbol, source):
  #include <windows.h>; the candidate's other `#include <..>` / `#pragma intrinsic|function` lines (hoisted);
  #include "DIR/<header>" for each pack.json header in order; the unit source's own text, when it has any besides
  those header includes (inlined); #pragma optimize("", on); the candidate without its `#include <windows.h>` and
  without any other preprocessor line (as the checker does).
"""
import hashlib, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_EXT = (".c", ".cpp", ".cc", ".cxx", ".cp")

# the f3pack adapter's filters for the candidate
F3_HOIST = [r"^[ \t]*#[ \t]*include[ \t]*<(?!windows\.h>)[^>]+>[ \t]*$", r"^[ \t]*#[ \t]*pragma[ \t]+(?:intrinsic|function)[ \t]*\("]
F3_DROP = [r"^[ \t]*(?:#|%:)"]          # windows.h (first anyway) and any directive that is not hoisted


def f3pack_spec(d):
    """The spec for an f3decomp pack directory."""
    d = os.path.abspath(d)
    try:
        pk = json.load(open(os.path.join(d, "pack.json")))
    except (OSError, ValueError) as e:
        sys.exit("f3pack %s: no readable pack.json (%s)" % (d, e))
    heads = list(pk.get("headers") or [])
    parts = [{"text": "#include <windows.h>"}, {"hoisted": True}]
    parts += [{"include": os.path.join(d, h)} for h in heads]
    src = pk.get("source")
    if src and os.path.isfile(os.path.join(d, src)):
        own = [r'^[ \t]*#[ \t]*include[ \t]*"%s"' % re.escape(h) for h in heads]
        text = open(os.path.join(d, src), errors="replace").read()
        rest = [ln for ln in text.splitlines() if not any(re.search(p, ln) for p in own)]
        if any(ln.strip() for ln in rest):
            parts.append({"inline": os.path.join(d, src), "drop": own})
        if "@fnv" in text:
            sys.stderr.write("unitcompose: %s has annotated definitions (@fnv): inlined as they are\n" % src)
    parts += [{"text": '#pragma optimize("", on)'}, {"candidate": True, "drop": F3_DROP, "hoist": F3_HOIST}]
    spec = {"parts": parts, "flags": list(pk.get("flags") or []), "func": pk.get("symbol") or None,
            "origin": "f3pack:" + d}
    return spec


def load_spec(s):
    """A spec from `f3pack:DIR` or a JSON file; relative part paths resolved against the file's directory."""
    if s.startswith("f3pack:"):
        return f3pack_spec(s[7:])
    if os.path.isdir(s) and os.path.isfile(os.path.join(s, "pack.json")):
        return f3pack_spec(s)
    try:
        spec = json.load(open(s))
    except (OSError, ValueError) as e:
        sys.exit("unit spec %s: %s" % (s, e))
    base = os.path.dirname(os.path.abspath(s))
    for p in spec.get("parts", []):
        for k in ("include", "inline"):
            if k in p:
                p[k] = os.path.join(base, p[k])
        if isinstance(p.get("candidate"), str):
            p["candidate"] = os.path.join(base, p["candidate"])
    spec["origin"] = os.path.abspath(s)
    return spec


def _lines(path):
    return open(path, errors="surrogateescape", newline="").read().replace("\r\n", "\n").split("\n")


def _strip_last(ls):
    return ls[:-1] if ls and ls[-1] == "" else ls          # a final newline is not a line


def compose(spec, cand=None):
    """(text, map). map["parts"]: per part {kind, file, first (1-based line in the text), lines (the source line of
    each of its lines; [] for literal lines)}."""
    parts = spec.get("parts") or []
    cps = [p for p in parts if "candidate" in p]
    if len(cps) != 1:
        sys.exit("unit spec: exactly one candidate part is needed (%d)" % len(cps))
    cp = cps[0]
    cand = cand or (cp["candidate"] if isinstance(cp["candidate"], str) else None)
    if not cand:
        sys.exit("unit spec: no candidate (give SRC, or a path in the candidate part)")
    cand = os.path.abspath(cand)
    pat = lambda key, p=cp: [re.compile(r) for r in p.get(key) or []]
    drop, hoist = pat("drop"), pat("hoist")
    body, hoisted, dropped = [], [], []
    for n, ln in enumerate(_strip_last(_lines(cand)), 1):
        if any(r.search(ln) for r in hoist):
            hoisted.append((n, ln))
        elif any(r.search(ln) for r in drop):
            dropped.append((n, ln))
        else:
            body.append((n, ln))
    out, pmap = [], []

    def emit(kind, f, rows):
        pmap.append(dict(kind=kind, file=f, first=len(out) + 1, n=len(rows), lines=[n for n, _ in rows if n is not None]))
        out.extend(ln for _, ln in rows)
    if hoisted and not any(p.get("hoisted") for p in parts):
        emit("hoisted", cand, hoisted)
    for p in parts:
        if "text" in p:
            emit("text", None, [(None, ln) for ln in p["text"].split("\n")])
        elif "include" in p:
            emit("include", p["include"], [(None, '#include "%s"' % os.path.abspath(p["include"]))])
        elif "inline" in p:
            dr = [re.compile(r) for r in p.get("drop") or []]
            emit("inline", p["inline"], [(n, ln) for n, ln in enumerate(_strip_last(_lines(p["inline"])), 1)
                                         if not any(r.search(ln) for r in dr)])
        elif p.get("hoisted"):
            emit("hoisted", cand, hoisted)
        elif "candidate" in p:
            emit("candidate", cand, body)
        else:
            sys.exit("unit spec: unknown part %r" % (p,))
    m = dict(candidate=cand, origin=spec.get("origin"), flags=spec.get("flags") or [], func=spec.get("func"),
             parts=pmap, dropped=dropped)
    return "\n".join(out) + "\n", m


def default_out(spec, cand, argv=()):
    """Where the composed unit goes: --keep DIR, else next to --export FILE, else work/units/<hash>/."""
    stem = spec.get("name") or os.path.splitext(os.path.basename(cand))[0]
    for i, a in enumerate(argv):
        if a == "--keep" and i + 1 < len(argv):
            return os.path.join(os.path.abspath(argv[i + 1]), stem + ".unit.cpp")
        if a.startswith("--keep="):
            return os.path.join(os.path.abspath(a[7:]), stem + ".unit.cpp")
    for i, a in enumerate(argv):
        if a == "--export" and i + 1 < len(argv):
            return os.path.join(os.path.dirname(os.path.abspath(argv[i + 1])), stem + ".unit.cpp")
    h = hashlib.sha1(("%s\0%s" % (os.path.abspath(cand), spec.get("origin"))).encode()).hexdigest()[:10]
    return os.path.join(ROOT, "work", "units", h, stem + ".unit.cpp")


def map_path(out):
    return os.path.splitext(out)[0] + ".json"


def write(spec, cand, out):
    """Compose and write out (atomically: parallel tools may compose the same unit) and its line map."""
    text, m = compose(spec, cand)
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    for path, data in ((out, text), (map_path(out), json.dumps(m, indent=1) + "\n")):
        tmp = "%s.tmp%d" % (path, os.getpid())
        with open(tmp, "w", errors="surrogateescape", newline="") as f:
            f.write(data)
        os.replace(tmp, path)
    return m


# ---------------------------------------------------------------- line numbers
ACTIVE = None          # the map of the unit this process composed from --unit (None: line numbers pass unchanged)


def load_map(out):
    try:
        return json.load(open(map_path(out)))
    except (OSError, ValueError):
        return None


def where(n, m=None):
    """Composed line n -> (kind, file, line in that file or None)."""
    m = m or ACTIVE
    if not m or n is None:
        return None
    for p in m["parts"]:
        k = n - p["first"]
        if 0 <= k < p["n"]:
            return (p["kind"], p["file"], p["lines"][k] if p["lines"] else None)
    return ("tu", None, None)


def cand_line(n, m=None):
    """The candidate's line for composed line n (None outside the candidate; n itself with no unit)."""
    m = m or ACTIVE
    if not m:
        return n
    w = where(n, m)
    return w[2] if w and w[0] in ("candidate", "hoisted") else None


def unit_line(c, m=None):
    """Candidate line c -> composed line (None if it was dropped)."""
    m = m or ACTIVE
    if not m:
        return c
    for p in m["parts"]:
        if p["kind"] in ("candidate", "hoisted") and c in p["lines"]:
            return p["first"] + p["lines"].index(c)
    return None


def label(n, m=None):
    """How a report prints composed line n: the candidate's line number, `name.h:L` for an inlined file's line,
    `tu:N` for anything else in the unit; str(n) when no unit is active."""
    m = m or ACTIVE
    if not m or n is None:
        return str(n)
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    w = where(n, m)
    if w and w[0] in ("candidate", "hoisted"):
        return str(w[2])
    if w and w[0] == "inline":
        return "%s:%d" % (os.path.basename(w[1]), w[2])
    return "tu:%d" % n


def offsets(m=None):
    """{'candidate first line in the unit': N, 'delta': unit line - candidate line for the candidate's first line}."""
    m = m or ACTIVE
    for p in (m or {}).get("parts", []):
        if p["kind"] == "candidate" and p["lines"]:
            return dict(first=p["first"], delta=p["first"] - p["lines"][0])
    return None


# ---------------------------------------------------------------- command lines of the tools
VALUE_OPTS_WITH_SOURCES = {"--with"}      # options whose value is a source file that is not the candidate


def take_argv(argv=None, quiet=False):
    """Take --unit / --f3pack / --unit-out (before any `--`) out of argv (default sys.argv, changed in place); compose
    the unit; put its path where the candidate (the first source file argument) was; add the spec's flags after
    `--` (before the user's), `--func SYMBOL` when no --func is given, and C2TRACE_FUNC. Returns the map or None."""
    global ACTIVE
    a = sys.argv if argv is None else argv
    end = a.index("--") if "--" in a else len(a)
    spec_s, out, k = None, None, 1
    while k < end:
        x = a[k]
        for opt in ("--unit", "--f3pack", "--unit-out"):
            if x == opt and k + 1 < end:
                val = a[k + 1]
                del a[k:k + 2]
                end -= 2
                break
            if x.startswith(opt + "="):
                val = x[len(opt) + 1:]
                del a[k]
                end -= 1
                break
        else:
            k += 1
            continue
        if opt == "--unit-out":
            out = val
        elif spec_s is not None:
            sys.exit("unitcompose: only one --unit / --f3pack")
        else:
            spec_s = ("f3pack:" + val) if opt == "--f3pack" else val
    if spec_s is None:
        if out:
            sys.exit("unitcompose: --unit-out needs --unit or --f3pack")
        return None
    spec = load_spec(spec_s)
    ci = None
    for i in range(1, end):
        if not a[i].startswith("-") and a[i].lower().endswith(SRC_EXT) and os.path.isfile(a[i]) and \
                a[i - 1] not in VALUE_OPTS_WITH_SOURCES:
            ci = i
            break
    cps = [p for p in spec.get("parts", []) if "candidate" in p]
    cand = a[ci] if ci is not None else (cps[0]["candidate"] if cps and isinstance(cps[0]["candidate"], str) else None)
    if cand is None:
        sys.exit("unitcompose: no candidate: give the candidate source as the tool's SRC argument")
    if ci is None:
        sys.exit("unitcompose: the spec names the candidate %s; give it as the tool's SRC argument too" % cand)
    out = os.path.abspath(out) if out else default_out(spec, cand, a[1:end])
    m = write(spec, cand, out)
    a[ci] = out
    has_func = any(x == "--func" or x.startswith("--func=") for x in a[1:end])
    if spec.get("func") and not has_func:
        a[ci + 1:ci + 1] = ["--func", spec["func"]]
        end += 2
    if spec.get("func") and not has_func and "C2TRACE_FUNC" not in os.environ:
        os.environ["C2TRACE_FUNC"] = spec["func"]
    if spec.get("flags"):
        if end < len(a):
            a[end + 1:end + 1] = list(spec["flags"])
        else:
            a.extend(["--"] + list(spec["flags"]))
    ACTIVE = m
    if not quiet:
        o = offsets(m)
        sys.stderr.write("unitcompose: %s -> %s (candidate line L is unit line L%+d%s%s%s)\n" % (
            os.path.basename(cand), out, o["delta"] if o else 0,
            "; %d line(s) dropped" % len(m["dropped"]) if m["dropped"] else "",
            "; flags %s" % " ".join(spec["flags"]) if spec.get("flags") else "",
            "; func %s" % spec["func"] if spec.get("func") else ""))
    return m


def extract(out, variant=None):
    """The candidate's part of the composed unit `out` (or of `variant`, an edited copy written by a sweep): the
    variant's text after the unit's prefix, which must be unchanged."""
    m = load_map(out)
    if not m:
        sys.exit("%s: no line map (%s)" % (out, map_path(out)))
    cp = next(p for p in m["parts"] if p["kind"] == "candidate")
    ls = open(variant or out, errors="surrogateescape").read().split("\n")
    pre = open(out, errors="surrogateescape").read().split("\n")[:cp["first"] - 1]
    if ls[:len(pre)] != pre:
        sys.exit("%s: the lines before the candidate differ from %s" % (variant, out))
    tail = len([p for p in m["parts"] if p["first"] > cp["first"]])
    if tail:
        sys.exit("%s: parts after the candidate: extract only handles a candidate at the end" % out)
    return "\n".join([ln for _, ln in m["dropped"]] + ls[len(pre):])


def main(argv):
    if not argv or argv[0] in ("-h", "--help") or argv[0] not in ("compose", "line", "extract"):
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    if cmd == "compose":
        o = None
        if "-o" in rest:
            k = rest.index("-o")
            o = rest[k + 1]
            del rest[k:k + 2]
        if not rest:
            sys.exit(__doc__)
        spec = load_spec(rest[0])
        cand = rest[1] if len(rest) > 1 else None
        if o:
            m = write(spec, cand, o)
            off = offsets(m)
            print("%s (candidate line L = unit line L%+d)" % (os.path.abspath(o), off["delta"] if off else 0))
        else:
            sys.stdout.write(compose(spec, cand)[0])
    elif cmd == "line":
        m = load_map(rest[0])
        if not m:
            sys.exit("%s: no line map" % rest[0])
        for n in rest[1:]:
            w = where(int(n), m)
            print("%s: %s" % (n, "%s %s line %s" % (w[0], w[1], w[2]) if w and w[1] and w[2] else w[0] if w else "?"))
    else:
        sys.stdout.write(extract(rest[0], rest[1] if len(rest) > 1 else None))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
