#!/usr/bin/env python3
"""Symbol keys (SYM.key: the number c2 hashes and sorts symbols by): traced, replayed, what-if.  Doc: docs/c2/symkeys.md

usage: symkeys.py keys    <src> --func F [--all] [--brief] [--grep RE] [--phase reader|inline|opt] [-- <cl args>]
       symkeys.py sites   <src> --func F [--line LO-HI] [--pass 1|2] [-- <cl args>]
       symkeys.py events  <src> --func F [-- <cl args>]
       symkeys.py whatif  <src> --func F EDIT [EDIT..] [--show RE] [--batch FILE|- [--compact]] [-- <cl args>]
       symkeys.py levers  <src> --func F --var NAME (--to LO-HI | --above K | --below K | --mod8 R | --hash-above H
                          | --hash-below H) [--line LO-HI] [--match REF.json] [--n 1,2,16 | --max N] [--no-inline]
       symkeys.py levers  <src> --func F --site LINE:A>B [--site ..] [--sites-file F] [--classes frame,tmp,glob]
                          [--n ..|--max N] [--depth 1|2|3] [--pool K]       (several wanted orders at once)
       symkeys.py compare <src> --func F [EDIT..] --vs VARIANT.json      (prediction vs a traced variant)
       symkeys.py export  <src> --func F --out FILE.json
   Every command takes --from FILE.json (an export) instead of <src>: no compile.  --c2 13.10 / 15.00 as usual.

keys     one row per symbol in key order: key, pool class, kind, name (user variable or parameter; `base+off.ty`
         field view; `$T<fekey>` a c1xx temporary, named as in the /FAsc listing; `$tN` / `$fN` unnamed temp / frame
         symbol), the inline expansion it was made in (`Callee#k`, k-th expansion), the line offset (relative to the
         function's first line) current when it was made, the OptHashNode hash (32*key folded to 16 bits) and key mod 8
         (what a pointer's member operands hash by), and the c2 function that made it (a value-numbering temp:
         `/walk`, `/assign` or `/later`, where in the optimiser it was numbered, symkeys.md section 8).  Default: symbols still alive at
         the first OptFuncExpr that are named, field views or user-made; --all adds temporaries and freed symbols.
         The header says `replay: N/N keys reproduced`: the allocator model below reproduces every traced key.
sites    every two-operand commutative sort (OptLeftWeightTree: x87 / integer operand order, x87.md rule 2) of the
         function, operands in the chosen order (1st = evaluated first, for two memory leaves the one loaded with fld),
         with weight SU/count/hash and the key that sets the hash.  Pass 2 (the OptFuncExpr in PreLowerGlobalOpts) is
         the one the code follows.
events   the raw stream: every SymAlloc (class, key, creator), SymFree (keys pushed on which free list), new block of
         32 keys, inline read/splice, pass marker and operand sort.
whatif   replay the stream with edits; prints the keys that change and the pass-2 operand orders that flip.
         --batch FILE|- [--compact]: one edit set per line, all against one load of the trace (stdin is read line by
         line, so it doubles as a persistent mode: each answer ends with `## end`; --compact: one line per set).  EDITs:
           add:CLASS@V[:n]   n more allocations of CLASS (frame | tmp | glob | other) just before V's first one
           sub:CLASS@V[:n]   n fewer: the last n CLASS allocations before V that are still alive there
           add:CLASS@#E[:n]  at event index E (as `events` prints it)
           del:V             V is never allocated (its frees vanish)
           swap:V,W          V's first allocation moves to just before W's (V first referenced later / earlier)
         V is a symbol as `keys` prints it (`_x`, `_v+4.f4`, `_t@Helper#2`, `$T5944+0.f4@Cls::Fn#2`).
levers   every single add/sub edit at every first reference of a named symbol (and every inline expansion start) before
         --var, n from --n / 1..--max; prints those that put --var's key where asked, ranked by the other operand orders
         they flip, with the source construct each one stands for.  --match REF.json ranks instead by how many pass-2
         operand orders still differ from REF (a draft whose code already matches, or any reference trace).
         --site LINE:A>B (repeatable; A, B substrings of two operands as `sites` prints them, LINE an offset, LO-HI or
         *): edit sets (singles, then pairs / triples of the best) that give every such sort the wanted order at once,
         ranked by how many are satisfied (partial solutions included), then other orders flipped.
compare  predicts VARIANT from <src> + EDITs: share of keys and of pass-2 operand orders predicted exactly.

What is modelled (docs/c2/symkeys.md section 1): keys come in blocks of 32 per pool class (frame, global, temp,
label/function, hash), handed out when a class's block is full; freed keys go on a LIFO list per class (13.10: temp and
frame only) and are reused first.  The replay is exact for the traced stream; an edit is exact as far as the source
change makes those allocations and no others (the construct table in symkeys.md section 4 says what each construct
allocates).
"""
import argparse, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace

CLASSES = ("tmp", "frame", "glob", "other", "hash")
# SymAlloc kind -> pool class (SymAlloc 0x10bef1d0, index table 0x10bef395)
KIND_CLASS = {0: "tmp", 1: "tmp", 2: "tmp", 3: "tmp", 6: "tmp", 4: "frame", 5: "frame", 13: "frame",
              7: "glob", 8: "glob", 9: "other", 10: "other", 11: "other", 15: "hash"}
# SymFree: the free list is picked from (head kind - 3) through the table at 0x10bef44c; kind 6: no list
FREE_CLASS = {0: "tmp", 1: "frame", 2: "frame", 3: None, 4: "glob", 5: "glob", 6: "other", 7: "other", 8: "other"}

# 14.00.50727.762 hooks, by role (the parser dispatches on the role, not the position).  Another c2 build gives its
# own in tools/c2builds/<build>.json: "hooks": {"symkeys": {role: hook}} (hex VAs are fine), plus the options below.
HOOKS = {
    "init": "@SymInit@0",                                               # new per-function table
    # kind (al), the line global (current line offset), stack words 0..8 (return addresses for the creator); key, sym
    "alloc": "@SymAlloc@4=eax,g10d0d6fc>0,a0,a1,a2,a3,a4,a5,a6,a7,a8/xeax>0x1c,eax",
    "free": "@SymFree@4+0x33=esi>0x1c,s4",                              # each chain member freed: key, head kind-3
    "uservar": "@SymMakeUserVar@4=eax>4$,eax,eax>0x28/xeax",            # SSR name, SSR, FE key; SYM
    "equiv": "@SymNewEquiv@16=ecx,ecx>8,eax/xeax,eax>0x10,eax>0x20",    # sym, base, offset; member, ty, size
    "splice": "@InlineInlinee@28=s5>0x28>0x18>4$,s5>0x14/xeax",         # splice an inlinee: callee name, call line
    "read": "@RdrReadProc@4/xeax",                                      # reader (nested = an inlinee)
    "opt": "@OptFuncExpr@8=g10d123ac>0>0>4$",                           # operand sort pass (x87.md rule 2)
    "gc": "@SymGarbageCollection@4",
    "pool": "@SymAllocPool@0/xeax>4>0x1c",                              # a block of 32: its first key
    "getinl": "@GetInlinee@28=ebx>4>0x28>0x18>4$,ebx>4>0x14/xeax",      # read the callee: name, call line
    # the operand sort of OptLeftWeightTree (x87.md rule 2): MscMergeSort called from 10bb662d; at exit ebp = the
    # instruction, eax = the sorted operand list: per operand its kind, weight and leaf sym key; for an instruction
    # operand (kind 13, a subtree) the kind and sym key of its first source
    "sort": "@MscMergeSort@8=a0/xebp>4~10ceb3d8$,ebp>0x14,eax>8,eax>0x10,eax>0x18>0x1c,eax>0x28>8,eax>0x28>0x18>0x1c,"
            "eax>0>8,eax>0>0x10,eax>0>0x18>0x1c,eax>0>0x28>8,eax>0>0x28>0x18>0x1c",
    # the same node at OptLeftWeightTree's exit (right after its sort): first and second source of each of the first
    # two operands: kind, sym key, and for a memory source its base leaf's key and displacement
    "sortx": "@OptLeftWeightTree@4/xeax>0x28>0x28>8,eax>0x28>0x28>0x18>0x1c,eax>0x28>0x28>0x2c>0x18>0x1c,"
             "eax>0x28>0x28>0x28,eax>0x28>0x28>0>8,eax>0x28>0x28>0>0x18>0x1c,"
             "eax>0x28>0>0x28>8,eax>0x28>0>0x28>0x18>0x1c,eax>0x28>0>0x28>0x2c>0x18>0x1c,eax>0x28>0>0x28>0x28,"
             "eax>0x28>0>0x28>0>8,eax>0x28>0>0x28>0>0x18>0x1c",
    # the operand list again right after the sort (a probe in OptLeftWeightTree, node in ebp): third and fourth
    # sorted operands (kind, weight, leaf key, first source's kind and key, memory base key, displacement; the fourth
    # without its source and displacement) and the fifth one's kind, so that n-ary sorts (an OPADD of three
    # operands: an address base + index*size + index*size) are seen whole
    "sort3": "10bb6635=ebp>0x28>0>0>8,ebp>0x28>0>0>0x10,ebp>0x28>0>0>0x18>0x1c,ebp>0x28>0>0>0x28>8,"
             "ebp>0x28>0>0>0x28>0x18>0x1c,ebp>0x28>0>0>0x2c>0x18>0x1c,ebp>0x28>0>0>0x28,ebp>0x28>0>0>0>8,"
             "ebp>0x28>0>0>0>0x10,ebp>0x28>0>0>0>0x18>0x1c,ebp>0x28>0>0>0>0x2c>0x18>0x1c,ebp>0x28>0>0>0>0>8",
    # value numbering (docs/c2/symkeys.md section 8): HshBuildTable's walk numbers every expression of the function in
    # tuple order, then every OPASSIGN (HshEnterAssign) after the walk; a hash temp made inside the first is "walk",
    # inside both "assign", any other "later" (assertions, sweeps, loops).  Optional: without them hph stays None.
    "hshwalk": "@HshBuildTable@4/xeax",
    "hshasg": "@HshEnterAssign@8/xeax",
    # optional roles (13.10): "free2" a second chain-free loop (same specs as free), "equivp" a probe right after a
    # member SYM is made where SymNewEquiv is inlined: new sym, base, offset.  13.10's "sort" also logs at entry
    # (a1-a10 after the return address): weight, kind, memory base key, displacement of the first two list members,
    # then each one's second-source base key, for member operands (memory leaves) and subtrees over them
}
HOOKS = c2trace.c2build.hooks("symkeys", HOOKS)
SORT_RET = int(c2trace.c2build.get("symkeys_sort_ret", "10bb6632"), 16)   # return address of that call
# creators that only forward: {function name or start VA: stack words from its return address to its caller's}
SKIP = c2trace.c2build.get("symkeys_skip", {
    "@SymMakeTmpReg@4": 4, "@SymMakeHashVar@4": 4, "@TupMakeTmpReg@4": 4, "@TupMakeTmpVar@4": 4})
