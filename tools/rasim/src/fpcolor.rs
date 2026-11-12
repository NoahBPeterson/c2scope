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
//! the drops and every pop against the model list. With the x87 view in the snapshot (c2trace.dll `dumpfp`, x87.md
//! section 11.1) it also predicts every FPCheckColorability answer, its sub-test calls and split points (`fpcheck`),
//! and FPInitialRegPressure's / FPUpdateRegisterPressure's split sets, against the trace; the list replay itself
//! still follows the traced answers. `need A>B` for x87 webs: the gap and the single edits that close it;
//! `need stack:V`: what keeps V on the stack (`cmd_need_stack`).

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
    /// the x87 views (c2trace.dll dumpfp): each dump a list of indices into ilpool
    #[serde(default)]
    ils: Vec<Vec<usize>>,
    #[serde(default)]
    ilpool: Vec<String>,
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
    // FPCheckColorability predicted from the x87 view at each check
    let names: HashMap<String, String> = fp.events.iter().filter(|e| s(e, "t") == "ins").map(|e| (s(e, "gr"), s(e, "name"))).collect();
    let cs = predict_checks(fp, &names, verbose);
    if cs.with_view > 0 {
        let fired: Vec<String> = cs.fired.iter().map(|(k, n)| format!("{} {}", k, n)).collect();
        println!(
            "  FPCheckColorability predicted from the x87 view: {} of {} answers, sub-test calls {} of {}, split points {} of {}{}{}",
            cs.ok,
            cs.with_view,
            cs.calls_ok,
            cs.with_view,
            cs.splits_ok,
            cs.splits_n,
            if cs.with_view < cs.checks { format!(" ({} checks without a view)", cs.checks - cs.with_view) } else { String::new() },
            if fired.is_empty() { String::new() } else { format!("; sub-tests that returned 1 (trace): {}", fired.join(", ")) }
        );
        if cs.sunk + cs.enter > 0 {
            println!("  model: {} copy sinkings (InsertColoredLifetimeEndSplit), {} enterload moves (Y test)", cs.sunk, cs.enter);
        }
        for n in cs.notes.iter().take(if verbose { 1000 } else { 10 }) {
            println!("  {}", n);
        }
    } else if predict && cs.checks > 0 && fp.ils.is_empty() {
        println!("  (no x87 view in this snapshot: traced with a c2trace.dll without dumpfp; colourability not predicted)");
    }
    if !fp.ils.is_empty() {
        let il0 = fp_il0(path);
        let (ia, iok, pa, pok, ine, pne, notes) = predict_sets(fp, il0);
        println!(
            "  split sets predicted: FPInitialRegPressure {} of {} ({} non-empty), FPUpdateRegisterPressure (after a colouring) {} of {} ({} non-empty)",
            iok, ia, ine, pok, pa, pne
        );
        for n in notes.iter().take(if verbose { 1000 } else { 5 }) {
            println!("  {}", n);
        }
    }
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

// ---------------------------------------------------------------------------------------------------------------------
// FPCheckColorability (13.10.3077 10712541; 3052 10712535), predicted from the x87 view the snapshot carries at each
// check (c2trace.dll `dumpfp`, "T X" lines; tools/c2regs.py `_fpexport` keys ils / ilpool, a check event's `il`).
// Doc: docs/c2/x87.md section 11.1.  The view is the IL as FPColor has left it when the check starts: every tuple in
// list order, each block's live-in / live-out / referenced sets (BB+0x48/+0x4c/+0x50), its stack image (BB+0x60) and
// edges, the depth bytes +0x16 (values on the x87 stack) / +0x17 (temporaries) of each instruction, and per globreg its
// colour, def list (+0x38) and end list (+0x3c).  The functions below follow the 3077 code instruction by instruction
// (addresses in the comments); the check's own IL changes (an enterload moved, a copy sunk, a lifetime extended) are
// applied to the model IL as the code makes them.

#[derive(Clone, Debug, Default)]
struct Leaf {
    reg: bool,  // kind 1 (a register leaf)
    kind: u8,
    fp: bool,   // x87 type (type word & 0xf000 == 0x4000)
    dies: bool, // +0x15 & 0x10
    gr: u32,    // the globreg (kind-2 SYM) at +0x1c, 0 if none / another SYM
    col: bool,  // that globreg is coloured (+0x10 != 0)
    symk: u8,   // kind of the SYM at +0x1c (2 for a globreg, 0 none)
    isreg: bool, // +0x18 is a register SYM (kind 1); on a code-address leaf (kind 4): its SYM has +0x20 & 0x10000
}

#[derive(Clone, Debug, PartialEq)]
enum TK {
    Blk(usize),
    Lab,
    Other(u8),
    Ins,
}

#[derive(Clone, Debug)]
struct Tup {
    t: u32,
    tk: TK,
    op: u32,
    kind: u8,
    ty: u32,
    b16: i32,
    b17: i32,
    line: u32,
    dst: Vec<Leaf>,
    src: Vec<Leaf>,
}

#[derive(Clone, Debug, Default)]
struct Blk {
    bb: u32,
    inn: BTreeSet<u32>,
    out: BTreeSet<u32>,
    u: BTreeSet<u32>,
    stk: Vec<u32>,
    p: Vec<u32>,
    #[allow(dead_code)]
    s: Vec<u32>, // successors (not used by the checks; in the view for reading)
}

#[derive(Clone, Debug, Default)]
struct GWeb {
    gr: u32,
    num: u32,
    col: bool,
    fb: u32,
    lb: u32,
    defs: Vec<u32>,
    ends: Vec<u32>,
}

#[derive(Clone, Debug, Default)]
struct Il {
    tups: Vec<Tup>,
    blks: Vec<Blk>,
    bbix: HashMap<u32, usize>,
    webs: BTreeMap<u32, GWeb>, // by globreg
    bynum: HashMap<u32, u32>,  // web number -> globreg
}

fn hx(x: &str) -> u32 {
    u32::from_str_radix(x, 16).unwrap_or(0)
}
fn sbv(x: &str) -> BTreeSet<u32> {
    let mut out = BTreeSet::new();
    for it in x.split(',').filter(|s| !s.is_empty()) {
        if let Some((b, bits)) = it.split_once(':') {
            let (b, bits) = (hx(b), hx(bits));
            for k in 0..32 {
                if bits >> k & 1 != 0 {
                    out.insert(b + k);
                }
            }
        }
    }
    out
}
fn leaf(tok: &str) -> Leaf {
    if let Some(k) = tok.strip_prefix('k') {
        let nocall = k.ends_with('n');
        return Leaf { kind: k.trim_end_matches('n').parse().unwrap_or(0), isreg: nocall, ..Default::default() };
    }
    let mut l = Leaf { reg: true, kind: 1, ..Default::default() };
    let parts: Vec<&str> = tok.split('.').collect();
    let fl = parts[0].trim_start_matches('r');
    l.fp = fl.contains('f');
    l.dies = fl.contains('x');
    if let Some(sy) = parts.get(1) {
        if *sy == "0" {
        } else if let Some(k) = sy.strip_prefix('s') {
            l.symk = k.parse().unwrap_or(0);
        } else {
            l.symk = 2;
            l.col = sy.ends_with('c');
            l.gr = hx(sy.trim_end_matches('c'));
        }
    }
    l.isreg = parts.get(2) == Some(&"R");
    l
}

