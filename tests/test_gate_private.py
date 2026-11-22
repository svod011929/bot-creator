"""Gate accepts private invite links when a numeric chat id is known."""
from __future__ import annotations

from creator.handlers.gate import _is_invite, _link, _public_username, _username


def test_invite_detected():
    assert _is_invite("https://t.me/+AbCdEfGhIjKl")
    assert _is_invite("https://t.me/joinchat/AbCdEfGhIjKl")
    assert not _is_invite("https://t.me/mychannel")


def test_public_username_skips_invites():
    assert _public_username("https://t.me/mychannel") == "@mychannel"
    assert _username("https://t.me/mychannel") == "@mychannel"
    assert _public_username("https://t.me/+AbCdEfGhIjKl") is None
    assert _username("https://t.me/+AbCdEfGhIjKl") is None


def test_link_normalizes_invite():
    assert _link("https://t.me/+AbCdEfGhIjKlMn") == "https://t.me/+AbCdEfGhIjKlMn"
