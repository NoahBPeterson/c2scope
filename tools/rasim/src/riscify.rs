//! cl 13.10's Riscify GetReg calls and `want:` answers for `rasim scratch` (docs/c2/regalloc.md section 6.6).
//!
//! Riscify runs after RegSimpleAssign. For an instruction it may split (a memory operand loaded into a new temp:
//! `add eax, [m]` -> `mov esi, [m]; add eax, esi`), it asks GetReg (13.10.3077 10785179) for a register:
//!   pass 0: the entries of _IntPhysRegs after the cursor (wrapping), up to the cursor itself, that are in the
//!           function's register table, free (not live there) and, for a byte value, eax ecx edx ebx;
//!   pass 1: the entries from the first one up to the cursor, without the table test;
//!   last:   first fit over the list (it ends at the first empty entry).
//! The cursor is set to the entry taken, by every call, whether or not Riscify then splits the instruction, and it
//! restarts at the top of every block (the entry before eax), which Riscify walks from its last instruction up.
//! The snapshot (`c2trace.py scratch --export`, key `riscify`) has every call: its block, busy registers, byte flag,
//! the traced cursor, the result and the temps it made, and (key `why`) why the instruction was split or not and
//! whether the split survived: a call for every instruction whose opcode has _Opcode_dope & 0x400 (never mov, lea,
//! imul); the first memory operand is split when GetReg finds a register; RiscifyEarlyUndo puts 16-bit splits back,
//! Riscify_undo (PreEncode, after Schedule) every split whose load is still right before its use (regalloc.md 6.6).

use serde::Deserialize;

#[derive(Deserialize, Clone)]
pub struct RCall {
    pub n: usize,
    pub block: Option<i64>,
    pub busy: Vec<String>,
    #[serde(default)]
    pub byte: bool,
    pub cur: Option<usize>,
    pub order: Vec<String>,
    pub count: Option<usize>,
    #[serde(default)]
    pub used: Vec<String>,
    pub reg: Option<String>,
    #[serde(default)]
    pub made: Vec<String>,
    pub off: Option<u64>,
    #[serde(default)]
    pub op: String,
    #[serde(default)]
    pub instr: String,
    /// why Riscify split the instruction or not, and whether the split survived (c2scratch.py split_reason)
    #[serde(default)]
    pub why: String,
}

#[derive(Deserialize, Clone)]
pub struct Unmod {
    pub tmp: String,
    pub reg: String,
    pub off: Option<u64>,
    pub phase: String,
    #[serde(default)]
    pub instr: String,
}

#[derive(Clone)]
pub enum REdit {
    Drop(usize),
    Ins(usize),
}

fn byte_ok(r: &str) -> bool {
    matches!(r, "eax" | "ecx" | "edx" | "ebx")
}

/// One GetReg call: -> (register, entry index taken), None when no register is free.
fn getreg(c: &RCall, busy: &[String], byte: bool, cur: usize) -> Option<(String, usize)> {
    let order = &c.order;
    let count = c.count.unwrap_or(order.len()).min(order.len()).max(1);
    let next = |i: usize| if i + 1 >= count { 0 } else { i + 1 };
    let ok = |i: usize| {
        let r = &order[i];
        !r.is_empty() && (!byte || byte_ok(r)) && !busy.iter().any(|b| b == r)
    };
    let mut i = next(cur);
    while i != cur {
        if ok(i) && c.used.iter().any(|u| *u == order[i]) {
            return Some((order[i].clone(), i));
        }
        i = next(i);
    }
    let mut i = 0;
    while i != cur && i < count {
        if ok(i) {
            return Some((order[i].clone(), i));
        }
        i = next(i);
    }
    for (i, r) in order.iter().enumerate() {
        if r.is_empty() {
            break;
        }
        if ok(i) {
            return Some((r.clone(), i));
        }
    }
    None
}

pub struct ROut {
    pub n: usize, // 0: an inserted call
    pub reg: Option<String>,
    pub cur: usize,
}

