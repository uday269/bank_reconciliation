"""What each page shows, assembled from services and repositories.

No FastAPI here: every function takes the services and plain arguments and returns a
dictionary for a template. That keeps the screens testable without a server, and keeps
the routes down to a few lines each.

Two rules from the wireframes apply throughout:
  * Nothing is hidden because of confidence or because the user may not act on it.
    Low-confidence items, conflicting evidence and blocked actions are all shown (CR-15).
  * An action the selected identity may not perform is shown disabled, with the rule
    that blocks it, rather than removed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.control import ControlViolation, duties, period_lock, permissions
from app.control.permissions import ROLE_NAMES
from app.domain.risk import CATEGORIES as CATEGORY_NAMES
from app.domain.explanation import split_stored
from app.domain.risk import RISK_RULES, confidence_band
from app.services.adjustment_service import ADJUSTABLE_EXCEPTIONS, LIVE_STATUSES
from app.services.container import Services

CATEGORIES = ("CAT-01", "CAT-02", "CAT-03", "CAT-04", "CAT-05")
ITEM_TYPES = ("bank", "ledger", "carry_in")
EXTERNAL = {"bank": "external_txn_id", "ledger": "external_entry_id", "carry_in": "external_item_id"}
DATE = {"bank": "transaction_date", "ledger": "posting_date", "carry_in": "original_date"}
ID_COLUMN = {"bank": "bank_transaction_id", "ledger": "ledger_entry_id", "carry_in": "carry_in_item_id"}
AUDIT_PAGE_SIZE = 200


@dataclass(frozen=True)
class Permission:
    allowed: bool
    reason: str = ""


def can(user: sqlite3.Row | None, action: str) -> Permission:
    """Whether the selected identity may perform an action, and if not, why."""
    if user is None:
        return Permission(False, "select an identity first")
    if not permissions.is_permitted(user["role_code"], action):
        allowed = ", ".join(ROLE_NAMES[r] for r in sorted(permissions.PERMISSION_MATRIX[action]))
        return Permission(False, f"{ROLE_NAMES[user['role_code']]} cannot {action} (permitted for {allowed})")
    return Permission(True)


def base(services: Services, user: sqlite3.Row | None, run: sqlite3.Row | None,
         flashes: list[dict], title: str) -> dict[str, Any]:
    """Context every page needs: identity selector, run header and messages."""
    return {"title": title, "user": user, "run": run, "flashes": flashes,
            "users": services.repositories.users.active(), "role_names": ROLE_NAMES,
            "organization": services.config.organization.name}


def resolve_run(services: Services, run_id: int | None) -> sqlite3.Row | None:
    return services.repositories.runs.get(run_id) if run_id else services.repositories.runs.latest()


# ---------------------------------------------------------------------------
# W1 run dashboard
# ---------------------------------------------------------------------------

def queue_rows(services: Services, run_id: int, category: str) -> list[sqlite3.Row]:
    """The open items in one queue; CAT-05 also holds everything escalated (FR-REV-08)."""
    review = services.review
    return review.senior_queue(run_id) if category == "CAT-05" else review.queue(run_id, category)


def dashboard(services: Services, run_id: int | None, user: sqlite3.Row | None) -> dict[str, Any]:
    run = resolve_run(services, run_id)
    if run is None:
        return {"run": None, "initialized": bool(services.repositories.users.active())}
    run_id = run["run_id"]
    repos = services.repositories

    counts = {item_type: repos.items.status_counts(run_id, item_type) for item_type in ITEM_TYPES}
    total = {status: sum(c.get(status, 0) for c in counts.values())
             for status in {s for c in counts.values() for s in c}}
    retained = sum(total.values()) - total.get("excluded", 0)
    awaiting = total.get("proposed", 0) + total.get("escalated", 0)

    queues = []
    for category in CATEGORIES:
        rows = queue_rows(services, run_id, category)
        value = sum(repos.items.get(r["subject_item_type"], r["subject_item_id"])["amount_cents"] for r in rows)
        queues.append({"code": category, "name": CATEGORY_NAMES[category], "open": len(rows),
                       "value_cents": value, "high_risk": sum(1 for r in rows if r["risk_level"] == "high")})

    matching_started = run["status"] not in ("created", "validating", "validation_failed")
    readiness = services.period.readiness(run_id) if matching_started else None
    return {
        "run": run,
        "counts": {"bank": sum(counts["bank"].values()), "ledger": sum(counts["ledger"].values()),
                  "carry_in": sum(counts["carry_in"].values()), "retained": retained,
                  "decided": retained - awaiting, "awaiting": awaiting,
                  "reconciled": total.get("reconciled", 0), "unresolved": total.get("unresolved", 0)},
        "queues": queues,
        "readiness": readiness,
        "adjustments_awaiting": len(services.adjustments.awaiting_approval(run_id)),
        "pending_reopen": repos.periods.pending_reopen_request(run_id),
        "actions": {"import": can(user, permissions.IMPORT), "batch": can(user, permissions.BATCH),
                    "senior": can(user, permissions.DECIDE_SENIOR),
                    "sign_off": can(user, permissions.SIGN_OFF),
                    "request_reopen": can(user, permissions.REQUEST_REOPEN),
                    "decide_reopen": can(user, permissions.DECIDE_REOPEN)},
        "matching_started": matching_started,
    }


# ---------------------------------------------------------------------------
# Queue listing
# ---------------------------------------------------------------------------

def queue(services: Services, run_id: int, category: str, user: sqlite3.Row | None = None) -> dict[str, Any]:
    if category not in CATEGORIES:
        raise KeyError(category)
    repos = services.repositories
    rows = []
    for rec in queue_rows(services, run_id, category):
        item = repos.items.get(rec["subject_item_type"], rec["subject_item_id"])
        rows.append({
            "recommendation_id": rec["recommendation_id"], "kind": rec["kind"],
            "record": item[EXTERNAL[rec["subject_item_type"]]], "item_type": rec["subject_item_type"],
            "date": item[DATE[rec["subject_item_type"]]], "amount_cents": item["amount_cents"],
            "direction": item["direction"], "description": item["description_original"],
            "source": rec["rule_name"] or "AI scoring", "exception_code": rec["exception_code"],
            "confidence": rec["confidence"], "risk": rec["risk_level"],
            "risk_rules": json.loads(rec["risk_rules"] or "[]"),
            "escalated": item["status"] == "escalated", "category": rec["category_code"],
            "senior": senior_context(services, rec["recommendation_id"], user) if category == "CAT-05" else None})
    return {"code": category, "name": CATEGORY_NAMES[category], "rows": rows,
            "run": repos.runs.get(run_id)}


# ---------------------------------------------------------------------------
# W8 audit log
# ---------------------------------------------------------------------------

def audit_log(services: Services, run_id: int, event_type: str | None = None, page: int = 1) -> dict[str, Any]:
    events = services.audit.events(run_id)
    types = sorted({event["event_type"] for event in events})
    if event_type:
        events = [event for event in events if event["event_type"] == event_type]
    pages = max(1, -(-len(events) // AUDIT_PAGE_SIZE))
    page = min(max(page, 1), pages)
    shown = events[(page - 1) * AUDIT_PAGE_SIZE: page * AUDIT_PAGE_SIZE]
    users = {u["user_id"]: u["full_name"] for u in services.repositories.users.active()}
    rows = [{"sequence": e["sequence_no"], "created_at": e["created_at"], "event_type": e["event_type"],
             "process": e["process_code"],
             "actor": users.get(e["actor_user_id"], f"user {e['actor_user_id']}") if e["actor_type"] == "human" else "System",
             "entity": f"{e['entity_type']} {e['entity_id'] or ''}".strip(),
             "items": ", ".join(json.loads(e["item_refs"] or "[]")[:4]),
             "decision": e["decision"] or "", "rule": e["rule_name"] or "",
             "status": (f"{e['previous_status'] or ''} → {e['new_status'] or ''}"
                        if e["previous_status"] or e["new_status"] else ""),
             "comment": e["comment"] or "", "hash": e["event_hash"],
             "blocked": e["event_type"] == "BLOCKED_ATTEMPT"} for e in shown]
    return {"verification": services.audit.verify(run_id), "rows": rows, "types": types,
            "event_type": event_type or "", "page": page, "pages": pages, "total": len(events)}


# ---------------------------------------------------------------------------
# Reports and JSON status
# ---------------------------------------------------------------------------

def reports_index(services: Services, run_id: int) -> dict[str, Any]:
    current = {(row["report_code"], row["output_format"]): row
               for row in services.repositories.reports.current(run_id)}
    reports = []
    for number, title in enumerate(REPORT_TITLES, start=1):
        code = f"RPT-{number:02d}"
        html = current.get((code, "html"))
        reports.append({"code": code, "title": title, "generated_at": html["generated_at"] if html else None,
                        "hash": html["content_sha256"] if html else None})
    return {"reports": reports, "generated": bool(current)}


REPORT_TITLES = ("Data Import Report", "Data Quality Report", "Transformation Report", "Exact Match Report",
                 "AI Recommendation Report", "Exception Report", "Adjustment Report", "Approval Report",
                 "Reconciliation Summary", "Change and Override Report", "Match Report",
                 "AI Performance Report", "Complete Audit Log")


def run_status(services: Services, run_id: int) -> dict[str, Any]:
    run = services.repositories.runs.get(run_id)
    return {"run_id": run_id, "status": run["status"], "period_start": run["period_start"],
            "period_end": run["period_end"],
            "items": {item_type: services.repositories.items.status_counts(run_id, item_type)
                      for item_type in ITEM_TYPES},
            "queues": {category: len(queue_rows(services, run_id, category)) for category in CATEGORIES}}


# ---------------------------------------------------------------------------
# W3 / W5 item detail
# ---------------------------------------------------------------------------

DECISIONS = ("approve", "modify", "reject", "escalate", "unresolved")
DECISION_LABELS = {"approve": "Approve the proposal", "modify": "Approve the selected candidate",
                   "reject": "Reject: this pairing is wrong", "escalate": "Escalate to a senior reviewer",
                   "unresolved": "Leave unresolved"}
def split_explanation(text: str | None) -> dict[str, Any]:
    """The stored explanation in the blocks W3 shows; conflicting evidence stays separate (CR-15)."""
    return split_stored(text)


def _member_rows(services: Services, keys: list[tuple[str, int]]) -> list[dict[str, Any]]:
    rows = []
    for item_type, item_id in keys:
        item = services.repositories.items.get(item_type, item_id)
        rows.append({"item_type": item_type, "item_id": item_id, "record": item[EXTERNAL[item_type]],
                     "date": item[DATE[item_type]], "amount_cents": item["amount_cents"],
                     "direction": item["direction"], "description": item["description_original"],
                     "status": item["status"]})
    return rows


def _action_states(services: Services, rec: sqlite3.Row, subject_status: str, run: sqlite3.Row,
                   user: sqlite3.Row | None, has_alternative: bool) -> list[dict[str, Any]]:
    """Every decision the item could take, enabled or disabled with the blocking rule.

    The service re-checks everything when the form is posted; this only decides what the
    screen offers, so a reviewer sees why an action is unavailable before trying it.
    """
    common: str | None = None
    if user is None:
        common = "select an identity first"
    elif rec["status"] != "open":
        common = "this recommendation has already been decided"
    elif run["status"] not in period_lock.WORKING:
        common = ("the period is closed" if run["status"] in period_lock.LOCKED
                  else f"the run is {run['status'].replace('_', ' ')}")

    independence: str | None = None
    if common is None and permissions.decision_level(rec, subject_status) == "senior":
        history = services.repositories.decisions.for_recommendation(rec["recommendation_id"])
        try:
            duties.require_independent_senior(
                user["user_id"], services.repositories.decisions.escalated_by(rec["recommendation_id"]),
                [row["decided_by"] for row in history if row["decision_level"] == "first"])
        except ControlViolation as violation:
            independence = f"{violation.message} ({violation.rule})"

    states = []
    for decision in DECISIONS:
        reason = common or independence
        if reason is None:
            try:
                permissions.require_decision_permitted(user, rec, subject_status, decision)
            except ControlViolation as violation:
                reason = f"{violation.message} ({violation.rule})"
        if reason is None and decision == "modify" and not has_alternative:
            reason = "there is no other candidate to select; for an exception, use manual pairing"
        comment_required = decision in permissions.COMMENT_REQUIRED or (
            decision == "approve" and rec["risk_level"] == "high")
        offered = decision in permissions.offered_actions(rec, subject_status)
        states.append({"decision": decision, "label": DECISION_LABELS[decision], "allowed": reason is None,
                       "reason": reason or "", "comment_required": comment_required, "offered": offered})
    return states


def item_detail(services: Services, recommendation_id: int, user: sqlite3.Row | None) -> dict[str, Any]:
    repos = services.repositories
    rec = repos.recommendations.get(recommendation_id)
    run = repos.runs.get(rec["run_id"])
    subject_key = (rec["subject_item_type"], rec["subject_item_id"])
    subject = repos.items.get(*subject_key)
    subject_status = subject["status"]

    fields = [{"label": label, "original": subject[f"{column}_original"],
               "normalized": subject[f"{column}_normalized"] if f"{column}_normalized" in subject.keys() else None}
              for column, label in (("description", "Description"), ("reference", "Reference"))
              if f"{column}_original" in subject.keys()]
    for column, label in (("payee_normalized", "Payee"), ("counterparty_normalized", "Counterparty")):
        if column in subject.keys() and subject[column]:
            fields.append({"label": label, "original": "", "normalized": subject[column]})

    snapshot = json.loads(run["parameter_snapshot"])["parameters"]
    candidates = []
    for candidate in repos.recommendations.candidates(recommendation_id):
        features = json.loads(candidate["feature_values"] or "{}")
        keys = services.review._members(candidate["candidate_id"], services.database.connect())
        candidates.append({
            "candidate_id": candidate["candidate_id"], "rank": candidate["rank_order"],
            "score": candidate["score"], "confidence": candidate["confidence"],
            "band": confidence_band(candidate["confidence"], snapshot["confidence_high_band"],
                                    snapshot["confidence_low_band"]),
            "origin": "reviewer" if features.get("origin") == "reviewer" else "system",
            "features": {k: v for k, v in features.items() if k not in ("origin", "scored", "selected_by")},
            "members": _member_rows(services, [k for k in keys if k != subject_key])})

    history = [{"decision": row["decision"], "level": row["decision_level"], "by": repos.users.get(row["decided_by"])["full_name"],
                "comment": row["comment"] or "", "at": row["decided_at"], "batch": row["review_batch_id"],
                "status": f"{row['previous_status']} → {row['new_status']}"}
               for row in repos.decisions.for_recommendation(recommendation_id)]

    system_candidates = [c for c in candidates if c["origin"] == "system"]
    has_alternative = rec["kind"] == "match" and len(system_candidates) > 1
    risk_rules = [{"code": code, "name": RISK_RULES[code][0]} for code in json.loads(rec["risk_rules"] or "[]")]

    page: dict[str, Any] = {
        "rec": rec, "run": run, "subject": subject, "subject_ref": subject[EXTERNAL[subject_key[0]]],
        "subject_type": subject_key[0], "subject_date": subject[DATE[subject_key[0]]], "fields": fields,
        "candidates": candidates, "explanation": split_explanation(rec["explanation"]),
        "band": confidence_band(rec["confidence"], snapshot["confidence_high_band"], snapshot["confidence_low_band"]),
        "risk_rules": risk_rules, "history": history, "category_name": CATEGORY_NAMES[rec["category_code"]],
        "level": permissions.decision_level(rec, subject_status),
        "actions": _action_states(services, rec, subject_status, run, user, has_alternative),
        "queue": "CAT-05" if subject_status == "escalated" else rec["category_code"],
        "manual": None, "adjustment": None,
        "prose_enabled": services.prose.enabled,
    }
    if rec["kind"] == "exception" and rec["status"] == "open":
        keys = []
        for row in services.review.manual_match_options(recommendation_id, limit=40):
            item_type = next(t for t in ITEM_TYPES if ID_COLUMN[t] in row.keys())
            keys.append((item_type, row[ID_COLUMN[item_type]]))
        reason = _manual_reason(rec, subject_status, run, user)
        page["manual"] = {"options": _member_rows(services, keys), "allowed": reason is None, "reason": reason or ""}
    page["adjustment"] = adjustment_panel(services, rec, user)
    return page


def _manual_reason(rec: sqlite3.Row, subject_status: str, run: sqlite3.Row, user: sqlite3.Row | None) -> str | None:
    if user is None:
        return "select an identity first"
    if run["status"] not in period_lock.WORKING:
        return "the period is not open for decisions"
    try:
        permissions.require_manual_match_permitted(user, rec, subject_status)
    except ControlViolation as violation:
        return f"{violation.message} ({violation.rule})"
    return None


def adjustment_panel(services: Services, rec: sqlite3.Row, user: sqlite3.Row | None) -> dict[str, Any] | None:
    """W5: the suggested entry and any adjustments already proposed for this item."""
    if rec["exception_code"] not in ADJUSTABLE_EXCEPTIONS:
        return None
    existing = [{"adjustment_id": row["adjustment_id"], "amount_cents": row["amount_cents"],
                 "debit": row["debit_account_code"], "credit": row["credit_account_code"],
                 "rationale": row["rationale"], "status": row["status"],
                 "prepared_by": services.repositories.users.get(row["prepared_by"])["full_name"]}
                for row in services.repositories.adjustments.for_recommendation(rec["recommendation_id"])]
    permission = can(user, permissions.PROPOSE_ADJUSTMENT)
    live = [row for row in existing if row["status"] in LIVE_STATUSES]
    reason = permission.reason or (f"adjustment {live[0]['adjustment_id']} is already {live[0]['status']}" if live else "")
    if not reason and rec["status"] == "superseded":
        reason = "this recommendation was superseded"
    return {"suggestion": services.adjustments.suggestion(rec["recommendation_id"]), "existing": existing,
            "accounts": services.repositories.accounts.gl_accounts(), "allowed": not reason, "reason": reason}


# ---------------------------------------------------------------------------
# W4 batch, W6 senior queue context, adjustment approvals
# ---------------------------------------------------------------------------

def batch(services: Services, run_id: int, user: sqlite3.Row | None) -> dict[str, Any]:
    """Open exact matches with Low risk: the only items a batch may hold (CR-11)."""
    rows = []
    for rec in services.review.queue(run_id, "CAT-01"):
        if rec["risk_level"] != "low":
            continue
        _, leading = services.review._leading(rec, services.database.connect())
        members = _member_rows(services, leading)
        subject = next(m for m in members if (m["item_type"], m["item_id"]) ==
                       (rec["subject_item_type"], rec["subject_item_id"]))
        rows.append({"recommendation_id": rec["recommendation_id"], "rule": rec["rule_name"], "subject": subject,
                     "counterparts": [m for m in members if m is not subject]})
    return {"rows": rows, "run": services.repositories.runs.get(run_id), "permission": can(user, permissions.BATCH)}


def senior_context(services: Services, recommendation_id: int, user: sqlite3.Row | None) -> dict[str, Any]:
    """W6 columns: who escalated and why, and whether the selected user is blocked (CR-03)."""
    history = services.repositories.decisions.for_recommendation(recommendation_id)
    escalation = next((row for row in reversed(history) if row["decision"] == "escalate"), None)
    blocked = ""
    if user is not None:
        reviewers = {row["decided_by"] for row in history if row["decision_level"] == "first"}
        if (escalation is not None and escalation["decided_by"] == user["user_id"]) or user["user_id"] in reviewers:
            blocked = "you reviewed or escalated this item (CR-03)"
    return {"escalated_by": services.repositories.users.get(escalation["decided_by"])["full_name"] if escalation else "",
            "escalation_reason": escalation["comment"] if escalation else "", "blocked": blocked}


def adjustment_queue(services: Services, run_id: int, user: sqlite3.Row | None) -> dict[str, Any]:
    rows = []
    for row in services.repositories.adjustments.for_run(run_id):
        rec = services.repositories.recommendations.get(row["recommendation_id"])
        item = services.repositories.items.get(rec["subject_item_type"], rec["subject_item_id"])
        reason = can(user, permissions.DECIDE_ADJUSTMENT).reason
        if not reason and user is not None and user["user_id"] == row["prepared_by"]:
            reason = "you prepared this adjustment (CR-04)"
        if not reason and row["status"] != "proposed":
            reason = f"already {row['status']}"
        decisions = services.repositories.adjustments.decisions(row["adjustment_id"])
        rows.append({"adjustment_id": row["adjustment_id"], "recommendation_id": row["recommendation_id"],
                     "record": item[EXTERNAL[rec["subject_item_type"]]], "exception_code": rec["exception_code"],
                     "amount_cents": row["amount_cents"], "debit": row["debit_account_code"],
                     "credit": row["credit_account_code"], "rationale": row["rationale"],
                     "prepared_by": services.repositories.users.get(row["prepared_by"])["full_name"],
                     "prepared_at": row["prepared_at"], "status": row["status"],
                     "decided_by": services.repositories.users.get(decisions[-1]["decided_by"])["full_name"] if decisions else "",
                     "allowed": not reason, "reason": reason})
    return {"rows": rows, "run": services.repositories.runs.get(run_id),
            "totals": services.repositories.adjustments.totals_cents(run_id)}


# ---------------------------------------------------------------------------
# W2 import and validation
# ---------------------------------------------------------------------------

def import_page(services: Services, run_id: int | None, user: sqlite3.Row | None) -> dict[str, Any]:
    """A new-run form when run_id is None; otherwise the run's files and validation results."""
    permission = can(user, permissions.IMPORT)
    if run_id is None:
        return {"run": None, "permission": permission, "files": [], "tests": [], "summary": None}
    run = services.repositories.runs.get(run_id)
    files = services.repositories.source_files.for_run(run_id)
    results = services.repositories.validation.for_run(run_id)
    file_failures = [r for r in results if r["scope"] == "file" and r["outcome"] == "fail"]
    row_failures = [r for r in results if r["scope"] == "row" and r["outcome"] == "fail"]
    warnings = [r for r in results if r["outcome"] == "warning"]
    return {
        "run": run, "permission": permission, "files": files, "tests": results,
        "summary": {"passed": sum(1 for r in results if r["outcome"] == "pass"), "file_failures": file_failures,
                    "row_failures": row_failures, "warnings": warnings} if results else None,
        "can_upload": run["status"] == "created", "can_process": run["status"] == "validating",
        "failed": run["status"] == "validation_failed",
    }


