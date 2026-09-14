/* Scratch diagnostic for the PIE timing experiment -- isolates which instruction faults.
 *
 * Not part of the experiment: delete once the cause is known.
 *
 * ABI: these are called through a function pointer (call8 rotates the register window), so each one
 * must pair ENTRY with RETW. A bare `ret` leaves the caller's window rotated and it then runs on the
 * callee's registers -- which is what made the first measurement firmware fault at address 0.
 */
    .text
    .align 4

/* uint32_t probe_cpenable(void) -- raw CPENABLE; bit 3 is the PIE/ACCX coprocessor (tie.h: CP3 cop_ai) */
    .align 4
    .global probe_cpenable
    .type probe_cpenable,@function
probe_cpenable:
    entry a1, 32
    rsr a2, 0x0e0
    retw.n
    .size probe_cpenable, .-probe_cpenable

/* uint32_t probe_ccount(void) -- raw CCOUNT, so the probe can time what it can run */
    .align 4
    .global probe_ccount
    .type probe_ccount,@function
probe_ccount:
    entry a1, 32
    rsr a2, 234
    retw.n
    .size probe_ccount, .-probe_ccount

/* void probe_andq_loop(void) -- PIE ALU only, no memory access */
    .align 4
    .global probe_andq_loop
    .type probe_andq_loop,@function
probe_andq_loop:
    entry a1, 32
    movi a6, 100
.Lpandq:
    EE.ANDQ q2, q0, q1
    addi a6, a6, -1
    bnez a6, .Lpandq
    retw.n
    .size probe_andq_loop, .-probe_andq_loop

/* void probe_ld_accx_once(void *p) -- one EE.LD.ACCX.IP from a valid, 16-byte aligned buffer */
    .align 4
    .global probe_ld_accx_once
    .type probe_ld_accx_once,@function
probe_ld_accx_once:
    entry a1, 32
    mov a3, a2
    EE.LD.ACCX.IP a3, 0
    retw.n
    .size probe_ld_accx_once, .-probe_ld_accx_once

/* void probe_st_accx_once(void *p) */
    .align 4
    .global probe_st_accx_once
    .type probe_st_accx_once,@function
probe_st_accx_once:
    entry a1, 32
    mov a3, a2
    EE.ST.ACCX.IP a3, 16
    retw.n
    .size probe_st_accx_once, .-probe_st_accx_once

/* void probe_ldqr_once(void *p) -- LD.QR, the instruction this experiment exists to measure */
    .align 4
    .global probe_ldqr_once
    .type probe_ldqr_once,@function
probe_ldqr_once:
    entry a1, 32
    mov a3, a2
    LD.QR q0, a3, 0
    retw.n
    .size probe_ldqr_once, .-probe_ldqr_once

/* void probe_ld_accx_16loop(void *p) -- the generated sequence, bounded to stay inside the buffer */
    .align 4
    .global probe_ld_accx_16loop
    .type probe_ld_accx_16loop,@function
probe_ld_accx_16loop:
    entry a1, 32
    mov a3, a2
    movi a6, 16
.Lp16:
    EE.LD.ACCX.IP a3, 0
    EE.ST.ACCX.IP a3, 16
    addi a6, a6, -1
    bnez a6, .Lp16
    retw.n
    .size probe_ld_accx_16loop, .-probe_ld_accx_16loop

/* uint32_t probe_ld_accx_loop(void *p) -- 2000 iterations with the address rewound every 16, so the
 * sequence the experiment uses can run without walking out of the buffer. */
    .align 4
    .global probe_ld_accx_loop
    .type probe_ld_accx_loop,@function
probe_ld_accx_loop:
    entry a1, 32
    movi a6, 2000
    rsr.ccount a7
.Lp2000:
    mov a3, a2
    EE.LD.ACCX.IP a3, 0
    EE.ST.ACCX.IP a3, 16
    addi a6, a6, -1
    bnez a6, .Lp2000
    rsr.ccount a2
    sub a2, a2, a7
    retw.n
    .size probe_ld_accx_loop, .-probe_ld_accx_loop
