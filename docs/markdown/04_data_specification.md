# Data Specification — AI-Assisted Bank Reconciliation

## 1. Purpose and Conventions

This document defines the data the system reads, stores and produces: the three CSV input files, the ground-truth label file used only for evaluation, the synthetic dataset design, and the data dictionary for all 23 database tables.

The physical schema in `db/schema.sql` is the source of truth. This document describes it; where the two ever differ, the schema wins.

| Convention | Rule |
|--------------|------------------------------------------------------------------|
| Money | Integer cents, always positive; `direction` carries the sign |
| Dates | ISO 8601 date (YYYY-MM-DD), business dates from the source records |
| Timestamps | ISO 8601 UTC (YYYY-MM-DDTHH:MM:SSZ) |
| Table names | Singular lower_snake_case |
| Deletion | Records are voided or superseded, never deleted |
| Text encoding | UTF-8, comma-delimited, double-quote escaped, one header row |

## 2. Business Data

All data is fictional and generated from a fixed seed. No real banking or customer data is used (ASM-03).

| Item | Value |
|------------------------|--------------------------------------------------------|
| Organization | Bonneville Provisions Co., specialty-food wholesaler, Salt Lake City |
| Bank | Lone Peak Commercial Bank |
| Account | Operating Checking ending 7310, USD |
| Ledger account | 1010 Cash — Operating |
| Period | 1–31 August 2026, 21 business days |
| Prior period | July 2026, supplying carry-in items |

### 2.1 Chart of accounts used by adjustments

| Code | Name | Type | Used for |
|--------|-----------------------|-------------|-------------------------------------|
| 1010 | Cash — Operating | asset | Every adjustment's other side |
| 1210 | Accounts Receivable | asset | Unrecorded customer receipts |
| 2010 | Accounts Payable | liability | Unrecorded vendor payments |
| 6810 | Bank Service Charges | expense | EXC-03 bank fees |
| 6820 | Returned Item Charges | expense | Returned deposits and related fees |
| 7010 | Interest Income | income | EXC-04 bank interest |
| 9990 | Suspense — Under Investigation | asset | EXC-07 unexplained items pending explanation |

### 2.2 Counterparties

The generator draws from a fixed list of 24 customers (restaurants and grocers) and 18 vendors (producers, freight, utilities, payroll), each with a full ledger name and one to three abbreviated bank forms, for example `Canyon Ridge Grocery` appearing as `CANYON RIDGE GRCRY`, `CYN RIDGE GROC` or `CANYONRIDGE GROC LLC`. These pairs are what SCN-03 name variation tests.

### 2.3 Staff identities

| ID | Name | Role |
|----------|-------------------------|---------------------------------------------|
| 1 | Maya Castillo | ROL-01 Staff Accountant |
| 2 | Ethan Brooks | ROL-01 Staff Accountant |
| 3 | Priya Raman | ROL-02 Senior Accountant |
| 4 | Daniel Okafor | ROL-03 Controller |

Two ROL-01 identities exist so separation of duties can be demonstrated (CR-03, CR-05).

## 3. Input Files

Five files are produced by the generator. Three are imported, one carries control totals, and one is the ground truth, which the application never reads.

| File | Purpose | Imported |
|-----------------------|------------------------------|-----------------------------|
| `bank_statement.csv` | Bank statement lines | Yes, as `file_type = bank` |
| `gl_cash_detail.csv` | Ledger entries for account 1010 | Yes, as `file_type = gl` |
| `carry_in_items.csv` | Outstanding items from July | Yes, as `file_type = carry_in` |
| `run_control.csv` | Declared row counts, control totals and balances for each file | Yes, used by validation |
| `ground_truth.csv` | Correct answer for every item | No, evaluation only (FR-EVL-11) |

### 3.1 bank_statement.csv

| Column | Type | Required | Notes |
|------------------|----------|-----------|---------------------------------------------|
| txn_id | text | yes | Unique within the file, format BT-000123 |
| txn_date | date | yes | Business date the bank posted the item |
| amount | decimal | yes | Positive, two decimal places |
| direction | text | yes | `debit` or `credit` |
| description | text | yes | Bank text, abbreviated and inconsistent by design |
| reference | text | no | Check number, ACH trace or wire reference |
| bank_type_code | text | yes | `CHK`, `ACH-DR`, `ACH-CR`, `WIRE`, `DEP`, `FEE`, `INT`, `RTN` |
| running_balance | decimal | yes | Balance after the item; used to check continuity |

