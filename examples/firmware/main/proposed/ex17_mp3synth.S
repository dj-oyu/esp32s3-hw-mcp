/* ex17 -- MP3 (minimp3) synthesis, three kernels in Q15 int16 lanes.
 *
 * The decoder this is written against: lieff/minimp3 (MINIMP3_ONLY_MP3, MINIMP3_NO_SIMD, internal
 * arithmetic in float, int16 only at the exit). It is NOT the ISO reference decoder and it is NOT a
 * float-for-float match: every kernel here is a Q15 INTEGER restatement of the same DSP stage, so the
 * agreement with minimp3's float path is an error budget, not bit equality. The .md next to this file
 * measures that budget per kernel and says where the bits go.
 *
 * The three stages, and where they are in minimp3.h (line numbers of
 * /workspace/cardputer-adv-pocketjs/.cache/codecs/minimp3/minimp3.h, 1865 lines):
 *
 *   A) ex17_imdct18         the 18-point IMDCT of one long-block subband: the butterfly + 9-point DCT
 *                           + twiddle rotation that minimp3 writes as a scalar fold in L3_imdct36
 *                           (1087-1142), whose twiddle table is g_twid9 (1090-1092) and whose state is
 *                           mdct_overlap[ch] -- 9 floats per band (line 20).
 *   B) ex17_window_overlap  the window + overlap-add + mirror fold + frequency inversion: L3_imdct36's
 *                           four lines 1136-1139 (window and overlap) plus L3_change_sign (1186-1192,
 *                           "frequency inversion": the odd samples of every second subband negated).
 *   C) ex17_polyphase       the synthesis filterbank's 16-tap recombination: mp3d_synth's inner loop
 *                           (1598-1625, the S0/S1/S2 macros) over the deinterleaved state `lins`
 *                           (1637, 1641) with the window table g_win (1482-1498, 15 phases x 16
 *                           coefficients).
 *
 * WHY THIS IS NOT A PORT. Three things in minimp3 have no PIE equivalent, and they are the reason A and
 * B are restructured rather than transcribed:
 *
 *   1. minimp3's IMDCT fold needs a LANE REVERSAL: si[8-2*i] and si[7-2*i] (1101, 1103) write the odd
 *      half of the butterfly in reverse index order, and its own SIMD path needs a reverse instruction
 *      to publish the mirrored half (VREV, 1130). The 220-instruction PIE set has no lane reverse --
 *      VZIP/VUNZIP only split and merge even/odd lanes (p290/p293) and EE.BITREV (p77) is a per-word
 *      bit reverse -- so kernel A computes the composite transform as a MATRIX (taps in the reduction,
 *      outputs in the lanes) and kernel B publishes the mirrored half in its own output plane instead
 *      of reversing it.
 *   2. There is no float and no 16-bit lane shift instruction. Every scale is a SAR-shifted multiply
 *      (EE.VMUL.S16, p198) or an explicit >> at the readout (EE.SRCMB.S16.QACC, p130).
 *   3. There is no negate instruction: the frequency inversion is a multiply by a +-1 pattern. That is
 *      exact for every int16 including -32768 (two's complement), where EE.VSUBS.S16(0, x) -- the
 *      "obvious" negation -- saturates to 32767 instead (p281). B uses the multiply and the .md counts
 *      the lanes where the two spellings would differ.
 *
 * ACCX vs QACC (the question this file is written to answer):
 *
 *   A and C both reduce a dot product over taps, but the parallelism is in a different place, and the
 *   accumulator follows the parallelism:
 *
 *   - A has EIGHTEEN INDEPENDENT OUTPUTS per band and the reduction runs over taps that are already in
 *     a register. Kernel A therefore puts the OUTPUTS in the lanes (one 40-bit accumulator per output:
 *     EE.VSMULAS.S16.QACC, p269) and lets the broadcast operand carry the tap: the broadcast lane sel8
 *     selects which x[k] multiplies the whole coefficient column. 18 outputs x 18 taps = 324 MACs in
 *     54 MAC instructions (3 groups of 8 lanes; output lanes 18..23 carry zero coefficients).
 *   - C has SIXTEEN TAPS per output and eight outputs per register, and the deinterleaved state is what
 *     makes that possible: the taps of one output are 16 bytes apart and the eight lanes of a load are
 *     eight DIFFERENT outputs (exactly why the state is deinterleaved -- minimp3's zlin[4*i - 64*k],
 *     line 1600). Kernel C therefore uses the same accumulator (outputs in the lanes) and puts the tap
 *     in the broadcast operand, which is what makes 16 MACs cost one instruction.
 *   - ACCX (EE.VMULAS.S16.ACCX + EE.SRS.ACCX, p210/p134) is the right accumulator only when the answer
 *     is a SINGLE scalar -- ex10's block SAD is the canonical case (eight lanes summed into one
 *     number). For A it would collapse the eight lanes into one output per pass: 18 outputs x (2 MACs
 *     + 1 SRS.ACCX) = 54 instructions AND 18 separate readouts instead of 3, and the 16-bit readout of
 *     SRCMB would be replaced by SRS.ACCX's 32-bit clamp (which also writes ACCX back, so it needs a
 *     ZERO.ACCX per output). The .md counts both spellings side by side; the MAC count is the same and
 *     the readout is 6x the instructions.
 *
 * SCHEDULING. A 128-bit load defines its register at pipeline stage 2 (M) and a MAC uses its operands
 * at stage 1 (E), so a load needs two issue slots before its consumer (TRM 1.7.1, D = max(SA - SB + 1,
 * 0); Table 1.7-2). Every rolling sequence below therefore keeps TWO registers in flight: the register
 * a load writes is the one the MAC two issues later reads, and the register a MAC just consumed is the
 * one the load refills. With that pipeline the modelled stall count is zero and each tap costs exactly
 * one instruction (the load is fused into it).
 *
 * PROPOSED. This file is not in examples/firmware/main/CMakeLists.txt, is not declared in examples.h and
 * is not called by main.c. No hardware was used: everything quoted in ex17_mp3synth.md comes from the
 * assembler, objdump, a Python model and a host C verifier.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex17_imdct18(const int16_t *x, const int16_t *tw, int32_t *acc, uint32_t shift)
 *
 *   a2 = x   : 24 int16, 16-BYTE ALIGNED. x[0..17] is one subband's requantized spectrum (Q15);
 *              x[18..23] is READ and never used (the coefficient columns of output lanes 18..23 are
 *              zero, so those products are zero whatever the pad lanes hold). 48 bytes are read.
 *   a3 = tw  : the Q15 IMDCT matrix, grouped for the walk: 56 columns x 8 lanes = 448 int16
 *              (896 bytes, 16-BYTE ALIGNED). Column (g,k) is the eight lanes of output indices
 *              8g..8g+7 for tap k:       tw[144*g + 8*k + j]  ==  Cq[8*g + j][k]
 *              with Cq[j][k] = 0 for j >= 18. The matrix is the composite of minimp3's fold (the
 *              butterflies at 1097-1104), its two 9-point DCTs (L3_dct3_9, 1047-1085) and its twiddle
 *              rotation (1136-1137), i.e. the linear map x -> (overlap half, sum half); the .md derives
 *              it with unit probes through a double-precision copy of those lines, then quantizes to
 *              Q15. Columns 54 and 55 (bytes 864..895) are LOADED by the last two rolling instructions
 *              and never used, so the buffer must be 56 columns wide and their contents do not matter.
 *   a4 = acc : 24 int32, 16-BYTE ALIGNED. acc[j] = (sum over k of x[k]*Cq[j][k]) >> shift, i.e. the
 *              exact shifted 40-bit lane of QACC; acc[18..23] are the pad lanes and come out 0 (their
 *              coefficient columns are zero). 96 bytes are written.
 *   a5 = shift: the readout shift, as[5:0] of EE.SRCMB.S16.QACC (0..63). 15 makes the output Q15.
 *
 * The readout, and where the bits go. EE.SRCMB.S16.QACC shifts all eight 40-bit lanes by as[5:0],
 * writes the shifted values back into QACC and puts a 16-bit SATURATING copy into qu. This kernel
 * stores the QACC side (EE.ST.QACC_L/H.L.128.IP, p139/p137, four int32 each), so acc is the exact
 * shifted 40-bit value and nothing is clamped; the 16-bit copy in q1 is deliberately dropped, and the
 * .md counts the lanes where that copy WOULD have clamped (|shifted| >= 2^15). The 40-bit accumulator
 * cannot overflow here: 18 taps x 32767 x 32767 < 2^35 < 2^39, so no lane saturates for any input.
 */
    .align 4
    .global ex17_imdct18
    .type ex17_imdct18,@function
