//! `rasim scratch`: replay regasg's scratch-register pass (RegSimpleAssignBody 10bda210 / RegAssign 10bda7c0) on a
//! snapshot from `uv run tools/c2trace.py scratch SRC --func F --export snap.json -- <cl args>`.
//!
//! The rule (docs/c2/regalloc.md section 6), for an integer temp, in this order:
//!   1. its preferred register, if free in _RegMap, available, and not in the temp's conflict set: no cursor move;
//!   2. /O2: scan eax, ecx, edx starting at the cursor (_NextIntPos), take the first that is free and not in the
//!      conflict set, and set the cursor to the entry after the one taken (after edx: eax);
//!   3. first fit over eax ecx edx esi edi [ebx [ebp]] with the same two tests: no cursor move;
//!   4. first fit ignoring conflicts / spill: not modelled (the traced register is kept and the pick is flagged).
//! The cursor starts at eax in every run (the body and each EH funclet are separate runs).
//!
//! Inputs taken from the trace per pick, none of which depends on earlier scratch picks: the temp's conflict set
//! (built before the first pick), which registers hold coloured variables ("phys" in _RegMap), which earlier temps
//! are still live, the preference and where it comes from. The simulator keeps its own cursor and its own
//! temp -> register map, so a change to one pick propagates: live temps occupy the register the simulation gave
//! them, and a preference copied from a temp's register follows that temp.
//!
//!   rasim scratch SNAP [--run K] [-v]             predict every pick; report predicted vs traced
//!   rasim scratch SNAP [--run K] EDIT...          what-if: which later picks change register
//! EDIT:
//!   drop:N          pick N does not happen (the temp became a variable, was folded, or the statement was removed)
//!   ins:N[:LIFE]    a new integer temp is picked just before pick N, with no preference and no conflicts; it stays
//!                   live over the next LIFE picks (default 0: dead before pick N)
//!   nopref:N        pick N has no preferred register (so it rotates)
//!   pref:N:REG      pick N prefers REG (as a copy from / lea of that register would)
//! N is a pick number of the chosen run (default: the body, the run with the most picks).

use serde::Deserialize;
use std::collections::{BTreeMap, HashMap};

const SCR: [&str; 3] = ["eax", "ecx", "edx"];

#[derive(Deserialize)]
struct Snap {
    func: String,
    runs: Vec<Run>,
}
#[derive(Deserialize)]
struct Run {
    #[serde(default)]
    body: bool,
    speed: Option<i64>,
    #[serde(default)]
    global_conflicts: bool,
    #[serde(default)]
    spills: u32,
    #[serde(default)]
    fixes: Vec<serde_json::Value>,
    picks: Vec<Pick>,
}
#[derive(Deserialize, Clone)]
struct Pick {
    n: usize,
    tmp: String,
    cls: String,
    cur: String,
    cur_after: Option<String>,
    pref: Option<String>,
    pref_src: Option<String>,
    how: String,
    reg: String,
    /// register -> "phys" (a coloured variable or pre-assigned temp) or the temp that holds it
    busy: BTreeMap<String, String>,
    avail: Vec<String>,
    conf: Option<Vec<String>>,
    off: Option<u64>,
    op: String,
    instr: String,
    #[serde(default)]
    spill: bool,
}

enum Edit {
    Drop(usize),
    Ins(usize, usize),
    NoPref(usize),
    Pref(usize, String),
}

struct Out {
    n: usize, // 0 for an inserted pick
    reg: String,
    how: &'static str,
    cur: usize,
    cur_after: usize,
    why: String,
    modelled: bool,
    collide: Option<String>,
}

fn idx(r: &str) -> usize {
    SCR.iter().position(|x| *x == r).unwrap_or(0)
}