impl Il {
    fn parse(lines: &[&str]) -> Il {
        let mut il = Il::default();
        for ln in lines {
            let f: Vec<&str> = ln.split_whitespace().collect();
            if f.is_empty() {
                continue;
            }
            let kv = |k: &str| f.iter().find_map(|x| x.strip_prefix(k)).unwrap_or("");
            match f[0] {
                "B" => {
                    let b = Blk {
                        bb: hx(f[2]),
                        inn: sbv(kv("in=")),
                        out: sbv(kv("out=")),
                        u: sbv(kv("u=")),
                        stk: kv("stk=").split(',').map(hx).collect(),
                        p: kv("p=").split(',').filter(|x| !x.is_empty()).map(hx).collect(),
                        s: kv("s=").split(',').filter(|x| !x.is_empty()).map(hx).collect(),
                    };
                    il.bbix.insert(b.bb, il.blks.len());
                    il.tups.push(Tup { t: hx(f[1]), tk: TK::Blk(il.blks.len()), op: 0, kind: 26, ty: 0, b16: 0, b17: 0, line: 0, dst: vec![], src: vec![] });
                    il.blks.push(b);
                }
                "L" => il.tups.push(Tup { t: hx(f[1]), tk: TK::Lab, op: 0, kind: 27, ty: 0, b16: 0, b17: 0, line: 0, dst: vec![], src: vec![] }),
                "N" => {
                    let k: u8 = f.get(2).and_then(|x| x.parse().ok()).unwrap_or(0);
                    il.tups.push(Tup { t: hx(f[1]), tk: TK::Other(k), op: f.get(3).map(|x| hx(x)).unwrap_or(0), kind: k, ty: 0, b16: 0, b17: 0, line: 0, dst: vec![], src: vec![] })
                }
                "I" => {
                    let (head, rest) = ln.split_once('|').unwrap_or((ln, ""));
                    let (d, s) = rest.split_once('|').unwrap_or((rest, ""));
                    let h: Vec<&str> = head.split_whitespace().collect();
                    il.tups.push(Tup {
                        t: hx(h[1]),
                        tk: TK::Ins,
                        op: hx(h[2]),
                        kind: h[3].parse().unwrap_or(0),
                        ty: hx(h[4]),
                        b16: h[5].parse().unwrap_or(0),
                        b17: h[6].parse().unwrap_or(0),
                        line: h[7].parse().unwrap_or(0),
                        dst: d.split_whitespace().map(leaf).collect(),
                        src: s.split_whitespace().map(leaf).collect(),
                    });
                }
                "W" => {
                    let w = GWeb {
                        gr: hx(f[1]),
                        num: f[2].parse().unwrap_or(0),
                        col: hx(f[3]) != 0,
                        fb: hx(f[5]),
                        lb: hx(f[6]),
                        defs: kv("d=").split(',').filter(|x| !x.is_empty()).map(hx).collect(),
                        ends: kv("e=").split(',').filter(|x| !x.is_empty()).map(hx).collect(),
                    };
                    il.bynum.insert(w.num, w.gr);
                    il.webs.insert(w.gr, w);
                }
                _ => {}
            }
        }
        il
    }
    fn ix(&self, t: u32) -> Option<usize> {
        self.tups.iter().position(|x| x.t == t)
    }
    fn ins(&self, i: usize) -> bool {
        self.tups[i].tk == TK::Ins
    }
    /// an instruction GetLiveColoredAtInstr / SplitAtMismatchedFlowJoinPoints look at: x87 type or opcode 0x5b
    fn fpish(&self, i: usize) -> bool {
        let t = &self.tups[i];
        t.tk == TK::Ins && ((t.ty & 0xf000) == 0x4000 || t.op == 0x5b)
    }
    /// the block a tuple is in (walk back to a BLOCK tuple that has a BB)
    fn blk_of(&self, i: usize) -> Option<usize> {
        (0..=i).rev().find_map(|j| if let TK::Blk(b) = self.tups[j].tk { Some(b) } else { None })
    }
    fn blk_start(&self, b: usize) -> usize {
        self.tups.iter().position(|x| x.tk == TK::Blk(b)).unwrap_or(0)
    }
    fn num(&self, gr: u32) -> u32 {
        self.webs.get(&gr).map(|w| w.num).unwrap_or(u32::MAX)
    }
    fn coloured(&self) -> BTreeSet<u32> {
        self.webs.values().filter(|w| w.col).map(|w| w.num).collect()
    }
    fn web_by_num(&self, n: u32) -> Option<&GWeb> {
        self.bynum.get(&n).and_then(|g| self.webs.get(g))
    }
}

/// GetLiveColoredAtInstr 10712949: S ∩ live-in of the instruction's block, then the block walked up to (not
/// including) it: a dying source of a coloured globreg leaves, a coloured globreg written enters (x87 instructions).
fn live_col(il: &Il, s: &BTreeSet<u32>, i: usize) -> BTreeSet<u32> {
    let Some(b) = il.blk_of(i) else { return BTreeSet::new() };
    let mut set: BTreeSet<u32> = s.intersection(&il.blks[b].inn).copied().collect();
    if let TK::Blk(_) = il.tups[i].tk {
        return set;
    }
    for j in il.blk_start(b)..i {
        if !il.fpish(j) {
            continue;
        }
        for l in &il.tups[j].src {
            if l.reg && l.dies && l.symk == 2 && l.col {
                set.remove(&il.num(l.gr));
            }
        }
        for l in &il.tups[j].dst {
            if l.reg && l.symk == 2 && l.col {
                set.insert(il.num(l.gr));
            }
        }
    }
    set
}

/// one sub-test call, as the trace logs it (why[]): test end / join / post / ext, its instruction, the coloured
/// web passed (end), what it returned
#[derive(Clone, Debug, PartialEq)]
struct Call {
    test: &'static str,
    at: u32,
    c: u32, // globreg passed as the coloured web (end), 0 none
    ret: i64,
}

#[derive(Default, Debug)]
struct CheckOut {
    ok: bool,
    calls: Vec<Call>,
    splits: Vec<u32>,  // ColMakeSplitPoint tuples, in order
    fsplits: Vec<u32>, // FPMakeSplitPoint tuples
    blockers: Vec<(u32, u32, &'static str)>, // (coloured web num, its instruction, rule)
    sunk: usize,   // InsertColoredLifetimeEndSplit's copy sinking (no split, C ends with W)
    enter: usize,  // W's enterload moved below coloured enterloads
}

struct Chk<'a> {
    il: Il,
    w: u32, // W's globreg
    s: BTreeSet<u32>,
    moved: bool, // InsertColoredLifetimeEndSplit's copy sinking happened in this check (mv[2] != 0)
    marks: u32,
    out: CheckOut,
    _p: std::marker::PhantomData<&'a ()>,
}

