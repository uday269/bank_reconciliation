-- =============================================================================
-- AI-Assisted Bank Reconciliation — physical schema (SQLite)
-- Source of truth for all tables, constraints, triggers and indexes.
-- Apply with:  sqlite3 reconciliation.db < db/schema.sql
--
-- Conventions
--   * Table names are singular lower_snake_case.
--   * Money is stored as INTEGER cents. Amounts are positive; direction carries the sign.
--   * Dates are ISO 8601 date strings (YYYY-MM-DD) using transaction business dates.
--   * Timestamps are ISO 8601 UTC strings (YYYY-MM-DDTHH:MM:SSZ).
--   * Records are voided or superseded, never deleted.
--   * Evidence tables (audit_event, review_decision, adjustment_decision,
--     reopen_request, reopen_decision, period_signoff, normalization_change)
--     are append-only, enforced by triggers.
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- -----------------------------------------------------------------------------
-- 1. Reference data
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_user (
    user_id        INTEGER PRIMARY KEY,
    full_name      TEXT    NOT NULL,
    role_code      TEXT    NOT NULL CHECK (role_code IN ('ROL-01', 'ROL-02', 'ROL-03')),
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_account (
    bank_account_id  INTEGER PRIMARY KEY,
    bank_name        TEXT NOT NULL,
    account_label    TEXT NOT NULL,
    account_mask     TEXT NOT NULL,
    gl_account_code  TEXT NOT NULL,
    currency_code    TEXT NOT NULL DEFAULT 'USD' CHECK (currency_code = 'USD')
);

CREATE TABLE IF NOT EXISTS gl_account (
    gl_account_code  TEXT PRIMARY KEY,
    account_name     TEXT NOT NULL,
    account_type     TEXT NOT NULL CHECK (account_type IN ('asset', 'liability', 'equity', 'income', 'expense'))
);

CREATE TABLE IF NOT EXISTS model_version (
    model_version_id      INTEGER PRIMARY KEY,
    version_label         TEXT    NOT NULL UNIQUE,
    algorithm             TEXT    NOT NULL,
    calibration_dataset   TEXT    NOT NULL,
    calibration_seed      INTEGER NOT NULL,
    trained_at            TEXT    NOT NULL,
    approved_by           INTEGER REFERENCES app_user (user_id),
    notes                 TEXT
);

-- -----------------------------------------------------------------------------
-- 2. Run and source data
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS reconciliation_run (
    run_id              INTEGER PRIMARY KEY,
    bank_account_id     INTEGER NOT NULL REFERENCES bank_account (bank_account_id),
    period_start        TEXT    NOT NULL,
    period_end          TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'created'
                        CHECK (status IN ('created', 'validating', 'validation_failed', 'processing',
                                          'in_review', 'ready_to_close', 'closed', 'reopen_requested')),
    parameter_snapshot  TEXT    NOT NULL,      -- JSON: PRM-01..PRM-10 values used by this run
    dataset_seed        INTEGER,
    model_version_id    INTEGER REFERENCES model_version (model_version_id),
    created_by          INTEGER NOT NULL REFERENCES app_user (user_id),
    created_at          TEXT    NOT NULL,
    CHECK (period_start <= period_end)
);

CREATE TABLE IF NOT EXISTS source_file (
    source_file_id               INTEGER PRIMARY KEY,
    run_id                       INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    file_type                    TEXT    NOT NULL CHECK (file_type IN ('bank', 'gl', 'carry_in')),
    file_name                    TEXT    NOT NULL,
    sha256                       TEXT    NOT NULL,
    row_count                    INTEGER NOT NULL CHECK (row_count >= 0),
    control_total_debits_cents   INTEGER NOT NULL DEFAULT 0,
    control_total_credits_cents  INTEGER NOT NULL DEFAULT 0,
    opening_balance_cents        INTEGER,
    closing_balance_cents        INTEGER,
    uploaded_by                  INTEGER NOT NULL REFERENCES app_user (user_id),
    uploaded_at                  TEXT    NOT NULL,
    UNIQUE (run_id, sha256)                    -- FR-IMP-05: same file cannot be imported twice
);

CREATE TABLE IF NOT EXISTS bank_transaction (
    bank_transaction_id     INTEGER PRIMARY KEY,
    run_id                  INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    source_file_id          INTEGER NOT NULL REFERENCES source_file (source_file_id),
    external_txn_id         TEXT    NOT NULL,
    transaction_date        TEXT    NOT NULL,
    amount_cents            INTEGER NOT NULL CHECK (amount_cents > 0),
    direction               TEXT    NOT NULL CHECK (direction IN ('debit', 'credit')),
    description_original    TEXT    NOT NULL,
    description_normalized  TEXT,
    reference_original      TEXT,
    reference_normalized    TEXT,
    payee_normalized        TEXT,
    bank_type_code          TEXT,
    running_balance_cents   INTEGER,
    status                  TEXT    NOT NULL DEFAULT 'imported'
                            CHECK (status IN ('imported', 'excluded', 'validated', 'proposed', 'escalated',
                                              'approved', 'unresolved', 'report_verified', 'reconciled')),
    is_possible_duplicate   INTEGER NOT NULL DEFAULT 0 CHECK (is_possible_duplicate IN (0, 1)),
    exclusion_reason        TEXT,
    UNIQUE (run_id, external_txn_id)
);

CREATE TABLE IF NOT EXISTS ledger_entry (
    ledger_entry_id         INTEGER PRIMARY KEY,
    run_id                  INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    source_file_id          INTEGER NOT NULL REFERENCES source_file (source_file_id),
    external_entry_id       TEXT    NOT NULL,
    posting_date            TEXT    NOT NULL,
    amount_cents            INTEGER NOT NULL CHECK (amount_cents > 0),
    direction               TEXT    NOT NULL CHECK (direction IN ('debit', 'credit')),
    description_original    TEXT    NOT NULL,
    description_normalized  TEXT,
    reference_original      TEXT,
    reference_normalized    TEXT,
    counterparty_normalized TEXT,
    gl_account_code         TEXT    NOT NULL REFERENCES gl_account (gl_account_code),
    source_journal          TEXT,
    status                  TEXT    NOT NULL DEFAULT 'imported'
                            CHECK (status IN ('imported', 'excluded', 'validated', 'proposed', 'escalated',
                                              'approved', 'unresolved', 'report_verified', 'reconciled')),
    is_possible_duplicate   INTEGER NOT NULL DEFAULT 0 CHECK (is_possible_duplicate IN (0, 1)),
    exclusion_reason        TEXT,
    UNIQUE (run_id, external_entry_id)
);

CREATE TABLE IF NOT EXISTS carry_in_item (
    carry_in_item_id            INTEGER PRIMARY KEY,
    run_id                      INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    source_file_id              INTEGER NOT NULL REFERENCES source_file (source_file_id),
    external_item_id            TEXT    NOT NULL,
    item_type                   TEXT    NOT NULL CHECK (item_type IN ('outstanding_check', 'deposit_in_transit')),
    original_date               TEXT    NOT NULL,
    amount_cents                INTEGER NOT NULL CHECK (amount_cents > 0),
    direction                   TEXT    NOT NULL CHECK (direction IN ('debit', 'credit')),
    description_original        TEXT    NOT NULL,
    description_normalized      TEXT,
    reference_original          TEXT,
    reference_normalized        TEXT,
    status                      TEXT    NOT NULL DEFAULT 'imported'
                                CHECK (status IN ('imported', 'excluded', 'validated', 'proposed', 'escalated',
                                                  'approved', 'unresolved', 'report_verified', 'reconciled')),
    cleared_by_bank_transaction_id INTEGER REFERENCES bank_transaction (bank_transaction_id),
    UNIQUE (run_id, external_item_id)
);

-- -----------------------------------------------------------------------------
-- 3. Validation and normalization
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS validation_result (
    validation_result_id  INTEGER PRIMARY KEY,
    run_id                INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    source_file_id        INTEGER REFERENCES source_file (source_file_id),
    test_code             TEXT    NOT NULL,
    test_name             TEXT    NOT NULL,
    scope                 TEXT    NOT NULL CHECK (scope IN ('file', 'row')),
    row_reference         TEXT,
    outcome               TEXT    NOT NULL CHECK (outcome IN ('pass', 'fail', 'warning')),
    message               TEXT,
    created_at            TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS normalization_change (
    normalization_change_id  INTEGER PRIMARY KEY,
    run_id                   INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    item_type                TEXT    NOT NULL CHECK (item_type IN ('bank', 'ledger', 'carry_in')),
    item_id                  INTEGER NOT NULL,
    field_name               TEXT    NOT NULL,
    original_value           TEXT,
    revised_value            TEXT,
    rule_name                TEXT    NOT NULL,
    created_at               TEXT    NOT NULL
);

-- -----------------------------------------------------------------------------
-- 4. Recommendations
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS recommendation (
    recommendation_id    INTEGER PRIMARY KEY,
    run_id               INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    subject_item_type    TEXT    NOT NULL CHECK (subject_item_type IN ('bank', 'ledger', 'carry_in')),
    subject_item_id      INTEGER NOT NULL,
    kind                 TEXT    NOT NULL CHECK (kind IN ('match', 'exception')),
    relationship         TEXT    NOT NULL DEFAULT 'none'
                         CHECK (relationship IN ('one_to_one', 'one_to_many', 'many_to_one', 'none')),
    source               TEXT    NOT NULL CHECK (source IN ('rule', 'ai')),
    rule_name            TEXT,
    model_version_id     INTEGER REFERENCES model_version (model_version_id),
    category_code        TEXT    NOT NULL CHECK (category_code IN ('CAT-01', 'CAT-02', 'CAT-03', 'CAT-04', 'CAT-05')),
    exception_code       TEXT    CHECK (exception_code IN ('EXC-01', 'EXC-02', 'EXC-03', 'EXC-04',
                                                           'EXC-05', 'EXC-06', 'EXC-07', 'EXC-08')),
    risk_level           TEXT    NOT NULL CHECK (risk_level IN ('low', 'medium', 'high')),
    risk_rules           TEXT,                      -- JSON array of triggered risk rule names
    confidence           REAL    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    explanation          TEXT    NOT NULL,
    genai_prose          TEXT,                      -- labelled AI prose, never a score (CR-13)
    status               TEXT    NOT NULL DEFAULT 'open'
                         CHECK (status IN ('open', 'decided', 'superseded')),
    created_at           TEXT    NOT NULL,
    CHECK (kind = 'match' OR exception_code IS NOT NULL),
    CHECK (source = 'ai' OR rule_name IS NOT NULL),
    CHECK (source = 'rule' OR model_version_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS candidate (
    candidate_id       INTEGER PRIMARY KEY,
    recommendation_id  INTEGER NOT NULL REFERENCES recommendation (recommendation_id),
    rank_order         INTEGER NOT NULL CHECK (rank_order >= 1),
    score              REAL    NOT NULL,
    confidence         REAL    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    feature_values     TEXT    NOT NULL,           -- JSON: date_distance, amount_similarity, text_similarity,
                                                   -- reference_similarity, relationship_type
    UNIQUE (recommendation_id, rank_order)
);

CREATE TABLE IF NOT EXISTS candidate_member (
    candidate_member_id  INTEGER PRIMARY KEY,
    candidate_id         INTEGER NOT NULL REFERENCES candidate (candidate_id),
    bank_transaction_id  INTEGER REFERENCES bank_transaction (bank_transaction_id),
    ledger_entry_id      INTEGER REFERENCES ledger_entry (ledger_entry_id),
    carry_in_item_id     INTEGER REFERENCES carry_in_item (carry_in_item_id),
    CHECK ((bank_transaction_id IS NOT NULL) + (ledger_entry_id IS NOT NULL) + (carry_in_item_id IS NOT NULL) = 1)
);

-- -----------------------------------------------------------------------------
-- 5. Review, decisions and adjustments
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS review_batch (
    review_batch_id  INTEGER PRIMARY KEY,
    run_id           INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    created_by       INTEGER NOT NULL REFERENCES app_user (user_id),
    created_at       TEXT    NOT NULL,
    item_count       INTEGER NOT NULL CHECK (item_count > 0)
);

CREATE TABLE IF NOT EXISTS review_decision (
    review_decision_id   INTEGER PRIMARY KEY,
    run_id               INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    recommendation_id    INTEGER NOT NULL REFERENCES recommendation (recommendation_id),
    chosen_candidate_id  INTEGER REFERENCES candidate (candidate_id),
    review_batch_id      INTEGER REFERENCES review_batch (review_batch_id),
    decision             TEXT    NOT NULL CHECK (decision IN ('approve', 'reject', 'modify', 'escalate', 'unresolved')),
    decision_level       TEXT    NOT NULL CHECK (decision_level IN ('first', 'senior')),
    comment              TEXT,
    decided_by           INTEGER NOT NULL REFERENCES app_user (user_id),
    opened_at            TEXT,                     -- detail view opened (FR-REV-11)
    decided_at           TEXT    NOT NULL,
    previous_status      TEXT    NOT NULL,
    new_status           TEXT    NOT NULL,
    CHECK (decision <> 'modify' OR chosen_candidate_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS adjustment (
    adjustment_id       INTEGER PRIMARY KEY,
    run_id              INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    recommendation_id   INTEGER NOT NULL REFERENCES recommendation (recommendation_id),
    amount_cents        INTEGER NOT NULL CHECK (amount_cents > 0),
    debit_account_code  TEXT    NOT NULL REFERENCES gl_account (gl_account_code),
    credit_account_code TEXT    NOT NULL REFERENCES gl_account (gl_account_code),
    rationale           TEXT    NOT NULL,
    evidence_refs       TEXT,
    prepared_by         INTEGER NOT NULL REFERENCES app_user (user_id),
    prepared_at         TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed', 'approved', 'rejected')),
    CHECK (debit_account_code <> credit_account_code)
);

CREATE TABLE IF NOT EXISTS adjustment_decision (
    adjustment_decision_id  INTEGER PRIMARY KEY,
    adjustment_id           INTEGER NOT NULL REFERENCES adjustment (adjustment_id),
    decision                TEXT    NOT NULL CHECK (decision IN ('approve', 'reject')),
    comment                 TEXT,
    decided_by              INTEGER NOT NULL REFERENCES app_user (user_id),
    decided_at              TEXT    NOT NULL
);

-- -----------------------------------------------------------------------------
-- 6. Period control
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS period_signoff (
    period_signoff_id            INTEGER PRIMARY KEY,
    run_id                       INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    signed_by                    INTEGER NOT NULL REFERENCES app_user (user_id),
    signed_at                    TEXT    NOT NULL,
    bank_ending_balance_cents    INTEGER NOT NULL,
    book_ending_balance_cents    INTEGER NOT NULL,
    deposits_in_transit_cents    INTEGER NOT NULL DEFAULT 0,
    outstanding_checks_cents     INTEGER NOT NULL DEFAULT 0,
    bank_originated_cents        INTEGER NOT NULL DEFAULT 0,
    unresolved_difference_cents  INTEGER NOT NULL DEFAULT 0,
    unresolved_item_count        INTEGER NOT NULL DEFAULT 0,
    comment                      TEXT,
    chain_head_hash              TEXT    NOT NULL,
    CHECK (unresolved_difference_cents = 0 OR comment IS NOT NULL)   -- CR-10
);

CREATE TABLE IF NOT EXISTS reopen_request (
    reopen_request_id  INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    requested_by       INTEGER NOT NULL REFERENCES app_user (user_id),
    reason             TEXT    NOT NULL,
    requested_at       TEXT    NOT NULL,
    original_status    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS reopen_decision (
    reopen_decision_id  INTEGER PRIMARY KEY,
    reopen_request_id   INTEGER NOT NULL REFERENCES reopen_request (reopen_request_id),
    decision            TEXT    NOT NULL CHECK (decision IN ('approve', 'reject')),
    comment             TEXT,
    decided_by          INTEGER NOT NULL REFERENCES app_user (user_id),
    decided_at          TEXT    NOT NULL,
    revised_status      TEXT    NOT NULL
);

-- -----------------------------------------------------------------------------
-- 7. Reports
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS report (
    report_id       INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    report_code     TEXT    NOT NULL CHECK (report_code IN ('RPT-01','RPT-02','RPT-03','RPT-04','RPT-05','RPT-06',
                                                            'RPT-07','RPT-08','RPT-09','RPT-10','RPT-11','RPT-12','RPT-13')),
    output_format   TEXT    NOT NULL CHECK (output_format IN ('html', 'csv')),
    file_path       TEXT    NOT NULL,
    content_sha256  TEXT    NOT NULL,
    generated_at    TEXT    NOT NULL
);

-- -----------------------------------------------------------------------------
-- 8. Audit log — append-only, hash-chained (FR-AUD-01..07, DD-06)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_event (
    audit_event_id    INTEGER PRIMARY KEY,
    run_id            INTEGER NOT NULL REFERENCES reconciliation_run (run_id),
    sequence_no       INTEGER NOT NULL,
    event_type        TEXT    NOT NULL,
    process_code      TEXT    NOT NULL CHECK (process_code IN ('P1','P2','P3','P4','P5','P6','P7','P8')),
    bank_account_id   INTEGER NOT NULL REFERENCES bank_account (bank_account_id),
    period_start      TEXT    NOT NULL,
    period_end        TEXT    NOT NULL,
    actor_type        TEXT    NOT NULL CHECK (actor_type IN ('human', 'system')),
    actor_user_id     INTEGER REFERENCES app_user (user_id),
    entity_type       TEXT    NOT NULL,
    entity_id         INTEGER,
    item_refs         TEXT,                        -- JSON array of affected transaction identifiers
    original_values   TEXT,                        -- JSON
    revised_values    TEXT,                        -- JSON
    rule_name         TEXT,
    model_version_id  INTEGER REFERENCES model_version (model_version_id),
    candidate_scores  TEXT,                        -- JSON array
    confidence        REAL,
    risk_level        TEXT CHECK (risk_level IS NULL OR risk_level IN ('low', 'medium', 'high')),
    explanation       TEXT,
    decision          TEXT,
    comment           TEXT,
    approval_status   TEXT,
    evidence_refs     TEXT,
    previous_status   TEXT,
    new_status        TEXT,
    previous_hash     TEXT    NOT NULL,
    event_hash        TEXT    NOT NULL UNIQUE,
    created_at        TEXT    NOT NULL,
    UNIQUE (run_id, sequence_no),
    CHECK (actor_type = 'system' OR actor_user_id IS NOT NULL)
);

-- -----------------------------------------------------------------------------
-- 9. Append-only triggers
-- Trigger-based protection detects and blocks edits made through this database
-- connection. It is not write-once storage: anyone holding the database file can
-- rebuild it. The hash chain plus the chain-head hash printed on the signed
-- Reconciliation Summary is what makes such tampering detectable (DD-06).
-- -----------------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS audit_event_block_update
BEFORE UPDATE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'audit_event is append-only: corrections must be new events (CR-09)');
END;

CREATE TRIGGER IF NOT EXISTS audit_event_block_delete
BEFORE DELETE ON audit_event
BEGIN
    SELECT RAISE(ABORT, 'audit_event is append-only: rows cannot be deleted (CR-09)');
END;

CREATE TRIGGER IF NOT EXISTS review_decision_block_update
BEFORE UPDATE ON review_decision
BEGIN
    SELECT RAISE(ABORT, 'review_decision is append-only: record a new decision instead (CR-09)');
END;

CREATE TRIGGER IF NOT EXISTS review_decision_block_delete
BEFORE DELETE ON review_decision
BEGIN
    SELECT RAISE(ABORT, 'review_decision is append-only: rows cannot be deleted (CR-09)');
END;

CREATE TRIGGER IF NOT EXISTS adjustment_decision_block_update
BEFORE UPDATE ON adjustment_decision
BEGIN
    SELECT RAISE(ABORT, 'adjustment_decision is append-only (CR-09)');
END;

CREATE TRIGGER IF NOT EXISTS adjustment_decision_block_delete
BEFORE DELETE ON adjustment_decision
BEGIN
    SELECT RAISE(ABORT, 'adjustment_decision is append-only (CR-09)');
END;

CREATE TRIGGER IF NOT EXISTS period_signoff_block_update
BEFORE UPDATE ON period_signoff
BEGIN
    SELECT RAISE(ABORT, 'period_signoff is append-only (CR-09)');
END;

CREATE TRIGGER IF NOT EXISTS period_signoff_block_delete
BEFORE DELETE ON period_signoff
BEGIN
    SELECT RAISE(ABORT, 'period_signoff is append-only (CR-09)');
END;

CREATE TRIGGER IF NOT EXISTS reopen_request_block_update
BEFORE UPDATE ON reopen_request
BEGIN
    SELECT RAISE(ABORT, 'reopen_request is append-only: record a reopen_decision instead (CR-09)');
END;

CREATE TRIGGER IF NOT EXISTS reopen_decision_block_update
BEFORE UPDATE ON reopen_decision
BEGIN
    SELECT RAISE(ABORT, 'reopen_decision is append-only (CR-09)');
END;

CREATE TRIGGER IF NOT EXISTS normalization_change_block_update
BEFORE UPDATE ON normalization_change
BEGIN
    SELECT RAISE(ABORT, 'normalization_change is append-only: originals must be preserved (FR-NRM-05)');
END;

CREATE TRIGGER IF NOT EXISTS normalization_change_block_delete
BEFORE DELETE ON normalization_change
BEGIN
    SELECT RAISE(ABORT, 'normalization_change is append-only (FR-NRM-05)');
END;

-- Locked periods accept no further changes (FR-PER-03, CR-18).
CREATE TRIGGER IF NOT EXISTS review_decision_block_when_locked
BEFORE INSERT ON review_decision
BEGIN
    SELECT CASE
        WHEN (SELECT status FROM reconciliation_run WHERE run_id = NEW.run_id) = 'closed'
        THEN RAISE(ABORT, 'period is closed: reopen it before recording decisions (CR-18)')
    END;
END;

CREATE TRIGGER IF NOT EXISTS adjustment_block_when_locked
BEFORE INSERT ON adjustment
BEGIN
    SELECT CASE
        WHEN (SELECT status FROM reconciliation_run WHERE run_id = NEW.run_id) = 'closed'
        THEN RAISE(ABORT, 'period is closed: adjustments cannot be proposed (FR-PER-03)')
    END;
END;

CREATE TRIGGER IF NOT EXISTS source_file_block_when_locked
BEFORE INSERT ON source_file
BEGIN
    SELECT CASE
        WHEN (SELECT status FROM reconciliation_run WHERE run_id = NEW.run_id) = 'closed'
        THEN RAISE(ABORT, 'period is closed: files cannot be imported (FR-PER-03)')
    END;
END;

-- -----------------------------------------------------------------------------
-- 10. Indexes
-- Candidate generation filters by run, direction, date and amount (FR-AI-01).
-- -----------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS ix_bank_transaction_lookup
    ON bank_transaction (run_id, direction, transaction_date, amount_cents);
CREATE INDEX IF NOT EXISTS ix_bank_transaction_amount
    ON bank_transaction (run_id, amount_cents);
CREATE INDEX IF NOT EXISTS ix_bank_transaction_status
    ON bank_transaction (run_id, status);
CREATE INDEX IF NOT EXISTS ix_ledger_entry_lookup
    ON ledger_entry (run_id, direction, posting_date, amount_cents);
CREATE INDEX IF NOT EXISTS ix_ledger_entry_amount
    ON ledger_entry (run_id, amount_cents);
CREATE INDEX IF NOT EXISTS ix_ledger_entry_status
    ON ledger_entry (run_id, status);
CREATE INDEX IF NOT EXISTS ix_carry_in_item_lookup
    ON carry_in_item (run_id, direction, amount_cents);
CREATE INDEX IF NOT EXISTS ix_recommendation_run
    ON recommendation (run_id, category_code, risk_level);
CREATE INDEX IF NOT EXISTS ix_recommendation_subject
    ON recommendation (run_id, subject_item_type, subject_item_id);
CREATE INDEX IF NOT EXISTS ix_candidate_recommendation
    ON candidate (recommendation_id, rank_order);
CREATE INDEX IF NOT EXISTS ix_review_decision_recommendation
    ON review_decision (recommendation_id, decided_at);
CREATE INDEX IF NOT EXISTS ix_audit_event_run_seq
    ON audit_event (run_id, sequence_no);
CREATE INDEX IF NOT EXISTS ix_audit_event_entity
    ON audit_event (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_validation_result_file
    ON validation_result (run_id, source_file_id, outcome);
