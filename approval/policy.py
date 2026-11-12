"""The policy floor: the minimum outcome an invoice's flags require.

The policy is data: POLICY is an ordered list of rules, and the first one that matches decides.
That keeps it readable by the people who own it (it can be shown to a VP as a table) and
changeable without touching a prompt. Nothing here calls a model, so the same flags always give
the same decision.

It reads only an invoice's flags and their severities. Every loader, the repairs and the checks
have already said what is wrong; this says what to do about it.

    REJECT   not payable as submitted; goes back with the reasons
    HOLD     a person decides
    APPROVE  payable; any warnings are on record

It is a floor: later stages may make an outcome stricter (approve -> hold -> reject), never looser.
"""
from dataclasses import dataclass
from typing import Callable

APPROVE, HOLD, REJECT = "APPROVE", "HOLD", "REJECT"
STRICTNESS = (APPROVE, HOLD, REJECT)  # least to most strict

# How many fraud signals make a rejection. One alone is not fraud (an unreadable due date is a
# hold); two together are, provided at least one is HARD: a fact about the invoice, not a choice
# of words. Wording is weak evidence, since honest invoices ask for bank transfers and mention
# deadlines, so wording alone can hold an invoice for a person but never reject it.
FRAUD_SIGNALS_TO_REJECT = 2
HARD_SIGNALS = ("ZERO_STOCK_ITEM", "UNREADABLE_DUE_DATE")
WORDING_SIGNALS = ("PRESSURE_LANGUAGE", "WIRE_REQUEST")


def fraud_signals(flags: list[dict], kinds_wanted: tuple = HARD_SIGNALS + WORDING_SIGNALS) -> list[dict]:
    """The flags that count as fraud signals, at most one per kind.

    Four kinds, each weak alone: billing for an item with no stock at all, a due date that isn't
    a date ("yesterday"), wording that pushes for fast payment, and a request to pay by wire or
    to a changed account. Several flags of one kind (pressure wording in both the notes and the
    terms) are one signal, not two.
    """
    kinds = {
        "ZERO_STOCK_ITEM": lambda f: f["code"] == "STOCK_EXCEEDED" and f.get("available") == 0,
        "UNREADABLE_DUE_DATE": lambda f: f["code"] == "UNPARSEABLE" and f.get("field") == "due_date",
        "PRESSURE_LANGUAGE": lambda f: f["code"] == "PRESSURE_LANGUAGE",
        "WIRE_REQUEST": lambda f: f["code"] == "WIRE_REQUEST",
    }
    return [next(f for f in flags if is_kind(f)) for name, is_kind in kinds.items()
            if name in kinds_wanted and any(is_kind(f) for f in flags)]


def _blocking(flags):
    return [f for f in flags if f["severity"] == "BLOCK"]


def _fraud(flags):
    signals = fraud_signals(flags)
    return signals if len(signals) >= FRAUD_SIGNALS_TO_REJECT and fraud_signals(flags, HARD_SIGNALS) else []


def _fraud_wording(flags):
    wording = fraud_signals(flags, WORDING_SIGNALS)
    return wording if len(wording) == len(WORDING_SIGNALS) else []


def _review(flags):
    return [f for f in flags if f["severity"] == "REVIEW"]


def _near_threshold_with_warnings(flags):
    warnings = [f for f in flags if f["severity"] == "WARN"]
    near = [f for f in warnings if f["code"] == "NEAR_THRESHOLD"]
    return warnings if near and len(warnings) > len(near) else []


@dataclass(frozen=True)
class Rule:
    """One row of the policy: if `matches` returns any flags, the outcome is `outcome` because of them."""
    name: str
    outcome: str
    description: str
    matches: Callable[[list[dict]], list[dict]]


# In order: the first rule that matches decides. INFO notes are never a reason for anything.
POLICY = [
    Rule("BLOCKING_FLAG", REJECT, "Any BLOCK flag: the invoice can't be paid as submitted.", _blocking),
    Rule("FRAUD_SIGNALS", REJECT, "Two or more fraud signals (zero-stock item, unreadable due date, pressure "
         "wording, wire or changed-account request), at least one of them not wording.", _fraud),
    Rule("NEEDS_REVIEW", HOLD, "Any REVIEW flag: a person must decide.", _review),
    Rule("FRAUD_WORDING", HOLD, "Pressure wording together with a wire or changed-account request, "
         "and nothing else: suspicious, but words alone don't reject.", _fraud_wording),
    Rule("NEAR_THRESHOLD_WITH_WARNINGS", HOLD, "A total just under the approval threshold together with "
         "any other warning.", _near_threshold_with_warnings),
]
DEFAULT = Rule("NO_OBJECTION", APPROVE, "Nothing above applies: payable, with any warnings on record.", lambda flags: [])


@dataclass(frozen=True)
class Decision:
    """What the policy requires for one invoice, and why.

    outcome: APPROVE, HOLD or REJECT.
    rule: the name of the POLICY row that decided.
    because: the codes of the flags that row matched, in order, without repeats. Empty for an approval.
    warnings: the codes of WARN flags on an approved invoice, so "approved with warnings" is visible.
    """
    outcome: str
    rule: str
    because: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    stage: str = "POLICY_FLOOR"


def _codes(flags: list[dict]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(f["code"] for f in flags))


def decide(flags: list[dict]) -> Decision:
    """The policy floor for one invoice.

    Args:
        flags: The invoice's graded flags (each with "code" and "severity"), as normalizer.check
            leaves them, either on an Invoice or in the JSON output.

    Returns:
        The Decision from the first matching rule, or an approval if none matches.

    Raises:
        KeyError: A flag has no severity. Deliberate: an ungraded flag must not be approved past.
    """
    for rule in POLICY:
        matched = rule.matches(flags)
        if matched:
            return Decision(rule.outcome, rule.name, _codes(matched))
    for f in flags:
        f["severity"]  # every flag is graded, even on the approve path
    return Decision(DEFAULT.outcome, DEFAULT.name, (), warnings=_codes([f for f in flags if f["severity"] == "WARN"]))
