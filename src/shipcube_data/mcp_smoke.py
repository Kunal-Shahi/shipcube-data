"""Data-tool smoke test through a running ShipCube MCP server (Streamable HTTP, bearer PAT).

    SHIPCUBE_MCP_URL=http://localhost:8080/mcp SHIPCUBE_PAT=sc_pat_... shipcube-data smoke --mcp

The same sequence as the in-process smoke, but end to end: auth, the server's tool mounting,
caller resolution and error mapping. Uses only the standard library (a minimal MCP client:
initialize, tools/list, tools/call; JSON or SSE responses).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Optional

PROTOCOL = "2025-06-18"
EXPECTED = {"data_list_cubes", "data_describe_cube", "data_query", "data_search_values", "data_propose_migration"}


class McpError(RuntimeError):
    pass


class McpClient:
    def __init__(self, url: str, token: str, timeout_s: float = 60):
        self.url, self.token, self.timeout = url, token, timeout_s
        self.session: Optional[str] = None
        self._id = 0

    def _post(self, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self.token}", "MCP-Protocol-Version": PROTOCOL}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                body = resp.read().decode()
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise McpError(f"HTTP {e.code} from {self.url}: {e.read().decode()[:300]}") from None
        if "id" not in payload:  # notification
            return None
        if "text/event-stream" in ctype:  # take the JSON-RPC response for our id from the SSE stream
            for line in body.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == payload["id"]:
                        return msg
            raise McpError("no response in the event stream")
        return json.loads(body)

    def request(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        self._id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        assert msg is not None
        if "error" in msg:
            raise McpError(f"{method}: {msg['error'].get('message')}")
        return msg["result"]

    def initialize(self) -> dict[str, Any]:
        out = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                          "clientInfo": {"name": "shipcube-data-smoke", "version": "1"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return out

    def call(self, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
        """(ok, structured result or error text)."""
        res = self.request("tools/call", {"name": name, "arguments": args})
        text = " ".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            return False, text
        if res.get("structuredContent") is not None:
            sc = res["structuredContent"]
            return True, sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
        return True, json.loads(text) if text.strip().startswith(("{", "[")) else text


def run(url: Optional[str] = None, token: Optional[str] = None, say: Callable[[str], None] = print) -> list[tuple[str, bool, str]]:
    url = url or os.environ.get("SHIPCUBE_MCP_URL", "http://localhost:8080/mcp")
    token = token or os.environ.get("SHIPCUBE_PAT") or os.environ.get("SHIPCUBE_TOKEN", "")
    client = McpClient(url, token)
    results: list[tuple[str, bool, str]] = []

    def step(name: str, fn: Callable[[], str]) -> None:
        try:
            results.append((name, True, fn()))
        except (AssertionError, McpError, KeyError, ValueError) as e:
            results.append((name, False, str(e)[:300]))
        ok, detail = results[-1][1], results[-1][2]
        say(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")

    def ok(name: str, args: dict[str, Any]) -> Any:
        good, out = client.call(name, args)
        assert good, f"{name} failed: {out}"
        return out

    def tools() -> str:
        client.initialize()
        names = {t["name"] for t in client.request("tools/list").get("tools", [])}
        data = {n for n in names if n.startswith("data_")}
        assert data == EXPECTED, f"data tools are {sorted(data)}"
        return ", ".join(sorted(data))

    def list_cubes() -> str:
        cubes = ok("data_list_cubes", {})["cubes"]
        assert {"orders", "order_items", "customers"} <= {c["name"] for c in cubes}
        return f"{len(cubes)} cubes"

    def describe() -> str:
        d = ok("data_describe_cube", {"cube": "orders"})
        assert d["partitioned_by"], d
        return f"{len(d['measures'])} measures, partitioned by {d['partitioned_by']['column']}"

    def query() -> str:
        out = ok("data_query", {"query": {"measures": ["orders.count"], "dimensions": ["states.region"],
                                          "time": {"dimension": "orders.purchased_at", "range": ["2018-01-01", "2018-03-31"]}, "limit": 50}})
        assert out["rows"] and out["partitions"]["orders"]["scanned"] == 3, out.get("partitions")
        return f"{len(out['rows'])} rows, 3/20 partitions, {out['query_id']}"

    def search() -> str:
        values = ok("data_search_values", {"dimension": "customers.city", "text": "sao"})["values"]
        assert "sao paulo" in values, values
        return ", ".join(values)

    proposal: dict[str, Any] = {}

    def propose() -> str:
        member = f"smoke_customers_{uuid.uuid4().hex[:6]}"
        out = ok("data_propose_migration", {"description": "Smoke test: distinct ordering customers",
                                            "ops": [{"op": "add_measure", "name": f"orders.{member}", "sql": "customer_id",
                                                     "agg": "count_distinct"}]})
        assert out["status"] == "proposed" and out["approval_url"], out
        proposal.update(out, member=member)
        return f"{out['id']} proposed; approval_url {out['approval_url']}"

    def cannot_apply() -> str:
        for name in ("data_apply_migration", "apply_migration"):
            try:
                good, out = client.call(name, {"id": proposal["id"]})
            except McpError:
                continue  # rejected at the protocol level: fine
            assert not good, f"{name} ran: {out}"
        measures = {m["path"] for m in ok("data_describe_cube", {"cube": "orders"})["measures"]}
        assert f"orders.{proposal['member']}" not in measures, "the proposed measure is live"
        return "no apply tool; the proposed measure is not in the model"

    for name, fn in [("tools/list", tools), ("data_list_cubes", list_cubes), ("data_describe_cube", describe),
                     ("data_query (L0)", query), ("data_search_values", search), ("data_propose_migration", propose)]:
        step(name, fn)
        if name == "tools/list" and not results[-1][1]:
            break
    if proposal:
        step("agent cannot apply", cannot_apply)
    return results
