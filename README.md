# AI-Assisted Bank Reconciliation

A human-supervised bank reconciliation system that recommends bank-to-ledger matches and exception treatments, requires human approval for every item, and records every system and human action in a tamper-evident audit log.

> **AI recommends. Humans authorize. The system documents both.**

## Status

**In development: Stage 2, Business Analysis.** The application, setup instructions and usage guide will be added as the build progresses. The full README ships with release v1.0.0.

## Documents

| ID | Document | Markdown | Word |
|---|---|---|---|
| DOC-01 | Project Proposal | [01_project_proposal.md](docs/markdown/01_project_proposal.md) | [01_project_proposal.docx](docs/word-files/01_project_proposal.docx) |
| DOC-02 | Requirements Specification | [02_requirements_specification.md](docs/markdown/02_requirements_specification.md) | [02_requirements_specification.docx](docs/word-files/02_requirements_specification.docx) |

## Repository Structure

```
bank_reconciliation/
├── docs/
│   ├── markdown/      Project documents (Markdown)
│   ├── word-files/    Project documents (Word)
│   └── diagrams/      Mermaid diagram sources (.mmd) and rendered images (.png)
└── guides/
    ├── markdown/      User-facing guides (Markdown)
    └── word-files/    User-facing guides (Word)
```

## Scope at a Glance

| Item | Value |
|---|---|
| Organization | Bonneville Provisions Co. (fictional) |
| Account | Lone Peak Commercial Bank, Operating Checking ending 7310 (fictional) |
| Period | August 2026 |
| Data | Synthetic only, generated from a fixed seed |
| Planned stack | Python, FastAPI, Jinja2, SQLAlchemy, SQLite |
