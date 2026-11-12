//! FPColor (cl 13.10 only): the x87 values, which 13.10 allocates outside Color's class loop.
//! Doc: docs/c2/port-1310.md section 12, docs/c2/x87.md section 11.
//!
//! The snapshot's `fp` key (tools/c2regs.py `_fpexport`, made by `c2trace.py regs --c2 13.10 ... --export`) holds
//! every FPSpillcosts pass with its terms per web and block, and FPColor's events in order (ins, drop, pop, check,
//! kept / nocolor, press, split). The model:
//!
//!   prio(v) = benefit(v) = sum over v's references of cost * w_B  -  sum over edges B->S that v is live out of B
//!             but not into S of w_S          (no pressure factor P: FPSpillcosts 1071128c, 13.10.3077 VAs)
//!   cost    = /O2: def 2 (float, constant) or 4 (double); use 0 (float) or 1 (double); a dying use 0 unless the
//!             pop folds (CanFoldPop), an fld of a dying value 1 or 2; enterload -1 / -2; leave-store -2 / -4
//!   order   = FPAddToPriorityList 107118e8: before the first entry e with (prio, defOrd) <= the newcomer's, so prio
//!             descending, then defOrd descending, and a newcomer before an equal entry (webs are inserted in
//!             ascending web number: on an exact tie the higher number pops first)
//!   drop    = FPSpillWorthless 10710b32: every web with benefit <= 0 leaves the list (in list order), except, in the
//!             first call, members of FPInitialRegPressure's split set
//!   pop     = the list head; FPCheckColorability 10712541 keeps it on the x87 stack or not; not colourable: it is
//!             split (FPRemoveSplitGlobregs, ColProcessSplitGlobregs) and only the pieces are costed again and
//!             inserted into the remaining list; a kept web may make FPUpdateRegisterPressure 10711bad name webs to
//!             split (stack depth 8)
//!
//! `check` replays the events: every term against cost * w, every inserted web's prio against the sum of its terms,
//! the drops and every pop against the model list. The colourability answers are taken from the trace (they depend
//! on the x87 stack shape at each instruction, which the snapshot does not carry); `check --predict` says so per
//! decision. `need A>B` for x87 webs: the gap and the single edits that close it.

use serde::Deserialize;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet, HashMap};

#[derive(Deserialize)]
struct FSnap {
    func: String,
    #[serde(default)]
    passes: Vec<IPass>,
    #[serde(default)]
    fp: Option<Fp>,
}
#[derive(Deserialize)]
struct IPass {
    #[serde(default)]
    grs: HashMap<String, IGr>,
}
#[derive(Deserialize)]
struct IGr {
    name: String,
}
#[derive(Deserialize)]
struct Fp {
    passes: Vec<FPass>,
    events: Vec<Value>,
    #[serde(default)]
    pops: Vec<Value>,
}
#[derive(Deserialize)]
struct FPass {
    n: u32,
    blocks: Vec<FBlock>,
    grs: HashMap<String, FGr>,
}
#[derive(Deserialize)]
struct FBlock {
    b: u32,
    w: i64,
}
#[derive(Deserialize)]
struct FGr {
    terms: Vec<FTerm>,
}
#[derive(Deserialize, Clone)]
struct FTerm {
    b: u32,
    kind: String,
    cost: Option<i64>,
    w: i64,
    delta: i64,
    #[serde(default)]
    succ: Option<u32>,
}

/// One web as it enters the list.
#[derive(Clone)]
struct Web {
    gr: String,
    num: u32,
    name: String,
    npass: u32,
    prio: i64,  // model
    tprio: i64, // trace
    def: u32,
    seq: usize, // insertion index (for name@k)
}

fn load(path: &str) -> FSnap {
    let txt = std::fs::read_to_string(path).unwrap_or_else(|e| super::die(&format!("{}: {}", path, e)));
    serde_json::from_str(&txt).unwrap_or_else(|e| super::die(&format!("{}: {}", path, e)))
}

fn s(v: &Value, k: &str) -> String {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string()
}
fn i(v: &Value, k: &str) -> i64 {
    v.get(k).and_then(|x| x.as_i64()).unwrap_or(0)
}
fn set(v: &Value) -> BTreeSet<u32> {
    v.get("set").and_then(|x| x.as_array()).map(|a| a.iter().filter_map(|x| x.as_u64()).map(|x| x as u32).collect()).unwrap_or_default()
}

