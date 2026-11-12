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

#[derive(Clone, PartialEq)]
pub struct PopOut {
    pub web: usize,
    pub avail: u32,
    pub w: [i64; NREG],
    pub reg: Option<u32>,
}

/// A line of `[I]` output: a relation the run used that the trace only shows from the other side, or another
/// inference. Kept structured and formatted when shown (`Model::inf_text`).
#[derive(Clone, PartialEq, Eq, Hash)]
pub enum Inf {
    Text(String),
    Whole(usize),                   // x is coloured here although the trace split it before its turn
    Src(usize, usize),              // (web, src): the copy gives no preference, src is not coloured yet
    Nb(usize, usize),               // (y, x): y is taken as a neighbour of x
    Pt(usize, usize),               // (y, x): y is taken as a copy partner of x
    NoReg(usize),                   // an extended web has no register left at its turn
    RunOut(usize, usize, u32, u32), // (y, x, r, left): an extended web runs out when x takes r
}

impl Inf {
    /// One of the lines `need` prints under an answer (the others are only counted).
    pub fn is_note(&self) -> bool {
        matches!(self, Inf::Whole(_) | Inf::NoReg(_) | Inf::RunOut(..))
    }
}

pub struct Outcome {
    pub colour: Vec<Option<u32>>,
    pub pops: Vec<PopOut>,
    pub inferred: Vec<Inf>, // relations used that the trace only shows from the other side
}

/// The free simulation's state of one web.
#[derive(Clone, PartialEq, Default)]
struct WebSt {
    avail: u32,
    init_av: u32,
    piece: bool, // an extended web after its split: only the extension's overlaps remain
    live: bool,  // queued and not coloured
    colour: Option<u32>,
    ben: i64,
    prefs: [i64; NREG],
    born: [u64; NREG], // creation order of each PREF node (only the order within one web is ever read)
}

/// One PREF node more (TICK apart: `run_diff` numbers its own nodes in between, see `Ov::stamp`).
const TICK: u64 = 1 << 24;

/// The free simulation's state between two steps.
pub struct RunSt {
    ws: Vec<WebSt>,
    // webs the trace split that are coloured whole in this what-if: their traced pieces (same name, later pass)
    // do not exist
    whole: Vec<usize>,
    tick: u64,
}

impl RunSt {
    fn new(n: usize) -> RunSt {
        RunSt { ws: vec![WebSt::default(); n], whole: vec![], tick: 0 }
    }

    fn colours(&self) -> Vec<Option<u32>> {
        self.ws.iter().map(|w| w.colour).collect()
    }
}

/// The state a step reads and writes: the full state (`RunSt`), the same while recording which webs a step changes
/// (`Rec`, for `trace`), or an unedited trace's state with a candidate's differences laid over it (`OvSim`).
trait Sim {
    fn get(&self, w: usize) -> &WebSt;
    fn get_mut(&mut self, w: usize) -> &mut WebSt;
    fn whole(&self) -> &[usize];
    fn whole_push(&mut self, w: usize);
    /// a creation number for a new PREF node, above every one given so far
    fn stamp(&mut self) -> u64;
}

impl Sim for RunSt {
    fn get(&self, w: usize) -> &WebSt {
        &self.ws[w]
    }
    fn get_mut(&mut self, w: usize) -> &mut WebSt {
        &mut self.ws[w]
    }
    fn whole(&self) -> &[usize] {
        &self.whole
    }
    fn whole_push(&mut self, w: usize) {
        self.whole.push(w)
    }
    fn stamp(&mut self) -> u64 {
        self.tick += TICK;
        self.tick
    }
}

struct Rec<'a> {
    st: &'a mut RunSt,
    log: Vec<usize>,
}

impl<'a> Sim for Rec<'a> {
    fn get(&self, w: usize) -> &WebSt {
        &self.st.ws[w]
    }
    fn get_mut(&mut self, w: usize) -> &mut WebSt {
        self.log.push(w);
        &mut self.st.ws[w]
    }
    fn whole(&self) -> &[usize] {
        &self.st.whole
    }
    fn whole_push(&mut self, w: usize) {
        self.st.whole.push(w)
    }
    fn stamp(&mut self) -> u64 {
        self.st.stamp()
    }
}

/// Where a step's colouring and inferred lines go.
trait Sink {
    fn pop(&mut self, p: PopOut);
    fn inf(&mut self, l: Inf);
}

impl Sink for Outcome {
    fn pop(&mut self, p: PopOut) {
        self.pops.push(p)
    }
    fn inf(&mut self, l: Inf) {
        self.inferred.push(l)
    }
}

#[derive(Default)]
struct Buf {
    pops: Vec<PopOut>,
    infs: Vec<Inf>,
}

impl Sink for Buf {
    fn pop(&mut self, p: PopOut) {
        self.pops.push(p)
    }
    fn inf(&mut self, l: Inf) {
        self.infs.push(l)
    }
}

/// Scratch lists of a step (reused).
#[derive(Default)]
struct Scratch {
    nbuf: Vec<usize>,         // the neighbours of the web being coloured
    mbuf: Vec<(usize, bool)>, // a neighbour or partner list with a change's additions
}

// How a step touches a web (`Model::touches`), as bits.
const T_INIT: usize = 0; // its queueing
const T_CORE: usize = 1; // its colouring, or RestrictToReg
const T_PREF: usize = 2; // a Spillcosts preference for it
const T_SRC: usize = 3; // the source of one (its colour is read)
const T_ADJ: usize = 4; // a neighbour or partner of the web being coloured (read and written only while live)
const T_BEN: usize = 5; // its benefit
const K_ADJ: u8 = 1 << T_ADJ;
const K_ALL: u8 = 63;
const K_NADJ: u8 = K_ALL & !K_ADJ;
const NONE: u32 = u32::MAX;

/// The unedited free simulation over one step order, kept for `Model::run_diff`: every web's state after every
/// step it changed, and every step that touches each web.
pub struct Trace {
    order: Vec<usize>,
    identity: bool,          // order is 0..items.len()
    pos_of: Vec<u32>,        // item (or items.len() + web) -> its position in order (NONE: not there)
    hoff: Vec<u32>,          // per web: its states hst[hoff[w]..hoff[w + 1]], each holding from step hpos[..] on
    hpos: Vec<u32>,
    hst: Vec<WebSt>,
    wrote_off: Vec<u32>,     // the webs step q changes: wrote[wrote_off[q]..wrote_off[q + 1]]
    wrote: Vec<u32>,
    tick_at: Vec<u64>,       // per step: the PREF creation count before it
    pops_at: Vec<u32>,       // per step: colourings before it
    inf_at: Vec<u32>,        // per step: inferred lines before it
    out: Outcome,
    pop_ix: Vec<u32>,        // per web: its colouring in out.pops (NONE: none)
    tl_off: Vec<u32>,        // per web: the steps that touch it, tl[tl_off[w]..tl_off[w + 1]] = (step, K bits)
    tl: Vec<(u32, u8)>,
    tn_off: Vec<u32>,        // the same without neighbour and partner touches
    tn: Vec<(u32, u8)>,
    inits: Vec<u32>,         // positions of the Init items
    init_max: Vec<u32>,      // per web: the last position of an Init item for it (NONE: none)
    nonpop: Vec<u32>,        // nonpop[q]: items before position q that are not pops
}

impl Trace {
    /// The state of web w before step t.
    fn at(&self, w: usize, t: usize) -> &WebSt {
        let (a, b) = (self.hoff[w] as usize, self.hoff[w + 1] as usize);
        let i = self.hpos[a..b].partition_point(|&p| p as usize <= t);
        &self.hst[a + i - 1]
    }

