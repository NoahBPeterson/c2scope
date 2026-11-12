#!/usr/bin/env python3
"""Trace c2.dll's own functions during a real compile.  Doc: docs/c2/tracer.md

usage: c2trace.py install                      build c2trace.dll and patch the traced toolchain copy's c2.dll
       c2trace.py il <src> --func S [--at P,P] [--grep RE | --count RE] [-- <cl args>]   IL dump at passes (c2il.py)
       c2trace.py il <src> --func S --diff PASS_A,PASS_B [-- <cl args>]   what the passes between A and B changed
       c2trace.py merges <src> --func S [--pairs] [-- <cl args>]   every tail/head merge and why each pair stopped
                                     (also `il --merges`; tools/c2merges.py)
       c2trace.py scratch <src> --func S [-- <cl args>]   scratch-register picks -> listing offsets (c2scratch.py)
       c2trace.py regs <src> [--func S] [--why a,b] [-- <cl args>]   register-allocation report (tools/c2regs.py)
       c2trace.py slots <src> --func S [--what EDIT..] [--orig ORIG.s] [--search] [-- <cl args>]   stack-slot packing:
                                     colours, elements, replay model, what-if (tools/c2slots.py)
       c2trace.py regmap <src> --func S --asm-vs ORIG.s --asm-func DEC [-- <cl args>]   which register each variable
                                     should have, read off the original's code (tools/regmap.py)
       c2trace.py [options] <src.c> [-- <extra cl args>]
  -H, --hook NAME[+0xOFF][=SPEC,SPEC..][/xSPEC,..]   hook a c2 function by PDB name (@Color@4) or VA (hex);
                                     SPECs are logged at entry, /x... ones at exit (see SPEC below). Repeatable.
                                     With +0xOFF (or a VA inside a function) it is a probe on that instruction:
                                     logged each time it is reached, sN = words at the current esp, no exit.
  --exit                             also log returns of every hook (eax/edx + exit specs)
  --max N                            stop logging after N events
  --raw                              print the raw log, not the decoded one
  --keep DIR                         keep cfg/log/obj/asm in DIR (default: a temp dir)
  default hooks (no -H): @Color@4 and @Spillcosts@12 with a few stack words
SPEC := base {'>' off | '~' table} ['#' n | '$']   base = eax..edi | sN (entry stack word N; s0 = return address, s1 =
        first stack arg) | aN (same word, saved at entry: usable in exit specs) | gHEX (a c2 VA, e.g. a global);
        '>off' loads dword [v+off]; '~T' loads dword [T + 4*v] (T a c2 VA in hex: index a table);
        '#n' logs n dwords at v; '$' logs the C string at v.
        Examples: s1  s1>0x10  ecx#8  a2#4  g10d123ac>0>0>4$ (name of the function being compiled)
                  a1>0x10>0x1c~10cec7b8$ (at ChoosePhysReg exit: name of the register it picked)
Compile flags default to /O2; extra cl args after '--' are appended (e.g. -- /Ox /Oy-).

The traced toolchain is work/toolchain/vs2005-trace (copy of work/toolchain/vs2005); only its VC/Bin/c2.dll is
patched (entry point -> 45-byte cave that LoadLibraryA's c2trace.dll) and VC/Bin/c2trace.dll is added. The helper
DLL reads %C2TRACE_CFG% and writes 5-byte JMP detours into c2's .text in memory; without C2TRACE_CFG it is inert,
so the traced copy compiles exactly like the original.
"""
import argparse, os, re, shutil, struct, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import time
import capstone
import c2build                # takes --c2 BUILD out of sys.argv; C2_BUILD selects a non-default c2 (port-1310.md)
from image import Image, ROOT
import ccache, msvc

TRACE = os.path.join(ROOT, "work", "toolchain", "vs2005-trace")
TBIN = os.path.join(TRACE, "VC", "Bin")
ORIG_C2 = os.path.join(ROOT, "Bin", "c2.dll")
CAVE_RVA = 0x1D7BB0          # .text slack: VirtualSize ends at RVA 0x1d7bab, raw data at 0x1d8000
TEXT_HDR_VSIZE = 0x1D6BAB
ENTRY_RVA = 0x1D706D         # __DllMainCRTStartup@12
IAT_LOADLIBRARYA = 0x1D8028  # KERNEL32!LoadLibraryA IAT slot (RVA)
FILE_DELTA, NSEC = 0, 5     # .text RVA - file offset; number of sections (build tables may override: patch)
DEFAULT_HOOKS = ["@Color@4=s1,s2", "@Spillcosts@12=s1,s2,s3"]
if c2build.table():           # another c2 build: its own toolchain copies and cave addresses (tools/c2builds/*.json)
    TRACE = c2build.toolchain(True)
    TBIN = os.path.join(TRACE, "VC", "Bin")
    ORIG_C2 = c2build.orig_c2()
    CAVE_RVA, TEXT_HDR_VSIZE, ENTRY_RVA, IAT_LOADLIBRARYA = (int(c2build.get("patch")[k], 16) for k in (
        "cave_rva", "text_vsize", "entry_rva", "iat_loadlibrarya"))
    # optional: .text RVA minus file offset (0 when the file is section-aligned like 14.00), section count
    FILE_DELTA, NSEC = int(c2build.get("patch").get("file_delta", "0"), 16), int(c2build.get("patch").get("nsec", 5))
    DEFAULT_HOOKS = c2build.hooks("default", DEFAULT_HOOKS)

