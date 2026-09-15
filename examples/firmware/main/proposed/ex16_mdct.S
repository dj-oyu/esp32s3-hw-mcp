/* ex16 -- the codec family's first sample: CELT/Opus's IMDCT pre-rotate and the TDAC overlap add.
 *
 * WHY THESE TWO. celt/mdct.c's clt_mdct_backward_c is the decoder's innermost loop and it is four
 * things in a row: PRE-ROTATE (N4 complex multiplies of the mirrored input pairs against a Q15
 * twiddle table), an N/4-point complex FFT in bit-reversed order, POST-ROTATE, and the TDAC window
 * fold ("Mirror on both sides for TDAC"). This file covers the stage before the FFT and the one
 * after it. Both are int16 work in this tree; the contracts below say exactly how the 16-bit domain
 * is bounded, and ex16_mdct.md next to this file carries the numbers.
 *
 * THE ONE STRUCTURAL FACT THAT SHAPES THIS FILE. CELT's pre-rotate walks the input from both ends
 * at once (mdct.c "Pre-rotate": xp1 = in, xp2 = in+stride*(N2-1), each stepping 2*stride per
 * iteration), so iteration i multiplies the pair
 *
 *      x1 = in[2*i]              (ascending in memory as i grows)
 *      x2 = in[N2-1-2*i]         (descending in memory as i grows)
 *
 * by t[i] and t[N4+i] -- two runs of ONE table (l->trig, built by clt_mdct_init). To do four
 * iterations in one 128-bit register the four (x1,x2) pairs have to be four 32-bit complex words,
 * and that is the whole problem:
 *
 *   (1) A 128-bit load at base b puts element b+L in lane L, so in ONE load the x1's rise with the
 *       lane index (dm/dL = +1/2) and the x2's fall (dm/dL = -1/2). Pairing an x1 load with an x2
 *       load and asking for the same lane index at every k gives the single equation
 *       b1+L = 2m and b2+L = 2*n4-1-2m, i.e. L = (2*n4-1-b1-b2)/2: exactly ONE lane matches, for any
 *       choice of b1 and b2. So a 4-lane form always needs a lane REVERSAL somewhere.
 *   (2) The lane shuffles this instruction set has are EE.VZIP.16 / EE.VUNZIP.16 (and the .8/.32
 *       widths): perfect interleave / de-interleave patterns. An exhaustive search of the
 *       permutation group they generate on two 128-bit registers (32 byte slots, 6 generators) has
 *       order 120 and contains NO 4-lane reversal (with or without one register COPY, which is how
 *       the post-rotate's re/im swap would be spelled -- EE.VADDS.S16 against a zero register is the
 *       only copy here, there is no QR->QR move), NO per-word 16-bit swap without such a copy, and a
 *       64-bit half swap only in ONE of the two registers (vz16 vz16 vunz32 vunz16), which is not the
 *       permutation this kernel needs.
 *       The search is reproduced in ex16_mdct.md, and it is why this file's INPUT contract is a
 *       LAYOUT contract: x arrives in mirror-pair ("zipped") order, complex word m already holding
 *       (in[2m], in[2*n4-1-2m]). Producing that order from CELT's natural layout is a separate pass;
 *       its cost is in the .md, and the honest reading of it is that the pass costs more than the
 *       kernel it feeds.
 *
 * A SECOND FACET OF THE SAME FACT, found while building this file and worth stating because it is
 * about the TWIDDLE table rather than the input: the two twiddle runs are 2*n4 BYTES apart, which is
 * a multiple of 16 only when n4 % 8 == 0. Of the levels the 48 kHz 20 ms mode asks for -- N4 = 480,
 * 240, 120, 60 (N = l->n >> shift with l->n = 2*shortMdctSize*nbShortMdcts = 2*120*8 = 1920,
 * modes.c:426-427 and celt_decoder.c:442-446) -- the TRANSIENT level N4 = 60 puts the second run 8
 * bytes off the 16-byte grid, where a plain EE.VLD.128.IP would silently round down and pair the
 * wrong entries. So the second run is fetched with the byte-granular funnel instead:
 * EE.LD.128.USAR.IP (TRM p93) records the dropped low four bits in SAR_BYTE, and EE.SRC.Q (TRM p125)
 * shifts the 32-byte concatenation of two aligned chunks right by SAR_BYTE*8 -- the two
 * instructions ex03/ex15 use for unaligned 128-bit windows, and the phase is set once because a
 * 16-byte stride preserves a pointer's low four bits. ex16_tw_zip below turns the runs into the
 * complex-word table that removes the funnel AND the VZIP from the hot loop; the .md prices all
 * three shapes.
 *
 * OPCODES (page numbers are data/pie_instructions.json's source_page, i.e. where the manual's
 * instruction section starts; ex16_mdct.md re-checks each one against the manual's bit diagram with
 * tools/asm_toolchain.py --check-instruction):
 *
 *   EE.CMUL.S16 qz, qx, qy, sel4        2 complex lanes per sel4 value: sel4[0] picks the 64-bit
 *                                       half, sel4[1] the conjugate form (unused here), and the
 *                                       result is >> SAR[5:0] then the low 16 bits. TRM p80.
 *   EE.VZIP.16   qs0, qs1               interleave 16-bit lanes -> the complex twiddles.  TRM p293.
 *   EE.VUNZIP.16 qs0, qs1               de-interleave -> the two runs back out of the complex words.
 *                                       TRM p290.
 *   EE.LD.128.USAR.IP qu, as, imm       aligned 128-bit load that also records as[3:0] in SAR_BYTE.
 *                                       TRM p93.
 *   EE.SRC.Q     qa, qs0, qs1           {qs1,qs0} >> SAR_BYTE*8 -> the 16 bytes at an arbitrary byte
 *                                       address. TRM p125.
 *   EE.LDXQ.32   qu, qs, as, s4, s8     indexed 32-bit load, ONE lane per instruction, index from a
 *                                       16-bit lane of qs (x4 bytes), destination lane s4. TRM p113.
 *                                       The only indexed LOAD in the 220-instruction set (the indexed
 *                                       store is EE.STXQ.32, TRM p145, not used here).
 *   EE.VLD.128.IP qu, as, imm  TRM p164      EE.VST.128.IP qv, as, imm  TRM p275
 *   EE.VADDS.S16  qa, qx, qy   saturating 16-bit add. TRM p146.
 *   EE.VLDBC.32   qu, as       broadcast a 32-bit word into four 32-bit lanes. TRM p173.
 *
 * WHAT THIS FILE IS NOT: proposed (nothing in the build refers to it) and NOT RUN ON SILICON. The
 * verification that exists is the assembler, objdump, the manual's bit diagrams, piesim executing
 * the actual loop bodies, and a host scalar reference -- all five pasted in ex16_mdct.md, whose last
 * section lists every semantic that is still an assumption.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex16_prerotate(const int16_t *x, const int16_t *tw, int16_t *out, uint32_t n4, uint32_t shift)
 *
 *   a2 = x     the input in MIRROR-PAIR order: n4 complex 16-bit words = 2*n4 int16, 16-BYTE
 *              ALIGNED. Word m = (re, im) = (X1[m], X2[m]) with X1[m] = in[2m], X2[m] = in[2*n4-1-2m]
 *              -- re in the LOW 16 bits, im in the HIGH 16 bits, which is the order EE.CMUL.S16's
 *              pseudo-code reads (its result's low half is qx[15:0]*qy[15:0] - qx[31:16]*qy[31:16]).
 *              The file header explains why the natural CELT layout cannot be used directly.
 *   a3 = tw    the level-0 twiddle table, CELT's l->trig, 16-BYTE ALIGNED: per level it is two runs,
 *              t[i] for i in [0,n4) then t[n4+i], and the level is selected the way mdct.c does it --
 *                  N = 4*n4;  N0 = N << shift;  level block = tw + (N0 - N) int16
 *              ("for (i=0;i<shift;i++) { N >>= 1; trig += N; }" telescopes to trig += N0-N).
 *              Q15 signed entries. Both runs are read with 64-BIT loads (EE.VLD.L.64.IP, TRM p168):
 *              the two runs are 2*n4 BYTES apart and that is a multiple of 8 but not of 16 when
 *              n4 % 8 != 0 -- the transient level N4 = 60 puts the second run 8 bytes off the 16-byte
 *              grid -- while every window this kernel reads is 8-byte aligned (the level block starts
 *              at 8*n4*(2^shift-1) bytes from an aligned tw, and 2*n4 is a multiple of 8 for every
 *              n4 % 4 == 0). A 128-bit fetch of the second run would silently round the address down
 *              (TRM p49) and pair the wrong entries; the 64-bit form cannot. The other half of the
 *              same coin: a 128-bit load would fetch eight entries per run while a four-lane group
 *              consumes four, and the upper four cannot be moved down (no half swap, header (2)).
 *              ex16_prerotate_z pays for a derived table instead and needs one 128-bit load per
 *              group; the .md prices both.
 *   a4 = out   n4 complex words, 16-BYTE ALIGNED. Word m = (yi, yr) in that memory order -- mdct.c's
 *              own store order (yp[2*rev] = yi, yp[2*rev+1] = yr, "we swap real and imag because we
 *              use an FFT instead of an IFFT"). NOT in bit-reversed order: CELT stores at
 *              2*l->kfft[shift]->bitrev[i] (kiss_fft.c:499-502 builds that table), this kernel does
 *              not, so the consumer must be the natural-order (DIF) FFT rather than CELT's DIT one.
 *              This is the one divergence the caller has to know about.
 *   a5 = n4    N/4, the complex-lane count. CONTRACT: n4 % 4 == 0 (the loop is four lanes wide and
 *              there is no scalar tail), which also makes 2*n4 a multiple of 8 and so keeps the
 *              second run's windows 8-byte aligned. Every level of the mode satisfies it: N4 =
 *              480 >> shift = 480, 240, 120, 60.
 *   a6 = shift the level shift mdct.c is called with (mode->maxLM - LM on the decoder's
 *              non-transient path, celt_decoder.c:442-446).
 *
 * Per complex lane m the instruction sequence computes, in int32 and then written as the low 16 bits
 * of the shifted value (CMUL's "lower 16-bit data of the shift result" -- TRUNCATION, not
 * saturation),
 *
 *      yi = (X1*t1 - X2*t2) >> 15        t1 = t[m],  t2 = t[n4+m]
 *      yr = (X1*t2 + X2*t1) >> 15        (mdct.c: yr = x2*t[i] + x1*t[N4+i],
 *                                                 yi = x1*t[i] - x2*t[N4+i])
 *
 * with SAR = 15 for the Q15 twiddles (celt_coef is opus_val16 and COEF_ONE is Q15ONE in the
 * non-EXT build, arch.h:198-200; S_MUL is MULT16_32_Q15, fixed_generic.h:55). CELT keeps yi/yr in 32
 * bits; the two agree exactly while -2^30 <= the product sum <= 2^30-1, and the .md's model run
 * counts the lanes outside that.
 *
 * Register walk: a2 x  a3 run 1  a4 out  a5 n4  a6 shift then SAR  a7 N=4*n4  a8 N0 -> byte offset
 * -> group counter  a9 the byte gap to the second run  a10 run 2  q0 the input words  q1 run 1 ->
 * the complex twiddles  q2 run 2  q3 the output.
 */
    .align 4
    .global ex16_prerotate
    .type ex16_prerotate,@function
