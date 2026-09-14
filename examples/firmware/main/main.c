/* PIE examples -- a runnable tour of the ESP32-S3's extended (PIE) instruction set.
 *
 * Each example runs a hand-written kernel from the .S files next to this one and checks it against a
 * scalar reference computed here in C, printing one line per check:
 *
 *   EX <n> <name> BEGIN
 *   EX <n> <name> DATA   <key>=<hex>,<hex>,...      inputs or results, for the host-side checker as well
 *   EX <n> <name> CHECK  <label> pie=<value> ref=<value> ok|FAIL
 *   EX <n> <name> RESULT ok=<n> fail=<n>
 *   END
 *
 * The point is that nothing here has to be believed: the C reference is written from the same manual
 * pseudo-code the assembly was written from, the host-side checker (tools/check_examples_log.py) re-derives
 * every value a third time in Python, and the examples that *cannot* be checked from the manual (the
 * address steps, the LD.QR interlock, the accumulator clamps) compare against the two competing readings
 * and say which one the silicon matches.
 *
 * All PIE buffers are 16-byte aligned: the 128-bit forms force the low address bits to 0 (TRM p49), so a
 * misaligned pointer silently reads or writes neighbouring bytes instead of faulting.
 */
#include <stdio.h>
#include <string.h>
#include <inttypes.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_chip_info.h"
#include "esp_timer.h"
#include "esp_idf_version.h"

#include "examples.h"

#define BUF_BYTES 1024
static int16_t s_a[16 * 16] __attribute__((aligned(16)));
static int16_t s_bt[16 * 16] __attribute__((aligned(16)));
static int32_t s_c[16 * 16] __attribute__((aligned(16)));
static int32_t s_c_ref[16 * 16] __attribute__((aligned(16)));
static int16_t s_x[64] __attribute__((aligned(16)));
static int16_t s_h[16] __attribute__((aligned(16)));
static int32_t s_y[64] __attribute__((aligned(16)));
static int32_t s_y_ref[64] __attribute__((aligned(16)));
static uint8_t s_src[BUF_BYTES] __attribute__((aligned(16)));
static uint8_t s_dst[BUF_BYTES] __attribute__((aligned(16)));
static int16_t s_window[16] __attribute__((aligned(16)));
#define PIXELS 1024
static int16_t s_v[32] __attribute__((aligned(16)));
static int16_t s_vout[32] __attribute__((aligned(16)));
static int16_t s_vref[32] __attribute__((aligned(16)));
static int16_t s_pa[PIXELS] __attribute__((aligned(16)));
static int16_t s_pb[PIXELS] __attribute__((aligned(16)));
static int16_t s_po[PIXELS] __attribute__((aligned(16)));
static int16_t s_pref[PIXELS] __attribute__((aligned(16)));
static int16_t s_lo8[8] __attribute__((aligned(16)));
static int16_t s_hi8[8] __attribute__((aligned(16)));
static int16_t s_tint8[8] __attribute__((aligned(16)));
static int16_t s_mask8[8] __attribute__((aligned(16)));
static int16_t s_ones8[8] __attribute__((aligned(16)));
static int16_t s_sign_s[8] __attribute__((aligned(16)));
static int16_t s_sign_u[8] __attribute__((aligned(16)));
static int16_t s_sign_s_ref[8] __attribute__((aligned(16)));
static int16_t s_sign_u_ref[8] __attribute__((aligned(16)));
static int32_t s_out2[2] __attribute__((aligned(16)));
static int16_t s_lanes8[8] __attribute__((aligned(16)));
static uint32_t s_words[4] __attribute__((aligned(16)));
/* ex09 (the QACC probe): the readout buffers (the vectors themselves are consts in ex09()). */
static int16_t s_out9[32] __attribute__((aligned(16)));
static int16_t s_out9b[32] __attribute__((aligned(16)));
static int16_t s_wb_a[8] __attribute__((aligned(16)));
static int16_t s_wb_b[8] __attribute__((aligned(16)));
static uint32_t s_qraw[16] __attribute__((aligned(16)));
static uint32_t s_qrows[64] __attribute__((aligned(16)));

static int s_ok, s_fail;

/* ------------------------------------------------------------------ helpers */

static void section_begin(const char *ex, const char *name)
{
    printf("EX %s %s BEGIN\n", ex, name);
    fflush(stdout);
}

static void section_end(const char *ex, const char *name, int ok, int fail)
{
    printf("EX %s %s RESULT ok=%d fail=%d\n", ex, name, ok, fail);
    fflush(stdout);
    s_ok += ok;
    s_fail += fail;
}

static void check(const char *ex, const char *name, const char *label, int64_t pie, int64_t ref)
{
    printf("EX %s %s CHECK %s pie=%" PRId64 " ref=%" PRId64 " %s\n", ex, name, label, pie, ref,
           pie == ref ? "ok" : "FAIL");
    fflush(stdout);
}

static void print_i16(const char *ex, const char *name, const char *key, const int16_t *v, int n)
{
    printf("EX %s %s DATA %s=", ex, name, key);
    for (int i = 0; i < n; i++) {
        printf("%s%04x", i ? "," : "", (unsigned)(uint16_t)v[i]);
    }
    printf("\n");
    fflush(stdout);
}

static void print_i32(const char *ex, const char *name, const char *key, const int32_t *v, int n)
{
    printf("EX %s %s DATA %s=", ex, name, key);
    for (int i = 0; i < n; i++) {
        printf("%s%08" PRIx32, i ? "," : "", (uint32_t)v[i]);
    }
    printf("\n");
    fflush(stdout);
}

/* A tiny deterministic generator, so a log can be reproduced from the seed alone. */
static uint32_t s_rng;
static int16_t rnd16(int lo, int hi)
{
    s_rng = s_rng * 1664525u + 1013904223u;
    return (int16_t)(lo + (int)((s_rng >> 16) % (uint32_t)(hi - lo + 1)));
}

static int32_t sat32(int64_t v)
{
    if (v > 2147483647LL) {
        return 2147483647;
    }
    if (v < -2147483648LL) {
        return -2147483648;
    }
    return (int32_t)v;
}

static int16_t sat16(int64_t v)
{
    if (v > 32767) {
        return 32767;
    }
    if (v < -32768) {
        return -32768;
    }
    return (int16_t)v;
}

static int16_t trunc16(int64_t v)
{
    /* EE.VMUL truncates the product (it does not saturate), so the reference wraps the same way: the low 16
     * bits of the shifted product, sign interpreted. Reading it as a saturation would hide the difference. */
    return (int16_t)((uint32_t)v & 0xFFFFu);
}

static int64_t sat40(int64_t v)
{
    return v > 0x7FFFFFFFFFLL ? 0x7FFFFFFFFFLL : (v < -0x8000000000LL ? -0x8000000000LL : v);
}

#define PIE16 __attribute__((aligned(16)))

/* A buffer handed to a PIE kernel has to start on a 16-byte boundary: the 128-bit forms force the low four
 * address bits to 0 (TRM p49), so a misaligned array is not reported -- it is read from the boundary below
 * it, which shifts the lanes by (pointer & 15) / 2 elements. The first run of ex07 and the ex09 probe proved
 * this from the C side: `static const int16_t` arrays landed 4 bytes off the grid and every value came back
 * two lanes to the right of the model. The checks below make that a reported failure instead of a mystery. */
