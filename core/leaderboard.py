"""ICPC ranking for a contest (ARCHITECTURE.md §4, Leaderboard).

Rules:
- A problem is solved by its first Accepted submission. Its time is the whole minutes from the
  contest's start to that submission.
- Each rejected attempt before it adds 20 minutes: WA, TLE, MLE, OLE and RE. A compile error
  never ran, and an internal error is the judge's fault, so neither costs anything.
- Penalty is the sum over solved problems of time plus those 20-minute charges. Unsolved
  problems cost nothing, however many attempts they had.
- More solved ranks higher; equal solved, lower penalty ranks higher; a full tie shares the
  rank, and the next rank skips (1, 2, 2, 4).
- Superseded submissions (replaced by a rejudge) and cancelled ones don't count. Submissions not
  yet judged show as pending; they can't be counted until they have a verdict.
"""

import json
from dataclasses import dataclass, field

import asyncpg

PENALTY_MINUTES = 20
REJECTED = ("WA", "TLE", "MLE", "OLE", "RE")

_QUERY = f"""
WITH subs AS (
    SELECT j.owner_id AS user_id, cp.label, j.submitted_at, j.status, r.verdict,
           floor(extract(epoch FROM j.submitted_at - c.starts_at) / 60)::int AS minute,
           -- A window function: every row gets this user's first AC on this problem,
           -- without collapsing the rows, so each attempt can be compared with it below
           min(j.submitted_at) FILTER (WHERE r.verdict = 'AC')
               OVER (PARTITION BY j.owner_id, j.problem_id) AS first_ac
    FROM jobs j
    JOIN contests c ON c.id = j.contest_id
    JOIN contest_problems cp ON cp.contest_id = j.contest_id AND cp.problem_id = j.problem_id
    LEFT JOIN results r ON r.job_id = j.id
    WHERE j.contest_id = $1 AND j.kind = 'submission' AND j.owner_type = 'user'
      AND NOT j.superseded AND j.status <> 'cancelled'
),
cells AS (
    -- One row per user and problem. Only attempts before the first AC count.
    SELECT user_id, label,
           first_ac IS NOT NULL AS solved,
           min(minute) FILTER (WHERE submitted_at = first_ac) AS minute,
           count(*) FILTER (WHERE verdict IN {REJECTED!r} AND (first_ac IS NULL OR submitted_at < first_ac)) AS rejected,
           count(*) FILTER (WHERE status IN ('queued', 'running') AND (first_ac IS NULL OR submitted_at < first_ac)) AS pending
    FROM subs
    GROUP BY user_id, label, first_ac
),
totals AS (
    SELECT user_id,
           count(*) FILTER (WHERE solved) AS solved,
           coalesce(sum(minute + {PENALTY_MINUTES} * rejected) FILTER (WHERE solved), 0) AS penalty,
           jsonb_object_agg(label, jsonb_build_object(
               'solved', solved, 'minute', minute, 'rejected', rejected, 'pending', pending)) AS cells
    FROM cells
    GROUP BY user_id
)
SELECT RANK() OVER (ORDER BY t.solved DESC, t.penalty) AS rank,
       u.display_name AS name, t.solved, t.penalty, t.cells::text AS cells
FROM totals t JOIN users u ON u.id = t.user_id
ORDER BY rank, u.display_name
"""


@dataclass
class Board:
    started: bool
    labels: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)


async def board(conn: asyncpg.Connection, contest_id: int) -> Board | None:
    """None if there's no such contest. Before the start the board is empty and lists no problems."""
    started = await conn.fetchval("SELECT starts_at <= now() FROM contests WHERE id = $1", contest_id)
    if started is None:
        return None
    if not started:
        return Board(started=False)
    labels = [r["label"] for r in await conn.fetch(
        "SELECT label FROM contest_problems WHERE contest_id = $1 ORDER BY label", contest_id
    )]
    rows = [
        {"rank": r["rank"], "name": r["name"], "solved": r["solved"], "penalty": r["penalty"],
         "cells": json.loads(r["cells"])}
        for r in await conn.fetch(_QUERY, contest_id)
    ]
    return Board(started=True, labels=labels, rows=rows)
