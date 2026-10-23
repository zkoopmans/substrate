#include <stdio.h>
#include <time.h>
#include <stdint.h>

int main(int argc, char **argv) {
    struct timespec mono, real;
    clock_gettime(CLOCK_MONOTONIC, &mono);
    clock_gettime(CLOCK_REALTIME, &real);
    printf("MONO_NS:%ld REAL_NS:%ld\n", 
           (int64_t)mono.tv_sec * 1000000000LL + mono.tv_nsec,
           (int64_t)real.tv_sec * 1000000000LL + real.tv_nsec);
    return 0;
}
