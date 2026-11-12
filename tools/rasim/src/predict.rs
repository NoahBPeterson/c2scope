//! `rasim check SNAP --predict [-v]`: run Color's loop (10b0cf40) as a model and predict every spill, deferral and
//! split decision, instead of replaying them from the trace.  Rules: docs/c2/regalloc.md section 3.6.
//!
//! The model keeps its own priority list (InsertInList 10b11970 and InsertByBenefit 10b119f0, node for node), its
//! own GLOBREG fields (benefit, priority, the deferred flag, the avail set, the PREF list) and decides, for every
//! pop, what c2's code decides:
//!   branch        benefit < 0, or == 0 and not (one block, not an immediate, nrefs > 2, /O2)  -> SpillOrDefer
//!                 else fl5 & 4 clear                                                          -> BetterToDefer [R]
//!                 else avail empty                                                            -> HandleCurrUncolorable
//!                 else                                                                        -> ChoosePhysReg
//!   SpillOrDefer  RestrictToReg (pin to the most-preferred available register) / first time: defer
//!                 (InsertByBenefit) / second time: Spill if one block or nEnter == 0, else cut into single-block
//!                 lives together with every later deferred web of the same immediate-ness
//!   split set     after a colouring: the neighbours whose avail set became empty (minus those folded away [R]) plus
//!                 InsertSplitPoints' own set [R]; a non-empty set starts a split round before the next pop
//!   SpillObvious  a new globreg with nrefs < 2 and (benefit <= 0 or a last def that is not an enterload) is spilled
//!   list order    which web is at the head at every pop, including deferred webs and the pieces of each round
//! [R] = replayed.  What is still taken from the trace, and why (each needs the IL, which the snapshot does not hold):
//!   - the pieces a split round creates (how many, their blocks, costs, benefit, avail set, PREFs, flags), unless
//!     `--pieces` (a `regs --pieces` snapshot): then pieces.rs gives their benefit, priority, nrefs, blocks, nEnter
//!     and airlock flag, and only the avail set and PREFs are the trace's;
//!   - the interfering neighbours and copy partners of each colouring (InsertSplitPoints' nbrs, _UpdatePrefsBv);
//!   - InsertSplitPoints' own split set (webs cut because more values are live than there are registers);
//!   - whether CmdSubstDef, FoldAwaySplitGlobreg and BetterToDefer succeed (instruction patterns), and the
//!     priority BetterToDefer gives (it needs the def's components).
//!   - what other code does to a queued web: a foldable def folded once its component is coloured
//!     (ProcessFoldableDefs), the benefit change after a spill (CmdUpdateCost), the re-split of a web whose
//!     user a spill deleted (_NeedNewLives), the restriction to esi/edi/ebp under byte-register pressure.
//! After a miss the model is put back on the trace, so every decision is scored against a correct history.
//! Then `numbering` replays the GLOBREG free list (regalloc.md 3.6.9): every split piece's record number and the
//! order of the reloads at a block entry.  Every c2 build: 13.10 and 15.00 snapshots carry their opcode numbers
//! (`c2.ops`); the addresses above are 14.00's.

use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet, HashMap};

const ORDER: [u32; 7] = [1, 2, 3, 7, 8, 4, 6]; // _IntRegOrder 10ce59a0

fn rname(r: u32) -> &'static str {
    ["r0", "eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"].get(r as usize).copied().unwrap_or("r?")
}
fn rnum(s: &str) -> Option<u32> {
    (1..=8).find(|&r| rname(r) == s)
}
fn mask(v: &[u32]) -> u32 {
    v.iter().filter(|&&r| r < 32).fold(0, |m, r| m | 1 << r)
}
fn mnames(m: u32) -> String {
    let v: Vec<&str> = ORDER.iter().filter(|&&r| m >> r & 1 == 1).map(|&r| rname(r)).collect();
    if v.is_empty() {
        "none".into()
    } else {
        v.join(" ")
    }
}

#[derive(Deserialize)]
struct PSnap {
    func: String,
    passes: Vec<PPass>,
    pops: Vec<PPop>,
    #[serde(default)]
    inserts: Vec<PIns>,
    #[serde(default)]
    pref_events: Vec<PPref>,
    #[serde(default)]
    decisions: Option<Vec<PDec>>,
    #[serde(default)]
    split_rounds: Vec<PSplit>,
    #[serde(default)]
    npasses: Option<u32>, // Spillcosts calls (a round that leaves no piece has no other event)
    #[serde(default)]
    c2: Option<PBuild>, // another c2 build (absent: 14.00.50727.762): the opcode numbers its rules test
}
#[derive(Deserialize)]
struct PBuild {
    #[serde(default)]
    build: String,
    ops: POps,
}
/// Opcode numbers the decision rules compare with (they differ between builds; port-1310.md, port-1500.md).
#[derive(Deserialize, Clone, Copy)]
struct POps {
    enterload: i64, // OPENTERLOAD: 14.00 0x295, 13.10 0x26a, 15.00 0x2e0 (SpillObvious)
    last: i64,      // first opcode past the machine opcodes: 0x273, 0x249, 0x2be (RestrictToReg's Opcode_dope bound)
}
const OPS14: POps = POps { enterload: 0x295, last: 0x273 };
#[derive(Deserialize)]
struct PPass {
    n: u32,
    cls: Option<i64>,
}
#[derive(Deserialize)]
struct PSplit {
    npass: u32,
    removed: Vec<String>,
}
#[derive(Deserialize)]
struct PPop {
    pop: usize,
    name: String,
    gr: String,
    prio: i64,
    reg: Option<String>,
    npass: u32,
    cls: Option<i64>,
    ben: Option<i64>,
    kind: Option<String>,
    num: Option<u64>,
    choice: Option<PChoice>,
    x: Option<PX>,
}
#[derive(Deserialize)]
struct PChoice {
    avail: Option<Vec<u32>>,
    prefs: Vec<(u32, i64)>,
    nbrs: Vec<PNbr>,
    partners: Vec<PPartner>,
}
#[derive(Deserialize)]
struct PNbr {
    gr: String,
    avail: Vec<u32>,
    ben: i64,
    name: String,
    prefs: Vec<(u32, i64)>,
}
#[derive(Deserialize)]
struct PPartner {
    gr: String,
    coloured: Option<bool>,
}
#[derive(Deserialize)]
struct PIns {
    npass: u32,
    after_pop: usize,
    gr: Option<String>,
    num: Option<serde_json::Value>,
    avail: Option<Vec<u32>>,
    ben: Option<i64>,
    prefs: Option<Vec<(u32, i64)>>,
    deferred: Option<bool>,
    x: Option<PX>,
}
/// A GLOBREG record as logged when it enters the list (inserts[].x) or is popped (pops[].x).
#[derive(Deserialize, Clone)]
struct PX {
    fl5: u32,
    fl6: u32,
    nrefs: Option<i64>,
    single: bool,
    nenter: Option<i64>,
    prio: Option<i64>,
    #[serde(rename = "defOrd")]
    def_ord: Option<u64>,
    name: Option<String>,
    kind: Option<String>,
    avail_empty: Option<bool>,
    lastop: Option<i64>,
    dope: Option<i64>,
    dst10: Option<i64>,
    speed: Option<i64>, // _Speed 10d0d71c: nonzero at /O2
}
#[derive(Deserialize)]
struct PPref {
    npass: u32,
    gr: String,
    reg: u32,
}
#[derive(Deserialize)]
struct PDec {
    npass: u32,
    after_pop: usize,
    t: String,
    gr: String,
    nrefs: Option<i64>,
    ben: Option<i64>,
    lastop: Option<i64>,
    name: Option<String>,
    ret: Option<i64>,
    ok: Option<bool>,
    site: Option<String>,
    delta: Option<i64>,
    regs: Option<Vec<u32>>,
    // GLOBREG record numbers (regalloc.md 3.6.9): remove / new / piece / reload / freelist events
    num: Option<u64>,
    pnum: Option<u64>,
    tup: Option<String>,
    next: Option<u64>,
    ids: Option<Vec<u64>>,
    full: Option<bool>,
}

