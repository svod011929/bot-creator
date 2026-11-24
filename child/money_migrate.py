"""Offline walk of child bot databases for the integer money migration."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from child.db import ChildDB


def iter_bot_dbs(bots_dir: Path) -> list[Path]:
    return sorted(path for path in Path(bots_dir).glob("*/bot.db") if path.is_file())


def migrate_tree(bots_dir: Path) -> dict[str, Any]:
    """Migrate every child bot.db under bots_dir.

    ChildDB applies the idempotent conversion on open and raises if sums do not
    match, leaving that file unchanged (transaction rollback).
    """
    reports: list[dict[str, Any]] = []
    failed: list[str] = []
    for path in iter_bot_dbs(bots_dir):
        try:
            db = ChildDB(path)
            try:
                report = getattr(db, "money_migration", None) or db.migrate_money_to_minor()
            finally:
                db.conn.close()
            report = dict(report)
            report["path"] = path.name
            report["bot_dir"] = path.parent.name
            reports.append(report)
        except Exception as exc:
            failed.append(f"{path.parent.name}: {type(exc).__name__}")
            reports.append({"path": path.name, "bot_dir": path.parent.name, "ok": False,
                            "error": type(exc).__name__})
    converted = sum(1 for r in reports if r.get("converted"))
    already = sum(1 for r in reports if r.get("already"))
    rounded = [r for r in reports if r.get("rounded")]
    return {
        "total": len(reports),
        "converted": converted,
        "already": already,
        "failed": failed,
        "rounded_bots": [r["bot_dir"] for r in rounded],
        "ok": not failed,
        "reports": reports,
    }