impl<'a> Chk<'a> {
    fn wnum(&self) -> u32 {
        self.il.num(self.w)
    }
    /// ColMakeSplitPoint 107967b2 / FPMakeSplitPoint 10823328: record the split point and insert its marker tuple
    /// (TupAlloc, opcode 0x2c7, not an instruction) right after the tuple, as the code does: a later walk of the same
    /// check that steps over it stops there (InsertColoredLifetimeEndSplit's copy sinking, for one)
    fn mark(&mut self, t: u32, fp: bool) {
        if fp {
            self.out.fsplits.push(t);
        } else {
            self.out.splits.push(t);
        }
        if let Some(i) = self.il.ix(t) {
            let n = self.marks;
            self.marks += 1;
            self.il.tups.insert(i + 1, Tup { t: 0xe0000000 | n, tk: TK::Other(0x1c), op: 0x2c7, kind: 0x1c, ty: 0, b16: 0, b17: 0, line: 0, dst: vec![], src: vec![] });
        }
    }
    /// InsertColoredLifetimeEndSplit 10711cce (P, W, C or 0): is W live at P (P's own operands included)?  Then the
    /// copy of C right before P may sink to W's end (C ends with W: no split); else W is split at P.
    fn icles(&mut self, p_t: u32, c: u32, rule: &'static str) -> bool {
        let il = &self.il;
        let w = self.w;
        let wn = self.wnum();
        let Some(p) = il.ix(p_t) else { return false };
        let Some(b) = il.blk_of(p) else { return false };
        let ret = (|| {
            if !il.blks[b].u.contains(&wn) {
                return 0; // 10711d1a: W not in BB+0x50
            }
            let mut live = il.blks[b].inn.contains(&wn); // 107ec9e9
            for j in il.blk_start(b)..=p {
                if !il.ins(j) {
                    continue;
                }
                if live {
                    if let Some(l) = il.tups[j].src.iter().find(|l| l.reg && l.gr == w) {
                        if l.dies {
                            live = false;
                        }
                    }
                } else if il.tups[j].dst.iter().any(|l| l.reg && l.gr == w) {
                    live = true;
                }
            }
            if !live {
                return 0;
            }
            1
        })();
        if ret == 0 {
            self.out.calls.push(Call { test: "end", at: p_t, c, ret: 0 });
            return false;
        }
        // 107eca81: the copy-sinking case
        let wend = self.il.webs.get(&w).map(|x| x.ends.clone()).unwrap_or_default();
        let cends = self.il.webs.get(&c).map(|x| x.ends.clone()).unwrap_or_default();
        let il = &self.il;
        if !self.moved && c != 0 && cends.len() == 1 && wend.len() == 1 {
            let e_t = wend[0];
            let ei = il.ix(e_t);
            let pblk = matches!(il.tups[p].tk, TK::Blk(_));
            if let Some(ei) = ei {
                let eblk = matches!(il.tups[ei].tk, TK::Blk(_));
                let mv = p.checked_sub(1).map(|q| &il.tups[q]);
                let copy = mv.map(|m| m.op == 0x63 && m.src.first().map(|l| l.gr == c).unwrap_or(false)).unwrap_or(false);
                if !eblk && !pblk && copy {
                    // walk P .. E: no copy of a globreg, depth < 7, no coloured globreg or register written
                    let mut t = p;
                    let reach = loop {
                        if !il.ins(t) {
                            break t == ei;
                        }
                        let x = &il.tups[t];
                        if x.op == 0x63 && x.src.first().map(|l| l.reg && l.symk == 2).unwrap_or(false) {
                            break p == ei;
                        }
                        if x.b16 >= 7 {
                            break p == ei;
                        }
                        if let Some(d) = x.dst.first() {
                            if d.fp && d.reg && ((d.symk == 2 && d.col) || d.isreg) {
                                break p == ei;
                            }
                        }
                        if t == ei {
                            break true;
                        }
                        t += 1;
                        if t >= il.tups.len() {
                            break false;
                        }
                    };
                    if reach {
                        // 107ecb6d: the copy moves to W's end; C now ends there (no split)
                        let m = self.il.tups.remove(p - 1);
                        let ei = self.il.ix(e_t).unwrap();
                        let before = self.il.tups[ei].dst.first().map(|l| l.fp).unwrap_or(false);
                        self.il.tups.insert(if before { ei } else { ei + 1 }, m);
                        if let Some(cw) = self.il.webs.get_mut(&c) {
                            cw.ends = vec![e_t];
                        }
                        self.moved = true;
                        self.out.sunk += 1;
                        self.out.calls.push(Call { test: "end", at: p_t, c, ret: 0 });
                        return false;
                    }
                }
            }
        }
        // 107ecc1e: split W at P (at the leave-store after P if there is one)
        let il = &self.il;
        let sp = if p + 1 < il.tups.len() && il.tups[p + 1].op == 0x26b && il.ins(p + 1) { il.tups[p + 1].t } else { p_t };
        let x = il.tups[p].clone();
        self.mark(sp, false);
        // a copy of a globreg at P with temporaries under it: W is also split around each later use in the expression
        if x.op == 0x63 && x.dst.first().map(|l| l.reg && l.symk == 2).unwrap_or(false) && x.b17 != 0 {
            let mut t = self.il.ix(p_t).unwrap() + 1;
            while t < self.il.tups.len() && !matches!(self.il.tups[t].tk, TK::Blk(_)) {
                if self.il.ins(t) && self.il.tups[t].op != 0x26a && self.il.tups[t].op != 0x26b {
                    if self.il.tups[t].b17 == 0 {
                        break;
                    }
                    if self.il.tups[t].src.iter().any(|l| l.reg && l.gr == w) {
                        let (a, b) = (self.il.tups[t].t, self.il.tups[t - 1].t);
                        self.mark(a, true);
                        self.mark(b, true);
                    }
                }
                t += 1;
            }
        }
        let cn = if c != 0 { self.il.num(c) } else { 0 };
        self.out.blockers.push((cn, p_t, rule));
        self.out.calls.push(Call { test: "end", at: p_t, c, ret: 1 });
        true
    }

    /// CanExtendLifetime 10823b50 (W, its end e, the common end of the later webs, Y): W's last use may move down to
    /// `target` if nothing in between defines a globreg, lets a coloured web outside Y die, or is 8 deep, and at most
    /// one block boundary is crossed (every predecessor of that block has W live out).
    fn can_extend(&mut self, e_t: u32, target: u32, y: &BTreeSet<u32>) -> bool {
        let il = &self.il;
        let w = self.w;
        let wn = self.wnum();
        let (Some(mut t), Some(tg)) = (il.ix(e_t), il.ix(target)) else { return false };
        // back from e to W's dying use (not past a block start)
        let u = loop {
            if il.ins(t) && il.tups[t].src.iter().any(|l| l.reg && l.dies && l.gr == w) {
                break t;
            }
            if t == 0 {
                return false;
            }
            t -= 1;
            if matches!(il.tups[t].tk, TK::Blk(_)) {
                return false;
            }
        };
        let mut crossed: Option<usize> = None;
        let mut q = u;
        loop {
            if q == tg {
                break;
            }
            match il.tups[q].tk {
                TK::Blk(b) => {
                    if crossed.is_some() {
                        return false;
                    }
                    crossed = Some(b);
                    for p in &il.blks[b].p {
                        if !il.bbix.get(p).map(|&k| il.blks[k].out.contains(&wn)).unwrap_or(false) {
                            return false;
                        }
                    }
                }
                TK::Ins => {
                    let x = &il.tups[q];
                    if let Some(d) = x.dst.first() {
                        if d.fp && d.reg && d.symk == 2 {
                            return false;
                        }
                    }
                    for l in &x.src {
                        if l.fp && l.reg && l.dies && l.symk == 2 && l.col && !y.contains(&il.num(l.gr)) {
                            return false;
                        }
                    }
                    if x.b16 == 8 {
                        return false;
                    }
                }
                _ => {}
            }
            q += 1;
            if q >= il.tups.len() {
                return false;
            }
        }
        // extended: W's use no longer dies, a dummy dying use sits before the target
        if let Some(b) = crossed {
            self.il.blks[b].inn.insert(wn);
            self.il.blks[b].u.insert(wn);
        }
        if let Some(l) = self.il.tups[u].src.iter_mut().find(|l| l.reg && l.gr == w) {
            l.dies = false;
        }
        let dummy = Tup {
            t: 0xd0000000 | target,
            tk: TK::Ins,
            op: 0x2ab,
            kind: 0x15,
            ty: 0x4008,
            b16: 0,
            b17: 0,
            line: 0,
            dst: vec![],
            src: vec![Leaf { reg: true, kind: 1, fp: true, dies: true, gr: w, col: false, symk: 2, isreg: false }],
        };
        self.il.tups.insert(tg, dummy);
        true
    }

