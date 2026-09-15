/* ex18 -- codec part 3: Opus/CELT stereo reconstruction and the pitch comb filter, in 16-bit lanes.
 *
 * Third of the CELT/Opus examples (notes/08-media-3d-perf.md plans this area; ex16/ex17 are the
 * MDCT/twiddle stages). This file is the two per-sample stages that surround the MDCT:
 *
 *   A) ex18_stereo_ms        CELT's stereo_merge (celt/bands.c:418-467) reduced to 16-bit lanes
 *   B) ex18_comb_filter      CELT's pitch comb filter (celt/celt.c:166-193 comb_filter_const_c)
 *   C) ex18_intensity_stereo CELT's intensity_stereo (celt/bands.c:379-403)
 *
 * The whole point of the file is the ORDER of rounding and saturation, so it is worth stating the two
 * references first, from the source, before any instruction:
 *
 *   A. bands.c:458-466, per element:
 *        l = MULT32_32_Q31(mid, X[j]);  r = Y[j];
 *        X[j] = VSHR32(MULT32_32_Q31(lgain, SUB32(l,r)), kl-15);
 *        Y[j] = VSHR32(MULT32_32_Q31(rgain, ADD32(l,r)), kr-15);
 *      On xtensa there is NO int64 (OPUS_FAST_INT64 is 0: celt/arch.h:125-128 selects the 64-bit
 *      macros only for x86_64/LP64/Win64/mips), so MULT32_32_Q31 is the decomposed form
 *      celt/fixed_generic.h:71 and VSHR32/SHR32 are plain arithmetic shifts with NO rounding
 *      (fixed_generic.h:118-125: PSHR32 is the rounding one and is NOT used here). ADD32/SUB32 are
 *      plain 32-bit adds, also without saturation (arch.h:110-111). The only narrowing in A is the
 *      implicit store into celt_norm, which in this tree is `typedef opus_val32 celt_norm`
 *      (arch.h:155) -- i.e. the C keeps the full 32-bit value and a 16-bit lane build must decide
 *      what to do with the bits that do not fit. THAT decision is this file's subject: see the .md.
 *
 *   B. celt.c:166-193 (the non-ARM, non-QEXT path; ENABLE_QEXT is not defined in this build and
 *      celt_coef is opus_val16 -- arch.h:193), per element:
 *        y[i] = x[i] + MULT16_32_Q15(g10, x[i-T]) + MULT16_32_Q15(g11, x[i-T+1] + x[i-T-1])
 *                    + MULT16_32_Q15(g12, x[i-T+2] + x[i-T-2]);
 *        y[i] = SUB32(y[i], 1);                    <- the deliberate fixed-point bias
 *        y[i] = SATURATE(y[i], SIG_SAT);           <- SIG_SAT = 536870911 = 2^29-1 (arch.h:215)
 *      With OPUS_FAST_INT64 = 0, MULT16_32_Q15(a,b) is fixed_generic.h:57 =
 *      ADD32(SHL(MULT16_16(a, SHR(b,16)),1), SHR(MULT16_16SU(a, b&0xffff),15)), which is exactly
 *      floor(a*b / 2^15) -- ONE TRUNCATION PER TAP, three of them summed, and the clamp is at +-2^29
 *      so it can never fire on this data (arch.h:207-215 says why: the clamp only has to admit a
 *      full-scale tone through the prefilter). Reading order is the whole contract of B: x[-T-2] is
 *      the EARLIEST sample touched and the loop never needs anything past x[N-1]; there is no
 *      zero-fill and no skipping inside the filter -- the missing-tap question is answered by the
 *      caller's buffer (celt_decoder.c:599 / celt.h:236-237: COMBFILTER_MAXPERIOD = 1024 samples of
 *      state, zero-cleared at init, and T >= COMBFILTER_MINPERIOD = 15 enforced at celt.c:265-266).
 *
 * WHAT THIS FILE CANNOT PRESERVE, and says so up front:
 *   - A uses `shift` = the C's (k-15) and assumes lgain = rgain = 2^31 (unity) and kl == kr; the two
 *     gains and the two ilogs are computed from band energies (bands.c:429-456, celt_rsqrt_norm32)
 *     and cannot live in a per-sample int16 kernel with one scalar parameter.
 *   - B uses the CELT gain table directly, which is what the C computes only when the postfilter gain
 *     is g = Q15ONE (g00 = MULT16_16_P15(g0, gains[..][0])). That is true for six of the seven table
 *     entries and FALSE for tapset 2's first tap: (16384 + 32767*26208) >> 15 = 26207, one LSB below
 *     the table's 26208 (verified by running the arithmetic -- see the .md). A 1-LSB gain error.
 *   - B's readout shift is a runtime parameter (SRCMB takes it from a register); CELT's value is 15.
 *
 * WHY THE 16-BIT LANES NEED A TRICK FOR THE SUM/DIFFERENCE. mid-side and mid+side need 17 bits
 * (|a +- b| <= 65535) while the operands are 16-bit lanes. The identity used in A is
 *
 *     floor(D / 2^s) = floor( floor(D/2) / 2^(s-1))          (s >= 1)
 *
 * and floor(D/2) for D = a - b is (a>>1) - (b>>1) - borrow with borrow = (a even) & (b odd), which
 * fits 16 bits; for D = a + b it is (a>>1) + (b>>1) + carry with carry = (a odd) & (b odd). Both
 * half-values are PROVABLY in range (checked in the .md: the two extrema never overflow), so
 * EE.VSUBS.S16 / EE.VADDS.S16 never saturate here even though they are saturating instructions.
 * The half is produced by EE.VMUL.S16 with a lane of 2^14 and SAR = 15, i.e. (a*2^14)>>15 = a>>1,
 * and the final shift by (s-1) by the same multiply with SAR = s+13. EE.VMUL.S16 is a real
 * truncating shift (TRM p198: (qx*qy) >> SAR, low 16 bits of the product), which is exactly SHR32.
 * For shift == 0 the C's store wraps (there is no room for the 17th bit), and the second loop here
 * reproduces that wrap: (2*half + lowbit) mod 2^16, where the +lowbit can never saturate because
 * 2*half is even. A naive lane build -- subtract first, shift after -- saturates the 17-bit
 * intermediate instead and disagrees with the C; the .md measures both forms.
 *
 * THE COMB FILTER IS BUILT ON THE SATURATING LANE MAC. EE.VSMULAS.S16.QACC (TRM p269) multiplies
 * one broadcast lane of qy by all eight lanes of qx and adds INTO THE CORRESPONDING 40-BIT LANES of
 * QACC (eight independent accumulators, saturating at +-2^39), and EE.SRCMB.S16.QACC (TRM p130)
 * shifts every 40-bit lane right by as[5:0] and writes them out as eight SATURATED int16 lanes.
 * That pair is the natural 16-bit-lane MAC+readout; the alternative EE.VMULAS.S16.ACCX is NOT used
 * because it horizontally SUMS the eight lane products into one accumulator (measured:
 * data/pie_examples_measured.json :: vmulas_s16_accx_sums_the_lanes), which destroys the eight
 * independent outputs a comb filter needs. The price of the single shift at the end is stated and
 * measured in the .md: CELT truncates three times (once per tap) and this kernel truncates once, so
 * the two differ by the rounding residue of the shift distribution, bounded by the number of taps.
 *
 * THE FIVE TAPS COME FROM ONE ALIGNED PAIR. hist[j] == x[j-T] holds the pitch-delayed input, so the
 * five taps of output i are hist[i-2 .. i+2] -- five 16-byte windows of one stream. A 128-bit access
 * forms its address as {as[31:4],4{0}} (TRM p49), so the windows are produced by the ex15 idiom:
 * EE.LD.128.USAR.IP (TRM p93) loads the aligned chunk CONTAINING the pointer and parks the dropped
 * low bits in SAR_BYTE; EE.SRC.Q (TRM p125) then returns {qs1,qs0} >> 8*SAR_BYTE. The five windows
 * need SAR_BYTE = 14, 15, 0, 1, 2 and three aligned chunks (Cm1, C0, C1); SAR_BYTE = 0 is the raw
 * C0 (no SRC.Q at all) and the other four come from four "poke" loads whose ADDRESS carries the
 * phase -- each poke reads a chunk the kernel is already loading, so it adds no memory footprint.
 *
 * THIS FILE IS PROPOSED. It is not in examples/firmware/main/CMakeLists.txt, it is not declared in
 * examples.h, it is not called by main.c, and it has NOT run on silicon: nothing in this file or in
 * ex18_stereo_comb.md next to it was produced with hardware. The .md carries the contract tables,
 * the Python model with its sweep against a host-compiled C transliteration of the three CELT
 * functions, the assembler/objdump transcript, the TRM page of every instruction used, the
 * piesim.py interpretation, and the list of assumptions that are not measurements.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* ===========================================================================================
 * A) void ex18_stereo_ms(const int16_t *mid, const int16_t *side, int16_t *l, int16_t *r,
 *                        uint32_t n, uint32_t shift)
 *   a2 = mid   a3 = side   a4 = l   a5 = r   a6 = n   a7 = shift
 *
 * Contract (the caller's side of it -- see the .md for why these all follow from bands.c):
 *   mid[]  C's `l = MULT32_32_Q31(mid_norm, X[j])`, already reduced to int16. That reduction is
 *          exact: bands.c computes it on the band vector X (celt_norm, +-2^15) with a Q31 scalar
 *          <= 2^31, so |l| <= |X[j]| <= 32768 and an int16 lane holds it without loss.
 *   side[] C's `r = Y[j]`.
 *   l/r    out: l[j] = (mid[j] - side[j]) >> shift, r[j] = (mid[j] + side[j]) >> shift, arithmetic
 *          shift, no rounding, and the C's own narrowing (see the shift == 0 loop).
 *   shift  the C's (kl-15), kl == kr. shift >= 1 uses the truncating shift path; shift == 0 uses the
 *          wrap path (kl == 15). kl < 15 (a LEFT shift in the C) is out of contract: the caller
 *          pre-scales by 2^(15-kl) (the .md lists this as an assumption).
 *   n      multiple of 8, n >= 8. All four buffers 16-byte aligned and non-overlapping.
 *
 * Register walk: a2..a5 walk with the .IP forms, a6 counts 8-sample chunks, a8 = 15 (SAR for the
 * half), a9 = shift+13 (SAR for the final shift), a10 = 0 (SAR for the shift == 0 doubling).
 * q0 = 0x4000 in every lane (2^14: the multiplier that turns a lane into a shift), q1 = 0x0001 in
 * every lane (the low-bit mask / the lane addend), q6/q7 = the two windows, q2/q3/q4/q5 = working.
 *
 * Schedule: static model of TRM Table 1.7-2 (data/pie_hazards.md). EE.VMUL.S16 defines qz at M (2)
 * and every consumer reads at E (1), so a multiply result is left TWO issue slots before its use;
 * EE.VSUBS/VADDS/ANDQ define at E, so back-to-back use is free. EE.VST.128.IP reads qv at E, so it
 * is placed two slots after the multiply that fills it. Two wsr.sar per iteration (15 and shift+13);
 * the Xtensa base ISA has no stage table, so their placement is an assumption, not a measurement.
 * =========================================================================================== */
    .align 4
    .global ex18_stereo_ms
    .type ex18_stereo_ms,@function
