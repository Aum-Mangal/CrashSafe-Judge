// judge-runner: runs one untrusted program inside the sandbox and reports what happened.
//
// It starts as root inside the gVisor container and runs the program as uid 65534, so the
// program can't signal the runner or reach its file descriptors. Limits are enforced from
// in here because killing `docker exec` from outside leaves the program running.
//
// Output: the program's stdout on stdout (up to the output limit), then exactly one JSON
// report line on stderr. The runner judges nothing; the worker turns the report into a verdict.
//
// Usage: judge-runner [options] -- PROGRAM [ARGS...]
//   --cpu-ms N   CPU limit; RLIMIT_CPU kills the program about 1 s after it (default 1000)
//   --wall-ms N  wall-clock limit; the program is killed when it's reached (default 3000)
//   --mem-mb N   address-space limit (RLIMIT_AS); 0 means none (default 0)
//   --out-kb N   stdout limit; the program is killed once it's passed (default 16384)
//   --nproc N    process limit for uid 65534 (default 64)
//   --cwd DIR    working directory (default /work)

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define SANDBOX_UID 65534
#define SANDBOX_GID 65534
#define STDERR_CAP (64 * 1024)
#define FILE_SIZE_CAP (16L << 20)
#define DRAIN_MS 200

struct options {
    long cpu_ms, wall_ms, mem_mb, out_kb, nproc;
    const char *cwd;
    char **argv;
};

// What the program wrote, collected from its two pipes
struct capture {
    struct pollfd fds[2];  // [0] program stdout, [1] program stderr
    long out_bytes, out_cap;
    bool stdout_open, output_exceeded;
    char err[STDERR_CAP];
    size_t err_len;
};

enum { STEP_LIMITS, STEP_USER, STEP_CWD, STEP_EXEC, STEP_COUNT };
static const char *const STEP_NAMES[STEP_COUNT] = {"limits", "user", "cwd", "exec"};

static long now_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static long timeval_ms(struct timeval t) {
    return t.tv_sec * 1000 + t.tv_usec / 1000;
}

// Reports a failure of the runner itself as the JSON line and exits.
// The worker treats this as an infrastructure error, never as the submission's fault.
static void fail(const char *what) {
    fprintf(stderr, "{\"runner_error\":\"%s: %s\"}\n", what, strerror(errno));
    exit(3);
}

static long parse_long(const char *s) {
    char *end;
    errno = 0;
    long v = strtol(s, &end, 10);
    if (errno || *end || v < 0) {
        errno = EINVAL;
        fail("bad number");
    }
    return v;
}

static struct options parse_args(int argc, char **argv) {
    struct options o = {1000, 3000, 0, 16384, 64, "/work", NULL};
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--") == 0) {
            if (i + 1 < argc) o.argv = argv + i + 1;
            break;
        }
        if (i + 1 >= argc) break;
        long *target = NULL;
        if (strcmp(argv[i], "--cpu-ms") == 0) target = &o.cpu_ms;
        else if (strcmp(argv[i], "--wall-ms") == 0) target = &o.wall_ms;
        else if (strcmp(argv[i], "--mem-mb") == 0) target = &o.mem_mb;
        else if (strcmp(argv[i], "--out-kb") == 0) target = &o.out_kb;
        else if (strcmp(argv[i], "--nproc") == 0) target = &o.nproc;
        else if (strcmp(argv[i], "--cwd") == 0) {
            o.cwd = argv[++i];
            continue;
        } else break;
        *target = parse_long(argv[++i]);
    }
    if (!o.argv) {
        errno = EINVAL;
        fail("usage: judge-runner [options] -- PROGRAM [ARGS...]");
    }
    return o;
}

static bool write_all(int fd, const char *buf, size_t n) {
    while (n > 0) {
        ssize_t w = write(fd, buf, n);
        if (w < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        buf += w;
        n -= (size_t)w;
    }
    return true;
}

// Bytes from 0x80 up pass through unchanged, so UTF-8 text (like g++'s curly quotes) stays intact.
// The worker decodes with replacement, so invalid UTF-8 can't break the JSON either.
static void json_escape(FILE *f, const char *s, size_t n) {
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"' || c == '\\') fprintf(f, "\\%c", c);
        else if (c == '\n') fputs("\\n", f);
        else if (c < 0x20 || c == 0x7f) fprintf(f, "\\u%04x", c);
        else fputc(c, f);
    }
}

