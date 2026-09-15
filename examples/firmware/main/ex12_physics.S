/* ex12 -- collision and physics primitives: the two loops a 2D/3D game spends its frame budget in.
 *
 * The examples so far are one effect each (a 4x4 transform, a blend, a clamp). This file is the first that
 * strings primitives together into the shapes a game actually calls every frame:
 *
 *   A) ex12_integrate    : semi-implicit Euler, out = clamp(pos + vel + acc), FOUR 32-bit lanes per
 *                          register, EIGHT objects per call. The set has no plain vector add and no
 *                          32-bit vector multiply, so `pos += vel + acc` has to be built out of the
 *                          *saturating* VADDS.S32 and the per-lane VMIN/VMAX.S32 clamp.
 *   B) ex12_sat_masks    : the AABB / separating-axis overlap test for EIGHT candidate boxes against one
 *                          query box, one 16-bit lane per (box, axis) comparison, ending in an
 *                          all-ones/zero mask per box (EE.VCMP.* + EE.ORQ + a single EE.NOTQ).
 *   C) ex12_dist2_qacc   : squared distance for EIGHT point pairs at once, accumulated per lane in QACC,
 *                          so a game compares against r^2 and never takes a square root (no divide and no
 *                          square root exist in this instruction set).
 *      ex12_dist2_q16    : the same accumulation with the cheap saturating QACC readout (EE.SRCMB.S16.QACC)
 *                          instead of the exact one, for callers that live in a Q-format radius.
 *
 * Everything works on 128-bit chunks: 4 x int32 (A), 8 x int16 (B, C). Buffers must be 16-byte aligned --
 * every 128-bit PIE access forms its address as {as[31:4], 4{0}} and *drops* the low four bits (TRM p49,
 * measured in ex03), so a misaligned pointer silently reads or writes the neighbouring 16 bytes.
 *
 * Constants never come from .rodata (an l32r from .iram1 cannot reach it): they arrive from C through a
 * pointer, or are broadcast from the CALLER's stack frame, or are built with movi/slli/addi. The lo/hi
 * scalars of ex12_integrate are the interesting case -- a scalar int32 argument has to reach a QR register
 * somehow, and there is no move-from-AR-to-QR instruction in the set, so the two values are spilled to the
 * frame that `entry` already allocated and read back with EE.VLDBC.32 (broadcast load).
 *
 * Instruction semantics used here, and the pages they come from (data/pie_instructions.json):
 *   EE.VADDS.S32   p149  qa[lane] = sat32(qx[lane] + qy[lane])          (SATURATES at +-2^31)
 *   EE.VSUBS.S32   p284  qa[lane] = sat32(qx[lane] - qy[lane])          (SATURATES at +-2^31)
 *   EE.VMIN.S32    p192  qa[lane] = min(qx[lane], qy[lane])             (exact)
 *   EE.VMAX.S32    p183  qa[lane] = max(qx[lane], qy[lane])             (exact)
 *   EE.VSUBS.S16   p281  qa[lane] = sat16(qx[lane] - qy[lane])          (SATURATES at +-2^15)
 *   EE.VCMP.LT.S16 p161  qa[lane] = (qx[lane] <  qy[lane]) ? 0xFFFF : 0
 *   EE.VCMP.GT.S16 p158  qa[lane] = (qx[lane] >  qy[lane]) ? 0xFFFF : 0
 *   EE.ORQ         p121  qa = qx | qy        (bitwise, whole 128-bit register)
 *   EE.NOTQ        p120  qa = ~qx
 *   EE.ANDQ        p76   qa = qx & qy
 *   EE.VMULAS.S16.QACC p215   QACC_H/L[i] = sat40(QACC[i] + qx[lane i] * qy[lane i])
 *   EE.SRCMB.S16.QACC  p130   qu[lane i] = sat16(QACC[i] >> as[5:0]), writes the shifted value BACK to QACC
 *   EE.ZERO.QACC   p300  QACC_L = QACC_H = 0
 *   EE.VLD.128.IP  p164 / EE.VST.128.IP p275   load/store 16 bytes, address forced to {as[31:4],4{0}},
 *                                              as += imm16 (the printed immediate, verbatim -- ex01)
 *   EE.VLDBC.16    p170  qu = {8{load16({as[31:1],1{0}})}}     (broadcast)
 *   EE.VLDBC.32    p173  qu = {4{load32({as[31:2],2{0}})}}     (broadcast)
 *   RUR.QACC_L_0..4 / RUR.QACC_H_0..4   TRM p64: the 160-bit QACC_L/QACC_H are read five AR words at a
 *                                        time; lane i of the 16-bit format is bits [40i, 40i+40) of the
 *                                        pair (TRM p105's LDQA pseudo-code states the same lane layout)
 */
    .section .iram1,"ax",@progbits
    .align 4

