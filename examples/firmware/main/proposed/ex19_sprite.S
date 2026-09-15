/* ex19 -- sprite compositing: a blit is a mask plus a select, and the select is branchless.
 *
 * notes/08-media-3d-perf.md names this stage as unbuilt:
 *   "| スプライト合成 | マスク（VCMP + ANDQ/ORQ）＋上記ブレンド | 未着手 |" (notes/08 line 65)
 *   "| ex19 | 2D | スプライト合成（マスク付き不透明 / 半透明 / カラーキーを VCMP + ANDQ/ORQ/NOTQ で分岐レスに）|"
 *                                                                        (notes/08 line 102)
 * so this file is the three kernels that line asks for, in the form the production list asks for.
 *
 * THE ONE DECISION THAT SHAPES ALL THREE KERNELS: the mask is a LANE mask, not a bit mask.
 *   mask is `const uint16_t *`, one 16-bit lane per pixel, value 0x0000 (transparent) or 0xFFFF (opaque),
 *   indexed exactly like dst and src: mask[i] is the 16-bit word at byte offset 2*i, lane i of the 128-bit
 *   register. Reasons, and the alternative's price, are in ex19_sprite.md section 1; the short version is
 *   that 0xFFFF/0x0000-per-lane is what EE.VCMP.*.S16 produces (TRM p155-163), what the suite's own clip
 *   kernel ex12_sat_masks produces, and what a 128-bit load already puts in the right lanes -- a 1-bit/pixel
 *   mask would need a bit->lane expansion step this instruction set has no single instruction for.
 *
 * HOW THE SELECT IS WRITTEN (this is the whole trick, and it is three ops):
 *
 *   out = dst ^ ((dst ^ src) & mask)
 *
 *   mask = 0xFFFF -> out = dst ^ (dst ^ src) = src            (opaque: the sprite replaces the background)
 *   mask = 0x0000 -> out = dst ^ 0           = dst            (transparent: the background is kept bit-exactly)
 *
 * Two EE.XORQ and one EE.ANDQ per register, no branch, and the transparent lanes come back as the ORIGINAL
 * bits (not "approximately what was there"): there is no arithmetic on the dst path at all, so a composited
 * sprite can be recomposited over itself any number of times without drift. The 4-op spelling of the same
 * select is `(src & mask) | (dst & ~mask)` -- one EE.NOTQ + two EE.ANDQ + one EE.ORQ -- which is why the
 * production list mentions NOTQ/ORQ; the XOR form is used here because it is one op cheaper and because
 * per-bit (rather than per-lane) masking falls out of it for free, which is stated in the .md's contract.
 *
 * A note the .md develops: any mask lane that is not 0x0000/0xFFFF does not "break" the kernel, it degrades
 * into a BITWISE merge chosen per bit of the lane -- the same thing the scalar tail computes, because the
 * tail is the same expression. That is why the check can feed it arbitrary masks and still demand equality.
 *
 * THE FRAME BUDGET this is written against (notes/08): a 135x240 RGB565 frame is 129.6 KB and the SPI
 * transfer floor is 13-16 ms at 80 MHz, so 30 fps leaves roughly 67 cycles per pixel for ALL CPU work.
 * Per eight pixels: kernel A is 7 PIE ops, kernel C is 8, kernel B is 11 + 2 scalar (and the half-blend
 * itself is ex08's 5-op RGB565 mask-shift-add, kept bit-identical). Instruction counts, not cycles: the
 * pipeline tables (data/pie_pipeline.json) have no entry that would let a cycle count be claimed here.
 *
 * NO l32r, NO .rodata. A .iram1 kernel cannot l32r into .rodata (the linker refuses with "dangerous
 * relocation: l32r: literal target out of range"), so every constant is either a caller-supplied pointer
 * (src, mask, ones8) or built in a register with movi/slli/or and read back with a broadcast load from the
 * function's own stack frame. That is the same pattern ex12_physics.S uses for lo/hi.
 *
 * THIS FILE IS PROPOSED. It is not in examples/firmware/main/CMakeLists.txt, it is not declared in
 * examples.h, and main.c does not call it: nothing in the running firmware changes. It has NOT run on
 * silicon -- no hardware was used for anything in this file or in ex19_sprite.md next to it. What was run:
 * the real assembler, an instruction-level Python model of these exact sequences (including the instruction
 * interpreter in /workspace/pjs-vm/tools/pie/piesim.py, extended for the instructions it did not carry),
 * and a host-compiled scalar C reference -- all three compared on the same cases, 0 mismatches.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex19_sprite_opaque(int16_t *dst, const int16_t *src, const uint16_t *mask, uint32_t n_px)
 *
 *   a2 = dst    in/out, 16-BYTE ALIGNED (contract). The 128-bit store forms {a2[31:4],4{0}} (TRM p49,
 *               figure 1.7-1): the low four address bits are DROPPED, not honored, so a dst off the grid
 *               would write the 16 bytes below it and silently corrupt the previous chunk.
 *   a3 = src    read-only, 16-byte aligned (same reason, on the load side)
 *   a4 = mask   read-only, 16-byte aligned, one uint16 lane per pixel: 0xFFFF opaque / 0x0000 transparent
 *   a5 = n_px   pixel count; 0 is legal and touches nothing
 *
 * Post-condition: for every i in [0, n_px), the 16-bit word of dst at byte offset 2*i is
 *     (uint16_t)dst[i] ^ (((uint16_t)dst[i] ^ (uint16_t)src[i]) & mask[i])
 * and NO byte of dst outside those 2*n_px bytes changes. The whole groups of eight are written with one
 * 128-bit store per eight pixels; the n_px & 7 remainder is written as single 16-bit words by the scalar
 * tail below, which never issues a 128-bit access -- see the .md's "範囲外を書かない" section.
 *
 * dst is loaded with immediate 0 (no advance) and stored with immediate 16: the .IP post-increment fires
 * after the store's address has been formed, so ONE address register serves both the load and the store of
 * the chunk the loop is working on, and walks forward exactly once per iteration. */
    .align 4
    .global ex19_sprite_opaque
    .type ex19_sprite_opaque,@function