ex16_prerotate:
    entry a1, 64
    /* ABI: this kernel writes a10 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    slli a7, a5, 2                  /* a7 = N = 4*n4 (int16 units) */
    mov a8, a7
    beqz a6, .Lpr_base              /* shift == 0: the level-0 block is the block we want */
.Lpr_shl:
    slli a8, a8, 1                  /* N0 = N << shift */
    addi a6, a6, -1
    bnez a6, .Lpr_shl
.Lpr_base:
    sub a8, a8, a7                  /* N0 - N in int16 units: mdct.c's "trig += N", telescoped */
    slli a8, a8, 1
    add a3, a3, a8                  /* a3 = the level block (16-byte aligned: 8*n4*(2^shift-1) bytes
                                     * and n4 is even, with tw itself aligned) */
    slli a9, a5, 1                  /* n4 int16 = 2*n4 bytes to the second run */
    add a10, a3, a9                 /* a10 = the t[n4+i] run (8-byte aligned, since n4 % 4 == 0) */
    movi a6, 15
    wsr.sar a6                      /* SAR = 15: the Q15 multiply shift */
    srli a8, a5, 2                  /* groups of four complex lanes */
    beqz a8, .Lpr_done
.Lpr_loop:
    EE.VLD.128.IP q0, a2, 16        /* four complex input words: (X1,X2) pairs, ready for CMUL */
    EE.VLD.L.64.IP q1, a3, 8        /* t[i..i+3], the four entries this group needs */
    EE.VLD.L.64.IP q2, a10, 8       /* t[n4+i..n4+i+3] */
    EE.VZIP.16 q1, q2               /* q1 = (t1,t2) complex words for the group; q2 gets the pairs of
                                     * lanes 4..7, which are outside both 64-bit windows and dropped */
    EE.CMUL.S16 q3, q0, q1, 0       /* q3[63:0]   = (yi,yr) for lanes 0,1 */
    EE.CMUL.S16 q3, q0, q1, 1       /* q3[127:64] = (yi,yr) for lanes 2,3 */
    EE.VST.128.IP q3, a4, 16
    addi a8, a8, -1
    bnez a8, .Lpr_loop
