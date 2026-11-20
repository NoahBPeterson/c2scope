#!/usr/bin/env python3
"""Effect mining: the inverse of the codegen models, measured.  Doc: docs/c2/effectmine.md

usage: effectmine.py mine  <tu.cpp> --func F [--asm-vs ORIG.s] [--want EFFECT ..] [--max-compiles 400]
                           [--features regs,scratch,slots,keys,sched,loads,frontend] [--classes RE] [--skip RE] [--eager]
                           [--eager-feats F,..] [--focus LO-HI,..]
                           [--pairs 16] [--depth 2 --depth-beam 4 --depth-compiles 60] [--jobs 24]
                           [--keep-variants 12] [--list] [--fresh] [-- <cl args>]
       effectmine.py query <tu.cpp> --func F --want EFFECT [..] [--asm-vs ORIG.s] [--verify N] [-- <cl args>]
       effectmine.py table [--build B] [--type T] [--class RE] [--min-n 3] [--all-types] [--by build|vtype|..]
       effectmine.py stats
   --c2 13.10 / 13.10.3052 / 15.00 as on every tool.

The models (rasim, rasim scratch, c2slots, symkeys, c2sched, loadcse) say WHAT c2 must do differently; realize.py
and rwsweep.py hold a hand-written catalogue of C++ spellings for some of those answers.  This tool measures the map
from source to model instead of writing it down: it generates a large, generic set of small meaning-preserving
mutations of one function and its translation unit, compiles every one TRACED (on the compile server; a compile
that would fall back to wine is refused), reads each model's input and decisions with the existing parsers, and
stores (mutation, class, context, effect) in a database that grows across functions and runs
(work/effectmine/effects.jsonl).

mine     generate the mutations (MUTATION CLASSES below), order them (classes that produced effects elsewhere first,
         with --want the sites nearest the wanted decision first, every class sampled), compile up to
         --max-compiles traced compiles, and record per variant:
           C  the final code against the base: `code` (registers masked), `bag` (same instructions in any order),
              `ident` (with registers), instruction count; with --asm-vs the bdiff against the original
           R  register allocation (realize.Delta: ref/unref/extend/drop/var/gone/P/tmp/interf/copy/pref/tie, the
              registers each value got, and `before:A:B` for colouring-order flips)
           S  scratch picks (realize.PickDelta: ins:N, drop:N, picks whose register changed)
           L  stack slots (realize.SlotDelta: uses, visiting position, conflicts, new/gone elements, offsets)
           K  symbol keys (realize.key_delta_f: allocations per pool class, named keys moved, operand orders flipped)
           Q  scheduler issue order (c2sched: regions whose input changed, instruction pairs issued in the other order)
           M  load reuse (loadcse: loads per location at Color and in the final code, read-modify-writes formed)
         R and C come from one compile (the regs trace); S, L, K, Q, M are one compile each and are run only when
         the regs trace shows a change (IL at Color, allocation input, or code), unless --eager or the feature is
         named by --want. A variant whose regs trace is identical to the base is `null`.
         The helper templates a variant needs (hs_id, rz_sub, ..) are declarations before the function, which on
         13.10 alone can move a scheduling tie (Mount & Blade 0x4b9110): each helper set is compiled once on its
         own (`decl.helpers`) and a variant's effect is measured against that control, not the bare base.
         With --want, the answers follow (as `query`). Then depth 2 (--depth 2, default): the --depth-beam best
         code-neutral variants that changed something (a change naming a wanted variable first) become bases, the
         generators run again on their text near the wanted lines, and up to --depth-compiles second edits are
         measured (class `chain`). Then pairs of independently helpful variants (--pairs, their own budget).
query    given wanted effects (the realize vocabulary below, or `bdiff`), rank (1) this function's own measured
         variants that produced them (database records of the same source, build and flags; mine first if there
         are none), then (2) the mutation classes that produced that effect type elsewhere, by measured rate; with
         --verify N the instances of those classes in this function that were not measured yet are compiled
         (N traced compiles at most), then pairs.
table    the reliability table: per mutation class, how often each effect type occurred (all builds, or --build),
         and how often it occurred with the instructions unchanged (registers masked).
stats    database size per function and build.

EFFECT (--want), as the models print them:
  ins:N  drop:N             a scratch pick more before pick N / pick N gone (rasim scratch)
  uses:X=N  pos:X=P  conf+:A,B  conf-:A,B     slot what-ifs (c2slots.py what; X as it names elements: _x$, #3, tv12)
  ref:V:B unref:V:B extend:V:B drop:V:B var:V:B P:B:+1   allocation edits (rasim need / want)
  pref:V:REG  before:A:B  A>B  tie:A>B        a register preference / colouring order (A coloured before B)
  reg:V=REG                 V ends in REG
  add:frame@V:n  sub:frame@V:n  keys:frame:+n     symbol keys (symkeys levers)
  sched:A>B                 instruction A issued before B (substrings of the scheduler's instruction text)
  load:LOC:N                N loads of LOC in the final code (loadcse location, e.g. [_this+16])
  bdiff                     fewer bytes against --asm-vs
MUTATION CLASSES: `effectmine.py mine ... --list` prints every generated instance with its class. Families: rwsweep's
  (wrap, alias, swap, split, touch, cflow, paren, guard, const, unalias, subst), realize's constructs (copy, uncopy,
  headdup, headhoist, taildup, tailhoist, eqif, eqif2, uneqif, helperbind), and this tool's own: decl.<kind>.<where>
  (N dummy declarations of a kind, before the function or at the top of the unit), deffn (N dummy function
  definitions before it), move (the function moved before the previous definition / to the end of the unit),
  pad (unused locals), constq (const added / removed), width (int <-> long), commute (operands of a commutative
  operator exchanged), cmpflip (a < b as b > a), argswap (memcmp / strcmp operands where only == 0 is used), compound (x op= y <-> x = x op y), incdec, nullcmp, index
  (a[i] <-> *(a + i)), wrapx (const-reference, by-value struct and doubled inline wrappers), subobj (a member through
  an inline method of a sub-object), retwrap, scope (a block around a declaration and the rest of its block), hop
  (a statement moved over two independent ones), declhoist / declsink / merge (declaration placement).

Outputs: work/effectmine/effects.jsonl (one record per measured variant; never pruned), work/effectmine/texts/
(each base source, gzipped, so a record's edits can be re-applied), work/effectmine/runs/<tu>-<hash>[@build]/<run>/
(per run: the base and the --keep-variants best variants, sources and listings; traces are deleted once read).
"""
import argparse, collections, difflib, gzip, hashlib, itertools, json, os, random, re, shutil, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from image import ROOT
import c2trace, c2regs, tsweep, rwsweep, helpersweep, realize

DBDIR = os.environ.get("EFFECTMINE_DIR") or os.path.join(ROOT, "work", "effectmine")     # tests use their own
DB = os.path.join(DBDIR, "effects.jsonl")
TEXTS = os.path.join(DBDIR, "texts")
RUNS = os.path.join(DBDIR, "runs")
FEATURES = ("regs", "scratch", "slots", "keys", "sched", "loads", "frontend")
MODEL_OF = {"scratch": "S", "slots": "L", "keys": "K", "sched": "Q", "loads": "M", "frontend": "F"}


# ---------------------------------------------------------------- no wine: traced compiles go to the compile server

class RemoteDeclined(RuntimeError):
    pass


_real_run_tool = c2trace.run_tool
_LOCAL = []


def _guarded_run_tool(args, trace=True, extra_env=None):
    """c2trace.run_trace calls this only when the compile server declined (or none is configured): the compile would
    run under wine here. Refused when a server is configured, unless EFFECTMINE_ALLOW_LOCAL=1."""
    if trace and os.environ.get("EFFECTMINE_ALLOW_LOCAL") != "1" and c2trace.remote_configured():
        _LOCAL.append(1)
        raise RemoteDeclined("the compile server declined a traced compile (it would run under wine here): refused; "
                             "C2TRACE_REMOTE_DEBUG=1 says why; EFFECTMINE_ALLOW_LOCAL=1 compiles here")
    return _real_run_tool(args, trace, extra_env)


c2trace.run_tool = _guarded_run_tool


# ---------------------------------------------------------------- helpers the rewrites use

HELPERS = collections.OrderedDict([
    ("hs_bind", "template<class HsT> __forceinline HsT& hs_bind(HsT& x) { HsT& r = x; return r; }"),
    ("hs_id", "template<class HsT> __forceinline HsT hs_id(HsT x) { return x; }"),
    ("hs_cp", "template<class HsT> __forceinline HsT hs_cp(HsT x) { HsT r = x; return r; }"),
    ("hs_nop", "template<class HsT> __forceinline void hs_nop(HsT) { }"),
    ("hs_nopr", "template<class HsT> __forceinline void hs_nopr(const HsT&) { }"),
    ("rz_g", "template<class RzT> struct rz_g { static RzT v; };"),
    ("rz_keep", "template<class RzT> __forceinline void rz_keep(RzT x) { rz_g<RzT>::v = x; }"),
    ("rz_ret", "template<class RzT> RzT rz_ret();"),
    ("rz_ext", "extern int rz_ext(int);"),
    ("rz_sub", "template<class RzT> struct rz_sub { RzT v; __forceinline RzT& get() { return v; } };"),
    ("em_cr", "template<class EmT> __forceinline const EmT& em_cr(const EmT& x) { return x; }"),
    ("em_v", "template<class EmT> struct em_v { EmT v; __forceinline em_v(const EmT& x) : v(x) { } "
             "__forceinline EmT get() const { return v; } };"),
    ("em_mkv", "template<class EmT> __forceinline em_v<EmT> em_mkv(const EmT& x) { return em_v<EmT>(x); }"),
    ("em_idr", "template<class EmT> __forceinline EmT em_idr(const EmT& x) { EmT r(x); return r; }"),
])
NEEDS = {"rz_keep": ["rz_g"], "em_mkv": ["em_v"]}
HELPER_RE = re.compile(r"\b(%s)\b" % "|".join(HELPERS))


def helpers_used(edits, tail):
    used = set()
    for e in edits:
        used |= set(HELPER_RE.findall(e[2]))
    used |= set(HELPER_RE.findall(tail or ""))
    for h in list(used):
        used |= set(NEEDS.get(h, []))
    return used


def defined_helpers(text):
    out = set()
    for nm in HELPERS:
        if re.search(r"(?:inline|__forceinline)[^;{]*\b%s\s*\(" % nm, text) or re.search(r"\bstruct\s+%s\b" % nm, text) \
                or re.search(r"\bextern\s+int\s+%s\s*\(" % nm, text):
            out.add(nm)
    return out


def prelude(hs, have):
    """The helper definitions of the set hs (minus those the source defines), one line: lines after it shift by one."""
    ls = [HELPERS[h] for h in HELPERS if h in hs and h not in have]
    return (" ".join(ls) + "\n") if ls else ""


# ---------------------------------------------------------------- the source: top-level positions

def _toplevel(text, k):
    """(construct start, opaque depth at k) for position k: the start of the top-level declaration that contains k
    (namespaces and extern "C" blocks are transparent; a class body is not), skipping comments, strings and
    preprocessor lines."""
    i, n = 0, len(text)
    stack = []                  # True = opaque brace
    boundary = 0                # position after the last ; } { at opaque depth 0
    outer = None                # boundary when the outermost opaque brace opened
    lastword = []
    bol = True
    while i < k and i < n:
        ch = text[i]
        if bol and ch == "#":
            j = i
            while True:          # a directive, with backslash continuations
                e = text.find("\n", j)
                if e < 0:
                    e = n
                if e > 0 and text[e - 1] == "\\":
                    j = e + 1
                    continue
                break
            i = e
            continue
        if ch == "\n":
            bol = True
            i += 1
            continue
        if ch in " \t\r":
            i += 1
            continue
        bol = False
        if text.startswith("//", i):
            e = text.find("\n", i)
            i = n if e < 0 else e
            continue
        if text.startswith("/*", i):
            e = text.find("*/", i + 2)
            i = n if e < 0 else e + 2
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            lastword.append(text[i:j])
            lastword = lastword[-4:]
            i = j
            continue
        if ch == "{":
            opaque = not ("namespace" in lastword or ('extern' in lastword and text[max(0, i - 12):i].count('"') >= 2))
            if opaque and not any(stack):
                outer = boundary
            stack.append(opaque)
            if not any(stack):
                boundary = i + 1
        elif ch == "}":
            if stack:
                stack.pop()
            if not any(stack):
                boundary = i + 1
        elif ch == ";":
            if not any(stack):
                boundary = i + 1
        if ch in "{};":
            lastword = []
        i += 1
    depth = sum(1 for s in stack if s)
    start = boundary if depth == 0 else outer
    return _skip_ws(text, start if start is not None else 0, k), depth


def _skip_ws(text, i, k):
    """First position >= i that is not space, a comment or a preprocessor line (not beyond k)."""
    n = len(text)
    while i < k:
        if text[i] in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            e = text.find("\n", i)
            i = n if e < 0 else e + 1
        elif text.startswith("/*", i):
            e = text.find("*/", i + 2)
            i = n if e < 0 else e + 2
        elif text[i] == "#" and (i == 0 or text[i - 1] == "\n"):
            e = text.find("\n", i)
            i = n if e < 0 else e + 1
        else:
            break
    return min(i, k)


# ---------------------------------------------------------------- generator context (realize's generators need it)

class GenCtx:
    """What realize.py's construct generators read: the text, the target's Body, the base Fn. No compile."""

    def __init__(self, text, func, base):
        self.rw, self.hs = rwsweep, helpersweep
        self.text = text
        self.a = argparse.Namespace(constructs=None, limit=None)
        self.fn, self.cls = realize.find_def(text, func, helpersweep, rwsweep)
        self.B = rwsweep.Body(text, self.fn, self.cls)
        realize._more_decls(self.B, rwsweep)
        self.line0 = text.count("\n", 0, self.fn[4])
        self.base = base
        self.func = func

    lineoff = realize.Ctx.lineoff
    stmts = realize.Ctx.stmts


