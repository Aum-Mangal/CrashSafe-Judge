"""The Tasks extension, io.modelcontextprotocol/tasks, on top of api/mcp.py (ARCHITECTURE.md §9).

Without it, a tool that starts a job holds the request open for up to 20 s, then hands back a
job id for get_result. With it, the tool answers at once with a task: a durable handle the client
polls with tasks/get, from any connection, even after a restart. The task id is the job id, and
the jobs table is the only state: nothing about a task lives in this process.

Rules from the extension's specification (2026-07-28 protocol only):
- A task is returned only to a request whose _meta declared the extension, never otherwise.
- The job is committed before the task is returned, so tasks/get for its id resolves at once.
- tasks/get puts the tool's result, or a JSON-RPC error, at the top level of its reply.
- A request to tasks/* that didn't declare the extension gets -32021. An unknown, expired or
  someone else's task gets -32602, all with the same message.
- A wrong answer is a successful judging: WA is completed, never failed. failed is only for a
  job the judge itself gave up on.
"""

import json
from typing import Any
from uuid import UUID

from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp_types import RequestParams
from mcp_types.jsonrpc import INTERNAL_ERROR, INVALID_PARAMS, MISSING_REQUIRED_CLIENT_CAPABILITY
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import Field

from core import agent, queue

EXTENSION = "io.modelcontextprotocol/tasks"
JOB_TOOLS = {"run_code", "verify_code", "submit_solution"}  # the read-only tools answer at once anyway
POLL_MS = 1000
AS_TASK = "mcp_as_task"  # set in the request's scope: tells api/mcp.py not to wait for the job
FAILED = "The judge failed to run this job after several tries. That is not your code's fault: try again."
NOT_FOUND = "no such task"


class TaskParams(RequestParams):
    task_id: str = Field(alias="taskId", max_length=64)


class UpdateParams(TaskParams):
    input_responses: dict[str, Any] | None = Field(default=None, alias="inputResponses")


def declared(ctx) -> bool:
    """Did this request, not some earlier one, declare the extension? Only the 2026-07-28
    protocol carries per-request capabilities."""
    if ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS:
        return False
    meta = (ctx.params or {}).get("_meta") or {}
    capabilities = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
    return EXTENSION in (capabilities.get("extensions") or {})


def _caller(ctx) -> tuple[int, Any]:
    return ctx.request.scope["state"]["mcp_client"], ctx.request.app


def _iso(moment) -> str:
    return moment.isoformat().replace("+00:00", "Z")


_STATUS = {"queued": "working", "running": "working", "completed": "completed", "cancelled": "cancelled", "dead": "failed"}
_MESSAGE = {"queued": "Waiting for a worker.", "running": "Being judged."}


async def _task(pool, client_id: int, job_id: UUID, with_payload: bool) -> dict:
    """The job as a Task. Raises MCPError -32602 if this client has no such job."""
    try:
        found = await agent.get(pool, client_id, job_id)
    except agent.NotFound:
        raise MCPError(INVALID_PARAMS, NOT_FOUND) from None
    times = await pool.fetchrow(
        "SELECT created_at, coalesce(finished_at, started_at, created_at) AS updated, expires_at FROM jobs WHERE id = $1",
        job_id,
    )
    task = {
        "taskId": str(job_id),
        "status": _STATUS[found["status"]],
        "createdAt": _iso(times["created_at"]),
        "lastUpdatedAt": _iso(times["updated"]),
        "ttlMs": int((times["expires_at"] - times["created_at"]).total_seconds() * 1000),
        "pollIntervalMs": POLL_MS,
    }
    if found["status"] in _MESSAGE:
        task["statusMessage"] = _MESSAGE[found["status"]]
    if not with_payload:
        return task
    if found["status"] == "completed":
        from api.mcp import _reply  # the same reply the tool gives when it waits

        reply = _reply(found).model_dump()
        task["result"] = {
            "content": [{"type": "text", "text": json.dumps(reply, indent=2)}],
            "structuredContent": reply,
            "isError": False,
        }
    elif found["status"] == "dead":
        task["statusMessage"] = FAILED
        task["error"] = {"code": INTERNAL_ERROR, "message": FAILED}
    return task


def _job_id(text: str) -> UUID:
    try:
        return UUID(text)
    except ValueError:
        raise MCPError(INVALID_PARAMS, NOT_FOUND) from None


def _require_declared(ctx) -> None:
    if not declared(ctx):
        raise MCPError(
            MISSING_REQUIRED_CLIENT_CAPABILITY,
            f"declare the {EXTENSION} extension in this request's client capabilities",
            {"requiredCapabilities": {"extensions": {EXTENSION: {}}}},
        )


class Tasks(Extension):
    identifier = EXTENSION

    def methods(self):
        modern = frozenset(MODERN_PROTOCOL_VERSIONS)
        return [
            MethodBinding("tasks/get", TaskParams, self.get, protocol_versions=modern),
            MethodBinding("tasks/update", UpdateParams, self.update, protocol_versions=modern),
            MethodBinding("tasks/cancel", TaskParams, self.cancel, protocol_versions=modern),
        ]

    async def intercept_tool_call(self, params, ctx, call_next):
        if params.name not in JOB_TOOLS or not declared(ctx):
            return await call_next(ctx)
        ctx.request.scope.setdefault("state", {})[AS_TASK] = True
        result = await call_next(ctx)
        # A request refused before any job existed (bad arguments, a busy client) stays an
        # ordinary tool error: there is nothing to poll
        if result.is_error:
            return result
        client_id, app = _caller(ctx)
        job_id = UUID(result.structured_content["job_id"])
        # The job was committed before the tool returned, so tasks/get resolves this id at once
        return {"resultType": "task", **await _task(app.state.pool, client_id, job_id, with_payload=False)}

    async def get(self, ctx, params: TaskParams):
        _require_declared(ctx)
        client_id, app = _caller(ctx)
        return {"resultType": "complete", **await _task(app.state.pool, client_id, _job_id(params.task_id), True)}

    async def update(self, ctx, params: UpdateParams):
        """No job ever asks for input, so there is never anything outstanding to answer: a known
        task gets the empty acknowledgement and its responses are ignored, as the spec says."""
        _require_declared(ctx)
        client_id, app = _caller(ctx)
        await self._owned(app, client_id, params.task_id)
        return {"resultType": "complete"}

    async def cancel(self, ctx, params: TaskParams):
        """Always an empty acknowledgement for a known task. A queued job is cancelled at once, a
        running one by its worker shortly after, a finished one not at all (§8)."""
        _require_declared(ctx)
        client_id, app = _caller(ctx)
        job_id = await self._owned(app, client_id, params.task_id)
        async with app.state.pool.acquire() as conn:
            await queue.request_cancel(conn, job_id)
        return {"resultType": "complete"}

    async def _owned(self, app, client_id: int, task_id: str) -> UUID:
        job_id = _job_id(task_id)
        try:
            await agent.get(app.state.pool, client_id, job_id)
        except agent.NotFound:
            raise MCPError(INVALID_PARAMS, NOT_FOUND) from None
        return job_id
