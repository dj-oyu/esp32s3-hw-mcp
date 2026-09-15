/* ex22 -- the decorative-ray pixel path (flower's god rays) in PIE lanes.
 *
 * PROPOSED: this file is NOT in main/CMakeLists.txt and nothing calls it. The scalar path it is written
 * against lives in another repository (cardputer-adv-pocketjs, main/scene/garden.c) and is left untouched.
 *
 * ---------------------------------------------------------------------------------------------------
 * WHAT THE SCALAR PATH IS (read from the source, with line numbers, before designing any of this)
 * ---------------------------------------------------------------------------------------------------
 * The decorative layer runs once per ROW, four times per row (four opportunities):
 *
 *   flower.c:1532        garden_row_blend(row,py,garden,...)          <- per row of the strip
 *   garden.c:1578-1580   garden_row_blend -> garden_atmosphere_row
 *   garden.c:1571        garden_decor_row(row,y,f)                    <- the whole decorative pass
 *   garden.c:1466-1481   for(x=lo;x<=hi;x++) { ...profiles...row[x]=garden_decor_mix(...) }
 *
 * Inside that per-pixel loop:
 *   garden.c:1473  int light  = garden_decor_profile(x*256-cx,inv)*gain>>8;        <- CHANGES WITH x
 *   garden.c:1474  int shadow = garden_decor_profile(x*256-shadow_cx,shadow_inv)*gain>>8;
 *   garden.c:1469-1471 gap/protect from garden_shaft(y,...) (garden.c:1431)        <- per pixel, per-row scalars
 *   garden.c:1479  int d = garden_dither(x,y)*64+32;                               <- per pixel
 *   garden.c:1480  row[x]=garden_decor_mix(row[x],light,shadow,d);                  <- per pixel
 * while the *parameters* are constant across the row:
 *   garden.c:1449 cloud (noise, per row), 1455 cx, 1458 radius, 1460 shadow_cx, 1465 inv/shadow_inv
 *   (two variable divisions per active ray-row, not per pixel -- flower-decor-cost.md:30), 1450 strength.
 *
 * So: THE PROFILE VARIES PER PIXEL, THE PARAMETERS DO NOT. That is the whole design permission for this
 * file. The row header (per row: four layer hashes, a fade test, a y>=end test, the arrival ramp, two
 * reciprocals, the span clip) stays scalar in the caller; everything between lo and hi is lane work.
 *
 * ---------------------------------------------------------------------------------------------------
 * THE THREE KERNELS, AND THE CONTRACT EACH ONE NEEDS
 * ---------------------------------------------------------------------------------------------------
 * A) ex22_decor_blend(fb, coverage, tint8, n_px, shift)
 *      out[i] = the RGB565 pixel attenuated by coverage[i] and then given a small warm bias tint8[i].
 *      coverage is 8 bytes per 8 pixels, ALIGNED 8.  tint8 is 8 x int16 per 8 pixels, ALIGNED 16, each
 *      lane holding a value 0..255.  fb is 8 x int16 (the packed framebuffer row), ALIGNED 16, and is
 *      read and written in place.  n_px must be a MULTIPLE OF 8: there is no scalar head or tail in this
 *      kernel on purpose (the caller owns the span; garden.c's own loop already runs lo..hi, so the caller
 *      pads the two ends itself).
 *
 *      Exactly, per pixel, with c=coverage, t=tint8, att=256-c:
 *        r=(p>>11)&31  g=(p>>5)&63  b=p&31
 *        R=min(31, ((r*att)>>8) + (t>>3))
 *        G=min(63, ((g*att)>>8) + (t>>4))
 *        B=min(31, ((b*att)>>8) + (t>>5))
 *        out=(R<<11)|(G<<5)|B
 *      Two floors only: the >>8 of the attenuation and the >>3/4/5 of the bias. 255*255 fits 16 bits in
 *      every product here, which is what makes EE.VMUL.U16 exact (see the semantics note below).
 *
 * B) ex22_decor_coverage(cov_out, ix, n_px, shift)
 *      A CELT-style parabola where the scalar code has a reciprocal-plus-cubic. ix is the per-pixel
 *      SIGNED Q11 distance from the opening's axis, ALREADY NORMALISED to the opening's half-width, so
 *      |ix| <= 2048 is the contract (2048 = the edge, 0 = the axis). The caller makes that normalisation
 *      free by building the ramp with the slope folded in:
 *          K = floor(524288/W)           W = the half-width in Q8 units (radius*256 in garden.c)
 *          C = floor(cx*K/256)           cx = the axis position in Q8 units
 *          ix[x] = x*K - C               one running add per block, not one division per pixel
 *      and clips ix to +-2048. Two things about the scale: it is 16x finer than Q7 on purpose, because
 *      W/256 is 18..31 pixels, so the slope K is only about 6 in Q7 units and floor() of it would stretch
 *      the profile by up to 15% at the wrong radius (measured in ex22_decor_ray.md); and u^2 for
 *      u <= 2048 reaches 4.2e6, which is why the square is taken through QACC (EE.VMULAS.U16.QACC
 *      accumulates the full 40-bit product and EE.SRCMB.S16.QACC reads it out shifted by AR) rather
 *      than through a 16-bit lane multiply.  The lane evaluation is
 *          u   = |ix|
 *          cov = min(255, 256 - (u*u >> 14))            (u = 2048*xi, so u^2>>14 = 256*xi^2)
 *      which is the parabola 256*(1-xi^2) with zero slope at the axis AND at the edge, the same shape
 *      family as the CELT parabola in docs/pie-simd.md section 3 (62 -> 40 instructions). It is NOT the
 *      scalar's shape: garden_smooth (garden.c:52) is the cubic (1-xi)^2 (1+2xi), and the two differ by
 *      2*xi^2*(1-xi), i.e. up to 30% of full scale at xi = 2/3 -- the parabola is the wider one. That is
 *      a visual decision, measured and written down in ex22_decor_ray.md, not a rounding difference. The
 *      exact cubic needs q*q*(768-2q) with a 4.99e7 product, which no 16-bit lane holds and which the
 *      accumulate-multiply shape of QACC cannot form either (it accumulates x*y for the SAME lane, so a
 *      three-term product is not reachable in two steps).
 *      `shift` is this kernel's fixed-point exponent: AR for the QACC readout, and 14 is the contract.
 *      cov_out is 8 x int16 per 8 pixels, ALIGNED 16. n_px must be a multiple of 8.
 *
 * C) ex22_decor_rowmask(cov, protect, n_px)
 *      Returns a 32-bit mask: BIT b SET == block b (pixels 8b..8b+7) has at least one surviving lane;
 *      mask == 0 == the whole row is below threshold and can be dropped. A lane survives iff
 *      cov >= 8 AND protect >= 32 (the two thresholds are part of the contract: 8/256 of the coverage
 *      parabola, and 3 pixels inside the 24-pixel protection ramp of garden.c:1471). Per-pixel branches
 *      are replaced by per-lane masks; the only branch left is per row.
 *      cov and protect are 8 x int16 per 8 pixels, ALIGNED 16. n_px must be a multiple of 8; the row must
 *      be <= 31 blocks (248 pixels) because the return is 32 bits.
 *
 * ---------------------------------------------------------------------------------------------------
 * CONTRACT: ALIGNMENT, ABI, AND WHAT THE KERNELS DO NOT DO
 * ---------------------------------------------------------------------------------------------------
 *   - .iram1, .align 4 per function, windowed ABI (entry a1,32 ... retw.n).
 *   - Every 128-bit PIE address must be 16-byte aligned: the low four address bits are DROPPED, not
 *     faulted (TRM p49 figure 1.7-1; reproduced on silicon in data/pie_examples_measured.json, finding
 *     vld128_drops_the_low_address_bits; docs/pie-simd.md section 5). The coverage pointer of kernel A is
 *     an EE.VLD.L.64.IP, whose address is formed as {as[31:3],3{0}} (TRM p168), so it must be 8-byte
 *     aligned; 8 bytes per block keeps it 8-byte aligned for the whole row after the entry block.
 *   - No instruction here is in an ISR: PIE is coprocessor 3 and is saved/restored on task switch, not
 *     usable in an interrupt handler (docs/pie-simd.md section 1).
 *   - CONSTANTS come from a table in this file, not from the caller: all three signatures are fixed by
 *     the request and none of them has room for a constant pointer, so each kernel walks its own 16-bit
 *     table with EE.VLDBC.16.IP from a literal address and REWINDS the walk pointer once per block
 *     (`mov a8, a7`), the same shape garden.c uses for its k[40] (garden.c:156-165). The tables are in
 *     .iram1 with the code, so the l32r literal stays in IRAM; ex08_media.S's comment (ex08_media.S:35-36)
 *     is about an l32r from .iram1 into .rodata, which is a different and broken case.
 *   - The constants are compile-time and identical in every lane; per-pixel values arrive in lanes.
 *
 * EE.VMUL.U16 SEMANTICS, because everything here depends on it. The TRM's printed Operation (p204) is
 * `qz[15:0] = (qx[15:0]*qy[15:0]) >> SAR[5:0]`, which two readings: (A) the 32-bit product shifted right
 * and truncated to 16 bits, or (B) the low 16 bits of the product shifted right. THIS FILE NEEDS (A) and
 * (A) IS THE ONE THE SILICON DOES -- ex08_tint with tint=300, SAR=8, on the device, 45/45 green:
 * a=0x7530 (=30000), out=0x8954 (=35156) = 30000*300>>8, while (B) would give (9000000 mod 65536)>>8 = 84.
 * Evidence: backups/pie-examples-20260914T183007Z.log line 162 (CHECK tint_matches_the_truncating_
 * reference pie=1 ref=1 ok) and the host model at tools/check_examples_log.py:364
 * (`as_i16(((x * 300) >> 8) & 0xFFFF)`). Every product in this file is also kept below 2^16 where the
 * arithmetic allows it, so kernels A and B are insensitive to the reading for those steps and (A) is only
 * load-bearing for the two placement multiplies (<<7 then <<4), which are called out in the .md.
 *
 * ---------------------------------------------------------------------------------------------------
 * TRM PAGES (source_page in data/pie_instructions.json; the section numbers are TRM 1.8.x)
 * ---------------------------------------------------------------------------------------------------
 *   EE.VLD.128.IP   p164  section 1.8.88     EE.VST.128.IP   p275  1.8.192
 *   EE.VLD.L.64.IP  p168  section 1.8.92     EE.VLDBC.16.IP  p171  1.8.95
 *   EE.VMUL.U16     p204  section 1.8.128    EE.VADDS.S16    p146  1.8.70
 *   EE.VSUBS.S16    p281  section 1.8.198    EE.VMIN.S16     p189  1.8.113
 *   EE.VMAX.S16     p180  section 1.8.104    EE.ANDQ         p76   1.8.1
 *   EE.ORQ          p121  section 1.8.45     EE.XORQ         p297  1.8.214
 *   EE.VZIP.16      p293  section 1.8.212    EE.VCMP.LT.S16  p161  1.8.85
 * (EE.VMUL.S16, p198/1.8.122, is deliberately NOT used anywhere in this file: every lane it multiplies
 * has bit 15 set (packed RGB565) or is a small non-negative Q8 value, so the arithmetic shift the signed
 * form would apply is either wrong (packed words: ex08's first device run was exactly this bug) or
 * unnecessary. One multiply rule, unsigned, for the whole file.)
 */
    .section .iram1,"ax",@progbits

