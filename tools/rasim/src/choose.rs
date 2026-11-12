//! Register choice: a model of c2's ChoosePhysReg (10b14b70) and of the state it reads (docs/c2/regalloc.md
//! section 4), replayed from a `regs --export` snapshot that carries the choice data (pops[].choice, inserts,
//! pref_events).
//!
//! State per globreg ("web": one GLOBREG record between two re-costings; records are recycled):
//!   avail   registers still allowed.  Initial value: what the record holds when InsertInList first queues it (after
//!           Spillcosts removed call kills and fixed registers).  Then: minus the register of every interfering web
//!           coloured while this one was uncoloured (UpdateAffectedGlobregs 10b15470).
//!   prefs   PREF counts.  +1 per Spillcosts event (a copy or lea with a physical register or a coloured globreg;
//!           use-side copies are counted again in every later Spillcosts pass), +1 per copy partner coloured while
//!           this web was uncoloured (_UpdatePrefsBv, 10b15152).
//! Choice for web g with uncoloured interfering neighbours N (the nbrs set InsertSplitPoints builds):
//!   w[r] = sum over n in N with benefit > 0 of (n.prefs[r] if r in n.avail) + (100 * n.benefit if n.avail == {r})
//!          - g.prefs[r];   the lowest w among g.avail wins, ties to the earlier of eax ecx edx esi edi ebx ebp.
//!
//! What a snapshot cannot tell (reported, never guessed silently):
//!   - N and the partner list are recorded as c2 built them, at the moment each web was coloured.  When a what-if
//!     colours A before B although the trace had B first, "B is a neighbour / partner of A" is inferred from
//!     "A was a neighbour / partner of B" (symmetry).  c2's rules are not symmetric for copies (regalloc.md 4.3).
//!   - Splits, spills and new Spillcosts passes are replayed as traced.  A what-if that would move a colouring
//!     across a Spillcosts pass, or that leaves a web without a register, is reported as not decidable.

use super::*;

pub const ORDER: [u32; 7] = [1, 2, 3, 7, 8, 4, 6]; // _IntRegOrder 10ce59a0
const NREG: usize = 9;

pub fn rname(r: u32) -> &'static str {
    match r {
        1 => "eax",
        2 => "ecx",
        3 => "edx",
        4 => "ebx",
        5 => "esp",
        6 => "ebp",
        7 => "esi",
        8 => "edi",
        _ => "r?",
    }
}

pub fn rnum(s: &str) -> Option<u32> {
    (1..=8).find(|&r| rname(r) == s)
}

fn mask(v: &[u32]) -> u32 {
    v.iter().filter(|&&r| r < 32).fold(0, |m, r| m | 1 << r)
}

fn mask_names(m: u32) -> String {
    let v: Vec<&str> = ORDER.iter().filter(|&&r| m >> r & 1 == 1).map(|&r| rname(r)).collect();
    if v.is_empty() {
        "none".into()
    } else {
        v.join(" ")
    }
}

fn prefs_names(p: &[i64; NREG]) -> String {
    let v: Vec<String> =
        ORDER.iter().filter(|&&r| p[r as usize] != 0).map(|&r| format!("{}:{}", rname(r), p[r as usize])).collect();
    if v.is_empty() {
        "-".into()
    } else {
        v.join(" ")
    }
}

fn pref_arr(v: &[(u32, i64)]) -> [i64; NREG] {
    let mut a = [0i64; NREG];
    for &(r, c) in v {
        if (r as usize) < NREG {
            a[r as usize] += c;
        }
    }
    a
}

pub struct Web {
    pub name: String,
    pub gr: String,
    pub pass: u32,
    pub num: Option<u64>,
    pub pop: Option<u32>,    // pop number of the pop that coloured it (traced)
    pub traced: Option<u32>, // the register c2 gave it
    pub prio: i64,
    ins_prefs: Option<[i64; NREG]>, // PREF list when first queued (cross-check of the event model)
}

pub struct TNbr {
    web: usize,
    avail: u32,
    ben: i64,
    prefs: [i64; NREG],
}

/// What the trace recorded at one colouring (for `check`).
pub struct Traced {
    avail: u32,
    prefs: [i64; NREG],
    nbrs: Vec<TNbr>,
    partners: Vec<usize>,
    reg: u32,
}

pub enum Item {
    Init { web: usize, avail: u32, ben: i64 },
    Ben { web: usize, ben: i64 },
    Pref { web: usize, reg: u32, src: Option<usize>, site: String },
    Requeue { web: usize, avail: u32, prefs: [i64; NREG] },
    /// SpillOrDefer -> RestrictToReg (10b1b430): a web with benefit <= 0 whose load cost outweighs that, and which
    /// has a PREF for a register still in its avail set, gets avail = {its most-preferred available register} and
    /// benefit = 1, and goes back to the head of the list (10b0d6a9), so it is coloured next.
    Restrict { web: usize },
    Pop { pi: usize, web: usize, traced: Option<Traced> },
}

pub struct Model {
    pub webs: Vec<Web>,
    pub items: Vec<Item>,
    pub init_seen: Vec<bool>, // the web was seen with an avail set (queued or as a neighbour)
    pub adj: Vec<BTreeSet<usize>>,  // adj[x]: neighbours c2 listed when it coloured x
    pub padj: Vec<BTreeSet<usize>>, // padj[x]: _UpdatePrefsBv partners listed when it coloured x
    radj: Vec<BTreeSet<usize>>, // reverse of adj
    rpadj: Vec<BTreeSet<usize>>,
    pub notes: Vec<String>,
}

