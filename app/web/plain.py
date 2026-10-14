"""Turn the system's evidence and state into sentences a reviewer can read.

`labels.py` holds fixed words; this module builds sentences from data: what a suggested
match rests on, which way money moved, what the person using the screen should do next,
and which close steps are still open. The underlying values are always kept for the
"Show details" view, so nothing here replaces evidence; it only explains it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.control import permissions
from app.domain.statement import money
from app.web import labels

# ---------------------------------------------------------------------------
# Money and dates
# ---------------------------------------------------------------------------

def money_in(item_type: str, direction: str) -> bool:
    """Cash coming in: a bank credit, or a debit to the cash account in the books."""
    return (direction == "credit") if item_type == "bank" else (direction == "debit")


def flow(item_type: str, direction: str) -> str:
    return "in" if money_in(item_type, direction) else "out"


def source_name(item_type: str) -> str:
    return {"bank": "Bank statement", "ledger": "Company books", "carry_in": "Last month's open item"}[item_type]


def kind_of_record(item_type: str, direction: str) -> str:
    """'Bank deposit', 'Book payment' and so on: what the record is, in a word or two."""
    incoming = money_in(item_type, direction)
    return {
        "bank": "Bank deposit" if incoming else "Bank payment",
        "ledger": "Receipt in the books" if incoming else "Payment in the books",
        "carry_in": "Open deposit from last month" if incoming else "Open payment from last month",
    }[item_type]


def long_date(value: str | None) -> str:
    if not value:
        return ""
    day = date.fromisoformat(value)
    return f"{day.day} {day.strftime('%b %Y')}"


def month_name(period_start: str) -> str:
    return date.fromisoformat(period_start).strftime("%B %Y")


# ---------------------------------------------------------------------------
# What a suggested match rests on
# ---------------------------------------------------------------------------

def match_facts(features: dict[str, Any]) -> list[tuple[str, bool]]:
    """The model's features as short statements, each marked as fitting (True) or not."""
    facts: list[tuple[str, bool]] = []
    difference = features.get("amount_difference_cents")
    if difference is not None:
        facts.append(("Amounts agree exactly", True) if difference == 0
                     else (f"Amounts differ by {money(abs(int(difference)))}", False))
    gap = features.get("business_day_gap")
    if gap is not None:
        gap = abs(int(gap))
        facts.append(("Same business day", True) if gap == 0
                     else (f"{gap} business day{'s' if gap != 1 else ''} apart", gap <= 3))
    similarity = features.get("text_similarity")
    if similarity is not None:
        percent = round(float(similarity) * 100)
        if similarity >= 0.8:
            facts.append((f"Names match ({percent}% alike)", True))
        elif similarity >= 0.5:
            facts.append((f"Names are similar ({percent}% alike)", True))
        else:
            facts.append((f"Names don't look alike ({percent}% alike)", False))
    state = features.get("reference_state")
    if state == "equal":
        facts.append(("Reference numbers agree", True))
    elif state == "partial":
        facts.append(("Reference numbers partly agree", True))
    elif state == "different":
        facts.append(("Reference numbers differ", False))
    return facts


_REWRITES = [
    (re.compile(r"counterparty text similarity is only ([0-9.]+), so the names do not support this pairing"),
     lambda m: f"The names don't look alike ({round(float(m[1]) * 100)}% similar)"),
    (re.compile(r"the next candidate \(([^)]*)\) scores [0-9.]+, only [0-9.]+ behind, so the two are hard to separate"),
     lambda m: f"Another possible match ({m[1]}) is almost as likely"),
    (re.compile(r"text similarity ([0-9.]+)"), lambda m: f"names {round(float(m[1]) * 100)}% alike"),
    (re.compile(r"^(\d+) entries sum exactly to"), lambda m: f"{m[1]} book entries add up exactly to"),
    (re.compile(r"^posted (\d+) business days? apart"), lambda m: f"Recorded {m[1]} business day{'s' if m[1] != '1' else ''} apart"),
    (re.compile(r"^same business date"), lambda m: "Same business day"),
]


def evidence_sentence(text: str) -> str:
    """Reword one line of system evidence into plain English, keeping every figure."""
    sentence = text.strip()
    for pattern, replacement in _REWRITES:
        sentence = pattern.sub(replacement, sentence)
    return sentence[:1].upper() + sentence[1:]


_CENTS = re.compile(r"\b(\d+) cents\b")
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_RULE_REF = re.compile(r"\s*\((?:BR|CR|RR)-\d+(?:, (?:BR|CR|RR)-\d+)*\)")
_EXACT = re.compile(r"Amount (\d+) cents, reference (\S+) and date (\S+) agree, and no other item on either side shares them")