// Tells the runner which setup step failed, through the setup pipe, then exits.
static void child_fail(int setup_fd, int step) {
    int msg[2] = {step, errno};
    (void)!write(setup_fd, msg, sizeof msg);
    _exit(127);
}

// Runs in the forked child: applies limits, drops to the sandbox user, and starts the program.
static void run_child(const struct options *o, int out_fd, int err_fd, int setup_fd) {
    // Its own session, so the program and everything it starts can be killed as one group
    setsid();
    // The runner ignores SIGPIPE, and an ignored signal would stay ignored across exec
    signal(SIGPIPE, SIG_DFL);

    if (dup2(out_fd, STDOUT_FILENO) < 0 || dup2(err_fd, STDERR_FILENO) < 0) child_fail(setup_fd, STEP_LIMITS);
    // Close every other descriptor except the setup pipe, which closes itself on exec
    for (int fd = 3; fd < 1024; fd++) {
        if (fd != setup_fd) close(fd);
    }

    struct {
        int resource;
        rlim_t value;
    } limits[] = {
        {RLIMIT_CPU, (rlim_t)((o->cpu_ms + 999) / 1000 + 1)},
        {RLIMIT_CORE, 0},
        {RLIMIT_NOFILE, 64},
        {RLIMIT_FSIZE, FILE_SIZE_CAP},
        {RLIMIT_NPROC, (rlim_t)o->nproc},
        {RLIMIT_AS, o->mem_mb > 0 ? (rlim_t)o->mem_mb << 20 : RLIM_INFINITY},
    };
    for (size_t i = 0; i < sizeof limits / sizeof limits[0]; i++) {
        struct rlimit r = {limits[i].value, limits[i].value};
        if (setrlimit(limits[i].resource, &r) != 0) child_fail(setup_fd, STEP_LIMITS);
    }

    // Drop to the sandbox user last. From here on, the program can't touch the runner.
    if (setgroups(0, NULL) != 0 || setgid(SANDBOX_GID) != 0 || setuid(SANDBOX_UID) != 0) {
        child_fail(setup_fd, STEP_USER);
    }
    // /work belongs to the sandbox user and the runner can't bypass that, so chdir comes after setuid
    if (chdir(o->cwd) != 0) child_fail(setup_fd, STEP_CWD);

    // A fixed environment, so nothing from docker exec leaks in and every run sees the same one
    static char *const env[] = {"PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/work", "TMPDIR=/work",
                                "LANG=C.UTF-8", NULL};
    execve(o->argv[0], o->argv, env);
    child_fail(setup_fd, STEP_EXEC);
}

// Reads whatever is ready on the program's pipes, waiting at most wait_ms.
static void pump(struct capture *c, long wait_ms) {
    if (poll(c->fds, 2, (int)wait_ms) <= 0) return;
    char buf[65536];
    for (int i = 0; i < 2; i++) {
        struct pollfd *p = &c->fds[i];
        if (p->fd < 0 || !(p->revents & (POLLIN | POLLHUP | POLLERR))) continue;
        ssize_t n = read(p->fd, buf, sizeof buf);
        if (n < 0 && (errno == EAGAIN || errno == EINTR)) continue;
        if (n <= 0) {
            close(p->fd);
            p->fd = -1;
            continue;
        }
        if (i == 0) {
            long room = c->out_cap - c->out_bytes;
            long take = room <= 0 ? 0 : (n < room ? n : room);
            // If the worker stopped reading, keep counting so the limit still works
            if (c->stdout_open && take > 0 && !write_all(STDOUT_FILENO, buf, (size_t)take)) c->stdout_open = false;
            c->out_bytes += n;
            if (c->out_bytes > c->out_cap) c->output_exceeded = true;
        } else {
            size_t room = STDERR_CAP - c->err_len;
            size_t take = (size_t)n < room ? (size_t)n : room;
            memcpy(c->err + c->err_len, buf, take);
            c->err_len += take;
        }
    }
}

