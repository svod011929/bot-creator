"""Owner drafts, optimistic text conflicts and idempotent atomic settings replacement."""

import concurrent.futures
import json
import threading
from types import SimpleNamespace

import pytest

from child.files import write_setting
from child.presentation import PRESETS, TITLES, preset_values
from creator import presentation as domain
from creator import presentation_views as views
from creator.config import Config
from creator.db import DB
from creator.presentation import PresentationError, open_presentation
from creator.services import Ctx
from creator.supervisor import Supervisor
from tests.test_clicker_events import snapshot

OWNER, OTHER = 100, 200
TOKEN = "123456789:AAEexampleexampleexampleexample12345"


@pytest.fixture(params=["clicker", "refbot", "starbot"])
def env(tmp_path, request):
    cfg = Config(data_dir=tmp_path)
    db = DB(cfg.db_path)
    for uid in (OWNER, OTHER):
        db.upsert_user(uid, f"synthetic{uid}", f"Synthetic {uid}")
    draft = db.create_draft(OWNER, request.param, TOKEN, "fixture_bot", "Fixture")
    bid = db.materialize_draft(draft, 1234)
    sup = Supervisor(db, cfg, creator_username="creator_bot")
    path = sup.write_files(bid)
    (path / "settings.json").write_text("{}")
    ctx = Ctx(db, cfg, sup)
    w = SimpleNamespace(ctx=ctx, id=bid, path=path, template=request.param)
    w.open = lambda uid=OWNER: open_presentation(ctx, uid, bid)
    w.preset = next(iter(PRESETS[w.template]))
    yield w
    db.close()


def create(w):
    with w.open() as s:
        return s.begin(w.preset, 0)


def files(path):
    return {p.name: p.read_bytes() for p in path.iterdir() if p.is_file()}


def test_all_read_views_are_side_effect_free_and_do_not_create_child_database(env):
    w = env
    before, data = files(w.path), snapshot(w.ctx.db)
    with w.open() as s:
        views.home(s)
        views.presets(s)
        for tab in ("greeting", "section", "guide"):
            views.preview(s, tab)
        assert s.draft() is None
    assert files(w.path) == before
    assert snapshot(w.ctx.db) == data
    assert not (w.path / "bot.db").exists()


def test_draft_is_persistent_and_does_not_write_settings_until_apply(env):
    w = env
    before = files(w.path)
    d = create(w)
    with w.open() as s:
        new = s.edit("welcome", "My new greeting <b>&", d["version"])
    assert files(w.path) == before
    with w.open() as s:
        assert s.draft() == new
        assert s.draft()["values"]["welcome"] == "My new greeting <b>&"
        assert s.status(s.draft(), s.raw_settings()) == "draft"


def test_bundle_preserves_financial_channel_unknown_values_and_file_mode(env):
    w = env
    original = {
        "currency": "CUSTOM",
        "ref_reward": 123.45,
        "min_withdraw": 4321,
        "click_reward": 7,
        "required_channel": "@channel",
        "channel_url": "https://t.me/channel",
        "custom_extension": {"enabled": True, "keys": [1, 2]},
        "unknown_secret": "DO_NOT_DISPLAY",
    }
    path = w.path / "settings.json"
    path.write_text(json.dumps(original))
    path.chmod(0o640)
    d = create(w)
    with w.open() as s:
        before = s.draft()
        assert s.apply(d["version"])
        after = s.raw_settings()
        assert all(after[key] == value for key, value in original.items())
        assert {k: after[k] for k in s.keys} == preset_values(w.template, w.preset)
        assert s.draft() == before  # The file stamp is the acknowledgement; no second SQLite write is needed.
        assert s.status(s.draft(), after) == "applied"
        stamp = path.stat().st_mtime_ns
        assert not s.apply(d["version"])
        assert path.stat().st_mtime_ns == stamp
    assert path.stat().st_mode & 0o777 == 0o640


