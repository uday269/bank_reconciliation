# System Design — AI-Assisted Bank Reconciliation

## 1. Purpose and Scope

This document defines how the system is built: its layers and modules, the technology used and why, how data is accessed, how the audit chain is produced and verified, how matching and review work internally, the API surface, error handling, security, permissions, configuration, report generation, performance targets, the testing approach, and the decisions taken along the way.

It describes the system as built. Where the implementation departed from the original design, the section says so and the reason is recorded in section 17.

## 2. Architecture

![Layered architecture](../diagrams/architecture.png){height=5.6in}

| Layer | Package | Responsibility | May call |
|-----------------|-----------------|--------------------------------------------|-----------------|
| Presentation | `app/web` | Routers, templates, identity selection | Services |
| Application services | `app/services` | One service per workflow step; owns transactions | Control, domain, infrastructure |
| Control | `app/control` | Permission, separation of duties, period lock, completion rule | Infrastructure |
| Domain | `app/domain` | Rules, candidate generation, scoring, calibration, risk, explanations | Nothing |
| Infrastructure | `app/infra` | Repositories, audit writer, report renderer, GenAI adapter | Database |

Dependencies point downward only. The domain layer performs no input or output at all: it receives plain data structures and returns results, which is what makes rules and scoring reproducible and testable without a database.

### 2.1 Module structure

```
app/
├── cli.py                  Command line: init, run, status, verify, report, serve, reset
├── config.py               Configuration load and parameter snapshot
├── web/
│   ├── main.py             Application factory, session, error handling
│   ├── common.py           Identity, messages, page and redirect helpers
│   ├── views.py            What each screen shows; no web framework imports
│   ├── rendering.py        Jinja2 environment and formatting filters
│   ├── routes/             dashboard.py, review.py, runs.py, period.py
│   ├── templates/          One template per screen (W1..W8) plus shared layout
│   └── static/app.css      Stylesheet
├── services/
│   ├── container.py            Builds every service from configuration
│   ├── run_service.py          W2: controls in front of import, validation, matching
│   ├── import_service.py       UC-01
│   ├── validation_service.py   UC-02, includes normalization
│   ├── matching_service.py     UC-03, orchestrates the domain layer; re-proposal of released items
│   ├── review_service.py       UC-04..UC-07, batches, manual pairing
│   ├── adjustment_service.py   UC-08, UC-09
│   ├── period_service.py       UC-11..UC-13, the bank-to-book statement
│   ├── report_service.py       UC-10, package and report check
│   ├── report_builders.py      Data for RPT-01..13
│   └── blocked.py              BLOCKED_ATTEMPT events for every service
├── control/
│   ├── errors.py           ControlViolation and error codes
│   ├── permissions.py      Permission matrix, levels, offered actions, comment rule
│   ├── duties.py           Separation of duties (CR-03..CR-06)
│   ├── states.py           Allowed status transitions from the DOC-03 state models
│   ├── period_lock.py      Lock, stage gates, close conditions (CR-10, CR-17, CR-18)
│   └── completion.py       Six-condition rule (CR-07)
├── domain/
│   ├── rules.py            BR-01..BR-12 deterministic rules
│   ├── normalize.py        Normalization with originals kept
│   ├── candidates.py       Candidate and group generation
│   ├── features.py, text.py, scoring.py, calibration.py
│   ├── risk.py             BR-13..BR-15 risk and routing
│   ├── exceptions.py       EXC-01..08 catalogue
│   ├── explanation.py      Supporting and conflicting evidence
│   ├── statement.py        Bank-to-book arithmetic (BR-17)
│   └── manual_match.py     Checks for a hand-selected pairing
└── infra/
    ├── db.py               Connection, schema bootstrap, transactions
    ├── repositories.py     All SQL, one class per aggregate
    ├── audit.py            Event builder, hash chain, verification
    ├── reports/            Renderer and the one report template
    └── genai/              Provider-neutral adapter, offline stub, Gemini option
```

Revised in Stage 8: the web layer separates view functions from routes so every screen can be built and checked without a server (ADR-22); repositories are one module rather than a package (ADR-29); the evaluation service arrives with the evaluation stage.

## 3. Technology

Every dependency is free, installs with pip, and earns its place.

