/* ex24 -- the 32-tap Q14 anti-alias FIR that runs before MP3 downsampling (mp3_decode.c:60-72), in PIE.
 *
 * THE LOOP THIS REPLACES (main/pocket/mp3_decode.c, rate > 24000 only, i.e. 32/44.1/48 kHz):
 *
 *     d->history[d->cursor]=(int16_t)sample;                       // 64
 *     int32_t sum=0;                                               // 65
 *     for(unsigned k=0;k<32;k++)                                   // 66
 *         sum+=(int32_t)d->history[(d->cursor-k)&31]*d->filter[k]; // 67
 *     d->cursor=(d->cursor+1)&31;                                  // 68
 *     sample=sum/16384;                                            // 69   (trunc toward zero)
 *     if(sample>32767) sample=32767; if(sample< -32768) sample= -32768;   // 70-71
 *
 * contract:  out = clamp16( trunc( (sum(k=0..31) x[n-k]*filter[k]) / 16384 ) )
 *
 * THREE THINGS DECIDE HOW THIS HAD TO BE WRITTEN.
 *
 * 1. THE 128-BIT LOAD DROPS THE LOW FOUR ADDRESS BITS. TRM p164 (EE.VLD.128.IP):
 *        qu[127:0] = load128({as[31:4],4{0}});  as = as + {imm[7],imm[7:0],4{0}}
 *    The low four bits of the address are *not* an offset, they are discarded, and there is no
 *    misalignment exception. So a window that starts at an odd 16-bit lane -- which is every
 *    8-lane-misaligned sample of a ring -- cannot be read by four plain VLD.128.IP: the hardware reads
 *    the same aligned 16 bytes for lane offsets 0..7. ex03 measured exactly that failure (42 of 49
 *    outputs wrong, correct only where 2*n happened to be 16-byte aligned).
 *
 * 2. THEREFORE THE WINDOW IS BOUGHT WITH THE UNALIGNED PAIR: EE.LD.128.USAR.IP (TRM p93) is the one
 *    128-bit load that RECORDS the dropped bits:
 *        qu = load128({as[31:4],4{0}});  SAR_BYTE = as[3:0]
 *    and EE.SRC.Q (TRM p125) shifts that byte count out of a 256-bit pair:
 *        qa = {qs1[127:0], qs0[127:0]} >> {SAR_BYTE[3:0] << 3}
 *    With qs0 = the chunk at the LOWER address and qs1 = the next one, the low 128 bits of that shift
 *    are bytes o..o+15 of the pair (o = window_address & 15) -- exactly the 16 unaligned window bytes.
 *    Four SRC.Q over five aligned chunks give the whole 64-byte, arbitrarily aligned 32-tap window with
 *    no staging copy. The operand order is not guesswork: swapped, the window is wrong for every
 *    o != 0, and the host model's 0-mismatch sweep against the scalar loop is what settles it
 *    (ex24_funnel_probe, and ex24_dsfir.md section 7.2).
 *
 * 3. THE WINDOW MUST BE 32 CONTIGUOUS LANES. The device ring is history[32] indexed with `& 31`, so
 *    the window wraps. The design that removes the wrap is a 128-lane ring (256 B) holding two copies
 *    of a 64-sample history, every sample written twice:
 *        ring[j] = x[ n - ((n-j) mod 64) ]   for j = 0..127,   maintained by
 *        ring[n mod 64] = x[n];  ring[64 + (n mod 64)] = x[n];
 *    -- 2 scalar stores, no copy and no shift. The 32 taps for output n are then ring[m .. m+31] with
 *    m = (n-31) mod 64: one contiguous run for every n. The funnel's 5th chunk reaches lane
 *    (m & ~7)+39 <= 95 < 128, so no padding is needed either. THE COEFFICIENTS MUST BE PRE-REVERSED
 *    once by the caller, because ascending memory is ascending time here while k counts backwards:
 *    filter[i] must hold the device filter[31-i] (a one-off 32-lane reverse, not a per-sample cost).
 *    The kernel contract is therefore  out = sum(i=0..31) window32[i]*filter[i].
 *
 * ACCX HEADROOM (the 'does the 40-bit accumulator fit' question): EE.VMULAS.S16.ACCX (TRM p210) sums
 * eight 16x16 products into ACCX[39:0] with a saturating 40-bit add (operation steps 5-7). Four of
 * them are the 32 taps. The worst case any input can reach is
 *        |ACCX|max = 32768 * sum(i=0..31) |filter[i]| = 32768 * L1
 * and for the two tables the device builds (host-measured, ex24_dsfir.md section 1):
 *        44.1 kHz: L1 = 28908  -> 947,257,344    (2^39-1 = 549,755,813,887)
 *        48.0 kHz: L1 = 27044  -> 886,177,792
 * ACCX is 580x the worst case, so nothing can saturate it. It fits int32 as well (2.15e9), which is
 * worth knowing: the 40-bit lanes are not what this kernel needs, they are what the instruction has.
 * The int16 clamp at the exit IS reachable (|sum|max/16384 = 1.76 * 32768) and is spelled here, exactly
 * as the C spells it.
 *
 * FUSED LOADS: EE.VMULAS.S16.ACCX.LD.IP qu, as, imm, qx, qy accumulates qx*qy FIRST and then loads qu
 * from as with as = as + imm (TRM p211, steps 1-9). The window chunks cannot use it (EE.SRC.Q needs
 * them in registers), so the fusion carries the COEFFICIENT chunks: 1 plain load + 3 fused + 1 plain
 * VMULAS covers all four chunks of each.
 *
 * NEGATIVE-SUM ROUNDING (the one place these kernels are deliberately NOT identical to the C):
 * sum/16384 in C truncates TOWARD ZERO; EE.SRS.ACCX au, as, 0 shifts ARITHMETICALLY, i.e. FLOORS. For
 * sum < 0 with a nonzero remainder the two differ by exactly 1 (the PIE result is the lower one).
 * ex24_dsfir.md section 4 enumerates it with a model and gives both ways to close it (change the C to
 * `>> 14`, or spend 4 more instructions in the kernel). These kernels floor, like the hardware.
 *
 * ABI: every kernel here is a `call8` callee, so a10..a15 ARE the caller's a2..a7 -- gcc keeps values
 * live in a2..a7 across a call8 (tools/check_abi.py documents the incident). Each kernel therefore
 * either stays inside a2..a9 or spills a10..a15 to its own frame and reloads them before retw.n.
 */

    .section .iram1,"ax",@progbits
    .align 4