    fn wrote(&self, q: usize) -> &[u32] {
        &self.wrote[self.wrote_off[q] as usize..self.wrote_off[q + 1] as usize]
    }

    pub fn order(&self) -> &[usize] {
        &self.order
    }

    /// The unedited run's outcome.
    pub fn outcome(&self) -> &Outcome {
        &self.out
    }
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
    pub fn restrict(avail: u32, prefs: &[i64; NREG], born: &[u64; NREG]) -> Option<u32> {
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
        let mut out = Outcome { colour: vec![], pops: vec![], inferred: moved.into_iter().map(Inf::Text).collect() };
        for (x, r, c) in &mods.prefs {
            st.ws[*x].prefs[*r as usize] += c;
        }
        let mut sc = Scratch::default();
        for &i in &order {
            self.step(mods, i, &mut st, &mut sc, &mut out)?;
        }
        out.colour = st.colours();
        Ok(out)
    }

    /// An inferred line as text.
    pub fn inf_text(&self, l: &Inf) -> String {
        match l {
            Inf::Text(s) => s.clone(),
            Inf::Whole(x) => format!(
                "{} is coloured here although the trace split it before its turn; its neighbours are those that listed it",
                self.label(*x)
            ),
            Inf::Src(w, s) => format!(
                "the copy between {} and {} gives no preference: {} is not coloured yet",
                self.label(*w),
                self.label(*s),
                self.label(*s)
            ),
            Inf::Nb(y, x) => format!(
                "{} is taken as a neighbour of {} because the trace has {} as a neighbour of {}",
                self.label(*y),
                self.label(*x),
                self.label(*x),
                self.label(*y)
            ),
            Inf::Pt(y, x) => format!(
                "{} is taken as a copy partner of {} because the trace has the reverse",
                self.label(*y),
                self.label(*x)
            ),
            Inf::NoReg(x) => {
                format!("{} has no register left at its turn: c2 splits it; its pieces are not modelled", self.label(*x))
            }
            Inf::RunOut(y, x, r, a) => format!(
                "{} runs out of registers when {} takes {}: c2 splits it; modelled as one piece over the extended range \
                 with {} left",
                self.label(*y),
                self.label(*x),
                rname(*r),
                mask_names(*a)
            ),
        }
    }

    /// One step of the free simulation: item i of the order (or items.len() + web: a colouring the trace does not
    /// have).
    fn step<S: Sim, K: Sink>(&self, mods: &Mods, i: usize, st: &mut S, sc: &mut Scratch, out: &mut K) -> Result<(), String> {
        let has = |v: &[(usize, usize)], a: usize, b: usize| v.iter().any(|&(x, y)| (x, y) == (a, b) || (x, y) == (b, a));
        fn addpref<S: Sim>(st: &mut S, w: usize, r: u32) {
            let b = if st.get(w).prefs[r as usize] == 0 { Some(st.stamp()) } else { None };
            let s = st.get_mut(w);
            if let Some(b) = b {
                s.born[r as usize] = b;
            }
            s.prefs[r as usize] += 1;
        }
        let x = if i >= self.items.len() {
            let x = i - self.items.len();
            if !st.get(x).live {
                return Ok(());
            }
            out.inf(Inf::Whole(x));
            x
        } else {
            match &self.items[i] {
                Item::Init { web, avail: a, ben: b } => {
                    let (nm, pass) = (&self.webs[*web].name, self.webs[*web].pass);
                    if st.whole().iter().any(|&y| self.webs[y].name == *nm && pass > self.webs[y].pass) {
                        return Ok(());
                    }
                    let s = st.get_mut(*web);
                    s.avail = (*a | mods.give(*web)) & !mods.kill(*web);
                    s.init_av = s.avail;
                    s.ben = *b + mods.dben(*web);
                    s.live = true;
                    return Ok(());
                }
                Item::Ben { web, ben: b } => {
                    st.get_mut(*web).ben = *b + mods.dben(*web);
                    return Ok(());
                }
                Item::Requeue { .. } => return Ok(()),
                Item::Pref { web, reg, src, .. } => {
                    if st.get(*web).colour.is_some() {
                        return Ok(()); // Spillcosts gives preferences to uncoloured globregs only
                    }
                    let r = match src {
                        None => Some(*reg),
                        Some(sw) if has(&mods.del_c, *web, *sw) => None,
                        Some(sw) => {
                            if st.get(*sw).colour.is_none() {
                                out.inf(Inf::Src(*web, *sw));
                            }
                            st.get(*sw).colour
                        }
                    };
                    if let Some(r) = r {
                        addpref(st, *web, r);
                    }
                    return Ok(());
                }
                Item::Restrict { web } => {
                    let s = st.get(*web);
                    match Self::restrict(s.avail, &s.prefs, &s.born) {
                        Some(r) => {
                            let s = st.get_mut(*web);
                            s.avail = 1 << r;
                            s.ben = 1;
                            return Ok(());
                        }
                        None => {
                            return Err(format!(
                                "{} was restricted to its preferred register in the trace (RestrictToReg) but has no \
                                 available preferred register here; c2 would defer or spill it (not modelled)",
                                self.label(*web)
                            ))
                        }
                    }
                }
                Item::Pop { web, traced, .. } => {
                    if traced.is_none() || !st.get(*web).live {
                        return Ok(()); // spilled, deferred or split: as traced; or a piece that does not exist here
                    }
                    *web
                }
            }
        };
        if i >= self.items.len() {
            st.whole_push(x);
        }
        // the weights are summed as the neighbours are visited (Self::weights, in the same order)
        let mut w = [0i64; NREG];
        let mut nbw = std::mem::take(&mut sc.nbuf);
        nbw.clear();
        // ascending and unique, as a set would give them; the trace's lists are merged once (Model::build), a
        // change's added edges here
        let extra = mods.add_e.iter().any(|e| e.0 == x || e.1 == x);
        let mut merged = std::mem::take(&mut sc.mbuf);
        if extra {
            let add = mods.add_e.iter().filter(|e| e.0 == x || e.1 == x).map(|e| if e.0 == x { e.1 } else { e.0 });
            merge_into(&self.nbl[x], add.map(|y| (y, self.adj[x].contains(&y))), &mut merged);
        }
        let xpiece = st.get(x).piece;
        for &(y, in_adj) in if extra { &merged[..] } else { &self.nbl[x][..] } {
            let ys = st.get(y);
            if y == x || !ys.live || has(&mods.del_e, x, y) {
                continue;
            }
            // a split piece of an extended web keeps only the overlaps the extension added
            if ys.piece && !mods.ext_of(y).map_or(false, |v| v.contains(&x)) {
                continue;
            }
            if xpiece && !mods.ext_of(x).map_or(false, |v| v.contains(&y)) {
                continue;
            }
            if !in_adj && !has(&mods.add_e, x, y) {
                out.inf(Inf::Nb(y, x));
            }
            contrib(ys, &mut w, 1);
            nbw.push(y);
        }
        {
            let xs = st.get(x);
            for r in 1..NREG {
                w[r] -= xs.prefs[r];
            }
        }
        let xav = st.get(x).avail;
        let Some(r) = Self::pick(xav, &w) else {
            sc.mbuf = merged;
            if mods.ext_of(x).is_some() {
                // the extended web is uncolourable at its turn: c2 splits it (HandleCurrUncolorable).
                // Its pieces are not modelled; the other values go on without it.
                out.inf(Inf::NoReg(x));
                out.pop(PopOut { web: x, avail: 0, w, reg: None });
                st.get_mut(x).live = false;
                sc.nbuf = nbw;
                return Ok(());
            }
            sc.nbuf = nbw;
            return Err(format!("{} is left without a register (c2 would split or spill; not modelled)", self.label(x)));
        };
        sc.mbuf = merged;
        out.pop(PopOut { web: x, avail: xav, w, reg: Some(r) });
        {
            let xs = st.get_mut(x);
            xs.colour = Some(r);
            xs.live = false;
        }
        for &y in &nbw {
            let ys = st.get_mut(y);
            ys.avail &= !(1 << r);
            if ys.avail != 0 {
                continue;
            }
            // An uncoloured neighbour without a register left is split by c2 (InsertAdditionalSplits). Splits are
            // not simulated, with one exception [I]: a web whose range a what-if extended is continued as the piece
            // covering the extension. That piece starts from the web's initial set, minus the register just taken,
            // minus the registers of the coloured values the extension overlaps (c2 re-costs a piece and subtracts
            // what is live at its defs).
            match mods.ext_of(y) {
                Some(over) if !st.get(y).piece => {
                    let mut a = st.get(y).init_av & !(1 << r);
                    for &z in over {
                        if let Some(c) = st.get(z).colour {
                            a &= !(1 << c);
                        }
                    }
                    let ys = st.get_mut(y);
                    ys.piece = true;
                    ys.avail = a;
                    if a == 0 {
                        ys.live = false;
                    }
                    out.inf(Inf::RunOut(y, x, r, a));
                }
                Some(_) => st.get_mut(y).live = false, // the piece runs dry too: gone (split again or left in memory)
                None => {}
            }
        }
        sc.nbuf = nbw;
        let extra = mods.copies.iter().any(|e| e.0 == x || e.1 == x);
        let mut merged = std::mem::take(&mut sc.mbuf);
        if extra {
            let add = mods.copies.iter().filter(|e| e.0 == x || e.1 == x).map(|e| if e.0 == x { e.1 } else { e.0 });
            merge_into(&self.ptl[x], add.map(|y| (y, self.padj[x].contains(&y))), &mut merged);
        }
        for &(y, in_padj) in if extra { &merged[..] } else { &self.ptl[x][..] } {
            if y == x || !st.get(y).live || has(&mods.del_c, x, y) {
                continue;
            }
            if !in_padj && !has(&mods.copies, x, y) {
                out.inf(Inf::Pt(y, x));
            }
            addpref(st, y, r);
        }
        sc.mbuf = merged;
        Ok(())
    }

