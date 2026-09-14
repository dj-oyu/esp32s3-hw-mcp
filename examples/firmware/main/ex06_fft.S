/* ex06 -- the FFT instruction set's two primitives, each checked against the manual's own pseudo-code.
 *
 * `EE.FFT.R2BF.S16 qa0, qa1, qx, qy, sel2` is a lane-wise radix-2 butterfly with a selectable packing:
 *
 *   sel2 = 0 : op_a = {qy[63:0],  qx[63:0]}    op_b = {qy[127:64], qx[127:64]}
 *   sel2 = 1 : op_a = {qy[95:64], qy[31:0], qx[95:64], qx[31:0]}
 *              op_b = {qy[127:96], qy[63:32], qx[127:96], qx[63:32]}
 *   qa0 low half  = op_a[0..3] + op_b[0..3]    qa0 high half = op_a[0..3] - op_b[0..3]
 *   qa1 low half  = op_a[4..7] + op_b[4..7]    qa1 high half = op_a[4..7] - op_b[4..7]
 *
 * With qx = qy = one 128-bit register holding eight int16 samples, sel2 = 0 therefore computes, per lane,
 * (x[j] + x[j+4], x[j] - x[j+4]) for j = 0..3 -- the first radix-2 stage of a real 8-point transform, which
 * is exactly the kind of claim the examples exist to check on silicon instead of on paper.
 *
 * `EE.CMUL.S16 qz, qx, qy, sel4` is two complex multiplies on interleaved (re, im) lane pairs, with the
 * result arithmetically shifted right by SAR[5:0] (SAR is the standard Xtensa shift-amount register, set
 * here with `ssr`):  re = ac - bd,  im = ad + bc.  sel4 = 0 works on lanes 0..3, sel4 = 1 on lanes 4..7
 * (the operand is an immediate, hence two functions).
 *
 * Both are register-only instructions, so this file has no memory stepping to reason about -- the checks in
 * main.c compare lanes against a scalar reference written straight from the pseudo-code above.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex06_r2bf_sel0(const int16_t *x8, int16_t *out8) */
    .align 4
    .global ex06_r2bf_sel0
    .type ex06_r2bf_sel0,@function
ex06_r2bf_sel0:
    entry a1, 32
    EE.VLD.128.IP q0, a2, 0
    EE.FFT.R2BF.S16 q1, q2, q0, q0, 0
    EE.VST.128.IP q1, a3, 0
    retw.n
    .size ex06_r2bf_sel0, .-ex06_r2bf_sel0

/* void ex06_r2bf_sel1(const int16_t *x8, int16_t *out8) */
    .align 4
    .global ex06_r2bf_sel1
    .type ex06_r2bf_sel1,@function
ex06_r2bf_sel1:
    entry a1, 32
    EE.VLD.128.IP q0, a2, 0
    EE.FFT.R2BF.S16 q1, q2, q0, q0, 1
    EE.VST.128.IP q1, a3, 0
    retw.n
    .size ex06_r2bf_sel1, .-ex06_r2bf_sel1

/* void ex06_cmul_half0(const int16_t *x, const int16_t *y, int16_t *out, uint32_t sar) */
    .align 4
    .global ex06_cmul_half0
    .type ex06_cmul_half0,@function
ex06_cmul_half0:
    entry a1, 32
    ssr a5                                  /* SAR = sar & 31 */
    EE.VLD.128.IP q0, a2, 0
    EE.VLD.128.IP q1, a3, 0
    EE.CMUL.S16 q2, q0, q1, 0
    EE.VST.128.IP q2, a4, 0
    retw.n
    .size ex06_cmul_half0, .-ex06_cmul_half0

/* void ex06_cmul_half1(const int16_t *x, const int16_t *y, int16_t *out, uint32_t sar) */
    .align 4
    .global ex06_cmul_half1
    .type ex06_cmul_half1,@function
ex06_cmul_half1:
    entry a1, 32
    ssr a5
    EE.VLD.128.IP q0, a2, 0
    EE.VLD.128.IP q1, a3, 0
    EE.CMUL.S16 q2, q0, q1, 1
    EE.VST.128.IP q2, a4, 0
    retw.n
    .size ex06_cmul_half1, .-ex06_cmul_half1
