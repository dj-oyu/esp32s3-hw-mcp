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
/* ex10 (motion): the block SAD gets an 8-bit sample case (the kernel's documented domain) and a full-range
 * uint16 case (outside the contract, where the signed reading of a lane stops meaning |a-b|), and the
 * half-pel row gets the same pair of domains. */
#define SAD_BLOCKS      64
#define SAD_FULL_BLOCKS 32
#define HP_LANES        128
#define HP_FULL_LANES   64
static uint16_t s_sad_a[SAD_BLOCKS * 8] __attribute__((aligned(16)));
static uint16_t s_sad_b[SAD_BLOCKS * 8] __attribute__((aligned(16)));
static uint16_t s_sad_fa[SAD_FULL_BLOCKS * 8] __attribute__((aligned(16)));
static uint16_t s_sad_fb[SAD_FULL_BLOCKS * 8] __attribute__((aligned(16)));
static uint32_t s_sad_out[4] __attribute__((aligned(16)));      /* [0..1] 8-bit case, [2..3] full range */
static int16_t s_hp_a[HP_LANES] __attribute__((aligned(16)));
static int16_t s_hp_b[HP_LANES] __attribute__((aligned(16)));
static int16_t s_hp_out[HP_LANES] __attribute__((aligned(16)));
static int16_t s_hp_ref[HP_LANES] __attribute__((aligned(16)));
static int16_t s_hp_fa[HP_FULL_LANES] __attribute__((aligned(16)));
static int16_t s_hp_fb[HP_FULL_LANES] __attribute__((aligned(16)));
static int16_t s_hp_fout[HP_FULL_LANES] __attribute__((aligned(16)));
static int16_t s_hp_fref[HP_FULL_LANES] __attribute__((aligned(16)));
/* ex11 (block8x8): the 8x8 block, the coefficient table, its transpose, and the readout with a shift small
 * enough that EE.SRCMB.S16.QACC has to saturate (the acc_sat buffers). */
#define BLOCK8_SHIFT     15
#define BLOCK8_SHIFT_SAT 8
static int16_t s_b8_coef[64] __attribute__((aligned(16)));
static int16_t s_b8_coeft[64] __attribute__((aligned(16)));
static int16_t s_b8_block[64] __attribute__((aligned(16)));
static int16_t s_b8_out[64] __attribute__((aligned(16)));
static int16_t s_b8_ref[64] __attribute__((aligned(16)));
static int16_t s_b8_outt[64] __attribute__((aligned(16)));
static int16_t s_b8_reft[64] __attribute__((aligned(16)));
static int16_t s_b8_outs[64] __attribute__((aligned(16)));
static int16_t s_b8_refs[64] __attribute__((aligned(16)));
/* ex12 (physics): 19 boxes and 19 point pairs, so both the whole groups of eight and the scalar tail run;
 * the plane-major images are 96 bytes per box group and 48 bytes per point group (see examples.h). */
#define BOXES  19
#define POINTS 19
#define BOX_GROUPS  ((BOXES + 7) / 8)
#define POINT_GROUPS ((POINTS + 7) / 8)
static int32_t s_phy_pos[8] __attribute__((aligned(16)));
static int32_t s_phy_vel[8] __attribute__((aligned(16)));
static int32_t s_phy_acc[8] __attribute__((aligned(16)));
static int32_t s_phy_out[8] __attribute__((aligned(16)));
static int32_t s_phy_outc[8] __attribute__((aligned(16)));
static int32_t s_phy_ref[8] __attribute__((aligned(16)));
static int32_t s_phy_refc[8] __attribute__((aligned(16)));
static int16_t s_phy_boxes[48 * BOX_GROUPS] __attribute__((aligned(16)));
static int16_t s_phy_query[8] __attribute__((aligned(16)));
static int16_t s_phy_masks[BOXES] __attribute__((aligned(16)));
static int16_t s_phy_masks_ref[BOXES] __attribute__((aligned(16)));
static int16_t s_phy_pta[24 * POINT_GROUPS] __attribute__((aligned(16)));
static int16_t s_phy_ptb[24 * POINT_GROUPS] __attribute__((aligned(16)));
static int32_t s_phy_d2[POINTS] __attribute__((aligned(16)));
static int32_t s_phy_d2_ref[POINTS] __attribute__((aligned(16)));
static int16_t s_phy_d2q[POINTS] __attribute__((aligned(16)));
static int16_t s_phy_d2q_ref[POINTS] __attribute__((aligned(16)));

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

/* The same generator over an int32 range: the physics example needs lanes whose pos + vel leaves int32,
 * which int16 lanes cannot reach. Every lane is still reproducible from the seed. */