# "kind": the free probes log the member's kind byte (13.10: kind 3 -> tmp list, anything else -> frame list)
FREE_BY = c2trace.c2build.get("symkeys_free_by", "headkind")
FREE_KIND = {3: "tmp"}
# pool classes that have no free list (14.00/15.00: hash; 13.10: also globals and others)
NOFREE = set(c2trace.c2build.get("symkeys_nofree", ["hash"]))


def shash(sym, key):
    """The hash a symbol's key gives it as a leaf: 32*key folded for named symbols, 64*key for temporaries and
    the optimiser's value-numbering temps (when they have several definitions; else their definition's weight)."""
    return (key << 6) & 0xffff if sym is not None and sym.cls in ("tmp", "hash") else h16(key)


def h16(key):
    """OptHashNode 0x10bb6813 for a named variable / temp leaf: fold the key to 16 bits, then rotate-ish by 5."""
    e = ((key >> 16) ^ (key & 0xffff)) & 0xffffffff
    return (((e << 5) & 0xffffffff) >> 16 ^ ((e & 0x7ff) << 5)) & 0xffff


class Sym:
    __slots__ = ("idx", "cls", "kind", "key", "ptr", "name", "creator", "line", "inl", "base", "off", "phase",
                 "freed", "user", "ssr", "fe", "hph")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Func:
    def __init__(self):
        self.name = None
        self.events = []      # ('alloc', sym) ('free', [keys], cls) ('pool', firstkey) ('inl+', name, line, id)
        self.syms = []        # ('inl-',) ('mark', what)
        self.optidx = None    # event index of the first OptFuncExpr
        self.line0 = 0        # source line of the function (FUNC+0x44): line offsets are relative to it


def _vals(rest):
    return c2trace._split(rest)


def _str(v):
    return v[1:-1] if v.startswith('"') else None


def parse(text, hooks, img):
    """-> [Func] in compile order."""
    funcs, f = [], None
    stack = []            # open SymAlloc frames (exit order), each a dict
    uvar = []             # open SymMakeUserVar frames: (name, ssr)
    equiv = []            # open SymNewEquiv frames: (base ptr, off)
    inl = []              # open inline expansions: [name, id]
    nread = 0
    sorts, nopt, lastsort = [], 0, None
    pend3 = None          # the operand list of the sort just logged, until the sort3 probe adds operands 3 and 4
    live, bykey = {}, {}  # sym ptr / key -> Sym (current function)
    counts = {}
    names = {}            # va -> creator name cache

    delta = [0]          # runtime base - preferred base (15.00's c2.dll is rebased under wine)
    hsh = [0, 0, "hshwalk" in ROLE.values()]   # depth in HshBuildTable, HshEnterAssign

    def creator(words):
        a = [(int(x, 16) - delta[0]) & 0xffffffff if x != "?" else 0 for x in words]
        k, fn = 0, "?"
        while k < len(a):
            hit = names.get(a[k])
            if hit is None:
                r = c2trace.containing(img, a[k])
                nm = r["symbol"] if r else "%08x" % a[k]
                m = re.match(r"\?(\w+)@@", nm)            # 15.00 C++ names: ?SymMakeTmpReg@@YI... -> SymMakeTmpReg
                hit = names[a[k]] = (m.group(1) if m else nm, nm, "%08x" % r["va"] if r else "")
            fn = hit[0]
            step = SKIP.get(hit[1]) or SKIP.get(hit[2]) or SKIP.get(fn)
            if not step:
                return fn
            k += step
        return fn

    for ln in text.splitlines():
        if ln.startswith("# c2trace base "):
            delta[0] = int(ln.split()[3], 16) - img.base
            continue
        if not (ln.startswith("E ") or ln.startswith("X ")):
            continue
        head, _, rest = ln.partition("|")
        hd = head.split()
        hk = ROLE.get(int(hd[3]))
        v = _vals(rest)
        ent = hd[0] == "E"
        if hk == "init" and ent:
            f = Func()
            funcs.append(f)
            live, bykey, stack, uvar, equiv, inl, nread = {}, {}, [], [], [], [], 0
            sorts, nopt, lastsort = [], 0, None
            pend3 = None
            counts = {}
            continue
        if f is None:
            continue
        ev = f.events
        if hk == "alloc":
            if ent:
                stack.append(dict(kind=int(v[0], 16) & 0xff, cr=creator(v[2:11]), line=int(v[1], 16) & 0xffff if v[1] != "?" else 0))
            elif stack:
                fr = stack.pop()
                s = Sym(idx=len(ev), cls=KIND_CLASS.get(fr["kind"], "tmp"), kind=fr["kind"], key=int(v[0], 16),
                        ptr=v[1], creator=fr["cr"], line=fr["line"], inl="%s#%d" % tuple(inl[-1]) if inl else None,
                        phase="opt" if f.optidx is not None else ("inline" if inl else "reader"))
                if uvar:
                    s.user = True
                if equiv:
                    s.base, s.off = equiv[-1]
                    s.base = live.get(s.base)
                if s.cls == "hash" and hsh[2]:
                    s.hph = "assign" if hsh[0] and hsh[1] else "walk" if hsh[0] else "later"
                live[s.ptr] = s
                bykey[s.key] = s
                ev.append(("alloc", s))
                f.syms.append(s)
        elif hk in ("free", "free2"):
            k = int(v[0], 16)
            if FREE_BY == "kind":
                cls = FREE_KIND.get(int(v[1], 16) & 0xff, "frame") if v[1] != "?" else "frame"
            else:
                cls = FREE_CLASS.get(int(v[1], 16))
            ev.append(("free", k, cls, hd[1]))
            s = bykey.pop(k, None)
            if s is not None:
                s.freed = len(ev) - 1
                live.pop(s.ptr, None)
        elif hk == "uservar":
            if ent:
                uvar.append((_str(v[0]), v[1], v[2] if len(v) > 2 else "?"))
            elif uvar:
                name, ssr, fek = uvar.pop()
                s = live.get(v[0])
                if s is not None and s.name is None:
                    s.name = name or ("$T%d" % int(fek, 16) if fek != "?" else None)
                    s.ssr = ssr
        elif hk == "equiv":
            if ent:
                equiv.append((v[1], int(v[2], 16)))
            elif equiv:
                equiv.pop()
                s = live.get(v[0])
                if s is not None and len(v) > 2 and v[2] != "?":
                    s.fe = "%x/%d" % (int(v[1], 16) & 0xffff, int(v[2], 16))
        elif hk == "equivp":
            sm = live.get(v[0])
            if sm is not None and sm.base is None:
                sm.base, sm.off = live.get(v[1]), int(v[2], 16) if v[2] != "?" else 0
        elif hk in ("splice", "getinl"):
            if ent:
                nm = _str(v[0]) or "?"
                if hk == "getinl" or nm not in counts:
                    counts[nm] = counts.get(nm, 0) + 1
                inl.append([nm, counts[nm]])
                ev.append(("inl+" if hk == "getinl" else "spl+", nm, counts[nm], int(v[1], 16) & 0xffff if v[1] != "?" else 0))
            elif inl:
                inl.pop()
                ev.append(("inl-" if hk == "getinl" else "spl-",))
        elif hk == "read":
            ev.append(("mark", "read" if ent else "read-end"))
        elif hk == "opt":
            if f.name is None:
                f.name = _str(v[0])
                f.line0 = int(v[1], 16) if len(v) > 1 and v[1] != "?" else 0
            if f.optidx is None:
                f.optidx = len(ev)
            nopt += 1
            ev.append(("mark", "OptFuncExpr"))
        elif hk == "gc":
            ev.append(("mark", "GC"))
        elif hk in ("hshwalk", "hshasg"):
            j = 0 if hk == "hshwalk" else 1
            hsh[j] = max(0, hsh[j] + (1 if ent else -1))
            if hk == "hshwalk":
                ev.append(("mark", "HshBuildTable" if ent else "HshBuildTable-end"))
        elif hk == "pool" and not ent:
            ev.append(("pool", int(v[0], 16)))
        elif hk == "sort":
            def num(x):
                return int(x, 16) if x != "?" else None
            if ent:
                # optional entry fields (13.10; the exit has no room, MAXSPEC 12): 1-8 weight, kind, base leaf key,
                # displacement of the first two list members, so that a memory-leaf operand gets its base; 9-10 the
                # base leaf key of each one's second source (used when sortx says that source is a memory operand)
                pre = [[num(x) for x in v[j:j + 4]] + [num(v[9 + k]) if len(v) > 9 + k else None]
                       for k, j in enumerate((1, 5)) if len(v) >= j + 4]
                sorts.append(((int(v[0], 16) - delta[0]) & 0xffffffff if v[0] != "?" else 0, pre))
            elif sorts and sorts[-1][0] == SORT_RET:
                pre = sorts.pop()[1]
                ops = []
                for j in (2, 7):
                    kind, w, key, skind, skey = (num(x) for x in v[j:j + 5])
                    if kind is None:
                        break
                    kind &= 0xff
                    leaf = key if kind in (1, 2) else None
                    sub = skey if kind == 13 and skind is not None and (skind & 0xff) in (1, 2) else None
                    ops.append(dict(kind=kind, w=w, key=leaf, dkey=sub, op=None,
                                    sym=bykey.get(leaf) if leaf is not None else None,
                                    dsym=bykey.get(sub) if sub is not None else None))
                    # a memory leaf (13.10's sort operands are [base+d] leaves): its base from the entry fields,
                    # matched by kind and weight (the sort is stable, so equal ones keep the entry order)
                    for q, (pw, pk, pb, pd, pb2) in enumerate(pre):
                        if pk is not None and pk & 0xff == kind and pw == w:
                            if kind == 6 and pb is not None:
                                ops[-1]["mem"] = (pb, pd, bykey.get(pb))
                            if kind == 13 and pb2 is not None:
                                ops[-1]["b2"] = pb2
                            del pre[q]
                            break
                opn = _str(v[0]) or OPNAMES.get("%x" % (num(v[0]) or 0)) or "op%s" % v[0]
                ev.append(("sort", nopt, opn, (num(v[1]) or 0) & 0xffff, ops))
                lastsort = ops
                pend3 = ops if len(ops) == 2 else None
            elif sorts:
                sorts.pop()
        elif hk == "sort3" and pend3 is not None:
            # the 3rd and 4th operands of the list just sorted (none when the list has two: the reads give '?')
            def num(x):
                return int(x, 16) if x != "?" else None
            vals = [num(x) for x in v] + [None] * 12
            for kind, w, key, skind, skey, mb, md in (vals[0:7], vals[7:11] + [None, None, None]):
                if kind is None or w is None:
                    break
                kind &= 0xff
                if kind not in (1, 2, 6, 7, 8, 9, 13, 3, 4, 5):
                    break
                o = dict(kind=kind, w=w, key=key if kind in (1, 2) else None, dkey=None, op=None,
                         sym=bykey.get(key) if kind in (1, 2) and key is not None else None, dsym=None, nth=True)
                if kind == 13 and skind is not None and (skind & 0xff) in (1, 2) and skey is not None:
                    o["dkey"], o["dsym"] = skey, bykey.get(skey)
                if kind == 6 and mb is not None:
                    o["mem"] = (mb, md, bykey.get(mb))
                pend3.append(o)
            if vals[11] is not None and len(pend3) == 4:
                pend3.append(dict(kind=vals[11] & 0xff, w=None, key=None, dkey=None, op=None, sym=None, dsym=None, nth=True))
            pend3 = None
        elif hk == "sortx" and not ent and lastsort is not None:
            def num(x):
                return int(x, 16) if x != "?" else None
            for o, j in zip(lastsort, (0, 6)):
                if o["kind"] != 13:
                    continue
                sk, key, base, disp, sk2, key2 = (num(x) for x in v[j:j + 6])
                if sk2 is not None and (sk2 & 0xff) in (1, 2) and key2 is not None:
                    o["dkey2"], o["dsym2"] = key2, bykey.get(key2)
                elif sk2 is not None and (sk2 & 0xff) == 6 and o.get("b2") is not None:
                    o["mem2"] = (o["b2"], None, bykey.get(o["b2"]))     # second source [base+d] (13.10)
                sk = sk & 0xff if sk is not None else None
                if sk in (1, 2) and key is not None:
                    o["dkey"], o["dsym"] = key, bykey.get(key)
                elif sk == 6 and base is not None:
                    o["mem"] = (base, disp, bykey.get(base))
            lastsort = None
    for f in funcs:
        name_syms(f)
    return funcs


