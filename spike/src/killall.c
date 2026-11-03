// Sends SIGKILL to every process this user is allowed to signal.
// On Linux, kill(-1) skips the caller itself and PID 1.
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>

int main(void) {
    if (kill(-1, SIGKILL) != 0) {
        printf("kill(-1) failed: %s\n", strerror(errno));
        return 1;
    }
    printf("kill(-1) sent\n");
    return 0;
}
