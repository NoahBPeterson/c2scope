00401000 push esi
00401001 mov esi, ecx
00401003 mov ecx, dword ptr [esi + 4]
00401006 mov eax, dword ptr [esi]
00401008 mov edx, dword ptr [ecx + 4]
0040100b mov eax, dword ptr [edx + eax*4]
0040100e push edi
0040100f mov edi, dword ptr [esp + 0xc]
00401013 push edi
00401014 push eax
00401015 call 0x1a
0040101a mov ecx, dword ptr [esi + 4]
0040101c mov edx, dword ptr [esi]
0040101f mov eax, dword ptr [ecx + 4]
00401022 push edx
00401023 mov ecx, dword ptr [eax + edi*4]
00401026 push ecx
00401027 call 0x2c
0040102c add esp, 0x10
0040102f pop edi
00401030 pop esi
00401031 ret 4
