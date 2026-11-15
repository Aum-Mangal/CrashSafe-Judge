"""Leakage tests (ARCHITECTURE.md §13): secret data never comes out of the API, the pages or MCP.

Checking one page for one secret proves only that page is safe. So these tests plant a unique
marker string, a canary, in every secret, then request every route the app has, as every kind
of viewer, with every interesting argument, and search every response for every canary that
viewer must not see. The routes are read from the app itself, so a route added later is crawled
automatically, and test_every_route_can_be_crawled fails until the crawl knows how to call it.

Viewers: an anonymous visitor, two contestants (ann and bob, signed in), and two MCP clients
(agent and rival, each with its API key).

Secrets, and who may see them:
- Hidden test input and answers, of contest and practice problems: nobody, not even the one
  judged on them.
- A problem in a contest that hasn't started (title, statement, slug, samples): nobody.
- Invite codes, password hashes and the signing secret: nobody.
- A contestant's code and email, an agent's code, input and output: only their owner.

A search that finds nothing proves something only if it could have found something. So each
test also has positive controls: the same search finding a secret where it is allowed.
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from itertools import product

import pytest

from admin.commands import create_client
from api.app import create_app
from api.ratelimit import Limit, LoginLimits
from core import queue
from helpers import LEGACY, MODERN, TEST_SECRET, insert_problem, make_contest, mcp_request, open_api
from worker.judge import judge

pytestmark = pytest.mark.sandbox

VIEWERS = ("anon", "ann", "bob", "agent", "rival")
MISSING_CONTEST = 999_999
MISSING_SLUG = "no-such-problem"
MISSING_JOB = uuid.UUID("00000000-0000-4000-8000-000000000000")
UNKNOWN_EMAIL = "nobody@example.com"
PASSWORD = "leak-test-password"
# The crawl tries the login forms a dozen times from one address. The limits have their own
# tests (test_api.py); here they would only turn later attempts into 429s.
UNLIMITED = LoginLimits(per_address=Limit("address", 1000, timedelta(minutes=1)))
OPTIONS = {"cookie_secure": False, "leaderboard_seconds": 0, "login_limits": UNLIMITED, "mcp_wait_seconds": 0}

# Both pass the sample (its input is its answer), then print the hidden test's input, which is
# exactly what a cheater would try. One prints it and gets WA, the other also crashes: RE.
ECHO = "import sys\ndata = sys.stdin.read()\nsys.stdout.write(data)\nsys.stderr.write(data)\n"
ECHO_THEN_CRASH = ECHO + "if data != 's\\n':\n    raise SystemExit(3)\n"


class World:
    """The planted data: ids, tokens, keys and the canaries, each unique to this run."""

    def __init__(self):
        def canary(name):
            return f"canary{name}{uuid.uuid4().hex[:10]}"

        self.never = {  # nobody may ever see these
            "hidden input": canary("hiddenin"),
            "hidden answer": canary("hiddenout"),
            "practice hidden input": canary("practicein"),
            "practice hidden answer": canary("practiceout"),
            "early title": canary("earlytitle"),
            "early statement": canary("earlystatement"),
            "early slug": canary("earlyslug"),
            "early sample": canary("earlysample"),
            "early hidden input": canary("earlyhiddenin"),
            "early hidden answer": canary("earlyhiddenout"),
            "invite code": canary("invite"),
            "early invite code": canary("earlyinvite"),
            "password hash": "$argon2",
            "signing secret": TEST_SECRET,
        }
        self.hidden = [k for k in self.never if "hidden" in k]
        self.emails = {name: f"{name}{uuid.uuid4().hex[:10]}@example.com" for name in ("ann", "bob")}
        self.source = canary("source")
        self.agent_source, self.agent_stdin = canary("agentsource"), canary("agentstdin")
        self.private = {  # each owner's own; everyone else must not see them
            "ann": {"ann's email": self.emails["ann"], "ann's code": self.source},
            "bob": {"bob's email": self.emails["bob"]},
            # run_code prints its stdin, so this is also the agent's output
            "agent": {"agent's code": self.agent_source, "agent's stdin and output": self.agent_stdin},
        }
        self.statement = canary("statement")  # a running contest's statement: anyone may see it
        self.tokens: dict[str, str] = {}  # contestants' JWTs
        self.keys: dict[str, str] = {}  # MCP clients' API keys
        self.jobs: dict[str, list[uuid.UUID]] = {"ann": [], "agent": []}
        self.slug = self.practice = self.running = self.upcoming = None

    def forbidden(self, viewer: str) -> dict[str, str]:
        secret = dict(self.never)
        for owner, theirs in self.private.items():
            if viewer != owner:
                secret.update(theirs)
        return secret

    def values(self) -> dict[str, list]:
        """What each parameter is filled with: real ones, secret ones and missing ones."""
        return {
            "contest_id": [self.running, self.upcoming, MISSING_CONTEST],
            "label": ["A", "Z"],
            "slug": [self.slug, self.practice, self.never["early slug"], MISSING_SLUG],
            "job_id": [*self.jobs["ann"], *self.jobs["agent"], MISSING_JOB],
        }

    def bodies(self) -> dict[tuple[str, str], list[dict]]:
        """How to call each route that writes. Requests that would succeed are included: a
        reply to a real submission must leak nothing either."""
        values = self.values()
        wrong_register = {"email": "new@example.com", "display_name": "New", "password": PASSWORD,
                          "invite_code": "wrong-code"}
        # A wrong password for a real account, and for no account: see twin()
        wrong_logins = [{"email": email, "password": "wrong-password"} for email in (self.emails["ann"], UNKNOWN_EMAIL)]
        return {
            ("POST", "/auth/register"): [{"json": wrong_register}],
            ("POST", "/auth/login"): [{"json": b} for b in wrong_logins],
            ("POST", "/submissions"): [
                {"json": {"problem": slug, "language": "python", "source": "print(1)", "contest_id": contest}}
                for slug in values["slug"] for contest in (None, self.running, self.upcoming)
            ],
            ("DELETE", "/submissions/{job_id}"): [{}],
            ("POST", "/login"): [{"form": b} for b in wrong_logins],
            ("POST", "/register"): [{"form": wrong_register}],
            ("POST", "/logout"): [{"form": {}}],
            ("POST", "/c/{contest_id}/{label}"): [{"form": {"language": "python", "source": "print(1)"}}],
            ("POST", "/mcp"): [{"mcp": call} for call in self.mcp_calls()],
        }

    def mcp_calls(self) -> list[dict]:
        """Every tool with every interesting argument, in both protocol eras."""
        values = self.values()
        calls = [{"era": MODERN, "method": "server/discover", "params": {}}]
        for era in (MODERN, LEGACY):
            def tool(name, **arguments):
                calls.append({"era": era, "method": "tools/call", "params": {"name": name, "arguments": arguments}})

            calls.append({"era": era, "method": "tools/list", "params": {}})
            tool("list_problems")
            for slug in values["slug"]:
                tool("get_problem", slug=slug)
                tool("submit_solution", problem_slug=slug, language="python", source="print(1)")
            for job in values["job_id"]:
                tool("get_result", job_id=str(job))
                tool("cancel_run", job_id=str(job))
            tool("run_code", language="python", source="print(1)", stdin="")
            tool("verify_code", language="python", source="print(1)", tests=[{"input": "", "expected": "1"}])
        # The Tasks extension (2026-07-28 only): tools that answer with a task, and the task
        # methods on every job, with the extension declared and without it
        for name, arguments in (("run_code", {"stdin": ""}), ("verify_code", {"tests": [{"input": "", "expected": "1"}]})):
            calls.append({"era": MODERN, "method": "tools/call", "tasks": True, "params": {
                "name": name, "arguments": {"language": "python", "source": "print(1)", **arguments}}})
        for slug in values["slug"]:
            calls.append({"era": MODERN, "method": "tools/call", "tasks": True, "params": {
                "name": "submit_solution", "arguments": {"problem_slug": slug, "language": "python", "source": "print(1)"}}})
        for job, method, declared in product(values["job_id"], ("tasks/get", "tasks/update", "tasks/cancel"), (True, False)):
            calls.append({"era": MODERN, "method": method, "tasks": declared, "params": {"taskId": str(job)}})
        return calls


def app_routes() -> list[tuple[str, str]]:
    """Every (method, path template) the app serves, including FastAPI's own /docs pages."""
    app = create_app("postgresql://unused", TEST_SECRET)  # connects only when started
    found = []

    def walk(routes):
        for route in routes:
            # FastAPI 0.162 keeps each include_router() as one entry holding the router's own
            # routes. We include ours without a prefix, so their paths are as written.
            if hasattr(route, "original_router"):
                walk(route.original_router.routes)
            for method in sorted(getattr(route, "methods", None) or ()):
                if method != "HEAD":
                    found.append((method, route.path))

    walk(app.routes)
    # That attribute is private, so check against the public OpenAPI list: if a FastAPI upgrade
    # hides routes from the walk again, this fails instead of the crawl silently shrinking
    documented = {(m.upper(), p) for p, ops in app.openapi()["paths"].items() for m in ops}
    assert documented <= set(found), f"routes the walk missed: {documented - set(found)}"
    return found