/// Replay every call with the model's own cursor (reset at each new block). With `resync`, a miss is corrected to
/// the traced cursor before going on.
pub fn replay(calls: &[RCall], edits: &[REdit], resync: bool) -> Vec<ROut> {
    let mut out = vec![];
    let mut cur = 0usize;
    let mut blk: Option<Option<i64>> = None;
    for c in calls {
        let start = c.count.unwrap_or(c.order.len()).max(1) - 1;
        if blk != Some(c.block) {
            cur = start;
            blk = Some(c.block);
        }
        for e in edits {
            if let REdit::Ins(n) = e {
                if *n == c.n {
                    let r = getreg(c, &c.busy, false, cur);
                    out.push(ROut { n: 0, reg: r.as_ref().map(|x| x.0.clone()), cur });
                    if let Some((_, i)) = r {
                        cur = i;
                    }
                }
            }
        }
        if edits.iter().any(|e| matches!(e, REdit::Drop(n) if *n == c.n)) {
            continue;
        }
        let r = getreg(c, &c.busy, c.byte, cur);
        out.push(ROut { n: c.n, reg: r.as_ref().map(|x| x.0.clone()), cur });
        if let Some((_, i)) = r {
            cur = i;
        }
        if resync {
            if let Some(t) = &c.reg {
                if let Some(i) = c.order.iter().position(|x| x == t) {
                    cur = i;
                }
            }
        }
    }
    out
}

fn curname(c: &RCall, i: usize) -> String {
    let start = c.count.unwrap_or(c.order.len()).max(1) - 1;
    if i >= start || c.order.get(i).map_or(true, |r| r.is_empty()) { "start".into() } else { c.order[i].clone() }
}

/// `rasim scratch SNAP` with a riscify key: predicted vs traced, one summary line.
pub fn check(calls: &[RCall], verbose: bool) {
    let out = replay(calls, &[], true);
    let (mut hit, mut chit) = (0, 0);
    let mut lines = vec![];
    for (o, c) in out.iter().zip(calls) {
        let good = o.reg == c.reg;
        let cgood = c.cur.map_or(true, |t| curname(c, t) == curname(c, o.cur));
        hit += good as usize;
        chit += cgood as usize;
        if verbose || !good || !cgood {
            lines.push(format!(
                "  R{:<3} blk {:<3} cursor {:<5} predicted {:<5} traced {:<5} (cursor {})  {}  busy {}{}  {}",
                c.n, c.block.map_or("?".into(), |b| b.to_string()), curname(c, o.cur), o.reg.as_deref().unwrap_or("none"),
                c.reg.as_deref().unwrap_or("?"), c.cur.map_or("?".into(), |t| curname(c, t)),
                if good && cgood { "ok" } else { "MISS" }, c.busy.join(","), if c.byte { " byte" } else { "" },
                if c.made.is_empty() { String::new() } else { format!("made {}", c.made.join(" ")) }
            ));
            if verbose && !c.why.is_empty() {
                lines.push(format!("        {}", c.why));
            }
        }
    }
    for l in lines {
        println!("{}", l);
    }
    let made: Vec<String> = calls.iter().filter(|c| !c.made.is_empty())
        .map(|c| format!("{} {} (R{})", c.made.join(" "), c.reg.as_deref().unwrap_or("?"), c.n)).collect();
    println!(
        "riscify: {} of {} GetReg calls predicted (register); cursor right at {} of {}; temps made: {}",
        hit, calls.len(), chit, calls.len(), if made.is_empty() { "none".into() } else { made.join(", ") }
    );
}

/// What-if on the GetReg calls: the calls (and so the temps) whose register changes.
pub fn whatif(calls: &[RCall], edits: &[REdit]) -> usize {
    for e in edits {
        let n = match e {
            REdit::Drop(n) | REdit::Ins(n) => *n,
        };
        if !calls.iter().any(|c| c.n == n) {
            crate::die(&format!("no GetReg call R{} (rasim scratch SNAP lists them)", n));
        }
    }
    let base = replay(calls, &[], false);
    let bad = base.iter().zip(calls).filter(|(o, c)| o.reg != c.reg).count();
    if bad > 0 {
        println!("warning: the GetReg model misses {} calls without any edit: the answer below is unreliable", bad);
    }
    let out = replay(calls, edits, false);
    let mut changed = 0;
    for o in out.iter().filter(|o| o.n == 0) {
        println!("  new GetReg call  cursor {}  -> {}", curname(&calls[0], o.cur), o.reg.as_deref().unwrap_or("none"));
    }
    for c in calls {
        match out.iter().find(|o| o.n == c.n) {
            None => println!("  R{:<3} removed (was {}){}", c.n, c.reg.as_deref().unwrap_or("?"),
                             if c.made.is_empty() { String::new() } else { format!("; it made {}", c.made.join(" ")) }),
            Some(o) if o.reg != c.reg => {
                changed += 1;
                println!(
                    "  R{:<3} {:>7}  {} -> {}  (cursor {}){}  {} {}",
                    c.n, c.off.map_or("gone".into(), |x| format!("+0x{:x}", x)), c.reg.as_deref().unwrap_or("?"),
                    o.reg.as_deref().unwrap_or("none"), curname(c, o.cur),
                    if c.made.is_empty() { String::new() } else { format!("  temps {}", c.made.join(" ")) }, c.op,
                    if c.instr.len() > 60 { &c.instr[..60] } else { &c.instr }
                );
            }
            _ => {}
        }
    }
    let temps: Vec<String> = calls.iter().filter(|c| !c.made.is_empty()).filter_map(|c| {
        let o = out.iter().find(|o| o.n == c.n);
        match o {
            None => Some(format!("{} gone", c.made.join(" "))),
            Some(o) if o.reg != c.reg => Some(format!("{} {} -> {}", c.made.join(" "), c.reg.as_deref().unwrap_or("?"), o.reg.as_deref().unwrap_or("none"))),
            _ => None,
        }
    }).collect();
    println!(
        "what-if (riscify): {} of {} GetReg calls change register; temps: {}",
        changed, calls.len(), if temps.is_empty() { "unchanged".into() } else { temps.join(", ") }
    );
    changed
}