# ---------------------------------------------------------------- mutation generators

def _cls_of(fam, desc):
    """A mutation class from rwsweep's / realize's (family, description)."""
    d = desc
    if fam == "wrap":
        m = re.match(r"(arg )?(hs_\w+)\((.*)", d)
        if m:
            what = "arg" if m.group(1) else ("this" if m.group(3).startswith("this)") else
                                             "chain" if re.search(r"->|\.|\[", m.group(3).split(")")[0]) else "var")
            return "wrap.%s.%s" % (what, m.group(2))
    if fam == "alias":
        return "alias." + d.split()[0]
    if fam == "touch":
        return "touch." + d.split()[0]
    if fam == "cflow":
        for k, v in (("swap arms", "swaparms"), ("empty then", "emptythen"), ("copied into both", "taildup"),
                     ("for(;;)", "forever"), ("for(;cond;)", "for"), ("do-while", "dowhile"), ("goto", "goto")):
            if k in d:
                return "cflow." + v
    if fam == "paren":
        m = re.match(r"(unwrap )?(\w+)", d)
        return "paren.%s%s" % ("unwrap." if m.group(1) else "", m.group(2))
    if fam == "const":
        return "const." + d.split()[0].replace("-", "")
    if fam == "guard":
        return "guard." + d.split()[0]
    if fam == "eqif":
        return "eqif.call" if "with a call" in d else ("eqif.decl" if d.startswith("`") and "; if" in d else "eqif.assign")
    if fam in ("headdup", "taildup"):
        return fam + (".mkelse" if "else arm is made" in d else "")
    return fam


def _prim_end(B, i):
    """One past a primary chain starting at token i (identifier / number / this / paren group, then . -> [ ] ( ))."""
    n = len(B.toks)
    t = B.toks[i][0] if i < n else ""
    if t == "(":
        k = _match(B, i)
        if k is None:
            return None
        k += 1
    elif re.match(r"[A-Za-z_]\w*$|\d", t) and (t not in rwsweep.KEYWORDS or t == "this"):
        k = i + 1
    else:
        return None
    while k < n:
        x = B.toks[k][0]
        if x in (".", "->") and k + 1 < n and re.match(r"[A-Za-z_]\w*$", B.toks[k + 1][0]):
            k += 2
        elif x in ("[", "("):
            m = _match(B, k)
            if m is None:
                return None
            k = m + 1
        else:
            break
    return k


def _match(B, i):
    """Index of the bracket closing the one at token i."""
    o = B.toks[i][0]
    c = {"(": ")", "[": "]", "{": "}"}[o]
    d = 0
    for k in range(i, len(B.toks)):
        x = B.toks[k][0]
        d += x == o
        d -= x == c
        if d == 0:
            return k
    return None


def _match_back(B, j):
    c = B.toks[j][0]
    o = {")": "(", "]": "["}[c]
    d = 0
    for k in range(j, -1, -1):
        x = B.toks[k][0]
        d += x == c
        d -= x == o
        if d == 0:
            return k
    return None


def _prim_start(B, j):
    """Start token of a primary chain ending at token j (inclusive)."""
    k = j
    while k >= 0:
        x = B.toks[k][0]
        if x in (")", "]"):
            o = _match_back(B, k)
            if o is None:
                return None
            if o > 0 and (re.match(r"[A-Za-z_]\w*$", B.toks[o - 1][0]) and B.toks[o - 1][0] not in rwsweep.KEYWORDS
                          or B.toks[o - 1][0] in (")", "]")):
                k = o - 1
                continue
            if x == "]":
                return None
            return o if o == 0 or B.toks[o - 1][0] not in (".", "->") else None
        if re.match(r"[A-Za-z_]\w*$|\d", x) and (x not in rwsweep.KEYWORDS or x == "this"):
            if k > 1 and B.toks[k - 1][0] in (".", "->"):
                k -= 2
                continue
            if k > 0 and B.toks[k - 1][0] == "::":
                return None
            return k
        return None
    return None


SAFE_L = {"(", ",", ";", "{", "}", "=", "return", "&&", "||", "?", ":", "!"} | rwsweep.ASSIGN
SAFE_R = {")", ",", ";", "&&", "||", "?", ":", "]"}
COMMUTE = {"+": "+", "*": "*", "&": "&", "|": "|", "^": "^", "==": "==", "!=": "!=", "<": ">", ">": "<", "<=": ">=",
           ">=": "<="}


def gen_commute(c):
    B = c.B
    for i in range(1, len(B.toks) - 1):
        op = B.toks[i][0]
        if op not in COMMUTE or B.toks[i + 1][0] in ("=",):
            continue
        a = _prim_start(B, i - 1)
        z = _prim_end(B, i + 1)
        if a is None or z is None or a == 0 or z >= len(B.toks):
            continue
        if B.toks[a - 1][0] not in SAFE_L or B.toks[z][0] not in SAFE_R:
            continue
        if B.toks[a - 1][0] == "(" and a >= 2 and B.toks[a - 2][0] in ("for",):
            continue
        if op in ("*", "&") and not (any(B.toks[q][0] in B.vars for q in range(a, i)) or
                                     re.match(r"\d", B.toks[a][0]) or B.toks[i - 1][0] in (")", "]")):
            continue                                   # `T * p` / `T & r` in a declaration
        if op in ("<", ">") and (B.toks[a][0] in ("template",) or re.match(r"[A-Z]", B.toks[a][0]) and
                                 B.toks[a][0] not in B.vars):
            continue                                   # template arguments
        L, R = B.src(a, i), B.src(i + 1, z)
        kind = "cmpflip" if op in ("<", ">", "<=", ">=") else "commute"
        yield (kind, "`%s %s %s` as `%s %s %s` at line +%d" % (L[:24], op, R[:24], R[:24], COMMUTE[op], L[:24],
                                                               c.lineoff(i)),
               [(B.toks[a][1], B.toks[z - 1][2], "%s %s %s" % (R, COMMUTE[op], L))], "",
               dict(op=op, i=i))


def gen_compound(c):
    B = c.B
    for k in range(len(B.starts)):
        s = rwsweep._stmt(B, k)
        if not s:
            continue
        i, j = s
        toks = [B.toks[q][0] for q in range(i, j)]
        eq = next((q for q, t in enumerate(toks) if t in rwsweep.ASSIGN), None)
        if eq is None or eq == 0 or any(t in ("(", "++", "--", ",") for t in toks[:eq]) or rwsweep.DECL.match(
                B.text, B.toks[i][1]):
            continue
        lhs = B.src(i, i + eq)
        rhs_a, rhs_z = i + eq + 1, j - 1
        if rhs_a >= rhs_z:
            continue
        rhs = B.src(rhs_a, rhs_z)
        op = toks[eq]
        if op != "=" and op[:-1] in ("+", "-", "*", "&", "|", "^", "<<", ">>"):
            yield ("compound.expand", "`%s %s ..` as `%s = %s %s (..)` at line +%d" % (lhs[:24], op, lhs[:20], lhs[:20],
                                                                                  op[:-1], c.lineoff(i)),
                   [(B.toks[i][1], B.toks[j - 1][2], "%s = %s %s (%s);" % (lhs, lhs, op[:-1], rhs))], "", dict(i=i))
        elif op == "=":
            n = eq
            if toks[eq + 1:eq + 1 + n] == toks[:eq] and eq + 1 + n < len(toks) - 1:
                bop = toks[eq + 1 + n]
                if bop in ("+", "-", "*", "&", "|", "^", "<<", ">>"):
                    ra = i + eq + 2 + n
                    z = _prim_end(B, ra)
                    if z == j - 1:
                        yield ("compound.fold", "`%s = %s %s ..` as `%s %s= ..` at line +%d" % (
                            lhs[:20], lhs[:20], bop, lhs[:20], bop, c.lineoff(i)),
                               [(B.toks[i][1], B.toks[j - 1][2], "%s %s= %s;" % (lhs, bop, B.src(ra, z)))], "", dict(i=i))


def gen_incdec(c):
    B = c.B
    for k in range(len(B.starts)):
        s = rwsweep._stmt(B, k)
        if not s:
            continue
        i, j = s
        toks = [B.toks[q][0] for q in range(i, j)]
        if len(toks) == 3 and toks[1] in ("++", "--") and re.match(r"[A-Za-z_]\w*$", toks[0]):
            v, op = toks[0], toks[1]
        elif len(toks) == 3 and toks[0] in ("++", "--") and re.match(r"[A-Za-z_]\w*$", toks[1]):
            v, op = toks[1], toks[0]
        else:
            continue
        txt = B.src(i, j)
        for alt in (op + v + ";", v + op + ";", "%s %s= 1;" % (v, op[0]), "%s = %s %s 1;" % (v, v, op[0])):
            if alt.replace(" ", "") != txt.replace(" ", ""):
                yield ("incdec", "`%s` as `%s` at line +%d" % (txt, alt, c.lineoff(i)),
                       [(B.toks[i][1], B.toks[j - 1][2], alt)], "", dict(i=i))


def gen_nullcmp(c):
    B = c.B
    for i in range(len(B.toks) - 2):
        if B.toks[i][0] not in ("if", "while") or B.toks[i + 1][0] != "(":
            continue
        pc = _match(B, i + 1)
        if pc is None:
            continue
        a = i + 2
        neg = B.toks[a][0] == "!"
        z = _prim_end(B, a + 1 if neg else a)
        if z == pc:
            e = B.src(a + 1 if neg else a, z)
            yield ("nullcmp.add", "`%s` as `%s %s 0` at line +%d" % (B.src(a, z)[:30], e[:30], "==" if neg else "!=",
                                                                      c.lineoff(i)),
                   [(B.toks[a][1], B.toks[z - 1][2], "%s %s 0" % (e, "==" if neg else "!="))], "", dict(i=i))
            continue
        z = _prim_end(B, a)
        if z is not None and z + 2 == pc and B.toks[z][0] in ("==", "!=") and B.toks[z + 1][0] in ("0", "NULL"):
            e = B.src(a, z)
            yield ("nullcmp.drop", "`%s` as `%s%s` at line +%d" % (B.src(a, pc)[:30], "!" if B.toks[z][0] == "==" else "",
                                                                   e[:30], c.lineoff(i)),
                   [(B.toks[a][1], B.toks[pc - 1][2], ("!" if B.toks[z][0] == "==" else "") + e)], "", dict(i=i))


def gen_index(c):
    B = c.B
    for v, uses in B.uses.items():
        ty = B.vars[v][0]
        if "*" not in ty or "&" in ty:
            continue
        for i in uses:
            if i + 1 < len(B.toks) and B.toks[i + 1][0] == "[":
                k = _match(B, i + 1)
                if k is None or (i and B.toks[i - 1][0] in (".", "->", "&")):
                    continue
                if k + 1 < len(B.toks) and B.toks[k + 1][0] in (".", "->", "["):
                    continue
                idx = B.src(i + 2, k)
                yield ("index", "`%s[%s]` as `*(%s + (%s))` at line +%d" % (v, idx[:20], v, idx[:20], c.lineoff(i)),
                       [(B.toks[i][1], B.toks[k][2], "*(%s + (%s))" % (v, idx))], "", dict(i=i))


def gen_wrapx(c):
    """Read uses of a variable or a member chain through other inline boundaries than rwsweep's wrap: a const
    reference (em_cr), a by-value struct with an inline getter (em_mkv(x).get()), a named copy through a reference
    (em_idr), and hs_id twice."""
    B = c.B
    seen = set()
    for v, uses in B.uses.items():
        for i in uses:
            for j in sorted({i + 1, B.chain_end(i)}):
                if (i, j) in seen or B.written(i, j):
                    continue
                if j < len(B.toks) and B.toks[j][0] in ("(", "::") or (i and B.toks[i - 1][0] in ("&", "::")):
                    continue
                seen.add((i, j))
                expr = B.src(i, j)
                what = "this" if expr == "this" else ("chain" if j > i + 1 else "var")
                for nm, fmt in (("em_cr", "em_cr(%s)"), ("em_mkv", "em_mkv(%s).get()"), ("em_idr", "em_idr(%s)"),
                                ("hs_id2", "hs_id(hs_id(%s))")):
                    yield ("wrapx.%s.%s" % (what, nm), "%s at line +%d" % (fmt % expr[:30], c.lineoff(i)),
                           [(B.toks[i][1], B.toks[j - 1][2], fmt % expr)], "", dict(i=i))


def gen_subobj(c):
    B = c.B
    done = set()
    for i in range(1, len(B.toks) - 1):
        if B.toks[i][0] not in ("->", ".") or not re.match(r"[A-Za-z_]\w*$", B.toks[i + 1][0]):
            continue
        a = _prim_start(B, i - 1)
        if a is None or (a and B.toks[a - 1][0] in ("&", ".", "->")):
            continue
        z = i + 2
        if z < len(B.toks) and B.toks[z][0] in ("(", ".", "->", "["):
            continue
        m = B.toks[i + 1][0]
        ty = c._mtype.get(m) if m in c._mtype else c._mtype.setdefault(m, realize._member_type(c, m))
        if not ty or "&" in ty:
            continue
        chain = B.src(a, z)
        if (a, z) in done:
            continue
        done.add((a, z))
        yield ("subobj", "`%s` through rz_sub<%s>::get() at line +%d" % (chain[:30], ty, c.lineoff(i)),
               [(B.toks[a][1], B.toks[z - 1][2], "((rz_sub<%s >*)&%s)->get()" % (ty, chain))], "", dict(i=a))


SYMCMP = {"memcmp", "strcmp", "strncmp", "wcscmp", "wcsncmp", "_stricmp", "_strnicmp", "stricmp", "strnicmp",
          "_wcsicmp", "_mbscmp", "lstrcmpA", "lstrcmpW", "lstrcmpiA", "lstrcmpiW"}


