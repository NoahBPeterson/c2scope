# the lifter at /O2 (docs/c2/lift2il.md): small sources compiled /O2 /arch:SSE /fp:fast, lifted from their own bytes
# and recompiled (o2_sx: a movsx load is a signed char); o2_fsel: `comiss; jbe` skips the code that runs when a > b
# (fcomi's flags: unordered jumps), written `a > b`, not the NaN-inclusive `!(a <= b)` that loads the operands the
# other way round (Spore 00745e00)
_r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'tools'); import il2c_od, json; "
                     "print(json.dumps(il2c_od.selfcheck('tests/data/lift/lift_o2.cpp', "
                     "'/O2 /MD /Gy /TP /arch:SSE /fp:fast')))"],
                    capture_output=True, text=True, cwd=HERE, env=dict(os.environ, C2_BUILD="15.00"))
try:
    _got = json.loads(_r.stdout.strip().splitlines()[-1])
except Exception:
    _got = {"?o2_sx@@": _r.stderr[-300:]}
check("lift /O2: a movsx load is a signed char", next((v for k, v in _got.items() if k.startswith("?o2_sx@@")), "?"),
      "MATCH")
import glob as _glob, hashlib as _hl
_g = os.path.join(HERE, "work", "scratch", "il2c_od", "gen_%s.cpp" % _hl.md5(b"?o2_fsel@@YAHPAUP@@HH@Z").hexdigest()[:8])
check("lift /O2: comiss + jbe skips the code that runs when a > b",
      bool(os.path.exists(_g) and re.search(r"\bt\d+ > \(float\)", open(_g).read())), True)