/* ---------------------------------------------------------------------------------------------------
 * A) void ex22_decor_blend(int16_t *fb, const uint8_t *coverage, const int16_t *tint8,
 *                          uint32_t n_px, uint32_t shift)
 *
 *   a2 = fb (16-byte aligned)   a3 = coverage (8-byte aligned)   a4 = tint8 (16-byte aligned)
 *   a5 = n_px (multiple of 8)   a6 = shift (the Q8 exponent; THE CONTRACT IS 8)
 *
 * Why this instruction sequence and not another (the question the .md has to answer):
 *
 *   * THE PACKED WORD CANNOT BE MULTIPLIED IN PLACE. The obvious kernel is one EE.VMUL.U16 of the whole
 *     RGB565 lane by att, which is what "attenuate the packed word" means arithmetically; it cannot be
 *     written, because (p & 0xF800) alone is 63488 and 63488*256 = 16.2e6, and the R field's product
 *     lands 8 bits above what the in-place reading of the result needs. So the three channels are MASKED
 *     APART, scaled in the low bits, and placed back with two multiplies each where the field sits above
 *     bit 7. That is channel separation, not a whole-word approximation, and it is forced by the widths.
 *   * EE.VMUL.U16 IS EVERY SHIFT. There is no 16-bit lane shift instruction (docs/pie-simd.md section 2),
 *     so >>8 is multiply-by-1, >>3 is multiply-by-32, <<4 is multiply-by-4096, <<7 by 32768. Every
 *     product below is bounded, so none of them wraps (except the deliberate r<<8 step, whose result is
 *     < 2^16 by construction).
 *   * VSUBS.S16 FOR att = 256 - c, AND ONLY THERE. It is the only 16-bit subtract and [1,256] saturates
 *     nowhere. It is not used to bring a field down: that is a multiply here.
 *   * VADDS.S16 CANNOT ADD TO A PACKED RGB565 WORD. VADDS.S16 is a SIGNED saturating add, so a lane whose
 *     bit 15 is set (R >= 16, i.e. most of a real frame) is read as negative: 0xFFFF + 1 saturates to
 *     0x7FFF, which is a huge wrong colour, not a carry. The bias is therefore added PER CHANNEL, in the
 *     low bits, before the fields are placed -- where every value is < 128 and VADDS can never saturate.
 *     This is the whole reason the kernel is not three multiplies.
 *   * VMIN.S16 FOR THE FIELD CLAMPS, matching the scalar path's `if(r>31)r=31` (garden.c:1411-1413) and
 *     making the packed OR that follows safe by construction: after the clamp each field is inside its
 *     own bits, so OR == add and no carry can cross a field boundary.
 */
    .align 4
    .literal_position
    .literal .Lk_blend_adr, .Lk_blend
    .global ex22_decor_blend
    .type ex22_decor_blend,@function