TYCLS = {1: "i", 2: "u", 3: "p", 4: "p", 5: "f", 6: "b", 7: "b"}


def tyname(ty):
    """TY word (il_sym.md section 7) as a short name: f4 float, f8 double, i4 int, u2, p4 pointer, b12 block."""
    c, n = ty >> 12, ty & 0xfff
    return "%s%d" % (TYCLS.get(c, "t%x_" % c), n)


def name_syms(f):
    """Give every symbol a printable name: user names, `base+off` field views, `$t` temps, inline suffixes;
    unnamed symbols made by the optimiser (after the first OptFuncExpr) are `$o<n>`, the n-th symbol the optimiser
    made: a name that survives an edit before it, where `$t<key>` would not."""
    nopt = 0
    for s in f.syms:
        if s.phase == "opt":
            nopt += 1
        if s.name:
            pass
        elif s.phase == "opt" and not (s.base is not None and s.base.name):
            s.name = "$o%d" % nopt
        elif s.base is not None and s.base.name:
            s.name = "%s+%d" % (s.base.name, s.off)
            if s.fe:
                s.name += "." + tyname(int(s.fe.split("/")[0], 16))
        elif s.cls == "frame":
            s.name = "$f%d" % s.key          # unnamed frame symbol: inliner local sN, promoted temp
        elif s.cls == "glob":
            s.name = "$g%d" % s.key
        else:
            s.name = "$t%d" % s.key
    # field views made before their base got its name (rare): retry once
    for s in f.syms:
        if s.base is not None and s.name.startswith("$") and s.base.name and not s.base.name.startswith("$"):
            s.name = "%s+%d" % (s.base.name, s.off)


def label(s):
    """Unique handle: name, plus @Callee#k for symbols made inside an inline expansion."""
    if s.inl and not re.match(r"\$[tfgo]\d", s.name):
        return "%s@%s" % (s.name, short(s.inl))
    return s.name


OPS = {"0": None, "1": "~", "2": "operator new", "3": "operator delete", "4": "operator=", "G": "operator-",
       "H": "operator+", "D": "operator*", "K": "operator/", "A": "operator[]", "R": "operator()", "Y": "operator+=",
       "Z": "operator-=", "X": "operator*=", "8": "operator==", "9": "operator!=", "M": "operator<", "O": "operator>"}


def short(inl):
    m = re.match(r"\?\?([0-9A-Z])([^@?]+)@", inl)
    if m:
        c, cls = m.group(1), m.group(2)
        n = cls + "::" + (cls if c == "0" else "~" + cls if c == "1" else OPS.get(c, "operator" + c))
        return n + inl[inl.rindex("#"):] if "#" in inl else n
    m = re.match(r"\?([^@?]+)@(?:([^@]+)@)?", inl)
    if m:
        n = m.group(2) + "::" + m.group(1) if m.group(2) else m.group(1)
        return n + inl[inl.rindex("#"):]
    return inl


# ---------------------------------------------------------------- replay

def stream(f):
    """The allocator ops: ('a', cls, sym) / ('f', cls, key), in order."""
    ops = []
    for e in f.events:
        if e[0] == "alloc":
            ops.append(["a", e[1].cls, e[1]])
        elif e[0] == "free":
            ops.append(["f", e[2], e[1]])
    return ops


def replay(ops):
    """SymInit + SymAlloc + SymFree.  Keys for the 'a' ops (list, same order); free keys are renamed to the keys
    the replay gave the symbol they belonged to."""
    nxt = 0
    cur = {}
    for c in ("frame", "glob", "tmp", "other"):     # SymInit: four blocks in this order
        cur[c] = [nxt, nxt + 32]
        nxt += 32
    cur["hash"] = None
    free = {c: [] for c in CLASSES}
    out = []
    owner = {}       # traced key of a live sym -> replayed key (for frees)
    for op in ops:
        if op[0] == "a":
            c = op[1]
            if c not in NOFREE and free[c]:
                k = free[c].pop()
            else:
                p = cur[c]
                if p is None or p[0] == p[1]:
                    p = cur[c] = [nxt, nxt + 32]
                    nxt += 32
                k = p[0]
                p[0] += 1
            out.append(k)
            if op[2] is not None:
                owner[op[2].key] = k
        else:
            c, k = op[1], op[2]
            k = owner.pop(k, k) if op[2] in owner else k
            if c is not None:
                free[c].append(k)
    return out


class FastKeys:
    """Keys of a few symbols (deps) after edits, replayed from a snapshot of the unedited allocator taken just
    before the first op the edits change, and only up to the last dep's allocation: what `levers --site` evaluates
    thousands of times.  The result equals keys_after() for those symbols."""

    def __init__(self, f, deps, every=256):
        self.f, self.ops = f, stream(f)
        self.deps = set(deps)
        idx = [i for i, op in enumerate(self.ops) if op[0] == "a" and op[2] is not None and id(op[2]) in self.deps]
        self.stop = max(idx) if idx else len(self.ops) - 1
        self.snaps = []
        st = self._init()
        for i, op in enumerate(self.ops[:self.stop + 1]):
            if i % every == 0:
                self.snaps.append((i, self._copy(st)))
            self._step(st, op)

    @staticmethod
    def _init():
        nxt, cur = 0, {}
        for c in ("frame", "glob", "tmp", "other"):
            cur[c] = [nxt, nxt + 32]
            nxt += 32
        cur["hash"] = None
        return dict(nxt=nxt, cur=cur, free={c: [] for c in CLASSES}, owner={})

    @staticmethod
    def _copy(st):
        return dict(nxt=st["nxt"], cur={c: (list(v) if v else v) for c, v in st["cur"].items()},
                    free={c: list(v) for c, v in st["free"].items()}, owner={})

    @staticmethod
    def _step(st, op):
        if op[0] == "a":
            c = op[1]
            if c not in NOFREE and st["free"][c]:
                k = st["free"][c].pop()
            else:
                p = st["cur"][c]
                if p is None or p[0] == p[1]:
                    p = st["cur"][c] = [st["nxt"], st["nxt"] + 32]
                    st["nxt"] += 32
                k = p[0]
                p[0] += 1
            if op[2] is not None:
                st["owner"][op[2].key] = k
            return k
        c, k = op[1], op[2]
        k = st["owner"].pop(k, k)
        if c is not None:
            st["free"][c].append(k)
        return None

    def _pos(self, name):
        if not hasattr(self, "_posmap"):
            self._posmap = {}
            for i, op in enumerate(self.ops):
                if op[0] == "a" and op[2] is not None:
                    self._posmap.setdefault(id(op[2]), i)
        try:
            return self._posmap.get(id(find(self.f, name)))
        except SystemExit:
            return None

    def keys(self, edits):
        """{id(sym): key} for the deps (unmoved deps are left out: callers default to sym.key)."""
        ops = self.ops
        m = re.match(r"^add:(frame|tmp|glob|other|hash)@([^#].*?)(?::(\d+))?$", edits[0]) if len(edits) == 1 else None
        if m and self._pos(m.group(2)) is not None:
            # one insertion: the edited stream is the base with n new allocations at the anchor (no O(n) rebuild)
            p = self._pos(m.group(2))
            ed = ops[:0]
            tail = [["a", m.group(1), None] for _ in range(int(m.group(3) or 1))]
        else:
            ed = apply_edits(self.f, edits)
            p = 0
            n = min(len(ed), len(ops))
            while p < n and ed[p] is ops[p]:
                p += 1
            tail = None
        k = max(i for i, (at, _) in enumerate(self.snaps) if at <= p) if self.snaps else 0
        at, snap = self.snaps[k]
        st = self._copy(snap)
        # the prefix before `at` is unedited: frees there name their own (traced = replayed) keys
        out, left = {}, set(self.deps)
        seq = (ops[at:p] + tail + ops[p:]) if tail is not None else ed[at:]
        for op in seq:
            key = self._step(st, op)
            if key is not None and op[2] is not None and id(op[2]) in left:
                out[id(op[2])] = key
                left.discard(id(op[2]))
                if not left:
                    break
        return out


def point_classes(f):
    """For pruning levers: per op index of the stream, (ops of each class so far, blocks taken so far) in the
    unedited replay.  Two insertion points with the same counts for class C (no C op and no new block between
    them) give identical keys for every symbol after an add/sub of C, so only the first one is evaluated."""
    if getattr(f, "_ptcls", None) is not None:
        return f._ptcls
    ops = stream(f)
    cnt = {c: 0 for c in CLASSES}
    nb = 0
    st = FastKeys._init()
    out = []
    for op in ops:
        out.append((dict(cnt), nb))
        before = st["nxt"]
        FastKeys._step(st, op)
        nb += st["nxt"] != before
        cnt[op[1]] = cnt.get(op[1], 0) + 1 if op[1] is not None else cnt.get(op[1], 0)
    out.append((dict(cnt), nb))
    f._ptcls = out
    return out


