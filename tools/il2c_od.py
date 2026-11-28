"""il2c_od: the /Od shape of lifted C++.

At /O2 the lifted IL's register temporaries (`t1004 = v_l92; t1005 = t1004 + 20; f(t1005)`) become registers.  At /Od
every declared local owns a frame slot and is reloaded for each statement, so each `tN` declared costs a store and a
reload the original does not have.  The original was compiled from source expressions: only its frame slots (the
`v_l*` / `a*` the lifter derives) are named; a register value lives inside one expression.  `od_fold` folds each
temporary back into the expression that reads it, when nothing between its definition and its use can change what it
read (conservatively: a store to a slot it names, any store / call when it reads memory, control flow).
"""
import re

TMP = re.compile(r"\bt\d+\b")
DEFL = re.compile(r"^(\s+)(t\d+) = (.*);$")
DECL = re.compile(r"^\s+(?!return\b|goto\b)[\w ]+\*? t\d+(?:\[\d+\])?;$")
SLOT = re.compile(r"\b(?:v_l\d+|a\d+|self)\b")
CTRL = re.compile(r"^\s*(?:\}|.*\{\s*$|L\w+:;|goto\b|if\b|else\b|do\b|while\b|switch\b|case\b|default\b|break\b|continue\b|"
                  r"return\b|__EH|__SWITCH|/\*)")
STORE_SLOT = re.compile(r"^\s+(v_l\d+|a\d+|self)(?:\[\d+\])? = ")
CALLISH = re.compile(r"[A-Za-z_]\w*\s*\(|->|\)\s*\(")
CASTS = ("(int)", "(char*)", "(unsigned short)", "(unsigned char)", "(unsigned int)", "(float)", "(double)", "(short)",
         "(int*)", "(float*)", "(double*)", "(unsigned short*)", "(unsigned char*)", "(short*)", "(char)", "(bool)",
         "(void*)", "(signed char)", "(__int64)", "(unsigned __int64)")


def has_call(e):
    r = e
    for c in CASTS:
        r = r.replace(c, "")
    return bool(CALLISH.search(r)) or any(k in e for k in ("__cdecl", "__stdcall", "__thiscall", "->*"))


def reads_mem(e):
    """A load through a pointer or from a global (`*(int*)...`), or any address-taken slot."""
    return "*(" in e or "&" in e


class _Leaf:
    PAT = re.compile(r"^[()]*&?(?:self|this|a\d+|v_l\d+|-?\d+)[()]*$")

    @staticmethod
    def match(e):
        return _Leaf.PAT.match(re.sub(r"\((?:int|unsigned int|char\*)\)", "", e))


LEAF = _Leaf


def fold_leaf(lines, alive, i, t, e, addr_taken):
    """A temporary that is a plain reload of a slot / `this` / a constant and is read more than once: the original
    reloaded it for each reader (a register value lives inside one statement), so every reader gets the expression,
    when the slot is not written and no control flow lies between the definition and the last reader."""
    isaddr = "&" in e                          # `&v_l56`: a frame address, no slot is read
    slots = set() if isaddr else set(SLOT.findall(e))
    if any(s_ in addr_taken for s_ in slots):
        return False
    pat = re.compile(r"\b%s\b" % t)
    last = max(k for k in range(i + 1, len(lines)) if alive[k] and pat.search(lines[k]) and not DECL.match(lines[k]))
    d = 0
    for k in range(i + 1, last + 1):
        if not alive[k] or DECL.match(lines[k]):
            continue
        L = lines[k]
        st = L.strip()
        if LABEL.match(L) or GOTO.match(L) or re.match(r"^(do|switch|for|while)\b", st) or st.startswith("} while"):
            return False                      # a join or a back edge: the slot may change in between
        if d - (1 if st.startswith("}") else 0) < 0:
            return False                      # left the block the definition is in
        d += L.count("{") - L.count("}")
        sm = STORE_SLOT.match(L)
        if sm and sm.group(1) in slots:
            if k < last or pat.search(L.split("=", 1)[0]):
                return False
        dm = DEFL.match(L)
        if dm and dm.group(2) == t:
            return False
        if re.search(r"&\s*\(?\s*%s\b" % t, L):
            return False                      # its address is taken (`*(T*)&t = ..`): a variable, not a value
    for k in range(i + 1, last + 1):
        if alive[k] and pat.search(lines[k]) and not DECL.match(lines[k]):
            lines[k] = pat.sub(lambda _m: "(" + e + ")", lines[k])
    alive[i] = False
    return True


def _parse(lines, i):
    """Statement tree of one-statement-per-line text: nodes ('s', k) | ('if', k, then, else) | ('loop', k, body) |
    ('blk', k, body); returns (nodes, index of the line that closed the block)."""
    nodes, n = [], len(lines)
    while i < n:
        l = lines[i]
        st = l.strip()
        if st.startswith("}"):
            return nodes, i
        if st.endswith("{"):
            body, j = _parse(lines, i + 1)
            if j >= n:
                return None, j
            if lines[j].strip().startswith("} else"):
                eb, k = _parse(lines, j + 1)
                nodes.append(("if", i, body, eb))
                i = k + 1
            else:
                kind = "if" if st.startswith("if ") else "loop" if st.startswith(("for ", "while ", "do ")) else "blk"
                nodes.append((kind, i, body, None) if kind == "if" else (kind, i, body))
                i = j + 1
                if lines[j].strip().startswith("} while"):
                    nodes[-1] = nodes[-1] + (j,)
            continue
        nodes.append(("s", i))
        i += 1
    return nodes, i


def _uses(lines, nodes):
    out = set()
    for nd in nodes:
        if nd[0] == "s":
            out |= set(TMP.findall(lines[nd[1]]))
        else:
            out |= set(TMP.findall(lines[nd[1]]))
            for sub in nd[2:]:
                if isinstance(sub, list):
                    out |= _uses(lines, sub)
    return out


def drop_dead_defs(lines):
    """Liveness over the structured text: a temporary assigned and never read afterwards on any path (a call's
    result nobody uses, `t = f(); .. t = g();`) loses the assignment; a call keeps the call.  Bails on any goto /
    label / switch (flow the tree does not show)."""
    if any(LABEL.match(l) or GOTO.match(l) or l.strip().startswith(("switch", "case ", "default")) for l in lines):
        return lines
    nodes, end = _parse(lines, 0)
    if nodes is None:
        return lines
    dead = {}

    def back(nodes, live):
        live = set(live)
        for nd in reversed(nodes):
            k = nd[1]
            if nd[0] == "s":
                l = lines[k]
                m = DEFL.match(l)
                if m:
                    t, rhs = m.group(2), m.group(3)
                    if t not in live:
                        dead[k] = "strip" if has_call(rhs) else "drop"
                        if dead[k] == "drop":
                            continue
                    live.discard(t)
                    live |= set(TMP.findall(rhs))
                elif l.strip().startswith("return"):
                    live = set(TMP.findall(l))
                else:
                    live |= set(TMP.findall(l))
            elif nd[0] == "if":
                a = back(nd[2], live)
                b = back(nd[3], live) if nd[3] is not None else live
                live = set(TMP.findall(lines[k])) | a | b
            else:
                allu = _uses(lines, [nd]) | live
                back(nd[2], allu)
                live = allu
        return live
    back(nodes, set())
    out = []
    for k, l in enumerate(lines):
        if dead.get(k) == "drop":
            continue
        if dead.get(k) == "strip":
            m = DEFL.match(l)
            rhs = re.sub(r"^\([\w ]+\*?\)", "", m.group(3))
            l = "%s%s;" % (m.group(1), rhs)
        out.append(l)
    return out


WEBS = {}


def split_webs(lines):
    """A temporary assigned several times (`t1007 = f(); .. t1007 = g(); if (t1007)`): each assignment whose reads all
    follow in straight line before the next assignment at its own nesting depth is a value of its own; rename it so
    the folds see one definition. A value nobody reads is dropped (a call keeps its call)."""
    n = len(lines)
    depth, d = [0] * n, 0
    for k, l in enumerate(lines):
        st = l.lstrip()
        depth[k] = d - (1 if st.startswith("}") else 0)
        d += l.count("{") - l.count("}")
    occ = {}
    for k, l in enumerate(lines):
        if DECL.match(l):
            continue
        for t in set(TMP.findall(l)):
            occ.setdefault(t, []).append(k)
    fresh = 0
    for t, ks in occ.items():
        defl = [k for k in ks if (DEFL.match(lines[k]) and DEFL.match(lines[k]).group(2) == t)]
        if len(defl) < 2:
            continue
        for i in defl:
            m = DEFL.match(lines[i])
            if lines[i] is None or m.group(2) != t:
                continue
            after = [k for k in ks if k > i]
            # the next assignment at i's depth, or the end
            nxt = next((k for k in after if k in defl and depth[k] == depth[i] and not re.search(r"\b%s\b" % t, DEFL.match(lines[k]).group(3))), n)
            region = [k for k in after if k < nxt]
            lo = max(region) if region else i
            bad = False
            for k in range(i + 1, lo + 1):
                if depth[k] < depth[i] or LABEL.match(lines[k] or ""):
                    bad = True
                    break
            if bad or any(k in defl for k in region):
                continue
            if nxt == n and region and False:
                continue
            if not region:
                if nxt == n:
                    continue                      # last value: may be read after the function's loops, leave it
                e = m.group(3)
                if not has_call(e):
                    lines[i] = None
                else:
                    lines[i] = "%s%s;" % (m.group(1), re.sub(r"^\([\w ]+\*?\)", "", e)) if False else lines[i]
                continue
            fresh += 1
            nm = "t%d" % (9000 + fresh)
            WEBS[nm] = t
            for k in [i] + region:
                lines[k] = re.sub(r"\b%s\b" % t, nm, lines[k])
    return [l for l in lines if l is not None]