ex22_decor_blend:
    entry a1, 32
    ssr a6                          /* SAR = shift. The contract is 8 (see the header). */
    mov a6, a2                      /* a6 held `shift` and is free now: THE STORE POINTER. The read
                                       pointer a2 walks itself past each store target with EE.VLD.128.IP,
                                       so the store cannot use a2 (the first block would be written one
                                       block late, into the next block's data). */
    l32r a7, .Lk_blend_adr          /* the address of the broadcast constant table */
    srli a5, a5, 3                  /* n_px -> blocks of eight */
    beqz a5, .Lbl_done
.Lbl_top:
    mov a8, a7                      /* rewind the constant walk */
    EE.VLD.128.IP q0, a2, 16        /* p: eight packed RGB565 pixels (read-modify-write, in place) */
    EE.VLD.L.64.IP q1, a3, 8        /* eight coverage bytes, as four 16-bit lanes */
    EE.VLD.128.IP q2, a4, 16        /* eight tint lanes, each 0..255 */
    /* coverage bytes -> one byte per lane. The odd bytes are the lane's high half: >>8 by multiply-by-1,
       the even bytes are the lane's low half: & 0x00FF, then EE.VZIP.16 interleaves the two halves back
       into pixel order (TRM p293: qs0 = {qs0[0],qs1[0],qs0[1],qs1[1],qs0[2],qs1[2],qs0[3],qs1[3]}). */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 1 ] */
    EE.VMUL.U16 q6, q1, q7          /* odd bytes: c1,c3,c5,c7 */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 0x00FF ] */
    EE.ANDQ q1, q1, q7              /* even bytes: c0,c2,c4,c6 */
    EE.VZIP.16 q1, q6               /* q1 = c0..c7, one per lane */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 256 ] */
    EE.VSUBS.S16 q1, q7, q1         /* att = 256 - c */
    /* ---- R: field bits 15:11, clamped to 5 bits, placed at <<11 (two multiplies, the limit is <<7) --- */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 0xF800 ] */
    EE.ANDQ q3, q0, q7              /* r<<11 */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 32 ] */
    EE.VMUL.U16 q3, q3, q7          /* (r*2^11)*32>>8 = r<<8: still exact, still < 2^16 */
    EE.VMUL.U16 q3, q3, q1          /* r*att <= 31*256, no wrap */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 1 ] */
    EE.VMUL.U16 q3, q3, q7          /* floor(r*att/256) */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 32 ] */
    EE.VMUL.U16 q5, q2, q7          /* warm bias br = t>>3 */
    EE.VADDS.S16 q3, q3, q5         /* <= 62: VADDS cannot saturate here */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 31 ] */
    EE.VMIN.S16 q3, q3, q7          /* clamp to the 5-bit field */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 32768 ] */
    EE.VMUL.U16 q3, q3, q7          /* <<7 */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 4096 ] */
    EE.VMUL.U16 q3, q3, q7          /* <<4: placed, <= 63488 */
    /* ---- G: field bits 10:5, clamped to 6 bits, placed at <<5 (one multiply) ----------------------- */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 0x07E0 ] */
    EE.ANDQ q4, q0, q7              /* g<<5 */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 8 ] */
    EE.VMUL.U16 q4, q4, q7          /* (g*32)*8>>8 = g */
    EE.VMUL.U16 q4, q4, q1          /* floor(g*att/256) */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 16 ] */
    EE.VMUL.U16 q5, q2, q7          /* bg = t>>4 */
    EE.VADDS.S16 q4, q4, q5         /* <= 78 */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 63 ] */
    EE.VMIN.S16 q4, q4, q7
    EE.VLDBC.16.IP q7, a8, 2        /* [ 8192 ] */
    EE.VMUL.U16 q4, q4, q7          /* <<5: placed, <= 2016 */
    /* ---- B: field bits 4:0, already in the low bits, placed by doing nothing ------------------------ */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 0x001F ] */
    EE.ANDQ q5, q0, q7
    EE.VMUL.U16 q5, q5, q1          /* floor(b*att/256) <= 31 */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 8 ] */
    EE.VMUL.U16 q6, q2, q7          /* bb = t>>5 */
    EE.VADDS.S16 q5, q5, q6         /* <= 38 */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 31 ] */
    EE.VMIN.S16 q5, q5, q7
    /* ---- pack: OR because every field is inside its own bits after the clamps ---------------------- */
    EE.ORQ q3, q3, q4
    EE.ORQ q3, q3, q5
    EE.VST.128.IP q3, a6, 16        /* the row is written in place, through the store pointer */
    addi a5, a5, -1
    bnez a5, .Lbl_top
