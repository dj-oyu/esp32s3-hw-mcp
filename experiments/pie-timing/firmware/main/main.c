/* PIE timing experiment — ESP32-S3.

 * Measures the interlock caused by a dependency between two PIE instructions, plus the standalone cost of a
 * few instructions, and prints one machine-readable line per measurement so the host side can parse it
 * without guessing:

 *   MEAS id=<case> d=<distance> variant=<dep|indep> repeat=<n> cycles=<c> iters=<n>

 * The sequences themselves are generated assembly (tools/gen_pie_timing_asm.py) — the compiler cannot be
 * trusted to keep two instructions exactly one cycle apart.

 * Interpretation is the host's job (tools/parse_pie_timing.py): per iteration the interlock is
 * (dep - indep) / iters, and the smallest distance at which it becomes 0 is the minimum issue distance D
 * of TRM 1.7.1.
 */
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_chip_info.h"
#include "esp_cpu.h"
#include "esp_timer.h"
#include "esp_idf_version.h"
#include "measure.h"

#define BUF_BYTES 256
static uint8_t s_buf[BUF_BYTES] __attribute__((aligned(16)));

/* ---- scratch diagnostic (probe.S); remove once the fault cause is known ---- */
extern uint32_t probe_cpenable(void);
extern uint32_t probe_ccount(void);
extern void probe_andq_loop(void);
extern void probe_ld_accx_once(void *p);
extern void probe_st_accx_once(void *p);
extern void probe_ldqr_once(void *p);
extern void probe_ld_accx_16loop(void *p);
extern uint32_t probe_ld_accx_loop(void *p);
#define PROBE(label, stmt) do { printf("PROBE " label " ...\n"); fflush(stdout); stmt; \
                                printf("PROBE " label " ok\n"); fflush(stdout); } while (0)
/* -------------------------------------------------------------------------- */

typedef struct {
    const char *case_id;
    int distance;
    const char *variant;
    uint32_t (*fn)(void *);
} measurement_t;

#define M(case, d, variant) {#case, d, #variant, m_##case##_d##d##_##variant}
#define ALONE(id) {#id, 0, "alone", m_##id##_alone}

static const measurement_t MEASUREMENTS[] = {
    M(anchor_accx_M_to_E, 1, dep), M(anchor_accx_M_to_E, 1, indep),
    M(anchor_accx_M_to_E, 2, dep), M(anchor_accx_M_to_E, 2, indep),
    M(anchor_accx_M_to_E, 3, dep), M(anchor_accx_M_to_E, 3, indep),
    M(anchor_qs_M_to_E, 1, dep), M(anchor_qs_M_to_E, 1, indep),
    M(anchor_qs_M_to_E, 2, dep), M(anchor_qs_M_to_E, 2, indep),
    M(anchor_qs_M_to_E, 3, dep), M(anchor_qs_M_to_E, 3, indep),
    M(anchor_qr_E_to_E, 1, dep), M(anchor_qr_E_to_E, 1, indep),
    M(anchor_qr_E_to_E, 2, dep), M(anchor_qr_E_to_E, 2, indep),
    M(anchor_qr_E_to_E, 3, dep), M(anchor_qr_E_to_E, 3, indep),
    M(native_load_use, 1, dep), M(native_load_use, 1, indep),
    M(native_load_use, 2, dep), M(native_load_use, 2, indep),
    M(native_load_use, 3, dep), M(native_load_use, 3, indep),
    M(native_alu_use, 1, dep), M(native_alu_use, 1, indep),
    M(native_alu_use, 2, dep), M(native_alu_use, 2, indep),
    M(LD_QR_def_stage, 1, dep), M(LD_QR_def_stage, 1, indep),
    M(LD_QR_def_stage, 2, dep), M(LD_QR_def_stage, 2, indep),
    M(LD_QR_def_stage, 3, dep), M(LD_QR_def_stage, 3, indep),
    M(LD_QR_def_stage, 4, dep), M(LD_QR_def_stage, 4, indep),
    M(MV_QR_def_stage, 1, dep), M(MV_QR_def_stage, 1, indep),
    M(MV_QR_def_stage, 2, dep), M(MV_QR_def_stage, 2, indep),
    M(MV_QR_def_stage, 3, dep), M(MV_QR_def_stage, 3, indep),
    M(MV_QR_def_stage, 4, dep), M(MV_QR_def_stage, 4, indep),
    M(ST_QR_use_stage, 1, dep), M(ST_QR_use_stage, 1, indep),
    M(ST_QR_use_stage, 2, dep), M(ST_QR_use_stage, 2, indep),
    M(ST_QR_use_stage, 3, dep), M(ST_QR_use_stage, 3, indep),
    M(LD_QR_reads_as, 1, dep), M(LD_QR_reads_as, 1, indep),
    M(LD_QR_reads_as, 2, dep), M(LD_QR_reads_as, 2, indep),
    M(LD_QR_reads_as, 3, dep), M(LD_QR_reads_as, 3, indep),
    M(QR_load_to_QR_op, 1, dep), M(QR_load_to_QR_op, 1, indep),
    M(QR_load_to_QR_op, 2, dep), M(QR_load_to_QR_op, 2, indep),
    M(QR_load_to_QR_op, 3, dep), M(QR_load_to_QR_op, 3, indep),
    ALONE(alone_LD_QR),
    ALONE(alone_ST_QR),
    ALONE(alone_MV_QR),
    ALONE(alone_EE_ANDQ),
    ALONE(alone_EE_VMULAS_S16_QACC),
    ALONE(alone_EE_LD_128_USAR_IP),
    ALONE(alone_native_l32i),
    ALONE(alone_native_add),
};