| Choice | Used for | Why this, not the alternative |
|---------------|--------------|------------------------------------------------------------|
| Python 3.11+ | Whole system | Standard library covers CSV, hashing, JSON and SQLite; no compiled dependencies |
| FastAPI | HTTP routing | Typed request handling and automatic input validation; Flask would need the same checks written by hand. Brings `python-multipart` for form posts and file uploads, and uses `itsdangerous` to sign the session cookie |
| Uvicorn | Server | Single-process local server, no configuration |
| Jinja2 | HTML rendering | Server-rendered pages keep the whole request path inspectable; a front-end framework would add build tooling without changing what reviewers see. Also renders the 13 reports |
| Standard library `sqlite3` | Database access | The schema is the source of truth and repositories write explicit SQL, so an abstraction layer would wrap `sqlite3` without replacing anything. Parameterized statements, transactions and row factories are all native (revised in Stage 6; see ADR-01) |
| SQLite | Storage | Single file, zero setup, supports triggers and CHECK constraints, which is where the controls live |
| `app/domain/text.py` | Text similarity | One token-set function written on `difflib`, about forty lines. A dependency for a single function is hard to justify, and bank abbreviations that drop vowels (SLVR for SILVER) need a subsequence test that off-the-shelf ratios handle poorly (revised in Stage 6) |
| scikit-learn | Confidence calibration | Isotonic regression is exactly the calibration tool needed. Used for nothing else, and never for matching decisions (DD-11) |
| pytest, httpx | Tests | Standard test runner plus an HTTP client for route tests |

Deliberately not used: an ORM, a migrations framework, a task queue, Docker, a front-end framework, JavaScript beyond two one-line form submits, a cloud service, or any paid API. Matching runs on the standard library plus scikit-learn; Jinja2 and the web packages are needed only for reports and the interface.

## 4. Data Access

| Point | Design |
|---------------|---------------------------------------------------------------------|
| Source of truth | `db/schema.sql`. On startup the connection executes it if the database is empty |
| Statements | Bound parameters only; table and column names come from repository code, never from input; no string-built SQL anywhere |
| Repositories | One class per aggregate (users, accounts, runs, files, items, validation, normalization, recommendations, decisions, adjustments, period, reports) in `infra/repositories.py`. Services never write SQL |
| Transactions | The service owns the boundary. Each request opens one transaction; the change and its audit event commit together, or neither does (CR-08) |
| Reads that guard a write | Taken on the transaction's own connection. A permission or separation-of-duties check therefore sees exactly the uncommitted state the write will change; status changes read the previous status the same way |
| Request handling | One SQLite connection, one request at a time: handlers are asynchronous and never await during database work (ADR-21) |
| Write mode | `BEGIN IMMEDIATE` for any write, so the audit sequence number cannot be allocated twice |
| Journal mode | WAL, so reading a report does not block a decision |
| Identifiers | Database-assigned integers. Display identifiers (BT-, GL-, CI-) come from the source files and are kept as given |

## 5. Audit and Hash Chain

### 5.1 Writing an event

1. The service calls `audit.write(event)` inside its open transaction.
2. The writer takes the next `sequence_no` for the run as `MAX(sequence_no) + 1`.
3. It serializes the event to canonical JSON: keys sorted, UTF-8, no insignificant whitespace, integers for money, ISO 8601 for dates.
4. It computes `event_hash = SHA-256(previous_hash + "|" + canonical_json)`, where `previous_hash` is the previous event's hash, or the literal `GENESIS` for the first event in a run.
5. It inserts the row. Triggers make the row immutable from that point (CR-09).

Because both the change and the event are inside one transaction, a failed audit write rolls back the action that caused it.

### 5.2 Verifying the chain

Verification reads events in sequence order, recomputes each hash from the stored content and the previous hash, and stops at the first mismatch. It reports events checked, the chain-head hash, and the first broken sequence number if any. The result appears on the audit screen, blocks close when broken (CR-10), and is printed on the signed Reconciliation Summary (FR-AUD-07).

### 5.3 Stated limits

Trigger-based protection applies to changes made through this application's connection. Anyone holding the database file can rebuild it, including the chain. What makes that detectable is the chain-head hash recorded in the signed summary and in the exported report package, which is kept outside the database. This is weaker than write-once storage and is described as such in every document and in the code comments (DD-06).

## 6. Matching Pipeline

Order matters: deterministic rules run first, so scoring never sees an item a rule already settled.

