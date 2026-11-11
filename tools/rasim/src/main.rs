//! rasim: what-if simulator for c2's register-allocation priorities (VS2005 SP1 c2.dll 14.00.50727.762).
//!
//! Input: a snapshot from `uv run tools/c2trace.py regs SRC --func F --export snap.json -- <cl args>`.
//! The model is c2's Spillcosts formula (docs/c2/regalloc.md section 3.2), applied to the measured block x globreg
//! matrix of one Spillcosts pass:
//!
//!   prio(v) = sum over blocks B that reference v of cost(v,B) * P_B * w_B  -  sum over blocks v lives through of P_B * w_B
//!   P_B     = number of globregs referenced in B (coloured ones included), w_B = 2^loop depth at /O2
//!   order   = prio descending, then defOrd descending (unsigned), exact ties by insertion order (section 3.4)
//!
//! A globreg is (record pointer, pass it was costed in): records are recycled. Splits come from the trace
//! (`split_removed`, RemoveSplitGlobregs); deferred webs pop again with a neighbour's priority (InsertByBenefit),
//! and `check` verifies those re-pops against the deferral order instead of a total (section 3.5).
//!
//! Commands:
//!   rasim check SNAP                       verify the model against the trace (every term, every total, the order
//!                                          of every pass up to its first deviation, and the re-pops)
//!   rasim order SNAP [--pass N] [--top K] [EDIT...]
//!                                          priority order of pass N, after the edits; changes are marked
//!   rasim need SNAP [--pass N] [EDIT...] A>B [A>B...] [--edits K]   (EDITs are applied first, as givens)
//!                                          for each constraint "A must be coloured before B": the current gap; then the
//!                                          single edits and pairs of edits that satisfy all constraints, cheapest first
//!   rasim blocks SNAP [--pass N] VAR...    each block's w, P and the variables' terms side by side
//!
//! EDIT (what a source change does to the IL, see docs/c2/regalloc.md section 7):
//!   ref:VAR:B[:COST]    VAR gets a reference of COST (default 2) in block B (P_B += 1 if it was not referenced)
//!   unref:VAR:B         VAR loses its references in B but stays live through it (P_B -= 1)
//!   drop:VAR:B          VAR is no longer referenced in or live through B (P_B -= 1 if it was referenced)
//!   var:NAME:B[:C],B..  a new globreg (e.g. a named local) referenced in those blocks (P += 1 in each)
//!   w:B:N  P:B:N        override a block's weight or pressure
//! VAR is a name (`this`), `name#grptr` when a name has several pieces, or `name@k` for its k-th piece.

use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::process::exit;

#[derive(Deserialize)]
struct Snap {
    func: String,
    passes: Vec<SPass>,
    pops: Vec<SPop>,
}
#[derive(Deserialize)]
struct SPass {
    n: u32,
    cls: Option<i64>,
    blocks: Vec<SBlock>,
    grs: HashMap<String, SGr>,
    /// globregs RemoveSplitGlobregs unlinked during this pass's colouring (split; replaced by pieces next pass).
    /// Absent in older snapshots: then splits are inferred from names.
    split_removed: Option<Vec<String>>,
}
#[derive(Deserialize)]
struct SBlock {
    b: u32,
    w: i64,
    #[serde(rename = "P")]
    p: i64,
}
#[derive(Deserialize)]
struct SGr {
    name: String,
    terms: Vec<STerm>,
}
#[derive(Deserialize)]
struct STerm {
    b: u32,
    cost: Option<i64>,
    delta: i64,
}
#[derive(Deserialize)]
struct SPop {
    pop: u32,
    name: String,
    gr: String,
    prio: i64,
    #[serde(rename = "defOrd")]
    def_ord: i64,
    reg: Option<String>,
    npass: u32,
    cls: Option<i64>,
    #[serde(default)]
    ben: Option<i64>, // GLOBREG.spillCost (+0x40): InsertByBenefit's key for deferred webs
    #[serde(default)]
    kind: Option<String>,
    #[serde(default)]
    num: Option<u32>, // GLOBREG.num (+0x1c): web number; its low 10 bits are the _GlobregHash bucket
}

#[derive(Clone)]
struct Var {
    name: String,
    id: String,
    def_ord: u32,
    cost: BTreeMap<u32, i64>, // referenced blocks -> reference cost in the block
    live: BTreeSet<u32>,      // blocks lived through without a reference
    own: Option<BTreeMap<u32, (i64, i64)>>, // the block table of an earlier pass this var was costed in
    removed: bool, // never popped and never split: taken out by SpillObvious (trace fact, rule not modelled)
    pass: u32,     // the Spillcosts pass this record was costed in (records are recycled across passes)
    num: Option<u32>, // web number, from its pop (None if never popped)
}

#[derive(Clone)]
struct State {
    blocks: BTreeMap<u32, (i64, i64)>, // b -> (w, P)
    vars: Vec<Var>,
}

