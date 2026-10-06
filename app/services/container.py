"""Wiring: build every service once from configuration.

The web application and the tests use this, so the order of construction and the
calibration artefact rule live in one place.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import Config
from app.domain.calibration import Calibrator
from app.infra.audit import AuditLog
from app.infra.db import Database, open_database
from app.infra.repositories import Repositories
from app.services.adjustment_service import AdjustmentService
from app.services.import_service import ImportService
from app.services.matching_service import MatchingService
from app.services.period_service import PeriodService
from app.services.prose_service import ProseService
from app.services.report_service import ReportService
from app.services.review_service import ReviewService
from app.services.run_service import RunService
from app.services.validation_service import ValidationService

DEFAULT_CALIBRATION = Path("models/calibration.json")


def load_calibrator(path: Path) -> Calibrator:
    """The fitted artefact, or raw scores clearly labelled as uncalibrated if it is missing."""
    if path.exists():
        try:
            return Calibrator.load(path)
        except (ValueError, KeyError) as error:
            print(f"Calibration artefact could not be read ({error}); raw scores will be reported.",
                  file=sys.stderr)
    return Calibrator.identity()


@dataclass
class Services:
    config: Config
    database: Database
    repositories: Repositories
    audit: AuditLog
    imports: ImportService
    validation: ValidationService
    matching: MatchingService
    review: ReviewService
    adjustments: AdjustmentService
    period: PeriodService
    reports: ReportService
    runs: RunService
    prose: ProseService

    @classmethod
    def build(cls, config: Config, calibration_path: Path = DEFAULT_CALIBRATION) -> "Services":
        database = open_database(config.paths.database, config.paths.schema)
        repositories = Repositories.for_database(database)
        audit = AuditLog(database)
        matching = MatchingService(database, repositories, audit, config, load_calibrator(calibration_path))
        period = PeriodService(database, repositories, audit)
        imports = ImportService(database, repositories, audit, config)
        validation = ValidationService(database, repositories, audit, config)
        return cls(
            config=config, database=database, repositories=repositories, audit=audit,
            imports=imports, validation=validation,
            matching=matching,
            review=ReviewService(database, repositories, audit, matching),
            adjustments=AdjustmentService(database, repositories, audit),
            period=period,
            reports=ReportService(database, repositories, audit, period, config.organization.name,
                                  config.paths.report_output),
            runs=RunService(config, database, repositories, audit, imports, validation, matching),
            prose=ProseService(config, database, repositories, audit))
