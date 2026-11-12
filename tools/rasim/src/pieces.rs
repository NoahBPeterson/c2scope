//! `rasim pieces SNAP [-v] [--chain]`: what the pieces of a split round look like (docs/c2/regalloc.md 3.6.11).
//!
//! Input: a snapshot exported with `c2trace.py regs ... --pieces --export SNAP`, which holds the function's IL as
//! GLOBREG numbers (the '^' dump, key `gdumps`) at the entry of every ColProcessSplitGlobregs call.  This module runs
//! the split round as c2's code does and costs the pieces as Spillcosts does:
//!
//!   ColProcessSplitGlobregs 10b11b20   the block list (blocks holding a split web), 32 parents per batch
//!   CreateGenAndKill        10b11f30   per block, backwards: gen / kill / downward-exposed / memory sets; split
//!                                      points keep only this round's webs (and are dropped when none is left)
//!   ComputeNewLiveness      10b122d0   backward liveness, then forward "reached and live", then (when a def has a
//!                                      memory home) backward "memory needed below"
//!   NewLiveRanges           10b12480   one component per connected region; records for the roots, newest first
//!   FlagSplitAppears        10b129a0   per block, backwards: exitsaves after defs whose value a split point or a
//!                                      memory read needs below; dead enterloads deleted, dead defs into memory;
//!                                      operand flags 0x40 (in the block's bottom piece) and 0x10 (last use)
//!   RewriteSplitGlobregs    10b12d20   per block, forwards: airlock enterloads (GrgAirlockEnterloads 10b59320),
//!                                      operands renamed to the live piece; a use no piece reaches gets a reload
//!                                      piece (or reads memory when it is the last use), a def a new piece
//!   Spillcosts              10b10420   the pieces' references, cost, benefit, P_B, priority, nEnter, blocks
//!
//! `rasim pieces SNAP` runs each traced round from that round's own input (the dump) and compares the pieces with
//! what c2 made (record numbers in order, parent, NewLiveRanges / use / def, per-block terms of the next Spillcosts
//! pass, nrefs, benefit, last def, priority, nEnter).  `--chain` instead carries the model's own IL from the first
//! round to the next (taking from the trace only what happens during the pops in between: colours, spills and the
//! split points made at pops), so the pieces of every later round come from the first round's input.
//! Addresses are 14.00's; 13.10 and 15.00 differ only in opcode numbers and layouts the dump hides (port docs).

use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet, HashMap};

type Set = BTreeSet<u32>;

#[derive(Deserialize)]
struct GDump {
    npass: u32,
    cls: Option<i64>,
    after_pop: usize,
    lines: Vec<String>,
}
#[derive(Deserialize)]
struct QSnap {
    #[serde(default)]
    gdumps: Vec<GDump>,
    #[serde(default)]
    decisions: Option<Vec<QDec>>,
    #[serde(default)]
    passes: Vec<QPass>,
    #[serde(default)]
    split_rounds: Vec<QSplit>,
    #[serde(default)]
    inserts: Vec<QIns>,
    #[serde(default)]
    pops: Vec<QPop>,
    #[serde(default)]
    c2: Option<QBuild>,
    func: String,
    #[serde(default)]
    refcosts: Vec<(u32, String, u32, i64, String)>,
}
#[derive(Deserialize)]
struct QBuild {
    #[serde(default)]
    build: String,
}
#[derive(Deserialize)]
struct QPop {
    npass: u32,
    pop: usize,
    gr: String,
    reg: Option<String>,
    x: Option<QX>,
}
#[derive(Deserialize, Clone)]
struct QX {
    fl5: u32,
    fl6: u32,
    nrefs: Option<i64>,
    single: bool,
    nenter: Option<i64>,
    prio: Option<i64>,
    speed: Option<i64>,
}
#[derive(Deserialize)]
struct QDec {
    npass: u32,
    after_pop: usize,
    t: String,
    gr: String,
    nrefs: Option<i64>,
    ben: Option<i64>,
    lastop: Option<i64>,
    site: Option<String>,
    num: Option<u64>,
    pnum: Option<u64>,
}
#[derive(Deserialize)]
struct QPass {
    n: u32,
    cls: Option<i64>,
    blocks: Vec<QBlock>,
    grs: BTreeMap<String, QGr>,
}
#[derive(Deserialize)]
struct QBlock {
    b: u32,
    w: i64,
    #[serde(rename = "P")]
    p: i64,
    bb: Option<String>,
}
#[derive(Deserialize)]
struct QGr {
    terms: Vec<QTerm>,
}
#[derive(Deserialize)]
struct QTerm {
    b: u32,
    cost: Option<i64>,
    delta: i64,
}
#[derive(Deserialize)]
struct QSplit {
    npass: u32,
    removed: Vec<String>,
}
#[derive(Deserialize)]
struct QIns {
    npass: u32,
    after_pop: usize,
    gr: Option<String>,
    num: Option<serde_json::Value>,
    avail: Option<Vec<u32>>,
    ben: Option<i64>,
    deferred: Option<bool>,
    x: Option<QX>,
}

// ------------------------------------------------------------------------------------------------ the IL

/// One operand (a leaf of a tuple's dst or src list), as the '^' dump prints it.
#[derive(Clone, Debug, PartialEq)]
pub enum Lf {
    /// REG leaf of a GLOBREG: n = the record at leaf+0x1c, sn = its self (+8; SubsumeGlobregs), ty = type word +0xa,
    /// f10 = +0x10 (a def with a memory home to store to), f14 = +0x14 (low nibble: the src a two-address dst is tied
    /// to, 1-based), f15 = +0x15 (0x10 last use, 0x40 in the block's bottom piece, 0x80 airlock enterload / cost),
    /// eq = the leaf's type is its symbol's (CmdSpillSingleDef's size test)
    G { n: u32, sn: u32, ty: u32, f10: u32, f14: u8, f15: u8, eq: bool },
    /// REG leaf of a physical register (0 = a temporary register, not a globreg)
    R { r: u32, ty: u32 },
    /// memory operand (base / index registers are also in the operand list): leaf kind 5 or 6, nz = it has a
    /// symbol or a displacement (CmdSubCost's test)
    M { ty: u32, k: u8, nz: bool },
    /// SYM leaf; f10 = the GLOBREG number of its home (FlagSplitAppears: a memory read of a split web)
    S { f10: u32, ty: u32 },
    K,
    O,
}
impl Lf {
    fn ty(&self) -> u32 {
        match self {
            Lf::G { ty, .. } | Lf::R { ty, .. } | Lf::M { ty, .. } | Lf::S { ty, .. } => *ty,
            _ => 0,
        }
    }
    fn is_mem(&self) -> bool {
        matches!(self, Lf::M { .. } | Lf::S { .. })
    }
}

#[derive(Clone, Debug)]
pub enum It {
    /// an instruction: opcode, tuple kind, tuple+0x1c, dsts, srcs
    I { tup: u32, op: u32, kind: u8, f1c: u32, d: Vec<Lf>, s: Vec<Lf> },
    /// a split point (opcode 0x2f6, kind 0x1c): the webs it splits; mask = this batch's bits (CreateGenAndKill)
    Sp { tup: u32, webs: Vec<u32>, done: bool, mask: u32 },
    L(u32),
    P(u32, u32, u8),
}

#[derive(Clone, Debug)]
pub struct Blk {
    pub tup: u32,
    pub bb: u32,
    pub lv: u32,
    pub fl: u32,
    pub p: Vec<u32>,
    pub s: Vec<u32>,
    pub li: Set,
    pub lo: Set,
    pub pr: Set,
    pub it: Vec<It>,
    /// made by the model (an airlock block): the trace's pointer is not known
    pub synth: bool,
    /// the block's loop (BB+0x16c; 13.10 BB+0x9c), a key of Fun::loops
    pub lp: Option<u32>,
}

#[derive(Clone, Debug)]
pub struct Rec {
    pub n: u32,
    pub ptr: u32,
    pub sn: u32,
    pub w4: u32, // +4: kind byte, fl5, fl6, f07
    pub col: u32,
    pub fb: u32,
    pub lb: u32,
    pub ne: i64,
    pub prio: i64,
    pub ben: i64,
    pub nrefs: i64,
    pub def: u32,
    pub sk: u32,
    pub skey: u32,
    pub avail: Set,
    /// an immediate candidate: its constant's leaf kind and value (sym+0x28)
    pub kval: Option<(u32, i64)>,
    /// the variable's index in loop reference sets (sym+0x34 -> [0])
    pub xidx: Option<u32>,
    // model-only
    pub pieces: Set, // a split parent: its pieces (parent+0x20 during the round)
    pub parent: u32,
    pub site: &'static str,
    pub lastop: Option<u32>,
}
impl Rec {
    fn fl5(&self) -> u32 {
        self.w4 >> 8 & 0xff
    }
    fn imm(&self) -> bool {
        self.sk == 0xd
    }
}

/// A loop record: its enclosing loop (+8), header block (+0x14), exit blocks (+0x24 list) and the variables it
/// references (+0x38: an exitsave in the loop costs only for one of them, Spillcosts 10b108e1..10b10913)
#[derive(Clone, Debug)]
pub struct Loop {
    pub up: u32,
    pub head: u32,
    pub exits: Vec<u32>,
    pub refs: Set,
}

#[derive(Clone)]
pub struct Fun {
    pub loops: BTreeMap<u32, Loop>,
    pub b: Vec<Blk>,
    pub recs: BTreeMap<u32, Rec>,
    pub new: Vec<Set>,
    next_tup: u32,
    next_bb: u32,
}

fn hx(s: &str) -> u32 {
    u32::from_str_radix(s, 16).unwrap_or(0)
}
fn set_of(s: &str) -> Set {
    s.split(',').filter(|x| !x.is_empty()).map(hx).collect()
}
fn list_of(s: &str) -> Vec<u32> {
    s.split(',').filter(|x| !x.is_empty()).map(hx).collect()
}

