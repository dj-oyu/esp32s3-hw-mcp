/* ex20 -- triangle setup and the depth test: the state a rasteriser needs BEFORE it fills anything.
 *
 * Item "ex20" of notes/08-media-3d-perf.md's production list ("深度テストの書き込みマスク、辺係数の
 * セットアップ、属性補間"), the stage that sits between ex07/ex13 (transform + perspective divide) and
 * ex14 (span fill).  Three kernels, all from the manual's pseudo-code (data/pie_instructions.json, page
 * cited per op):
 *
 *   A) ex20_depth_test  -- the depth test's WRITE MASK, eight uint16 depth lanes per iteration.
 *                          EE.VCMP.LT.S16 makes the per-lane mask, EE.ANDQ + EE.VMULAS.U16.ACCX
 *                          compress the eight lanes into the packed 16-bit word the caller gets, so
 *                          the mask is directly usable as the input of the composite/blend stage.
 *   B) ex20_edge_setup  -- the edge coefficients of a triangle: the three consecutive vertex
 *                          differences (dx, dy) per plane plus their signs, four int32 lanes per
 *                          vector.
 *   C) ex20_attr_interp -- span-direction attribute interpolation (z / UV): eight int16 lanes of a
 *                          Q0.16 attribute advance from a_top to a_bot in one call.
 *
 * ------------------------------------------------------------------------------------------------------
 * The 32-bit lane group, and what does NOT exist (asked for by the task, checked against the 220 entries)
 * ------------------------------------------------------------------------------------------------------
 *
 * data/pie_instructions.json holds 220 instructions (199 EE.* + LD.QR/ST.QR/MV.QR + the QR and FFT
 * forms).  Every entry whose data segments are 32 bits wide:
 *
 *   EE.VADDS.S32    p149    qa = sat32(qx + qy)                     (+ .LD.INCP p150 / .ST.INCP p151)
 *   EE.VSUBS.S32    p284    qa = sat32(qx - qy)                     (+ .LD.INCP p285 / .ST.INCP p286)
 *   EE.VMAX.S32     p183    qa = max(qx, qy)  per lane              (+ p184 / p185)
 *   EE.VMIN.S32     p192    qa = min(qx, qy)  per lane              (+ p193 / p194)
 *   EE.VCMP.EQ.S32  p156    qa = (qx == qy) ? 0xFFFFFFFF : 0        (all-ones mask, per lane)
 *   EE.VCMP.GT.S32  p159    qa = (qx >  qy) ? 0xFFFFFFFF : 0
 *   EE.VCMP.LT.S32  p162    qa = (qx <  qy) ? 0xFFFFFFFF : 0
 *   EE.VSL.32       p268    qa = qs << SAR[5:0]                     (per 32-bit lane)
 *   EE.VSR.32       p274    qa = qs >> SAR[5:0]                     (per lane, sign-propagating)
 *   EE.VZIP.32      p294 / EE.VUNZIP.32 p291                        (interleave / de-interleave pairs)
 *   EE.VLDBC.32     p173, EE.LDXQ.32 p113, EE.STXQ.32 p145          (broadcast / indexed lane access)
 *   EE.MOVI.32.A    p118 / EE.MOVI.32.Q p119                        (GPR <-> one 32-bit lane)
 *
 * and what is ABSENT, which is what shapes these three kernels:
 *
 *   - there is no 32-bit MULTIPLY at all: EE.VMUL.* is S16/U16/S8/U8 only (p198-p209), and the MAC
 *     family (EE.VMULAS.* / EE.VSMULAS.*) is 16-bit-lane only too.  So no cross product, no 32-bit
 *     attribute scaling, no area computation in the 32-bit lane domain -- ex20_edge_setup cannot
 *     compute the triangle's signed area, only the edge differences and their signs.
 *   - EE.VCMP has exactly three relations -- EQ, GT, LT -- and no GE/LE and no UNSIGNED form
 *     (p155-p163).  "keep if the new depth is smaller" therefore has to be spelled with LT (kernel A)
 *     or with a sign flip on the data (the XORQ trick below).
 *   - no 32-bit absolute value, no negate, no shift-per-lane (SAR is a single scalar for the whole
 *     instruction, and it is shared by everything that runs in between: see the SAR note below).
 *
 * ------------------------------------------------------------------------------------------------------
 * A) ex20_depth_test -- the write mask, packed into the word the next stage consumes
 * ------------------------------------------------------------------------------------------------------
 *
 *   void ex20_depth_test(const uint16_t *z_buf, const uint16_t *z_new, uint16_t *keep_mask, uint32_t n);
 *
 *   z_buf, z_new   n*8 uint16 depth lanes each.  MUST be 16-byte aligned (EE.VLD.128.IP forces the
 *                  low four address bits to 0, TRM p49 -- a misaligned pointer reads the wrong chunk
 *                  silently, so this is a hard requirement, not a preference).
 *   keep_mask      n uint16.  keep_mask[g] bit l is the keep decision for lane l of group g:
 *                      bit l set  <=>  z_new[8g+l] < z_buf[8g+l]   (as UNSIGNED 16-bit depths)
 *                  The kernel writes exactly 8 bytes per group with `s16i`, so 2-byte alignment is
 *                  enough for the store; the expansion path (EE.VLDBC.16, below) has the same 2-byte
 *                  requirement, which is where it comes from.
 *   n              number of 8-lane groups.  n == 0 writes nothing.
 *
 * The mask bits are in LANE ORDER: bit 0 = lane 0 = the lane held in q[15:0] = the first uint16 in
 * memory (little-endian, the order EE.VLD.128 reads).  This is not asserted: ex20_raster_setup.md's
 * model run packs the mask, expands it back with the exact instruction sequence below, and compares
 * against the 256 possible lane masks exhaustively -- and it also checks the memory byte order, i.e.
 * that a group whose only surviving lane is lane 3 has keep_mask[g] == 0x0008 in the little-endian
 * word the s16i writes.
 *
 * Per group, the instructions (14 of them for eight lanes, plus a 21-instruction prologue):
 *
 *   EE.VLD.128.IP  q0, a2, 16    z_buf lanes
 *   EE.ZERO.ACCX                 ACCX = 0 for THIS group (see the packing note)
 *   EE.VLD.128.IP  q1, a3, 16    z_new lanes
 *   EE.XORQ        q0, q0, q6    q6 = 0x8000 in every lane: the unsigned -> signed relabel
 *   EE.XORQ        q1, q1, q6
 *   EE.VCMP.LT.S16 q2, q1, q0    q2[l] = 0xFFFF where z_new[l] < z_buf[l], else 0
 *   EE.ANDQ        q2, q2, q4    q4 = {1,2,4,...,128}: lane l becomes its own bit weight (or 0)
 *   EE.VMULAS.U16.ACCX q2, q6    ACCX = 32768 * (sum of the set lanes' weights) = 32768 * packed
 *   addi.n         a5, a5, -1    filler 1 of the MAC -> readout distance (D = 2, see the .md)
 *   nop.n                        filler 2
 *   EE.SRS.ACCX    a6, a8, 0     a6 = sat32(ACCX >> as[5:0]) with a8 = 15, i.e. the packed byte
 *   s16i           a6, a4, 0     keep_mask[g] = packed byte
 *   addi           a4, a4, 2     next keep_mask word
 *   bnez           a5, .Ldepth
 *
 * The readout is EE.SRS.ACCX (p134) rather than RUR.ACCX_0 (ex10's choice): SRS is in the
 * 220-entry instruction list and has a Table 1.7-2 row (data/pie_pipeline.json p68: use ACCX at stage
 * E, def au at E), while the RUR forms are in neither -- ex10 had to guess a 4-nop distance for them.
 * Table 1.7-2 puts EE.VMULAS.U16.ACCX's ACCX write at stage M (p71), so the distance to SRS.ACCX's
 * ACCX read at E is D = max(2-1+1, 0) = 2 cycles, i.e. one instruction between them: the `addi.n` and
 * the `nop.n` above are exactly that.  SRS.ACCX also *writes back* the shifted ACCX and saturates the
 * value it returns to 32 bits; both are harmless here, because ACCX is zeroed at the top of every
 * group and one group's packed byte is at most 255.  (One trap: the third operand of EE.SRS.ACCX is a
 * fixed 0 field -- the syntax line reads `EE.SRS.ACCX au, as, 0` and the toolchain rejects anything
 * else ("Error: operand 3 of 'ee.srs.accx' has invalid value '15'").  The shift is the REGISTER
 * operand, so the kernel puts 15 in a8 and passes the fixed 0.)
 *
 * Why the XORQ pair is there.  EE.VCMP.LT.S16 compares the SIGNED reading of the two 16-bit lanes
 * (p161: `qa[15:0] = (qx[15:0] < qy[15:0]) ? 0xFFFF : 0`, and the whole VCMP family is .S16/.S8/.S32
 * -- there is no .U16).  For a uint16 depth buffer, "closer" means a smaller UNSIGNED value, so the
 * signed compare is wrong exactly on the lanes where the two depths straddle 32768.  Flipping the
 * sign bit of both operands turns the unsigned order into the signed order:
 *
 *     u < v   <=>   (u ^ 0x8000) <s (v ^ 0x8000)          for all u, v in [0, 65535]
 *
 * so two EE.XORQ (p297, qa = qx ^ qy, bitwise over the whole 128 bits) make the LT exact over the
 * full uint16 range.  Without them the kernel is exact only when both depths of a lane are in
 * 0..32767 (a 15-bit depth buffer) -- ex20_raster_setup.md counts both variants over a sweep and
 * reports the diverging lanes, which are exactly the lanes where the pair straddles 32768.
 *
 * Why the MAC's multiplicand is q6 (0x8000) and the readout shifts by 15.  EE.VMULAS.U16.ACCX needs a
 * multiplicand, and the obvious one -- "eight lanes of 1" -- is what ex10 built with four
 * EE.MOVI.32.Q; here the constant that already exists is reused instead.  With q6 = 0x8000 in every
 * lane the accumulator holds 32768 * sum_l w_l, and EE.SRS.ACCX's shift operand divides the 2^15 back
 * out (the pseudo-code is `au = sat32(ACCX >> as[5:0])`, p134).  Nothing can saturate on the way:
 * the sum is at most 255 * 32768 = 8355840, far below ACCX's 40-bit clamp and below SRS's 32-bit one.
 * (A first version of this kernel wrote `EE.ZERO.Q q5` + `EE.NOTQ q5, q5` for the multiplicand; NOTQ
 * of zero is 0xFFFF in every lane, i.e. 65535, not 1 -- the model run caught it as a mask of 0xff01
 * instead of 0x00ff, which is why the constant is now the 0x8000 vector and the scale is in the
 * readout.)
 *
 * Why the packed byte is built this way.  The 220-entry list has no mask-extraction instruction: no
 * MOVF/MOVT, nothing that moves a lane comparison into a GPR or a bit of a register, and EE.VCMP writes
 * a 128-bit all-ones/zero mask, not a scalar.  The only cross-lane reducers are EE.VMULAS.*.{ACCX,QACC}
 * (p210-p263), and they need a per-lane multiplicand -- hence the weights vector q4 = {1,2,4,8,16,32,64,
 * 128}: ANDQ keeps each surviving lane's weight and clears the others, and the MAC sums those eight
 * weights (times 2^15, removed by the readout's shift).  EE.ZERO.ACCX (p298) is the one instruction per
 * group that makes ACCX hold one group's byte instead of the running sum of all groups.
 *
 * The expansion back into lanes (what the composite stage does with keep_mask) is three instructions
 * and is written out in ex20_raster_setup.md: EE.VLDBC.16 (p170, broadcast the 16-bit mask word to all
 * eight lanes) then EE.ANDQ with the same weights, then EE.VCMP.GT.S16 against zero -- which rebuilds
 * exactly the 0xFFFF/0 vector the LT produced, for every one of the 256 masks.  The model run checks
 * that round trip, so "bit l of keep_mask[g] is lane l" is a checked statement rather than a comment.
 *
 * ------------------------------------------------------------------------------------------------------
 * B) ex20_edge_setup -- three consecutive vertex differences, four int32 lanes, and the padded fourth lane
 * ------------------------------------------------------------------------------------------------------
 *
 *   void ex20_edge_setup(const int32_t *v, int32_t *edges);
 *
 *   v     8 int32 = {x0, y0, x1, y1, x2, y2, pad0, pad1}, 16-byte aligned.  v[0..5] are the three
 *         vertices in SCREEN SPACE, interleaved (x, y) per vertex.  v[6], v[7] are padding: the caller
 *         may leave them as anything at all -- the kernel never lets them reach a meaningful output
 *         word (see the guarantee below), and it never reads past v[7] (both 128-bit loads and the two
 *         64-bit loads stay inside v[0..7], i.e. inside the 32 bytes the caller owns).
 *   edges  16 int32 = FOUR planes of four int32, 16-byte aligned.  Only lanes 0..2 of each plane are
 *         written; lane 3 of each plane is the padded fourth lane and is NEVER touched:
 *
 *             edges[0..2]   = dx0, dx1, dx2       dx_j = x_{j+1} - x_j  (x3 := x0)
 *             edges[4..6]   = dy0, dy1, dy2       dy_j = y_{j+1} - y_j
 *             edges[8..10]  = sgn(dx0), sgn(dx1), sgn(dx2)
 *             edges[12..14] = sgn(dy0), sgn(dy1), sgn(dy2)
 *
 *             edges[3], edges[7], edges[11], edges[15]   -- NOT WRITTEN.  They keep whatever the caller
 *                                                           had there, and the caller must not read
 *                                                           them: their value would be padding-derived.
 *
 *         sgn() is +1, -1 or 0, delivered as an int32 per lane (a sign, not a boolean), because the
 *         consumer of an edge coefficient wants it for the fill convention, and because a mask in the
 *         next stage wants either 0xFFFFFFFF or 0 -- both spellings are one instruction apart.
 *
 * What each instruction does:
 *
 *   addi            a10, a2, 8      a10 = v + 2 int32 = &v[2] (byte 8)
 *   addi            a11, a2, 16     a11 = v + 4 int32 = &v[4] (byte 16)
 *   EE.VLD.128.IP   q0, a2, 16      q0 = {x0, y0, x1, y1}                       (bytes  0..15)
 *   EE.VLD.L.64.IP  q1, a10, 8      q1 LOW  64 bits = {x1, y1}  (bytes  8..15)
 *   EE.VLD.128.IP   q3, a11, 0      q3 = {x2, y2, pad0, pad1}                   (bytes 16..31)
 *   EE.VLD.H.64.IP  q1, a11, 8      q1 HIGH 64 bits = {x2, y2}  (bytes 16..23)
 *   EE.ZERO.Q       q7
 *   EE.VSUBS.S32    q2, q1, q0      q2 = {x1-x0, y1-y0, x2-x1, y2-y1} = {dx0,dy0,dx1,dy1}
 *   EE.VSUBS.S32    q4, q0, q3      q4 = {x0-x2, y0-y2, x1-pad0, y1-pad1}
 *                                        lane 0,1 = {dx2, dy2}; lane 2,3 are padding-derived
 *   EE.VUNZIP.32    q2, q4          q2 = {dx0, dx1, dx2, x1-pad0}   q4 = {dy0, dy1, dy2, y1-pad1}
 *   EE.VCMP.GT.S32  q5, q2, q7      q5 = (dx > 0) ? -1 : 0
 *   EE.VCMP.LT.S32  q6, q2, q7      q6 = (dx < 0) ? -1 : 0
 *   EE.VSUBS.S32    q5, q6, q5      q5 = sgn(dx) = {+1, -1, 0}          (LT mask - GT mask)
 *   EE.VCMP.GT.S32  q6, q4, q7
 *   EE.VCMP.LT.S32  q1, q4, q7
 *   EE.VSUBS.S32    q6, q1, q6      q6 = sgn(dy)
 *   then, per plane, exactly 12 bytes are stored (lanes 0..2) with 4 instructions:
 *       EE.VST.L.64.IP q, a3, 8     store lanes 0,1 (8 bytes); a3 += 8
 *       EE.MOVI.32.A   q, a9, 2     a9 = lane 2 of that plane            (p118: au = qs[sel4])
 *       s32i           a9, a3, 0    store lane 2 at plane+8
 *       addi           a3, a3, 8    a3 = plane+16
 *
 * The sign is a subtraction of two masks because the two masks are already all-ones/zero: with q6 = the
 * "less than" mask (-1 in each negative lane) and q5 = the "greater than" mask (-1 in each positive
 * lane), q6 - q5 is -1 where the value is negative, +1 where it is positive and 0 where it is zero --
 * no selection, no boolean materialisation, and EE.VSUBS.S32's saturation cannot bite because the
 * operands are 0 and -1 (TRM p284 clamps at +/-2^31, which this never approaches).
 *
 * The three differences at once come from the interleaved layout, not from a shuffle: the vector
 * starting at byte 8 is {x1, y1, x2, y2}, so one 32-bit-lane subtraction against {x0, y0, x1, y1} is
 * already (v1-v0, v2-v1) in four lanes.  The third edge is the closing one, {x0-x2, y0-y2}, which is
 * the same subtraction run the other way against the load of bytes 16..31.  Getting the two 64-bit
 * halves of {x1,y1,x2,y2} out of an 8-byte-misaligned window is what EE.VLD.L.64.IP / EE.VLD.H.64.IP
 * (p168 / p166) are for: each writes ONE half of the destination register and leaves the other half
 * alone, and each forces only the low THREE address bits to 0, so byte 8 and byte 16 of a 16-byte
 * aligned v are loaded exactly -- no funnel shift, no staging copy, no reliance on the p49 rounding
 * of a 128-bit access.  (The 128-bit loads still have the usual requirement: v 16-byte aligned.)
 * EE.VUNZIP.32 (p291) then de-interleaves the two (dx,dy) vectors into planes: with P = {dx0,dy0,dx1,
 * dy1} and Q = {dx2,dy2,*,*}, the pseudo-code's old-value reading gives {P0,P2,Q0,Q2} and {P1,P3,Q1,
 * Q3} = the dx plane and the dy plane.  The loads and their uses are also ordered for Table 1.7-2, where
 * every 64/128-bit load has its def at stage M with a use at E: the two VSUBS.S32 sit three and four
 * instructions after the loads that feed them (see the scheduling section of the .md).
 *
 * THE PADDED FOURTH LANE -- the two guarantees, both checked in the model run:
 *
 *   1. never written: the stores above cover plane+0..plane+11 (one 64-bit store and one 32-bit store),
 *      so edges[3], edges[7], edges[11] and edges[15] are bit-identical before and after the call, for
 *      any input.  A EE.VST.128.IP of the plane would have written all 16 bytes including that lane --
 *      which is why the plane's last word is written with s32i instead.
 *   2. never influences lanes 0..2: v[6] and v[7] are read (they are inside the bytes 16..31 load) and
 *      they DO reach lane 3 of q4/q2/q4, but every instruction in the kernel is lane-parallel, so no
 *      lane 3 value can cross into lanes 0..2.  The model run drives v[6], v[7] over adversarial
 *      values (0, -1, 0x7FFFFFFF, 0x80000000) and requires the 12 stored words to be bit-identical
 *      every time, and it also reports which output lane each pad word lands in (lane 3 of the dx and
 *      dy planes only).
 *
 * A note on the "fourth lane" of the INPUT: the vertex vector is 3 vertices wide and the padded fourth
 * slot of a four-lane group is the depth test's business, not this kernel's -- what matters here is that
 * the fourth SLOT OF THE OUTPUT is the padding, because the three edges occupy exactly three lanes of
 * each plane.  Same statement, either way: three elements, one padded lane, and a stated rule for it.
 *
 * ------------------------------------------------------------------------------------------------------
 * C) ex20_attr_interp -- advancing a Q0.16 attribute 8 lanes at a time
 * ------------------------------------------------------------------------------------------------------
 *
 *   void ex20_attr_interp(const int16_t *a_top, const int16_t *a_bot, int16_t *out, uint32_t n,
 *                         uint32_t shift);
 *
 *   a_top, a_bot  n*8 int16 lanes each, 16-byte aligned.  Lane l of group g holds one span's attribute
 *                 at the START (a_top) and at the END (a_bot) of the chunk, in Q0.16: 16 fractional
 *                 bits, so the lane value v means v/65536 and the representable range is [-0.5, 0.5).
 *   out           n*8 int16 lanes, 16-byte aligned.
 *   n             number of 8-lane groups.
 *   shift         the fixed-point scale of the advance: shift = log2(chunk length in pixels), i.e. 3
 *                 for the eight-lane chunk this kernel walks.  Only SAR[5:0] reaches the instruction
 *                 (p198: `>> SAR[5:0]`), so the caller passes 0..15 and the exactness statement below
 *                 is a separate matter.  MUST NOT be > 15.
 *
 *   out[8g+l] = sat16( a_top[8g+l] + ( (sat16(a_bot[8g+l] - a_top[8g+l]) * l) >> shift ) )
 *
 * i.e. lane l gets the attribute l/8 of the way from a_top to a_bot (value l/8 = l * 2^-3 in Q0.16,
 * which is what the multiply by the lane ramp {0,1,2,3,4,5,6,7} followed by >> 3 evaluates exactly).
 * Lane 0 is a_top itself; lane 7 is 7/8 of the way.  The intended use is the rasteriser's inner loop:
 * the caller keeps the span's start and end attribute vectors and walks the chunk in one call, or
 * advances several spans at once (each lane carrying its own span's pair).
 *
 * Per group, 9 instructions (7 + the two loop ones are listed below):
 *
 *   EE.VLD.128.IP  q0, a2, 16    a_top lanes
 *   EE.VLD.128.IP  q1, a3, 16    a_bot lanes
 *   addi.n         a5, a5, -1
 *   EE.VSUBS.S16   q2, q1, q0    d = sat16(a_bot - a_top)        <-- SATURATES
 *   EE.VMUL.S16    q3, q2, q4    q3 = (d * l) >> SAR             <-- does NOT saturate (see below)
 *   nop.n                        Table 1.7-2: EE.VMUL.S16's qdef is at stage M (p70), a use at E
 *   EE.VADDS.S16   q2, q0, q3    out = sat16(a_top + q3)         <-- SATURATES
 *   EE.VST.128.IP  q2, a4, 16
 *   bnez           a5, .Lattr
 *
 * WHERE THE SATURATION IS AND WHERE IT IS NOT (this is the point of the kernel):
 *
 *   1. EE.VSUBS.S16 (p281) SATURATES: d = min(max(a_bot - a_top, -2^15), 2^15-1).  Exact as long as
 *      a_bot - a_top is representable in int16; when the two endpoints straddle the range (a_top =
 *      -32768 and a_bot = 32767, say) the difference is clamped to 32767 instead of 65535 and every
 *      lane of that group is then off.
 *   2. EE.VMUL.S16 (p198) NEVER SATURATES.  Its pseudo-code is `qz[l] = (qx[l] * qy[l]) >> SAR[5:0]`
 *      with a 32-bit product and an ARITHMETIC shift, and the instruction writes the LOW 16 BITS of
 *      that shifted product -- a truncation, i.e. a wrap, not a clamp.  Two consequences:
 *        - the product is computed in 32 bits, so (d * l) for d = +/-32768 and l = 7 is +/-229376 and
 *          nothing wraps there (the largest thing the vector unit sees is 229376, not 32767);
 *        - the shifted result |d| * 7 / 2^shift fits in int16 exactly when shift >= 3, i.e. for the
 *          eight-lane chunk this kernel is written for the multiply CANNOT wrap, for any int16 d.
 *          With shift < 3 it can (> 32767), and then the low 16 bits are stored -- silently, as a
 *          wrapped value, not as 32767.  ex20_raster_setup.md's model run counts those lanes.
 *   3. EE.VADDS.S16 (p146) SATURATES: out = min(max(a_top + step, -2^15), 2^15-1).  Reached at the
 *      outer lanes when a_top is near the top of the range, e.g. a_top = 30000 with a positive delta.
 *   4. the shift itself loses the low `shift` bits of the product (floor toward -inf, because the shift
 *      is arithmetic): that is the Q0.16 quantisation of the advance, not an error.
 *
 * So: exact for every int16 pair when shift >= 3 and the two saturating steps are not reached -- which
 * in Q0.16 terms is "the two endpoints are within half a unit of each other", the normal case for the
 * per-chunk attribute pairs a span DDA holds.  The model run sweeps both boundaries and prints the
 * divergence counts per cause rather than claiming the kernel is exact.
 *
 * The lane ramp {0,1,2,3,4,5,6,7} is built in registers, with no memory and no literal pool: the pair
 * value 0x00010000 is one {0,1} in 16-bit lanes, and the pair step 0x00020002 adds {2,2} to both lanes
 * at once, so the four 32-bit segments 0x00010000 / 0x00030002 / 0x00050004 / 0x00070006 are
 * base + k * step for k = 0..3 -- seven GPR instructions and four EE.MOVI.32.Q (p119).  Building the
 * constants in registers rather than loading them is not a preference here: none of the three
 * signatures takes a pointer to constants, and .iram1 code cannot reach .rodata through a literal pool
 * (`l32r` from .iram1 into .rodata is refused by the linker, which is why the other examples take their
 * constants as C pointers).  Kernel A builds its three constants the same way.
 *
 * ------------------------------------------------------------------------------------------------------
 * SAR is one register for the whole core -- read this before calling two of these kernels back to back
 * ------------------------------------------------------------------------------------------------------
 *
 * Only kernel C programs SAR (`ssr a6`, p198's `>> SAR[5:0]`).  Kernels A and B use no shift-by-SAR
 * instruction at all, so they neither read nor write it.  A caller that interleaves C with other
 * SAR-using code (EE.VMUL.*, EE.VSL.32/VSR.32, EE.RELU, EE.CMUL.S16, EE.SRCMB.*) must re-program it;
 * there is one SAR for the whole core, not one per QR.
 *
 * Everything here except the calls is PASSIVE.  All three kernels are PROPOSED: they are not wired into
 * main.c / examples.h / CMakeLists.txt (this suite's build lists its sources explicitly, so a file in
 * proposed/ is inert until someone adds it).  See ex20_raster_setup.md next to this file for the scalar
 * C reference, the Python model of all three kernels, the packing/expansion proof, and the list of
 * semantics that were not confirmed on silicon -- in particular that none of this has been run on an
 * ESP32-S3, so nothing below is a statement about the hardware.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* ------------------------------------------------------------------------------------------------ A */

