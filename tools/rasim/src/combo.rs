//! Combined what-ifs: a source-level edit (a reference more or fewer in a block, a longer or shorter live range)
//! is carried through both models. The edit changes priorities (main.rs, regalloc.md 3.2), hence the order in
//! which Color pops the globregs; it changes benefits (the 100 x benefit weight) and which ranges overlap; and the
//! register model (choose.rs, regalloc.md 4) is then run over that new order and state.
//!
//!   rasim regs SNAP [CHANGE|EDIT...]            every register after the changes and edits
//!   rasim need SNAP reg:VAR=REG... [CHANGE|EDIT...]   single changes, then pairs, that give every VAR its REG
//!   rasim want SNAP REGMAP.txt [CHANGE|EDIT...] the same with the targets read from `c2trace.py regmap` output
//!
//! What a source edit does in the model (blocks are numbered as in the first int Spillcosts pass, `regs --why`):
//!   ref:V:B     cost +2 in B: prio += 2*P_B*w_B (and the live-through penalty goes), benefit += 2*w_B
//!   unref:V:B   the references in B go, V still lives through B: P_B -= 1 for everyone, benefit -= cost*w_B
//!   drop:V:B    V's range no longer reaches B (B must be an end of the range): as unref without the penalty;
//!               V stops interfering with the values it met only in B
//!   extend:V:B  V's range grows to reach B, with a reference there and the blocks in between lived through:
//!               penalties for those blocks; V interferes with every value present in a block it now lives through
//!               or live across a block boundary it now crosses; V loses the registers killed in those blocks
//!               (estimated: the registers no value spanning such a block may use)
//! Then: the colouring order is recomputed from the priorities (round by round: a split round still starts after
//! the pop that started it in the trace), and the register model runs free over it.
//!
//! Not modelled, and said in the output where it applies: priorities of split pieces (later Spillcosts passes number
//! their blocks differently, so an edit is applied to the first pass only); new splits; a new variable's own
//! register (`var:` only raises P); preferences created by the edit (give `pref:` / `copy:` as well).

use super::*;
use choose::{rname, rnum, Change, Item, Model, Mods, Outcome, ORDER};

/// A source edit resolved to a globreg of the first int pass (index into Uni::base.vars).
#[derive(Clone, Debug)]
pub enum Src {
    Ref(usize, u32, i64),
    Unref(usize, u32),
    Drop(usize, u32),
    Extend(usize, u32),
    NewVar(String, Vec<(u32, i64)>),
}

pub struct Uni {
    base: State, // every int-class globreg of every pass, each with its own pass's block table
    p1: u32,
    web_of: Vec<Option<usize>>,
    rounds: Vec<u32>,
    clusters: Vec<Vec<usize>>,     // per round: the Spillcosts results that precede its pops
    lead: Vec<Vec<usize>>,         // per round: pops that cannot be placed by priority and precede every other
    ev_of: Vec<Option<Vec<usize>>>, // per var: its first pop (and the re-pops that followed it in the trace)
    trigger: Vec<Option<usize>>,   // per round: the var whose pop was the round's last
    synth: Vec<bool>,              // never popped in the trace, but it has an avail set: can be coloured if it outranks
    gone_after: Vec<Option<u32>>,  // split away in this round
    sup: Vec<Option<u32>>,         // record costed again in this pass
    tail: Vec<usize>,
    pub exact: Result<(), String>,
}

fn range(v: &Var) -> Option<(u32, u32)> {
    let mut k = v.cost.keys().chain(v.live.iter()).copied();
    let f = k.next()?;
    Some(k.fold((f, f), |(lo, hi), b| (lo.min(b), hi.max(b))))
}

fn present(v: &Var, b: u32) -> bool {
    v.cost.contains_key(&b) || v.live.contains(&b)
}

impl Uni {
    pub fn build(s: &Snap, m: &Model) -> Uni {
        let d = defs(s);
        let mut passes: Vec<&SPass> = s.passes.iter().filter(|p| p.cls == Some(0)).collect();
        passes.sort_by_key(|p| p.n);
        let p1 = passes.first().map_or(0, |p| p.n);
        let mut vars = vec![];
        for p in &passes {
            let one = State::from_pass(p, &d);
            for mut v in one.vars {
                v.own = Some(one.blocks.clone());
                vars.push(v);
            }
        }
        let mut base = State { blocks: BTreeMap::new(), vars };
        mark_removed(s, &mut base);
        set_nums(s, &mut base);
        let nv = base.vars.len();
        let mut wmap: HashMap<(String, u32), usize> = HashMap::new();
        for (i, w) in m.webs.iter().enumerate() {
            let k = (w.gr.clone(), w.pass);
            match wmap.get(&k) {
                Some(&j) if m.webs[j].pop.is_some() || !m.init_seen[i] => {}
                _ => {
                    wmap.insert(k, i);
                }
            }
        }
        let vidx: HashMap<(String, u32), usize> =
            base.vars.iter().enumerate().map(|(i, v)| ((v.id.clone(), v.pass), i)).collect();
        let web_of: Vec<Option<usize>> = base.vars.iter().map(|v| wmap.get(&(v.id.clone(), v.pass)).copied()).collect();
        let gone_after: Vec<Option<u32>> = base.vars.iter().map(|v| split_round(s, &v.id, v.pass)).collect();
        let sup: Vec<Option<u32>> = base
            .vars
            .iter()
            .map(|v| passes.iter().filter(|p| p.n > v.pass && p.grs.contains_key(&v.id)).map(|p| p.n).min())
            .collect();
        // re-pops: the same record (id, costing pass) popped again
        let mut first_pop: BTreeSet<(String, u32)> = BTreeSet::new();
        let repop: Vec<bool> = s
            .pops
            .iter()
            .map(|q| !first_pop.insert((q.gr.clone(), costed_at(s, &q.gr, q.npass).unwrap_or(0))))
            .collect();
        let mut u = Uni {
            base,
            p1,
            web_of,
            rounds: vec![],
            clusters: vec![],
            lead: vec![],
            ev_of: vec![None; nv],
            trigger: vec![],
            synth: vec![false; nv],
            gone_after,
            sup,
            tail: vec![],
            exact: Ok(()),
        };
        let mut pending: Vec<usize> = vec![];
        let mut pre: Vec<usize> = vec![];
        let mut last_sortable: Option<usize> = None;
        for (i, it) in m.items.iter().enumerate() {
            match it {
                Item::Restrict { .. } => pre.push(i),
                Item::Pop { pi, .. } => {
                    let pop = &s.pops[*pi];
                    if u.rounds.last() != Some(&pop.npass) {
                        u.rounds.push(pop.npass);
                        u.clusters.push(std::mem::take(&mut pending));
                        u.lead.push(vec![]);
                        u.trigger.push(None);
                        last_sortable = None;
                    }
                    let r = u.rounds.len() - 1;
                    u.clusters[r].append(&mut pending); // seen mid-round: state the round started with
                    let var = costed_at(s, &pop.gr, pop.npass).and_then(|h| vidx.get(&(pop.gr.clone(), h)).copied());
                    let mut mine = std::mem::take(&mut pre);
                    mine.push(i);
                    match var {
                        Some(v) if !repop[*pi] && u.ev_of[v].is_none() => {
                            u.ev_of[v] = Some(mine);
                            u.trigger[r] = Some(v);
                            last_sortable = Some(v);
                        }
                        _ => match last_sortable {
                            Some(v) => u.ev_of[v].as_mut().unwrap().append(&mut mine),
                            None => u.lead[r].append(&mut mine),
                        },
                    }
                }
                _ => pending.push(i),
            }
        }
        u.tail = pending;
        if let Some(t) = u.trigger.last_mut() {
            *t = None; // the last round ends when the list is empty
        }
        if u.rounds.is_empty() {
            u.exact = Err("no int-class pops in this snapshot".into());
            return u;
        }
        for v in 0..nv {
            u.synth[v] = u.ev_of[v].is_none()
                && !u.base.vars[v].removed
                && u.web_of[v].map_or(false, |w| m.init_seen[w] && m.webs[w].pop.is_none());
        }
        // Calibration: with no edit the computed order must be the traced one. A never-popped web that the model
        // would pop (its defOrd is not in the trace, so ties are unknown) is taken out of the candidates.
        let natural: Vec<usize> =
            (0..m.items.len()).filter(|&i| matches!(&m.items[i], Item::Pop { traced: Some(_), .. })).collect();
        loop {
            let o = u.steps(m, &u.base);
            if let Some(&x) = o.iter().find(|&&i| i >= m.items.len()) {
                let w = x - m.items.len();
                let v = (0..nv).find(|&v| u.synth[v] && u.web_of[v] == Some(w)).unwrap();
                u.synth[v] = false;
                continue;
            }
            let got: Vec<usize> =
                o.iter().copied().filter(|&i| matches!(&m.items[i], Item::Pop { traced: Some(_), .. })).collect();
            if got != natural {
                let k = got.iter().zip(&natural).position(|(a, b)| a != b).unwrap_or(0);
                let nm = |i: Option<&usize>| match i.map(|&i| &m.items[i]) {
                    Some(Item::Pop { web, .. }) => m.label(*web),
                    _ => "?".into(),
                };
                u.exact = Err(format!(
                    "the priority model does not reproduce this trace's colouring order (colouring {}: model {}, trace {}; \
                     see `rasim check`), so source-level edits are not simulated",
                    k + 1,
                    nm(got.get(k)),
                    nm(natural.get(k))
                ));
            } else {
                let md = Mods { order: Some(o), ..Default::default() };
                match m.run(&md) {
                    Ok(out) => {
                        if (0..m.webs.len()).any(|i| m.webs[i].traced.is_some() && out.colour[i] != m.webs[i].traced) {
                            u.exact = Err("the register model does not reproduce this trace (see `rasim check`), so \
                                           source-level edits are not simulated"
                                .into());
                        }
                    }
                    Err(e) => u.exact = Err(format!("the register model fails on the unedited trace: {}", e)),
                }
            }
            break;
        }
        u
    }