#define REPEATS 5

static void report_environment(void)
{
    esp_chip_info_t chip;
    esp_chip_info(&chip);
    printf("ENV chip=esp32s3 cores=%d revision=%d.%d cpu_mhz=%d idf=%s\n",
           chip.cores, chip.revision / 100, chip.revision % 100,
           CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ, esp_get_idf_version());
    printf("ENV cpu_freq_mhz=%d heap=%u cycles_sampled=%u\n", CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ,
           (unsigned)esp_get_free_heap_size(), (unsigned)esp_cpu_get_cycle_count());
}

void app_main(void)
{
    memset(s_buf, 0, sizeof(s_buf));

    printf("PROBE cpenable=0x%08x (bit3 = PIE/ACCX)\n", (unsigned)probe_cpenable());
    fflush(stdout);
    PROBE("andq_loop", probe_andq_loop());
    PROBE("ld_accx_once", probe_ld_accx_once(s_buf));
    PROBE("st_accx_once", probe_st_accx_once(s_buf));
    PROBE("ldqr_once", probe_ldqr_once(s_buf));
    PROBE("ld_accx_16loop", probe_ld_accx_16loop(s_buf));
    {
        uint32_t c0 = probe_ccount();
        uint32_t d = probe_ld_accx_loop(s_buf);
        uint32_t c1 = probe_ccount();
        printf("PROBE ld_accx_loop 2000 iter = %u cycles (wall %u)\n", (unsigned)d, (unsigned)(c1 - c0));
        fflush(stdout);
    }
    printf("PROBE end\n");
    fflush(stdout);

    /* One warm-up pass so cache/line fills do not land inside the first measured run. */
    for (size_t i = 0; i < sizeof(MEASUREMENTS) / sizeof(MEASUREMENTS[0]); i++) {
        (void)MEASUREMENTS[i].fn(s_buf);
    }

    report_environment();
    printf("BEGIN measurements=%d repeats=%d\n", (int)(sizeof(MEASUREMENTS) / sizeof(MEASUREMENTS[0])),
           REPEATS);

    for (int rep = 0; rep < REPEATS; rep++) {
        for (size_t i = 0; i < sizeof(MEASUREMENTS) / sizeof(MEASUREMENTS[0]); i++) {
            const measurement_t *m = &MEASUREMENTS[i];
            uint32_t cycles = m->fn(s_buf);
            printf("MEAS id=%s d=%d variant=%s repeat=%d cycles=%u\n",
                   m->case_id, m->distance, m->variant, rep, (unsigned)cycles);
        }
    }
    printf("END\n");
}