    /// Every web step i reads or writes, by how (the T_* kinds): T_INIT its queueing, T_BEN its benefit, T_CORE its
    /// colouring or RestrictToReg, T_PREF a Spillcosts preference for it (a no-op once it is coloured), T_SRC the
    /// preference's source (its colour is read), T_ADJ a neighbour or partner of the web being coloured (read and
    /// written only while live; the lists as the trace has them, both directions). A web a step does not touch is
    /// neither read nor written by it.
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

    /// The unedited free simulation over `order` (None: the traced order), recorded for `run_diff`.
    pub fn trace(&self, order: Option<Vec<usize>>) -> Result<Trace, String> {
        let mods = Mods { order, ..Default::default() };
        let mut moved = vec![];
        let order = self.order(mods.order.as_ref(), &[], &mut moved)?;
        let (n, ni) = (self.webs.len(), self.items.len());
        let mut st = RunSt::new(n);
        let mut out = Outcome { colour: vec![], pops: vec![], inferred: moved.into_iter().map(Inf::Text).collect() };
        let mut hist: Vec<Vec<(u32, WebSt)>> = vec![vec![(0, WebSt::default())]; n];
        let (mut wrote_off, mut wrote, mut tick_at, mut pops_at, mut inf_at) = (vec![], vec![], vec![], vec![], vec![]);
        let mut tl: Vec<Vec<(u32, u8)>> = vec![vec![]; n];
        let mut tn: Vec<Vec<(u32, u8)>> = vec![vec![]; n];
        let (mut inits, mut init_max, mut nonpop) = (vec![], vec![NONE; n], vec![0u32]);
        let mut pos_of = vec![NONE; ni + n];
        let mut sc = Scratch::default();
        let mut seen = vec![false; n];
        for (q, &i) in order.iter().enumerate() {
            let qq = q as u32;
            pos_of[i] = qq;
            tick_at.push(st.tick);
            pops_at.push(out.pops.len() as u32);
            inf_at.push(out.inferred.len() as u32);
            let pop = i >= ni || matches!(self.items[i], Item::Pop { .. });
            nonpop.push(nonpop[q] + !pop as u32);
            if i < ni {
                if let Item::Init { web, .. } = self.items[i] {
                    inits.push(qq);
                    init_max[web] = qq;
                }
            }
            self.touches(i, &mut |w, t| {
                let k = 1u8 << t;
                for l in [&mut tl[w]].into_iter().chain((t != T_ADJ).then_some(&mut tn[w])) {
                    match l.last_mut() {
                        Some(e) if e.0 == qq => e.1 |= k,
                        _ => l.push((qq, k)),
                    }
                }
            });
            let mut rec = Rec { st: &mut st, log: vec![] };
            self.step(&mods, i, &mut rec, &mut sc, &mut out)?;
            let mut log = rec.log;
            log.retain(|&w| !std::mem::replace(&mut seen[w], true));
            wrote_off.push(wrote.len() as u32);
            for w in log {
                seen[w] = false;
                if hist[w].last().map_or(true, |e| e.1 != st.ws[w]) {
                    hist[w].push((qq + 1, st.ws[w].clone()));
                    wrote.push(w as u32);
                }
            }
        }
        tick_at.push(st.tick);
        pops_at.push(out.pops.len() as u32);
        inf_at.push(out.inferred.len() as u32);
        wrote_off.push(wrote.len() as u32);
        out.colour = st.colours();
        let mut pop_ix = vec![NONE; n];
        for (k, p) in out.pops.iter().enumerate().rev() {
            pop_ix[p.web] = k as u32;
        }
        let flat = |v: Vec<Vec<(u32, u8)>>| {
            let mut off = vec![0u32];
            let mut all = vec![];
            for l in v {
                all.extend(l);
                off.push(all.len() as u32);
            }
            (off, all)
        };
        let (mut hoff, mut hpos, mut hst) = (vec![0u32], vec![], vec![]);
        for h in hist {
            for (p, w) in h {
                hpos.push(p);
                hst.push(w);
            }
            hoff.push(hpos.len() as u32);
        }
        let (tl_off, tl) = flat(tl);
        let (tn_off, tn) = flat(tn);
        let identity = order.iter().enumerate().all(|(q, &i)| q == i) && order.len() == ni;
        if !st.whole.is_empty() {
            return Err("the unedited run colours a split web whole".into());
        }
        Ok(Trace {
            order,
            identity,
            pos_of,
            hoff,
            hpos,
            hst,
            wrote_off,
            wrote,
            tick_at,
            pops_at,
            inf_at,
            out,
            pop_ix,
            tl_off,
            tl,
            tn_off,
            tn,
            inits,
            init_max,
            nonpop,
        })
    }