def path_params(template: str) -> list[str]:
    return re.findall(r"{(\w+)}", template)


@dataclass(frozen=True)
class Request:
    viewer: str
    method: str
    path: str
    params: tuple = ()
    body: str = ""  # the JSON of one entry from World.bodies()

    @property
    def url(self) -> str:
        return self.path.format(**dict(self.params))

    def sent(self) -> list[str]:
        """The text this request carried. A reply may repeat it, like a login form refilling the
        email: the sender already knows it, so that is no leak."""
        body = json.loads(self.body) if self.body else {}
        mcp = body.get("mcp", {}).get("params", {})
        fields = body.get("json") or body.get("form") or mcp.get("arguments") or mcp
        return [v for v in fields.values() if isinstance(v, str)]


def twin(w: World, req: Request) -> Request:
    """The same request with each secret swapped for something that doesn't exist. Where the
    secret is that something exists at all, both must get exactly the same reply."""
    params = dict(req.params)
    if params.get("slug") == w.never["early slug"]:
        params["slug"] = MISSING_SLUG
    if params.get("contest_id") == w.upcoming and params.get("label") == "A":
        params["label"] = "Z"
    # A job you don't own looks like no job, by REST, the pages or MCP
    others = [job for owner, jobs in w.jobs.items() if owner != req.viewer for job in jobs]
    if params.get("job_id") in others:
        params["job_id"] = MISSING_JOB
    # A failed login must not reveal whether the account exists
    body = req.body.replace(w.never["early slug"], MISSING_SLUG).replace(w.emails["ann"], UNKNOWN_EMAIL)
    for job in others:
        body = body.replace(str(job), str(MISSING_JOB))
    return replace(req, params=tuple(sorted(params.items())), body=body)