fn leaf(t: &str) -> Lf {
    // g<n>[=<sn>]/ty/f10/f14/f15/eq | r<n>/ty/f15 | r?/ty | m(..)/ty | s<f10>/ty | K | .<k> | -
    if let Some(r) = t.strip_prefix('g') {
        let f: Vec<&str> = r.split('/').collect();
        let (n, sn) = match f[0].split_once('=') {
            Some((a, b)) => (hx(a), hx(b)),
            None => (hx(f[0]), hx(f[0])),
        };
        let g = |i: usize| f.get(i).map(|x| hx(x)).unwrap_or(0);
        return Lf::G { n, sn, ty: g(1), f10: g(2), f14: g(3) as u8, f15: g(4) as u8, eq: f.get(5) == Some(&"1") };
    }
    if let Some(r) = t.strip_prefix('r') {
        let f: Vec<&str> = r.split('/').collect();
        let reg = if f[0] == "?" { 0 } else { f[0].parse().unwrap_or(0) };
        return Lf::R { r: reg, ty: f.get(1).map(|x| hx(x)).unwrap_or(0) };
    }
    if t.starts_with('m') && t[1..].contains('(') {
        let k = t[1..t.find('(').unwrap()].parse().unwrap_or(6);
        let tail = t.rsplit_once(")/").map(|x| x.1).unwrap_or("0");
        let (ty, nz) = tail.split_once('/').unwrap_or((tail, "1"));
        return Lf::M { ty: hx(ty), k, nz: nz != "0" };
    }
    if let Some(r) = t.strip_prefix('s') {
        let (a, b) = r.split_once('/').unwrap_or((r, "0"));
        return Lf::S { f10: hx(a), ty: hx(b) };
    }
    if t == "K" {
        return Lf::K;
    }
    Lf::O
}

pub fn parse(lines: &[String]) -> Fun {
    let mut f = Fun { loops: BTreeMap::new(), b: vec![], recs: BTreeMap::new(), new: vec![Set::new(); 7], next_tup: 0xf000_0000, next_bb: 0xe000_0000 };
    for ln in lines {
        let w: Vec<&str> = ln.split(' ').collect();
        match w.first().copied() {
            Some("B") if w.len() >= 5 => {
                let mut b = Blk {
                    tup: hx(w[1]),
                    bb: hx(w[2]),
                    lv: w[3].parse().unwrap_or(0),
                    fl: hx(w[4]),
                    p: vec![],
                    s: vec![],
                    li: Set::new(),
                    lo: Set::new(),
                    pr: Set::new(),
                    it: vec![],
                    synth: false,
                    lp: None,
                };
                for x in &w[5..] {
                    if let Some(v) = x.strip_prefix("p=") {
                        b.p = list_of(v);
                    } else if let Some(v) = x.strip_prefix("s=") {
                        b.s = list_of(v);
                    } else if let Some(v) = x.strip_prefix("i=") {
                        b.li = set_of(v);
                    } else if let Some(v) = x.strip_prefix("o=") {
                        b.lo = set_of(v);
                    } else if let Some(v) = x.strip_prefix("n=") {
                        b.pr = set_of(v);
                    } else if let Some(v) = x.strip_prefix("L=") {
                        b.lp = Some(hx(v));
                    }
                }
                f.b.push(b);
            }
            Some("I") if w.len() >= 6 => {
                let Some(b) = f.b.last_mut() else { continue };
                // I tup op kind f9 f1c | dsts | srcs
                let bars: Vec<usize> = w.iter().enumerate().filter(|x| *x.1 == "|").map(|x| x.0).collect();
                if bars.len() < 2 {
                    continue;
                }
                let d = w[bars[0] + 1..bars[1]].iter().filter(|x| !x.is_empty()).map(|x| leaf(x)).collect();
                let s = w[bars[1] + 1..].iter().filter(|x| !x.is_empty()).map(|x| leaf(x)).collect();
                b.it.push(It::I {
                    tup: hx(w[1]),
                    op: hx(w[2]),
                    kind: w[3].parse().unwrap_or(0),
                    f1c: hx(w[5]),
                    d,
                    s,
                });
            }
            Some("S") if w.len() >= 3 => {
                let Some(b) = f.b.last_mut() else { continue };
                b.it.push(It::Sp { tup: hx(w[1]), webs: w.get(3).map(|x| list_of(x)).unwrap_or_default(), done: w[2] != "0", mask: 0 });
            }
            Some("L") if w.len() >= 2 => {
                if let Some(b) = f.b.last_mut() {
                    b.it.push(It::L(hx(w[1])));
                }
            }
            Some("P") if w.len() >= 4 => {
                if let Some(b) = f.b.last_mut() {
                    b.it.push(It::P(hx(w[1]), hx(w[2]), w[3].parse().unwrap_or(0)));
                }
            }
            Some("R") if w.len() >= 15 => {
                let d = |i: usize| w[i].parse::<i64>().unwrap_or(0);
                let r = Rec {
                    n: hx(w[1]),
                    ptr: hx(w[2]),
                    sn: hx(w[3]),
                    w4: hx(w[4]),
                    col: d(5) as u32,
                    nrefs: d(6),
                    fb: hx(w[7]),
                    lb: hx(w[8]),
                    ne: d(9),
                    prio: d(10),
                    ben: d(11),
                    def: d(12) as u32,
                    sk: d(13) as u32,
                    skey: d(14) as u32,
                    avail: w.get(15).and_then(|x| x.strip_prefix("a=")).map(set_of).unwrap_or_default(),
                    xidx: w.iter().find_map(|x| x.strip_prefix("x=")).map(hx),
                    kval: w.iter().find_map(|x| x.strip_prefix("k=")).and_then(|x| x.split_once(',')).map(|(a, b)| (a.parse().unwrap_or(0), b.parse().unwrap_or(0))),
                    pieces: Set::new(),
                    parent: 0,
                    site: "",
                    lastop: None,
                };
                f.recs.insert(r.n, r);
            }
            Some("O") if w.len() >= 4 => {
                let mut l = Loop { up: hx(w[2]), head: hx(w[3]), exits: vec![], refs: Set::new() };
                for x in &w[4..] {
                    if let Some(v) = x.strip_prefix("e=") {
                        l.exits = list_of(v);
                    } else if let Some(v) = x.strip_prefix("r=") {
                        l.refs = set_of(v);
                    }
                }
                f.loops.insert(hx(w[1]), l);
            }
            Some("N") if w.len() >= 2 => {
                let c: usize = w[1].parse().unwrap_or(0);
                if c < 7 {
                    f.new[c] = w.get(2).map(|x| set_of(x)).unwrap_or_default();
                }
            }
            _ => {}
        }
    }
    f
}

// ------------------------------------------------------------------------------------------------ build data

/// What differs between builds and is not in the dump.
#[derive(Clone, Copy)]
pub struct Bld {
    pub enterload: u32,
    pub exitsave: u32,
    regclass: [i32; 16],
    /// _RegSym (GLOBREG.color points at _RegSym + 0x60·reg): FlagSplitAppears / CreateGenAndKill OR a coloured
    /// web's +0x10 into a bit mask when a memory read names its home
    regsym: u32,
    x87nib: u32,
}
pub fn bld(name: &str) -> Bld {
    if name.starts_with("13.10") {
        Bld {
            enterload: 0x26a,
            exitsave: 0x26b,
            regclass: [-1, 0, 0, 0, 1, 0, -1, 0, -1, -1, -1, 5, 6, 6, 6, -1],
            regsym: if name.ends_with("3052") { 0x108a97a0 } else { 0x108a97a4 },
            x87nib: 0x4000,
        }
    } else if name.starts_with("15.00") {
        Bld {
            enterload: 0x2e0,
            exitsave: 0x2e1,
            regclass: [-1, 0, 0, 0, 0, 1, 0, -1, -1, -1, -1, -1, 5, 6, 6, 6],
            regsym: 0x51250640,
            x87nib: 0x5000,
        }
    } else {
        Bld {
            enterload: 0x295,
            exitsave: 0x296,
            regclass: [-1, 0, 0, 0, 0, 1, 0, -1, -1, -1, -1, -1, 5, 6, 6, 6],
            regsym: 0x10d09f70,
            x87nib: 0x5000,
        }
    }
}
impl Bld {
    fn class(&self, ty: u32) -> i32 {
        self.regclass[(ty >> 12 & 0xf) as usize]
    }
}

// ------------------------------------------------------------------------------------------------ the split round

#[derive(Clone, Debug)]
pub struct Piece {
    pub num: u32,
    pub parent: u32,
    pub site: &'static str, // nlr / use / def
}

#[derive(Default, Clone)]
struct Bs {
    gen: u32,
    kill: u32,
    de: u32,
    m58: u32,
    m5c: u32,
    i38: u32,
    o3c: u32,
    li: u32,
    lo: u32,
    top: Option<usize>,
    bot: Option<usize>,
}

struct Comp {
    up: Option<usize>,
    num: u32,
}
fn root(c: &[Comp], mut x: usize) -> usize {
    while let Some(u) = c[x].up {
        x = u;
    }
    x
}

impl Fun {
    fn idx(&self) -> HashMap<u32, usize> {
        self.b.iter().enumerate().map(|(i, b)| (b.bb, i)).collect()
    }
    fn tup(&mut self) -> u32 {
        self.next_tup += 1;
        self.next_tup
    }
    /// GrgNewGlobreg 10b5b7b0 for a piece of `parent`: the number comes from `num` (the free list)
    fn new_rec(&mut self, parent: u32, site: &'static str, num: &mut dyn FnMut() -> u32, cls: usize) -> u32 {
        let n = num();
        let p = self.recs.get(&parent).cloned().expect("parent record");
        let r = Rec {
            n,
            ptr: 0,
            sn: n,
            w4: (p.w4 & 0xff) | 1 << 8,
            col: 0,
            fb: 0,
            lb: 0,
            ne: 0,
            prio: 0,
            ben: 0,
            nrefs: 0,
            def: p.def,
            sk: p.sk,
            skey: p.skey,
            avail: Set::new(),
            kval: p.kval,
            xidx: p.xidx,
            pieces: Set::new(),
            parent,
            site,
            lastop: None,
        };
        self.recs.insert(n, r);
        self.recs.get_mut(&parent).unwrap().pieces.insert(n);
        self.new[cls].insert(n);
        n
    }
}

/// The value FlagSplitAppears / CreateGenAndKill read at GLOBREG+0x10 of the web a memory read names: the batch
/// bit of a split parent, 0 for an uncoloured web, and the colour's REGSYM address for a coloured one (c2 ORs that
/// pointer into a bit mask; it matters only when a memory read names a coloured web's home).
fn f10val(recs: &BTreeMap<u32, Rec>, bit: &HashMap<u32, u32>, n: u32, b: &Bld) -> u32 {
    if let Some(&m) = bit.get(&n) {
        return m;
    }
    match recs.get(&n) {
        Some(r) if r.col != 0 => b.regsym.wrapping_add(0x60 * r.col),
        _ => 0,
    }
}