#[derive(Clone)]
struct Web {
    gr: String,
    name: String,
    num: u64,
    imm: bool,
    prio: i64,
    def_ord: u64,
    ben: i64,
    nrefs: i64,
    fl5: u32,
    fl6: u32,
    single: bool,
    nenter: i64,
    avail: u32,
    prefs: Vec<(u32, i64)>,
    pmark: bool, // the model says: this web is in the split set (fl5 |= 2)
    nmark: bool, // replayed: Spill deleted a dead def that used this web (_NeedNewLives, fl5 |= 2)
}

/// What happens to a popped globreg.
#[derive(Clone, PartialEq, Debug)]
enum Kind {
    Restrict(u32),
    Defer,
    Spill,
    Force(BTreeSet<String>), // the other deferred webs cut in the same call
    Requeue,
    Uncol,
    Colour(u32),
    Unknown,
}
impl Kind {
    fn branch(&self) -> u8 {
        match self {
            Kind::Restrict(_) | Kind::Defer | Kind::Spill | Kind::Force(_) => 0,
            Kind::Requeue => 1,
            Kind::Uncol => 2,
            Kind::Colour(_) => 3,
            Kind::Unknown => 9,
        }
    }
    fn sub(&self) -> u8 {
        match self {
            Kind::Restrict(_) => 0,
            Kind::Defer => 1,
            Kind::Spill => 2,
            Kind::Force(_) => 3,
            _ => 9,
        }
    }
    fn text(&self) -> String {
        match self {
            Kind::Restrict(r) => format!("pin to {}", rname(*r)),
            Kind::Defer => "defer".into(),
            Kind::Spill => "spill".into(),
            Kind::Force(o) => format!("cut into single-block lives (+{} deferred webs)", o.len()),
            Kind::Requeue => "re-queue behind its components".into(),
            Kind::Uncol => "no register left: split".into(),
            Kind::Colour(r) => rname(*r).into(),
            Kind::Unknown => "?".into(),
        }
    }
}

#[derive(Default, Clone)]
struct Tally {
    pops: usize,
    head: usize,
    branch: (usize, usize),
    sub: (usize, usize),
    reg: (usize, usize),
    nbr: (usize, usize),   // colourings where a neighbour ran out of registers (predicted or traced)
    force: (usize, usize), // the set of webs cut together
    trig: (usize, usize),  // split rounds: predicted and traced / traced
    trig_false: usize,
    trig_own: usize, // traced rounds the model starts without InsertSplitPoints' replayed set
    isp_only: usize, // webs in that set which still had a register (cut for register pressure)
    trig_need: usize, // traced rounds started by _NeedNewLives (replayed)
    so: (usize, usize),
    so_spill: (usize, usize), // spilled by SpillObvious: predicted and traced / traced
    ins: (usize, usize),
    rm: (usize, usize),
    requeue: usize,
    isp: usize,
    isp_webs: usize,
    new_webs: usize,
    state: usize,
}
impl Tally {
    fn add(&mut self, o: &Tally) {
        self.pops += o.pops;
        self.head += o.head;
        for (a, b) in [
            (&mut self.branch, &o.branch),
            (&mut self.sub, &o.sub),
            (&mut self.reg, &o.reg),
            (&mut self.nbr, &o.nbr),
            (&mut self.force, &o.force),
            (&mut self.trig, &o.trig),
            (&mut self.so, &o.so),
            (&mut self.so_spill, &o.so_spill),
            (&mut self.ins, &o.ins),
            (&mut self.rm, &o.rm),
        ] {
            a.0 += b.0;
            a.1 += b.1;
        }
        self.trig_false += o.trig_false;
        self.trig_own += o.trig_own;
        self.isp_only += o.isp_only;
        self.trig_need += o.trig_need;
        self.requeue += o.requeue;
        self.isp += o.isp;
        self.isp_webs += o.isp_webs;
        self.new_webs += o.new_webs;
        self.state += o.state;
    }
}

struct Sim {
    ops: POps,
    webs: Vec<Web>,
    list: Vec<usize>,
    cur: HashMap<String, usize>,
    misses: Vec<String>,
    notes: Vec<String>,
}