static int32_t rnd32(int32_t lo, int32_t hi)
{
    s_rng = s_rng * 1664525u + 1013904223u;
    return lo + (int32_t)((uint32_t)(s_rng >> 8) % (uint32_t)(hi - lo + 1));
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

/* ------------------------------------------------------------------ ex10: motion (block SAD + half-pel) */

/* The C references for the two kernels, written from the same manual pseudo-code the assembly was (see
 * examples/docs/ex10_motion.md): the SAD reference follows the INSTRUCTIONS -- a saturating signed
 * distance -- rather than the textbook formula, so comparing against it is exact on any input. The
 * textbook version is here too, because it is what says where the kernel's inputs stopped being in
 * contract; the examples print that difference as a DATA line instead of asserting it away.
 *
 * The SAD reference returns its total instead of writing a buffer, so the timed loop has to consume it: a
 * pure call whose value is discarded can be deleted by the optimiser, and the cycle count would then
 * describe nothing at all. */
static volatile uint64_t s_bench_sink;

uint64_t ex10_sad8_c(const uint16_t *a, const uint16_t *b, uint32_t n_blocks)
{
    uint64_t acc = 0;
    for (uint32_t i = 0; i < n_blocks * 8; i++) {
        int32_t d = (int32_t)(int16_t)a[i] - (int32_t)(int16_t)b[i];   /* VSUBS(VMAX.S16, VMIN.S16) */
        if (d < 0) {
            d = -d;
        }
        if (d > 32767) {
            d = 32767;                     /* EE.VSUBS.S16 saturates at +32767 */
        }
        acc += (uint32_t)d;
    }
    return acc;
}

uint64_t ex10_sad8_plain_c(const uint16_t *a, const uint16_t *b, uint32_t n_blocks)
{
    uint64_t acc = 0;
    for (uint32_t i = 0; i < n_blocks * 8; i++) {
        acc += (a[i] > b[i]) ? (uint32_t)(a[i] - b[i]) : (uint32_t)(b[i] - a[i]);
    }
    return acc;
}

/* out[i] = (uint16_t)(a[i] + b[i] + 1) >> 1 -- the cast is before the shift, which is the EE.VMUL.U16
 * reading of the >>1 and not the EE.VMUL.S16 one. */
void ex10_halfpel_c(const int16_t *a, const int16_t *b, int16_t *out, uint32_t n_lanes)
{
    for (uint32_t i = 0; i < n_lanes; i++) {
        out[i] = (int16_t)((uint16_t)((int32_t)a[i] + (int32_t)b[i] + 1) >> 1);
    }
}

static void ex10(void)
{
    const char *ex = "ex10", *name = "motion";
    section_begin(ex, name);

    s_rng = 0x10A;                              /* fixed seed: this file plus the log is the whole story */
    for (int i = 0; i < SAD_BLOCKS * 8; i++) {
        s_sad_a[i] = (uint16_t)rnd16(0, 255);   /* an 8-bit luma sample zero-extended into the lane */
        s_sad_b[i] = (uint16_t)rnd16(0, 255);
    }
    for (int i = 0; i < SAD_FULL_BLOCKS * 8; i++) {
        s_sad_fa[i] = (uint16_t)rnd16(-32768, 32767);   /* full-range lanes: outside the contract */
        s_sad_fb[i] = (uint16_t)rnd16(-32768, 32767);
    }
    for (int i = 0; i < 8; i++) {
        s_ones8[i] = 1;                         /* ex10_halfpel's ones8: the +1 addend AND the x1 factor */
    }
    for (int i = 0; i < HP_LANES; i++) {
        s_hp_a[i] = rnd16(0, 255);
        s_hp_b[i] = rnd16(0, 255);
    }
    for (int i = 0; i < HP_FULL_LANES; i++) {
        s_hp_fa[i] = rnd16(-32768, 32767);
        s_hp_fb[i] = rnd16(-32768, 32767);
    }

    int fail = 0;
    int ok;
    check_aligned(ex, name, "sad_a_16_byte_aligned", s_sad_a, &fail);
    check_aligned(ex, name, "sad_b_16_byte_aligned", s_sad_b, &fail);
    check_aligned(ex, name, "sad_full_a_16_byte_aligned", s_sad_fa, &fail);
    check_aligned(ex, name, "sad_full_b_16_byte_aligned", s_sad_fb, &fail);
    check_aligned(ex, name, "sad_out_16_byte_aligned", s_sad_out, &fail);
    check_aligned(ex, name, "halfpel_a_16_byte_aligned", s_hp_a, &fail);
    check_aligned(ex, name, "halfpel_b_16_byte_aligned", s_hp_b, &fail);
    check_aligned(ex, name, "ones8_16_byte_aligned", s_ones8, &fail);
    check_aligned(ex, name, "halfpel_full_a_16_byte_aligned", s_hp_fa, &fail);
    check_aligned(ex, name, "halfpel_full_b_16_byte_aligned", s_hp_fb, &fail);

    /* --- A: the block SAD, 8-bit sample data first (the domain the kernel is exact in) --- */
    ex10_sad8(s_sad_a, s_sad_b, SAD_BLOCKS, s_sad_out);
    const uint64_t sad_total = ((uint64_t)s_sad_out[1] << 32) | s_sad_out[0];
    const uint64_t sad_ref = ex10_sad8_c(s_sad_a, s_sad_b, SAD_BLOCKS);
    const uint64_t sad_plain = ex10_sad8_plain_c(s_sad_a, s_sad_b, SAD_BLOCKS);
    int sad_straddle = 0, sad_clamped = 0;
    for (int i = 0; i < SAD_BLOCKS * 8; i++) {
        int32_t d = (int32_t)(int16_t)s_sad_a[i] - (int32_t)(int16_t)s_sad_b[i];
        if (d < 0) {
            d = -d;
        }
        if ((s_sad_a[i] >= 32768) != (s_sad_b[i] >= 32768)) {
            sad_straddle++;                 /* the two lanes are in different halves of the range */
        }
        if (d > 32767) {
            sad_clamped++;
        }
    }
    ok = (sad_total == sad_ref);
    check(ex, name, "sad8_8bit_matches_the_saturating_C_reference", (int64_t)sad_total, (int64_t)sad_ref);
    fail += ok ? 0 : 1;
    ok = (sad_total == sad_plain);
    check(ex, name, "sad8_8bit_domain_equals_the_textbook_SAD", (int64_t)sad_total, (int64_t)sad_plain);
    fail += ok ? 0 : 1;
    ok = (sad_straddle == 0 && sad_clamped == 0);
    check(ex, name, "sad8_8bit_domain_straddles_no_lane_and_clamps_none", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;

    /* ... and then full-range uint16 lanes, which are outside it: the kernel computes its own rule
     * (|a-b| when the lanes share a half, 65536-|a-b| when they straddle, clamped at 32767) and the
     * textbook SAD is a different number. Both are output; the CHECK is against the instruction rule. */
    ex10_sad8(s_sad_fa, s_sad_fb, SAD_FULL_BLOCKS, &s_sad_out[2]);
    const uint64_t sadf_total = ((uint64_t)s_sad_out[3] << 32) | s_sad_out[2];
    const uint64_t sadf_ref = ex10_sad8_c(s_sad_fa, s_sad_fb, SAD_FULL_BLOCKS);
    const uint64_t sadf_plain = ex10_sad8_plain_c(s_sad_fa, s_sad_fb, SAD_FULL_BLOCKS);
    uint64_t sadf_rule = 0;
    int sadf_straddle = 0, sadf_clamped = 0;
    for (int i = 0; i < SAD_FULL_BLOCKS * 8; i++) {
        int same_half = (s_sad_fa[i] >= 32768) == (s_sad_fb[i] >= 32768);
        /* The rule as ex10_motion.S states it, in terms of the UNSIGNED readings: the difference, or its
         * 65536-complement when the two lanes sit in different halves, clamped at 32767. The signed
         * readings give the same number without the complement step -- |signed(a) - signed(b)| is what the
         * C reference computes -- and that identity is what this second reading is here to demonstrate. */
        int32_t d = (s_sad_fa[i] >= s_sad_fb[i]) ? (int32_t)(s_sad_fa[i] - s_sad_fb[i])
                                                : (int32_t)(s_sad_fb[i] - s_sad_fa[i]);
        if (!same_half) {
            sadf_straddle++;
            d = 65536 - d;
        }
        if (d > 32767) {
            sadf_clamped++;
            d = 32767;
        }
        sadf_rule += (uint32_t)d;
    }
    ok = (sadf_total == sadf_ref);
    check(ex, name, "sad8_full_range_matches_the_saturating_C_reference", (int64_t)sadf_total,
          (int64_t)sadf_ref);
    fail += ok ? 0 : 1;
    ok = (sadf_rule == sadf_total);
    check(ex, name, "sad8_full_range_per_lane_rule_matches_the_kernel", (int64_t)sadf_rule,
          (int64_t)sadf_total);
    fail += ok ? 0 : 1;
    ok = (sadf_straddle > 0 && sadf_clamped > 0);
    check(ex, name, "sad8_full_range_reaches_the_straddling_and_clamping_lanes", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;

    /* --- B: the half-pel row. 8-bit samples first: a + b + 1 <= 511, so neither saturating add can
     * fire and the kernel is the reference expression lane for lane. --- */
    ex10_halfpel(s_hp_a, s_hp_b, s_ones8, s_hp_out, HP_LANES);
    ex10_halfpel_c(s_hp_a, s_hp_b, s_hp_ref, HP_LANES);
    int hp_match = 0;
    for (int i = 0; i < HP_LANES; i++) {
        hp_match += s_hp_out[i] == s_hp_ref[i];
    }
    check(ex, name, "halfpel_8bit_matches_the_reference_expression", hp_match, HP_LANES);
    fail += (hp_match == HP_LANES) ? 0 : 1;

    ex10_halfpel(s_hp_fa, s_hp_fb, s_ones8, s_hp_fout, HP_FULL_LANES);
    ex10_halfpel_c(s_hp_fa, s_hp_fb, s_hp_fref, HP_FULL_LANES);
    int hp_kern = 0, hp_mismatch = 0, hp_sat_pos = 0, hp_sat_neg = 0;
    for (int i = 0; i < HP_FULL_LANES; i++) {
        int32_t s = (int32_t)s_hp_fa[i] + (int32_t)s_hp_fb[i] + 1;
        int16_t t = sat16((int32_t)s_hp_fa[i] + s_hp_fb[i]);    /* EE.VADDS.S16 (saturating) */
        t = sat16((int32_t)t + 1);                              /* EE.VADDS.S16 with the ones8 (+1) */
        int16_t k = (int16_t)((uint16_t)t >> 1);                /* EE.VMUL.U16 by ones8, SAR = 1 */
        hp_kern += k == s_hp_fout[i];
        hp_mismatch += k != s_hp_fref[i];
        if (s > 32767) {
            hp_sat_pos++;
        }
        if (s < -32768) {
            hp_sat_neg++;
        }
    }
    check(ex, name, "halfpel_full_range_follows_the_kernel_sequence", hp_kern, HP_FULL_LANES);
    fail += (hp_kern == HP_FULL_LANES) ? 0 : 1;
    ok = (hp_mismatch == hp_sat_pos + hp_sat_neg);
    check(ex, name, "halfpel_full_range_diverging_lanes_are_exactly_the_saturating_rounding_add",
          hp_mismatch, hp_sat_pos + hp_sat_neg);
    fail += ok ? 0 : 1;
    ok = (hp_sat_pos > 0 && hp_sat_neg > 0);
    check(ex, name, "halfpel_full_range_reaches_both_saturation_directions", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;

    print_i16(ex, name, "sad_a", (const int16_t *)s_sad_a, SAD_BLOCKS * 8);
    print_i16(ex, name, "sad_b", (const int16_t *)s_sad_b, SAD_BLOCKS * 8);
    print_i32(ex, name, "sad_accx", (const int32_t *)s_sad_out, 2);
    printf("EX %s %s DATA sad_blocks=%d\n", ex, name, SAD_BLOCKS);
    printf("EX %s %s DATA sad_total=%llu\n", ex, name, (unsigned long long)sad_total);
    printf("EX %s %s DATA sad_plain_textbook=%llu\n", ex, name, (unsigned long long)sad_plain);
    printf("EX %s %s DATA sad_straddling_lanes=%d\n", ex, name, sad_straddle);
    printf("EX %s %s DATA sad_clamped_lanes=%d\n", ex, name, sad_clamped);
    print_i16(ex, name, "sad_full_a", (const int16_t *)s_sad_fa, SAD_FULL_BLOCKS * 8);
    print_i16(ex, name, "sad_full_b", (const int16_t *)s_sad_fb, SAD_FULL_BLOCKS * 8);
    print_i32(ex, name, "sad_full_accx", (const int32_t *)&s_sad_out[2], 2);
    printf("EX %s %s DATA sad_full_blocks=%d\n", ex, name, SAD_FULL_BLOCKS);
    printf("EX %s %s DATA sad_full_total=%llu\n", ex, name, (unsigned long long)sadf_total);
    printf("EX %s %s DATA sad_full_plain_textbook=%llu\n", ex, name, (unsigned long long)sadf_plain);
    printf("EX %s %s DATA sad_full_straddling_lanes=%d\n", ex, name, sadf_straddle);
    printf("EX %s %s DATA sad_full_clamped_lanes=%d\n", ex, name, sadf_clamped);
    print_i16(ex, name, "halfpel_a", s_hp_a, HP_LANES);
    print_i16(ex, name, "halfpel_b", s_hp_b, HP_LANES);
    print_i16(ex, name, "halfpel_out", s_hp_out, HP_LANES);
    printf("EX %s %s DATA halfpel_lanes=%d\n", ex, name, HP_LANES);
    printf("EX %s %s DATA halfpel_ones8=1\n", ex, name);
    print_i16(ex, name, "halfpel_full_a", s_hp_fa, HP_FULL_LANES);
    print_i16(ex, name, "halfpel_full_b", s_hp_fb, HP_FULL_LANES);
    print_i16(ex, name, "halfpel_full_out", s_hp_fout, HP_FULL_LANES);
    printf("EX %s %s DATA halfpel_full_lanes=%d\n", ex, name, HP_FULL_LANES);
    printf("EX %s %s DATA halfpel_full_mismatch=%d\n", ex, name, hp_mismatch);
    printf("EX %s %s DATA halfpel_full_sat_pos=%d\n", ex, name, hp_sat_pos);
    printf("EX %s %s DATA halfpel_full_sat_neg=%d\n", ex, name, hp_sat_neg);
    fflush(stdout);

    /* Cycles per block and per pixel, the assembly against the same arithmetic in C at -O2. */
    const int reps = 100;
    uint32_t t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex10_sad8(s_sad_a, s_sad_b, SAD_BLOCKS, s_sad_out);
    }
    uint32_t sad_pie_cycles = ex07_ccount() - t0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        s_bench_sink = ex10_sad8_c(s_sad_a, s_sad_b, SAD_BLOCKS);
    }
    uint32_t sad_c_cycles = ex07_ccount() - t0;
    printf("BENCH sad8 blocks=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", reps * SAD_BLOCKS,
           sad_pie_cycles, sad_c_cycles);
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex10_halfpel(s_hp_a, s_hp_b, s_ones8, s_hp_out, HP_LANES);
    }
    uint32_t hp_pie_cycles = ex07_ccount() - t0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex10_halfpel_c(s_hp_a, s_hp_b, s_hp_ref, HP_LANES);
    }
    uint32_t hp_c_cycles = ex07_ccount() - t0;
    printf("BENCH halfpel pixels=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", reps * HP_LANES,
           hp_pie_cycles, hp_c_cycles);
    fflush(stdout);
    section_end(ex, name, (fail == 0) ? 1 : 0, fail);
}