/* ---------------------------------------------------------------- A: integrate + clamp
 * void ex12_integrate(const int32_t *pos, const int32_t *vel, const int32_t *acc, int32_t *out,
 *                     int32_t lo, int32_t hi)
 *
 *   out[0..7] = clamp(sat32(sat32(pos[i] + vel[i]) + acc[i]), lo, hi)          i = 0..7
 *
 *   pos/vel/acc/out are 8 int32 each, 16-byte aligned. No count argument: exactly EIGHT objects per call,
 *   two four-lane register groups. `vel` and `acc` are the frame's velocity and acceleration terms already
 *   scaled by dt (semi-implicit Euler: the caller updates the velocity first, which is what makes the step
 *   symplectic; the extra `acc` term is the usual velocity-Verlet position correction).
 *
 *   The dependency that makes this kernel interesting is the saturation: EE.VADDS.S32 clamps at +-(2^31-1)
 *   per lane, so the result equals the mathematical clamp(sat32(pos+vel)+acc..) only where the first add did
 *   not saturate. For |pos[i] + vel[i]| <= 2^31-1 the whole expression is exact (see ex12_physics.md).
 *
 *   Instruction count (from the object file, see ex12_physics.md): 18 PIE ops per call -- 8 per
 *   four-lane group (3 loads, 2 saturating adds, 1 VMIN, 1 VMAX, 1 store) plus the 2 VLDBC broadcasts --
 *   and 24 instructions in all including `entry`/`retw.n`. That is 2.25 PIE ops and 3.0 instructions per
 *   object, in a straight line with no loop.
 */
    .align 4
    .global ex12_integrate
    .type ex12_integrate,@function
ex12_integrate:
    entry a1, 32
    s32i a6, a1, 0                  /* lo -> frame[0..3] */
    s32i a7, a1, 4                  /* hi -> frame[4..7] */
    EE.VLDBC.32 q6, a1              /* q6 = {4{lo}}  (there is no AR->QR move; the frame is the way in) */
    addi a9, a1, 4
    EE.VLDBC.32 q7, a9              /* q7 = {4{hi}} */
    /* group 0: objects 0..3 (one 128-bit register of pos, vel, acc and out) */
    EE.VLD.128.IP q0, a2, 16        /* pos[0..3]; a2 += 16 */
    EE.VLD.128.IP q1, a3, 16        /* vel[0..3]; a3 += 16 */
    EE.VLD.128.IP q2, a4, 16        /* acc[0..3]; a4 += 16 */
    EE.VADDS.S32 q3, q0, q1         /* sat32(pos + vel) */
    EE.VADDS.S32 q3, q3, q2         /* sat32(that + acc) */
    EE.VMIN.S32 q3, q3, q7          /* <= hi */
    EE.VMAX.S32 q3, q3, q6          /* >= lo  (order matters: lo must win when lo > hi, so the caller
                                     *         must pass lo <= hi) */
    EE.VST.128.IP q3, a5, 16        /* out[0..3]; a5 += 16 */
    /* group 1: objects 4..7 */
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.VLD.128.IP q2, a4, 16
    EE.VADDS.S32 q3, q0, q1
    EE.VADDS.S32 q3, q3, q2
    EE.VMIN.S32 q3, q3, q7
    EE.VMAX.S32 q3, q3, q6
    EE.VST.128.IP q3, a5, 16
    retw.n
    .size ex12_integrate, .-ex12_integrate

