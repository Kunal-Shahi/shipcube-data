"""The MCP-level smoke against a real MCP server (the official SDK, Streamable HTTP) serving our tools."""

import socket
import threading
import time
import uuid
from typing import Any, Optional

import pytest

mcp_server = pytest.importorskip("mcp.server.mcpserver")  # MCP SDK 2.x, as ShipCube's server
uvicorn = pytest.importorskip("uvicorn")

from shipcube_data import local, mcp_smoke  # noqa: E402
from shipcube_data.identity import Caller  # noqa: E402
from shipcube_data.tools import ToolError  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path, monkeypatch):
    ws = str(uuid.uuid4())
    for k, v in {"SHIPCUBE_DEV_MODE": "1", "SHIPCUBE_DATA_CATALOG_URL": str(tmp_path / "c.db"), "SHIPCUBE_DATA_WORKSPACE": ws,
                 "SHIPCUBE_DATA_DUCKDB_PATH": str(tmp_path / "olist.duckdb")}.items():
        monkeypatch.setenv(k, v)
    local.seed(local.Settings.from_env())
    tools = local.tools_from_env()
    caller = Caller(workspace_id=uuid.UUID(ws), via="mcp", email="dev@example.com", user_id=uuid.uuid4(), membership_role="developer",
                    token_id=uuid.uuid4(), scopes=["data:read", "data:migrate"])
    app = mcp_server.MCPServer("shipcube-test")

    def run(name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return tools.call(name, {k: v for k, v in args.items() if v is not None}, caller)
        except ToolError as e:
            raise mcp_server.exceptions.ToolError(str(e.to_dict())) from None

    @app.tool(name="data_list_cubes")
    def list_cubes() -> dict[str, Any]:
        return run("data_list_cubes", {})

    @app.tool(name="data_describe_cube")
    def describe_cube(cube: str) -> dict[str, Any]:
        return run("data_describe_cube", {"cube": cube})

    @app.tool(name="data_query")
    def query(query: dict[str, Any], mode: str = "run", dialect: Optional[str] = None) -> dict[str, Any]:
        return run("data_query", {"query": query, "mode": mode, "dialect": dialect})

    @app.tool(name="data_search_values")
    def search_values(dimension: str, text: str = "", limit: int = 20) -> dict[str, Any]:
        return run("data_search_values", {"dimension": dimension, "text": text, "limit": limit})

    @app.tool(name="data_propose_migration")
    def propose_migration(description: Optional[str] = None, ops: Optional[list[dict[str, Any]]] = None,
                          request: Optional[str] = None, code: Optional[str] = None) -> dict[str, Any]:
        return run("data_propose_migration", {"description": description, "ops": ops, "request": request, "code": code})

    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp", tools, ws
    srv.should_exit = True
    t.join(timeout=5)


def test_mcp_smoke_end_to_end(server):
    url, tools, ws = server
    results = mcp_smoke.run(url, "sc_pat_test", say=lambda line: None)
    assert [n for n, _, _ in results] == ["tools/list", "data_list_cubes", "data_describe_cube", "data_query (L0)",
                                          "data_search_values", "data_propose_migration", "agent cannot apply"]
    assert all(good for _, good, _ in results), results
    cat = tools.layer_for(uuid.UUID(ws)).catalog
    assert cat.head() == 5 and [m["status"] for m in cat.migrations("proposed")] == ["proposed"]


def test_mcp_smoke_reports_a_missing_tool(server, monkeypatch):
    url, _, _ = server
    monkeypatch.setattr(mcp_smoke, "EXPECTED", mcp_smoke.EXPECTED | {"data_extra"})
    results = mcp_smoke.run(url, "sc_pat_test", say=lambda line: None)
    assert results == [("tools/list", False, results[0][2])] and "data tools are" in results[0][2]
