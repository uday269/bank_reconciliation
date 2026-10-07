# Evaluation Report — AI-Assisted Bank Reconciliation

## 1. Purpose and Method

This report measures what the system proposes, what scoring adds over rules alone, how far confidence can be trusted, and what people did with the proposals. It answers the evaluation measures of brief section 11 (FR-EVL-01..11) and the design questions of brief section 15.

| Point | Method |
|------------------|------------------------------------------------------------------|
| Data | August 2026, seed 20260801: 612 bank transactions, 628 ledger entries, 18 carry-in items, all eight scenarios labelled in `ground_truth.csv`. One bank row is excluded by validation, leaving 1,257 items |
| Calibration data | July 2026, seed 20260701, used only to fit the calibrator (DD-11). No August data was used in fitting |
| Configurations | Rules only and rules plus scoring, each on its own fresh database, differing in one switch (FR-EVL-09) |
| Correct match | Every record in the proposal carries the same true match group |
| Hypothetical auto-accept (BR-19) | An exact match with Low risk, or a scored match with confidence ≥ 0.95 (PRM-09) and Low risk. Computed, never executed (DD-01) |
| Counts | Every rate is shown with the counts it came from (FR-EVL-10) |
| Reproduce | `python -m app.cli evaluate --run N` writes `reports/evaluation.md`. Only the evaluator reads ground truth (FR-EVL-11) |

## 2. Results at a Glance

| Measure | Rules only | Rules plus scoring |
|-----------------------------------|----------------------|----------------------|
| Precision | 1.0000 (390/390) | **1.0000 (556/556)** |
| Recall | 0.6866 (390/568) | **0.9789 (556/568)** |
| F1 | 0.8142 | **0.9893** |
| Exception classification accuracy | 0.8434 (70/83) | 0.8734 (69/79) |
| Risk level agreement | 0.7624 (661/867) | 0.9348 (631/675) |
| Recommendations to review | 867 | **675** |
| Hypothetical false automatic match rate | — | **0.0000 (0/467)** |
| Review rate | 100% by design | 100% by design |
| Unresolved difference before review | — | -$77,663.05 |
| Unresolved difference, correct | -$9,392.35 | -$9,392.35 |
| Time, import to routed queues | 0.31 s | 0.45 s |

Scoring finds 166 matches the rules cannot, adds no incorrect match, and leaves 192 fewer recommendations to review.

## 3. Matching

| Scenario | Rules only | Rules plus scoring | What scoring adds |
|-----------------------|------------------|------------------|----------------------------------|
| SCN-01 Exact | 1.0000 (390/390) | 1.0000 (390/390) | Nothing to add |
| SCN-02 Timing difference | 0.0000 (0/95) | 1.0000 (95/95) | Date distance as evidence, not a filter |
| SCN-03 Name variation | 0.0000 (0/55) | 1.0000 (55/55) | Text similarity on normalized names |
| SCN-04 One to many | 0.0000 (0/12) | 1.0000 (12/12) | Group search, bank item to several ledger entries |
| SCN-05 Many to one | 0.0000 (0/10) | 0.0000 (0/10) | Not found; see section 8 |
| SCN-07 Duplicate | 0.0000 (0/6) | 0.6667 (4/6) | The genuine copy; see section 8 |

Precision stays at 1.0000 because every scored match must also pass the amount, direction and member checks that bound the search. That figure is for one synthetic month and is likely optimistic for real data (section 9).

## 4. Exceptions and Risk

Exception accuracy counts only items that truly are exceptions, so a record wrongly left unmatched is a recall miss, not an exception error.

| Correct category | Proposed as | Items | Why |
|---------------------|------------------|------:|--------------------------------------------|
| EXC-07 Unexplained | EXC-02 Deposit in transit | 3 | A late ledger receipt with no bank counterpart looks like a timing item |
| EXC-07 Unexplained | EXC-06 Missing ledger entry | 2 | A bank item with no ledger counterpart looks like an unrecorded bank charge |
| EXC-07 Unexplained | EXC-01 Outstanding check | 2 | A late ledger payment looks like an uncleared check |
| EXC-05 Duplicate | EXC-01 Outstanding check | 2 | The extra copy of a duplicated payment looks like an uncleared check |
| EXC-07 Unexplained | EXC-08 Capped search | 1 | The group search stopped at its cap |

All ten disagreements share one shape: an item with no counterpart is classified by the most common reason for having none. From the data alone they are indistinguishable; the reviewer resolves them by marking the item unresolved, which keeps it on the statement.