ex17_imdct18:
    entry a1, 32
    mov a7, a5                      /* SRCMB's shift comes from an address register (TRM p130) */
    EE.VLD.128.IP q5, a2, 16        /* q5 = x[0..7]   -- taps 0..7   (sel8 = 0..7) */
    EE.VLD.128.IP q6, a2, 16        /* q6 = x[8..15]  -- taps 8..15  (sel8 = 0..7) */
    EE.VLD.128.IP q7, a2, 16        /* q7 = x[16..23] -- taps 16,17  (sel8 = 0,1; 18..23 are pad) */
    EE.VLD.128.IP q0, a3, 16        /* prologue: coefficient column (g0,k0) -- two registers in flight, */
    EE.ZERO.QACC                    /* so every load is two slots ahead of the MAC that reads it */
    EE.VLD.128.IP q4, a3, 16        /* prologue: column (g0,k1) */
/* ---- group 0: output lanes 0..7 (taps 0..17, coefficient columns 0..17) ---------------------------- */
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 0   /* lane j += Cq[j][0]*x[0];  q0 <- column (g0,k2) */
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 1   /* lane j += Cq[j][1]*x[1];  q4 <- column (g0,k3) */
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 2
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 3
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 4
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 5
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 6
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 7
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 0   /* taps 8..15 read x[8..15] */
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 1
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 2
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 3
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 4
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 5
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 6
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 7
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q7, 0   /* taps 16,17 read x[16], x[17] */
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q7, 1   /* ... and q4 already holds group 1's column 0 */
    EE.SRCMB.S16.QACC q1, a7, 0     /* QACC = QACC >> shift, all eight lanes; q1 = the 16-bit copy we drop */
    EE.ST.QACC_L.L.128.IP a4, 16    /* acc[0..3] = lanes 0..3 (exact int32) */
    EE.ST.QACC_H.L.128.IP a4, 16    /* acc[4..7] = lanes 4..7 */