def rename_webs(lines):
    """Reaching definitions over the lines (if / else / do-while / goto / return), temporaries only: every definition
    with the reads it reaches is a web (two definitions that reach one read are one web).  A temporary with several
    webs gets one name per web (`t1002 = a4; ..; t1002 = v_l8` was one eax); a definition nobody reads is dropped (a
    call keeps its call).  Returns the new lines.  Bails (unchanged) on a switch."""
    n = len(lines)
    if any(l.strip().startswith(("switch", "case ", "default")) for l in lines):
        return lines
    lab = {}
    for k, l in enumerate(lines):
        m = LABEL.match(l)
        if m:
            lab[m.group(1)] = k
    stack, close_of, else_of, open_of = [], {}, {}, {}
    for k, l in enumerate(lines):
        st = l.strip()
        if st.startswith("}"):
            if st.startswith("} else"):
                else_of[stack[-1]] = k
                open_of[k] = stack[-1]
            else:
                o = stack.pop()
                close_of[o] = k
                open_of[k] = o
        if st.endswith("{") and not st.startswith("} else"):
            stack.append(k)
    if stack:
        return lines
    succ = [[k + 1] if k + 1 < n else [] for k in range(n)]
    for k, l in enumerate(lines):
        st = l.strip()
        g = GOTO.match(l)
        if g:
            succ[k] = [lab[g.group(1)]] if g.group(1) in lab else []
        elif st.startswith("return"):
            succ[k] = []
        elif st.startswith("if ") and st.endswith("{"):
            succ[k] = [k + 1, (else_of[k] + 1) if k in else_of else close_of[k]]
        elif st.startswith("} else"):
            succ[k] = [close_of[open_of[k]]]
        elif st.startswith("} while"):
            succ[k] = [k + 1, open_of[k] + 1]
    pred = [[] for _ in range(n)]
    for k in range(n):
        for q in succ[k]:
            if q < n:
                pred[q].append(k)
    defline = {}
    for k, l in enumerate(lines):
        m = DEFL.match(l)
        if m and not DECL.match(l):
            defline[k] = m.group(2)
    # reaching definitions as bitsets over the definition lines (bit b = the line dlist[b]); a definition of t kills
    # every other definition of t
    dlist = sorted(defline)
    bit = {k: 1 << b for b, k in enumerate(dlist)}
    tmask = {}
    for k in dlist:
        tmask[defline[k]] = tmask.get(defline[k], 0) | bit[k]
    IN = [0] * n
    OUT = [0] * n
    import collections as _c
    work = _c.deque(range(n))
    inq = set(work)
    while work:
        k = work.popleft()
        inq.discard(k)
        st = 0
        for q in pred[k]:
            st |= OUT[q]
        IN[k] = st
        out = (st & ~tmask[defline[k]]) | bit[k] if k in defline else st
        if out != OUT[k]:
            OUT[k] = out
            for q in succ[k]:
                if q < n and q not in inq:
                    work.append(q)
                    inq.add(q)
    parent = {k: k for k in defline}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    used = set()
    usesite = {}
    for k, l in enumerate(lines):
        if DECL.match(l):
            continue
        m = DEFL.match(l)
        for t in set(TMP.findall(m.group(3) if m else l)):
            v = IN[k] & tmask.get(t, 0)
            if not v:
                continue
            ds = []
            while v:
                low = v & -v
                ds.append(dlist[low.bit_length() - 1])
                v ^= low
            for d in ds:
                used.add(d)
                parent[find(d)] = find(ds[0])
            usesite[(k, t)] = ds[0]
    use_by_t = {}
    for (k, tt), d0 in usesite.items():
        use_by_t.setdefault(tt, []).append((k, d0))
    webs = {}
    for d, t in defline.items():
        webs.setdefault(t, {}).setdefault(find(d), []).append(d)
    out = list(lines)
    fresh = 0
    for t, ws in webs.items():
        # a web whose definitions nobody reads
        for root, ds in list(ws.items()):
            if not any(d in used for d in ds):
                for d in ds:
                    m = DEFL.match(lines[d])
                    out[d] = None if not has_call(m.group(3)) else "%s%s;" % (m.group(1), re.sub(r"^\([\w ]+\*?\)", "", m.group(3)))
                del ws[root]
        if len(ws) < 2:
            continue
        for wi, (root, ds) in enumerate(ws.items()):
            if wi == 0:
                continue
            fresh += 1
            nm = "t%d" % (9000 + fresh)
            WEBS[nm] = t
            for d in ds:
                if out[d] is not None:
                    out[d] = re.sub(r"^(\s+)%s\b" % t, r"\1" + nm, out[d])
            for k, d0 in use_by_t.get(t, ()):
                if find(d0) == root and out[k] is not None:
                    m = DEFL.match(out[k])
                    if m and m.group(2) in (t, nm):
                        out[k] = "%s%s = %s;" % (m.group(1), m.group(2), re.sub(r"\b%s\b" % t, nm, m.group(3)))
                    else:
                        out[k] = re.sub(r"\b%s\b" % t, nm, out[k])
    return [l for l in out if l is not None]


def ternaries(lines, deep):
    """`if (c) { t = a; } else { t = b; }` for a compiler temporary below `this` (cl's own slot for a `?:` result):
    `t = c ? a : b;`."""
    changed = True
    while changed:
        changed = False
        for i in range(len(lines) - 4):
            m = IFL.match(lines[i])
            if not m or lines[i + 2].strip() != "} else {" or lines[i + 4].strip() != "}":
                continue
            a, b = DEFL.match(lines[i + 1]), DEFL.match(lines[i + 3])
            if not a or not b or a.group(2) != b.group(2) or a.group(2) not in deep:
                continue
            lines[i:i + 5] = ["%s%s = (%s) ? (%s) : (%s);" % (m.group(1), a.group(2), m.group(2), a.group(3), b.group(3))]
            changed = True
            break
    return lines


def chain_consts(lines):
    """`t = 0; a = t; b = t; c = t;` (eax loaded once, stored several times in a row) is `c = b = a = 0;` in the source:
    cl keeps the constant in one register across a chain, and reloads it for separate statements."""
    i = 0
    while i < len(lines):
        m = DEFL.match(lines[i])
        if m and re.fullmatch(r"[()]*(?:\((?:int|unsigned int|unsigned char|char)\))?[()]*-?\d+[()]*", m.group(3)):
            t, k, tg = m.group(2), i + 1, []
            while k < len(lines):
                u = re.match(r"^\s+([\w.\[\]]+) = [()\w ]*\(*%s\)*;$" % t, lines[k])
                if not u or DECL.match(lines[k]):
                    break
                tg.append(u.group(1))
                k += 1
            occ = sum(len(re.findall(r"\b%s\b" % t, l)) for l in lines if not DECL.match(l))
            if len(tg) >= 2 and occ == len(tg) + 1:
                lines[i:k] = ["%s%s = %s;" % (m.group(1), " = ".join(reversed(tg)), m.group(3))]
        i += 1
    return lines


def od_fold(body, deep=()):
    lines = body.split("\n")
    lines = rename_webs(lines)
    lines = chain_consts(lines)
    n = len(lines)
    alive = [True] * n
    addr_taken = set(re.findall(r"&(v_l\d+)", body))
    # counts per temporary, kept up to date as lines change: reads per line (uses), definitions (defs)
    uses, defs = {}, {}

    def account(k, sign):
        if not alive[k] or DECL.match(lines[k]):
            return
        m = DEFL.match(lines[k])
        for t in TMP.findall(m.group(3) if m else lines[k]):
            u = uses.setdefault(t, {})
            u[k] = u.get(k, 0) + sign
            if not u[k]:
                del u[k]
        if m:
            defs[m.group(2)] = defs.get(m.group(2), 0) + sign
    for k in range(n):
        account(k, 1)
    changed = True
    while changed:
        changed = False
        for i in range(n):
            if not alive[i]:
                continue
            m = DEFL.match(lines[i])
            if not m:
                continue
            t, e = m.group(2), m.group(3)
            ut = uses.get(t, {})
            nocc = sum(ut.values())
            if defs.get(t) == 1 and nocc > 1 and t not in e and LEAF.match(e):
                aff = sorted(set([i]) | set(ut))
                for k in aff:
                    account(k, -1)
                ok_ = fold_leaf(lines, alive, i, t, e, addr_taken)
                for k in aff:
                    account(k, 1)
                if ok_:
                    changed = True
                    break
                continue
            if defs.get(t) != 1 or nocc != 1 or t in e:
                continue
            j = next(iter(ut))
            if re.search(r"&\s*\(?\s*%s\b" % t, lines[j]):
                continue                          # its address is the use (`f(&t)`): a variable, not a value
            if j <= i:
                continue
            call = has_call(e)
            mem = reads_mem(e) or call
            slots = set(SLOT.findall(e)) - {"self"}
            ok = True
            for k in range(i + 1, j):
                if not alive[k]:
                    continue
                L = lines[k]
                if DECL.match(L):
                    continue
                if CTRL.match(L):
                    ok = False
                    break
                dm = DEFL.match(L)
                if dm:
                    e2 = dm.group(3)
                    if has_call(e2) and (mem or call):
                        ok = False
                        break
                    if call and (reads_mem(e2) or SLOT.search(e2)):
                        ok = False
                        break
                    continue
                if call:
                    ok = False
                    break
                sm = STORE_SLOT.match(L)
                if sm:
                    if sm.group(1) in slots or (mem and (sm.group(1) in addr_taken)):
                        ok = False
                        break
                    continue
                if mem or has_call(L) or True:      # a store through a pointer / global, or a call statement
                    ok = False
                    break
            if not ok:
                continue
            lj = lines[j]
            mj = DEFL.match(lj)
            if mj:
                new = "%s%s = %s;" % (mj.group(1), mj.group(2), re.sub(r"\b%s\b" % t, lambda _m: "(" + e + ")", mj.group(3)))
            else:
                new = re.sub(r"\b%s\b" % t, lambda _m: "(" + e + ")", lj)
            account(i, -1)
            account(j, -1)
            lines[j] = new
            alive[i] = False
            account(j, 1)
            changed = True
            break
    out = [l for k, l in enumerate(lines) if alive[k]]
    if deep:
        n0 = len(out)
        out2 = ternaries(list(out), set(deep))
        if len(out2) != n0:
            WEBS_SAVE = dict(WEBS)
            text2 = od_fold("\n".join(out2), ())
            WEBS.update(WEBS_SAVE)
            return text2
    text = "\n".join(out)
    for nm, old in WEBS.items():
        if re.search(r"\b%s\b" % nm, text):
            m = re.search(r"(?m)^(    [\w ]+?\*? )%s;\n" % old, text + "\n")
            if m:
                text = text.replace(m.group(0), m.group(0) + "%s%s;\n" % (m.group(1), nm), 1)
    WEBS.clear()
    return text


