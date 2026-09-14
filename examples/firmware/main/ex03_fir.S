/* ex03 -- 16-tap Q15 FIR, the same accumulator path as ex02 but over a sliding window.
 *
 * y[n] = sat32( (sum(k = 0..15) x[n + k] * h[k]) >> shift )
 *
 * The shift is a parameter because it is the interesting part on this data path: EE.SRS.ACCX takes the
 * shift from a *register* (as[5:0]), so one kernel can produce both the raw accumulator and the Q15
 * result, and the two are compared against the scalar reference in C and in Python.
 *
 * The coefficients are loaded once (outside the loop); the signal window slides by 2 bytes per output
 * sample, and the two 128-bit loads of the window are done from freshly computed addresses every
 * iteration, which is what EE.VLD.128.IP with an immediate of 0 does (no post-increment: the address
 * register is left alone, so the pointer bookkeeping stays explicit and readable).
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex03_fir16(const int16_t *x, int n_out, const int16_t *h, uint32_t shift, int32_t *y)
 *   a2 = x, a3 = n_out, a4 = h, a5 = shift, a6 = y */
    .align 4
    .global ex03_fir16
    .type ex03_fir16,@function
ex03_fir16:
    entry a1, 32
    mov a12, a5                     /* shift for EE.SRS.ACCX */
    mov a11, a2                     /* window pointer */
    EE.VLD.128.IP q0, a4, 0         /* q0 = h[0..7]  */
    addi a10, a4, 16
    EE.VLD.128.IP q1, a10, 0        /* q1 = h[8..15] */
.Lfir:
    mov a9, a11
    addi a10, a9, 16
    EE.VLD.128.IP q2, a9, 0         /* x[n..n+7]   */
    EE.VLD.128.IP q3, a10, 0        /* x[n+8..n+15] */
    EE.ZERO.ACCX
    EE.VMULAS.S16.ACCX q0, q2
    EE.VMULAS.S16.ACCX q1, q3
    EE.SRS.ACCX a9, a12, 0
    s32i a9, a6, 0
    addi a6, a6, 4
    addi a11, a11, 2
    addi a3, a3, -1
    bnez a3, .Lfir
    retw.n
    .size ex03_fir16, .-ex03_fir16