ex18_stereo_ms:
    entry a1, 64
    /* ABI: this kernel writes a10, a11 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    srli a6, a6, 3                  /* n -> 8-sample chunks (the contract makes the shift exact) */
    beqz a6, .Lms_done
    movi a8, 64
    slli a8, a8, 8                  /* 64*256 = 16384; written this way because movi's immediate is
                                     * 12-bit signed and the assembler would otherwise emit an l32r
                                     * (which the .iram1 rule in this suite does not want: no literal
                                     * pool reference from a kernel) */
    slli a11, a8, 16
    or a8, a8, a11                  /* 0x40004000: EE.MOVI.32.Q stores a WORD, so a per-lane
                                     * broadcast needs the same 16-bit value in BOTH halves of it
                                     * (this is the bug the model's T1 caught: 0x00004000 leaves every
                                     * other lane zero) */
    EE.MOVI.32.Q q0, a8, 0
    EE.MOVI.32.Q q0, a8, 1
    EE.MOVI.32.Q q0, a8, 2
    EE.MOVI.32.Q q0, a8, 3          /* q0 = 2^14 in every 16-bit lane */
    movi a9, 1
    slli a11, a9, 16
    or a9, a9, a11                  /* 0x00010001 */
    EE.MOVI.32.Q q1, a9, 0
    EE.MOVI.32.Q q1, a9, 1
    EE.MOVI.32.Q q1, a9, 2
    EE.MOVI.32.Q q1, a9, 3          /* q1 = 1 in every lane */
    addi a9, a7, 13                 /* the final SAR = shift + 13 ((x*2^14)>>(shift+13) == x>>(shift-1)) */
    movi a8, 15                     /* SAR for the halves: (x * 2^14) >> 15 == x >> 1 */
    movi a10, 13                    /* SAR for the shift == 0 doubling: (x * 2^14) >> 13 == x << 1 */
                                    /* (a left shift by one, done in 32 bits and truncated to the
                                     * lane, which is what the C's 17-bit value needs: a saturating
                                     * 16-bit add could not hold 2*32767) */
    beqz a7, .Lms_shift0            /* shift == 0: the C's narrowing WRAPS, so no shift can be used */
