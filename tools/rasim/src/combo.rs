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
use choose::{rname, rnum, Change, DiffOut, Inf, Item, Model, Mods, Outcome, PopOut, ORDER};

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
    // Precomputed for the fast path of `Ctx::eval` (same results as the direct computations they replace):
    init_av: Vec<Option<u32>>,                 // per web: init_avail
    kill_tab: BTreeMap<u32, Vec<(usize, bool, u32)>>, // per block k: the first-pass vars spanning k (var, lives through k, initial avail)
    pres: BTreeMap<u32, Vec<usize>>,           // per block: the first-pass vars present there, ascending
    prio0: Vec<i64>,                           // per var: its unedited priority
    elig: Vec<usize>,                          // the vars `steps` can pop, ascending
    elig_at: Vec<usize>,                       // per var: its index in elig (usize::MAX: none)
    bcoef: BTreeMap<u32, Vec<(usize, i64)>>,   // per block b: (index in elig, d prio / d P_b) of the first-pass vars
    // for `Uni::quick`:
    ord0: Vec<usize>,      // elig indices in colouring order (unedited priorities)
    rank: Vec<u32>,        // per elig index: its place in ord0
    tiek: Vec<Option<(std::cmp::Reverse<u32>, std::cmp::Reverse<u32>, i64)>>, // per elig index: sort_order's tie key
    kall: Vec<u32>,        // per block: `kills_fast` for a var that does not span it
    pw: Vec<i64>,          // pw[k]: sum of P_b * w_b over the first-pass blocks b < k
    p1vars: Vec<usize>,    // the first-pass vars, ascending
    rnew: Vec<Vec<usize>>, // per round: the vars that `steps_ord` can pop from it on (empty: rounds not ascending)
    pres_of: Vec<(u32, u32, bool, u32, u32)>, // per var: (first, last present block, present in all between, ...)
    pbl: Vec<u32>,         // per var u: its present blocks pbl[pres_of[u].3..], then the blocks k with k and k + 1
                           // present, up to pres_of[u].4
}

/// A source edit as `Uni::quick` gives it: the order is unchanged, or the colouring order it gives.
enum Quick {
    Same,
    Order(Vec<usize>),
}

