"""python -m admin <command>: the organiser's tools. Reads DATABASE_URL. See admin/commands.py."""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg

from admin import commands, report
from db.migrate import DEFAULT_DSN
from worker.sandbox import DockerError


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m admin", description="Run a contest on the judge.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import-problem", help="create or replace a problem from its folder")
    p.add_argument("folder", type=Path)

    p = sub.add_parser("check-problem", help="judge a solution against a problem folder, without importing it")
    p.add_argument("folder", type=Path)
    p.add_argument("solution", type=Path, help="a .cpp or .py file")
    p.add_argument("--expect", default="AC", type=str.upper,
                   help="the verdict this solution should get, like TLE for a slow one (default AC)")

    p = sub.add_parser("create-contest", help="create a contest and print its invite code")
    p.add_argument("--title", required=True)
    p.add_argument("--start", required=True, help="local time, like '2026-11-28 10:00'")
    p.add_argument("--minutes", type=int, required=True)
    p.add_argument("--problems", required=True, help="slugs in label order, comma separated")
    p.add_argument("--tz", default="Asia/Kolkata")
    p.add_argument("--invite-code", help="default: a random one")

    p = sub.add_parser("extend-contest", help="move a contest's end later, after an outage")
    p.add_argument("--contest", type=int, required=True)
    p.add_argument("--minutes", type=int, required=True)
    p.add_argument("--reopen", action="store_true", help="also open a contest that is already over")
    p.add_argument("--tz", default="Asia/Kolkata", help="the time zone the new end is printed in")

    p = sub.add_parser("create-client", help="issue an API key for an MCP client")
    p.add_argument("--name", required=True)
    p.add_argument("--max-inflight", type=int, default=5)

    p = sub.add_parser("rejudge", help="judge a contest problem's submissions again")
    p.add_argument("--contest", type=int, required=True)
    p.add_argument("--problem", required=True, help="the problem's slug")

    sub.add_parser("status", help="the queue, running jobs, recent verdicts and warnings, right now")

    p = sub.add_parser("contest-report", help="a contest's numbers as markdown, safe to publish")
    p.add_argument("--contest", type=int, required=True)
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> str:
    conn = await asyncpg.connect(os.environ.get("DATABASE_URL", DEFAULT_DSN))
    try:
        if args.command == "import-problem":
            problem_id, samples, hidden = await commands.import_problem(conn, args.folder)
            return (f"imported {args.folder.name} as problem {problem_id}: {samples} samples, {hidden} hidden tests\n"
                    "If it is in a contest that has started, run rejudge for it.")
        if args.command == "create-contest":
            slugs = [s.strip() for s in args.problems.split(",") if s.strip()]
            contest_id, code = await commands.create_contest(
                conn, args.title, args.start, args.minutes, slugs, args.tz, args.invite_code
            )
            return f"created contest {contest_id}\ninvite code: {code}"
        if args.command == "extend-contest":
            old, new = await commands.extend_contest(conn, args.contest, args.minutes, args.reopen)
            tz = ZoneInfo(args.tz)
            return (f"contest {args.contest} now ends at {new.astimezone(tz):%Y-%m-%d %H:%M} {args.tz}, "
                    f"{args.minutes} minutes after {old.astimezone(tz):%H:%M}\nAnnounce the new end to the contestants.")
        if args.command == "create-client":
            client_id, key = await commands.create_client(conn, args.name, args.max_inflight)
            return f"created client {client_id}\nAPI key (shown only now, store it safely): {key}"
        if args.command == "status":
            return (await report.status(conn))[0]
        if args.command == "contest-report":
            try:
                return await report.contest_report(conn, args.contest)
            except report.NoSuchContest as e:
                raise commands.AdminError(str(e)) from None
        new = await commands.rejudge(conn, args.contest, args.problem)
        return f"queued {len(new)} submissions for rejudging"
    finally:
        await conn.close()


async def check(args: argparse.Namespace) -> tuple[str, bool]:
    """Returns the report and whether the solution did what --expect said. A reference solution
    (AC expected) also has to leave half the time limit spare."""
    found = await commands.check_problem(args.folder, args.solution)
    passed = found.result.verdict == args.expect and not found.too_slow
    verdict = "as expected" if passed else f"NOT as expected (wanted {args.expect})"
    return f"{commands.describe(found, args.solution)}\n{verdict}", passed


def main(argv: list[str]) -> int:
    args = parse(argv)
    try:
        if args.command == "check-problem":
            report, passed = asyncio.run(check(args))
            print(report)
            return 0 if passed else 1
        print(asyncio.run(run(args)))
    except commands.AdminError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except DockerError as e:
        print(f"error: the sandbox failed, not the solution: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