def credentials(w: World, viewer: str) -> dict[str, str]:
    """A contestant sends the same token both ways: as a REST token and as the page cookie. An MCP
    client sends its API key."""
    if viewer in w.keys:
        return {"Authorization": f"Bearer {w.keys[viewer]}"}
    if viewer not in w.tokens:
        return {}
    token = w.tokens[viewer]
    return {"Authorization": f"Bearer {token}", "Cookie": f"judge_session={token}"}


async def call_tool(client, w: World, viewer: str, name: str, era: str = MODERN, **arguments) -> dict:
    headers, body = mcp_request(era, "tools/call", {"name": name, "arguments": arguments})
    reply = await client.post("/mcp", headers={**headers, **credentials(w, viewer)}, json=body)
    assert reply.status_code == 200, reply.text
    return reply.json()["result"]


@pytest.fixture
async def world(dsn, db, pool, caplog):
    caplog.set_level(logging.DEBUG)  # so test_no_secret_reaches_any_response can read every log line
    w = World()
    sample = ("s\n", "s\n", True)
    running_problem = await insert_problem(db, [sample, (w.never["hidden input"] + "\n", w.never["hidden answer"] + "\n", False)])
    await db.execute("UPDATE problems SET statement_md = $2 WHERE id = $1", running_problem, w.statement)
    practice = await insert_problem(db, [sample, (w.never["practice hidden input"] + "\n", w.never["practice hidden answer"] + "\n", False)])
    await db.execute("UPDATE problems SET is_public = true WHERE id = $1", practice)
    w.slug, w.practice = [await db.fetchval("SELECT slug FROM problems WHERE id = $1", p) for p in (running_problem, practice)]
    early = await insert_problem(db, [
        (w.never["early sample"] + "\n", "1\n", True),
        (w.never["early hidden input"] + "\n", w.never["early hidden answer"] + "\n", False),
    ])
    await db.execute(
        "UPDATE problems SET slug = $2, title = $3, statement_md = $4 WHERE id = $1",
        early, w.never["early slug"], w.never["early title"], w.never["early statement"],
    )
    w.running = await make_contest(db, -60, 3600, [running_problem], code=w.never["invite code"])
    w.upcoming = await make_contest(db, 3600, 7200, [early], code=w.never["early invite code"])
    for name in ("agent", "rival"):
        w.keys[name] = (await create_client(db, name, 20))[1]

    async with open_api(dsn, **OPTIONS) as client:
        for name, email in w.emails.items():
            reply = await client.post("/auth/register", json={
                "email": email, "display_name": name.title(), "password": PASSWORD, "invite_code": w.never["invite code"],
            })
            assert reply.status_code == 201, reply.text
            w.tokens[name] = reply.json()["access_token"]
        for program in (ECHO, ECHO_THEN_CRASH):
            reply = await client.post("/submissions", headers=credentials(w, "ann"), json={
                "problem": w.slug, "language": "python", "source": f"{program}# {w.source}\n", "contest_id": w.running,
            })
            assert reply.status_code == 201, reply.text
            w.jobs["ann"].append(uuid.UUID(reply.json()["id"]))
        # The agent runs its code on its own input, and tries the echo trick on a practice problem
        ran = await call_tool(client, w, "agent", "run_code", language="python",
                              source=f"{ECHO}# {w.agent_source}\n", stdin=w.agent_stdin + "\n")
        tried = await call_tool(client, w, "agent", "submit_solution", problem_slug=w.practice, language="python", source=ECHO)
        w.jobs["agent"] += [uuid.UUID(r["structuredContent"]["job_id"]) for r in (ran, tried)]

    # Judged in real sandboxes, as a worker would
    for _ in range(len(w.jobs["ann"]) + len(w.jobs["agent"])):
        claimed = await queue.claim(db, "leak-worker")
        result = await judge(pool, claimed, await db.fetchval("SELECT current_database()"))
        assert await queue.finish(db, claimed.job_id, "leak-worker", claimed.token, result)
    return w


