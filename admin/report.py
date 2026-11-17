"""What the organiser reads on contest day and after it (DECISIONS.md, 141).

status() is the live picture: the contest, the queue, each running job's lease, the last 10 minutes
of verdicts, and warnings for what needs a person. It replaces the planned /metrics endpoint: one
organiser watches one laptop, and nothing would read the metrics.

contest_report() is the contest's numbers, for the README. They are counts and percentiles only:
never an email, a display name, code or a test. Times come from the jobs table. A job retried
after a crash keeps only its last start (core/queue.py sets started_at at every claim), so its
queue wait is counted from that start.
"""

from datetime import timedelta

import asyncpg

from core.jobs import Caps

RECENT = timedelta(minutes=10)
WAITING_TOO_LONG_S = 60
LEASE_GRACE_S = 10  # the reaper runs every few seconds; a lease this far past its end is stuck


class NoSuchContest(Exception):
    pass


def _seconds(value) -> str:
    return "-" if value is None else f"{value:.1f} s"


async def status(conn: asyncpg.Connection) -> tuple[str, list[str]]:
    """Returns (the report, warnings). Reads only; safe to run as often as you like."""
    lines, warnings = [], []
    now = await conn.fetchval("SELECT now()")
    lines.append(f"Judge status at {now:%Y-%m-%d %H:%M:%S %Z}")

    contests = await conn.fetch(
        """
        SELECT c.id, c.title, c.starts_at, c.ends_at,
               extract(epoch FROM c.starts_at - now()) AS starts_in,
               extract(epoch FROM c.ends_at - now()) AS ends_in,
               (SELECT count(*) FROM jobs j WHERE j.contest_id = c.id AND j.rejudge_of IS NULL) AS submissions
        FROM contests c
        WHERE c.ends_at > now() AND c.starts_at < now() + interval '1 day'
        ORDER BY c.starts_at
        """
    )
    for c in contests:
        if c["starts_in"] > 0:
            lines.append(f"Contest {c['id']} \"{c['title']}\" starts in {c['starts_in'] / 60:.0f} min")
        else:
            lines.append(f"Contest {c['id']} \"{c['title']}\" is running, {c['ends_in'] / 60:.0f} min left, "
                         f"{c['submissions']} submissions")
    if not contests:
        lines.append("No contest running or starting within a day")

    queue = await conn.fetchrow(
        """
        SELECT count(*) FILTER (WHERE status = 'queued' AND rejudge_of IS NULL) AS queued,
               count(*) FILTER (WHERE status = 'queued' AND rejudge_of IS NOT NULL) AS rejudges,
               count(*) FILTER (WHERE status = 'running') AS running,
               extract(epoch FROM now() - min(submitted_at) FILTER (WHERE status = 'queued')) AS oldest
        FROM jobs WHERE status IN ('queued', 'running')
        """
    )
    lines.append(f"Queue: {queue['queued']} waiting (+{queue['rejudges']} rejudges), {queue['running']} running, "
                 f"oldest waiting {_seconds(queue['oldest'])}")
    if queue["oldest"] is not None and queue["oldest"] > WAITING_TOO_LONG_S:
        warnings.append(f"a job has waited {queue['oldest']:.0f} s: are the workers running?")
    if queue["queued"] >= 0.8 * Caps().global_queued:
        warnings.append(f"{queue['queued']} jobs waiting, near the cap of {Caps().global_queued}: new ones get 429")

    running = await conn.fetch(
        """
        SELECT worker_id, kind, attempts, extract(epoch FROM now() - started_at) AS for_s,
               extract(epoch FROM lease_until - now()) AS lease_left
        FROM jobs WHERE status = 'running' ORDER BY started_at
        """
    )
    for r in running:
        lines.append(f"  {r['worker_id']}: a {r['kind']} for {r['for_s']:.1f} s, attempt {r['attempts']}, "
                     f"lease {r['lease_left']:+.1f} s")
        if r["lease_left"] < -LEASE_GRACE_S:
            warnings.append(f"{r['worker_id']}'s lease ran out {-r['lease_left']:.0f} s ago and the job wasn't "
                            "requeued: is any worker running its reaper?")

    recent = await conn.fetchrow(
        """
        SELECT count(*) AS finished,
               count(*) FILTER (WHERE j.status = 'dead') AS dead,
               count(*) FILTER (WHERE r.verdict = 'IE') AS ie,
               count(*) FILTER (WHERE j.attempts > 1) AS retried,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM j.finished_at - j.submitted_at))
                   FILTER (WHERE j.status = 'completed') AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM j.finished_at - j.submitted_at))
                   FILTER (WHERE j.status = 'completed') AS p95
        FROM jobs j LEFT JOIN results r ON r.job_id = j.id
        WHERE j.finished_at > now() - $1::interval
        """,
        RECENT,
    )
    verdicts = await conn.fetch(
        """SELECT r.verdict, count(*) FROM jobs j JOIN results r ON r.job_id = j.id
           WHERE j.finished_at > now() - $1::interval GROUP BY r.verdict ORDER BY count(*) DESC""",
        RECENT,
    )
    counts = ", ".join(f"{v['verdict']} {v['count']}" for v in verdicts) or "none"
    lines.append(f"Last 10 min: {recent['finished']} finished ({counts}), submission to verdict "
                 f"p50 {_seconds(recent['p50'])}, p95 {_seconds(recent['p95'])}, "
                 f"{recent['retried']} needed a retry")
    if recent["ie"] or recent["dead"]:
        warnings.append(f"{recent['ie']} IE verdicts and {recent['dead']} dead jobs in the last 10 min: "
                        "read the workers' logs (journalctl -u 'judge-worker@*')")

    lines += [f"WARNING: {w}" for w in warnings] or ["No warnings"]
    return "\n".join(lines), warnings


