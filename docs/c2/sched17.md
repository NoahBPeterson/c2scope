# Schedule: static map of the x64 instruction scheduler in c2 17.00.61030 (x86_amd64 c2.dll)

Binary: `work/vs2012/bin/c2_x64_1700.dll` (PE32, base 0x10000000). All addresses are VAs in that image.
Method: static reading only (capstone via `work/port1700/c2dis.py`). Nothing was compiled or run.
Disassembly dumps of the functions read: `work/port1700/t/sched/*.s`. Helpers written for this map (all
`uv run python work/port1700/<tool>`):

- `optab.py k8|atom [NAME|HEXOP ...]`: OPSCHEDINFO rows with opcode names (names from `Opcode_name` 0x100226e8).
- `rd.py VA COUNT [w|d|b] [percol]`: dump a table from the image.
- `xref.py HEX...`: instructions whose bytes contain a 32-bit value (TLS offsets, globals).
- `callers.py NAME|VA...`: rel32 call/jmp/jcc sites that target an address.
- `hookchk.py VA...`: displaced instructions (>= 5 bytes) at VA and any branch that lands strictly inside them.
- `findptr.py HEXVA`, `near.py VA...`: pointer occurrences in data / nearest public symbol.

**[C]** = read from the instructions. **[I]** = inferred (from shape, from names, or by analogy with the x86
scheduler of `docs/c2/sched.md`). "TLS+x" = the per-thread compiler state block. Calls are `__fastcall`.

The model in one sentence **[C]**: per region, a list scheduler issues up to 3 instructions per cycle, each time
taking the first node of the ready list (sorted by dynamic priority, ties to the lower lexical index) whose
functional unit is not saturated, that is not a macro-fusible compare being held back for its `jcc`, that is not
waiting on a breakable producer, and whose earliest cycle is <= cycle + busy[unit]; static priority is
`B<<23 + L<<15 + nsucc<<10 + height<<5 + C<<1 (+ S for a float-class tuple)`.

---

## 1. Phases

### 1.1 When a function is scheduled

| step | where | rule |
|---|---|---|
| caller | `dop2_PreEncode` 0x10092050, call at 0x100920fd | the only caller of `Schedule` (`callers.py`) **[C]**. One pass, after register allocation, before `LoFinalRunAtEnd` and encoding |
| gate 1 | 0x10092066..0x100920eb | `Schedule(FUNC)` runs iff TLS+0xdcf4 != 0 (optimizing) and `FWvmSafe` (0x103f99a4) == 0 and `FUNC+0xe4 & 0x4000000` == 0 and `MOD+0x394 & 0x4000000` == 0 (MOD = [TLS+0x850]) and `FUNC+0xe4 & 0xa00000` == 0 **[C]**. What the three flag tests mean was not traced; by position they are the x86 "SEH / EH" exclusions **[I]** |
| gate 2 | `SchmdFuncNeedSchedule` 0x101a4330 | returns `TLS+0x1073c != 1` **[C]**. 0x1073c is the PGO mode (2 = optimize, ncolor.md); 1 = instrumenting **[I]**. The `int` argument of `Schedule` is not read anywhere **[C]** |
| pre-scheduling | TLS+0x11450 (`preSched`) | **never on**: the only store to TLS+0x11450 in .text is `mov [edi+0x11450], 0` at 0x100bf80c in `Schedule` (`xref.py 11450`: 1 write, 7 reads) **[C]**. So `SchmdBlockNeedPreSchedule` 0x10253980, `SchmdBlockTailForPreSched` 0x10253a70, the second weight table (0x10021cac) and the pre-sched branch of `SchmdComputeDynamicPriority` (cold 0x102df604) are dead. There is only the post-register-allocation pass |
| model | `SchmdInitialize` 0x10191ec0 | model index TLS+0x11c48 (word) = 1 if `MOD+0x398 & 0x40`, else 0 **[C]**. 0 = K8: table `OpSchedInfoK8` 0x10012788, unit function `SchmdGetK8FuncUnit` 0x100e5950. 1 = Atom: `OpSchedInfoAtom` 0x10023ac0, `SchAtomGetFuncUnit` 0x10226000 (cold 0x102df82a, 0x102df846) **[C]** |
| how chosen | `MscProcessSwitches` 0x100f91e9, `ModSetModuleFlags` 0x100fdeb4 | `MainMod+0x398` bit 6 = low bit of the switch variable 0x103e814c, which is zeroed at 0x100f8f1f and appears in exactly one switch-table entry: `L"-atom"` (rows of string, variable, type: 0x102f9f58 = string 0x10018130, 0x102f9f5c = variable 0x103e814c, 0x102f9f60 = type 0x101; the next row is `-NoPrefetch` with the public `NoPrefetch`, which fixes the row layout) **[C]**. So **at /O2 the model is K8** unless c2 gets `-atom`; cl's `/favor:ATOM` passes it (traced: MOD+0x398 = 0xa040 with it, 0xa000 without; section 7) |

### 1.2 `Schedule` 0x100bf7e0 (ecx = FUNC)

1. `SchmdFuncNeedSchedule`; TLS+0x11450 = 0; TLS+0x11410 = `DagCreateGraph()` 0x1019de00 (a 0x38-byte DAGGRAPH,
   node size 0x44, edge size 0x18); `SchmdInitialize`; `SchmdAdjustProEpi(FUNC)` 0x10192790. **[C]**
2. Walk from the first tuple (`[[FUNC+8]]+0x1c`) to the tuple with opcode 0x595; that tuple becomes the
   "last emitted" tuple TLS+0x1141c and scheduling starts at its successor **[C]**. 0x595 = OPPROLOGEND **[I]**
   (not in `pseudo_ops17.json`; `SchmdAdjustProEpi` moves it with Unlink/InsertAfter, as x86 does).
3. Region loop (0x100bf8fb) until the current tuple has opcode 0x593 (OPEXIT): **[C]**
   - TLS+0x11414 = start; TLS+0x11418 = tail = `SchBlockTail(start)` 0x10188910; next = tail.next.
   - if `SchNeedSchedule(start, tail)` 0x10192d50: `DagInitializeGraph(graph, 1)` 0x100b62a0 (recycles nodes and
     edges, installs the callbacks TLS+0xda78 = `SchmdInstrFuncUnitID` 0x100e47b0 and TLS+0xda7c =
     `SchmdDagLatency` 0x100e4820), `DagBuildGraph(graph, start, tail)` 0x100e29e0,
     `SchComputeStaticPriority(graph)` 0x100e65c0, `SchSchedGraph(graph)` 0x100e5440; then a loop over the
     emitted tuples (0x100bf89f..0x100bf9d4) that only rewrites the line word tuple+0x20 (calls
     `LinVirtualToLineRange`, `TupGetInlinedFrame`) **[C]**: debug line info, no effect on order **[I]**.
   - else the region is linked through unchanged (last.next = start, TLS+0x1141c = tail).
4. Link the exit tuple, `SchmdRestoreProEpi(FUNC)` 0x10196210, `freemem(8)`. **[C]**

### 1.3 Regions

`SchBlockTail(start)` **[C]**: walk tuples counting from 0; return value = the region's last tuple.

- stop after 81 tuples (count > 0x50) or at the end of the list: tail = the previous tuple;
- `SchmdTupEndsSchBlock(t)` 0x101889a0 returns t when **t or t.next** has one of these opcodes, so the tuple
  *before* such an instruction closes the region and the instruction itself starts (and ends) the next one:
  cli, sti, hlt, ud2, pause, fxsave/fxrstor(64), invlpg, lfence, mfence, sfence, lidt, sidt, the `lock ...`
  forms 0x11e..0x123 and 0x125..0x127, `_movsr` 0x158, prefetch* 0x1d7..0x1dc, rdmsr/_rdxmsr, rdtsc, rdtscp,
  wbinvd/wrmsr/_wrxmsr, xsave/xrstor/xgetbv/xsetbv 0x2c5..0x2cc, the VMX/SVM group 0x21f..0x230, invpcid and
  the xacquire/xrelease/xbegin forms 0x4e8..0x4f0, xabort/xend, and pseudo-opcode 0x577 (unnamed);
- by tuple kind byte (+0xc): 0x11 (branch), 0x13, 0x1a (LABEL): tail = this tuple (it is the region's last
  node); 0x18: tail = previous tuple; 0x16 with opcode 0x597: tail = previous tuple. 0x597 = OPEPILOGSTART and
  0x13 = SWITCH are **[I]** (x86 analogy); a start tuple with opcode 0x597 is skipped before the walk.

`SchNeedSchedule(start, tail)` **[C]**: 0 if start's opcode is 0x597; otherwise 1 iff at least **two
instruction tuples (flags bit 0) lie strictly before `tail`**. So a region needs two instructions plus its tail.

`SchmdAdjustProEpi` (read only at the top) **[C]**: skipped when TLS+0x1073c == 1 or `[CurFunc+0x5c] & 1`; walks
the prolog from the first tuple (kind 0x17) to opcode 0x595, remembering the last `_epush` (0x81) / opcode 0x7f
as the tuple that must stay, treats `_emov` 0x7e, `and` 0xa and `lea` 0x114 specially, moves the 0x595 marker up
behind that tuple (at most 10 moved instructions, counter TLS+0x11cac), and does the mirror walk back from the
last tuple (kind 0x18) to opcode 0x597. Which prolog instructions end up schedulable on x64 was **not decoded**.

### 1.4 DAG construction (`DagBuildGraph` 0x100e29e0, 7212 bytes; read in part)

Nodes **[C]**: an entry pseudo-node (tuple 0, flag 0x20 = control point), one node per tuple from start to tail
inclusive in list order, then an exit pseudo-node (the graph tail). Per node at creation (0x100e2b2d..0x100e2be5):