#[derive(Clone, Debug)]
pub enum Change {
    Before(String, String), // A is coloured just before B
    After(String, String),
    Interf(String, String),
    NoInterf(String, String),
    Pref(String, u32, i64),
    Copy(String, String),
    NoCopy(String, String),
    NoReg(String, u32),  // A cannot have REG: it lives across something that overwrites REG
    CanReg(String, u32), // the reverse
}

impl std::fmt::Display for Change {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        match self {
            Change::Before(a, b) => write!(f, "before:{}:{}", a, b),
            Change::After(a, b) => write!(f, "after:{}:{}", a, b),
            Change::Interf(a, b) => write!(f, "interf:{}:{}", a, b),
            Change::NoInterf(a, b) => write!(f, "nointerf:{}:{}", a, b),
            Change::Pref(a, r, n) if *n == 1 => write!(f, "pref:{}:{}", a, rname(*r)),
            Change::Pref(a, r, n) => write!(f, "pref:{}:{}:{}", a, rname(*r), n),
            Change::Copy(a, b) => write!(f, "copy:{}:{}", a, b),
            Change::NoCopy(a, b) => write!(f, "nocopy:{}:{}", a, b),
            Change::NoReg(a, r) => write!(f, "noreg:{}:{}", a, rname(*r)),
            Change::CanReg(a, r) => write!(f, "canreg:{}:{}", a, rname(*r)),
        }
    }
}

pub fn is_change(s: &str) -> bool {
    ["before:", "after:", "interf:", "nointerf:", "pref:", "copy:", "nocopy:", "noreg:", "canreg:"]
        .iter()
        .any(|p| s.starts_with(p))
}

pub fn parse_change(s: &str) -> Result<Change, String> {
    let p: Vec<&str> = s.split(':').collect();
    let bad = || {
        format!(
            "bad change {:?} (before:A:B after:A:B interf:A:B nointerf:A:B pref:A:REG[:N] copy:A:B nocopy:A:B \
             noreg:A:REG canreg:A:REG)",
            s
        )
    };
    match (p[0], p.len()) {
        ("before", 3) => Ok(Change::Before(p[1].into(), p[2].into())),
        ("after", 3) => Ok(Change::After(p[1].into(), p[2].into())),
        ("interf", 3) => Ok(Change::Interf(p[1].into(), p[2].into())),
        ("nointerf", 3) => Ok(Change::NoInterf(p[1].into(), p[2].into())),
        ("copy", 3) => Ok(Change::Copy(p[1].into(), p[2].into())),
        ("nocopy", 3) => Ok(Change::NoCopy(p[1].into(), p[2].into())),
        ("noreg", 3) => Ok(Change::NoReg(p[1].into(), rnum(p[2]).ok_or_else(bad)?)),
        ("canreg", 3) => Ok(Change::CanReg(p[1].into(), rnum(p[2]).ok_or_else(bad)?)),
        ("pref", 3) | ("pref", 4) => {
            let r = rnum(p[2]).ok_or_else(bad)?;
            let n = if p.len() == 4 { p[3].parse().map_err(|_| bad())? } else { 1 };
            Ok(Change::Pref(p[1].into(), r, n))
        }
        _ => Err(bad()),
    }
}

/// Changes resolved to web indices.
#[derive(Default, Clone)]
pub struct Mods {
    pub moves: Vec<(usize, usize, bool)>, // (a, b, before): a's colouring moves to just before / after b's
    pub add_e: Vec<(usize, usize)>,
    pub del_e: Vec<(usize, usize)>,
    pub prefs: Vec<(usize, u32, i64)>,
    pub copies: Vec<(usize, usize)>,
    pub del_c: Vec<(usize, usize)>,
    // Source-level edits (combo.rs) arrive as:
    /// the whole step order recomputed from the edited priorities: item indices, or items.len() + web for a
    /// colouring the trace does not have (a web that was split away in the trace and now outranks the split)
    pub order: Option<Vec<usize>>,
    pub ben: Vec<(usize, i64)>,   // benefit deltas (a reference more or fewer)
    pub kills: Vec<(usize, u32)>, // registers a longer range loses to the calls it now crosses
    pub gives: Vec<(usize, u32)>, // registers a shorter range gets back
    /// a web whose range was extended, with the webs it newly overlaps. If it runs out of registers before it is
    /// coloured, c2 splits it; the model keeps the piece that covers the extension (see `run`).
    pub ext: Vec<(usize, Vec<usize>)>,
}

pub struct PopOut {
    pub web: usize,
    pub avail: u32,
    pub w: [i64; NREG],
    pub reg: Option<u32>,
}

pub struct Outcome {
    pub colour: Vec<Option<u32>>,
    pub pops: Vec<PopOut>,
    pub inferred: Vec<String>, // relations used that the trace only shows from the other side
}

#[derive(Default)]
pub struct Check {
    pub pops: usize,
    pub local: usize, // formula applied to the traced inputs
    pub sim: usize,   // formula applied to the simulated state
    pub diverged: usize,
    pub lines: Vec<String>,
}

