"""Temporary SQLite only: compatible migration, snapshot metadata and bounded history reads."""
import concurrent.futures
import sqlite3

import pytest

from child.db import ChildDB


def legacy(path, extra=''):
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,'
                  'amount REAL NOT NULL,details TEXT,status TEXT NOT NULL DEFAULT \'pending\',created_at INTEGER NOT NULL'
                  + extra + ')')
        c.executemany('INSERT INTO withdrawals(user_id,amount,details,status,created_at) VALUES(?,?,?,?,?)',
                      [(10, 3.5, 'fake_secret', 'pending', 100), (20, 2, 'fake_other', 'paid', 101)])


def columns(db):
    return {r['name']: r for r in db.all('PRAGMA table_info(withdrawals)')}


def open_and_close(path):
    db = ChildDB(path)
    names = tuple(columns(db))
    db.conn.close()
    return names


@pytest.mark.parametrize('extra', ['', ',currency_at_request TEXT', ',resolved_at INTEGER'])
def test_old_partial_repeat_and_concurrent_migration(tmp_path, extra):
    path = tmp_path / 'old.db'
    legacy(path, extra)
    with concurrent.futures.ProcessPoolExecutor(3) as pool:
        results = list(pool.map(open_and_close, [str(path)] * 3))
    assert all('currency_at_request' in r and 'resolved_at' in r for r in results)
    db = ChildDB(path)
    col = columns(db)
    assert col['currency_at_request']['type'] == 'TEXT' and col['currency_at_request']['notnull'] == 0
    assert col['resolved_at']['type'] == 'INTEGER' and col['resolved_at']['notnull'] == 0
    rows = db.all('SELECT * FROM withdrawals ORDER BY id')
    assert len(rows) == 2 and rows[0]['amount'] == 3.5 and rows[1]['status'] == 'paid'
    assert all(r['currency_at_request'] is None and r['resolved_at'] is None for r in rows)
    db.conn.close()


@pytest.mark.parametrize('unit', ['🪙', '₽', '⭐', 'custom <unit>', ''])
def test_snapshot_first_resolution_and_single_refund(tmp_path, monkeypatch, unit):
    import child.db as mod
    db = ChildDB(tmp_path / 'new.db')
    db.upsert_user(10, None, 'fake', None)
    db.add_balance(10, 20)
    wid = db.create_withdrawal(10, 3.5, 'fake_secret', currency=unit)
    row = db.withdrawal(wid, uid=10)
    assert row['currency_at_request'] == unit and row['resolved_at'] is None
    monkeypatch.setattr(mod, 'now', lambda: 200)
    assert db.resolve_withdrawal(wid, False)
    monkeypatch.setattr(mod, 'now', lambda: 300)
    assert db.resolve_withdrawal(wid, True) is None
    assert db.resolve_withdrawal(wid, False) is None
    assert db.user(10)['balance'] == 20
    row = db.withdrawal(wid)
    assert row['resolved_at'] == 200 and row['status'] == 'rejected' and row['currency_at_request'] == unit
    db.conn.close()


def test_old_pending_resolved_now_does_not_invent_unit(tmp_path, monkeypatch):
    import child.db as mod
    path = tmp_path / 'old.db'
    legacy(path)
    db = ChildDB(path)
    monkeypatch.setattr(mod, 'now', lambda: 200)
    assert db.resolve_withdrawal(1, True)
    assert db.withdrawal(1)['resolved_at'] == 200
    assert db.withdrawal(1)['currency_at_request'] is None
    assert db.resolve_withdrawal(2, True) is None
    assert db.withdrawal(2)['resolved_at'] is None
    db.conn.close()


