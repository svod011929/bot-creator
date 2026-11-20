"""Tests for the broadcast-monetization factory client (mocked, no network)."""
from __future__ import annotations

import pytest

from creator.mgram_factory import MgramError, MgramFactory
from creator.supervisor import CHILD_ENV_KEYS


@pytest.mark.asyncio
async def test_register_requires_key():
    f = MgramFactory("")
    with pytest.raises(MgramError, match="не настроен"):
        await f.register_bot("123:ABC", 1)


def test_enabled():
    assert MgramFactory("").enabled is False
    assert MgramFactory("   ").enabled is False
    assert MgramFactory("key").enabled is True


def test_partner_key_is_not_given_to_child_processes():
    assert "MGRAM_PARTNER_API_KEY" not in CHILD_ENV_KEYS
    assert "MGRAM_PARTNER_API_KEY" not in {k.upper() for k in CHILD_ENV_KEYS}