impl Model {
    pub fn build(s: &Snap) -> Result<Model, String> {
        if !s.pops.iter().any(|p| p.choice.is_some()) {
            return Err("this snapshot has no register-choice data (re-export it with the current tools/c2regs.py)".into());
        }
        let costed = |np: u32, gr: &str| s.passes.iter().any(|p| p.n == np && p.grs.contains_key(gr));
        let name_of = |np: u32, gr: &str| -> String {
            s.passes
                .iter()
                .filter(|p| p.n <= np)
                .filter_map(|p| p.grs.get(gr).map(|g| (p.n, g.name.clone())))
                .max_by_key(|x| x.0)
                .map(|x| x.1)
                .unwrap_or_else(|| "?".into())
        };
        let mut m = Model { webs: vec![], items: vec![], init_seen: vec![], adj: vec![], padj: vec![], radj: vec![], rpadj: vec![], notes: vec![] };
        let mut cur: HashMap<String, usize> = HashMap::new();
        let mut done: Vec<bool> = vec![];
        let mut inited: Vec<bool> = vec![];
        let mut ben: Vec<i64> = vec![];
        // timeline: (after_pop, pass, 0 = preference event / 1 = InsertInList, index)
        let mut tl: Vec<(usize, u32, u8, usize)> = vec![];
        for (i, e) in s.pref_events.iter().flatten().enumerate() {
            if e.reg <= 8 {
                tl.push((e.after_pop, e.npass, 0, i));
            }
        }
        for (i, e) in s.inserts.iter().flatten().enumerate() {
            if e.gr.is_some() && e.avail.as_ref().map_or(true, |a| a.iter().all(|&r| r <= 8)) {
                tl.push((e.after_pop, e.npass, 1, i));
            }
        }
        tl.sort();
        let mut ti = 0;
        macro_rules! new_web {
            ($gr:expr, $np:expr, $num:expr) => {{
                let id = m.webs.len();
                m.webs.push(Web {
                    name: name_of($np, $gr),
                    gr: $gr.to_string(),
                    pass: $np,
                    num: $num,
                    pop: None,
                    traced: None,
                    prio: 0,
                    ins_prefs: None,
                });
                done.push(false);
                inited.push(false);
                ben.push(0);
                m.adj.push(BTreeSet::new());
                m.padj.push(BTreeSet::new());
                cur.insert($gr.to_string(), id);
                id
            }};
        }
        for (k, pop) in s.pops.iter().enumerate() {
            while ti < tl.len() && tl[ti].0 <= k {
                let (_, np, kind, i) = tl[ti];
                ti += 1;
                if kind == 0 {
                    let e = &s.pref_events.as_ref().unwrap()[i];
                    let recost = costed(np, &e.gr);
                    let w = match cur.get(&e.gr) {
                        Some(&w) if !done[w] && !(recost && m.webs[w].pass != np) => w,
                        _ => new_web!(&e.gr, np, e.num.as_ref().and_then(|v| v.as_u64())),
                    };
                    let src = e.src.as_ref().and_then(|g| cur.get(g).copied());
                    m.items.push(Item::Pref { web: w, reg: e.reg, src, site: e.site.clone() });
                } else {
                    let e = &s.inserts.as_ref().unwrap()[i];
                    let gr = e.gr.as_ref().unwrap();
                    let num = e.num.as_ref().and_then(|v| v.as_u64());
                    let recost = costed(np, gr);
                    let w = match cur.get(gr) {
                        Some(&w) if !done[w] && m.webs[w].num == num && !(recost && m.webs[w].pass != np) => w,
                        _ => new_web!(gr, np, num),
                    };
                    let av = mask(e.avail.as_deref().unwrap_or(&[]));
                    let pf = pref_arr(e.prefs.as_deref().unwrap_or(&[]));
                    if !inited[w] {
                        inited[w] = true;
                        ben[w] = e.ben.unwrap_or(0);
                        m.webs[w].ins_prefs = Some(pf);
                        m.items.push(Item::Init { web: w, avail: av, ben: ben[w] });
                    } else {
                        m.items.push(Item::Requeue { web: w, avail: av, prefs: pf });
                    }
                }
            }
            if pop.cls != Some(0) {
                continue;
            }
            let w = match cur.get(&pop.gr) {
                Some(&w) if !done[w] => w,
                _ => new_web!(&pop.gr, pop.npass, pop.num.map(|n| n as u64)),
            };
            m.webs[w].name = pop.name.clone();
            m.webs[w].prio = pop.prio;
            let Some(c) = pop.choice.as_ref() else {
                let next = s.pops.get(k + 1);
                if next.map_or(false, |q| q.gr == pop.gr && q.choice.is_some() && q.cls == Some(0)) && inited[w] {
                    m.items.push(Item::Restrict { web: w });
                    ben[w] = 1;
                }
                m.items.push(Item::Pop { pi: k, web: w, traced: None });
                continue;
            };
            let Some(reg) = pop.reg.as_deref().and_then(rnum) else {
                m.notes.push(format!("pop {} {}: ChoosePhysReg ran but the outcome is {:?}", pop.pop, pop.name, pop.reg));
                m.items.push(Item::Pop { pi: k, web: w, traced: None });
                continue;
            };
            let av = mask(c.avail.as_deref().unwrap_or(&[]));
            if !inited[w] {
                inited[w] = true;
                ben[w] = pop.ben.unwrap_or(0);
                m.notes.push(format!("pop {} {}: never seen entering the priority list; avail taken from its colouring", pop.pop, pop.name));
                m.items.push(Item::Init { web: w, avail: av, ben: ben[w] });
            }
            let mut tn = vec![];
            for n in &c.nbrs {
                let y = match cur.get(&n.gr) {
                    Some(&y) if !done[y] => y,
                    _ => {
                        let y = new_web!(&n.gr, pop.npass, None);
                        m.webs[y].name = n.name.clone();
                        y
                    }
                };
                if m.webs[y].name == "?" {
                    m.webs[y].name = n.name.clone();
                }
                if !inited[y] {
                    inited[y] = true;
                    ben[y] = n.ben;
                    m.notes.push(format!("{} (neighbour at pop {}): never seen entering the priority list", n.name, pop.pop));
                    m.items.push(Item::Init { web: y, avail: mask(&n.avail), ben: n.ben });
                }
                if ben[y] != n.ben {
                    ben[y] = n.ben;
                    m.items.push(Item::Ben { web: y, ben: n.ben });
                }
                m.adj[w].insert(y);
                tn.push(TNbr { web: y, avail: mask(&n.avail), ben: n.ben, prefs: pref_arr(&n.prefs) });
            }
            let mut tp = vec![];
            for q in &c.partners {
                if let Some(&y) = cur.get(&q.gr) {
                    if y != w {
                        m.padj[w].insert(y);
                        if q.coloured != Some(true) {
                            tp.push(y);
                        }
                    }
                }
            }
            done[w] = true;
            m.webs[w].pop = Some(pop.pop);
            m.webs[w].traced = Some(reg);
            m.items.push(Item::Pop {
                pi: k,
                web: w,
                traced: Some(Traced { avail: av, prefs: pref_arr(&c.prefs), nbrs: tn, partners: tp, reg }),
            });
        }
        let n = m.webs.len();
        m.init_seen = inited;
        m.radj = vec![BTreeSet::new(); n];
        m.rpadj = vec![BTreeSet::new(); n];
        // "y lists x" is inferred from "x lists y" only where the trace is silent: x was coloured before y, so when
        // c2 built y's lists x was already coloured (coloured globregs are not neighbours, and a coloured copy
        // partner is listed only from the use side, section 4.3). If y was coloured first, y's own lists are the fact.
        let first = |a: usize, b: usize| match (m.webs[a].pop, m.webs[b].pop) {
            (Some(p), Some(q)) => p < q,
            _ => false,
        };
        for x in 0..n {
            for &y in &m.adj[x] {
                // a web the trace never coloured has no list of its own: everything that listed it counts
                if first(x, y) || m.webs[y].pop.is_none() {
                    m.radj[y].insert(x);
                }
            }
            for &y in &m.padj[x] {
                if first(x, y) {
                    m.rpadj[y].insert(x);
                }
            }
        }
        Ok(m)
    }