DLL_SRC = r'''/* c2trace.dll: in-process function-entry/exit tracer for c2.dll (VS2005 SP1).
 * Loaded by a cave at the patched c2.dll's entry point. Reads a hook list from %C2TRACE_CFG%,
 * writes 5-byte JMP detours into c2's .text, logs to %C2TRACE_LOG%. Does nothing if C2TRACE_CFG is unset.
 *
 * Config (text, one directive per line, '#' comments):
 *   hook <id> <rva-hex> <steal> <exit 0|1>   steal = bytes of whole, position-independent prologue instrs (>=5)
 *   e <spec>      value logged at entry of the last hook
 *   x <spec>      value logged at exit of the last hook (only if exit=1)
 *   i <len> <kind> <cc> <target-rva>   (optional, after hook) one displaced instruction; kind 0 copy verbatim,
 *                 1 jcc (cc = condition nibble), 2 jmp, 3 call: re-encoded rel32 to target
 *   max <n>       stop logging after n events
 *   ilmax <n>     cap on tuples per '%' IL dump (default 2000000; env C2TRACE_ILMAX on the Python side)
 *   set <name> <hex>  another c2 build (tools/c2build.py): base = preferred image base, regname / opname = the
 *                 _RegName / _TupOpcodeName tables, opcount = entries in the latter, blk / len / mem = tuple layout
 *                 (extended dump only: ext / labtup / brcc / ehop / ulabop, see dumpilx); fename 1 = the plain dump
 *                 prints a CODEADDR leaf (call target) by the front-end name at sym+4 (13.10; logs "# fename 1")
 *   op <hex> <name>   name of an opcode the build's own table lacks (after `set opcount`)
 * spec := base { '>' off } [ '#' n ]
 *   base: eax ecx edx ebx esp ebp esi edi | sN (stack word N at entry: s0 = return address, s1 = first stack arg)
 *         | aN (entry stack word N, saved, usable at exit) | gHEX (c2 VA, e.g. a global; rebased if c2 moved)
 *   '>off' replaces v with dword [v+off] (off hex or decimal, may be negative)
 *   '~HEX' replaces v with dword [HEX + 4*v] (HEX a c2 VA of a pointer/word table, rebased)
 *   '#n'   logs n dwords starting at address v instead of v itself;  '$' logs the C string at v
 *   '%'    v is a tuple: dump the tuple stream from it as "T ..." lines after the event (IL dump)
 *   '!'    the same for the one tuple v.  T I lines: T I <tuple> <opcode> <line offset> <+0x1c byte> | dst <- src
 *   '%+' '!+'  the extended dump (dumpilx below): the same lines plus notes after " ;" and the P/C/A line kinds
 * Log lines:  E <seq> <depth> <id> eax ecx edx ebx ebp esi edi esp | specs...
 *             X <seq> <depth> <id> eax edx | specs...
 */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#define MAXHOOK 128
#define MAXSPEC 12
#define MAXDEPTH 4096
#define NSAVE 8

typedef struct { DWORD edi, esi, ebp, esp, ebx, edx, ecx, eax, eflags, slot; } Regs;  /* pushfd;pushad image */

#define MAXDER 12             /* '>' / '~' steps per spec (was 6: longer PREF-chain specs were cut short) */
typedef struct { int base; DWORD idx; int nder; int der[MAXDER]; char tbl[MAXDER]; int n; } Spec; /* base: 0..7 reg, 8 s, 9 a, 10 g */
static DWORD delta;            /* actual c2 base - imgbase */
static DWORD imgbase = 0x10b00000, REGNAME = 0x10cec7b8, OPNAME = 0x10ceb3d8;   /* 14.00.50727.762; `set` changes them */
static DWORD OPCOUNT = 0xffffffff;   /* entries in the opcode-name table (13.10: machine opcodes only) */
/* tuple layout, `set blk|len|mem`: BLOCK tuple's BB*, an instruction's encoded-length byte, a memory operand's
 * {sym, disp, base leaf, index leaf} (13.10: 0x18, 0x16, 0x2c) */
static int LBLK = 0x20, LLEN = 0x1c, LMEM = 0x24;
static char opx[256][20];      /* `op <hex> <name>`: names of opcodes >= OPCOUNT */
typedef struct {
    int id; DWORD rva; int steal; int exitp; int ne, nx;
    int ni; int ilen[8], kind[8], cc[8]; DWORD tgt[8];   /* displaced instrs: kind 0 copy, 1 jcc, 2 jmp, 3 call */
    Spec e[MAXSPEC], x[MAXSPEC];
} Hook;
typedef struct { int hook; DWORD esp; DWORD ret; DWORD save[NSAVE]; } Frame;

static Hook hooks[MAXHOOK];
static int nhooks;
static Frame frames[MAXDEPTH];
static int depth;
static unsigned long seq, maxev = 0xffffffff;
static HANDLE logf = INVALID_HANDLE_VALUE;
static char buf[1 << 16];
static int blen;
static int eachflush;
static BYTE *exits;           /* per-depth exit stubs */

static void flush(void) {
    DWORD w;
    if (blen && logf != INVALID_HANDLE_VALUE) WriteFile(logf, buf, blen, &w, NULL);
    blen = 0;
}
static void out(const char *fmt, ...) {
    va_list ap; int n;
    if (blen > (int)sizeof buf - 512) flush();
    va_start(ap, fmt);
    n = _vsnprintf(buf + blen, sizeof buf - blen - 1, fmt, ap);
    va_end(ap);
    if (n > 0) blen += n;
}

static int readd(DWORD a, DWORD *v) {
    __try { *v = *(DWORD *)a; return 1; } __except (EXCEPTION_EXECUTE_HANDLER) { return 0; }
}

static const char *regn[8] = { "edi", "esi", "ebp", "esp", "ebx", "edx", "ecx", "eax" };

static DWORD pend_il; static int pend_max; static int ilmax = 2000000;
static int pend_x;            /* '%+' / '!+': extended dump (dumpilx) */
static void emit(Spec *s, int ns, Regs *r, DWORD *stk, Frame *f) {
    int i, j, k;
    for (i = 0; i < ns; i++) {
        DWORD v = 0; int ok = 1;
        if (s[i].base < 8) v = ((DWORD *)r)[s[i].base];
        else if (s[i].base == 8) ok = stk ? readd((DWORD)(stk + s[i].idx), &v) : 0;
        else if (s[i].base == 10) v = s[i].idx + delta;
        else ok = f && s[i].idx < NSAVE ? (v = f->save[s[i].idx], 1) : 0;
        for (j = 0; ok && j < s[i].nder; j++)
            ok = s[i].tbl[j] ? readd((DWORD)s[i].der[j] + delta + 4 * v, &v) : readd(v + s[i].der[j], &v);
        if (!ok) { out(" ?"); continue; }
        if (s[i].n <= -2 && s[i].n >= -5) { out(" IL"); pend_il = v; pend_x = s[i].n < -3; pend_max = s[i].n & 1 ? 1 : ilmax; continue; }
        if (s[i].n < 0) {           /* '$': C string at v */
            char c[80]; int m = 0; DWORD w = 0;
            while (m < 79 && readd(v + m, &w) && (c[m] = (char)w) != 0) { if (c[m] <= ' ' || c[m] > '~' || strchr("[]|\"", c[m])) c[m] = '.'; m++; }
            c[m] = 0; out(" \"%s\"", c);
        } else if (s[i].n) {
            out(" [");
            for (k = 0; k < s[i].n; k++) { DWORD w; if (readd(v + 4 * k, &w)) out(k ? " %x" : "%x", w); else out(k ? " ?" : "?"); }
            out("]");
        } else out(" %x", v);
    }
}


static int rstr(DWORD a, char *c, int max) {
    int m = 0; DWORD w = 0;
    while (m < max - 1 && readd(a + m, &w) && (c[m] = (char)w) != 0) { if (c[m] <= ' ' || c[m] > '~') c[m] = '.'; m++; }
    c[m] = 0; return m;
}
static BYTE rb(DWORD a) { DWORD w = 0; readd(a, &w); return (BYTE)w; }
static DWORD rd(DWORD a) { DWORD w = 0; readd(a, &w); return w; }
static void symname(DWORD x) {
    char c[64]; BYTE k;
    if (!x) { out("0"); return; }
    k = rb(x + 4);
    if (k == 1) { rstr(rd(REGNAME + delta + 4 * rd(x + 0x1c)), c, sizeof c); out("%s", c); return; }
    if (k == 2) { out("^"); x = rd(x); if (!x) { out("?"); return; } k = rb(x + 4); }
    if (rd(x) && rstr(rd(rd(x) + 4), c, sizeof c)) out("%s", c);
    else out("%c%d", k == 3 ? 't' : k == 0xd ? 'k' : 's', rd(x + 0x1c));
}
static int FENAME = 0;         /* `set fename 1` (13.10): CODEADDR leaves print the front-end symbol's name at sym+4 */
static int cdname(DWORD s) {   /* a call / tail-jump target: its symbol is the front end's, name pointer at +4 ([sym+0]
                                * is not a name record there: 13.10 printed another symbol's name or s0) */
    char c[80]; DWORD p, t;
    if (!s) return 0;
    t = rd(s + 0x33);
    if (t && rb(t + 8) == 27) return 0;                 /* a label (branch target): keep the old text */
    p = rd(s + 4);
    if (!p || !rstr(p, c, sizeof c) || !(c[0] == '_' || c[0] == '?' || c[0] == '@' || isalpha((BYTE)c[0]))) return 0;
    out("%s", c); return 1;
}
static void leaf(DWORD l) {
    BYTE k = rb(l + 8);
    DWORD a, b;
    switch (k) {
    case 1: a = rd(l + 0x18); b = rd(l + 0x1c);
            if (b && b != a && rb(b + 4) == 1) { symname(b); out("="); }
            symname(a); break;
    case 4: if (FENAME && cdname(rd(l + 0x18))) break;  /* fall through */
    case 2: case 3: symname(rd(l + 0x18)); break;
    case 7: out("%d", rd(l + 0x18)); break;
    case 9: out("fconst"); break;
    case 5: case 6:
        out("[");
        if (rd(l + LMEM)) { symname(rd(l + LMEM)); out("+"); }
        if (rd(l + LMEM + 8)) { leaf(rd(l + LMEM + 8)); out("+"); }
        if (rd(l + LMEM + 12)) { leaf(rd(l + LMEM + 12)); out("*%d+", 1 << (rb(l + 0x14) & 15)); }
        out("%d]", (int)rd(l + LMEM + 4)); break;
    case 11: out("regset"); break;
    case 12: out("pas%d", rd(l + 0x18)); break;
    default: out("k%d", k);
    }
    out(":%x", rd(l + 0x08) >> 16 & 0xffff);
}
static void oplist(DWORD l) {
    int n = 0;
    while (l && n++ < 16) { out(" "); leaf(l); l = rd(l); }
}
static void dumpil(DWORD t, int max) {
    int n = 0; char c[48];
    while (t && n++ < max) {
        BYTE k = rb(t + 8), f = rb(t + 9);
        DWORD op = rd(t + 4);
        if (k == 26) out("T B %x\n", rd(t + LBLK));
        else if (k == 27) out("T L %x\n", t);
        else if (f & 1) {
            if (op >= OPCOUNT && op - OPCOUNT < 256 && opx[op - OPCOUNT][0]) strcpy(c, opx[op - OPCOUNT]);
            else if (op >= OPCOUNT || !rstr(rd(OPNAME + delta + 4 * op), c, sizeof c)) sprintf(c, "op%x", op);
            out("T I %x %s %d %d |", t, c, rd(t + 0x14) & 0xffff, rb(t + LLEN));
            oplist(rd(t + 0x2c)); out(" <-"); oplist(rd(t + 0x28));
            if (k == 18 && rb(rd(t + 0x28) + 8) == 4) { out(" ->"); }
            out("\n");
        }
        if (blen > (int)sizeof buf - 1024) flush();
        t = rd(t);
    }
}

/* ---- extended IL dump ('%+' / '!+' specs): what the plain dump leaves out (docs/c2/tracer.md, `il`).
 * Adds to T I lines, after " ;": cc=<x86 cc> (BRANCH, QUESTION), sN=/dN= notes on the Nth src/dst operand
 * (& address leaf, <name> code address, L<label tuple>, float constant value), intrin=, default=/cases= (SWITCH),
 * eh{..} (EH record). New lines: T L <tuple> [name], T P <tuple> <opcode> <+0x20 word> [name] (non-instruction
 * tuples), T C <switch> <low> <high> L<label> (case list), T A <line> (unwind action of the preceding OPPUSHSTATE).
 * Layout (`set`): ext = per-kind word after the operand lists (QUESTION cc, SWITCH default, EH record; cases at
 * ext+4), labtup = label SSR -> its LABEL tuple, brcc = BRANCH condition-code nibble, ehop / ulabop = the
 * opcode numbers of OPPUSHSTATE and OPUSERLABEL. */
static int LEXT = 0x30, LLABT = 0x33, LBRCC = 0x0a;
static DWORD OPEH = 0x2db, OPULAB = 0x300;   /* opcode numbers: OPPUSHSTATE (.. OPSETSTATE = +9), OPUSERLABEL */
static int xdepth;
static const char *ccname[17] = { "0", "e", "ne", "l", "g", "le", "ge", "b", "a", "be", "ae", "s", "ns", "p", "np", "o", "no" };
static void xopname(DWORD op, char *c, int max) {
    if (op >= OPCOUNT && op - OPCOUNT < 256 && opx[op - OPCOUNT][0]) strcpy(c, opx[op - OPCOUNT]);
    else if (op >= OPCOUNT || !rstr(rd(OPNAME + delta + 4 * op), c, max)) sprintf(c, "op%x", op);
}
static int ssrname(DWORD s, const char *pre) {   /* front-end symbol (CODEADDR leaves, labels): name pointer at +4 */
    char c[80]; DWORD p = s ? rd(s + 4) : 0;
    if (p && rstr(p, c, sizeof c)) { out("%s%s", pre, c); return 1; }
    return 0;
}
static DWORD labtup(DWORD s) { DWORD l = s ? rd(s + LLABT) : 0; return l && rb(l + 8) == 27 ? l : 0; }
static void xfconst(DWORD l) {         /* FCONST leaf: 80-bit x87 value at +0x18 */
    DWORD w[3]; double d;
    if (!readd(l + 0x18, &w[0]) || !readd(l + 0x1c, &w[1]) || !readd(l + 0x20, &w[2])) { out("?"); return; }
    __asm fld tbyte ptr w
    __asm fstp qword ptr d
    out((rd(l + 8) >> 16 & 0xfff) == 4 ? "%.9g" : "%.17g", d);
}
static int xehinfo(DWORD e) {          /* 1 if e looks like an ehinfo (kind byte 1..15, small state) */
    DWORD k = e ? rd(e + 0x10) & 0xff : 0;
    return k && k < 16 && (rd(e + 0x14) < 0x10000 || rd(e + 0x14) == 0xffffffff);
}
static void xnotes(DWORD t, BYTE k, DWORD op) {
    int side, i, any = 0; DWORD l, s, r, e;
#define NOTE (any++ ? out(" ") : out(" ; "))
    if (k == 18 && (rb(t + LBRCC) & 0xf)) { NOTE; out("cc=%s", ccname[rb(t + LBRCC) & 0xf]); }
    if (k == 17 && rb(t + LEXT) <= 16) { NOTE; out("cc=%s", ccname[rb(t + LEXT)]); }
    if (k == 19) { NOTE; out("intrin=%d", rd(t + LEXT) & 0xffff); }
    for (side = 0; side < 2; side++)
        for (i = 0, l = rd(t + (side ? 0x2c : 0x28)); l && i < 16; i++, l = rd(l)) {
            BYTE lk = rb(l + 8);
            if (lk != 3 && lk != 4 && lk != 5 && lk != 9) continue;
            NOTE; out("%c%d=", side ? 'd' : 's', i);
            if (lk == 9) xfconst(l);
            else if (lk != 4) out("&");
            else if ((s = labtup(rd(l + 0x18))) != 0) out("L%x", s);
            else if (!ssrname(rd(l + 0x18), "")) out("?");
        }
    if (k == 20) {
        for (i = 0, l = rd(t + LEXT + 4); l && i < 100000; i++) l = rd(l);
        NOTE; out("default=L%x cases=%d", rd(t + LEXT), i);
    }
    if (k == 21 && (op >= OPEH && op <= OPEH + 9) && (r = rd(t + LEXT)) != 0) {
        NOTE; e = rd(r + 4);
        if (op == OPEH + 1 || op == OPEH + 2) out("eh{n=%d fl=%x", rd(r), rd(r + 8));
        else if (op == OPEH) out("eh{fl=%x", rd(r + 8));
        else if (op == OPEH + 8) out("eh{flag=%x", rd(r));
        else if (op == OPEH + 5 && !xehinfo(e)) {       /* reader form: catch object, type descriptor, flags */
            out("eh{obj="); if (!ssrname(rd(r), "")) out("%x", rd(r));
            out(" type="); if (!ssrname(e, "")) out("%x", e);
            out(" fl=%x", rd(r + 8));
        } else out("eh{w=%x,%x,%x", rd(r), e, rd(r + 8));
        if (xehinfo(e)) {
            out(" state=%d to=%d kind=%d", rd(e + 0x14), rd(e + 0x18), rd(e + 0x10) & 0xff);
            if (op == OPEH + 8 && xehinfo(rd(r + 0xc))) out(" next=%d", rd(rd(r + 0xc) + 0x14));
        }
        if (op != OPEH + 8 && op > OPEH + 2 && rd(r + 0xc)) out(" try=%x", rd(r + 0xc));
        out("}");
    }
#undef NOTE
}
static void dumpilx(DWORD t, int max, const char *pf) {
    int n = 0, i; char c[48]; DWORD l, r, e;
    while (t && n++ < max) {
        BYTE k = rb(t + 8), f = rb(t + 9);
        DWORD op = rd(t + 4);
        if (k == 26) out("%sB %x\n", pf, rd(t + LBLK));
        else if (k == 27) { out("%sL %x", pf, t); ssrname(rd(t + 0x24), " "); out("\n"); }
        else if (f & 1) {
            xopname(op, c, sizeof c);
            out("%sI %x %s %d %d |", pf, t, c, rd(t + 0x14) & 0xffff, rb(t + LLEN));
            oplist(rd(t + 0x2c)); out(" <-"); oplist(rd(t + 0x28));
            if (k == 18 && rb(rd(t + 0x28) + 8) == 4) { out(" ->"); }
            xnotes(t, k, op);
            out("\n");
            if (k == 20)
                for (i = 0, l = rd(t + LEXT + 4); l && i < 4096; i++, l = rd(l)) {
                    out("%sC %x %d %d L%x\n", pf, t, rd(l + 0xc), rd(l + 0x10), rd(l + 0x14));
                    if (blen > (int)sizeof buf - 1024) flush();
                }
            if (k == 21 && op == OPEH && !xdepth && (r = rd(t + LEXT)) != 0) {
                e = rd(r + 4);            /* unwind action: reader form record+0, later the ehinfo's funclet body */
                l = rd(r);
                if (!l && xehinfo(e) && rd(e + 0x34)) l = rd(rd(rd(rd(e + 0x34) + 8)) + 0x1c);
                if (l) { xdepth++; dumpilx(l, 64, "T A "); xdepth--; }
            }
        } else if (k >= 13) {
            xopname(op, c, sizeof c);
            out("%sP %x %s %x", pf, t, c, rd(t + LBLK));
            if (op == OPULAB) ssrname(rd(t + LBLK), " ");
            out("\n");
        }
        if (blen > (int)sizeof buf - 1024) flush();
        t = rd(t);
    }
}

/* called from the entry trampoline: pushfd; pushad; push esp; push i; call */
static void __cdecl on_entry(int i, Regs *r) {
    Hook *h = &hooks[i];
    DWORD *stk = &r->slot;             /* esp at function entry: [0] = return address */
    DWORD esp0 = (DWORD)stk;
    Frame *f = NULL;
    int k;
    while (depth > 0 && frames[depth - 1].esp <= esp0) depth--;   /* frames abandoned by longjmp */
    if (h->exitp && depth < MAXDEPTH) {
        f = &frames[depth];
        f->hook = i; f->esp = esp0; f->ret = stk[0];
        for (k = 0; k < NSAVE; k++) if (!readd((DWORD)(stk + k), &f->save[k])) f->save[k] = 0;
        stk[0] = (DWORD)(exits + 10 * depth);
        depth++;
    }
    if (seq < maxev) {
        out("E %lu %d %d %x %x %x %x %x %x %x %x |", seq, f ? depth - 1 : depth, h->id,
            r->eax, r->ecx, r->edx, r->ebx, r->ebp, r->esi, r->edi, esp0);
        emit(h->e, h->ne, r, stk, f);
        out("\n");
        if (pend_il) { if (pend_x) dumpilx(pend_il, pend_max, "T "); else dumpil(pend_il, pend_max); pend_il = 0; }
        if (eachflush) flush();
    }
    seq++;
}

/* called from the common exit stub: (push k) ; pushfd; pushad; push esp; call.  r->slot = k, becomes ret */
static void __cdecl on_exit_(Regs *r) {
    int k = (int)r->slot;
    Frame *f = &frames[k];
    Hook *h = &hooks[f->hook];
    r->slot = f->ret;
    depth = k;
    if (seq < maxev) {
        out("X %lu %d %d %x %x |", seq, k, h->id, r->eax, r->edx);
        emit(h->x, h->nx, r, NULL, f);
        out("\n");
        if (eachflush) flush();
    }
    seq++;
}

static BYTE *put32(BYTE *p, DWORD v) { *(DWORD *)p = v; return p + 4; }
static BYTE *rel(BYTE *p, BYTE op, void *to) { *p++ = op; return put32(p, (DWORD)to - (DWORD)(p + 4)); }

static int parsespec(const char *t, Spec *s) {
    char *e; int i;
    memset(s, 0, sizeof *s);
    for (i = 0; i < 8; i++) if (!strncmp(t, regn[i], 3)) { s->base = i; t += 3; break; }
    if (i == 8) {
        if (*t == 's') s->base = 8; else if (*t == 'a') s->base = 9; else if (*t == 'g') s->base = 10; else return 0;
        s->idx = strtoul(t + 1, &e, s->base == 10 ? 16 : 0); t = e;
    }
    while ((*t == '>' || *t == '~') && s->nder < MAXDER) {
        s->tbl[s->nder] = *t == '~';
        s->der[s->nder] = *t == '~' ? (int)strtoul(t + 1, &e, 16) : strtol(t + 1, &e, 0);
        s->nder++; t = e;
    }
    if (*t == '#') s->n = strtol(t + 1, &e, 0);
    else if (*t == '$') s->n = -1;
    else if (*t == '%') s->n = t[1] == '+' ? -4 : -2;
    else if (*t == '!') s->n = t[1] == '+' ? -5 : -3;
    return 1;
}

static void setvar(const char *nm, DWORD v) {
    if (!strcmp(nm, "base")) imgbase = v;
    else if (!strcmp(nm, "regname")) REGNAME = v;
    else if (!strcmp(nm, "opname")) OPNAME = v;
    else if (!strcmp(nm, "opcount")) OPCOUNT = v;
    else if (!strcmp(nm, "blk")) LBLK = (int)v;
    else if (!strcmp(nm, "len")) LLEN = (int)v;
    else if (!strcmp(nm, "mem")) LMEM = (int)v;
    else if (!strcmp(nm, "ext")) LEXT = (int)v;
    else if (!strcmp(nm, "labtup")) LLABT = (int)v;
    else if (!strcmp(nm, "brcc")) LBRCC = (int)v;
    else if (!strcmp(nm, "ehop")) OPEH = v;
    else if (!strcmp(nm, "ulabop")) OPULAB = v;
    else if (!strcmp(nm, "fename")) { FENAME = (int)v; out("# fename %d\n", FENAME); }  /* marker: this DLL knows it */
}

static void install(BYTE *base) {
    char path[MAX_PATH], line[512];
    FILE *fp;
    BYTE *code, *p, *common;
    int i, k;
    DWORD old;
    if (!GetEnvironmentVariableA("C2TRACE_CFG", path, sizeof path)) return;
    fp = fopen(path, "r");
    if (!fp) return;
    while (fgets(line, sizeof line, fp)) {
        char a[64]; Hook *h = nhooks ? &hooks[nhooks - 1] : NULL;
        if (sscanf(line, "%63s", a) != 1 || a[0] == '#') continue;
        if (!strcmp(a, "hook") && nhooks < MAXHOOK) {
            h = &hooks[nhooks];
            memset(h, 0, sizeof *h);
            if (sscanf(line, "hook %d %lx %d %d", &h->id, &h->rva, &h->steal, &h->exitp) == 4) nhooks++;
        } else if (!strcmp(a, "e") && h && h->ne < MAXSPEC) {
            sscanf(line, "e %63s", a); if (parsespec(a, &h->e[h->ne])) h->ne++;
        } else if (!strcmp(a, "x") && h && h->nx < MAXSPEC) {
            sscanf(line, "x %63s", a); if (parsespec(a, &h->x[h->nx])) h->nx++;
        } else if (!strcmp(a, "i") && h && h->ni < 8) {
            int k = h->ni;
            if (sscanf(line, "i %d %d %d %lx", &h->ilen[k], &h->kind[k], &h->cc[k], &h->tgt[k]) == 4) h->ni++;
        } else if (!strcmp(a, "max")) {
            sscanf(line, "max %lu", &maxev);
        } else if (!strcmp(a, "ilmax")) {
            sscanf(line, "ilmax %d", &ilmax);
        } else if (!strcmp(a, "set")) {
            char nm[32]; unsigned long v;
            if (sscanf(line, "set %31s %lx", nm, &v) == 2) setvar(nm, v);
        } else if (!strcmp(a, "op")) {
            char nm[20]; unsigned long v;
            if (sscanf(line, "op %lx %19s", &v, nm) == 2 && v >= OPCOUNT && v - OPCOUNT < 256) strcpy(opx[v - OPCOUNT], nm);
        } else if (!strcmp(a, "flush")) eachflush = 1;
    }
    fclose(fp);
    if (GetEnvironmentVariableA("C2TRACE_LOG", path, sizeof path))
        logf = CreateFileA(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    delta = (DWORD)base - imgbase;
    out("# c2trace base %x hooks %d\n", (DWORD)base, nhooks);

    code = (BYTE *)VirtualAlloc(NULL, 64 * MAXHOOK + 10 * MAXDEPTH + 64, MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    if (!code) return;
    /* common exit stub */
    common = p = code;
    *p++ = 0x9c; *p++ = 0x60; *p++ = 0x54;          /* pushfd; pushad; push esp */
    p = rel(p, 0xe8, (void *)on_exit_);
    *p++ = 0x83; *p++ = 0xc4; *p++ = 0x04;          /* add esp,4 */
    *p++ = 0x61; *p++ = 0x9d; *p++ = 0xc3;          /* popad; popfd; ret */
    exits = code + 32;
    for (k = 0; k < MAXDEPTH; k++) {                /* push k ; jmp common  (10 bytes each) */
        p = exits + 10 * k;
        *p = 0x68; put32(p + 1, k);
        rel(p + 5, 0xe9, common);
    }
    p = exits + 10 * MAXDEPTH;
    for (i = 0; i < nhooks; i++) {
        Hook *h = &hooks[i];
        BYTE *tgt = base + h->rva, *tr = p;
        if (h->steal < 5 || h->steal > 24) continue;
        *p++ = 0x9c; *p++ = 0x60; *p++ = 0x54;      /* pushfd; pushad; push esp */
        *p++ = 0x68; p = put32(p, i);               /* push i */
        p = rel(p, 0xe8, (void *)on_entry);
        *p++ = 0x83; *p++ = 0xc4; *p++ = 0x08;      /* add esp,8 */
        *p++ = 0x61; *p++ = 0x9d;                   /* popad; popfd */
        if (!h->ni) { memcpy(p, tgt, h->steal); p += h->steal; }     /* displaced prologue */
        else {
            BYTE *src = tgt; int j;
            for (j = 0; j < h->ni; j++) {       /* rel branches re-encoded as rel32 to the same target */
                BYTE *to = base + h->tgt[j];
                if (h->kind[j] == 0) { memcpy(p, src, h->ilen[j]); p += h->ilen[j]; }
                else if (h->kind[j] == 1) { *p++ = 0x0f; p = rel(p, (BYTE)(0x80 + h->cc[j]), to); }
                else p = rel(p, h->kind[j] == 2 ? 0xe9 : 0xe8, to);
                src += h->ilen[j];
            }
        }
        p = rel(p, 0xe9, tgt + h->steal);
        VirtualProtect(tgt, h->steal, PAGE_EXECUTE_READWRITE, &old);
        rel(tgt, 0xe9, tr);
        for (k = 5; k < h->steal; k++) tgt[k] = 0xcc;
        VirtualProtect(tgt, h->steal, old, &old);
    }
    FlushInstructionCache(GetCurrentProcess(), NULL, 0);
    flush();
}

BOOL WINAPI DllMain(HINSTANCE hi, DWORD reason, LPVOID res) {
    if (reason == DLL_PROCESS_ATTACH) {
        HMODULE c2 = GetModuleHandleA("c2.dll");
        DisableThreadLibraryCalls(hi);
        if (c2) install((BYTE *)c2);
    } else if (reason == DLL_PROCESS_DETACH) {
        if (logf != INVALID_HANDLE_VALUE) {
            out("# end seq %lu\n", seq);
            flush(); CloseHandle(logf); logf = INVALID_HANDLE_VALUE;
        }
    }
    return TRUE;
}
'''

