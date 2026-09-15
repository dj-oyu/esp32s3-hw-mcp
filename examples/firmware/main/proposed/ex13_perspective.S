/* ex13 -- the perspective-divide stage: a Q15 reciprocal table, the per-lane lookup, and the near plane.
 *
 * notes/08-media-3d-perf.md lists this stage as unbuilt ("PIE に除算は無い" -- there is no divide in
 * PIE, so the perspective divide and any normalisation are built from a reciprocal table plus a
 * multiply). This file is that stage, with the two properties of the instruction set that decide its
 * shape:
 *
 *   1. There is no gather, and EE.LD.QR is not one. `LD.QR qu, as, imm` loads 128 CONTIGUOUS bits
 *      from one address register (TRM p301, `qu = load128(as + imm)`), so it can only serve a
 *      lookup whose index is the SAME for all eight lanes. The per-lane indexed memory access is
 *      `EE.LDXQ.32 qu, qs, as, sel4, sel8` (TRM p113): it forms vaddr_k = as + qs[lane k] * 4 for
 *      all eight lanes, loads the ONE selected by the immediate sel8, and writes those 32 bits into
 *      the 32-bit segment sel4 of qu. So a per-lane 16-bit lookup is one LDXQ.32 per lane, and the
 *      table entry must be a 32-bit word holding the Q15 value in BOTH 16-bit halves -- then a
 *      single load delivers the value at whichever lane position the segment lands on, and the
 *      even/odd lanes are merged with a lane-pair mask. That is the layout stated below.
 *      EE.LD.QR still appears, for the 16-byte-aligned parameter block (the eps vector and the
 *      lane-pair mask) -- see the .md, which also costs the LD.QR-only alternative.
 *
 *   2. w <= eps. The kernel emits a defined MARKER (the reciprocal lane is 0, a value no valid
 *      entry can take) instead of clamping the hyperbolic tail; the .md argues why, and what
 *      happens if the caller would rather have the tail.
 *
 * Register budget: the QR operand field of these instructions is 3 bits wide, so this kernel (like
 * every kernel in this suite) can name only q0..q7 -- the assembler rejects q8..q15 with "register
 * number out of range", and the manual's instruction-word diagram agrees for every op used here
 * (p76 ANDQ: qa[2:1], qa[0]; p164 VLD.128.IP: qu[2:1], qu[0]; ...). Eight registers is the budget
 * the two kernels below are scheduled against.
 *
 * void ex13_recip_q15(const uint16_t *w, const int32_t *table, const int16_t *num_x8,
 *                     const int16_t *consts, int16_t *inv_out, int16_t *x_over_w_out);
 *
 *   a2 = w             8 uint16 lanes, 16-byte aligned. READ AS UNSIGNED, 0..65535.
 *   a3 = table         256 entries. Entry i is the int32 at byte offset 4*i (so the table must be
 *                      4-byte aligned; LDXQ.32 forces vaddr[1:0] = 0, p113, so a misaligned table
 *                      silently reads the wrong word rather than faulting). Both 16-bit halves of
 *                      entry i are the same value:
 *                          half(entry i) = clamp(round(2^23 / w_rep(i)), 1, 32767)
 *                          w_rep(i)      = 256*i + 128        (the midpoint of the bucket w>>8 == i)
 *                      entry 0 (w < 256) is the CLAMP 32767: the caller must set eps >= 256 to keep
 *                      those lanes out of the arithmetic (the .md says where that corruption lands).
 *   a4 = num_x8        8 int16 lanes, 16-byte aligned: the numerator (the already-scaled x).
 *   a5 = consts        32 bytes, 16-byte ALIGNED (a 128-bit QR load; see the .md on alignment):
 *                          +0   eight int16 lanes, each the near-plane epsilon (unsigned, 0..32767)
 *                          +16  four 32-bit segments of 0x0000FFFF  (the even-lane pair mask)
 *   a6 = inv_out       8 int16 lanes: the Q15 reciprocal of w (Q15 to the index scale, see the .md)
 *                      -- **0 marks a near-plane lane** (w <= eps); 0 is unreachable by a real
 *                      entry (every entry is >= 1), so the caller tests inv_out == 0, branchless.
 *   a7 = x_over_w_out  8 int16 lanes: (num * entry) >> 15, i.e. num/w in Q8 -- and 0 on a
 *                      near-plane lane (the marker, not a clamped tail).
 *
 * Exactly eight vertices per call: the windowed ABI passes the first six arguments in a2..a7, so a
 * count argument would have to be read from the caller's stack frame. The caller loops (one extra
 * `addi`/`bnez` per eight vertices), which is what ex12_integrate already does.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* ------------------------------------------------------------------ ex13_recip_q15 */
    .align 4
    .global ex13_recip_q15
    .type ex13_recip_q15,@function
