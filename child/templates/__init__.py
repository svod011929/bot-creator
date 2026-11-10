"""Реестр шаблонов дочерних ботов и описание их настроек."""
from __future__ import annotations

import importlib
import math
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    default: Any
    kind: str = "text"  # text | int | float | channel

    def parse(self, raw: str) -> Any:
        raw = raw.strip()
        if self.kind == "int":
            value = int(raw)
            if value < 0:
                raise ValueError("negative")
            return value
        if self.kind == "float":
            value = float(raw.replace(",", "."))
            if not math.isfinite(value) or value < 0:
                raise ValueError("Invalid number")
            return value
        if self.kind == "channel":
            if raw in {"-", "0", "нет", "off"}:
                return ""
            if raw.startswith("https://t.me/"):
                raw = "@" + raw.removeprefix("https://t.me/").strip("/")
            if not (raw.startswith("@") or raw.lstrip("-").isdigit()):
                raise ValueError("channel")
            return raw
        if not raw:
            raise ValueError("empty")
        return raw[:3500]

    def show(self, value: Any) -> str:
        if self.kind == "channel" and not value:
            return "не задан"
        text = str(value)
        return text if len(text) <= 60 else text[:57] + "…"


@dataclass(frozen=True)
class TemplateInfo:
    key: str
    title: str
    emoji: str
    price_key: str
    description: str
    settings: list[Setting] = field(default_factory=list)
    version: int = 1
    capabilities: tuple[str, ...] = ()

    @property
    def button(self) -> str:
        return f"{self.emoji} {self.title}"

    def defaults(self) -> dict[str, Any]:
        return {s.key: s.default for s in self.settings}

    def setting(self, key: str) -> Setting | None:
        return next((s for s in self.settings if s.key == key), None)


TEMPLATE_MODULES = {
    "clicker": "child.templates.clicker",
    "refbot": "child.templates.refbot",
    "starbot": "child.templates.starbot",
}


def load(key: str) -> ModuleType:
    if key not in TEMPLATE_MODULES:
        raise KeyError(key)
    return importlib.import_module(TEMPLATE_MODULES[key])


def info(key: str) -> TemplateInfo:
    return load(key).INFO


def all_infos() -> list[TemplateInfo]:
    return [info(k) for k in TEMPLATE_MODULES]
