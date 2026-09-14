/* ex09 -- the QACC accumulate -> readout hazard, measured instead of assumed.
 *
 * ex07 (the 4x4 transform) came back from the device with rows the manual's pseudo-code does not predict,
 * and ex07's sequence is exactly this: four `EE.VSMULAS.S16.QACC` into the same accumulator followed
 * immediately by `EE.SRCMB.S16.QACC`. Table 1.7-2 lists no `def` for the accumulator on the VMULAS family,
 * so a static stall calculation predicts zero cycles between the MAC and its consumer. This file is the
 * instrument that decides whether the hardware agrees -- the same two-reference discipline the rest of the
 * suite uses, applied to a question the documents do not answer:
 *
 *   1. one MAC, then the readout at issue distance 0..4     -> how many slots the readout must wait
 *   2. one MAC with sel8 = 0..3 at a safe distance          -> which operand lane is broadcast
 *   3. four MACs (ex07's row) at issue distance 0, 2, 4     -> the shape ex07 actually hit
 *   4. the raw 40-bit lanes read through RUR (QACC_L_0..4, QACC_H_0..4)  -> the lane layout, no readout
 *   5. ZERO.QACC between two MACs                           -> is the zero ordered before the next MAC?
 *   6. SRCMB twice with different shifts                    -> does the readout write the accumulator back?
 *
 * Every function takes (v, coef, out, shift) in a2..a5 unless its comment says otherwise; buffers are
 * 16-byte aligned by the caller. `gap` is a count of `nop.n` slots between the MAC and the readout, and the
 * .macro bodies are instantiations so each value of `sel`/`gap` is a real immediate in the encoding.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* One MAC (sel8 = imm), then `gap` slot(s), then the readout into out[0..7]. */
.macro MAC1 fname, sel, gap
    .align 4
    .global \fname
    .type \fname,@function
\fname:
    entry a1, 32
    mov a12, a5                     /* shift amount for EE.SRCMB (from a register, not SAR) */
    EE.ZERO.QACC
    EE.VLD.128.IP q1, a2, 0
    EE.VLD.128.IP q0, a3, 0
    EE.VSMULAS.S16.QACC q1, q0, \sel
    .rept \gap
    nop.n
    .endr
    EE.SRCMB.S16.QACC q5, a12, 0
    EE.VST.128.IP q5, a4, 0
    retw.n
    .size \fname, .-\fname
.endm

/* Four MACs (the ex07 row), then `gap` slots, then the readout. a2 = coef row, a3 = v (4 rows of 8). */
.macro MAC4 fname, gap
    .align 4
    .global \fname
    .type \fname,@function
\fname:
    entry a1, 32
    mov a12, a5
    EE.ZERO.QACC
    EE.VLD.128.IP q0, a2, 0
    EE.VLD.128.IP q1, a3, 16
    EE.VSMULAS.S16.QACC q1, q0, 0
    EE.VLD.128.IP q2, a3, 16
    EE.VSMULAS.S16.QACC q2, q0, 1
    EE.VLD.128.IP q3, a3, 16
    EE.VSMULAS.S16.QACC q3, q0, 2
    EE.VLD.128.IP q4, a3, 16
    EE.VSMULAS.S16.QACC q4, q0, 3
    .rept \gap
    nop.n
    .endr
    EE.SRCMB.S16.QACC q5, a12, 0
    EE.VST.128.IP q5, a4, 0
    retw.n
    .size \fname, .-\fname
.endm

/* `rows` output rows, each = one MAC per coefficient lane; a2 = coefs (rows x 4 int16), a3 = v, a4 = out,
 * a5 = shift, a6 = rows. `gap` slots between the last MAC and this row's readout -- the loop is ex07's
 * shape, so the difference between gap 0 and gap 4 is the whole finding. */
.macro CHAIN fname, gap
    .align 4
    .global \fname
    .type \fname,@function
\fname:
    entry a1, 32
    mov a12, a5
    mov a7, a6                      /* rows remaining */