thread_local! {
    static MARK: std::cell::RefCell<Vec<(u32, i64)>> = std::cell::RefCell::new(vec![]); // per elig index: (stamp, new prio)
    static MARK_G: std::cell::Cell<u32> = std::cell::Cell::new(0);
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
            init_av: {
                // init_avail for every web, in one pass
                let mut v = vec![None; m.webs.len()];
                for it in &m.items {
                    if let Item::Init { web, avail, .. } = it {
                        if v[*web].is_none() {
                            v[*web] = Some(*avail);
                        }
                    }
                }
                v
            },
            kill_tab: BTreeMap::new(),
            pres: BTreeMap::new(),
            prio0: vec![],
            elig: vec![],
            elig_at: vec![],
            bcoef: BTreeMap::new(),
            ord0: vec![],
            rank: vec![],
            tiek: vec![],
            kall: vec![],
            pw: vec![],
            p1vars: vec![],
            rnew: vec![],
            pres_of: vec![],
            pbl: vec![],
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

    /// The tables the fast path of `Ctx::eval` uses (`need`, `want`).
    fn prepare(&mut self) {
        let u = self;
        for (i, y) in u.base.vars.iter().enumerate() {
            if y.pass != u.p1 {
                continue;
            }
            for b in y.cost.keys().chain(y.live.iter()) {
                u.pres.entry(*b).or_default().push(i);
            }
            let (Some((a, z)), Some(av)) = (range(y), u.web_of[i].and_then(|w| u.init_av[w])) else { continue };
            for k in a + 1..z {
                if present(y, k) {
                    u.kill_tab.entry(k).or_default().push((i, y.live.contains(&k), av));
                }
            }
        }
        for l in u.pres.values_mut() {
            l.sort_unstable();
            l.dedup();
        }
        u.prio0 = u.base.vars.iter().map(|v| u.base.prio(v)).collect();
        let nv = u.base.vars.len().min(u.ev_of.len());
        u.elig = (0..nv).filter(|&v| !u.base.vars[v].removed && (u.ev_of[v].is_some() || u.synth[v])).collect();
        u.elig_at = vec![usize::MAX; u.base.vars.len()];
        for (i, &v) in u.elig.iter().enumerate() {
            u.elig_at[v] = i;
            let x = &u.base.vars[v];
            let (true, Some(t)) = (x.pass == u.p1, x.own.as_ref()) else { continue };
            let blocks: BTreeSet<u32> = x.cost.keys().chain(x.live.iter()).copied().collect();
            for b in blocks {
                let c = x.cost.get(&b).copied().unwrap_or(0) - x.live.contains(&b) as i64;
                u.bcoef.entry(b).or_default().push((i, c * t.get(&b).map_or(1, |y| y.0)));
            }
        }
        u.prepare_quick();
    }

    /// The rest of `prepare`: the unedited colouring order of the eligible vars, kill estimates and penalty sums.
    fn prepare_quick(&mut self) {
        let u = self;
        let tie = |v: &Var| {
            v.num.map(|n| {
                if v.pass <= 1 {
                    (std::cmp::Reverse(v.pass), std::cmp::Reverse(n & 0x3ff), n as i64)
                } else {
                    (std::cmp::Reverse(v.pass), std::cmp::Reverse(0), -(n as i64))
                }
            })
        };
        u.tiek = u.elig.iter().map(|&v| tie(&u.base.vars[v])).collect();
        let mut o: Vec<(usize, i64)> = u.elig.iter().map(|&v| (v, u.prio0[v])).collect();
        u.base.sort_order(&mut o);
        u.ord0 = o.iter().map(|x| u.elig_at[x.0]).collect();
        u.rank = vec![0; u.elig.len()];
        for (r, &i) in u.ord0.iter().enumerate() {
            u.rank[i] = r as u32;
        }
        let tbl = u.base.vars.iter().find(|v| v.pass == u.p1).and_then(|v| v.own.clone()).unwrap_or_default();
        let maxb = tbl.keys().copied().max().unwrap_or(0) as usize + 2;
        u.pw = vec![0; maxb + 1];
        for k in 0..maxb {
            let (w, p) = tbl.get(&(k as u32)).copied().unwrap_or((1, 0));
            u.pw[k + 1] = u.pw[k] + p * w;
        }
        u.kall = (0..maxb as u32).map(|k| u.kills_fast(usize::MAX, k)).collect();
        // the rounds in which steps_ord may pop each var: an interval, the passes ascending
        if u.rounds.windows(2).all(|p| p[0] < p[1]) {
            let nv = u.base.vars.len().min(u.ev_of.len());
            let mut rnew = vec![vec![]; u.rounds.len()];
            for v in 0..nv {
                if !(u.ev_of[v].is_some() || u.synth[v]) {
                    continue;
                }
                let ok: Vec<bool> = u.rounds.iter().map(|&k| u.base.vars[v].pass <= k && u.alive(v, k)).collect();
                let Some(lo) = ok.iter().position(|&b| b) else { continue };
                let hi = ok.iter().rposition(|&b| b).unwrap();
                if ok[lo..=hi].iter().any(|&b| !b) {
                    rnew.clear();
                    break;
                }
                rnew[lo].push(v);
            }
            u.rnew = rnew;
        }
        u.pres_of = vec![(0, 0, false, 0, 0); u.base.vars.len()];
        for (i, x) in u.base.vars.iter().enumerate() {
            if x.pass != u.p1 {
                continue;
            }
            let mut bl: Vec<u32> = x.cost.keys().chain(x.live.iter()).copied().collect();
            bl.sort_unstable();
            bl.dedup();
            if bl.is_empty() {
                continue;
            }
            u.p1vars.push(i);
            let (lo, hi) = (bl[0], *bl.last().unwrap());
            let all = (hi - lo + 1) as usize == bl.len();
            let s = u.pbl.len() as u32;
            let pairs: Vec<u32> = bl.windows(2).filter(|p| p[1] == p[0] + 1).map(|p| p[0]).collect();
            u.pbl.extend(&bl);
            let m = u.pbl.len() as u32;
            u.pbl.extend(pairs);
            u.pres_of[i] = (lo, hi, all, s, m);
        }
    }

    /// Var u is present in a block of k0..k1, or in two neighbouring blocks of c0..=c1.
    fn meets(&self, u: usize, k0: u32, k1: u32, c0: u32, c1: u32) -> bool {
        let (lo, hi, all, s, m) = self.pres_of[u];
        if all {
            return (k0 < k1 && lo < k1 && hi >= k0) || lo.max(c0) < hi.min(c1);
        }
        let bl = &self.pbl[s as usize..m as usize];
        let i = bl.partition_point(|&b| b < k0);
        if i < bl.len() && bl[i] < k1 {
            return true;
        }
        let pe = self.pair_end(u);
        let pr = &self.pbl[m as usize..pe];
        let j = pr.partition_point(|&b| b < c0);
        j < pr.len() && pr[j] < c1
    }

    fn pair_end(&self, u: usize) -> usize {
        // the next first-pass var's lists start where u's pairs end
        match self.p1vars.binary_search(&u) {
            Ok(k) if k + 1 < self.p1vars.len() => self.pres_of[self.p1vars[k + 1]].3 as usize,
            _ => self.pbl.len(),
        }
    }

    /// sort_order's rule: elig index i (priority pi) is coloured before j (priority pj).
    fn first(&self, i: usize, pi: i64, j: usize, pj: i64) -> bool {
        let (a, b) = (&self.base.vars[self.elig[i]], &self.base.vars[self.elig[j]]);
        (pj, b.def_ord, &self.tiek[i], i) < (pi, a.def_ord, &self.tiek[j], j)
    }

    /// One `ref:`, `unref:` or `extend:` edit for the fast path of `Ctx::eval`: what `apply` adds to the mods
    /// (without the description), and the colouring order from the changed priorities (computed from the few vars
    /// whose priority changes). None: an edit this does not handle.
    fn quick(&self, m: &Model, e: &Src, md: &mut Mods) -> Option<Result<Quick, String>> {
        let v = match e {
            Src::Ref(v, ..) | Src::Unref(v, ..) | Src::Extend(v, ..) => *v,
            _ => return None,
        };
        let x = &self.base.vars[v];
        if x.pass != self.p1 {
            return None;
        }
        let name = || self.vlabel(v);
        let web = self.web_of[v];
        let tb = |b: u32| x.own.as_ref().and_then(|t| t.get(&b)).copied().unwrap_or((1, 0));
        let lv = |b: &u32| x.live.contains(b) as i64;
        // (the edited var's new priority, the block whose P changes and by how much)
        let (pv, bump): (i64, Option<(u32, i64)>) = match e {
            Src::Ref(_, b, c) => {
                if !present(x, *b) {
                    return Some(Err(format!("{} is not live in block {}; use extend:{}:{}", name(), b, name(), b)));
                }
                let had = x.cost.contains_key(b);
                let c0 = x.cost.get(b).copied().unwrap_or(0);
                let (w, p) = tb(*b);
                let old = c0 * p * w - lv(b) * p * w;
                let new = (c0 + c) * (p + !had as i64) * w;
                if let Some(wv) = web {
                    md.ben.push((wv, c * w));
                }
                (self.prio0[v] - old + new, (!had).then_some((*b, 1)))
            }
            Src::Unref(_, b) => {
                let Some(&c0) = x.cost.get(b) else {
                    return Some(Err(format!("{} has no reference in block {}", name(), b)));
                };
                let (w, p) = tb(*b);
                let old = c0 * p * w - lv(b) * p * w;
                let new = -(p - 1) * w;
                if let Some(wv) = web {
                    md.ben.push((wv, -c0 * w));
                }
                (self.prio0[v] - old + new, Some((*b, -1)))
            }
            Src::Extend(_, b) => {
                let Some((lo, hi)) = range(x) else { return Some(Err(format!("{} has no blocks", name()))) };
                if (lo..=hi).contains(b) {
                    return Some(Err(format!("{} already reaches block {} (range {}..{}); use ref:", name(), b, lo, hi)));
                }
                let Some(wv) = web else { return Some(Err(format!("{} has no avail set in the trace", name()))) };
                if !x.own.as_ref().map_or(false, |t| t.contains_key(b)) {
                    return Some(Err(format!("no block {} in Spillcosts pass {}", b, self.p1)));
                }
                let (k0, k1) = if *b < lo { (*b + 1, lo) } else { (hi + 1, *b) }; // the blocks in between
                let (c0, c1) = if *b < lo { (*b, lo) } else { (hi, *b) }; // the chain of blocks it now spans
                if (k1 as usize) >= self.pw.len() {
                    return None;
                }
                // the first-pass vars present in a block in between, or in two neighbouring blocks of the chain
                let meet: Vec<usize> = self.p1vars.iter().copied().filter(|&u| self.meets(u, k0, k1, c0, c1)).collect();
                let mut over: Vec<usize> =
                    meet.iter().filter(|&&u| u != v).filter_map(|&u| self.web_of[u]).filter(|&wu| m.init_seen[wu]).collect();
                let mut ws = over.clone();
                ws.sort_unstable();
                if ws.windows(2).any(|p| p[0] == p[1]) {
                    let mut seen = BTreeSet::new();
                    over.retain(|&w| seen.insert(w));
                }
                let mut kills = 0u32;
                for k in k0..k1 {
                    kills |= self.kall[k as usize];
                }
                kills &= self.init_av[wv].unwrap_or(0);
                let (w, p) = tb(*b);
                md.ben.push((wv, 2 * w));
                for &y in &over {
                    md.add_e.push((wv, y));
                }
                if kills != 0 {
                    md.kills.push((wv, kills));
                }
                md.ext.push((wv, over));
                let pen = self.pw[k1 as usize] - self.pw[k0 as usize];
                (self.prio0[v] - pen + 2 * (p + 1) * w, Some((*b, 1)))
            }
            _ => unreachable!(),
        };
        // the eligible vars whose priority changes, with the new one
        let mut chg: Vec<(usize, i64)> = vec![];
        if let Some((b, dp)) = bump {
            for &(i, c) in self.bcoef.get(&b).map_or(&[][..], |l| &l[..]) {
                chg.push((i, self.prio0[self.elig[i]] + c * dp));
            }
        }
        let ve = self.elig_at[v];
        if ve != usize::MAX {
            match chg.iter_mut().find(|x| x.0 == ve) {
                Some(x) => x.1 = pv,
                None => chg.push((ve, pv)),
            }
        }
        chg.retain(|&(i, p)| p != self.prio0[self.elig[i]]);
        let n = self.ord0.len();
        MARK.with(|mk| {
            let mut mk = mk.borrow_mut();
            if mk.len() < n {
                mk.resize(n, (0, 0));
            }
            let g = MARK_G.with(|c| {
                let g = c.get().wrapping_add(1).max(1);
                c.set(g);
                g
            });
            if g == 1 {
                mk.iter_mut().for_each(|x| x.0 = 0);
            }
            for &(i, p) in &chg {
                mk[i] = (g, p);
            }
            let prio = |i: usize| if mk[i].0 == g { mk[i].1 } else { self.prio0[self.elig[i]] };
            let sorted = chg.iter().all(|&(i, p)| {
                let r = self.rank[i] as usize;
                (r == 0 || self.first(self.ord0[r - 1], prio(self.ord0[r - 1]), i, p))
                    && (r + 1 >= n || self.first(i, p, self.ord0[r + 1], prio(self.ord0[r + 1])))
            });
            if sorted {
                return Some(Ok(Quick::Same));
            }
            // the unchanged ones keep their order; the changed ones are sorted and merged in
            let mut moved: Vec<(usize, i64)> = chg.clone();
            moved.sort_by(|a, b| {
                if self.first(a.0, a.1, b.0, b.1) {
                    std::cmp::Ordering::Less
                } else {
                    std::cmp::Ordering::Greater
                }
            });
            let mut ord: Vec<usize> = Vec::with_capacity(n);
            let mut k = 0;
            for &i in &self.ord0 {
                if mk[i].0 == g {
                    continue;
                }
                let p = self.prio0[self.elig[i]];
                while k < moved.len() && self.first(moved[k].0, moved[k].1, i, p) {
                    ord.push(self.elig[moved[k].0]);
                    k += 1;
                }
                ord.push(self.elig[i]);
            }
            ord.extend(moved[k..].iter().map(|x| self.elig[x.0]));
            Some(Ok(Quick::Order(ord)))
        })
    }

    fn alive(&self, v: usize, k: u32) -> bool {
        self.gone_after[v].map_or(true, |r| r >= k) && self.sup[v].map_or(true, |q| q > k)
    }

    /// The step order Color would follow with these priorities: per round, the list is popped in priority order
    /// until the pop that ended the round in the trace; then that round's Spillcosts results come in.
    fn steps(&self, m: &Model, st: &State) -> Vec<usize> {
        let ord: Vec<usize> = st.order().iter().map(|x| x.0).collect();
        self.steps_ord(m, &ord)
    }

    /// `steps` from the colouring order. Only the order of the vars in `elig` matters.
    fn steps_ord(&self, m: &Model, ord: &[usize]) -> Vec<usize> {
        let st = &self.base; // pass and count only: an edit changes neither
        let nv = st.vars.len().min(self.ev_of.len());
        let mut popped = vec![false; st.vars.len()];
        // A var can be popped in an interval of rounds (Uni::rlo). A new round's scan would start over at ord[0]; it
        // finds the same var when started at the old position or at the first var that has just become poppable.
        let fresh = !self.rnew.is_empty();
        let mut pos: Vec<u32> = vec![];
        if fresh {
            pos = vec![u32::MAX; st.vars.len()];
            for (i, &v) in ord.iter().enumerate() {
                if v < pos.len() {
                    pos[v] = i as u32;
                }
            }
        }
        // ord[..cur] holds nothing round r can pop: popped, or not in the list in this round
        let (mut cur, mut cur_r) = (0usize, usize::MAX);
        let nr = self.rounds.len();
        let mut out: Vec<usize> = vec![];
        let mut r = 0;
        out.extend(&self.clusters[0]);
        out.extend(&self.lead[0]);
        loop {
            let k = self.rounds[r];
            if cur_r != r {
                if !fresh || cur_r == usize::MAX {
                    cur = 0;
                } else {
                    for rr in cur_r + 1..=r {
                        for &v in &self.rnew[rr] {
                            if !popped[v] {
                                cur = cur.min(pos[v] as usize);
                            }
                        }
                    }
                }
                cur_r = r;
            }
            while cur < ord.len() {
                let v = ord[cur];
                if v < nv
                    && !popped[v]
                    && (self.ev_of[v].is_some() || self.synth[v])
                    && st.vars[v].pass <= k
                    && self.alive(v, k)
                {
                    break;
                }
                cur += 1;
            }
            let next = ord.get(cur).copied();
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
        let mut have = vec![false; m.items.len() + m.webs.len()];
        for &i in &out {
            have[i] = true;
        }
        for v in 0..nv {
            if let Some(items) = &self.ev_of[v] {
                out.extend(items.iter().filter(|&&i| !have[i]));
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

    /// `kills` from the precomputed table.
    fn kills_fast(&self, v: usize, k: u32) -> u32 {
        let mut through: Option<u32> = None;
        let mut around = 0u32;
        for &(u, thr, av) in self.kill_tab.get(&k).map_or(&[][..], |l| &l[..]) {
            if u == v {
                continue;
            }
            if thr {
                through = Some(through.unwrap_or(0x1de) & !av & 0x1de);
            } else {
                around |= !av & 0xe;
            }
        }
        through.unwrap_or(around)
    }

    fn kill_est(&self, m: &Model, v: usize, k: u32, fast: bool) -> u32 {
        if fast {
            self.kills_fast(v, k)
        } else {
            self.kills(m, v, k)
        }
    }

    fn iav(&self, m: &Model, w: usize, fast: bool) -> Option<u32> {
        if fast {
            self.init_av[w]
        } else {
            init_avail(m, w)
        }
    }

    fn weight<E: Es>(&self, st: &E, v: usize, b: u32) -> i64 {
        st.v(v).own.as_ref().and_then(|t| t.get(&b)).map_or(1, |x| x.0)
    }

    /// Apply one edit to the priority state and collect what it means for the register model.
    fn apply<E: Es>(
        &self,
        m: &Model,
        e: &Src,
        st: &mut E,
        md: &mut Mods,
        desc: &mut Vec<String>,
        fast: bool,
    ) -> Result<(), String> {
        let v = match e {
            Src::Ref(v, ..) | Src::Unref(v, ..) | Src::Drop(v, ..) | Src::Extend(v, ..) => *v,
            Src::NewVar(n, refs) => {
                for (b, _) in refs {
                    st.bump(self.p1, *b, 1);
                }
                desc.push(format!("{}: P += 1 in its blocks; its own register and overlaps are not modelled", n));
                return Ok(());
            }
        };
        let name = self.vlabel(v);
        let web = self.web_of[v];
        let p0 = st.prio_of(v);
        let ben = |md: &mut Mods, d: i64| {
            if let Some(w) = web {
                md.ben.push((w, d));
            }
        };
        match e {
            Src::Ref(_, b, c) => {
                if !present(st.v(v), *b) {
                    return Err(format!("{} is not live in block {}; use extend:{}:{}", name, b, name, b));
                }
                let had = st.v(v).cost.contains_key(b);
                *st.vm(v).cost.entry(*b).or_insert(0) += c;
                st.vm(v).live.remove(b);
                if !had {
                    st.bump(self.p1, *b, 1);
                }
                let w = self.weight(st, v, *b);
                ben(md, c * w);
            }
            Src::Unref(_, b) => {
                let Some(c0) = st.vm(v).cost.remove(b) else {
                    return Err(format!("{} has no reference in block {}", name, b));
                };
                st.bump(self.p1, *b, -1);
                st.vm(v).live.insert(*b);
                let w = self.weight(st, v, *b);
                ben(md, -c0 * w);
            }
            Src::Drop(_, b) => {
                let Some((lo, hi)) = range(st.v(v)) else { return Err(format!("{} has no blocks", name)) };
                if *b != lo && *b != hi {
                    return Err(format!("block {} is inside {}'s range {}..{}; only an end can be dropped", b, name, lo, hi));
                }
                if st.v(v).cost.keys().all(|k| k == b) {
                    return Err(format!("block {} holds {}'s only references", b, name));
                }
                // the range now ends at the nearest block that still has a reference: the blocks lived through
                // on the way go too
                let mut gone = vec![*b];
                let mut k = *b;
                loop {
                    k = if *b == lo { k + 1 } else { k - 1 };
                    if st.v(v).cost.contains_key(&k) || k == 0 || k > hi {
                        break;
                    }
                    if st.v(v).live.contains(&k) {
                        gone.push(k);
                    }
                }
                let mut back = 0u32;
                for k in &gone {
                    if let Some(c0) = st.vm(v).cost.remove(k) {
                        st.bump(self.p1, *k, -1);
                        let w = self.weight(st, v, *k);
                        ben(md, -c0 * w);
                    } else {
                        back |= self.kill_est(m, v, *k, fast);
                    }
                    st.vm(v).live.remove(k);
                }
                // registers killed in the blocks it lived through come back unless a block it still spans kills them
                // [I: the end blocks of the remaining range are taken as not killing them]
                if let (Some(wv), Some((nlo, nhi))) = (web, range(st.v(v))) {
                    for k in nlo + 1..nhi {
                        back &= !self.kill_est(m, v, k, fast);
                    }
                    back &= !self.iav(m, wv, fast).unwrap_or(0);
                    if back != 0 {
                        md.gives.push((wv, back));
                    }
                }
                let mut lost = vec![];
                for u in 0..st.n() {
                    let y = st.v(u);
                    if u == v || y.pass != self.p1 || !gone.iter().any(|k| present(y, *k)) {
                        continue;
                    }
                    let x = st.v(v);
                    let shared = y.cost.keys().chain(y.live.iter()).any(|k| present(x, *k));
                    if let (false, Some(wv), Some(wu)) = (shared, web, self.web_of[u]) {
                        md.del_e.push((wv, wu));
                        lost.push(m.label(wu));
                    }
                }
                let (nlo, nhi) = range(st.v(v)).unwrap_or((lo, hi));
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
                let Some((lo, hi)) = range(st.v(v)) else { return Err(format!("{} has no blocks", name)) };
                if (lo..=hi).contains(b) {
                    return Err(format!("{} already reaches block {} (range {}..{}); use ref:", name, b, lo, hi));
                }
                let Some(wv) = web else { return Err(format!("{} has no avail set in the trace", name)) };
                let known = |k: &u32| st.v(v).own.as_ref().map_or(false, |t| t.contains_key(k));
                if !known(b) {
                    return Err(format!("no block {} in Spillcosts pass {}", b, self.p1));
                }
                let between: Vec<u32> = if *b < lo { (*b + 1..lo).collect() } else { (hi + 1..*b).collect() };
                let chain: Vec<u32> = if *b < lo { (*b..=lo).collect() } else { (hi..=*b).collect() };
                let mut over: Vec<usize> = vec![];
                let mut kills = 0u32;
                // the first-pass vars present in a block in between, or in two neighbouring blocks of the chain
                let meet: Vec<usize> = if fast {
                    let at = |k: &u32| self.pres.get(k).map_or(&[][..], |l| &l[..]);
                    let mut mark = vec![false; self.base.vars.len()];
                    for k in &between {
                        for &u in at(k) {
                            mark[u] = true;
                        }
                    }
                    for p in chain.windows(2) {
                        // both lists ascending: their intersection
                        let (a, b) = (at(&p[0]), at(&p[1]));
                        let (mut i, mut j) = (0, 0);
                        while i < a.len() && j < b.len() {
                            if a[i] < b[j] {
                                i += 1;
                            } else if b[j] < a[i] {
                                j += 1;
                            } else {
                                mark[a[i]] = true;
                                i += 1;
                                j += 1;
                            }
                        }
                    }
                    mark[v] = false;
                    (0..mark.len()).filter(|&u| mark[u]).collect()
                } else {
                    (0..self.base.vars.len())
                        .filter(|&u| {
                            let y = &self.base.vars[u];
                            u != v
                                && y.pass == self.p1
                                && (between.iter().any(|k| present(y, *k))
                                    || chain.windows(2).any(|p| present(y, p[0]) && present(y, p[1])))
                        })
                        .collect()
                };
                for u in meet {
                    if let Some(wu) = self.web_of[u] {
                        if m.init_seen[wu] && !over.contains(&wu) {
                            over.push(wu);
                        }
                    }
                }
                for k in &between {
                    kills |= self.kill_est(m, v, *k, fast);
                }
                kills &= self.iav(m, wv, fast).unwrap_or(0);
                for k in between {
                    st.vm(v).live.insert(k);
                }
                st.vm(v).cost.insert(*b, 2);
                st.bump(self.p1, *b, 1);
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
        let p1 = st.prio_of(v);
        if p1 != p0 {
            desc.push(format!("{} prio {} -> {}", name, p0, p1));
        }
        Ok(())
    }
}

/// The priority state an edit works on: `State` (a full copy of the base) or `Ov` (the base with a few vars copied
/// out and the P changes listed).
trait Es {
    fn v(&self, u: usize) -> &Var;
    fn vm(&mut self, u: usize) -> &mut Var;
    /// P_b += dp in the block table of every var of pass p1
    fn bump(&mut self, p1: u32, b: u32, dp: i64);
    fn n(&self) -> usize;
    fn prio_of(&self, u: usize) -> i64;
}

fn bump_var(v: &mut Var, p1: u32, b: u32, dp: i64) {
    if v.pass == p1 {
        if let Some(t) = v.own.as_mut() {
            t.entry(b).or_insert((1, 0)).1 += dp;
        }
    }
}

impl Es for State {
    fn v(&self, u: usize) -> &Var {
        &self.vars[u]
    }
    fn vm(&mut self, u: usize) -> &mut Var {
        &mut self.vars[u]
    }
    fn bump(&mut self, p1: u32, b: u32, dp: i64) {
        for v in self.vars.iter_mut() {
            bump_var(v, p1, b, dp);
        }
    }
    fn n(&self) -> usize {
        self.vars.len()
    }
    fn prio_of(&self, u: usize) -> i64 {
        self.prio(&self.vars[u])
    }
}

/// The base state with the edited vars copied out (their block tables carry every bump) and the bumps listed; an
/// unedited var's priority is its base priority plus what the bumps add (the priority is linear in each P_b).
struct Ov<'a> {
    base: &'a State,
    prio0: &'a [i64],
    ed: Vec<(usize, Var)>,
    bumps: Vec<(u32, u32, i64)>, // (p1, b, dp)
}

impl<'a> Es for Ov<'a> {
    fn v(&self, u: usize) -> &Var {
        self.ed.iter().find(|x| x.0 == u).map_or(&self.base.vars[u], |x| &x.1)
    }
    fn vm(&mut self, u: usize) -> &mut Var {
        let i = match self.ed.iter().position(|x| x.0 == u) {
            Some(i) => i,
            None => {
                let mut v = self.base.vars[u].clone();
                for &(p1, b, dp) in &self.bumps {
                    bump_var(&mut v, p1, b, dp);
                }
                self.ed.push((u, v));
                self.ed.len() - 1
            }
        };
        &mut self.ed[i].1
    }
    fn bump(&mut self, p1: u32, b: u32, dp: i64) {
        self.bumps.push((p1, b, dp));
        for (_, v) in self.ed.iter_mut() {
            bump_var(v, p1, b, dp);
        }
    }
    fn n(&self) -> usize {
        self.base.vars.len()
    }
    fn prio_of(&self, u: usize) -> i64 {
        if let Some(x) = self.ed.iter().find(|x| x.0 == u) {
            return self.base.prio(&x.1);
        }
        let v = &self.base.vars[u];
        let mut p = self.prio0[u];
        if let Some(t) = v.own.as_ref() {
            for &(p1, b, dp) in &self.bumps {
                if v.pass == p1 {
                    let c = v.cost.get(&b).copied().unwrap_or(0) - v.live.contains(&b) as i64;
                    p += c * t.get(&b).map_or(1, |x| x.0) * dp;
                }
            }
        }
        p
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
    /// false (RASIM_REF=1, or `rasim regs`): every candidate copies the whole priority state and runs the register
    /// model from the start (the reference the fast path is checked against; same output)
    fast: bool,
    tr_nat: Option<choose::Trace>,   // the unedited run in the traced order (abstract changes only)
    tr_steps: Option<choose::Trace>, // the unedited run in the order `Uni::steps` computes (with source edits)
    alias: bool,                     // the two unedited runs colour alike, in the same colourings
    collide: bool,                   // two webs have the same label (inferred lines are then compared as text)
}

/// A run's outcome: in full (the reference path), or as differences from an unedited run (`Model::run_diff`).
pub enum Run<'t> {
    Full(Outcome),
    Diff(DiffOut<'t>),
}

impl<'t> Run<'t> {
    pub fn colour(&self, w: usize) -> Option<u32> {
        match self {
            Run::Full(o) => o.colour[w],
            Run::Diff(d) => d.colour(w),
        }
    }

    pub fn colours(&self, n: usize) -> Vec<Option<u32>> {
        (0..n).map(|w| self.colour(w)).collect()
    }

    pub fn first_pop(&self, w: usize) -> Option<&PopOut> {
        match self {
            Run::Full(o) => o.pops.iter().find(|p| p.web == w),
            Run::Diff(d) => d.first_pop(w),
        }
    }

    pub fn pops(&self) -> Vec<&PopOut> {
        match self {
            Run::Full(o) => o.pops.iter().collect(),
            Run::Diff(d) => d.pops().collect(),
        }
    }

    pub fn infs(&self) -> Vec<&Inf> {
        match self {
            Run::Full(o) => o.inferred.iter().collect(),
            Run::Diff(d) => d.infs().collect(),
        }
    }

    pub fn outcome(&self, n: usize) -> Outcome {
        Outcome {
            colour: self.colours(n),
            pops: self.pops().into_iter().cloned().collect(),
            inferred: self.infs().into_iter().cloned().collect(),
        }
    }
}

/// A candidate ready to run (`Ctx::prep`).
struct Prep {
    md: Mods,
    same: bool,                // the step order is the unedited one
    ord: Option<Vec<usize>>,   // the colouring order (its step order is computed when run)
    desc: Vec<String>,
}

pub struct Eval<'t> {
    pub out: Run<'t>,
    pub desc: Vec<String>,
}

impl<'a> Ctx<'a> {
    pub fn new(s: &'a Snap) -> Ctx<'a> {
        Self::with(s, false)
    }

    /// `fast`: the fast path of `eval` (an overlay of the priority state, precomputed tables, and the unedited runs
    /// kept for `Model::run_diff`); worth its setup only when many candidates are evaluated. RASIM_REF=1 turns it off.
    pub fn with(s: &'a Snap, traces: bool) -> Ctx<'a> {
        let m = Model::build(s).unwrap_or_else(|e| die(&e));
        let mut uni = Uni::build(s, &m);
        let fast = traces && std::env::var("RASIM_REF").map_or(true, |v| v.is_empty() || v == "0");
        if fast {
            uni.prepare();
        }
        let (tr_nat, tr_steps) = if fast {
            let st = if uni.exact.is_ok() { m.trace(Some(uni.steps(&m, &uni.base))).ok() } else { None };
            (m.trace(None).ok(), st)
        } else {
            (None, None)
        };
        let alias = match (&tr_nat, &tr_steps) {
            (Some(a), Some(b)) => a.outcome().pops == b.outcome().pops && a.outcome().colour == b.outcome().colour,
            _ => false,
        };
        let mut labs: Vec<String> = (0..m.webs.len()).map(|w| m.label(w)).collect();
        labs.sort_unstable();
        let collide = labs.windows(2).any(|p| p[0] == p[1]);
        Ctx { s, m, uni, fast, tr_nat, tr_steps, alias, collide }
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

    pub fn eval(&self, cs: &[&Cand]) -> Result<Eval<'_>, String> {
        self.eval_d(cs, true)
    }

    /// `want_desc` false: the edits' descriptions may be left out (Eval.desc empty).
    pub fn eval_d(&self, cs: &[&Cand], want_desc: bool) -> Result<Eval<'_>, String> {
        let p = self.prep(cs, want_desc)?;
        let out = self.exec(&p)?;
        Ok(Eval { out, desc: p.desc })
    }

    /// Everything but the register model's run: the changes resolved, the edits applied, the step order.
    fn prep(&self, cs: &[&Cand], want_desc: bool) -> Result<Prep, String> {
        let abs: Vec<Change> = cs.iter().filter_map(|c| if let Cand::Abs(c) = c { Some(c.clone()) } else { None }).collect();
        let mut md = self.m.resolve(&abs)?;
        let mut desc = vec![];
        let srcs: Vec<&Src> = cs.iter().filter_map(|c| if let Cand::Src(e) = c { Some(e) } else { None }).collect();
        let (mut same, mut ord) = (false, None);
        if !srcs.is_empty() {
            self.uni.exact.clone()?;
            let q = match (&self.tr_steps, self.fast && !want_desc && srcs.len() == 1) {
                (Some(_), true) => self.uni.quick(&self.m, srcs[0], &mut md),
                _ => None,
            };
            if let Some(q) = q {
                let q = q?;
                if std::env::var_os("RASIM_QCHECK").is_some() {
                    // the overlay path must give the same mods and order
                    let mut m2 = self.m.resolve(&abs)?;
                    let o2 = self.ov_order(&srcs, &mut m2, &mut vec![])?;
                    let o1 = match &q {
                        Quick::Same => self.tr_steps.as_ref().unwrap().order().to_vec(),
                        Quick::Order(o) => self.uni.steps_ord(&self.m, o),
                    };
                    let key = |m: &Mods| format!("{:?} {:?} {:?} {:?} {:?} {:?}", m.ben, m.kills, m.gives, m.add_e, m.del_e, m.ext);
                    assert!(o1 == o2 && key(&md) == key(&m2), "quick differs for {}", self.uni.label(srcs[0]));
                }
                match q {
                    Quick::Same => same = true,
                    Quick::Order(o) => ord = Some(o),
                }
                if same && !md.moves.is_empty() {
                    md.order = Some(self.tr_steps.as_ref().unwrap().order().to_vec());
                    same = false;
                }
            } else if self.fast {
                md.order = Some(self.ov_order(&srcs, &mut md, &mut desc)?);
            } else {
                let mut st = self.uni.base.clone();
                for e in srcs {
                    self.uni.apply(&self.m, e, &mut st, &mut md, &mut desc, false)?;
                }
                md.order = Some(self.uni.steps(&self.m, &st));
            }
        }
        Ok(Prep { md, same, ord, desc })
    }

    /// The register model's run for a `prep`.
    fn exec(&self, p: &Prep) -> Result<Run<'_>, String> {
        let steps;
        let md = match &p.ord {
            Some(o) => {
                let mut m = p.md.clone();
                steps = self.uni.steps_ord(&self.m, o);
                m.order = Some(steps);
                std::borrow::Cow::Owned(m)
            }
            None => std::borrow::Cow::Borrowed(&p.md),
        };
        let tr = if md.order.is_some() || p.same { self.tr_steps.as_ref() } else { self.tr_nat.as_ref() };
        Ok(match tr {
            Some(t) => Run::Diff(self.m.run_diff(&md, t, p.same)?),
            None => Run::Full(self.m.run(&md)?),
        })
    }

    /// The edits applied to an overlay of the priority state; the step order from the changed priorities.
    fn ov_order(&self, srcs: &[&Src], md: &mut Mods, desc: &mut Vec<String>) -> Result<Vec<usize>, String> {
        let u = &self.uni;
        let mut ov = Ov { base: &u.base, prio0: &u.prio0, ed: vec![], bumps: vec![] };
        for e in srcs {
            u.apply(&self.m, e, &mut ov, md, desc, true)?;
        }
        // the priorities Ov::prio_of gives, from the per-block coefficients
        let mut o: Vec<(usize, i64)> = u.elig.iter().map(|&v| (v, u.prio0[v])).collect();
        for &(p1, b, dp) in &ov.bumps {
            debug_assert_eq!(p1, u.p1);
            for &(i, c) in u.bcoef.get(&b).map_or(&[][..], |l| &l[..]) {
                o[i].1 += c * dp;
            }
        }
        for (v, x) in &ov.ed {
            if u.elig_at[*v] != usize::MAX {
                o[u.elig_at[*v]].1 = u.base.prio(x);
            }
        }
        u.base.sort_order(&mut o);
        let ord: Vec<usize> = o.into_iter().map(|x| x.0).collect();
        Ok(u.steps_ord(&self.m, &ord))
    }

    /// The webs whose colour differs between two outcomes, ascending.
    fn colour_diff(&self, a: &Run, b: &Run) -> Vec<usize> {
        let n = self.m.webs.len();
        if let (Run::Diff(x), Run::Diff(y)) = (a, b) {
            if self.alias || std::ptr::eq(x.tr, y.tr) {
                let mut v: Vec<usize> = x.cdiff.iter().chain(&y.cdiff).map(|e| e.0).collect();
                v.sort_unstable();
                v.dedup();
                v.retain(|&w| a.colour(w) != b.colour(w));
                return v;
            }
        }
        (0..n).filter(|&w| a.colour(w) != b.colour(w)).collect()
    }

    /// Some colouring at the same index differs in web, candidates or weights (over the shorter list).
    fn pops_differ(&self, a: &Run, b: &Run) -> bool {
        if let (Run::Diff(x), Run::Diff(y)) = (a, b) {
            return DiffOut::pops_differ(x, y, self.alias);
        }
        a.pops().iter().zip(b.pops().iter()).any(|(p, q)| p.web != q.web || p.avail != q.avail || p.w != q.w)
    }

    /// The number of distinct inferred lines, and those of them `need` prints under an answer.
    fn inf_summary(&self, o: &Run) -> (usize, Vec<String>) {
        let all = o.infs();
        if all.is_empty() {
            return (0, vec![]);
        }
        if self.collide {
            let txt: Vec<String> = all.iter().map(|l| self.m.inf_text(l)).collect();
            let u = uniq(&txt);
            let notes = u
                .iter()
                .filter(|l| l.contains("runs out of registers") || l.contains("is coloured here") || l.contains("no register left"))
                .cloned()
                .collect();
            return (u.len(), notes);
        }
        let mut seen: std::collections::HashSet<&Inf> = std::collections::HashSet::new();
        let mut n = 0;
        let mut notes = vec![];
        for l in all {
            if seen.insert(l) {
                n += 1;
                if l.is_note() {
                    notes.push(self.m.inf_text(l));
                }
            }
        }
        (n, notes)
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
    let n = cx.m.webs.len();
    let given: Vec<Cand> = args.iter().map(|a| cx.parse(a).unwrap_or_else(|e| die(&e))).collect();
    let base = cx.eval(&[]).unwrap_or_else(|e| die(&e)).out.outcome(n);
    let refs: Vec<&Cand> = given.iter().collect();
    match cx.eval(&refs) {
        Ok(ev) => {
            for d in &ev.desc {
                println!("edit: {}", d);
            }
            let out = ev.out.outcome(n);
            choose::show(&cx.m, &out, if given.is_empty() { None } else { Some(&base) });
            let txt: Vec<String> = out.inferred.iter().map(|l| cx.m.inf_text(l)).collect();
            for l in uniq(&txt) {
                println!("  [I] {}", l);
            }
            if !given.is_empty() {
                let side = cx.side(&base, &out, &[]);
                println!(
                    "{} of {} registers change{}{}",
                    side.len(),
                    base.pops.len(),
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
    ids: [u32; 3], // the candidates combined (indices into the single changes), ids[..nid]
    nid: u8,
    unmet: usize,
    pen: i64,
    changed: bool,
    side: Vec<(usize, Option<u32>, Option<u32>)>, // other registers that move: (web, from, to)
    ninf: usize,
    notes: Vec<String>,
    miss: Vec<(usize, Option<u32>, u32)>, // targets still unmet: (web, now, wanted)
}

impl Res {
    fn ids(&self) -> &[u32] {
        &self.ids[..self.nid as usize]
    }
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
    let unmet = |o: &Run| tg.iter().filter(|&&(w, r)| o.colour(w) != Some(r)).count();
    // how far a miss is: the wanted register is not available (2), or available but outweighed / later in order (1)
    let pen = |o: &Run| -> i64 {
        tg.iter()
            .map(|&(w, r)| match (o.colour(w) == Some(r), o.first_pop(w)) {
                (true, _) => 0,
                (false, Some(p)) if p.avail >> r & 1 == 1 => 1,
                _ => 2,
            })
            .sum()
    };
    let shown = tg.len() <= 12;
    for &(w, r) in tg {
        let p = base.first_pop(w);
        let holds = base.colour(w) == Some(r);
        if !shown && holds {
            continue;
        }
        println!(
            "{} (pop {}, prio {}): now {}, wanted {}; avail when coloured: {}{}",
            m.label(w),
            m.webs[w].pop.unwrap_or(0),
            m.webs[w].prio,
            base.colour(w).map_or("-", rname),
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
    let col: Vec<usize> = base.pops().iter().map(|p| p.web).collect(); // coloured webs, in colouring order
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
    let ids_of = |v: &[usize]| -> ([u32; 3], u8) {
        let mut a = [0u32; 3];
        for (i, &x) in v.iter().enumerate() {
            a[i] = x as u32;
        }
        (a, v.len() as u8)
    };
    let judge = |kind: u8, cost: i64, ids: &[usize], ev: &Eval| -> Res {
        let o = &ev.out;
        let (ninf, notes) = cx.inf_summary(o);
        let diffw = cx.colour_diff(&base, o);
        let changed = !diffw.is_empty() || cx.pops_differ(o, &base);
        let (ids, nid) = ids_of(ids);
        Res {
            kind,
            cost,
            ids,
            nid,
            unmet: unmet(o),
            pen: pen(o),
            changed,
            side: diffw.iter().filter(|w| !tg.iter().any(|t| t.0 == **w)).map(|&w| (w, base.colour(w), o.colour(w))).collect(),
            ninf,
            notes,
            miss: tg.iter().filter(|&&(w, r)| o.colour(w) != Some(r)).map(|&(w, r)| (w, o.colour(w), r)).collect(),
        }
    };
    // labels, descriptions and side effects are made only for what is printed
    let lab = |x: &Res| -> String { x.ids().iter().map(|&i| cx.label(&cands[i as usize].2)).collect::<Vec<_>>().join("  +  ") };
    let side_txt = |x: &Res| -> Vec<String> {
        x.side.iter().map(|&(w, a, b)| format!("{} {}->{}", m.label(w), a.map_or("-", rname), b.map_or("-", rname))).collect()
    };
    let miss_txt = |x: &Res| -> Vec<String> {
        x.miss.iter().map(|&(w, c, r)| format!("{} {} (wanted {})", m.label(w), c.map_or("-", rname), rname(r))).collect()
    };
    let desc_of = |x: &Res| -> Vec<String> {
        let mut all = g.clone();
        all.extend(x.ids().iter().map(|&i| &cands[i as usize].2));
        cx.eval(&all).map_or(vec![], |e| e.desc)
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
    let pen0 = pen(&base);
    let miss0: Vec<(usize, Option<u32>, u32)> =
        tg.iter().filter(|&&(w, r)| base.colour(w) != Some(r)).map(|&(w, r)| (w, base.colour(w), r)).collect();
    let idx: Vec<usize> = (0..cands.len()).collect();
    let singles: Vec<Result<Res, String>> = par_map(&idx, |&k| {
        let (kind, cost, c) = &cands[k];
        if beyond(c) {
            let (ids, nid) = ids_of(&[k]);
            return Ok(Res {
                kind: *kind,
                cost: *cost,
                ids,
                nid,
                unmet: u0,
                pen: pen0,
                changed: false,
                side: vec![],
                ninf: 0,
                notes: vec![],
                miss: miss0.clone(),
            });
        }
        let mut all = g.clone();
        all.push(c);
        cx.eval_d(&all, false).map(|ev| judge(*kind, *cost, &[k], &ev))
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
    let line = |x: &Res, label: &str| -> String {
        let side = side_txt(x);
        format!(
            "  {:<34}{}{}{}",
            label,
            if x.kind == 0 { format!(" gap {:<4}", x.cost) } else { String::new() },
            if side.is_empty() { "  no other register changes".to_string() } else { format!("  also moves: {}", side.join(", ")) },
            if x.ninf > 0 { format!("  [I: {} inferred relations]", x.ninf) } else { String::new() }
        )
    };
    let detail = |x: &Res| {
        for d in desc_of(x).iter().chain(x.notes.iter()) {
            println!("      {}", d);
        }
    };
    let mut nsol = 0;
    let mut nsrc = 0;
    for k in [4u8, 0, 1, 5, 2, 3] {
        if tried[k as usize] == 0 {
            continue;
        }
        let mut mine: Vec<(&Res, String)> = res.iter().map(|x| &x.1).filter(|x| x.kind == k && x.unmet == 0).map(|x| (x, lab(x))).collect();
        // two more references where one more already does it: not listed
        let labels: BTreeSet<String> = mine.iter().map(|x| x.1.clone()).collect();
        mine.retain(|x| !(x.1.starts_with("ref:") && x.1.ends_with(":4") && labels.contains(&x.1[..x.1.len() - 2])));
        mine.sort_by(|a, b| (a.0.side.len(), a.0.cost, &a.1).cmp(&(b.0.side.len(), b.0.cost, &b.1)));
        nsol += mine.len();
        if k == 4 {
            nsrc = mine.len();
        }
        println!("{}: {} single changes tried, {} give every target its register", KINDS[k as usize], tried[k as usize], mine.len());
        // answers that extend the same value the same way (same side effects) are one line; the nearest block first
        let gkey = |x: &(&Res, String)| -> (String, Vec<String>) {
            match (x.1.starts_with("extend:"), x.1.rfind(':')) {
                (true, Some(i)) => (x.1[..i].to_string(), side_txt(x.0)),
                _ => (x.1.clone(), vec![]),
            }
        };
        let keys: Vec<(String, Vec<String>)> = mine.iter().map(gkey).collect();
        let mut seen: Vec<(String, Vec<String>)> = vec![];
        let mut shown_n = 0;
        for (xi, x) in mine.iter().enumerate() {
            let key = &keys[xi];
            if seen.contains(key) {
                continue;
            }
            if shown_n == opts.top {
                println!("  ... more (--top N)");
                break;
            }
            shown_n += 1;
            println!("{}", line(x.0, &x.1));
            if k == 4 {
                detail(x.0);
                let same: Vec<&str> = mine
                    .iter()
                    .enumerate()
                    .filter(|(yi, y)| keys[*yi] == *key && y.1 != x.1)
                    .filter_map(|(_, y)| y.1.rsplit(':').next())
                    .collect();
                if !same.is_empty() {
                    println!("      the same result when the range reaches block {} instead", same.join(", "));
                }
            }
            seen.push(key.clone());
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
        let mut pairs: Vec<(Res, String)> = par_map(&todo, |(a, b)| {
            let mut all = g.clone();
            all.push(&cands[a.0].2);
            all.push(&cands[b.0].2);
            cx.eval_d(&all, false).ok().map(|ev| judge(a.1.kind.min(b.1.kind), a.1.cost + b.1.cost, &[a.0, b.0], &ev))
        })
        .into_iter()
        .flatten()
        .map(|r| {
            let l = lab(&r);
            (r, l)
        })
        .collect();
        pairs.sort_by(|a, b| (a.0.unmet, a.0.side.len(), a.0.cost, &a.1).cmp(&(b.0.unmet, b.0.side.len(), b.0.cost, &b.1)));
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
        for (x, l) in pairs.iter().filter(|p| p.0.unmet == 0).take(opts.top) {
            let side = side_txt(x);
            println!(
                "  {}{}{}",
                l,
                if side.is_empty() { "  no other register changes".to_string() } else { format!("  also moves: {}", side.join(", ")) },
                if x.ninf > 0 { format!("  [I: {} inferred relations]", x.ninf) } else { String::new() }
            );
            detail(x);
        }
        if n2 > opts.top {
            println!("  ... {} more (--top N)", n2 - opts.top);
        }
        if n2 == 0 && opts.depth >= 3 && !src_only {
            // triples: the best pairs, each with one more change from the head of the pool
            let heads: Vec<&(Res, String)> = pairs.iter().take(40).collect();
            let mut triples: Vec<(Res, String)> = vec![];
            let mut nt = 0;
            for (pr, _) in heads {
                for c in pool.iter().take(60) {
                    let ids: Vec<usize> = pr.ids().iter().map(|&i| i as usize).collect();
                    if ids.contains(&c.0) {
                        continue;
                    }
                    let mut all = g.clone();
                    all.extend(ids.iter().map(|&i| &cands[i].2));
                    all.push(&cands[c.0].2);
                    nt += 1;
                    if let Ok(ev) = cx.eval_d(&all, false) {
                        let r = judge(4, pr.cost + c.1.cost, &[ids[0], ids[1], c.0], &ev);
                        if r.unmet == 0 {
                            let l = lab(&r);
                            triples.push((r, l));
                        }
                    }
                }
            }
            triples.sort_by(|a, b| (a.0.side.len(), a.0.cost, &a.1).cmp(&(b.0.side.len(), b.0.cost, &b.1)));
            nsol += triples.len();
            println!("no pair works. Triples (the 40 best pairs, each with one of the 60 best singles): {} tried, {} work", nt, triples.len());
            for (x, l) in triples.iter().take(opts.top) {
                let side = side_txt(x);
                println!(
                    "  {}{}",
                    l,
                    if side.is_empty() { "  no other register changes".to_string() } else { format!("  also moves: {}", side.join(", ")) }
                );
                detail(x);
            }
        }
        if nsol == 0 || (src_only && n2 == 0) {
            println!("nothing found{}. Closest:", if src_only { " among pairs of source-level edits" } else { "" });
            let mut best: Vec<&Res> = pairs.iter().map(|p| &p.0).chain(res.iter().map(|x| &x.1)).filter(|x| !src_only || x.kind == 4).collect();
            best.sort_by_key(|x| (x.unmet, x.pen, x.side.len(), x.cost));
            for x in best.iter().take(3) {
                println!("  {}  leaves {} of {} unmet: {}", lab(x), x.unmet, tg.len(), miss_txt(x).join(", "));
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
    let cx = Ctx::with(s, true);
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
    let cx = Ctx::with(s, true);
    warn_model(&cx);
    let m = &cx.m;
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| die(&format!("{}: {}", path, e)));
    let now: Vec<Option<u32>> = match cx.eval(&[]) {
        Ok(e) => e.out.colours(m.webs.len()),
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
