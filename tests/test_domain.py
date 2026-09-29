"""Unit tests for the domain layer (TC-U): normalization, rules, risk and routing.

Pure functions, no database, so each test states its inputs in full.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.normalize import (
    NormalizationError, business_days_between, normalize_amount_to_cents, normalize_date,
    normalize_description, normalize_direction, normalize_item, normalize_reference,
)
from app.domain.risk import assess_risk, assign_category, confidence_band
from app.domain.rules import (
    Item, apply_rules, classify_bank_originated, classify_period_end_item,
    classify_unmatched_bank_item, find_carry_in_clearings, find_exact_matches,
)
from app.domain import exceptions as exception_catalog


def bank_item(item_id=1, amount=125000, direction="credit", description="CANYON RIDGE GROCERY",
              reference="8842", type_code="ACH-CR", duplicate=False, day=10):
    return Item("bank", item_id, f"BT-{item_id:06d}", date(2026, 8, day), amount, direction,
                description, reference, description, type_code, duplicate)


def ledger_item(item_id=1, amount=125000, direction="debit", description="CANYON RIDGE GROCERY",
                reference="8842", day=10):
    return Item("ledger", item_id, f"GL-{item_id:06d}", date(2026, 8, day), amount, direction,
                description, reference, description)


# -- normalization ----------------------------------------------------------

def test_tcu_001_amount_parsing_accepts_common_formats():
    assert normalize_amount_to_cents("1234.56") == 123456
    assert normalize_amount_to_cents("$1,234.56") == 123456
    assert normalize_amount_to_cents("(78.50)") == 7850
    assert normalize_amount_to_cents("640") == 64000


@pytest.mark.parametrize("value", ["N/A", "", "abc", "12.345", "0.00"])
def test_tcu_002_amount_parsing_rejects_unusable_values(value):
    with pytest.raises(NormalizationError):
        normalize_amount_to_cents(value)


def test_tcu_003_dates_normalize_to_iso():
    assert normalize_date("2026-08-14") == "2026-08-14"
    assert normalize_date("08/14/2026") == "2026-08-14"
    with pytest.raises(NormalizationError):
        normalize_date("not a date")


def test_tcu_004_payee_extraction_converges_on_the_counterparty():
    """SCN-03: an abbreviated bank description and a full ledger name must agree."""
    bank_payee = normalize_description("ACH DEP CANYON RIDGE GRCRY 8842")[2]
    ledger_payee = normalize_description("Canyon Ridge Grocery - invoice 8842")[2]
    assert bank_payee == ledger_payee == "CANYON RIDGE GROCERY"


def test_tcu_005_references_strip_prefixes_and_leading_zeros():
    assert normalize_reference("INV8842") == "8842"
    assert normalize_reference("CHK#04101") == "4101"
    assert normalize_reference("") is None


def test_tcu_006_original_values_are_never_returned_as_changed_when_identical():
    values, changes = normalize_item("CANYON RIDGE GROCERY", "8842")
    assert values["description_normalized"] == "CANYON RIDGE GROCERY"
    assert all(change.field_name != "description" for change in changes)


def test_tcu_007_business_day_gap_is_signed_and_skips_weekends():
    assert business_days_between(date(2026, 8, 14), date(2026, 8, 17)) == 1      # Fri to Mon
    assert business_days_between(date(2026, 8, 17), date(2026, 8, 14)) == -1
    assert business_days_between(date(2026, 8, 14), date(2026, 8, 14)) == 0


# -- deterministic rules ----------------------------------------------------

def test_tcu_010_exact_match_requires_amount_reference_and_date():
    matches, _ = find_exact_matches([bank_item()], [ledger_item()])
    assert len(matches) == 1
    assert matches[0].rule_name == "BR-01"


def test_tcu_011_uniqueness_prevents_a_false_exact_match():
    """BR-02: two pairs sharing amount, date and reference are left for scoring."""
    bank = [bank_item(1), bank_item(2)]
    ledger = [ledger_item(1), ledger_item(2)]
    matches, _ = find_exact_matches(bank, ledger)
    assert matches == []


def test_tcu_012_flagged_duplicate_is_never_auto_matched():
    matches, _ = find_exact_matches([bank_item(duplicate=True)], [ledger_item()])
    assert matches == []


def test_tcu_013_cash_effect_reconciles_the_two_direction_conventions():
    assert bank_item(direction="credit").cash_effect == ledger_item(direction="debit").cash_effect
    assert bank_item(direction="debit").cash_effect < 0


def test_tcu_014_transaction_code_beats_description_keywords():
    """Regression: 'TIMPANOGOS COFFEE' must not read as a fee because it contains FEE."""
    receipt = bank_item(description="TIMPANOGOS COFFEE INV8262", type_code="ACH-CR")
    assert classify_bank_originated(receipt) is None
    assert classify_bank_originated(bank_item(description="MONTHLY ANALYSIS FEE",
                                              type_code="FEE"))[0] == "EXC-03"
    assert classify_bank_originated(bank_item(description="INTEREST CREDIT",
                                              type_code="INT"))[0] == "EXC-04"


def test_tcu_015_keywords_only_match_whole_words():
    no_code = bank_item(description="COFFEE COMPANY PAYMENT", type_code=None)
    assert classify_bank_originated(no_code) is None
    assert classify_bank_originated(bank_item(description="WIRE FEE", type_code=None))[0] == "EXC-03"


def test_tcu_016_carry_in_clearing_needs_the_opposite_cash_effect():
    check = Item("carry_in", 1, "CI-0001", date(2026, 7, 20), 41000, "credit",
                 "Check 4101", "4101", None, carry_in_type="outstanding_check")
    paid = bank_item(amount=41000, direction="debit", description="4101", reference="4101")
    received = bank_item(amount=41000, direction="credit", description="4101", reference="4101")
    assert len(find_carry_in_clearings([paid], [check], 3)[0]) == 1
    assert find_carry_in_clearings([received], [check], 3)[0] == []


def test_tcu_017_stale_items_change_the_rule_and_the_action():
    recent = ledger_item(amount=120000, direction="credit", day=28)
    stale = Item("carry_in", 2, "CI-0002", date(2026, 6, 20), 88000, "credit", "Check 3900",
                 "3900", None, carry_in_type="outstanding_check")
    period_end = date(2026, 8, 31)
    assert classify_period_end_item(recent, period_end, 45, 5).rule_name == "BR-08"
    assert classify_period_end_item(stale, period_end, 45, 5).rule_name == "BR-10"


def test_tcu_018_unmatched_bank_item_splits_into_missing_or_unexplained():
    known = {"CANYON RIDGE GROCERY"}
    assert classify_unmatched_bank_item(bank_item(), known).exception_code == "EXC-06"
    unknown = bank_item(description="MISC CREDIT ADJ 4471", reference=None)
    assert classify_unmatched_bank_item(unknown, known).exception_code == "EXC-07"


def test_tcu_019_rules_run_in_order_and_report_what_is_left():
    fee = bank_item(9, 7850, "debit", "MONTHLY ANALYSIS FEE", None, "FEE")
    outcome = apply_rules([bank_item(), fee], [ledger_item()], [],
                          period_end=date(2026, 8, 31), timing_window_days=3,
                          stale_check_days=45, stale_deposit_days=5)
    assert len(outcome.matches) == 1
    assert [e.exception_code for e in outcome.exceptions] == ["EXC-03"]
    assert outcome.unmatched_bank == []


# -- risk and routing -------------------------------------------------------

def test_tcu_020_risk_takes_the_highest_triggered_level():
    assessment = assess_risk(amount_cents=1_200_000, senior_approval_amount_cents=1_000_000,
                             is_group=True, counterparty="NEW PARTY", known_counterparties=set())
    assert assessment.level == "high"
    assert set(assessment.triggered) == {"RR-01", "RR-04", "RR-05"}
    assert assessment.requires_senior_approval


def test_tcu_021_no_triggered_rule_is_recorded_as_low():
    assessment = assess_risk(amount_cents=5000, senior_approval_amount_cents=1_000_000,
                             counterparty="KNOWN", known_counterparties={"KNOWN"})
    assert assessment.level == "low"
    assert assessment.triggered == ["RR-07"]


def test_tcu_022_high_risk_exact_match_never_enters_a_batch():
    """CR-11: risk is tested before the exact rule, so a large match goes to CAT-05."""
    assert assign_category(risk_level="high", kind="match", source="rule")[0] == "CAT-05"
    assert assign_category(risk_level="low", kind="match", source="rule")[0] == "CAT-01"


def test_tcu_023_a_close_runner_up_makes_an_item_ambiguous():
    clear, _ = assign_category(risk_level="low", kind="match", source="ai",
                               confidence=0.94, runner_up_confidence=0.55)
    close, _ = assign_category(risk_level="low", kind="match", source="ai",
                               confidence=0.94, runner_up_confidence=0.88)
    assert clear == "CAT-02"
    assert close == "CAT-03"


def test_tcu_024_confidence_bands_match_the_configured_thresholds():
    assert confidence_band(0.95, 0.90, 0.70) == "High"
    assert confidence_band(0.80, 0.90, 0.70) == "Medium"
    assert confidence_band(0.40, 0.90, 0.70) == "Low"
    assert confidence_band(None, 0.90, 0.70) == "not scored"


def test_tcu_025_every_exception_category_has_an_action():
    for code, category in exception_catalog.CATEGORIES.items():
        assert category.next_action
        assert category.default_disposition
    assert exception_catalog.statement_section("EXC-01") == "outstanding_checks"
    assert exception_catalog.proposes_adjustment("EXC-03") is True
    assert exception_catalog.proposes_adjustment("EXC-05") is False