def cave():
    c = CAVE_RVA
    code = bytes.fromhex("837c240801" "7515" "60" "e800000000" "5b")
    code += b"\x8d\x83" + struct.pack("<i", (c + 0x21) - (c + 0xd))          # lea eax,[ebx+str]
    code += b"\x50"                                                         # push eax
    code += b"\xff\x93" + struct.pack("<i", IAT_LOADLIBRARYA - (c + 0xd))   # call [ebx+IAT]
    code += b"\x61"                                                         # popad
    code += b"\xe9" + struct.pack("<i", ENTRY_RVA - (c + 0x21))              # jmp original entry
    assert len(code) == 0x21
    return code + b"c2trace.dll\0"


def patch_c2(data):
    data = bytearray(data)
    lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    opt = lfanew + 24
    nsec = struct.unpack_from("<H", data, lfanew + 6)[0]
    shdr = opt + struct.unpack_from("<H", data, lfanew + 20)[0]
    assert data[shdr:shdr + 5] == b".text" and nsec == NSEC
    assert struct.unpack_from("<I", data, opt + 16)[0] == ENTRY_RVA, "c2.dll is not the pristine %s build" % (
        c2build.get("version", "50727.762"))
    assert struct.unpack_from("<I", data, shdr + 8)[0] == TEXT_HDR_VSIZE
    co = CAVE_RVA - FILE_DELTA                                             # 14.00 .text: file offset == RVA
    assert data[co:co + 0x40] == bytes(0x40)
    cv = cave()
    data[co:co + len(cv)] = cv
    struct.pack_into("<I", data, opt + 16, CAVE_RVA)                       # AddressOfEntryPoint
    struct.pack_into("<I", data, shdr + 8, CAVE_RVA + 0x40 - 0x1000)       # .text VirtualSize covers the cave
    import pefile
    pe = pefile.PE(data=bytes(data), fast_load=True)
    struct.pack_into("<I", data, opt + 64, pe.generate_checksum())
    return bytes(data)