impl Sim {
    fn alive(&self, w: usize) -> bool {
        self.list.contains(&w)
    }
    fn live_web(&self, gr: &str) -> Option<usize> {
        self.cur.get(gr).copied().filter(|&w| self.alive(w))
    }
    /// InsertInList 10b11970: before the first node that is not greater (prio descending, then defOrd descending
    /// unsigned); so on an exact tie the newcomer goes in front.
    fn insert_in_list(&mut self, w: usize) {
        let (p, d) = (self.webs[w].prio, self.webs[w].def_ord);
        let at = self.list.iter().position(|&n| {
            let n = &self.webs[n];
            !(n.prio > p || (n.prio == p && n.def_ord > d))
        });
        match at {
            Some(i) => self.list.insert(i, w),
            None => self.list.push(w),
        }
    }
    /// InsertByBenefit 10b119f0, node for node.  The re-inserted web takes the priority of the node it is linked
    /// next to (10b11a24, 10b11a63).
    fn insert_by_benefit(&mut self, w: usize) {
        let n = self.list.len();
        debug_assert!(n > 0);
        let last = n - 1;
        if self.webs[w].imm {
            self.webs[w].prio = self.webs[self.list[last]].prio;
            self.list.push(w);
            return;
        }
        let deferred = |s: &Sim, i: usize| s.webs[s.list[i]].fl5 & 0x40 != 0;
        let mut i = 0;
        while !deferred(self, i) && i < last {
            i += 1;
        }
        if !deferred(self, i) {
            self.webs[w].prio = self.webs[self.list[i]].prio;
            self.list.push(w);
            return;
        }
        let b = self.webs[w].ben;
        let (nb, nimm) = (self.webs[self.list[i]].ben, self.webs[self.list[i]].imm);
        if (b <= nb || nimm) && i == 0 {
            self.webs[w].prio = self.webs[self.list[0]].prio;
            self.list.insert(0, w);
            return;
        }
        if nb < b {
            loop {
                if self.webs[self.list[i]].imm || i == last {
                    break;
                }
                i += 1;
                if !(self.webs[self.list[i]].ben < b) {
                    break;
                }
            }
            if self.webs[self.list[i]].ben < b && !self.webs[self.list[i]].imm {
                self.webs[w].prio = self.webs[self.list[i]].prio;
                self.list.insert(i + 1, w);
                return;
            }
        }
        self.webs[w].prio = self.webs[self.list[i]].prio;
        self.list.insert(i, w);
    }
    /// RestrictToReg 10b1b430: the register it would pin the web to.
    fn restrict(&self, w: usize, x: Option<&PX>) -> Option<u32> {
        let g = &self.webs[w];
        // CmdLoadCost 10c23740.  /O2: 1 for an immediate, else 2.  /O1: 2, 3 or 5 for an immediate (by value; 2
        // is used here), fa08 + 3 for a frame variable (3 is used), 6 otherwise: approximate, see regalloc.md 3.6
        let speed = x.and_then(|x| x.speed).unwrap_or(1) != 0;
        let load = match (speed, g.imm) {
            (true, true) => 1,
            (true, false) => 2,
            (false, true) => 2,
            (false, false) => 3,
        };
        if load + g.ben <= 0 {
            return None;
        }
        let mut best: Option<(u32, i64)> = None;
        for &(r, c) in &g.prefs {
            if r < 32 && g.avail >> r & 1 == 1 && best.map_or(true, |b| b.1 < c) {
                best = Some((r, c));
            }
        }
        let (r, _) = best?;
        // not when nrefs == 3 and the last def is an arithmetic instruction (Opcode_dope & 0x40) whose dst
        // operand has +0x10 == 0 (10b1b4c1..10b1b4ee)
        if let Some(x) = x {
            let op = x.lastop.unwrap_or(0);
            if g.nrefs == 3 && op != 0 && op < self.ops.last && x.dope.unwrap_or(0) & 0x40 != 0 && x.dst10 == Some(0) {
                return None;
            }
        }
        Some(r)
    }
    /// ChoosePhysReg 10b14b70 on the model's state.
    fn choose(&self, w: usize, nbrs: &[usize]) -> Option<u32> {
        let mut wt = [0i64; 32];
        for &y in nbrs {
            let n = &self.webs[y];
            if n.ben > 0 {
                for &(r, c) in &n.prefs {
                    if r < 32 && n.avail >> r & 1 == 1 {
                        wt[r as usize] += c;
                    }
                }
                if n.avail.count_ones() == 1 {
                    wt[n.avail.trailing_zeros() as usize] += n.ben * 100;
                }
            }
        }
        for &(r, c) in &self.webs[w].prefs {
            if r < 32 {
                wt[r as usize] -= c;
            }
        }
        let mut best: Option<u32> = None;
        for &r in &ORDER {
            if self.webs[w].avail >> r & 1 == 1 && best.map_or(true, |b| wt[r as usize] < wt[b as usize]) {
                best = Some(r);
            }
        }
        best
    }
    fn add_pref(&mut self, w: usize, r: u32) {
        match self.webs[w].prefs.iter_mut().find(|p| p.0 == r) {
            Some(p) => p.1 += 1,
            None => self.webs[w].prefs.push((r, 1)),
        }
    }
    /// The model's decision for the globreg just popped (Color 10b0d350..10b0d3c5, SpillOrDefer 10b0d690).
    /// `btd`: what BetterToDefer returned in the trace (replayed), if it ran.
    fn decide(&self, w: usize, x: Option<&PX>, btd: Option<bool>, nbrs: Option<&[usize]>) -> Kind {
        let g = &self.webs[w];
        let speed = x.and_then(|x| x.speed).unwrap_or(1) != 0;
        if g.ben < 1 && (g.ben != 0 || !g.single || g.imm || g.nrefs < 3 || !speed) {
            if g.fl6 & 1 == 0 {
                if let Some(r) = self.restrict(w, x) {
                    return Kind::Restrict(r);
                }
            }
            if g.fl5 & 0x40 == 0 && !self.list.is_empty() {
                return Kind::Defer;
            }
            if g.fl6 & 1 == 0 && (g.single || g.nenter == 0) {
                return Kind::Spill;
            }
            let mut others = BTreeSet::new();
            for &o in &self.list {
                let p = &self.webs[o];
                if p.ben < 1 && (p.fl6 & 1 != 0 || (!p.single && p.nenter > 0)) && p.imm == g.imm && p.fl5 & 0x40 != 0 {
                    others.insert(p.gr.clone());
                }
            }
            return Kind::Force(others);
        }
        if g.fl5 & 4 == 0 && btd == Some(true) {
            return Kind::Requeue;
        }
        if g.avail == 0 {
            return Kind::Uncol;
        }
        match self.choose(w, nbrs.unwrap_or(&[])) {
            Some(r) => Kind::Colour(r),
            None => Kind::Uncol,
        }
    }
}

fn pct(a: (usize, usize)) -> String {
    if a.1 == 0 {
        "-".into()
    } else {
        format!("{}/{}", a.0, a.1)
    }
}

/// `brief`: one summary line (the fourth line of plain `rasim check`); nothing at all for a snapshot without
/// decision events.
pub fn main(path: &str, verbose: bool, brief: bool) {
    main_with(path, verbose, brief, None)
}