static int aligned16(const void *p)
{
    return ((uintptr_t)p & 15u) == 0;
}

static void check_aligned(const char *ex, const char *name, const char *what, const void *p, int *fail)
{
    int ok = aligned16(p);
    check(ex, name, what, ok, 1);
    *fail += ok ? 0 : 1;
}

/* The C reference implementations the examples are timed against. They are deliberately not `static`: a
 * non-static function writing to a global cannot be reasoned away by the optimiser, so the timed loop is
 * actually executed instead of being hoisted out of the measurement. */
void c_transform8(const int16_t *m, const int16_t *v, int16_t *out, uint32_t shift)
{
    for (int r = 0; r < 4; r++) {
        for (int j = 0; j < 8; j++) {
            int64_t acc = 0;
            for (int k = 0; k < 4; k++) {
                acc += (int32_t)m[r * 8 + k] * (int32_t)v[k * 8 + j];   /* rows are padded to 8 lanes */
            }
            out[r * 8 + j] = sat16(acc >> shift);
        }
    }
}

void c_half_blend(const int16_t *a, const int16_t *b, int16_t *out, int n)
{
    /* The lanes are packed RGB565, i.e. unsigned (the kernels do the >>1 with EE.VMUL.U16 for exactly this
     * reason: EE.VMUL.S16 would sign-extend a lane whose bit 15 is set). */
    for (int i = 0; i < n; i++) {
        out[i] = (int16_t)((((uint16_t)a[i] & 0xF7DEu) >> 1) + (((uint16_t)b[i] & 0xF7DEu) >> 1));
    }
}

void c_brighten(const int16_t *a, const int16_t *b, int16_t *out, int n)
{
    for (int i = 0; i < n; i++) {
        out[i] = sat16((int32_t)a[i] + (int32_t)b[i]);
    }
}

void c_clamp(const int16_t *a, int16_t lo, int16_t hi, int16_t *out, int n)
{
    for (int i = 0; i < n; i++) {
        int16_t v = a[i];
        out[i] = v < lo ? lo : (v > hi ? hi : v);
    }
}

/* ------------------------------------------------------------------ ex01: address steps and accumulator readout */

static void ex01(void)
{
    const char *ex = "ex01", *name = "encoding";
    section_begin(ex, name);

    memset(s_src, 0, sizeof(s_src));
    for (int i = 0; i < BUF_BYTES; i++) {
        s_src[i] = (uint8_t)(i * 7 + 1);
    }

    /* What does each memory form add to its address register for a *printed* immediate? The assembler
     * encodes imm/8 for the ACCX forms and imm/16 for the 128-bit forms, so a delta equal to the printed
     * immediate means the hardware's step matches the assembler's scale; half of it would mean the
     * manual's printed range (a 4-byte step for ST.ACCX.IP) is what the silicon does. */
    struct {
        const char *label;
        int32_t got, printed;
    } steps[] = {
        {"ld_accx+0", (int32_t)ex01_ld_accx_step_0(s_src), 0},
        {"ld_accx+16", (int32_t)ex01_ld_accx_step_16(s_src), 16},
        {"ld_accx+32", (int32_t)ex01_ld_accx_step_32(s_src), 32},
        {"ld_accx-16", (int32_t)ex01_ld_accx_step_m16(s_src), -16},
        {"st_accx+0", (int32_t)ex01_st_accx_step_0(s_src), 0},
        {"st_accx+16", (int32_t)ex01_st_accx_step_16(s_src), 16},
        {"st_accx+32", (int32_t)ex01_st_accx_step_32(s_src), 32},
        {"ld128+0", (int32_t)ex01_ld128_step_0(s_src), 0},
        {"ld128+16", (int32_t)ex01_ld128_step_16(s_src), 16},
        {"ld128+32", (int32_t)ex01_ld128_step_32(s_src), 32},
        {"vld128+0", (int32_t)ex01_vld128_step_0(s_src), 0},
        {"vld128+16", (int32_t)ex01_vld128_step_16(s_src), 16},
        {"vst128+16", (int32_t)ex01_st128_step_16(s_src), 16},
    };
    int ok = 0, fail = 0;
    for (size_t i = 0; i < sizeof(steps) / sizeof(steps[0]); i++) {
        printf("EX %s %s DATA step_%s=%" PRId32 "\n", ex, name, steps[i].label, steps[i].got);
        fflush(stdout);
        if (steps[i].got == steps[i].printed) {
            ok++;
        } else {
            fail++;
        }
    }
    printf("EX %s %s DATA step_summary ok=%d mismatch=%d (a value equal to the printed immediate means "
           "the step follows the assembler)\n", ex, name, ok, fail);
    fflush(stdout);

    /* The decisive one: field = 1, written as raw bytes because the assembler refuses it. The pseudo-code
     * says the LD form adds {21{imm8[7]},imm8[7:0],3{0}} = 8 per field unit; the ST form's printed syntax
     * range says 4, and its printed pseudo-code says 1. */
    uint32_t raw_ld = ex01_raw_ld_accx_field1(s_src);
    uint32_t raw_st = ex01_raw_st_accx_field1(s_src);
    printf("EX %s %s DATA raw_field1_ld=%" PRIu32 "\n", ex, name, raw_ld);
    printf("EX %s %s DATA raw_field1_st=%" PRIu32 "\n", ex, name, raw_st);
    printf("EX %s %s HYPOTHESIS ld: 8=pseudo-code, 4=?, 1=?\n", ex, name);
    printf("EX %s %s HYPOTHESIS st: 8=assembler, 4=printed range, 1=printed pseudo-code\n", ex, name);
    fflush(stdout);
    check(ex, name, "raw_ld_field1_follows_pseudocode", raw_ld, 8);
    check(ex, name, "raw_st_field1_follows_assembler", raw_st, 8);

    /* EE.SRS.ACCX: ACCX = the value, then sat32(ACCX >> shift). */
    int64_t value = ((int64_t)1 << 38) + 12345;      /* 40-bit accumulator, positive */
    int32_t got0 = ex01_srs_accx(&value, 0);
    int32_t got8 = ex01_srs_accx(&value, 8);
    check(ex, name, "srs_accx_shift0_saturates_to_int32", got0, sat32(value));
    check(ex, name, "srs_accx_shift8", got8, sat32(value >> 8));
    int64_t neg = -((int64_t)1 << 38) - 999;
    check(ex, name, "srs_accx_negative_shift5", ex01_srs_accx(&neg, 5), sat32(neg >> 5));

    /* EE.BITREV: does the operand act as an address (the diagram) or as a value (the pseudo-code)?
     * Two different values are passed as the address register's contents; the printed words say which
     * reading the hardware used, and the host-side checker interprets them. */
    memset(s_words, 0, sizeof(s_words));
    ex01_bitrev((uint32_t)(uintptr_t)s_src, s_words);
    printf("EX %s %s DATA bitrev_%08" PRIx32 "=%08" PRIx32 ",%08" PRIx32 ",%08" PRIx32 ",%08" PRIx32 "\n",
           ex, name, (uint32_t)(uintptr_t)s_src, s_words[0], s_words[1], s_words[2], s_words[3]);
    ex01_bitrev((uint32_t)(uintptr_t)(s_src + 16), s_words);
    printf("EX %s %s DATA bitrev_%08" PRIx32 "=%08" PRIx32 ",%08" PRIx32 ",%08" PRIx32 ",%08" PRIx32 "\n",
           ex, name, (uint32_t)(uintptr_t)(s_src + 16), s_words[0], s_words[1], s_words[2], s_words[3]);
    fflush(stdout);

    section_end(ex, name, ok, fail);
}

