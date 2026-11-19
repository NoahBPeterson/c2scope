//! x87 stack after Color (cl 14.00 and 15.00): the GFx -> ST(i) mapping and the calls that keep x87 values.
//! Doc: docs/c2/x87.md section 12.
//!
//! The snapshot's `x87` key (tools/c2x87.py, also added by `c2trace.py regs --export` on 14.00 / 15.00) holds the
//! function in GF form as X87MapGFxToSTx maps it (IL at BuildLiveOnEntryAndExitSets, i.e. after
//! PerformPreMappingPeepholes), its blocks with predecessor and successor lists, the mapper's output (IL at FPPeeps
//! entry), every call with the kill set LoAddCallScratchRegs gave it, and every function body generated before it
//! with the number of x87 values its final code pushes (MscCollectUsedRegs).
//!
//! Two models, both predicted from the snapshot and checked against the trace:
//!
//!   calls   LoAddCallScratchRegs 10c3f5f0 (15.00 51215420) adds GF0..GF7 to a call's kill set unless the target's
//!           symbol has sym+0x20 & 0x10000, which MscRecordInterProcRegInfo 10ba2290 (510ed8d0) sets on a function
//!           whose final code pushes no x87 value, or pushes one and returns float / double (sym+0x37 & 0x1f == 5).
//!           A pushed value: fld, fild, fldz, fld1, fldpi.., fptan, fxtract; a call to a target that is not x87-free,
//!           an indirect call or inline asm counts as 2. Only bodies generated before the caller have the flag
//!           (code generation is bottom-up over the call graph, so source order does not matter, but an in-class
//!           member function is generated after its callers). The call's own FP result register is never killed.
//!           A value live across a call whose kill set holds no GF stays on the stack; otherwise Color splits it.
//!   mapper  X87MapGFxToSTx 10c2eb00 (512048d0): a depth-first worklist of blocks; each starts from the layout of
//!           the first predecessor mapped (InitializeFunctionBlockList, PropagateBlocksLiveOnExit); per instruction
//!           ProcessBlock 10c30170: loads push (0.0 / 1.0 -> fldz / fld1), a copy of a dying value renames, of a live
//!           one is `fld ST(i)`, stores `fst` / `fstp` after an fxch to the top, binary ops raise one operand to
//!           the top and use the pop form when the other dies (X87StkRaiseAndMapSrcsToST0X, processEgFADD/FSUB);
//!           FinalizeBlock 10c2f4e0: `fldz` for a value live out but not on the stack (AppendFldZeroToBlock), and
//!           an airlock block on every edge into a block whose layout is fixed and differs (pops of values dead
//!           on the edge in GF order, fldz for values live in but missing, then fxch pairs sorting the stack:
//!           InsertExitToEntryAdjustments 10c2f810, BalanceBlockWithConstraint 10c2eef0).
//!
//! Commands (main.rs): `rasim check SNAP --predict` prints both validations when the snapshot has `x87`;
//! `rasim need SNAP stack:V` on a 14.00 / 15.00 snapshot answers what keeps V on the stack or forces its reload.

use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet, HashMap};

#[derive(Clone, Debug, PartialEq)]
enum Opd {
    Gf(u8, String),
    Mem(String),
    Other,
}

#[derive(Clone, Debug)]
struct Ins {
    tup: String,
    op: String,
    line: i64,
    dsts: Vec<Opd>,
    srcs: Vec<Opd>,
}

#[derive(Clone, Debug, Default)]
struct Blk {
    bb: String,
    ins: Vec<Ins>,
    preds: Vec<String>,
    succs: Vec<String>,
}


fn parse_opd(t: &str) -> Opd {
    // GF3=t249:5008, GF0f=__real@00000000:5004, [esi=_out:1004+4]:5004, _k:5004, eax=t1:1004, fcc:9000, 4:2004
    if let Some(rest) = t.strip_prefix("GF") {
        let n: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
        if let Ok(k) = n.parse::<u8>() {
            let name = t.split_once('=').map(|(_, b)| b.rsplit_once(':').map(|(a, _)| a).unwrap_or(b)).unwrap_or("");
            return Opd::Gf(k, name.to_string());
        }
    }
    let (body, ty) = t.rsplit_once(':').unwrap_or((t, ""));
    if ty.starts_with('9') || body.is_empty() {
        return Opd::Other;
    }
    if t.starts_with('[') {
        return Opd::Mem(body.to_string());
    }
    if body.contains('=') {
        return Opd::Other; // a physical register
    }
    // anything else named (a variable, a temporary, a global or a constant symbol: _x, t12, s3, ?g@@3NA, __real@..)
    // is a memory operand; numbers, register sets (regset), PAS sets (pasN) and esp are not
    let numeric = body.trim_start_matches('-').chars().all(|x| x.is_ascii_digit() || x == '.');
    let special = body.starts_with("ST(") || body.starts_with("regset") || body.starts_with("pas") || body == "esp" || body.starts_with("cc");
    if !numeric && !special && (ty.starts_with('5') || ty.starts_with('6') || ty.starts_with('1') || ty.starts_with('2')) {
        return Opd::Mem(body.to_string());
    }
    Opd::Other
}

