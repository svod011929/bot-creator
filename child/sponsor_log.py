"""Структурированные логи sponsor-запросов без токенов, ссылок и персональных данных."""
from __future__ import annotations

from collections import Counter
from typing import Iterable


def status_counts(statuses: Iterable[str]) -> str:
    c = Counter(str(s or "unknown") for s in statuses)
    return ",".join(f"{k}:{v}" for k, v in sorted(c.items())) if c else ""


def log_start(log, network: str, endpoint: str) -> None:
    log.info("sponsor_req network=%s endpoint=%s event=start", network, endpoint)


def log_done(
    log,
    network: str,
    endpoint: str,
    *,
    http_status: int,
    duration_ms: int,
    tasks: int | None = None,
    statuses: str | None = None,
    result: str | None = None,
    error: str | None = None,
) -> None:
    parts = [
        f"sponsor_req network={network}",
        f"endpoint={endpoint}",
        "event=done",
        f"http_status={http_status}",
        f"duration_ms={duration_ms}",
    ]
    if tasks is not None:
        parts.append(f"tasks={tasks}")
    if statuses:
        parts.append(f"statuses={statuses}")
    if result:
        parts.append(f"result={result}")
    if error:
        parts.append(f"error={error}")
    log.info("%s", " ".join(parts))


def log_skip(log, network: str, reason: str) -> None:
    log.info("sponsor_req network=%s event=skip skip=%s", network, reason)


def classify_exc(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in name or "timeout" in msg:
        return "timeout"
    if isinstance(exc, ValueError):
        return "bad_json"
    return "network"