/* ------------------------------------------------------------------ ex02: 16x16 matrix multiply */

static void ex02(void)
{
    const char *ex = "ex02", *name = "matmul16";
    section_begin(ex, name);

    s_rng = 0x1234;
    int16_t b[16 * 16] __attribute__((aligned(16)));
    for (int i = 0; i < 256; i++) {
        s_a[i] = rnd16(-100, 100);
        b[i] = rnd16(-100, 100);
    }
    for (int i = 0; i < 16; i++) {                    /* Bt = transpose(B) */
        for (int j = 0; j < 16; j++) {
            s_bt[i * 16 + j] = b[j * 16 + i];
        }
    }
    for (int i = 0; i < 16; i++) {                    /* scalar reference */
        for (int j = 0; j < 16; j++) {
            int64_t acc = 0;
            for (int k = 0; k < 16; k++) {
                acc += (int32_t)s_a[i * 16 + k] * (int32_t)s_bt[j * 16 + k];
            }
            s_c_ref[i * 16 + j] = sat32(acc);
        }
    }

    int64_t t0 = esp_timer_get_time();
    ex02_matmul16(s_a, s_bt, s_c);
    int64_t t1 = esp_timer_get_time();

    int ok = 0, fail = 0;
    for (int i = 0; i < 256; i++) {
        if (s_c[i] == s_c_ref[i]) {
            ok++;
        } else {
            fail++;
            if (fail <= 3) {
                printf("EX %s %s DATA first_mismatch index=%d pie=%" PRId32 " ref=%" PRId32 " A_row=%d Bt_row=%d\n",
                       ex, name, i, s_c[i], s_c_ref[i], i / 16, i % 16);
            }
        }
    }
    print_i16(ex, name, "a", s_a, 256);
    print_i16(ex, name, "bt", s_bt, 256);
    print_i32(ex, name, "c", s_c, 256);
    printf("EX %s %s DATA cycles_us=%" PRId64 "\n", ex, name, t1 - t0);
    fflush(stdout);
    check(ex, name, "elements_matching_reference", ok, 256);
    section_end(ex, name, ok == 256 ? 1 : 0, fail);
}

/* ------------------------------------------------------------------ ex03: 16-tap FIR */

static void ex03(void)
{
    const char *ex = "ex03", *name = "fir16";
    section_begin(ex, name);

    s_rng = 0xbeef;
    for (int i = 0; i < 64; i++) {
        s_x[i] = rnd16(-3000, 3000);
    }
    for (int i = 0; i < 16; i++) {
        s_h[i] = (int16_t)(1000 - i * 90);            /* a plausible Q15 taper, small enough to be exact */
    }
    const int n_out = 49;

    int ok_total = 0, fail_total = 0;
    for (int variant = 0; variant < 2; variant++) {
        uint32_t shift = variant == 0 ? 0 : 15;
        for (int i = 0; i < n_out; i++) {
            int64_t acc = 0;
            for (int k = 0; k < 16; k++) {
                acc += (int32_t)s_x[i + k] * (int32_t)s_h[k];
            }
            s_y_ref[i] = sat32(acc >> shift);
            /* Every 128-bit PIE access drops the low four address bits (TRM p49), so the sliding window
             * has to reach the kernel on the 16-byte grid. The first run of the version that loaded
             * `x + 2*n` directly is what produced this file's history: 42 of 49 outputs came back as y[0]. */
            memcpy(s_window, &s_x[i], 16 * sizeof(int16_t));
            s_y[i] = ex03_fir16_tap(s_window, s_h, shift);
        }
        int ok = 0, fail = 0;
        for (int i = 0; i < n_out; i++) {
            if (s_y[i] == s_y_ref[i]) {
                ok++;
            } else {
                fail++;
                if (fail <= 3) {
                    printf("EX %s %s DATA first_mismatch shift=%" PRIu32 " n=%d pie=%" PRId32 " ref=%" PRId32 "\n",
                           ex, name, shift, i, s_y[i], s_y_ref[i]);
                }
            }
        }
        print_i32(ex, name, variant == 0 ? "y_raw" : "y_q15", s_y, n_out);
        printf("EX %s %s DATA shift=%" PRIu32 " matched=%d of %d\n", ex, name, shift, ok, n_out);
        fflush(stdout);
        ok_total += ok;
        fail_total += fail;
    }
    print_i16(ex, name, "x", s_x, 64);
    print_i16(ex, name, "h", s_h, 16);
    fflush(stdout);
    check(ex, name, "samples_matching_reference", ok_total, 2 * n_out);

    /* The failure that shaped this file, kept as a measurement: a 128-bit load from x+2 returns the bytes
     * at x, because the hardware forms the address as {as[31:4], 4{0}} (TRM p49). */
    int16_t probe[16] __attribute__((aligned(16)));
    ex03_align_probe(s_x, probe);
    int align_same = memcmp(probe, probe + 8, 16) == 0;
    print_i16(ex, name, "align_probe", probe, 16);
    check(ex, name, "vld128_at_x_plus_2_returns_the_bytes_at_x", align_same ? 1 : 0, 1);

    /* The no-copy alternative for windows off the grid: EE.LD.128.USAR.IP leaves the dropped bits in
     * SAR_BYTE and EE.SRC.Q shifts the window out of two aligned chunks. Whichever operand order produces
     * the window starting at byte 2 is the one where the first operand is qs0 -- the syntax line does not
     * extract, so the log decides. Expected window = x[1..8] (byte offset 2), and x is 16-byte aligned. */
    int16_t funnel_ab[8] __attribute__((aligned(16))), funnel_ba[8] __attribute__((aligned(16)));
    ex03_funnel_probe(&s_x[1], &s_x[8], funnel_ab, funnel_ba);
    int ab_hit = memcmp(funnel_ab, &s_x[1], 16) == 0;
    int ba_hit = memcmp(funnel_ba, &s_x[1], 16) == 0;
    print_i16(ex, name, "funnel_ab", funnel_ab, 8);
    print_i16(ex, name, "funnel_ba", funnel_ba, 8);
    print_i16(ex, name, "funnel_expected", &s_x[1], 8);
    printf("EX %s %s DATA funnel_verdict ab=%d ba=%d\n", ex, name, ab_hit, ba_hit);
    fflush(stdout);
    check(ex, name, "src_q_one_operand_order_gives_the_window_at_byte_2", (ab_hit || ba_hit) ? 1 : 0, 1);
    check(ex, name, "src_q_the_two_operand_orders_differ", (funnel_ab[0] == funnel_ba[0]) ? 0 : 1, 1);
    section_end(ex, name, fail_total == 0 ? 1 : 0, fail_total);
}

/* ------------------------------------------------------------------ ex04: QR transfers and the LD.QR interlock */