| Step | Module | Design |
|------------------|---------------------------------|--------------------------------------------------|
| 1. Exact rules | `domain/rules.py` | Equal amount in cents, same signed cash effect, equal non-empty normalized reference, same business date, and exactly one candidate on each side (BR-01, BR-02) |
| 2. Bank-originated | `domain/rules.py` | Bank type code in FEE, INT, RTN, or description keyword match, with no ledger counterpart (BR-06) |
| 3. Carry-in clearing | `domain/rules.py` | Current-period bank item matched to a carry-in item on amount, direction and reference (BR-11) |
| 4. Candidate generation | `domain/candidates.py` | Blocking on signed effect and date window (PRM-02), then amount: exact amount first, then within the near-miss tolerance. At most PRM-04 candidates per item |
| 5. Group search | `domain/candidates.py` | Depth-limited search up to PRM-05 members, pruned by remaining amount and date window, stopping at the first five solutions. If the search hits either cap, the item becomes EXC-08 rather than being dropped (DD-07) |
| 6. Scoring | `domain/features.py`, `scoring.py` | Five features, each normalized to 0..1, combined as a weighted sum |
| 7. Calibration | `domain/calibration.py` | Isotonic regression maps score to a calibrated probability, fitted on the July dataset only (DD-11) |
| 8. Risk | `domain/risk.py` | Rule-based, inspectable, never learned |
| 9. Explanation | `domain/explanation.py` | Sentence templates over feature values, plus conflict detectors |
| 10. Routing | `services/matching_service.py` | Category assigned by BR-13, first match wins, High risk checked first |

### 6.1 Features

| Feature | Definition | Weight (default) |
|---------------|-----------------------------------------------------------|------------|
| Date distance | `exp(-business_day_gap / PRM-02)`; 1.0 on the same day | 0.20 |
| Amount similarity | `1 - abs(a - b) / max(a, b)`; 1.0 when equal in cents | 0.30 |
| Text similarity | RapidFuzz token-set ratio over normalized description and payee, scaled to 0..1 | 0.25 |
| Reference similarity | 1.0 when normalized references are equal and non-empty, partial ratio otherwise, 0 when either is missing | 0.20 |
| Relationship type | 1.0 for one-to-one, 0.8 for a group | 0.05 |

Weights live in configuration and are tuned on the calibration dataset only. The evaluation dataset is never used to choose them (DD-11).

### 6.2 Risk rules

| ID | Condition | Level |
|-----------|--------------------------------------------------------|-------------|
| RR-01 | Amount at or above PRM-01 | High |
| RR-02 | Suspected duplicate (BR-07) | High |
| RR-03 | Unexplained item (EXC-07) | High |
| RR-04 | Counterparty not seen earlier in the period or in carry-in | Medium |
| RR-05 | Group match (one-to-many or many-to-one) | Medium |
| RR-06 | Stale item (BR-10) | Medium |
| RR-07 | None of the above | Low |

The highest triggered level applies, and every triggered rule is stored with the recommendation and shown to the reviewer.

### 6.3 Explanations and conflicting evidence

Supporting sentences are generated from the feature values that contributed most. Conflict detectors run separately and always report, whatever the score:

| Detector | Raises |
|-------------------|-------------------------------------------------------------|
| Close runner-up | Second candidate within PRM-08 of the first |
| Repeated amount | The same amount appears more than once on the other side within the window |
| Reference mismatch | Both references present and different |
| Date at the edge | Gap equals the PRM-02 limit |
| Group tolerance | Group sums exactly but spans more than two business days |
| Novel counterparty | Counterparty not seen before in this period |

If the optional GenAI adapter is enabled, its prose is stored in a separate field, labelled in the interface, and shown beside the deterministic explanation, never in place of it (CR-13).
## 7. Review and Approval