ex13_recip_q15:
    entry a1, 64
    /* ABI: this kernel writes a10 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    LD.QR q4, a5, 16                /* q4 = the lane-pair mask (p301; the block is 16-byte aligned) */
    EE.VLD.128.IP q0, a2, 0         /* q0 = the eight uint16 w lanes (p164) */
    movi a8, 1
    EE.MOVI.32.Q q1, a8, 0          /* q1 = eight lanes of 1 (p119), for the shift-free index step */
    EE.MOVI.32.Q q1, a8, 1
    EE.MOVI.32.Q q1, a8, 2
    EE.MOVI.32.Q q1, a8, 3
    movi a9, 8
    ssr a9
    EE.VMUL.U16 q2, q0, q1          /* q2[l] = (uint16 w[l] * 1) >> 8 = the table index 0..255 (p204).
                                     * .U16 is required: with .S16 a w >= 32768 would be read as a
                                     * negative and arithmetic-shifted, giving the wrong bucket. */
    EE.ZERO.Q q1                    /* the ones are dead; q1 becomes the zero vector for the
                                     * unsigned-compare split below (p299) */
    LD.QR q3, a5, 0                 /* q3 = the eight eps lanes (p301) */
    EE.LDXQ.32 q5, q2, a3, 0, 0     /* eight per-lane indexed loads (p113): segment s of q5 gets
                                     * entry[idx[2s]] and segment s of q6 gets entry[idx[2s+1]].
                                     * Each instruction writes ONE 32-bit segment -- the eight of
                                     * them write all four segments of both registers, so no lane
                                     * is left holding stale data. */
    EE.LDXQ.32 q5, q2, a3, 1, 2
    EE.LDXQ.32 q5, q2, a3, 2, 4
    EE.LDXQ.32 q5, q2, a3, 3, 6
    EE.LDXQ.32 q6, q2, a3, 0, 1
    EE.LDXQ.32 q6, q2, a3, 1, 3
    EE.LDXQ.32 q6, q2, a3, 2, 5
    EE.LDXQ.32 q6, q2, a3, 3, 7
    EE.ANDQ q5, q5, q4              /* keep the even 16-bit lanes of the first pass (p76) */
    EE.NOTQ q4, q4                  /* the odd-lane mask (p120) */
    EE.ANDQ q6, q6, q4
    EE.ORQ q5, q5, q6               /* q5[l] = entry[idx[l]] = the Q15 reciprocal, one per lane (p121) */
    EE.VCMP.GT.S16 q6, q0, q3       /* w > eps, signed (p158) */
    EE.VCMP.LT.S16 q7, q0, q1       /* w < 0 signed  <=>  w >= 32768 unsigned (p161) */
    EE.ORQ q6, q6, q7               /* keep = (w > eps) OR (w >= 32768): the exact unsigned test,
                                     * because a single .S16 compare would misplace every w >= 32768
                                     * (a real vertex, not a near-plane one) */
    EE.ANDQ q5, q5, q6              /* near-plane lanes: the reciprocal becomes 0 = the marker */
    EE.VLD.128.IP q7, a4, 0         /* q7 = the eight numerators (loaded late: q7 was the compare scratch) */
    movi a10, 15
    ssr a10
    EE.VMUL.S16 q7, q7, q5          /* q7[l] = (num[l] * entry[l]) >> 15 = num/w in Q8 (p198).
                                     * .S16 is required here: the numerator is signed, and .U16 would
                                     * read a negative numerator as a large positive (ex08_shift_sign).
                                     * The entry is 1..32767, so both forms read it the same way. */
    EE.VST.128.IP q5, a6, 0         /* inv_out (p275) */
    EE.VST.128.IP q7, a7, 0         /* x_over_w_out */
    /* ABI: put the caller's a2 back before returning. */
    l32i a10, a1, 32
    retw.n
    .size ex13_recip_q15, .-ex13_recip_q15

