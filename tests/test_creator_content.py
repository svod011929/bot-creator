"""Creator/child content consistency on independent synthetic SQLite files."""

import concurrent.futures
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from child.core import ChildApp
from child.db import ChildDB
from child.files import read_config, write_config
from child.referral_club import SEED, ClubError
from child.star_catalog import starter_cards
from child.star_journey import JourneyError
from creator import content_views as views
from creator.config import Config
from creator.content import CODES, ContentError, decode, encode, open_content
from creator.db import DB
from creator.services import Ctx
from creator.supervisor import Supervisor

OWNER, OTHER = 100, 200


@pytest.fixture(params=["refbot", "starbot"])
def content_env(tmp_path, request):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    db.upsert_user(OWNER, "owner", "Synthetic owner")
    db.upsert_user(OTHER, "other", "Other owner")
    template = request.param
    draft = db.create_draft(OWNER, template, "123456:FAKE_CONTENT_TOKEN_NOT_REAL_0123456789", "fixture_bot", "Fixture")
    bid = db.materialize_draft(draft, 1234)
    sup = Supervisor(db, cfg)
    path = sup.write_files(bid)
    app = ChildApp(path)
    app.db.upsert_user(OWNER, "owner", "Synthetic owner", None)
    app.db.add_balance(OWNER, 17.5)
    ctx = Ctx(db, cfg, sup)
    w = SimpleNamespace(ctx=ctx, app=app, path=path, id=bid, template=template, code=CODES[template])
    w.open = lambda **kw: open_content(ctx, OWNER, bid, w.code, **kw)
    w.native = app.club if template == "refbot" else app.sky
    yield w
    app.db.conn.close()
    db.close()


def data(w):
    return {**SEED, "days": 7} if w.code == "r" else {**starter_cards()[0], "starts": "сегодня", "days": 7}


def complete_form(w, content, *, title="Creator draft"):
    content.form_begin()
    values = {**data(w), "title": title}
    for field in content.fields:
        content.form_input(values[field], content.form_get()["version"])
    return content.form_get()


