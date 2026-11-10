"""Failure injection and independent SQLite connections; no production data or API calls."""
import concurrent.futures
import sqlite3
import subprocess
import sys
import threading

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, GetChatMember

from child.db import ChildDB
from child.templates import Setting
from tests.conftest import cb_update, msg_update
from tests.test_child_bots import A, B, make


@pytest.fixture
def child_db(tmp_path):
    db = ChildDB(tmp_path / "atomic.db")
    db.upsert_user(A, None, "Synthetic", None)
    db.add_balance(A, 100)
    yield db
    db.conn.close()


def test_withdrawal_insert_failure_rolls_back_debit(child_db):
    child_db.exec("CREATE TRIGGER fault BEFORE INSERT ON withdrawals "
                  "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        child_db.create_withdrawal(A, 30, "synthetic", currency="points")
    assert child_db.user(A)["balance"] == 100
    assert child_db.scalar("SELECT COUNT(*) FROM withdrawals") == 0
    child_db.exec("DROP TRIGGER fault")
    assert child_db.create_withdrawal(A, 30, "synthetic", currency="points")
    assert child_db.user(A)["balance"] == 70


def test_rejection_refund_failure_leaves_request_pending(child_db):
    wid = child_db.create_withdrawal(A, 30, "synthetic", currency="points")
    child_db.exec("CREATE TRIGGER fault BEFORE UPDATE OF balance ON users WHEN NEW.balance>OLD.balance "
                  "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        child_db.resolve_withdrawal(wid, False)
    assert child_db.withdrawal(wid)["status"] == "pending"
    assert child_db.withdrawal(wid)["resolved_at"] is None
    assert child_db.user(A)["balance"] == 70
    child_db.exec("DROP TRIGGER fault")
    assert child_db.resolve_withdrawal(wid, False)
    assert child_db.resolve_withdrawal(wid, False) is None
    assert child_db.user(A)["balance"] == 100


def test_referral_failure_rolls_back_reward_marker(child_db):
    child_db.upsert_user(B, None, "Synthetic friend", A)
    child_db.exec("CREATE TRIGGER fault BEFORE UPDATE OF balance ON users "
                  "BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        child_db.reward_referrer_once(B, 5)
    assert child_db.user(B)["ref_rewarded"] == 0
    child_db.exec("DROP TRIGGER fault")
    assert child_db.reward_referrer_once(B, 5) == A
    assert child_db.reward_referrer_once(B, 5) is None
    assert child_db.user(A)["balance"] == 105


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "+inf", "-inf", "1e309"])
def test_non_finite_setting_is_rejected(raw):
    with pytest.raises(ValueError):
        Setting("x", "X", 1.0, "float").parse(raw)


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf"), -float("inf")])
def test_invalid_withdrawal_cannot_change_balance(child_db, amount):
    assert child_db.create_withdrawal(A, amount, "synthetic", currency="points") is None
    assert child_db.user(A)["balance"] == 100
    assert child_db.scalar("SELECT COUNT(*) FROM withdrawals") == 0


@pytest.mark.parametrize("failure", ["bad_request", "network", "retry_after"])
async def test_task_api_failure_never_rewards(tmp_path, bot, session, monkeypatch, failure):
    app, feed = make(tmp_path, "starbot", bot)
    await feed(msg_update(A, "/start"))
    app.db.exec("INSERT INTO tasks(title,url,channel,reward) VALUES(?,?,?,?)", "Synthetic", "https://t.me/test", "@test", 2)
    original = session.make_request

    async def request(bot, method, timeout=None):
        if isinstance(method, GetChatMember):
            exc = {"bad_request": lambda: TelegramBadRequest(method=method, message="injected"),
                   "network": lambda: TelegramNetworkError(method=method, message="injected"),
                   "retry_after": lambda: TelegramRetryAfter(method=method, message="injected", retry_after=1)}
            raise exc[failure]()
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", request)
    await feed(cb_update(A, "task:1"))
    assert app.db.user(A)["balance"] == 0
    assert app.db.scalar("SELECT COUNT(*) FROM task_done") == 0
    assert session.of(AnswerCallbackQuery)[-1].show_alert
    app.db.conn.close()


async def test_task_credit_failure_rolls_back_completion(tmp_path, bot):
    app, feed = make(tmp_path, "starbot", bot)
    await feed(msg_update(A, "/start"))
    app.db.exec("INSERT INTO tasks(title,url,reward) VALUES(?,?,?)", "Synthetic", "https://t.me/test", 2)
    app.db.exec("CREATE TRIGGER fault BEFORE UPDATE OF balance ON users BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await feed(cb_update(A, "task:1"))
    assert app.db.scalar("SELECT COUNT(*) FROM task_done") == 0
    app.db.exec("DROP TRIGGER fault")
    await feed(cb_update(A, "task:1"))
    await feed(cb_update(A, "task:1"))
    assert app.db.user(A)["balance"] == 2
    app.db.conn.close()


async def test_upgrade_failure_rolls_back_spend(tmp_path, bot):
    app, feed = make(tmp_path, "clicker", bot)
    await feed(msg_update(A, "/start"))
    app.db.add_balance(A, 200)
    app.db.exec("CREATE TRIGGER fault BEFORE UPDATE OF level ON users BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        await feed(cb_update(A, "upg"))
    assert app.db.user(A)["balance"] == 200 and app.db.user(A)["level"] == 1
    app.db.exec("DROP TRIGGER fault")
    await feed(cb_update(A, "upg"))
    assert app.db.user(A)["balance"] == 100 and app.db.user(A)["level"] == 2
    app.db.conn.close()


def test_competing_referrals_reward_once(tmp_path):
    path = tmp_path / "shared.db"
    first, second = ChildDB(path), ChildDB(path)
    first.upsert_user(A, None, "Synthetic", None)
    first.upsert_user(B, None, "Synthetic friend", A)
    barrier = threading.Barrier(2)

    def reward(db):
        barrier.wait(timeout=5)
        return db.reward_referrer_once(B, 5)

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(reward, db) for db in (first, second)]
        results = [j.result(timeout=10) for j in jobs]
    assert results.count(A) == 1
    assert first.user(A)["balance"] == 5
    first.conn.close()
    second.conn.close()


def test_process_exit_during_withdrawal_rolls_back(child_db, tmp_path):
    code = '''
import os, sys
from child.db import ChildDB
db = ChildDB(sys.argv[1])
db.conn.create_function("injected_exit", 0, lambda: os._exit(99))
db.exec("CREATE TEMP TRIGGER fault BEFORE INSERT ON withdrawals BEGIN SELECT injected_exit(); END")
db.create_withdrawal(10, 30, "synthetic", currency="points")
'''
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "atomic.db")], capture_output=True, timeout=15)
    assert result.returncode == 99, result.stderr.decode()
    assert child_db.user(A)["balance"] == 100
    assert child_db.scalar("SELECT COUNT(*) FROM withdrawals") == 0


