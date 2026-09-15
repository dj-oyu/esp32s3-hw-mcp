/* ex15 -- unaligned framebuffer transfer to the LCD path: a window that starts on an odd byte.
 *
 * The panel this is written for: 135x240 px, 16 bits per pixel, 2 bytes per pixel, no PSRAM. The whole
 * framebuffer is 64800 bytes = 4050 sixteen-byte chunks exactly, but one ROW is 270 bytes = 16.875 chunks,
 * and a window can start anywhere inside a chunk. notes/08-media-3d-perf.md names this stage as unbuilt
 * ("フレームバッファ転送 | EE.SRCQ.128.ST.INCP(非整列寄せ, SAR_BYTE) + GDMA/SPI | 未着手"); the three
 * instructions it names are the three this file is built from.
 *
 * WHY THIS IS NOT A memcpy. A 128-bit PIE access forms its address as {as[31:4], 4{0}} (TRM p49,
 * figure 1.7-1): the low four address bits are dropped, not honoured. A store to a window that starts at
 * byte 1 therefore writes the 16 bytes at byte 0 and silently destroys the byte BEFORE the window. ex03
 * reproduced exactly this for loads on silicon (finding vld128_drops_the_low_address_bits in
 * data/pie_examples_measured.json: "the low four address bits are dropped, not honoured, so an access off
 * the 16-byte grid silently reads a different 16 bytes (and does not fault)"). There is no unaligned 128-bit
 * store, so a window off the grid is moved by exactly two mechanisms, and this file uses both:
 *
 *   (1) SHIFT ON THE WAY IN. EE.LD.128.USAR.IP (TRM p93) loads the aligned chunk *containing* the pointer
 *       and records the dropped bits in SAR_BYTE; EE.SRCQ.128.ST.INCP (TRM p132) stores
 *       {qs1[127:0], qs0[127:0]} >> (SAR_BYTE*8) to {as[31:4],4{0}} and then adds 16 to as. One pair of
 *       instructions moves a 16-byte window that starts on ANY byte, and the store address walks itself.
 *       The operand order matters and is not obvious from the syntax line: the FIRST operand is qs0, the LOW
 *       half (settled on silicon, finding src_q_operand_order_and_the_funnel_path).
 *   (2) READ-MODIFY-WRITE the two chunks the window only partly covers, because the bytes outside the window
 *       that share those chunks must keep their old values. Both ends are RMW'd here, and the interior is
 *       (1) alone: 2 memory operations per 16 bytes.
 *
 * THE SPLIT, and why it falls where it does. The head RMW absorbs the DESTINATION's misalignment; after it
 * every destination chunk of the window is 16-byte aligned. From there the only phase that matters is the
 * SOURCE pointer's, and a 16-byte stride preserves a pointer's low four bits -- so SAR_BYTE is set once by
 * the first load of the interior and stays valid to the end of the loop. That is what makes the interior 2
 * memory operations per 16 bytes and not 3.
 *
 * WHY THE MASKS ARE BUILT WITH EE.SRC.Q AND A LOAD, not with EE.SRCI.2Q. The obvious mask builder is
 * EE.SRCI.2Q (TRM p129), a 256-bit logical shift. Its amount is a 4-BIT IMMEDIATE FIELD (sar16[3:0],
 * shift = (sar16+1)*8), so it cannot express a mask that depends on the runtime offset k. EE.SRC.Q's shift
 * amount comes from SAR_BYTE, which every EE.LD.128.USAR.IP writes from the low four bits of that load's
 * address -- a runtime value. With qs0 = a register of zeroes and qs1 = a register of ones, EE.SRC.Q
 * returns the 16-byte window at offset SAR_BYTE of the 32-byte concatenation {ones, zero}, which is
 *
 *     EE.SRC.Q qa, qs0=zero, qs1=ones   ->   qa = 0x00 x (16-s) ++ 0xFF x s,  i.e. "bytes (16-s)..15 set"
 *
 * so the shift s and the mask boundary b are related by s = 16-b. Every mask in this file is that shape,
 * and the SHIFT COMES FROM A LOAD ADDRESS whose low four bits are 16-b, which is why the kernel contains
 * three loads whose data is thrown away ("pokes", marked below). They are cheap and they never read outside
 * the framebuffer chunk that the same read-modify-write is already touching:
 *
 *     SAR_BYTE = 16-k    from  c0 + h        (h = 16-k)      -> the head's replace mask
 *     SAR_BYTE = 16-k-b  from  c0 + (h-b)                    -> the "window ends inside the head chunk" case
 *     SAR_BYTE = 16-m    from  floor(E) + (16-m)             -> the tail's keep mask
 *
 * and the two loads that are NOT pokes do double duty, because they return exactly the bytes that must
 * survive AND set SAR_BYTE from a pointer the kernel already has:
 *
 *     EE.LD.128.USAR.IP q5, D, 0   ->  q5 = floor(D) = the head chunk (the old bytes), SAR_BYTE = k
 *     EE.LD.128.USAR.IP q5, E, 0   ->  q5 = floor(E) = the tail chunk (the old bytes), SAR_BYTE = m
 *
 * with D = dst + byte_off (the window start) and E = D + n_bytes (the window end). With a 16-byte-aligned
 * source the head's funnel pointer src-k already has low bits 16-k, so that one poke would be redundant --
 * a fast path that is left out on purpose, to keep one sequence and one proof.
 *
 * THIS FILE IS PROPOSED. It is not in examples/firmware/main/CMakeLists.txt, it is not declared in
 * examples.h, it is not called by main.c, and it has NOT run on silicon: no hardware was used to produce
 * anything in this file or in ex15_fbstore.md next to it. That .md carries the contract table, the Python
 * model of all three kernels with its equivalence run against a memcpy reference, the host-compiled C
 * verifier, the assembler/objdump transcript, the TRM page of every instruction used, and the list of
 * semantics that are still assumptions rather than measurements.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex15_store_window(int16_t *dst, int32_t byte_off, const uint8_t *src, uint32_t n_bytes)
 *   a2 = dst      framebuffer base, 16-BYTE ALIGNED (contract; a 128-bit access to a misaligned pointer is
 *                 not diagnosed, it reads the boundary below it -- pie_buffers_must_be_declared_aligned_16)
 *   a3 = byte_off byte offset of the window; ARBITRARY, odd allowed. (An odd window start cannot come from
 *                 the panel's own CASET/RASET addressing, which is pixel-granular; it comes from
 *                 byte-granular sources: packed 1/8-bpp sprites, a byte-addressable scroll, a skewed or
 *                 rotated window, or a row that is not a multiple of 16 bytes.)
 *   a4 = src      the bytes to place at dst + byte_off; ANY byte alignment
 *   a5 = n_bytes  how many; 0 is legal and touches nothing
 *
 * Post-condition:  framebuffer[byte_off + j] == src[j] for every j in [0, n_bytes), and NO other byte of the
 * framebuffer changes -- in particular not the bytes that share the first and last chunk with the window.
 *
 * WHICH SEQUENCE FOR WHICH ALIGNMENT CASE (k = byte_off & 15, h = 16-k, b = min(n_bytes, h), E = D+n_bytes):
 *
 *   n_bytes == 0                nothing at all: no load, no store, not even a read of the framebuffer.
 *   k == 0                      no head chunk exists (the window starts on the grid). The sequence is the
 *                               interior, then the tail RMW if E is off the grid.
 *   k != 0, k even              head RMW + interior + tail RMW. The window start is a PIXEL boundary
 *                               (2 bytes per pixel); the runs the two RMWs replace are even-length runs at
 *                               even offsets, so every pixel the kernel touches is written by one store and
 *                               the pixels outside the window keep their old value. This is the case the
 *                               panel path has in practice (a window starting 4 px = 8 bytes in).
 *   k != 0, k odd               the SAME sequence. Pixels can straddle: byte k is the high byte of the
 *                               pixel at k-1, so the first and last pixel of a row take one byte from the
 *                               window and one byte from outside it. That is inherent to a byte-granular
 *                               window on a 16-bit panel -- the sequence handles it, and the bytes outside
 *                               the window still keep their old values.
 *   n_bytes < h ("less than 16 bytes remaining", the whole window inside the head chunk)
 *                               one head RMW with a two-sided mask (replace bytes [k..k+b-1], keep
 *                               [0..k-1] and [k+b..15]); no interior store at all; the tail phase is not
 *                               reached.
 *   n_bytes < 16 but n_bytes > h
 *                               head RMW, no interior store (there is no full chunk), one tail RMW.
 *
 * Register walk:  a3 n_bytes (it outlives a5, which counts down)   a4 src   a5 remaining   a6 D   a7 k
 *   a8 h   a9 the chunk being RMW'd   a10/a15 pointers   a11 interior destination chunk   a12 interior
 *   source pointer   a13 E   a14 b / the tail remainder   a2 free once D is computed.
 *   q0 zero   q1 scratch   q2 all-ones   q3 keep mask   q4 replace mask   q5 the old chunk   q6/q7 the
 *   window pair.
 *
 * The scheduling comments are the static hazard model of TRM Table 1.7-2 (data/pie_hazards.md) with the two
 * anchors measured on silicon (data/pie_timing_measured.json: a stage-M def consumed at stage E costs 1
 * cycle at issue distance 1 and 0 at distance 2; a stage-E def costs 0 at distance 1). They are issue-slot
 * orderings, NOT measured cycle counts -- no cycle measurement exists for this kernel.
 */
    .align 4
    .global ex15_store_window
    .type ex15_store_window,@function