/* ---------------------------------------------------------------- B: the AABB overlap mask
 * void ex12_sat_masks(const int16_t *boxes, const int16_t *query, int16_t *out, uint32_t n_boxes)
 *
 *   out[b] = 0xFFFF if box b overlaps the query box in all three axes, 0 if any axis separates them.
 *
 *   DATA LAYOUT (this is the part a caller has to get exactly right):
 *
 *     boxes  : plane-major, eight boxes per group of six 16-byte planes, in this order
 *                + 0  min_x[8]   +16  max_x[8]   +32  min_y[8]
 *                +48  max_y[8]   +64  min_z[8]   +80  max_z[8]        (96 bytes per group)
 *              element (plane p, box i) is at  boxes + 96*(i/8) + 16*p + 2*(i%8).
 *              Only the lanes below n_boxes matter; the vector loop runs over whole groups of eight, and the
 *              (n_boxes % 8) remainder is finished by the scalar tail, so a partial last group needs only
 *              its own 2 bytes per plane (no padding lanes are ever read).
 *     query  : six int16, the same order: min_x, max_x, min_y, max_y, min_z, max_z. May be 2-byte aligned
 *              (EE.VLDBC.16 drops the low address bit; anything else would silently read the wrong scalar).
 *     out    : one int16 per box, 0xFFFF (=-1, all ones) for overlap, 0 for separated. 16-byte aligned not
 *              required for the scalar tail, but IS required for the vector stores: out is written in
 *              16-byte chunks (8 lanes) by EE.VST.128.IP, so `out` must be 16-byte aligned and must have
 *              room for 8 * ceil(n_boxes/8) int16... no: exactly n_boxes int16, because the vector loop
 *              only ever writes FULL groups of eight (n_boxes >> 3), and the tail writes single int16s.
 *              So `out` needs exactly n_boxes int16 and 16-byte alignment.
 *
 *   FIXED-POINT: the values are 16-bit signed fixed point (the kernel suggests Q8, i.e. 1 world unit =
 *   256) -- but the test is invariant under any common scale, only the ORDER of the numbers matters, so
 *   the same kernel serves Q8 or Q15 or raw pixel coordinates. This is also why a 2D game can reuse it
 *   unchanged: pass min_z = -32768, max_z = 32767 and the z axis never separates.
 *
 *   The test, per axis: separated iff (box_max < query_min) or (box_min > query_max). Both are one VCMP
 *   (LT / GT) with the query broadcast into all eight lanes; ORQ accumulates the separation bits across
 *   the two tests and then across the three axes (De Morgan), and a single NOTQ at the end turns "no
 *   separation anywhere" into the all-ones overlap mask. Why ORQ and not two NOTQs: NOT(a<b) & NOT(a>b)
 *   = NOT((a<b)|(a>b)), one NOT per group instead of two per axis.
 *
 *   Instruction count (3D, measured): the group body is 32 instructions per EIGHT boxes -- 25 PIE ops
 *   (6 VLD.128 + 6 VLDBC.16 + 6 VCMP + 5 ORQ + 1 NOTQ + 1 VST) and 7 scalar (6 addi for the query
 *   offsets, 1 addi + 1 bnez for the loop). That is 3.125 PIE ops and 4.0 instructions per box; the
 *   scalar tail (n_boxes % 8) is 21 instructions per box, and a 2D caller that folds z away drops
 *   8 PIE ops per group (2.1 per box).
 */
    .align 4
    .global ex12_sat_masks
    .type ex12_sat_masks,@function
ex12_sat_masks:
    entry a1, 80
    /* ABI: this kernel writes a10, a11, a12, a13, a14 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    s32i a12, a1, 40
    s32i a13, a1, 44
    s32i a14, a1, 48
    srli a6, a5, 3                  /* a6 = whole groups of eight */
    beqz a6, .Lsm_tail
