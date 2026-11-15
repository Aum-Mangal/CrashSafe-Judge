"""The MCP server at /mcp, for AI agents (ARCHITECTURE.md §9, MCP).

Streamable HTTP, stateless: every request stands alone, so no session lives in memory and a
restart loses nothing. Built on the official SDK (mcp 2.3.0, DECISIONS.md 117), which answers
both the 2025-11-25 protocol and the per-request 2026-07-28 one.

Every request carries an API key from `python -m admin create-client`, as
`Authorization: Bearer jk_...`, checked here before the SDK sees anything (RequireApiKey). The
key's client owns the jobs it creates. No client can see or cancel another's: they answer
exactly like missing ones.

A tool that starts a job waits up to 20 s for the verdict, which covers most runs. Otherwise it
returns the job id with status queued or running, and the agent calls get_result. This works
with every client; the Tasks extension comes on top of it.
"""

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse
from starlette.routing import Route

from api.pages import VERDICTS
from core import agent, problems, submissions
from core.jobs import IdempotencyConflict, Rejected

INSTRUCTIONS = """\
Runs and judges C++17 and Python 3 code in a sandbox with no network.
run_code runs a program on your stdin. verify_code checks it against tests you give.
submit_solution judges it against a practice problem's tests, which stay hidden.
Each waits up to 20 seconds; if the job isn't finished by then you get its job_id, and
get_result(job_id) returns it later. Results are kept for 24 hours.
To retry a call safely, pass the same idempotency_key: you get the same job, never a second one."""

Language = Annotated[Literal["cpp", "python"], Field(description="cpp is C++17 (g++), python is Python 3")]
Source = Annotated[str, Field(min_length=1, max_length=65536, description="the whole program, up to 64 KB")]
Key = Annotated[str | None, Field(max_length=100, description="optional; reuse it to retry this call safely")]
JobId = Annotated[str, Field(max_length=64, description="the job_id an earlier call returned")]
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
STARTS_JOB = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)


class Test(BaseModel):
    input: str
    expected: str = Field(description="compared token by token: spacing and line ends don't matter")


class JobReply(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "cancelled", "failed"]
    kind: str
    verdict: str | None = Field(None, description="AC, WA, TLE, MLE, OLE, RE, CE, or OK for run_code")
    meaning: str | None = None
    failed_test: int | None = Field(None, description="1-based number of the first failing test")
    tests: list[dict] | None = Field(None, description="per test: verdict, cpu_ms, wall_ms, peak_kb")
    peak_memory_kb: int | None = None
    stdout: str | None = Field(None, description="run_code's output, or the failing test's; never a hidden test's")
    stderr: str | None = None
    compile_log: str | None = None
    note: str | None = None


class ProblemSummary(BaseModel):
    slug: str
    title: str


class Problem(BaseModel):
    slug: str
    title: str
    statement_md: str
    time_limit_ms: int
    memory_limit_mb: int
    checker: str
    samples: list[Test]


# --- Who is calling


