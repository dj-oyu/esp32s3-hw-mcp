/* ex23 -- glyph coverage row (u8) -> 8-bit mask row, in PIE lanes.
 *
 * PROPOSED: this file is NOT in examples/firmware/main/CMakeLists.txt and nothing calls it. The scalar
 * code it is written against lives in another repository (cardputer-adv-pocketjs, .cache/pocketjs at the
 * pinned revision 6a0a1b6) and is left untouched.
 *
 * ---------------------------------------------------------------------------------------------------
 * WHAT THE SCALAR PATH IS (read from the source, with line numbers, before designing any of this)
 * ---------------------------------------------------------------------------------------------------
 * JS apps draw text through the PocketJS DrawList rgb565 backend. A GLYPH_RUN becomes:
 *
 *   lib.rs:702-704   let mask = self.mask_mut(); fill_mask_rect(mask, width, rect, 0);
 *   lib.rs:733       let sy = coverage_index(py - gy*scale, scale, density, coverage_h);
 *   lib.rs:736       let sx = coverage_index(px - gx*scale, scale, density, coverage_w);
 *   lib.rs:739-742   composite_mask(&mut mask[local_y*width + local_x],
 *                                   ((row[sx] as u32 * a + 127) / 255) as u8);
 *   lib.rs:1075-1078 composite_mask: *d = s + (d*(255-s) + 127) / 255
 *   raster.rs:984    coverage_index(p, scale, density, limit) =
 *                        ((((2p+1)*density) / (2*scale)).clamp(0, limit-1)) as usize
 *
 * ONE DIVISION PER PIXEL, and its divisor is a runtime value (2*scale), so the compiler must emit the
 * hardware divider: 16-18 cycles on this core (docs/pie-simd.md sec.3.7, device-measured).
 *
 * The shipped configuration is scale = 1 and atlas density = 1 (main/app_session.c:596 sets rc.scale=1,
 * :489 sets cc.raster_density=1; every atlas the firmware loads is density 1: tools/make_font.py:55 packs
 * density=1 for font_small (cell 6x8) and font_large (cell 12x16), and main/text/jsfont.c:45 does the same
 * for the Japanese slot (jpfont cell 12x12, JSFONT_SLOT 2)). At scale == density,
 *
 *     sx = floor((2p+1)*d / (2*d)) = floor((2p+1)/2) = p            (p = px - gx*scale)
 *
 * so sx is the loop counter, the clamp is dead (p <= cell_w*scale-1 = coverage_w-1 = limit-1), and the
 * whole division disappears. THAT is this kernel's fast-path condition, and it is what the scalar control
 * in ex23_covmask.md measures (130,460 swept pixels, 0 mismatches when scale == density; 7,378 of 8,400
 * differ when they are not equal, so the increment spelling must be gated on the equality).
 *
 * ---------------------------------------------------------------------------------------------------
 * THE KERNELS, AND THE CONTRACT EACH ONE NEEDS
 * ---------------------------------------------------------------------------------------------------
 * A) void ex23_cov_to_mask(uint8_t *mask, const uint8_t *cov, const uint8_t *a_row,
 *                          uint32_t n_px, uint32_t scale_tag)
 *      mask[i] = s + (mask[i]*(255-s) + 127) / 255   for i in [0, n_px),  s = (cov[i]*a + 127)/255,
 *      a = *a_row -- exactly lib.rs:739-742 over a CONTIGUOUS coverage row.
 *
 *      scale_tag declares the coverage geometry:
 *        0  = contiguous 1:1 sampling (scale == density). THE ONLY MODE THIS KERNEL IMPLEMENTS.
 *        != 0 = not implemented: returns WITHOUT WRITING ANYTHING, so a caller that forgot to check sees
 *             an unchanged mask rather than a wrong one. Gate the call with
 *             ex23_cov_to_mask_supported() (1 instruction, below).
 *
 *      alpha: a == 0 returns without writing (out == d, so the write would be a no-op; not touching the
 *      buffer is the stronger promise). a == 255 takes the vector body, which is BIT-EXACT against the
 *      scalar definition by the identity
 *          s + floor((d*(255-s)+127)/255)   with s == c (a == 255 makes the prescale exact)
 *        = floor((255(c+d) - c*d + 127) / 255)
 *        = c + d - floor((c*d + 127) / 255)                       <-- what the lanes compute
 *      (exhaustive over all 65,536 (c,d) pairs; see the .md). The same collapse does NOT hold for
 *      a < 255 (270,679 of 327,680 (a,c,d) triples differ), so 0 < a < 255 is done by this file's own
 *      scalar loop instead of an approximating vector body.
 *      The scalar loop is still division-free, so it is never slower than lib.rs:715-745; a two-stage
 *      vector body for a < 255 is a documented follow-up (it needs three products and two roundings per
 *      lane, which is one register more than this machine has alongside the unaligned coverage reader).
 *
 * B) void ex23_cov_row_advance(uint8_t *mask, const uint8_t *cov_row, uint8_t s, uint32_t n_px)
 *      mask[i] = s + (mask[i]*(255-s) + 127) / 255 with the SAME s for every i.
 *      Contract: cov_row[i] == s for all i (a uniform coverage row: a blank row, a solid row, or a row
 *      the caller has already collapsed). cov_row is read by the scalar head and tail only, so a row that
 *      is non-uniform at its ends is still right there; the body uses s and pays NO coverage load at all.
 *      That is the point of this kernel -- 27 instructions per 16 pixels against kernel A's 35.
 *        s == 0   -> returns WITHOUT WRITING (out == d for every pixel).
 *        s == 255 -> out == 255 for every pixel; the path is a scalar 255 fill (rare, and not vectorised:
 *                    vectorising it would duplicate the head/tail structure for one constant).
 *      Both early-outs are exact, not approximations (checked exhaustively in the .md).
 *
 * ---------------------------------------------------------------------------------------------------
 * ALIGNMENT, ABI, AND WHAT THE KERNELS DO NOT DO
 * ---------------------------------------------------------------------------------------------------
 *   - .iram1, .align 4 per function, windowed ABI (entry a1, 32 ... retw.n). PIE is coprocessor 3 and is
 *     saved/restored on task switch, so this is TASK context only, never an ISR (docs/pie-simd.md sec.1).
 *   - `mask` MAY BE ANY ALIGNMENT. Both kernels align it themselves: a scalar head until the pointer is
 *     16-byte aligned, then 16-pixel blocks, then a scalar tail. The head and tail are the only scalar
 *     work, and they are division-free (identity 1 below).
 *   - `cov` MAY BE ANY ALIGNMENT (kernel A). The 16-byte coverage window is fetched with the
 *     EE.LD.128.USAR.IP + EE.SRC.Q.QUP pair (TRM p93 / p128) -- the only way to get an unaligned 16-byte
 *     window out of this machine, because a plain 128-bit load DROPS the low four address bits instead of
 *     faulting (TRM 1.7.1, reproduced on silicon in data/pie_examples_measured.json finding
 *     vld128_drops_the_low_address_bits). THE PRICE: those loads cover aligned 16-byte chunks, so the
 *     kernel reads up to 15 bytes before and up to 15 bytes after the windows it uses. THE CALLER MUST
 *     GUARANTEE at least one more readable 16-byte chunk after the last block's window.
 *   - The mask is written ONLY inside [0, n_px) from the mask pointer: the head covers [0, head), each
 *     block writes exactly its 16 bytes, the tail covers the rest. Nothing past mask+n_px is written.
 *     When scale_tag != 0, or a == 0 (A), or s == 0 (B), NOTHING is written -- the mask is not even read.
 *   - CONSTANTS come from the table at the bottom of this file, walked with EE.VLDBC.16.IP and rewound
 *     once per block (`addi a8, a7, 2`), the shape ex22_decor_ray.S uses. Per-call values come from the
 *     caller: the alpha as a POINTER (a_row, the request's own signature), s as a byte.
 *   - SAR is never touched: every multiply is EE.VMULAS.U16.QACC (no SAR) and the /255 step is
 *     EE.SRCMB.S16.QACC, whose shift amount comes from an AR register (TRM p130), not SAR. A call cannot
 *     disturb a caller using SAR for its own EE.VMUL.U16 chain. q0-q7 and QACC are clobbered, as the
 *     project's kernels already assume.
 *
 * THE TWO /255 IDENTITIES THIS FILE RESTS ON (both swept exhaustively in the .md)
 *   1. floor(x/255) = (x + (x>>8) + 1) >> 8 for every 0 <= x <= 65152 (above that it fails: the first
 *      counterexample is x = 65535), and every numerator here is <= 255*255+127 = 65152 by construction.
 *      In the lanes that is two QACC passes: accumulate the numerator, SRCMB>>8 for t = x>>8, accumulate
 *      the numerator again plus 128, SRCMB>>8 for (x + t + 1) >> 8. The +127 and +128 are added as
 *      products of the constant with an all-ones lane vector: this ISA has no "add a broadcast constant
 *      to a lane" instruction (ex22's kernel hit the same wall and used the same ones-register idea).
 *   2. the prescale (c*a + 127)/255 that makes s is identity 1 with x = c*a + 127, and for a == 255 it is
 *      exactly c -- which is what lets A's fast path skip it.
 *
 * ---------------------------------------------------------------------------------------------------
 * TRM PAGES (source_page in data/pie_instructions.json; section numbers are TRM 1.8.x)
 * ---------------------------------------------------------------------------------------------------
 *   EE.LD.128.USAR.IP  p93  1.8.16     EE.SRC.Q.QUP       p128 1.8.50
 *   EE.VLD.128.IP      p164 1.8.88     EE.VST.128.IP      p275 1.8.192
 *   EE.VLDBC.16        p170 1.8.94     EE.VLDBC.16.IP     p171 1.8.95
 *   EE.VZIP.8          p294 1.8.213    EE.VUNZIP.8        p292 1.8.207
 *   EE.ZERO.Q          p299 1.8.216    EE.ZERO.QACC       p300 1.8.217
 *   EE.VMULAS.U16.QACC p245 1.8.163    EE.SRCMB.S16.QACC  p130 1.8.54
 *   EE.VADDS.S16       p146 1.8.70     EE.VSUBS.S16       p281 1.8.198
 * (EE.VMUL.U8, p207/1.8.132, exists and is deliberately NOT used: its result is 8 bits wide
 *  (`qz[7:0] = (qx[7:0]*qy[7:0]) >> SAR`), so with SAR=8 it gives floor(c*d/256), not floor((c*d+127)/255):
 *  65025>>8 = 254 (a full-coverage lane can never reach 255), the two disagree on 47,056 of the 65,536
 *  (c,d) pairs, and the SAR=0 form of the same instruction disagrees on 64,927 of them. ex23_covmask.md enumerates every u8-lane form in data/pie_instructions.json.)
 */
    .section .iram1,"ax",@progbits

