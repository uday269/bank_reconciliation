"""Render report data to HTML and CSV, and hash its content (FR-RPT-01..04, ADR-14).

Every report is the same shape: a header, summary figures, one or more tables and notes.
Builders produce that shape; this module turns it into files. One template keeps the
13 reports visually consistent and keeps rendering out of the business code.

Content hash
    The hash covers the report's data in canonical JSON: code, title, summary, tables and
    notes. The generation timestamp is printed on the page but excluded from the hash, so
    regenerating an unchanged report yields the same hash (ADR-14), and a report can be
    shown to be unchanged since it was generated.

PDF
    Not produced here: the HTML prints cleanly to PDF from any browser (NOP-09).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).resolve().parent / "templates"
_environment = Environment(loader=FileSystemLoader(TEMPLATES),
                           autoescape=select_autoescape(["html"]),   # descriptions are untrusted text
                           trim_blocks=True, lstrip_blocks=True)


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list[str]] = field(default_factory=list)
    numeric: list[str] = field(default_factory=list)          # columns right-aligned


@dataclass
class ReportData:
    code: str
    title: str
    purpose: str
    summary: list[tuple[str, str]] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def canonical(self) -> str:
        """Byte-stable JSON of the content; nothing time-dependent is included."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Header:
    organization: str
    bank_account: str
    period: str
    run_id: int
    generated_at: str


def render_html(report: ReportData, header: Header) -> str:
    return _environment.get_template("report.html").render(
        report=report, header=header, content_hash=report.content_hash())


def render_csv(report: ReportData) -> str:
    """One block per table: a section row, the column row, the data, then a blank row."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([report.code, report.title])
    for label, value in report.summary:
        writer.writerow([label, value])
    for table in report.tables:
        writer.writerow([])
        writer.writerow([f"[{table.title}]"])
        writer.writerow(table.columns)
        writer.writerows(table.rows)
    return buffer.getvalue()


def write_report(report: ReportData, header: Header, directory: Path) -> dict[str, Path]:
    """Write both formats and return their paths, keyed by format."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = report.code.lower().replace("-", "_")
    paths = {"html": directory / f"{stem}.html", "csv": directory / f"{stem}.csv"}
    paths["html"].write_text(render_html(report, header), encoding="utf-8")
    paths["csv"].write_text(render_csv(report), encoding="utf-8")
    return paths