.Lsm_group:
    /* --- x: separated iff (max_x < query_min_x) or (min_x > query_max_x) --- */
    EE.VLD.128.IP q0, a2, 16        /* min_x[8]; a2 += 16 */
    EE.VLD.128.IP q1, a2, 16        /* max_x[8]; a2 += 16 */
    EE.VLDBC.16   q2, a3            /* q2 = {8{qmin_x}} */
    addi a7, a3, 2
    EE.VLDBC.16   q3, a7            /* q3 = {8{qmax_x}} */
    EE.VCMP.LT.S16 q4, q1, q2       /* max_x < qmin_x  -> separated */
    EE.VCMP.GT.S16 q5, q0, q3       /* min_x > qmax_x  -> separated */
    EE.ORQ q4, q4, q5               /* separation bits, axis x */
    /* --- y --- */
    addi a7, a3, 4
    EE.VLDBC.16   q2, a7
    addi a7, a3, 6
    EE.VLDBC.16   q3, a7
    EE.VLD.128.IP q0, a2, 16        /* min_y */
    EE.VLD.128.IP q1, a2, 16        /* max_y */
    EE.VCMP.LT.S16 q5, q1, q2
    EE.VCMP.GT.S16 q6, q0, q3
    EE.ORQ q5, q5, q6
    EE.ORQ q4, q4, q5
    /* --- z --- */
    addi a7, a3, 8
    EE.VLDBC.16   q2, a7
    addi a7, a3, 10
    EE.VLDBC.16   q3, a7
    EE.VLD.128.IP q0, a2, 16        /* min_z */
    EE.VLD.128.IP q1, a2, 16        /* max_z */
    EE.VCMP.LT.S16 q5, q1, q2
    EE.VCMP.GT.S16 q6, q0, q3
    EE.ORQ q5, q5, q6
    EE.ORQ q4, q4, q5
    /* --- overlap = !separated --- */
    EE.NOTQ q7, q4                  /* 0xFFFF where the box overlaps on all three axes, 0 otherwise */
    EE.VST.128.IP q7, a4, 16        /* out[8]; a4 += 16 */
    addi a6, a6, -1
    bnez a6, .Lsm_group
.Lsm_tail:
    /* --- the (n_boxes & 7) remainder, one box at a time, same test, signed 16-bit compares --- */
    movi a14, 7
    and a6, a5, a14                 /* a6 = n_boxes & 7 (there is no ANDI in this assembler) */
    beqz a6, .Lsm_done
    l16si a8,  a3, 0                /* qmin_x */
    l16si a9,  a3, 2                /* qmax_x */
    l16si a10, a3, 4                /* qmin_y */
    l16si a11, a3, 6                /* qmax_y */
    l16si a12, a3, 8                /* qmin_z */
    l16si a13, a3, 10               /* qmax_z */
.Lsm_tail_box:
    l16si a14, a2, 0                /* min_x */
    blt a9, a14, .Lsm_sep
    l16si a14, a2, 16               /* max_x */
    blt a14, a8, .Lsm_sep
    l16si a14, a2, 32               /* min_y */
    blt a11, a14, .Lsm_sep
    l16si a14, a2, 48               /* max_y */
    blt a14, a10, .Lsm_sep
    l16si a14, a2, 64               /* min_z */
    blt a13, a14, .Lsm_sep
    l16si a14, a2, 80               /* max_z */
    blt a14, a12, .Lsm_sep
    movi a14, -1                    /* all ones: overlap */
    s16i a14, a4, 0
    j .Lsm_tail_next
.Lsm_sep:
    movi a14, 0
    s16i a14, a4, 0
.Lsm_tail_next:
    addi a2, a2, 2                  /* next box: 2 bytes along every plane */
    addi a4, a4, 2
    addi a6, a6, -1
    bnez a6, .Lsm_tail_box
