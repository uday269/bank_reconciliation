# Baseline Benchmark - deterministic rules only

Produced by `scripts/baseline_benchmark.py`. These are the figures the hybrid system is
compared against in DOC-07. No scoring, no confidence and no model are involved: the
system either proves a match by rule or leaves the item for a reviewer.

| Setting | Value |
|---|---|
| Dataset | `data/august_2026` |
| Model version | baseline-rules-only |
| Senior approval threshold (PRM-01) | 1000000 cents |
| Elapsed | 0.1s |

## Matching

| Measure | Value |
|---|---|
| Matches proposed | 390 |
| Correct | 390 |
| Incorrect | 0 |
| **Precision** | **1.0000** |
| True match groups in the data | 568 |
| Groups found | 390 |
| **Recall** | **0.6866** |

## Recall by scenario

| Scenario | Groups found | Groups in data | Recall |
|---|---|---|---|
| SCN-01 | 390 | 390 | 1.0000 |
| SCN-02 | 0 | 95 | 0.0000 |
| SCN-03 | 0 | 55 | 0.0000 |
| SCN-04 | 0 | 12 | 0.0000 |
| SCN-05 | 0 | 10 | 0.0000 |
| SCN-07 | 0 | 6 | 0.0000 |

## Matches by rule

| Rule | Matches |
|---|---|
| BR-01 | 378 |
| BR-11 | 12 |

## Exceptions

| Measure | Value |
|---|---|
| Exceptions raised | 477 |
| Items with a labelled exception code | 83 |
| Baseline agrees with the label | 70 |
| **Exception accuracy** | **0.8434** |

## Workload

| Measure | Value |
|---|---|
| Recommendations created | 867 |
| Items left unmatched for a reviewer | 443 |
| Review rate | 1.0000 (every item requires approval, by design) |

## Queues

| Category | Items |
|---|---|
| CAT-01 | 378 |
| CAT-04 | 389 |
| CAT-05 | 100 |

## Audit

| Measure | Value |
|---|---|
| Events written | 873 |
| Chain | Chain intact: 873 events, head cca027b33f63… |

## Reading these numbers

Precision of 1.0 is expected and is not an achievement: a rule that acts only when it
is certain cannot propose a wrong match. The numbers that matter are recall and the
count of items left for a person to resolve by hand (443).
Closing that gap without losing precision is what the AI layer has to do.

Exception accuracy is lower than matching accuracy for the same reason: without
scoring, an unmatched timing difference looks exactly like an outstanding check, so
the baseline files it as one.
