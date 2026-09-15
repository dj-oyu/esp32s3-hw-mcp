# ex11 (proposed) — 8x8 integer block transform, the eight columns in the SIMD lanes

> **Status (integrated)**: this kernel now lives at `examples/firmware/main/ex11_block8x8.S`, is listed in
> `examples/firmware/main/CMakeLists.txt`, and is called from `main.c`'s `ex11()`. It builds and the host
> checker (`tools/selftest_examples_checker.py`) passes; it has **not been run on hardware**. The body below
> still says `proposed/` and "not wired in" because it records the state before the move (the pasted
> command transcripts are the originals, and the `.S` was moved byte for byte), and the heading still
> carries the old `(proposed)`.

`examples/firmware/main/proposed/ex11_block8x8.S` — the transform MP3 (IMDCT / hybrid filterbank), JPEG
(integer DCT) and H.264 (4x4 / 8x8 integer transform) all reduce to:

```
out[k][j] = sat16( ( sum(i = 0..7) coef[k][i] * block[i][j] ) >> shift )
```

One 8x8 int16 block, one 8x8 int16 coefficient matrix, eight lanes busy per output row. This is ex07
(`ex07_transform3d.S`) with the MAC row extended from four taps to eight and the accumulator readout made
explicit.

```
void ex11_block8x8(const int16_t *coef, const int16_t *block, int16_t *out, uint32_t shift);
    a2 = coef , a3 = block, a4 = out, a5 = shift     (windowed ABI: entry a1, 32 ... retw.n)
```

## Layout (the contract, stated exactly)

| buffer  | shape | addressing | what a 128-bit load puts in lanes 0..7 |
|---|---|---|---|
| `coef`  | 64 int16, row-major | `coef[k][i]` at `coef + 16*k + 2*i` | `coef[k][0..7]` — the eight taps of output row `k` |
| `block` | 64 int16, row-major | `block[i][j]` at `block + 16*i + 2*j` | `block[i][0..7]` — **the eight columns `j` are the eight lanes** |
| `out`   | 64 int16, row-major | `out[k][j]` at `out + 16*k + 2*j` | lane `j` of the register stored for row `k` is `out[k][j]` |

`EE.VSMULAS.S16.QACC qx, qy, sel8` broadcasts ONE lane of `qy` (TRM p269: `temp = qy[sel8*16+15 : sel8*16]`)
and multiply-accumulates it against all eight lanes of `qx`, each lane into its own saturating 40-bit
accumulator. So the **reduction index `i` goes in the broadcast operand and the free index `j` in the lanes**:
`q0` holds the coefficient row (lane `i` = `coef[k][i]`, broadcast lane index = tap index), `q1..q4` hold the
block rows (lane `j` = `block[i][j]`), and `sel8 = i` walks the eight taps.

**No transposition is needed for this pass**: the row that streams through memory is the row that streams
through the MAC sequence, and the free index is the one inside a row. That is the mirror image of ex07, where
the free index (the eight vertices) was also the within-row index but the reduction index was the row — same
instruction, same shape, the 8x8's two axes swapped into the two roles.

For the **other axis** of a 2-D transform the caller either transposes the intermediate block and calls again,
or supplies a second 64-word table holding `coef^T`; both are the same arithmetic because the kernel is a
general 8x8 matrix product. The transpose form is the one a codec uses when it wants one table (the `S` case
in T3 below).

Two consequences of the layout that the caller has to respect:

* **16-byte alignment.** The 128-bit PIE forms force the low four address bits of `as` to 0 (TRM p49), so a
  misaligned pointer is not reported — it silently reads or writes the neighbouring bytes (ex03 reproduced
  this on the device).
* **The first pass' output is int16.** `EE.SRCMB.S16.QACC` saturates, so a two-pass transform is exact only
  while the intermediate fits 16 bits; pick the pass-1 shift for headroom, not for full scale.

## Cost

Static issue slots (not measured — see "What is not confirmed"): the loop body is 27 instructions / 75 bytes
per output row of eight elements, i.e.

| quantity | value |
|---|---|
| instructions per output row (8 elements) | 27 (75 bytes) |
| **instructions per output element** | **3.375** |
| **instructions per 8x8 block** | **216** (600 bytes in the loop) for the 64 outputs |
| total function | 32 instructions / 86 bytes (`nm -S`: `ex11_block8x8` = 0x56 bytes) |
| broadcast-MACs per block | 64 (`EE.VSMULAS.S16.QACC`) = **512 lane multiply-accumulates** |
| 128-bit loads / stores per block | 72 loads (8 block rows × 8 output rows + 8 coefficient rows), 8 stores |
| readouts per block | 8 (`EE.SRCMB.S16.QACC`, eight lanes each) |
| accumulator gaps per block | 32 slots (4 `nop.n` × 8 rows = 64 bytes) |