impl State {
    fn from_pass(p: &SPass, defs: &HashMap<String, i64>) -> State {
        let blocks = p.blocks.iter().map(|b| (b.b, (b.w, b.p))).collect();
        let mut vars: Vec<Var> = p
            .grs
            .iter()
            .map(|(id, g)| {
                let mut cost = BTreeMap::new();
                let mut live = BTreeSet::new();
                for t in &g.terms {
                    match t.cost {
                        Some(c) => *cost.entry(t.b).or_insert(0) += c,
                        None => {
                            live.insert(t.b);
                        }
                    }
                }
                Var {
                    name: g.name.clone(),
                    id: id.clone(),
                    def_ord: *defs
                        .get(&format!("{}|{}", id, g.name))
                        .or_else(|| defs.get(&format!("name:{}", g.name)))
                        .unwrap_or(&0) as u32,
                    cost,
                    live,
                    own: None,
                    removed: false,
                    pass: p.n,
                    num: None,
                }
            })
            .collect();
        vars.sort_by(|a, b| a.id.cmp(&b.id));
        State { blocks, vars }
    }

    fn prio(&self, v: &Var) -> i64 {
        let tbl = v.own.as_ref().unwrap_or(&self.blocks);
        let mut s = 0;
        for (b, c) in &v.cost {
            let (w, p) = tbl.get(b).copied().unwrap_or((1, 0));
            s += c * p * w;
        }
        for b in &v.live {
            let (w, p) = tbl.get(b).copied().unwrap_or((1, 0));
            s -= p * w;
        }
        s
    }

    /// The list Color pops from when pass k starts: pass k's new pieces plus every globreg of the same class costed
    /// in an earlier pass that was neither popped before pass k nor replaced by pieces (same name) in pass k.
    /// Earlier globregs keep the priority of the pass they were costed in (c2 does not re-cost them).
    fn at_pass(s: &Snap, k: u32, defs: &HashMap<String, i64>) -> State {
        let pk = s.passes.iter().find(|p| p.n == k).unwrap_or_else(|| die(&format!("no pass {}", k)));
        let mut st = State::from_pass(pk, defs);
        let exact = has_split_info(s);
        let names: BTreeSet<String> = pk.grs.values().map(|g| g.name.clone()).collect();
        let popped = popped_set(s, Some(k));
        let mut seen: BTreeSet<String> = pk.grs.iter().map(|(k, g)| format!("{}|{}", k, g.name)).collect();
        let mut earlier: Vec<&SPass> = s.passes.iter().filter(|p| p.n < k && p.cls == pk.cls).collect();
        earlier.sort_by(|a, b| b.n.cmp(&a.n));
        for p in earlier {
            let one = State::from_pass(p, defs);
            // pieces with this name in any later pass up to k replace the globreg costed here
            let later: BTreeSet<String> = s
                .passes
                .iter()
                .filter(|q| q.n > p.n && q.n <= k && q.cls == pk.cls)
                .flat_map(|q| q.grs.values().map(|g| g.name.clone()))
                .collect();
            for mut v in one.vars {
                if !seen.insert(format!("{}|{}", v.id, v.name)) || popped.contains(&(v.id.clone(), p.n)) {
                    continue;
                }
                let gone = if exact {
                    split_round(s, &v.id, p.n).map_or(false, |r| r < k)
                } else {
                    names.contains(&v.name) || later.contains(&v.name)
                };
                if gone {
                    continue;
                }
                v.own = Some(one.blocks.clone());
                st.vars.push(v);
            }
        }
        st
    }

    /// Colouring order: prio descending, then defOrd descending (unsigned). On an exact tie InsertInList puts the
    /// web inserted later in front, so exact ties (immediates have defOrd 0, split pieces copy their parent's)
    /// follow the insertion order backwards:
    ///   - the initial list (OrderGlobregs 10b11770) visits _GlobregHash buckets 0..1023, each chain head first:
    ///     bucket descending, then lower num first within a bucket [bucket: C; within a bucket: I, untested];
    ///   - pieces from a split round (InsertNewGlobregs 10b133e0) are inserted later, in ascending web number
    ///     (SbvNext over the split set): later rounds first, then higher num first [C, big synthetic +350 pops].
    fn order(&self) -> Vec<(usize, i64)> {
        let mut o: Vec<(usize, i64)> =
            self.vars.iter().enumerate().filter(|(_, v)| !v.removed).map(|(i, v)| (i, self.prio(v))).collect();
        let tie = |v: &Var| {
            v.num.map(|n| {
                if v.pass <= 1 {
                    (std::cmp::Reverse(v.pass), std::cmp::Reverse(n & 0x3ff), n as i64)
                } else {
                    (std::cmp::Reverse(v.pass), std::cmp::Reverse(0), -(n as i64))
                }
            })
        };
        o.sort_by(|a, b| {
            b.1.cmp(&a.1)
                .then(self.vars[b.0].def_ord.cmp(&self.vars[a.0].def_ord))
                .then(tie(&self.vars[a.0]).cmp(&tie(&self.vars[b.0])))
        });
        o
    }