.Lsm_done:
    /* ABI: put the caller's a2/a3/a4/a5/a6 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    l32i a13, a1, 44
    l32i a14, a1, 48
    retw.n
    .size ex12_sat_masks, .-ex12_sat_masks

/* ---------------------------------------------------------------- C: 8 squared distances per call
 * void ex12_dist2_qacc(const int16_t *pt_a, const int16_t *pt_b, int32_t *out, uint32_t n_points)
 *
 *   out[i] = min(dx*dx + dy*dy + dz*dz, 2^31-1)        dx = sat16(a.x - b.x), etc.
 *
 *   DATA LAYOUT: both point arrays are plane-major groups of eight points:
 *     group g holds  x[8] at +0, y[8] at +16, z[8] at +32   (48 bytes per group)
 *     element (plane p, point i) is at  pt + 48*(i/8) + 16*p + 2*(i%8).
 *   out is int32 per point, 16-byte aligned (it is written with two EE.VST.128.IP rounds of four lanes).
 *
 *   The kernel: three saturating 16-bit subtractions (one per axis) and three EE.VMULAS.S16.QACC, each
 *   squaring a lane and adding it to that lane's own 40-bit accumulator -- so after the third MAC every
 *   QACC lane holds dx^2+dy^2+dz^2 for one point pair. No square root is needed (or available): the game
 *   compares the result against r^2.
 *
 *   READOUT: RUR.QACC_L_0..4 / RUR.QACC_H_0..4 (five 32-bit words each, TRM p64) give the raw 160-bit
 *   accumulators, and the lanes are bit-packed at 40-bit offsets (EE.LDQA.S16.128.IP p105 states the same
 *   lane layout). Only the LOW 32 bits of each lane are needed here, and that is not an approximation:
 *   because the difference comes out of a saturating 16-bit subtract, |dx| <= 32767 for every input, so
 *   dist2 <= 3*32767^2 = 3221028867 < 2^32 -- bits 32..39 of every lane are zero by construction, whatever
 *   the caller passes. The value is then unsigned-clamped to 2^31-1 (minu) so it fits the int32 output;
 *   that clamp preserves "is this point within r^2" for every r^2 <= 2^31-1 (see ex12_physics.md).
 *
 *   Instruction count (measured): the group body is 68 instructions per EIGHT point pairs -- 13 PIE ops
 *   (1 ZERO + 6 VLD.128 + 3 VSUBS.S16 + 3 VMULAS.S16.QACC) and 10 RUR (five words per 160-bit half)
 *   and 45 scalar -- per lane: 2 shifts and an or to squeeze the low 32 bits out of two words (lane 0
 *   needs none, hence 3 instructions for it and 6 for each of the other three), a load of the clamp
 *   ceiling, an unsigned min, a store -- plus 3 for the loop = 8.5 instructions per pair, of which only
 *   1.6 are the arithmetic. The Q16 variant below does the same arithmetic and gets the
 *   values out in ONE instruction per eight lanes (17 instructions per group = 2.1 per pair), at the
 *   cost of 16-bit saturation -- the honest reading of the cost table is that the exact int32 readout,
 *   not the multiply-accumulate, is what this primitive costs.
 */
    .align 4
    .global ex12_dist2_qacc
    .type ex12_dist2_qacc,@function
ex12_dist2_qacc:
    entry a1, 96
    /* ABI: this kernel writes a10, a11, a12, a15 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 64
    s32i a11, a1, 68
    s32i a12, a1, 72
    s32i a15, a1, 76
    movi a6, 1
    slli a6, a6, 31
    addi a6, a6, -1                 /* 0x7FFFFFFF: movi cannot hold it in one instruction */
    s32i a6, a1, 36                 /* frame[36] = the int32 clamp ceiling */
    movi a9, 7
    and a6, a5, a9                  /* a6 = n_points & 7 */
    s32i a6, a1, 40                 /* frame[40] = number of leftover pairs */
    srli a15, a5, 3                 /* a15 = whole groups of eight (the loop counter; never a temp) */
    beqz a15, .Ld2_tail