    /// The candidate order as runs aligned with the trace's order, candidate-only steps and trace-only steps.
    fn ops(&self, mods: &Mods, tr: &Trace, same: bool, moved: &mut Vec<String>) -> Result<Vec<Op>, String> {
        let nb = tr.order.len();
        if same || (mods.order.is_none() && mods.moves.is_empty() && tr.identity) {
            return Ok(vec![Op::Al(0, nb)]);
        }
        if mods.order.is_none() && mods.moves.len() == 1 && tr.identity && mods.moves[0].0 != mods.moves[0].1 {
            // `order` for one move on the traced order, without building it
            let (a, b, before) = mods.moves[0];
            let pi = |w: usize| (self.pop_at[w] != usize::MAX).then_some(self.pop_at[w]);
            let (ia, ib) = match (pi(a), pi(b)) {
                (Some(x), Some(y)) => (x, y),
                _ => return Err("not a coloured globreg".into()),
            };
            let (lo, hi) = (ia.min(ib), ia.max(ib));
            if tr.nonpop[hi + 1] > tr.nonpop[lo] {
                moved.push(format!(
                    "{} and {} are coloured in different Spillcosts rounds; the splits between them are replayed as traced",
                    self.label(a),
                    self.label(b)
                ));
            }
            let ibr = if ib > ia { ib - 1 } else { ib };
            let at = if before { ibr } else { ibr + 1 };
            let j = tr.init_max[a];
            if j != NONE {
                let j = j as usize;
                if (if j > ia { j - 1 } else { j }) >= at {
                    return Err(format!(
                        "{} does not exist yet when {} is coloured (it is a split piece)",
                        self.label(a),
                        self.label(b)
                    ));
                }
            }
            let mut v = vec![];
            let al = |v: &mut Vec<Op>, s: usize, e: usize| {
                if e > s {
                    v.push(Op::Al(s, e - s))
                }
            };
            if at == ia {
                al(&mut v, 0, nb);
            } else if at < ia {
                al(&mut v, 0, at);
                v.push(Op::Cand(ia));
                al(&mut v, at, ia);
                v.push(Op::Skip(ia));
                al(&mut v, ia + 1, nb);
            } else {
                al(&mut v, 0, ia);
                v.push(Op::Skip(ia));
                al(&mut v, ia + 1, at + 1);
                v.push(Op::Cand(ia));
                al(&mut v, at + 1, nb);
            }
            return Ok(v);
        }
        let c = self.order(mods.order.as_ref(), &mods.moves, moved)?;
        Ok(DX.with(|d| align(&c, tr, &mut d.borrow_mut().posc)))
    }

    /// `run` with the same result, as differences from the unedited run `tr`: only the steps that touch a web whose
    /// state differs from the trace's, or that a change concerns, are simulated, on the trace's state with the
    /// differences laid over it; everything else is the trace's. `same`: the step order is the trace's.
    pub fn run_diff<'t>(&self, mods: &Mods, tr: &'t Trace, same: bool) -> Result<DiffOut<'t>, String> {
        let mut moved = vec![];
        let ops = self.ops(mods, tr, same, &mut moved)?;
        DX.with(|d| {
            let mut dx = d.borrow_mut();
            let dx = &mut *dx;
            dx.reset(self.webs.len());
            let mut out = DiffOut { tr, pops: vec![], inf: moved.into_iter().map(|s| ISeg::Own(Inf::Text(s))).collect(), cdiff: vec![] };
            let r = Dr { m: self, mods, tr, dx: &mut *dx, out: &mut out }.go(&ops);
            for (w, s) in dx.ov.ents.iter() {
                if s.colour != tr.out.colour[*w] {
                    out.cdiff.push((*w, s.colour));
                }
            }
            out.cdiff.sort_unstable_by_key(|x| x.0);
            dx.clear();
            r.map(|_| out)
        })
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
        let mut born = vec![[0u64; NREG]; n];
        let mut tick = 0u64;
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

/// The weight a live neighbour adds to each register (ChoosePhysReg, regalloc.md 4): its PREF counts on the registers
/// it may have, and 100 x its benefit on its only register; nothing if its benefit is <= 0. `sign` -1 takes it away.
fn contrib(s: &WebSt, w: &mut [i64; NREG], sign: i64) {
    if s.ben > 0 {
        for r in 1..NREG {
            if s.avail >> r & 1 == 1 {
                w[r] += sign * s.prefs[r];
            }
        }
        if s.avail.count_ones() == 1 {
            w[s.avail.trailing_zeros() as usize] += sign * 100 * s.ben;
        }
    }
}

/// c (a candidate's state of a web) behaves as b (the trace's) in every later step: equal, with the PREF nodes
/// created in the same order; or coloured alike and no longer live (nothing reads more than the colour then).
fn eqv(c: &WebSt, b: &WebSt) -> bool {
    if c.colour.is_some() && c.colour == b.colour && !c.live && !b.live {
        return true;
    }
    if c.avail != b.avail
        || c.init_av != b.init_av
        || c.piece != b.piece
        || c.live != b.live
        || c.colour != b.colour
        || c.ben != b.ben
        || c.prefs != b.prefs
    {
        return false;
    }
    for r in 1..NREG {
        if c.prefs[r] <= 0 {
            continue;
        }
        for s in r + 1..NREG {
            if c.prefs[s] > 0 && c.born[r].cmp(&c.born[s]) != b.born[r].cmp(&b.born[s]) {
                return false;
            }
        }
    }
    true
}

/// A candidate step order against the trace's: Al(q, n) the trace's steps q..q+n in the same order; Cand(item) a
/// step the trace does not take here; Skip(q) the trace's step q, which the candidate does not take here.
#[derive(Clone, Copy, Debug)]
enum Op {
    Al(usize, usize),
    Cand(usize),
    Skip(usize),
}

/// Ops for an order c: the common head and tail, and in between a greedy match (an item is taken as moved when its
/// partner position is further away than the other side's).
fn align(c: &[usize], tr: &Trace, posc: &mut Vec<u32>) -> Vec<Op> {
    let b = &tr.order;
    let (nc, nb) = (c.len(), b.len());
    let mut d = 0;
    while d < nc && d < nb && c[d] == b[d] {
        d += 1;
    }
    let mut s = 0;
    while s < nc - d && s < nb - d && c[nc - 1 - s] == b[nb - 1 - s] {
        s += 1;
    }
    let (ce, be) = (nc - s, nb - s);
    let mut ops = vec![];
    if d > 0 {
        ops.push(Op::Al(0, d));
    }
    if posc.len() < tr.pos_of.len() {
        posc.resize(tr.pos_of.len(), NONE);
    }
    for p in d..ce {
        posc[c[p]] = p as u32;
    }
    let (mut p, mut t) = (d, d);
    let mut run: Option<(usize, usize)> = None;
    while p < ce || t < be {
        if p < ce && t < be && c[p] == b[t] {
            run = Some(match run {
                Some((s0, n)) => (s0, n + 1),
                None => (t, 1),
            });
            p += 1;
            t += 1;
            continue;
        }
        if let Some((s0, n)) = run.take() {
            ops.push(Op::Al(s0, n));
        }
        if t >= be {
            ops.push(Op::Cand(c[p]));
            p += 1;
            continue;
        }
        if p >= ce {
            ops.push(Op::Skip(t));
            t += 1;
            continue;
        }
        let (i, j) = (c[p], b[t]);
        let pi = tr.pos_of[i] as usize;
        let pj = posc[j] as usize;
        let pi_ok = tr.pos_of[i] != NONE && pi >= t && pi < be;
        let pj_ok = posc[j] != NONE && pj >= p && pj < ce;
        if !pi_ok || (pj_ok && pi - t > pj - p) {
            ops.push(Op::Cand(i));
            p += 1;
        } else {
            ops.push(Op::Skip(t));
            t += 1;
        }
    }
    if let Some((s0, n)) = run.take() {
        ops.push(Op::Al(s0, n));
    }
    for p in d..ce {
        posc[c[p]] = NONE;
    }
    if s > 0 {
        ops.push(Op::Al(be, s));
    }
    ops
}