/* ------------------------------------------------------------------ ex11: the 8x8 block transform */

/* out[k][j] = sat16((sum over i of coef[k][i] * block[i][j]) >> shift), int64 accumulation and the
 * saturation only at the readout -- the independent scalar reading of the contract in
 * examples/docs/ex11_block8x8.md. */
void ex11_block8x8_c(const int16_t *coef, const int16_t *block, int16_t *out, uint32_t shift)
{
    for (int k = 0; k < 8; k++) {
        for (int j = 0; j < 8; j++) {
            int64_t acc = 0;
            for (int i = 0; i < 8; i++) {
                acc += (int32_t)coef[k * 8 + i] * (int32_t)block[i * 8 + j];
            }
            out[k * 8 + j] = sat16(acc >> shift);
        }
    }
}

static void ex11(void)
{
    const char *ex = "ex11", *name = "block8x8";
    section_begin(ex, name);

    s_rng = 0x11B;
    /* A Q15-scale coefficient table (rows the size a DCT-II uses) and a block of moderate amplitude: at the
     * shift the readout lands inside int16, so the CHECK is about the arithmetic and not about the
     * readout's saturation. The third call drops the shift to 8 to pin the saturating readout itself, which
     * is the one place ex09's probes say this datapath is not free. */
    for (int k = 0; k < 8; k++) {
        for (int i = 0; i < 8; i++) {
            s_b8_coef[k * 8 + i] = rnd16(-16384, 16384);
            s_b8_coeft[i * 8 + k] = s_b8_coef[k * 8 + i];       /* the transpose: the other axis */
        }
    }
    for (int i = 0; i < 64; i++) {
        s_b8_block[i] = rnd16(-1024, 1024);
    }

    int fail = 0, ok;
    check_aligned(ex, name, "coef_16_byte_aligned", s_b8_coef, &fail);
    check_aligned(ex, name, "coef_transposed_16_byte_aligned", s_b8_coeft, &fail);
    check_aligned(ex, name, "block_16_byte_aligned", s_b8_block, &fail);
    check_aligned(ex, name, "out_16_byte_aligned", s_b8_out, &fail);
    check_aligned(ex, name, "out_transposed_16_byte_aligned", s_b8_outt, &fail);
    check_aligned(ex, name, "out_saturating_16_byte_aligned", s_b8_outs, &fail);

    ex11_block8x8(s_b8_coef, s_b8_block, s_b8_out, BLOCK8_SHIFT);
    ex11_block8x8_c(s_b8_coef, s_b8_block, s_b8_ref, BLOCK8_SHIFT);
    ex11_block8x8(s_b8_coeft, s_b8_block, s_b8_outt, BLOCK8_SHIFT);
    ex11_block8x8_c(s_b8_coeft, s_b8_block, s_b8_reft, BLOCK8_SHIFT);
    ex11_block8x8(s_b8_coef, s_b8_block, s_b8_outs, BLOCK8_SHIFT_SAT);
    ex11_block8x8_c(s_b8_coef, s_b8_block, s_b8_refs, BLOCK8_SHIFT_SAT);

    int m0 = 0, m1 = 0, ms = 0, tr = 1, sat_lanes = 0;
    for (int i = 0; i < 64; i++) {
        m0 += s_b8_out[i] == s_b8_ref[i];
        m1 += s_b8_outt[i] == s_b8_reft[i];
        ms += s_b8_outs[i] == s_b8_refs[i];
        sat_lanes += s_b8_outs[i] == 32767 || s_b8_outs[i] == -32768;
    }
    for (int k = 0; k < 8; k++) {                    /* the transpose table really is the transpose */
        for (int i = 0; i < 8; i++) {
            tr &= s_b8_coeft[i * 8 + k] == s_b8_coef[k * 8 + i];
        }
    }
    /* The 40-bit accumulator's headroom: the largest |row sum| over the whole block, and how many
     * (k,j) reach the clamp at all. */
    int64_t max_abs_sum = 0;
    int over40 = 0;
    for (int k = 0; k < 8; k++) {
        for (int j = 0; j < 8; j++) {
            int64_t acc = 0;
            for (int i = 0; i < 8; i++) {
                acc += (int32_t)s_b8_coef[k * 8 + i] * (int32_t)s_b8_block[i * 8 + j];
            }
            if (acc < 0) {
                acc = -acc;
            }
            if (acc > max_abs_sum) {
                max_abs_sum = acc;
            }
            if (acc > 0x7FFFFFFFFFLL) {
                over40++;
            }
        }
    }
    check(ex, name, "all_64_coefficients_match_the_int64_C_reference", m0, 64);
    fail += (m0 == 64) ? 0 : 1;
    check(ex, name, "transpose_form_matches_its_own_C_reference", m1, 64);
    fail += (m1 == 64) ? 0 : 1;
    check(ex, name, "the_second_table_is_the_transpose_of_the_first", tr ? 1 : 0, 1);
    fail += tr ? 0 : 1;
    ok = (ms == 64);
    check(ex, name, "saturating_readout_matches_sat16_of_the_row_sum", ms, 64);
    fail += ok ? 0 : 1;
    ok = (sat_lanes > 0);
    check(ex, name, "the_small_shift_makes_the_readout_saturate", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;
    ok = (over40 == 0);
    check(ex, name, "no_output_row_reaches_the_40_bit_accumulator_clamp", over40, 0);
    fail += ok ? 0 : 1;

    print_i16(ex, name, "coef", s_b8_coef, 64);
    print_i16(ex, name, "coef_transposed", s_b8_coeft, 64);
    print_i16(ex, name, "block", s_b8_block, 64);
    print_i16(ex, name, "out", s_b8_out, 64);
    print_i16(ex, name, "out_transposed", s_b8_outt, 64);
    print_i16(ex, name, "out_saturating", s_b8_outs, 64);
    printf("EX %s %s DATA shift=%" PRIu32 "\n", ex, name, (uint32_t)BLOCK8_SHIFT);
    printf("EX %s %s DATA shift_saturating=%" PRIu32 "\n", ex, name, (uint32_t)BLOCK8_SHIFT_SAT);
    printf("EX %s %s DATA saturated_lanes=%d\n", ex, name, sat_lanes);
    printf("EX %s %s DATA rows_over_40bit=%d\n", ex, name, over40);
    printf("EX %s %s DATA max_abs_row_sum=%lld\n", ex, name, (long long)max_abs_sum);
    fflush(stdout);

    /* Cycles per 8x8 block: the kernel against the scalar C matrix product, both at -O2. */
    const int reps = 2000;
    uint32_t t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex11_block8x8(s_b8_coef, s_b8_block, s_b8_out, BLOCK8_SHIFT);
    }
    uint32_t pie_cycles = ex07_ccount() - t0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex11_block8x8_c(s_b8_coef, s_b8_block, s_b8_ref, BLOCK8_SHIFT);
    }
    uint32_t c_cycles = ex07_ccount() - t0;
    printf("BENCH block8x8 blocks=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", reps, pie_cycles,
           c_cycles);
    fflush(stdout);
    section_end(ex, name, (fail == 0) ? 1 : 0, fail);
}

