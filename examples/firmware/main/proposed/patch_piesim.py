#!/usr/bin/env python3
"""Extend a copy of /workspace/pjs-vm/tools/pie/piesim.py with the instructions ex24 needs.

The original knows ee.vld.128.ip / ee.vst.128.ip / ee.vldbc.16[.ip] / ee.ldxq.32 / ee.zero.q[acc] /
ee.vadds.s16 / ee.vsubs.s16 / ee.vmul.s16 / ee.vsmulas...qacc / ee.srcmb... and a few more (see its
docstring). It has no ACCX accumulator, no EE.SRC.Q, no EE.LD.128.USAR.IP -- i.e. none of the three
instructions this kernel is built on. Rather than retype their semantics in the model, this script
copies the file and inserts handlers written from the Operation pseudo-code in
data/pie_instructions.json (the TRM page is quoted next to each one), plus a tolerant branch-target
lookup (the original's label handling expects `1b`-style local labels) and three state fields.

    python3 /tmp/patch_piesim.py     ->  /tmp/piesim_ex24.py
"""
import re

SRC = '/workspace/pjs-vm/tools/pie/piesim.py'
DST = '/tmp/piesim_ex24.py'

src = open(SRC, encoding='utf-8').read()

# (a) state: ACCX (one 40-bit accumulator) and SAR_BYTE, neither of which the original had
src = src.replace("        self.sar = 0\n",
                  "        self.sar = 0\n        self.sar_byte = 0\n        self.accx = 0\n", 1)

# (b) branch/loop labels: accept `1b`/`1f` (the original) and full `.Lname` labels
src = src.replace("pc = labels[a[1][:-1]] + 1", "pc = labels[label_of(a[1])] + 1")
src = src.replace("n, lbl = arv(a[0]), a[1][:-1]", "n, lbl = arv(a[0]), label_of(a[1])")

