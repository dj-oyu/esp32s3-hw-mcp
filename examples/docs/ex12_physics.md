# ex12 — 物理と衝突（PIE で 8 レーンずつ） / physics and collision, eight lanes at a time

> **状態（統合後）**: この3カーネルは `examples/firmware/main/ex12_physics.S` に移り、
> `examples/firmware/main/CMakeLists.txt` のビルドに入って `main.c` の `ex12()` から呼ばれる。
> ビルドとホスト側チェッカー（`tools/selftest_examples_checker.py`）は通っているが、**実機では未実行**。
> 本文が `proposed/` のパスや「ビルドに入っていない」と書いているのは移動前の状態のままで、貼ってある
> 実行記録も当時のまま（.S は移動以外は一字も変えていない）。

`examples/firmware/main/proposed/ex12_physics.S` — **new sample, not in the build**. `main.c`,
`examples.h` and `CMakeLists.txt` are untouched (this directory is not compiled by
`examples/firmware/main/CMakeLists.txt`), so nothing in the running firmware changes until someone moves
the file next to `ex08_media.S` and adds the `ex12()` section described at the bottom.

Three kernels, each shaped like a loop a 2D/3D game actually calls every frame:

| kernel | shape | registers |
|---|---|---|
| `ex12_integrate` | semi-implicit Euler position update + clamp, **8 objects per call** | 4 × int32 lanes |
| `ex12_sat_masks` | AABB / separating-axis overlap test of 8 candidate boxes against 1 query box | 8 × int16 lanes |
| `ex12_dist2_qacc` | squared distance of 8 point pairs, accumulated in QACC (no square root, no divide) | 8 × int16 lanes → 8 × int40 lanes |
| `ex12_dist2_q16` | the same accumulation with the one-instruction QACC readout | 8 × int16 lanes |

Functions defined in the file: `ex12_integrate`, `ex12_sat_masks`, `ex12_dist2_qacc`, `ex12_dist2_q16`.

## 何が確認済みで、何が未確認か (status, honestly)

* **Assembled**: yes — `xtensa-esp32s3-elf-gcc -c` with the ESP-IDF v6.0.1 toolchain, no warnings, no
  literal pool, no `l32r` (transcript below).
* **Encodings**: every PIE instruction used is re-checked against the manual's instruction-word diagram
  with the repo's own `tools/asm_toolchain.py --check-instruction` — **15 of 15 emitted, all `match`**
  (plus `EE.LDQA.S16.128.IP`, cited for the QACC lane layout and not emitted; table below).
* **Behaviour**: **model vs reference on the host, not silicon.** The Python model in this file is an
  instruction-level model of the sequence in the `.S` (lane lists, the saturating ops, the 40-bit QACC
  packing, the RUR word order), and it is compared against the `ex12_*_c` scalar code in C compiled with
  the host gcc, over 700 random cases (2400 int32 lanes, 228 eight-box groups + 704 tail boxes, 2374
  point pairs). Result: **0 mismatches** (output pasted below).
* What that does *not* prove: the model is a second reading of my own assembly, so it can only catch the
  `.S` disagreeing with the scalar formula — it cannot say what the silicon does. It did catch four real
  discrepancies while this file was being written, three of them in the *reference* code (all listed at
  the bottom); the fourth was my own first model of the query broadcast. The unconfirmed *hardware*
  semantics this kernel leans on are listed explicitly in "未確認の前提" below — nothing there is guessed
  silently.

```bash
# assemble (the exact command, output pasted verbatim)
$ xtensa-esp32s3-elf-gcc -c -o /tmp/ex12.o examples/firmware/main/proposed/ex12_physics.S
rc=0

$ xtensa-esp32s3-elf-nm --print-size /tmp/ex12.o
00000234 00000041 T ex12_dist2_q16
00000100 00000133 T ex12_dist2_qacc
00000000 00000041 T ex12_integrate
00000044 000000bc T ex12_sat_masks

$ xtensa-esp32s3-elf-objdump -h /tmp/ex12.o | grep -E 'Idx|iram1|literal'
Idx Name          Size      VMA       LMA       File off  Algn
  3 .iram1        00000275  00000000  00000000  00000034  2**2

$ xtensa-esp32s3-elf-objdump -d /tmp/ex12.o | grep -c l32r   # constants from .rodata?
0

```

## A. `ex12_integrate` — pos += vel + acc, clamped

```c
void ex12_integrate(const int32_t *pos, const int32_t *vel, const int32_t *acc, int32_t *out,
                    int32_t lo, int32_t hi);
/* out[0..7] = clamp(sat32(sat32(pos[i] + vel[i]) + acc[i]), lo, hi)     i = 0..7 */
```

* **Data**: four arrays of 8 int32, each 16-byte aligned. No count argument: exactly eight objects per
  call, i.e. two four-lane register groups. `vel` and `acc` are per-step terms already scaled by `dt`;
  the "semi-implicit" half of the name (update `v` first, then `x`) is the *caller's* ordering — this
  kernel only does the position update. There is **no 32-bit vector multiply and no divide** in the set,
  so an acceleration term cannot be scaled here: the caller hands in `acc` already multiplied (or keeps
  the state in 16-bit Q-format and scales it with `EE.VMUL.S16`).
* **Why it is built the way it is**: the set has no plain `VADD.S32`, no `SUB.S32` at all, and the only
  vector adds are the saturating `EE.VADDS.S32`. So `pos += vel + acc` becomes two `VADDS.S32`, then the
  `[lo,hi]` clamp as `VMIN.S32` (against a broadcast `hi`) followed by `VMAX.S32` (against `lo`).
* **Broadcasting the two scalar arguments**: there is no AR→QR move instruction, so `lo`/`hi` are spilled
  with `s32i` into the 32-byte frame that `entry a1, 32` already allocated and read back with
  `EE.VLDBC.32` (broadcast load). Constants never come from `.rodata` — an `l32r` from `.iram1` cannot
  reach it.
* **Cost (measured from the object file)**: 24 instructions per call = **3.0 per object**, of which 18
  are PIE ops (**2.25 per object**: per four-lane group 3 loads + 2 saturating adds + 1 `VMIN` + 1 `VMAX`
  + 1 store). Straight line, no loop.
* **Where it saturates (numeric bound)**: `EE.VADDS.S32` (TRM p149) clamps each 32-bit lane at
  **+2^31-1 = 2147483647 / -2^31 = -2147483648**. Therefore
  * for every lane with **|pos[i] + vel[i]| ≤ 2147483647** the kernel equals the 64-bit exact
    `clamp(pos+vel+acc, lo, hi)` — this is the caller's contract, and the check found **0 divergences in
    1993 in-domain lanes**;
  * outside that bound the first `VADDS` saturates and the kernel computes
    `clamp32(sat32(2147483647 + acc))` instead of `clamp(s + acc)`, which can differ: **51 of the 407
    out-of-range lanes** in the run. Worst case seen: `pos = vel = 2147483647, acc = -1,
    lo = -2^31, hi = 2^31-1` → kernel `2147483646`, exact `2147483647`.
  * the *second* `VADDS` saturating is harmless once the clamp has run: if it hits +2^31-1 the true value
    is ≥ 2^31-1 ≥ `hi`, so both readings give `hi` (mirror argument for -2^31 and `lo`) — **as long as the
    caller passes lo ≤ hi**. If it does not, `VMAX` runs last and `lo` wins.
* Lanes that can overflow: any lane whose `pos+vel` leaves int32 — the bound above is the whole story.
  The loads themselves are exact (a full 128-bit load per four lanes).

## B. `ex12_sat_masks` — the separating-axis test as a mask

```c
void ex12_sat_masks(const int16_t *boxes, const int16_t *query, int16_t *out, uint32_t n_boxes);
/* out[b] = 0xFFFF (all ones) if box b overlaps the query box on all three axes, 0 if any axis separates */
```

**Data layout (the caller has to get exactly this right).** `boxes` is plane-major, eight boxes per group
of six 16-byte planes:

```
group g (8 boxes): +0 min_x[8]  +16 max_x[8]  +32 min_y[8]  +48 max_y[8]  +64 min_z[8]  +80 max_z[8]
element (plane p, box i) is at byte offset  96*(i/8) + 16*p + 2*(i%8)
                        = int16 index       48*(i/8) +  8*p +    (i%8)
```

* `query`: six int16 in the same order — `min_x, max_x, min_y, max_y, min_z, max_z`. Two-byte aligned is
  enough (`EE.VLDBC.16` forces the low bit of the address to 0; anything coarser silently reads the
  wrong scalar).
