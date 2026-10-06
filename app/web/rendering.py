"""Jinja2 environment for the reviewer pages.

Pages are rendered to a string here and returned as HTML by the routes, so every page can
be rendered and checked without starting a server. Autoescaping is on: descriptions,
payees and comments come from imported files and reviewers, and are treated as text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATES = Path(__file__).resolve().parent / "templates"


def money(cents: int | None) -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def confidence(value: float | None) -> str:
    return "not scored" if value is None else f"{value:.2f}"


def short_hash(value: str | None, length: int = 12) -> str:
    return "" if not value else value[:length] + "…"


def timestamp(value: str | None) -> str:
    """2026-09-08T16:01:30Z -> 2026-09-08 16:01:30 UTC"""
    return "" if not value else value.replace("T", " ").replace("Z", " UTC")


environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]),
                          undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)
environment.filters.update(money=money, percent=percent, confidence=confidence,
                           short_hash=short_hash, timestamp=timestamp)


def render(template: str, **context: Any) -> str:
    return environment.get_template(template).render(**context)
