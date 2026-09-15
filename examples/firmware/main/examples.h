/* Kernels for the PIE examples. Every function here is hand-written Xtensa/PIE assembly whose encodings
 * were checked against the assembler by tools/asm_toolchain.py (see data/pie_encoding_errata.json for the
 * instructions where the manual and the assembler disagree).
 *
 * All of them are called from C, so they follow the windowed ABI: `entry a1, 32` ... `retw.n` (a bare
 * `ret` would corrupt the caller's register window). Buffers passed in must be 16-byte aligned: the PIE
 * 128-bit forms force the low 4 bits of the address to 0 (TRM p49), so a misaligned pointer is not
 * reported, it silently reads or writes the wrong bytes.
 */
#ifndef PIE_EXAMPLES_H
#define PIE_EXAMPLES_H

#include <stdint.h>

/* ex01: what the PIE memory forms do to their address operand. Each function runs one instruction and
 * returns (address_after - address_before); the TRM's printed ranges and the assembler's accepted ranges
 * disagree for EE.ST.ACCX.IP (4-byte step vs 8-byte step), so this is measured rather than read. */
uint32_t ex01_ld_accx_step_0(void *p);
uint32_t ex01_ld_accx_step_16(void *p);
uint32_t ex01_ld_accx_step_32(void *p);
uint32_t ex01_ld_accx_step_m16(void *p);
uint32_t ex01_st_accx_step_0(void *p);
uint32_t ex01_st_accx_step_16(void *p);
uint32_t ex01_st_accx_step_32(void *p);
uint32_t ex01_ld128_step_0(void *p);
uint32_t ex01_ld128_step_16(void *p);
uint32_t ex01_ld128_step_32(void *p);
uint32_t ex01_vld128_step_0(void *p);
uint32_t ex01_vld128_step_16(void *p);
uint32_t ex01_st128_step_16(void *p);
/* The same field value (imm8 = 1) written as a raw instruction word taken from the manual's diagram,
 * because the assembler refuses a value that is not a multiple of 8. */
uint32_t ex01_raw_ld_accx_field1(void *p);
uint32_t ex01_raw_st_accx_field1(void *p);
/* EE.SRS.ACCX au, as, 0: ACCX = *(int64 *)value64; result = sat32(ACCX >> (shift & 63)). */
int32_t ex01_srs_accx(const void *value64, uint32_t shift);
/* EE.BITREV qa, as: the diagram calls it an address register, the pseudo-code uses it as a value. The
 * four words of qa are written to out4 for the host to interpret. */
void ex01_bitrev(uint32_t as_value, uint32_t *out4);

/* ex02: C[16][16] (int32) = A[16][16] * Bt[16][16], int16 row-major, Bt being B transposed so the inner
 * loop reads contiguous 128-bit chunks. Two VMULAS.S16.ACCX (8 lanes summed into ACCX) per dot product. */
void ex02_matmul16(const int16_t *a, const int16_t *bt, int32_t *c);

/* ex03: 16-tap FIR. The kernel takes one window already on the 16-byte grid, because a 128-bit PIE access
 * drops the low four address bits (TRM p49) -- the first run of the sliding version proved that on silicon
 * (42 of 49 outputs were the same number, correct exactly at the 16-byte boundaries). The C side stages the
 * window; the aligned probe keeps the failure visible; the funnel probe measures the no-copy alternative.
 *   y = sat32((sum window[0..15] * h[0..15]) >> shift), the shift coming from a register (EE.SRS.ACCX). */
int32_t ex03_fir16_tap(const int16_t *window16, const int16_t *h, uint32_t shift);
void ex03_align_probe(const int16_t *x, int16_t *out32);
void ex03_funnel_probe(const int16_t *misaligned, const int16_t *next_chunk, int16_t *out_ab,
                       int16_t *out_ba);

/* ex04: QR transfers (LD.QR / ST.QR / MV.QR) and the LD.QR interlock at issue distance 0, 1 and 2. */
void ex04_qr_copy(void *dst, const void *src, uint32_t n_bytes);
void ex04_qr_move_roundtrip(void *dst, const void *src);
uint32_t ex04_ldqr_dep_d0(void *buf);
uint32_t ex04_ldqr_ind_d0(void *buf);
uint32_t ex04_ldqr_dep_d1(void *buf);
uint32_t ex04_ldqr_ind_d1(void *buf);
uint32_t ex04_ldqr_dep_d2(void *buf);
uint32_t ex04_ldqr_ind_d2(void *buf);

/* ex05: the accumulator's edges. out2[0] = sat32(ACCX), out2[1] = ACCX >> 9 (exact). */
void ex05_accx_saturation(int iterations, const int16_t *lanes8, int32_t *out2);

