/* ex04 -- QR register transfers, and the LD.QR interlock at issue distance 1 and 2.
 *
 * LD.QR and ST.QR are the two instructions of the PIE set that Table 1.7-2 does not list at all, so their
 * staging had to be measured (data/pie_timing_measured.json: LD.QR writes its QR operand at M, i.e. stage
 * 2, and ST.QR reads its source at M). This example re-derives that on the bench: the same pair of
 * sequences, one where the consumer reads what LD.QR just wrote and one where it reads an unrelated QR
 * register, at 0, 1 and 2 filler instructions in between. The per-iteration difference is the interlock,
 * and it must be 1 cycle with no filler and 0 cycles once one instruction separates them.
 *
 * The copy loop deliberately walks the source and destination pointers by hand: LD.QR/ST.QR take an
 * address register plus an immediate and do *not* write the address back, unlike the .IP/.XP forms.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* void ex04_qr_copy(void *dst, const void *src, uint32_t n_bytes) -- whole 16-byte blocks */
    .align 4
    .global ex04_qr_copy
    .type ex04_qr_copy,@function
ex04_qr_copy:
    entry a1, 32
    srli a4, a4, 4
    beqz a4, .Lcopy_done
.Lcopy:
    LD.QR q0, a2, 0
    ST.QR q0, a3, 0
    addi a2, a2, 16
    addi a3, a3, 16
    addi a4, a4, -1
    bnez a4, .Lcopy
.Lcopy_done:
    retw.n
    .size ex04_qr_copy, .-ex04_qr_copy

/* void ex04_qr_move_roundtrip(void *dst, const void *src) -- LD.QR + MV.QR + ST.QR */
    .align 4
    .global ex04_qr_move_roundtrip
    .type ex04_qr_move_roundtrip,@function
ex04_qr_move_roundtrip:
    entry a1, 32
    LD.QR q0, a2, 0
    MV.QR q1, q0
    ST.QR q1, a3, 0
    retw.n
    .size ex04_qr_move_roundtrip, .-ex04_qr_move_roundtrip

/* uint32_t f(void *buf): 2000 iterations of [LD.QR ; fillers ; consumer], returns the CCOUNT delta.
 * The `\consumer` choice is what makes the pair a difference measurement: reading q0 (what LD.QR wrote)
 * against reading q4 (untouched). Fillers are 1-cycle adds, identical in both arms. */
.macro HAZ name, fillers, consumer
    .align 4
    .global \name
    .type \name,@function
\name:
    entry a1, 64
    /* ABI: this kernel writes a14 -- the call8 window makes the
     * callee's a10..a15 the caller's a2..a7, so they are saved here and restored
     * before every retw.n (tools/check_abi.py). */
    s32i a14, a1, 32
    movi a6, 2000
    rsr.ccount a7
.Lh_\name:
    mov a3, a2
    LD.QR q0, a3, 0
    .rept \fillers
    add a14, a13, a12
    .endr
    \consumer
    addi a6, a6, -1
    bnez a6, .Lh_\name
    rsr.ccount a2
    sub a2, a2, a7
    /* ABI: put the caller's a6 back before returning. */
    l32i a14, a1, 32
    retw.n
    .size \name, .-\name
.endm

HAZ ex04_ldqr_dep_d0, 0, "EE.ANDQ q1, q0, q0"
HAZ ex04_ldqr_ind_d0, 0, "EE.ANDQ q1, q4, q4"
HAZ ex04_ldqr_dep_d1, 1, "EE.ANDQ q1, q0, q0"
HAZ ex04_ldqr_ind_d1, 1, "EE.ANDQ q1, q4, q4"
HAZ ex04_ldqr_dep_d2, 2, "EE.ANDQ q1, q0, q0"
HAZ ex04_ldqr_ind_d2, 2, "EE.ANDQ q1, q4, q4"
