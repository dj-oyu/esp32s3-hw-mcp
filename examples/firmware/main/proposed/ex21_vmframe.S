/* ex21 -- the QuickJS VM's frame-setup constant work, on PIE 128-bit registers.
 *
 * PROPOSED, and a WEAK proposal on purpose. This file is not in
 * examples/firmware/main/CMakeLists.txt, nothing calls any of it, and notes/cardputer-adv-project/vm-pie-fit.md already
 * established that the hottest VM path this suite can measure on the host (fib(30), bench_calls.js) has
 * var_count == 0 -- so kernel A's loop never runs there at all. What this file is for is the *shape* of
 * "if this ever pays, this is where it pays", plus the contract that has to hold before a single line of
 * it is correct. ex21_vmframe.md next to this file has the scalar C references, the instruction-level
 * Python model, the equivalence runs and the list of semantics that were NOT confirmed on silicon.
 *
 * There is no device run: /dev/ttyACM0 belongs to another agent (hard rule of this session).
 *
 * Facts this file is written against (all re-read from the sources, not remembered):
 *
 *   JSValue is uint64_t, NAN boxing is the 32-bit default (quickjs.h:155-157, :241),
 *     sizeof(JSValue) = 8 on the target.
 *   JS_UNDEFINED = JS_MKVAL(JS_TAG_UNDEFINED, 0) = (uint64_t)(3) << 32 | 0 = 0x0000_0003_0000_0000
 *     (quickjs.h:175 JS_TAG_UNDEFINED = 3, :249 JS_MKVAL, :408), so it is NOT a zero fill: the high
 *     32-bit word of every slot is 3 and the low word is 0. A two-slot constant is therefore the
 *     128-bit pattern  { 0, 3, 0, 3 }  across the four 32-bit segments.
 *   The frame block is [JSVMLink][JSStackFrame][slots][var_refs] (quickjs.c:18718-18721),
 *     local_buf = (JSValue *)(sf + 1) (quickjs.c:18736), var_buf = local_buf + arg_allocated_size
 *     (quickjs.c:18778), the slots are filled with JS_UNDEFINED (quickjs.c:18782-18784) and the
 *     var_refs with NULL (quickjs.c:18789-18791).
 *   sizeof(JSStackFrame) = 48, sizeof(JSVMLink) = 4, JS_VM_FRAME_ALIGN = 4 on the target
 *     (quickjs-vmstack.h:113); a segment's payload base is the only 16-byte-aligned thing in the
 *     picture (quickjs-vmstack.h:70-72, :110).
 *
 * THE FOUR KERNELS (all leaf functions on the windowed ABI: entry a1, 32 ... retw.n):
 *
 *   A  void ex21_fill_undefined(int16_t *var_buf, uint32_t n_values, const int16_t *undef_pair)
 *   A' void ex21_fill_undefined_inline(int16_t *var_buf, uint32_t n_values)        (same body)
 *      var_buf[0 .. n_values-1] = JS_UNDEFINED. One EE.VST.128.IP per TWO slots (16 bytes); an odd
 *      n_values ends with one EE.VST.L.64.IP (8 bytes, TRM p279). The C it replaces stores one 8-byte
 *      slot per iteration at 6..7 instructions per slot [obj, notes/cardputer-adv-project/vm-pie-fit.md §2.3].
 *   B  void ex21_fill_null(uint32_t *ptrs, uint32_t n_ptrs)
 *      ptrs[0 .. n_ptrs-1] = NULL: EE.ZERO.Q once, then one EE.VST.128.IP per FOUR pointers.
 *   C  uint32_t ex21_scan_first_nonzero(const uint32_t *p, uint32_t n_words)
 *      the index of the first non-zero word, or n_words when every word is zero. The shape a stack
 *      watermark or a free-list "first free slot" search wants. The set has no horizontal-min, no
 *      first-set-lane and no OR-reduction-across-lanes instruction, so the position is found by a
 *      branchless four-word `movnez` chain; the vector unit does the bulk skip and the counting.
 *   D  void ex21_copy_values(int16_t *dst, const int16_t *src, uint32_t n_values)
 *      128 bits per iteration, the odd trailing slot copied with EE.VLD.L.64.IP / EE.VST.L.64.IP.
 *
 * THE CONTRACT -- and this is the reason the file exists at all: every pointer passed here must be
 * 16-BYTE ALIGNED. A 128-bit access forms its address as {as[31:4], 4{0}}: the low four address bits are
 * DROPPED, not honoured (EE.VLD.128.IP TRM p164 / EE.VST.128.IP TRM p275 -- quoted verbatim in the .md),
 * and nothing in the ISA faults on it. ex03 reproduced exactly that on silicon
 * (data/pie_examples_measured.json, finding vld128_drops_the_low_address_bits: "the low four address bits
 * are dropped, not honoured, so an access off the 16-byte grid silently reads a different 16 bytes (and
 * does not fault)").
 *
 * The VM does NOT satisfy that contract today, and it cannot be repaired inside these kernels:
 *
 *   - var_buf = block + (JS_VM_FRAME_PREFIX + sizeof(JSStackFrame)) + 8*arg_allocated_size
 *             = block + 4 + 48 + 8*arg_allocated_size          (FLATCALLS build)
 *   - so var_buf % 16 == 4 (arg_allocated_size even) or 12 (odd)  -- never 0.
 *   - and 16 does NOT divide 52, so peeling whole 8-byte slots cannot reach the grid: from 4, adding
 *     8k stays in {4, 12}. The fix has to be in the allocator (align var_buf, or round
 *     JS_VM_FRAME_PREFIX + sizeof(JSStackFrame) and the block size up so that every frame's var_buf
 *     lands on 16). That is a change to quickjs.c / quickjs-vmstack.h, which this file does not make.
 *
 * Worked failure, var_buf % 16 == 4, n_values = 8 (one frame's 64 bytes of locals, V=8):
 *   the kernel writes 16 bytes at var_buf-4, var_buf+12, var_buf+28, var_buf+44:
 *     - the 4 bytes at var_buf-4..var_buf-1 -- the HIGH word of the last argument in arg_buf, i.e. the
 *       tag of a live JSValue -- are overwritten with the low word of JS_UNDEFINED (0),
 *     - the array's last 4 bytes (var_buf+60..63) are never written and keep whatever was there.
 *   No exception, no error code. A tag word of 0 turns a boxed value into JS_TAG_INT, i.e. it reads back
 *   as a different value; the effect surfaces far away from this call.
 *
 * The price of satisfying the contract, or of not: see ex21_vmframe.md §契約 (the trade is a few bytes
 * per frame against a silent 4-byte overrun, and the frame is 84..188 B on the device per
 * docs/vm-L2-design.md §13.2).
 *
 * Scheduling: the loops below are store-only (A, B) or load/store pairs (D). The only hazard with a
 * measured anchor in this set is a load's stage-M def consumed by a stage-E use at issue distance 1,
 * which costs 1.000 cycle (data/pie_timing_measured.json; Table 1.7-2 = data/pie_hazards.md). Kernel D's
 * unpaired group carries one such pair on purpose (see the comment there); the pair loop does not.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* ------------------------------------------------------------------------------------------------------------------
 * A: void ex21_fill_undefined(int16_t *var_buf, uint32_t n_values, const int16_t *undef_pair)
 *
 *   a2 = var_buf     n_values slots of 8 bytes, 16-BYTE ALIGNED
 *   a3 = n_values    the slot count (b->var_count at quickjs.c:18783)
 *   a4 = undef_pair  two JS_UNDEFINED values = 16 bytes, 16-BYTE ALIGNED. The constant arrives as a
 *                    POINTER (this suite's rule for constants; there is no l32r from .iram1 to .rodata).
 *                    A caller would hold `static const JSValue undef2[2] = { JS_UNDEFINED, JS_UNDEFINED };`
 *   A' = ex21_fill_undefined_inline(int16_t *var_buf, uint32_t n_values): the same fill with the constant
 *        built in two AR registers + four EE.MOVI.32.Q (TRM p119), for callers that have no such array.
 *        Cost: 6 instructions once per call instead of one EE.VLD.128.IP. That is the whole difference.
 *
 * Steps: one 16-byte group per iteration (2 slots) via EE.VST.128.IP with the pointer walking itself;
 * n_values odd -> one EE.VST.L.64.IP for the last slot. The odd slot is at 8*i with i even, so it is
 * 16-byte aligned too -- a 128-bit store there would be IN range and still wrong: it would write the 8
 * bytes after the array, which in a frame block are the first JSVarRef* (quickjs.c:18787), and the
 * kernel's job is to leave them (they get NULLed right after) alone. 8 bytes is the whole point.
 * ---------------------------------------------------------------------------------------------------------------- */
    .align 4
    .global ex21_fill_undefined
    .type ex21_fill_undefined,@function
