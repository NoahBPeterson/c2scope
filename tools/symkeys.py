#!/usr/bin/env python3
"""Symbol keys (SYM.key: the number c2 hashes and sorts symbols by): traced, replayed, what-if.  Doc: docs/c2/symkeys.md

usage: symkeys.py keys    <src> --func F [--all] [--brief] [--grep RE] [--phase reader|inline|opt] [-- <cl args>]
       symkeys.py sites   <src> --func F [--line LO-HI] [--pass 1|2] [--source FILE] [-- <cl args>]
       symkeys.py events  <src> --func F [-- <cl args>]
       symkeys.py whatif  <src> --func F EDIT [EDIT..] [--show RE] [--batch FILE|- [--compact]] [-- <cl args>]
       symkeys.py levers  <src> --func F --var NAME (--to LO-HI | --above K | --below K | --mod8 R | --hash-above H
                          | --hash-below H) [--line LO-HI] [--match REF.json] [--n 1,2,16 | --max N] [--no-inline]
       symkeys.py levers  <src> --func F --site LINE:A>B [--site ..] [--sites-file F] [--classes frame,tmp,glob]
                          [--n ..|--max N] [--depth 1|2|3] [--pool K] [--per-expansion]   (several wanted orders at once)
       symkeys.py compare <src> --func F [EDIT..] --vs VARIANT.json      (prediction vs a traced variant)
       symkeys.py export  <src> --func F --out FILE.json
   Every command takes --from FILE.json (an export) instead of <src>: no compile.  --c2 13.10 / 15.00 / 17.00 (x64) as usual.

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
         V is a symbol as `keys` prints it (`_x`, `_v+4.f4`, `_t@Helper#2`, `$T5944+0.f4@Cls::Fn#2`); when several share
         the name, `NAME@KEY` picks one by its key (`_dir+0@169`).  `e1|e2` is one edit made at several places.
levers   every single add/sub edit at every first reference of a named symbol (and every inline expansion start) before
         --var, n from --n / 1..--max; prints those that put --var's key where asked, ranked by the other operand orders
         they flip, with the source construct each one stands for.  --match REF.json ranks instead by how many pass-2
         operand orders still differ from REF (a draft whose code already matches, or any reference trace).
         --site LINE:A>B (repeatable; A, B substrings of two operands as `sites` prints them, LINE an offset, LO-HI or
         *): edit sets (singles, then pairs / triples of the best) that give every such sort the wanted order at once,
         ranked by how many are satisfied (partial solutions included), then other orders flipped.  An operand is also
         `#KEY` or `NAME@KEY` (by symbol key), or the `?hXXXX` label of an unnamed subtree.  A wanted order the keys
         cannot give (SU / node count / an unfollowable hash decides it) is reported first, with what would change it
         (tools/sulever.py has the source rewrites), and left out of the search.  Each edit prints its `level:`
         (reader / inline xN / optimiser): an edit in an inline helper's body is made in every expansion (--per-expansion
         keeps the single-expansion edits).
compare  predicts VARIANT from <src> + EDITs: share of keys and of pass-2 operand orders predicted exactly.

What is modelled (docs/c2/symkeys.md section 1): keys come in blocks of 32 per pool class (frame, global, temp,
label/function, hash), handed out when a class's block is full; freed keys go on a LIFO list per class (13.10: temp and
frame only) and are reused first.  The replay is exact for the traced stream; an edit is exact as far as the source
change makes those allocations and no others (the construct table in symkeys.md section 4 says what each construct
allocates).
"""
import argparse, collections, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import c2trace

CLASSES = ("tmp", "frame", "glob", "other", "hash")
# SymAlloc kind -> pool class (SymAlloc 0x10bef1d0, index table 0x10bef395)
KIND_CLASS = {0: "tmp", 1: "tmp", 2: "tmp", 3: "tmp", 6: "tmp", 4: "frame", 5: "frame", 13: "frame",
              7: "glob", 8: "glob", 9: "other", 10: "other", 11: "other", 15: "hash"}
# a build whose SymAlloc picks its free lists by other kind ranges (the Xbox 360 c2): {kind: class}
KIND_CLASS = {int(k): v for k, v in (c2trace.c2build.get("symkeys_kind_class") or {}).items()} or KIND_CLASS
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
    # optional roles (13.10): "free2" a second chain-free loop (same specs as free; 17.00 x64 has free2..free5, the
    # copies of SymFree's loop inlined in four functions), "equivp" a probe right after a
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
# "kindmap" (17.00 x64): the free probe logs the head's kind byte, mapped by this table (other kinds: the default)
FREE_MAP = {int(k): v for k, v in (c2trace.c2build.get("symkeys_free_map", {}) or {}).items()}
FREE_DEFAULT = c2trace.c2build.get("symkeys_free_default", "frame")
# tuple kinds: [K, d] = raw kinds >= K are logged d lower than 14.00's (17.00 x64: an instruction operand is 12)
KIND_SHIFT = c2trace.c2build.get("symkeys_kind_shift")
# a memory leaf's base sits at the source-list offset, so a sort's "first source" fields are its base (17.00 x64;
# 13.00.9044 shifts kinds too but keeps the base at its own offset, logged by the sort's entry fields)
MEM_SRC = c2trace.c2build.get("symkeys_mem_src", bool(KIND_SHIFT))
# the sort exit logs the two operands' displacements after the 12 x86 values (17.00 x64)
SORT_DISP = c2trace.c2build.get("symkeys_sort_disp", False)
# keys SymInit itself takes from a class's first block, unseen by the alloc hooks (Xbox 360: frame key 0)
INIT_USED = dict(c2trace.c2build.get("symkeys_init_used") or {})
# the equiv entry's offset is absolute; a 4th value is the root's offset, subtracted (17.00 x64)
EQUIV_REL = c2trace.c2build.get("symkeys_equiv_rel", False)


# the worker pools inherit the loaded snapshot by fork; Windows has no fork, so it runs the edits in one process
_CAN_FORK = __import__("multiprocessing").get_all_start_methods().count("fork") > 0

def tkind(k):
    """A logged tuple kind in 14.00's numbering (leaves 1-12, an instruction 13)."""
    if KIND_SHIFT and k is not None and k >= KIND_SHIFT[0]:
        return k + KIND_SHIFT[1]
    return k


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
        self.imms = []        # --imm (13.00.9044): ('imm', ...) ('walk', ...) ('gr', ...) events of the immediate table
        self.orders = []      # role "order" (13.00.9044): OptRebuildTuples' evaluation order of a binary node


def _vals(rest):
    return c2trace._split(rest)


def _str(v):
    return v[1:-1] if v.startswith('"') else None


def parse(text, hooks, img):
    """-> [Func] in compile order.  c2 17.00 compiles each function on one of its worker threads (SymInit and the
    symbol table are per thread): the log's `# thread N` lines say whose events follow, and each thread's events
    are parsed on their own; functions are ordered by their SymInit."""
    lines = text.splitlines()
    if not any(ln.startswith("# thread ") for ln in lines):
        return _parse(list(enumerate(lines)), img)
    groups, head, thr = {}, [], "0"
    for i, ln in enumerate(lines):
        if ln.startswith("# thread "):
            thr = ln.split()[2]
        elif ln.startswith("# c2trace base "):
            head.append((i, ln))
        else:
            groups.setdefault(thr, []).append((i, ln))
    funcs = []
    for t in groups:
        funcs += _parse(head + groups[t], img)
    funcs.sort(key=lambda f: f.order)
    return funcs