    /// A coloured web by `name`, `name@pN` (the one popped as pop N) or `name#grptr`.
    pub fn find(&self, sel: &str) -> Result<usize, String> {
        let col: Vec<usize> = (0..self.webs.len()).filter(|&i| self.webs[i].pop.is_some()).collect();
        if let Some((n, p)) = sel.split_once("@p") {
            let p: u32 = p.parse().map_err(|_| format!("bad pop number in {}", sel))?;
            return col
                .iter()
                .copied()
                .find(|&i| self.webs[i].name == n && self.webs[i].pop == Some(p))
                .ok_or(format!("pop {} is not a colouring of {}", p, n));
        }
        if let Some((n, g)) = sel.split_once('#') {
            return col
                .iter()
                .copied()
                .find(|&i| self.webs[i].name == n && self.webs[i].gr == g)
                .ok_or(format!("no coloured globreg {}", sel));
        }
        let hits: Vec<usize> = col.iter().copied().filter(|&i| self.webs[i].name == sel).collect();
        match hits.len() {
            0 => Err(format!("no coloured globreg named {}", sel)),
            1 => Ok(hits[0]),
            _ => Err(format!(
                "{} is coloured {} times; use {}",
                sel,
                hits.len(),
                hits.iter().map(|&i| self.label(i)).collect::<Vec<_>>().join(" or ")
            )),
        }
    }

    /// Like `find`, but a web the trace never coloured (split away, spilled) is accepted too: `name`, or
    /// `name#grptr` when several uncoloured webs share the name.
    pub fn find_any(&self, sel: &str) -> Result<usize, String> {
        let first = self.find(sel);
        if first.is_ok() || sel.contains("@p") {
            return first;
        }
        let (n, g) = match sel.split_once('#') {
            Some((n, g)) => (n, Some(g)),
            None => (sel, None),
        };
        if g.is_none() && (0..self.webs.len()).any(|i| self.webs[i].pop.is_some() && self.webs[i].name == n) {
            return first;
        }
        let hits: Vec<usize> = (0..self.webs.len())
            .filter(|&i| self.webs[i].pop.is_none() && self.init_seen[i] && self.webs[i].name == n)
            .filter(|&i| g.map_or(true, |g| self.webs[i].gr == g))
            .collect();
        match hits.len() {
            0 => first,
            1 => Ok(hits[0]),
            _ => Err(format!(
                "{} names {} values that were never coloured; use {}",
                sel,
                hits.len(),
                hits.iter().map(|&i| self.label(i)).collect::<Vec<_>>().join(" or ")
            )),
        }
    }

    /// Shortest selector that `find` (or `find_any`) resolves to this web.
    pub fn label(&self, w: usize) -> String {
        let x = &self.webs[w];
        if x.pop.is_none() {
            let same = (0..self.webs.len()).filter(|&i| self.init_seen[i] && self.webs[i].name == x.name).count();
            return if same > 1 { format!("{}#{}", x.name, x.gr) } else { x.name.clone() };
        }
        let same = (0..self.webs.len()).filter(|&i| self.webs[i].pop.is_some() && self.webs[i].name == x.name).count();
        match (same > 1, x.pop) {
            (true, Some(p)) => format!("{}@p{}", x.name, p),
            _ => x.name.clone(),
        }
    }