NARROW = re.compile(r"unsigned|char|short|float|double|__int64|bool")


def _match(s, i):
    """Index of the ')' closing the '(' at s[i]."""
    d = 0
    for k in range(i, len(s)):
        if s[k] == "(":
            d += 1
        elif s[k] == ")":
            d -= 1
            if d == 0:
                return k
    return -1


def simplify(line, narrow_names):
    """Drop `(int)` casts of an expression that is plainly int already: `(int)((int)(a4))` is `a4`. A cast of
    something narrower, unsigned or floating (an operand's declared type or a cast inside) stays."""
    out, i = [], 0
    while i < len(line):
        if line.startswith("(int)(", i):
            j = _match(line, i + 5)
            inner = simplify(line[i + 6:j], narrow_names)
            if NARROW.search(inner) or any(re.search(r"\b%s\b" % n, inner) for n in narrow_names):
                out.append("(int)(" + (inner[6:-1] if inner.startswith("(int)(") and _match(inner, 5) == len(inner) - 1
                                       else inner) + ")")
            else:
                out.append("(" + inner + ")")
            i = j + 1
        else:
            out.append(line[i])
            i += 1
    r = "".join(out)
    for T in ("float", "double"):                      # `(float)(*(float*)...)`: a float load needs no conversion
        pre = "(%s)(" % T
        i = r.find(pre)
        while i >= 0:
            j = _match(r, i + len(pre) - 1)
            inner = r[i + len(pre):j] if j > 0 else ""
            core = inner.strip()
            while core.startswith("(") and _match(core, 0) == len(core) - 1:
                core = core[1:-1].strip()
            fl = core.startswith("*(%s*)" % T) and core[len("*(%s*)" % T):len("*(%s*)" % T) + 1] == "(" and \
                _match(core, len("*(%s*)" % T)) == len(core) - 1
            if j > 0 and (fl or (core.startswith(pre) and _match(core, len(pre) - 1) == len(core) - 1)):
                r = r[:i] + "(" + inner + ")" + r[j + 1:]
                i = r.find(pre, i)
            else:
                i = r.find(pre, i + 1)
    r = re.sub(r"\(int\)(-?\d+)", r"\1", r)
    r = re.sub(r"\(int\)\(int\)", "(int)", r)
    return unparen(r)


def unparen(r):
    """`((x))` -> `(x)`: remove a pair of parentheses that wraps another pair exactly (and `(ident)` as a whole
    operand of a cast or operator stays: only doubles go)."""
    i = 0
    while i < len(r) - 1:
        if r[i] == "(" and r[i + 1] == "(":
            j = _match(r, i)
            if j > 0 and _match(r, i + 1) == j - 1:
                r = r[:i] + r[i + 1:j] + r[j + 1:]
                continue
        i += 1
    return r


def od_clean(body):
    """Casts and parentheses the fold left behind; `this` for the frame copy of the incoming ecx."""
    narrow = set(m.group(1) for m in re.finditer(r"(?m)^    (?:unsigned |signed )?(?:char|short|float|double|__int64|unsigned \w+|bool)\*? "
                                                  r"(\w+)(?:\[\d+\])?;$", body))
    narrow |= set(m.group(1) for m in re.finditer(r"(?m)^    (?:[\w ]+\*) (\w+);$", body))
    narrow |= {"this", "self"}
    return "\n".join(simplify(l, narrow) if l.startswith("    ") and not l.startswith("    //") and not DECL_ANY.match(l)
                     else l for l in body.split("\n"))


DECL_ANY = re.compile(r"^    [\w ]+\*? \w+(?:\[\d+\])?;$")


def this_home(body):
    """`v_lN = (int)(self);` once, nothing else assigns v_lN: v_lN is the slot cl gives `this` at /Od (the prologue's
    `mov [ebp-N], ecx`), so the body reads `self` itself and the member function's own home slot appears."""
    m = re.search(r"(?m)^    (v_l\d+) = \(int\)\(?self\)?;\n", body)
    if not m:
        return body, None
    v = m.group(1)
    if len(re.findall(r"(?m)^\s*%s = " % v, body)) != 1 or re.search(r"&\(*%s\b" % v, body):
        return body, None
    body = body.replace(m.group(0), "", 1).replace("    int %s;\n" % v, "", 1)
    return od_parens(re.sub(r"\b%s\b" % v, "((int)self)", body)), int(v[3:])


SCALAR = {"int": 4, "unsigned int": 4, "unsigned char": 1, "char": 1, "unsigned short": 2, "short": 2, "float": 4,
          "double": 8, "__int64": 8, "char*": 4, "int*": 4, "bool": 1}


PIECE_NAMES = ["fb", "fa"]      # two named automatics: fb is allocated first, then fa (measured, 15.00: work/od notes)


