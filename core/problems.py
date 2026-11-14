"""Which problems a caller may see (ARCHITECTURE.md §9, Problem visibility).

A problem is visible if it is public, or if it belongs to a contest that has started, by the
database's clock. Everything else answers exactly as if the problem did not exist. REST and MCP
both read problems only through this module, so the rule lives in one place.
"""

import asyncpg

# Used as `FROM problems p WHERE ... AND {VISIBLE}`
VISIBLE = """(
    p.is_public OR EXISTS (
        SELECT 1 FROM contest_problems cp JOIN contests c ON c.id = cp.contest_id
        WHERE cp.problem_id = p.id AND c.starts_at <= now()
    )
)"""


async def list_visible(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(f"SELECT p.slug, p.title FROM problems p WHERE {VISIBLE} ORDER BY p.id")


async def get_visible(conn: asyncpg.Connection, slug: str) -> asyncpg.Record | None:
    """The problem with this slug, or None if it doesn't exist or isn't visible yet."""
    return await conn.fetchrow(
        f"""
        SELECT p.id, p.slug, p.title, p.statement_md, p.time_limit_ms, p.memory_limit_mb, p.checker,
               p.is_public
        FROM problems p WHERE p.slug = $1 AND {VISIBLE}
        """,
        slug,
    )


async def contest(conn: asyncpg.Connection, contest_id: int) -> asyncpg.Record | None:
    """A contest, with the database's time, so callers judge "started" and "ended" by one clock.
    The invite code is never selected: it is the only thing guarding registration."""
    return await conn.fetchrow(
        "SELECT id, title, starts_at, ends_at, now() AS db_now FROM contests WHERE id = $1", contest_id
    )


async def contests(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT id, title, starts_at, ends_at, now() AS db_now FROM contests ORDER BY starts_at DESC"
    )


async def contest_problems(conn: asyncpg.Connection, contest_id: int) -> list[asyncpg.Record]:
    """The contest's visible problems by label: none before the start."""
    return await conn.fetch(
        f"""
        SELECT cp.label, p.slug, p.title
        FROM contest_problems cp JOIN problems p ON p.id = cp.problem_id
        WHERE cp.contest_id = $1 AND {VISIBLE}
        ORDER BY cp.label
        """,
        contest_id,
    )


async def contest_problem(conn: asyncpg.Connection, contest_id: int, label: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"""
        SELECT p.id, p.slug, p.title, p.statement_md, p.time_limit_ms, p.memory_limit_mb, p.checker, cp.label
        FROM contest_problems cp JOIN problems p ON p.id = cp.problem_id
        WHERE cp.contest_id = $1 AND cp.label = $2 AND {VISIBLE}
        """,
        contest_id, label,
    )


async def samples(conn: asyncpg.Connection, problem_id: int) -> list[asyncpg.Record]:
    """Sample tests only. Hidden tests are never read by the API."""
    return await conn.fetch(
        "SELECT input_text, expected_text FROM testcases WHERE problem_id = $1 AND is_sample ORDER BY ordinal",
        problem_id,
    )