ex21_fill_undefined:
    entry a1, 32
    beqz a3, .Lfu_done
    EE.VLD.128.IP q0, a4, 0         /* q0 = { low=0, high=3, low=0, high=3 }: two JS_UNDEFINED slots */
    j .Lfu_go
    .align 4
    .global ex21_fill_undefined_inline
    .type ex21_fill_undefined_inline,@function
ex21_fill_undefined_inline:
    entry a1, 32
    beqz a3, .Lfu_done
    movi a5, 0                      /* JS_VALUE_GET_INT(JS_UNDEFINED) = 0: the low word of a slot */
    movi a6, 3                      /* JS_VALUE_GET_TAG(JS_UNDEFINED) = 3: the high word (NAN boxing) */
    EE.MOVI.32.Q q0, a5, 0
    EE.MOVI.32.Q q0, a5, 2          /* segments 0 and 2 = the low words of the two slots */
    EE.MOVI.32.Q q0, a6, 1
    EE.MOVI.32.Q q0, a6, 3          /* segments 1 and 3 = the tag words */
.Lfu_go:
    extui a5, a3, 0, 1              /* the odd slot, if any */
    srli a4, a3, 1                  /* 16-byte groups = n_values >> 1 */
    beqz a4, .Lfu_odd
.Lfu_loop:
    EE.VST.128.IP q0, a2, 16        /* two slots per store; a2 += 16 */
    addi a4, a4, -1
    bnez a4, .Lfu_loop
