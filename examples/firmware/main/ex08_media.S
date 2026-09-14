/* ex08 -- framebuffer effects on 128-bit chunks: the media half of the tuning story.
 *
 * Every kernel here works on eight 16-bit lanes at a time, with the exact pseudo-code semantics that the
 * examples suite has already pinned down on silicon:
 *
 *   EE.ANDQ   qa, qx, qy          qa = qx & qy
 *   EE.VMUL.S16 qz, qx, qy        qz[lane] = (signed qx[lane] * qy[lane]) >> SAR[5:0]   (ARITHMETIC shift)
 *   EE.VMUL.U16 qz, qx, qy        qz[lane] = (unsigned qx[lane] * qy[lane]) >> SAR[5:0] (LOGICAL shift)
 *   EE.VADDS.S16 qa, qx, qy       qa[lane] = sat16(qx[lane] + qy[lane])
 *   EE.VMIN.S16 / EE.VMAX.S16     per-lane clamp
 *   EE.SRC.Q  qa, qs0, qs1        qa = {qs1, qs0} >> (SAR_BYTE*8): the unaligned window (first operand is
 *                                 qs0 -- measured, see data/pie_examples_measured.json)
 *
 * The signed/unsigned split is not cosmetic, and it is the first run of this file that proved it: with a
 * register of ones and SAR = 1, `EE.VMUL.S16` is an arithmetic shift of the SIGNED lane, so a lane with
 * bit 15 set comes back with its sign extended (0x8AD0 & 0xF7DE >> 1 = 0xC168, not 0x4168), while
 * `EE.VMUL.U16` is the logical shift the RGB565 mask-add actually wants. ex08_shift_sign puts both
 * instructions on the same lanes so the difference is in the log rather than in a comment.
 *
 * Note also that there is no SRAQ in the set: the multiply-by-ones is how a per-lane right shift is spelled,
 * and `EE.VMUL` TRUNCATES (it does not saturate) -- so an oversized tint wraps instead of clamping, which
 * ex08_tint's reference now reproduces instead of pretending otherwise.
 *
 * The intended consumers on a Cardputer-scale display: alpha/ghost blending of two rows (half_blend is the
 * classic RGB565 mask-shift-add), additive glow (brighten), shade/tint passes (tint), and keeping a shading
 * pass inside a range (clamp). Each kernel's C equivalent is timed in main.c, because the point of the
 * exercise is the cycles per pixel, not just the answer.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex08_half_blend(const int16_t *a, const int16_t *b, const int16_t *mask8, const int16_t *ones8,
 *                      int16_t *out, int n_pixels)
 *   out[i] = ((a[i] & 0xF7DE) >> 1) + ((b[i] & 0xF7DE) >> 1)        8 UNSIGNED pixels per iteration
 * mask8 and ones8 are eight identical lanes supplied by the caller: keeping the constants in C avoids an
 * l32r from .iram1 into .rodata, which the linker refuses (literal target out of range).
 * The shift is `EE.VMUL.U16` and not `.S16`: these lanes are packed RGB565, i.e. unsigned, and the signed
 * form sign-extends a lane whose bit 15 is set. */
    .align 4
    .global ex08_half_blend
    .type ex08_half_blend,@function
ex08_half_blend:
    entry a1, 32
    srli a8, a7, 3                  /* a7 = n_pixels -> iterations of eight */
    movi a9, 1
    ssr a9                          /* SAR = 1, read by EE.VMUL.U16 */
    EE.VLD.128.IP q6, a4, 0
    EE.VLD.128.IP q7, a5, 0
    beqz a8, .Lhb_done
.Lhb:
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.ANDQ q2, q0, q6
    EE.VMUL.U16 q2, q2, q7          /* (a & mask) >> 1, logical */
    EE.ANDQ q3, q1, q6
    EE.VMUL.U16 q3, q3, q7
    EE.VADDS.S16 q4, q2, q3
    EE.VST.128.IP q4, a6, 16
    addi a8, a8, -1
    bnez a8, .Lhb
