#include <stdio.h>
#include <unistd.h>
#include <time.h>
#include <stdint.h>
#include <signal.h>

volatile int tick_count = 0;

void handler(int sig) {
    tick_count++;
}

int main() {
    struct sigaction sa = { .sa_handler = handler };
    sigaction(SIGALRM, &sa, NULL);

    timer_t timerid;
    struct sigevent sev = {
        .sigev_notify = SIGEV_SIGNAL,
        .sigev_signo = SIGALRM,
    };
    timer_create(CLOCK_MONOTONIC, &sev, &timerid);

    // Tick every 100ms
    struct itimerspec its = {
        .it_interval = { .tv_sec = 0, .tv_nsec = 100000000 },
        .it_value    = { .tv_sec = 0, .tv_nsec = 100000000 },
    };
    timer_settime(timerid, 0, &its, NULL);

    struct timespec mono;
    clock_gettime(CLOCK_MONOTONIC, &mono);
    printf("START_TICK:%d MONO_SEC:%ld\n", tick_count, mono.tv_sec);
    fflush(stdout);

    while (1) {
        pause();
        clock_gettime(CLOCK_MONOTONIC, &mono);
        printf("TICK:%d MONO_SEC:%ld.%03ld\n", tick_count, mono.tv_sec, mono.tv_nsec / 1000000);
        fflush(stdout);
    }
    return 0;
}