/* ---------------------------------------------------------------------------------------------------
 * uint32_t ex23_cov_to_mask_supported(uint32_t scale_tag)  ->  1 if A implements this tag, else 0
 * One instruction's worth of work; exists so a caller can gate the call cheaply. Reads nothing, writes
 * nothing, clobbers only a2.
 * --------------------------------------------------------------------------------------------------- */
    .align 4
    .global ex23_cov_to_mask_supported
    .type ex23_cov_to_mask_supported,@function
ex23_cov_to_mask_supported:
    entry a1, 16
    movi a3, 1
    movi a4, 0
    movnez a3, a4, a2               /* tag != 0 -> the answer becomes 0 */
    mov a2, a3
    retw.n
    .size ex23_cov_to_mask_supported, .-ex23_cov_to_mask_supported

/* ---------------------------------------------------------------------------------------------------
 * A) void ex23_cov_to_mask(uint8_t *mask, const uint8_t *cov, const uint8_t *a_row,
 *                          uint32_t n_px, uint32_t scale_tag)
 *
 *   a2 = mask (any alignment)   a3 = cov (any alignment)   a4 = a_row (pointer to the alpha byte)
 *   a5 = n_px                   a6 = scale_tag
 *   a7 = table base  a8 = scratch  a9 = counter  a10 = alpha  a11 = mask store pointer
 *   a12 = block counter  a13 = 8 (the SRCMB shift)  a14 = 255  a15 = 127
 *
 * q-register map (exactly q0..q7, which is why the arithmetic unit is 8 lanes twice per 16 pixels:
 * the unaligned coverage reader holds two q registers of its own, and the u16 lane chain needs c, d, out
 * and its constant live at once):
 *   q0, q1  the aligned-chunk pair of the unaligned reader (live across the whole block loop)
 *   q2, q3  coverage lanes of the low / high eight pixels of the block
 *   q4, q5  mask lanes of the low / high eight pixels (reused for the result as each half retires)
 *   q6      result of half 0, then the packed 16 output bytes
 *   q7      all-ones lane vector (the multiplier that turns the +127/+128 constants into accumulations)
 */
    .align 4
    .literal_position
    .literal .Lk_ct_adr, .Lk_ct
    .global ex23_cov_to_mask
    .type ex23_cov_to_mask,@function