* `out`: exactly `n_boxes` int16, **16-byte aligned** (the vector loop writes whole groups of eight with
  one `EE.VST.128.IP`; the `n_boxes % 8` remainder is written as single int16 by the scalar tail, so no
  padding lanes are ever read or written).
* **Fixed point**: the values are 16-bit signed (Q8 in the game's units, i.e. 1 world unit = 256) — but
  the test only depends on the *order* of the numbers, so the same kernel serves Q8, Q15 or raw pixel
  coordinates. A 2D game reuses it unchanged by passing `min_z = -32768, max_z = 32767`: the z axis then
  can never separate (verified: 53 such calls, none separated by z).
* **The test**: on each axis, `separated ⟺ (box_max < query_min) ∨ (box_min > query_max)`. Each half is
  one `EE.VCMP.LT.S16` / `EE.VCMP.GT.S16` against a broadcast query scalar (0xFFFF per lane if true);
  `EE.ORQ` accumulates the separation bits across the two halves and then across the axes (De Morgan's
  rule — `¬a ∧ ¬b = ¬(a ∨ b)`), and a single `EE.NOTQ` at the end turns "no separation anywhere" into the
  all-ones overlap mask. Using one `NOTQ` per group instead of two per axis is why the axis loop costs
  three PIE ops and not five.
* **Cost (measured)**: the group body is **32 instructions per eight boxes = 4.0 per box**, of which 25
  are PIE ops (**3.125 per box**: 6 `VLD.128` + 6 `VLDBC.16` + 6 `VCMP` + 5 `ORQ` + 1 `NOTQ` + 1 `VST`)
  and 7 are scalar (6 `addi` for the query offsets, 1 `addi` + 1 `bnez` for the loop). A 2D caller that
  folds z away drops 8 of those PIE ops per group (2.1 per box). The scalar tail is 21 instructions per
  box, and is only entered for `n_boxes % 8 ≠ 0`.
* **Where it saturates**: **nowhere.** `VCMP`/`ORQ`/`NOTQ` are pure bitwise logic and the compares are
  exact on 16-bit signed integers — there is no arithmetic in this kernel at all, so there is no numeric
  bound to state beyond "the inputs are int16 and the comparisons are signed 16-bit". The result is exact
  for every input that satisfies the layout contract; a `min > max` input is not saturated, it is simply
  a nonsense box. (The one thing this kernel does depend on is that `EE.VCMP.LT/GT.S16` really is a
  **signed** compare — see the unconfirmed list.)

## C. `ex12_dist2_qacc` — squared distance in eight lanes

```c
void ex12_dist2_qacc(const int16_t *pt_a, const int16_t *pt_b, int32_t *out, uint32_t n_points);
/* out[i] = min(dx*dx + dy*dy + dz*dz, 2^31-1)      dx = sat16(a.x - b.x), dy = ..., dz = ... */

uint32_t ex12_dist2_q16(const int16_t *pt_a, const int16_t *pt_b, int16_t *out, uint32_t n_points,
                        uint32_t shift);
/* out[i] = sat16(dist2[i] >> shift) for the whole groups of eight; returns the pairs written ((n/8)*8) */
```

**Data layout** (both point arrays, same shape): plane-major groups of eight points,

```
group g:  +0 x[8]   +16 y[8]   +32 z[8]                        (48 bytes per group)
element (axis ax, point i) at byte offset  48*(i/8) + 16*ax + 2*(i%8)
                           = int16 index   24*(i/8) +  8*ax +    (i%8)
```

`out` is int32 per point, 16-byte aligned (written as two four-lane stores per group).

* **Arithmetic**: three `EE.VSUBS.S16` (one per axis, saturating at ±2^15) and three
  `EE.VMULAS.S16.QACC`, each squaring its lane and adding it into that lane's own 40-bit accumulator — so
  after the third MAC each QACC lane holds `dx²+dy²+dz²` for one pair. The game compares that against r²;
  there is no square root instruction and no divide.
* **Readout (the interesting half)**: `RUR.QACC_L_0..4` / `RUR.QACC_H_0..4` hand out the two 160-bit
  accumulators as five 32-bit AR words each (TRM p64), and the eight lanes are bit-packed at 40-bit
  offsets (the same layout is stated by `EE.LDQA.S16.128.IP`'s pseudo-code, TRM p105). Only the **low 32
  bits** of each lane are taken. That is not an approximation: because the difference comes out of a
  saturating 16-bit subtract, |dx| ≤ 32767 for *every* input, so
  `dist2 ≤ 3·32767² = 3221028867 < 2^32` — bits 32..39 of every lane are zero by construction (0
  violations over 2374 pairs in the run). The value is then unsigned-clamped to 2^31-1 with `minu` so it
  fits the int32 output.
* **Cost (measured)**: the group body is **68 instructions per eight pairs = 8.5 per pair**, split
  13 PIE ops (1.6 per pair: 1 `ZERO.QACC` + 6 `VLD.128` + 3 `VSUBS` + 3 `VMULAS`), 10 `RUR` (1.25 per
  pair) and 45 scalar (5.6 per pair: per lane 2 shifts + 1 or where the lane straddles two words, a load
  of the clamp ceiling, an unsigned min, a store). The Q16 variant does the same arithmetic with a
  one-instruction readout: **17 instructions per group = 2.1 per pair** (15 PIE = 1.9 per pair). The
  honest reading of that table is that **the exact int32 readout, not the multiply-accumulate, is what
  this primitive costs** — a game that can live in a Q-format radius should use `ex12_dist2_q16`.
  These are *instruction counts*, not cycles: the pipeline tables have no entry for the QACC accumulator
  or for `RUR` (see `notes/08` and the unconfirmed list).
* **Where it saturates (numeric bound)**:
  * `EE.VSUBS.S16`: every axis with **|a-b| > 32767** is clamped to ±32767. The result is the true
    squared distance **iff |a-b| ≤ 32767 on all three axes**; beyond that the kernel returns the squared
    distance of the clamped difference, which is smaller than the truth (bounded by 3·32767²). The run
    saturated 370 of 7122 axis differences (max |a-b| seen 65030).
  * `EE.VMULAS.S16.QACC`'s 40-bit saturation is **unreachable here**: 3·32767² = 3221028867 is far below
    2^39-1 = 549755813887, and the saturating subtract already bounds the input.
  * the int32 clamp fires for **dist2 ≥ 2^31 = 2147483648** (992 lanes in the run). Clamping to
    2^31-1 **preserves the "is this point within r²" test for every r² ≤ 2^31-1**: if the true dist2
    exceeds 2^31-1 ≥ r² the clamped value is also ≥ r² (test still false), and if it does not, nothing was
    clamped.
  * `ex12_dist2_q16` saturates at ±32767 in 16 bits, so it is exact while **dist2 ≤ 32767 << shift**; with
    the usual `shift = 8` that is dist2 ≤ 8388352, i.e. |Δ| ≤ 1672 on all three axes (2048 in 2D). Note
    the resolution trap: comparing shifted values is monotone (dist2 ≤ r² ⟹ (dist2>>s) ≤ (r²>>s)) but the
    converse can fail inside one 2^s bucket, so the Q16 test can pull in points up to 2^s-1 of squared
    distance outside r². The exact kernel has no such bucket.
  * the scalar tail (n_points % 8) reproduces the **same saturating 16-bit difference** by an explicit
    min/max clamp, and the same unsigned clamp at the end, so the two paths agree even outside the
    documented range (checked on 726 tail pairs).

## 命令ごとの出典 (per instruction: page and quote)

Generated from `data/pie_instructions.json` (each `source_page` is where the manual's instruction
section starts) with the encoding cross-check from `tools/asm_toolchain.py --check-instruction`
(`manual_word == toolchain_word` means the assembler and the manual's bit diagram agree):

| instruction | TRM page | assembler syntax | Operation (quoted) | manual vs toolchain encoding |
|---|---|---|---|---|
| `EE.NOTQ` | p120 | `EE.NOTQ qa, qx` | qa = ~qx | match, manual edffc4 == toolchain edffc4 |
| `EE.ORQ` | p121 | `EE.ORQ qa, qx, qy` | qa = qx \| qy | match, manual edfce4 == toolchain edfce4 |
| `EE.SRCMB.S16.QACC` | p130 | `EE.SRCMB.S16.QACC qu, as, 0` | temp0[39:0] = QACC_L[ 39: 0]; ...; ... | match, manual edf264 == toolchain edf264 |
| `EE.VADDS.S32` | p149 | `EE.VADDS.S32 qa, qx, qy` | qa[ 31: 0] = min(max(qx[ 31: 0] + qy[ 31: 0], -2^{31}), 2^{31}-1); qa[ 63: 32] = min(max(qx[ 63: 32] + qy[ 63: 32], -2^{31}), 2^{31}-1); ... | match, manual aede74 == toolchain aede74 |
| `EE.VCMP.GT.S16` | p158 | `EE.VCMP.GT.S16 qa, qx, qy` | qa[ 15: 0] = (qx[ 15: 0]>qy[ 15: 0]) ? 0x{}FFFF :; qa[ 31: 16] = (qx[ 31: 16]>qy[ 31: 16]) ? 0x{}FFFF :; ... | match, manual aedec4 == toolchain aedec4 |
| `EE.VCMP.LT.S16` | p161 | `EE.VCMP.LT.S16 qa, qx, qy` | qa[ 15: 0] = (qx[ 15: 0]<qy[ 15: 0]) ? 0x{}FFFF :; qa[ 31: 16] = (qx[ 31: 16]<qy[ 31: 16]) ? 0x{}FFFF :; ... | match, manual aedef4 == toolchain aedef4 |
| `EE.VLD.128.IP` | p164 | `EE.VLD.128.IP qu, as, -2048..2032` | qu[127:0] = load128({as[31:4],4{0}}); as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}} | match, manual e38064 == toolchain e38064 |
| `EE.VLDBC.16` | p170 | `EE.VLDBC.16 qu, as` | qu[127:0] = {8{load16({as[31:1],1{0}})}} | match, manual edf364 == toolchain edf364 |
| `EE.VLDBC.32` | p173 | `EE.VLDBC.32 qu, as` | qu[127:0] = {4{load32({as[31:2],2{0}})}} | match, manual edf764 == toolchain edf764 |
| `EE.VMAX.S32` | p183 | `EE.VMAX.S32 qa, qx, qy` | qa[ 31: 0] = (qx[ 31: 0]>=qy[ 31: 0]) ? qx[ 31: 0] : qy[ 31: 0]; qa[ 63: 32] = (qx[ 63: 32]>=qy[ 63: 32]) ? qx[ 63: 32] : qy[ 63: 32]; ... | match, manual aefe34 == toolchain aefe34 |
| `EE.VMIN.S32` | p192 | `EE.VMIN.S32 qa, qx, qy` | qa[ 31: 0] = (qx[ 31: 0]<=qy[ 31: 0]) ? qx[ 31: 0] : qy[ 31: 0]; qa[ 63: 32] = (qx[ 63: 32]<=qy[ 63: 32]) ? qx[ 63: 32] : qy[ 63: 32]; ... | match, manual aefe64 == toolchain aefe64 |
| `EE.VMULAS.S16.QACC` | p215 | `EE.VMULAS.S16.QACC qx, qy` | QACC_L[ 39: 0] = min(max(QACC_L[ 39: 0] + qx[ 15: 0] * qy[ 15: 0], -2^{39}), 2^{39}-1); QACC_L[ 79: 40] = min(max(QACC_L[ 79: 40] + qx[ 31: 16] * qy[ 31: 16], -2^{39}), 2^{39}-1); ... | match, manual 1a7584 == toolchain 1a7584 |
| `EE.VST.128.IP` | p275 | `EE.VST.128.IP qv, as, -2048..2032` | qv[127:0] => store128({as[31:4],4{0}}); as[31:0] = as[31:0] + {20{imm16[7]},imm16[7:0],4{0}} | match, manual ea8064 == toolchain ea8064 |
| `EE.VSUBS.S16` | p281 | `EE.VSUBS.S16 qa, qx, qy` | qa[ 15: 0] = min(max(qx[ 15: 0] - qy[ 15: 0], -2^{15}), 2^{15}-1); qa[ 31: 16] = min(max(qx[ 31: 16] - qy[ 31: 16], -2^{15}), 2^{15}-1); ... | match, manual aefed4 == toolchain aefed4 |
| `EE.ZERO.QACC` | p300 | `EE.ZERO.QACC` | QACC_L =; QACC_H = 0 | match, manual 250844 == toolchain 250844 |
| `EE.ANDQ *(cited for the lane layout, not emitted here)*` | p76 | `EE.ANDQ qa, qx, qy` | qa = qx & qy | match, manual edbce4 == toolchain edbce4 |
| `EE.VSUBS.S32 *(cited for the lane layout, not emitted here)*` | p284 | `EE.VSUBS.S32 qa, qx, qy` | qa[ 31: 0] = min(max(qx[ 31: 0] - qy[ 31: 0], -2^{31}), 2^{31}-1); qa[ 63: 32] = min(max(qx[ 63: 32] - qy[ 63: 32], -2^{31}), 2^{31}-1); ... | match, manual aefee4 == toolchain aefee4 |
| `EE.LDQA.S16.128.IP *(cited for the lane layout, not emitted)*` | p105 | `EE.LDQA.S16.128.IP as, -2048..2032` | dataIn[127:0] = load128({as[31:4],4{0}}); QACC_L[ 39: 0] = {24{dataIn[15]}, dataIn[ 15: 0]}; ... | match, manual 410054 == toolchain 410054 |

RUR forms used (not PIE instructions, so absent from data/pie_instructions.json): `RUR.QACC_H_0`, `RUR.QACC_H_1`, `RUR.QACC_H_2`, `RUR.QACC_H_3`, `RUR.QACC_H_4`, `RUR.QACC_L_0`, `RUR.QACC_L_1`, `RUR.QACC_L_2`, `RUR.QACC_L_3`, `RUR.QACC_L_4`

count of EE.* distinct emitted: 15 + 1 cited: 16

`RUR.QACC_L_*` / `RUR.QACC_H_*` are not PIE instructions, so they are absent from
`data/pie_instructions.json`; their only mention in the corpus is TRM p64 ("QACC_H and QACC_L registers
realize data transfer via the five AR registers"), which is what the readout above relies on.

## 実測コスト (measured instruction cost, from the object file)

```
ex12_integrate: 24 instructions total, 18 of them EE.* PIE ops
    straight-line, no loops
ex12_sat_masks: 66 instructions total, 25 of them EE.* PIE ops
    loop body 0x004d..0x00a7: 32 instructions (25 EE.* PIE + 0 RUR, rest scalar/branch)
    loop body 0x00c4..0x00fb: 21 instructions (0 EE.* PIE + 0 RUR, rest scalar/branch)
ex12_dist2_qacc: 114 instructions total, 13 of them EE.* PIE ops
    loop body 0x0119..0x01d1: 68 instructions (13 EE.* PIE + 10 RUR, rest scalar/branch)
    loop body 0x01e5..0x022e: 28 instructions (0 EE.* PIE + 0 RUR, rest scalar/branch)
ex12_dist2_q16: 23 instructions total, 15 of them EE.* PIE ops
    loop body 0x023e..0x026d: 17 instructions (15 EE.* PIE + 0 RUR, rest scalar/branch)
```

## モデルと参照実装 (the two implementations that were compared)

### `ex12_*_c` — the scalar references (host `gcc -O2`, would be the `main.c` version)

static int32_t sat32_ll(int64_t v)
{
    if (v > 2147483647LL) return 2147483647;
    if (v < -2147483648LL) return -2147483648;
    return (int32_t)v;
}

static int32_t sat16_i32(int32_t v)
{
    if (v > 32767) return 32767;
    if (v < -32768) return -32768;
    return (int16_t)v;
}

/* ex12_integrate_c: the kernel's model, in the order the pipeline applies it -- VADDS.S32, VADDS.S32,
 * VMIN.S32, VMAX.S32. The saturating adds are what make this differ from the exact sum wherever
 * |pos[i]+vel[i]| exceeds the int32 range (ex12_integrate_exact_c is the other reading, and the caller
 * can count the disagreement instead of assuming it away). */
void ex12_integrate_c(const int32_t *pos, const int32_t *vel, const int32_t *acc, int32_t *out,
                      int32_t lo, int32_t hi)
{
    for (int i = 0; i < 8; i++) {
        int32_t t = sat32_ll((int64_t)pos[i] + vel[i]);         /* EE.VADDS.S32 (sat32) */
        int32_t u = sat32_ll((int64_t)t + acc[i]);              /* EE.VADDS.S32 (sat32) */
        int32_t m = (u < hi) ? u : hi;                          /* EE.VMIN.S32 */
        out[i] = (m > lo) ? m : lo;                             /* EE.VMAX.S32 (lo wins if lo > hi) */
    }
}

/* The same three-term sum done in 64-bit and clamped once: "the maths", used only to say where the
 * saturating implementation stops agreeing with it. */
void ex12_integrate_exact_c(const int32_t *pos, const int32_t *vel, const int32_t *acc, int32_t *out,
                            int32_t lo, int32_t hi)
{
    for (int i = 0; i < 8; i++) {
        int64_t s = (int64_t)pos[i] + vel[i] + acc[i];
        int64_t m = (s < hi) ? s : hi;
        out[i] = (int32_t)((m > lo) ? m : lo);
    }
}

/* ex12_sat_masks_c: separated on an axis iff box_max < query_min or box_min > query_max; the mask is
 * -1 (all ones) for overlap and 0 for separated. `boxes` is the plane-major image the kernel loads:
 * box b, axis ax has its min at BYTE offset 96*(b/8) + 32*ax + 2*(b%8) and its max 16 bytes further on,
 * which in int16 units (this pointer) is 48*(b/8) + 16*ax + (b%8) and + 8. The kernel builds those
 * byte offsets by walking the pointer 16 bytes at a time. */
void ex12_sat_masks_c(const int16_t *boxes, const int16_t *query, int16_t *out, uint32_t n_boxes)
{
    for (uint32_t b = 0; b < n_boxes; b++) {
        int sep = 0;
        for (int ax = 0; ax < 3; ax++) {
            int16_t bmin = boxes[48 * (b / 8) + 16 * ax + (b % 8)];
            int16_t bmax = boxes[48 * (b / 8) + 16 * ax + 8 + (b % 8)];
            int16_t qmin = query[2 * ax];
            int16_t qmax = query[2 * ax + 1];
            if (bmax < qmin || bmin > qmax) {
                sep = 1;
            }
        }
        out[b] = sep ? 0 : -1;
    }
}

/* ex12_dist2_qacc_c: the QACC lane after three saturating 16-bit subtractions and three squaring MACs,
 * clamped to the int32 output exactly as the kernel's unsigned `minu` does. `pt` is the plane-major
 * image: axis ax of point i at BYTE offset 48*(i/8) + 16*ax + 2*(i%8) = int16 index
 * 24*(i/8) + 8*ax + (i%8). */
void ex12_dist2_qacc_c(const int16_t *pt_a, const int16_t *pt_b, int32_t *out, uint32_t n_points)
{
    for (uint32_t i = 0; i < n_points; i++) {
        int64_t s = 0;                                          /* the QACC lane is 40 bits wide */
        for (int ax = 0; ax < 3; ax++) {
            int16_t a = pt_a[24 * (i / 8) + 8 * ax + (i % 8)];
            int16_t b = pt_b[24 * (i / 8) + 8 * ax + (i % 8)];
            int16_t d = sat16_i32((int32_t)a - (int32_t)b);      /* EE.VSUBS.S16 (sat16) */
            s += (int32_t)d * d;                                /* EE.VMULAS.S16.QACC, per-lane */
        }
        out[i] = (s <= 0x7FFFFFFF) ? (int32_t)s : 2147483647;   /* unsigned minu with 2^31-1 */
    }
}

/* The Q16 readout: sat16(lane >> shift), the same thing EE.SRCMB.S16.QACC produces. */
void ex12_dist2_q16_c(const int16_t *pt_a, const int16_t *pt_b, int16_t *out, uint32_t n_points,
                      uint32_t shift)
{
    for (uint32_t i = 0; i < n_points; i++) {
        int64_t s = 0;                                          /* ditto: shift the 40-bit lane */
        for (int ax = 0; ax < 3; ax++) {
            int16_t a = pt_a[24 * (i / 8) + 8 * ax + (i % 8)];
            int16_t b = pt_b[24 * (i / 8) + 8 * ax + (i % 8)];
            int16_t d = sat16_i32((int32_t)a - (int32_t)b);
            s += (int32_t)d * d;
        }
        out[i] = sat16_i32((int32_t)(s >> shift));
    }
}

/* Two 64-bit references to say WHERE the int32 kernel stops being exact:
 *   ex12_dist2_satdiff_c : the 16-bit differences saturate (as EE.VSUBS.S16 does), the SUM does not.
 *                          The kernel's int32 output is min(this, 2^31-1) for every input.
 *   ex12_dist2_exact_c   : nothing saturates anywhere; equal to the kernel only while every |a-b| fits
 *                          int16 and the sum fits int32. */
int64_t ex12_dist2_satdiff_c(const int16_t *pt_a, const int16_t *pt_b, uint32_t i)
{
    int64_t s = 0;
    for (int ax = 0; ax < 3; ax++) {
        int32_t a = pt_a[24 * (i / 8) + 8 * ax + (i % 8)];
        int32_t b = pt_b[24 * (i / 8) + 8 * ax + (i % 8)];
        int64_t d = sat16_i32(a - b);
        s += d * d;
    }
    return s;
}

int64_t ex12_dist2_exact_c(const int16_t *pt_a, const int16_t *pt_b, uint32_t i)
{
    int64_t s = 0;
    for (int ax = 0; ax < 3; ax++) {
        int32_t a = pt_a[24 * (i / 8) + 8 * ax + (i % 8)];
        int32_t b = pt_b[24 * (i / 8) + 8 * ax + (i % 8)];
        int64_t d = (int64_t)a - b;
        s += d * d;
    }
    return s;
}

### the Python model — the `.S` read op by op

#!/usr/bin/env python3
"""ex12_physics.S -- host model + check.

The model below is written as the *instruction sequence* of ex12_physics.S, one Python function per
kernel, with each PIE op applied to lane lists using the semantics quoted from the TRM in the .md
(saturating adds, saturating 16-bit subtract, per-lane compares, whole-register logic ops, the 40-bit
QACC lane packing and the RUR word order). The reference it is compared against is the `ex12_*_c` scalar
code in ref.c (compiled with the host gcc), which is what would go into main.c; the case inputs and the
reference outputs are exchanged through a text file, so the two implementations share nothing but the
format.

What this can and cannot show is stated in ex12_physics.md: it is a model-vs-reference check (my reading
of my own instruction sequence against the scalar formula), not a silicon run.
"""
import random
import subprocess
import sys

M32 = 0xFFFFFFFF
M16 = 0xFFFF


# ---------------------------------------------------------------- reference-side scalar helpers (as in ref.c)
def sat16(v):
    return max(-32768, min(32767, v))


def sat32(v):
    return max(-2147483648, min(2147483647, v))


def sat40(v):
    return max(-(2 ** 39), min(2 ** 39 - 1, v))


def clamp(v, lo, hi):
    m = v if v < hi else hi
    return m if m > lo else lo


# ---------------------------------------------------------------- memory, as the PIE accesses see it
def ld16(mem, addr):
    return int.from_bytes(mem[addr:addr + 2], "little", signed=True)


def ld32(mem, addr):
    return int.from_bytes(mem[addr:addr + 4], "little", signed=True)


def st32(mem, addr, v):
    mem[addr:addr + 4] = (v & M32).to_bytes(4, "little")


def vld128_s16(mem, addr):
    addr &= ~15                                  # {as[31:4],4{0}}: the low four bits are dropped
    return [ld16(mem, addr + 2 * i) for i in range(8)]


def vld128_s32(mem, addr):
    addr &= ~15
    return [ld32(mem, addr + 4 * i) for i in range(4)]


def vldbc16(mem, addr):
    v = ld16(mem, addr & ~1)
    return [v] * 8


def vldbc32(mem, addr):
    v = ld32(mem, addr & ~3)
    return [v] * 4


# ---------------------------------------------------------------- A: ex12_integrate
def model_integrate(mem_pos, mem_vel, mem_acc, mem_out, lo, hi):
    frame = bytearray(32)                        # `entry a1, 32`
    st32(frame, 0, lo)
    st32(frame, 4, hi)
    q6 = vldbc32(frame, 0)                       # EE.VLDBC.32 q6, a1  -> {4{lo}}
    q7 = vldbc32(frame, 4)                       # EE.VLDBC.32 q7, a9  -> {4{hi}}
    p = v = a = o = 0
    for _ in range(2):                           # two four-lane register groups
        q0 = vld128_s32(mem_pos, p); p += 16
        q1 = vld128_s32(mem_vel, v); v += 16
        q2 = vld128_s32(mem_acc, a); a += 16
        q3 = [sat32(x + y) for x, y in zip(q0, q1)]      # EE.VADDS.S32
        q3 = [sat32(x + y) for x, y in zip(q3, q2)]      # EE.VADDS.S32
        q3 = [x if x <= h else h for x, h in zip(q3, q7)]  # EE.VMIN.S32 qa = (qx<=qy) ? qx : qy
        q3 = [x if x >= l else l for x, l in zip(q3, q6)]  # EE.VMAX.S32
        for i, val in enumerate(q3):
            st32(mem_out, o + 4 * i, val)
        o += 16
    return mem_out


# ---------------------------------------------------------------- B: ex12_sat_masks
def vcmp_lt(x, y):
    return [M16 if a < b else 0 for a, b in zip(x, y)]


def vcmp_gt(x, y):
    return [M16 if a > b else 0 for a, b in zip(x, y)]


def v_or(x, y):
    return [a | b for a, b in zip(x, y)]


def v_not(x):
    return [(~a) & M16 for a in x]


def model_sat_masks(mem_boxes, mem_query, mem_out, n_boxes):
    groups = n_boxes >> 3
    bp = 0
    op = 0
    for _ in range(groups):
        sep = None
        for ax in range(3):
            q0 = vld128_s16(mem_boxes, bp); bp += 16         # min_<ax>
            q1 = vld128_s16(mem_boxes, bp); bp += 16         # max_<ax>
            q2 = vldbc16(mem_query, 4 * ax)                  # {8{qmin}}: the query is
            q3 = vldbc16(mem_query, 4 * ax + 2)              # {8{qmax}}  [qmin_x,qmax_x,qmin_y,...]
            s1 = vcmp_lt(q1, q2)                             # EE.VCMP.LT.S16: max < qmin
            s2 = vcmp_gt(q0, q3)                             # EE.VCMP.GT.S16: min > qmax
            s = v_or(s1, s2)                                 # EE.ORQ
            sep = s if sep is None else v_or(sep, s)          # EE.ORQ across axes
        mask = v_not(sep)                                    # EE.NOTQ
        for i, val in enumerate(mask):
            mem_out[op + 2 * i:op + 2 * i + 2] = (val & M16).to_bytes(2, "little")
        op += 16
    tail = n_boxes & 7
    if tail:
        base = 96 * groups                                    # a2 has already advanced this far
        qmin = [ld16(mem_query, 2 * i) for i in range(6)]     # l16si a8..a13
        for b in range(tail):
            sep = False
            for ax in range(3):
                bmin = ld16(mem_boxes, base + 32 * ax + 2 * b)
                bmax = ld16(mem_boxes, base + 32 * ax + 16 + 2 * b)
                if bmin > qmin[2 * ax + 1] or bmax < qmin[2 * ax]:
                    sep = True
            mem_out[op:op + 2] = (0 if sep else M16).to_bytes(2, "little")
            op += 2
    return mem_out


# ---------------------------------------------------------------- C: ex12_dist2_qacc / _q16
def pack_words(lanes40):
    """QACC_L/QACC_H as the five AR words RUR.QACC_L_0..4 / RUR.QACC_H_0..4 hand out: 160 bits, lanes at
    40-bit offsets, read as 32-bit little-endian words."""
    v = 0
    for j, lane in enumerate(lanes40):
        v |= (lane & ((1 << 40) - 1)) << (40 * j)
    return [(v >> (32 * i)) & M32 for i in range(5)]


def lane_words_lo32(w):
    """The low 32 bits of the four lanes packed in one 160-bit register, extracted exactly as the
    assembly does it from w0..w4."""
    return [
        w[0],
        ((w[1] >> 8) | ((w[2] << 24) & M32)) & M32,
        ((w[2] >> 16) | ((w[3] << 16) & M32)) & M32,
        ((w[3] >> 24) | ((w[4] << 8) & M32)) & M32,
    ]


def _mac(qacc, d):
    """EE.VMULAS.S16.QACC q2, q2: lane j of the 16-bit format accumulates into lane j of QACC_L (j<4)
    or QACC_H (j>=4), saturating at 40 bits."""
    for j in range(8):
        prod = d[j] * d[j]
        k = j & 3
        acc = qacc[j >> 2][k]
        qacc[j >> 2][k] = sat40(acc + prod)


def model_dist2(mem_a, mem_b, mem_out, n_points):
    """Returns (out_bytes, hi_bits_violations): the second value counts 40-bit lanes whose bits 32..39
    were nonzero, i.e. the cases where dropping them (as the kernel does) would lose information."""
    groups = n_points >> 3
    ap = bp = op = 0
    hi_violations = 0
    for _ in range(groups):
        qacc = [[0, 0, 0, 0], [0, 0, 0, 0]]                   # EE.ZERO.QACC: L lanes 0..3, H lanes 4..7
        for _ax in range(3):
            q0 = vld128_s16(mem_a, ap); ap += 16
            q1 = vld128_s16(mem_b, bp); bp += 16
            d = [sat16(x - y) for x, y in zip(q0, q1)]         # EE.VSUBS.S16
            _mac(qacc, d)                                      # EE.VMULAS.S16.QACC
        for half in range(2):
            for lane in qacc[half]:
                if (lane >> 32) != 0:
                    hi_violations += 1
        w = pack_words(qacc[0]) + pack_words(qacc[1])          # 10 RUR reads: L_0..4 then H_0..4
        vals = lane_words_lo32(w[0:5]) + lane_words_lo32(w[5:10])
        for i, val in enumerate(vals):
            val = min(val, 0x7FFFFFFF)                         # unsigned `minu` with the frame constant
            st32(mem_out, op + 4 * i, val)
        op += 32
    tail = n_points & 7
    if tail:
        base = 48 * groups                                    # a2/a3 have already advanced this far
        for t in range(tail):
            s = 0
            for ax in range(3):
                a = ld16(mem_a, base + 16 * ax + 2 * t)
                b = ld16(mem_b, base + 16 * ax + 2 * t)
                d = sat16(a - b)                               # min/max pair = the same sat16
                s = (s + d * d) & M32                          # 32-bit add, no saturation
            s = min(s, 0x7FFFFFFF)                             # minu
            st32(mem_out, op + 4 * t, s)
        op += 4 * tail
    return mem_out, hi_violations


def model_dist2_q16(mem_a, mem_b, mem_out, n_points, shift):
    """The SRCMB.S16.QACC readout: eight lanes in one instruction, saturating at 16 bits."""
    groups = n_points >> 3
    ap = bp = op = 0
    for _ in range(groups):
        qacc = [[0, 0, 0, 0], [0, 0, 0, 0]]
        for _ax in range(3):
            q0 = vld128_s16(mem_a, ap); ap += 16
            q1 = vld128_s16(mem_b, bp); bp += 16
            d = [sat16(x - y) for x, y in zip(q0, q1)]
            _mac(qacc, d)
        lanes = qacc[0] + qacc[1]
        for i, lane in enumerate(lanes):
            val = sat16(lane >> (shift & 0x3F))                 # as[5:0], arithmetic shift
            mem_out[op + 2 * i:op + 2 * i + 2] = (val & M16).to_bytes(2, "little")
        op += 16
    return (n_points >> 3) << 3, mem_out


# ---------------------------------------------------------------- case generation
def place_box(img, b, ax, is_max, val):
    off = 96 * (b // 8) + 32 * ax + (16 if is_max else 0) + 2 * (b % 8)
    img[off:off + 2] = (val & M16).to_bytes(2, "little")


def place_pt(img, i, ax, val):
    off = 48 * (i // 8) + 16 * ax + 2 * (i % 8)
    img[off:off + 2] = (val & M16).to_bytes(2, "little")


def main():
    rnd = random.Random(0xE12C0DE)
    cases = []            # (kind, payload, ...)

    # ---- A: integrate. Three families: full-range int32 (the saturating corner), small (in-domain),
    #         and the exact powers-of-two edges.
    for _ in range(120):
        pos = [rnd.randint(-2 ** 31, 2 ** 31 - 1) for _ in range(8)]
        vel = [rnd.randint(-2 ** 31, 2 ** 31 - 1) for _ in range(8)]
        acc = [rnd.randint(-2 ** 31, 2 ** 31 - 1) for _ in range(8)]
        lo, hi = -1000, 1000
        cases.append(("A", (pos, vel, acc, lo, hi)))
    for _ in range(120):
        pos = [rnd.randint(-10 ** 6, 10 ** 6) for _ in range(8)]
        vel = [rnd.randint(-10 ** 6, 10 ** 6) for _ in range(8)]
        acc = [rnd.randint(-10 ** 6, 10 ** 6) for _ in range(8)]
        lo, hi = rnd.choice([(-1000, 1000), (-2 ** 31, 2 ** 31 - 1), (0, 100)])
        cases.append(("A", (pos, vel, acc, lo, hi)))
    for _ in range(60):
        pos = [rnd.choice([2 ** 31 - 1, -2 ** 31, 0, 2 ** 30, -2 ** 30]) for _ in range(8)]
        vel = [2 ** 30, -2 ** 30, 2 ** 31 - 1, -2 ** 31, 1, -1, 1073741824, -1073741824]
        acc = [rnd.choice([-2 ** 31, 2 ** 31 - 1, -1, 1, 0]) for _ in range(8)]
        lo, hi = -2 ** 31, 2 ** 31 - 1
        cases.append(("A", (pos, vel, acc, lo, hi)))

    # ---- B: sat_masks. n_boxes 0..27 (so both the vector path and the scalar tail run), 2D-ish small
    #         boxes and 3D boxes, with touching coordinates to sit on the <= / >= boundary.
    for _ in range(200):
        n = rnd.randint(0, 27)
        words = ((n + 7) // 8) * 48
        img = bytearray(2 * words)
        qimg = bytearray(12)
        cx = [rnd.randint(-2000, 2000) for _ in range(3)]
        half = [rnd.randint(0, 400) for _ in range(3)]
        for ax in range(3):
            qimg[4 * ax:4 * ax + 4] = ((cx[ax] - half[ax]) & M16).to_bytes(2, "little") + \
                                      ((cx[ax] + half[ax]) & M16).to_bytes(2, "little")
        for b in range(n):
            for ax in range(3):
                # half the boxes straddle the query, half are flung out to +-30000
                if rnd.random() < 0.5:
                    c = cx[ax] + rnd.randint(-500, 500)
                    h = rnd.randint(0, 300)
                    if rnd.random() < 0.15:
                        h = half[ax]                       # touching/cloned edges
                else:
                    c = rnd.randint(-30000, 30000)
                    h = rnd.randint(0, 200)
                place_box(img, b, ax, False, c - h)
                place_box(img, b, ax, True, c + h)
        if rnd.random() < 0.3:                             # a 2D caller: z = the full range
            for b in range(n):
                place_box(img, b, 2, False, -32768)
                place_box(img, b, 2, True, 32767)
            qimg[8:12] = (-32768 & M16).to_bytes(2, "little") + (32767 & M16).to_bytes(2, "little")
        cases.append(("B", (n, qimg, img)))

    # ---- C: dist2. Small points (in-domain), full-range int16 (the saturating corner, including the
    #         exact +-32767 extreme), and n_points 0..23 so the scalar tail runs.
    for _ in range(200):
        n = rnd.randint(0, 23)
        words = ((n + 7) // 8) * 24
        ia = bytearray(2 * words)
        ib = bytearray(2 * words)
        big = rnd.random() < 0.4
        lim = 32767 if big else 2000
        for i in range(n):
            for ax in range(3):
                place_pt(ia, i, ax, rnd.randint(-lim, lim))
                place_pt(ib, i, ax, rnd.randint(-lim, lim))
        if rnd.random() < 0.3 and n:
            for i in range(n):                          # the extreme lane: |d| = 32767 on all axes
                for ax in range(3):
                    place_pt(ia, i, ax, 32767 if i % 2 else -32768)
                    place_pt(ib, i, ax, 0)
        cases.append(("C", (n, ia, ib, 8)))

    # ---- write the case file, run the C reference
    lines = []
    for kind, p in cases:
        if kind == "A":
            pos, vel, acc, lo, hi = p
            lines.append("A " + " ".join(str(x) for x in pos + vel + acc + [lo, hi]))
        elif kind == "B":
            n, qimg, img = p
            vals = [n] + [ld16(qimg, 2 * i) for i in range(6)] + [ld16(img, 2 * i) for i in range(len(img) // 2)]
            lines.append("B " + " ".join(str(x) for x in vals))
        else:
            n, ia, ib, shift = p
            vals = [n] + [ld16(ia, 2 * i) for i in range(len(ia) // 2)] + \
                   [ld16(ib, 2 * i) for i in range(len(ib) // 2)] + [shift]
            lines.append("C " + " ".join(str(x) for x in vals))
    with open("/tmp/ex12check/cases.txt", "w") as f:
        f.write("\n".join(lines) + "\n")

    proc = subprocess.run(["/tmp/ex12check/ref"], stdin=open("/tmp/ex12check/cases.txt", "rb"),
                          stdout=subprocess.PIPE, check=True)
    ref_lines = proc.stdout.decode().strip().split("\n")
    assert len(ref_lines) == len(cases), (len(ref_lines), len(cases))

    # ---- run the model and compare
    n_a = n_b = n_c = 0
    a_lanes = 0          # int32 lanes compared
    a_sat_cand = 0       # lanes whose pos+vel leaves the int32 range (the only ones that may diverge)
    a_div_in_domain = 0  # divergences where pos+vel DID fit int32 (must be 0)
    b_groups = 0
    b_tail = 0
    b_over = b_sep = 0
    c_pairs = 0
    c_tail = 0
    c_groups = 0
    c_maxd = 0
    bad = []
    a_div = []            # where the kernel's saturating adds differ from the exact sum
    c_clamp = 0           # lanes where the int32 clamp fired
    c_satdiff = 0         # lanes where the 16-bit difference saturated (|a-b| > 32767)
    c_q16_sat = 0         # lanes where the 16-bit readout saturated
    hi_viol = 0
    b_2d = 0
    max_dist2 = 0
    for (kind, p), rline in zip(cases, ref_lines):
        r = rline.split()
        if kind == "A":
            n_a += 1
            pos, vel, acc, lo, hi = p
            mem_pos = bytearray(32)
            mem_vel = bytearray(32)
            mem_acc = bytearray(32)
            for i in range(8):
                st32(mem_pos, 4 * i, pos[i])
                st32(mem_vel, 4 * i, vel[i])
                st32(mem_acc, 4 * i, acc[i])
            mem_out = bytearray(32)
            model_integrate(mem_pos, mem_vel, mem_acc, mem_out, lo, hi)
            got = [ld32(mem_out, 4 * i) for i in range(8)]
            want = [int.from_bytes(bytes.fromhex(w), "big", signed=True) for w in r[1:9]]
            want_exact = [int.from_bytes(bytes.fromhex(w), "big", signed=True) for w in r[9:17]]
            if got != want:
                bad.append(("A", n_a, got, want, p))
            for i in range(8):
                a_lanes += 1
                in_domain = abs(pos[i] + vel[i]) <= 2 ** 31 - 1
                if not in_domain:
                    a_sat_cand += 1
                if want[i] != want_exact[i]:
                    a_div.append((n_a, i, pos[i], vel[i], acc[i], lo, hi, want[i], want_exact[i]))
                    if in_domain:
                        a_div_in_domain += 1
        elif kind == "B":
            n_b += 1
            n, qimg, img = p
            mem_out = bytearray(2 * n)
            model_sat_masks(img, qimg, mem_out, n)
            got = [ld16(mem_out, 2 * i) for i in range(n)]
            got_u = [v % 65536 for v in got]                  # 0xFFFF is -1 as int16
            want = [int.from_bytes(bytes.fromhex(w), "big", signed=False) for w in r[1:]]
            if got_u != want:
                bad.append(("B", n_b, got, want, p))
            if any(v not in (0, 0xFFFF) for v in got_u):
                bad.append(("B-mask", n_b, got_u, "0/0xFFFF", p))
            b_groups += n >> 3
            b_tail += n & 7
            b_over += sum(1 for v in got_u if v == 0xFFFF)
            b_sep += sum(1 for v in got_u if v == 0)
            # the 2D note: with z = the full range the z test can never separate
            if qimg[8:12] == (-32768 & M16).to_bytes(2, "little") + (32767 & M16).to_bytes(2, "little"):
                b_2d += 1
                for i in range(n):
                    if ld16(img, 64 + 2 * (i % 8)) > ld16(qimg, 10) or \
                       ld16(img, 96 * (i // 8) + 80 + 2 * (i % 8)) < ld16(qimg, 8):
                        bad.append(("B-2d-z", n_b, i, "z must not separate", p))
        else:
            n_c += 1
            n, ia, ib, shift = p
            mem_out = bytearray(4 * n)
            model_dist2(ia, ib, mem_out, n)
            got_u = [ld32(mem_out, 4 * i) for i in range(n)]
            want = [int.from_bytes(bytes.fromhex(w), "big", signed=True) for w in r[1:1 + n]]
            if got_u != want:
                bad.append(("C", n_c, got_u, want, p))
            satdiff = [int.from_bytes(bytes.fromhex(w), "big", signed=False)
                       for w in r[3 + 2 * n:3 + 3 * n]]
            exact = [int.from_bytes(bytes.fromhex(w), "big", signed=False) for w in r[4 + 3 * n:4 + 4 * n]]
            for i in range(n):
                # claim 1: the int32 kernel is min(sum of the saturating 16-bit differences, 2^31-1)
                if got_u[i] != min(satdiff[i], 0x7FFFFFFF):
                    bad.append(("C-clamp", n_c, i, got_u[i], satdiff[i]))
                # claim 2: it is the true sum of squares exactly while no 16-bit difference saturates
                #          and the sum stays below 2^31
                if satdiff[i] == exact[i] and satdiff[i] < 2 ** 31:
                    if got_u[i] != exact[i]:
                        bad.append(("C-exact", n_c, i, got_u[i], exact[i]))
                    max_dist2 = max(max_dist2, exact[i])
                elif satdiff[i] != exact[i]:
                    c_satdiff += 1
                if got_u[i] == 0x7FFFFFFF:
                    c_clamp += 1
            q16_out = bytearray(2 * n)
            written, q16_out = model_dist2_q16(ia, ib, q16_out, n, shift)
            ng = (n >> 3) << 3                       # the Q16 kernel only writes whole groups
            want_q = [int.from_bytes(bytes.fromhex(w), "big", signed=True) for w in r[2 + n:2 + n + ng]]
            got_q = [ld16(q16_out, 2 * i) for i in range(ng)]
            if got_q != want_q:
                bad.append(("C-q16", n_c, got_q, want_q, p))
            if written != (n >> 3) << 3:
                bad.append(("C-q16-count", n_c, written, (n >> 3) << 3, p))
            c_pairs += n
            c_groups += n >> 3
            c_tail += n & 7
            for i in range(n):
                for ax in range(3):
                    c_maxd = max(c_maxd, abs(ld16(ia, 48 * (i // 8) + 16 * ax + 2 * (i % 8)) -
                                             ld16(ib, 48 * (i // 8) + 16 * ax + 2 * (i % 8))))
            _, hi_v = model_dist2(ia, ib, bytearray(4 * n), n)
            hi_viol += hi_v
            for i in range(ng):
                if got_q[i] == 32767 or got_q[i] == -32768:
                    c_q16_sat += 1

    print("ex12_physics.S host check: instruction-level model (Python) vs ex12_*_c (host gcc -O2)")
    print("inputs: deterministic PRNG, seed 0xE12C0DE; 300 A cases, 200 B cases, 200 C cases")
    print("")
    print(f"kernel A (ex12_integrate): 2400 int32 lanes. {a_sat_cand} of them have |pos+vel| > 2^31-1")
    print(f"   (the only lanes where the saturating VADDS.S32 pair can differ from the exact sum).")
    print(f"   model vs ex12_integrate_c mismatches: {sum(1 for b in bad if b[0] == 'A')}")
    print(f"   kernel vs ex12_integrate_exact_c: {len(a_div)} divergent lanes, all with "
          f"|pos+vel| > 2^31-1: {a_div_in_domain == 0}")
    if a_div:
        c = a_div[0]
        print(f"   worst case seen: pos={c[2]} vel={c[3]} acc={c[4]} lo={c[5]} hi={c[6]}"
              f" -> kernel={c[7]}, exact sum={c[8]} (pos+vel={c[2] + c[3]})")
    print("")
    print(f"kernel B (ex12_sat_masks): {b_groups} eight-box groups through the vector loop plus")
    print(f"   {b_tail} boxes through the scalar tail; {b_over} overlaps and {b_sep} separations.")
    print(f"   model vs ex12_sat_masks_c mismatches: {sum(1 for b in bad if b[0] == 'B')}")
    print(f"   every mask is exactly 0xFFFF or 0: {not any(b[0] == 'B-mask' for b in bad)}")
    print(f"   the {b_2d} calls whose z range is the full int16 range: no box was separated by z "
          f"(the 2D reuse note): {not any(b[0] == 'B-2d-z' for b in bad)}")
    print("")
    print(f"kernel C (ex12_dist2_qacc): {c_pairs} point pairs = {c_groups} groups of eight + {c_tail} tail")
    print(f"   pairs; max |a-b| seen = {c_maxd} (so the saturating 16-bit subtract was exercised).")
    print(f"   model vs ex12_dist2_qacc_c mismatches: {sum(1 for b in bad if b[0] == 'C')}")
    print(f"   model == min(sum of the saturating differences, 2^31-1) on every lane: "
          f"{not any(b[0] in ('C', 'C-clamp') for b in bad)}")
    print(f"   == the unbounded sum of squares wherever no difference saturates and the sum < 2^31: "
          f"{not any(b[0] == 'C-exact' for b in bad)}")
    print(f"   40-bit QACC lanes whose bits 32..39 were nonzero: {hi_viol} (0 = only the low 32 bits matter)")
    print(f"   int32 clamp (dist2 >= 2^31) fired on {c_clamp} lanes; 16-bit readout saturated on {c_q16_sat}")
    print(f"   ex12_dist2_q16: mismatches {sum(1 for b in bad if b[0] == 'C-q16')}, "
          f"written-count mismatches {sum(1 for b in bad if b[0] == 'C-q16-count')}, "
          f"max in-domain dist2 {max_dist2} (3*32767^2 = {3 * 32767 * 32767})")
    print(f"   differences that saturated (|a-b| > 32767): {c_satdiff} lanes")
    print("")
    print(f"TOTAL model-vs-reference mismatches over {n_a + n_b + n_c} cases: {len(bad)}")
    for b in bad[:3]:
        print("   MISMATCH", b[0], b[1], str(b[2])[:120], str(b[3])[:120])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

## 実行結果 (the run, verbatim)

```
ex12_physics.S host check: instruction-level model (Python) vs ex12_*_c (host gcc -O2)
inputs: deterministic PRNG, seed 0xE12C0DE; 300 A cases, 200 B cases, 200 C cases

kernel A (ex12_integrate): 2400 int32 lanes. 407 of them have |pos+vel| > 2^31-1
   (the only lanes where the saturating VADDS.S32 pair can differ from the exact sum).
   model vs ex12_integrate_c mismatches: 0
   kernel vs ex12_integrate_exact_c: 51 divergent lanes, all with |pos+vel| > 2^31-1: True
   worst case seen: pos=2147483647 vel=2147483647 acc=-1 lo=-2147483648 hi=2147483647 -> kernel=2147483646, exact sum=2147483647 (pos+vel=4294967294)

kernel B (ex12_sat_masks): 228 eight-box groups through the vector loop plus
   704 boxes through the scalar tail; 154 overlaps and 2374 separations.
   model vs ex12_sat_masks_c mismatches: 0
   every mask is exactly 0xFFFF or 0: True
   the 53 calls whose z range is the full int16 range: no box was separated by z (the 2D reuse note): True

kernel C (ex12_dist2_qacc): 2374 point pairs = 206 groups of eight + 726 tail
   pairs; max |a-b| seen = 65030 (so the saturating 16-bit subtract was exercised).
   model vs ex12_dist2_qacc_c mismatches: 0
   model == min(sum of the saturating differences, 2^31-1) on every lane: True
   == the unbounded sum of squares wherever no difference saturates and the sum < 2^31: True
   40-bit QACC lanes whose bits 32..39 were nonzero: 0 (0 = only the low 32 bits matter)
   int32 clamp (dist2 >= 2^31) fired on 992 lanes; 16-bit readout saturated on 1253
   ex12_dist2_q16: mismatches 0, written-count mismatches 0, max in-domain dist2 1952879858 (3*32767^2 = 3221028867)
   differences that saturated (|a-b| > 32767): 370 lanes

TOTAL model-vs-reference mismatches over 700 cases: 0
```

## 未確認の前提 (semantics this kernel depends on that I could NOT confirm)

Each item says what is assumed, what the manual does say, and what breaks if the assumption is wrong. No
guessing is hidden in the code: every one of these is a dependency, and the first `ex09`-style measurement
run is what would settle them.

1. **`EE.VSUBS.S16` operand order (kernel C).** The manual contradicts itself: the Operation pseudo-code
   (p281) says `qa = qx - qy`, while the description text says "Registers qx and qy are the subtrahend and
   the minuend respectively", i.e. `qy - qx`. Nothing in the example suite has measured `VSUBS`
   (`ex08` only used the commutative `VADDS`). I assume the Operation. **Exposure: none** — a squared
   difference does not care about the sign, with exactly one exception: |a-b| = 32768, where one order
   saturates to +32767 and the other is representable as -32768, so the squares differ (32767² vs 32768²).
   That is outside the documented |Δ| ≤ 32767 domain. Kernel A uses only `VADDS` (commutative).
2. **The 40-bit QACC lane packing (kernel C).** "lane i lives at bits [40i, 40i+40) of QACC_L (i<4) or
   QACC_H (i≥4)" comes from the pseudo-code of `EE.VMULAS.S16.QACC` (p215), `EE.SRCMB.S16.QACC` (p130) and
   `EE.LDQA.S16.128.IP` (p105). It is **not measured**: `data/pie_examples_measured.json` has no QACC lane
   layout entry, and the run logs in `/workspace/backups` stop before `ex09` — the probe written for exactly
   this question. If the hardware packed the lanes differently, the reconstructed distances would be
   permuted/shifted.
3. **The `RUR.QACC_L_n` / `RUR.QACC_H_n` word order (kernel C).** TRM p64 says the 160-bit registers are
   transferred "via the five AR registers" but never says which word is which, nor whether the high word is
   sign- or zero-extended (for `ACCX` it explicitly says the second word is zero-extended — a different
   register, which is exactly why I do not want to assume). I assume word n = bits [32n, 32n+32). Same
   probe (`ex09_raw_qacc`, whose C model `lane40()` uses this same assumption) settles it.
4. **`EE.SRCMB.S16.QACC` writes the shifted value back into QACC** (`ex12_dist2_q16`). The pseudo-code
   (p130) assigns the shifted result back to QACC_L/QACC_H, i.e. it is a read-**modify**-write; if so, a
   second readout with a different shift sees already-shifted data. `ex09_srcmb_wb` is the probe; no
   measurement available. `ex12_dist2_q16` calls it **once per accumulation**, so it is correct under
   either reading — but a caller who wants two shifts of the same accumulation must re-accumulate.
5. **`EE.VCMP.LT.S16` / `EE.VCMP.GT.S16` are SIGNED compares (kernel B).** The name says `.S16`, the text
   says "compares the numerical values", and no example in the suite has ever compared anything (ex08 has
   no `VCMP`). Kernel B is built on it: with negative coordinates an unsigned compare would give a
   different mask. This is the single assumption most worth measuring next.
6. **Cycle cost of the accumulator chain.** Instruction counts here are counts. Table 1.7-2 has no `def`
   for the `VMULAS` family's accumulator (`notes/08`), and `RUR` does not appear in the timing tables at
   all, so the readout chain's stalls are unknown. `ex09`'s gap probes are the instrument; kernel C should
   not be scheduled on a cycle budget until that run exists.
7. **`EE.VLDBC.16` / `EE.VLDBC.32` alignment.** The pseudo-code forces the low 1 (or 2) address bits to 0.
   That is the same forcing that was measured for the 128-bit forms in ex03, but the broadcast loads
   themselves were not measured; the contract above (2-byte aligned query, 4-byte aligned frame slot for
   the int32 broadcasts) is what the pseudo-code requires, and a misaligned pointer would silently read
   the neighbouring scalar.
8. **Kernel A's saturating-add reading is a *choice*, not a measurement.** `VADDS` saturation at ±2^31 is
   quoted from p149 and is consistent with the ACCX/QACC saturation measured in ex05, but no example has
   measured `VADDS.S32` specifically. The divergence from the exact sum is stated above as a numeric
   bound rather than assumed away, so if the hardware wrapped instead of saturating, that bound is what
   changes.
9. **The data layouts (B and C) are my design, not a manual fact.** Plane-major groups of eight with the
   byte offsets above exist so that every load is a full 128-bit chunk on the 16-byte grid. The check
   verifies that the `.S` loads, the model and the scalar reference agree on that layout; it cannot verify
   anything about the hardware here, and the layout is the first thing to re-check when the kernel is
   wired into a caller.

## 検査が捕まえたもの (what the model-vs-reference check actually caught)

Four discrepancies, all resolved before the run above was pasted; three of them were in the *reference*:

1. The scalar reference read the plane-major image with **byte** offsets through an `int16_t *` (a factor
   of two). The model and the `.S` agreed with each other; the reference was wrong. This is the classic
   layout bug the layout table in this file exists to prevent.
2. The same reference accumulated the QACC lane in a 32-bit `int32_t`, which wraps where the hardware has a
   40-bit lane — it showed up as `+32767` where the 16-bit readout saturates (`-32768` on the other side
   of the correct answer). Fixed by modelling the 40-bit lane and shifting that.
3. My first model of kernel B's query broadcast used a 2-byte stride between axes instead of 4
   (`[qmin_x,qmax_x,qmin_y,qmax_y,...]`), and my first tail model forgot that the pointer has already
   advanced by the whole groups. Both were model bugs; the reference and the `.S` were right.
4. The host driver initially printed the C `dist2` results out of a buffer sized for one group of eight —
   the per-case bookkeeping, not the kernel; it showed up as garbage in C cases with more than 8 points.

## ビルドへの入れ方 (how to wire it in, not applied here)

```c
/* examples/firmware/main/examples.h */
void ex12_integrate(const int32_t *pos, const int32_t *vel, const int32_t *acc, int32_t *out,
                    int32_t lo, int32_t hi);
void ex12_sat_masks(const int16_t *boxes, const int16_t *query, int16_t *out, uint32_t n_boxes);
void ex12_dist2_qacc(const int16_t *pt_a, const int16_t *pt_b, int32_t *out, uint32_t n_points);
uint32_t ex12_dist2_q16(const int16_t *pt_a, const int16_t *pt_b, int16_t *out, uint32_t n_points,
                        uint32_t shift);

/* main.c, ex12(): the suite's DATA/CHECK/BENCH shape */
static void ex12(void)
{
    const char *ex = "ex12", *name = "physics";
    section_begin(ex, name);
    /* inputs: deterministic rnd16, printed with print_i16/print_i32 so the host checker can recompute */
    ex12_integrate(s_pos32, s_vel32, s_acc32, s_out32, -1000, 1000);
    ex12_integrate_c(s_pos32, s_vel32, s_acc32, s_ref32, -1000, 1000);
    check(ex, name, "integrate_matches_C", memcmp(s_out32, s_ref32, 32) == 0 ? 1 : 0, 1);
    print_i32(ex, name, "pos", s_pos32, 8);  print_i32(ex, name, "vel", s_vel32, 8);
    print_i32(ex, name, "acc", s_acc32, 8);  print_i32(ex, name, "integrated", s_out32, 8);
    printf("EX %s %s DATA bounds lo=%d hi=%d objects=8\n", ex, name, -1000, 1000);

    ex12_sat_masks(s_boxes, s_query, s_masks, N_BOXES);
    ex12_sat_masks_c(s_boxes, s_query, s_masks_ref, N_BOXES);
    check(ex, name, "sat_masks_matches_C", memcmp(s_masks, s_masks_ref, N_BOXES * 2) == 0 ? 1 : 0, 1);
    print_i16(ex, name, "boxes", s_boxes, BOX_WORDS);   print_i16(ex, name, "query", s_query, 6);
    print_i16(ex, name, "masks", s_masks, N_BOXES);
    printf("EX %s %s DATA layout n_boxes=%d planes=min_x,max_x,min_y,max_y,min_z,max_z group_bytes=96\n",
           ex, name, N_BOXES);

    ex12_dist2_qacc(s_pt_a, s_pt_b, s_d2, N_POINTS);
    ex12_dist2_qacc_c(s_pt_a, s_pt_b, s_d2_ref, N_POINTS);
    check(ex, name, "dist2_matches_C", memcmp(s_d2, s_d2_ref, N_POINTS * 4) == 0 ? 1 : 0, 1);
    check(ex, name, "dist2_equals_the_big_sum_where_no_difference_saturates", agree ? 1 : 0, 1);
    print_i16(ex, name, "pt_a", s_pt_a, POINT_WORDS);   print_i16(ex, name, "pt_b", s_pt_b, POINT_WORDS);
    print_i32(ex, name, "dist2", s_d2, N_POINTS);
    printf("EX %s %s DATA domain n_points=%d max_abs_delta=%d int32_clamps=%d\n", ...);

    ex12_dist2_q16(s_pt_a, s_pt_b, s_d2q, N_POINTS, 8);
    /* and the two BENCH lines the notes/08 format expects: */
    printf("BENCH integrate objects=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", ...);
    printf("BENCH dist2 pairs=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", ...);
    section_end(ex, name, fail == 0 ? 1 : 0, fail);
}
```

* The host checker gets a `check_ex12()` in `tools/check_examples_log.py` that recomputes all three from
  the printed inputs (the `ex12_*_c` code above is what it should port), plus a `DATA` mutation in
  `tools/selftest_examples_checker.py`.
* `ex12_dist2_qacc`'s cycle cost is the number worth measuring first, because the *instruction* count says
  the readout dominates: if `RUR` turns out to be a multi-cycle transfer, the Q16 path (2.1
  instructions/pair) is the only viable one at 60 fps — see the frame budget in `notes/08`.