static void ex04(void)
{
    const char *ex = "ex04", *name = "qr";
    section_begin(ex, name);

    memset(s_src, 0, sizeof(s_src));
    memset(s_dst, 0, sizeof(s_dst));
    for (int i = 0; i < BUF_BYTES; i++) {
        s_src[i] = (uint8_t)(i ^ 0x5a);
    }
    ex04_qr_copy(s_dst, s_src, 256);
    int copy_ok = memcmp(s_dst, s_src, 256) == 0;

    memset(s_dst, 0, sizeof(s_dst));
    ex04_qr_move_roundtrip(s_dst, s_src);
    int move_ok = memcmp(s_dst, s_src, 16) == 0;

    check(ex, name, "qr_copy_256_bytes", copy_ok ? 1 : 0, 1);
    check(ex, name, "qr_move_roundtrip_16_bytes", move_ok ? 1 : 0, 1);

    /* The interlock: dep reads what LD.QR wrote, ind reads q4. Two thousand iterations each, so the
     * per-iteration difference is resolved well below a cycle. */
    const int iters = 2000;
    struct {
        const char *label;
        uint32_t dep, ind;
    } haz[] = {
        {"d0", ex04_ldqr_dep_d0(s_src), ex04_ldqr_ind_d0(s_src)},
        {"d1", ex04_ldqr_dep_d1(s_src), ex04_ldqr_ind_d1(s_src)},
        {"d2", ex04_ldqr_dep_d2(s_src), ex04_ldqr_ind_d2(s_src)},
    };
    int ok = 0, fail = 0;
    for (size_t i = 0; i < sizeof(haz) / sizeof(haz[0]); i++) {
        double stall = ((double)haz[i].dep - (double)haz[i].ind) / iters;
        /* The measured staging says LD.QR writes at M (stage 2) and the consumer reads at E (stage 1), so
         * the interlock is 1 cycle with no filler and 0 once one instruction separates them. */
        int expect = strcmp(haz[i].label, "d0") == 0 ? 1 : 0;
        int got = (stall > 0.5) ? 1 : 0;
        printf("EX %s %s DATA interlock_%s stall=%.3f dep=%" PRIu32 " ind=%" PRIu32 " iters=%d\n",
               ex, name, haz[i].label, stall, haz[i].dep, haz[i].ind, iters);
        fflush(stdout);
        check(ex, name, haz[i].label, got, expect);
        if (got == expect) {
            ok++;
        } else {
            fail++;
        }
    }
    section_end(ex, name, ok, fail);
}

/* ------------------------------------------------------------------ ex05: the accumulator's edges */

static void ex05(void)
{
    const char *ex = "ex05", *name = "saturation";
    section_begin(ex, name);

    for (int i = 0; i < 8; i++) {
        s_lanes8[i] = 32000;
    }
    const int iterations = 200;
    ex05_accx_saturation(iterations, s_lanes8, s_out2);

    /* Reference: 8 products of 32000*32000 = 8.192e9 per instruction, clamped into the 40-bit
     * accumulator the manual documents (-2^39 .. 2^39-1). */
    const int64_t lane_product = 32000LL * 32000LL;
    const int64_t per_iteration = 8 * lane_product;
    const int64_t acc_max = (1LL << 39) - 1;
    int64_t acc = 0;
    for (int i = 0; i < iterations; i++) {
        acc += per_iteration;
        if (acc > acc_max) {
            acc = acc_max;
        }
    }
    printf("EX %s %s DATA mathematical_sum=%" PRId64 "\n", ex, name, per_iteration * iterations);
    printf("EX %s %s DATA clamped_40bit=%" PRId64 "\n", ex, name, acc);
    printf("EX %s %s DATA per_iteration=%" PRId64 "\n", ex, name, per_iteration);
    printf("EX %s %s DATA readout_sat32=%" PRId32 "\n", ex, name, s_out2[0]);
    printf("EX %s %s DATA readout_shifted9=%" PRId32 "\n", ex, name, s_out2[1]);
    fflush(stdout);
    check(ex, name, "accx_40bit_clamp", s_out2[1], acc >> 9);
    check(ex, name, "srs_accx_int32_clamp", s_out2[0], sat32(acc));
    check(ex, name, "clamped_below_mathematical_sum", acc < per_iteration * iterations ? 1 : 0, 1);
    section_end(ex, name, 3, (s_out2[0] == sat32(acc) && s_out2[1] == (acc >> 9)) ? 0 : 1);
}

/* ------------------------------------------------------------------ ex06: the FFT primitives */

static void ex06(void)
{
    const char *ex = "ex06", *name = "fft";
    section_begin(ex, name);

    int16_t x[8] __attribute__((aligned(16))) = {100, -200, 300, -400, 500, -600, 700, -800};
    int16_t out[8] __attribute__((aligned(16)));
    int ok = 0, fail = 0;

    /* sel2 = 0: qa0 low half = x[0..3] + x[4..7], high half = x[0..3] - x[4..7] (op_a is {qy[63:0],
     * qx[63:0]} and op_b is {qy[127:64], qx[127:64]}, and here qx = qy = x). */
    ex06_r2bf_sel0(x, out);
    int ok0 = 0;
    for (int i = 0; i < 4; i++) {
        int16_t ref_sum = (int16_t)(x[i] + x[i + 4]);
        int16_t ref_dif = (int16_t)(x[i] - x[i + 4]);
        printf("EX %s %s DATA r2bf_sel0_lane%d=%04x\n", ex, name, i, (unsigned)(uint16_t)out[i]);
        if (out[i] == ref_sum) {
            ok0++;
        }
        if (out[i + 4] == ref_dif) {
            ok0++;
        }
    }
    print_i16(ex, name, "r2bf_sel0_out", out, 8);
    check(ex, name, "r2bf_sel0_lanes", ok0, 8);
    fflush(stdout);
    ok += ok0 == 8;
    fail += ok0 == 8 ? 0 : 1;

    /* sel2 = 1: the manual writes its operand lists MSB first -- the leftmost field occupies the highest
     * lanes. The first silicon run settled that reading: {qy[95:64], qy[31:0], qx[95:64], qx[31:0]} puts
     * {qx0,x1} in lanes 0..1, {qx4,x5} in lanes 2..3, {qy0,qy1} in lanes 4..5, {qy4,qy5} in lanes 6..7.
     * (Reading it the other way round swaps the two halves of the result, which is exactly what the log
     * showed before this line was corrected.) */
    ex06_r2bf_sel1(x, out);
    int16_t a1[8] = {x[0], x[1], x[4], x[5], x[0], x[1], x[4], x[5]};
    int16_t b1[8] = {x[2], x[3], x[6], x[7], x[2], x[3], x[6], x[7]};
    int ok1 = 0;
    for (int i = 0; i < 4; i++) {
        check(ex, name, "r2bf_sel1_sum", out[i], (int16_t)(a1[i] + b1[i]));
        check(ex, name, "r2bf_sel1_dif", out[i + 4], (int16_t)(a1[i] - b1[i]));
        ok1 += (out[i] == (int16_t)(a1[i] + b1[i]));
        ok1 += (out[i + 4] == (int16_t)(a1[i] - b1[i]));
    }
    print_i16(ex, name, "r2bf_sel1_out", out, 8);
    ok += ok1 == 8;
    fail += ok1 == 8 ? 0 : 1;
    fflush(stdout);

    /* EE.CMUL.S16: two complex multiplies on interleaved (re,im) lanes, result shifted right by SAR.
     * sel4 = 0 writes lanes 0..3, sel4 = 1 writes lanes 4..7 (the other half is left untouched). */
    int16_t u[8] PIE16 = {1000, 2000, 3000, -4000, 7000, 8000, 9000, -10000};
    int16_t v[8] PIE16 = {500, -600, 700, 800, -900, 1000, 1100, 1200};
    for (uint32_t sar = 0; sar <= 12; sar += 12) {
        int16_t w[8] PIE16 = {0, 0, 0, 0, 0, 0, 0, 0};
        ex06_cmul_half0(u, v, w, sar);
        for (int pair = 0; pair < 2; pair++) {
            int re = u[pair * 2], im = u[pair * 2 + 1];
            int cre = v[pair * 2], cim = v[pair * 2 + 1];
            check(ex, name, "cmul_re", w[pair * 2], (int16_t)((re * cre - im * cim) >> sar));
            check(ex, name, "cmul_im", w[pair * 2 + 1], (int16_t)((re * cim + im * cre) >> sar));
        }
        printf("EX %s %s DATA cmul_half0_sar%" PRIu32 "=", ex, name, sar);
        for (int i = 0; i < 4; i++) {
            printf("%s%04x", i ? "," : "", (unsigned)(uint16_t)w[i]);
        }
        printf("\n");

        int16_t w1[8] = {0, 0, 0, 0, 0, 0, 0, 0};
        ex06_cmul_half1(u, v, w1, sar);
        for (int pair = 2; pair < 4; pair++) {
            int re = u[pair * 2], im = u[pair * 2 + 1];
            int cre = v[pair * 2], cim = v[pair * 2 + 1];
            check(ex, name, "cmul1_re", w1[pair * 2], (int16_t)((re * cre - im * cim) >> sar));
            check(ex, name, "cmul1_im", w1[pair * 2 + 1], (int16_t)((re * cim + im * cre) >> sar));
        }
        printf("EX %s %s DATA cmul_half1_sar%" PRIu32 "=", ex, name, sar);
        for (int i = 4; i < 8; i++) {
            printf("%s%04x", i == 4 ? "" : ",", (unsigned)(uint16_t)w1[i]);
        }
        printf("\n");
        fflush(stdout);
    }

    section_end(ex, name, ok, fail);
}