ex15_store_window:
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
    beqz a5, .Lsw_done              /* n_bytes == 0 */
    add a6, a2, a3                  /* a6 = D: the window start (unaligned) */
    add a13, a6, a5                 /* a13 = E: the window end (floor(E) is the tail chunk) */
    extui a7, a3, 0, 4              /* a7 = k = byte_off & 15 */
    mov a3, a5                      /* keep n_bytes: the tail window is the LAST m bytes of the window */
    mov a11, a6                     /* k == 0: the first destination chunk is the window start */
    mov a12, a4                     /* k == 0: the interior source pointer is src */
    beqz a7, .Lsw_interior          /* 16-byte aligned destination: no head chunk to preserve */

/* ---- head chunk: read-modify-write of bytes [k .. k+b-1] of floor(D) -------------------------------- */
    movi a8, 16
    sub a8, a8, a7                  /* a8 = h = 16-k: how many window bytes this chunk can take */
    mov a9, a6
    sub a9, a9, a7                  /* a9 = floor(D): the head chunk */
    mov a14, a8                     /* b = min(n_bytes, h) */
    bgeu a5, a8, .Lsw_bknown
    mov a14, a5                     /* n_bytes < h: the window also ends inside this chunk */
.Lsw_bknown:
    sub a10, a4, a7                 /* the window that ENDS k bytes into the chunk is the 16 bytes at
                                     * src-k: its bytes [k..15] are src[0 .. 15-k], its bytes [0..k-1] are
                                     * memory before src and are masked away below. Reading there is a load
                                     * with no side effect (contract: up to 30 bytes before src[0]; the .md's
                                     * model reports the footprint it actually uses) */
    EE.LD.128.USAR.IP q5, a10, 0    /* the aligned chunk containing src-k; SAR_BYTE = (src-k) & 15 */
    EE.ZERO.Q q0                    /* free slot, and the zero operand of every mask */
    addi a15, a10, 16
    EE.LD.128.USAR.IP q6, a15, 0    /* the following chunk; +16 leaves as[3:0] alone, so SAR_BYTE is still
                                     * (src-k) & 15 */
    EE.NOTQ q2, q0                  /* q2 = 16 bytes of 0xFF: the ones operand of every mask */
    EE.SRC.Q q7, q5, q6             /* q7 = the 16 bytes at src-k: q7[k..15] == src[0 .. 15-k]. The first
                                     * operand is qs0 = the low half; the q6 load is 2 issues back, so its
                                     * stage-M def is not charged */
    EE.LD.128.USAR.IP q5, a6, 0     /* q5 = floor(D) = the head chunk: the bytes that must survive */
    add a15, a9, a8                 /* c0 + h, whose low four bits are h = 16-k */
    EE.LD.128.USAR.IP q3, a15, 0    /* POKE: SAR_BYTE = h = 16-k. The mask below must have its boundary at
                                     * k, and EE.SRC.Q's boundary is at 16-SAR_BYTE, so SAR_BYTE has to be
                                     * 16-k -- k is already spent on the funnel above. floor(c0+h) is c0, so
                                     * this reads the head chunk itself; the data is discarded */
    EE.SRC.Q q4, q0, q2             /* q4 = bytes k..15 set = the replace mask */
    bgeu a5, a8, .Lsw_mask          /* n_bytes >= h: the window fills [k..15] of this chunk exactly */
    sub a15, a8, a14                /* h - b = 16-k-b */
    add a15, a9, a15                /* c0 + (16-k-b): low four bits 16-k-b, still inside the head chunk */
    EE.LD.128.USAR.IP q3, a15, 0    /* POKE: SAR_BYTE = 16-k-b (the window ends inside this chunk) */
    EE.SRC.Q q1, q0, q2             /* q1 = bytes (k+b)..15 set */
    EE.NOTQ q3, q1                  /* q3 = bytes 0..(k+b-1) set */
    EE.ANDQ q4, q4, q3              /* replace = bytes [k .. k+b-1] only; [k+b..15] keeps its old bytes */