# ---------------------------------------------------------------------------
# W7 close period and reopening
# ---------------------------------------------------------------------------

STATEMENT_SECTIONS = (("deposits_in_transit", "Deposits in transit"), ("outstanding_checks", "Outstanding checks"),
                      ("bank_originated", "Bank-originated items not yet recorded"),
                      ("unresolved", "Unresolved items carried forward"))


def close_page(services: Services, run_id: int, user: sqlite3.Row | None) -> dict[str, Any]:
    repos = services.repositories
    run = repos.runs.get(run_id)
    readiness = services.period.readiness(run_id)
    statement = readiness.statement

    sign_reason = can(user, permissions.SIGN_OFF).reason
    if not sign_reason and run["status"] in period_lock.LOCKED:
        sign_reason = "the period is already closed"
    if not sign_reason and not readiness.ready_for_signoff:
        sign_reason = "every close condition must be met first (CR-10)"
    if not sign_reason and run["status"] != "ready_to_close":
        sign_reason = "generate the report package to move the run to ready to close"
    if not sign_reason and user is not None:
        try:
            duties.require_independent_signer(user["user_id"], repos.decisions.deciders(run_id, "first"))
        except ControlViolation as violation:
            sign_reason = f"{violation.message} ({violation.rule})"

    pending = repos.periods.pending_reopen_request(run_id)
    request_reason = can(user, permissions.REQUEST_REOPEN).reason
    if not request_reason and run["status"] != "closed":
        request_reason = "only a closed period can be reopened"
    decide_reason = can(user, permissions.DECIDE_REOPEN).reason
    if not decide_reason and pending is not None and user is not None and pending["requested_by"] == user["user_id"]:
        decide_reason = "you requested this reopening (CR-06)"

    names = {u["user_id"]: u["full_name"] for u in repos.users.active()}
    signoffs = [{"by": names.get(row["signed_by"], row["signed_by"]), "at": row["signed_at"],
                 "difference_cents": row["unresolved_difference_cents"], "comment": row["comment"] or "",
                 "chain_head": row["chain_head_hash"]} for row in repos.periods.signoffs(run_id)]
    reopenings = [{"id": row["reopen_request_id"], "by": names.get(row["requested_by"], row["requested_by"]),
                   "at": row["requested_at"], "reason": row["reason"], "decision": row["decision"] or "awaiting decision",
                   "decided_by": names.get(row["decided_by"], "") if row["decided_by"] else "",
                   "comment": row["comment"] or "", "revised": row["revised_status"] or ""}
                  for row in repos.periods.reopen_history(run_id)]
    return {
        "run": run, "readiness": readiness, "statement": statement,
        "sections": [{"title": title, "lines": statement.section(name),
                      "total_cents": sum(line.amount_cents for line in statement.section(name))}
                     for name, title in STATEMENT_SECTIONS],
        "comment_required": statement.unresolved_difference_cents != 0,
        "signoffs": signoffs, "reopenings": reopenings, "pending": pending,
        "sign": Permission(not sign_reason, sign_reason),
        "request": Permission(not request_reason, request_reason),
        "decide": Permission(not decide_reason, decide_reason),
    }