| Point | Design |
|------------|-----------------------------------------------------------------------|
| Queue order | Risk descending, then confidence ascending, then identifier. The least certain work surfaces first |
| No filtering | Low-confidence items cannot be hidden. The interface offers queues and ordering, never a confidence filter (CR-15) |
| Senior queue | CAT-05 plus every escalated item, wherever it was first routed (FR-REV-08) |
| Detail required | Approving or modifying a non-exact item requires the reviewer's own DETAIL_OPENED event. The service reads it from the audit log, never from a value the browser sends (FR-REV-10) |
| Timing | `opened_at` comes from that event, `decided_at` from the decision. Both are stored on the decision, so time per item is measured (DD-04) |
| Comment rule | Reject, modify, escalate, unresolved and any High-risk approval (CR-16) |
| Offered actions | Per category and level (FR-REV-04). Reject and modify apply to matches only. Duplicates, unexplained items and capped searches (EXC-05, EXC-07, EXC-08) cannot be approved: approving would mark unexplained money reconciled (ADR-25) |
| Approve | The leading candidate, or the exception disposition. No candidate is preselected on screen |
| Modify | Selects another candidate of the same recommendation; the original and all candidates stay unchanged |
| Reject | Marks the recommendation superseded and re-proposes each released record as an exception, classified by the matching rules (ADR-24) |
| Escalate | Item status escalated; the escalating user is read later for CR-03 |
| Manual pairing | For exceptions only. The reviewer selects records from the other side; the system checks they are free and agree to the cent, adds the pairing as an unscored candidate, and records a modify decision (ADR-23) |
| Settling a record | Approve, modify and unresolved settle every other open proposal for the same records: an exception raised for the record is superseded; another open match refuses the decision. Superseding a CAT-05 or escalated exception requires an independent senior (ADR-26) |
| Refusals | Every check runs inside the transaction. A refusal rolls back, then a BLOCKED_ATTEMPT event names the rule (ADR-12) |

### 7.1 Batch approval (DD-02)

A batch holds open CAT-01 items with Low risk only, all ticked by default, and the reviewer may untick any. One submission opens one transaction and, per item, writes a decision and an audit event; a batch rejection writes one rejection per item and re-proposes each released record. One ineligible item refuses the whole batch rather than being skipped silently (CR-11). The batch identifier is stored on each decision, so reports show both the action and the individual records.

## 8. Period Control

| Step | Design |
|------------|-----------------------------------------------------------------------|
| Statement | `period_service.statement()` builds the bank-to-book statement from the reviewer's dispositions, not the ground truth: an item left unresolved is listed as unresolved whatever its proposed category; a hand-paired exception is a match. The arithmetic is in `domain/statement.py` and is shared with RPT-09 (DD-12) |
| Readiness | `period_service.readiness()` evaluates each CR-10 condition separately. Condition 5 requires a package generated after the latest change and no approved item left unverified |
| Ready to close | A system transition between `in_review` and `ready_to_close`, re-evaluated after every package, with a STATUS_CHANGED event |
| Sign-off | Controller only, from `ready_to_close`, all conditions met, CR-05, and a comment when the difference is non-zero. Records the statement and the chain head as it stood at signing, then writes SIGNOFF and PERIOD_LOCKED |
| Lock | Enforced in `control/period_lock.py` and again by database triggers for decisions, adjustments and imports. Adjustment decisions have no run column, so their lock is the service check alone (ADR-30) |
| Reopen | A request with a reason, and a separate decision by a Controller who is not the requester (CR-06). A pending request does not unlock the period. Approval returns the run to `in_review`; both states are recorded |
| Re-close | Needs a new package and a new sign-off; the earlier sign-off stays on record |

## 9. Report Generation

| Point | Design |
|------------------|-------------------------------------------------------------------|
| Building | `report_builders.py` reads the run once and returns each report as data: summary figures, tables and notes. Ground truth is never read |
| Rendering | One Jinja2 template renders all 13 reports to HTML, and the same data is written to CSV (ADR-27) |
| Package | `report_service.generate_package()` builds RPT-01..13, runs the report check, writes both formats under `reports/run_<id>/`, stores each file with its content hash, and writes one REPORT_GENERATED event |
| Content hash | SHA-256 of the report's data in canonical JSON; the generation timestamp is excluded, so an unchanged report regenerates with the same hash (FR-RPT-04, ADR-14). RPT-13 changes with every package because generating one adds an event |
| Report check | Each approved item must appear in the Match or Exception Report with its amount and an approved outcome. When all six CR-07 conditions hold it moves `approved` → `report_verified` → `reconciled`, with one REPORT_VERIFIED event per recommendation. Nothing else sets `reconciled` (FR-RPT-05) |
| After close | A package can be regenerated; it changes no item status, and RPT-09 then shows the sign-off and the chain head recorded at signing (FR-AUD-07) |
| PDF | Produced by printing the HTML from the browser; no PDF library is required (NOP-09) |

