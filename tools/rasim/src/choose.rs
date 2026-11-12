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
    nbl: Vec<Vec<(usize, bool)>>, // adj[x] and radj[x] merged, ascending, with "in adj[x]"
    ptl: Vec<Vec<(usize, bool)>>, // padj[x] and rpadj[x] merged, ascending, with "in padj[x]"
    bylabel: HashMap<String, usize>, // label(w) -> w for each coloured web that `find_scan` resolves so
    labels: Vec<String>,             // label(w), once the webs are final
    pop_at: Vec<usize>,              // per web: the item that colours it as traced (usize::MAX: none)
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

impl Mods {
    fn dben(&self, w: usize) -> i64 {
        self.ben.iter().filter(|x| x.0 == w).map(|x| x.1).sum()
    }
    fn kill(&self, w: usize) -> u32 {
        self.kills.iter().filter(|x| x.0 == w).fold(0, |k, x| k | x.1)
    }
    fn give(&self, w: usize) -> u32 {
        self.gives.iter().filter(|x| x.0 == w).fold(0, |k, x| k | x.1)
    }
    fn ext_of(&self, w: usize) -> Option<&Vec<usize>> {
        self.ext.iter().find(|x| x.0 == w).map(|x| &x.1)
    }
}

/// out = the union of `a` (ascending, unique) and `add`, ascending and unique: what sorting and deduplicating
/// their concatenation gives.
fn merge_into(a: &[(usize, bool)], add: impl Iterator<Item = (usize, bool)>, out: &mut Vec<(usize, bool)>) {
    let mut b: Vec<(usize, bool)> = add.collect();
    b.sort_unstable();
    b.dedup();
    out.clear();
    let (mut i, mut j) = (0, 0);
    while i < a.len() || j < b.len() {
        let x = match (a.get(i), b.get(j)) {
            (Some(&p), Some(&q)) if p < q => {
                i += 1;
                p
            }
            (Some(&p), Some(&q)) if q < p => {
                j += 1;
                q
            }
            (Some(&p), Some(_)) => {
                i += 1;
                j += 1;
                p
            }
            (Some(&p), None) => {
                i += 1;
                p
            }
            (None, Some(&q)) => {
                j += 1;
                q
            }
            (None, None) => unreachable!(),
        };
        out.push(x);
    }
}

#[derive(Clone)]
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

/// The free simulation's state of one web.
#[derive(Clone)]
struct WebSt {
    avail: u32,
    init_av: u32,
    piece: bool, // an extended web after its split: only the extension's overlaps remain
    live: bool,  // queued and not coloured
    colour: Option<u32>,
    ben: i64,
    prefs: [i64; NREG],
    born: [u32; NREG], // creation order of each PREF node
}

/// The free simulation's state between two steps.
#[derive(Clone)]
pub struct RunSt {
    ws: Vec<WebSt>,
    // a web the trace split is coloured whole in this what-if: its traced pieces (same name, later pass) do not exist
    whole: Vec<(String, u32)>,
    tick: u32,
    nbuf: Vec<usize>,         // scratch: the neighbours of the web being coloured
    mbuf: Vec<(usize, bool)>, // scratch: a neighbour or partner list with a change's additions
}

impl RunSt {
    fn new(n: usize) -> RunSt {
        let w = WebSt { avail: 0, init_av: 0, piece: false, live: false, colour: None, ben: 0, prefs: [0; NREG], born: [0; NREG] };
        RunSt { ws: vec![w; n], whole: vec![], tick: 0, nbuf: vec![], mbuf: vec![] }
    }

    fn colours(&self) -> Vec<Option<u32>> {
        self.ws.iter().map(|w| w.colour).collect()
    }

    /// `hint`: the web the last failed comparison stopped at (it is compared first; on a failure, set to where
    /// this one stopped).
    fn same(&self, o: &RunSt, look: &[(usize, u8)], hint: &mut Option<usize>) -> bool {
        if self.whole != o.whole {
            return false;
        }
        if let Some(h) = *hint {
            if let Ok(i) = look.binary_search_by_key(&h, |x| x.0) {
                if !self.same_web(o, look[i].0, look[i].1) {
                    return false;
                }
            }
        }
        for &(w, f) in look {
            if !self.same_web(o, w, f) {
                *hint = Some(w);
                return false;
            }
        }
        true
    }