def prune_points(f, pts, cls):
    """pts (anchor, where, op index) reduced to one per equivalence class for edits of class cls; the others are
    reported as 'same as'.  Points without an op index (just before the target) are kept."""
    pc = point_classes(f)
    keep, seen = [], {}
    for p in pts:
        if p[2] is None or p[2] >= len(pc):
            keep.append(p)
            continue
        c, nb = pc[p[2]]
        sig = (c.get(cls, 0), nb)
        if sig in seen:
            seen[sig][1] += 1
            continue
        seen[sig] = [p, 0]
        keep.append(p)
    return keep, {id(v[0]): v[1] for v in seen.values()}


def check_replay(f):
    ops = stream(f)
    got = replay(ops)
    want = [op[2].key for op in ops if op[0] == "a"]
    return sum(1 for a, b in zip(got, want) if a == b), len(want)


# ---------------------------------------------------------------- reporting

def sort_syms(f):
    """ids of the symbols an operand sort depends on (leaf, first / second source, memory base)."""
    if getattr(f, "_sortsyms", None) is None:
        ids = set()
        for e in f.events:
            if e[0] == "sort":
                for o in e[4]:
                    for k in ("sym", "dsym", "dsym2"):
                        if o.get(k) is not None:
                            ids.add(id(o[k]))
                    for k in ("mem", "mem2"):
                        if o.get(k) and o[k][2] is not None:
                            ids.add(id(o[k][2]))
        f._sortsyms = ids
    return f._sortsyms


def visible(f, s, show_all):
    if f.optidx is not None and s.idx > f.optidx:
        # made by the optimiser: shown (and followed by whatif / compare) when an operand sort depends on it
        return s.phase == "opt" and id(s) in sort_syms(f)
    if s.freed is not None and (f.optidx is None or s.freed < f.optidx):
        return show_all
    if show_all:
        return True
    return s.cls in ("frame", "glob") or s.user


def print_brief(f, a):
    """One line: the user-visible symbols in allocation order, name:key, plus per-class allocation counts."""
    rows = [s for s in f.syms if visible(f, s, False)]
    n = {}
    for s in f.syms:
        if f.optidx is None or s.idx < f.optidx:
            n[s.cls] = n.get(s.cls, 0) + 1
    print("%-34s %s   [%s]" % (f.name, " ".join("%s:%d" % (label(s), s.key) for s in rows),
                               " ".join("%s %d" % (c, n[c]) for c in CLASSES if c in n)))


def print_keys(f, a):
    if a.brief:
        return print_brief(f, a)
    ok, n = check_replay(f)
    print("#### %s  (%d symbols allocated before OptFuncExpr, %d events; replay: %d/%d keys reproduced)" % (
        f.name, sum(1 for s in f.syms if f.optidx is None or s.idx < f.optidx), len(f.events), ok, n))
    blocks = [e[1] for e in f.events if e[0] == "pool"]
    print("# blocks of 32 (first key, in the order taken): %s" % " ".join(str(b) for b in blocks[:64]) +
          (" ..." if len(blocks) > 64 else ""))
    print("%5s %-5s %-4s %-28s %-24s %5s %6s %4s %s" % ("key", "class", "kind", "symbol", "inline", "line", "hash",
                                                       "mod8", "made by"))
    rx = re.compile(a.grep) if a.grep else None
    rows = [s for s in f.syms if (visible(f, s, a.all) or (a.phase == "opt" and s.phase == "opt" and a.all))
            and (a.phase in (None, "all") or s.phase == a.phase)]
    for s in sorted(rows, key=lambda s: (s.key, s.idx)):
        line = "%5d %-5s %-4d %-28s %-24s %5d %6s %4d %s%s%s" % (
            s.key, s.cls, s.kind, s.name, short(s.inl) if s.inl else "", s.line, "%04x" % shash(s, s.key), s.key % 8,
            s.creator + ("/" + s.hph if s.hph else ""),
            " (freed)" if s.freed is not None and (f.optidx is None or s.freed < f.optidx) else "",
            " (optimiser; in operand sorts)" if s.phase == "opt" and id(s) in sort_syms(f) else
            " (optimiser)" if s.phase == "opt" else "")
        if rx is None or rx.search(line):
            print(line)


def print_events(f):
    print("#### %s" % f.name)
    for i, e in enumerate(f.events):
        if e[0] == "alloc":
            s = e[1]
            print("%6d alloc %-5s key %5d  %-26s %s line %d" % (i, s.cls, s.key, label(s), s.creator, s.line))
        elif e[0] == "free":
            print("%6d free  %-5s key %5d" % (i, e[2] or "-", e[1]))
        elif e[0] == "sort":
            print("%6d sort  pass %d line %d %s: %s" % (i, e[1], e[3], e[2], " | ".join(opdesc(o) for o in e[4])))
        elif e[0] == "pool":
            print("%6d block %d..%d" % (i, e[1], e[1] + 31))
        elif e[0] in ("inl+", "spl+"):
            print("%6d %s %s (call line %d)" % (i, "read inlinee" if e[0] == "inl+" else "splice", short("%s#%d" % (e[1], e[2])), e[3]))
        elif e[0] in ("inl-", "spl-"):
            print("%6d %s end" % (i, "read inlinee" if e[0] == "inl-" else "splice"))
        else:
            print("%6d -- %s" % (i, e[1]))


LEAFKIND = {1: "reg", 2: "sym", 3: "&", 4: "code", 5: "ea", 6: "mem", 7: "iconst", 8: "iconst", 9: "fconst"}


def opdesc(o):
    w = o["w"] or 0
    t = "SU%d n%d h%04x" % (w >> 24, (w >> 16) & 0xff, w & 0xffff)
    if o["kind"] in (1, 2):
        s, d = o["sym"], o["dsym"]
        if s is not None and (s.cls != "tmp" or s.phase == "opt"):
            what = "%s (key %d)" % (label(s), s.key)
        else:
            what = "t%d" % o["key"]
    elif o["kind"] == 13:
        d, m = o["dsym"], o.get("mem")
        if m:
            b = m[2]
            what = "([%s+%d] ..)" % ("%s key %d" % (label(b), b.key) if b is not None and (b.cls != "tmp" or b.phase == "opt") else
                                     "t%d" % m[0], m[1] or 0)
        else:
            what = "(%s ..)" % ("%s key %d" % (label(d), d.key) if d is not None and (d.cls != "tmp" or d.phase == "opt") else
                                "t%d" % o["dkey"] if o["dkey"] is not None else "?")
        m2 = o.get("mem2")
        if m2 and m2[2] is not None and m2[2].cls != "tmp":
            what = what[:-4] + " [%s key %d+..] ..)" % (label(m2[2]), m2[2].key)
    elif o["kind"] == 6 and o.get("mem"):
        m = o["mem"]
        b = m[2]
        what = "[%s+%d]" % ("%s key %d" % (label(b), b.key) if b is not None and (b.cls != "tmp" or b.phase == "opt") else "t%d" % m[0],
                            m[1] or 0)
    else:
        what = LEAFKIND.get(o["kind"], "kind%d" % o["kind"])
    return "%-46s %s" % (what, t)


def print_sites(f, a):
    lo, hi = (0, 1 << 30)
    if a.line:
        p = a.line.split("-")
        lo, hi = int(p[0]), int(p[-1])
    print("#### %s: commutative operand sorts (OptLeftWeightTree), first operand = evaluated first / loaded" % f.name)
    for e in f.events:
        if e[0] != "sort" or not lo <= e[3] <= hi or (a.pass_ and e[1] != a.pass_):
            continue
        ops = e[4]
        if a.leaves and not all(o["kind"] in (1, 2, 6) for o in ops):
            continue
        tie = len(ops) > 1 and ops[0]["w"] == ops[1]["w"]
        print("pass %d line %4d %-10s 1st %s" % (e[1], e[3], e[2], opdesc(ops[0]) if ops else "-"))
        for k, o in enumerate(ops[1:]):
            print("%31s %s %s%s" % ("", ("2nd", "3rd", "4th", "5th")[min(k, 3)], opdesc(o) if o["w"] is not None else
                                    "%s (5th or later: not logged)" % LEAFKIND.get(o["kind"], "kind%d" % o["kind"]),
                                    "   (tie: source order)" if tie and k == 0 else ""))


# ---------------------------------------------------------------- what-if

def find(f, name):
    hits = [s for s in f.syms if label(s) == name or s.name == name]
    if not hits:
        raise SystemExit("no symbol %s (names are as `keys` prints them)" % name)
    return hits[0]


def apply_edits(f, edits):
    """-> list of ops (with ('a', cls, None) for inserted symbols) after the edits."""
    ops = stream(f)
    pos = {}         # sym -> op index
    for i, op in enumerate(ops):
        if op[0] == "a":
            pos[id(op[2])] = i
    ins = []         # (op index, [ops])
    drop = set()
    for e in edits:
        e = re.sub(r"^add:", "+", re.sub(r"^sub:", "-", re.sub(r"^del:", "-", e)))
        m = re.match(r"([+-])(frame|tmp|glob|other|hash)@(#?)(.+?)(?::(\d+))?$", e)
        if m:
            sign, cls, ev, where, n = m.group(1), m.group(2), m.group(3), m.group(4), int(m.group(5) or 1)
            if ev:
                ei = int(where)
                at = sum(1 for x in f.events[:ei] if x[0] in ("alloc", "free"))
            else:
                at = pos[id(find(f, where))]
            if sign == "+":
                ins.append((at, [["a", cls, None] for _ in range(n)]))
            else:
                # drop the last n allocations of cls before `at` that are not freed before `at`
                freed_before = set()
                for op in ops[:at]:
                    if op[0] == "f":
                        freed_before.add(op[2])
                k = 0
                for i in range(at - 1, -1, -1):
                    op = ops[i]
                    if op[0] == "a" and op[1] == cls and op[2] is not None and op[2].key not in freed_before:
                        drop.add(i)
                        k += 1
                        if k == n:
                            break
            continue
        m = re.match(r"swap:(.+),(.+)$", e)
        if m:
            # V's first allocation is moved to just before W's (V first referenced earlier / later)
            sv, sw = find(f, m.group(1)), find(f, m.group(2))
            iv, iw = pos[id(sv)], pos[id(sw)]
            drop.add(iv)
            ins.append((iw, [ops[iv]]))
            continue
        m = re.match(r"-(.+)$", e)
        if m:
            s = find(f, m.group(1))
            drop.add(pos[id(s)])
            continue
        raise SystemExit("bad edit %r" % e)
    moved_ops = {id(op) for _, x in ins for op in x if op[2] is not None}
    dropped_keys = {ops[i][2].key for i in drop if id(ops[i]) not in moved_ops}
    out = []
    insd = {}
    for at, x in ins:
        insd.setdefault(at, []).extend(x)
    for i, op in enumerate(ops):
        out.extend(insd.get(i, []))
        if i in drop:
            continue
        if op[0] == "f" and op[2] in dropped_keys:
            continue          # (approximation: a key reused later by another sym is not dropped twice)
        out.append(op)
    out.extend(insd.get(len(ops), []))
    return out