def _parse(lines, img):
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
                mc = None if m else re.match(r"\?(\w+)@(\w+)@@", nm)    # 17.00 members: ?MakeTmpBinary@Tuple@@
                hit = names[a[k]] = (m.group(1) if m else "%s::%s" % (mc.group(2), mc.group(1)) if mc else nm, nm,
                                     "%08x" % r["va"] if r else "")
            fn = hit[0]
            step = SKIP.get(hit[1]) or SKIP.get(hit[2]) or SKIP.get(fn)
            if not step:
                return fn
            k += step
        return fn

    for lno, ln in lines:
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
            f.order = lno
            funcs.append(f)
            live, bykey, stack, uvar, equiv, inl, nread = {}, {}, [], [], [], [], 0
            sorts, nopt, lastsort = [], 0, None
            pend3 = None
            counts = {}
            continue
        if f is None:
            continue
        ev = f.events
        if hk in ("alloc", "alloc2"):    # alloc2: a second allocator (the Xbox 360 c2's temporaries)
            if ent:
                stack.append(dict(kind=(int(v[0], 16) & 0xff) if hk == "alloc" else 3, cr=creator(v[2:]),
                                  line=int(v[1], 16) & 0xffff if v[1] != "?" else 0))   # alloc2: always a temporary
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
        elif hk in ("free", "free2", "free3", "free4", "free5"):
            k = int(v[0], 16)
            if FREE_BY == "kind":
                cls = FREE_KIND.get(int(v[1], 16) & 0xff, "frame") if v[1] != "?" else "frame"
            elif FREE_BY == "kindmap":
                cls = FREE_MAP.get(int(v[1], 16) & 0xff, FREE_DEFAULT) if v[1] != "?" else FREE_DEFAULT
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
                off = int(v[2], 16)
                if EQUIV_REL and len(v) > 3 and v[3] != "?":
                    off = (off - int(v[3], 16)) & 0xffffffff
                    off = off - (1 << 32) if off >= 1 << 31 else off
                equiv.append((v[1], off))
            elif equiv:
                equiv.pop()
                s = live.get(v[0])
                if s is not None and len(v) > 2 and v[2] != "?":
                    s.fe = "%x/%d" % (int(v[1], 16) & 0xffff, int(v[2], 16))
        elif hk == "equivp":
            sm = live.get(v[0])
            if sm is not None and sm.base is None:
                sm.base, sm.off = live.get(v[1]), int(v[2], 16) if v[2] != "?" else 0
                if len(v) > 4 and v[3] != "?" and v[4] != "?":
                    sm.fe = "%x/%d" % (int(v[3], 16) & 0xffff, int(v[4], 16))   # type word, size (13.00.9044)
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
        elif hk in IMM_ROLES:
            _parse_imm(f, hk, ent, v)
        elif hk == "order":
            _parse_order(f, nopt, v, bykey)
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
                    kind = tkind(kind & 0xff)
                    skind = tkind(skind & 0xff) if skind is not None else None
                    leaf = key if kind in (1, 2) else None
                    sub = skey if kind == 13 and skind is not None and (skind & 0xff) in (1, 2) else None
                    ops.append(dict(kind=kind, w=w, key=leaf, dkey=sub, op=None,
                                    sym=bykey.get(leaf) if leaf is not None else None,
                                    dsym=bykey.get(sub) if sub is not None else None))
                    if MEM_SRC and kind == 6 and skind in (1, 2) and skey is not None:
                        # 17.00 x64: a memory leaf's base is at the source-list offset (+0x30), so the "first
                        # source" fields are its base leaf (the displacement is not logged here)
                        dv = num(v[12 + len(ops) - 1]) if SORT_DISP and len(v) > 13 else None
                        dv = (dv - (1 << 32) if dv >= 1 << 31 else dv) if dv is not None else "?"
                        ops[-1]["mem"] = (skey, dv, bykey.get(skey))
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
                kind = tkind(kind & 0xff)
                skind = tkind(skind & 0xff) if skind is not None else None
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
                pend3.append(dict(kind=tkind(vals[11] & 0xff), w=None, key=None, dkey=None, op=None, sym=None, dsym=None, nth=True))
            pend3 = None
        elif hk == "sortx" and not ent and lastsort is not None:
            def num(x):
                return int(x, 16) if x != "?" else None
            for o, j in zip(lastsort, (0, 6)):
                if o["kind"] != 13:
                    continue
                sk, key, base, disp, sk2, key2 = (num(x) for x in v[j:j + 6])
                sk = tkind(sk & 0xff) if sk is not None else None
                sk2 = tkind(sk2 & 0xff) if sk2 is not None else None
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


def _parse_order(f, nopt, v, bykey):
    """role "order" (13.00.9044, a probe in OptRebuildTuples before its OptCmpHashVal call): a binary node (TupIsBinary:
    OPADD..OPSHR, OPCMP, OPEQ..OPGE) being turned back into tuples.  Its second operand's subtree is emitted (evaluated)
    first when weight(src0) < weight(src1), unsigned, and the node's type class is not float (0x4000); the operand
    order of the node itself is kept.  Logged: opcode, line, type word, then per operand kind, weight, leaf key and
    the opcode of the leaf's single definition (the subtree a temporary stands for)."""
    def num(x):
        return int(x, 16) if x not in ("?", None) else None
    vals = [num(x) for x in v] + [None] * 12
    op, line, ty = vals[0], vals[1], vals[2]
    ops = []
    for kind, w, key, dop in (vals[3:7], vals[7:11]):
        kind = tkind(kind & 0xff) if kind is not None else None
        leaf = key if kind in (1, 2) else None
        ops.append(dict(kind=kind, w=w, key=leaf, sym=bykey.get(leaf) if leaf is not None else None,
                        dop=OPNAMES.get("%x" % dop) if dop is not None else None))
    for j, o in enumerate(ops):          # D's 2nd / 3rd sources (13.00.9044 logs them after the 11 fields above)
        srcs = []
        for k in range(2):
            sk, key, sk2, key2 = vals[11 + 8 * j + 4 * k: 15 + 8 * j + 4 * k]
            sk = tkind(sk & 0xff) if sk is not None else None
            sk2 = tkind(sk2 & 0xff) if sk2 is not None else None
            if sk in (1, 2) and key is not None and sk2 in (1, 2, 3) and key2 is not None and o["dop"]:
                srcs.append([sk2, key2, bykey.get(key2)])     # a temporary defined by OPARG x / OPASSIGN x: x
            elif sk in (1, 2, 3) and key is not None:
                srcs.append([sk, key, bykey.get(key)])
            else:
                srcs.append([sk, None, None])
        o["srcs"] = srcs
    if len(ops) < 2 or ops[0]["w"] is None or ops[1]["w"] is None:
        return
    swap = (ty or 0) & 0xf000 != 0x4000 and ops[0]["w"] < ops[1]["w"]
    f.orders.append(dict(npass=nopt, op=OPNAMES.get("%x" % (op or 0)) or "op%x" % (op or 0), line=(line or 0) & 0xffff,
                         ops=ops, swap=swap))


