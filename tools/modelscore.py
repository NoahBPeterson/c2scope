#!/usr/bin/env python3
"""Score the models on functions you have already matched: register allocation (rasim --predict) and frame slots
(c2trace.py slots), per function and in total.  For x86 builds (13.00.9044, 13.10, 14.00, 15.00).

usage: modelscore.py LIST [--c2 BUILD] [--out DIR] [--only regs|slots]

LIST is a text file, one function per line:   SRC  FUNC  [cl flags ...]
  SRC    the matched source (a unit that compiles as is)
  FUNC   the --func filter (decorated or plain name, as the tools take it)
  flags  the compile flags (default /O2); `#` starts a comment
Every compile runs where you are (outputs go to --out, default ./modelscore.out); nothing is sent anywhere else
unless the tracer's own remote compile is configured.

Per function: `regs` = rasim check --predict decisions predicted / checked (the "all" row: list head, branch,
spill/defer, register, no-reg, cut set, split round, SpillObvious, insertion order); colourings = `rasim check`'s
int colourings predicted from the simulated state (it also counts split pieces, which --predict replays); `slots` = the frame-packing model against the trace (exact or not) and the elements packed. A function
whose trace fails is listed with the reason. The total line is the share of decisions and of exact frames.
"""
import os, re, shlex, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RASIM = os.path.join(ROOT, "tools", "rasim", "target", "release", "rasim") + (".exe" if os.name == "nt" else "")


def run(cmd, env):
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=ROOT)
    return r.returncode, r.stdout + r.stderr


def score_regs(src, func, flags, out, env):
    snap = os.path.join(out, re.sub(r"[^\w]+", "_", func)[:60] + ".regs.json")
    rc, txt = run([sys.executable, os.path.join(ROOT, "tools", "c2trace.py"), "regs", src, "--func", func,
                   "--export", snap, "--"] + flags, env)
    if rc or not os.path.exists(snap):
        return None, "trace failed: " + (txt.strip().splitlines() or ["?"])[-1][:120]
    rc, txt = run([RASIM, "check", snap, "--predict"], env)
    ok = tot = 0
    for line in txt.splitlines():
        if line.strip().startswith("all "):
            for a, b in re.findall(r"(\d+)/(\d+)", line):
                ok, tot = ok + int(a), tot + int(b)
    _, plain = run([RASIM, "check", snap], env)
    m = re.search(r"registers: (\d+) of (\d+)", plain)
    regs = "%s/%s" % m.groups() if m else "-"
    return (ok, tot), "decisions %d/%d, colourings (plain check) %s" % (ok, tot, regs)


def score_slots(src, func, flags, env):
    rc, txt = run([sys.executable, os.path.join(ROOT, "tools", "c2trace.py"), "slots", src, "--func", func,
                   "--quiet", "--"] + flags, env)
    m = re.search(r"# \S+: (\d+) elements.*model: (\w+)", txt)
    if not m:
        if re.search(r"no stack elements|0 elements", txt):
            return None, "no packed frame"
        return None, "trace failed: " + (txt.strip().splitlines() or ["?"])[-1][:120]
    return (m.group(2) == "exact", 1), "%s elements, model %s" % (m.group(1), m.group(2))


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="modelscore.py", usage=__doc__)
    ap.add_argument("list")
    ap.add_argument("--c2")
    ap.add_argument("--out", default="modelscore.out")
    ap.add_argument("--only", choices=("regs", "slots"))
    a = ap.parse_args(argv)
    env = dict(os.environ)
    if a.c2:
        env["C2_BUILD"] = a.c2
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    rows = []
    for ln in open(a.list):
        ln = ln.split("#", 1)[0].strip()
        if not ln:
            continue
        p = shlex.split(ln)
        if len(p) < 2:
            continue
        src, func, flags = os.path.abspath(p[0]), p[1], p[2:] or ["/O2"]
        rows.append((src, func, flags))
    dec = [0, 0]
    fr = [0, 0]
    for src, func, flags in rows:
        cells = []
        if a.only != "slots":
            r, t = score_regs(src, func, flags, out, env)
            if r:
                dec[0] += r[0]
                dec[1] += r[1]
            cells.append("regs: " + t)
        if a.only != "regs":
            r, t = score_slots(src, func, flags, env)
            if r:
                fr[0] += r[0]
                fr[1] += r[1]
            cells.append("slots: " + t)
        print("%-40s %s" % (func[:40], " | ".join(cells)), flush=True)
    pct = lambda x: "%.1f%%" % (100.0 * x[0] / x[1]) if x[1] else "-"
    print("total: %d functions; allocator decisions %d/%d (%s); frames exact %d/%d (%s)" % (
        len(rows), dec[0], dec[1], pct(dec), fr[0], fr[1], pct(fr)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