    /// GetFPStackLevel 10822dc4: the number of coloured values under the temporaries at a def
    fn level(&self, d_t: u32) -> i32 {
        let il = &self.il;
        let Some(i) = il.ix(d_t) else { return 0 };
        let x = &il.tups[i];
        if x.op != 0x26a && x.op != 0x26b {
            return x.b16 - x.b17;
        }
        let mut cnt = 0;
        let mut t = i;
        loop {
            if t == 0 {
                return cnt;
            }
            t -= 1;
            let y = &il.tups[t];
            if let TK::Blk(b) = y.tk {
                let Some(p) = il.blks[b].p.first() else { return cnt };
                let n = il.bbix.get(p).map(|&k| il.blks[k].stk.iter().take(8).take_while(|&&g| g != 0).count()).unwrap_or(0);
                return n as i32 + cnt;
            }
            if y.tk == TK::Ins && ((y.ty & 0xf000) == 0x4000 || y.op == 0x5b || y.kind == 0xf || (y.kind == 0x16 && y.op == 0x2a0)) {
                if y.op == 0x26a {
                    if y.dst.first().map(|l| l.col).unwrap_or(false) {
                        cnt += 1;
                    }
                    continue;
                }
                if y.op == 0x26b {
                    continue;
                }
                return y.b16 + cnt;
            }
        }
    }

    /// SplitAtMismatchedFlowJoinPoints 10822e8a: W's position on the stack at the end of each block of its range
    /// (stack images simulated from a predecessor's BB+0x60); a join whose predecessors disagree is a split point.
    fn joins(&mut self) -> bool {
        let il = &self.il;
        let w = self.w;
        let wn = self.wnum();
        let Some(wb) = il.webs.get(&w) else { return false };
        let (Some(&b0), Some(&b1)) = (il.bbix.get(&wb.fb), il.bbix.get(&wb.lb)) else { return false };
        let range: Vec<usize> = (b0..=b1.max(b0)).collect();
        let mut lvl: HashMap<usize, usize> = HashMap::new();
        let mut q: std::collections::VecDeque<usize> = range.iter().copied().filter(|&b| il.blks[b].out.contains(&wn)).collect();
        let mut guard = 0;
        while let Some(b) = q.pop_front() {
            guard += 1;
            if guard > 100000 {
                break;
            }
            let bk = &il.blks[b];
            let mut st: Vec<u32>;
            if bk.inn.contains(&wn) {
                let p = bk.p.iter().filter_map(|p| il.bbix.get(p)).find(|&&k| lvl.get(&k).copied().unwrap_or(0) != 0);
                let Some(&p) = p else {
                    q.push_back(b);
                    continue;
                };
                let lv = lvl[&p];
                let ps = &il.blks[p].stk;
                st = vec![0; 8];
                for k in 0..lv - 1 {
                    st[k] = ps.get(k).copied().unwrap_or(0);
                }
                st[lv - 1] = w;
                for k in lv..8 {
                    st[k] = ps.get(k - 1).copied().unwrap_or(0);
                }
            } else {
                st = bk.p.first().and_then(|p| il.bbix.get(p)).map(|&k| il.blks[k].stk.clone()).unwrap_or_else(|| vec![0; 8]);
                st.resize(8, 0);
            }
            let mut top: i64 = st.iter().take_while(|&&g| g != 0).count() as i64 - 1;
            st.resize(16, 0);
            let start = il.blk_start(b);
            let mut j = start + 1;
            while j < il.tups.len() && !matches!(il.tups[j].tk, TK::Blk(_)) && !matches!(il.tups[j].tk, TK::Other(26)) {
                if il.fpish(j) {
                    for l in &il.tups[j].src {
                        if l.reg && l.dies && l.symk == 2 {
                            if !l.col && l.gr != w {
                                continue;
                            }
                            if !l.col && top >= 0 && st[top as usize] != w && top > 0 {
                                if let Some(k) = (0..top as usize).find(|&k| st[k] == w) {
                                    st.remove(k);
                                    st.push(0);
                                }
                            }
                            if top >= 0 {
                                st[top as usize] = 0;
                            }
                            top -= 1;
                        }
                    }
                    for l in &il.tups[j].dst {
                        if l.reg && l.symk == 2 && (l.col || l.gr == w) {
                            let present = (0..=top.max(-1)).any(|k| k >= 0 && st[k as usize] == l.gr);
                            if !present {
                                top += 1;
                                if (top as usize) < st.len() {
                                    st[top as usize] = l.gr;
                                }
                            }
                        }
                    }
                }
                j += 1;
            }
            let pos = st.iter().position(|&g| g == w).map(|k| k + 1).unwrap_or(0);
            lvl.insert(b, pos);
        }
        let mut ret = false;
        let mut sps = vec![];
        for &b in &range {
            let bk = &il.blks[b];
            if !bk.inn.contains(&wn) || bk.p.is_empty() {
                continue;
            }
            let l0 = il.bbix.get(&bk.p[0]).and_then(|k| lvl.get(k)).copied().unwrap_or(0);
            if bk.p[1..].iter().any(|p| il.bbix.get(p).and_then(|k| lvl.get(k)).copied().unwrap_or(0) != l0) {
                let s = il.blk_start(b);
                let sp = if s + 1 < il.tups.len() && il.tups[s + 1].op == 0x26b { il.tups[s + 1].t } else { il.tups[s].t };
                sps.push((sp, il.tups[s].t));
                ret = true;
            }
        }
        for (sp, bt) in sps {
            self.mark(sp, false);
            self.out.blockers.push((0, bt, "join"));
        }
        ret
    }