// Kills every process still running as the sandbox user. The helper switches to that uid
// first, so kill(-1) reaches exactly those processes; Linux spares the caller and PID 1.
static void kill_sandbox_user_processes(void) {
    pid_t helper = fork();
    if (helper == 0) {
        if (setgid(SANDBOX_GID) == 0 && setuid(SANDBOX_UID) == 0) kill(-1, SIGKILL);
        _exit(0);
    }
    if (helper > 0) waitpid(helper, NULL, 0);
}

int main(int argc, char **argv) {
    struct options o = parse_args(argc, argv);
    if (geteuid() != 0) {
        errno = EPERM;
        fail("runner must start as root");
    }
    signal(SIGPIPE, SIG_IGN);
    // Orphans of the program are handed to the runner instead of PID 1,
    // so the runner can kill and reap every one of them
    if (prctl(PR_SET_CHILD_SUBREAPER, 1) != 0) fail("prctl");

    int out_pipe[2], err_pipe[2], setup_pipe[2];
    if (pipe(out_pipe) != 0 || pipe(err_pipe) != 0 || pipe2(setup_pipe, O_CLOEXEC) != 0) fail("pipe");

    long start = now_ms();
    pid_t pid = fork();
    if (pid < 0) fail("fork");
    if (pid == 0) run_child(&o, out_pipe[1], err_pipe[1], setup_pipe[1]);
    close(out_pipe[1]);
    close(err_pipe[1]);
    close(setup_pipe[1]);

    // Returns 0 bytes once exec succeeds, because the pipe closes on exec
    int setup[2] = {0, 0};
    ssize_t setup_len = read(setup_pipe[0], setup, sizeof setup);
    close(setup_pipe[0]);

    static struct capture c;
    c.fds[0] = (struct pollfd){.fd = out_pipe[0], .events = POLLIN};
    c.fds[1] = (struct pollfd){.fd = err_pipe[0], .events = POLLIN};
    c.out_cap = o.out_kb * 1024;
    c.stdout_open = true;
    fcntl(out_pipe[0], F_SETFL, O_NONBLOCK);
    fcntl(err_pipe[0], F_SETFL, O_NONBLOCK);

    long deadline = start + o.wall_ms;
    int status = 0;
    bool exited = false, timed_out = false;
    while (!exited && !c.output_exceeded) {
        long left = deadline - now_ms();
        if (left <= 0) {
            timed_out = true;
            break;
        }
        pump(&c, left < 20 ? left : 20);
        exited = waitpid(pid, &status, WNOHANG) == pid;
    }
    long end = now_ms();

    if (exited) {
        // Collect output still in the pipes. Bounded, because a leftover process could keep writing.
        long stop = end + DRAIN_MS;
        while ((c.fds[0].fd >= 0 || c.fds[1].fd >= 0) && !c.output_exceeded && now_ms() < stop) {
            pump(&c, stop - now_ms());
        }
    } else {
        kill(-pid, SIGKILL);
        waitpid(pid, &status, 0);
    }

    kill_sandbox_user_processes();
    // Reap everything that was handed to the runner; zombies would count toward nproc
    while (wait(NULL) > 0) {}

    // Covers the program and every descendant the runner reaped
    struct rusage ru;
    getrusage(RUSAGE_CHILDREN, &ru);

    fprintf(stderr,
            "{\"exit_code\":%d,\"signal\":%d,\"cpu_ms\":%ld,\"wall_ms\":%ld,\"peak_kb\":%ld,"
            "\"timed_out\":%s,\"output_exceeded\":%s,\"stdout_bytes\":%ld,\"setup_error\":",
            WIFEXITED(status) ? WEXITSTATUS(status) : -1, WIFSIGNALED(status) ? WTERMSIG(status) : 0,
            timeval_ms(ru.ru_utime) + timeval_ms(ru.ru_stime), end - start, ru.ru_maxrss,
            timed_out ? "true" : "false", c.output_exceeded ? "true" : "false", c.out_bytes);
    if (setup_len == (ssize_t)sizeof setup && setup[0] >= 0 && setup[0] < STEP_COUNT) {
        fprintf(stderr, "\"%s: %s\"", STEP_NAMES[setup[0]], strerror(setup[1]));
    } else {
        fputs("null", stderr);
    }
    fputs(",\"stderr\":\"", stderr);
    json_escape(stderr, c.err, c.err_len);
    fputs("\"}\n", stderr);
    return 0;
}