/* ---------------------------------------------------------------------------------------------------
 * int16_t ex24_dsfir32(const int16_t *window32, const int16_t *filter, uint32_t shift)
 *   a2 = window32 : 32 contiguous int16, ascending time: window32[i] = x[n-31+i]. ANY 2-byte
 *                   alignment -- the kernel does the 16-byte alignment itself. READ FOOTPRINT: the
 *                   80 bytes starting at (window32 & ~15) are read; the last 16 only ever contribute
 *                   through the shift, so the caller must keep them READABLE, not correct.
 *   a3 = filter   : 16-byte aligned, 32 int16, the PRE-REVERSED table: filter[i] is the device
 *                   table's filter[31-i]. Built once per rate by the caller, not per sample.
 *   a4 = shift    : the readout shift, 14 for this Q14 table (0..31 accepted).
 * returns        : clamp16( floor( sum(i=0..31) window32[i]*filter[i] ) >> shift )
 */
    .align 4
    .global ex24_dsfir32
    .type ex24_dsfir32,@function
ex24_dsfir32:
    entry a1, 32
    mov a9, a4                              /* shift -> a9 (a8/a9 are the caller's a0/a1: free)      */
    l32r a7, .Lex24_max                     /* 32767 and -32768 materialised here, where the first   */
    movi a6, -32768                         /* load's latency hides them                             */
    EE.LD.128.USAR.IP q4, a2, 0             /* c0 (aligned chunk) and SAR_BYTE = window32 & 15       */
    addi a2, a2, 16
    EE.VLD.128.IP q5, a2, 16                /* c1 (a2 := p+32; the low bits are still the window's)  */
    EE.VLD.128.IP q6, a2, 16                /* c2                                                    */
    EE.VLD.128.IP q7, a2, 16                /* c3                                                    */
    EE.SRC.Q q0, q4, q5                     /* window lanes 0..7   = bytes  0..15 of {c1,c0} >> o    */
    EE.SRC.Q q1, q5, q6                     /* window lanes 8..15                                    */
    EE.SRC.Q q2, q6, q7                     /* window lanes 16..23                                   */
    EE.VLD.128.IP q4, a2, 16                /* c4: 16 bytes past the window end, read for the shift's
                                             * last o bytes; everything else in it is shifted out    */
    EE.SRC.Q q3, q7, q4                     /* window lanes 24..31                                   */
    EE.VLD.128.IP q4, a3, 16                /* filter[0..7]  (a3 := filter+16)                       */
    EE.ZERO.ACCX
    EE.VMULAS.S16.ACCX.LD.IP q5, a3, 16, q0, q4  /* ACCX += w0*f0 ; q5 = filter[8..15]               */
    EE.VMULAS.S16.ACCX.LD.IP q6, a3, 16, q1, q5  /* ACCX += w1*f1 ; q6 = filter[16..23]              */
    EE.VMULAS.S16.ACCX.LD.IP q7, a3, 16, q2, q6  /* ACCX += w2*f2 ; q7 = filter[24..31]              */
    EE.VMULAS.S16.ACCX q3, q7                    /* ACCX += w3*f3 -- all 32 taps in 4 MAC ops        */
    EE.SRS.ACCX a2, a9, 0                   /* a2 = sat32(ACCX >> shift)   [arithmetic: floor]       */
    min a2, a2, a7                          /* the int16 clamp the C spells as two ifs               */
    max a2, a2, a6
    retw.n
    .size ex24_dsfir32, .-ex24_dsfir32