- +0x3a lexical index = running counter TLS+0xda44 (entry node = 0);
- flag bit 2 (0x04, "reads memory") = some source leaf has kind 6 or 2; bit 3 (0x08, "writes memory") = some
  destination leaf has kind 6 or 2 (leaf kinds: 1 REG, 2/3 symbol, 5/6 memory, 7 ICONST; port-1700.md);
- +0x3e unit = `SchmdInstrFuncUnitID(tuple)`; +0x3c latency byte = `SchmdDagLatency(node)` (section 2.4).

Edges: `DagMakeDep(from, to, type)` 0x100e4920 appends to the end of from's successor list and of to's
predecessor list, latency 0, `from.nsucc++`, `to.npred++`; `DagMakeOrUpdateDep` 0x100e4dc0 keeps one edge per
(from, to) pair and ORs the type bits **[C]**. Type constants seen at the call sites **[C]**: 1 true, 2 anti,
4 output, 0x20 / 0x40 / 0x80 memory (passed to `DagChkSymDefs` / `DagChkSymRefs` / `DagChkMemref*`), 0x2000
volatile, 0x80000 control. Same set as x86.

Latencies **[C]**: only these edges get a non-zero latency:

- **true register edge**: `lat = max(lat, producer.latency + SchmdAgiStall(edge, sym))`, or 0 when the producer
  is OPPAREN 0x517 (0x100e2db6..0x100e2dfc and `SchmdAdjustRegTrueDepStall` 0x10253900). `SchmdAgiStall`
  0x100e46d0 (K8) = 1 when the consumer has a memory leaf (kind 5/6 source, kind 6 destination) whose base
  (+0x30) or index (+0x34) register symbol is the edge's symbol; also 1 when the symbol is
  `[TLS+0x113f4]+0x1f4` (the stack pointer **[I]**), the producer is not push/pop/`_epush`/`_epop` and the
  consumer is one of those four; else 0.
- **output edge**: `lat = max(lat, T[model])`, T at 0x1000648c = {0 (K8), 1 (Atom)} (`SchmdAdjustRegOutputDepStall`).
- the edge into the region's final `jcc` found by `SchmdFindCBranchDep` (1.5): latency 1.

Because `SchmdDagLatency` returns table latency + 1, **every true edge has latency >= 2** on K8 (add 2, add with a
memory operand 5, mov load 5, mov store 4, lea 4, push/pop 4; section 2.4).

Control points (flag 0x20, `DagMakeControlPoint` 0x100e1710; call sites read, the function itself not) **[C for
the triggers]**: `int` 0xfa, `cli`, `sti`, pseudo-opcode 0x51b, an instruction whose first destination is the
register symbol FUNC+0x58 of CurFunc, two value tests at 0x100e41f5 (== 0x3eb) and 0x100e4282 (0x4f2..0x4f3:
xabort/xend if the value is the opcode, register not traced **[I]**), tuple kind 0x15,
tuple kind 0x16 (for opcode 0x59e only when `IsAnnotation`), and tuple kinds 0xf, 0x11, 0x13, 0x14, 0x17..0x1a
(except opcodes 0x577 and, for kind 0x14, 0x58f) which get a 0x80000 edge from the node TLS+0xda3c when that
node has no successor yet (0x100e34b9; "previous control point" is **[I]**). That calls are barriers is **[I]** from x86 (the kind numbers were not mapped to names here).

Zeroing idioms **[C]**: `xor`/`xorpd`/`xorps` (0x2b5..0x2b7) and `vxorpd`/`vxorps` (0x3e8, 0x3e9) with two
identical REG sources (and not a dummy globreg) get only anti/output edges (`DagMakeAntiOutputDeps` 0x100e4e10),
no true edge on the register. `or r, -1` (0x16d with ICONST -1) has its own path at 0x100e38c1 (not followed).

Memory disambiguation (`ItfPartialInterf` 0x100ddb70, `ItfSymsTotalInterf`, `StkIsOverlayedWithAddrTakenSyms`,
`ItfInterfVolatileFilter`) was **not read**; the calls sit where x86 has them, so sched.md rule 4 is the working
assumption **[I]**.

### 1.5 `SchComputeStaticPriority` 0x100e65c0 (ecx = graph)

1. `SchmdFindCBranchDep(head, tail)` 0x100e6fc0: if the last real node (tail.prev) has opcode 0xff..0x102
   (`_jcc`, `_jcc`, jcxz, jecxz), every predecessor with a true edge gets flag bit 1 (0x02, B); a predecessor
   with only a control edge whose instruction writes the register read by the branch's second source gets B,
   edge type |= 1, edge latency = 1. **[C]**
2. Backward pass from the exit node (height 0): `height(n) = 1 + max over successor edges e (height(e.to) +
   e.lat)`; a node without successors gets 1. **[C]**
3. Forward pass over head .. node before tail **[C]**:
   - for every successor edge: `SchmdDepIsBreakable(edge)` 0x100e67b0 (may set the break kind in edge+0x16 and
     increment `to.nbreak` +0x28);
   - if the node is not the head and `npred == nbreak` (no hard predecessor): `DagMakeBackwardControlDep(node)`
     0x100e71d0, an 0x80000 edge from the nearest earlier control point (whose `nsucc` grows *after* its own
     priority was computed);
   - critical path: head has C (flag bit 0); a node with C gives C to each successor s with
     `height(n) == height(s) + lat + 1`;
   - priority with the weight table W = [TLS+0x1140c] (9 words per model at 0x10006194 + 0x12*model; a negative
     entry means shift right):

         K8   W = {23, 5, 10, 1, 3, 15, 0, 0, 0}        Atom W = {23, 5, 10, 1, 25, 15, 0, 0, 0}

         static (+0x2c) = B << W[0]          // sets the final jcc's flags          1<<23
                        + height << W[1]     //                                      *32
                        + nsucc  << W[2]     // number of out-edges (+0x26)          *1024
                        + C      << W[3]     // on the critical path                 *2
                        + L      << W[5]     // reads memory (flag bit 2)            1<<15
                        + S      << W[6]     // writes memory (bit 3), only if (tuple+0x10 & 0x1f000) == 0x5000
         dynamic (+0x30) = static

     W[4] is the dynamic bonus shift (1.7). Type class 5 = floating point is **[I]**.

### 1.6 `SchSchedGraph` 0x100e5440 (ecx = graph) and `SchSelectInstrs` 0x100e5600 (ecx = cycle)

Setup **[C]**: issued count TLS+0x11444 = 0, TLS+0x11448 = 0, head/tail copied to TLS+0x1143c/0x11440, last issued
node TLS+0x11454 = 0, `SchmdResetFuncUnit` (busy[u] = 0, TLS+0x11c4c = 0), ready list emptied, head marked issued
(flag 0x10); each successor of head gets `npred--` and, when `npred == nbreak`, `SchAddToReadyList`. cycle
TLS+0x1144c = 0.

Loop **[C]**: while the ready list is non-empty and its head is not the exit node:

    n = SchSelectInstrs(cycle)                    // nodes in the issue array TLS+0x1142c[0..n-1]
    if 2 <= n <= 3: for each issued node: tuple+0xd &= ~4          // meaning unknown
    for each issued node, in slot order:
        append its tuple to the output chain (TLS+0x1141c); issued count++
        if 0 < opcode < 0x4f5:                    // K8
            if busy[unit] < 0: busy[unit] = 0
            busy[unit] += node.latency (+0x3c)    // Atom (cold 0x102d3a92): busy = max(busy, SchmdDagThroughput(node))
    cycle++; for every unit u < nUnits: busy[u]--

`SchSelectInstrs(cycle)` **[C]** (here `esi` = TLS+8, so `[esi+0x11418]` is TLS+0x11420 and so on):

1. `SchmdComputeDynamicPriority(readyHead, cycle)` (1.7).
2. If the ready count TLS+0x11428 > 1: the list is rebuilt by re-inserting every node in its current order with
   the rule of `SchAddToReadyList` (inlined at 0x100e57db..0x100e5880): **descending dynamic priority (+0x30,
   unsigned), equal priority: lower lexical index (+0x3a) first**.
3. For slot n = 0 .. issueRate-1 (issueRate = word TLS+0x11c3c: 3 on K8, 2 on Atom), scan the ready list from its
   head; return n as soon as a scan reaches the end of the list or the exit node. A node is **skipped** when
   - `unit = GetFuncUnit(node, &issueArray, n)` (pointer TLS+0x11c44) is negative, or
   - `SchmdShouldDelayForMacroFusion(node)` is non-zero (1.8), or
   - it has a predecessor edge with break kind >= 0x10 whose producer is not issued, is itself ready
     (`npred == nbreak`) and has `est <= cycle + 1`, or
   - `node.est (+0x34) > cycle + d`, with `d = max(busy[unit], 0)`.
4. The first node not skipped is issued:
   - if `SchmdTupRequiresSeparaterResource(tuple)`: TLS+0x11c50 = cycle, TLS+0x11c4c = issued count + 1 + n;
   - `SchRemoveFromReadyList`, issueArray[n] = node, flag |= 0x10, node+0x3e = unit;
   - if `npred > 0` (only breakable predecessors remain): `SchmdBreakDep(edge)` 0x100e5fa0 for each predecessor
     edge whose producer is not issued, until `npred == 0`;
   - if the previously issued node (TLS+0x11454) is a `lea` 0x114 and `SchmdHasFoldableAGI(last, node)` finds an
     edge last -> node with break kind 6, 8 or 10: `last.nsucc++`, `node.npred++`, `node.nbreak++`,
     `SchmdBreakDep(edge)`;
   - for each successor edge e, s = e.to: `s.est = max(s.est, e.lat + d + cycle)`; `wasReady = (s.npred ==
     s.nbreak)`; if e has a break kind: `s.nbreak--`; `s.npred--`; if `!wasReady && s.npred == s.nbreak`:
     `SchAddToReadyList(s)` (inserted by its current +0x30, same ordering rule);
   - TLS+0x11454 = node; next slot (the scan restarts at the list head; priorities are **not** recomputed inside
     a cycle).