/* ------------------------------------------------------------------ ex07: 3D vertex transform */

static void ex07(void)
{
    const char *ex = "ex07", *name = "transform3d";
    section_begin(ex, name);

    /* Eight cube corners of side 1.0 in Q8 (1.0 = 256), and a rotation about Y by 30 degrees: cos = 0.866
     * -> 222 and sin = 0.5 -> 128, also Q8, so each product carries 2^16 and the readout shifts by 16. */
    /* Four coefficient rows, each padded to eight int16. `EE.VLD.128.IP` always advances by a multiple of
     * 16 bytes, so a 4-lane (8-byte) row makes the loop read every OTHER row and then walk off the array --
     * which is exactly what the first run of this example did (row 1 came back as row 2's answer, rows 2
     * and 3 read the vertex array). Lanes 4..7 of each row are padding and never read: sel8 = 0..3. */
    static const int16_t m[32] PIE16 = {222, 0, 128, 0, 0, 0, 0, 0,
                                        0, 256, 0, 0, 0, 0, 0, 0,
                                        -128, 0, 222, 0, 0, 0, 0, 0,
                                        0, 0, 0, 256, 0, 0, 0, 0};
    static const int16_t verts[8][4] PIE16 = {
        {-128, -128, -128, 256}, {128, -128, -128, 256}, {128, 128, -128, 256}, {-128, 128, -128, 256},
        {-128, -128, 128, 256}, {128, -128, 128, 256}, {128, 128, 128, 256}, {-128, 128, 128, 256}};
    for (int k = 0; k < 4; k++) {
        for (int j = 0; j < 8; j++) {
            s_v[k * 8 + j] = verts[j][k];        /* structure of arrays: what the vector kernel wants */
        }
    }

    ex07_transform8(m, s_v, s_vout, 16);
    c_transform8(m, s_v, s_vref, 16);

    int ok = 0, fail = 0;
    /* Both of these sat 4 bytes off the 16-byte grid in the first run, which shifted every lane by two
     * elements: the alignment is asserted before the kernel is asked anything. */
    check_aligned(ex, name, "matrix_16_byte_aligned", m, &fail);
    check_aligned(ex, name, "vertices_16_byte_aligned", verts, &fail);
    check_aligned(ex, name, "v_soa_16_byte_aligned", s_v, &fail);
    check_aligned(ex, name, "out_16_byte_aligned", s_vout, &fail);
    for (int i = 0; i < 32; i++) {
        if (s_vout[i] == s_vref[i]) {
            ok++;
        } else {
            fail++;
            if (fail <= 3) {
                printf("EX %s %s DATA first_mismatch index=%d pie=%d ref=%d\n", ex, name, i,
                       s_vout[i], s_vref[i]);
            }
        }
    }
    print_i16(ex, name, "matrix", m, 32);
    print_i16(ex, name, "vertices_soa", s_v, 32);
    print_i16(ex, name, "out", s_vout, 32);
    print_i16(ex, name, "ref", s_vref, 32);
    check(ex, name, "all_32_transformed_coordinates_match_C", ok, 32);

    /* Cycles per vertex: the assembly against the same arithmetic in C, built at -O2 (main/CMakeLists.txt)
     * so the comparison is instruction set against instruction set rather than against a debug build. */
    const int reps = 500;
    uint32_t t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex07_transform8(m, s_v, s_vout, 16);
    }
    uint32_t pie_cycles = ex07_ccount() - t0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        c_transform8(m, s_v, s_vref, 16);
    }
    uint32_t c_cycles = ex07_ccount() - t0;
    printf("BENCH transform8 vertices=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", reps * 8,
           pie_cycles, c_cycles);
    fflush(stdout);
    section_end(ex, name, fail == 0 ? 1 : 0, fail);
}

/* ------------------------------------------------------------------ ex08: framebuffer effects */

