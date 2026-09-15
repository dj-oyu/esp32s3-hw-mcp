/* ex14 -- the span-fill rasteriser stage: turn span x-pairs into framebuffer pixels.
 *
 * This is item 2 of notes/08-media-3d-perf.md's production list ("ex10 スパン塗りラスタライザ",
 * renumbered ex14 here), the stage between ex07 (vertex transform) and the transfer stage: after the
 * transform and the perspective divide, a triangle is a list of horizontal spans, one per scanline, and
 * the span filler is the pass that turns them into pixels. It is the last CPU stage before the LCD
 * transfer that notes/08's frame budget ("~67 cycles/px at 30 fps") has to absorb.
 *
 * Two kernels, both from the manual's pseudo-code (data/pie_instructions.json, page cited per op):
 *
 *   ex14_span_fill -- fill n horizontal spans on ONE scanline of a 16-bit-per-pixel framebuffer,
 *                     eight pixels (128 bits) per iteration with EE.VST.128.IP.
 *   ex14_edge_dda  -- the fixed-point edge walk that PRODUCES those spans: four edges per int32 vector
 *                     with EE.VADDS.S32 / EE.VSR.32 / EE.VMIN.S32 / EE.VMAX.S32, saturating so a long
 *                     steep edge cannot wrap.
 *
 * ---------------------------------------------------------------------------------------------------
 * ex14_span_fill -- the layout contract, stated exactly
 * ---------------------------------------------------------------------------------------------------
 *
 *   void ex14_span_fill(int16_t *fb, uint32_t stride_px, uint32_t y, const int32_t *x_pairs,
 *                       const int16_t *color8, uint32_t n_spans);
 *
 *   fb         base of the framebuffer, 16 bpp, pixel (x, y) at fb[y*stride_px + x].  MUST be
 *              16-byte aligned (see the alignment rule below).
 *   stride_px  row stride in PIXELS.  MUST be a multiple of 8, which makes every row start 16-byte
 *              aligned (2*8 = 16 bytes per eight pixels), so "pixel index is a multiple of 8" and
 *              "byte address is 16-byte aligned" are the same statement -- the whole chunk arithmetic
 *              below is in pixel units because of this.
 *   y          scanline index.  The kernel does NOT bounds-check y or the x pair against the panel;
 *              the caller clips (ex14_edge_dda's clip box is where that happens in this pair).
 *   x_pairs    2*n_spans int32, interleaved: x_pairs[2j] = x0 (inclusive), x_pairs[2j+1] = x1
 *              (exclusive) for span j.  4-byte aligned (read with l32i).
 *   color8     8 int16, MUST be 16-byte aligned and MUST be eight copies of the same colour: the
 *              128-bit path writes all eight lanes, the sub-chunk edges write lane 0 (l16ui).
 *   n_spans    number of spans.
 *
 * Each span fills exactly the pixels x0 .. x1-1 of scanline y and NOTHING else.  The address of pixel
 * p is  addr(p) = (char *)fb + 2*(y*stride_px + p)  bytes, so with fb 16-byte aligned and stride_px a
 * multiple of 8:   p % 8 == 0  <=>  addr(p) % 16 == 0   and   p % 4 == 0  <=>  addr(p) % 8 == 0.
 *
 * ---------------------------------------------------------------------------------------------------
 * What happens when a span is NOT 16-byte aligned (this is the design decision of the kernel)
 * ---------------------------------------------------------------------------------------------------
 *
 * TRM p49 (data/pie_instructions.json, EE.VST.128.IP p275 text): "This instruction forces the lower
 * 4 bits of the access address in register as to 0 and stores the 128 bits ... to memory".  The same
 * rounding is on EE.VST.L.64.IP / EE.VST.H.64.IP with the lower 3 bits (p279 / p277) and on
 * EE.SRCQ.128.ST.INCP with the lower 4 bits (p132).  So a PIE store CANNOT write a partial chunk: on a
 * misaligned address it silently writes the enclosing 16-byte chunk and clobbers the pixels outside
 * the span (ex03 reproduced exactly this on silicon for the load side).  EE.SRC.Q / EE.SRCQ.128 do not
 * change that -- they align the READ side (a funnel shift of two loaded chunks, p125/p132), while the
 * store address is still rounded down, so they cannot write a 16-byte window that starts mid-chunk.
 *
 * The kernel therefore never asks a 128-bit store to do a partial chunk.  Per span:
 *
 *   head   hx = (x0 + 7) & ~7  (round x0 up to the next eight-pixel boundary; 8 pixels = 16 bytes)
 *          head_end = min(hx, x1)
 *          pixels [x0, head_end) are written with scalar `s16i` : 2*(head_end - x0) bytes, 0..14 bytes,
 *          at byte address 2*p which for odd p is not even 8-byte aligned, hence no PIE store form
 *          (there is no 16-bit or 8-bit PIE store in the 220-entry instruction list).
 *   body   chunks of eight pixels starting at hx: floor((x1 - hx)/8) iterations of
 *          EE.VST.128.IP q6, a12, 16 -- 16 bytes each, and by construction the address is 16-byte
 *          aligned (hx % 8 == 0 => 2*hx % 16 == 0), so the low-4-bit forcing at p275 is a NO-OP and
 *          this path is byte-exact.  A chunk is only entered when all eight of its pixels are inside
 *          [x0, x1), so it cannot spill into the neighbouring span.
 *   tail   r = x1 - (hx + 8*body), 0..7 pixels left.  If r >= 4 one EE.VST.L.64.IP q6, a12, 8 writes
 *          8 bytes = the four pixels [hx+8*body, +4) from lanes 0..3 (p279: the lower 64 bits of qv);
 *          its address 2*p with p a multiple of 4 is 8-byte aligned, so the low-3-bit forcing is again
 *          a no-op.  The remaining 0..3 pixels go through `s16i` (<= 6 bytes).
 *
 * Byte accounting per span, which is what makes "never outside [x0, x1)" checkable:
 *   2*(x1 - x0) = 2*(head_end - x0) + 16*body + [ 8 if r >= 4 ] + 2*(r mod 4)
 * A span that lies entirely inside ONE 16-byte chunk (x0 >= 8*k, x1 <= 8*k+8) has hx > x1, so
 * head_end = x1: the body and tail are empty and the head's scalar stores do the whole span -- that
 * path is deliberately the common case for sprite-sized spans, and it is the reason the kernel never
 * needs a read-modify-write of the enclosing chunk.
 *
 * Rejection rule (explicit and testable): a span with x0 >= x1 is SKIPPED -- no load of the colour, no
 * store of any width, not even a read of the framebuffer.  The compare is signed (`bge a9, a10`), so
 * x1 <= x0, x0 == x1 and negative widths are all the same case, and the only effect is that the span
 * counter advances.  ex14_edge_dda produces such spans on purpose: after its clip box collapses a
 * span (or a finished triangle's frozen edges are parked outside the box) x0 >= x1 reaches the filler.
 *
 * ---------------------------------------------------------------------------------------------------
 * ex14_edge_dda -- the fixed-point edge walk, contract and rounding rule
 * ---------------------------------------------------------------------------------------------------
 *
 *   void ex14_edge_dda(int32_t *x_left, int32_t *x_right, int32_t *edge_x, const int32_t *edge_dx,
 *                      const int32_t *clip, uint32_t n);
 *
 *   n          number of spans; four span edges are one int32 vector (4 lanes), G = ceil(n/4) groups.
 *   edge_x     Q16.16 fixed point, 2*G*4 int32 in TWO PLANES, 16-byte aligned, walked IN PLACE: the
 *                kernel writes the advanced state back (a DDA that did not write its state back would
 *                force the caller to duplicate the saturating add -- that is why this one is not
 *                `const`):
 *                plane L (left edges) : edge_x[4*g + i]        = the left edge of span 4*g+i
 *                plane R (right edges): edge_x[4*G + 4*g + i]  = the right edge of span 4*g+i
 *              The lanes of the last group with index 4*G-1 >= j >= n are PADDING LANES: they are
 *              inside the array, they are read (a 128-bit load cannot read less than four lanes) and
 *              their advanced values are written back, but every instruction in the loop is lane-wise
 *              (no lane ever sees another lane's value), so the padding lanes cannot influence the
 *              valid ones -- the model checks that with hostile padding.
 *   edge_dx    same shape and alignment, the per-scanline increment in Q16.16 (not modified).
 *   clip       { clip_lo_px, clip_hi_px } as two int32, 4-byte aligned (read with EE.VLDBC.32, which
 *              forces the low 2 address bits to 0, p173).  lo <= hi is required.
 *   x_left /   4*G int32 each, 16-byte aligned, the integer pixel spans of THIS scanline: x_left[j]
 *   x_right    inclusive, x_right[j] exclusive (the pair the filler consumes).  j >= n is the padding
 *              group's undefined result and is not read by the caller.  The two arrays are separate,
 *              which is why the state is de-interleaved into the two planes above: a 128-bit store
 *              can only land in ONE array, and the lane-wise ops do not care which side a lane is.
 *
 * A fixed-point value f denotes the real coordinate f / 65536 (Q16.16).  The walk for one scanline is
 *
 *      f' = sat32(f + dx)                       EE.VADDS.S32   (p149: min(max(a+b, -2^31), 2^31-1))
 *
 * and the span endpoints come from the half-open pixel rule
 *
 *      pixel p is inside the span  <=>  fL/65536 <= p < fR/65536
 *
 * so both endpoints are the same rounding:  x = ceil(f / 65536) = floor((f + 0xFFFF) / 65536), which
 * the kernel evaluates as the saturating bias-add EE.VADDS.S32 (+0xFFFF splat) followed by the
 * ARITHMETIC shift EE.VSR.32 with SAR = 16 (p274: "the higher bits are padded with signed bit"), so
 * negative coordinates round the same way.  x = ceil(fL) and x = ceil(fR) is exactly the half-open
 * interval [ceil(fL), ceil(fR)) for integer pixels, and a span with ceil(fL) >= ceil(fR) is the empty
 * span that ex14_span_fill rejects -- degeneracy and rejection are the same code path in the pair.
 *
 * The clamp (EE.VMAX.S32 against the lo splat, then EE.VMIN.S32 against the hi splat, p183/p192)
 * happens AFTER the shift, on the pixel index, so the filler can never be handed a span outside the
 * box -- and clamping either endpoint can only make x0 >= x1, i.e. turn a span into a rejected one.
 * The saturation of the walk is what keeps a long edge from wrapping (a wrapping accumulator would
 * jump to the opposite side of the box and paint a whole scanline); see the model's wrap test.
 *
 * Documented boundary: the bias add saturates too, so for f in (2^31-1-0xFFFF, 2^31-1] the result
 * pins at 2^31-1 and x = 32767 instead of the exact ceil 32768 (i.e. |x| > 32766.999 px).  The model
 * prints the exact f values where that happens; no panel of this class is that wide.  Note that the
 * clip box is applied AFTER the shift, so inside a real box the artifact is not even observable: the
 * model shows both (raw ceil with and without the box).
 *
 * EE.VSUBS.S32 (p284, the saturating subtract) is deliberately NOT used: nothing here subtracts.  The
 * ceil is `f + 0xFFFF` and the shift, and both the walk and the bias add only ever add.  The
 * alternative spelling of the ceil, ceil(f/2^16) = -((-f) >> 16), would use EE.VSUBS.S32 twice per
 * plane (p=0 - f, then 0 - shifted) and would move the same one-pixel boundary artifact from the top
 * of the int32 range to the bottom -- two more instructions per group for no gain; the model has both
 * variants and reports that they differ only in which end the artifact sits at.
 *
 * ---------------------------------------------------------------------------------------------------
 * Constants come from the caller (no l32r from .iram1 into .rodata: the linker refuses it), the ABI is
 * the windowed one (entry a1, 32 ... retw.n), each function is .align 4, and both kernels are leaf
 * functions with no stack frame beyond the window.  Both are .iram1 (they run in the SPI-flash cache
 * off path like the rest of the examples).
 * ---------------------------------------------------------------------------------------------------
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex14_span_fill(int16_t *fb, uint32_t stride_px, uint32_t y, const int32_t *x_pairs,
 *                     const int16_t *color8, uint32_t n_spans)
 *   a2 = fb, a3 = stride_px, a4 = y, a5 = x_pairs, a6 = color8, a7 = n_spans
 *   Row base: a8 = fb + 2*y*stride_px.  For every span: a9 = x cursor, a10 = x1, a11 = head end,
 *   a12 = byte write pointer, a13 = the scalar colour (lane 0), a14 = span counter, a15 = scratch. */
    .align 4
    .global ex14_span_fill
    .type ex14_span_fill,@function