/* ---- shift >= 1: floor((mid -+ side) / 2^shift) exactly, no rounding, no saturation -------------- */
.Lms_loop:
    wsr.sar a8                      /* SAR = 15 */
    EE.VLD.128.IP q6, a2, 16        /* mid window */
    EE.VLD.128.IP q7, a3, 16        /* side window */
    EE.ANDQ q3, q6, q1              /* Pl = mid & 1 */
    EE.ANDQ q2, q7, q1              /* Pr = side & 1 */
    EE.VMUL.S16 q5, q6, q0          /* A = mid >> 1                                   (qz @ M) */
    EE.ANDQ q4, q3, q2              /* carry = Pl & Pr */
    EE.VMUL.S16 q6, q7, q0          /* B = side >> 1                                  (qz @ M) */
    EE.VSUBS.S16 q2, q2, q4         /* borrow = Pr - carry (== (Pl == 0) & Pr) */
    wsr.sar a9                      /* SAR = shift + 13 */
    EE.VSUBS.S16 q7, q5, q6         /* A - B        (q5 defined 7 slots back, q6 2 back: 0 stalls) */
    EE.VSUBS.S16 q7, q7, q2         /* half-diff = floor((mid - side)/2) */
    EE.VADDS.S16 q5, q5, q6         /* A + B */
    EE.VADDS.S16 q3, q5, q4         /* half-sum = floor((mid + side)/2) */
    EE.VMUL.S16 q7, q7, q0          /* l[j] = half-diff >> (shift-1) = (mid - side) >> shift */
    EE.VMUL.S16 q3, q3, q0          /* r[j] = half-sum  >> (shift-1) = (mid + side) >> shift */
    EE.VST.128.IP q7, a4, 16
    EE.VST.128.IP q3, a5, 16        /* both stores read a qz defined 2 issue slots back: 0 stalls */
    addi a6, a6, -1
    bnez a6, .Lms_loop
    /* ABI: put the caller's a2/a3 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    retw.n

/* ---- shift == 0: the C keeps 17 bits and its narrowing store wraps; a 16-bit lane must wrap too ---
 * l[j] = (mid - side) mod 2^16 and r[j] = (mid + side) mod 2^16, i.e. 2*half + b with
 * b = (mid&1) XOR (side&1) for BOTH outputs (the low bit of a sum and of a difference of two
 * integers is the same parity). EE.XORQ (TRM p297) gives b in one instruction; the doubling is
 * EE.VMUL.S16 with a lane of 2 at SAR = 0, a 32-bit product truncated to the low 16 bits (no
 * saturation), and the following EE.VADDS.S16 cannot saturate because 2*half is even, hence <= 32766
 * as a signed lane. The naive alternative -- EE.VSUBS.S16 of the two windows, then a shift -- is the
 * form that disagrees with the C; the .md measures both. */
