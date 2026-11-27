#!/usr/bin/env python3
"""Trace c2.dll's own functions during a real compile.  Doc: docs/c2/tracer.md

usage: c2trace.py install                      build c2trace.dll and patch the traced toolchain copy's c2.dll
       c2trace.py il <src> --func S [--at P,P] [--grep RE | --count RE] [-- <cl args>]   IL dump at passes (c2il.py)
       c2trace.py il <src> --func S --diff PASS_A,PASS_B [-- <cl args>]   what the passes between A and B changed
       c2trace.py merges <src> --func S [--pairs] [-- <cl args>]   every tail/head merge and why each pair stopped
                                     (also `il --merges`; tools/c2merges.py)
       c2trace.py scratch <src> --func S [--why] [--color] [-- <cl args>]   scratch-register picks -> listing offsets, and
                                     which registers are Color's instead (C rows); --why: why each pick (c2scratch.py)
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
  --func S                           log only while function S is compiled (default: $C2TRACE_FUNC, else all)
  --unit SPEC | --f3pack DIR         (every tool) SRC is a candidate: compile the unit SPEC composes around it
                                     (tools/unitcompose.py; tracer.md "Composed units")
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
import unitcompose            # --unit SPEC / --f3pack DIR: SRC is a candidate, composed into its unit (tracer.md)
unitcompose.take_argv()


def _argv_func(argv):
    """The tool's own --func value (before any `--`), or None: the default of the tracer's function filter."""
    end = argv.index("--") if "--" in argv else len(argv)
    for k in range(1, end):
        if argv[k] == "--func" and k + 1 < end:
            return argv[k + 1]
        if argv[k].startswith("--func="):
            return argv[k][len("--func="):]
    return None


ARGV_FUNC = _argv_func(sys.argv)   # every tool that imports c2trace logs only its --func's compile by default
from image import Image, ROOT
import ccache, msvc

TRACE = os.path.join(ROOT, "work", "toolchain", "vs2005-trace")
TBIN = c2build.tcpath(TRACE, "VC", "Bin")
ORIG_C2 = os.path.join(ROOT, "Bin", "c2.dll")
CAVE_RVA = 0x1D7BB0          # .text slack: VirtualSize ends at RVA 0x1d7bab, raw data at 0x1d8000
TEXT_HDR_VSIZE = 0x1D6BAB
ENTRY_RVA = 0x1D706D         # __DllMainCRTStartup@12
IAT_LOADLIBRARYA = 0x1D8028  # KERNEL32!LoadLibraryA IAT slot (RVA)
FILE_DELTA, NSEC = 0, 5     # .text RVA - file offset; number of sections (build tables may override: patch)
LOADLIB_W = False
LOADLIB_EXW = False
DEFAULT_HOOKS = ["@Color@4=s1,s2", "@Spillcosts@12=s1,s2,s3"]
if c2build.table():           # another c2 build: its own toolchain copies and cave addresses (tools/c2builds/*.json)
    TRACE = c2build.toolchain(True)
    TBIN = c2build.tcpath(TRACE, "VC", "Bin")
    ORIG_C2 = c2build.orig_c2()
    CAVE_RVA, TEXT_HDR_VSIZE, ENTRY_RVA, IAT_LOADLIBRARYA = (int(c2build.get("patch")[k], 16) for k in (
        "cave_rva", "text_vsize", "entry_rva", "iat_loadlibrarya"))
    # optional: .text RVA minus file offset (0 when the file is section-aligned like 14.00), section count
    FILE_DELTA, NSEC = int(c2build.get("patch").get("file_delta", "0"), 16), int(c2build.get("patch").get("nsec", 5))
    LOADLIB_W = c2build.get("patch").get("loadlib") == "w"   # the IAT slot is LoadLibraryW (cl 17.00)
    LOADLIB_EXW = c2build.get("patch").get("loadlib") == "exw"   # LoadLibraryExW (the Xbox 360 cross c2)
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
 *   func <text>   (repeatable) log only while the decorated name of the function being compiled contains every
 *                 <text> (read at [[[_CurFunc]+0]+4]; `set curfunc <VA>` for another build); logs "# func <n>"
 *   ilmax <n>     cap on tuples per '%' IL dump (default 2000000; env C2TRACE_ILMAX on the Python side)
 *   set <name> <hex>  another c2 build (tools/c2build.py): base = preferred image base, regname / opname = the
 *                 _RegName / _TupOpcodeName tables, opcount = entries in the latter, blk / len / mem = tuple layout
 *                 (extended dump only: ext / extin / labtup / labsym / brcc / casen / casenx / fcoff / fc64 / ehop /
 *                 ulabop, see dumpilx; its tuple and leaf fields follow the plain dump's lop / lkind / lsrc ..);
 *                 fename 1 = the plain dump
 *                 prints a CODEADDR leaf (call target) by the front-end name at sym+4 (13.10; logs "# fename 1")
 *   op <hex> <name>   name of an opcode the build's own table lacks (after `set opcount`)
 *   set ilpas <off>  memory leaves also print "@<hex word at leaf+off>" (the PAS index; loadcse.py); logs "# ilpas"
 *   set fpdump 1 writes "# fpdump 1" (this DLL has the '&' dump); set bbin / bbstk: its BB offsets (13.10: 48, 60)
 * spec := base { '>' off } [ '#' n ]
 *   base: eax ecx edx ebx esp ebp esi edi | sN (stack word N at entry: s0 = return address, s1 = first stack arg)
 *         | aN (entry stack word N, saved, usable at exit) | gHEX (c2 VA, e.g. a global; rebased if c2 moved)
 *         | tHEX (offset HEX in the calling thread's static TLS block of c2.dll, [[fs:0x2c] + 4*[_tls_index]]:
 *           cl 17.00 keeps its per-function state there; needs `set tlsidx <VA of __tls_index>`)
 *   '>off' replaces v with dword [v+off] (off hex or decimal, may be negative)
 *   '~HEX' replaces v with dword [HEX + 4*v] (HEX a c2 VA of a pointer/word table, rebased)
 *   '#n'   logs n dwords starting at address v instead of v itself;  '$' logs the C string at v
 *   '%'    v is a tuple: dump the tuple stream from it as "T ..." lines after the event (IL dump)
 *   '!'    the same for the one tuple v.  T I lines: T I <tuple> <opcode> <line offset> <+0x1c byte> | dst <- src
 *   '%+' '!+'  the extended dump (dumpilx below): the same lines plus notes after " ;" and the P/C/A line kinds
 *   '&'    v is a tuple: the x87 view of the stream from it as "T X" lines (dumpfp below; cl 13.10 layout)
 *   '*N'   v is a list (next at +0): logs words +8 and +0xc of up to N nodes, "[b e b e ..]" (NColor ranges);
 *          '*N:W' word +W of each node, '*N:W1:W2' two words, '*N:W1:W2:NX' next pointer at +NX
 * Log lines:  E <seq> <depth> <id> eax ecx edx ebx ebp esi edi esp | specs...
 *             X <seq> <depth> <id> eax edx | specs...
 */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#define MAXHOOK 128
#define MAXSPEC 96            /* specs per hook (was 12; c2ncol --export logs 73 at one probe) */
#define MAXDEPTH 4096
#define NSAVE 8

typedef struct { DWORD edi, esi, ebp, esp, ebx, edx, ecx, eax, eflags, slot; } Regs;  /* pushfd;pushad image */

#define MAXDER 12             /* '>' / '~' steps per spec (was 6: longer PREF-chain specs were cut short) */
typedef struct { int base; DWORD idx; int nder; int der[MAXDER]; char tbl[MAXDER]; int n; int w1, w2, nx; } Spec; /* base: 0..7 reg, 8 s, 9 a, 10 g, 11 t */
static DWORD delta;            /* actual c2 base - imgbase */
static DWORD TLSIDX;           /* `set tlsidx <VA>`: c2.dll's __tls_index (cl 17.00); base 't' and `set curfunctls` */
static int readd(DWORD a, DWORD *v);
static DWORD tlsarray(void) {  /* the TEB's ThreadLocalStoragePointer (fs:[0x2c]); inline asm: cl 13.10 has no
                                * __readfsdword */
    DWORD a;
    __asm mov eax, fs:[0x2c]
    __asm mov a, eax
    return a;
}
static DWORD tlsblock(void) {  /* c2.dll's TLS block of the calling thread, 0 if unknown */
    DWORD idx, b;
    if (!TLSIDX || !readd(TLSIDX + delta, &idx) || !readd(tlsarray() + 4 * idx, &b)) return 0;
    return b;
}
static DWORD imgbase = 0x10b00000, REGNAME = 0x10cec7b8, OPNAME = 0x10ceb3d8;   /* 14.00.50727.762; `set` changes them */
static DWORD OPCOUNT = 0xffffffff;   /* entries in the opcode-name table (13.10: machine opcodes only) */
/* tuple layout, `set blk|len|mem`: BLOCK tuple's BB*, an instruction's encoded-length byte, a memory operand's
 * {sym, disp, base leaf, index leaf} (13.10: 0x18, 0x16, 0x2c) */
static int LBLK = 0x20, LLEN = 0x1c, LMEM = 0x24;
static char opx[1024][20];     /* `op <hex> <name>`: names of opcodes >= OPCOUNT (`set opcount 0`: of every opcode,
                                * for a build whose c2.dll has no name table: the Xbox 360 cross c2) */
typedef struct {
    int id; DWORD rva; int steal; int exitp; int ne, nx;
    int ni; int ilen[8], kind[8], cc[8]; DWORD tgt[8];   /* displaced instrs: kind 0 copy, 1 jcc, 2 jmp, 3 call */
    Spec e[MAXSPEC], x[MAXSPEC];
} Hook;
typedef struct { int hook; DWORD esp; DWORD ret; DWORD save[NSAVE]; DWORD tid; int d; } Frame;

static Hook hooks[MAXHOOK];
static int nhooks;
/* Threads: cl 17.00's c2 generates code on worker threads (4 by default). Frames come from one pool (an exit stub's
 * slot number is global), each tagged with its thread and that thread's depth; both handlers run under one lock;
 * a "# thread N" line (N = order of first appearance) precedes an event of another thread than the previous event's
 * (none before the first event, so a single-threaded log is as before). */
static Frame frames[MAXDEPTH];      /* tid 0 = free */
#define MAXTHR 64
static DWORD tids[MAXTHR]; static int tdep[MAXTHR], ntids, lastthr = -1;
static CRITICAL_SECTION lk; static int lkinit;
static void out(const char *fmt, ...);
static int thr(void) {             /* this thread's ordinal; logs the change of thread */
    DWORD t = GetCurrentThreadId(); int i;
    for (i = 0; i < ntids && tids[i] != t; i++) ;
    if (i == ntids && ntids < MAXTHR) tids[ntids++] = t;
    if (i >= MAXTHR) i = MAXTHR - 1;
    return i;
}
static void thrmark(int i) { if (lastthr >= 0 && i != lastthr) out("# thread %d\n", i); lastthr = i; }
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
static DWORD pend_fp;         /* '&': x87 view (dumpfp, 13.10 FPColor) */
static DWORD pend_g;          /* '^': globreg view (dumpg: split pieces, docs/c2/regalloc.md 3.6.11) */
static void emit(Spec *s, int ns, Regs *r, DWORD *stk, Frame *f) {
    int i, j, k;
    for (i = 0; i < ns; i++) {
        DWORD v = 0; int ok = 1;
        if (s[i].base < 8) v = ((DWORD *)r)[s[i].base];
        else if (s[i].base == 8) ok = stk ? readd((DWORD)(stk + s[i].idx), &v) : 0;
        else if (s[i].base == 10) v = s[i].idx + delta;
        else if (s[i].base == 11) ok = (v = tlsblock()) != 0 ? (v += s[i].idx, 1) : 0;
        else ok = f && s[i].idx < NSAVE ? (v = f->save[s[i].idx], 1) : 0;
        for (j = 0; ok && j < s[i].nder; j++)
            ok = s[i].tbl[j] ? readd((DWORD)s[i].der[j] + delta + 4 * v, &v) : readd(v + s[i].der[j], &v);
        if (!ok) { out(" ?"); continue; }
        if (s[i].n <= -2 && s[i].n >= -5) { out(" IL"); pend_il = v; pend_x = s[i].n < -3; pend_max = s[i].n & 1 ? 1 : ilmax; continue; }
        if (s[i].n == -6) { out(" FP"); pend_fp = v; continue; }
        if (s[i].n == -7) { out(" GV"); pend_g = v; continue; }
        if (s[i].n <= -100) {      /* '*N': a list from v (next at +0): words +8 and +0xc of up to N nodes */
            DWORD a = v, b, c; int m = 0;
            out(" [");
            while (a && m < -s[i].n - 100 && readd(a + s[i].w1, &b) && (s[i].w2 < 0 || readd(a + s[i].w2, &c))) {
                if (s[i].w2 < 0) out(m ? " %x" : "%x", b); else out(m ? " %x %x" : "%x %x", b, c);
                m++;
                if (!readd(a + s[i].nx, &a)) break;
            }
            out("]"); continue;
        }
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
/* `func <text>` (repeatable) + `set curfunc <c2 VA of _CurFunc>`: log only while the function being compiled is
 * named so: an event is written (and counted for `max`) only when the decorated name at [[[_CurFunc]+0]+4] contains
 * every <text>. Without `func` lines everything is logged as before. Hooks keep running either way (exit frames are
 * still tracked), so the filter changes the log only. c2trace.py writes it from C2TRACE_FUNC or run_trace(func=). */
#define MAXFF 4
static char ffilt[MAXFF][256]; static int nffilt; static DWORD CURFUNC = 0x10d123ac;
static DWORD CURFUNCTLS;       /* `set curfunctls <off>`: _CurFunc is at this offset of the TLS block (cl 17.00) */
static int CFNAME = 4;         /* `set cfname <off>`: the name pointer's offset in [FUNC+0] (13.00.9044: 0x18) */
static int infunc(void) {
    char nm[1024]; DWORD f, s, n; int i, m = 0;
    if (!nffilt) return 1;
    if (!readd(CURFUNCTLS ? tlsblock() + CURFUNCTLS : CURFUNC + delta, &f) || !f || !readd(f, &s) || !s || !readd(s + CFNAME, &n) || !n) return 0;
    while (m < (int)sizeof nm - 1 && readd(n + m, &f) && (nm[m] = (char)f) != 0) m++;
    nm[m] = 0;
    for (i = 0; i < nffilt; i++) if (!strstr(nm, ffilt[i])) return 0;
    return 1;
}
/* IL layout (`set`; defaults = 14.00). Tuple: next / opcode / kind byte / flags byte / line word / src, dst lists /
 * type word; leaf: payload, second payload; SYM: name record, kind byte, number; kind numbers are mapped to the
 * 14.00 ones by `kind <native> <14.00>` lines (cl 17.00: a vtable at +0 moves every field). `set ilraw N` adds a
 * "T R <addr> <N words>" line after each tuple and leaf: the raw words a new layout is read from. */
static int LNEXT = 0, LOP = 4, LKIND = 8, LFLG = 9, LLINE = 0x14, LSRC = 0x28, LDST = 0x2c, LTYW = 0x0a;
static int LPAY = 0x18, LPAY2 = 0x1c, SNAME = 0, SKIND = 4, SNUM = 0x1c, ILRAW = 0;
static int LICON = -1, SNSTR = 4;   /* `set licon` ICONST value offset (-1: lpay), `set snstr` name pointer in the name
                                    * record (13.00.9044: 0x18 and 0x18) */
static int LMD = -1, LMB = -1, LMI = -1, LMS = 0x14;
static int ILNUM = 0;          /* `set ilnum <off>` (env C2TRACE_ILNUM): "T N <tuple> <word at +off>" after each T I line */  /* memory leaf disp / base / index (-1: LMEM+4/+8/+12), scale byte */
static BYTE kmap[256]; static int kmapset;
static BYTE kindof(DWORD t) { BYTE k = rb(t + LKIND); return kmapset ? kmap[k] : k; }
static void rawwords(DWORD a) {
    int i;
    if (!ILRAW) return;
    out("T R %x", a);
    for (i = 0; i < ILRAW; i++) out(" %x", rd(a + 4 * i));
    out("\n");
}
static void symname(DWORD x) {
    char c[64]; BYTE k;
    if (!x) { out("0"); return; }
    k = rb(x + SKIND);
    if (k == 1) { rstr(rd(REGNAME + delta + 4 * rd(x + SNUM)), c, sizeof c); out("%s", c); return; }
    if (k == 2) { out("^"); x = rd(x + SNAME); if (!x) { out("?"); return; } k = rb(x + SKIND); }
    if (rd(x + SNAME) && rstr(rd(rd(x + SNAME) + SNSTR), c, sizeof c)) out("%s", c);
    else out("%c%d", k == 3 ? 't' : k == 0xd ? 'k' : 's', rd(x + SNUM));
}
static int LEXT = 0x30, LLABT = 0x33, LBRCC = 0x0a, LFC = 0x18, FCDBG = 0;   /* extended dump layout, below */
static int FENAME = 0;         /* `set fename 1` (13.10): CODEADDR leaves print the front-end symbol's name at sym+4 */
static int ILPAS = 0;          /* `set ilpas <off>` (env C2TRACE_ILPAS): memory leaves also print "@<word at leaf+off>",
                                * the INDIR points-at-set index (14.00: +0x1c); 0 = off, the dump is unchanged */
static int cdname(DWORD s) {   /* a call / tail-jump target: its symbol is the front end's, name pointer at +4 ([sym+0]
                                * is not a name record there: 13.10 printed another symbol's name or s0) */
    char c[80]; DWORD p, t;
    if (!s) return 0;
    t = rd(s + LLABT);
    if (t && kindof(t) == 27) return 0;                 /* a label (branch target): keep the old text */
    p = rd(s + 4);
    if (!p || !rstr(p, c, sizeof c) || !(c[0] == '_' || c[0] == '?' || c[0] == '@' || isalpha((BYTE)c[0]))) return 0;
    out("%s", c); return 1;
}
static void leaf(DWORD l) {
    BYTE k = kindof(l);
    DWORD a, b;
    switch (k) {
    case 1: a = rd(l + LPAY); b = rd(l + LPAY2);
            if (b && b != a && rb(b + SKIND) == 1) { symname(b); out("="); }
            symname(a); break;
    case 4: if (FENAME && cdname(rd(l + LPAY))) break;  /* fall through */
    case 2: case 3: symname(rd(l + LPAY)); break;
    case 7: out("%d", rd(l + (LICON < 0 ? LPAY : LICON))); break;
    case 9: out("fconst"); break;
    case 5: case 6:
        out("[");
        if (rd(l + LMEM)) { symname(rd(l + LMEM)); out("+"); }
        if (rd(l + (LMB < 0 ? LMEM + 8 : LMB))) { leaf(rd(l + (LMB < 0 ? LMEM + 8 : LMB))); out("+"); }
        if (rd(l + (LMI < 0 ? LMEM + 12 : LMI))) { leaf(rd(l + (LMI < 0 ? LMEM + 12 : LMI))); out("*%d+", 1 << (rb(l + LMS) & 15)); }
        out("%d]", (int)rd(l + (LMD < 0 ? LMEM + 4 : LMD)));
        if (ILPAS) out("@%x", rd(l + ILPAS));
        break;
    case 11: out("regset"); break;
    case 12: out("pas%d", rd(l + LPAY)); break;
    default: out("k%d", rb(l + LKIND));
    }
    out(":%x", rd(l + LTYW) & 0xffff);
}
static void oplist(DWORD l) {
    int n = 0;
    while (l && n++ < 16) { out(" "); leaf(l); l = rd(l + LNEXT); }
}
static int ILSYMW = 0;         /* `set ilsymw N` (env C2TRACE_ILSYMW): with ilraw, "T S <sym> <N words>" after each leaf's
                               * T R line: the words of the leaf's symbol (leaf + lpay; "T S 0" without one) */
static void rawlist(DWORD l) {
    int n = 0, i; DWORD s;
    while (ILRAW && l && n++ < 16) {
        rawwords(l);
        if (ILSYMW) {
            s = rd(l + LPAY);
            out("T S %x", s);
            for (i = 0; s && i < ILSYMW; i++) out(" %x", rd(s + 4 * i));
            out("\n");
        }
        l = rd(l + LNEXT);
    }
}
static void dumpil(DWORD t, int max) {
    int n = 0; char c[48];
    while (t && n++ < max) {
        BYTE k = kindof(t), f = rb(t + LFLG);
        DWORD op = rd(t + LOP);
        if (k == 26) out("T B %x\n", rd(t + LBLK));
        else if (k == 27) out("T L %x\n", t);
        else if (f & 1) {
            if (op >= OPCOUNT && op - OPCOUNT < 1024 && opx[op - OPCOUNT][0]) strcpy(c, opx[op - OPCOUNT]);
            else if (op >= OPCOUNT || !rstr(rd(OPNAME + delta + 4 * op), c, sizeof c)) sprintf(c, "op%x", op);
            out("T I %x %s %d %d |", t, c, rd(t + LLINE) & 0xffff, rb(t + LLEN));
            oplist(rd(t + LDST)); out(" <-"); oplist(rd(t + LSRC));
            if (k == 18 && kindof(rd(t + LSRC)) == 4) { out(" ->"); }
            out("\n");
            if (ILNUM) out("T N %x %x\n", t, rd(t + ILNUM));
        } else if (ILRAW) out("T K %x kind %d op %x\n", t, rb(t + LKIND), op);
        if (ILRAW) { rawwords(t); if (f & 1) { rawlist(rd(t + LDST)); rawlist(rd(t + LSRC)); } }
        if (blen > (int)sizeof buf - 1024) flush();
        t = rd(t + LNEXT);
    }
}

/* ---- extended IL dump ('%+' / '!+' specs): what the plain dump leaves out (docs/c2/tracer.md, `il`).
 * Adds to T I lines, after " ;": cc=<x86 cc> (BRANCH, QUESTION), sN=/dN= notes on the Nth src/dst operand
 * (& address leaf, <name> code address, L<label tuple>, float constant value), intrin=, default=/cases= (SWITCH),
 * eh{..} (EH record). New lines: T L <tuple> [name], T P <tuple> <opcode> <+0x20 word> [name] (non-instruction
 * tuples), T C <switch> <low> <high> L<label> (case list), T A <line> (unwind action of the preceding OPPUSHSTATE).
 * Layout (`set`): ext = per-kind word after the operand lists (QUESTION cc, SWITCH default, EH record; cases at
 * ext+4), labtup = label SSR -> its LABEL tuple, brcc = BRANCH condition-code nibble, ehop / ulabop = the
 * opcode numbers of OPPUSHSTATE and OPUSERLABEL; the tuple / leaf fields, kind numbers (`kind` lines) and the
 * next links are the plain dump's (lop, lkind, lflg, lline, lsrc, ldst, lnext, lpay): defaults = 14.00, so the
 * x86 builds print what they printed. cl 17.00 x64 also sets extin, labsym, casen / casenx, fcoff / fc64. */
static int LEXTI = -1;         /* `set extin`: INTRINSIC's number when it is not at `ext` (cl 17.00 x64: 0x48); -1 = LEXT */
static int LLABS = 0x24, LCASE = 0x0c, LCASEN = 0, FC64 = 0;  /* `set labsym`: LABEL tuple -> its symbol; `set casen`
                               * / `set casenx`: CASE node low (high +4, label +8) / next; `set fc64 1`: FCONST holds a
                               * double at fcoff (cl 17.00 x64), not an 80-bit x87 value */
static DWORD OPEH = 0x2db, OPULAB = 0x300;   /* opcode numbers: OPPUSHSTATE (.. OPSETSTATE = +9), OPUSERLABEL */
#define XI (LEXTI < 0 ? LEXT : LEXTI)
static int xdepth;
static const char *ccname[17] = { "0", "e", "ne", "l", "g", "le", "ge", "b", "a", "be", "ae", "s", "ns", "p", "np", "o", "no" };
static void xopname(DWORD op, char *c, int max) {
    if (op >= OPCOUNT && op - OPCOUNT < 1024 && opx[op - OPCOUNT][0]) strcpy(c, opx[op - OPCOUNT]);
    else if (op >= OPCOUNT || !rstr(rd(OPNAME + delta + 4 * op), c, max)) sprintf(c, "op%x", op);
}
static int ssrname(DWORD s, const char *pre) {   /* front-end symbol (CODEADDR leaves, labels): name pointer at +4 */
    char c[80]; DWORD p = s ? rd(s + 4) : 0;
    if (p && rstr(p, c, sizeof c)) { out("%s%s", pre, c); return 1; }
    return 0;
}
static DWORD labtup(DWORD s) { DWORD l = s ? rd(s + LLABT) : 0; return l && kindof(l) == 27 ? l : 0; }
static void xfconst(DWORD l) {         /* FCONST leaf: 80-bit x87 value at +LFC (`set fcoff`; 14.00: 0x18) */
    DWORD w[3]; double d; int i;
    if (FCDBG) { out("raw["); for (i = 0; i < 12; i++) out(i ? " %x" : "%x", rd(l + 4 * i)); out("]"); }
    if (!readd(l + LFC, &w[0]) || !readd(l + LFC + 4, &w[1]) || (!FC64 && !readd(l + LFC + 8, &w[2]))) {
        out("?"); return;
    }
    if (FC64) { memcpy(&d, w, 8); out("%.17g", d); return; }
    __asm fld tbyte ptr w
    __asm fstp qword ptr d
    out("%.17g", d);                  /* exact double; c2il.py prints the shortest spelling (and the f form) */
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
    if (k == 19) { NOTE; out("intrin=%d", rd(t + XI) & 0xffff); }
    for (side = 0; side < 2; side++)
        for (i = 0, l = rd(t + (side ? LDST : LSRC)); l && i < 16; i++, l = rd(l + LNEXT)) {
            BYTE lk = kindof(l);
            if (lk != 3 && lk != 4 && lk != 5 && lk != 9) continue;
            NOTE; out("%c%d=", side ? 'd' : 's', i);
            if (lk == 9) xfconst(l);
            else if (lk != 4) out("&");
            else if ((s = labtup(rd(l + LPAY))) != 0) out("L%x", s);
            else if (!ssrname(rd(l + LPAY), "")) out("?");
        }
    if (k == 20) {
        for (i = 0, l = rd(t + LEXT + 4); l && i < 100000; i++) l = rd(l + LCASEN);
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
        BYTE k = kindof(t), f = rb(t + LFLG);
        DWORD op = rd(t + LOP);
        if (k == 26) out("%sB %x\n", pf, rd(t + LBLK));
        else if (k == 27) { out("%sL %x", pf, t); ssrname(rd(t + LLABS), " "); out("\n"); }
        else if (f & 1) {
            xopname(op, c, sizeof c);
            out("%sI %x %s %d %d |", pf, t, c, rd(t + LLINE) & 0xffff, rb(t + LLEN));
            oplist(rd(t + LDST)); out(" <-"); oplist(rd(t + LSRC));
            if (k == 18 && kindof(rd(t + LSRC)) == 4) { out(" ->"); }
            xnotes(t, k, op);
            out("\n");
            if (k == 20)
                for (i = 0, l = rd(t + LEXT + 4); l && i < 4096; i++, l = rd(l + LCASEN)) {
                    out("%sC %x %d %d L%x\n", pf, t, rd(l + LCASE), rd(l + LCASE + 4), rd(l + LCASE + 8));
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
        t = rd(t + LNEXT);
    }
}

/* ---- x87 view ('&' spec, cl 13.10 FPColor; docs/c2/x87.md section 11, tools/c2regs.py _fpil): v is the first tuple.
 * T X B <tuple> <bb> <bb+0x18> <bb+0x1c> <bb+0x20> in=<sbv> out=<sbv> u=<sbv> stk=<8 words at bb+BBSTK> p=<pred bbs> s=<succ bbs>
 *   (sbv = base:bits,... from bb+BBIN, +4, +8; 13.10 nodes {base, next, bits}; pred edge {?, next +4, bb +8},
 *   succ edge {next, .., bb +0xc})
 * T X L <tuple>                                           a label
 * T X N <tuple> <kind> <op>                               any other tuple that is not an instruction
 * T X I <tuple> <op> <kind> <type word> <b16> <b17> <line> |<dst leaves> |<src leaves>   (b16/b17: bytes LLEN, LLEN+1)
 *   leaf: k<kind>[n] (not a register leaf; n: a code address whose SYM has +0x20 & 0x10000) or r[f][x].<sym>[.R]: f = x87 type (0x4000), x = dies (+0x15 & 0x10),
 *   sym = the globreg (kind-2 SYM) and c when coloured (+0x10), s<kind> another SYM, 0 none; .R: +0x18 is a register
 * T X W <globreg> <num> <colour> <+4 dword> <first bb +0x28> <last bb +0x2c> d=<def tuples +0x38> e=<end tuples +0x3c>
 *   once per globreg seen in an x87 leaf */
static int BBIN = 0x48, BBSTK = 0x60;
static void fpsbv(const char *nm, DWORD h) {
    int n = 0; DWORD x = h ? rd(h) : 0;
    out(" %s=", nm);
    while (x && n++ < 4096) { if (rd(x + 8)) out(n > 1 ? ",%x:%x" : "%x:%x", rd(x), rd(x + 8)); x = rd(x + 4); }
}
static DWORD fpseen[4096]; static int nfpseen;
static void fpleaf(DWORD l) {
    BYTE k = rb(l + 8); DWORD s, r;
    if (k != 1) {          /* a code address whose symbol has +0x20 & 0x10000 (a callee that leaves the x87 stack alone) */
        out(" k%d%s", k, k == 4 && (s = rd(l + 0x18)) != 0 && (rd(s + 0x20) & 0x10000) ? "n" : "");
        return;
    }
    s = rd(l + 0x1c); r = rd(l + 0x18);
    out(" r%s%s.", (rd(l + 8) >> 16 & 0xf000) == 0x4000 ? "f" : "", rb(l + 0x15) & 0x10 ? "x" : "");
    if (!s) out("0");
    else if (rb(s + 4) == 2) {
        int i;
        out("%x%s", s, rd(s + 0x10) ? "c" : "");
        if ((rd(l + 8) >> 16 & 0xf000) == 0x4000) {
            for (i = 0; i < nfpseen && fpseen[i] != s; i++) ;
            if (i == nfpseen && nfpseen < 4096) fpseen[nfpseen++] = s;
        }
    } else out("s%d", rb(s + 4));
    if (r && rb(r + 4) == 1) out(".R");
}
static void fplist(const char *nm, DWORD x) {
    int n = 0;
    out(" %s=", nm);
    while (x && n++ < 4096) { out(n > 1 ? ",%x" : "%x", rd(x + 4)); x = rd(x); }
}
static void dumpfp(DWORD t) {
    int n = 0, i; DWORD bb, l;
    nfpseen = 0;
    while (t && n++ < ilmax) {
        BYTE k = rb(t + 8), f = rb(t + 9);
        if (k == 26 && (bb = rd(t + LBLK)) != 0) {
            out("T X B %x %x %x %x %x", t, bb, rd(bb + 0x18), rd(bb + 0x1c), rd(bb + 0x20));
            fpsbv("in", rd(bb + BBIN)); fpsbv("out", rd(bb + BBIN + 4)); fpsbv("u", rd(bb + BBIN + 8));
            out(" stk=");
            l = rd(bb + BBSTK);
            for (i = 0; i < 8; i++) out(i ? ",%x" : "%x", l ? rd(l + 4 * i) : 0);
            out(" p=");
            for (i = 0, l = rd(bb + 8); l && i < 4096; i++, l = rd(l + 4)) out(i ? ",%x" : "%x", rd(l + 8));
            out(" s=");
            for (i = 0, l = rd(bb + 0xc); l && i < 4096; i++, l = rd(l)) out(i ? ",%x" : "%x", rd(l + 0xc));
            out("\n");
        } else if (k == 27) out("T X L %x\n", t);
        else if (!(f & 1)) out("T X N %x %d %x\n", t, k, rd(t + 4));
        else {
            out("T X I %x %x %d %x %d %d %d |", t, rd(t + 4), k, rd(t + 8) >> 16 & 0xffff, rb(t + LLEN), rb(t + LLEN + 1),
                rd(t + 0x14) & 0xffff);
            for (i = 0, l = rd(t + 0x2c); l && i < 16; i++, l = rd(l)) fpleaf(l);
            out(" |");
            for (i = 0, l = rd(t + 0x28); l && i < 16; i++, l = rd(l)) fpleaf(l);
            out("\n");
        }
        if (blen > (int)sizeof buf - 1024) flush();
        t = rd(t);
    }
    for (i = 0; i < nfpseen; i++) {
        DWORD g = fpseen[i];
        out("T X W %x %d %x %x %x %x", g, rd(g + 0x1c), rd(g + 0x10), rd(g + 4), rd(g + 0x28), rd(g + 0x2c));
        fplist("d", rd(g + 0x38)); fplist("e", rd(g + 0x3c));
        out("\n");
        if (blen > (int)sizeof buf - 1024) flush();
    }
}

/* ---- globreg view ('^' spec; split pieces, docs/c2/regalloc.md 3.6.11, tools/c2regs.py `--pieces`): v is the first
 * tuple of the function.  Every line starts "T G":
 * T G B <tuple> <bb> <loop level> <bb+0x18> p=<pred bbs> s=<succ bbs> i=<bb+0x48> o=<bb+0x4c> n=<bb+0x50>
 *   (SBVs of GLOBREG numbers; pred edge {?, next +4, bb +8}, succ edge {next, .., bb +0xc}; loop level = word at
 *   bb+GLOOP: 14.00 / 15.00 0x17e, 13.10 0xa6)
 * T G I <tuple> <opcode hex> <kind> <+9 byte> <+0x1c dword> | <dst leaves> | <src leaves>
 *   leaf: g<num>[=<self num>]/<type word +0xa>/<+0x10>/<+0x14 byte>/<+0x15 byte>/<1 if (sym+0x10 ^ type) & 0xfff == 0>
 *         (a REG leaf of a GLOBREG), r<reg>/<type>/<+0x15> (a physical register), m(<base>,<index>)/<type> (memory),
 *         (memory: m<kind>(..)/<type>/<1 if it has a symbol or displacement>),
 *         s<+0x10>/<type> (a SYM leaf, kind 2 or 3: +0x10 = the globreg number of its home), K (register set),
 *         .<kind> (any other)
 * T G S <tuple> <+0x1c byte> <webs>        a split point (kind 0x1c): its SBV at +0x20 (or, once processed, the list
 *                                          at +0x24)
 * T G L <tuple>                            a label;   T G P <tuple> <opcode> <kind>: another non-instruction tuple
 * T G R <num> <record> <self num> <+4 dword> <colour reg> <nrefs> <first bb> <last bb> <nEnter> <prio> <benefit>
 *       <defOrd> <sym kind> <sym key> a=<avail>       every GLOBREG of _GlobregHash (GRHASH, 0x400 chains at +0x30)
 * T G N <class> <webs>                     _NewGlobregs[class] (GNEW, 7 SBV pointers) */
static int GLOOPP = 0x16c;          /* BB -> its loop (13.10 0x9c): the loop's +0x38 SBV holds the variables it references */
static int GSBV32 = 0, GLOOP = 0x17e, GSPB = 0x1c, GSPS = 0x20, GSPL = 0x24;   /* split point: done byte, SBV, list (13.10: 0x16, 0x18, 0x1c) */
static DWORD GRHASH = 0x10d15090, GNEW = 0x10d12278;     /* 14.00.50727.762 */
static void gsbv(const char *nm, DWORD h) {      /* h: SBV header, first chunk at [h] */
    int n = 0, any = 0, b; DWORD x = h ? rd(h) : 0;
    out(nm);
    while (x && n++ < 65536) {
        DWORD base = rd(x);
        if (GSBV32) {
            DWORD w = rd(x + 8);
            for (b = 0; b < 32; b++) if (w >> b & 1) out(any++ ? ",%x" : "%x", base + b);
            x = rd(x + 4);
        } else {
            DWORD lo = rd(x + 4), hi = rd(x + 8);
            for (b = 0; b < 32; b++) if (lo >> b & 1) out(any++ ? ",%x" : "%x", base + b);
            for (b = 0; b < 32; b++) if (hi >> b & 1) out(any++ ? ",%x" : "%x", base + 32 + b);
            x = rd(x + 0xc);
        }
    }
}
static void gleaf(DWORD l, int depth) {
    BYTE k; DWORD x, y, ty;
    if (!l) { out("-"); return; }
    k = rb(l + 8); ty = rd(l + 8) >> 16 & 0xffff;
    if (k == 1) {
        x = rd(l + 0x1c);
        if (x && rb(x + 4) == 2) {
            y = rd(x + 8);
            out("g%x", rd(x + 0x1c));
            if (y && y != x) out("=%x", rd(y + 0x1c));
            out("/%x/%x/%x/%x/%d", ty, rd(l + 0x10), rb(l + 0x14), rb(l + 0x15),
                ((rd(rd(l + 0x18) + 0x10) ^ ty) & 0xfff) == 0);
        } else if (x && rb(x + 4) == 1) out("r%d/%x/%x", rd(x + 0x1c), ty, rb(l + 0x15));
        else out("r?/%x", ty);
    } else if ((k == 5 || k == 6) && depth < 3) {
        out("m%d(", k); gleaf(rd(l + LMEM + 8), depth + 1); out(","); gleaf(rd(l + LMEM + 12), depth + 1);
        out(")/%x/%d", ty, rd(l + LMEM) != 0 || rd(l + LMEM + 4) != 0);
    } else if (k == 2 || k == 3) out("s%x/%x", rd(l + 0x10), ty);
    else if (k == 11) out("K");
    else out(".%d", k);
}
static DWORD loops[4096]; static int nloops;
static void dumpg(DWORD t) {
    int n = 0, i; DWORD bb, l, g;
    nloops = 0;
    while (t && n++ < ilmax) {
        BYTE k = rb(t + 8), f = rb(t + 9);
        if (k == 26 && (bb = rd(t + LBLK)) != 0) {
            out("T G B %x %x %d %x p=", t, bb, rd(bb + GLOOP) & 0xffff, rd(bb + 0x18));
            for (i = 0, l = rd(bb + 8); l && i < 4096; i++, l = rd(l + 4)) out(i ? ",%x" : "%x", rd(l + 8));
            out(" s=");
            for (i = 0, l = rd(bb + 0xc); l && i < 4096; i++, l = rd(l)) out(i ? ",%x" : "%x", rd(l + 0xc));
            gsbv(" i=", rd(bb + 0x48)); gsbv(" o=", rd(bb + 0x4c)); gsbv(" n=", rd(bb + 0x50));
            if ((l = rd(bb + GLOOPP)) != 0) {       /* the loop and its enclosing loops (+8), once each */
                out(" L=%x", l);
                for (g = l; g && nloops < 4096; g = rd(g + 8)) {
                    for (i = 0; i < nloops && loops[i] != g; i++) ;
                    if (i == nloops) loops[nloops++] = g;
                }
            }
            out("\n");
        } else if (k == 27) out("T G L %x\n", t);
        else if (f & 1) {
            out("T G I %x %x %d %x %x |", t, rd(t + 4), k, f, rd(t + 0x1c));
            for (i = 0, l = rd(t + 0x2c); l && i < 16; i++, l = rd(l)) { out(" "); gleaf(l, 0); }
            out(" |");
            for (i = 0, l = rd(t + 0x28); l && i < 16; i++, l = rd(l)) { out(" "); gleaf(l, 0); }
            out("\n");
        } else if (k == 0x1c) {
            out("T G S %x %d", t, rb(t + GSPB));
            if (rb(t + GSPB)) {
                out(" ");
                for (i = 0, l = rd(t + GSPL); l && i < 4096; i++, l = rd(l)) out(i ? ",%x" : "%x", rd(rd(l + 4) + 0x1c));
            } else gsbv(" ", rd(t + GSPS));
            out("\n");
        } else if (k >= 13) out("T G P %x %x %d\n", t, rd(t + 4), k);
        if (blen > (int)sizeof buf - 1024) flush();
        t = rd(t);
    }
    for (i = 0; i < 0x400; i++)
        for (n = 0, g = rd(GRHASH + delta + 4 * i); g && n < 65536; n++, g = rd(g + 0x30)) {
            DWORD sy = rd(g), y = rd(g + 8), c = rd(g + 0x10);
            out("T G R %x %x %x %x %d %d %x %x %d %d %d %d %d %d", rd(g + 0x1c), g, y ? rd(y + 0x1c) : 0, rd(g + 4),
                c ? rd(c + 0x1c) : 0, rd(g + 0x24), rd(g + 0x28), rd(g + 0x2c), rb(g + 0x34), rd(g + 0xc), rd(g + 0x40),
                rd(g + 0x44), sy ? rb(sy + 4) : 0, sy ? rd(sy + 0x1c) : 0);
            gsbv(" a=", rd(g + 0x20));
            if (sy && rd(sy + 0x34)) out(" x=%x", rd(rd(sy + 0x34)));
            if (sy && rb(sy + 4) == 0xd && rd(sy + 0x28)) out(" k=%d,%d", rb(rd(sy + 0x28) + 8), rd(rd(sy + 0x28) + 0x18));
            out("\n");
            if (blen > (int)sizeof buf - 1024) flush();
        }
    for (i = 0; i < 7; i++) { out("T G N %d", i); gsbv(" ", rd(GNEW + delta + 4 * i)); out("\n"); }
    for (i = 0; i < nloops; i++) {        /* T G O <loop> <enclosing> <header bb +0x14> e=<exit bbs +0x24 list> r=<+0x38> */
        out("T G O %x %x %x e=", loops[i], rd(loops[i] + 8), rd(loops[i] + 0x14));
        for (n = 0, l = rd(loops[i] + 0x24); l && n < 4096; n++, l = rd(l + 4)) out(n ? ",%x" : "%x", rd(l));
        gsbv(" r=", rd(loops[i] + 0x38));
        out("\n");
    }
}

/* called from the entry trampoline: pushfd; pushad; push esp; push i; call */
static void __cdecl on_entry_(int i, Regs *r);
static void __cdecl on_entry(int i, Regs *r) {
    if (lkinit) EnterCriticalSection(&lk);
    on_entry_(i, r);
    if (lkinit) LeaveCriticalSection(&lk);
}
static void __cdecl on_entry_(int i, Regs *r) {
    Hook *h = &hooks[i];
    DWORD *stk = &r->slot;             /* esp at function entry: [0] = return address */
    DWORD esp0 = (DWORD)stk, tid = GetCurrentThreadId();
    Frame *f = NULL;
    int k, me = thr(), depth;
    for (k = 0; k < MAXDEPTH; k++)    /* this thread's frames abandoned by longjmp */
        if (frames[k].tid == tid && frames[k].esp <= esp0) { frames[k].tid = 0; tdep[me]--; }
    depth = tdep[me];
    if (h->exitp) {
        static int hint;
        for (k = 0; k < MAXDEPTH && frames[(hint + k) % MAXDEPTH].tid; k++) ;
        if (k < MAXDEPTH) {
            int slot = (hint + k) % MAXDEPTH;
            hint = slot + 1;
            f = &frames[slot];
            f->hook = i; f->esp = esp0; f->ret = stk[0]; f->tid = tid; f->d = depth;
            for (k = 0; k < NSAVE; k++) if (!readd((DWORD)(stk + k), &f->save[k])) f->save[k] = 0;
            stk[0] = (DWORD)(exits + 10 * slot);
            tdep[me]++;
        }
    }
    if (!infunc()) return;            /* `func` filter: another function is being compiled */
    if (seq < maxev) {
        thrmark(me);
        out("E %lu %d %d %x %x %x %x %x %x %x %x |", seq, depth, h->id,
            r->eax, r->ecx, r->edx, r->ebx, r->ebp, r->esi, r->edi, esp0);
        emit(h->e, h->ne, r, stk, f);
        out("\n");
        if (pend_il) { if (pend_x) dumpilx(pend_il, pend_max, "T "); else dumpil(pend_il, pend_max); pend_il = 0; }
        if (pend_fp) { dumpfp(pend_fp); pend_fp = 0; }
        if (pend_g) { dumpg(pend_g); pend_g = 0; }
        if (eachflush) flush();
    }
    seq++;
}

/* called from the common exit stub: (push k) ; pushfd; pushad; push esp; call.  r->slot = k, becomes ret */
static void __cdecl on_exit2(Regs *r) {
    int k = (int)r->slot, me = thr(), d;
    Frame *f = &frames[k];
    Hook *h = &hooks[f->hook];
    r->slot = f->ret;
    d = f->d;
    if (f->tid) { f->tid = 0; tdep[me] = d; }
    if (!infunc()) return;
    if (seq < maxev) {
        thrmark(me);
        out("X %lu %d %d %x %x |", seq, d, h->id, r->eax, r->edx);
        emit(h->x, h->nx, r, NULL, f);
        out("\n");
        if (eachflush) flush();
    }
    seq++;
}
static void __cdecl on_exit_(Regs *r) {
    if (lkinit) EnterCriticalSection(&lk);
    on_exit2(r);
    if (lkinit) LeaveCriticalSection(&lk);
}

static BYTE *put32(BYTE *p, DWORD v) { *(DWORD *)p = v; return p + 4; }
static BYTE *rel(BYTE *p, BYTE op, void *to) { *p++ = op; return put32(p, (DWORD)to - (DWORD)(p + 4)); }

static int parsespec(const char *t, Spec *s) {
    char *e; int i;
    memset(s, 0, sizeof *s);
    for (i = 0; i < 8; i++) if (!strncmp(t, regn[i], 3)) { s->base = i; t += 3; break; }
    if (i == 8) {
        if (*t == 's') s->base = 8; else if (*t == 'a') s->base = 9; else if (*t == 'g') s->base = 10;
        else if (*t == 't') s->base = 11; else return 0;
        s->idx = strtoul(t + 1, &e, s->base >= 10 ? 16 : 0); t = e;
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
    else if (*t == '&') s->n = -6;
    else if (*t == '^') s->n = -7;
    else if (*t == '*') {          /* *N[:W1[:W2]]: default words +8 and +0xc; one offset: that word only */
        s->n = -100 - (int)strtol(t + 1, &e, 0); s->w1 = 8; s->w2 = 12; s->nx = 0;
        if (*e == ':') { s->w1 = (int)strtol(e + 1, &e, 0); s->w2 = -1; if (*e == ':') s->w2 = (int)strtol(e + 1, &e, 0); }
        if (*e == ':') s->nx = (int)strtol(e + 1, &e, 0);   /* *N:W1:W2:NEXT: the next pointer at +NEXT */
    }
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
    else if (!strcmp(nm, "fcoff")) LFC = (int)v;
    else if (!strcmp(nm, "fcdbg")) FCDBG = (int)v;
    else if (!strcmp(nm, "brcc")) LBRCC = (int)v;
    else if (!strcmp(nm, "extin")) LEXTI = (int)v;
    else if (!strcmp(nm, "labsym")) LLABS = (int)v;
    else if (!strcmp(nm, "casen")) LCASE = (int)v;
    else if (!strcmp(nm, "casenx")) LCASEN = (int)v;
    else if (!strcmp(nm, "fc64")) FC64 = (int)v;
    else if (!strcmp(nm, "ehop")) OPEH = v;
    else if (!strcmp(nm, "ulabop")) OPULAB = v;
    else if (!strcmp(nm, "fename")) { FENAME = (int)v; out("# fename %d\n", FENAME); }  /* marker: this DLL knows it */
    else if (!strcmp(nm, "fpdump")) out("# fpdump %d\n", (int)v);             /* marker: this DLL has the '&' dump */
    else if (!strcmp(nm, "bbin")) BBIN = (int)v;
    else if (!strcmp(nm, "bbstk")) BBSTK = (int)v;
    else if (!strcmp(nm, "gdump")) out("# gdump %d\n", (int)v);               /* marker: this DLL has the '^' dump */
    else if (!strcmp(nm, "gsbv32")) GSBV32 = (int)v;
    else if (!strcmp(nm, "gloop")) GLOOP = (int)v;
    else if (!strcmp(nm, "grhash")) GRHASH = v;
    else if (!strcmp(nm, "gnew")) GNEW = v;
    else if (!strcmp(nm, "gspb")) GSPB = (int)v;
    else if (!strcmp(nm, "gloopp")) GLOOPP = (int)v;
    else if (!strcmp(nm, "gsps")) GSPS = (int)v;
    else if (!strcmp(nm, "gspl")) GSPL = (int)v;
    else if (!strcmp(nm, "curfunc")) CURFUNC = v;
    else if (!strcmp(nm, "curfunctls")) CURFUNCTLS = v;
    else if (!strcmp(nm, "cfname")) CFNAME = (int)v;
    else if (!strcmp(nm, "licon")) LICON = (int)v;
    else if (!strcmp(nm, "snstr")) SNSTR = (int)v;
    else if (!strcmp(nm, "lnext")) LNEXT = (int)v;
    else if (!strcmp(nm, "lop")) LOP = (int)v;
    else if (!strcmp(nm, "lkind")) LKIND = (int)v;
    else if (!strcmp(nm, "lflg")) LFLG = (int)v;
    else if (!strcmp(nm, "lline")) LLINE = (int)v;
    else if (!strcmp(nm, "lsrc")) LSRC = (int)v;
    else if (!strcmp(nm, "ldst")) LDST = (int)v;
    else if (!strcmp(nm, "ltyw")) LTYW = (int)v;
    else if (!strcmp(nm, "lpay")) LPAY = (int)v;
    else if (!strcmp(nm, "lpay2")) LPAY2 = (int)v;
    else if (!strcmp(nm, "sname")) SNAME = (int)v;
    else if (!strcmp(nm, "skind")) SKIND = (int)v;
    else if (!strcmp(nm, "snum")) SNUM = (int)v;
    else if (!strcmp(nm, "ilraw")) ILRAW = (int)v;
    else if (!strcmp(nm, "ilsymw")) { ILSYMW = (int)v; out("# ilsymw %x\n", ILSYMW); }
    else if (!strcmp(nm, "lmd")) LMD = (int)v;
    else if (!strcmp(nm, "ilnum")) ILNUM = (int)v;
    else if (!strcmp(nm, "lmb")) LMB = (int)v;
    else if (!strcmp(nm, "lmi")) LMI = (int)v;
    else if (!strcmp(nm, "lms")) LMS = (int)v;
    else if (!strcmp(nm, "tlsidx")) TLSIDX = v;
    else if (!strcmp(nm, "ilpas")) { ILPAS = (int)v; out("# ilpas %x\n", ILPAS); }   /* marker: this DLL knows it */
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
        } else if (!strcmp(a, "kind")) {             /* kind <native> <14.00>: map a build's kind numbers */
            unsigned long a1, a2; int q;
            if (!kmapset) { for (q = 0; q < 256; q++) kmap[q] = (BYTE)(200 + (q & 0x3f)); kmapset = 1; }
            if (sscanf(line, "kind %lx %lx", &a1, &a2) == 2 && a1 < 256) kmap[a1] = (BYTE)a2;
        } else if (!strcmp(a, "op")) {
            char nm[20]; unsigned long v;
            if (sscanf(line, "op %lx %19s", &v, nm) == 2 && v >= OPCOUNT && v - OPCOUNT < 1024) strcpy(opx[v - OPCOUNT], nm);
        } else if (!strcmp(a, "func") && nffilt < MAXFF) {
            if (sscanf(line, "func %255s", ffilt[nffilt]) == 1) nffilt++;
        } else if (!strcmp(a, "flush")) eachflush = 1;
    }
    fclose(fp);
    if (GetEnvironmentVariableA("C2TRACE_LOG", path, sizeof path))
        logf = CreateFileA(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    InitializeCriticalSection(&lk); lkinit = 1;
    delta = (DWORD)base - imgbase;
    out("# c2trace base %x hooks %d\n", (DWORD)base, nhooks);
    if (nffilt) out("# func %d\n", nffilt);             /* marker: this DLL filters by function (`func`) */

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
    ex = LOADLIB_EXW                       # LoadLibraryExW(name, 0, 0): two more pushes (the Xbox 360 c2 imports no
    n = 0x21 + (4 if ex else 0)            # other loader); n = the cave's code length, the string follows it
    code = bytes.fromhex("837c240801") + b"\x75" + bytes([n - 0xc]) + bytes.fromhex("60" "e800000000" "5b")
    if ex:
        code += bytes.fromhex("6a00" "6a00")                                # push 0 (dwFlags); push 0 (hFile)
    code += b"\x8d\x83" + struct.pack("<i", (c + n) - (c + 0xd))            # lea eax,[ebx+str]
    code += b"\x50"                                                         # push eax
    code += b"\xff\x93" + struct.pack("<i", IAT_LOADLIBRARYA - (c + 0xd))   # call [ebx+IAT]
    code += b"\x61"                                                         # popad
    code += b"\xe9" + struct.pack("<i", ENTRY_RVA - (c + n))                # jmp original entry
    assert len(code) == n
    if LOADLIB_W or ex:
        out = code + "c2trace.dll\0".encode("utf-16-le")   # 0x21 + 24 / 0x25 + 24: inside the 0x40 the patch checks
        assert len(out) <= 0x40
        return out
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
        db = c2build.get("dll_build")         # a cross compiler (17.00 x64) builds c2trace.dll with an x86 cl
        rc, out = run_tool(["/nologo", "/O2", "/MT", "/LD", "/W2", src, "/Fo" + os.path.join(d, "c2trace.obj"),
                            "/Fe" + os.path.join(d, "c2trace.dll")], trace=False,
                           extra_env={"C2_BUILD": db} if db else None)
        if rc:
            sys.exit(out)
        _replace(os.path.join(TBIN, "c2trace.dll"), open(os.path.join(d, "c2trace.dll"), "rb").read())
    print("patched %s (entry -> cave at rva %x), built %s" % (os.path.join(TBIN, "c2.dll"), CAVE_RVA,
                                                             os.path.join(TBIN, "c2trace.dll")))
    # local to this checkout (work/ is never shared): e.g. copy the new DLLs to a compile server. Per checkout, not
    # per user: another checkout (c2scope next to this repo) must not run this one's deploy.
    hook = os.path.join(ROOT, "work", "c2trace-post-install")
    if os.path.isfile(hook):
        cmd = open(hook).read().strip()
        if cmd:
            print("running %s: %s" % (hook, cmd))
            subprocess.run(cmd, shell=True, cwd=ROOT)


def run_tool(args, trace=True, extra_env=None):
    e = os.environ.copy()
    if trace:
        e["VS8_ROOT"] = TRACE
    e.update(extra_env or {})
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "msvc.py")] + args, env=e,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace")
    return p.returncode, p.stdout + p.stderr


# Compile servers on a Windows machine, used for every traced compile when one is reachable. The clients come from
# C2TRACE_REMOTE (one path; "0" turns remote compiles off) or from the local file ~/.config/c2trace-remote: one client
# per line, tried in order, each optionally followed by the build prefixes it serves (`/path/client.py 13.10`).
# A client takes cl's arguments, MB_RC_TC (the traced compiler directory's name), MB_RC_TRACE_CFG and MB_RC_TRACE_LOG,
# and exits with cl's status, 75 (busy) or 76 (cannot compile this here). No client configured: compile locally.
def _remote_clients():
    path = os.path.expanduser("~/.config/c2trace-remote")
    out = []
    try:
        for line in open(path):
            parts = line.split()
            if parts and not parts[0].startswith("#"):
                out.append((os.path.expanduser(parts[0]), parts[1:]))
    except OSError:
        pass
    return out


REMOTE_CLIENTS = _remote_clients()
REMOTE_DEFAULT = REMOTE_CLIENTS[0][0] if REMOTE_CLIENTS else ""


def remote_configured():
    """Is a compile server configured (and not turned off with C2TRACE_REMOTE=0)? Tools that refuse to fall back to
    wine (effectmine, icount) refuse only then: with no server configured, every traced compile is local anyway."""
    v = os.environ.get("C2TRACE_REMOTE")
    if v == "0":
        return False
    return bool(v) or bool(REMOTE_CLIENTS)


def run_remote(args, cfg, log):
    """The traced compile on a Windows machine instead of under wine, or None to compile here (see REMOTE_CLIENTS).
    The client ships the source, the headers it reaches and the hook list, and writes the listing, object and trace
    log back to the paths asked for."""
    if os.environ.get("C2TRACE_REMOTE") == "0":
        return None
    if any(a in ("/Zi", "/ZI", "/Z7") for a in args) or any(a[:3].upper() == "/GL" for a in args):
        return None                                    # /GL objects belong to the local link (c2ltcg)
    clients = [(os.environ["C2TRACE_REMOTE"], [])] if os.environ.get("C2TRACE_REMOTE") else REMOTE_CLIENTS
    for client, builds in clients:
        if not os.path.isfile(client) or (builds and not any(c2build.name().startswith(b) for b in builds)):
            continue
        r = _run_remote_client(client, args, cfg, log)
        if r is not None:
            return r
    return None


def _run_remote_client(client, args, cfg, log):
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
    if p.returncode in (75, 76) or (not os.path.exists(log) and p.returncode == 0):
        if os.environ.get("C2TRACE_REMOTE_DEBUG"):
            sys.stderr.write("c2trace: remote compile declined (%d), compiling here\n%s" % (
                p.returncode, (p.stdout + p.stderr)[-1500:]))
        return None
    if not _REMOTE_SAID:
        _REMOTE_SAID.append(1)
        sys.stderr.write("c2trace: traced compiles run on the remote compile server (%s); C2TRACE_REMOTE=0 keeps "
                         "them local\n" % os.path.basename(client))
    return p.returncode, p.stdout + p.stderr


_REMOTE_SAID = []
_FPDUMP_SAID = []


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


def func_filter(pat):
    """The `func` lines of the trace config for a --func value: the decorated-name text it is looked up as
    (func_pat), split around anonymous-namespace hashes (the DLL tests plain substrings; the hash depends on the
    compile's path). [] selects everything."""
    p = func_pat(pat) if pat else ""
    return [x for x in _ANON_NS.split(p) if x and not x.isspace()][:4] if p else []


def curfunc_va():
    """c2 VA of _CurFunc (the FUNC being compiled) in this build."""
    return c2build.get("globals", {}).get("_CurFunc", "10d123ac") if c2build.table() else "10d123ac"


def write_cfg(cfg, img, hooks, maxn=None, exit_all=False, func=None):
    """The trace config c2trace.dll reads (%C2TRACE_CFG%) for these parsed hooks. `func` (or the environment
    variable C2TRACE_FUNC when func is None, else the tool's own --func; C2TRACE_FUNC= empty turns it off): log only
    while that function is compiled (DLL directive `func`)."""
    if func is None:   # C2TRACE_FUNC (set and empty: no filter), else the tool's --func
        func = (os.environ["C2TRACE_FUNC"] if "C2TRACE_FUNC" in os.environ else ARGV_FUNC) or None
    with open(cfg, "w") as f:
        if maxn is not None:
            f.write("max %d\n" % maxn)
        ff = func_filter(func)
        if ff:
            f.write("set curfunc %s\n" % curfunc_va())
            f.writelines("func %s\n" % x for x in ff)
        if os.environ.get("C2TRACE_ILMAX"):
            f.write("ilmax %d\n" % int(os.environ["C2TRACE_ILMAX"]))
        if os.environ.get("C2TRACE_ILPAS"):          # memory leaves print their PAS index (tools/loadcse.py)
            f.write("set ilpas %s\n" % (os.environ["C2TRACE_ILPAS"] if os.environ["C2TRACE_ILPAS"] != "1" else
                                        c2build.get("dll", {}).get("ilpas_off", "1c")))
        if os.environ.get("C2TRACE_ILRAW"):          # "T R" raw words after each tuple and leaf (c2ncol.py --il)
            f.write("set ilraw %x\n" % int(os.environ["C2TRACE_ILRAW"]))
        if os.environ.get("C2TRACE_ILSYMW"):         # with ilraw: "T S" the words of each leaf's symbol
            f.write("set ilsymw %x\n" % int(os.environ["C2TRACE_ILSYMW"]))
        if os.environ.get("C2TRACE_ILNUM"):          # tuple numbers after each instruction (tools/c2ncol.py)
            f.write("set ilnum %s\n" % (os.environ["C2TRACE_ILNUM"] if os.environ["C2TRACE_ILNUM"] != "1" else
                                        c2build.get("dll", {}).get("ilnum_off", "28")))
        if c2build.table():
            f.write("set base %s\n" % c2build.get("image_base"))
            f.writelines("set %s %s\n" % kv for kv in sorted(c2build.get("dll", {}).items()))
            f.writelines("op %s %s\n" % kv for kv in sorted(c2build.get("opnames", {}).items()))
            f.writelines("kind %s %s\n" % kv for kv in sorted(c2build.get("kinds", {}).items()))
        for k, h in enumerate(hooks):
            n, ins = steal_len(img, h["va"])
            f.write("# %s\nhook %d %x %d %d\n" % (h["name"], k, h["va"] - img.base, n, int(h["entry"] and (exit_all or bool(h["x"])))))
            if any(kind for _, kind, _, _ in ins):
                f.writelines("i %d %d %d %x\n" % t for t in ins)
            f.writelines("e %s\n" % s for s in h["e"])
            f.writelines("x %s\n" % s for s in h["x"])


def func_pat(pat):
    """The text a --func value is looked up as in decorated names (shared by every subcommand and the sweeps).
    `Class::method` -> `?method@Class@` (nested: `N::C::m` -> `?m@C@N@`; `C::C` / `C::~C` -> `??0C@` / `??1C@`):
    the qualifiers' end is left open, so a class inside any enclosing namespace matches too (`?method@Class@NS@@`);
    `::name` -> a global function (`?name@@` in C++ names; for C names use the plain form);
    anything else (a plain name, `method@Class`, a full decorated name) is returned unchanged: a substring."""
    if not pat or "::" not in pat:
        return pat
    parts = [x for x in pat.split("::") if x]
    if not parts:
        return pat
    name, scope = parts[-1], parts[:-1]
    end = "@" if scope else "@@"
    if scope and name in (scope[-1], "~" + scope[-1]):      # constructor / destructor: ??0C@ / ??1C@ (no @ after 0/1)
        return "??" + ("1" if name[0] == "~" else "0") + "@".join(scope[::-1]) + end
    return "?" + "@".join([name] + scope[::-1]) + end


def func_decl(pat):
    """A --func value as the start of a decorated member name where one is needed to find the definition in the
    source (rwsweep, helpersweep): `Class::method` and `method@Class` -> `?method@Class@` (any enclosing namespace
    still matches); others unchanged."""
    pat = func_pat(pat)
    m = re.fullmatch(r"(\w+)@(\w+)", pat or "")
    return "?%s@%s@" % m.groups() if m else pat


_ANON_NS = re.compile(r"\?A0x[0-9a-fA-F]+@")


def anon_ns(s):
    """`s` with every anonymous namespace's hash removed (`?A0x1a2b3c4d@` -> `?A0x@`): cl hashes the source file's
    path into it, so the same function is named differently when compiled at another path (a compile server)."""
    return _ANON_NS.sub("?A0x@", s) if s and "?A0x" in s else s


def func_match(pat, name):
    """True when the decorated `name` is selected by the --func value `pat` (None or '' selects everything).
    Anonymous-namespace hashes are ignored on both sides (anon_ns)."""
    p = anon_ns(func_pat(pat))
    name = anon_ns(name.strip('"'))  # some callers pass the logged value with its quotes (c2scratch)
    # the trace logs names cut to 79 characters: a full decorated name longer than that contains the cut name
    return not pat or p in name or (len(name) >= 79 and len(p) > len(name) and p.startswith(name))


def listing_proc(text, func):
    """(start, end) of the PROC..ENDP text of the function selected by `func` (any --func form) in a /FAsc listing;
    a PROC named exactly that wins, else the first whose name contains it (a decorated name is contained in the names
    of its local classes' members: `?normalize_inl@vec4n@?2??compute_normals@mesh@rgl@@QAEXXZ@QAEMXZ` comes before
    `?compute_normals@mesh@rgl@@QAEXXZ` in Mount & Blade's mesh listing).  KeyError when there is none."""
    pat = anon_ns(func_pat(func) or "")
    first = None
    for m in re.finditer(r"^(\S+)[ \t]+PROC\b", text, re.M):
        name = anon_ns(m.group(1))
        if name == pat:
            return m.start(), text.index("ENDP", m.end())
        if first is None and pat in name:
            first = m
    if first is not None:
        return first.start(), text.index("ENDP", first.end())
    raise KeyError("%s PROC not in the listing" % func)


def split_cl(argv):
    """(tool arguments, cl arguments): everything after the first `--` is passed to cl untouched, so the tool's own
    options may come anywhere before it (before or after the source path)."""
    if "--" in argv:
        k = argv.index("--")
        return argv[:k], argv[k + 1:]
    return argv, []


def keep_path(keep):
    """A --keep directory as an absolute path. Relative paths are taken against the caller's cwd, except that the
    repository root is not a scratch place: from there a relative path outside work/ goes under work/keep/ (a
    script that `cd`s here and passes its own relative `--keep b/k_x` wrote 512 such directories into the root)."""
    if os.path.isabs(keep):
        return keep
    ab = os.path.abspath(keep)
    root = os.path.abspath(ROOT)
    if os.path.abspath(os.getcwd()) == root and ab.startswith(root + os.sep) and \
            not ab.startswith(os.path.join(root, "work") + os.sep):
        ab = os.path.join(ROOT, "work", "keep", os.path.relpath(ab, ROOT))
        if not _KEEP_SAID:
            _KEEP_SAID.append(1)
            sys.stderr.write("c2trace: --keep %s is relative to the repository root: kept in %s instead\n" % (keep, ab))
    return ab


_KEEP_SAID = []


def run_trace(src, hook_texts, cl, maxn=None, keep=None, exit_all=False, quiet=False, func=None):
    """Compile src with the traced c2; returns (img, hooks, rc, cl output, raw log text).
    Relative `src` and `keep` paths are taken against the caller's cwd (resolved here, before anything runs elsewhere).
    func: a --func value; events are then logged only while that function is compiled (write_cfg; default: the
    environment variable C2TRACE_FUNC if set, else the tool's own --func; False or C2TRACE_FUNC= empty: everything)."""
    src = os.path.abspath(src)
    if not os.path.isfile(src):
        # before anything is written: a script that passed a relative source from the wrong cwd used to leave one
        # keep directory per call with only c2trace.cfg in it (the stray b/k_wm_b000.. at the repository root)
        sys.exit("c2trace: %s: no such source file" % src)
    keep = keep_path(keep) if keep else keep
    if open(os.path.join(TBIN, "c2.dll"), "rb").read() == open(ORIG_C2, "rb").read() or \
            not os.path.exists(os.path.join(TBIN, "c2trace.dll")):
        sys.exit("traced toolchain not installed: uv run tools/c2trace.py install")
    img = Image("c2")
    hooks = [parse_hook(img, h) for h in hook_texts]
    d = keep or tempfile.mkdtemp(prefix="c2trace_")
    os.makedirs(d, exist_ok=True)
    cfg, log = os.path.join(d, "c2trace.cfg"), os.path.join(d, "c2trace.log")
    write_cfg(cfg, img, hooks, maxn, exit_all, func)
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
        r = run_remote(args, cfg, log)
        if r and os.path.exists(log) and any(x.startswith("set fpdump 1") for x in open(cfg)) and \
                "# fpdump 1" not in open(log, errors="replace").read(4096):
            # the compile server's c2trace.dll predates the x87 view ('&', `set fpdump`): compile here instead
            if not _FPDUMP_SAID:
                _FPDUMP_SAID.append(1)
                sys.stderr.write("c2trace: the compile server's c2trace.dll has no x87 view ('&' spec): compiled here; "
                                 "redeploy %s there\n" % TBIN)
            os.remove(log)
            r = None
        if r and os.path.exists(log) and any(x.startswith("set gdump 1") for x in open(cfg)) and \
                "# gdump 1" not in open(log, errors="replace").read(4096):
            # the same for the globreg view ('^', `set gdump`, c2regs.py --pieces)
            if not _FPDUMP_SAID:
                _FPDUMP_SAID.append(1)
                sys.stderr.write("c2trace: the compile server's c2trace.dll has no globreg view ('^' spec): compiled "
                                 "here; redeploy %s there\n" % TBIN)
            os.remove(log)
            r = None
        rc, out = r or \
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


def pipe_safe(fn, *args):
    """fn(*args), but a reader that goes away (`c2trace.py regmap ... | head`) ends the program quietly with status
    141 (128 + SIGPIPE, as a C tool would), instead of a BrokenPipeError traceback. Only stdout's own pipe counts: a
    broken pipe elsewhere (a socket) is re-raised."""
    try:
        try:
            return fn(*args)
        finally:                             # also on sys.exit (argparse --help): the buffer is written here
            sys.stdout.flush()
    except OSError as ex:                    # Windows: a closed pipe is EINVAL on flush, not EPIPE
        if not isinstance(ex, BrokenPipeError) and not (os.name == "nt" and ex.errno == 22):
            raise
        try:
            sys.stdout.flush()
            ours = _stdout_closed()
        except OSError:
            ours = True
        if not ours:
            raise
        try:                                 # the interpreter flushes stdout again at exit: send that to /dev/null
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        os._exit(141)


def _stdout_closed():
    """True when stdout is a pipe whose reader has closed it (a zero-byte write does not tell; poll does)."""
    import select
    try:
        p = select.poll()
        p.register(sys.stdout.fileno(), select.POLLOUT)
        return any(ev & (select.POLLERR | select.POLLHUP) for _, ev in p.poll(0))
    except (OSError, AttributeError, ValueError):
        return os.name != "nt"               # Windows has no poll: stdout just flushed fine, so it is not closed


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
    ap.add_argument("--func")            # log only while this function is compiled (default: C2TRACE_FUNC)
    ap.add_argument("src")
    ap.add_argument("cl", nargs="*")     # cl arguments without `--` (c2ltcg.py raw passes them so)
    argv, cl = split_cl(sys.argv[1:])    # options anywhere before `--`; everything after it goes to cl unchanged
    a = ap.parse_args(argv)
    img, hooks, rc, out, text = run_trace(a.src, a.hook or DEFAULT_HOOKS, a.cl + cl, a.max, a.keep, a.exit,
                                          func=a.func)
    sys.stdout.write(out)
    sys.stdout.write(decode(text, hooks, a.raw))
    return rc


if __name__ == "__main__":
    sys.exit(pipe_safe(main))