    fn same_web(&self, o: &RunSt, w: usize, f: u8) -> bool {
        let (core, pref, adj) = (f & L_CORE != 0, f & L_PREF != 0, f & L_ADJ != 0);
        if f & L_COLOUR != 0 && self.ws[w].colour != o.ws[w].colour {
            return false;
        }
        if (core || adj) && self.ws[w].live != o.ws[w].live {
            return false;
        }
        if !(core || (pref && self.ws[w].colour.is_none()) || (adj && self.ws[w].live)) {
            return true;
        }
        if self.ws[w].avail != o.ws[w].avail
            || self.ws[w].live != o.ws[w].live
            || self.ws[w].ben != o.ws[w].ben
            || self.ws[w].init_av != o.ws[w].init_av
            || self.ws[w].piece != o.ws[w].piece
            || self.ws[w].prefs != o.ws[w].prefs
        {
            return false;
        }
        let (p, a, b) = (&self.ws[w].prefs, &self.ws[w].born, &o.ws[w].born);
        for r in 1..NREG {
            if p[r] <= 0 {
                continue;
            }
            for s in r + 1..NREG {
                if p[s] > 0 && a[r].cmp(&a[s]) != b[r].cmp(&b[s]) {
                    return false;
                }
            }
        }
        true
    }
}

const L_CORE: u8 = 1; // set up or coloured later
const L_PREF: u8 = 2; // a later preference event for it
const L_ADJ: u8 = 4; // a later neighbour or partner, or a later benefit (read only while live)
const L_COLOUR: u8 = 8; // its colour is read later

const T_INIT: usize = 0;
const T_CORE: usize = 1;
const T_PREF: usize = 2;
const T_SRC: usize = 3;
const T_ADJ: usize = 4;
const T_BEN: usize = 5;
const NT: usize = 6;

