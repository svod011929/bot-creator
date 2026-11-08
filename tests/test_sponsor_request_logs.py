"""Безопасные INFO-логи исходящих sponsor-запросов дочернего бота (без токенов и PII)."""
from __future__ import annotations

import logging
import re

import pytest
from aiogram.types import User
from aiohttp import web
from aiohttp.test_utils import TestServer

from child.db import ChildDB
from child.piarflow import PiarFlowGate
from child.tgrass import TGrassGate

TG_KEY = "tg-secret-key-DO-NOT-LOG"
PF_KEY = "pf-secret-key-DO-NOT-LOG"
SECRET_NAME = "IvanSecret"
SECRET_USER = "ivan_secret_user"
OFFER_LINK = "https://t.me/secret_channel_xyz?start=leak"
SPONSOR_LINK = "https://t.me/secret_sponsor?start=leak"


def _user(uid: int = 4242) -> User:
    return User(id=uid, is_bot=False, first_name=SECRET_NAME, username=SECRET_USER)


def _joined(caplog) -> str:
    return "\n".join(
        r.getMessage() for r in caplog.records
        if r.name.startswith(("child", "creator"))
    )


def _assert_no_secrets(text: str) -> None:
    for needle in (TG_KEY, PF_KEY, SECRET_NAME, SECRET_USER, "4242", OFFER_LINK, SPONSOR_LINK,
                   "secret_channel", "secret_sponsor", "Auth:", "Bearer ", "tg_login", "tg_user_id"):
        assert needle not in text, f"leaked {needle!r} in: {text}"
    assert not re.search(r"https?://\S+\?", text), f"query URL leaked: {text}"


class FakeTG:
    def __init__(self):
        self.mode = "tasks"  # tasks | empty | http500 | hang
        self.subscribed: set[int] = set()

    def app(self):
        a = web.Application()
        a.router.add_post("/offers", self.offers)
        a.router.add_post("/check", self.check)
        return a

    async def offers(self, req):
        assert req.headers.get("Auth") == TG_KEY
        if self.mode == "http500":
            return web.json_response({"detail": "boom token=" + TG_KEY, "user": SECRET_USER}, status=500)
        if self.mode == "empty":
            return web.json_response({"status": "no_offers", "offers": []})
        body = await req.json()
        _uid = int(body.get("tg_user_id") or 0)
        sub = 7 in self.subscribed
        return web.json_response({
            "status": "ok" if sub else "not_ok",
            "offers": [{
                "offer_id": 7, "name": SECRET_NAME, "link": OFFER_LINK, "type": "channel",
                "subscribed": sub,
            }],
        })

    async def check(self, req):
        body = await req.json()
        ok = body["offer_id"] in self.subscribed
        return web.json_response({"status": "subscribed" if ok else "not_subscribed", "is_fake": False})


class FakePF:
    def __init__(self):
        self.mode = "tasks"
        self.done: dict[str, str] = {}

    def app(self):
        a = web.Application()
        a.router.add_post("/sponsors", self.sponsors)
        a.router.add_post("/sponsors/check", self.check)
        return a

    async def sponsors(self, req):
        assert req.headers.get("Authorization") == f"Bearer {PF_KEY}"
        if self.mode == "http500":
            return web.json_response({"status": "error", "message": "boom " + PF_KEY}, status=500)
        if self.mode == "empty":
            return web.json_response({"status": "ok", "sponsors": []})
        return web.json_response({"status": "ok", "sponsors": [
            {"link": SPONSOR_LINK, "status": self.done.get(SPONSOR_LINK, "unsubscribed"), "price": 1},
        ]})

    async def check(self, req):
        body = await req.json()
        return web.json_response({"status": "ok", "sponsors": [
            {"link": link, "status": self.done.get(link, "unsubscribed")} for link in body["links"]
        ]})


@pytest.fixture
async def tg_api():
    api = FakeTG()
    server = TestServer(api.app())
    await server.start_server()
    yield api, str(server.make_url("")).rstrip("/")
    await server.close()


@pytest.fixture
async def pf_api():
    api = FakePF()
    server = TestServer(api.app())
    await server.start_server()
    yield api, str(server.make_url("")).rstrip("/")
    await server.close()


@pytest.fixture
def db(tmp_path):
    return ChildDB(tmp_path / "bot.db")


