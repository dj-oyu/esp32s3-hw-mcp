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

#endif