/// One RegAssign call. busy: register -> occupant.
fn assign(p: &Pick, pref: Option<&str>, busy: &HashMap<String, String>, cur: usize, speed: bool) -> (String, &'static str, usize, String, bool) {
    let conf = |r: &str| p.conf.as_ref().map_or(false, |c| c.iter().any(|x| x == r));
    let state = |r: &str| -> Option<String> {
        if let Some(o) = busy.get(r) {
            Some(format!("{} busy ({})", r, if o == "phys" { "coloured variable" } else { o }))
        } else if conf(r) {
            Some(format!("{} conflicts", r))
        } else {
            None
        }
    };
    let mut why = vec![];
    if let Some(r) = pref {
        match state(r) {
            None if p.avail.iter().any(|x| x == r) => return (r.to_string(), "pref", cur, format!("preferred {}", r), true),
            None => why.push(format!("preference {} refused: not available", r)),
            Some(s) => why.push(format!("preference {} refused: {}", r, s)),
        }
    }
    if speed {
        for k in 0..3 {
            let r = SCR[(cur + k) % 3];
            match state(r) {
                None => return (r.to_string(), "ROT", (cur + k + 1) % 3, why.join("; "), true),
                Some(s) => why.push(s),
            }
        }
    }
    // first fit: _IntPhysRegs = eax ecx edx esi edi, then ebx and ebp while available (the list ends at the first gap)
    let mut order = vec!["eax", "ecx", "edx", "esi", "edi"];
    if p.avail.iter().any(|x| x == "ebx") {
        order.push("ebx");
        if p.avail.iter().any(|x| x == "ebp") {
            order.push("ebp");
        }
    }
    for r in order {
        if state(r).is_none() {
            why.push("no scratch register: first fit".into());
            return (r.to_string(), "fit", cur, why.join("; "), true);
        }
    }
    why.push("no free register without a conflict: second first-fit / spill, not modelled".into());
    (p.reg.clone(), "fit", cur, why.join("; "), false)
}

/// Replay a run. With `resync`, a missed prediction is corrected to the traced register and cursor before going on.
fn replay(run: &Run, edits: &[Edit], resync: bool) -> Vec<Out> {
    let speed = run.speed.unwrap_or(1) != 0;
    let mut cur = 0usize;
    let mut place: HashMap<String, String> = HashMap::new(); // temp -> register the simulation gave it (kept after death)
    let mut dropped: Vec<String> = vec![];
    let mut extra: Vec<(String, usize)> = vec![]; // inserted temps: (register, picks left to live)
    let mut out = vec![];
    for p in &run.picks {
        for e in edits {
            if let Edit::Ins(n, life) = e {
                if *n == p.n {
                    let mut busy = sim_busy(p, &place, &dropped, &extra).0;
                    busy.retain(|_, o| o != &p.tmp);
                    let ghost = Pick { conf: Some(vec![]), pref: None, pref_src: None, tmp: "new".into(), ..p.clone() };
                    let (reg, how, c2, why, m) = assign(&ghost, None, &busy, cur, speed);
                    out.push(Out { n: 0, reg: reg.clone(), how, cur, cur_after: c2, why, modelled: m, collide: None });
                    cur = c2;
                    if *life > 0 {
                        extra.push((reg, *life));
                    }
                }
            }
        }
        if edits.iter().any(|e| matches!(e, Edit::Drop(n) if *n == p.n)) {
            dropped.push(p.tmp.clone());
            place.remove(&p.tmp);
            continue;
        }
        let (mut busy, collide) = sim_busy(p, &place, &dropped, &extra);
        busy.retain(|_, o| o != &p.tmp);
        if p.cls != "int" {
            out.push(Out { n: p.n, reg: p.reg.clone(), how: "fit", cur, cur_after: cur, why: "not an integer temp".into(), modelled: false, collide });
            for x in extra.iter_mut() {
                x.1 = x.1.saturating_sub(1);
            }
            extra.retain(|x| x.1 > 0);
            continue;
        }
        let mut pref: Option<String> = p.pref.clone();
        if let Some(t) = &p.pref_src {
            pref = if dropped.contains(t) { None } else { place.get(t).cloned().or(pref) };
        }
        for e in edits {
            match e {
                Edit::NoPref(n) if *n == p.n => pref = None,
                Edit::Pref(n, r) if *n == p.n => pref = Some(r.clone()),
                _ => {}
            }
        }
        let (mut reg, how, mut c2, why, mut m) = assign(p, pref.as_deref(), &busy, cur, speed);
        if p.spill {
            m = false;
        }
        let o_cur = cur;
        if resync && (reg != p.reg || !m) {
            out.push(Out { n: p.n, reg: reg.clone(), how, cur: o_cur, cur_after: c2, why, modelled: m, collide });
            reg = p.reg.clone();
            c2 = p.cur_after.as_deref().map(idx).unwrap_or(c2);
        } else {
            out.push(Out { n: p.n, reg: reg.clone(), how, cur: o_cur, cur_after: c2, why, modelled: m, collide });
        }
        place.insert(p.tmp.clone(), reg);
        cur = c2;
        for x in extra.iter_mut() {
            x.1 = x.1.saturating_sub(1);
        }
        extra.retain(|x| x.1 > 0);
    }
    out
}

