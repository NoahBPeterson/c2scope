#!/usr/bin/env python3
"""Regression tests for the tracer, the reports and rasim. usage: uv run python tests/run_tests.py [-v]

Each test compiles tests/data/*.cpp with the traced c2 (game flags /TP /MT /GS /EHsc /Gy /O2) and compares a
number the tools report with the value measured when the tools were validated. A failure means the setup is
wrong (another c2.dll, a wine problem) or a tool regressed.
"""
import json, os, re, subprocess, sys, tempfile
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "tests", "data", "eh_helpers.cpp")
FLAGS = ["--", "/TP", "/MT", "/GS", "/EHsc", "/Gy"]
VERBOSE = "-v" in sys.argv
results = []


def tool(*args):
    r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "c2trace.py")] + list(args),
                       capture_output=True, text=True, cwd=HERE)
    if VERBOSE:
        print(r.stdout[-3000:], r.stderr[-2000:])
    return r.stdout + r.stderr


def check(name, got, want):
    good = got == want
    results.append(good)
    print("  %-58s %s%s" % (name, "ok" if good else "FAIL", "" if good else "  (got %r, want %r)" % (got, want)), flush=True)


def globregs(out, func):
    m = re.search(r"^\?%s@@\S*\s+\((\d+) globregs" % func, out, re.M)
    return int(m.group(1)) if m else None


print("regression tests (each line is one traced compile or one rasim run)")
out = tool("regs", DATA, *FLAGS)
check("regs: EH function, plain helper: 6 values", globregs(out, "eA"), 6)
check("regs: EH function, reference-binding helper: 7 values", globregs(out, "eB"), 7)
check("regs: no EH frame, plain helper: 4 values", globregs(out, "nA"), 4)
check("regs: no EH frame, reference-binding helper: 4 values", globregs(out, "nB"), 4)

out = tool("scratch", DATA, "--func", "?eA@@", *FLAGS)
seq = " ".join(m.group(2) + ("" if m.group(1) == "ROT" else "*")
               for m in re.finditer(r"^\s+\d+\s+\S+\s+\S+\s+(ROT|pref|fit)\s+(\S+)", out, re.M))
check("scratch: eA picks (rotation order, * = copy hint/first fit)", seq, "ecx edx eax* ecx edx eax ecx")
out = tool("scratch", DATA, "--func", "?eB@@", *FLAGS)
seq = " ".join(m.group(2) + ("" if m.group(1) == "ROT" else "*")
               for m in re.finditer(r"^\s+\d+\s+\S+\s+\S+\s+(ROT|pref|fit)\s+(\S+)", out, re.M))
check("scratch: eB picks (the binding is pick-neutral)", seq, "ecx edx ecx edx eax ecx")

snap = os.path.join(tempfile.mkdtemp(), "eA.json")
tool("regs", DATA, "--func", "?eA@@", "--export", snap, *FLAGS)
rasim = os.path.join(HERE, "tools", "rasim", "target", "release", "rasim")
r = subprocess.run([rasim, "check", snap], capture_output=True, text=True).stdout
m = re.search(r"(\d+) mismatches; colouring order: (\d+) of (\d+)", r)
check("rasim check: 0 mismatches", int(m.group(1)) if m else None, 0)
check("rasim check: whole colouring order predicted", (m.group(2) == m.group(3)) if m else None, True)

out = tool("il", DATA, "--func", "?eA@@", "--at", "@GrgIdentifyCandidates@4", "--count", "OPCALL", *FLAGS)
m = re.search(r"@GrgIdentifyCandidates@4\s+(\d+)", out)
check("il: 7 calls (3 use, take, InterlockedIncrement, D ctor and dtor)", int(m.group(1)) if m else None, 7)

print("%d of %d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