ex19_sprite_opaque:
    entry a1, 64
    /* ABI: this kernel writes a10, a11 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    srli a8, a5, 3                  /* whole groups of eight */
    beqz a8, .Lop_tail
.Lop_loop:
    EE.VLD.128.IP q0, a2, 0         /* dst: imm 0, the store's .IP walks a2 */
    EE.VLD.128.IP q1, a3, 16        /* src */
    EE.VLD.128.IP q2, a4, 16        /* mask */
    EE.XORQ q3, q0, q1              /* dst ^ src */
    EE.ANDQ q3, q3, q2              /* ... & mask */
    EE.XORQ q3, q0, q3              /* dst ^ (...) == (mask ? src : dst), bitwise */
    EE.VST.128.IP q3, a2, 16        /* 16-byte store at the chunk just loaded, then a2 += 16 */
    addi a8, a8, -1
    bnez a8, .Lop_loop
.Lop_tail:
    extui a8, a5, 0, 3              /* n_px & 7 -- the pixels the vector loop did not cover */
    beqz a8, .Lop_done
.Lop_tail_loop:
    l16ui a11, a2, 0                /* dst word */
    l16ui a10, a3, 0                /* src word */
    xor a10, a11, a10               /* the same expression as the loop, on one pixel */
    l16ui a9, a4, 0                 /* mask word */
    and a10, a10, a9
    xor a10, a11, a10
    s16i a10, a2, 0                 /* single 16-bit store: the only write outside the 16-byte grid */
    addi a2, a2, 2
    addi a3, a3, 2
    addi a4, a4, 2
    addi a8, a8, -1
    bnez a8, .Lop_tail_loop