.Ld2_group:
    EE.ZERO.QACC
    EE.VLD.128.IP q0, a2, 16        /* ax[8] */
    EE.VLD.128.IP q1, a3, 16        /* bx[8] */
    EE.VSUBS.S16 q2, q0, q1         /* dx = sat16(ax - bx)  (order irrelevant after the square) */
    EE.VMULAS.S16.QACC q2, q2       /* QACC[lane] += dx*dx */
    EE.VLD.128.IP q0, a2, 16        /* ay[8] */
    EE.VLD.128.IP q1, a3, 16        /* by[8] */
    EE.VSUBS.S16 q2, q0, q1
    EE.VMULAS.S16.QACC q2, q2
    EE.VLD.128.IP q0, a2, 16        /* az[8] */
    EE.VLD.128.IP q1, a3, 16        /* bz[8] */
    EE.VSUBS.S16 q2, q0, q1
    EE.VMULAS.S16.QACC q2, q2
    /* ---- the L half of the accumulator: lanes 0..3, each lane's low 32 bits ---- */
    RUR.QACC_L_0 a6                 /* w0 = bits [31:0]     -> lane 0 bits [31:0] */
    RUR.QACC_L_1 a7
    RUR.QACC_L_2 a8
    RUR.QACC_L_3 a9
    RUR.QACC_L_4 a10
    /* lane 0: exactly w0 */
    l32i a11, a1, 36
    minu a11, a6, a11
    s32i a11, a4, 0
    /* lane 1: bits [40,80) -> low 32 = (w1 >> 8) | (w2 << 24) */
    srli a11, a7, 8
    slli a12, a8, 24
    or a11, a11, a12
    l32i a12, a1, 36
    minu a11, a11, a12
    s32i a11, a4, 4
    /* lane 2: bits [80,120) -> low 32 = (w2 >> 16) | (w3 << 16) */
    srli a11, a8, 16
    slli a12, a9, 16
    or a11, a11, a12
    l32i a12, a1, 36
    minu a11, a11, a12
    s32i a11, a4, 8
    /* lane 3: bits [120,160) -> low 32 = (w3 >> 24) | (w4 << 8) */
    srli a11, a9, 24
    slli a12, a10, 8
    or a11, a11, a12
    l32i a12, a1, 36
    minu a11, a11, a12
    s32i a11, a4, 12
    /* ---- the H half: lanes 4..7, same word offsets inside QACC_H ---- */
    RUR.QACC_H_0 a6
    RUR.QACC_H_1 a7
    RUR.QACC_H_2 a8
    RUR.QACC_H_3 a9
    RUR.QACC_H_4 a10
    l32i a11, a1, 36
    minu a11, a6, a11
    s32i a11, a4, 16
    srli a11, a7, 8
    slli a12, a8, 24
    or a11, a11, a12
    l32i a12, a1, 36
    minu a11, a11, a12
    s32i a11, a4, 20
    srli a11, a8, 16
    slli a12, a9, 16
    or a11, a11, a12
    l32i a12, a1, 36
    minu a11, a11, a12
    s32i a11, a4, 24
    srli a11, a9, 24
    slli a12, a10, 8
    or a11, a11, a12
    l32i a12, a1, 36
    minu a11, a11, a12
    s32i a11, a4, 28
    addi a4, a4, 32                 /* eight int32 written */
    addi a15, a15, -1
    bnez a15, .Ld2_group
.Ld2_tail:
    /* the (n_points & 7) leftover pairs, scalar, with the SAME saturating 16-bit difference the vector
     * path takes, so the two paths agree even outside the documented |a-b| <= 32767 range */
    l32i a6, a1, 40
    beqz a6, .Ld2_done
    movi a7, 1
    slli a7, a7, 15
    addi a7, a7, -1                 /* 32767: a plain movi would need an l32r literal pool */
    movi a8, -1
    slli a8, a8, 15                 /* -32768 */
