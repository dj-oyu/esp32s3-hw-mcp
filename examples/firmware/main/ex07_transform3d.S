/* ex07 -- a 4x4 transform of eight vertices at once: the inner loop of a 3D pipeline.
 *
 * The instruction that makes this pay is `EE.VSMULAS.S16.QACC qx, qy, sel8`: it broadcasts ONE 16-bit lane
 * of qy (temp = qy[sel8*16+15 : sel8*16]) and multiply-accumulates it against all eight lanes of qx, each
 * lane into its own saturating 40-bit accumulator (QACC_L holds lanes 0..3, QACC_H holds 4..7). So the
 * vertices are kept in structure-of-arrays form -- all eight x in one register, all eight y in the next --
 * and eight vertices advance together. Four VSMULAS with sel8 = 0..3 sum the four terms of a matrix row for
 * all eight vertices at once, and `EE.SRCMB.S16.QACC qu, as, 0` shifts every 40-bit accumulator right by
 * as[5:0] and saturates it into its 16-bit lane, which is the readout.
 *
 * One row of a 4x4 transform therefore costs 4 loads + 4 MACs + 1 readout + 1 store for EIGHT vertices,
 * against 8x4 multiply-accumulates for the same work in C -- which is why the caller reports both.
 *
 * Frames in these examples are int16 lanes rather than packed pixels: the Q-format arithmetic (coordinates
 * in Q8, matrix in Q8, >>16 at the readout) is the same arithmetic a Q15 DSP path uses, and the lane
 * values stay readable in the log.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* uint32_t ex07_ccount(void) -- read the cycle counter so the caller can report cycles per element. */
    .align 4
    .global ex07_ccount
    .type ex07_ccount,@function
ex07_ccount:
    entry a1, 32
    rsr.ccount a2
    retw.n
    .size ex07_ccount, .-ex07_ccount

/* void ex07_transform8(const int16_t *m, const int16_t *v, int16_t *out, uint32_t shift)
 *   a2 = m : 4x4 int16 row-major (row r at m + 16*r)
 *   a3 = v : four rows of eight int16 (v[k][0..7] at v + 16*k)
 *   a4 = out: four rows of eight int16
 *   a5 = shift for the readout
 *   out[r][j] = sat16( (sum(k = 0..3) m[r][k] * v[k][j]) >> shift ) */
    .align 4
    .global ex07_transform8
    .type ex07_transform8,@function
ex07_transform8:
    entry a1, 32
    mov a12, a5                     /* shift amount register for EE.SRCMB.S16.QACC */
    mov a6, a3                      /* the vertices are re-read for every output row */
    movi a7, 4
.Lxform_row:
    EE.ZERO.QACC
    mov a3, a6
    EE.VLD.128.IP q0, a2, 16        /* the row's four coefficients land in lanes 0..3 */
    EE.VLD.128.IP q1, a3, 16
    EE.VSMULAS.S16.QACC q1, q0, 0   /* lane j += v[0][j] * m[r][0] */
    EE.VLD.128.IP q2, a3, 16
    EE.VSMULAS.S16.QACC q2, q0, 1
    EE.VLD.128.IP q3, a3, 16
    EE.VSMULAS.S16.QACC q3, q0, 2
    EE.VLD.128.IP q4, a3, 16
    EE.VSMULAS.S16.QACC q4, q0, 3
    EE.SRCMB.S16.QACC q5, a12, 0    /* eight lanes: >> shift, then saturate into 16 bits */
    EE.VST.128.IP q5, a4, 16
    addi a7, a7, -1
    bnez a7, .Lxform_row
    retw.n
    .size ex07_transform8, .-ex07_transform8