/* ---------------------------------------------------------------------------------------------------
 * void ex24_funnel_probe(const int16_t *window32, int16_t *out32)
 *   a2 = the window (any 2-byte alignment), a3 = 32 int16 of output.
 *   out32[0..7]   = the funnel's answer for window lanes 0..7 (the unaligned window, correct)
 *   out32[8..15]  = what a plain EE.VLD.128.IP of the same pointer gives (the aligned chunk -- the
 *                   ex03 failure, reproduced as a measurement rather than a comment)
 *   out32[16..23] = EE.SRC.Q with the operands crossed ({qs0,qs1} instead of {qs1,qs0}): identical to
 *                   the correct answer when the window is 8-lane aligned (SAR_BYTE = 0) and different
 *                   from it at every other lane offset -- which is what pins the operand order used
 *                   above.
 */
    .align 4
    .global ex24_funnel_probe
    .type ex24_funnel_probe,@function
ex24_funnel_probe:
    entry a1, 32
    mov a6, a3
    EE.VLD.128.IP q1, a2, 0                 /* the same address with a plain load: the low bits are  */
    EE.LD.128.USAR.IP q4, a2, 0             /* dropped. USAR: q4 = the identical aligned chunk, plus */
    addi a2, a2, 16                         /* SAR_BYTE = a2 & 15                                    */
    EE.VLD.128.IP q5, a2, 16                /* c1                                                    */
    addi a7, a6, 16
    EE.SRC.Q q0, q4, q5                     /* the unaligned window                                  */
    EE.VST.128.IP q0, a6, 0
    EE.VST.128.IP q1, a7, 0                 /* group 1: the plain VLD's answer                       */
    EE.SRC.Q q2, q5, q4                     /* the crossed operand order                             */
    addi a8, a6, 32
    EE.VST.128.IP q2, a8, 0
    retw.n
    .size ex24_funnel_probe, .-ex24_funnel_probe

/* ---------------------------------------------------------------------------------------------------
 * int16_t *ex24_dsfir32_run(const int16_t *in, int16_t *out, uint32_t n,
 *                           const int16_t *filter, int16_t *state)
 *   a2 = in      : n mono int16 samples (the caller has already done the (L+R)/2 downmix)
 *   a3 = out     : n int16, one filtered sample each
 *   a4 = n
 *   a5 = filter  : 16-byte aligned, 32 int16, pre-reversed exactly as ex24_dsfir32's
 *   a6 = state   : 16-byte aligned, 136 int16 (272 B):
 *                    state[0] = w, the write index into the 64-sample history (0..63). The caller
 *                               starts it at 0 with the ring zeroed -- equivalent to the device's
 *                               history[32] = {0} except that the ring is 64 deep, so the first 32
 *                               outputs see 32 more zeros of history than the device does (and the
 *                               extra history is zero, so the outputs are identical).
 *                    state[1] = the readout shift (14)
 *                    state[2..7] = reserved, never read or written
 *                    state[8..135] = the 128-lane ring, ring[j] = x[n - ((n-j) mod 64)]
 * returns out + n.
 *
 * Same arithmetic per sample as ex24_dsfir32. What it adds is the ring advance (2 scalar stores, 3
 * address ops); what it removes is the per-sample call and the caller's pointer work. ex24_dsfir.md
 * section 5 counts both spellings; they come out within a few instructions of each other, so this
 * exists mainly because one call over n samples is the unit a cycle measurement can time.
 * The four spilled registers cost 8 instructions per CALL, not per sample.
 */
    .align 4
    .global ex24_dsfir32_run
    .type ex24_dsfir32_run,@function
