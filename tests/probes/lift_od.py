# tools/il2c_od.py: the lifter's /Od mode (docs/c2/lift2il.md "/Od mode"): compile a small source /Od, lift each function
# from its own bytes, recompile at /Od: byte-identical. Always on the 15.00 toolchain (the build the Spore corpus uses),
# whatever build the rest of this run traces.
_r = subprocess.run([sys.executable, os.path.join(HERE, "tools", "il2c_od.py"),
                     os.path.join(HERE, "tests", "data", "lift", "lift_od.cpp")],
                    capture_output=True, text=True, cwd=HERE, env=dict(os.environ, C2_BUILD="15.00"))
_got = {l.split()[0]: " ".join(l.split()[1:]) for l in _r.stdout.splitlines() if l.startswith("?")}
for _nm, _what in (("?od_sum@@", "for loop with an induction variable and a loaded element"),
                   ("?od_while@@", "while loop"), ("?od_cp3@@", "three dword copies are one structure assignment"),
                   ("?od_vcall@@", "virtual call: the object evaluated for the vtable and again for this"),
                   ("?od_ptrwalk@@", "for loop with an empty init and a stride"),
                   ("?tf@T@@", "member: `this` is its own home slot, a bit test, a leftover eax is no return value"),
                   ("?tg@T@@", "member if / else, no return value"),
                   ("?od_ifelse@@", "if / else assigning one local"),
                   ("?od_fcopy@@", "float member copies stay separate statements (fld / fstp), no parentheses around the right side"),
                   ("?od_fadd@@", "float read-modify-write")):
    check("lift /Od: %s" % _what, next((v for k, v in _got.items() if k.startswith(_nm)), _r.stderr[-300:]), "MATCH")