    fn alive(&self, v: usize, k: u32) -> bool {
        self.gone_after[v].map_or(true, |r| r >= k) && self.sup[v].map_or(true, |q| q > k)
    }

    /// The step order Color would follow with these priorities: per round, the list is popped in priority order
    /// until the pop that ended the round in the trace; then that round's Spillcosts results come in.
    fn steps(&self, m: &Model, st: &State) -> Vec<usize> {
        let ord: Vec<usize> = st.order().iter().map(|x| x.0).collect();
        let nv = st.vars.len().min(self.ev_of.len());
        let mut popped = vec![false; st.vars.len()];
        let nr = self.rounds.len();
        let mut out: Vec<usize> = vec![];
        let mut r = 0;
        out.extend(&self.clusters[0]);
        out.extend(&self.lead[0]);
        loop {
            let k = self.rounds[r];
            let next = ord.iter().copied().find(|&v| {
                v < nv
                    && !popped[v]
                    && (self.ev_of[v].is_some() || self.synth[v])
                    && st.vars[v].pass <= k
                    && self.alive(v, k)
            });
            match next {
                None => {
                    if r + 1 >= nr {
                        break;
                    }
                }
                Some(v) => {
                    popped[v] = true;
                    match &self.ev_of[v] {
                        Some(items) => out.extend(items),
                        None => out.push(m.items.len() + self.web_of[v].unwrap()),
                    }
                    if !(r + 1 < nr && self.trigger[r].map_or(true, |t| popped[t])) {
                        continue;
                    }
                }
            }
            loop {
                r += 1;
                out.extend(&self.clusters[r]);
                out.extend(&self.lead[r]);
                if !(r + 1 < nr && self.trigger[r].map_or(true, |t| popped[t])) {
                    break;
                }
            }
        }
        // anything the rounds did not reach (a pop whose turn never came): as traced, at the end
        let have: BTreeSet<usize> = out.iter().copied().collect();
        for v in 0..nv {
            if let Some(items) = &self.ev_of[v] {
                out.extend(items.iter().filter(|i| !have.contains(i)));
            }
        }
        out.extend(&self.tail);
        out
    }

    fn vlabel(&self, v: usize) -> String {
        let x = &self.base.vars[v];
        let same = self.base.vars.iter().filter(|y| y.pass == self.p1 && y.name == x.name && !y.removed).count();
        if same > 1 {
            format!("{}#{}", x.name, x.id)
        } else {
            x.name.clone()
        }
    }

    pub fn label(&self, e: &Src) -> String {
        match e {
            Src::Ref(v, b, 2) => format!("ref:{}:{}", self.vlabel(*v), b),
            Src::Ref(v, b, c) => format!("ref:{}:{}:{}", self.vlabel(*v), b, c),
            Src::Unref(v, b) => format!("unref:{}:{}", self.vlabel(*v), b),
            Src::Drop(v, b) => format!("drop:{}:{}", self.vlabel(*v), b),
            Src::Extend(v, b) => format!("extend:{}:{}", self.vlabel(*v), b),
            Src::NewVar(n, r) => {
                format!("var:{}:{}", n, r.iter().map(|(b, c)| format!("{}:{}", b, c)).collect::<Vec<_>>().join(","))
            }
        }
    }