def _replace(path, data):
    """Write a file by rename, so a compile that has the old one mapped (another agent's, under wine) keeps it."""
    tmp = path + ".new%d" % os.getpid()
    open(tmp, "wb").write(data)
    os.replace(tmp, path)


def install():
    if not os.path.isdir(TRACE):
        sys.exit("missing %s: cp -RL work/toolchain/vs2005 work/toolchain/vs2005-trace" % TRACE)
    orig = open(ORIG_C2, "rb").read()
    if c2build.table() and c2build.sha256(ORIG_C2) != c2build.get("c2_sha256"):
        sys.exit("%s is not c2.dll %s" % (ORIG_C2, c2build.get("version")))
    _replace(os.path.join(TBIN, "c2.dll"), patch_c2(orig))
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "c2trace.c")
        open(src, "w").write(DLL_SRC)
        rc, out = run_tool(["/nologo", "/O2", "/MT", "/LD", "/W2", src, "/Fo" + os.path.join(d, "c2trace.obj"),
                            "/Fe" + os.path.join(d, "c2trace.dll")], trace=False)
        if rc:
            sys.exit(out)
        _replace(os.path.join(TBIN, "c2trace.dll"), open(os.path.join(d, "c2trace.dll"), "rb").read())
    print("patched %s (entry -> cave at rva %x), built %s" % (os.path.join(TBIN, "c2.dll"), CAVE_RVA,
                                                             os.path.join(TBIN, "c2trace.dll")))