### 3.2 gl_cash_detail.csv

| Column | Type | Required | Notes |
|-----------------|----------|-----------|---------------------------------------------|
| entry_id | text | yes | Unique within the file, format GL-000123 |
| posting_date | date | yes | Date the entry was posted in the ledger |
| amount | decimal | yes | Positive, two decimal places |
| direction | text | yes | `debit` increases cash, `credit` decreases cash |
| description | text | yes | Ledger text, generally the full counterparty name |
| reference | text | no | Invoice, check or deposit reference |
| gl_account | text | yes | Always 1010 in this scope |
| source_journal | text | yes | `CR` receipts, `CD` disbursements, `PR` payroll, `GJ` general |

The two `direction` columns describe the same movement from opposite viewpoints: a customer receipt is a `credit` on the bank file and a `debit` in the ledger. Normalization records both, and matching compares the movement, not the label.

### 3.3 carry_in_items.csv

| Column | Type | Required | Notes |
|------------------|-----------|-------------|--------------------------------------|
| item_id | text | yes | Format CI-0012 |
| item_type | text | yes | `outstanding_check` or `deposit_in_transit` |
| original_date | date | yes | Date in the prior period |
| amount | decimal | yes | Positive |
| direction | text | yes | As recorded in the ledger |
| description | text | yes | Counterparty and purpose |
| reference | text | no | Check number or deposit reference |

### 3.4 run_control.csv

| Column | Type | Notes |
|---------------------|-----------|------------------------------------------------|
| file_type | text | `bank`, `gl` or `carry_in` |
| row_count | integer | Declared row count, compared with rows read (FR-VAL-03) |
| total_debits | decimal | Declared debit total |
| total_credits | decimal | Declared credit total |
| opening_balance | decimal | Period opening balance; blank for carry-in |
| closing_balance | decimal | Period closing balance; blank for carry-in |

Validation checks that opening balance plus credits minus debits equals closing balance for both the bank and ledger files. This is what makes the bank-to-book statement provable rather than asserted.

### 3.5 ground_truth.csv

Read only by the evaluation harness, never by the application (FR-EVL-11, DD-11).

| Column | Type | Notes |
|-----------------------|-------|-------------------------------------------------------|
| item_type | text | `bank`, `ledger` or `carry_in` |
| item_id | text | The item this label describes |
| true_match_group | text | Shared key for all items that belong together, blank if the item matches nothing |
| scenario_code | text | SCN-01..SCN-08 |
| exception_code | text | EXC-01..EXC-08 where the item is an exception |
| expected_risk | text | `low`, `medium` or `high` under BR-15 |
| expected_disposition | text | `match`, `carry_forward`, `adjust`, `investigate` or `unresolved` |
| note | text | Why the item was generated this way |

A group key rather than a single match ID is used because one-to-many and many-to-one relationships have no single counterpart. All members of a group share one key.

## 4. Dataset Design

### 4.1 Volumes

1,240 transactions in total, plus 18 carry-in items from July.

| Scenario | Bank items | Ledger items | Groups or pairs |
|-----------------------------------|----------|-----------|-------------------------------------|
| SCN-01 Exact match | 378 | 378 | 378 pairs |
| SCN-02 Timing difference | 95 | 95 | 95 pairs |
| SCN-03 Name variation | 55 | 55 | 55 pairs |
| SCN-04 One to many | 12 | 42 | 12 groups: six of 4, six of 3 |
| SCN-05 Many to one | 30 | 10 | 10 groups of 3 |
| SCN-06 Bank-originated | 16 | 0 | 10 fees, 3 interest, 3 returned items |
| SCN-07 Duplicate | 8 | 10 | 6 groups; one genuine copy matches, the extra copy does not |
| SCN-08 Unexplained | 3 | 5 | 8 items with no counterpart |
| Carry-in clearings | 12 | 0 | Clears 12 of the 18 July items (BR-11) |
| Outstanding checks at period end | 0 | 22 | Become EXC-01 |
| Deposits in transit at period end | 0 | 11 | Become EXC-02 |
| Validation test rows | 3 | 0 | 1 invalid amount, 2 dated outside the period |
| **Total** | **612** | **628** | |