.Lsw_mask:
    EE.NOTQ q3, q4                  /* q3 = keep = every other byte of the chunk */
    EE.ANDQ q5, q5, q3              /* old & keep */
    EE.ANDQ q4, q7, q4              /* window & replace */
    EE.ORQ q5, q5, q4
    EE.VST.128.IP q5, a9, 0         /* the merged head chunk */
    sub a5, a5, a14                 /* n_bytes -= b */
    beqz a5, .Lsw_done              /* the whole window fitted in the head chunk */
    addi a11, a9, 16                /* from here on every destination chunk is on the 16-byte grid */
    add a12, a4, a14                /* src + b */

/* ---- interior: full 16-byte chunks, EE.SRCQ.128.ST.INCP --------------------------------------------- */
.Lsw_interior:
    srli a15, a5, 4                 /* full = n_bytes >> 4 chunks */
    beqz a15, .Lsw_tail             /* no full chunk: only the tail RMW is left */
    srli a9, a15, 1                 /* pairs of chunks per loop iteration (2 chunks per iteration) */
    EE.LD.128.USAR.IP q0, a12, 16   /* C0: the aligned chunk containing the window start. SAR_BYTE = the
                                     * SOURCE phase, and the printed immediate is the byte step as takes
                                     * (+16), so the address register walks itself */
    EE.LD.128.USAR.IP q1, a12, 16   /* C1 */
    beqz a9, .Lsw_odd               /* this slot also puts the q1 load 2 issues ahead of its consumer */
