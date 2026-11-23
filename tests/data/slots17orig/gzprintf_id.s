00000000 mov qword ptr [rsp + 0x10], rdx
00000005 mov qword ptr [rsp + 0x18], r8
0000000a mov qword ptr [rsp + 0x20], r9
0000000f push rbx
00000010 mov eax, 0x1030
00000015 call 0x1a
0000001a sub rsp, rax
0000001d mov rax, qword ptr [rip]
00000024 xor rax, rsp
00000027 mov qword ptr [rsp + 0x1020], rax
0000002f mov rbx, rcx
00000032 mov r8, rdx
00000035 lea r9, [rsp + 0x1050]
0000003d lea rcx, [rsp + 0x20]
00000042 mov edx, 0x1000
00000047 mov byte ptr [rsp + 0x101f], 0
0000004f call 0x54
00000054 lea edx, [rax - 1]
00000057 cmp edx, 0xffe
0000005d ja 0x7b
0000005f cmp byte ptr [rsp + 0x101f], 0
00000067 jne 0x7b
00000069 lea rdx, [rsp + 0x20]
0000006e mov r8d, eax
00000071 mov rcx, rbx
00000074 call 0x79
00000079 jmp 0x7d
0000007b xor eax, eax
0000007d mov rcx, qword ptr [rsp + 0x1020]
00000085 xor rcx, rsp
00000088 call 0x8d
0000008d add rsp, 0x1030
00000094 pop rbx
00000095 ret 
