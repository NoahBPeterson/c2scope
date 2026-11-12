//! `rasim fold SNAP VAR`: does a reload fold into the named variable VAR, and which single edit would make it fold.
//! Rules: docs/c2/regalloc.md section 3.6.10 (same on 13.10.3077, 13.10.3052, 14.00 and 15.00).
//!
//! A "reload" is a web whose def is an OPENTERLOAD: the dword of an address-taken local, a global, or a split piece.
//! `mov VAR <- S` reads memory directly (S has no web: "the reload folds into VAR") when
//!   - before Color (Enterloads 10b588e0): S's stretch, from its OPENTERLOAD to the next one or the block end, holds
//!     exactly one reference (the copy) and S is not live out of the block (UndoRegCandidate);
//!   - in a split round (SpillObvious 10b11560): the piece has one reference (nrefs < 2) and benefit <= 0 -> Spill.
//! CmdSubstDef (nrefs == 2) never folds a reload: it needs a def `mov web, mem` (opcode 1), and an enterload is not.
//! The colouring order of S and VAR (their exact tie: the reload is the instruction just before the copy, so it has
//! the larger defOrd and pops first) decides registers, not the fold.
//!
//! Input: a snapshot from `c2trace.py regs SRC --func F --export snap.json` made after the export carried the IL at
//! Color's entry (`il`); without it only the decision events (split pieces, CmdSubstDef) are read.

use serde_json::Value;
use std::collections::BTreeMap;

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(|x| x.as_str()).unwrap_or("")
}
fn i(v: &Value, k: &str) -> i64 {
    v.get(k).and_then(|x| x.as_i64()).unwrap_or(0)
}

/// One IL line: opcode, source line offset, dst and src operand names.
struct Ins {
    op: String,
    line: i64,
    dst: Vec<String>,
    src: Vec<String>,
}
fn names(t: &str) -> Vec<String> {
    // every identifier followed by ':<hex type>' (phys=pseudo gives the pseudo, memory gives base/index)
    let b = t.as_bytes();
    let mut out = vec![];
    let mut k = 0;
    while k < b.len() {
        let c = b[k];
        if c.is_ascii_alphabetic() || c == b'_' || c == b'?' || c == b'$' {
            let st = k;
            while k < b.len() && (b[k].is_ascii_alphanumeric() || b"_?@$.".contains(&b[k])) {
                k += 1;
            }
            if k < b.len() && b[k] == b':' {
                out.push(t[st..k].to_string());
            }
        } else {
            k += 1;
        }
    }
    out
}
fn parse(l: &str) -> Option<Ins> {
    let (head, ops) = l.split_once('|')?;
    let f: Vec<&str> = head.split_whitespace().collect();
    if f.first() != Some(&"I") || f.len() < 4 {
        return None;
    }
    let (d, sr) = ops.split_once("<-").unwrap_or((ops, ""));
    Some(Ins { op: f[2].to_string(), line: f[3].parse().unwrap_or(0), dst: names(d), src: names(sr) })
}
/// The IL spellings of a pop's name: `_x` for a named variable, `sN` for an unnamed keyed symbol (local12 = s12).
fn il_names(name: &str) -> Vec<String> {
    let mut v = vec![format!("_{}", name), name.to_string()];
    let d: String = name.chars().rev().take_while(|c| c.is_ascii_digit()).collect::<Vec<_>>().into_iter().rev().collect();
    if !d.is_empty() && name.len() > d.len() && !name.starts_with('t') {
        v.push(format!("s{}", d));
    }
    v
}
fn pop_name_of(pops: &[Value], tok: &str) -> Option<String> {
    pops.iter().map(|p| s(p, "name").to_string()).find(|n| il_names(n).iter().any(|x| x == tok))
}