.Lpr_done:
    /* ABI: put the caller's a2 back before returning. */
    l32i a10, a1, 32
    retw.n
    .size ex16_prerotate, .-ex16_prerotate

/* void ex16_prerotate_z(const int16_t *x, const int16_t *twz, int16_t *out, uint32_t n4)
 *
 * The same arithmetic as ex16_prerotate with the twiddle handed over ALREADY INTERLEAVED (the layout
 * ex16_tw_zip builds), which removes the funnel, the extra load and the VZIP from the loop: one
 * aligned load per input block, one per twiddle block. The price is the derived table (4*n4 bytes,
 * built once per mode, not per frame) and 16-byte alignment for it.
 *
 *   a2 = x, a4 = out, a5 = n4   all as in ex16_prerotate
 *   a3 = twz   n4 complex words, 16-BYTE ALIGNED: word i = (t[i], t[n4+i]) for the level wanted
 *
 * Cost (static, from the object file): 5 PIE + 2 scalar instructions per four complex lanes against
 * ex16_prerotate's 7 PIE + 2 -- TWO instructions per group cheaper, i.e. 240 per frame over the 120
 * groups of N4 = 480 (1094 vs 846 instructions executed per call in the .md's run), paid for by one
 * 4*n4-byte table. That trade is the .md's table-lookup section; this entry point exists so the trade
 * is a measurement and not an estimate.
 */
    .align 4
    .global ex16_prerotate_z
    .type ex16_prerotate_z,@function
