#!/usr/bin/env python3
"""Search mechanical source rewrites of one function for ones that move registers without changing the code.

usage: rwsweep.py <tu.cpp> --func DECORATED [--asm-vs ORIG.s] [--want 'A>B'] [--pass K] [--families a,b,...] [--vars v,w]
                  [--jobs 8] [--max-combo 2] [--verify 8] [--limit N] [--list] [-- <cl args>]

helpersweep covers one rule (reference bindings in inline helpers of EH functions). This tool is the general
form: it does not know which rewrite will work, it enumerates every site of several rewrite families in the
target's own body, compiles each variant once with the regs trace, and sorts them by what they did.

Families (all keep the meaning of the program):
  this    member calls written without an object get an explicit `this->` (applied to the baseline itself, so
          that `this` is a visible token for the other families)
  wrap    one use of a variable, one member-access chain (`p->a.b`, `a[i]`) or one non-trivial call argument is
          passed through an inline identity helper: hs_id (by value), hs_cp (by value through a local) or
          hs_bind (reference bound to it; lvalues only)
  alias   `T a = V;` (value copy; only where V is not written afterwards in the block) or `T& a = V;` inserted
          at a statement boundary, with the later uses of V in that block renamed to the alias
  swap    two adjacent simple statements exchanged, when neither writes what the other mentions
  split   `T x = e;` written as `T x; x = e;`
  paren   redundant parentheses around a right-hand side, return value, call argument, additive term or left
          prefix (`(a + b) + c`): each pair on a float/double expression is an OPPAREN tuple that reaches
          allocation and scheduling; on integers it leaves nothing
  guard   `delete p; p = 0;` <-> `if (p) { delete p; p = 0; }`: same final code when the delete runs a
          destructor, but the reset store changes block at allocation time
  const   constants as register candidates (docs/c2/immcand.md): a char/short store of -1 or 255 respelled through
          the type of the other signedness (same byte stored, another candidate), and a for-init `T i = K` moved
          out of the for (over up to three statements) or back into it
  cflow   control flow respelled: if/else arms swapped under a negated condition, `while` as for(;;)+break,
          for(;c;), if+do-while or a goto loop, and the statement after an if copied into both arms
  touch   a statement that uses V and does nothing (`(void)V;`, an empty inline helper taking V by value or by
          reference, `if (V) { }`, `__assume(V != 0);`, `V = V;`), at every statement boundary: finds uses that
          leave a reference at allocation time and no code
  reread  after `X = p->m;`, a later use of X (before the next call or write) is spelled `(p->m)` again; the
          meaning is kept only if nothing stores to that memory in between, so check the hits by eye
  unalias  a reference or pointer alias of a member chain (`T& r = p->m;`, `T* q = &p->m;`, `T*& pf = flags;`)
          removed, its uses spelled as the chain: such an alias, used twice or more, is a variable c2 does not
          copy-propagate, so its reads never share a load with the chain's own reads (docs/c2/loadcse.md R2, R3)
  cmp     an if / while condition that is one relational compare: `b > a` for `a < b` (same meaning) and
          `!(a >= b)` (same for integers; for floats it differs only when an operand is NaN, so check the hit
          against the original's NaN behaviour): on x87 it decides the load order, the compare form and the branch
  subst   `T v = E;` read once afterwards with no call, store or loop in between: the declaration removed and the
          read spelled `((T)(E))` (scalar or pointer T); one variable and one front-end assignment fewer
  repair  (not in the default set; --families repair) rewrites that CHANGE the meaning, for a draft whose
          statement differs from the original's (icount `structure`): a statement or an early `if (..) return;`
          removed, `L = C` as `L |= C` / `L &= C` (and back), `<` <-> `<=` / `>` <-> `>=` in a condition, a local's
          signedness flipped, `X = --N` as `X = N - 1`. Each hit is a hypothesis about the original's source
  recompute  (not in the default set; --families recompute) `V = E;` with E made of literals and pure math calls
          (`dq = sqrt(13.0 / 8.0);`) is written again before a later statement that uses V after a call: the same
          value, computed once as before, but V is then one live range per call-free stretch instead of one
          across the calls (float values: docs/c2/x87.md section 10)
  byval   a call that returns a class by value (`map.find(&key)`, the return type found in the unit's declarations)
          passed through a generic by-value inline forwarding template placed at file scope before the target:
          `rz_bv_find1(map, &key)`, `template <class M, class A0> inline typename M::It rz_bv_find1(M& m,
          const A0& a0) { return m.find(a0); }`. It changes code: the result is read from the wrapper's return
          temporary, not through the returned pointer `[eax]` (Spore 005f0a60, with `alias` on the iterator: MATCH)

Measures per variant:
  shape    aligned diff against the baseline with every register except esp masked: 0 = the same instructions
           in the same order, only registers may differ ("code-neutral")
  regs     aligned diff against the baseline including registers: 0 = nothing changed at all
  bdiff    (--asm-vs) the byte-faithful score against the original (tsweep.bdiff: whole instructions, with
           displacements and immediates; relocations, call/jump targets and padding masked): the number to bring
           to 0, and the sort key
  regdiff  (--asm-vs) the same alignment over mnemonic + registers only (tsweep.regdiff)
  dgap     (--want A>B) change of prio(B) - prio(A); the constraint holds when the gap is <= 0

Output: every variant that changed something, best first; then combinations of the code-neutral ones that
helped (gaps add up, as in helpersweep), compiled and reported. Variants stay in work/rwsweep/<tu>-<dirhash>/.
"""
import argparse, difflib, itertools, os, re, shutil, sys
import hashlib
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from image import ROOT
import c2trace, c2regs, tsweep, helpersweep

PRELUDE = ("template<class HsT> __forceinline HsT& hs_bind(HsT& x) { HsT& r = x; return r; }\n"
           "template<class HsT> __forceinline HsT hs_id(HsT x) { return x; }\n"
           "template<class HsT> __forceinline HsT hs_cp(HsT x) { HsT r = x; return r; }\n"
           "template<class HsT> __forceinline void hs_nop(HsT) { }\n"
           "template<class HsT> __forceinline void hs_nopr(const HsT&) { }\n")
TOK = re.compile(r"""\s+|//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_]\w*|\d[\w.]*"""
                 r"""|->\*?|\+\+|--|<<=|>>=|[-+*/%&|^!=<>]=|&&|\|\||::|<<|>>|.""", re.S)
KEYWORDS = set("if else while for do switch case default return break continue goto sizeof new delete throw try catch "
               "const volatile static register unsigned signed struct class union enum typedef typename template "
               "this true false".split())
ASSIGN = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}
NOCALL = {"if", "while", "for", "switch", "sizeof", "return", "catch", "hs_bind", "hs_id", "hs_cp", "hs_nop", "hs_nopr",
          "__assume"}
DECL = re.compile(r"((?:const\s+)?(?:(?:unsigned|signed)\s+)?[A-Za-z_][\w:]*(?:\s+const)?(?:\s*[*&]+(?:\s*const\b)?\s*|\s+))"
                  r"([A-Za-z_]\w*)\s*(=|;)")


def lex(text, lo, hi):
    """Tokens of text[lo:hi]: (text, start, end), without spaces and comments."""
    out = []
    for m in TOK.finditer(text, lo, hi):
        t = m.group()
        if t.isspace() or t.startswith("//") or t.startswith("/*"):
            continue
        out.append((t, m.start(), m.end()))
    return out


class Body:
    """The target's body as tokens with block structure, declared variables and their uses."""

    def __init__(self, text, fn, cls):
        self.text, self.cls = text, cls
        name, params, b, e, d = fn
        self.b, self.e = b, e
        self.d = d                      # where the definition's name starts (the return type may come before it)
        self.toks = lex(text, b, e)
        n = len(self.toks)
        self.paren = [0] * n            # paren depth before the token
        self.block = [0] * n            # index of the innermost `{` token enclosing the token
        self.close = {}                 # `{` token index -> `}` token index
        stack, depth = [], 0
        for i, (t, s, _) in enumerate(self.toks):
            self.paren[i] = depth
            self.block[i] = stack[-1] if stack else 0
            if t in "([" and len(t) == 1:
                depth += 1
            elif t in ")]" and len(t) == 1:
                depth -= 1
                self.paren[i] = depth
            elif t == "{":
                stack.append(i)
            elif t == "}":
                self.close[stack.pop()] = i
                self.block[i] = stack[-1] if stack else 0
        # statement starts: token index following `;` `{` `}` at paren depth 0, not before `else`/`}`
        self.starts = []
        for i in range(1, n - 1):
            # (`)` after the `;` of an empty for-increment `for (a; b; )` sits at depth 0 once closed: not a start)
            if self.toks[i - 1][0] in (";", "{", "}") and self.paren[i] == 0 and self.toks[i][0] not in ("else", "}", ")"):
                if self.toks[i - 1][0] == "}" and self.toks[i][0] in ("while",):      # do { } while
                    continue
                self.starts.append(i)
        # variables: parameters, this, locals
        self.vars = {}                  # name -> (type text, decl token index or -1)
        for p in params.split(","):
            m = re.match(r"\s*(.*?[\s*&])(\w+)\s*$", p)
            if m and m.group(1).strip():
                self.vars[m.group(2)] = (m.group(1).strip(), -1)
        if cls:
            self.vars["this"] = (cls + "*", -1)
        for i in self.starts:
            s = self.toks[i][1]
            m = DECL.match(text, s)
            if not m or m.end() > e:
                continue
            ty = m.group(1).strip()
            if ty.split()[0] in KEYWORDS - {"const", "unsigned", "signed"} or ty in self.vars:
                continue
            k = next(j for j in range(i, n) if self.toks[j][1] == m.start(2))
            self.vars[m.group(2)] = (ty, k)
        self.uses = {v: [] for v in self.vars}
        for i, (t, s, _) in enumerate(self.toks):
            if t in self.vars and self.vars[t][1] != i and (i == 0 or self.toks[i - 1][0] not in (".", "->", "::")):
                self.uses[t].append(i)

    def chain_end(self, i):
        """Token index just past the member/index chain starting at identifier token i (stops before a method)."""
        k, n = i + 1, len(self.toks)
        while k < n:
            t = self.toks[k][0]
            if t in ("->", ".") and k + 2 < n and self.toks[k + 2][0] != "(":
                k += 2
            elif t == "[":
                d = 0
                while k < n:
                    d += self.toks[k][0] == "["
                    d -= self.toks[k][0] == "]"
                    k += 1
                    if d == 0:
                        break
            else:
                break
        return k

    def written(self, i, j):
        """Is the expression at tokens [i, j) assigned, stepped or address-taken?"""
        nxt = self.toks[j][0] if j < len(self.toks) else ""
        prv = self.toks[i - 1][0] if i else ""
        if nxt in ASSIGN or nxt in ("++", "--") or prv in ("++", "--"):
            return True
        if prv == "&":
            pp = self.toks[i - 2][0] if i > 1 else ""
            return not (re.match(r"[\w)\]]", pp[-1:]) and pp not in KEYWORDS)
        return False

    def src(self, i, j):
        return self.text[self.toks[i][1]:self.toks[j - 1][2]]


