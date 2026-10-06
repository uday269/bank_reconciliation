"""W1 dashboard, identity selection, queues, W8 audit log, reports and the JSON status API."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from app.infra.audit import AuditEvent, RunContext
from app.web import views
from app.web.common import back, current_user, flash, require_user, services
from app.web.common import page as render_page

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, run: int | None = None):
    data = views.dashboard(services(request), run, current_user(request))
    return render_page(request, "dashboard.html", "Dashboard", data["run"], page=data)


@router.post("/identity")
async def select_identity(request: Request, user_id: str = Form(""), run_id: str = Form("")):
    """Switch the acting identity. Recorded, because decisions are attributed to it (DD-05)."""
    svc = services(request)
    if not user_id:
        request.session.pop("user_id", None)
        return back(request)
    user = svc.repositories.users.get(int(user_id))
    request.session["user_id"] = user["user_id"]
    run = svc.repositories.runs.get(int(run_id)) if run_id else svc.repositories.runs.latest()
    if run is not None:
        with svc.database.transaction() as connection:
            svc.audit.write(connection, RunContext.load(svc.database, run["run_id"]), AuditEvent(
                event_type="IDENTITY_SELECTED", process_code="P6", entity_type="app_user",
                entity_id=user["user_id"], actor_type="human", actor_user_id=user["user_id"]))
    flash(request, "ok", f"Acting as {user['full_name']}.")
    return back(request)


@router.get("/runs/{run_id}/queues/{category}", response_class=HTMLResponse)
async def queue(request: Request, run_id: int, category: str):
    data = views.queue(services(request), run_id, category, current_user(request))
    return render_page(request, "queue.html", f"{category} queue", data["run"], page=data)


@router.get("/runs/{run_id}/audit", response_class=HTMLResponse)
async def audit_log(request: Request, run_id: int, event_type: str = "", page: int = 1):
    data = views.audit_log(services(request), run_id, event_type or None, page)
    return render_page(request, "audit.html", "Audit log", services(request).repositories.runs.get(run_id), page=data)


@router.get("/runs/{run_id}/reports", response_class=HTMLResponse)
async def reports(request: Request, run_id: int):
    data = views.reports_index(services(request), run_id)
    return render_page(request, "reports.html", "Reports", services(request).repositories.runs.get(run_id), page=data)


@router.post("/runs/{run_id}/reports")
async def generate_reports(request: Request, run_id: int):
    user = require_user(request)
    if user is None:
        return back(request)
    result = services(request).reports.generate_package(run_id, user["user_id"])
    details = [f"{result.promoted} item(s) reconciled by the report check."] if result.promoted else []
    if result.failures:
        flash(request, "error", f"{len(result.failures)} approved item(s) failed the report check.", result.failures)
    flash(request, "ok", f"Report package generated. Run status: {result.run_status.replace('_', ' ')}.", details)
    return back(request, f"/runs/{run_id}/reports")


# Registered before the HTML view: "{report_code}" alone would also match "RPT-09.csv".
@router.get("/runs/{run_id}/reports/{report_code}.csv")
async def export_report(request: Request, run_id: int, report_code: str):
    user = current_user(request)
    path = services(request).reports.export(run_id, report_code, "csv", user["user_id"] if user else None)
    return FileResponse(path, media_type="text/csv", filename=path.name)


@router.get("/runs/{run_id}/reports/{report_code}", response_class=HTMLResponse)
async def view_report(request: Request, run_id: int, report_code: str):
    current = [row for row in services(request).repositories.reports.current(run_id)
               if row["report_code"] == report_code and row["output_format"] == "html"]
    if not current:
        raise FileNotFoundError(f"{report_code} has not been generated for run {run_id}")
    return HTMLResponse(Path(current[0]["file_path"]).read_text(encoding="utf-8"))


@router.get("/api/runs/{run_id}/status")
async def run_status(request: Request, run_id: int):
    return JSONResponse(views.run_status(services(request), run_id))


@router.get("/api/runs/{run_id}/audit/verify")
async def verify_chain(request: Request, run_id: int):
    """Recompute the chain and record that it was checked (FR-AUD-06)."""
    svc = services(request)
    result = svc.audit.verify(run_id)
    user = current_user(request)
    with svc.database.transaction() as connection:
        svc.audit.write(connection, RunContext.load(svc.database, run_id), AuditEvent(
            event_type="CHAIN_VERIFIED", process_code="P8", entity_type="reconciliation_run", entity_id=run_id,
            actor_type="human" if user else "system", actor_user_id=user["user_id"] if user else None,
            approval_status="intact" if result.intact else "broken",
            evidence_refs=[f"chain_head:{result.head_hash}"]))
    return JSONResponse({"run_id": run_id, "intact": result.intact, "events_checked": result.events_checked,
                         "head_hash": result.head_hash, "summary": result.summary()})