ex16_prerotate_z:
    entry a1, 32
    movi a6, 15
    wsr.sar a6
    srli a8, a5, 2
    beqz a8, .Lpz_done
.Lpz_loop:
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.CMUL.S16 q3, q0, q1, 0
    EE.CMUL.S16 q3, q0, q1, 1
    EE.VST.128.IP q3, a4, 16
    addi a8, a8, -1
    bnez a8, .Lpz_loop
.Lpz_done:
    retw.n
    .size ex16_prerotate_z, .-ex16_prerotate_z

/* void ex16_prerotate_idx(const int16_t *x, const int16_t *twz, int16_t *out, uint32_t n4)
 *
 * The same arithmetic again, with the twiddles fetched by the set's ONLY indexed load, EE.LDXQ.32 --
 * ONE 32-bit lane per instruction, so four of them per group against the one 128-bit load
 * ex16_prerotate_z needs. This entry point exists to PRICE that (same data, one variable) and it is
 * the shape to reach for when the twiddle index is NOT a walk: a permuted or bit-reversed
 * coefficient order, a band-interleaved table, or CELT's stride != 1 (transient) form where the
 * index depends on runtime data. For a sequential walk it loses by three instructions per group.
 *
 *   a2 = x, a3 = twz (n4 complex words), a4 = out, a5 = n4 -- n4 % 4 == 0
 *
 * The index vector (q7) holds the word indices for the group in its four LOW 16-BIT LANES -- LDXQ
 * takes its addend from a 16-bit lane of qs -- and is advanced by four per group with a saturating
 * add; the upper lanes go stale and unused. n4 <= 480 for every level, so the add never clamps.
 */
    .align 4
    .global ex16_prerotate_idx
    .type ex16_prerotate_idx,@function