## 10. API Design

HTML routes serve the reviewer interface; two JSON routes exist where a machine-readable result is useful. Every route that changes data is a POST, calls one service method inside one transaction, and writes an audit event. Every page is viewable by any identity; actions an identity may not take are shown disabled with the rule that blocks them.

| Method and path | Purpose | May act | Audit event |
|---------------------------------------------|-----------------------|-------------|----------------------|
| GET `/` | Run dashboard (W1) | — | — |
| POST `/identity` | Select acting identity | any | IDENTITY_SELECTED |
| GET `/import` | New run form (W2) | — | — |
| POST `/runs` | Create a run | ROL-01, ROL-02 | — |
| GET `/runs/{run_id}/import` | Import and validation (W2) | — | — |
| POST `/runs/{run_id}/import` | Upload the CSV files | ROL-01, ROL-02 | IMPORT or BLOCKED_ATTEMPT |
| POST `/runs/{run_id}/match` | Validate, normalize, match and score | ROL-01, ROL-02 | VALIDATION, NORMALIZATION, RULE, AI_RECOMMENDATION, ROUTING |
| GET `/runs/{run_id}/queues/{category}` | Queue listing; CAT-05 is the senior queue (W6) | — | — |
| GET `/items/{recommendation_id}` | Item detail (W3, W5) | — | DETAIL_OPENED |
| POST `/items/{recommendation_id}/decision` | Approve, modify, reject, escalate, unresolved | ROL-01..03 by level | DECISION or BLOCKED_ATTEMPT |
| POST `/items/{recommendation_id}/manual-match` | Pair an exception by hand | ROL-01..03 by level | DECISION or BLOCKED_ATTEMPT |
| GET `/runs/{run_id}/batch` | Exact-match batch (W4) | — | — |
| POST `/runs/{run_id}/batch` | Batch approve or reject | ROL-01, ROL-02 | DECISION per item |
| POST `/items/{recommendation_id}/adjustment` | Propose an adjustment | ROL-01, ROL-02 | ADJUSTMENT_PROPOSED |
| GET `/runs/{run_id}/adjustments` | Adjustment approvals | — | — |
| POST `/adjustments/{adjustment_id}/decision` | Approve or reject an adjustment | ROL-02, ROL-03 | ADJUSTMENT_DECIDED or BLOCKED_ATTEMPT |
| GET `/runs/{run_id}/close` | Statement, conditions, sign-off, reopening (W7) | — | — |
| POST `/runs/{run_id}/close` | Sign off and lock | ROL-03 | SIGNOFF, PERIOD_LOCKED |
| POST `/runs/{run_id}/reopen-request` | Request reopening | ROL-01, ROL-02 | REOPEN_REQUESTED |
| POST `/reopen-requests/{id}/decision` | Approve or reject reopening | ROL-03 | REOPEN_DECIDED or BLOCKED_ATTEMPT |
| GET, POST `/runs/{run_id}/reports` | Package index; generate the package | any identity | REPORT_GENERATED, REPORT_VERIFIED |
| GET `/runs/{run_id}/reports/{report_code}` | View a report | — | — |
| GET `/runs/{run_id}/reports/{report_code}.csv` | Export a report | — | REPORT_EXPORTED |
| GET `/runs/{run_id}/audit` | Audit log and verification (W8) | — | — |
| GET `/api/runs/{run_id}/audit/verify` | Chain verification as JSON | — | CHAIN_VERIFIED |
| GET `/api/runs/{run_id}/status` | Run status and queue counts as JSON | — | — |

Reviewer decisions, adjustments, sign-off and reopening exist only in the interface, never on the command line, so there is one path to each and it goes through the same controls (ADR-31). Evaluation will be a command line entry point, not a route, because it reads ground truth (ADR-11).

## 11. Validation and Error Handling

