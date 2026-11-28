# The x64 lifter's pieces that need no executable (docs/c2/lift2il64.md): x64metrics' register mapping and
# regfix64's wanted registers, ehinfo64 on a hand-built image, lift2il64's nullcast / rdtsc / rotate shapes and
# web names, tidy64's callloads rung. With C2_CL64 set: ehinfo64 on a small /EHsc program compiled and linked by it.
import sys as _sys, struct as _st
_l64_build = os.environ.get("C2_BUILD")   # lift2il64.emitter_mod() pops C2_BUILD for il2c's dialect: put it back below,
                                          # or every later probe compiles with the default build
_sys.path.insert(0, os.path.join(HERE, "tools"))
import x64metrics as _xm, regfix64 as _rf, ehinfo64 as _eh, lift2il64 as _l64, tidy64 as _t64
import capstone as _cs

# x64metrics: the same code with rbx -> rsi and one slot moved is shape 0; the mapping says ours -> original
_orig = [("+0x0000", "push rbx"), ("+0x0001", "sub rsp, 0x20"), ("+0x0005", "mov rbx, rcx"),
         ("+0x0008", "mov dword ptr [rsp + 0x30], edx"), ("+0x000c", "call <target>"), ("+0x0011", "mov eax, ebx"),
         ("+0x0013", "add rsp, 0x20"), ("+0x0017", "pop rbx"), ("+0x0018", "ret")]
_ours = [(o, k.replace("rbx", "rsi").replace("ebx", "esi").replace("0x30", "0x38")) for o, k in _orig]
_m = _xm.metrics(_orig, _ours)
check("lift64: x64metrics on a register-renamed pair (edits, shape, mapcost, regpairs)",
      (_m["edits"], _m["edits_noreg"], _m["mapcost"], _m["regpairs"].get("rsi")), (5, 0, 2, "rbx"))
check("lift64: x64metrics on the same code is all zero", (_xm.metrics(_orig, _orig)["edits"],
                                                          _xm.metrics(_orig, _orig)["mapcost"]), (0, 0))
check("lift64: regfix64 wants the original's callee-saved register for each variable (scratch ones left)",
      _rf.wanted([("v1001", "rsi"), ("v1002", "rdi"), ("v1003", "rax")], {"rsi": "rbx", "rdi": "rdi", "rax": "rcx"}),
      ({"v1001": "rbx", "v1002": "rdi"}, {"v1001": "rbx"}))


class _Img(_l64.Image):
    """A flat image: bytes at base + 0 .. (no PE file); .pdata at pdata_rva when given."""
    def __init__(self, data, base=0x180000000, pdata=None):
        self.base, self.data, self.dir64 = base, bytes(data), set()
        self.secs = [(base, len(data), 0, len(data))]
        self.md = _cs.Cs(_cs.CS_ARCH_X86, _cs.CS_MODE_64)
        self.md.detail = True
        if pdata:
            class _D:
                VirtualAddress, Size = pdata
            self.pe = type("pe", (), {"OPTIONAL_HEADER": type("oh", (), {"DATA_DIRECTORY": {3: _D}})})

    def tls_start(self):
        return None


# ehinfo64 on a hand-built image: f (sub rsp, 0x38) has a FuncInfo with one state whose cleanup funclet destroys
# the local at [rsp+0x20] (lea rcx, [rdx+0x20]; jmp dtor)
_b = bytearray(0x3400)
_b[0x1000:0x100a] = bytes.fromhex("4883ec38904883c438c3")
_b[0x1100:0x1109] = bytes.fromhex("488d4a20") + b"\xe9" + _st.pack("<i", 0x1200 - 0x1109)
_b[0x1200] = 0xC3
_st.pack_into("<III", _b, 0x2000, 0x1000, 0x100a, 0x3000)
_b[0x3000:0x3006] = bytes([0x09, 4, 1, 0, 4, 0x62])                 # UNW_FLAG_EHANDLER, alloc 0x38
_st.pack_into("<II", _b, 0x3008, 0x1300, 0x3100)                      # handler, FuncInfo
_st.pack_into("<IiIIIIIi", _b, 0x3100, 0x19930522, 1, 0x3200, 0, 0, 2, 0x3300, 0x30)
_st.pack_into("<iI", _b, 0x3200, -1, 0x1100)
_st.pack_into("<IiIi", _b, 0x3300, 0x1000, -1, 0x1005, 0)
_img = _Img(_b, pdata=(0x2000, 12))
_i = _eh.eh_info(_img, 0x180001000)
check("lift64: ehinfo64 decodes a FuncInfo (states, frame, UnwindHelp, cleanup call and its object, ip2state)",
      (_i["max_state"], _i["frame"]["total"], _i["unwind_help"], _i["states"][0]["cleanup"]["calls"][0]["target"],
       _i["states"][0]["cleanup"]["calls"][0]["rcx"], _i["ip2state"]),
      (1, 0x38, 0x30, 0x180001200, ("frame", 0x20), [(0x180001000, -1), (0x180001005, 0)]))