Group sizes respect the caps in DD-07 (at most 4 members), and two groups are generated deliberately at the boundary so the cap behaviour is exercised.

### 4.2 Risk and confidence mix

| Property | Count | Purpose |
|--------------------------------------------|-----------------------|--------------------------------------------------|
| Items at or above $10,000 (PRM-01) | 23 (12 bank, 11 ledger) | Force senior approval (CR-02) |
| Suspected duplicate groups (BR-07) | 6 groups, 18 items | Exercise CAT-05 routing on duplication |
| Unexplained items (EXC-07) | 10 | Exercise escalation and the unresolved end state |
| New counterparties not seen before | 9 pairs | Exercise the medium-risk novelty rule |
| Stale carry-in items over PRM-03 | 2 remaining at period end | Exercise BR-10 |
| Near-miss amounts within $1.00, same day | 11 pairs | Produce genuinely competing candidates |
| Repeated identical amount and date | 11 pairs | Test that BR-02 uniqueness prevents a false exact match |

### 4.3 Reproducibility

| Dataset | Seed | Period | Use |
|----------------|-----------|----------------------|---------------------------------|
| Calibration | 20260701 | July 2026, about 1,000 items | Fit the confidence calibrator only (DD-11) |
| Evaluation and demonstration | 20260801 | August 2026, 1,240 items | Every demo, test and reported metric |

The two datasets never overlap. A fixed seed plus the parameter snapshot on the run means the same input always produces the same recommendations, metrics and reports (NFR-01, SC-06).

### 4.4 Balance tie-out

The generator builds the ledger from the bank file and the known differences, so the reconciliation statement provably ties:

```
adjusted bank balance = bank closing balance
                      + deposits in transit
                      - outstanding checks
adjusted book balance = ledger closing balance
                      + unrecorded interest
                      - unrecorded fees and returned items
```

With every generated difference accounted for, the two sides agree exactly, apart from items that are unmatched by design: the unexplained items (SCN-08) and the extra copy of each duplicate (SCN-07). Those leave an unresolved difference of $9,392.35 in the seeded dataset, so the close screen and the CR-10 comment rule are exercised on a real figure. The generator asserts this identity on every run and fails if it does not hold.

## 5. Normalization Rules

Every change is recorded with its original value and rule name in `normalization_change` (FR-NRM-05).

| Rule | Applies to | Action |
|-------------|---------------|-------------------------------------------------------------|
| NRM-DATE | Dates | Parse to ISO 8601; reject unparseable values |
| NRM-AMOUNT | Amounts | Convert to integer cents; reject non-numeric values |
| NRM-CASE | Descriptions | Uppercase and collapse repeated whitespace |
| NRM-PUNCT | Descriptions | Remove punctuation other than digits and spaces |
| NRM-ABBREV | Descriptions | Expand known abbreviations: GRCRY → GROCERY, MKT → MARKET, CO → COMPANY, INTL → INTERNATIONAL, and 40 more |
| NRM-NOISE | Descriptions | Strip bank prefixes such as `ACH DEP`, `POS DEB`, `WIRE OUT` into a separate channel field |
| NRM-PAYEE | Descriptions | Extract the counterparty name after prefix removal |
| NRM-REF | References | Strip `CHK#`, `INV`, `REF` prefixes and leading zeros |

Normalized values are used for matching. Original values are always kept and are what the reviewer sees alongside them.

## 6. Data Dictionary

Twenty-three tables, generated from `db/schema.sql`. Types are SQLite storage classes. Constraints and triggers are stated in the schema file and summarized in DOC-05.

### app_user

Reviewer identities and their roles. Selected from a list at review time; there is no authentication (DD-05).

| Column | Type | Key | Null | Description |
|-------------|----------|------|-------|-----------------------------------------------|
| user_id | INTEGER | PK | no |  |
| full_name | TEXT |  | no |  |
| role_code | TEXT |  | no | ROL-01 Staff Accountant, ROL-02 Senior Accountant, ROL-03 Controller |
| is_active | INTEGER |  | no | 0 deactivates the identity; rows are never deleted |
| created_at | TEXT |  | no | UTC timestamp when the row was written |

