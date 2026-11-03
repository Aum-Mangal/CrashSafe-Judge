// Allocates and touches memory in 16 MB steps until it reaches the target or is killed.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    long target_mb = argc > 1 ? atol(argv[1]) : 512;
    for (long mb = 16; mb <= target_mb; mb += 16) {
        char *p = malloc(16L << 20);
        if (!p) {
            printf("malloc failed at %ld MB\n", mb);
            return 2;
        }
        // Touching the pages is what makes the kernel actually hand over memory
        memset(p, 1, 16L << 20);
        if (mb % 64 == 0) {
            printf("touched %ld MB\n", mb);
            fflush(stdout);
        }
    }
    return 0;
}