    /// SplitAtPostDefEndPoints 10712528 (W, an end e): back from e to W's last use; if, inside the same expression
    /// (temporaries on the stack: +0x17 != 0), an instruction before it defines another globreg, W is split there.
    fn post(&mut self, e_t: u32) -> bool {
        let il = &self.il;
        let w = self.w;
        let Some(mut p) = il.ix(e_t) else { return false };
        if !il.ins(p) {
            return false;
        }
        loop {
            if il.tups[p].src.iter().any(|l| l.reg && l.gr == w) {
                if p == 0 || !il.ins(p - 1) {
                    return false;
                }
                let mut t = p - 1;
                loop {
                    let x = &il.tups[t];
                    if x.b17 == 0 && x.op != 0x26a && x.op != 0x26b {
                        return false;
                    }
                    if let Some(d) = x.dst.first() {
                        if (d.fp && d.reg && d.symk == 2 && x.op != 0x26a) && d.gr != w {
                            break;
                        }
                    }
                    if t == 0 || !il.ins(t - 1) {
                        return false;
                    }
                    t -= 1;
                }
                let a = if p + 1 < il.tups.len() && il.tups[p + 1].op == 0x26b { il.tups[p + 1].t } else { il.tups[p].t };
                let b = if il.tups[p].op == 0x26b { il.tups[p].t } else { il.tups[p - 1].t };
                let blk = (il.num(il.tups[t].dst[0].gr), il.tups[t].t, "post");
                self.mark(a, false);
                self.mark(b, false);
                self.out.blockers.push(blk);
                return true;
            }
            loop {
                if p == 0 {
                    return false;
                }
                p -= 1;
                if il.ins(p) {
                    break;
                }
            }
        }
    }
}

/// FPCheckColorability 10712541 for globreg w on the view il.
fn fpcheck(il: &Il, w: u32) -> CheckOut {
    let mut ck = Chk { il: il.clone(), w, s: il.coloured(), moved: false, marks: 0, out: CheckOut::default(), _p: std::marker::PhantomData };
    let Some(wb) = ck.il.webs.get(&w).cloned() else { return CheckOut { ok: true, ..Default::default() } };
    // the lists are built by prepending: both loops run over W's defs and ends in reverse list order
    let dl: Vec<(u32, BTreeSet<u32>)> = wb.defs.iter().rev().map(|&d| (d, ck.il.ix(d).map(|i| live_col(&ck.il, &ck.s, i)).unwrap_or_default())).collect();
    let mut el: Vec<(u32, BTreeSet<u32>)> =
        wb.ends.iter().rev().map(|&e| (e, ck.il.ix(e).map(|i| live_col(&ck.il, &ck.s, i)).unwrap_or_default())).collect();
    let nends = el.len();
    let mut split = false;
    let mut ext = false;
    for (d, ld) in &dl {
        for k in 0..el.len() {
            let (e, le) = el[k].clone();
            // X: coloured below W at its def, gone at its end: such a C ends while W is above it
            let x: Vec<u32> = ld.difference(&le).copied().collect();
            for c in x {
                let Some(cw) = ck.il.web_by_num(c).cloned() else { continue };
                if cw.ends.contains(&e) {
                    continue;
                }
                for ce in cw.ends.clone() {
                    split |= ck.icles(ce, cw.gr, "end");
                }
            }
            // Y: coloured at W's end but not at its def: a C pushed above W that is still there when W ends
            let mut y: BTreeSet<u32> = le.difference(ld).copied().collect();
            if !y.is_empty() {
                if let Some(di) = ck.il.ix(*d) {
                    if ck.il.tups[di].op == 0x26a {
                        // 107ec3b0: W's enterload moves below the coloured enterloads right after it
                        let mut t = di + 1;
                        while t < ck.il.tups.len() && !ck.il.ins(t) && ck.il.tups[t].kind != 27 {
                            t += 1;
                        }
                        let mut cur = di;
                        while t < ck.il.tups.len() && ck.il.tups[t].op == 0x26a {
                            let g = ck.il.tups[t].dst.first().cloned().unwrap_or_default();
                            if g.col && y.contains(&ck.il.num(g.gr)) {
                                y.remove(&ck.il.num(g.gr));
                                ck.out.enter += 1;
                                let m = ck.il.tups.remove(cur);
                                // t shifted down by one after the removal
                                ck.il.tups.insert(t, m);
                                cur = t;
                                t += 1;
                            } else {
                                t += 1;
                            }
                        }
                    }
                }
            }
            if !y.is_empty() {
                let mut done = false;
                if k + 1 == nends {
                    // the head of W's end list (the reversed list's last node, 107ec511): every C in Y has one end,
                    // all the same: W's end may be extended to it
                    let mut common: Option<u32> = None;
                    let mut ok = true;
                    for c in &y {
                        let ce = ck.il.web_by_num(*c).map(|x| x.ends.clone()).unwrap_or_default();
                        if ce.len() != 1 {
                            ok = false;
                            break;
                        }
                        match common {
                            None => common = Some(ce[0]),
                            Some(z) if z != ce[0] => {
                                ok = false;
                                break;
                            }
                            _ => {}
                        }
                    }
                    if let (true, Some(z)) = (ok, common) {
                        if z != e {
                            let r = ck.can_extend(e, z, &y);
                            ck.out.calls.push(Call { test: "ext", at: e, c: 0, ret: r as i64 });
                            if r {
                                el[k].0 = z;
                                if let Some(ww) = ck.il.webs.get_mut(&w) {
                                    if let Some(h) = ww.ends.first_mut() {
                                        *h = z; // only the head node's instruction changes (107ec5a0)
                                    }
                                }
                                ext = true;
                                done = true;
                            }
                        }
                    }
                }
                if !done {
                    let e_now = el[k].0;
                    for c in y.clone() {
                        let Some(cw) = ck.il.web_by_num(c).cloned() else { continue };
                        if cw.ends.contains(&e_now) {
                            continue;
                        }
                        for cd in cw.defs.clone() {
                            if ck.icles(cd, 0, "start") {
                                split = true;
                                ck.out.blockers.last_mut().map(|b| b.0 = c);
                            }
                        }
                    }
                }
            }
        }
        // every coloured web under W at its def: none of its ends may come while W is live
        for c in ld.clone() {
            let Some(cw) = ck.il.web_by_num(c).cloned() else { continue };
            for ce in cw.ends.clone() {
                split |= ck.icles(ce, cw.gr, "end");
            }
        }
    }
    let _ = ext;
    if split {
        ck.out.ok = false;
        return ck.out;
    }
    let defs = ck.il.webs.get(&w).map(|x| x.defs.clone()).unwrap_or_default();
    if defs.len() > 1 {
        let l0 = ck.level(defs[0]);
        if defs[1..].iter().any(|&d| ck.level(d) != l0) {
            let r = ck.joins();
            ck.out.calls.push(Call { test: "join", at: 0, c: 0, ret: r as i64 });
            if r {
                ck.out.ok = false;
                return ck.out;
            }
        }
    }
    let ends = ck.il.webs.get(&w).map(|x| x.ends.clone()).unwrap_or_default();
    let mut any = false;
    for e in ends {
        let r = ck.post(e);
        ck.out.calls.push(Call { test: "post", at: e, c: 0, ret: r as i64 });
        any |= r;
    }
    ck.out.ok = !any;
    ck.out
}

/// The view of dump k of the snapshot
fn view(fp: &Fp, k: usize) -> Option<Il> {
    let d = fp.ils.get(k)?;
    let lines: Vec<&str> = d.iter().filter_map(|&x| fp.ilpool.get(x).map(|s| s.as_str())).collect();
    Some(Il::parse(&lines))
}

