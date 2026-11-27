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
    IN = [dict() for _ in range(n)]
    OUT = [dict() for _ in range(n)]
    work = list(range(n))
    inq = set(work)
    while work:
        k = work.pop(0)
        inq.discard(k)
        st = {}
        for q in pred[k]:
            for t, ds in OUT[q].items():
                st[t] = st.get(t, frozenset()) | ds
        IN[k] = st
        out = dict(st)
        if k in defline:
            out[defline[k]] = frozenset([k])
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
            ds = IN[k].get(t)
            if not ds:
                continue
            ds = list(ds)
            for d in ds:
                used.add(d)
                parent[find(d)] = find(ds[0])
            usesite[(k, t)] = ds[0]
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
            for (k, tt), d0 in usesite.items():
                if tt == t and find(d0) == root and out[k] is not None:
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
    changed = True
    while changed:
        changed = False
        occ = {}
        defs = {}
        use_line = {}
        for k, l in enumerate(lines):
            if not alive[k] or DECL.match(l):
                continue
            m = DEFL.match(l)
            for t in TMP.findall(m.group(3) if m else l):
                occ[t] = occ.get(t, 0) + 1
                use_line[t] = k
            if m:
                defs[m.group(2)] = defs.get(m.group(2), 0) + 1
        for i in range(n):
            if not alive[i]:
                continue
            m = DEFL.match(lines[i])
            if not m:
                continue
            t, e = m.group(2), m.group(3)
            if defs.get(t) == 1 and occ.get(t, 0) > 1 and t not in e and LEAF.match(e):
                if fold_leaf(lines, alive, i, t, e, addr_taken):
                    changed = True
                    break
                continue
            if defs.get(t) != 1 or occ.get(t) != 1 or t in e:
                continue
            j = use_line[t]
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
            lines[j] = new
            alive[i] = False
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
                if e < 0 or not GOTO.match(lines[e - 1]) or GOTO.match(lines[e - 1]).group(1) != Lab or refs(Lab) != 1:
                    continue
                ind = im.group(1)
                new = ["%sfor (; %s; %s) {" % (ind, im.group(2), ", ".join(inc))] + lines[f + 1:e - 1] + [lines[e]]
                lines[i:e + 1] = new
                changed = True
                break
            if ml and i + 1 < len(lines):
                im = IFL.match(lines[i + 1])
                if not im:
                    continue
                e = _close(lines, i + 1)
                if e < 0 or not GOTO.match(lines[e - 1]) or GOTO.match(lines[e - 1]).group(1) != ml.group(1) or \
                        refs(ml.group(1)) != 1:
                    continue
                new = ["%swhile (%s) {" % (im.group(1), im.group(2))] + lines[i + 2:e - 1] + [lines[e]]
                lines[i:e + 1] = new
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
            g = GOTO.match(lines[e - 1])
            if not g or lines[i + 1:e].count(lines[e - 1]) != 1:
                continue
            di = depth_of(lines, i)
            if any(lines[k].strip().startswith("} else") and depth_of(lines, k) == di for k in range(i + 1, e)):
                continue                      # the if already has an else arm
            L = g.group(1)
            if sum(1 for x in lines if re.search(r"\bgoto %s;" % L, x)) != 1:
                continue
            lab = next((k for k in range(e + 1, len(lines)) if lines[k].strip() == "%s:;" % L), None)
            if lab is None or lab == e + 1:
                continue
            seg = lines[e + 1:lab]
            if any(LABEL.match(x) for x in seg) or sum(x.count("{") - x.count("}") for x in seg) != 0:
                continue
            if depth_of(lines, i) != depth_of(lines, lab):
                continue
            ind = a.group(1)
            new = lines[i:e - 1] + [ind + "} else {"] + ["    " + x for x in seg] + [ind + "}"]
            lines[i:lab + 1] = new
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
        s = "%s %s %s" % (_pr(n[2], p), n[1], _pr(n[3], p + 1))
    own = _prec(n)
    return "(%s)" % s if own < parent else s


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


def od_parens(body):
    return "\n".join(fix_line(l) if l.startswith("    ") else l for l in body.split("\n"))