/* void ex20_depth_test(const uint16_t *z_buf, const uint16_t *z_new, uint16_t *keep_mask, uint32_t n)
 *   a2 = z_buf, a3 = z_new : n*8 uint16 lanes, 16-byte aligned
 *   a4 = keep_mask         : n uint16, 2-byte aligned; bit l of word g = lane l of group g
 *   a5 = n                 : number of 8-lane groups (n == 0 writes nothing)
 *   q4 = weights {1,2,4,8,16,32,64,128} in the eight 16-bit lanes
 *   q6 = 0x8000 in every lane: the relabel for EE.VCMP.LT.S16 and the MAC's multiplicand (a8 = 15)
 *   a8 = 15 : the EE.SRS.ACCX shift that removes the MAC's 2^15 scale
 */
    .global ex20_depth_test
    .type ex20_depth_test, @function
ex20_depth_test:
    entry a1, 64
    /* ABI: this kernel writes a10 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    beqz    a5, .Ldepth_done
    movi.n  a8, 15                      /* the SRS.ACCX shift: it removes the MAC's 2^15 scale */

    /* q6 = 0x8000 in all eight 16-bit lanes: the unsigned -> signed relabel AND the MAC's
     * multiplicand (a9 = 1 << 15, duplicated into the high 16-bit lane of each 32-bit segment). */
    movi    a9, 1
    slli    a9, a9, 15                  /* a9  = 0x00008000 */
    slli    a10, a9, 16                 /* a10 = 0x80000000 */
    or      a9, a9, a10                 /* a9  = 0x80008000 */
    ee.movi.32.q q6, a9, 0
    ee.movi.32.q q6, a9, 1
    ee.movi.32.q q6, a9, 2
    ee.movi.32.q q6, a9, 3

    /* q4 = {1,2,4,8,16,32,64,128}: each 32-bit segment is two weights, and segment k+1 is segment k
     * shifted left by 2 -- segment 0 = (2 << 16) | 1 = 0x00020001. */
    movi    a9, 2
    slli    a9, a9, 16                  /* 0x00020000 */
    addi    a9, a9, 1                   /* 0x00020001 = lanes {1, 2} */
    ee.movi.32.q q4, a9, 0
    slli    a10, a9, 2
    ee.movi.32.q q4, a10, 1             /* 0x00080004 = lanes {4, 8} */
    slli    a10, a9, 4
    ee.movi.32.q q4, a10, 2             /* 0x00200010 = lanes {16, 32} */
    slli    a10, a9, 6
    ee.movi.32.q q4, a10, 3             /* 0x00800040 = lanes {64, 128} */