def keys_after(f, ops):
    got = replay(ops)
    res, j = {}, 0
    for op in ops:
        if op[0] == "a":
            if op[2] is not None:
                res[id(op[2])] = got[j]
            j += 1
    return res


def interesting(f, s):
    return visible(f, s, False)


def whatif(f, edits, show=None):
    base = {id(s): s.key for s in f.syms}
    new = keys_after(f, apply_edits(f, edits))
    rx = re.compile(show) if show else None
    moved = []
    for s in f.syms:
        if (not interesting(f, s) and not (rx and rx.search(label(s)))) or id(s) not in new:
            continue
        if new[id(s)] != base[id(s)] or (rx and rx.search(label(s))):
            moved.append((s, base[id(s)], new[id(s)]))
    return moved


def _g(sym, key):
    """The part of an operand's hash that depends on this symbol's key (OptHashNode 0x10bb6720): 32*key folded to
    16 bits for a named variable or field view, 64*key for a temporary with several definitions; None if unknown."""
    if sym is None:
        return None
    if sym.cls in ("frame", "glob"):
        return h16(key)
    if sym.cls in ("tmp", "hash"):
        return (key << 6) & 0xffff
    return None


def _tmp_ok(sym, lo, shift=0):
    """A temporary hashes by its key only when it has no single definition (else it takes its def's weight):
    accept it when the recorded hash is (64*key << shift) plus an opcode constant."""
    if sym is None or sym.cls in ("frame", "glob"):
        return True
    return ((lo - ((_g(sym, sym.key) or 0) << shift)) & 0xffff) < 0x100 if shift == 0 else True


# memory bases whose hash is their key: named variables and field views (32*key), and the optimiser's
# value-numbering temporaries (hash pool class, HshInsertEntry: 64*key, so through the `<< 8` only key & 3 counts;
# Fallout 3 0x00806ab0: [$o key 8554] h8025, key 8557 h4025).  A reader temporary as a base may hash by its single
# definition instead and is taken as fixed.
KEYED_BASE = ("frame", "glob", "hash")


def op_weight(o, newkey):
    """The operand's sort weight with the keys in `newkey` (id(sym) -> key); the recorded weight when the
    operand's hash does not depend on keys the trace can follow (a deeper subtree, a single-def temporary)."""
    w = o["w"]
    if w is None:
        return None
    lo = w & 0xffff
    if o["kind"] in (1, 2):
        s = o["sym"]
        if s is None or _g(s, s.key) is None or (s.cls not in ("frame", "glob") and lo != _g(s, s.key)):
            return w
        return (w & ~0xffff) | (lo - _g(s, s.key) + _g(s, newkey.get(id(s), s.key))) & 0xffff
    if o["kind"] in (6, 13):
        m, m2, d2 = o.get("mem"), o.get("mem2"), 0
        if m2 and m2[2] is not None and m2[2].cls in KEYED_BASE:
            b = m2[2]          # a second source [base+d]: its hash enters the subtree's shifted by 1 (OptHashNode)
            d2 = ((_g(b, newkey.get(id(b), b.key)) << 8) - (_g(b, b.key) << 8)) << 1
        if m and m[2] is not None and m[2].cls in KEYED_BASE:
            b = m[2]
            d = ((_g(b, newkey.get(id(b), b.key)) << 8) - (_g(b, b.key) << 8))
            return (w & ~0xffff) | (lo + d + d2) & 0xffff
        if o["kind"] == 6:
            return w
        s, s2 = o["dsym"], o.get("dsym2")
        if s is not None and _g(s, s.key) is not None and _tmp_ok(s, lo) and s2 is None:
            lo = lo - _g(s, s.key) + _g(s, newkey.get(id(s), s.key))
        elif s is not None and s.cls in ("frame", "glob") and _g(s, s.key) is not None:
            lo = lo - _g(s, s.key) + _g(s, newkey.get(id(s), s.key))
        if s2 is not None and s2.cls in ("frame", "glob"):
            lo = lo - (_g(s2, s2.key) << 1) + (_g(s2, newkey.get(id(s2), s2.key)) << 1)
        return (w & ~0xffff) | (lo + d2) & 0xffff
    return w


def flips(f, newkey, pass_=None, lines=None):
    """Sort decisions (two-operand commutative nodes) whose order changes with the new keys:
    [(event, 'flip' | 'tie')]."""
    out = []
    for e in f.events:
        if e[0] != "sort" or len(e[4]) < 2 or (pass_ and e[1] != pass_):
            continue
        if lines and not lines[0] <= e[3] <= lines[1]:
            continue
        if len(e[4]) > 2:
            how = nary_change(e[4], newkey)
            if how:
                out.append((e, how))
            continue
        a, b = e[4]
        if a["w"] is None or b["w"] is None:
            continue
        wa, wb = op_weight(a, newkey), op_weight(b, newkey)
        if wa == a["w"] and wb == b["w"]:
            continue
        if wa < wb:
            out.append((e, "flip"))
        elif wa == wb and a["w"] != b["w"]:
            out.append((e, "tie"))
    return out


def nary_order(ops, newkey):
    """The order MscMergeSort gives an n-operand list with the new keys: weight descending, stable. The input
    order of the list is not traced, only the sorted one; operands whose weights were equal keep their traced
    order (a stable sort leaves them as the input had them), which is what this does.  None when an operand's
    weight is unknown (a fifth operand: the probe stops at four)."""
    if any(o["w"] is None for o in ops):
        return None
    ws = [op_weight(o, newkey) for o in ops]
    return sorted(range(len(ops)), key=lambda i: -ws[i]), ws


def nary_change(ops, newkey):
    """'flip' when the predicted order of an n-ary sort differs from the traced one, 'tie' when two operands whose
    weights differed become equal (their order then depends on the untraced input order), else None."""
    r = nary_order(ops, newkey)
    if r is None:
        return None
    order, ws = r
    if order != list(range(len(ops))):
        return "flip"
    for i in range(len(ops) - 1):
        if ws[i] == ws[i + 1] and ops[i]["w"] != ops[i + 1]["w"]:
            return "tie"
    return None


def print_flips(fl, n=20, newkey=None):
    for e, how in fl[:n]:
        if len(e[4]) > 2 and newkey is not None:
            r = nary_order(e[4], newkey)
            print("    %s pass %d line %d %s (%d operands): was %s; now %s" % (
                how, e[1], e[3], e[2], len(e[4]), " / ".join(opdesc(o).split("  ")[0] for o in e[4]),
                " / ".join(opdesc(e[4][i]).split("  ")[0] for i in r[0]) if r else "?"))
            continue
        a, b = e[4][:2]
        print("    %s pass %d line %d %s: was 1st %s / 2nd %s%s" % (how, e[1], e[3], e[2], opdesc(a).split("  ")[0],
                                                               opdesc(b).split("  ")[0],
                                                               " / %d more (n-ary)" % (len(e[4]) - 2) if len(e[4]) > 2 else ""))
    if len(fl) > n:
        print("    ... %d more" % (len(fl) - n))


def print_whatif(f, a):
    newkey = keys_after(f, apply_edits(f, a.edits))
    moved = whatif(f, a.edits, a.show)
    print("#### %s  edits: %s" % (f.name, " ".join(a.edits)))
    nm = sum(1 for _, k0, k1 in moved if k0 != k1)
    print("# %d user-visible symbols change key" % nm)
    for s, k0, k1 in sorted(moved, key=lambda t: t[1]):
        print("  %-30s %5d -> %5d   hash %04x -> %04x  mod8 %d -> %d%s" % (
            label(s), k0, k1, shash(s, k0), shash(s, k1), k0 % 8, k1 % 8, "" if k0 != k1 else "  (unchanged)"))
    fl = flips(f, newkey, a.pass_ or 2)
    print("# %d operand orders flip (pass %d sorts; weights recomputed as in x87.md rule 2)" % (len(fl), a.pass_ or 2))
    print_flips(fl, 40, newkey)


def whatif_batch(f, a):
    """Many edit sets against one loaded trace: one per line of --batch FILE (or stdin, read line by line, so a
    driving process can keep this running and send sets as it goes: each answer ends with `## end`).  Blank lines
    and lines starting with # are skipped."""
    src = sys.stdin if a.batch == "-" else open(a.batch)
    k = 0
    for ln in src:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        k += 1
        a.edits = ln.split()
        try:
            if a.compact:
                newkey = keys_after(f, apply_edits(f, a.edits))
                moved = sum(1 for _, k0, k1 in whatif(f, a.edits) if k0 != k1)
                fl = flips(f, newkey, a.pass_ or 2)
                print("set %d: %s | %d keys move, %d orders flip%s" % (k, ln, moved, len(fl), "".join(
                    " | line %d %s %s" % (e[3], e[2], how) for e, how in fl[:8]) + (" ..." if len(fl) > 8 else "")))
            else:
                print("## set %d: %s" % (k, ln))
                print_whatif(f, a)
        except SystemExit as x:
            print("set %d: %s | error: %s" % (k, ln, x))
        print("## end", flush=True) if not a.compact else sys.stdout.flush()


# ---------------------------------------------------------------- levers

def target_ok(a, k):
    if a.to:
        lo, hi = (int(x) for x in a.to.split("-"))
        return lo <= k <= hi
    if a.above is not None:
        return k > a.above
    if a.below is not None:
        return k < a.below
    if a.mod8 is not None:
        return k % 8 == a.mod8
    if a.hash_above is not None:
        return h16(k) > a.hash_above
    if a.hash_below is not None:
        return h16(k) < a.hash_below
    return True


def resolve_ref(f, v):
    """--above/--below/--hash-* may name a symbol: its current key (hash)."""
    return v


def candidate_points(f, target, inline_too=True, opt_too=False):
    """Insertion points before the target's allocation, as (edit anchor, description, op index):
    the first allocation of each named symbol of the function itself (reader order), the start of each inline
    expansion (reading the callee), and, inside expansions, the first named local of each one."""
    pts, seen, opi = [], set(), 0
    for i, e in enumerate(f.events):
        if e[0] == "alloc" and e[1] is target:
            break
        if e[0] == "alloc":
            s = e[1]
            if s.user and s.cls in ("frame", "glob") and (s.inl is None or inline_too) and not s.name.startswith("$"):
                k = ("v", label(s))
                if k not in seen:
                    seen.add(k)
                    pts.append(("@" + label(s), "before the first reference of %s" % label(s), opi))
        elif e[0] == "inl+" and inline_too:
            pts.append(("@#%d" % i, "at the start of inline %s" % short("%s#%d" % (e[1], e[2])), opi))
        if e[0] == "alloc" and opt_too and e[1].phase == "opt" and e[1].cls == "hash":
            # the optimiser's value-numbering temps: one point per source line and phase (walk / assign / later)
            k = ("o", e[1].line, e[1].hph)
            if k not in seen:
                seen.add(k)
                pts.append(("@" + label(e[1]), "in the optimiser, at the first value-numbering temp of line %d%s"
                            % (e[1].line, HPH_WHERE.get(e[1].hph, "")), opi))
        if e[0] in ("alloc", "free"):
            opi += 1
    return pts