The same arithmetic in scalar C (`ex11_block8x8_c` below) is 512 multiply-accumulates plus 512 loads and 64
stores for the block, dispatched one at a time; the kernel issues 64 MAC instructions for the same work. The
C side of the `BENCH` line already exists in the suite's shape (`ex07_ccount()` for the cycle counter,
`main.c`'s `ex07`/`ex08` for the harness), so the on-device number is a build-and-flash away — it is not in
this file because ex11 is proposed, not wired in.

### Where the 40-bit accumulator's saturation bites

`EE.VSMULAS.S16.QACC` accumulates into a 40-bit saturating lane (TRM p269, `min(max(..., -2^39), 2^39-1)`;
ex05 confirmed the same saturating behaviour for ACCX on silicon: 32000×32000×200 stopped at exactly
2^39-1 = 549755813887).

Worst case for one output element of the 8-tap row, both operands full-scale int16:

```
|sum| <= 8 * |coef|max * |block|max = 8 * 32768 * 32768 = 8589934592 = 2^33
40-bit range: [-549755813888, 549755813887]  ->  headroom factor 64 (6 bits)
```

So **no single 8x8 output row can saturate the accumulator**, even with full-scale Q15 coefficients and
full-scale int16 data — there are six spare bits. Concretely:

* A real DCT-II row is much smaller than full scale: the Q15 normalized DCT-II matrix used in T4 has
  `|C[k][i]| <= 16384`, so the product bound drops to `8 * 2^29 = 2^32`, seven spare bits.
* The accumulator only starts to bite if the **same accumulator is carried across more than one 8-tap
  group**, i.e. if the kernel is called with `EE.ZERO.QACC` removed and the row's MACs chained into a longer
  reduction: `2^39 / 2^30 = 512` maximal-magnitude products saturate one lane, so a 512-tap filter (or ~64
  chained 8-tap groups at full scale) is where saturation stops being free. This kernel deliberately zeroes
  the accumulator at the top of every output row — for **reuse**, not for overflow, because `SRCMB` writes its
  shifted result back into QACC (see below).
* **The saturation that actually bites in the fast path is the 16-bit readout.** `SRCMB` saturates each
  shifted lane into ±32767, so a shift that is too small turns real coefficients into `0x7fff`/`0x8000`
  (ex09's `mac4_g*` rows are exactly that picture: `7fff,8000,7fff,8000,...` at gap 0, 2 and 4). For the Q15
  DCT-II, an 8x8 block of ±32767 renders a DC of `8 * 11585 * 32767 >> 15 ≈ 92600` — 3x past int16 — so the
  pass shift must be chosen for the intended dynamic range, not derived from the coefficient scale.

## C reference (`ex11_block8x8_c`)

An independent scalar reading of the same contract, int64 accumulation, sat16 only at the end — the same
shape as `main.c`'s `c_transform8`/`model_mac4` references (`examples.h` declaration:
`void ex11_block8x8(const int16_t *coef, const int16_t *block, int16_t *out, uint32_t shift);` — not added,
see "What is not confirmed"):

```c
static int16_t sat16(int32_t v)
{
    return v > 32767 ? 32767 : (v < -32768 ? -32768 : (int16_t)v);
}

/* out[k][j] = sat16((sum over i of coef[k][i] * block[i][j]) >> shift), row-major, int64 accumulation. */
void ex11_block8x8_c(const int16_t *coef, const int16_t *block, int16_t *out, uint32_t shift)
{
    for (int k = 0; k < 8; k++) {
        for (int j = 0; j < 8; j++) {
            int64_t acc = 0;
            for (int i = 0; i < 8; i++) {
                acc += (int32_t)coef[k * 8 + i] * (int32_t)block[i * 8 + j];
            }
            out[k * 8 + j] = sat16((int32_t)(acc >> shift));
        }
    }
}
```

## Python model and the checks it ran

The model re-implements the **kernel's datapath** (eight saturating 40-bit lanes, then `SRCMB`'s
`>> shift` + int16 saturate, plus a variant with `SRCMB`'s documented write-back) and compares it against a
naive int64 reference. The script below is the real one that produced the output after it.