.Ldepth_loop:
    ee.vld.128.ip q0, a2, 16            /* z_buf lanes; def at M (pipeline p69) */
    ee.zero.accx                        /* ACCX = 0: this group's packed byte, not a running sum (p298) */
    ee.vld.128.ip q1, a3, 16            /* z_new lanes */
    ee.xorq  q0, q0, q6                 /* relabel: unsigned order -> signed order (p297) */
    ee.xorq  q1, q1, q6
    ee.vcmp.lt.s16 q2, q1, q0           /* q2[l] = 0xFFFF where z_new[l] < z_buf[l] (p161) */
    ee.andq  q2, q2, q4                 /* surviving lane -> its own bit weight (p76) */
    ee.vmulas.u16.accx q2, q6           /* ACCX = 32768 * (packed byte) (p240) */
    addi.n  a5, a5, -1                  /* filler 1: D = max(2-1+1,0) = 2 for MAC -> SRS (p71/p68) */
    nop.n                               /* filler 2 */
    ee.srs.accx a6, a8, 0               /* a6 = sat32(ACCX >> a8[5:0]) = the packed byte (p134) */
    s16i    a6, a4, 0                   /* keep_mask[g] = packed byte (<= 255) */
    addi    a4, a4, 2                   /* next keep_mask word */
    bnez    a5, .Ldepth_loop