/// The unedited free simulation over one step order, kept for `Model::run_fast`.
pub struct Trace {
    order: Vec<usize>,
    pub k: usize,             // a checkpoint every k steps
    cps: Vec<RunSt>,          // cps[j]: the state before step j*k
    pops_at: Vec<usize>,      // per step: colourings before it
    inf_at: Vec<usize>,       // per step: inferred-relation lines before it
    out: Outcome,
    first: Vec<[usize; NT]>,  // per web and touch kind (T_*): the first step (usize::MAX: none)
    last1: Vec<[usize; NT]>,  // per web and touch kind: 1 + the last step (0: none)
    look: Vec<Vec<(usize, u8)>>, // per checkpoint j: the webs a step from j*k on reads, with L_* flags
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
        let mut m = Model {
            webs: vec![],
            items: vec![],
            init_seen: vec![],
            adj: vec![],
            padj: vec![],
            radj: vec![],
            rpadj: vec![],
            notes: vec![],
            nbl: vec![],
            ptl: vec![],
            bylabel: HashMap::new(),
            labels: vec![],
            pop_at: vec![],
        };
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
        let merged = |a: &BTreeSet<usize>, b: &BTreeSet<usize>| -> Vec<(usize, bool)> {
            a.union(b).map(|&y| (y, a.contains(&y))).collect()
        };
        m.nbl = (0..n).map(|x| merged(&m.adj[x], &m.radj[x])).collect();
        m.ptl = (0..n).map(|x| merged(&m.padj[x], &m.rpadj[x])).collect();
        // label_scan for every web, from per-name counts
        let mut cnt: HashMap<&str, (usize, usize)> = HashMap::new(); // (coloured, seen with an avail set)
        for (i, x) in m.webs.iter().enumerate() {
            let c = cnt.entry(x.name.as_str()).or_default();
            c.0 += x.pop.is_some() as usize;
            c.1 += m.init_seen[i] as usize;
        }
        m.labels = (0..n)
            .map(|w| {
                let x = &m.webs[w];
                let (col, seen) = cnt[x.name.as_str()];
                match x.pop {
                    None if seen > 1 => format!("{}#{}", x.name, x.gr),
                    Some(p) if col > 1 => format!("{}@p{}", x.name, p),
                    _ => x.name.clone(),
                }
            })
            .collect();
        debug_assert!((0..n).all(|w| m.labels[w] == m.label_scan(w)));
        m.pop_at = vec![usize::MAX; n];
        for (i, it) in m.items.iter().enumerate() {
            if let Item::Pop { web, traced: Some(_), .. } = it {
                if m.pop_at[*web] == usize::MAX {
                    m.pop_at[*web] = i;
                }
            }
        }
        for w in 0..n {
            if m.webs[w].pop.is_some() {
                let l = m.label(w);
                if m.find_scan(&l) == Ok(w) {
                    m.bylabel.insert(l, w);
                }
            }
        }
        Ok(m)
    }

    /// A coloured web by `name`, `name@pN` (the one popped as pop N) or `name#grptr`.
    pub fn find(&self, sel: &str) -> Result<usize, String> {
        if let Some(&w) = self.bylabel.get(sel) {
            return Ok(w);
        }
        self.find_scan(sel)
    }

    fn find_scan(&self, sel: &str) -> Result<usize, String> {
        let col = || (0..self.webs.len()).filter(|&i| self.webs[i].pop.is_some());
        if let Some((n, p)) = sel.split_once("@p") {
            let p: u32 = p.parse().map_err(|_| format!("bad pop number in {}", sel))?;
            return col()
                .find(|&i| self.webs[i].name == n && self.webs[i].pop == Some(p))
                .ok_or(format!("pop {} is not a colouring of {}", p, n));
        }
        if let Some((n, g)) = sel.split_once('#') {
            return col()
                .find(|&i| self.webs[i].name == n && self.webs[i].gr == g)
                .ok_or(format!("no coloured globreg {}", sel));
        }
        let hits: Vec<usize> = col().filter(|&i| self.webs[i].name == sel).collect();
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
        match self.labels.get(w) {
            Some(l) => l.clone(),
            None => self.label_scan(w),
        }
    }

    fn label_scan(&self, w: usize) -> String {
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
        match self.pop_at.get(w) {
            Some(&p) => order.iter().position(|&i| i == n + w || i == p),
            None => order.iter().position(|&i| {
                i == n + w || (i < n && matches!(&self.items[i], Item::Pop { web, traced: Some(_), .. } if *web == w))
            }),
        }
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
        let mut moved = vec![];
        let order = self.order(mods.order.as_ref(), &mods.moves, &mut moved)?;
        let mut st = RunSt::new(self.webs.len());
        let mut out = Outcome { colour: vec![], pops: vec![], inferred: moved };
        for (x, r, c) in &mods.prefs {
            st.ws[*x].prefs[*r as usize] += c;
        }
        for &i in &order {
            self.step(mods, i, &mut st, &mut out)?;
        }
        out.colour = st.colours();
        Ok(out)
    }

    /// One step of the free simulation: item i of the order (or items.len() + web: a colouring the trace does not
    /// have).
    fn step(&self, mods: &Mods, i: usize, st: &mut RunSt, out: &mut Outcome) -> Result<(), String> {
        let has = |v: &[(usize, usize)], a: usize, b: usize| v.iter().any(|&(x, y)| (x, y) == (a, b) || (x, y) == (b, a));
        let RunSt { ws, whole, tick, nbuf, mbuf } = st;
        macro_rules! addpref {
            ($w:expr, $r:expr) => {{
                if ws[$w].prefs[$r as usize] == 0 {
                    *tick += 1;
                    ws[$w].born[$r as usize] = *tick;
                }
                ws[$w].prefs[$r as usize] += 1;
            }};
        }
        let x = if i >= self.items.len() {
            let x = i - self.items.len();
            if !ws[x].live {
                return Ok(());
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
                        return Ok(());
                    }
                    ws[*web].avail = (*a | mods.give(*web)) & !mods.kill(*web);
                    ws[*web].init_av = ws[*web].avail;
                    ws[*web].ben = *b + mods.dben(*web);
                    ws[*web].live = true;
                    return Ok(());
                }
                Item::Ben { web, ben: b } => {
                    ws[*web].ben = *b + mods.dben(*web);
                    return Ok(());
                }
                Item::Requeue { .. } => return Ok(()),
                Item::Pref { web, reg, src, .. } => {
                    if ws[*web].colour.is_some() {
                        return Ok(()); // Spillcosts gives preferences to uncoloured globregs only
                    }
                    let r = match src {
                        None => Some(*reg),
                        Some(sw) if has(&mods.del_c, *web, *sw) => None,
                        Some(sw) => {
                            if ws[*sw].colour.is_none() {
                                out.inferred.push(format!(
                                    "the copy between {} and {} gives no preference: {} is not coloured yet",
                                    self.label(*web),
                                    self.label(*sw),
                                    self.label(*sw)
                                ));
                            }
                            ws[*sw].colour
                        }
                    };
                    if let Some(r) = r {
                        addpref!(*web, r);
                    }
                    return Ok(());
                }
                Item::Restrict { web } => match Self::restrict(ws[*web].avail, &ws[*web].prefs, &ws[*web].born) {
                    Some(r) => {
                        ws[*web].avail = 1 << r;
                        ws[*web].ben = 1;
                        return Ok(());
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
                    if traced.is_none() || !ws[*web].live {
                        return Ok(()); // spilled, deferred or split: as traced; or a piece that does not exist here
                    }
                    *web
                }
            }
        };
        if i >= self.items.len() {
            whole.push((self.webs[x].name.clone(), self.webs[x].pass));
        }
        // the weights are summed as the neighbours are visited (Self::weights, in the same order)
        let mut w = [0i64; NREG];
        let mut nbw = std::mem::take(nbuf);
        nbw.clear();
        // ascending and unique, as a set would give them; the trace's lists are merged once (Model::build), a
        // change's added edges here (this loop is the hot path of `need reg:`, which runs the model once per
        // candidate change)
        let extra = mods.add_e.iter().any(|e| e.0 == x || e.1 == x);
        let mut merged = std::mem::take(mbuf);
        if extra {
            let add = mods.add_e.iter().filter(|e| e.0 == x || e.1 == x).map(|e| if e.0 == x { e.1 } else { e.0 });
            merge_into(&self.nbl[x], add.map(|y| (y, self.adj[x].contains(&y))), &mut merged);
        }
        for &(y, in_adj) in if extra { &merged[..] } else { &self.nbl[x][..] } {
            if y == x || !ws[y].live || has(&mods.del_e, x, y) {
                continue;
            }
            // a split piece of an extended web keeps only the overlaps the extension added
            if ws[y].piece && !mods.ext_of(y).map_or(false, |v| v.contains(&x)) {
                continue;
            }
            if ws[x].piece && !mods.ext_of(x).map_or(false, |v| v.contains(&y)) {
                continue;
            }
            if !in_adj && !has(&mods.add_e, x, y) {
                out.inferred.push(format!(
                    "{} is taken as a neighbour of {} because the trace has {} as a neighbour of {}",
                    self.label(y),
                    self.label(x),
                    self.label(x),
                    self.label(y)
                ));
            }
            let (b, a) = (ws[y].ben, ws[y].avail);
            if b > 0 {
                for r in 1..NREG {
                    if a >> r & 1 == 1 {
                        w[r] += ws[y].prefs[r];
                    }
                }
                if a.count_ones() == 1 {
                    w[a.trailing_zeros() as usize] += 100 * b;
                }
            }
            nbw.push(y);
        }
        for r in 1..NREG {
            w[r] -= ws[x].prefs[r];
        }
        let Some(r) = Self::pick(ws[x].avail, &w) else {
            if mods.ext_of(x).is_some() {
                // the extended web is uncolourable at its turn: c2 splits it (HandleCurrUncolorable).
                // Its pieces are not modelled; the other values go on without it.
                out.inferred.push(format!(
                    "{} has no register left at its turn: c2 splits it; its pieces are not modelled",
                    self.label(x)
                ));
                out.pops.push(PopOut { web: x, avail: 0, w, reg: None });
                ws[x].live = false;
                *nbuf = nbw;
                *mbuf = merged;
                return Ok(());
            }
            return Err(format!("{} is left without a register (c2 would split or spill; not modelled)", self.label(x)));
        };
        *mbuf = merged;
        out.pops.push(PopOut { web: x, avail: ws[x].avail, w, reg: Some(r) });
        ws[x].colour = Some(r);
        ws[x].live = false;
        for &y in &nbw {
            ws[y].avail &= !(1 << r);
            if ws[y].avail != 0 {
                continue;
            }
            // An uncoloured neighbour without a register left is split by c2 (InsertAdditionalSplits). Splits are
            // not simulated, with one exception [I]: a web whose range a what-if extended is continued as the piece
            // covering the extension. That piece starts from the web's initial set, minus the register just taken,
            // minus the registers of the coloured values the extension overlaps (c2 re-costs a piece and subtracts
            // what is live at its defs).
            match mods.ext_of(y) {
                Some(over) if !ws[y].piece => {
                    ws[y].piece = true;
                    let mut a = ws[y].init_av & !(1 << r);
                    for &z in over {
                        if let Some(c) = ws[z].colour {
                            a &= !(1 << c);
                        }
                    }
                    ws[y].avail = a;
                    out.inferred.push(format!(
                        "{} runs out of registers when {} takes {}: c2 splits it; modelled as one piece over the \
                         extended range with {} left",
                        self.label(y),
                        self.label(x),
                        rname(r),
                        mask_names(a)
                    ));
                    if a == 0 {
                        ws[y].live = false;
                    }
                }
                Some(_) => ws[y].live = false, // the piece runs dry too: gone (split again or left in memory)
                None => {}
            }
        }
        *nbuf = nbw;
        let extra = mods.copies.iter().any(|e| e.0 == x || e.1 == x);
        let mut merged = std::mem::take(mbuf);
        if extra {
            let add = mods.copies.iter().filter(|e| e.0 == x || e.1 == x).map(|e| if e.0 == x { e.1 } else { e.0 });
            merge_into(&self.ptl[x], add.map(|y| (y, self.padj[x].contains(&y))), &mut merged);
        }
        for &(y, in_padj) in if extra { &merged[..] } else { &self.ptl[x][..] } {
            if y == x || !ws[y].live || has(&mods.del_c, x, y) {
                continue;
            }
            if !in_padj && !has(&mods.copies, x, y) {
                out.inferred.push(format!(
                    "{} is taken as a copy partner of {} because the trace has the reverse",
                    self.label(y),
                    self.label(x)
                ));
            }
            addpref!(y, r);
        }
        *mbuf = merged;
        Ok(())
    }

    /// Every web step i reads or writes (the colouring's neighbour and partner lists as the trace has them, both
    /// directions). A change confined to webs that a step does not touch cannot alter that step.
    /// f(web, kind) for every web step i reads or writes, by how: T_INIT its queueing, T_BEN its benefit, T_CORE its colouring or RestrictToReg, T_PREF a Spillcosts preference for it (a no-op once it is coloured),
    /// T_SRC the preference's source (its colour is read), T_ADJ a neighbour or partner of the web being coloured
    /// (read and written only while live). A change confined to webs a step does not touch cannot alter it.
    fn touches(&self, i: usize, f: &mut impl FnMut(usize, usize)) {
        let n = self.items.len();
        let x = if i >= n {
            i - n
        } else {
            match &self.items[i] {
                Item::Init { web, .. } => return f(*web, T_INIT),
                Item::Ben { web, .. } => return f(*web, T_BEN),
                Item::Requeue { .. } => return,
                Item::Restrict { web } => return f(*web, T_CORE),
                Item::Pref { web, src, .. } => {
                    f(*web, T_PREF);
                    if let Some(s) = src {
                        f(*s, T_SRC);
                    }
                    return;
                }
                Item::Pop { web, .. } => *web,
            }
        };
        f(x, T_CORE);
        for y in self.adj[x].iter().chain(&self.radj[x]).chain(&self.padj[x]).chain(&self.rpadj[x]) {
            f(*y, T_ADJ);
        }
    }

    /// The unedited free simulation over `order` (None: the traced order), with its state saved every K steps, for
    /// `run_fast`.
    pub fn trace(&self, order: Option<Vec<usize>>) -> Result<Trace, String> {
        let mods = Mods { order, ..Default::default() };
        let mut moved = vec![];
        let order = self.order(mods.order.as_ref(), &[], &mut moved)?;
        let n = self.webs.len();
        let k = (order.len() / 256).max(8);
        let mut st = RunSt::new(n);
        let mut out = Outcome { colour: vec![], pops: vec![], inferred: moved };
        let (mut cps, mut pops_at, mut inf_at) = (vec![], vec![], vec![]);
        let mut first = vec![[usize::MAX; NT]; n];
        let mut last1 = vec![[0usize; NT]; n];
        for (p, &i) in order.iter().enumerate() {
            if p % k == 0 {
                cps.push(st.clone());
            }
            pops_at.push(out.pops.len());
            inf_at.push(out.inferred.len());
            self.touches(i, &mut |w, t| {
                first[w][t] = first[w][t].min(p);
                last1[w][t] = p + 1;
            });
            self.step(&mods, i, &mut st, &mut out)?;
        }
        if order.len() % k == 0 {
            cps.push(st.clone());
        }
        pops_at.push(out.pops.len());
        inf_at.push(out.inferred.len());
        out.colour = st.colours();
        let look = (0..cps.len())
            .map(|j| {
                let q = j * k;
                (0..n)
                    .filter_map(|w| {
                        let later = |t: usize| last1[w][t] > q;
                        let mut f = 0u8;
                        if later(T_INIT) || later(T_CORE) {
                            f |= L_CORE | L_COLOUR;
                        }
                        if later(T_PREF) {
                            f |= L_PREF | L_COLOUR;
                        }
                        if later(T_SRC) {
                            f |= L_COLOUR;
                        }
                        if later(T_ADJ) || later(T_BEN) {
                            f |= L_ADJ;
                        }
                        (f != 0).then_some((w, f))
                    })
                    .collect()
            })
            .collect();
        Ok(Trace { order, k, cps, pops_at, inf_at, out, first, last1, look })
    }

    /// `run` with the same result, from a saved state of the unedited simulation: it starts at the last checkpoint
    /// before the first step the changes can affect (a step that reads a changed web's changed property, or the
    /// first position where the orders differ), and stops at the first checkpoint after the last such step where
    /// everything a later step reads equals the unedited state (the remaining steps are then the unedited ones).
    pub fn run_fast(&self, mods: &Mods, tr: &Trace) -> Result<Outcome, String> {
        let mut moved = vec![];
        let order = self.order(mods.order.as_ref(), &mods.moves, &mut moved)?;
        // Per changed web: the touches from which on the change can matter (they read or write what the change
        // alters; an Init or Ben before them is patched into the restored state below), the touches that read the
        // change itself (it matters until the last of these), and the touches that read it only while the web is
        // live (they do not matter once it is not live and its Init is past: nothing makes it live again).
        struct Ch {
            web: usize,
            from: &'static [usize],
            reads: &'static [usize],
            live: &'static [usize],
        }
        let mut changed: Vec<Ch> = vec![];
        let ch = |web: usize, from: &'static [usize], reads: &'static [usize], live: &'static [usize]| Ch { web, from, reads, live };
        // PREF counts: added to the state at the start; read by the colouring, a preference event, a neighbour
        for x in &mods.prefs {
            changed.push(ch(x.0, &[T_CORE, T_PREF, T_ADJ], &[], &[]));
        }
        // neighbours and copy partners: read when either web is coloured, and only if the other one is live then
        // (see `edges` below)
        let mut edges: Vec<(usize, usize)> = vec![];
        for v in [&mods.add_e, &mods.del_e, &mods.copies] {
            for e in v.iter() {
                changed.push(ch(e.0, &[T_CORE], &[], &[]));
                changed.push(ch(e.1, &[T_CORE], &[], &[]));
                edges.push(*e);
            }
        }
        // a copy removed: also its Spillcosts preference event
        for e in &mods.del_c {
            for w in [e.0, e.1] {
                changed.push(ch(w, &[T_CORE, T_PREF, T_SRC], &[T_CORE, T_PREF, T_SRC], &[]));
            }
        }
        // benefit: applied at Init and Ben, read by a neighbour's colouring (while live), overwritten by
        // RestrictToReg; patched into the state at the start when an Init or Ben came before
        for x in &mods.ben {
            changed.push(ch(x.0, &[T_CORE, T_ADJ], &[T_INIT], &[T_BEN]));
        }
        // killed and given registers: applied at Init (patched in when it came before the start)
        for x in mods.kills.iter().chain(&mods.gives) {
            changed.push(ch(x.0, &[T_CORE, T_ADJ], &[T_INIT], &[]));
        }
        // an extended web: its colouring, and a neighbour's colouring that leaves it without registers
        for x in &mods.ext {
            changed.push(ch(x.0, &[T_CORE, T_ADJ], &[T_CORE], &[T_ADJ]));
        }
        let (ne, nb) = (order.len(), tr.order.len());
        let d = order.iter().zip(&tr.order).position(|(a, b)| a != b).unwrap_or(ne.min(nb));
        let same_tail = order.iter().rev().zip(tr.order.iter().rev()).take_while(|(a, b)| a == b).count();
        let (mut t0, mut free) = (d, 0usize);
        for c in &changed {
            for &t in c.from {
                t0 = t0.min(tr.first[c.web][t]);
            }
            for &t in c.reads {
                free = free.max(tr.last1[c.web][t]);
            }
        }
        let start = (t0 / tr.k) * tr.k;
        let mut st = tr.cps[start / tr.k].clone();
        // No step before t0 reads or writes what a change alters, so the changes can be applied to the state here:
        // PREF counts are still the initial ones; a benefit, avail set or initial set is still what Init or Ben
        // last set (without the change) if one came before.
        for (x, r, c) in &mods.prefs {
            st.ws[*x].prefs[*r as usize] += c;
        }
        let before = |w: usize, t: usize| tr.first[w][t] < start;
        for &(w, d) in &mods.ben {
            if before(w, T_INIT) || before(w, T_BEN) {
                st.ws[w].ben += d;
            }
        }
        let mut done: Vec<usize> = vec![];
        for &(w, _) in mods.kills.iter().chain(&mods.gives) {
            if before(w, T_INIT) && !done.contains(&w) {
                done.push(w);
                st.ws[w].avail = (st.ws[w].avail | mods.give(w)) & !mods.kill(w);
                st.ws[w].init_av = st.ws[w].avail;
            }
        }
        let mut out = Outcome { colour: vec![], pops: tr.out.pops[..tr.pops_at[start]].to_vec(), inferred: moved };
        out.inferred.extend(tr.out.inferred[..tr.inf_at[start]].iter().cloned());
        let tail_from = ne - same_tail; // order[tail_from..] == tr.order[tail_from + nb - ne..]
        let mut hint = None;
        for p in start..ne {
            if p > start && p >= tail_from {
                let q = p + nb - ne;
                if q >= free
                    && q % tr.k == 0
                    && q / tr.k < tr.cps.len()
                    && changed.iter().all(|c| {
                        c.live.iter().all(|&t| tr.last1[c.web][t] <= q)
                            || (!st.ws[c.web].live && tr.last1[c.web][T_INIT] <= q)
                    })
                    // an edge or copy change is read no more once both colourings are past, or once either web is
                    // not live for good (its colouring is skipped, and the other's skips it)
                    && edges.iter().all(|&(a, b)| {
                        let dead = |w: usize| !st.ws[w].live && tr.last1[w][T_INIT] <= q;
                        (tr.last1[a][T_CORE] <= q && tr.last1[b][T_CORE] <= q) || dead(a) || dead(b)
                    })
                    && st.same(&tr.cps[q / tr.k], &tr.look[q / tr.k], &mut hint)
                {
                    let rest = &tr.out.pops[tr.pops_at[q]..];
                    for x in rest {
                        if let Some(r) = x.reg {
                            st.ws[x.web].colour = Some(r);
                        }
                    }
                    out.pops.extend(rest.iter().cloned());
                    out.inferred.extend(tr.out.inferred[tr.inf_at[q]..].iter().cloned());
                    out.colour = st.colours();
                    return Ok(out);
                }
            }
            self.step(mods, order[p], &mut st, &mut out)?;
        }
        out.colour = st.colours();
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