def snapshot(db):
    return {
        r[0]: [tuple(v) for v in db.all(f'SELECT * FROM "{r[0]}" ORDER BY rowid')]
        for r in db.all("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    }


def test_read_views_do_not_initialize_forms_or_award_users(content_env):
    w = content_env
    before, creator_before = snapshot(w.app.db), snapshot(w.ctx.db)
    for _ in range(2):
        with w.open() as c:
            views.home(c)
            views.catalog(c, 0)
            views.calendar(c, 0)
            views.owner_card(c, 1)
            views.preview(c, 1)
            assert c.form_get() is None
    assert snapshot(w.app.db) == before
    assert snapshot(w.ctx.db) == creator_before


def test_creator_form_persists_and_does_not_touch_child_form(content_env):
    w = content_env
    if w.code == "r":
        w.native.wizard_begin(OWNER)
        native_before = w.native.wizard_get(OWNER)
    else:
        w.native.editor_begin(OWNER)
        native_before = w.native.editor_get(OWNER)
    with w.open(write=True) as c:
        d = c.form_begin()
        c.form_input(data(w)[c.fields[0]], d["version"])
        form = c.form_get()
    with w.open(write=True) as c:
        assert c.form_begin() == form
        for field in c.fields[1:]:
            c.form_input(data(w)[field], c.form_get()["version"])
        cid = c.form_save(c.form_get()["version"])
        assert c.form_get() is None
        assert c.owner_data(cid)["status"] == "draft"
    native_after = w.native.wizard_get(OWNER) if w.code == "r" else w.native.editor_get(OWNER)
    assert native_after == native_before
    assert w.app.db.user(OWNER)["balance"] == 17.5


def test_child_and_creator_edits_share_revision_guards(content_env):
    w = content_env
    cid = w.native.create_draft(OWNER, data(w))
    with w.open(write=True) as c:
        d = c.form_begin(cid, 1)
        c.form_field("title", d["version"])
        c.form_input("Creator version", c.form_get()["version"])
    w.native.update_draft(OWNER, cid, 1, {**data(w), "title": "Child version"})
    with w.open(write=True) as c:
        with pytest.raises((ClubError, JourneyError)):
            c.form_save(c.form_get()["version"])
        assert c.owner_data(cid)["title"] == "Child version"
        assert c.form_get()["data"]["title"] == "Creator version"


def test_save_and_form_removal_are_one_child_transaction(content_env):
    w = content_env
    table = "ref_campaigns" if w.code == "r" else "star_cards"
    with w.open(write=True) as c:
        d = complete_form(w, c)
        count = c.db.scalar(f"SELECT COUNT(*) FROM {table}")
        c.db.exec("CREATE TRIGGER fault BEFORE DELETE ON creator_content_forms BEGIN SELECT RAISE(ABORT,'injected'); END")
        with pytest.raises(sqlite3.IntegrityError):
            c.form_save(d["version"])
        assert c.db.scalar(f"SELECT COUNT(*) FROM {table}") == count
        assert c.form_get()["step"] == "confirm"
        c.db.exec("DROP TRIGGER fault")
        cid = c.form_save(d["version"])
        with pytest.raises(ContentError):
            c.form_save(d["version"])
        assert c.owner_data(cid)["status"] == "draft"


def test_old_form_buttons_cannot_edit_or_discard_new_form(content_env):
    w = content_env
    with w.open(write=True) as c:
        first = c.form_begin()
        c.form_discard(first["version"])
        second = c.form_begin()
        assert first["version"] != second["version"]
        with pytest.raises(ContentError):
            c.form_discard(first["version"])
        with pytest.raises(ContentError):
            c.form_input(data(w)[c.fields[0]], first["version"])
        assert c.form_get() == second


def test_owner_transfer_requires_matching_current_config_and_hides_old_forms(content_env):
    w = content_env
    with w.open(write=True) as c:
        complete_form(w, c, title="Previous owner's form")
    w.ctx.db.transfer_bot(w.id, OTHER)
    with pytest.raises(ContentError):
        with w.open():
            pass
    with pytest.raises(ContentError):
        with open_content(w.ctx, OTHER, w.id, w.code):
            pass
    cfg = read_config(w.path)
    write_config(w.path, {**cfg, "owner_id": OTHER})
    with open_content(w.ctx, OTHER, w.id, w.code) as c:
        assert c.form_get() is None
        assert c.owner_data(1)


@pytest.mark.parametrize("problem", ["deleted", "wrong_template", "bad_config", "symlink_db", "missing_config"])
def test_invalid_identity_or_path_fails_closed(content_env, tmp_path, problem):
    w = content_env
    if problem == "deleted":
        w.ctx.db.delete_bot(w.id)
    elif problem == "wrong_template":
        w.ctx.db.update_bot(w.id, template="starbot" if w.code == "r" else "refbot")
    elif problem == "bad_config":
        (w.path / "config.json").write_text("broken")
    elif problem == "missing_config":
        (w.path / "config.json").unlink()
    else:
        destination = tmp_path / "unrelated.db"
        destination.write_text("do not modify")
        w.app.db.conn.close()
        (w.path / "bot.db").unlink()
        (w.path / "bot.db").symlink_to(destination)
    with pytest.raises(ContentError):
        with w.open(write=True):
            pass


def test_missing_catalog_requires_explicit_prepare(content_env):
    w = content_env
    w.app.db.conn.close()
    for suffix in ("", "-wal", "-shm"):
        (w.path / ("bot.db" + suffix)).unlink(missing_ok=True)
    with w.open() as c:
        assert c.engine is None
        assert "не подготовлен" in views.home(c)[0]
    assert not (w.path / "bot.db").exists()
    with w.open(write=True, prepare=True) as c:
        assert c.engine is not None
        expected = 1 if w.code == "r" else 24
        table = "ref_campaigns" if w.code == "r" else "star_cards"
        assert c.db.scalar(f"SELECT COUNT(*) FROM {table}") == expected


def test_concurrent_child_and_creator_updates_only_one_revision(content_env):
    w = content_env
    cid = w.native.create_draft(OWNER, data(w))
    with w.open(write=True) as c:
        d = c.form_begin(cid, 1)
        c.form_field("title", d["version"])
        c.form_input("Creator wins", c.form_get()["version"])
    barrier = threading.Barrier(2)

    def save_creator():
        barrier.wait(timeout=5)
        try:
            with w.open(write=True) as c:
                c.form_save(c.form_get()["version"])
            return True
        except (ClubError, JourneyError):
            return False

    def save_child():
        barrier.wait(timeout=5)
        try:
            w.native.update_draft(OWNER, cid, 1, {**data(w), "title": "Child wins"})
            return True
        except (ClubError, JourneyError):
            return False

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(save_creator), pool.submit(save_child)]
        assert sum(job.result(timeout=10) for job in jobs) == 1
    with w.open() as c:
        assert c.owner_data(cid)["revision"] == 2


def test_existing_database_open_never_creates_or_migrates(tmp_path):
    missing = tmp_path / "missing.db"
    for writable in (False, True):
        with pytest.raises(sqlite3.OperationalError):
            ChildDB.open_existing(missing, writable=writable)
        assert not missing.exists()
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sentinel(value TEXT)")
    conn.close()
    readonly = ChildDB.open_existing(path)
    try:
        assert [r[0] for r in readonly.all("SELECT name FROM sqlite_master WHERE type='table'")] == ["sentinel"]
        with pytest.raises(sqlite3.OperationalError):
            readonly.exec("INSERT INTO sentinel VALUES('forbidden')")
    finally:
        readonly.conn.close()


@pytest.mark.parametrize("number", [0, 1, 35, 36, 10**12, 2**63 - 1])
def test_compact_callback_identifiers_are_lossless(number):
    assert decode(encode(number)) == number


def test_redaction_happens_before_view_truncation(content_env):
    w = content_env
    token = w.ctx.db.bot(w.id)["token"]
    values = {**data(w), "title": token[:30] + token[30:]}
    cid = w.native.create_draft(OWNER, values)
    with w.open() as c:
        for text, kb in (views.owner_card(c, cid), views.preview(c, cid), views.catalog(c, 0)):
            assert token not in text and token[:20] not in text
            assert all(token[:20] not in b.text for row in kb.inline_keyboard for b in row)
        before = snapshot(c.db)
        views.preview(c, cid)
        assert snapshot(c.db) == before


def test_imported_secret_is_redacted_and_creator_publish_rejects_unreviewed_keys(content_env):
    from creator.handlers.content import perform

    w = content_env
    token = w.ctx.db.bot(w.id)["token"]
    cid = w.native.create_draft(OWNER, {**data(w), "title": token})
    with w.open(write=True) as c:
        with pytest.raises(ContentError):
            perform(c, "publish", [encode(cid), encode(1)])
        assert c.owner_data(cid)["status"] == "draft"
        form = c.form_begin(cid, 1)
        assert token not in str(form)
        c.form_save(form["version"])
        assert c.owner_data(cid)["title"] == "[скрыто]"
        perform(c, "publish", [encode(cid), encode(2)])
        assert c.owner_data(cid)["status"] == "active"