`SchmdGetK8FuncUnit(node, issueArray, n)` 0x100e5950 **[C]**: `u = node+0x3e`; -1 if `busy[u] >= cap[u]`; -1 if the
tuple "requires a separate resource", TLS+0x11c4c != 0, `(issuedCount + n - TLS+0x11c4c) % 4 != 2` and
`cycle - TLS+0x11c50 < 100`; else u. `SchmdTupRequiresSeparaterResource` 0x100e59d0 (K8 only): opcode is bswap
0x11, rcl 0x200, rcr 0x203, rol 0x215 or ror 0x216. In words: after one of these five is issued as the k-th
instruction of the region, the next one may only be issued as instruction k+3, k+7, ... (or 100 cycles later).

`SchmdBreakDep` (head read) **[C]**: rewrites the displacement (+0x38) of the memory leaf of the consumer (break
kind < 0x10) or of the producer (kind >= 0x10), removes the edge (`DagRemoveDep`), and when edge+0x16 bit 5 is set
removes the producer from the DAG (`SchRemoveDag` 0x102262d0: TLS+0x11448++, its successors are resolved with
`SchResolveDep` 0x10226320, which can call `SchAddToReadyList`); it clears tuple+0xd bit 2 on both tuples. So a
break can change operands and can delete an instruction. The per-kind cases (jump table 0x100e6408) were not
decoded.

### 1.7 `SchmdComputeDynamicPriority(readyHead, cycle)` 0x100e5be0, K8 path **[C]**

Returns at once when the list has fewer than two nodes or W[4] < 0 (so +0x30 keeps whatever it held). Otherwise,
for each ready node A in list order (nodes without a tuple are skipped):

    A.dyn = A.static
    if A.est > cycle: continue
    if SchmdTupRequiresSeparaterResource(A.tuple): A.dyn = A.static + (1 << 30); continue
    B = first node in the ready list with a tuple, B.est <= cycle, B != A
    if none: B = first successor of A (edge order) with B.npred == 1, edge latency 0 and B.est <= cycle
    if none: continue
    dA = SchmdDagDecoder(A); dB = SchmdDagDecoder(B)
    if (dA == 3) != (dB == 3): continue
    A.dyn = A.static + (1 << W[4]);  B.dyn = B.static + (1 << W[4])        // +8 on K8

`SchmdDagDecoder` 0x100e5d80: OPSCHEDINFO +4, two bits chosen by operand form (2.4); 1/2/3 = K8 DirectPath
single / double / VectorPath **[I]**. Net effect: a node that can issue this cycle gets +8 when the first other
issuable ready node is of the same "vector or not" class; +8 outranks the critical-path bit (2) and the float bit
(1) and never reaches the height field (32). Only one B is tried per A, and B's +8 is overwritten when the outer
loop reaches B itself (when B is later in the list) or stays (when B was earlier, or is a not-yet-ready successor).

Atom path: `SchAtomPairing(readyHead, cycle)` 0x10226160 (cold 0x102df5f8): section 7.2.

### 1.8 `SchmdShouldDelayForMacroFusion(node)` 0x100e5a30 (K8 only; Atom returns 0) **[C]**

Returns 1 (do not issue now) when all of these hold:

- `node.nsucc == 1`, the successor S (first successor edge) is an instruction with opcode 0xff (`_jcc`; 0x100,
  jcxz, jecxz do not count) and **`S.npred != 1`** (the branch still waits for someone else);
- the node is, with src1 = first source leaf, src2 = second:
  - `add` 3, `and` 0xa, `sub` 0x29e: src1 is REG; src2 is not a kind-2 leaf whose symbol (`[leaf+0x24]`) has kind
    byte 7 or 8;
  - `cmp` 0x32, `test` 0x2a4: src1 is REG or src2 is REG; neither is a kind-2 leaf with symbol kind 7 or 8;
  - `dec` 0x75, `inc` 0xf7: src1 is REG;
- the branch condition code `cc = S.tuple+0xf & 0x1f` is in: 1..6 for inc/dec; 1..10 for add/cmp/sub; 1..16 for
  and/test.

Reading **[I]**: these are the Sandy Bridge macro-fusion pairs (cc 1..6 = e/ne/l/ge/le/g, 7..10 = b/ae/be/a,
11..16 = s/ns/p/np/o/no; symbol kinds 7/8 = operands that cannot fuse). Since the final branch is a control
point that depends on the whole region, **a fusible flag setter is held until it is the branch's last unissued
predecessor and comes out immediately before the `jcc`**, although it carries the B bonus (1<<23). A flag setter
that is not fusible, or that has a second successor (a later writer of one of its registers, a setcc/cmov), is
not held and goes as early as its inputs allow, as on x86.

---

## 2. Layouts

### 2.1 DAGNODE (0x44 bytes) **[C]**

| off | size | field |
|---|---|---|
| +0x00 / +0x04 | 4 / 4 | next / prev node in lexical order |
| +0x08 | 4 | first predecessor edge (chain through edge+4) |
| +0x0c | 4 | first successor edge (chain through edge+0) |
| +0x10 / +0x14 | 4 / 4 | ready-list next / prev |
| +0x1c | 4 | tuple (0 for the entry and exit nodes) |
| +0x20 | 4 | zeroed at creation; use not seen |
| +0x24 | 2 | unissued predecessors (`npred`) |
| +0x26 | 2 | successors (`nsucc`) |
| +0x28 | 2 | breakable predecessors (`nbreak`); ready when `npred == nbreak` |
| +0x2c | 4 | static priority |
| +0x30 | 4 | dynamic priority (ready-list key) |
| +0x34 | 4 | earliest cycle (`est`) |
| +0x38 | 2 | height |
| +0x3a | 2 | lexical index |
| +0x3c | 1 | latency (`SchmdDagLatency`) |
| +0x3e | 2 | functional unit (overwritten with the unit used at issue) |
| +0x40 | 1 | flags: 0x01 C critical path, 0x02 B sets the final branch's flags, 0x04 L reads memory, 0x08 S writes memory, 0x10 issued, 0x20 control point |

### 2.2 DAG edge (0x18 bytes) **[C]**

+0x00 next edge of the same producer, +0x04 next edge of the same consumer, +0x08 from, +0x0c to, +0x10 type bits
(1 true, 2 anti, 4 output, 0x20/0x40/0x80 memory, 0x2000 volatile, 0x80000 control), +0x14 latency (word),
+0x16 byte: bits 0..4 break kind (0 = not breakable; >= 0x10 = the producer's operand is rewritten; 6, 8, 10 =
foldable into a preceding lea), bit 5 = "remove the producer when broken", bits 6..7 preserved.

### 2.3 DAGGRAPH (0x38 bytes, `DagCreateGraph`) **[C]**

+0x00 head (entry node), +0x04 tail (exit node), +0x08 / +0x0c zeroed, +0x10 = 8 (memory pool), +0x14 = 0x44 (node
size), +0x18 = 0x18 (edge size), +0x1c free node list, +0x20 free edge list.

### 2.4 OPSCHEDINFO row: 12 bytes, three dwords, index = tuple opcode, valid for opcode < 0x4f5 **[C]**

The publics give 15320 bytes for `OpSchedInfoK8`; 1270 rows * 12 = 15240, the last 80 bytes are the unnamed
tables of 2.5. The two models read the three dwords differently:

**K8** (`optab.py k8 ...`):

- +0 latency word. Bit 16: `SchmdLatencyCallbacks(tuple)` 0x100e4a70 gives the value. Otherwise byte 0 (signed) =
  base, byte 1 (signed) = extra when any source or destination leaf has kind 6, 3, 4 or 2. **Node latency =
  value + 1** in both cases. Bits 17, 18, 19 are set on groups of rows (non-temporal/locked, implicit-operand
  ops, x87) and were not seen read.
- +4 decoder: bit 6 = `SchmdDecoderCallbacks` 0x100e6520; else `(dword >> s) & 3` with s = 2 if a source leaf is
  memory, else 4 if a destination leaf is memory, else 0. Bit 7 marks the x87/unsupported rows.
- +8 unit mask: `SchmdInstrFuncUnitID` returns the first unit u (0..4) with `mask & M[u]`, M at 0x10016338 +
  0x14*model = {2, 1, 4, 8, 0x10} for K8; no match (mask 0) gives 0. So: unit 0 = integer (274 rows, mask 2) and
  everything unmapped, unit 1 = mul/imul/idiv (5 rows, mask 1; `div` has mask 2), unit 2 = SSE add/compare and
  the integer-SIMD and convert rows (masks 4, 0xc, 0x1c), unit 3 = mulps/divps/and/or/xor ps-pd (mask 8), unit 4
  = SSE moves and non-temporal stores (mask 0x10).

Values that matter for integer code (node latency, i.e. already +1) **[C]**:

| instruction | latency |
|---|---|
| add adc and or xor sub cmp test inc dec neg not shl shr sar rol ror cmovcc movsx movsxd movzx | 2; 5 with a memory operand (row 0x00000301) |
| mov 0x13b, `_emov` 0x7e (callback, default case 0x100e4ae4) | 5 load (memory source), 4 store, 2 register/immediate |
| lea 0x114 (callback) | 4 (5 when the destination size field is 2) |
| push, pop, `_epush`, `_epop` | 4 |
| setcc | 2 (4 with memory) |
| `_jcc` 0xff/0x100, jcxz, jecxz | 2 |
| call | 4 (5 with memory); ret 6 |
| imul 0xf4..0xf6, mul 0x163 (callback 0x100e4c2c; m = 1 if a source is memory) | size 1 or 4 bytes: 3(m+1)+1; 2 bytes: 3m+5; 8 bytes: 3m+6 |
| div 0x76 | 8*size + 9; idiv 0xf3: 8*size + m + 11 |
| bswap cdq cdqe cbw | 2 |
| addss/addsd 5 (7 mem), mulss 5, divss 17, pxor 3, xorps 4 (6), comiss 5, movaps/movss/movsd: callback | |
| pseudo-opcodes (>= 0x4f5) | 1, unit 0 |