    fn find(&self, sel: &str) -> Result<usize, String> {
        let (name, pick) = if let Some((n, id)) = sel.split_once('#') {
            (n, Some(("#", id)))
        } else if let Some((n, k)) = sel.split_once('@') {
            (n, Some(("@", k)))
        } else {
            (sel, None)
        };
        let hits: Vec<usize> = self.vars.iter().enumerate().filter(|(_, v)| v.name == name).map(|(i, _)| i).collect();
        match pick {
            Some(("#", id)) => hits
                .into_iter()
                .find(|&i| self.vars[i].id == id)
                .ok_or(format!("no piece {}#{} in this pass", name, id)),
            Some((_, k)) => {
                let k: usize = k.parse().map_err(|_| format!("bad piece index in {}", sel))?;
                hits.get(k.wrapping_sub(1)).copied().ok_or(format!("{} has {} pieces", name, hits.len()))
            }
            None => match hits.len() {
                0 => Err(format!("no globreg named {} in this pass", name)),
                1 => Ok(hits[0]),
                _ => Err(format!(
                    "{} has {} pieces here; use {}",
                    name,
                    hits.len(),
                    hits.iter().map(|&i| format!("{}#{}", name, self.vars[i].id)).collect::<Vec<_>>().join(" or ")
                )),
            },
        }
    }

    /// A source change alters P in every pass, so the edit reaches every block table (also earlier passes').
    fn bump(&mut self, b: u32, dp: i64) {
        self.blocks.entry(b).or_insert((1, 0)).1 += dp;
        for v in &mut self.vars {
            if let Some(t) = v.own.as_mut() {
                t.entry(b).or_insert((1, 0)).1 += dp;
            }
        }
    }

