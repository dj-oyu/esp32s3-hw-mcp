/* ex02 -- 16x16 int16 matrix multiply with the PIE accumulator.
 *
 * C[i][j] = sum(k = 0..15) A[i][k] * Bt[j][k], with Bt = transpose(B) so both operands of the inner loop
 * are contiguous 128-bit chunks. That is what the PIE data path wants: `EE.VMULAS.S16.ACCX qx, qy`
 * multiplies 8 lanes of 16-bit values and **sums the 8 products into ACCX** (one 40-bit, saturating
 * accumulator), so two of them (8 + 8 lanes) complete a 16-element dot product:
 *
 *   1 ACCX            = 0                              (EE.ZERO.ACCX)
 *   2 ACCX           += sum(q0[l] * q2[l] for l)       (EE.VMULAS.S16.ACCX)
 *   3 ACCX           += sum(q1[l] * q3[l] for l)       (EE.VMULAS.S16.ACCX)
 *   4 a5              = saturate_to_int32(ACCX >> 0)   (EE.SRS.ACCX a5, a12, 0)
 *
 * Two details that matter and are easy to get wrong:
 *   - `EE.VMULAS.S16.QACC` is a *different* instruction: it keeps 8 separate 40-bit lane accumulators
 *     (useful for 8 independent dot products), where the .ACCX form sums the lanes.
 *   - `EE.SRS.ACCX au, as, 0` shifts ACCX right by as[5:0] *and writes the shifted value back* to ACCX,
 *     then saturates into a 32-bit register. a12 holds the shift (0 here).
 *
 * Table 1.7-2 does not list ACCX as written by EE.VMULAS (its row shows only qx/qy as used), so the stall
 * model predicts nothing for step 3 -> 4; the example measures the loop so the gap is visible as a number
 * rather than as a surprise (see examples/README.md and the checker's cycle report).
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex02_matmul16(const int16_t *a, const int16_t *bt, int32_t *c) */
    .align 4
    .global ex02_matmul16
    .type ex02_matmul16,@function
ex02_matmul16:
    entry a1, 32
    movi a12, 0                     /* shift for EE.SRS.ACCX */
    movi a6, 16                     /* 16 rows of A */
.Lrow:
    mov a8, a2                      /* a8 -> A[i][0..7]   */
    addi a9, a8, 16                 /* a9 -> A[i][8..15]  */
    mov a10, a3                     /* a10 -> Bt[0][0..7] */
    addi a11, a10, 16               /* a11 -> Bt[0][8..15] */
    movi a7, 16                     /* 16 columns of Bt */
.Lcol:
    EE.VLD.128.IP q0, a8, 0
    EE.VLD.128.IP q1, a9, 0
    EE.VLD.128.IP q2, a10, 0
    EE.VLD.128.IP q3, a11, 0
    EE.ZERO.ACCX
    EE.VMULAS.S16.ACCX q0, q2
    EE.VMULAS.S16.ACCX q1, q3
    EE.SRS.ACCX a5, a12, 0
    s32i a5, a4, 0
    addi a4, a4, 4
    addi a10, a10, 32
    addi a11, a11, 32
    addi a7, a7, -1
    bnez a7, .Lcol
    addi a2, a2, 32
    addi a6, a6, -1
    bnez a6, .Lrow
    retw.n
    .size ex02_matmul16, .-ex02_matmul16