class RequireApiKey:
    """An ASGI app in front of the SDK. A request without a known key gets 401 and never reaches
    MCP. A known key's client id goes into the request's scope for the tools to read.

    The key is looked up by its sha256 (admin/commands.py explains why a fast hash is enough).
    Starlette treats a plain function as a request handler, so this is a class."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        client_id = None
        auth = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        scheme, _, key = auth.partition(" ")
        if scheme.lower() == "bearer" and key.startswith("jk_") and len(key) <= 100:
            client_id = await scope["app"].state.pool.fetchval(
                "SELECT id FROM api_clients WHERE key_hash = $1", hashlib.sha256(key.encode()).hexdigest()
            )
        if client_id is None:
            reply = JSONResponse({"error": "missing or unknown API key"}, 401, headers={"WWW-Authenticate": "Bearer"})
            await reply(scope, receive, send)
            return
        scope.setdefault("state", {})["mcp_client"] = client_id
        await self.inner(scope, receive, send)


def _caller(ctx: Context) -> tuple[int, object]:
    """(client id, the FastAPI app). The SDK hands every tool the HTTP request it came in on."""
    request = ctx.request_context.request
    return request.scope["state"]["mcp_client"], request.app


def _job_id(text: str) -> UUID:
    try:
        return UUID(text)
    except ValueError:
        raise ToolError("no such job") from None


# --- Replies

_STATUS = {"dead": "failed"}  # the judge gave up after its retries: say so plainly


def _reply(found: dict) -> JobReply:
    status = _STATUS.get(found["status"], found["status"])
    note = None
    if status in ("queued", "running"):
        note = "Still judging. Call get_result with this job_id; results are kept for 24 hours."
    elif status == "failed":
        note = "The judge failed to run this job after several tries. That is not your code's fault: try again."
    return JobReply(
        job_id=str(found["id"]), status=status, kind=found["kind"], verdict=found["verdict"],
        meaning=VERDICTS.get(found["verdict"]), failed_test=found["failed_test"], tests=found["tests"],
        peak_memory_kb=found["peak_memory_kb"], stdout=found["stdout_trunc"], stderr=found["stderr_trunc"],
        compile_log=found["compile_log"], note=note,
    )


async def _start(ctx: Context, create) -> JobReply:
    """Creates the job with create(conn, client_id), waits for it, and replies."""
    client_id, app = _caller(ctx)
    try:
        async with app.state.pool.acquire() as conn:
            job_id, _ = await create(conn, client_id)
    except (agent.Invalid, submissions.NotAllowed) as e:
        raise ToolError(str(e)) from None
    except submissions.NotFound:
        raise ToolError("no such problem") from None
    except IdempotencyConflict:
        raise ToolError("this idempotency_key was already used for a different request") from None
    except Rejected as e:
        raise ToolError(f"refused for now ({e.reason}): try again in {e.retry_after} seconds") from None

    async def finished() -> bool:
        return await agent.status(app.state.pool, job_id) in ("completed", "cancelled", "dead")

    await app.state.job_waiter.wait(job_id, app.state.mcp_wait_seconds, finished)
    return _reply(await agent.get(app.state.pool, client_id, job_id))


# --- The server


def build() -> tuple[MCPServer, Route]:
    """A fresh server per app: the SDK's session manager can only be started once."""
    server = MCPServer("judge", instructions=INSTRUCTIONS, version="0.1")

    @server.tool(annotations=STARTS_JOB, structured_output=True)
    async def run_code(language: Language, source: Source, ctx: Context,
                       stdin: Annotated[str, Field(description="up to 1 MiB")] = "",
                       idempotency_key: Key = None) -> JobReply:
        """Run a program once on the given stdin, with 10 s of CPU and 256 MB. Returns its stdout
        and stderr (up to 64 KB each) and exit verdict: OK, RE, TLE, MLE or OLE, or CE."""
        return await _start(ctx, lambda conn, client: agent.run(conn, client, language, source, stdin, idempotency_key))

    @server.tool(annotations=STARTS_JOB, structured_output=True)
    async def verify_code(language: Language, source: Source,
                          tests: Annotated[list[Test], Field(min_length=1, max_length=agent.MAX_TESTS)],
                          ctx: Context, idempotency_key: Key = None) -> JobReply:
        """Check a program against your own tests: up to 20, 2 s of CPU each and 30 s in all. Every
        test runs, and each gets a verdict. The first failing test's output is returned."""
        cases = [t.model_dump() for t in tests]
        return await _start(ctx, lambda conn, client: agent.verify(conn, client, language, source, cases, idempotency_key))

    @server.tool(annotations=STARTS_JOB, structured_output=True)
    async def submit_solution(problem_slug: Annotated[str, Field(max_length=100)], language: Language,
                              source: Source, ctx: Context, idempotency_key: Key = None) -> JobReply:
        """Judge a solution to a public practice problem against all its tests, stopping at the
        first failure. Hidden tests show only their verdict, time and memory, never output."""
        return await _start(ctx, lambda conn, client: submissions.submit(
            conn, client, problem_slug, language, source, None, idempotency_key, owner_type="client"))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    async def list_problems(ctx: Context) -> list[ProblemSummary]:
        """The problems you can read now."""
        _, app = _caller(ctx)
        async with app.state.pool.acquire() as conn:
            return [ProblemSummary(**dict(r)) for r in await problems.list_visible(conn)]

    @server.tool(annotations=READ_ONLY, structured_output=True)
    async def get_problem(slug: Annotated[str, Field(max_length=100)], ctx: Context) -> Problem:
        """A problem's statement, limits and sample tests."""
        _, app = _caller(ctx)
        async with app.state.pool.acquire() as conn:
            problem = await problems.get_visible(conn, slug)
            if problem is None:
                raise ToolError("no such problem")  # also before its contest starts
            samples = await problems.samples(conn, problem["id"])
        return Problem(
            slug=problem["slug"], title=problem["title"], statement_md=problem["statement_md"],
            time_limit_ms=problem["time_limit_ms"], memory_limit_mb=problem["memory_limit_mb"],
            checker=problem["checker"],
            samples=[Test(input=s["input_text"], expected=s["expected_text"]) for s in samples],
        )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    async def get_result(job_id: JobId, ctx: Context) -> JobReply:
        """The current state of a job you started, with its result once it has one."""
        client_id, app = _caller(ctx)
        try:
            return _reply(await agent.get(app.state.pool, client_id, _job_id(job_id)))
        except agent.NotFound:
            raise ToolError("no such job") from None

    @server.tool(annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True, openWorldHint=False),
                 structured_output=True)
    async def cancel_run(job_id: JobId, ctx: Context) -> JobReply:
        """Stop a job you started. A queued job is cancelled at once, a running one a moment
        later; a finished one is left as it is."""
        client_id, app = _caller(ctx)
        try:
            async with app.state.pool.acquire() as conn:
                await agent.cancel(conn, client_id, _job_id(job_id))
            return _reply(await agent.get(app.state.pool, client_id, _job_id(job_id)))
        except agent.NotFound:
            raise ToolError("no such job") from None

    sdk_app = server.streamable_http_app(
        stateless_http=True,
        # One plain JSON reply per request. The longest wait is 20 s, well inside proxy timeouts.
        json_response=True,
        # The SDK's DNS-rebinding guard protects servers that trust anyone who reaches localhost.
        # Behind the tunnel the Host is the public name, and every request here needs a key that
        # a rebinding web page can't have (DECISIONS.md, 118).
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return server, Route("/mcp", endpoint=RequireApiKey(sdk_app.routes[0].endpoint), methods=["POST"])
