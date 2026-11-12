"""Approval: turn an invoice's flags into a decision.

    policy.py    the policy floor: an ordered table of rules, in code, and decide()
    reviewer.py  the reviewer agent: a model with lookup tools that explains, and may add a hold

The floor is the minimum outcome the flags require, and it never calls a model. The reviewer
agent may make the outcome stricter (an approval becomes a hold), never looser.
"""
from .policy import APPROVE, HOLD, POLICY, REJECT, Decision, Rule, decide, fraud_signals
from .reviewer import assess, needs_agent

__all__ = ["APPROVE", "HOLD", "POLICY", "REJECT", "Decision", "Rule", "assess", "decide", "fraud_signals", "needs_agent"]
