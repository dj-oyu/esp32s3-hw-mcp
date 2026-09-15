#!/usr/bin/env python3
"""ex24_dsfir model -- the host side of the three-way check for the 32-tap Q14 downsampling FIR kernel.

1. Reads the coefficient tables the DEVICE builds, dumped by the host C copy of filter_init
   (/tmp/ex24_tables.c -> /tmp/ex24_tables.txt): 32/44.1/48 kHz, the raw table, the final table, the
   correction, L1, the symmetry indices.
2. Executes the REAL text of examples/firmware/main/proposed/ex24_dsfir.S instruction by instruction
   through an extended copy of /workspace/pjs-vm/tools/pie/piesim.py (see /tmp/patch_piesim.py: the
   added instructions are EE.LD.128.USAR.IP, EE.SRC.Q, EE.VMULAS.S16.ACCX[.LD.IP], EE.ZERO.ACCX,
   EE.SRS.ACCX, EE.VLDBC.16, EE.ZERO.Q, EE.MOVI.32.Q and the core ops addx2/extui/movi/l16si/l16ui/
   s16i/min/max/beqz/entry/retw.n). The .S bytes are not retyped: the kernel bodies are cut out of the
   file and fed to the simulator as text.
3. Compares each kernel against a scalar reference written from the SAME C the device runs
   (mp3_decode.c:64-71), in the device's own index order, at every one of the 8 possible 16-bit lane
   offsets a ring window can have.
4. Dumps the cases to /tmp/ex24_cases.txt for the host C verifier (/tmp/ex24_ref.c), the third opinion.

    python3 /tmp/ex24_model.py
"""
import math
import os
import random
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, '/tmp')
if not os.path.exists('/tmp/piesim_ex24.py'):
    subprocess.run([sys.executable, os.path.join(HERE, 'patch_piesim.py')], check=True)
import piesim_ex24                                             # noqa: E402  (the patched copy)

PIESIM_DST = '/tmp/piesim_ex24.py'
EX24S = os.path.join(HERE, 'ex24_dsfir.S')
TABLES = '/tmp/ex24_tables.txt'
CASES = '/tmp/ex24_cases.txt'

Q14 = 16384
MASK16 = 0xFFFF

BASE_RING = 0x1000
BASE_FILT = 0x2000
BASE_OUT = 0x3000
BASE_MISC = 0x4000
BASE_FRAME = 0x6000     # where the model puts the kernels' own stack frame (a1)


def s16(v):
    v &= MASK16
    return v - 0x10000 if v & 0x8000 else v


def sat16(v):
    return max(-32768, min(32767, v))


def strip_comments(text):
    return re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)


def kernel_body(path, name):
    """The instruction text of `name`: from just after its label to its .size directive, with the
    mnemonics lowercased (the assembler is case-insensitive; the simulator dispatches on lower case).
    Labels, register names and comments are left exactly as the .S has them."""
    src = strip_comments(open(path, encoding='utf-8').read())
    m = re.search(r'^' + re.escape(name) + r':\s*$(.*?)^\s*\.size\s+' + re.escape(name) + r'\b',
                  src, re.S | re.M)
    if not m:
        raise SystemExit('kernel %s not found' % name)
    out = []
    for line in m.group(1).splitlines():
        s = line.strip()
        if not s or s.endswith(':'):
            out.append(line)
            continue
        t = s.split(None, 1)
        op = t[0].lower()
        out.append('    ' + (op if len(t) == 1 else op + ' ' + t[1]))
    return '\n'.join(out)


def read_tables():
    out = {}
    for line in open(TABLES, encoding='utf-8'):
        p = line.split()
        if not p:
            continue
        if p[0] == 'TABLE':
            out.setdefault(int(p[1]), {})['stats'] = [int(x) for x in p[2:]]
        elif p[0] == 'FILTER':
            out.setdefault(int(p[1]), {})['filter'] = [int(x) for x in p[2:]]
        elif p[0] == 'REV':
            out.setdefault(int(p[1]), {})['rev'] = [int(x) for x in p[2:]]
        elif p[0] == 'HALF':
            out.setdefault(int(p[1]), {})['half'] = [int(x) for x in p[2:]]
        elif p[0] == 'SYM':
            out.setdefault(int(p[1]), {})['corr'] = int(p[4].split('=')[1])
    return out