.Lms_shift0:
    wsr.sar a8                      /* SAR = 15: (a * 2^14) >> 15 == a >> 1 */
    EE.VLD.128.IP q2, a2, 16        /* mid window */
    EE.VLD.128.IP q3, a3, 16        /* side window */
    EE.ANDQ q4, q2, q1              /* Pl = mid & 1 */
    EE.ANDQ q5, q3, q1              /* Pr = side & 1 */
    EE.VMUL.S16 q6, q2, q0          /* A = mid >> 1                                   (qz @ M) */
    EE.ANDQ q7, q4, q5              /* carry = Pl & Pr */
    EE.VMUL.S16 q2, q3, q0          /* B = side >> 1                                  (qz @ M) */
    EE.XORQ q3, q4, q5              /* b = Pl ^ Pr: the low bit of BOTH mid-side and mid+side */
    wsr.sar a10                     /* SAR = 13: (x * 2^14) >> 13 == x << 1 */
    EE.VSUBS.S16 q4, q5, q7         /* borrow = Pr - carry (== (Pl == 0) & Pr) */
    EE.VSUBS.S16 q5, q6, q2         /* A - B      (q6 defined 6 slots back, q2 4 back: 0 stalls) */
    EE.VSUBS.S16 q5, q5, q4         /* half-diff = floor((mid - side)/2) */
    EE.VADDS.S16 q6, q6, q2         /* A + B */
    EE.VADDS.S16 q6, q6, q7         /* half-sum  = floor((mid + side)/2) */
    EE.VMUL.S16 q5, q5, q0          /* 2*half-diff mod 2^16  -- a LEFT shift in 32 bits, truncated */
    EE.VMUL.S16 q6, q6, q0          /* 2*half-sum  mod 2^16  -- the C's 17th bit is what is dropped */
    addi a6, a6, -1                 /* also the slot that puts the two VADDS 2 issues after the 2x */
    EE.VADDS.S16 q5, q5, q3         /* l[j] = 2*half-diff + b   (never saturates: 2*half is even) */
    EE.VADDS.S16 q6, q6, q3         /* r[j] = 2*half-sum  + b */
    EE.VST.128.IP q5, a4, 16
    EE.VST.128.IP q6, a5, 16
    bnez a6, .Lms_shift0