def gen_argswap(c):
    """The two compared operands of memcmp / strcmp / .. exchanged where only equality with 0 is used (`!f(a, b)`,
    `f(a, b) == 0`, `!= 0`, or the whole condition): the same truth value, the operands set up in another order."""
    B = c.B
    for i in range(len(B.toks) - 3):
        if B.toks[i][0] not in SYMCMP or B.toks[i + 1][0] != "(" or (i and B.toks[i - 1][0] in (".", "->", "::")):
            continue
        z = _match(B, i + 1)
        if z is None:
            continue
        args, a0, d = [], i + 2, 0
        for k in range(i + 2, z + 1):
            x = B.toks[k][0]
            d += x in ("(", "[")
            d -= x in (")", "]")
            if (x == "," and d == 0) or k == z:
                args.append((a0, k))
                a0 = k + 1
        if len(args) < 2:
            continue
        prv, nxt = B.toks[i - 1][0], B.toks[z + 1][0] if z + 1 < len(B.toks) else ""
        eq0 = nxt in ("==", "!=") and z + 2 < len(B.toks) and B.toks[z + 2][0] in ("0", "NULL")
        cond = prv == "(" and i > 1 and B.toks[i - 2][0] in ("if", "while") and nxt == ")"
        if not (prv == "!" or eq0 or cond):
            continue
        (a1, b1), (a2, b2) = args[0], args[1]
        A1, A2 = B.src(a1, b1), B.src(a2, b2)
        yield ("argswap", "%s(%s, %s, ..) as %s(%s, %s, ..) at line +%d" % (B.toks[i][0], A1[:20], A2[:20], B.toks[i][0],
                                                                          A2[:20], A1[:20], c.lineoff(i)),
               [(B.toks[a1][1], B.toks[b1 - 1][2], A2), (B.toks[a2][1], B.toks[b2 - 1][2], A1)], "", dict(i=i))


def gen_retwrap(c):
    B = c.B
    for i in range(len(B.toks) - 2):
        if B.toks[i][0] != "return" or B.toks[i + 1][0] == ";":
            continue
        j = i + 1
        while j < len(B.toks) and B.toks[j][0] != ";":
            j += 1
        if j - i < 3:
            continue
        e = B.src(i + 1, j)
        for h in ("hs_id", "hs_cp"):
            yield ("retwrap." + h, "return %s(%s) at line +%d" % (h, e[:30], c.lineoff(i)),
                   [(B.toks[i + 1][1], B.toks[j - 1][2], "%s(%s)" % (h, e))], "", dict(i=i))


def _decl_at(c, st):
    m = rwsweep.DECL.match(c.B.text, c.B.toks[st][1])
    return m


SCAL = r"^(?:unsigned\s+|signed\s+)?(?:int|long|short|char|bool|float|double|__int64|size_t|DWORD|UINT|BYTE|WORD|BOOL)$"


def gen_constq(c):
    B = c.B
    for st in B.starts:
        m = _decl_at(c, st)
        if not m or m.group(2) not in B.vars or B.paren[st]:
            continue
        ty, v = m.group(1).strip(), m.group(2)
        if B.vars[v][1] < 0 or "&" in ty:
            continue
        if ty.startswith("const ") and m.group(3) == "=":
            yield ("constq.remove", "`%s %s` without const at line +%d" % (ty, v, c.lineoff(st)),
                   [(m.start(1), m.end(1), ty[6:] + " ")], "", dict(i=st))
            continue
        if m.group(3) != "=" or "const" in ty:
            continue
        if any(B.written(i, i + 1) for i in B.uses[v]) or any(B.toks[i - 1][0] == "&" for i in B.uses[v]):
            continue
        if not (re.match(SCAL, ty) or ty.endswith("*")):
            continue
        yield ("constq.add", "`const %s %s` at line +%d" % (ty, v, c.lineoff(st)),
               [(m.start(1), m.start(1), "const ")], "", dict(i=st))
        if ty.endswith("*"):
            yield ("constq.addptr", "`%s const %s` at line +%d" % (ty, v, c.lineoff(st)),
                   [(m.end(1) - 0, m.end(1), " const ")], "", dict(i=st))


WIDTH = {"int": "long", "long": "int", "unsigned int": "unsigned long", "unsigned": "unsigned long",
         "unsigned long": "unsigned int", "signed int": "long"}


def gen_width(c):
    B = c.B
    for st in B.starts:
        m = _decl_at(c, st)
        if not m or m.group(2) not in B.vars:
            continue
        ty = re.sub(r"\s+", " ", m.group(1).strip())
        if ty in WIDTH:
            yield ("width", "`%s %s` as `%s` at line +%d" % (ty, m.group(2), WIDTH[ty], c.lineoff(st)),
                   [(m.start(1), m.end(1), WIDTH[ty] + " ")], "", dict(i=st))


def gen_pad(c):
    B = c.B
    open_ = B.toks[0][2] if B.toks and B.toks[0][0] == "{" else None
    if open_ is not None:
        for n in (1, 2, 4, 8, 16):
            yield ("pad.init.top", "%d unused `int em_u = 0;` at the function's start" % n,
                   [(open_, open_, " " + " ".join("int em_u%d = 0;" % q for q in range(n)))], "", dict(i=0, n=n))
        yield ("pad.noinit.top", "unused `int em_u;` at the function's start", [(open_, open_, " int em_u0;")], "",
               dict(i=0, n=1))
    for k, st in enumerate(B.starts):
        if B.paren[st] or B.toks[st][0] in ("case", "default", "else", "}"):
            continue
        if B.toks[st - 1][0] == ")" or B.toks[st - 1][0] == "else":
            continue                                     # `if (c) S`: the local would become the arm
        pos = B.toks[st][1]
        yield ("pad.init", "unused `int em_u = 0;` before line +%d" % c.lineoff(st),
               [(pos, pos, "int em_u0 = 0; ")], "", dict(i=st, n=1))
        yield ("pad.vol", "unused `volatile int em_u = 0;` before line +%d" % c.lineoff(st),
               [(pos, pos, "volatile int em_u0 = 0; ")], "", dict(i=st, n=1))


def gen_scope(c):
    B = c.B
    for st in B.starts:
        m = _decl_at(c, st)
        if not m or B.paren[st]:
            continue
        blk = B.block[st]
        end = B.close.get(blk)
        if end is None or blk == 0 and B.toks[0][0] != "{":
            continue
        if B.toks[st - 1][0] in (")", "else"):
            continue
        if any(B.toks[q][0] in ("case", "default", "goto") for q in range(st, end)):
            continue
        yield ("scope", "{ } from `%s %s` to the end of its block at line +%d" % (m.group(1).strip()[:20], m.group(2),
                                                                                   c.lineoff(st)),
               [(B.toks[st][1], B.toks[st][1], "{ "), (B.toks[end][1], B.toks[end][1], "} ")], "", dict(i=st))


def gen_hop(c):
    B = c.B
    for k in range(len(B.starts) - 2):
        a, b, d = rwsweep._stmt(B, k), rwsweep._stmt(B, k + 1), rwsweep._stmt(B, k + 2)
        if not a or not b or not d or a[1] != b[0] or b[1] != d[0] or not (B.block[a[0]] == B.block[b[0]] == B.block[d[0]]):
            continue
        ia, wa, ma, mwa = rwsweep._facts(B, *a)
        ok = True
        for x in (b, d):
            ib, wb, mb, mwb = rwsweep._facts(B, *x)
            if wa & ib or wb & ia or (ma and mb and (mwa or mwb)):
                ok = False
        if not ok:
            continue
        sa, sb, sd = B.src(*a), B.src(*b), B.src(*d)
        yield ("hop.down", "`%s` moved below the next two statements at line +%d" % (sa[:30], c.lineoff(a[0])),
               [(B.toks[a[0]][1], B.toks[d[1] - 1][2], sb + " " + sd + " " + sa)], "", dict(i=a[0]))
        yield ("hop.up", "`%s` moved above the two statements before it at line +%d" % (sd[:30], c.lineoff(d[0])),
               [(B.toks[a[0]][1], B.toks[d[1] - 1][2], sd + " " + sa + " " + sb)], "", dict(i=d[0]))


def gen_declplace(c):
    B = c.B
    open_ = B.toks[0][2] if B.toks and B.toks[0][0] == "{" else None
    body = B.text[B.b:B.e]
    for k, st in enumerate(B.starts):
        m = _decl_at(c, st)
        if not m or B.paren[st] or m.group(2) not in B.vars:
            continue
        ty, v = m.group(1).strip(), m.group(2)
        if "&" in ty or "const" in ty or not (re.match(SCAL, re.sub(r"\s+", " ", ty)) or ty.endswith("*")):
            continue
        if len(re.findall(r"[;{}(]\s*(?:const\s+)?[\w:<>]+[\s*&]+%s\s*[=;\[,]" % re.escape(v), body)) != 1:
            continue                                     # declared more than once in the body
        s = rwsweep._stmt(B, k)
        if not s:
            continue
        multi = any(B.toks[q][0] == "," and B.paren[q] == 0 for q in range(s[0], s[1]))
        if multi:
            continue
        if m.group(3) == "=" and B.block[st] != (0 if B.toks[0][0] != "{" else 0) and open_ is not None and \
                B.block[st] != B.block[B.starts[0]]:
            yield ("declhoist", "`%s %s` declared at the function's start, assigned at line +%d" % (ty, v, c.lineoff(st)),
                   [(open_, open_, " %s %s;" % (ty, v)), (m.start(1), m.end(2), v)], "", dict(i=st))
        if m.group(3) == ";":
            nxt = rwsweep._stmt(B, k + 1) if k + 1 < len(B.starts) else None
            if nxt and nxt[0] == s[1] and B.toks[nxt[0]][0] == v and B.toks[nxt[0] + 1][0] == "=":
                yield ("merge", "`%s %s; %s = ..` as `%s %s = ..` at line +%d" % (ty, v, v, ty, v, c.lineoff(st)),
                       [(B.toks[s[0]][1], B.toks[nxt[0] + 1][2], "%s %s =" % (ty, v))], "", dict(i=st))
            later = [i for i in B.uses[v] if i > st]
            if later:
                fst = max(x for x in B.starts if x <= later[0])
                if B.block[fst] == B.block[st] and fst != s[1] and fst in B.starts and not B.paren[fst] and \
                        B.toks[fst][0] not in ("case", "default", "else"):
                    yield ("declsink", "`%s %s;` moved to its first use at line +%d" % (ty, v, c.lineoff(fst)),
                           [(B.toks[s[0]][1], B.toks[s[1] - 1][2], ""),
                            (B.toks[fst][1], B.toks[fst][1], "%s %s; " % (ty, v))], "", dict(i=st))
        if m.group(3) == "=" and st != B.starts[0] and B.block[st] == B.block[B.starts[0]] and open_ is not None:
            yield ("declhoist.top", "`%s %s` declared at the function's start, assigned at line +%d" % (ty, v, c.lineoff(st)),
                   [(open_, open_, " %s %s;" % (ty, v)), (m.start(1), m.end(2), v)], "", dict(i=st))


DECL_KINDS = collections.OrderedDict([
    ("extvar", "extern int em_dv%d_%d;"),
    ("fdecl", "int em_df%d_%d(int);"),
    ("fwd", "struct em_ds%d_%d;"),
    ("sdef", "struct em_dt%d_%d { int a; };"),
    ("tdef", "typedef int em_dy%d_%d;"),
    ("tmpl", "template<class EmT> void em_dg%d_%d(EmT);"),
    ("enum", "enum em_de%d_%d { em_dE%d_%d };"),
    ("inl", "inline int em_di%d_%d() { return 1; }"),
])
DECL_N = (1, 2, 3, 4, 5, 6, 8, 12, 16, 24, 32)