.Lop_done:
    /* ABI: put the caller's a2/a3 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    retw.n
    .size ex19_sprite_opaque, .-ex19_sprite_opaque

/* void ex19_sprite_blend(int16_t *dst, const int16_t *src, const uint16_t *mask, const int16_t *ones8,
 *                        uint32_t n_px)
 *
 *   a2 = dst     in/out, 16-byte aligned
 *   a3 = src     read-only, 16-byte aligned
 *   a4 = mask    read-only, 16-byte aligned, 0xFFFF/0x0000 lanes -- "blend here" / "keep dst here"
 *   a5 = ones8   read-only, 16-byte aligned, eight identical lanes of 1. With SAR = 1,
 *                EE.VMUL.U16(q, ones) is a per-lane LOGICAL >>1 -- there is no SRAQ in the set, and the
 *                UNSIGNED form is the one the RGB565 mask-shift-add needs (finding
 *                vmul_s16_is_arithmetic_and_vmul_u16_is_logical: EE.VMUL.S16 would sign-extend a lane
 *                whose bit 15 is set). The caller keeps the constant in C, not in .rodata: an l32r from
 *                .iram1 into .rodata does not link.
 *   a6 = n_px    pixel count; 0 is legal and touches nothing
 *
 * Post-condition: where mask[i] == 0xFFFF the output is ex08_half_blend's value for that lane,
 *     hb = sat16((((uint16_t)src[i] & 0xF7DE) >> 1) + (((uint16_t)dst[i] & 0xF7DE) >> 1))
 * where mask[i] == 0x0000 the output is dst[i] bit-for-bit, and nothing outside the n_px pixels is touched.
 * The blend is EXACTLY ex08's five-op RGB565 half-blend (EE.ANDQ with the 0xF7DE lane mask, EE.VMUL.U16 by
 * ones with SAR = 1, EE.VADDS.S16), so a half-transparent sprite and a full-row ghost blend of the same two
 * rows cannot disagree by one LSB. ex19_sprite.md section 3.2 states where that saturating add is exact and
 * where it clamps, with the boundary as a number.
 *
 * 0xF7DE IS NOT A PARAMETER OF THIS FUNCTION, so it is built in a register and bounced through this
 * function's own frame: movi(12-bit immediate) -> slli -> or gives 0xF7DEF7DE, s32i puts it in the frame at
 * a1+0, and EE.VLDBC.32 broadcasts the 32-bit word to all eight 16-bit lanes in one instruction (TRM p173).
 * The same two scalar constants the tail needs (0xF7DE and the 0x7FFF ceiling of the saturating add) are
 * parked in the frame's upper half and read back with l16ui. Seven instructions, once per call, no l32r,
 * no .rodata.
 *
 * SCHEDULING (TRM table 1.7-2, data/pie_pipeline.json: EE.VLD.128.IP and EE.VMUL.* write at stage 2 and
 * read at stage 1, so a consumer placed immediately after its producer stalls one cycle, D = max(SA-SB+1,0)
 * = 2). The body below is ordered so every stage-2 producer is at least two instructions away from its
 * consumer: the mask load sits early but is consumed late, and the loop's own `addi` -- which depends on
 * nothing in the vector chain -- is placed inside the multiply chain to fill the one gap. */
    .align 4
    .global ex19_sprite_blend
    .type ex19_sprite_blend,@function
