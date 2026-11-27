#!/usr/bin/env python3
"""Frame-colouring inverse: which source edit gives the stack-slot layout the original has.  Doc: docs/c2/slotwant.md

usage: slotwant.py <src> --func F (--asm-vs ORIG.s | --image EXE --va VA | --want W ...) [--want W ...]
                   [--max-compiles N] [--jobs J] [--depth 1|2|3] [--no-verify] [--keep DIR] [-- <cl args>]
       slotwant.py <src> --func F --diag (--asm-vs ORIG.s | --image EXE --va VA)     the derived wants only

c2trace.py slots says why each element got its slot (the packer's tries) and replays the packer (docs/c2/slots.md).
This tool runs the other way, in four steps:

  1. the want. Explicit (--want, repeatable; NAME = a listing name such as _namebuf$118012, a name stem, L<line>
     (the element first referenced on that line) or #IDX; $T and tv temporaries are best named by L<line>):
       with:A,B     A shares B's slot (one colour)          apart:A,B    A and B in different slots
       at:A=OFF     A at packer offset OFF                  frame=N      the frame (sub esp) is N bytes
       ncol:SIZE=N  N colours of SIZE bytes
     or derived from the original (--asm-vs / --image): its frame size, and for elements whose slot the original's
     aligned code shows (2+ votes, a majority) every pair that shares a slot in one listing and not in the other
     (pairs are shift-invariant: an extra slot of the original at the bottom of the frame moves every offset below
     it, not which elements share). --diag prints them; name the ones that matter with --want;
  2. why: for each unmet with:A,B, the packer's decision for the one of A, B it visits later (X; Y the other): the
     colour Y was in at X's visit (not made yet / refused for size / for a conflict with member M / X took a newer
     colour first). The inputs that decide it are the packer's (slots.md section 1): element size (the visiting
     order is size ascending, and a colour admits at most twice its size), use count (visiting order within a size;
     the key 1000*uses/size orders colours when T > 128), and the conflict sets (LiveOnDef: overlapping lifetimes);
  3. the model inverse: single model edits, then pairs of the most promising ones, replayed with c2slots.Model and
     scored by the wants met, then the frame size, then the number of other elements moved. Edits:
       size:X=N  (halved, doubled, quartered)    uses:X=N (+-1..3, either end of its run)
       conf-:X,M (a refusal on X's way to the wanted colour; join:X>Y removes every conflict with Y's colour)
       conf+:X,M (a member of the colour X joined)
       add:bridgeN (the wanted colour is too small for X: an element of N bytes the original has and we lack)
     --depth 3 extends the 10 best pairs by a third edit. With an original, the second score component is the number
     of large well-voted elements whose slot-mates are the original's;
  4. levers: each model edit becomes the source edits that make it (catalogue LEVERS below), on the declarations and
     uses of the named elements, written so that no source line moves (the listing's line numbers stay valid and the
     variants' elements are matched to the base's by name stem, line and size). Every lever variant is compiled with
     the slots trace (--jobs at once, at most --max-compiles) and judged on what it really did: the wants met in the
     traced packing, the frame size, the elements whose slot moved, and bdiff against the original's code.

LEVERS (source edits; what they move in the packer; scope levers only for address-taken elements, a `lea` of them in
our listing: any other element lives from its definitions to its uses whatever its scope):
  narrow:X     wrap X's declaration .. the statement of its last use in its own `{ }` block: an address-taken X is
               then dead after the block (a function-scope one lives over its whole scope)         -> conf-:X,*
  narrow-late:X  the same, with X's declaration moved down to the statement of its first use (no initializer) -> conf-
  widen:X      X's declaration moved up to the start of the function body (builtin or array type, no initializer):
               an address-taken X then lives from the start                                          -> conf+:X,*
  share:X,Y    one variable for both: Y's declaration deleted and its uses renamed to X, X widened to the block that
               holds both (same declared type)                                                      -> one element
  size:X=N     an array's dimension changed (X's type is the original's: check it against what the code needs)
uses:/pos: edits have no catalogue lever (a use count is the number of frame operands in the final code); they are
printed as the model's answer only.

Variants live in work/slotwant/<stem>-<hash>/v<k>/ (the variant's source, its listing, snapshot and trace log).
"""
import argparse, collections, concurrent.futures as cf, hashlib, itertools, json, os, re, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace          # noqa: F401  (takes --c2 / --unit out of argv; the traced compile)
import c2slots as CS

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILTIN = {"char", "signed", "unsigned", "short", "int", "long", "float", "double", "bool", "__int64", "__int8",
           "__int16", "__int32", "wchar_t", "uint8", "uint16", "uint32", "int8", "int16", "int32", "DWORD", "WORD",
           "BYTE", "BOOL", "size_t"}
KEYWORDS = {"return", "if", "else", "while", "for", "do", "switch", "case", "default", "break", "continue", "goto",
            "delete", "new", "throw", "try", "catch", "sizeof", "typedef", "using", "namespace"}


# ================================================================ element identity across compiles
def stem(lname):
    """`_hbuf$117756` -> `_hbuf`, `$T183410` -> `$T`, `tv3489` -> `tv`."""
    if lname.startswith("$T"):
        return "$T"
    if re.fullmatch(r"tv\d+", lname):
        return "tv"
    return lname.split("$")[0]


def ekey(s, i):
    """(name stem, first source line): the same element in two compiles of sources with the same lines (a size
    lever changes the size, so the size is not part of it)."""
    L = s["listing"].get(i) or {}
    return (stem(L.get("lname") or CS.ename(s, i)), L.get("line"))


def keymap(s):
    """{(stem, line, k): idx}: k numbers elements that share the first two (in size order)."""
    out, seen = {}, collections.Counter()
    for i in sorted(s["elems"], key=lambda i: (s["elems"][i]["size"], i)):
        k = ekey(s, i)
        out[k + (seen[k],)] = i
        seen[k] += 1
    return out


def ikey(s, i):
    for k, j in keymap(s).items():
        if j == i:
            return k
    return None


# ================================================================ frames
def frame_alloc(ins):
    """The prologue's frame allocation in bytes: `sub esp, N`, `mov eax, N; call __chkstk`, or 4 for `push ecx`."""
    for k, (mn, ops) in enumerate(ins[:24]):
        if mn == "mov" and k + 1 < len(ins) and ins[k + 1][0] == "call" and "chkstk" in ins[k + 1][1].lower():
            m = re.match(r"eax,\s*(0x[0-9a-f]+|\d+)", ops.lower())
            if m:
                return CS._num(m.group(1).replace("h", ""))
        if mn == "sub" and re.match(r"esp,", ops.replace(" ", "")):
            v = ops.split(",", 1)[1].strip().lower()
            try:
                return int(v[:-1], 16) if v.endswith("h") else CS._num(v)
            except ValueError:
                return None
        if mn == "call" and k > 0 and ins[k - 1][0] == "mov" and "eax" in ins[k - 1][1]:
            m = re.match(r"eax,\s*(\w+)", ins[k - 1][1].lower())
            if m:
                v = m.group(1)
                return int(v[:-1], 16) if v.endswith("h") else CS._num(v)
    return None


