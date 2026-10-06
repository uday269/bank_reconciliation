"""Application factory for the reviewer interface (DOC-05 sections 10 and 11).

    python -m app.web            serves http://127.0.0.1:8000

Requests are served one at a time against one SQLite connection (ADR-21): handlers are
async and do their database work without awaiting, so the event loop never interleaves
two of them. For a local, single-team prototype that is simpler and safer than a pool.

Errors map to the classes in DOC-05 section 11:
    control refusal (E-CTL)   message naming the rule, back to the same page; the service
                              has already written the BLOCKED_ATTEMPT event
    input problems  (E-VAL)   every problem listed, back to the same page, nothing written
    not found                 404 page
Anything else is a system error: the transaction has rolled back and the page says the
action was not saved.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import Config, load_config
from app.control import ControlViolation
from app.infra.repositories import RepositoryError
from app.services.adjustment_service import AdjustmentInputError
from app.services.container import DEFAULT_CALIBRATION, Services
from app.services.import_service import ImportError_
from app.services.period_service import PeriodInputError
from app.services.review_service import ManualMatchError
from app.services.prose_service import ProseUnavailable
from app.services.run_service import RunInputError
from app.web.common import back, flash, page
from app.web.routes import dashboard, period, review, runs

STATIC = Path(__file__).resolve().parent / "static"


def create_app(config: Config | None = None, calibration_path: Path = DEFAULT_CALIBRATION,
               services: Services | None = None) -> FastAPI:
    """Build the application. Tests pass ready-made `services` on their own database."""
    app = FastAPI(title="Bank Reconciliation", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = services or Services.build(config or load_config(), calibration_path)

    # The session holds only the selected identity and pending messages. The key comes
    # from the environment when set; otherwise a new one per start, which simply means
    # choosing the identity again after a restart.
    app.add_middleware(SessionMiddleware, same_site="lax",
                       secret_key=os.environ.get("BANKREC_SESSION_KEY") or secrets.token_hex(32))
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.include_router(dashboard.router)
    app.include_router(review.router)
    app.include_router(runs.router)
    app.include_router(period.router)
    _register_error_handlers(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ControlViolation)
    async def control_refused(request: Request, error: ControlViolation):
        flash(request, "error", f"Not allowed: {error.message} ({error.rule}, {error.code}). "
                                "The attempt has been recorded.", error.details)
        return back(request)

    async def input_problems(request: Request, error: Exception):
        problems = getattr(error, "problems", None) or [str(error)]
        flash(request, "error", "Nothing was saved. Please correct the following:", problems)
        return back(request)

    for error_class in (AdjustmentInputError, ManualMatchError, PeriodInputError, ImportError_, RunInputError, ProseUnavailable):
        app.add_exception_handler(error_class, input_problems)

    async def not_found(request: Request, error: Exception):
        return page(request, "error.html", "Not found", status_code=404,
                    heading="Not found", message=str(error) or "That record does not exist.")

    for error_class in (RepositoryError, FileNotFoundError, KeyError):
        app.add_exception_handler(error_class, not_found)