ex19_sprite_blend:
    entry a1, 64
    /* ABI: this kernel writes a10, a11, a12 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    s32i a12, a1, 40
    movi a9, 0xF7
    slli a9, a9, 8
    movi a10, 0xDE
    or a9, a9, a10                  /* a9 = 0x0000F7DE */
    s16i a9, a1, 4                  /* frame+4: the 0xF7DE lane mask, for the scalar tail */
    slli a10, a9, 16
    or a9, a9, a10                  /* a9 = 0xF7DEF7DE */
    s32i a9, a1, 0                  /* frame+0: the 128-bit broadcast source */
    EE.VLDBC.32 q6, a1              /* q6 = {8{0xF7DE}} = 0xF7DEF7DE |
                                       the RGB565 field-LSB mask of ex08_half_blend */
    movi a9, -1
    extui a9, a9, 0, 15             /* a9 = 0x7FFF: the ceiling EE.VADDS.S16 saturates at */
    s16i a9, a1, 6                  /* frame+6, for the scalar tail */
    EE.VLD.128.IP q7, a5, 0         /* q7 = {8{1}} -- the caller's ones8 */
    movi a9, 1
    ssr a9                          /* SAR = 1: EE.VMUL.U16(q, q7) is a per-lane >>1 */
    srli a8, a6, 3
    beqz a8, .Lbl_tail
.Lbl_loop:
    EE.VLD.128.IP q0, a2, 0         /* dst */
    EE.VLD.128.IP q1, a3, 16        /* src */
    EE.VLD.128.IP q2, a4, 16        /* mask (consumed at op 10, deliberately far from this load) */
    EE.ANDQ q3, q1, q6              /* src & 0xF7DE */
    EE.ANDQ q4, q0, q6              /* dst & 0xF7DE */
    EE.VMUL.U16 q3, q3, q7          /* (src & 0xF7DE) >> 1, logical */
    EE.VMUL.U16 q4, q4, q7          /* (dst & 0xF7DE) >> 1, logical */
    addi a8, a8, -1                 /* independent of the vector chain: fills the VMUL->VADDS gap */
    EE.VADDS.S16 q3, q3, q4         /* hb = sat16(half(src) + half(dst)) */
    EE.XORQ q4, q0, q3              /* dst ^ hb */
    EE.ANDQ q4, q4, q2              /* ... & mask */
    EE.XORQ q4, q0, q4              /* dst ^ (...) == (mask ? hb : dst), bitwise */
    EE.VST.128.IP q4, a2, 16
    bnez a8, .Lbl_loop
.Lbl_tail:
    extui a8, a6, 0, 3
    beqz a8, .Lbl_done
.Lbl_tail_loop:
    l16ui a9, a4, 0                 /* mask word */
    l16ui a10, a3, 0                /* src word */
    l16ui a11, a2, 0                /* dst word */
    l16ui a12, a1, 4                /* 0xF7DE */
    and a10, a10, a12
    srli a10, a10, 1                /* half(src) */
    and a11, a11, a12
    srli a11, a11, 1                /* half(dst) */
    add a10, a10, a11               /* both terms are <= 0x7BEF, so the 32-bit add cannot overflow */
    l16ui a12, a1, 6                /* 0x7FFF */
    minu a10, a10, a12              /* the same clamp EE.VADDS.S16 applies (both terms are non-negative) */
    l16ui a11, a2, 0                /* dst again (a11 was consumed by the halving) */
    xor a10, a10, a11               /* dst ^ hb */
    and a10, a10, a9                /* ... & mask */
    xor a10, a10, a11               /* dst ^ (...) */
    s16i a10, a2, 0
    addi a2, a2, 2
    addi a3, a3, 2
    addi a4, a4, 2
    addi a8, a8, -1
    bnez a8, .Lbl_tail_loop