def test_conflicting_text_is_preserved_until_explicit_refresh_and_reconfirmation(env):
    w = env
    d = create(w)
    write_setting(w.path, "welcome", "Changed in existing editor")
    with w.open() as s:
        with pytest.raises(PresentationError):
            s.apply(d["version"])
        assert s.raw_settings()["welcome"] == "Changed in existing editor"
        updated = s.refresh(d["version"])
        with pytest.raises(PresentationError):
            s.apply(d["version"])
        assert updated["values"] == d["values"]
        assert s.apply(updated["version"])


def test_unrelated_financial_update_is_not_lost_or_reported_as_text_conflict(env):
    w = env
    d = create(w)
    write_setting(w.path, "ref_reward", 765)
    with w.open() as s:
        assert s.apply(d["version"])
        assert s.raw_settings()["ref_reward"] == 765


def test_already_applied_draft_cannot_overwrite_later_native_text_edits(env):
    w = env
    d = create(w)
    with w.open() as s:
        s.apply(d["version"])
    write_setting(w.path, "welcome", "Native edit after apply")
    with w.open() as s:
        assert s.status(s.draft(), s.raw_settings()) == "changed_after_apply"
        for action in (
            lambda: s.apply(d["version"]),
            lambda: s.edit("welcome", "old", d["version"]),
            lambda: s.refresh(d["version"]),
        ):
            with pytest.raises(PresentationError):
                action()
        fresh = s.begin("current", d["version"])
        assert fresh["values"]["welcome"] == "Native edit after apply"
        assert fresh["version"] != d["version"]
        with pytest.raises(PresentationError):
            s.discard(d["version"])


def test_failure_before_file_replace_preserves_existing_settings_and_draft(env, monkeypatch):
    w = env
    d = create(w)
    before = (w.path / "settings.json").read_bytes()

    def fail(*args):
        raise OSError("synthetic")

    monkeypatch.setattr("child.files.os.replace", fail)
    with pytest.raises(PresentationError):
        with w.open() as s:
            s.apply(d["version"])
    assert (w.path / "settings.json").read_bytes() == before
    assert not list(w.path.glob(".tmp-*.json"))
    with w.open() as s:
        assert s.draft() == d


def test_retry_recognizes_success_after_replace_even_if_caller_failed_before_acknowledgement(env, monkeypatch):
    w = env
    d = create(w)
    original = domain._save_settings

    def interrupted(path, data):
        original(path, data)
        raise OSError("synthetic post-replace interruption")

    monkeypatch.setattr(domain, "_save_settings", interrupted)
    with pytest.raises(PresentationError):
        with w.open() as s:
            s.apply(d["version"])
    monkeypatch.setattr(domain, "_save_settings", original)
    with w.open() as s:
        assert s.status(s.draft(), s.raw_settings()) == "applied"
        stamp = (w.path / "settings.json").stat().st_mtime_ns
        assert not s.apply(d["version"])
        assert (w.path / "settings.json").stat().st_mtime_ns == stamp


def test_two_creator_connections_apply_once(env):
    w = env
    d = create(w)
    other_db = DB(w.ctx.cfg.db_path)
    other = Ctx(other_db, w.ctx.cfg, Supervisor(other_db, w.ctx.cfg))
    barrier = threading.Barrier(2)

    def apply(ctx):
        barrier.wait(timeout=5)
        with open_presentation(ctx, OWNER, w.id) as s:
            return s.apply(d["version"])

    try:
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(apply, ctx) for ctx in (w.ctx, other)]
            assert sum(job.result(timeout=10) for job in jobs) == 1
    finally:
        other_db.close()


def test_stale_versions_and_forged_financial_fields_cannot_mutate_draft(env):
    w = env
    d = create(w)
    with w.open() as s:
        for key in ("currency", "ref_reward", "required_channel", "unknown"):
            with pytest.raises(PresentationError):
                s.edit(key, "5", d["version"])
        newer = s.edit("welcome", "Reviewed", d["version"])
        for action in (lambda: s.apply(d["version"]), lambda: s.discard(d["version"]), lambda: s.begin(w.preset, d["version"])):
            with pytest.raises(PresentationError):
                action()
        assert s.draft() == newer