/* void ex13_clip_quad(const int16_t *x8, const int16_t *y8, const int16_t *lo4, const int16_t *hi4,
 *                     uint16_t *mask8);
 *
 *   a2 = x8     8 int16 lanes (the projected x), 16-byte aligned
 *   a3 = y8     8 int16 lanes (the projected y), 16-byte aligned
 *   a4 = lo4    4 int16, 2-byte aligned: lo4[0] = xmin, lo4[1] = ymin  (only these two are READ)
 *   a5 = hi4    4 int16, 2-byte aligned: hi4[0] = xmax, hi4[1] = ymax  (only these two are READ)
 *               lo4[2]/lo4[3]/hi4[2]/hi4[3] are reserved for a z/w version of the same test and are
 *               not touched, so a caller that carries a 4-plane box can pass the whole thing.
 *   a6 = mask8  8 uint16 lanes, 16-byte aligned: 0xFFFF = inside the rectangle, 0 = outside.
 *
 * The caller drops a whole triangle (or keeps it) with no branch: AND the eight masks of the three
 * vertices (vmin) or OR them (vmax) and test against 0/0xFFFF. The kernel itself has no branch, no
 * arithmetic and therefore nothing to saturate: the compares are exact on 16-bit signed values and
 * the rest is bitwise (p76/p121/p120).
 *
 * The four bounds are broadcast to all eight lanes with EE.VLDBC.16 (p170). The address arithmetic
 * is explicit (`addi`) rather than EE.VLDBC.16.IP, whose increment the manual writes as
 * `{24{0}, imm2[6:0], 0}` (p171) -- i.e. twice the immediate -- which is not what the syntax line
 * `0..254` suggests; the explicit address costs two scalar instructions and removes the doubt.
 */
    .align 4
    .global ex13_clip_quad
    .type ex13_clip_quad,@function
ex13_clip_quad:
    entry a1, 32
    addi a8, a4, 2
    addi a9, a5, 2
    EE.VLDBC.16 q2, a4              /* q2 = {8{xmin}} (p170) */
    EE.VLDBC.16 q3, a8              /* q3 = {8{ymin}} */
    EE.VLDBC.16 q4, a5              /* q4 = {8{xmax}} */
    EE.VLDBC.16 q5, a9              /* q5 = {8{ymax}} */
    EE.VLD.128.IP q0, a2, 0         /* the x lanes */
    EE.VLD.128.IP q1, a3, 0         /* the y lanes */
    EE.VCMP.LT.S16 q6, q0, q2       /* x < xmin  -> separated (p161) */
    EE.VCMP.GT.S16 q7, q0, q4       /* x > xmax  -> separated (p158) */
    EE.ORQ q6, q6, q7
    EE.VCMP.LT.S16 q7, q1, q3       /* y < ymin */
    EE.ORQ q6, q6, q7
    EE.VCMP.GT.S16 q7, q1, q5       /* y > ymax */
    EE.ORQ q6, q6, q7               /* q6 = 0xFFFF where the vertex is outside */
    EE.NOTQ q6, q6                  /* ... and the accept mask is its complement: one NOTQ for the
                                     * whole 4-plane test (De Morgan, as ex12_sat_masks does), where an
                                     * ANDQ accumulation of the four inside-tests would need a NOTQ per
                                     * compare (4 extra ops, p120/p121) */
    EE.VST.128.IP q6, a6, 0
    retw.n
    .size ex13_clip_quad, .-ex13_clip_quad