/// A source-level what-if on the FP terms (the same spellings as the int model where they mean the same).
#[derive(Clone, Debug)]
enum FEdit {
    Ref(String, u32, i64), // ref:VAR:B:COST   VAR gets a reference of COST * w_B in block B
    Unref(String, u32),    // unref:VAR:B      VAR loses its references (not its edge pops) in block B
    W(u32, i64),           // w:B:N            block B weighs N
}
impl std::fmt::Display for FEdit {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        match self {
            FEdit::Ref(v, b, c) => write!(f, "ref:{}:{}:{}", v, b, c),
            FEdit::Unref(v, b) => write!(f, "unref:{}:{}", v, b),
            FEdit::W(b, n) => write!(f, "w:{}:{}", b, n),
        }
    }
}
fn parse_edit(e: &str) -> Result<FEdit, String> {
    let p: Vec<&str> = e.split(':').collect();
    let n = |x: &str| x.parse::<i64>().map_err(|_| format!("bad number {:?} in {}", x, e));
    match (p[0], p.len()) {
        ("ref", 3) => Ok(FEdit::Ref(p[1].into(), n(p[2])? as u32, 1)),
        ("ref", 4) => Ok(FEdit::Ref(p[1].into(), n(p[2])? as u32, n(p[3])?)),
        ("unref", 3) | ("drop", 3) => Ok(FEdit::Unref(p[1].into(), n(p[2])? as u32)),
        ("w", 3) => Ok(FEdit::W(n(p[1])? as u32, n(p[2])?)),
        _ => Err(format!("x87 (FPColor) edit {:?}: use ref:VAR:B[:COST], unref:VAR:B or w:B:N", e)),
    }
}

struct Model<'a> {
    fp: &'a Fp,
    webs: Vec<Web>,                       // in insertion order
    names: HashMap<String, Vec<usize>>,   // name -> webs
    wmap: HashMap<(u32, u32), i64>,       // (pass, block) -> w (after edits)
    extra: HashMap<usize, i64>,           // web -> prio change from edits
}

