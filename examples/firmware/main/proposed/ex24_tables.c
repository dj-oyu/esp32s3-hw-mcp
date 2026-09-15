/* ex24_tables.c -- the exact coefficient table mp3_decode.c builds, dumped for the models.
 *
 * A copy of main/pocket/mp3_decode.c:27-45 (filter_init) with the struct fields inlined, compiled on the
 * HOST with the same float32 arithmetic (float + sinf/cosf/lroundf), so these are the numbers the device
 * computes. Nothing in the device tree is modified: this is a copy for arithmetic only.
 *
 *   cc -O2 -o /tmp/ex24_tables /tmp/ex24_tables.c -lm && /tmp/ex24_tables > /tmp/ex24_tables.txt
 */
#include <stdio.h>
#include <math.h>
#include <stdint.h>

static int16_t filter[32];

static void filter_init(unsigned rate) {
    float taps[32], sum=0;
    float cutoff=10800.0f/(float)rate;
    for(unsigned i=0;i<32;i++) {
        float x=(float)i-15.5f;
        taps[i]=sinf(6.28318530718f*cutoff*x)/(3.14159265359f*x)*
                (0.54f-0.46f*cosf(6.28318530718f*i/31.0f));
        sum+=taps[i];
    }
    int total=0;
    for(unsigned i=0;i<32;i++) {
        filter[i]=(int16_t)lroundf(taps[i]*16384.0f/sum);
        total+=filter[i];
    }
    filter[15]+=(int16_t)(16384-total);
}

static void filter_init_raw(unsigned rate, int16_t *out, long *total_out) {
    float taps[32], sum=0;
    float cutoff=10800.0f/(float)rate;
    for(unsigned i=0;i<32;i++) {
        float x=(float)i-15.5f;
        taps[i]=sinf(6.28318530718f*cutoff*x)/(3.14159265359f*x)*
                (0.54f-0.46f*cosf(6.28318530718f*i/31.0f));
        sum+=taps[i];
    }
    long total=0;
    for(unsigned i=0;i<32;i++) { out[i]=(int16_t)lroundf(taps[i]*16384.0f/sum); total+=out[i]; }
    *total_out=total;
}

int main(void){
    /* the three rates the device filters (mp3_decode.c:54 runs filter_init when h.rate > 24000) */
    unsigned rates[3]={32000,44100,48000};
    printf("# rate rawtotal corr L1 accx_bound asym_indices\n");
    for(int r=0;r<3;r++){
        unsigned rate=rates[r];
        int16_t raw[32]; long rawtotal=0;
        filter_init_raw(rate,raw,&rawtotal);
        filter_init(rate);
        long sum=0, l1=0, accmax=0;
        int asym=0;
        for(unsigned i=0;i<32;i++){
            sum+=filter[i]; l1+=filter[i]<0?-filter[i]:filter[i];
            if(filter[i]!=filter[31-i]) asym++;
            accmax+=32768L*(filter[i]<0?-filter[i]:filter[i]);
        }
        int rawsym=0;
        for(unsigned i=0;i<32;i++) if(raw[i]!=raw[31-i]) rawsym++;
        printf("TABLE %u %ld %ld %ld %ld %d %d\n",rate,rawtotal,16384-rawtotal,l1,accmax,asym,rawsym);
        printf("FILTER %u",rate);
        for(unsigned i=0;i<32;i++) printf(" %d",filter[i]);
        printf("\n");
        /* the kernel's table: filter_rev[i] = filter[31-i] */
        printf("REV %u",rate);
        for(unsigned i=0;i<32;i++) printf(" %d",filter[31-i]);
        printf("\n");
        /* the folded table the _sym kernel wants: [filter[0..14], filter[16]] */
        printf("HALF %u",rate);
        for(unsigned i=0;i<15;i++) printf(" %d",filter[i]);
        printf(" %d\n",filter[16]);
        printf("SYM %u filter[15]=%d filter[16]=%d correction=%d\n",rate,filter[15],filter[16],
               filter[15]-filter[16]);
    }
    return 0;
}
