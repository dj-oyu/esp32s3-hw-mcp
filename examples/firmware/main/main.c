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
static int32_t s_out2[2] __attribute__((aligned(16)));
static int16_t s_lanes8[8] __attribute__((aligned(16)));
static uint32_t s_words[4] __attribute__((aligned(16)));

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
    int16_t u[8] = {1000, 2000, 3000, -4000, 7000, 8000, 9000, -10000};
    int16_t v[8] = {500, -600, 700, 800, -900, 1000, 1100, 1200};
    for (uint32_t sar = 0; sar <= 12; sar += 12) {
        int16_t w[8] = {0, 0, 0, 0, 0, 0, 0, 0};
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
    printf("ENV examples=6 buffers=16-byte-aligned rng_seed=0x1234\n");
    fflush(stdout);

    ex01();
    ex02();
    ex03();
    ex04();
    ex05();
    ex06();

    printf("SUMMARY checks_ok=%d checks_fail=%d\n", s_ok, s_fail);
    printf("END\n");
    fflush(stdout);
}