ex16_prerotate_idx:
    entry a1, 32
    movi a6, 15
    wsr.sar a6                      /* SAR = 15 */
    movi a6, 0
    s16i a6, a1, 0
    movi a6, 1
    s16i a6, a1, 2
    movi a6, 2
    s16i a6, a1, 4
    movi a6, 3
    s16i a6, a1, 6
    EE.VLD.128.IP q7, a1, 0         /* q7 = [0, 1, 2, 3, -, -, -, -] in its 16-bit lanes */
    movi a6, 4
    slli a7, a6, 16
    or a6, a6, a7                   /* (4 << 16) | 4: the value 4 in each of the eight 16-bit lanes */
    s32i a6, a1, 12
    addi a6, a1, 12
    EE.VLDBC.32 q6, a6              /* q6 = eight lanes of 4: the per-group index step */
    srli a8, a5, 2
    beqz a8, .Lpi_done
.Lpi_loop:
    EE.VLD.128.IP q0, a2, 16
    EE.LDXQ.32 q1, q7, a3, 0, 0     /* one complex twiddle word per instruction ... */
    EE.LDXQ.32 q1, q7, a3, 1, 1
    EE.LDXQ.32 q1, q7, a3, 2, 2
    EE.LDXQ.32 q1, q7, a3, 3, 3     /* ... four of them for a four-lane group */
    EE.CMUL.S16 q3, q0, q1, 0
    EE.CMUL.S16 q3, q0, q1, 1
    EE.VST.128.IP q3, a4, 16
    EE.VADDS.S16 q7, q7, q6         /* index += 4 */
    addi a8, a8, -1
    bnez a8, .Lpi_loop
.Lpi_done:
    retw.n
    .size ex16_prerotate_idx, .-ex16_prerotate_idx

/* void ex16_tw_zip(const int16_t *tw, int16_t *twz, uint32_t n4, uint32_t shift)
 *
 * Build the complex-word twiddle table for ONE level, once per mode instead of once per frame:
 * twz[i] = (t[i], t[n4+i]). EE.VZIP.16 is the whole operation, on two 64-bit loads (the same pair of
 * windows ex16_prerotate reads, so the alignment argument there applies unchanged).
 *
 *   a2 = tw    the level-0 table, 16-byte aligned, as in ex16_prerotate
 *   a3 = twz   n4 complex words out, 16-byte aligned
 *   a4 = n4    n4 % 4 == 0
 *   a5 = shift the level
 *
 * Cost: 4 PIE + 2 scalar per four complex words, 732 instructions executed for N4 = 480, once per
 * mode, against the 240 per frame that ex16_prerotate_z saves over ex16_prerotate -- the derived
 * table pays for itself in about three frames.
 */
    .align 4
    .global ex16_tw_zip
    .type ex16_tw_zip,@function
