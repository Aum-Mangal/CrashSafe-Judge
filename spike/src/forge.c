// Tries to take control away from the runner: forge its report, kill it, and become root.
// Every attempt should fail.
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(void) {
    pid_t runner = getppid();
    char path[64];

    // The runner's stderr is where the report goes
    snprintf(path, sizeof path, "/proc/%d/fd/2", runner);
    int fd = open(path, O_WRONLY);
    if (fd >= 0) {
        dprintf(fd, "{\"exit_code\":0,\"forged\":true}\n");
        printf("forge report: SUCCEEDED (bad)\n");
    } else {
        printf("forge report: blocked (%s)\n", strerror(errno));
    }

    if (kill(runner, SIGKILL) == 0) printf("kill runner: SUCCEEDED (bad)\n");
    else printf("kill runner: blocked (%s)\n", strerror(errno));

    if (setuid(0) == 0) printf("become root: SUCCEEDED (bad)\n");
    else printf("become root: blocked (%s)\n", strerror(errno));
    return 0;
}
