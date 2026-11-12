"""Approval: turn an invoice's flags into a decision.

    policy.py    the policy floor: an ordered table of rules, in code, and decide()
    reviewer.py  the reviewer agent: a model with lookup tools that explains, and may add a hold
    critic.py    the critic: checks the agent's assessment, in code and with a second model call

The floor is the minimum outcome the flags require, and it never calls a model. The reviewer
agent may make the outcome stricter (an approval becomes a hold), never looser.
"""
from .policy import APPROVE, HOLD, POLICY, REJECT, Decision, Rule, decide, fraud_signals
from .critic import MAX_ROUNDS, code_checks, critique
from .reviewer import assess, needs_agent

__all__ = ["APPROVE", "HOLD", "MAX_ROUNDS", "POLICY", "REJECT", "Decision", "Rule", "assess", "code_checks", "critique",
           "decide", "fraud_signals", "needs_agent"]