def _call_arg(B, i, j):
    """(callee name, argument index) when tokens [i, j) are one whole argument of a call `f(..)` / `p->f(..)`."""
    if not i or B.toks[i - 1][0] not in ("(", ",") or j >= len(B.toks) or B.toks[j][0] not in (",", ")"):
        return None
    d, k, idx = 0, i - 1, 0
    while k >= 0:
        t = B.toks[k][0]
        if t in (")", "]"):
            d += 1
        elif t in ("(", "["):
            if d == 0:
                break
            d -= 1
        elif t == "," and d == 0:
            idx += 1
        k -= 1
    if k < 1 or B.toks[k][0] != "(" or not re.match(r"[A-Za-z_]\w*$", B.toks[k - 1][0]) or B.toks[k - 1][0] in KEYWORDS:
        return None
    return B.toks[k - 1][0], idx


_PARAMS = {}


def ref_params(text, name):
    """Indices of the parameters of every declaration of function `name` in the unit that are non-const lvalue
    references (`bool& any`): an argument there must stay an lvalue, so hs_id / hs_cp (prvalues) cannot wrap it
    (C2664 `bool` to `bool &`, Spore 0073afb0). Overloads are merged: an index counts if any overload has it."""
    key = (id(text), name)
    if key not in _PARAMS:
        out = set()
        for m in re.finditer(r"\b%s\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*(?:const\s*)?[{;:]" % re.escape(name), text):
            for k, prm in enumerate(m.group(1).split(",")):
                prm = re.sub(r"=.*", "", prm)
                if re.search(r"(?<!&)&(?!&)", prm) and not re.search(r"\bconst\b[^&]*&", prm):
                    out.add(k)
        _PARAMS[key] = out
    return _PARAMS[key]


def _lvalue_arg(B, i, j):
    """Is [i, j) passed to a non-const reference parameter of a function declared in the unit?"""
    ca = _call_arg(B, i, j)
    return bool(ca) and ca[1] in ref_params(B.text, ca[0])


def gen_wrap(B):
    seen = set()
    for v, uses in B.uses.items():
        for i in uses:
            for j in sorted({i + 1, B.chain_end(i)}):
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                expr, w = B.src(i, j), B.written(i, j) or _lvalue_arg(B, i, j)
                decl = j < len(B.toks) and B.toks[j][0] in ASSIGN and i in B.starts and v == "this"
                for h in (("hs_bind",) if w else ("hs_id", "hs_cp", "hs_bind")):
                    if (h == "hs_bind" and expr == "this") or decl:
                        continue
                    yield ("wrap", "%s(%s) @%d" % (h, expr, B.toks[i][1] - B.b),
                           [(B.toks[i][1], B.toks[j - 1][2], "%s(%s)" % (h, expr))])
    # non-trivial call arguments
    for i, (t, s, e) in enumerate(B.toks[:-1]):
        if not re.match(r"[A-Za-z_]", t) or t in NOCALL or t in KEYWORDS or B.toks[i + 1][0] != "(":
            continue
        d, k, a0, args = 0, i + 1, i + 2, []
        while k < len(B.toks):
            x = B.toks[k][0]
            d += x in ("(", "[")
            d -= x in (")", "]")
            if (x == "," and d == 1) or d == 0:
                if k > a0:
                    args.append((a0, k))
                a0 = k + 1
                if d == 0:
                    break
            k += 1
        refs = ref_params(B.text, t)
        for ai, (a, z) in enumerate(args):
            if z - a < 2 or (z - a == 2 and B.toks[a][0] == "&") or B.toks[a][0] in ("hs_id", "hs_cp"):
                continue
            if B.toks[a][0] == "&" or (a, z) in seen or ai in refs:      # a prvalue cannot bind to T&
                continue
            expr = B.src(a, z)
            for h in ("hs_id", "hs_cp"):
                yield ("wrap", "arg %s(%s) of %s" % (h, expr[:40], t), [(B.toks[a][1], B.toks[z - 1][2], "%s(%s)" % (h, expr))])


SCALAR = set("void bool char short int long float double unsigned signed size_t wchar_t __int64 uint8_t uint16_t "
             "uint32_t uint64_t int8_t int16_t int32_t int64_t uint uchar ushort ulong BOOL DWORD WORD BYTE HRESULT".split())


def byval_returns(text):
    """{function name: [(return type text, enclosing class or None, nested)]} for the declarations in the unit whose
    return type is a class by value (no pointer or reference, a struct/class/template the unit defines or names).
    `nested`: the type is declared inside the enclosing class (`struct AreaMap { struct It {..}; It find(..); }`),
    so a wrapper template spells it `typename M::It`."""
    bare = re.sub(r"//[^\n]*|/\*.*?\*/", lambda c: re.sub(r"[^\n]", " ", c.group(0)), text, flags=re.S)
    classes = []                                  # (name, body start, body end)
    for m in re.finditer(r"\b(?:struct|class|union)\s+(?:__declspec\([^)]*\)\s*)?(\w+)\s*(?::[^{;]*)?\{", bare):
        b = m.end() - 1
        classes.append((m.group(1), b, helpersweep.match_brace(bare, b)))
    out = {}
    for m in re.finditer(r"(?:^|[;{}:])\s*(?:(?:static|virtual|inline|__forceinline|explicit)\s+)*((?:const\s+)?"
                         r"(?:typename\s+)?[A-Za-z_][\w:]*(?:\s*<[^;{}()]*?>)?(?:::\w+)?)\s+"
                         r"(?:\w+\s*::\s*)?([A-Za-z_]\w*)\s*\(", bare):
        rt, name = m.group(1).strip(), m.group(2)
        base = re.sub(r"\b(const|typename)\b", " ", rt).strip()
        if name in KEYWORDS or base in SCALAR or base.split()[0] in KEYWORDS | {"return", "else", "new", "delete"} \
                or name == base or name.startswith("rz_") or name.startswith("hs_"):
            continue
        if not _is_class(bare, base):
            td = re.search(r"\btypedef\s+(?:typename\s+)?([^;{}]+?)\s+%s\s*;" % re.escape(base.split("::")[-1]), bare)
            if not td or "*" in td.group(1) or "&" in td.group(1) or not _is_class(bare, td.group(1)):
                continue
        pos = m.start(2)
        enc = [c for c in classes if c[1] < pos < c[2]]
        enc = min(enc, key=lambda c: c[2] - c[1]) if enc else None
        nested = bool(enc) and bool(re.search(r"\b(?:struct|class|union)\s+%s\b|\btypedef\b[^;]*\b%s\s*;" % (
            re.escape(base), re.escape(base)), bare[enc[1]:enc[2]]))
        ent = (rt, enc[0] if enc else None, nested)
        if ent not in out.setdefault(name, []):
            out[name].append(ent)
    return out


def _file_scope_pos(text, at):
    """Where a declaration can go at file scope before position `at` (a definition's name): before the outermost
    brace block (namespace, class) around it, else on the line after the previous `;` or `}` (the definition's return
    type and the comments above it stay together after the insertion)."""
    bare = re.sub(r"//[^\n]*|/\*.*?\*/", lambda c: re.sub(r"[^\n]", " ", c.group(0)), text, flags=re.S)
    for m in re.finditer(r"\b(?:namespace|struct|class)\s*\w*\s*(?::[^{;]*)?\{", bare[:at]):
        b = m.end() - 1
        if helpersweep.match_brace(bare, b) > at:
            return text.rfind("\n", 0, m.start()) + 1
    p = max(bare.rfind(";", 0, at), bare.rfind("}", 0, at))
    if p < 0:
        return text.rfind("\n", 0, at) + 1
    nl = text.find("\n", p)
    return nl + 1 if 0 <= nl < at else text.rfind("\n", 0, at) + 1


def _obj_start(B, k):
    """Token index where the object expression ending just before token k (`.` / `->`) starts, or None."""
    j = k - 1
    while j >= 0:
        t = B.toks[j][0]
        if t == "]":
            d = 0
            while j >= 0:
                d += B.toks[j][0] == "]"
                d -= B.toks[j][0] == "["
                j -= 1
                if d == 0:
                    break
            continue
        if not re.match(r"[A-Za-z_]\w*$", t) or t in KEYWORDS - {"this"}:
            return None
        if j > 0 and B.toks[j - 1][0] in (".", "->"):
            j -= 2
            continue
        return j
    return None


