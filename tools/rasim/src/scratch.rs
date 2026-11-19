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
//!   rdrop:N         (cl 13.10) Riscify's GetReg call N does not happen (riscify.rs)
//!   rins:N          (cl 13.10) one more GetReg call just before call N, in its block, with its busy set
//!   want:TEMP=REG   how TEMP (a pick, a Riscify temp or another temp of the final code) got its register, and the
//!                   single edits above that give it REG (also --want REGMAP.txt: every WANTS row of regmap output)
//! N is a pick number of the chosen run (default: the body, the run with the most picks).
//!
//!   rasim scratch SNAP --target T.json [--top K] [EDIT...]
//!                   re-alignment scan (written by `c2trace.py scratch --phase ORIG.s`): T gives the original's
//!                   register for picks read off its disassembly and a group (switch case or block) per pick; every
//!                   single drop:N / ins:N (on top of the EDITs) is replayed and ranked by how many groups end up
//!                   with every known pick on the original's register (gained minus lost)

use serde::Deserialize;
use std::collections::{BTreeMap, HashMap};

const SCR: [&str; 3] = ["eax", "ecx", "edx"];

#[derive(Deserialize)]
struct Snap {
    func: String,
    runs: Vec<Run>,
    /// cl 13.10: Riscify's GetReg calls (riscify.rs)
    #[serde(default)]
    riscify: Vec<crate::riscify::RCall>,
    /// temps of the final code that no modelled phase gave their register, with the phase that did
    #[serde(default)]
    unmodelled: Vec<crate::riscify::Unmod>,
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

#[derive(Clone, PartialEq)]
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
        _ => crate::die(&format!("bad edit {:?} (drop:N ins:N[:LIFE] nopref:N pref:N:REG rdrop:N rins:N want:TEMP=REG)", s)),
    }
}

/// The WANTS rows of `c2trace.py regmap` output (or VAR=REG lines) as (temp, register).
fn want_file(path: &str) -> Vec<(String, String)> {
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| crate::die(&format!("{}: {}", path, e)));
    let mut out = vec![];
    for ln in text.lines() {
        let t: Vec<&str> = ln.split_whitespace().collect();
        if t.is_empty() || ln.starts_with('#') || ln.starts_with(' ') || ln.starts_with("var (IL)") || ln.starts_with("swap:") {
            continue;
        }
        if t.len() == 1 {
            if let Some((v, r)) = t[0].split_once('=') {
                out.push((v.to_string(), r.to_string()));
            }
            continue;
        }
        if let Some(k) = t.iter().position(|x| *x == "WANTS") {
            if let Some(r) = t.get(k + 1) {
                out.push((t[0].to_string(), r.to_string()));
            }
        }
    }
    out
}

/// want:T=R for a scratch pick: how it got its register; every single drop:K / ins:K (K up to the pick), nopref and
/// pref on the pick itself, that gives it R.
fn want_pick(s: &Snap, k: usize, t: &str, r: &str) -> bool {
    let run = &s.runs[k];
    let named = |p: &Pick| p.tmp == t || p.instr.split(|c: char| c == ':' || c.is_whitespace()).next() == Some(t);
    let Some(p) = run.picks.iter().find(|p| named(p)) else { return false };
    println!("{}: scratch pick {} (RegAssign, {} {}; cursor {}){}", t, p.n, p.how, p.reg, p.cur,
             if p.tmp != t { format!(", temp {} in the final code", p.tmp) } else { String::new() });
    if p.reg == r {
        println!("  it already has {}", r);
        return true;
    }
    if !SCR.contains(&r) {
        println!("  {} is not one of the rotation's registers (eax ecx edx): a pick gets it only by preference (a copy from / lea of {}) or by first fit when eax, ecx and edx are all busy or in its conflict set", r, r);
    }
    if p.busy.get(r).map_or(false, |o| o == "phys") {
        println!("  {} holds a coloured value at this pick: no scratch edit frees it (that is Color's choice: `rasim need reg:`)", r);
    }
    let mut cands: Vec<(String, Edit)> = vec![];
    for q in run.picks.iter().filter(|q| q.n < p.n) {
        cands.push((format!("drop:{}", q.n), Edit::Drop(q.n)));
    }
    for q in run.picks.iter().filter(|q| q.n <= p.n) {
        cands.push((format!("ins:{}", q.n), Edit::Ins(q.n, 0)));
    }
    cands.push((format!("nopref:{}", p.n), Edit::NoPref(p.n)));
    cands.push((format!("pref:{}:{}", p.n, r), Edit::Pref(p.n, r.to_string())));
    let base = replay(run, &[], false);
    let mut hits = vec![];
    for (lbl, e) in cands {
        let out = replay(run, std::slice::from_ref(&e), false);
        if out.iter().find(|o| o.n == p.n).map_or(false, |o| o.reg == r && o.modelled) {
            let moved = out.iter().filter(|o| o.n != 0 && o.n != p.n)
                .filter(|o| base.iter().find(|b| b.n == o.n).map_or(false, |b| b.reg != o.reg)).count();
            hits.push(format!("  {:<12} pick {} gets {} ({} other picks change)", lbl, p.n, r, moved));
        }
    }
    if hits.is_empty() {
        println!("  no single scratch edit gives it {}", r);
    } else {
        for h in hits {
            println!("{}", h);
        }
    }
    true
}

