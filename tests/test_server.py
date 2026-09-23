"""Protocol-level tests: talk to the server the way an MCP client does.

* In-process through the installed SDK's own client (``mcp.Client`` on mcp
  2.x, ``create_connected_server_and_client_session`` on mcp 1.x).
* A real subprocess speaking newline-delimited JSON-RPC over stdio, with no
  SDK client involved at all, for both ``python -m mcp_git_historian.server``
  and the ``mcp-git-historian`` console entry point (``python -m
  mcp_git_historian`` with no arguments).

Skipped as a whole when the ``mcp`` package is not installed, so the core
and CLI tests still run without it.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")
anyio = pytest.importorskip("anyio")

from conftest import rev  # noqa: E402

from mcp_git_historian import __version__, server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "repo_summary": ["repo_path"],
    "hotspots": ["repo_path"],
    "file_history": ["repo_path", "file"],
    "blame_summary": ["repo_path", "file"],
    "bus_factor": ["repo_path"],
    "search_commits": ["repo_path", "query"],
    "find_change": ["repo_path", "pattern"],
    "change_coupling": ["repo_path"],
    "knowledge_risk": ["repo_path"],
    "commit_details": ["repo_path", "ref"],
    "health_report": ["repo_path"],
}


@asynccontextmanager
async def connect():
    """An initialized client session against the in-process server."""
    if server.SDK_MAJOR == 2:
        from mcp import Client

        async with Client(server.mcp) as client:
            yield client
    else:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
            yield session


def as_dict(model) -> dict:
    """SDK objects as wire-format dicts (camelCase), whatever the SDK major."""
    return model.model_dump(by_alias=True, exclude_none=True)


def call_all(calls: list[tuple[str, dict]]) -> list[dict]:
    async def go():
        async with connect() as client:
            return [as_dict(await client.call_tool(name, args)) for name, args in calls]

    return anyio.run(go)


def list_tools() -> list[dict]:
    async def go():
        async with connect() as client:
            return [as_dict(t) for t in (await client.list_tools()).tools]

    return anyio.run(go)


def text_of(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result["content"])


def test_lists_every_tool_with_schema_and_read_only_annotations():
    tools = {t["name"]: t for t in list_tools()}
    assert set(tools) == set(EXPECTED_TOOLS)
    for name, required in EXPECTED_TOOLS.items():
        tool = tools[name]
        schema = tool["inputSchema"]
        assert schema["required"] == required, name
        assert "Absolute path" in schema["properties"]["repo_path"]["description"]
        assert tool["title"], name
        assert tool["description"], name
        ann = tool["annotations"]
        assert ann["readOnlyHint"] is True and ann["destructiveHint"] is False, name
        assert ann["idempotentHint"] is True and ann["openWorldHint"] is False, name
    assert tools["hotspots"]["inputSchema"]["properties"]["top"]["minimum"] == 1
    assert tools["search_commits"]["inputSchema"]["properties"]["regex"]["default"] is False


def test_every_tool_runs_end_to_end(forensics_repo: Path):
    repo = str(forensics_repo)
    merge = rev(forensics_repo, "Merge branch 'feature/greeting'")
    calls = [
        ("repo_summary", {"repo_path": repo}),
        ("hotspots", {"repo_path": repo, "since": "", "top": 3}),
        ("file_history", {"repo_path": repo, "file": "src/helpers.py"}),
        ("blame_summary", {"repo_path": repo, "file": "legacy/lexer.py"}),
        ("bus_factor", {"repo_path": repo}),
        ("search_commits", {"repo_path": repo, "query": "[WIP"}),
        ("find_change", {"repo_path": repo, "pattern": "tokens_keep_tabs"}),
        ("change_coupling", {"repo_path": repo, "since": ""}),
        ("knowledge_risk", {"repo_path": repo, "inactive_after": "2026-01-01", "top": 2}),
        ("commit_details", {"repo_path": repo, "ref": merge}),
        ("health_report", {"repo_path": repo, "since": "", "inactive_after": "2026-01-01"}),
    ]
    results = call_all(calls)
    for (name, _), result in zip(calls, results):
        assert result.get("isError") is not True, (name, text_of(result))
        data = result["structuredContent"]
        assert data["repo_path"] == repo, name
        assert json.loads(text_of(result)) == data, name  # text mirror for older clients
    by_name = {name: r["structuredContent"] for (name, _), r in zip(calls, results)}
    assert by_name["repo_summary"]["total_authors"] == 3
    assert by_name["hotspots"]["hotspots"][0]["file"] == "src/app.py"
    assert by_name["file_history"]["count"] == 6
    assert by_name["blame_summary"]["dominant_author"] == "Carol Gone"
    assert by_name["bus_factor"]["merge_commits_excluded"] == 2
    assert by_name["search_commits"]["commits"][0]["subject"] == "fix: [WIP] thing"
    assert by_name["find_change"]["commits"][0]["author"] == "Carol Gone"
    assert by_name["change_coupling"]["pairs"][0]["degree"] == 1.0
    assert by_name["knowledge_risk"]["files"][0]["orphaned"] is True
    assert by_name["commit_details"]["is_merge"] is True
    assert by_name["health_report"]["markdown"].startswith("# Git health report: ")
    assert "1. `legacy/parser.py`" in by_name["health_report"]["markdown"]


def test_tool_errors_reach_the_model_verbatim(forensics_repo: Path, tmp_path: Path):
    """mcp 2.x hides the text of unexpected exceptions; ours must survive."""
    repo = str(forensics_repo)
    results = call_all([
        ("hotspots", {"repo_path": str(tmp_path / "missing")}),
        ("commit_details", {"repo_path": repo, "ref": "does-not-exist"}),
        ("search_commits", {"repo_path": repo, "query": "[WIP", "regex": True}),
        ("hotspots", {"repo_path": repo, "since": "garbage"}),
        ("search_commits", {"repo_path": repo, "query": "fix", "limit": -1}),
    ])
    assert all(r["isError"] is True for r in results)
    assert "Repo not found" in text_of(results[0]) and "absolute path" in text_of(results[0])
    assert "Unknown commit 'does-not-exist'" in text_of(results[1])
    assert "Invalid regular expression" in text_of(results[2])
    assert "could not understand since='garbage'" in text_of(results[3])
    assert "limit" in text_of(results[4])  # rejected by the input schema (minimum 1)


# ---------------------------------------------------------------------------
# real stdio subprocess, raw JSON-RPC
# ---------------------------------------------------------------------------

class StdioPeer:
    """Minimal newline-delimited JSON-RPC client for an MCP server subprocess."""

    def __init__(self, *args: str) -> None:
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, cwd=str(ROOT),
        )
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.next_id = 0

    def _pump(self) -> None:
        for raw in self.proc.stdout:
            self.lines.put(raw)

    def send(self, message: dict) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        while True:
            message = json.loads(self.lines.get(timeout=timeout).decode("utf-8"))
            if message.get("id") == self.next_id:
                return message

    def close(self) -> None:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.mark.parametrize(
    "entry",
    [["-m", "mcp_git_historian.server"], ["-m", "mcp_git_historian"], ["-m", "mcp_git_historian", "serve"]],
    ids=["server-module", "cli-default", "cli-serve"],
)
def test_stdio_server_answers_initialize_list_and_call(entry: list[str], forensics_repo: Path):
    peer = StdioPeer(*entry)
    try:
        init = peer.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "raw-jsonrpc-test", "version": "0"},
        })
        result = init["result"]
        assert result["serverInfo"]["name"] == "mcp-git-historian"
        if server.SDK_MAJOR == 2:
            assert result["serverInfo"]["version"] == __version__
        assert "ABSOLUTE path" in result["instructions"]
        assert "tools" in result["capabilities"]
        peer.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = peer.request("tools/list")["result"]["tools"]
        assert {t["name"] for t in tools} == set(EXPECTED_TOOLS)

        call = peer.request("tools/call", {"name": "bus_factor", "arguments": {"repo_path": str(forensics_repo)}})
        assert call["result"].get("isError") is not True
        assert call["result"]["structuredContent"]["total_authors"] == 3

        bad = peer.request("tools/call", {"name": "hotspots", "arguments": {"repo_path": "/no/such/repo"}})
        assert bad["result"]["isError"] is True
        assert "Repo not found at /no/such/repo — pass an absolute path" in bad["result"]["content"][0]["text"]
    finally:
        peer.close()