/* ex06: the FFT primitives. r2bf = lane-wise butterfly (sel2 picks the packing, so two functions); cmul =
 * two complex multiplies on interleaved (re,im) pairs with a SAR shift (sel4 picks the lane half). */
void ex06_r2bf_sel0(const int16_t *x8, int16_t *out8);
void ex06_r2bf_sel1(const int16_t *x8, int16_t *out8);
void ex06_cmul_half0(const int16_t *x, const int16_t *y, int16_t *out, uint32_t sar);
void ex06_cmul_half1(const int16_t *x, const int16_t *y, int16_t *out, uint32_t sar);

/* ex07: cycles per element, and a 4x4 transform of eight vertices at once. VSMULAS.S16.QACC broadcasts one
 * lane of the coefficient register and accumulates eight vertices in parallel; SRCMB.S16.QACC is the
 * saturating readout. out[r][j] = sat16((sum_k m[r][k]*v[k][j]) >> shift), v/w/out in SoA (8 lanes a row). */
uint32_t ex07_ccount(void);
void ex07_transform8(const int16_t *m, const int16_t *v, int16_t *out, uint32_t shift);

/* ex08: framebuffer effects over 128-bit chunks (16-bit lanes). Each has a C equivalent timed in main.c.
 *   half_blend: out[i] = ((uint16_t)a[i] & 0xF7DE) >> 1) + (((uint16_t)b[i] & 0xF7DE) >> 1)  (unsigned:
 *               the kernel uses EE.VMUL.U16 for the shift, so no lane sign-extends)
 *   shift_sign: the same shift with EE.VMUL.S16 and EE.VMUL.U16 side by side (they differ where bit 15 is set)
 *   brighten  : out[i] = sat16(a[i] + b[i])
 *   clamp     : out[i] = min(max(a[i], lo), hi) with lo/hi given as eight identical lanes
 *   tint      : out[i] = (int16_t)((a[i] * tint) >> shift)  -- truncating, not saturating */
void ex08_half_blend(const int16_t *a, const int16_t *b, const int16_t *mask8, const int16_t *ones8,
                     int16_t *out, int n_pixels);
void ex08_shift_sign(const int16_t *a, const int16_t *ones8, int16_t *out_signed, int16_t *out_unsigned,
                     uint32_t shift);
void ex08_brighten(const int16_t *a, const int16_t *b, int16_t *out, int n_pixels);
void ex08_clamp(const int16_t *a, const int16_t *lo8, const int16_t *hi8, int16_t *out, int n_pixels);
void ex08_tint(const int16_t *a, const int16_t *tint8, int16_t *out, int n_pixels, uint32_t shift);

/* ex09: the QACC accumulate -> readout hazard, probed rather than assumed (see ex09_qacc.S). All of these
 * take (v8, coef8, out8, shift); `one MAC with the coefficient lane sel8` then the readout after N slots.
 * The MAC4/CHAIN forms are ex07's row shape (four MACs into the same accumulator). */
void ex09_mac1_g0(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift);
void ex09_mac1_g1(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift);
void ex09_mac1_g2(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift);
void ex09_mac1_g3(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift);
void ex09_mac1_g4(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift);
void ex09_mac1_g6(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift);
void ex09_mac1_s1_g4(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift);
void ex09_mac1_s2_g4(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift);
void ex09_mac1_s3_g4(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift);
void ex09_mac4_g0(const int16_t *coef_row, const int16_t *v4rows, int16_t *out8, uint32_t shift);
void ex09_mac4_g2(const int16_t *coef_row, const int16_t *v4rows, int16_t *out8, uint32_t shift);
void ex09_mac4_g4(const int16_t *coef_row, const int16_t *v4rows, int16_t *out8, uint32_t shift);
void ex09_chain_g0(const int16_t *coefs, const int16_t *v4rows, int16_t *out, uint32_t shift, uint32_t rows);
void ex09_chain_g4(const int16_t *coefs, const int16_t *v4rows, int16_t *out, uint32_t shift, uint32_t rows);
/* QACC_L_0..4 then QACC_H_0..4 as raw words: lane i is (QACC_H_i[7:0] << 32) | QACC_L_i. */
void ex09_raw_qacc(const int16_t *v8, const int16_t *coef8, uint32_t *out10, uint32_t shift);
/* The row loop with no readout at all: ten raw accumulator words per row (four rows). */
void ex09_rows_raw(const int16_t *coefs, const int16_t *v4rows, uint32_t *out, uint32_t rows);
/* The same four rows with a per-row readout and no loop tail. */
void ex09_rows_unrolled(const int16_t *coefs, const int16_t *v4rows, int16_t *out, uint32_t shift);
/* The address walk alone (load+store, no MAC) and with four back-to-back loads per iteration. */
void ex09_ipwalk(const int16_t *src, int16_t *dst, uint32_t rows);
void ex09_ipwalk4(const int16_t *src, int16_t *dst, uint32_t rows);
/* The row loop with the coefficient register loaded once, outside the loop. */
void ex09_mac_fixed(const int16_t *coef_row, const int16_t *v4rows, int16_t *out, uint32_t rows,
                    uint32_t shift);