/* ------------------------------------------------------------------ ex12: physics and collision */

/* One box of the plane-major image: plane p of box b at 96*(b/8) + 16*p + 2*(b%8), the max 16 bytes on
 * from its min (see examples.h). */
static void box_set(int16_t *boxes, uint32_t b, int ax, int16_t lo, int16_t hi)
{
    boxes[48 * (int)(b / 8) + 16 * ax + (int)(b % 8)] = lo;
    boxes[48 * (int)(b / 8) + 16 * ax + 8 + (int)(b % 8)] = hi;
}

/* The three scalar references, from the same pseudo-code the kernel was written from
 * (examples/docs/ex12_physics.md): the saturating adds of the kernel, not the exact sum. */
void ex12_integrate_c(const int32_t *pos, const int32_t *vel, const int32_t *acc, int32_t *out,
                      int32_t lo, int32_t hi)
{
    for (int i = 0; i < 8; i++) {
        int32_t t = sat32((int64_t)pos[i] + vel[i]);            /* EE.VADDS.S32 */
        int32_t u = sat32((int64_t)t + acc[i]);                 /* EE.VADDS.S32 */
        int32_t m = (u < hi) ? u : hi;                          /* EE.VMIN.S32 */
        out[i] = (m > lo) ? m : lo;                             /* EE.VMAX.S32 (lo wins if lo > hi) */
    }
}