check("lift64: lift2il64 reads the C++ frame from the unwind data (is_eh)",
      (_l64.Lifter(_img, 0x180001000, 10).is_eh(), _eh.eh_info(_img, 0x180001200)), (True, None))


def _unit(hexcode):
    code = bytes.fromhex(hexcode)
    return _l64.unit(_Img(code), 0x180000000, len(code))[0]


# cl's null-checked pointer adjustment (a static_cast between classes 16 bytes apart)
_u = _unit("488d4110" "4885c9" "7502" "31c0" "c3")
check("lift64: lea r, [x+16]; test x, x; jne; xor -> nullcast_up16(a0) and its helper classes",
      ("nullcast_up16(" in _u, "struct DerivedWithBaseAt16 : PadTo16, BaseAt16" in _u, "test" in _u), (True, True, False))
_u = _unit("0f31" "48c1e220" "4809d0" "c3")
check("lift64: rdtsc; shl rdx, 32; or rax, rdx -> __rdtsc()", "__rdtsc()" in _u and "<<" not in _u, True)
_u = _unit("89c8" "c1c003" "c3")
check("lift64: rol eax, 3 -> _rotl(.., 3)", bool(re.search(r"_rotl\(.*3", _u)), True)
_u = _unit("4883ec28" "488b5108" "488b09" "e800010000" "4883c428" "c3")
check("lift64: register webs are v1xxx (not t\\d+, cl's temporaries to ncolsim)",
      (bool(re.search(r"\bv1\d{3}\b", _u)), bool(re.search(r"\bt1\d{3}\b", _u))), (True, False))
# tidy64's callloads=0 rung keeps a load that feeds a call as its own statement (cl loads in statement order)
_fold = [r for r in _t64.LADDER if r.get("callloads", 1)][0]
_keep = [r for r in _t64.LADDER if r.get("callloads", 1) == 0][0]
_tf, _tk = _t64.tidy_one(_u, _fold), _t64.tidy_one(_u, _keep)
_cl_f = next(l for l in _tf.splitlines() if "FUN_" in l and "(" in l and not l.startswith(("void", "__int64", "int")))
_cl_k = next(l for l in _tk.splitlines() if "FUN_" in l and "(" in l and not l.startswith(("void", "__int64", "int")))
check("lift64: tidy64 callloads=0 keeps the argument loads out of the call (the folding rung puts them in)",
      ("*" in _cl_f, "*" in _cl_k), (True, False))

if os.environ.get("C2_CL64"):
    import shlex as _sh, tempfile as _tf2
    _d = _tf2.mkdtemp(prefix="lift64eh")
    open(os.path.join(_d, "t.cpp"), "w").write(
        "struct A { int x; __declspec(noinline) A(int v); __declspec(noinline) ~A(); };\n"
        "int g;\nA::A(int v) : x(v) {}\nA::~A() { g += x; }\n"
        "void use(A& a) { a.x++; }\nvoid (*volatile pu)(A&) = use;     // a call cl cannot prove nothrow\n"
        "__declspec(noinline) int f(int v) { A a(v), b(v + 1); pu(a); pu(b); return a.x + b.x; }\n"
        "int main() { return f(3); }\n")
    open(os.path.join(_d, "stub.c"), "w").write("int __CxxFrameHandler3(void) { return 0; }\n")
    _p = subprocess.run(_sh.split(os.environ["C2_CL64"]) + ["/nologo", "/EHsc", "/O2", "/GS-", "t.cpp", "stub.c",
                        "/link", "/nodefaultlib", "/entry:main", "/subsystem:console", "/map", "/out:t.exe"],
                        cwd=_d, capture_output=True, text=True)
    if not os.path.exists(os.path.join(_d, "t.exe")):
        skip("lift64: ehinfo64 on a compiled /EHsc program", "C2_CL64 did not link: " + _p.stdout[-200:])
    else:
        _syms = {}
        for _ln in open(os.path.join(_d, "t.map"), errors="replace"):
            _f = _ln.split()
            if len(_f) >= 3 and re.match(r"^[0-9a-f]{16}$", _f[2]) and _f[1].startswith("?"):
                _syms[_f[1]] = int(_f[2], 16)
        _img = _l64.Image(os.path.join(_d, "t.exe"))
        _fva = next(v for k, v in _syms.items() if k.startswith("?f@@"))
        _dva = next(v for k, v in _syms.items() if k.startswith("??1A@@"))
        _i = _eh.eh_info(_img, _fva)
        _calls = [c for s in (_i or {}).get("states", []) for c in (s.get("cleanup") or {}).get("calls", [])]
        check("lift64: ehinfo64 on f() of a compiled /EHsc program: 2 states, each destroying its own local with ~A",
              (_i and _i["max_state"], sorted({c["target"] == _dva for c in _calls}), len({c["rcx"] for c in _calls}),
               {c["rcx"][0] for c in _calls} <= {"frame", "argval"}, _l64.Lifter(_img, _fva, 16).is_eh()),
              (2, [True], 2, True, True))
else:
    skip("lift64: ehinfo64 on a compiled /EHsc program", "C2_CL64 is not set (x64check's compiler command)")
if _l64_build is not None:
    os.environ["C2_BUILD"] = _l64_build