/* QACC zeroed between two MACs: v*coef[1] if the zero is ordered, v*(coef[0]+coef[1]) if it is not. */
void ex09_zero_vis(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift);
/* SRCMB twice with different shifts: does the readout write the accumulator back (read-modify-write)? */
void ex09_srcmb_wb(const int16_t *v8, const int16_t *coef8, int16_t *out_a, int16_t *out_b,
                   uint32_t shift_a, uint32_t shift_b);

/* ex10: motion video -- a block SAD accumulated in ACCX and a half-pel interpolated row. What is pinned
 * down: (A) there is no SAD and no ABS in the 220-instruction set, so |a-b| is spelled
 * VSUBS.S16(VMAX.S16, VMIN.S16) over the SIGNED lane readings, and the sum comes from EE.VMULAS.U16.ACCX
 * against a register of ones with the 40-bit accumulator read out ONCE through RUR.ACCX_0/ACCX_1 (no
 * saturation and no side effect, unlike EE.SRS.ACCX, which writes ACCX back and clamps into 32 bits). The
 * C reference in main.c follows the instructions -- a saturating signed distance -- so its CHECK is exact
 * on any input; 8-bit sample data (0..255, zero-extended into the lanes) is the domain where that also
 * equals the textbook SAD, and main.c counts the lanes where a full-range input makes the two part company
 * instead of pretending they agree.
 *   (B) the half-pel row is EE.VADDS.S16 twice (the sum, then the +1 rounding -- both SATURATING) followed
 * by EE.VMUL.U16 with SAR = 1, which is a LOGICAL shift (the reference expression casts to uint16_t before
 * the shift); one constant register serves as both the addend 1 and the multiplier 1. The rounding add
 * saturates, so the two readings of the expression agree exactly where a+b+1 fits a signed 16-bit lane:
 * main.c prints the divergence count and the saturation count rather than assuming they are equal.
 *   Layout: ex10_sad8 takes n_blocks*8 uint16 lanes in each of a/b and writes out[0] = ACCX[31:0],
 * out[1] = ACCX[39:32] (host total = ((uint64_t)out[1] << 32) | out[0]); ex10_halfpel takes n_lanes
 * (a multiple of 8, trailing lanes untouched), an ones8 of eight lanes holding 1, and writes n_lanes.
 * Every buffer 16-byte aligned (TRM p49). */
void ex10_sad8(const uint16_t *a, const uint16_t *b, uint32_t n_blocks, uint32_t *out);
void ex10_halfpel(const int16_t *a, const int16_t *b, const int16_t *ones8, int16_t *out,
                  uint32_t n_lanes);

/* ex11: the separable 8x8 integer block transform (the MP3 / JPEG / H.264 inner shape), eight columns in
 * the SIMD lanes. What is pinned down: EE.VSMULAS.S16.QACC broadcasts ONE lane of qy and multiply-
 * accumulates it against all eight lanes of qx, each lane into its own saturating 40-bit accumulator, so
 * the REDUCTION index goes in the broadcast operand and the FREE index in the lanes; the readout is
 * EE.SRCMB.S16.QACC (shift, saturate to 16 bits -- and it writes the shifted value back into QACC, which
 * is why this kernel zeroes the accumulator at the top of every output row). The PIE has eight 128-bit
 * registers and the assembler rejects q8, so ex07's "one live register per tap" shape does not scale from
 * four taps to eight: the row registers roll with a reuse distance of eight instructions.
 *   Layout: coef, block and out are 64 int16 row-major (coef/out row k at +16*k, block row i at +16*i) and
 * out[k][j] = sat16((sum over i of coef[k][i] * block[i][j]) >> shift). The eight lanes are the eight
 * COLUMNS j of the block, so this pass needs no transposition; the other axis of a 2-D transform is a
 * second call with the transposed coefficient table, which is the same arithmetic (main.c does both and
 * ships both tables in its DATA lines). Buffers must be 16-byte aligned. */