def our_frame(asm, func):
    import tsweep
    try:
        L = tsweep.listing_instrs(asm, func)
    except KeyError:
        return None
    return frame_alloc([(mn, ops) for mn, ops in L])


def func_first_line(asm, func):
    text = open(asm, errors="replace").read()
    i, j = c2trace.listing_proc(text, func)
    m = re.search(r"^; (\d+)\s*:", text[i:j], re.M)
    return int(m.group(1)) if m else None


# ================================================================ the original
def orig_keys(s, asm, ins, callee_pop=None):
    """({idx: (key, votes, total)}, info): the original's slot of each of our elements seen in aligned code, in the
    packer's coordinates. x86 ESP frames: the key relative to the return-address slot IS the packer offset (no shift
    is fitted: frames of different sizes keep the top of the frame in place); EBP frames and x64: c2slots' map."""
    om, oi = CS.orig_map(s, asm, ins, callee_pop)
    if CS.is_x64() or s["fn"].get("ebp"):
        return om, oi
    return {i: (o + (oi["pshift"] if o > 0 else oi["shift"]), n, t) for i, (o, n, t) in om.items()}, oi


def saved_regs(ins):
    """Callee-saved registers the prologue pushes (before the first call / frame use)."""
    n = 0
    for mn, ops in ins[:12]:
        if mn == "push" and ops.strip().lower() in ("ebx", "ebp", "esi", "edi"):
            n += 1
        elif mn == "call" or (mn == "mov" and "ebp" in ops and "esp" in ops):
            break
    return n


ESPSYM = re.compile(r"([_$A-Za-z0-9?@]+)\[esp\s*\+\s*(\d+)\]")


def orig_keys_ourdepth(s, asm, ins):
    """{idx: Counter(key)}: the original's slots with OUR push depth at each aligned instruction (the listing's
    `name$[esp+K]` gives it exactly) instead of the original's walked depth: a walk that loses a push (a cdecl
    cleanup merged across calls, a by-value argument) shifts a whole stretch by 4; aligned code has the same pushes
    on both sides almost everywhere. x86 ESP frames only."""
    import regmap, tsweep
    lst = tsweep.listing_offsets(asm, s["func"])
    offs = sorted(lst)
    raw = [lst[o] for o in offs]
    ourins = [(t.split()[0], t.split(None, 1)[1] if " " in t else "") for t in raw]
    ours = [regmap.norm(tsweep.canon_listing(mn, ops)) for mn, ops in ourins]
    orig = [(mn, ops) for _, mn, ops in ins]
    pairs = regmap.align(ours, orig)
    adj = ((frame_alloc(ourins) or 0) + 4 * saved_regs(ourins)) - \
          ((frame_alloc(orig) or 0) + 4 * saved_regs(orig))
    name2i = {L["lname"]: i for i, L in s["listing"].items()}
    votes = collections.defaultdict(collections.Counter)
    for i, j in pairs:
        us = ESPSYM.findall(raw[i])
        os_ = [m for m in CS.FRAMEOP.finditer(orig[j][1]) if m.group(1) == "esp"]
        if not us or len(us) != len(os_):
            continue
        for (nm, K), m in zip(us, os_):
            e = name2i.get(nm)
            if e is None:
                continue
            d = CS._num(m.group(3)) * (-1 if m.group(2) == "-" else 1) if m.group(3) else 0
            votes[e][d - int(K) + adj] += 1
    return votes


def merge_orig(om, votes2):
    """Per element the map whose top key has the larger share of its votes (ties: the walked depth)."""
    out = dict(om)
    for i, c in votes2.items():
        k2, n2 = c.most_common(1)[0]
        t2 = sum(c.values())
        if i not in om:
            out[i] = (k2, n2, t2)
            continue
        k1, n1, t1 = om[i]
        if n2 * t1 >= n1 * t2:
            out[i] = (k2, n2, t2)
    return out


def derive_wants(s, res, om, min_size=8, cap=24):
    """Pair wants from the original's map: (kind, a, b, weight) for well-voted elements of min_size bytes or more."""
    good = {i: o for i, (o, n, t) in om.items() if n >= 2 and 2 * n > t and s["elems"][i]["size"] >= min_size
            and not s["elems"][i]["param"]}
    offs = res["offs"]
    out = []
    for a, b in itertools.combinations(sorted(good), 2):
        same_o, same_u = good[a] == good[b], offs.get(a) == offs.get(b)
        if same_o != same_u:
            w = min(s["elems"][a]["size"], s["elems"][b]["size"])
            out.append(("with" if same_o else "apart", a, b, w))
    out.sort(key=lambda w: (-w[3], w[1], w[2]))
    return out[:cap]


# ================================================================ wants
def ncols(offs, sizes):
    """{colour size: number of colours} from element offsets (a colour = the elements at one offset; its size the
    largest member's, rounded up to 4)."""
    by = collections.defaultdict(int)
    for i, o in offs.items():
        if o is not None and i in sizes:
            by[o] = max(by[o], sizes[i])
    out = collections.Counter(CS.align4(z) for z in by.values())
    return dict(out)


class Want:
    def __init__(self, kind, a=None, b=None, val=None, w=1, text=None):
        self.kind, self.a, self.b, self.val, self.w, self.text = kind, a, b, val, w, text

    def met(self, offs, frame, sizes=None):
        if self.kind == "with":
            return offs.get(self.a) is not None and offs.get(self.a) == offs.get(self.b)
        if self.kind == "apart":
            return offs.get(self.a) != offs.get(self.b)
        if self.kind == "at":
            return offs.get(self.a) == self.val
        if self.kind == "frame":
            return frame == self.val
        if self.kind == "ncol":
            return ncols(offs, sizes or {}).get(self.b, 0) == self.val
        return False

    def label(self, s):
        if self.text:
            return self.text
        if self.kind == "frame":
            return "frame=%d" % self.val
        if self.kind == "ncol":
            return "ncol:%d=%d" % (self.b, self.val)
        if self.kind == "at":
            return "at:%s=%d" % (CS.ename(s, self.a), self.val)
        return "%s:%s,%s" % (self.kind, CS.ename(s, self.a), CS.ename(s, self.b))

    def remap(self, km_base, s_base, km_var):
        """The same want on a variant's snapshot (elements matched by key); None when an element is gone."""
        def m(i):
            if i is None:
                return None
            for k, j in km_base.items():
                if j == i:
                    return km_var.get(k)
            return None
        if self.kind in ("frame", "ncol"):
            return Want(self.kind, None, self.b, self.val, self.w, self.label(s_base))
        a, b = m(self.a), m(self.b)
        if self.kind in ("with", "apart") and (a is None or b is None):
            return None
        if self.kind == "at" and a is None:
            return None
        return Want(self.kind, a, b, self.val, self.w, self.label(s_base))