The other callback cases (bt*, bsr, the SSE moves through the jump table 0x100e4cd0, rcl/rcr, cold part
0x102df85b = the Atom version) were not tabulated.

**Atom** (`optab.py atom ...`): +0 latency (same decoding, callback cold 0x102df85b), +4 throughput
(`SchmdDagThroughput` 0x10253be0: bit 16 callback `SchmdThroughputCallbacks`, else byte 0 + 1 + byte 1 if memory),
+8 port/pairing word (`SchmdDagIssuePort` 0x10253ab0: bit 17 callback `SchmdPortCallbacks`; `& 0xf0`, or
`(>> 8) & 0xf0` for a memory form when bits 12..15 are set); unit masks M = {2, 1, 4, 0, 0}. Callbacks and
values: section 7.1.

### 2.5 Small model tables (unnamed data, indexed by model 0 = K8, 1 = Atom) **[C]**

| table | values | goes to |
|---|---|---|
| 0x1001080c (word) | 3, 2 | TLS+0x11c3c issue rate |
| 0x1000657c (word) | 2, 0 | TLS+0x11c3e (not seen read on the K8 path) |
| 0x10009fc4 (word) | 5, 3 | TLS+0x11c40 number of units |
| 0x10016310 + 20*model (5 pairs of words) | K8 (10,1) (4,1) (5,1) (5,1) (5,1); Atom (1,1) (5,5) (1,1) | unit u: cap, second word |
| 0x10016338 + 20*model (5 dwords) | K8 2, 1, 4, 8, 0x10; Atom 2, 1, 4, 0, 0 | unit masks |
| 0x10006194 + 18*model (9 words) | weights W (1.5) | TLS+0x1140c |
| 0x1000648c (word) | 0, 1 | output-dependence latency |

### 2.6 TLS fields **[C as accessed; names I]**

| TLS offset | meaning |
|---|---|
| 0x850 | current MOD; MOD+0x398 bit 6 = Atom model |
| 0xdccc | CurFunc |
| 0xdcf4 / 0xdd08 / 0x1073c | optimizing / favor speed (read in `SchmdDepIsBreakable`) / PGO mode |
| 0xda30 | graph being built; 0xda3c last control point; 0xda44 lexical counter; 0xda48 node count; 0xda4c edge count |
| 0xda50 / 0xda54 | optional node / edge creation callbacks (0 for the scheduler) |
| 0xda78 / 0xda7c | unit callback (`SchmdInstrFuncUnitID`) / latency callback (`SchmdDagLatency`) |
| 0x1140c | weight table W |
| 0x11410 | the DAGGRAPH |
| 0x11414 / 0x11418 | region first tuple / region tail tuple |
| 0x1141c | last tuple of the output chain |
| 0x11420 / 0x11424 / 0x11428 | ready-list head / tail / count |
| 0x1142c, 0x11430, 0x11434 | issue array (nodes issued this cycle) |
| 0x1143c / 0x11440 | entry node / exit node |
| 0x11444 | instructions issued so far in the region |
| 0x11448 | nodes removed by `SchRemoveDag` |
| 0x1144c | current cycle |
| 0x11450 | preSched (always 0) |
| 0x11454 | last issued node |
| 0x11c00 + 12*u | unit u: +0 cap (word), +2 word, +4 word 0, +8 busy counter (dword, i.e. TLS+0x11c08 + 12*u) |
| 0x11c3c / 0x11c3e / 0x11c40 | issue rate / second model word / number of units |
| 0x11c44 | GetFuncUnit function pointer |
| 0x11c48 | model index (word) |
| 0x11c4c / 0x11c50 | 1-based output position / cycle of the last "separate resource" instruction |
| 0x11c54, 0x11cac | `SchmdAdjustProEpi` state (saved tuple, moved-instruction count) |
| 0x11cb4 | OPSCHEDINFO table of the model |

---

## 3. Differences from the x86 scheduler (docs/c2/sched.md) that change the predicted order

1. **Weights [C]**: height counts 32 per level instead of 1024, a successor still counts 1024, and the
   critical-path bit now has weight 2 (x86: none). One extra out-edge outweighs 31 levels of height; the memory
   bonus (1<<15) equals 32 successors; the order of terms is B, then L + nsucc + height summed, then the dynamic
   +8, C, S.
2. **Latencies [C]**: table latency + 1, so every true edge is >= 2 and heights grow by latency + 1 per chain
   link: an `add` link adds 3 to the height, a load link 6, a `lea` link 5 (plus 1 for an address-generation use).
   x86 used the P5 table with 1 for most integer ops.
3. **Dynamic priority is live [C]**: +8 for nodes that can pair by decoder class, +2^30 for an issuable
   bswap/rcl/rcr/rol/ror; the ready list is re-sorted at the start of every cycle. On x86 dynamic == static.
4. **Macro-fusion hold [C]**: a fusible cmp/test/add/sub/and/inc/dec feeding only the region's `jcc` is issued
   last, directly before the branch. This reverses x86 rule "compare early" for the common case; the early
   compare survives only for non-fusible forms (memory with immediate, condition codes outside the sets) and
   for flag setters with a second successor.
5. **Three issues per cycle and five units [C]**: 3 slots per cycle (x86 also 3) but units are int (cap 10),
   multiply (4), and three SSE units (5 each); a unit refuses work while `busy >= cap` and busy grows by the
   latency (>= 2) per issue and falls by 1 per cycle. Integer code alone therefore runs: 3 issues, 3 issues,
   then about one issue every other cycle; SSE instructions interleave with integer ones by unit. As on x86 the
   `est` test adds `busy[unit]`, so latency by itself seldom delays a consumer.
6. **Separate-resource spacing [C]**: two of bswap/rcl/rcr/rol/ror must be 3 (mod 4) instructions apart.
7. **lea folding at issue time [C]**: a node issued right after a `lea` can have the lea folded into its address
   (break kinds 6, 8, 10), and broken dependencies can delete the producer (`SchRemoveDag`).
8. **Zeroing idioms carry no true dependence [C]** (xor/xorps/xorpd/vxorps/vxorpd of a register with itself).
9. **Region ends [C]**: the same 81-tuple limit and branch/label walls, plus the instruction list of 1.3 (each such
   instruction is isolated in its own unscheduled region). A region needs two instructions before its tail.
10. **Prolog [C in part]**: `_epush` tuples stay in front of OPPROLOGEND (x86 let callee-saved pushes float).
11. **Unchanged [C]**: DAGNODE / edge layouts and flag bits, edge type constants, the height recurrence, the
    ready-list ordering rule, `est` update, the 0x80000 edge from the previous control point for nodes without a
    hard predecessor, B flag from `SchmdFindCBranchDep`, preSched never set.

Simulator outline (K8), all steps **[C]**:

    build nodes/edges; latency, unit, L/S flags; FindCBranchDep; heights; DepIsBreakable marks; C bits; static prio
    ready = successors of entry with npred == nbreak, inserted by (dyn desc, lex asc); cycle = 0; busy[*] = 0
    while ready and ready[0] is not exit:
        if len(ready) > 1: recompute dyn (1.7); stable re-insert by (dyn desc, lex asc)
        issued = []
        for n in 0..2:
            pick first node in ready that passes the four tests of 1.6 step 3 (unit test uses n and len(issued))
            if none: break
            issue it (1.6 step 4): est of successors uses d = max(busy[unit], 0) as read *before* this cycle's charges
            issued.append(node)
        for node in issued: busy[u] = max(busy[u], 0) + latency (real opcodes only)
        cycle += 1; busy[*] -= 1

Note that `busy` is charged only after `SchSelectInstrs` returns, so all slots of one cycle see the same counters.

---

## 4. Hook points

Displaced bytes checked with `hookchk.py`: whole instructions, >= 5 bytes, no rel32 or short branch in .text lands
strictly inside any of them **[C]**. "abs" = the displaced bytes contain an absolute address (relocated).

### 4.1 Entries

| VA | function | displaced | on entry |
|---|---|---|---|
| 0x100bf7e0 | `Schedule` | 6: `push ebp; mov ebp,esp; sub esp,0x10` | ecx = FUNC (pre-schedule IL) |
| 0x100e29e0 | `DagBuildGraph` | 6: `push ebp; mov ebp,esp; sub esp,0x28` | ecx = graph, edx = first tuple, [esp+4] = tail tuple |
| 0x100e65c0 | `SchComputeStaticPriority` | 6: `push ebp; mov ebp,esp; sub esp,0x1c` | ecx = graph (DAG built, heights not yet) |
| 0x100e67b0 | `SchmdDepIsBreakable` | 6: `push ebp; mov ebp,esp; sub esp,0x14` | ecx = edge; called once per successor edge of every node before the tail, in node order; eax on exit = 1 if marked |
| 0x100e5440 | `SchSchedGraph` | 10: `push ebp; mov ebp,esp; push ecx; mov edx,[0x10304a04]` (abs) | ecx = graph; region start; all node fields final except est/dyn |
| 0x100e5600 | `SchSelectInstrs` | 6: `push ebp; mov ebp,esp; sub esp,0x20` | ecx = cycle; on exit eax = n, nodes in TLS+0x1142c[0..n-1] |
| 0x100e5be0 | `SchmdComputeDynamicPriority` | 6: `push ebp; mov ebp,esp; sub esp,0x10` | ecx = ready head, edx = cycle |
| 0x100e5ea0 | `SchAddToReadyList` | 6: `mov edx,[0x10304a04]` (abs) | ecx = node **becoming ready**; key node+0x30; cycle = TLS+0x1144c; est node+0x34 already final for this arrival |
| 0x100e5e40 | `SchRemoveFromReadyList` | 6: `mov eax,fs:[0x2c]` | ecx = node (issue, or `SchRemoveDag`) |
| 0x100e5950 | `SchmdGetK8FuncUnit` | 9: `push ebp; mov ebp,esp; mov edx,[0x10304a04]` (abs) | ecx = node, edx = &issue array, [esp+4] = slot n; exit ax = unit or -1 (`ret 4`) |
| 0x100e5a30 | `SchmdShouldDelayForMacroFusion` | 6: `mov edx,[0x10304a04]` (abs) | ecx = node; exit eax = 1 when held |
| 0x100e59d0 | `SchmdTupRequiresSeparaterResource` | 6: `mov eax,fs:[0x2c]` | ecx = tuple |
| 0x100e5fa0 | `SchmdBreakDep` | 6: `push ebp; mov ebp,esp; sub esp,0xc` | ecx = edge being broken (from +8, to +0xc, kind +0x16 & 0x1f) |
| 0x100e4920 | `DagMakeDep` | 5: `push ebp; mov ebp,esp; push ecx; push ecx` | ecx = from, edx = to, [esp+4] = type; misses the edges `DagBuildGraph` creates inline |
| 0x10226000 / 0x10226160 | `SchAtomGetFuncUnit` / `SchAtomPairing` | 5: `push ebp; mov ebp,esp; push ebx; push esi` / 6: `push ebp; mov ebp,esp; sub esp,0xc` | Atom model only |

