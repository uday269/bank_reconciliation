"""W7: close readiness, sign-off and lock, reopening (P7)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.domain.statement import money
from app.web import views
from app.web.common import back, current_user, flash, redirect, require_user, services
from app.web.common import page as render_page

router = APIRouter()


@router.get("/runs/{run_id}/close", response_class=HTMLResponse)
async def close_screen(request: Request, run_id: int):
    data = views.close_page(services(request), run_id, current_user(request))
    return render_page(request, "close.html", "Close period", data["run"], page=data)


@router.post("/runs/{run_id}/close")
async def sign_off(request: Request, run_id: int, comment: str = Form("")):
    user = require_user(request)
    if user is None:
        return back(request, f"/runs/{run_id}/close")
    svc = services(request)
    svc.period.sign_off(run_id, user["user_id"], comment.strip() or None)
    signoff = svc.repositories.periods.latest_signoff(run_id)
    flash(request, "ok", f"Period signed off and locked. Unresolved difference "
                         f"{money(signoff['unresolved_difference_cents'])}; chain head {signoff['chain_head_hash'][:16]}…",
          ["Generate the report package again to archive the signed Reconciliation Summary."])
    return redirect(f"/runs/{run_id}/close")


@router.post("/runs/{run_id}/reopen-request")
async def request_reopen(request: Request, run_id: int, reason: str = Form("")):
    user = require_user(request)
    if user is None:
        return back(request, f"/runs/{run_id}/close")
    services(request).period.request_reopen(run_id, user["user_id"], reason)
    flash(request, "ok", "Reopening requested. A Controller other than you must decide it (CR-06).")
    return redirect(f"/runs/{run_id}/close")


@router.post("/reopen-requests/{request_id}/decision")
async def decide_reopen(request: Request, request_id: int, decision: str = Form(...), comment: str = Form("")):
    user = require_user(request)
    svc = services(request)
    run_id = svc.repositories.periods.get_reopen_request(request_id)["run_id"]
    if user is None:
        return back(request, f"/runs/{run_id}/close")
    status = svc.period.decide_reopen(request_id, user["user_id"], decision, comment.strip() or None)
    flash(request, "ok", "Reopening approved: the run is back in review and needs a new sign-off to close."
          if status == "in_review" else "Reopening rejected: the period stays closed.")
    return redirect(f"/runs/{run_id}/close")