/// The candidate's state: the trace's before step t, with the webs in `ents` differing.
#[derive(Default)]
struct Ov {
    t: usize,
    slot: Vec<u32>, // per web: its index in ents (NONE: as the trace)
    ents: Vec<(usize, WebSt)>,
    log: Vec<usize>, // webs written since the last look
    whole: Vec<usize>,
    cnt: u64,
    vc: Vec<std::cell::Cell<(u32, u32)>>, // per web: (run, index into the trace's states) last read
    run: u32,
}

impl Ov {
    /// `Trace::at`, from where the last read of w in this run was (reads move forward in time).
    fn at<'t>(&self, tr: &'t Trace, w: usize, t: usize) -> &'t WebSt {
        let (a, b) = (tr.hoff[w] as usize, tr.hoff[w + 1] as usize);
        let (r, i) = self.vc[w].get();
        let mut i = i as usize;
        if r != self.run || i < a || i >= b || tr.hpos[i] as usize > t {
            i = a + tr.hpos[a..b].partition_point(|&p| p as usize <= t) - 1;
        } else {
            while i + 1 < b && tr.hpos[i + 1] as usize <= t {
                i += 1;
            }
        }
        self.vc[w].set((self.run, i as u32));
        &tr.hst[i]
    }
}

struct OvSim<'a> {
    tr: &'a Trace,
    ov: &'a mut Ov,
}

impl<'a> Sim for OvSim<'a> {
    fn get(&self, w: usize) -> &WebSt {
        match self.ov.slot[w] {
            NONE => self.ov.at(self.tr, w, self.ov.t),
            s => &self.ov.ents[s as usize].1,
        }
    }
    fn get_mut(&mut self, w: usize) -> &mut WebSt {
        let ov = &mut *self.ov;
        if ov.slot[w] == NONE {
            ov.slot[w] = ov.ents.len() as u32;
            ov.ents.push((w, ov.at(self.tr, w, ov.t).clone()));
        }
        ov.log.push(w);
        &mut ov.ents[ov.slot[w] as usize].1
    }
    fn whole(&self) -> &[usize] {
        &self.ov.whole // the trace's is empty (`Model::trace`)
    }
    fn whole_push(&mut self, w: usize) {
        self.ov.whole.push(w)
    }
    /// above every node of the trace before step t, below every node the trace creates from step t on
    fn stamp(&mut self) -> u64 {
        self.ov.cnt += 1;
        assert!(self.ov.cnt < TICK);
        self.tr.tick_at[self.ov.t] + self.ov.cnt
    }
}

/// Per-thread scratch of `run_diff` (sized once, cleared after each run).
#[derive(Default)]
struct Dx {
    ov: Ov,
    cmask: Vec<u8>,  // per web: the touches a change concerns
    cfull: Vec<bool>, // per web: its colouring is simulated in full (a change alters its lists)
    wmask: Vec<u8>,  // per web: the touches watched (cmask, and every touch while its state differs)
    cur_a: Vec<u32>, // per web: cursors into its touch lists
    cur_n: Vec<u32>,
    inwl: Vec<bool>,
    wl: Vec<usize>, // webs with any of the above set
    heap: std::collections::BinaryHeap<std::cmp::Reverse<(u32, u32)>>,
    gen: Vec<u32>,
    g: u32,
    s: Vec<usize>,              // the watched webs the current step touches
    extra: Vec<(usize, usize)>, // added edges, both ways, sorted
    sc: Scratch,
    buf: Buf,
    posc: Vec<u32>,
    look: Vec<Look>,
    lines: (Vec<usize>, Vec<usize>),
}

thread_local! {
    static DX: std::cell::RefCell<Dx> = std::cell::RefCell::new(Dx::default());
}

#[derive(Clone, Copy)]
struct Look {
    y: usize,
    inc_c: bool, // a neighbour that counts, in the candidate / in the trace
    inc_b: bool,
    part_c: bool, // a live copy partner
    part_b: bool,
    line_c: bool, // an inferred-neighbour line
    line_b: bool,
    pline_c: bool, // an inferred-partner line
    pline_b: bool,
}

impl Dx {
    fn reset(&mut self, n: usize) {
        if self.ov.slot.len() < n {
            self.ov.slot.resize(n, NONE);
            self.cmask.resize(n, 0);
            self.cfull.resize(n, false);
            self.wmask.resize(n, 0);
            self.cur_a.resize(n, 0);
            self.cur_n.resize(n, 0);
            self.inwl.resize(n, false);
            self.gen.resize(n, 0);
        }
        if self.ov.vc.len() < n {
            self.ov.vc.resize(n, std::cell::Cell::new((0, 0)));
        }
        self.ov.run = self.ov.run.wrapping_add(1);
        if self.ov.run == 0 {
            self.ov.vc.iter().for_each(|c| c.set((0, 0)));
            self.ov.run = 1;
        }
        self.ov.t = 0;
        self.ov.cnt = 0;
    }

    fn clear(&mut self) {
        for &w in &self.wl {
            self.cmask[w] = 0;
            self.cfull[w] = false;
            self.wmask[w] = 0;
            self.cur_a[w] = 0;
            self.cur_n[w] = 0;
            self.inwl[w] = false;
        }
        self.wl.clear();
        for (w, _) in &self.ov.ents {
            self.ov.slot[*w] = NONE;
        }
        self.ov.ents.clear();
        self.ov.log.clear();
        self.ov.whole.clear();
        self.heap.clear();
        self.extra.clear();
        self.s.clear();
    }

    fn reg(&mut self, w: usize) {
        if !self.inwl[w] {
            self.inwl[w] = true;
            self.wl.push(w);
        }
    }

    fn next_gen(&mut self) -> u32 {
        self.g = self.g.wrapping_add(1);
        if self.g == 0 {
            self.gen.iter_mut().for_each(|x| *x = 0);
            self.g = 1;
        }
        self.g
    }

    /// The first step from t on that touches w in a watched way.
    fn next_touch(&mut self, tr: &Trace, w: usize, t: usize) -> Option<usize> {
        let m = self.wmask[w];
        if m == 0 {
            return None;
        }
        let (l, cur) = if m & K_ADJ == 0 {
            (&tr.tn[tr.tn_off[w] as usize..tr.tn_off[w + 1] as usize], &mut self.cur_n[w])
        } else {
            (&tr.tl[tr.tl_off[w] as usize..tr.tl_off[w + 1] as usize], &mut self.cur_a[w])
        };
        let mut c = *cur as usize;
        while c < l.len() && (l[c].0 as usize) < t {
            c += 1;
        }
        *cur = c as u32;
        while c < l.len() && l[c].1 & m == 0 {
            c += 1;
        }
        l.get(c).map(|e| e.0 as usize)
    }

    fn push(&mut self, tr: &Trace, w: usize, t: usize) {
        if let Some(p) = self.next_touch(tr, w, t) {
            self.heap.push(std::cmp::Reverse((p as u32, w as u32)));
        }
    }

    /// Watch w as its state now requires.
    fn upd(&mut self, tr: &Trace, w: usize) {
        let t = self.ov.t;
        let dm = match self.ov.slot[w] {
            NONE => 0,
            s => {
                if self.ov.ents[s as usize].1.live || self.ov.at(tr, w, t).live {
                    K_ALL
                } else {
                    K_NADJ
                }
            }
        };
        let m = self.cmask[w] | dm;
        if m != self.wmask[w] {
            self.reg(w);
            self.wmask[w] = m;
            self.push(tr, w, t);
        }
    }