.Lfu_odd:
    beqz a5, .Lfu_done
    EE.VST.L.64.IP q0, a2, 8        /* the last slot: q0[63:0] = { low=0, high=3 } (TRM p279).
                                     * The immediate is the WALK: 8 leaves a2 one past the slot, so the
                                     * caller can read "how many bytes were written" off the pointer for
                                     * every n_values -- 16 per group and 8 here (checked in the .md). */
.Lfu_done:
    retw.n
    .size ex21_fill_undefined, .-ex21_fill_undefined
    .size ex21_fill_undefined_inline, .Lfu_go-ex21_fill_undefined_inline

/* ------------------------------------------------------------------------------------------------------------------
 * B: void ex21_fill_null(uint32_t *ptrs, uint32_t n_ptrs)
 *
 *   a2 = ptrs     n_ptrs pointers (4 bytes each), 16-BYTE ALIGNED
 *   a3 = n_ptrs   the pointer count (b->var_ref_count at quickjs.c:18790)
 *
 * The slot type is a POINTER, not a JSValue: 4 bytes, so one EE.VST.128.IP covers four of them, and the
 * whole fill is one register that is zeroed once (EE.ZERO.Q, TRM p299). The tail (n_ptrs % 4 = 1..3) is
 * plain s32i: a 16-byte store at ptrs + 4*k for a k > 0 would write past the array into whatever follows
 * the var_refs in the frame block (nothing does -- var_refs is the last array of the block -- but it would
 * write past the END OF THE BLOCK, into the next frame or the segment's free space).
 * ---------------------------------------------------------------------------------------------------------------- */
    .align 4
    .global ex21_fill_null
    .type ex21_fill_null,@function
ex21_fill_null:
    entry a1, 32
    beqz a3, .Lfn_done
    EE.ZERO.Q q0                    /* q0 = 16 zero bytes: the only value this kernel ever stores */
    extui a5, a3, 0, 2              /* the 1..3 pointers that do not fill a group */
    srli a4, a3, 2                  /* 16-byte groups = n_ptrs >> 2 */
    beqz a4, .Lfn_tail