    pub fn resolve(&self, cs: &[Change]) -> Result<Mods, String> {
        let mut m = Mods::default();
        for c in cs {
            match c {
                Change::Before(a, b) => m.moves.push((self.find(a)?, self.find(b)?, true)),
                Change::After(a, b) => m.moves.push((self.find(a)?, self.find(b)?, false)),
                Change::Interf(a, b) => m.add_e.push((self.find_any(a)?, self.find_any(b)?)),
                Change::NoInterf(a, b) => m.del_e.push((self.find_any(a)?, self.find_any(b)?)),
                Change::Pref(a, r, n) => m.prefs.push((self.find(a)?, *r, *n)),
                Change::Copy(a, b) => m.copies.push((self.find(a)?, self.find(b)?)),
                Change::NoCopy(a, b) => m.del_c.push((self.find(a)?, self.find(b)?)),
                Change::NoReg(a, r) => m.kills.push((self.find_any(a)?, 1 << r)),
                Change::CanReg(a, r) => m.gives.push((self.find_any(a)?, 1 << r)),
            }
        }
        Ok(m)
    }

    fn pop_item(&self, order: &[usize], w: usize) -> Option<usize> {
        let n = self.items.len();
        order.iter().position(|&i| {
            i == n + w || (i < n && matches!(&self.items[i], Item::Pop { web, traced: Some(_), .. } if *web == w))
        })
    }

    /// The item order after the moves. Anything but a pop between the two positions is a Spillcosts pass (a split
    /// round), whose splits depend on what was coloured before it: such a move is simulated with the splits as
    /// traced and flagged as inferred. A web cannot move to before the round that created it.
    fn order(
        &self,
        base: Option<&Vec<usize>>,
        moves: &[(usize, usize, bool)],
        inferred: &mut Vec<String>,
    ) -> Result<Vec<usize>, String> {
        let n = self.items.len();
        let mut o: Vec<usize> = base.cloned().unwrap_or_else(|| (0..n).collect());
        for &(a, b, before) in moves {
            let (ia, ib) = match (self.pop_item(&o, a), self.pop_item(&o, b)) {
                (Some(x), Some(y)) => (x, y),
                _ => return Err("not a coloured globreg".into()),
            };
            let (lo, hi) = (ia.min(ib), ia.max(ib));
            if o[lo..=hi].iter().any(|&i| i < n && !matches!(self.items[i], Item::Pop { .. })) {
                inferred.push(format!(
                    "{} and {} are coloured in different Spillcosts rounds; the splits between them are replayed as traced",
                    self.label(a),
                    self.label(b)
                ));
            }
            let it = o.remove(ia);
            let ib = self.pop_item(&o, b).unwrap();
            let at = if before { ib } else { ib + 1 };
            if o[at..].iter().any(|&i| i < n && matches!(self.items[i], Item::Init { web, .. } if web == a)) {
                return Err(format!("{} does not exist yet when {} is coloured (it is a split piece)", self.label(a), self.label(b)));
            }
            o.insert(at, it);
        }
        Ok(o)
    }

    fn weights(nb: &[(i64, u32, [i64; NREG])], own: &[i64; NREG]) -> [i64; NREG] {
        let mut w = [0i64; NREG];
        for (ben, avail, prefs) in nb {
            if *ben <= 0 {
                continue;
            }
            for r in 1..NREG {
                if avail >> r & 1 == 1 {
                    w[r] += prefs[r];
                }
            }
            if avail.count_ones() == 1 {
                w[avail.trailing_zeros() as usize] += 100 * ben;
            }
        }
        for r in 1..NREG {
            w[r] -= own[r];
        }
        w
    }

    /// RestrictToReg's register: the highest PREF count among the available registers; the PREF list is scanned
    /// from its head and replaced only by a strictly higher count, so an equal count goes to the PREF created last.
    pub fn restrict(avail: u32, prefs: &[i64; NREG], born: &[u32; NREG]) -> Option<u32> {
        let mut best: Option<u32> = None;
        for r in 1..NREG as u32 {
            if avail >> r & 1 == 0 || prefs[r as usize] <= 0 {
                continue;
            }
            let better = best.map_or(true, |b| {
                (prefs[r as usize], born[r as usize]) > (prefs[b as usize], born[b as usize])
            });
            if better {
                best = Some(r);
            }
        }
        best
    }

    fn pick(avail: u32, w: &[i64; NREG]) -> Option<u32> {
        let mut best: Option<u32> = None;
        for &r in &ORDER {
            if avail >> r & 1 == 1 && best.map_or(true, |b| w[r as usize] < w[b as usize]) {
                best = Some(r);
            }
        }
        best
    }

