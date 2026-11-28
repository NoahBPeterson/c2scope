# tools/il2c_lift.py inline_sites: a leaf callee's body shape and its inputs (stack arguments, `this`), the key the
# lifter matches inlined copies of byte-exact callees by (docs/c2/lift2il.md "Inlined callees")
import capstone as _cs
sys.path.insert(0, os.path.join(HERE, "tools"))
import il2c_lift as _il2c_lift
_md = _cs.Cs(_cs.CS_ARCH_X86, _cs.CS_MODE_32)
# SetKey-like: mov eax,[esp+4]; mov edx,[eax]; mov [ecx],edx; mov edx,[eax+4]; mov [ecx+4],edx; mov eax,[eax+8];
# mov [ecx+8],eax; ret 4
_code = bytes.fromhex("8b4424048b1089118b5004895104" "8b4008894108c20400")
_body, _inp = _il2c_lift._callee_body(list(_md.disasm(_code, 0x401000)))
check("lift inline: a leaf callee's body (stack-argument load and ret dropped) and inputs (argument 0, this)",
      (len(_body), sorted(map(str, _inp)), _il2c_lift._ins_shape(_body[0])),
      (6, ["('eax', 0)", "('ecx', 'this')"], "mov R, dword ptr [R]"))