.Ld2_tail_pt:
    l16si a9, a2, 0                 /* a.x */
    l16si a10, a3, 0                /* b.x */
    sub a9, a9, a10
    min a9, a9, a7
    max a9, a9, a8                  /* dx = sat16(a.x - b.x) */
    mull a9, a9, a9                 /* dx*dx <= 2^30 */
    l16si a10, a2, 16               /* a.y */
    l16si a11, a3, 16               /* b.y */
    sub a10, a10, a11
    min a10, a10, a7
    max a10, a10, a8
    mull a10, a10, a10
    add a9, a9, a10
    l16si a10, a2, 32               /* a.z */
    l16si a11, a3, 32               /* b.z */
    sub a10, a10, a11
    min a10, a10, a7
    max a10, a10, a8
    mull a10, a10, a10
    add a9, a9, a10                 /* <= 3*32767^2 < 2^32: no wrap (unsigned), bit 31 may be set */
    l32i a10, a1, 36
    minu a9, a9, a10                /* clamp to 2^31-1 exactly as the vector path does */
    s32i a9, a4, 0
    addi a2, a2, 2
    addi a3, a3, 2
    addi a4, a4, 4
    addi a6, a6, -1
    bnez a6, .Ld2_tail_pt
.Ld2_done:
    /* ABI: put the caller's a2/a3/a4/a7 back before returning. */
    l32i a10, a1, 64
    l32i a11, a1, 68
    l32i a12, a1, 72
    l32i a15, a1, 76
    retw.n
    .size ex12_dist2_qacc, .-ex12_dist2_qacc

/* ---------------------------------------------------------------- C': the same, Q16 readout
 * uint32_t ex12_dist2_q16(const int16_t *pt_a, const int16_t *pt_b, int16_t *out, uint32_t n_points,
 *                         uint32_t shift)
 *
 *   out[i] = sat16(dist2[i] >> shift)     for the (n_points & ~7) pairs of the whole groups;
 *                                         returns the number of pairs written.
 *
 *   Same layout and same three MACs as ex12_dist2_qacc; the difference is the readout: one
 *   EE.SRCMB.S16.QACC turns all eight 40-bit lanes into eight 16-bit lanes, arithmetic-shifted by
 *   `shift` (a register operand, not an immediate) and saturating at +-32767. That is the whole point of
 *   the Q-format: the game keeps its radius as r2 = (r*r) >> shift and compares two shifted values, so a
 *   radius test costs one instruction per eight pairs instead of five.
 *
 *   Domain: exact while dist2 <= 32767 << shift (no lane saturates). With shift = 8 that is
 *   dist2 <= 8388352, i.e. |dx| <= 1672 on all three axes (2048 in 2D). Beyond that the readout clamps
 *   and the value stops being a usable order relation -- the caller either raises the shift (losing
 *   resolution) or uses the exact kernel above.
 *
 *   n_points not a multiple of eight: this variant processes whole groups only and returns the count
 *   written (n_points & ~7), so nothing is silently dropped by the kernel -- the leftover is the caller's.
 */
    .align 4
    .global ex12_dist2_q16
    .type ex12_dist2_q16,@function
ex12_dist2_q16:
    entry a1, 64
    /* ABI: this kernel writes a10 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    srli a7, a5, 3                  /* whole groups of eight = the return value */
    beqz a7, .Lq16_done
    mov a10, a6                     /* `as` operand of SRCMB: the shift comes from a register */
.Lq16_group:
    EE.ZERO.QACC
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.VSUBS.S16 q2, q0, q1
    EE.VMULAS.S16.QACC q2, q2
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.VSUBS.S16 q2, q0, q1
    EE.VMULAS.S16.QACC q2, q2
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.VSUBS.S16 q2, q0, q1
    EE.VMULAS.S16.QACC q2, q2
    EE.SRCMB.S16.QACC q5, a10, 0    /* qu = sat16(QACC[lane] >> as[5:0]) -- and it writes the shifted
                                     * value back into QACC, so it may only be called ONCE per
                                     * accumulation (ex09_srcmb_wb is the probe that decides this; see
                                     * the unconfirmed list in ex12_physics.md) */
    EE.VST.128.IP q5, a4, 16
    addi a7, a7, -1
    bnez a7, .Lq16_group
.Lq16_done:
    srli a2, a5, 3                  /* return (n_points & ~7): the pairs actually written */
    /* ABI: put the caller's a2 back before returning. */
    l32i a10, a1, 32
    retw.n
    .size ex12_dist2_q16, .-ex12_dist2_q16