.Lbl_done:
    retw.n
    .size ex22_decor_blend, .-ex22_decor_blend

/* ---------------------------------------------------------------------------------------------------
 * B) void ex22_decor_coverage(int16_t *cov_out, const int16_t *ix, uint32_t n_px, uint32_t shift)
 *
 *   a2 = cov_out (16-byte aligned)  a3 = ix (16-byte aligned)  a4 = n_px (multiple of 8)  a5 = shift
 *
 * cov = min(255, 256 - ((|ix|*|ix|)>>8)*4), evaluated for eight positions at once. The absolute value
 * has to be built: PIE has neither ABS nor SAD (docs/pie-simd.md section 8 and ex10's SAD kernel), so
 * |ix| = VMAX.S16(ix, VSUBS.S16(0,ix)) -- two instructions, no branch, and the double width is why the
 * whole profile stays in lanes. The parabola replaces garden.c's garden_decor_profile (garden.c:1397-1402:
 * a reciprocal, a clamp and garden_smooth's cubic, garden.c:52) with two multiplies; the normalisation
 * that the scalar code gets from the reciprocal is folded into the caller's ramp slope instead, so no
 * per-row uniform is needed inside the loop and the constant table is compile-time only.
 */
    .align 4
    .literal_position
    .literal .Lk_cov_adr, .Lk_cov
    .global ex22_decor_coverage
    .type ex22_decor_coverage,@function