ex16_tw_zip:
    entry a1, 32
    slli a6, a4, 2                  /* N = 4*n4 */
    mov a7, a6
    beqz a5, .Ltz_base
.Ltz_shl:
    slli a7, a7, 1                  /* N0 = N << shift */
    addi a5, a5, -1
    bnez a5, .Ltz_shl
.Ltz_base:
    sub a7, a7, a6                  /* N0 - N, int16 units */
    slli a7, a7, 1
    add a2, a2, a7                  /* a2 = the level block (run 1) */
    slli a6, a4, 1
    add a6, a2, a6                  /* a6 = the t[n4+i] run */
    srli a5, a4, 2                  /* groups of four complex words */
    beqz a5, .Ltz_done
.Ltz_loop:
    EE.VLD.L.64.IP q0, a2, 8        /* t[i..i+3] */
    EE.VLD.L.64.IP q1, a6, 8        /* t[n4+i..n4+i+3] */
    EE.VZIP.16 q0, q1               /* the four complex words in q0's low half */
    EE.VST.128.IP q0, a3, 16
    addi a5, a5, -1
    bnez a5, .Ltz_loop
.Ltz_done:
    retw.n
    .size ex16_tw_zip, .-ex16_tw_zip

/* void ex16_unzip_pairs(const int16_t *z, int16_t *r1, int16_t *r2, uint32_t n_pairs)
 *
 * The inverse of the layout the pre-rotate needs, and this file's real use of EE.VUNZIP.16: eight
 * complex words in, the two 16-bit runs out. DE-interleaving is free (one VUNZIP.16 per two
 * registers); INTERLEAVING two runs that live in opposite orders is what the instruction set cannot
 * do -- the header's search -- which is the asymmetry this pair of functions makes concrete.
 *
 *   a2 = z       8*n_groups complex words, 16-BYTE ALIGNED (word m = (X1[m], X2[m]))
 *   a3 = r1      X1[0 .. 8*n_groups-1], 16-byte aligned
 *   a4 = r2      X2[0 .. 8*n_groups-1], 16-byte aligned
 *   a5 = n_pairs CONTRACT: n_pairs % 8 == 0 (8 words = 16 int16 = two registers per iteration)
 */
    .align 4
    .global ex16_unzip_pairs
    .type ex16_unzip_pairs,@function
ex16_unzip_pairs:
    entry a1, 32
    srli a6, a5, 3
    beqz a6, .Luz_done
.Luz_loop:
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a2, 16
    EE.VUNZIP.16 q0, q1             /* TRM p290: q0 = lanes 0,2,4,6 of each operand, q1 = 1,3,5,7,
                                     * so q0 = X1[m..m+7] and q1 = X2[m..m+7] */
    EE.VST.128.IP q0, a3, 16
    EE.VST.128.IP q1, a4, 16
    addi a6, a6, -1
    bnez a6, .Luz_loop
.Luz_done:
    retw.n
    .size ex16_unzip_pairs, .-ex16_unzip_pairs