### bank_account

The bank account in scope and the ledger account it reconciles to.

| Column | Type | Key | Null | Description |
|------------------|----------|------|-------|----------------------------------------------|
| bank_account_id | INTEGER | PK | no | Account in scope |
| bank_name | TEXT |  | no |  |
| account_label | TEXT |  | no |  |
| account_mask | TEXT |  | no | Masked account number shown in the interface, for example 7310 |
| gl_account_code | TEXT |  | no | Ledger cash account reconciled against this bank account |
| currency_code | TEXT |  | no |  |

### gl_account

Chart of accounts subset used by adjustments, so every proposed entry cites a real account.

| Column | Type | Key | Null | Description |
|----------------------------|-----------|---------|-----------|----------------------|
| gl_account_code | TEXT | PK | no |  |
| account_name | TEXT |  | no |  |
| account_type | TEXT |  | no | asset, liability, equity, income or expense |

### model_version

Scoring and calibration model in use, recorded on every AI recommendation (FR-AI-07).

| Column | Type | Key | Null | Description |
|----------------------|----------|-----------|-------|---------------------------------------|
| model_version_id | INTEGER | PK | no | Model version in use |
| version_label | TEXT |  | no |  |
| algorithm | TEXT |  | no |  |
| calibration_dataset | TEXT |  | no |  |
| calibration_seed | INTEGER |  | no | Seed of the calibration dataset, kept separate from the evaluation dataset (DD-11) |
| trained_at | TEXT |  | no |  |
| approved_by | INTEGER | FK → app_user | yes | Who approved this model version for use (CR-14) |
| notes | TEXT |  | yes |  |

### reconciliation_run

One account and one accounting period. Holds run status and the parameter values used, so a rerun reproduces the same results.

| Column | Type | Key | Null | Description |
|---------------------|----------|----------------|-------|------------------------------------------|
| run_id | INTEGER | PK | no | Owning reconciliation run |
| bank_account_id | INTEGER | FK → bank_account | no | Account in scope |
| period_start | TEXT |  | no |  |
| period_end | TEXT |  | no |  |
| status | TEXT |  | no | created, validating, validation_failed, processing, in_review, ready_to_close, closed, reopen_requested |
| parameter_snapshot | TEXT |  | no | JSON copy of PRM-01..PRM-10 as used by this run |
| dataset_seed | INTEGER |  | yes | Seed used to generate the synthetic data, for reproducibility |
| model_version_id | INTEGER | FK → model_version | yes | Model version in use |
| created_by | INTEGER | FK → app_user | no |  |
| created_at | TEXT |  | no | UTC timestamp when the row was written |

### source_file

Metadata for each imported CSV: hash, row count and control totals (FR-IMP-04).

| Column | Type | Key | Null | Description |
|------------------------------|----------|---------------------|-------|-------------------------|
| source_file_id | INTEGER | PK | no | File the row was imported from |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| file_type | TEXT |  | no | bank, gl or carry_in |
| file_name | TEXT |  | no | Name of the imported file |
| sha256 | TEXT |  | no | Hash of the file as imported; unique per run so the same file cannot be imported twice |
| row_count | INTEGER |  | no | Rows in the file as imported |
| control_total_debits_cents | INTEGER |  | no |  |
| control_total_credits_cents | INTEGER |  | no |  |
| opening_balance_cents | INTEGER |  | yes | From the statement or trial balance; null for the carry-in file |
| closing_balance_cents | INTEGER |  | yes |  |
| uploaded_by | INTEGER | FK → app_user | no | Identity that imported the file |
| uploaded_at | TEXT |  | no | UTC timestamp of the import |

### bank_transaction

One line of the bank statement, with original and normalized values and its current status.