async def contest_report(conn: asyncpg.Connection, contest_id: int) -> str:
    """The contest's numbers as markdown, safe to publish."""
    contest = await conn.fetchrow(
        "SELECT title, starts_at, ends_at, now() >= ends_at AS over FROM contests WHERE id = $1", contest_id
    )
    if contest is None:
        raise NoSuchContest(f"no contest {contest_id}")
    ours = "j.contest_id = $1 AND j.kind = 'submission' AND j.rejudge_of IS NULL"

    totals = await conn.fetchrow(
        f"""
        SELECT count(*) AS submissions, count(DISTINCT j.owner_id) AS contestants,
               count(*) FILTER (WHERE j.language = 'cpp') AS cpp,
               count(*) FILTER (WHERE j.language = 'python') AS python,
               count(*) FILTER (WHERE j.status IN ('queued', 'running')) AS unfinished,
               count(*) FILTER (WHERE j.status = 'cancelled') AS cancelled,
               count(*) FILTER (WHERE j.status = 'dead') AS dead,
               count(*) FILTER (WHERE j.attempts > 1) AS retried,
               max(j.attempts) AS most_attempts
        FROM jobs j WHERE {ours}
        """,
        contest_id,
    )
    verdicts = await conn.fetch(
        f"""SELECT r.verdict, count(*) FROM jobs j JOIN results r ON r.job_id = j.id
            WHERE {ours} GROUP BY r.verdict ORDER BY count(*) DESC, r.verdict""",
        contest_id,
    )
    times = await conn.fetchrow(
        f"""
        SELECT {", ".join(
            f"percentile_cont({p}) WITHIN GROUP (ORDER BY extract(epoch FROM j.{end} - j.submitted_at)) AS {name}_{label}"
            for name, end in (("verdict", "finished_at"), ("wait", "started_at"))
            for p, label in ((0.5, "p50"), (0.95, "p95"), (0.99, "p99"))
        )},
               max(extract(epoch FROM j.finished_at - j.submitted_at)) AS verdict_max,
               max(extract(epoch FROM j.started_at - j.submitted_at)) AS wait_max
        FROM jobs j WHERE {ours} AND j.status = 'completed'
        """,
        contest_id,
    )
    busiest = await conn.fetchrow(
        f"""SELECT date_trunc('minute', j.submitted_at) AS minute, count(*) FROM jobs j WHERE {ours}
            GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1""",
        contest_id,
    )
    # How many of the contest's jobs were waiting at the moment each one was submitted
    deepest = await conn.fetchval(
        f"""
        SELECT max((SELECT count(*) FROM jobs o
                    WHERE o.contest_id = $1 AND o.kind = 'submission' AND o.rejudge_of IS NULL
                      AND o.submitted_at <= j.submitted_at
                      -- until a worker starts it, or it is cancelled before that
                      AND coalesce(o.started_at, o.finished_at, 'infinity') > j.submitted_at))
        FROM jobs j WHERE {ours}
        """,
        contest_id,
    )
    rejudges = await conn.fetchval(
        "SELECT count(*) FROM jobs WHERE contest_id = $1 AND rejudge_of IS NOT NULL", contest_id
    )

    verdict_counts = ", ".join(f"{v['verdict']} {v['count']}" for v in verdicts) or "none"

    def ms(value) -> str:
        return "-" if value is None else f"{value * 1000:.0f}"

    minutes = (contest["ends_at"] - contest["starts_at"]).total_seconds() / 60
    lines = [
        f"# Contest {contest_id}: {contest['title']}",
        "",
        f"{contest['starts_at']:%Y-%m-%d %H:%M} to {contest['ends_at']:%H:%M} UTC, {minutes:.0f} minutes."
        + ("" if contest["over"] else " **Still running: these numbers are not final.**"),
        "",
        f"- {totals['submissions']} submissions from {totals['contestants']} contestants: "
        f"{totals['cpp']} C++, {totals['python']} Python",
        f"- Verdicts: {verdict_counts}",
        f"- Busiest minute: {busiest['count'] if busiest else 0} submissions"
        + (f" ({busiest['minute']:%H:%M} UTC)" if busiest else ""),
        f"- Most of the contest's submissions waiting at once: {deepest or 0}",
        "",
        "| Time (ms) | p50 | p95 | p99 | max |",
        "|---|---|---|---|---|",
        f"| Submission to verdict | {ms(times['verdict_p50'])} | {ms(times['verdict_p95'])} | "
        f"{ms(times['verdict_p99'])} | {ms(times['verdict_max'])} |",
        f"| Waiting for a worker | {ms(times['wait_p50'])} | {ms(times['wait_p95'])} | "
        f"{ms(times['wait_p99'])} | {ms(times['wait_max'])} |",
        "",
        "**Reliability**",
        "",
        f"- Recovered after a worker crash (more than one attempt): {totals['retried']}"
        + (f", at most {totals['most_attempts']} attempts" if totals["retried"] else ""),
        f"- Dead (3 failed attempts, judged IE): {totals['dead']}",
        f"- Cancelled: {totals['cancelled']}",
        f"- Not finished when this was written: {totals['unfinished']}",
        f"- Rejudged afterwards: {rejudges}",
        "",
        "Times run from submission to the result being stored. A job retried after a crash counts its "
        "wait from its last start.",
    ]
    return "\n".join(lines)