ex14_span_fill:
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
    mull a8, a4, a3                 /* a8 = y * stride_px (pixels) */
    slli a8, a8, 1                  /* -> bytes (2 bytes per pixel) */
    add a8, a8, a2                  /* a8 = &fb16[y][0] */
    EE.VLD.128.IP q6, a6, 0         /* q6 = the eight colour lanes (a6 must be 16-byte aligned) */
    l16ui a13, a6, 0                /* lane 0 as a scalar, for the sub-chunk head and tail */
    beqz a7, .Lsf_done
    movi a14, 0                     /* span counter */
.Lsf_span:
    l32i a9, a5, 0                  /* x0 (inclusive) */
    l32i a10, a5, 4                 /* x1 (exclusive) */
    addi a5, a5, 8
    bge a9, a10, .Lsf_next          /* REJECT: x0 >= x1 -> not one load, not one store */
    movi a15, -8
    addi a11, a9, 7
    and a11, a11, a15               /* a11 = hx = (x0 + 7) & ~7: the first 8-pixel boundary >= x0 */
    blt a11, a10, .Lsf_head_ok
    mov a11, a10                    /* head_end = min(hx, x1) -- a span inside one chunk stops here */
.Lsf_head_ok:
    slli a12, a9, 1
    add a12, a12, a8                /* a12 = &byte of pixel x0 */
    beq a9, a11, .Lsf_body          /* head empty when x0 was already on the boundary */