fn want(s: &Snap, k: usize, targets: &[(String, String)]) {
    if targets.len() > 1 {
        joint(s, k, targets);
        println!();
        println!("per want:");
    }
    for (t, r) in targets {
        if want_pick(s, k, t, r) || crate::riscify::want(&s.riscify, t, r) {
            continue;
        }
        if let Some(u) = s.unmodelled.iter().find(|u| u.tmp == *t) {
            println!("{}: {} at {} -- assigned by {}; rasim does not model that phase, so it has no edit to offer ({})",
                     t, u.reg, u.off.map_or("?".into(), |o| format!("+0x{:x}", o)), u.phase, u.instr);
            continue;
        }
        println!("{}: not a scratch pick, Riscify temp or other temp of {} in this snapshot (a Color value: `rasim want` / `need reg:` on a `regs --export` snapshot)", t, s.func);
    }
}

/// A wanted register: a scratch pick of run k (its number) or a temp a Riscify GetReg call made (the call's number).
#[derive(Clone, Copy, PartialEq)]
enum Tg {
    Pick(usize),
    Call(usize),
}

/// One edit of the joint search: a scratch edit or a GetReg edit.
#[derive(Clone, PartialEq)]
enum JE {
    S(Edit),
    R(usize, bool), // (call, true = rdrop / false = rins)
}

fn je_label(e: &JE) -> String {
    match e {
        JE::S(Edit::Drop(n)) => format!("drop:{}", n),
        JE::S(Edit::Ins(n, 0)) => format!("ins:{}", n),
        JE::S(Edit::Ins(n, l)) => format!("ins:{}:{}", n, l),
        JE::S(Edit::NoPref(n)) => format!("nopref:{}", n),
        JE::S(Edit::Pref(n, r)) => format!("pref:{}:{}", n, r),
        JE::R(n, true) => format!("rdrop:{}", n),
        JE::R(n, false) => format!("rins:{}", n),
    }
}

