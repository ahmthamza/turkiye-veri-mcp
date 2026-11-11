"""Lightweight, privacy-respecting usage logging.

Two kinds of lines go into one append-only JSONL file:

* tool calls: ``{"ts", "tool"}`` plus, over HTTP, ``"v"`` (anonymous visitor
  id) and, if the caller sent an X-Evds-Api-Key header, ``"k"`` (anonymous
  key-holder id);
* sessions (HTTP only): ``{"ts", "event": "session", "proto", "client",
  "client_version"?, "v", "k"?}`` -- one per MCP ``initialize`` (handshake
  protocol versions) or ``server/discover`` (2026-07-28, which has no
  initialize and no session id).

``v`` is a truncated HMAC-SHA256 of the client IP (first X-Forwarded-For hop
when present, else the socket peer) under a secret salt; ``k`` the same over
the EVDS key. The raw IP, the EVDS key, request arguments and responses are
never written anywhere. Without the salt the ids cannot be turned back into
an IP or key.

The request identity is handed from the MCP middleware to ``record`` through
a contextvar, so stdio (no HTTP request) simply logs ``{"ts", "tool"}`` as
before.

Storage: default location is the same cache directory as the EVDS series
index; override with TURKIYE_VERI_USAGE_LOG. Salt: TURKIYE_VERI_USAGE_SALT,
else a random salt kept next to the log (``<log>.salt``), else in memory.

Caveat for hosted deployments: Render's free tier disk is ephemeral, so the
log (and a file-kept salt) resets on redeploy or restart.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

_ID_HEX = 16  # 64 bits: ample to tell visitors apart, too short to be a useful fingerprint
_write_lock = threading.Lock()
_salt_lock = threading.Lock()
_salt_cache: dict[str, bytes] = {}

# Identity of the HTTP request being served: {"v": ..., "k": ...} or None (stdio).
_current: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "turkiye_veri_usage_identity", default=None
)

SESSION_METHODS = ("initialize", "server/discover")
_CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"


def _log_path() -> Path:
    root = os.environ.get("TURKIYE_VERI_USAGE_LOG")
    if root:
        return Path(root)
    cache_root = os.environ.get("TURKIYE_VERI_CACHE") or (Path.home() / ".cache" / "turkiye-veri-mcp")
    return Path(cache_root) / "usage.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Anonymous ids
# ---------------------------------------------------------------------------

def _salt() -> bytes:
    env = os.environ.get("TURKIYE_VERI_USAGE_SALT")
    if env:
        return env.encode("utf-8")
    salt_path = _log_path().with_name(_log_path().name + ".salt")
    cache_key = str(salt_path)
    with _salt_lock:
        cached = _salt_cache.get(cache_key)
        if cached is not None:
            return cached
        salt: bytes | None = None
        try:
            salt_path.parent.mkdir(parents=True, exist_ok=True)
            text = secrets.token_hex(32)
            try:  # O_EXCL: two processes starting together agree on one salt
                fd = os.open(salt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(text + "\n")
                salt = text.encode("ascii")
            except FileExistsError:
                existing = salt_path.read_text(encoding="ascii").strip()
                if len(existing) >= 32:
                    salt = existing.encode("ascii")
        except (OSError, ValueError):
            salt = None
        if salt is None:  # unwritable directory: stable for this process only
            salt = secrets.token_hex(32).encode("ascii")
        _salt_cache[cache_key] = salt
        return salt


def anon_id(value: str, *, purpose: str = "ip", salt: bytes | None = None) -> str:
    """Truncated HMAC-SHA256 of ``value``; ``purpose`` separates IP and key ids."""
    key = salt if salt is not None else _salt()
    digest = hmac.new(key, f"{purpose}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:_ID_HEX]


def _normalize_ip(raw: str | None) -> str | None:
    """'1.2.3.4', '1.2.3.4:5678', '[::1]:80', '::1' -> canonical IP; else None."""
    if not raw:
        return None
    text = raw.strip().strip('"')
    if text.startswith("["):  # [v6]:port
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:  # v4:port
        text = text.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def client_ip(headers: Mapping[str, str] | None, peer: str | None) -> str | None:
    """First X-Forwarded-For hop when it is a valid IP, else the socket peer."""
    forwarded = None
    if headers:
        for name, value in headers.items():
            if name.lower() == "x-forwarded-for":
                forwarded = value
                break
    if forwarded:
        ip = _normalize_ip(forwarded.split(",", 1)[0])
        if ip:
            return ip
    return _normalize_ip(peer)


def _header(headers: Mapping[str, str] | None, wanted: str) -> str | None:
    if not headers:
        return None
    for name, value in headers.items():
        if name.lower() == wanted:
            return value
    return None


def identity(headers: Mapping[str, str] | None, peer: str | None) -> dict[str, str]:
    """Anonymous ids for one HTTP request: {"v"?, "k"?}. Never contains raw values."""
    out: dict[str, str] = {}
    ip = client_ip(headers, peer)
    if ip:
        out["v"] = anon_id(ip, purpose="ip")
    key = (_header(headers, "x-evds-api-key") or "").strip()
    if key:
        out["k"] = anon_id(key, purpose="evds")
    return out


# ---------------------------------------------------------------------------
# Client name
# ---------------------------------------------------------------------------

_SAFE = re.compile(r"[^A-Za-z0-9._@+\- ]")


def _clean(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = _SAFE.sub("", value).strip()[:limit]
    return text or None


def client_info(
    method: str, params: Mapping[str, Any] | None, headers: Mapping[str, str] | None
) -> tuple[str, str | None]:
    """(name, version) from initialize's clientInfo or a 2026-07-28 request's
    _meta clientInfo; falls back to 'ua:<User-Agent family>' or 'unknown'."""
    info: Any = None
    if isinstance(params, Mapping):
        if method == "initialize":
            info = params.get("clientInfo")
        if not isinstance(info, Mapping):
            meta = params.get("_meta")
            if isinstance(meta, Mapping):
                info = meta.get(_CLIENT_INFO_META_KEY)
    if isinstance(info, Mapping):
        name = _clean(info.get("name"), 64)
        if name:
            return name, _clean(info.get("version"), 32)
    ua = _header(headers, "user-agent") or ""
    family = _clean(re.split(r"[/\s(]", ua.strip(), maxsplit=1)[0] if ua.strip() else "", 40)
    return (f"ua:{family}" if family else "unknown"), None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _append(event: dict[str, Any]) -> None:
    """Append one line. Never raises -- logging must not break a request."""
    try:
        line = json.dumps(event, ensure_ascii=False)
        path = _log_path()
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:  # noqa: BLE001 -- best-effort; full disk / read-only mount
        pass


def set_current(ident: dict[str, str] | None) -> contextvars.Token:
    return _current.set(ident or None)


def reset_current(token: contextvars.Token) -> None:
    try:
        _current.reset(token)
    except Exception:  # noqa: BLE001
        pass


def record(tool_name: str) -> None:
    """Log one tool call (+ anonymous ids of the current HTTP request, if any)."""
    event: dict[str, Any] = {"ts": _now_iso(), "tool": tool_name}
    try:
        ident = _current.get()
        if ident:
            event.update({k: v for k, v in ident.items() if k in ("v", "k")})
    except Exception:  # noqa: BLE001
        pass
    _append(event)


def record_session(
    *, protocol_version: str | None, client: str, client_version: str | None, ident: Mapping[str, str]
) -> None:
    event: dict[str, Any] = {"ts": _now_iso(), "event": "session", "client": client}
    if client_version:
        event["client_version"] = client_version
    if protocol_version:
        event["proto"] = _clean(protocol_version, 16)
    event.update({k: v for k, v in ident.items() if k in ("v", "k")})
    _append(event)


def http_request_parts(request: Any) -> tuple[Mapping[str, str] | None, str | None]:
    """(headers, peer host) from a Starlette Request, or (None, None)."""
    if request is None:
        return None, None
    headers = getattr(request, "headers", None)
    client = getattr(request, "client", None)
    return headers, getattr(client, "host", None)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

_CALLS_UNATTRIBUTED = "birden_cok_veya_bilinmiyor"

_NOTE = (
    "Bu sayılar bir tahmindir, kişi sayımı değildir. 'Ziyaretçi', istemci IP "
    "adresinin tuzlanmış ve kısaltılmış özetidir (ham IP saklanmaz). claude.ai "
    "web ve Claude Desktop bağlayıcıları bu sunucuya Anthropic'in sunucuları "
    "üzerinden ulaştığı için çok sayıda kişi birkaç IP'yi paylaşabilir (eksik "
    "sayım); tek bir kişi farklı ağlardan bağlanırsa birden fazla sayılır "
    "(fazla sayım). 'Oturum', bir MCP initialize (veya 2026-07-28 sürümünde "
    "server/discover) isteğidir; bir istemci tek kişi için birçok oturum "
    "açabilir. Kendi bilgisayarında stdio ile (yerel kurulum) çalıştıranlar bu "
    "sunucuya hiç ulaşmaz ve burada görünmez. Render ücretsiz katmanında bu "
    "kayıt yeniden başlatma veya yeniden dağıtımda sıfırlanır; sayılar yalnızca "
    "first_seen tarihinden bu yana geçerlidir. unique_visitors_by_client "
    "(ve son N gün karşılığı) aynı uyarılarla bir tahmindir: her istemci adı "
    "için, o adla oturum açan farklı ziyaretçi sayısıdır; iki istemci kullanan "
    "(veya aynı IP'yi paylaşan) bir ziyaretçi ikisinde de sayılır, bu yüzden "
    "toplamı unique_visitors'ı aşabilir. calls_by_client, araç çağrılarını "
    "ziyaretçinin oturumlarındaki istemciye bağlar; yalnızca ziyaretçi günlükte "
    "tam olarak bir istemci adıyla görünüyorsa atanır. Birden çok istemcili, "
    "hiç oturum kaydı olmayan veya ziyaretçi kimliği olmayan (stdio) çağrılar "
    f"'{_CALLS_UNATTRIBUTED}' altında toplanır."
)


def summary(days_recent: int = 7) -> dict[str, Any]:
    """Aggregate the log. Contains counts only -- no ids, no hashes."""
    days_recent = max(1, int(days_recent))
    recent_key = f"calls_last_{days_recent}_days"
    path = _log_path()

    total_calls = recent_calls = sessions = recent_sessions = skipped = 0
    by_tool: Counter[str] = Counter()
    by_client: Counter[str] = Counter()
    visitors: set[str] = set()
    recent_visitors: set[str] = set()
    key_holders: set[str] = set()
    per_day: defaultdict[str, set[str]] = defaultdict(set)
    calls_per_visitor: Counter[str] = Counter()
    visitors_by_client: defaultdict[str, set[str]] = defaultdict(set)
    recent_visitors_by_client: defaultdict[str, set[str]] = defaultdict(set)
    clients_of_visitor: defaultdict[str, set[str]] = defaultdict(set)
    first_ts: str | None = None
    last_ts: str | None = None
    cutoff = time.time() - days_recent * 86400

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        handle = None
    if handle is not None:
        with handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    skipped += 1
                    continue
                if not isinstance(event, dict):
                    skipped += 1
                    continue
                ts = event.get("ts") if isinstance(event.get("ts"), str) else None
                when: datetime | None = None
                if ts:
                    try:
                        when = datetime.fromisoformat(ts)
                        if when.tzinfo is None:
                            when = when.replace(tzinfo=timezone.utc)
                    except ValueError:
                        when = None
                if when is not None:
                    first_ts = first_ts or ts
                    last_ts = ts
                is_recent = when is not None and when.timestamp() >= cutoff
                visitor = event.get("v") if isinstance(event.get("v"), str) else None
                kind = event.get("event", "tool")

                if kind == "session":
                    sessions += 1
                    recent_sessions += is_recent
                    client = str(event.get("client") or "unknown")
                    by_client[client] += 1
                    visitors_by_client[client]  # every client gets a key, even without ids
                    if visitor:
                        visitors_by_client[client].add(visitor)
                        clients_of_visitor[visitor].add(client)
                        if is_recent:
                            recent_visitors_by_client[client].add(visitor)
                elif kind == "tool":
                    total_calls += 1
                    recent_calls += is_recent
                    by_tool[str(event.get("tool", "?"))] += 1
                    if visitor:
                        calls_per_visitor[visitor] += 1
                else:
                    skipped += 1
                    continue

                if visitor:
                    visitors.add(visitor)
                    if is_recent:
                        recent_visitors.add(visitor)
                        per_day[when.astimezone(timezone.utc).date().isoformat()].add(visitor)
                if isinstance(event.get("k"), str):
                    key_holders.add(event["k"])

    counts = list(calls_per_visitor.values())

    # Tool calls -> client, only when the visitor maps to exactly one client
    # name across all session events in the log; everything else (several
    # clients, no session event, no visitor id at all) goes to one bucket.
    calls_by_client: Counter[str] = Counter()
    for visitor, n in calls_per_visitor.items():
        clients = clients_of_visitor.get(visitor)
        if clients is not None and len(clients) == 1:
            calls_by_client[next(iter(clients))] += n
        else:
            calls_by_client[_CALLS_UNATTRIBUTED] += n
    without_visitor = total_calls - sum(calls_per_visitor.values())
    if without_visitor:
        calls_by_client[_CALLS_UNATTRIBUTED] += without_visitor

    def _by_size(groups: Mapping[str, set[str]]) -> dict[str, int]:
        return dict(sorted(((c, len(ids)) for c, ids in groups.items()), key=lambda kv: (-kv[1], kv[0])))
    return {
        # existing keys (unchanged meaning)
        "total_calls": total_calls,
        "by_tool": dict(by_tool.most_common()),
        "first_seen": first_ts,
        "last_seen": last_ts,
        recent_key: recent_calls,
        # sessions and visitors (HTTP only)
        "total_sessions": sessions,
        f"sessions_last_{days_recent}_days": recent_sessions,
        "sessions_by_client": dict(by_client.most_common()),
        "unique_visitors": len(visitors),
        f"unique_visitors_last_{days_recent}_days": len(recent_visitors),
        "unique_visitors_by_day": {day: len(per_day[day]) for day in sorted(per_day)},
        "unique_visitors_by_client": _by_size(visitors_by_client),
        f"unique_visitors_by_client_last_{days_recent}_days": _by_size(recent_visitors_by_client),
        "calls_by_client": dict(calls_by_client.most_common()),
        "unique_evds_key_holders": len(key_holders),
        "calls_per_visitor": {
            "visitors_with_calls": len(counts),
            "mean": round(sum(counts) / len(counts), 2) if counts else 0,
            "max": max(counts) if counts else 0,
        },
        "skipped_lines": skipped,
        "note": _NOTE if (total_calls or sessions) else "Henüz kayıt yok. " + _NOTE,
    }