CONSTRUCT = {
    "frame": "one more frame symbol: a referenced local (even a dead `int u = 0;`), a reference or pointer local, "
             "a struct field touched for the first time, or (in an inline helper) one more local / by-value parameter",
    "tmp": "one more reader temporary: one more operation or value (call result, address, conversion) in this "
           "statement or before it",
    "glob": "one more global (or string literal, static local) referenced for the first time",
    "other": "one more label / call target referenced for the first time",
    "hash": "one more optimiser value-numbering temporary at that point: one more distinct value in that statement "
            "(code-neutral, all builds, symkeys.md 8.2: one more pair of parentheses around a float subexpression "
            "that is not a bare variable or load, `((e))` +2; a `(float)` cast of a float expression; a float sum "
            "through a `double` local (+1, 13.10 +3); a `bool` flag for a float compare: +1 here, +2 assignment "
            "entries after the walk, +1 later); repeated spellings of one value add nothing",
}
UNCONSTRUCT = {
    "frame": "one frame symbol fewer: reuse an existing local instead of a new one, drop a dead initialised local or "
             "a reference local",
    "tmp": "one reader temporary fewer: one operation / value fewer (fold a computation, pass a variable instead of an "
           "expression)",
    "glob": "one global fewer referenced before here",
    "other": "one label / call target fewer before here",
    "hash": "one optimiser value-numbering temporary fewer before here: one distinct value fewer (code-neutral: drop "
            "a redundant pair of parentheses around a product in a sum, `(a * b) + c` -> `a * b + c`; spell an "
            "abs as `(float)fabs(x)` instead of an inline `float(fabs(x))` helper: -2 on 14.00/15.00, -1 on 13.10; "
            "or make it the same expression as an earlier one)",
}
# where in the optimiser a hash point sits (Sym.hph, from the hshwalk / hshasg hooks)
HPH_WHERE = {"walk": " (HshBuildTable's walk: the value is numbered at its first occurrence in tuple order)",
             "assign": " (assignment entries, numbered after the whole walk: one per assignment to a variable)",
             "later": " (made after HshBuildTable: assertions on branch arms, propagation sweeps, loops)"}


def _prefix_key(ops, at, ins_cls, n, sign, tidx):
    """Replay ops[:tidx+1] with n allocations of ins_cls inserted (sign '+') or the last n live ones removed
    (sign '-') at op index `at`; -> the key the op at tidx gets (None if the edit is impossible)."""
    if sign == "+":
        e = ops[:at] + [["a", ins_cls, None]] * n + ops[at:tidx + 1]
    else:
        freed = set(op[2] for op in ops[:at] if op[0] == "f")
        drop, k = set(), 0
        for i in range(at - 1, -1, -1):
            op = ops[i]
            if op[0] == "a" and op[1] == ins_cls and op[2] is not None and op[2].key not in freed:
                drop.add(i)
                k += 1
                if k == n:
                    break
        if k < n:
            return None
        gone = {ops[i][2].key for i in drop}
        e = [op for i, op in enumerate(ops[:tidx + 1]) if i not in drop and not (op[0] == "f" and op[2] in gone)]
    return replay(e)[-1]


def levers(f, a):
    tgt = find(f, a.var)
    k0 = tgt.key
    want = a.to or ("> %s" % a.above if a.above is not None else "< %s" % a.below if a.below is not None else
                    "mod8 %s" % a.mod8 if a.mod8 is not None else "hash > %#x" % a.hash_above
                    if a.hash_above is not None else "hash < %#x" % a.hash_below)
    print("#### %s: %s key %d (hash %04x, mod8 %d); want key %s" % (f.name, label(tgt), k0, h16(k0), k0 % 8, want))
    if target_ok(a, k0):
        print("# already there")
    ops = stream(f)
    tidx = next(i for i, op in enumerate(ops) if op[0] == "a" and op[2] is tgt)
    keep = re.compile(a.keep) if a.keep else None
    lines = tuple(int(x) for x in a.line.split("-")) if a.line else None
    if lines and len(lines) == 1:
        lines = (lines[0], lines[0])
    opt = tgt.phase == "opt" or "hash" in (a.classes or "")
    pts = candidate_points(f, tgt, not a.no_inline, opt) + [("@" + label(tgt), "just before %s itself" % label(tgt), tidx)]
    ns = [int(x) for x in a.n.split(",")] if a.n else list(range(1, a.max + 1))
    hits = []
    lclasses = (a.classes or ("frame,tmp,other,glob,hash" if opt else "frame,tmp,other,glob")).split(",")
    for anchor, where, at in pts:
        for cls in lclasses:
            for sign in ("+", "-"):
                for n in ns:
                    k1 = _prefix_key(ops, at, cls, n, sign, tidx)
                    if k1 is not None and target_ok(a, k1):
                        hits.append((anchor, where, cls, sign, n, k1))
                        break
    if a.match:
        return levers_match(f, a, pts, ops, tgt, ns)
    res = []
    for anchor, where, cls, sign, n, k1 in hits:
        e = "%s:%s%s:%d" % ("add" if sign == "+" else "sub", cls, anchor, n)
        newkey = keys_after(f, apply_edits(f, [e]))
        moved = [(s, s.key, newkey[id(s)]) for s in f.syms if s is not tgt and interesting(f, s) and id(s) in newkey
                 and newkey[id(s)] != s.key]
        fl = flips(f, newkey, a.pass_ or 2)
        fixed = [x for x in fl if (lines is None or lines[0] <= x[0][3] <= lines[1]) and
                 any(o.get("sym") is tgt or o.get("dsym") is tgt for o in x[0][4])]
        other = [x for x in fl if x not in fixed]
        bad = [s for s, _, _ in moved if keep and keep.search(label(s))]
        res.append((len(other), len(bad), len(moved), n, e, where, cls, sign, k1, moved, fixed, other))
    res.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    if not res:
        print("# no single edit of %s symbols at %d points reaches the target" % ("/".join(map(str, ns)), len(pts)))
    print("# %d points x 4 classes x add/sub, n in %s: %d edits reach it; ranked by other order flips, then keys "
          "moved" % (len(pts), ",".join(map(str, ns)), len(res)))
    for no, nb, nm, n, e, where, cls, sign, k1, moved, fixed, other in res[:a.top]:
        print("%s\n    -> %s key %d (hash %04x); %d target sorts change, %d other sorts flip, %d other keys move%s" % (
            e, label(tgt), k1, h16(k1), len(fixed), no, nm, (", %d of --keep" % nb) if keep else ""))
        print("    %s: %s" % (where, (CONSTRUCT if sign == "+" else UNCONSTRUCT)[cls]) + (" (x%d)" % n if n > 1 else ""))
        if other:
            print_flips(other, 4)
    return res


# ---------------------------------------------------------------- levers --site: several operand orders at once

def opname(o):
    """The operand's text as `sites` prints it (without the weight)."""
    return opdesc(o).split("  ")[0].strip()


def parse_site(t):
    """'LINE:A>B' (LINE an offset as `sites` prints it, LO-HI, or *; A and B substrings of two operands' text):
    in every pass-2 sort on those lines with an operand matching A and another matching B, A must come first."""
    m = re.match(r"^([^:]+):(.+?)>(.+)$", t)
    if not m:
        raise SystemExit("bad --site %r (LINE:A>B, e.g. '12:_vs>_p0' or '*:g_scene>t64')" % t)
    ln, A, B = m.group(1), m.group(2).strip(), m.group(3).strip()
    lo, hi = (0, 1 << 30) if ln == "*" else (int(ln.split("-")[0]), int(ln.split("-")[-1]))
    return dict(text=t, lo=lo, hi=hi, A=A, B=B)


def site_events(f, site, pass_):
    out = []
    for e in f.events:
        if e[0] != "sort" or e[1] != pass_ or not site["lo"] <= e[3] <= site["hi"] or len(e[4]) < 2:
            continue
        nm = [opname(o) for o in e[4]]
        ia = [i for i, x in enumerate(nm) if site["A"] in x]
        ib = [i for i, x in enumerate(nm) if site["B"] in x and i not in ia[:1]]
        if ia and ib:
            out.append((e, ia[0], ib[0]))
    return out


def site_state(e, ia, ib, newkey):
    """'ok' when operand ia comes before ib with the new keys, 'tie' when two operands end with equal weights (their
    order then follows the untraced input order), else 'no'."""
    ops = e[4]
    if len(ops) > 2:
        r = nary_order(ops, newkey)
        if r is None:
            return "no"
        order, ws = r
        if ws[ia] == ws[ib] and ops[ia]["w"] != ops[ib]["w"]:
            return "tie"
        return "ok" if order.index(ia) < order.index(ib) else "no"
    wa, wb = op_weight(ops[ia], newkey), op_weight(ops[ib], newkey)
    if wa is None or wb is None:
        return "no"
    if wa == wb:
        return "ok" if ia < ib and ops[ia]["w"] == ops[ib]["w"] else "tie"
    return "ok" if wa > wb else "no"


_SW = None


def _site_one(e):
    """levers --site worker: (sites satisfied, states, []) for one edit."""
    return _SW([e])