#[derive(Default)]
struct CStats {
    checks: usize,
    with_view: usize,
    ok: usize,
    calls_ok: usize,
    splits_ok: usize,
    splits_n: usize,
    notes: Vec<String>,
    fired: BTreeMap<&'static str, usize>,
    sunk: usize,
    enter: usize,
}

/// Predict every FPCheckColorability answer that has a view, against the trace (answer, sub-test calls, split points).
fn predict_checks(fp: &Fp, names: &HashMap<String, String>, verbose: bool) -> CStats {
    let mut st = CStats::default();
    let ev = &fp.events;
    for (k, e) in ev.iter().enumerate() {
        if s(e, "t") != "check" {
            continue;
        }
        st.checks += 1;
        let Some(ilk) = e.get("il").and_then(|x| x.as_u64()) else { continue };
        let Some(il) = view(fp, ilk as usize) else { continue };
        st.with_view += 1;
        let w = hx(&s(e, "gr"));
        let out = fpcheck(&il, w);
        st.sunk += out.sunk;
        st.enter += out.enter;
        let tok = i(e, "ok") != 0;
        let name = names.get(&s(e, "gr")).cloned().unwrap_or(s(e, "gr"));
        // traced calls and split points (the csp / fsp events until the check's outcome)
        let tcalls: Vec<(String, u32, u32, i64)> = e
            .get("why")
            .and_then(|x| x.as_array())
            .map(|a| a.iter().map(|x| (s(x, "test"), hx(&s(x, "at")), hx(&s(x, "c")), i(x, "ret"))).collect())
            .unwrap_or_default();
        let mut tsp = vec![];
        let mut tfsp = vec![];
        for x in &ev[k + 1..] {
            match s(x, "t").as_str() {
                "csp" => tsp.push(hx(&s(x, "tup"))),
                "fsp" => tfsp.push(hx(&s(x, "tup"))),
                "kept" | "nocolor" | "pop" | "pass" | "check" => break,
                _ => {}
            }
        }
        let mcalls: Vec<(String, u32, u32, i64)> = out.calls.iter().map(|c| (c.test.to_string(), c.at, c.c, c.ret)).collect();
        // the trace logs `at` and `c` for end calls only
        let norm = |v: &[(String, u32, u32, i64)]| -> Vec<(String, u32, u32, i64)> {
            v.iter().map(|(t, a, c, r)| if t == "end" { (t.clone(), *a, *c, (*r != 0) as i64) } else { (t.clone(), 0, 0, (*r != 0) as i64) }).collect()
        };
        let calls_same = norm(&mcalls) == norm(&tcalls);
        let mut msp = out.splits.clone();
        msp.extend(out.fsplits.iter());
        let mut tall = tsp.clone();
        tall.extend(tfsp.iter());
        let sp_same = msp == tall;
        if out.ok == tok {
            st.ok += 1;
        }
        if calls_same {
            st.calls_ok += 1;
        }
        st.splits_n += 1;
        if sp_same {
            st.splits_ok += 1;
        }
        for (t, _, _, r) in &tcalls {
            if *r != 0 {
                *st.fired.entry(match t.as_str() {
                    "end" => "end",
                    "join" => "join",
                    "post" => "post",
                    "ext" => "ext",
                    _ => "?",
                })
                .or_insert(0) += 1;
            }
        }
        if out.ok != tok || !calls_same || !sp_same || verbose {
            st.notes.push(format!(
                "check {} (web {}): model {} trace {}{}{}",
                name,
                i(e, "num"),
                if out.ok { "colourable" } else { "split" },
                if tok { "colourable" } else { "split" },
                if calls_same { String::new() } else { format!("; calls model {:?} trace {:?}", mcalls, tcalls) },
                if sp_same { String::new() } else { format!("; split points model {:x?} trace {:x?}", msp, tall) }
            ));
        }
    }
    st
}

/// LfFPRegsPressure 1071947c: the temporaries an instruction pushes (+) or pops (-)
fn lf_press(t: &Tup) -> i32 {
    let callish = t.kind == 0xf || (t.kind == 0x16 && t.op == 0x2a0) || t.op == 0x2ab || t.op == 0x26d;
    if !callish {
        if (t.ty & 0xf000) == 0x4000 || t.op == 0x5b {
            return match t.op {
                0x4b | 0x4c | 0x4d | 0x4e | 0x4f | 0x50 | 0x53 | 0x5b | 0x5d | 0x63 | 0x69 | 0x86 | 0x91 | 0x92 | 0xd2 | 0x108 | 0x10a => -1,
                0x56 | 0x60 | 0x64 | 0x7f | 0x80 | 0x81 | 0x82 | 0x83 | 0x84 | 0x88 | 0x90 | 0x94 => 1,
                0x5e | 0xd3 => -2,
                _ => 0,
            };
        }
        return 0;
    }
    t.dst.iter().filter(|l| l.reg && l.fp).count() as i32 - t.src.iter().filter(|l| l.reg && l.fp).count() as i32
}

/// a call for FPInitialRegPressure (107108dd, 10710e44..10710e76): kind 0xf unless its target's SYM has +0x20 &
/// 0x10000, kind 0x16 opcode 0x2a0, opcode 0x298, or type class 0xb000
fn callish(t: &Tup) -> bool {
    let call = t.kind == 0xf && !t.src.first().map(|l| l.kind == 4 && l.isreg).unwrap_or(false);
    call || (t.kind == 0x16 && t.op == 0x2a0) || t.op == 0x298 || (t.ty & 0xf000) == 0xb000
}

/// FPInitialRegPressure 10710863 on the view at its entry (il0): each block walked backwards from its live-out x87
/// webs; every web live across a call is split there, and so is every web live where the temporaries reach 8 (or a
/// call comes with temporaries on the stack), at the next point with none.  Returns (split set, split points).
fn init_predict(il: &Il) -> (BTreeSet<u32>, Vec<u32>) {
    let x: BTreeSet<u32> = il.webs.values().map(|w| w.num).collect();
    let mut set = BTreeSet::new();
    let mut sps = vec![];
    let mut temps: i32 = 0;
    let mut b7 = false;
    let mut b16: HashMap<usize, i32> = HashMap::new();
    let n = il.tups.len();
    let sp_at = |i: usize| if i + 1 < n && il.tups[i + 1].op == 0x26b { il.tups[i + 1].t } else { il.tups[i].t };
    for b in 0..il.blks.len() {
        let start = il.blk_start(b);
        let end = (start + 1..n).find(|&j| il.tups[j].kind == 26).unwrap_or(n);
        let mut live: BTreeSet<u32> = x.intersection(&il.blks[b].out).copied().collect();
        for i in (start + 1..end).rev() {
            if b7 && temps == 0 {
                b7 = false;
                if !live.is_empty() {
                    sps.push(sp_at(i));
                    set.extend(live.iter().copied());
                }
                let mut j = i + 1;
                while j < n && il.tups[j].kind != 26 {
                    if il.ins(j) {
                        let jb = b16.get(&j).copied().unwrap_or(0);
                        for pass in 0..2 {
                            let lst = if pass == 0 { if jb != 0 { &il.tups[j].dst } else { continue } } else { &il.tups[j].src };
                            for l in lst {
                                if l.fp && l.reg && l.symk == 2 {
                                    sps.push(sp_at(j));
                                    sps.push(sp_at(j - 1));
                                    set.insert(il.num(l.gr));
                                }
                            }
                        }
                        if jb == 0 {
                            break;
                        }
                    }
                    j += 1;
                }
            }
            if !il.ins(i) {
                continue;
            }
            let t = &il.tups[i];
            if temps != 0 && t.kind == 0xf && !t.src.first().map(|l| l.kind == 4 && l.isreg).unwrap_or(false) {
                b7 = true;
            }
            if callish(t) && !live.is_empty() {
                sps.push(sp_at(i));
                set.extend(live.iter().copied());
            }
            b16.insert(i, temps);
            if temps == 8 {
                b7 = true;
            }
            if let Some(d) = t.dst.first() {
                if d.fp && d.reg {
                    if d.symk == 2 {
                        live.remove(&il.num(d.gr));
                    } else if d.isreg && t.op != 0x63 && t.op != 0x5f {
                        temps += 1; // a register result gets a copy through a new temporary (10710cc8 ff.)
                    }
                }
            }
            for l in &t.src {
                if l.reg && l.fp && l.symk == 2 {
                    live.insert(il.num(l.gr));
                }
            }
            temps -= lf_press(t);
        }
    }
    (set, sps)
}