/// What a set of edits gives: (wanted registers that hold, per target; other registers that change).
fn joint_eval(s: &Snap, k: usize, tg: &[(Tg, String)], set: &[&JE], base: &(Vec<Out>, Vec<crate::riscify::ROut>)) -> (Vec<bool>, usize) {
    let run = &s.runs[k];
    let se: Vec<Edit> = set.iter().filter_map(|e| if let JE::S(x) = e { Some(x.clone()) } else { None }).collect();
    let re: Vec<crate::riscify::REdit> = set.iter().filter_map(|e| match e {
        JE::R(n, true) => Some(crate::riscify::REdit::Drop(*n)),
        JE::R(n, false) => Some(crate::riscify::REdit::Ins(*n)),
        _ => None,
    }).collect();
    let so = if se.is_empty() { None } else { Some(replay(run, &se, false)) };
    let ro = if re.is_empty() { None } else { Some(crate::riscify::replay(&s.riscify, &re, false)) };
    let picks = so.as_ref().unwrap_or(&base.0);
    let calls = ro.as_ref().unwrap_or(&base.1);
    let ok: Vec<bool> = tg.iter().map(|(t, r)| match t {
        Tg::Pick(n) => picks.iter().find(|o| o.n == *n).map_or(false, |o| o.reg == *r && o.modelled),
        Tg::Call(n) => calls.iter().find(|o| o.n == *n).and_then(|o| o.reg.as_deref()) == Some(r.as_str()),
    }).collect();
    let mut side = 0;
    if so.is_some() {
        for o in picks.iter().filter(|o| o.n > 0 && !tg.iter().any(|(t, _)| *t == Tg::Pick(o.n))) {
            if base.0.iter().find(|b| b.n == o.n).map_or(false, |b| b.reg != o.reg) {
                side += 1;
            }
        }
    }
    if ro.is_some() {
        for c in s.riscify.iter().filter(|c| !c.made.is_empty() && !tg.iter().any(|(t, _)| *t == Tg::Call(c.n))) {
            let now = calls.iter().find(|o| o.n == c.n).map(|o| o.reg.clone());
            let was = base.1.iter().find(|o| o.n == c.n).map(|o| o.reg.clone());
            if now != was {
                side += 1;
            }
        }
    }
    (ok, side)
}