.Ldepth_done:
    /* ABI: put the caller's a2 back before returning. */
    l32i a10, a1, 32
    retw.n
    .size ex20_depth_test, . - ex20_depth_test

/* ------------------------------------------------------------------------------------------------ B */

/* void ex20_edge_setup(const int32_t *v, int32_t *edges)
 *   a2 = v     : 8 int32 = {x0,y0,x1,y1,x2,y2,pad0,pad1}, 16-byte aligned (only v[0..5] are data)
 *   a3 = edges : 16 int32 = four planes of four int32, 16-byte aligned; the kernel writes the first
 *                12 bytes of each plane and never touches lane 3 of any plane
 *                edges[0..2]=dx, edges[4..6]=dy, edges[8..10]=sgn(dx), edges[12..14]=sgn(dy)
 */
    .align 4
    .global ex20_edge_setup
    .type ex20_edge_setup, @function
ex20_edge_setup:
    entry a1, 64
    /* ABI: this kernel writes a10, a11 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    addi    a10, a2, 8                  /* byte 8  = &v[2] : the 8-byte-aligned window {x1,y1,...} */
    addi    a11, a2, 16                 /* byte 16 = &v[4] : the 16-byte-aligned window {x2,y2,...} */
    ee.vld.128.ip q0, a2, 16            /* q0 = {x0, y0, x1, y1} */
    ee.vld.l.64.ip q1, a10, 8           /* q1 LOW  64 bits = {x1, y1} (p168: the low half only) */
    ee.vld.128.ip q3, a11, 0            /* q3 = {x2, y2, pad0, pad1} */
    ee.vld.h.64.ip q1, a11, 8           /* q1 HIGH 64 bits = {x2, y2} (p166: the high half only) */
    ee.zero.q q7                        /* q7 = 0, the compare's second operand (p299) */
    ee.vsubs.s32 q2, q1, q0             /* {x1-x0, y1-y0, x2-x1, y2-y1} = {dx0,dy0,dx1,dy1} (p284) */
    ee.vsubs.s32 q4, q0, q3             /* lane0,1 = {dx2,dy2}; lane2,3 = v[6]/v[7]-derived */
    ee.vunzip.32 q2, q4                 /* de-interleave: q2 = dx plane, q4 = dy plane (p291) */
    ee.vcmp.gt.s32 q5, q2, q7           /* (dx > 0) ? -1 : 0  (p159) */
    ee.vcmp.lt.s32 q6, q2, q7           /* (dx < 0) ? -1 : 0  (p162) */
    ee.vsubs.s32 q5, q6, q5             /* sgn(dx) = {+1, -1, 0} per lane */
    ee.vcmp.gt.s32 q6, q4, q7
    ee.vcmp.lt.s32 q1, q4, q7
    ee.vsubs.s32 q6, q1, q6             /* sgn(dy) */

    /* Plane 0: dx -- lanes 0..2 only (12 bytes), so the padded lane 3 stays the caller's. */
    ee.vst.l.64.ip q2, a3, 8            /* edges[0], edges[1] (8 bytes) */
    ee.movi.32.a q2, a9, 2              /* a9 = lane 2 = dx2 (p118: qs first, then au) */
    s32i    a9, a3, 0                   /* edges[2] = dx2     (a3 now points at plane+8) */
    addi    a3, a3, 8                   /* -> plane+16 */
    /* Plane 1: dy */
    ee.vst.l.64.ip q4, a3, 8
    ee.movi.32.a q4, a9, 2
    s32i    a9, a3, 0
    addi    a3, a3, 8
    /* Plane 2: sgn(dx) */
    ee.vst.l.64.ip q5, a3, 8
    ee.movi.32.a q5, a9, 2
    s32i    a9, a3, 0
    addi    a3, a3, 8
    /* Plane 3: sgn(dy) */
    ee.vst.l.64.ip q6, a3, 8
    ee.movi.32.a q6, a9, 2
    s32i    a9, a3, 0
    /* ABI: put the caller's a2/a3 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    retw.n
    .size ex20_edge_setup, . - ex20_edge_setup

/* ------------------------------------------------------------------------------------------------ C */

