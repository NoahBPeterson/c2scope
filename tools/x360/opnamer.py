"""Name the Xbox 360 c2's machine opcodes: the IL at EncEmitFunc (one tuple per emitted instruction) against the
/FAsc listing of the same compile, function by function; votes opcode -> mnemonic.
usage: python opnamer.py SRC.cpp... > votes.json      (C2_BUILD=xbox360, compiles locally)"""
import collections, json, os, re, sys, tempfile
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
os.environ.setdefault("C2_BUILD", "15.00.8153-xbox360")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import c2trace

SPEC = "@EncEmitFunc@4=g10c38108>0>0>4$,g10c38108>0>8>0>0x1c%"
LST = re.compile(r"^\s+([0-9a-f]{5})\t([0-9a-f]{8})\t\s*(\S+)(.*)$")


def listing(path):
    out, cur = collections.defaultdict(list), None
    for l in open(path, errors="replace"):
        m = re.match(r"^(\S+)\s+PROC", l)
        if m:
            cur = m.group(1)
            continue
        if re.match(r"^\S+\s+ENDP", l):
            cur = None
            continue
        m = LST.match(l)
        if m and cur:
            out[cur].append(m.group(3))
    return out


def one(src, votes, flags):
    with tempfile.TemporaryDirectory() as d:
        asm = os.path.join(d, os.path.splitext(os.path.basename(src))[0] + ".asm")
        try:
            img, hk, rc, out, log = c2trace.run_trace(src, [SPEC], [*flags], keep=d, func=False)
        except SystemExit as e:
            print("%s: %s" % (src, e), file=sys.stderr)
            return 0, 0
        if rc or not os.path.exists(asm):
            print("%s: rc %s" % (src, rc), file=sys.stderr)
            return 0, 0
        lst = listing(asm)
    funcs, cur = collections.defaultdict(list), None
    for l in log.splitlines():
        m = re.search(r'\| "([^"]*)"', l)
        if l.startswith("E ") and m:
            cur = m.group(1)
            funcs[cur] = []
        elif l.startswith("T I ") and cur:
            funcs[cur].append(l.split()[3])
    ok = bad = 0
    for f, ops in funcs.items():
        mn = lst.get(f)
        if mn is None:
            continue
        if len(ops) != len(mn):
            bad += 1
            continue
        ok += 1
        for o, n in zip(ops, mn):
            votes[o][n] += 1
    return ok, bad


if __name__ == "__main__":
    votes = collections.defaultdict(collections.Counter)
    for flags in (["/O2"], ["/O1"], ["/Od"]):
        for s in sys.argv[1:]:
            ok, bad = one(s, votes, flags)
            print("%s %s: %d functions aligned, %d not" % (s, flags[0], ok, bad), file=sys.stderr)
    json.dump({k: dict(v) for k, v in sorted(votes.items())}, sys.stdout, indent=0)