```python
#!/usr/bin/env python3
"""ex11_block8x8 -- an independent model of the kernel's datapath, checked against a naive reference.

Two models of the same pseudo-code, on purpose (the suite's MANUAL -> C -> Python discipline):

  naive(coef, block, shift)  : the straight scalar reading of the contract, int64 accumulation, sat16 at the
                               end. This is what ex11_block8x8_c in the .md computes.
  sim(coef, block, shift)    : the kernel's datapath, per TRM p269/p130:
                                 - eight independent 40-bit SATURATING lane accumulators (VSMULAS.S16.QACC),
                                   lane j summed over the eight taps i = 0..7;
                                 - SRCMB.S16.QACC: each 40-bit lane >> (shift & 63), then saturated to int16.
                               A third variant adds SRCMB's documented write-back (QACC = shifted value),
                               because the device said read_modify_write (ex09).

Checks:
  T1 random      : 200 random (coef, block, shift) cases, naive vs kernel datapath, shifts 0..20.
  T2 identity    : coef = I -> out == block >> shift (sat16): the degenerate symmetric case, and the check
                   that pins the row/lane mapping (a transposed or lane-rotated kernel would not pass it).
  T3 symmetric   : the symmetric cosine matrix S[k][i] = round(32767*cos(pi*k*i/8)) (S == S^T, so one table
                   serves both axes): the two-pass transpose trick computed with the kernel datapath equals
                   the same two passes computed with the naive reference, saturation in the middle included.
  T4 DCT-II      : the Q15 normalized DCT-II matrix C -- C*C^T = 2^30*I (report the real Gram matrix
                   residuals), the render of a constant block (DC only), and a kernel round trip
                   X = (C*B)>>15, Y = (C^T*X)>>15 with max |Y-B| reported.
  T5 write-back  : sim_with_writeback == sim, because every output row starts with EE.ZERO.QACC.
"""
import math
import random

N = 8
SAT40_MIN, SAT40_MAX = -(1 << 39), (1 << 39) - 1
results = []


def sat16(v):
    return -32768 if v < -32768 else (32767 if v > 32767 else v)


def sat40(v):
    return SAT40_MIN if v < SAT40_MIN else (SAT40_MAX if v > SAT40_MAX else v)


def naive(coef, block, shift):
    """ex11_block8x8_c: out[k][j] = sat16((sum_i coef[k][i]*block[i][j]) >> shift), int64 accumulation."""
    out = [[0] * N for _ in range(N)]
    for k in range(N):
        for j in range(N):
            acc = 0
            for i in range(N):
                acc += coef[k][i] * block[i][j]
            out[k][j] = sat16(acc >> shift)
    return out


def sim(coef, block, shift, writeback=False):
    """The kernel's datapath: per output row k, 8 broadcast-MACs into 8 saturating 40-bit lanes, then
    SRCMB (>> shift, sat16). writeback=True also models SRCMB storing the shifted value back into QACC."""
    out = [[0] * N for _ in range(N)]
    for k in range(N):
        qacc = [0] * N                                  # EE.ZERO.QACC
        for i in range(N):
            tap = coef[k][i]                            # broadcast lane sel8 = i of the coef row
            for j in range(N):
                qacc[j] = sat40(qacc[j] + block[i][j] * tap)
        for j in range(N):
            sh = qacc[j] >> (shift & 63)                # as[5:0]
            if writeback:
                qacc[j] = sh                            # SRCMB writes the shifted value back (ex09)
            out[k][j] = sat16(sh)
    return out


def flat(m):
    return [v for row in m for v in row]


def eq(a, b):
    return flat(a) == flat(b)


def transpose(m):
    return [[m[i][j] for i in range(N)] for j in range(N)]


def two_pass(model, coef, block, s1, s2):
    """The transpose trick a separable codec uses: rows pass, transpose, rows pass, transpose back.
    Y = coef*(coef*B)^T, and transpose(Y) = coef*B*coef^T is the 2-D transform."""
    return transpose(model(coef, transpose(model(coef, block, s1)), s2))


def check(name, ok, detail):
    results.append(ok)
    print("%-15s: %-5s %s" % (name, "PASS" if ok else "FAIL", detail))


rng = random.Random(0x1234)

# ------------------------------------------------------------------ T1 random
bad = 0
cases = 200
for c in range(cases):
    coef = [[rng.randint(-32768, 32767) for _ in range(N)] for _ in range(N)]
    block = [[rng.randint(-32768, 32767) for _ in range(N)] for _ in range(N)]
    shift = c % 21
    if not eq(naive(coef, block, shift), sim(coef, block, shift)):
        bad += 1
check("T1 random", bad == 0, "%d/%d cases agree (naive vs kernel datapath, shifts 0..20), %d elements"
      % (cases - bad, cases, cases * 64))
worst = 8 * 32768 * 32768
print("   worst-case |sum| over 8 taps of two full-scale int16 (Q15 x Q15): 8 * 32768^2 = %d = 2**%.0f"
      % (worst, math.log2(worst)))
print("   40-bit accumulator: [%d, %d] -> headroom factor %.0f (6 bits), so no single 8-tap row can"
      " saturate it" % (SAT40_MIN, SAT40_MAX, (1 << 39) / worst))

# ------------------------------------------------------------------ T2 identity
ident = [[1 if k == i else 0 for i in range(N)] for k in range(N)]
block = [[rng.randint(-32768, 32767) for _ in range(N)] for _ in range(N)]
ok2 = True
for shift in (0, 8, 15):
    want = [[sat16(block[k][j] >> shift) for j in range(N)] for k in range(N)]
    got = sim(ident, block, shift)
    ok2 &= eq(got, want) and eq(naive(ident, block, shift), got)
    print("   identity, shift %2d: out == block >> %d (sat16) == %s; naive agrees: %s"
          % (shift, shift, eq(got, want), eq(naive(ident, block, shift), got)))
check("T2 identity", ok2, "out[k][j] == block[k][j]: rows and lanes land where the contract says")

# ------------------------------------------------------------------ T3 symmetric cosine matrix
S = [[int(round(32767 * math.cos(math.pi * k * i / N))) for i in range(N)] for k in range(N)]
sym = S == transpose(S)
B = [[rng.randint(-64, 64) for _ in range(N)] for _ in range(N)]
for s1, s2 in ((15, 15), (15, 18)):
    a = two_pass(sim, S, B, s1, s2)
    b = two_pass(naive, S, B, s1, s2)
    print("   two-pass S, shifts %d/%d: kernel datapath == naive reference over 64 elements: %s"
          % (s1, s2, eq(a, b)))
    sym &= eq(a, b)
const = [[7] * N for _ in range(N)]
c_out = sim(S, const, 15)
col_const = all(len(set(row)) == 1 for row in c_out)
# independent prediction from the table alone: with a constant block, out[k][j] = sat16((sum_i S[k][i]) * 7 >> 15)
pred = [[sat16((sum(S[k]) * 7) >> 15)] * N for k in range(N)]
print("   S row sums = %s (rows 0..3); constant block -> every output row constant across the eight lanes:"
      " %s" % ([sum(S[k]) for k in range(4)], col_const))
print("   constant block (7) render, from the row sums: %s" % [pred[k][0] for k in range(N)])
print("   constant block (7) render, from the kernel model: %s" % [c_out[k][0] for k in range(N)])
check("T3 symmetric", sym and col_const and c_out == pred,
      "S == S^T (one 64-word table for both axes); kernel datapath == naive reference through both transpose"
      " passes; and the constant-block render equals the row-sum prediction exactly")

# ------------------------------------------------------------------ T4 Q15 DCT-II
C = [[int(round(32768 * math.sqrt(1.0 / N if k == 0 else 2.0 / N) * math.cos(math.pi * (2 * i + 1) * k / (2 * N))))
      for i in range(N)] for k in range(N)]
gram = [[sum(C[k][i] * C[m][i] for i in range(N)) for m in range(N)] for k in range(N)]
diag = set(gram[k][k] for k in range(N))
off = max(abs(gram[k][m]) for k in range(N) for m in range(N) if k != m)
print("   C is orthonormal in Q15: diag(C*C^T) = %s (2^30 = %d), worst |off-diagonal| = %d = %.2f LSB"
      % (sorted(diag), 1 << 30, off, off / 32768.0))
print("   C == C^T (symmetric)? %s -> the inverse pass needs the transposed table (C_III), unlike S"
      % (C == transpose(C)))
print("   C row sums = %s (rows 0..3) -> a constant block renders into row 0 only, and rows 1..7 sum to exactly 0"
      % [sum(C[k]) for k in range(4)])
X = sim(C, B, 15)                       # pass 1: DCT-II with C in Q15, >> 15 leaves Q0 coefficients
Y = sim(transpose(C), X, 15)            # pass 2: C^T; C*C^T = 2^30 I, so >>15 then >>15 reconstructs
err = max(abs(Y[k][j] - B[k][j]) for k in range(N) for j in range(N))
# Derived bound: `>>` is floor, so pass 1 loses < 1 LSB per coefficient; pass 2 sums 8 of those through
# |C| <= 16384 = 0.5 * 2^15 -> |C^T * dX| >> 15 <= 8 * 16384 / 32768 = 4, plus pass 2's own < 1.
bound = 8 * 16384 // 32768 + 1
print("   kernel round trip X=(C*B)>>15 then Y=(C^T*X)>>15: max |Y-B| = %d, truncation bound = %d"
      " (8 * max|C| / 2^15 + 1; `>>` floors, it does not round)" % (err, bound))
print("   B[0] = %s" % B[0])
print("   X[0] = %s  (DC row)" % X[0])
print("   Y[0] = %s" % Y[0])
cB = [[1000] * N for _ in range(N)]
cX = sim(C, cB, 15)
cpred = [[sat16((sum(C[k]) * 1000) >> 15)] * N for k in range(N)]
print("   constant block (1000) -> kernel: %s" % [cX[k][0] for k in range(N)])
print("   constant block (1000) -> row-sum prediction: %s (C row sums %s)" % ([cpred[k][0] for k in range(N)],
                                                                             [sum(C[k]) for k in range(N)]))
check("T4 DCT-II", off < 2 * 32768 and err <= bound and cX == cpred and C != transpose(C),
      "Q15 DCT-II: orthonormal to <2 LSB, a constant block renders into row 0 only, and the kernel's two"
      " passes reconstruct the block within the derived truncation bound")

# ------------------------------------------------------------------ T5 the write-back
wb_ok = all(eq(sim(coef, block, s), sim(coef, block, s, writeback=True)) for s in (0, 7, 16))
check("T5 write-back", wb_ok,
      "SRCMB's read-modify-write of QACC changes nothing here (expected): every output row starts with"
      " EE.ZERO.QACC, so the stale shifted value is discarded")

print()
print("ALL CHECKS:", "PASS" if all(results) else "FAIL", "(%d/%d)" % (sum(results), len(results)))
```

