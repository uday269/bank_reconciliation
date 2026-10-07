# Test Plan and Results — AI-Assisted Bank Reconciliation

## 1. Purpose and Scope

This document defines how the system is tested, shows which tests cover each control rule, scenario and requirement group, and records the results. It covers the whole system, including the evaluator. The measured results of the evaluation (all brief section 11 metrics, performance by confidence band, error analysis) are reported in DOC-07 Evaluation Report; this document records the tests that guard them.

## 2. Strategy

| Level | What it proves | ID | Tests |
|------------------|------------------------------------------------------------------|-----------|------:|
| Unit | Domain and control functions on fixed inputs: rules, features, scoring, calibration, risk, explanations, statement arithmetic, permissions, separation of duties, state models, audit completeness rules | TC-U-nnn | 71 |
| Integration | Services against a real database, the web layer through view functions and HTTP routes, and the evaluator | TC-I-nnn | 78 |
| Control | One or more tests per control rule: the refusal, the rule it names, and the BLOCKED_ATTEMPT event where one applies | TC-C-nnn | 65 |
| Hybrid | The AI layer against the labelled dataset: what scoring adds and what it must never do | TC-H-nnn | 7 |
| Scenario | SCN-01..08 against the labelled dataset, and the complete August reconciliation | TC-S-nnn | 8 |
| Reproducibility | Same seed and data give the same results; parameters are frozen on the run; the timing sample is seeded | TC-R-nnn | 3 |
| **Total** | | | **232** |

Counts are test cases; one unit test runs five parameterized cases.

Principles that apply to every test:

- **Real database, no mocks of the system under test.** Each test builds a fresh SQLite database from `db/schema.sql`, so triggers and CHECK constraints are exercised exactly as in use.
- **Seeded data.** The August dataset (1,240 transactions plus 18 carry-in items) and the July calibration dataset are generated from fixed seeds, so every run sees identical data.
- **Ground truth only where it belongs.** Scenario and hybrid tests read `ground_truth.csv`; the application code never does (ADR-11).
- **A control test checks both halves.** The action is refused with the right rule, and nothing is written except the evidence that the control fired.
- **Misbehaving dependencies are simulated on purpose.** The generative AI tests use scripted providers that decide, invent percentages or time out.

## 3. Environment and How to Run

| Item | Value |
|-------------|--------------------------------------------------------------|
| Platform | macOS, Python 3.13 |
| Packages | `requirements.txt` (pytest, FastAPI test client, Jinja2, scikit-learn) |
| Configuration | `pytest.ini`: test paths, and one filtered library notice from the web test client |
| Data | `data/august_2026/`, `data/july_2026_calibration/`, `models/calibration.json` |

```
pip install -r requirements.txt
python -m pytest -q                        # whole suite
python -m pytest tests/test_control.py -q   # one file
python -m pytest -k tcc_038 -q              # one test by ID
```

## 4. Test Inventory

| File | Covers | Tests |
|-------------------------|-------------------------------------------------------------|------:|
| `test_domain.py` | Parsing, normalization, deterministic rules, risk and routing | 27 |
| `test_scoring.py` | Text similarity, features, candidate and group search, ranking, explanations, calibration | 32 |
| `test_pipeline.py` | Import to routed queues; scenarios SCN-01..08 on the baseline; hybrid layer; reproducibility | 22 |
| `test_controls.py` | Audit immutability and chain, rollback, period lock on imports, validation gates | 14 |
| `test_repositories.py` | Connection-scoped reads, decision, adjustment, period and report records | 11 |
| `test_control.py` | Permission matrix, levels, offered actions, comment rule, separation of duties, state models, close conditions, six-condition rule | 21 |
| `test_review.py` | Decisions, escalation, modify, batches, blocked attempts | 18 |
| `test_manual_match.py` | Hand pairing, and the statement tie-out after a correct review | 9 |
| `test_adjustments.py` | Proposal, approval, CR-04, never posted | 11 |
| `test_period.py` | Statement arithmetic, readiness, sign-off, lock, reopening | 11 |
| `test_reports.py` | Package, content hashes, report check, export, the complete reconciliation | 8 |
| `test_genai.py` | Optional prose: decision-language guard, fallback, audit, nothing else changed | 8 |
| `test_web_views.py` | Every screen's data and template, without a server | 17 |
| `test_web_routes.py` | The HTTP layer: identity, decisions, refusals, batch, adjustments, pairing, import, close | 13 |
| `test_evaluation.py` | The evaluator: figures pinned to the benchmarks, review measures, audit completeness rules, timing sample, never writes to the working database | 8 |
| `test_cli.py` | Command line: init, run, status, report, verify, reset | 2 |