    fn apply(&mut self, e: &Edit) -> Result<(), String> {
        match e {
            Edit::Ref(v, b, c) => {
                let i = self.find(v)?;
                let had = self.vars[i].cost.contains_key(b);
                *self.vars[i].cost.entry(*b).or_insert(0) += c;
                self.vars[i].live.remove(b);
                if !had {
                    self.bump(*b, 1);
                }
            }
            Edit::Unref(v, b, keep_live) => {
                let i = self.find(v)?;
                let had = self.vars[i].cost.remove(b).is_some();
                if had {
                    self.bump(*b, -1);
                }
                if *keep_live {
                    self.vars[i].live.insert(*b);
                } else {
                    self.vars[i].live.remove(b);
                }
            }
            Edit::NewVar(name, refs) => {
                let mut cost = BTreeMap::new();
                for (b, c) in refs {
                    cost.insert(*b, *c);
                    self.bump(*b, 1);
                }
                self.vars.push(Var {
                    name: name.clone(),
                    id: format!("new{}", self.vars.len()),
                    def_ord: 0,
                    cost,
                    live: BTreeSet::new(),
                    own: None,
                    removed: false,
                    pass: 0,
                    num: None,
                });
            }
            Edit::W(b, n) => {
                self.blocks.entry(*b).or_insert((1, 0)).0 = *n;
            }
            Edit::P(b, n) => {
                self.blocks.entry(*b).or_insert((1, 0)).1 = *n;
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
enum Edit {
    Ref(String, u32, i64),
    Unref(String, u32, bool),
    NewVar(String, Vec<(u32, i64)>),
    W(u32, i64),
    P(u32, i64),
}

impl std::fmt::Display for Edit {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        match self {
            Edit::Ref(v, b, c) => write!(f, "ref:{}:{}:{}", v, b, c),
            Edit::Unref(v, b, true) => write!(f, "unref:{}:{}", v, b),
            Edit::Unref(v, b, false) => write!(f, "drop:{}:{}", v, b),
            Edit::NewVar(n, r) => write!(
                f,
                "var:{}:{}",
                n,
                r.iter().map(|(b, c)| format!("{}:{}", b, c)).collect::<Vec<_>>().join(",")
            ),
            Edit::W(b, n) => write!(f, "w:{}:{}", b, n),
            Edit::P(b, n) => write!(f, "P:{}:{}", b, n),
        }
    }
}

fn parse_edit(s: &str) -> Result<Edit, String> {
    let p: Vec<&str> = s.splitn(3, ':').collect();
    let num = |x: &str| x.parse::<i64>().map_err(|_| format!("bad number {:?} in {}", x, s));
    let blk = |x: &str| x.parse::<u32>().map_err(|_| format!("bad block {:?} in {}", x, s));
    match (p.first().copied(), p.len()) {
        (Some("ref"), 3) => {
            let (b, c) = p[2].split_once(':').unwrap_or((p[2], "2"));
            Ok(Edit::Ref(p[1].into(), blk(b)?, num(c)?))
        }
        (Some("unref"), 3) => Ok(Edit::Unref(p[1].into(), blk(p[2])?, true)),
        (Some("drop"), 3) => Ok(Edit::Unref(p[1].into(), blk(p[2])?, false)),
        (Some("var"), 3) => {
            let mut refs = vec![];
            for t in p[2].split(',') {
                let (b, c) = t.split_once(':').unwrap_or((t, "2"));
                refs.push((blk(b)?, num(c)?));
            }
            Ok(Edit::NewVar(p[1].into(), refs))
        }
        (Some("w"), 3) => Ok(Edit::W(blk(p[1])?, num(p[2])?)),
        (Some("P"), 3) => Ok(Edit::P(blk(p[1])?, num(p[2])?)),
        _ => Err(format!("bad edit {:?} (ref:VAR:B[:C] unref:VAR:B drop:VAR:B var:N:B[:C],.. w:B:N P:B:N)", s)),
    }
}

fn load(path: &str) -> Snap {
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| die(&format!("{}: {}", path, e)));
    serde_json::from_str(&text).unwrap_or_else(|e| die(&format!("{}: {}", path, e)))
}

fn die(m: &str) -> ! {
    eprintln!("rasim: {}", m);
    exit(2)
}

/// defOrd by globreg pointer, plus "name:<n>" fallbacks: split pieces copy their parent's defOrd.
fn defs(s: &Snap) -> HashMap<String, i64> {
    // GLOBREG records are recycled (_FreeGlobs), so a pointer is only an identity together with the name
    let mut m: HashMap<String, i64> = s.pops.iter().map(|p| (format!("{}|{}", p.gr, p.name), p.def_ord)).collect();
    for p in &s.pops {
        m.entry(format!("name:{}", p.name)).or_insert(p.def_ord);
    }
    m
}

fn state(s: &Snap, n: Option<u32>) -> State {
    let mut st = match n {
        Some(k) => State::at_pass(s, k, &defs(s)),
        None => State::from_pass(pass(s, None), &defs(s)),
    };
    mark_removed(s, &mut st);
    set_nums(s, &mut st);
    st
}

/// Web numbers from the pops: the first pop of the same record (id, costing pass).
fn set_nums(s: &Snap, st: &mut State) {
    for v in &mut st.vars {
        v.num = s
            .pops
            .iter()
            .find(|q| q.gr == v.id && costed_at(s, &q.gr, q.npass) == Some(v.pass))
            .and_then(|q| q.num);
    }
}

/// Globregs that are never popped and whose name never reappears in a later pass (not split): SpillObvious
/// removed them before ordering. This is read from the trace, not predicted; a what-if cannot add or undo it.
/// The pass in which globreg record `id` was last costed at or before pass `at` (records are recycled, so a
/// record is one value only together with the pass it was costed in).
fn costed_at(s: &Snap, id: &str, at: u32) -> Option<u32> {
    s.passes.iter().filter(|p| p.n <= at && p.grs.contains_key(id)).map(|p| p.n).max()
}

/// (id, costing pass) of every pop made in a pass < before (all pops if before is None).
fn popped_set(s: &Snap, before: Option<u32>) -> BTreeSet<(String, u32)> {
    s.pops
        .iter()
        .filter(|q| before.map_or(true, |k| q.npass < k))
        .filter_map(|q| costed_at(s, &q.gr, q.npass).map(|h| (q.gr.clone(), h)))
        .collect()
}

fn has_split_info(s: &Snap) -> bool {
    s.passes.iter().any(|p| p.split_removed.is_some())
}

/// The colouring round (pass number) in which the globreg record `id`, as costed in pass `costed`, was split:
/// the first round r >= costed that unlinked `id`, unless the record was re-used (costed again) before r.
fn split_round(s: &Snap, id: &str, costed: u32) -> Option<u32> {
    let mut ps: Vec<&SPass> = s.passes.iter().filter(|p| p.n >= costed).collect();
    ps.sort_by_key(|p| p.n);
    for p in ps {
        if p.n > costed && p.grs.contains_key(id) {
            return None; // recycled into a new globreg before any split of the old one
        }
        if p.split_removed.as_ref().map_or(false, |v| v.iter().any(|g| g == id)) {
            return Some(p.n);
        }
    }
    None
}

fn mark_removed(s: &Snap, st: &mut State) {
    let popped = popped_set(s, None);
    // last pass each (globreg, name) was costed in, and last pass each name appears in at all
    let mut home: HashMap<String, u32> = HashMap::new();
    let mut last_name: HashMap<&str, u32> = HashMap::new();
    for p in &s.passes {
        for (id, g) in &p.grs {
            home.insert(format!("{}|{}", id, g.name), p.n);
            let e = last_name.entry(g.name.as_str()).or_insert(0);
            *e = (*e).max(p.n);
        }
    }
    for v in &mut st.vars {
        if v.id.starts_with("new") {
            continue;
        }
        let h = v.pass;
        if popped.contains(&(v.id.clone(), h)) {
            continue;
        }
        let split_later = if has_split_info(s) {
            split_round(s, &v.id, h).is_some()
        } else {
            *last_name.get(v.name.as_str()).unwrap_or(&0) > h
        };
        if !split_later {
            v.removed = true;
        }
    }
}

fn pass<'a>(s: &'a Snap, n: Option<u32>) -> &'a SPass {
    match n {
        Some(n) => s.passes.iter().find(|p| p.n == n).unwrap_or_else(|| die(&format!("no pass {}", n))),
        None => s.passes.iter().find(|p| p.cls == Some(0)).unwrap_or_else(|| &s.passes[0]),
    }
}