/// One split round: ColProcessSplitGlobregs 10b11b20 with the webs `split` (record numbers).  `num` hands out the
/// record numbers (GrgNewGlobreg).  Returns the pieces in the order their records were taken.
pub fn split_round(f: &mut Fun, split: &Set, cls: usize, num: &mut dyn FnMut() -> u32, b: &Bld, notes: &mut Vec<String>) -> Vec<Piece> {
    let parents: Vec<u32> = split.iter().copied().filter(|n| f.recs.contains_key(n)).collect();
    // the block list: blocks whose present set holds a split web
    let inl: Vec<usize> = (0..f.b.len()).filter(|&i| f.b[i].pr.iter().any(|n| split.contains(n))).collect();
    let inset: BTreeSet<usize> = inl.iter().copied().collect();
    for i in 0..f.b.len() {
        if inset.contains(&i) {
            let blk = &mut f.b[i];
            blk.li.retain(|n| !split.contains(n));
            blk.lo.retain(|n| !split.contains(n));
        } else if f.b[i].fl & 0x40000 != 0 {
            f.b[i].it.retain(|x| !matches!(x, It::Sp { .. }));
        }
    }
    for i in 0..f.b.len() {
        f.b[i].fl &= !0x40000;
    }
    let idx = f.idx();
    let mut pieces = vec![];
    let mut st: Vec<Bs> = vec![Bs::default(); f.b.len()];
    for batch in parents.chunks(32) {
        let bit: HashMap<u32, u32> = batch.iter().enumerate().map(|(k, &n)| (n, 1u32 << k)).collect();
        for s in st.iter_mut() {
            *s = Bs::default();
        }
        // CreateGenAndKill 10b11f30
        let mut any10 = false;
        for &i in &inl {
            let pr = f.b[i].pr.clone();
            let (mut gen, mut kill, mut de, mut m58, mut m5c) = (0u32, 0u32, 0u32, 0u32, 0u32);
            let mut drop = vec![];
            for (k, it) in f.b[i].it.iter_mut().enumerate().rev() {
                match it {
                    It::Sp { webs, done, mask, .. } => {
                        if !*done {
                            webs.retain(|n| split.contains(n) && pr.contains(n));
                            if webs.is_empty() {
                                drop.push(k);
                                continue;
                            }
                            *done = true;
                        }
                        let m = webs.iter().map(|n| bit.get(n).copied().unwrap_or(0)).fold(0, |a, x| a | x);
                        kill |= m;
                        gen &= !m;
                        m58 |= m;
                        *mask = m;
                    }
                    It::I { d, s, .. } => {
                        for l in d.iter() {
                            if let Lf::G { n, f14, f10, .. } = l {
                                if let Some(&u) = bit.get(n) {
                                    if kill & u == 0 && f14 & 0xf == 0 {
                                        de |= u;
                                        kill |= u;
                                    }
                                    m5c |= u;
                                    gen &= !u;
                                    m58 &= !u;
                                    any10 |= *f10 != 0;
                                }
                            }
                        }
                        for l in s.iter() {
                            match l {
                                Lf::G { n, f15, .. } => {
                                    if let Some(&u) = bit.get(n) {
                                        if kill & u == 0 {
                                            de |= u;
                                        }
                                        gen |= u;
                                        if f15 & 0x10 != 0 {
                                            m5c |= u;
                                            m58 &= !u;
                                        }
                                    }
                                }
                                Lf::S { f10, .. } if *f10 != 0 => {
                                    m58 |= f10val(&f.recs, &bit, *f10, b);
                                }
                                _ => {}
                            }
                        }
                    }
                    _ => {}
                }
            }
            for k in drop {
                f.b[i].it.remove(k);
            }
            st[i] = Bs { gen, kill, de, m58, m5c, i38: gen, o3c: 0, ..Bs::default() };
        }
        // ComputeNewLiveness 10b122d0
        loop {
            let mut ch = 0u32;
            for &i in inl.iter().rev() {
                let out = f.b[i].s.iter().filter_map(|x| idx.get(x)).fold(0, |a, &j| a | st[j].i38);
                if out != st[i].o3c {
                    let old = st[i].i38;
                    let inn = (!st[i].kill & out) | st[i].gen;
                    st[i].o3c = out;
                    st[i].i38 = inn;
                    ch |= inn ^ old;
                }
            }
            if ch == 0 {
                break;
            }
        }
        for &i in &inl {
            let s = &mut st[i];
            s.li = s.i38;
            s.i38 = 0;
            s.lo = s.o3c;
            s.o3c = s.de & s.o3c;
        }
        loop {
            let mut ch = 0u32;
            for &i in &inl {
                let inn = f.b[i].p.iter().filter_map(|x| idx.get(x)).fold(0, |a, &j| a | st[j].o3c) & st[i].li;
                if inn != st[i].i38 {
                    let old = st[i].o3c;
                    st[i].i38 = inn;
                    let o = ((!st[i].kill & inn) | st[i].de) & st[i].lo;
                    st[i].o3c = o;
                    ch |= o ^ old;
                }
            }
            if ch == 0 {
                break;
            }
        }
        for &i in &inl {
            let s = &mut st[i];
            s.li = s.i38;
            s.lo = s.o3c;
            s.o3c = 0;
            if any10 {
                s.i38 = s.m58;
            } else {
                s.i38 = 0;
                s.m58 = 0;
            }
        }
        if any10 {
            loop {
                let mut ch = 0u32;
                for &i in inl.iter().rev() {
                    let out = f.b[i].s.iter().filter_map(|x| idx.get(x)).fold(0, |a, &j| a | st[j].i38);
                    if out != st[i].o3c {
                        let old = st[i].i38;
                        let inn = (!st[i].m5c & out) | st[i].m58;
                        st[i].o3c = out;
                        st[i].i38 = inn;
                        ch |= inn ^ old;
                    }
                }
                if ch == 0 {
                    break;
                }
            }
            for &i in &inl {
                st[i].m58 = st[i].o3c;
            }
        }
        for s in st.iter_mut() {
            s.i38 = 0;
            s.o3c = 0;
        }
        // NewLiveRanges 10b12480
        for &g in batch {
            let m = bit[&g];
            let (fb, lb) = {
                let r = &f.recs[&g];
                (idx.get(&r.fb).copied(), idx.get(&r.lb).copied())
            };
            let (Some(fb), Some(lb)) = (fb, lb) else {
                notes.push(format!("web {:x}: its first / last block is not in the dump", g));
                continue;
            };
            let mut comps: Vec<Comp> = vec![];
            let mut order: Vec<usize> = vec![]; // creation order
            let mut newc = |comps: &mut Vec<Comp>, order: &mut Vec<usize>| {
                comps.push(Comp { up: None, num: 0 });
                order.push(comps.len() - 1);
                comps.len() - 1
            };
            for i in fb..=lb.max(fb) {
                if !inset.contains(&i) {
                    continue;
                }
                let inn = st[i].li & m != 0;
                let out = st[i].lo & m != 0;
                let killed = st[i].kill & m != 0;
                let mut top: Option<usize> = None;
                let mut bot: Option<usize> = None;
                if inn {
                    for p in &f.b[i].p {
                        let Some(&j) = idx.get(p) else { continue };
                        if let Some(c) = st[j].bot {
                            match top {
                                None => top = Some(c),
                                Some(t) if t != c => {
                                    let (rt, rc) = (root(&comps, t), root(&comps, c));
                                    if rt != rc {
                                        comps[rc].up = Some(rt);
                                    }
                                }
                                _ => {}
                            }
                        }
                    }
                    if !out || killed {
                        if top.is_none() {
                            top = Some(newc(&mut comps, &mut order));
                        }
                    } else {
                        for s in &f.b[i].s {
                            let Some(&j) = idx.get(s) else { continue };
                            if let Some(c) = st[j].top {
                                match top {
                                    None => top = Some(c),
                                    Some(t) if t != c => {
                                        let (rt, rc) = (root(&comps, t), root(&comps, c));
                                        if rt != rc {
                                            comps[rc].up = Some(rt);
                                        }
                                    }
                                    _ => {}
                                }
                            }
                        }
                        if top.is_none() {
                            top = Some(newc(&mut comps, &mut order));
                        }
                        bot = top;
                    }
                    st[i].top = top;
                }
                if out && (!inn || killed) {
                    for s in &f.b[i].s {
                        let Some(&j) = idx.get(s) else { continue };
                        if let Some(c) = st[j].top {
                            match bot {
                                None => bot = Some(c),
                                Some(t) if t != c => {
                                    let (rt, rc) = (root(&comps, t), root(&comps, c));
                                    if rt != rc {
                                        comps[rc].up = Some(rt);
                                    }
                                }
                                _ => {}
                            }
                        }
                    }
                    if bot.is_none() {
                        bot = Some(newc(&mut comps, &mut order));
                    }
                }
                st[i].bot = bot;
            }
            // records for the roots, newest component first
            for &c in order.iter().rev() {
                if comps[c].up.is_none() {
                    let n = f.new_rec(g, "nlr", num, cls);
                    comps[c].num = n;
                    pieces.push(Piece { num: n, parent: g, site: "nlr" });
                }
            }
            for c in 0..comps.len() {
                let r = root(&comps, c);
                comps[c].num = comps[r].num;
            }
            for i in fb..=lb.max(fb) {
                if let Some(c) = st[i].top.take() {
                    let n = comps[c].num;
                    f.b[i].li.insert(n);
                    f.b[i].pr.insert(n);
                }
                if let Some(c) = st[i].bot.take() {
                    let n = comps[c].num;
                    f.b[i].lo.insert(n);
                    f.b[i].pr.insert(n);
                }
            }
        }
        // FlagSplitAppears 10b129a0
        for &i in &inl {
            let mut m1c = st[i].m58;
            let mut live = st[i].lo;
            let mut ncb = !live;
            let items = std::mem::take(&mut f.b[i].it);
            let mut out: Vec<It> = Vec::with_capacity(items.len() + 4);
            for it in items.into_iter().rev() {
                match it {
                    It::Sp { tup, webs, done, mask } => {
                        ncb |= mask;
                        live &= !mask;
                        m1c |= mask;
                        out.push(It::Sp { tup, webs, done, mask });
                    }
                    It::I { tup, op, kind, f1c, mut d, mut s } => {
                        let mut exits: Vec<It> = vec![];
                        let mut deleted = false;
                        for l in d.iter_mut() {
                            let Lf::G { n, ty, f10, f14, f15, eq, .. } = l.clone() else { continue };
                            let Some(&u) = bit.get(&n) else { continue };
                            let fl5 = f.recs[&n].fl5();
                            if f10 != 0 && fl5 & 0x10 == 0 && m1c & u != 0 {
                                if let Lf::G { f10: x, .. } = l {
                                    *x = 0;
                                }
                                let mut ef = 0x10u8;
                                if ncb & u == 0 {
                                    ef |= 0x40;
                                }
                                // CmdInsertExit: the exitsave's operand (+0x15): 0x40 = in the bottom piece, 0x10 =
                                // the value is not live below
                                let mut e15 = 0u8;
                                if ncb & u == 0 {
                                    e15 |= 0x40;
                                }
                                if live & u == 0 {
                                    e15 |= 0x10;
                                }
                                let _ = ef;
                                exits.push(It::I {
                                    tup: 0,
                                    op: b.exitsave,
                                    kind: 13,
                                    f1c: 0,
                                    d: vec![Lf::S { f10: 0, ty }],
                                    s: vec![Lf::G { n, sn: n, ty, f10: 0, f14: 0x10, f15: e15, eq: true }],
                                });
                            }
                            m1c &= !u;
                            if let Some(lp) = f.b[i].lp {
                                // 10b12ad6..10b12b07: the web is not live into the loop's header or out of an exit
                                let lpr = f.loops.get(&lp).cloned();
                                if let Some(lpr) = lpr {
                                    let hd = idx.get(&lpr.head).map_or(0, |&h| st[h].li);
                                    let ex = lpr.exits.iter().any(|e| idx.get(e).map_or(true, |&j| st[j].lo & u == 0));
                                    if hd & u == 0 || ex {
                                        loop_refs(f, lp, n);
                                    }
                                }
                            }
                            if f14 & 0xf == 0 {
                                if live & u == 0 {
                                    if op == b.enterload {
                                        ncb |= u;
                                        deleted = true;
                                        break;
                                    }
                                    let f10now = if let Lf::G { f10: x, .. } = l { *x } else { 0 };
                                    if f10now == 0 && ty & 0xf000 != b.x87nib && eq {
                                        // CmdSpillSingleDef: the dead def writes memory
                                        ncb |= u;
                                        *l = Lf::M { ty, k: 6, nz: true };
                                        continue;
                                    }
                                } else {
                                    live &= !u;
                                }
                                if let Lf::G { f15: x, .. } = l {
                                    if ncb & u == 0 {
                                        ncb |= u;
                                        *x |= 0x40;
                                    } else {
                                        *x &= !0x40;
                                    }
                                }
                            } else {
                                if let Lf::G { f15: x, .. } = l {
                                    if ncb & u == 0 {
                                        *x |= 0x40;
                                    } else {
                                        *x &= !0x40;
                                    }
                                }
                                live &= !u;
                            }
                            let _ = f15;
                        }
                        if deleted {
                            continue;
                        }
                        let ns = s.len();
                        for k in 0..ns {
                            match s[k].clone() {
                                Lf::G { n, .. } => {
                                    let Some(&u) = bit.get(&n) else { continue };
                                    let later = s[k + 1..].iter().any(|x| matches!(x, Lf::G { n: m2, .. } if *m2 == n));
                                    if let Lf::G { f15, .. } = &mut s[k] {
                                        if ncb & u == 0 {
                                            *f15 |= 0x40;
                                        } else {
                                            *f15 &= !0x40;
                                        }
                                        if *f15 & 0x10 != 0 {
                                            m1c &= !u;
                                        }
                                        if live & u == 0 {
                                            if later {
                                                *f15 &= !0x10;
                                            } else {
                                                live |= u;
                                                *f15 |= 0x10;
                                            }
                                        } else {
                                            *f15 &= !0x10;
                                        }
                                    }
                                }
                                Lf::S { f10, ty } if f10 != 0 => {
                                    let v = f10val(&f.recs, &bit, f10, b);
                                    if v != 0 {
                                        m1c |= v;
                                        s[k] = Lf::S { f10: 0, ty };
                                    }
                                }
                                _ => {}
                            }
                        }
                        // exits go after the instruction: pushed first since `out` is reversed
                        for mut e in exits.into_iter().rev() {
                            if let It::I { tup: t, .. } = &mut e {
                                f.next_tup += 1;
                                *t = f.next_tup;
                            }
                            out.push(e);
                        }
                        out.push(It::I { tup, op, kind, f1c, d, s });
                    }
                    other => out.push(other),
                }
            }
            out.reverse();
            f.b[i].it = out;
        }
    }
    for &i in &inl {
        let blk = &mut f.b[i];
        blk.pr.retain(|n| !split.contains(n));
    }
    // RewriteSplitGlobregs 10b12d20 (with GrgAirlockEnterloads 10b59320 at each block)
    let mut nl = inl.clone();
    let mut k = 0;
    while k < nl.len() {
        let i = nl[k];
        let ins = airlock(f, i, cls, b);
        // the block list shifted when airlock blocks were inserted before block i
        if ins > 0 {
            for x in nl.iter_mut() {
                if *x >= i {
                    *x += ins;
                }
            }
        }
        let i = nl[k];
        let mut lv: Set = f.b[i].li.iter().copied().filter(|n| f.new[cls].contains(n)).collect();
        let lo = f.b[i].lo.clone();
        let items = std::mem::take(&mut f.b[i].it);
        let mut out: Vec<It> = Vec::with_capacity(items.len() + 4);
        for it in items {
            match it {
                It::Sp { webs, .. } => {
                    for g in webs {
                        if let Some(lp) = f.b[i].lp {
                            loop_refs(f, lp, g);
                        }
                        if let Some(r) = f.recs.get(&g) {
                            for p in &r.pieces {
                                lv.remove(p);
                            }
                        }
                    }
                }
                It::I { tup, op, kind, f1c, mut d, mut s } => {
                    let mut reloads: Vec<(u32, u32)> = vec![];
                    for k2 in 0..s.len() {
                        let Lf::G { n, ty, f14, f15, .. } = s[k2].clone() else { continue };
                        if !split.contains(&n) {
                            continue;
                        }
                        let ps = f.recs[&n].pieces.clone();
                        let p = ps.iter().find(|x| lv.contains(x)).copied();
                        let piece = match p {
                            Some(p) => {
                                if f15 & 0x10 != 0 {
                                    lv.remove(&p);
                                }
                                p
                            }
                            None => {
                                let q = if f15 & 0x40 == 0 {
                                    if f15 & 0x10 != 0 && ty & 0xf000 != b.x87nib && f14 & 0xf == 0 {
                                        // GrgChangeRegToImmed / CmdSpillSingleUse: the last use reads memory
                                        s[k2] = if f.recs[&n].imm() { Lf::O } else { Lf::M { ty, k: 6, nz: true } };
                                        continue;
                                    }
                                    let q = f.new_rec(n, "use", num, cls);
                                    pieces.push(Piece { num: q, parent: n, site: "use" });
                                    f.b[i].pr.insert(q);
                                    if f15 & 0x10 == 0 {
                                        lv.insert(q);
                                    }
                                    q
                                } else {
                                    let q = ps.iter().find(|x| lo.contains(x)).copied();
                                    let Some(q) = q else {
                                        notes.push(format!("block {:x}: a use of {:x} flagged live-out has no live-out piece", f.b[i].bb, n));
                                        continue;
                                    };
                                    lv.insert(q);
                                    q
                                };
                                reloads.push((q, ty));
                                q
                            }
                        };
                        if let Lf::G { n: x, sn, .. } = &mut s[k2] {
                            *x = piece;
                            *sn = piece;
                        }
                        if ty & 0xfff == 1 {
                            f.recs.get_mut(&piece).unwrap().w4 |= 0x80 << 8;
                        }
                    }
                    for k2 in 0..d.len() {
                        let Lf::G { n, ty, f14, f15, .. } = d[k2].clone() else { continue };
                        if !split.contains(&n) {
                            continue;
                        }
                        let ps = f.recs[&n].pieces.clone();
                        for p in &ps {
                            lv.remove(p);
                        }
                        let piece = if f14 & 0xf == 0 {
                            if f15 & 0x40 != 0 {
                                match ps.iter().find(|x| lo.contains(x)).copied() {
                                    Some(q) => q,
                                    None => {
                                        notes.push(format!("block {:x}: a def of {:x} flagged live-out has no live-out piece", f.b[i].bb, n));
                                        continue;
                                    }
                                }
                            } else {
                                let q = f.new_rec(n, "def", num, cls);
                                pieces.push(Piece { num: q, parent: n, site: "def" });
                                f.b[i].pr.insert(q);
                                q
                            }
                        } else {
                            let t = (f14 & 0xf) as usize;
                            match s.get(t - 1) {
                                Some(Lf::G { n: q, .. }) => *q,
                                _ => {
                                    notes.push(format!("tuple {:x}: tied operand {} is not a register", tup, t));
                                    continue;
                                }
                            }
                        };
                        if let Lf::G { n: x, sn, .. } = &mut d[k2] {
                            *x = piece;
                            *sn = piece;
                        }
                        lv.insert(piece);
                        if ty & 0xfff == 1 {
                            f.recs.get_mut(&piece).unwrap().w4 |= 0x80 << 8;
                        }
                    }
                    for (q, ty) in reloads {
                        let t = f.tup();
                        let im = f.recs.get(&q).map_or(false, |r| r.imm());
                        out.push(enter(b, q, ty, 0, t, im));
                    }
                    out.push(It::I { tup, op, kind, f1c, d, s });
                }
                other => out.push(other),
            }
        }
        f.b[i].it = out;
        k += 1;
    }
    // the parents are retired
    for g in &parents {
        f.recs.remove(g);
        for blk in f.b.iter_mut() {
            blk.li.remove(g);
            blk.lo.remove(g);
            blk.pr.remove(g);
        }
    }
    pieces
}