fn parse_ins(l: &str) -> Option<Ins> {
    // I <tuple> <op> <line> <len> | dsts <- srcs
    let (head, rest) = l.split_once('|')?;
    let h: Vec<&str> = head.split_whitespace().collect();
    if h.len() < 4 || h[0] != "I" {
        return None;
    }
    let (d, s) = rest.split_once("<-").unwrap_or((rest, ""));
    let s = s.split(" ; ").next().unwrap_or(s);
    let s = s.split("->").next().unwrap_or(s);
    Some(Ins {
        tup: h[1].to_string(),
        op: h[2].to_string(),
        line: h[3].parse().unwrap_or(0),
        dsts: d.split_whitespace().map(parse_opd).collect(),
        srcs: s.split_whitespace().map(parse_opd).collect(),
    })
}

fn parse_blocks(lines: &[String]) -> Vec<Blk> {
    let mut out: Vec<Blk> = vec![];
    for l in lines {
        if let Some(bb) = l.strip_prefix("B ") {
            if bb.trim() == "0" {
                continue;
            }
            out.push(Blk { bb: bb.trim().to_string(), ..Default::default() });
        } else if l.starts_with("I ") {
            if let (Some(b), Some(i)) = (out.last_mut(), parse_ins(l)) {
                b.ins.push(i);
            }
        }
    }
    out
}

fn gfs(v: &[Opd]) -> Vec<u8> {
    v.iter().filter_map(|o| if let Opd::Gf(k, _) = o { Some(*k) } else { None }).collect()
}

struct Func {
    p6: bool,
    blocks: Vec<Blk>,
    idx: HashMap<String, usize>,
    live_in: Vec<u8>,
    live_out: Vec<u8>,
    names: BTreeMap<u8, BTreeSet<String>>,
}

fn bits(v: &[u8]) -> u8 {
    v.iter().fold(0u8, |a, k| a | (1 << k))
}

impl Func {
    fn new(x: &Value) -> Func {
        let lines: Vec<String> = x["gf"].as_array().map(|a| a.iter().filter_map(|s| s.as_str().map(String::from)).collect()).unwrap_or_default();
        let mut blocks = parse_blocks(&lines);
        let mut gv: HashMap<String, (Vec<String>, Vec<String>)> = HashMap::new();
        for l in x["gview"].as_array().into_iter().flatten().filter_map(|s| s.as_str()) {
            if !l.starts_with("G B ") {
                continue;
            }
            let f: Vec<&str> = l.split_whitespace().collect();
            let bb = f.get(3).unwrap_or(&"").to_string();
            let lst = |p: &str| -> Vec<String> {
                f.iter().find_map(|t| t.strip_prefix(p)).map(|s| s.split(',').filter(|x| !x.is_empty()).map(String::from).collect()).unwrap_or_default()
            };
            gv.insert(bb, (lst("p="), lst("s=")));
        }
        for b in blocks.iter_mut() {
            if let Some((p, s)) = gv.get(&b.bb) {
                b.preds = p.clone();
                b.succs = s.clone();
            }
        }
        let idx: HashMap<String, usize> = blocks.iter().enumerate().map(|(i, b)| (b.bb.clone(), i)).collect();
        let n = blocks.len();
        let mut uses = vec![0u8; n];
        let mut defs = vec![0u8; n];
        let mut names: BTreeMap<u8, BTreeSet<String>> = BTreeMap::new();
        for (i, b) in blocks.iter().enumerate() {
            for ins in &b.ins {
                for o in ins.srcs.iter().chain(ins.dsts.iter()) {
                    if let Opd::Gf(k, nm) = o {
                        if !nm.is_empty() {
                            names.entry(*k).or_default().insert(nm.clone());
                        }
                    }
                }
                uses[i] |= bits(&gfs(&ins.srcs)) & !defs[i];
                defs[i] |= bits(&gfs(&ins.dsts));
            }
        }
        let mut live_in = vec![0u8; n];
        let mut live_out = vec![0u8; n];
        loop {
            let mut ch = false;
            for i in (0..n).rev() {
                let o = blocks[i].succs.iter().filter_map(|s| idx.get(s)).fold(0u8, |a, &j| a | live_in[j]);
                let li = uses[i] | (o & !defs[i]);
                if o != live_out[i] || li != live_in[i] {
                    live_out[i] = o;
                    live_in[i] = li;
                    ch = true;
                }
            }
            if !ch {
                break;
            }
        }
        let p6 = x["cl"].as_array().into_iter().flatten().filter_map(|a| a.as_str())
            .any(|a| a.to_ascii_lowercase().starts_with("/arch:sse") || a.to_ascii_lowercase().starts_with("-arch:sse"));
        Func { p6, blocks, idx, live_in, live_out, names }
    }
}

/// One mapped x87 instruction, in the canonical text both sides are reduced to.
type Key = String;

struct Sim<'a> {
    f: &'a Func,
    p6: bool,                           // fcomi (a P6 target: /arch:SSE or /arch:SSE2)
    gaps: usize,                        // GF sources not on the model's stack (should stay 0)
    out: Vec<Vec<Key>>,                 // per block
    airlocks: Vec<(usize, usize, Vec<Key>)>,
    entry: Vec<Option<Vec<u8>>>,        // fixed entry layouts (top first)
    fldz_live_out: Vec<(usize, u8)>,
}

fn st(stk: &[u8], g: u8) -> Option<usize> {
    stk.iter().position(|&x| x == g)
}

impl<'a> Sim<'a> {
    fn fxch_top(stk: &mut Vec<u8>, i: usize, out: &mut Vec<Key>) {
        if i != 0 {
            stk.swap(0, i);
            out.push(format!("fxch ST({})", i));
        }
    }

