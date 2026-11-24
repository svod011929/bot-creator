"""Constructor hub-key auto-save: no leaks, no child key, no overwrite."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from child.mgram_proxy import fetch_pending_result
from child.templates.microtasks import _mgram_owner_hint
from creator.config import Config
from creator.db import DB
from creator.integration_keys import resolve, value
from creator.mgram_factory import MgramError
from creator.mgram_hub import ensure_hub_key, persist_hub_key
from creator.mgram_tasks import load_hub_key, start_proxy, stop_proxy
from creator.supervisor import Supervisor
from tests.test_mgram_microtasks import HUB, PARTNER, USER, _micro_bot

CREATOR_TOKEN = "123456:FAKE_CREATOR_TOKEN_NOT_REAL_0123456789"
HUB_NEW = "example-registered-hub-key-z4k2"


def _cfg(tmp_path: Path, **kw) -> Config:
    return Config(data_dir=tmp_path, **kw)


@pytest.mark.asyncio
async def test_ensure_saves_registered_key_into_admin_store(tmp_path, caplog):
    cfg = _cfg(tmp_path, bot_token=CREATOR_TOKEN, mgram_partner_api_key=PARTNER)
    calls = []

    class Fake:
        async def register_bot(self, token, owner_id, referrer_id=None):
            calls.append({"token": token, "owner_id": owner_id, "referrer_id": referrer_id})
            return {"data": {"api_key": HUB_NEW, "bot_id": 9, "username": "creator"}}

        async def close(self):
            pass

    caplog.set_level(logging.DEBUG)
    status = await ensure_hub_key(cfg, bot_token=CREATOR_TOKEN, owner_id=42, factory=Fake())
    assert status == "saved"
    assert calls == [{"token": CREATOR_TOKEN, "owner_id": 42, "referrer_id": None}]
    assert value(cfg, "mgram_hub") == HUB_NEW
    assert load_hub_key(cfg) == HUB_NEW
    assert resolve(cfg, "mgram_hub").origin == "admin"
    path = tmp_path / "integration_keys" / "mgram_hub.json"
    assert path.is_file() and path.stat().st_mode & 0o777 == 0o600
    assert HUB_NEW not in caplog.text
    # second call is idempotent and does not hit the API again
    calls.clear()
    assert await ensure_hub_key(cfg, bot_token=CREATOR_TOKEN, owner_id=42, factory=Fake()) == "present"
    assert not calls
    assert value(cfg, "mgram_hub") == HUB_NEW


@pytest.mark.asyncio
async def test_ensure_skips_when_key_or_partner_missing(tmp_path):
    cfg = _cfg(tmp_path, bot_token=CREATOR_TOKEN)
    called = []

    class Fake:
        async def register_bot(self, *a, **k):
            called.append(1)
            raise AssertionError("must not register")

        async def close(self):
            pass

    assert await ensure_hub_key(cfg, bot_token=CREATOR_TOKEN, factory=Fake()) == "skipped_no_partner"
    cfg.mgram_partner_api_key = PARTNER
    cfg.bot_token = ""
    assert await ensure_hub_key(cfg, bot_token="", factory=Fake()) == "skipped_no_token"
    cfg.bot_token = CREATOR_TOKEN
    cfg.mgram_hub_api_key = HUB
    assert await ensure_hub_key(cfg, bot_token=CREATOR_TOKEN, factory=Fake()) == "present"
    assert value(cfg, "mgram_hub") == HUB
    assert not called


@pytest.mark.asyncio
async def test_ensure_does_not_overwrite_legacy_or_env(tmp_path):
    cfg = _cfg(tmp_path, bot_token=CREATOR_TOKEN, mgram_partner_api_key=PARTNER)
    (tmp_path / "mgram_hub.json").write_text('{"api_key": "%s"}' % HUB, encoding="utf-8")
    called = []

    class Fake:
        async def register_bot(self, *a, **k):
            called.append(1)
            return {"api_key": HUB_NEW}

        async def close(self):
            pass

    assert await ensure_hub_key(cfg, bot_token=CREATOR_TOKEN, factory=Fake()) == "present"
    assert value(cfg, "mgram_hub") == HUB
    assert not called
    assert persist_hub_key(cfg, HUB_NEW) == "present"
    assert value(cfg, "mgram_hub") == HUB


@pytest.mark.asyncio
async def test_ensure_logs_failure_without_secrets(tmp_path, caplog):
    cfg = _cfg(tmp_path, bot_token=CREATOR_TOKEN, mgram_partner_api_key=PARTNER)
    caplog.set_level(logging.DEBUG)

    class NoKey:
        async def register_bot(self, *a, **k):
            return {"data": {"bot_id": 1, "username": "creator"}}

        async def close(self):
            pass

    class Boom:
        async def register_bot(self, *a, **k):
            raise MgramError("secret-should-not-leak-xyz", code="already", status=409)

        async def close(self):
            pass

    assert await ensure_hub_key(cfg, bot_token=CREATOR_TOKEN, factory=NoKey()) == "failed_no_key"
    assert value(cfg, "mgram_hub") == ""
    assert await ensure_hub_key(cfg, bot_token=CREATOR_TOKEN, factory=Boom()) == "failed"
    assert "secret-should-not-leak-xyz" not in caplog.text
    assert "already" in caplog.text
    assert CREATOR_TOKEN not in caplog.text
    assert PARTNER not in caplog.text


@pytest.mark.asyncio
async def test_proxy_explains_missing_hub_key_and_child_never_gets_it(tmp_path):
    cfg = _cfg(tmp_path, mgram_partner_api_key=PARTNER)
    db = DB(cfg.db_path)
    bot_id = _micro_bot(db)
    runner = await start_proxy(cfg, db)
    try:
        Supervisor(db, cfg).write_files(bot_id)
        child_cfg = __import__("json").loads(
            (cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8")
        )
        assert "mgram_key" not in child_cfg
        listing = await fetch_pending_result(child_cfg, bot_id, USER)
        assert listing.tasks == []
        assert listing.reason == "not_configured"
        assert _mgram_owner_hint("not_configured")
        assert "Ключи интеграций" in _mgram_owner_hint("not_configured")
        assert _mgram_owner_hint("error")
        assert not _mgram_owner_hint("ok")
    finally:
        await stop_proxy(runner, cfg)


@pytest.mark.asyncio
async def test_child_refused_config_reports_reason(tmp_path):
    from child.mgram_proxy import fetch_pending_result as result
    cfg = _cfg(tmp_path, mgram_hub_api_key=HUB)
    db = DB(cfg.db_path)
    bot_id = _micro_bot(db)
    Supervisor(db, cfg).write_files(bot_id)
    import json
    child_cfg = json.loads((cfg.bots_dir / str(bot_id) / "config.json").read_text(encoding="utf-8"))
    child_cfg["mgram_key"] = HUB
    listing = await result(child_cfg, bot_id, USER)
    assert listing.tasks == []
    assert listing.reason == "refused"