Real run (the script was saved to `/tmp/ex11_model.py`; it needs nothing but the Python standard library):

```
$ python3 /tmp/ex11_model.py
T1 random      : PASS  200/200 cases agree (naive vs kernel datapath, shifts 0..20), 12800 elements
   worst-case |sum| over 8 taps of two full-scale int16 (Q15 x Q15): 8 * 32768^2 = 8589934592 = 2**33
   40-bit accumulator: [-549755813888, 549755813887] -> headroom factor 64 (6 bits), so no single 8-tap row can saturate it
   identity, shift  0: out == block >> 0 (sat16) == True; naive agrees: True
   identity, shift  8: out == block >> 8 (sat16) == True; naive agrees: True
   identity, shift 15: out == block >> 15 (sat16) == True; naive agrees: True
T2 identity    : PASS  out[k][j] == block[k][j]: rows and lanes land where the contract says
   two-pass S, shifts 15/15: kernel datapath == naive reference over 64 elements: True
   two-pass S, shifts 15/18: kernel datapath == naive reference over 64 elements: True
   S row sums = [262136, 32767, 0, 32767] (rows 0..3); constant block -> every output row constant across the eight lanes: True
   constant block (7) render, from the row sums: [55, 6, 0, 6, 0, 6, 0, 6]
   constant block (7) render, from the kernel model: [55, 6, 0, 6, 0, 6, 0, 6]
T3 symmetric   : PASS  S == S^T (one 64-word table for both axes); kernel datapath == naive reference through both transpose passes; and the constant-block render equals the row-sum prediction exactly
   C is orthonormal in Q15: diag(C*C^T) = [1073697800, 1073719420, 1073766676] (2^30 = 1073741824), worst |off-diagonal| = 37698 = 1.15 LSB
   C == C^T (symmetric)? False -> the inverse pass needs the transposed table (C_III), unlike S
   C row sums = [92680, 0, 0, 0] (rows 0..3) -> a constant block renders into row 0 only, and rows 1..7 sum to exactly 0
   kernel round trip X=(C*B)>>15 then Y=(C^T*X)>>15: max |Y-B| = 3, truncation bound = 5 (8 * max|C| / 2^15 + 1; `>>` floors, it does not round)
   B[0] = [-55, -64, 45, 56, 25, 53, -43, 47]
   X[0] = [-34, -36, -4, 36, 41, -19, -47, 55]  (DC row)
   Y[0] = [-57, -65, 43, 54, 24, 51, -45, 44]
   constant block (1000) -> kernel: [2828, 0, 0, 0, 0, 0, 0, 0]
   constant block (1000) -> row-sum prediction: [2828, 0, 0, 0, 0, 0, 0, 0] (C row sums [92680, 0, 0, 0, 0, 0, 0, 0])
T4 DCT-II      : PASS  Q15 DCT-II: orthonormal to <2 LSB, a constant block renders into row 0 only, and the kernel's two passes reconstruct the block within the derived truncation bound
T5 write-back  : PASS  SRCMB's read-modify-write of QACC changes nothing here (expected): every output row starts with EE.ZERO.QACC, so the stale shifted value is discarded

ALL CHECKS: PASS (5/5)
```

