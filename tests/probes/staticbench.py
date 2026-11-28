# tools/staticbench.py: the flags verify passes to a project's chk.py. Spore builds a 13.10 row only with its `/vc71`
# pseudo-flag (else chk.py uses VS2008 and reports false failures); 15.00 rows and M&B (13.10 natively) are unchanged.
sys.path.insert(0, os.path.join(HERE, "tools"))
import staticbench as _sb
check("staticbench verify: a Spore 13.10 row gets /vc71",
      _sb.chk_flags("spore_o2_v2", {"flags": "/O2 /MD /Gy /EHsc /TP", "c2": "13.10.3077"}), "/vc71 /O2 /MD /Gy /EHsc /TP")
check("staticbench verify: a Spore 15.00 row keeps its flags",
      _sb.chk_flags("spore_od_v2", {"flags": "/Od /Ob1 /TP", "c2": "15.00"}), "/Od /Ob1 /TP")
check("staticbench verify: an M&B 13.10 row keeps its flags",
      _sb.chk_flags("mb_v2", {"flags": "/O2 /GS", "c2": "13.10.3077"}), "/O2 /GS")