def trunc_div(a, b):
    """C's signed /: truncation toward zero."""
    q = abs(a) // b
    return q if a >= 0 else -q


def scalar_device(history, cursor, filt, shift):
    """mp3_decode.c:64-71 verbatim, in its own index order: history[(cursor-k)&31] * filter[k], then
    sum/16384 (truncating), then the int16 clamp. Returns (sum, truncated, clamped)."""
    s = 0
    for k in range(32):
        s += history[(cursor - k) & 31] * filt[k]
    t = trunc_div(s, 1 << shift)
    return s, t, sat16(t)


def scalar_shift(history, cursor, filt, shift):
    """The same sum with an arithmetic shift where the C divides."""
    s = sum(history[(cursor - k) & 31] * filt[k] for k in range(32))
    return s, s >> shift, sat16(s >> shift)


def put16(mem, addr, vals):
    for i, v in enumerate(vals):
        v &= MASK16
        mem[addr + 2 * i] = v & 0xFF
        mem[addr + 2 * i + 1] = (v >> 8) & 0xFF


def get16(mem, addr, n):
    return [mem[addr + 2 * i] | (mem[addr + 2 * i + 1] << 8) for i in range(n)]


class TrackingSim(piesim_ex24.Sim):
    """A Sim that records every byte address it reads, so the read footprint of a kernel can be
    measured rather than asserted. ld16/ld32/ldq all funnel through ld16."""

    def __init__(self, mem):
        piesim_ex24.Sim.__init__(self, mem)
        self.reads = []

    def ld16(self, a):
        self.reads.append(a)
        self.reads.append(a + 1)
        return piesim_ex24.Sim.ld16(self, a)


def ar_map(**kw):
    """The address registers a kernel call starts with: a1 = a real frame address (so a spill/reload
    round-trips through memory), a10..a15 = 0 (the caller's a2..a7 -- their values do not matter, the
    kernel must not touch them without a spill)."""
    base = {('a%d' % i): 0 for i in range(2, 16)}
    base['a1'] = BASE_FRAME
    base.update(kw)
    return base


class Runner:
    """One bytearray of memory; the kernels cut out of the .S file and run on it."""

    def __init__(self):
        self.bodies = {n: kernel_body(EX24S, n) for n in
                       ('ex24_dsfir32', 'ex24_funnel_probe', 'ex24_dsfir32_run', 'ex24_dsfir32_sym')}

    @staticmethod
    def mem():
        return bytearray(0x8000)

    def fir32(self, mem, window_addr, filt_addr, shift, counts=None, track=False):
        sim = TrackingSim(mem) if track else piesim_ex24.Sim(mem)
        sim.run(self.bodies['ex24_dsfir32'], ar_map(a2=window_addr, a3=filt_addr, a4=shift))
        if counts is not None:
            counts.append(sim.count)
        if track:
            win = [a for a in sim.reads if a < BASE_FILT]     # the window side only
            return s16(sim.ar['a2']), (min(win), max(win))
        return s16(sim.ar['a2'])

    def funnel(self, mem, window_addr, out_addr):
        sim = piesim_ex24.Sim(mem)
        sim.run(self.bodies['ex24_funnel_probe'], ar_map(a2=window_addr, a3=out_addr))
        return get16(mem, out_addr, 24), sim.count

    def run_n(self, mem, in_addr, out_addr, n, filt_addr, state_addr, counts=None, track=False):
        sim = TrackingSim(mem) if track else piesim_ex24.Sim(mem)
        sim.run(self.bodies['ex24_dsfir32_run'],
                ar_map(a2=in_addr, a3=out_addr, a4=n, a5=filt_addr, a6=state_addr))
        if counts is not None:
            counts.append(sim.count)
        if track:
            return sim.ar['a2'], sim.count, (min(sim.reads), max(sim.reads))
        return sim.ar['a2'], sim.count

    def sym(self, mem, hist_addr, half_addr, corr, shift, counts=None, track=False):
        sim = TrackingSim(mem) if track else piesim_ex24.Sim(mem)
        sim.run(self.bodies['ex24_dsfir32_sym'],
                ar_map(a2=hist_addr, a3=half_addr, a4=corr & MASK16, a5=shift))
        if counts is not None:
            counts.append(sim.count)
        if track:
            win = [a for a in sim.reads if a < BASE_FILT]     # the window side only
            return s16(sim.ar['a2']), (min(win), max(win))
        return s16(sim.ar['a2'])