.Lc\fname:
    beqz a7, .Lce\fname
    EE.ZERO.QACC
    mov a6, a3
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a6, 16
    EE.VSMULAS.S16.QACC q1, q0, 0
    EE.VLD.128.IP q2, a6, 16
    EE.VSMULAS.S16.QACC q2, q0, 1
    EE.VLD.128.IP q3, a6, 16
    EE.VSMULAS.S16.QACC q3, q0, 2
    EE.VLD.128.IP q4, a6, 16
    EE.VSMULAS.S16.QACC q4, q0, 3
    .rept \gap
    nop.n
    .endr
    EE.SRCMB.S16.QACC q5, a12, 0
    EE.VST.128.IP q5, a4, 16
    addi a7, a7, -1
    j .Lc\fname
.Lce\fname:
    retw.n
    .size \fname, .-\fname
.endm

MAC1 ex09_mac1_g0, 0, 0
MAC1 ex09_mac1_g1, 0, 1
MAC1 ex09_mac1_g2, 0, 2
MAC1 ex09_mac1_g3, 0, 3
MAC1 ex09_mac1_g4, 0, 4
MAC1 ex09_mac1_g6, 0, 6
MAC1 ex09_mac1_s1_g4, 1, 4
MAC1 ex09_mac1_s2_g4, 2, 4
MAC1 ex09_mac1_s3_g4, 3, 4

MAC4 ex09_mac4_g0, 0
MAC4 ex09_mac4_g2, 2
MAC4 ex09_mac4_g4, 4

CHAIN ex09_chain_g0, 0
CHAIN ex09_chain_g4, 4

/* void ex09_raw_qacc(const int16_t *v8, const int16_t *coef8, uint32_t *out10, uint32_t shift)
 *   one MAC (sel8 = 0) at a safe distance, then QACC_L_0..4 and QACC_H_0..4 read out raw: lane i is
 *   (QACC_H_i[7:0] << 32) | QACC_L_i, with lanes 0..3 in QACC_L and 4..7 in QACC_H. No SRCMB anywhere, so
 *   this reads the accumulator as the MACs left it. */
    .align 4
    .global ex09_raw_qacc
    .type ex09_raw_qacc,@function
ex09_raw_qacc:
    entry a1, 32
    EE.ZERO.QACC
    EE.VLD.128.IP q1, a2, 0
    EE.VLD.128.IP q0, a3, 0
    EE.VSMULAS.S16.QACC q1, q0, 0
    nop.n
    nop.n
    nop.n
    nop.n
    RUR.QACC_L_0 a6
    RUR.QACC_L_1 a7
    RUR.QACC_L_2 a8
    RUR.QACC_L_3 a9
    RUR.QACC_L_4 a10
    RUR.QACC_H_0 a11
    RUR.QACC_H_1 a12
    RUR.QACC_H_2 a13
    RUR.QACC_H_3 a14
    RUR.QACC_H_4 a15
    s32i a6, a4, 0
    s32i a7, a4, 4
    s32i a8, a4, 8
    s32i a9, a4, 12
    s32i a10, a4, 16
    s32i a11, a4, 20
    s32i a12, a4, 24
    s32i a13, a4, 28
    s32i a14, a4, 32
    s32i a15, a4, 36
    retw.n
    .size ex09_raw_qacc, .-ex09_raw_qacc

/* void ex09_zero_vis(const int16_t *v8, const int16_t *coef8, int16_t *out8, uint32_t shift)
 *   MAC with the sel8 = 0 coefficient, then ZERO.QACC, then a MAC with the sel8 = 1 coefficient: the readout
 *   is v*coef[1] if the zero is ordered before the second MAC, and v*(coef[0]+coef[1]) if it is not. */
    .align 4
    .global ex09_zero_vis
    .type ex09_zero_vis,@function
ex09_zero_vis:
    entry a1, 32
    mov a12, a5
    EE.ZERO.QACC
    EE.VLD.128.IP q1, a2, 0
    EE.VLD.128.IP q0, a3, 0
    EE.VSMULAS.S16.QACC q1, q0, 0
    EE.ZERO.QACC
    EE.VSMULAS.S16.QACC q1, q0, 1
    nop.n
    nop.n
    nop.n
    nop.n
    EE.SRCMB.S16.QACC q5, a12, 0
    EE.VST.128.IP q5, a4, 0
    retw.n
    .size ex09_zero_vis, .-ex09_zero_vis