    /// Drop w from the overlay if it now behaves as the trace's (before step ov.t), and watch it accordingly.
    fn clean(&mut self, tr: &Trace, w: usize) {
        let s = self.ov.slot[w];
        if s != NONE && eqv(&self.ov.ents[s as usize].1, self.ov.at(tr, w, self.ov.t)) {
            self.ov.ents.swap_remove(s as usize);
            if (s as usize) < self.ov.ents.len() {
                let moved = self.ov.ents[s as usize].0;
                self.ov.slot[moved] = s;
            }
            self.ov.slot[w] = NONE;
        }
        self.upd(tr, w);
    }

    fn clean_log(&mut self, tr: &Trace) {
        let g = self.next_gen();
        let log = std::mem::take(&mut self.ov.log);
        for &w in &log {
            if self.gen[w] != g {
                self.gen[w] = g;
                self.clean(tr, w);
            }
        }
        self.ov.log = log;
        self.ov.log.clear();
    }

    /// The first step in t..e that touches a watched web as watched (or an Init step while `whole` differs).
    fn next_event(&mut self, tr: &Trace, t: usize, e: usize) -> Option<usize> {
        let mut best = None;
        while let Some(&std::cmp::Reverse((p, w))) = self.heap.peek() {
            let (p, w) = (p as usize, w as usize);
            if p >= e {
                break;
            }
            let nt = self.next_touch(tr, w, t);
            if nt == Some(p) {
                best = Some(p);
                break;
            }
            self.heap.pop();
            if let Some(q) = nt {
                self.heap.push(std::cmp::Reverse((q as u32, w as u32)));
            }
        }
        if !self.ov.whole.is_empty() {
            let k = tr.inits.partition_point(|&p| (p as usize) < t);
            if let Some(&p) = tr.inits.get(k) {
                if (p as usize) < e && best.map_or(true, |b| (p as usize) < b) {
                    best = Some(p as usize);
                }
            }
        }
        best
    }

    /// The watched webs step q touches, taken off the heap (into self.s).
    fn collect(&mut self, tr: &Trace, q: usize) {
        self.s.clear();
        let g = self.next_gen();
        while let Some(&std::cmp::Reverse((p, w))) = self.heap.peek() {
            let (p, w) = (p as usize, w as usize);
            if p != q {
                break;
            }
            self.heap.pop();
            if self.gen[w] == g {
                continue;
            }
            match self.next_touch(tr, w, q) {
                Some(p) if p == q => {
                    self.gen[w] = g;
                    self.s.push(w);
                }
                Some(p) => self.heap.push(std::cmp::Reverse((p as u32, w as u32))),
                None => {}
            }
        }
    }
}

pub enum PSeg {
    Base(u32, u32), // the trace's colourings a..b
    Own(PopOut),
}

pub enum ISeg {
    Base(u32, u32),
    Own(Inf),
}

/// `run`'s outcome as differences from a trace's.
pub struct DiffOut<'t> {
    pub tr: &'t Trace,
    pub pops: Vec<PSeg>,
    pub inf: Vec<ISeg>,
    pub cdiff: Vec<(usize, Option<u32>)>, // webs whose final colour differs from the trace's, ascending
}

impl<'t> DiffOut<'t> {
    pub fn colour(&self, w: usize) -> Option<u32> {
        match self.cdiff.binary_search_by_key(&w, |x| x.0) {
            Ok(k) => self.cdiff[k].1,
            Err(_) => self.tr.out.colour[w],
        }
    }

    pub fn first_pop(&self, w: usize) -> Option<&PopOut> {
        let k = self.tr.pop_ix[w];
        for s in &self.pops {
            match s {
                PSeg::Base(a, b) => {
                    if k != NONE && *a <= k && k < *b {
                        return Some(&self.tr.out.pops[k as usize]);
                    }
                }
                PSeg::Own(p) => {
                    if p.web == w {
                        return Some(p);
                    }
                }
            }
        }
        None
    }

    pub fn pops(&self) -> impl Iterator<Item = &PopOut> {
        self.pops.iter().flat_map(move |s| -> Box<dyn Iterator<Item = &PopOut>> {
            match s {
                PSeg::Base(a, b) => Box::new(self.tr.out.pops[*a as usize..*b as usize].iter()),
                PSeg::Own(p) => Box::new(std::iter::once(p)),
            }
        })
    }

    pub fn infs(&self) -> impl Iterator<Item = &Inf> {
        self.inf.iter().flat_map(move |s| -> Box<dyn Iterator<Item = &Inf>> {
            match s {
                ISeg::Base(a, b) => Box::new(self.tr.out.inferred[*a as usize..*b as usize].iter()),
                ISeg::Own(l) => Box::new(std::iter::once(l)),
            }
        })
    }

    /// Some pair of colourings at the same index differs (the shorter list's length is compared); `alias`: a and
    /// b's traces have the same colourings, so equal trace ranges at equal indices are skipped.
    pub fn pops_differ(a: &DiffOut, b: &DiffOut, alias: bool) -> bool {
        let same_tr = alias || std::ptr::eq(a.tr, b.tr);
        let (mut i, mut j) = ((0usize, 0u32), (0usize, 0u32)); // (segment, offset in it)
        let len = |s: &PSeg| match s {
            PSeg::Base(x, y) => y - x,
            PSeg::Own(_) => 1,
        };
        loop {
            // skip empty segments
            while i.0 < a.pops.len() && i.1 >= len(&a.pops[i.0]) {
                i = (i.0 + 1, 0);
            }
            while j.0 < b.pops.len() && j.1 >= len(&b.pops[j.0]) {
                j = (j.0 + 1, 0);
            }
            if i.0 >= a.pops.len() || j.0 >= b.pops.len() {
                return false;
            }
            match (&a.pops[i.0], &b.pops[j.0]) {
                (PSeg::Base(x, y), PSeg::Base(u, v)) if same_tr && x + i.1 == u + j.1 => {
                    let k = (y - x - i.1).min(v - u - j.1);
                    i.1 += k;
                    j.1 += k;
                }
                (sa, sb) => {
                    let pa = match sa {
                        PSeg::Base(x, _) => &a.tr.out.pops[(x + i.1) as usize],
                        PSeg::Own(p) => p,
                    };
                    let pb = match sb {
                        PSeg::Base(u, _) => &b.tr.out.pops[(u + j.1) as usize],
                        PSeg::Own(p) => p,
                    };
                    if pa.web != pb.web || pa.avail != pb.avail || pa.w != pb.w {
                        return true;
                    }
                    i.1 += 1;
                    j.1 += 1;
                }
            }
        }
    }
}

/// One `run_diff`.
struct Dr<'a, 't> {
    m: &'a Model,
    mods: &'a Mods,
    tr: &'t Trace,
    dx: &'a mut Dx,
    out: &'a mut DiffOut<'t>,
}

impl<'a, 't> Dr<'a, 't> {
    fn emit_pops(&mut self, q0: usize, q1: usize) {
        let (a, b) = (self.tr.pops_at[q0], self.tr.pops_at[q1]);
        if a < b {
            match self.out.pops.last_mut() {
                Some(PSeg::Base(_, y)) if *y == a => *y = b,
                _ => self.out.pops.push(PSeg::Base(a, b)),
            }
        }
    }

    fn emit_inf(&mut self, q0: usize, q1: usize) {
        let (a, b) = (self.tr.inf_at[q0], self.tr.inf_at[q1]);
        if a < b {
            match self.out.inf.last_mut() {
                Some(ISeg::Base(_, y)) if *y == a => *y = b,
                _ => self.out.inf.push(ISeg::Base(a, b)),
            }
        }
    }

