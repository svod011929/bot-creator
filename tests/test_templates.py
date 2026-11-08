import pytest

from child import templates
from child.files import human_size, read_settings, write_config, write_setting
from child.templates.starbot import parse_task


def test_registry():
    keys = [t.key for t in templates.all_infos()]
    assert keys == ["clicker", "refbot", "starbot"]
    for t in templates.all_infos():
        assert t.defaults()["welcome"]
        assert "min_withdraw" in t.defaults()


def test_setting_parse():
    s = templates.Setting("x", "X", 1, "int")
    assert s.parse(" 5 ") == 5
    with pytest.raises(ValueError):
        s.parse("-1")
    f = templates.Setting("x", "X", 1.0, "float")
    assert f.parse("0,5") == 0.5
    c = templates.Setting("c", "C", "", "channel")
    assert c.parse("https://t.me/mychan") == "@mychan"
    assert c.parse("-1001234") == "-1001234"
    assert c.parse("-") == ""
    with pytest.raises(ValueError):
        c.parse("mychan")


def test_settings_files(tmp_path):
    write_config(tmp_path, {"token": "secret"})
    assert oct((tmp_path / "config.json").stat().st_mode)[-3:] == "600"
    defaults = {"a": 1, "b": "x"}
    assert read_settings(tmp_path, defaults) == defaults
    write_setting(tmp_path, "a", 7)
    write_setting(tmp_path, "zzz", 1)
    assert read_settings(tmp_path, defaults) == {"a": 7, "b": "x"}


def test_parse_task():
    assert parse_task("Канал | https://t.me/x | 0,5 | @x") == ("Канал", "https://t.me/x", 0.5, "@x")
    assert parse_task("Сайт | https://a.b | 2") == ("Сайт", "https://a.b", 2.0, "")
    for bad in ("a | b", "a | ftp://x | 1", "a | https://x | 0", "a | https://x | 1 | chan"):
        with pytest.raises(ValueError):
            parse_task(bad)


def test_human_size():
    assert human_size(10) == "10 Б"
    assert human_size(2048) == "2.0 КБ"
