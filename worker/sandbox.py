"""Runs programs inside gVisor sandboxes through the Docker CLI (ARCHITECTURE.md §7).

Every Docker call is an async subprocess with a timeout, because a wedged sandbox can make
Docker itself hang (DECISIONS.md, probe 1). Anything that goes wrong on Docker's side raises
DockerError, which the worker treats as an infrastructure failure, never as the submission's fault.
"""

import asyncio
import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

IMAGES = {"cpp": "judge-cpp", "python": "judge-python"}
RUNNER = "/usr/local/bin/judge-runner"
SANDBOX_UID = "65534"
# Container memory above the problem's limit. gVisor itself used about 13 MB (DECISIONS.md, probe 7).
CGROUP_HEADROOM_MB = 64
# Address space allowed above the problem's limit, so a program can pass its limit and be judged MLE
# before the cgroup kills the whole sandbox. Kept below CGROUP_HEADROOM_MB on purpose.
ADDRESS_SPACE_HEADROOM_MB = 48


class DockerError(Exception):
    """Docker failed or hung. The job goes back to the queue; the submission isn't blamed."""


@dataclass(frozen=True)
class Limits:
    cpu_ms: int
    memory_mb: int
    wall_ms: int
    output_kb: int = 16384

    @classmethod
    def for_time(cls, cpu_ms: int, memory_mb: int, output_kb: int = 16384) -> "Limits":
        """Limits whose wall-clock limit is three times the CPU limit, as for every test."""
        return cls(cpu_ms=cpu_ms, memory_mb=memory_mb, wall_ms=3 * cpu_ms, output_kb=output_kb)


@dataclass(frozen=True)
class Report:
    """What judge-runner saw, plus whether the whole sandbox died. Facts only; verdict.py judges."""

    exit_code: int = 0
    signal: int = 0
    cpu_ms: int = 0
    wall_ms: int = 0
    peak_kb: int = 0
    timed_out: bool = False
    output_exceeded: bool = False
    stdout: bytes = b""
    stderr: str = ""
    sandbox_died: bool = False
    oom_killed: bool = False