| Class | Raised as | Behaviour |
|-------------|-----------------|-----------------------------------------------------------|
| File validation | VAL-nn results | File-level failures leave the run in `validation_failed` and block matching (CR-17); row-level failures exclude the row; warnings are kept for review. The correction is a new run (ADR-28) |
| Input problems | `AdjustmentInputError`, `ManualMatchError`, `PeriodInputError`, `RunInputError`, `ImportError_` | Every problem listed at once, nothing written, the form is shown again. Not a control, so no BLOCKED_ATTEMPT event |
| Control refusal | `ControlViolation`, E-CTL-001..030 | Permission, level, offered action, comment, detail view, separation of duties, batch contents, lock, stage, state transition. Rolled back; a BLOCKED_ATTEMPT event names the rule; the screen shows rule and code |
| Conflict | E-CTL-030 (STATE) | Already decided, already closed, a record another match proposes, a run at the wrong stage. Handled as a control refusal, so it is also evidenced (revised in Stage 8: one class instead of a separate E-CON range) |
| Not found | 404 page | Unknown run, recommendation, report or queue |
| System | Server error | The transaction rolls back and nothing is saved; the stack trace stays in the server console |

Rules that apply everywhere:

- No partial writes. Every handler wraps its work in one transaction.
- An audit write failure rolls back the action that caused it (CR-08).
- Messages name the rule or test that failed and never blame the user.
- Stack traces never reach the screen.

## 12. Security

The prototype runs locally on synthetic data with no authentication (DD-05). Within that scope:

| Concern | Design |
|----------------|-----------------------------------------------------------------|
| Identity | Selected from a list, stored in a signed session cookie, recorded on every decision and as IDENTITY_SELECTED. Not authentication, and documented as such. The signing key comes from `BANKREC_SESSION_KEY`, or is generated per start |
| Authorization | Every state-changing route calls a service, which checks role and control rules. The interface shows unavailable actions disabled with the blocking rule; the screen is guidance, never the control |
| Injection | Parameterized statements only; no string-built SQL |
| Cross-site scripting | Jinja2 autoescaping on; bank descriptions are treated as untrusted text |
| Cross-site request forgery | The session cookie is `SameSite=Lax`, so browsers do not send it with a form posted from another site; such a post arrives with no identity and is refused. No separate form token (revised in Stage 8: the application listens on 127.0.0.1 only) |
| File upload | Parsed with the standard CSV reader, never evaluated, and validated before anything is matched. Written to a temporary directory under fixed names, so the uploaded file name is never used as a path. No upload size limit: a stated limit of a local prototype |
| Secrets | The optional provider key is read from `.env`, which `.gitignore` excludes. No key is required to run the system |
| External calls | Disabled by default. Enabling requires both a configuration flag and a `data_classification = synthetic` assertion; the adapter refuses to send otherwise (NFR-05) |
| Logging | No file contents in logs; identifiers only |
| Database | Local file; the append-only triggers and hash chain are the integrity control, with limits stated in section 5.3 |

## 13. Roles and Permissions

Checks are implemented once, in `app/control`, and called by services rather than routers, so a new route cannot bypass them.

| Action | ROL-01 | ROL-02 | ROL-03 | Rule |
|-------------------------------|------------|------------|------------|------------|
| Import and start a run | yes | yes | no | CR-17 |
| Decide CAT-01..04 items | yes | yes | no | CR-01 |
| Batch approve CAT-01 | yes | yes | no | CR-11 |
| Escalate | yes | yes | no | — |
| Decide CAT-05 or escalated | no | yes | yes | CR-02, CR-03 |
| Pair an exception by hand | by level | by level | senior level only | CR-02, CR-03 |
| Propose an adjustment | yes | yes | no | — |
| Approve an adjustment | no | yes | yes | CR-04 |
| Sign off and close | no | no | yes | CR-05, CR-10 |
| Request reopening | yes | yes | no | — |
| Approve reopening | no | no | yes | CR-06 |
| View reports and audit log | yes | yes | yes | — |

Separation-of-duties checks compare user identifiers across records: the escalating user on the item, the preparer on the adjustment, the first-level reviewers in the run, and the requester on the reopen request.

CR-05 and CR-06 cannot be reached through normal use, because the matrix already keeps the Controller out of first-level review and out of reopen requests. Both checks are still enforced and tested directly, as defence in depth.

## 14. Configuration

| Source | Holds |
|------------------------|----------------------------------------------------------|
| `config/default.toml` | PRM-01..10, feature weights, model version label, report output directory |
| `config/local.toml` | Optional overrides, ignored by Git |
| `.env` | Optional GenAI provider key and the synthetic-data assertion flag |

At run creation the effective values are copied into `reconciliation_run.parameter_snapshot`. Reports and the evaluation read the snapshot, not the current configuration, so a later change cannot silently alter what a closed period reported (NFR-01).