def gen_byval(B):
    """A call returning a class by value passed through a generic by-value inline forwarding template:
    `map.find(&key)` -> `rz_bv_find(map, &key)` with
    `template <class M, class A0> inline typename M::It rz_bv_find(M& m, const A0& a0) { return m.find(a0); }`
    placed at file scope before the target (outside its namespace or class). The returned object then goes through
    the wrapper's own return temporary: Spore 005f0a60 (`it = r2_find(m, k)`, then the erase through a reference
    to `it`: MATCH) read the iterator from the temp instead of through the returned pointer `[eax]`."""
    rets = byval_returns(B.text)
    if not rets:
        return
    fn_at = getattr(B, "d", B.b)
    n = len(B.toks)
    for i in range(1, n - 1):
        t = B.toks[i][0]
        if t not in rets or B.toks[i + 1][0] != "(":
            continue
        prev = B.toks[i - 1][0]
        if prev in (".", "->"):
            j = _obj_start(B, i - 1)
            if j is None:
                continue
            obj = B.src(j, i - 1)
            obj = "*" + obj if prev == "->" else obj
            kinds = [e for e in rets[t] if e[1]]
            start = j
        elif prev in ("::",):
            continue
        else:
            kinds = [e for e in rets[t] if e[1] is None]
            if B.cls and any(e[1] == B.cls for e in rets[t]):
                kinds, obj = [e for e in rets[t] if e[1] == B.cls], "*this"
            else:
                obj = None
            start = i
        if not kinds:
            continue
        # the argument list
        d, k, a0, args = 0, i + 1, i + 2, []
        while k < n:
            x = B.toks[k][0]
            d += x in ("(", "[")
            d -= x in (")", "]")
            if (x == "," and d == 1) or d == 0:
                if k > a0:
                    args.append(B.src(a0, k))
                a0 = k + 1
                if d == 0:
                    break
            k += 1
        if k >= n:
            continue
        rt, owner, nested = kinds[0]
        na = len(args)
        wname = "rz_bv_%s%s" % (t, na)
        tps = (["class M"] if obj else []) + ["class A%d" % q for q in range(na)]
        prm = (["M& m"] if obj else []) + ["const A%d& a%d" % (q, q) for q in range(na)]
        call = ("m.%s" % t if obj else t) + "(%s)" % ", ".join("a%d" % q for q in range(na))
        rts = "typename M::%s" % rt.split("::")[-1] if (nested and obj) else rt
        if not tps:
            continue                               # a free function without arguments: nothing to deduce
        decl = "template <%s> inline %s %s(%s) { return %s; }\n" % (", ".join(tps), rts, wname, ", ".join(prm), call)
        if wname in B.text:
            continue
        at = _file_scope_pos(B.text, fn_at)
        repl = "%s(%s)" % (wname, ", ".join(([obj] if obj else []) + args))
        yield ("byval", "%s through a by-value inline wrapper (returns %s) at body line %d" % (
            B.src(start, k + 1)[:50], rts, B.text.count("\n", B.b, B.toks[i][1])),
            [(at, at, decl), (B.toks[start][1], B.toks[k][2], repl)])


def gen_alias(B):
    for v, (ty, decl) in B.vars.items():
        if "&" in ty:
            continue
        for st in B.starts:
            if decl >= st:
                continue
            if decl >= 0:
                blk = B.block[decl]
                if blk and not (blk < st < B.close.get(blk, 1 << 30)):
                    continue
            blk = B.block[st]
            end = B.close.get(blk, len(B.toks) - 1)
            inside = [i for i in B.uses[v] if st <= i < end]
            if not inside or B.toks[st][0] in ("case", "default"):
                continue
            wr = any(B.written(i, i + 1) for i in inside)
            pos = B.toks[st][1]
            ren = [(B.toks[i][1], B.toks[i][2], "rw_a") for i in inside]
            line = B.text.count("\n", B.b, pos)
            forms = [] if v == "this" else [("ref", "%s& rw_a = %s; " % (ty, v))]
            if not wr:
                forms.append(("val", "%s rw_a = %s; " % (ty, v)))
            for nm, ins in forms:
                yield ("alias", "%s %s at body line %d (%d uses)" % (nm, v, line, len(inside)), [(pos, pos, ins)] + ren)


def _stmt(B, k):
    """(first token, one past the `;`) of the simple statement starting at B.starts[k], or None."""
    i = B.starts[k]
    if B.toks[i][0] in KEYWORDS - {"const", "unsigned", "signed", "this"}:
        return None
    j = i
    while j < len(B.toks) and B.toks[j][0] != ";":
        if B.toks[j][0] in ("{", "}"):
            return None
        j += 1
    return i, j + 1


def _facts(B, i, j):
    toks = [B.toks[k][0] for k in range(i, j)]
    ids = {t for t in toks if re.match(r"[A-Za-z_]", t) and t not in KEYWORDS}
    wr, mem, memw = set(), False, False
    for k in range(i, j):
        t = B.toks[k][0]
        if t in ASSIGN or t in ("++", "--"):
            lhs = [B.toks[q][0] for q in range(i, k)]
            base = [x for x in lhs if re.match(r"[A-Za-z_]", x) and x not in KEYWORDS]
            if "->" in lhs or "[" in lhs or "*" in lhs[:1]:
                memw = True
            if base:
                wr.add(base[-1] if not ("->" in lhs or "[" in lhs) else base[0])
            if t in ("++", "--") and k + 1 < j:
                wr.add(B.toks[k + 1][0])
        if t in ("->", "[") or (t == "(" and k > i and re.match(r"[A-Za-z_]", B.toks[k - 1][0])
                                and B.toks[k - 1][0] not in KEYWORDS):
            mem = True
            if t == "(":
                memw = True
        if t == "*" and (k == i or not re.match(r"[\w)\]]", B.toks[k - 1][0][-1:])):
            mem = True
        if t == "&" and k + 1 < j and B.toks[k + 1][0] in B.vars and not re.match(r"[\w)\]]", B.toks[k - 1][0][-1:]):
            wr.add(B.toks[k + 1][0])
    m = DECL.match(B.text, B.toks[i][1])
    if m and m.end() <= B.toks[j - 1][2] + 1:
        wr.add(m.group(2))
    return ids, wr, mem, memw


def gen_swap(B):
    for k in range(len(B.starts) - 1):
        a, b = _stmt(B, k), _stmt(B, k + 1)
        if not a or not b or a[1] != b[0] or B.block[a[0]] != B.block[b[0]]:
            continue
        ia, wa, ma, mwa = _facts(B, *a)
        ib, wb, mb, mwb = _facts(B, *b)
        if wa & ib or wb & ia or (ma and mb and (mwa or mwb)):
            continue
        sa, sb = B.src(*a), B.src(*b)
        line = B.text.count("\n", B.b, B.toks[a[0]][1])
        yield ("swap", "body line %d: `%s` <-> `%s`" % (line, sa[:30], sb[:30]),
               [(B.toks[a[0]][1], B.toks[b[1] - 1][2], sb + " " + sa)])


def gen_split(B):
    for k in range(len(B.starts)):
        s = _stmt(B, k)
        if not s:
            continue
        m = DECL.match(B.text, B.toks[s[0]][1])
        depth, multi = 0, False                 # `T a = x, b = y;`: `T a; a = x, b = y;` would not declare b
        for q in range(s[0], s[1]):
            t = B.toks[q][0]
            depth += t in ("(", "[", "{") or -(t in (")", "]", "}"))
            multi = multi or (t == "," and depth == 0)
        if m and not multi and m.group(3) == "=" and m.group(2) in B.vars and "&" not in m.group(1) and \
                "const" not in m.group(1):
            yield ("split", "%s %s" % (m.group(1), m.group(2)),
                   [(m.start(), m.end(), "%s %s; %s =" % (m.group(1), m.group(2), m.group(2)))])


def gen_reread(B):
    """`X = E;` with E a memory read (`p->m`, `a[i]`): a later use of X, before the next call or write to X or to
    a variable of E, is spelled `(E)` again. Same meaning only if nothing stores to that memory in between."""
    for k in range(len(B.starts)):
        s = _stmt(B, k)
        if not s:
            continue
        i, j = s
        eq = next((q for q in range(i, j) if B.toks[q][0] == "="), None)
        if eq is None or eq == i or B.toks[eq - 1][0] not in B.vars or (eq - 1 > i and B.toks[eq - 2][0] in ("->", ".", "]")):
            continue
        x = B.toks[eq - 1][0]
        rhs = [B.toks[q][0] for q in range(eq + 1, j - 1)]
        if not ("->" in rhs or "[" in rhs) or any(t in ASSIGN or t in ("++", "--", "?") for t in rhs):
            continue
        if any(re.match(r"[A-Za-z_]", rhs[q]) and rhs[q] not in KEYWORDS and rhs[q + 1] == "(" for q in range(len(rhs) - 1)):
            continue
        ids = {t for t in rhs if t in B.vars} | {x}
        expr = B.src(eq + 1, j - 1)
        end = B.close.get(B.block[i], len(B.toks) - 1)
        for q in range(j, end):
            t = B.toks[q][0]
            if re.match(r"[A-Za-z_]", t) and t not in KEYWORDS and t not in NOCALL and B.toks[q + 1][0] == "(":
                break
            if t in ("while", "for", "do", "goto") or (t in ids and B.written(q, q + 1)):
                break
            if t == x and q in B.uses[x]:
                line = B.text.count("\n", B.b, B.toks[q][1])
                yield ("reread", "%s -> (%s) at body line %d" % (x, expr, line), [(B.toks[q][1], B.toks[q][2], "(" + expr + ")")])


def _pclose(B, i):
    """Token index of the `)` matching the `(` at i."""
    d = 0
    for k in range(i, len(B.toks)):
        d += B.toks[k][0] == "("
        d -= B.toks[k][0] == ")"
        if d == 0:
            return k
    return len(B.toks) - 1


def stmt_end(B, i):
    """Token index one past the statement starting at token i."""
    t = B.toks[i][0]
    if t == "{":
        return B.close[i] + 1
    if t == "if":
        e = stmt_end(B, _pclose(B, i + 1) + 1)
        return stmt_end(B, e + 1) if e < len(B.toks) and B.toks[e][0] == "else" else e
    if t in ("while", "for", "switch"):
        return stmt_end(B, _pclose(B, i + 1) + 1)
    if t == "do":
        e = stmt_end(B, i + 1)
        return _pclose(B, e + 1) + 2
    k = i
    while B.toks[k][0] != ";":
        k += 1
    return k + 1