async def docker(*args: str, stdin: bytes | None = None, timeout: float) -> tuple[int, bytes, bytes]:
    """Runs one docker CLI command and returns (exit code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        await _stop(proc)
        raise DockerError(f"docker {args[0]} timed out after {timeout:.0f} s") from None
    except asyncio.CancelledError:
        await _stop(proc)
        raise
    return proc.returncode, out, err


async def _stop(proc: asyncio.subprocess.Process) -> None:
    # Only stops the docker client. The program inside keeps running until its container is removed.
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    await proc.wait()


async def kill_runsc_processes(container_id: str) -> None:
    """Last resort for a sandbox Docker can't remove: kills its gVisor processes directly.

    Only works while the worker runs on the host. A worker in a container would need --pid=host.
    """
    proc = await asyncio.create_subprocess_exec("pkill", "-9", "-f", f"runsc.*{container_id}")
    await proc.wait()


class Sandbox:
    """One gVisor container, labelled with its instance, job and fencing token so reconciliation
    can find orphans. The instance names the database the job lives in, so workers for different
    databases on one Docker host never touch each other's sandboxes."""

    def __init__(self, container_id: str):
        self.id = container_id

    @classmethod
    async def create(cls, language: str, memory_mb: int, job_id, token: int, instance: str) -> "Sandbox":
        memory = f"{memory_mb + CGROUP_HEADROOM_MB}m"
        code, out, err = await docker(
            "run", "-d", "--runtime=runsc", "--network", "none", "--read-only",
            "--tmpfs", f"/work:rw,exec,size=64m,uid={SANDBOX_UID},gid={SANDBOX_UID},mode=0700",
            # /work must be the only writable place, because it's the only one emptied between tests.
            # Docker adds a writable /dev/shm and gVisor a writable /tmp unless told otherwise.
            "--ipc", "none", "--tmpfs", "/tmp:ro,size=1m",
            "--memory", memory, "--memory-swap", memory,
            "--pids-limit", "512", "--ulimit", "nproc=64:64", "--cpus", "1",
            "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "KILL",
            # Only the root runner uses SYS_ADMIN, to give each program its own IPC namespace.
            # The program loses every capability when the runner drops it to the sandbox user.
            "--cap-add", "SYS_ADMIN",
            "--security-opt", "no-new-privileges", "--user", SANDBOX_UID,
            "--label", f"judge.instance={instance}",
            "--label", f"judge.job={job_id}", "--label", f"judge.token={token}",
            IMAGES[language], "sleep", "infinity",
            timeout=30,
        )
        if code != 0:
            raise DockerError(f"docker run failed: {err.decode(errors='replace').strip()[-300:]}")
        return cls(out.decode().strip())

    async def write_file(self, path: str, data: bytes, executable: bool = False) -> None:
        mode = "755" if executable else "644"
        code, _, err = await docker(
            "exec", "-i", "--user", SANDBOX_UID, self.id,
            "sh", "-c", 'cat > "$1" && chmod "$2" "$1"', "sh", path, mode,
            stdin=data, timeout=30,
        )
        if code != 0:
            raise DockerError(f"writing {path} failed: {err.decode(errors='replace').strip()[-300:]}")

    async def read_file(self, path: str) -> bytes:
        code, out, err = await docker("exec", "--user", SANDBOX_UID, self.id, "cat", path, timeout=30)
        if code != 0:
            raise DockerError(f"reading {path} failed: {err.decode(errors='replace').strip()[-300:]}")
        return out

    async def run(
        self, argv: list[str], stdin: bytes, limits: Limits, address_space: bool = True, keep: list[str] = ()
    ) -> Report:
        """Runs one program through judge-runner and returns what happened.

        address_space=False leaves RLIMIT_AS off, for the compiler, which reserves far more
        address space than it uses. keep, when given, makes the runner empty /work first,
        except those names, so earlier runs leave nothing behind.
        """
        memory = limits.memory_mb + ADDRESS_SPACE_HEADROOM_MB if address_space else 0
        keep_args = [arg for name in keep for arg in ("--keep", name)]
        code, out, err = await docker(
            "exec", "-i", "--user", "0", self.id, RUNNER,
            "--cpu-ms", str(limits.cpu_ms), "--wall-ms", str(limits.wall_ms),
            "--mem-mb", str(memory), "--out-kb", str(limits.output_kb), *keep_args,
            "--", *argv,
            stdin=stdin, timeout=limits.wall_ms / 1000 + 20,
        )
        data = _parse_report(err)
        if data is not None:
            if data["setup_error"]:
                # The worker gave a bad path or the image is broken: our problem, not the submission's
                raise DockerError(f"program could not start: {data['setup_error']}")
            return Report(
                exit_code=data["exit_code"],
                signal=data["signal"],
                cpu_ms=data["cpu_ms"],
                wall_ms=data["wall_ms"],
                peak_kb=data["peak_kb"],
                timed_out=data["timed_out"],
                output_exceeded=data["output_exceeded"],
                stdout=out,
                stderr=data["stderr"],
            )
        # No report: either the whole sandbox died under the program, or Docker failed
        died, oom = await self._death()
        if died:
            return Report(stdout=out, sandbox_died=True, oom_killed=oom)
        raise DockerError(f"no report from judge-runner (docker exit {code}): {err.decode(errors='replace')[-300:]}")

    async def _death(self) -> tuple[bool, bool]:
        """Returns (died, OOM-killed). Docker can report a dead sandbox as running for about
        0.3 s, so this polls for up to 3 s before deciding.

        A sandbox the program killed is stopped but still exists. If Docker can't find it at all,
        someone removed it, and that is never the submission's doing."""
        for _ in range(12):
            code, out, err = await docker("inspect", "-f", "{{.State.Running}} {{.State.OOMKilled}}", self.id, timeout=10)
            if code != 0:
                raise DockerError(f"sandbox is gone: {err.decode(errors='replace').strip()[-300:]}")
            running, oom = out.decode().split()
            if running == "false":
                return True, oom == "true"
            await asyncio.sleep(0.25)
        return False, False

    async def remove(self) -> None:
        """Removes the container. If Docker hangs on a wedged sandbox, kills its runsc processes."""
        try:
            await docker("rm", "-f", self.id, timeout=20)
        except DockerError:
            log.warning("docker rm hung for %s; killing its runsc processes", self.id[:12])
            await kill_runsc_processes(self.id)


def _parse_report(stderr: bytes) -> dict | None:
    """The runner's report is the last line of its stderr. Returns None if there isn't one."""
    lines = stderr.decode(errors="replace").strip().splitlines()
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "runner_error" in data:
        raise DockerError(f"judge-runner failed: {data['runner_error']}")
    return data