/* ---- group 1: output lanes 8..15 ------------------------------------------------------------------ */
    EE.ZERO.QACC
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 0
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 1
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 2
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 3
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 4
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 5
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 6
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 7
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 0
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 1
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 2
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 3
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 4
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 5
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 6
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 7
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q7, 0
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q7, 1
    EE.SRCMB.S16.QACC q1, a7, 0
    EE.ST.QACC_L.L.128.IP a4, 16
    EE.ST.QACC_H.L.128.IP a4, 16
/* ---- group 2: lanes 16..23 (16,17 real; 18..23 are pad lanes whose coefficients are zero) ---------- */
    EE.ZERO.QACC
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 0
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 1
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 2
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 3
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 4
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 5
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q5, 6
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q5, 7
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 0
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 1
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 2
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 3
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 4
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 5
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q6, 6
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q6, 7
    EE.VSMULAS.S16.QACC.LD.INCP q0, a3, q0, q7, 0
    EE.VSMULAS.S16.QACC.LD.INCP q4, a3, q4, q7, 1   /* the two pad columns, loaded and never used */
    EE.SRCMB.S16.QACC q1, a7, 0
    EE.ST.QACC_L.L.128.IP a4, 16
    EE.ST.QACC_H.L.128.IP a4, 16
    retw.n
    .size ex17_imdct18, .-ex17_imdct18