| Column | Type | Key | Null | Description |
|-------------------------|----------|---------------------|-------|---------------------------------------|
| bank_transaction_id | INTEGER | PK | no |  |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| source_file_id | INTEGER | FK → source_file | no | File the row was imported from |
| external_txn_id | TEXT |  | no | Identifier supplied by the bank, unique within the run |
| transaction_date | TEXT |  | no |  |
| amount_cents | INTEGER |  | no | Always positive; direction carries the sign |
| direction | TEXT |  | no | debit reduces cash, credit increases cash, as seen by the account holder |
| description_original | TEXT |  | no | Exactly as supplied, never overwritten |
| description_normalized | TEXT |  | yes | Cleaned text used for matching |
| reference_original | TEXT |  | yes | Reference exactly as supplied |
| reference_normalized | TEXT |  | yes | Reference after normalization |
| payee_normalized | TEXT |  | yes |  |
| bank_type_code | TEXT |  | yes | Bank transaction code, used to identify fees and interest (BR-06) |
| running_balance_cents | INTEGER |  | yes |  |
| status | TEXT |  | no | Item lifecycle status: imported through reconciled |
| is_possible_duplicate | INTEGER |  | no | Flagged by BR-07 for investigation; the row is not excluded |
| exclusion_reason | TEXT |  | yes | Why a row was excluded during validation |

### ledger_entry

One general ledger cash entry, with original and normalized values and its current status.

| Column | Type | Key | Null | Description |
|--------------------------|----------|---------------------|-------|-----------------------------------|
| ledger_entry_id | INTEGER | PK | no |  |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| source_file_id | INTEGER | FK → source_file | no | File the row was imported from |
| external_entry_id | TEXT |  | no | Identifier supplied by the ledger export, unique within the run |
| posting_date | TEXT |  | no |  |
| amount_cents | INTEGER |  | no | Amount in integer cents, always positive |
| direction | TEXT |  | no |  |
| description_original | TEXT |  | no | Description exactly as supplied, never overwritten |
| description_normalized | TEXT |  | yes | Cleaned text used for matching |
| reference_original | TEXT |  | yes | Reference exactly as supplied |
| reference_normalized | TEXT |  | yes | Reference after normalization |
| counterparty_normalized | TEXT |  | yes |  |
| gl_account_code | TEXT | FK → gl_account | no |  |
| source_journal | TEXT |  | yes | Originating journal, for example cash receipts or accounts payable |
| status | TEXT |  | no |  |
| is_possible_duplicate | INTEGER |  | no |  |
| exclusion_reason | TEXT |  | yes |  |

### carry_in_item

An outstanding item brought forward from the prior period (DD-12).

| Column | Type | Key | Null | Description |
|---------------------------------|----------|---------------------|-------|-----------------------------|
| carry_in_item_id | INTEGER | PK | no |  |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| source_file_id | INTEGER | FK → source_file | no | File the row was imported from |
| external_item_id | TEXT |  | no |  |
| item_type | TEXT |  | no | outstanding_check or deposit_in_transit |
| original_date | TEXT |  | no |  |
| amount_cents | INTEGER |  | no | Amount in integer cents, always positive |
| direction | TEXT |  | no |  |
| description_original | TEXT |  | no | Description exactly as supplied, never overwritten |
| description_normalized | TEXT |  | yes | Cleaned text used for matching |
| reference_original | TEXT |  | yes | Reference exactly as supplied |
| reference_normalized | TEXT |  | yes | Reference after normalization |
| status | TEXT |  | no |  |
| cleared_by_bank_transaction_id | INTEGER | FK → bank_transaction | yes | Set when a current-period bank item clears this carry-in item (BR-11) |

### validation_result

Outcome of one validation test at file or row level (FR-VAL-06).

| Column | Type | Key | Null | Description |
|-----------------------|----------|---------------------|-------|-----------------------------------|
| validation_result_id | INTEGER | PK | no |  |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| source_file_id | INTEGER | FK → source_file | yes | File the row was imported from |
| test_code | TEXT |  | no |  |
| test_name | TEXT |  | no |  |
| scope | TEXT |  | no | file for whole-file tests, row for per-row tests |
| row_reference | TEXT |  | yes |  |
| outcome | TEXT |  | no | pass, fail or warning. File-level failures block matching (CR-17) |
| message | TEXT |  | yes |  |
| created_at | TEXT |  | no | UTC timestamp when the row was written |

### normalization_change

Original value, revised value and the rule applied, for every field the system changed (FR-NRM-05).

