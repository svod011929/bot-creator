"""Own-channel config writes and strict syntax (no Telegram)."""
import concurrent.futures
import json
from pathlib import Path

import pytest

from child import files
from child.files import read_json, write_setting


def put_field(args):
    path, key = args
    write_setting(Path(path), key, key)


def test_pair_cas_preserves_fields_and_mode(tmp_path):
    write_setting(tmp_path, "welcome", "old")
    (tmp_path / 'settings.json').chmod(0o600)
    files.write_settings_pair(tmp_path, expected=("", "-"), channel="-100123", url="https://t.me/+abc")
    assert read_json(tmp_path / 'settings.json') == {
        'welcome': 'old', 'required_channel': '-100123', 'channel_url': 'https://t.me/+abc',
    }
    assert (tmp_path / 'settings.json').stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        files.write_settings_pair(tmp_path, expected=("", "-"), channel="", url="")
    assert read_json(tmp_path / 'settings.json')['required_channel'] == '-100123'


def test_concurrent_processes_keep_unrelated_fields(tmp_path):
    with concurrent.futures.ProcessPoolExecutor(4) as pool:
        list(pool.map(put_field, [(str(tmp_path), f'k{i}') for i in range(30)]))
    assert len(read_json(tmp_path / 'settings.json')) == 30


@pytest.mark.parametrize('data', ['broken', '[]'])
def test_corrupt_json_not_overwritten(tmp_path, data):
    (tmp_path / 'settings.json').write_text(data)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        write_setting(tmp_path, 'x', 1)
    assert (tmp_path / 'settings.json').read_text() == data


def test_failed_replace_retains_old_pair(tmp_path, monkeypatch):
    write_setting(tmp_path, 'welcome', 'old')
    old = (tmp_path / 'settings.json').read_bytes()
    def fail(*args):
        raise PermissionError('no permission')
    monkeypatch.setattr(files.os, 'replace', fail)
    with pytest.raises(PermissionError):
        files.write_settings_pair(tmp_path, expected=('', '-'), channel='-100', url='https://t.me/+a')
    assert (tmp_path / 'settings.json').read_bytes() == old
    assert not list(tmp_path.glob('.tmp-*'))


def test_invalidation_revision(tmp_path):
    assert files.subscription_revision(tmp_path) == ''
    files.invalidate_subscription(tmp_path)
    first = files.subscription_revision(tmp_path)
    files.invalidate_subscription(tmp_path)
    assert files.subscription_revision(tmp_path) != first


@pytest.mark.parametrize(('raw', 'expected'), [('@mychan', '@mychan'),
    ('https://t.me/mychan/', '@mychan'), ('-100123', -100123)])
def test_parse_channel(raw, expected):
    from child.subscription import parse_channel
    assert parse_channel(raw) == expected


@pytest.mark.parametrize('raw', ['@', '@bad/name', 'https://t.me/+abc', 'https://t.me/c/123/4',
    'https://t.me/x?start=abc', 'https://t.me.evil/a', 'https://t.me:443/mychan',
    'https://evil@t.me/mychan', 'https://t.me/joinchat/abc', '0', 'bad', '--123'])
def test_bad_channel(raw):
    from child.subscription import parse_channel
    with pytest.raises(ValueError):
        parse_channel(raw)


@pytest.mark.parametrize('raw', ['https://t.me/+abc_123-x', 'https://t.me/joinchat/abc'])
def test_private_url(raw):
    from child.subscription import join_url
    assert join_url(raw, None) == raw


def test_public_url_matches_actual_username():
    from child.subscription import join_url
    assert join_url('@mychan', 'mychan') == 'https://t.me/mychan'
    with pytest.raises(ValueError):
        join_url('@other', 'mychan')


@pytest.mark.parametrize('raw', ['https://evil/+abc', 'https://t.me/+abc#x',
    'http://t.me/+abc', 'https://t.me/+abc?x=y', 'https://t.me/+abc/', '@mychan'])
def test_invalid_private_url(raw):
    from child.subscription import join_url
    with pytest.raises(ValueError):
        join_url(raw, None)


def test_pair_change_has_distinct_revision_even_after_restore(tmp_path):
    files.write_settings_pair(tmp_path, expected=('', '-'), channel='-100123', url='https://t.me/+a')
    first = files.subscription_revision(tmp_path)
    assert first
    files.write_settings_pair(tmp_path, expected=('-100123', 'https://t.me/+a'), channel='', url='')
    files.write_settings_pair(tmp_path, expected=('', ''), channel='-100123', url='https://t.me/+a')
    assert files.subscription_revision(tmp_path) != first