ex23_cov_to_mask:
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
    bnez a6, .Lct_ret              /* scale_tag != 0: not implemented -> write nothing */
    beqz a5, .Lct_ret              /* n_px == 0 */
    l8ui a10, a4, 0                /* a = *a_row */
    beqz a10, .Lct_ret             /* a == 0: out == d, so writing would change nothing: don't touch */
    l32r a7, .Lk_ct_adr
    movi a13, 8                    /* SRCMB.S16.QACC's shift (from an AR, not SAR) */
    movi a14, 255
    movi a15, 127
    movi a9, 255
    bne a10, a9, .Lct_scalar_all   /* 0 < a < 255: the scalar loop below, no vector body */
    EE.VLDBC.16 q7, a7             /* q7 = 1 in every lane (table entry 0) */
    /* ---- head: scalar until the mask pointer is 16-byte aligned ---- */
    neg a9, a2
    extui a9, a9, 0, 4
    minu a9, a9, a5
    sub a5, a5, a9
    beqz a9, .Lct_blocks
.Lct_head:
    l8ui a11, a3, 0                /* c */
    l8ui a12, a2, 0                /* d */
    add  a8, a11, a12              /* c + d  (<= 510, no wrap) */
    mull a11, a11, a12             /* c*d */
    add  a11, a11, a15             /* x = c*d + 127 */
    srli a12, a11, 8               /* t = x >> 8 */
    add  a11, a11, a12
    addi a11, a11, 1
    srli a11, a11, 8               /* q = floor(x/255) */
    sub  a8, a8, a11               /* out = (c+d) - q = c + floor((d*(255-c)+127)/255) */
    s8i  a8, a2, 0
    addi a2, a2, 1
    addi a3, a3, 1
    addi a9, a9, -1
    bnez a9, .Lct_head
