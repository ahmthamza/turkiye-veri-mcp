"""Lightweight, privacy-respecting usage logging.

Records only a timestamp and a tool name per call -- no IP, no client
identity, no request content. This is an activity counter, not a unique-user
counter: one person calling five tools in a session logs five lines. Good
enough to answer "is anyone using this, and how much", not "how many people".

Storage: an append-only JSONL file. Default location is the same cache
directory as the EVDS series index; override with TURKIYE_VERI_USAGE_LOG.

Caveat for hosted deployments: Render's free tier disk is ephemeral, so the
log resets on redeploy or restart. Fine for a rough sense of activity; not a
durable analytics store. A paid persistent disk (or an external log sink)
would fix that if it matters later.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _log_path() -> Path:
    root = os.environ.get("TURKIYE_VERI_USAGE_LOG")
    if root:
        return Path(root)
    cache_root = os.environ.get("TURKIYE_VERI_CACHE") or (Path.home() / ".cache" / "turkiye-veri-mcp")
    return Path(cache_root) / "usage.jsonl"


def record(tool_name: str) -> None:
    """Append one usage line. Never raises -- logging must not break a tool call."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "tool": tool_name},
            ensure_ascii=False,
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass  # best-effort; a full disk or read-only mount should not break tools


def summary(days_recent: int = 7) -> dict[str, Any]:
    """Read the log and summarize call counts. Returns zeros if none yet."""
    path = _log_path()
    if not path.exists():
        return {
            "total_calls": 0,
            "by_tool": {},
            "first_seen": None,
            "last_seen": None,
            f"calls_last_{days_recent}_days": 0,
            "note": "Henüz kayıt yok.",
        }

    now = time.time()
    cutoff = now - days_recent * 86400
    total = 0
    recent = 0
    by_tool: Counter[str] = Counter()
    first_ts: str | None = None
    last_ts: str | None = None

    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            total += 1
            by_tool[event.get("tool", "?")] += 1
            ts = event.get("ts")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
                try:
                    event_epoch = datetime.fromisoformat(ts).timestamp()
                    if event_epoch >= cutoff:
                        recent += 1
                except ValueError:
                    pass

    return {
        "total_calls": total,
        "by_tool": dict(by_tool.most_common()),
        "first_seen": first_ts,
        "last_seen": last_ts,
        f"calls_last_{days_recent}_days": recent,
        "note": (
            "Bu bir çağrı sayacıdır, kişi sayacı değil: tek kişi tek oturumda "
            "birden çok araç çağırırsa her biri ayrı satır olarak sayılır. "
            "Hosted (Render ücretsiz katman) ortamında bu dosya yeniden "
            "dağıtımda sıfırlanır."
        ),
    }