def orddesc(o):
    w = o["w"] or 0
    s = o["sym"]
    what = ("%s (key %d)" % (label(s), s.key) if s is not None and s.cls != "tmp" else
            "t%d" % o["key"] if o["key"] is not None else LEAFKIND.get(o["kind"], "kind%s" % o["kind"]))
    if o.get("dop"):
        args = []
        for sk, key, sy in o.get("srcs") or []:
            if key is None:
                args.append(LEAFKIND.get(sk, "kind%s" % sk) if sk is not None else "?")
                continue
            t = "%s(key %d)" % (label(sy), key) if sy is not None and sy.cls != "tmp" else "t%d" % key
            # the hash such a leaf contributes (11.3): &x its folded key, a named symbol 32*key folded, a temporary
            # with several definitions 64*key (one with a single definition hashes by it: not modelled here)
            hh = (key & 0xffff) ^ (key >> 16) if sk == 3 else shash(sy, key)
            args.append("%s%s h%04x" % ("&" if sk == 3 else "", t, hh))
        what += " = %s(%s)" % (o["dop"], ", ".join(args)) if args else " = %s(..)" % o["dop"]
    return "%-40s SU%d n%d h%04x" % (what, w >> 24, (w >> 16) & 0xff, w & 0xffff)


def print_orders(f, a, lo, hi):
    """sites (13.00.9044, role "order"): the binary nodes whose two operands are both subtrees (count >= 2), with
    the operand OptRebuildTuples evaluates first.  Lines with only leaves or constants on one side are left out
    (their order does not move code)."""
    rows = [e for e in f.orders if lo <= e["line"] <= hi and (not a.pass_ or e["npass"] == a.pass_)
            and all(((o["w"] or 0) >> 16) & 0xff >= 2 for o in e["ops"])]
    if not rows:
        return
    print("#### %s: evaluation order of binary nodes (OptRebuildTuples: the 2nd operand's subtree first when its "
          "weight is larger)" % f.name)
    for e in rows:
        o0, o1 = e["ops"]
        first, second = (o1, o0) if e["swap"] else (o0, o1)
        print("pass %d line %4d %-10s first  %s%s" % (e["npass"], e["line"], e["op"], orddesc(first),
                                                    "   (2nd operand, heavier)" if e["swap"] else
                                                    "   (1st operand%s)" % (", tie" if o0["w"] == o1["w"] else "")))
        print("%31s then   %s" % ("", orddesc(second)))