def gen_decls(c):
    """N dummy declarations of one kind, before the function (at the start of its top-level construct) or at the top
    of the unit; and N dummy external function definitions (compiled: c2 runs on each) before the function. Written
    on one line, so no line number moves."""
    P, _depth = _toplevel(c.text, c.fn[4])
    for where, pos in (("before", P), ("top", 0)):
        for kind, fmt in DECL_KINDS.items():
            for n in DECL_N:
                txt = " ".join((fmt % ((n, q) * (fmt.count("%d") // 2)) if kind != "enum" else fmt % (n, q, n, q))
                               for q in range(n))
                yield ("decl.%s.%s" % (kind, where), "%d x `%s` %s" % (n, fmt.split("%")[0] + "..", where),
                       [(pos, pos, txt + " ")], "", dict(n=n, i=-1))
    for n in (1, 2, 3, 4, 6, 8):
        txt = " ".join("int em_dd%d_%d(int x) { return x * %d + 1; }" % (n, q, q + 3) for q in range(n))
        yield ("deffn.before", "%d dummy function definitions before the function" % n, [(P, P, txt + " ")], "",
               dict(n=n, i=-1))


def gen_move(c):
    """The function's definition moved before the previous top-level function definition, and to the end of the
    unit (it is declared earlier, in its class or a header, or the move does not compile and is dropped)."""
    text = c.text
    P, depth = _toplevel(text, c.fn[4])
    if depth:
        return
    E = c.fn[3] + 1
    mine = text[P:E]
    fns = [f for f in helpersweep.functions(text) if f[3] < P]
    if fns:
        prev = max(fns, key=lambda f: f[3])
        Q, d2 = _toplevel(text, prev[4])
        if not d2 and Q < P:
            between = text[Q:P]
            if "\n" in mine:
                yield ("move.up", "moved before %s (the previous definition)" % prev[0],
                       [(Q, E, mine + "\n" + between.rstrip() + "\n" + "\n" * (between.count("\n") -
                                                                                 between.rstrip().count("\n") - 1))],
                       "", dict(i=-1))
    yield ("move.end", "moved to the end of the unit", [(P, E, "\n" * mine.count("\n"))], "\n" + mine + "\n", dict(i=-1))


def gen_realize(c):
    """realize.py's code-neutral constructs over the whole body (not only one block's lines)."""
    B = c.B
    sts = list(B.starts)
    out = []
    for v in [x for x in B.vars if x != "this"]:
        out += list(realize.g_copy(c, v, sts))
    out += list(realize.g_uncopy(c, None, sts))
    out += list(realize.g_headdup(c, None, sts, None)) + list(realize.g_headdup2(c, None, None))
    out += list(realize.g_headhoist(c, None, sts, None))
    out += list(realize.g_taildup(c, None, None)) + list(realize.g_tailhoist(c, None, None))
    conds = realize._cond_vars(c, limit=4)
    for V in (["this"] if c.cls else []) + [v for v in B.vars if v != "this" and B.vars[v][1] < 0]:
        out += list(realize.g_eqif(c, V, sts))
    for W in conds:
        out += [("eqif2",) + g[1:] for g in realize.g_eqif(c, W, sts)]
    # equal arms tested on a local (a stack local's test is one more slot use, deleted after packing on 14.00 /
    # 15.00: realize.md rule 10), around the statements near each of its uses
    for V in [v for v in B.vars if v != "this" and B.vars[v][1] >= 0]:
        if not realize._eqif_cond(c, V):
            continue
        near = set()
        for u in B.uses.get(V, []):
            k = max([q for q, st in enumerate(B.starts) if st <= u] or [0])
            near |= {B.starts[q] for q in range(max(0, k - 2), min(len(B.starts), k + 3)) if B.starts[q] > B.vars[V][1]}
        out += [("eqifv",) + g[1:] for g in realize.g_eqif(c, V, sorted(near))]
    out += list(realize.g_uneqif(c, None, None))
    out += list(realize.g_helperbind(c))
    for g in out:
        yield g[:4] + (dict(),)


def gen_rwsweep(c):
    fams = ["wrap", "alias", "swap", "split", "touch", "cflow", "paren", "guard", "const", "unalias", "byval", "subst"]
    for fam in [f for f in fams if f in rwsweep.FAMILIES]:
        try:
            for f, desc, edits in rwsweep.FAMILIES[fam](c.B):
                yield (f, desc, edits, "", dict())
        except Exception as e:                       # a generator that trips on this body: the others still run
            sys.stderr.write("effectmine: rwsweep %s failed here: %s\n" % (fam, e))


GENERATORS = [gen_rwsweep, gen_realize, gen_commute, gen_compound, gen_incdec, gen_nullcmp, gen_index, gen_wrapx,
              gen_subobj, gen_argswap, gen_retwrap, gen_constq, gen_width, gen_pad, gen_scope, gen_hop, gen_declplace, gen_decls,
              gen_move]


def asm_ranges(c):
    """Text ranges of the body's inline assembler (`__asm { .. }`, `__asm op ..` to the end of the line): no rewrite
    may touch them."""
    out = []
    for m in re.finditer(r"\b_?_asm\b", c.text[c.B.b:c.B.e]):
        a = c.B.b + m.start()
        k = m.end() + c.B.b
        rest = c.text[k:k + 400]
        if rest.lstrip().startswith("{"):
            o = k + rest.index("{")
            z = c.text.find("}", o)
            out.append((a, z + 1 if z > 0 else c.B.e))
        else:
            z = c.text.find("\n", k)
            out.append((a, z if z > 0 else c.B.e))
    return out


def generate(c):
    out, seen = [], set()
    asm = asm_ranges(c)
    for g in GENERATORS:
        try:
            items = list(g(c))
        except Exception as e:
            sys.stderr.write("effectmine: generator %s failed: %r\n" % (g.__name__, e))
            continue
        for fam, desc, edits, tail, meta in items:
            key = (repr(sorted(edits)), tail)
            if key in seen or not edits and not tail:
                continue
            if any(a <= e[0] < z or a < e[1] <= z for e in edits for a, z in asm):
                continue
            seen.add(key)
            cls = _cls_of(fam, desc) if g in (gen_rwsweep, gen_realize) else fam
            out.append(dict(cls=cls, fam=cls.split(".")[0], desc=desc, edits=[list(e) for e in edits], tail=tail,
                            meta=meta))
    return out


def site_context(c, v):
    """Where a mutation is: line offset, block, loop, the variable it touches and that variable's kind and type."""
    B = c.B
    if v["meta"].get("i", None) == -1 or not v["edits"]:
        return dict(where="unit", line=None)
    pos = min(e[0] for e in v["edits"])
    if pos < B.b or pos > B.e:
        return dict(where="unit", line=None)
    k = next((q for q, t in enumerate(B.toks) if t[1] >= pos), len(B.toks) - 1)
    line = c.text.count("\n", 0, pos) - c.line0
    st = max([s for s in B.starts if s <= k] or [0])
    blk = None
    for (b, w, P, ls) in c.base.blocks:
        if line in ls:
            blk = b
            break
    var = None
    for q in range(k, min(len(B.toks), k + 12)):
        t = B.toks[q][0]
        if t in B.vars:
            var = t
            break
    vk = vt = None
    if var:
        ty, d = B.vars[var]
        vk = "this" if var == "this" else ("param" if d < 0 else "local")
        vt = "ref" if "&" in ty else "ptr" if "*" in ty else "float" if re.search(r"\b(float|double)\b", ty) else \
            "int" if re.search(SCAL[1:-1], ty) else "class"
    stmt = B.toks[st][0] if st < len(B.toks) else ""
    sk = "if" if stmt == "if" else "loop" if stmt in ("for", "while", "do") else "return" if stmt == "return" else \
        "decl" if rwsweep.DECL.match(B.text, B.toks[st][1]) else "stmt"
    return dict(where="body", line=line, block=blk, loop=bool(realize._in_loop(B, st)) if st in B.starts else False,
                var=var, vkind=vk, vtype=vt, stmt=sk)


# ---------------------------------------------------------------- one compile per feature, and the differences

def ilsig(fn):
    """A hash of the IL at Color, without the tuple addresses (heap pointers: they move with anything the compiler
    allocated before, e.g. one more declaration)."""
    h = hashlib.md5()
    ilb = fn.fn.get("ilb", {})
    for bb in list(dict.fromkeys(fn.fn.get("bborder", []))) or sorted(ilb):
        for ln in ilb.get(bb, []):
            h.update((re.sub(r"^I [0-9a-f]+ ", "I ", ln) + "\n").encode())
        h.update(b"#\n")
    return h.hexdigest()


def pop_order(fn):
    """Named values (and compiler locals `localN`, whose name is their symbol key) in the order Color first pops them."""
    out = []
    for r in fn.fn["rows"]:
        nm = r["name"]
        if r["cls"] in (0, None) and r["npass"] and nm not in out and not re.match(r"(t|imm|k\d+_)\d+$|\?$", nm):
            out.append(nm)
    return out


def local_prefs(fn):
    """Register preferences of compiler locals (`localN`): realize.Fn leaves them out of its named relations."""
    gname = {p["gr"]: p["name"] for p in fn.snap.get("pops", [])}
    rn = fn.snap.get("regnames", {})
    out = collections.Counter()
    for e in fn.snap.get("pref_events", []):
        nm = gname.get(e["gr"])
        if nm and re.match(r"local\d+$", nm) and e.get("src") is None:
            out[(nm, rn.get(str(e["reg"]), str(e["reg"])))] += 1
    return out


def local_map(b, v):
    """Compiler locals are named by symbol key (`local4`), which any key edit renumbers: the variant's names mapped
    onto the base's by colouring order when the two have the same number of them (else unmapped)."""
    la = [n for n in pop_order(b) if re.match(r"local\d+$", n)]
    lb = [n for n in pop_order(v) if re.match(r"local\d+$", n)]
    if set(la) == set(lb) or len(la) != len(lb):
        return {}
    return dict(zip(lb, la))


def regs_effects(b, v):
    d = realize.Delta(b, v)
    eff = d.strings()
    eff += ["reg:%s:%s>%s" % (n, "/".join(x or ["-"]), "/".join(y or ["-"])) for n, (x, y) in d.regs.items()]
    lm = local_map(b, v)
    ren = lambda n: lm.get(n, n)
    oa, ob = pop_order(b), [ren(n) for n in pop_order(v)]
    both = [x for x in ob if x in oa]
    flips = []
    for i, x in enumerate(both):
        for y in both[i + 1:]:
            if oa.index(x) > oa.index(y):
                flips.append("before:%s:%s" % (x, y))
    eff += flips[:40]
    pa = local_prefs(b)
    pb = collections.Counter({(ren(k[0]), k[1]): c for k, c in local_prefs(v).items()})
    eff += ["pref:%s:%s:%+d" % (k[0], k[1], pb[k] - pa[k]) for k in sorted(set(pa) | set(pb)) if pa[k] != pb[k]]
    vregs = {ren(n): r for n, r in v.regs.items()}
    for n in sorted(set(b.regs) | set(vregs)):
        if re.match(r"local\d+$", n) and b.regs.get(n) != vregs.get(n):
            eff.append("reg:%s:%s>%s" % (n, "/".join(b.regs.get(n) or ["-"]), "/".join(vregs.get(n) or ["-"])))
    code = dict(code=d.code, bag=d.bag, ident=d.ident, dn=d.dn, n=len(v.code))
    try:        # the listing names stack slots (`_x$[esp+8]`): the bytes show a moved slot, the listing text does not
        code["bytes"] = realize.adiff(tsweep.bkeys(b.instrs), tsweep.bkeys(v.instrs))
    except Exception:
        code["bytes"] = None
    return eff, code


def sched_regions(path, func, cl, keep):
    import c2sched
    _i, _h, rc, out, log = c2trace.run_trace(path, c2sched.HOOKS, cl, keep=keep, quiet=True)
    fs = [f for f in c2sched.parse(log, func)]
    if not fs or not fs[0][3]:
        raise RuntimeError("the scheduler did not run on %s (EH frame without -EHs, or no region)" % func)
    name, pre, post, regions = fs[0]
    txt = c2sched.instr_text(pre)
    # temporaries, stack symbols, alias classes and hash temps are numbered by symbol key: any key edit renumbers them
    norm = lambda t: re.sub(r"\s+", " ", re.sub(r"\$L?\d+", "$", re.sub(
        r"(?<![\w$])(\$o|\$T|t|s|pas|tv|local|imm)\d+", r"\1", c2sched.short(t))))
    out = []
    for r in regions:
        tups = sorted((t for t in r["nodes"] if t), key=lambda t: r["nodes"][t]["lex"])
        issued = [t for _c, ts in r["issue"] for t in ts if t]
        out.append(dict(lex=[norm(txt.get(t, "?")) for t in tups], iss=[norm(txt.get(t, "?")) for t in issued]))
    return out


def sched_effects(ra, rb):
    sa, sb = [tuple(r["lex"]) for r in ra], [tuple(r["lex"]) for r in rb]
    sm = difflib.SequenceMatcher(None, sa, sb, autojunk=False)
    eff, changed = [], 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            changed += max(i2 - i1, j2 - j1)
            continue
        for i, j in zip(range(i1, i2), range(j1, j2)):
            A, Bq = ra[i]["iss"], rb[j]["iss"]
            if A == Bq:
                continue
            pa = {x: k for k, x in enumerate(A)}
            for p in range(len(Bq)):
                for q in range(p + 1, len(Bq)):
                    x, y = Bq[p], Bq[q]
                    if x in pa and y in pa and pa[x] > pa[y]:
                        eff.append("sched:R%d:%s > %s" % (i, x, y))
    if changed:
        eff.append("regions:%d" % changed)
    return eff[:40]


def load_report(path, func, cl, keep):
    """loadcse's report, from a child process: its traces need C2TRACE_ILPAS=1, which changes the configuration of
    every trace in the process that sets it. The child is this tool (`_loads`), so it refuses wine too."""
    import subprocess
    os.makedirs(keep, exist_ok=True)
    out = os.path.join(keep, "loads.json")
    env = dict(os.environ, C2TRACE_ILPAS="1")
    p = subprocess.run([sys.executable, os.path.abspath(__file__), "_loads", path, func, out, keep, "--"] + list(cl),
                       env=env, capture_output=True, text=True, errors="replace")
    if p.returncode or not os.path.exists(out):
        raise RuntimeError("loadcse failed: " + (p.stdout + p.stderr)[-200:])
    return json.load(open(out))


def _loads_child(argv, cl):
    path, func, out, keep = argv[:4]
    import loadcse, contextlib, io
    with contextlib.redirect_stdout(io.StringIO()):
        fn, got = loadcse.run(path, func, cl, keep=keep)
        rep = loadcse.analyse(fn, got)
    locs = {e["location"]: (e["at_color"], e["final"]) for e in rep["locations"]}
    rmw = {}
    for r in rep["rmw"]:
        rmw.setdefault(r["location"], []).append(r["status"].split(" (")[0])
    json.dump(dict(locs=locs, rmw=rmw), open(out, "w"))


def load_effects(a, b):
    eff = []
    for loc in sorted(set(a["locs"]) | set(b["locs"])):
        x, y = a["locs"].get(loc), b["locs"].get(loc)
        if x != y:
            eff.append("load:%s:%s>%s" % (loc, x[1] if x else "-", y[1] if y else "-"))
    for loc in sorted(set(a["rmw"]) | set(b["rmw"])):
        x, y = a["rmw"].get(loc), b["rmw"].get(loc)
        if x != y:
            eff.append("rmw:%s:%s>%s" % (loc, "/".join(x or ["-"]), "/".join(y or ["-"])))
    return eff


def slot_effects(sa, sb):
    d = realize.SlotDelta(sa, sb)
    eff = d.strings()
    A, Bt = d.a, d.b
    eff += ["ord:%s:%d>%d" % (k, A[k]["ord"], Bt[k]["ord"]) for k in A if k in Bt and A[k]["ord"] != Bt[k]["ord"]]
    eff += ["off:%s:%s>%s" % (k, x, y) for k, (x, y) in sorted(d.offs.items())]
    if d.T[0] != d.T[1]:
        eff.append("T:%s>%s" % d.T)
    return eff


def key_effects(fa, fb):
    r = realize.key_delta_f(fa, fb)
    eff = ["keys:%s:%+d" % kv for kv in sorted(r["counts"].items())]
    eff += ["key:%s:%+d" % (n.split("~")[0], kb - ka) for n, ka, kb in r["moved"][:30]]
    if r["flips"]:
        eff.append("flips:%d" % r["flips"])
    return eff


def etype(model, s):
    """The effect type of one effect string (what generalises across functions)."""
    k = s.split(":")[0]
    if model == "R":
        if k in ("ref", "P", "tmp"):
            return "R.%s%s" % (k, "+" if s.rstrip().split(":")[-1].startswith("+") else "-")
        if k == "pref":
            return "R.pref" + ("+" if s.split(":")[-1].startswith("+") else "-")
        return "R." + k
    if model == "S":
        return "S." + k.split("(")[0]
    if model == "L":
        if k == "uses":
            a, b = s.split(":")[-1].split(">")
            return "L.uses" + ("+" if int(b) > int(a) else "-")
        return "L." + k
    if model == "K":
        if k == "keys":
            return "K.%s%s" % (s.split(":")[1], "+" if s.split(":")[2].startswith("+") else "-")
        return "K." + {"key": "moved"}.get(k, k)
    if model == "Q":
        return "Q." + {"sched": "flip"}.get(k, k)
    if model == "F":                       # fe:SHAPE:+n -> F.fe:OPCODE+ (the opcode with a branch's condition)
        op = s.split(":", 1)[1].rsplit(":", 1)[0].split("(")[0]
        return "F.%s:%s%s" % (k, op, "+" if s.rsplit(":", 1)[1].startswith("+") else "-")
    if model == "M":
        if k == "load":
            a, b = s.rsplit(":", 1)[1].split(">")
            try:
                return "M.load" + ("+" if int(b) > int(a) else "-")
            except ValueError:
                return "M.load"
        return "M." + k
    return model + "." + k


def okey(code, missing=0):
    """A measured variant's sort key against the original: (rdiff, bdiff); a record written before rdiff was kept
    has only its bdiff ("bdo"), and is ranked by it."""
    b = code.get("bdo")
    if b is None:
        return (missing, missing)
    r = code.get("rdo")
    return (r if r is not None else b, b)


def ocmp(code, base):
    """-1 / 0 / 1: the variant against the base on the original, rdiff first and bdiff the tie-break (bdiff alone
    when either lacks an rdiff); None without an original. base: {"bdo", "rdo"} (Miner.base_osc)."""
    if base is None or code.get("bdo") is None or base.get("bdo") is None:
        return None
    if code.get("rdo") is not None and base.get("rdo") is not None:
        x, y = (code["rdo"], code["bdo"]), (base["rdo"], base["bdo"])
    else:
        x, y = code["bdo"], base["bdo"]
    return (x > y) - (x < y)


def ogain(code, base):
    """How much closer to the original than the base (rdiff when both have one, else bdiff); 0 without one."""
    if base is None or code.get("bdo") is None:
        return 0
    if code.get("rdo") is not None and base.get("rdo") is not None:
        return base["rdo"] - code["rdo"]
    return base["bdo"] - code["bdo"]


def code_types(code, base):
    t = []
    if code.get("ident") == 0 and code.get("bytes"):
        t.append("C.frame")                    # the same instructions and registers, other stack offsets
    elif code.get("ident") == 0:
        t.append("C.same")
    elif code.get("code") == 0:
        t.append("C.regsonly")
    elif code.get("bag") == 0:
        t.append("C.bag0")
    else:
        t.append("C.changed")
    c = ocmp(code, base)                       # C.bdiff-/+: closer / farther by (rdiff, bdiff) (names kept)
    if c is not None:
        if c < 0:
            t.append("C.bdiff-")
        elif c > 0:
            t.append("C.bdiff+")
        if code["bdo"] == 0:
            t.append("C.match")
    return t


def rec_types(rec, base):
    ts = set(code_types(rec["code"], base))
    for m in "RSLKQM":
        for s in rec["eff"].get(m, []):
            ts.add(etype(m, s))
    ts.update(f_types(rec["eff"].get("F", [])))
    return sorted(ts)


def f_types(eff):
    """Model F types by the net count per stage and opcode: `F.fe:OPADD-` only when the reader IL has fewer OPADD
    tuples; a change of operand kinds alone (`hs_id(this)`: `OPADD(T<-V,C)` -> `OPADD(T<-T,C)`) is `F.fe:kind`."""
    net, ts = collections.Counter(), set()
    for s in eff:
        st, _, b = s.partition(":")
        sh, _, c = b.rpartition(":")
        net[(st, sh.split("(", 1)[0])] += int(c)
    for (st, op), n in net.items():
        ts.add("F.%s:%s%s" % (st, op, "+" if n > 0 else "-") if n else "F.%s:kind" % st)
    return ts


def is_null(rec):
    return bool(rec.get("ok") and rec["code"].get("ident") == 0 and not rec["code"].get("bytes") and
                not any(rec["eff"].get(m) for m in "RSLKQMF"))


# ---------------------------------------------------------------- a reference compile (the base, or a helper control)

class Ref:
    """One compiled source with every feature traced lazily (a variant's effects are measured against it)."""

    def __init__(self, miner, tag, text):
        self.m, self.tag, self.text = miner, tag, text
        self.dir = os.path.join(miner.out, tag)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, tag + ".cpp")
        open(self.path, "w").write(text)
        self.fn = miner.trace_regs(self.path, self.dir)
        self.sig = ilsig(self.fn)
        self.feat = {}
        self.lock = threading.Lock()
        self.alias = None                 # a control identical to the base: measure against the base

    def get(self, feat):
        if self.alias is not None:
            return self.alias.get(feat)
        with self.lock:
            if feat not in self.feat:
                try:
                    self.feat[feat] = self.m.trace_feat(feat, self.path, self.dir)
                except (Exception, SystemExit) as e:
                    self.feat[feat] = e
            r = self.feat[feat]
        if isinstance(r, BaseException):
            raise RuntimeError("reference %s has no %s trace: %s" % (self.tag, feat, str(r)[:200]))
        return r


# ---------------------------------------------------------------- the miner

def apply_edits(text, edits, tail=""):
    for s_, e_, r in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        text = text[:s_] + r + text[e_:]
    return text + (tail or "")


def _lock_append(path, line, _lk=threading.Lock()):
    with _lk:
        with open(path, "a") as f:
            f.write(line + "\n")


def load_db(path=DB):
    """The records, without those of the runs listed in retired.txt (one run id per line: runs measured by an older
    version of this tool whose effect strings are not comparable; their lines stay in the file)."""
    out = []
    if not os.path.exists(path):
        return out
    rp = os.path.join(os.path.dirname(path), "retired.txt")
    retired = set(open(rp).read().split()) if os.path.exists(rp) else set()
    for ln in open(path):
        ln = ln.strip()
        if ln:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("run") not in retired:
                out.append(r)
    return out


def class_stats(db, build=None, exclude_func=None, by=None):
    """{cls: dict(n, null, fail, types: Counter, neutral: Counter)} over the database (pairs left out); with `by` (a
    context key: build, vtype, vkind, stmt, loop, where) the class is split by that key's value."""
    st = {}
    for r in db:
        if r.get("cls", "").startswith("pair") or (build and r.get("build") != build):
            continue
        if exclude_func and r.get("fkey") == exclude_func:
            continue
        key = r["cls"]
        if by:
            key += "|" + str(r.get("build") if by == "build" else (r.get("ctx") or {}).get(by))
        s = st.setdefault(key, dict(n=0, null=0, fail=0, types=collections.Counter(), neutral=collections.Counter(),
                                         funcs=set(), traced=collections.Counter()))
        if not r.get("ok"):
            s["fail"] += 1
            continue
        s["n"] += 1
        s["funcs"].add(r.get("fkey"))
        for f in r.get("traced") or ["regs"]:
            s["traced"][MODEL_OF.get(f, "R")] += 1
        if is_null(r):
            s["null"] += 1
        neutral = r["code"].get("code") == 0
        types = [t for t in r.get("types", []) if not t.startswith("F.")] + sorted(f_types(r["eff"].get("F", [])))
        for t in types:                    # F types recomputed: records written before f_types() typed renames
            s["types"][t] += 1
            if neutral:
                s["neutral"][t] += 1
    return st


class Miner:
    def __init__(self, a, cl):
        self.a, self.cl = a, cl
        raw = open(a.src).read()
        hdr = os.path.dirname(os.path.abspath(a.src))
        self.text = re.sub(r'#include "([^"/][^"]*)"', lambda m: '#include "%s"' % os.path.join(hdr, m.group(1))
                           if os.path.exists(os.path.join(hdr, m.group(1))) else m.group(0), raw)
        self.have = defined_helpers(self.text)
        self.build = c2trace.c2build.name() if c2trace.c2build.table() else "14.00"
        self.sha = hashlib.sha1(self.text.encode()).hexdigest()[:16]
        self.flags = " ".join(cl)
        stem = os.path.splitext(os.path.basename(a.src))[0]
        self.run = time.strftime("%Y%m%d-%H%M%S") + "-" + self.sha[:6]
        # one directory per run (two runs on one source, or a --list beside a run, must not share or delete it)
        self.out = os.path.join(RUNS, stem + "-" + hashlib.md5(os.path.abspath(a.src).encode()).hexdigest()[:6] +
                                ("@" + self.build if self.build != "14.00" else ""), self.run + "-%d" % os.getpid())
        os.makedirs(self.out, exist_ok=True)
        os.makedirs(TEXTS, exist_ok=True)
        tp = os.path.join(TEXTS, self.sha + ".cpp.gz")
        if not os.path.exists(tp):
            with gzip.open(tp, "wt") as f:
                f.write(self.text)
        self.orig = tsweep.orig_instrs(a.asm_vs) if a.asm_vs else None
        self.orig_slot = None
        self.feats = [f for f in a.features.split(",") if f]
        self.compiles = 0
        self.per_feat = {}
        self.clock = threading.Lock()
        self.funcpat = c2trace.func_decl(a.func)
        self.func = None
        self.base = Ref(self, "base", self.text)
        self.func = self.base.fn.name
        self.fkey = "%s|%s|%s|%s" % (self.func, self.sha, self.build, self.flags)
        self.c = GenCtx(self.text, self.func, self.base.fn)
        self.c._mtype = {}
        self.base_bdo = self.bdo(self.base.fn)
        self.base_osc = None if self.base_bdo is None else dict(bdo=self.base_bdo, rdo=self.rdo(self.base.fn))
        self.ctl = {}
        self.ctl_lock = threading.Lock()

    # -- compiles
    def spend(self, n=1):
        with self.clock:
            self.compiles += n

    def left(self):
        return getattr(self, "pair_limit", self.a.max_compiles) - self.compiles

    def trace_regs(self, path, d):
        self.spend()
        mute = getattr(sys.stdout, "tl", None)        # cl's error text of a variant that does not compile: not printed
        if mute is not None and self.func:
            mute.on = True
        try:
            t = realize.Trace(path, self.cl, keep=d)
        finally:
            if mute is not None:
                mute.on = False
        return t.func(self.func or self.funcpat)

    def trace_feat(self, feat, path, d):
        self.spend()
        with self.clock:
            self.per_feat[feat] = self.per_feat.get(feat, 0) + 1
        if feat == "scratch":
            js, _p = realize.pick_snap(path, self.func, self.cl, keep=os.path.join(d, "scratch"))
            return js
        if feat == "slots":
            return realize.slot_snap(path, self.func, self.cl, keep=os.path.join(d, "slots"))
        if feat == "keys":
            import symkeys
            fs = [f for f in symkeys.trace(path, self.cl) if f.name and c2trace.func_match(self.func, f.name)]
            if not fs:
                raise RuntimeError("no symkeys trace for %s" % self.func)
            return fs[0]
        if feat == "sched":
            return sched_regions(path, self.func, self.cl, keep=os.path.join(d, "sched"))
        if feat == "loads":
            return load_report(path, self.func, self.cl, keep=os.path.join(d, "loads"))
        if feat == "frontend":
            import feshape
            return feshape.collect(path, self.func, self.cl, keep=os.path.join(d, "frontend"))
        raise ValueError(feat)

    def bdo(self, fn):
        return tsweep.bdiff(fn.instrs, self.orig) if self.orig is not None else None

    def rdo(self, fn):
        return tsweep.rdiff(fn.instrs, self.orig) if self.orig is not None else None

    def slot_agree(self, s):
        if self.orig is None:
            return None
        import c2slots
        if self.orig_slot is None:
            self.orig_slot = c2slots.read_orig(self.a.asm_vs)
        r = realize.slot_agree(s, self.orig_slot)
        return list(r) if r else None

    # -- variants
    def text_of(self, v):
        t = apply_edits(self.text, v["base_edits"], v.get("base_tail")) if v.get("base_edits") is not None else self.text
        t = apply_edits(t, v["edits"], v.get("tail"))
        return prelude(v["helpers"], self.have) + t

    def control(self, hs):
        """The reference for a variant that needs the helper set hs: the base plus those helpers alone."""
        key = ",".join(sorted(h for h in hs if h not in self.have))
        if not key:
            return self.base, None
        with self.ctl_lock:
            if key not in self.ctl:
                tag = "ctl_" + hashlib.md5(key.encode()).hexdigest()[:6]
                try:
                    r = Ref(self, tag, prelude(set(key.split(",")), self.have) + self.text)
                    if r.sig == self.base.sig and realize.adiff(r.fn.code, self.base.fn.code) == 0:
                        r.alias = self.base
                    self.ctl[key] = r
                    v = dict(cls="decl.helpers", fam="decl", desc="helpers %s alone" % key, edits=[], tail="",
                             helpers=sorted(key.split(",")), meta=dict(n=len(key.split(","))), id="ctl")
                    rec = self.record(v, r.fn, self.base, None, ctx=dict(where="unit", line=None))
                    self.measure_more(rec, r.path, r.dir, self.base, v)
                    self.write(rec)
                except (Exception, SystemExit) as e:
                    self.ctl[key] = e
            r = self.ctl[key]
        if isinstance(r, BaseException):
            raise RuntimeError("helper control %s failed: %s" % (key, str(r)[:200]))
        return r, key

    def record(self, v, fn, ref, err, ctx):
        rec = dict(run=self.run, ts=int(time.time()), build=self.build, func=self.func, fkey=self.fkey, sha=self.sha,
                   src=os.path.abspath(self.a.src), cls=v["cls"], fam=v["fam"], desc=v["desc"], meta=v.get("meta", {}),
                   ctx=ctx, edits=v["edits"], tail=v.get("tail", ""), helpers=sorted(v.get("helpers", [])),
                   base_edits=v.get("base_edits"), base_tail=v.get("base_tail"),
                   vs=ref.tag if ref is not None else None, ok=fn is not None, err=err, eff={}, code={})
        if fn is None:
            return rec
        eff, code = regs_effects(ref.fn, fn)
        code["bdo"] = self.bdo(fn)
        code["rdo"] = self.rdo(fn)
        rec["code"] = code
        rec["eff"]["R"] = eff
        rec["sig_same"] = ilsig(fn) == ref.sig
        return rec

    def measure_more(self, rec, path, d, ref, v):
        """The secondary features (S L K Q M) of a measured variant, when the regs trace changed or eagerly."""
        changed = not (rec.get("sig_same") and rec["code"].get("ident") == 0 and not rec["code"].get("bytes") and
                       not rec["eff"].get("R"))
        rec["traced"] = ["regs"]
        for feat in self.feats:
            if feat == "regs":
                continue
            if not (changed or self.a.eager or feat in self.want_feats or feat in self.eager_feats):
                continue
            if self.left() <= 0 and v.get("id") != "ctl":
                rec.setdefault("skipped", []).append(feat)
                continue
            if isinstance(self.base.feat.get(feat), BaseException):
                continue                           # the base has no such trace (no stack packing, no scheduled region)
            try:
                base_f = ref.get(feat)
                var_f = self.trace_feat(feat, path, d)
            except (Exception, SystemExit) as e:
                rec.setdefault("ferr", {})[feat] = str(e)[:200]
                continue
            rec["traced"].append(feat)
            m = MODEL_OF[feat]
            try:
                if feat == "scratch":
                    rec["eff"][m] = realize.PickDelta(base_f, var_f).strings()
                elif feat == "slots":
                    rec["eff"][m] = slot_effects(base_f, var_f)
                    ag = self.slot_agree(var_f)
                    if ag:
                        rec["code"]["slot_agree"] = ag
                elif feat == "keys":
                    rec["eff"][m] = key_effects(base_f, var_f)
                elif feat == "sched":
                    rec["eff"][m] = sched_effects(base_f, var_f)
                elif feat == "loads":
                    rec["eff"][m] = load_effects(base_f, var_f)
                elif feat == "frontend":
                    import feshape
                    rec["eff"][m] = feshape.effects(base_f, var_f)
            except Exception as e:
                rec.setdefault("ferr", {})[feat] = "compare: " + str(e)[:200]

    def write(self, rec):
        rec["types"] = rec_types(rec, self.base_osc) if rec.get("ok") else []
        rec["null"] = is_null(rec)
        _lock_append(DB, json.dumps(rec, sort_keys=True))

    def measure(self, v):
        """Compile one variant (regs trace), then its secondary features; returns the record."""
        tag = "v%04d" % v["id"] if isinstance(v["id"], int) else str(v["id"])
        d = os.path.join(self.out, tag)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, tag + ".cpp")
        ctx = v.get("ctx") or site_context(self.c, v)
        try:
            ref, _k = self.control(v["helpers"])
        except Exception as e:
            rec = self.record(v, None, None, "control: " + str(e)[:200], ctx)
            self.write(rec)
            return rec
        open(p, "w").write(self.text_of(v))
        fn, err = None, None
        try:
            fn = self.trace_regs(p, d)
        except (Exception, SystemExit) as e:
            err = str(e)[-240:]
        rec = self.record(v, fn, ref, err, ctx)
        if fn is not None:
            self.measure_more(rec, p, d, ref, v)
        rec["dir"] = d
        rec["id"] = v["id"]
        for f in (os.listdir(d) if os.path.isdir(d) else []):   # the traces are read: only the source and the listing stay
            q = os.path.join(d, f)
            if os.path.isdir(q):
                shutil.rmtree(q, ignore_errors=True)
            elif not f.endswith((".cpp", ".asm")):
                os.remove(q)
        return rec


# ---------------------------------------------------------------- wanted effects

def parse_want(s):
    s = s.strip()
    if s in ("bdiff", "smaller"):
        return dict(m="C", kind="bdiff", text=s)
    m = re.fullmatch(r"(ins|drop):(\d+)(?::\d+)?", s)
    if m:
        return dict(m="S", kind=m.group(1), n=int(m.group(2)), text=s, feat="scratch")
    m = re.fullmatch(r"uses:(.+?)=(\d+)\^?", s)
    if m:
        return dict(m="L", kind="uses", x=m.group(1), n=int(m.group(2)), text=s, feat="slots")
    m = re.fullmatch(r"pos:(.+?)=(\d+)", s)
    if m:
        return dict(m="L", kind="pos", x=m.group(1), n=int(m.group(2)), text=s, feat="slots")
    m = re.fullmatch(r"conf([+-]):(.+?),(.+)", s)
    if m:
        return dict(m="L", kind="conf" + m.group(1), x=m.group(2), y=m.group(3), text=s, feat="slots")
    m = re.fullmatch(r"pref:(.+?):(e?[a-d]x|e?[sd]i|e?bp)(?::\+?\d+)?", s)
    if m:
        return dict(m="R", kind="pref", x=m.group(1), reg=m.group(2), text=s, feat="regs")
    m = re.fullmatch(r"reg:(.+?)=(\w+)", s)
    if m:
        return dict(m="R", kind="reg", x=m.group(1), reg=m.group(2), text=s, feat="regs")
    m = re.fullmatch(r"(?:before:(.+?):(.+)|tie:(.+?)>(.+)|after:(.+?):(.+))", s)
    if m:
        g = m.groups()
        a, b = (g[0], g[1]) if g[0] else (g[2], g[3]) if g[2] else (g[5], g[4])
        return dict(m="R", kind="order", x=a, y=b, text=s, feat="regs")
    m = re.fullmatch(r"(ref|unref|extend|drop|var):(.+?):(\w+)(?::[+-]?\d+)?", s)
    if m:
        return dict(m="R", kind=m.group(1), x=m.group(2), blk=m.group(3), text=s, feat="regs")
    m = re.fullmatch(r"P:(\w+):([+-]\d+)", s)
    if m:
        return dict(m="R", kind="P", blk=m.group(1), n=int(m.group(2)), text=s, feat="regs")
    m = re.fullmatch(r"(add|sub):(frame|tmp|glob|other|hash)@(.+?)(?::(\d+))?", s)
    if m:
        n = int(m.group(4) or 1) * (1 if m.group(1) == "add" else -1)
        return dict(m="K", kind="key", cls=m.group(2), x=m.group(3), n=n, text=s, feat="keys")
    m = re.fullmatch(r"keys:(\w+):([+-]\d+)", s)
    if m:
        return dict(m="K", kind="count", cls=m.group(1), n=int(m.group(2)), text=s, feat="keys")
    m = re.fullmatch(r"sched:(.+?)>(.+)", s)
    if m:
        return dict(m="Q", kind="order", x=m.group(1).strip(), y=m.group(2).strip(), text=s, feat="sched")
    m = re.fullmatch(r"(fe|fi):(.+):([+-]\d+)", s)
    if m:
        return dict(m="F", kind=m.group(1), x=m.group(2), n=int(m.group(3)), text=s, feat="frontend")
    m = re.fullmatch(r"load:(.+):(\d+)", s)
    if m:
        return dict(m="M", kind="load", x=m.group(1), n=int(m.group(2)), text=s, feat="loads")
    m = re.fullmatch(r"([\w$@?#]+)>([\w$@?#]+)", s)
    if m:
        return dict(m="R", kind="order", x=m.group(1), y=m.group(2), text=s, feat="regs")
    raise SystemExit("cannot read the wanted effect %r (see --help: EFFECT)" % s)


def want_type(w, miner=None):
    """The effect type a wanted effect is an instance of (for the classes measured elsewhere)."""
    k = w["kind"]
    if w["m"] == "C":
        return "C.bdiff-"
    if w["m"] == "S":
        return "S." + k
    if w["m"] == "L":
        if k == "uses":
            cur = None
            if miner is not None:
                try:
                    tab = realize.slot_table(miner.base.get("slots"))
                    cur = tab[realize.slot_key_of(miner.base.get("slots"), w["x"])]["uses"]
                except Exception:
                    cur = None
            return "L.uses" + ("-" if cur is not None and w["n"] < cur else "+")
        return "L." + ("ord" if k == "pos" else k)
    if w["m"] == "R":
        return {"pref": "R.pref+", "reg": "R.reg", "order": "R.before", "ref": "R.ref+", "unref": "R.unref",
                "extend": "R.extend", "drop": "R.drop", "var": "R.var", "P": "R.P" + ("+" if w.get("n", 1) > 0 else "-")}[k]
    if w["m"] == "K":
        return "K.%s%s" % (w["cls"], "+" if w["n"] > 0 else "-")
    if w["m"] == "Q":
        return "Q.flip"
    if w["m"] == "M":
        return "M.load"
    if w["m"] == "F":
        return "F.%s:%s%s" % (k, w["x"].split("(")[0], "+" if w["n"] > 0 else "-")
    return "?"


def _strip_var(n):
    return re.sub(r"\$\d*$", "", n).lstrip("_")


def want_hit(w, rec, miner):
    """(hit, note): did this measured variant produce the wanted effect exactly?"""
    eff = rec.get("eff", {})
    m, k = w["m"], w["kind"]
    if m == "C":
        return ocmp(rec["code"], miner.base_osc) == -1, ""
    if m == "S":
        for s in eff.get("S", []):
            if re.match(r"%s:%d(\D|$)" % (k, w["n"]), s):
                return True, s
        return False, ""
    if m == "L":
        try:
            sb = miner.base.get("slots")
            X = realize.slot_key_of(sb, w["x"])
        except Exception:
            X = w["x"]
        if k == "uses":
            for s in eff.get("L", []):
                mm = re.match(r"uses:(.+):(\d+)>(\d+)$", s)
                if mm and mm.group(1) == X and int(mm.group(3)) == w["n"]:
                    return True, s
        elif k == "pos":
            for s in eff.get("L", []):
                mm = re.match(r"ord:(.+):(\d+)>(\d+)$", s)
                if mm and mm.group(1) == X and int(mm.group(3)) == w["n"]:
                    return True, s
        else:
            try:
                Y = realize.slot_key_of(sb, w["y"])
            except Exception:
                Y = w["y"]
            pair = "%s:%s,%s" % (k, *sorted((X, Y)))
            if pair in eff.get("L", []):
                return True, pair
        return False, ""
    if m == "R":
        R = eff.get("R", [])
        x = _strip_var(w.get("x", ""))
        if k == "pref":
            hit = [s for s in R if re.match(r"pref:%s:%s:\+" % (re.escape(x), w["reg"]), s)]
        elif k == "reg":
            hit = [s for s in R if re.match(r"reg:%s:.*>(.*/)?%s(/|$)" % (re.escape(x), w["reg"]), s)]
        elif k == "order":
            y = _strip_var(w["y"])
            hit = [s for s in R if s in ("before:%s:%s" % (x, y), "tie:%s>%s" % (x, y))]
        elif k == "P":
            hit = [s for s in R if s == "P:%s:%+d" % (w["blk"], w["n"])]
        elif k == "ref":
            hit = [s for s in R if re.match(r"ref:%s:%s:\+" % (re.escape(x), w["blk"]), s) or
                   s == "extend:%s:%s" % (x, w["blk"])]
        else:
            hit = [s for s in R if s.startswith("%s:%s:%s" % (k, x, w["blk"]))]
        return bool(hit), " ".join(hit[:2])
    if m == "K":
        K = eff.get("K", [])
        if k == "count":
            return ("keys:%s:%+d" % (w["cls"], w["n"]) in K), ""
        x = _strip_var(w["x"])
        hit = [s for s in K if re.match(r"key:_?%s(@\S*)?:%+d$" % (re.escape(x), w["n"]), s)]
        return bool(hit), " ".join(hit[:1])
    if m == "Q":
        for s in eff.get("Q", []):
            mm = re.match(r"sched:R\d+:(.*) > (.*)$", s)
            if mm and w["x"] in mm.group(1) and w["y"] in mm.group(2):
                return True, s
        return False, ""
    if m == "F":
        import feshape                     # the shape AND its opcode's net count (a kind change is no removal)
        s = feshape.hit(eff.get("F", []), w["text"])
        return bool(s), s or ""
    if m == "M":
        for s in eff.get("M", []):
            mm = re.match(r"load:(.+):(\S+)>(\S+)$", s)
            if mm and w["x"] in mm.group(1) and mm.group(3) == str(w["n"]):
                return True, s
        return False, ""
    return False, ""


def want_lines(w, miner):
    """Source line offsets (in the function) near the wanted decision: candidates there are measured first."""
    try:
        if w["m"] == "S":
            js = miner.base.get("scratch")
            for p in realize._body_picks(js):
                if p["n"] == w["n"] and p.get("line") is not None:
                    return {p["line"] - 1, p["line"], p["line"] + 1}
        if w["m"] == "L":
            import c2slots
            s = miner.base.get("slots")
            i = c2slots.resolve(s, w["x"])
            L = (s["listing"].get(i) or {}).get("line")
            nm = c2slots.ename(s, i) or ""
            var = re.sub(r"\$.*$", "", nm).lstrip("_")
            ls = set()
            if L:
                ls.add(L - 1 - miner.c.line0 - 0)
            if var in miner.c.B.vars:
                ls |= {miner.c.lineoff(q) for q in miner.c.B.uses[var]}
            return ls
        if w["m"] == "R":
            ls = set()
            for v in (w.get("x"), w.get("y")):
                v = _strip_var(v or "")
                if v in miner.c.B.vars:
                    ls |= {miner.c.lineoff(q) for q in miner.c.B.uses[v]}
            if w.get("blk") and str(w["blk"]).isdigit():
                ls |= set(miner.base.fn.lines_of(int(w["blk"])))
            return ls
        if w["m"] == "K":
            v = _strip_var(w.get("x", ""))
            if v in miner.c.B.vars:
                return {miner.c.lineoff(q) for q in miner.c.B.uses[v][:1]}
    except Exception:
        pass
    return set()


# ---------------------------------------------------------------- ordering, mining loop, pairs

def prior(stats, cls, types=None):
    """Measured rate (Laplace-smoothed) of any model effect, or of the wanted types, for a class."""
    s = stats.get(cls)
    if not s or not s["n"]:
        return 0.5
    if types:
        return max((s["types"].get(t, 0) + 0.5) / (denom(s, t) + 1.0) for t in types)
    return (s["n"] - s["null"] + 0.5) / (s["n"] + 1.0)


def want_names(wants, M=None):
    names = set()
    for w in wants:
        for k in ("x", "y"):
            if w.get(k) and w["m"] in ("R", "K", "L"):
                names.add(_strip_var(w[k]))
        if w["m"] == "L" and M is not None:
            try:
                names.add(realize.slot_key_of(M.base.get("slots"), w["x"]).lstrip("_").split("#")[0].split("@")[0])
            except Exception:
                pass
    names.discard("")
    return {n for n in names if re.match(r"[A-Za-z_]\w*$", n)}


def order(cands, stats, wants, wlines, seed=1, names=()):
    """Every class sampled: round robin over classes ordered by their prior; inside a class, the sites nearest the
    wanted decision first, the rest spread over the function (shuffled)."""
    rnd = random.Random(seed)
    wtypes = [w["type"] for w in wants]
    by = collections.OrderedDict()
    for v in cands:
        by.setdefault(v["cls"], []).append(v)
    for cls, vs in by.items():
        rnd.shuffle(vs)
        if wlines:
            vs.sort(key=lambda v: min([abs((v["ctx"].get("line") or 999) - L) for L in wlines] or [999])
                    if v["ctx"].get("line") is not None else 50)
    pr = {cls: prior(stats, cls, wtypes or None) for cls in by}
    classes = sorted(by, key=lambda c: (-round(pr[c], 2), hashlib.md5(("%d" % seed + c).encode()).hexdigest()))
    out, k = [], 0
    if wlines:
        # the focus tier: every class's instances on the wanted decision's lines (+-1) first, round robin
        near = lambda v: v["ctx"].get("line") is not None and min(abs(v["ctx"]["line"] - L) for L in wlines) <= 1
        named = lambda v: any(re.search(r"\b%s\b" % re.escape(n), v["desc"]) for n in names)
        foc = collections.OrderedDict((c, sorted([v for v in by[c] if near(v)], key=lambda v: not named(v)))
                                      for c in classes)
        for c in classes:
            by[c] = [v for v in by[c] if not near(v)]
        while any(foc.values()):
            for c in classes:
                if foc[c]:
                    out.append(foc[c].pop(0))
    while any(by[c] for c in classes):
        for c in classes:
            # classes with a high prior get more picks per round
            take = 1 + int(3 * pr[c])
            for _ in range(take):
                if by[c]:
                    out.append(by[c].pop(0))
        k += 1
    return out, pr


def overlap(e1, e2):
    return any(a[0] < b[1] and b[0] < a[1] or a[0] == b[0] for a in e1 for b in e2)


def score_rec(rec, miner, wants):
    """Lower is better: wanted effects missed, then (rdiff, bdiff) against the original, then code changed."""
    hits = sum(1 for w in wants if want_hit(w, rec, miner)[0])
    return (-hits,) + okey(rec["code"]) + (rec["code"].get("code", 999),)


def run_mine(a, cl):
    if not isinstance(sys.stdout, realize._Mute):
        sys.stdout = realize._Mute(sys.stdout)
    db = load_db()
    stats = class_stats(db, build=None)
    try:
        M = Miner(a, cl)
    except RemoteDeclined as e:
        sys.exit("base: %s" % e)
    except (RuntimeError, SystemExit) as e:
        sys.exit("base: the source does not compile or %s is not in it: %s" % (a.func, str(e)[:300]))
    M.want_feats = set()
    M.eager_feats = set(f for f in (a.eager_feats or "").split(",") if f)
    wants = [parse_want(s) for s in (a.want or [])]
    for w in wants:
        if w.get("feat") and w["feat"] != "regs":
            M.want_feats.add(w["feat"])
            if w["feat"] not in M.feats:
                M.feats.append(w["feat"])
    for w in wants:
        w["type"] = want_type(w, M)
    print("%s: %d instructions, %d blocks, build %s%s; base compiled (%s)" % (
        M.func, len(M.base.fn.code), len(M.base.fn.blocks), M.build,
        "" if M.base_bdo is None else ", rdiff %s, bdiff %d against the original" % (M.base_osc["rdo"], M.base_bdo),
        M.out), flush=True)
    cands = generate(M.c)
    for i, v in enumerate(cands):
        v["helpers"] = sorted(helpers_used([tuple(e) for e in v["edits"]], v["tail"]))
        v["ctx"] = site_context(M.c, v)
    if a.classes:
        cands = [v for v in cands if re.search(a.classes, v["cls"])]
    if a.skip:
        cands = [v for v in cands if not re.search(a.skip, v["cls"])]
    cnt = collections.Counter(v["cls"].split(".")[0] for v in cands)
    print("%d mutations generated (%s)" % (len(cands), ", ".join("%s %d" % kv for kv in sorted(cnt.items()))), flush=True)
    wl = set()
    for w in wants:
        wl |= want_lines(w, M)
    for rg in (a.focus or "").split(","):
        if rg.strip():
            lo, _, hi = rg.partition("-") if not rg.startswith("-") else (rg, "", "")
            wl |= set(range(int(lo), int(hi or lo) + 1))
    cands, pr = order(cands, stats, wants, wl, names=want_names(wants, M))
    for i, v in enumerate(cands):
        v["id"] = i
    if a.list:
        for v in cands:
            print("  %4d %-24s %-5s %s" % (v["id"], v["cls"], v["ctx"].get("line"), v["desc"][:100]))
        shutil.rmtree(M.out, ignore_errors=True)
        return
    # records of this very source, build, flags already in the database: not compiled again
    known = {}
    if not a.fresh:
        for r in db:
            if r.get("fkey") == M.fkey and r.get("ok") is not None and r.get("cls") not in ("decl.helpers", "chain", "pair"):
                known[(repr(sorted(map(tuple, r["edits"]))), r.get("tail", ""))] = r
    t0 = time.time()
    recs, todo = [], []
    for v in cands:
        r = known.get((repr(sorted(map(tuple, v["edits"]))), v["tail"]))
        if r is not None:
            r = dict(r, id=v["id"])
            recs.append(r)
        else:
            todo.append(v)
    if recs:
        print("%d of them were measured before (database): not compiled again" % len(recs), flush=True)
    budget_main = a.max_compiles
    # the base's own secondary traces (their cost counts)
    for f in M.feats:
        if f != "regs":
            try:
                M.base.get(f)
            except Exception as e:
                print("  base: no %s trace (%s)" % (f, str(e)[:120]), flush=True)
    measured = []
    lock = threading.Lock()
    bad = [0]

    def one(v):
        if M.compiles >= budget_main or bad[0] > 20 or _LOCAL:
            return None
        r = M.measure(v)
        with lock:
            measured.append(r)
            if not r.get("ok") and "declined" in (r.get("err") or ""):
                bad[0] += 1
        M.write(r)
        return r

    with ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(one, todo))
    if _LOCAL:
        print("STOP: %d traced compiles were declined by the compile server; refused to run them under wine" %
              len(_LOCAL), flush=True)
    for r in measured:
        r["types"] = rec_types(r, M.base_osc) if r.get("ok") else []
        r["null"] = is_null(r)
    recs += measured
    dt = time.time() - t0
    report(M, recs, wants, stats, dt)
    if a.depth >= 2 and a.depth_compiles > 0:
        recs += deep_search(M, recs, wants, wl, stats, a)
    if (wants or M.orig is not None) and a.pairs:
        # the pairs have their own budget: --pairs compiles of the regs trace plus the wanted models' traces
        M.pair_limit = M.compiles + a.pairs * (1 + len(M.want_feats))
        pairs = pair_search(M, recs, wants, a)
        recs += pairs
    prune(M, recs, wants)
    left = [p for p in os.popen("ps -o pid=,command= -U %d" % os.getuid()).read().splitlines()
            if re.search(r"\bcl\.exe\b", p) and "wine" in p.lower() and "python" not in p]
    if left:
        print("note: cl.exe under wine is running on this machine (not this run's: every compile here went to the "
              "server):", len(left))
    print("compiles: %d traced (%s; %d variants measured, %d from the database); %.0fs" % (
        M.compiles, ", ".join("%s %d" % kv for kv in sorted(M.per_feat.items())), len(measured),
        len(recs) - len(measured), dt))
    return M, recs