def _txt(B, i, j):
    return B.text[B.toks[i][1]:B.toks[j - 1][2]]


def gen_cflow(B):
    """Control-flow respellings with the same meaning: arms swapped under a negated condition, loop forms, and
    the statement after an if moved into both arms (c2 may merge the copies again after allocation)."""
    n = len(B.toks)
    for i in range(1, n - 1):
        t = B.toks[i][0]
        line = B.text.count("\n", B.b, B.toks[i][1])
        if t == "if" and B.toks[i + 1][0] == "(":
            pc = _pclose(B, i + 1)
            cond = _txt(B, i + 2, pc)
            e1 = stmt_end(B, pc + 1)
            s1 = _txt(B, pc + 1, e1)
            has_else = e1 < n and B.toks[e1][0] == "else"
            e2 = stmt_end(B, e1 + 1) if has_else else e1
            s2 = _txt(B, e1 + 1, e2) if has_else else ""
            a, z = B.toks[i][1], B.toks[e2 - 1][2]
            if has_else:
                yield ("cflow", "swap arms of if at body line %d" % line,
                       [(a, z, "if (!(%s)) { %s } else { %s }" % (cond, s2, s1))])
            else:
                yield ("cflow", "empty then, body in else, if at body line %d" % line,
                       [(a, z, "if (!(%s)) { } else { %s }" % (cond, s1))])
            if B.toks[i - 1][0] != "else" and e2 < n and B.toks[e2][0] not in ("}", "else") and B.paren[i] == 0:
                e3 = stmt_end(B, e2)
                tail = _txt(B, e2, e3)
                if not DECL.match(B.text, B.toks[e2][1]) and B.toks[e2][0] not in ("case", "default", "break", "continue"):
                    yield ("cflow", "statement after if at body line %d copied into both arms" % line,
                           [(a, B.toks[e3 - 1][2], "if (%s) { %s %s } else { %s %s }" % (cond, s1, tail, s2, tail))])
        if t == "while" and B.toks[i - 1][0] in (";", "{", "}") and B.toks[i + 1][0] == "(":
            pc = _pclose(B, i + 1)
            if B.toks[pc + 1][0] == ";":
                continue                                   # the tail of do-while
            cond = _txt(B, i + 2, pc)
            e1 = stmt_end(B, pc + 1)
            body = _txt(B, pc + 1, e1)
            a, z = B.toks[i][1], B.toks[e1 - 1][2]
            has_cont = bool(re.search(r"\bcontinue\b", body))
            yield ("cflow", "while at body line %d as for(;;) with break" % line,
                   [(a, z, "for (;;) { if (!(%s)) break; %s }" % (cond, body))])
            yield ("cflow", "while at body line %d as for(;cond;)" % line, [(a, z, "for (; %s; ) %s" % (cond, body))])
            if not has_cont:
                yield ("cflow", "while at body line %d as if + do-while" % line,
                       [(a, z, "if (%s) { do %s while (%s); }" % (cond, body, cond))])
                yield ("cflow", "while at body line %d as goto loop" % line,
                       [(a, z, "rw_top%d: if (%s) { %s goto rw_top%d; }" % (i, cond, body, i))])


CMP_MIRROR = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}
CMP_NEG = {"<": ">=", ">": "<=", "<=": ">", ">=": "<"}


def gen_cmp(B):
    """An if / while condition that is one relational compare `A op B` (nothing else at its top level): written
    `B op' A` (mirrored; the same meaning) and `!(A neg B)` (negated; the same for integers, for floats it differs
    when an operand is NaN). On x87 the spelling decides which operand is loaded first, the compare form and the
    branch: Spore 00c22a80 (`if (fDist < fCap)` -> `!(fDist >= fCap)`) on 15.00."""
    n = len(B.toks)
    for i in range(1, n - 1):
        if B.toks[i][0] not in ("if", "while") or B.toks[i + 1][0] != "(":
            continue
        pc = _pclose(B, i + 1)
        if B.toks[i][0] == "while" and pc + 1 < n and B.toks[pc + 1][0] == ";":
            continue                                       # the tail of a do-while: fine too, but keep it simple
        ops, d = [], 0
        for q in range(i + 2, pc):
            t = B.toks[q][0]
            d += t in ("(", "[") or -(t in (")", "]"))
            if d == 0 and t in ("<", ">", "<=", ">=", "==", "!=", "&&", "||", "?", ",", "=", "<<", ">>"):
                ops.append((q, t))
        if len(ops) != 1 or ops[0][1] not in CMP_MIRROR:
            continue
        q, op = ops[0]
        if any(B.toks[k][0] in ("<", ">") and B.toks[k - 1][0] in ("static_cast", "const_cast", "reinterpret_cast")
               for k in range(i + 2, pc)):
            continue
        a, b = _txt(B, i + 2, q), _txt(B, q + 1, pc)
        line = B.text.count("\n", B.b, B.toks[i][1])
        s, z = B.toks[i + 2][1], B.toks[pc - 1][2]
        yield ("cmp", "%s at body line %d: `%s %s %s`" % (B.toks[i][0], line, b.strip(), CMP_MIRROR[op], a.strip()),
               [(s, z, "%s %s %s" % (b.strip(), CMP_MIRROR[op], a.strip()))])
        yield ("cmp", "%s at body line %d: `!(%s %s %s)` (floats: differs for NaN)" % (
            B.toks[i][0], line, a.strip(), CMP_NEG[op], b.strip()),
               [(s, z, "!(%s %s %s)" % (a.strip(), CMP_NEG[op], b.strip()))])


def _fully_wrapped(B, i, j):
    return B.toks[i][0] == "(" and _pclose(B, i) == j - 1


def gen_paren(B):
    """Redundant parentheses. On float and double expressions each pair is an OPPAREN tuple that survives to
    register allocation and scheduling (all three compilers); on integer expressions it leaves nothing. Sites:
    the whole right-hand side of an assignment, a return value, a non-trivial call argument, each top-level
    additive term, and each left prefix `a + b` of `a + b + c` (the grouping the language already implies).
    A right-hand side that is already wrapped is also offered unwrapped."""
    seen = set()

    def offer(i, j, what):
        if j - i < 2 or (i, j) in seen:
            return
        seen.add((i, j))
        line = B.text.count("\n", B.b, B.toks[i][1])
        expr = _txt(B, i, j)
        if _fully_wrapped(B, i, j):
            if what in ("rhs", "return"):
                yield ("paren", "unwrap %s at body line %d: %s" % (what, line, expr[:40]),
                       [(B.toks[i][1], B.toks[j - 1][2], _txt(B, i + 1, j - 1))])
            return
        yield ("paren", "%s at body line %d: (%s)" % (what, line, expr[:40]), [(B.toks[i][1], B.toks[j - 1][2], "(" + expr + ")")])

    def terms(i, j):
        """Top-level additive split points of tokens [i, j)."""
        d, cuts = 0, []
        for k in range(i, j):
            t = B.toks[k][0]
            d += t in ("(", "[")
            d -= t in (")", "]")
            if d == 0 and t in ("+", "-") and k > i and re.match(r"[\w)\]]", B.toks[k - 1][0][-1:]) \
                    and B.toks[k - 1][0] not in KEYWORDS:
                cuts.append(k)
            if d == 0 and t in ("?", ",", "<", ">", "==", "!=", "<=", ">=", "&&", "||", "&", "|", "^", "<<", ">>"):
                return []                     # lower-precedence operators at top level: leave it alone
        return cuts

    def expr_sites(i, j, what):
        yield from offer(i, j, what)
        cuts = terms(i, j)
        a = i
        for n, c in enumerate(cuts + [j]):
            yield from offer(a, c, "term")
            if 0 < n < len(cuts):
                yield from offer(i, c, "prefix")
            a = c + 1

    for k in range(len(B.starts)):
        s = _stmt(B, k) if B.toks[B.starts[k]][0] != "return" else None
        i = B.starts[k]
        if B.toks[i][0] == "return":
            j = i
            while B.toks[j][0] != ";":
                j += 1
            yield from expr_sites(i + 1, j, "return")
            continue
        if not s:
            continue
        i, j = s
        eq = next((q for q in range(i, j) if B.toks[q][0] in ASSIGN and B.paren[q] == B.paren[i]), None)
        if eq is not None:
            yield from expr_sites(eq + 1, j - 1, "rhs")
    for i, (t, _, _) in enumerate(B.toks[:-1]):
        if not re.match(r"[A-Za-z_]", t) or t in NOCALL or t in KEYWORDS or B.toks[i + 1][0] != "(":
            continue
        d, k, a0 = 0, i + 1, i + 2
        while k < len(B.toks):
            x = B.toks[k][0]
            d += x in ("(", "[")
            d -= x in (")", "]")
            if (x == "," and d == 1) or d == 0:
                if k - a0 >= 3 and B.toks[a0][0] != "&":
                    yield from offer(a0, k, "arg")
                a0 = k + 1
                if d == 0:
                    break
            k += 1


GUARD_OPEN = re.compile(r"delete\s*(\[\s*\])?\s*([^;{}]+?)\s*;\s*([^;{}=]+?)\s*=\s*(0|NULL)\s*;")
GUARD_SHUT = re.compile(r"if\s*\(\s*([^(){};]+?)\s*(?:!=\s*(?:0|NULL)\s*)?\)\s*\{\s*delete\s*(\[\s*\])?\s*([^;{}]+?)\s*;"
                        r"\s*([^;{}=]+?)\s*=\s*(0|NULL)\s*;\s*\}")