static void ex08(void)
{
    const char *ex = "ex08", *name = "media";
    section_begin(ex, name);

    s_rng = 0x5150;
    for (int i = 0; i < PIXELS; i++) {
        s_pa[i] = rnd16(0, 0x7fff);
        s_pb[i] = rnd16(0x2000, 0x7fff);        /* big enough that the saturating add has to clamp */
    }
    for (int i = 0; i < PIXELS; i += 7) {       /* give the clamp something to clamp */
        s_pa[i] = 30000;
        if (i + 1 < PIXELS) {
            s_pa[i + 1] = -30000;
        }
    }
    for (int i = 0; i < 8; i++) {
        s_lo8[i] = -1000;
        s_hi8[i] = 1000;
        s_tint8[i] = 300;                       /* Q8 tint: 300/256 = 1.17x */
        s_mask8[i] = (int16_t)0xF7DE;            /* the RGB565 half-blend mask */
        s_ones8[i] = 1;                          /* with SAR = 1, EE.VMUL by ones is a >>1 */
    }

    const int reps = 32;
    uint32_t t0;
    int fail = 0;

    /* half blend: the classic RGB565 mask-shift-add, five vector ops per eight pixels */
    ex08_half_blend(s_pa, s_pb, s_mask8, s_ones8, s_po, PIXELS);
    c_half_blend(s_pa, s_pb, s_pref, PIXELS);
    int hb = memcmp(s_po, s_pref, PIXELS * 2) == 0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex08_half_blend(s_pa, s_pb, s_mask8, s_ones8, s_po, PIXELS);
    }
    uint32_t hb_pie = ex07_ccount() - t0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        c_half_blend(s_pa, s_pb, s_pref, PIXELS);
    }
    uint32_t hb_c = ex07_ccount() - t0;
    printf("BENCH half_blend pixels=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", reps * PIXELS,
           hb_pie, hb_c);
    check(ex, name, "half_blend_matches_C", hb ? 1 : 0, 1);
    fail += hb ? 0 : 1;
    print_i16(ex, name, "half_blend", s_po, PIXELS);

    /* brighten: saturating add, one vector op per eight pixels */
    ex08_brighten(s_pa, s_pb, s_po, PIXELS);
    c_brighten(s_pa, s_pb, s_pref, PIXELS);
    int br = memcmp(s_po, s_pref, PIXELS * 2) == 0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex08_brighten(s_pa, s_pb, s_po, PIXELS);
    }
    uint32_t br_pie = ex07_ccount() - t0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        c_brighten(s_pa, s_pb, s_pref, PIXELS);
    }
    uint32_t br_c = ex07_ccount() - t0;
    printf("BENCH brighten pixels=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", reps * PIXELS,
           br_pie, br_c);
    check(ex, name, "brighten_matches_the_saturating_C_reference", br ? 1 : 0, 1);
    fail += br ? 0 : 1;
    print_i16(ex, name, "brightened", s_po, PIXELS);

    /* clamp: two vector ops per eight pixels */
    ex08_clamp(s_pa, s_lo8, s_hi8, s_po, PIXELS);
    c_clamp(s_pa, -1000, 1000, s_pref, PIXELS);
    int cl = memcmp(s_po, s_pref, PIXELS * 2) == 0;
    check(ex, name, "clamp_matches_C", cl ? 1 : 0, 1);
    fail += cl ? 0 : 1;
    print_i16(ex, name, "clamped", s_po, PIXELS);

    /* tint: per-lane multiply with a SAR shift. EE.VMUL truncates rather than saturating, so the reference
     * wraps the same way the hardware does; a saturating tint would need an explicit VMIN/VMAX pass. */
    ex08_tint(s_pa, s_tint8, s_po, PIXELS, 8);
    for (int i = 0; i < PIXELS; i++) {
        s_pref[i] = trunc16(((int32_t)s_pa[i] * 300) >> 8);
    }
    int ti = memcmp(s_po, s_pref, PIXELS * 2) == 0;
    check(ex, name, "tint_matches_the_truncating_reference", ti ? 1 : 0, 1);
    fail += ti ? 0 : 1;

    /* shift sign: one shift of the same lanes two ways. EE.VMUL.S16 shifts the SIGNED lane (arithmetic),
     * EE.VMUL.U16 the unsigned one (logical); the two can only differ where bit 15 is set. */
    ex08_shift_sign(s_pa, s_ones8, s_sign_s, s_sign_u, 1);
    for (int i = 0; i < 8; i++) {
        s_sign_s_ref[i] = (int16_t)(s_pa[i] >> 1);
        s_sign_u_ref[i] = (int16_t)(((uint16_t)s_pa[i]) >> 1);
    }
    int ss = memcmp(s_sign_s, s_sign_s_ref, 16) == 0;
    int su = memcmp(s_sign_u, s_sign_u_ref, 16) == 0;
    check(ex, name, "shift_signed_multiply_is_arithmetic", ss ? 1 : 0, 1);
    check(ex, name, "shift_unsigned_multiply_is_logical", su ? 1 : 0, 1);
    fail += ss ? 0 : 1;
    fail += su ? 0 : 1;
    print_i16(ex, name, "shift_in", s_pa, 8);
    print_i16(ex, name, "shift_signed", s_sign_s, 8);
    print_i16(ex, name, "shift_unsigned", s_sign_u, 8);
    printf("EX %s %s DATA shift_expected signed=%d,%d unsigned=%d,%d shift=1\n", ex, name,
           s_sign_s_ref[0], s_sign_s_ref[1], s_sign_u_ref[0], s_sign_u_ref[1]);
    fflush(stdout);

    print_i16(ex, name, "a", s_pa, PIXELS);
    print_i16(ex, name, "b", s_pb, PIXELS);
    print_i16(ex, name, "tint", s_po, PIXELS);
    printf("EX %s %s DATA limits lo=%d hi=%d tint=%d shift=%d pixels=%d\n", ex, name, -1000, 1000, 300,
           8, PIXELS);
    fflush(stdout);
    section_end(ex, name, fail == 0 ? 1 : 0, fail);
}

/* ------------------------------------------------------------------ ex09: the accumulator's timing */

/* The 40-bit saturating per-lane accumulator written from the same pseudo-code the kernel was: lane j holds
 * sum over the coefficient lanes the MACs used of coef[k] * v[k][j]. Same model as ex07's, on purpose --
 * ex07 came back with rows this model does not predict, and this example is the instrument that says why. */
static void model_mac4(const int16_t *coef_row, const int16_t *v, int16_t *out, uint32_t shift)
{
    for (int j = 0; j < 8; j++) {
        int64_t acc = 0;
        for (int k = 0; k < 4; k++) {
            acc = sat40(acc + (int32_t)coef_row[k] * (int32_t)v[k * 8 + j]);
        }
        out[j] = sat16(acc >> shift);
    }
}

/* The same model without the readout: the raw 40-bit accumulators, for comparing against RUR.QACC_*. */
static void model_mac4_raw(const int16_t *coef_row, const int16_t *v, int64_t *acc)
{
    for (int j = 0; j < 8; j++) {
        int64_t a = 0;
        for (int k = 0; k < 4; k++) {
            a = sat40(a + (int32_t)coef_row[k] * (int32_t)v[k * 8 + j]);
        }
        acc[j] = a;
    }
}

/* Lane i of a 160-bit accumulator register held as five 32-bit words read through RUR. */
static int64_t lane40(const uint32_t *w, int i)
{
    int bit = i * 40;
    uint64_t pair = (uint64_t)w[bit / 32] | ((uint64_t)w[bit / 32 + 1] << 32);
    uint64_t raw = (pair >> (bit % 32)) & 0xFFFFFFFFFFull;
    return (int64_t)(raw << 24) >> 24;          /* sign-extend from bit 39 */
}