/// The registers busy at pick p in the simulation: the traced coloured ones, the temps the trace shows live (in the
/// register the simulation gave them), and inserted temps. Second value: a temp sitting in a register that the
/// trace shows holding a coloured variable (cannot happen unless an edit moved it there against its conflict set).
fn sim_busy(p: &Pick, place: &HashMap<String, String>, dropped: &[String], extra: &[(String, usize)]) -> (HashMap<String, String>, Option<String>) {
    let mut busy: HashMap<String, String> = HashMap::new();
    let mut collide = None;
    for (r, o) in &p.busy {
        if o == "phys" {
            busy.insert(r.clone(), o.clone());
        }
    }
    for (r, o) in &p.busy {
        if o == "phys" || dropped.contains(o) {
            continue;
        }
        let at = place.get(o).cloned().unwrap_or_else(|| r.clone());
        if busy.get(&at).map_or(false, |x| x == "phys") {
            collide = Some(format!("{} would sit in {} while a coloured variable holds it", o, at));
        } else {
            busy.insert(at, o.clone());
        }
    }
    for (r, _) in extra {
        busy.entry(r.clone()).or_insert_with(|| "new".into());
    }
    (busy, collide)
}

fn short(p: &Pick) -> String {
    let s = format!("{} {}", p.op, p.instr);
    if s.len() > 70 { format!("{}...", &s[..67]) } else { s }
}

fn off(p: &Pick) -> String {
    p.off.map_or("gone".into(), |o| format!("+0x{:x}", o))
}

fn parse_edit(s: &str) -> Edit {
    let f: Vec<&str> = s.split(':').collect();
    let n = |k: usize| -> usize { f.get(k).and_then(|x| x.parse().ok()).unwrap_or_else(|| crate::die(&format!("bad edit {:?}", s))) };
    match (f[0], f.len()) {
        ("drop", 2) => Edit::Drop(n(1)),
        ("ins", 2) => Edit::Ins(n(1), 0),
        ("ins", 3) => Edit::Ins(n(1), n(2)),
        ("nopref", 2) => Edit::NoPref(n(1)),
        ("pref", 3) if ["eax", "ecx", "edx", "esi", "edi", "ebx", "ebp"].contains(&f[2]) => Edit::Pref(n(1), f[2].into()),
        _ => crate::die(&format!("bad edit {:?} (drop:N ins:N[:LIFE] nopref:N pref:N:REG)", s)),
    }
}

pub fn main(args: &[String]) {
    let text = std::fs::read_to_string(&args[0]).unwrap_or_else(|e| crate::die(&format!("{}: {}", args[0], e)));
    let s: Snap = serde_json::from_str(&text).unwrap_or_else(|e| crate::die(&format!("{}: not a scratch snapshot (c2trace.py scratch --export): {}", args[0], e)));
    let (mut run, mut verbose, mut edits) = (None, false, vec![]);
    let mut it = args[1..].iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--run" => run = it.next().and_then(|x| x.parse::<usize>().ok()),
            "-v" => verbose = true,
            _ => edits.push(parse_edit(a)),
        }
    }
    if edits.is_empty() {
        check(&s, run, verbose);
    } else {
        let k = run.map(|k| k.saturating_sub(1)).unwrap_or_else(|| s.runs.iter().position(|r| r.body).unwrap_or(0));
        if k >= s.runs.len() {
            crate::die("no such run");
        }
        whatif(&s, k, &edits);
    }
}