def run_tool(args, trace=True, extra_env=None):
    e = os.environ.copy()
    if trace:
        e["VS8_ROOT"] = TRACE
    e.update(extra_env or {})
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "msvc.py")] + args, env=e,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace")
    return p.returncode, p.stdout + p.stderr


# A compile server on a Windows machine, used for every traced compile when it is reachable (C2TRACE_REMOTE=0 turns it
# off, C2TRACE_REMOTE=PATH names another client). Mount & Blade's client: it needs ~/.config/mb-compile-token and
# -server, and the traced compiler directories installed on the server (D:\\mb\\rc\\tc\\<trace directory's name>).
REMOTE_DEFAULT = os.path.join(os.path.dirname(ROOT), "mountandblade-decomp", "tools", "remote", "rcl.py") \
    if os.path.isfile(os.path.expanduser("~/.config/mb-compile-token")) else ""   # 13.10 builds only (run_remote)


def run_remote(args, cfg, log):
    """The traced compile on a Windows machine instead of under wine, or None to compile here.

    C2TRACE_REMOTE names the client of a compile server that has this traced compiler installed (for Mount & Blade:
    ../mountandblade-decomp/tools/remote/rcl.py, whose server holds D:\\mb\\rc\\tc\\<the trace directory's name>). The
    client ships the source, the headers it reaches and the hook list, and writes the listing, object and trace log
    back to the paths asked for. It exits 75 (busy or unreachable) or 76 (cannot run there: /Zi, a header it could not
    ship, an anonymous namespace, a compiler the server lacks); both mean compile here."""
    client = os.environ.get("C2TRACE_REMOTE", REMOTE_DEFAULT)
    if os.environ.get("C2TRACE_REMOTE") == "0":
        return None
    if not client or not os.path.isfile(client) or any(a in ("/Zi", "/ZI", "/Z7") for a in args):
        return None
    if any(a[:3].upper() == "/GL" for a in args):
        return None                                    # /GL objects belong to the local link (c2ltcg)
    if not c2build.name().startswith("13.10"):
        return None                                    # the server's INCLUDE holds 13.10 headers only (shipping another
                                                       # build's headers with /X /I made every compile time out)
    # not msvc.winpath: it resolves symlinks, and the client tells a toolchain header (already on the server) from a
    # file to ship by the path as written
    wp = lambda q: "Z:" + os.path.abspath(q).replace("/", "\\")
    w = []
    for a in args:
        if a[:3] in ("/Fd", "-Fd"):
            continue                                   # only /Zi writes a PDB, and /Zi compiles stay here
        if a[:2] in ("/I", "-I") and len(a) > 2 and a[2] != "{":
            a = "/I" + wp(a[2:])
        elif a[:3] in ("/Fa", "/Fo") and len(a) > 3:
            a = a[:3] + wp(a[3:])
        elif a[:1] not in "/-" or os.path.isfile(a):   # a source: an absolute path here starts with "/" like an option
            a = wp(a)
        if a[:2] == "/E" and a not in ("/E", "/EP"):
            a = "-" + a[1:]                            # the client reads every /E... as "preprocess": -EHsc is the same option
        w.append(a)
    e = dict(os.environ, MB_RC_TC=os.path.basename(TRACE), MB_RC_TRACE_CFG=cfg, MB_RC_TRACE_LOG=log)
    for attempt in range(40):                          # 75: every slot there is taken (or a connect timed out); wait, a
        p = subprocess.run([sys.executable, client] + w, env=e, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           errors="replace")           # compile there is far cheaper than one more wine process here
        if p.returncode != 75:
            break
        time.sleep(0.5 + 0.1 * attempt)
    if p.returncode in (75, 76) or not os.path.exists(log):
        if os.environ.get("C2TRACE_REMOTE_DEBUG"):
            sys.stderr.write("c2trace: remote compile declined (%d), compiling here\n%s" % (
                p.returncode, (p.stdout + p.stderr)[-1500:]))
        return None
    if not _REMOTE_SAID:
        _REMOTE_SAID.append(1)
        sys.stderr.write("c2trace: traced 13.10 compiles run on the remote compile server (%s); C2TRACE_REMOTE=0 keeps "
                         "them local\n" % os.path.basename(client))
    return p.returncode, p.stdout + p.stderr


