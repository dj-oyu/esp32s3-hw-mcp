/* ex11 (proposed) -- the separable 8x8 integer block transform, eight columns in flight at once.
 *
 * MP3 (the IMDCT / hybrid filterbank), JPEG (the integer DCT) and H.264 (the 4x4 / 8x8 integer transform) all
 * run the same shape: out[k][j] = sat16( (sum over i of coef[k][i] * block[i][j]) >> shift ). The reduction
 * index i is one axis of the block, the free index j is the other. `EE.VSMULAS.S16.QACC qx, qy, sel8` (TRM
 * p269) broadcasts ONE 16-bit lane of qy (temp = qy[sel8*16+15 : sel8*16]) and multiply-accumulates it
 * against all eight lanes of qx, each lane into its own saturating 40-bit accumulator (QACC_L lanes 0..3,
 * QACC_H lanes 4..7). So the REDUCTION index goes in the broadcast operand and the FREE index in the lanes:
 *
 *   q0     = coef row k     -> lane i = coef[k][i]      (the 8 taps of one output row, one per lane)
 *   q1..q4 = block rows     -> lane j = block[i][j]     (the eight COLUMNS of the block are the lanes)
 *   sel8 = i -> broadcast coef[k][i], MAC against block row i:  acc[j] += coef[k][i] * block[i][j]
 *
 * LAYOUT (stated exactly, because it is the whole contract):
 *   coef  : 64 int16, row-major. coef[k][i] at coef + 16*k + 2*i. A 128-bit load at coef + 16*k puts
 *           coef[k][0..7] in lanes 0..7, which is exactly what the sel8 broadcast indexes.
 *   block : 64 int16, row-major. block[i][j] at block + 16*i + 2*j. A 128-bit load at block + 16*i puts
 *           block[i][0..7] in lanes 0..7, so THE EIGHT SIMD LANES ARE THE EIGHT COLUMNS j of the block and
 *           the eight loads walk the rows i. No transposition is needed for this pass: the row that streams
 *           through memory is the row that streams through the MAC sequence, and the free index is the one
 *           inside a row. (Contrast ex07: same instruction, same shape, but there the free index -- the
 *           eight vertices -- was the within-row index AND the reduction index was the row; here the two
 *           axes of the 8x8 swap roles, which is why the row-major block needs no shuffle.)
 *   out   : 64 int16, row-major. Lane j of the register stored for output row k is out[k][j].
 *   To transform the OTHER axis of a 2-D block the caller transposes the intermediate 8x8 and calls again
 *   (or supplies a second coefficient table holding coef^T). The kernel is a general 8x8 matrix product, so
 *   the two forms are the same arithmetic; the transpose form lets a codec keep one 64-word table. Note the
 *   intermediate is int16 (SRCMB saturates), so a two-pass transform is exact only while the first pass'
 *   output fits 16 bits -- which is why real codecs pick the pass-1 shift for headroom, not for full scale.
 *
 * WHY THE ROW REGISTERS ROLL (q1..q4 for eight rows): the ESP32-S3 PIE has eight 128-bit registers q0..q7 --
 * the assembler rejects q8 with "Error: register number out of range" -- so ex07's shape, one live register
 * per tap, does not scale from 4 taps to 8. The four row registers are reused with a reuse distance of eight
 * instructions (four load/MAC pairs): the load for row i only overwrites the register that row i-4's MAC
 * read, and the pipeline issues in order, so that earlier MAC has already read its operand. The `nop.n`
 * slots below guard the other side of the hazard -- the read of the accumulator -- and not this one.
 *
 * Cost: 27 instructions per output row of eight elements = 3.375 instructions per output element; the 8x8
 * block is eight rows, so 216 instructions in the loop (221 with the prologue/epilogue) for 64 outputs.
 * Inside those 216 are 64 broadcast-MACs, each performing eight lane multiply-accumulates = 512 lane-MACs,
 * the same arithmetic the scalar reference does one at a time, plus 72 128-bit loads (8 block rows x 8
 * output rows + 8 coefficient rows) and 8 readouts.
 *
 * The readout: `EE.SRCMB.S16.QACC qu, as, 0` (TRM p130) shifts each 40-bit accumulator right by as[5:0],
 * saturates the result into its 16-bit lane, and writes the shifted value back into QACC_H/QACC_L. The
 * write-back is why the loop zeroes the accumulator at the top of every row instead of trusting the first
 * MAC to overwrite it: ex09's srcmb_verdict came back "read_modify_write" from the device, so QACC after
 * SRCMB is the shifted value and not the raw sum. The four `nop.n` slots between the last MAC and SRCMB are
 * the separation the accumulator readout was measured to need (4 slots -- see ex09 / ex09_qacc.S chain_g4).
 * The static model asks for less: Table 1.7-2 (TRM 1.7, printed p.67) gives EE.VSMULAS.S16.QACC a QACC_H /
 * QACC_L def at pipeline stage 2 (M) and EE.SRCMB.S16.QACC a QACC use at stage 1 (E), i.e. one cycle of
 * separation on paper. The probe's value is taken because the accumulator's write-to-read behaviour was
 * exactly what the static model got wrong, so paper is not the authority here.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex11_block8x8(const int16_t *coef, const int16_t *block, int16_t *out, uint32_t shift)
 *   a2 = coef : 8x8 int16 row-major, row k at coef + 16*k
 *   a3 = block: 8x8 int16 row-major, row i at block + 16*i (its eight columns are the eight lanes)
 *   a4 = out  : 8x8 int16 row-major, row k at out + 16*k
 *   a5 = shift: >> this, then saturate to 16 bits, at the readout (as[5:0], TRM p130)
 *   out[k][j] = sat16( (sum(i = 0..7) coef[k][i] * block[i][j]) >> shift )
 *   Buffers must be 16-byte aligned: the 128-bit PIE forms zero the low four address bits (TRM p49), so a
 *   misaligned pointer is not reported, it silently reads or writes the neighbouring bytes. */
    .align 4
    .global ex11_block8x8
    .type ex11_block8x8,@function