| Column | Type | Key | Null | Description |
|--------------------------|----------|---------------------|-------|----------------------|
| normalization_change_id | INTEGER | PK | no |  |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| item_type | TEXT |  | no | bank, ledger or carry_in |
| item_id | INTEGER |  | no |  |
| field_name | TEXT |  | no |  |
| original_value | TEXT |  | yes |  |
| revised_value | TEXT |  | yes |  |
| rule_name | TEXT |  | no |  |
| created_at | TEXT |  | no | UTC timestamp when the row was written |

### recommendation

A proposed match or exception disposition for one item, with category, risk, confidence and explanation.

| Column | Type | Key | Null | Description |
|--------------------|----------|---------------------|-------|-----------------------------------------|
| recommendation_id | INTEGER | PK | no | Recommendation this row belongs to |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| subject_item_type | TEXT |  | no | Which side the recommendation is about |
| subject_item_id | INTEGER |  | no |  |
| kind | TEXT |  | no | match or exception |
| relationship | TEXT |  | no | one_to_one, one_to_many, many_to_one, or none for exceptions |
| source | TEXT |  | no | rule for deterministic results, ai for scored results |
| rule_name | TEXT |  | yes |  |
| model_version_id | INTEGER | FK → model_version | yes | Model version in use |
| category_code | TEXT |  | no | CAT-01 Exact through CAT-05 High risk, assigned by BR-13 |
| exception_code | TEXT |  | yes | EXC-01..08; required when kind is exception |
| risk_level | TEXT |  | no | low, medium or high, assigned by rule (BR-15) |
| risk_rules | TEXT |  | yes | JSON array naming each risk rule that triggered |
| confidence | REAL |  | yes | Calibrated probability between 0 and 1; null for rule-based results |
| explanation | TEXT |  | no | Plain-language supporting and conflicting evidence |
| genai_prose | TEXT |  | yes | Optional AI-written prose, labelled and never a score (CR-13) |
| status | TEXT |  | no | open, decided or superseded |
| created_at | TEXT |  | no | UTC timestamp when the row was written |

### candidate

One ranked option within a recommendation, with its score and feature values.

| Column | Type | Key | Null | Description |
|--------------------|----------|-----------------|-------|--------------------------------------------|
| candidate_id | INTEGER | PK | no | Candidate this row belongs to |
| recommendation_id | INTEGER | FK → recommendation | no | Recommendation this row belongs to |
| rank_order | INTEGER |  | no | 1 is the best-scoring option; every candidate is retained and shown |
| score | REAL |  | no |  |
| confidence | REAL |  | yes |  |
| feature_values | TEXT |  | no | JSON: date distance, amount similarity, text similarity, reference similarity, relationship type |

### candidate_member

A transaction that forms part of a candidate. Groups have several members (one-to-many, many-to-one).

| Column | Type | Key | Null | Description |
|------------------------|-----------|---------------------|--------|----------------|
| candidate_member_id | INTEGER | PK | no |  |
| candidate_id | INTEGER | FK → candidate | no | Candidate this row belongs to |
| bank_transaction_id | INTEGER | FK → bank_transaction | yes |  |
| ledger_entry_id | INTEGER | FK → ledger_entry | yes |  |
| carry_in_item_id | INTEGER | FK → carry_in_item | yes |  |

### review_batch

A group of exact matches presented for one approval action (CR-11, DD-02).

| Column | Type | Key | Null | Description |
|------------------|----------|---------------------|-------|-------------------------|
| review_batch_id | INTEGER | PK | no |  |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| created_by | INTEGER | FK → app_user | no |  |
| created_at | TEXT |  | no | UTC timestamp when the row was written |
| item_count | INTEGER |  | no |  |

### review_decision

One human decision on one recommendation. Append-only: corrections add rows.

| Column | Type | Key | Null | Description |
|----------------------|----------|---------------------|-------|-----------------------------------------|
| review_decision_id | INTEGER | PK | no |  |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| recommendation_id | INTEGER | FK → recommendation | no | Recommendation this row belongs to |
| chosen_candidate_id | INTEGER | FK → candidate | yes | Required when the decision is modify |
| review_batch_id | INTEGER | FK → review_batch | yes | Set when the decision came from a batch action; the record is still per item |
| decision | TEXT |  | no | approve, reject, modify, escalate or unresolved |
| decision_level | TEXT |  | no | first for first-level review, senior for escalated decisions |
| comment | TEXT |  | yes | Required to reject, modify, escalate, leave unresolved or approve a high-risk item (CR-16) |
| decided_by | INTEGER | FK → app_user | no | Identity that made the decision |
| opened_at | TEXT |  | yes | When the detail view was opened, used for time per item (DD-04) |
| decided_at | TEXT |  | no | UTC timestamp of the decision |
| previous_status | TEXT |  | no | Status before the action |
| new_status | TEXT |  | no | Status after the action |