`SchAddToReadyList` is *not* called by the per-cycle re-sort (inlined), so it fires exactly once per node that
becomes ready (callers: 0x100e54c2 initial, 0x100e57d4 after an issue, tail jump from `SchResolveDep`). **[C]**

### 4.2 Mid-function

| VA | in | displaced | what is observable |
|---|---|---|---|
| 0x100bf883 | `Schedule`+0xa3 | 6: `mov ecx,[edi+0x11410]` | **whole DAG final** (after `SchComputeStaticPriority`, before `SchSchedGraph`): edi = TLS, graph = [edi+0x11410]; walk nodes from [graph] via node+0, edges via node+0xc / edge+0. Best single place for heights, priorities, flags, edge types, latencies and break kinds (includes the edges added by `DagMakeBackwardControlDep`, which the `SchmdDepIsBreakable` hook does not see) |
| 0x100bf88e | `Schedule`+0xae | 6: `mov eax,[edi+0x1141c]` | region scheduled; edi = TLS, esi = the tuple that preceded the region, so esi+4 chains the region in its new order |
| 0x100e5649 | `SchSelectInstrs`+0x49 | 7: `xor eax,eax; mov ebx,edx; mov [ebp-0x18],ebx` | once per cycle, after dynamic priorities and the re-sort: esi = TLS+8, ready list from [esi+0x11418] via node+0x10 with keys node+0x30; cycle = [ebp-4] |
| 0x100e56fb | `SchSelectInstrs`+0xfb | 7: `mov ecx,edi; call SchRemoveFromReadyList` (rel32 call, re-encode) | **"node N selected at cycle C with priority P"**: edi = node (P = [edi+0x30], static [edi+0x2c], tuple [edi+0x1c]), cycle = [ebp-4], unit = [ebp-0xc], busy offset d = [ebp-0x10], slot n = ebx = [ebp-0x18], esi = TLS+8. Reached for every issued node (also from the separate-resource path 0x100e5945) |
| 0x100e5702 | `SchSelectInstrs`+0x102 | 6: `mov eax,[ebp-8]; mov [esi+eax],edi` | same data, after the node left the ready list |
| 0x100e573d | `SchSelectInstrs`+0x13d | 5: `mov ebx,[edi+0xc]; test ebx,ebx` | after dependency breaking and lea folding for the issued node (edi), before its successors are released |
| 0x100e57a7 | `SchSelectInstrs`+0x1a7 | 6: `mov [eax+0x11454],edi` | issue of edi complete (successors released); eax = TLS |
| 0x100e5512 | `SchSchedGraph`+0xd2 | 5: `mov ebx,eax; cmp ebx,1` | eax = n issued this cycle; esi = TLS; cycle = [esi+0x1144c]; nodes [esi+0x1142c + 4*i] |
| 0x100e5549 | `SchSchedGraph`+0x109 | 6: `mov eax,[esi+0x1141c]` | per emitted node, in output order: edx = node, esi = TLS, ebx = nodes left in this cycle, busy not yet charged |
| 0x100e5521 | `SchSchedGraph`+0xe1 | 6: `inc dword [esi+0x1144c]` | end of cycle, busy counters charged (TLS+0x11c08 + 12*u), before the decrement |
| 0x10092102 | `dop2_PreEncode`+0xb2 | 6: `mov [esi+0xdcbc],ebx` | `Schedule` returned: edi = FUNC (post-schedule IL), esi = TLS |

A tracer equivalent to `tools/c2sched.py`'s hooks: `Schedule` entry (0), `SchSchedGraph` entry (1),
`SchAddToReadyList` entry reading `ecx>0x1c, ecx>0x2c, ecx>0x38, ecx>0x24, ecx>0x40, ecx>0x34, cycle, ecx>0x3c,
ecx>0x1c>8` (2; the opcode is at tuple+8 here, not +4), `SchSelectInstrs` entry/exit with the three issue slots at
TLS+0x1142c (3), 0x10092102 (4), and a DAG walk at 0x100bf883 in place of the x86 per-edge hook (5). The x86
script reads globals by address; here every one of them is `TLS + offset`.

---

## 5. Not read / open

- `DagBuildGraph` beyond node creation, the true-edge latency code and the call sites listed in 1.4;
  `DagMakeControlPoint`, `DagChkSymDefs/Refs`, `DagChkMemrefDefs/Refs`, `DagMakeAntiOutputDeps`,
  `SchmdPseudoCCDef` bodies; the interference functions. Which tuple kinds 0xf..0x1a are (calls, returns, ...).
- `SchmdDepIsBreakable` case table (producers add/dec/inc/lea/mul?/sub for true edges, consumers
  add/dec/inc/lea/sub for anti edges, displacement range test at 0x100e6c84, favor-speed test) and
  `SchmdBreakDep` per kind.
- `SchmdAdjustProEpi` / `SchmdRestoreProEpi` in full; `SchmdUpdateProLogList`, `SchmdProLogListIndex`.
- The Atom model: read in section 7 (`SchAtomPairing`, `SchAtomGetFuncUnit`, `SchAtomCanBePaired`, the three
  `SchAtom*Stall` functions, the Atom latency / throughput / port callbacks); open there: two break cases and the
  earlier-phase Atom tests.
- The meaning of tuple flag +0xd bit 2, cleared for instructions issued in groups of 2 or 3 and on both ends of a
  broken dependence.
- Names of pseudo-opcodes 0x595, 0x597, 0x577, 0x51b, 0x58f, 0x59e and of the FUNC / MOD flag bits in gate 1.
- Nothing here was checked against a compile; the x86 map found two of its static predictions wrong only by
  probing (sched.md "Probes"), so rules 1.6 to 1.8 should be confirmed with the selection hook 0x100e56fb first.

---------------------------------------------------------------------------------------------------

## 6. Checked against traces (tools/c2sched17.py, 2026-11-21)

`c2sched17.py SRC --func F [--ready] --c2 17.00` hooks the issue point 0x100e56fb (node, cycle, unit), the
region entry (SchSchedGraph) and, with --ready, the per-cycle sorted ready list 0x100e5649.

- **Static priority** (1.5) as read: every issued node's +0x2c decomposes as B<<23 + L<<15 + nsucc<<10 +
  height<<5 + C<<1 + S with the flags at +0x40 (e.g. a flag-setting `cmp` 0x800462 = B, nsucc 1, height 3, C).
- **Dynamic priority** (1.7): +8 on the nodes that paired, else static.
- **Macro-fusion hold** (1.8) seen: a `cmp` with the highest priority in the ready list is passed over and
  issues last, directly before its `_jcc`.
- **Issue order** on zlib's deflate / inflate / trees (17,000 events): of the issues that can be checked against
  the logged ready list, 2573 were its head and 167 skipped a higher node (the skip rules of 1.6); 1844 issued a
  node that became ready during the cycle, and 551 fell in cycles where the list was not re-sorted (one node),
  neither checkable from this probe.