    /// A first-pass globreg by name (`name#grptr` to pick one of several); with a block, the one present there.
    fn var(&self, sel: &str, b: Option<u32>) -> Result<usize, String> {
        let (name, id) = match sel.split_once('#') {
            Some((n, g)) => (n, Some(g)),
            None => (sel.split("@p").next().unwrap_or(sel), None),
        };
        let all: Vec<usize> = (0..self.base.vars.len())
            .filter(|&i| {
                let v = &self.base.vars[i];
                v.pass == self.p1 && v.name == name && id.map_or(true, |g| v.id == g)
            })
            .collect();
        let mut hits: Vec<usize> = all.iter().copied().filter(|&i| !self.base.vars[i].removed).collect();
        if hits.is_empty() {
            hits = all;
        }
        if hits.len() > 1 {
            if let Some(b) = b {
                let here: Vec<usize> = hits.iter().copied().filter(|&i| present(&self.base.vars[i], b)).collect();
                if here.len() == 1 {
                    return Ok(here[0]);
                }
            }
        }
        match hits.len() {
            0 => Err(format!(
                "no globreg named {} in Spillcosts pass {} (source edits apply to the first int pass; pieces made by \
                 later split rounds cannot be edited)",
                sel, self.p1
            )),
            1 => Ok(hits[0]),
            _ => Err(format!(
                "{} names {} globregs; use {}",
                sel,
                hits.len(),
                hits.iter().map(|&i| format!("{}#{}", name, self.base.vars[i].id)).collect::<Vec<_>>().join(" or ")
            )),
        }
    }

    pub fn resolve(&self, e: &Edit) -> Result<Src, String> {
        match e {
            Edit::Ref(v, b, c) => Ok(Src::Ref(self.var(v, Some(*b))?, *b, *c)),
            Edit::Unref(v, b, true) => Ok(Src::Unref(self.var(v, Some(*b))?, *b)),
            Edit::Unref(v, b, false) => Ok(Src::Drop(self.var(v, Some(*b))?, *b)),
            Edit::Extend(v, b) => Ok(Src::Extend(self.var(v, None)?, *b)),
            Edit::NewVar(n, r) => Ok(Src::NewVar(n.clone(), r.clone())),
            Edit::W(..) | Edit::P(..) => Err("w: and P: overrides are for `order` / `need A>B` only".into()),
        }
    }

    fn bump(&self, st: &mut State, b: u32, dp: i64) {
        for v in st.vars.iter_mut().filter(|v| v.pass == self.p1) {
            if let Some(t) = v.own.as_mut() {
                t.entry(b).or_insert((1, 0)).1 += dp;
            }
        }
    }

    /// Registers killed in block k (by a call), estimated [I] from the other values of the first pass:
    ///   - if some value lives through k without a reference, the registers none of those may use (they are live
    ///     across everything in the block);
    ///   - otherwise, which of eax ecx edx a value lacks whose range starts before k and ends after it (it may
    ///     lack them because of another block: an over-estimate).
    /// 0 when no value spans the block.
    fn kills(&self, m: &Model, v: usize, k: u32) -> u32 {
        let mut through: Option<u32> = None;
        let mut around = 0u32;
        for u in 0..self.base.vars.len() {
            let y = &self.base.vars[u];
            let spans = range(y).map_or(false, |(a, z)| a < k && k < z) && present(y, k);
            if u == v || y.pass != self.p1 || !spans {
                continue;
            }
            if let Some(av) = self.web_of[u].and_then(|w| init_avail(m, w)) {
                if y.live.contains(&k) {
                    through = Some(through.unwrap_or(0x1de) & !av & 0x1de);
                } else {
                    around |= !av & 0xe;
                }
            }
        }
        through.unwrap_or(around)
    }

    fn weight(&self, st: &State, v: usize, b: u32) -> i64 {
        st.vars[v].own.as_ref().and_then(|t| t.get(&b)).map_or(1, |x| x.0)
    }