/* void ex20_attr_interp(const int16_t *a_top, const int16_t *a_bot, int16_t *out, uint32_t n,
 *                       uint32_t shift)
 *   a2 = a_top, a3 = a_bot : n*8 int16 lanes (Q0.16), 16-byte aligned
 *   a4 = out               : n*8 int16 lanes, 16-byte aligned
 *   a5 = n                 : number of 8-lane groups
 *   a6 = shift             : SAR for the advance's scale (3 = eight-lane chunk), MUST be <= 15
 *   q4 = the lane ramp {0,1,2,3,4,5,6,7}
 */
    .align 4
    .global ex20_attr_interp
    .type ex20_attr_interp, @function
ex20_attr_interp:
    entry a1, 64
    /* ABI: this kernel writes a10, a11 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    beqz    a5, .Lattr_done

    /* q4 = {0,1,2,3,4,5,6,7} in eight int16 lanes: the four 32-bit segments are base + k * step with
     * base = 0x00010000 (the lane pair {0,1}) and step = 0x00020002 (adds {2,2} to both lanes at
     * once).  The chain must start from base, not from 0x00010001: the adds are 32-bit, so adding
     * 0x00020002 to 0x00010001 carries into the low lane and yields {3,3} instead of {2,3} -- a defect
     * the model run in ex20_raster_setup.md caught (it read the ramp out of q4 as
     * {0,1,3,3,5,5,7,7}). */
    movi    a9, 1
    slli    a9, a9, 16                  /* a9  = 0x00010000 = lanes {0,1} */
    ee.movi.32.q q4, a9, 0
    addi    a10, a9, 1                  /* a10 = 0x00010001 : {1,1}, the pair the step is measured in */
    slli    a11, a10, 1                 /* a11 = 0x00020002 = the segment step {2,2} */
    add     a9, a9, a11                 /* 0x00030002 = lanes {2,3} */
    ee.movi.32.q q4, a9, 1
    add     a9, a9, a11                 /* 0x00050004 = lanes {4,5} */
    ee.movi.32.q q4, a9, 2
    add     a9, a9, a11                 /* 0x00070006 = lanes {6,7} */
    ee.movi.32.q q4, a9, 3
    ssr     a6                          /* SAR = shift: the >> that scales the advance (p198) */

.Lattr_loop:
    ee.vld.128.ip q0, a2, 16            /* a_top lanes (def at M) */
    ee.vld.128.ip q1, a3, 16            /* a_bot lanes */
    addi.n  a5, a5, -1                  /* loop bookkeeping, while the loads are in flight */
    ee.vsubs.s16 q2, q1, q0             /* d = sat16(a_bot - a_top) -- SATURATES (p281) */
    ee.vmul.s16 q3, q2, q4              /* (d * l) >> SAR -- 32-bit product, arithmetic >>, low 16
                                         * bits stored: truncates, never saturates (p198) */
    nop.n                               /* EE.VMUL.S16's qdef is at M (pipeline p70), use at E */
    ee.vadds.s16 q2, q0, q3             /* out = sat16(a_top + step) -- SATURATES (p146) */
    ee.vst.128.ip q2, a4, 16
    bnez    a5, .Lattr_loop

.Lattr_done:
    /* ABI: put the caller's a2/a3 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    retw.n
    .size ex20_attr_interp, . - ex20_attr_interp
