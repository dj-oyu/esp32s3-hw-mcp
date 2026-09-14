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

#endif