_REMOTE_SAID = []


def steal_len(img, va):
    """Whole prologue instructions (>= 5 bytes) to move into the trampoline: (length, [(len, kind, cc, target)]).
    Relative jcc/jmp/call are re-encoded by the DLL (kind 1/2/3); anything else that is eip-relative is refused."""
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    n, ins = 0, []
    for i in md.disasm(img.read(va, 32), va):
        op = i.bytes
        imm = i.operands[0].imm if i.operands and i.operands[0].type == capstone.x86.X86_OP_IMM else None
        if i.group(capstone.CS_GRP_RET) or i.group(capstone.CS_GRP_INT) or (
                (i.group(capstone.CS_GRP_JUMP) or i.group(capstone.CS_GRP_CALL)) and imm is None) or \
                i.mnemonic in ("jecxz", "jcxz", "loop", "loope", "loopne"):
            raise SystemExit("%08x: cannot displace %s %s from the hooked prologue" % (i.address, i.mnemonic, i.op_str))
        if i.group(capstone.CS_GRP_CALL):
            kind, cc = 3, 0
        elif i.mnemonic == "jmp":
            kind, cc = 2, 0
        elif i.group(capstone.CS_GRP_JUMP):
            kind, cc = 1, (op[0] - 0x70) if 0x70 <= op[0] <= 0x7f else (op[1] - 0x80)
        else:
            kind, cc = 0, 0
        ins.append((i.size, kind, cc, (imm - img.base) if kind else 0))
        n += i.size
        if n >= 5 or kind == 2:
            break
    if n < 5:
        raise SystemExit("%08x: function too short to hook" % va)
    row = containing(img, va)
    if row:  # nothing in the function may branch into the moved bytes
        if row["va"] + row["size"] < va + n:
            raise SystemExit("%08x: the 5-byte detour would run past the end of %s" % (va, row["symbol"]))
        for i in md.disasm(img.read(row["va"], row["size"]), row["va"]):
            if i.group(capstone.CS_GRP_JUMP) and i.operands and i.operands[0].type == capstone.x86.X86_OP_IMM:
                if va < i.operands[0].imm < va + n:
                    raise SystemExit("%08x branches into the hooked prologue of %08x" % (i.address, va))
    return n, ins