def main():
    R = Runner()
    tables = read_tables()
    rng = random.Random(0xE24)
    dump = open(CASES, 'w')
    print('ex24_dsfir model -- the .S text executed through %s (piesim.py + the ex24 patch)' %
          PIESIM_DST)
    print()

    # ------------------------------------------------------------------ T0: the tables
    print('T0 the coefficient tables (host C copy of filter_init, /tmp/ex24_tables.c)')
    for rate in (32000, 44100, 48000):
        rawtotal, corr, l1, accmax, asym, rawsym = tables[rate]['stats']
        print('   %5u Hz  raw total %6d  correction %+d  L1 %5d  |ACCX|max = 32768*L1 = %d' %
              (rate, rawtotal, corr, l1, accmax))
        print('           asymmetric indices: %d in the final table, %d in the RAW table' %
              (asym, rawsym))
        print('           filter[15]=%d filter[16]=%d  |sum|max/2^14 = %.3f * 32768 (clamp reachable)'
              % (tables[rate]['filter'][15], tables[rate]['filter'][16], l1 / 16384.0))
    print()

    # ------------------------------------------------------------------ T1: kernel A vs the scalar
    nA = badA = badAs = roundDiff = clampHits = 0
    first = firsts = None
    offs = {}
    cntA = []
    perrate = {}
    for rate in (32000, 44100, 48000):
        rev, filt = tables[rate]['rev'], tables[rate]['filter']
        for trial in range(600):
            delta = 2 * (trial % 8)                      # every lane offset a ring window can have
            if trial % 4 == 0:
                vals = [rng.randint(-32768, 32767) for _ in range(32)]
            elif trial % 4 == 1:
                vals = [rng.choice([-32768, 32767, 0, 1, -1]) for _ in range(32)]
            elif trial % 4 == 2:
                vals = [rng.randint(-2000, 2000) for _ in range(32)]
            else:
                vals = [(32767 if filt[i] >= 0 else -32768) for i in range(32)]   # sign-matched: the
                                                                                 # worst |sum| possible
            mem = R.mem()
            put16(mem, BASE_FILT, rev)
            put16(mem, BASE_RING + delta, vals)
            got = R.fir32(mem, BASE_RING + delta, BASE_FILT, 14, cntA)
            hist = vals                                  # history[i] = window[i], cursor = 31: then
            s, t, want = scalar_device(hist, 31, filt, 14)   # history[(31-k)&31] = window[31-k] and
            s2, t2, want2 = scalar_shift(hist, 31, filt, 14)  # the kernel's sum = window[i]*rev[i]
            assert s == s2 and t2 == s >> 14
            nA += 1
            offs[delta] = offs.get(delta, 0) + 1
            lo = 0
            gotk = sum(vals[i] * rev[i] for i in range(32))
            assert gotk == s, 'the two orders of the same dot product must be the same integer'
            if got != want:
                badA += 1
                if first is None:
                    first = (rate, delta, s, t, got, want)
            if got != want2:
                badAs += 1
                if firsts is None:
                    firsts = (rate, delta, s, t, got, want2)
            if want != t:
                clampHits += 1
            if t != (s >> 14):
                roundDiff += 1
            perrate.setdefault(rate, []).append((delta, vals, got, want, want2))
    print('T1 ex24_dsfir32 vs mp3_decode.c:64-71, 3 tables x 600 windows = %d windows' % nA)
    print('   lane offsets exercised: %s' % ', '.join('o=%d x%d' % (k // 2, v)
                                                      for k, v in sorted(offs.items())))
    print('   vs the C division  (sum/16384, trunc):  %d mismatching windows' % badA)
    print('   vs the arithmetic shift (sum >> 14):    %d mismatching windows' % badAs)
    if first:
        rate, delta, s, t, got, want = first
        print('   FIRST trunc mismatch: %u Hz o=%d sum=%d floor=%d trunc=%d kernel=%d want=%d' %
              (rate, delta // 2, s, s >> 14, t, got, want))
    print('   windows where sum < 0 and sum %% 2^14 != 0 (the entire difference): %d of %d (%.1f%%)'
          % (roundDiff, nA, 100.0 * roundDiff / nA))
    print('   windows where the int16 clamp fires: %d of %d' % (clampHits, nA))
    print('   instructions executed per call: %d (model), identical for every call' % cntA[0])
    print()

    # ------------------------------------------------------------------ T1b: the read footprint
    print('T1b read footprint of ex24_dsfir32 (byte addresses relative to the window pointer)')
    rev = tables[44100]['rev']
    for delta in range(0, 16, 2):
        mem = R.mem()
        put16(mem, BASE_FILT, rev)
        put16(mem, BASE_RING + delta, list(range(100, 132)))
        _, (lo, hi) = R.fir32(mem, BASE_RING + delta, BASE_FILT, 14, track=True)
        print('   o=%d  window at +%d: reads %+d .. %+d  -> %d bytes from the aligned base %+d,'
              ' ending %d bytes past the window (the 64-byte window plus the funnel\'s 16-byte'
              ' over-read)' % (delta // 2, delta, lo - BASE_RING - delta, hi - BASE_RING - delta,
                               hi - lo + 1, -(delta), hi - (BASE_RING + delta + 63)))
    print()

    # ------------------------------------------------------------------ T2: the funnel probe
    print('T2 ex24_funnel_probe: one window at each of the 8 lane offsets')
    okc = 0
    for delta in range(0, 16, 2):
        vals = [100 + i for i in range(32)]
        mem = R.mem()
        put16(mem, BASE_RING + delta, vals)
        out, cn = R.funnel(mem, BASE_RING + delta, BASE_OUT)
        want = vals[:8]
        ok = out[:8] == want
        okc += ok
        print('   o=%d  funnel=%s %s   plain-VLD=%s   crossed-SRC.Q=%s' %
              (delta // 2, 'ok' if ok else 'WRONG', out[:8] if not ok else '', out[8:16], out[16:24]))
    print('   the funnel is correct at %d of 8 offsets; the plain VLD and the crossed operands each'
          ' agree only at o=0' % okc)
    print()

    # ------------------------------------------------------------------ T3: the run kernel and the ring
    print('T3 ex24_dsfir32_run: 512 samples through the 128-lane ring vs the device ring')
    for rate in (32000, 44100, 48000):
        rev, filt = tables[rate]['rev'], tables[rate]['filter']
        n = 512
        sig = [rng.randint(-32768, 32767) for _ in range(n)]
        mem = R.mem()
        put16(mem, BASE_FILT, rev)
        put16(mem, BASE_MISC, [0, 14] + [0] * 6)          # state[0]=w, state[1]=shift
        put16(mem, BASE_MISC + 16, [0] * 128)             # the ring
        put16(mem, BASE_OUT, sig)
        put16(mem, BASE_OUT + 0x400, [0] * n)
        _, cn = R.run_n(mem, BASE_OUT, BASE_OUT + 0x400, n, BASE_FILT, BASE_MISC)
        got = [s16(v) for v in get16(mem, BASE_OUT + 0x400, n)]
        hist, cur, want, wantt = [0] * 32, 0, [], []
        for i in range(n):
            hist[cur] = sig[i]
            s, t, w = scalar_device(hist, cur, filt, 14)
            _, t2, w2 = scalar_shift(hist, cur, filt, 14)
            cur = (cur + 1) & 31
            wantt.append(w)
            want.append(w2)
        bad = sum(1 for i in range(n) if got[i] != want[i])
        badt = sum(1 for i in range(n) if got[i] != wantt[i])
        firstbad = next((i for i in range(n) if got[i] != want[i]), None)
        print('   %u Hz: %d samples, %d mismatching vs sum>>14 (first %s), %d vs sum/16384'
              ' (the floor/trunc difference); state[0] = %d; %d instructions for the whole call'
              ' (%.2f/sample)' %
              (rate, n, bad, firstbad, badt, get16(mem, BASE_MISC, 1)[0], cn, cn / float(n)))
    print()

    # ------------------------------------------------------------------ T4: the symmetric fold
    print('T4 ex24_dsfir32_sym: the fold vs the scalar (the mirrored window is supplied here)')
    for rate in (32000, 44100, 48000):
        rev, filt = tables[rate]['rev'], tables[rate]['filter']
        half, corr = tables[rate]['half'], tables[rate]['corr']
        stat = {'small': [0, 0, 0], 'full': [0, 0, 0], 'sine': [0, 0, 0]}
        cntS = []
        worst = 0
        for trial in range(400):
            delta = 2 * (trial % 8)
            cls = ('small', 'full', 'sine')[trial % 3]
            if cls == 'small':
                vals = [rng.randint(-14000, 14000) for _ in range(32)]
            elif cls == 'full':
                vals = [rng.randint(-32768, 32767) for _ in range(32)]
            else:
                ph = rng.random() * 6.283
                vals = [int(32000 * math.sin(ph + 6.283 * 1000.0 * (i + 1) / rate)) for i in range(32)]
            mem = R.mem()
            put16(mem, BASE_FILT, rev)
            put16(mem, BASE_RING + delta, vals)
            put16(mem, BASE_RING + delta + 64, list(reversed(vals)))
            put16(mem, BASE_MISC, half)
            got = R.sym(mem, BASE_RING + delta, BASE_MISC, corr, 14, cntS)
            s, t, wantt = scalar_device(vals, 31, filt, 14)
            _, _, want = scalar_shift(vals, 31, filt, 14)
            sat = sum(1 for k in range(16) if abs(vals[k] + vals[31 - k]) > 32767)
            stat[cls][0] += 1
            if sat:
                stat[cls][1] += 1                      # windows the fold CANNOT represent
                stat[cls][2] += 1 if got != want else 0
                worst = max(worst, abs(got - want))
            elif got != want:
                stat[cls][2] += 1
        print('   %u Hz  mid_coef=%d   %d instructions per call' % (rate, corr, cntS[0]))
        for cls, label in (('small', '|w| <= 14000  (no pair can overflow)'),
                           ('full', 'full-scale random'),
                           ('sine', 'a 1 kHz full-scale sine')):
            n, nsat, nbad = stat[cls]
            print('      %-34s %4d windows, %4d with a saturated pair sum, %4d mismatching the'
                  ' scalar' % (label, n, nsat, nbad))
        print('      worst |kernel - scalar| over the saturated windows: %d' % worst)
    print()

    # ------------------------------------------------------------------ the dump for the C verifier
    ncase = 0
    for rate in (32000, 44100, 48000):
        for (delta, vals, got, want, want2) in perrate[rate][:40]:
            dump.write('CASE %u %d %d %d %d %s\n' %
                       (rate, delta // 2, want, want2, got, ','.join(str(v) for v in vals)))
            ncase += 1
    for rate in (32000, 44100, 48000):
        n = 64
        sig = [rng.randint(-32768, 32767) for _ in range(n)]
        mem = R.mem()
        put16(mem, BASE_FILT, tables[rate]['rev'])
        put16(mem, BASE_MISC, [0, 14] + [0] * 6)
        put16(mem, BASE_MISC + 16, [0] * 128)
        put16(mem, BASE_OUT, sig)
        put16(mem, BASE_OUT + 0x400, [0] * n)
        R.run_n(mem, BASE_OUT, BASE_OUT + 0x400, n, BASE_FILT, BASE_MISC)
        got = [s16(v) for v in get16(mem, BASE_OUT + 0x400, n)]
        dump.write('RUN %u %s\nSIG %s\n' % (rate, ','.join(str(v) for v in got),
                                            ','.join(str(v) for v in sig)))
        ncase += 1
    dump.close()
    print('case dump for the host C verifier: %d CASE lines + 3 RUN+SIG pairs -> %s' % (ncase, CASES))


if __name__ == '__main__':
    main()
