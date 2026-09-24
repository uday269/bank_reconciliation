# AI-Assisted Bank Reconciliation

A human-supervised bank reconciliation system that recommends bank-to-ledger matches and exception treatments, requires human approval for every item, and records every system and human action in a tamper-evident audit log.

> **AI recommends. Humans authorize. The system documents both.**

## Status

**In development: Stage 5, System Design.** The application, setup instructions and usage guide will be added as the build progresses. The full README ships with release v1.0.0.

## Documents

| ID | Document | Markdown | Word |
|---|---|---|---|
| DOC-01 | Project Proposal | [01_project_proposal.md](docs/markdown/01_project_proposal.md) | [01_project_proposal.docx](docs/word-files/01_project_proposal.docx) |
| DOC-02 | Requirements Specification | [02_requirements_specification.md](docs/markdown/02_requirements_specification.md) | [02_requirements_specification.docx](docs/word-files/02_requirements_specification.docx) |
| DOC-03 | Systems Analysis | [03_systems_analysis.md](docs/markdown/03_systems_analysis.md) | [03_systems_analysis.docx](docs/word-files/03_systems_analysis.docx) |
| DOC-04 | Data Specification | [04_data_specification.md](docs/markdown/04_data_specification.md) | [04_data_specification.docx](docs/word-files/04_data_specification.docx) |
| DOC-05 | System Design | [05_system_design.md](docs/markdown/05_system_design.md) | [05_system_design.docx](docs/word-files/05_system_design.docx) |
| — | Reviewer Interface Wireframes | [reviewer_interface_wireframes.md](docs/markdown/reviewer_interface_wireframes.md) | [reviewer_interface_wireframes.docx](docs/word-files/reviewer_interface_wireframes.docx) |

## Data

The dataset is synthetic and generated from a fixed seed, so every run produces identical files.

```
python3 scripts/generate_dataset.py                        August 2026, evaluation and demonstration
python3 scripts/generate_dataset.py --profile calibration  July 2026, confidence calibration only
```

The generator checks its own output: it recomputes the bank-to-book statement and fails if the two sides do not tie, if a scenario is unrepresented, or if no high-risk items exist.

| Dataset | Seed | Transactions | Use |
|---|---|---|---|
| `data/august_2026` | 20260801 | 1,240 | Every demonstration, test and reported metric |
| `data/july_2026_calibration` | 20260701 | 988 | Fitting the confidence calibrator only |

`ground_truth.csv` holds the correct answer for every item and is read by the evaluation harness only, never by the application.

## Database

```
sqlite3 reconciliation.db < db/schema.sql
```

`db/schema.sql` is the source of truth for the schema: 23 tables, append-only triggers on evidence tables, and a hash-chained audit log.

## Architecture

Six layers with dependencies pointing one way: presentation, application services, control, domain, infrastructure, storage. Control rules (permissions, separation of duties, period lock, the completion rule) live in their own layer and are enforced again by database constraints and triggers. See [DOC-05](docs/markdown/05_system_design.md).

## Repository Structure

```
bank_reconciliation/
├── app/               Application (from Stage 6)
├── data/              Generated datasets (CSV, seeded and deterministic)
├── db/
│   └── schema.sql     Physical schema, constraints, triggers, indexes
├── docs/
│   ├── markdown/      Project documents (Markdown)
│   ├── word-files/    Project documents (Word)
│   └── diagrams/      Mermaid sources, wireframe HTML, and rendered images
├── guides/
│   ├── markdown/      User-facing guides (Markdown)
│   └── word-files/    User-facing guides (Word)
└── scripts/
    └── generate_dataset.py
```

## Scope at a Glance

| Item | Value |
|---|---|
| Organization | Bonneville Provisions Co. (fictional) |
| Account | Lone Peak Commercial Bank, Operating Checking ending 7310 (fictional) |
| Period | August 2026 |
| Data | Synthetic only, generated from a fixed seed |
| Planned stack | Python, FastAPI, Jinja2, SQLAlchemy, SQLite |