def report(M, recs, wants, stats, dt):
    ok = [r for r in recs if r.get("ok")]
    fail = [r for r in recs if not r.get("ok")]
    null = [r for r in ok if is_null(r)]
    print("\n%d variants: %d compiled, %d failed, %d null (regs trace identical to the reference)" % (
        len(recs), len(ok), len(fail), len(null)))
    by = collections.OrderedDict()
    for r in recs:
        by.setdefault(r["cls"], []).append(r)
    print("\n## per class (this function): n ok null | neutral-with-effect | effect types seen")
    for cls in sorted(by):
        rs = by[cls]
        o = [r for r in rs if r.get("ok")]
        nn = [r for r in o if is_null(r)]
        neu = [r for r in o if not is_null(r) and r["code"].get("code") == 0]
        tc = collections.Counter(t for r in o for t in r.get("types", []) if not t.startswith("C.same"))
        print("  %-26s %3d %3d %3d | %3d | %s" % (cls, len(rs), len(o), len(nn), len(neu),
                                                 " ".join("%s:%d" % kv for kv in tc.most_common(8))))
    if M.orig is not None:
        best = sorted(ok, key=lambda r: okey(r["code"], 1 << 30) + (r["code"].get("code", 0),))[:10]
        print("\n## best against the original (base rdiff %s, bdiff %s)" % (
            (M.base_osc or {}).get("rdo"), M.base_bdo))
        for r in best:
            print("  rdiff %4s bdiff %4s  code %3d  %-24s %s" % (r["code"].get("rdo"), r["code"].get("bdo"),
                                                                 r["code"].get("code", -1), r["cls"], r["desc"][:90]))
    for w in wants:
        answer(M, recs, w, stats)