    /// Free simulation: every register is the model's own choice.
    pub fn run(&self, mods: &Mods) -> Result<Outcome, String> {
        let n = self.webs.len();
        let mut moved = vec![];
        let order = self.order(mods.order.as_ref(), &mods.moves, &mut moved)?;
        let dben = |w: usize| -> i64 { mods.ben.iter().filter(|x| x.0 == w).map(|x| x.1).sum() };
        let kill = |w: usize| -> u32 { mods.kills.iter().filter(|x| x.0 == w).fold(0, |k, x| k | x.1) };
        let give = |w: usize| -> u32 { mods.gives.iter().filter(|x| x.0 == w).fold(0, |k, x| k | x.1) };
        let ext_of = |w: usize| mods.ext.iter().find(|x| x.0 == w).map(|x| &x.1);
        let mut init_av = vec![0u32; n];
        let mut piece = vec![false; n]; // an extended web after its split: only the extension's overlaps remain
        // a web the trace split is coloured whole in this what-if: its traced pieces (same name, later pass) do not exist
        let mut whole: Vec<(String, u32)> = vec![];
        let mut avail = vec![0u32; n];
        let mut prefs = vec![[0i64; NREG]; n];
        let mut ben = vec![0i64; n];
        let mut live = vec![false; n]; // queued and not coloured
        let mut born = vec![[0u32; NREG]; n]; // creation order of each PREF node
        let mut tick = 0u32;
        macro_rules! addpref {
            ($w:expr, $r:expr) => {{
                if prefs[$w][$r as usize] == 0 {
                    tick += 1;
                    born[$w][$r as usize] = tick;
                }
                prefs[$w][$r as usize] += 1;
            }};
        }
        let mut colour: Vec<Option<u32>> = vec![None; n];
        let mut out = Outcome { colour: vec![], pops: vec![], inferred: moved };
        let has = |v: &[(usize, usize)], a: usize, b: usize| v.iter().any(|&(x, y)| (x, y) == (a, b) || (x, y) == (b, a));
        for (x, r, c) in &mods.prefs {
            prefs[*x][*r as usize] += c;
        }
        for &i in &order {
            let x = if i >= self.items.len() {
                let x = i - self.items.len();
                if !live[x] {
                    continue;
                }
                out.inferred.push(format!(
                    "{} is coloured here although the trace split it before its turn; its neighbours are those that listed it",
                    self.label(x)
                ));
                x
            } else {
                match &self.items[i] {
                Item::Init { web, avail: a, ben: b } => {
                    if whole.iter().any(|(nm, p)| *nm == self.webs[*web].name && self.webs[*web].pass > *p) {
                        continue;
                    }
                    avail[*web] = (*a | give(*web)) & !kill(*web);
                    init_av[*web] = avail[*web];
                    ben[*web] = *b + dben(*web);
                    live[*web] = true;
                    continue;
                }
                Item::Ben { web, ben: b } => {
                    ben[*web] = *b + dben(*web);
                    continue;
                }
                Item::Requeue { .. } => continue,
                Item::Pref { web, reg, src, .. } => {
                    if colour[*web].is_some() {
                        continue; // Spillcosts gives preferences to uncoloured globregs only
                    }
                    let r = match src {
                        None => Some(*reg),
                        Some(sw) if has(&mods.del_c, *web, *sw) => None,
                        Some(sw) => {
                            if colour[*sw].is_none() {
                                out.inferred.push(format!(
                                    "the copy between {} and {} gives no preference: {} is not coloured yet",
                                    self.label(*web),
                                    self.label(*sw),
                                    self.label(*sw)
                                ));
                            }
                            colour[*sw]
                        }
                    };
                    if let Some(r) = r {
                        addpref!(*web, r);
                    }
                    continue;
                }
                Item::Restrict { web } => match Self::restrict(avail[*web], &prefs[*web], &born[*web]) {
                    Some(r) => {
                        avail[*web] = 1 << r;
                        ben[*web] = 1;
                        continue;
                    }
                    None => {
                        return Err(format!(
                            "{} was restricted to its preferred register in the trace (RestrictToReg) but has no \
                             available preferred register here; c2 would defer or spill it (not modelled)",
                            self.label(*web)
                        ))
                    }
                },
                Item::Pop { web, traced, .. } => {
                    if traced.is_none() || !live[*web] {
                        continue; // spilled, deferred or split: as traced; or a piece that does not exist here
                    }
                    *web
                }
                }
            };
            if i >= self.items.len() {
                whole.push((self.webs[x].name.clone(), self.webs[x].pass));
            }
            {
                {
                    let mut nb = vec![];
                    let mut nbw = vec![];
                    // ascending and unique, as a set would give them (a sorted Vec: this loop is the hot path of
                    // `need reg:`, which runs the model once per candidate change)
                    let mut cand: Vec<usize> = self.adj[x]
                        .iter()
                        .chain(self.radj[x].iter())
                        .copied()
                        .chain(mods.add_e.iter().filter(|e| e.0 == x || e.1 == x).map(|e| if e.0 == x { e.1 } else { e.0 }))
                        .collect();
                    cand.sort_unstable();
                    cand.dedup();
                    for y in cand {
                        if y == x || !live[y] || has(&mods.del_e, x, y) {
                            continue;
                        }
                        // a split piece of an extended web keeps only the overlaps the extension added
                        if piece[y] && !ext_of(y).map_or(false, |v| v.contains(&x)) {
                            continue;
                        }
                        if piece[x] && !ext_of(x).map_or(false, |v| v.contains(&y)) {
                            continue;
                        }
                        if !self.adj[x].contains(&y) && !has(&mods.add_e, x, y) {
                            out.inferred.push(format!(
                                "{} is taken as a neighbour of {} because the trace has {} as a neighbour of {}",
                                self.label(y),
                                self.label(x),
                                self.label(x),
                                self.label(y)
                            ));
                        }
                        nb.push((ben[y], avail[y], prefs[y]));
                        nbw.push(y);
                    }
                    let w = Self::weights(&nb, &prefs[x]);
                    let Some(r) = Self::pick(avail[x], &w) else {
                        if ext_of(x).is_some() {
                            // the extended web is uncolourable at its turn: c2 splits it (HandleCurrUncolorable).
                            // Its pieces are not modelled; the other values go on without it.
                            out.inferred.push(format!(
                                "{} has no register left at its turn: c2 splits it; its pieces are not modelled",
                                self.label(x)
                            ));
                            out.pops.push(PopOut { web: x, avail: 0, w, reg: None });
                            live[x] = false;
                            continue;
                        }
                        return Err(format!("{} is left without a register (c2 would split or spill; not modelled)", self.label(x)));
                    };
                    out.pops.push(PopOut { web: x, avail: avail[x], w, reg: Some(r) });
                    colour[x] = Some(r);
                    live[x] = false;
                    for y in nbw {
                        avail[y] &= !(1 << r);
                        if avail[y] != 0 {
                            continue;
                        }
                        // An uncoloured neighbour without a register left is split by c2 (InsertAdditionalSplits).
                        // Splits are not simulated, with one exception [I]: a web whose range a what-if extended is
                        // continued as the piece covering the extension. That piece starts from the web's initial
                        // set, minus the register just taken, minus the registers of the coloured values the
                        // extension overlaps (c2 re-costs a piece and subtracts what is live at its defs).
                        match ext_of(y) {
                            Some(over) if !piece[y] => {
                                piece[y] = true;
                                let mut a = init_av[y] & !(1 << r);
                                for &z in over {
                                    if let Some(c) = colour[z] {
                                        a &= !(1 << c);
                                    }
                                }
                                avail[y] = a;
                                out.inferred.push(format!(
                                    "{} runs out of registers when {} takes {}: c2 splits it; modelled as one piece over \
                                     the extended range with {} left",
                                    self.label(y),
                                    self.label(x),
                                    rname(r),
                                    mask_names(a)
                                ));
                                if a == 0 {
                                    live[y] = false;
                                }
                            }
                            Some(_) => live[y] = false, // the piece runs dry too: gone (split again or left in memory)
                            None => {}
                        }
                    }
                    let mut part: Vec<usize> = self.padj[x]
                        .iter()
                        .chain(self.rpadj[x].iter())
                        .copied()
                        .chain(mods.copies.iter().filter(|e| e.0 == x || e.1 == x).map(|e| if e.0 == x { e.1 } else { e.0 }))
                        .collect();
                    part.sort_unstable();
                    part.dedup();
                    for y in part {
                        if y == x || !live[y] || has(&mods.del_c, x, y) {
                            continue;
                        }
                        if !self.padj[x].contains(&y) && !has(&mods.copies, x, y) {
                            out.inferred.push(format!(
                                "{} is taken as a copy partner of {} because the trace has the reverse",
                                self.label(y),
                                self.label(x)
                            ));
                        }
                        addpref!(y, r);
                    }
                }
            }
        }
        out.colour = colour;
        Ok(out)
    }