.Lhb_done:
    retw.n
    .size ex08_half_blend, .-ex08_half_blend

/* void ex08_shift_sign(const int16_t *a, const int16_t *ones8, int16_t *out_signed, int16_t *out_unsigned,
 *                      uint32_t shift)
 *   The same shift of the same lanes, once with EE.VMUL.S16 and once with EE.VMUL.U16, by a register of
 *   ones. The two differ exactly on the lanes whose bit 15 is set: the signed form shifts in the sign. */
    .align 4
    .global ex08_shift_sign
    .type ex08_shift_sign,@function
ex08_shift_sign:
    entry a1, 32
    ssr a6
    EE.VLD.128.IP q7, a3, 0
    EE.VLD.128.IP q0, a2, 0
    EE.VMUL.S16 q1, q0, q7
    EE.VMUL.U16 q2, q0, q7
    EE.VST.128.IP q1, a4, 0
    EE.VST.128.IP q2, a5, 0
    retw.n
    .size ex08_shift_sign, .-ex08_shift_sign

/* void ex08_brighten(const int16_t *a, const int16_t *b, int16_t *out, int n_pixels)
 *   out[i] = sat16(a[i] + b[i])       (additive glow; saturating, never wraps) */
    .align 4
    .global ex08_brighten
    .type ex08_brighten,@function
ex08_brighten:
    entry a1, 32
    srli a8, a5, 3
    beqz a8, .Lbr_done
.Lbr:
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.VADDS.S16 q2, q0, q1
    EE.VST.128.IP q2, a4, 16
    addi a8, a8, -1
    bnez a8, .Lbr
.Lbr_done:
    retw.n
    .size ex08_brighten, .-ex08_brighten

/* void ex08_clamp(const int16_t *a, const int16_t *lo8, const int16_t *hi8, int16_t *out, int n_pixels)
 *   out[i] = min(max(a[i], lo), hi)      (lo and hi are eight identical lanes provided by the caller) */
    .align 4
    .global ex08_clamp
    .type ex08_clamp,@function
ex08_clamp:
    entry a1, 32
    srli a8, a6, 3
    EE.VLD.128.IP q6, a3, 0
    EE.VLD.128.IP q7, a4, 0
    beqz a8, .Lcl_done
.Lcl:
    EE.VLD.128.IP q0, a2, 16
    EE.VMIN.S16 q1, q0, q7
    EE.VMAX.S16 q2, q1, q6
    EE.VST.128.IP q2, a5, 16
    addi a8, a8, -1
    bnez a8, .Lcl
.Lcl_done:
    retw.n
    .size ex08_clamp, .-ex08_clamp

/* void ex08_tint(const int16_t *a, const int16_t *tint8, int16_t *out, int n_pixels, uint32_t shift)
 *   out[i] = (int16_t)((a[i] * tint) >> shift)   -- 16-bit lanes, TRUNCATED: EE.VMUL does not saturate, so
 *   a tint that pushes the product past 32767 wraps instead of clamping (measured; the C reference in
 *   main.c reproduces the wrap, and a saturating variant would need an explicit EE.VADDS/VMAX pass). */
    .align 4
    .global ex08_tint
    .type ex08_tint,@function
ex08_tint:
    entry a1, 32
    srli a8, a5, 3
    ssr a6                                  /* SAR = shift, read by EE.VMUL.S16 */
    EE.VLD.128.IP q6, a3, 0
    beqz a8, .Lti_done
.Lti:
    EE.VLD.128.IP q0, a2, 16
    EE.VMUL.S16 q1, q0, q6
    EE.VST.128.IP q1, a4, 16
    addi a8, a8, -1
    bnez a8, .Lti
.Lti_done:
    retw.n
    .size ex08_tint, .-ex08_tint