## 5. Control Rule Coverage

Every control rule has at least one test that tries to break it.

| Rule | What is tested | Tests |
|--------|-------------------------------------------------------------|------------------------------|
| CR-01 | Nothing is approved by confidence; every item needs a recorded human decision before it can close | TC-I-007, TC-I-039, TC-C-034, TC-S-008 |
| CR-02 | Staff cannot decide CAT-05 or escalated items, including by settling one indirectly through modify or hand pairing | TC-C-020, TC-C-037, TC-C-056, TC-I-019, TC-I-054 |
| CR-03 | The escalator and first-level reviewers cannot make the senior decision; shown on screen | TC-C-026, TC-C-038, TC-I-055 |
| CR-04 | The preparer cannot approve the adjustment | TC-C-027, TC-C-043, TC-I-058 |
| CR-05 | The signer cannot be the only first-level reviewer | TC-C-028, TC-C-051 |
| CR-06 | The reopen approver cannot be the requester | TC-C-029, TC-C-052 |
| CR-07 | Each of the six conditions alone blocks reconciliation; only the report check sets `reconciled` | TC-U-158, TC-U-159, TC-C-030, TC-C-060, TC-I-039 |
| CR-08 | A failed action rolls back its audit event | TC-C-003 |
| CR-09 | Audit and decision records cannot be updated or deleted; tampering breaks the chain | TC-C-001, TC-C-002, TC-C-006, TC-C-014, TC-C-015 |
| CR-10 | Close is refused until every condition holds; each unmet condition is listed; a difference needs a comment | TC-C-016, TC-C-034, TC-C-049, TC-C-050, TC-U-157, TC-I-031 |
| CR-11 | Batches hold only open, Low-risk exact matches; one ineligible item refuses the whole batch; one record per item | TC-C-025, TC-C-039, TC-I-011, TC-I-021 |
| CR-12 | Approving an adjustment changes no ledger record | TC-I-029 |
| CR-13 | Generated prose cannot carry a decision, score or invented figure, and changes no other field | TC-U-165, TC-U-166, TC-U-167, TC-I-071, TC-I-072, TC-I-073 |
| CR-14 | No retraining from decisions: the calibration artefact is written only by the fitting script, versioned, and recorded on every scored match | TC-U-155, TC-U-156, TC-H-004, and inspection (no other code path writes the artefact) |
| CR-15 | Every candidate and all conflicting evidence are kept and shown; no queue hides low confidence | TC-U-140..142, TC-H-003, TC-H-005, TC-I-042, TC-I-053 |
| CR-16 | Reject, modify, escalate, unresolved and High-risk approval need a comment | TC-C-023, TC-C-036, TC-C-048, TC-C-058 |
| CR-17 | Matching cannot start before validation; a file-level failure blocks the run | TC-C-007, TC-C-008, TC-C-032, TC-I-065 |
| CR-18 | No import, decision, adjustment or prose in a closed period, or while a reopening is pending | TC-C-004, TC-C-005, TC-C-017, TC-C-032, TC-C-040, TC-C-047, TC-C-061, TC-C-064 |

Other controls tested: role matrix (TC-C-019, TC-C-021, TC-C-044, TC-C-062), detail view before approval (TC-C-024, TC-C-035, TC-I-023), offered actions per category (TC-C-022, TC-C-042, TC-C-059), state models (TC-C-030, TC-C-031, TC-C-041), duplicate import (TC-C-009), original values kept (TC-C-010), generative AI off by default and synthetic-only (TC-C-063).

## 6. Scenario Coverage

| Scenario | Tests |
|-----------|---------------------------------------------------------------|
| SCN-01 Exact match | TC-S-002, TC-U-010, TC-U-011, TC-I-021 |
| SCN-02 Timing difference | TC-H-001, TC-H-002, TC-U-110 |
| SCN-03 Name variation | TC-U-100..103, TC-H-001 |
| SCN-04 One to many | TC-U-121, TC-U-122, TC-H-006 |
| SCN-05 Many to one | TC-S-007, TC-I-035, TC-I-063 |
| SCN-06 Bank-originated item | TC-S-003, TC-I-025..029 |
| SCN-07 Duplicate | TC-S-004, TC-U-012, TC-C-059 |
| SCN-08 Unexplained item | TC-S-005, TC-I-057, TC-C-059 |
| Large items | TC-S-006, TC-I-018 |
| Complete reconciliation | TC-S-008, TC-I-068 |

