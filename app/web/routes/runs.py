"""W2: create a run, import the files, validate and match (P1..P5)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.services.run_service import UPLOADS
from app.web import views
from app.web.common import back, current_user, flash, redirect, require_user, services
from app.web.common import page as render_page

router = APIRouter()


@router.get("/import", response_class=HTMLResponse)
async def new_run(request: Request):
    data = views.import_page(services(request), None, current_user(request))
    return render_page(request, "import.html", "New run", None, page=data)


@router.post("/runs")
async def create_run(request: Request, period_start: str = Form(""), period_end: str = Form("")):
    user = require_user(request)
    if user is None:
        return back(request, "/import")
    run_id = services(request).runs.create(user["user_id"], period_start.strip(), period_end.strip())
    flash(request, "ok", f"Run {run_id} created for {period_start} to {period_end}. Import its files next.")
    return redirect(f"/runs/{run_id}/import")


@router.get("/runs/{run_id}/import", response_class=HTMLResponse)
async def import_screen(request: Request, run_id: int):
    data = views.import_page(services(request), run_id, current_user(request))
    return render_page(request, "import.html", "Import and validation", data["run"], page=data)


@router.post("/runs/{run_id}/import")
async def import_files(request: Request, run_id: int):
    user = require_user(request)
    if user is None:
        return back(request, f"/runs/{run_id}/import")
    form = await request.form()
    files: dict[str, bytes] = {}
    for name in UPLOADS:
        upload = form.get(name)
        files[name] = await upload.read() if hasattr(upload, "read") else b""
    result = services(request).runs.import_files(run_id, user["user_id"], files)
    flash(request, "ok", f"Imported {result.total_rows} rows from {len(result.files)} files.",
          [f"{f.file_type}: {f.stored_rows} stored, {len(f.rejected_rows)} unreadable" for f in result.files])
    return redirect(f"/runs/{run_id}/import")


@router.post("/runs/{run_id}/match")
async def validate_and_match(request: Request, run_id: int):
    user = require_user(request)
    if user is None:
        return back(request, f"/runs/{run_id}/import")
    result = services(request).runs.process(run_id, user["user_id"])
    if result.matching is None:
        flash(request, "error", "Validation failed at file level, so matching was not started (CR-17).",
              result.validation.failures)
        return redirect(f"/runs/{run_id}/import")
    m = result.matching
    flash(request, "ok", f"{result.validation.summary().rstrip('.')}. Matching produced {m.recommendations} "
                         f"recommendations for review.", [f"{code}: {count}" for code, count in sorted(m.by_category.items())])
    return redirect(f"/?run={run_id}")