@pytest.mark.asyncio
async def test_tgrass_logs_success_empty_and_http_error(tg_api, db, caplog):
    api, url = tg_api
    gate = TGrassGate(TG_KEY, db, url)
    caplog.set_level(logging.INFO)

    api.mode = "tasks"
    kb = await gate.check(_user())
    assert kb is not None
    text = _joined(caplog)
    assert "network=tgrass" in text and "endpoint=/offers" in text
    assert "event=start" in text and "event=done" in text
    assert "http_status=200" in text and "tasks=1" in text
    assert "duration_ms=" in text
    _assert_no_secrets(text)

    # empty
    gate._pass_until.clear()
    caplog.clear()
    api.mode = "empty"
    assert await gate.check(_user(55)) is None
    text = _joined(caplog)
    assert "endpoint=/offers" in text and "http_status=200" in text and "tasks=0" in text
    assert "result=no_tasks" in text or "skip=no_tasks" in text
    _assert_no_secrets(text)

    # http error — no response body / tokens
    gate._pass_until.clear()
    caplog.clear()
    api.mode = "http500"
    assert await gate.check(_user(66)) is None
    text = _joined(caplog)
    assert "endpoint=/offers" in text and "http_status=500" in text
    assert "error=http" in text
    _assert_no_secrets(text)
    await gate.close()


@pytest.mark.asyncio
async def test_tgrass_logs_check_and_cache_skip(tg_api, db, caplog):
    api, url = tg_api
    gate = TGrassGate(TG_KEY, db, url)
    caplog.set_level(logging.INFO)
    u = _user(77)
    await gate.check(u)
    # mark shown then subscribed → /check
    api.subscribed.add(7)
    caplog.clear()
    await gate.check(u, force=True)
    text = _joined(caplog)
    assert "endpoint=/check" in text and "event=done" in text and "http_status=200" in text
    _assert_no_secrets(text)

    caplog.clear()
    assert await gate.check(u) is None  # cache
    text = _joined(caplog)
    assert "skip=cache" in text
    assert "endpoint=" not in text or "event=start" not in text
    _assert_no_secrets(text)
    await gate.close()


@pytest.mark.asyncio
async def test_piarflow_logs_success_empty_http_and_check(pf_api, db, caplog):
    api, url = pf_api
    gate = PiarFlowGate(PF_KEY, db, url)
    caplog.set_level(logging.INFO)

    kb = await gate.check(_user())
    assert kb is not None
    text = _joined(caplog)
    assert "network=piarflow" in text and "endpoint=/sponsors" in text
    assert "event=start" in text and "event=done" in text
    assert "http_status=200" in text and "tasks=1" in text
    assert "statuses=unsubscribed:1" in text
    _assert_no_secrets(text)

    gate._pass_until.clear()
    caplog.clear()
    api.mode = "empty"
    assert await gate.check(_user(88)) is None
    text = _joined(caplog)
    assert "tasks=0" in text and ("result=no_tasks" in text or "skip=no_tasks" in text)
    _assert_no_secrets(text)

    gate._pass_until.clear()
    caplog.clear()
    api.mode = "http500"
    assert await gate.check(_user(89)) is None
    text = _joined(caplog)
    assert "http_status=500" in text and "error=http" in text
    _assert_no_secrets(text)

    # restore tasks, show, then check
    api.mode = "tasks"
    gate._pass_until.clear()
    await gate.check(_user(90))
    api.done[SPONSOR_LINK] = "subscribed"
    caplog.clear()
    assert await gate.check(_user(90), force=True) is None
    text = _joined(caplog)
    assert "endpoint=/sponsors/check" in text and "http_status=200" in text
    assert "statuses=subscribed:1" in text
    _assert_no_secrets(text)
    await gate.close()


@pytest.mark.asyncio
async def test_piarflow_network_error_safe(db, caplog):
    gate = PiarFlowGate(PF_KEY, db, "http://127.0.0.1:1")  # closed port
    gate.timeout = __import__("aiohttp").ClientTimeout(total=0.2)
    caplog.set_level(logging.INFO)
    assert await gate.check(_user()) is None
    text = _joined(caplog)
    assert "network=piarflow" in text and "endpoint=/sponsors" in text
    assert "error=network" in text or "error=timeout" in text
    _assert_no_secrets(text)
    await gate.close()


@pytest.mark.asyncio
async def test_core_owner_skip_logged(tmp_path, caplog):
    from child.core import ChildApp
    from child.files import write_config

    write_config(tmp_path, {
        "token": "1:TEST", "template": "clicker", "owner_id": 4242, "username": "kid",
        "tgrass_key": TG_KEY, "tgrass_base_url": "http://127.0.0.1:1",
    })
    app = ChildApp(tmp_path)
    caplog.set_level(logging.INFO)
    assert await app.tgrass_gate(_user(4242)) is None
    text = _joined(caplog)
    assert "skip=owner" in text
    _assert_no_secrets(text)