ex24_dsfir32_run:
    entry a1, 64
    /* ABI: this kernel writes a10, a11, a12 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    s32i a12, a1, 40
    s32i a6, a1, 12                         /* the state pointer (needed once, at the end)          */
    l16ui a12, a6, 2                        /* shift                                                */
    l16ui a7, a6, 0                         /* w                                                    */
    addi a6, a6, 16                         /* &ring[0]: 16 bytes into a 16-byte-aligned state      */
    l32r a8, .Lex24_max
    movi a9, -32768
    beqz a4, .Lex24_run_out
.Lex24_run_loop:
    l16si a10, a2, 0                        /* s = *in                                              */
    addi a2, a2, 2
    addx2 a11, a6, a7                       /* &ring[w]                                             */
    s16i a10, a11, 0                        /* ring[w]    = s                                       */
    s16i a10, a11, 128                      /* ring[w+64] = s   <- the second copy, not a shift     */
    addi a11, a7, 33
    extui a11, a11, 0, 6                    /* m = (w-31) mod 64 = (w+33) & 63                      */
    addx2 a11, a6, a11                      /* the window start                                     */
    EE.LD.128.USAR.IP q4, a11, 0
    addi a11, a11, 16
    EE.VLD.128.IP q5, a11, 16
    EE.VLD.128.IP q6, a11, 16
    EE.VLD.128.IP q7, a11, 16
    EE.SRC.Q q0, q4, q5
    EE.SRC.Q q1, q5, q6
    EE.SRC.Q q2, q6, q7
    EE.VLD.128.IP q4, a11, 16
    EE.SRC.Q q3, q7, q4
    EE.VLD.128.IP q4, a5, 16                /* filter_rev[0..7]                                     */
    EE.ZERO.ACCX
    EE.VMULAS.S16.ACCX.LD.IP q5, a5, 16, q0, q4
    EE.VMULAS.S16.ACCX.LD.IP q6, a5, 16, q1, q5
    EE.VMULAS.S16.ACCX.LD.IP q7, a5, -48, q2, q6  /* the -48 walks the coefficient pointer back to 0:
                                             * the rewind is the load's own post-increment, so no
                                             * separate copy of the filter base has to stay live  */
    EE.VMULAS.S16.ACCX q3, q7
    EE.SRS.ACCX a10, a12, 0
    min a10, a10, a8
    max a10, a10, a9
    s16i a10, a3, 0                         /* *out                                                 */
    addi a3, a3, 2
    addi a7, a7, 1
    extui a7, a7, 0, 6                      /* w = (w+1) & 63                                       */
    addi a4, a4, -1
    bnez a4, .Lex24_run_loop
.Lex24_run_out:
    l32i a10, a1, 12
    s16i a7, a10, 0                         /* the write index survives the call                    */
    mov a2, a3                              /* return out + n                                       */
    l32i a12, a1, 8
    l32i a11, a1, 4
    l32i a10, a1, 0
    /* ABI: put the caller's a2/a3/a4 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    retw.n
    .size ex24_dsfir32_run, .-ex24_dsfir32_run

/* ---------------------------------------------------------------------------------------------------
 * int16_t ex24_dsfir32_sym(const int16_t *history32, const int16_t *filter_half,
 *                          int16_t mid_coef, uint32_t shift)
 *   a2 = history32 : 16-byte aligned, 64 int16: [0..31] = the window, ascending time as in
 *                    ex24_dsfir32; [32..63] = the MIRROR window, lane i = window[31-i].
 *   a3 = filter_half : 16-byte aligned, 16 int16 = the FOLDED coefficients
 *                    [filter[0], ..., filter[14], filter[16]]   (15 unfolded pairs + the true centre)
 *   a4 = mid_coef  : the centre-tap CORRECTION filter_init added at filter[15], i.e.
 *                    filter[15] - filter[16]  (measured: +2 at 44.1 kHz, 0 at 48 kHz -- section 1).
 *                    Pass 0 where the table is exactly symmetric; the kernel pays for it either way.
 *   a5 = shift     : the readout shift.
 *
 * WHY THIS IS NOT A SAVING (the answer to "can the 32 taps be folded in half"):
 *   The table IS symmetric: the raw table has 0 asymmetric indices at 32/44.1/48 kHz, and the only
 *   broken pair is the centre pair (15,16), broken by filter_init's one-scalar correction (line 44).
 *   So the exact identity is
 *      sum_i w[i]*c[i] = sum_{k=0..14}(w[k]+w[31-k])*filter[k] + (w[15]+w[16])*filter[16] + w[16]*corr
 *   and forming w[k]+w[31-k] is a MIRROR PERMUTATION over 32 lanes. The 220-instruction PIE set has no
 *   lane mirror: the only lane-permuting instructions are EE.VZIP.16 (p293) and EE.VUNZIP.16 (p290),
 *   both of them the even/odd split, and EE.BITREV (p77) is a per-word bit reverse for FFT addressing.
 *   So the mirror has to be BOUGHT, with a second ring written in the opposite index direction
 *   (ring_B[(C-n) mod 64] = x[n], which reads forward as descending time) -- 2 more scalar stores per
 *   sample and 256 more bytes -- and that ring is what this kernel consumes as lanes 32..63.
 *   Even then the fold does not pay: it turns 4 MAC ops into 2 + 1 correction and pays 5 extra loads
 *   for the second funnel (16 lanes of window B need two SRC.Q over three raw chunks, and its
 *   alignment phase is its own, so its own USAR is needed), and the pair sum w[k]+w[31-k] does not fit
 *   int16 (EE.VADDS.S16 saturates). Measured in ex24_dsfir.md section 6: 35 instructions against
 *   ex24_dsfir32's 24, exact only while no pair overflows -- which 68-72% of full-scale audio does.
 */
    .align 4
    .global ex24_dsfir32_sym
    .type ex24_dsfir32_sym,@function
