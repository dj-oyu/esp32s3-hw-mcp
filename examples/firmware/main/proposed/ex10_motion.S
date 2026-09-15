/* ex10 -- motion video / sprite motion: a block SAD and a half-pel interpolated row.
 *
 * Two primitives a video decoder's motion stage is built from, both on eight 16-bit lanes per iteration:
 *
 *   A) ex10_sad8     : block sum of absolute differences, 8 uint16 lanes per block, accumulated in ACCX
 *                      with EE.VMULAS.U16.ACCX against a register of ones. The 40-bit total is read out
 *                      ONCE at the end through RUR.ACCX_0 / RUR.ACCX_1, so the C side can report it.
 *   B) ex10_halfpel  : the half-pel interpolated row  out[i] = (uint16_t)(a[i] + b[i] + 1) >> 1
 *
 * There is no SAD and no ABS instruction in the 220-instruction PIE set, so |a - b| is spelled
 * EE.VSUBS.S16(EE.VMAX.S16(a,b), EE.VMIN.S16(a,b)) -- subtract the per-lane minimum from the per-lane
 * maximum. Note there is no unsigned VMAX/VMIN/VSUBS either: only the .S16 forms exist, and they are what
 * makes the exactness limits in the two comments below.
 *
 *   EE.VMAX.S16   qa, qx, qy   qa[l] = (sx[l] >= sy[l]) ? sx[l] : sy[l]        (signed compare)  TRM p180
 *   EE.VMIN.S16   qa, qx, qy   qa[l] = (sx[l] <= sy[l]) ? sx[l] : sy[l]        (signed compare)  TRM p189
 *   EE.VSUBS.S16  qa, qx, qy   qa[l] = sat16(sx[l] - sy[l])                   (SATURATING)      TRM p281
 *   EE.VMULAS.U16.ACCX qx, qy  ACCX = clamp(ACCX + sum_l u16(x[l])*u16(y[l]), 0, 2^40-1)         TRM p240
 *   EE.VADDS.S16  qa, qx, qy   qa[l] = sat16(sx[l] + sy[l])                   (SATURATING)      TRM p146
 *   EE.VMUL.U16   qz, qx, qy   qz[l] = (u16(x[l]) * u16(y[l])) >> SAR[5:0]    (LOGICAL shift)   TRM p204
 *
 * The lane values here are pixel samples, not signed DSP samples: an 8-bit sample 0..255 rides in a uint16
 * lane zero-extended, and inside that domain the signed and unsigned readings of a lane agree -- which is
 * why 8-bit sample data is the domain both kernels below are exact in, and why the comments state, per
 * kernel, exactly where that stops being true.
 *
 * Both kernels are PROPOSED: they are not wired into main.c / examples.h / CMakeLists.txt (this suite's
 * build lists its sources explicitly, so a file in proposed/ is inert until someone adds it). See
 * ex10_motion.md next to this file for the scalar C references, the Python model of the two kernels and
 * the equivalence run, and the list of semantics that were not confirmed on silicon.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex10_sad8(const uint16_t *a, const uint16_t *b, uint32_t n_blocks, uint32_t *out)
 *   a2 = a, a3 = b   : n_blocks * 8 uint16 lanes each (16-byte aligned; the PIE 128-bit forms force the
 *                      low four address bits to 0, TRM p49, so a misaligned pointer is silent)
 *   a4 = n_blocks    : number of 8-lane blocks
 *   a5 = out         : out[0] = ACCX[31:0], out[1] = ACCX[39:32]; total = ((uint64_t)out[1] << 32) | out[0]
 *
 *   total = sum over all n_blocks*8 lanes of the |a[i] - b[i]| the vector unit computes: exactly
 *           sum |a[i] - b[i]|, each difference saturating at 32767, when no lane pair straddles 32768
 *           (which is always the case for 8-bit samples); see the exactness note below.
 *           out[0]/out[1] are the two halves of that 40-bit total, so the host reports
 *           total = ((uint64_t)out[1] << 32) | out[0].
 *
 * The absolute difference is VSUBS(VMAX.S16, VMIN.S16) over the SIGNED readings of the lanes, and how exact
 * that is needs stating precisely, because the signed reading is not a no-op here. Writing A = signed(a)
 * (= a - 65536 when a >= 32768) and B = signed(b) -- the rule below is checked over 655360 lane pairs by
 * the model run in ex10_motion.md (0 counterexamples):
 *
 *   - a and b in the SAME half of the range (both <= 32767, or both >= 32768): A - B = a - b exactly, so
 *     the result is exactly |a - b|. This is the case for 8-bit luma/chroma samples zero-extended into
 *     the lanes (0..255), and for any pair that shares a half.
 *   - a and b STRADDLING 32768: A - B = (a - b) +/- 65536, whose magnitude is 65536 - |a - b| -- the
 *     complement of the difference, not |a - b|.
 *
 * EE.VSUBS.S16 then saturates at 32767, so the per-lane contribution is min(<|a - b| or 65536 - |a - b|>,
 * 32767): for 8-bit samples both caveats are unreachable (|a - b| <= 255, same half) and the SAD is exact
 * for every input. The C reference in ex10_motion.md follows the instructions -- the saturating signed
 * distance -- rather than the textbook formula, so a CHECK against it stays exact on any input; that same
 * file's model run counts the lanes where the kernel and the textbook SAD diverge instead of pretending
 * they agree.
 *
 * The accumulator: EE.VMULAS.U16.ACCX q4, q7 with q7 = eight lanes of 1 is an UNSIGNED sum of the eight
 * 16-bit lanes into the single 40-bit ACCX (one add per lane, no SAR in the pseudo-code -- unlike
 * EE.VMUL.U16, the MAC family does not shift the product). ACCX is saturated by the instruction, to
 * [0, 2^40-1] in the TRM pseudo-code (p240) where the S16 variant measured 2^39-1 on silicon; a SAD
 * cannot reach either bound: 8 lanes x 32767 = 262136 per block, i.e. ~2.1e6 blocks to fill 2^39-1.
 *
 * Readout: RUR.ACCX_0 reads ACCX[31:0] and RUR.ACCX_1 reads ACCX[39:32] zero-extended (TRM p64, section
 * 1.6.10), so the pair is the whole accumulator with no saturation and no side effect on ACCX -- which is
 * why this kernel does not use EE.SRS.ACCX: `EE.SRS.ACCX au, as, 0` writes ACCX back shifted AND clamps
 * the result into a 32-bit signed register (TRM p134), so it would silently freeze the total at 2^31-1
 * (from ~8192 blocks with full-range lanes, ~1e6 blocks with 8-bit samples). The readout happens once,
 * not per block, and the four nop.n ahead of it are a conservative gap rather than a measured one: the
 * stall table lists ACCX as a stage-2 def for EE.VMULAS.U16.ACCX (Table 1.7-2, stall-table page 71; the
 * instruction's own page is 240) but has no row at all for RUR.ACCX_* -- the RUR forms are processor
 * control instructions (TRM section 1.6.10), not EE.* ones -- so the distance between the last MAC and
 * this readout is exactly the kind of question ex09 was written to probe. Four slots cost nothing here
 * (the readout is outside the block loop) and cover the def-stage-2 case with margin.
 */
    .align 4
    .global ex10_sad8
    .type ex10_sad8,@function