def gen_guard(B):
    """`delete p; p = 0;` <-> `if (p) { delete p; p = 0; }`. The compiler's own null test around a delete that
    runs a destructor makes the final code the same, but unguarded the reset store sits in the block after the
    join at allocation time and adds its candidates there (Mount & Blade Game::load_game_data, cl 13.10)."""
    body = B.text[B.b:B.e]
    shut = [m.span() for m in GUARD_SHUT.finditer(body)]
    for m in GUARD_SHUT.finditer(body):
        if m.group(1).strip() == m.group(3).strip() == m.group(4).strip():
            line = body.count("\n", 0, m.start())
            arr = "[]" if m.group(2) else ""
            yield ("guard", "unguard delete%s %s at body line %d" % (arr, m.group(1).strip()[:30], line),
                   [(B.b + m.start(), B.b + m.end(), "delete%s %s; %s = %s;" % (arr, m.group(3), m.group(4), m.group(5)))])
    for m in GUARD_OPEN.finditer(body):
        if any(a <= m.start() < z for a, z in shut) or m.group(2).strip() != m.group(3).strip():
            continue
        line = body.count("\n", 0, m.start())
        arr = "[]" if m.group(1) else ""
        x = m.group(2).strip()
        yield ("guard", "guard delete%s %s at body line %d" % (arr, x[:30], line),
               [(B.b + m.start(), B.b + m.end(), "if (%s) { delete%s %s; %s = %s; }" % (x, arr, x, x, m.group(4)))])


def _intlit(B, i, j):
    """Value of the integer literal spelled by tokens [i, j) (`7`, `-1`, `0xff`, with u/l suffixes), or None."""
    toks = [B.toks[k][0] for k in range(i, j)]
    neg = toks[:1] == ["-"]
    if len(toks) != 1 + neg:
        return None
    m = re.fullmatch(r"(0[xX][0-9a-fA-F]+|\d+)[uUlL]*", toks[-1])
    if not m:
        return None
    v = int(m.group(1), 0)
    return -v if neg else v


def gen_const(B):
    """Rewrites that change which constants are register candidates, or where their references sit, without
    changing a value (docs/c2/immcand.md):
      narrow  `X = -1;` on a char or short lvalue stored through the unsigned type (`*(unsigned char*)&(X) = 255;`)
              and the reverse for 128..255 / 32768..65535: the byte stored is the same, but c2 keys a constant
              candidate on its 32-bit value, so the store stops (or starts) sharing a candidate with the other
              uses of -1 / 255 in the function. A variant for the wrong width does not compile (sizeof check).
      hoist   `for (T i = K; c; s)` as `T i = K;` placed before the 1, 2 or 3 preceding statements and `for (; c; s)`:
              the reference to K and the definition of i move to an earlier block.
      sink    `T i = K;` directly followed by `for (; ...)` or `for (i = K; ...)` joined back into the for."""
    for k in range(len(B.starts)):
        st = _stmt(B, k)
        if st:
            i, j = st
            eq = [x for x in range(i, j - 1) if B.toks[x][0] == "=" and B.paren[x] == B.paren[i]]
            if len(eq) == 1 and eq[0] > i and not DECL.match(B.text, B.toks[i][1]):
                v = _intlit(B, eq[0] + 1, j - 1)
                lhs = B.src(i, eq[0])
                line = B.text.count("\n", B.b, B.toks[i][1])
                forms = []
                if v is not None and -128 <= v < 0:
                    forms.append(("unsigned char", v & 0xff))
                if v is not None and -32768 <= v < 0:
                    forms.append(("unsigned short", v & 0xffff))
                if v is not None and 128 <= v <= 255:
                    forms.append(("signed char", v - 256))
                if v is not None and 32768 <= v <= 65535:
                    forms.append(("short", v - 65536))
                for ty, nv in forms:
                    yield ("const", "narrow %s = %d as %s %d at body line %d" % (lhs[:30], v, ty, nv, line),
                           [(B.toks[i][1], B.toks[j - 1][2],
                             "{ typedef char hs_width[sizeof(%s) == sizeof(%s) ? 1 : -1]; *(%s*)&(%s) = (%s)(%d); }"
                             % (lhs, ty, ty, lhs, ty, nv))])
        i = B.starts[k]
        if B.toks[i][0] != "for" or B.toks[i + 1][0] != "(":
            continue
        close = _pclose(B, i + 1)
        semi = next((x for x in range(i + 2, close) if B.toks[x][0] == ";" and B.paren[x] == B.paren[i + 1] + 1), None)
        if semi is None:
            continue
        init = B.src(i + 2, semi) if semi > i + 2 else ""
        line = B.text.count("\n", B.b, B.toks[i][1])
        m = DECL.match(B.text, B.toks[i + 2][1]) if init else None
        if m and m.group(3) == "=" and m.end() <= B.toks[semi][1]:
            # hoist: only past statements of the same block that do not mention the variable
            name, prev = m.group(2), []
            for back in range(1, 4):
                if k - back < 0 or B.block[B.starts[k - back]] != B.block[i]:
                    break
                ps = B.starts[k - back]
                if stmt_end(B, ps) > i or any(B.toks[x][0] == name for x in range(ps, i)):
                    break
                prev.append(ps)
            for n, ps in enumerate(prev, 1):
                pos = B.toks[ps][1]
                yield ("const", "hoist for-init %s over %d statement(s) at body line %d" % (init[:30], n, line),
                       [(pos, pos, init + "; "), (B.toks[i + 2][1], B.toks[semi - 1][2], "")])
            yield ("const", "for-init %s as a statement at body line %d" % (init[:30], line),
                   [(B.toks[i][1], B.toks[i][1], init + "; "), (B.toks[i + 2][1], B.toks[semi - 1][2], "")])
        elif not init and k > 0:
            ps = _stmt(B, k - 1)
            if ps and ps[1] == i and B.block[ps[0]] == B.block[i]:
                d = DECL.match(B.text, B.toks[ps[0]][1])
                if d and d.group(3) == "=":
                    # sink: the declaration moves into the for; valid only if the variable is not used after the
                    # loop (otherwise the variant does not compile and is dropped)
                    txt = B.src(ps[0], ps[1] - 1)
                    yield ("const", "sink %s into the for at body line %d" % (txt[:30], line),
                           [(B.toks[ps[0]][1], B.toks[ps[1] - 1][2], ""), (B.toks[i + 1][2], B.toks[i + 1][2], txt)])


TOUCH = [("void", "(void)%s; "), ("nop", "hs_nop(%s); "), ("nopref", "hs_nopr(%s); "), ("cp", "hs_cp(%s); "),
         ("if", "if (%s) { } "), ("assume", "__assume(%s != 0); "), ("self", "%s = %s; "),
         ("scope", "{ hs_bind(%s); } ")]


def _top_const(ty):
    """Is a variable of declared type `ty` not assignable (`const int`, `T* const`, `const T&`)?"""
    t = ty.replace("&", " ").strip()
    if re.search(r"\bconst$", t):
        return True
    return "*" not in t and bool(re.search(r"\bconst\b", t))


def _is_class(text, ty):
    """Is `ty` (no pointer) a class type: a template instance or a struct/class/union the unit defines?"""
    if "*" in ty:
        return False
    base = re.sub(r"\b(const|volatile|struct|class|union)\b|&", " ", ty).strip()
    if "<" in base:
        return True
    nm = base.split("::")[-1].strip()
    return bool(nm) and bool(re.search(r"\b(?:struct|class|union)\s+(?:__declspec\([^)]*\)\s*)?%s\s*[{:]" % re.escape(nm), text))


def gen_touch(B):
    """A statement that uses V and does nothing, at every statement boundary where V is in scope. Forms that cannot
    compile for V's declared type are left out: `V = V;` for a const V, `if (V)` / `__assume(V != 0)` for a class."""
    for v, (ty, decl) in B.vars.items():
        skip = set()
        if _top_const(ty):
            skip.add("self")
        if v != "this" and _is_class(B.text, ty):
            skip |= {"if", "assume"}
        for st in B.starts:
            if decl >= st or B.toks[st][0] in ("case", "default"):
                continue
            if decl >= 0:
                blk = B.block[decl]
                if blk and not (blk < st < B.close.get(blk, 1 << 30)):
                    continue
            if decl >= 0 and not any(i < st for i in B.uses[v]):
                continue                                   # not assigned yet: reading it would be undefined
            pos = B.toks[st][1]
            line = B.text.count("\n", B.b, pos)
            for nm, fmt in TOUCH:
                if (v == "this" and nm in ("self", "scope", "nopref")) or nm in skip:
                    continue
                yield ("touch", "%s %s at body line %d" % (nm, v, line), [(pos, pos, fmt.replace("%s", v))])


PURE = set("sqrt sqrtf fabs fabsf sin sinf cos cosf tan tanf atan atanf atan2 atan2f exp expf log logf pow powf "
           "floor floorf ceil ceilf".split())