What the five checks are worth:

* **T1** is the plain equivalence of the SIMD datapath and the scalar contract on 12800 elements with
  full-scale Q15 coefficients — the test that catches a lane map or a truncation that differs (the kernel
  shifts with `>>`, i.e. floor, not round; the C reference and the model use the same `>>`).
* **T2** is the identity as a *mapping* test: `out == block` after a shift of 0 can only hold if row `k` is
  broadcast from `coef` lane `i`, the lanes really are the block's columns, and the store puts lane `j` back
  into `out[k][j]`. A transposed or lane-rotated kernel fails it.
* **T3** is the symmetric matrix: `S[k][i] = round(32767*cos(pi*k*i/8))` satisfies `S == S^T`, so one 64-word
  table serves both axes of a separable transform. The kernel datapath agrees with the naive reference
  through both transpose passes (with saturation in the middle), and with a constant input every output row
  is constant across the eight lanes and equal to the row-sum prediction `sat16((sum_i S[k][i]) * 7 >> 15)`
  exactly — `[55, 6, 0, 6, 0, 6, 0, 6]`, i.e. this DCT-I-style kernel puts DC energy into rows 0, 1, 3, 5, 7
  (their row sums are non-zero), which is a property of the table, not of the implementation.
* **T4** is the real codec matrix: the Q15 normalized DCT-II is orthonormal in Q15
  (`C*C^T = 2^30*I` to within 1.15 LSB), `C != C^T` so the inverse pass needs the transposed table, a
  constant block renders into row 0 only, and the kernel's two passes reconstruct the input within 3 LSB
  (derived truncation bound 5). Note **`>>` floors**: a round-to-nearest pass-1 (`+ (1 << 14)` before the
  shift) would tighten this — that is a caller-side choice, not a kernel one.