/// GrgUpdateLoopRefs 10b5bb20: the variable of web `n` (not an immediate) joins the reference set of loop `l` and
/// of every loop around it
fn loop_refs(f: &mut Fun, l: u32, n: u32) {
    let Some(r) = f.recs.get(&n) else { return };
    let (Some(x), false) = (r.xidx, r.imm()) else { return };
    let mut l = l;
    let mut k = 0;
    while l != 0 && k < 64 {
        let Some(lp) = f.loops.get_mut(&l) else { break };
        lp.refs.insert(x);
        l = lp.up;
        k += 1;
    }
}

fn enter(b: &Bld, q: u32, ty: u32, f15: u8, tup: u32, imm: bool) -> It {
    // CmdInsertEnter 10c235c0: OPENTERLOAD piece <- the variable's memory, or for an immediate the constant (type
    // 0x1004)
    let ty = if imm { 0x1004 } else { ty };
    It::I {
        tup,
        op: b.enterload,
        kind: 13,
        f1c: 0,
        d: vec![Lf::G { n: q, sn: q, ty, f10: 0, f14: 0x10, f15, eq: true }],
        s: vec![if imm { Lf::O } else { Lf::S { f10: 0, ty } }],
    }
}

/// GrgAirlockEnterloads 10b59320 for block i (called with its 3rd argument 1 by RewriteSplitGlobregs): for each
/// predecessor P when the block has two or more, the pieces live into the block and not out of P get an enterload
/// each, in ascending number: at P's end (GetEnterInsertPoint: before P's branch) when P has one successor, else
/// in an airlock block between P and the block (FgInsertAirlockBlock: placed just before the block; the previous
/// pred's airlock is reused when it loads the same set).  P's live-out set grows by them.  Returns the number of
/// blocks inserted before block i.
fn airlock(f: &mut Fun, i: usize, cls: usize, b: &Bld) -> usize {
    if f.b[i].p.len() < 2 {
        return 0;
    }
    let mut inserted = 0usize;
    let mut last: Option<(u32, Set)> = None; // the airlock made for the previous pred: its bb, its set
    let preds = f.b[i].p.clone();
    for (pk, p) in preds.iter().enumerate() {
        let bi = i + inserted;
        let idx = f.idx();
        let Some(&j) = idx.get(p) else { continue };
        let need: Set = f.b[bi].li.iter().copied().filter(|n| !f.b[j].lo.contains(n)).collect();
        if need.is_empty() {
            continue;
        }
        let tyof = |f: &Fun, n: u32| -> u32 {
            for blk in &f.b {
                for it in &blk.it {
                    if let It::I { d, s, .. } = it {
                        for l in d.iter().chain(s.iter()) {
                            if let Lf::G { n: m, ty, .. } = l {
                                if *m == n {
                                    return *ty;
                                }
                            }
                        }
                    }
                }
            }
            0x1004
        };
        let multi = f.b[j].s.len() >= 2 || f.b[j].fl & 0x1000000 != 0;
        if multi {
            if let Some((abb, aset)) = &last {
                if *aset == need {
                    // reuse: P's branch now goes to that airlock
                    let abb = *abb;
                    let bb = f.b[bi].bb;
                    for x in f.b[j].s.iter_mut() {
                        if *x == bb {
                            *x = abb;
                        }
                    }
                    f.b[bi].p.retain(|x| x != p);
                    let ai = f.idx()[&abb];
                    f.b[ai].p.push(*p);
                    f.b[j].lo.extend(need.iter().copied());
                    continue;
                }
            }
            // FgInsertAirlockBlock: a new block between P and the block, just before the block
            f.next_bb += 1;
            let abb = f.next_bb;
            f.next_tup += 1;
            let bb = f.b[bi].bb;
            let mut it = vec![];
            for &q in &need {
                f.next_tup += 1;
                let im = f.recs.get(&q).map_or(false, |r| r.imm());
                it.push(enter(b, q, tyof(f, q), 0x80, f.next_tup, im));
            }
            // A+0x48 = B+0x48 & P+0x4c, A+0x4c = a copy, A+0x50 = a copy; then the reloads make S live out / present
            let thru: Set = f.b[bi].li.iter().copied().filter(|n| f.b[j].lo.contains(n)).collect();
            let nb = Blk {
                tup: f.next_tup,
                bb: abb,
                lv: f.b[bi].lv,
                fl: 0,
                p: vec![*p],
                s: vec![bb],
                li: thru.clone(),
                lo: thru.union(&need).copied().collect(),
                pr: thru.union(&need).copied().collect(),
                it,
                synth: true,
                lp: f.b[bi].lp,
            };
            for x in f.b[j].s.iter_mut() {
                if *x == bb {
                    *x = abb;
                }
            }
            if let Some(x) = f.b[bi].p.iter_mut().find(|x| **x == *p) {
                *x = abb;
            }
            f.b.insert(bi, nb);
            inserted += 1;
            let _ = pk;
            last = Some((abb, need.clone()));
            let jj = if j >= bi { j + 1 } else { j };
            f.b[jj].lo.extend(need.iter().copied());
        } else {
            // GetEnterInsertPoint: before P's last instruction when it is a branch, else at the end
            let mut pos = f.b[j].it.len();
            while pos > 0 && matches!(f.b[j].it[pos - 1], It::Sp { .. }) {
                pos -= 1;
            }
            if pos > 0 {
                if let It::I { kind, .. } = f.b[j].it[pos - 1] {
                    if kind == 16 || kind == 18 || kind == 20 {
                        pos -= 1;
                    }
                }
            }
            let mut k = 0;
            for &q in &need {
                f.next_tup += 1;
                let im = f.recs.get(&q).map_or(false, |r| r.imm());
                let e = enter(b, q, tyof(f, q), 0x80, f.next_tup, im);
                f.b[j].it.insert(pos + k, e);
                k += 1;
            }
            f.b[j].lo.extend(need.iter().copied());
            f.b[j].pr.extend(need.iter().copied()); // 10b59772: P+0x50 |= S
        }
        let _ = cls;
    }
    inserted
}