/// want: with several targets: the smallest edit sets that give all of them their register at once (or the most
/// of them), searched over single edits, pairs, then triples grown from the best pairs. Printed before the
/// per-want answers.
fn joint(s: &Snap, k: usize, targets: &[(String, String)]) {
    let run = &s.runs[k];
    let named = |p: &Pick, t: &str| p.tmp == t || p.instr.split(|c: char| c == ':' || c.is_whitespace()).next() == Some(t);
    let mut tg: Vec<(Tg, String)> = vec![];
    let mut skipped = vec![];
    for (t, r) in targets {
        if let Some(p) = run.picks.iter().find(|p| named(p, t)) {
            tg.push((Tg::Pick(p.n), r.clone()));
        } else if let Some(c) = s.riscify.iter().find(|c| c.made.iter().any(|m| m == t)) {
            tg.push((Tg::Call(c.n), r.clone()));
        } else {
            skipped.push(t.clone());
        }
    }
    let name = |i: usize| -> String { format!("{}={}", targets.iter().filter(|(t, _)| !skipped.contains(t)).nth(i).map_or("?", |x| x.0.as_str()), tg[i].1) };
    println!("joint: {} wants{}", tg.len(),
             if skipped.is_empty() { String::new() } else { format!(" ({} not a pick or Riscify temp, left out: {})", skipped.len(), skipped.join(" ")) });
    if tg.is_empty() {
        return;
    }
    let base = (replay(run, &[], false), crate::riscify::replay(&s.riscify, &[], false));
    let (ok0, _) = joint_eval(s, k, &tg, &[], &base);
    let n0 = ok0.iter().filter(|x| **x).count();
    println!("  now: {} of {} hold{}", n0, tg.len(),
             if n0 == tg.len() { String::new() } else { format!("; missing: {}", (0..tg.len()).filter(|&i| !ok0[i]).map(name).collect::<Vec<_>>().join(" ")) });
    if n0 == tg.len() {
        return;
    }
    // the pool: scratch edits up to the last wanted pick, GetReg edits in the wanted calls' blocks up to the call
    let mut pool: Vec<JE> = vec![];
    if let Some(last) = tg.iter().filter_map(|(t, _)| if let Tg::Pick(n) = t { Some(*n) } else { None }).max() {
        for q in run.picks.iter().filter(|q| q.n <= last) {
            if q.n < last {
                pool.push(JE::S(Edit::Drop(q.n)));
            }
            pool.push(JE::S(Edit::Ins(q.n, 0)));
        }
        for (t, r) in &tg {
            if let Tg::Pick(n) = t {
                pool.push(JE::S(Edit::NoPref(*n)));
                pool.push(JE::S(Edit::Pref(*n, r.clone())));
            }
        }
    }
    for (t, _) in &tg {
        if let Tg::Call(n) = t {
            let blk = s.riscify.iter().find(|c| c.n == *n).map(|c| c.block);
            for c in s.riscify.iter().filter(|c| Some(c.block) == blk && c.n <= *n) {
                for d in [true, false] {
                    if d && c.n == *n {
                        continue;
                    }
                    let e = JE::R(c.n, d);
                    if !pool.contains(&e) {
                        pool.push(e);
                    }
                }
            }
        }
    }
    let score = |ok: &[bool]| ok.iter().filter(|x| **x).count();
    // (holds, -edits, -side) larger is better; repeated ins:N are allowed (two more temps), other repeats are not
    let mut found: Vec<(usize, usize, Vec<usize>, Vec<bool>)> = vec![]; // (held, side, pool indices, ok)
    let mut singles: Vec<(usize, usize, usize)> = vec![];
    for i in 0..pool.len() {
        let (ok, side) = joint_eval(s, k, &tg, &[&pool[i]], &base);
        singles.push((score(&ok), side, i));
        found.push((score(&ok), side, vec![i], ok));
    }
    let full = |f: &Vec<(usize, usize, Vec<usize>, Vec<bool>)>| f.iter().any(|x| x.0 == tg.len());
    let rep_ok = |a: &JE, b: &JE| a != b || matches!(a, JE::S(Edit::Ins(..)));
    if !full(&found) {
        let lim = pool.len().min(400);
        singles.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        let top: Vec<usize> = singles.iter().take(lim).map(|x| x.2).collect();
        for (x, &i) in top.iter().enumerate() {
            for &j in &top[x..] {
                if !rep_ok(&pool[i], &pool[j]) {
                    continue;
                }
                let (ok, side) = joint_eval(s, k, &tg, &[&pool[i], &pool[j]], &base);
                if score(&ok) > n0 {
                    found.push((score(&ok), side, vec![i, j], ok));
                }
            }
        }
    }
    if !full(&found) {
        let mut pairs: Vec<&(usize, usize, Vec<usize>, Vec<bool>)> = found.iter().filter(|x| x.2.len() == 2).collect();
        pairs.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        let seeds: Vec<Vec<usize>> = pairs.iter().take(40).map(|x| x.2.clone()).collect();
        let mut seen: std::collections::HashSet<Vec<usize>> = std::collections::HashSet::new();
        let mut more = vec![];
        for sd in seeds {
            for j in 0..pool.len() {
                if sd.iter().any(|&i| !rep_ok(&pool[i], &pool[j])) {
                    continue;
                }
                let mut v = sd.clone();
                v.push(j);
                v.sort();
                if !seen.insert(v.clone()) {
                    continue;
                }
                let set: Vec<&JE> = v.iter().map(|&i| &pool[i]).collect();
                let (ok, side) = joint_eval(s, k, &tg, &set, &base);
                if score(&ok) > n0 {
                    more.push((score(&ok), side, v, ok));
                }
            }
        }
        found.extend(more);
    }
    found.sort_by(|a, b| b.0.cmp(&a.0).then(a.2.len().cmp(&b.2.len())).then(a.1.cmp(&b.1)));
    let best = found.first().map_or(0, |x| x.0);
    if best <= n0 {
        println!("  no set of up to three edits ({} candidates: drop/ins/nopref/pref up to the last wanted pick, rdrop/rins in the wanted calls' blocks) gives more than {}", pool.len(), n0);
        return;
    }
    let nmin = found[0].2.len();
    println!("  best: {} of {} with {} edit{} ({} candidates searched: singles, pairs{}){}", best, tg.len(), nmin, if nmin == 1 { "" } else { "s" },
             pool.len(), if found.iter().any(|x| x.2.len() == 3) { ", triples from the best 40 pairs" } else { "" },
             if best == tg.len() { "" } else { "; no set gives all" });
    for f in found.iter().filter(|x| x.0 == best && x.2.len() == nmin).take(8) {
        let lbl: Vec<String> = f.2.iter().map(|&i| je_label(&pool[i])).collect();
        let miss: Vec<String> = (0..tg.len()).filter(|&i| !f.3[i]).map(name).collect();
        let lost: Vec<String> = (0..tg.len()).filter(|&i| ok0[i] && !f.3[i]).map(name).collect();
        println!("  {:<24} {} of {}; {} other register{} change{}{}", lbl.join(" "), f.0, tg.len(), f.1, if f.1 == 1 { "" } else { "s" },
                 if miss.is_empty() { String::new() } else { format!("; still missing: {}", miss.join(" ")) },
                 if lost.is_empty() { String::new() } else { format!(" (lost: {})", lost.join(" ")) });
    }
    let nmore = found.iter().filter(|x| x.0 == best && x.2.len() == nmin).count();
    if nmore > 8 {
        println!("  ... {} more of that size", nmore - 8);
    }
    println!("  (the edits are the per-want ones below, applied together; `rasim scratch SNAP EDIT...` replays a set)");
}