def parse_want(s, text):
    kind, _, arg = text.partition(":")
    if text.startswith("frame="):
        return Want("frame", val=int(text.split("=", 1)[1], 0), w=4)
    if kind == "ncol":
        z, _, n = arg.partition("=")
        return Want("ncol", b=int(z), val=int(n), w=max(4, int(z)))
    if kind in ("with", "apart"):
        a, b = (CS.resolve(s, x.strip()) for x in arg.split(",", 1))
        return Want(kind, a, b, w=max(4, min(s["elems"][a]["size"], s["elems"][b]["size"])))
    if kind == "at":
        nm, _, v = arg.partition("=")
        a = CS.resolve(s, nm)
        return Want("at", a, val=int(v, 0), w=max(4, s["elems"][a]["size"]))
    sys.exit("want %r: with:A,B apart:A,B at:A=OFF frame=N ncol:SIZE=N" % text)


ORIG_PART = {}      # {element: the original's address} of the well-voted elements of 8+ bytes (main sets it)


def agreement(offs):
    """Elements of ORIG_PART whose slot-mates (among ORIG_PART) are the original's."""
    if not ORIG_PART:
        return 0
    ours, orig = collections.defaultdict(set), collections.defaultdict(set)
    for i, o in ORIG_PART.items():
        ours[offs.get(i)].add(i)
        orig[o].add(i)
    return sum(1 for i, o in ORIG_PART.items() if ours[offs.get(i)] == orig[o])


def score(wants, offs, frame, base_offs=None, orig_frame=None, sizes=None):
    """(weight of the wants met, elements sharing slots as in the original, -|frame - orig|, -elements moved):
    larger is better."""
    met = sum(w.w for w in wants if w.met(offs, frame, sizes))
    fd = -abs(frame - orig_frame) if orig_frame is not None and frame is not None else 0
    mv = -sum(1 for i in set(offs) | set(base_offs or {}) if (base_offs or {}).get(i) != offs.get(i)) if base_offs else 0
    return (met, agreement(offs), fd, mv)


# ================================================================ why
def why_with(s, res, a, b):
    """Words: why a and b are not in one colour (the decision of the later-visited one)."""
    order = CS.base_order(s)
    pa, pb = order.index(a), order.index(b)
    X, Y = (a, b) if pa > pb else (b, a)
    E = s["elems"]
    log = res["log"].get(X) or {}
    cy = (res["log"].get(Y) or {}).get("res")
    nm = lambda i: CS.ename(s, i)
    if cy is None:
        return "%s: %s has no local colour (a parameter home?)" % (nm(X), nm(Y)), X, Y, None
    tried = dict((c, w) for c, w in log.get("tries", []))
    C = res["created"][cy] if cy < len(res["created"]) else None
    out = "%s (size %d, uses %d, visit %d) is visited after %s (visit %d, colour %d made by %s): " % (
        nm(X), E[X]["size"], E[X]["uses"], order.index(X), nm(Y), order.index(Y), cy, nm(C["members"][0]) if C else "?")
    w = tried.get(cy)
    blockers = []
    if w == "size":
        out += "colour %d is refused for size (%d > 2 x the colour's size then)" % (cy, E[X]["size"])
    elif isinstance(w, (list, tuple)) and w[0] == "conf":
        mdl = CS.model_of(s)
        blockers = [m for m in C["members"] if order.index(m) < order.index(X) and
                    (mdl.lod[X] >> m & 1 or mdl.lod[m] >> X & 1)]
        out += "colour %d is refused for a conflict with %s" % (cy, ", ".join(nm(m) for m in blockers) or nm(w[1]))
    elif log.get("res") is not None and log["res"] != cy:
        newer = [c for c, _ in log.get("tries", [])]
        out += "it joined colour %d (newest first) before trying colour %d" % (log["res"], cy) if cy not in newer \
            else "colour %d tried: %s" % (cy, w)
    else:
        out += "?"
    return out, X, Y, blockers