def levers_sites(f, a):
    """Edit sets that satisfy several wanted operand orders at once (--site LINE:A>B, repeatable, or --sites-file):
    every single add/sub edit before the last symbol the sites depend on, then pairs (--depth 2) and triples
    (--depth 3) of the --pool best ones, ranked by sites satisfied, then other pass-2 orders flipped, then size."""
    import time
    t0 = time.time()
    pass_ = a.pass_ or 2
    specs = list(a.site or [])
    if a.sites_file:
        specs += [l.strip() for l in open(a.sites_file) if l.strip() and not l.startswith("#")]
    sites = [parse_site(t) for t in specs]
    evs = []
    for st in sites:
        hits = site_events(f, st, pass_)
        if not hits:
            raise SystemExit("--site %s: no pass-%d sort with operands matching %r and %r (see `sites`)" % (st["text"], pass_, st["A"], st["B"]))
        evs.extend((st, e, ia, ib) for e, ia, ib in hits)
    base = {id(s): s.key for s in f.syms}
    print("#### %s: %d site constraints, %d sorts" % (f.name, len(sites), len(evs)))
    for st, e, ia, ib in evs:
        print("  %-28s line %d %s: %s before %s now: %s" % (st["text"], e[3], e[2], opname(e[4][ia]), opname(e[4][ib]),
                                                          site_state(e, ia, ib, base)))
    deps = set()
    for _, e, ia, ib in evs:
        for o in (e[4][ia], e[4][ib]):
            for k in ("sym", "dsym", "dsym2"):
                if o.get(k) is not None:
                    deps.add(id(o[k]))
            for k in ("mem", "mem2"):
                if o.get(k) and o[k][2] is not None:
                    deps.add(id(o[k][2]))
    last = max((s for s in f.syms if id(s) in deps), key=lambda s: s.idx, default=None)
    if last is None:
        raise SystemExit("the sites' operands have no symbol whose key an edit can move")
    classes = (a.classes or "frame,tmp,glob").split(",")
    pts = candidate_points(f, last, not a.no_inline, "hash" in classes) + [("@" + label(last), "just before %s itself" % label(last), None)]
    ns = [int(x) for x in a.n.split(",")] if a.n else list(range(1, a.max + 1))
    site_ids = {id(e) for _, e, _, _ in evs}

    fk = FastKeys(f, deps)
    print("# replay snapshots: %d (ops up to the last dependency: %d) in %.1f s" % (len(fk.snaps), fk.stop, time.time() - t0))

    def score(edits, full=False):
        """(sites satisfied, states, other flips); the other flips (a whole replay) only when full."""
        try:
            newkey = keys_after(f, apply_edits(f, edits)) if full else fk.keys(edits)
        except (SystemExit, KeyError, StopIteration, IndexError, ValueError):
            return None
        st = [site_state(e, ia, ib, newkey) for _, e, ia, ib in evs]
        other = [x for x in flips(f, newkey, pass_) if id(x[0]) not in site_ids] if full else []
        return (sum(1 for x in st if x == "ok"), st, other)

    singles = []
    cand = []
    for cls in classes:
        kp, _ = prune_points(f, pts, cls)
        cand += [("%s:%s%s:%d" % (sign, cls, anchor, n), n) for anchor, where, _ in kp for sign in ("add", "sub") for n in ns]
    global _SW
    _SW = score
    njobs = a.jobs or min(8, os.cpu_count() or 1)
    print("# %d single edits to try, %d worker processes" % (len(cand), njobs), flush=True)
    if njobs > 1 and len(cand) > 50:
        import multiprocessing
        with multiprocessing.get_context("fork").Pool(njobs) as pool:
            got = pool.map(_site_one, [c[0] for c in cand], chunksize=max(1, len(cand) // (njobs * 8)))
    else:
        got = [_site_one(c[0]) for c in cand]
    for (e, n), r in zip(cand, got):
        if r is not None:
            singles.append((r[0], 0, -n, [e], r))
    singles.sort(key=lambda x: (x[0], x[2]), reverse=True)
    res = list(singles)
    pool = singles[:a.pool]
    print("# %d single edits (%d points, pruned per class to those that differ, x %s x add/sub x n in %s) in %.1f s" % (len(singles), len(pts), "/".join(classes),
                                                                           ",".join(map(str, ns)), time.time() - t0))
    if a.depth >= 2 and pool:
        combos = []
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                ea, eb = pool[i][3][0], pool[j][3][0]
                if ea.rsplit(":", 1)[0] == eb.rsplit(":", 1)[0]:
                    continue                    # the same edit with another n: covered by the n list
                combos.append([ea, eb])
        if a.depth >= 3:
            top3 = pool[:max(6, a.pool // 3)]
            for i in range(len(top3)):
                for j in range(i + 1, len(top3)):
                    for k in range(j + 1, len(top3)):
                        combos.append([top3[i][3][0], top3[j][3][0], top3[k][3][0]])
        for c in combos:
            r = score(c)
            if r is not None:
                res.append((r[0], 0, -sum(int(x.rsplit(":", 1)[1]) for x in c), c, r))
        print("# %d combinations of the %d best singles in %.1f s" % (len(combos), len(pool), time.time() - t0))
    # other operand orders: a whole replay, for the best candidates only
    res.sort(key=lambda x: (x[0], -len(x[3]), x[2]), reverse=True)
    best, seen0 = [], set()
    for r in res:
        key = tuple(sorted(r[3]))
        if key in seen0:
            continue
        seen0.add(key)
        rr = score(r[3], full=True)
        if rr is not None:
            best.append((rr[0], -len(rr[2]), r[2], r[3], rr))
        if len(best) >= max(3 * a.top, 30):
            break
    full = sum(1 for r in res if r[0] == len(evs))
    res = best
    res.sort(key=lambda x: (x[0], x[1], -len(x[3]), x[2]), reverse=True)
    print("# %d edit sets satisfy all %d sorts; best (sites satisfied, then fewest other flips, then fewest edits):" % (full, len(evs)))
    seen = set()
    shown = 0
    for nsat, nfl, nn, edits, (sat, st, other) in res:
        key = tuple(sorted(edits))
        if key in seen:
            continue
        seen.add(key)
        print("%d/%d sites  %3d other flips  %s" % (sat, len(evs), len(other), " ".join(edits)))
        if sat < len(evs):
            print("    unsatisfied: %s" % ", ".join("%s (%s)" % (x[0]["text"], s_) for x, s_ in zip(evs, st) if s_ != "ok"))
        if other:
            print_flips(other, 3)
        shown += 1
        if shown >= a.top:
            break
    return res


def mismatches(sa, sb):
    return [k for k in sa if k in sb and set(sigord(sa[k])) == set(sigord(sb[k])) and sigord(sa[k]) != sigord(sb[k])]


def levers_match(f, a, pts, ops, tgt, ns):
    """Rank single edits by how many pass-2 operand orders still differ from a reference trace (--match: a draft
    that already matches the original's code, or the original's order written down)."""
    ref = [g for g in from_json(json.load(open(a.match))) if g.name == f.name]
    if not ref:
        raise SystemExit("--match: %s not in %s" % (f.name, a.match))
    sb = sort_sig(ref[0], nocallee=True)
    base = mismatches(sort_sig(f, nocallee=True), sb)
    print("# --match %s: %d operand orders differ now:" % (a.match, len(base)))
    sa0 = sort_sig(f, nocallee=True)
    for k in base[:12]:
        print("    line %d %s #%d: now 1st %s, wanted 1st %s" % (sa0[k][2], k[0], k[-1], sa0[k][0], sb[k][0]))
    import time
    t0 = time.time()
    mclasses = (a.classes or "frame,tmp,hash").split(",")
    jobs = []
    for cls in mclasses:
        if cls == "hash":
            continue
        kp, _ = prune_points(f, pts, cls)
        for anchor, where, at in kp:
            for sign in ("add", "sub"):
                for n in ns:
                    jobs.append(("%s:%s%s:%d" % (sign, cls, anchor, n), n, where, cls, sign))
    if "hash" in mclasses:
        # value-numbering temps are made by the optimiser, after the target: their points span the whole function
        # (a hash key moves only the sorts that depend on it, e.g. Fallout 3's [$o] * $T crosses)
        hp = prune_points(f, [p for p in candidate_points(f, None, False, True) if "value-numbering" in p[1]], "hash")[0]
        for anchor, where, at in hp:
            for sign in ("add", "sub"):
                for n in ns:
                    jobs.append(("%s:hash%s:%d" % (sign, anchor, n), n, where, "hash", sign))
    global _MW
    _MW = (f, FastKeys(f, sort_syms(f)), sb)
    njobs = a.jobs or min(8, os.cpu_count() or 1)
    print("# %d edits to try, %d worker processes" % (len(jobs), njobs), flush=True)
    if njobs > 1 and len(jobs) > 50:
        import multiprocessing
        with multiprocessing.get_context("fork").Pool(njobs) as pool:
            got = pool.map(_match_one, [j[0] for j in jobs], chunksize=max(1, len(jobs) // (njobs * 8)))
    else:
        got = [_match_one(j[0]) for j in jobs]
    res = [(len(mm), n, e, where, cls, sign, mm) for (e, n, where, cls, sign), mm in zip(jobs, got) if mm is not None]
    print("# %.1f s" % (time.time() - t0))
    res.sort(key=lambda r: (r[0], r[1]))
    print("# %d edits tried (%d points x %s x add/sub x %d n); best:" % (len(res), len(pts), "/".join(mclasses), len(ns)))
    for nmm, n, e, where, cls, sign, mm in res[:a.top]:
        print("%-60s %d orders differ" % (e, nmm))
        print("    %s: %s" % (where, (CONSTRUCT if sign == "add" else UNCONSTRUCT)[cls]) + (" (x%d)" % n if n > 1 else ""))
    return res


_MW = None


def _match_one(e):
    """levers --match worker: the operand orders that still differ from the reference after edit e (None if the
    edit cannot be made)."""
    f, fk, sb = _MW
    try:
        newkey = fk.keys([e])
    except (SystemExit, KeyError, StopIteration, ValueError):
        return None
    return mismatches(sort_sig(f, newkey, nocallee=True), sb)


# ---------------------------------------------------------------- accuracy: prediction vs a traced variant

def stable_names(f, nocallee=False):
    """id(sym) -> a name that survives a source edit: $T<fekey> temporaries become $T, plus an ordinal per name;
    nocallee: also drop which helper an inlined symbol came from (to compare drafts that use other helpers)."""
    cache = f.__dict__.setdefault("_stable", {})
    if nocallee in cache:
        return cache[nocallee]
    out, cnt = {}, {}
    cache[nocallee] = out
    for s in f.syms:
        if not visible(f, s, False):
            continue
        n = re.sub(r"\$T\d+", "$T", label(s))
        n = re.sub(r"^\$o\d+", "$o", n)      # optimiser symbols: the k-th one an operand sort depends on
        n = re.sub(r"#\d+$", "", n)
        if nocallee:
            n = re.sub(r"@.*$", "@", n)
        cnt[n] = cnt.get(n, 0) + 1
        out[id(s)] = "%s~%d" % (n, cnt[n])
    return out


def sort_sig(f, newkey=None, pass_=2, nocallee=False):
    """{(opcode, operand names, ordinal): (first operand, second operand, line)} (stable names) for the pass's
    two-operand sorts; with newkey the order is the predicted one.  Sorts are matched by opcode and operand set
    (k-th sort of that set), not by visit order: a flip upstream changes the order later sorts are visited in."""
    names = stable_names(f, nocallee)
    out, cnt = {}, {}

    def nm(o):
        s = o.get("sym") or o.get("dsym") or (o.get("mem") or (0, 0, None))[2]
        n = names.get(id(s), "?") if s is not None else "%s/%s" % (o["kind"], (o["w"] or 0) >> 16)
        if o["kind"] == 13 and (s is None or n == "?"):
            # first source unnamed (an address temporary): name the subtree by its second source
            s2 = o.get("dsym2") or (o.get("mem2") or (0, 0, None))[2]
            if s2 is not None and id(s2) in names:
                n = "(.. %s)" % names[id(s2)]
        return n
    for e in f.events:
        if e[0] != "sort" or e[1] != pass_ or len(e[4]) < 2:
            continue
        if len(e[4]) > 2:                              # n-ary: the whole order (sigord) decides
            ops = e[4]
            if newkey is not None:
                r = nary_order(ops, newkey)
                if r is not None:
                    ops = [ops[i] for i in r[0]]
            names_ = tuple(nm(o) for o in ops)
            k = (e[2], tuple(sorted(names_)))
            cnt[k] = cnt.get(k, 0) + 1
            out[k + (cnt[k],)] = (names_[0], names_[1], e[3], names_)
            continue
        a, b = e[4]
        k = (e[2], tuple(sorted((nm(a), nm(b)))))     # line offsets move when the edit adds lines
        cnt[k] = cnt.get(k, 0) + 1
        if newkey is not None:
            wa, wb = op_weight(a, newkey), op_weight(b, newkey)
            if wa < wb:
                a, b = b, a
        out[k + (cnt[k],)] = (nm(a), nm(b), e[3])
    return out


def sigord(v):
    """The order a sort_sig value stands for: all operands of an n-ary sort, else the first two."""
    return v[3] if len(v) > 3 else v[:2]


def compare(fa, edits, fb):
    """Predict fb (a traced variant) from fa + edits; report key and sort-order agreement."""
    newkey = keys_after(fa, apply_edits(fa, edits)) if edits else {id(s): s.key for s in fa.syms}
    na, nb = stable_names(fa), stable_names(fb)
    kb = {nb[id(s)]: s.key for s in fb.syms if id(s) in nb}
    both = [(na[id(s)], newkey.get(id(s), s.key), kb[na[id(s)]], s.key) for s in fa.syms if id(s) in na and na[id(s)] in kb]
    ok = sum(1 for _, p, t, _ in both if p == t)
    moved = [(n, p, t, o) for n, p, t, o in both if t != o]
    okm = sum(1 for _, p, t, _ in moved if p == t)
    print("keys: %d symbols in both; predicted = traced for %d (%.1f%%); of the %d whose key really moved, %d predicted "
          "exactly" % (len(both), ok, 100.0 * ok / max(1, len(both)), len(moved), okm))
    bad = [(n, p, t, o) for n, p, t, o in both if p != t]
    for n, p, t, o in bad[:12]:
        print("   %-40s base %5d predicted %5d traced %5d" % (n, o, p, t))
    sa, sb = sort_sig(fa, newkey), sort_sig(fb)
    common = [k for k in sa if k in sb and set(sigord(sa[k])) == set(sigord(sb[k]))]
    agree = sum(1 for k in common if sigord(sa[k]) == sigord(sb[k]))
    base = sort_sig(fa)
    changed = [k for k in common if sigord(base.get(k, ())) != sigord(sb[k])]
    chok = sum(1 for k in changed if sigord(sa[k]) == sigord(sb[k]))
    print("sorts (pass 2, two or more operands, same operand set in both): %d; predicted order = traced for %d (%.1f%%); "
          "%d really changed order, %d of them predicted" % (len(common), agree, 100.0 * agree / max(1, len(common)),
                                                             len(changed), chok))
    for k in [k for k in common if sigord(sa[k]) != sigord(sb[k])][:12]:
        print("   line %d %s #%d: predicted 1st %s, traced 1st %s" % (sa[k][2], k[0], k[-1], sa[k][0], sb[k][0]))
    return ok, len(both), agree, len(common)


# ---------------------------------------------------------------- driver

ROLE = {}
OPNAMES = c2trace.c2build.get("opnames", {}) or {}


def trace(src, cl):
    roles = list(HOOKS)
    ROLE.clear()
    ROLE.update(enumerate(roles))
    img, hooks, rc, out, text = c2trace.run_trace(src, [HOOKS[r] for r in roles], cl, maxn=None)
    if rc:
        sys.stdout.write(out)
    return parse(text, hooks, img)


def to_json(funcs):
    out = []
    for f in funcs:
        idx = {id(s): i for i, s in enumerate(f.syms)}
        syms = [dict((k, getattr(s, k)) for k in Sym.__slots__ if k not in ("base",)) for s in f.syms]
        for d, s in zip(syms, f.syms):
            d["base"] = idx.get(id(s.base)) if s.base is not None else None
        def sop(o):
            o = dict(o)
            o["sym"] = idx.get(id(o["sym"])) if o["sym"] is not None else None
            o["dsym"] = idx.get(id(o["dsym"])) if o["dsym"] is not None else None
            if o.get("dsym2") is not None:
                o["dsym2"] = idx.get(id(o["dsym2"]))
            for k in ("mem", "mem2"):
                if o.get(k):
                    o[k] = [o[k][0], o[k][1], idx.get(id(o[k][2])) if o[k][2] is not None else None]
            return o
        evs = [["alloc", idx[id(e[1])]] if e[0] == "alloc" else
               ["sort", e[1], e[2], e[3], [sop(o) for o in e[4]]] if e[0] == "sort" else list(e) for e in f.events]
        out.append(dict(name=f.name, optidx=f.optidx, line0=f.line0, syms=syms, events=evs))
    return out


def from_json(data):
    funcs = []
    for d in data:
        f = Func()
        f.name, f.optidx, f.line0 = d["name"], d["optidx"], d.get("line0", 0)
        f.syms = [Sym(**{k: v for k, v in s.items() if k != "base"}) for s in d["syms"]]
        for s, sd in zip(f.syms, d["syms"]):
            s.base = f.syms[sd["base"]] if sd["base"] is not None else None
        def lop(o):
            o = dict(o)
            o["sym"] = f.syms[o["sym"]] if o["sym"] is not None else None
            o["dsym"] = f.syms[o["dsym"]] if o["dsym"] is not None else None
            if o.get("dsym2") is not None:
                o["dsym2"] = f.syms[o["dsym2"]]
            for k in ("mem", "mem2"):
                if o.get(k):
                    o[k] = (o[k][0], o[k][1], f.syms[o[k][2]] if o[k][2] is not None else None)
            return o
        nopt = 0
        for sy in f.syms:                    # exports made before optimiser symbols were named `$o<n>`
            if sy.phase == "opt":
                nopt += 1
                if re.fullmatch(r"\$[tfg]\d+", sy.name or ""):
                    sy.name = "$o%d" % nopt
        f.events = [("alloc", f.syms[e[1]]) if e[0] == "alloc" else
                    ("sort", e[1], e[2], e[3], [lop(o) for o in e[4]]) if e[0] == "sort" else tuple(e)
                    for e in d["events"]]
        funcs.append(f)
    return funcs


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cl = []
    if "--" in argv:
        i = argv.index("--")
        argv, cl = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("cmd", choices=("keys", "events", "whatif", "levers", "export", "sites", "compare"))
    ap.add_argument("src", nargs="?")
    ap.add_argument("edits", nargs="*")
    ap.add_argument("--from", dest="from_")
    ap.add_argument("--func", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--phase")
    ap.add_argument("--grep")
    ap.add_argument("--show")
    ap.add_argument("--var")
    ap.add_argument("--to")
    ap.add_argument("--above", type=int)
    ap.add_argument("--below", type=int)
    ap.add_argument("--mod8", type=int)
    ap.add_argument("--hash-above", type=lambda x: int(x, 0))
    ap.add_argument("--hash-below", type=lambda x: int(x, 0))
    ap.add_argument("--keep")
    ap.add_argument("--max", type=int, default=4)
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--out")
    ap.add_argument("--line")
    ap.add_argument("--pass", dest="pass_", type=int)
    ap.add_argument("--leaves", action="store_true")
    ap.add_argument("--n")
    ap.add_argument("--vs", help="compare: the traced variant (export JSON) to predict")
    ap.add_argument("--match", help="levers: rank edits by operand orders that differ from this export")
    ap.add_argument("--no-inline", action="store_true")
    ap.add_argument("--site", action="append", help="levers: a wanted order LINE:A>B (repeatable)")
    ap.add_argument("--sites-file", help="levers: wanted orders, one LINE:A>B per line")
    ap.add_argument("--classes", help="levers: allocation classes to edit (frame,tmp,glob,other,hash; defaults: --site "
                    "frame,tmp,glob, --match frame,tmp,hash, --var frame,tmp,other,glob (+hash for an optimiser target))")
    ap.add_argument("--depth", type=int, default=2, help="levers --site: 1 singles, 2 pairs, 3 triples")
    ap.add_argument("--pool", type=int, default=40, help="levers --site: singles combined into pairs/triples")
    ap.add_argument("--jobs", type=int, help="levers --match: worker processes (default min(8, cores))")
    ap.add_argument("--batch", help="whatif: edit sets from FILE (or - for stdin), one per line; the export is loaded once")
    ap.add_argument("--compact", action="store_true", help="whatif --batch: one line per edit set")
    a = ap.parse_intermixed_args(argv)
    if a.from_:
        if a.src:
            a.edits.insert(0, a.src)
        funcs = from_json(json.load(open(a.from_)))
    else:
        if not a.src:
            ap.error("need <src> or --from")
        funcs = trace(a.src, cl)
    sel = [f for f in funcs if f.name and c2trace.func_match(a.func, f.name)]
    if a.func and not sel:
        sys.exit("no function matching %s (compiled: %s)" % (a.func, ", ".join(f.name or "?" for f in funcs)))
    if a.cmd == "export":
        json.dump(to_json(sel), open(a.out, "w"))
        print("wrote %s (%d functions)" % (a.out, len(sel)))
        return 0
    if a.cmd == "compare":
        fb = [g for g in from_json(json.load(open(a.vs))) if g.name and c2trace.func_match(a.func, g.name)]
        for f in sel:
            g = [x for x in fb if x.name == f.name]
            print("#### %s  edits: %s" % (f.name, " ".join(a.edits) or "(none)"))
            if g:
                compare(f, a.edits, g[0])
        return 0
    for f in sel:
        if a.cmd == "keys":
            print_keys(f, a)
        elif a.cmd == "sites":
            print_sites(f, a)
        elif a.cmd == "events":
            print_events(f)
        elif a.cmd == "whatif" and a.batch:
            whatif_batch(f, a)
        elif a.cmd == "whatif":
            print_whatif(f, a)
        elif a.cmd == "levers" and (a.site or a.sites_file):
            levers_sites(f, a)
        elif a.cmd == "levers":
            if not a.var:
                ap.error("levers needs --var (or --site LINE:A>B for several orders at once)")
            levers(f, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
