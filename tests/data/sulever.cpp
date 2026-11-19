// sulever.py: a 3-operand OR whose order is decided by SU (Spore 0x0068c6d0's shape).
// lo (a zero-extended byte parameter) is SU0 and sorts after the mask term (SU1) whatever its key;
// `(*(unsigned int*)&lo & 0xff)` makes it SU1 and instruction selection folds the AND back into movzx.
struct Key { unsigned int a, b, g; };
void kind3(Key* k, unsigned char hi, unsigned char lo)
{
    k->g = ((unsigned int)hi << 8) | (unsigned int)lo | (k->g & 0xffff0000u);
}