def gen_recompute(B):
    """`V = E;` (or `T V = E;`, T not const) where E is built from literals and pure math calls only
    (`sqrt(13.0 / 8.0)`) and V is not written anywhere else: the assignment is written again in front of a later
    statement that uses V after a call. The value is the same and c2 still computes E once (it shares the
    recomputation in a temporary), but V becomes one live range per call-free stretch instead of one range across
    the calls, which changes when and at what pressure its pieces are costed (docs/c2/x87.md section 10)."""
    for k in range(len(B.starts)):
        s = _stmt(B, k)
        if not s:
            continue
        i, j = s
        eq = next((q for q in range(i, j) if B.toks[q][0] == "=" and B.paren[q] == B.paren[i]), None)
        if eq is None or eq == i or (eq - 1 > i and B.toks[eq - 2][0] in ("->", ".", "]", "*", "&")):
            continue
        x = B.toks[eq - 1][0]
        if not re.match(r"[A-Za-z_]\w*$", x) or x in KEYWORDS or "const" in [B.toks[q][0] for q in range(i, eq)]:
            continue
        rhs = [B.toks[q][0] for q in range(eq + 1, j - 1)]
        idents = [(q, t) for q, t in enumerate(rhs) if re.match(r"[A-Za-z_]", t)]
        if not idents or any(t not in PURE or q + 1 >= len(rhs) or rhs[q + 1] != "(" for q, t in idents):
            continue
        if any(t in ASSIGN or t in ("++", "--") for t in rhs):
            continue
        # every other mention of V in the body must be a plain read (Body.vars misses `double a, b;` lists, so the
        # mentions are collected here), and V must not be declared const
        uses = [q for q, (t, _, _) in enumerate(B.toks) if t == x and q != eq - 1 and (q == 0 or B.toks[q - 1][0] not in (".", "->", "::"))]
        decl = [q for q in uses if q < eq and B.toks[q - 1][0] in (",", "float", "double", "int", "long", "short", "char", "unsigned")
                and B.toks[q + 1][0] in (",", ";")]
        if any("const" in [B.toks[a][0] for a in range(max(st for st in B.starts if st <= q), q)] for q in decl if any(st <= q for st in B.starts)):
            continue
        uses = [q for q in uses if q not in decl]
        if any(B.written(q, q + 1) for q in uses):
            continue
        B.uses.setdefault(x, uses)
        expr = B.src(eq + 1, j - 1)
        end = B.close.get(B.block[i], len(B.toks) - 1)
        sites, call = [], False
        for q in range(j, end):
            t = B.toks[q][0]
            if re.match(r"[A-Za-z_]", t) and t not in KEYWORDS and t not in NOCALL and t not in PURE and B.toks[q + 1][0] == "(":
                call = True
            if t == x and q in uses and call:
                st = max((a for a in B.starts if a <= q), default=None)
                if st is None or st < j or B.toks[st][0] in ("else", "case", "default") or st in sites:
                    continue
                sites.append(st)
                call = False
        for st in sites:
            line = B.text.count("\n", B.b, B.toks[st][1])
            yield ("recompute", "%s = %s again at body line %d" % (x, expr, line),
                   [(B.toks[st][1], B.toks[st][1], "%s = %s; " % (x, expr))])
        if len(sites) > 1:
            yield ("recompute", "%s = %s again at all %d places it is used after a call" % (x, expr, len(sites)),
                   [(B.toks[st][1], B.toks[st][1], "%s = %s; " % (x, expr)) for st in sites])


UNALIAS = re.compile(r"(?:const\s+)?[A-Za-z_][\w:<>]*(?:\s+const)?\s*((?:\*\s*(?:const\s*)?)*[*&])\s*(?:const\s+)?([A-Za-z_]\w*)\s*=\s*(&?)\s*"
                     r"((?:this|[A-Za-z_]\w*)(?:\s*(?:->|\.)\s*[A-Za-z_]\w*|\s*\[[^\]\[;]*\])*)\s*;")


def gen_unalias(B):
    """docs/c2/loadcse.md: a reference or pointer alias of a member chain (`T& r = p->m;`, `T* q = &p->m;`,
    `T*& rp = flags;`) used twice or more is a variable c2 does not copy-propagate, so reads through it never share
    a value with reads of the chain itself (R3), and a stored-through alias of a pointer gives its statements their
    own alias class (R2). Variant: the declaration removed and every use spelled as the chain. Same meaning while
    nothing the chain reads (its root variable) is assigned in the alias's scope; checked on the root only."""
    for k, st in enumerate(B.starts):
        m = UNALIAS.match(B.text, B.toks[st][1])
        if not m or m.end() > B.e:
            continue
        kind, name, amp, chain = m.group(1)[-1], m.group(2), m.group(3), m.group(4)
        if (kind == "&") == bool(amp):
            continue
        root = re.match(r"\w+", chain).group()
        decl = next((j for j in range(st, len(B.toks)) if B.toks[j][1] == m.start(2)), None)
        if decl is None:
            continue
        end = B.close.get(B.block[st], len(B.toks) - 1)
        uses = [i for i in range(decl + 1, end) if B.toks[i][0] == name and B.toks[i - 1][0] not in (".", "->", "::")]
        if not uses or any(B.written(i, i + 1) and kind == "*" for i in uses):
            continue
        if root in B.uses and any(decl < i < end and B.written(i, B.chain_end(i)) and B.chain_end(i) == i + 1
                                  for i in B.uses.get(root, ())):
            continue
        if kind == "*" and any(B.toks[i - 1][0] == "&" for i in uses):
            continue
        dend = next(j for j in range(st, len(B.toks)) if B.toks[j][0] == ";") + 1
        edits = [(B.toks[st][1], B.toks[dend - 1][2], "")]
        for i in uses:
            if kind == "&":
                edits.append((B.toks[i][1], B.toks[i][2], "(" + chain + ")"))
            elif B.toks[i - 1][0] == "*" and (i < 2 or not re.match(r"[\w)\]]", B.toks[i - 2][0][-1:])
                                              or B.toks[i - 2][0] in KEYWORDS):
                edits.append((B.toks[i - 1][1], B.toks[i][2], "(" + chain + ")"))
            else:
                edits.append((B.toks[i][1], B.toks[i][2], "(&" + chain + ")"))
        line = B.text.count("\n", B.b, B.toks[st][1])
        yield ("unalias", "%s %s%s = %s%s removed at body line %d (%d uses)" % (
            "ref" if kind == "&" else "ptr", kind, name, amp, chain, line, len(uses)), edits)


SCALAR = set("bool char short int long unsigned signed float double __int64 size_t intptr_t uintptr_t BOOL BYTE WORD "
             "DWORD UINT INT LONG ULONG USHORT uint8_t uint16_t uint32_t int8_t int16_t int32_t uint int8 uint8 int16 "
             "uint16 int32 uint32".split())


def gen_subst(B):
    """`T v = E;` whose v is read exactly once afterwards, in the same block or a nested non-loop block, with no call,
    store, step or loop between the declaration and the read: the declaration removed and the read spelled
    `((T)(E))`. Same value (nothing E reads can change in between; T a scalar or pointer, so the cast is the
    declaration's conversion), one variable and one OPASSIGN fewer in the front end's IL (effectmine model F: the
    `structure` extras of a draft's single-use locals, Mount & Blade 00532de0, Spore 00d9eb70)."""
    for k in range(len(B.starts)):
        st = _stmt(B, k)
        if not st:
            continue
        i, j = st
        m = DECL.match(B.text, B.toks[i][1])
        if not m or m.group(3) != "=" or m.end() > B.e:
            continue
        ty, v = m.group(1).strip(), m.group(2)
        if "&" in ty or v not in B.vars or B.vars[v][1] < 0 or "static" in ty or "volatile" in ty:
            continue
        if not ("*" in ty or ty.replace("const", "").split()[0] in SCALAR):
            continue
        eq = next(q for q in range(i, j) if B.toks[q][1] >= m.end() - 1)
        expr = [B.toks[q][0] for q in range(eq + 1, j - 1)]
        if not expr or any(t in ASSIGN or t in ("++", "--", ",", "new", "delete", "throw") for t in expr):
            continue
        if any(re.match(r"[A-Za-z_]\w*$", t) and t not in KEYWORDS and t not in NOCALL and q + 1 < len(expr) and
               expr[q + 1] == "(" for q, t in enumerate(expr)):
            continue                                 # a call in E: evaluating it later is not the same
        uses = B.uses.get(v, [])
        if len(uses) != 1 or uses[0] < j or B.written(uses[0], uses[0] + 1):
            continue
        u = uses[0]
        b = B.block[i]
        q = B.block[u]                               # the read: in the declaration's block or nested in it
        while q != b and q > b:
            q = B.block[q]
        if q != b or u > B.close.get(b, len(B.toks)):
            continue
        between = [B.toks[q][0] for q in range(j, u)]
        if any(t in ASSIGN or t in ("++", "--", "for", "while", "do", "goto", "case", "default", "asm", "__asm", "_asm")
               for t in between):
            continue
        if any(re.match(r"[A-Za-z_]\w*$", t) and t not in KEYWORDS and t not in NOCALL and q + 1 < len(between) and
               between[q + 1] == "(" for q, t in enumerate(between)):
            continue
        e = B.src(eq + 1, j - 1)
        line = B.text.count("\n", B.b, B.toks[i][1])
        yield ("subst", "%s %s = %s substituted into its one use at body line %d" % (ty, v, e[:30], line),
               [(B.toks[i][1], B.toks[j - 1][2], ""), (B.toks[u][1], B.toks[u][2], "((%s)(%s))" % (ty, e))])


CMP_EDGE = {"<": "<=", "<=": "<", ">": ">=", ">=": ">"}