// ------------------------------------------------------------------------------------------------ Spillcosts

/// CmdCanDirectSubstitute 10c23980 for a two-address dst (its default case, 10c23b23 / 10c23bd1): the instruction
/// could take the web from memory, so the dst costs nothing, unless the other operand is itself memory.  The f4
/// probe shows one exception (`add x, const` costs 2, `sub x, const` 0), which the variant PIECES_TIED=reg keeps.
fn tied_dst_free(op: u32, s: &[Lf], _b: &Bld) -> bool {
    let other = s.get(1);
    if std::env::var("PIECES_TIED").map_or(false, |v| v == "reg") {
        return matches!(other, Some(Lf::G { .. }) | Some(Lf::R { .. }));
    }
    if std::env::var("PIECES_TIED").map_or(false, |v| v == "code") {
        return !matches!(other, Some(Lf::M { .. }) | Some(Lf::S { .. }));
    }
    // observed (f4, 14.00 probes): `add x, const` costs 2
    !matches!(other, Some(Lf::M { .. }) | Some(Lf::S { .. })) && !(op == 0x2d && matches!(other, Some(Lf::O)))
}

#[derive(Clone, Debug, Default)]
pub struct Cost {
    pub terms: Vec<(u32, Option<i64>, i64)>, // (bb, blkcost or None for live-through unreferenced, delta)
    pub nrefs: i64,
    pub ben: i64,
    pub prio: i64,
    pub ne: i64,
    pub fb: Option<u32>,
    pub lb: Option<u32>,
    pub lastop: Option<u32>,
    pub fl5: u32,
    pub fl6: u32,
    /// every costed reference: (tuple, 's' | 'd', cost, shape = opcode / tied / second src kind)
    pub refs: Vec<(u32, char, i64, String)>,
    /// benefit parts: enterloads (count, total), exitsaves (count, total)
    pub enters: (i64, i64),
    pub exits: (i64, i64),
}


/// Spillcosts 10b10420 for the new webs `new` (class `cls`), at /O2 when `speed`: walks the blocks whose present set
/// holds one of them.  Returns per new web its terms and totals, and the walked blocks (bb, w, P).
pub fn spillcosts(f: &Fun, new: &Set, cls: i32, speed: bool, b: &Bld) -> (BTreeMap<u32, Cost>, Vec<(u32, i64, i64)>) {
    let mut c: BTreeMap<u32, Cost> = new.iter().map(|&n| (n, Cost::default())).collect();
    let mut walked = vec![];
    let selfof = |n: u32| f.recs.get(&n).map(|r| r.sn).unwrap_or(n);
    let coloured = |n: u32| f.recs.get(&n).map_or(false, |r| r.col != 0);
    for blk in &f.b {
        if !blk.pr.iter().any(|n| new.contains(n)) {
            continue;
        }
        let w: i64 = if speed { 1i64 << blk.lv.min(30) } else { 1 };
        let mut live: Set = blk.li.iter().copied().filter(|n| new.contains(n)).collect();
        for &n in &live {
            let e = c.get_mut(&n).unwrap();
            if e.fb.is_none() {
                e.fb = Some(blk.bb);
            }
            e.lb = Some(blk.bb);
        }
        let mut died: Set = Set::new();
        let mut oldref: Set = Set::new();
        let mut refd: Set = Set::new();
        let mut cost: HashMap<u32, i64> = HashMap::new();
        for it in &blk.it {
            let It::I { tup, op, d, s, .. } = it else { continue };
            let s2 = match s.get(1) {
                Some(Lf::G { .. }) => "G",
                Some(Lf::R { .. }) => "R",
                Some(Lf::M { .. }) | Some(Lf::S { .. }) => "M",
                Some(Lf::O) => "C",
                Some(_) => "?",
                None => "-",
            };
            for (k, l) in s.iter().enumerate() {
                let Lf::G { n, ty, f14, f15, .. } = l else { continue };
                if b.class(*ty) != cls {
                    continue;
                }
                let g = selfof(*n);
                if coloured(g) {
                    died.insert(g);
                    continue;
                }
                if !new.contains(&g) {
                    oldref.insert(g);
                    continue;
                }
                let e = c.get_mut(&g).unwrap();
                if *op == b.exitsave {
                    e.ne += 1;
                    let xi = f.recs.get(&g).and_then(|r| r.xidx);
                    let charged = !speed || match (blk.lp.and_then(|l| f.loops.get(&l)), xi) {
                        (Some(l), Some(x)) => l.refs.contains(&x),
                        _ => true,
                    };
                    if charged {
                        e.ben -= 2 * w;
                        e.fl5 |= 4;
                        e.exits.0 += 1;
                        e.exits.1 -= 2 * w;
                    }
                } else {
                    let imm = f.recs.get(&g).map_or(false, |r| r.imm());
                    let x = if imm {
                        // CmdSubCost 10c237c0 at /O2 for an immediate: 1 when the instruction's anchor operand (the
                        // first dst for a mov, else the first src) is a symbol or a memory operand with a symbol or
                        // displacement, 0 otherwise and always for `cmp x, 0`
                        let zero = f.recs.get(&g).and_then(|r| r.kval) == Some((7, 0));
                        let anchor = if *op == 1 { d.first() } else { s.first() };
                        let m = matches!(anchor, Some(Lf::S { .. }) | Some(Lf::M { k: 6, nz: true, .. }));
                        (!(*op == 0x2f && zero) && m) as i64
                    } else {
                        2
                    };
                    *cost.entry(g).or_insert(0) += x;
                    e.nrefs += 1;
                    e.ben += x * w;
                    e.refs.push((*tup, 's', x, format!("{:x}/{}/{}", op, if imm { "imm" } else { "reg" }, s2)));
                    if f14 & 0x20 == 0 {
                        e.fl5 |= 4;
                    } else if s[k + 1..].iter().any(|y| matches!(y, Lf::G { n: m, .. } if *m == *n)) {
                        e.ne += 1;
                    }
                }
                refd.insert(g);
                if f15 & 0x10 != 0 && f14 & 0xf == 0 {
                    let tied_dst = d.iter().any(|y| matches!(y, Lf::G { n: m, f14: t, .. } if *m == *n && t & 0xf != 0));
                    if !tied_dst {
                        live.remove(&g);
                        died.insert(g);
                    }
                }
            }
            for l in d.iter() {
                let Lf::G { n, ty, f14, f15, .. } = l else { continue };
                if b.class(*ty) != cls {
                    continue;
                }
                let g = selfof(*n);
                if coloured(g) {
                    died.insert(g);
                    continue;
                }
                if !new.contains(&g) {
                    oldref.insert(g);
                    continue;
                }
                let e = c.get_mut(&g).unwrap();
                if *op == b.enterload {
                    let imm = f.recs.get(&g).map_or(false, |r| r.imm());
                    e.ben -= if imm { 1 } else { 2 } * w;
                    e.enters.0 += 1;
                    e.enters.1 -= if imm { 1 } else { 2 } * w;
                    e.fl5 |= 4;
                    if f15 & 0x80 != 0 {
                        e.fl6 |= 1;
                    }
                } else {
                    // CmdCanDirectSubstitute 10c23980 + CmdSubCost: a two-address dst costs 0 when the other operand
                    // is a register (the instruction could take the web from memory); imul (0xc1) never
                    let x = if f14 & 0xf != 0 && *op != 0xc1 && tied_dst_free(*op, s, b) {
                        0
                    } else {
                        2
                    };
                    *cost.entry(g).or_insert(0) += x;
                    e.nrefs += 1;
                    e.ben += x * w;
                    e.refs.push((*tup, 'd', x, format!("{:x}/{}/{}", op, if f14 & 0xf != 0 { "tied" } else { "free" }, s2)));
                }
                e.lastop = Some(*op);
                if e.fb.is_none() {
                    e.fb = Some(blk.bb);
                }
                e.lb = Some(blk.bb);
                refd.insert(g);
                if f14 & 0xf == 0 {
                    died.remove(&g);
                    live.insert(g);
                }
            }
        }
        let p = oldref.len() as i64
            + died.len() as i64
            + live.iter().filter(|n| refd.contains(n) && !died.contains(n)).count() as i64;
        let pw = p * w;
        walked.push((blk.bb, w, p));
        for &g in &died {
            if let Some(e) = c.get_mut(&g) {
                let bc = cost.get(&g).copied().unwrap_or(0);
                e.prio += bc * pw;
                e.terms.push((blk.bb, Some(bc), bc * pw));
            }
        }
        for &g in &live {
            if died.contains(&g) {
                continue;
            }
            let e = c.get_mut(&g).unwrap();
            if refd.contains(&g) {
                let bc = cost.get(&g).copied().unwrap_or(0);
                e.prio += bc * pw;
                e.terms.push((blk.bb, Some(bc), bc * pw));
            } else {
                e.prio -= pw;
                e.terms.push((blk.bb, None, -pw));
            }
        }
    }
    (c, walked)
}