    fn block(&mut self, bi: usize, mut stk: Vec<u8>) -> Vec<u8> {
        let b = &self.f.blocks[bi];
        let mut out: Vec<Key> = vec![];
        // death: backward over the block from its live-out set
        let n = b.ins.len();
        let mut after = vec![0u8; n];
        let mut live = self.f.live_out[bi];
        for (k, ins) in b.ins.iter().enumerate().rev() {
            after[k] = live;
            live &= !bits(&gfs(&ins.dsts));
            live |= bits(&gfs(&ins.srcs));
        }
        for (k, ins) in b.ins.iter().enumerate() {
            let dies = |g: u8| after[k] & (1 << g) == 0;
            let sg = gfs(&ins.srcs);
            let dg = gfs(&ins.dsts);
            let op = ins.op.as_str();
            for &g in &sg {
                // a GF source the model does not have on its stack is a model gap: assume it was pushed, go on
                if st(&stk, g).is_none() && op != "call" {
                    stk.insert(0, g);
                    self.gaps += 1;
                }
            }
            match op {
                "_fmov" | "fld" | "fst" | "fstp" => {
                    if let (Some(&d), Some(&s)) = (dg.first(), sg.first()) {
                        // GFd <- GFs
                        if st(&stk, s).is_none() {
                            stk.insert(0, s); // not on the model's stack (a model gap): assume it was pushed
                        }
                        let si = st(&stk, s).unwrap_or(0);
                        if dies(d) && d != s {
                            continue;
                        }
                        if dies(s) || d == s {
                            stk[si] = d;
                        } else {
                            if let Some(di) = st(&stk, d) {
                                // a stale value of d: overwrite it
                                if si == 0 {
                                    out.push(format!("fst ST({})", di));
                                    stk[di] = d;
                                    stk[0] = s;
                                    continue;
                                }
                            }
                            out.push(format!("fld ST({})", si));
                            stk.insert(0, d);
                        }
                    } else if let Some(&d) = dg.first() {
                        // GFd <- mem
                        if dies(d) {
                            continue;
                        }
                        if let Some(di) = st(&stk, d) {
                            Self::fxch_top(&mut stk, di, &mut out);
                            out.push("fstp ST(0)".into());
                            stk.remove(0);
                        }
                        let m = ins.srcs.iter().find_map(|o| if let Opd::Mem(t) = o { Some(t.clone()) } else { None }).unwrap_or_default();
                        let z = m.starts_with("__real@") && m[7..].chars().all(|c| c == '0');
                        let one = m == "__real@3f800000" || m == "__real@3ff0000000000000";
                        out.push(if z { "fldz".into() } else if one { "fld1".into() } else { "fld m".into() });
                        stk.insert(0, d);
                    } else if let Some(&s) = sg.first() {
                        // mem <- GFs
                        if st(&stk, s).is_none() {
                            stk.insert(0, s);
                        }
                        let si = st(&stk, s).unwrap_or(0);
                        Self::fxch_top(&mut stk, si, &mut out);
                        if dies(s) {
                            out.push("fstp m".into());
                            stk.remove(0);
                        } else {
                            out.push("fst m".into());
                        }
                    }
                }
                "fadd" | "fmul" | "fsub" | "fdiv" | "fsubr" | "fdivr" | "fiadd" | "fimul" | "fisub" | "fidiv" => {
                    let rev0 = op.ends_with('r');
                    let base = op.trim_end_matches('r');
                    let (s1, s2) = (sg.first().copied(), sg.get(1).copied());
                    let d = dg.first().copied().unwrap_or(0);
                    let Some(s1) = s1 else { continue };
                    let s1d = dies(s1) || d == s1;
                    match s2 {
                        None => {
                            // s2 in memory: s1 to the top, fop m
                            let i = st(&stk, s1).unwrap_or(0);
                            Self::fxch_top(&mut stk, i, &mut out);
                            out.push(format!("{} m", op));
                            stk[0] = d;
                        }
                        Some(s2) => {
                            let s2d = dies(s2) || d == s2;
                            let (i1, i2) = (st(&stk, s1).unwrap_or(0), st(&stk, s2).unwrap_or(0));
                            // raise (X87StkRaiseAndMapSrcsToST0X mode 1)
                            let top_is_s2 = if i1 == 0 {
                                false
                            } else if i2 == 0 {
                                true
                            } else if !s1d && s2d {
                                Self::fxch_top(&mut stk, i2, &mut out);
                                true
                            } else {
                                Self::fxch_top(&mut stk, i1, &mut out);
                                false
                            };
                            let (t, o) = if top_is_s2 { (s2, s1) } else { (s1, s2) };
                            let (td, od) = if top_is_s2 { (s2d, s1d) } else { (s1d, s2d) };
                            let k = st(&stk, o).unwrap_or(0);
                            let _ = t;
                            let commut = base == "fadd" || base == "fmul" || base == "fiadd" || base == "fimul";
                            if od {
                                // result into O's slot, and T popped when it dies too: reverse when that slot held s2
                                let r = !commut && ((o == s2) != rev0);
                                if td {
                                    out.push(format!("{}{}p ST({}),ST(0)", base, if r { "r" } else { "" }, k));
                                    stk[k] = d;
                                    stk.remove(0);
                                } else {
                                    out.push(format!("{}{} ST({}),ST(0)", base, if r { "r" } else { "" }, k));
                                    stk[k] = d;
                                }
                            } else {
                                // result on top (T's slot); reverse when T was s2
                                let r = !commut && ((t == s2) != rev0);
                                let _ = td;
                                out.push(format!("{}{} ST(0),ST({})", base, if r { "r" } else { "" }, k));
                                stk[0] = d;
                            }
                        }
                    }
                }
                "fchs" | "fabs" | "fsqrt" | "fsin" | "fcos" | "frndint" | "f2xm1" => {
                    if let Some(&s) = sg.first() {
                        let i = st(&stk, s).unwrap_or(0);
                        Self::fxch_top(&mut stk, i, &mut out);
                        out.push(op.to_string());
                        stk[0] = dg.first().copied().unwrap_or(s);
                    }
                }
                "_fcvt" => {
                    // processFCVT 10c32140: int -> FP is a push (fild m); FP -> int raises the source, fistp if it dies
                    if let (Some(&d), true) = (dg.first(), sg.is_empty()) {
                        out.push("fild m".into());
                        stk.insert(0, d);
                    } else if let Some(&s) = sg.first() {
                        if dg.is_empty() {
                            let i = st(&stk, s).unwrap_or(0);
                            Self::fxch_top(&mut stk, i, &mut out);
                            if dies(s) {
                                out.push("fistp m".into());
                                stk.remove(0);
                            } else {
                                out.push("fist m".into());
                            }
                        }
                    }
                }
                "fild" | "fldz" | "fld1" | "fldpi" => {
                    if let Some(&d) = dg.first() {
                        out.push(if op == "fild" { "fild m".into() } else { op.to_string() });
                        stk.insert(0, d);
                    }
                }
                "fist" | "fistp" => {
                    if let Some(&s) = sg.first() {
                        let i = st(&stk, s).unwrap_or(0);
                        Self::fxch_top(&mut stk, i, &mut out);
                        if dies(s) {
                            out.push("fistp m".into());
                            stk.remove(0);
                        } else {
                            out.push("fist m".into());
                        }
                    }
                }
                "fcom" | "fucom" | "ficom" => {
                    let (l, r) = (sg.first().copied(), sg.get(1).copied());
                    match (l, r) {
                        (Some(l), None) | (None, Some(l)) => {
                            let i = st(&stk, l).unwrap_or(0);
                            Self::fxch_top(&mut stk, i, &mut out);
                            // there is no fucom with a memory operand: == and != use fcom m too (table 10d09ac0)
                            let op = if op == "fucom" { "fcom" } else { op };
                            if dies(l) {
                                out.push(format!("{}p m", op));
                                stk.remove(0);
                            } else {
                                out.push(format!("{} m", op));
                            }
                        }
                        (Some(l), Some(r)) if self.p6 => {
                            // P6 target (/arch:SSE, SSE2): fcomi; the right operand is raised to the top
                            let (ld, rd) = (dies(l), dies(r));
                            let ir = st(&stk, r).unwrap_or(0);
                            Self::fxch_top(&mut stk, ir, &mut out);
                            let k = st(&stk, l).unwrap_or(1);
                            let u = if op == "fucom" { "fucomi" } else { "fcomi" };
                            if rd {
                                out.push(format!("{}p ST(0),ST({})", u, k));
                                stk.remove(0);
                            } else {
                                out.push(format!("{} ST(0),ST({})", u, k));
                            }
                            if ld {
                                let j = st(&stk, l).unwrap_or(0);
                                Self::fxch_top(&mut stk, j, &mut out);
                                out.push("fstp ST(0)".into());
                                stk.remove(0);
                            }
                        }
                        (Some(l), Some(r)) => {
                            let (ld, rd) = (dies(l), dies(r));
                            let (il, ir) = (st(&stk, l).unwrap_or(0), st(&stk, r).unwrap_or(0));
                            let top_r = if il == 0 {
                                false
                            } else if ir == 0 {
                                true
                            } else if !ld && rd {
                                Self::fxch_top(&mut stk, ir, &mut out);
                                true
                            } else {
                                Self::fxch_top(&mut stk, il, &mut out);
                                false
                            };
                            let (t, o) = if top_r { (r, l) } else { (l, r) };
                            let (td, od) = if top_r { (rd, ld) } else { (ld, rd) };
                            let k = st(&stk, o).unwrap_or(1);
                            let _ = t;
                            if td && od && k == 1 {
                                out.push(format!("{}pp", op));
                                stk.drain(0..2);
                            } else if td {
                                out.push(format!("{}p ST({})", op, k));
                                stk.remove(0);
                                if od {
                                    // processEgFCOM: fxch ST(j); fstp ST(0) after the fnstsw (LoFinalLower folds it)
                                    let j = st(&stk, o).unwrap_or(0);
                                    Self::fxch_top(&mut stk, j, &mut out);
                                    out.push("fstp ST(0)".into());
                                    stk.remove(0);
                                }
                            } else {
                                out.push(format!("{} ST({})", op, k));
                                if od {
                                    Self::fxch_top(&mut stk, k, &mut out);
                                    out.push("fstp ST(0)".into());
                                    stk.remove(0);
                                }
                            }
                        }
                        _ => {}
                    }
                }
                "call" => {
                    // GF sources (helper calls) are consumed; an FP result is pushed
                    for &s in &sg {
                        if let Some(i) = st(&stk, s) {
                            stk.remove(i);
                        }
                    }
                    if let Some(&d) = dg.first() {
                        stk.insert(0, d);
                    } else if ins.dsts.iter().any(|o| matches!(o, Opd::Other)) && false {
                        out.push("fstp ST(0)".into());
                    }
                }
                "OPSIDEEFFECT" => {
                    if ins.dsts.iter().all(|o| *o == Opd::Other) && !sg.is_empty() {
                        if !stk.is_empty() {
                            stk.remove(0);
                        }
                    }
                }
                _ => {
                    // any other instruction that writes a GF value from nothing on the stack (OPHARDINTRIN: an
                    // intrinsic's x87 result) pushes it
                    if sg.is_empty() {
                        for &d in &dg {
                            stk.insert(0, d);
                        }
                    }
                }
            }
        }
        if std::env::var("X87DEBUG").is_ok() {
            eprintln!("block {} exit stack (top first) GF{:?} live out {:02x}", b.bb, stk, self.f.live_out[bi]);
        }
        // FinalizeBlock: fldz for what is live out but not on the stack
        let lo = self.f.live_out[bi];
        for g in 0..8u8 {
            if lo & (1 << g) != 0 && st(&stk, g).is_none() {
                out.push("fldz".into());
                stk.insert(0, g);
                self.fldz_live_out.push((bi, g));
            }
        }
        self.out[bi] = out;
        stk
    }