# (c) the handlers, inserted before the first branch the original's dispatch has after the PIE ones
HANDLERS = '''
            # ---------------- ex24 additions ------------------------------------------------
            elif op == 'label_of':
                raise NotImplementedError(op)
            elif op == 'ee.ld.128.usar.ip':
                # 1.8.18 [p93]: qu = load128({as[31:4],4{0}}); SAR_BYTE = as[3:0]; as += imm16
                Q[qi(a[0])] = self.ldq(arv(a[1]))
                self.sar_byte = arv(a[1]) & 15
                arset(a[1], arv(a[1]) + int(a[2]))
            elif op == 'ee.src.q':
                # 1.8.59 [p125]: qa = {qs1[127:0], qs0[127:0]} >> {SAR_BYTE[3:0] << 3}
                qs0, qs1 = Q[qi(a[1])], Q[qi(a[2])]
                cat = b''.join(struct.pack('<H', v) for v in qs0 + qs1)
                sh = cat[self.sar_byte:] + b'\\0' * self.sar_byte
                Q[qi(a[0])] = list(struct.unpack('<8H', sh[:16]))
            elif op == 'ee.zero.accx':
                # 1.8.210 [p298]: ACCX = 0
                self.accx = 0
            elif op == 'ee.vmulas.s16.accx':
                # 1.8.150 [p210]: sum[40:0] = ACCX + sum(d[l]); ACCX = sat40(sum)
                x, y = Q[qi(a[0])], Q[qi(a[1])]
                tot = self.accx + sum(s16(x[i]) * s16(y[i]) for i in range(8))
                self.accx = max(-(1 << 39), min((1 << 39) - 1, tot))
            elif op == 'ee.vmulas.s16.accx.ld.ip':
                # 1.8.151 [p211]: the accumulate (steps 1-6) FIRST, then qu = load128, then as += imm
                x, y = Q[qi(a[3])], Q[qi(a[4])]
                tot = self.accx + sum(s16(x[i]) * s16(y[i]) for i in range(8))
                self.accx = max(-(1 << 39), min((1 << 39) - 1, tot))
                Q[qi(a[0])] = self.ldq(arv(a[1]))
                arset(a[1], arv(a[1]) + int(a[2]))
            elif op == 'ee.srs.accx':
                # 1.8.61 [p134]: temp = ACCX >> as[5:0]; ACCX = temp; au = sat32(temp)
                temp = self.accx >> (arv(a[1]) & 63)
                self.accx = temp
                arset(a[0], max(-(1 << 31), min((1 << 31) - 1, temp)))
            elif op == 'ee.vldbc.16':
                # 1.8.86 [p170]: qu = {8{load16({as[31:1],1{0}})}}
                Q[qi(a[0])] = [self.ld16(arv(a[1]) & ~1)] * 8
            elif op == 'ee.zero.q':
                # 1.8.211 [p299]: qa = 0
                Q[qi(a[0])] = [0] * 8
            elif op == 'ee.movi.32.q':
                # 1.8.40 [p119]: qu[32*sel4+31:32*sel4] = as
                sel = int(a[2])
                v = arv(a[1]) & 0xFFFFFFFF
                Q[qi(a[0])][2 * sel] = v & 0xFFFF
                Q[qi(a[0])][2 * sel + 1] = (v >> 16) & 0xFFFF
            elif op == 'ee.movi.32.a':
                # 1.8.39 [p118]: au = qs[32*sel4+31:32*sel4]
                sel = int(a[2])
                arset(a[1], Q[qi(a[0])][2 * sel] | (Q[qi(a[0])][2 * sel + 1] << 16))
            # ---- core instructions the ex24 source uses and the original never needed
            elif op == 'addx2':
                arset(a[0], arv(a[1]) + 2 * arv(a[2]))
            elif op == 'extui':
                arset(a[0], (arv(a[1]) >> int(a[2])) & ((1 << int(a[3])) - 1))
            elif op == 'movi':
                arset(a[0], int(a[1], 0) & 0xFFFFFFFF)
            elif op == 'l32r':
                if a[1] == '.Lex24_max':
                    arset(a[0], 32767)          # the only literal the ex24 kernel reads
                else:
                    raise NotImplementedError('l32r ' + a[1])
            elif op == 's32i':
                _ad = arv(a[1]) + int(a[2])
                _v = arv(a[0]) & 0xFFFFFFFF
                for _k in range(4):
                    self.mem[_ad + _k] = (_v >> (8 * _k)) & 0xFF
            elif op == 'l32i':
                _ad = arv(a[1]) + int(a[2])
                arset(a[0], self.ld32(_ad))
            elif op == 'l16si':
                arset(a[0], s16(self.ld16(arv(a[1]) + int(a[2]))))
            elif op == 'l16ui':
                arset(a[0], self.ld16(arv(a[1]) + int(a[2])))
            elif op == 's16i':
                _ad = arv(a[1]) + int(a[2])
                _v = arv(a[0]) & 0xFFFF
                self.mem[_ad] = _v & 0xFF
                self.mem[_ad + 1] = (_v >> 8) & 0xFF
            elif op == 'min':
                arset(a[0], min(s32(arv(a[1])), s32(arv(a[2]))) & 0xFFFFFFFF)
            elif op == 'max':
                arset(a[0], max(s32(arv(a[1])), s32(arv(a[2]))) & 0xFFFFFFFF)
            elif op == 'beqz':
                if arv(a[0]) == 0:
                    pc = labels[label_of(a[1])] + 1
            elif op in ('entry', 'nop'):
                pass
            elif op == 'retw.n':
                break
'''
anchor = "            elif op == 'ee.vprelu.s16':"
assert anchor in src
src = src.replace(anchor, HANDLERS + anchor, 1)

# helpers the handlers use, and the label function
HELPERS = '''

def label_of(t):
    """The branch-target normalisation: `1b` (the original's local labels) or `.Lname` (ex24's)."""
    if t.endswith(':'):
        return t[:-1]
    if t and t[-1] in 'bf' and not t.endswith('ff') and not t.endswith('bb'):
        return t[:-1]
    return t


def s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def s16_signed(v):
    return s16(v)

'''
src = src.replace("\n\ndef _strip_comments(text):", HELPERS + "\ndef _strip_comments(text):", 1)
if 'import struct' not in src:
    src = src.replace('import re\n', 'import re\nimport struct\n', 1)
open(DST, 'w', encoding='utf-8').write(src)
print('wrote %s (%d bytes)' % (DST, len(src)))
