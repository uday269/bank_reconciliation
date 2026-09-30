"""Candidate generation, including the capped group search (FR-AI-01..03, BR-03..05).

Finding which ledger entries could pair with a bank item is the expensive part of
reconciliation: comparing everything with everything is 612 x 628 comparisons for one
month, and worse for groups, where the number of possible subsets grows exponentially.

Two ideas keep it tractable and honest:

  * Blocking. Only items that share a direction of cash movement and fall inside the
    date window are considered at all, so each item is compared against tens of others
    rather than hundreds.
  * Disclosed caps. Group search stops at PRM-04 candidates and PRM-05 members. Finding
    subsets that sum to a target is NP-complete, so a limit is unavoidable; what matters
    is that a capped search reports itself (EXC-08) instead of quietly returning nothing
    (DD-07).

Pure functions: items in, candidates out. No database, no configuration file.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations
from typing import Iterable, Sequence

from app.domain.normalize import business_days_between
from app.domain.rules import Item

# A group is only worth searching when its members are plausibly small parts of the
# whole. Anything at or above this share of the target is a one-to-one candidate.
MAX_MEMBER_SHARE = 0.95

# Safety limit on search effort, expressed as a multiple of the configured candidate cap.
# Meet-in-the-middle is quadratic in the pool, so a window holding a few hundred items is
# comfortable; beyond that the item is reported as beyond the search (EXC-08).
POOL_GUARD_MULTIPLIER = 30


@dataclass(frozen=True)
class Candidate:
    """One possible answer for a subject item."""

    members: tuple[Item, ...]
    relationship: str                 # one_to_one, one_to_many, many_to_one

    @property
    def total_cents(self) -> int:
        return sum(item.amount_cents for item in self.members)

    @property
    def is_group(self) -> bool:
        return len(self.members) > 1

    def identity(self) -> tuple:
        """Stable key, so the same candidate is never proposed twice."""
        return tuple(sorted((item.item_type, item.item_id) for item in self.members))


@dataclass
class CandidateSet:
    """Everything found for one subject, and whether the search was complete."""

    subject: Item
    candidates: list[Candidate] = field(default_factory=list)
    considered: int = 0
    search_capped: bool = False
    cap_reason: str | None = None

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------

def opposite_side(subject: Item, pool: Sequence[Item]) -> list[Item]:
    """Items whose cash movement could explain the subject's.

    A bank credit is explained by a ledger debit: both increase cash. Comparing the
    signed cash effect handles the two sources' opposite conventions without rewriting
    either of them.
    """
    wanted_sign = 1 if subject.cash_effect > 0 else -1
    return [item for item in pool
            if (1 if item.cash_effect > 0 else -1) == wanted_sign]


def within_window(subject: Item, pool: Sequence[Item], window_business_days: int) -> list[Item]:
    """Items inside the timing window, in business days (BR-03)."""
    return [item for item in pool
            if abs(business_days_between(subject.business_date, item.business_date))
            <= window_business_days]


def block(subject: Item, pool: Sequence[Item], window_business_days: int) -> list[Item]:
    """The candidates worth scoring at all, before any amount test."""
    return within_window(subject, opposite_side(subject, pool), window_business_days)


# ---------------------------------------------------------------------------
# One-to-one candidates
# ---------------------------------------------------------------------------

def one_to_one_candidates(subject: Item, pool: Sequence[Item], *, window_business_days: int,
                          near_miss_tolerance_cents: int, limit: int) -> list[Candidate]:
    """Single items that could pair with the subject.

    Exact amounts first, then near misses within the tolerance. The tolerance exists so
    a transposed digit or a deducted fee still produces a candidate for a person to
    consider; it never produces a match on its own (BR-01 requires an exact amount).
    """
    target = abs(subject.cash_effect)
    blocked = block(subject, pool, window_business_days)

    exact = [item for item in blocked if item.amount_cents == target]
    near = [item for item in blocked
            if item.amount_cents != target
            and abs(item.amount_cents - target) <= near_miss_tolerance_cents]

    ordered = exact + sorted(near, key=lambda item: abs(item.amount_cents - target))
    return [Candidate((item,), "one_to_one") for item in ordered[:limit]]


# ---------------------------------------------------------------------------
# Group candidates
# ---------------------------------------------------------------------------

def group_candidates(subject: Item, pool: Sequence[Item], *, window_business_days: int,
                     candidate_cap: int, member_cap: int,
                     solution_limit: int = 5) -> tuple[list[Candidate], bool, str | None]:
    """Subsets of `pool` that sum exactly to the subject's amount (BR-04, BR-05).

    Returns the groups found, whether the search was capped, and why. A capped search is
    reported so the item becomes EXC-08 rather than appearing to have no answer (DD-07).

    Method: meet in the middle. Every subset of one or two items is summed once and
    indexed by total, then each of those totals is looked up against its complement. That
    finds every group of up to four members in time proportional to the square of the pool
    rather than to the number of subsets, so the whole date window can be searched instead
    of an arbitrary slice of it.

    Why this replaced a depth-first search over the largest candidates: keeping only the
    largest items is the wrong slice. A deposit of $5,376.40 made up of $705, $1,876, $770
    and $2,026 has members ranking 36th to 60th by size, so a search restricted to the top
    15 could never find it. Groups of five or more members are still out of reach, and that
    limit is reported rather than hidden.
    """
    target = abs(subject.cash_effect)
    relationship = "one_to_many" if subject.item_type == "bank" else "many_to_one"

    blocked = [item for item in block(subject, pool, window_business_days)
               if item.amount_cents < target * MAX_MEMBER_SHARE]

    capped = False
    cap_reason = None

    # The pool guard is a safety limit on work, not a slice of the search. Below it the
    # whole window is searched; above it the item is reported as beyond the search rather
    # than answered from an arbitrary subset.
    pool_guard = max(candidate_cap * POOL_GUARD_MULTIPLIER, candidate_cap)
    if len(blocked) > pool_guard:
        return [], True, (f"{len(blocked)} possible members within the date window exceeds the "
                          f"search limit of {pool_guard}")

    half = min(member_cap // 2, 2) or 1

    # Every subset of up to `half` members, indexed by total.
    partial: dict[int, list[tuple[Item, ...]]] = defaultdict(list)
    for size in range(1, half + 1):
        for combination in combinations(blocked, size):
            total = sum(item.amount_cents for item in combination)
            if total < target:
                partial[total].append(combination)

    found: list[Candidate] = []
    seen: set[tuple] = set()
    truncated = False

    def record(members: tuple[Item, ...]) -> bool:
        key = tuple(sorted((item.item_type, item.item_id) for item in members))
        if key in seen:
            return False
        seen.add(key)
        found.append(Candidate(tuple(members), relationship))
        return True

    for total, combinations_at_total in list(partial.items()):
        complement = target - total
        if complement <= 0 or complement not in partial:
            continue
        for left in combinations_at_total:
            left_ids = {(item.item_type, item.item_id) for item in left}
            for right in partial[complement]:
                if left_ids & {(item.item_type, item.item_id) for item in right}:
                    continue                          # an item cannot be its own partner
                if len(left) + len(right) > member_cap:
                    continue
                if record(left + right) and len(found) >= solution_limit:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break

    if truncated:
        capped = True
        cap_reason = (f"more than {solution_limit} groups sum to this amount, "
                      f"so the search stopped early")
    elif not found and len(blocked) > member_cap:
        # Nothing summed correctly within the member limit, and the pool was large enough
        # for the limit to have bound the search. A larger group might exist, and saying so
        # is the difference between "no answer" and "no answer we looked for" (DD-07).
        capped = True
        cap_reason = (f"no group of {member_cap} or fewer members sums to this amount; "
                      f"a larger group may exist")
    # When the pool holds no more items than the member cap, every subset was searched.
    # Reporting that as capped would raise an exception for an item that genuinely has
    # no group, which is a false alarm rather than a disclosure.
    return found, capped, cap_reason


# ---------------------------------------------------------------------------
# Generation for one subject
# ---------------------------------------------------------------------------

def generate(subject: Item, pool: Sequence[Item], *, window_business_days: int,
             near_miss_tolerance_cents: int, candidate_cap: int, member_cap: int,
             include_groups: bool = True) -> CandidateSet:
    """Every candidate worth scoring for one subject item."""
    result = CandidateSet(subject=subject)

    singles = one_to_one_candidates(
        subject, pool, window_business_days=window_business_days,
        near_miss_tolerance_cents=near_miss_tolerance_cents, limit=candidate_cap)
    result.candidates.extend(singles)

    if include_groups and member_cap >= 2:
        groups, capped, reason = group_candidates(
            subject, pool, window_business_days=window_business_days,
            candidate_cap=candidate_cap, member_cap=member_cap)
        seen = {candidate.identity() for candidate in result.candidates}
        for candidate in groups:
            if candidate.identity() not in seen:
                result.candidates.append(candidate)
                seen.add(candidate.identity())
        result.search_capped = capped
        result.cap_reason = reason

    result.considered = len(block(subject, pool, window_business_days))
    return result


def generate_all(subjects: Iterable[Item], pool: Sequence[Item], *, window_business_days: int,
                 near_miss_tolerance_cents: int, candidate_cap: int, member_cap: int,
                 include_groups: bool = True) -> list[CandidateSet]:
    """Generate for many subjects against one pool, sharing an index of the pool.

    Grouping the pool by date once turns the window filter from a scan of every item
    into a lookup of a few days, which is what keeps a full run inside its time budget.
    """
    by_date: dict[date, list[Item]] = defaultdict(list)
    for item in pool:
        by_date[item.business_date].append(item)
    dates = sorted(by_date)

    results = []
    for subject in subjects:
        nearby: list[Item] = []
        for day in dates:
            if abs((day - subject.business_date).days) <= window_business_days + 4:
                nearby.extend(by_date[day])          # calendar pre-filter, business days after
        results.append(generate(
            subject, nearby, window_business_days=window_business_days,
            near_miss_tolerance_cents=near_miss_tolerance_cents,
            candidate_cap=candidate_cap, member_cap=member_cap, include_groups=include_groups))
    return results