    /// InsertExitToEntryAdjustments (+ BalanceBlockWithConstraint when `want` is the fixed layout)
    fn airlock(&mut self, from: usize, to: usize, stk: &[u8], want: Option<&Vec<u8>>) -> Vec<u8> {
        let mut s = stk.to_vec();
        let mut out = vec![];
        let lo = self.f.live_out[from];
        let li = self.f.live_in[to];
        for g in 0..8u8 {
            if lo & !li & (1 << g) != 0 {
                if let Some(i) = st(&s, g) {
                    out.push(format!("fstp ST({})", i));
                    s.remove(i);
                    if i > 0 {
                        let t = s.remove(0);
                        s.insert(i - 1, t);
                    }
                }
            }
        }
        for g in 0..8u8 {
            if li & !lo & (1 << g) != 0 {
                out.push("fldz".into());
                s.insert(0, g);
            }
        }
        if let Some(w) = want {
            if w.len() == s.len() {
                for k in (1..s.len()).rev() {
                    if s[k] != w[k] {
                        let j = st(&s, w[k]).unwrap_or(0);
                        Self::fxch_top(&mut s, j, &mut out);
                        Self::fxch_top(&mut s, k, &mut out);
                    }
                }
            }
        }
        if !out.is_empty() {
            self.airlocks.push((from, to, out));
        }
        s
    }

