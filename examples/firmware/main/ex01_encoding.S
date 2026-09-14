/* ex01 -- what the PIE memory forms do to their address operand, and the accumulator readout.
 *
 * The TRM and the assembler disagree about `EE.ST.ACCX.IP`'s immediate: the syntax line prints
 * "-512..508" (a 4-byte step over an 8-bit field), the assembler refuses anything that is not a multiple
 * of 8, and the operation pseudo-code prints "as += imm8" with no shift at all (while the LD form next to
 * it prints "{21{imm8[7]},imm8[7:0],3{0}}", a step of 8). Reading cannot settle that; the address register
 * can. Each function runs one instruction and returns (address_after - address_before).
 *
 * The last two functions use the *raw* encoding the manual's own diagram gives, so a value the assembler
 * refuses can still be put on the wire:
 *
 *   EE.LD.ACCX.IP diagram: 0 | imm8[7] | 0011100 | imm8[6:0] | as[3:0] | 0100
 *     as = a3 (0011), field = 1  ->  00001110 00000001 00110100 = 0x0E0134 -> memory order 34 01 0E
 *     (tools/asm_toolchain.py rebuilds this, and objdump decodes the bytes back as "a3, 8")
 *   EE.ST.ACCX.IP diagram: 0 | imm8[7] | 0000100 | imm8[6:0] | as[3:0] | 0100 -> 0x020134 -> 34 01 02
 *
 * So the raw form asks the hardware the question "is the field a multiple of 8 (the assembler) or of 4
 * (the printed range) or of 1 (the printed pseudo-code)?" with the exact same field value in all three
 * readings.
 */
    .section .iram1,"ax",@progbits
    .align 4

/* uint32_t f(void *p): a3 = p; one PIE instruction with an immediate; return a3_after - a3_before. */
.macro STEP_AS name, insn, imm
    .align 4
    .global \name
    .type \name,@function
\name:
    entry a1, 32
    mov a3, a2
    \insn a3, \imm
    sub a2, a3, a2
    retw.n
    .size \name, .-\name
.endm

/* Same, for the two-operand 128-bit forms: load into q0 from a3 + imm. */
.macro STEP_QU name, insn, imm
    .align 4
    .global \name
    .type \name,@function
\name:
    entry a1, 32
    mov a3, a2
    \insn q0, a3, \imm
    sub a2, a3, a2
    retw.n
    .size \name, .-\name
.endm

STEP_AS ex01_ld_accx_step_0,   EE.LD.ACCX.IP, 0
STEP_AS ex01_ld_accx_step_16,  EE.LD.ACCX.IP, 16
STEP_AS ex01_ld_accx_step_32,  EE.LD.ACCX.IP, 32
STEP_AS ex01_ld_accx_step_m16, EE.LD.ACCX.IP, -16
STEP_AS ex01_st_accx_step_0,   EE.ST.ACCX.IP, 0
STEP_AS ex01_st_accx_step_16,  EE.ST.ACCX.IP, 16
STEP_AS ex01_st_accx_step_32,  EE.ST.ACCX.IP, 32
STEP_QU ex01_ld128_step_0,     EE.LD.128.USAR.IP, 0
STEP_QU ex01_ld128_step_16,    EE.LD.128.USAR.IP, 16
STEP_QU ex01_ld128_step_32,    EE.LD.128.USAR.IP, 32
STEP_QU ex01_vld128_step_0,    EE.VLD.128.IP, 0
STEP_QU ex01_vld128_step_16,   EE.VLD.128.IP, 16
STEP_QU ex01_st128_step_16,    EE.VST.128.IP, 16

/* The same field value (imm8 = 1) as a raw instruction word, so the hardware answers which scale it uses.
 * .byte is memory order; the word is 0x0E0134 / 0x020134 as the manual's diagram lays it out. */
    .align 4
    .global ex01_raw_ld_accx_field1
    .type ex01_raw_ld_accx_field1,@function
ex01_raw_ld_accx_field1:
    entry a1, 32
    mov a3, a2
    .byte 0x34, 0x01, 0x0e          /* EE.LD.ACCX.IP a3, <field 1> */
    sub a2, a3, a2
    retw.n
    .size ex01_raw_ld_accx_field1, .-ex01_raw_ld_accx_field1

    .align 4
    .global ex01_raw_st_accx_field1
    .type ex01_raw_st_accx_field1,@function
ex01_raw_st_accx_field1:
    entry a1, 32
    mov a3, a2
    .byte 0x34, 0x01, 0x02          /* EE.ST.ACCX.IP a3, <field 1> */
    sub a2, a3, a2
    retw.n
    .size ex01_raw_st_accx_field1, .-ex01_raw_st_accx_field1

/* int32_t ex01_srs_accx(const void *value64, uint32_t shift)
 *   ACCX = *(int64 *)value64                     (EE.LD.ACCX.IP uses the low 40 bits)
 *   a2   = saturate_to_int32(ACCX >> (shift & 63))   (EE.SRS.ACCX, shift taken from a register)
 * Note the shift amount is a *register*: `EE.SRS.ACCX au, as, 0` shifts by as[5:0]. */
    .align 4
    .global ex01_srs_accx
    .type ex01_srs_accx,@function
ex01_srs_accx:
    entry a1, 32
    mov a4, a2                      /* a4 = pointer to the 64-bit value */
    mov a5, a3                      /* a5 = shift amount register */
    EE.LD.ACCX.IP a4, 0
    EE.SRS.ACCX a2, a5, 0
    retw.n
    .size ex01_srs_accx, .-ex01_srs_accx

/* void ex01_bitrev(uint32_t as_value, uint32_t *out4)
 *   EE.BITREV q0, a5 : the diagram names the operand an address register, the pseudo-code uses as[15:0] as
 *   a value and reads data X (gated by the special register FFT_BIT_WIDTH). Passing a valid buffer address
 *   as the value is safe under either reading; the returned words say which one the hardware implemented. */
    .align 4
    .global ex01_bitrev
    .type ex01_bitrev,@function
ex01_bitrev:
    entry a1, 32
    mov a5, a2
    mov a6, a3
    EE.BITREV q0, a5
    EE.VST.128.IP q0, a6, 0
    retw.n
    .size ex01_bitrev, .-ex01_bitrev