## 7. Requirement Coverage

| Group | Tested in |
|----------|---------------------------------------------------------------|
| FR-IMP, FR-VAL, FR-NRM | `test_pipeline.py`, `test_controls.py`, `test_domain.py`, `test_web_views.py` (W2) |
| FR-MAT, FR-AI | `test_domain.py`, `test_scoring.py`, `test_pipeline.py` |
| FR-RSK, FR-EXC | `test_domain.py`, `test_pipeline.py` |
| FR-REV | `test_review.py`, `test_manual_match.py`, `test_control.py`, `test_web_*.py` |
| FR-ADJ | `test_adjustments.py` |
| FR-AUD | `test_controls.py`, `test_repositories.py`, every service test through BLOCKED_ATTEMPT |
| FR-PER | `test_period.py`, `test_reports.py` |
| FR-RPT | `test_reports.py`, `test_cli.py` |
| FR-GAI | `test_genai.py` |
| FR-EVL | `test_evaluation.py` |

## 8. Results

| Item | Result |
|-------------|--------------------------------------------------------------|
| Suite | **232 passed, 0 failed** |
| Duration | About 95 seconds on a laptop; the complete-reconciliation tests take most of it |
| Warnings | None |

Figures the suite holds in place, so a change that moves them fails a test:

| Figure | Value | Guarded by |
|-------------------------|----------------------------------------------|-------------------|
| Rules-only baseline | Precision 1.0000 (390/390), recall 0.6866 | TC-S-001, TC-I-074 |
| Rules plus scoring | Precision 1.0000 (556/556), recall 0.9789 (556/568); hypothetical false automatic match rate 0/467 | TC-H-001, TC-H-002, TC-I-074, TC-I-075 |
| Routed queues | 378 / 99 / 63 / 105 / 30 across CAT-01..05 (675 recommendations) | TC-I-041, TC-I-067 |
| Statement after a correct review | Unresolved difference **-$9,392.35**, exactly the dataset's | TC-I-030, TC-S-007, TC-S-008 |
| Complete reconciliation | Every item reconciled or unresolved; signed, locked, chain intact | TC-S-008 |
| Report hashes | Identical when regenerated from unchanged data | TC-I-038 |

## 9. Defects Found by Testing

Each of these was found by a test or by writing one, fixed, and is now pinned by a test.

| Defect | Found by | Fix |
|------------------------------------|------------------|--------------------------------------------|
| A bank description containing "COFFEE" was classified as a fee | TC-U-014, TC-U-015 | Keywords match whole words only |
| Status changes read the previous status on a different connection | TC-I-008, written to fail on the old code | Reads that guard a write use the transaction's connection |
| Selecting a different candidate could settle a high-risk exception without a senior | TC-I-019 | Superseding a CAT-05 or escalated exception requires an independent senior |
| Matching left four records proposed in a match and flagged as duplicates at once | TC-S-008 | Settling a record settles every other proposal for it |
| The statement still counted hand-paired exceptions as reconciling items | TC-S-007 | Exceptions decided by modify are treated as matches |
| Investigate-only exceptions could be approved, and so reconciled while unexplained | TC-S-008 | EXC-05, EXC-07 and EXC-08 cannot be approved |
| The Reconciliation Summary hash changed on every regeneration | TC-I-038 | It shows the chain head recorded at sign-off, not the live head |
| The generative AI adapter was built but never called, and its guards were untested | Coverage review of CR-13 | Wired as an optional reviewer action and tested with misbehaving providers |
| The calibration benchmark passed calibrated confidence through the calibrator again, reporting an error of 0.0836 instead of 0.1234 | Writing the evaluator's calibration measure | Both now assess the raw score once; figures pinned by TC-I-074 |

## 10. Not Covered, and Why

| Area | Status |
|----------------------|----------------------------------------------------------------|
| Browser-level interface testing | Screens are tested through their data and rendered templates, and through HTTP. Layout is checked by hand against the wireframes; no browser automation is used |
| Concurrent users | Not applicable by design: one connection, one request at a time (ADR-21) |
| Cross-site request forgery, upload size | Stated limits of a local prototype (DOC-05 section 12); not tested |
| Lock on adjustment decisions at database level | No trigger exists for that table (ADR-30); the service lock is tested directly (TC-C-047) |
| Time per item | Measured in timed review sessions and reported in DOC-07 (DD-04); the suite checks that timings are recorded and attributed (TC-I-015, TC-I-076) |
| Matching quality metrics | Measured by the evaluator and reported in DOC-07; the suite pins the figures that must not regress |