.Lfn_loop:
    EE.VST.128.IP q0, a2, 16        /* four NULL pointers per store; a2 += 16 */
    addi a4, a4, -1
    bnez a4, .Lfn_loop
.Lfn_tail:
    beqz a5, .Lfn_done
    movi a6, 0
.Lfn_tail_loop:
    s32i a6, a2, 0
    addi a2, a2, 4
    addi a5, a5, -1
    bnez a5, .Lfn_tail_loop
.Lfn_done:
    retw.n
    .size ex21_fill_null, .-ex21_fill_null

/* ------------------------------------------------------------------------------------------------------------------
 * C: uint32_t ex21_scan_first_nonzero(const uint32_t *p, uint32_t n_words)
 *
 *   a2 = p        16-BYTE ALIGNED, a3 = n_words
 *   returns the index of the first word != 0, or n_words when every word is zero. n_words == 0 returns 0
 *   (which is n_words): "not found" and "the array is empty" are the same answer by construction.
 *
 * CONTRACT DETAIL the caller has to follow: "not found" is reported AS n_words, one past the last valid
 * index, so the caller's test is `i = ex21_scan_first_nonzero(p, n); if (i == n) { ...not found... }`.
 * That value is not distinguishable from a hit at n-1 by magnitude, so the caller has to compare against
 * n rather than treat the result as a pure index. A caller that prefers a sentinel can turn it into
 * 0xFFFFFFFF with two scalar instructions (`if (i == n) i = ~0u;`).
 *
 * THREE PHASES, and why (the .md's §C works through the alternatives):
 *   1. 64 bytes (16 words) per iteration: four EE.VLD.128.IP folded with three EE.ORQ, then the folded
 *      register is tested for "all zero" by summing its eight unsigned 16-bit LANES into ACCX
 *      (EE.ZERO.ACCX + EE.VMULAS.U16.ACCX against a register whose u16 lanes are all 1 + EE.SRS.ACCX,
 *      TRM p298/p240/p134) and branching on the scalar result a7. The sum of non-negative lanes is zero
 *      iff every lane is zero, so this test needs no compare and no mask: the OR fold is what makes one
 *      test cover 16 words instead of 4.
 *   2. once phase 1 says the hit is inside the current 64 bytes, the same test per 16-byte group, but
 *      with EE.VCMP.EQ.S32 + EE.NOTQ + EE.ANDQ (TRM p156/p120/p76) in front of it, so that ACCX ends up
 *      holding THE NUMBER OF NON-ZERO WORDS in the group (0..4) instead of a boolean: the ANDQ leaves
 *      0x00000001 in each lane whose word is non-zero, and multiplying a lane of 1 by q1's u16 lane of 1
 *      sums to one per non-zero word. It is the same instruction count as the plain "is it zero" test
 *      (the compare and the two logical ops replace nothing else), and it is the number a caller asking
 *      "how many slots in this group are still free" wants. At most three iterations of this run per
 *      call, so its 10-instruction body is a fixed cost, not a per-word one.
 *   3. the four words of the group that holds the answer -- or the 0..3 words of the tail -- are loaded
 *      and reduced by a BRANCHLESS chain: `movnez` (the core ISA's conditional move, operand order
 *      pinned by the toolchain's own codegen for `b ? a : 7` in the .md) from the highest index down, so
 *      the last write that fires is the smallest index. An initial value of n_words makes the not-found
 *      case fall out with no clamp and no extra branch.
 *
 * Phase 1's pointer has already walked to the END of its 64 bytes when the test fires, so the hot path
 * rewinds it by 64 before phase 2 localises. Forgetting that rewind is a bug this file had: the
 * instruction-level model in ex21_vmframe.md found it on the first sweep after phase 1 and phase 2 were
 * written (16 words, word 0 non-zero -> the kernel answered "not found", because phase 2 was scanning
 * the SECOND super-chunk).
 * ---------------------------------------------------------------------------------------------------------------- */
    .align 4
    .global ex21_scan_first_nonzero
    .type ex21_scan_first_nonzero,@function