/* void ex17_window_overlap(const int16_t *blocks, const int16_t *win, int16_t *out, uint32_t n)
 *
 *   a2 = blocks: n records of 32 int16 (64 bytes each, 16-BYTE ALIGNED base). Record b:
 *                  [0..15]  plane 0 = ovl[0..8]  -- the STATE: the previous frame's overlap half
 *                                                     (minimp3's mdct_overlap, line 20: 9 per band)
 *                  [16..31] plane 1 = sum[0..8]  -- this frame's rotation half (the `sum` of line 1136)
 *                Lanes 9..15 of both planes are LOADED and not used: they must be readable, their
 *                contents do not matter (the window's pad lanes are zero, so their products are zero).
 *   a3 = win  : 2 rows of 40 int16 (80 bytes per row, 16-BYTE ALIGNED), row 0 for even subbands and
 *               row 1 for odd ones:
 *                  [0..7]   A1 = W[0..7]        the window, first half
 *                  [8..15]  A2 = W[9..16]       the window, second half
 *                  [16..23] Pfirst:  the multiply pattern applied to the "present" half
 *                  [24..31] Pmirror: the pattern applied to the mirrored half
 *                  [32] W[8]   [33] W[17]       the two window values the scalar tail needs
 *                  [34..39] pad
 *               Row 0 (even subband): Pfirst = Pmirror = eight lanes of 1.
 *               Row 1 (odd subband):  Pfirst  = (+1,-1,+1,-1,+1,-1,+1,-1)
 *                                     Pmirror = (-1,+1,-1,+1,-1,+1,-1,+1)
 *               Both rows carry the SAME A1/A2: the frequency inversion lives in the patterns, so it
 *               costs one multiply per register and no extra table. The .md also counts the alternative
 *               spelling (signs baked into four window vectors) and the lanes where the two differ.
 *   a4 = out  : n records of 32 int16 (64 bytes each, 16-BYTE ALIGNED base):
 *                  plane 0 = out[0..8]  = the present half, i = 0..8  (lane 8 from the scalar tail)
 *                  plane 1 = out[16+i]  = the MIRROR half: lane i holds the sample at natural index
 *                                         17-i, i = 0..8. Lanes 9..15 of both planes are NOT written.
 *               The mirror half gets its own plane because the fold reverses it and the PIE set has no
 *               lane reverse (minimp3's SIMD path needs VREV for exactly this, line 1130). Natural
 *               order for a caller that wants it: natural[n] = plane0[n] (n = 0..8) and
 *               natural[n] = plane1[17-n] (n = 9..17).
 *   a5 = n    : number of SUBBANDS (blocks). Must be EVEN, and the first subband of the range must have
 *               an EVEN index: the two window rows are consumed in pairs (2t, 2t+1) and L3_change_sign
 *               (1186-1192) negates the odd samples of ODD subbands. n = 0 touches nothing.
 *
 * The arithmetic, per subband, for i = 0..8 (all Q15):
 *
 *   present[i] = sat16( ((ovl[i]*W[i]) >> 15) - ((sum[i]*W[9+i]) >> 15) )   * Pfirst
 *   mirror[i]  = sat16( ((ovl[i]*W[9+i]) >> 15) + ((sum[i]*W[i]) >> 15) )   * Pmirror
 *
 * which is minimp3's lines 1138-1139 with the two products shifted one at a time (the float code adds
 * first and rounds once), and with the two operators SATURATING (EE.VSUBS.S16 / EE.VADDS.S16 are the
 * only vector add and subtract in the set). The .md states where that is exact and counts the lanes
 * outside it: a present half above +32767 freezes at 32767 instead of wrapping.
 *
 * i = 8 is a SCALAR TAIL (two of the eighteen outputs), the same convention ex12 uses for its
 * remainder: the vector path holds eight lanes, the ninth pair is two multiplies either way. The tail
 * reproduces the saturating semantics with scalar mull/srai and a min+max clamp, and the odd subband's
 * negation there is `neg` (exact, including -32768) rather than a clamp.
 *
 * The pair-unrolled loop body is 223 bytes (objdump: the bnez at 0x215 jumps back to 0x136), inside
 * loopgtz's 256-byte limit; the kernel uses addi/bnez anyway, which is what the suite's measured
 * comparison could not tell apart (docs/pie-simd.md section 7). The byte count is quoted so the choice
 * can be revisited rather than assumed.
 */
    .align 4
    .global ex17_window_overlap
    .type ex17_window_overlap,@function
