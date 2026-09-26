"""Tiny YAML-based i18n. User texts are HTML (parse_mode=HTML); escape user data with ``h()``."""

from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
LANGS = ("ru", "en")
DEFAULT_LANG = "ru"


def _flatten(prefix: str, value: Any, out: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), item, out)
    else:
        out[prefix] = str(value)


@lru_cache
def _catalog() -> dict[str, dict[str, str]]:
    catalog: dict[str, dict[str, str]] = {}
    for lang in LANGS:
        path = LOCALES_DIR / f"{lang}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        flat: dict[str, str] = {}
        _flatten("", data or {}, flat)
        catalog[lang] = flat
    return catalog


def h(value: Any) -> str:
    return html.escape(str(value), quote=False)


def gettext(lang: str | None, key: str, **kwargs: Any) -> str:
    catalog = _catalog()
    text = catalog.get(lang or DEFAULT_LANG, {}).get(key)
    if text is None:
        text = catalog[DEFAULT_LANG].get(key, key)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


class Translator:
    def __init__(self, lang: str | None) -> None:
        self.lang = lang if lang in LANGS else DEFAULT_LANG

    def __call__(self, key: str, **kwargs: Any) -> str:
        return gettext(self.lang, key, **kwargs)


def missing_keys() -> dict[str, set[str]]:
    catalog = _catalog()
    keys = set().union(*(set(v) for v in catalog.values()))
    return {lang: keys - set(catalog[lang]) for lang in LANGS}