_RULE_WORDS = [
    (re.compile(r"prior-period outstanding check CI-\d+"), "last month's uncleared check"),
    (re.compile(r"prior-period deposit in transit CI-\d+"), "last month's deposit in transit"),
    (re.compile(r"The candidate search was limited: no group of (\d+) or fewer members sums to this amount; "
                r"a larger group may exist"),
     r"No combination of up to \1 records on the other side adds up to this amount; a larger one may exist"),
    (re.compile(r"no ledger entry exists for it"), "there is nothing for it in the books"),
    (re.compile(r"has no ledger entry, but the counterparty appears elsewhere in the period"),
     "has nothing in the books, but the same name appears elsewhere this month"),
    (re.compile(r"Another item on the same side has the same amount, direction and description"),
     "Another record from the same source has the same amount and description"),
    (re.compile(r"identified by transaction code"), "identified by the bank's transaction code"),
]


def readable(text: str) -> str:
    """Money in dollars, dates as '3 Aug 2026', rule wording in plain words, and rule
    numbers removed (they stay in "Show details")."""
    for pattern, replacement in _RULE_WORDS:
        text = pattern.sub(replacement, text)
    text = _CENTS.sub(lambda m: money(int(m[1])), text)
    text = _ISO_DATE.sub(lambda m: long_date(m[1]), text)
    return _RULE_REF.sub("", text).strip()


def rule_evidence(explanation: str | None) -> list[str]:
    """What a deterministic rule found, as plain sentences.

    Rule-settled items carry one finding rather than scored features, so this reads that
    finding back. Routing, suggested action and risk notes are left out here because the
    screen shows each of them in its own place.
    """
    text = explanation or ""
    exact = _EXACT.search(text)
    if exact:
        return [f"Same amount on both: {money(int(exact[1]))}",
                f"Same reference on both: {exact[2]}",
                f"Same date on both: {long_date(exact[3])}",
                "No other record on either side has these same details"]
    finding = re.split(r" (?:Suggested action:|Routed to |Risk rules triggered:)", text)[0]
    sentences = [part.strip() for part in readable(finding).split(". ") if part.strip()]
    return [sentence[:1].upper() + sentence[1:].rstrip(".") for sentence in sentences]


_CONTROL = re.compile(r"declared (\d+)/(\d+) cents \(debit/credit\), stored (\d+)/(\d+)")
_DISCONTINUITY = re.compile(r"^(\d+) discontinuity\(ies\) across (\d+) rows$")


def check_message(text: str | None) -> str:
    """One file-check result in plain words, keeping every figure."""
    message = (text or "").strip()
    control = _CONTROL.search(message)
    if control:
        d1, c1, d2, c2 = (money(int(value)) for value in control.groups())
        return f"Control totals: debits {d1}, credits {c1}. The rows add up to debits {d2}, credits {c2}"
    gaps = _DISCONTINUITY.match(message)
    if gaps:
        return f"{gaps[1]} gap(s) in the running balance across {gaps[2]} rows"
    message = message.replace(", direction and description within", " and description within")
    message = message.replace("business day(s)", "business day")
    message = re.sub(r"dated (\S+), outside (\S+) to (\S+)", r"dated \1, outside the month (\2 to \3)", message)
    message = readable(message)
    return message[:1].upper() + message[1:]


def suggestion_sentence(kind: str, subject_kind: str, members: int, exception_code: str | None) -> str:
    """One sentence saying what the system thinks this record is."""
    if kind == "exception":
        return f"{labels.exception_name(exception_code)}. {labels.EXCEPTION_HELP.get(exception_code or '', '')}"
    if members == 1:
        return f"The system thinks this {subject_kind.lower()} matches one record on the other side."
    return (f"The system thinks this {subject_kind.lower()} matches {members} records on the other side "
            "that add up to the same amount.")


def sureness(band: str | None, exact: bool) -> str:
    if exact:
        return "Exact match"
    return {"High": "Fairly sure", "Medium": "Some doubt", "Low": "Unsure"}.get(band or "", "Not scored")


# ---------------------------------------------------------------------------
# What to do next, for the person using the screen
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NextStep:
    text: str
    action: str = ""          # button label; empty when there is nothing for this person to do
    url: str = ""
    method: str = "get"