/// `pieces`: Some(chain) = the split pieces' records come from the piece model (pieces.rs; `--pieces`, with
/// `--chain` carried from the first round) instead of the trace: benefit, priority, nrefs, single block, nEnter,
/// the airlock flag and the last def SpillObvious looks at.
pub fn main_with(path: &str, verbose: bool, brief: bool, pieces: Option<bool>) {
    let model = pieces.and_then(|c| super::pieces::model_pieces(path, c));
    let mut used = (0usize, 0usize); // (piece records taken from the model, new globregs of split rounds)
    let txt = std::fs::read_to_string(path).unwrap_or_else(|e| super::die(&format!("{}: {}", path, e)));
    let s: PSnap = serde_json::from_str(&txt).unwrap_or_else(|e| super::die(&format!("{}: {}", path, e)));
    let Some(dec) = s.decisions.as_ref() else {
        if brief {
            return;
        }
        println!(
            "predict: not run (this snapshot has no decision events; re-export it with the current tools/c2regs.py \
             on cl 14.00, 13.10 or 15.00)"
        );
        return;
    };
    let Some(n0) = s.passes.iter().filter(|p| p.cls == Some(0)).map(|p| p.n).min() else {
        if brief {
            return;
        }
        println!("predict: no integer Spillcosts pass in this snapshot");
        return;
    };
    let nmax = s
        .pops
        .iter()
        .map(|p| p.npass)
        .chain(s.inserts.iter().map(|i| i.npass))
        .chain(dec.iter().map(|d| d.npass))
        .chain(s.npasses)
        .max()
        .unwrap_or(n0);
    // indexes
    let mut dec_at: HashMap<(u32, usize), Vec<&PDec>> = HashMap::new();
    let mut so_at: BTreeMap<u32, Vec<usize>> = BTreeMap::new();
    for (i, d) in dec.iter().enumerate() {
        if d.t == "so" {
            so_at.entry(d.npass).or_default().push(i);
        } else {
            dec_at.entry((d.npass, d.after_pop)).or_default().push(d);
        }
    }
    let mut ins_at: HashMap<(u32, usize), Vec<&PIns>> = HashMap::new();
    for i in &s.inserts {
        ins_at.entry((i.npass, i.after_pop)).or_default().push(i);
    }
    let removed: HashMap<u32, BTreeSet<String>> =
        s.split_rounds.iter().map(|r| (r.npass, r.removed.iter().cloned().collect())).collect();
    let pop_index: HashMap<usize, usize> = s.pops.iter().enumerate().map(|(i, p)| (p.pop, i)).collect();
    let start = |n: u32| s.pops.iter().filter(|p| p.npass < n).map(|p| p.pop).max().unwrap_or(0);

    let ops = s.c2.as_ref().map_or(OPS14, |b| b.ops);
    let mut sim = Sim { ops, webs: vec![], list: vec![], cur: HashMap::new(), misses: vec![], notes: vec![] };
    let mut rounds: Vec<(u32, Tally)> = vec![];
    let mut subst = (0usize, 0usize);
    let mut folds = (0usize, 0usize);
    let mut ncolour = 0usize;
    let mut folded_defs = 0usize;
    let mut bencost = 0usize;
    let mut bytepress = 0usize;
    let mut need_lives = false; // _NeedNewLives is non-empty (replayed)
    macro_rules! miss {
        ($($a:tt)*) => { sim.misses.push(format!($($a)*)) };
    }
    for n in n0..=nmax {
        let mut t = Tally::default();
        let st = start(n);
        let mut pending = false; // the model says a split round starts before the next pop
        // --- the split round that produced this pass: RemoveSplitGlobregs
        if n > n0 {
            let pred: BTreeSet<String> = sim
                .list
                .iter()
                .filter(|&&w| sim.webs[w].pmark || sim.webs[w].nmark)
                .map(|&w| sim.webs[w].gr.clone())
                .collect();
            let empty = BTreeSet::new();
            let tr = removed.get(&(n - 1)).unwrap_or(&empty);
            t.rm.1 += 1;
            if &pred == tr {
                t.rm.0 += 1;
            } else {
                miss!(
                    "round {}: RemoveSplitGlobregs unlinked {} webs, the model's list holds {} marked ones",
                    n,
                    tr.len(),
                    pred.len()
                );
            }
            let webs = &sim.webs;
            sim.list.retain(|&w| !webs[w].pmark && !webs[w].nmark && !tr.contains(&webs[w].gr));
            for w in sim.webs.iter_mut() {
                w.pmark = false;
                w.nmark = false;
            }
        }
        // --- Spillcosts: preferences for webs that stay queued (use-side copies are counted again in every pass)
        let so_idx: &[usize] = so_at.get(&n).map(|v| v.as_slice()).unwrap_or(&[]);
        let fresh: BTreeSet<&str> = so_idx.iter().map(|&i| dec[i].gr.as_str()).collect();
        if n > n0 {
            for e in s.pref_events.iter().filter(|e| e.npass == n && e.reg <= 8) {
                if fresh.contains(e.gr.as_str()) {
                    continue;
                }
                if let Some(w) = sim.live_web(&e.gr) {
                    sim.add_pref(w, e.reg);
                }
            }
        }
        // replayed: CmdUpdateCost 10c242d0.  When a web is spilled (here by SpillObvious), an instruction that used
        // it together with another globreg now has a memory operand, and the other globreg's benefit goes up:
        // at /O2 by the block weight for an immediate (by 0 for anything else).
        for e in dec_at.get(&(n, st)).map(|v| v.as_slice()).unwrap_or(&[]) {
            if e.t == "bencost" && !fresh.contains(e.gr.as_str()) {
                if let Some(y) = sim.live_web(&e.gr) {
                    sim.webs[y].ben += e.delta.unwrap_or(0);
                    bencost += (e.delta.unwrap_or(0) != 0) as usize;
                }
            }
        }
        // --- SpillObvious 10b11560
        let mut kept: BTreeSet<&str> = BTreeSet::new();
        for &i in so_idx {
            let d = &dec[i];
            let ov = model.as_ref().and_then(|m| m.get(&(n, d.gr.clone())));
            let nrefs = ov.map_or(d.nrefs.unwrap_or(0), |o| o.nrefs);
            let ben = ov.map_or(d.ben.unwrap_or(0), |o| o.ben);
            let lastop = ov.map_or(d.lastop, |o| o.lastop.map(|x| x as i64));
            let pred_spill = nrefs < 2 && (ben < 1 || lastop.map_or(false, |op| op != ops.enterload));
            let mut tr_spill = false;
            let mut tr_subst = false;
            for e in dec[i + 1..].iter().take_while(|e| e.t != "so" && e.npass == d.npass && e.after_pop == d.after_pop) {
                if e.gr == d.gr && e.t == "spill" {
                    tr_spill = true;
                }
                if e.gr == d.gr && e.t == "subst" {
                    subst.1 += 1;
                    if e.ok == Some(true) {
                        subst.0 += 1;
                        tr_subst = true;
                    }
                    if nrefs != 2 {
                        miss!("round {}: CmdSubstDef ran on {} although nrefs is {}", n, d.name.as_deref().unwrap_or("?"), nrefs);
                    }
                }
            }
            t.so.1 += 1;
            if tr_spill {
                t.so_spill.1 += 1;
            }
            if pred_spill == tr_spill {
                t.so.0 += 1;
                if tr_spill {
                    t.so_spill.0 += 1;
                }
            } else {
                miss!(
                    "round {}: SpillObvious on {} (nrefs {}, benefit {}, last def op {:?}): model {}, c2 {}",
                    n,
                    d.name.as_deref().unwrap_or("?"),
                    nrefs,
                    ben,
                    d.lastop,
                    if pred_spill { "spills" } else { "keeps" },
                    if tr_spill { "spills" } else { "keeps" }
                );
            }
            if !tr_spill && !tr_subst {
                kept.insert(d.gr.as_str());
            }
        }
        // --- OrderGlobregs 10b11770 / InsertNewGlobregs 10b133e0: the new globregs enter the list
        let empty_ins = vec![];
        let new: Vec<&PIns> =
            ins_at.get(&(n, st)).unwrap_or(&empty_ins).iter().copied().filter(|i| i.deferred != Some(true)).collect();
        let num_of = |i: &PIns| i.num.as_ref().and_then(|v| v.as_u64()).unwrap_or(0);
        let mut rule: Vec<(u64, &str)> = new.iter().map(|i| (num_of(i), i.gr.as_deref().unwrap_or(""))).collect();
        if n == n0 {
            rule.sort_by(|a, b| (a.0 & 0x3ff).cmp(&(b.0 & 0x3ff)).then(b.0.cmp(&a.0)));
        } else {
            rule.sort();
        }
        for (k, i) in new.iter().enumerate() {
            t.ins.1 += 1;
            if rule[k].1 == i.gr.as_deref().unwrap_or("") {
                t.ins.0 += 1;
            }
        }
        let ins_grs: BTreeSet<&str> = new.iter().filter_map(|i| i.gr.as_deref()).collect();
        if !so_idx.is_empty() && ins_grs != kept {
            miss!(
                "round {}: {} globregs survive SpillObvious but {} enter the list",
                n,
                kept.len(),
                ins_grs.len()
            );
        }
        for i in &new {
            let (Some(gr), Some(x)) = (i.gr.as_ref(), i.x.as_ref()) else {
                super::die("predict: an InsertInList event has no record fields; re-export the snapshot");
            };
            let w = sim.webs.len();
            let ov = model.as_ref().and_then(|m| m.get(&(n, gr.clone())));
            if n > n0 {
                used.1 += 1;
                used.0 += ov.is_some() as usize;
            }
            sim.webs.push(Web {
                gr: gr.clone(),
                name: x.name.clone().unwrap_or_else(|| "?".into()),
                num: num_of(i),
                imm: x.kind.as_deref() == Some("imm"),
                prio: ov.map_or(x.prio.unwrap_or(0), |o| o.prio),
                def_ord: x.def_ord.unwrap_or(0),
                ben: ov.map_or(i.ben.unwrap_or(0), |o| o.ben),
                nrefs: ov.map_or(x.nrefs.unwrap_or(0), |o| o.nrefs),
                fl5: x.fl5 & !2,
                fl6: ov.map_or(x.fl6, |o| (x.fl6 & !1) | o.airlock as u32),
                single: ov.map_or(x.single, |o| o.single),
                nenter: ov.map_or(x.nenter.unwrap_or(0), |o| o.nenter),
                avail: mask(i.avail.as_deref().unwrap_or(&[])),
                prefs: i.prefs.clone().unwrap_or_default(),
                pmark: false,
                nmark: false,
            });
            sim.cur.insert(gr.clone(), w);
            t.new_webs += 1;
            // OrderGlobregs (first pass only): a web that starts with no register is split at once (10b118bb)
            if n == n0 && sim.webs[w].avail == 0 {
                sim.webs[w].pmark = true;
                pending = true;
            }
            sim.insert_in_list(w);
        }
        // replayed: Spill (here: from SpillObvious) deleted a dead def; the webs that def used are re-split.
        // _NeedNewLives is looked at only at the end of a split round (10b0d327): a non-empty set starts the
        // next round at once.
        for e in dec_at.get(&(n, st)).map(|v| v.as_slice()).unwrap_or(&[]) {
            if e.t == "mark" && e.site.as_deref() == Some("needlives") {
                need_lives = true;
                if let Some(y) = sim.live_web(&e.gr) {
                    sim.webs[y].nmark = true;
                }
            }
        }
        let pending_need = n > n0 && need_lives;
        if n > n0 {
            need_lives = false;
        }
        // --- the pops of this pass
        let pops_n: Vec<&PPop> = s.pops.iter().filter(|p| p.npass == n && p.cls == Some(0)).collect();
        if pops_n.is_empty() {
            let traced = n < nmax;
            if traced {
                t.trig.1 += 1;
                if pending {
                    t.trig.0 += 1;
                    t.trig_own += 1;
                } else if pending_need {
                    t.trig.0 += 1;
                    t.trig_need += 1;
                } else {
                    miss!("round {}: c2 starts another split round before any pop; the model has no reason for it", n);
                }
            } else if pending {
                t.trig_false += 1;
            }
        } else if pending {
            t.trig_false += 1;
            miss!("round {}: the model splits a web with no register at once, c2 goes on popping", n);
            for w in sim.webs.iter_mut() {
                w.pmark = false;
            }
        }
        for (pi, p) in pops_n.iter().enumerate() {
            t.pops += 1;
            let evs: &[&PDec] = dec_at.get(&(n, p.pop)).map(|v| v.as_slice()).unwrap_or(&[]);
            let ins_here: &[&PIns] = ins_at.get(&(n, p.pop)).map(|v| v.as_slice()).unwrap_or(&[]);
            let traced_trigger = pi + 1 == pops_n.len() && n < nmax;
            // head of the list
            let w = match sim.live_web(&p.gr) {
                Some(w) => w,
                None => {
                    // not in the model's list: take the record from the trace
                    let x = p.x.clone();
                    let w = sim.webs.len();
                    sim.webs.push(Web {
                        gr: p.gr.clone(),
                        name: p.name.clone(),
                        num: p.num.unwrap_or(0),
                        imm: p.kind.as_deref() == Some("imm"),
                        prio: p.prio,
                        def_ord: 0,
                        ben: p.ben.unwrap_or(0),
                        nrefs: x.as_ref().and_then(|x| x.nrefs).unwrap_or(0),
                        fl5: x.as_ref().map_or(4, |x| x.fl5 & !2),
                        fl6: x.as_ref().map_or(0, |x| x.fl6),
                        single: x.as_ref().map_or(false, |x| x.single),
                        nenter: x.as_ref().and_then(|x| x.nenter).unwrap_or(0),
                        avail: mask(p.choice.as_ref().and_then(|c| c.avail.as_deref()).unwrap_or(&[])),
                        prefs: p.choice.as_ref().map(|c| c.prefs.clone()).unwrap_or_default(),
                        pmark: false,
                nmark: false,
                    });
                    sim.cur.insert(p.gr.clone(), w);
                    sim.list.insert(0, w);
                    t.state += 1;
                    miss!("pop {} {}: not in the model's list (taken from the trace)", p.pop, p.name);
                    w
                }
            };
            if sim.list.first() == Some(&w) {
                t.head += 1;
            } else {
                let h = &sim.webs[sim.list[0]];
                miss!(
                    "pop {} {} (prio {}): the model's head is {} (prio {}, benefit {})",
                    p.pop,
                    p.name,
                    p.prio,
                    h.name,
                    h.prio,
                    h.ben
                );
            }
            sim.list.retain(|&y| y != w);
            // --- what c2 did
            let mine = |tag: &str| evs.iter().any(|e| e.t == tag && e.gr == p.gr);
            let btd = evs.iter().find(|e| e.t == "btd" && e.gr == p.gr).map(|e| e.ret.unwrap_or(0) != 0);
            let traced: Kind = match p.reg.as_deref() {
                Some(r) if rnum(r).is_some() => Kind::Colour(rnum(r).unwrap()),
                Some("uncolourable") => Kind::Uncol,
                Some("spill/defer") => {
                    if evs.iter().any(|e| e.t == "restrict" && e.gr == p.gr && e.ret == Some(1)) {
                        let r = pop_index
                            .get(&(p.pop + 1))
                            .and_then(|&i| s.pops[i].choice.as_ref())
                            .and_then(|c| c.avail.as_ref())
                            .and_then(|a| a.first().copied())
                            .unwrap_or(0);
                        Kind::Restrict(r)
                    } else if mine("spill") {
                        Kind::Spill
                    } else if mine("force1") || mine("forceair") {
                        Kind::Force(
                            evs.iter()
                                .filter(|e| (e.t == "force1" || e.t == "forceair") && e.gr != p.gr)
                                .map(|e| e.gr.clone())
                                .collect(),
                        )
                    } else if ins_here.iter().any(|i| i.deferred == Some(true)) {
                        Kind::Defer
                    } else {
                        Kind::Unknown
                    }
                }
                _ => {
                    if btd == Some(true) {
                        Kind::Requeue
                    } else {
                        Kind::Unknown
                    }
                }
            };
            // replayed: byte-register pressure (InsertSplitPoints 10b147ac..10b148f3 and CmdProcessBytePressure
            // 10c24fe0, before the register is chosen).  Where the live values that need a byte register use up
            // the byte registers, the web being coloured and every other uncoloured web live there that can use
            // esi/edi/ebp are restricted to them.
            for e in evs.iter().filter(|e| e.t == "bytepress") {
                let y = if e.gr == p.gr { Some(w) } else { sim.live_web(&e.gr) };
                if let (Some(y), Some(r)) = (y, e.regs.as_ref()) {
                    sim.webs[y].avail &= mask(r);
                    bytepress += 1;
                }
            }
            // --- the model's decision, from its own state
            let nb: Option<Vec<usize>> = p.choice.as_ref().map(|c| {
                c.nbrs.iter().filter_map(|x| sim.live_web(&x.gr)).collect()
            });
            let pred = sim.decide(w, p.x.as_ref(), btd, nb.as_deref());
            if traced == Kind::Requeue || pred == Kind::Requeue {
                t.requeue += 1; // BetterToDefer's answer is replayed
            } else {
                t.branch.1 += 1;
                if pred.branch() == traced.branch() {
                    t.branch.0 += 1;
                } else {
                    miss!("pop {} {}: model: {}; c2: {}", p.pop, p.name, pred.text(), traced.text());
                }
            }
            if pred.branch() == 0 && traced.branch() == 0 {
                t.sub.1 += 1;
                if pred.sub() == traced.sub() && (pred.sub() != 0 || pred == traced) {
                    t.sub.0 += 1;
                } else {
                    miss!("pop {} {}: model: {}; c2: {}", p.pop, p.name, pred.text(), traced.text());
                }
                if let (Kind::Force(a), Kind::Force(b)) = (&pred, &traced) {
                    t.force.1 += 1;
                    if a == b {
                        t.force.0 += 1;
                    } else {
                        miss!("pop {} {}: the model cuts {} other deferred webs, c2 {}", p.pop, p.name, a.len(), b.len());
                    }
                }
            }
            if let (Kind::Colour(a), Kind::Colour(b)) = (&pred, &traced) {
                t.reg.1 += 1;
                if a == b {
                    t.reg.0 += 1;
                } else {
                    miss!("pop {} {}: model {}, c2 {}", p.pop, p.name, rname(*a), rname(*b));
                }
            }
            // --- the record: model against trace (then the trace's values are taken)
            {
                let g = &mut sim.webs[w];
                let mut diff = vec![];
                if let Some(b) = p.ben {
                    if g.ben != b {
                        diff.push(format!("benefit {} (c2 {})", g.ben, b));
                        g.ben = b;
                    }
                }
                if g.prio != p.prio {
                    diff.push(format!("prio {} (c2 {})", g.prio, p.prio));
                    g.prio = p.prio;
                }
                if let Some(x) = p.x.as_ref() {
                    if (g.fl5 ^ x.fl5) & 0x44 != 0 {
                        diff.push(format!("fl5 {:x} (c2 {:x})", g.fl5, x.fl5));
                        g.fl5 = x.fl5 & !2;
                    }
                    if g.single != x.single || g.nenter != x.nenter.unwrap_or(0) || g.fl6 & 1 != x.fl6 & 1 {
                        diff.push("range fields".into());
                        g.single = x.single;
                        g.nenter = x.nenter.unwrap_or(0);
                        g.fl6 = x.fl6;
                    }
                    if let Some(n) = x.nrefs {
                        if g.nrefs != n {
                            diff.push(format!("nrefs {} (c2 {})", g.nrefs, n));
                            g.nrefs = n;
                        }
                    }
                    if let Some(e) = x.avail_empty {
                        if e != (g.avail == 0) {
                            diff.push(format!("avail {} (c2 {})", mnames(g.avail), if e { "empty" } else { "not empty" }));
                            if e {
                                g.avail = 0;
                            }
                        }
                    }
                }
                if !diff.is_empty() {
                    t.state += 1;
                    let name = g.name.clone();
                    miss!("pop {} {}: record differs: {}", p.pop, name, diff.join(", "));
                }
            }
            // --- apply what c2 did
            let mut pred_trigger = matches!(pred, Kind::Uncol | Kind::Force(_));
            let mut own_trigger = pred_trigger;
            match &traced {
                Kind::Colour(r) => {
                    ncolour += 1;
                    let c = p.choice.as_ref().unwrap();
                    let (av, pf) = (mask(c.avail.as_deref().unwrap_or(&[])), &c.prefs);
                    let same_prefs = |a: &[(u32, i64)], b: &[(u32, i64)]| {
                        let m = |v: &[(u32, i64)]| {
                            let mut x: Vec<(u32, i64)> = v.iter().copied().filter(|p| p.1 != 0).collect();
                            x.sort();
                            x
                        };
                        m(a) == m(b)
                    };
                    if av != sim.webs[w].avail || !same_prefs(pf, &sim.webs[w].prefs) {
                        t.state += 1;
                        miss!(
                            "pop {} {}: avail/PREFs differ: model {} {:?}, c2 {} {:?}",
                            p.pop,
                            p.name,
                            mnames(sim.webs[w].avail),
                            sim.webs[w].prefs,
                            mnames(av),
                            pf
                        );
                    }
                    let mut pred_empty: BTreeSet<String> = BTreeSet::new();
                    for x in &c.nbrs {
                        let Some(y) = sim.live_web(&x.gr) else {
                            t.state += 1;
                            miss!("pop {} {}: neighbour {} is not in the model's list", p.pop, p.name, x.name);
                            continue;
                        };
                        let xa = mask(&x.avail);
                        let g = &mut sim.webs[y];
                        if g.avail != xa || g.ben != x.ben || (x.ben > 0 && !same_prefs(&x.prefs, &g.prefs)) {
                            t.state += 1;
                            let m = format!(
                                "pop {} {}: neighbour {}: model avail {} benefit {} {:?}, c2 {} {} {:?}",
                                p.pop,
                                p.name,
                                x.name,
                                mnames(g.avail),
                                g.ben,
                                g.prefs,
                                mnames(xa),
                                x.ben,
                                x.prefs
                            );
                            g.avail = xa;
                            g.ben = x.ben;
                            if x.ben > 0 {
                                g.prefs = x.prefs.clone();
                            }
                            sim.misses.push(m);
                        }
                        // UpdateAffectedGlobregs 10b15470: the chosen register leaves every neighbour's avail set
                        let g = &mut sim.webs[y];
                        if g.avail != 0 {
                            g.avail &= !(1 << r);
                            if g.avail == 0 {
                                pred_empty.insert(g.gr.clone());
                            }
                        }
                    }
                    // replayed: FoldAwaySplitGlobreg (a web with no unfoldable reference may be folded instead)
                    let folded: BTreeSet<String> =
                        evs.iter().filter(|e| e.t == "fold" && e.ret.unwrap_or(0) != 0).map(|e| e.gr.clone()).collect();
                    folds.1 += evs.iter().filter(|e| e.t == "fold").count();
                    folds.0 += folded.len();
                    let pred_marked: BTreeSet<String> = pred_empty.difference(&folded).cloned().collect();
                    let tr_nbr: BTreeSet<String> = evs
                        .iter()
                        .filter(|e| e.t == "mark" && matches!(e.site.as_deref(), Some("nbr") | Some("nbr8")))
                        .map(|e| e.gr.clone())
                        .collect();
                    // replayed: InsertSplitPoints' own split set
                    let isp: BTreeSet<String> = evs
                        .iter()
                        .filter(|e| e.t == "mark" && e.site.as_deref() == Some("isp"))
                        .map(|e| e.gr.clone())
                        .filter(|g| !folded.contains(g))
                        .collect();
                    if !isp.is_empty() {
                        t.isp += 1;
                        t.isp_webs += isp.len();
                    }
                    if !pred_marked.is_empty() || !tr_nbr.is_empty() {
                        t.nbr.1 += 1;
                        if pred_marked == tr_nbr {
                            t.nbr.0 += 1;
                        } else {
                            miss!(
                                "pop {} {}: neighbours left without a register: model {}, c2 {}",
                                p.pop,
                                p.name,
                                pred_marked.len(),
                                tr_nbr.len()
                            );
                        }
                    }
                    if matches!(pred, Kind::Colour(_)) {
                        pred_trigger = !pred_marked.is_empty() || !isp.is_empty();
                        own_trigger = !pred_marked.is_empty();
                    }
                    t.isp_only += isp.difference(&tr_nbr).count();
                    for g in tr_nbr.iter().chain(isp.iter()) {
                        if let Some(y) = sim.live_web(g) {
                            sim.webs[y].pmark = true;
                        }
                    }
                    for g in &folded {
                        if let Some(y) = sim.live_web(g) {
                            sim.list.retain(|&z| z != y);
                        }
                    }
                    // _UpdatePrefsBv 10b15152: an uncoloured copy partner now prefers the chosen register
                    for q in &c.partners {
                        if q.coloured == Some(true) || q.gr == p.gr {
                            continue;
                        }
                        if let Some(y) = sim.live_web(&q.gr) {
                            sim.add_pref(y, *r);
                        }
                    }
                }
                Kind::Uncol => {
                    sim.webs[w].pmark = true;
                    if sim.list.is_empty() {
                        sim.list.push(w); // 10b0d95e: an empty list gets the web back so that the loop goes on
                    }
                }
                Kind::Restrict(r) => {
                    sim.webs[w].avail = 1 << r;
                    sim.webs[w].ben = 1;
                    sim.list.insert(0, w);
                }
                Kind::Defer => {
                    sim.webs[w].fl5 |= 0x40;
                    if sim.list.is_empty() {
                        sim.list.push(w);
                    } else {
                        sim.insert_by_benefit(w);
                    }
                }
                Kind::Spill | Kind::Unknown => {}
                Kind::Force(others) => {
                    sim.webs[w].pmark = true;
                    sim.list.insert(0, w);
                    for g in others {
                        if let Some(y) = sim.live_web(g) {
                            sim.webs[y].pmark = true;
                        }
                    }
                }
                Kind::Requeue => {
                    // replayed: the new priority (min of the components' priorities - 1) needs the def's components
                    if let Some(i) = ins_here.iter().find(|i| i.gr.as_deref() == Some(p.gr.as_str())) {
                        if let Some(pr) = i.x.as_ref().and_then(|x| x.prio) {
                            sim.webs[w].prio = pr;
                        }
                        sim.insert_in_list(w);
                    }
                }
            }
            for e in evs.iter().filter(|e| e.t == "bencost" && e.gr != p.gr) {
                if let Some(y) = sim.live_web(&e.gr) {
                    sim.webs[y].ben += e.delta.unwrap_or(0);
                    bencost += (e.delta.unwrap_or(0) != 0) as usize;
                }
            }
            for e in evs.iter().filter(|e| e.t == "mark" && e.site.as_deref() == Some("needlives")) {
                need_lives = true;
                if let Some(y) = sim.live_web(&e.gr) {
                    sim.webs[y].nmark = true;
                }
            }
            // replayed: a queued web whose record is retired during this pop without being in the split set was
            // folded into its uses (InsertSplitPoints -> ProcessFoldableDefs 10b19030, once a component is coloured)
            for e in evs.iter().filter(|e| e.t == "remove" && e.gr != p.gr) {
                if let Some(y) = sim.live_web(&e.gr) {
                    if !sim.webs[y].pmark && !sim.webs[y].nmark {
                        sim.list.retain(|&z| z != y);
                        folded_defs += 1;
                    }
                }
            }
            if traced == Kind::Unknown {
                sim.notes.push(format!("pop {} {}: outcome not recognised in the trace", p.pop, p.name));
            }
            // --- does a split round start now?
            if traced_trigger {
                t.trig.1 += 1;
                if own_trigger {
                    t.trig_own += 1;
                }
                if pred_trigger {
                    t.trig.0 += 1;
                } else {
                    miss!("pop {} {}: c2 starts a split round here, the model does not", p.pop, p.name);
                }
            } else if pred_trigger {
                t.trig_false += 1;
                miss!("pop {} {}: the model starts a split round here, c2 does not", p.pop, p.name);
                for g in sim.webs.iter_mut() {
                    g.pmark = false;
                }
            }
        }
        rounds.push((n, t));
    }
    // --- report
    let mut tot = Tally::default();
    for (_, t) in &rounds {
        tot.add(t);
    }
    if brief {
        let parts = [(tot.head, tot.pops), tot.branch, tot.sub, tot.reg, tot.nbr, tot.force, tot.trig, tot.so];
        let (a, b) = parts.iter().fold((0, 0), |x, y| (x.0 + y.0, x.1 + y.1));
        println!(
            "decisions: {} of {} predicted from the model's own state ({} pops, {} rounds: list head, spill / defer / \
             pin / cut, register, split set, round start, SpillObvious); {} state differences; split pieces and \
             neighbour lists are replayed (rasim check SNAP --predict)",
            a,
            b,
            tot.pops,
            rounds.len(),
            tot.state
        );
        return;
    }
    println!(
        "predict: {}: {} int pops in {} rounds; each decision is predicted from the model's own state and checked \
         against the trace{}",
        s.func,
        tot.pops,
        rounds.len(),
        s.c2.as_ref().map_or(String::new(), |b| format!(" (c2 {})", b.build))
    );
    println!(
        "  {:>5} {:>6} {:>11} {:>11} {:>11} {:>11} {:>9} {:>9} {:>11} {:>12} {:>9}",
        "round", "new", "list head", "branch", "spill/defer", "register", "no-reg", "cut set", "split round", "SpillObvious", "ins order"
    );
    let row = |name: String, t: &Tally| {
        println!(
            "  {:>5} {:>6} {:>11} {:>11} {:>11} {:>11} {:>9} {:>9} {:>11} {:>12} {:>9}",
            name,
            t.new_webs,
            pct((t.head, t.pops)),
            pct(t.branch),
            pct(t.sub),
            pct(t.reg),
            pct(t.nbr),
            pct(t.force),
            format!("{}{}", pct(t.trig), if t.trig_false > 0 { format!("+{}", t.trig_false) } else { String::new() }),
            pct(t.so),
            pct(t.ins)
        );
    };
    let quiet: Vec<&(u32, Tally)> = rounds.iter().collect();
    // rounds with a single pop and nothing new are folded into one line per run to keep big functions readable
    let mut i = 0;
    while i < quiet.len() {
        let (n, t) = quiet[i];
        let small = |t: &Tally| t.pops <= 1 && t.new_webs == 0 && t.so.1 == 0;
        if small(t) && !verbose {
            let mut j = i;
            let mut acc = Tally::default();
            while j < quiet.len() && small(&quiet[j].1) {
                acc.add(&quiet[j].1);
                j += 1;
            }
            if j - i > 2 {
                row(format!("{}-{}", n, quiet[j - 1].0), &acc);
                i = j;
                continue;
            }
        }
        row(n.to_string(), t);
        i += 1;
    }
    row("all".into(), &tot);
    println!(
        "  columns: new = globregs entering the list; list head = the popped web is the model's head; branch = \
         spill-or-defer / no register / colour; spill/defer = pin / defer / spill / cut; no-reg = colourings after \
         which a neighbour has no register left: the set; cut set = the deferred webs cut together; split round = \
         traced rounds the model starts (+N: started by the model only); ins order = insertion order by web number"
    );
    println!(
        "  SpillObvious spills: {} of {} predicted; RemoveSplitGlobregs sets consistent with the marks: {}",
        tot.so_spill.0,
        tot.so_spill.1,
        pct(tot.rm)
    );
    println!(
        "  split rounds: {} of {} are started by a decision of the model alone (a neighbour without a register, a \
         deferred web cut, a web with no register); {} by _NeedNewLives (replayed: a spill deleted a dead def), {} only by InsertSplitPoints' replayed set ({} of \
         its {} webs still had a register)",
        tot.trig_own,
        tot.trig.1,
        tot.trig_need,
        tot.trig.0.saturating_sub(tot.trig_own + tot.trig_need),
        tot.isp_only,
        tot.isp_webs
    );
    println!(
        "  replayed from the trace (need the IL): the {} pieces of {} split rounds; the neighbour and partner lists \
         of {} colourings; InsertSplitPoints' split set ({} webs at {} colourings); CmdSubstDef ({} of {} folded); \
         FoldAwaySplitGlobreg ({} of {}); BetterToDefer ({} re-queues); foldable defs folded while queued ({}); benefit changes of queued webs after a spill, CmdUpdateCost ({}); byte-register pressure, CmdProcessBytePressure ({})",
        rounds.iter().skip(1).map(|r| r.1.new_webs).sum::<usize>(),
        rounds.len().saturating_sub(1),
        ncolour,
        tot.isp_webs,
        tot.isp,
        subst.0,
        subst.1,
        folds.0,
        folds.1,
        tot.requeue,
        folded_defs,
        bencost,
        bytepress
    );
    println!("  state: {} places where the model's record differs from c2's (it is then taken from the trace)", tot.state);
    if pieces.is_some() {
        println!(
            "  pieces: {} of the {} records split rounds put in the list come from the piece model (pieces.rs{}): \
             benefit, priority, nrefs, single, nEnter, airlock flag, last def; avail set and PREFs from the trace",
            used.0,
            used.1,
            if pieces == Some(true) { ", --chain" } else { "" }
        );
    }
    let num_misses = numbering(dec, &s.pops);
    if !sim.list.is_empty() {
        println!("  {} webs are left in the model's list at the end", sim.list.len());
    }
    let show = if verbose { sim.misses.len() } else { 6 };
    for m in sim.misses.iter().take(show) {
        println!("  miss: {}", m);
    }
    if sim.misses.len() > show {
        println!("  ... {} more (-v lists all)", sim.misses.len() - show);
    }
    for m in sim.notes.iter().take(if verbose { usize::MAX } else { 3 }) {
        println!("  note: {}", m);
    }
    for m in num_misses.iter().take(if verbose { usize::MAX } else { 4 }) {
        println!("  number miss: {}", m);
    }
    if !verbose && num_misses.len() > 4 {
        println!("  ... {} more number misses (-v lists all)", num_misses.len() - 4);
    }
}