static void ex09(void)
{
    const char *ex = "ex09", *name = "qacc";
    section_begin(ex, name);

    /* The one MAC probe: coefficient lane 0 (and 1..3 for the broadcast test) against eight lanes. */
    static const int16_t v8[8] PIE16 = {1000, -2000, 3000, -4000, 5000, -6000, 7000, -8000};
    static const int16_t coef8[8] PIE16 = {3, 7, 5, 11, 0, 0, 0, 0};
    /* The four-MAC (ex07 row) probe: four coefficient rows and four vector rows. */
    /* Four coefficient rows padded to eight int16, for the reason ex07 documents: a row of four coefficients
     * is 8 bytes and the 128-bit .IP load steps 16. */
    static const int16_t coef_rows[32] PIE16 = {3, 7, 5, 11, 0, 0, 0, 0,
                                                0, -2, 0, 4, 0, 0, 0, 0,
                                                1, 0, 0, 2, 0, 0, 0, 0,
                                                0, 0, 4, 0, 0, 0, 0, 0};
    static const int16_t v_rows[32] PIE16 = {1000, -2000, 3000, -4000, 5000, -6000, 7000, -8000,
                                             500, -500, 500, -500, 250, -250, 250, -250,
                                             1000, 1000, -1000, -1000, 1000, 1000, -1000, -1000,
                                             3, 5, 7, 11, 13, 17, 19, 23};
    int16_t model[32] PIE16, got[32] PIE16;
    const uint32_t shift = 0;
    int fail = 0;

    /* The trap that made the first run of this probe unreadable: an array that is not on the 16-byte grid
     * shifts every lane. Assert it for every buffer the kernels touch. */
    check_aligned(ex, name, "v8_16_byte_aligned", v8, &fail);
    check_aligned(ex, name, "coef8_16_byte_aligned", coef8, &fail);
    check_aligned(ex, name, "coef_rows_16_byte_aligned", coef_rows, &fail);
    check_aligned(ex, name, "v_rows_16_byte_aligned", v_rows, &fail);
    check_aligned(ex, name, "out9_16_byte_aligned", s_out9, &fail);
    check_aligned(ex, name, "out9b_16_byte_aligned", s_out9b, &fail);
    check_aligned(ex, name, "qraw_16_byte_aligned", s_qraw, &fail);

    /* 1. one MAC, then the readout one slot later each time (gap 0..6). */
    int16_t g[7][8] PIE16;
    const char *gnames[7] = {"g0", "g1", "g2", "g3", "g4", "g6", "g7"};
    ex09_mac1_g0(v8, coef8, g[0], shift);
    ex09_mac1_g1(v8, coef8, g[1], shift);
    ex09_mac1_g2(v8, coef8, g[2], shift);
    ex09_mac1_g3(v8, coef8, g[3], shift);
    ex09_mac1_g4(v8, coef8, g[4], shift);
    ex09_mac1_g6(v8, coef8, g[5], shift);
    ex09_mac1_g6(v8, coef8, g[6], shift);
    for (int j = 0; j < 8; j++) {
        model[j] = sat16((int32_t)v8[j] * coef8[0]);
    }
    int min_gap = -1;
    for (int i = 0; i < 7; i++) {
        print_i16(ex, name, gnames[i], g[i], 8);
        if (min_gap < 0 && memcmp(g[i], model, 16) == 0) {
            min_gap = i;
        }
    }
    printf("EX %s %s DATA mac1_min_gap=%d mac1_model_matched_at_g6=%d\n", ex, name, min_gap,
           memcmp(g[5], model, 16) == 0);
    print_i16(ex, name, "mac1_model", model, 8);
    check(ex, name, "mac1_min_gap_found", min_gap >= 0 ? 1 : 0, 1);
    /* The same MAC with the coefficient in lanes 1, 2 and 3: which operand lane does sel8 broadcast? */
    ex09_mac1_s1_g4(v8, coef8, got, shift);
    for (int j = 0; j < 8; j++) {
        model[j] = sat16((int32_t)v8[j] * coef8[1]);
    }
    int s1 = memcmp(got, model, 16) == 0;
    check(ex, name, "sel1_broadcasts_coefficient_lane1", s1 ? 1 : 0, 1);
    fail += s1 ? 0 : 1;
    print_i16(ex, name, "sel1", got, 8);
    ex09_mac1_s2_g4(v8, coef8, got, shift);
    for (int j = 0; j < 8; j++) {
        model[j] = sat16((int32_t)v8[j] * coef8[2]);
    }
    int s2 = memcmp(got, model, 16) == 0;
    check(ex, name, "sel2_broadcasts_coefficient_lane2", s2 ? 1 : 0, 1);
    fail += s2 ? 0 : 1;
    ex09_mac1_s3_g4(v8, coef8, got, shift);
    for (int j = 0; j < 8; j++) {
        model[j] = sat16((int32_t)v8[j] * coef8[3]);
    }
    int s3 = memcmp(got, model, 16) == 0;
    check(ex, name, "sel3_broadcasts_coefficient_lane3", s3 ? 1 : 0, 1);
    fail += s3 ? 0 : 1;

    /* 2. the raw accumulator: no readout in between, so this is the lane layout the MACs left behind. */
    ex09_raw_qacc(v8, coef8, s_qraw, shift);
    for (int i = 0; i < 10; i++) {
        printf("EX %s %s DATA qacc_%s_%d=%08" PRIx32 "\n", ex, name, i < 5 ? "L" : "H", i % 5, s_qraw[i]);
    }
    int64_t lanes[8];
    for (int i = 0; i < 4; i++) {
        lanes[i] = lane40(&s_qraw[0], i);
        lanes[4 + i] = lane40(&s_qraw[5], i);
    }
    int lane_ok = 1;
    for (int j = 0; j < 8; j++) {
        int64_t want = (int64_t)v8[j] * coef8[0];
        printf("EX %s %s DATA qacc_lane%d=%lld want=%lld\n", ex, name, j, (long long)lanes[j],
               (long long)want);
        if (lanes[j] != want) {
            lane_ok = 0;
        }
    }
    check(ex, name, "raw_qacc_lane_layout", lane_ok ? 1 : 0, 1);
    fail += lane_ok ? 0 : 1;

    /* 3. ZERO.QACC between two MACs: is the zero ordered before the next MAC? */
    ex09_zero_vis(v8, coef8, got, shift);
    for (int j = 0; j < 8; j++) {
        model[j] = sat16((int32_t)v8[j] * coef8[1]);
    }
    int zv = memcmp(got, model, 16) == 0;
    check(ex, name, "zero_qacc_between_macs_is_ordered", zv ? 1 : 0, 1);
    fail += zv ? 0 : 1;
    print_i16(ex, name, "zero_vis", got, 8);

    /* 4. four MACs (ex07's row) at readout distances 0, 2 and 4. */
    int16_t m4[3][8] PIE16;
    ex09_mac4_g0(coef_rows, v_rows, m4[0], shift);
    ex09_mac4_g2(coef_rows, v_rows, m4[1], shift);
    ex09_mac4_g4(coef_rows, v_rows, m4[2], shift);
    model_mac4(coef_rows, v_rows, model, shift);
    int m4_hit = 0;
    for (int i = 0; i < 3; i++) {
        int match = memcmp(m4[i], model, 16) == 0;
        m4_hit += match;
        printf("EX %s %s DATA mac4_gap%d_matches_model=%d\n", ex, name, i * 2, match);
        print_i16(ex, name, i == 0 ? "mac4_g0" : (i == 1 ? "mac4_g2" : "mac4_g4"), m4[i], 8);
    }
    print_i16(ex, name, "mac4_model", model, 8);
    check(ex, name, "mac4_reaches_the_model_at_some_gap", m4_hit > 0 ? 1 : 0, 1);
    fail += m4_hit > 0 ? 0 : 1;

    /* 5. the looped version (ex07's actual shape): four rows, readout at gap 0 vs gap 4. */
    ex09_chain_g0(coef_rows, v_rows, s_out9, shift, 4);
    ex09_chain_g4(coef_rows, v_rows, s_out9b, shift, 4);
    for (int r = 0; r < 4; r++) {
        model_mac4(&coef_rows[r * 8], v_rows, &model[r * 8], shift);
    }
    int c0 = 0, c4 = 0;
    for (int i = 0; i < 32; i++) {
        c0 += s_out9[i] == model[i];
        c4 += s_out9b[i] == model[i];
    }
    printf("EX %s %s DATA chain_g0_matched=%d chain_g4_matched=%d of=32\n", ex, name, c0, c4);
    print_i16(ex, name, "chain_g0", s_out9, 32);
    print_i16(ex, name, "chain_g4", s_out9b, 32);
    print_i16(ex, name, "chain_model", model, 32);
    check(ex, name, "chain_variant_matches_the_model", (c0 == 32 || c4 == 32) ? 1 : 0, 1);
    fail += (c0 == 32 || c4 == 32) ? 0 : 1;

    /* 6. does the readout write the accumulator back? shift_a = 5 first, then shift_b = 0: if it does,
     * out_b is out_a (already shifted and saturated); if it does not, out_b is the raw accumulator. */
    ex09_srcmb_wb(v8, coef8, s_wb_a, s_wb_b, 5, 0);
    for (int j = 0; j < 8; j++) {
        int64_t acc = sat40((int32_t)v8[j] * coef8[0]);
        model[j] = sat16(acc >> 5);
    }
    int wb_a_ok = memcmp(s_wb_a, model, 16) == 0;
    check(ex, name, "srcmb_first_readout_is_accx_shift5", wb_a_ok ? 1 : 0, 1);
    fail += wb_a_ok ? 0 : 1;
    int wb_rmw = memcmp(s_wb_b, s_wb_a, 16) == 0;
    int wb_single = 1;
    for (int j = 0; j < 8; j++) {
        wb_single &= s_wb_b[j] == sat16(sat40((int32_t)v8[j] * coef8[0]) >> 0);
    }
    printf("EX %s %s DATA srcmb_verdict %s\n", ex, name, wb_rmw ? "read_modify_write" : (wb_single ? "single_shot" : "neither"));
    print_i16(ex, name, "wb_a", s_wb_a, 8);
    print_i16(ex, name, "wb_b", s_wb_b, 8);
    print_i16(ex, name, "wb_model_shift_a", model, 8);   /* the shift-5 prediction out_a must match */
    check(ex, name, "srcmb_second_readout_is_one_of_the_two_models", (wb_rmw || wb_single) ? 1 : 0, 1);
    fail += (wb_rmw || wb_single) ? 0 : 1;

    /* 7. what the MACs actually consumed, row by row, with the readout out of the picture: the raw
     * accumulator words, and which coefficient row each one equals. */
    print_i16(ex, name, "coef_rows", coef_rows, 32);
    print_i16(ex, name, "v_rows", v_rows, 32);
    ex09_rows_raw(coef_rows, v_rows, s_qrows, 4);
    int raw_ok = 0;
    for (int r = 0; r < 4; r++) {
        int64_t lanes[8], want[8];
        for (int i = 0; i < 4; i++) {
            lanes[i] = lane40(&s_qrows[r * 10], i);
            lanes[4 + i] = lane40(&s_qrows[r * 10 + 5], i);
        }
        model_mac4_raw(&coef_rows[r * 8], v_rows, want);
        int same = memcmp(lanes, want, sizeof(lanes)) == 0;
        raw_ok += same;
        int which = -1;
        for (int i = 0; i < 4 && which < 0; i++) {
            int64_t cand[8];
            model_mac4_raw(&coef_rows[i * 8], v_rows, cand);
            if (memcmp(lanes, cand, sizeof(cand)) == 0) {
                which = i;
            }
        }
        printf("EX %s %s DATA raw_row%d_matched_own=%d\n", ex, name, r, same);
        printf("EX %s %s DATA raw_row%d_equals_coef_row=%d\n", ex, name, r, which);
        printf("EX %s %s DATA raw_row%d=", ex, name, r);
        for (int j = 0; j < 8; j++) {
            printf("%s%04x", j ? "," : "", (unsigned)(uint16_t)(lanes[j] & 0xFFFF));
        }
        printf("\n");
        fflush(stdout);
    }
    check(ex, name, "raw_rows_match_their_own_coefficient_row", raw_ok, 4);
    fail += (raw_ok == 4) ? 0 : 1;

    /* 8. the same four rows with a per-row readout and no loop tail: is the branch part of the effect? */
    ex09_rows_unrolled(coef_rows, v_rows, s_out9b, 0);
    for (int r = 0; r < 4; r++) {
        model_mac4(&coef_rows[r * 8], v_rows, &model[r * 8], 0);
    }
    int un = 0;
    for (int i = 0; i < 32; i++) {
        un += s_out9b[i] == model[i];
    }
    printf("EX %s %s DATA unrolled_matched=%d of=32\n", ex, name, un);
    print_i16(ex, name, "unrolled", s_out9b, 32);

    /* 9. bisect: the address walk on its own, and the accumulate/readout with the coefficient held in a
     * register. Whichever of these two reproduces the oddity says where the oddity lives. */
    ex09_ipwalk(coef_rows, s_out9, 4);          /* 4 iterations x 1 row = 32 int16 */
    int walk_ok = 1;
    for (int i = 0; i < 32; i++) {              /* coef_rows[] is 32 int16 (four padded rows) */
        walk_ok &= s_out9[i] == coef_rows[i];
    }
    printf("EX %s %s DATA ipwalk_matched=%d of=32\n", ex, name, walk_ok ? 32 : 31);
    print_i16(ex, name, "ipwalk_out", s_out9, 32);
    ex09_ipwalk4(coef_rows, s_out9, 1);         /* 4 back-to-back loads in one iteration */
    print_i16(ex, name, "ipwalk4_out", s_out9, 32);

    ex09_mac_fixed(coef_rows, v_rows, s_out9, 4, 0);
    model_mac4(&coef_rows[0], v_rows, model, 0);
    int fx0 = 0;
    for (int i = 0; i < 8; i++) {
        fx0 += s_out9[i] == model[i];
    }
    int fx_ident = 1;
    for (int r = 1; r < 4; r++) {
        for (int i = 0; i < 8; i++) {
            fx_ident &= s_out9[r * 8 + i] == s_out9[i];
        }
    }
    printf("EX %s %s DATA mac_fixed_row0_matched=%d of=8 mac_fixed_rows_identical=%d\n", ex, name, fx0,
           fx_ident);
    print_i16(ex, name, "mac_fixed", s_out9, 32);

    printf("EX %s %s DATA probe v=1000..-8000 coef_lanes=3,7,5,11 shift=0\n", ex, name);
    fflush(stdout);
    section_end(ex, name, (fail == 0) ? 1 : 0, fail);
}

/* ------------------------------------------------------------------ */

void app_main(void)
{
    esp_chip_info_t chip;
    esp_chip_info(&chip);
    s_rng = 0x1234;

    /* Give the capture time to attach: the whole report is emitted in milliseconds, and a port that is
     * read a second later has already lost the oldest lines in the buffer. */
    vTaskDelay(pdMS_TO_TICKS(1500));

    printf("ENV chip=esp32s3 cores=%d revision=%d.%d idf=%s\n", chip.cores, chip.revision / 100,
           chip.revision % 100, esp_get_idf_version());
    printf("ENV examples=9 buffers=16-byte-aligned rng_seed=0x1234 c_flags=-O2\n");
    fflush(stdout);

    ex01();
    ex02();
    ex03();
    ex04();
    ex05();
    ex06();
    ex07();
    ex08();
    ex09();

    printf("SUMMARY checks_ok=%d checks_fail=%d\n", s_ok, s_fail);
    printf("END\n");
    fflush(stdout);
}
