int g[64]; int h[64]; short s[64];
int big(int op, int *p, int a, int b)
{
    switch (op) {
    case 0: p[b] = g[11] ^ h[a + 5] ^ 13; break;
    case 1: h[b + 21] = p[2] + p[a + 3]; break;
    case 2: g[27] += h[18] - s[b + 4]; break;
    case 3: g[a + 12] = p[b + 19] + h[2]; break;
    case 4: h[17] = p[a] * 7 + p[b + 2]; break;
    case 5: g[3] = s[a + 14] - p[14]; break;
    case 6: p[b] = g[3] ^ h[a + 8] ^ 3; break;
    case 7: h[b + 18] = p[14] + p[a + 2]; break;
    case 8: g[27] += h[19] - s[b + 4]; break;
    case 9: g[a + 8] = p[b + 21] + h[21]; break;
    case 10: h[19] = p[a] * 2 + p[b + 19]; break;
    case 11: g[19] = s[a + 13] - p[2]; break;
    case 12: p[b] = g[8] ^ h[a + 2] ^ 18; break;
    case 13: h[b + 28] = p[5] + p[a + 10]; break;
    case 14: g[14] += h[5] - s[b + 18]; break;
    case 15: g[a + 4] = p[b + 19] + h[10]; break;
    case 16: h[18] = p[a] * 27 + p[b + 22]; break;
    case 17: g[6] = s[a + 4] - p[19]; break;
    case 18: p[b] = g[19] ^ h[a + 21] ^ 7; break;
    case 19: h[b + 12] = p[4] + p[a + 18]; break;
    case 20: g[23] += h[3] - s[b + 19]; break;
    case 21: g[a + 2] = p[b + 20] + h[7]; break;
    case 22: h[16] = p[a] * 22 + p[b + 18]; break;
    case 23: g[14] = s[a + 25] - p[11]; break;
    case 24: p[b] = g[15] ^ h[a + 19] ^ 15; break;
    case 25: h[b + 12] = p[10] + p[a + 8]; break;
    case 26: g[26] += h[6] - s[b + 23]; break;
    case 27: g[a + 25] = p[b + 8] + h[3]; break;
    case 28: h[19] = p[a] * 10 + p[b + 17]; break;
    case 29: g[16] = s[a + 29] - p[11]; break;
    case 30: p[b] = g[24] ^ h[a + 15] ^ 10; break;
    case 31: h[b + 20] = p[3] + p[a + 4]; break;
    case 32: g[17] += h[14] - s[b + 6]; break;
    case 33: g[a + 25] = p[b + 11] + h[5]; break;
    case 34: h[16] = p[a] * 14 + p[b + 2]; break;
    case 35: g[22] = s[a + 3] - p[25]; break;
    case 36: p[b] = g[18] ^ h[a + 19] ^ 26; break;
    case 37: h[b + 29] = p[27] + p[a + 11]; break;
    case 38: g[11] += h[23] - s[b + 12]; break;
    case 39: g[a + 20] = p[b + 16] + h[19]; break;
    }
    return 0;
}