.Lsf_head:
    s16i a13, a12, 0                /* scalar 2-byte store: 0..7 pixels, 0..14 bytes */
    addi a12, a12, 2
    addi a9, a9, 1
    blt a9, a11, .Lsf_head
.Lsf_body:
    addi a15, a9, 8
    blt a10, a15, .Lsf_tail         /* x1 < a9 + 8: no whole chunk left for this span */
.Lsf_chunk:
    EE.VST.128.IP q6, a12, 16       /* 8 pixels = 16 bytes; a12 % 16 == 0, so nothing is rounded */
    addi a9, a9, 8
    addi a15, a15, 8
    bge a10, a15, .Lsf_chunk
.Lsf_tail:
    sub a15, a10, a9                /* 0..7 pixels left (a9 tracks the pixel, a12 the byte address) */
    blti a15, 4, .Lsf_rest
    EE.VST.L.64.IP q6, a12, 8       /* 4 pixels = 8 bytes from lanes 0..3; a12 % 8 == 0 */
    addi a15, a15, -4
.Lsf_rest:
    beqz a15, .Lsf_next
.Lsf_rest_loop:
    s16i a13, a12, 0                /* the last 0..3 pixels */
    addi a12, a12, 2
    addi a15, a15, -1
    bnez a15, .Lsf_rest_loop