void ex11_block8x8(const int16_t *coef, const int16_t *block, int16_t *out, uint32_t shift);

/* ex12: the two loops a 2D/3D game spends its frame budget in.
 *   ex12_integrate: semi-implicit Euler on FOUR int32 lanes (EIGHT objects per call, two register groups),
 * out[i] = clamp(sat32(sat32(pos[i] + vel[i]) + acc[i]), lo, hi). The set has no plain vector add, so the
 * two adds are the SATURATING EE.VADDS.S32 and the clamp is VMIN.S32(hi) then VMAX.S32(lo) in that order
 * (lo must win when lo > hi, so the caller passes lo <= hi); lo/hi reach the vector unit through the
 * frame `entry` already allocated, read back with EE.VLDBC.32, because there is no move from an AR to a QR
 * register. The reference in main.c follows the instructions, so the CHECK is exact; where the kernel
 * stops agreeing with the exact 64-bit sum (|pos+vel| leaving int32) main.c prints the count.
 *   ex12_sat_masks: the AABB / separating-axis test for eight candidate boxes against one query box: on an
 * axis separated iff box_max < query_min or box_min > query_max (one EE.VCMP.LT.S16 and one
 * EE.VCMP.GT.S16 against a broadcast query scalar), EE.ORQ accumulates the separation bits across the two
 * tests and then across the three axes (De Morgan), and a single EE.NOTQ turns "no separation anywhere"
 * into the all-ones overlap mask. The compares are SIGNED 16-bit.
 *   Layout, which is the whole contract here: boxes is plane-major, six 16-byte planes per group of eight
 * boxes in the order +0 min_x, +16 max_x, +32 min_y, +48 max_y, +64 min_z, +80 max_z (96 bytes per group),
 * element (plane p, box i) at 96*(i/8) + 16*p + 2*(i%8); only the lanes below n_boxes are read (whole
 * groups of eight through the vector loop, the n_boxes & 7 remainder through the scalar tail). query is
 * six int16 in the same order -- min_x, max_x, min_y, max_y, min_z, max_z -- and 2-byte alignment is
 * enough for the broadcast loads. out is n_boxes int16, 0xFFFF (all ones) for overlap and 0 for separated,
 * 16-byte aligned: the vector stores are whole groups of eight and the tail writes single int16.
 *   ex12_dist2_qacc: squared distance of eight point pairs per group, accumulated per lane in QACC, so a
 * caller compares against r^2 and never needs a square root (there is neither a divide nor a square root
 * in the set). pt_a/pt_b are plane-major groups of eight points (+0 x, +16 y, +32 z; element (axis ax,
 * point i) at 48*(i/8) + 16*ax + 2*(i%8), 48 bytes per group). The readout is RUR.QACC_L_0..4 /
 * RUR.QACC_H_0..4: five 32-bit words per 160-bit half, the lanes bit-packed at 40-bit offsets. Only the
 * low 32 bits of a lane are taken, which is exact rather than approximate because the difference comes out
 * of a saturating 16-bit subtract: |dx| <= 32767, so dist2 <= 3*32767^2 < 2^32 and bits 32..39 of every
 * lane are zero for any input; the value is then unsigned-clamped (minu) to 2^31-1 to fit the int32 out.
 * out is n_points int32, 16-byte aligned. The n_points & 7 tail is scalar and takes the same saturating
 * difference and the same clamp, so the two paths agree.
 *   ex12_dist2_q16: the same three MACs with the one-instruction readout (EE.SRCMB.S16.QACC): sat16 of the
 * lane shifted by `shift` (a register operand, not an immediate), exact while dist2 <= 32767 << shift --
 * for callers that live in a Q-format radius. It processes whole groups of eight only and RETURNS the
 * number of pairs written ((n_points & ~7)), so nothing is silently dropped; out is int16 per pair.
 * All buffers 16-byte aligned: the 128-bit PIE forms zero the low four address bits (TRM p49), so a
 * misaligned pointer is not reported, it silently reads or writes the neighbouring bytes. */
void ex12_integrate(const int32_t *pos, const int32_t *vel, const int32_t *acc, int32_t *out,
                    int32_t lo, int32_t hi);
void ex12_sat_masks(const int16_t *boxes, const int16_t *query, int16_t *out, uint32_t n_boxes);
void ex12_dist2_qacc(const int16_t *pt_a, const int16_t *pt_b, int32_t *out, uint32_t n_points);
uint32_t ex12_dist2_q16(const int16_t *pt_a, const int16_t *pt_b, int16_t *out, uint32_t n_points,
                        uint32_t shift);

#endif
