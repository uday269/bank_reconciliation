"""Request helpers shared by every route: services, identity, messages, responses."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.infra.repositories import RepositoryError
from app.services.container import Services
from app.web import views
from app.web.rendering import render

MAX_FLASH_DETAILS = 20          # keeps the session cookie small; the audit event has the full list


def services(request: Request) -> Services:
    return request.app.state.services


def current_user(request: Request) -> sqlite3.Row | None:
    """The selected identity (DD-05). An unknown or deactivated id counts as none."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    try:
        user = services(request).repositories.users.get(int(user_id))
    except (RepositoryError, ValueError):
        return None
    return user if user["is_active"] else None


def flash(request: Request, kind: str, text: str, details: list[str] | None = None) -> None:
    details = list(details or [])
    if len(details) > MAX_FLASH_DETAILS:
        details = details[:MAX_FLASH_DETAILS] + [f"… and {len(details) - MAX_FLASH_DETAILS} more"]
    request.session.setdefault("flashes", []).append({"kind": kind, "text": text, "details": details})


def take_flashes(request: Request) -> list[dict[str, Any]]:
    return request.session.pop("flashes", [])


def page(request: Request, template: str, title: str, run: sqlite3.Row | None = None,
         status_code: int = 200, **context: Any) -> HTMLResponse:
    """Render a page with the shared header context."""
    user = current_user(request)
    body = render(template, **views.base(services(request), user, run, take_flashes(request), title), **context)
    return HTMLResponse(body, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    """303 after a POST, so a refresh never repeats the action."""
    return RedirectResponse(url, status_code=303)


def back(request: Request, fallback: str = "/") -> RedirectResponse:
    """Return to the page the form was on."""
    referer = request.headers.get("referer", "")
    base = str(request.base_url).rstrip("/")
    return redirect(referer[len(base):] if referer.startswith(base) else fallback)


def require_user(request: Request) -> sqlite3.Row | None:
    """The identity for a write, or None after queuing a message asking for one."""
    user = current_user(request)
    if user is None:
        flash(request, "error", "Select an identity before taking an action.")
    return user