.Lsf_next:
    addi a14, a14, 1
    blt a14, a7, .Lsf_span
.Lsf_done:
    /* ABI: put the caller's a2/a3/a4/a5/a6/a7 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    l32i a13, a1, 44
    l32i a14, a1, 48
    l32i a15, a1, 52
    retw.n
    .size ex14_span_fill, .-ex14_span_fill

/* void ex14_edge_dda(int32_t *x_left, int32_t *x_right, int32_t *edge_x, const int32_t *edge_dx,
 *                    const int32_t *clip, uint32_t n)
 *   a2 = x_left, a3 = x_right, a4 = edge_x plane L, a5 = edge_dx plane L, a6 = clip, a7 = n,
 *   a8 = edge_x plane R, a9 = edge_dx plane R, a10 = 16*G (the plane offset), a11 = G = ceil(n/4),
 *   a12 = the SAR value 16, a13 = group counter, a15 = scratch (the 3).
 *   q4 = clip lo splat, q5 = clip hi splat, q6 = the 0x0000FFFF ceil bias in all four lanes,
 *   q0/q1 = the edge and its increment, q2 = the pixel result.
 *   One loop iteration advances four LEFT edges and four RIGHT edges (eight edges per iteration).
 *   Leaves SAR = 16 behind: ex14_span_fill does not read SAR, but anything using EE.VMUL must reset it. */
    .align 4
    .global ex14_edge_dda
    .type ex14_edge_dda,@function