/* void ex09_rows_raw(const int16_t *coefs, const int16_t *v, uint32_t *out, uint32_t rows)
 *   The row loop with NO readout at all: per row, one coefficient row and four vertex rows are MAC'd into
 *   QACC, and then the accumulator is read out raw through RUR (the ten words of QACC_L+QACC_H) and stored
 *   as ten words per row. No SRCMB anywhere, so whatever the accumulators hold is what the MACs consumed --
 *   this separates "the MACs used the wrong data" from "the readout showed the wrong instant". */
    .align 4
    .global ex09_rows_raw
    .type ex09_rows_raw,@function
ex09_rows_raw:
    entry a1, 32
    mov a7, a5                      /* rows remaining */
.Lrr:
    beqz a7, .Lrr_end
    EE.ZERO.QACC
    mov a6, a3
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a6, 16
    EE.VSMULAS.S16.QACC q1, q0, 0
    EE.VLD.128.IP q2, a6, 16
    EE.VSMULAS.S16.QACC q2, q0, 1
    EE.VLD.128.IP q3, a6, 16
    EE.VSMULAS.S16.QACC q3, q0, 2
    EE.VLD.128.IP q4, a6, 16
    EE.VSMULAS.S16.QACC q4, q0, 3
    nop.n
    nop.n
    nop.n
    nop.n
    RUR.QACC_L_0 a8
    RUR.QACC_L_1 a9
    RUR.QACC_L_2 a10
    RUR.QACC_L_3 a11
    RUR.QACC_L_4 a12
    s32i a8, a4, 0
    s32i a9, a4, 4
    s32i a10, a4, 8
    s32i a11, a4, 12
    s32i a12, a4, 16
    RUR.QACC_H_0 a8
    RUR.QACC_H_1 a9
    RUR.QACC_H_2 a10
    RUR.QACC_H_3 a11
    RUR.QACC_H_4 a12
    s32i a8, a4, 20
    s32i a9, a4, 24
    s32i a10, a4, 28
    s32i a11, a4, 32
    s32i a12, a4, 36
    addi a4, a4, 40
    addi a7, a7, -1
    j .Lrr
.Lrr_end:
    retw.n
    .size ex09_rows_raw, .-ex09_rows_raw

/* void ex09_rows_unrolled(const int16_t *coefs, const int16_t *v, int16_t *out, uint32_t shift)
 *   The same four rows with no loop and no branch: four copies of the row body, each with its own readout.
 *   Comparing this against the looped version says whether the branch/loop tail is part of the effect. */
    .align 4
    .global ex09_rows_unrolled
    .type ex09_rows_unrolled,@function
ex09_rows_unrolled:
    entry a1, 32
    mov a12, a5
    mov a6, a3
    movi a11, 4
.Lru:
    EE.ZERO.QACC
    mov a3, a6
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.VSMULAS.S16.QACC q1, q0, 0
    EE.VLD.128.IP q2, a3, 16
    EE.VSMULAS.S16.QACC q2, q0, 1
    EE.VLD.128.IP q3, a3, 16
    EE.VSMULAS.S16.QACC q3, q0, 2
    EE.VLD.128.IP q4, a3, 16
    EE.VSMULAS.S16.QACC q4, q0, 3
    nop.n
    nop.n
    nop.n
    nop.n
    EE.SRCMB.S16.QACC q5, a12, 0
    EE.VST.128.IP q5, a4, 16
    addi a11, a11, -1
    bnez a11, .Lru
    retw.n
    .size ex09_rows_unrolled, .-ex09_rows_unrolled

/* void ex09_ipwalk(const int16_t *src, int16_t *dst, uint32_t rows)
 *   No MAC, no accumulator: per iteration one `EE.VLD.128.IP q0, a2, 16` and one `EE.VST.128.IP q0, a3, 16`.
 *   If the rows come out in order, the .IP address walk itself is sound in a loop and the oddity above is in
 *   the accumulate/readout path; if they do not, the walk is what moves. */
    .align 4
    .global ex09_ipwalk
    .type ex09_ipwalk,@function
ex09_ipwalk:
    entry a1, 32
