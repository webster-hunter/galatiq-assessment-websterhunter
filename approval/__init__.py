"""Approval: turn an invoice's flags into a decision.

    policy.py   the policy floor: an ordered table of rules, in code, and decide()

The floor is the minimum outcome the flags require, and it never calls a model. A reviewer agent
(not built yet) may make the outcome stricter, never looser.
"""
from .policy import APPROVE, HOLD, POLICY, REJECT, Decision, Rule, decide, fraud_signals

__all__ = ["APPROVE", "HOLD", "POLICY", "REJECT", "Decision", "Rule", "decide", "fraud_signals"]