.Lsw_pair:
    EE.SRCQ.128.ST.INCP q0, q1, a11 /* store {q1,q0} >> 8*SAR_BYTE at floor(a11) = a11, then a11 += 16 */
    EE.LD.128.USAR.IP q0, a12, 16   /* the chunk after the pair replaces the chunk just stored: a WAR
                                     * against the store's read of q0, not a RAW -- in-order issue, and the
                                     * store read q0 at its E stage, before this load can write */
    addi a9, a9, -1
    EE.SRCQ.128.ST.INCP q1, q0, a11 /* the pair is (C1,C2) now: qs0 = the LOW chunk = q1 */
    EE.LD.128.USAR.IP q1, a12, 16   /* C3 */
    bnez a9, .Lsw_pair
.Lsw_odd:
    extui a9, a15, 0, 1             /* an odd chunk left over? */
    beqz a9, .Lsw_tail
    EE.SRCQ.128.ST.INCP q0, q1, a11 /* the last full chunk: (q0,q1) is the live pair */

/* ---- tail chunk: read-modify-write of bytes [0 .. m-1] of floor(E) ---------------------------------- */
.Lsw_tail:
    extui a14, a13, 0, 4            /* a14 = m = E & 15; 0 means the window ended on the grid */
    beqz a14, .Lsw_done
    EE.LD.128.USAR.IP q5, a13, 0    /* q5 = floor(E) = the tail chunk: the bytes past the window */
    EE.ZERO.Q q0
    sub a9, a13, a14                /* a9 = floor(E) = E - m, the merge's destination */
    EE.NOTQ q2, q0                  /* q2 = 16 bytes of 0xFF */
    movi a15, 16
    sub a15, a15, a14               /* 16-m */
    add a15, a9, a15                /* floor(E) + (16-m): low four bits 16-m, inside the tail chunk */
    EE.LD.128.USAR.IP q1, a15, 0    /* POKE: SAR_BYTE = 16-m, the shift that makes the keep mask's boundary
                                     * land on m. The data is discarded */
    EE.SRC.Q q3, q0, q2             /* q3 = bytes m..15 set = the keep mask (those bytes are outside the
                                     * window, so they keep their old values) */
    EE.NOTQ q4, q3                  /* q4 = bytes 0..m-1 set = the replace mask */
    sub a10, a3, a14                /* n_bytes - m */
    add a10, a4, a10                /* src + n_bytes - m: the window's last m bytes */
    EE.LD.128.USAR.IP q6, a10, 0    /* the aligned chunk containing them; SAR_BYTE = its phase, which is the
                                     * source phase again: 16-byte strides preserved it */
    addi a15, a10, 16
    EE.LD.128.USAR.IP q7, a15, 0    /* the following chunk */
    EE.SRC.Q q1, q6, q7             /* q1 = the 16 bytes at that pointer: q1[0..m-1] = the last m bytes */
    EE.ANDQ q5, q5, q3              /* old & keep */
    EE.ANDQ q1, q1, q4              /* window & replace */
    EE.ORQ q5, q5, q1
    EE.VST.128.IP q5, a9, 0
