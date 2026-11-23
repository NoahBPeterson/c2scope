/* A small bytecode interpreter: a switch-dispatched loop with many values live across it. */
typedef unsigned char u8;
typedef struct Vm { int *stack; int sp; const u8 *code; int pc; int acc; int regs[8]; int steps; int err; } Vm;
extern int vm_call(Vm *vm, int fn, int argc);
extern void vm_trace(int pc, int op, int acc);
extern int vm_load(int addr);
extern void vm_store(int addr, int v);

int vm_run(Vm *vm, int limit, int trace)
{
    const u8 *code = vm->code;
    int *stack = vm->stack;
    int sp = vm->sp, pc = vm->pc, acc = vm->acc;
    int r0 = vm->regs[0], r1 = vm->regs[1], r2 = vm->regs[2], r3 = vm->regs[3];
    int steps = 0, a, b;
    while (steps < limit) {
        int op = code[pc++];
        if (trace)
            vm_trace(pc, op, acc);
        steps++;
        switch (op) {
        case 0: goto done;
        case 1: acc = code[pc] | (code[pc + 1] << 8); pc += 2; break;
        case 2: stack[sp++] = acc; break;
        case 3: acc = stack[--sp]; break;
        case 4: a = stack[--sp]; acc = a + acc; break;
        case 5: a = stack[--sp]; acc = a - acc; break;
        case 6: a = stack[--sp]; acc = a * acc; break;
        case 7: a = stack[--sp]; if (acc == 0) { vm->err = 1; goto done; } acc = a / acc; break;
        case 8: r0 = acc; break;
        case 9: r1 = acc; break;
        case 10: r2 = acc; break;
        case 11: r3 = acc; break;
        case 12: acc = r0; break;
        case 13: acc = r1; break;
        case 14: acc = r2; break;
        case 15: acc = r3; break;
        case 16: b = (signed char)code[pc++]; if (acc) pc += b; break;
        case 17: b = (signed char)code[pc++]; if (!acc) pc += b; break;
        case 18: b = (signed char)code[pc++]; pc += b; break;
        case 19: a = code[pc++]; acc = vm_call(vm, a, code[pc++]); break;
        case 20: acc = vm_load(acc + r0); break;
        case 21: vm_store(r1 + r2, acc); break;
        case 22: acc = acc << (code[pc++] & 31); break;
        case 23: acc = (int)((unsigned)acc >> (code[pc++] & 31)); break;
        case 24: a = stack[sp - 1]; stack[sp - 1] = stack[sp - 2]; stack[sp - 2] = a; break;
        case 25: acc = acc < stack[--sp]; break;
        case 26: r0 += r1; r1 += r2; r2 += r3; break;
        default: vm->err = 2; goto done;
        }
    }
done:
    vm->sp = sp; vm->pc = pc; vm->acc = acc; vm->steps += steps;
    vm->regs[0] = r0; vm->regs[1] = r1; vm->regs[2] = r2; vm->regs[3] = r3;
    return vm->err;
}

int vm_checksum(const u8 *code, int n, int seed)
{
    int h = seed, i, k = 0, run = 0, last = -1;
    for (i = 0; i < n; i++) {
        int c = code[i];
        if (c == last) { run++; continue; }
        if (run > 2) h = h * 31 + run;
        run = 0;
        last = c;
        switch (c & 7) {
        case 0: h ^= c; break;
        case 1: h += c << 3; break;
        case 2: h -= c; k++; break;
        case 3: h = (h << 5) | ((unsigned)h >> 27); break;
        case 4: k += h & 15; break;
        default: h += k; break;
        }
    }
    return h + k + run;
}
