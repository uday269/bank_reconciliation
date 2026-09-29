"""Control tests (TC-C): one test per control rule, asserting the refusal.

These are the tests that matter most. Each one proves a control cannot be bypassed,
which is the claim the whole project rests on.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.infra.audit import AuditEvent, AuditError, AuditLog, RunContext, compute_hash
from app.infra.db import AppendOnlyViolation, DatabaseError, PeriodLockedError, insert, update
from app.infra.repositories import RepositoryError
from app.services.import_service import ImportError_
from tests.conftest import DATASET


def test_tcc_001_audit_events_cannot_be_updated(harness):
    """CR-09: corrections are new events, never edits."""
    run_id = harness.imported_run()
    with pytest.raises(AppendOnlyViolation):
        with harness.database.transaction() as connection:
            connection.execute("UPDATE audit_event SET comment = 'edited' WHERE run_id = ?", (run_id,))


def test_tcc_002_audit_events_cannot_be_deleted(harness):
    run_id = harness.imported_run()
    with pytest.raises(AppendOnlyViolation):
        with harness.database.transaction() as connection:
            connection.execute("DELETE FROM audit_event WHERE run_id = ?", (run_id,))


def test_tcc_003_a_failed_action_rolls_back_its_audit_event(harness):
    """CR-08: evidence and action commit together or not at all."""
    run_id = harness.imported_run()
    before = harness.audit.count(run_id)
    context = RunContext.load(harness.database, run_id)
    with pytest.raises(DatabaseError):
        with harness.database.transaction() as connection:
            harness.audit.write(connection, context,
                                AuditEvent("RULE", "P3", "recommendation"))
            insert(connection, "app_user", {"user_id": 1, "full_name": "duplicate",
                                            "role_code": "ROL-01", "created_at": "2026-09-01T00:00:00Z"})
    assert harness.audit.count(run_id) == before


def test_tcc_004_a_closed_period_refuses_new_decisions(harness):
    """CR-18 and FR-PER-03: after close, only a reopening can change anything."""
    run_id = harness.matched_run()
    recommendation = harness.repositories.recommendations.queue(run_id, "CAT-01")[0]
    with harness.database.transaction() as connection:
        update(connection, "reconciliation_run", {"status": "closed"}, "run_id = ?", (run_id,))
    with pytest.raises(PeriodLockedError):
        with harness.database.transaction() as connection:
            insert(connection, "review_decision", {
                "run_id": run_id, "recommendation_id": recommendation["recommendation_id"],
                "decision": "approve", "decision_level": "first",
                "decided_by": harness.users["Maya Castillo"], "decided_at": "2026-09-01T00:00:00Z",
                "previous_status": "proposed", "new_status": "approved"})


def test_tcc_005_a_closed_period_refuses_new_imports(harness):
    run_id = harness.new_run()
    with harness.database.transaction() as connection:
        update(connection, "reconciliation_run", {"status": "closed"}, "run_id = ?", (run_id,))
    with pytest.raises(PeriodLockedError):
        with harness.database.transaction() as connection:
            insert(connection, "source_file", {
                "run_id": run_id, "file_type": "bank", "file_name": "x.csv", "sha256": "z",
                "row_count": 1, "uploaded_by": harness.users["Maya Castillo"],
                "uploaded_at": "2026-09-01T00:00:00Z"})


def test_tcc_006_tampering_with_an_event_breaks_the_chain(harness):
    """DD-06: detection, not prevention. The triggers are removed to simulate direct access."""
    run_id = harness.matched_run()
    assert harness.audit.verify(run_id).intact

    path = harness.database.path
    harness.database.close()
    raw = sqlite3.connect(path)
    raw.execute("DROP TRIGGER audit_event_block_update")
    raw.execute("UPDATE audit_event SET comment = 'edited' WHERE sequence_no = 5")
    raw.commit()
    raw.close()

    result = harness.audit.verify(run_id)
    assert not result.intact
    assert result.first_broken_sequence == 5


def test_tcc_007_matching_is_refused_before_validation(harness):
    """CR-17: no matching until every file-level check passes."""
    run_id = harness.imported_run()
    with pytest.raises(ValueError):
        harness.matching.run_rules(run_id)


def test_tcc_008_file_level_failure_blocks_the_run(harness, dataset_copy):
    """CR-17: a wrong control total stops the run instead of matching bad data."""
    control = dataset_copy / "run_control.csv"
    text = control.read_text().splitlines()
    header, bank = text[0], text[1].split(",")
    bank[2] = "1.00"                                  # declared debits no longer agree
    control.write_text("\n".join([header, ",".join(bank), *text[2:]]) + "\n")

    run_id = harness.imported_run(dataset_copy)
    summary = harness.validation.validate_and_normalize(run_id)
    assert not summary.passed
    assert harness.repositories.runs.get(run_id)["status"] == "validation_failed"
    with pytest.raises(ValueError):
        harness.matching.run_rules(run_id)


def test_tcc_009_the_same_file_cannot_be_imported_twice(harness):
    """FR-IMP-05: a repeated file would double the period's activity."""
    run_id = harness.imported_run()
    with pytest.raises(ImportError_):
        harness.imports.import_directory(run_id, DATASET, harness.users["Maya Castillo"])


def test_tcc_010_original_values_cannot_be_overwritten(harness):
    """FR-NRM-05: normalization adds values, it never replaces the source."""
    run_id = harness.imported_run()
    with pytest.raises(RepositoryError):
        with harness.database.transaction() as connection:
            harness.repositories.items.set_normalized(
                connection, "bank", 1, {"description_original": "tampered"})


def test_tcc_011_a_human_event_must_name_the_acting_user(harness):
    """FR-AUD-02: every human action is attributable."""
    run_id = harness.imported_run()
    context = RunContext.load(harness.database, run_id)
    with pytest.raises(AuditError):
        with harness.database.transaction() as connection:
            harness.audit.write(connection, context,
                                AuditEvent("DECISION", "P6", "recommendation", actor_type="human"))


def test_tcc_012_unknown_event_types_are_refused(harness):
    run_id = harness.imported_run()
    context = RunContext.load(harness.database, run_id)
    with pytest.raises(AuditError):
        with harness.database.transaction() as connection:
            harness.audit.write(connection, context, AuditEvent("MADE_UP", "P6", "recommendation"))


def test_tcc_013_every_audit_event_carries_the_required_fields(harness):
    """FR-AUD-02 and FR-EVL-07: audit completeness is measurable, so it is measured."""
    run_id = harness.matched_run()
    required = ("run_id", "sequence_no", "event_type", "process_code", "bank_account_id",
                "period_start", "period_end", "actor_type", "entity_type", "previous_hash",
                "event_hash", "created_at")
    events = harness.audit.events(run_id)
    assert events
    for event in events:
        for column in required:
            assert event[column] is not None, f"event {event['sequence_no']} missing {column}"


def test_tcc_014_the_chain_links_every_event_in_order(harness):
    run_id = harness.matched_run()
    events = harness.audit.events(run_id)
    previous = "GENESIS"
    for position, event in enumerate(events, start=1):
        assert event["sequence_no"] == position
        assert event["previous_hash"] == previous
        assert compute_hash(previous, dict(event)) == event["event_hash"]
        previous = event["event_hash"]