def gen_repair(B):
    """Meaning-CHANGING rewrites, for a draft whose statement differs from the original's (icount `structure`; Mount &
    Blade and Spore rows: `= 1` where the original ORs, an early return it lacks, a compare off by one, a value of
    the other signedness). Not in the default set (--families repair); a hit is a hypothesis about the original's
    source, to be checked against what the function must do. Kinds:
      repair.drop    a simple statement, or an `if (..) return ..;` / `if (..) break;` without braces, removed
      repair.rmw     `L = C;` (C a literal) as `L |= C;` and `L &= C;`; `L op= C;` (| & ^) as `L = C;`
      repair.edge    an if / while condition that is one relational compare, `<` <-> `<=`, `>` <-> `>=`
      repair.sign    a local declared `int` / `long` / `short` / `char` as `unsigned ..`, or `unsigned ..` as signed
      repair.step    `X = --N;` / `X = ++N;` as `X = N - 1;` / `X = N + 1;` (N unchanged), and `X = N--;` as
                     `X = N;`"""
    n = len(B.toks)
    for k in range(len(B.starts)):
        i = B.starts[k]
        if B.toks[i][0] == "if" and i + 1 < n and B.toks[i + 1][0] == "(":
            pc = _pclose(B, i + 1)
            if pc + 1 < n and B.toks[pc + 1][0] in ("return", "break", "continue"):
                j = next((q for q in range(pc + 1, n) if B.toks[q][0] in (";", "{", "}")), None)
                if j is not None and B.toks[j][0] == ";" and not (j + 1 < n and B.toks[j + 1][0] == "else"):
                    yield ("repair.drop", "`%s` removed at body line %d" % (B.src(i, j + 1)[:50],
                                                                          B.text.count("\n", B.b, B.toks[i][1])),
                           [(B.toks[i][1], B.toks[j][2], "")])
            continue
        st = _stmt(B, k)
        if not st:
            continue
        i, j = st
        line = B.text.count("\n", B.b, B.toks[i][1])
        txt = B.src(i, j)
        m = DECL.match(B.text, B.toks[i][1])
        if m and m.end() <= B.e and m.group(2) in B.vars and B.vars[m.group(2)][1] >= 0:
            ty = m.group(1)
            u = re.match(r"\s*(const\s+)?unsigned(\s+(int|long|short|char))?\s*$", ty)
            w = re.match(r"\s*(const\s+)?(signed\s+)?(int|long|short|char)\s*$", ty)
            new = ((u.group(1) or "") + (u.group(3) or "int") + " ") if u else \
                ((w.group(1) or "") + "unsigned " + w.group(3) + " ") if w else None
            if new:
                yield ("repair.sign", "`%s %s` as `%s %s` at body line %d" % (ty.strip(), m.group(2), new.strip(),
                                                                              m.group(2), line),
                       [(m.start(1), m.end(1), new)])
            r = [B.toks[q][0] for q in range(i, j - 1) if B.toks[q][1] >= m.end()]
            if len(r) == 2 and r[0] in ("++", "--") and re.match(r"[A-Za-z_]\w*$", r[1]):
                yield ("repair.step", "`%s = %s%s` as `%s = %s %s 1` at body line %d" % (
                    m.group(2), r[0], r[1], m.group(2), r[1], r[0][0], line),
                       [(m.end(), B.toks[j - 2][2], " %s %s 1" % (r[1], r[0][0]))])
            if len(r) == 2 and r[1] in ("++", "--") and re.match(r"[A-Za-z_]\w*$", r[0]):
                yield ("repair.step", "`%s = %s%s` as `%s = %s` at body line %d" % (m.group(2), r[0], r[1], m.group(2),
                                                                                    r[0], line),
                       [(m.end(), B.toks[j - 2][2], " " + r[0])])
            continue                                 # a declaration is never dropped (its name is used later)
        toks = [B.toks[q][0] for q in range(i, j)]
        if not any(t in ("(", "=") or t in ASSIGN or t in ("++", "--") for t in toks):
            continue
        yield ("repair.drop", "`%s` removed at body line %d" % (txt[:50], line), [(B.toks[i][1], B.toks[j - 1][2], "")])
        eq = next((q for q, t in enumerate(toks) if t in ASSIGN and B.paren[i + q] == B.paren[i]), None)
        if eq is None or eq == 0:
            continue
        lhs, rhs = B.src(i, i + eq), B.src(i + eq + 1, j - 1)
        lit = re.fullmatch(r"\(?\s*-?(0x[0-9a-fA-F]+|\d+)[uUlL]*\s*\)?", rhs.strip())
        if lit and toks[eq] == "=":
            for op in ("|=", "&="):
                yield ("repair.rmw", "`%s = %s` as `%s %s %s` at body line %d" % (lhs[:30], rhs, lhs[:30], op, rhs,
                                                                                    line),
                       [(B.toks[i][1], B.toks[j - 1][2], "%s %s %s;" % (lhs, op, rhs))])
        elif lit and toks[eq] in ("|=", "&=", "^="):
            yield ("repair.rmw", "`%s %s %s` as `%s = %s` at body line %d" % (lhs[:30], toks[eq], rhs, lhs[:30], rhs,
                                                                            line),
                   [(B.toks[i][1], B.toks[j - 1][2], "%s = %s;" % (lhs, rhs))])
        r = toks[eq + 1:-1]
        if toks[eq] == "=" and len(r) == 2 and r[0] in ("++", "--") and re.match(r"[A-Za-z_]\w*$", r[1]):
            yield ("repair.step", "`%s = %s%s` as `%s = %s %s 1` at body line %d" % (
                lhs[:30], r[0], r[1], lhs[:30], r[1], r[0][0], line),
                   [(B.toks[i][1], B.toks[j - 1][2], "%s = %s %s 1;" % (lhs, r[1], r[0][0]))])
        if toks[eq] == "=" and len(r) == 2 and r[1] in ("++", "--") and re.match(r"[A-Za-z_]\w*$", r[0]):
            yield ("repair.step", "`%s = %s%s` as `%s = %s` at body line %d" % (lhs[:30], r[0], r[1], lhs[:30], r[0],
                                                                                line),
                   [(B.toks[i][1], B.toks[j - 1][2], "%s = %s;" % (lhs, r[0]))])
    for i in range(1, n - 1):
        if B.toks[i][0] not in ("if", "while") or B.toks[i + 1][0] != "(":
            continue
        pc = _pclose(B, i + 1)
        ops, d = [], 0
        for q in range(i + 2, pc):
            t = B.toks[q][0]
            d += t in ("(", "[") or -(t in (")", "]"))
            if d == 0 and t in ("<", ">", "<=", ">=", "==", "!=", "&&", "||", "?", ",", "=", "<<", ">>"):
                ops.append((q, t))
        if len(ops) != 1 or ops[0][1] not in CMP_EDGE:
            continue
        q, op = ops[0]
        yield ("repair.edge", "%s at body line %d: `%s` as `%s`" % (B.toks[i][0], B.text.count("\n", B.b, B.toks[i][1]),
                                                                    op, CMP_EDGE[op]),
               [(B.toks[q][1], B.toks[q][2], CMP_EDGE[op])])


FAMILIES = {"const": gen_const, "guard": gen_guard, "paren": gen_paren, "cflow": gen_cflow, "touch": gen_touch, "wrap": gen_wrap, "alias": gen_alias, "swap": gen_swap, "split": gen_split, "reread": gen_reread, "recompute": gen_recompute, "cmp": gen_cmp, "unalias": gen_unalias, "byval": gen_byval, "subst": gen_subst, "repair": gen_repair}


def explicit_this(text, fn, cls):
    """Insert `this->` before calls of the class's own members written without an object."""
    if not cls:
        return text, 0
    m = re.search(r"\b(?:class|struct)\s+%s\b[^;{]*\{" % re.escape(cls), text)
    if not m:
        return text, 0
    cbody = text[m.end():helpersweep.match_brace(text, m.end() - 1)]
    members = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", cbody)) - KEYWORDS - {cls}
    toks = lex(text, fn[2], fn[3])
    ins = [s for i, (t, s, _) in enumerate(toks[:-1]) if t in members and toks[i + 1][0] == "("
           and (i == 0 or toks[i - 1][0] not in (".", "->", "::"))]
    for s in reversed(ins):
        text = text[:s] + "this->" + text[s:]
    return text, len(ins)


def body_of(text, func):
    """helpersweep.body_of, plus free functions given as `?name@@Y...` or by plain name."""
    m = re.match(r"\?(\w+?)@@Y", func)
    if m or not func.startswith("?"):
        name = m.group(1) if m else func
        if not m and re.match(r"_[A-Za-z_]\w*(@\d+)?$", func):      # an extern "C" symbol: `_T2_FindSID`, `_f@8`
            names = {fn[0] for fn in helpersweep.functions(text)}
            c = re.sub(r"@\d+$", "", func)[1:]
            name = c if c in names or func not in names else func
        for fn in helpersweep.functions(text):
            if fn[0] == name:
                return fn
        sys.exit("definition of %s not found in the TU" % func)
    return helpersweep.body_of(text, func)


def prelude_for(text):
    """PRELUDE without the helpers the source already defines (an earlier rwsweep or realize variant as the input:
    a second copy is C2995)."""
    return "".join(ln + "\n" for ln in PRELUDE.splitlines()
                   if not any(re.search(r"(?:inline|__forceinline)[^;{]*\b%s\s*\(" % nm, text)
                              for nm in re.findall(r"\b(hs_\w+)\s*\(", ln)))


def apply(text, edits):
    pre = prelude_for(text)
    for s, e, r in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        text = text[:s] + r + text[e:]
    return pre + text


def overlap(e1, e2):
    return any(a[0] < b[1] and b[0] < a[1] or a[0] == b[0] for a in e1 for b in e2)


def _mask(ins):
    k = tsweep._key(ins)
    return (k[0], tuple("esp" if r == "esp" else "r" for r in k[1]) if isinstance(k[1], tuple) else k[1])


def adiff(a, b):
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    matched = sum(m.size for m in sm.get_matching_blocks())
    return len(a) + len(b) - 2 * matched


class Run:
    def __init__(self, src, func, cl, work, keep=False):
        _, _, _, out, log = c2trace.run_trace(src, c2regs.HOOKS, cl, keep=os.path.join(work, "r"), quiet=True)
        fns = c2regs.parse(log, func)
        if not fns:
            raise RuntimeError(("compile error: " + " ".join(re.findall(r"error C\d+:[^\n]*", out))[:160])
                               if "error" in out else "function not register-allocated")
        self.snap = c2regs.export(fns[0])
        asm = os.path.join(work, "r", os.path.splitext(os.path.basename(src))[0] + ".asm")
        self.instrs = tsweep.listing_instrs(asm, func)
        self.shape = [_mask(x) for x in self.instrs]
        self.keys = [tsweep._key(x) for x in self.instrs]
        if not keep:
            shutil.rmtree(os.path.join(work, "r"), ignore_errors=True)

    def gap(self, a, b, k):
        pa, pb = helpersweep.prio_at(self.snap, a, k), helpersweep.prio_at(self.snap, b, k)
        if pa is None or pb is None:
            return None
        g = pb - pa
        if g == 0 and helpersweep.defpos(self.snap, a) <= helpersweep.defpos(self.snap, b):
            g = 1
        return g