    /// Apply one edit to the priority state and collect what it means for the register model.
    fn apply(&self, m: &Model, e: &Src, st: &mut State, md: &mut Mods, desc: &mut Vec<String>) -> Result<(), String> {
        let v = match e {
            Src::Ref(v, ..) | Src::Unref(v, ..) | Src::Drop(v, ..) | Src::Extend(v, ..) => *v,
            Src::NewVar(n, refs) => {
                for (b, _) in refs {
                    self.bump(st, *b, 1);
                }
                desc.push(format!("{}: P += 1 in its blocks; its own register and overlaps are not modelled", n));
                return Ok(());
            }
        };
        let name = self.vlabel(v);
        let web = self.web_of[v];
        let p0 = st.prio(&st.vars[v]);
        let ben = |md: &mut Mods, d: i64| {
            if let Some(w) = web {
                md.ben.push((w, d));
            }
        };
        match e {
            Src::Ref(_, b, c) => {
                if !present(&st.vars[v], *b) {
                    return Err(format!("{} is not live in block {}; use extend:{}:{}", name, b, name, b));
                }
                let had = st.vars[v].cost.contains_key(b);
                *st.vars[v].cost.entry(*b).or_insert(0) += c;
                st.vars[v].live.remove(b);
                if !had {
                    self.bump(st, *b, 1);
                }
                let w = self.weight(st, v, *b);
                ben(md, c * w);
            }
            Src::Unref(_, b) => {
                let Some(c0) = st.vars[v].cost.remove(b) else {
                    return Err(format!("{} has no reference in block {}", name, b));
                };
                self.bump(st, *b, -1);
                st.vars[v].live.insert(*b);
                let w = self.weight(st, v, *b);
                ben(md, -c0 * w);
            }
            Src::Drop(_, b) => {
                let Some((lo, hi)) = range(&st.vars[v]) else { return Err(format!("{} has no blocks", name)) };
                if *b != lo && *b != hi {
                    return Err(format!("block {} is inside {}'s range {}..{}; only an end can be dropped", b, name, lo, hi));
                }
                if st.vars[v].cost.keys().all(|k| k == b) {
                    return Err(format!("block {} holds {}'s only references", b, name));
                }
                // the range now ends at the nearest block that still has a reference: the blocks lived through
                // on the way go too
                let mut gone = vec![*b];
                let mut k = *b;
                loop {
                    k = if *b == lo { k + 1 } else { k - 1 };
                    if st.vars[v].cost.contains_key(&k) || k == 0 || k > hi {
                        break;
                    }
                    if st.vars[v].live.contains(&k) {
                        gone.push(k);
                    }
                }
                let mut back = 0u32;
                for k in &gone {
                    if let Some(c0) = st.vars[v].cost.remove(k) {
                        self.bump(st, *k, -1);
                        let w = self.weight(st, v, *k);
                        ben(md, -c0 * w);
                    } else {
                        back |= self.kills(m, v, *k);
                    }
                    st.vars[v].live.remove(k);
                }
                // registers killed in the blocks it lived through come back unless a block it still spans kills them
                // [I: the end blocks of the remaining range are taken as not killing them]
                if let (Some(wv), Some((nlo, nhi))) = (web, range(&st.vars[v])) {
                    for k in nlo + 1..nhi {
                        back &= !self.kills(m, v, k);
                    }
                    back &= !init_avail(m, wv).unwrap_or(0);
                    if back != 0 {
                        md.gives.push((wv, back));
                    }
                }
                let mut lost = vec![];
                for u in 0..st.vars.len() {
                    let y = &st.vars[u];
                    if u == v || y.pass != self.p1 || !gone.iter().any(|k| present(y, *k)) {
                        continue;
                    }
                    let x = &st.vars[v];
                    let shared = y.cost.keys().chain(y.live.iter()).any(|k| present(x, *k));
                    if let (false, Some(wv), Some(wu)) = (shared, web, self.web_of[u]) {
                        md.del_e.push((wv, wu));
                        lost.push(m.label(wu));
                    }
                }
                let (nlo, nhi) = range(&st.vars[v]).unwrap_or((lo, hi));
                desc.push(format!(
                    "{} range {}..{} -> {}..{}{}{}",
                    name,
                    lo,
                    hi,
                    nlo,
                    nhi,
                    if lost.is_empty() { String::new() } else { format!(", no longer overlaps {}", lost.join(" ")) },
                    if back != 0 { format!(", gets back {}", regs_of(back)) } else { String::new() }
                ));
            }
            Src::Extend(_, b) => {
                let Some((lo, hi)) = range(&st.vars[v]) else { return Err(format!("{} has no blocks", name)) };
                if (lo..=hi).contains(b) {
                    return Err(format!("{} already reaches block {} (range {}..{}); use ref:", name, b, lo, hi));
                }
                let Some(wv) = web else { return Err(format!("{} has no avail set in the trace", name)) };
                let known = |k: &u32| st.vars[v].own.as_ref().map_or(false, |t| t.contains_key(k));
                if !known(b) {
                    return Err(format!("no block {} in Spillcosts pass {}", b, self.p1));
                }
                let between: Vec<u32> = if *b < lo { (*b + 1..lo).collect() } else { (hi + 1..*b).collect() };
                let chain: Vec<u32> = if *b < lo { (*b..=lo).collect() } else { (hi..=*b).collect() };
                let mut over: Vec<usize> = vec![];
                let mut kills = 0u32;
                for u in 0..self.base.vars.len() {
                    let y = &self.base.vars[u];
                    if u == v || y.pass != self.p1 {
                        continue;
                    }
                    let meets = between.iter().any(|k| present(y, *k))
                        || chain.windows(2).any(|p| present(y, p[0]) && present(y, p[1]));
                    if let (true, Some(wu)) = (meets, self.web_of[u]) {
                        if m.init_seen[wu] && !over.contains(&wu) {
                            over.push(wu);
                        }
                    }
                }
                for k in &between {
                    kills |= self.kills(m, v, *k);
                }
                kills &= init_avail(m, wv).unwrap_or(0);
                for k in between {
                    st.vars[v].live.insert(k);
                }
                st.vars[v].cost.insert(*b, 2);
                self.bump(st, *b, 1);
                let w = self.weight(st, v, *b);
                ben(md, 2 * w);
                let fresh: Vec<String> = over
                    .iter()
                    .filter(|&&y| !(m.adj[wv].contains(&y) || m.adj[y].contains(&wv)))
                    .map(|&y| m.label(y))
                    .collect();
                desc.push(format!(
                    "{} range {}..{} -> {}..{}{}{}",
                    name,
                    lo,
                    hi,
                    lo.min(*b),
                    hi.max(*b),
                    if fresh.is_empty() { String::new() } else { format!(", now overlaps {}", fresh.join(" ")) },
                    if kills != 0 { format!(", loses {}", regs_of(kills)) } else { String::new() }
                ));
                for &y in &over {
                    md.add_e.push((wv, y));
                }
                if kills != 0 {
                    md.kills.push((wv, kills));
                }
                md.ext.push((wv, over));
            }
            Src::NewVar(..) => unreachable!(),
        }
        let p1 = st.prio(&st.vars[v]);
        if p1 != p0 {
            desc.push(format!("{} prio {} -> {}", name, p0, p1));
        }
        Ok(())
    }
}

/// A source variable (a parameter, a local, `this`): not an immediate candidate (`imm0`) or a compiler temporary
/// (`t343`), which no source edit addresses directly. Those can still be named in an explicit EDIT.
fn is_user(name: &str) -> bool {
    let digits = |p: &str| name.strip_prefix(p).map_or(false, |r| !r.is_empty() && r.chars().all(|c| c.is_ascii_digit()));
    !(digits("imm") || digits("t") || name == "?")
}

fn regs_of(mask: u32) -> String {
    ORDER.iter().filter(|&&r| mask >> r & 1 == 1).map(|&r| rname(r)).collect::<Vec<_>>().join(" ")
}

fn init_avail(m: &Model, w: usize) -> Option<u32> {
    m.items.iter().find_map(|it| match it {
        Item::Init { web, avail, .. } if *web == w => Some(*avail),
        _ => None,
    })
}

#[derive(Clone)]
pub enum Cand {
    Abs(Change),
    Src(Src),
}

pub struct Ctx<'a> {
    s: &'a Snap,
    pub m: Model,
    pub uni: Uni,
}

pub struct Eval {
    pub out: Outcome,
    pub desc: Vec<String>,
}