def answer(M, recs, w, stats):
    print("\n## want %s  (effect type %s)" % (w["text"], w["type"]))
    hits = []
    for r in recs:
        if not r.get("ok"):
            continue
        h, note = want_hit(w, r, M)
        if h:
            hits.append((r, note))
    hits.sort(key=lambda t: okey(t[0]["code"]) + (t[0]["code"].get("code", 999), len(t[0]["eff"].get(w["m"], []))))
    print("  this function: %d measured variants produce it" % len(hits))
    for r, note in hits[:12]:
        side = [s for s in r["eff"].get(w["m"], []) if s != note][:4]
        print("   %-24s code %3d bag %3d%s  %s\n%s     %s%s" % (
            r["cls"], r["code"].get("code", -1), r["code"].get("bag", -1),
            "" if r["code"].get("bdo") is None else " rdiff %s bdiff %d" % (r["code"].get("rdo"), r["code"]["bdo"]),
            r["desc"][:80], " " * 3, note,
            ("   also: " + " ".join(side)) if side else ""))
    rows = []
    for cls, s in stats.items():
        d = denom(s, w["type"])
        if d >= 2 and s["types"].get(w["type"]):
            rows.append((s["types"][w["type"]] / d, s["neutral"].get(w["type"], 0) / d, d, cls, len(s["funcs"])))
    rows.sort(reverse=True)
    print("  classes that produced %s elsewhere (database, before this run): rate, rate with code unchanged, n, functions" %
          w["type"])
    for rate, neu, n, cls, nf in rows[:10]:
        print("   %-26s %4.0f%% %4.0f%%  n=%d f=%d" % (cls, 100 * rate, 100 * neu, n, nf))


