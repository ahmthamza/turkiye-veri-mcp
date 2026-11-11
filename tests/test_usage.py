"""Kullanım günlüğü (usage) testleri.

Çevrimdışı birim testleri: anonim kimlik (HMAC), X-Forwarded-For ayrıştırma,
özet toplama, bozuk satır toleransı ve stdio davranışı. Son test gerçek
sunucuyu yerel bir portta ``--transport http`` ile başlatır ve gerçek bir MCP
istemcisiyle bağlanır (internet gerekmez; yalnızca 127.0.0.1).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from turkiye_veri_mcp import usage

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("TURKIYE_VERI_USAGE_LOG", str(path))
    monkeypatch.setenv("TURKIYE_VERI_USAGE_SALT", "test-salt")
    return path


def _write(path: Path, events: list) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write((event if isinstance(event, str) else json.dumps(event)) + "\n")


def _ts(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def test_anon_id_stable_and_salted():
    a = usage.anon_id("203.0.113.7", salt=b"s1")
    assert a == usage.anon_id("203.0.113.7", salt=b"s1")
    assert len(a) == 16 and int(a, 16) >= 0
    assert a != usage.anon_id("203.0.113.7", salt=b"s2")
    assert a != usage.anon_id("203.0.113.8", salt=b"s1")
    # IP and EVDS-key ids live in separate namespaces
    assert a != usage.anon_id("203.0.113.7", purpose="evds", salt=b"s1")


def test_env_salt_changes_ids(log, monkeypatch):
    first = usage.identity({"x-forwarded-for": "203.0.113.7"}, None)["v"]
    monkeypatch.setenv("TURKIYE_VERI_USAGE_SALT", "another-salt")
    assert usage.identity({"x-forwarded-for": "203.0.113.7"}, None)["v"] != first


def test_file_salt_created_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("TURKIYE_VERI_USAGE_SALT", raising=False)
    monkeypatch.setenv("TURKIYE_VERI_USAGE_LOG", str(tmp_path / "u.jsonl"))
    usage._salt_cache.clear()
    first = usage.anon_id("198.51.100.1")
    salt_file = tmp_path / "u.jsonl.salt"
    assert salt_file.exists() and len(salt_file.read_text().strip()) == 64
    usage._salt_cache.clear()  # simulate a restart: salt comes back from the file
    assert usage.anon_id("198.51.100.1") == first


def test_raw_ip_and_key_never_written(log):
    ident = usage.identity(
        {"X-Forwarded-For": "203.0.113.7, 10.0.0.1", "X-Evds-Api-Key": "SECRETKEY123"}, "10.0.0.2"
    )
    token = usage.set_current(ident)
    try:
        usage.record("tuik_list_themes")
    finally:
        usage.reset_current(token)
    usage.record_session(protocol_version="2025-11-25", client="claude-code", client_version="2.0", ident=ident)
    text = log.read_text()
    for secret in ("203.0.113.7", "10.0.0.1", "10.0.0.2", "SECRETKEY123"):
        assert secret not in text
    lines = [json.loads(line) for line in text.splitlines()]
    assert lines[0]["tool"] == "tuik_list_themes" and lines[0]["v"] == ident["v"] and lines[0]["k"] == ident["k"]
    assert lines[1]["event"] == "session" and lines[1]["client"] == "claude-code"


def test_stdio_record_unchanged(log):
    usage.record("evds_categories")  # no request identity in context
    (event,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert set(event) == {"ts", "tool"}


def test_record_never_raises_on_unwritable_path(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("TURKIYE_VERI_USAGE_LOG", str(blocker / "sub" / "usage.jsonl"))
    monkeypatch.setenv("TURKIYE_VERI_USAGE_SALT", "s")
    usage.record("x")
    usage.record_session(protocol_version=None, client="c", client_version=None, ident={"v": "abc"})
    assert usage.summary()["total_calls"] == 0


def test_in_memory_salt_when_dir_unwritable(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.delenv("TURKIYE_VERI_USAGE_SALT", raising=False)
    monkeypatch.setenv("TURKIYE_VERI_USAGE_LOG", str(blocker / "usage.jsonl"))
    usage._salt_cache.clear()
    a = usage.anon_id("192.0.2.1")
    assert a == usage.anon_id("192.0.2.1")  # stable within the process


# ---------------------------------------------------------------------------
# X-Forwarded-For / client name
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "headers, peer, expected",
    [
        ({"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}, "10.1.1.1", "203.0.113.7"),
        ({"x-forwarded-for": " 203.0.113.7 "}, None, "203.0.113.7"),
        ({"x-forwarded-for": "203.0.113.7:4711, 10.0.0.1"}, None, "203.0.113.7"),
        ({"x-forwarded-for": "2001:db8::1, 10.0.0.1"}, None, "2001:db8::1"),
        ({"x-forwarded-for": "[2001:db8::1]:443"}, None, "2001:db8::1"),
        ({"x-forwarded-for": "unknown, 10.0.0.1"}, "198.51.100.9", "198.51.100.9"),
        ({"x-forwarded-for": ""}, "198.51.100.9", "198.51.100.9"),
        ({}, "198.51.100.9", "198.51.100.9"),
        (None, None, None),
        ({}, "testclient", None),
    ],
)
def test_client_ip(headers, peer, expected):
    assert usage.client_ip(headers, peer) == expected


def test_identity_without_ip_or_key_is_empty():
    assert usage.identity({}, None) == {}
    assert set(usage.identity({"x-evds-api-key": "k"}, None)) == {"k"}


def test_client_info_sources():
    assert usage.client_info("initialize", {"clientInfo": {"name": "claude-ai", "version": "0.1.0"}}, {}) == (
        "claude-ai",
        "0.1.0",
    )
    modern = {"_meta": {"io.modelcontextprotocol/clientInfo": {"name": "probe", "version": "9"}}}
    assert usage.client_info("server/discover", modern, {}) == ("probe", "9")
    assert usage.client_info("initialize", {}, {"user-agent": "python-httpx/0.28.1"}) == ("ua:python-httpx", None)
    assert usage.client_info("initialize", {}, {"User-Agent": "Mozilla/5.0 (Mac)"}) == ("ua:Mozilla", None)
    assert usage.client_info("initialize", None, None) == ("unknown", None)
    name, _ = usage.client_info("initialize", {"clientInfo": {"name": "x<script>\n" + "a" * 200}}, {})
    assert "<" not in name and "\n" not in name and len(name) <= 64


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_summary_aggregates(log):
    today = datetime.now(timezone.utc).date().isoformat()
    _write(
        log,
        [
            {"ts": _ts(30), "tool": "tuik_get_data"},  # old, pre-visitor line
            {"ts": _ts(20), "event": "session", "client": "claude-ai", "v": "old0000000000000"},
            {"ts": _ts(20), "tool": "tuik_get_data", "v": "old0000000000000"},
            {"ts": _ts(0), "event": "session", "client": "claude-code", "client_version": "2", "v": "aaaa", "k": "k1"},
            {"ts": _ts(0), "tool": "evds_get_data", "v": "aaaa", "k": "k1"},
            {"ts": _ts(0), "tool": "evds_get_data", "v": "aaaa", "k": "k1"},
            {"ts": _ts(0), "event": "session", "client": "claude-ai", "v": "bbbb"},
            {"ts": _ts(0), "event": "session", "client": "claude-ai", "v": "bbbb"},
            {"ts": _ts(0), "tool": "usage_stats", "v": "bbbb"},
            {"ts": _ts(0), "tool": "usage_stats"},  # stdio call
        ],
    )
    s = usage.summary(days_recent=7)
    assert s["total_calls"] == 6
    assert s["by_tool"] == {"tuik_get_data": 2, "evds_get_data": 2, "usage_stats": 2}
    assert s["calls_last_7_days"] == 4
    assert s["total_sessions"] == 4 and s["sessions_last_7_days"] == 3
    assert s["sessions_by_client"] == {"claude-ai": 3, "claude-code": 1}
    assert s["unique_visitors"] == 3
    assert s["unique_visitors_last_7_days"] == 2
    assert s["unique_visitors_by_day"] == {today: 2}
    assert s["unique_evds_key_holders"] == 1
    assert s["calls_per_visitor"] == {"visitors_with_calls": 3, "mean": 1.33, "max": 2}
    assert s["first_seen"] and s["last_seen"] and "tahmin" in s["note"]
    # aggregates only: no ids or hashes leak into the summary
    dumped = json.dumps(s)
    for ident in ("aaaa", "bbbb", "old0000000000000", "k1"):
        assert f'"{ident}"' not in dumped


def test_summary_per_day(log):
    d0 = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    d1 = d0 - timedelta(days=1)
    _write(
        log,
        [
            {"ts": d1.isoformat(), "tool": "a", "v": "x"},
            {"ts": d1.isoformat(), "tool": "a", "v": "y"},
            {"ts": d0.isoformat(), "tool": "a", "v": "x"},
        ],
    )
    s = usage.summary(days_recent=3)
    assert s["unique_visitors_by_day"] == {d1.date().isoformat(): 2, d0.date().isoformat(): 1}
    assert s["unique_visitors_last_3_days"] == 2


def test_summary_tolerates_corrupt_lines(log):
    _write(
        log,
        [
            '{"ts": "2026-01-01T00:00:00+00:00", "tool": "a"}',
            "not json at all",
            '{"ts": "2026-01-01T00:00:01+00:00", "tool": "b"',  # truncated write
            "[1, 2, 3]",
            '{"ts": "bad-date", "tool": "c"}',
            '{"event": "mystery"}',
            "",
        ],
    )
    with log.open("ab") as handle:
        handle.write(b"\xff\xfe garbage bytes\n")
    s = usage.summary()
    assert s["total_calls"] == 2
    assert s["by_tool"] == {"a": 1, "c": 1}
    assert s["skipped_lines"] == 5


def test_summary_empty(log):
    s = usage.summary()
    assert s["total_calls"] == 0 and s["total_sessions"] == 0 and s["unique_visitors"] == 0
    assert "calls_last_7_days" in s and s["note"].startswith("Henüz kayıt yok")


def test_middleware_logs_session_only_for_http(log):
    from types import SimpleNamespace

    from turkiye_veri_mcp import server

    assert server._usage_middleware in server.app.middleware

    async def call_next(ctx):
        usage.record("probe_tool")  # what _track does inside the handler
        return {"ok": True}

    params = {"clientInfo": {"name": "claude-code", "version": "2.1"}}
    stdio_ctx = SimpleNamespace(method="initialize", params=params, request=None, protocol_version="2025-11-25")
    request = SimpleNamespace(headers={"x-forwarded-for": "203.0.113.7"}, client=SimpleNamespace(host="10.0.0.1"))
    http_ctx = SimpleNamespace(method="initialize", params=params, request=request, protocol_version="2025-11-25")

    assert asyncio.run(server._usage_middleware(stdio_ctx, call_next)) == {"ok": True}
    asyncio.run(server._usage_middleware(http_ctx, call_next))
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e.get("event", "tool") for e in events] == ["tool", "tool", "session"]
    assert "v" not in events[0] and events[1]["v"] == events[2]["v"]
    assert events[2]["client"] == "claude-code" and events[2]["proto"] == "2025-11-25"
    assert usage._current.get() is None  # contextvar reset after the request


def test_middleware_propagates_handler_errors(log):
    from types import SimpleNamespace

    from turkiye_veri_mcp import server

    async def boom(ctx):
        raise RuntimeError("handler failed")

    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="198.51.100.1"))
    ctx = SimpleNamespace(method="initialize", params={}, request=request, protocol_version="2025-11-25")
    with pytest.raises(RuntimeError):
        asyncio.run(server._usage_middleware(ctx, boom))
    assert not log.exists() or "session" not in log.read_text()


# ---------------------------------------------------------------------------
# End to end: real server over HTTP, real MCP client
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 -- localhost only
        assert response.status == 200
        return json.loads(response.read().decode("utf-8"))


async def _connect(url: str, mode: str, xff: str, name: str, evds_key: str | None = None) -> None:
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
    from mcp_types import Implementation

    headers = {"X-Forwarded-For": xff}
    if evds_key:
        headers["X-Evds-Api-Key"] = evds_key
    async with create_mcp_http_client(headers=headers) as http_client:
        async with Client(
            streamable_http_client(url, http_client=http_client),
            mode=mode,
            client_info=Implementation(name=name, version="1.0"),
        ) as client:
            result = await client.call_tool("usage_stats", {"days_recent": 7})
            assert not result.is_error


def test_http_server_counts_sessions_and_visitors(tmp_path):
    log = tmp_path / "usage.jsonl"
    port = _free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO / "src"),
        "TURKIYE_VERI_USAGE_LOG": str(log),
        "TURKIYE_VERI_CACHE": str(tmp_path / "cache"),
    }
    env.pop("TURKIYE_VERI_USAGE_SALT", None)  # exercise the file-kept salt
    proc = subprocess.Popen(
        [sys.executable, "-m", "turkiye_veri_mcp.server", "--transport", "http", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while True:
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1).close()  # noqa: S310
                break
            except OSError:
                if proc.poll() is not None or time.time() > deadline:
                    raise AssertionError("server did not start: " + proc.stderr.read().decode(errors="replace"))
                time.sleep(0.2)

        async def scenario():
            # visitor A: handshake-era client (initialize + Mcp-Session-Id), twice
            await _connect(base + "/mcp", "legacy", "203.0.113.7, 10.0.0.1", "client-a", evds_key="SECRETKEY123")
            await _connect(base + "/mcp", "legacy", "203.0.113.7", "client-a")
            # visitor B: 2026-07-28 client (server/discover, no session id)
            await _connect(base + "/mcp", "auto", "198.51.100.23", "client-b")

        asyncio.run(scenario())

        stats = _get_json(base + "/stats")
        assert stats["total_sessions"] == 3
        assert stats["sessions_by_client"] == {"client-a": 2, "client-b": 1}
        assert stats["unique_visitors"] == 2
        assert stats["unique_visitors_last_7_days"] == 2
        assert sum(stats["unique_visitors_by_day"].values()) >= 2
        assert stats["unique_evds_key_holders"] == 1
        assert stats["total_calls"] == 3 and stats["by_tool"] == {"usage_stats": 3}
        assert stats["calls_per_visitor"]["visitors_with_calls"] == 2
        assert "note" in stats
        assert _get_json(base + "/stats?days=1")["calls_last_1_days"] == 3

        text = log.read_text()
        for secret in ("203.0.113.7", "198.51.100.23", "10.0.0.1", "127.0.0.1", "SECRETKEY123"):
            assert secret not in text
        events = [json.loads(line) for line in text.splitlines()]
        assert all("v" in e for e in events)
        protos = sorted(e["proto"] for e in events if e.get("event") == "session")
        assert protos[-1] == "2026-07-28" and protos[0] != "2026-07-28"
        assert (tmp_path / "usage.jsonl.salt").exists()
        # /stats exposes aggregates only
        dumped = json.dumps(stats)
        for e in events:
            assert e["v"] not in dumped
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