pub fn main(path: &str, var: &str) {
    let txt = std::fs::read_to_string(path).unwrap_or_else(|e| super::die(&format!("{}: {}", path, e)));
    let snap: Value = serde_json::from_str(&txt).unwrap_or_else(|e| super::die(&format!("{}: {}", path, e)));
    let empty = vec![];
    let pops = snap.get("pops").and_then(|x| x.as_array()).unwrap_or(&empty);
    let decs = snap.get("decisions").and_then(|x| x.as_array()).unwrap_or(&empty);
    let passes = snap.get("passes").and_then(|x| x.as_array()).unwrap_or(&empty);
    let mine: Vec<&Value> = pops.iter().filter(|p| s(p, "name") == var).collect();
    if mine.is_empty() && !decs.iter().any(|d| s(d, "name") == var) {
        super::die(&format!("fold: no globreg named {} in {}", var, s(&snap, "func")));
    }
    println!("{}: {}", s(&snap, "func"), var);
    for p in &mine {
        println!(
            "  pop {:3}  pass {}  prio {}  benefit {}  defOrd {}  -> {}",
            i(p, "pop"),
            i(p, "npass"),
            i(p, "prio"),
            i(p, "ben"),
            i(p, "defOrd"),
            s(p, "reg")
        );
    }
    // block number of a BB in pass n (rasim's EDIT numbering)
    let bnum = |n: i64, bb: &str| -> Option<i64> {
        passes.iter().filter(|p| i(p, "n") == n || n == 0).find_map(|p| {
            p.get("blocks")?.as_array()?.iter().find(|b| s(b, "bb") == bb).map(|b| i(b, "b"))
        })
    };
    let vnames = il_names(var);
    let mut nreload = 0;
    let mut tips: Vec<String> = vec![];
    if let Some(il) = snap.get("il") {
        let order: Vec<&str> = il.get("order").and_then(|x| x.as_array()).map(|a| a.iter().filter_map(|x| x.as_str()).collect()).unwrap_or_default();
        let blocks: BTreeMap<&str, Vec<Ins>> = order
            .iter()
            .map(|bb| {
                let ls = il.get("blocks").and_then(|b| b.get(*bb)).and_then(|x| x.as_array()).map(|a| a.iter().filter_map(|l| l.as_str().and_then(parse)).collect()).unwrap_or_default();
                (*bb, ls)
            })
            .collect();
        println!("reloads copied into {} (Color entry; Enterloads keeps a reload whose stretch has 2+ references or is live out):", var);
        for bb in &order {
            let ins = &blocks[bb];
            for (k, c) in ins.iter().enumerate() {
                if c.op != "mov" || !c.dst.iter().any(|d| vnames.contains(d)) || c.src.len() != 1 {
                    continue;
                }
                let sv = &c.src[0];
                // the stretch: the last OPENTERLOAD of sv before the copy .. the next one (or the block end)
                let Some(e) = (0..k).rev().find(|&j| ins[j].op == "OPENTERLOAD" && ins[j].dst.contains(sv)) else {
                    // no reload: a memory symbol read straight into VAR (Enterloads undid it, or it never was one)
                    let mem = (sv.starts_with('_') || (sv.starts_with('s') && sv[1..].chars().all(|c| c.is_ascii_digit())))
                        && pop_name_of(pops, sv).map_or(true, |n| !pops.iter().any(|p| s(p, "name") == n && i(p, "npass") <= 1));
                    if mem {
                        let b = bnum(0, bb).map(|b| b.to_string()).unwrap_or("?".into());
                        println!("  block {} (+{}): {} <- {}: folded, {} is loaded from memory (no web for {} here)", b, c.line, var, sv, var, sv);
                        nreload += 1;
                    }
                    continue;
                };
                let end = (k + 1..ins.len()).find(|&j| ins[j].op == "OPENTERLOAD" && ins[j].dst.contains(sv)).unwrap_or(ins.len());
                let refs: Vec<usize> = (e + 1..end).filter(|&j| ins[j].src.contains(sv) || ins[j].dst.contains(sv)).collect();
                let others: Vec<usize> = refs.iter().copied().filter(|&j| j != k).collect();
                // live out: referenced in a later block before any reload of it there [I: no flow graph in the IL]
                let later = order.iter().skip_while(|b| *b != bb).skip(1).any(|b| {
                    blocks[b].iter().take_while(|x| !(x.op == "OPENTERLOAD" && x.dst.contains(sv))).any(|x| x.src.contains(sv))
                });
                nreload += 1;
                let sname = pop_name_of(pops, sv).unwrap_or_else(|| sv.clone());
                let spop = pops.iter().find(|p| s(p, "name") == sname);
                let npass = spop.map(|p| i(p, "npass")).unwrap_or(1);
                let b = bnum(npass, bb).map(|b| b.to_string()).unwrap_or("?".into());
                println!(
                    "  block {} (+{}): {} <- {}{}",
                    b,
                    c.line,
                    var,
                    sname,
                    spop.map(|p| format!("  [pop {}, prio {}, defOrd {}, {}]", i(p, "pop"), i(p, "prio"), i(p, "defOrd"), s(p, "reg"))).unwrap_or_default()
                );
                let list: Vec<String> = refs.iter().map(|&j| format!("{} +{}{}", ins[j].op, ins[j].line, if j == k { " (the copy)" } else { "" })).collect();
                println!("    stretch: {} reference{}: {}", refs.len(), if refs.len() == 1 { "" } else { "s" }, list.join(", "));
                if others.is_empty() && !later {
                    println!("    folds: the copy is the only reference, so Enterloads undoes the reload ({} is loaded from memory)", var);
                    continue;
                }
                if others.is_empty() {
                    println!("    kept although the copy is its only reference here: {} is read in a later block without a reload first (live out) [I]", sname);
                    tips.push(format!("{} in block {}: end {}'s use in the later block (read {} there)", sname, b, sname, var));
                    continue;
                }
                let what: Vec<String> = others.iter().map(|&j| format!("{} +{}", ins[j].op, ins[j].line)).collect();
                if others.len() == 1 {
                    println!(
                        "    not folded: one reference too many. Single edit: make the {} read {} instead of {} (e.g. write {}'s first use after the next call or store that may write {}'s memory, so the optimizer does not copy-propagate {} into it); then {} is loaded straight from memory{}",
                        what[0], var, sname, var, sname, sname, var,
                        if later { ", but it is also live out [I]" } else { "" }
                    );
                } else {
                    println!(
                        "    not folded: {} references besides the copy ({}); no single edit folds it: all of them must read {} (each one alone leaves the reload)",
                        others.len(),
                        what.join(", "),
                        var
                    );
                }
                let cost = 2 * others.len();
                let drop = if refs.len() == ins.iter().filter(|x| x.op != "OPENTERLOAD" && (x.src.contains(sv) || x.dst.contains(sv))).count() { "drop" } else { "unref" };
                println!("    model of the edit (priorities after it): rasim order SNAP {}:{}:{} ref:{}:{}:{}", drop, sname, b, var, b, cost);
                // the tie
                if let (Some(sp), Some(vp)) = (spop, mine.iter().find(|p| i(p, "npass") == npass)) {
                    if i(sp, "prio") == i(vp, "prio") {
                        let first = if i(sp, "defOrd") as u64 > i(vp, "defOrd") as u64 { &sname } else { var };
                        println!(
                            "    tie: {} and {} both prio {}; {} pops first (defOrd {} vs {}{}). Not a fold lever: the order decides registers only (rasim regs SNAP before:{}:{})",
                            sname,
                            var,
                            i(sp, "prio"),
                            first,
                            i(sp, "defOrd"),
                            i(vp, "defOrd"),
                            if i(sp, "defOrd") == i(vp, "defOrd") + 1 { ": the reload is the instruction just before the copy" } else { "" },
                            if first == &sname { var } else { &sname },
                            if first == &sname { &sname } else { var }
                        );
                    }
                }
                tips.push(format!("{} in block {}: {} reference(s) to move to {}", sname, b, others.len(), var));
            }
        }
        if nreload == 0 {
            println!("  none: no `mov {} <- S` right after a reload of S in the IL at Color's entry", var);
        }
    } else {
        println!("(no IL in the snapshot: re-export it with this checkout's c2trace.py regs --export to see the reloads)");
    }
    // split pieces and CmdSubstDef, from the decision events
    let enter = snap.pointer("/c2/ops/enterload").and_then(|x| x.as_str()).and_then(|x| i64::from_str_radix(x, 16).ok()).unwrap_or(0x295);
    let mut any = false;
    for (k, d) in decs.iter().enumerate() {
        if s(d, "t") != "so" || s(d, "name") != var {
            continue;
        }
        if !any {
            println!("SpillObvious on {}'s webs (each pass's new globregs):", var);
            any = true;
        }
        let gr = s(d, "gr");
        let (nrefs, ben, lastop) = (i(d, "nrefs"), i(d, "ben"), i(d, "lastop"));
        let after: Vec<&Value> = decs[k + 1..].iter().take_while(|e| s(e, "t") != "so").filter(|e| s(e, "gr") == gr).collect();
        let spilled = after.iter().any(|e| s(e, "t") == "spill");
        let subst = after.iter().find(|e| s(e, "t") == "subst");
        let isenter = lastop == enter;
        let verdict = if spilled && nrefs < 2 {
            if isenter {
                "spilled: one reference behind its reload, so the use reads memory (the reload folds)".to_string()
            } else {
                "spilled (one reference)".to_string()
            }
        } else if let Some(e) = subst {
            if e.get("ok").and_then(|x| x.as_bool()) == Some(true) {
                "CmdSubstDef folded its def into the use".to_string()
            } else if isenter {
                "CmdSubstDef tried (2 references) and cannot fold: the def is a reload (OPENTERLOAD), not `mov web, mem`; one reference fewer would let SpillObvious fold it".to_string()
            } else {
                "CmdSubstDef tried (2 references) and refused: the def is not `mov web, mem` immediately followed by its use, or the use cannot take a memory operand".to_string()
            }
        } else if isenter {
            format!("kept: {} references behind its reload; a reload folds only with 1", nrefs)
        } else {
            format!("kept: {} references", nrefs)
        };
        println!("  pass {}  {}  nrefs {}  benefit {}  def {}: {}", i(d, "npass"), gr, nrefs, ben, if isenter { "reload".into() } else { format!("op {}", lastop) }, verdict);
    }
    if !tips.is_empty() {
        println!("single edits that fold a reload into {}:", var);
        for t in &tips {
            println!("  {}", t);
        }
    } else if nreload > 0 {
        println!("every reload copied into {} already folds", var);
    }
}