* **T5** is the `SRCMB` write-back: `SRCMB` stores its shifted result back into `QACC_H`/`QACC_L` (the TRM
  Operation says so, and ex09's device run agreed: `srcmb_verdict read_modify_write`). Because every output
  row begins with `EE.ZERO.QACC`, that write-back cannot leak into the next row — the model reproduces it and
  the result is identical either way. Removing the `ZERO.QACC` for speed would make it *not* identical.

## Assembly evidence

The exact required command, the real output (nothing — and it is worth saying that this is the *second*
version: the first one needed 10 live 128-bit registers and was rejected):

```
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex11.o examples/firmware/main/proposed/ex11_block8x8.S
(no output, exit status 0)          # assembler and preprocessor silent, no warnings
$ xtensa-esp32s3-elf-nm -S /tmp/ex11.o
00000000 00000056 T ex11_block8x8
```

**Finding (recorded because it shapes the kernel): the ESP32-S3 PIE has eight 128-bit registers, `q0..q7`.**
The first draft kept one register per tap, the ex07 way (coef in `q0`, the eight block rows in `q1..q8`, the
readout in `q9`) and the assembler refused:

```
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex11.o examples/firmware/main/proposed/ex11_block8x8.S
examples/firmware/main/proposed/ex11_block8x8.S: Assembler messages:
examples/firmware/main/proposed/ex11_block8x8.S:78: Error: register number out of range
examples/firmware/main/proposed/ex11_block8x8.S:79: Error: register number out of range
examples/firmware/main/proposed/ex11_block8x8.S:84: Error: register number out of range
examples/firmware/main/proposed/ex11_block8x8.S:85: Error: register number out of range
exit=1
```

`q8` and `q9` are the out-of-range ones (a one-line probe with `EE.VSMULAS.S16.QACC q8, q0, 3` reproduces it
while `q7` passes). ex07's four taps fit in `q1..q4`; eight taps do not. The version in the file therefore
frees each row register eight instructions after its MAC — a **WAR**, not a RAW, hazard: the load for row 4
only overwrites `q1` after four load/MAC pairs have issued, and the pipeline issues in order, so row 0's MAC
has already read its operand. The `nop.n` slots guard the other side (the accumulator read, which *is* a RAW).

Disassembly of the shipped version, `xtensa-esp32s3-elf-objdump -d /tmp/ex11.o`:

```
Disassembly of section .iram1:

00000000 <ex11_block8x8>:
   0:	004136       	entry	a1, 32
   3:	05cd     	mov.n	a12, a5
   5:	036d     	mov.n	a6, a3
   7:	870c     	movi.n	a7, 8
   9:	250844       	ee.zero.qacc
   c:	063d     	mov.n	a3, a6
   e:	830124       	ee.vld.128.ip	q0, a2, 16
  11:	838134       	ee.vld.128.ip	q1, a3, 16
  14:	8e21c4       	ee.vsmulas.s16.qacc	q1, q0, 0
  17:	930134       	ee.vld.128.ip	q2, a3, 16
  1a:	8ea2c4       	ee.vsmulas.s16.qacc	q2, q0, 1
  1d:	938134       	ee.vld.128.ip	q3, a3, 16
  20:	9e23c4       	ee.vsmulas.s16.qacc	q3, q0, 2
  23:	a30134       	ee.vld.128.ip	q4, a3, 16
  26:	9ea4c4       	ee.vsmulas.s16.qacc	q4, q0, 3
  29:	838134       	ee.vld.128.ip	q1, a3, 16
  2c:	ae21c4       	ee.vsmulas.s16.qacc	q1, q0, 4
  2f:	930134       	ee.vld.128.ip	q2, a3, 16
  32:	aea2c4       	ee.vsmulas.s16.qacc	q2, q0, 5
  35:	938134       	ee.vld.128.ip	q3, a3, 16
  38:	be23c4       	ee.vsmulas.s16.qacc	q3, q0, 6
  3b:	a30134       	ee.vld.128.ip	q4, a3, 16
  3e:	bea4c4       	ee.vsmulas.s16.qacc	q4, q0, 7
  41:	f03d     	nop.n
  43:	f03d     	nop.n
  45:	f03d     	nop.n
  47:	f03d     	nop.n
  49:	edf2c4       	ee.srcmb.s16.qacc	q5, a12, 0
  4c:	aa8144       	ee.vst.128.ip	q5, a4, 16
  4f:	770b     	addi.n	a7, a7, -1
  51:	fb4756       	bnez	a7, 9 <ex11_block8x8+0x9>
  54:	f01d     	retw.n
```

32 instructions, 86 bytes; the loop body (0x9..0x54) is 27 instructions / 75 bytes and the branch target is
the `EE.ZERO.QACC` at 0x9. Note the assembler relaxed `mov`/`movi`/`addi` to the 16-bit `.n` forms (that is
why the average is under 3 bytes per instruction) and that each `EE.*` is a single 24-bit word — one issue
slot each.

## Instruction sources (`data/pie_instructions.json`)

Every PIE instruction the kernel uses, with the manual page the repo's extraction recorded:

```
$ python3 - <<'PY'
import json
d = json.load(open('data/pie_instructions.json'))
used = ['EE.VLD.128.IP','EE.VSMULAS.S16.QACC','EE.SRCMB.S16.QACC','EE.VST.128.IP','EE.ZERO.QACC']
for n in used:
    e = [x for x in d if x['name'] == n][0]
    print('%-22s source_page=%-4d sections=%s  syntax=%s' % (n, e['source_page'], e['sections_page_range'], e['assembler_syntax']))
print('native opcodes in the file:', [n for n in ('NOP.N','ENTRY','RETW.N','MOV','MOVI','ADDI','BNEZ') if any(x['name']==n for x in d)] or 'none -- pie_instructions.json is the PIE chapter list (220 entries)')
PY
EE.VLD.128.IP          source_page=164  sections=[164, 165]  syntax=EE.VLD.128.IP qu, as, -2048..2032
EE.VSMULAS.S16.QACC    source_page=269  sections=[269, 270]  syntax=EE.VSMULAS.S16.QACC qx, qy, sel8
EE.SRCMB.S16.QACC      source_page=130  sections=[130, 131]  syntax=EE.SRCMB.S16.QACC qu, as, 0
EE.VST.128.IP          source_page=275  sections=[275, 276]  syntax=EE.VST.128.IP qv, as, -2048..2032
EE.ZERO.QACC           source_page=300  sections=[300, 301]  syntax=EE.ZERO.QACC
native opcodes in the file: none -- pie_instructions.json is the PIE chapter list (220 entries)
```

Quoted from the same file, the two pages the arithmetic rests on:

* **p269, `EE.VSMULAS.S16.QACC`** — `temp[15:0] = qy[sel8*16+15:sel8*16]` then
  `QACC_L[39:0] = min(max(QACC_L[39:0] + qx[15:0] * temp[15:0], -2^39), 2^39-1)`, … eight times, lanes 0..3
  in QACC_L and 4..7 in QACC_H. That is the broadcast-and-MAC the kernel's `sel8 = 0..7` sequence is built
  from, one tap per MAC.
* **p130, `EE.SRCMB.S16.QACC`** — `temp_shfN[39:0] = tempN[39:0] >> as[5:0]`, the shifted value written back
  into QACC_L/QACC_H, and `qu[15:0] = min(max(temp_shf0[39:0], -2^15), 2^15-1)` … for all eight lanes: the
  `>>shift` + int16 saturate readout.
* **p164 / p275** (`EE.VLD.128.IP` / `EE.VST.128.IP`) — `qu[127:0] = load128({as[31:4],4{0}})`,
  `as += {20{imm16[7]},imm16[7:0],4{0}}`: the 16-byte post-increment walk and the alignment rounding.
* **p300, `EE.ZERO.QACC`** — `QACC_L = 0; QACC_H = 0`.

The **native** instructions in the file (`entry`, `mov.n`, `movi.n`, `addi.n`, `bnez`, `retw.n`, `nop.n`)
are not in `data/pie_instructions.json` — that file is the PIE chapter's instruction list (220 entries), so
there is no `source_page` to quote for them from it. What the repo does record for them is Table 1.7-2
(`data/pie_hazards.md`, printed pages 65–75 of TRM v1.8, verbatim), which is where the stall model comes
from. For the accumulator pair that table says:

```
EE.VSMULAS.S16.QACC   | qx 1, qy 1 | — | QACC_H 2, QACC_L 2 | QACC_H 2, QACC_L 2
EE.SRCMB.S16.QACC     | as 1 | qu 1 | QACC_H 1, QACC_L 1 | QACC_H 1, QACC_L 1
```

i.e. on paper the MAC's accumulator write lands in stage 2 (M) and the readout reads QACC in stage 1 (E), a
one-cycle separation. The kernel spends four `nop.n` slots instead: the accumulator's write-to-read behaviour
is precisely what ex07/ex09 found the static model does not get right, so the safe value is taken from the
probe (`ex09_qacc.S`, `chain_g4`) rather than from the table.

## What is not confirmed (and what ex11 still needs)

* **The QACC path itself.** ex07 (the same `VSMULAS` + `SRCMB` pair) does **not** reproduce its model on the
  device: run `pie-examples-20260914T181234Z.log` has `all_32_transformed_coordinates_match_C pie=19
  ref=32 FAIL`, the next run `pie=9`, and ex09's whole QACC probe fails at every gap 0..7
  (`mac1_min_gap=-1`, values that are not the model at any gap) — i.e. the readout separation is *not*
  actually pinned down by the current device output, and this kernel's 4-slot gap is the suite's convention,
  not a measurement this file can cite as settled. Until that is resolved, `ex11_block8x8` is written from the
  manual's pseudo-code with the suite's conventions and modelled in Python; it has **not** been run on
  silicon.
* **Cycles.** The cost above is instruction/slot counting from the disassembly. No `BENCH` line exists for
  ex11 because it is not wired into `main.c`, and the count is not a cycle measurement (`EE.VSMULAS`,
  `EE.VLD` and `EE.SRCMB` have their own pipeline behaviours, and the in-order single-issue issue rate is an
  assumption).
* **Not done on purpose** (the task forbade touching existing files): the declaration in `examples.h`, the
  `ex11()` harness in `main.c` (deterministic input → kernel → `ex11_block8x8_c` → `CHECK` → `BENCH` →
  `DATA`), the `check_ex11()` in `tools/check_examples_log.py`, the mutation entry in
  `tools/selftest_examples_checker.py`, registration in `examples/firmware/main/CMakeLists.txt` (the
  `proposed/` directory is not in the build; only `ex11_block8x8.S` is new, nothing existing was modified),
  and the README/notes 08 table updates.
* **A faster shape exists.** `EE.VSMULAS.S16.QACC.LD.INCP` (p270) folds the 16-byte load into the MAC and
  walks `as` by 16 per instruction; software-pipelined (`qu` = next row, `qx` = current row) that is 19
  instructions per output row instead of 27 (2.375 per element, 152 per block) and two live vector registers
  instead of five. It is not used here because it is a different hazard profile (the streamed register is
  written by one instruction and read by the next) and it has no device evidence at all yet.