    fn go(&mut self, ops: &[Op]) -> Result<(), String> {
        let (mods, tr) = (self.mods, self.tr);
        let dx = &mut *self.dx;
        {
            let cm = |dx: &mut Dx, w: usize, k: u8, full: bool| {
                dx.reg(w);
                dx.cmask[w] |= k;
                dx.cfull[w] |= full;
            };
            let k = |t: usize| 1u8 << t;
            for &(w, _) in &mods.ben {
                cm(dx, w, k(T_INIT) | k(T_BEN), false);
            }
            for &(w, _) in mods.kills.iter().chain(&mods.gives) {
                cm(dx, w, k(T_INIT), false);
            }
            for &(a, b) in &mods.add_e {
                cm(dx, a, k(T_CORE), false);
                cm(dx, b, k(T_CORE), false);
                dx.extra.push((a, b));
                dx.extra.push((b, a));
            }
            for &(a, b) in mods.del_e.iter().chain(&mods.copies) {
                cm(dx, a, k(T_CORE), true);
                cm(dx, b, k(T_CORE), true);
            }
            for &(a, b) in &mods.del_c {
                cm(dx, a, k(T_CORE) | k(T_PREF) | k(T_SRC), true);
                cm(dx, b, k(T_CORE) | k(T_PREF) | k(T_SRC), true);
            }
            for (w, _) in &mods.ext {
                cm(dx, *w, k(T_CORE) | K_ADJ, true);
            }
            dx.extra.sort_unstable();
            dx.extra.dedup();
            for &(x, r, c) in &mods.prefs {
                OvSim { tr, ov: &mut dx.ov }.get_mut(x).prefs[r as usize] += c;
            }
            dx.ov.log.clear();
            let ws: Vec<usize> = dx.wl.iter().copied().chain(dx.ov.ents.iter().map(|e| e.0)).collect();
            for w in ws {
                dx.upd(tr, w);
            }
        }
        for op in ops {
            match *op {
                Op::Al(s, n) => self.aligned(s, s + n)?,
                Op::Cand(i) => {
                    let mut buf = std::mem::take(&mut self.dx.buf);
                    let r = self.m.step(mods, i, &mut OvSim { tr, ov: &mut self.dx.ov }, &mut self.dx.sc, &mut buf);
                    for p in buf.pops.drain(..) {
                        self.out.pops.push(PSeg::Own(p));
                    }
                    for l in buf.infs.drain(..) {
                        self.out.inf.push(ISeg::Own(l));
                    }
                    self.dx.buf = buf;
                    r?;
                    self.dx.clean_log(tr);
                }
                Op::Skip(q) => {
                    self.dx.ov.t = q;
                    for &w in tr.wrote(q) {
                        OvSim { tr, ov: &mut self.dx.ov }.get_mut(w as usize);
                    }
                    self.dx.ov.t = q + 1;
                    self.dx.clean_log(tr);
                }
            }
        }
        Ok(())
    }

    fn aligned(&mut self, s: usize, e: usize) -> Result<(), String> {
        let tr = self.tr;
        let mut t = s;
        self.dx.ov.t = s;
        loop {
            match self.dx.next_event(tr, t, e) {
                None => {
                    self.emit_pops(t, e);
                    self.emit_inf(t, e);
                    self.dx.ov.t = e;
                    return Ok(());
                }
                Some(q) => {
                    self.emit_pops(t, q);
                    self.emit_inf(t, q);
                    self.dx.ov.t = q;
                    self.dx.collect(tr, q);
                    let i = tr.order[q];
                    let done = match self.m.items.get(i) {
                        Some(Item::Pop { web, traced: Some(_), .. }) => self.incr_pop(q, *web),
                        _ => false,
                    };
                    if !done {
                        self.full(q, i)?;
                    }
                    t = q + 1;
                    self.dx.ov.t = t;
                    let s = std::mem::take(&mut self.dx.s);
                    for &w in &s {
                        self.dx.push(tr, w, t);
                    }
                    self.dx.s = s;
                }
            }
        }
    }

    /// Step q simulated on the overlay: the webs the trace's step changes are laid over first (at their state before
    /// it), so that each ends as the candidate's step leaves it.
    fn full(&mut self, q: usize, i: usize) -> Result<(), String> {
        let tr = self.tr;
        self.dx.ov.t = q;
        for &w in tr.wrote(q) {
            OvSim { tr, ov: &mut self.dx.ov }.get_mut(w as usize);
        }
        let mut buf = std::mem::take(&mut self.dx.buf);
        let r = self.m.step(self.mods, i, &mut OvSim { tr, ov: &mut self.dx.ov }, &mut self.dx.sc, &mut buf);
        if r.is_ok() {
            let (p0, p1) = (tr.pops_at[q] as usize, tr.pops_at[q + 1] as usize);
            let (i0, i1) = (tr.inf_at[q] as usize, tr.inf_at[q + 1] as usize);
            if buf.pops[..] == tr.out.pops[p0..p1] {
                self.emit_pops(q, q + 1);
            } else {
                self.out.pops.extend(buf.pops.drain(..).map(PSeg::Own));
            }
            if buf.infs[..] == tr.out.inferred[i0..i1] {
                self.emit_inf(q, q + 1);
            } else {
                self.out.inf.extend(buf.infs.drain(..).map(ISeg::Own));
            }
        }
        buf.pops.clear();
        buf.infs.clear();
        self.dx.buf = buf;
        r?;
        self.dx.ov.t = q + 1;
        self.dx.clean_log(tr);
        Ok(())
    }

    /// Every watched web step q (x's colouring) touches differs from the trace in a way x's colouring cannot see:
    /// not a web a change concerns, live alike, not a split piece, and (as a neighbour) adding the same weights.
    fn invisible(m: &Model, tr: &Trace, dx: &Dx, q: usize, x: usize) -> bool {
        dx.s.iter().all(|&y| {
            if y == x {
                return true;
            }
            let sl = dx.ov.slot[y];
            if sl == NONE || dx.cmask[y] != 0 {
                return false;
            }
            let (yc, yb) = (&dx.ov.ents[sl as usize].1, dx.ov.at(tr, y, q));
            if yc.live != yb.live || yc.piece || yb.piece {
                return false;
            }
            if !yc.live || m.nbl[x].binary_search_by_key(&y, |e| e.0).is_err() {
                return true;
            }
            let mut w = [0i64; NREG];
            contrib(yc, &mut w, 1);
            contrib(yb, &mut w, -1);
            w.iter().all(|&v| v == 0)
        })
    }

