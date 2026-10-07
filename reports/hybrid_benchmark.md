# Hybrid Benchmark - rules only against rules plus scoring

Both columns are the same code over the same data. The only difference is whether the
scoring layer ran, so the comparison measures the AI layer rather than two systems.

| Setting | Value |
|---|---|
| Dataset | `data/august_2026` |
| Calibration artefact | calibration-july_2026_calibration (isotonic, 793 points) |
| Calibration data | data/july_2026_calibration |
| Senior approval threshold | 1000000 cents |
| Confidence bands | High at or above 0.9, Low below 0.7 |

## Headline

| Measure | Rules only | Hybrid |
|---|---|---|
| Matches proposed | 390 | 556 |
| Correct | 390 | 556 |
| Incorrect | 0 | 0 |
| **Precision** | **1.0000 (n=390)** | **1.0000 (n=556)** |
| **Recall** | **0.6866 (n=568)** | **0.9789 (n=568)** |
| Exception accuracy | 0.8434 (n=83) | 0.8734 (n=79) |
| Items left unmatched for a reviewer | 443 | 85 |
| Recommendations created | 867 | 675 |
| Elapsed | 0.4s | 0.4s |

## Recall by scenario

| Scenario | Rules only | Hybrid | Groups in data |
|---|---|---|---|
| SCN-01 | 1.0000 | 1.0000 | 390 |
| SCN-02 | 0.0000 | 1.0000 | 95 |
| SCN-03 | 0.0000 | 1.0000 | 55 |
| SCN-04 | 0.0000 | 1.0000 | 12 |
| SCN-05 | 0.0000 | 0.0000 | 10 |
| SCN-07 | 0.0000 | 0.6667 | 6 |

## Performance by confidence band

Scored matches only. Rule-settled matches carry no confidence value.

| Band | Matches | Correct | Wrong | Accuracy |
|---|---|---|---|---|
| High | 103 | 103 | 0 | 1.0000 |
| Medium | 52 | 52 | 0 | 1.0000 |
| Low | 11 | 11 | 0 | 1.0000 |

### Calibration on unseen data

The artefact was fitted on data/july_2026_calibration. These figures are the first
time it has met this dataset.

- Expected calibration error: 0.1234
- Worst band error: 0.9800
- Brier score: 0.0707

| Band | n | Stated | Observed | Error |
|---|---|---|---|---|
| 0.0-0.1 | 11 | 0.020 | 1.000 | 0.980 |
| 0.8-0.9 | 52 | 0.853 | 1.000 | 0.147 |
| 0.9-1.0 | 103 | 0.980 | 1.000 | 0.020 |

## Hypothetical automatic acceptance (DD-01)

Nothing is ever accepted without a reviewer (CR-01). This measures what **would**
have happened under a fixed policy defined before the measurement: an exact
rule-settled match with no risk flag, or a scored match at or above 0.95 confidence with no risk flag.

| Measure | Rules only | Hybrid |
|---|---|---|
| Items that would have been auto-accepted | 378 | 467 |
| Of those, incorrect | 0 | 0 |
| **False automatic match rate** | **0.0000 (n=378)** | **0.0000 (n=467)** |

## Review workload

| Measure | Rules only | Hybrid |
|---|---|---|
| Review rate | 1.0000 by design | 1.0000 by design |
| Items requiring a decision | 867 | 675 |
| Of those, exceptions | 477 | 119 |

## Incorrect scored matches

None on this dataset. That is a property of this data, not a guarantee: the
scenarios here are generated, and a real month would contain ambiguities this
dataset does not.

## Reading these numbers

Recall is the measure that moved. Precision was already 1.0 for the rules, and the
question was whether scoring could raise coverage without lowering it.

Every figure here is measured against labelled data that the system never sees: no
module under `app/` reads the ground truth file. The scoring layer was tuned on a
different month, so this is the first contact between the fitted artefact and this
data.

The review rate stays at 1.0 in both columns because every item requires human
approval by design. The AI layer changes how much judgement each decision needs,
not how many decisions there are.