**Replay model** (`c2sched17.py SRC --check`: `simulate` in the tool). From the traced DAG (each issued node's
successor edges with latencies, its static priority, latency and unit, its opcode and operand kinds), the rules
of 1.6 to 1.8: at each cycle's start the dynamic priority of 1.7 (decoder classes from OpSchedInfoK8 +4 by the
operand forms: +8 to a pair of issuable nodes both VectorPath or both not), up to 3 issues per cycle, the ready
list by dynamic priority descending then lexical index, a node
skipped while its unit is at its cap, while it is a macro-fusible flag setter whose only successor is a `_jcc`
still waiting for others, or while `est > cycle + max(busy, 0)`; at issue each successor's `est = max(est, lat +
busy + cycle)`; after the cycle `busy[unit] += latency` for each issued node, then every busy count drops by 1.
On zlib at /O2 (12 files): **1152 of 1173 regions in exactly the traced order** (98.2%; deflate 261/262, trees
215/216, inflate 221/226, gzio 186/195). With the pairing bonus read from the trace instead (its value at issue
only): 1125.

Not done: breakable dependences and lea
folding (SchmdBreakDep), DAG construction itself (memory disambiguation: the edges are read from the trace). The
Atom path: section 7.

### 6.1 Predicted DAG: the region's DAG and static priorities from its IL (`c2sched17.py --predict`, 2026-11-21)

`c2sched17.py SRC --predict -- FLAGS` (or `--il --export F.json`, then `c2sched17.py F.json --predict` offline)
logs each region's IL as DagBuildGraph creates its nodes and builds the DAG, the latencies, units, heights and
static priorities from that IL and OpSchedInfoK8 (`predict_dag`), then schedules it (`simulate`) and compares
everything with the trace.

**Probes** (hooks 4..8 of `c2sched17.hooks(il=True)`, no table change): DagBuildGraph entry (first / tail tuple;
head / tail merging also run it, so a region keeps the graph built last before SchSchedGraph on its thread); a
probe at 0x100e2bfa, once per tuple node as created: opcode, the +0xc dword (kind, flags, condition code), type
word, lexical index, (kind, +0x1c) and (+0x24, +0x28) of every source / destination leaf, the first 11 words of
the SYM of the first three sources and two destinations, and the tuple as an IL line (`!`, with
`C2TRACE_ILPAS=28`: memory leaves print their points-at set `@pas`); a probe at 0x100e3918 (a register-set
destination as DagBuildGraph walks it: the sparse bit vector's chunks); `ItfPasInterf` and `ItfIsMemberOfPas`
entry / exit (the alias facts, below). With `--il` the issue probe logs all successor edges (90, not 24) and their
types. Two layout facts found on the way **[C]**: an ICONST leaf's value is at +0x28 (the plain dump prints +0x24,
which is 0 on x64: `add r10, 16` prints `0:1008`); a code-address leaf's +0x24 is the label SYM, the LABEL tuple's
+0x2c the same SYM.

**DagBuildGraph, as now read** (0x100e29e0 in full for the paths zlib reaches; `DagMakeControlPoint`,
`DagChkSymDefs/Refs`, `DagChkMemrefDefs`, `DagMakeAntiOutputDeps`, `SchmdPseudoCCDef`, `ItfSymsTotalInterf`,
`ItfPartialInterf`'s dispatch, `ItfPasInterf`, `SchmdAgiStall`, `SchmdLatencyCallbacks`, `SchmdFindCBranchDep`,
`SchComputeStaticPriority`, `SchmdDepIsBreakable`, `SchmdFindAGI` / `SchmdFindIndir`, the head of
`SchmdBreakDep`) **[C]**. Four lists, newest first, all cleared at a control point: register / variable
definitions (TLS+0xda60) and uses (0xda58), memory definitions (0xda70) and references (0xda68). Per tuple, in
order:

1. Control point (tuple kinds 0xf, 0x11 branch, 0x13 switch, 0x14 unless opcode 0x58f, 0x17..0x1a label, not
   opcode 0x577; kind 0x15; kind 0x16 unless an opcode-0x59e non-annotation): a 0x80000 edge from every node
   since the last control point that has no successor yet, a label after a label also from that one; the lists
   are cleared. A call (0x18; also 0x3eb, 0x4f0, 0x4f2, 0x4f3) becomes a control point *after* its operand edges,
   before its own definitions enter the lists.
2. Sources. A REG leaf's physical register (+0x28; none: skipped) against the definitions: same family (+0xc of
   the register SYM) and overlapping (+0x28 offset, +0x24 size; table below) gives a **true** edge, latency
   `max(lat, producer latency + AGI)` (0 from OPPAREN 0x517); the walk stops at a definition whose range holds
   the use (ItfSymsTotalInterf). SchmdAgiStall: +1 when the consumer's first memory leaf (sources, then a memory
   destination; kind 5 counts) has that exact register as index or base, or the register is rsp, the consumer a
   push / pop and the producer not. A variable leaf (kind 2) whose SYM is a variable / temporary (kind 3..5) and
   not address-taken (SYM +9 bit 1) is looked up by SYM in the same lists (`DagChkSymDefs`: same root, overlapping
   offset / size; type 0x20, **latency 1**, stops at a covering definition); other variable leaves and memory
   leaves (kind 6, and kind 11, a call's memory set) are checked against every memory definition (0x20, no stop),
   unless the memory leaf's PAS is 2.
3. Destinations. A register: **output** edges (type 4, latency max(lat, 0) on K8) from overlapping definitions,
   stopping at a covering one except for the flags (family cc: never stops); **anti** edges (type 2) from every
   overlapping use since the control point. An unnamed flags clobber (the leaf's own symbol is cc) gets no output
   edges when a later instruction of the region writes the flags before anything reads them, with no branch or
   non-instruction between (0x100e3624 = SchmdPseudoCCDef). A variable: 0x80 from its definitions, 0x40 from its
   uses. Memory: 0x80 from interfering memory definitions (`DagChkMemrefDefs`), 0x40 from interfering
   references. A register set (kind 10: a call's clobbers) defines every register of its bit vector: for an
   extern call rcx rdx r8..r11 ymm0..15 (rax is the result operand), for a static callee in the same file only
   what it clobbers (so an argument register can have no output edge into the call).
4. Zero idioms (`xor` / `xorps` / `xorpd` / `vxorps` / `vxorpd` of one register with itself, `or r, -1`): only
   the destinations' output and anti edges (`DagMakeAntiOutputDeps`, the same pseudo-flags rule).
5. The tuple enters the lists: its register uses / definitions, a variable leaf's frame register as a use
   (`get_local_base_reg`) and the variable itself (or, address-taken, the memory lists), its memory references /
   definitions. A node without a predecessor gets a 0x80000 edge from the last control point. The exit node is a
   control point. A non-instruction tuple's node latency is 0.

Register SYMs (read off the live table [TLS+0x113f4] + 0x64 * number, `reginfo`) **[C]**: rax..r15 their own
family, size 64; eax.. / ax.. / al.. in it at offset 0, sizes 32 / 16 / 8; ah..bh at offset 8; ymmN 256 with ymmNh
(128 at 128), xmmN / dxmmN / ixmmN (128), their h (64 at 64), l and s (64 / 32 at 0), fxmmNl / s, all in ymmN; the
flags cc (5) with cc_sozp 4, cc_soz 3, cc_so 2, cc_sf 1 at 0, cc_of at 1, cc_pf at 2, cc_zc 2 at 3, cc_zf at 3,
cc_cf at 4.

**Memory interference** (`ItfPartialInterf` 0x100ddb70) **[C]**: two memory leaves by `ItfPasInterf(pas, pas)`
(PAS 2 meets nothing, 3 only 3, equal sets or set 1 meet, other pairs when their bit vectors [TLS+0xebf4]
intersect, refined for derived sets >= [TLS+0xec48]); a memory set (kind 11) by its PAS at +0x24 (flagged +0x1c
bit 8: only with another flagged one); a variable against a set by `ItfIsMemberOfPas(sym, pas)`; two variables
by root and range. A volatile memory definition (+0x1c bit 13) meets everything. The bit vectors are the
optimizer's alias sets and are not in the IL dump: the probe logs the answers the scheduler's own DagBuildGraph
asked (`alias` per region: (pas, pas, answer), (sym, pas, answer)); without them (`--predict`'s "IL only" line,
`predict_dag(region, alias=False)`) a guess fitted on zlib: set 6 meets only itself, 7 (the call / escaping set),
10, 13, 15 meet every set but 6; two per-access sets (>= 16) meet unless both leaves are based on the same
variable without an index; a variable is in 7, 10, 13, 15.

**Breakable edges** (`SchmdDepIsBreakable`, called per successor edge in the forward pass before the node's
priority) **[C for the cases implemented]**: neither end a control point.

- True edge, producer `add r, imm` (kind 1), `sub r, imm` (3, not rsp), `inc` (0xb), `dec` (0xc, not rsp), `lea`
  (5: the consumer's leaf has no index; 7: the register is its base, the lea's address without index or x1), the
  consumer reaching memory through exactly that register (SchmdFindAGI: the producer's first register
  destination; the consumer's first memory leaf, sources before destinations, by base or index; not when the
  consumer reads the flags or names the register as its first source): breakable unless the displacement is short
  and would become long (|d| < 0x80, |d + delta| >= 0x80). The consumer then gets the producer's true and control
  predecessors (true ones with the broken edge's latency). If the edge is also anti / output: only for a lea whose
  address does not use its own destination, split into a true edge of kind + 1 (6, 8: folded at issue time by
  SchmdHasFoldableAGI) and a new plain edge with the other bits.
- Anti edge (no output bit), consumer `add r, imm` (0x10), `sub` (0x11), `inc` (0x15), `dec` (0x16), `lea` (0x13,
  displacement - delta within int32) moving the register the producer addresses memory through; not rsp; needs
  favor-speed.

Each marks the edge's kind (latency word bits 16..20) and counts it in the consumer's `nbreak`; a node whose
predecessors are all breakable gets `DagMakeBackwardControlDep` (0x80000 from the nearest earlier control point).
In the replay (`simulate(..., breaks=True)`): a node is ready when `npred == nbreak`; it waits while a producer of
one of its anti breaks (kind >= 0x10) is unissued, ready and could issue by the next cycle; when it issues first
its edges from unissued producers are removed (SchmdBreakDep), and a producer left without successors gets a
control edge to the next control point (`DagMakeForwardControlDep`, called from SchmdBreakDep 0x100e6094).

**Latencies** (`k8_latency`): OpSchedInfoK8 +0 as in 2.4, the callback cases of `SchmdLatencyCallbacks` read for
lea, imul / mul, div / idiv, the 0x13d..0x157 table and the default (mov, _emov, movzx: 4 load, 3 store, 1
register; memory = a leaf of kind 6, 3, 4 or 2, so a call or a jump to a code address counts), + 1.

**Measured** (zlib 1.2.1, 12 files at /O2, 1173 regions, 8450 nodes; tests/data/ncol's four sources in brackets,
72 regions):

| | IL + alias facts | IL only (PAS guess) |
|---|---|---|
| traced edges reproduced (type and latency equal) | 20028 of 20043 [2507 of 2511] | 19876 of 20043 [2488 of 2511] |
| edges predicted | 20028 [2507] | 20015 [2686] |
| DAG exact (traced edge set) | 1158 [68] | 1073 [64] |
| node latency, unit | 8450 / 8450 of 8450 [769 / 769] | the same |
| height, static priority | 8450 / 8450 [769 / 769] | 8367 / 8269 [732 / 695] |
| regions in exactly the traced issue order | **1155** [72] | 1124 [70] |

The same replay from the traced DAG: 1152 [71] (as in 6), 1153 [71] with `breaks=True`. The 15 traced edges not
predicted are all control edges SchmdBreakDep adds during scheduling (above), not edges of the built DAG; a traced
breakable edge the traced order broke is not in the trace and is not counted against the prediction. Still read
from the trace with `--predict`: the alias facts (the IL-only line shows their weight); every other input of the
DAG, its latencies, units, heights and priorities comes from the IL and the DLL's tables.

Not done: break kinds 9 (a lea folded into an index; not taken in zlib's K8 traces; on Atom: section 7.3) and the lea split's
issue-time folding (SchmdHasFoldableAGI); `SchmdBreakDep`'s operand rewrites and producer removal (bit 5: never set
by the cases above); a switch's case edges and opcode 0x51b / the CurFunc+0x58 register / int, cli, sti control
points (not reached by zlib); `ItfStackLocIsMemberOfPas` (TLS+0xebdc never seen set). Offline test:
`sched17_predict_tests` (tests/data/sched17/il/*.json: c1, c2, sum, adler32, compress, uncompr, zutil, inftrees).

---------------------------------------------------------------------------------------------------

## 7. The Atom model (`/favor:ATOM`, 2026-11-21)

**Selection** **[C, traced]**: with `/favor:ATOM` on the cl command line, `MOD+0x398` reads 0xa040 at `Schedule`'s
entry (0xa000 without it), so bit 6 is set and `SchmdInitialize` picks model 1; every traced dynamic priority is
static + 2^25 or static, never + 8, and no cycle issues more than two nodes. `c2sched17.py`'s function probe logs
`[[TLS+0x850]+0x398]` (`t850>0>0x398`) and the replay picks the model from it per function; K8 stays the default
when the field is missing (older exports). The flag also changes earlier phases (`PpSsaALUToLea`,
`PpSsaMovIndir`, `PpSsaIsAGUOperationCandidate`, `AddrModes`, `LgoEvaluateZero`, `MscAvoidStoreForwardStalls`,
`SzoWidenImmediates`, `MdmCanReplaceRegWithImmed`, ...: `xref.py 398`), so the IL the scheduler sees is not the K8
IL; those were not read.

Every model test in the scheduler (`xref.py 398` / `11c48`) **[C]**:

| where | K8 | Atom |
|---|---|---|
| `SchmdInitialize` | issue rate 3, 5 units, `SchmdGetK8FuncUnit`, OpSchedInfoK8 | issue rate 2, 3 units with caps 1, 5, 1, `SchAtomGetFuncUnit` 0x10226000, OpSchedInfoAtom 0x10023ac0 (2.5) |
| `SchmdComputeDynamicPriority` 0x100e5c03 | 1.7 | `SchAtomPairing(readyHead, cycle)` (cold 0x102df5f8), then return (7.2) |
| `SchmdShouldDelayForMacroFusion` 0x100e5a4b | 1.8 | 0 (no hold) |
| `SchmdTupRequiresSeparaterResource` 0x100e59e5 | bswap rcl rcr rol ror | 0 (so no +2^30 and no spacing) |
| `SchSchedGraph` 0x100e5586 | busy = max(busy, 0) + latency | busy = max(busy, `SchmdDagThroughput(node)`) (cold 0x102d3a92; signed compare, no clamp) |
| `SchmdInstrFuncUnitID` (masks by model index) | {2, 1, 4, 8, 0x10} on +8 | {2, 1, 4, 0, 0} on the port word +8: unit 0 integer, 1 multiply, 2 SSE |
| `SchmdDagLatency` (table TLS+0x11cb4) | OpSchedInfoK8 +0 | OpSchedInfoAtom +0, same decoding; bit 16: `SchmdLatencyCallbacks`' Atom half (cold 0x102df85b, 7.4) |
| true register edge (`DagBuildGraph` 0x100e2dc9 inline, `SchmdAdjustRegTrueDepStall` 0x10253923) | producer latency + `SchmdAgiStall` | producer latency + `SchAtomLoadToUseStall` + `SchAtomCCFlagStall` + `SchAtomAgiStall` (7.3) |
| output edge (`DagBuildGraph` 0x100e342a / 0x100e3a2c, `SchmdAdjustRegOutputDepStall`) | max(lat, 0) | max(lat, 1) |
| `SchmdDepIsBreakable` 0x100e6803 (anti edge) | consumer add / sub / inc / dec / lea | consumer lea only (cold 0x102df7b1) |
| `SchmdDepIsBreakable` 0x100e6e64 (a broken true edge's consumer inherits the producer's true predecessors) | new edge latency = the broken edge's | new edge latency 1 (cold 0x102df7a5) |
| weights W (0x10006194 + 18) | W[4] = 3 | W[4] = 25; the static terms are the same |

`SchSelectInstrs` itself has no model test: the skip rules of 1.6 step 3 hold, with `SchAtomGetFuncUnit` through
the pointer TLS+0x11c44, `d = max(busy[unit], 0)` of the node's own unit, and the macro-fusion test always false.

### 7.1 OPSCHEDINFO Atom: latency, throughput, port **[C]**

Same 12-byte rows (`optab.py atom`), read by three functions, each returning a fixed value without a tuple (entry
/ exit node: latency 0, throughput 0, port 0x30) and reading 0 for opcode 0 or >= 0x4f5. "memory" =
`SchmdHasMemoryAccesses(t, dst, src)` 0x100e4d10: a leaf of kind 6, 3, 4 or 2 among the destinations (edx) /
sources (stack argument).

- **latency** +0 (`SchmdDagLatency`): bit 16 -> callback + 1, else `s8(byte 0) + 1 + (memory ? s8(byte 1) : 0)`.
  For integer code almost every row is 0: **node latency 1** for add, and, cmp, test, xor, inc, dec, sub, mov,
  `_emov`, lea, `_jcc`, shl, rol, movsxd, with or without memory (traced: 8394 of 8394 nodes predicted); push
  0x100: 1, 2 with memory; pop 0x200: 1, 3 with memory; imul 0xf4 0x205: 6, 8 with memory.
- **throughput** +4 (`SchmdDagThroughput` 0x10253be0): bit 16 -> `SchmdThroughputCallbacks` 0x10253dc0 + 1, else
  `s8(byte 0) + 1 + (memory ? s8(byte 1) : 0)`. 1 for the integer rows above and push, pop 1 (2 with memory),
  imul 0xf4 5 (7).
- **port** +8 (`SchmdDagIssuePort` 0x10253ab0): 0 -> 0x30; bit 17 -> `SchmdPortCallbacks` 0x10253cd0; with memory
  and bits 12..15 set: `(word >> 8) & 0xf0`; else `word & 0xf0`. Values: **0x40** either port (add, and, cmp,
  test, xor, sub, inc, dec, or, mov, `_emov`: word 0x1042, so with a memory operand **0x10**), **0x10** port 0
  only (shl, rol, ror, movsxd, movzx, movsx, `_imul2/3` of 32 bits), **0x20** port 1 only (lea, `_jcc`, jmp to a
  register / address), **0x30** not pairable (call, ret, push, pop, `_epush`, div, idiv, mul, imul 0xf4 (0x31),
  shld, shrd, lock forms). That 0x10 / 0x20 are Atom's ports 0 / 1 is **[I]** (names; the code only uses them as
  a pairing relation). The low nibble is the unit mask (7.2).

Callbacks (value before the + 1 of latency / throughput; m_s / m_d = a memory source / destination; size = tuple
+0x10 & 0xfff, s0 / s1 / d0 = first / second source, first destination; "imm-like" = s1 is kind 7 or 4, or kind 3
with +8 != 0x4fc, or kind 5 with +8 0x502 / 0x503):

| opcode | latency (cold 0x102df85b) | throughput | port |
|---|---|---|---|
| call 0x18, jmp 0x103 | 0 if s0 is kind 4 or 1, else 1 | call: the same | jmp: 0x20 if s0 kind 4 / 1, else 0x30 |
| movzx 0x160, movsx 0x15b | m_s + 1 for a 16-bit tuple, else 0 | table | table (0x10) |
| `_imul2` 0xf5, `_imul3` 0xf6 | 16 bits: m_s + 5; 32: 4; else 0xd if s1 imm-like, else 0xa | 16: m_s + 4; 32: 0; else 0xc / 9 | 32 bits 0x10, else 0x30 |
| mul 0x163 | s0 of 8 bytes 0xb, else m_s + 5 | 10, else m_s + 4 | table |
| div 0x76, idiv 0xf3 | s0 size 1 / 2 / 4 / 8: 0x20 / 0x29 / 0x38 / 0xc4 | 0x1f / 0x28 / 0x37 / 0xc3 | table |
| bt 0x12 | m_s: (s0 16-bit) + (s1 register ? 8 : 0), else 0 | m_s and s1 register: 7, else 0 | register 0x20; memory: s1 register 0x30, else 0x40 |
| btc btr bts 0x13..0x15 | m_s: (16-bit) + (s1 register ? 0xa : 1), else 0 | m_d: s1 register 9 + (16-bit), else 1; else 0 | table |
| shld 0x289 / shrd 0x28b | 32 bits: (m_d ? 2 : 0) + 1; else 0xa / 8 | 32 bits: m_d ? 2 : 0; else 9 / 7 | table |
| rcl 0x200 | by 1: 0, else 0xd | by 1: 0, else 0xc | by 1: 0x10, else 0x30 |
| rcr 0x203 | by 1: m_d ? 6 : 4, else 0xb | by 1: m_d ? 5 : 3, else 10 | table |
| cvtsi2sd 0x66 / cvtsi2ss 0x67 | m_s: 4 / 6; else (s0 32-bit) / (s0 not 32-bit) | table | table |
| cvtss2si 0x69, cvttss2si 0x6f | (d0 not 32-bit) + 7 + m_s | (d0 not 32-bit) + 6 + m_s | table |
| movd 0x13e | 2 when d0 is an integer register, else 0 | table | table |
| movdqu 0x143, movupd 0x15e / movups 0x15f | m_s 2, else m_d 1, else 0 / m_d 1, else m_s 2, else 0 | table | table |
| psll* / psr* 0x1e2..0x1eb | 0 if s1 imm-like, else m_s + 1 | psrad / psraw: 0 if imm-like, else m_s | 0x10 if imm-like, else 0x30 |

### 7.2 Pairing, units, busy **[C]**

`SchAtomCanBePaired(a, b)` 0x10225fb0 on the two ports: a 0x10 pairs with 0x20 / 0x40, a 0x20 with 0x10 / 0x40, a
0x30 with nothing, anything else (0x40) with 0x10 / 0x20 / 0x40.

`SchAtomPairing(readyHead, cycle)` 0x10226160 (returns at once when preSched, fewer than two ready nodes or W[4]
< 0), over the ready list in its current (sorted) order:

    A = first ready node with a tuple and est <= cycle;  if none or port(A) == 0x30: return
    B = none
    for X after A with a tuple, est <= cycle and CanBePaired(A, X):
        if port(A) == 0x40 and port(X) == 0x20: B = X unless B is already a 0x20; continue
        if port(X) is the exact complement (A 0x40 -> X 0x10, A 0x10 -> X 0x20, else X 0x10): B = X; break
        if no B yet and port(X) == 0x40: B = X
    if no B: B = first successor S of A (edge order) with S.npred == 1, edge latency 0, a tuple, est <= cycle and
             CanBePaired(A, S)
    if B: A.dyn = A.static + 2^25;  B.dyn = B.static + 2^25          // SchHeurVal(1, W[4]), W[4] = 25

Differences from K8's 1.7: **one** pair per cycle (only the first issuable node looks for a partner), the bonus
2^25 outranks everything in the static priority including B (2^23), and **no reset**: dyn is assigned only here,
so a node keeps static + 2^25 until it issues, also when it was paired as a not yet ready successor (it then enters
the ready list with the bonus). The re-sort of 1.6 step 2 follows as on K8.

`SchAtomGetFuncUnit(node, issueArray, n)` 0x10226000, u = node+0x3e, avail(k) = busy[k] < cap[k] (caps 1, 5, 1;
`SchmdFuncUnitAvailable` 0x10253ca0):

- -1 unless avail(u);
- u = 0 (integer): -1 when busy[1] > 0 (a multiply in flight), and -1 when !avail(2) and the node's latency byte >
  1; u = 1: needs avail(0) and avail(2); u = 2: needs avail(0) and avail(1);
- slot n > 0: -1 unless `SchAtomCanBePaired(issueArray[0], node)`;
- else u (the node keeps its own unit).

Busy (cold 0x102d3a92): after the cycle, for each issued node with 0 < opcode < 0x4f5, `busy[u] = max(busy[u],
throughput)`; then every busy count drops by 1. With throughput 1 an integer node frees its unit for the next
cycle, so the cap of 1 does not stop two integer nodes in one cycle (busy is charged after the cycle); a multiply
(throughput 5, unit 1, cap 5 never reached) blocks integer issue for 4 cycles by the busy[1] > 0 rule.

### 7.3 True-edge latency on Atom **[C]**

`lat = max(lat, producer.latency + LoadToUse + CCFlag + Agi)` (0 from OPPAREN 0x517), with the edge's register
symbol r:

- `SchAtomAgiStall` 0x10225d80: **3** when the consumer reaches memory through r (any memory leaf of kind 5 / 6
  among its sources and destinations, r as base (a REG leaf) or index), or reads r implicitly: cvtsi2sd, cvtsi2ss,
  movd, movq, pinsrw (an integer register: SYM byte +8 == 1, type class 1..4), maskmovdqu, maskmovq, stos (rdi),
  movs (rsi, rdi). **0** when the producer is a lea (its result reaches the address unit directly **[I]**), and for
  rsp when the producer updates rsp implicitly (`SchAtomHasImplicitSPUpdate` 0x102260a0: call, `_epop`, `_epush`,
  `_eret`, leave, pop, push, pushf, ret, fatret, retf). Without an address use: 3 only for rsp into an implicit
  rsp user from a producer that is neither an implicit user nor a lea.
- `SchAtomLoadToUseStall` 0x102260f0: **-1** when the producer is a 32-bit `mov` (opcode 0x13b, tuple size 4) whose
  first source is memory (kind 5, 6 or 2) and the Agi stall is 0 (a test of a consumer tuple of kind 2, never an
  instruction, was not followed).
- `SchAtomCCFlagStall` 0x10225f70: **+1** when r is in the flags family (cc, SYM 353) and the consumer is not a
  branch (tuple kind 0x11): setcc, cmovcc, adc, sbb.

So on Atom the typical true edges are 1 (ALU -> ALU, lea -> address), 4 (ALU or load -> address), 2 (flags ->
cmov / setcc), 0 (32-bit load -> ALU), and heights grow by 2 per ALU link. Traced true edges on zlib (5756 between
issued nodes): latency 1: 4238, 4: 981, 0: 249, 5: 147, 2: 106, 6: 23, others 12.

Breakable edges on Atom (`SchmdDepIsBreakable` as in 6.1, with the two model tests of the table) **[C]**, two cases
the K8 replay did not need: **kind 9** (0x100e6db1), the register is the consumer's index and the lea's address
is base + disp, or base + index with the two scales adding up to <= 3 (gzerror, inflate_fast: the consumer goes
first); an M of the form [b + i*1] goes the kind-5 way (0x102df770), which the replay does not distinguish; and
**kind 0x14** (0x100e6e76) for an anti edge whose register is the producer's index (0x13: its base). When a true
edge is marked, the consumer inherits the producer's true predecessors with the broken edge's latency, and on
Atom the producer's own predecessor edge is then set to 1 (cold 0x102df7a5 writes the loop's edge, not the new
one: compress_block, a movsxd -> lea index edge traced at 1 instead of 4). Kind 9 is enabled for the Atom
model only in the tool (the code is shared; on K8 it was never taken in the traces and is left out so the K8
numbers stay as measured).

### 7.4 Replay and prediction (`c2sched17.py`, model "atom")

`simulate(region, model="atom")`: issue rate 2, `SchAtomPairing` at the start of each cycle (dyn never reset), the
unit test of `SchAtomGetFuncUnit` including the pairing test for slot 1, no macro-fusion hold, busy = max(busy,
throughput) for real opcodes; ports and throughputs from OpSchedInfoAtom and the callbacks (`atom_port`,
`atom_throughput`, `atom_latency`, from a tuple view: the issue probe now also logs the tuple's type word and the
type / value words of its first four sources and two destinations, fields 17..20). `predict_dag(region,
model="atom")`: the Atom latencies, units, true / output edge latencies and breakable cases of the table above;
the rest of 6.1 unchanged. Exports of Atom traces carry `model`, `port` and `thr`, so the offline test needs no DLL.

**Measured** (zlib 1.2.1, 12 files at /O2 /favor:ATOM /I zlib, 1162 regions, 8394 nodes; tests/data/ncol's four
sources in brackets, 72 regions, 762 nodes):

| | |
|---|---|
| replay from the traced DAG (`--check`), regions in exactly the traced order | **1151 of 1162** [72 of 72] |
| the same with breakable edges (`simulate(..., breaks=True)`) | **1162 of 1162** [72] |
| K8 rules (`simulate(r)`) on the same Atom traces | 662 of 1162 |
| predicted DAG (`--predict`, IL + alias facts): traced edges reproduced, type and latency equal | 20014 of 20028 [2482 of 2485], 20014 predicted [2482] |
| DAG exact | 1148 [69] |
| node latency, unit, height, static priority | 8394 / 8394 / 8394 / 8394 of 8394 [762 each] |
| regions in exactly the traced order with the predicted DAG | **1161 of 1162** [72] |
| IL only (PAS guess): edges, static priority, issue order | 19907, 8194, 1124 [2479, 692, 70] |

The 11 regions the plain replay misses (crc32 3, gzio 3, inffast 4, inftrees 1) all need the breakable edges (a
consumer issued before its lea / add / inc producer). The 14 traced edges not predicted are, as on K8, control
edges SchmdBreakDep adds while scheduling. The one region off with the predicted DAG is gen_codes region 1: a lea
`rax <- [r8 + r10*2]` feeding `add r9w, [rax*1+0]` is predicted kind 9 but the trace has no break (a check not
found; that an index-only address takes another path is the guess). Also open there: gen_bitlen region 18, a lea
split (kind 6) the trace does not mark (no effect on the order). The Atom regions differ from the K8 ones (1162 vs
1173) because the earlier `0x398 & 0x40` tests change the IL.

Offline test: `sched17_atom_tests` (tests/data/sched17/atom/*.json: `--il --export` of c1, c2, sum, adler32,
compress, uncompr, zutil, inftrees, inffast under /O2 /favor:ATOM: replay 127 / 132 (132 with breaks, 81 with K8
rules), predicted DAG 1836 of 1840 edges, 877 of 877 nodes, order 132 of 132).

Not read on the Atom path: `SchmdBreakDep`'s per-kind rewrites (as on K8), the 0x2c / +8 fields of a memory leaf
the break and callback tests read (+8 = 0x4fe / 0x501 / 0x502 / 0x503 look like address-form codes **[I]**), and
the earlier-phase Atom tests listed at the top.