Risk agreement with the labels is 0.9348 (631/675). Of the 44 disagreements, 36 are one step apart on routine items: Low against Medium on name variations, groups, bank-originated items and one timing item. **Eight are more serious: six unexplained items and two duplicates labelled High were assessed Medium or Low**, because their misclassification (above) also gave them a timing item's risk. This is the most important weakness found; section 9 and the batch-approval answer in section 10 return to it.

## 5. Confidence and Calibration

| Band | Proposed | Correct | Observed precision |
|---------|------:|------:|---------------------|
| High (≥ 0.90) | 103 | 103 | 1.0000 (103/103) |
| Medium (0.70–0.90) | 52 | 52 | 1.0000 (52/52) |
| Low (< 0.70) | 11 | 11 | 1.0000 (11/11) |

| Stated confidence | n | Stated | Observed | Error |
|-------------------|----:|-------:|---------:|------:|
| 0.0–0.1 | 11 | 0.0200 | 1.0000 | 0.9800 |
| 0.8–0.9 | 52 | 0.8529 | 1.0000 | 0.1471 |
| 0.9–1.0 | 103 | 0.9800 | 1.0000 | 0.0200 |

Expected calibration error 0.1234, Brier score 0.0707, n = 166.

**The calibrator is underconfident on August data.** Fitted on July, it maps a raw score near 0.6 to 0.02, because in July most candidates at that score were wrong. In August every one of those eleven was right. Recommendation 1930 is typical: bank deposit BT-000541 for $5,376.40, matched to four ledger entries that sum to it exactly on the same date, stated at 0.02 because the counterparty names are dissimilar (text similarity 0.27).

This is a distribution shift between two months of synthetic data, and it is the strongest argument in this report for the design choice that confidence orders the queue and never decides. A threshold fitted on one month would have rejected eleven correct matches the next.

**A measurement error was found and corrected.** The rules-plus-scoring benchmark from the AI layer passed already-calibrated confidence through the calibrator a second time. It reported an expected calibration error of 0.0836; measured correctly, the error is 0.1234. Both the benchmark and this evaluator now assess the raw score once, and the evaluator's tests pin the figures (TC-I-074).

## 6. Automation That Was Not Used

| Measure | Value |
|----------------------------------------|----------------------------------------|
| Review rate | **100% by design**: every item needs a recorded human decision (DD-03) |
| Matches BR-19 would have accepted | 467: 378 exact matches and 89 scored matches |
| Hypothetical false automatic match rate | **0.0000 (0/467)** (DD-01) |
| Items still needing review under BR-19 | 0.2570 (323/1,257) |

Zero errors in 467 is not a zero error rate. With no failures observed, the upper bound of a 95% confidence interval is about 3 / n: **0.64% for all 467, and 3.4% for the 89 scored matches alone**. Section 10 sets out what further evidence that bound would need.

## 7. Review Results

*Measured on run 3, reviewed in the reviewer interface. This section is completed from `python -m app.cli evaluate --run 3` once the run is reviewed and signed.*

| Measure | Value |
|----------------------------------------|----------------------------------------|
| Recommendations decided | pending |
| Approve, modify, reject, escalate, unresolved rates (FR-EVL-04) | pending |
| Approvals correct against ground truth | pending |
| Pairings made by hand | pending |
| Time per item, per person (FR-EVL-05, DD-04) | pending; "not measured" for anyone with no timed decisions |
| Audit completeness (FR-EVL-07) | pending |
| Unresolved difference at sign-off | pending; correct value -$9,392.35 |

The automated end-to-end test of the same review (TC-S-008) already shows the target: every approval correct, audit completeness 100%, chain intact, and a signed difference of -$9,392.35.

## 8. Error Analysis

| Miss | Cause | Effect | Remedy |
|-----------------------------|----------------------------------------------|----------------------------|------------------------------|
| SCN-05, 10 of 10 many-to-one groups | Group search starts from a bank item and looks for several ledger entries. Several bank deposits settling one ledger entry needs the reverse search, which was not built | 30 bank items left as exceptions (27 hit the search cap, 3 look like missing entries); 10 ledger entries left as outstanding checks | Reviewers pair them by hand (ADR-23), which brings the statement to the correct difference. An automated fix is a symmetric search with the same cap |
| SCN-07, 2 of 6 duplicate groups | A ledger payment duplicated by the bank: one genuine copy matches, the extra copy must not. The labelled group holds both, so it cannot be found whole | None on the statement: the extra copy is an exception, as it should be | A scoring definition point, not a system fault |
| 28 group searches capped (EXC-08) | Subset search is exponential; the cap (15 candidates, 4 members) is disclosed (DD-07) | Shown to reviewers as "search incomplete", never as no match | Hand pairing; or a larger cap, at measured cost |
| 10 exception misclassifications | Section 4 | Wrong category on the screen | Reviewer marks unresolved; investigate-only categories cannot be approved (ADR-25) |
| 8 high-risk items assessed lower | Section 4 | The most important weakness found | Section 9 |
| Calibration underconfident | Section 5 | Correct matches stated at 0.02 | Confidence orders, never decides; recalibrate on more months |