/// want:T=R for a temp Riscify made: every single rdrop:K / rins:K that gives it R.
pub fn want(calls: &[RCall], t: &str, r: &str) -> bool {
    let Some(c) = calls.iter().find(|c| c.made.iter().any(|m| m == t)) else { return false };
    println!(
        "{}: made by Riscify (GetReg call R{}, block {}, busy {}): {} by GetReg's rotation from cursor {}",
        t, c.n, c.block.map_or("?".into(), |b| b.to_string()), c.busy.join(",").to_string() + if c.byte { " byte" } else { "" },
        c.reg.as_deref().unwrap_or("?"), c.cur.map_or("?".into(), |i| curname(c, i))
    );
    if !c.why.is_empty() {
        println!("  R{}: {}", c.n, c.why);
    }
    if c.reg.as_deref() == Some(r) {
        println!("  it already has {}", r);
        return true;
    }
    if c.busy.iter().any(|b| b == r) {
        println!("  {} is live there (busy): no change to the GetReg calls gives it; free {} at that instruction first", r, r);
    }
    let mut hits = vec![];
    let same: Vec<&RCall> = calls.iter().filter(|x| x.block == c.block && x.n <= c.n).collect();
    for k in same.iter().map(|x| x.n) {
        for e in [REdit::Drop(k), REdit::Ins(k)] {
            if matches!(e, REdit::Drop(n) if n == c.n) {
                continue;
            }
            let lbl = match e {
                REdit::Drop(n) => format!("rdrop:{}", n),
                REdit::Ins(n) => format!("rins:{}", n),
            };
            let out = replay(calls, &[e], false);
            if out.iter().find(|o| o.n == c.n).and_then(|o| o.reg.as_deref()) == Some(r) {
                let others = out.iter().filter(|o| o.n != 0 && o.n != c.n)
                    .filter(|o| calls.iter().find(|x| x.n == o.n).map_or(false, |x| x.reg != o.reg && !x.made.is_empty())).count();
                hits.push(format!("  {:<9} {} gets {}{}", lbl, t, r,
                    if others > 0 { format!(" ({} other made temps change)", others) } else { String::new() }));
            }
        }
    }
    if hits.is_empty() {
        println!("  no single rdrop:K / rins:K in its block gives {} (the calls before it in the walk: {})", r,
                 same.iter().filter(|x| x.n < c.n).map(|x| format!("R{}", x.n)).collect::<Vec<_>>().join(" "));
    } else {
        println!("  single edits in its block (Riscify walks the block from its last instruction up, so 'before' in the walk is later in the code):");
        for h in hits {
            println!("{}", h);
        }
        for x in same.iter().filter(|x| x.n < c.n && !x.why.is_empty()) {
            println!("  R{} ({} {}): {}", x.n, x.op, if x.instr.len() > 40 { &x.instr[..40] } else { &x.instr }, x.why);
        }
        println!("  rdrop:K = call K does not happen: that instruction is gone, moved to another block, or has an opcode GetReg is not called for (mov, lea, imul; a register-only add/cmp/test/or/push.. still calls it); rins:K = one more such instruction just before K in the walk (just after it in the code)");
    }
    true
}