def _parse_imm(f, hk, ent, v):
    """--imm (13.00.9044): the immediate table of Globregs (GrgRewriteImmedAsReg, its 64 buckets, the walk that frees
    the unused ones) and every globreg number GrgNewGlobreg hands out (a freed record keeps its number)."""
    def num(x):
        return int(x, 16) if x not in ("?", None) else None
    if hk == "immnew":
        kind, val = num(v[0]), num(v[1])
        kind = kind & 0xff if kind is not None else None
        name, fe = (_str(v[2]), num(v[3])) if kind == 3 else (_str(v[4]), num(v[5])) if kind == 4 else (None, None)
        if kind == 7 and val is not None:
            what, bucket = "%d" % (val - (1 << 32) if val >= 1 << 31 else val), val & 0x3f
        else:
            what, bucket = ("&" if kind == 3 else "") + (name or "?"), fe & 0x3f if fe is not None else None
        f.imms.append(dict(ev="imm", kind=kind, what=what, fe=fe if kind in (3, 4) else None, bucket=bucket, gr=None))
    elif hk == "immwalk":
        f.imms.append(dict(ev="walk", sym=v[0], bucket=(num(v[1]) or 0) // 4, refs=num(v[2])))
    elif hk == "immfree":
        f.imms.append(dict(ev="free", gr=num(v[0])))
    elif hk == "newgr":
        if ent:
            f._grpend = dict(ev="gr", sym=v[0], kind=num(v[1]), ikind=num(v[2]), val=num(v[3]), name=_str(v[4]),
                             top=num(v[5]))
            return
        g, f._grpend = getattr(f, "_grpend", None), None
        if g is None:
            return
        g["gr"] = num(v[0])
        g["reused"] = g["top"] is not None and g["top"] == g["gr"]
        walked = any(x["ev"] == "walk" for x in f.imms)
        if not walked:
            # before the walk: only an immediate's own web, made at its first reference, is of interest
            if (g["kind"] or 0) & 0xff == 0xd and f.imms and f.imms[-1]["ev"] == "imm" and f.imms[-1]["gr"] is None:
                f.imms[-1]["gr"] = g["gr"]
            return
        f.imms.append(g)

TYCLS = {1: "i", 2: "u", 3: "p", 4: "p", 5: "f", 6: "b", 7: "b"}
# the TY word's class nibble per build (13.00.9044: 1 int, 2 unsigned, 3 pointer, 4 float, 5 block)
TYCLS = {int(k): v for k, v in (c2trace.c2build.get("symkeys_tycls") or {}).items()} or TYCLS


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
        cur[c] = [nxt + INIT_USED.get(c, 0), nxt + 32]
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
            cur[c] = [nxt + INIT_USED.get(c, 0), nxt + 32]
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
        m = re.match(r"^add:(frame|tmp|glob|other|hash)@([^#|].*?)(?::(\d+))?$", edits[0]) if len(edits) == 1 and "|" not in edits[0] else None
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
    if getattr(a, "imm", False):
        print_imms(f)


def print_imms(f):
    """--imm: the immediate table (13.00.9044, Globregs).  Integer constants and addresses held in registers are
    SYMs of kind 0xd that GrgRewriteImmedAsReg makes with getmem, not SymAlloc: they have no key.  What orders their
    webs is the globreg number: an immediate's own web is numbered at its first reference; webs made after the
    table walk (variables' webs in 1073053e, Color's split pieces) take freed numbers first, the last freed first.
    The walk frees unused immediates bucket by bucket (0..63): bucket = value & 63 for a constant, the c1 symbol
    number (SSR +0x28) & 63 for an address, so a declaration before the function changes the order."""
    if not f.imms:
        print("# immediates: none traced (pass --imm; 13.00.9044 only)")
        return
    seen = {}
    for x in f.imms:
        if x["ev"] == "imm":
            seen.setdefault((x["kind"], x["what"]), []).append(x)
    imms = [v[0] for v in seen.values()]
    for x in imms:
        x["nref"] = len(seen[(x["kind"], x["what"])])
    print("# immediates in first-reference order (GrgRewriteImmedAsReg; integer constants and addresses held in registers "
          "are getmem'd SYMs of kind 0xd: no key): c1 number, bucket, references, web number")
    print("%-28s %6s %6s %5s %6s %s" % ("immediate", "c1#", "bucket", "refs", "web", "fate"))
    walk = [x for x in f.imms if x["ev"] in ("walk", "free")]
    fate, last = {}, None
    for x in walk:
        if x["ev"] == "walk":
            last = x
        elif last is not None:
            fate[x["gr"]] = "freed (refs %s)" % last["refs"]
    order = [x["gr"] for x in walk if x["ev"] == "free"]
    for x in imms:
        print("%-28s %6s %6s %5d %6s %s" % (x["what"], x["fe"] if x["fe"] is not None else "", x["bucket"] if x["bucket"]
                                             is not None else "?", x["nref"], x["gr"] if x["gr"] is not None else "-",
                                             fate.get(x["gr"], "kept" if x["gr"] is not None else "not enregistered")))
    if order:
        print("# freed in bucket order (webs): %s; the free list hands them out last-freed first: %s" % (
            " ".join(str(g) for g in order), " ".join(str(g) for g in reversed(order))))
    later = [x for x in f.imms if x["ev"] == "gr"]
    if later:
        print("# webs numbered after the walk (variables' webs, Color's split pieces): number (reused = a freed one)")
        for x in later:
            k = (x["kind"] or 0) & 0xff
            if k == 0xd:
                v = x["val"]
                what = "piece of %d" % (v - (1 << 32) if v >= 1 << 31 else v) if (x["ikind"] or 0) & 0xff == 7 and \
                    v is not None else "piece of an address immediate"
            else:
                what = x["name"] or ("a temporary's web" if k == 3 else "sym %s (kind %d)" % (x["sym"], k))
            print("   %-34s web %4s%s" % (what, x["gr"], "  (reused)" if x.get("reused") else ""))


def event_text(f, i):
    """One event as `events` prints it (for `#N` anchors: what event N is)."""
    e = f.events[i]
    if e[0] == "alloc":
        return "alloc %s key %d %s line %d" % (e[1].cls, e[1].key, label(e[1]), e[1].line)
    if e[0] in ("inl+", "spl+"):
        return "%s %s (call line %d)" % ("read inlinee" if e[0] == "inl+" else "splice", short("%s#%d" % (e[1], e[2])),
                                        e[3])
    if e[0] in ("inl-", "spl-"):
        return "%s end" % ("read inlinee" if e[0] == "inl-" else "splice")
    return " ".join(str(x) for x in e[:3])


def print_tmps(f, a):
    """The tmp-class symbols allocated in an event range, grouped by the construct they were made in (the inline
    expansion, else the function itself) and the source line current then: a `sub:tmp@#N:k` lever needs k of
    these gone, and this says where they come from."""
    num = lambda t: int(str(t).lstrip("#")) if t is not None else None
    hi, lo = num(a.before), num(a.after) or 0
    hi = len(f.events) if hi is None else hi
    print("#### %s: tmp allocations in events [%d, %d)" % (f.name, lo, hi))
    if hi < len(f.events):
        print("# event #%d = %s" % (hi, event_text(f, hi)))
    groups = collections.OrderedDict()
    for i in range(lo, min(hi, len(f.events))):
        e = f.events[i]
        if e[0] != "alloc" or e[1].cls != "tmp":
            continue
        s = e[1]
        k = (short(s.inl) if s.inl else "(the function's own code)", s.line)
        groups.setdefault(k, []).append((i, s))
    total = sum(len(v) for v in groups.values())
    print("# %d tmp allocations; by construct and line (line = offset from the function's first line):" % total)
    for (con, line), v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print("  %4d  %-40s line %4d   events %s%s" % (len(v), con[:40], line, ", ".join("#%d" % i for i, _s in v[:6]),
                                                       " ..." if len(v) > 6 else ""))


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
    for e in f.orders:      # role "order" (13.00.9044): not part of the key stream, printed after it
        o0, o1 = e["ops"]
        print("order pass %d line %d %s: first %s | then %s" % (e["npass"], e["line"], e["op"],
              orddesc(o1 if e["swap"] else o0).strip(), orddesc(o0 if e["swap"] else o1).strip()))


LEAFKIND = {1: "reg", 2: "sym", 3: "&", 4: "code", 5: "ea", 6: "mem", 7: "iconst", 8: "iconst", 9: "fconst"}


def _disp(d):
    """A memory operand's displacement as printed: '?' when the trace does not log it (17.00 x64 sort leaves)."""
    return d if isinstance(d, str) else "%d" % (d or 0)


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
            what = "([%s+%s] ..)" % ("%s key %d" % (label(b), b.key) if b is not None and (b.cls != "tmp" or b.phase == "opt") else
                                     "t%d" % m[0], _disp(m[1]))
        else:
            what = "(%s ..)" % ("%s key %d" % (label(d), d.key) if d is not None and (d.cls != "tmp" or d.phase == "opt") else
                                "t%d" % o["dkey"] if o["dkey"] is not None else "?h%04x" % (w & 0xffff))
        m2 = o.get("mem2")
        if m2 and m2[2] is not None and m2[2].cls != "tmp":
            what = what[:-4] + " [%s key %d+..] ..)" % (label(m2[2]), m2[2].key)
    elif o["kind"] == 6 and o.get("mem"):
        m = o["mem"]
        b = m[2]
        what = "[%s+%s]" % ("%s key %d" % (label(b), b.key) if b is not None and (b.cls != "tmp" or b.phase == "opt") else "t%d" % m[0],
                            _disp(m[1]))
    else:
        what = LEAFKIND.get(o["kind"], "kind%d" % o["kind"])
    return "%-46s %s" % (what, t)


def line_note(f):
    """Site lines are offsets from the function's first source line (FUNC+0x44), as --line and --site take them."""
    if f.line0:
        return "# line = offset from the function's first line (source line %d): absolute = %d + line" % (f.line0, f.line0)
    return "# line = offset from the function's first line (that line was not traced)"


CREATOR_HINT = {"HshInsertEntry": "value-numbering temp: a value the line computes (a repeated one is numbered once)",
                "TupMakeTmpBinary": "result temp of a binary operation of that line",
                "TupMakeTmpUnary": "result temp of a unary operation / conversion of that line",
                "OptRebuildTuples": "temp made rebuilding the tuples of that line (a subtree evaluated first)"}


def opt_syms(o):
    """The optimiser-made (`$o<n>`) symbols an operand reads: leaf, first / second source, memory bases."""
    out = []
    for k in ("sym", "dsym", "dsym2"):
        if o.get(k) is not None:
            out.append(o[k])
    for k in ("mem", "mem2"):
        if o.get(k) and o[k][2] is not None:
            out.append(o[k][2])
    seen = []
    for x in out:
        if x.phase == "opt" and x not in seen:
            seen.append(x)
    return seen


def first_line(f, src):
    """The source line of the function's first line: traced (FUNC+0x44), else the first line of `src` that declares
    its undecorated name (the offsets `sites` prints count from there)."""
    if f.line0:
        return f.line0
    m = re.match(r"\??(?:\?\?\w)?([^@?]+)@", f.name or "")
    if m and src:
        rx = re.compile(r"(?<![\w:])(?:\w+::)?%s\s*\(" % re.escape(m.group(1)))
        for i, ln in enumerate(src):
            if rx.search(ln):
                return i + 1
    return 0


def prov(f, o, src=None):
    """Where an optimiser temporary of this operand comes from: the c2 step that made it and the source line it
    was made for (the line offset is the one `--line` takes; `src` = the source file's lines, when known)."""
    out = []
    for x in opt_syms(o):
        mk = re.sub(r"^@|@\d+$", "", re.sub(r"/\w+$", "", x.creator or ""))
        t = "%s: made by %s at line %d" % (label(x), mk, x.line)
        l0 = first_line(f, src)
        if l0 and src and 0 < l0 + x.line <= len(src):
            t += " (source %d: %s)" % (l0 + x.line, src[l0 + x.line - 1].strip()[:90])
        if mk in CREATOR_HINT:
            t += ", " + CREATOR_HINT[mk]
        out.append(t)
    return out


def print_sites(f, a):
    lo, hi = (0, 1 << 30)
    if a.line:
        p = a.line.split("-")
        lo, hi = int(p[0]), int(p[-1])
    srcf = getattr(a, "source", None) or (a.src if a.src and os.path.isfile(a.src) else None)
    try:
        src = open(srcf, errors="replace").read().split("\n") if srcf else None
    except OSError:
        src = None
    print("#### %s: commutative operand sorts (OptLeftWeightTree), first operand = evaluated first / loaded" % f.name)
    print(line_note(f))
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
        shown = set()
        for o in ops:
            for t in prov(f, o, src):
                if t not in shown:
                    shown.add(t)
                    print("%31s ^ %s" % ("", t))
    print_orders(f, a, lo, hi)


# ---------------------------------------------------------------- what-if

def _label_count(f):
    if getattr(f, "_lc", None) is None:
        f._lc = {}
        for s in f.syms:
            f._lc[label(s)] = f._lc.get(label(s), 0) + 1
    return f._lc


def anchor_of(f, s):
    """The name an edit uses for symbol s: its label, plus `@KEY` when another symbol has the same label (two field
    views of one variable, `_dir+0` keys 16 and 169)."""
    return label(s) if _label_count(f)[label(s)] == 1 else "%s@%d" % (label(s), s.key)


_NOTED = set()


def find(f, name):
    """A symbol by label or name.  `NAME@KEY` (or `#KEY`) picks the one with that key when several share the label
    (`_dir+0@169`); a bare name takes the first and says so."""
    hits = [s for s in f.syms if label(s) == name] or [s for s in f.syms if s.name == name]
    if not hits:
        m = re.fullmatch(r"(.*?)@(\d+)", name) or re.fullmatch(r"()#(\d+)", name)
        if m:
            hits = [s for s in f.syms if s.key == int(m.group(2)) and (not m.group(1) or label(s) == m.group(1) or s.name == m.group(1))]
    if not hits:
        raise SystemExit("no symbol %s (names are as `keys` prints them; NAME@KEY or #KEY selects by key)" % name)
    if len(hits) > 1 and (id(f), name) not in _NOTED and len({s.key for s in hits}) > 1 and label(hits[0]) == name:
        _NOTED.add((id(f), name))
        sys.stderr.write("symkeys: %s names %d symbols (keys %s); using key %d, select one with %s@KEY\n" % (
            name, len(hits), ", ".join(str(s.key) for s in hits), hits[0].key, name))
    return hits[0]


def split_edits(edits):
    """An edit may be several joined by `|` (one source change made at every expansion of an inline helper)."""
    return [x for e in edits for x in e.split("|")]


def expand_inline(f, e):
    """An add/sub edit anchored at a symbol inside an inline expansion's body, as the one source change it stands for:
    the same edit in every expansion of that helper (its body is one piece of source; no construct gives one
    expansion a different offset).  Other edits are returned as they are."""
    m = re.match(r"^(add|sub):(\w+)@([^#|].*?):(\d+)$", e)
    if not m:
        return e
    try:
        s = find(f, m.group(3))
    except SystemExit:
        return e
    if not s.inl:
        return e
    base = s.inl.rsplit("#", 1)[0]
    peers = {}
    for x in f.syms:
        if x.inl and x.inl.rsplit("#", 1)[0] == base and x.name == s.name and x.inl not in peers:
            peers[x.inl] = x
    if len(peers) < 2:
        return e
    return "|".join("%s:%s@%s:%s" % (m.group(1), m.group(2), anchor_of(f, x), m.group(4))
                    for x in sorted(peers.values(), key=lambda x: x.idx))


def edit_level(f, e):
    """Where in the source an edit lives: `reader` (a statement of the function itself, or the call statement of an
    inline helper), `inline xN` (in a helper's body, so in all N expansions), `inline x1` (a helper expanded once),
    `optimiser` (value-numbering temps: code-neutral constructs, symkeys.md 8.2)."""
    parts = split_edits([e])
    if len(parts) > 1:
        return "inline x%d (the helper's body: every expansion gets it)" % len(parts)
    m = re.match(r"^(?:add|sub):(\w+)@(#?)(.+?)(?::\d+)?$", e)
    if not m:
        return "reader (first references reordered)"
    if m.group(1) == "hash":
        return "optimiser"
    if m.group(2):
        return "reader (a statement before the inline call)"
    try:
        if find(f, m.group(3)).inl:
            return "inline x1 (a helper expanded once)"
    except SystemExit:
        pass
    return "reader"


def _sub_drop(ops, at, cls, n):
    """sub:CLS@..:n -> the op indices it drops: the last n allocations of cls before `at` not freed before `at`."""
    freed_before = {op[2] for op in ops[:at] if op[0] == "f"}
    out = set()
    for i in range(at - 1, -1, -1):
        op = ops[i]
        if op[0] == "a" and op[1] == cls and op[2] is not None and op[2].key not in freed_before:
            out.add(i)
            if len(out) == n:
                break
    return out


def sub_drops(f, edits):
    """The symbols the sub: edits of an edit set remove (what the source change has to delete)."""
    ops = stream(f)
    pos = {id(op[2]): i for i, op in enumerate(ops) if op[0] == "a" and op[2] is not None}
    out = []
    for e in split_edits(edits):
        m = re.match(r"sub:(frame|tmp|glob|other|hash)@(#?)(.+?)(?::(\d+))?$", e)
        if not m:
            continue
        cls, ev, where, n = m.group(1), m.group(2), m.group(3), int(m.group(4) or 1)
        if ev:
            at = sum(1 for x in f.events[:int(where)] if x[0] in ("alloc", "free"))
        else:
            at = pos.get(id(find(f, where)))
            if at is None:
                continue
        out += [ops[i][2] for i in sorted(_sub_drop(ops, at, cls, n))]
    return out


def used_syms(f):
    """id()s of the symbols an operand sort reads (either pass): a source change cannot remove them."""
    if getattr(f, "_used", None) is None:
        u = set()
        for e in f.events:
            if e[0] != "sort":
                continue
            for o in e[4]:
                for k in ("sym", "dsym", "dsym2"):
                    if o.get(k) is not None:
                        u.add(id(o[k]))
                for k in ("mem", "mem2"):
                    if o.get(k) and o[k][2] is not None:
                        u.add(id(o[k][2]))
        f._used = u
    return f._used


def realizable(f, edits, keep=()):
    """False when a sub: edit would remove a named symbol the function still reads (an operand of a sort, or one
    of `keep`): no source change deletes a variable that is used (docs/c2/symkeys.md 10.3)."""
    used = used_syms(f) | {id(x) for x in keep}
    for s_ in sub_drops(f, edits):
        if s_.creator == "SymInit" or id(s_) in used and s_.name and not s_.name.startswith("$"):
            return False          # SymInit's dummy, or a symbol a sort reads
    return True


def named_drops(f, edits):
    """The named symbols an edit set removes: each must be dead in the source (an initialised, unread local)."""
    return [x for x in sub_drops(f, edits) if x.name and not x.name.startswith("$")]


def apply_edits(f, edits):
    """-> list of ops (with ('a', cls, None) for inserted symbols) after the edits."""
    ops = stream(f)
    pos = {}         # sym -> op index
    for i, op in enumerate(ops):
        if op[0] == "a":
            pos[id(op[2])] = i
    ins = []         # (op index, [ops])
    drop = set()
    for e in split_edits(edits):
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
                drop |= _sub_drop(ops, at, cls, n)
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


def same_statement_start(f, i):
    """Is event i (an inline expansion's start) in the same statement as an earlier top-level expansion, or inside
    another expansion's body?  Then no source statement of the caller falls between the two, and an edit there has
    no reader-level construct (a local declared in the caller lands before the whole statement, so before the first
    of them); it exists only as an edit of the helper's body, in every expansion."""
    depth, prev = 0, None
    for e in f.events[:i]:
        if e[0] == "inl+":
            if depth == 0:
                prev = e
            depth += 1
        elif e[0] == "inl-":
            depth -= 1
    return depth > 0 or (prev is not None and prev[3] == f.events[i][3])


def candidate_points(f, target, inline_too=True, opt_too=False, per_exp=False):
    """Insertion points before the target's allocation, as (edit anchor, description, op index):
    the first allocation of each named symbol of the function itself (reader order), the start of each inline
    expansion (reading the callee), and, inside expansions, the first named local of each one."""
    pts, seen, opi = [], set(), 0
    f._skipped_inl = 0
    for i, e in enumerate(f.events):
        if e[0] == "alloc" and e[1] is target:
            break
        if e[0] == "alloc":
            s = e[1]
            if s.user and s.cls in ("frame", "glob") and (s.inl is None or inline_too) and not s.name.startswith("$"):
                k = ("v", label(s), s.key if _label_count(f)[label(s)] > 1 else 0)
                if k not in seen:
                    seen.add(k)
                    pts.append(("@" + anchor_of(f, s), ("before parameter %s (parameters take keys last to first: a frame "
                                                 "symbol here is one more parameter after it)" % label(s))
                                if s.kind == 5 and s.inl is None else "before the first reference of %s" % label(s), opi))
        elif e[0] == "inl+" and inline_too and not per_exp and same_statement_start(f, i):
            f._skipped_inl += 1
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
SWAP_CONSTRUCT = ("declare it with an initialiser, or give it a dead store (`x = 0;`, removed by the optimiser), before "
                  "that local's first use: a local takes its key at its first reference")
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
    pts = candidate_points(f, tgt, not a.no_inline, opt, a.per_expansion) + [("@" + anchor_of(f, tgt), "just before %s itself" % label(tgt), tidx)]
    ns = [int(x) for x in a.n.split(",")] if a.n else list(range(1, a.max + 1))
    if f._skipped_inl:
        print("# %d inline-expansion start points left out: no statement of the caller falls between them and the "
              "previous expansion (--per-expansion keeps them)" % f._skipped_inl)
    hits = []
    fk = None
    lclasses = (a.classes or ("frame,tmp,other,glob,hash" if opt else "frame,tmp,other,glob")).split(",")
    for anchor, where, at in pts:
        for cls in lclasses:
            for sign in ("+", "-"):
                for n in ns:
                    ex = "%s:%s%s:%d" % ("add" if sign == "+" else "sub", cls, anchor, n)
                    ex = ex if a.per_expansion else expand_inline(f, ex)
                    if "|" in ex:
                        # a helper's body: the edit lands in every expansion
                        try:
                            fk = fk or FastKeys(f, {id(tgt)})
                            k1 = fk.keys([ex]).get(id(tgt), tgt.key)
                        except (SystemExit, KeyError, IndexError, ValueError, StopIteration):
                            k1 = None
                    else:
                        k1 = _prefix_key(ops, at, cls, n, sign, tidx)
                    if k1 is not None and target_ok(a, k1):
                        if sign == "-" and not realizable(f, ["sub:%s%s:%d" % (cls, anchor, n)], [tgt]):
                            break       # it removes a symbol the code reads: no source change does that
                        hits.append((anchor, where, cls, sign, n, k1))
                        break
    if a.match:
        return levers_match(f, a, pts, ops, tgt, ns)
    # the target first referenced before another named local of the function (17.00 measurement, symkeys.md 10.3)
    if tgt.user and tgt.cls == "frame" and tgt.kind == 4 and tgt.inl is None:
        for w in f.syms:
            if w.idx < tgt.idx and w.user and w.cls == "frame" and w.kind == 4 and w.inl is None and w.name and \
                    not w.name.startswith("$"):
                try:
                    k1 = keys_after(f, apply_edits(f, ["swap:%s,%s" % (label(tgt), label(w))])).get(id(tgt))
                except (SystemExit, KeyError):
                    continue
                if k1 is not None and target_ok(a, k1):
                    hits.append((anchor_of(f, w), "%s first referenced just before %s" % (label(tgt), label(w)), "frame",
                                 "swap", 1, k1))
    res = []
    done = set()
    for anchor, where, cls, sign, n, k1 in hits:
        e = ("swap:%s,%s" % (anchor_of(f, tgt), anchor) if sign == "swap" else
             "%s:%s%s:%d" % ("add" if sign == "+" else "sub", cls, anchor, n))
        if not a.per_expansion:
            e = expand_inline(f, e)
        if e in done:
            continue            # the same helper edit reached from another expansion
        done.add(e)
        newkey = keys_after(f, apply_edits(f, [e]))
        if "|" in e:
            k1 = newkey.get(id(tgt), k1)       # the helper edit at every expansion moves the target differently
            if not target_ok(a, k1):
                continue
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
        print("    level: %s" % edit_level(f, e))
        print("    %s: %s" % (where, SWAP_CONSTRUCT if sign == "swap" else (CONSTRUCT if sign == "+" else UNCONSTRUCT)[cls])
              + (" (x%d)" % n if n > 1 else ""))
        if sign == "-" and named_drops(f, [e]):
            print("    removes: %s (each must be an unread local in the source)" % ", ".join(
                label(x) for x in named_drops(f, [e])))
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


def op_match(nm, text):
    """Does operand text `nm` (as `sites` prints it) match the --site operand `text`?  A substring, or a key selector:
    `#169` (any operand with a symbol of key 169) / `_dir+0@169` (that name and key); an unnamed subtree as `?h00c8`
    is its printed label, a plain substring."""
    m = re.fullmatch(r"(.*?)@(\d+)", text) or re.fullmatch(r"()#(\d+)", text)
    if m:
        return m.group(1) in nm and re.search(r"\bkey %s\b" % m.group(2), nm) is not None
    return text in nm


def site_events(f, site, pass_):
    out = []
    for e in f.events:
        if e[0] != "sort" or e[1] != pass_ or not site["lo"] <= e[3] <= site["hi"] or len(e[4]) < 2:
            continue
        nm = [opname(o) for o in e[4]]
        # an exact operand name wins over a substring match: with substrings only, `A>B` and `B>A` could pick the
        # same two operands (each name inside both) and print the same verdict for both directions (M&B feedback)
        ia = [i for i, x in enumerate(nm) if x == site["A"]] or [i for i, x in enumerate(nm) if op_match(x, site["A"])]
        ib = [i for i, x in enumerate(nm) if x == site["B"] and i not in ia[:1]] or \
            [i for i, x in enumerate(nm) if op_match(x, site["B"]) and i not in ia[:1]]
        if ia and ib:
            amb = len({i for i, x in enumerate(nm) if op_match(x, site["A"])} & {i for i, x in enumerate(nm) if op_match(x, site["B"])}) > 0 \
                and nm[ia[0]] != site["A"] and nm[ib[0]] != site["B"]
            if amb:
                sys.stderr.write("symkeys: --site %s is ambiguous at line %s: %r and %r both match %s; name the operands "
                                 "exactly\n" % (site["text"], e[3], site["A"], site["B"], ", ".join(nm)))
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


def wstr(w):
    return "SU%d n%d h%04x" % wparts(w) if w is not None else "?"


def wparts(w):
    return (w >> 24, (w >> 16) & 0xff, w & 0xffff)


def key_dependent(o):
    """Does the operand's weight move with a symbol key, as the model follows it (op_weight: a named leaf, a field
    view, a memory base, a temporary that hashes by its own key, a subtree's first sources)?  Not a constant, an
    unnamed subtree (`?h00c8`: its leaves are not logged), or a temporary that hashes by its definition."""
    if o["w"] is None:
        return True
    syms = [x for k in ("sym", "dsym", "dsym2") for x in (o.get(k),) if x is not None]
    syms += [o[k][2] for k in ("mem", "mem2") if o.get(k) and o[k][2] is not None]
    for d in (1, 5, 33, 64, 2049):
        if op_weight(o, {id(x): x.key + d for x in syms}) != o["w"]:
            return True
    return False


def decide(ops, ia, ib):
    """Which component of the weight (SU << 24 | count << 16 | hash) puts operand ia relative to ib:
    -> (component, satisfied, reachable): `SU` / `count` / `hash` / `equal`; satisfied = ia sorts before ib now;
    reachable = a symbol-key edit can still give A before B (the hash decides and some operand's hash moves with a key)."""
    A, B = ops[ia], ops[ib]
    if A["w"] is None or B["w"] is None:
        return "?", ia < ib, True
    sa, na, ha = wparts(A["w"])
    sb, nb, hb = wparts(B["w"])
    comp = "SU" if sa != sb else "count" if na != nb else "hash" if ha != hb else "equal"
    sat = ia < ib
    if comp in ("SU", "count"):
        return comp, sat, False
    return comp, sat, key_dependent(A) or key_dependent(B)


def need_lines(A, B, comp):
    """What would have to change for A to sort before B when SU / count (not the hash) decides."""
    sa, na, ha = wparts(A["w"])
    sb, nb, hb = wparts(B["w"])
    need = []
    if comp == "SU":
        need.append("raise A to SU%d (count / hash then decide: B is n%d h%04x)" % (sb, nb, hb))
        need.append("raise A to SU%d (wins outright)" % (sb + 1))
        need.append("lower B to SU%d" % sa + " (then count: A n%d vs B n%d at the same SU)" % (na, nb))
    else:
        need.append("raise A's count to %d or more (or the same count and a larger hash)" % nb)
        need.append("lower B's count below %d" % na)
    return need


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
    print(line_note(f))
    for st, e, ia, ib in evs:
        print("  %-28s line %d %s: %s before %s now: %s" % (st["text"], e[3], e[2], opname(e[4][ia]), opname(e[4][ib]),
                                                          site_state(e, ia, ib, base)))
    blocked = []
    for st, e, ia, ib in evs:
        comp, sat, reach = decide(e[4], ia, ib)
        if not sat and not reach:
            blocked.append((st, e, ia, ib, comp))
    if blocked:
        print("# %d of %d wanted orders cannot be given by symbol keys (the weight is SU << 24 | count << 16 | hash; "
              "keys move only the hash):" % (len(blocked), len(evs)))
        for st, e, ia, ib, comp in blocked:
            A, B = e[4][ia], e[4][ib]
            if comp in ("SU", "count"):
                gap = (wparts(B["w"])[0] - wparts(A["w"])[0], "SU") if comp == "SU" else \
                    (wparts(B["w"])[1] - wparts(A["w"])[1], "node(s)")
                print("#   %s line %d %s: %s (%s) vs %s (%s): decided by %s, A is %d %s behind" % (
                    st["text"], e[3], e[2], opname(A), wstr(A["w"]), opname(B), wstr(B["w"]), comp, gap[0], gap[1]))
                for x in need_lines(A, B, comp):
                    print("#       to change it: %s" % x)
            else:
                print("#   %s line %d %s: %s (%s) vs %s (%s): decided by the %s, which no symbol key moves "
                      "(neither operand's hash follows a key: an unnamed subtree, a constant, or a temporary that hashes "
                      "by its definition)" % (
                          st["text"], e[3], e[2], opname(A), wstr(A["w"]), opname(B), wstr(B["w"]), comp))
        print("#   source rewrites that change the deciding property: tools/sulever.py <SRC | --from EXPORT.json> "
              "--func <F> --site '%s' [--spell '<operand text>']" % blocked[0][0]["text"])
        evs = [x for x in evs if not any(x[1] is b[1] and x[2] == b[2] and x[3] == b[3] for b in blocked)]
        if not evs:
            print("# nothing left for keys to do: no key search")
            return []
        print("# searching keys for the remaining %d sorts" % len(evs))
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
    pts = candidate_points(f, last, not a.no_inline, "hash" in classes, a.per_expansion) + [("@" + anchor_of(f, last), "just before %s itself" % label(last), None)]
    ns = [int(x) for x in a.n.split(",")] if a.n else list(range(1, a.max + 1))
    if f._skipped_inl:
        print("# %d inline-expansion start points left out: no statement of the caller falls between them and the "
              "previous expansion (--per-expansion keeps them)" % f._skipped_inl)
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
    if not a.per_expansion:
        # an edit inside an inline helper's body is made in every expansion of it (one piece of source)
        seen_c, c2 = set(), []
        for e_, n_ in cand:
            e_ = expand_inline(f, e_)
            if e_ not in seen_c:
                seen_c.add(e_)
                c2.append((e_, n_))
        cand = c2
    # a sub that removes a symbol the code still reads is no source change (the sites' own operands included)
    keep = [s_ for s_ in f.syms if id(s_) in deps]
    nc = len(cand)
    cand = [c for c in cand if not c[0].startswith("sub:") or realizable(f, [c[0]], keep)]
    # first references in another order: each named local of the function the sites depend on, moved before an
    # earlier named local (declare / first use it earlier)
    locs = [s_ for s_ in f.syms if s_.user and s_.cls == "frame" and s_.kind == 4 and s_.inl is None and s_.name
            and not s_.name.startswith("$")]
    for v in locs:
        if id(v) not in deps:
            continue
        for w in locs:
            if w.idx < v.idx:
                cand.append(("swap:%s,%s" % (label(v), label(w)), 1))
    if nc != len(cand):
        print("# %d sub edits dropped (they remove a symbol the code reads), %d swaps added" % (
            nc - sum(1 for c in cand if not c[0].startswith("swap:")), sum(1 for c in cand if c[0].startswith("swap:"))))
    global _SW
    _SW = score
    njobs = a.jobs or min(8, os.cpu_count() or 1) if _CAN_FORK else 1
    print("# %d single edits to try, %d worker processes" % (len(cand), njobs), flush=True)
    if njobs > 1 and len(cand) > 50 and _CAN_FORK:
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
                if (ea.startswith("sub:") or eb.startswith("sub:")) and not realizable(f, [ea, eb], keep):
                    continue
                combos.append([ea, eb])
        if a.depth >= 3:
            top3 = pool[:max(6, a.pool // 3)]
            for i in range(len(top3)):
                for j in range(i + 1, len(top3)):
                    for k in range(j + 1, len(top3)):
                        c3 = [top3[i][3][0], top3[j][3][0], top3[k][3][0]]
                        if any(x.startswith("sub:") for x in c3) and not realizable(f, c3, keep):
                            continue
                        combos.append(c3)
        for c in combos:
            r = score(c)
            if r is not None:
                res.append((r[0], 0, -sum(1 if x.startswith("swap:") else int(x.rsplit(":", 1)[1]) for x in c), c, r))
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
            best.append((rr[0], -len(named_drops(f, r[3])), -len(rr[2]), r[2], r[3], rr))
        if len(best) >= max(3 * a.top, 30):
            break
    full = sum(1 for r in res if r[0] == len(evs))
    res = best
    res.sort(key=lambda x: (x[0], x[1], x[2], -len(x[4]), x[3]), reverse=True)
    print("# %d edit sets satisfy all %d sorts; best (sites satisfied, then fewest named symbols removed, fewest other "
          "flips, fewest edits):" % (full, len(evs)))
    seen = set()
    shown = 0
    for nsat, _nd, nfl, nn, edits, (sat, st, other) in res:
        key = tuple(sorted(edits))
        if key in seen:
            continue
        seen.add(key)
        print("%d/%d sites  %3d other flips  %s" % (sat, len(evs), len(other), " ".join(edits)))
        lv = sorted({edit_level(f, x) for x in edits})
        if lv != ["reader"]:
            print("    level: %s" % "; ".join(lv))
        gone = [label(x) for x in named_drops(f, edits)]
        if gone:
            print("    removes: %s (each must be an unread local in the source)" % ", ".join(gone))
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
    njobs = a.jobs or min(8, os.cpu_count() or 1) if _CAN_FORK else 1
    print("# %d edits to try, %d worker processes" % (len(jobs), njobs), flush=True)
    if njobs > 1 and len(jobs) > 50 and _CAN_FORK:
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


IMM_ROLES = ("immnew", "immwalk", "immfree", "newgr")    # keys --imm (a build table's optional roles)


def trace(src, cl, imm=False):
    roles = [r for r in HOOKS if imm or r not in IMM_ROLES]
    ROLE.clear()
    ROLE.update(enumerate(roles))
    img, hooks, rc, out, text = c2trace.run_trace(src, [HOOKS[r] for r in roles], cl, maxn=None,
                                                  func=False)   # unfiltered: keys are assigned outside F's compile
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
        ix = lambda y: idx.get(id(y)) if y is not None else None
        ords = [dict(e, ops=[dict(o, sym=ix(o["sym"]), srcs=[[a, b, ix(c)] for a, b, c in o.get("srcs", [])])
                             for o in e["ops"]]) for e in f.orders]
        out.append(dict(name=f.name, optidx=f.optidx, line0=f.line0, syms=syms, events=evs, imms=f.imms,
                        orders=ords))
    return out


def from_json(data):
    funcs = []
    for d in data:
        f = Func()
        f.name, f.optidx, f.line0 = d["name"], d["optidx"], d.get("line0", 0)
        f.imms = d.get("imms", [])
        f.orders = d.get("orders", [])
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
        for e in f.orders:
            for o in e["ops"]:
                o["sym"] = f.syms[o["sym"]] if o["sym"] is not None else None
                o["srcs"] = [[a, b, f.syms[c] if c is not None else None] for a, b, c in o.get("srcs", [])]
        funcs.append(f)
    return funcs


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cl = []
    if "--" in argv:
        i = argv.index("--")
        argv, cl = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("cmd", choices=("keys", "events", "whatif", "levers", "export", "sites", "compare", "tmps"))
    ap.add_argument("--before", help="tmps: list the temporaries allocated before this event (#N as levers "
                    "prints it in `sub:tmp@#N`, or a plain number), grouped by construct and line")
    ap.add_argument("--after", help="tmps: and at or after this event (#N)")
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
    ap.add_argument("--imm", action="store_true", help="keys (13.00.9044): also trace and print the immediate table "
                    "(constants / addresses held in registers: no key; their bucket, c1 number and globreg numbers)")
    ap.add_argument("--n")
    ap.add_argument("--vs", help="compare: the traced variant (export JSON) to predict")
    ap.add_argument("--match", help="levers: rank edits by operand orders that differ from this export")
    ap.add_argument("--no-inline", action="store_true")
    ap.add_argument("--per-expansion", action="store_true", help="levers: keep an edit inside an inline expansion to that "
                    "one expansion (no source construct does that: a helper's body is one piece of source)")
    ap.add_argument("--source", help="sites: the source file, to print the source line a `$o` temporary was made for "
                    "(default: <src> when it is a file)")
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
        if a.imm and not all(r in HOOKS for r in IMM_ROLES):
            sys.exit("--imm: this build table has no immediate-table hooks (13.00.9044 only)")
        funcs = trace(a.src, cl, imm=a.imm)
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
            if not g:      # the edit changed the signature (added / removed parameters): the same undecorated name
                base = re.sub(r"^\?([^@]+)@.*$", r"\1", f.name)
                g = [x for x in fb if re.sub(r"^\?([^@]+)@.*$", r"\1", x.name) == base]
                g = g if len(g) == 1 else []
            print("#### %s  edits: %s%s" % (f.name, " ".join(a.edits) or "(none)",
                                           "  (vs %s)" % g[0].name if g and g[0].name != f.name else ""))
            if g:
                compare(f, a.edits, g[0])
            else:
                print("# %s not in %s" % (f.name, a.vs))
        return 0
    for f in sel:
        if a.cmd == "keys":
            print_keys(f, a)
        elif a.cmd == "sites":
            print_sites(f, a)
        elif a.cmd == "events":
            print_events(f)
        elif a.cmd == "tmps":
            print_tmps(f, a)
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