impl<'a> Model<'a> {
    fn new(fp: &'a Fp) -> Model<'a> {
        let mut m = Model { fp, webs: vec![], names: HashMap::new(), wmap: HashMap::new(), extra: HashMap::new() };
        for p in &fp.passes {
            for b in &p.blocks {
                m.wmap.insert((p.n, b.b), b.w);
            }
        }
        for e in &fp.events {
            if s(e, "t") == "ins" {
                let k = m.webs.len();
                let w = Web {
                    gr: s(e, "gr"),
                    num: i(e, "num") as u32,
                    name: s(e, "name"),
                    npass: i(e, "npass") as u32,
                    prio: 0,
                    tprio: i(e, "prio"),
                    def: i(e, "defOrd") as u32,
                    seq: k,
                };
                m.names.entry(w.name.clone()).or_default().push(k);
                m.webs.push(w);
            }
        }
        m.recost();
        m
    }
    fn terms(&self, k: usize) -> Vec<FTerm> {
        let w = &self.webs[k];
        self.fp.passes.iter().find(|p| p.n == w.npass).and_then(|p| p.grs.get(&w.gr)).map(|g| g.terms.clone()).unwrap_or_default()
    }
    fn recost(&mut self) {
        for k in 0..self.webs.len() {
            let np = self.webs[k].npass;
            let mut sum = 0;
            for t in self.terms(k) {
                sum += match t.kind.as_str() {
                    "edge" => -self.wmap.get(&(np, t.succ.unwrap_or(0))).copied().unwrap_or(t.w),
                    _ => t.cost.unwrap_or(0) * self.wmap.get(&(np, t.b)).copied().unwrap_or(t.w),
                };
            }
            self.webs[k].prio = sum + self.extra.get(&k).copied().unwrap_or(0);
        }
    }
    /// VAR, VAR@k (k-th web of that name, insertion order) or VAR#gr
    fn find(&self, sel: &str) -> Result<Vec<usize>, String> {
        let (name, pick) = match sel.split_once('@') {
            Some((n, k)) if k.chars().all(|c| c.is_ascii_digit()) && !k.is_empty() => (n, Some(k)),
            _ => (sel, None),
        };
        let (name, gr) = match name.split_once('#') {
            Some((n, g)) => (n, Some(g)),
            None => (name, None),
        };
        let hits = self.names.get(name).cloned().unwrap_or_default();
        if hits.is_empty() {
            return Err(format!("no x87 web named {}", name));
        }
        if let Some(g) = gr {
            return Ok(hits.into_iter().filter(|&k| self.webs[k].gr == g).collect());
        }
        if let Some(k) = pick {
            let k: usize = k.parse().unwrap();
            return hits.get(k.wrapping_sub(1)).map(|&x| vec![x]).ok_or(format!("{} has {} webs", name, hits.len()));
        }
        Ok(hits)
    }
    fn apply(&mut self, e: &FEdit) -> Result<(), String> {
        match e {
            FEdit::Ref(v, b, c) => {
                for k in self.find(v)? {
                    let w = self.wmap.get(&(self.webs[k].npass, *b)).copied().unwrap_or(1);
                    *self.extra.entry(k).or_insert(0) += c * w;
                }
            }
            FEdit::Unref(v, b) => {
                for k in self.find(v)? {
                    let np = self.webs[k].npass;
                    let d: i64 = self
                        .terms(k)
                        .iter()
                        .filter(|t| t.b == *b && t.kind != "edge")
                        .map(|t| t.cost.unwrap_or(0) * self.wmap.get(&(np, t.b)).copied().unwrap_or(t.w))
                        .sum();
                    *self.extra.entry(k).or_insert(0) -= d;
                }
            }
            FEdit::W(b, n) => {
                let keys: Vec<(u32, u32)> = self.wmap.keys().filter(|k| k.1 == *b).copied().collect();
                for k in keys {
                    self.wmap.insert(k, *n);
                }
            }
        }
        self.recost();
        Ok(())
    }
}

/// FPAddToPriorityList: before the first entry whose (prio, defOrd) is <= the newcomer's.
fn insert(list: &mut Vec<usize>, webs: &[Web], k: usize) {
    let (p, d) = (webs[k].prio, webs[k].def);
    let at = list.iter().position(|&e| (webs[e].prio, webs[e].def) <= (p, d)).unwrap_or(list.len());
    list.insert(at, k);
}

#[derive(Default)]
struct Stats {
    terms: usize,
    bad_terms: usize,
    prio: usize,
    bad_prio: usize,
    pops: usize,
    pop_ok: usize,
    drops: usize,
    drop_ok: usize,
    kept: usize,
    nocolor: usize,
    rounds: usize,
    press: usize,
    init: usize,
    keeps: usize,
    notes: Vec<String>,
    why: BTreeMap<String, usize>,
    /// (web, list it was popped from) at every pop: for need
    at_pop: Vec<(usize, Vec<usize>)>,
}

/// Replay FPColor's events with the model list; decisions (drops are predicted, colourability is replayed).
fn replay(m: &Model, verbose: bool) -> Stats {
    let mut st = Stats::default();
    let mut list: Vec<usize> = vec![];
    let by_ins: Vec<usize> = (0..m.webs.len()).collect();
    let mut next_ins = 0usize;
    let mut first_worthless = true;
    // FPInitialRegPressure's split set, as far as the list is concerned: the webs the first split round takes out of
    // the list when that round comes before any pop (the set itself is logged only in part)
    let mut init: BTreeSet<String> = BTreeSet::new();
    let ev0 = &m.fp.events;
    if let Some(k) = ev0.iter().position(|e| matches!(s(e, "t").as_str(), "split" | "pop")) {
        if s(&ev0[k], "t") == "split" {
            init = ev0[k + 1..].iter().take_while(|e| s(e, "t") == "rm").map(|e| s(e, "gr")).collect();
        }
    }
    let mut pending_drops = false;
    let ev = &m.fp.events;
    let mut j = 0;
    while j < ev.len() {
        let e = &ev[j];
        let t = s(e, "t");
        if t != "ins" && pending_drops {
            // FPSpillWorthless right after the insertions: benefit (= prio) <= 0 leaves the list, in list order
            pending_drops = false;
            let model: Vec<usize> = list
                .iter()
                .copied()
                .filter(|&k| m.webs[k].prio <= 0 && !(first_worthless && init.contains(&m.webs[k].gr)))
                .collect();
            first_worthless = false;
            let mut traced = vec![];
            let mut q = j;
            while q < ev.len() && matches!(s(&ev[q], "t").as_str(), "drop" | "keep") {
                if s(&ev[q], "t") == "drop" {
                    traced.push(s(&ev[q], "gr"));
                } else {
                    st.keeps += 1;
                    if !init.contains(&s(&ev[q], "gr")) {
                        st.notes.push(format!("{} kept by FPSpillWorthless (initial split set) but not split first", s(&ev[q], "name")));
                    }
                }
                q += 1;
            }
            let mgr: Vec<String> = model.iter().map(|&k| m.webs[k].gr.clone()).collect();
            st.drops += traced.len().max(mgr.len());
            st.drop_ok += traced.iter().zip(&mgr).filter(|(a, b)| a == b).count();
            if traced != mgr {
                st.notes.push(format!(
                    "drops after pop {}: model [{}], trace [{}]",
                    st.pops,
                    model.iter().map(|&k| m.webs[k].name.clone()).collect::<Vec<_>>().join(" "),
                    traced.iter().map(|g| list.iter().find(|&&k| &m.webs[k].gr == g).map(|&k| m.webs[k].name.clone()).unwrap_or(g.clone())).collect::<Vec<_>>().join(" ")
                ));
            }
            list.retain(|&k| !traced.contains(&m.webs[k].gr));
            j = q;
            continue;
        }
        match t.as_str() {
            "init" => {
                if !set(e).is_empty() {
                    st.init += 1;
                }
            }
            "rm" => {
                let g = s(e, "gr");
                list.retain(|&k| m.webs[k].gr != g);
            }
            "ins" => {
                let k = by_ins[next_ins];
                next_ins += 1;
                st.prio += 1;
                if m.webs[k].prio != m.webs[k].tprio {
                    st.bad_prio += 1;
                    st.notes.push(format!("{} (web {}): model prio {} trace {}", m.webs[k].name, m.webs[k].num, m.webs[k].prio, m.webs[k].tprio));
                }
                insert(&mut list, &m.webs, k);
                pending_drops = true;
            }
            "pop" => {
                let g = s(e, "gr");
                st.pops += 1;
                let head = list.first().copied();
                let k = list.iter().copied().find(|&k| m.webs[k].gr == g);
                st.at_pop.push((k.unwrap_or(usize::MAX), list.clone()));
                if head.is_some() && head == k {
                    st.pop_ok += 1;
                } else {
                    st.notes.push(format!(
                        "pop {}: model {} , trace {}",
                        st.pops,
                        head.map(|h| format!("{} ({}, def {})", m.webs[h].name, m.webs[h].prio, m.webs[h].def)).unwrap_or("-".into()),
                        k.map(|h| format!("{} ({}, def {})", m.webs[h].name, m.webs[h].prio, m.webs[h].def)).unwrap_or(g.clone())
                    ));
                }
                if verbose {
                    if let Some(h) = k {
                        println!("  fp pop {:>3}  {:<16} prio {:>5} def {:>4} web {:>3}", st.pops, m.webs[h].name, m.webs[h].prio, m.webs[h].def, m.webs[h].num);
                    }
                }
            }
            "check" => {
                if let Some(w) = e.get("why").and_then(|x| x.as_array()) {
                    for x in w {
                        if i(x, "ret") != 0 {
                            *st.why.entry(s(x, "test")).or_insert(0) += 1;
                        }
                    }
                }
            }
            "kept" => {
                st.kept += 1;
                let g = s(e, "gr");
                list.retain(|&k| m.webs[k].gr != g);
            }
            "nocolor" => {
                st.nocolor += 1;
            }
            "press" => {
                if !set(e).is_empty() {
                    st.press += 1;
                }
            }
            "split" => {
                st.rounds += 1;
            }
            _ => {}
        }
        j += 1;
    }
    for p in &m.fp.passes {
        for g in p.grs.values() {
            for t in &g.terms {
                st.terms += 1;
                let want = match t.kind.as_str() {
                    "edge" => -t.w,
                    _ => t.cost.unwrap_or(0) * t.w,
                };
                if want != t.delta {
                    st.bad_terms += 1;
                }
            }
        }
    }
    st
}

pub fn cmd_check(path: &str, predict: bool, verbose: bool) {
    let sn = load(path);
    let Some(fp) = sn.fp.as_ref() else { return };
    if fp.events.is_empty() {
        return;
    }
    let m = Model::new(fp);
    let st = replay(&m, verbose);
    println!(
        "x87 (FPColor, cl 13.10) {}: {} costing passes, {} webs; terms {} ({} mismatches against cost*w), \
         priorities {} of {}, pops {} of {} in model order, worthless drops {} of {}",
        sn.func,
        fp.passes.len(),
        m.webs.len(),
        st.terms,
        st.bad_terms,
        st.prio - st.bad_prio,
        st.prio,
        st.pop_ok,
        st.pops,
        st.drop_ok,
        st.drops
    );
    let why: Vec<String> = st.why.iter().map(|(k, n)| format!("{} {}", k, n)).collect();
    println!(
        "  colourability (from the trace{}): {} kept on the stack, {} not colourable{}; {} split rounds ({} after a \
         depth split set, {} from FPInitialRegPressure)",
        if predict { "; FPCheckColorability is not modelled" } else { "" },
        st.kept,
        st.nocolor,
        if why.is_empty() { String::new() } else { format!(" (sub-tests that said split: {})", why.join(", ")) },
        st.rounds,
        st.press,
        st.init
    );
    for n in st.notes.iter().take(if verbose { 1000 } else { 10 }) {
        println!("  {}", n);
    }
    let _ = fp.pops.len();
}

/// True when every name in the constraints is an x87 web of this snapshot and none is an int globreg.
pub fn handles(path: &str, cons: &[(String, String)]) -> bool {
    let sn = load(path);
    let Some(fp) = sn.fp.as_ref() else { return false };
    let base = |x: &str| x.split(|c| c == '#' || c == '@').next().unwrap_or(x).to_string();
    let fpn: BTreeSet<String> = fp.events.iter().filter(|e| s(e, "t") == "ins").map(|e| s(e, "name")).collect();
    let intn: BTreeSet<String> = sn.passes.iter().flat_map(|p| p.grs.values().map(|g| g.name.clone())).collect();
    !cons.is_empty()
        && cons.iter().all(|(a, b)| {
            let (a, b) = (base(a), base(b));
            fpn.contains(&a) && fpn.contains(&b) && !intn.contains(&a) && !intn.contains(&b)
        })
}

/// The list in which a and b are first compared: the list at the first pop of either while both are in it.
fn decide(m: &Model, st: &Stats, a: &[usize], b: &[usize]) -> Option<(usize, usize, Vec<usize>)> {
    for (k, list) in &st.at_pop {
        let ia = list.iter().copied().find(|x| a.contains(x));
        let ib = list.iter().copied().find(|x| b.contains(x));
        if let (Some(x), Some(y)) = (ia, ib) {
            if a.contains(k) || b.contains(k) {
                return Some((x, y, list.clone()));
            }
        }
    }
    let _ = m;
    None
}

/// (def cost, use cost) of a web from its terms: a float defs at 2 and its uses cost 0; a double 4 and 1
/// (FPSpillcosts' FLDCostDif and def costs at /O2; constants count as floats).
fn costs(ts: &[FTerm]) -> (i64, i64) {
    let float = ts.iter().any(|t| matches!((t.kind.as_str(), t.cost), ("def", Some(2)) | ("enter", Some(-1)) | ("store", Some(-2))));
    let double = ts.iter().any(|t| matches!((t.kind.as_str(), t.cost), ("def", Some(4)) | ("enter", Some(-2)) | ("store", Some(-4)) | ("use", Some(1))));
    if double && !float {
        (4, 1)
    } else {
        (2, 0)
    }
}

/// a pops before b in a list holding both: (prio, defOrd) larger; on an exact tie the later insertion.
fn before(m: &Model, a: usize, b: usize) -> bool {
    let (wa, wb) = (&m.webs[a], &m.webs[b]);
    (wa.prio, wa.def) > (wb.prio, wb.def) || ((wa.prio, wa.def) == (wb.prio, wb.def) && wa.seq > wb.seq)
}

fn slack(m: &Model, a: usize, b: usize) -> i64 {
    let (wa, wb) = (&m.webs[a], &m.webs[b]);
    let tiewin = (wa.def, wa.seq) > (wb.def, wb.seq);
    wa.prio - wb.prio - if tiewin { 0 } else { 1 }
}

pub fn cmd_need(path: &str, cons: &[(String, String)], pre: &[String], top: usize) {
    let sn = load(path);
    let fp = sn.fp.as_ref().unwrap_or_else(|| super::die("this snapshot has no FPColor data"));
    let mut base = Model::new(fp);
    for e in pre {
        let ed = parse_edit(e).unwrap_or_else(|x| super::die(&x));
        base.apply(&ed).unwrap_or_else(|x| super::die(&x));
    }
    let st = replay(&base, false);
    let mut pairs = vec![];
    for (a, b) in cons {
        let wa = base.find(a).unwrap_or_else(|x| super::die(&x));
        let wb = base.find(b).unwrap_or_else(|x| super::die(&x));
        let Some((x, y, list)) = decide(&base, &st, &wa, &wb) else {
            println!("{} and {} are never in the FP list together (a split or a drop separates them); nothing to order", a, b);
            continue;
        };
        let (vx, vy) = (&base.webs[x], &base.webs[y]);
        let ok = before(&base, x, y);
        println!(
            "{} ({}, def {}, web {}) before {} ({}, def {}, web {}): slack {}  ({})   [x87, FPColor list of {} webs]",
            a, vx.prio, vx.def, vx.num, b, vy.prio, vy.def, vy.num, slack(&base, x, y), if ok { "holds" } else { "VIOLATED" }, list.len()
        );
        pairs.push((a.clone(), b.clone(), x, y));
    }
    if pairs.is_empty() {
        return;
    }
    // single edits: a reference (def cost, double use cost 1) of a winner in a block of its pass, or the loss of a
    // loser's references in one block, or a block weight
    let mut cands: Vec<FEdit> = vec![];
    for (a, b, x, y) in &pairs {
        let np = base.webs[*x].npass;
        let blocks: Vec<u32> = fp.passes.iter().find(|p| p.n == np).map(|p| p.blocks.iter().map(|b| b.b).collect()).unwrap_or_default();
        let (defc, usec) = costs(&base.terms(*x));
        for &bl in &blocks {
            for c in [usec, defc] {
                if c > 0 {
                    cands.push(FEdit::Ref(a.clone(), bl, c));
                }
            }
        }
        let mut seen = BTreeSet::new();
        for t in base.terms(*y) {
            if t.kind != "edge" && t.cost.unwrap_or(0) > 0 && seen.insert(t.b) {
                cands.push(FEdit::Unref(b.clone(), t.b));
            }
        }
        for t in base.terms(*x).iter().chain(base.terms(*y).iter()) {
            for n in [t.w / 2, t.w * 2] {
                if n >= 1 && n != t.w {
                    cands.push(FEdit::W(t.b, n));
                }
            }
        }
    }
    let mut seen = BTreeSet::new();
    cands.retain(|e| seen.insert(e.to_string()));
    let holds = pairs.iter().all(|(_, _, x, y)| before(&base, *x, *y));
    let mut good = vec![];
    for e in &cands {
        let mut mm = Model::new(fp);
        mm.wmap = base.wmap.clone();
        mm.extra = base.extra.clone();
        mm.recost();
        if mm.apply(e).is_err() {
            continue;
        }
        if pairs.iter().all(|(_, _, x, y)| before(&mm, *x, *y)) != holds {
            let worthless: Vec<String> = pairs.iter().flat_map(|(a, _, x, _)| if mm.webs[*x].prio <= 0 { Some(a.clone()) } else { None }).collect();
            let cost: i64 = pairs.iter().map(|(_, _, x, y)| (mm.webs[*x].prio - base.webs[*x].prio).abs() + (mm.webs[*y].prio - base.webs[*y].prio).abs()).sum();
            good.push((cost, e.to_string(), pairs.iter().map(|(_, _, x, y)| slack(&mm, *x, *y)).min().unwrap_or(0), worthless));
        }
    }
    good.sort();
    println!(
        "{} single edits tried, {} {} (x87 priorities have no pressure factor: an edit moves only its own web; \
         whether the web then stays on the stack is FPCheckColorability's, not modelled):",
        cands.len(),
        good.len(),
        if holds { "would break it" } else { "satisfy every constraint" }
    );
    let wof = |b: u32| fp.passes.iter().flat_map(|p| p.blocks.iter()).find(|x| x.b == b).map(|x| x.w).unwrap_or(1);
    for (cost, e, sl, wl) in good.iter().take(top) {
        let p: Vec<&str> = e.split(':').collect();
        let what = match p[0] {
            "ref" => format!("{} in block {} (w={}): {}", p[1], p[2], wof(p[2].parse().unwrap_or(0)), match p[3] {
                "1" => "one more use of a double",
                "2" => "one more def of a float",
                "4" => "one more def of a double",
                _ => "a reference",
            }),
            "unref" => format!("{} loses its references in block {} (w={})", p[1], p[2], wof(p[2].parse().unwrap_or(0))),
            _ => format!("block {} weighs {} (another loop depth)", p[1], p[2]),
        };
        println!(
            "  {:<20} prio change {:>4}  slack now {:>3}   {}{}",
            e,
            cost,
            sl,
            what,
            if wl.is_empty() { String::new() } else { format!("  ({} would become worthless: benefit <= 0)", wl.join(" ")) }
        );
    }
}
