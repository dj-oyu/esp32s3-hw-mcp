/* ex03 -- 16-tap Q15 FIR, and the alignment rule that decides how it has to be written.
 *
 * y[n] = sat32( (sum(k = 0..15) x[n + k] * h[k]) >> shift )
 *
 * First run (log pie-examples-20260914T174138Z) settled this the hard way: the sliding version, which
 * loaded `x + 2*n` directly with EE.VLD.128.IP, produced y[0] for every sample whose 16-byte window did
 * not happen to start on the grid -- 42 of 49 outputs wrong, with the right answer exactly at n =
 * 0, 8, 16, 24, 32, 40, 48. The cause is the documented one (TRM p49): a 128-bit PIE access forms its
 * address as {as[31:4], 4{0}}, so the low four address bits are *dropped*, not honoured. A 2-byte slide is
 * therefore invisible to the hardware and reads the same 16 bytes.
 *
 * So the kernel takes one window that is already on the 16-byte grid, and the caller stages it (memcpy into
 * an aligned buffer, or a DMA that lands there). `ex03_align_probe` keeps the failure visible as a
 * measurement rather than a comment, and `ex03_funnel_probe` measures the fast path for the general case:
 * EE.LD.128.USAR.IP records the dropped address bits in SAR_BYTE and EE.SRC.Q shifts the window out of two
 * aligned chunks, so no staging copy is needed. The probe prints both operand orders so the first
 * instruction-level run can say which one is qs0 and which is qs1.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* int32_t ex03_fir16_tap(const int16_t *window16, const int16_t *h, uint32_t shift)
 *   a2 = the window (16-byte aligned), a3 = h, a4 = shift
 * Two VMULAS.S16.ACCX per output: each sums its eight lane products into the 40-bit ACCX, so the pair is
 * the full 16-tap dot product; EE.SRS.ACCX then applies the shift (from a register) and clamps to int32.
 * Every 128-bit load here uses an immediate of 0, i.e. no post-increment, so the address registers are
 * left alone and the second chunk is reached with its own pointer. */
    .align 4
    .global ex03_fir16_tap
    .type ex03_fir16_tap,@function
ex03_fir16_tap:
    entry a1, 32
    mov a12, a4
    EE.VLD.128.IP q0, a2, 0         /* window[0..7]  */
    addi a9, a2, 16
    EE.VLD.128.IP q1, a9, 0         /* window[8..15] */
    EE.VLD.128.IP q2, a3, 0         /* h[0..7]       */
    addi a10, a3, 16
    EE.VLD.128.IP q3, a10, 0        /* h[8..15]      */
    EE.ZERO.ACCX
    EE.VMULAS.S16.ACCX q0, q2
    EE.VMULAS.S16.ACCX q1, q3
    EE.SRS.ACCX a2, a12, 0
    retw.n
    .size ex03_fir16_tap, .-ex03_fir16_tap

/* void ex03_align_probe(const int16_t *x, int16_t *out32)
 *   out32[0..7]  = the first 16 bytes at x
 *   out32[8..15] = the first 16 bytes at x + 2 bytes, which the hardware sees as x again.
 * The two halves are expected to be identical; a difference would mean the low address bits are honoured
 * after all, and the whole sliding-window design in this file is wrong for the other reason. */
    .align 4
    .global ex03_align_probe
    .type ex03_align_probe,@function
ex03_align_probe:
    entry a1, 32
    addi a10, a2, 2
    EE.VLD.128.IP q0, a2, 0
    EE.VLD.128.IP q1, a10, 0
    EE.VST.128.IP q0, a3, 0
    addi a9, a3, 16
    EE.VST.128.IP q1, a9, 0
    retw.n
    .size ex03_align_probe, .-ex03_align_probe

/* void ex03_funnel_probe(const int16_t *misaligned, const int16_t *next_chunk, int16_t *out_ab, int16_t *out_ba)
 *   a2 = an address off the 16-byte grid (its low 4 bits become SAR_BYTE), a3 = the aligned chunk that
 *   follows it, a4/a5 = two output buffers.
 * EE.LD.128.USAR.IP loads the aligned chunk *and* stores the dropped bits in SAR_BYTE; EE.SRC.Q then
 * shifts {qs1, qs0} right by SAR_BYTE*8 bits, which is the unaligned window. Which of the two operands is
 * qs0 is not readable off the page (the syntax line for EE.SRC.Q does not extract), so both orders run and
 * the log carries both results. */
    .align 4
    .global ex03_funnel_probe
    .type ex03_funnel_probe,@function
ex03_funnel_probe:
    entry a1, 32
    EE.LD.128.USAR.IP q0, a2, 0     /* q0 = aligned chunk, SAR_BYTE = a2[3:0] */
    EE.VLD.128.IP q1, a3, 0         /* q1 = the following chunk */
    EE.SRC.Q q3, q0, q1
    EE.VST.128.IP q3, a4, 0
    EE.SRC.Q q4, q1, q0
    EE.VST.128.IP q4, a5, 0
    retw.n
    .size ex03_funnel_probe, .-ex03_funnel_probe