ex21_scan_first_nonzero:
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
    mov a4, a3                      /* a4 = the answer for "nothing found": n_words */
    mov a12, a2                     /* keep p: the base index is computed from the final pointer */
    movi a5, 0                      /* EE.SRS.ACCX shifts by a5[5:0] = 0 */
    EE.ZERO.Q q0                    /* the zero operand of EE.VCMP.EQ.S32 */
    movi a14, 1
    slli a14, a14, 16
    addi a14, a14, 1                /* a14 = 0x00010001: two u16 lanes of 1 per 32-bit segment */
    EE.MOVI.32.Q q1, a14, 0
    EE.MOVI.32.Q q1, a14, 1
    EE.MOVI.32.Q q1, a14, 2
    EE.MOVI.32.Q q1, a14, 3         /* q1 = eight u16 lanes of 1: the multiplier that turns a sum
                                     * of lanes into a COUNT (each lane contributes its own value) */
    movi a15, 1
    EE.MOVI.32.Q q6, a15, 0
    EE.MOVI.32.Q q6, a15, 1
    EE.MOVI.32.Q q6, a15, 2
    EE.MOVI.32.Q q6, a15, 3         /* q6 = 0x00000001 in every 32-bit lane: the non-zero-word mask */
    srli a6, a3, 2                  /* a6 = the 16-byte groups left */
    srli a8, a6, 2                  /* a8 = the 64-byte super-chunks */
    beqz a8, .Lsc_groups
.Lsc_l1:
    EE.VLD.128.IP q2, a2, 16
    EE.VLD.128.IP q3, a2, 16
    EE.VLD.128.IP q4, a2, 16
    EE.VLD.128.IP q5, a2, 16        /* 16 words: a2 += 64 */
    EE.ORQ q2, q2, q3
    EE.ORQ q4, q4, q5
    EE.ORQ q2, q2, q4               /* q2 = the OR of the four chunks: 0 iff all 64 bytes are 0 */
    EE.ZERO.ACCX
    EE.VMULAS.U16.ACCX q2, q1       /* ACCX = the sum of q2's eight u16 lanes, i.e. 0 iff q2 == 0 */
    EE.SRS.ACCX a7, a5, 0           /* a7 = that sum (<= 8 * 65535, no saturation in 40 bits) */
    bnez a7, .Lsc_hot               /* a non-zero word is inside this 64 bytes */
    addi a6, a6, -4                 /* four groups consumed */
    addi a8, a8, -1
    bnez a8, .Lsc_l1
.Lsc_groups:                        /* a6 = the groups left (0..3) */
    beqz a6, .Lsc_tail
.Lsc_g2:
    EE.VLD.128.IP q2, a2, 16        /* four words; a2 += 16 */
    EE.VCMP.EQ.S32 q3, q2, q0       /* 0xFFFFFFFF in the lanes whose word is zero */
    EE.NOTQ q3, q3                  /* 0xFFFFFFFF in the lanes whose word is NOT zero */
    EE.ANDQ q3, q3, q6              /* 0x00000001 per non-zero word */
    EE.ZERO.ACCX
    EE.VMULAS.U16.ACCX q3, q1       /* ACCX = the NUMBER of non-zero words in this group (0..4) */
    EE.SRS.ACCX a7, a5, 0
    bnez a7, .Lsc_gfound            /* the answer's word is in here */
    addi a6, a6, -1
    bnez a6, .Lsc_g2
.Lsc_tail:                          /* 0..3 words left at a2: load them and never past the end */
    movi a7, 0
    movi a8, 0
    movi a9, 0
    movi a10, 0                     /* w0..w3 = 0: a word that is not there can never win the chain */
    extui a11, a3, 0, 2             /* the words left (n_words & 3) */
    beqz a11, .Lsc_done
    l32i a7, a2, 0
    addi a11, a11, -1
    beqz a11, .Lsc_sel
    l32i a8, a2, 4
    addi a11, a11, -1
    beqz a11, .Lsc_sel
    l32i a9, a2, 8