@pytest.mark.parametrize(
    "key,value",
    [
        ("welcome", ""),
        ("welcome", "x" * 1201),
        ("launch_text", "x" * 701),
        ("welcome", TOKEN),
        ("launch_scene", "not-a-preset"),
        ("welcome", ["not text"]),
    ],
)
def test_invalid_values_leave_draft_and_file_unchanged(env, key, value):
    w = env
    d = create(w)
    before = (w.path / "settings.json").read_bytes()
    with w.open() as s:
        with pytest.raises(PresentationError):
            s.edit(key, value, d["version"])
        assert s.draft() == d
    assert (w.path / "settings.json").read_bytes() == before


def test_imported_secret_is_redacted_and_not_copied_into_draft_table(env):
    w = env
    write_setting(w.path, "welcome", "prefix " + TOKEN + " 🌟" * 800)
    with w.open() as s:
        text, _ = views.preview(s, "greeting")
        assert TOKEN not in text and TOKEN[:20] not in text
        s.begin(w.preset, 0)
        row = w.ctx.db.one("SELECT * FROM presentation_drafts")
        assert TOKEN not in str(tuple(row))


@pytest.mark.parametrize("problem", ["config", "settings_symlink", "lock_symlink", "corrupt", "nonfinite", "overflow", "array"])
def test_bad_or_unsafe_source_files_fail_closed_without_writing(env, problem, tmp_path):
    w = env
    d = create(w)
    protected = tmp_path / "protected.json"
    protected.write_text('{"valuable":"untouched"}')
    if problem == "config":
        cfg = json.loads((w.path / "config.json").read_text())
        (w.path / "config.json").write_text(json.dumps({**cfg, "username": "other_bot"}))
    elif problem == "settings_symlink":
        (w.path / "settings.json").unlink()
        (w.path / "settings.json").symlink_to(protected)
    elif problem == "lock_symlink":
        (w.path / ".settings.lock").symlink_to(protected)
    else:
        (w.path / "settings.json").write_text(
            {"corrupt": "broken", "nonfinite": '{"ref_reward":NaN}', "overflow": '{"ref_reward":1e999}', "array": "[]"}[problem]
        )
    with pytest.raises(PresentationError):
        with w.open() as s:
            s.apply(d["version"])
    assert protected.read_text() == '{"valuable":"untouched"}'


def test_new_owner_cannot_see_previous_owner_draft_and_metadata_change_invalidates_input(env):
    w = env
    d = create(w)
    w.ctx.db.transfer_bot(w.id, OTHER)
    with pytest.raises(PresentationError):
        with w.open():
            pass
    cfg = json.loads((w.path / "config.json").read_text())
    (w.path / "config.json").write_text(json.dumps({**cfg, "owner_id": OTHER}))
    with w.open(OTHER) as s:
        assert s.draft() is None
        s.begin(w.preset, 0)
    assert w.ctx.db.scalar("SELECT COUNT(*) FROM presentation_drafts") == 2
    assert w.ctx.db.scalar("SELECT version FROM presentation_drafts WHERE owner_id=?", OWNER) == d["version"]


def test_missing_settings_use_defaults_but_are_created_only_on_explicit_apply(env):
    w = env
    path = w.path / "settings.json"
    path.unlink()
    with w.open() as s:
        views.home(s)
        assert not path.exists()
        d = s.begin(w.preset, 0)
        assert not path.exists()
        s.apply(d["version"])
    assert path.exists()


def test_settings_size_limit_is_checked_before_replace(env):
    w = env
    path = w.path / "settings.json"
    path.write_text(json.dumps({"extension": "x" * (domain.MAX_JSON - 50)}))
    before = path.read_bytes()
    d = create(w)
    with pytest.raises(PresentationError):
        with w.open() as s:
            s.apply(d["version"])
    assert path.read_bytes() == before


def test_section_intro_matches_the_native_six_hundred_character_limit(env):
    w = env
    d = create(w)
    key = TITLES[w.template].replace("_title", "_intro")
    with w.open() as s:
        updated = s.edit(key, "🌟" * 600, d["version"])
        with pytest.raises(PresentationError):
            s.edit(key, "🌟" * 601, updated["version"])