ex17_window_overlap:
    entry a1, 80
    /* ABI: this kernel writes a10, a11, a12, a13, a14 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    s32i a12, a1, 40
    s32i a13, a1, 44
    s32i a14, a1, 48
    beqz a5, .Lwo_done
    mov a6, a3                      /* a6 = win+0   (row 0, even subband) */
    addi a7, a3, 80                 /* a7 = win+80  (row 1, odd subband) */
    movi a8, 15
    wsr.sar a8                      /* SAR = 15: every EE.VMUL.S16 below is a Q15 >> 15 */
    movi a3, 1
    slli a3, a3, 15                 /* a3 = 32768. Built in-core: `movi a3, 32768` does not fit the
                                     * 12-bit immediate and the assembler turns it into an l32r from
                                     * .literal, which .iram1 cannot reach (ex15_fbstore.S hit the same
                                     * wall with its colour broadcast) */
    movi a14, -1
    add a14, a3, a14                /* a14 = 32767: the tail's high clamp */
    neg a3, a3                      /* a3 = -32768: the tail's low clamp. a3/a14 are loop-invariant */
    srli a5, a5, 1                  /* pairs of subbands: (even, odd) */
    beqz a5, .Lwo_done
.Lwo_loop:
/* ---------------- even subband: window row 0 (patterns = ones, no inversion) ------------------------ */
    mov a10, a2                     /* a10 = this record's base (a2 walks past it) */
    l16si a9, a10, 16               /* ovl[8]  (plane 0, lane 8) */
    l16si a11, a10, 48              /* sum[8]  (plane 1, lane 8) */
    l16si a8, a6, 64                /* W[8]  */
    l16si a10, a6, 66               /* W[17] (a10 is free: ovl[8]/sum[8] are already in a9/a11) */
    EE.VLD.128.IP q0, a2, 32        /* q0 = ovl[0..7] (16 bytes), skip plane 0's pad lanes */
    EE.VLD.128.IP q1, a2, 32        /* q1 = sum[0..7] */
    EE.VLD.128.IP q2, a6, 16        /* q2 = A1 */
    EE.VLD.128.IP q3, a6, 16        /* q3 = A2 */
    EE.VLD.128.IP q4, a6, 16        /* q4 = Pfirst  (ones) */
    EE.VLD.128.IP q5, a6, 16        /* q5 = Pmirror (ones) */
    mull a12, a9, a8                /* ovl[8]*W[8]  */
    mull a13, a11, a10              /* sum[8]*W[17] */
    EE.VMUL.S16 q6, q0, q2          /* ovl*W[i]   */
    EE.VMUL.S16 q7, q1, q3          /* sum*W[9+i] */
    srai a12, a12, 15
    srai a13, a13, 15
    EE.VMUL.S16 q2, q1, q2          /* sum*W[i]   (A1 is spent) */
    EE.VMUL.S16 q3, q0, q3          /* ovl*W[9+i] (A2 is spent) */
    sub a12, a12, a13               /* present[8]: natural index 8, even -> never inverted */
    mull a13, a9, a10               /* ovl[8]*W[17] */
    mull a8, a11, a8                /* sum[8]*W[8]  (a8 is spent) */
    EE.VSUBS.S16 q6, q6, q7         /* present half */
    EE.VADDS.S16 q7, q3, q2         /* mirror half */
    min a12, a12, a14
    max a12, a12, a3
    srai a13, a13, 15
    srai a8, a8, 15
    EE.VMUL.S16 q6, q6, q4          /* frequency inversion: exact multiply by +-1 */
    EE.VMUL.S16 q7, q7, q5
    add a13, a13, a8                /* mirror[8]: natural index 9 */
    min a13, a13, a14
    max a13, a13, a3
    s16i a12, a4, 16                /* out plane 0, lane 8 */
    s16i a13, a4, 48                /* out plane 1, lane 8 = natural index 9 */
    EE.VST.128.IP q6, a4, 32        /* out plane 0, lanes 0..7 (imm 32: skip plane 0's pad lanes,
                                     * exactly like the two loads above) */
    EE.VST.128.IP q7, a4, 32        /* out plane 1, lanes 0..7 */
