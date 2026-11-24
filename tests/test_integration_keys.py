"""Admin integration keys: masking, access, no leaks, child isolation."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from aiogram.methods import DeleteMessage

from creator.config import Config
from creator.supervisor import CHILD_ENV_KEYS
from tests.conftest import cb_update, msg_update
from tests.test_creator_flow import ADMIN, OWNER, env  # noqa: F401

SECRET = "example-admin-secret-key-zz9k"
SECRET2 = "example-admin-secret-key-ww8m"
HUB_LEGACY = "example-legacy-hub-key-abcd"


def action(session, suffix):
    for m in reversed(session.requests):
        kb = getattr(m, "reply_markup", None)
        for row in getattr(kb, "inline_keyboard", []) or []:
            for b in row:
                if b.callback_data and suffix in b.callback_data:
                    return b.callback_data
    raise AssertionError(f"Button not found: {suffix}")



def last_kb(session):
    for m in reversed(session.requests):
        kb = getattr(m, "reply_markup", None)
        if kb is not None and getattr(kb, "inline_keyboard", None):
            return [b.callback_data for row in kb.inline_keyboard for b in row]
    return []

def dump_db(ctx) -> str:
    return "\n".join(ctx.db.conn.iterdump())


def test_mask_and_status_hide_full_secret():
    from creator.integration_keys import mask_tail
    assert mask_tail("") == ""
    assert mask_tail("short") == ""
    assert mask_tail("shortk") == ""
    assert mask_tail("shortkey") == "…tkey"
    assert mask_tail(SECRET) == "…" + SECRET[-4:]
    assert SECRET not in mask_tail(SECRET)


def test_store_precedence_backup_permissions_and_env_lock(tmp_path):
    from creator.integration_keys import (
        KeyErrorSafe,
        has_admin_file,
        remove,
        resolve,
        revision,
        save,
        value,
    )
    cfg = Config(data_dir=tmp_path)
    assert resolve(cfg, "mgram_hub").origin == "unset"
    assert value(cfg, "mgram_hub") == ""
    legacy = tmp_path / "mgram_hub.json"
    legacy.write_text(json.dumps({"api_key": HUB_LEGACY}), encoding="utf-8")
    st = resolve(cfg, "mgram_hub")
    assert st.origin == "file" and st.key == HUB_LEGACY
    assert st.mask == "…" + HUB_LEGACY[-4:]
    assert HUB_LEGACY not in repr(st)
    old = revision(cfg, st.spec)
    save(cfg, "mgram_hub", SECRET, old)
    st = resolve(cfg, "mgram_hub")
    assert st.origin == "admin" and st.key == SECRET
    path = tmp_path / "integration_keys" / "mgram_hub.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert has_admin_file(cfg, st.spec)
    # second save keeps a 0600 backup of the previous file
    save(cfg, "mgram_hub", SECRET2, revision(cfg, st.spec))
    bak = path.with_name(path.name + ".bak")
    assert bak.is_file() and bak.stat().st_mode & 0o777 == 0o600
    assert SECRET in bak.read_text(encoding="utf-8")
    assert resolve(cfg, "mgram_hub").key == SECRET2
    # stale revision is rejected and current value stays
    with pytest.raises(KeyErrorSafe):
        save(cfg, "mgram_hub", SECRET, old)
    assert resolve(cfg, "mgram_hub").key == SECRET2
    cfg.mgram_hub_api_key = "example-env-hub-key-env1"
    assert resolve(cfg, "mgram_hub").origin == "env"
    with pytest.raises(KeyErrorSafe):
        save(cfg, "mgram_hub", SECRET, revision(cfg, st.spec))
    with pytest.raises(KeyErrorSafe):
        remove(cfg, "mgram_hub", revision(cfg, st.spec))
    cfg.mgram_hub_api_key = ""
    remove(cfg, "mgram_hub", revision(cfg, st.spec))
    assert not path.exists()
    assert resolve(cfg, "mgram_hub").key == HUB_LEGACY
    assert resolve(cfg, "mgram_hub").origin == "file"
    assert bak.is_file()  # last admin value remains in backup only


def test_invalid_keys_and_readonly_specs(tmp_path):
    from creator.integration_keys import KeyErrorSafe, resolve, revision, save, spec
    cfg = Config(data_dir=tmp_path, lavepay_api_key="")
    item = spec("lavepay")
    for bad in ["", "bad key", "x" * 513, "42:TEST-TOKEN", "<evil>", "ключ"]:
        with pytest.raises(KeyErrorSafe):
            save(cfg, "lavepay", bad, revision(cfg, item))
    assert resolve(cfg, "lavepay").origin == "unset"
    for key_id in ("bot_token", "lavepay_wh", "payout"):
        item = spec(key_id)
        assert item.editable is False
        with pytest.raises(KeyErrorSafe):
            save(cfg, key_id, SECRET, "absent")


def test_payout_status_does_not_create_key(tmp_path):
    from creator.integration_keys import resolve
    from creator.secretbox import KEY_FILE_NAME
    cfg = Config(data_dir=tmp_path)
    st = resolve(cfg, "payout")
    assert st.origin == "unset" and not st.present
    assert not (tmp_path / KEY_FILE_NAME).exists()


def test_lavepay_and_mgram_enabled_see_admin_file(tmp_path):
    from creator.integration_keys import revision, save, spec
    from creator.mgram_tasks import load_hub_key
    cfg = Config(data_dir=tmp_path)
    assert cfg.lavepay_enabled is False
    assert cfg.mgram_enabled is False
    save(cfg, "lavepay", SECRET, revision(cfg, spec("lavepay")))
    save(cfg, "mgram_partner", SECRET2, revision(cfg, spec("mgram_partner")))
    save(cfg, "mgram_hub", HUB_LEGACY, revision(cfg, spec("mgram_hub")))
    assert cfg.lavepay_enabled is True
    assert cfg.mgram_enabled is True
    assert load_hub_key(cfg) == HUB_LEGACY
    # partner key must never be used as the hub key
    cfg2 = Config(data_dir=tmp_path / "other", mgram_partner_api_key=SECRET2)
    assert load_hub_key(cfg2) == ""


def test_child_env_excludes_all_integration_key_names():
    from creator.integration_keys import SPECS
    for item in SPECS:
        assert item.env_name not in CHILD_ENV_KEYS


async def test_admin_flow_masks_deletes_input_and_audits_without_secrets(env, caplog):  # noqa: F811
    from creator.integration_keys import resolve, value
    ctx, feed, session = env
    caplog.set_level(logging.DEBUG)
    await feed(msg_update(ADMIN, "/start"))
    await feed(msg_update(OWNER, "/start"))
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "a:g:settings"))
    assert any("Ключи интеграций" in (b.text or "") for m in session.requests
               for row in getattr(getattr(m, "reply_markup", None), "inline_keyboard", []) or [] for b in row)
    n = len(session.texts())
    await feed(cb_update(OWNER, "ik:home"))
    assert len(session.texts()) == n or "Ключи интеграций" not in session.texts()[-1]
    await feed(cb_update(ADMIN, "ik:home"))
    text = session.texts()[-1]
    assert "Ключи интеграций" in text
    assert "переменная окружения" in text
    assert "LavePay API: не задан" in text
    await feed(cb_update(ADMIN, "ik:v:lavepay"))
    await feed(cb_update(ADMIN, "ik:edit:lavepay"))
    assert "сквозного шифрования" in session.texts()[-1]
    await feed(msg_update(ADMIN, SECRET))
    assert session.of(DeleteMessage)
    assert value(ctx.cfg, "lavepay") == ""
    assert SECRET not in "\n".join(session.texts())
    assert SECRET not in repr(session.requests)
    assert SECRET not in repr(ctx.key_inputs)
    save_cb = action(session, "ik:save:lavepay:")
    assert len(save_cb.encode()) <= 64
    await feed(cb_update(OWNER, save_cb))
    assert value(ctx.cfg, "lavepay") == ""
    await feed(cb_update(ADMIN, save_cb))
    assert value(ctx.cfg, "lavepay") == SECRET
    assert resolve(ctx.cfg, "lavepay").origin == "admin"
    shown = "\n".join(session.texts())
    assert "…" + SECRET[-4:] in shown
    assert SECRET not in shown
    assert SECRET not in dump_db(ctx)
    assert SECRET not in caplog.text
    rows = ctx.db.admin_actions()
    assert any(r["action"] == "integration_key_save" and r["target"] == "lavepay" and r["admin_id"] == ADMIN
               for r in rows)
    assert all(SECRET not in (r["detail"] or "") and SECRET not in (r["target"] or "") for r in rows)
    # replace requires a new confirm; previous save button is stale
    before = value(ctx.cfg, "lavepay")
    await feed(cb_update(ADMIN, save_cb))
    assert value(ctx.cfg, "lavepay") == before
    # delete requires confirm
    await feed(cb_update(ADMIN, "ik:v:lavepay"))
    await feed(cb_update(ADMIN, "ik:remove:lavepay"))
    assert value(ctx.cfg, "lavepay") == SECRET
    drop = action(session, "ik:drop:lavepay:")
    await feed(cb_update(ADMIN, drop))
    assert value(ctx.cfg, "lavepay") == ""
    assert any(r["action"] == "integration_key_remove" and r["target"] == "lavepay" for r in ctx.db.admin_actions())
    assert SECRET not in dump_db(ctx)
    assert SECRET not in caplog.text


async def test_probe_failure_is_secret_safe_and_still_allows_save(env, monkeypatch):  # noqa: F811
    from creator.integration_keys import value

    class Boom:
        def __init__(self, *a, **k):
            pass

        async def me(self):
            raise RuntimeError("provider-error-with-secret-xyz")

        async def close(self):
            pass

    monkeypatch.setattr("creator.lavepay.LavePayClient", Boom)
    ctx, feed, session = env
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "ik:home"))
    await feed(cb_update(ADMIN, "ik:v:lavepay"))
    await feed(cb_update(ADMIN, "ik:edit:lavepay"))
    await feed(msg_update(ADMIN, SECRET))
    assert session.of(DeleteMessage)
    text = session.texts()[-1]
    assert "проверка: не удалась" in text
    assert "provider-error-with-secret-xyz" not in text
    assert SECRET not in text
    await feed(cb_update(ADMIN, action(session, "ik:save:lavepay:")))
    assert value(ctx.cfg, "lavepay") == SECRET


async def test_env_key_is_read_only_in_admin_ui(env):  # noqa: F811
    ctx, feed, session = env
    ctx.cfg.lavepay_api_key = SECRET
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "ik:home"))
    text = session.texts()[-1]
    assert "сервер (env)" in text
    assert "…" + SECRET[-4:] in text
    assert SECRET not in text
    await feed(cb_update(ADMIN, "ik:v:lavepay"))
    assert "изменение через бот недоступно" in session.texts()[-1].lower()
    # last keyboard should not offer edit/delete
    flat = last_kb(session)
    assert not any((x or "").startswith("ik:edit:lavepay") or (x or "").startswith("ik:remove:lavepay") for x in flat)
    await feed(cb_update(ADMIN, "ik:edit:lavepay"))
    path = Path(ctx.cfg.data_dir) / "integration_keys" / "lavepay.json"
    assert not path.exists()


async def test_readonly_keys_have_no_edit_and_payout_view_does_not_create(env):  # noqa: F811
    from creator.secretbox import KEY_FILE_NAME
    ctx, feed, session = env
    ctx.cfg.bot_token = "123456:FAKESECRET_q3r4s5t6u7v8w9x0y1z2"
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "ik:home"))
    await feed(cb_update(ADMIN, "ik:v:bot_token"))
    assert "только на сервере" in session.texts()[-1]
    last = last_kb(session)
    assert not any("ik:edit:bot_token" in (x or "") or "ik:remove:bot_token" in (x or "") for x in last)
    await feed(cb_update(ADMIN, "ik:v:payout"))
    assert "нечитаемыми" in session.texts()[-1]
    assert not (Path(ctx.cfg.data_dir) / KEY_FILE_NAME).exists()
    assert "123456:FAKE_CREATOR_TOKEN_NOT_REAL_0123456789" not in "\n".join(session.texts())


async def test_cancel_and_start_clear_pending_key(env):  # noqa: F811
    ctx, feed, session = env
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "ik:home"))
    await feed(cb_update(ADMIN, "ik:v:mgram_partner"))
    await feed(cb_update(ADMIN, "ik:edit:mgram_partner"))
    await feed(msg_update(ADMIN, SECRET))
    assert ctx.key_inputs
    await feed(msg_update(ADMIN, "/cancel"))
    assert not ctx.key_inputs
    await feed(cb_update(ADMIN, "ik:home"))
    await feed(cb_update(ADMIN, "ik:v:mgram_partner"))
    await feed(cb_update(ADMIN, "ik:edit:mgram_partner"))
    await feed(msg_update(ADMIN, SECRET))
    assert ctx.key_inputs
    await feed(msg_update(ADMIN, "/start"))
    assert not ctx.key_inputs
    from creator.integration_keys import value
    assert value(ctx.cfg, "mgram_partner") == ""


async def test_sponsor_hub_key_shares_source_store(env):  # noqa: F811
    from creator.integration_keys import value
    from creator.sponsor_sources import SourceStore
    ctx, feed, session = env
    await feed(msg_update(ADMIN, "/admin"))
    await feed(cb_update(ADMIN, "ik:home"))
    await feed(cb_update(ADMIN, "ik:v:tgrass_hub"))
    await feed(cb_update(ADMIN, "ik:edit:tgrass_hub"))
    await feed(msg_update(ADMIN, SECRET))
    await feed(cb_update(ADMIN, action(session, "ik:save:tgrass_hub:")))
    assert value(ctx.cfg, "tgrass_hub") == SECRET
    store = SourceStore(ctx.cfg, "tgrass")
    assert store.resolve().key == SECRET
    assert store.resolve().origin == "manual"
    if ctx.tgrass is not None:
        await ctx.tgrass.client.close()