void ex12_sat_masks_c(const int16_t *boxes, const int16_t *query, int16_t *out, uint32_t n_boxes)
{
    for (uint32_t b = 0; b < n_boxes; b++) {
        int sep = 0;
        for (int ax = 0; ax < 3; ax++) {
            int16_t bmin = boxes[48 * (int)(b / 8) + 16 * ax + (int)(b % 8)];
            int16_t bmax = boxes[48 * (int)(b / 8) + 16 * ax + 8 + (int)(b % 8)];
            if (bmax < query[2 * ax] || bmin > query[2 * ax + 1]) {
                sep = 1;
            }
        }
        out[b] = sep ? 0 : -1;
    }
}

void ex12_dist2_qacc_c(const int16_t *pt_a, const int16_t *pt_b, int32_t *out, uint32_t n_points)
{
    for (uint32_t i = 0; i < n_points; i++) {
        int64_t s = 0;                                          /* the QACC lane is 40 bits wide */
        for (int ax = 0; ax < 3; ax++) {
            int16_t d = sat16((int32_t)pt_a[24 * (int)(i / 8) + 8 * ax + (int)(i % 8)] -
                              (int32_t)pt_b[24 * (int)(i / 8) + 8 * ax + (int)(i % 8)]);
            s += (int32_t)d * d;
        }
        out[i] = (s <= 0x7FFFFFFF) ? (int32_t)s : 2147483647;   /* unsigned minu with 2^31-1 */
    }
}