def deep_search(M, recs, wants, wl, stats, a):
    """Depth 2: the best code-neutral variants that changed something (preferring a change that names a wanted
    variable or element) become bases themselves; the generators run again on their text, near the wanted lines,
    and those second edits are measured (class `chain`; the record keeps both edit lists). This reaches what one
    edit cannot: an edit that is only possible after another (a declaration hoisted, then a test on it)."""
    names = want_names(wants, M)

    models = {w["m"] for w in wants if w["m"] != "C"} or set("RSLKQMF")

    def mention(r):        # a change in a wanted model that names a wanted variable or element
        return sum(1 for m in models for e in r["eff"].get(m, []) for n in names
                   if re.search(r"\b_?%s\b" % re.escape(n), e))
    ok = [r for r in recs if r.get("ok") and not r.get("null") and r["code"].get("code") == 0 and r.get("edits")
          and not r["cls"].startswith(("pair", "chain", "decl", "deffn", "move")) and r.get("base_edits") is None]
    ok.sort(key=lambda r: (-mention(r), -ogain(r["code"], M.base_osc), len(sum((r["eff"].get(m, []) for m in "RSLKQMF"), []))))
    beams = ok[:a.depth_beam]
    if not beams:
        return []
    print("\n## depth 2: second edits on %d code-neutral variants (%s)" % (
        len(beams), ", ".join("%s %s" % (b["cls"], b["desc"][:30]) for b in beams)), flush=True)
    out, lock = [], threading.Lock()
    limit = M.compiles + a.depth_compiles
    jobs = []
    for bi, b in enumerate(beams):
        t2 = apply_edits(M.text, [tuple(e) for e in b["edits"]], b.get("tail"))
        try:
            d = os.path.join(M.out, "deep%d" % bi)
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, "deep%d.cpp" % bi)
            open(p, "w").write(prelude(set(b.get("helpers", [])), M.have) + t2)
            fn2 = M.trace_regs(p, d)
            c2 = GenCtx(t2, M.func, fn2)
            c2._mtype = M.c._mtype
            cs = generate(c2)
        except (Exception, SystemExit) as e:
            print("  beam %d: %s" % (bi, str(e)[:120]))
            continue
        for v in cs:
            if v["cls"].startswith(("decl", "deffn", "move")):
                continue
            v["ctx"] = site_context(c2, v)
            if wl and (v["ctx"].get("line") is None or min(abs(v["ctx"]["line"] - L) for L in wl) > 3):
                continue
            v["helpers"] = sorted(set(helpers_used([tuple(e) for e in v["edits"]], v["tail"])) | set(b.get("helpers", [])))
            v["meta"] = dict(v.get("meta", {}), parts=[b["cls"], v["cls"]], first=b["desc"][:80])
            v["base_edits"], v["base_tail"] = b["edits"], b.get("tail")
            v["desc"] = "%s THEN %s" % (b["desc"][:50], v["desc"][:70])
            v["cls"] = "chain"
            jobs.append(v)
    seen = {(repr(sorted(map(tuple, r["edits"]))), repr(r.get("base_edits"))) for r in recs}
    jobs = [v for v in jobs if (repr(sorted(map(tuple, v["edits"]))), repr(v["base_edits"])) not in seen]
    # interleave the beams; inside each the second edit's classes round robin
    jobs, _pr = order(jobs, stats, wants, wl, names=names)
    for i, v in enumerate(jobs):
        v["id"] = "d%d" % i

    def one(v):
        if M.compiles >= limit:
            return None
        r = M.measure(v)
        r["types"] = rec_types(r, M.base_osc) if r.get("ok") else []
        r["null"] = is_null(r)
        M.write(r)
        with lock:
            out.append(r)
        return r
    saved = getattr(M, "pair_limit", None)
    M.pair_limit = limit
    with ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(one, jobs))
    if saved is None:
        del M.pair_limit
    else:
        M.pair_limit = saved
    print("  %d second edits measured" % len(out))
    for w in wants:
        hits = [r for r in out if r.get("ok") and want_hit(w, r, M)[0]]
        hits.sort(key=lambda r: okey(r["code"]) + (r["code"].get("code", 999),))
        print("  want %s: %d" % (w["text"], len(hits)))
        for r in hits[:6]:
            print("    code %3d%s  %s" % (r["code"].get("code", -1), "" if r["code"].get("bdo") is None else
                                       " rdiff %s bdiff %d" % (r["code"].get("rdo"), r["code"]["bdo"]), r["desc"][:140]))
    if M.orig is not None:
        for r in sorted([r for r in out if r.get("ok")], key=lambda r: okey(r["code"], 1 << 30))[:5]:
            print("    best: rdiff %s bdiff %s code %s  %s" % (r["code"].get("rdo"), r["code"].get("bdo"),
                                                           r["code"].get("code"), r["desc"][:140]))
    return out