async def crawl(client, w: World) -> dict[Request, object]:
    """Every route, as every viewer, with every combination of parameter values. Reads first,
    then writes, so the writes can't change what the reads see."""
    values, bodies = w.values(), w.bodies()
    reads, writes = [], []
    for viewer, (method, path) in product(VIEWERS, app_routes()):
        names = path_params(path)
        for combo in product(*(values[n] for n in names)):
            params = tuple(sorted(zip(names, combo)))
            if method == "GET":
                reads.append(Request(viewer, method, path, params))
            else:
                writes += [Request(viewer, method, path, params, json.dumps(b, sort_keys=True, default=str))
                           for b in bodies[(method, path)]]

    csrf = {}
    for viewer in ("ann", "bob"):
        page = await client.get("/", headers=credentials(w, viewer))
        csrf[viewer] = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)

    replies = {}
    for req in reads + writes:
        client.cookies.clear()  # each request carries only its own viewer's credentials
        headers = credentials(w, req.viewer)
        body = json.loads(req.body) if req.body else {}
        if "mcp" in body:
            mcp_headers, payload = mcp_request(**body["mcp"])
            reply = await client.request(req.method, req.url, headers={**mcp_headers, **headers}, json=payload)
        elif "json" in body:
            headers["Idempotency-Key"] = uuid.uuid4().hex
            reply = await client.request(req.method, req.url, headers=headers, json=body["json"])
        elif "form" in body:
            form = {**body["form"], "csrf": csrf.get(req.viewer, ""), "key": uuid.uuid4().hex}
            reply = await client.request(req.method, req.url, headers=headers, data=form)
        else:
            reply = await client.request(req.method, req.url, headers=headers)
        replies[req] = reply
    return replies