def containing(img, va):
    import bisect
    k = bisect.bisect_right(img.starts, va) - 1
    r = img.by_va[img.starts[k]] if k >= 0 else None
    if c2build.table() and not (r and va < r["va"] + r["size"]) and img.base + 0x1000 <= va < img.base + TEXT_HDR_VSIZE + 0x1000:
        # a build without a PDB (functions scattered by BBT, sizes unknown): any .text address can be hooked;
        # the caller is responsible for it being an instruction that nothing branches into the middle of
        return dict(va=va, size=16, symbol="%08x" % va, flags="address")
    return r if r and va < r["va"] + r["size"] else None


def parse_hook(img, text):
    """NAME[+0xOFF] or VA, '=' entry specs, '/x' exit specs. A hook not at a function start is a probe:
    it logs registers/specs when that instruction is reached, sN are words at the current esp, no exit."""
    text, _, xspecs = text.partition("/x")
    name, _, specs = text.partition("=")
    base, _, off = name.partition("+")
    try:
        va = int(base, 16)
    except ValueError:
        r = img.func(base)
        if not r:
            sys.exit("no c2 function %s" % base)
        va = r["va"]
    va += int(off, 0) if off else 0
    fn = containing(img, va)
    if not fn:
        sys.exit("%08x is not inside a known c2 function" % va)
    entry = fn["va"] == va
    sym = fn["symbol"] + ("" if entry else "+0x%x" % (va - fn["va"]))
    loose = fn.get("flags") in ("probe", "address")     # another c2 build: a listed probe, or a bare address
    if loose:        # never given exit logging by --exit; a bare address counts as an entry only with /x specs
        entry = fn["flags"] == "address" and bool(xspecs)
    if xspecs and not entry:
        sys.exit("%s: exit specs need a function-entry hook" % sym)
    if not entry and not loose:  # a probe must sit on an instruction boundary (linear sweep of the function)
        md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
        if va not in {i.address for i in md.disasm(img.read(fn["va"], fn["size"]), fn["va"])}:
            sys.exit("%s (%08x) is not an instruction boundary" % (sym, va))
    return dict(va=va, name=sym, entry=entry, fn=fn, e=[s for s in specs.split(",") if s],
                x=[s for s in xspecs.split(",") if s])


def decode(log, hooks, raw):
    if raw:
        return log
    out = []
    for ln in log.splitlines():
        if ln.startswith("#") or ln.startswith("T ") or "|" not in ln:
            out.append(ln)
            continue
        head, _, rest = ln.partition("|")
        f = head.split()
        h = hooks[int(f[3])]
        vals = _split(rest)
        ind = "  " * int(f[2])
        if f[0] == "E":
            regs = dict(zip("eax ecx edx ebx ebp esi edi esp".split(), f[4:12]))
            sp = " ".join("%s=%s" % (s, v) for s, v in zip(h["e"], vals))
            out.append("%5s %s> %s  ecx=%s edx=%s eax=%s esp=%s%s" % (f[1], ind, h["name"], regs["ecx"], regs["edx"],
                                                                    regs["eax"], regs["esp"], "  " + sp if sp else ""))
        else:
            sp = " ".join("%s=%s" % (s, v) for s, v in zip(h["x"], vals))
            out.append("%5s %s< %s  eax=%s%s" % (f[1], ind, h["name"], f[4], "  " + sp if sp else ""))
    return "\n".join(out) + "\n"


def _split(s):
    out, cur, lvl, q = [], "", 0, False
    for ch in s.strip() + " ":
        if ch == '"':
            q = not q
        elif ch == "[" and not q:
            lvl += 1
        elif ch == "]" and not q:
            lvl -= 1
        if ch == " " and not lvl and not q:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    return out


def write_cfg(cfg, img, hooks, maxn=None, exit_all=False):
    """The trace config c2trace.dll reads (%C2TRACE_CFG%) for these parsed hooks."""
    with open(cfg, "w") as f:
        if maxn is not None:
            f.write("max %d\n" % maxn)
        if os.environ.get("C2TRACE_ILMAX"):
            f.write("ilmax %d\n" % int(os.environ["C2TRACE_ILMAX"]))
        if c2build.table():
            f.write("set base %s\n" % c2build.get("image_base"))
            f.writelines("set %s %s\n" % kv for kv in sorted(c2build.get("dll", {}).items()))
            f.writelines("op %s %s\n" % kv for kv in sorted(c2build.get("opnames", {}).items()))
        for k, h in enumerate(hooks):
            n, ins = steal_len(img, h["va"])
            f.write("# %s\nhook %d %x %d %d\n" % (h["name"], k, h["va"] - img.base, n, int(h["entry"] and (exit_all or bool(h["x"])))))
            if any(kind for _, kind, _, _ in ins):
                f.writelines("i %d %d %d %x\n" % t for t in ins)
            f.writelines("e %s\n" % s for s in h["e"])
            f.writelines("x %s\n" % s for s in h["x"])


