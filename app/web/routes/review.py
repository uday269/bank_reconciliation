"""W3/W5 item detail and decisions, manual pairing, W4 batch, adjustments (P6).

Every POST calls one service method. The services apply every control and write the
evidence; a refusal comes back as ControlViolation and is shown by the handler in main.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.services.adjustment_service import AdjustmentInputError
from app.web import views
from app.web.common import back, current_user, flash, redirect, require_user, services
from app.web.common import page as render_page

router = APIRouter()

PAST_TENSE = {"approve": "approved", "modify": "approved with the selected candidate", "reject": "rejected",
              "escalate": "escalated", "unresolved": "left unresolved"}


def _origin_queue(svc, rec) -> str:
    subject = svc.repositories.items.get(rec["subject_item_type"], rec["subject_item_id"])
    return "CAT-05" if subject["status"] == "escalated" else rec["category_code"]


def _next_in_queue(svc, run_id: int, category: str) -> str:
    """After a decision, open the next item in the same queue, or the queue if it is empty."""
    rows = views.queue_rows(svc, run_id, category)
    return f"/items/{rows[0]['recommendation_id']}" if rows else f"/runs/{run_id}/queues/{category}"


# ---------------------------------------------------------------------------
# Item detail and decisions
# ---------------------------------------------------------------------------

@router.get("/items/{recommendation_id}", response_class=HTMLResponse)
async def item_detail(request: Request, recommendation_id: int):
    svc, user = services(request), current_user(request)
    rec = svc.repositories.recommendations.get(recommendation_id)
    if user is not None and rec["status"] == "open":
        svc.review.open_detail(recommendation_id, user["user_id"])     # DETAIL_OPENED (FR-REV-10, -11)
    data = views.item_detail(svc, recommendation_id, user)
    return render_page(request, "item.html", f"Item {data['subject_ref']}", data["run"], page=data)


@router.post("/items/{recommendation_id}/decision")
async def decide(request: Request, recommendation_id: int, decision: str = Form(...),
                 comment: str = Form(""), chosen_candidate_id: str = Form("")):
    user = require_user(request)
    if user is None:
        return back(request)
    svc = services(request)
    rec = svc.repositories.recommendations.get(recommendation_id)
    origin = _origin_queue(svc, rec)
    chosen = int(chosen_candidate_id) if decision == "modify" and chosen_candidate_id else None
    outcome = svc.review.decide(recommendation_id, user["user_id"], decision, comment.strip() or None, chosen)
    details = []
    if outcome.requeued:
        details.append(f"Re-proposed as exception recommendation(s) {', '.join(map(str, outcome.requeued))}.")
    if outcome.superseded:
        details.append(f"Superseded recommendation(s) {', '.join(map(str, outcome.superseded))}.")
    flash(request, "ok", f"Recommendation {recommendation_id} {PAST_TENSE[decision]}.", details)
    return redirect(_next_in_queue(svc, rec["run_id"], origin))


@router.post("/items/{recommendation_id}/manual-match")
async def manual_match(request: Request, recommendation_id: int):
    user = require_user(request)
    if user is None:
        return back(request)
    form = await request.form()
    counterparts = []
    for value in form.getlist("counterpart"):
        item_type, _, item_id = str(value).partition(":")
        counterparts.append((item_type, int(item_id)))
    svc = services(request)
    rec = svc.repositories.recommendations.get(recommendation_id)
    origin = _origin_queue(svc, rec)
    outcome = svc.review.manual_match(recommendation_id, user["user_id"], counterparts,
                                      str(form.get("comment", "")).strip() or None)
    flash(request, "ok", f"Recommendation {recommendation_id} paired by hand with {len(counterparts)} record(s).",
          [f"Superseded recommendation(s) {', '.join(map(str, outcome.superseded))}."] if outcome.superseded else [])
    return redirect(_next_in_queue(svc, rec["run_id"], origin))


@router.post("/items/{recommendation_id}/prose")
async def generate_prose(request: Request, recommendation_id: int):
    """Optional prose (DD-10). Restates the evidence; never a score or a decision (CR-13)."""
    user = require_user(request)
    if user is None:
        return back(request)
    result = services(request).prose.generate(recommendation_id, user["user_id"])
    if result.rejected_reason:
        flash(request, "error", f"Provider text discarded ({result.rejected_reason}); "
                                "the offline summary was stored instead.")
    else:
        flash(request, "ok", f"{result.label} stored." if result.generated else
              "Offline summary of the evidence stored (no external provider is configured).")
    return redirect(f"/items/{recommendation_id}")


# ---------------------------------------------------------------------------
# W4 batch
# ---------------------------------------------------------------------------

@router.get("/runs/{run_id}/batch", response_class=HTMLResponse)
async def batch(request: Request, run_id: int):
    data = views.batch(services(request), run_id, current_user(request))
    return render_page(request, "batch.html", "Exact-match batch", data["run"], page=data)


@router.post("/runs/{run_id}/batch")
async def decide_batch(request: Request, run_id: int):
    user = require_user(request)
    if user is None:
        return back(request)
    form = await request.form()
    ids = [int(value) for value in form.getlist("recommendation_id")]
    action, comment = str(form.get("action", "")), str(form.get("comment", "")).strip() or None
    if not ids:
        flash(request, "error", "Nothing was saved: select at least one row.")
        return back(request, f"/runs/{run_id}/batch")
    svc = services(request)
    if action == "reject":
        result = svc.review.reject_batch(run_id, user["user_id"], ids, comment)
    else:
        result = svc.review.approve_batch(run_id, user["user_id"], ids, comment)
    flash(request, "ok", f"Batch {result.review_batch_id}: {len(result.outcomes)} item(s) "
                         f"{'rejected' if action == 'reject' else 'approved'}, one decision record each.")
    return redirect(f"/runs/{run_id}/batch")


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------

def _cents(amount: str) -> int:
    try:
        value = Decimal(amount.replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise AdjustmentInputError(["amount must be a number, such as 86.90"])
    if value != value.quantize(Decimal("0.01")):
        raise AdjustmentInputError(["amount cannot have more than two decimal places"])
    return int(value * 100)


@router.post("/items/{recommendation_id}/adjustment")
async def propose_adjustment(request: Request, recommendation_id: int, amount: str = Form(""),
                             debit: str = Form(""), credit: str = Form(""), rationale: str = Form(""),
                             evidence: str = Form("")):
    user = require_user(request)
    if user is None:
        return back(request)
    adjustment_id = services(request).adjustments.propose(
        recommendation_id, user["user_id"], amount_cents=_cents(amount), debit_account_code=debit,
        credit_account_code=credit, rationale=rationale, evidence_refs=evidence.strip() or None)
    flash(request, "ok", f"Adjustment {adjustment_id} proposed. It awaits approval by someone else; "
                         "nothing has been posted.")
    return redirect(f"/items/{recommendation_id}")


@router.get("/runs/{run_id}/adjustments", response_class=HTMLResponse)
async def adjustments(request: Request, run_id: int):
    data = views.adjustment_queue(services(request), run_id, current_user(request))
    return render_page(request, "adjustments.html", "Adjustments", data["run"], page=data)


@router.post("/adjustments/{adjustment_id}/decision")
async def decide_adjustment(request: Request, adjustment_id: int, decision: str = Form(...),
                            comment: str = Form("")):
    user = require_user(request)
    if user is None:
        return back(request)
    svc = services(request)
    run_id = svc.repositories.adjustments.get(adjustment_id)["run_id"]
    status = svc.adjustments.decide(adjustment_id, user["user_id"], decision, comment.strip() or None)
    flash(request, "ok", f"Adjustment {adjustment_id} {status}. Nothing has been posted.")
    return redirect(f"/runs/{run_id}/adjustments")