### adjustment

A proposed correcting entry. Recorded only, never posted (CR-12).

| Column | Type | Key | Null | Description |
|----------------------|----------|---------------------|-------|---------------------------------|
| adjustment_id | INTEGER | PK | no | Adjustment this row belongs to |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| recommendation_id | INTEGER | FK → recommendation | no | Recommendation this row belongs to |
| amount_cents | INTEGER |  | no | Amount in integer cents, always positive |
| debit_account_code | TEXT | FK → gl_account | no | Account to debit; must differ from the credit account |
| credit_account_code | TEXT | FK → gl_account | no |  |
| rationale | TEXT |  | no | Why the entry is proposed |
| evidence_refs | TEXT |  | yes | References to supporting documents or records |
| prepared_by | INTEGER | FK → app_user | no | Identity that prepared the record |
| prepared_at | TEXT |  | no | UTC timestamp when the record was prepared |
| status | TEXT |  | no | proposed, approved or rejected |

### adjustment_decision

Approval or rejection of an adjustment by someone other than the preparer (CR-04).

| Column | Type | Key | Null | Description |
|-------------------------|----------|-------------|-------|----------------------------|
| adjustment_decision_id | INTEGER | PK | no |  |
| adjustment_id | INTEGER | FK → adjustment | no | Adjustment this row belongs to |
| decision | TEXT |  | no |  |
| comment | TEXT |  | yes | Free-text reviewer comment |
| decided_by | INTEGER | FK → app_user | no | Identity that made the decision |
| decided_at | TEXT |  | no | UTC timestamp of the decision |

### period_signoff

Controller sign-off with final balances, unresolved difference and the audit chain head hash.

| Column | Type | Key | Null | Description |
|------------------------------|----------|---------------------|-------|----------------------|
| period_signoff_id | INTEGER | PK | no |  |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| signed_by | INTEGER | FK → app_user | no |  |
| signed_at | TEXT |  | no |  |
| bank_ending_balance_cents | INTEGER |  | no |  |
| book_ending_balance_cents | INTEGER |  | no |  |
| deposits_in_transit_cents | INTEGER |  | no |  |
| outstanding_checks_cents | INTEGER |  | no |  |
| bank_originated_cents | INTEGER |  | no |  |
| unresolved_difference_cents | INTEGER |  | no | Adjusted bank balance minus adjusted book balance; a non-zero value requires a comment |
| unresolved_item_count | INTEGER |  | no |  |
| comment | TEXT |  | yes | Free-text reviewer comment |
| chain_head_hash | TEXT |  | no | Hash of the last audit event at sign-off, the external anchor for tamper detection |

### reopen_request

A request to reopen a closed period, with reason and requester.

| Column | Type | Key | Null | Description |
|--------------------|----------|---------------------|-------|---------------------------|
| reopen_request_id | INTEGER | PK | no | Reopen request this decision answers |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| requested_by | INTEGER | FK → app_user | no |  |
| reason | TEXT |  | no |  |
| requested_at | TEXT |  | no |  |
| original_status | TEXT |  | no | Run status before the request |

### reopen_decision

The Controller decision on a reopen request (CR-06).

| Column | Type | Key | Null | Description |
|---------------------|----------|-----------------|-------|-----------------------------|
| reopen_decision_id | INTEGER | PK | no |  |
| reopen_request_id | INTEGER | FK → reopen_request | no | Reopen request this decision answers |
| decision | TEXT |  | no |  |
| comment | TEXT |  | yes | Free-text reviewer comment |
| decided_by | INTEGER | FK → app_user | no | Identity that made the decision |
| decided_at | TEXT |  | no | UTC timestamp of the decision |
| revised_status | TEXT |  | no | Run status after the decision |