def without_sent(req: Request, text: str) -> str:
    for sent in req.sent():
        text = text.replace(sent, "<sent>")
    return text


def what_came_back(req: Request, reply) -> str:
    """The reply's body and every header, minus the request's own text."""
    return without_sent(req, reply.text + "\n" + "\n".join(f"{k}: {v}" for k, v in reply.headers.items()))


def shape(req: Request, reply) -> tuple:
    """What twins must share. Not every header: a refilled email of another length changes
    content-length, and that is the sender's own text again."""
    return reply.status_code, without_sent(req, reply.text), reply.headers.get("location")


# The JSON-RPC errors /mcp is meant to give. The 2026-07-28 wire sends them with HTTP 400.
DESIGNED_ERRORS = {(-32602, "no such task")}
MISSING_CAPABILITY = -32021


def malformed(req: Request, reply) -> bool:
    """Did the crawl send something the server couldn't parse, or break it? Then the reply
    proves nothing about leaks."""
    if reply.status_code >= 500:
        return True
    if req.path != "/mcp" or reply.status_code == 401:
        return reply.status_code in (400, 422)
    error = reply.json().get("error")
    return error is not None and error["code"] != MISSING_CAPABILITY and (error["code"], error["message"]) not in DESIGNED_ERRORS


def mcp_reply(replies, w: World, viewer: str, name: str, **arguments):
    call = {"era": MODERN, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    return replies[Request(viewer, "POST", "/mcp", body=json.dumps({"mcp": call}, sort_keys=True))].json()["result"]


async def test_no_secret_reaches_any_response(world, dsn, db, caplog):
    w = world
    async with open_api(dsn, **OPTIONS) as client:
        replies = await crawl(client, w)

        leaks = [
            f"{req.viewer} {req.method} {req.url} {req.body}: {what}"
            for req, reply in replies.items()
            for what, value in w.forbidden(req.viewer).items()
            if value in what_came_back(req, reply)
        ]
        assert not leaks, "\n".join(leaks)

        # Something that exists but is secret must look exactly like something that doesn't
        differ = []
        for req, reply in replies.items():
            other = twin(w, req)
            if other != req:
                theirs = replies[other]
                if shape(req, reply) != shape(other, theirs):
                    differ.append(f"{req.viewer} {req.method} {req.url} {req.body} answers unlike {other.url} {other.body}")
        assert not differ, "\n".join(differ)

        # The crawl itself worked: no request was malformed or broke the server, and every
        # page was really reached by someone, not just turned away
        broken = [f"{r.viewer} {r.method} {r.url} {r.body}: {replies[r].status_code} {replies[r].text[:200]}"
                  for r in replies if malformed(r, replies[r])]
        assert not broken, "\n".join(broken)
        reached = {(r.method, r.path) for r in replies if r.method == "GET" and replies[r].status_code == 200}
        assert reached == {route for route in app_routes() if route[0] == "GET"}
        mcp_ok = [r for r in replies if r.path == "/mcp" and replies[r].status_code == 200 and "error" not in replies[r].json()]
        assert {r.viewer for r in mcp_ok} == {"agent", "rival"}  # MCP answered both clients, and only them

        # Positive controls: the same search finds what each viewer is allowed to see
        mine = replies[Request("ann", "GET", "/submissions/{job_id}", (("job_id", w.jobs["ann"][0]),))]
        assert mine.json()["verdict"] == "WA" and mine.json()["failed_test"] == 2  # REST doesn't return code
        assert w.source in replies[Request("ann", "GET", "/s/{job_id}", (("job_id", w.jobs["ann"][0]),))].text
        assert w.source in replies[Request("ann", "GET", "/s/{job_id}", (("job_id", w.jobs["ann"][1]),))].text
        public = replies[Request("anon", "GET", "/c/{contest_id}/{label}", (("contest_id", w.running), ("label", "A")))]
        assert w.statement in public.text
        ran = mcp_reply(replies, w, "agent", "get_result", job_id=str(w.jobs["agent"][0]))
        assert w.agent_stdin in ran["structuredContent"]["stdout"]
        tried = mcp_reply(replies, w, "agent", "get_result", job_id=str(w.jobs["agent"][1]))
        assert (tried["structuredContent"]["verdict"], tried["structuredContent"]["failed_test"]) == ("WA", 2)
        polled = {"era": MODERN, "method": "tasks/get", "tasks": True, "params": {"taskId": str(w.jobs["agent"][0])}}
        polled = replies[Request("agent", "POST", "/mcp", body=json.dumps({"mcp": polled}, sort_keys=True))].json()["result"]
        assert polled["status"] == "completed" and w.agent_stdin in polled["result"]["structuredContent"]["stdout"]
        practice = mcp_reply(replies, w, "rival", "get_problem", slug=w.practice)
        assert practice["structuredContent"]["samples"] == [{"input": "s\n", "expected": "s\n"}]

        # Only the clock kept the early problem hidden: once its contest starts, the crawl finds it
        await db.execute("UPDATE contests SET starts_at = now() - interval '1 minute' WHERE id = $1", w.upcoming)
        shown = await client.get(f"/problems/{w.never['early slug']}")
        assert w.never["early title"] in shown.text and w.never["early sample"] in shown.text
        assert w.never["early statement"] in (await client.get(f"/c/{w.upcoming}/A")).text
        assert w.never["early title"] in json.dumps(await call_tool(client, w, "rival", "get_problem", slug=w.never["early slug"]))
        assert w.never["early hidden input"] not in shown.text

    # Source, input and program output are never logged (ARCHITECTURE.md §10, Data)
    logged = [r.getMessage() for r in caplog.get_records("setup") + caplog.get_records("call")]
    for what, value in [*((h, w.never[h]) for h in w.hidden), ("ann's code", w.source),
                        ("agent's code", w.agent_source), ("agent's stdin", w.agent_stdin)]:
        assert not any(value in line for line in logged), what


async def test_hidden_test_output_is_never_stored(world, db):
    """Data that is never stored can't leak through any future bug (ARCHITECTURE.md §4)."""
    w = world
    judged = await db.fetch("SELECT verdict, failed_test FROM results ORDER BY verdict, failed_test")
    # ann's two reached the hidden test; the agent's run printed its input; its echo reached the hidden test
    assert [tuple(r) for r in judged] == [("OK", None), ("RE", 2), ("WA", 2), ("WA", 2)]

    tables = [r["tablename"] for r in await db.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]

    async def rows_holding(table, text):
        return await db.fetchval(f'SELECT count(*) FROM "{table}" t WHERE t::text LIKE $1', f"%{text}%")

    # Three programs printed a hidden input to stdout and stderr. Only testcases may hold it.
    for table in tables:
        if table != "testcases":
            for what in w.hidden:
                assert await rows_holding(table, w.never[what]) == 0, f"{what} stored in {table}"
    # Positive controls: the same scan does find text that is stored
    assert await rows_holding("testcases", w.never["hidden input"]) == 1
    assert await rows_holding("jobs", w.source) == 2
    assert await rows_holding("results", w.agent_stdin) == 1  # run_code keeps its output


def test_every_route_can_be_crawled():
    """Runs without a database. A new route must be given parameter values and, if it writes,
    a request body before the leakage crawl can call it: that is the moment to think about
    what it could leak."""
    w = World()
    values, bodies = w.values(), w.bodies()
    for method, path in app_routes():
        for name in path_params(path):
            assert name in values, f"{method} {path}: add values for {{{name}}} to World.values()"
        if method != "GET":
            assert (method, path) in bodies, f"{method} {path}: add a request to World.bodies()"
    assert set(bodies) <= set(app_routes()), "World.bodies() names a route that no longer exists"
