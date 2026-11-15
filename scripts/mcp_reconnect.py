"""The MCP half of Gate 3: a real MCP client, the official SDK's, starts verify_code, loses its
connection before the verdict, then reconnects as a brand-new client and gets the result.

The retry is safe because both calls carry the same idempotency_key: the second call finds the
job the first one created, instead of judging the code twice (DECISIONS.md, 75 and 120).

Usage, with the server from scripts/demo.sh running and the key it printed:
    JUDGE_MCP_KEY=jk_... python scripts/mcp_reconnect.py http://localhost:8000/mcp
The key comes from the environment, not the command line, so it stays out of the shell history.
"""

import asyncio
import os
import sys
import time
import uuid

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

# Sleeps, so the verdict takes several seconds: long enough to drop the connection first
SLOW_SUM = "import time\ntime.sleep(2)\na, b = map(int, input().split())\nprint(a + b)\n"
# The third expected answer is wrong on purpose: every test still runs and gets its own verdict
TESTS = [
    {"input": "1 2\n", "expected": "3"},
    {"input": "2000000000 2000000000\n", "expected": "4000000000"},
    {"input": "5 5\n", "expected": "11"},
]
DROP_AFTER = 1.5


def connect(url: str, key: str) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60)
    return Client(streamable_http_client(url, http_client=http))


async def main(url: str, key: str) -> int:
    arguments = {"language": "python", "source": SLOW_SUM, "tests": TESTS,
                 "idempotency_key": f"reconnect-demo-{uuid.uuid4().hex[:8]}"}

    print(f"1. First connection: verify_code, then the connection drops after {DROP_AFTER} s.")
    async with connect(url, key) as client:
        with anyio.move_on_after(DROP_AFTER) as dropped:
            await client.call_tool("verify_code", arguments)
    if not dropped.cancelled_caught:
        print("   The verdict came back before the drop, so there was nothing to recover. Run it again.")
        return 1
    print("   Dropped. The client never saw a job id.")

    print("2. A new client on a new connection retries with the same idempotency_key.")
    started = time.monotonic()
    async with connect(url, key) as client:
        result = await client.call_tool("verify_code", arguments)
        reply = result.structured_content
        while not result.is_error and reply["status"] in ("queued", "running"):
            await asyncio.sleep(1)  # the server waits up to 20 s per call, so this is rare
            result = await client.call_tool("get_result", {"job_id": reply["job_id"]})
            reply = result.structured_content
    if result.is_error:
        print("   Error:", result.content[0].text)
        return 1
    print(f"   Got the result after {time.monotonic() - started:.1f} s: job {reply['job_id']}")
    print(f"   {reply['status']}, verdict {reply['verdict']}, first failing test {reply['failed_test']},",
          "per test:", [t["verdict"] for t in reply["tests"]])
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or not os.environ.get("JUDGE_MCP_KEY"):
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(sys.argv[1], os.environ["JUDGE_MCP_KEY"])))