.Lbl_done:
    /* ABI: put the caller's a2/a3/a4 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    retw.n
    .size ex19_sprite_blend, .-ex19_sprite_blend

/* void ex19_sprite_colorkey(int16_t *dst, const int16_t *src, int16_t key, uint32_t n_px)
 *
 *   a2 = dst    in/out, 16-byte aligned
 *   a3 = src    read-only, 16-byte aligned
 *   a4 = key    the transparent colour, one int16 lane value to compare against every pixel of src
 *   a5 = n_px   pixel count; 0 is legal and touches nothing
 *
 * Post-condition: dst[i] = (src[i] == key) ? dst[i] : src[i], bit-for-bit, for every i in [0, n_px), and
 * nothing outside those pixels is touched. The mask is not an input here: it is SYNTHESISED, per lane, in
 * the register file, so the caller needs no side buffer at all --
 *
 *   EE.VCMP.EQ.S16 q2, q0, q7   -> q2 = (src == key) ? 0xFFFF : 0   (TRM p155)
 *   EE.NOTQ       q2, q2        -> q2 = (src != key) ? 0xFFFF : 0   (TRM p120), the mask of kernel A
 *
 * and then the same three-op select as kernel A. Equality between two 16-bit lanes is a bit-pattern
 * comparison, so this mask does not depend on the signed/unsigned reading of the pixels at all -- but the
 * comparison instruction's own signedness is still an assumption worth naming (it is in the .md's
 * "未確認の前提": the suite has measured no EE.VCMP.* yet).
 *
 * The key arrives as a scalar in an AR, and there is no AR->QR move instruction, so it is spilled with
 * s16i into the frame `entry a1, 32` already allocated and read back with EE.VLDBC.16 (TRM p170), which
 * broadcasts the 16-bit word to all eight lanes -- ex12_physics.S uses the same pairing for lo/hi. */
    .align 4
    .global ex19_sprite_colorkey
    .type ex19_sprite_colorkey,@function
ex19_sprite_colorkey:
    entry a1, 64
    /* ABI: this kernel writes a10, a11, a12, a14 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a10, a1, 32
    s32i a11, a1, 36
    s32i a12, a1, 40
    s32i a14, a1, 44
    s16i a4, a1, 0                  /* the key, in the frame */
    EE.VLDBC.16 q7, a1              /* q7 = {8{key}} */
    srli a8, a5, 3
    beqz a8, .Lck_tail
.Lck_loop:
    EE.VLD.128.IP q0, a3, 16        /* src */
    EE.VLD.128.IP q1, a2, 0         /* dst: imm 0, the store's .IP walks a2 */
    EE.VCMP.EQ.S16 q2, q0, q7       /* 0xFFFF where src == key */
    EE.NOTQ q2, q2                  /* 0xFFFF where src != key: the opaque mask */
    EE.XORQ q3, q1, q0              /* dst ^ src */
    EE.ANDQ q3, q3, q2              /* ... & mask */
    EE.XORQ q3, q1, q3              /* dst ^ (...) == (opaque ? src : dst), bitwise */
    EE.VST.128.IP q3, a2, 16
    addi a8, a8, -1
    bnez a8, .Lck_loop
.Lck_tail:
    extui a8, a5, 0, 3
    beqz a8, .Lck_done
    l16ui a12, a1, 0                /* key, zero-extended: the mask below is a 16-bit comparison */
    movi a14, 1
.Lck_tail_loop:
    l16ui a11, a2, 0                /* dst word */
    l16ui a10, a3, 0                /* src word */
    xor a9, a10, a12                /* src ^ key: 0 iff the 16-bit patterns are equal */
    minu a9, a9, a14                /* collapse any nonzero to 1 */
    addi a9, a9, -1                 /* 1 -> 0x0000, 0 -> 0xFFFF: the KEEP mask (src == key) */
    xor a11, a11, a10               /* dst ^ src */
    and a11, a11, a9                /* (dst ^ src) & keep */
    xor a10, a10, a11               /* src ^ (...) -- the same select, carried on src instead of dst */
    s16i a10, a2, 0
    addi a2, a2, 2
    addi a3, a3, 2
    addi a8, a8, -1
    bnez a8, .Lck_tail_loop
.Lck_done:
    /* ABI: put the caller's a2/a3/a4/a6 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    l32i a14, a1, 44
    retw.n
    .size ex19_sprite_colorkey, .-ex19_sprite_colorkey