// ------------------------------------------------------------------------------------------------ the command

#[derive(Default, Clone, Copy)]
struct Score {
    rounds: usize,
    seq: (usize, usize),
    blocks: (usize, usize),
    terms: (usize, usize),
    nrefs: (usize, usize),
    ben: (usize, usize),
    lastop: (usize, usize),
    prio: (usize, usize),
    ne: (usize, usize),
    exact: (usize, usize),
}
fn frac(a: (usize, usize)) -> String {
    if a.1 == 0 {
        "-".into()
    } else {
        format!("{}/{}", a.0, a.1)
    }
}

pub fn main(path: &str, args: &[String]) {
    let verbose = args.iter().any(|a| a == "-v");
    let chain = args.iter().any(|a| a == "--chain");
    let txt = std::fs::read_to_string(path).unwrap_or_else(|e| super::die(&format!("{}: {}", path, e)));
    let s: QSnap = serde_json::from_str(&txt).unwrap_or_else(|e| super::die(&format!("{}: {}", path, e)));
    if s.gdumps.is_empty() {
        println!("pieces: this snapshot has no globreg view (export it with `c2trace.py regs ... --pieces --export`)");
        return;
    }
    let build = s.c2.as_ref().map_or("14.00".to_string(), |x| x.build.clone());
    let b = bld(&build);
    let speed = s.pops.iter().find_map(|p| p.x.as_ref().and_then(|x| x.speed)).map_or(true, |v| v != 0);
    let dec = s.decisions.as_deref().unwrap_or(&[]);
    println!(
        "pieces: {}: {} split rounds dumped (c2 {}){}",
        s.func,
        s.gdumps.len(),
        build,
        if chain { "; --chain: each round's IL is the model's own, from the first round's input" } else { "" }
    );
    println!(
        "  {:>5} {:>5} {:>7} {:>9} {:>9} {:>9} {:>9} {:>9} {:>9} {:>9} {:>9}",
        "round", "pass", "parents", "pieces", "blocks", "terms", "nrefs", "benefit", "last def", "prio", "exact"
    );
    let mut tot = Score::default();
    let mut misses: Vec<String> = vec![];
    let mut notes: Vec<String> = vec![];
    let mut carried: Option<Fun> = None;
    let mut rc_tot = (0usize, 0usize);
    let mut rc_bad: BTreeMap<String, (usize, usize)> = BTreeMap::new(); // shape -> (wrong, seen)
    for (ri, gd) in s.gdumps.iter().enumerate() {
        if gd.cls != Some(0) {
            continue;
        }
        let traced_f = parse(&gd.lines);
        let ptr2n: HashMap<u32, u32> = traced_f.recs.values().map(|r| (r.ptr, r.n)).collect();
        let mut f = match (&carried, chain) {
            (Some(c), true) => merge_chain(c, &traced_f, &mut notes),
            _ => traced_f.clone(),
        };
        // the split set: RemoveSplitGlobregs' webs of this pass
        let split: Set = s
            .split_rounds
            .iter()
            .filter(|r| r.npass == gd.npass)
            .flat_map(|r| r.removed.iter().filter_map(|g| ptr2n.get(&hx(g)).copied()))
            .collect();
        let marked: Set = traced_f.recs.values().filter(|r| r.fl5() & 2 != 0).map(|r| r.n).collect();
        if split != marked {
            notes.push(format!(
                "round {}: RemoveSplitGlobregs' set ({}) is not the records with fl5 & 2 in the dump ({})",
                ri + 1,
                split.len(),
                marked.len()
            ));
        }
        // the trace's pieces of this round, in the order their records were taken
        let tp: Vec<&QDec> = dec
            .iter()
            .filter(|d| d.t == "piece" && d.npass == gd.npass && d.after_pop == gd.after_pop)
            .collect();
        let mut nums = tp.iter().map(|d| d.num.unwrap_or(0) as u32).collect::<Vec<_>>().into_iter();
        let mut extra = 0x7000_0000u32;
        let mut take = || nums.next().unwrap_or_else(|| {
            extra += 1;
            extra
        });
        if let Ok(w) = std::env::var("PIECES_WEB") {
            // debugging aid: the instructions that reference web W, the model's input against the trace's
            let w = hx(&w);
            let show = |g: &Fun, tag: &str| {
                for blk in &g.b {
                    for it in &blk.it {
                        match it {
                            It::I { tup, op, d, s: src, .. } if d.iter().chain(src.iter()).any(|l| matches!(l, Lf::G { n, .. } if *n == w)) => {
                                eprintln!("{} r{} {:x} {:x} {:x} | {:?} | {:?}", tag, ri + 1, blk.bb, tup, op, d, src)
                            }
                            It::Sp { tup, webs, .. } if webs.contains(&w) => eprintln!("{} r{} {:x} {:x} split", tag, ri + 1, blk.bb, tup),
                            _ => {}
                        }
                    }
                }
            };
            show(&f, "model");
            show(&traced_f, "trace");
        }
        let pieces = split_round(&mut f, &split, 0, &mut take, &b, &mut notes);
        let mut sc = Score { rounds: 1, ..Score::default() };
        // the sequence: (site, parent) of each record handed out
        let seq_ok = pieces.len() == tp.len()
            && pieces.iter().zip(&tp).all(|(p, d)| {
                Some(p.site) == d.site.as_deref() && d.pnum.map(|x| x as u32) == Some(p.parent) && d.num.map(|x| x as u32) == Some(p.num)
            });
        sc.seq = (seq_ok as usize, 1);
        if !seq_ok {
            let fmt = |v: Vec<String>| v.join(" ");
            misses.push(format!(
                "round {} (pass {}): pieces: model {} [{}], c2 {} [{}]",
                ri + 1,
                gd.npass,
                pieces.len(),
                fmt(pieces.iter().take(12).map(|p| format!("{}:{:x}<{:x}", p.site, p.num, p.parent)).collect()),
                tp.len(),
                fmt(tp.iter().take(12).map(|d| format!("{}:{:x}<{:x}", d.site.as_deref().unwrap_or("?"), d.num.unwrap_or(0), d.pnum.unwrap_or(0))).collect())
            ));
        }
        // costs of the next Spillcosts pass
        let newset: Set = f.new[0].clone();
        let (cost, walked) = spillcosts(&f, &newset, 0, speed, &b);
        let np = gd.npass + 1;
        let pass = s.passes.iter().find(|p| p.n == np && p.cls == Some(0));
        let num2gr: HashMap<u32, String> = tp.iter().map(|d| (d.num.unwrap_or(0) as u32, d.gr.clone())).collect();
        // block correspondence: the trace's walked blocks against the model's (synthetic airlock blocks by position)
        let mut bbmap: HashMap<u32, u32> = HashMap::new(); // model bb -> trace bb
        if let Some(p) = pass {
            let tb: Vec<u32> = p.blocks.iter().map(|x| x.bb.as_deref().map(hx).unwrap_or(0)).collect();
            let mb: Vec<u32> = walked.iter().map(|x| x.0).collect();
            let known: BTreeSet<u32> = f.b.iter().filter(|x| !x.synth).map(|x| x.bb).collect();
            let mut ok = tb.len() == mb.len();
            if ok {
                for (a, t) in mb.iter().zip(&tb) {
                    if known.contains(a) && a != t {
                        ok = false;
                    }
                    bbmap.insert(*a, *t);
                }
            }
            sc.blocks = (ok as usize, 1);
            if !ok {
                misses.push(format!(
                    "round {} (pass {}): Spillcosts walks {} blocks in the model, {} in c2",
                    ri + 1,
                    np,
                    mb.len(),
                    tb.len()
                ));
            }
            // w and P per block
            for ((mbb, w, pp), tbk) in walked.iter().zip(&p.blocks) {
                if *w != tbk.w || *pp != tbk.p {
                    misses.push(format!(
                        "round {} (pass {}): block {:x}: model w {} P {}, c2 w {} P {}",
                        ri + 1,
                        np,
                        mbb,
                        w,
                        pp,
                        tbk.w,
                        tbk.p
                    ));
                    break;
                }
            }
        } else if !pieces.is_empty() {
            notes.push(format!("round {}: no Spillcosts pass {} in the snapshot (it walked no block)", ri + 1, np));
        }
        let so: HashMap<&str, &QDec> = dec.iter().filter(|d| d.t == "so" && d.npass == np).map(|d| (d.gr.as_str(), d)).collect();
        let ins: HashMap<&str, &QIns> = s
            .inserts
            .iter()
            .filter(|i| i.npass == np && i.deferred != Some(true))
            .filter_map(|i| i.gr.as_deref().map(|g| (g, i)))
            .collect();
        // per-reference cost against Spillcosts' own probes (14.00: 10b109b7 / 10b10ff9)
        if !s.refcosts.is_empty() {
            let mut tr: HashMap<(u32, u32, char), Vec<i64>> = HashMap::new();
            for (n, tup, num, c, side) in &s.refcosts {
                if *n == np {
                    tr.entry((hx(tup), *num, side.chars().next().unwrap_or('s'))).or_default().push(*c);
                }
            }
            for p in &pieces {
                let Some(mc) = cost.get(&p.num) else { continue };
                for (tup, side, c, shape) in &mc.refs {
                    let Some(v) = tr.get_mut(&(*tup, p.num, *side)) else { continue };
                    if v.is_empty() {
                        continue;
                    }
                    let t = v.remove(0);
                    rc_tot.1 += 1;
                    let e = rc_bad.entry(format!("{} {}", side, shape)).or_default();
                    e.1 += 1;
                    if t == *c {
                        rc_tot.0 += 1;
                    } else {
                        e.0 += 1;
                    }
                }
            }
        }
        for p in &pieces {
            let Some(gr) = num2gr.get(&p.num) else { continue };
            let mc = cost.get(&p.num).cloned().unwrap_or_default();
            let mut exact = true;
            if let Some(pass) = pass {
                let bbof = |b: u32| pass.blocks.iter().find(|x| x.b == b).and_then(|x| x.bb.as_deref()).map(hx).unwrap_or(0);
                let tt: BTreeMap<u32, (Option<i64>, i64)> = pass
                    .grs
                    .get(gr)
                    .map(|g| g.terms.iter().map(|t| (bbof(t.b), (t.cost, t.delta))).collect())
                    .unwrap_or_default();
                let mt: BTreeMap<u32, (Option<i64>, i64)> =
                    mc.terms.iter().map(|(bb, c, d)| (bbmap.get(bb).copied().unwrap_or(*bb), (*c, *d))).collect();
                sc.terms.1 += 1;
                if tt == mt {
                    sc.terms.0 += 1;
                } else {
                    exact = false;
                    misses.push(format!(
                        "round {} piece {:x} (of {:x}, {}): terms model {:?} c2 {:?}",
                        ri + 1,
                        p.num,
                        p.parent,
                        p.site,
                        mt.iter().map(|(b, (c, d))| format!("{:x}:{}/{}", b, c.map_or("-".into(), |x| x.to_string()), d)).collect::<Vec<_>>(),
                        tt.iter().map(|(b, (c, d))| format!("{:x}:{}/{}", b, c.map_or("-".into(), |x| x.to_string()), d)).collect::<Vec<_>>()
                    ));
                }
            }
            if let Some(d) = so.get(gr.as_str()) {
                sc.nrefs.1 += 1;
                sc.ben.1 += 1;
                sc.lastop.1 += 1;
                if d.nrefs == Some(mc.nrefs) {
                    sc.nrefs.0 += 1;
                } else {
                    exact = false;
                }
                if d.ben == Some(mc.ben) {
                    sc.ben.0 += 1;
                } else {
                    exact = false;
                }
                if d.lastop.map(|x| x as u32) == mc.lastop || d.lastop.is_none() {
                    sc.lastop.0 += 1;
                } else {
                    exact = false;
                }
                if d.nrefs != Some(mc.nrefs) || d.ben != Some(mc.ben) {
                    misses.push(format!(
                        "round {} piece {:x}: nrefs {} benefit {} [enterloads {:?}, exitsaves {:?}] (c2 {:?} {:?})",
                        ri + 1,
                        p.num,
                        mc.nrefs,
                        mc.ben,
                        mc.enters,
                        mc.exits,
                        d.nrefs,
                        d.ben
                    ));
                }
            }
            if let Some(i) = ins.get(gr.as_str()) {
                if let Some(x) = &i.x {
                    sc.prio.1 += 1;
                    sc.ne.1 += 1;
                    if x.prio == Some(mc.prio) {
                        sc.prio.0 += 1;
                    } else {
                        exact = false;
                    }
                    let single = mc.fb == mc.lb;
                    if x.nenter == Some(mc.ne) && x.single == single && (x.fl6 & 1) == (mc.fl6 & 1) {
                        sc.ne.0 += 1;
                    } else {
                        exact = false;
                        misses.push(format!(
                            "round {} piece {:x}: nEnter {} single {} airlock {} (c2 {:?} {} {})",
                            ri + 1,
                            p.num,
                            mc.ne,
                            single,
                            mc.fl6 & 1,
                            x.nenter,
                            x.single,
                            x.fl6 & 1
                        ));
                    }
                }
            }
            sc.exact.1 += 1;
            if exact && seq_ok {
                sc.exact.0 += 1;
            }
        }
        println!(
            "  {:>5} {:>5} {:>7} {:>9} {:>9} {:>9} {:>9} {:>9} {:>9} {:>9} {:>9}",
            ri + 1,
            np,
            split.len(),
            format!("{}{}", pieces.len(), if seq_ok { "" } else { "*" }),
            frac(sc.blocks),
            frac(sc.terms),
            frac(sc.nrefs),
            frac(sc.ben),
            frac(sc.lastop),
            frac(sc.prio),
            frac(sc.exact)
        );
        for (a, x) in [
            (&mut tot.seq, sc.seq),
            (&mut tot.blocks, sc.blocks),
            (&mut tot.terms, sc.terms),
            (&mut tot.nrefs, sc.nrefs),
            (&mut tot.ben, sc.ben),
            (&mut tot.lastop, sc.lastop),
            (&mut tot.prio, sc.prio),
            (&mut tot.ne, sc.ne),
            (&mut tot.exact, sc.exact),
        ] {
            a.0 += x.0;
            a.1 += x.1;
        }
        // the model's enterloads / exitsaves against the next dump's (pieces that were not spilled in between)
        if let Some(next) = s.gdumps.get(ri + 1) {
            let tn = parse(&next.lines);
            let mine: Set = pieces.iter().map(|p| p.num).filter(|n| tn.recs.contains_key(n)).collect();
            let count = |g: &Fun| -> BTreeMap<(u32, u32, u32), i64> {
                let mut m = BTreeMap::new();
                for blk in &g.b {
                    for it in &blk.it {
                        if let It::I { op, d, s, .. } = it {
                            if *op == b.enterload || *op == b.exitsave {
                                for l in d.iter().chain(s.iter()) {
                                    if let Lf::G { n, .. } = l {
                                        if mine.contains(n) {
                                            *m.entry((if blk.synth { 0 } else { blk.bb }, *op, *n)).or_insert(0) += 1;
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                m
            };
            let (a, t) = (count(&f), count(&tn));
            let known: BTreeSet<u32> = f.b.iter().filter(|x| !x.synth).map(|x| x.bb).collect();
            let t: BTreeMap<(u32, u32, u32), i64> =
                t.into_iter().map(|((bb, op, n), c)| ((if known.contains(&bb) { bb } else { 0 }, op, n), c)).fold(BTreeMap::new(), |mut m, (k, c)| {
                    *m.entry(k).or_insert(0) += c;
                    m
                });
            for (k, c) in a.iter() {
                if t.get(k) != Some(c) {
                    misses.push(format!("round {}: {} of piece {:x} in block {:x}: model {}, c2 {}", ri + 1, if k.1 == b.enterload { "enterloads" } else { "exitsaves" }, k.2, k.0, c, t.get(k).copied().unwrap_or(0)));
                }
            }
            for (k, c) in t.iter() {
                if !a.contains_key(k) {
                    misses.push(format!("round {}: {} of piece {:x} in block {:x}: model 0, c2 {}", ri + 1, if k.1 == b.enterload { "enterloads" } else { "exitsaves" }, k.2, k.0, c));
                }
            }
        }
        tot.rounds += 1;
        carried = Some(f);
    }
    println!(
        "  all: {} rounds; piece sequence {}; blocks walked {}; per-block terms {}; nrefs {}; benefit {}; last def {}; \
         priority {}; nEnter/single/airlock {}; pieces exact {}",
        tot.rounds,
        frac(tot.seq),
        frac(tot.blocks),
        frac(tot.terms),
        frac(tot.nrefs),
        frac(tot.ben),
        frac(tot.lastop),
        frac(tot.prio),
        frac(tot.ne),
        frac(tot.exact)
    );
    if rc_tot.1 > 0 {
        println!("  reference costs (against Spillcosts' probes): {} of {}", rc_tot.0, rc_tot.1);
        for (k, (bad, seen)) in &rc_bad {
            if *bad > 0 {
                println!("    shape {}: {} of {} wrong", k, bad, seen);
            }
        }
    }
    let show = if verbose { misses.len() } else { 8 };
    for m in misses.iter().take(show) {
        println!("  miss: {}", m);
    }
    if misses.len() > show {
        println!("  ... {} more (-v)", misses.len() - show);
    }
    for n in notes.iter().take(if verbose { usize::MAX } else { 4 }) {
        println!("  note: {}", n);
    }
}

/// --chain: the next round's input is the model's IL after its own previous round, updated with what the trace
/// says happened during the pops in between (colours, spills, records retired, split points made).  Taken from
/// the traced dump of this round: each record's colour and flags, the split points (by position: after the same
/// instruction), and which records still exist; the instructions, operands, enterloads and exitsaves are the
/// model's.
fn merge_chain(prev: &Fun, traced: &Fun, notes: &mut Vec<String>) -> Fun {
    let mut f = prev.clone();
    // records: colours / flags from the trace; records the trace no longer has are gone (spilled, folded)
    let mut gone = vec![];
    for (n, r) in f.recs.iter_mut() {
        match traced.recs.get(n) {
            Some(t) => {
                r.col = t.col;
                r.w4 = t.w4;
                r.ptr = t.ptr;
                r.fb = t.fb;
                r.lb = t.lb;
            }
            None => gone.push(*n),
        }
    }
    for n in gone {
        f.recs.remove(&n);
        // Spill: every operand of the web reads or writes memory now
        for blk in f.b.iter_mut() {
            blk.li.remove(&n);
            blk.lo.remove(&n);
            blk.pr.remove(&n);
            for it in blk.it.iter_mut() {
                if let It::I { d, s, .. } = it {
                    for l in d.iter_mut().chain(s.iter_mut()) {
                        if let Lf::G { n: m, ty, .. } = l {
                            if *m == n {
                                *l = Lf::M { ty: *ty, k: 6, nz: true };
                            }
                        }
                    }
                }
            }
        }
    }
    for (n, t) in &traced.recs {
        if !f.recs.contains_key(n) {
            notes.push(format!("chain: record {:x} exists in the trace only", n));
            f.recs.insert(*n, t.clone());
        }
    }
    f.new = traced.new.clone();
    // the model's airlock blocks get the trace's pointers: a synthetic block sits just before the same block in both
    let known: BTreeSet<u32> = f.b.iter().filter(|x| !x.synth).map(|x| x.bb).collect();
    let mut ren: HashMap<u32, u32> = HashMap::new();
    for (k, tb) in traced.b.iter().enumerate() {
        if known.contains(&tb.bb) {
            continue;
        }
        let Some(next) = traced.b.get(k + 1).map(|x| x.bb) else { continue };
        if let Some(mi) = f.b.iter().position(|x| x.bb == next) {
            if mi > 0 && f.b[mi - 1].synth && !ren.contains_key(&f.b[mi - 1].bb) {
                ren.insert(f.b[mi - 1].bb, tb.bb);
            }
        }
    }
    for blk in f.b.iter_mut() {
        if let Some(&nb) = ren.get(&blk.bb) {
            blk.bb = nb;
            blk.synth = false;
        }
        for x in blk.p.iter_mut().chain(blk.s.iter_mut()) {
            if let Some(&nb) = ren.get(x) {
                *x = nb;
            }
        }
        if let Some(tb) = traced.b.iter().find(|x| x.bb == blk.bb) {
            blk.lv = tb.lv;
            blk.lp = tb.lp;
        }
    }
    f.loops = traced.loops.clone();
    // the model's own tuples (reloads, stores) get the trace's ids: same block, same opcode, same operands, in order
    let sig = |it: &It| -> Option<(u32, Vec<u32>)> {
        if let It::I { op, d, s, .. } = it {
            let mut v: Vec<u32> = d.iter().chain(s.iter()).filter_map(|l| if let Lf::G { n, .. } = l { Some(*n) } else { None }).collect();
            v.sort();
            Some((*op, v))
        } else {
            None
        }
    };
    for blk in f.b.iter_mut() {
        let Some(tb) = traced.b.iter().find(|x| x.bb == blk.bb) else { continue };
        let mine: BTreeSet<u32> = blk.it.iter().filter_map(|x| if let It::I { tup, .. } = x { Some(*tup) } else { None }).collect();
        let mut free: Vec<(u32, (u32, Vec<u32>))> = tb
            .it
            .iter()
            .filter_map(|x| match x {
                It::I { tup, .. } if !mine.contains(tup) => sig(x).map(|g| (*tup, g)),
                _ => None,
            })
            .collect();
        for it in blk.it.iter_mut() {
            let g = sig(it);
            if let It::I { tup, .. } = it {
                if *tup >= 0xf000_0000 {
                    if let Some(k) = free.iter().position(|x| Some(&x.1) == g.as_ref()) {
                        *tup = free.remove(k).0;
                    }
                }
            }
        }
    }
    // instructions the trace no longer has were deleted at a pop (ForceNoAirlocks' enterloads, a dead def a spill
    // removed, a def CmdSubstDef folded): drop them too
    let tt: BTreeSet<u32> = traced.b.iter().flat_map(|b| b.it.iter()).filter_map(|x| if let It::I { tup, .. } = x { Some(*tup) } else { None }).collect();
    let mut dropped = 0;
    for blk in f.b.iter_mut() {
        let n0 = blk.it.len();
        blk.it.retain(|x| !matches!(x, It::I { tup, .. } if !tt.contains(tup)));
        dropped += n0 - blk.it.len();
    }
    if dropped > 0 {
        notes.push(format!("chain: {} instructions deleted at the pops (not in the trace's IL any more)", dropped));
    }
    // split points: put each traced one after the same instruction (by tuple id) in the model's IL
    let have: BTreeSet<u32> = f
        .b
        .iter()
        .flat_map(|b| b.it.iter())
        .filter_map(|x| match x {
            It::I { tup, .. } | It::L(tup) | It::P(tup, _, _) => Some(*tup),
            _ => None,
        })
        .collect();
    let mut after: HashMap<u32, Vec<It>> = HashMap::new();
    let mut at_top: HashMap<u32, Vec<It>> = HashMap::new();
    for blk in &traced.b {
        let mut prev_tup: Option<u32> = None; // the last tuple before the point that the model also has
        for it in &blk.it {
            match it {
                It::Sp { .. } => match prev_tup {
                    Some(t) => after.entry(t).or_default().push(it.clone()),
                    None => at_top.entry(blk.bb).or_default().push(it.clone()),
                },
                It::I { tup, .. } | It::L(tup) | It::P(tup, _, _) => {
                    if have.contains(tup) {
                        prev_tup = Some(*tup);
                    }
                }
            }
        }
    }
    for blk in f.b.iter_mut() {
        blk.it.retain(|x| !matches!(x, It::Sp { .. }));
        let mut out = vec![];
        if let Some(v) = at_top.remove(&blk.bb) {
            out.extend(v);
        }
        for it in std::mem::take(&mut blk.it) {
            let t = match &it {
                It::I { tup, .. } | It::L(tup) | It::P(tup, _, _) => Some(*tup),
                _ => None,
            };
            out.push(it);
            if let Some(t) = t {
                if let Some(v) = after.remove(&t) {
                    out.extend(v);
                }
            }
        }
        blk.it = out;
        if let Some(tb) = traced.b.iter().find(|x| x.bb == blk.bb) {
            blk.fl = tb.fl;
        }
    }
    if !after.is_empty() || !at_top.is_empty() {
        notes.push(format!("chain: {} split points follow an instruction the model does not have", after.len() + at_top.len()));
    }
    f
}

// ------------------------------------------------------------------------------------------------ for --predict

/// The model's record of one piece, as `rasim check --predict --pieces` uses it in place of the trace's.
#[derive(Clone, Debug)]
pub struct Ov {
    pub ben: i64,
    pub prio: i64,
    pub nrefs: i64,
    pub single: bool,
    pub nenter: i64,
    pub airlock: bool,
    pub lastop: Option<u32>,
}

/// Every piece of every integer split round, keyed by (Spillcosts pass it is costed in, record pointer as the
/// trace spells it): per round from that round's dump, or with `chain` from the first round's dump onwards.
/// None when the snapshot has no globreg view.
pub fn model_pieces(path: &str, chain: bool) -> Option<HashMap<(u32, String), Ov>> {
    let txt = std::fs::read_to_string(path).ok()?;
    let s: QSnap = serde_json::from_str(&txt).ok()?;
    if s.gdumps.is_empty() {
        return None;
    }
    let build = s.c2.as_ref().map_or("14.00".to_string(), |x| x.build.clone());
    let b = bld(&build);
    let speed = s.pops.iter().find_map(|p| p.x.as_ref().and_then(|x| x.speed)).map_or(true, |v| v != 0);
    let dec = s.decisions.as_deref().unwrap_or(&[]);
    let mut out = HashMap::new();
    let mut carried: Option<Fun> = None;
    let mut notes = vec![];
    for gd in &s.gdumps {
        if gd.cls != Some(0) {
            continue;
        }
        let traced_f = parse(&gd.lines);
        let ptr2n: HashMap<u32, u32> = traced_f.recs.values().map(|r| (r.ptr, r.n)).collect();
        let mut f = match (&carried, chain) {
            (Some(c), true) => merge_chain(c, &traced_f, &mut notes),
            _ => traced_f.clone(),
        };
        let split: Set = s
            .split_rounds
            .iter()
            .filter(|r| r.npass == gd.npass)
            .flat_map(|r| r.removed.iter().filter_map(|g| ptr2n.get(&hx(g)).copied()))
            .collect();
        let tp: Vec<&QDec> =
            dec.iter().filter(|d| d.t == "piece" && d.npass == gd.npass && d.after_pop == gd.after_pop).collect();
        let mut nums = tp.iter().map(|d| d.num.unwrap_or(0) as u32).collect::<Vec<_>>().into_iter();
        let mut extra = 0x7000_0000u32;
        let mut take = || nums.next().unwrap_or_else(|| {
            extra += 1;
            extra
        });
        let pieces = split_round(&mut f, &split, 0, &mut take, &b, &mut notes);
        let newset: Set = f.new[0].clone();
        let (cost, _) = spillcosts(&f, &newset, 0, speed, &b);
        let num2gr: HashMap<u32, String> = tp.iter().map(|d| (d.num.unwrap_or(0) as u32, d.gr.clone())).collect();
        for p in &pieces {
            if let (Some(gr), Some(c)) = (num2gr.get(&p.num), cost.get(&p.num)) {
                out.insert(
                    (gd.npass + 1, gr.clone()),
                    Ov {
                        ben: c.ben,
                        prio: c.prio,
                        nrefs: c.nrefs,
                        single: c.fb == c.lb,
                        nenter: c.ne,
                        airlock: c.fl6 & 1 != 0,
                        lastop: c.lastop,
                    },
                );
            }
        }
        carried = Some(f);
    }
    Some(out)
}