.Lct_blocks:
    srli a12, a5, 4                /* blocks of 16 pixels */
    extui a5, a5, 0, 4             /* the tail: n_px & 15 (no AND-immediate on this ISA) */
    beqz a12, .Lct_tail
    mov a11, a2                    /* the store pointer walks with the load pointer (both .IP) */
    /* the unaligned reader's prologue: two aligned chunks, SAR_BYTE = cov & 15 (set by the load) */
    EE.LD.128.USAR.IP q0, a3, 16
    EE.LD.128.USAR.IP q1, a3, 16
.Lct_blk:
    addi a8, a7, 2                 /* rewind the constant walk (each block consumes 8 bytes) */
    EE.SRC.Q.QUP q2, q0, q1        /* q2 = the 16 coverage bytes starting at cov; q0 = q1 (rotate) */
    EE.LD.128.USAR.IP q1, a3, 16   /* the next aligned chunk; SAR_BYTE = the same offset again */
    EE.ZERO.Q q3
    EE.VZIP.8 q2, q3               /* q2 = c0..c7 in 16-bit lanes, q3 = c8..c15 */
    EE.VLD.128.IP q4, a2, 16       /* 16 mask bytes (16-byte aligned: the head made it so) */
    EE.ZERO.Q q5
    EE.VZIP.8 q4, q5               /* q4 = d0..d7, q5 = d8..d15 */
    /* ---- low eight pixels: out = (c+d) - floor((c*d+127)/255) ---- */
    EE.VLDBC.16.IP q6, a8, 2       /* 127 */
    EE.ZERO.QACC
    EE.VMULAS.U16.QACC q2, q4      /* QACC = c*d */
    EE.VMULAS.U16.QACC q6, q7      /* QACC = c*d + 127 = x */
    EE.SRCMB.S16.QACC q6, a13, 0   /* QACC = x >> 8 = t (the q6 readout is discarded) */
    EE.VLDBC.16.IP q6, a8, 2       /* 128 */
    EE.VMULAS.U16.QACC q2, q4      /* QACC = t + c*d */
    EE.VMULAS.U16.QACC q6, q7      /* QACC = t + c*d + 128 = x + t + 1 */
    EE.SRCMB.S16.QACC q6, a13, 0   /* q6 = (x + t + 1) >> 8 = floor(x/255) */
    EE.VADDS.S16 q2, q2, q4        /* c + d (q2 is dead after this) */
    EE.VSUBS.S16 q6, q2, q6        /* q6 = the eight result bytes' lanes */
    /* ---- high eight pixels, same sequence ---- */
    EE.VLDBC.16.IP q2, a8, 2       /* 127 */
    EE.ZERO.QACC
    EE.VMULAS.U16.QACC q3, q5
    EE.VMULAS.U16.QACC q2, q7
    EE.SRCMB.S16.QACC q2, a13, 0
    EE.VLDBC.16.IP q2, a8, 2       /* 128 */
    EE.VMULAS.U16.QACC q3, q5
    EE.VMULAS.U16.QACC q2, q7
    EE.SRCMB.S16.QACC q2, a13, 0
    EE.VADDS.S16 q4, q3, q5        /* c + d (q4 held the dead low-half d) */
    EE.VSUBS.S16 q2, q4, q2
    EE.VUNZIP.8 q6, q2             /* the low bytes of q6's eight lanes then q2's: 16 result bytes */
    EE.VST.128.IP q6, a11, 16
    addi a12, a12, -1
    bnez a12, .Lct_blk
    addi a3, a3, -32               /* the unaligned reader ran two chunks ahead of the bytes it
                                      produced (two prologue loads, one per block); the tail reads
                                      cov from the byte after the last block, so put a3 back. */