## 15. Performance

| Point | Design |
|--------------|---------------------------------------------------------------------|
| Target | A full run from import to report package in under two minutes on a laptop (NFR-07) |
| Candidate generation | Blocked by signed effect, date window and amount, using the indexes in `schema.sql`, so each item compares against tens of rows rather than the whole period |
| Group search | Depth-limited and pruned, capped by PRM-04 and PRM-05, with early exit after five solutions |
| Text similarity | Computed only for candidates that survive blocking |
| Reports | Single pass per report with indexed queries |
| Measurement | The run records elapsed time per stage in the audit log, so the evaluation reports real figures rather than estimates |
| Measured | Import to routed queues for the August data: about half a second. A 13-report package: about a third of a second. Both on a laptop, well inside NFR-07 |

## 16. Testing Approach

| Level | Scope | Identifiers |
|------------------|-----------------------------------------------------------|--------------|
| Unit | Domain and control functions with fixed inputs | TC-U-nnn |
| Integration | Service plus database, and the web layer through view functions and HTTP routes | TC-I-nnn |
| Control | One test per control rule, asserting the refusal, the rule named, and the BLOCKED_ATTEMPT event where one applies | TC-C-nnn |
| Scenario | SCN-01..08 against the labelled dataset, and the complete August reconciliation | TC-S-nnn |
| Reproducibility | Same seed yields identical data, recommendations and report hashes | TC-R-nnn |

The test plan, the coverage of every control rule, and the results are in DOC-06.

## 17. Design Decisions

Decisions DD-01..DD-12 are recorded in DOC-01 and are all settled as the working design. DD-01 (the false automatic match rate reported as a hypothetical) and DD-02 (one batch action, one record per item) are implemented as described there; both remain isolated, in the evaluation and in `approve_batch` and the W4 screen respectively.