ex14_edge_dda:
    entry a1, 64
    /* ABI: this kernel writes a10, a11, a12, a13 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    s32i a12, a1, 40
    s32i a13, a1, 44
    EE.VLDBC.32 q4, a6              /* q4 = {4{clip_lo_px}}   (low 2 address bits forced to 0, p173) */
    addi a6, a6, 4
    EE.VLDBC.32 q5, a6              /* q5 = {4{clip_hi_px}} */
    movi a11, -1
    srli a11, a11, 16               /* a11 = 0x0000FFFF */
    EE.MOVI.32.Q q6, a11, 0         /* the ceil bias, one 32-bit lane at a time (p119) */
    EE.MOVI.32.Q q6, a11, 1
    EE.MOVI.32.Q q6, a11, 2
    EE.MOVI.32.Q q6, a11, 3
    movi a12, 16
    ssr a12                         /* SAR = 16, the Q16.16 fractional width, for EE.VSR.32 */
    addi a10, a7, 3
    srli a11, a10, 2                /* a11 = G = ceil(n/4) groups */
    slli a10, a11, 4                /* 16 bytes per group per plane */
    add a8, a4, a10                 /* a8 = edge_x plane R */
    add a9, a5, a10                 /* a9 = edge_dx plane R */
    beqz a11, .Ldda_done
    mov a13, a11
.Ldda_group:
    EE.VLD.128.IP q0, a4, 16        /* the four LEFT edges of this group (a4 += 16) */
    EE.VLD.128.IP q1, a5, 16        /* their four per-scanline increments (a5 += 16) */
    EE.VADDS.S32 q0, q0, q1         /* the walk, saturating: a long edge cannot wrap (p149) */
    EE.VST.128.IP q0, a4, -16       /* ... and the advanced state goes back in place (p275) */
    EE.VADDS.S32 q2, q0, q6         /* + 0xFFFF, saturating: the ceil bias */
    EE.VSR.32 q2, q2                /* >> 16 ARITHMETIC (p274): negative coordinates ceil too */
    EE.VMAX.S32 q2, q2, q4          /* clamp the pixel index into the clip box (p183) */
    EE.VMIN.S32 q2, q2, q5          /* (p192) */
    EE.VST.128.IP q2, a2, 16        /* four x_left pixels (a2 += 16) */
    EE.VLD.128.IP q0, a8, 16        /* the four RIGHT edges of this group (a8 += 16) */
    EE.VLD.128.IP q1, a9, 16
    EE.VADDS.S32 q0, q0, q1
    EE.VST.128.IP q0, a8, -16
    EE.VADDS.S32 q2, q0, q6
    EE.VSR.32 q2, q2
    EE.VMAX.S32 q2, q2, q4
    EE.VMIN.S32 q2, q2, q5
    EE.VST.128.IP q2, a3, 16        /* four x_right pixels (a3 += 16) */
    addi a4, a4, 16                 /* the state pointer advanced for real (the -16 above undid it) */
    addi a8, a8, 16
    addi a13, a13, -1
    bnez a13, .Ldda_group
.Ldda_done:
    /* ABI: put the caller's a2/a3/a4/a5 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    l32i a13, a1, 44
    retw.n
    .size ex14_edge_dda, .-ex14_edge_dda
