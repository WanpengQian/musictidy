"""Jinja2Templates 单例 + 自定义 filters."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _year(value: str | None) -> str:
    if not value:
        return ""
    return value[:4]


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.0f}%"


def _parse_json(value: str | None) -> list:
    if not value:
        return []
    try:
        out = json.loads(value)
        return out if isinstance(out, list) else []
    except Exception:
        return []


def _join_types(secondary_json: str | None) -> str:
    types = _parse_json(secondary_json)
    return " · ".join(types) if types else ""


templates.env.filters["year"] = _year
templates.env.filters["pct"] = _pct
templates.env.filters["sec_types"] = _join_types