.Lms_done:
    /* ABI: put the caller's a2/a3 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    retw.n
    .size ex18_stereo_ms, .-ex18_stereo_ms

/* ===========================================================================================
 * B) void ex18_comb_filter(int16_t *y, const int16_t *x, const int16_t *hist, uint32_t n,
 *                          uint32_t tapset, uint32_t shift)
 *   a2 = y   a3 = x   a4 = hist   a5 = n   a6 = tapset -> the pointers below   a7 = shift
 *
 * TAP COUNT AND HISTORY LENGTH -- the contract, from the C:
 *   three tap GAINS {g0,g1,g2} from the CELT table (celt.c:246-249), applied to FIVE signal taps
 *   because the two outer pairs are summed before the multiply:
 *       y[i] = x[i] + g0*hist[i] + g1*(hist[i+1] + hist[i-1]) + g2*(hist[i+2] + hist[i-2])
 *   with hist[j] == x[j-T] (the caller passes hist = x - T; T itself never appears, which is why the
 *   kernel needs no period argument). The earliest sample the C touches is hist[-2] (i = 0, the g2
 *   tap) and the latest hist[n-1] (i = n-1, the g0 tap): THREE gain taps over FIVE signal taps, and a
 *   history of T+2 samples is what makes them exist.
 *
 *   MISSING TAPS AT THE HEAD OF THE HISTORY: CELT neither zero-fills nor skips. celt.c:273-276 reads
 *   x[-T1+1] .. x[-T1-2] unconditionally, i.e. T+2 samples back, and the buffers make that legal:
 *   x points into decode_mem/prefilter_mem, which hold COMBFILTER_MAXPERIOD = 1024 samples
 *   (celt.h:236) allocated inside the decoder state, cleared at init, and T is clamped to
 *   >= COMBFILTER_MINPERIOD = 15 (celt.c:265-266). So this kernel also reads its taps
 *   unconditionally and requires a valid, contiguous history -- exactly what CELT does. If a caller
 *   genuinely has no history (the first frame of a stream), the CELT-compatible value there is ZERO:
 *   the state was cleared at init. A "skip the taps that fall off the head" variant is NOT what CELT
 *   does, and the .md measures how far it lands from the C (T5: one or two samples per frame start).
 *
 *   Reads only. Footprint: each window is fetched as the aligned chunk CONTAINING its address plus
 *   the chunk after it, so the kernel reads [floor(hist-2), floor(hist+n+1)+32) bytes: at most 16
 *   bytes before hist[0] and 48 bytes past the last tap window. The model reports the exact range.
 *
 *   tapset 0..2 selects the triple; the constants used are the table's own Q15 values. One honest
 *   exception: the C's own per-call gain multiply MULT16_16_P15(Q15ONE, 26208) yields 26207 (the
 *   +16384 of the rounded multiply tips it over), so tapset 2's first tap here is 1 LSB above a
 *   g = Q15ONE CELT call. Measured, and listed in the .md (T4).
 *   shift == 15 is CELT's Q15 contract (SRCMB's readout). Other values are accepted and make the same
 *   table mean Q(31-shift) taps; the kernel does not verify it -- a wrong shift is a silent gain
 *   error (listed as an assumption).
 *   n is a multiple of 8, n >= 8. y and x are 16-byte aligned; hist may have ANY byte alignment
 *   (see below). y must not overlap hist; y may alias x, because the direct tap x[i..i+7] is loaded
 *   before the store of the same iteration.
 *
 * WHY FIVE WINDOWS COST TEN LOADS. The five taps are five 16-byte windows of one stream, at
 * hist[i-2 .. i+2]. A 128-bit access forms its address as {as[31:4],4{0}} (TRM p49), so an off-grid
 * window has to be built with the ex15 idiom: EE.LD.128.USAR.IP (TRM p93) loads the aligned chunk
 * CONTAINING the pointer and parks the dropped low four bits in SAR_BYTE, and EE.SRC.Q (TRM p125)
 * returns {qs1,qs0} >> (SAR_BYTE*8), i.e. the 16 bytes starting at floor(ptr) + SAR_BYTE. The window
 * at address A therefore needs the pair {floor(A), floor(A)+16} and the SAR that A's own low bits
 * carry -- and BOTH loads set the same SAR_BYTE (adding 16 preserves the low four bits), so one
 * EE.SRC.Q per window does it, at any alignment of `hist`:
 *
 *     EE.LD.128.USAR.IP q2, P_m, 16       ; q2 = floor(A_m), SAR_BYTE = A_m & 15
 *     EE.LD.128.USAR.IP q3, Q_m, 16       ; q3 = floor(A_m) + 16, SAR_BYTE = A_m & 15
 *     EE.SRC.Q q1, q2, q3                 ; q1 = the 16 bytes at A_m  = hist[i+k+m]
 *
 * with A_m = hist + i + m, P_m = hist + m and Q_m = P_m + 16. All ten pointers advance by 16 bytes
 * per iteration with the .IP post-increment, so the loop body carries no pointer arithmetic at all.
 * The last tap window's high load reaches 32 bytes past hist[n+1]; a caller whose history ends at
 * hist[n-1] must be able to read that (CELT's buffers always can: the frame is followed by the MDCT
 * overlap inside the same allocation).
 *
 * A cheaper 4-load variant exists and is NOT used here, because it needs a contract the real caller
 * cannot meet: with ONE shared chunk pair {floor(hist+i-2), +16} and four "poke" loads that only set
 * SAR_BYTE, the whole loop is 3 loads + 4 pokes + 4 SRC.Q + 1 raw window -- but the windows at
 * m = +1, +2 need that pair's SECOND chunk only when (hist+i) & 15 >= 14, and the windows at m = -2,
 * -1 fall into the PREVIOUS chunk when (hist+i) & 15 <= 1; the period T decides that phase and it is
 * arbitrary, so the fast path is 12 of the 16 phases and the other four need a second pair selected
 * by a branch at entry (two more loop bodies). The .md gives both counts. A correct kernel first.
 *
 * Accumulator headroom: |QACC| <= sum |g_k| * |tap| <= 26208*32768 + 8784*65536 + 4248*65536 =
 * 1.71e9 < 2^39 = 5.5e11, so the 40-bit lane accumulators of EE.VSMULAS.S16.QACC cannot saturate on
 * any input. The int16 readout CAN, and does: the C's value reaches x + sum <= 85065, which no
 * 16-bit lane holds, so this kernel clamps where the C keeps 17 bits. Measured in the .md (T3).
 * =========================================================================================== */
    .align 4
    .global ex18_comb_filter
    .type ex18_comb_filter,@function