.Lct_tail:
    beqz a5, .Lct_ret
.Lct_tail_loop:
    l8ui a11, a3, 0
    l8ui a12, a2, 0
    add  a8, a11, a12
    mull a11, a11, a12
    add  a11, a11, a15
    srli a12, a11, 8
    add  a11, a11, a12
    addi a11, a11, 1
    srli a11, a11, 8
    sub  a8, a8, a11
    s8i  a8, a2, 0
    addi a2, a2, 1
    addi a3, a3, 1
    addi a5, a5, -1
    bnez a5, .Lct_tail_loop
    /* ABI: put the caller's a2/a3/a4/a5/a6/a7 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    l32i a13, a1, 44
    l32i a14, a1, 48
    l32i a15, a1, 52
    retw.n
    /* ---- 0 < a < 255: the whole run, scalar, division-free, bit-exact ---- */
.Lct_scalar_all:
    mov a9, a5
.Lct_sa_loop:
    l8ui a11, a3, 0                /* c */
    l8ui a12, a2, 0                /* d */
    mull a11, a11, a10             /* c*a */
    add  a11, a11, a15             /* x = c*a + 127 */
    srli a8, a11, 8                /* t */
    add  a11, a11, a8
    addi a11, a11, 1
    srli a11, a11, 8               /* s = floor((c*a+127)/255) */
    sub  a8, a14, a11              /* 255 - s */
    mull a8, a8, a12               /* d*(255-s) */
    add  a8, a8, a15               /* y = d*(255-s) + 127 */
    srli a12, a8, 8                /* t2 */
    add  a8, a8, a12
    addi a8, a8, 1
    srli a8, a8, 8                 /* q = floor(y/255) */
    add  a8, a8, a11               /* out = s + q, exactly lib.rs:1075-1078 */
    s8i  a8, a2, 0
    addi a2, a2, 1
    addi a3, a3, 1
    addi a9, a9, -1
    bnez a9, .Lct_sa_loop