ex24_dsfir32_sym:
    entry a1, 32
    mov a9, a5                              /* shift -> a9 (a5, the shift argument, is then free)    */
    mov a5, a3                              /* the filter_half pointer -> a5, the coefficient walker
                                             * (a3, the filter_half argument, is free for the mirror) */
    addi a6, a2, 64                         /* the mirror window's base                             */
    mov a3, a6                              /* ... and its walker                                   */
    l32r a7, .Lex24_max
    movi a8, -32768
    /* ---- 16 lanes of the window and 16 lanes of its mirror: the fold is k = 0..15 */
    EE.LD.128.USAR.IP q4, a2, 0             /* SAR_BYTE = the window's own phase                    */
    addi a2, a2, 16
    EE.VLD.128.IP q5, a2, 16
    EE.VLD.128.IP q6, a2, 16
    EE.SRC.Q q0, q4, q5
    EE.SRC.Q q1, q5, q6
    EE.LD.128.USAR.IP q4, a3, 0             /* the mirror's phase is independent: its own USAR       */
    addi a3, a3, 16
    EE.VLD.128.IP q5, a3, 16
    EE.VLD.128.IP q6, a3, 16
    EE.SRC.Q q2, q4, q5
    EE.SRC.Q q3, q5, q6
    EE.VADDS.S16 q0, q0, q2                 /* P_0..P_7  = w[0..7]  + w[31..24]  -- SATURATING: see
                                             * the header. |P| can reach 65536 and int16 cannot hold
                                             * it, so this kernel is only exact while no pair overflows. */
    EE.VADDS.S16 q1, q1, q3                 /* P_8..P_15 = w[8..15] + w[23..16]                      */
    /* ---- the centre-pair correction w[16]*mid_coef, inside the accumulator (adding it after the
     *      shift would not be the same number). w[16] is the mirror window's lane 15. */
    addi a6, a6, 30                         /* &mirror[15] = the mirror base + 30 bytes             */
    EE.VLDBC.16 q7, a6                      /* w[16] broadcast into all eight lanes                 */
    extui a4, a4, 0, 16                     /* the argument's low 16 bits: the windowed ABI does not
                                             * promise the upper bits of a narrow argument          */
    EE.ZERO.Q q6
    EE.MOVI.32.Q q6, a4, 0                  /* {corr,0,0,0,0,0,0}: one lane of product              */
    EE.VLD.128.IP q2, a5, 16                /* the folded coefficients, 2 chunks (a5 := +16)        */
    EE.ZERO.ACCX
    EE.VMULAS.S16.ACCX.LD.IP q3, a5, 16, q0, q2  /* ACCX += P_0..7  * filter[0..7] ; q3 = next     */
    EE.VMULAS.S16.ACCX q1, q3                     /* ACCX += P_8..15 * filter[8..14], centre        */
    EE.VMULAS.S16.ACCX q7, q6                     /* ACCX += w[16] * corr                           */
    EE.SRS.ACCX a2, a9, 0
    min a2, a2, a7
    max a2, a2, a8
    retw.n
    .size ex24_dsfir32_sym, .-ex24_dsfir32_sym

    .section .iram1.literal
    .align 4
.Lex24_max:
    .word 32767