ex10_sad8:
    entry a1, 32
    movi a9, 1
    slli a9, a9, 16
    addi a9, a9, 1                  /* a9 = 0x00010001: two lanes of 1 */
    EE.MOVI.32.Q q7, a9, 0          /* q7 = eight lanes of 1, built in-core: a movi of a 32-bit constant  */
    EE.MOVI.32.Q q7, a9, 1          /* would go through a literal pool (l32r), and .iram1 cannot reach     */
    EE.MOVI.32.Q q7, a9, 2          /* .rodata (the constants in the other examples come from C pointers)  */
    EE.MOVI.32.Q q7, a9, 3
    EE.ZERO.ACCX
    beqz a4, .Lsad_out
.Lsad:
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.VMAX.S16 q2, q0, q1
    EE.VMIN.S16 q3, q0, q1
    EE.VSUBS.S16 q4, q2, q3         /* |a - b| per lane, saturating at 32767 */
    EE.VMULAS.U16.ACCX q4, q7       /* ACCX += sum of the eight unsigned lanes */
    addi a4, a4, -1
    bnez a4, .Lsad
.Lsad_out:
    nop.n
    nop.n
    nop.n
    nop.n
    RUR.ACCX_0 a6                   /* ACCX[31:0]   */
    RUR.ACCX_1 a7                   /* ACCX[39:32]  */
    s32i a6, a5, 0
    s32i a7, a5, 4
    retw.n
    .size ex10_sad8, .-ex10_sad8