def pair_search(M, recs, wants, a):
    """Pairs of variants that helped on their own (bdiff lower, or a wanted effect) and touch different text."""
    ok = [r for r in recs if r.get("ok") and r.get("edits") is not None]

    def gain(r):
        g = 0.0
        g += ogain(r["code"], M.base_osc)
        g += 3 * sum(1 for w in wants if want_hit(w, r, M)[0])
        return g
    good = sorted((r for r in ok if gain(r) > 0), key=lambda r: -gain(r))[:14]
    if len(good) < 2 or not a.pairs:
        return []
    combos = []
    for x, y in itertools.combinations(good, 2):
        ex, ey = [tuple(e) for e in x["edits"]], [tuple(e) for e in y["edits"]]
        if overlap(ex, ey) or (x.get("tail") and y.get("tail")) or x["cls"] == y["cls"] == "move.end":
            continue
        combos.append((-(gain(x) + gain(y)), x, y))
    combos.sort(key=lambda t: t[0])
    print("\n## pairs of independently helpful variants (%d helpful singles, %d pairs possible)" % (len(good), len(combos)))
    out = []
    lock = threading.Lock()

    def one(t):
        _g, x, y = t
        if M.compiles >= M.pair_limit:
            return None
        v = dict(cls="pair", fam="pair", desc="%s + %s" % (x["desc"][:60], y["desc"][:60]),
                 edits=x["edits"] + y["edits"], tail=(x.get("tail") or "") + (y.get("tail") or ""),
                 helpers=sorted(set(x.get("helpers", [])) | set(y.get("helpers", []))),
                 meta=dict(parts=[x["cls"], y["cls"]]), id="p%s_%s" % (x.get("id"), y.get("id")),
                 ctx=dict(where="pair"))
        r = M.measure(v)
        r["types"] = rec_types(r, M.base_osc) if r.get("ok") else []
        r["null"] = is_null(r)
        M.write(r)
        with lock:
            out.append(r)
        return r

    with ThreadPoolExecutor(a.jobs) as ex:
        list(ex.map(one, combos[:a.pairs]))
    for r in sorted(out, key=lambda r: score_rec(r, M, wants)):
        hs = [w["text"] for w in wants if want_hit(w, r, M)[0]]
        print("  %s  code %s%s  %s%s" % ("ok  " if r.get("ok") else "fail", r["code"].get("code", "-"),
                                          "" if r["code"].get("bdo") is None else " bdiff %d" % r["code"]["bdo"],
                                          r["desc"][:120], ("  wants: " + ",".join(hs)) if hs else ""))
    return out


def prune(M, recs, wants):
    """Keep the --keep-variants best variant sources (and no trace logs); delete the rest of the run directory."""
    ok = [r for r in recs if r.get("ok") and r.get("dir")]
    keep = set(r["dir"] for r in sorted(ok, key=lambda r: score_rec(r, M, wants))[:M.a.keep_variants])
    for r in recs:
        d = r.get("dir")
        if not d or not os.path.isdir(d):
            continue
        if d in keep:
            for f in os.listdir(d):
                p = os.path.join(d, f)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif not f.endswith(".cpp") and not f.endswith(".asm"):
                    os.remove(p)
        else:
            shutil.rmtree(d, ignore_errors=True)
    for root, dirs, files in os.walk(M.out):       # the references' traces (a large unit's log is 100+ MB each)
        for f in files:
            if f == "c2trace.log" or f.endswith((".obj", ".pdb")):
                os.remove(os.path.join(root, f))
    if keep:
        print("kept: %s" % ", ".join(sorted(os.path.basename(d) for d in keep)))


# ---------------------------------------------------------------- table / stats

TYPE_ORDER = ["R.ref+", "R.ref-", "R.P+", "R.P-", "R.tmp+", "R.var", "R.gone", "R.extend", "R.drop", "R.tie",
              "R.before", "R.pref+", "R.reg", "R.interf", "S.ins", "S.drop", "S.regs", "L.uses+", "L.uses-", "L.pos",
              "L.ord", "L.conf+", "L.conf-", "L.off", "K.frame+", "K.tmp+", "K.moved", "K.flips", "Q.flip", "M.load+",
              "M.load-", "C.regsonly", "C.bag0", "C.changed", "C.bdiff-"]


def denom(s, t):
    """The variants an effect type could be seen in: every compiled one for R and C, else those whose model was
    traced (S L K Q M run only where the regs trace changed, unless eager)."""
    m = t.split(".")[0]
    return s["n"] if m in ("R", "C") else s["traced"].get(m, 0)


def cmd_table(a):
    db = load_db()
    st = class_stats(db, build=a.build, by=a.by)
    rows = [(c, s) for c, s in st.items() if s["n"] >= a.min_n and (not a.cls or re.search(a.cls, c))]
    types = TYPE_ORDER
    seen = collections.Counter()
    for _c, s in rows:
        seen.update(s["types"])
    if a.type:                             # F types are open-ended (one per opcode): the most frequent seen ones
        dyn = [t for t, _n in seen.most_common() if t not in TYPE_ORDER and re.search(a.type, t)][:8]
        types = [t for t in TYPE_ORDER if re.search(a.type, t)] + dyn or [a.type]
    elif not a.all_types:
        types = [t for t in TYPE_ORDER if seen.get(t)]
    nf = len({r.get("fkey") for r in db})
    print("effect database: %d records, %d functions (source+build+flags)%s" % (len(db), nf,
                                                                              "" if not a.build else ", build " + a.build))
    print("per class: n compiled, f functions, failed, null %; then per effect type the % of variants that showed it, "
          "and in brackets the % that showed it with the instructions unchanged (registers masked). R and C types: "
          "of all compiled variants; S L K Q M: of those where that model was traced")
    print("%-26s %5s %3s %5s %5s  %s" % ("class", "n", "f", "fail", "null", "  ".join("%-9s" % t for t in types)))
    rows.sort(key=lambda t: (-(t[1]["n"] - t[1]["null"]) / max(1, t[1]["n"]), t[0]))
    for c, s in rows:
        n = s["n"]
        cells = []
        for t in types:
            k, u = s["types"].get(t, 0), s["neutral"].get(t, 0)
            d = denom(s, t)
            cells.append(("%3d(%3d)" % (100 * k / d, 100 * u / d)) if k and d else "    .    ")
        print("%-26s %5d %3d %4d%% %4d%%  %s" % (c[:34] if a.by else c[:26], n, len(s["funcs"]), 100 * s["fail"] // max(1, n + s["fail"]),
                                                100 * s["null"] // n, " ".join("%-9s" % x for x in cells)))


def cmd_stats(a):
    db = load_db()
    by = collections.Counter((r.get("func", "?")[:60], r.get("build")) for r in db)
    for (f, b), n in sorted(by.items()):
        print("  %-62s %-12s %d" % (f, b, n))
    sz = os.path.getsize(DB) if os.path.exists(DB) else 0
    print("%d records, %.1f MB" % (len(db), sz / 1e6))


def _on_term(signum, frame):
    """Killed (SIGTERM / SIGINT): stop the compile clients this process started, so none is left running."""
    import subprocess
    subprocess.run(["pkill", "-TERM", "-P", str(os.getpid())], capture_output=True)
    os._exit(128 + signum)


def main():
    import signal
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    argv = sys.argv[1:]
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd = argv.pop(0)
    if cmd == "_loads":
        return _loads_child(argv, cl)
    ap = argparse.ArgumentParser(prog="effectmine.py " + cmd, usage=__doc__)
    if cmd in ("mine", "query"):
        ap.add_argument("src")
        ap.add_argument("--func", required=True)
        ap.add_argument("--asm-vs")
        ap.add_argument("--want", nargs="+", default=[])
        ap.add_argument("--max-compiles", type=int, default=400)
        ap.add_argument("--features", default=",".join(FEATURES))
        ap.add_argument("--focus", help="line offsets in the function (LO-HI,..) measured first, as wanted lines")
        ap.add_argument("--classes")
        ap.add_argument("--skip")
        ap.add_argument("--eager", action="store_true", help="every feature for every variant")
        ap.add_argument("--eager-feats", default="", help="these features for every variant (e.g. keys)")
        ap.add_argument("--pairs", type=int, default=16)
        ap.add_argument("--depth", type=int, default=2, help="2: second edits on the best code-neutral variants")
        ap.add_argument("--depth-beam", type=int, default=4)
        ap.add_argument("--depth-compiles", type=int, default=60)
        ap.add_argument("--jobs", type=int, default=24)
        ap.add_argument("--keep-variants", type=int, default=12)
        ap.add_argument("--list", action="store_true")
        ap.add_argument("--fresh", action="store_true", help="measure again what the database already has")
        ap.add_argument("--verify", type=int, default=None)
        a = ap.parse_args(argv)
        if cmd == "query":
            if not a.want:
                sys.exit("query needs --want")
            if a.verify is None:
                a.max_compiles = 1 + len(a.features.split(","))     # the base and its references only
                a.pairs = 0
                a.depth = 1
            else:
                a.max_compiles = a.verify
            # what the database knows of this function is reused; only unmeasured instances are compiled
        run_mine(a, cl)
    elif cmd == "table":
        ap.add_argument("--build")
        ap.add_argument("--type")
        ap.add_argument("--class", dest="cls")
        ap.add_argument("--min-n", type=int, default=3)
        ap.add_argument("--all-types", action="store_true")
        ap.add_argument("--by", choices=["build", "vtype", "vkind", "stmt", "loop", "where"])
        cmd_table(ap.parse_args(argv))
    elif cmd == "stats":
        cmd_stats(ap.parse_args(argv))
    else:
        sys.exit("unknown command %r (mine, query, table, stats)" % cmd)


if __name__ == "__main__":
    main()