    /// Replay against the trace: at every colouring compare the simulated state with what ChoosePhysReg saw,
    /// predict the register from the simulated state, then continue from the traced state.
    pub fn check(&self, s: &Snap) -> Check {
        let n = self.webs.len();
        let mut avail = vec![0u32; n];
        let mut prefs = vec![[0i64; NREG]; n];
        let mut colour: Vec<Option<u32>> = vec![None; n];
        let mut ck = Check::default();
        let mut started = vec![false; n];
        let mut born = vec![[0u32; NREG]; n];
        let mut tick = 0u32;
        for it in &self.items {
            match it {
                Item::Init { web, avail: a, .. } => {
                    avail[*web] = *a;
                    started[*web] = true;
                    if let Some(ip) = self.webs[*web].ins_prefs {
                        if ip != prefs[*web] {
                            ck.diverged += 1;
                            ck.lines.push(format!(
                                "{} (pass {}): queued with prefs {} but the Spillcosts events give {}",
                                self.webs[*web].name,
                                self.webs[*web].pass,
                                prefs_names(&ip),
                                prefs_names(&prefs[*web])
                            ));
                            prefs[*web] = ip;
                        }
                    }
                }
                Item::Ben { .. } => {}
                Item::Requeue { web, avail: a, prefs: p } => {
                    if avail[*web] != *a || prefs[*web] != *p {
                        ck.diverged += 1;
                        ck.lines.push(format!(
                            "{}: re-queued with avail {} prefs {}; model has {} / {}",
                            self.webs[*web].name,
                            mask_names(*a),
                            prefs_names(p),
                            mask_names(avail[*web]),
                            prefs_names(&prefs[*web])
                        ));
                        avail[*web] = *a;
                        prefs[*web] = *p;
                    }
                }
                Item::Pref { web, reg, src, site } => {
                    if let Some(sw) = src {
                        if colour[*sw] != Some(*reg) {
                            ck.diverged += 1;
                            ck.lines.push(format!(
                                "preference event {} for {}: register {} but its source {} has {}",
                                site,
                                self.webs[*web].name,
                                rname(*reg),
                                self.webs[*sw].name,
                                colour[*sw].map_or("no colour", rname)
                            ));
                        }
                    }
                    if prefs[*web][*reg as usize] == 0 {
                        tick += 1;
                        born[*web][*reg as usize] = tick;
                    }
                    prefs[*web][*reg as usize] += 1;
                }
                Item::Restrict { web } => {
                    if let Some(r) = Self::restrict(avail[*web], &prefs[*web], &born[*web]) {
                        avail[*web] = 1 << r;
                    }
                }
                Item::Pop { pi, web, traced } => {
                    let Some(t) = traced else { continue };
                    let pop = &s.pops[*pi];
                    let x = *web;
                    ck.pops += 1;
                    // the formula on exactly what c2 had
                    let tnb: Vec<(i64, u32, [i64; NREG])> = t.nbrs.iter().map(|q| (q.ben, q.avail, q.prefs)).collect();
                    let lw = Self::weights(&tnb, &t.prefs);
                    if Self::pick(t.avail, &lw) == Some(t.reg) {
                        ck.local += 1;
                    } else {
                        ck.lines.push(format!(
                            "pop {} {}: the formula on the traced inputs gives {} but c2 chose {}",
                            pop.pop,
                            pop.name,
                            Self::pick(t.avail, &lw).map_or("none", rname),
                            rname(t.reg)
                        ));
                    }
                    // the simulated state
                    let mut nb = vec![];
                    for q in &t.nbrs {
                        // c2 reads a neighbour's PREF list only when its benefit is > 0, so only then is it traced
                        if avail[q.web] != q.avail || (q.ben > 0 && prefs[q.web] != q.prefs) {
                            ck.diverged += 1;
                            ck.lines.push(format!(
                                "pop {} {}: neighbour {} has avail {} prefs {}; model has {} / {}",
                                pop.pop,
                                pop.name,
                                self.webs[q.web].name,
                                mask_names(q.avail),
                                prefs_names(&q.prefs),
                                mask_names(avail[q.web]),
                                prefs_names(&prefs[q.web])
                            ));
                        }
                        nb.push((q.ben, avail[q.web], prefs[q.web]));
                        avail[q.web] = q.avail;
                        if q.ben > 0 {
                            prefs[q.web] = q.prefs;
                        }
                    }
                    if avail[x] != t.avail || prefs[x] != t.prefs {
                        ck.diverged += 1;
                        ck.lines.push(format!(
                            "pop {} {}: avail {} prefs {}; model has {} / {}",
                            pop.pop,
                            pop.name,
                            mask_names(t.avail),
                            prefs_names(&t.prefs),
                            mask_names(avail[x]),
                            prefs_names(&prefs[x])
                        ));
                    }
                    let w = Self::weights(&nb, &prefs[x]);
                    let got = Self::pick(avail[x], &w);
                    if got == Some(t.reg) {
                        ck.sim += 1;
                    } else {
                        ck.lines.push(format!(
                            "pop {} {}: model {} but c2 chose {}",
                            pop.pop,
                            pop.name,
                            got.map_or("none", rname),
                            rname(t.reg)
                        ));
                    }
                    colour[x] = Some(t.reg);
                    for q in &t.nbrs {
                        avail[q.web] &= !(1 << t.reg);
                    }
                    for &y in &t.partners {
                        if prefs[y][t.reg as usize] == 0 {
                            tick += 1;
                            born[y][t.reg as usize] = tick;
                        }
                        prefs[y][t.reg as usize] += 1;
                    }
                }
            }
        }
        // the free simulation (what `need reg:` and `regs` use) must give the same registers
        match self.run(&Mods::default()) {
            Ok(o) => {
                let bad: Vec<String> = (0..n)
                    .filter(|&i| self.webs[i].traced.is_some() && o.colour[i] != self.webs[i].traced)
                    .map(|i| self.label(i))
                    .collect();
                if !bad.is_empty() {
                    ck.lines.push(format!("free simulation differs from the trace for: {}", bad.join(" ")));
                }
                if !o.inferred.is_empty() {
                    ck.lines.push(format!("free simulation of the trace itself used {} inferred relations", o.inferred.len()));
                }
            }
            Err(e) => ck.lines.push(format!("free simulation fails: {}", e)),
        }
        ck
    }
}