.Lct_ret:
    /* ABI: put the caller's a2/a3/a4/a5/a6/a7 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    l32i a13, a1, 44
    l32i a14, a1, 48
    l32i a15, a1, 52
    retw.n
    .size ex23_cov_to_mask, .-ex23_cov_to_mask

/* ---------------------------------------------------------------------------------------------------
 * B) void ex23_cov_row_advance(uint8_t *mask, const uint8_t *cov_row, uint8_t s, uint32_t n_px)
 *
 *   a2 = mask (any alignment)  a3 = cov_row  a4 = s  a5 = n_px
 *   a7 = table base  a8 = scratch/walk  a9 = counter  a11 = store pointer  a12 = block counter
 *   a13 = 8  a14 = 255  a15 = 127
 *
 *   q0 = 255 - s in every lane    q1 = s in every lane    q7 = ones
 *   q2, q3 = the mask's low/high eight lanes (q4/q5 take the results)
 * The per-pixel definition is out = s + floor((d*(255-s)+127)/255), so the numerator is d*(255-s)+127
 * and the sequence is the same two-pass /255 as kernel A -- but every operand is data-free except d, which
 * is why the body is 27 instructions per 16 pixels and why a uniform row is the cheap row.
 */
    .align 4
    .literal_position
    .literal .Lk_ct_adr2, .Lk_ct
    .global ex23_cov_row_advance
    .type ex23_cov_row_advance,@function
ex23_cov_row_advance:
    entry a1, 80
    /* ABI: this kernel writes a11, a12, a13, a14, a15 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a11, a1, 32
    s32i a12, a1, 36
    s32i a13, a1, 40
    s32i a14, a1, 44
    s32i a15, a1, 48
    beqz a5, .Lrv_ret
    extui a4, a4, 0, 8             /* s, as a byte */
    beqz a4, .Lrv_ret              /* s == 0: out == d for every pixel -> write nothing */
    l32r a7, .Lk_ct_adr2
    movi a13, 8
    movi a14, 255
    movi a15, 127
    s16i a4, a1, 0                 /* the two lane vectors are built once, in the frame */
    sub  a8, a14, a4
    s16i a8, a1, 4
    EE.VLDBC.16 q1, a1             /* q1 = s */
    addi a8, a1, 4
    EE.VLDBC.16 q0, a8             /* q0 = 255 - s */
    EE.VLDBC.16 q7, a7             /* q7 = ones (table entry 0) */
    movi a9, 255
    beq a4, a9, .Lrv_fill          /* s == 255: every pixel becomes 255 */
    /* ---- head ---- */
    neg a9, a2
    extui a9, a9, 0, 4
    minu a9, a9, a5
    sub a5, a5, a9
    beqz a9, .Lrv_blocks
.Lrv_head:
    l8ui a11, a3, 0                /* cov_row[i], == s by contract */
    l8ui a12, a2, 0                /* d */
    sub  a8, a14, a11              /* 255 - s */
    mull a8, a8, a12
    add  a8, a8, a15               /* y = d*(255-s) + 127 */
    srli a12, a8, 8
    add  a8, a8, a12
    addi a8, a8, 1
    srli a8, a8, 8                 /* q = floor(y/255) */
    add  a8, a8, a11               /* out = s + q */
    s8i  a8, a2, 0
    addi a2, a2, 1
    addi a3, a3, 1
    addi a9, a9, -1
    bnez a9, .Lrv_head
.Lrv_blocks:
    srli a12, a5, 4
    extui a5, a5, 0, 4
    beqz a12, .Lrv_tail
    mov a11, a2
