#!/usr/bin/env python3
"""The C++ exception tables of one x64 function (x64 __CxxFrameHandler3 FuncInfo), decoded so the
lifter can rebuild the objects whose destructors run on unwind instead of wrapping the body in try/catch.

    from ehinfo64 import eh_info
    info = eh_info(img, va)     # None when the function has no C++ frame handler

info = {
  "funcinfo": RVA, "max_state": n, "unwind_help": frame offset of the UnwindHelp slot,
  "frame": {"pushes": [...], "alloc": n, "total": bytes from entry rsp to the establisher frame, "fpreg": ...},
  "states": [{"state": i, "to": parent state, "action": funclet VA or 0, "cleanup": decoded funclet}],
  "tries": [{"low", "high", "catch_high", "catches": [{"adj", "type", "obj", "handler"}]}],
  "ip2state": [(VA, state), ...],
}

A decoded cleanup funclet is {"calls": [{"target": VA, "via": "call"|"jmp", "rcx": where}], "raw": [asm]}; rcx is
where the object is, as seen from the parent function:
  ("frame", off)        lea rcx, [rdx+off]           a local at parent rsp+off (after the prologue)
  ("framep", off)       mov rcx, [rdx+off]           a pointer held in a parent slot (this, a new'd block)
  ("argval", n)         lea rcx, [home slot n]       a by-value argument of 8 bytes or less, in its home slot
  ("home", n, add)      the slot is the parent's home slot of argument n, plus `add` (this+add: a member)
  ("framep+", off, add) mov rcx,[rdx+off]; add/lea +add: a member of an object held in a slot
Anything else is kept as the funclet text in "raw".

usage: ehinfo64.py VA [VA ...]        print the decoded tables
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

MAGICS = (0x19930520, 0x19930521, 0x19930522)
UWOP_PUSH_NONVOL, UWOP_ALLOC_LARGE, UWOP_ALLOC_SMALL, UWOP_SET_FPREG = 0, 1, 2, 3
UWOP_SAVE_NONVOL, UWOP_SAVE_NONVOL_FAR, UWOP_SAVE_XMM128, UWOP_SAVE_XMM128_FAR, UWOP_PUSH_MACHFRAME = 4, 5, 8, 9, 10
REG = ("rax", "rcx", "rdx", "rbx", "rsp", "rbp", "rsi", "rdi") + tuple("r%d" % n for n in range(8, 16))

def _u32(img, va):
    return struct.unpack("<I", img.read(va, 4))[0]


def _i32(img, va):
    return struct.unpack("<i", img.read(va, 4))[0]


def pdata(img):
    """{begin VA: (end VA, unwind VA)} from .pdata (kept on the image)."""
    got = getattr(img, "_ehinfo_pdata", None)
    if got is None:
        d = img.pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
        raw = img.read(img.base + d.VirtualAddress, d.Size)
        got = {}
        for k in range(0, len(raw) - 11, 12):
            b, e, u = struct.unpack_from("<III", raw, k)
            if b:
                got[img.base + b] = (img.base + e, img.base + u)
        img._ehinfo_pdata = got
    return got


def unwind(img, uva):
    """UNWIND_INFO at uva: (flags, prolog size, codes, frame reg, frame off, handler VA, handler data VA, chain)."""
    b = img.read(uva, 4)
    flags, plen, n, fr = b[0] >> 3, b[1], b[2], b[3]
    raw = img.read(uva + 4, 2 * n)
    codes = []
    k = 0
    while k < n:
        off, op = raw[2 * k], raw[2 * k + 1]
        uop, info = op & 15, op >> 4
        ext = None
        if uop == UWOP_ALLOC_LARGE:
            if info == 0:
                ext = struct.unpack_from("<H", raw, 2 * k + 2)[0] * 8
                k += 1
            else:
                ext = struct.unpack_from("<I", raw, 2 * k + 2)[0]
                k += 2
        elif uop in (UWOP_SAVE_NONVOL, UWOP_SAVE_XMM128):
            ext = struct.unpack_from("<H", raw, 2 * k + 2)[0] * (8 if uop == UWOP_SAVE_NONVOL else 16)
            k += 1
        elif uop in (UWOP_SAVE_NONVOL_FAR, UWOP_SAVE_XMM128_FAR):
            ext = struct.unpack_from("<I", raw, 2 * k + 2)[0]
            k += 2
        codes.append((off, uop, info, ext))
        k += 1
    tail = uva + 4 + 2 * ((n + 1) & ~1)
    hva = hdata = chain = None
    if flags & 0x4:
        cb = struct.unpack("<III", img.read(tail, 12))
        chain = (img.base + cb[0], img.base + cb[2])
    elif flags & 0x3:
        hva = img.base + _u32(img, tail)
        hdata = tail + 4
    return flags, plen, codes, fr & 15, (fr >> 4) * 16, hva, hdata, chain


def frame_of(codes, freg, foff):
    """Pushes, allocation and the distance from the entry rsp (after the return address) to the parent's frame
    pointer as cl's funclets receive it in rdx (the establisher frame: rsp after the prologue, or the frame
    register minus its offset)."""
    pushes, alloc, saves, xsaves, fp = [], 0, {}, {}, None
    for off, uop, info, ext in codes:
        if uop == UWOP_PUSH_NONVOL:
            pushes.append(REG[info])
        elif uop in (UWOP_ALLOC_LARGE,):
            alloc += ext
        elif uop == UWOP_ALLOC_SMALL:
            alloc += info * 8 + 8
        elif uop == UWOP_SET_FPREG:
            fp = (REG[freg], foff)
        elif uop in (UWOP_SAVE_NONVOL, UWOP_SAVE_NONVOL_FAR):
            saves[REG[info]] = ext
        elif uop in (UWOP_SAVE_XMM128, UWOP_SAVE_XMM128_FAR):
            xsaves["xmm%d" % info] = ext
    pushes.reverse()                       # codes are listed last-first
    total = 8 * len(pushes) + alloc        # entry rsp (return address at [rsp]) minus the frame base
    return {"pushes": pushes, "alloc": alloc, "saves": saves, "xsaves": xsaves, "fpreg": fp, "total": total}


def funcinfo_of(img, va):
    """(primary begin VA, UNWIND fields, FuncInfo VA) for the function at va, or None."""
    pd = pdata(img)
    if va not in pd:
        return None
    end, uva = pd[va]
    u = unwind(img, uva)
    seen = 0
    while u[7] and seen < 16:              # a chained fragment: the handler is on the primary
        va, uva = u[7]
        u = unwind(img, uva)
        seen += 1
    if u[5] is None or not (u[0] & 0x3):
        return None
    fi = img.base + _u32(img, u[6])
    if _u32(img, fi) not in MAGICS:
        return None
    return va, u, fi


def _rcx_from(insns, k, frame):
    """Where rcx points before the call at index k of a funclet, as seen from the parent's frame (rdx at entry,
    kept in rbp by cl's funclet prologue)."""
    base = {"rdx": ("frame", 0)}               # register -> ("frame", off) meaning parent-frame + off
    val = {}
    for i in insns[:k]:
        m, ops = i.mnemonic, [o.strip() for o in i.op_str.split(",")]
        if m == "mov" and len(ops) == 2 and ops[1] in base and ops[0] in REG:
            base[ops[0]] = base[ops[1]]
            val.pop(ops[0], None)
        elif m in ("mov", "lea") and len(ops) == 2 and ops[0] in REG and ops[1].startswith("qword ptr [") or \
                m == "lea" and len(ops) == 2 and ops[0] in REG:
            inner = ops[1][ops[1].index("[") + 1:-1].replace(" ", "")
            r, _, d = inner.partition("+")
            neg = False
            if not d and "-" in inner:
                r, d = inner.split("-", 1)
                neg = True
            try:
                d = int(d, 16) if d else 0
            except ValueError:
                base.pop(ops[0], None)
                val.pop(ops[0], None)
                continue
            d = -d if neg else d
            if r in base and base[r][0] == "frame":
                off = base[r][1] + d
                if m == "lea":
                    base[ops[0]] = ("frame", off)
                    val.pop(ops[0], None)
                else:
                    val[ops[0]] = ("framep", off, 0)
                    base.pop(ops[0], None)
            elif r in val and m == "lea":
                v = val[r]
                val[ops[0]] = (v[0], v[1], v[2] + d)
                base.pop(ops[0], None)
            else:
                base.pop(ops[0], None)
                val.pop(ops[0], None)
        elif m == "add" and len(ops) == 2 and ops[0] in val:
            try:
                v = val[ops[0]]
                val[ops[0]] = (v[0], v[1], v[2] + int(ops[1], 16))
            except ValueError:
                val.pop(ops[0])
    if "rcx" in val:
        kind, off, add = val["rcx"]
        return _home(("framep", off, add), frame)
    if "rcx" in base:
        off = base["rcx"][1]
        h = off - frame["total"] - 8
        if h >= 0 and h % 8 == 0 and h // 8 < 4:
            return ("argval", h // 8)          # a by-value argument kept in its home slot (8 bytes or less)
        return ("frame", off)
    return None


def _home(v, frame):
    """A parent slot above the frame is an argument home slot: entry rsp + 8 * (n + 1)."""
    kind, off, add = v
    h = off - frame["total"] - 8
    if h >= 0 and h % 8 == 0 and h // 8 < 4:
        return ("home", h // 8, add)
    return ("framep", off, add) if add == 0 else ("framep+", off, add)


def cleanup(img, va, frame, names=None):
    """Decode a cleanup funclet: the calls it makes and what rcx is at each."""
    out = {"calls": [], "raw": []}
    ins = img.insns(va, 0x80)
    body = []
    for i in ins:
        body.append(i)
        if i.mnemonic in ("ret", "jmp") or i.mnemonic == "int3":
            break
    out["raw"] = ["%s %s" % (i.mnemonic, i.op_str) for i in body]
    for k, i in enumerate(body):
        if i.mnemonic in ("call", "jmp") and i.operands and i.operands[0].type == 2:   # imm target
            t = i.operands[0].imm
            if i.mnemonic == "jmp" and va <= t < va + 0x80:
                continue
            c = {"target": t, "via": i.mnemonic, "rcx": _rcx_from(body, k, frame)}
            if names:
                c["name"] = names.get(t)
            out["calls"].append(c)
        elif i.mnemonic == "call" and "qword ptr [rip" in i.op_str:
            t = i.address + i.size + i.operands[0].mem.disp
            out["calls"].append({"target": t, "via": "icall", "rcx": _rcx_from(body, k, frame),
                                 "name": (names or {}).get(t)})
    return out


def eh_info(img, va, names=None):
    got = funcinfo_of(img, va)
    if not got:
        return None
    pva, u, fi = got
    flags, plen, codes, freg, foff, hva, hdata, chain = u
    frame = frame_of(codes, freg, foff)
    magic, maxst, d_um, ntry, d_try, nip, d_ip, uhelp = struct.unpack("<IiIIIIIi", img.read(fi, 32))
    states = []
    for s in range(maxst):
        e = img.base + d_um + 8 * s
        to, act = _i32(img, e), _u32(img, e + 4)
        st = {"state": s, "to": to, "action": img.base + act if act else 0}
        if act:
            st["cleanup"] = cleanup(img, img.base + act, frame, names)
        states.append(st)
    tries = []
    for t in range(ntry):
        e = img.base + d_try + 20 * t
        lo, hi, ch, nc, dh = struct.unpack("<iiiII", img.read(e, 20))
        cs = []
        for c in range(nc):
            adj, dt, obj, dho, dfr = struct.unpack("<IIiII", img.read(img.base + dh + 20 * c, 20))
            cs.append({"adj": adj, "type": img.base + dt if dt else 0, "obj": obj,
                       "handler": img.base + dho, "frame": dfr})
        tries.append({"low": lo, "high": hi, "catch_high": ch, "catches": cs})
    ip = []
    for k in range(nip):
        e = img.base + d_ip + 8 * k
        ip.append((img.base + _u32(img, e), _i32(img, e + 4)))
    return {"va": va, "primary": pva, "funcinfo": fi, "magic": magic, "max_state": maxst, "unwind_help": uhelp,
            "frame": frame, "prolog": plen, "states": states, "tries": tries, "ip2state": ip}


def describe(info):
    f = info["frame"]
    out = ["0x%x  FuncInfo 0x%x  states %d  tries %d  UnwindHelp [rsp+%#x]  frame: push %s alloc %#x total %#x%s"
           % (info["va"], info["funcinfo"], info["max_state"], len(info["tries"]), info["unwind_help"],
              ",".join(f["pushes"]) or "-", f["alloc"], f["total"],
              "  saves " + ",".join("%s@%#x" % kv for kv in f["saves"].items()) if f["saves"] else "")]
    for s in info["states"]:
        c = s.get("cleanup")
        calls = "; ".join("%s %s(%s)" % (x["via"], x.get("name") or "0x%x" % x["target"], x["rcx"])
                          for x in (c or {}).get("calls", []))
        out.append("  state %d -> %d  %s" % (s["state"], s["to"], calls or ("(no action)" if not c else
                                                                             " | ".join(c["raw"]))))
    for t in info["tries"]:
        out.append("  try %d..%d catch..%d  %s" % (t["low"], t["high"], t["catch_high"],
                                                    ["0x%x adj %x obj %#x" % (c["handler"], c["adj"], c["obj"])
                                                     for c in t["catches"]]))
    out.append("  ip2state " + " ".join("%x:%d" % (a - info["va"], s) for a, s in info["ip2state"]))
    return "\n".join(out)


def load_names():
    import json
    import lift2il64 as L
    p = os.environ.get("LIFT64_SYMBOLS")         # {"symbols": {name: "0x..."}}: names for the funclets' callees
    if not p:
        return {}
    try:
        d = json.load(open(p))
    except Exception:
        return {}
    out = {}
    for k, v in (d.get("symbols") or {}).items():           # name -> "0x..." address
        try:
            out.setdefault(int(v, 16), k)
        except (ValueError, TypeError):
            pass
    return out


if __name__ == "__main__":
    import lift2il64 as L
    img = L.Image()
    names = load_names()
    for a in sys.argv[1:]:
        info = eh_info(img, int(a, 16), names)
        print(describe(info) if info else "0x%s: no C++ frame handler" % a)