@pytest.mark.parametrize('count', [0, 1, 10, 11, 21])
def test_history_orders_filter_clamp_no_mutations(tmp_path, count):
    db = ChildDB(tmp_path / 'new.db')
    for _ in range(count):
        db.exec('INSERT INTO withdrawals(user_id,amount,details,status,created_at) VALUES(?,?,?,?,?)',
                10, 2, 'fake_secret', 'pending', 100)
    db.exec('INSERT INTO withdrawals(user_id,amount,details,status,created_at) VALUES(?,?,?,?,?)',
            20, 3, 'fake_other', 'paid', 101)
    before = [tuple(r) for r in db.all('SELECT * FROM withdrawals')]
    pending, page, total = db.withdrawal_page('pending', 0)
    assert [r['id'] for r in pending] == list(range(1, min(10, count) + 1))
    mine, page, total = db.withdrawal_page('mine', 0, uid=10)
    assert total == count and [r['id'] for r in mine] == list(range(count, max(0, count - 10), -1))
    last, page, total = db.withdrawal_page('mine', 100, uid=10)
    assert page == max(0, (count - 1) // 10)
    assert len(last) <= 10
    done, page, total = db.withdrawal_page('completed', 0)
    assert total == 1 and done[0]['user_id'] == 20
    assert db.withdrawal(count + 1, uid=10) is None
    assert [tuple(r) for r in db.all('SELECT * FROM withdrawals')] == before
    for scope, page in [('bogus', 0), ('mine', -1), ('mine', 10**30)]:
        with pytest.raises(ValueError):
            db.withdrawal_page(scope, page, uid=10)
    db.conn.close()


def test_failed_migration_rolls_back_and_preserves_legacy_data(tmp_path, monkeypatch):
    import child.db as mod
    path = tmp_path / 'old.db'
    legacy(path)
    original = sqlite3.connect
    def connection(*args, **kwargs):
        c = original(*args, **kwargs)
        alterations = []
        def auth(action, *rest):
            if action == sqlite3.SQLITE_ALTER_TABLE:
                alterations.append(1)
                if len(alterations) == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        c.set_authorizer(auth)
        return c
    monkeypatch.setattr(mod.sqlite3, 'connect', connection)
    with pytest.raises(sqlite3.DatabaseError):
        ChildDB(path)
    with original(path) as check:
        assert check.execute('SELECT COUNT(*) FROM withdrawals').fetchone()[0] == 2
        assert len(check.execute('PRAGMA table_info(withdrawals)').fetchall()) == 6


@pytest.mark.parametrize('approvals', [(False, False), (True, False)])
def test_competing_connections_only_one_resolution_refund(tmp_path, monkeypatch, approvals):
    import threading

    import child.db as mod
    path = tmp_path / 'shared.db'
    first, second = ChildDB(path), ChildDB(path)
    first.upsert_user(10, None, 'fake', None)
    first.add_balance(10, 20)
    wid = first.create_withdrawal(10, 3.5, 'fake', currency='⭐')
    monkeypatch.setattr(mod, 'now', lambda: 200)
    barrier = threading.Barrier(2)
    for db in (first, second):
        original = db.one
        def read(sql, *args, original=original):
            row = original(sql, *args)
            if sql == "SELECT * FROM withdrawals WHERE id=? AND status='pending'":
                barrier.wait(timeout=5)
            return row
        monkeypatch.setattr(db, 'one', read)
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(db.resolve_withdrawal, wid, approve)
                for db, approve in zip((first, second), approvals, strict=True)]
        results = [job.result(timeout=10) for job in jobs]
    assert sum(r is not None for r in results) == 1
    row = first.withdrawal(wid)
    assert row['resolved_at'] == 200 and row['currency_at_request'] == '⭐'
    assert first.user(10)['balance'] == (20 if row['status'] == 'rejected' else 16.5)
    first.conn.close()
    second.conn.close()


def test_migration_preserves_existing_user_records_and_balances(tmp_path):
    from child.db import SCHEMA
    path = tmp_path / 'old.db'
    legacy(path)
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA.split('CREATE TABLE IF NOT EXISTS withdrawals')[0])
        connection.execute('INSERT INTO users(id,username,full_name,referrer_id,balance,ref_rewarded,created_at) '
                           'VALUES(10,?,?,?,?,?,?)', ('fake', 'test', 20, 17.25, 1, 99))
        before = connection.execute('SELECT * FROM users').fetchall()
        old_withdrawals = connection.execute('SELECT * FROM withdrawals ORDER BY id').fetchall()
    db = ChildDB(path)
    assert [tuple(row) for row in db.all('SELECT * FROM users')] == before
    assert [tuple(row)[:6] for row in db.all('SELECT * FROM withdrawals ORDER BY id')] == old_withdrawals
    db.conn.close()


def test_completed_sort_uses_creation_not_resolution_and_empty_history(tmp_path):
    db = ChildDB(tmp_path / 'new.db')
    for created, resolved in [(100, 999), (200, 300), (200, 201)]:
        db.exec('INSERT INTO withdrawals(user_id,amount,status,created_at,resolved_at) VALUES(?,?,?,?,?)',
                10, 3, 'paid', created, resolved)
    rows, page, count = db.withdrawal_page('completed', 0)
    assert [row['id'] for row in rows] == [3, 2, 1] and count == 3 and page == 0
    rows, page, count = db.withdrawal_page('mine', 100, uid=11)
    assert rows == [] and page == count == 0
    db.conn.close()