.Lsw_done:
    /* ABI: put the caller's a2/a3/a4/a5/a6/a7 back before returning. */
    l32i a10, a1, 32
    l32i a11, a1, 36
    l32i a12, a1, 40
    l32i a13, a1, 44
    l32i a14, a1, 48
    l32i a15, a1, 52
    retw.n
    .size ex15_store_window, .-ex15_store_window

/* void ex15_copy_row(int16_t *dst, const int16_t *src, uint32_t n_px)
 *   a2 = dst, a3 = src   both 16-BYTE ALIGNED (contract), a4 = n_px
 *
 * The aligned baseline the unaligned path is measured against: a whole scanline, 128 bits (8 px) per chunk,
 * one EE.VLD.128.IP and one EE.VST.128.IP per chunk. n_px must be a multiple of 8. A 135-px row is NOT
 * (270 bytes = 16.875 chunks): pad rows to a 16-byte stride, or hand the row to ex15_store_window as a byte
 * count -- which is the whole reason that function exists. 240 px = 30 chunks, 136 px = 17 chunks.
 *
 * Cost, statically: 2 memory instructions per 16 bytes. A naive `EE.VLD.128.IP q0 / EE.VST.128.IP q0` pair
 * is 2 issue slots but pays the one-cycle stage-M-to-stage-E interlock every iteration (Table 1.7-2: the
 * load defines qu at M, the store reads qv at E; the measured anchor in data/pie_timing_measured.json is
 * 1.000 cycles at issue distance 1 and 0.000 at distance 2). Keeping one chunk in flight and alternating
 * the two vector registers puts 2 issues between a load and the store that consumes it, so the loop body
 * below is 4 vector + 2 scalar slots per 2 chunks with no modelled stall.
 */
    .align 4
    .global ex15_copy_row
    .type ex15_copy_row,@function