fn cmd_check(s: &Snap) {
    let d = defs(s);
    let mut bad = 0;
    let mut nterms = 0;
    let mut states: HashMap<u32, State> = HashMap::new();
    for p in &s.passes {
        let st = State::from_pass(p, &d);
        // every term: cost*P*w or -P*w
        for (id, g) in &p.grs {
            for t in &g.terms {
                let (w, pp) = st.blocks.get(&t.b).copied().unwrap_or((1, 0));
                let want = match t.cost {
                    Some(c) => c * pp * w,
                    None => -pp * w,
                };
                nterms += 1;
                if want != t.delta {
                    bad += 1;
                    if bad <= 10 {
                        println!("pass {} {}#{} block {}: model {} trace {}", p.n, g.name, id, t.b, want, t.delta);
                    }
                }
            }
        }
        // P: in the first pass of a class nothing is coloured, so P must equal the referenced count
        let first_of_class = s.passes.iter().find(|q| q.cls == p.cls).map(|q| q.n) == Some(p.n);
        if first_of_class {
            let mut refd: HashMap<u32, i64> = HashMap::new();
            for v in &st.vars {
                for b in v.cost.keys() {
                    *refd.entry(*b).or_insert(0) += 1;
                }
            }
            for (b, (_, pp)) in &st.blocks {
                let r = *refd.get(b).unwrap_or(&0);
                if r != *pp {
                    bad += 1;
                    if bad <= 10 {
                        println!("pass {} block {}: P {} but {} globregs referenced", p.n, b, pp, r);
                    }
                }
            }
        }
        states.insert(p.n, st);
    }
    // where each (globreg, name) was costed: the passes, in order
    let mut costed: HashMap<String, Vec<u32>> = HashMap::new();
    for p in &s.passes {
        for (id, g) in &p.grs {
            costed.entry(format!("{}|{}", id, g.name)).or_default().push(p.n);
        }
    }
    let index: HashMap<u32, HashMap<String, usize>> = states
        .iter()
        .map(|(n, st)| (*n, st.vars.iter().enumerate().map(|(i, v)| (format!("{}|{}", v.id, v.name), i)).collect()))
        .collect();
    // a re-pop: the same record (id, costing pass) popped again after SpillOrDefer re-inserted it with
    // InsertByBenefit, which copies the neighbour's priority into it (10b11a24, 10b11a63): not a Spillcosts total
    let mut first_pop: BTreeSet<(String, u32)> = BTreeSet::new();
    let repop: Vec<bool> = s
        .pops
        .iter()
        .map(|q| {
            let h = costed_at(s, &q.gr, q.npass).unwrap_or(0);
            !first_pop.insert((q.gr.clone(), h))
        })
        .collect();
    let nrepop = repop.iter().filter(|&&r| r).count();
    // totals of the pass in which each globreg was popped
    let mut ntot = 0;
    let mut nprio = 0;
    for (pi, pop) in s.pops.iter().enumerate() {
        if repop[pi] {
            continue;
        }
        let key = format!("{}|{}", pop.gr, pop.name);
        let pn = costed.get(&key).and_then(|v| v.iter().filter(|&&n| n <= pop.npass).last().copied());
        if let Some(pn) = pn {
            let st = &states[&pn];
            let v = &st.vars[index[&pn][&key]];
            ntot += 1;
            if st.prio(v) != pop.prio {
                nprio += 1;
                if nprio <= 10 {
                    println!(
                        "pop {} {}: model {} (pass {}) trace {}  (re-costed or deferred in between?)",
                        pop.pop,
                        pop.name,
                        st.prio(v),
                        pn,
                        pop.prio
                    );
                }
            }
        }
    }
    bad += nprio;
    // order: for every pass of the int class, the model's list vs the pops made before the next pass
    let mut agree = 0;
    let mut npops = 0;
    let mut first_bad: Option<String> = None;
    for p in s.passes.iter().filter(|p| p.cls == Some(0)) {
        let mut st = State::at_pass(s, p.n, &d);
        mark_removed(s, &mut st);
        set_nums(s, &mut st);
        let pred: Vec<String> =
            st.order().iter().map(|(i, _)| format!("{}|{}", st.vars[*i].id, st.vars[*i].name)).collect();
        // re-pops of this pass: deferred non-immediates in ascending spillCost (a later deferral goes before an
        // equal one), then immediates in the order they were deferred
        let rp: Vec<(usize, &SPop)> =
            s.pops.iter().enumerate().filter(|(i, q)| q.npass == p.n && q.cls == p.cls && repop[*i]).collect();
        if !rp.is_empty() {
            let mut want: Vec<&SPop> = vec![];
            // deferral order = order of their first pops
            let defer_order: Vec<&SPop> = {
                let mut v: Vec<(usize, &SPop)> = rp
                    .iter()
                    .map(|(_, q)| {
                        let h = costed_at(s, &q.gr, q.npass);
                        let first = s
                            .pops
                            .iter()
                            .position(|f| f.gr == q.gr && f.npass <= q.npass && costed_at(s, &f.gr, f.npass) == h)
                            .unwrap_or(0);
                        (first, *q)
                    })
                    .collect();
                v.sort_by_key(|x| x.0);
                v.into_iter().map(|x| x.1).collect()
            };
            for q in &defer_order {
                if q.kind.as_deref() == Some("imm") {
                    continue;
                }
                let b = q.ben.unwrap_or(0);
                let at = want.iter().position(|w| w.kind.as_deref() != Some("imm") && w.ben.unwrap_or(0) >= b);
                match at {
                    Some(k) => want.insert(k, q),
                    None => want.push(q),
                }
            }
            want.extend(defer_order.iter().filter(|q| q.kind.as_deref() == Some("imm")));
            for (k, (_, q)) in rp.iter().enumerate() {
                npops += 1;
                if want.get(k).map(|w| w.pop) == Some(q.pop) {
                    agree += 1;
                } else if first_bad.is_none() {
                    first_bad = Some(format!(
                        "pass {}: re-pop {} is {} but the deferral model's #{} is {}",
                        p.n,
                        q.pop,
                        q.name,
                        k + 1,
                        want.get(k).map(|w| w.name.clone()).unwrap_or("?".into())
                    ));
                }
            }
        }
        let actual: Vec<&SPop> = s
            .pops
            .iter()
            .enumerate()
            .filter(|(i, q)| q.npass == p.n && q.cls == p.cls && !repop[*i])
            .map(|(_, q)| q)
            .collect();
        for (k, q) in actual.iter().enumerate() {
            npops += 1;
            if pred.get(k) == Some(&format!("{}|{}", q.gr, q.name)) {
                agree += 1;
            } else {
                if first_bad.is_none() {
                    first_bad = Some(format!(
                        "pass {}: pop {} is {} (prio {}) but the model's #{} is {}",
                        p.n,
                        q.pop,
                        q.name,
                        q.prio,
                        k + 1,
                        pred.get(k)
                            .and_then(|id| st.vars.iter().find(|v| &format!("{}|{}", v.id, v.name) == id))
                            .map(|v| format!("{} (prio {})", v.name, st.prio(v)))
                            .unwrap_or("?".into())
                    ));
                }
                break; // after a deviation the rest of this pass's list is not comparable
            }
        }
    }
    if let Some(m) = first_bad {
        println!("order: {}", m);
    }
    println!(
        "{}: {} terms, {} pop totals checked; {} mismatches; colouring order: {} of {} int pops predicted \
         ({} re-pops after deferral)",
        s.func, nterms, ntot, bad, agree, npops, nrepop
    );
}