/* The Q16 readout, on the whole groups only, returning the number of pairs written -- the same contract as
 * the kernel's (n_points & ~7) return value. */
uint32_t ex12_dist2_q16_c(const int16_t *pt_a, const int16_t *pt_b, int16_t *out, uint32_t n_points,
                          uint32_t shift)
{
    uint32_t n = n_points & ~7u;
    for (uint32_t i = 0; i < n; i++) {
        int64_t s = 0;
        for (int ax = 0; ax < 3; ax++) {
            int16_t d = sat16((int32_t)pt_a[24 * (int)(i / 8) + 8 * ax + (int)(i % 8)] -
                              (int32_t)pt_b[24 * (int)(i / 8) + 8 * ax + (int)(i % 8)]);
            s += (int32_t)d * d;
        }
        out[i] = sat16((int32_t)(s >> shift));
    }
    return n;
}

#define Q16_SHIFT 8

static void ex12(void)
{
    const char *ex = "ex12", *name = "physics";
    section_begin(ex, name);

    s_rng = 0x12C;
    int fail = 0, ok;
    uint32_t t0;
    const int reps = 2000;

    /* --- A: semi-implicit Euler on eight objects. Two lanes are pushed to the int32 edge on purpose, so
     * |pos + vel| leaves int32 and the saturating add is exercised rather than assumed. --- */
    for (int i = 0; i < 8; i++) {
        s_phy_pos[i] = rnd32(-200000, 200000);
        s_phy_vel[i] = rnd32(-200000, 200000);
        s_phy_acc[i] = rnd32(-1000, 1000);
    }
    s_phy_pos[6] = 2147483647;
    s_phy_vel[6] = 2147483647;
    s_phy_acc[6] = -1;
    s_phy_pos[7] = -2147483647 - 1;
    s_phy_vel[7] = -2147483647 - 1;
    s_phy_acc[7] = 1;
    const int32_t lo_wide = -2147483647 - 1, hi_wide = 2147483647;   /* widest bounds: no clamp in the way */
    const int32_t lo_tight = -300000, hi_tight = 300000;             /* bounds a game would actually use */

    check_aligned(ex, name, "pos_16_byte_aligned", s_phy_pos, &fail);
    check_aligned(ex, name, "vel_16_byte_aligned", s_phy_vel, &fail);
    check_aligned(ex, name, "acc_16_byte_aligned", s_phy_acc, &fail);
    check_aligned(ex, name, "integrated_16_byte_aligned", s_phy_out, &fail);
    check_aligned(ex, name, "boxes_16_byte_aligned", s_phy_boxes, &fail);
    check_aligned(ex, name, "query_16_byte_aligned", s_phy_query, &fail);
    check_aligned(ex, name, "masks_16_byte_aligned", s_phy_masks, &fail);
    check_aligned(ex, name, "pt_a_16_byte_aligned", s_phy_pta, &fail);
    check_aligned(ex, name, "pt_b_16_byte_aligned", s_phy_ptb, &fail);
    check_aligned(ex, name, "dist2_16_byte_aligned", s_phy_d2, &fail);

    ex12_integrate(s_phy_pos, s_phy_vel, s_phy_acc, s_phy_out, lo_wide, hi_wide);
    ex12_integrate_c(s_phy_pos, s_phy_vel, s_phy_acc, s_phy_ref, lo_wide, hi_wide);
    int i_ok = 0, i_div = 0, i_domain = 0, i_domain_ok = 0;
    for (int i = 0; i < 8; i++) {
        int64_t sum = (int64_t)s_phy_pos[i] + s_phy_vel[i] + s_phy_acc[i];
        int64_t exact = sum > hi_wide ? hi_wide : (sum < lo_wide ? lo_wide : sum);
        int64_t pv = (int64_t)s_phy_pos[i] + s_phy_vel[i];
        i_ok += s_phy_out[i] == s_phy_ref[i];
        i_div += (int64_t)s_phy_out[i] != exact;
        if (pv <= 2147483647LL && pv >= -2147483647LL - 1) {     /* the documented in-domain condition */
            i_domain++;
            i_domain_ok += (int64_t)s_phy_out[i] == exact;
        }
    }
    check(ex, name, "integrate_matches_the_saturating_C_reference", i_ok, 8);
    fail += (i_ok == 8) ? 0 : 1;
    ok = (i_domain_ok == i_domain);
    check(ex, name, "integrate_in_domain_lanes_equal_the_exact_64_bit_sum", i_domain_ok, i_domain);
    fail += ok ? 0 : 1;
    ok = (i_div > 0);
    check(ex, name, "integrate_leaves_the_exact_sum_where_pos_plus_vel_leaves_int32", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;

    ex12_integrate(s_phy_pos, s_phy_vel, s_phy_acc, s_phy_outc, lo_tight, hi_tight);
    ex12_integrate_c(s_phy_pos, s_phy_vel, s_phy_acc, s_phy_refc, lo_tight, hi_tight);
    int c_ok = 0, c_moved = 0;
    for (int i = 0; i < 8; i++) {
        c_ok += s_phy_outc[i] == s_phy_refc[i];
        c_moved += s_phy_outc[i] != s_phy_out[i];
    }
    check(ex, name, "integrate_with_narrow_bounds_matches_the_C_reference", c_ok, 8);
    fail += (c_ok == 8) ? 0 : 1;
    ok = (c_moved > 0);
    check(ex, name, "the_bounds_move_at_least_one_lane", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;

    print_i32(ex, name, "pos", s_phy_pos, 8);
    print_i32(ex, name, "vel", s_phy_vel, 8);
    print_i32(ex, name, "acc", s_phy_acc, 8);
    print_i32(ex, name, "integrated", s_phy_out, 8);
    print_i32(ex, name, "integrated_narrow_bounds", s_phy_outc, 8);
    printf("EX %s %s DATA objects=8\n", ex, name);
    printf("EX %s %s DATA integrate_lo=%" PRId32 "\n", ex, name, lo_wide);
    printf("EX %s %s DATA integrate_hi=%" PRId32 "\n", ex, name, hi_wide);
    printf("EX %s %s DATA integrate_lo_narrow=%" PRId32 "\n", ex, name, lo_tight);
    printf("EX %s %s DATA integrate_hi_narrow=%" PRId32 "\n", ex, name, hi_tight);
    printf("EX %s %s DATA integrate_diverging_from_exact=%d\n", ex, name, i_div);
    printf("EX %s %s DATA integrate_moved_by_the_bounds=%d\n", ex, name, c_moved);
    fflush(stdout);

    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex12_integrate(s_phy_pos, s_phy_vel, s_phy_acc, s_phy_outc, lo_tight, hi_tight);
    }
    uint32_t int_pie = ex07_ccount() - t0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex12_integrate_c(s_phy_pos, s_phy_vel, s_phy_acc, s_phy_refc, lo_tight, hi_tight);
    }
    uint32_t int_c = ex07_ccount() - t0;
    printf("BENCH integrate objects=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", reps * 8, int_pie,
           int_c);

    /* --- B: the AABB masks. Boxes 0..4 sit inside the query box, then one box per axis that separates,
     * one that separates on two axes, one that only TOUCHES (bmin == qmax, which is not a separation), one
     * that separates below qmin, and the rest random. The query and the boxes both carry negative
     * coordinates on purpose: the compares are signed 16-bit. 19 boxes so the scalar tail runs too. --- */
    s_phy_query[0] = -500;
    s_phy_query[1] = 500;
    s_phy_query[2] = -400;
    s_phy_query[3] = 400;
    s_phy_query[4] = -300;
    s_phy_query[5] = 300;
    for (uint32_t b = 0; b < BOXES; b++) {
        for (int ax = 0; ax < 3; ax++) {
            int16_t qmin = s_phy_query[2 * ax], qmax = s_phy_query[2 * ax + 1];
            int mode;                       /* 0 inside, 1 above qmax, 2 below qmin, 3 touching, -1 random */
            if (b < 5) {
                mode = 0;
            } else if (b == 5) {
                mode = (ax == 1) ? 1 : 0;
            } else if (b == 6) {
                mode = (ax == 2) ? 1 : 0;
            } else if (b == 7) {
                mode = (ax == 1) ? 0 : 1;
            } else if (b == 8) {
                mode = 3;
            } else if (b == 9) {
                mode = (ax == 0) ? 2 : 0;
            } else {
                mode = -1;
            }
            int16_t lo, hi;
            switch (mode) {
            case 0:
                lo = (int16_t)(qmin + rnd16(0, 100));
                hi = (int16_t)(lo + rnd16(0, 200));
                break;
            case 1:
                lo = (int16_t)(qmax + 10);
                hi = (int16_t)(lo + 50);
                break;
            case 2:
                hi = (int16_t)(qmin - 10);
                lo = (int16_t)(hi - 50);
                break;
            case 3:
                lo = qmax;
                hi = (int16_t)(qmax + 50);
                break;
            default:
                lo = rnd16(-30000, 30000);
                hi = (int16_t)(lo + rnd16(0, 1000));
                break;
            }
            box_set(s_phy_boxes, b, ax, lo, hi);
        }
    }

    ex12_sat_masks(s_phy_boxes, s_phy_query, s_phy_masks, BOXES);
    ex12_sat_masks_c(s_phy_boxes, s_phy_query, s_phy_masks_ref, BOXES);
    int b_ok = 0, overlaps = 0;
    for (uint32_t b = 0; b < BOXES; b++) {
        b_ok += s_phy_masks[b] == s_phy_masks_ref[b];
        overlaps += s_phy_masks[b] == -1;
    }
    check(ex, name, "sat_masks_matches_the_C_reference", b_ok, BOXES);
    fail += (b_ok == (int)BOXES) ? 0 : 1;
    ok = (overlaps > 0 && overlaps < (int)BOXES);
    check(ex, name, "sat_masks_reports_both_verdicts", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;

    print_i16(ex, name, "boxes", s_phy_boxes, 48 * BOX_GROUPS);
    print_i16(ex, name, "query", s_phy_query, 6);
    print_i16(ex, name, "masks", s_phy_masks, BOXES);
    printf("EX %s %s DATA n_boxes=%d\n", ex, name, BOXES);
    printf("EX %s %s DATA overlap_boxes=%d\n", ex, name, overlaps);
    fflush(stdout);

    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex12_sat_masks(s_phy_boxes, s_phy_query, s_phy_masks, BOXES);
    }
    uint32_t sm_pie = ex07_ccount() - t0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex12_sat_masks_c(s_phy_boxes, s_phy_query, s_phy_masks_ref, BOXES);
    }
    uint32_t sm_c = ex07_ccount() - t0;
    printf("BENCH sat_masks boxes=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", reps * BOXES, sm_pie,
           sm_c);

    /* --- C: squared distances. Points 0..5 are hand-built to cover the boundaries: identical points; a
     * difference of exactly 32767 (no saturation); distances just under and just over 2^31 (26756 and
     * 26757 on all three axes); a difference of 32768, which SATURATES to 32767 and so is not 32768^2; and
     * all three axes at 65535, the saturated maximum. 6.. are random. --- */
    static const int16_t spec_a[6] = {0, 32767, 26756, 26757, 0, 32767};
    static const int16_t spec_b[6] = {0, 0, 0, 0, -32768, -32768};
    for (uint32_t i = 0; i < POINTS; i++) {
        for (int ax = 0; ax < 3; ax++) {
            s_phy_pta[24 * (int)(i / 8) + 8 * ax + (int)(i % 8)] = rnd16(-32768, 32767);
            s_phy_ptb[24 * (int)(i / 8) + 8 * ax + (int)(i % 8)] = rnd16(-32768, 32767);
        }
    }
    for (uint32_t i = 0; i < 6; i++) {
        for (int ax = 0; ax < 3; ax++) {
            s_phy_pta[24 * (int)(i / 8) + 8 * ax + (int)(i % 8)] = spec_a[i];
            s_phy_ptb[24 * (int)(i / 8) + 8 * ax + (int)(i % 8)] = spec_b[i];
        }
    }

    ex12_dist2_qacc(s_phy_pta, s_phy_ptb, s_phy_d2, POINTS);
    ex12_dist2_qacc_c(s_phy_pta, s_phy_ptb, s_phy_d2_ref, POINTS);
    int d_ok = 0, d_clamp = 0, d_satax = 0, d_changed = 0, d_max_delta = 0;
    for (uint32_t i = 0; i < POINTS; i++) {
        int64_t sat_sum = 0, exact_sum = 0;
        for (int ax = 0; ax < 3; ax++) {
            int32_t a = s_phy_pta[24 * (int)(i / 8) + 8 * ax + (int)(i % 8)];
            int32_t b = s_phy_ptb[24 * (int)(i / 8) + 8 * ax + (int)(i % 8)];
            int32_t d = a - b;
            int32_t ad = (d < 0) ? -d : d;
            if (d > 32767 || d < -32768) {
                d_satax++;                          /* EE.VSUBS.S16 clamps this axis */
            }
            if (ad > d_max_delta) {
                d_max_delta = ad;
            }
            int16_t ds = sat16(d);
            sat_sum += (int32_t)ds * ds;
            exact_sum += (int64_t)d * d;
        }
        d_ok += s_phy_d2[i] == s_phy_d2_ref[i];
        d_clamp += sat_sum > 0x7FFFFFFF;
        d_changed += sat_sum != exact_sum;
    }
    check(ex, name, "dist2_matches_the_C_reference", d_ok, POINTS);
    fail += (d_ok == (int)POINTS) ? 0 : 1;
    ok = (d_clamp > 0);
    check(ex, name, "dist2_reaches_the_int32_clamp", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;
    ok = (d_satax > 0 && d_changed > 0);
    check(ex, name, "dist2_reaches_both_the_saturating_difference_and_its_effect", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;
    ok = (d_satax > 0 && d_max_delta > 32767);
    check(ex, name, "dist2_max_abs_delta_exceeds_the_16_bit_difference", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;

    print_i16(ex, name, "pt_a", s_phy_pta, 24 * POINT_GROUPS);
    print_i16(ex, name, "pt_b", s_phy_ptb, 24 * POINT_GROUPS);
    print_i32(ex, name, "dist2", s_phy_d2, POINTS);
    printf("EX %s %s DATA n_points=%d\n", ex, name, POINTS);
    printf("EX %s %s DATA dist2_int32_clamps=%d\n", ex, name, d_clamp);
    printf("EX %s %s DATA dist2_saturating_axis_diffs=%d\n", ex, name, d_satax);
    printf("EX %s %s DATA dist2_max_abs_delta=%d\n", ex, name, d_max_delta);
    printf("EX %s %s DATA dist2_changed_by_the_saturating_difference=%d\n", ex, name, d_changed);
    fflush(stdout);

    const uint32_t q16_written = ex12_dist2_q16(s_phy_pta, s_phy_ptb, s_phy_d2q, POINTS, Q16_SHIFT);
    const uint32_t q16_ref_written = ex12_dist2_q16_c(s_phy_pta, s_phy_ptb, s_phy_d2q_ref, POINTS,
                                                     Q16_SHIFT);
    int q_ok = 0, q_sat = 0;
    for (uint32_t i = 0; i < q16_written; i++) {
        q_ok += s_phy_d2q[i] == s_phy_d2q_ref[i];
        q_sat += s_phy_d2q[i] == 32767 || s_phy_d2q[i] == -32768;
    }
    ok = (q_ok == (int)q16_written);
    check(ex, name, "dist2_q16_matches_sat16_of_the_shifted_lane", q_ok, (int)q16_written);
    fail += ok ? 0 : 1;
    ok = (q16_written == (POINTS & ~7u) && q16_ref_written == q16_written);
    check(ex, name, "dist2_q16_writes_whole_groups_only_and_returns_the_count", q16_written,
          (int)(POINTS & ~7u));
    fail += ok ? 0 : 1;
    ok = (q_sat > 0);
    check(ex, name, "dist2_q16_readout_saturates_at_this_shift", ok ? 1 : 0, 1);
    fail += ok ? 0 : 1;

    print_i16(ex, name, "dist2_q16", s_phy_d2q, (int)q16_written);
    printf("EX %s %s DATA q16_shift=%d\n", ex, name, Q16_SHIFT);
    printf("EX %s %s DATA q16_written=%d\n", ex, name, (int)q16_written);
    printf("EX %s %s DATA q16_saturated_lanes=%d\n", ex, name, q_sat);
    fflush(stdout);

    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex12_dist2_qacc(s_phy_pta, s_phy_ptb, s_phy_d2, POINTS);
    }
    uint32_t d2_pie = ex07_ccount() - t0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        ex12_dist2_qacc_c(s_phy_pta, s_phy_ptb, s_phy_d2_ref, POINTS);
    }
    uint32_t d2_c = ex07_ccount() - t0;
    printf("BENCH dist2 pairs=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n", reps * POINTS, d2_pie,
           d2_c);
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        s_bench_sink = ex12_dist2_q16(s_phy_pta, s_phy_ptb, s_phy_d2q, POINTS, Q16_SHIFT);
    }
    uint32_t q16_pie = ex07_ccount() - t0;
    t0 = ex07_ccount();
    for (int i = 0; i < reps; i++) {
        s_bench_sink = ex12_dist2_q16_c(s_phy_pta, s_phy_ptb, s_phy_d2q_ref, POINTS, Q16_SHIFT);
    }
    uint32_t q16_c = ex07_ccount() - t0;
    printf("BENCH dist2_q16 pairs=%d cycles_pie=%" PRIu32 " cycles_c=%" PRIu32 "\n",
           reps * (int)(POINTS & ~7u), q16_pie, q16_c);
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
    printf("ENV examples=12 buffers=16-byte-aligned rng_seed=0x1234 c_flags=-O2\n");
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
    ex10();
    ex11();
    ex12();

    printf("SUMMARY checks_ok=%d checks_fail=%d\n", s_ok, s_fail);
    printf("END\n");
    fflush(stdout);
}