/* void ex10_halfpel(const int16_t *a, const int16_t *b, const int16_t *ones8, int16_t *out,
 *                   uint32_t n_lanes)
 *   a2 = a, a3 = b : n_lanes lanes of pixel data (16-byte aligned)
 *   a4 = ones8     : eight identical lanes supplied by the caller (the constant stays in C)
 *   a5 = out       : n_lanes lanes
 *   a6 = n_lanes   : a multiple of 8; trailing lanes are not touched
 *
 *   out[i] = (uint16_t)(a[i] + b[i] + 1) >> 1           (the C reference expression, cast before the shift)
 *
 * THE SEQUENCE, and why this one:
 *
 *   1  ssr(1)                          SAR = 1 for the logical shift below
 *   2  EE.VADDS.S16 q2, q0, q1         q2 = sat16(a + b)             -- the sum, SATURATING
 *   3  EE.VADDS.S16 q2, q2, q6         q2 = sat16(q2 + 1)            -- the rounding add, q6 = ones8
 *   4  EE.VMUL.U16  q2, q2, q6         q2 = (u16(q2) * 1) >> 1       -- the shift, LOGICAL (SAR = 1)
 *
 * One constant register serves both steps 3 and 4: a lane of ones is the addend 1 and the multiplier 1.
 * Step 4 is EE.VMUL.U16 and not EE.VMUL.S16 because these lanes are read as UNSIGNED after the cast in the
 * reference expression: .U16 shifts the lane's unsigned value logically, so a lane holding 0x8000..0xFFFF
 * shifts to 0x4000..0x7FFF, which is what ((uint16_t)x) >> 1 gives. .S16 would sign-extend first and the
 * high half of the lane range would come back wrong (that is the same difference ex08_shift_sign was
 * written to show). A bare right shift instruction does not exist in the PIE set, so "multiply by ones
 * with SAR = 1" is how a per-lane >> 1 is spelled.
 *
 * THE ROUNDING ADD DOES SATURATE, and here is exactly what that costs. EE.VADDS.S16 clamps to
 * [-32768, 32767] (TRM p146), so the two readings of the expression agree on exactly those lanes where
 * a + b + 1 already fits in a signed 16-bit lane, and differ on exactly those where it does not (the
 * model run in ex10_motion.md finds the mismatch set equal to that set, with nothing outside it):
 *
 *   - a + b + 1 > 32767 : the sum freezes at 32767 and step 4 yields 16383; the reference's
 *     ((uint16_t)(a + b + 1)) >> 1 keeps the 16-bit truncation and yields 16384..32767.
 *   - a + b + 1 < -32768: the sum freezes at -32767, which step 4 reads as the unsigned 32769 and shifts
 *     to 16384; the reference wraps the sum into the 16-bit range first (mod 65536) and yields a small
 *     positive value.
 *
 * Both are unreachable for 8-bit samples (0..255: a + b + 1 <= 511), which is the domain this primitive is
 * for, and neither can be fixed by reordering the two adds -- VADDS is the only vector add in the set, and
 * there is no wrapping 16-bit lane add, so an out-of-contract input cannot be reproduced by rearranging
 * saturating adds. Full-range int16 lanes are out of contract, and the model run counts the lanes where the
 * two readings diverge rather than asserting that they agree.
 */
    .align 4
    .global ex10_halfpel
    .type ex10_halfpel,@function
ex10_halfpel:
    entry a1, 32
    srli a6, a6, 3                  /* n_lanes -> iterations of eight */
    movi a7, 1
    ssr a7                          /* SAR = 1, read by EE.VMUL.U16 */
    EE.VLD.128.IP q6, a4, 0
    beqz a6, .Lhp_done
.Lhp:
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.VADDS.S16 q2, q0, q1
    EE.VADDS.S16 q2, q2, q6
    EE.VMUL.U16 q2, q2, q6
    EE.VST.128.IP q2, a5, 16
    addi a6, a6, -1
    bnez a6, .Lhp
.Lhp_done:
    retw.n
    .size ex10_halfpel, .-ex10_halfpel