ex22_decor_coverage:
    entry a1, 32
    l32r a7, .Lk_cov_adr
    srli a4, a4, 3
    beqz a4, .Lcv_done
.Lcv_top:
    mov a8, a7
    EE.VLD.128.IP q0, a3, 16        /* ix: signed, |ix| <= 2048 by contract */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 0 ] */
    EE.VSUBS.S16 q1, q7, q0         /* -ix */
    EE.VMAX.S16 q1, q0, q1          /* u = |ix| (PIE has no ABS: ex10's SAD kernel does the same) */
    EE.ZERO.QACC
    EE.VMULAS.U16.QACC q1, q1       /* QACC = u^2 exactly (<= 4.2e6: 40 bits, no wrap) */
    EE.SRCMB.S16.QACC q2, a5, 0     /* q2 = floor(u^2 / 2^shift), shift = 14 by contract: 256*xi^2 */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 256 ] */
    EE.VSUBS.S16 q2, q7, q2         /* cov = 256 - 256*xi^2: zero slope at axis and edge */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 255 ] */
    EE.VMIN.S16 q2, q2, q7          /* the axis pixel lands on 256; clip to the byte range */
    EE.VST.128.IP q2, a2, 16
    addi a4, a4, -1
    bnez a4, .Lcv_top
.Lcv_done:
    retw.n
    .size ex22_decor_coverage, .-ex22_decor_coverage

