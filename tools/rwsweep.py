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
  recompute  (not in the default set; --families recompute) `V = E;` with E made of literals and pure math calls
          (`dq = sqrt(13.0 / 8.0);`) is written again before a later statement that uses V after a call: the same
          value, computed once as before, but V is then one live range per call-free stretch instead of one
          across the calls (float values: docs/c2/x87.md section 10)

Measures per variant:
  shape    aligned diff against the baseline with every register except esp masked: 0 = the same instructions
           in the same order, only registers may differ ("code-neutral")
  regs     aligned diff against the baseline including registers: 0 = nothing changed at all
  regdiff  (--asm-vs) aligned diff against the original, registers included: the number to bring to 0
  dgap     (--want A>B) change of prio(B) - prio(A); the constraint holds when the gap is <= 0

Output: every variant that changed something, best first; then combinations of the code-neutral ones that
helped (gaps add up, as in helpersweep), compiled and reported. Variants stay in work/rwsweep/<tu>/.
"""
import argparse, difflib, itertools, os, re, shutil, sys
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
        name, params, b, e, _ = fn
        self.b, self.e = b, e
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
            if self.toks[i - 1][0] in (";", "{", "}") and self.paren[i] == 0 and self.toks[i][0] not in ("else", "}"):
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


def gen_wrap(B):
    seen = set()
    for v, uses in B.uses.items():
        for i in uses:
            for j in sorted({i + 1, B.chain_end(i)}):
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                expr, w = B.src(i, j), B.written(i, j)
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
        for a, z in args:
            if z - a < 2 or (z - a == 2 and B.toks[a][0] == "&") or B.toks[a][0] in ("hs_id", "hs_cp"):
                continue
            if B.toks[a][0] == "&" or (a, z) in seen:
                continue
            expr = B.src(a, z)
            for h in ("hs_id", "hs_cp"):
                yield ("wrap", "arg %s(%s) of %s" % (h, expr[:40], t), [(B.toks[a][1], B.toks[z - 1][2], "%s(%s)" % (h, expr))])


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
        if m and m.group(3) == "=" and m.group(2) in B.vars and "&" not in m.group(1) and "const" not in m.group(1):
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


def gen_touch(B):
    """A statement that uses V and does nothing, at every statement boundary where V is in scope."""
    for v, (ty, decl) in B.vars.items():
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
                if v == "this" and nm in ("self", "scope", "nopref"):
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


FAMILIES = {"const": gen_const, "guard": gen_guard, "paren": gen_paren, "cflow": gen_cflow, "touch": gen_touch, "wrap": gen_wrap, "alias": gen_alias, "swap": gen_swap, "split": gen_split, "reread": gen_reread, "recompute": gen_recompute}


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
        for fn in helpersweep.functions(text):
            if fn[0] == name:
                return fn
        sys.exit("definition of %s not found in the TU" % func)
    return helpersweep.body_of(text, func)


def apply(text, edits):
    for s, e, r in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        text = text[:s] + r + text[e:]
    return PRELUDE + text


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
    ap.add_argument("--families", default="wrap,alias,swap,split,reread,touch,cflow,paren,guard,const")
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
    a.func = c2trace.func_decl(a.func)         # Class::method or method@Class -> ?method@Class@@
    m = re.match(r"\?(\w+)@(\w+)@@", a.func)
    cls = m.group(2) if m else None
    raw = text
    text, nthis = explicit_this(text, body_of(text, a.func), cls)
    fn = body_of(text, a.func)
    B = Body(text, fn, cls)
    variants = []
    for fam in a.families.split(","):
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
    out = os.path.join(ROOT, "work", "rwsweep", os.path.splitext(os.path.basename(a.tu))[0])
    if c2trace.c2build.table():       # another c2 build (--c2 / C2_BUILD): its own variant directory
        out += "@" + c2trace.c2build.name()
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    orig = [tsweep._key(x) for x in tsweep.orig_instrs(a.asm_vs)] if a.asm_vs else None

    def build(tag, src_text, keep=False):
        d = os.path.join(out, tag)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, tag + ".cpp")
        open(p, "w").write(src_text)
        return Run(p, a.func, cl, d, keep)

    def rd(r):
        if orig is None:
            return None
        sm = difflib.SequenceMatcher(None, r.keys, orig, autojunk=False)
        blocks = [x for x in sm.get_matching_blocks() if x.size]
        if not blocks:
            return len(r.keys) + len(orig)
        matched = sum(x.size for x in blocks)
        return (blocks[-1].a + blocks[-1].size - matched) + (len(orig) - matched)

    base0 = build("orig", PRELUDE + raw)
    base = build("base", apply(text, []), keep=True)
    g0 = base.gap(A, Bn, a.k) if A else None
    print("baseline: %d instructions%s%s%s" % (
        len(base.instrs), "" if orig is None else ", regdiff %d" % rd(base),
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
        rows.append(dict(i=i, shape=adiff(r.shape, base.shape), regs=adiff(r.keys, base.keys), rd=rd(r),
                         dgap=None if g0 is None or r.gap(A, Bn, a.k) is None else r.gap(A, Bn, a.k) - g0, n=len(r.instrs)))
    same = [r for r in rows if r["regs"] == 0 and not r["dgap"]]
    print("%d measured, %d failed to compile, %d changed nothing (no register, no priority)" % (len(rows), failed, len(same)))
    r0 = rd(base)
    shown = [r for r in rows if a.all or r not in same]
    shown.sort(key=lambda r: (r["rd"] if r["rd"] is not None else 0, r["shape"], r["dgap"] or 0, r["i"]))
    print("   id   family shape regs %s%s  rewrite" % ("regdiff " if orig is not None else "", "dgap " if A else ""))
    for r in shown:
        fam, desc, _ = variants[r["i"]]
        print("  v%04d %-6s %5d %4d %s%s  %s" % (
            r["i"], fam, r["shape"], r["regs"], "" if orig is None else "%4d%s  " % (r["rd"], "*" if r["rd"] < r0 else " "),
            "" if not A else ("%+4d " % r["dgap"] if r["dgap"] is not None else "   ? "), desc))
    # combinations of code-neutral variants that helped
    good = [r for r in rows if r["shape"] == 0 and ((r["dgap"] or 0) < 0 or (orig is not None and r["rd"] < r0))]
    good.sort(key=lambda r: ((r["dgap"] or 0), r["rd"] or 0))
    print("%d code-neutral variants improve the gap or the regdiff" % len(good))
    combos = []
    for n in range(2, a.max_combo + 1):
        for c in itertools.combinations(good[:12], n):
            if any(overlap(variants[x["i"]][2], variants[y["i"]][2]) for x, y in itertools.combinations(c, 2)):
                continue
            pred = None if g0 is None else g0 + sum(x["dgap"] or 0 for x in c)
            combos.append((pred if pred is not None else 0, n, [x["i"] for x in c]))
    combos.sort()
    for pred, n, ids in combos[:a.verify]:
        tag = "c_" + "_".join("%04d" % i for i in ids)
        try:
            r = build(tag, apply(text, [e for i in ids for e in variants[i][2]]), keep=True)
        except Exception as e:
            print("  %s failed: %s" % (tag, str(e)[-120:]))
            continue
        print("  %-16s shape %d regs %d%s%s" % (
            "+".join("v%04d" % i for i in ids), adiff(r.shape, base.shape), adiff(r.keys, base.keys),
            "" if orig is None else "  regdiff %d" % rd(r),
            "" if g0 is None else "  gap predicted %+d real %s" % (pred, r.gap(A, Bn, a.k))))
    print("variants: %s/v<id>/v<id>.cpp" % out)


if __name__ == "__main__":
    main()