fn show_order(st: &State, base: &State, top: usize) {
    let o = st.order();
    let bo = base.order();
    let brank: HashMap<String, (usize, i64)> =
        bo.iter().enumerate().map(|(k, (i, pr))| (base.vars[*i].id.clone(), (k + 1, *pr))).collect();
    for (k, (i, pr)) in o.iter().enumerate().take(top) {
        let v = &st.vars[*i];
        let note = match brank.get(&v.id) {
            Some((r, bp)) if *r != k + 1 || bp != pr => format!("   (was #{} prio {})", r, bp),
            None => "   (new)".into(),
            _ => String::new(),
        };
        println!("{:4}  {:<24} prio {:6}  def {:5}{}", k + 1, format!("{}#{}", v.name, v.id), pr, v.def_ord, note);
    }
}

fn cmd_order(s: &Snap, n: Option<u32>, top: usize, edits: &[Edit]) {
    let base = state(s, n);
    let mut st = base.clone();
    for e in edits {
        st.apply(e).unwrap_or_else(|m| die(&m));
    }
    show_order(&st, &base, top);
}

/// Constraint "a before b": a's prio greater, or equal with a larger defOrd. Returns the slack (>0 satisfied).
fn slack(st: &State, a: usize, b: usize) -> i64 {
    let (pa, pb) = (st.prio(&st.vars[a]), st.prio(&st.vars[b]));
    if pa != pb {
        pa - pb
    } else if st.vars[a].def_ord > st.vars[b].def_ord {
        1
    } else {
        0
    }
}