### report

A generated report file with its content hash (RPT-01..13).

| Column | Type | Key | Null | Description |
|-----------------|----------|---------------------|-------|-------------------------|
| report_id | INTEGER | PK | no |  |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| report_code | TEXT |  | no | RPT-01..RPT-13 |
| output_format | TEXT |  | no |  |
| file_path | TEXT |  | no |  |
| content_sha256 | TEXT |  | no | Hash of the generated file, so a report can be shown to be unchanged |
| generated_at | TEXT |  | no |  |

### audit_event

One system or human action. Append-only and hash-chained (FR-AUD-01..07).

| Column | Type | Key | Null | Description |
|-------------------|----------|---------------------|-------|---------------------------------------|
| audit_event_id | INTEGER | PK | no |  |
| run_id | INTEGER | FK → reconciliation_run | no | Owning reconciliation run |
| sequence_no | INTEGER |  | no | Position in the run chain, unique per run |
| event_type | TEXT |  | no | IMPORT, VALIDATION, NORMALIZATION, RULE, AI_RECOMMENDATION, ROUTING, DECISION, ADJUSTMENT, REPORT, SIGNOFF, REOPEN, BLOCKED_ATTEMPT |
| process_code | TEXT |  | no | P1..P8 from the data flow diagram |
| bank_account_id | INTEGER | FK → bank_account | no | Account in scope |
| period_start | TEXT |  | no |  |
| period_end | TEXT |  | no |  |
| actor_type | TEXT |  | no | human or system; actor_user_id is required for human events |
| actor_user_id | INTEGER | FK → app_user | yes |  |
| entity_type | TEXT |  | no |  |
| entity_id | INTEGER |  | yes |  |
| item_refs | TEXT |  | yes | JSON array of affected transaction identifiers |
| original_values | TEXT |  | yes | JSON of values before the action |
| revised_values | TEXT |  | yes | JSON of values after the action |
| rule_name | TEXT |  | yes |  |
| model_version_id | INTEGER | FK → model_version | yes | Model version in use |
| candidate_scores | TEXT |  | yes | JSON array of all candidate scores at the time of the event |
| confidence | REAL |  | yes |  |
| risk_level | TEXT |  | yes |  |
| explanation | TEXT |  | yes |  |
| decision | TEXT |  | yes |  |
| comment | TEXT |  | yes | Free-text reviewer comment |
| approval_status | TEXT |  | yes |  |
| evidence_refs | TEXT |  | yes | References to supporting documents or records |
| previous_status | TEXT |  | yes | Status before the action |
| new_status | TEXT |  | yes | Status after the action |
| previous_hash | TEXT |  | no | event_hash of the previous event, GENESIS for the first |
| event_hash | TEXT |  | no | SHA-256 over the event content and previous_hash |
| created_at | TEXT |  | no | UTC timestamp when the row was written |

## 7. Retention and Privacy

| Point | Position |
|---------------|--------------------------------------------------------------------|
| Data source | Synthetic only, generated locally from a seed |
| Personal data | None. Counterparty and staff names are invented |
| External transmission | None by default. With the optional GenAI adapter enabled, only synthetic evidence text is sent (DD-10, NFR-05) |
| Deletion | No table supports deletion of evidence; decisions and audit events are append-only |
| Database file | Local SQLite file, recreated from the generator at any time |

## 8. Traceability

| Requirement | Where satisfied |
|--------------------------------------------------|------------------------------|
| FR-IMP-01..03 CSV imports | §3.1–3.3 |
| FR-IMP-04 hash and control totals | §3.4, `source_file` |
| FR-VAL-03 control totals | §3.4, §4.4 |
| FR-NRM-01..05 normalization with originals | §5, `normalization_change` |
| FR-AI-07 model version on recommendations | `recommendation`, `model_version` |
| FR-AUD-02 minimum audit fields | `audit_event` |
| FR-EVL-11 separate calibration and evaluation data | §4.3 |
| SCN-01..08 coverage | §4.1 |
| DD-07 group caps | §4.1 |
| DD-09 approximately 1,240 transactions | §4.1 |
| DD-12 carry-in items and full statement | §3.3, §4.4 |
| SC-06 reproducibility | §4.3 |
