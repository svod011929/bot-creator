"""Integer minor-unit conversion and the reversible child-DB money migration."""
from __future__ import annotations

from pathlib import Path

import pytest

from child.db import ChildDB, fmt
from child.money import from_minor, to_minor


def test_to_minor_and_fmt_keep_display():
    assert to_minor(10) == 1000
    assert to_minor("10.5") == 1050
    assert to_minor(10.5) == 1050
    assert to_minor(0.005) == 1  # half-up from 0.5 minor? 0.005*100=0.5 -> 1
    assert to_minor(0.004) == 0
    assert fmt(1000) == "10"
    assert fmt(1050) == "10.5"
    assert fmt(1005) == "10.05"
    assert fmt(0) == "0"
    assert fmt(to_minor(10.5)) == "10.5"
    assert from_minor(1050) == __import__("decimal").Decimal("10.5")


def test_money_migration_is_idempotent_and_reversible(tmp_path: Path):
    path = tmp_path / "bot.db"
    raw = ChildDB(path)
    # Simulate a pre-migration database: store major units and clear the flag.
    raw.upsert_user(1, "a", "A", None)
    raw.upsert_user(2, "b", "B", None)
    raw.exec("UPDATE users SET balance=? WHERE id=1", 10.5)
    raw.exec("UPDATE users SET balance=? WHERE id=2", 3.333)
    raw.exec(
        "INSERT INTO tasks(title,url,channel,reward,active) VALUES('t','https://t.me/x','',?,1)",
        1.25,
    )
    raw._set_meta("money_unit", "major")
    before = raw.money_sums()
    expected_balance = to_minor(10.5) + to_minor(3.333)
    report = raw.migrate_money_to_minor()
    assert report["converted"] is True and report["ok"] is True
    assert raw.money_unit() == "minor"
    assert raw.user(1)["balance"] == 1050
    assert raw.user(2)["balance"] == to_minor(3.333)
    assert raw.money_sums()["users.balance"] == expected_balance
    assert abs(before["users.balance"] * 100 - expected_balance) <= 1  # within one minor unit of rounding
    rounded = [item for item in report["rounded"] if item["column"] == "balance"]
    assert rounded  # 3.333 is not exact
    again = raw.migrate_money_to_minor()
    assert again["already"] is True and again["converted"] is False
    assert raw.user(1)["balance"] == 1050
    revert = raw.revert_money_to_major()
    assert revert["reverted"] is True and raw.money_unit() == "major"
    assert raw.user(1)["balance"] == pytest.approx(10.5)
    # Re-applying the forward migration restores the same integers.
    raw.migrate_money_to_minor()
    assert raw.user(1)["balance"] == 1050


def test_new_database_is_already_minor(tmp_path: Path):
    db = ChildDB(tmp_path / "fresh.db")
    assert db.money_unit() == "minor"
    report = db.migrate_money_to_minor()
    assert report["already"] is True


def test_migrate_tree_converts_each_bot_and_is_idempotent(tmp_path: Path):
    from child.money_migrate import migrate_tree

    first = tmp_path / "1"
    second = tmp_path / "2"
    first.mkdir()
    second.mkdir()
    a = ChildDB(first / "bot.db")
    a.upsert_user(1, "a", "A", None)
    a.exec("UPDATE users SET balance=?", 2.5)
    a._set_meta("money_unit", "major")
    a.conn.close()
    b = ChildDB(second / "bot.db")
    assert b.money_unit() == "minor"
    b.conn.close()
    report = migrate_tree(tmp_path)
    assert report["ok"] is True and report["total"] == 2
    assert report["converted"] == 1 and report["already"] == 1
    again = migrate_tree(tmp_path)
    assert again["ok"] is True and again["already"] == 2 and again["converted"] == 0
    db = ChildDB(first / "bot.db")
    assert db.user(1)["balance"] == to_minor(2.5)
    db.conn.close()