ex11_block8x8:
    entry a1, 32
    mov a12, a5                     /* shift amount register for EE.SRCMB.S16.QACC */
    mov a6, a3                      /* the block rows are re-read for every output row */
    movi a7, 8                      /* output rows remaining */
.Lex11_row:
    EE.ZERO.QACC
    mov a3, a6
    EE.VLD.128.IP q0, a2, 16        /* coef row k: lane i = coef[k][i], broadcast lane index = tap index */
    EE.VLD.128.IP q1, a3, 16        /* block row 0: lane j = block[0][j] */
    EE.VSMULAS.S16.QACC q1, q0, 0   /* lane j += block[0][j] * coef[k][0] */
    EE.VLD.128.IP q2, a3, 16
    EE.VSMULAS.S16.QACC q2, q0, 1
    EE.VLD.128.IP q3, a3, 16
    EE.VSMULAS.S16.QACC q3, q0, 2
    EE.VLD.128.IP q4, a3, 16
    EE.VSMULAS.S16.QACC q4, q0, 3
    EE.VLD.128.IP q1, a3, 16        /* row 4: q1 is free again, 8 instructions after its MAC */
    EE.VSMULAS.S16.QACC q1, q0, 4
    EE.VLD.128.IP q2, a3, 16
    EE.VSMULAS.S16.QACC q2, q0, 5
    EE.VLD.128.IP q3, a3, 16
    EE.VSMULAS.S16.QACC q3, q0, 6
    EE.VLD.128.IP q4, a3, 16
    EE.VSMULAS.S16.QACC q4, q0, 7
    nop.n                           /* four slots: the SRCMB readout of QACC must not follow the last MAC */
    nop.n                           /* immediately -- 4 slots (see ex09 / ex09_qacc.S chain_g4). Table 1.7-2 */
    nop.n                           /* asks for only 1 (QACC def at M, reuse at E), and the accumulator's */
    nop.n                           /* write-to-read behaviour is what that model got wrong, so: the probe. */
    EE.SRCMB.S16.QACC q5, a12, 0    /* >> shift, saturate to 16 bits, eight lanes at once */
    EE.VST.128.IP q5, a4, 16
    addi a7, a7, -1
    bnez a7, .Lex11_row
    retw.n
    .size ex11_block8x8, .-ex11_block8x8
