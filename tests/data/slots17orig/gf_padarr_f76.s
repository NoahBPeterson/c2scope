00000000 push r13
00000002 push rbp
00000003 push r12
00000005 push r14
00000007 push r15
00000009 sub rsp, 0x1a0
00000010 lea r13, [rsp + 0xf0]
00000018 mov qword ptr [r13 + 0xe8], rbx
0000001f mov qword ptr [r13 + 0xf0], rsi
00000026 mov qword ptr [r13 + 0xf8], rdi
0000002d lea rbp, [rsp + 0x110]
00000035 and rbp, 0xffffffffffffffe0
00000039 mov rax, qword ptr [rip]
00000040 xor rax, r13
00000043 mov qword ptr [rbp + 0x80], rax
0000004a mov dword ptr [rbp + 0x20], ecx
0000004d lea rcx, [rbp + 0x40]
00000051 call 0x56
00000056 mov eax, dword ptr [rbp + 0x48]
00000059 lea rcx, [rbp + 0x30]
0000005d add dword ptr [rip], eax
00000063 call 0x68
00000068 lea rcx, [rbp + 0x28]
0000006c call 0x71
00000071 lea rcx, [rbp + 0x24]
00000075 cvttsd2si eax, qword ptr [rbp + 0x28]
0000007a add dword ptr [rip], eax
00000080 call 0x85
00000085 lea rcx, [rbp + 0x60]
00000089 call 0x8e
0000008e mov eax, dword ptr [rbp + 0x20]
00000091 mov edx, dword ptr [rip]
00000097 add edx, dword ptr [rbp + 0x68]
0000009a lea eax, [rax*4 + 4]
000000a1 movsxd rcx, eax
000000a4 mov dword ptr [rip], edx
000000aa lea rax, [rcx + 0xf]
000000ae cmp rax, rcx
000000b1 ja 0xbd
000000b3 movabs rax, 0xffffffffffffff0
000000bd and rax, 0xfffffffffffffff0
000000c1 call 0xc6
000000c6 sub rsp, rax
000000c9 lea rcx, [rsp + 0x110]
000000d1 mov dword ptr [rcx], edx
000000d3 call 0xd8
000000d8 lea rcx, [rbp + 0x20]
000000dc call 0xe1
000000e1 mov r8d, dword ptr [rbp + 0x20]
000000e5 mov edx, dword ptr [rbp + 0x20]
000000e8 lea eax, [r8 + 0x21]
000000ec lea ecx, [r8 + 0x20]
000000f0 lea r10d, [r8 + 0x1f]
000000f4 mov dword ptr [rsp + 0x108], eax
000000fb mov dword ptr [rsp + 0x100], ecx
00000102 mov ecx, dword ptr [rbp + 0x20]
00000105 mov dword ptr [rsp + 0xf8], r10d
0000010d lea r11d, [r8 + 0x1e]
00000111 lea ebx, [r8 + 0x1d]
00000115 mov dword ptr [rsp + 0xf0], r11d
0000011d mov dword ptr [rsp + 0xe8], ebx
00000124 lea edi, [r8 + 0x1c]
00000128 mov dword ptr [rsp + 0xe0], edi
0000012f lea esi, [r8 + 0x1b]
00000133 lea r14d, [r8 + 0x1a]
00000137 mov dword ptr [rsp + 0xd8], esi
0000013e mov dword ptr [rsp + 0xd0], r14d
00000146 lea r15d, [r8 + 0x19]
0000014a mov dword ptr [rsp + 0xc8], r15d
00000152 lea r12d, [r8 + 0x18]
00000156 lea eax, [rcx + 0x17]
00000159 mov dword ptr [rsp + 0xc0], r12d
00000161 mov dword ptr [rsp + 0xb8], eax
00000168 lea eax, [rcx + 0x16]
0000016b mov dword ptr [rsp + 0xb0], eax
00000172 lea eax, [rcx + 0x15]
00000175 lea r9d, [r8 + 3]
00000179 mov dword ptr [rsp + 0xa8], eax
00000180 lea eax, [rcx + 0x14]
00000183 add r8d, 2
00000187 mov dword ptr [rsp + 0xa0], eax
0000018e lea eax, [rcx + 0x13]
00000191 inc edx
00000193 mov dword ptr [rsp + 0x98], eax
0000019a lea eax, [rcx + 0x12]
0000019d mov dword ptr [rsp + 0x90], eax
000001a4 lea eax, [rcx + 0x11]
000001a7 mov dword ptr [rsp + 0x88], eax
000001ae lea eax, [rcx + 0x10]
000001b1 mov dword ptr [rsp + 0x80], eax
000001b8 lea eax, [rcx + 0xf]
000001bb mov dword ptr [rsp + 0x78], eax
000001bf lea eax, [rcx + 0xe]
000001c2 mov dword ptr [rsp + 0x70], eax
000001c6 lea eax, [rcx + 0xd]
000001c9 mov dword ptr [rsp + 0x68], eax
000001cd lea eax, [rcx + 0xc]
000001d0 mov dword ptr [rsp + 0x60], eax
000001d4 lea eax, [rcx + 0xb]
000001d7 mov dword ptr [rsp + 0x58], eax
000001db lea eax, [rcx + 0xa]
000001de mov dword ptr [rsp + 0x50], eax
000001e2 lea eax, [rcx + 9]
000001e5 mov dword ptr [rsp + 0x48], eax
000001e9 lea eax, [rcx + 8]
000001ec mov dword ptr [rsp + 0x40], eax
000001f0 lea eax, [rcx + 7]
000001f3 mov dword ptr [rsp + 0x38], eax
000001f7 lea eax, [rcx + 6]
000001fa mov dword ptr [rsp + 0x30], eax
000001fe lea eax, [rcx + 5]
00000201 mov dword ptr [rsp + 0x28], eax
00000205 lea eax, [rcx + 4]
00000208 mov dword ptr [rsp + 0x20], eax
0000020c call 0x211
00000211 mov ecx, dword ptr [rip]
00000217 add ecx, eax
00000219 mov dword ptr [rip], ecx
0000021f mov eax, ecx
00000221 mov rcx, qword ptr [rbp + 0x80]
00000228 xor rcx, r13
0000022b call 0x230
00000230 mov rbx, qword ptr [r13 + 0xe8]
00000237 mov rsi, qword ptr [r13 + 0xf0]
0000023e mov rdi, qword ptr [r13 + 0xf8]
00000245 lea rsp, [r13 + 0xb0]
0000024c pop r15
0000024e pop r14
00000250 pop r12
00000252 pop rbp
00000253 pop r13
00000255 ret 