# ================================================================ model search
def model_edits(s, res, wants, focus):
    """Candidate single edits (lists of c2slots EDIT strings) for the focus elements."""
    E = s["elems"]
    cands = []
    for i in sorted(focus):
        if E[i]["param"]:
            continue
        nm = "#%d" % i
        for d in (-3, -2, -1, 1, 2, 3):
            u = E[i]["uses"] + d
            if u >= 1:
                cands += [["uses:%s=%d" % (nm, u)], ["uses:%s=%d^" % (nm, u)]]
        z = E[i]["size"]
        for n in sorted({z // 2, z * 2, z // 4} if z >= 8 else {1, 2, 4, 8} - {z}):
            if n >= 1:
                cands += [["size:%s=%d" % (nm, n)], ["size:%s=%d^" % (nm, n)]]
        lg = res["log"].get(i) or {}
        for c, why in lg.get("tries", []):
            if isinstance(why, (list, tuple)) and why[0] == "conf":
                cands.append(["conf-:%s,#%d" % (nm, why[1])])
        if lg and not lg.get("new"):
            for m in res["created"][lg["res"]]["members"][:6]:
                if m != i:
                    cands.append(["conf+:%s,#%d" % (nm, m)])
    mdl = CS.model_of(s)
    for w in wants:
        if w.kind == "with":                 # join: every conflict between the later one and the other's colour
            order = CS.base_order(s)
            X, Y = (w.a, w.b) if order.index(w.a) > order.index(w.b) else (w.b, w.a)
            cy = (res["log"].get(Y) or {}).get("res")
            if cy is None:
                continue
            mem = [m for m in res["created"][cy]["members"] if m != X]
            bl = [m for m in mem if mdl.lod[X] >> m & 1 or mdl.lod[m] >> X & 1]
            if bl:
                cands.append(["conf-:#%d,#%d" % (X, m) for m in bl])
            # a bridge: Y's colour is too small for X (a colour admits at most twice its size) and only an element
            # between the two sizes, joining Y's colour first, makes it big enough. The original then has an
            # element we lack (a temporary or a local live where Y is): `add:` with Y's conflicts, and every member
            # of a newer colour that would take it first
            C = res["created"][cy]
            csz = max(E[m]["size"] for m in C["members"] if order.index(m) < order.index(X)) if any(
                order.index(m) < order.index(X) for m in C["members"]) else C["size"]
            if E[X]["size"] > 2 * csz:
                z = max(csz + 1, (E[X]["size"] + 1) // 2)
                if z <= 2 * csz:
                    z = (z + 3) // 4 * 4
                    conf = {m for m in range(len(order)) if mdl.lod[Y] >> m & 1 or mdl.lod.get(m, 0) >> Y & 1}
                    for C2 in res["created"][cy + 1:]:
                        if 2 * C2["size"] >= z:
                            conf.update(C2["members"])
                    conf = sorted(c for c in conf if c in E and c not in C["members"])
                    cands.append(["add:bridge%d,%d,1,conf=%s" % (z, z, ";".join("#%d" % c for c in conf))])
        if w.kind == "apart":
            cands.append(["conf+:#%d,#%d" % (w.a, w.b)])
    seen, out = set(), []
    for c in cands:
        t = tuple(c)
        if t not in seen:
            seen.add(t)
            out.append(c)
    return out


def run_model(s, edits):
    try:
        res, el = CS.run_edits(dict(s, listing=dict(s["listing"])), edits)
        res["_sizes"] = {i: x["size"] for i, x in el.items()}
        return res
    except (SystemExit, KeyError, ValueError):
        return None


def search(s, res, wants, frame0, ofr, focus, depth=2, pool=40, top=12):
    sz0 = {i: x["size"] for i, x in s["elems"].items()}
    base_s = score(wants, res["offs"], frame0, res["offs"], ofr, sz0)
    rows = []
    singles = []
    for ed in model_edits(s, res, wants, focus):
        r = run_model(s, ed)
        if r is None:
            continue
        fr = frame0 + (r["T"] - res["T"]) if frame0 is not None else None
        sc = score(wants, r["offs"], fr, res["offs"], ofr, r["_sizes"])
        singles.append((sc, ed, r, fr))
    singles.sort(key=lambda x: x[0], reverse=True)
    rows += [x for x in singles if x[0][:2] > base_s[:2]]
    if depth >= 2:
        pl = [x for x in singles if x[0][:2] != base_s[:2]][:pool]
        for (s1, e1, _, _), (s2, e2, _, _) in itertools.combinations(pl, 2):
            ed = e1 + e2
            r = run_model(s, ed)
            if r is None:
                continue
            fr = frame0 + (r["T"] - res["T"]) if frame0 is not None else None
            sc = score(wants, r["offs"], fr, res["offs"], ofr, r["_sizes"])
            if sc[:2] > max(s1[:2], s2[:2]):
                rows.append((sc, ed, r, fr))
    rows.sort(key=lambda x: (x[0], -len(x[1])), reverse=True)
    if depth >= 3:                       # greedy: the best pairs with one more edit of the pool
        pl = [x for x in singles if x[0][:2] != base_s[:2]][:pool]
        more = []
        for sc0, ed0, _, _ in rows[:10]:
            for s3, e3, _, _ in pl:
                if e3[0] in ed0:
                    continue
                ed = ed0 + e3
                r = run_model(s, ed)
                if r is None:
                    continue
                fr = frame0 + (r["T"] - res["T"]) if frame0 is not None else None
                sc = score(wants, r["offs"], fr, res["offs"], ofr, r["_sizes"])
                if sc[:2] > sc0[:2]:
                    more.append((sc, ed, r, fr))
        rows = sorted(rows + more, key=lambda x: (x[0], -len(x[1])), reverse=True)
    return base_s, rows[:top]


# ================================================================ the source
def mask_text(t):
    """t with comments, string and char literals blanked (same length, newlines kept)."""
    out, i, n = list(t), 0, len(t)
    while i < n:
        c = t[i]
        if t.startswith("//", i):
            j = t.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif t.startswith("/*", i):
            j = t.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if t[k] != "\n":
                    out[k] = " "
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and t[j] != c:
                j += 2 if t[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if t[k] != "\n":
                    out[k] = " "
            i = j + 1
        elif c == "#" and (i == 0 or t[i - 1] == "\n"):         # preprocessor line
            j = t.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


class Source:
    def __init__(self, path):
        self.path = path
        self.text = open(path, errors="replace").read()
        self.m = mask_text(self.text)
        self.lstart = [0] + [k + 1 for k, c in enumerate(self.text) if c == "\n"]

    def line_of(self, pos):
        import bisect
        return bisect.bisect_right(self.lstart, pos)          # 1-based

    def pos_of_line(self, ln):
        return self.lstart[ln - 1]

    def body(self, first_line):
        """(open, close) positions of the function body whose header starts on first_line."""
        p = self.pos_of_line(first_line)
        o = self.m.find("{", p)
        return o, self.match(o)

    def match(self, o):
        d = 0
        for k in range(o, len(self.m)):
            c = self.m[k]
            if c == "{":
                d += 1
            elif c == "}":
                d -= 1
                if d == 0:
                    return k
        return len(self.m) - 1

    def blocks(self, o, c):
        """[(open, close)] of every brace pair inside (o, c), o's pair first."""
        out, st = [(o, c)], []
        for k in range(o + 1, c):
            ch = self.m[k]
            if ch == "{":
                st.append(k)
            elif ch == "}" and st:
                out.append((st.pop(), k))
        return out

    def statements(self, o, c):
        """[(start, end)] of the statements directly in the block (o, c): end is the position after the `;` or `}`."""
        m, out, k = self.m, [], o + 1

        def skip_ws(k):
            while k < c and m[k].isspace():
                k += 1
            return k
        while True:
            k = skip_ws(k)
            if k >= c:
                break
            st, par, br, prev, init = k, 0, 0, "", []
            starts_do = bool(re.match(r"do\b", m[k:k + 3]))
            while k < c:
                ch = m[k]
                if ch == "(":
                    par += 1
                elif ch == ")":
                    par -= 1
                elif ch == "{":
                    init.append(prev in ("=", ",", "(") or par > 0)
                    br += 1
                elif ch == "}":
                    br -= 1
                    was_init = init.pop() if init else False
                    if br == 0 and par == 0 and not was_init:
                        nx = skip_ws(k + 1)
                        word = re.match(r"\w+", m[nx:nx + 8])
                        word = word.group(0) if word else m[nx:nx + 1]
                        if word not in ("else", "catch") and not (starts_do and word == "while") and word != ";":
                            k += 1
                            break
                elif ch == ";" and par == 0 and br == 0:
                    k += 1
                    break
                if not ch.isspace():
                    prev = ch
                k += 1
            out.append((st, k))
        return out

    def ident_at(self, name, a, b):
        return [mm.start() for mm in re.finditer(r"(?<![\w.>:])%s\b" % re.escape(name), self.m[a:b])]


def decl_re(name):
    return re.compile(r"^\s*((?:(?:const|static|volatile|unsigned|signed|struct|class|register)\s+)*"
                      r"[A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?(?:\s*[*&]+\s*|\s+))%s\s*((?:\[[^\]]*\])*)\s*(=|;|\(|\{)"
                      % re.escape(name))


class Decl:
    pass


def find_decl(src, body, name, line):
    """The declaration of `name` in scope at source `line` inside the function body (the latest one before it whose
    block holds the line), or None."""
    o, c = body
    blocks = src.blocks(o, c)
    best = None
    rx = decl_re(name)
    for bo, bc in blocks:
        for st, en in src.statements(bo, bc):
            seg = src.m[st:en]
            mm = rx.match(seg)
            if not mm or seg.lstrip().split()[0] in KEYWORDS:
                continue
            if src.line_of(st) > line or not (bo <= src.pos_of_line(line) <= bc):
                continue
            if best is None or st > best.st:
                d = Decl()
                d.st, d.en, d.block = st, en, (bo, bc)
                d.type = mm.group(1).strip()
                d.dims = mm.group(2)
                d.init = mm.group(3) != ";"
                d.multi = "," in src.m[st + mm.end(3) - 1:en] and mm.group(3) == ";"
                d.name = name
                d.text = src.text[st:en]
                best = d
    return best


def uses_in(src, d):
    """[(statement start, end)] of the statements of d's block after d that mention d.name (block level)."""
    bo, bc = d.block
    out = []
    for st, en in src.statements(bo, bc):
        if st >= d.en and src.ident_at(d.name, st, en):
            out.append((st, en))
    return out


def base_type_builtin(t):
    w = re.findall(r"[A-Za-z_]\w*", t)
    w = [x for x in w if x not in ("const", "static", "volatile", "unsigned", "signed", "register")]
    return (not w) or all(x in BUILTIN for x in w) or t.rstrip().endswith("*")


class Edit:
    """Source edit: a list of (pos, old, new) replacements on the original text; positions do not move lines."""
    def __init__(self, name, what, reps, note=""):
        self.name, self.what, self.reps, self.note = name, what, reps, note

    def apply(self, text):
        out = text
        for pos, old, new in sorted(self.reps, key=lambda r: -r[0]):
            assert out[pos:pos + len(old)] == old, (self.name, pos, old)
            out = out[:pos] + new + out[pos + len(old):]
        return out

    def diff(self, src):
        new = self.apply(src.text).split("\n")
        old = src.text.split("\n")
        lines = []
        for k, (a, b) in enumerate(zip(old, new)):
            if a != b:
                lines.append("    L%d: %s\n      -> %s" % (k + 1, a.strip()[:110], b.strip()[:110]))
        return "\n".join(lines)


def _blank(text):
    return re.sub(r"[^\n]", " ", text)


def lever_narrow(src, d, late=False):
    us = uses_in(src, d)
    if not us:
        return None
    bo, bc = d.block
    sts = src.statements(bo, bc)
    first_use, last_use = us[0], us[-1]
    start = d.st
    reps = []
    if late:
        if d.init or d.multi or first_use[0] == sts[[x[0] for x in sts].index(d.st) + 1][0]:
            return None          # nothing to move (or not movable)
        start = first_use[0]
        reps.append((d.st, src.text[d.st:d.en], _blank(src.text[d.st:d.en])))
    # the range [start, last_use end) must not hold a declaration used after it
    ist = [x[0] for x in sts]
    k0, k1 = ist.index(start) if start in ist else None, ist.index(last_use[0])
    if k0 is None:
        return None
    if k1 == len(sts) - 1:
        return None              # the scope already ends after the last use: a new block ends it at the same place
    for st, en in sts[k0:k1 + 1]:
        seg = src.m[st:en]
        mm = re.match(r"\s*(?:const\s+|static\s+|unsigned\s+|struct\s+)*[A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?[\s*&]+([A-Za-z_]\w*)"
                      r"\s*(?:\[[^\]]*\])*\s*(?:=|;|\(|,)", seg)
        if mm and seg.split()[0] not in KEYWORDS and mm.group(1) != d.name:
            for st2, en2 in sts[k1 + 1:]:
                if src.ident_at(mm.group(1), st2, en2):
                    return None
    reps.append((last_use[1], "", " }"))
    if late:
        reps.append((start, "", "{ " + re.sub(r"\s+", " ", src.text[d.st:d.en]).strip() + " "))
    else:
        reps.append((start, "", "{ "))
    what = "{ %s .. L%d }" % (("move `%s` down to L%d," % (d.name, src.line_of(start))) if late else
                              "`%s` from L%d" % (d.name, src.line_of(d.st)), src.line_of(last_use[1] - 1))
    return Edit(("narrow-late:" if late else "narrow:") + "%s@L%d" % (d.name, src.line_of(d.st)), what, reps)


def lever_widen(src, body, d):
    if d.init or d.multi or not (d.dims or base_type_builtin(d.type)):
        return None
    if d.block == body:
        return None
    o = body[0]
    txt = src.text[d.st:d.en]
    one = re.sub(r"\s+", " ", txt).strip()
    reps = [(d.st, txt, _blank(txt)), (o + 1, "", " " + one)]
    return Edit("widen:%s@L%d" % (d.name, src.line_of(d.st)), "`%s` moved up to the function body (L%d)" % (one, src.line_of(o)), reps)


def lever_share(src, body, dx, dy):
    """Y's uses renamed to X, Y's declaration deleted; X declared in the block that holds both."""
    if dx.type != dy.type or dx.dims != dy.dims or dx.init or dy.init or dx.multi or dy.multi:
        return None
    # the innermost block holding both declarations
    cands = [b for b in src.blocks(*body) if b[0] <= min(dx.st, dy.st) and max(dx.en, dy.en) <= b[1]]
    B = max(cands, key=lambda b: b[0])
    reps = []
    tx, ty = src.text[dx.st:dx.en], src.text[dy.st:dy.en]
    reps.append((dy.st, ty, _blank(ty)))
    for st, en in uses_in(src, dy):
        for p in src.ident_at(dy.name, st, en):
            reps.append((st + p, dy.name, dx.name if len(dx.name) == len(dy.name) else dx.name))
    if dx.block != B:
        reps.append((dx.st, tx, _blank(tx)))
        reps.append((B[0] + 1, "", " " + re.sub(r"\s+", " ", tx).strip()))
    if any(len(new) != len(old) and "\n" in old for _, old, new in reps):
        return None
    ed = Edit("share:%s,%s" % (dx.name, dy.name), "one `%s` for both (L%d and L%d)" % (
        dx.name, src.line_of(dx.st), src.line_of(dy.st)), reps)
    ed.alias = {"_" + dy.name: "_" + dx.name, dy.name: dx.name}
    return ed


def lever_size(src, d, n, esize):
    if not d.dims:
        return None
    dims = re.findall(r"\[([^\]]*)\]", d.dims)
    if len(dims) != 1 or not re.fullmatch(r"\s*\d+\s*", dims[0]):
        return None
    old = int(dims[0])
    if old == 0 or esize % old:
        return None
    per = esize // old
    if n % per:
        return None
    new = n // per
    txt = src.text[d.st:d.en]
    k = txt.index("[" + dims[0] + "]")
    return Edit("size:%s@L%d=%d" % (d.name, src.line_of(d.st), new), "`%s[%d]` -> `%s[%d]`" % (d.name, old, d.name, new),
                [(d.st + k, "[" + dims[0] + "]", "[%d]" % new)], note="changes the array's size")


def combine(eds, why):
    """One Edit doing all of eds (their replacements must not overlap), or None."""
    eds = [e for e in eds if e]
    if not eds:
        return None
    if len(eds) == 1:
        return eds[0]
    reps = [r for e in eds for r in e.reps]
    spans = sorted((p, p + len(o)) for p, o, n in reps)
    if any(spans[k][1] > spans[k + 1][0] for k in range(len(spans) - 1)):
        return None
    ed = Edit(" + ".join(e.name for e in eds), "; ".join(e.what for e in eds), reps,
              note="; ".join(sorted({e.note for e in eds if e.note})))
    ed.alias = {k: v for e in eds for k, v in getattr(e, "alias", {}).items()}
    return ed


def lea_names(asm, func):
    """Listing names that some `lea` of the function takes the address of (address-taken elements: only their scope
    and first definition change their lifetime; an element never lea'd lives from its definitions to its uses)."""
    import tsweep
    out = set()
    for o, t in tsweep.listing_offsets(asm, func).items():
        if t.startswith("lea "):
            out.update(m.group(1) for m in CS.OPSYM.finditer(t))
    return out


def levers_for(src, body, s, model_rows, wants, focus, addr=None):
    """[(Edit, why)] from the model's winning edits (a pair of model edits becomes one source edit doing both) and,
    after them, the catalogue applied to the elements of the wants."""
    E = s["elems"]

    def decl(i):
        L = s["listing"].get(i) or {}
        nm = L.get("lname") or ""
        if nm.startswith("$") or nm.startswith("tv") or not L.get("line"):
            return None
        name = nm.split("$")[0].lstrip("_") if nm.startswith("_") else nm.split("$")[0]
        for cand in (name, "_" + name):
            d = find_decl(src, body, cand, L["line"])
            if d:
                return d
        return None

    def scoped(i):
        return addr is None or (s["listing"].get(i) or {}).get("lname") in addr

    def one(e):
        """The source levers for one model edit."""
        kind, _, arg = e.partition(":")
        if kind == "size":
            nm, _, v = arg.partition("=")
            i = int(nm[1:])
            d = decl(i)
            return [lever_size(src, d, int(v.rstrip("^")), E[i]["size"])] if d else []
        out = []
        if kind in ("conf-", "conf+"):
            for x in arg.split(","):
                d = decl(int(x[1:])) if scoped(int(x[1:])) else None
                if d:
                    out += [lever_narrow(src, d), lever_narrow(src, d, late=True)] if kind == "conf-" else \
                        [lever_widen(src, body, d)]
        return [x for x in out if x]
    out, seen = [], set()

    def add(ed, why):
        if ed:
            key = tuple(sorted((p, o, n) for p, o, n in ed.reps))
            if key not in seen:
                seen.add(key)
                out.append((ed, why))
    for sc, eds, r, fr in model_rows:
        opts = [one(e) for e in eds]
        if not all(opts):
            continue
        for combo in itertools.islice(itertools.product(*opts), 4):
            add(combine(list(combo), " ".join(eds)), " ".join(eds))
    for w in wants:
        if w.kind == "with":
            da, db = decl(w.a), decl(w.b)
            if da and db and da.st != db.st:
                add(lever_share(src, body, da, db) if da.st < db.st else lever_share(src, body, db, da), w.label(s))
    for i in sorted(focus, key=lambda i: -E[i]["size"]):
        d = decl(i) if scoped(i) else None
        if d:
            add(lever_narrow(src, d), "focus")
            add(lever_narrow(src, d, late=True), "focus")
            add(lever_widen(src, body, d), "focus")
    return out


# ================================================================ compiles
def trace_one(src, func, cl, keep):
    """(dict(s, res, bad, asm, frame, func), None) for one traced compile, or (None, reason). Thread-safe: nothing is
    printed (c2slots.trace writes the compiler's output to stdout on an error)."""
    os.makedirs(keep, exist_ok=True)
    info = CS.build_info()
    hooks = [CS.expand(h, info["sbv"], info.get("sym", CS.SYM9)) for h in info["hooks"]]
    try:
        img, hk, rc, out, log = c2trace.run_trace(src, hooks, cl, keep=keep, quiet=True)
    except SystemExit as e:
        return None, "compile failed: %s" % e
    funcs = [F for F in CS.parse(log, info) if c2trace.func_match(func, F["name"] or "")]
    funcs.sort(key=lambda F: (F["name"] or "").strip('"') != func)
    asm = os.path.join(keep, os.path.splitext(os.path.basename(src))[0] + ".asm")
    if not funcs:
        import tsweep
        return None, ("cl rc %d: %s" % (rc, tsweep.cl_errors(out))) if rc else "no stack packing traced"
    F = funcs[0]
    lst = {}
    if os.path.exists(asm):
        try:
            lst = CS.attach_listing(F, CS.listing_slots(asm, F["name"]))
        except KeyError:
            pass
    s = CS.snapshot(F, info, lst)
    res = CS.model_of(s).run(CS.base_order(s), record=True)
    bad = CS.check(s, res)
    return dict(s=s, res=res, bad=bad, asm=asm, frame=our_frame(asm, F["name"]) if os.path.exists(asm) else None,
                func=F["name"]), None


def bdiff_of(asm, func, orig_path):
    import tsweep
    if not orig_path:
        return None
    try:
        return tsweep.bdiff(tsweep.listing_instrs(asm, func), tsweep.orig_instrs(orig_path))
    except Exception:
        return None


def code_change(asm_a, asm_b, func):
    """Instructions that differ between two of our listings (bdiff of one against the other's capstone text)."""
    import tsweep
    try:
        A = tsweep.listing_instrs(asm_a, func)
        B = tsweep.listing_instrs(asm_b, func)
    except Exception:
        return None
    return tsweep.regdiff(A, list(B))


def partition_changes(s0, r0, s1, r1, km0, km1):
    """Elements (base names) whose set of slot-mates differs between two packings (offsets may all shift when the
    frame changes size; which elements share a slot is what an edit really moved)."""
    def groups(s, r, km):
        inv = {j: k for k, j in km.items()}
        by = collections.defaultdict(set)
        for i, o in r["offs"].items():
            if i in inv:
                by[o].add(inv[i])
        return {k: frozenset(g) for g in by.values() for k in g}
    g0, g1 = groups(s0, r0, km0), groups(s1, r1, km1)
    return [CS.ename(s0, km0[k]) for k in km0 if k in g0 and g0.get(k) != g1.get(k)]


# ================================================================ driver
def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(prog="slotwant.py", usage=__doc__)
    ap.add_argument("src")
    ap.add_argument("--func", required=True)
    ap.add_argument("--asm-vs")
    ap.add_argument("--image")
    ap.add_argument("--va")
    ap.add_argument("--want", action="append", default=[])
    ap.add_argument("--diag", action="store_true")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--max-compiles", type=int, default=16)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--min-size", type=int, default=8)
    ap.add_argument("--keep")
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args(argv)
    src_path = os.path.abspath(a.src)
    cl = cl + ["/I" + os.path.dirname(src_path)]
    h = hashlib.sha1((open(src_path, "rb").read() + repr((a.func, cl, os.environ.get("C2_BUILD"))).encode())).hexdigest()[:10]
    wd = a.keep or os.path.join(HERE, "work", "slotwant", "%s-%s" % (os.path.splitext(os.path.basename(src_path))[0], h))
    os.makedirs(wd, exist_ok=True)
    print("# slotwant %s --func %s  (work dir %s)" % (c2trace.c2build.relpath(src_path), a.func, c2trace.c2build.relpath(wd, HERE)))

    base, err = trace_one(src_path, a.func, cl, os.path.join(wd, "base"))
    if base is None:
        sys.exit("slotwant: the base compile: " + err)
    s, res, frame0 = base["s"], base["res"], base["frame"]
    func = base["func"]
    print("# %s: %d elements, %d colours, T=%d, frame %s%s; model %s" % (
        func, len(s["elems"]), len(res["cols"]), res["T"], frame0, " sorted" if res["sort"] else "",
        "exact" if not base["bad"] else "INEXACT (%d differences): what-ifs are weaker" % len(base["bad"])))

    orig_path, ofr, om = a.asm_vs, None, {}
    if a.asm_vs or a.image:
        ins = CS.read_orig(a.asm_vs, a.image, int(a.va, 16) if a.va else None)
        if not a.asm_vs:
            orig_path = os.path.join(wd, "orig.s")
            with open(orig_path, "w") as f:
                for va, mn, ops in ins:
                    f.write("%08x %s %s\n" % (va, mn, ops))
        ofr = frame_alloc([(mn, ops) for _, mn, ops in ins])
        om, oi = orig_keys(s, base["asm"], ins, CS.image_callee_pop(a.image) if a.image else None)
        if not CS.is_x64() and not s["fn"].get("ebp"):
            om = merge_orig(om, orig_keys_ourdepth(s, base["asm"], ins))
        agree = sum(1 for i, (o, n, t) in om.items() if res["offs"].get(i) == o)
        print("# original: frame %s (ours %s, %+d), %d of our elements seen in its code, %d at our offset; bdiff %s" % (
            ofr, frame0, (frame0 or 0) - (ofr or 0), len(om), agree, bdiff_of(base["asm"], func, orig_path)))

    wants = [parse_want(s, w) for w in a.want]
    if not wants and om:
        if ofr is not None and ofr != frame0:
            wants.append(Want("frame", val=ofr, w=4))
        wants += [Want(k, x, y, w=wt) for k, x, y, wt in derive_wants(s, res, om, a.min_size)]
    if a.diag or not wants:
        print("\n## wants derived from the original (name the ones that matter with --want)")
        for w in ([Want("frame", val=ofr)] if ofr else []) + \
                 [Want(k, x, y, w=wt) for k, x, y, wt in derive_wants(s, res, om, a.min_size, cap=60)]:
            ex = ""
            if w.kind in ("with", "apart"):
                ex = "   ours %s / %s, orig %s / %s" % (res["offs"].get(w.a), res["offs"].get(w.b), om[w.a][0], om[w.b][0])
            print("  --want '%s'%s" % (w.label(s), ex))
        if not wants:
            return 0
        if a.diag:
            return 0

    print("\n## wants (%d)" % len(wants))
    focus = set()
    for w in wants:
        sz0 = {i: x["size"] for i, x in s["elems"].items()}
        st = "met" if w.met(res["offs"], frame0, sz0) else "NOT met"
        if w.kind == "ncol":
            st += " (ours %d)" % ncols(res["offs"], sz0).get(w.b, 0)
        print("  %-48s %s" % (w.label(s), st))
        if w.kind == "with" and not w.met(res["offs"], frame0, sz0):
            txt, X, Y, bl = why_with(s, res, w.a, w.b)
            print("      why: " + txt)
            focus.update([X, Y] + list(bl or []))
            cy = (res["log"].get(Y) or {}).get("res")
            if cy is not None:
                focus.update(res["created"][cy]["members"])
        elif w.kind in ("apart", "at"):
            focus.add(w.a)
            if w.b is not None:
                focus.add(w.b)
    for w in wants:
        if w.kind == "ncol":
            for C in res["cols"]:
                if CS.align4(C["size"]) == w.b:
                    focus.update(C["members"])
    if not focus:                     # frame only: every element of the largest colours
        for C in sorted(res["cols"], key=lambda C: -C["size"])[:6]:
            focus.update(C["members"])

    ORIG_PART.update({i: o for i, (o, n, tt) in om.items() if n >= 2 and 2 * n > tt and s["elems"][i]["size"] >= a.min_size
                      and not s["elems"][i]["param"]})
    base_s, rows = search(s, res, wants, frame0, ofr, focus, a.depth, top=a.top)
    print("\n## model: edits that meet more wants (base: weight %d of %d met, %d of %d elements share slots as in the "
          "original, frame %s)" % (base_s[0], sum(w.w for w in wants), base_s[1], len(ORIG_PART), frame0))
    for sc, eds, r, fr in rows:
        shown = [re.sub(r",conf=.*", ",conf=<%d>" % e.count("#"), e) if e.startswith("add:") else e for e in eds]
        print("  %4d  as orig %-3d frame %-6s moved %-3d  %s   (%s)" % (sc[0], sc[1], fr, -sc[3], " ".join(shown), ", ".join(
            CS.ename(s, int(x[1:])) for x in re.findall(r"#\d+", " ".join(shown)))))
        for e in eds:
            if e.startswith("add:bridge"):
                print("        add:bridge = the original has an element we lack: %s bytes, live where the smaller "
                      "one is (its conflicts), joining that colour before the larger one is visited" % e.split(",")[1])
    if not rows:
        print("  no single edit or pair of the model's edits meets more of the wants")

    func_line = func_first_line(base["asm"], func)
    src = Source(src_path)
    if func_line is None:
        print("\n(no source lines in the listing: no levers)")
        return 0
    body = src.body(func_line)
    lev = levers_for(src, body, s, rows, wants, focus, lea_names(base["asm"], func))
    print("\n## levers (%d source edits%s)" % (len(lev), "" if not a.no_verify else ", not compiled"))
    for k, (ed, why) in enumerate(lev):
        print("  [%d] %-32s %s   (from %s)%s" % (k, ed.name, ed.what, why, ("  NOTE " + ed.note) if ed.note else ""))
    if not lev:
        print("  none: the model's edits name elements that are not address-taken (their lifetimes are their "
              "definitions and uses: a statement moved, not a scope) or edits with no catalogue lever (uses, pos)")
    if a.no_verify or not lev:
        return 0

    lev = lev[:a.max_compiles]
    km0 = keymap(s)
    results = [None] * len(lev)

    def job(k):
        ed = lev[k][0]
        d = os.path.join(wd, "v%d" % k)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, os.path.basename(src_path))
        open(p, "w").write(ed.apply(src.text))
        return trace_one(p, a.func, cl, os.path.join(d, "t"))
    with cf.ThreadPoolExecutor(max(1, a.jobs)) as ex:
        futs = {ex.submit(job, k): k for k in range(len(lev))}
        for f in cf.as_completed(futs):
            k = futs[f]
            try:
                results[k] = f.result()
            except Exception as e:           # one variant's failure is that variant's result
                results[k] = (None, "error: %s" % e)
            v, err = results[k]
            sys.stderr.write("  compiled [%d] %s%s\n" % (k, lev[k][0].name, "" if v else ": " + err[:100]))

    print("\n## verified (each lever compiled with the slots trace)")
    table = []
    for k, (ed, why) in enumerate(lev):
        v, err = results[k]
        if v is None:
            table.append(((-1,), k, "did not compile: " + err.splitlines()[0][:120] if err else "failed", None))
            continue
        km = keymap(v["s"])
        if ed.name.startswith("share:"):        # Y is gone: its want is now X's (one element)
            for kk, i in km0.items():
                if kk not in km and ed.alias.get(stem(kk[0])):
                    xs = [k2 for k2 in km if k2[0] == ed.alias[stem(kk[0])] and k2[2] == kk[2] and km[k2] in
                          [km.get(k3) for k3 in km0 if k3[0] == ed.alias[stem(kk[0])]]]
                    if len(xs) == 1:
                        km[kk] = km[xs[0]]
        ws = [w.remap(km0, s, km) for w in wants]
        szv = {i: x["size"] for i, x in v["s"]["elems"].items()}
        met = [w for w in ws if w is not None and w.met(v["res"]["offs"], v["frame"], szv)]
        lost = [w.label(s) for w, w2 in zip(wants, ws) if w2 is None]
        mw = sum(w.w for w in met)
        moved = []
        inv = {}
        for kk, j in km.items():
            inv[j] = kk
        for kk, i in km0.items():
            j = km.get(kk)
            if j is None:
                moved.append("%s gone" % CS.ename(s, i))
            elif res["offs"].get(i) != v["res"]["offs"].get(j):
                moved.append("%s %s->%s" % (CS.ename(s, i), res["offs"].get(i), v["res"]["offs"].get(j)))
        new = [CS.ename(v["s"], j) for j in v["s"]["elems"] if inv.get(j) not in km0]
        regroup = partition_changes(s, res, v["s"], v["res"], km0, km)
        at_o = sum(1 for i, (o, n, tt) in om.items() if n >= 2 and 2 * n > tt and
                   km.get(ikey(s, i)) is not None and v["res"]["offs"].get(km[ikey(s, i)]) == o)
        bd = bdiff_of(v["asm"], func, orig_path)
        cc = code_change(base["asm"], v["asm"], func)
        table.append(((mw, -abs((v["frame"] or 0) - (ofr if ofr is not None else (v["frame"] or 0))),
                       -(bd if bd is not None else 0)), k, dict(
            met=[w.text or w.label(v["s"]) for w in met], mw=mw, frame=v["frame"], bd=bd, cc=cc, moved=moved, new=new,
            lost=lost, exact=not v["bad"], regroup=regroup, at_o=at_o), ed))
    table.sort(key=lambda r: r[0], reverse=True)
    tot = sum(w.w for w in wants)
    b0 = bdiff_of(base["asm"], func, orig_path)
    ngood = sum(1 for i, (o, n, tt) in om.items() if n >= 2 and 2 * n > tt)
    a0 = sum(1 for i, (o, n, tt) in om.items() if n >= 2 and 2 * n > tt and res["offs"].get(i) == o)
    print("  base: weight %d of %d, frame %s, bdiff %s%s" % (base_s[0], tot, frame0, b0,
          (", %d of %d elements at the original's offset" % (a0, ngood)) if om else ""))
    for key, k, info, ed in table:
        if not isinstance(info, dict):
            print("  [%d] %-30s %s" % (k, lev[k][0].name, info))
            continue
        verdict = "WANT" if info["mw"] == tot else ("better" if info["mw"] > base_s[0] else
                                                     "no" if info["mw"] == base_s[0] else "worse")
        print("  [%d] %-30s %-6s weight %d/%d  frame %s  bdiff %s (base %s)  code changed %s  regrouped %d%s" % (
            k, ed.name, verdict, info["mw"], tot, info["frame"], info["bd"], b0, info["cc"], len(info["regroup"]),
            ("" if info["exact"] else "  (model inexact)") + ("  at orig %d/%d" % (info["at_o"], ngood) if om else "")))
        if info["mw"] > base_s[0] or verdict == "WANT":
            print("      met: " + "; ".join(info["met"]))
            print(ed.diff(src))
            if info["regroup"]:
                print("      regrouped (slot-mates changed): " + ", ".join(info["regroup"][:12]) +
                      (" ..." if len(info["regroup"]) > 12 else ""))
            if info["new"]:
                print("      new elements: " + ", ".join(info["new"][:8]))
    json.dump([dict(k=k, edit=lev[k][0].name, what=lev[k][0].what, info=info if isinstance(info, dict) else str(info))
               for key, k, info, ed in table], open(os.path.join(wd, "verified.json"), "w"), indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