    /// Step q, the colouring of x, from the trace's own colouring at q: the weights are the trace's corrected by the
    /// webs whose state differs (and by x's own PREF counts and added neighbours). Only when x gets the trace's
    /// register and no extended web runs dry here; otherwise false (and nothing is changed).
    fn incr_pop(&mut self, q: usize, x: usize) -> bool {
        let (m, mods, tr) = (self.m, self.mods, self.tr);
        let dx = &mut *self.dx;
        if dx.cfull[x] {
            return false;
        }
        let (p0, p1) = (tr.pops_at[q] as usize, tr.pops_at[q + 1] as usize);
        if p1 != p0 + 1 {
            return false;
        }
        let bp = &tr.out.pops[p0];
        let Some(rb) = bp.reg else { return false };
        let ex_lo = dx.extra.partition_point(|e| e.0 < x);
        let ex_hi = dx.extra.partition_point(|e| e.0 <= x);
        if dx.ov.slot[x] == NONE && ex_lo == ex_hi && Self::invisible(m, tr, dx, q, x) {
            // x is coloured as in the trace, and each web whose state differs is read alike: the trace's colouring,
            // with the trace's writes applied to those webs
            dx.ov.t = q;
            let s = std::mem::take(&mut dx.s);
            let mut recheck = std::mem::take(&mut dx.look);
            recheck.clear();
            for &y in &s {
                if y == x || !dx.ov.ents[dx.ov.slot[y] as usize].1.live {
                    continue;
                }
                let nb = m.nbl[x].binary_search_by_key(&y, |e| e.0).is_ok();
                let pt = m.ptl[x].binary_search_by_key(&y, |e| e.0).is_ok();
                let mut sim = OvSim { tr, ov: &mut dx.ov };
                let differs = (sim.get(y).avail ^ tr.at(y, q).avail) >> rb & 1 == 1;
                if nb {
                    sim.get_mut(y).avail &= !(1 << rb);
                }
                if pt {
                    let b = if sim.get(y).prefs[rb as usize] == 0 { Some(sim.stamp()) } else { None };
                    let st = sim.get_mut(y);
                    if let Some(b) = b {
                        st.born[rb as usize] = b;
                    }
                    st.prefs[rb as usize] += 1;
                }
                if (nb && differs) || pt {
                    recheck.push(Look { y, inc_c: false, inc_b: false, part_c: false, part_b: false, line_c: false, line_b: false, pline_c: false, pline_b: false });
                }
            }
            dx.ov.log.clear();
            dx.ov.t = q + 1;
            for l in &recheck {
                dx.clean(tr, l.y);
            }
            dx.look = recheck;
            dx.s = s;
            self.emit_pops(q, q + 1);
            self.emit_inf(q, q + 1);
            return true;
        }
        let xb = dx.ov.at(tr, x, q);
        let xc = match dx.ov.slot[x] {
            NONE => xb,
            s => &dx.ov.ents[s as usize].1,
        };
        if !xc.live || xc.piece {
            return false;
        }
        let mut w = bp.w;
        for r in 1..NREG {
            w[r] += xb.prefs[r] - xc.prefs[r];
        }
        let xav = xc.avail;
        let g = dx.next_gen();
        dx.gen[x] = g;
        let mut look = std::mem::take(&mut dx.look);
        look.clear();
        let mut ldiff = false;
        for k in 0..dx.s.len() + (ex_hi - ex_lo) {
            let y = if k < dx.s.len() { dx.s[k] } else { dx.extra[ex_lo + k - dx.s.len()].1 };
            if dx.gen[y] == g {
                continue;
            }
            dx.gen[y] = g;
            let (in_nbl, in_adj) = match m.nbl[x].binary_search_by_key(&y, |e| e.0) {
                Ok(k) => (true, m.nbl[x][k].1),
                Err(_) => (false, false),
            };
            let is_ex = dx.extra[ex_lo..ex_hi].binary_search(&(x, y)).is_ok();
            let (in_ptl, in_padj) = match m.ptl[x].binary_search_by_key(&y, |e| e.0) {
                Ok(k) => (true, m.ptl[x][k].1),
                Err(_) => (false, false),
            };
            let yb = dx.ov.at(tr, y, q);
            let (inc_c, part_c) = {
                let yc = match dx.ov.slot[y] {
                    NONE => yb,
                    s => &dx.ov.ents[s as usize].1,
                };
                let inc = (in_nbl || is_ex) && yc.live && !(yc.piece && !mods.ext_of(y).map_or(false, |v| v.contains(&x)));
                if inc {
                    contrib(yc, &mut w, 1);
                }
                (inc, in_ptl && yc.live)
            };
            let inc_b = in_nbl && yb.live;
            if inc_b {
                contrib(yb, &mut w, -1);
            }
            let part_b = in_ptl && yb.live;
            let l = Look {
                y,
                inc_c,
                inc_b,
                part_c,
                part_b,
                line_c: inc_c && !in_adj && !is_ex,
                line_b: inc_b && !in_adj,
                pline_c: part_c && !in_padj,
                pline_b: part_b && !in_padj,
            };
            ldiff |= l.line_c != l.line_b || l.pline_c != l.pline_b;
            look.push(l);
        }
        let ok = match Model::pick(xav, &w) {
            Some(rc) if rc == rb => !look.iter().any(|l| {
                l.inc_c
                    && mods.ext_of(l.y).is_some()
                    && match dx.ov.slot[l.y] {
                        NONE => dx.ov.at(tr, l.y, q).avail,
                        s => dx.ov.ents[s as usize].1.avail,
                    } & !(1 << rc)
                        == 0
            }),
            _ => false,
        };
        if !ok {
            dx.look = look;
            return false;
        }
        let rc = rb;
        // the colouring and the inferred lines
        let pop = PopOut { web: x, avail: xav, w, reg: Some(rc) };
        let same_pop = pop == *bp;
        let (i0, i1) = (tr.inf_at[q] as usize, tr.inf_at[q + 1] as usize);
        let mut own_lines: Vec<Inf> = vec![];
        if ldiff {
            let (mut nb, mut pt) = std::mem::take(&mut dx.lines);
            nb.clear();
            pt.clear();
            for l in &tr.out.inferred[i0..i1] {
                match l {
                    Inf::Nb(y, _) => nb.push(*y),
                    Inf::Pt(y, _) => pt.push(*y),
                    _ => {}
                }
            }
            for l in &look {
                if l.line_c && !l.line_b {
                    nb.push(l.y);
                } else if !l.line_c && l.line_b {
                    nb.retain(|&y| y != l.y);
                }
                if l.pline_c && !l.pline_b {
                    pt.push(l.y);
                } else if !l.pline_c && l.pline_b {
                    pt.retain(|&y| y != l.y);
                }
            }
            nb.sort_unstable();
            pt.sort_unstable();
            own_lines.extend(nb.iter().map(|&y| Inf::Nb(y, x)));
            own_lines.extend(pt.iter().map(|&y| Inf::Pt(y, x)));
            dx.lines = (nb, pt);
        }
        // the writes: a web the trace's step writes alike stays as the trace has it
        dx.ov.t = q;
        {
            let mut sim = OvSim { tr, ov: &mut dx.ov };
            if sim.ov.slot[x] != NONE {
                let s = sim.get_mut(x);
                s.colour = Some(rc);
                s.live = false;
            }
            for l in &look {
                let dirty = sim.ov.slot[l.y] != NONE;
                if l.inc_c && (dirty || !l.inc_b) {
                    sim.get_mut(l.y).avail &= !(1 << rc);
                } else if !l.inc_c && l.inc_b && !dirty {
                    sim.get_mut(l.y);
                }
                if l.part_c && (dirty || !l.part_b) {
                    let b = if sim.get(l.y).prefs[rc as usize] == 0 { Some(sim.stamp()) } else { None };
                    let s = sim.get_mut(l.y);
                    if let Some(b) = b {
                        s.born[rc as usize] = b;
                    }
                    s.prefs[rc as usize] += 1;
                } else if !l.part_c && l.part_b && !dirty {
                    sim.get_mut(l.y);
                }
            }
        }
        dx.ov.t = q + 1;
        dx.clean(tr, x);
        for l in &look {
            dx.clean(tr, l.y);
        }
        dx.clean_log(tr);
        dx.look = look;
        if same_pop {
            self.emit_pops(q, q + 1);
        } else {
            self.out.pops.push(PSeg::Own(pop));
        }
        if ldiff {
            self.out.inf.extend(own_lines.into_iter().map(ISeg::Own));
        } else {
            self.emit_inf(q, q + 1);
        }
        true
    }
}