    fn run(f: &'a Func) -> Sim<'a> {
        let n = f.blocks.len();
        let mut sim = Sim { f, p6: f.p6, gaps: 0, out: vec![vec![]; n], airlocks: vec![], entry: vec![None; n], fldz_live_out: vec![] };
        // InitializeFunctionBlockList: every block with no predecessor or no GF live in starts empty; the worklist
        // is LIFO and its head is the first such block in layout order (the entry block first)
        let mut work: Vec<usize> = vec![];
        for (i, b) in f.blocks.iter().enumerate().rev() {
            if b.preds.is_empty() || f.live_in[i] == 0 {
                sim.entry[i] = Some(vec![]);
                work.push(i);
            }
        }
        let mut done = vec![false; n];
        while let Some(bi) = work.pop() {
            if done[bi] {
                continue;
            }
            let stk = sim.block(bi, sim.entry[bi].clone().unwrap_or_default());
            done[bi] = true;
            let succs: Vec<usize> = f.blocks[bi].succs.iter().filter_map(|s| f.idx.get(s).copied()).collect();
            for &s in &succs {
                if let Some(w) = sim.entry[s].clone() {
                    if w != stk {
                        sim.airlock(bi, s, &stk, Some(&w));
                    }
                }
            }
            for &s in &succs {
                if sim.entry[s].is_none() {
                    let e = sim.airlock(bi, s, &stk, None);
                    sim.entry[s] = Some(e);
                    if !done[s] {
                        work.push(s);
                    }
                }
            }
        }
        sim
    }
}

/// The trace side: the mapped IL at FPPeeps entry reduced to the same keys, per output block.
fn trace_key(i: &Ins, raw: &str) -> Option<Key> {
    let op = i.op.as_str();
    // control-word and status-word instructions (fnstcw / fldcw around a truncating fistp) do not touch the stack
    if !op.starts_with('f') || op == "fnstsw" || op == "fwait" || op == "fnstcw" || op == "fldcw" {
        return None;
    }
    let (d, s) = raw.split_once('|').map(|(_, r)| r.split_once("<-").unwrap_or((r, ""))).unwrap_or(("", ""));
    let stn = |t: &str| -> Vec<usize> {
        t.split_whitespace().filter_map(|x| x.strip_prefix("ST(").and_then(|y| y.split(')').next()).and_then(|y| y.parse().ok())).collect()
    };
    let (ds, ss) = (stn(d), stn(s));
    let mem = |t: &str| t.split_whitespace().any(|x| matches!(parse_opd(x), Opd::Mem(_)) || x.starts_with("__real@") || x.starts_with('['));
    let (dm, sm) = (mem(d), mem(s));
    Some(match op {
        "fld" | "fild" => {
            if let Some(k) = ss.first() {
                format!("fld ST({})", k)
            } else {
                format!("{} m", op)
            }
        }
        "fldz" | "fld1" | "fldpi" | "fchs" | "fabs" | "fsqrt" | "fsin" | "fcos" | "frndint" | "f2xm1" | "fcompp" | "fucompp" => op.to_string(),
        "fst" | "fstp" | "fist" | "fistp" => {
            if dm {
                format!("{} m", op)
            } else {
                format!("{} ST({})", op, ds.first().copied().unwrap_or(0))
            }
        }
        "fxch" => format!("fxch ST({})", ss.iter().copied().find(|&k| k != 0).unwrap_or(0)),
        "fcom" | "fcomp" | "fucom" | "fucomp" | "ficom" | "ficomp" => {
            if sm {
                format!("{} m", op)
            } else {
                format!("{} ST({})", op, ss.iter().copied().find(|&k| k != 0).unwrap_or(1))
            }
        }
        _ => {
            if sm {
                format!("{} m", op)
            } else {
                let a = ds.first().copied().unwrap_or(0);
                let b = ss.iter().copied().find(|&k| k != a).unwrap_or(0);
                format!("{} ST({}),ST({})", op, a, b)
            }
        }
    })
}

struct Traced {
    per_block: Vec<Vec<Key>>,           // indexed like Func.blocks (original blocks)
    airlocks: Vec<Vec<Key>>,
}

fn traced(f: &Func, x: &Value) -> Traced {
    let lines: Vec<String> = x["st"].as_array().map(|a| a.iter().filter_map(|s| s.as_str().map(String::from)).collect()).unwrap_or_default();
    // original tuples (instructions and labels) -> their block; the mapper may recycle a freed x87 tuple's address
    // for an airlock instruction, so an output block is owned by the original block its tuples vote for (a
    // non-x87 tuple or a label 10, an x87 tuple that is not freed 2; at least 2)
    let mut tupblk: HashMap<String, usize> = HashMap::new();
    let mut cur: Option<usize> = None;
    for l in x["gf"].as_array().into_iter().flatten().filter_map(|s| s.as_str()) {
        let w: Vec<&str> = l.split_whitespace().collect();
        match w.first() {
            Some(&"B") => cur = w.get(1).and_then(|bb| f.idx.get(*bb).copied()),
            Some(&"L") | Some(&"I") => {
                // tuples the mapper frees (OPPAREN, a GF-to-GF copy that is only a rename) do not vote
                let freed = w.get(2) == Some(&"OPPAREN")
                    || (w.get(2) == Some(&"_fmov") && parse_ins(l).map(|i| !gfs(&i.dsts).is_empty() && !gfs(&i.srcs).is_empty()).unwrap_or(false));
                if let (Some(c), Some(t), false) = (cur, w.get(1), freed) {
                    tupblk.insert(t.to_string(), c);
                }
            }
            _ => {}
        }
    }
    let mut per_block = vec![vec![]; f.blocks.len()];
    let mut airlocks = vec![];
    let mut grp: Vec<Key> = vec![];
    let mut votes: HashMap<usize, i32> = HashMap::new();
    let flush = |grp: &mut Vec<Key>, votes: &mut HashMap<usize, i32>, per_block: &mut Vec<Vec<Key>>, airlocks: &mut Vec<Vec<Key>>| {
        let owner = votes.iter().filter(|(_, &v)| v >= 2).max_by_key(|(_, &v)| v).map(|(&b, _)| b);
        votes.clear();
        let keys: Vec<Key> = grp.drain(..).collect();
        match owner {
            Some(b) => per_block[b].extend(keys),
            None => {
                if !keys.is_empty() {
                    airlocks.push(keys)
                }
            }
        }
    };
    for l in &lines {
        if l.starts_with("B ") {
            flush(&mut grp, &mut votes, &mut per_block, &mut airlocks);
            continue;
        }
        if let Some(t) = l.strip_prefix("L ") {
            if let Some(&b) = tupblk.get(t.trim()) {
                *votes.entry(b).or_default() += 10;
            }
            continue;
        }
        let Some(i) = parse_ins(l) else { continue };
        let k = trace_key(&i, l);
        if let Some(&b) = tupblk.get(&i.tup) {
            *votes.entry(b).or_default() += if k.is_some() { 2 } else { 10 };
        }
        if let Some(k) = k {
            grp.push(k);
        }
        if i.op == "_jcc" || i.op == "jmp" || i.op == "_eret" {
            // a block ends at its branch even where the rebuilt flow graph prints no B line after it
            flush(&mut grp, &mut votes, &mut per_block, &mut airlocks);
        }
    }
    flush(&mut grp, &mut votes, &mut per_block, &mut airlocks);
    Traced { per_block, airlocks }
}

struct CallPred {
    target: String,
    kept_pred: bool,
    kept_trace: Option<bool>,
    why: String,
}

fn calls(x: &Value) -> Vec<CallPred> {
    let mut body: HashMap<String, (i64, i64)> = HashMap::new();
    for c in x["callees"].as_array().into_iter().flatten() {
        if let Some(n) = c["name"].as_str() {
            body.insert(n.to_string(), (c["pushes"].as_i64().unwrap_or(2), c["ret"].as_i64().unwrap_or(0)));
        }
    }
    let mut out = vec![];
    for c in x["calls"].as_array().into_iter().flatten() {
        let target = c["target"].as_str().unwrap_or("").to_string();
        let (kept, why) = match body.get(&target) {
            None if target.is_empty() => (false, "indirect or helper call: no recorded body".to_string()),
            None => (false, "no body generated before this function (external, or generated later: in-class inline)".to_string()),
            Some(&(p, r)) => {
                if p == 0 {
                    (true, "its body pushes no x87 value".to_string())
                } else if p == 1 && r == 5 {
                    (true, "its body pushes one x87 value and returns it (float/double)".to_string())
                } else {
                    (false, format!("its body pushes {} x87 value{}{}", if p >= 2 { "2 or more".to_string() } else { p.to_string() },
                                    if p == 1 { "" } else { "s" }, if p == 1 { " and does not return float/double" } else { "" }))
                }
            }
        };
        let kt = c["kill"]["gf"].as_array().map(|a| a.is_empty());
        out.push(CallPred { target, kept_pred: kept, kept_trace: kt, why });
    }
    out
}

#[allow(dead_code)] // the per-function counts, for callers that sum a corpus
pub struct Totals {
    pub calls: (usize, usize),
    pub ins: (usize, usize),
    pub blocks: (usize, usize),
    pub airlocks: (usize, usize),
    pub fldz: usize,
}

pub fn check_value(x: &Value, verbose: bool, label: &str) -> Totals {
    let f = Func::new(x);
    let cs = calls(x);
    let call_ok = cs.iter().filter(|c| c.kept_trace == Some(c.kept_pred)).count();
    let sim = Sim::run(&f);
    let tr = traced(&f, x);
    let (mut bok, mut bn, mut iok, mut in_) = (0, 0, 0, 0);
    for (bi, b) in f.blocks.iter().enumerate() {
        let (p, t) = (&sim.out[bi], &tr.per_block[bi]);
        if p.is_empty() && t.is_empty() {
            continue;
        }
        bn += 1;
        in_ += t.len().max(p.len());
        iok += p.iter().zip(t.iter()).take_while(|(a, b)| a == b).count();
        if p == t {
            bok += 1;
        } else if verbose {
            println!("  x87 block {} (line {}, entry GF{:?} live in {:02x}, preds {:?}): model {:?}\n                       trace {:?}", b.bb,
                     b.ins.first().map(|i| i.line).unwrap_or(0), sim.entry[bi], f.live_in[bi], b.preds, p, t);
        }
    }
    let mut ta: Vec<Vec<Key>> = tr.airlocks.clone();
    let mut aok = 0;
    for (_, _, a) in &sim.airlocks {
        if let Some(i) = ta.iter().position(|x| x == a) {
            ta.remove(i);
            aok += 1;
        }
    }
    let an = sim.airlocks.len().max(tr.airlocks.len());
    if verbose && (aok < an) {
        println!("  airlocks: model {:?}\n            trace {:?}", sim.airlocks.iter().map(|x| &x.2).collect::<Vec<_>>(), tr.airlocks);
    }
    if verbose {
        for c in &cs {
            println!("  call {}: {} ({}){}", c.target, if c.kept_pred { "x87 values stay" } else { "kills GF0..GF7" }, c.why,
                     match c.kept_trace { Some(k) if k != c.kept_pred => " MISS", _ => "" });
        }
    }
    println!(
        "x87 stack (X87MapGFxToSTx, cl {}) {}: calls {} of {} kill sets predicted; blocks {} of {} mapped exactly \
         ({} of {} x87 instructions in order); airlocks {} of {}; fldz for values live out but not on the stack: {}",
        x["build"].as_str().unwrap_or("?"), label, call_ok, cs.len(), bok, bn, iok, in_, aok, an, sim.fldz_live_out.len()
    );
    if sim.gaps > 0 {
        println!("  model gaps: {} GF sources were not on the model's stack (an unmodelled instruction before them)", sim.gaps);
    }
    Totals { calls: (call_ok, cs.len()), ins: (iok, in_), blocks: (bok, bn), airlocks: (aok, an), fldz: sim.fldz_live_out.len() }
}

pub fn cmd_check(path: &str, verbose: bool) {
    let v: Value = match std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str(&t).ok()) {
        Some(v) => v,
        None => return,
    };
    if v.get("x87").is_none() {
        return;
    }
    let name = v["func"].as_str().unwrap_or("?").to_string();
    check_value(&v["x87"], verbose, &name);
}