pub fn cmd_check(s: &Snap) {
    let m = match Model::build(s) {
        Ok(m) => m,
        Err(e) => {
            println!("registers: not checked ({})", e);
            return;
        }
    };
    let ck = m.check(s);
    for l in ck.lines.iter().take(20) {
        println!("registers: {}", l);
    }
    for l in m.notes.iter().take(10) {
        println!("registers: note: {}", l);
    }
    println!(
        "registers: {} of {} int colourings predicted from the simulated state ({} of {} with the traced inputs); \
         {} state divergences",
        ck.sim, ck.pops, ck.local, ck.pops, ck.diverged
    );
}

pub fn show(m: &Model, o: &Outcome, base: Option<&Outcome>) {
    println!("  pop  {:<24} {:<5} {:<5}  cands (weight)", "var", "reg", "trace");
    for p in &o.pops {
        let x = &m.webs[p.web];
        let c: Vec<String> = ORDER
            .iter()
            .filter(|&&r| p.avail >> r & 1 == 1)
            .map(|&r| format!("{}:{}", rname(r), p.w[r as usize]))
            .collect();
        let was = base.and_then(|b| b.colour[p.web]);
        println!(
            "  {:3}  {:<24} {:<5} {:<5}  {}{}",
            x.pop.unwrap_or(0),
            m.label(p.web),
            p.reg.map_or("-", rname),
            x.traced.map_or("-", rname),
            c.join(" "),
            if base.is_some() && was != p.reg { format!("   <- was {}", was.map_or("-", rname)) } else { String::new() }
        );
    }
}
