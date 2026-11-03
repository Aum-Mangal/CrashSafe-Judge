// Runs a program and prints the CPU time, wall time and peak memory that wait4 reports for it.
// This is the measurement the judge-runner will rely on.
#include <stdio.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static long to_ms(struct timeval t) {
    return t.tv_sec * 1000 + t.tv_usec / 1000;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: measure PROGRAM [ARGS...]\n");
        return 2;
    }

    struct timespec start, end;
    clock_gettime(CLOCK_MONOTONIC, &start);

    pid_t pid = fork();
    if (pid == 0) {
        execv(argv[1], argv + 1);
        _exit(127);
    }

    int status;
    struct rusage ru;
    wait4(pid, &status, 0, &ru);
    clock_gettime(CLOCK_MONOTONIC, &end);

    long wall_ms = (end.tv_sec - start.tv_sec) * 1000 + (end.tv_nsec - start.tv_nsec) / 1000000;
    printf("cpu_ms=%ld wall_ms=%ld maxrss_kb=%ld status=%d\n",
           to_ms(ru.ru_utime) + to_ms(ru.ru_stime), wall_ms, ru.ru_maxrss, status);
    return 0;
}