/* ---------------- odd subband: window row 1, patterns carry the inversion --------------------------- */
    mov a10, a2
    l16si a9, a10, 16
    l16si a11, a10, 48
    l16si a8, a7, 64
    l16si a10, a7, 66
    EE.VLD.128.IP q0, a2, 32
    EE.VLD.128.IP q1, a2, 32
    EE.VLD.128.IP q2, a7, 16
    EE.VLD.128.IP q3, a7, 16
    EE.VLD.128.IP q4, a7, 16        /* Pfirst  = (+1,-1,...)  */
    EE.VLD.128.IP q5, a7, 16        /* Pmirror = (-1,+1,...)  */
    mull a12, a9, a8
    mull a13, a11, a10
    EE.VMUL.S16 q6, q0, q2
    EE.VMUL.S16 q7, q1, q3
    srai a12, a12, 15
    srai a13, a13, 15
    EE.VMUL.S16 q2, q1, q2
    EE.VMUL.S16 q3, q0, q3
    sub a12, a12, a13
    mull a13, a9, a10
    mull a8, a11, a8
    EE.VSUBS.S16 q6, q6, q7
    EE.VADDS.S16 q7, q3, q2
    min a12, a12, a14               /* present[8]: natural 8, even -> not inverted */
    max a12, a12, a3
    srai a13, a13, 15
    srai a8, a8, 15
    EE.VMUL.S16 q6, q6, q4
    EE.VMUL.S16 q7, q7, q5
    add a13, a13, a8
    min a13, a13, a14
    max a13, a13, a3
    neg a13, a13                    /* mirror[8]: natural index 9 is ODD -> two's complement negate,
                                     * so -32768 stays -32768 (VSUBS.S16(0,x) would clamp it to 32767) */
    s16i a12, a4, 16
    s16i a13, a4, 48
    EE.VST.128.IP q6, a4, 32
    EE.VST.128.IP q7, a4, 32
    addi a5, a5, -1
    bnez a5, .Lwo_loop
.Lwo_done:
    /* ABI: put the caller's a2/a3/a4/a5/a6 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    l32i a13, a1, 44
    l32i a14, a1, 48
    retw.n
    .size ex17_window_overlap, .-ex17_window_overlap

/* void ex17_polyphase(const int16_t *v, const int16_t *win, int32_t *out, uint32_t n)
 *
 *   a2 = v   : the deinterleaved polyphase state, group-major: (n/8) groups x 16 taps x 8 lanes =
 *              n*16 int16 (16-BYTE ALIGNED). v[128*g + 8*t + j] is tap t of output sample (8g+j), i.e.
 *              V_t[s]. The 16 taps of one group are 16 bytes apart, which is what lets the rolling load
 *              form (fixed +16 increment, p270) fetch tap t+1 with the MAC of tap t. This layout is the
 *              transposed twin of minimp3's tap-major lins (zlin[4*i - 64*k], line 1600; the state is
 *              15*64 floats, 1637/1653): the same 16 taps, with the group of eight samples as the
 *              outer index.
 *              READ FOOTPRINT: the last group's rolling pipeline reads 32 bytes PAST the end of v (two
 *              loads whose values are never used). The caller must keep 32 readable bytes there.
 *   a3 = win : 16 int16 (32 bytes, 16-BYTE ALIGNED): win[t] is the Q15 coefficient of tap t. minimp3's
 *              table is per phase (g_win, 15 rows x 16, lines 1482-1498); which phase a call needs is
 *              the caller's choice of this vector, which is why the kernel takes a pointer, not an
 *              index, and why its own cost does not depend on the phase.
 *   a4 = out : n int32 (4n bytes, 16-BYTE ALIGNED). out[s] = (sum over t = 0..15 of V_t[s]*win[t]) >> 15,
 *              the exact shifted 40-bit accumulator (four int32 per store, p139/p137).
 *   a5 = n   : number of output samples; must be a multiple of 8.
 *
 * Cost: 16 MAC instructions per group of eight samples -- each instruction is eight lane-MACs, so 128
 * MACs per group and 16 MACs per output sample -- plus one shift readout and two stores: 21
 * instructions per group, no separate load instruction and no modelled stall, because the two rolling
 * registers keep two issue slots between every load and the MAC that reads it. The .md tabulates that
 * against the scalar C loop and against the two alternative spellings (ACCX with one output at a time;
 * the same loop with the loads as separate instructions).
 */
    .align 4
    .global ex17_polyphase
    .type ex17_polyphase,@function