.Liw:
    beqz a4, .Liw_end
    EE.VLD.128.IP q0, a2, 16
    EE.VST.128.IP q0, a3, 16
    addi a4, a4, -1
    j .Liw
.Liw_end:
    retw.n
    .size ex09_ipwalk, .-ex09_ipwalk

/* void ex09_ipwalk4(const int16_t *src, int16_t *dst, uint32_t rows)
 *   The same, but four back-to-back .IP loads on the SAME address register per iteration -- the shape the
 *   vertex loads use inside the row loop. */
    .align 4
    .global ex09_ipwalk4
    .type ex09_ipwalk4,@function
ex09_ipwalk4:
    entry a1, 32
.Liw4:
    beqz a4, .Liw4_end
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a2, 16
    EE.VLD.128.IP q2, a2, 16
    EE.VLD.128.IP q3, a2, 16
    EE.VST.128.IP q0, a3, 16
    EE.VST.128.IP q1, a3, 16
    EE.VST.128.IP q2, a3, 16
    EE.VST.128.IP q3, a3, 16
    addi a4, a4, -1
    j .Liw4
.Liw4_end:
    retw.n
    .size ex09_ipwalk4, .-ex09_ipwalk4

/* void ex09_mac_fixed(const int16_t *coef_row, const int16_t *v, int16_t *out, uint32_t rows, uint32_t shift)
 *   The row loop with the coefficient register loaded ONCE, outside the loop: the same four MACs, the same
 *   readout, the same vertex walk. Rows that are not all equal to the model here mean the loop body itself
 *   (accumulate across iterations, or the readout) is what changes the answer. */
    .align 4
    .global ex09_mac_fixed
    .type ex09_mac_fixed,@function
ex09_mac_fixed:
    entry a1, 32
    mov a12, a6                     /* shift */
    mov a7, a3                      /* vertex base */
    EE.VLD.128.IP q0, a2, 0         /* coefficients: loaded once */
.Lfx:
    beqz a5, .Lfx_end
    EE.ZERO.QACC
    mov a3, a7
    EE.VLD.128.IP q1, a3, 16
    EE.VSMULAS.S16.QACC q1, q0, 0
    EE.VLD.128.IP q2, a3, 16
    EE.VSMULAS.S16.QACC q2, q0, 1
    EE.VLD.128.IP q3, a3, 16
    EE.VSMULAS.S16.QACC q3, q0, 2
    EE.VLD.128.IP q4, a3, 16
    EE.VSMULAS.S16.QACC q4, q0, 3
    nop.n
    nop.n
    nop.n
    nop.n
    EE.SRCMB.S16.QACC q5, a12, 0
    EE.VST.128.IP q5, a4, 16
    addi a5, a5, -1
    j .Lfx
.Lfx_end:
    retw.n
    .size ex09_mac_fixed, .-ex09_mac_fixed

/* void ex09_srcmb_wb(const int16_t *v8, const int16_t *coef8, int16_t *out_a, int16_t *out_b,
 *                    uint32_t shift_a, uint32_t shift_b)
 *   one MAC, then SRCMB with shift_a into out_a and SRCMB again with shift_b into out_b. If the readout
 *   writes the accumulator back (as the manual's Operation field says), out_b is the twice-shifted value;
 *   if it does not, out_b is the original accumulator shifted by shift_b only. One run decides it. */
    .align 4
    .global ex09_srcmb_wb
    .type ex09_srcmb_wb,@function
ex09_srcmb_wb:
    entry a1, 32
    mov a10, a6                     /* shift_a */
    mov a11, a7                     /* shift_b */
    EE.ZERO.QACC
    EE.VLD.128.IP q1, a2, 0
    EE.VLD.128.IP q0, a3, 0
    EE.VSMULAS.S16.QACC q1, q0, 0
    nop.n
    nop.n
    nop.n
    nop.n
    EE.SRCMB.S16.QACC q5, a10, 0
    EE.VST.128.IP q5, a4, 0
    nop.n
    nop.n
    nop.n
    nop.n
    EE.SRCMB.S16.QACC q6, a11, 0
    EE.VST.128.IP q6, a5, 0
    retw.n
    .size ex09_srcmb_wb, .-ex09_srcmb_wb