## 9. Limitations

- **One synthetic month.** Every figure comes from 1,240 generated transactions. Real statements carry noise the generator does not model, so precision of 1.0000 should be read as "no error found here", not as an expected production rate.
- **Ground truth and system share an author.** The generator and the matcher were written by the same team, so blind spots can coincide. An independently labelled sample would be the stronger test.
- **One calibration month.** Section 5 shows what one month of shift does. Calibration needs several months and periodic refitting.
- **Risk can follow a wrong category.** An unexplained item classified as a timing item gets a timing item's risk (section 4). A reviewer who trusts the category could approve it as an outstanding check. The mitigations are the statement, which keeps such items visible, and the stale-item rule; a stronger fix is to raise risk for any no-counterpart item above a value threshold.
- **Many-to-one matching is manual.** SCN-05 relies on hand pairing (section 8).
- **Time per item is a small sample.** At most two people and about fifty items each (DD-04), on synthetic data in a quiet setting. It indicates effort; it does not predict a team's throughput.

## 10. Design Questions (brief section 15)

| Question | Answer, with the evidence |
|-------------------------|----------------------------------------------------------------------|
| Which rules should always be deterministic? | Exact matching (BR-01..BR-07), exception categories (BR-08..BR-12), risk and routing (BR-13..BR-15), every control (CR-01..CR-18) and the statement arithmetic (BR-17). Exact rules settle 390 matches with no error (section 3); risk must stay inspectable because its failures matter most (section 4) |
| Which transactions require senior approval? | Anything High risk: at or above $10,000 (PRM-01), suspected duplicates, unexplained items; plus anything a first-level reviewer escalates. A Staff Accountant cannot settle such an item directly, or indirectly through modify or hand pairing (CR-02, TC-I-019) |
| How will confidence be calibrated and communicated? | Isotonic regression fitted on a separate month (DD-11), shown as a number and a band beside the supporting and conflicting evidence, and used only to order queues. Section 5 shows why it must never decide: one month of shift made eleven correct matches look 2% likely |
| How are group candidates generated efficiently? | Candidates are filtered by direction, date window and amount first, then a meet-in-the-middle subset search runs within a disclosed cap of 15 candidates and 4 members (DD-07). Import to routed queues takes 0.45 s for the month; 28 searches reach the cap and are flagged, not hidden |
| How does the design prevent over-trust in AI? | No automatic acceptance (review rate 100%); every candidate and all conflicting evidence shown; nothing preselected; detail view required before approving a scored item; comments required to override; generated prose labelled and unable to state a decision; confidence never a filter (CR-15, CR-16, FR-REV-09, FR-REV-10, CR-13) |
| How do corrections preserve audit history? | Nothing is updated or deleted: decisions, sign-offs and reopenings are new rows; superseded recommendations keep their candidates; every action, and every refused action, is a hash-chained event (CR-09, ADR-12). The chain head printed at sign-off makes tampering detectable |
| What evidence would justify limited batch approval in production? | See below |

### Evidence that would justify limited batch approval

Batch approval today is one reviewer action over exact, Low-risk matches, still recorded per item (DD-02). Letting the system accept any subset *without* a reviewer action (BR-19) would need, at minimum:

| Evidence | Why | Current state |
|------------------------------------|--------------------------------------------|--------------------------|
| Zero errors over enough items that the upper bound is acceptably low: about 3,000 for 0.1% | With no failures, the 95% bound is about 3 / n | 467 items: bound 0.64% |
| The same result across several months and accounts, on real data | One synthetic month cannot show stability; section 5 shows a month of shift | One synthetic month |
| Restricted to exact matches first | They carry no model risk; scored matches add calibration risk | 378 of the 467 are exact |
| Risk assessment that does not follow a wrong category | Section 4: eight high-risk items were assessed lower | Not yet |
| Ongoing sample audits of accepted items, with the policy and threshold under change control | Detects drift after the decision | Designed, not built |

On this evidence the honest conclusion is that batch approval by a reviewer is justified now, and automatic acceptance is not.