.Lsc_sel:                           /* a7..a10 = the four words of the group holding the answer */
    sub a11, a2, a12                /* the group's byte offset from p (p is still in a12) */
    srli a11, a11, 2                /* a11 = the absolute index of w0 */
    addi a12, a11, 3
    movnez a4, a12, a10             /* w3 != 0 -> the answer is w0's index + 3 ... */
    addi a13, a11, 2
    movnez a4, a13, a9              /* ... then +2, +1, +0 override it in that order, so the SMALLEST
                                     * index whose word is non-zero wins, and a4 keeps n_words when
                                     * none of the four fires. Branchless: four conditional moves. */
    addi a12, a11, 1
    movnez a4, a12, a8
    movnez a4, a11, a7
.Lsc_done:
    mov a2, a4
    /* ABI: put the caller's a2/a3/a4/a5/a6/a7 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    l32i a13, a1, 44
    l32i a14, a1, 48
    l32i a15, a1, 52
    retw.n
.Lsc_hot:                           /* phase 1's hit: a2 is already 64 bytes past the hot chunk */
    addi a2, a2, -64
    j .Lsc_groups
.Lsc_gfound:                        /* phase 2's hit: a2 is already 16 bytes past the hot group */
    addi a2, a2, -16
    l32i a7, a2, 0
    l32i a8, a2, 4
    l32i a9, a2, 8
    l32i a10, a2, 12
    j .Lsc_sel
    .size ex21_scan_first_nonzero, .-ex21_scan_first_nonzero

/* ------------------------------------------------------------------------------------------------------------------
 * D: void ex21_copy_values(int16_t *dst, const int16_t *src, uint32_t n_values)
 *
 *   a2 = dst, a3 = src   both 16-BYTE ALIGNED, a4 = n_values (slots of 8 bytes)
 *
 * One 16-byte group (two slots) per iteration, the trailing odd slot with the 64-bit pair.
 *
 * The loop below is NOT software-pipelined, on purpose. Pipelining it (load the next group before storing
 * the current one) buys the load->store distance the measured anchor wants -- 1.000 cycle when a load's
 * stage-M def is consumed at distance 1, 0.000 at distance 2 -- but a load-after-store pipeline reads one
 * 16-byte group PAST the end of the region, and the region here is a frame block inside a segment whose
 * end is a segment boundary. ex15_copy_row makes the other choice for a row buffer it owns; this kernel
 * does not. The price is 2 modelled cycles per 32 bytes, the alternative's is a read beyond the caller's
 * object.
 * ---------------------------------------------------------------------------------------------------------------- */
    .align 4
    .global ex21_copy_values
    .type ex21_copy_values,@function
ex21_copy_values:
    entry a1, 32
    extui a5, a4, 0, 1              /* the odd slot, if any */
    srli a4, a4, 1                  /* 16-byte groups */
    beqz a4, .Lcp_odd
    extui a6, a4, 0, 1              /* an odd number of groups? */
    srli a7, a4, 1                  /* pairs of groups (32 bytes per iteration) */
    beqz a6, .Lcp_pairs
    EE.VLD.128.IP q0, a3, 16        /* the group that cannot be paired: one distance-1 load->store */
    EE.VST.128.IP q0, a2, 16
.Lcp_pairs:
    beqz a7, .Lcp_odd
.Lcp_loop:
    EE.VLD.128.IP q0, a3, 16
    EE.VLD.128.IP q1, a3, 16        /* both loads first: each store then follows its load by 1 issue */
    EE.VST.128.IP q0, a2, 16
    EE.VST.128.IP q1, a2, 16
    addi a7, a7, -1
    bnez a7, .Lcp_loop
.Lcp_odd:
    beqz a5, .Lcp_done
    EE.VLD.L.64.IP q0, a3, 8        /* q0[63:0] = the last slot (TRM p168); the walk is 8 bytes, so
                                     * both pointers end one past the region the caller named */
    EE.VST.L.64.IP q0, a2, 8        /* 8 bytes out (TRM p279) */
.Lcp_done:
    retw.n
    .size ex21_copy_values, .-ex21_copy_values