/// `rasim need SNAP stack:V` on 14.00 / 15.00: what keeps V on the stack across blocks and calls, or reloads it.
pub fn cmd_need_stack(path: &str, var: &str) -> bool {
    let v: Value = match std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str(&t).ok()) {
        Some(v) => v,
        None => return false,
    };
    let Some(x) = v.get("x87") else { return false };
    let f = Func::new(x);
    let sim = Sim::run(&f);
    // names: the GF form keeps the symbol's leading '_', the regs snapshot drops one; compare without any
    let norm = |n: &str| n.trim_start_matches('_').to_string();
    let vn = norm(var);
    let want = |n: &str| norm(n) == vn;
    let gfs_of: Vec<u8> = f.names.iter().filter(|(_, ns)| ns.iter().any(|n| want(n))).map(|(k, _)| *k).collect();
    // Color's side (class 1): V's webs in every Spillcosts pass, its pops, and the pass whose round split it
    let pops: Vec<&Value> = v["pops"].as_array().into_iter().flatten().filter(|p| p["cls"].as_i64() == Some(1)).collect();
    let mine: Vec<&&Value> = pops.iter().filter(|p| p["name"].as_str().map(want).unwrap_or(false)).collect();
    println!("{}: {} x87 piece{} coloured by Color", var, mine.len(), if mine.len() == 1 { "" } else { "s" });
    for p in &mine {
        println!("  pop {} (pass {}): prio {} benefit {} defOrd {} -> {}", p["pop"], p["npass"], p["prio"], p["ben"], p["defOrd"],
                 p["reg"].as_str().map(String::from).unwrap_or_else(|| p["reg"].to_string()));
    }
    let mut blocker: Option<String> = None;
    let mut snapname = var.to_string();
    let mut bpass = 0i64;
    for ps in v["passes"].as_array().into_iter().flatten().filter(|ps| ps["cls"].as_i64() == Some(1)) {
        let n = ps["n"].as_i64().unwrap_or(0);
        let grs = ps["grs"].as_object();
        let Some(grs) = grs else { continue };
        let ids: Vec<&String> = grs.iter().filter(|(_, g)| g["name"].as_str().map(want).unwrap_or(false)).map(|(k, _)| k).collect();
        if ids.is_empty() {
            continue;
        }
        if let Some(nm) = grs[ids[0].as_str()]["name"].as_str() {
            // name#record: a name may hold '@' (__real@...), which rasim's piece syntax would read as name@k
            if blocker.is_none() {
                snapname = format!("{}#{}", nm, ids[0]);
            }
        }
        let split: Vec<&str> = ps["split_removed"].as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
        let coloured: Vec<&&Value> = pops.iter().filter(|p| p["npass"].as_i64() == Some(n)).collect();
        let popped = coloured.iter().any(|p| p["name"].as_str().map(want).unwrap_or(false));
        let was_split = ids.iter().any(|g| split.contains(&g.as_str()));
        if was_split {
            let names: Vec<String> = coloured.iter().map(|p| format!("{} ({}, defOrd {})", p["name"].as_str().unwrap_or("?"), p["prio"], p["defOrd"])).collect();
            println!("  pass {}: split before its pop (RemoveSplitGlobregs): the x87 webs coloured in that pass were {}", n, names.join(", "));
            println!("    with K = 8 slots (coloured values live + the temporaries of each instruction), the colourings above filled \
                      the stack where {} is live, and InsertSplitPoints cut it there (x87.md section 2); its pieces are re-costed \
                      in the next pass", var);
            if let Some(c) = coloured.iter().rev().find(|p| p["npass"].as_i64() == Some(n)) {
                if blocker.is_none() {
                    blocker = c["gr"].as_str().zip(c["name"].as_str()).map(|(g, nm)| format!("{}#{}", nm, g));
                    bpass = n;
                }
            }
        } else if !popped {
            println!("  pass {}: costed, never popped as a whole (spilled, or benefit <= 0: a web needs 2 or more references)", n);
        }
    }
    if let Some(c) = &blocker {
        println!("  blocker: {} (the last x87 web coloured before the split); popped before it, {} would get a GF register \
                  (a web that was never popped has no defOrd in the snapshot: rasim shows def 0; the real one is its \
                  enterload's place among the block's enterloads, ascending record number, regalloc.md 3.6.9):", c, var);
        if let Ok(exe) = std::env::current_exe() {
            if let Ok(o) = std::process::Command::new(exe).args(["need", path, "--pass", &bpass.to_string(), &format!("{}>{}", snapname, c)]).output() {
                for l in String::from_utf8_lossy(&o.stdout).lines().take(14) {
                    println!("    | {}", l);
                }
            }
        }
    }
    if gfs_of.is_empty() {
        println!("  not in GF form at the mapper: V is memory-only (its uses are memory operands)");
    }
    for &g in &gfs_of {
        let on: Vec<String> = f.blocks.iter().enumerate().filter(|(i, _)| f.live_in[*i] & (1 << g) != 0)
            .map(|(i, b)| format!("{} (line {}, ST({}))", b.bb, b.ins.first().map(|i| i.line).unwrap_or(0),
                 sim.entry[i].as_ref().and_then(|e| st(e, g)).map(|k| k as i64).unwrap_or(-1))).collect();
        println!("  GF{}: on the x87 stack at the entry of {} block{}{}", g, on.len(), if on.len() == 1 { "" } else { "s" },
                 if on.is_empty() { String::new() } else { format!(": {}", on.join(", ")) });
        for (bi, gg) in &sim.fldz_live_out {
            if *gg == g {
                println!("    fldz at the end of block {}: GF{} is live out there but no path defines it (AppendFldZeroToBlock)", f.blocks[*bi].bb, g);
            }
        }
        for (a, b, ks) in &sim.airlocks {
            if ks.iter().any(|k| k.starts_with("fxch")) && (f.live_in[*b] & (1 << g) != 0) {
                println!("    airlock {} -> {}: {:?} (the layouts at the edge differ; the first-mapped predecessor fixed {}'s)",
                         f.blocks[*a].bb, f.blocks[*b].bb, ks, f.blocks[*b].bb);
            }
        }
    }
    for c in calls(x) {
        println!("  call {}: {} - {}", c.target, if c.kept_pred { "x87 values stay on the stack across it" } else {
            "kills GF0..GF7: a value live across it is split, stored before and reloaded after" }, c.why);
        if !c.kept_pred {
            println!("    to keep values across it: give the callee a body in this unit that pushes no x87 value, or one and \
                      returns float/double, not inline-in-class (generated before this caller)");
        }
    }
    true
}