pub fn main(args: &[String]) {
    let text = std::fs::read_to_string(&args[0]).unwrap_or_else(|e| crate::die(&format!("{}: {}", args[0], e)));
    let s: Snap = serde_json::from_str(&text).unwrap_or_else(|e| crate::die(&format!("{}: not a scratch snapshot (c2trace.py scratch --export): {}", args[0], e)));
    let (mut run, mut verbose, mut edits) = (None, false, vec![]);
    let (mut target, mut top) = (None, 25usize);
    let (mut redits, mut wants) = (vec![], vec![]);
    let mut it = args[1..].iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--run" => run = it.next().and_then(|x| x.parse::<usize>().ok()),
            "-v" => verbose = true,
            "--target" => target = it.next().cloned(),
            "--top" => top = it.next().and_then(|x| x.parse::<usize>().ok()).unwrap_or(25),
            "--want" => wants.extend(want_file(it.next().unwrap_or_else(|| crate::die("--want REGMAP.txt")))),
            x if x.starts_with("want:") => match x[5..].split_once('=') {
                Some((t, r)) => wants.push((t.to_string(), r.to_string())),
                None => crate::die(&format!("bad {:?} (want:TEMP=REG)", x)),
            },
            x if x.starts_with("rdrop:") || x.starts_with("rins:") => {
                let n = x.split(':').nth(1).and_then(|v| v.parse().ok()).unwrap_or_else(|| crate::die(&format!("bad edit {:?}", x)));
                redits.push(if x.starts_with("rdrop:") { crate::riscify::REdit::Drop(n) } else { crate::riscify::REdit::Ins(n) });
            }
            _ => edits.push(parse_edit(a)),
        }
    }
    if !wants.is_empty() {
        let k = run.map(|k| k.saturating_sub(1)).unwrap_or_else(|| s.runs.iter().position(|r| r.body).unwrap_or(0));
        if k >= s.runs.len() {
            crate::die("no such run");
        }
        return want(&s, k, &wants);
    }
    if !redits.is_empty() {
        if s.riscify.is_empty() {
            crate::die("rdrop/rins: this snapshot has no Riscify calls (cl 13.10 only; re-export with `c2trace.py scratch --export`)");
        }
        crate::riscify::whatif(&s.riscify, &redits);
        if edits.is_empty() {
            return;
        }
    }
    if let Some(t) = target {
        let k = run.map(|k| k.saturating_sub(1)).unwrap_or_else(|| s.runs.iter().position(|r| r.body).unwrap_or(0));
        if k >= s.runs.len() {
            crate::die("no such run");
        }
        return scan(&s, k, &t, &edits, top);
    }
    if edits.is_empty() {
        check(&s, run, verbose);
        if !s.riscify.is_empty() {
            crate::riscify::check(&s.riscify, verbose);
        }
        if !s.unmodelled.is_empty() {
            println!("not modelled: {} temps of the final code got their register outside RegAssign, Color's pops and Riscify (want:TEMP=REG names the phase)",
                     s.unmodelled.len());
        }
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


// ---------------------------------------------------------------- re-alignment scan (--target)

const ALL: [&str; 7] = ["eax", "ecx", "edx", "esi", "edi", "ebx", "ebp"];

fn ri(r: &str) -> u8 {
    ALL.iter().position(|x| *x == r).map_or(7, |k| k as u8)
}

/// A pick with its inputs as small integers, for replaying thousands of edits.
struct FP {
    int: bool,
    tmp: u32,
    phys: u8,
    live: Vec<(u8, u32)>,
    conf: u8,
    avail: u8,
    pref: Option<u8>,
    pref_src: Option<u32>,
    spill: bool,
    reg: u8,
}

#[derive(Clone, Copy, PartialEq)]
enum Occ {
    Phys,
    Tmp(u32),
    New,
}

#[derive(Clone, Default)]
struct FEdits {
    drop: Vec<bool>,
    ins: Vec<Vec<usize>>,
    nopref: Vec<bool>,
    pref: Vec<Option<u8>>,
}

fn fast_picks(run: &Run) -> (Vec<FP>, usize) {
    let mut ids: HashMap<String, u32> = HashMap::new();
    let mut id = |t: &str| -> u32 {
        let n = ids.len() as u32;
        *ids.entry(t.to_string()).or_insert(n)
    };
    let mask = |v: &[String]| v.iter().fold(0u8, |m, r| { let k = ri(r); if k < 7 { m | 1 << k } else { m } });
    let mut out = vec![];
    for p in &run.picks {
        let mut phys = 0u8;
        let mut live = vec![];
        for (r, o) in &p.busy {
            let k = ri(r);
            if o == "phys" {
                if k < 7 {
                    phys |= 1 << k;
                }
            } else if k < 7 {
                live.push((k, id(o)));
            }
        }
        out.push(FP {
            int: p.cls == "int",
            tmp: id(&p.tmp),
            phys,
            live,
            conf: p.conf.as_ref().map_or(0, |c| mask(c)),
            avail: mask(&p.avail),
            pref: p.pref.as_deref().map(ri).filter(|k| *k < 7),
            pref_src: p.pref_src.as_deref().map(|t| id(t)),
            spill: p.spill,
            reg: ri(&p.reg),
        });
    }
    let n = ids.len();
    (out, n)
}

/// One RegAssign, as `assign`: -> (register, cursor after, modelled).
fn fassign(p: &FP, pref: Option<u8>, occ: &[Option<Occ>; 8], conf: u8, cur: u8, speed: bool) -> (u8, u8, bool) {
    let free = |r: u8| occ[r as usize].is_none() && conf & (1 << r) == 0;
    if let Some(r) = pref {
        if free(r) && p.avail & (1 << r) != 0 {
            return (r, cur, true);
        }
    }
    if speed {
        for k in 0..3u8 {
            let r = (cur + k) % 3;
            if free(r) {
                return (r, (cur + k + 1) % 3, true);
            }
        }
    }
    let mut n = 5u8;
    if p.avail & (1 << 5) != 0 {
        n = 6;
        if p.avail & (1 << 6) != 0 {
            n = 7;
        }
    }
    for r in 0..n {
        if free(r) {
            return (r, cur, true);
        }
    }
    (p.reg, cur, false)
}

/// `replay` without resync, on FPs: the register each pick gets (255 for a dropped pick).
fn freplay(ps: &[FP], ntmp: usize, e: &FEdits, speed: bool) -> Vec<u8> {
    let mut cur = 0u8;
    let mut place = vec![255u8; ntmp];
    let mut dropped = vec![false; ntmp];
    let mut extra: Vec<(u8, usize)> = vec![];
    let mut out = Vec::with_capacity(ps.len());
    let busy = |p: &FP, place: &[u8], dropped: &[bool], extra: &[(u8, usize)]| -> [Option<Occ>; 8] {
        let mut occ = [None; 8];
        for r in 0..7 {
            if p.phys & (1 << r) != 0 {
                occ[r] = Some(Occ::Phys);
            }
        }
        for &(r, o) in &p.live {
            if dropped[o as usize] {
                continue;
            }
            let at = if place[o as usize] < 7 { place[o as usize] } else { r } as usize;
            if occ[at] != Some(Occ::Phys) {
                occ[at] = Some(Occ::Tmp(o));
            }
        }
        for &(r, _) in extra {
            if occ[r as usize].is_none() {
                occ[r as usize] = Some(Occ::New);
            }
        }
        for x in occ.iter_mut() {
            if *x == Some(Occ::Tmp(p.tmp)) {
                *x = None;
            }
        }
        occ
    };
    let age = |extra: &mut Vec<(u8, usize)>| {
        for x in extra.iter_mut() {
            x.1 = x.1.saturating_sub(1);
        }
        extra.retain(|x| x.1 > 0);
    };
    for (i, p) in ps.iter().enumerate() {
        for &life in &e.ins[i] {
            let occ = busy(p, &place, &dropped, &extra);
            let g = FP { int: true, tmp: u32::MAX, phys: 0, live: vec![], conf: 0, avail: p.avail, pref: None, pref_src: None, spill: false, reg: p.reg };
            let (r, c2, _) = fassign(&g, None, &occ, 0, cur, speed);
            cur = c2;
            if life > 0 {
                extra.push((r, life));
            }
        }
        if e.drop[i] {
            dropped[p.tmp as usize] = true;
            place[p.tmp as usize] = 255;
            out.push(255);
            continue;
        }
        if !p.int {
            out.push(p.reg);
            age(&mut extra);
            continue;
        }
        let occ = busy(p, &place, &dropped, &extra);
        let mut pref = p.pref;
        if let Some(t) = p.pref_src {
            pref = if dropped[t as usize] { None } else if place[t as usize] < 7 { Some(place[t as usize]) } else { pref };
        }
        if e.nopref[i] {
            pref = None;
        }
        if let Some(r) = e.pref[i] {
            pref = Some(r);
        }
        let (r, c2, _) = fassign(p, pref, &occ, p.conf, cur, speed);
        let _ = p.spill;
        out.push(r);
        place[p.tmp as usize] = r;
        cur = c2;
        age(&mut extra);
    }
    out
}

#[derive(Deserialize)]
struct Target {
    /// [pick n, register the original has there]
    want: Vec<(usize, String)>,
    /// [pick n, group index]
    group: Vec<(usize, usize)>,
    groups: Vec<String>,
    /// pick numbers to try edits at (default: every integer pick)
    #[serde(default)]
    cands: Option<Vec<usize>>,
}

fn scan(s: &Snap, k: usize, path: &str, given: &[Edit], top: usize) {
    let text = std::fs::read_to_string(path).unwrap_or_else(|e| crate::die(&format!("{}: {}", path, e)));
    let t: Target = serde_json::from_str(&text).unwrap_or_else(|e| crate::die(&format!("{}: {}", path, e)));
    let run = &s.runs[k];
    let speed = run.speed.unwrap_or(1) != 0;
    let (ps, ntmp) = fast_picks(run);
    let np = ps.len();
    let pos: HashMap<usize, usize> = run.picks.iter().enumerate().map(|(i, p)| (p.n, i)).collect();
    let at = |n: usize| -> usize { *pos.get(&n).unwrap_or_else(|| crate::die(&format!("run {} has no pick {}", k + 1, n))) };
    let mut base_e = FEdits { drop: vec![false; np], ins: vec![vec![]; np], nopref: vec![false; np], pref: vec![None; np] };
    for e in given {
        match e {
            Edit::Drop(n) => base_e.drop[at(*n)] = true,
            Edit::Ins(n, l) => base_e.ins[at(*n)].push(*l),
            Edit::NoPref(n) => base_e.nopref[at(*n)] = true,
            Edit::Pref(n, r) => base_e.pref[at(*n)] = Some(ri(r)),
        }
    }
    // the fast replay must agree with the reference one (replay) before any answer is trusted
    let slow = replay(run, given, false);
    let fast0 = freplay(&ps, ntmp, &base_e, speed);
    let slow_regs: Vec<u8> = {
        let by: HashMap<usize, &Out> = slow.iter().filter(|o| o.n > 0).map(|o| (o.n, o)).collect();
        run.picks.iter().map(|p| by.get(&p.n).map_or(255, |o| ri(&o.reg))).collect()
    };
    let disagree = fast0.iter().zip(&slow_regs).filter(|(a, b)| a != b).count();
    let want: Vec<(usize, u8)> = t.want.iter().filter_map(|(n, r)| pos.get(n).map(|i| (*i, ri(r)))).collect();
    let mut grp = vec![usize::MAX; np];
    for (n, g) in &t.group {
        if let Some(i) = pos.get(n) {
            grp[*i] = *g;
        }
    }
    let ng = t.groups.len();
    let mut nwant = vec![0usize; ng];
    for (i, _) in &want {
        if grp[*i] < ng {
            nwant[grp[*i]] += 1;
        }
    }
    let exact = |regs: &[u8]| -> (Vec<bool>, usize) {
        let mut bad = vec![false; ng];
        let mut hit = 0;
        for (i, r) in &want {
            let ok = regs[*i] == 255 || regs[*i] == *r;
            if ok {
                hit += 1;
            } else if grp[*i] < ng {
                bad[grp[*i]] = true;
            }
        }
        ((0..ng).map(|g| nwant[g] > 0 && !bad[g]).collect(), hit)
    };
    let (ex0, hit0) = exact(&fast0);
    let nex0 = ex0.iter().filter(|x| **x).count();
    let ngw = nwant.iter().filter(|x| **x > 0).count();
    println!("{} run {}: re-alignment scan, {} picks, {} with the original's register known, {} groups with known picks",
             s.func, k + 1, np, want.len(), ngw);
    if disagree > 0 {
        println!("warning: the fast replay differs from the reference replay at {} picks: answers are unreliable", disagree);
    }
    let bad = slow.iter().zip(&run.picks).filter(|(o, p)| p.cls == "int" && (o.reg != p.reg || !o.modelled)).count();
    if bad > 0 && given.is_empty() {
        println!("warning: the model misses {} traced picks without any edit (rasim scratch SNAP)", bad);
    }
    println!("baseline: {} of {} known picks on the original's register; {} of {} groups exact", hit0, want.len(), nex0, ngw);
    let cands: Vec<usize> = match &t.cands {
        Some(c) => c.iter().filter_map(|n| pos.get(n).copied()).collect(),
        None => (0..np).filter(|i| ps[*i].int).collect(),
    };
    let mut res = vec![];
    for &i in &cands {
        for kind in 0..2 {
            if kind == 0 && (base_e.drop[i] || !ps[i].int) {
                continue;
            }
            let mut e = base_e.clone();
            if kind == 0 {
                e.drop[i] = true;
            } else {
                e.ins[i].push(0);
            }
            let regs = freplay(&ps, ntmp, &e, speed);
            let (ex, hit) = exact(&regs);
            let gained: Vec<usize> = (0..ng).filter(|g| ex[*g] && !ex0[*g]).collect();
            let lost: Vec<usize> = (0..ng).filter(|g| !ex[*g] && ex0[*g]).collect();
            if gained.is_empty() && lost.is_empty() && hit == hit0 {
                continue;
            }
            let moved = regs.iter().zip(&fast0).filter(|(a, b)| a != b).count();
            res.push((gained.len() as i64 - lost.len() as i64, hit as i64 - hit0 as i64, i, kind, gained, lost, moved));
        }
    }
    res.sort_by(|a, b| b.0.cmp(&a.0).then(a.5.len().cmp(&b.5.len())).then(b.1.cmp(&a.1)).then(a.2.cmp(&b.2)));
    println!("{} single edits tried; {} change a known pick; best (groups gained - lost, then picks):", cands.len() * 2, res.len());
    let names = |v: &[usize]| -> String {
        let mut s: Vec<String> = v.iter().take(8).map(|g| t.groups[*g].clone()).collect();
        if v.len() > 8 {
            s.push(format!("... {} more", v.len() - 8));
        }
        s.join(", ")
    };
    for (net, dh, i, kind, gained, lost, moved) in res.iter().take(top) {
        let p = &run.picks[*i];
        let g = if grp[*i] < ng { t.groups[grp[*i]].clone() } else { "-".into() };
        println!("edit {}:{} group {} {}  net {:+}  gains {} loses {}  picks {:+} ({} picks change)",
                 if *kind == 0 { "drop" } else { "ins" }, p.n, g, off(p), net, gained.len(), lost.len(), dh, moved);
        if !gained.is_empty() {
            println!("    gains: {}", names(gained));
        }
        if !lost.is_empty() {
            println!("    loses: {}", names(lost));
        }
    }
}
