00000000 push rbp
00000002 push rsi
00000003 push r12
00000005 push r14
00000007 push r15
00000009 lea rbp, [rsp - 0x27]
0000000e sub rsp, 0xd0
00000015 mov rax, qword ptr [rip]
0000001c xor rax, rsp
0000001f mov qword ptr [rbp + 7], rax
00000023 mov r14, r9
00000026 mov qword ptr [rbp - 0x59], r9
0000002a mov r9, qword ptr [rbp + 0x7f]
0000002e mov r11d, r8d
00000031 mov r8, qword ptr [rbp + 0x77]
00000035 xor r12d, r12d
00000038 mov qword ptr [rbp - 0x51], r9
0000003c mov r15, rdx
0000003f mov qword ptr [rbp - 0x41], rdx
00000043 mov qword ptr [rbp - 0x49], r8
00000047 mov esi, ecx
00000049 mov dword ptr [rbp - 0x71], ecx
0000004c mov r10d, r12d
0000004f npad 
00000050 mov eax, r10d
00000053 add rax, rax
00000056 cmp rax, 0x20
0000005a jae 0x52b
00000060 inc r10d
00000063 mov word ptr [rbp + rax - 0x39], r12w
00000069 cmp r10d, 0xf
0000006d jbe 0x50
0000006f mov qword ptr [rsp + 0x100], rbx
00000077 mov qword ptr [rsp + 0xc8], rdi
0000007f mov qword ptr [rsp + 0xc0], r13
00000087 test r11d, r11d
0000008a je 0xb1
0000008c mov rcx, rdx
0000008f mov rdx, r11
00000092 npad 
000000a0 movzx eax, word ptr [rcx]
000000a3 lea rcx, [rcx + 2]
000000a7 inc word ptr [rbp + rax*2 - 0x39]
000000ac dec rdx
000000af jne 0xa0
000000b1 mov r13d, dword ptr [r8]
000000b4 or edi, 0xffffffff
000000b7 mov ebx, 0xf
000000bc mov dword ptr [rsp + 0x24], edi
000000c0 mov eax, ebx
000000c2 cmp word ptr [rbp + rax*2 - 0x39], r12w
000000c8 jne 0xd1
000000ca add ebx, edi
000000cc cmp ebx, 1
000000cf jae 0xc0
000000d1 cmp r13d, ebx
000000d4 cmova r13d, ebx
000000d8 test ebx, ebx
000000da je 0x523
000000e0 mov edx, 3
000000e5 mov r10d, 1
000000eb npad 
000000f0 mov eax, r10d
000000f3 cmp word ptr [rbp + rax*2 - 0x39], r12w
000000f9 jne 0x149
000000fb lea eax, [rdx - 1]
000000fe cmp word ptr [rbp + rax*2 - 0x39], r12w
00000104 jne 0x146
00000106 mov eax, edx
00000108 cmp word ptr [rbp + rax*2 - 0x39], r12w
0000010e jne 0x140
00000110 lea eax, [rdx + 1]
00000113 cmp word ptr [rbp + rax*2 - 0x39], r12w
00000119 jne 0x13a
0000011b lea eax, [rdx + 2]
0000011e cmp word ptr [rbp + rax*2 - 0x39], r12w
00000124 jne 0x134
00000126 add edx, 5
00000129 add r10d, 5
0000012d cmp edx, 0x11
00000130 jbe 0xf0
00000132 jmp 0x149
00000134 add r10d, 4
00000138 jmp 0x149
0000013a add r10d, 3
0000013e jmp 0x149
00000140 add r10d, 2
00000144 jmp 0x149
00000146 inc r10d
00000149 cmp r13d, r10d
0000014c mov edx, 1
00000151 cmovb r13d, r10d
00000155 mov r8d, edx
00000158 npad 
00000160 mov eax, edx
00000162 add r8d, r8d
00000165 movzx ecx, word ptr [rbp + rax*2 - 0x39]
0000016a sub r8d, ecx
0000016d js 0x523
00000173 inc edx
00000175 cmp edx, 0xf
00000178 jbe 0x160
0000017a test r8d, r8d
0000017d jle 0x199
0000017f test esi, esi
00000181 je 0x523
00000187 movzx eax, word ptr [rbp - 0x39]
0000018b mov ecx, r11d
0000018e sub ecx, eax
00000190 cmp ecx, 1
00000193 jne 0x523
00000199 mov word ptr [rbp - 0x17], r12w
0000019e mov rcx, r12
000001a1 mov edx, 0xe
000001a6 npad 
000001b0 movzx eax, word ptr [rbp + rcx - 0x37]
000001b5 lea rcx, [rcx + 2]
000001b9 add ax, word ptr [rbp + rcx - 0x19]
000001be mov word ptr [rbp + rcx - 0x17], ax
000001c3 dec rdx
000001c6 jne 0x1b0
000001c8 mov r8d, r12d
000001cb test r11d, r11d
000001ce je 0x1fc
000001d0 mov rdx, r15
000001d3 movzx eax, word ptr [rdx]
000001d6 test ax, ax
000001d9 je 0x1f0
000001db movzx eax, ax
000001de movzx ecx, word ptr [rbp + rax*2 - 0x19]
000001e3 mov word ptr [r9 + rcx*2], r8w
000001e8 movzx eax, word ptr [rdx]
000001eb inc word ptr [rbp + rax*2 - 0x19]
000001f0 inc r8d
000001f3 add rdx, 2
000001f7 cmp r8d, r11d
000001fa jb 0x1d3
000001fc mov ecx, esi
000001fe test esi, esi
00000200 je 0x246
00000202 dec ecx
00000204 je 0x21e
00000206 lea r11, [rip]
0000020d lea r9, [rip]
00000214 or r12d, 0xffffffff
00000218 mov qword ptr [rbp - 0x69], r11
0000021c jmp 0x253
0000021e lea r9, [rip]
00000225 lea r11, [rip]
0000022c mov r12d, 0x100
00000232 sub r9, 0x202
00000239 sub r11, 0x202
00000240 mov qword ptr [rbp - 0x69], r11
00000244 jmp 0x253
00000246 mov r11, r9
00000249 mov qword ptr [rbp - 0x69], r9
0000024d mov r12d, 0x13
00000253 mov r15, qword ptr [r14]
00000256 xor eax, eax
00000258 mov ecx, r13d
0000025b mov r14d, 1
00000261 mov r8d, eax
00000264 mov edx, eax
00000266 shl r14d, cl
00000269 cmp dword ptr [rbp - 0x71], 1
0000026d mov dword ptr [rsp + 0x28], eax
00000271 mov esi, eax
00000273 lea eax, [r14 - 1]
00000277 mov ecx, r14d
0000027a mov dword ptr [rbp - 0x61], eax
0000027d mov qword ptr [rbp - 0x79], r9
00000281 mov dword ptr [rbp - 0x7d], ecx
00000284 jne 0x2e0
00000286 cmp ecx, 0x506
0000028c jb 0x2e0
0000028e mov eax, 1
00000293 mov rdi, qword ptr [rsp + 0xc8]
0000029b mov rbx, qword ptr [rsp + 0x100]
000002a3 mov r13, qword ptr [rsp + 0xc0]
000002ab mov rcx, qword ptr [rbp + 7]
000002af xor rcx, rsp
000002b2 call 0x2b7
000002b7 add rsp, 0xd0
000002be pop r15
000002c0 pop r14
000002c2 pop r12
000002c4 pop rsi
000002c5 pop rbp
000002c6 ret 
000002c7 npad 
000002d0 mov edi, dword ptr [rsp + 0x24]
000002d4 npad 
000002e0 mov rcx, qword ptr [rbp - 0x51]
000002e4 movzx eax, r10b
000002e8 sub al, sil
000002eb mov byte ptr [rsp + 0x21], al
000002ef mov eax, edx
000002f1 movzx ecx, word ptr [rcx + rax*2]
000002f5 cmp ecx, r12d
000002f8 jge 0x306
000002fa mov byte ptr [rsp + 0x20], 0
000002ff mov word ptr [rsp + 0x22], cx
00000304 jmp 0x327
00000306 jle 0x31b
00000308 add rcx, rcx
0000030b movzx eax, byte ptr [rcx + r11]
00000310 mov byte ptr [rsp + 0x20], al
00000314 movzx eax, word ptr [rcx + r9]
00000319 jmp 0x322
0000031b mov byte ptr [rsp + 0x20], 0x60
00000320 xor eax, eax
00000322 mov word ptr [rsp + 0x22], ax
00000327 mov eax, dword ptr [rsp + 0x20]
0000032b mov ecx, r10d
0000032e mov r11d, 1
00000334 sub ecx, esi
00000336 mov edx, r8d
00000339 mov r9d, r14d
0000033c shl r11d, cl
0000033f mov ecx, esi
00000341 shr edx, cl
00000343 add edx, r14d
00000346 npad 
00000350 sub edx, r11d
00000353 mov dword ptr [r15 + rdx*4], eax
00000357 sub r9d, r11d
0000035a jne 0x350
0000035c lea ecx, [r10 - 1]
00000360 mov edx, 1
00000365 shl edx, cl
00000367 test r8d, edx
0000036a je 0x377
0000036c npad 
00000370 shr edx, 1
00000372 test r8d, edx
00000375 jne 0x370
00000377 test edx, edx
00000379 je 0x388
0000037b lea eax, [rdx - 1]
0000037e and r8d, eax
00000381 add r8d, edx
00000384 xor ecx, ecx
00000386 jmp 0x38d
00000388 xor ecx, ecx
0000038a mov r8d, ecx
0000038d mov edx, dword ptr [rsp + 0x28]
00000391 mov eax, r10d
00000394 mov r9d, 0xffff
0000039a inc edx
0000039c add word ptr [rbp + rax*2 - 0x39], r9w
000003a2 mov dword ptr [rsp + 0x28], edx
000003a6 jne 0x3c2
000003a8 cmp r10d, ebx
000003ab je 0x491
000003b1 mov rcx, qword ptr [rbp - 0x51]
000003b5 mov rax, qword ptr [rbp - 0x41]
000003b9 movzx ecx, word ptr [rcx + rdx*2]
000003bd movzx r10d, word ptr [rax + rcx*2]
000003c2 mov r9, qword ptr [rbp - 0x79]
000003c6 mov r11, qword ptr [rbp - 0x69]
000003ca cmp r10d, r13d
000003cd jbe 0x2e0
000003d3 mov edi, dword ptr [rbp - 0x61]
000003d6 and edi, r8d
000003d9 cmp edi, dword ptr [rsp + 0x24]
000003dd je 0x2d0
000003e3 test esi, esi
000003e5 mov r11d, r10d
000003e8 mov edx, 1
000003ed cmove esi, r13d
000003f1 lea r15, [r15 + r14*4]
000003f5 sub r11d, esi
000003f8 mov ecx, r11d
000003fb lea r9d, [rsi + r11]
000003ff shl edx, cl
00000401 cmp r9d, ebx
00000404 jae 0x429
00000406 npad 
00000410 movzx ecx, word ptr [rbp + r9*2 - 0x39]
00000416 sub edx, ecx
00000418 test edx, edx
0000041a jle 0x429
0000041c inc r9d
0000041f inc r11d
00000422 add edx, edx
00000424 cmp r9d, ebx
00000427 jb 0x410
00000429 mov eax, dword ptr [rbp - 0x7d]
0000042c mov ecx, r11d
0000042f mov r14d, 1
00000435 shl r14d, cl
00000438 add eax, r14d
0000043b cmp dword ptr [rbp - 0x71], 1
0000043f mov dword ptr [rbp - 0x7d], eax
00000442 jne 0x44f
00000444 cmp eax, 0x506
00000449 jae 0x28e
0000044f mov rcx, qword ptr [rbp - 0x59]
00000453 mov r9, qword ptr [rbp - 0x79]
00000457 lea rdx, [rdi*4]
0000045f mov rax, qword ptr [rcx]
00000462 mov dword ptr [rsp + 0x24], edi
00000466 mov byte ptr [rdx + rax], r11b
0000046a mov rax, qword ptr [rcx]
0000046d mov r11, qword ptr [rbp - 0x69]
00000471 mov byte ptr [rdx + rax + 1], r13b
00000476 mov rcx, qword ptr [rcx]
00000479 mov rax, r15
0000047c sub rax, rcx
0000047f sar rax, 2
00000483 mov word ptr [rdx + rcx + 2], ax
00000488 mov edx, dword ptr [rsp + 0x28]
0000048c jmp 0x2e0
00000491 mov r14, qword ptr [rbp - 0x59]
00000495 movzx eax, r10b
00000499 mov byte ptr [rsp + 0x20], 0x40
0000049e sub al, sil
000004a1 mov word ptr [rsp + 0x22], cx
000004a6 mov byte ptr [rsp + 0x21], al
000004aa test r8d, r8d
000004ad je 0x50b
000004af npad 
000004b0 test esi, esi
000004b2 je 0x4cb
000004b4 mov eax, dword ptr [rbp - 0x61]
000004b7 and eax, r8d
000004ba cmp eax, edi
000004bc je 0x4cb
000004be mov r15, qword ptr [r14]
000004c1 mov esi, ecx
000004c3 mov r10d, r13d
000004c6 mov byte ptr [rsp + 0x21], r13b
000004cb mov eax, dword ptr [rsp + 0x20]
000004cf mov ecx, esi
000004d1 mov edx, r8d
000004d4 shr rdx, cl
000004d7 lea ecx, [r10 - 1]
000004db mov dword ptr [r15 + rdx*4], eax
000004df mov eax, 1
000004e4 shl eax, cl
000004e6 test r8d, eax
000004e9 je 0x4f7
000004eb npad 
000004f0 shr eax, 1
000004f2 test r8d, eax
000004f5 jne 0x4f0
000004f7 test eax, eax
000004f9 je 0x50b
000004fb lea ecx, [rax - 1]
000004fe and r8d, ecx
00000501 mov ecx, 0
00000506 add r8d, eax
00000509 jne 0x4b0
0000050b mov eax, dword ptr [rbp - 0x7d]
0000050e shl rax, 2
00000512 add qword ptr [r14], rax
00000515 mov rax, qword ptr [rbp - 0x49]
00000519 mov dword ptr [rax], r13d
0000051c xor eax, eax
0000051e jmp 0x293
00000523 or eax, 0xffffffff
00000526 jmp 0x293
0000052b call 0x530
00000530 int3 