ex18_comb_filter:
    entry a1, 80
    /* ABI: this kernel writes a10, a11, a12, a13, a14, a15 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    s32i a12, a1, 40
    s32i a13, a1, 44
    s32i a14, a1, 48
    s32i a15, a1, 52
    srli a5, a5, 3                  /* n -> 8-sample chunks */
    beqz a5, .Lcf_done
    /* ---- the tap gains, in-core: a .rodata reference is impossible from .iram1 (the suite's rule is
     * that constants arrive as pointers, and this signature carries only `tapset`). Each value v is
     * synthesised as movi(v>>8), slli 8, addi(v&0xFF): movi's immediate is 12-bit signed, so the
     * assembler would otherwise relax the load into an l32r with a literal-pool entry. a8/a9/a10 are
     * scratch here and become pointers below. */
    beqz a6, .Lcf_g0
    movi a8, 1
    beq a6, a8, .Lcf_g1
    /* tapset 2: 26208, 3280, 0 */
    movi a8, 102
    slli a8, a8, 8
    addi a8, a8, 96                 /* 102*256+96 = 26208 */
    movi a9, 12
    slli a9, a9, 8
    addi a9, a9, 208                /* 12*256+208 = 3280 */
    movi a10, 0
    j .Lcf_build
.Lcf_g0:
    /* tapset 0: 10048, 7112, 4248 */
    movi a8, 39
    slli a8, a8, 8
    addi a8, a8, 64                 /* 39*256+64 = 10048 */
    movi a9, 27
    slli a9, a9, 8
    addi a9, a9, 200                /* 27*256+200 = 7112 */
    movi a10, 16
    slli a10, a10, 8
    addi a10, a10, 152              /* 16*256+152 = 4248 */
    j .Lcf_build
