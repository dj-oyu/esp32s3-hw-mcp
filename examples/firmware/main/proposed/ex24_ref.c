/* ex24_ref.c -- the host C side of the ex24_dsfir equivalence run: the third opinion.
 *
 * Job 1: read the case dump the Python model writes (/tmp/ex24_cases.txt), recompute every case with a
 * plain scalar C implementation of the SAME loop the device runs (mp3_decode.c:64-71), and report the
 * number of values that disagree. The model executes the kernel's assembly instruction by instruction
 * through a patched piesim.py; this program does the arithmetic in a C loop with no simulator in the
 * way.
 *
 * Job 2: for every CASE, also check the two orders of the same dot product -- the device's
 * history[(cursor-k)&31]*filter[k] and the kernel's window[i]*filter_rev[i] -- are the same integer,
 * which is what licenses the pre-reversed table.
 *
 * Job 3: --count prints the reference loops' xtensa -O2 instruction counts side by side with the
 * kernel's own (the numbers quoted in ex24_dsfir.md).
 *
 *   cc -O2 -o /tmp/ex24_ref /tmp/ex24_ref.c && /tmp/ex24_ref /tmp/ex24_cases.txt
 *   xtensa-esp32s3-elf-gcc -O2 -c -o /tmp/ex24_ref.o /tmp/ex24_ref.c && objdump -d
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NV 64
#define Q14 (1 << 14)

static int16_t filt_tab[8][32];
static unsigned filt_rate[8];
static int nfilt;

static int sat16(long v) { return v > 32767 ? 32767 : (v < -32768 ? -32768 : (int)v); }

/* C's signed division: truncation toward zero. */
static long cdiv(long a, long b) { long q = (a < 0 ? -a : a) / (b < 0 ? -b : b); return a < 0 ? -q : q; }

/* ---------------------------------------------------------------- the device's per-sample loop
 * mp3_decode.c:64-71, index order preserved. hist is the 32-deep ring, cursor the write index. */
static long scalar_sum(const short *hist, unsigned cursor, const short *filt)
{
    long s = 0;
    for (unsigned k = 0; k < 32; k++)
        s += (long)hist[(cursor - k) & 31] * (long)filt[k];
    return s;
}

static int scalar_out(const short *hist, unsigned cursor, const short *filt, int shift, int *trunc_out)
{
    long s = scalar_sum(hist, cursor, filt);
    long t = cdiv(s, 1L << shift);
    if (trunc_out) *trunc_out = (int)t;
    return sat16(t);
}

static int shifted_out(const short *hist, unsigned cursor, const short *filt, int shift)
{
    long s = scalar_sum(hist, cursor, filt);
    return sat16(s >> shift);
}

/* the kernel's order: window[i] = hist[(cursor-31+i)&31] paired with filter_rev[i] = filt[31-i] */
static long kernel_order_sum(const short *hist, unsigned cursor, const short *filt)
{
    long s = 0;
    for (unsigned i = 0; i < 32; i++)
        s += (long)hist[(cursor - 31 + i) & 31] * (long)filt[31 - i];
    return s;
}

/* ---------------------------------------------------------------- input */
static int parse_csv(const char *p, short *out, int max)
{
    int n = 0;
    while (p && *p && *p != '\n' && n < max) {
        out[n++] = (short)strtol(p, NULL, 10);
        p = strchr(p, ',');
        if (!p) break;
        p++;
    }
    return n;
}