/* ---------------------------------------------------------------------------------------------------
 * C) uint32_t ex22_decor_rowmask(const int16_t *cov, const int16_t *protect, uint32_t n_px)
 *
 *   a2 = cov   a3 = protect   a4 = n_px   ->  a2 = bitmask (bit b: block b has a live lane)
 *
 * PIE HAS NO REDUCTION ACROSS LANES AND NO WAY TO READ ONE LANE INTO AN AR REGISTER here (no
 * EE.RUR.* / EE.SRS.* in this file's instruction set, and docs/pie-simd.md section 1: there is no
 * distribute-one-lane instruction either). So the per-block decision is materialised by the one
 * mechanism that is always available -- a 128-bit store to the stack frame -- and the two 32-bit halves
 * are ORed by the scalar side. That is 5 scalar instructions per block against the ~110 per pixel the
 * block's eight blend pixels would have cost; the per-pixel branch is gone, the per-block one is not.
 * The thresholds are contract constants: cov >= 8 (3% of the parabola's peak) and protect >= 32 (the
 * 24-pixel protection ramp of garden.c:1471, so 3 pixels inside the main beam).
 */
    .align 4
    .literal_position
    .literal .Lk_mask_adr, .Lk_mask
    .global ex22_decor_rowmask
    .type ex22_decor_rowmask,@function
ex22_decor_rowmask:
    entry a1, 96                    /* 16 of the 64 bytes are the lane materialisation scratch */
    /* ABI: this kernel writes a10, a11, a12 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 64
    s32i a11, a1, 68
    s32i a12, a1, 72
    l32r a6, .Lk_mask_adr
    srli a4, a4, 3                  /* n_px -> blocks of eight (<= 31 blocks) */
    movi a7, 0                      /* the mask being built */
    beqz a4, .Lrm_done
    movi a5, 1                      /* this block's bit, rolled left once per block */