/// FPUpdateRegisterPressure 10711bad after w is coloured (view: the one at w's check): w adds one to the stack depth
/// (+0x16) of every x87 instruction it is on the stack over; where a depth reaches 8, at the next point without
/// temporaries the uncoloured x87 webs live there, and those the expression around it references, are split.
fn press_predict(il: &Il, w: u32) -> BTreeSet<u32> {
    let mut out = BTreeSet::new();
    let wn = il.num(w);
    let Some(wb) = il.webs.get(&w) else { return out };
    let (Some(&b0), Some(&b1)) = (il.bbix.get(&wb.fb), il.bbix.get(&wb.lb)) else { return out };
    let col: BTreeSet<u32> = il.coloured();
    let x: BTreeSet<u32> = il.webs.values().map(|w| w.num).collect();
    let n = il.tups.len();
    let fpish2 = |t: &Tup| t.tk == TK::Ins && ((t.ty & 0xf000) == 0x4000 || t.op == 0x5b || t.kind == 0xf || t.kind == 0x16);
    for b in b0..=b1.max(b0) {
        if !il.blks[b].u.contains(&wn) {
            continue;
        }
        let mut on = il.blks[b].inn.contains(&wn);
        let mut uncol: BTreeSet<u32> = il.blks[b].inn.iter().copied().filter(|k| x.contains(k) && !col.contains(k) && *k != wn).collect();
        let start = il.blk_start(b);
        let end = (start + 1..n).find(|&j| il.tups[j].kind == 26).unwrap_or(n);
        let mut p8 = false;
        let mut endmark: Option<usize> = None;
        for i in start + 1..end {
            let t = &il.tups[i];
            if !fpish2(t) {
                continue;
            }
            for l in &t.src {
                if l.fp && l.reg && l.dies && l.symk == 2 {
                    if l.gr == w {
                        if t.b17 == 0 {
                            on = false;
                        } else {
                            // w stays counted up to the last x87 instruction before the next point without temporaries
                            let nx = (i + 1..end).find(|&j| il.ins(j) && il.tups[j].b17 == 0 && il.tups[j].op != 0x26a && il.tups[j].op != 0x26b);
                            let last = nx.and_then(|j| (i..j).rev().find(|&k| fpish2(&il.tups[k])));
                            if last == Some(i) || last.is_none() {
                                on = false;
                            } else {
                                endmark = last;
                            }
                        }
                    } else if !l.col {
                        uncol.remove(&il.num(l.gr));
                    }
                }
            }
            for l in &t.dst {
                if l.fp && l.reg && l.symk == 2 {
                    if l.gr == w {
                        on = true;
                    } else if !l.col {
                        uncol.insert(il.num(l.gr));
                    }
                }
            }
            if on {
                if t.b16 + 1 == 8 {
                    p8 = true;
                }
                if endmark == Some(i) {
                    on = false;
                    endmark = None;
                }
            }
            if p8 && t.b17 == 0 && t.op != 0x26a && t.op != 0x26b {
                p8 = false;
                out.extend(uncol.iter().copied());
                // the expression before it: webs it writes (while temporaries are live) or reads
                let mut j = i;
                while j > start + 1 {
                    j -= 1;
                    let y = &il.tups[j];
                    if !il.ins(j) || y.op == 0x26a || y.op == 0x26b {
                        continue;
                    }
                    for pass in 0..2 {
                        let lst = if pass == 0 { if y.b17 != 0 { &y.dst } else { continue } } else { &y.src };
                        for l in lst {
                            if l.fp && l.reg && l.symk == 2 && !l.col && l.gr != w {
                                out.insert(il.num(l.gr));
                            }
                        }
                    }
                    if y.b17 == 0 {
                        break;
                    }
                }
            }
        }
    }
    out
}

/// FPInitialRegPressure's and FPUpdateRegisterPressure's split sets, predicted against the trace (sets as far as the
/// trace logs them: its first two 32-bit SBV chunks)
fn predict_sets(fp: &Fp, il0: Option<usize>) -> (usize, usize, usize, usize, usize, usize, Vec<String>) {
    let mut notes = vec![];
    let cut = |v: &BTreeSet<u32>, logged: &BTreeSet<u32>| -> BTreeSet<u32> {
        // an empty logged set is complete (the function returned 0); a non-empty one holds at most its first two
        // SBV chunks: compare below the highest chunk it shows
        if logged.is_empty() {
            return v.clone();
        }
        let lim = logged.iter().max().map(|m| (m / 32 + 1) * 32).unwrap_or(64).max(64);
        v.iter().copied().filter(|k| *k < lim).collect()
    };
    let (mut ia, mut iok, mut pa, mut pok, mut ine, mut pne) = (0, 0, 0, 0, 0, 0);
    if let Some(k) = il0 {
        if let Some(il) = view(fp, k) {
            let (mset, _) = init_predict(&il);
            if let Some(e) = fp.events.iter().find(|e| s(e, "t") == "init") {
                let t: BTreeSet<u32> = set(e);
                ia += 1;
                ine += !t.is_empty() as usize;
                if cut(&mset, &t) == t {
                    iok += 1;
                } else {
                    notes.push(format!("FPInitialRegPressure set: model {:?} trace {:?}", mset, t));
                }
            }
        }
    }
    let ev = &fp.events;
    for (k, e) in ev.iter().enumerate() {
        if s(e, "t") != "check" || i(e, "ok") == 0 {
            continue;
        }
        let Some(il) = e.get("il").and_then(|x| x.as_u64()).and_then(|x| view(fp, x as usize)) else { continue };
        let Some(pe) = ev[k + 1..].iter().take_while(|x| s(x, "t") != "check").find(|x| s(x, "t") == "press") else { continue };
        let t = set(pe);
        let m = press_predict(&il, hx(&s(e, "gr")));
        pa += 1;
        pne += !t.is_empty() as usize;
        if cut(&m, &t) == t {
            pok += 1;
        } else {
            notes.push(format!("FPUpdateRegisterPressure after {} (web {}): model {:?} trace {:?}", s(e, "gr"), i(e, "num"), m, t));
        }
    }
    (ia, iok, pa, pok, ine, pne, notes)
}