ex15_copy_row:
    entry a1, 32
    srli a4, a4, 3                  /* n_px -> chunks of 8 px (16 bytes) */
    extui a6, a4, 0, 1              /* an odd number of chunks? */
    srli a5, a4, 1                  /* a5 = pairs of chunks */
    beqz a6, .Lcr_pairs
    EE.VLD.128.IP q0, a3, 16        /* 8 px that cannot be paired: one plain load/store */
    EE.VST.128.IP q0, a2, 16
.Lcr_pairs:
    beqz a5, .Lcr_done
    EE.VLD.128.IP q0, a3, 16        /* start the pipeline */
.Lcr_loop:
    EE.VLD.128.IP q1, a3, 16
    EE.VST.128.IP q0, a2, 16        /* q0 was loaded 2 issues ago (M def, E use): distance 2 */
    EE.VLD.128.IP q0, a3, 16
    EE.VST.128.IP q1, a2, 16        /* q1 was loaded 3 issues ago */
    addi a5, a5, -1
    bnez a5, .Lcr_loop
.Lcr_done:
    retw.n
    .size ex15_copy_row, .-ex15_copy_row

/* void ex15_fill_row(int16_t *dst, int16_t color, uint32_t n_px)
 *   a2 = dst (16-byte aligned), a3 = color (RGB565: only the low 16 bits are read), a4 = n_px (a multiple of
 *   8). The cheapest row write there is: the colour is broadcast into eight int16 lanes ONCE, then one
 *   EE.VST.128.IP per 8 px, and nothing is read from memory at all.
 *
 *   The broadcast is built in-core, because a constant cannot be loaded from .rodata inside .iram1 (no l32r
 *   from here; the suite's rule is that constants arrive as pointer arguments, and this kernel's colour
 *   arrives as a value): EXTUI keeps the low 16 bits as unsigned, `slli` + `or` make the 32-bit pattern
 *   (c << 16) | c, and four EE.MOVI.32.Q (TRM p119) put that word into all four 32-bit segments of the
 *   register. Cost: 3 scalar + 4 vector instructions once per call, then 1 memory instruction per 16 bytes,
 *   i.e. half of copy_row's -- this is the lower bound the unaligned path is compared against.
 */
    .align 4
    .global ex15_fill_row
    .type ex15_fill_row,@function
ex15_fill_row:
    entry a1, 32
    extui a5, a3, 0, 16             /* the colour as an unsigned 16-bit value */
    slli a6, a5, 16
    or a5, a5, a6                   /* a5 = (color << 16) | color: two identical int16 lanes */
    EE.ZERO.Q q0
    EE.MOVI.32.Q q0, a5, 0
    EE.MOVI.32.Q q0, a5, 1
    EE.MOVI.32.Q q0, a5, 2
    EE.MOVI.32.Q q0, a5, 3          /* q0 = eight identical int16 lanes */
    srli a4, a4, 3                  /* chunks (8 px each) */
    beqz a4, .Lfr_done
.Lfr:
    EE.VST.128.IP q0, a2, 16
    addi a4, a4, -1
    bnez a4, .Lfr
.Lfr_done:
    retw.n
    .size ex15_fill_row, .-ex15_fill_row