/* void ex16_overlap_add(const int16_t *a, const int16_t *b, int16_t *out, uint32_t n)
 *
 * The TDAC overlap add: out[i] = sat16(a[i] + b[i]). a and b are the two windowed halves -- the
 * previous frame's falling tail and the current frame's rising head -- ALREADY multiplied by their
 * window coefficients. The window is deliberately absent from this kernel: in mdct.c it enters at the
 * same place as the fold ("Mirror on both sides for TDAC": *yp1++ = SUB32_ovflw(S_MUL(x2, *wp2),
 * S_MUL(x1, *wp1)), and the same shape in prefilter_and_fold, celt_decoder.c:613-617), and this
 * instruction set has no 16-bit lane shift, so a Q15 window can only be applied through the
 * SAR-carrying multiply that the caller already has. This kernel is the pure sum, which is what
 * makes the saturation question answerable.
 *
 * DOES CELT SATURATE HERE? NO, and here is the code it comes from (opus-1.6.1 as vendored here):
 *
 *   - The window fold and the IMDCT's adds use SUB32_ovflw / ADD32_ovflw (mdct.c's TDAC mirror loop)
 *     and ADD32 (celt_decoder.c:613-617). In the FIXED_POINT build -- the only one this tree
 *     compiles (components/opus/CMakeLists.txt:67) -- those WRAP at 32 bits:
 *     ADD32_ovflw(a,b) = (opus_val32)((opus_uint32)a + (opus_uint32)b), fixed_generic.h:157-158.
 *   - The only saturation on the IMDCT path is ONE 32-bit clamp of the whole IMDCT output,
 *     celt_decoder.c:504-508 ("Saturate IMDCT output so that we can't overflow in the pitch
 *     postfilter ..."), SATURATE(x, SIG_SAT) with SIG_SAT = 536870911 = 2^29-1 (arch.h:215). It runs
 *     after clt_mdct_backward has returned, not per product and not per half.
 *   - celt_sig is opus_val32 in that build (arch.h:144), so the two halves CELT adds are 32-bit and
 *     an int16 input pair cannot overflow that sum at all.
 *
 * EE.VADDS.S16 saturates per lane at +32767/-32768 (TRM p146). So this kernel is NOT "CELT's add with
 * a saturating instruction": it is a different arithmetic that agrees with CELT wherever the true sum
 * stays inside int16 and CLAMPS where CELT would carry the wider value on through the rest of the
 * decoder. ex16_mdct.md reports both numbers -- the lanes that saturate and the lanes whose result
 * therefore differs -- instead of claiming agreement. A caller that must not clamp needs the 32-bit
 * path, which the set also has: EE.VADDS.S32 on four int32 lanes (TRM p149) at half the lane width.
 *
 *   a2 = a, a3 = b   n int16 each, 16-BYTE ALIGNED (a 128-bit PIE access forces address bits 3..0 to
 *                    0, TRM p164/p275, so a misaligned pointer is silent, not a fault)
 *   a4 = out         n int16, 16-byte aligned
 *   a5 = n           any count; n % 8 is handled by the scalar tail
 */
    .align 4
    .global ex16_overlap_add
    .type ex16_overlap_add,@function
ex16_overlap_add:
    entry a1, 64
    /* ABI: this kernel writes a10, a11 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    srli a6, a5, 3
    beqz a6, .Loa_tail
.Loa_loop:
    EE.VLD.128.IP q0, a2, 16
    EE.VLD.128.IP q1, a3, 16
    EE.VADDS.S16 q2, q0, q1         /* sat16(a+b), eight lanes per iteration */
    EE.VST.128.IP q2, a4, 16
    addi a6, a6, -1
    bnez a6, .Loa_loop
.Loa_tail:
    extui a7, a5, 0, 3
    beqz a7, .Loa_done
    movi a10, -1
    srli a10, a10, 17               /* a10 = 32767: movi's immediate is 12-bit signed and an l32r out
                                     * of .iram1 cannot reach .rodata, so no literal pool entry */
    movi a11, -8
    slli a11, a11, 12               /* a11 = -32768, same reason */
.Loa_t:
    l16si a8, a2, 0
    l16si a9, a3, 0
    add a8, a8, a9
    min a8, a8, a10                 /* the same clamp EE.VADDS.S16 applies on the vector path */
    max a8, a8, a11
    s16i a8, a4, 0
    addi a2, a2, 2
    addi a3, a3, 2
    addi a4, a4, 2
    addi a7, a7, -1
    bnez a7, .Loa_t
.Loa_done:
    /* ABI: put the caller's a2/a3 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    retw.n
    .size ex16_overlap_add, .-ex16_overlap_add
