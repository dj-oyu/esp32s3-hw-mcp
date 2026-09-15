/* ex05 -- the accumulator's edges: ACCX is 40-bit and saturating, and EE.SRS.ACCX saturates into 32 bits.
 *
 * `EE.VMULAS.S16.ACCX qx, qy` adds the eight 16x16 products of the lanes to ACCX, saturating to 40 bits
 * (operation pseudo-code: sum[40:0] = ACCX + add0 + ... + add7, clamped to -2^39 .. 2^39-1). Eight
 * products of 32000*32000 are 8.192e9 per instruction, so enough iterations walk a positive accumulator
 * into the clamp. Then the two readouts show both clamps at once:
 *
 *   EE.SRS.ACCX a10, a8(0), 0 : au = saturate_to_int32(ACCX)          -> the 32-bit clamp
 *   EE.SRS.ACCX a11, a9(9), 0 : au = ACCX >> 9 (exact, fits)          -> the 40-bit value, scaled
 *
 * a11 * 512 must reproduce the mathematical sum, and a10 must be exactly 2^31-1: the numbers in the log
 * are what make "it silently clamps instead of wrapping" a checked statement rather than a warning.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex05_accx_saturation(int iterations, const int16_t *lanes8, int32_t *out2)
 *   out2[0] = saturate_to_int32(ACCX)   (32-bit clamp, shift 0)
 *   out2[1] = ACCX >> 9                 (exact: the accumulator is positive and 2^39 >> 9 = 2^30)
 */
    .align 4
    .global ex05_accx_saturation
    .type ex05_accx_saturation,@function
ex05_accx_saturation:
    entry a1, 64
    /* ABI: this kernel writes a10, a11 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    mov a6, a3                      /* a6 = the eight 16-bit lanes to multiply */
    mov a7, a4                      /* a7 = out2 */
    EE.VLD.128.IP q0, a6, 0
    movi a8, 0                      /* shift 0 -> 32-bit readout */
    movi a9, 9                      /* shift 9 -> exact 40-bit scaled readout */
    EE.ZERO.ACCX
.Lsat:
    EE.VMULAS.S16.ACCX q0, q0
    addi a2, a2, -1
    bnez a2, .Lsat
    EE.SRS.ACCX a10, a8, 0
    s32i a10, a7, 0
    EE.SRS.ACCX a11, a9, 0
    s32i a11, a7, 4
    /* ABI: put the caller's a2/a3 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    retw.n
    .size ex05_accx_saturation, .-ex05_accx_saturation
