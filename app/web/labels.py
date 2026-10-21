"""Plain-language vocabulary for the reviewer interface.

Every label and message a reviewer reads is defined here, once, in words an accountant
uses. The codes behind them (CAT-03, EXC-02, CR-10, a model score) are still recorded in
the audit trail and the reports, and the interface shows them under "Show details", so
nothing is hidden; they are simply not the first thing a reviewer has to decode.

Keeping the words in one module means a term is the same on every screen, and the
User Guide and the Help page can quote it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Queues (BR-13 categories)
# ---------------------------------------------------------------------------

QUEUE_NAMES = {
    "CAT-01": "Exact matches",
    "CAT-02": "Likely matches",
    "CAT-03": "Matches to check closely",
    "CAT-04": "Unmatched items",
    "CAT-05": "Senior review",
}

QUEUE_HELP = {
    "CAT-01": "Bank and book records that agree exactly. Usually quick to confirm, and can be approved as a batch.",
    "CAT-02": "Pairs the system is fairly sure about, such as the same amount a day or two apart.",
    "CAT-03": "Pairs with something that doesn't fit, such as a different name. Read the evidence before deciding.",
    "CAT-04": "Records with no partner: uncleared checks, deposits not yet at the bank, bank fees and the like.",
    "CAT-05": "Large amounts, possible duplicates, unexplained items, and anything another reviewer escalated.",
}

# ---------------------------------------------------------------------------
# Kinds of unmatched item (EXC-01..08)
# ---------------------------------------------------------------------------

EXCEPTION_NAMES = {
    "EXC-01": "Check not yet cleared",
    "EXC-02": "Deposit not yet at the bank",
    "EXC-03": "Bank fee not yet recorded",
    "EXC-04": "Interest not yet recorded",
    "EXC-05": "Possible duplicate",
    "EXC-06": "Bank item missing from the books",
    "EXC-07": "Unexplained item",
    "EXC-08": "Search not finished; pair by hand",
}

EXCEPTION_HELP = {
    "EXC-01": "A check recorded in the books that the bank has not paid yet. It reconciles next month when it clears.",
    "EXC-02": "A deposit recorded in the books that the bank has not credited yet. It reconciles when it arrives.",
    "EXC-03": "A charge the bank took that the books do not show yet. Propose an adjustment to record it.",
    "EXC-04": "Interest the bank paid that the books do not show yet. Propose an adjustment to record it.",
    "EXC-05": "The same amount appears twice. Find out which copy is genuine before doing anything else.",
    "EXC-06": "Money moved at the bank with nothing in the books. Find the source and propose an adjustment.",
    "EXC-07": "No explanation found. Leave it open for investigation; it stays on the reconciliation statement.",
    "EXC-08": "Too many possible combinations to check automatically. Pick the matching records by hand.",
}

# ---------------------------------------------------------------------------
# Statuses, decisions, risk, confidence
# ---------------------------------------------------------------------------

ITEM_STATUS = {
    "imported": "Imported",
    "validated": "Checked",
    "proposed": "Waiting for review",
    "escalated": "With a senior reviewer",
    "approved": "Approved",
    "report_verified": "Confirmed in the reports",
    "reconciled": "Reconciled",
    "unresolved": "Left open",
    "excluded": "Excluded (failed checks)",
}

RUN_STATUS = {
    "created": "Waiting for files",
    "validating": "Files imported, not yet checked",
    "validation_failed": "Files failed the checks",
    "processing": "Matching in progress",
    "in_review": "In review",
    "ready_to_close": "Ready to close",
    "closed": "Closed",
    "reopen_requested": "Reopening requested",
}

RECOMMENDATION_STATUS = {"open": "Waiting for a decision", "decided": "Decided", "superseded": "Replaced"}

DECISION_NAMES = {
    "approve": "Approved",
    "modify": "Approved a different match",
    "reject": "Not a match",
    "escalate": "Sent to a senior reviewer",
    "unresolved": "Left open",
}

DECISION_BUTTONS = {
    "approve": "Approve",
    "modify": "Use the match I selected",
    "reject": "Not a match",
    "escalate": "Ask a senior reviewer",
    "unresolved": "Leave open for investigation",
}

DECISION_HELP = {
    "approve": "You agree with the suggested match or classification.",
    "modify": "A different suggestion is the right one. Select it first, in the list on the left.",
    "reject": "These records don't belong together. They go back to the unmatched list.",
    "escalate": "You're not sure, or the item needs more authority. A senior reviewer will decide.",
    "unresolved": "You can't explain it yet. It stays open on the reconciliation statement.",
}

RISK_NAMES = {"high": "High risk", "medium": "Medium risk", "low": "Low risk"}

CONFIDENCE_TEXT = {
    "High": "The system is fairly sure.",
    "Medium": "The system has some doubt.",
    "Low": "The system is unsure. Check the evidence carefully.",
    "Uncalibrated": "Confidence not calibrated.",
}

RISK_RULE_TEXT = {
    "RR-01": "Large amount",
    "RR-02": "Possible duplicate",
    "RR-03": "Unexplained item",
    "RR-04": "New customer or supplier this month",
    "RR-05": "Several records combined",
    "RR-06": "Older than expected",
    "RR-07": "Nothing unusual",
}

ADJUSTMENT_STATUS = {"proposed": "Waiting for approval", "approved": "Approved", "rejected": "Rejected"}

ROLE_DESCRIPTIONS = {
    "ROL-01": "Imports files, reviews items, proposes adjustments.",
    "ROL-02": "Everything a Staff Accountant does, plus high-risk items and approving adjustments.",
    "ROL-03": "Approves adjustments, signs off and closes the period, decides reopening.",
}

# ---------------------------------------------------------------------------
# Files, checks, accounts statement, reports and the audit trail
# ---------------------------------------------------------------------------

FILE_NAMES = {
    "bank": "Bank statement",
    "gl": "Company books (cash account)",
    "ledger": "Company books (cash account)",
    "carry_in": "Open items from last month",
    "control": "Control totals",
}

CHECK_NAMES = {
    "VAL-01": "The file has the expected columns",
    "VAL-02": "Every row can be read",
    "VAL-03": "Totals agree with the control file",
    "VAL-04": "No record number is used twice",
    "VAL-05": "Possible duplicate transactions",
    "VAL-06": "Dates fall within the month",
    "VAL-07": "The bank balance carries on without gaps",
    "VAL-08": "Book entries are on the cash account",
}

STATEMENT_SECTIONS = {
    "deposits_in_transit": ("Deposits not yet at the bank",
                            "Recorded in the books, not yet credited by the bank. Added to the bank balance."),
    "outstanding_checks": ("Checks not yet cleared",
                           "Written and recorded in the books, not yet paid by the bank. Taken off the bank balance."),
    "bank_originated": ("Bank items not yet in the books",
                        "Fees, interest and other bank entries the books don't show yet. Adjust the book balance."),
    "unresolved": ("Items left open",
                   "Not explained yet. They carry forward to next month and stay on this statement."),
}

REPORT_DESCRIPTIONS = {
    "RPT-01": "Which files were loaded, with their row counts and fingerprints.",
    "RPT-02": "Every check on the files, including rows set aside and warnings.",
    "RPT-03": "How descriptions and references were tidied for comparison, next to the originals.",
    "RPT-04": "Pairs matched by fixed rules, and who approved them.",
    "RPT-05": "Every suggestion the system scored, with its evidence and how sure it was.",
    "RPT-06": "Records with no partner, and what was decided about each.",
    "RPT-07": "Correcting entries proposed, and who approved or rejected them. Nothing is posted.",
    "RPT-08": "Every decision: what, who and when.",
    "RPT-09": "The bank-to-book statement and the difference left to explain.",
    "RPT-10": "Where a reviewer chose differently from the system's suggestion.",
    "RPT-11": "Every final match, however it was made.",
    "RPT-12": "How the system's suggestions compared with reviewers' decisions.",
    "RPT-13": "Every recorded action in order, with the tamper check.",
}

EVENT_NAMES = {
    "GENESIS": "Audit trail started",
    "IMPORT": "File imported",
    "VALIDATION": "Files checked",
    "NORMALIZATION": "Data tidied for comparison",
    "RULE": "Fixed rule applied",
    "AI_RECOMMENDATION": "Suggestion scored",
    "ROUTING": "Sent to a review list",
    "DETAIL_OPENED": "Item opened",
    "DECISION": "Decision recorded",
    "BLOCKED_ATTEMPT": "Action refused",
    "ADJUSTMENT_PROPOSED": "Adjustment proposed",
    "ADJUSTMENT_DECIDED": "Adjustment decided",
    "REPORT_GENERATED": "Report created",
    "REPORT_EXPORTED": "Report downloaded",
    "REPORT_VERIFIED": "Confirmed in the reports",
    "CHAIN_VERIFIED": "Audit trail checked",
    "SIGNOFF": "Month signed off",
    "PERIOD_LOCKED": "Month locked",
    "REOPEN_REQUESTED": "Reopening requested",
    "REOPEN_DECIDED": "Reopening decided",
    "GENAI_PROSE": "AI summary written",
    "DECISION_REVERSED": "Decision reversed",
    "IDENTITY_SELECTED": "Person selected",
    "STAGE_TIMING": "Step timed",
    "STATUS_CHANGED": "Status changed",
}


def event_name(event_type: str | None) -> str:
    return EVENT_NAMES.get(event_type or "", (event_type or "").replace("_", " ").capitalize())


def any_status(status: str | None) -> str:
    """A status from any table (item, month, suggestion or adjustment) in plain words."""
    for names in (ITEM_STATUS, RUN_STATUS, RECOMMENDATION_STATUS, ADJUSTMENT_STATUS):
        if status in names:
            return names[status]
    return (status or "").replace("_", " ")


# ---------------------------------------------------------------------------
# Close conditions (CR-10), in the order the period service evaluates them
# ---------------------------------------------------------------------------

CLOSE_CONDITIONS = {
    1: "Every item has a decision",
    2: "No item is waiting for a senior reviewer",
    3: "Every adjustment is approved or rejected",
    4: "The audit trail shows no tampering",
    5: "The reports are up to date",
    6: "The remaining difference is explained",
}

# ---------------------------------------------------------------------------
# Why an action is blocked
# ---------------------------------------------------------------------------

RULE_TEXT = {
    "CR-02": "This needs a Senior Accountant or the Controller.",
    "CR-03": "You reviewed or escalated this item, so someone else must make the final decision.",
    "CR-04": "You prepared this adjustment, so someone else must approve it.",
    "CR-05": "You were the only reviewer this month, so someone else must sign off.",
    "CR-06": "You asked for the reopening, so someone else must decide it.",
    "CR-07": "This item hasn't met every condition to count as reconciled yet.",
    "CR-10": "The period can't be closed yet.",
    "CR-11": "A batch can only hold exact matches with low risk.",
    "CR-16": "Please add a comment explaining your decision.",
    "CR-17": "The files failed their checks, so matching can't start. Correct the files and start a new run.",
    "CR-18": "This period is closed. It must be reopened before anything can change.",
    "CR-19": "You made this decision, so someone else must reverse it.",
    "FR-REV-04": "That action isn't available for this kind of item.",
    "FR-REV-10": "Open the item and read its evidence before approving it.",
    "FR-ADJ-01": "Adjustments are only for bank fees, interest, and bank items missing from the books.",
}

_ROLE_MESSAGE = re.compile(r"^(?P<role>.+?) cannot (?P<action>.+); permitted for (?P<allowed>.+)$")


@dataclass(frozen=True)
class Refusal:
    """A blocked action, explained. `text` is for the reviewer; `rule` and `detail` for auditors."""

    text: str
    rule: str = ""
    detail: str = ""

    def __str__(self) -> str:
        return self.text


def refusal(rule: str, message: str) -> Refusal:
    """Turn a control refusal into a sentence a reviewer can act on."""
    if rule == "ROLE":
        match = _ROLE_MESSAGE.match(message)
        if match:
            allowed = _join(match["allowed"].split(", "))
            return Refusal(f"Only {_with_article(allowed)} can {match['action']}.", rule, message)
        if "deactivated" in message:
            return Refusal("This person's access has been switched off.", rule, message)
    if rule in RULE_TEXT:
        return Refusal(RULE_TEXT[rule], rule, message)
    return Refusal(_sentence(message), rule, message)


def no_identity() -> Refusal:
    return Refusal("Choose who you are from \"Acting as\" at the top right.")


# ---------------------------------------------------------------------------
# Formatting helpers used as template filters
# ---------------------------------------------------------------------------

def queue_name(code: str | None) -> str:
    return QUEUE_NAMES.get(code or "", code or "")


def exception_name(code: str | None) -> str:
    return EXCEPTION_NAMES.get(code or "", code or "")


def item_status(status: str | None) -> str:
    return ITEM_STATUS.get(status or "", (status or "").replace("_", " "))


def run_status(status: str | None) -> str:
    return RUN_STATUS.get(status or "", (status or "").replace("_", " "))


def decision_name(decision: str | None) -> str:
    return DECISION_NAMES.get(decision or "", decision or "")


def risk_name(level: str | None) -> str:
    return RISK_NAMES.get(level or "", level or "")


def percent_text(value: float | None) -> str:
    return "" if value is None else f"{round(value * 100)}%"


def _sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] + ("" if text.endswith((".", "?", "!")) else ".")


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _with_article(names: str) -> str:
    """'Controller' -> 'the Controller'; 'Senior Accountant or Controller' -> 'a Senior Accountant or the Controller'."""
    return " or ".join(("the " if part == "Controller" else "a ") + part for part in names.split(" or "))


FILTERS = {
    "queue_name": queue_name, "exception_name": exception_name, "item_status": item_status,
    "run_status": run_status, "decision_name": decision_name, "risk_name": risk_name,
    "percent_text": percent_text, "event_name": event_name, "any_status": any_status,
}