.Lcf_g1:
    /* tapset 1: 15200, 8784, 0 */
    movi a8, 59
    slli a8, a8, 8
    addi a8, a8, 96                 /* 59*256+96 = 15200 */
    movi a9, 34
    slli a9, a9, 8
    addi a9, a9, 80                 /* 34*256+80 = 8784 */
    movi a10, 0
.Lcf_build:
    slli a11, a9, 16
    or a8, a8, a11                  /* (g1 << 16) | g0  -> 16-bit lanes 0,1 */
    EE.ZERO.Q q5
    EE.MOVI.32.Q q5, a8, 0
    slli a11, a10, 16
    or a11, a10, a11                /* (0 << 16) | g2   -> 16-bit lanes 2,3 */
    EE.MOVI.32.Q q5, a11, 1         /* q5 lanes 0,1,2 = g0,g1,g2 (EE.VSMULAS's sel8 = 0,1,2) */
    movi a8, 1
    slli a11, a8, 16
    or a8, a8, a11                  /* 0x00010001: a WORD into EE.MOVI.32.Q, so both halves of it
                                     * carry the same lane value */
    EE.MOVI.32.Q q6, a8, 0
    EE.MOVI.32.Q q6, a8, 1
    EE.MOVI.32.Q q6, a8, 2
    EE.MOVI.32.Q q6, a8, 3          /* q6 = the +1 of the fixed-point bias, in every lane */
    /* ---- ten pointers, each advancing 16 bytes per iteration: P_m = hist+i+m, Q_m = P_m+16 ---- */
    addi a8, a4, -4                 /* P_-2 */
    addi a9, a4, -2                 /* P_-1 */
    addi a10, a4, 2                 /* P_+1 */
    addi a11, a4, 4                 /* P_+2 */
    addi a12, a4, 12                /* Q_-2 */
    addi a13, a4, 14                /* Q_-1 */
    addi a14, a4, 18                /* Q_+1 */
    addi a15, a4, 20                /* Q_+2 */
    addi a6, a4, 16                 /* Q_0 (a6 held `tapset` until the gains were built) */
.Lcf_loop:
    EE.VLD.128.IP q0, a3, 16        /* x[i..i+7]: the direct tap */
    EE.ZERO.QACC
    EE.LD.128.USAR.IP q2, a8, 16    /* m = -2 low: floor(hist+i-2), SAR_BYTE = (hist+i-2) & 15 */
    EE.LD.128.USAR.IP q3, a12, 16   /* m = -2 high */
    EE.SRC.Q q1, q2, q3             /* window hist[i+k-2] */
    EE.VSMULAS.S16.QACC q1, q5, 2   /* QACC += g2 * hist[i+k-2] */
    EE.LD.128.USAR.IP q2, a9, 16    /* m = -1 low */
    EE.LD.128.USAR.IP q3, a13, 16   /* m = -1 high */
    EE.SRC.Q q1, q2, q3             /* window hist[i+k-1] */
    EE.VSMULAS.S16.QACC q1, q5, 1   /* g1 */
    EE.LD.128.USAR.IP q2, a4, 16    /* m = 0 low */
    EE.LD.128.USAR.IP q3, a6, 16    /* m = 0 high */
    EE.SRC.Q q1, q2, q3             /* window hist[i+k] */
    EE.VSMULAS.S16.QACC q1, q5, 0   /* g0 */
    EE.LD.128.USAR.IP q2, a10, 16   /* m = +1 low */
    EE.LD.128.USAR.IP q3, a14, 16   /* m = +1 high */
    EE.SRC.Q q1, q2, q3             /* window hist[i+k+1] */
    EE.VSMULAS.S16.QACC q1, q5, 1   /* g1 */
    EE.LD.128.USAR.IP q2, a11, 16   /* m = +2 low */
    EE.LD.128.USAR.IP q3, a15, 16   /* m = +2 high */
    EE.SRC.Q q1, q2, q3             /* window hist[i+k+2] */
    EE.VSMULAS.S16.QACC q1, q5, 2   /* g2 */
    addi a5, a5, -1                 /* this slot also puts the SRCMB two issues after the last MAC:
                                     * QACC is defined at M (2) by the MAC and read at E (1) by the
                                     * SRCMB, so one slot would cost 1 cycle (Table 1.7-2) */
    EE.SRCMB.S16.QACC q1, a7, 0     /* q1 = sat16(each 40-bit lane >> shift); 15 is CELT's value */
    EE.VADDS.S16 q1, q1, q0         /* + x[i+k]  (16-bit lane: this is where the port clamps) */
    EE.VSUBS.S16 q1, q1, q6         /* - 1       (celt.c:184's bias; the C subtracts it BEFORE its
                                     * clamp, this kernel after SRCMB's own int16 saturation) */
    EE.VST.128.IP q1, a2, 16
    bnez a5, .Lcf_loop