ex17_polyphase:
    entry a1, 32
    movi a6, 15                     /* the Q15 readout shift, as[5:0] for EE.SRCMB.S16.QACC */
    srli a5, a5, 3                  /* eight samples per group */
    beqz a5, .Lpp_done
    EE.VLD.128.IP q4, a3, 16        /* q4 = win[0..7]  */
    EE.VLD.128.IP q5, a3, 16        /* q5 = win[8..15] */
    EE.VLD.128.IP q0, a2, 16        /* prologue: tap 0 of group 0 into the first rolling register */
    EE.VLD.128.IP q1, a2, 16        /* prologue: tap 1 into the second */
.Lpp_loop:
    EE.ZERO.QACC
    EE.VSMULAS.S16.QACC.LD.INCP q0, a2, q0, q4, 0   /* lane j += V_0[8g+j]*win[0];  q0 <- tap 2 */
    EE.VSMULAS.S16.QACC.LD.INCP q1, a2, q1, q4, 1   /* lane j += V_1[8g+j]*win[1];  q1 <- tap 3 */
    EE.VSMULAS.S16.QACC.LD.INCP q0, a2, q0, q4, 2
    EE.VSMULAS.S16.QACC.LD.INCP q1, a2, q1, q4, 3
    EE.VSMULAS.S16.QACC.LD.INCP q0, a2, q0, q4, 4
    EE.VSMULAS.S16.QACC.LD.INCP q1, a2, q1, q4, 5
    EE.VSMULAS.S16.QACC.LD.INCP q0, a2, q0, q4, 6
    EE.VSMULAS.S16.QACC.LD.INCP q1, a2, q1, q4, 7
    EE.VSMULAS.S16.QACC.LD.INCP q0, a2, q0, q5, 0   /* taps 8..15: the second coefficient register */
    EE.VSMULAS.S16.QACC.LD.INCP q1, a2, q1, q5, 1
    EE.VSMULAS.S16.QACC.LD.INCP q0, a2, q0, q5, 2
    EE.VSMULAS.S16.QACC.LD.INCP q1, a2, q1, q5, 3
    EE.VSMULAS.S16.QACC.LD.INCP q0, a2, q0, q5, 4
    EE.VSMULAS.S16.QACC.LD.INCP q1, a2, q1, q5, 5
    EE.VSMULAS.S16.QACC.LD.INCP q0, a2, q0, q5, 6
    EE.VSMULAS.S16.QACC.LD.INCP q1, a2, q1, q5, 7   /* ... and the next group's taps 0,1 are loaded */
    EE.SRCMB.S16.QACC q6, a6, 0     /* >> 15, all eight lanes; the 16-bit copy is dropped (see A) */
    EE.ST.QACC_L.L.128.IP a4, 16    /* out[8g .. 8g+3] */
    EE.ST.QACC_H.L.128.IP a4, 16    /* out[8g+4 .. 8g+7] */
    addi a5, a5, -1
    bnez a5, .Lpp_loop
.Lpp_done:
    retw.n
    .size ex17_polyphase, .-ex17_polyphase
