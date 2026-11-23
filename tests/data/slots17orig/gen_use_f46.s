00000000 mov qword ptr [rsp + 8], rbx
00000005 mov qword ptr [rsp + 0x20], rsi
0000000a push rbp
0000000b push rdi
0000000c push r14
0000000e mov rbp, rsp
00000011 sub rsp, 0x80
00000018 mov rax, qword ptr [rip]
0000001f xor rax, rsp
00000022 mov qword ptr [rbp - 8], rax
00000026 mov ebx, ecx
00000028 lea rcx, [rbp - 0x60]
0000002c mov edi, r8d
0000002f mov esi, edx
00000031 call 0x36
00000036 mov eax, dword ptr [rip]
0000003c mov edx, dword ptr [rbp - 0x60]
0000003f lea rcx, [rbp - 0x58]
00000043 lea edx, [rax + rdx*2]
00000046 mov dword ptr [rip], edx
0000004c call 0x51
00000051 xor r14d, r14d
00000054 movsd xmm1, qword ptr [rip]
0000005c addsd xmm1, qword ptr [rbp - 0x58]
00000061 mov dword ptr [rip], r14d
00000068 addsd xmm1, qword ptr [rbp - 0x58]
0000006d movsd qword ptr [rip], xmm1
00000075 test ebx, ebx
00000077 jle 0xbc
00000079 npad 
00000080 lea rcx, [rbp - 0x60]
00000084 call 0x89
00000089 movsx ecx, word ptr [rbp - 0x60]
0000008d mov eax, ecx
0000008f add eax, dword ptr [rip]
00000095 lea eax, [rax + rcx*2]
00000098 lea rcx, [rbp - 0x58]
0000009c mov dword ptr [rip], eax
000000a2 call 0xa7
000000a7 mov edx, dword ptr [rbp - 0x58]
000000aa inc edx
000000ac add edx, dword ptr [rip]
000000b2 mov dword ptr [rip], edx
000000b8 cmp edx, ebx
000000ba jl 0x80
000000bc mov dword ptr [rip], r14d
000000c3 test ebx, ebx
000000c5 jle 0xfd
000000c7 lea rcx, [rbp - 0x30]
000000cb call 0xd0
000000d0 mov eax, dword ptr [rbp - 0x30]
000000d3 lea rcx, [rbp - 0x30]
000000d7 add dword ptr [rip], eax
000000dd call 0xe2
000000e2 lea rcx, [rbp - 0x60]
000000e6 call 0xeb
000000eb mov eax, dword ptr [rip]
000000f1 inc eax
000000f3 mov dword ptr [rip], eax
000000f9 cmp eax, ebx
000000fb jl 0xc7
000000fd mov ecx, esi
000000ff call 0x104
00000104 lea rcx, [rbp - 0x60]
00000108 add dword ptr [rip], eax
0000010e call 0x113
00000113 lea rcx, [rbp - 0x58]
00000117 call 0x11c
0000011c mov ecx, dword ptr [rbp - 0x58]
0000011f mov eax, ecx
00000121 add eax, dword ptr [rip]
00000127 lea eax, [rax + rcx*2]
0000012a lea rcx, [rbp - 0x40]
0000012e mov dword ptr [rip], eax
00000134 call 0x139
00000139 mov ecx, dword ptr [rbp - 0x40]
0000013c add ecx, dword ptr [rbp - 0x38]
0000013f add dword ptr [rip], ecx
00000145 lea rcx, [rbp - 0x40]
00000149 call 0x14e
0000014e lea rcx, [rbp - 0x50]
00000152 call 0x157
00000157 lea rcx, [rbp - 0x5c]
0000015b call 0x160
00000160 movsx ecx, byte ptr [rbp - 0x5c]
00000164 mov eax, dword ptr [rip]
0000016a lea ecx, [rax + rcx*2]
0000016d mov dword ptr [rip], ecx
00000173 lea rcx, [rbp - 0x60]
00000177 call 0x17c
0000017c movsx ecx, word ptr [rbp - 0x60]
00000180 mov eax, ecx
00000182 add eax, dword ptr [rip]
00000188 lea eax, [rax + rcx*2]
0000018b lea rcx, [rbp - 0x60]
0000018f mov dword ptr [rip], eax
00000195 call 0x19a
0000019a mov dword ptr [rip], r14d
000001a1 test ebx, ebx
000001a3 jle 0x1de
000001a5 lea rcx, [rbp - 0x30]
000001a9 call 0x1ae
000001ae lea rcx, [rbp - 0x30]
000001b2 call 0x1b7
000001b7 lea rcx, [rbp - 0x58]
000001bb call 0x1c0
000001c0 mov ecx, dword ptr [rbp - 0x58]
000001c3 mov eax, ecx
000001c5 lea ecx, [rcx*2 + 1]
000001cc add eax, dword ptr [rip]
000001d2 add ecx, eax
000001d4 mov dword ptr [rip], ecx
000001da cmp ecx, ebx
000001dc jl 0x1a5
000001de mov dword ptr [rip], r14d
000001e5 test ebx, ebx
000001e7 jle 0x20e
000001e9 npad 
000001f0 lea rcx, [rbp - 0x30]
000001f4 call 0x1f9
000001f9 mov edx, dword ptr [rbp - 0x2c]
000001fc inc edx
000001fe add edx, dword ptr [rip]
00000204 mov dword ptr [rip], edx
0000020a cmp edx, ebx
0000020c jl 0x1f0
0000020e lea rcx, [rbp - 0x48]
00000212 call 0x217
00000217 mov ecx, edi
00000219 movsd xmm0, qword ptr [rbp - 0x48]
0000021e movsd xmm1, qword ptr [rip]
00000226 addsd xmm1, xmm0
0000022a addsd xmm1, xmm0
0000022e addsd xmm1, xmm0
00000232 movsd qword ptr [rip], xmm1
0000023a call 0x23f
0000023f mov ecx, dword ptr [rip]
00000245 add ecx, eax
00000247 mov dword ptr [rip], ecx
0000024d mov eax, ecx
0000024f mov rcx, qword ptr [rbp - 8]
00000253 xor rcx, rsp
00000256 call 0x25b
0000025b lea r11, [rsp + 0x80]
00000263 mov rbx, qword ptr [r11 + 0x20]
00000267 mov rsi, qword ptr [r11 + 0x38]
0000026b mov rsp, r11
0000026e pop r14
00000270 pop rdi
00000271 pop rbp
00000272 ret 