def _pad(n, k):
    """`n` bytes of dead room: int array for the dwords (a char array would make cl add a /GS cookie), chars for the rest."""
    out = []
    if n >= 4:
        out.append("    int dead%d[%d];" % (k, n // 4))
        k += 1
    for _ in range(n % 4):
        out.append("    char dead%d;" % k)
        k += 1
    return out, k


def od_frame(body, size, va):
    """Every `v_lN` the lifter derived from a frame slot goes into packed struct locals laid out at the original's
    offsets (`v_lN` is the slot at ebp-(N-4)), the dead room between them filled: /Od gives each named automatic its
    own slot in an order set by a name hash, so separate locals cannot be placed; an aggregate can.  `size` is the
    named block's bytes (ebp-4 down to ebp-size).  An aggregate of 64 bytes or more is aligned to 8, so a block
    of that size whose length is not a multiple of 8 goes in pieces under 64 bytes, named so the allocator takes them
    top down.  Returns (struct text, body) or (None, body)."""
    orig = body
    items = []                               # (depth of the low end d, bytes, declaration text, name)
    for m in re.finditer(r"(?m)^    (?!return\b)([\w ]+?\*?) (v_l(\d+))(?:\[(\d+)\])?;\n", body):
        ty, name, n, cnt = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if ty not in SCALAR:
            return None, orig
        items.append((n - 4, SCALAR[ty] * (int(cnt) if cnt else 1), m.group(0), name,
                      "%s %s%s;" % (ty, name, "[%s]" % cnt if cnt else "")))
    items.sort()
    last, bytes_mode = 0, False
    for d, sz, _t, _n, _x in items:
        if d > size:
            return None, orig                # below the block: a compiler temporary, not ours to place
        if d - sz < last:
            bytes_mode = True                # overlapping slots (a structure local seen in pieces): offsets only
        last = max(last, d)
    if size < 64 or size % 8 == 0:
        cuts = [(0, size)]
    else:
        cuts = None                          # two pieces: the first ends at a multiple of 8 (its bottom is 8-aligned),
        for c in range(size - 4, 59, -8):    # the second is under 64 bytes
            if c % 8 == 0 and size - c < 64 and not any(d - sz < c < d for d, sz, _t, _n, _x in items):
                cuts = [(0, c), (c, size)]
                break
        if cuts is None:
            return None, orig
    texts, where = [], {}
    for pi, (a, b) in enumerate(cuts):
        mine = [x for x in items if a < x[0] <= b and x[0] - x[1] >= a]
        if bytes_mode:
            mine = []
        fields, at, k = [], b, 0             # walk down from the piece's deepest depth ... fields in address order
        for d, sz, _t, name, decl in sorted(mine, reverse=True):
            if at > d:
                f, k = _pad(at - d, k)
                fields += f
            fields.append("    " + decl)
            at = d - sz
        if at > a:
            f, k = _pad(at - a, k)
            fields += f
        texts.append("struct Fr%d_%08x {\n%s\n};" % (pi, va, "\n".join(fields)))
        for x in mine:
            where[x[3]] = PIECE_NAMES[pi]
    for d, sz, text, name, decl in items:
        body = body.replace(text, "", 1)
        if name in where:
            body = re.sub(r"(?<![\w.])%s\b" % name, "%s.%s" % (where[name], name), body)
            continue
        # offsets only: the piece holding the item, its address plus the item's distance from the piece's bottom
        pi = next((i for i, (a, b) in enumerate(cuts) if a < d <= b and d - sz >= a), None)
        if pi is None:
            return None, orig
        ty = re.match(r"^(.*?) v_l\d+", decl).group(1)
        isarr = "[" in decl
        ptr = "((%s*)((char*)&%s + %d))" % (ty, PIECE_NAMES[pi], cuts[pi][1] - d)
        body = re.sub(r"&\(%s\)|&%s(?![\w\[])" % (name, name), lambda _m: ptr, body)
        body = re.sub(r"(?<![\w.])%s\[" % name, lambda _m: ptr + "[", body)
        body = re.sub(r"(?<![\w.])%s\b" % name, lambda _m: ptr if isarr else "(*%s)" % ptr, body)
    decl = "".join("    Fr%d_%08x %s;\n" % (pi, va, PIECE_NAMES[pi]) for pi in range(len(cuts)))
    body = body.replace("{\n", "{\n" + decl, 1)
    return "#pragma pack(push, 1)\n" + "\n".join(texts) + "\n#pragma pack(pop)", body


LABEL = re.compile(r"^\s*(L\w+):;\s*$")
GOTO = re.compile(r"^\s*goto (L\w+);\s*$")
IFL = re.compile(r"^(\s*)if \((.*)\) \{\s*$")
ASSIGN1 = re.compile(r"^\s+(.*);$")


def _close(lines, i):
    """Index of the `}` line closing the `{` that ends lines[i] (lines are one statement each)."""
    d = 0
    for k in range(i, len(lines)):
        d += lines[k].count("{") - lines[k].count("}")
        if d == 0 and k > i or (d == 0 and k == i and "}" in lines[k]):
            return k
    return -1


def _nested_loop(lines, a, k, kinds=("for", "while", "do", "switch")):
    """Is lines[k] inside a loop (or switch) block opened at or after lines[a]?"""
    st = []
    for j in range(a, k):
        s = lines[j]
        for _ in range(s.count("}")):
            if st:
                st.pop()
        for _ in range(s.count("{")):
            st.append(bool(re.match(r"^\s*(?:%s)\b" % "|".join(kinds), s)))
    return any(st)


def _inner_backjump(lines, a, b):
    """A label in lines[a:b] that a later goto in lines[a:b] reaches: an inner loop not yet written as one."""
    for k in range(a, b):
        m = LABEL.match(lines[k])
        if m and any(re.search(r"\bgoto %s;" % m.group(1), lines[j]) for j in range(k + 1, b)):
            return True
    return False


def _loop_body(lines, f, e, Lab, refs):
    """The body of the loop whose `if` is lines[f] (closed at lines[e]) and whose back edge is `goto Lab`, written
    with `continue` / `break`, or None. The back edges inside the body (not inside an inner loop) are `continue`; a
    body that falls out of the `if` leaves the loop (`break` at its end, or in the arm that falls out when the other
    arm is a bare `continue`); a goto to the label right after the loop is `break`."""
    body = lines[f + 1:e]
    a = f + 1
    inside = sum(1 for l in body if re.search(r"\bgoto %s;" % Lab, l))
    if inside != refs(Lab) or not inside:
        return None
    last_back = GOTO.match(lines[e - 1]) is not None and GOTO.match(lines[e - 1]).group(1) == Lab
    if last_back and inside == 1:
        return None                               # the plain loop: od_loops' own rule
    if _inner_backjump(lines, a, e):
        return None
    for k in range(a, e - (1 if last_back else 0)):
        if re.search(r"\bgoto %s;" % Lab, lines[k]) and _nested_loop(lines, a, k):
            return None
    out_lab = LABEL.match(lines[e + 1]).group(1) if e + 1 < len(lines) and LABEL.match(lines[e + 1]) else None
    if out_lab:
        for k in range(a, e):
            if re.search(r"\bgoto %s;" % out_lab, lines[k]) and _nested_loop(lines, a, k, ("for", "while", "do",
                                                                                            "switch")):
                return None
    body = list(body)
    if last_back:
        body = body[:-1]
    else:
        if not body or re.match(r"^\s*(?:return\b.*|goto \w+);$", body[-1]):
            return None
        ind = re.match(r"^(\s*)", lines[f]).group(1) + "    "
        # `if (c) { A } else { goto Lab; }` at the end: `if (c) { A; break; }`
        if len(body) >= 3 and body[-1] == ind + "}" and body[-3] == ind + "} else {" and \
                GOTO.match(body[-2]) and GOTO.match(body[-2]).group(1) == Lab and \
                not re.match(r"^\s*(?:return\b.*|goto \w+|continue|break);$", body[-4]):
            body = body[:-3] + [ind + "    break;", ind + "}"]
        else:
            return None                           # (the goto form compiles like the source's own loop: Spore 004549b0)
    body = [re.sub(r"\bgoto %s;" % Lab, "continue;", l) for l in body]
    drop_out = False
    if out_lab:
        n_out = sum(1 for l in body if re.search(r"\bgoto %s;" % out_lab, l))
        if n_out:
            body = [re.sub(r"\bgoto %s;" % out_lab, "break;", l) for l in body]
            drop_out = n_out == refs(out_lab)
    return body, drop_out


def od_loops(body):
    """cl compiles `for (init; c; inc) body` at /Od as `init; jmp C; I: inc; C: c; jcc out; body; jmp I` and
    `while (c) body` as `T: c; jcc out; body; jmp T`; a `goto` written for either gets another jmp.  il2c's structuring
    leaves both as gotos around an `if`; they go back to loops here."""
    lines = body.split("\n")
    refs = lambda L: sum(1 for l in lines if re.search(r"\bgoto %s;" % L, l))
    changed = True
    while changed:
        changed = False
        for i, l in enumerate(lines):
            g = GOTO.match(l)
            ml = LABEL.match(l)
            if g and i + 1 < len(lines):
                C = g.group(1)
                j = i + 1
                lm = LABEL.match(lines[j])
                if not lm:
                    continue
                Lab = lm.group(1)
                k = j + 1
                inc = []
                while k < len(lines) and not LABEL.match(lines[k]) and ASSIGN1.match(lines[k]) and \
                        not CTRL.match(lines[k]) and not DECL_ANY.match(lines[k]):
                    inc.append(ASSIGN1.match(lines[k]).group(1))
                    k += 1
                cm = LABEL.match(lines[k]) if k < len(lines) else None
                if not cm or cm.group(1) != C or refs(C) != 1:
                    continue
                f = k + 1
                im = IFL.match(lines[f]) if f < len(lines) else None
                if not im:
                    continue
                e = _close(lines, f)
                if e < 0:
                    continue
                ind = im.group(1)
                if GOTO.match(lines[e - 1]) and GOTO.match(lines[e - 1]).group(1) == Lab and refs(Lab) == 1:
                    new = ["%sfor (; %s; %s) {" % (ind, im.group(2), ", ".join(inc))] + lines[f + 1:e - 1] + [lines[e]]
                    lines[i:e + 1] = new
                else:                                 # continue / break inside, or a body that leaves the loop
                    lb = _loop_body(lines, f, e, Lab, refs)
                    if lb is None:
                        continue
                    new = ["%sfor (; %s; %s) {" % (ind, im.group(2), ", ".join(inc))] + lb[0] + [lines[e]]
                    tail = lines[e + 2:] if lb[1] else lines[e + 1:]
                    lines[i:] = new + tail
                changed = True
                break
            if ml and i + 1 < len(lines):
                im = IFL.match(lines[i + 1])
                if not im:
                    continue
                e = _close(lines, i + 1)
                if e < 0:
                    continue
                if GOTO.match(lines[e - 1]) and GOTO.match(lines[e - 1]).group(1) == ml.group(1) and \
                        refs(ml.group(1)) == 1:
                    new = ["%swhile (%s) {" % (im.group(1), im.group(2))] + lines[i + 2:e - 1] + [lines[e]]
                    lines[i:e + 1] = new
                else:
                    lb = _loop_body(lines, i + 1, e, ml.group(1), refs)
                    if lb is None:
                        continue
                    new = ["%swhile (%s) {" % (im.group(1), im.group(2))] + lb[0] + [lines[e]]
                    lines[i:] = new + (lines[e + 2:] if lb[1] else lines[e + 1:])
                changed = True
                break
    return "\n".join(lines)




SDECLS = set()
_MEM = re.compile(r"\*\(int\*\)\(\(char\*\)\(")


def _sname(sig):
    return "S%d" % len(sig) if set(sig) == {"i"} else "SF_" + sig


def _addr(e, i):
    """Parse `*(int*)((char*)(P)[ + k])` (or `*(float*)`) at e[i:]: (P, k, end, "i" | "f") or None."""
    ty = "i" if e.startswith("*(int*)((char*)(", i) else "f" if e.startswith("*(float*)((char*)(", i) else None
    if ty is None:
        return None
    a = i + len("*(int*)((char*)" if ty == "i" else "*(float*)((char*)")
    b = _match(e, a)
    if b < 0:
        return None
    P = e[a + 1:b]
    k, j = 0, b + 1
    m = re.match(r" \+ (\d+)\)", e[j:])
    if m:
        k, j = int(m.group(1)), j + m.end()
    elif e[j:j + 1] == ")":
        j += 1
    else:
        return None
    return P, k, j, ty


def od_struct_copies(body):
    """Consecutive dword copies `*(P + 0) = *(Q + 0); *(P + 4) = *(Q + 4); ..` are one structure assignment in the
    source (cl keeps P and Q in registers across it; separate statements would reload them)."""
    lines = body.split("\n")
    for k, l in enumerate(lines):                  # a block copy the lifter wrote as memcpy (rep movsd)
        m = re.match(r"^(\s+)memcpy\(\(void\*\)(\(.*)$", l)
        if not m:
            continue
        a = m.group(2)
        j = _match(a, 0)
        rest = a[j + 1:]
        m2 = re.match(r", \(const void\*\)(\(.*)$", rest)
        if j < 0 or not m2:
            continue
        b = m2.group(1)
        j2 = _match(b, 0)
        m3 = re.match(r", \(unsigned int\)\(+(?:\(int\))?(\d+)\)+;$", b[j2 + 1:]) if j2 > 0 else None
        if not m3 or int(m3.group(1)) % 4 or int(m3.group(1)) < 8:
            continue
        sig = "i" * (int(m3.group(1)) // 4)
        SDECLS.add(sig)
        lines[k] = "%s*(%s*)%s = *(%s*)%s;" % (m.group(1), _sname(sig), a[:j + 1], _sname(sig), b[:j2 + 1])
    out, i = [], 0
    while i < len(lines):
        run = []
        j = i
        while j < len(lines):
            m = re.match(r"^(\s+)(\*\((?:int|float)\*\)\(\(char\*\)\(.*?) = (.*);$", lines[j])
            if not m:
                break
            rhs = m.group(3)
            while True:
                if rhs.startswith("(int)"):
                    rhs = rhs[5:]
                elif rhs.startswith("(float)"):
                    rhs = rhs[7:]
                elif rhs.startswith("(") and _match(rhs, 0) == len(rhs) - 1:
                    rhs = rhs[1:-1]
                else:
                    break
            a, b = _addr(m.group(2), 0), _addr(rhs, 0)
            if a and a[2] == len(m.group(2)) and not b:
                ms = re.fullmatch(r"[()]*(v_l(\d+))[()]*", rhs)      # a frame slot: the source is a local structure
                if ms:
                    b = ("&" + ms.group(1), -int(ms.group(2)), len(rhs), "i")
            if not a or not b or a[2] != len(m.group(2)) or b[2] != len(rhs):
                break
            run.append((m.group(1), a, b))
            j += 1
        # longest prefix of the run that is an ascending dword copy of one pair
        n = 0
        while n < len(run) and run[n][1][0] == run[0][1][0] and \
                run[n][1][1] == 4 * n + run[0][1][1] and \
                ((run[n][2][0] == run[0][2][0] and run[n][2][1] == 4 * n + run[0][2][1]) or
                 (run[0][2][0].startswith("&") and run[n][2][0].startswith("&") and
                  run[n][2][1] == run[0][2][1] + 4 * n)):
            n += 1
        if n >= (3 if run and run[0][2][0].startswith("&") else 2) and run[0][1][1] == 0 and \
                (run[0][2][1] == 0 or run[0][2][0].startswith("&")) and \
                run[0][1][0] != run[0][2][0] and \
                all(run[q][1][3] == run[q][2][3] == "i" for q in range(n)):
            sig = "".join(run[q][1][3] for q in range(n))
            SDECLS.add(sig)
            ind, a, b = run[0]
            src = b[0] if b[0].startswith("&") else "(%s)" % b[0]
            out.append("%s*(%s*)(%s) = *(%s*)%s;" % (ind, _sname(sig), a[0], _sname(sig), src))
            i += n
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


# ---- self-check: compile a source /Od, lift every function from its own bytes, recompile, compare ----

class _FakeImage:
    """One function's bytes at a fixed address, no relocations: the lifter's image."""

    def __init__(self, data, base):
        import capstone
        self.data, self.base, self.relocs = data, base, []
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        self.md.detail = True
        self.secs = [(base, len(data), 0)]

    def reloc_at(self, va):
        return False

    def off(self, va):
        return va - self.base if self.base <= va < self.base + len(self.data) else None

    def insns(self, va, n):
        o = self.off(va)
        return list(self.md.disasm(self.data[o:o + n], va)) if o is not None else []

    def bytes_at(self, va, n):
        o = self.off(va)
        return self.data[o:o + n]


def selfcheck(src, flags="/Od /Ob1 /MD /Gy /TP /EHsc", only=()):
    """{symbol: "MATCH" | "DIFF a/b" | "LIFT-ERR .." | "CL-ERR"} for each function of src (no calls, no globals: a
    relocated operand is not an input here). The original's bytes are the compiled source's own."""
    import hashlib, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import w8_score, w8_diff, il2c_lift
    from coff import parse_coff
    root = os.path.dirname(here)
    scratch = os.path.join(root, "work", "scratch", "il2c_od")
    os.makedirs(scratch, exist_ok=True)
    obj, log = w8_score.compile_one((os.path.abspath(src), flags, "od_" + hashlib.md5(open(src).read().encode()).hexdigest()[:10]))
    if not obj:
        return {"(compile)": "CL-ERR " + (log or "")[-200:]}
    secs, syms, _ = parse_coff(obj, sizes=True)
    out = {}
    for nm, (si, off, msz) in sorted(syms.items()):
        if not nm.startswith("?") or (only and not any(o in nm for o in only)):
            continue
        sec = secs[si]
        if sec["data"] is None or not msz and False:
            continue
        d = bytes(sec["data"][off:off + msz] if msz else sec["data"][off:]).rstrip(b"\xcc")
        if not d:
            continue
        try:
            text, _lf = il2c_lift.unit(_FakeImage(d, 0x401000), 0x401000, len(d), set(), flags=flags)
        except Exception as ex:
            out[nm] = "LIFT-ERR %s %s" % (type(ex).__name__, ex)
            continue
        p = os.path.join(scratch, "gen_%s.cpp" % hashlib.md5(nm.encode()).hexdigest()[:8])
        open(p, "w").write(text)
        o2, log2 = w8_score.compile_one((p, flags, "odg_" + hashlib.md5(text.encode()).hexdigest()[:10]))
        if not o2:
            out[nm] = "CL-ERR " + (log2 or "")[-120:]
            continue
        mine = bytes(w8_diff.ours_bytes(o2, 0x401000)[0]).rstrip(b"\xcc")
        out[nm] = "MATCH" if mine == d else "DIFF %d/%d" % (len(mine), len(d))
    return out


if __name__ == "__main__":
    import sys
    for k, v in selfcheck(sys.argv[1], *sys.argv[2:3]).items():
        print(k, v)


DELS = set()


def od_inline_calls(body):
    """`xor ecx, ecx; and ecx, 1; je L; push p; call f; add esp, 4`: an inlined `if (flags & 1) f(p)` whose argument is the
    constant 0 (a scalar-deleting-destructor's operator delete).  The front end folds `0 & 1`; c2 inlining an `inline`
    helper does not, so the statement is a call of a helper that is inlined at /Ob1."""
    pat = re.compile(r"(?m)^(\s+)if \(+\(?(-?\d+)\)? & 1\)+ != 0\) \{\n\s+(FUN_[0-9a-f]{8})\(\(unsigned int\)\((.*)\)\);\n\s+\}$")

    def rep(m):
        DELS.add(m.group(3))
        return "%sil2c_d_%s(%s, %s);" % (m.group(1), m.group(3), m.group(4), m.group(2))
    return pat.sub(rep, body)


def _block_open(lines, close):
    """Index of the line that opens the block the `}` at lines[close] ends (its `if (..) {`, not the else)."""
    d = 0
    for k in range(close, -1, -1):
        st = lines[k].strip()
        d += st.count("}") - st.count("{")
        if d == 0 and st.endswith("{") and not st.startswith("}"):
            return k
    return -1


def _push_ret(lines, close, T):
    """Rewrite the tail of every arm of the if / else ending at lines[close] from `T = E;` to `return E;`; None when
    an arm does not end that way. Returns {index: new line}."""
    o = _block_open(lines, close)
    if o < 0:
        return None
    depth, e = 0, None
    for k in range(o, close + 1):
        st = lines[k].strip()
        if st.startswith("} else") and depth == 1:
            e = k
        depth += st.count("{") - st.count("}")
    if e is None:
        return None
    out = {}
    for lo, hi in ((o + 1, e), (e + 1, close)):
        a = hi - 1
        if a < lo:
            return None
        m = re.match(r"^(\s+)%s = (.*);$" % T, lines[a])
        if m:
            out[a] = "%sreturn %s;" % (m.group(1), m.group(2))
        elif lines[a].strip() == "}":
            sub = _push_ret(lines, a, T)
            if sub is None:
                return None
            out.update(sub)
        elif re.match(r"^\s+(return\b|goto\b)", lines[a]):
            continue
        else:
            return None
    return out


def _sw_eval(n, T, v):
    """Evaluate a parsed condition with variable T = v (32-bit int semantics), or raise _NoParse."""
    k = n[0]
    if k == "grp":
        return _sw_eval(n[1], T, v)
    if k == "atom":
        if n[1] == T:
            return v
        if re.match(r"^\d+$", n[1]):
            return int(n[1])
        raise _NoParse(n[1])
    if k == "cast":
        x = _sw_eval(n[2], T, v) & 0xFFFFFFFF
        if n[1] == "unsigned int":
            return x
        if n[1] == "int":
            return x - (1 << 32) if x & 0x80000000 else x
        raise _NoParse(n[1])
    if k == "un":
        x = _sw_eval(n[2], T, v)
        if n[1] == "-":
            return -x
        if n[1] == "!":
            return int(not x)
        raise _NoParse(n[1])
    if k == "bin":
        op = n[1]
        if op == "&&":
            return int(bool(_sw_eval(n[2], T, v)) and bool(_sw_eval(n[3], T, v)))
        if op == "||":
            return int(bool(_sw_eval(n[2], T, v)) or bool(_sw_eval(n[3], T, v)))
        a, b = _sw_eval(n[2], T, v), _sw_eval(n[3], T, v)
        f = {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}
        if op not in f:
            raise _NoParse(op)
        return int(f[op])
    raise _NoParse(k)


def _sw_consts(n, out):
    if isinstance(n, tuple):
        if n[0] in ("atom", "un", "cast", "grp") and n[0] != "bin":
            try:
                if _sw_eval(n, "\0", 0) is not None and not (n[0] == "un" and n[1] == "!"):
                    out.add(_sw_eval(n, "\0", 0))
                    return
            except _NoParse:
                pass
        for x in n[1:]:
            _sw_consts(x, out)


SWTEMPS = set()
SWIF = re.compile(r"^(\s*)if \((.*)\) (goto L\w+;|\{)$")


def od_switch(body):
    """/Od sparse switch: `T = e;` then a compare tree on T (`cmp [T], K; jg / je ..`, lifted as ifs and gotos) whose
    leaves are the case labels and the label after the switch: `switch (e) { case K: .. }` (cl builds the same tree and
    its own temporary T). The switch closes at that label (no default: a value outside the cases leaves the switch)."""
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s+)(v_l\d+) = (.*);$", lines[i])
        if not m:
            i += 1
            continue
        ind, T, E = m.groups()
        # the dispatch: ifs / gotos on T only, ending right before a label
        j, d, conds, ok = i + 1, 0, [], True
        while j < len(lines):
            l = lines[j]
            if LABEL.match(l) and d == 0:
                break
            mi = SWIF.match(l)
            if mi:
                try:
                    p = _P(_tokens(mi.group(2)))
                    n = p.expr(3)
                    if p.peek() is not None:
                        raise _NoParse("tail")
                    _sw_eval(n, T, 0)
                except (_NoParse, RecursionError, IndexError):
                    ok = False
                    break
                conds.append(n)
                d += mi.group(3) == "{"
            elif GOTO.match(l):
                pass
            elif l.strip() == "}" and d > 0:
                d -= 1
            elif l.strip() == "} else {" and d > 0:
                pass
            else:
                ok = False
                break
            j += 1
        if not ok or j >= len(lines) or len(conds) < 2 or d != 0:
            i += 1
            continue
        # T only here (its assignment, the dispatch) and in plain stores before it
        uses = [k for k, l in enumerate(lines) if re.search(r"\b%s\b" % T, l) and not DECL_ANY.match(l)]
        pre = [k for k in uses if k < i and re.match(r"^\s+%s = [^;]*;$" % T, lines[k])]
        if any(not (i <= k < j) and k not in pre for k in uses):
            i += 1
            continue
        fall = LABEL.match(lines[j]).group(1)

        def run(a, b, v):
            k = a
            while k < b:
                l = lines[k]
                g = GOTO.match(l)
                if g:
                    return g.group(1)
                mi = SWIF.match(l)
                if mi:
                    c = _sw_eval(_P(_tokens(mi.group(2))).expr(3), T, v)
                    if mi.group(3) != "{":
                        if c:
                            return re.match(r"goto (L\w+);", mi.group(3)).group(1)
                        k += 1
                        continue
                    e = _close(lines, k)
                    el = next((q for q in range(k + 1, e) if lines[q].startswith(mi.group(1) + "} else {")), None)
                    if c:
                        r = run(k + 1, el if el is not None else e, v)
                    else:
                        r = run(el + 1, e, v) if el is not None else None
                    if r:
                        return r
                    k = e + 1
                    continue
                k += 1
            return None
        ks = set()
        for n in conds:
            _sw_consts(n, ks)
        ks = sorted(x - (1 << 32) if x >= 0x80000000 else x for x in ks)
        try:
            tgt = {v: run(i + 1, j, v) or fall for v in ks}
            probes = {ks[0] - 1, ks[-1] + 1} | {x + 1 for x in ks} | {x - 1 for x in ks}
            dflt = {run(i + 1, j, v) or fall for v in probes if v not in ks}
        except (_NoParse, RecursionError, IndexError):
            i += 1
            continue
        if len(dflt) != 1:
            i += 1
            continue
        D = dflt.pop()
        cases = {v: t for v, t in tgt.items() if t != D}
        if len(cases) < 2:
            i += 1
            continue
        # the labels: case targets after the dispatch, then D at the dispatch's depth; braces balanced between
        labs = {LABEL.match(l).group(1): k for k, l in enumerate(lines) if LABEL.match(l)}
        if D not in labs or any(t not in labs for t in cases.values()):
            i += 1
            continue
        dk = labs[D]
        if any(not j <= labs[t] < dk for t in cases.values()) or dk < j or \
                not lines[dk].startswith(ind + "L") or sum(x.count("{") - x.count("}") for x in lines[j:dk]) != 0:
            i += 1
            continue
        run_, bad = 0, False
        for x in lines[j:dk]:
            if run_ == 0 and x.strip().startswith("}"):
                bad = True
            run_ += x.count("{") - x.count("}")
        if bad:
            i += 1
            continue
        uns = any("unsigned" in str(n) for n in conds)
        SWTEMPS.add(int(T[3:]))                   # its slot is cl's switch temporary now
        out = [l for k, l in enumerate(lines[:i]) if k not in pre]
        out.append("%sswitch (%s) {" % (ind, "(unsigned int)(%s)" % E if uns else E))
        bylab = {}
        for v, t in cases.items():
            bylab.setdefault(t, []).append(v)
        nD = sum(1 for l in lines if re.search(r"\bgoto %s;" % D, l))
        nDin = 0
        for k in range(j, dk):
            l = lines[k]
            ml = LABEL.match(l)
            if ml and ml.group(1) in bylab:
                for v in sorted(bylab[ml.group(1)]):
                    out.append("%scase %d:" % (ind, v))
            if re.search(r"\bgoto %s;" % D, l) and not _nested_loop(lines, j, k):
                l = re.sub(r"\bgoto %s;" % D, "break;", l)
                nDin += 1
            out.append(("    " + l) if l.strip() else l)
        out.append(ind + "}")
        nDdisp = sum(1 for k in range(i + 1, j) if re.search(r"\bgoto %s;" % D, lines[k]))
        keep = nD - nDdisp - nDin > 0
        out += lines[dk:] if keep else lines[dk + 1:]
        lines = out
        i += 1
    return "\n".join(lines)


def od_return_merge(body):
    """`if (c) { ..; t = e1; } else { ..; t = e2; } return t;`: the source had a `return` in each arm (cl's eax is the
    one register both arms leave, not a variable)."""
    lines = body.split("\n")
    for r in range(len(lines) - 1, 0, -1):
        m = re.match(r"^\s+return \(*(t\d+)\)*;$", lines[r])
        if not m or lines[r - 1].strip() != "}":
            continue
        T = m.group(1)
        sub = _push_ret(lines, r - 1, T)
        if sub is None:
            continue
        occ = sum(len(re.findall(r"\b%s\b" % T, l)) for l in lines if not DECL.match(l))
        if occ != sum(1 for k in sub if re.search(r"\b%s\b" % T, lines[k])) + 1:
            continue
        for k, v in sub.items():
            lines[k] = v
        del lines[r]
        return od_return_merge("\n".join(lines))
    return od_unelse(body)


def od_unelse(body):
    """`if (c) { ..; return a; } else { ..; return b; }` as the function's last statement: cl emits a jmp for the last
    return too; with the else arm spliced in after the if (its code falls into the epilogue) there is none."""
    lines = body.split("\n")
    top = [k for k, l in enumerate(lines) if l.startswith("    } else {")]
    for e in top:
        if not re.match(r"^\s+return\b", lines[e - 1]):
            continue
        close = next((k for k in range(e + 1, len(lines)) if lines[k] == "    }"), None)
        if close is None:
            continue
        tail = [l for l in lines[close + 1:] if l.strip()]
        if tail != ["}"]:
            continue
        lines[e] = "    }"
        del lines[close]
        return "\n".join(lines)
    return body


_FLIP = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}


def negate(c):
    """!c for an if condition: a single top-level comparison flips its operator; anything else is wrapped."""
    c = c.strip()
    while c.startswith("(") and _match(c, 0) == len(c) - 1:
        c = c[1:-1].strip()
    ops, d, i = [], 0, 0
    while i < len(c):
        ch = c[i]
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
        elif d == 0:
            two = c[i:i + 2]
            if two in ("&&", "||"):
                return "!(%s)" % c
            if two in ("==", "!=", "<=", ">="):
                ops.append((i, two))
                i += 2
                continue
            if ch in "<>" and c[i + 1:i + 2] != "<" and c[i - 1:i] not in ("<", ">"):
                ops.append((i, ch))
        i += 1
    if len(ops) == 1:
        i, op = ops[0]
        return "%s %s %s" % (c[:i].strip(), _FLIP[op], c[i + len(op):].strip())
    return "!(%s)" % c


def od_short_circuit(body):
    """`if (A) { if (B) goto L; } body.. L:;`  is  `if (!A || !B) { body.. }` (cl compiles an `||` as a chain of jumps to
    the body and past it; the goto form has the same jumps in another order).  L must be reached only from there."""
    lines = body.split("\n")
    changed = True
    while changed:
        changed = False
        for i in range(len(lines) - 3):
            a = IFL.match(lines[i])
            b = re.match(r"^\s+if \((.*)\) goto (L\w+);$", lines[i + 1])
            if not a or not b or lines[i + 2].strip() != "}":
                continue
            L = b.group(2)
            lab = next((k for k in range(i + 3, len(lines)) if lines[k].strip() == "%s:;" % L), None)
            if lab is None or sum(1 for l in lines if re.search(r"\bgoto %s;" % L, l)) != 1:
                continue
            seg = lines[i + 3:lab]
            if any(LABEL.match(l) for l in seg) or sum(l.count("{") - l.count("}") for l in seg) != 0:
                continue
            ind = a.group(1)
            new = ["%sif (%s || %s) {" % (ind, negate(a.group(2)), negate(b.group(1)))] + ["    " + l for l in seg] + [ind + "}"]
            lines[i:lab + 1] = new
            changed = True
            break
    return "\n".join(lines)


def od_goto_else(body):
    """`if (c) { s; goto L; } r; L:;` is `if (c) { s; } else { r; }` (one jmp over the else arm; the `goto` adds another)."""
    lines = body.split("\n")
    changed = True
    while changed:
        changed = False
        for i, l in enumerate(lines):
            a = IFL.match(l)
            if not a:
                continue
            e = _close(lines, i)
            if e < 0 or e - 1 <= i or e + 1 >= len(lines) or lines[e].strip() != "}":
                continue
            b_ = IFL.match(lines[i + 1])
            ib_ = b_.group(1) if b_ else None
            if b_ and _close(lines, i + 1) == e - 1 and GOTO.match(lines[e - 2]) and \
                    not any(x.startswith(ib_ + "} else") for x in lines[i + 2:e - 1]):
                # `if (A) { if (B) { s; goto L; } }`: `if (A && B) { s; goto L; }` (the same code), then an else
                lines[i:e + 1] = ["%sif ((%s) && (%s)) {" % (a.group(1), a.group(2), b_.group(2))] + \
                    [x[4:] for x in lines[i + 2:e - 1]] + [lines[e]]
                changed = True
                break
            g = GOTO.match(lines[e - 1])
            if not g or lines[i + 1:e].count(lines[e - 1]) != 1:
                continue
            di = depth_of(lines, i)
            if any(lines[k].strip().startswith("} else") and depth_of(lines, k) == di for k in range(i + 1, e)):
                continue                      # the if already has an else arm
            L = g.group(1)
            uses = sum(1 for x in lines if re.search(r"\bgoto %s;" % L, x))
            lab = next((k for k in range(e + 1, len(lines)) if lines[k].strip() == "%s:;" % L), None)
            if lab is None or lab == e + 1:
                continue
            seg = lines[e + 1:lab]
            # the label may follow the ends of enclosing if / else arms (`if (A) { ..; goto L; } if (B) { ..; goto L; }
            # r; L:` once the outer one is an else: B's goto reaches L by falling out of the else arm)
            closers = max(0, -sum(x.count("{") - x.count("}") for x in seg))
            if closers and any(x.strip() != "}" for x in seg[len(seg) - closers:]):
                continue
            run_ = 0
            for x in seg[:len(seg) - closers]:    # r stays inside the block it starts in
                if run_ == 0 and x.strip().startswith("}"):
                    break
                run_ += x.count("{") - x.count("}")
                if run_ < 0:
                    break
            else:
                run_ = None
            if run_ is not None:
                continue
            if closers:
                body_ = seg[:len(seg) - closers]
                if sum(x.count("{") - x.count("}") for x in body_) != 0 or not body_:
                    continue
                ok = True
                for q in range(closers):          # each closed block is an if or else arm, not a loop body
                    k_ = e + 1 + len(body_) + q
                    d, o = 0, None
                    for j in range(k_, -1, -1):
                        d += lines[j].count("}") - lines[j].count("{")
                        if d == 0:
                            o = j
                            break
                    if o is None or not re.match(r"^\s*(?:\} else )?(?:if \(.*\) )?\{\s*$|^\s*\} else \{\s*$", lines[o]) \
                            or not (IFL.match(lines[o]) or lines[o].strip().startswith("} else")):
                        ok = False
                        break
                if not ok:
                    continue
                seg = body_
            inner = [LABEL.match(x).group(1) for x in seg if LABEL.match(x)]
            segtext = "\n".join(seg)
            if any(len(re.findall(r"\bgoto %s;" % q, segtext)) != len(re.findall(r"\bgoto %s;" % q, "\n".join(lines)))
                   for q in inner) or sum(x.count("{") - x.count("}") for x in seg) != 0:
                continue                          # a label in r that only r's own loops reach moves with it
            if depth_of(lines, i) != depth_of(lines, lab) + closers:
                continue
            ind = a.group(1)
            new = lines[i:e - 1] + [ind + "} else {"] + ["    " + x for x in seg] + [ind + "}"]
            if uses == 1 and not closers:
                lines[i:lab + 1] = new
            else:                                 # other gotos still reach the label
                lines[i:e + 1 + len(seg)] = new
                if uses == 1:
                    lab_ = next(k for k in range(len(lines)) if lines[k].strip() == "%s:;" % L)
                    del lines[lab_]
            changed = True
            break
    return "\n".join(lines)


def depth_of(lines, k):
    d = 0
    for j in range(k):
        d += lines[j].count("{") - lines[j].count("}")
    return d - (1 if lines[k].strip().startswith("}") else 0)


# ---- minimal parentheses: at /Od cl evaluates `x = (expr)` and `x = expr` in a different order, so the emitted C++ must
# ---- carry only the parentheses precedence needs (what a source file has), not the ones the folds leave behind ----

_TOK = re.compile(r"\s*(->|<<|>>|<=|>=|==|!=|&&|\|\||0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?[fFuUlL]*|[A-Za-z_]\w*|[-+*/%&|^~!<>?:=,()\[\].])")
_TYPEW = {"unsigned", "signed", "int", "char", "short", "float", "double", "bool", "void", "long", "__int64", "const",
          "__cdecl", "__stdcall", "__thiscall", "__fastcall"}
_TYPEPAT = re.compile(r"^(?:il2c_vc\d+|C_[0-9a-f]{8}|D_[0-9a-f]{8}|SF_\w+|S\d+|FUN_[0-9a-f]{8}|Fr\d_[0-9a-f]{8})$")
_BINPREC = {"*": 13, "/": 13, "%": 13, "+": 12, "-": 12, "<<": 11, ">>": 11, "<": 10, "<=": 10, ">": 10, ">=": 10,
            "==": 9, "!=": 9, "&": 8, "^": 7, "|": 6, "&&": 5, "||": 4}


class _NoParse(Exception):
    pass


def _tokens(s):
    out, i = [], 0
    s = s.rstrip()
    while i < len(s):
        m = _TOK.match(s, i)
        if not m:
            raise _NoParse(s[i:i + 10])
        out.append(m.group(1))
        i = m.end()
    return out


class _P:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else None

    def eat(self, x=None):
        v = self.peek()
        if v is None or (x is not None and v != x):
            raise _NoParse("expected %s got %s" % (x, v))
        self.i += 1
        return v

    def type_end(self, j):
        """If self.t[j] is '(' opening a cast type, the index after its ')' else None."""
        d, k, words = 0, j, []
        while k < len(self.t):
            x = self.t[k]
            if x == "(":
                d += 1
            elif x == ")":
                d -= 1
                if d == 0:
                    break
            elif d == 1 and not (x == "*" or x in _TYPEW or _TYPEPAT.match(x)):
                return None
            elif d >= 2 and not (x in ("*", ",") or x in _TYPEW or _TYPEPAT.match(x) or x in ("(", ")")):
                return None
            if d >= 1 and x not in ("(", ")", "*"):
                words.append(x)
            k += 1
        if k >= len(self.t) or not words:
            return None
        nxt = self.t[k + 1] if k + 1 < len(self.t) else None
        if nxt is None or not (re.match(r"[A-Za-z_\d]", nxt) or nxt in ("(", "*", "&", "-", "!", "~")):
            return None
        return k + 1

    def expr(self, minp=3):
        left = self.unary()
        while True:
            op = self.peek()
            if op == "?" and minp <= 3:
                self.eat("?")
                a = self.expr(3)
                self.eat(":")
                b = self.expr(3)
                left = ("tern", left, a, b)
                continue
            p = _BINPREC.get(op)
            if p is None or p < minp:
                return left
            self.eat()
            right = self.expr(p + 1)
            left = ("bin", op, left, right)

    def unary(self):
        t = self.peek()
        if t in ("*", "&", "-", "!", "~", "+"):
            self.eat()
            return ("un", t, self.unary())
        if t == "(":
            e = self.type_end(self.i)
            if e is not None:
                typ = " ".join(self.t[self.i + 1:e - 1]).replace("( ", "(").replace(" )", ")").replace(" *", "*")
                typ = re.sub(r"\s+", " ", typ).replace(") (", ")(")
                self.i = e
                return ("cast", typ, self.unary())
            self.eat("(")
            inner = self.expr(0 + 3)
            while self.peek() == ",":
                self.eat()
                self.expr(3)
                raise _NoParse("comma")
            self.eat(")")
            return self.post(("grp", inner))
        return self.post(self.atom())

    def atom(self):
        t = self.eat()
        if not re.match(r"[A-Za-z_\d]", t):
            raise _NoParse("atom " + t)
        return ("atom", t)

    def post(self, node):
        while True:
            t = self.peek()
            if t == "->" or t == ".":
                self.eat()
                node = ("mem", t, node, self.eat())
            elif t == "[":
                self.eat()
                node = ("idx", node, self.expr(3))
                self.eat("]")
            elif t == "(" and node[0] != "atom" or (t == "(" and node[0] == "atom" and re.match(r"[A-Za-z_]", node[1])):
                self.eat("(")
                args = []
                if self.peek() != ")":
                    args.append(self.expr(3))
                    while self.peek() == ",":
                        self.eat()
                        args.append(self.expr(3))
                self.eat(")")
                node = ("call", node, args)
            else:
                return node


def _prec(n):
    k = n[0]
    if k == "grp":
        return _prec(n[1])
    return {"atom": 20, "mem": 16, "idx": 16, "call": 16, "un": 15, "cast": 15, "tern": 3}.get(k) or _BINPREC[n[1]]


def _pr(n, parent=0, right=False):
    k = n[0]
    if k == "grp":
        return _pr(n[1], parent, right)
    if k == "atom":
        s = n[1]
    elif k == "mem":
        s = "%s%s%s" % (_pr(n[2], 16), n[1], n[3])
    elif k == "idx":
        s = "%s[%s]" % (_pr(n[1], 16), _pr(n[2]))
    elif k == "call":
        s = "%s(%s)" % (_pr(n[1], 16), ", ".join(_pr(a) for a in n[2]))
    elif k == "un":
        s = n[1] + _pr(n[2], 15)
    elif k == "cast":
        s = "(%s)%s" % (n[1], _pr(n[2], 15))
    elif k == "tern":
        s = "%s ? %s : %s" % (_pr(n[1], 4), _pr(n[2], 0), _pr(n[3], 3))
    else:
        p = _BINPREC[n[1]]
        if n[1] not in ("&&", "||") and _numeric(n[2]) and _numeric(n[3]):
            # both operands constant: the front end would fold it, so in the original the left one was an inlined
            # callee's parameter that c2 replaced by the constant (`mov eax, 31; cmp eax, 60` at /Od)
            KCONST.add(1)
            s = "il2c_k(%s) %s %s" % (_pr(n[2]), n[1], _pr(n[3], p + 1))
        else:
            s = "%s %s %s" % (_pr(n[2], p), n[1], _pr(n[3], p + 1))
    own = _prec(n)
    return "(%s)" % s if own < parent else s


KCONST = set()


def _numeric(n):
    """A literal number, maybe cast or parenthesised."""
    while n[0] in ("grp", "cast"):
        n = n[1] if n[0] == "grp" else n[2]
    return n[0] == "atom" and re.match(r"^\d", n[1]) is not None


def minparens(e):
    """The expression text with only the parentheses precedence needs; unchanged when it does not parse."""
    try:
        p = _P(_tokens(e))
        n = p.expr(3)
        if p.peek() is not None:
            return e
        return _pr(n)
    except (_NoParse, RecursionError, IndexError):
        return e


def fix_line(l):
    st = l.strip()
    ind = l[:len(l) - len(l.lstrip())]
    m = re.match(r"^if \((.*)\) \{$", st)
    if m:
        return "%sif (%s) {" % (ind, minparens(m.group(1)))
    m = re.match(r"^if \((.*)\) (goto L\w+;)$", st)
    if m:
        return "%sif (%s) %s" % (ind, minparens(m.group(1)), m.group(2))
    m = re.match(r"^while \((.*)\) \{$", st)
    if m:
        return "%swhile (%s) {" % (ind, minparens(m.group(1)))
    m = re.match(r"^for \(; (.*?); (.*)\) \{$", st)
    if m:
        incs = _split_top(m.group(2), ",")
        return "%sfor (; %s; %s) {" % (ind, minparens(m.group(1)), ", ".join(fix_stmt(x) for x in incs))
    m = re.match(r"^return (.*);$", st)
    if m:
        return "%sreturn %s;" % (ind, minparens(m.group(1)))
    if st.startswith(("}", "goto", "struct", "#", "return")) or re.match(r"^L\w+:;$", st) or \
            re.match(r"^[A-Za-z_]\w*[\w *]* [A-Za-z_]\w*(\[\d+\])?;$", st):
        return l
    if st.endswith(";"):
        return ind + fix_stmt(st[:-1]) + ";"
    return l


def _split_top(s, sep):
    out, d, cur = [], 0, ""
    for ch in s:
        if ch in "([":
            d += 1
        elif ch in ")]":
            d -= 1
        if ch == sep and d == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def fix_stmt(s):
    d = 0
    for i, ch in enumerate(s):
        if ch in "([":
            d += 1
        elif ch in ")]":
            d -= 1
        elif d == 0 and s[i:i + 3] == " = " and s[i - 1:i] not in "=!<>":
            return "%s = %s" % (minparens(s[:i]), minparens(s[i + 3:]))
    return minparens(s)


BITFIELDS = set()
_BF = re.compile(r"^(\s+)(.+?) = (.+?) & (-?\d+) \| \(il2c_k\((\d+)\) & (\d+)\)(?: << (\d+))?;$")


def _bitfield(l):
    """`x = x & ~(w << s) | (il2c_k(k) & w) << s`: an inline setter of a bitfield called with a constant (cl computes
    the field before loading x; written as the mask and or, it loads x first)."""
    m = _BF.match(l)
    if not m or m.group(2) != m.group(3):
        return l
    ind, x, _x, mk, k, w, sh = m.groups()
    sh, w = int(sh or 0), int(w)
    n = w.bit_length()
    if w != (1 << n) - 1 or (~int(mk)) & 0xFFFFFFFF != (w << sh) & 0xFFFFFFFF or sh + n > 32:
        return l
    BITFIELDS.add((sh, n))
    return "%sil2c_bf_%d_%d(&(%s), %s);" % (ind, sh, n, x, k)


def od_parens(body):
    # a shift count is cl's low byte in the code, an int in the source (`(unsigned char)` there is a movzx)
    body = re.sub(r"(<<|>>) \(unsigned char\)", r"\1 ", body)
    return "\n".join(_bitfield(fix_line(l)) if l.startswith("    ") else l for l in body.split("\n"))


# ---- an inlined callee's `return x++ op k` whose result the caller drops (lift2il.md "/Od mode", postfix results):
# ---- c2 keeps the return value in an IL temporary, which /Od gives a slot below `this`'s home ----

POSTINC = {}       # helper name -> its struct declaration


def _strip(n):
    while n[0] in ("grp", "cast"):
        n = n[1] if n[0] == "grp" else n[2]
    return n


def _parse_expr(s):
    try:
        p = _P(_tokens(s))
        n = p.expr(3)
        return n if p.peek() is None else None
    except (_NoParse, RecursionError, IndexError):
        return None


def _deref(n):
    """`*(T*)P` -> (T, P) for an integral T, else None."""
    n = _strip(n)
    if n[0] != "un" or n[1] != "*":
        return None
    q = n[2]
    while q[0] == "grp":
        q = q[1]
    if q[0] != "cast" or not q[1].endswith("*"):
        return None
    ty = q[1][:-1].strip()
    if ty not in ("int", "unsigned int", "short", "unsigned short", "char", "unsigned char", "signed char"):
        return None
    return ty, q[2]


def _base_off(p):
    """P = `B + K` or `B`, B a plain variable, `this`, a constant or `il2c_k(constant)`: (B text, K)."""
    p = _strip(p)
    off = 0
    if p[0] == "bin" and p[1] == "+" and _numeric(p[3]):
        k = _strip(p[3])[1]
        off = int(k, 0)
        p = p[2]
    b = _strip(p)
    # (a member of the packed frame structure, `fb.v_l56`, is a load to the inliner: its `this` would become a
    # symbol, a named slot; Spore 004ca430 grows by 24 bytes)
    ok = (b[0] == "atom" and (re.match(r"^(?:v_l\d+|a\d+|self|this|\d+)$", b[1]))) or \
        (b[0] == "call" and b[1] == ("atom", "il2c_k") and len(b[2]) == 1 and _numeric(b[2][0]))
    return (_pr(p), off) if ok and 0 <= off < 4096 else None


def _subst(n, key, new):
    """n with each subtree that prints as `key` (casts and parentheses around it kept) replaced; (tree, count)."""
    if n[0] == "atom":
        return n, 0
    if n[0] in ("un", "cast", "grp", "mem") or n[0] in ("bin", "tern", "idx", "call"):
        if _pr(n) == key:
            return new, 1
    cnt = 0
    out = []
    for x in n:
        if isinstance(x, tuple):
            y, c = _subst(x, key, new)
            cnt += c
            out.append(y)
        elif isinstance(x, list):
            ys = []
            for z in x:
                y, c = _subst(z, key, new)
                cnt += c
                ys.append(y)
            out.append(ys)
        else:
            out.append(x)
    return tuple(out), cnt


def _leaves_const(n):
    """Every leaf of n a number or the postfix placeholder."""
    if n[0] == "atom":
        return n[1] == "x++" or n[1] == "x--" or re.match(r"^\d", n[1]) is not None
    if n[0] == "call":
        return False
    return all(_leaves_const(x) for x in n[1:] if isinstance(x, tuple))


def od_postinc(body):
    """`t = E(X); X = X + 1;` where t is stored and never read (its slot lies below `this`'s home) and E reads X once
    beside constants: an inlined member `return x++ op k;` whose result the caller drops. c2 keeps that return value in
    an IL temporary (not a symbol), and /Od allocates temporaries after the named locals and `this`'s home, in
    evaluation order with the `?:` results. Written as a call of such a helper on the object X lies in; the helper's
    `this` is X's base, which must be a variable or a constant (an expression would become the inlinee's `this`
    symbol, a named slot)."""
    lines = body.split("\n")
    for i in range(len(lines) - 1):
        m = DEFL.match(lines[i])
        if not m:
            continue
        ind, t, e = m.groups()
        if len(re.findall(r"\b%s\b" % t, body)) != 2:      # its declaration and this store
            continue
        u = re.match(r"^(\s+)(.+?) = (.+);$", lines[i + 1])
        if not u or u.group(1) != ind:
            continue
        lhs, rhs = _parse_expr(u.group(2)), _parse_expr(u.group(3))
        en = _parse_expr(e)
        if not lhs or not rhs or not en:
            continue
        d = _deref(lhs)
        if not d:
            continue
        r = _strip(rhs)
        if r[0] != "bin" or r[1] not in ("+", "-") or not _numeric(r[3]) or _strip(r[3])[1] != "1":
            continue
        key = _pr(_strip(lhs))
        if _pr(_strip(r[2])) != key:
            continue
        bo = _base_off(d[1])
        if not bo:
            continue
        en2, cnt = _subst(en, key, ("atom", "x++" if r[1] == "+" else "x--"))
        if cnt != 1 or not _leaves_const(en2):
            continue
        ty, (base, off) = d[0], bo
        ret = _pr(en2)
        import zlib
        name = "il2c_pi_%d_%s_%08x" % (off, ty.replace(" ", ""), zlib.crc32(ret.encode()))
        POSTINC[name] = "struct %s { %s%s x; __forceinline int f() { return %s; } };" % (
            name, "char pad[%d]; " % off if off else "", ty, ret)
        lines[i:i + 2] = ["%s((%s*)(%s))->f();" % (ind, name, base)]
        return od_postinc("\n".join(lines))
    return "\n".join(lines)