fn check(s: &Snap, only: Option<usize>, verbose: bool) {
    println!("{}: scratch picks, predicted from the pick rule vs traced", s.func);
    let (mut hit, mut tot, mut curhit, mut skipped, mut unmod, mut nruns) = (0, 0, 0, 0, 0, 0);
    for (k, r) in s.runs.iter().enumerate() {
        if only.map_or(false, |o| o != k + 1) || r.picks.is_empty() {
            continue;
        }
        nruns += 1;
        let out = replay(r, &[], true);
        let (mut h, mut t) = (0, 0);
        let mut lines = vec![];
        for (o, p) in out.iter().zip(&r.picks) {
            if p.cls != "int" {
                skipped += 1;
                continue;
            }
            t += 1;
            let good = o.reg == p.reg && o.how == p.how && o.modelled;
            let cgood = SCR[o.cur] == p.cur && p.cur_after.as_deref().map_or(true, |c| good && c == SCR[o.cur_after] || !good);
            if good {
                h += 1;
            }
            if !o.modelled {
                unmod += 1;
            }
            if cgood {
                curhit += 1;
            }
            if verbose || !good || !cgood {
                lines.push(format!(
                    "  {:>3} {:>7}  cursor {} -> {}  predicted {:<4} {:<3}  traced {:<4} {:<3} (cursor {} -> {})  {}{}{}",
                    p.n, off(p), SCR[o.cur], SCR[o.cur_after], o.how, o.reg, p.how, p.reg, p.cur, p.cur_after.as_deref().unwrap_or("?"),
                    if good && cgood { "ok" } else if !o.modelled { "NOT MODELLED" } else { "MISS" },
                    if o.why.is_empty() { String::new() } else { format!("  [{}]", o.why) },
                    if verbose { format!("  {}", short(p)) } else { String::new() }
                ));
            }
        }
        println!(
            "run {}{}: {} of {} integer picks predicted{}{}",
            k + 1,
            if r.body { " (body)" } else { " (EH funclet)" },
            h, t,
            if r.global_conflicts { "" } else { "  (local conflict mode: no conflict sets in the snapshot, _RegMap only)" },
            if r.spills > 0 || !r.fixes.is_empty() { format!("  ({} RegSpill, {} RegFixConflict in the trace: not modelled)", r.spills, r.fixes.len()) } else { String::new() }
        );
        for l in lines {
            println!("{}", l);
        }
        hit += h;
        tot += t;
    }
    println!(
        "scratch: {} of {} integer picks predicted (register and how); cursor right at {} of {}; {} not modelled; {} non-integer picks skipped; {} runs",
        hit, tot, curhit, tot, unmod, skipped, nruns
    );
}

fn whatif(s: &Snap, k: usize, edits: &[Edit]) {
    let r = &s.runs[k];
    for e in edits {
        let n = match e {
            Edit::Drop(n) | Edit::Ins(n, _) | Edit::NoPref(n) | Edit::Pref(n, _) => *n,
        };
        if !r.picks.iter().any(|p| p.n == n) {
            crate::die(&format!("run {} has no pick {}", k + 1, n));
        }
    }
    let base = replay(r, &[], false);
    let bad = base.iter().zip(&r.picks).filter(|(o, p)| p.cls == "int" && (o.reg != p.reg || !o.modelled)).count();
    println!("{} run {}{}: what-if on {} picks", s.func, k + 1, if r.body { " (body)" } else { "" }, r.picks.len());
    if bad > 0 {
        println!("warning: the model misses {} picks of this run without any edit (rasim scratch SNAP): the answer below is unreliable", bad);
    }
    if !r.global_conflicts {
        println!("warning: this run has no conflict sets in the snapshot: a moved pick may land on a register it conflicts with");
    }
    let out = replay(r, edits, false);
    let by: HashMap<usize, &Out> = out.iter().filter(|o| o.n > 0).map(|o| (o.n, o)).collect();
    for o in out.iter().filter(|o| o.n == 0) {
        println!("  new  {:>7}  cursor {} -> {}  {:<4} {}  (inserted temp){}", "", SCR[o.cur], SCR[o.cur_after], o.how, o.reg,
                 if o.why.is_empty() { String::new() } else { format!("  [{}]", o.why) });
    }
    let (mut changed, mut last) = (0, 0);
    for p in &r.picks {
        match by.get(&p.n) {
            None => println!("  {:>3}  {:>7}  removed (was {} {})  {}", p.n, off(p), p.how, p.reg, short(p)),
            Some(o) => {
                if o.reg != p.reg || o.collide.is_some() {
                    changed += 1;
                    last = p.n;
                    println!(
                        "  {:>3}  {:>7}  {} -> {}  ({} -> {}; cursor {} -> {}){}{}  {}",
                        p.n, off(p), p.reg, o.reg, p.how, o.how, p.cur, SCR[o.cur],
                        if o.why.is_empty() { String::new() } else { format!("  [{}]", o.why) },
                        o.collide.as_ref().map_or(String::new(), |c| format!("  COLLISION: {}", c)),
                        short(p)
                    );
                }
            }
        }
    }
    let ints = r.picks.iter().filter(|p| p.cls == "int").count();
    let end = out.last().map_or(0, |o| o.cur_after);
    let tend = r.picks.iter().rev().find_map(|p| p.cur_after.as_deref()).map(idx).unwrap_or(0);
    println!(
        "what-if: {} of {} integer picks change register{}; final cursor {}{}",
        changed, ints,
        if changed > 0 { format!(" (last: pick {})", last) } else { String::new() },
        SCR[end],
        if end == tend { " (as traced)".to_string() } else { format!(" (traced {})", SCR[tend]) }
    );
    println!("offsets are listing offsets of the traced compile; an edit that changes code size moves them");
}