fn cmd_need(s: &Snap, n: Option<u32>, cons: &[(String, String)], pre: &[Edit], top: usize) {
    let mut base = state(s, n);
    for e in pre {
        base.apply(e).unwrap_or_else(|m| die(&m));
        println!("given {}", e);
    }
    let idx = |st: &State, c: &(String, String)| -> (usize, usize) {
        (st.find(&c.0).unwrap_or_else(|m| die(&m)), st.find(&c.1).unwrap_or_else(|m| die(&m)))
    };
    let deficit = |st: &State| -> i64 {
        cons.iter()
            .map(|c| {
                let (a, b) = idx(st, c);
                let sl = slack(st, a, b);
                if sl > 0 {
                    0
                } else {
                    1 - sl
                }
            })
            .sum()
    };
    for c in cons {
        let (a, b) = idx(&base, c);
        println!(
            "{} ({}, def {}) before {} ({}, def {}): slack {}{}",
            c.0,
            base.prio(&base.vars[a]),
            base.vars[a].def_ord,
            c.1,
            base.prio(&base.vars[b]),
            base.vars[b].def_ord,
            slack(&base, a, b),
            if slack(&base, a, b) > 0 { "  (holds)" } else { "  (VIOLATED)" }
        );
    }
    let d0 = deficit(&base);
    if d0 == 0 {
        println!("all constraints hold");
        return;
    }
    // candidate single edits: references added/removed for the constrained variables in every block, a new
    // variable in every block (what a named local does), one per block.
    let mut names: Vec<String> = vec![];
    for c in cons {
        for v in [&c.0, &c.1] {
            if !names.contains(v) {
                names.push(v.clone());
            }
        }
    }
    let blocks: Vec<u32> = base.blocks.keys().copied().collect();
    let mut cands: Vec<Edit> = vec![];
    for v in &names {
        let i = base.find(v).unwrap();
        for &b in &blocks {
            // a reference only where the variable is already live: elsewhere it would also stretch its range
            if base.vars[i].cost.contains_key(&b) || base.vars[i].live.contains(&b) {
                cands.push(Edit::Ref(v.clone(), b, 2));
            }
            if base.vars[i].cost.contains_key(&b) {
                cands.push(Edit::Unref(v.clone(), b, true));
            }
        }
    }
    for &b in &blocks {
        cands.push(Edit::NewVar("new".into(), vec![(b, 2)]));
    }
    // Incremental evaluation: an edit touches one block (P, and the edited variable's state there), so a
    // constrained variable's prio changes only by its terms in the touched blocks. O(edits) per evaluation instead
    // of recomputing every priority; equal to the full model (checked against it on the first candidates).
    let cvars: Vec<usize> = {
        let mut v = vec![];
        for c in cons {
            let (a, b) = idx(&base, c);
            for i in [a, b] {
                if !v.contains(&i) {
                    v.push(i);
                }
            }
        }
        v
    };
    let base_prio: HashMap<usize, i64> = cvars.iter().map(|&i| (i, base.prio(&base.vars[i]))).collect();
    let pairs_idx: Vec<(usize, usize)> = cons.iter().map(|c| idx(&base, c)).collect();
    #[derive(Clone, Copy, PartialEq)]
    enum St {
        Ref(i64),
        Live,
        Out,
    }
    let st_of = |i: usize, b: u32| -> St {
        let v = &base.vars[i];
        match v.cost.get(&b) {
            Some(c) => St::Ref(*c),
            None if v.live.contains(&b) => St::Live,
            None => St::Out,
        }
    };
    let term = |st: St, w: i64, p: i64| -> i64 {
        match st {
            St::Ref(c) => c * p * w,
            St::Live => -p * w,
            St::Out => 0,
        }
    };
    let resolve = |e: &Edit| -> Option<(Option<usize>, u32)> {
        match e {
            Edit::Ref(v, b, _) | Edit::Unref(v, b, _) => Some((Some(base.find(v).ok()?), *b)),
            Edit::NewVar(_, r) if r.len() == 1 => Some((None, r[0].0)),
            _ => None,
        }
    };
    let eval_inc = |es: &[&Edit]| -> Option<i64> {
        // per touched block: (w, P) and the constrained variables' states, after all edits
        let mut touched: Vec<u32> = vec![];
        for e in es {
            let (_, b) = resolve(e)?;
            if !touched.contains(&b) {
                touched.push(b);
            }
        }
        let mut prio: HashMap<usize, i64> = base_prio.clone();
        for &b in &touched {
            let mut dp = 0i64; // P change in this block; it applies to every pass's table (see State::bump)
            let mut stv: HashMap<usize, St> = cvars.iter().map(|&i| (i, st_of(i, b))).collect();
            for e in es {
                let (vi, eb) = resolve(e)?;
                if eb != b {
                    continue;
                }
                match (e, vi) {
                    (Edit::Ref(_, _, c), Some(i)) => {
                        let cur = stv.get(&i).copied().unwrap_or_else(|| st_of(i, b));
                        match cur {
                            St::Ref(c0) => {
                                stv.insert(i, St::Ref(c0 + c));
                            }
                            _ => {
                                stv.insert(i, St::Ref(*c));
                                dp += 1;
                            }
                        }
                    }
                    (Edit::Unref(_, _, keep), Some(i)) => {
                        let cur = stv.get(&i).copied().unwrap_or_else(|| st_of(i, b));
                        if let St::Ref(_) = cur {
                            dp -= 1;
                        }
                        stv.insert(i, if *keep { St::Live } else { St::Out });
                    }
                    (Edit::NewVar(..), None) => dp += 1,
                    _ => return None,
                }
            }
            for &i in &cvars {
                // a variable costed in an earlier pass keeps that pass's block table
                let tbl = base.vars[i].own.as_ref().unwrap_or(&base.blocks);
                let (w, p0) = tbl.get(&b).copied().unwrap_or((1, 0));
                let before = term(st_of(i, b), w, p0);
                let after = term(stv[&i], w, p0 + dp);
                *prio.get_mut(&i).unwrap() += after - before;
            }
        }
        Some(
            pairs_idx
                .iter()
                .map(|&(a, b)| {
                    let (pa, pb) = (prio[&a], prio[&b]);
                    let sl = if pa != pb {
                        pa - pb
                    } else if base.vars[a].def_ord > base.vars[b].def_ord {
                        1
                    } else {
                        0
                    };
                    if sl > 0 {
                        0
                    } else {
                        1 - sl
                    }
                })
                .sum(),
        )
    };
    let eval_full = |es: &[&Edit]| -> Option<i64> {
        let mut st = base.clone();
        for e in es {
            st.apply(e).ok()?;
        }
        Some(deficit(&st))
    };
    let step = (cands.len() / 24).max(1);
    for e in cands.iter().step_by(step) {
        if eval_inc(&[e]) != eval_full(&[e]) {
            die(&format!("incremental model disagrees with the full model on {}", e));
        }
    }
    let eval = |es: &[&Edit]| eval_inc(es).or_else(|| eval_full(es));
    let mut singles: Vec<(i64, usize)> =
        cands.iter().enumerate().filter_map(|(k, e)| eval(&[e]).map(|d| (d, k))).collect();
    singles.sort();
    let solved1: Vec<&(i64, usize)> = singles.iter().filter(|x| x.0 == 0).collect();
    println!("{} single edits tried, {} satisfy every constraint (deficit now {}):", cands.len(), solved1.len(), d0);
    let describe = |e: &Edit| -> String {
        let b = match e {
            Edit::Ref(_, b, _) | Edit::Unref(_, b, _) => Some(*b),
            Edit::NewVar(_, r) => r.first().map(|x| x.0),
            _ => None,
        };
        match b.and_then(|b| base.blocks.get(&b).map(|wp| (b, wp))) {
            Some((b, (w, p))) => {
                let mut refd: Vec<&str> =
                    base.vars.iter().filter(|v| v.cost.contains_key(&b)).map(|v| v.name.as_str()).collect();
                refd.truncate(6);
                format!("block {} (w={} P={}; refs {})", b, w, p, refd.join(" "))
            }
            None => String::new(),
        }
    };
    for (d, k) in singles.iter().take(top) {
        println!("  deficit {:4}  {:<28} {}", d, cands[*k].to_string(), describe(&cands[*k]));
    }
    if solved1.is_empty() {
        // pairs among the best 60 singles
        let best: Vec<usize> = singles.iter().take(60).map(|x| x.1).collect();
        let mut pairs: Vec<(i64, usize, usize)> = vec![];
        for (x, &i) in best.iter().enumerate() {
            for &j in &best[x + 1..] {
                if let Some(d) = eval(&[&cands[i], &cands[j]]) {
                    pairs.push((d, i, j));
                }
            }
        }
        pairs.sort();
        let n0 = pairs.iter().filter(|p| p.0 == 0).count();
        println!("{} pairs tried, {} satisfy every constraint:", pairs.len(), n0);
        for (d, i, j) in pairs.iter().take(top) {
            println!("  deficit {:4}  {}  +  {}", d, cands[*i], cands[*j]);
        }
    }
}