def next_step(run, user, counts: dict[str, int], lists: dict[str, int], adjustments_waiting: int,
              reports_current: bool, pending_reopen: bool) -> NextStep:
    """The single most useful thing this person can do now."""
    run_id, status = run["run_id"], run["status"]
    if user is None:
        return NextStep("Choose who you are from \"Acting as\" at the top right to see what to do next.")
    role = user["role_code"]
    imports = permissions.is_permitted(role, permissions.IMPORT)

    if status in ("created", "validating", "validation_failed"):
        if status == "validation_failed":
            return NextStep("The files for this month failed their checks. Correct them and start the month again.",
                            "Start the month again" if imports else "", "/import")
        if imports:
            return NextStep("Upload this month's files and let the system check and match them.",
                            "Import the files", f"/runs/{run_id}/import")
        return NextStep("Waiting for an accountant to import this month's files.")

    if status == "closed":
        if pending_reopen:
            return NextStep("A request to reopen this month is waiting for the Controller.",
                            "View the request", f"/runs/{run_id}/close")
        return NextStep("This month is closed and locked.", "View the closed month", f"/runs/{run_id}/close")
    if status == "reopen_requested":
        if permissions.is_permitted(role, permissions.DECIDE_REOPEN):
            return NextStep("Someone has asked to reopen this month. Decide whether to allow it.",
                            "Decide the request", f"/runs/{run_id}/close")
        return NextStep("A request to reopen this month is waiting for the Controller.")

    senior = permissions.is_permitted(role, permissions.DECIDE_SENIOR)
    first = permissions.is_permitted(role, permissions.DECIDE_FIRST)
    if senior and lists.get("CAT-05"):
        return NextStep(f"{lists['CAT-05']} item(s) need a senior reviewer.", "Open senior review",
                        f"/runs/{run_id}/queues/CAT-05")
    if permissions.is_permitted(role, permissions.DECIDE_ADJUSTMENT) and adjustments_waiting:
        return NextStep(f"{adjustments_waiting} proposed adjustment(s) are waiting for approval.",
                        "Review adjustments", f"/runs/{run_id}/adjustments")
    if first:
        if lists.get("CAT-01"):
            return NextStep(f"{lists['CAT-01']} exact matches are ready. Most can be confirmed together.",
                            "Approve exact matches", f"/runs/{run_id}/batch")
        for code in ("CAT-02", "CAT-03", "CAT-04"):
            if lists.get(code):
                return NextStep(f"{lists[code]} item(s) in \"{labels.queue_name(code)}\" need your decision.",
                                f"Review {labels.queue_name(code).lower()}", f"/runs/{run_id}/queues/{code}")
    if counts.get("awaiting"):
        return NextStep(f"{counts['awaiting']} item(s) are still waiting for other reviewers.")
    if not reports_current:
        return NextStep("Every item has a decision. Update the reports to confirm them.",
                        "Update the reports", f"/runs/{run_id}/reports", "post")
    if permissions.is_permitted(role, permissions.SIGN_OFF):
        return NextStep("Everything is reviewed and the reports are up to date. Review the statement and close the month.",
                        "Close the month", f"/runs/{run_id}/close")
    return NextStep("Everything is reviewed. The Controller can now close the month.")


def progress_steps(run, counts: dict[str, int], reports_current: bool) -> list[dict[str, str]]:
    """The four stages of a month, each marked done, current or still to come."""
    status = run["status"]
    imported = status not in ("created", "validating", "validation_failed")
    reviewed = imported and counts.get("awaiting", 1) == 0
    closed = status in ("closed", "reopen_requested")
    states = [imported, reviewed, reviewed and reports_current, closed]
    details = [
        "Files checked and matched" if imported else "Waiting for files",
        f"{counts.get('decided', 0):,} of {counts.get('retained', 0):,} decided" if imported else "Not started",
        "Up to date" if reviewed and reports_current else "Not yet",
        "Signed off and locked" if closed else "Not yet",
    ]
    names = ["Import files", "Review", "Update reports", "Close the month"]
    current = next((i for i, done in enumerate(states) if not done), None)
    return [{"name": names[i], "detail": details[i],
             "state": "done" if states[i] else ("current" if i == current else "todo")} for i in range(4)]


def close_checklist(conditions, counts: dict[str, int], escalated: int, adjustments_waiting: int,
                    difference_cents: int) -> list[dict[str, Any]]:
    """CR-10 as a checklist in plain words, with the original wording kept for details."""
    plain_detail = {
        1: "Done" if counts.get("awaiting", 0) == 0 else f"{counts['awaiting']:,} still to review",
        2: "Done" if escalated == 0 else f"{escalated} waiting",
        3: "Done" if adjustments_waiting == 0 else f"{adjustments_waiting} waiting",
        4: "No tampering detected",
        5: "Up to date",
        6: "No difference" if difference_cents == 0
           else f"{money(difference_cents)} to explain when signing off",
    }
    rows = []
    for condition in conditions:
        detail = plain_detail[condition.number]
        if condition.number == 4 and not condition.met:
            detail = "Problem found: contact the system administrator"
        if condition.number == 5 and not condition.met:
            detail = "Not updated since the last change"
        rows.append({"label": labels.CLOSE_CONDITIONS[condition.number], "met": condition.met,
                     "detail": detail, "technical": f"CR-10 condition {condition.number}: {condition.detail}"})
    return rows
