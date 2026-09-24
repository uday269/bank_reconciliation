#!/usr/bin/env python3
"""
Generate the synthetic reconciliation dataset described in DOC-04.

Deterministic: the same seed always produces identical files. Standard library only.

Usage
  python3 scripts/generate_dataset.py                       August 2026 evaluation dataset
  python3 scripts/generate_dataset.py --profile calibration July 2026 calibration dataset
  python3 scripts/generate_dataset.py --seed 20260801 --out data/august_2026

Files written
  bank_statement.csv   imported as file_type = bank
  gl_cash_detail.csv   imported as file_type = gl
  carry_in_items.csv   imported as file_type = carry_in
  run_control.csv      declared row counts, control totals and balances
  ground_truth.csv     correct answer per item; read by the evaluator only (FR-EVL-11)
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import random
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Reference data (fictional)
# ---------------------------------------------------------------------------

CUSTOMERS = [
    ("Canyon Ridge Grocery", ["CANYON RIDGE GRCRY", "CYN RIDGE GROC", "CANYONRIDGE GROC LLC"]),
    ("Alta Vista Bistro", ["ALTA VISTA BISTRO", "ALTAVISTA BSTRO", "A VISTA BISTRO LLC"]),
    ("Silver Fork Cafe", ["SILVER FORK CAFE", "SLVR FORK CAFE", "SILVERFORK CF"]),
    ("Redrock Provisions", ["REDROCK PROVISIONS", "RED ROCK PROV", "REDRCK PROVSNS"]),
    ("Wasatch Deli Group", ["WASATCH DELI GRP", "WSTCH DELI", "WASATCH DELI GROUP INC"]),
    ("Bearclaw Bakery", ["BEARCLAW BAKERY", "BEAR CLAW BKRY", "BRCLAW BAKERY"]),
    ("Juniper Table", ["JUNIPER TABLE", "JUNPR TABLE", "JUNIPER TBL LLC"]),
    ("Snowbasin Catering", ["SNOWBASIN CATERING", "SNOWBSN CATER", "SNOW BASIN CTRG"]),
    ("Park City Market", ["PARK CITY MARKET", "PC MARKET", "PARKCITY MKT"]),
    ("Ogden Street Foods", ["OGDEN STREET FOODS", "OGDEN ST FOODS", "OGDN STREET FD"]),
    ("Lakeside Grill", ["LAKESIDE GRILL", "LAKESIDE GRL", "LKSIDE GRILL LLC"]),
    ("Timpanogos Coffee", ["TIMPANOGOS COFFEE", "TIMP COFFEE", "TIMPANOGOS COF CO"]),
    ("Bonneville Bagels", ["BONNEVILLE BAGELS", "BONNEV BAGELS", "BVILLE BAGEL CO"]),
    ("Granite Peak Foods", ["GRANITE PEAK FOODS", "GRANITE PK FDS", "GRNT PEAK FOODS"]),
    ("Emigration Kitchen", ["EMIGRATION KITCHEN", "EMIGRTN KTCHN", "EMIGRATION KTCH"]),
    ("Red Butte Diner", ["RED BUTTE DINER", "RD BUTTE DNR", "REDBUTTE DINER"]),
    ("Copper Ridge Market", ["COPPER RIDGE MARKET", "COPPER RDG MKT", "CPR RIDGE MARKET"]),
    ("Heber Valley Foods", ["HEBER VALLEY FOODS", "HEBER VLY FDS", "HBR VALLEY FOOD"]),
    ("Saltair Supper Club", ["SALTAIR SUPPER CLUB", "SALTAIR SUPPER", "SALT AIR SPR CLB"]),
    ("Provo Provisions", ["PROVO PROVISIONS", "PROVO PROVSNS", "PRV PROVISIONS"]),
    ("Cottonwood Catering", ["COTTONWOOD CATERING", "CTNWD CATERING", "COTTONWD CTRG"]),
    ("Logan Canyon Cafe", ["LOGAN CANYON CAFE", "LOGAN CYN CAFE", "LGN CANYON CF"]),
    ("Moab Trading Post", ["MOAB TRADING POST", "MOAB TRDG POST", "MOAB TRADE PST"]),
    ("Brighton Bistro", ["BRIGHTON BISTRO", "BRGHTN BISTRO", "BRIGHTON BSTR LLC"]),
]

VENDORS = [
    ("Wasatch Produce Partners", ["WASATCH PRODUCE", "WSTCH PRODUCE PTNR", "WASATCH PROD PARTNERS"]),
    ("High Desert Dairy", ["HIGH DESERT DAIRY", "HI DESERT DAIRY", "HGH DSRT DAIRY"]),
    ("Sundance Freight Lines", ["SUNDANCE FREIGHT", "SUNDNC FRGHT LN", "SUNDANCE FRT LINES"]),
    ("Beehive Packaging", ["BEEHIVE PACKAGING", "BEEHIVE PKG", "BEEHV PACKAGING"]),
    ("Cache Valley Creamery", ["CACHE VALLEY CREAMERY", "CACHE VLY CRMRY", "CV CREAMERY"]),
    ("Rocky Mountain Power", ["ROCKY MOUNTAIN POWER", "RCKY MTN POWER", "RM POWER UTIL"]),
    ("Zion Cold Storage", ["ZION COLD STORAGE", "ZION CLD STRG", "ZION COLDSTORE"]),
    ("Dominguez Olive Oils", ["DOMINGUEZ OLIVE OILS", "DOMINGUEZ OLIVE", "DMGZ OLIVE OIL"]),
    ("Northgate Paper Supply", ["NORTHGATE PAPER", "NGATE PAPER SPLY", "NORTHGT PAPER SUP"]),
    ("Alpine Equipment Repair", ["ALPINE EQUIP REPAIR", "ALPINE EQP RPR", "ALPN EQUIPMENT RPR"]),
    ("Salt Flats Beverage", ["SALT FLATS BEVERAGE", "SALTFLATS BEV", "SF BEVERAGE CO"]),
    ("Weber Fleet Services", ["WEBER FLEET SERVICES", "WEBER FLEET SVC", "WBR FLEET SERV"]),
    ("Cedar Mesa Spices", ["CEDAR MESA SPICES", "CEDAR MESA SPC", "CDR MESA SPICE CO"]),
    ("Union Pacific Logistics", ["UNION PACIFIC LOG", "UP LOGISTICS", "UNION PAC LOGISTIC"]),
    ("Great Basin Insurance", ["GREAT BASIN INSURANCE", "GRT BASIN INS", "GB INSURANCE GRP"]),
    ("Sego Lily Cleaning", ["SEGO LILY CLEANING", "SEGO LILY CLN", "SEGOLILY CLEAN CO"]),
    ("Trailhead Print Works", ["TRAILHEAD PRINT", "TRAILHD PRNT WKS", "TRAILHEAD PRNT WORKS"]),
    ("Deseret Payroll Services", ["DESERET PAYROLL", "DESERET PAYRL SVC", "DSRT PAYROLL SERV"]),
]

NEW_COUNTERPARTIES = [
    ("Antelope Island Foods", ["ANTELOPE ISLAND FOODS", "ANTLP ISL FOODS"]),
    ("Kanab Creek Supply", ["KANAB CREEK SUPPLY", "KANAB CRK SPLY"]),
    ("Fremont Springs Water", ["FREMONT SPRINGS WATER", "FREMONT SPG WTR"]),
]

BANK_PREFIX_IN = ["ACH DEP", "DEPOSIT", "REMOTE DEP", "ACH CREDIT"]
BANK_PREFIX_OUT = ["ACH DEBIT", "CHECK PAID", "POS DEB", "WIRE OUT"]

UNEXPLAINED_BANK = [
    "MISC CREDIT ADJ 4471", "COUNTER CREDIT 8802", "REVERSAL ENTRY 5518",
]
UNEXPLAINED_LEDGER = [
    "Suspense entry pending research", "Reclass from prior month accrual",
    "Cash clearing adjustment", "Unidentified customer remittance", "Branch deposit variance",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    name: str
    seed: int
    period_start: dt.date
    period_end: dt.date
    opening_balance_cents: int
    exact_pairs: int
    timing_pairs: int
    name_variation_pairs: int
    one_to_many_groups: int
    many_to_one_groups: int
    bank_fees: int
    bank_interest: int
    bank_returns: int
    duplicate_bank_pairs: int
    duplicate_ledger_pairs: int
    unexplained_bank: int
    unexplained_ledger: int
    carry_in_checks: int
    carry_in_deposits: int
    carry_in_cleared: int
    outstanding_checks: int
    deposits_in_transit: int
    invalid_rows: int
    out_of_period_rows: int
    high_value_items: int
    out_dir: str


EVALUATION = Profile(
    name="evaluation", seed=20260801,
    period_start=dt.date(2026, 8, 1), period_end=dt.date(2026, 8, 31),
    opening_balance_cents=48_620_177,
    exact_pairs=378, timing_pairs=95, name_variation_pairs=55,
    one_to_many_groups=12, many_to_one_groups=10,
    bank_fees=10, bank_interest=3, bank_returns=3,
    duplicate_bank_pairs=2, duplicate_ledger_pairs=4,
    unexplained_bank=3, unexplained_ledger=5,
    carry_in_checks=14, carry_in_deposits=4, carry_in_cleared=12,
    outstanding_checks=22, deposits_in_transit=11,
    invalid_rows=1, out_of_period_rows=2, high_value_items=13,
    out_dir="data/august_2026",
)

CALIBRATION = Profile(
    name="calibration", seed=20260701,
    period_start=dt.date(2026, 7, 1), period_end=dt.date(2026, 7, 31),
    opening_balance_cents=51_004_880,
    exact_pairs=300, timing_pairs=78, name_variation_pairs=44,
    one_to_many_groups=10, many_to_one_groups=8,
    bank_fees=8, bank_interest=2, bank_returns=2,
    duplicate_bank_pairs=2, duplicate_ledger_pairs=3,
    unexplained_bank=2, unexplained_ledger=4,
    carry_in_checks=11, carry_in_deposits=3, carry_in_cleared=9,
    outstanding_checks=18, deposits_in_transit=7,
    invalid_rows=0, out_of_period_rows=0, high_value_items=9,
    out_dir="data/july_2026_calibration",
)


# ---------------------------------------------------------------------------
# Row containers
# ---------------------------------------------------------------------------

@dataclass
class Dataset:
    bank: list = field(default_factory=list)
    ledger: list = field(default_factory=list)
    carry_in: list = field(default_factory=list)
    truth: list = field(default_factory=list)


def business_days(start: dt.date, end: dt.date) -> list:
    days, day = [], start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += dt.timedelta(days=1)
    return days


def shift_business_days(days: list, day: dt.date, offset: int) -> dt.date:
    if day in days:
        i = days.index(day)
    else:
        i = max(0, sum(1 for d in days if d <= day) - 1)
    return days[min(max(i + offset, 0), len(days) - 1)]


class Builder:
    """Creates rows with stable identifiers and records the ground truth for each."""

    def __init__(self, profile: Profile):
        self.p = profile
        self.rnd = random.Random(profile.seed)
        self.days = business_days(profile.period_start, profile.period_end)
        self.ds = Dataset()
        self._bank_n = 0
        self._ledger_n = 0
        self._carry_n = 0
        self._group_n = 0
        self._check_n = 4100

    # -- identifiers ---------------------------------------------------------
    def next_bank_id(self) -> str:
        self._bank_n += 1
        return f"BT-{self._bank_n:06d}"

    def next_ledger_id(self) -> str:
        self._ledger_n += 1
        return f"GL-{self._ledger_n:06d}"

    def next_carry_id(self) -> str:
        self._carry_n += 1
        return f"CI-{self._carry_n:04d}"

    def next_group(self, scenario: str) -> str:
        self._group_n += 1
        return f"{scenario}-G{self._group_n:04d}"

    def next_check(self) -> str:
        self._check_n += 1
        return str(self._check_n)

    # -- helpers -------------------------------------------------------------
    def amount(self, low: float, high: float) -> int:
        return self.rnd.randrange(int(low * 100), int(high * 100), 5)

    def day(self, first: int = 0, last: int | None = None) -> dt.date:
        last = len(self.days) - 1 if last is None else last
        return self.days[self.rnd.randint(first, last)]

    def customer(self):
        return self.rnd.choice(CUSTOMERS)

    def vendor(self):
        return self.rnd.choice(VENDORS)

    # -- writers -------------------------------------------------------------
    def add_bank(self, date, amount, direction, description, reference, type_code):
        row = {
            "txn_id": self.next_bank_id(), "txn_date": date.isoformat(),
            "amount": f"{amount / 100:.2f}", "direction": direction,
            "description": description, "reference": reference or "",
            "bank_type_code": type_code, "running_balance": "",
            "_amount_cents": amount, "_date": date,
        }
        self.ds.bank.append(row)
        return row

    def add_ledger(self, date, amount, direction, description, reference, journal):
        row = {
            "entry_id": self.next_ledger_id(), "posting_date": date.isoformat(),
            "amount": f"{amount / 100:.2f}", "direction": direction,
            "description": description, "reference": reference or "",
            "gl_account": "1010", "source_journal": journal,
            "_amount_cents": amount, "_date": date,
        }
        self.ds.ledger.append(row)
        return row

    def add_carry_in(self, item_type, date, amount, direction, description, reference):
        row = {
            "item_id": self.next_carry_id(), "item_type": item_type,
            "original_date": date.isoformat(), "amount": f"{amount / 100:.2f}",
            "direction": direction, "description": description, "reference": reference or "",
            "_amount_cents": amount,
        }
        self.ds.carry_in.append(row)
        return row

    def label(self, item_type, item_id, group, scenario, exception, risk, disposition, note):
        self.ds.truth.append({
            "item_type": item_type, "item_id": item_id, "true_match_group": group or "",
            "scenario_code": scenario, "exception_code": exception or "",
            "expected_risk": risk, "expected_disposition": disposition, "note": note,
        })

    # -- scenarios -----------------------------------------------------------
    def build(self):
        p = self.p
        self.high_value_budget = p.high_value_items
        self.build_carry_in()
        self.build_exact(p.exact_pairs)
        self.build_timing(p.timing_pairs)
        self.build_name_variation(p.name_variation_pairs)
        self.build_one_to_many(p.one_to_many_groups)
        self.build_many_to_one(p.many_to_one_groups)
        self.build_bank_originated()
        self.build_duplicates()
        self.build_unexplained()
        self.build_outstanding_checks(p.outstanding_checks)
        self.build_deposits_in_transit(p.deposits_in_transit)
        self.build_validation_rows()
        return self.ds

    def high_value(self) -> bool:
        """Spend the high-value budget evenly across the run."""
        if self.high_value_budget > 0 and self.rnd.random() < 0.035:
            self.high_value_budget -= 1
            return True
        return False

    def build_carry_in(self):
        """July items brought forward; some clear during the current period (BR-11)."""
        p = self.p
        stale_cutoff = p.period_start - dt.timedelta(days=50)
        items = []
        for i in range(p.carry_in_checks):
            vendor, _ = self.vendor()
            # four checks are deliberately stale, older than PRM-03 (45 days)
            date = (stale_cutoff - dt.timedelta(days=self.rnd.randint(1, 20))) if i < 4 \
                else (p.period_start - dt.timedelta(days=self.rnd.randint(3, 25)))
            amount = self.amount(180, 7_400)
            ref = self.next_check()
            row = self.add_carry_in("outstanding_check", date, amount, "credit",
                                    f"Check {ref} — {vendor}", ref)
            items.append(("outstanding_check", row, vendor, ref, amount, date))
        for _ in range(p.carry_in_deposits):
            customer, _ = self.customer()
            date = p.period_start - dt.timedelta(days=self.rnd.randint(1, 4))
            amount = self.amount(900, 18_000)
            ref = f"DEP{self.rnd.randint(10000, 99999)}"
            row = self.add_carry_in("deposit_in_transit", date, amount, "debit",
                                    f"Deposit {ref} — {customer}", ref)
            items.append(("deposit_in_transit", row, customer, ref, amount, date))

        self.rnd.shuffle(items)
        cleared = items[:p.carry_in_cleared]
        remaining = items[p.carry_in_cleared:]

        for kind, row, party, ref, amount, _ in cleared:
            group = self.next_group("SCN-01")
            clear_day = self.day(0, 8)
            if kind == "outstanding_check":
                bank = self.add_bank(clear_day, amount, "debit",
                                     f"CHECK PAID {ref}", ref, "CHK")
                note = "Prior-period check clearing this period"
            else:
                bank = self.add_bank(clear_day, amount, "credit",
                                     f"{self.rnd.choice(BANK_PREFIX_IN)} {party.upper()} {ref}", ref, "DEP")
                note = "Prior-period deposit clearing this period"
            risk = "high" if amount >= 1_000_000 else "low"
            self.label("bank", bank["txn_id"], group, "SCN-01", None, risk, "match", note)
            self.label("carry_in", row["item_id"], group, "SCN-01", None, risk, "match", note)

        for kind, row, *_ in remaining:
            code = "EXC-01" if kind == "outstanding_check" else "EXC-02"
            original = dt.date.fromisoformat(row["original_date"])
            stale = (self.p.period_end - original).days > 45
            self.label("carry_in", row["item_id"], "", "SCN-02", code,
                       "medium" if stale else "low", "carry_forward",
                       "Stale prior-period item" if stale else "Prior-period item still outstanding")

    def build_exact(self, n):
        """Exact pairs, including deliberate collisions that make matching non-trivial.

        Repeated amounts on the same day test that the uniqueness rule (BR-02) prevents a
        false exact match; near-miss amounts within a dollar produce genuinely competing
        candidates for the scoring layer.
        """
        collision_at = {i for i in range(40, 40 + 11 * 12, 12)}     # 11 repeated amount and date pairs
        near_miss_at = {i for i in range(55, 55 + 14 * 9, 9)}       # 14 amounts within $1.00 of the previous pair
        previous = None
        for i in range(n):
            group = self.next_group("SCN-01")
            day = self.day()
            inbound = self.rnd.random() < 0.52
            party, abbrevs = self.customer() if inbound else self.vendor()
            amount = self.amount(9_800, 24_000) if self.high_value() else self.amount(60, 8_600)
            note = "Amount, reference and date agree"
            if previous and i in collision_at:
                amount, day = previous
                note = "Same amount and date as another pair; only the reference separates them"
            elif previous and i in near_miss_at:
                amount = previous[0] + self.rnd.choice([-95, -60, -35, 35, 60, 95])
                day = previous[1]
                note = "Amount within $1.00 of another pair on the same day"
            if inbound:
                ref = f"INV{self.rnd.randint(8000, 8999)}"
                bank = self.add_bank(day, amount, "credit", f"ACH DEP {abbrevs[0]} {ref}", ref, "ACH-CR")
                led = self.add_ledger(day, amount, "debit", f"{party} — invoice {ref[3:]}", ref, "CR")
            else:
                ref = self.next_check()
                bank = self.add_bank(day, amount, "debit", f"CHECK PAID {ref}", ref, "CHK")
                led = self.add_ledger(day, amount, "credit", f"{party} — check {ref}", ref, "CD")
            risk = "high" if amount >= 1_000_000 else "low"
            self.label("bank", bank["txn_id"], group, "SCN-01", None, risk, "match", note)
            self.label("ledger", led["entry_id"], group, "SCN-01", None, risk, "match", note)
            previous = (amount, day)

    def build_timing(self, n):
        for _ in range(n):
            group = self.next_group("SCN-02")
            led_day = self.day(0, len(self.days) - 4)
            bank_day = shift_business_days(self.days, led_day, self.rnd.randint(1, 3))
            inbound = self.rnd.random() < 0.5
            party, abbrevs = self.customer() if inbound else self.vendor()
            amount = self.amount(9_800, 26_000) if self.high_value() else self.amount(85, 7_900)
            if inbound:
                ref = f"INV{self.rnd.randint(8000, 8999)}"
                bank = self.add_bank(bank_day, amount, "credit",
                                     f"{self.rnd.choice(BANK_PREFIX_IN)} {abbrevs[0]} {ref}", ref, "ACH-CR")
                led = self.add_ledger(led_day, amount, "debit", f"{party} — invoice {ref[3:]}", ref, "CR")
            else:
                ref = self.next_check()
                bank = self.add_bank(bank_day, amount, "debit", f"CHECK PAID {ref}", ref, "CHK")
                led = self.add_ledger(led_day, amount, "credit", f"{party} — check {ref}", ref, "CD")
            risk = "high" if amount >= 1_000_000 else "low"
            note = f"Posted {(bank_day - led_day).days} calendar days after the ledger entry"
            self.label("bank", bank["txn_id"], group, "SCN-02", None, risk, "match", note)
            self.label("ledger", led["entry_id"], group, "SCN-02", None, risk, "match", note)

    def build_name_variation(self, n):
        """Bank description is abbreviated and the reference is missing, so only text similarity links them."""
        pool = CUSTOMERS + VENDORS
        for i in range(n):
            group = self.next_group("SCN-03")
            day = self.day()
            party, abbrevs = NEW_COUNTERPARTIES[i % len(NEW_COUNTERPARTIES)] if i < 9 else self.rnd.choice(pool)
            amount = self.amount(9_800, 21_000) if self.high_value() else self.amount(120, 6_400)
            abbrev = abbrevs[min(1, len(abbrevs) - 1)]
            bank = self.add_bank(day, amount, "credit",
                                 f"{self.rnd.choice(BANK_PREFIX_IN)} {abbrev}", None, "ACH-CR")
            led = self.add_ledger(day, amount, "debit", f"{party} — remittance", None, "CR")
            risk = "medium" if i < 9 else ("high" if amount >= 1_000_000 else "low")
            note = "New counterparty, abbreviated bank description" if i < 9 else "Abbreviated bank description, no reference"
            self.label("bank", bank["txn_id"], group, "SCN-03", None, risk, "match", note)
            self.label("ledger", led["entry_id"], group, "SCN-03", None, risk, "match", note)

    def build_one_to_many(self, n):
        """One bank deposit equals several ledger receipts (group size within DD-07 caps)."""
        for i in range(n):
            group = self.next_group("SCN-04")
            day = self.day(0, len(self.days) - 2)
            members = 4 if i % 2 == 0 else 3      # half the groups sit at the DD-07 cap
            parts = [self.amount(320, 3_100) for _ in range(members)]
            total = sum(parts)
            bank = self.add_bank(day, total, "credit",
                                 f"REMOTE DEP BATCH {self.rnd.randint(200, 899)}", None, "DEP")
            risk = "high" if total >= 1_000_000 else "low"
            self.label("bank", bank["txn_id"], group, "SCN-04", None, risk, "match",
                       f"Single deposit equal to {members} ledger receipts")
            for amount in parts:
                customer, _ = self.customer()
                ref = f"INV{self.rnd.randint(8000, 8999)}"
                led = self.add_ledger(shift_business_days(self.days, day, -self.rnd.randint(0, 1)),
                                      amount, "debit", f"{customer} — invoice {ref[3:]}", ref, "CR")
                self.label("ledger", led["entry_id"], group, "SCN-04", None, risk, "match",
                           "Member of a grouped deposit")

    def build_many_to_one(self, n):
        """Several bank debits equal one ledger entry, for example a payroll or split vendor payment."""
        for _ in range(n):
            group = self.next_group("SCN-05")
            day = self.day(0, len(self.days) - 2)
            members = 3
            parts = [self.amount(640, 4_200) for _ in range(members)]
            total = sum(parts)
            vendor, abbrevs = self.vendor()
            risk = "high" if total >= 1_000_000 else "low"
            led = self.add_ledger(day, total, "credit", f"{vendor} — settlement", None, "CD")
            self.label("ledger", led["entry_id"], group, "SCN-05", None, risk, "match",
                       f"Single ledger entry equal to {members} bank items")
            for amount in parts:
                bank = self.add_bank(shift_business_days(self.days, day, self.rnd.randint(0, 1)),
                                     amount, "debit", f"ACH DEBIT {abbrevs[0]}", None, "ACH-DR")
                self.label("bank", bank["txn_id"], group, "SCN-05", None, risk, "match",
                           "Member of a split payment")

    def build_bank_originated(self):
        p = self.p
        for i in range(p.bank_fees):
            day = self.day(len(self.days) - 6)
            amount = self.amount(12, 145)
            bank = self.add_bank(day, amount, "debit",
                                 self.rnd.choice(["MONTHLY ANALYSIS FEE", "WIRE FEE", "ACH ORIGINATION FEE",
                                                  "RETURNED ITEM FEE", "ACCOUNT MAINT FEE"]), None, "FEE")
            self.label("bank", bank["txn_id"], "", "SCN-06", "EXC-03", "low", "adjust",
                       "Bank fee with no ledger entry")
        for _ in range(p.bank_interest):
            bank = self.add_bank(self.days[-1], self.amount(18, 320), "credit",
                                 "INTEREST CREDIT", None, "INT")
            self.label("bank", bank["txn_id"], "", "SCN-06", "EXC-04", "low", "adjust",
                       "Interest credited by the bank")
        for _ in range(p.bank_returns):
            customer, abbrevs = self.customer()
            bank = self.add_bank(self.day(4), self.amount(400, 2_900), "debit",
                                 f"RETURNED DEPOSIT {abbrevs[0]}", None, "RTN")
            self.label("bank", bank["txn_id"], "", "SCN-06", "EXC-06", "medium", "adjust",
                       "Returned deposit, ledger entry missing")

    def build_duplicates(self):
        """A duplicate is one real transaction that appears twice on one side.

        The genuine copy matches its counterpart on the other side; the extra copy has no
        counterpart and stays in the unresolved difference until it is investigated (BR-07).
        Both copies are flagged, because the system cannot tell which one is genuine.
        """
        p = self.p
        for _ in range(p.duplicate_bank_pairs):
            group = self.next_group("SCN-07")
            day = self.day(0, len(self.days) - 2)
            vendor, abbrevs = self.vendor()
            amount = self.amount(280, 1_450)
            led = self.add_ledger(day, amount, "credit", f"{vendor} — settlement", None, "CD")
            self.label("ledger", led["entry_id"], group, "SCN-07", "EXC-05", "high", "investigate",
                       "Ledger entry paid twice by the bank")
            for offset in (0, 1):
                bank = self.add_bank(shift_business_days(self.days, day, offset), amount, "debit",
                                     f"ACH DEBIT {abbrevs[0]}", None, "ACH-DR")
                self.label("bank", bank["txn_id"], group, "SCN-07", "EXC-05", "high", "investigate",
                           "Same amount, payee and near date as another bank item")
        for _ in range(p.duplicate_ledger_pairs):
            group = self.next_group("SCN-07")
            day = self.day(0, len(self.days) - 2)
            customer, abbrevs = self.customer()
            amount = self.amount(240, 1_300)
            ref = f"INV{self.rnd.randint(8000, 8999)}"
            bank = self.add_bank(day, amount, "credit",
                                 f"ACH DEP {abbrevs[0]} {ref}", ref, "ACH-CR")
            self.label("bank", bank["txn_id"], group, "SCN-07", "EXC-05", "high", "investigate",
                       "Single receipt recorded twice in the ledger")
            for offset in (0, 1):
                led = self.add_ledger(shift_business_days(self.days, day, offset), amount, "debit",
                                      f"{customer} — invoice {ref[3:]}", ref, "CR")
                self.label("ledger", led["entry_id"], group, "SCN-07", "EXC-05", "high", "investigate",
                           "Same amount, counterparty and near date as another ledger entry")

    def build_unexplained(self):
        p = self.p
        for i in range(p.unexplained_bank):
            text = UNEXPLAINED_BANK[i % len(UNEXPLAINED_BANK)]
            direction = "credit" if i % 2 == 0 else "debit"
            bank = self.add_bank(self.day(3), self.amount(240, 1_900), direction, text, None, "ACH-CR")
            self.label("bank", bank["txn_id"], "", "SCN-08", "EXC-07", "high", "unresolved",
                       "No credible ledger candidate")
        for i in range(p.unexplained_ledger):
            text = UNEXPLAINED_LEDGER[i % len(UNEXPLAINED_LEDGER)]
            direction = "debit" if i % 2 == 0 else "credit"
            led = self.add_ledger(self.day(3), self.amount(180, 2_100), direction, text, None, "GJ")
            self.label("ledger", led["entry_id"], "", "SCN-08", "EXC-07", "high", "unresolved",
                       "No credible bank candidate")

    def build_outstanding_checks(self, n):
        """Ledger payments issued too late in the period to clear the bank."""
        for _ in range(n):
            vendor, _ = self.vendor()
            day = self.day(len(self.days) - 6)
            amount = self.amount(9_800, 19_000) if self.high_value() else self.amount(150, 5_400)
            ref = self.next_check()
            led = self.add_ledger(day, amount, "credit", f"{vendor} — check {ref}", ref, "CD")
            self.label("ledger", led["entry_id"], "", "SCN-02", "EXC-01",
                       "high" if amount >= 1_000_000 else "low", "carry_forward",
                       "Check issued but not yet cleared at period end")

    def build_deposits_in_transit(self, n):
        for _ in range(n):
            customer, _ = self.customer()
            day = self.day(len(self.days) - 3)
            amount = self.amount(9_800, 22_000) if self.high_value() else self.amount(600, 9_200)
            ref = f"DEP{self.rnd.randint(10000, 99999)}"
            led = self.add_ledger(day, amount, "debit", f"{customer} — deposit {ref}", ref, "CR")
            self.label("ledger", led["entry_id"], "", "SCN-02", "EXC-02",
                       "high" if amount >= 1_000_000 else "low", "carry_forward",
                       "Deposit recorded but not yet on the statement at period end")

    def build_validation_rows(self):
        """Deliberately broken rows so validation has something to catch (FR-VAL-02, FR-VAL-05)."""
        p = self.p
        for _ in range(p.out_of_period_rows):
            day = p.period_start - dt.timedelta(days=1)
            vendor, abbrevs = self.vendor()
            bank = self.add_bank(day, self.amount(200, 1_800), "debit",
                                 f"ACH DEBIT {abbrevs[0]}", None, "ACH-DR")
            self.label("bank", bank["txn_id"], "", "SCN-08", "EXC-07", "low", "investigate",
                       "Dated outside the period; flagged by validation")
        for _ in range(p.invalid_rows):
            row = self.add_bank(self.day(), 100, "debit", "POS DEB TERMINAL 4471", None, "ACH-DR")
            row["amount"] = "N/A"          # deliberately unparseable (FR-VAL-02)
            row["_invalid"] = True
            self.label("bank", row["txn_id"], "", "SCN-08", "", "low", "investigate",
                       "Invalid amount; excluded by validation")


# ---------------------------------------------------------------------------
# Balances and output
# ---------------------------------------------------------------------------

def compute_bank_balances(ds: Dataset, opening_cents: int):
    """Order the statement by date, then by identifier, and fill the running balance."""
    ds.bank.sort(key=lambda r: (r["_date"], r["txn_id"]))
    balance = opening_cents
    debits = credits = 0
    for row in ds.bank:
        if row.get("_invalid"):
            row["running_balance"] = f"{balance / 100:.2f}"
            continue
        amount = row["_amount_cents"]
        if row["direction"] == "credit":
            balance += amount
            credits += amount
        else:
            balance -= amount
            debits += amount
        row["running_balance"] = f"{balance / 100:.2f}"
    return balance, debits, credits


def compute_ledger_totals(ds: Dataset):
    ds.ledger.sort(key=lambda r: (r["_date"], r["entry_id"]))
    debits = sum(r["_amount_cents"] for r in ds.ledger if r["direction"] == "debit")
    credits = sum(r["_amount_cents"] for r in ds.ledger if r["direction"] == "credit")
    return debits, credits


def write_csv(path: Path, rows: list, columns: list):
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def generate(profile: Profile, out_dir: Path) -> dict:
    builder = Builder(profile)
    ds = builder.build()

    bank_close, bank_debits, bank_credits = compute_bank_balances(ds, profile.opening_balance_cents)
    ledger_debits, ledger_credits = compute_ledger_totals(ds)

    truth = {(t["item_type"], t["item_id"]): t for t in ds.truth}

    def cents(item_type, predicate):
        rows = ds.ledger if item_type == "ledger" else ds.bank
        key = "entry_id" if item_type == "ledger" else "txn_id"
        total = 0
        for row in rows:
            label = truth.get((item_type, row[key]))
            if label and predicate(label):
                total += row["_amount_cents"] * (1 if row["direction"] in ("debit", "credit") else 1)
        return total

    def signed(item_type, predicate):
        """Signed effect on cash: bank credit and ledger debit increase cash."""
        rows = ds.ledger if item_type == "ledger" else ds.bank
        key = "entry_id" if item_type == "ledger" else "txn_id"
        positive = "debit" if item_type == "ledger" else "credit"
        total = 0
        for row in rows:
            label = truth.get((item_type, row[key]))
            if label and predicate(label) and not row.get("_invalid"):
                total += row["_amount_cents"] if row["direction"] == positive else -row["_amount_cents"]
        return total

    # Reconciling items at period end
    oc_ledger = sum(r["_amount_cents"] for r in ds.ledger
                    if truth.get(("ledger", r["entry_id"]), {}).get("exception_code") == "EXC-01")
    dit_ledger = sum(r["_amount_cents"] for r in ds.ledger
                     if truth.get(("ledger", r["entry_id"]), {}).get("exception_code") == "EXC-02")
    oc_carry = sum(r["_amount_cents"] for r in ds.carry_in
                   if truth.get(("carry_in", r["item_id"]), {}).get("exception_code") == "EXC-01")
    dit_carry = sum(r["_amount_cents"] for r in ds.carry_in
                    if truth.get(("carry_in", r["item_id"]), {}).get("exception_code") == "EXC-02")
    outstanding_checks = oc_ledger + oc_carry
    deposits_in_transit = dit_ledger + dit_carry

    bank_originated = signed("bank", lambda t: t["exception_code"] in ("EXC-03", "EXC-04", "EXC-06"))
    unmatched_codes = ("EXC-05", "EXC-07")
    unexplained_bank = signed("bank", lambda t: t["exception_code"] in unmatched_codes)
    unexplained_ledger = signed("ledger", lambda t: t["exception_code"] in unmatched_codes)

    # Ledger opening balance is the bank opening adjusted for prior-period items,
    # which is what makes the closing statement tie (DOC-04 section 4.4).
    all_carry_oc = sum(r["_amount_cents"] for r in ds.carry_in if r["item_type"] == "outstanding_check")
    all_carry_dit = sum(r["_amount_cents"] for r in ds.carry_in if r["item_type"] == "deposit_in_transit")
    ledger_opening = profile.opening_balance_cents + all_carry_dit - all_carry_oc
    ledger_close = ledger_opening + ledger_debits - ledger_credits

    adjusted_bank = bank_close + deposits_in_transit - outstanding_checks
    adjusted_book = ledger_close + bank_originated
    unresolved = adjusted_bank - adjusted_book
    expected_unresolved = unexplained_bank - unexplained_ledger

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "bank_statement.csv", ds.bank,
              ["txn_id", "txn_date", "amount", "direction", "description", "reference",
               "bank_type_code", "running_balance"])
    write_csv(out_dir / "gl_cash_detail.csv", ds.ledger,
              ["entry_id", "posting_date", "amount", "direction", "description", "reference",
               "gl_account", "source_journal"])
    write_csv(out_dir / "carry_in_items.csv", ds.carry_in,
              ["item_id", "item_type", "original_date", "amount", "direction", "description", "reference"])
    write_csv(out_dir / "ground_truth.csv", ds.truth,
              ["item_type", "item_id", "true_match_group", "scenario_code", "exception_code",
               "expected_risk", "expected_disposition", "note"])

    control = [
        {"file_type": "bank", "row_count": len(ds.bank),
         "total_debits": f"{bank_debits / 100:.2f}", "total_credits": f"{bank_credits / 100:.2f}",
         "opening_balance": f"{profile.opening_balance_cents / 100:.2f}",
         "closing_balance": f"{bank_close / 100:.2f}"},
        {"file_type": "gl", "row_count": len(ds.ledger),
         "total_debits": f"{ledger_debits / 100:.2f}", "total_credits": f"{ledger_credits / 100:.2f}",
         "opening_balance": f"{ledger_opening / 100:.2f}",
         "closing_balance": f"{ledger_close / 100:.2f}"},
        {"file_type": "carry_in", "row_count": len(ds.carry_in),
         "total_debits": f"{sum(r['_amount_cents'] for r in ds.carry_in if r['direction'] == 'debit') / 100:.2f}",
         "total_credits": f"{sum(r['_amount_cents'] for r in ds.carry_in if r['direction'] == 'credit') / 100:.2f}",
         "opening_balance": "", "closing_balance": ""},
    ]
    write_csv(out_dir / "run_control.csv", control,
              ["file_type", "row_count", "total_debits", "total_credits", "opening_balance", "closing_balance"])

    return {
        "bank_rows": len(ds.bank), "ledger_rows": len(ds.ledger), "carry_in_rows": len(ds.carry_in),
        "truth_rows": len(ds.truth),
        "bank_close": bank_close, "ledger_close": ledger_close,
        "outstanding_checks": outstanding_checks, "deposits_in_transit": deposits_in_transit,
        "bank_originated": bank_originated,
        "adjusted_bank": adjusted_bank, "adjusted_book": adjusted_book,
        "unresolved": unresolved, "expected_unresolved": expected_unresolved,
        "high_value": sum(1 for t in ds.truth if t["expected_risk"] == "high"),
        "scenarios": {code: sum(1 for t in ds.truth if t["scenario_code"] == code)
                      for code in [f"SCN-0{i}" for i in range(1, 9)]},
    }


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def main():
    parser = argparse.ArgumentParser(description="Generate the synthetic reconciliation dataset (DOC-04).")
    parser.add_argument("--profile", choices=["evaluation", "calibration"], default="evaluation")
    parser.add_argument("--seed", type=int, help="override the profile seed")
    parser.add_argument("--out", help="override the output directory")
    args = parser.parse_args()

    profile = EVALUATION if args.profile == "evaluation" else CALIBRATION
    if args.seed is not None:
        profile = Profile(**{**profile.__dict__, "seed": args.seed})
    out_dir = Path(args.out or profile.out_dir)

    summary = generate(profile, out_dir)

    print(f"Dataset: {profile.name}  seed {profile.seed}  "
          f"{profile.period_start} to {profile.period_end}")
    print(f"Output:  {out_dir}/")
    print()
    print(f"  bank_statement.csv   {summary['bank_rows']:>5} rows")
    print(f"  gl_cash_detail.csv   {summary['ledger_rows']:>5} rows")
    print(f"  carry_in_items.csv   {summary['carry_in_rows']:>5} rows")
    print(f"  ground_truth.csv     {summary['truth_rows']:>5} labels")
    print(f"  total transactions   {summary['bank_rows'] + summary['ledger_rows']:>5}")
    print()
    print("Scenario coverage")
    for code, count in summary["scenarios"].items():
        print(f"  {code}  {count:>4} labelled items")
    print()
    print("Reconciliation statement")
    print(f"  bank closing balance      {money(summary['bank_close']):>16}")
    print(f"  add deposits in transit   {money(summary['deposits_in_transit']):>16}")
    print(f"  less outstanding checks   {money(-summary['outstanding_checks']):>16}")
    print(f"  adjusted bank balance     {money(summary['adjusted_bank']):>16}")
    print(f"  book closing balance      {money(summary['ledger_close']):>16}")
    print(f"  bank-originated items     {money(summary['bank_originated']):>16}")
    print(f"  adjusted book balance     {money(summary['adjusted_book']):>16}")
    print(f"  unresolved difference     {money(summary['unresolved']):>16}")
    print()

    errors = []
    if summary["unresolved"] != summary["expected_unresolved"]:
        errors.append(f"statement does not tie: {summary['unresolved']} != {summary['expected_unresolved']}")
    if any(count == 0 for count in summary["scenarios"].values()):
        errors.append("a scenario has no labelled items")
    if summary["high_value"] == 0:
        errors.append("no high-risk items generated")
    if errors:
        for message in errors:
            print(f"CHECK FAILED: {message}")
        raise SystemExit(1)
    print("Checks passed: statement ties to the unexplained items, every scenario is represented.")


if __name__ == "__main__":
    main()