| ID | Decision | Reason | Consequence |
|---------|------------------|--------------------------------|--------------------------|
| ADR-01 | Schema in SQL, accessed through the standard library | Constraints and triggers are the control layer; restating them in Python would create two sources of truth. With no ORM and no migrations, SQLAlchemy would wrap `sqlite3` rather than replace it (revised in Stage 6) | `sqlite3` with explicit SQL in repositories; no migrations framework |
| ADR-02 | Separate control layer | One implementation and one test per control rule | Services must call control functions; routers never check rules themselves |
| ADR-03 | Server-rendered HTML | Keeps the request path inspectable and matches the wireframes | No client-side state; full page loads |
| ADR-04 | Domain layer performs no input or output | Reproducible, testable rules and scoring | Services assemble data and persist results |
| ADR-05 | Rules before scoring | Scoring never re-opens what a deterministic rule settled | Exact matches carry no confidence value |
| ADR-06 | Calibration fitted on a separate dataset | Fitting and evaluating on the same data overstates accuracy | A second dataset must be generated and maintained |
| ADR-07 | Risk rules stay deterministic | Risk is control policy and must be inspectable and changeable without retraining | Risk levels cannot adapt to data; that is the intent |
| ADR-08 | Canonical JSON for hashing | Byte-stable serialization makes verification reproducible | Event content must avoid unordered structures |
| ADR-09 | `BEGIN IMMEDIATE` for writes | Prevents two events claiming the same sequence number | Writes serialize; acceptable for single-account use |
| ADR-10 | Parameter snapshot per run | A configuration change must not alter what a closed period reported | Reports read the snapshot, not the live configuration |
| ADR-11 | Evaluation as a command, not a route | Ground truth must stay out of the application surface | Evaluation runs offline and is tested separately |
| ADR-12 | Blocked attempts are audited | Refused actions are evidence that controls work | Extra events in the log, labelled BLOCKED_ATTEMPT |
| ADR-13 | GenAI adapter in infrastructure | Nothing in the domain layer can reach it | Prose is stored in a separate field and labelled |
| ADR-14 | Reports hashed on content | Lets a report be shown unchanged since generation | The generation timestamp is excluded from the hash |
| ADR-15 | Elapsed time recorded per stage | Performance is reported from measurement, not estimated | Extra timing events in the audit log |
| ADR-16 | Text similarity written in the project, not imported | One function, and the abbreviation case needs a subsequence test; a reviewer asking why two names scored 0.94 can read the answer | Forty lines to maintain, and no library to upgrade |
| ADR-17 | Group search by meet in the middle | Keeping only the largest candidates is the wrong slice: a deposit made of four mid-sized receipts would never be found. Indexing subset sums searches the whole window in quadratic time | Groups of five or more members remain out of reach, and that limit is reported as EXC-08 |
| ADR-18 | Confidence clamped to [0.02, 0.98] | A finite sample cannot establish certainty, and an interface showing "100% confident" invites the over-trust the control design exists to prevent | No recommendation can ever display as certain |
| ADR-19 | Hard negatives added when fitting calibration | Candidate generation is narrow, so its output alone is almost all correct and teaches a calibrator nothing about the middle of the range | The fitting set is not the production distribution, which is recorded in the artefact notes |
| ADR-20 | A missing calibration artefact is not an error | A run proceeds and labels itself uncalibrated, so no report can present an unfitted score as a calibrated confidence | Two possible states to handle in reports |
| ADR-21 | One connection, one request at a time | A local, single-team prototype gains nothing from a pool, and serial handling makes every check-then-write atomic without extra locking | The connection allows use across threads; a slow request delays the next |
| ADR-22 | View functions separate from routes | Every screen can be built and its template rendered in tests without a server | Routes are a few lines each; screen logic lives in `web/views.py` |
| ADR-23 | Manual pairing for exceptions | Where the group search hit its cap, or missed a many-to-one group, reviewers otherwise had no way to correct it; the best achievable difference on the August data was -$77,663.05 against a true -$9,392.35 | A reviewer-selected candidate with no score sits beside the system's candidates, recorded as modify; the arithmetic must agree to the cent |
| ADR-24 | Reject re-proposes as exceptions; modify selects a candidate | One verb per intent: "this pairing is wrong" and "that other pairing is right" | Re-proposed records are classified by the same rules as matching |
| ADR-25 | Investigate-only exceptions cannot be approved | Approving a duplicate, an unexplained item or a capped search would mark unexplained money reconciled | Those items are paired by hand, escalated, or left unresolved and carried on the statement. Approved timing and bank-originated items count as reconciled |
| ADR-26 | Settling a record settles every other proposal for it | Matching can leave a record both in a match and flagged as a possible duplicate; two approvals must never claim one record | Exceptions for the record are superseded; competing matches refuse the decision; a senior is required when the superseded item needs one |
| ADR-27 | One template for all 13 reports | Consistent layout, and rendering stays out of report logic | Reports are data plus one template; layout changes are made once |
| ADR-28 | Files imported once per run | A second import into a failed run would hold two copies of a statement | A failed run stays as evidence; the correction is a new run |
| ADR-29 | Repositories in one module | Splitting into a package would change every import for no functional gain | Section 2.1 revised; one class per aggregate |
| ADR-30 | Adjustment decisions locked by the service only | The `adjustment_decision` table has no run column, so no trigger can see the period | The service check is the only lock for that table; tested directly and stated as a limit |
| ADR-31 | Decisions only through the interface | A command line shortcut would be a second, weaker path to the same actions | The command line covers setup, the automated pipeline and evidence |

## 18. Traceability

| Requirement group | Implemented in |
|---------------|------------------------------------------------------------------|
| FR-IMP | `services/import_service.py`, `services/run_service.py`, `web/routes/runs.py` |
| FR-VAL, FR-NRM | `services/validation_service.py` |
| FR-MAT | `domain/rules.py` |
| FR-AI | `domain/candidates.py`, `features.py`, `scoring.py`, `calibration.py` |
| FR-RSK, FR-EXC | `domain/risk.py`, `domain/explanation.py`, `services/matching_service.py` |
| FR-REV | `services/review_service.py`, `web/routes/review.py`, `web/views.py`, `control/permissions.py` |
| FR-ADJ | `services/adjustment_service.py`, `control/duties.py` |
| FR-AUD | `infra/audit.py`, schema triggers |
| FR-PER | `services/period_service.py`, `domain/statement.py`, `web/routes/period.py`, `control/period_lock.py` |
| FR-RPT | `services/report_service.py`, `services/report_builders.py`, `infra/reports/`, `control/completion.py` |
| FR-EVL | Evaluation command (evaluation stage) |
| FR-GAI | `infra/genai/` |
| CR-01..CR-18 | `app/control` plus schema constraints and triggers |
