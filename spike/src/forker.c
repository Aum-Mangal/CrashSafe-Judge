// Forks up to N sleeping children, reports how many succeeded and why it stopped, then cleans up.
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_CHILDREN 4096

int main(int argc, char **argv) {
    int n = argc > 1 ? atoi(argv[1]) : 200;
    if (n > MAX_CHILDREN) n = MAX_CHILDREN;
    static pid_t kids[MAX_CHILDREN];
    int made = 0;

    for (; made < n; made++) {
        pid_t pid = fork();
        if (pid < 0) {
            printf("fork %d failed: %s\n", made + 1, strerror(errno));
            break;
        }
        if (pid == 0) {
            pause();
            _exit(0);
        }
        kids[made] = pid;
    }
    if (made == n) printf("all %d forks succeeded\n", n);

    for (int i = 0; i < made; i++) kill(kids[i], SIGKILL);
    while (wait(NULL) > 0) {}
    return 0;
}