impl<'a> Ctx<'a> {
    pub fn new(s: &'a Snap) -> Ctx<'a> {
        let m = Model::build(s).unwrap_or_else(|e| die(&e));
        let uni = Uni::build(s, &m);
        Ctx { s, m, uni }
    }

    pub fn label(&self, c: &Cand) -> String {
        match c {
            Cand::Abs(c) => c.to_string(),
            Cand::Src(e) => self.uni.label(e),
        }
    }

    /// Parse a command-line change or edit.
    pub fn parse(&self, a: &str) -> Result<Cand, String> {
        if choose::is_change(a) {
            Ok(Cand::Abs(choose::parse_change(a)?))
        } else {
            self.uni.exact.clone()?;
            Ok(Cand::Src(self.uni.resolve(&parse_edit(a)?)?))
        }
    }

    pub fn eval(&self, cs: &[&Cand]) -> Result<Eval, String> {
        let abs: Vec<Change> = cs.iter().filter_map(|c| if let Cand::Abs(c) = c { Some(c.clone()) } else { None }).collect();
        let mut md = self.m.resolve(&abs)?;
        let mut desc = vec![];
        let srcs: Vec<&Src> = cs.iter().filter_map(|c| if let Cand::Src(e) = c { Some(e) } else { None }).collect();
        if !srcs.is_empty() {
            self.uni.exact.clone()?;
            let mut st = self.uni.base.clone();
            for e in srcs {
                self.uni.apply(&self.m, e, &mut st, &mut md, &mut desc)?;
            }
            md.order = Some(self.uni.steps(&self.m, &st));
        }
        Ok(Eval { out: self.m.run(&md)?, desc })
    }

    fn side(&self, base: &Outcome, o: &Outcome, tg: &[(usize, u32)]) -> Vec<String> {
        (0..self.m.webs.len())
            .filter(|&w| (base.colour[w].is_some() || o.colour[w].is_some()) && o.colour[w] != base.colour[w])
            .filter(|w| !tg.iter().any(|t| t.0 == *w))
            .map(|w| {
                format!("{} {}->{}", self.m.label(w), base.colour[w].map_or("-", rname), o.colour[w].map_or("-", rname))
            })
            .collect()
    }
}

fn uniq(v: &[String]) -> Vec<String> {
    let mut seen = BTreeSet::new();
    v.iter().filter(|l| seen.insert((*l).clone())).cloned().collect()
}

/// `rasim regs SNAP [CHANGE|EDIT...]`: the model's register for every colouring, after the changes.
pub fn cmd_regs(s: &Snap, args: &[String]) {
    let cx = Ctx::new(s);
    let given: Vec<Cand> = args.iter().map(|a| cx.parse(a).unwrap_or_else(|e| die(&e))).collect();
    let base = cx.eval(&[]).unwrap_or_else(|e| die(&e));
    let refs: Vec<&Cand> = given.iter().collect();
    match cx.eval(&refs) {
        Ok(ev) => {
            for d in &ev.desc {
                println!("edit: {}", d);
            }
            choose::show(&cx.m, &ev.out, if given.is_empty() { None } else { Some(&base.out) });
            for l in uniq(&ev.out.inferred) {
                println!("  [I] {}", l);
            }
            if !given.is_empty() {
                let side = cx.side(&base.out, &ev.out, &[]);
                println!(
                    "{} of {} registers change{}{}",
                    side.len(),
                    base.out.pops.len(),
                    if side.is_empty() { "" } else { ": " },
                    side.join(", ")
                );
            }
        }
        Err(e) => println!("not decidable: {}", e),
    }
}

struct Res {
    kind: u8,
    cost: i64,
    label: String,
    unmet: usize,
    pen: i64,
    changed: bool,
    side: Vec<String>,
    ninf: usize,
    desc: Vec<String>,
    notes: Vec<String>,
    miss: Vec<String>, // targets still unmet
}

const KINDS: [&str; 6] = [
    "order (colour A just before/after B; cost = priority gap to close, see `rasim need SNAP 'A>B'`)",
    "interference added or removed between two values",
    "preference added or removed (pref:V:REG = one more copy between V and that register, e.g. a call result in eax, a thiscall/fastcall argument)",
    "copy between two values (the one coloured second gets a preference for the first one's register)",
    "source-level edits to a variable (a reference more or fewer in a block, a longer or shorter live range; priority, order, benefit and overlaps all follow)",
    "register excluded or allowed (noreg:V:REG = V lives across something that overwrites REG: a call for eax ecx edx, a shift count for ecx, a division for eax edx, an argument loaded into ecx or edx; canreg: it no longer does)",
];

pub struct Opts {
    pub top: usize,
    pub pool: usize,
    pub depth: usize,
}

/// Single changes of every kind, then pairs (and triples) of the most promising ones, that give every target its
/// register. `keep` are registers that must not move (targets that already hold).
fn search(cx: &Ctx, tg: &[(usize, u32)], given: &[Cand], opts: &Opts) {
    let m = &cx.m;
    let g: Vec<&Cand> = given.iter().collect();
    let base = match cx.eval(&g) {
        Ok(e) => e,
        Err(e) => die(&format!("not decidable with the given changes: {}", e)),
    };
    for c in given {
        println!("given {}", cx.label(c));
    }
    for d in &base.desc {
        println!("  edit: {}", d);
    }
    let base = base.out;
    let unmet = |o: &Outcome| tg.iter().filter(|&&(w, r)| o.colour[w] != Some(r)).count();
    // how far a miss is: the wanted register is not available (2), or available but outweighed / later in order (1)
    let pen = |o: &Outcome| -> i64 {
        tg.iter()
            .map(|&(w, r)| match (o.colour[w] == Some(r), o.pops.iter().find(|p| p.web == w)) {
                (true, _) => 0,
                (false, Some(p)) if p.avail >> r & 1 == 1 => 1,
                _ => 2,
            })
            .sum()
    };
    let shown = tg.len() <= 12;
    for &(w, r) in tg {
        let p = base.pops.iter().find(|p| p.web == w);
        let holds = base.colour[w] == Some(r);
        if !shown && holds {
            continue;
        }
        println!(
            "{} (pop {}, prio {}): now {}, wanted {}; avail when coloured: {}{}",
            m.label(w),
            m.webs[w].pop.unwrap_or(0),
            m.webs[w].prio,
            base.colour[w].map_or("-", rname),
            rname(r),
            p.map_or("?".into(), |p| ORDER
                .iter()
                .filter(|&&q| p.avail >> q & 1 == 1)
                .map(|&q| format!("{}:{}", rname(q), p.w[q as usize]))
                .collect::<Vec<_>>()
                .join(" ")),
            if holds { "  (holds)" } else { "" }
        );
    }
    let u0 = unmet(&base);
    if !shown {
        println!("{} of {} wanted registers hold now", tg.len() - u0, tg.len());
    }
    if u0 == 0 {
        println!("every target holds");
        return;
    }
    let col: Vec<usize> = base.pops.iter().map(|p| p.web).collect(); // coloured webs, in colouring order
    let mut cands: Vec<(u8, i64, Cand)> = vec![];
    for (i, &a) in col.iter().enumerate() {
        for (j, &b) in col.iter().enumerate().skip(i + 1) {
            // a is coloured before b. Order changes: b first (two ways to say it), cost = the priority gap
            let gap = (m.webs[a].prio - m.webs[b].prio).abs();
            cands.push((0, gap, Cand::Abs(Change::Before(m.label(b), m.label(a)))));
            if j > i + 1 {
                cands.push((0, gap, Cand::Abs(Change::After(m.label(a), m.label(b)))));
            }
            if m.adj[a].contains(&b) || m.adj[b].contains(&a) {
                cands.push((1, 3, Cand::Abs(Change::NoInterf(m.label(a), m.label(b)))));
            } else {
                cands.push((1, 3, Cand::Abs(Change::Interf(m.label(a), m.label(b)))));
            }
            if !(m.padj[a].contains(&b) || m.padj[b].contains(&a)) {
                cands.push((3, 2, Cand::Abs(Change::Copy(m.label(a), m.label(b)))));
            } else {
                cands.push((3, 2, Cand::Abs(Change::NoCopy(m.label(a), m.label(b)))));
            }
        }
        for &r in &ORDER {
            cands.push((2, 2, Cand::Abs(Change::Pref(m.label(a), r, 1))));
            match init_avail(m, a) {
                Some(av) if av >> r & 1 == 1 => cands.push((5, 2, Cand::Abs(Change::NoReg(m.label(a), r)))),
                Some(_) => cands.push((5, 2, Cand::Abs(Change::CanReg(m.label(a), r)))),
                None => {}
            }
        }
    }
    // a preference that exists can also be taken away (one copy with a physical register fewer)
    let mut phys: BTreeSet<(usize, u32)> = BTreeSet::new();
    for it in &m.items {
        if let Item::Pref { web, reg, src: None, .. } = it {
            if m.webs[*web].pop.is_some() {
                phys.insert((*web, *reg));
            }
        }
    }
    for (a, r) in phys {
        cands.push((2, 2, Cand::Abs(Change::Pref(m.label(a), r, -1))));
    }
    // source-level edits on every first-pass globreg that has a web
    let uni = &cx.uni;
    if uni.exact.is_ok() {
        let blocks: Vec<u32> =
            uni.base.vars.iter().find(|v| v.pass == uni.p1).and_then(|v| v.own.as_ref()).map_or(vec![], |t| t.keys().copied().collect());
        for v in 0..uni.base.vars.len() {
            let x = &uni.base.vars[v];
            if x.pass != uni.p1 || x.removed || uni.web_of[v].map_or(true, |w| !m.init_seen[w]) || !is_user(&x.name) {
                continue;
            }
            let Some((lo, hi)) = range(x) else { continue };
            for &b in &blocks {
                if present(x, b) {
                    cands.push((4, 1, Cand::Src(Src::Ref(v, b, 2))));
                    cands.push((4, 2, Cand::Src(Src::Ref(v, b, 4)))); // two more references
                    if x.cost.contains_key(&b) && x.cost.len() > 1 {
                        cands.push((4, 1, Cand::Src(Src::Unref(v, b))));
                        if (b == lo || b == hi) && lo != hi {
                            cands.push((4, 2, Cand::Src(Src::Drop(v, b))));
                        }
                    }
                } else if b < lo || b > hi {
                    let far = if b < lo { lo - b } else { b - hi } as i64;
                    cands.push((4, 2 + far, Cand::Src(Src::Extend(v, b))));
                }
            }
        }
    } else if let Err(e) = &uni.exact {
        println!("note: {}", e);
    }
    let mut undecided = [0usize; 6];
    let mut why: [Option<String>; 6] = [None, None, None, None, None, None];
    let mut tried = [0usize; 6];
    let mut res: Vec<(usize, Res)> = vec![];
    let judge = |kind: u8, cost: i64, label: String, ev: &Eval| -> Res {
        let o = &ev.out;
        let inf = uniq(&o.inferred);
        Res {
            kind,
            cost,
            label,
            unmet: unmet(o),
            pen: pen(o),
            changed: (0..m.webs.len()).any(|w| o.colour[w] != base.colour[w])
                || o.pops.iter().zip(&base.pops).any(|(a, b)| a.web != b.web || a.avail != b.avail || a.w != b.w),
            side: cx.side(&base, o, tg),
            ninf: inf.len(),
            desc: ev.desc.clone(),
            notes: inf
                .into_iter()
                .filter(|l| l.contains("runs out of registers") || l.contains("is coloured here") || l.contains("no register left"))
                .collect(),
            miss: tg
                .iter()
                .filter(|&&(w, r)| o.colour[w] != Some(r))
                .map(|&(w, r)| format!("{} {} (wanted {})", m.label(w), o.colour[w].map_or("-", rname), rname(r)))
                .collect(),
        }
    };
    // Every candidate is an independent run of the model: evaluated on all cores (par_map keeps the order, so the
    // report is the same as from a serial loop).
    // A change between two values that are both coloured after the last target cannot reach a target: a colouring
    // reads only what was coloured before it and its own uncoloured neighbours. Such a change is not run; it is
    // recorded as "nothing moves" (it can still be picked for a pair, where it is run).
    let pos: HashMap<usize, usize> = col.iter().enumerate().map(|(i, &w)| (w, i)).collect();
    let last_tg: Option<usize> = tg.iter().map(|t| pos.get(&t.0).copied()).collect::<Option<Vec<usize>>>().and_then(|v| v.into_iter().max());
    let beyond = |c: &Cand| -> bool {
        let (Some(last), true) = (last_tg, g.is_empty()) else { return false };
        match c {
            Cand::Abs(Change::Before(x, y))
            | Cand::Abs(Change::After(x, y))
            | Cand::Abs(Change::Interf(x, y))
            | Cand::Abs(Change::NoInterf(x, y))
            | Cand::Abs(Change::Copy(x, y))
            | Cand::Abs(Change::NoCopy(x, y)) => match (m.find(x).ok().and_then(|w| pos.get(&w)), m.find(y).ok().and_then(|w| pos.get(&w))) {
                (Some(&i), Some(&j)) => i > last && j > last,
                _ => false,
            },
            _ => false,
        }
    };
    let singles: Vec<Result<Res, String>> = par_map(&cands, |(kind, cost, c)| {
        if beyond(c) {
            return Ok(Res {
                kind: *kind,
                cost: *cost,
                label: cx.label(c),
                unmet: u0,
                pen: pen(&base),
                changed: false,
                side: vec![],
                ninf: 0,
                desc: vec![],
                notes: vec![],
                miss: tg
                    .iter()
                    .filter(|&&(w, r)| base.colour[w] != Some(r))
                    .map(|&(w, r)| format!("{} {} (wanted {})", m.label(w), base.colour[w].map_or("-", rname), rname(r)))
                    .collect(),
            });
        }
        let mut all = g.clone();
        all.push(c);
        cx.eval(&all).map(|ev| judge(*kind, *cost, cx.label(c), &ev))
    });
    for (k, ((kind, _, c), r)) in cands.iter().zip(singles).enumerate() {
        tried[*kind as usize] += 1;
        match r {
            Err(e) => {
                undecided[*kind as usize] += 1;
                why[*kind as usize].get_or_insert(format!("{}: {}", cx.label(c), e));
            }
            Ok(x) => res.push((k, x)),
        }
    }
    let line = |x: &Res| -> String {
        format!(
            "  {:<34}{}{}{}",
            x.label,
            if x.kind == 0 { format!(" gap {:<4}", x.cost) } else { String::new() },
            if x.side.is_empty() { "  no other register changes".to_string() } else { format!("  also moves: {}", x.side.join(", ")) },
            if x.ninf > 0 { format!("  [I: {} inferred relations]", x.ninf) } else { String::new() }
        )
    };
    let detail = |x: &Res| {
        for d in x.desc.iter().chain(x.notes.iter()) {
            println!("      {}", d);
        }
    };
    let mut nsol = 0;
    let mut nsrc = 0;
    for k in [4u8, 0, 1, 5, 2, 3] {
        if tried[k as usize] == 0 {
            continue;
        }
        let mut mine: Vec<&Res> = res.iter().map(|x| &x.1).filter(|x| x.kind == k && x.unmet == 0).collect();
        // two more references where one more already does it: not listed
        let labels: BTreeSet<String> = mine.iter().map(|x| x.label.clone()).collect();
        mine.retain(|x| !(x.label.starts_with("ref:") && x.label.ends_with(":4") && labels.contains(&x.label[..x.label.len() - 2])));
        mine.sort_by(|a, b| (a.side.len(), a.cost, &a.label).cmp(&(b.side.len(), b.cost, &b.label)));
        nsol += mine.len();
        if k == 4 {
            nsrc = mine.len();
        }
        println!("{}: {} single changes tried, {} give every target its register", KINDS[k as usize], tried[k as usize], mine.len());
        // answers that extend the same value the same way (same side effects) are one line; the nearest block first
        let gkey = |x: &Res| -> (String, Vec<String>) {
            match (x.label.starts_with("extend:"), x.label.rfind(':')) {
                (true, Some(i)) => (x.label[..i].to_string(), x.side.clone()),
                _ => (x.label.clone(), vec![]),
            }
        };
        let mut seen: Vec<(String, Vec<String>)> = vec![];
        let mut shown_n = 0;
        for x in mine.iter() {
            let key = gkey(x);
            if seen.contains(&key) {
                continue;
            }
            if shown_n == opts.top {
                println!("  ... more (--top N)");
                break;
            }
            shown_n += 1;
            println!("{}", line(x));
            if k == 4 {
                detail(x);
                let same: Vec<&str> = mine
                    .iter()
                    .filter(|y| gkey(y) == key && y.label != x.label)
                    .filter_map(|y| y.label.rsplit(':').next())
                    .collect();
                if !same.is_empty() {
                    println!("      the same result when the range reaches block {} instead", same.join(", "));
                }
            }
            seen.push(key);
        }
        if undecided[k as usize] > 0 {
            println!("  not decidable: {} changes (e.g. {})", undecided[k as usize], why[k as usize].clone().unwrap_or_default());
        }
    }
    // Pairs: of everything when no single change works; of source-level edits alone when only abstract changes do
    let src_only = nsol > 0;
    if (nsol == 0 || (nsrc == 0 && tried[4] > 0)) && opts.depth >= 2 {
        // pairs: the most promising singles first (fewest targets missed, nearest misses, changes that move
        // something, source edits before abstract changes, cheapest)
        let mut rank: Vec<&(usize, Res)> = res.iter().collect();
        rank.sort_by_key(|x| (x.1.unmet, x.1.pen, !x.1.changed, x.1.kind != 4, x.1.cost, x.0));
        let pool: Vec<&(usize, Res)> =
            rank.into_iter().filter(|x| !src_only || x.1.kind == 4).take(opts.pool).collect();
        let mut todo: Vec<(&(usize, Res), &(usize, Res))> = vec![];
        for (i, a) in pool.iter().enumerate() {
            for b in &pool[i + 1..] {
                todo.push((*a, *b));
            }
        }
        let ntried = todo.len();
        let mut pairs: Vec<(Res, Vec<usize>)> = par_map(&todo, |(a, b)| {
            let mut all = g.clone();
            all.push(&cands[a.0].2);
            all.push(&cands[b.0].2);
            cx.eval(&all).ok().map(|ev| {
                let lab = format!("{}  +  {}", a.1.label, b.1.label);
                (judge(a.1.kind.min(b.1.kind), a.1.cost + b.1.cost, lab, &ev), vec![a.0, b.0])
            })
        })
        .into_iter()
        .flatten()
        .collect();
        pairs.sort_by(|a, b| (a.0.unmet, a.0.side.len(), a.0.cost, &a.0.label).cmp(&(b.0.unmet, b.0.side.len(), b.0.cost, &b.0.label)));
        let n2 = pairs.iter().filter(|p| p.0.unmet == 0).count();
        nsol += n2;
        println!(
            "no single {} works. Pairs of the {} most promising single {} (--pool N): {} tried, {} give every \
             target its register (fewest other registers moved first, then cheapest)",
            if src_only { "source-level edit" } else { "change" },
            pool.len(),
            if src_only { "source-level edits" } else { "changes" },
            ntried,
            n2
        );
        for (x, _) in pairs.iter().filter(|p| p.0.unmet == 0).take(opts.top) {
            println!(
                "  {}{}{}",
                x.label,
                if x.side.is_empty() { "  no other register changes".to_string() } else { format!("  also moves: {}", x.side.join(", ")) },
                if x.ninf > 0 { format!("  [I: {} inferred relations]", x.ninf) } else { String::new() }
            );
            detail(x);
        }
        if n2 > opts.top {
            println!("  ... {} more (--top N)", n2 - opts.top);
        }
        if n2 == 0 && opts.depth >= 3 && !src_only {
            // triples: the best pairs, each with one more change from the head of the pool
            let heads: Vec<&(Res, Vec<usize>)> = pairs.iter().take(40).collect();
            let mut triples: Vec<Res> = vec![];
            let mut nt = 0;
            for (pr, ids) in heads {
                for c in pool.iter().take(60) {
                    if ids.contains(&c.0) {
                        continue;
                    }
                    let mut all = g.clone();
                    all.extend(ids.iter().map(|&i| &cands[i].2));
                    all.push(&cands[c.0].2);
                    nt += 1;
                    if let Ok(ev) = cx.eval(&all) {
                        let lab = format!("{}  +  {}", pr.label, c.1.label);
                        let r = judge(4, pr.cost + c.1.cost, lab, &ev);
                        if r.unmet == 0 {
                            triples.push(r);
                        }
                    }
                }
            }
            triples.sort_by(|a, b| (a.side.len(), a.cost, &a.label).cmp(&(b.side.len(), b.cost, &b.label)));
            nsol += triples.len();
            println!("no pair works. Triples (the 40 best pairs, each with one of the 60 best singles): {} tried, {} work", nt, triples.len());
            for x in triples.iter().take(opts.top) {
                println!(
                    "  {}{}",
                    x.label,
                    if x.side.is_empty() { "  no other register changes".to_string() } else { format!("  also moves: {}", x.side.join(", ")) }
                );
                detail(x);
            }
        }
        if nsol == 0 || (src_only && n2 == 0) {
            println!("nothing found{}. Closest:", if src_only { " among pairs of source-level edits" } else { "" });
            let mut best: Vec<&Res> = pairs.iter().map(|p| &p.0).chain(res.iter().map(|x| &x.1)).filter(|x| !src_only || x.kind == 4).collect();
            best.sort_by_key(|x| (x.unmet, x.pen, x.side.len(), x.cost));
            for x in best.iter().take(3) {
                println!("  {}  leaves {} of {} unmet: {}", x.label, x.unmet, tg.len(), x.miss.join(", "));
            }
        }
    } else if nsol == 0 {
        println!("no single change in the model gives every target its register");
    }
    if cx.s.passes.iter().filter(|p| p.cls == Some(0)).count() > 1 {
        println!(
            "note: this function has split rounds (several Spillcosts passes). Splits, the pieces' initial avail sets \
             and the neighbour lists are replayed as traced; a change that alters an earlier colouring can change \
             them. Re-trace to confirm."
        );
    }
}

/// `f` over `items` on every core, results in input order. RASIM_THREADS=1 (or a single core) runs it serially.
fn par_map<T: Sync, R: Send, F: Fn(&T) -> R + Sync>(items: &[T], f: F) -> Vec<R> {
    let nt = std::env::var("RASIM_THREADS")
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or_else(|| std::thread::available_parallelism().map_or(1, |n| n.get()))
        .clamp(1, 64);
    if nt == 1 || items.len() < 64 {
        return items.iter().map(&f).collect();
    }
    let next = std::sync::atomic::AtomicUsize::new(0);
    const CHUNK: usize = 32;
    let mut parts: Vec<(usize, Vec<R>)> = std::thread::scope(|sc| {
        let hs: Vec<_> = (0..nt)
            .map(|_| {
                sc.spawn(|| {
                    let mut out = vec![];
                    loop {
                        let lo = next.fetch_add(CHUNK, std::sync::atomic::Ordering::Relaxed);
                        if lo >= items.len() {
                            break;
                        }
                        let hi = (lo + CHUNK).min(items.len());
                        out.push((lo, items[lo..hi].iter().map(&f).collect::<Vec<R>>()));
                    }
                    out
                })
            })
            .collect();
        hs.into_iter().flat_map(|h| h.join().expect("worker panicked")).collect()
    });
    parts.sort_by_key(|p| p.0);
    parts.into_iter().flat_map(|p| p.1).collect()
}

fn warn_model(cx: &Ctx) {
    let ck = cx.m.check(cx.s);
    if ck.sim != ck.pops || ck.diverged != 0 {
        println!(
            "warning: the model reproduces {} of {} traced registers ({} state divergences); see `rasim check`",
            ck.sim, ck.pops, ck.diverged
        );
    }
}

/// `rasim need SNAP reg:VAR=REG ... [CHANGE|EDIT...]`
pub fn cmd_need(s: &Snap, targets: &[(String, String)], given: &[String], opts: &Opts) {
    let cx = Ctx::new(s);
    warn_model(&cx);
    let tg: Vec<(usize, u32)> = targets
        .iter()
        .map(|(v, r)| {
            (cx.m.find(v).unwrap_or_else(|e| die(&e)), rnum(r).unwrap_or_else(|| die(&format!("unknown register {}", r))))
        })
        .collect();
    let given: Vec<Cand> = given.iter().map(|a| cx.parse(a).unwrap_or_else(|e| die(&e))).collect();
    search(&cx, &tg, &given, opts);
}

/// `rasim want SNAP REGMAP.txt [CHANGE|EDIT...]`: targets from `c2trace.py regmap` output (or lines `VAR=REG`).
/// A row `WANTS r` is a target for every coloured piece of that variable that now has the register in the row's
/// `ours` column (`WANTS r for q`: the pieces that have q); a row `ok` (regmap --all) pins the variable's current
/// registers; `mixed` rows are skipped.
pub fn cmd_want(s: &Snap, path: &str, given: &[String], opts: &Opts) {
    let cx = Ctx::new(s);
    warn_model(&cx);
    let m = &cx.m;
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| die(&format!("{}: {}", path, e)));
    let now: Vec<Option<u32>> = match cx.eval(&[]) {
        Ok(e) => e.out.colour,
        Err(e) => die(&e),
    };
    let coloured = |name: &str| -> Vec<usize> { (0..m.webs.len()).filter(|&i| m.webs[i].pop.is_some() && m.webs[i].name == name).collect() };
    let mut tg: Vec<(usize, u32)> = vec![];
    let (mut nwant, mut nkeep, mut skipped) = (0, 0, vec![]);
    let add = |tg: &mut Vec<(usize, u32)>, w: usize, r: u32| {
        if !tg.iter().any(|t| t.0 == w) {
            tg.push((w, r));
        }
    };
    for ln in text.lines() {
        let t: Vec<&str> = ln.split_whitespace().collect();
        if t.is_empty() || ln.starts_with('#') || ln.starts_with("swap:") || ln.starts_with("var (IL)") || ln.starts_with("      ") {
            continue;
        }
        // hand-written: VAR=REG
        if t.len() == 1 && t[0].contains('=') {
            let (v, r) = t[0].split_once('=').unwrap();
            let v = v.strip_prefix("reg:").unwrap_or(v);
            match (m.find(v), rnum(r)) {
                (Ok(w), Some(r)) => {
                    nwant += 1;
                    add(&mut tg, w, r)
                }
                (Err(e), _) => skipped.push(format!("{}: {}", v, e)),
                (_, None) => skipped.push(format!("{}: unknown register {}", v, r)),
            }
            continue;
        }
        if t.len() < 4 {
            continue;
        }
        let name = t[1];
        let ours = t[2].split(':').next().and_then(rnum);
        let webs = coloured(name);
        let vi = t.iter().position(|x| *x == "ok" || *x == "WANTS" || *x == "mixed");
        let Some(vi) = vi else { continue };
        if webs.is_empty() {
            if t[vi] != "ok" {
                skipped.push(format!("{} ({}): not a value Color coloured (a scratch temporary: see `rasim scratch`)", name, t[vi..].join(" ")));
            }
            continue;
        }
        match t[vi] {
            "ok" => {
                for w in webs {
                    if let Some(r) = now[w] {
                        nkeep += 1;
                        add(&mut tg, w, r);
                    }
                }
            }
            "WANTS" => {
                let Some(r) = t.get(vi + 1).and_then(|r| rnum(r)) else { continue };
                let from = if t.get(vi + 2) == Some(&"for") { t.get(vi + 3).and_then(|q| rnum(q)) } else { ours };
                let hits: Vec<usize> = webs.iter().copied().filter(|&w| now[w] == from).collect();
                if hits.is_empty() {
                    skipped.push(format!("{}: no coloured piece has {} in this snapshot", name, from.map_or("?", rname)));
                }
                for w in hits {
                    nwant += 1;
                    add(&mut tg, w, r);
                }
            }
            _ => skipped.push(format!("{}: {}", name, t[vi..].join(" "))),
        }
    }
    println!("{}: {} registers wanted, {} to keep (rows marked ok), {} rows skipped", path, nwant, nkeep, skipped.len());
    for l in &skipped {
        println!("  skipped {}", l);
    }
    if tg.is_empty() {
        die("no targets in the file (expected `c2trace.py regmap` output or lines VAR=REG)");
    }
    let given: Vec<Cand> = given.iter().map(|a| cx.parse(a).unwrap_or_else(|e| die(&e))).collect();
    search(&cx, &tg, &given, opts);
}

/// Third line of `rasim check`: can source-level edits be carried into the register model for this snapshot?
pub fn cmd_check(s: &Snap) {
    let Ok(m) = Model::build(s) else { return };
    let uni = Uni::build(s, &m);
    match &uni.exact {
        Ok(()) => println!(
            "edits: the colouring order recomputed from the priorities is the traced one in all {} rounds; source-level \
             edits can be simulated (rasim regs / need reg: / want)",
            uni.rounds.len()
        ),
        Err(e) => println!("edits: {}", e),
    }
}