/// the snapshot's il0 (the view at FPInitialRegPressure's entry)
fn fp_il0(path: &str) -> Option<usize> {
    let txt = std::fs::read_to_string(path).ok()?;
    let v: Value = serde_json::from_str(&txt).ok()?;
    v.get("fp")?.get("il0")?.as_u64().map(|x| x as usize)
}

/// `rasim need SNAP stack:V`: what keeps x87 web V on the stack (cl 13.10).  For each web named V (V@k: the k-th
/// web of that name in insertion order): its fate in FPColor; at its FPCheckColorability, the model's blockers (the
/// coloured web and the rule that split V, with its instruction and source line) and, for each coloured blocker,
/// whether V would have been colourable had it popped before that web (the check rerun on the same view with the
/// blocker uncoloured), with the priority gap to close (`need 'V>C'` lists the edits).
pub fn cmd_need_stack(path: &str, sel: &str) {
    let sn = load(path);
    let fp = sn.fp.as_ref().unwrap_or_else(|| super::die("this snapshot has no FPColor data (cl 13.10 x87)"));
    let m = Model::new(fp);
    let ks = m.find(sel).unwrap_or_else(|e| super::die(&e));
    let names: HashMap<String, String> = fp.events.iter().filter(|e| s(e, "t") == "ins").map(|e| (s(e, "gr"), s(e, "name"))).collect();
    let prio: HashMap<String, (i64, i64)> = fp.events.iter().filter(|e| s(e, "t") == "ins").map(|e| (s(e, "gr"), (i(e, "prio"), i(e, "defOrd")))).collect();
    let nm = |g: u32| names.get(&format!("{:x}", g)).cloned().unwrap_or(format!("{:x}", g));
    for k in ks {
        let w = &m.webs[k];
        let gr = w.gr.clone();
        print!("{} (web {}, prio {}, def {}): ", w.name, w.num, w.tprio, w.def);
        let ev = &fp.events;
        let fate: Vec<&Value> = ev.iter().filter(|e| s(e, "gr") == gr && matches!(s(e, "t").as_str(), "drop" | "pop" | "rm" | "check" | "kept" | "nocolor")).collect();
        if fate.iter().any(|e| s(e, "t") == "drop") {
            println!("dropped by FPSpillWorthless (benefit {} <= 0): it never reaches the stack test; raise its priority above 0 (need 'V>X' edits: a use of a double +1·w, a def +2/+4·w)", w.tprio);
            continue;
        }
        if fate.iter().any(|e| s(e, "t") == "rm") && !fate.iter().any(|e| s(e, "t") == "check") {
            let why = ev.iter().position(|e| s(e, "t") == "rm" && s(e, "gr") == gr).map(|p| {
                ev[..p].iter().rev().find(|e| matches!(s(e, "t").as_str(), "press" | "init")).map(|e| s(e, "t")).unwrap_or_default()
            });
            println!(
                "split before its pop ({}): {}",
                why.clone().unwrap_or_default(),
                if why.as_deref() == Some("init") {
                    "FPInitialRegPressure: live across a call (or 8 temporaries); only its pieces compete"
                } else {
                    "FPUpdateRegisterPressure: a colouring made the stack 8 deep where it is live"
                }
            );
            continue;
        }
        let Some(ce) = ev.iter().find(|e| s(e, "t") == "check" && s(e, "gr") == gr) else {
            println!("never popped (still in the list when FPColor ended, or a piece made later)");
            continue;
        };
        let traced = i(ce, "ok") != 0;
        let Some(il) = ce.get("il").and_then(|x| x.as_u64()).and_then(|x| view(fp, x as usize)) else {
            println!("{} (trace); this snapshot has no x87 view, so the blockers are not modelled", if traced { "kept on the stack" } else { "not colourable" });
            continue;
        };
        let w0 = hx(&gr);
        let out = fpcheck(&il, w0);
        println!(
            "{} (trace; model: {})",
            if traced { "kept on the stack" } else { "not colourable: split" },
            if out.ok { "colourable" } else { "split" }
        );
        // a tuple's source line offset (a block start: its first instruction's)
        let line = |t: u32| {
            il.ix(t).map(|x| (x..il.tups.len()).find(|&j| il.ins(j)).map(|j| il.tups[j].line).unwrap_or(0)).unwrap_or(0)
        };
        if out.ok {
            // what it relies on: the coloured webs live with it
            let col: Vec<String> = il.webs.values().filter(|x| x.col).map(|x| format!("{} (web {})", nm(x.gr), x.num)).collect();
            println!("  on the stack with {} coloured web(s) before it: {}", col.len(), col.join(", "));
            println!("  it stays while every one of them that is live at its defs ends after it ends (LIFO), and none is pushed above it and still there at its end");
            continue;
        }
        let mut seen = BTreeSet::new();
        for (cn, at, rule) in &out.blockers {
            if !seen.insert((*cn, *at, *rule)) {
                continue;
            }
            let cg = il.bynum.get(cn).copied().unwrap_or(0);
            let what = match *rule {
                "end" => format!("{} (web {}) ends at src +{} while {} is on the stack above it (InsertColoredLifetimeEndSplit)", nm(cg), cn, line(*at), w.name),
                "start" => format!("{} (web {}) is pushed above {} at src +{} and still there when {} ends (split at its def)", nm(cg), cn, w.name, line(*at), w.name),
                "post" => format!("{} (web {}) is defined at src +{} inside the expression of {}'s last use (SplitAtPostDefEndPoints)", nm(cg), cn, line(*at), w.name),
                _ => format!("its defs leave it at different stack depths at the join at src +{} (SplitAtMismatchedFlowJoinPoints)", line(*at)),
            };
            println!("  blocker: {}", what);
            if cg != 0 && il.webs.get(&cg).map(|x| x.col).unwrap_or(false) {
                // the same check with the blocker not coloured: had V popped before it
                let mut il2 = il.clone();
                if let Some(x) = il2.webs.get_mut(&cg) {
                    x.col = false;
                }
                for t in il2.tups.iter_mut() {
                    for l in t.dst.iter_mut().chain(t.src.iter_mut()) {
                        if l.gr == cg {
                            l.col = false;
                        }
                    }
                }
                let o2 = fpcheck(&il2, w0);
                let (pc, dc) = prio.get(&format!("{:x}", cg)).copied().unwrap_or((0, 0));
                // the priority V must gain to pop first: above C's, or equal with the larger defOrd
                let gap = pc - w.tprio + if (w.def as i64) > dc { 0 } else { 1 };
                println!(
                    "    popped before {} ({} prio {}, def {}): {}; {}",
                    nm(cg),
                    nm(cg),
                    pc,
                    dc,
                    if o2.ok { format!("{} would be colourable", w.name) } else { format!("{} would still be split", w.name) },
                    if gap > 0 {
                        format!("it needs +{} priority (rasim need SNAP '{}>{}' lists the edits)", gap, w.name, nm(cg))
                    } else {
                        "it already outranks it in the list now".to_string()
                    }
                );
            }
        }
        if !out.splits.is_empty() {
            let pts: Vec<String> = out.splits.iter().map(|t| format!("src +{}", line(*t))).collect();
            println!("  split points: {}", pts.join(", "));
        }
    }
}