.Lcf_done:
    /* ABI: put the caller's a2/a3/a4/a5/a6/a7 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    l32i a13, a1, 44
    l32i a14, a1, 48
    l32i a15, a1, 52
    retw.n
    .size ex18_comb_filter, .-ex18_comb_filter

/* ===========================================================================================
 * C) void ex18_intensity_stereo(int16_t *x, const int16_t *y, uint32_t n, uint32_t a1, uint32_t a2)
 *   a2 = x (in and out)   a3 = y   a4 = n   a5 = a1 (Q15)   a6 = a2 (Q15)
 *
 * bands.c:398-402, per element, with the C's own truncating multiply:
 *     X[j] = MULT16_32_Q15(a1, X[j]) + MULT16_32_Q15(a2, Y[j])
 *         = floor(a1*X[j]/2^15) + floor(a2*Y[j]/2^15)     (two truncations, then a plain 32-bit add)
 * where a1 = DIV32_16(SHL32(EXTEND32(left),15), norm) and a2 likewise (bands.c:387-397): the
 * per-band gains are computed from the band energies and the kernel receives them as Q15 scalars.
 * Each term fits int16 exactly (|a1| <= 2^15 and |X[j]| <= 32768), but the SUM does not: it needs 17
 * bits, and in this tree celt_norm is opus_val32 (arch.h:155), so the C keeps them. A 16-bit lane
 * cannot: this kernel's EE.VADDS.S16 saturates at +-32767, and the .md reports how often that clamp
 * fires (a1^2 + a2^2 = 2^30 exactly -- norm = sqrt(left^2+right^2) -- so a1 + a2 can reach
 * 2^15*sqrt(2) = 46341, and a real band can put both channels' energy in one sample). The other
 * possible choice, wrapping like an int16 narrowing store, is measured in the model as well.
 *   n multiple of 8, n >= 8; x and y 16-byte aligned (x is written in place).
 * q0 = a1 in every lane, q1 = a2 in every lane, q2/q3 working. SAR = 15 for both multiplies.
 * =========================================================================================== */
    .align 4
    .global ex18_intensity_stereo
    .type ex18_intensity_stereo,@function
ex18_intensity_stereo:
    entry a1, 32
    srli a4, a4, 3
    beqz a4, .Lis_done
    extui a5, a5, 0, 16
    slli a7, a5, 16
    or a5, a5, a7                   /* (a1 << 16) | a1 */
    EE.MOVI.32.Q q0, a5, 0
    EE.MOVI.32.Q q0, a5, 1
    EE.MOVI.32.Q q0, a5, 2
    EE.MOVI.32.Q q0, a5, 3
    extui a6, a6, 0, 16
    slli a7, a6, 16
    or a6, a6, a7                   /* (a2 << 16) | a2 */
    EE.MOVI.32.Q q1, a6, 0
    EE.MOVI.32.Q q1, a6, 1
    EE.MOVI.32.Q q1, a6, 2
    EE.MOVI.32.Q q1, a6, 3
    movi a7, 15
    wsr.sar a7                      /* SAR = 15: the Q15 form of MULT16_32_Q15 */
.Lis_loop:
    EE.VLD.128.IP q2, a2, 0         /* x is READ AND WRITTEN through a2, so the LOAD must not advance
                                     * it: this is the one place in the file where a .IP load steps by
                                     * 0 and the store carries the +16. (piesim.py caught the first
                                     * version, which loaded at x+i and then stored at x+i+8.) */
    EE.VLD.128.IP q3, a3, 16        /* y is read-only: its pointer can walk itself */
    EE.VMUL.S16 q2, q2, q0          /* floor(a1 * X[j] / 2^15)     (qz @ M) */
    EE.VMUL.S16 q3, q3, q1          /* floor(a2 * Y[j] / 2^15)     (qz @ M) */
    EE.VADDS.S16 q2, q2, q3         /* the C adds in 32 bits; a 16-bit lane SATURATES here */
    EE.VST.128.IP q2, a2, 16
    addi a4, a4, -1
    bnez a4, .Lis_loop
.Lis_done:
    retw.n
    .size ex18_intensity_stereo, .-ex18_intensity_stereo