@pytest.mark.parametrize("change", ["reward", "channel", "url", "active"])
async def test_changed_task_is_not_credited_after_old_verification(tmp_path, bot, session, monkeypatch, change):
    app, feed = make(tmp_path, "starbot", bot)
    await feed(msg_update(A, "/start"))
    app.db.exec("INSERT INTO tasks(title,url,channel,reward) VALUES(?,?,?,?)", "Synthetic", "https://t.me/test", "@test", 2)
    original = session.make_request

    async def request(bot, method, timeout=None):
        result = await original(bot, method, timeout)
        if isinstance(method, GetChatMember) and method.user_id == A:
            values = {"reward": 99, "channel": "@other", "url": "https://t.me/other", "active": 0}
            app.db.exec(f"UPDATE tasks SET {change}=? WHERE id=1", values[change])
        return result

    monkeypatch.setattr(session, "make_request", request)
    await feed(cb_update(A, "task:1"))
    assert app.db.user(A)["balance"] == 0
    assert app.db.scalar("SELECT COUNT(*) FROM task_done") == 0
    app.db.conn.close()


async def test_missing_bot_admin_rights_never_rewards(tmp_path, bot, session, monkeypatch):
    from types import SimpleNamespace

    app, feed = make(tmp_path, "starbot", bot)
    await feed(msg_update(A, "/start"))
    app.db.exec("INSERT INTO tasks(title,url,channel,reward) VALUES(?,?,?,?)", "Synthetic", "https://t.me/test", "@test", 2)
    original = session.make_request

    async def request(bot, method, timeout=None):
        if isinstance(method, GetChatMember) and method.user_id == bot.id:
            return SimpleNamespace(status="member")
        return await original(bot, method, timeout)

    monkeypatch.setattr(session, "make_request", request)
    await feed(cb_update(A, "task:1"))
    assert app.db.user(A)["balance"] == 0
    assert app.db.scalar("SELECT COUNT(*) FROM task_done") == 0
    app.db.conn.close()


@pytest.mark.parametrize("raw", ["task:", "task:no", "task:-1", "task:١", "task:" + "9" * 50])
async def test_malformed_task_buttons_are_safe(tmp_path, bot, raw):
    app, feed = make(tmp_path, "starbot", bot)
    await feed(msg_update(A, "/start"))
    await feed(cb_update(A, raw))
    assert app.db.user(A)["balance"] == 0
    app.db.conn.close()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
async def test_legacy_invalid_daily_bonus_does_not_corrupt_balance(tmp_path, bot, value):
    app, feed = make(tmp_path, "starbot", bot)
    await feed(msg_update(A, "/start"))
    app.set("daily_bonus", value)
    await feed(msg_update(A, "🎁 Ежедневный бонус"))
    assert app.db.user(A)["balance"] == app.db.user(A)["last_daily"] == 0
    app.db.conn.close()