def func_pat(pat):
    """The text a --func value is looked up as in decorated names (shared by every subcommand and the sweeps).
    `Class::method` -> `?method@Class@@` (nested: `N::C::m` -> `?m@C@N@@`; `C::C` / `C::~C` -> `??0C@@` / `??1C@@`);
    `::name` -> a global function (`?name@@` in C++ names; for C names use the plain form);
    anything else (a plain name, `method@Class`, a full decorated name) is returned unchanged: a substring."""
    if not pat or "::" not in pat:
        return pat
    parts = [x for x in pat.split("::") if x]
    if not parts:
        return pat
    name, scope = parts[-1], parts[:-1]
    if scope and name == scope[-1]:
        name = "?0"
    elif scope and name == "~" + scope[-1]:
        name = "?1"
    return "?" + "@".join([name] + scope[::-1]) + "@@"


def func_decl(pat):
    """A --func value as the start of a decorated member name where one is needed to find the definition in the
    source (rwsweep, helpersweep): `Class::method` and `method@Class` -> `?method@Class@@`; others unchanged."""
    pat = func_pat(pat)
    m = re.fullmatch(r"(\w+)@(\w+)", pat or "")
    return "?%s@%s@@" % m.groups() if m else pat


def func_match(pat, name):
    """True when the decorated `name` is selected by the --func value `pat` (None or '' selects everything)."""
    return not pat or func_pat(pat) in name


def listing_proc(text, func):
    """(start, end) of the PROC..ENDP text of the function selected by `func` (any --func form) in a /FAsc listing;
    the first match wins.  KeyError when there is none."""
    pat = func_pat(func) or ""
    for m in re.finditer(r"^(\S+)[ \t]+PROC\b", text, re.M):
        if pat in m.group(1):
            return m.start(), text.index("ENDP", m.end())
    raise KeyError("%s PROC not in the listing" % func)


def split_cl(argv):
    """(tool arguments, cl arguments): everything after the first `--` is passed to cl untouched, so the tool's own
    options may come anywhere before it (before or after the source path)."""
    if "--" in argv:
        k = argv.index("--")
        return argv[:k], argv[k + 1:]
    return argv, []


def run_trace(src, hook_texts, cl, maxn=None, keep=None, exit_all=False, quiet=False):
    """Compile src with the traced c2; returns (img, hooks, rc, cl output, raw log text).
    Relative `src` and `keep` paths are taken against the caller's cwd (resolved here, before anything runs elsewhere)."""
    src = os.path.abspath(src)
    keep = os.path.abspath(keep) if keep else keep
    if open(os.path.join(TBIN, "c2.dll"), "rb").read() == open(ORIG_C2, "rb").read() or \
            not os.path.exists(os.path.join(TBIN, "c2trace.dll")):
        sys.exit("traced toolchain not installed: uv run tools/c2trace.py install")
    img = Image("c2")
    hooks = [parse_hook(img, h) for h in hook_texts]
    d = keep or tempfile.mkdtemp(prefix="c2trace_")
    os.makedirs(d, exist_ok=True)
    cfg, log = os.path.join(d, "c2trace.cfg"), os.path.join(d, "c2trace.log")
    write_cfg(cfg, img, hooks, maxn, exit_all)
    if os.path.exists(log):
        os.remove(log)
    stem = os.path.splitext(os.path.basename(src))[0]
    asm, obj = os.path.join(d, stem + ".asm"), os.path.join(d, stem + ".obj")
    pdb = [] if any(a[:3] in ("/Fd", "-Fd") for a in cl) else ["/Fd" + os.path.join(d, stem + ".pdb")]
    # ^ with /Zi every compile would otherwise share vcNN.pdb in the cwd: parallel sweeps then fail with C1033
    args = ["/nologo", "/c", "/O2"] + cl + pdb + ["/FAsc", "/Fa" + asm, "/Fo" + obj, os.path.abspath(src)]
    k = ccache.key(src, args, [open(cfg).read(), ccache.file_sha(os.path.join(TBIN, "c2.dll")),
                               ccache.file_sha(os.path.join(TBIN, "c2trace.dll")), "trace"])
    hit = ccache.get(k)
    if hit:
        info = ccache.meta(hit)
        for name, dst in (("log", log), ("asm", asm), ("obj", obj)):
            if os.path.exists(os.path.join(hit, name)):
                shutil.copy(os.path.join(hit, name), dst)
        rc, out = info["rc"], info["out"]
    else:
        rc, out = run_remote(args, cfg, log) or \
            run_tool(args, extra_env={"C2TRACE_CFG": msvc.winpath(cfg), "C2TRACE_LOG": msvc.winpath(log)})
        if os.path.exists(log):
            ccache.put(k, {"log": log, "asm": asm, "obj": obj}, {"rc": rc, "out": out, "src": os.path.abspath(src)})
    if not os.path.exists(log):
        sys.stdout.write(out)
        sys.exit("no log: the compile failed before c2 ran (cl rc %d)" % rc if rc else
                 "no log written: c2trace.dll did not load or C2TRACE_* did not reach wine")
    text = open(log, errors="replace").read()
    if "\nset fename 1" in "\n" + open(cfg).read() and "# fename 1" not in text[:4096]:
        sys.stderr.write("c2trace: the c2trace.dll that ran this compile predates `set fename` (a compile server's copy: "
                         "redeploy %s there, or C2TRACE_REMOTE=0): call-target names in the plain IL dump are the old, "
                         "wrong ones\n" % TBIN)
    if keep:
        if not quiet:
            print("# kept %s" % d)
    else:
        shutil.rmtree(d, ignore_errors=True)
    return img, hooks, rc, out, text


def main():
    if sys.argv[1:2] == ["install"]:
        return install()
    if sys.argv[1:2] == ["scratch"]:
        import c2scratch
        return c2scratch.main(sys.argv[2:])
    if sys.argv[1:2] == ["merges"] or sys.argv[1:2] == ["il"] and "--merges" in split_cl(sys.argv[2:])[0]:
        import c2merges
        return c2merges.main(sys.argv[2:])
    if sys.argv[1:2] == ["il"]:
        import c2il
        return c2il.main(sys.argv[2:])
    if sys.argv[1:2] == ["regmap"]:
        import regmap
        return regmap.main(sys.argv[2:])
    if sys.argv[1:2] == ["regs"]:
        import c2regs
        return c2regs.main(sys.argv[2:])
    if sys.argv[1:2] == ["slots"]:
        import c2slots
        return c2slots.main(sys.argv[2:])
    ap = argparse.ArgumentParser(usage=__doc__.replace("%", "%%"))   # argparse %-formats the usage text
    ap.add_argument("-H", "--hook", action="append", default=[])
    ap.add_argument("--exit", action="store_true")
    ap.add_argument("--max", type=int)
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--keep")
    ap.add_argument("src")
    ap.add_argument("cl", nargs="*")     # cl arguments without `--` (c2ltcg.py raw passes them so)
    argv, cl = split_cl(sys.argv[1:])    # options anywhere before `--`; everything after it goes to cl unchanged
    a = ap.parse_args(argv)
    img, hooks, rc, out, text = run_trace(a.src, a.hook or DEFAULT_HOOKS, a.cl + cl, a.max, a.keep, a.exit)
    sys.stdout.write(out)
    sys.stdout.write(decode(text, hooks, a.raw))
    return rc


if __name__ == "__main__":
    sys.exit(main())