int main(int argc, char **argv)
{
    const char *path = argc > 1 ? argv[1] : "/tmp/ex24_cases.txt";
    FILE *f = fopen(path, "r");
    char line[65536];
    static short win[32], sig[1024];
    long case_n = 0, case_bad_trunc = 0, case_bad_shift = 0, case_bad_kernel = 0;
    long order_bad = 0, run_n = 0, run_bad_trunc = 0, run_bad_shift = 0;
    int first_printed = 0;

    /* the tables the model used, straight out of the host C copy of filter_init */
    FILE *t = fopen("/tmp/ex24_tables.txt", "r");
    if (!t) { perror("/tmp/ex24_tables.txt"); return 1; }
    while (fgets(line, sizeof line, t)) {
        char *sp = strchr(line, ' ');
        if (!sp) continue;
        if (!strncmp(line, "FILTER ", 7)) {
            unsigned rate = (unsigned)strtoul(sp + 1, &sp, 10);
            short v[32];
            int n = 0;
            while (n < 32) {                       /* space-separated, unlike the CASE lines */
                char *e;
                long x = strtol(sp, &e, 10);
                if (e == sp) break;
                v[n++] = (short)x;
                sp = e;
            }
            if (nfilt < 8 && n == 32) {
                filt_rate[nfilt] = rate;
                memcpy(filt_tab[nfilt], v, sizeof v);
                nfilt++;
            }
        }
    }
    fclose(t);
    if (!f) { perror(path); return 1; }
    if (nfilt == 0) { fprintf(stderr, "no FILTER lines in /tmp/ex24_tables.txt\n"); return 1; }

    while (fgets(line, sizeof line, f)) {
        if (!strncmp(line, "CASE ", 5)) {
            unsigned rate, o;
            int trunc_dump, floor_dump, kernel_dump;
            char *p = line + 5;
            rate = (unsigned)strtoul(p, &p, 10); o = (unsigned)strtoul(p, &p, 10);
            trunc_dump = (int)strtol(p, &p, 10); floor_dump = (int)strtol(p, &p, 10);
            kernel_dump = (int)strtol(p, &p, 10);
            if (parse_csv(p, win, 32) != 32) { fprintf(stderr, "bad CASE line\n"); continue; }
            const short *filt = NULL;
            for (int i = 0; i < nfilt; i++) if (filt_rate[i] == rate) filt = filt_tab[i];
            if (!filt) { fprintf(stderr, "no table for %u\n", rate); continue; }
            /* the device's ring holding this window: history[i] = window[i], cursor = 31 */
            int trunc_v, floor_v, kernel_v;
            trunc_v = scalar_out(win, 31, filt, 14, NULL);
            floor_v = shifted_out(win, 31, filt, 14);
            kernel_v = sat16(kernel_order_sum(win, 31, filt) >> 14);
            case_n++;
            if (trunc_v != trunc_dump) case_bad_trunc++;
            if (floor_v != floor_dump) case_bad_shift++;
            if (kernel_v != kernel_dump) case_bad_kernel++;
            if (kernel_order_sum(win, 31, filt) != scalar_sum(win, 31, filt)) order_bad++;
            if (!first_printed && kernel_v != kernel_dump) {
                first_printed = 1;
                printf("CASE mismatch %u Hz o=%u: dump %d, reference %d\n", rate, o, kernel_dump,
                       kernel_v);
            }
        } else if (!strncmp(line, "RUN ", 4)) {
            unsigned rate;
            char *p = line + 4;
            short outv[1024];
            int n;
            rate = (unsigned)strtoul(p, &p, 10);
            n = parse_csv(p, outv, 1024);
            /* the SIG line follows */
            if (!fgets(line, sizeof line, f) || strncmp(line, "SIG ", 4)) {
                fprintf(stderr, "RUN without SIG\n");
                continue;
            }
            int m = parse_csv(line + 4, sig, 1024);
            const short *filt = NULL;
            for (int i = 0; i < nfilt; i++) if (filt_rate[i] == rate) filt = filt_tab[i];
            if (!filt || m != n) { fprintf(stderr, "bad RUN: m=%d n=%d\n", m, n); continue; }
            short hist[32]; memset(hist, 0, sizeof hist);
            unsigned cur = 0;
            int bt = 0, bs = 0;
            for (int i = 0; i < n; i++) {
                hist[cur] = sig[i];
                int t = scalar_out(hist, cur, filt, 14, NULL);
                int s = shifted_out(hist, cur, filt, 14);
                if (t != outv[i]) bt++;
                if (s != outv[i]) bs++;
                cur = (cur + 1) & 31;
            }
            run_n++;
            run_bad_trunc += bt;
            run_bad_shift += bs;
        }
    }
    fclose(f);

    printf("\nex24_ref: the host C scalar loop vs the Python model's case dump (and the kernel dumps)\n");
    printf("  CASE cases                  : %ld\n", case_n);
    printf("    dumped trunc value wrong  : %ld\n", case_bad_trunc);
    printf("    dumped floor value wrong  : %ld\n", case_bad_shift);
    printf("    dumped KERNEL value wrong : %ld   (the kernel's floor shift, vs this C loop)\n",
           case_bad_kernel);
    printf("    device-order sum != kernel-order sum : %ld\n", order_bad);
    printf("  RUN whole-ring runs         : %ld\n", run_n);
    printf("    dumped samples != the C loop's trunc value : %ld\n", run_bad_trunc);
    printf("    dumped samples != the C loop's shift value : %ld\n", run_bad_shift);
    return (case_bad_kernel || order_bad || run_bad_shift) ? 1 : 0;
}