.Lrv_blk:
    addi a8, a7, 2                 /* rewind the constant walk */
    EE.VLD.128.IP q2, a2, 16
    EE.ZERO.Q q3
    EE.VZIP.8 q2, q3               /* q2 = d0..d7, q3 = d8..d15 */
    EE.VLDBC.16.IP q4, a8, 2       /* 127 */
    EE.ZERO.QACC
    EE.VMULAS.U16.QACC q2, q0      /* QACC = d*(255-s) */
    EE.VMULAS.U16.QACC q4, q7      /* + 127 = y */
    EE.SRCMB.S16.QACC q4, a13, 0   /* t */
    EE.VLDBC.16.IP q4, a8, 2       /* 128 */
    EE.VMULAS.U16.QACC q2, q0
    EE.VMULAS.U16.QACC q4, q7
    EE.SRCMB.S16.QACC q4, a13, 0   /* q = floor(y/255) */
    EE.VADDS.S16 q4, q4, q1        /* out = s + q */
    EE.VLDBC.16.IP q5, a8, 2       /* 127 */
    EE.ZERO.QACC
    EE.VMULAS.U16.QACC q3, q0
    EE.VMULAS.U16.QACC q5, q7
    EE.SRCMB.S16.QACC q5, a13, 0
    EE.VLDBC.16.IP q5, a8, 2       /* 128 */
    EE.VMULAS.U16.QACC q3, q0
    EE.VMULAS.U16.QACC q5, q7
    EE.SRCMB.S16.QACC q5, a13, 0
    EE.VADDS.S16 q5, q5, q1        /* out = s + q */
    EE.VUNZIP.8 q4, q5
    EE.VST.128.IP q4, a11, 16
    addi a12, a12, -1
    bnez a12, .Lrv_blk
.Lrv_tail:
    beqz a5, .Lrv_ret
.Lrv_tail_loop:
    l8ui a11, a3, 0
    l8ui a12, a2, 0
    sub  a8, a14, a11
    mull a8, a8, a12
    add  a8, a8, a15
    srli a12, a8, 8
    add  a8, a8, a12
    addi a8, a8, 1
    srli a8, a8, 8
    add  a8, a8, a11
    s8i  a8, a2, 0
    addi a2, a2, 1
    addi a3, a3, 1
    addi a5, a5, -1
    bnez a5, .Lrv_tail_loop
.Lrv_ret:
    /* ABI: put the caller's a3/a4/a5/a6/a7 back before returning. */
    l32i a11, a1, 32
    l32i a12, a1, 36
    l32i a13, a1, 40
    l32i a14, a1, 44
    l32i a15, a1, 48
    retw.n
    /* ---- s == 255: a scalar 255 fill over exactly [mask, mask+n_px). The values are known, so this
       path reads nothing at all -- not even cov_row. (Not vectorised: it is one constant, and a vector
       body would duplicate the head/tail structure for a case this rare.) ---- */
.Lrv_fill:
    movi a8, 255
.Lrv_fill_loop:
    s8i a8, a2, 0
    addi a2, a2, 1
    addi a5, a5, -1
    bnez a5, .Lrv_fill_loop
    /* ABI: put the caller's a3/a4/a5/a6/a7 back before returning. */
    l32i a11, a1, 32
    l32i a12, a1, 36
    l32i a13, a1, 40
    l32i a14, a1, 44
    l32i a15, a1, 48
    retw.n
    .size ex23_cov_row_advance, .-ex23_cov_row_advance

/* ---------------------------------------------------------------------------------------------------
 * The constant table. 16-bit entries, walked in issue order by EE.VLDBC.16.IP (which broadcasts the
 * halfword at {as[31:1],1{0}} to all eight lanes, TRM p170-171). THE WALK ORDER IS THE ORDER OF THE BODY'S
 * LOADS: entry 1 is 127 and entry 2 is 128 for the low eight pixels, entry 3 is 127 and entry 4 is 128 for
 * the high eight. A load moved without moving its entry silently changes a constant and nothing fails to
 * assemble. Entry 0 (the ones) is read once per CALL, not per block, so it sits outside the walk.
 * --------------------------------------------------------------------------------------------------- */
    .align 2
.Lk_ct:
    .short 1                        /* 0) the ones lane vector (VLDBC.16 from the table base) */
    .short 127                      /* 1) the rounding constant, first pass */
    .short 128                      /* 2) the +1 of (x + t + 1) >> 8, second pass */
    .short 127                      /* 3) the same pair for the high eight pixels */
    .short 128                      /* 4) */
    .short 0xFFFF                   /* 5) unused by these two kernels (room for a fill path) */