fn cmd_blocks(s: &Snap, n: Option<u32>, vars: &[String]) {
    let st = state(s, n);
    let ids: Vec<usize> = vars.iter().map(|v| st.find(v).unwrap_or_else(|m| die(&m))).collect();
    print!("block   w    P");
    for v in vars {
        print!("  {:>12}", v);
    }
    println!();
    for (b, (w, p)) in &st.blocks {
        let cells: Vec<String> = ids
            .iter()
            .map(|&i| {
                let v = &st.vars[i];
                if let Some(c) = v.cost.get(b) {
                    format!("{:+} (c{})", c * p * w, c)
                } else if v.live.contains(b) {
                    format!("{:+} live", -p * w)
                } else {
                    ".".into()
                }
            })
            .collect();
        if cells.iter().all(|c| c == ".") {
            continue;
        }
        print!("{:5} {:3} {:4}", b, w, p);
        for c in cells {
            print!("  {:>12}", c);
        }
        println!();
    }
    print!("total          ");
    for &i in &ids {
        print!("  {:>12}", st.prio(&st.vars[i]));
    }
    println!();
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() < 2 {
        eprintln!("{}", include_str!("main.rs").lines().take_while(|l| l.starts_with("//!")).map(|l| l.trim_start_matches("//!").trim_start_matches(' ')).collect::<Vec<_>>().join("\n"));
        exit(2);
    }
    let s = load(&args[1]);
    let mut n = None;
    let mut top = 30usize;
    let mut rest = vec![];
    let mut it = args[2..].iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--pass" => n = it.next().and_then(|x| x.parse().ok()),
            "--top" | "--edits" => top = it.next().and_then(|x| x.parse().ok()).unwrap_or(top),
            _ => rest.push(a.clone()),
        }
    }
    match args[0].as_str() {
        "check" => cmd_check(&s),
        "order" => {
            let edits: Vec<Edit> = rest.iter().map(|e| parse_edit(e).unwrap_or_else(|m| die(&m))).collect();
            cmd_order(&s, n, top, &edits)
        }
        "need" => {
            let pre: Vec<Edit> =
                rest.iter().filter(|c| !c.contains('>')).map(|e| parse_edit(e).unwrap_or_else(|m| die(&m))).collect();
            let cons: Vec<(String, String)> = rest
                .iter()
                .filter(|c| c.contains('>'))
                .map(|c| {
                    c.split_once('>')
                        .map(|(a, b)| (a.to_string(), b.to_string()))
                        .unwrap_or_else(|| die(&format!("constraint {:?} is not A>B", c)))
                })
                .collect();
            cmd_need(&s, n, &cons, &pre, top)
        }
        "blocks" => cmd_blocks(&s, n, &rest),
        c => die(&format!("unknown command {}", c)),
    }
    let _ = (s.pops.first().map(|p| (&p.reg, p.cls)),);
}