def plan_combos(rows, variants, b0, g0, max_combo):
    """(code-neutral singles that help, code-changing singles that lower the bdiff, combinations to compile as
    (predicted gap, size, ids), best first). rows: rwsweep's measured singles (i, shape, bd, dgap); b0: the
    baseline's bdiff (None without --asm-vs); g0: the baseline's gap (None without --want).
    With --asm-vs, singles that lower the bdiff while changing the code are combined too (each other and with the
    code-neutral ones): two code changes that each move toward the original often add up (Spore 0099f110: four
    such singles and no combination row before)."""
    good = [r for r in rows if r["shape"] == 0 and ((r["dgap"] or 0) < 0 or (b0 is not None and r["bd"] < b0))]
    good.sort(key=lambda r: ((r["dgap"] or 0), r["bd"] or 0))
    better = []
    if b0 is not None:
        better = sorted((r for r in rows if r["shape"] != 0 and r["bd"] < b0), key=lambda r: (r["bd"], r["shape"], r["i"]))
    pool = good[:12] + better[:8]
    combos = []
    for n in range(2, max_combo + 1):
        for c in itertools.combinations(pool, n):
            if any(overlap(variants[x["i"]][2], variants[y["i"]][2]) for x, y in itertools.combinations(c, 2)):
                continue
            pred = None if g0 is None else g0 + sum(x["dgap"] or 0 for x in c)
            gain = 0 if b0 is None else sum(x["bd"] - b0 for x in c)        # bdiff gains added up: a ranking only
            combos.append((pred if pred is not None else 0, gain, n, [x["i"] for x in c]))
    combos.sort()
    return good, better, [(pred, n, ids) for pred, _g, n, ids in combos]


def main():
    argv = sys.argv[1:]
    cl = []
    if "--" in argv:
        k = argv.index("--")
        argv, cl = argv[:k], argv[k + 1:]
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument("tu")
    ap.add_argument("--func", required=True, help="decorated name")
    ap.add_argument("--asm-vs")
    ap.add_argument("--want", help="A>B: A must be coloured before B")
    ap.add_argument("--pass", dest="k", type=int, default=2)
    ap.add_argument("--families", default="wrap,alias,swap,split,reread,touch,cflow,cmp,paren,guard,const,unalias,byval,subst")
    ap.add_argument("--vars", help="comma-separated: only rewrites that mention one of these variables")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--max-combo", type=int, default=2)
    ap.add_argument("--verify", type=int, default=8)
    ap.add_argument("--limit", type=int, help="measure only the first N variants")
    ap.add_argument("--list", action="store_true", help="print the variants and stop")
    ap.add_argument("--all", action="store_true", help="also print variants that changed nothing")
    a = ap.parse_args(argv)
    A, Bn = a.want.split(">") if a.want else (None, None)
    text = open(a.tu).read()
    hdr = os.path.dirname(os.path.abspath(a.tu))
    text = re.sub(r'#include "([^"/][^"]*)"', lambda m: '#include "%s"' % os.path.join(hdr, m.group(1))
                  if os.path.exists(os.path.join(hdr, m.group(1))) else m.group(0), text)
    a.func = c2trace.func_decl(a.func)         # Class::method or method@Class -> ?method@Class@ (any namespace)
    # a full decorated `?method@Class@@..` given by hand: one @ fewer also selects a class inside a namespace
    tfunc = a.func[:-1] if re.fullmatch(r"\?\w+@\w+@@", a.func) else a.func
    m = re.match(r"\?(\w+)@(\w+)@", a.func)   # a namespaced class too
    cls = m.group(2) if m else None
    raw = text
    text, nthis = explicit_this(text, body_of(text, a.func), cls)
    fn = body_of(text, a.func)
    B = Body(text, fn, cls)
    variants = []
    for fam in a.families.split(","):
        if fam == "this":                      # applied to the baseline itself (explicit_this above), no variants
            continue
        if fam not in FAMILIES:
            sys.exit("unknown family %r (families: this, %s)" % (fam, ", ".join(FAMILIES)))
        variants += list(FAMILIES[fam](B))
    if a.vars:
        pat = re.compile(r"\b(%s)\b" % "|".join(map(re.escape, a.vars.split(","))))
        variants = [v for v in variants if pat.search(v[1])]
    if a.limit:
        variants = variants[:a.limit]
    count = {}
    for v in variants:
        count[v[0]] = count.get(v[0], 0) + 1
    print("%d variables, %d statements, %d variants (%s)" % (len(B.vars), len(B.starts), len(variants),
          ", ".join("%s %d" % kv for kv in count.items())), flush=True)
    if a.list:
        for i, v in enumerate(variants):
            print("  v%04d %-6s %s" % (i, v[0], v[1]))
        return
    # the source's directory enters the name (as in tsweep): two agents' v3.cpp must not share, and rmtree, one directory
    out = os.path.join(ROOT, "work", "rwsweep", tsweep.out_name(a.tu, a.func))
    if c2trace.c2build.table():       # another c2 build (--c2 / C2_BUILD): its own variant directory
        out += "@" + c2trace.c2build.name()
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    orig = tsweep.orig_instrs(a.asm_vs) if a.asm_vs else None

    def build(tag, src_text, keep=False):
        d = os.path.join(out, tag)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, tag + ".cpp")
        open(p, "w").write(src_text)
        return Run(p, tfunc, cl, d, keep)

    def rd(r):          # tsweep's own scores, so every tool reports the same baseline
        return None if orig is None else tsweep.regdiff(r.instrs, orig)

    def bd(r):
        return None if orig is None else tsweep.bdiff(r.instrs, orig)

    base0 = build("orig", prelude_for(raw) + raw)
    if prelude_for(raw):
        # the helper templates are declarations before the function: on 13.10 their count alone has been seen to flip
        # a scheduling tie (Mount & Blade 0x4b9110, 2 vs 5 declarations); every variant carries them, so say so
        raw0 = build("raw", raw)
        if raw0.keys != base0.keys:
            print("WARNING: the helper prelude alone changes the code (%s); variants are measured against the "
                  "baseline with it" % ("bdiff %d -> %d" % (bd(raw0), bd(base0)) if orig is not None else "regs"),
                  flush=True)
    base = build("base", apply(text, []), keep=True)
    g0 = base.gap(A, Bn, a.k) if A else None
    print("baseline: %d instructions%s%s%s" % (
        len(base.instrs), "" if orig is None else ", bdiff %d, regdiff %d" % (bd(base), rd(base)),
        "" if g0 is None else ", gap %+d (%s %s, %s %s)" % (g0, A, helpersweep.prio_at(base.snap, A, a.k), Bn,
                                                           helpersweep.prio_at(base.snap, Bn, a.k)),
        "" if not nthis else "; %d member calls given `this->` (%s)" % (
            nthis, "no change" if base.keys == base0.keys else "CODE CHANGED by that alone")), flush=True)

    def measure(i):
        fam, desc, edits = variants[i]
        for attempt in (0, 1):
            try:
                return i, build("v%04d" % i, apply(text, edits)), None
            except (Exception, SystemExit) as e:     # SystemExit: run_trace found no log (front-end error)
                err = str(e)[-200:]
                if "compile error" in err or "compile failed" in err:
                    break
        return i, None, err

    with ThreadPoolExecutor(a.jobs) as ex:
        res = list(ex.map(measure, range(len(variants))))
    rows, failed = [], 0
    for i, r, err in res:
        if r is None:
            failed += 1
            if a.all:
                print("  v%04d failed: %s | %s" % (i, variants[i][1], err))
            continue
        rows.append(dict(i=i, shape=adiff(r.shape, base.shape), regs=adiff(r.keys, base.keys), rd=rd(r), bd=bd(r),
                         dgap=None if g0 is None or r.gap(A, Bn, a.k) is None else r.gap(A, Bn, a.k) - g0, n=len(r.instrs)))
    same = [r for r in rows if r["regs"] == 0 and not r["dgap"]]
    print("%d measured, %d failed to compile, %d changed nothing (no register, no priority)" % (len(rows), failed, len(same)))
    r0, b0 = rd(base), bd(base)
    shown = [r for r in rows if a.all or r not in same]
    shown.sort(key=lambda r: (r["bd"] if r["bd"] is not None else 0, r["shape"], r["dgap"] or 0, r["i"]))
    print("   id   family shape regs %s%s  rewrite" % ("bdiff regdiff " if orig is not None else "", "dgap " if A else ""))
    for r in shown:
        fam, desc, _ = variants[r["i"]]
        print("  v%04d %-6s %5d %4d %s%s  %s" % (
            r["i"], fam, r["shape"], r["regs"], "" if orig is None else "%5d%s %6d  " % (
                r["bd"], "*" if r["bd"] < b0 else " ", r["rd"]),
            "" if not A else ("%+4d " % r["dgap"] if r["dgap"] is not None else "   ? "), desc))
    # combinations of the variants that helped
    good, better, combos = plan_combos(rows, variants, b0 if orig is not None else None, g0, a.max_combo)
    print("%d code-neutral variants improve the gap or the bdiff" % len(good))
    if orig is not None:
        print("%d variants that change the code improve the bdiff (combined too)" % len(better))
    # a variant that reaches the original is the answer whatever its shape against the draft (Spore 0073afb0: a wrap
    # with shape 2 and bdiff 0, which the line above does not count)
    if orig is not None:
        for r in rows:
            if r["bd"] == 0:
                print("  v%04d %s: bdiff 0 against the original (MATCH candidate: check the bytes)" % (
                    r["i"], variants[r["i"]][1]))
    for pred, n, ids in combos[:a.verify]:
        tag = "c_" + "_".join("%04d" % i for i in ids)
        try:
            r = build(tag, apply(text, [e for i in ids for e in variants[i][2]]), keep=True)
        except Exception as e:
            print("  %s failed: %s" % (tag, str(e)[-120:]))
            continue
        print("  %-16s shape %d regs %d%s%s" % (
            "+".join("v%04d" % i for i in ids), adiff(r.shape, base.shape), adiff(r.keys, base.keys),
            "" if orig is None else "  bdiff %d regdiff %d" % (bd(r), rd(r)),
            "" if g0 is None else "  gap predicted %+d real %s" % (pred, r.gap(A, Bn, a.k))))
    print("variants: %s/v<id>/v<id>.cpp" % out)


if __name__ == "__main__":
    main()