.Lrm_top:
    mov a8, a6
    EE.VLD.128.IP q0, a2, 16        /* eight coverage values */
    EE.VLD.128.IP q1, a3, 16        /* eight protection values */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 8 ] the coverage threshold */
    EE.VCMP.LT.S16 q2, q0, q7       /* 0xFFFF where cov is too small to matter */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 32 ] the protection threshold */
    EE.VCMP.LT.S16 q3, q1, q7       /* 0xFFFF where the pixel is inside the protected core */
    EE.ORQ q2, q2, q3               /* 0xFFFF where the pixel is dead either way */
    EE.VLDBC.16.IP q7, a8, 2        /* [ 0xFFFF ] */
    EE.XORQ q2, q2, q7              /* 0xFFFF where the pixel survives */
    EE.VST.128.IP q2, a1, 0         /* materialise the eight lanes (imm 0: a1 is not moved) */
    l32i a9, a1, 0                  /* eight 16-bit lanes = FOUR 32-bit words; all four have to be */
    l32i a10, a1, 4                 /* ORed, or a live lane in an unread word disappears (this was a */
    l32i a11, a1, 8                 /* real bug in the first draft of this kernel: two words looked */
    l32i a12, a1, 12                /* sufficient and were not) */
    or a9, a9, a10
    or a11, a11, a12
    or a9, a9, a11
    beqz a9, .Lrm_next              /* whole block dead: its bit stays 0 */
    or a7, a7, a5
.Lrm_next:
    slli a5, a5, 1
    addi a4, a4, -1
    bnez a4, .Lrm_top
.Lrm_done:
    mov a2, a7                      /* the return value */
    /* ABI: put the caller's a2/a3/a4 back before returning. */
    l32i a10, a1, 64
    l32i a11, a1, 68
    l32i a12, a1, 72
    retw.n
    .size ex22_decor_rowmask, .-ex22_decor_rowmask

/* ---------------------------------------------------------------------------------------------------
 * The constant tables. 16-bit entries, walked in issue order by EE.VLDBC.16.IP (which broadcasts the
 * halfword at {as[31:1],1{0}} to all eight lanes, TRM p171) and rewound once per block. The walk order
 * below IS the order of the loads in the kernels above; a load that is moved without moving its entry
 * silently changes a constant, and nothing will fail to assemble.
 * --------------------------------------------------------------------------------------------------- */
    .align 2
.Lk_blend:                          /* 18 entries, one per EE.VLDBC.16.IP in kernel A */
    .short 1                            /* 1)   >>8 for the coverage byte split        */
    .short 0x00FF                       /* 2)   the low half of a coverage byte pair    */
    .short 256                          /* 3)   att = 256 - c                          */
    .short 0xF800                       /* 4)   the R field of RGB565                  */
    .short 32                           /* 5)   (r<<11) -> r<<8                        */
    .short 1                            /* 6)   r*att -> floor(r*att/256)              */
    .short 32                           /* 7)   br = t>>3                              */
    .short 31                           /* 8)   the R clamp                            */
    .short 0x8000                       /* 9)   <<7                                    */
    .short 4096                         /* 10)  <<4 (<<11 = <<7 then <<4: 2^19 does not fit a lane) */
    .short 0x07E0                       /* 11)  the G field of RGB565                  */
    .short 8                            /* 12)  (g<<5) -> g                            */
    .short 16                           /* 13)  bg = t>>4                              */
    .short 63                           /* 14)  the G clamp                            */
    .short 8192                         /* 15)  <<5                                    */
    .short 0x001F                       /* 16)  the B field of RGB565                  */
    .short 8                            /* 17)  bb = t>>5                              */
    .short 31                           /* 18)  the B clamp                            */
    .align 2
.Lk_cov:                            /* 3 entries, kernel B */
    .short 0                            /* 1) the zero that builds -ix */
    .short 256                          /* 2) the parabola's peak */
    .short 255                          /* 3) the byte clip */
    .align 2
.Lk_mask:                           /* 3 entries, kernel C */
    .short 8                            /* 1) the coverage threshold */
    .short 32                           /* 2) the protection threshold */
    .short 0xFFFF                       /* 3) all ones */
