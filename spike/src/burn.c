// Burns a fixed amount of CPU work, so its CPU time can be compared across runtimes.
#include <stdlib.h>

int main(int argc, char **argv) {
    long n = argc > 1 ? atol(argv[1]) : 1000000000L;
    // volatile stops the compiler from deleting the loop
    for (volatile long i = 0; i < n; i++) {}
    return 0;
}