/// GLOBREG record numbers and the reload order (regalloc.md 3.6.9), replayed over the decision events in log
/// order.  `_FreeGlobs` is a LIFO list: ColRemoveGlobregFromHashTable pushes a retired record (keeping its number),
/// GrgNewGlobreg pops the head, or takes `_NextGlobregNum++` when the list is empty (both reset by Globregs).  At
/// Color's entry the snapshot holds the counter and the first 11 records of the list (event `freelist`).  Checked:
///   - every record handed out has the number the list predicts (pieces of NewLiveRanges, the use / def pieces of
///     RewriteSplitGlobregs, others: FPColor's splits, ...);
///   - NewLiveRanges takes the split parents in ascending number (ColProcessSplitGlobregs links them so);
///   - the enterloads GrgAirlockEnterloads puts at one place (a block entry after a split point) come in
///     ascending number of their pieces (SbvNext over the live-in set), with the numbers the list predicts.
/// What is replayed: which records are retired and when (the decisions themselves are scored above), how many
/// pieces each parent gets and which piece needs a reload (the IL).  Returns the misses.
fn numbering(dec: &[PDec], pops: &[PPop]) -> Vec<String> {
    let mut misses = vec![];
    if !dec.iter().any(|d| d.t == "new") {
        return misses;
    }
    let mut free: Vec<u64> = vec![]; // top = last
    let mut bottom_known = false; // the traced list was complete (fewer than 11 records)
    let mut next: Option<u64> = None;
    let mut started = false;
    let (mut hit, mut n, mut unknown) = ([0usize; 4], [0usize; 4], 0usize);
    let site_ix = |s: Option<&str>| match s {
        Some("nlr") => 0,
        Some("use") => 1,
        Some("def") => 2,
        _ => 3,
    };
    let mut model: HashMap<String, Option<u64>> = HashMap::new(); // piece record -> the model's number
    let (mut ord_hit, mut ord_n) = (0usize, 0usize); // NewLiveRanges blocks whose parents ascend
    let (mut grp_hit, mut grp_n, mut grp_pieces) = (0usize, 0usize, 0usize);
    let last_pop = pops.last().map(|p| p.pop).unwrap_or(0);
    let mut i = 0;
    while i < dec.len() {
        let d = &dec[i];
        // after the last pop, a record handed out that is not a split piece belongs to the next function's
        // Globregs (CreateGlobregs; the trace has no Color exit event)
        if d.t == "new" && d.after_pop >= last_pop && !dec.get(i + 1).map_or(false, |e| e.t == "piece" && e.gr == d.gr) {
            break;
        }
        match d.t.as_str() {
            "freelist" => {
                free = d.ids.clone().unwrap_or_default();
                free.reverse();
                bottom_known = d.full.unwrap_or(false);
                next = d.next;
                started = true;
            }
            "remove" if started => {
                if let Some(x) = d.num {
                    free.push(x);
                }
            }
            "new" if started => {
                let site = dec.get(i + 1).filter(|e| e.t == "piece" && e.gr == d.gr).and_then(|e| e.site.as_deref());
                let k = site_ix(site);
                let pred = if let Some(x) = free.pop() {
                    Some(x)
                } else if bottom_known {
                    next.map(|x| {
                        next = Some(x + 1);
                        x
                    })
                } else {
                    None
                };
                let got = d.num;
                match pred {
                    None => unknown += 1,
                    Some(p) => {
                        n[k] += 1;
                        if Some(p) == got {
                            hit[k] += 1;
                        } else {
                            misses.push(format!(
                                "round {}: a {} piece got record {:?}, the free list says {}",
                                d.npass,
                                site.unwrap_or("non-split"),
                                got,
                                p
                            ));
                            // back on the trace: c2's number leaves the list (or the counter passes it)
                            if let Some(g) = got {
                                if let Some(j) = free.iter().rposition(|&y| y == g) {
                                    free.remove(j);
                                    free.push(p);
                                } else if next.map_or(false, |x| g >= x) {
                                    if Some(p) != next.map(|x| x - 1) {
                                        free.push(p);
                                    }
                                    next = Some(g + 1);
                                }
                            }
                        }
                    }
                }
                model.insert(d.gr.clone(), pred);
            }
            "piece" if d.site.as_deref() == Some("nlr") && (i == 0 || dec[i - 1].t == "new") => {
                // one NewLiveRanges block: the consecutive nlr pieces of this ColProcessSplitGlobregs call
                let mut j = i;
                let mut parents = vec![];
                while j < dec.len() && (dec[j].t == "new" || (dec[j].t == "piece" && dec[j].site.as_deref() == Some("nlr"))) {
                    if dec[j].t == "piece" {
                        parents.push(dec[j].pnum.unwrap_or(0));
                    }
                    j += 1;
                }
                if parents.len() > 1 && (i == 1 || !(dec[i - 2].t == "piece" && dec[i - 2].site.as_deref() == Some("nlr"))) {
                    ord_n += 1;
                    if parents.windows(2).all(|w| w[0] <= w[1]) {
                        ord_hit += 1;
                    } else {
                        misses.push(format!("round {}: NewLiveRanges takes parents {:?}, not in ascending number", d.npass, parents));
                    }
                }
            }
            "reload" => {
                let mut j = i;
                while j < dec.len() && dec[j].t == "reload" && dec[j].tup == d.tup {
                    j += 1;
                }
                if j - i > 1 {
                    let traced: Vec<&str> = dec[i..j].iter().map(|e| e.gr.as_str()).collect();
                    let mut by_model: Vec<(Option<u64>, &str)> =
                        dec[i..j].iter().map(|e| (model.get(&e.gr).copied().flatten(), e.gr.as_str())).collect();
                    let known = by_model.iter().all(|x| x.0.is_some());
                    by_model.sort();
                    grp_n += 1;
                    grp_pieces += j - i;
                    if known && by_model.iter().map(|x| x.1).collect::<Vec<_>>() == traced {
                        grp_hit += 1;
                    } else {
                        let nums: Vec<Option<u64>> = dec[i..j].iter().map(|e| e.num).collect();
                        misses.push(format!(
                            "round {}: {} enterloads at one place in record order {:?}; model numbers {:?}",
                            d.npass,
                            j - i,
                            nums,
                            dec[i..j].iter().map(|e| model.get(&e.gr).copied().flatten()).collect::<Vec<_>>()
                        ));
                    }
                    i = j;
                    continue;
                }
            }
            _ => {}
        }
        i += 1;
    }
    if n.iter().sum::<usize>() + unknown + ord_n + grp_n == 0 {
        return misses; // no split round handed out a record
    }
    let f = |k: usize| pct((hit[k], n[k]));
    println!(
        "  record numbers (LIFO _FreeGlobs, regalloc.md 3.6.9): {} of {} records handed out get the number the free list \
         predicts (NewLiveRanges pieces {}, reload pieces of RewriteSplitGlobregs {}, def pieces {}, other {}){}",
        hit.iter().sum::<usize>(),
        n.iter().sum::<usize>(),
        f(0),
        f(1),
        f(2),
        f(3),
        if unknown > 0 { format!("; {} beyond the traced free list", unknown) } else { String::new() }
    );
    println!(
        "  split parents taken in ascending number: {}; enterloads at one place in ascending piece number: {} \
         ({} reloads)",
        pct((ord_hit, ord_n)),
        pct((grp_hit, grp_n)),
        grp_pieces
    );
    misses
}
